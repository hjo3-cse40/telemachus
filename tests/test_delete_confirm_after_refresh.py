"""The second d deletes the entry the first d named, even when a refresh in between brings in a
newer day (another process logged one). Synthetic temp ledger only."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from telemachus.cockpit.finance_panel import FinanceTuiPanel
from telemachus.cockpit.tui import CockpitTUI

try:
    from tests.cockpit_isolation import isolate_module
except ImportError:  # run from inside tests/
    from cockpit_isolation import isolate_module

setUpModule, tearDownModule = isolate_module()


class TestDeleteConfirmAfterRefresh(unittest.TestCase):
    @patch("curses.color_pair", return_value=0)
    @patch("curses.curs_set")
    def setUp(self, *_):
        self.stdscr = MagicMock()
        self.stdscr.getmaxyx.return_value = (46, 95)
        with patch.object(CockpitTUI, "_init_curses"):
            self.tui = CockpitTUI(self.stdscr)
        self.panel = self.tui.finance_panel
        for date in ("2026-10-26", "2026-10-27", "2026-10-28"):  # the ledger is shared by this module
            self.panel.delete_day(date)
        self.panel.log_day_bullets("2026-10-28", "coffee 4.50")
        self.panel.log_day_bullets("2026-10-26", "groceries 58.94")
        self.tui.refresh_data()
        self.tui.current_tab = "money"

    def payees(self):
        return {it["payee"].lower() for day in self.tui.finance_days for it in day["items"]}

    def open_day(self, date):
        self.tui.selected_day_idx = [d["date"] for d in self.tui.finance_days].index(date)
        self.tui.handle_key(10)
        self.assertEqual(self.tui.finances_view_mode, "day")

    def test_refresh_keeps_the_open_day_and_the_armed_entry(self):
        self.open_day("2026-10-26")
        self.tui.handle_key(ord("d"))
        self.assertIn("groceries", self.tui.notification.lower())
        # Another process (OpenCode, the CLI) logs a newer day; then the 10 s refresh runs.
        FinanceTuiPanel(store=self.panel.store).log_day_bullets("2026-10-27", "parking 9.00")
        self.tui.refresh_data()
        self.assertEqual(self.tui.finance_days[self.tui.selected_day_idx]["date"], "2026-10-26")
        self.tui.handle_key(ord("d"))
        self.assertNotIn("groceries", self.payees())
        self.assertIn("parking", self.payees())

    def test_a_vanished_entry_asks_again(self):
        self.open_day("2026-10-26")
        self.tui.handle_key(ord("d"))
        gid = self.tui.finance_days[self.tui.selected_day_idx]["items"][0]["id"]
        other = FinanceTuiPanel(store=self.panel.store)
        other.delete_transaction(gid)
        other.log_day_bullets("2026-10-26", "lunch 12.00")
        self.tui.refresh_data()
        self.tui.handle_key(ord("d"))
        self.assertIn("lunch", self.payees())  # not deleted: the d only armed it
        self.assertIn("press d again", self.tui.notification.lower())


if __name__ == "__main__":
    unittest.main()
