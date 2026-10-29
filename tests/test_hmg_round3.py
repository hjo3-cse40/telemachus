"""Review round 3 on Home, Memory and Guard: a bill paid early for next month, the selection
following its item across a refresh, and guard log masking that stays fast on huge text."""
from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from telemachus.cockpit import guard_view, home_view, memory_view
from telemachus.cockpit import view_common as vc
from telemachus.cockpit.guard_view import GuardData, build_guard
from telemachus.cockpit.home_view import HomeData, build_home, money_summary, next_month
from telemachus.cockpit.memory_view import MemoryData, build_memory
from telemachus.cockpit.shell import Panels
from telemachus.cockpit.theme import line_text
from telemachus.fs import guard_log
from tests.test_hmg_views import NOW, emails, facts, guard_status, home_data, mem_stats, runway

DOWN = vc.DOWN_KEYS[1]
ENTER = vc.ENTER_KEYS[0]


def wifi(status):
    return {"bills": [{"key": "wifi", "name": "Wi-Fi", "category": "Utility", "due_day": 1,
                       "amount_cents": 4000, "status": status, "is_active": True}]}


class TestBillPaidEarly(unittest.TestCase):
    """Wi-Fi is due on the 1st. On Oct 28 its next due date is Nov 1 (before payday Nov 6)."""

    today = date(2026, 10, 28)

    def test_paid_for_next_month_is_not_listed(self):
        # This month's breakdown says paid (Oct 1); next month's also says paid (logged Oct 28).
        out = money_summary(runway(), wifi("paid"), None, self.today, next_breakdown=wifi("paid"))
        self.assertEqual(out["bills"], [])

    def test_paid_this_month_but_due_next_month_is_listed(self):
        # The Oct 1 bill was paid, the Nov 1 one was not: it is still due before payday.
        out = money_summary(runway(), wifi("paid"), None, self.today, next_breakdown=wifi("due"))
        self.assertEqual([(b["name"], b["date"], b["before_payday"]) for b in out["bills"]],
                         [("Wi-Fi", "2026-11-01", True)])

    def test_this_month_status_still_used_for_a_bill_due_this_month(self):
        bills = {"bills": [dict(wifi("paid")["bills"][0], due_day=30)]}
        out = money_summary(runway(), bills, None, self.today, next_breakdown=wifi("due"))
        self.assertEqual(out["bills"], [])

    def test_next_month_wraps_the_year(self):
        self.assertEqual(next_month(date(2026, 12, 15)), "2027-01")
        self.assertEqual(next_month(date(2026, 10, 28)), "2026-11")

    def test_load_asks_for_next_months_breakdown(self):
        months = []

        class FakeFinance:
            store = None

            def get_runway(self):
                return runway()

            def get_daily_ledger(self):
                return [{"date": "2026-10-28"}]

            def get_monthly_bills_breakdown(self, month=None):
                months.append(month)
                return wifi("paid")

            def get_card_summary(self):
                return None

        class Empty:
            def __getattr__(self, name):
                return lambda *a, **k: [] if name.startswith("get_recent") or name == "get_triage_items" else {}

        d = home_view.load(Panels(email_panel=Empty(), safety_panel=Empty(), finance_panel=FakeFinance()))
        self.assertIn(next_month(date.today()), months)
        self.assertEqual(d.money["bills"], [])


def _sel_text(frame):
    return next(line_text(l) for l in frame.lines if line_text(l).startswith("▸"))


class TestSelectionFollowsItem(unittest.TestCase):
    def test_home_new_mail_above_keeps_the_open_mail(self):
        d = home_data()
        state = {}
        state.update(build_home(d, state, 94, 40).state)
        for ch in (DOWN, DOWN, ENTER):
            home_view.handle_key(ch, state, d)
        state.update(build_home(d, state, 94, 40).state)
        before = _sel_text(build_home(d, state, 94, 40))
        self.assertIn("Sender 2", before)
        new = {"badge": "URGENT", "kind": "urgent", "sender": "New Sender", "subject": "Fresh",
               "date": "2026-10-28", "epoch": NOW - 10, "snippet": "new", "uid": 999}
        d2 = home_data(emails=[new] + emails())
        f = build_home(d2, state, 94, 40)
        state.update(f.state)
        self.assertIn("Sender 2", _sel_text(f))
        self.assertTrue(state.get("open"))
        self.assertEqual(state["sel"], 3)
        # j from there moves to the next mail after Sender 2, not from the old index.
        home_view.handle_key(DOWN, state, d2)
        self.assertIn("Sender 4", _sel_text(build_home(d2, state, 94, 40)))

    def test_home_selected_mail_gone_closes_preview(self):
        d = home_data()
        state = {}
        state.update(build_home(d, state, 94, 40).state)
        home_view.handle_key(DOWN, state, d)
        home_view.handle_key(ENTER, state, d)
        gone = [e for e in emails() if e["sender"] != "Sender 1"]
        f = build_home(home_data(emails=gone), state, 94, 40)
        self.assertIs(f.state.get("open"), False)

    def test_guard_new_event_above_keeps_the_selection(self):
        g = guard_status()
        d = GuardData(g, now=NOW)
        state = {}
        state.update(build_guard(d, state, 94, 40).state)
        guard_view.handle_key(DOWN, state, d)
        guard_view.handle_key(ENTER, state, d)
        state.update(build_guard(d, state, 94, 40).state)
        self.assertIn("CHECKPOINT", _sel_text(build_guard(d, state, 94, 40)))
        g2 = dict(g, events=[{"ts": NOW - 5, "kind": "TRASH", "target": "~/old.txt", "rule": "trash",
                              "detail": "moved", "action": "delete"}] + g["events"])
        f = build_guard(GuardData(g2, now=NOW), state, 94, 40)
        self.assertIn("CHECKPOINT", _sel_text(f))
        self.assertEqual(f.state["sel"], 2)

    def test_memory_new_fact_above_keeps_the_selection(self):
        d = MemoryData(memory_stats=mem_stats(), facts=facts(), now=NOW)
        state = {}
        state.update(build_memory(d, state, 94, 40).state)
        for _ in range(3):
            memory_view.handle_key(DOWN, state, d)
        state.update(build_memory(d, state, 94, 40).state)
        self.assertIn("fact number 3 ", _sel_text(build_memory(d, state, 94, 40)))
        new = dict(facts(1)[0], id="brand-new", text="a brand new fact", ts=NOW)
        d2 = MemoryData(memory_stats=mem_stats(16), facts=[new] + facts(), now=NOW)
        f = build_memory(d2, state, 94, 40)
        self.assertIn("fact number 3 ", _sel_text(f))
        self.assertEqual(f.state["sel"], 4)

    def test_memory_search_still_starts_at_the_top(self):
        d = MemoryData(memory_stats=mem_stats(), facts=facts(), now=NOW)
        state = {}
        state.update(build_memory(d, state, 94, 40).state)
        for _ in range(3):
            memory_view.handle_key(DOWN, state, d)
        memory_view.handle_key(ord("/"), state, d)
        memory_view.handle_key(ord("f"), state, d)
        f = build_memory(d, state, 94, 40)
        self.assertEqual(f.state["sel"], 0)


class TestGuardLogStaysFast(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "guard_events.jsonl"
        self._env = patch.dict(os.environ, {"TELEMACHUS_GUARD_LOG": str(self.path)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_record_is_fast_on_hostile_text(self):
        # Each of these took seconds to minutes before (cubic or quadratic regex work).
        for text in ("echo " + "secret_" * 1000, "a+" * 50000, "mysql " * 20000, "x" * 2_000_000):
            t = time.perf_counter()
            self.assertTrue(guard_log.record("ALLOW", text, "intent", text, action="run"))
            self.assertLess(time.perf_counter() - t, 1.0, text[:20])
        for ev in guard_log.read_recent(10, path=self.path):
            self.assertLessEqual(len(ev["target"]), guard_log.TARGET_MAX)
            self.assertLessEqual(len(ev["detail"]), guard_log.DETAIL_MAX)

    def test_guard_evaluate_returns_fast(self):
        from telemachus.fs.guard import LayaGuard
        from telemachus.fs.models import FileActionType
        t = time.perf_counter()
        LayaGuard().evaluate(FileActionType.EXECUTE, target="echo " + "secret_" * 1000)
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_long_target_keeps_both_ends_and_masks(self):
        text = "API_KEY=abc123 " + "y " * 5000 + "--token zz9"
        out = guard_log.short_target(text)
        self.assertTrue(out.startswith("API_KEY=***"), out)
        self.assertTrue(out.endswith("--token ***"), out)
        self.assertNotIn("abc123", out)
        self.assertNotIn("zz9", out)

    def test_masking_rules_unchanged(self):
        self.assertEqual(guard_log.redact("export MY_SERVICE_API_KEY=abc"), "export MY_SERVICE_API_KEY=***")
        self.assertEqual(guard_log.redact("git+ssh://me:pw@host"), "git+ssh://me:***@host")
        self.assertEqual(guard_log.redact("k a+ab12cdef34ghij56klmn78opqr90stuv12"), "k ***")
        self.assertEqual(guard_log.redact("mysql -h db -u root -pS3cret mydb"), "mysql -h db -u root -p*** mydb")

    def test_huge_old_line_is_cut_when_read(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"ts": %f, "kind": "ALLOW", "target": "%s"}\n' % (NOW, "a+" * 50000))
        t = time.perf_counter()
        evs = guard_log.read_recent(5, path=self.path)
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertEqual(len(evs), 1)


if __name__ == "__main__":
    unittest.main()
