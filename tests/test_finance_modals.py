"""Tests: Add / Edit money boxes (FinanceModals) driven by key sequences. No curses, synthetic data only."""
from __future__ import annotations

import curses
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from telemachus.cockpit import theme
from telemachus.cockpit import finance_modals
from telemachus.cockpit.finance_modals import ACCOUNTS, FinanceModals, preview_line, selection_after_delete
from telemachus.cockpit.finance_panel import EDIT_CATEGORIES, FinanceTuiPanel
from telemachus.finance.store import FinanceStore

TODAY = date(2026, 10, 28)
ENTER, ESC, TAB, CTRL_S, CTRL_D, BACKSPACE = 10, 27, 9, 19, 4, 127


def type_text(m: FinanceModals, text: str) -> None:
    for ch in text:
        m.handle_key(ord(ch))


def box_text(m: FinanceModals, cols: int = 95, rows: int = 46) -> str:
    ov = m.render(cols, rows)
    return "\n".join(theme.line_text(line) for line in ov.lines)


class StoreCase(unittest.TestCase):
    """A real ledger in a temp folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FinanceStore(root_dir=Path(self.tmp.name) / "finances")
        self.panel = FinanceTuiPanel(store=self.store)
        self.m = FinanceModals(panel_getter=lambda: self.panel, today=lambda: TODAY)

    def items(self, day: str = "2026-10-28"):
        for d in self.panel.get_daily_ledger():
            if d["date"] == day:
                return d["items"]
        return []


class TestPreview(unittest.TestCase):
    def test_kinds_accounts_and_signs(self):
        p = preview_line("groceries 25.50")
        self.assertEqual((p.ok, p.category, p.account, p.kind, p.amount_cents), (True, "Groceries", "Checking", "spend", -2550))
        p = preview_line("payday + 1480")
        self.assertEqual((p.kind, p.account, p.amount_cents), ("income", "Checking", 148000))
        p = preview_line("lunch 13.15 visa")
        self.assertEqual((p.kind, p.account, p.marked), ("spend", "Visa", True))
        p = preview_line("savings + 500")
        self.assertEqual((p.kind, p.amount_cents), ("transfer", -50000))

    def test_default_account_only_moves_unmarked_spends(self):
        self.assertEqual(preview_line("gas 40", "Visa").account, "Visa")
        self.assertTrue(preview_line("gas 40", "Visa").defaulted)
        self.assertEqual(preview_line("gas 40 debit", "Visa").account, "Checking")
        self.assertEqual(preview_line("payday + 100", "Visa").account, "Checking")

    def test_bad_line_has_a_short_reason(self):
        p = preview_line("boba seven")
        self.assertFalse(p.ok)
        self.assertIn("No amount found", p.reason)
        self.assertIn("boba", p.reason)


class TestAddBox(StoreCase):
    def test_opens_in_one_step_with_today(self):
        self.m.open_add()
        self.assertEqual((self.m.kind, self.m.add_stage, self.m.date_buffer), ("add", "bullets", "2026-10-28"))
        self.assertIn("today, Wed Oct 28", box_text(self.m))

    def test_add_flow_with_card_lines(self):
        self.m.open_add()
        for line in ("groceries 25.50", "lunch 13.15 visa", "payday + 1480"):
            type_text(self.m, line)
            self.m.handle_key(ENTER)
        self.assertEqual(len(self.m.batch_lines), 3)
        text = box_text(self.m)
        self.assertIn("Groceries · Checking · spend", text)
        self.assertIn("Dining · Visa · spend", text)
        self.assertIn("+$1,480.00", text)
        self.assertIn("3 ready", text)
        res = self.m.handle_key(ENTER)  # empty line saves all
        self.assertTrue(res.closed and res.changed)
        self.assertEqual(res.toast_kind, "ok")
        self.assertIn("1 on Visa", res.toast)
        accounts = sorted((it["payee"], it["account_short"]) for it in self.items())
        self.assertIn(("lunch", "Visa"), accounts)
        self.assertIn(("groceries", "Checking"), accounts)

    def test_invalid_line_is_red_and_not_saved(self):
        self.m.open_add()
        type_text(self.m, "boba seven")
        self.m.handle_key(ENTER)
        type_text(self.m, "gas 42")
        text = box_text(self.m)
        self.assertIn("✕ No amount found", text)
        self.assertIn("1 needs a fix", text)
        res = self.m.handle_key(CTRL_S)  # first press asks
        self.assertFalse(res.closed)
        self.assertEqual(self.m.confirm, "skip")
        self.assertIn("again saves the other 1", box_text(self.m))
        res = self.m.handle_key(CTRL_S)
        # The good line is saved; the box stays open with the line that needs a fix, ready to edit.
        self.assertFalse(res.closed)
        self.assertTrue(res.changed)
        self.assertEqual(res.toast_kind, "warn")
        self.assertIn("1 line still needs a fix", res.toast)
        self.assertEqual([it["payee"] for it in self.items()], ["gas"])
        self.assertEqual((self.m.batch_lines, self.m.line_buffer, self.m.confirm), ([], "boba seven", None))
        type_text(self.m, " 7")
        res = self.m.handle_key(ENTER)
        self.assertFalse(res.closed)  # the fixed line is now a finished line
        res = self.m.handle_key(ENTER)
        self.assertTrue(res.closed)
        self.assertEqual(sorted(it["payee"] for it in self.items()), ["boba seven", "gas"])

    def test_only_bad_lines_never_save(self):
        self.m.open_add()
        type_text(self.m, "boba seven")
        res = self.m.handle_key(CTRL_S)
        self.assertEqual(res.toast_kind, "error")
        self.assertTrue(self.m.active)

    def test_default_account_toggle(self):
        self.m.open_add()
        self.m.handle_key(TAB)
        self.assertEqual(self.m.default_account, "Visa")
        self.assertIn("[ Visa ]", box_text(self.m))
        for line in ("groceries 25.50", "coffee 5 debit", "payday + 100"):
            type_text(self.m, line)
            self.m.handle_key(ENTER)
        self.assertIn("Groceries · Visa", box_text(self.m))
        self.m.handle_key(CTRL_S)
        got = {it["payee"]: it["account_short"] for it in self.items()}
        self.assertEqual(got["groceries"], "Visa")
        self.assertEqual(got["coffee"], "Checking")
        self.assertEqual([it["kind"] for it in self.items() if it["payee"] == "payday"], ["income"])
        self.m.open_add()
        self.m.handle_key(TAB)
        self.m.handle_key(TAB)
        self.assertEqual(self.m.default_account, "Checking")

    def test_date_change_with_arrows_and_brackets(self):
        self.m.open_add()
        type_text(self.m, "gas 4")
        self.m.handle_key(curses.KEY_LEFT)
        self.m.handle_key(curses.KEY_LEFT)
        self.m.handle_key(ord("]"))
        self.assertEqual(self.m.date_buffer, "2026-10-27")
        self.assertEqual(self.m.line_buffer, "gas 4")  # the line is untouched
        self.assertIn("yesterday, Tue Oct 27", box_text(self.m))
        self.m.handle_key(ord("["))
        self.assertIn("Mon Oct 26, 2 days ago", box_text(self.m))
        self.m.handle_key(CTRL_S)
        self.assertEqual(len(self.items("2026-10-26")), 1)

    def test_cancel_asks_when_lines_are_not_saved(self):
        panel = MagicMock()
        m = FinanceModals(panel_getter=lambda: panel, today=lambda: TODAY)
        m.open_add()
        type_text(m, "gas 40")
        m.handle_key(ENTER)
        res = m.handle_key(ESC)
        self.assertTrue(m.active)
        self.assertFalse(res.closed)
        self.assertIn("esc again to drop 1 unsaved line", box_text(m))
        type_text(m, "x")  # any other key disarms
        self.assertIsNone(m.confirm)
        m.handle_key(ESC)
        res = m.handle_key(ESC)
        self.assertTrue(res.closed)
        panel.log_day_bullets.assert_not_called()

    def test_esc_on_an_empty_box_closes_at_once(self):
        self.m.open_add()
        self.assertTrue(self.m.handle_key(ESC).closed)

    def test_enter_on_empty_first_line_keeps_the_box(self):
        self.m.open_add()
        res = self.m.handle_key(ENTER)
        self.assertTrue(self.m.active)
        self.assertIn("Type an entry", res.toast)

    def test_backspace_on_empty_line_pulls_previous_line_back(self):
        self.m.open_add()
        type_text(self.m, "gas 40")
        self.m.handle_key(ENTER)
        self.m.handle_key(BACKSPACE)
        self.assertEqual((self.m.batch_lines, self.m.line_buffer), ([], "gas 40"))
        self.m.handle_key(BACKSPACE)
        self.assertEqual(self.m.line_buffer, "gas 4")

    def test_plain_checking_lines_use_log_day_bullets(self):
        panel = MagicMock()
        panel.log_day_bullets.return_value = [1, 2]
        m = FinanceModals(panel_getter=lambda: panel, today=lambda: TODAY)
        m.open_add("2026-10-23")
        type_text(m, "gas - 40")
        m.handle_key(ENTER)
        type_text(m, "payday + 1")
        m.handle_key(CTRL_S)
        panel.log_day_bullets.assert_called_once_with("2026-10-23", "gas - 40\npayday + 1")

    def test_visa_default_saves_with_marker_text(self):
        panel = MagicMock()
        panel.log_day_bullets.return_value = [1]
        m = FinanceModals(panel_getter=lambda: panel, today=lambda: TODAY)
        m.open_add("2026-10-23")
        m.handle_key(TAB)
        type_text(m, "lunch 12")
        m.handle_key(CTRL_S)
        panel.log_day_bullets.assert_called_once_with("2026-10-23", "lunch 12 on visa")


class TestPaste(StoreCase):
    def test_burst_of_keys_makes_several_lines_and_never_saves(self):
        self.m.open_add()
        keys = [ord(c) for c in "gas 40\n\nlunch 12\r\n"]
        res = self.m.handle_keys(keys)
        self.assertFalse(res.closed)
        self.assertEqual(self.m.batch_lines, ["gas 40", "lunch 12"])
        self.assertEqual(self.m.line_buffer, "")

    def test_bracketed_paste_markers_are_dropped(self):
        self.m.open_add()
        keys = [ESC] + [ord(c) for c in "[200~a 1\nb 2"] + [ESC] + [ord(c) for c in "[201~"]
        self.m.handle_keys(keys)
        self.assertTrue(self.m.active)
        self.assertEqual((self.m.batch_lines, self.m.line_buffer), (["a 1"], "b 2"))

    def test_tab_inside_a_paste_is_a_space(self):
        self.m.open_add()
        self.m.handle_keys([ord(c) for c in "coffee"] + [TAB] + [ord(c) for c in "4.50"])
        self.assertEqual((self.m.line_buffer, self.m.default_account), ("coffee 4.50", "Checking"))

    def test_short_burst_of_typed_keys_works_like_single_keys(self):
        # Keys typed while the screen was busy arrive together: they are not a paste.
        self.m.open_add()
        self.m.handle_keys([ord("g"), ord("a"), ord("s"), TAB])
        self.assertEqual((self.m.line_buffer, self.m.default_account), ("gas", "Visa"))
        self.m.handle_keys([ord("]"), ord(" "), ord("4")])
        self.assertEqual((self.m.line_buffer, self.m.date_buffer), ("gas 4", "2026-10-29"))
        res = self.m.handle_keys([ENTER, ENTER])  # a line, then enter on the empty line saves
        self.assertTrue(res.closed and res.changed)
        self.assertEqual(len(self.items("2026-10-29")), 1)

    def test_short_paste_with_a_blank_line_never_saves(self):
        self.m.open_add()
        res = self.m.handle_keys([ord(c) for c in "a 1\n\nb 2"])
        self.assertFalse(res.closed)
        self.assertEqual((self.m.batch_lines, self.m.line_buffer), (["a 1"], "b 2"))

    def test_keys_outside_bracketed_markers_are_typed(self):
        self.m.open_add()
        keys = [ESC] + [ord(c) for c in "[200~x 1\ty 2"] + [ESC] + [ord(c) for c in "[201~"] + [TAB]
        self.m.handle_keys(keys)
        self.assertEqual((self.m.line_buffer, self.m.default_account), ("x 1 y 2", "Visa"))

    def test_brackets_inside_a_paste_are_text(self):
        self.m.open_add()
        self.m.handle_keys([ord(c) for c in "[x] gas 4"])
        self.assertEqual((self.m.line_buffer, self.m.date_buffer), ("[x] gas 4", "2026-10-28"))

    def test_paste_text(self):
        self.m.open_add()
        self.m.paste("gas 40\nlunch 12\n")
        self.assertEqual(self.m.batch_lines, ["gas 40", "lunch 12"])

    def test_utf8_dash_from_notes_becomes_plain(self):
        self.m.open_add()
        for b in "gas – 40".encode("utf-8"):
            self.m.handle_key(b)
        self.assertEqual(self.m.line_buffer, "gas - 40")


class TestEditBox(StoreCase):
    def setUp(self):
        super().setUp()
        self.panel.log_day_bullets("2026-10-28", "- lunch - 13.15\npayday + 1480\n- gas - 42")
        self.day = next(d for d in self.panel.get_daily_ledger() if d["date"] == "2026-10-28")

    def idx(self, payee: str) -> int:
        return next(i for i, it in enumerate(self.day["items"]) if it["payee"] == payee)

    def test_fields_and_tab_order(self):
        self.assertIsNone(self.m.open_edit(self.day, self.idx("lunch")))
        self.assertEqual(self.m.edit_field, "text")
        text = box_text(self.m)
        for word in ("ACCOUNT", "CATEGORY", "TEXT", "[ Checking ]", "Auto, from the text: Dining", "current", "Edit entry · Wed Oct 28"):
            self.assertIn(word, text)
        self.m.handle_key(TAB)
        self.assertEqual(self.m.edit_field, "account")
        self.m.handle_key(TAB)
        self.assertEqual(self.m.edit_field, "category")
        self.m.handle_key(curses.KEY_BTAB)
        self.assertEqual(self.m.edit_field, "account")

    def test_account_change_checking_to_visa(self):
        self.m.open_edit(self.day, self.idx("lunch"))
        self.m.handle_key(TAB)  # account field
        self.m.handle_key(curses.KEY_RIGHT)
        self.assertEqual(ACCOUNTS[self.m.account_idx], "Visa")
        self.assertIn("Dining · Visa · spend", box_text(self.m))
        res = self.m.handle_key(ENTER)
        self.assertTrue(res.closed and res.changed)
        self.assertEqual(res.toast_kind, "ok")
        got = {it["payee"]: it["account_short"] for it in self.items()}
        self.assertEqual(got["lunch"], "Visa")

    def test_visa_item_stays_on_visa_after_a_text_edit(self):
        self.m.open_edit(self.day, self.idx("lunch"))
        self.m.handle_key(TAB)
        self.m.handle_key(curses.KEY_RIGHT)
        self.m.handle_key(ENTER)
        day = next(d for d in self.panel.get_daily_ledger() if d["date"] == "2026-10-28")
        lunch = next(i for i, it in enumerate(day["items"]) if it["payee"] == "lunch")
        self.m.open_edit(day, lunch)
        self.assertEqual(ACCOUNTS[self.m.account_idx], "Visa")
        self.m.handle_key(21)  # Ctrl+U clears the text; the new text names no account
        type_text(self.m, "lunch 13.10")
        self.m.handle_key(ENTER)
        got = {it["payee"]: (it["account_short"], it["amount_cents"]) for it in self.items()}
        self.assertEqual(got["lunch"], ("Visa", 1310))

    def test_typed_marker_moves_the_field_and_a_hand_pick_drops_it(self):
        self.m.open_edit(self.day, self.idx("lunch"))
        type_text(self.m, " visa")  # typing a marker moves the field to Visa
        self.assertEqual(ACCOUNTS[self.m.account_idx], "Visa")
        self.m.handle_key(TAB)
        self.m.handle_key(curses.KEY_LEFT)  # back to Checking by hand: the marker goes away
        self.assertEqual(ACCOUNTS[self.m.account_idx], "Checking")
        self.assertEqual(self.m.edit_buffer, "- lunch - 13.15")
        self.assertIn("Dining · Checking · spend", box_text(self.m))
        self.m.handle_key(ENTER)
        self.assertEqual({it["payee"]: it["account_short"] for it in self.items()}["lunch"], "Checking")

    def test_category_choice_in_text_and_list(self):
        self.m.open_edit(self.day, self.idx("lunch"))
        self.m.handle_key(curses.KEY_DOWN)  # in the text field the arrows pick the category
        self.assertEqual(EDIT_CATEGORIES[self.m.category_idx][0], "Discretionary")
        self.m.handle_key(TAB)
        self.m.handle_key(TAB)  # category field
        self.m.handle_key(curses.KEY_DOWN)
        self.assertEqual(EDIT_CATEGORIES[self.m.category_idx][0], "Groceries")
        self.assertIn("Groceries · Checking", box_text(self.m))
        self.m.handle_key(ENTER)
        self.assertEqual({it["payee"]: it["category"] for it in self.items()}["lunch"], "Groceries")

    def test_income_offers_checking_and_savings_only(self):
        self.m.open_edit(self.day, self.idx("payday"))
        self.assertIn("lands in", box_text(self.m))
        self.m.handle_key(TAB)
        self.m.handle_key(curses.KEY_RIGHT)
        self.assertEqual(ACCOUNTS[self.m.account_idx], "Savings")
        self.m.handle_key(curses.KEY_RIGHT)  # Visa is skipped
        self.assertEqual(ACCOUNTS[self.m.account_idx], "Checking")
        self.m.handle_key(curses.KEY_LEFT)
        self.assertIn("Income · Savings · income", box_text(self.m))
        self.m.handle_key(ENTER)
        payday = [it for it in self.items() if it["kind"] == "income"]
        self.assertEqual([(p["account_short"], p["amount_cents"]) for p in payday], [("Savings", 148000)])

    def test_savings_transfer_account_is_locked(self):
        self.m.open_edit(self.day, self.idx("gas"))
        self.m.handle_key(21)
        type_text(self.m, "savings + 50")
        self.m.handle_key(TAB)
        res = self.m.handle_key(curses.KEY_RIGHT)
        self.assertEqual(res.toast_kind, "warn")
        self.assertIn("always checking", box_text(self.m))

    def test_plain_text_keeps_the_income_kind(self):
        self.m.open_edit(self.day, self.idx("payday"))
        self.m.handle_key(21)
        type_text(self.m, "1500")
        self.assertIn("Income · Checking · income", box_text(self.m))
        self.m.handle_key(ENTER)
        self.assertEqual([it["amount_cents"] for it in self.items() if it["kind"] == "income"], [150000])

    def test_card_payment_can_come_from_savings(self):
        self.panel.log_day_bullets("2026-10-27", "visa payment 300")
        day = next(d for d in self.panel.get_daily_ledger() if d["date"] == "2026-10-27")
        self.m.open_edit(day, 0)
        self.assertIn("paid from", box_text(self.m))
        self.m.handle_key(TAB)
        self.m.handle_key(curses.KEY_RIGHT)
        self.assertEqual(ACCOUNTS[self.m.account_idx], "Savings")
        self.assertIn("Card payment · Savings · payment", box_text(self.m))
        self.m.handle_key(ENTER)
        got = [(it["kind"], it["account_short"]) for it in self.items("2026-10-27")]
        self.assertEqual(got, [("card_payment", "Savings")])

    def test_bad_text_keeps_the_box_open(self):
        self.m.open_edit(self.day, self.idx("lunch"))
        self.m.edit_buffer = "lunch"
        res = self.m.handle_key(ENTER)
        self.assertTrue(self.m.active)
        self.assertEqual(res.toast_kind, "error")
        self.assertIn("✕", box_text(self.m))

    def test_delete_asks_then_deletes(self):
        self.m.open_edit(self.day, self.idx("gas"))
        res = self.m.handle_key(CTRL_D)
        self.assertFalse(res.closed)
        self.assertIn("⌃D again deletes gas", box_text(self.m))
        self.m.handle_key(curses.KEY_DOWN)  # any other key disarms
        self.assertIsNone(self.m.confirm)
        self.m.handle_key(CTRL_D)
        res = self.m.handle_key(CTRL_D)
        self.assertTrue(res.closed and res.changed)
        self.assertNotIn("gas", [it["payee"] for it in self.items()])

    def test_day_menu_pick_back_and_clear(self):
        self.assertIsNone(self.m.open_edit(self.day))
        self.assertEqual(self.m.edit_stage, "day_menu")
        self.assertIn("Which entry?", box_text(self.m))
        self.m.handle_key(ord("j"))
        res = self.m.handle_key(ENTER)
        self.assertEqual((self.m.edit_stage, res.select_item), ("item", 1))
        self.m.handle_key(ESC)  # back to the menu, not closed
        self.assertEqual((self.m.edit_stage, self.m.active), ("day_menu", True))
        self.m.handle_key(ord("3"))
        self.assertEqual(self.m.menu_idx, 2)
        self.m.handle_key(CTRL_D)
        self.assertIn("⌃D again clears all 3", box_text(self.m))
        res = self.m.handle_key(CTRL_D)
        self.assertTrue(res.closed and res.changed)
        self.assertEqual(self.items(), [])


class TestReviewFixes(StoreCase):
    """Fixes from the modal review (category kept, selection follows, keys, totals)."""

    def day(self, date: str = "2026-10-28"):
        return next(d for d in self.panel.get_daily_ledger() if d["date"] == date)

    def test_amount_edit_keeps_a_hand_picked_category(self):
        self.panel.log_day_bullets("2026-10-28", "lunch 13 visa")
        self.m.open_edit(self.day(), 0)
        self.m.handle_key(TAB)
        self.m.handle_key(TAB)  # category field
        while EDIT_CATEGORIES[self.m.category_idx][0] != "Groceries":
            self.m.handle_key(curses.KEY_DOWN)
        self.m.handle_key(ENTER)
        self.assertEqual(self.items()[0]["category"], "Groceries")
        self.m.open_edit(self.day(), 0)  # opens on Groceries, not Auto (the text alone says Dining)
        self.assertEqual(EDIT_CATEGORIES[self.m.category_idx][0], "Groceries")
        self.m.edit_buffer = self.m.edit_buffer.replace("13", "15")
        self.m.handle_key(ENTER)
        it = self.items()[0]
        self.assertEqual((it["category"], it["amount_cents"], it["account_short"]), ("Groceries", 1500, "Visa"))

    def test_category_that_matches_the_text_opens_on_auto(self):
        self.panel.log_day_bullets("2026-10-28", "lunch 13\npayday + 100")
        items = self.day()["items"]
        for i, it in enumerate(items):
            self.m.open_edit(self.day(), i)
            self.assertEqual(self.m.category_idx, 0, it["payee"])

    def test_saved_edit_keeps_the_highlight_on_the_edited_entry(self):
        self.panel.log_day_bullets("2026-10-28", "- lunch - 13.15\n- coffee - 4 visa\n- lunch - 9")
        before = [it["id"] for it in self.day()["items"]]
        idx = next(i for i, it in enumerate(self.day()["items"]) if it["payee"] == "coffee")
        self.m.open_edit(self.day(), idx)
        self.m.handle_key(TAB)
        self.m.handle_key(curses.KEY_RIGHT)
        res = self.m.handle_key(ENTER)
        items = self.day()["items"]
        self.assertNotEqual([it["id"] for it in items], before)  # the edit made a new entry
        self.assertEqual(items[res.select_item]["payee"], "coffee")
        self.assertEqual(res.select_day, 0)

    def test_delete_in_the_box_keeps_the_day_or_says_it_is_gone(self):
        self.panel.log_day_bullets("2026-10-26", "gas 4")
        self.panel.log_day_bullets("2026-10-28", "lunch 9\ncoffee 3")
        self.m.open_edit(self.day(), 1)
        self.m.handle_key(CTRL_D)
        res = self.m.handle_key(CTRL_D)
        self.assertEqual((res.select_day, res.select_item, res.day_gone), (0, 0, False))
        self.m.open_edit(self.day(), 0)
        self.m.handle_key(CTRL_D)
        res = self.m.handle_key(CTRL_D)
        self.assertTrue(res.day_gone)
        self.assertEqual(res.select_day, 0)  # Mon Oct 26 is now first; the TUI goes back to the days
        self.assertIn("No entries left for Wed Oct 28", res.toast)

    def test_selection_after_delete(self):
        days = [{"date": "2026-10-28", "items": [1, 2]}, {"date": "2026-10-26", "items": [1]}]
        self.assertEqual(selection_after_delete(days, "2026-10-28", 5), (0, 1, False))
        self.assertEqual(selection_after_delete(days, "2026-10-27", 0), (1, 0, True))
        self.assertEqual(selection_after_delete(days, "2026-10-30", 3), (0, 0, True))
        self.assertEqual(selection_after_delete(days, "2026-10-01", 0), (1, 0, True))
        self.assertEqual(selection_after_delete([], "2026-10-01", 0), (0, 0, True))

    def test_plain_c_does_not_clear_the_day(self):
        self.panel.log_day_bullets("2026-10-28", "lunch 9\ncoffee 3")
        self.m.open_edit(self.day())
        self.m.handle_key(ord("c"))
        self.m.handle_key(ord("c"))
        self.assertIsNone(self.m.confirm)
        self.assertEqual(len(self.items()), 2)

    def test_mouse_events_do_not_disarm_the_esc_confirm(self):
        self.m.open_add()
        type_text(self.m, "lunch 9")
        self.m.handle_key(ESC)
        self.m.handle_key(curses.KEY_MOUSE)
        self.m.handle_key(curses.KEY_RESIZE)
        self.assertEqual(self.m.line_buffer, "lunch 9")
        self.assertTrue(self.m.handle_key(ESC).closed)

    def test_totals_keep_card_spends_and_payments_apart(self):
        self.m.open_add()
        for line in ("- groceries - 25.50 visa", "pay visa 100", "payday + 1480"):
            type_text(self.m, line)
            self.m.handle_key(ENTER)
        text = box_text(self.m)
        self.assertIn("-$25.50 spent", text)
        self.assertIn("$100.00 to Visa", text)
        self.assertIn("+$1,480.00 in", text)
        self.assertNotIn("out", text)
        narrow = box_text(self.m, 50, 30)
        self.assertIn("3 ready", narrow)

    def test_direct_visa_path_keeps_identical_lines(self):
        self.m.open_add()
        self.m.handle_key(TAB)  # Visa default
        for _ in range(2):
            type_text(self.m, "gas 4")
            self.m.handle_key(ENTER)
        with patch.object(finance_modals, "VISA_MARKER", " zq"):  # forces the direct store path
            self.m.handle_key(ENTER)
        got = [(it["payee"], it["account_short"]) for it in self.items()]
        self.assertEqual(got, [("gas", "Visa"), ("gas", "Visa")])

    def test_account_hint_matches_the_keys(self):
        self.panel.log_day_bullets("2026-10-28", "lunch 9")
        self.m.open_edit(self.day(), 0)
        text = box_text(self.m)
        self.assertNotIn("tab to switch", text)
        self.assertIn("tab here, then ←/→", text)
        self.m.handle_key(TAB)
        self.assertIn("←/→ to switch", box_text(self.m))


class TestHiddenAmounts(StoreCase):
    """Hidden-amount mode (m): the boxes never show an amount from the ledger or a computed total."""

    def setUp(self):
        super().setUp()
        self.hidden = True
        self.m = FinanceModals(panel_getter=lambda: self.panel, today=lambda: TODAY, mask_getter=lambda: self.hidden)
        self.panel.log_day_bullets("2026-10-28", "- lunch - 13.15\npayday + 1480\n- gas - 42")
        self.day = next(d for d in self.panel.get_daily_ledger() if d["date"] == "2026-10-28")

    def test_mask_numbers(self):
        self.assertEqual(finance_modals.mask_numbers("- lunch - 13.15"), "- lunch - •••")
        self.assertEqual(finance_modals.mask_numbers("rent $1,480.00 visa"), "rent ••• visa")

    def test_day_menu_hides_every_amount(self):
        self.m.open_edit(self.day)
        text = box_text(self.m)
        self.assertIn("Which entry?", text)
        for amount in ("13.15", "1,480", "42.00"):
            self.assertNotIn(amount, text)
        self.assertIn("-$•••", text)
        self.assertIn("+$•••", text)

    def test_edit_text_preview_and_toast_hide_the_amount(self):
        idx = next(i for i, it in enumerate(self.day["items"]) if it["payee"] == "lunch")
        self.m.open_edit(self.day, idx)
        text = box_text(self.m)
        self.assertNotIn("13.15", text)
        self.assertIn("› - lunch - •••", text)
        self.assertIn("-$•••", text)
        self.assertEqual(self.m.edit_buffer, "- lunch - 13.15")  # only the drawing hides it
        res = self.m.handle_key(ENTER)
        self.assertTrue(res.changed)
        self.assertNotIn("13.15", res.toast)
        self.assertIn("$•••", res.toast)
        self.assertEqual([it["amount_cents"] for it in self.items() if it["payee"] == "lunch"], [1315])

    def test_typed_digits_show_and_ledger_digits_stay_hidden(self):
        idx = next(i for i, it in enumerate(self.day["items"]) if it["payee"] == "lunch")
        self.m.open_edit(self.day, idx)
        self.m.handle_key(BACKSPACE)
        self.m.handle_key(BACKSPACE)
        type_text(self.m, "99")
        self.assertEqual(self.m.edit_buffer, "- lunch - 13.99")
        self.assertEqual(self.m.edit_text_shown(), "- lunch - •••99")
        self.m.handle_key(21)  # ctrl-U: everything after is typed here
        type_text(self.m, "lunch 7.25")
        self.assertEqual(self.m.edit_text_shown(), "lunch 7.25")

    def test_delete_toast_without_payee_hides_the_amount(self):
        self.m.open_edit(self.day, 0)
        self.m.item_label, self.m._label_from_text = "- 13.15", True
        self.m.handle_key(CTRL_D)
        self.assertNotIn("13.15", box_text(self.m))
        res = self.m.handle_key(CTRL_D)
        self.assertNotIn("13.15", res.toast)

    def test_add_box_hides_preview_amounts_and_totals(self):
        self.m.open_add()
        type_text(self.m, "coffee 3.20")
        self.m.handle_key(ENTER)
        type_text(self.m, "payday + 1480")
        text = box_text(self.m)
        self.assertNotIn("$3.20", text)
        self.assertNotIn("$1,480", text)
        self.assertIn("-$••• spent", text)
        self.assertIn("coffee 3.20", text)  # what Sam types stays visible

    def test_shown_mode_is_unchanged(self):
        self.hidden = False
        self.m.open_edit(self.day)
        self.assertIn("-$13.15", box_text(self.m))


class TestWideText(StoreCase):
    def keys(self, text: str) -> None:
        for b in text.encode("utf-8"):
            self.m.handle_key(b)

    def test_korean_text_is_kept_in_the_add_box(self):
        self.m.open_add()
        self.keys("점심 12")
        self.assertEqual(self.m.line_buffer, "점심 12")
        self.assertIn("점심 12", box_text(self.m))
        res = self.m.handle_key(CTRL_S)
        self.assertTrue(res.changed)
        self.assertEqual([it["payee"] for it in self.items()], ["점심"])

    def test_accents_full_width_and_emoji(self):
        self.m.open_add()
        self.keys("café ４")
        self.assertEqual(self.m.line_buffer, "café ４")
        self.m.handle_key(21)
        self.keys("tea \U0001F375 \u2615 3")  # emoji (wide, width differs per terminal) are dropped
        self.assertEqual(self.m.line_buffer, "tea   3")

    def test_korean_text_in_the_edit_box(self):
        self.panel.log_day_bullets("2026-10-28", "- lunch - 13.15")
        day = next(d for d in self.panel.get_daily_ledger() if d["date"] == "2026-10-28")
        self.m.open_edit(day, 0)
        self.m.handle_key(21)
        self.keys("점심 13.15")
        self.assertEqual(self.m.edit_buffer, "점심 13.15")

    def test_long_wide_line_keeps_the_end_and_fits(self):
        self.m.open_add()
        self.keys("점심" * 40 + " 12")
        for cols, rows in ((95, 46), (50, 20)):
            ov = self.m.render(cols, rows)
            self.assertTrue(all(theme.line_width(l) == ov.width for l in ov.lines))
            self.assertIn(" 12█", box_text(self.m, cols, rows))


class TestSkipWarning(StoreCase):
    def test_warning_names_enter_only_on_an_empty_line(self):
        self.m.open_add()
        type_text(self.m, "lunch")
        self.m.handle_key(ENTER)
        type_text(self.m, "snack 1.25")
        self.assertIsNone(self.m.handle_key(CTRL_S).toast)
        text = box_text(self.m)
        self.assertIn("1 line needs a fix. ⌃S again saves the other 1", text)
        self.assertNotIn("↵ or ⌃S", text)
        # ⌃S again saves the good line and keeps the bad one.
        res = self.m.handle_key(CTRL_S)
        self.assertTrue(res.changed)
        self.assertEqual([it["payee"] for it in self.items()], ["snack"])

    def test_on_an_empty_line_enter_does_save(self):
        self.m.open_add()
        type_text(self.m, "lunch")
        self.m.handle_key(ENTER)
        type_text(self.m, "snack 1.25")
        self.m.handle_key(ENTER)
        self.m.handle_key(CTRL_S)
        self.assertIn("↵ or ⌃S again saves the other 1", box_text(self.m))
        res = self.m.handle_key(ENTER)
        self.assertTrue(res.changed)
        self.assertEqual([it["payee"] for it in self.items()], ["snack"])


class TestRender(StoreCase):
    SIZES = ((95, 46), (140, 46), (70, 30), (50, 20), (40, 14))

    def check(self, label: str):
        for cols, rows in self.SIZES:
            ov = self.m.render(cols, rows)
            with self.subTest(label=label, size=(cols, rows)):
                self.assertTrue(all(theme.line_width(l) == ov.width for l in ov.lines))
                self.assertLessEqual(ov.left + ov.width, cols - 1)
                self.assertGreaterEqual(ov.top, 2)
                self.assertLessEqual(ov.top + len(ov.lines), rows - 2)  # toast and footer stay visible
                self.assertTrue(theme.line_text(ov.lines[0]).startswith("╭─ "))
                self.assertTrue(theme.line_text(ov.lines[-1]).startswith("╰"))
                inner = [s for l in ov.lines[1:-1] for _, s in l[1:-1]]
                self.assertTrue(all("@" in theme.resolve(s) for s in inner))  # the card fill everywhere

    def test_every_box_fits_every_size(self):
        self.panel.log_day_bullets("2026-10-28", "- lunch - 13.15\npayday + 1480")
        day = next(d for d in self.panel.get_daily_ledger() if d["date"] == "2026-10-28")
        self.m.open_add()
        self.check("add empty")
        for i in range(12):
            type_text(self.m, f"coffee {i + 1}")
            self.m.handle_key(ENTER)
        type_text(self.m, "boba seven")
        self.check("add many")
        self.assertIn("earlier lines", box_text(self.m, 70, 30))
        self.m.open_edit(day)
        self.check("day menu")
        self.m.handle_key(ENTER)
        self.check("edit item")
        self.m.handle_key(CTRL_D)
        self.check("edit delete armed")

    def test_footer_keys_follow_the_box(self):
        self.m.open_add()
        labels = [h.label for g in self.m.keys() for h in g.hints if h.footer]
        self.assertIn("save all", labels)
        self.assertIn("account", labels)


if __name__ == "__main__":
    unittest.main()
