"""Review round 2 on Home, Memory and Guard: bills before payday with the card first, plain guard
reasons, bad timestamps in the guard log, secrets in VAR=value form, and demo mode data paths."""
from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from telemachus.cockpit import email_panel, guard_view, home_view, memory_view
from telemachus.cockpit import view_common as vc
from telemachus.cockpit.email_panel import EmailTriagePanel, demo_mode
from telemachus.cockpit.guard_view import GuardData, build_guard
from telemachus.cockpit.home_view import HomeData, build_home, money_summary
from telemachus.cockpit.safety_memory_panel import SafetyMemoryPanel
from telemachus.cockpit.theme import line_text, line_width
from telemachus.fs import guard_log
from tests.test_hmg_views import NOW, TODAY, guard_status, home_data, runway, text

# The demo vault on 10/28: payday Fri Nov 6, the card due Mon Nov 2.
DEMO_TODAY = date(2026, 10, 28)


def demo_bills():
    return {"bills": [
        {"key": "pge", "name": "PG&E", "category": "Utility", "due_day": 28, "amount_cents": 6000,
         "status": "due", "is_active": True},
        {"key": "gym", "name": "Gym Membership", "category": "Fixed Bill", "due_day": 1, "amount_cents": 2500,
         "status": "due", "is_active": True},
        {"key": "icloud", "name": "Apple iCloud", "category": "Subscription", "due_day": 11, "amount_cents": 299,
         "status": "due", "is_active": True},
        {"key": "wifi", "name": "Wi-Fi", "category": "Utility", "due_day": 15, "amount_cents": 5500,
         "status": "due", "is_active": True},
    ]}


def demo_card():
    return {"owed_cents": 43200, "status": "owed", "next_due_date": "2026-11-02", "due_before_payday": True}


def next_line(lines):
    return next(line_text(l).rstrip() for l in lines if line_text(l).strip().startswith("next"))


class TestNextBills(unittest.TestCase):
    def money(self, mask=False):
        return money_summary(runway(), demo_bills(), demo_card(), DEMO_TODAY, mask=mask)

    def test_only_bills_before_payday(self):
        for w in (95, 120, 180):
            with self.subTest(w=w):
                nxt = next_line(home_view.money_block(self.money(), w - 1, DEMO_TODAY))
                self.assertNotIn("iCloud", nxt)
                self.assertNotIn("Wi-Fi", nxt)
                self.assertNotIn("more", nxt)
        rows = home_view.money_block(self.money(), 60, DEMO_TODAY, list_bills=True)
        joined = " ".join(line_text(l) for l in rows)
        self.assertNotIn("iCloud", joined)
        self.assertNotIn("Wi-Fi", joined)

    def test_card_always_shows_masked_or_not(self):
        for mask in (False, True):
            for w in (40, 50, 60, 80, 94):
                with self.subTest(mask=mask, w=w):
                    lines = home_view.money_block(self.money(mask), w, DEMO_TODAY)
                    self.assertIn("Visa", next_line(lines))
                    self.assertEqual(line_width(lines[2]), w)

    def test_shown_bills_stay_in_date_order_and_same_day_words(self):
        nxt = next_line(home_view.money_block(self.money(), 94, DEMO_TODAY))
        self.assertEqual(nxt.strip(), "next   PG&E $60.00 today   ·   Gym Membership $25.00 Sun   ·   Visa $432.00 Mon")
        self.assertNotIn("due", nxt)

    def test_bigger_bill_wins_when_only_two_fit(self):
        nxt = next_line(home_view.money_block(self.money(), 70, DEMO_TODAY))
        self.assertIn("Visa", nxt)
        self.assertIn("PG&E", nxt)  # $60 beats the $25 gym
        self.assertNotIn("Gym", nxt)
        self.assertIn("+1 more", nxt)
        self.assertLess(nxt.index("PG&E"), nxt.index("Visa"))

    def test_card_due_after_payday_is_left_out(self):
        card = dict(demo_card(), next_due_date="2026-11-20", due_before_payday=False)
        m = money_summary(runway(), demo_bills(), card, DEMO_TODAY)
        self.assertNotIn("Visa", next_line(home_view.money_block(m, 94, DEMO_TODAY)))

    def test_nothing_before_payday(self):
        m = money_summary(runway(), {"bills": demo_bills()["bills"][2:]}, None, DEMO_TODAY)
        self.assertIn("no bills before payday", next_line(home_view.money_block(m, 94, DEMO_TODAY)))

    def test_list_mode_caps_rows_with_more(self):
        bills = {"bills": [
            {"key": f"b{i}", "name": f"Bill {i}", "category": "Utility", "due_day": 29 + (i % 2),
             "amount_cents": 1000 + i, "status": "due", "is_active": True} for i in range(6)]}
        m = money_summary(runway(), bills, demo_card(), DEMO_TODAY)
        rows = [line_text(l).rstrip() for l in home_view.money_block(m, 60, DEMO_TODAY, list_bills=True)[2:]]
        self.assertEqual(len(rows), home_view.LIST_BILLS_MAX)  # 3 bills and the "+4 more" row
        self.assertIn("+4 more", rows[-1])
        self.assertTrue(any("Visa" in r for r in rows))


class TestPlainGuardReasons(unittest.TestCase):
    def detail(self, kind, detail):
        ev = {"ts": NOW - 60, "kind": kind, "target": "a.txt", "rule": "path shield", "detail": detail}
        return " ".join(line_text(l).strip() for l in guard_view._detail_lines(ev, 94, 2, NOW))

    def test_no_doubled_verb(self):
        cases = {
            "Permitted: Passed Tier 0 boundaries.": "Allowed: inside the safe paths.",
            "Permitted by Laya: Intent match verified (0.93).":
                "Allowed: the intent check says it matches your request (0.93).",
            "Read-only operation verified safe by Tier 0 boundaries.": "Allowed: read only, inside the safe paths.",
        }
        for engine, plain in cases.items():
            with self.subTest(engine=engine):
                self.assertEqual(self.detail("ALLOW", engine), plain)
        t = self.detail("TRASH", "Permitted under Tier 2: Deletion will be routed through macOS Trash.")
        self.assertEqual(t, "Moved to Trash: the delete goes to the Trash, so it can be undone.")
        self.assertNotIn("Permitted", self.detail("ALLOW", "Permitted by something new: Fine."))
        self.assertEqual(self.detail("BLOCK", "Tier 0 Violation: access is forbidden."),
                         "Blocked: Access is forbidden.")


class LogCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "guard_events.jsonl"
        self._env = patch.dict(os.environ, {"TELEMACHUS_GUARD_LOG": str(self.path)})
        self._env.start()
        for i in range(13):
            guard_log.record("BLOCK" if i == 0 else "ALLOW", f"f{i}.txt", "path shield", "ok", ts=NOW - 600 + i)
        guard_log.record("CHECKPOINT", "repo", "git checkpoint", "stash", ts=NOW - 120)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def append(self, *events):
        with open(self.path, "a", encoding="utf-8") as f:
            for ev in events:
                f.write((ev if isinstance(ev, str) else json.dumps(ev)) + "\n")

    def panel(self):
        return SafetyMemoryPanel(vault_root=Path(self._tmp.name) / "vault",
                                 audit_log=Path(self._tmp.name) / "audit.jsonl", guard_events=self.path)


class TestBadTimestamps(LogCase):
    def test_one_bad_line_hides_only_itself(self):
        self.append({"ts": "x", "kind": 5}, {"ts": None, "kind": "BLOCK"}, {"ts": True, "kind": "BLOCK"},
                    '{"ts": NaN, "kind": "BLOCK"}', '{"ts": Infinity, "kind": "BLOCK"}', {"ts": 1e400, "kind": "BLOCK"})
        events = guard_log.read_all()
        self.assertEqual(len(events), 14)
        s = guard_log.summary(now=NOW)
        self.assertEqual(s["blocked_today"], 1)
        self.assertEqual(s["last_checkpoint_ts"], NOW - 120)

    def test_millisecond_stamp_is_scaled(self):
        self.append({"ts": (NOW - 30) * 1000, "kind": "CHECKPOINT", "target": "x", "rule": "git checkpoint"})
        last = guard_log.read_recent(1)[0]
        self.assertAlmostEqual(last["ts"], NOW - 30, places=2)
        self.assertEqual(guard_log.summary(now=NOW)["last_checkpoint_ts"], last["ts"])

    def test_kind_and_latency_are_cleaned(self):
        self.append({"ts": NOW, "kind": "block", "target": 7, "latency_ms": "fast"},
                    {"ts": NOW + 1, "kind": "ALLOW", "latency_ms": float("inf")})
        a, b = guard_log.read_recent(2)
        self.assertEqual(b["kind"], "BLOCK")
        self.assertEqual(b["target"], "7")
        self.assertNotIn("latency_ms", b)
        self.assertNotIn("latency_ms", a)

    def test_guard_tab_and_home_never_crash(self):
        self.append({"ts": 1793240000123, "kind": "CHECKPOINT", "target": "x", "rule": "git checkpoint"},
                    {"ts": "x", "kind": 5}, '{"ts": 1e400, "kind": "ALLOW"}')
        g = self.panel().get_guard_status()
        self.assertEqual(g["log"]["total"], 15)
        for state in ({}, {"open": True, "sel": 0}, {"open": True, "sel": 14}):
            for w, h in ((95, 46), (70, 26), (40, 12)):
                with self.subTest(state=state, w=w):
                    f = build_guard(GuardData(g, now=NOW), dict(state), w, h)
                    self.assertTrue(all(line_width(l) == w for l in f.lines))
        t = text(build_home(home_data(guard_status=g), {}, 94, 40))
        self.assertNotIn("now ago", t)

    def test_view_helpers_take_any_value(self):
        for bad in ("x", None, float("inf"), float("nan"), 1e400, -5, True, [1]):
            with self.subTest(bad=bad):
                self.assertEqual(vc.clock(bad, NOW), "--:--")
                self.assertEqual(vc.ago(bad, NOW), "")
                self.assertIsNone(vc.safe_dt(bad))
        self.assertEqual(vc.ago_words(NOW + 3600, NOW), "just now")
        self.assertEqual(vc.clock(NOW * 1000, NOW), vc.clock(NOW, NOW))

    def test_memory_and_inbox_open_rows_survive_bad_times(self):
        f = {"id": "a1", "text": "t", "category": "fact", "tier": "private", "source": "chat",
             "ts": 1e400, "uses": 0, "pinned": False, "about": "", "quote": "", "kind": ""}
        lines = memory_view._meta_lines(f, 94, full=True)
        self.assertNotIn("saved", " ".join(line_text(l) for l in lines))
        mail = [{"kind": "urgent", "sender": "A", "subject": "s", "epoch": "soon", "snippet": "x"},
                {"kind": "urgent", "sender": "B", "subject": "s", "epoch": 1e400, "snippet": "x"}]
        t = text(build_home(home_data(emails=mail), {"sel": 0, "open": True}, 94, 40))
        self.assertIn("From A", t)


class TestSecretMasking(unittest.TestCase):
    def test_var_name_forms(self):
        cases = {
            "DB_PASSWORD=hunter2 ./run": "DB_PASSWORD=*** ./run",
            "export OPENAI_API_KEY=sk-abc123def": "export OPENAI_API_KEY=***",
            "GITHUB_TOKEN=abc12345 gh pr list": "GITHUB_TOKEN=*** gh pr list",
            "AWS_SECRET_ACCESS_KEY=abc/def+g run": "AWS_SECRET_ACCESS_KEY=*** run",
            'SECRET_KEY="a b c" run': "SECRET_KEY=*** run",
            "PASSWORD=hunter2": "PASSWORD=***",
            "curl -u sam:hunter2 https://x": "curl -u sam:*** https://x",
            "curl --user sam:hunter2 https://x": "curl --user sam:*** https://x",
            "mysql -phunter22": "mysql -p***",
            "mysql -u root -phunter22 db": "mysql -u root -p*** db",
            "git clone https://sam:pw123@host/x.git": "git clone https://sam:***@host/x.git",
            "tool --password s3cret --x": "tool --password *** --x",
            "sshpass -p hunter2 ssh h": "sshpass -p *** ssh h",
            'curl -H "Authorization: Bearer abc.def" x': 'curl -H "Authorization: Bearer ***" x',
        }
        for raw, masked in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(guard_log.short_target(raw), masked)

    def test_harmless_commands_unchanged(self):
        for raw in ("mkdir -p /tmp/x", "ssh -p 22 host", "mysql -u root -p mydb", "git commit --author=sam -m x",
                    "pip install token-bucket", "grep -r tokenizer src", "rm -rf ~/Projects/old_drafts",
                    "echo hello", "Basic setup steps"):
            with self.subTest(raw=raw):
                self.assertEqual(guard_log.short_target(raw), raw)

    def test_old_lines_are_masked_when_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "g.jsonl"
            path.write_text(json.dumps({"ts": NOW, "kind": "ALLOW", "target": "DB_PASSWORD=hunter2 ./run",
                                        "detail": "ran GITHUB_TOKEN=abc12345 gh"}) + "\n", encoding="utf-8")
            ev = guard_log.read_all(path)[0]
            self.assertNotIn("hunter2", json.dumps(ev))
            self.assertNotIn("abc12345", json.dumps(ev))

    def test_record_masks_the_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "g.jsonl"
            with patch.dict(os.environ, {"TELEMACHUS_GUARD_LOG": str(path)}):
                guard_log.record("ALLOW", "DB_PASSWORD=hunter2 ./run", "command shield", "ok")
            self.assertNotIn("hunter2", path.read_text(encoding="utf-8"))


class TestDemoMode(unittest.TestCase):
    def test_fake_home_never_reads_project_data(self):
        with tempfile.TemporaryDirectory() as fake:
            env = {"HOME": fake}
            with patch.dict(os.environ, env):
                os.environ.pop("TELEMACHUS_DEMO", None)
                os.environ.pop("TELEMACHUS_EMAIL_DB", None)
                self.assertTrue(demo_mode())
                self.assertNotEqual(EmailTriagePanel().db_path.name, "email_index.sqlite3")
                audit = SafetyMemoryPanel(vault_root=Path(fake) / "v").audit_log
                self.assertTrue(str(audit).startswith(fake), audit)

    def test_flag_wins(self):
        with patch.dict(os.environ, {"TELEMACHUS_DEMO": "1"}):
            self.assertTrue(demo_mode())
            self.assertNotEqual(EmailTriagePanel().db_path.name, "email_index.sqlite3")
        with tempfile.TemporaryDirectory() as fake, patch.dict(os.environ, {"HOME": fake, "TELEMACHUS_DEMO": "0"}):
            self.assertFalse(demo_mode())

    def test_real_home_is_not_demo(self):
        import pwd
        real = pwd.getpwuid(os.getuid()).pw_dir
        with patch.dict(os.environ, {"HOME": real}):
            os.environ.pop("TELEMACHUS_DEMO", None)
            self.assertFalse(demo_mode())

    def test_explicit_paths_still_win(self):
        with tempfile.TemporaryDirectory() as fake, patch.dict(os.environ, {"HOME": fake, "TELEMACHUS_DEMO": "1"}):
            self.assertEqual(EmailTriagePanel(db_path=Path(fake) / "x.sqlite3").db_path, Path(fake) / "x.sqlite3")
            self.assertEqual(SafetyMemoryPanel(vault_root=Path(fake) / "v", audit_log=Path(fake) / "a").audit_log,
                             Path(fake) / "a")


if __name__ == "__main__":
    unittest.main()
