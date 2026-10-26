import json
import logging

from .method import Question, aggregate, payload
from .metaculus import already_forecasted, still_open
from .providers import ensemble
from .state import BudgetExceeded, StateError

log = logging.getLogger(__name__)


def run_question(post, *, resolved, config, ledger, api, router, news):
    q = Question.from_post(post)
    if resolved:
        if q.status != "resolved":
            raise ValueError("Development mode accepts only resolved public questions")
    elif not still_open(q) or already_forecasted(post):
        return False
    if len(q.context()) > 30000:
        raise ValueError("Question context is too large to process within the budget")
    key = ("resolved:" if resolved else "live:") + str(q.id)
    if not ledger.claim(key):
        return False
    try:
        research = news.research(key, q)
        predictions, rationales = ensemble(key, q, research, config, ledger, router, resolved=resolved)
        prediction = aggregate(q, predictions, config.floor)
        if resolved:
            # This output is permitted only for confirmed resolved questions.
            log.info("resolved_result=%s", json.dumps({"post": q.post_id, "forecast": payload(q, prediction),
                                                       "pipeline_check_only": True}, allow_nan=False))
            ledger.mark(key, "resolved_complete")
            return True
        # Refresh eligibility immediately before committing to the external write.
        fresh = api.get_post(q.post_id)
        fresh_q = Question.from_post(fresh)
        if fresh_q != q or not still_open(fresh_q) or already_forecasted(fresh):
            ledger.mark(key, "changed_or_closed")
            return False
        comment = ("Autonomous API forecast. Independent models, median aggregation, no extremization.\n"
                   + "Final forecast (including probability guardrails):\n" + json.dumps(payload(q, prediction), allow_nan=False)
                   + "\nModel reasoning:\n" + json.dumps(rationales, ensure_ascii=False)
                   + "\nResearch sources and dates:\n" + research)
        ledger.mark(key, "submission_started")
        api.publish(q, prediction, comment)
        ledger.mark(key, "submitted")
        return True
    except StateError:
        raise
    except BudgetExceeded as error:
        ledger.mark(key, "budget_exhausted")
        log.info("question_id=%s forecasting stopped: %s", q.id, error)
        raise
    except Exception as error:
        # Never log response text, live forecasts, research, prompts, or exception strings.
        ledger.mark(key, "failed_or_uncertain")
        log.warning("question_id=%s stopped category=%s", q.id, type(error).__name__)
        return False
    finally:
        log.info("question_id=%s accounted_usd=%.6f utc_day_accounted_usd=%.6f utc_month_accounted_usd=%.6f", q.id,
                 ledger.spent(key=key) / 1_000_000, ledger.spent(day=ledger.day()) / 1_000_000,
                 ledger.spent(month=ledger.day()[:7]) / 1_000_000)
