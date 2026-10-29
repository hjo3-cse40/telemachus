"""Tests: keyword categorisation, edit-modal category picker, and delete while editing (synthetic data only)."""
from __future__ import annotations

import curses
import unittest

from telemachus.cockpit.finance_panel import EDIT_CATEGORIES
from telemachus.finance.import_text import parse_bullet_line
from tests.test_cockpit_finances import TestCockpitTUIFinances, modal_key


class TestKeywordMatching(unittest.TestCase):
    def test_att_no_longer_matches_inside_words(self):
        self.assertEqual(parse_bullet_line("- amazon air mattress - 32.88")["category"], "personal")

    def test_att_still_matches_as_whole_word(self):
        self.assertEqual(parse_bullet_line("- att - 40")["category"], "wifi")

    def test_gas_does_not_match_inside_vegas(self):
        self.assertEqual(parse_bullet_line("- vegas trip - 90")["category"], "personal")

    def test_plural_and_prefix_keywords_still_match(self):
        self.assertEqual(parse_bullet_line("- whole foods - 35")["category"], "dining")
        self.assertEqual(parse_bullet_line("- costco gas - 50")["category"], "gas")
        self.assertEqual(parse_bullet_line("- amazon groceries - 9")["category"], "groceries")


class TestEditModalCategoryAndDelete(unittest.TestCase):
    def setUp(self):
        TestCockpitTUIFinances.setUp(self)
        self.addCleanup(self.temp_dir.cleanup)
        # Force the bad old behaviour: a mattress filed under WiFi.
        self.panel.store.log_batch_transactions(
            [{"payee": "amazon air mattress", "amount": 32.88, "category": "wifi", "date": "2026-10-14"}]
        )
        self.panel.store.log_batch_transactions(
            [{"payee": "Expense", "amount": 0.0, "category": "personal", "date": "2026-10-14"}]
        )
        self.tui.refresh_data()
        self.tui.finances_focus = "items"

    def _open(self, payee: str) -> None:
        items = self.tui.finance_days[0]["items"]
        self.tui.selected_item_idx = next(i for i, it in enumerate(items) if it["payee"] == payee)
        self.tui.open_edit_modal()

    def _categories_for(self, payee: str):
        return [it["category"] for it in self.panel.get_daily_ledger()[0]["items"] if it["payee"] == payee]

    def test_modal_shows_current_category_and_opens_on_it(self):
        # The text alone gives Discretionary, so WiFi counts as picked by hand: the box opens on it,
        # and Enter keeps it (it used to open on Auto and silently re-derive the category).
        self._open("amazon air mattress")
        self.assertEqual(self.tui.editing_current_category, "WiFi")
        self.assertEqual(EDIT_CATEGORIES[self.tui.editing_category_idx][0], "WiFi")

    def test_arrow_keys_cycle_categories_both_ways(self):
        self._open("amazon air mattress")
        wifi = self.tui.editing_category_idx
        modal_key(self.tui, curses.KEY_RIGHT)
        self.assertEqual(self.tui.editing_category_idx, wifi + 1)
        modal_key(self.tui, curses.KEY_LEFT)
        self.assertEqual(self.tui.editing_category_idx, wifi)
        for _ in range(wifi):
            modal_key(self.tui, curses.KEY_LEFT)
        self.assertEqual(self.tui.editing_category_idx, 0)
        modal_key(self.tui, curses.KEY_LEFT)
        self.assertEqual(self.tui.editing_category_idx, len(EDIT_CATEGORIES) - 1)

    def test_switch_wifi_to_discretionary(self):
        self._open("amazon air mattress")
        for _ in range(self.tui.editing_category_idx):  # back up to Auto: the text gives Discretionary
            modal_key(self.tui, curses.KEY_LEFT)
        modal_key(self.tui, 10)  # Enter
        self.assertEqual(self._categories_for("amazon air mattress"), ["Discretionary"])

    def test_explicit_category_beats_text_keywords(self):
        self._open("amazon air mattress")
        self.tui.editing_buffer = "- savings gift - 32.88"
        self.tui.editing_category_idx = 1  # Discretionary
        modal_key(self.tui, 10)
        self.assertEqual(self._categories_for("savings gift"), ["Discretionary"])

    def test_plain_d_types_when_buffer_has_text(self):
        self._open("Expense")
        before = self.tui.editing_buffer
        modal_key(self.tui, ord("d"))
        self.assertEqual(self.tui.editing_buffer, before + "d")
        self.assertEqual(len(self.panel.get_daily_ledger()[0]["items"]), 2)

    def test_ctrl_d_deletes_even_with_prefilled_buffer(self):
        self._open("Expense")
        self.assertTrue(self.tui.editing_buffer)
        modal_key(self.tui, 4)  # Ctrl+D asks first
        self.assertTrue(self.tui.is_editing_finances)
        modal_key(self.tui, 4)  # Ctrl+D again deletes
        self.assertFalse(self.tui.is_editing_finances)
        payees = [it["payee"] for it in self.panel.get_daily_ledger()[0]["items"]]
        self.assertEqual(payees, ["amazon air mattress"])

    def test_delete_key_also_deletes(self):
        self._open("Expense")
        modal_key(self.tui, curses.KEY_DC)  # asks first
        modal_key(self.tui, curses.KEY_DC)
        payees = [it["payee"] for it in self.panel.get_daily_ledger()[0]["items"]]
        self.assertNotIn("Expense", payees)


if __name__ == "__main__":
    unittest.main()
