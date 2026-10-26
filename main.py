"""Cloud entrypoint. All local checks use stubbed providers in tests/."""
import argparse
import logging
import os
from pathlib import Path

from futureeval.config import Config
from futureeval.http import RemoteError
from futureeval.metaculus import Metaculus
from futureeval.providers import AskNews, ProviderRouter, QuestionContextResearch
from futureeval.runner import run_question
from futureeval.state import BudgetExceeded, Ledger, StateError, git_publisher


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("tournament", "resolved"))
    args = parser.parse_args()
    # User policy: neither live nor resolved network forecasting runs on the local machine.
    if os.getenv("GITHUB_ACTIONS") != "true":
        raise ValueError("Network forecasting runs only in GitHub Actions. Use offline unit tests locally.")
    config = Config.from_env()
    resolved = args.mode == "resolved"
    required = ["METACULUS_TOKEN"]
    if any(m.startswith("claude-") for m in config.models):
        required.append("ANTHROPIC_API_KEY")
    if any(not m.startswith("claude-") for m in config.models):
        required.append("OPENROUTER_API_KEY")
    if any(not os.getenv(name) for name in required):
        raise ValueError("Required API credentials are missing from Actions secrets")
    folder = Path(os.environ["BOT_STATE_DIR"])
    publisher = git_publisher(folder, os.getenv("BOT_STATE_BRANCH", "bot-state"), os.getenv("BOT_STATE_REMOTE", "origin"))
    ledger = Ledger(folder, publish=publisher)
    # Reads need authentication too. The resolved client cannot publish.
    api = Metaculus(os.environ["METACULUS_TOKEN"], allow_publish=not resolved)
    if resolved:
        ids = [int(x.strip()) for x in os.getenv("RESOLVED_POST_IDS", "").split(",") if x.strip()]
        if not ids:
            raise ValueError("Resolved mode requires RESOLVED_POST_IDS")
        posts = [api.get_post(post_id) for post_id in ids]
        # Validate the whole batch before creating any paid provider clients.
        from futureeval.method import Question
        if any(Question.from_post(post).status != "resolved" for post in posts):
            raise ValueError("Resolved mode rejected an open or unresolved question")
    else:
        posts = []
        seen = set()
        for tournament in config.tournaments:
            for post in api.list_open(tournament):
                if post["id"] not in seen:
                    seen.add(post["id"])
                    posts.append(post)
        posts.sort(key=lambda p: (p.get("question") or {}).get("scheduled_close_time") or "9999")
    if not posts:
        logging.info("No eligible questions found")
        return
    try:
        ledger.check_budget(None, 1, config)
    except BudgetExceeded as error:
        logging.info("Forecasting stopped: %s", error)
        return
    router = ProviderRouter(config, ledger, anthropic_key=os.getenv("ANTHROPIC_API_KEY"),
                            openrouter_key=os.getenv("OPENROUTER_API_KEY"))
    if os.getenv("ASKNEWS_API_KEY") or (os.getenv("ASKNEWS_CLIENT_ID") and os.getenv("ASKNEWS_SECRET")):
        news = AskNews(config, ledger)
    else:
        news = QuestionContextResearch()
    completed = 0
    for post in posts:
        try:
            ledger.check_budget(None, 1, config)
            if not resolved:
                # List responses can omit fields. Forecast from the full detail response.
                post = api.get_post(post["id"])
            completed += bool(run_question(post, resolved=resolved, config=config, ledger=ledger, api=api, router=router, news=news))
        except BudgetExceeded as error:
            logging.info("Forecasting stopped: %s", error)
            break
        except (StateError, RemoteError):
            raise
        except (ValueError, KeyError, TypeError):
            logging.warning("Skipped unsupported or ineligible post_id=%s", post.get("id"))
    logging.info("completed=%s utc_day_accounted_usd=%.6f utc_month_accounted_usd=%.6f effective_daily_cap_usd=%.6f",
                 completed, ledger.spent(day=ledger.day()) / 1_000_000,
                 ledger.spent(month=ledger.day()[:7]) / 1_000_000, ledger.effective_daily_cap(config) / 1_000_000)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Do not expose SDK request bodies or provider errors through dependency logs.
    for name in ("anthropic", "asknews_sdk", "httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        main()
    except Exception as error:
        logging.error("Bot stopped category=%s; check configuration or state without inspecting live forecasts", type(error).__name__)
        raise SystemExit(1) from None
