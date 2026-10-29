"""Guard event log: write, rotate, read recent, summary, and it never raises."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from telemachus.fs import guard_log
from telemachus.fs.guard import LayaGuard
from telemachus.fs.models import FileActionType, GuardVerdict


class _Bridge:
    """Stands in for the Laya worker."""

    def __init__(self, result):
        self.result = result

    def evaluate_record(self, **kwargs):
        return self.result


class GuardLogCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "guard_events.jsonl"
        self._env = patch.dict(os.environ, {"TELEMACHUS_GUARD_LOG": str(self.path)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


class TestWriteAndRead(GuardLogCase):
    def test_write_then_read_recent_newest_first(self):
        self.assertTrue(guard_log.record("BLOCK", "~/.ssh/id_ed25519", "path shield", "no", action="read"))
        self.assertTrue(guard_log.record("ALLOW", "a.txt", "path shield", "ok", latency_ms=41.26))
        events = guard_log.read_recent(10)
        self.assertEqual([e["kind"] for e in events], ["ALLOW", "BLOCK"])
        self.assertEqual(events[0]["latency_ms"], 41.3)
        self.assertEqual(events[1]["action"], "read")
        self.assertNotIn("latency_ms", events[1])
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")

    def test_read_recent_limit_and_bad_lines(self):
        for i in range(5):
            guard_log.record("ALLOW", f"f{i}")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("not json\n{\"no\": \"kind\"}\n\n")
        events = guard_log.read_recent(2)
        self.assertEqual([e["target"] for e in events], ["f4", "f3"])
        self.assertEqual(len(guard_log.read_all()), 5)

    def test_off_writes_nothing(self):
        with patch.dict(os.environ, {"TELEMACHUS_GUARD_LOG": "off"}):
            self.assertIsNone(guard_log.log_path())
            self.assertFalse(guard_log.record("BLOCK", "x"))
        self.assertFalse(self.path.exists())

    def test_default_path_follows_home(self):
        with patch.dict(os.environ, {"HOME": self._tmp.name}):
            os.environ.pop("TELEMACHUS_GUARD_LOG", None)
            os.environ.pop("TELEMACHUS_MEMORY_DIR", None)
            self.assertEqual(guard_log.log_path(), Path(self._tmp.name) / ".memory" / "guard_events.jsonl")

    def test_secrets_are_masked_and_home_is_tilde(self):
        home = str(Path.home())
        guard_log.record("BLOCK", f"curl -H 'Authorization: Bearer abc' {home}/x?token=s3cret",
                         detail="key sk1234567890abcdefghijklmnopqrstuvwxyz used")
        ev = guard_log.read_recent(1)[0]
        self.assertNotIn("s3cret", ev["target"])
        self.assertNotIn("abc'", ev["target"])
        self.assertIn("~/x", ev["target"])
        self.assertNotIn("sk1234567890", ev["detail"])
        # ordinary paths and long snake_case names stay readable
        self.assertEqual(guard_log.redact("telemachus/cockpit/finance_view.py test_unmeasured_gate_latency_is_hidden"),
                         "telemachus/cockpit/finance_view.py test_unmeasured_gate_latency_is_hidden")

    def test_long_target_is_cut_in_the_middle(self):
        guard_log.record("ALLOW", "a" * 50 + "/" + "b" * 200)
        target = guard_log.read_recent(1)[0]["target"]
        self.assertLessEqual(len(target), guard_log.TARGET_MAX)
        self.assertIn("…", target)
        self.assertTrue(target.endswith("b"))


class TestRotate(GuardLogCase):
    def test_rotates_to_one_old_copy(self):
        with patch.object(guard_log, "MAX_BYTES", 2000):
            for i in range(60):
                guard_log.record("ALLOW", f"file_{i:03d}.txt", "path shield", "ok")
        old = Path(str(self.path) + ".1")
        self.assertTrue(old.exists())
        self.assertLessEqual(self.path.stat().st_size, 2000)
        self.assertLessEqual(old.stat().st_size, 2000)
        events = guard_log.read_all()
        self.assertEqual(events[-1]["target"], "file_059.txt")
        times = [e["ts"] for e in events]
        self.assertEqual(times, sorted(times))
        self.assertFalse(Path(str(self.path) + ".2").exists())


class TestNeverRaises(GuardLogCase):
    def test_unwritable_place(self):
        blocker = Path(self._tmp.name) / "file"
        blocker.write_text("x")
        with patch.dict(os.environ, {"TELEMACHUS_GUARD_LOG": str(blocker / "sub" / "log.jsonl")}):
            self.assertFalse(guard_log.record("BLOCK", "x"))
            self.assertEqual(guard_log.read_recent(5), [])
            self.assertEqual(guard_log.summary()["total"], 0)

    def test_os_error_while_writing(self):
        with patch("telemachus.fs.guard_log.os.open", side_effect=OSError("disk full")):
            self.assertFalse(guard_log.record("BLOCK", "x"))

    def test_odd_values(self):
        self.assertTrue(guard_log.record("block", object(), None, None, latency_ms=None))
        self.assertEqual(guard_log.read_recent(1)[0]["kind"], "BLOCK")

    def test_guard_still_decides_when_logging_breaks(self):
        with patch("telemachus.fs.guard.guard_log.record", side_effect=RuntimeError("boom")):
            d = LayaGuard(bridge=_Bridge(None)).evaluate(FileActionType.READ, "~/.ssh/id_rsa")
        self.assertEqual(d.verdict, GuardVerdict.BLOCK)


class TestSummary(GuardLogCase):
    def test_counts_today_only(self):
        now = time.time()
        guard_log.record("BLOCK", "old", "path shield", ts=now - 3 * 86400)
        guard_log.record("BLOCK", "a", "path shield", ts=now - 10)
        guard_log.record("BLOCK", "b", "intent check", latency_ms=38.0, ts=now - 9)
        guard_log.record("CHECKPOINT", "repo @ abc1234", "git checkpoint", ts=now - 8)
        guard_log.record("TRASH", "x", "soft delete", ts=now - 7)
        s = guard_log.summary(now=now)
        self.assertEqual(s["total"], 5)
        self.assertEqual(s["blocked_today"], 2)
        self.assertEqual(s["path_blocks_today"], 1)
        self.assertEqual(s["intent_blocks_today"], 1)
        self.assertEqual(s["trash_today"], 1)
        self.assertAlmostEqual(s["last_checkpoint_ts"], now - 8, places=2)
        self.assertEqual(s["last_intent_latency_ms"], 38.0)

    def test_empty_log(self):
        s = guard_log.summary()
        self.assertEqual(s["total"], 0)
        self.assertIsNone(s["last_intent_latency_ms"])
        self.assertIsNone(s["last_checkpoint_ts"])


class TestGuardWritesEvents(GuardLogCase):
    def test_block_and_allow_are_logged(self):
        guard = LayaGuard(bridge=_Bridge(None))
        guard.evaluate(FileActionType.READ, "~/.ssh/id_rsa")
        guard.evaluate(FileActionType.EXECUTE, "ls -la")
        events = guard_log.read_recent(5)
        self.assertEqual((events[1]["kind"], events[1]["rule"], events[1]["action"]), ("BLOCK", "path shield", "read"))
        self.assertEqual((events[0]["kind"], events[0]["rule"]), ("ALLOW", "command shield"))
        self.assertNotIn("latency_ms", events[0])

    def test_intent_check_records_its_time(self):
        guard = LayaGuard(bridge=_Bridge({"intent_alignment": "unaligned_mismatch", "alignment_confidence": 0.9}))
        d = guard.evaluate(FileActionType.WRITE, "/tmp/x.txt", user_intent="read the docs")
        self.assertEqual(d.verdict, GuardVerdict.BLOCK)
        ev = guard_log.read_recent(1)[0]
        self.assertEqual((ev["kind"], ev["rule"]), ("BLOCK", "intent check"))
        self.assertIsInstance(ev["latency_ms"], float)

    def test_confirmation_is_logged_as_confirm(self):
        guard = LayaGuard(bridge=_Bridge({"intent_alignment": "aligned", "alignment_confidence": 0.7,
                                          "action_risk": "destructive_loss"}))
        guard.evaluate(FileActionType.DELETE, "/tmp/x.txt", user_intent="clean up")
        self.assertEqual(guard_log.read_recent(1)[0]["kind"], "CONFIRM")


if __name__ == "__main__":
    unittest.main()
