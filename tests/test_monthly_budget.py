"""Durable monthly accounting and calendar pacing. No external services."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from futureeval.config import Config
from futureeval.providers import ProviderRouter, QuestionContextResearch, ensemble
from futureeval.runner import run_question
from futureeval.state import BudgetExceeded, Ledger


class MonthlyBudgetTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        (self.folder / "ledger.json").write_text(json.dumps({"version": 1, "questions": {}, "calls": {}}))
        self.day = "2026-11-01"
        self.ledger = Ledger(self.folder, day=lambda: self.day)
        self.config = Config()

    def seed_cost(self, charged, day, *, days=None):
        self.ledger.data["calls"][str(len(self.ledger.data["calls"]))] = {
            "question": "live:old", "day": day, "days": days or [day], "reserved": charged,
            "charged": charged, "status": "known", "kind": "fixture"}
        self.ledger.save()

    def test_monthly_default_and_config_override(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Config.from_env().monthly_cap, 200_000_000)
            self.assertEqual(Config.from_env().sample_counts, (4, 2))
        with patch.dict("os.environ", {"MONTHLY_BUDGET_USD": "123.45"}, clear=True):
            self.assertEqual(Config.from_env().monthly_cap, 123_450_000)

    def test_daily_pacing_is_remaining_month_divided_by_inclusive_days(self):
        self.assertEqual(self.ledger.effective_daily_cap(self.config), 200_000_000 // 30)
        self.seed_cost(50_000_000, "2026-11-01")
        self.day = "2026-11-16"
        self.assertEqual(self.ledger.effective_daily_cap(self.config), 10_000_000)
        self.seed_cost(60_000_000, "2026-11-15")
        self.assertEqual(self.ledger.effective_daily_cap(self.config), 90_000_000 // 15)
        lower = replace(self.config, daily_cap=2_000_000)
        self.assertEqual(self.ledger.effective_daily_cap(lower), 2_000_000)

    def test_february_and_leap_year_remaining_days(self):
        self.day = "2028-02-01"
        self.assertEqual(self.ledger.effective_daily_cap(self.config), 200_000_000 // 29)
        self.day = "2027-02-01"
        self.assertEqual(self.ledger.effective_daily_cap(self.config), 200_000_000 // 28)

    def test_monthly_spend_is_durable_and_cap_blocks_after_reload(self):
        self.seed_cost(self.config.monthly_cap, self.day)
        restored = Ledger(self.folder, day=lambda: self.day)
        self.assertEqual(restored.spent(month="2026-11"), 200_000_000)
        with self.assertRaisesRegex(BudgetExceeded, "monthly"):
            restored.reserve("live:new", 1, self.config, "fixture")

    def test_month_reset_keeps_historical_costs_without_charging_new_month(self):
        self.seed_cost(self.config.monthly_cap, "2026-10-01")
        self.assertEqual(self.ledger.spent(month="2026-10"), 200_000_000)
        self.assertEqual(self.ledger.spent(month="2026-11"), 0)
        self.ledger.reserve("live:new", 1000, self.config, "fixture")

    def test_pending_request_counts_once_per_month_not_once_per_day(self):
        call = self.ledger.reserve("live:new", 1000, self.config, "fixture")
        self.assertEqual(self.ledger.spent(month="2026-11"), 1000)
        self.assertEqual(self.ledger.spent(day="2026-11-02"), 1000)
        restored = Ledger(self.folder, day=lambda: self.day)
        self.assertEqual(restored.spent(month="2026-11"), 1000)
        self.ledger.settle(call, 100)
        self.assertEqual(self.ledger.spent(month="2026-11"), 100)
        self.assertEqual(self.ledger.spent(day="2026-11-02"), 0)

    def test_month_boundary_hold_protects_both_months(self):
        self.day = "2026-10-31"
        call = self.ledger.reserve("live:new", 1000, self.config, "fixture")
        self.assertEqual(self.ledger.spent(month="2026-10"), 1000)
        self.assertEqual(self.ledger.spent(month="2026-11"), 1000)
        self.day = "2026-11-01"
        self.ledger.settle(call, 500)
        self.assertEqual(self.ledger.spent(month="2026-10"), 500)
        self.assertEqual(self.ledger.spent(month="2026-11"), 500)

    def test_paced_daily_cap_blocks_before_hard_daily_cap(self):
        self.seed_cost(6_500_000, self.day)
        with self.assertRaisesRegex(BudgetExceeded, "daily"):
            self.ledger.reserve("live:new", 1, self.config, "fixture")
        self.assertLess(self.ledger.spent(day=self.day), self.config.daily_cap)

    def test_hard_question_cap_still_applies(self):
        self.ledger.reserve("live:new", 1000, self.config, "fixture")
        with self.assertRaisesRegex(BudgetExceeded, "Question"):
            self.ledger.reserve("live:new", self.config.question_cap, self.config, "fixture")

    def test_global_sample_override_and_explicit_counts(self):
        with patch.dict("os.environ", {"SAMPLES_PER_MODEL": ""}, clear=True):
            self.assertEqual(Config.from_env().sample_counts, (4, 2))
        with patch.dict("os.environ", {"SAMPLES_PER_MODEL": "3"}, clear=True):
            self.assertEqual(Config.from_env().sample_counts, (3, 3))
        with patch.dict("os.environ", {"MODEL_SAMPLE_COUNTS": "3,1"}, clear=True):
            self.assertEqual(Config.from_env().sample_counts, (3, 1))
        with patch.dict("os.environ", {"MODEL_SAMPLE_COUNTS": "4"}, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()

    def test_configured_price_override_is_validated(self):
        prices = {"claude-sonnet-5-5": {"input": 3, "output": 15, "cache_write": 3.75, "cache_read": .15}}
        with patch.dict("os.environ", {"ANTHROPIC_PRICES_JSON": json.dumps(prices)}, clear=True):
            self.assertEqual(Config.from_env().anthropic_prices["claude-sonnet-5-5"]["input"], 3)
        prices["claude-sonnet-5-5"]["input"] = -1
        with patch.dict("os.environ", {"ANTHROPIC_PRICES_JSON": json.dumps(prices)}, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()

    def test_anthropic_only_does_not_contact_openrouter_even_when_key_present(self):
        send = Mock(side_effect=AssertionError("OpenRouter must not be contacted"))
        routed = ProviderRouter(self.config, self.ledger, anthropic_key="unit-test-placeholder",
                                openrouter_key="unused-unit-test-placeholder", anthropic_client=Mock(), send=send)
        self.assertEqual(set(routed.routes), set(self.config.models))
        send.assert_not_called()

    def test_explicit_openrouter_model_requires_key_before_contact(self):
        cfg = replace(self.config, models=("claude-sonnet-5-5", "openrouter:openai/fixture"), sample_counts=(4, 2))
        send = Mock()
        with self.assertRaises(ValueError):
            ProviderRouter(cfg, self.ledger, anthropic_key="unit-test-placeholder", anthropic_client=Mock(), send=send)
        send.assert_not_called()

    def test_optional_news_fallback_labels_the_missing_current_research(self):
        from futureeval.method import Question
        q = Question(1, 2, "binary", "A fixture question")
        with self.assertLogs("futureeval.providers", level="WARNING") as logs:
            report = QuestionContextResearch().research("live:2", q)
        self.assertIn("No live news search", report)
        self.assertIn("no current news research", logs.output[0])

    def test_budget_stop_logs_and_prevents_forecasting_calls(self):
        from futureeval.method import Question
        q = Question(1, 2, "binary", "A fixture question", close_time="2099-01-01T00:00:00Z")
        post = {"id": 1, "question": {"id": 2, "title": q.title, "type": "binary", "status": "open",
                "scheduled_close_time": q.close_time}}
        self.seed_cost(self.config.monthly_cap, self.day)
        router, news, api = Mock(), Mock(), Mock()
        news.research.return_value = "fixture research"
        router.price.return_value = 100
        with self.assertLogs("futureeval.runner", level="INFO") as logs, self.assertRaises(BudgetExceeded):
            run_question(post, resolved=False, config=self.config, ledger=self.ledger, api=api, router=router, news=news)
        router.complete.assert_not_called()
        api.publish.assert_not_called()
        self.assertIn("forecasting stopped", " ".join(logs.output))

    def test_default_ensemble_runs_all_samples_with_two_claude_models(self):
        from futureeval.method import Question
        q = Question(1, 2, "binary", "A fixture question")
        router = Mock()
        router.price.return_value = 100
        router.complete.return_value = {"reasoning": "fixture evidence", "probability": .6}
        predictions, _ = ensemble("live:2", q, "fixture", self.config, self.ledger, router)
        self.assertEqual(len(predictions), 6)
        self.assertEqual([call.args[1] for call in router.complete.call_args_list],
                         ["claude-sonnet-5-5"] * 4 + ["claude-opus-5-5"] * 2)


if __name__ == "__main__":
    unittest.main()
