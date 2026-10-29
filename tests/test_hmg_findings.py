"""Review findings on the Home, Memory and Guard tabs (narrow MONEY line, bill and mail order,
the mail-list error state, wide layout, keys, today's trash count, home path to ~)."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from telemachus.cockpit import home_view
from telemachus.cockpit.email_panel import EmailTriagePanel
from telemachus.cockpit.guard_view import GuardData, build_guard
from telemachus.cockpit.home_view import HomeData, build_home, money_summary
from telemachus.cockpit.memory_view import MemoryData, build_memory
from telemachus.cockpit.safety_memory_panel import SafetyMemoryPanel
from telemachus.cockpit.theme import line_text, line_width
from telemachus.fs import guard_log
from tests.test_hmg_views import NOW, TODAY, bills, card, facts, guard_status, home_data, mem_stats, runway, text


def keys_text(frame) -> str:
    return " ".join(f"{h.keys} {h.label}" for g in frame.keys for h in g.hints)


class TestMoneyLine(unittest.TestCase):
    def test_narrow_pane_drops_the_second_part_instead_of_cutting_a_word(self):
        for w in (30, 40, 50, 56, 60, 94):
            with self.subTest(w=w):
                lines = home_view.money_block(home_data().money, w, TODAY)
                first = line_text(lines[1]).rstrip()
                self.assertEqual(line_width(lines[1]), w)
                self.assertTrue(first.endswith(("days", "a day", "bills", "…")), first)
        self.assertEqual(line_text(home_view.money_block(home_data().money, 50, TODAY)[1]).rstrip(),
                         "  $4,200.15 left after bills  ·  $466.68 a day")
        self.assertEqual(line_text(home_view.money_block(home_data().money, 40, TODAY)[1]).rstrip(),
                         "  $4,200.15 left after bills")

    def test_next_line_is_in_date_order_with_the_card_by_its_date(self):
        t = text(build_home(home_data(), {}, 94, 40))
        nxt = next(l for l in t.splitlines() if l.strip().startswith("next"))
        # PG&E today (10/28), Gym Sun (11/1), Visa due Mon (11/2)
        self.assertLess(nxt.index("PG&E"), nxt.index("Gym"))
        self.assertLess(nxt.index("Gym"), nxt.index("Visa"))

    def test_bills_that_do_not_fit_say_how_many_more(self):
        # Changed in review round 2: the card claims the short line first (it used to show
        # "PG&E $60.00 today" and hide the bigger Visa payment in "+2 more").
        lines = home_view.money_block(home_data().money, 50, TODAY)
        nxt = line_text(lines[2]).rstrip()
        self.assertIn("Visa $312.40 Mon", nxt)
        self.assertIn("+2 more", nxt)
        self.assertEqual(line_width(lines[2]), 50)

    def test_list_mode_right_aligns_amounts(self):
        lines = home_view.money_block(home_data().money, 60, TODAY, list_bills=True)
        rows = [line_text(l).rstrip() for l in lines[2:]]
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0].startswith("  next   PG&E"))
        ends = {r.index("$") + len(r.split("$")[1].split()[0]) for r in rows}
        self.assertEqual(len(ends), 1, rows)  # amounts end in the same column


class TestInbox(unittest.TestCase):
    def test_fyi_rows_are_newest_first_whatever_the_panel_order(self):
        mails = [
            {"kind": "fyi", "sender": "Newsletter", "subject": "n", "epoch": NOW - 3600 * 30},
            {"kind": "fyi", "sender": "Library", "subject": "l", "epoch": NOW - 3600 * 26},
            {"kind": "urgent", "sender": "Dana", "subject": "u", "epoch": NOW - 3600 * 2},
            {"kind": "fyi", "sender": "Ana", "subject": "a", "epoch": NOW - 3600 * 80},
        ]
        order = [m["sender"] for m in home_view._inbox(HomeData(emails=mails))]
        self.assertEqual(order, ["Dana", "Library", "Newsletter", "Ana"])

    def test_error_state_when_the_list_query_failed(self):
        d = home_data(emails=[], email_error=True, email_stats={"total": 5, "kind_urgent": 1, "kind_action": 2})
        t = text(build_home(d, {}, 94, 40))
        self.assertIn("1 urgent · 2 action · 5 indexed", t)
        self.assertIn("Could not read the mail list.", t)
        self.assertNotIn("Nothing to triage.", t)

    def test_nothing_to_triage_only_without_an_error(self):
        d = home_data(emails=[], email_stats={"total": 2})
        self.assertIn("Nothing to triage.", text(build_home(d, {}, 94, 40)))


class TestEmailPanel(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "email.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, sql_cols: str, rows):
        conn = sqlite3.connect(self.db)
        conn.execute(f"CREATE TABLE indexed_messages ({sql_cols})")
        n = len(sql_cols.split(","))
        conn.executemany(f"INSERT INTO indexed_messages VALUES ({','.join('?' * n)})", rows)
        conn.commit()
        conn.close()
        return EmailTriagePanel(db_path=self.db)

    def test_missing_optional_columns_still_list_mail(self):
        # Same shape as the review's synthetic index: no uid column.
        panel = self.make(
            "account_id TEXT, sender TEXT, subject TEXT, date_str TEXT, category TEXT, attention TEXT, snippet TEXT, date_epoch INTEGER",
            [("a", "Dana <d@x.test>", "Lease", "", "important_documents", "urgent_review", "sign", 100),
             ("a", "CI <c@x.test>", "Build", "", "transactional", "action_needed", "", 200)],
        )
        items = panel.get_triage_items()
        self.assertIsNone(panel.last_error)
        self.assertEqual([i["sender"] for i in items], ["Dana", "CI"])
        self.assertIsNone(items[0]["uid"])
        self.assertEqual(panel.get_summary_stats()["total"], 2)

    def test_query_error_is_reported(self):
        panel = self.make("account_id TEXT, sender TEXT", [("a", "x")])
        with patch("telemachus.cockpit.email_panel.KIND_RANK_SQL", "no_such_column"):
            self.assertEqual(panel.get_triage_items(), [])
        self.assertTrue(panel.last_error)
        panel.get_triage_items()
        self.assertIsNone(panel.last_error)  # a good read clears it

    def test_groups_then_newest_first(self):
        panel = self.make(
            "account_id TEXT, uid TEXT, sender TEXT, subject TEXT, date_str TEXT, date_epoch INTEGER, category TEXT, attention TEXT, snippet TEXT",
            [("a", "1", "Library", "s", "", 300, "personal_work", "routine", ""),
             ("a", "2", "TLDR", "s", "", 400, "newsletter_marketing", "routine", ""),
             ("a", "3", "Ana", "s", "", 100, "personal_work", "routine", ""),
             ("a", "4", "Bank", "s", "", 50, "transactional", "routine", ""),
             ("a", "5", "Spam", "s", "", 900, "suspicious_spam", "none", ""),
             ("a", "6", "Dana", "s", "", 10, "important_documents", "urgent_review", "")],
        )
        items = panel.get_triage_items()
        self.assertEqual([i["sender"] for i in items], ["Dana", "Bank", "TLDR", "Library", "Ana", "Spam"])
        self.assertEqual(panel.get_triage_items(limit=3)[-1]["sender"], "TLDR")

    def test_load_passes_the_error_to_the_view(self):
        class Ep:
            last_error = None

            def get_triage_items(self, limit=25):
                self.last_error = "no such column: uid"
                return []

            def get_summary_stats(self):
                return {"total": 3}

            def index_exists(self):
                return True

        class Sp:
            def get_guard_status(self):
                return {}

            def get_memory_stats(self):
                return {}

            def get_recent_events(self, limit=8):
                return []

        panels = type("P", (), {"email_panel": Ep(), "safety_panel": Sp(), "finance_panel": None})()
        self.assertTrue(home_view.load(panels).email_error)


class TestWideHome(unittest.TestCase):
    def test_180_by_50_shows_guard_whole_and_every_bill(self):
        d = home_data(facts=facts(40), memory_stats=mem_stats(40))
        f = build_home(d, {}, 180, 50)
        t = text(f)
        self.assertIn("last checkpoint 2m ago", t)
        for name in ("PG&E", "Gym", "Visa"):
            self.assertIn(name, t)
        filled = [i for i, l in enumerate(f.lines) if line_text(l).strip()]
        self.assertGreaterEqual(max(filled), 45)  # memory fills the right column
        for line in f.lines:
            self.assertEqual(line_width(line), 180)

    def test_120_wide_fits(self):
        f = build_home(home_data(), {}, 120, 30)
        for line in f.lines:
            self.assertEqual(line_width(line), 120)
        self.assertIn("GUARD", text(f))


class TestKeys(unittest.TestCase):
    def test_m_on_home_is_the_mask_command(self):
        self.assertEqual(home_view.handle_key(ord("m"), {}, home_data()), "mask")

    def test_footer_offers_m_with_money_and_follows_the_mask(self):
        self.assertIn("m hide $", keys_text(build_home(home_data(), {}, 94, 40)))
        self.assertIn("m show $", keys_text(build_home(home_data(), {"mask": True}, 94, 40)))
        self.assertNotIn("m ", keys_text(build_home(home_data(money=None), {}, 94, 40)))

    def test_no_list_keys_for_an_empty_inbox(self):
        d = home_data(emails=[], email_stats={}, email_index_found=False)
        k = keys_text(build_home(d, {}, 94, 40))
        self.assertNotIn("j/k", k)
        self.assertNotIn("↵", k)
        self.assertNotIn("t triage", k)
        self.assertIn("j/k move", keys_text(build_home(home_data(), {}, 94, 40)))

    def test_memory_review_count_and_command_are_separated(self):
        f = build_memory(MemoryData(memory_stats=mem_stats(), facts=facts(), pending=2, now=NOW), {}, 140, 30)
        first = line_text(f.lines[0])
        self.assertIn("2 to review  ·  run python3 -m telemachus.memory review", first)


class TestTrashToday(unittest.TestCase):
    def test_guard_tab_uses_todays_trash_count(self):
        g = guard_status()
        g["trash_substitutions"] = 999  # all-time audit lines: not shown
        g["log"]["trash_today"] = 2
        t = text(build_guard(GuardData(g, now=NOW), {}, 94, 40))
        self.assertIn("trashed 2 today", t)
        self.assertNotIn("999", t)
        self.assertIn("2 trashed today", text(build_home(home_data(guard_status=g), {}, 94, 40)))

    def test_panel_reads_the_audit_only_when_it_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audit = root / "trash_audit.jsonl"
            audit.write_text(json.dumps({"original_path": "/x/a.txt"}) + "\n")
            panel = SafetyMemoryPanel(vault_root=root / "vault", audit_log=audit, guard_events=root / "g.jsonl")
            first = panel.get_guard_status()
            self.assertEqual((first["trash_substitutions"], first["last_trash_item"]), (1, "a.txt"))
            with patch("builtins.open", side_effect=AssertionError("re-read")):
                panel._trash_audit()  # same size and time: cached
            with open(audit, "a") as f:
                f.write(json.dumps({"original_path": "/x/b.txt"}) + "\n")
            self.assertEqual(panel._trash_audit(), (2, "b.txt"))


class TestHomeTilde(unittest.TestCase):
    def test_only_whole_home_paths_become_tilde(self):
        with patch.object(Path, "home", return_value=Path("/Users/sam")):
            self.assertEqual(guard_log.short_target("/Users/sam2/x"), "/Users/sam2/x")
            self.assertEqual(guard_log.short_target("/Users/sam.bak/x"), "/Users/sam.bak/x")
            self.assertEqual(guard_log.short_target("/backup/Users/sam/x"), "/backup/Users/sam/x")
            self.assertEqual(guard_log.short_target("/Users/sam/x"), "~/x")
            self.assertEqual(guard_log.short_target("/Users/sam"), "~")
            self.assertEqual(guard_log.short_target("cd /Users/sam && ls"), "cd ~ && ls")
            self.assertEqual(guard_log._clean("path '/Users/sam/.ssh/id' is forbidden", 200),
                             "path '~/.ssh/id' is forbidden")


if __name__ == "__main__":
    unittest.main()
