"""Small offline fixtures. No network, providers, or credentials are needed."""
from dataclasses import replace
import io
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from futureeval.config import Config, micros
from futureeval.http import RemoteError
from futureeval.metaculus import Metaculus
from futureeval.providers import AskNews, OpenRouter
from futureeval.runner import run_question
from futureeval.state import BudgetExceeded, Ledger, StateError


def post(kind="binary", status="open"):
    return {"id": 222, "question": {"id": 111, "title": "Will a fixture event happen?",
            "type": kind, "status": status, "scheduled_close_time": "2099-01-01T00:00:00Z",
            "resolution_criteria": "Use an official announcement."}}


class OfflineFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        (self.folder / "ledger.json").write_text(json.dumps({"version": 1, "questions": {}, "calls": {}}))
        self.persist = Mock()
        self.day = "2026-10-27"
        self.ledger = Ledger(self.folder, publish=self.persist, day=lambda: self.day)
        # Keep exercising the optional OpenRouter protocol with three fixture models.
        self.config = Config(models=("openai/gpt-6.1-sol", "anthropic/claude-sonnet-5.5", "google/gemini-4-argon"),
                             sample_counts=(2, 2, 2))

    def router(self, response=None):
        data = {"data": [{"id": m, "context_length": 100000, "pricing": {"prompt": ".000002", "completion": ".000010"}}
                         for m in self.config.models]}
        good = {"usage": {"cost": .001}, "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"reasoning": "Fixture evidence", "probability": .7})}}]}
        def send(method, url, **kwargs):
            if method == "GET":
                return data
            # Reservation must already exist in persisted state when provider is invoked.
            disk = json.loads((self.folder / "ledger.json").read_text())
            self.assertTrue(any(c["status"] == "pending" for c in disk["calls"].values()))
            return good if response is None else response
        return OpenRouter(self.config, self.ledger, "unit-test-placeholder", send=send)

    def run_post(self, p=None, resolved=False, router=None):
        api = Mock()
        api.get_post.return_value = p or post()
        news = Mock()
        news.research.return_value = '[{"url":"https://example.org/fixture","date":"2026-10-27"}]'
        result = run_question(p or post(), resolved=resolved, config=self.config, ledger=self.ledger,
                              api=api, news=news, router=router or self.router())
        return result, api, news

    def test_atomic_submission_and_private_reasoning(self):
        result, api, _ = self.run_post()
        self.assertTrue(result)
        q, probability, comment = api.publish.call_args.args
        self.assertEqual(q.id, 111)
        self.assertAlmostEqual(probability, .7)
        self.assertIn("Fixture evidence", comment)
        self.assertIn('"probability_yes": 0.7', comment)
        self.assertEqual(self.ledger.data["questions"]["live:111"]["status"], "submitted")
        self.assertEqual(self.ledger.spent(key="live:111"), 6000)

    def test_successful_question_is_never_run_again(self):
        self.run_post()
        result, api, news = self.run_post()
        self.assertFalse(result)
        api.publish.assert_not_called()
        news.research.assert_not_called()

    def test_interrupted_claim_survives_new_process(self):
        self.ledger.claim("live:111")
        self.ledger = Ledger(self.folder)
        result, api, news = self.run_post()
        self.assertFalse(result)
        news.research.assert_not_called()

    def test_unknown_submission_is_not_repeated(self):
        api, news = Mock(), Mock()
        api.get_post.return_value = post()
        api.publish.side_effect = RemoteError("fixture timeout")
        news.research.return_value = "fixture"
        kwargs = dict(resolved=False, config=self.config, ledger=self.ledger, api=api, news=news, router=self.router())
        self.assertFalse(run_question(post(), **kwargs))
        self.assertFalse(run_question(post(), **kwargs))
        self.assertEqual(api.publish.call_count, 1)

    def test_failed_persistence_prevents_paid_calls(self):
        self.persist.side_effect = RuntimeError("fixture push failed")
        router = self.router()
        with self.assertRaises(StateError):
            self.run_post(router=router)
        self.assertTrue(self.ledger.poisoned)
        self.assertEqual(self.ledger.data["calls"], {})

    def test_reservation_push_failure_prevents_provider_request(self):
        router = self.router()
        self.ledger.claim("live:111")
        self.persist.side_effect = RuntimeError("fixture push failed")
        with self.assertRaises(StateError):
            router.complete("live:111", self.config.models[0], [{"role": "user", "content": "fixture"}])

    def test_daily_budget_blocks_across_processes_and_questions(self):
        tiny = replace(self.config, daily_cap=100)
        call = self.ledger.reserve("live:1", 100, tiny, "fixture")
        self.ledger.settle(call, 100)
        fresh = Ledger(self.folder, day=lambda: self.day)
        with self.assertRaises(BudgetExceeded):
            fresh.reserve("live:2", 1, tiny, "fixture")
        self.day = "2026-10-28"
        fresh.reserve("live:2", 1, tiny, "fixture")

    def test_midnight_request_reserves_both_dates(self):
        tiny = replace(self.config, daily_cap=100)
        call = self.ledger.reserve("live:1", 100, tiny, "fixture")
        self.day = "2026-10-28"
        self.assertEqual(self.ledger.spent(day=self.day), 100)
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve("live:2", 1, tiny, "fixture")
        self.ledger.settle(call, 80)
        self.assertEqual(self.ledger.spent(day="2026-10-27"), 80)
        self.assertEqual(self.ledger.spent(day="2026-10-28"), 80)

    def test_day_changed_during_push_prevents_send(self):
        call = self.ledger.reserve("live:1", 100, self.config, "fixture")
        self.day = "2026-10-28"
        with self.assertRaises(BudgetExceeded):
            self.ledger.before_send(call)

    def test_question_cap_survives_day_rollover(self):
        tiny = replace(self.config, question_cap=100)
        self.ledger.reserve("live:1", 100, tiny, "fixture")
        self.day = "2026-10-28"
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve("live:1", 1, tiny, "fixture")

    def test_unknown_cost_keeps_full_reservation(self):
        router = self.router(response={"choices": [{"finish_reason": "stop", "message": {
            "content": '{"reasoning":"fixture","probability":0.5}'}}]})
        messages = [{"role": "user", "content": "fixture"}]
        ceiling = router.price(self.config.models[0], messages)
        router.complete("live:1", self.config.models[0], messages)
        self.assertEqual(self.ledger.spent(), ceiling)

    def test_underreported_ceiling_stops_pipeline(self):
        call = self.ledger.reserve("live:1", 100, self.config, "fixture")
        with self.assertRaises(StateError):
            self.ledger.settle(call, 101)
        self.assertEqual(self.ledger.spent(), 100)
        self.assertTrue(self.ledger.poisoned)

    def test_resolved_mode_never_posts(self):
        result, api, _ = self.run_post(post(status="resolved"), resolved=True)
        self.assertTrue(result)
        api.publish.assert_not_called()
        self.assertEqual(self.ledger.data["questions"]["resolved:111"]["status"], "resolved_complete")

    def test_resolved_mode_rejects_open_before_research(self):
        with self.assertRaises(ValueError):
            self.run_post(resolved=True)
        self.assertEqual(self.ledger.data["questions"], {})
        self.assertEqual(self.ledger.data["calls"], {})

    def test_old_forecast_is_skipped(self):
        p = post()
        p["question"]["my_forecasts"] = [{"forecast_values": [.6]}]
        result, api, news = self.run_post(p)
        self.assertFalse(result)
        news.research.assert_not_called()

    def test_live_forecasts_are_not_in_logs(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("futureeval.runner")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)
        self.run_post()
        self.assertNotIn("probability_yes", output.getvalue())
        self.assertNotIn("Fixture evidence", output.getvalue())

    def test_fewer_than_three_valid_models_prevents_post(self):
        router = self.router(response={"usage": {"cost": .001}, "choices": [{"finish_reason": "stop", "message": {
            "content": '{"reasoning":"fixture","probability":true}'}}]})
        result, api, _ = self.run_post(router=router)
        self.assertFalse(result)
        api.publish.assert_not_called()

    def test_changed_question_is_not_posted(self):
        api, news = Mock(), Mock()
        changed = post()
        changed["question"]["resolution_criteria"] = "Changed criteria"
        api.get_post.return_value = changed
        news.research.return_value = "fixture"
        self.assertFalse(run_question(post(), resolved=False, config=self.config, ledger=self.ledger,
                                      api=api, news=news, router=self.router()))
        api.publish.assert_not_called()

    def test_atomic_api_payload(self):
        from futureeval.method import Question
        send = Mock(return_value={})
        client = Metaculus("unit-test-placeholder", send=send, allow_publish=True)
        client.publish(Question.from_post(post()), .65, "fixture reasoning")
        args, kwargs = send.call_args
        self.assertTrue(args[1].endswith("/questions/bulk-forecast-comment/"))
        self.assertEqual(kwargs["payload"]["forecasts"][0]["question"], 111)
        self.assertEqual(kwargs["payload"]["comments"][0]["on_post"], 222)
        self.assertTrue(kwargs["payload"]["comments"][0]["is_private"])

    def test_read_only_client_cannot_publish_even_with_token(self):
        from futureeval.method import Question
        send = Mock(return_value={})
        client = Metaculus("unit-test-placeholder", send=send)
        with self.assertRaises(ValueError):
            client.publish(Question.from_post(post()), .65, "fixture")
        send.assert_not_called()

    def test_empty_own_forecast_container_is_not_a_forecast(self):
        p = post()
        p["question"]["my_forecasts"] = {"latest": None, "history": []}
        result, api, news = self.run_post(p)
        self.assertTrue(result)
        api.publish.assert_called_once()

    def test_news_reserves_before_sdk_search(self):
        from types import SimpleNamespace
        sdk = Mock()
        def search(**kwargs):
            self.assertTrue(self.persist.called)
            return SimpleNamespace(as_dicts=[SimpleNamespace(article_url="https://example.org/fixture", pub_date="2026-10-27",
                                                              eng_title="Fixture", summary="News")])
        sdk.news.search_news.side_effect = search
        cfg = replace(self.config, news_cost=1000)
        news = AskNews(cfg, self.ledger, client=sdk)
        from futureeval.method import Question
        self.assertIn("https://example.org/fixture", news.research("live:111", Question.from_post(post())))
        self.assertEqual(self.ledger.spent(), 2000)

    def test_missing_ledger_is_not_silently_reset(self):
        with self.assertRaises(StateError):
            Ledger(self.folder / "missing")

    def test_corrupt_negative_spend_is_rejected(self):
        self.ledger.reserve("live:1", 100, self.config, "fixture")
        data = json.loads((self.folder / "ledger.json").read_text())
        next(iter(data["calls"].values()))["charged"] = -1
        (self.folder / "ledger.json").write_text(json.dumps(data))
        with self.assertRaises(StateError):
            Ledger(self.folder)

    def test_environment_only_configuration_and_invalid_values(self):
        with patch.dict("os.environ", {"DAILY_BUDGET_USD": "2.25"}, clear=True):
            self.assertEqual(Config.from_env().daily_cap, 2250000)
        with patch.dict("os.environ", {"FORECAST_MODELS": "a,a,a"}, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()
        for bad in ("NaN", "Infinity", "-1"):
            with self.assertRaises(ValueError):
                micros(bad)


if __name__ == "__main__":
    unittest.main()
