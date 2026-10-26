"""Direct Anthropic provider checks using SDK-shaped fixtures only."""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from futureeval.config import Config
from futureeval.method import Question
from futureeval.providers import AnthropicProvider, messages_for
from futureeval.http import RemoteError
from futureeval.state import Ledger


SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"


class AnthropicProviderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        (self.folder / "ledger.json").write_text(json.dumps({
            "version": 1, "questions": {}, "calls": {},
        }), encoding="utf-8")
        self.persist = Mock()
        self.ledger = Ledger(self.folder, publish=self.persist)
        self.config = Config()
        self.question = Question.from_post({"id": 20, "question": {
            "id": 10, "title": "Will a synthetic event occur?", "type": "binary",
            "status": "open", "scheduled_close_time": "2099-01-01T00:00:00Z",
            "description": "A synthetic scenario.",
            "resolution_criteria": "Use the specified announcement.",
        }})
        self.research = '[{"url":"https://example.org/fixture","summary":"Fixture evidence"}]'
        self.messages = messages_for(self.question, self.research, 0)

    def response(self, model=SONNET, *, stop="end_turn", usage=None):
        if usage is None:
            usage = SimpleNamespace(input_tokens=100, output_tokens=200,
                                    cache_creation_input_tokens=1000,
                                    cache_read_input_tokens=2000)
        return SimpleNamespace(model=model, stop_reason=stop, usage=usage, content=[
            SimpleNamespace(type="thinking", thinking="Private internal fixture thought"),
            SimpleNamespace(type="text", text='{"reasoning":"Fixture evidence","probability":0.7}'),
        ])

    def provider(self, response=None):
        client = Mock()
        client.messages.create.return_value = response or self.response()
        provider = AnthropicProvider(self.config, self.ledger, "unit-test-placeholder", client=client)
        return provider, client

    def test_usage_categories_are_billed_separately(self):
        for model, expected_micros in ((SONNET, 4900), (OPUS, 9800)):
            with self.subTest(model=model):
                provider, _ = self.provider(self.response(model))
                key = "live:" + model
                result = provider.complete(key, model, self.messages)
                self.assertEqual(result["probability"], .7)
                self.assertEqual(self.ledger.spent(key=key), expected_micros)

    def test_cache_hit_uses_read_rate_without_double_counting_input(self):
        usage = SimpleNamespace(input_tokens=100, output_tokens=200,
                                cache_creation_input_tokens=0, cache_read_input_tokens=2000)
        provider, _ = self.provider(self.response(usage=usage))
        provider.complete("live:cache-hit", SONNET, self.messages)
        self.assertEqual(self.ledger.spent(key="live:cache-hit"), 2400)

    def test_cold_cache_write_is_not_reserved_as_a_cache_hit(self):
        provider, _ = self.provider()
        non_ascii = [{"role": "system", "content": "System"},
                     {"role": "user", "content": "Evidence λλλ"}]
        ceiling = provider.price(SONNET, non_ascii)
        text_bytes = len("System".encode()) + len("Evidence λλλ".encode())
        minimum_ceiling = (text_bytes + 4096) * 2.5 + self.config.max_tokens * 10
        self.assertGreaterEqual(ceiling, minimum_ceiling)

    def test_reservation_is_persisted_before_sdk_request(self):
        provider, client = self.provider()
        def create(**kwargs):
            disk = json.loads((self.folder / "ledger.json").read_text())
            self.assertTrue(any(call["status"] == "pending" for call in disk["calls"].values()))
            self.assertTrue(self.persist.called)
            return self.response()
        client.messages.create.side_effect = create
        provider.complete("live:write-ahead", SONNET, self.messages)

    def test_thinking_block_before_text_does_not_enter_parsed_forecast(self):
        provider, _ = self.provider()
        result = provider.complete("live:thinking", SONNET, self.messages)
        self.assertEqual(result, {"reasoning": "Fixture evidence", "probability": .7})
        self.assertNotIn("Private internal", json.dumps(result))

    def test_refusal_and_truncation_are_charged_then_rejected_without_retry(self):
        for stop in ("max_tokens", "refusal"):
            with self.subTest(stop=stop):
                provider, client = self.provider(self.response(stop=stop))
                with self.assertRaises(ValueError):
                    provider.complete("live:" + stop, SONNET, self.messages)
                self.assertEqual(client.messages.create.call_count, 1)
                self.assertEqual(self.ledger.spent(key="live:" + stop), 4900)

    def test_uncertain_sdk_failure_retains_ceiling_and_is_not_retried(self):
        provider, client = self.provider()
        client.messages.create.side_effect = TimeoutError("Synthetic timeout")
        ceiling = provider.price(SONNET, self.messages)
        with self.assertRaises(RemoteError):
            provider.complete("live:timeout", SONNET, self.messages)
        self.assertEqual(client.messages.create.call_count, 1)
        self.assertEqual(self.ledger.spent(key="live:timeout"), ceiling)

    def test_unexpected_billed_model_is_rejected_with_full_reservation(self):
        provider, _ = self.provider(self.response(OPUS))
        ceiling = provider.price(SONNET, self.messages)
        with self.assertRaises(ValueError):
            provider.complete("live:unexpected-model", SONNET, self.messages)
        self.assertEqual(self.ledger.spent(key="live:unexpected-model"), ceiling)

    def test_missing_or_malformed_usage_retains_full_reservation(self):
        invalid_usages = [None, SimpleNamespace(input_tokens=1),
                          SimpleNamespace(input_tokens=True, output_tokens=1,
                                          cache_creation_input_tokens=0, cache_read_input_tokens=0),
                          SimpleNamespace(input_tokens=1, output_tokens=-1,
                                          cache_creation_input_tokens=0, cache_read_input_tokens=0)]
        for index, usage in enumerate(invalid_usages):
            with self.subTest(usage=usage):
                response = self.response()
                response.usage = usage
                provider, client = self.provider(response)
                ceiling = provider.price(SONNET, self.messages)
                key = "live:missing-usage:" + str(index)
                try:
                    provider.complete(key, SONNET, self.messages)
                except ValueError:
                    pass
                self.assertEqual(self.ledger.spent(key=key), ceiling)
                self.assertEqual(client.messages.create.call_count, 1)

    def test_samples_share_only_the_stable_cache_prefix(self):
        first = messages_for(self.question, self.research, 0)
        second = messages_for(self.question, self.research, 1)
        self.assertEqual(first[0], second[0])
        first_blocks, second_blocks = first[1]["content"], second[1]["content"]
        self.assertEqual(first_blocks[0], second_blocks[0])
        self.assertEqual(first_blocks[0]["cache_control"], {"type": "ephemeral", "ttl": "5m"})
        self.assertIn("Fixture evidence", first_blocks[0]["text"])
        self.assertNotEqual(first_blocks[1]["text"], second_blocks[1]["text"])
        self.assertNotIn("cache_control", first_blocks[1])

    def test_sdk_request_has_explicit_effort_and_keeps_cache_block(self):
        provider, client = self.provider()
        provider.complete("live:request-shape", SONNET, self.messages)
        request = client.messages.create.call_args.kwargs
        self.assertEqual(request["model"], SONNET)
        self.assertEqual(request["max_tokens"], self.config.max_tokens)
        self.assertEqual(request["output_config"]["effort"], "low")
        self.assertEqual(request["messages"][0]["content"][0]["cache_control"],
                         {"type": "ephemeral", "ttl": "5m"})
        self.assertNotIn("tools", request)
        self.assertNotEqual(request.get("thinking", {}).get("type"), "disabled")

    def test_sdk_constructor_disables_automatic_retries(self):
        constructor = Mock(return_value=Mock())
        fake_module = SimpleNamespace(Anthropic=constructor)
        with patch.dict(sys.modules, {"anthropic": fake_module}):
            AnthropicProvider(self.config, self.ledger, "unit-test-placeholder")
        kwargs = constructor.call_args.kwargs
        self.assertEqual(kwargs["api_key"], "unit-test-placeholder")
        self.assertEqual(kwargs["max_retries"], 0)
        self.assertEqual(kwargs["timeout"], 120)


if __name__ == "__main__":
    unittest.main()
