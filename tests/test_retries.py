"""Recovery from transient failures, offline. No network or credentials are needed."""
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

import main as entrypoint
from futureeval.http import RemoteError, request
from futureeval.runner import run_question
from futureeval.state import Ledger, git_publisher
import test_pipeline
from test_pipeline import post


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError("https://example.org/fixture", code, "fixture", headers, None)


class RemoteRetryTests(unittest.TestCase):
    def test_get_retries_rate_limits_with_bounded_backoff(self):
        waits = []
        with patch("urllib.request.urlopen", side_effect=[http_error(429, 7), http_error(503), Response(b'{"ok": 1}')]):
            self.assertEqual(request("GET", "https://example.org/fixture", sleep=waits.append), {"ok": 1})
        self.assertEqual(waits, [7, 15])

    def test_get_gives_up_after_four_attempts(self):
        with patch("urllib.request.urlopen", side_effect=[http_error(429)] * 4) as urlopen:
            with self.assertRaisesRegex(RemoteError, "HTTP 429"):
                request("GET", "https://example.org/fixture", sleep=lambda s: None)
        self.assertEqual(urlopen.call_count, 4)

    def test_client_errors_and_posts_never_retry(self):
        with patch("urllib.request.urlopen", side_effect=[http_error(404)]) as urlopen:
            with self.assertRaisesRegex(RemoteError, "HTTP 404"):
                request("GET", "https://example.org/fixture", sleep=lambda s: None)
        self.assertEqual(urlopen.call_count, 1)
        with patch("urllib.request.urlopen", side_effect=[http_error(503)]) as urlopen:
            with self.assertRaises(RemoteError):
                request("POST", "https://example.org/fixture", payload={}, sleep=lambda s: None)
        self.assertEqual(urlopen.call_count, 1)


class ClaimRetryTests(unittest.TestCase):
    # Borrow the pipeline fixture without inheriting, and so rerunning, its tests.
    setUp = test_pipeline.OfflineFixture.setUp
    router = test_pipeline.OfflineFixture.router
    run_post = test_pipeline.OfflineFixture.run_post

    def write_question(self, status, **fields):
        data = json.loads((self.folder / "ledger.json").read_text())
        data["questions"]["live:111"] = {"status": status, "day": "2026-10-27", **fields}
        (self.folder / "ledger.json").write_text(json.dumps(data))
        self.ledger = Ledger(self.folder, publish=self.persist, day=lambda: self.day)

    def test_claim_left_by_a_dead_run_is_forecast_later(self):
        # Entries written before claims carried a timestamp count as stale.
        self.write_question("claimed")
        result, api, _ = self.run_post()
        self.assertTrue(result)
        api.publish.assert_called_once()
        self.assertEqual(self.ledger.data["questions"]["live:111"]["attempts"], 2)

    def test_recent_claim_is_left_alone(self):
        self.write_question("claimed", attempts=1, at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        result, api, _ = self.run_post()
        self.assertFalse(result)
        api.publish.assert_not_called()
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        self.write_question("claimed", attempts=1, at=old)
        self.assertTrue(self.ledger.claimable("live:111"))

    def test_submitted_uncertain_and_exhausted_questions_are_never_retried(self):
        for status, fields in (("submitted", {}), ("submission_started", {}), ("failed_or_uncertain", {}),
                               ("failed_before_submit", {"attempts": 3})):
            self.write_question(status, **fields)
            self.assertFalse(self.ledger.claimable("live:111"), status)

    def test_failure_before_submission_is_retryable(self):
        api, news = Mock(), Mock()
        api.get_post.return_value = post()
        news.research.side_effect = [RuntimeError("fixture outage"), "fixture"]
        kwargs = dict(resolved=False, config=self.config, ledger=self.ledger, api=api, news=news, router=self.router())
        self.assertFalse(run_question(post(), **kwargs))
        self.assertEqual(self.ledger.data["questions"]["live:111"]["status"], "failed_before_submit")
        self.assertTrue(run_question(post(), **kwargs))
        api.publish.assert_called_once()

    def test_listed_finished_question_skips_the_detail_request(self):
        self.write_question("submitted")
        env = {"GITHUB_ACTIONS": "true", "BOT_STATE_DIR": str(self.folder), "METACULUS_TOKEN": "unit-test-placeholder",
               "ANTHROPIC_API_KEY": "unit-test-placeholder", "TOURNAMENT_IDS": "fixture"}
        api = Mock()
        api.list_open.return_value = [post()]
        with (patch.dict("os.environ", env, clear=True),
              patch.object(sys, "argv", ["main.py", "--mode", "tournament"]),
              patch.object(entrypoint, "Metaculus", return_value=api),
              patch.object(entrypoint, "git_publisher", return_value=lambda: None),
              patch.dict(sys.modules, {"anthropic": Mock()}),
              patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden in unit tests"))):
            entrypoint.main()
        api.get_post.assert_not_called()
        api.publish.assert_not_called()


class PushRetryTests(unittest.TestCase):
    def test_transient_push_failure_is_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote, writer = root / "remote.git", root / "writer"

            def git(*args):
                result = subprocess.run(["git", *map(str, args)], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

            git("init", "--bare", "--initial-branch=bot-state", remote)
            git("init", "--initial-branch=bot-state", writer)
            (writer / "ledger.json").write_text(json.dumps({"version": 1, "questions": {}, "calls": {}}))
            git("-C", writer, "add", "ledger.json")
            git("-C", writer, "-c", "user.name=fixture", "-c", "user.email=fixture@example.org", "commit", "-m", "fixture")
            git("-C", writer, "remote", "add", "origin", remote)
            git("-C", writer, "push", "origin", "bot-state")
            # The remote rejects exactly one push, like a transient server error.
            hook = remote / "hooks" / "pre-receive"
            hook.write_text('#!/bin/sh\nif [ ! -f rejected-once ]; then touch rejected-once; exit 1; fi\nexit 0\n', newline="\n")
            hook.chmod(0o755)
            waits = []
            ledger = Ledger(writer, publish=git_publisher(writer, sleep=waits.append))
            self.assertTrue(ledger.claim("live:1"))
            self.assertEqual(waits, [5])
            shown = subprocess.run(["git", "-C", str(remote), "show", "bot-state:ledger.json"],
                                   capture_output=True, text=True, timeout=10)
            self.assertIn("live:1", shown.stdout)


if __name__ == "__main__":
    unittest.main()
