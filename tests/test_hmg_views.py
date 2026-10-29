"""Home, Memory and Guard views: size bounds, empty states, keys, clicks and the MONEY line."""
from __future__ import annotations

from datetime import date, datetime
import unittest
from types import SimpleNamespace

from telemachus.cockpit import guard_view, home_view, memory_view
from telemachus.cockpit import view_common as vc
from telemachus.cockpit.guard_view import GuardData, build_guard
from telemachus.cockpit.home_view import HomeData, build_home, money_summary
from telemachus.cockpit.memory_view import MemoryData, build_memory
from telemachus.cockpit.theme import line_text, line_width

NOW = datetime(2026, 10, 28, 14, 32).timestamp()
TODAY = date(2026, 10, 28)
SIZES = [(94, 40), (79, 24), (139, 44), (60, 20), (40, 12), (30, 6), (94, 3)]


def text(frame) -> str:
    return "\n".join(line_text(l) for l in frame.lines)


def emails():
    kinds = ["urgent", "action", "action", "spam", "fyi", "fyi"]
    return [
        {"badge": "INFO", "kind": k, "sender": f"Sender {i}", "subject": f"Subject number {i}",
         "date": "2026-10-28", "epoch": NOW - (i + 1) * 3600, "snippet": f"Preview text for mail {i}. " * 8}
        for i, k in enumerate(kinds)
    ]


def facts(n=15):
    tiers = ["private", "project", "public"]
    return [
        {"id": f"id{i:04d}abcdef", "text": f"fact number {i} about tmux panes and colors", "category": "project",
         "tier": tiers[i % 3], "source": "opencode chat", "ts": NOW - i * 1800, "uses": i, "pinned": i == 2,
         "about": "", "quote": "", "kind": ""}
        for i in range(n)
    ]


def mem_stats(n=15):
    return {"available": True, "total_memories": n,
            "by_tier": {"tier_2_private": 5, "tier_1_project": 5, "tier_0_public": 5}, "by_category": {}}


def guard_status(intent=True, latency=None, events=True):
    evs = [
        {"ts": NOW - 60, "kind": "BLOCK", "target": "~/.ssh/id_ed25519", "rule": "path shield",
         "detail": "Tier 0 Violation: Access to sensitive security credential path is forbidden.", "action": "read"},
        {"ts": NOW - 120, "kind": "CHECKPOINT", "target": "telemachus @ 3f9c2a1", "rule": "git checkpoint",
         "detail": "Before editing tui.py", "action": "checkpoint"},
        {"ts": NOW - 200, "kind": "ALLOW", "target": "~/x.py", "rule": "path shield", "detail": "ok", "action": "edit"},
    ] if events else []
    return {
        "protected_paths": ["~/.ssh", "~/.gnupg", ".env", "/System"],
        "trash_substitutions": 25,
        "router_latency_ms": latency,
        "intent_available": intent,
        "log": {"total": len(evs), "today": len(evs), "blocked_today": 1 if evs else 0,
                "path_blocks_today": 1 if evs else 0, "intent_blocks_today": 0,
                "last_event_ts": NOW - 60 if evs else None,
                "last_checkpoint_ts": NOW - 120 if evs else None, "last_intent_latency_ms": latency},
        "log_found": bool(evs),
        "events": evs,
    }


def runway(left=420015, per_day=46668, days=9, payday="2026-11-06"):
    return SimpleNamespace(left_cents=left, per_day_cents=per_day, days_left=days, next_paycheck=payday)


def bills():
    return {"bills": [
        {"key": "pge", "name": "PG&E Electric & Gas", "category": "Utility", "due_day": 28, "amount_cents": 6000,
         "status": "due", "is_active": True},
        {"key": "gym", "name": "Gym", "category": "Fixed Bill", "due_day": 1, "amount_cents": 2500,
         "status": "due", "is_active": True},
        {"key": "brother", "name": "Brother Payment", "category": "Fixed Bill", "due_day": 1, "amount_cents": 0,
         "status": "due", "is_active": False},
        {"key": "spotify", "name": "Spotify", "category": "Subscription", "due_day": 30, "amount_cents": 1199,
         "status": "paid", "is_active": True},
        {"key": "credit_card", "name": "Credit Card (Visa)", "category": "Card Balance", "due_day": 2,
         "amount_cents": 31240, "status": "due", "is_active": True},
    ]}


def card(owed=31240):
    return {"owed_cents": owed, "status": "owed" if owed > 0 else "clear", "next_due_date": "2026-11-02",
            "due_before_payday": True}


def home_data(**kw):
    base = dict(emails=emails(), email_stats={"total": 6, "kind_urgent": 1, "kind_action": 2},
                guard_status=guard_status(), memory_stats=mem_stats(), facts=facts(),
                money=money_summary(runway(), bills(), card(), TODAY), now=NOW)
    base.update(kw)
    return HomeData(**base)


class TestSizes(unittest.TestCase):
    def check(self, frame, w, h):
        self.assertLessEqual(len(frame.lines), h)
        for line in frame.lines:
            self.assertEqual(line_width(line), w, line_text(line))
        for idx in frame.rows:
            self.assertLess(idx, h)

    def test_every_view_fits_every_size(self):
        cases = [
            (home_view, home_data()), (home_view, HomeData()),
            (memory_view, MemoryData(memory_stats=mem_stats(), facts=facts(), pending=2, now=NOW)),
            (memory_view, MemoryData()),
            (guard_view, GuardData(guard_status(latency=41.0), now=NOW)), (guard_view, GuardData()),
        ]
        for mod, data in cases:
            for state in ({}, {"open": True, "sel": 3}):
                for w, h in SIZES:
                    with self.subTest(mod=mod.__name__, w=w, h=h, state=state):
                        self.check(mod.build(data, dict(state), w, h), w, h)


class TestHome(unittest.TestCase):
    def test_blocks_in_order_and_spam_hidden(self):
        t = text(build_home(home_data(), {}, 94, 40))
        self.assertLess(t.index("MONEY"), t.index("INBOX"))
        self.assertLess(t.index("INBOX"), t.index("MEMORY"))
        self.assertLess(t.index("MEMORY"), t.index("GUARD"))
        self.assertIn("URGENT", t)
        self.assertIn("ACTION", t)
        self.assertIn("FYI", t)
        self.assertNotIn("Subject number 3", t)  # spam
        self.assertIn("1 urgent · 2 action", t)

    def test_selection_marker_rows_and_preview(self):
        f = build_home(home_data(), {"sel": 1}, 94, 40)
        rows = {v: k for k, v in f.rows.items()}
        self.assertEqual(sorted(i for _, i in f.rows.values()), list(range(5)))
        sel_line = line_text(f.lines[rows[("email", 1)]])
        self.assertTrue(sel_line.startswith("▸ ACTION"))
        self.assertTrue(line_text(f.lines[rows[("email", 0)]]).startswith("  URGENT"))
        t = text(f)
        self.assertIn("Preview text for mail 1.", t)
        self.assertIn("2h", sel_line)

    def test_open_shows_more(self):
        closed = text(build_home(home_data(), {"sel": 0}, 94, 40))
        opened = text(build_home(home_data(), {"sel": 0, "open": True}, 94, 40))
        self.assertIn("From Sender 0", opened)
        self.assertNotIn("From Sender 0", closed)
        self.assertGreater(opened.count("Preview text"), closed.count("Preview text"))

    def test_money_line(self):
        t = text(build_home(home_data(), {}, 94, 40))
        self.assertIn("until payday Fri Nov 6", t)
        self.assertIn("$4,200.15 left after bills", t)
        self.assertIn("$466.68 a day for 9 days", t)
        self.assertIn("PG&E $60.00 today", t)
        self.assertIn("Visa $312.40 Mon", t)  # same day word as the bills (was "due Mon")
        self.assertNotIn("Brother", t)  # inactive, $0
        self.assertNotIn("Spotify", t)  # paid this month

    def test_money_masked(self):
        m = money_summary(runway(), bills(), card(), TODAY, mask=True)
        t = text(build_home(home_data(money=m), {}, 94, 40))
        line = next(l for l in t.splitlines() if "left after bills" in l)
        self.assertNotRegex(line, r"\d,\d|\.\d\d")
        self.assertIn("$•••", line)

    def test_money_masked_by_tui_state(self):
        t = text(build_home(home_data(), {"mask": True}, 94, 40))
        self.assertNotIn("4,200.15", t)
        self.assertNotIn("$60.00", t)

    def test_money_short(self):
        m = money_summary(runway(left=-1250), bills(), None, TODAY)
        t = text(build_home(home_data(money=m), {}, 94, 40))
        self.assertIn("$12.50 short after bills", t)

    def test_money_summary_pure(self):
        m = money_summary(runway(), bills(), card(0), TODAY)
        self.assertEqual([b["name"] for b in m["bills"]], ["PG&E", "Gym"])
        self.assertEqual(m["bills"][1]["date"], "2026-11-01")
        self.assertIsNone(m["card"])
        self.assertFalse(money_summary(None, bills(), None, TODAY)["available"])

    def test_empty_states_show_real_commands(self):
        t = text(build_home(HomeData(email_index_found=False, money={"available": False}, now=NOW), {}, 94, 40))
        self.assertIn("No mail indexed yet.", t)
        self.assertIn("python3 -m telemachus.email index", t)
        self.assertIn("No memories yet.", t)
        self.assertIn("python3 -m telemachus.finance setup", t)
        self.assertIn("no checkpoint yet", t)

    def test_money_hidden_when_not_loaded(self):
        self.assertNotIn("MONEY", text(build_home(HomeData(), {}, 94, 40)))

    def test_two_columns_when_wide(self):
        f = build_home(home_data(), {}, 140, 40)
        first = line_text(f.lines[0])
        self.assertIn("INBOX", first)
        self.assertIn("MONEY", first)
        self.assertIn(("email", 0), f.rows.values())

    def test_small_height_keeps_the_inbox(self):
        f = build_home(home_data(), {}, 79, 12)
        self.assertIn("INBOX", text(f))
        self.assertTrue(f.rows)

    def test_keys(self):
        d = home_data()
        st = {}
        self.assertIsNone(home_view.handle_key(ord("j"), st, d))
        self.assertEqual(st["sel"], 1)
        for _ in range(10):
            home_view.handle_key(ord("j"), st, d)
        self.assertEqual(st["sel"], 4)  # 5 non-spam mails
        home_view.handle_key(10, st, d)
        self.assertTrue(st["open"])
        home_view.handle_key(ord("k"), st, d)
        self.assertFalse(st["open"])
        self.assertEqual(home_view.handle_key(ord("t"), st, d), "triage")

    def test_click_selects_then_opens(self):
        d = home_data()
        st = {"sel": 0}
        home_view.handle_click(("email", 2), st, d)
        self.assertEqual((st["sel"], st["open"]), (2, False))
        home_view.handle_click(("email", 2), st, d)
        self.assertTrue(st["open"])

    def test_guard_line_counts_only_measured(self):
        t = text(build_home(home_data(guard_status=guard_status(intent=False)), {}, 94, 40))
        self.assertIn("3 of 4 checks on", t)
        self.assertNotIn("ms", t.split("GUARD")[1])
        t = text(build_home(home_data(), {}, 94, 40))
        # trash count is today's, from the guard log (was the all-time audit line count)
        self.assertIn("all checks on   1 blocked today   0 trashed today   last checkpoint 2m ago", t)


class TestMemory(unittest.TestCase):
    def data(self, **kw):
        base = dict(memory_stats=mem_stats(), facts=facts(), pending=3, now=NOW)
        base.update(kw)
        return MemoryData(**base)

    def test_layout(self):
        f = build_memory(self.data(), {}, 94, 40)
        t = text(f)
        self.assertIn("PRV 5   PRJ 5   PUB 5   15 facts   3 to review", t)
        self.assertIn("/ search facts", t)
        self.assertIn("tier: all", t)
        self.assertIn("RECENT", t)
        self.assertIn("15 of 15", t)
        self.assertIn("used 0 times", t)  # detail of fact 0
        self.assertEqual(f.rows[2], ("search", 0))
        fact_rows = [v for v in f.rows.values() if v[0] == "fact"]
        self.assertEqual(len(fact_rows), 15)
        self.assertTrue(line_text(f.lines[next(k for k, v in f.rows.items() if v == ("fact", 0))]).startswith("▸ "))

    def test_bar_shares(self):
        bar = vc.bar([(49, "magenta"), (4, "blue"), (6, "green")], 60)
        self.assertEqual(sum(len(t) for t, _ in bar), 60)
        self.assertEqual([s for _, s in bar], ["magenta", "blue", "green"])
        self.assertEqual(vc.bar([(1, "a"), (0, "b"), (999, "c")], 10)[0], ("━", "a"))

    def test_live_search(self):
        d = self.data(facts=facts() + [{"id": "zz", "text": "prefers dark Tokyo Night colors", "category": "preference",
                                        "tier": "private", "ts": NOW, "uses": 6}])
        st = {}
        memory_view.handle_key(ord("/"), st, d)
        self.assertTrue(memory_view.captures_keys(st))
        for ch in "pre da":
            memory_view.handle_key(ord(ch), st, d)
        self.assertEqual(st["query"], "pre da")
        self.assertEqual([f["id"] for f in memory_view.visible_facts(d, st)], ["zz"])
        t = text(build_memory(d, st, 94, 40))
        self.assertIn("RESULTS", t)
        self.assertIn("1 found", t)
        memory_view.handle_key(127, st, d)  # backspace
        self.assertEqual(st["query"], "pre d")
        memory_view.handle_key(10, st, d)  # keep
        self.assertFalse(memory_view.captures_keys(st))
        self.assertEqual(st["query"], "pre d")
        memory_view.handle_key(27, st, d)  # clear
        self.assertEqual(st["query"], "")

    def test_search_typing_q_is_text(self):
        st = {"searching": True}
        memory_view.handle_key(ord("q"), st, self.data())
        self.assertEqual(st["query"], "q")

    def test_fulltext_for_unloaded_facts(self):
        calls = []

        def searcher(q, limit):
            calls.append(q)
            return [{"id": "old", "text": "an old fact about tmux", "tier": "public", "ts": 1}]

        d = self.data(memory_stats=dict(mem_stats(), total_memories=5000), searcher=searcher)
        st = {"searching": True}
        for ch in "tmux":
            memory_view.handle_key(ord(ch), st, d)
        self.assertEqual(calls[-1], "tmux")
        self.assertIn("old", [f["id"] for f in memory_view.visible_facts(d, st)])

    def test_tier_filter_cycles(self):
        d = self.data()
        st = {}
        memory_view.handle_key(ord("f"), st, d)
        self.assertEqual(st["tier"], "private")
        self.assertTrue(all(f["tier"] == "private" for f in memory_view.visible_facts(d, st)))
        self.assertIn("tier: private", text(build_memory(d, st, 94, 40)))
        for _ in range(3):
            memory_view.handle_key(ord("f"), st, d)
        self.assertEqual(st["tier"], "all")

    def test_empty_and_no_match(self):
        self.assertIn("No memories yet.", text(build_memory(MemoryData(), {}, 94, 40)))
        t = text(build_memory(self.data(), {"query": "zzzz"}, 94, 40))
        self.assertIn('No fact matches "zzzz".', t)
        t = text(build_memory(MemoryData(memory_stats={"available": False}), {}, 94, 40))
        self.assertIn("could not be read", t)

    def test_open_and_click(self):
        d = self.data()
        st = {}
        memory_view.handle_click(("fact", 4), st, d)
        self.assertEqual(st["sel"], 4)
        memory_view.handle_click(("fact", 4), st, d)
        self.assertTrue(st["open"])
        self.assertIn("id id0004ab", text(build_memory(d, st, 94, 40)))


class TestGuard(unittest.TestCase):
    def test_layout_and_measured_latency_only(self):
        t = text(build_guard(GuardData(guard_status(), now=NOW), {}, 94, 40))
        self.assertIn("all 4 checks on", t)
        self.assertIn("CHECKS", t)
        self.assertIn("RECENT EVENTS", t)
        self.assertIn("no check yet", t)
        self.assertNotIn(" ms", t)
        self.assertNotIn("58.2", t)
        t = text(build_guard(GuardData(guard_status(latency=41.2), now=NOW), {}, 94, 40))
        self.assertIn("last check 41 ms", t)

    def test_events_and_detail(self):
        f = build_guard(GuardData(guard_status(), now=NOW), {}, 94, 40)
        t = text(f)
        self.assertIn("BLOCK", t)
        self.assertIn("read ~/.ssh/id_ed25519", t)
        self.assertIn("CHECKPOINT", t)
        self.assertIn("Blocked: Access to sensitive security credential path", t)
        self.assertNotIn("Tier 0 Violation", t)
        self.assertEqual(sorted(v for v in f.rows.values()), [("event", 0), ("event", 1), ("event", 2)])

    def test_intent_off_is_shown(self):
        t = text(build_guard(GuardData(guard_status(intent=False), now=NOW), {}, 94, 40))
        self.assertIn("3 of 4 checks on", t)
        self.assertIn("Laya worker not found", t)

    def test_empty(self):
        t = text(build_guard(GuardData(guard_status(events=False), now=NOW), {}, 94, 40))
        self.assertIn("No guard events yet.", t)
        self.assertIn("no events logged yet", t)

    def test_keys(self):
        d = GuardData(guard_status(), now=NOW)
        st = {}
        guard_view.handle_key(ord("j"), st, d)
        self.assertEqual(st["sel"], 1)
        guard_view.handle_key(10, st, d)
        self.assertTrue(st["open"])
        self.assertIn("rule git checkpoint", text(build_guard(d, st, 94, 40)))
        self.assertEqual(guard_view.handle_key(ord("o"), st, d), "open_trash")


class TestCommon(unittest.TestCase):
    def test_wrap(self):
        self.assertEqual(vc.wrap("aa bb cc dd", 5, 2), ["aa bb", "cc dd"])
        self.assertEqual(vc.wrap("aa bb cc dd ee", 5, 2), ["aa bb", "cc d…"])
        self.assertEqual(vc.wrap("", 5, 2), [])

    def test_window(self):
        self.assertEqual(vc.window(10, 9, 0, 3), (9, 7))
        self.assertEqual(vc.window(10, 0, 5, 3), (0, 0))
        self.assertEqual(vc.window(0, 4, 4, 3), (0, 0))

    def test_ago(self):
        self.assertEqual(vc.ago(NOW - 30, NOW), "now")
        self.assertEqual(vc.ago(NOW - 720, NOW), "12m")
        self.assertEqual(vc.ago(NOW - 7200, NOW), "2h")
        self.assertEqual(vc.ago(None, NOW), "")


if __name__ == "__main__":
    unittest.main()
