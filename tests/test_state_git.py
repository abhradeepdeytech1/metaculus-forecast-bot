"""Exercise the durable write path with tiny local git repositories only."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from futureeval.config import Config
from futureeval.state import Ledger, StateError, git_publisher


class GitStateTests(unittest.TestCase):
    def test_state_roundtrip_and_stale_writer_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote, writer, stale = root / "remote.git", root / "writer", root / "stale"

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
            git("clone", "--quiet", "--branch=bot-state", remote, stale)
            ledger = Ledger(writer, publish=git_publisher(writer))
            self.assertTrue(ledger.claim("live:1"))
            ledger.reserve("live:1", 1000, Config(), "fixture")
            git("-C", stale, "pull", "--ff-only")
            restored = Ledger(stale, publish=git_publisher(stale))
            self.assertFalse(restored.claim("live:1"))
            self.assertEqual(restored.spent(), 1000)
            ledger.claim("live:2")
            # The stale writer must fail before it can make any paid external request.
            with self.assertRaises(StateError):
                restored.reserve("live:3", 1000, Config(), "fixture")
            self.assertTrue(restored.poisoned)


if __name__ == "__main__":
    unittest.main()
