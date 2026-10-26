"""Cloud entrypoint integration with no credentials or external service calls."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import main as entrypoint


class CloudEntrypointTests(unittest.TestCase):
    def test_resolved_pipeline_needs_only_anthropic_and_metaculus_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "ledger.json").write_text(json.dumps({"version": 1, "questions": {}, "calls": {}}))
            post = {"id": 20, "question": {"id": 10, "title": "A resolved fixture", "type": "binary",
                                          "status": "resolved", "scheduled_close_time": "2026-10-01T00:00:00Z"}}
            api = Mock()
            api.get_post.return_value = post
            client = Mock()

            def create(**kwargs):
                return SimpleNamespace(model=kwargs["model"], stop_reason="end_turn", usage=SimpleNamespace(
                    input_tokens=100, output_tokens=200, cache_creation_input_tokens=1000, cache_read_input_tokens=0),
                    content=[SimpleNamespace(type="text", text='{"reasoning":"Fixture evidence","probability":0.7}')])

            client.messages.create.side_effect = create
            constructor = Mock(return_value=client)
            env = {"GITHUB_ACTIONS": "true", "BOT_STATE_DIR": temp, "RESOLVED_POST_IDS": "20",
                   "METACULUS_TOKEN": "unit-test-placeholder", "ANTHROPIC_API_KEY": "unit-test-placeholder",
                   "SAMPLES_PER_MODEL": "", "MODEL_SAMPLE_COUNTS": ""}
            with (patch.dict("os.environ", env, clear=True),
                  patch.dict(sys.modules, {"anthropic": SimpleNamespace(Anthropic=constructor)}),
                  patch.object(sys, "argv", ["main.py", "--mode", "resolved"]),
                  patch.object(entrypoint, "Metaculus", return_value=api) as api_type,
                  patch.object(entrypoint, "git_publisher", return_value=lambda: None),
                  patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden in unit tests"))):
                entrypoint.main()
            self.assertEqual(api_type.call_args.kwargs["allow_publish"], False)
            api.publish.assert_not_called()
            self.assertEqual(client.messages.create.call_count, 6)
            self.assertEqual([call.kwargs["model"] for call in client.messages.create.call_args_list],
                             ["claude-sonnet-5-5"] * 4 + ["claude-opus-5-5"] * 2)
            state = json.loads((folder / "ledger.json").read_text())
            self.assertEqual(state["questions"]["resolved:10"]["status"], "resolved_complete")
            self.assertTrue(all(call["kind"].startswith("anthropic:") for call in state["calls"].values()))

    def test_exhausted_month_stops_before_provider_initialization(self):
        with tempfile.TemporaryDirectory() as temp:
            day = datetime.now(timezone.utc).date().isoformat()
            data = {"version": 1, "questions": {}, "calls": {"fixture": {
                "question": "live:old", "day": day, "days": [day], "reserved": 200_000_000,
                "charged": 200_000_000, "status": "known", "kind": "fixture"}}}
            (Path(temp) / "ledger.json").write_text(json.dumps(data))
            api = Mock()
            api.get_post.return_value = {"id": 20, "question": {"id": 10, "title": "Resolved fixture", "type": "binary", "status": "resolved"}}
            env = {"GITHUB_ACTIONS": "true", "BOT_STATE_DIR": temp, "RESOLVED_POST_IDS": "20",
                   "METACULUS_TOKEN": "unit-test-placeholder", "ANTHROPIC_API_KEY": "unit-test-placeholder"}
            with (patch.dict("os.environ", env, clear=True),
                  patch.object(sys, "argv", ["main.py", "--mode", "resolved"]),
                  patch.object(entrypoint, "Metaculus", return_value=api),
                  patch.object(entrypoint, "git_publisher", return_value=lambda: None),
                  patch.object(entrypoint, "ProviderRouter") as router,
                  self.assertLogs(level="INFO") as logs):
                entrypoint.main()
            router.assert_not_called()
            api.publish.assert_not_called()
            self.assertIn("monthly budget exhausted", " ".join(logs.output))

    def test_cloud_guard_runs_before_any_network_or_secret_checks(self):
        with (patch.dict("os.environ", {}, clear=True),
              patch.object(sys, "argv", ["main.py", "--mode", "resolved"]),
              patch.object(entrypoint, "Metaculus") as api):
            with self.assertRaisesRegex(ValueError, "GitHub Actions"):
                entrypoint.main()
            api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
