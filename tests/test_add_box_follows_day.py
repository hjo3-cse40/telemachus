"""Tests: the Add box from a Day screen starts on that day, and after a save the Money screen moves to
the day the entries went to (even when the save makes a new, newer day). Synthetic data only."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from telemachus.cockpit.finance_modals import FinanceModals
from telemachus.cockpit.finance_panel import FinanceTuiPanel
from telemachus.cockpit.telemetry import CockpitTelemetry
from telemachus.cockpit.tui import CockpitTUI
from telemachus.finance.store import FinanceStore

try:
    from tests.cockpit_isolation import isolate_module
except ImportError:  # run from inside tests/
    from cockpit_isolation import isolate_module

# CockpitTUI() opens the finance ledger and memory vault: keep them in a temp dir, never Sam's.
setUpModule, tearDownModule = isolate_module()

CTRL_S = 19
TODAY = date.today()
PAST = (TODAY - timedelta(days=4)).strftime("%Y-%m-%d")
YESTERDAY = (TODAY - timedelta(days=1)).strftime("%Y-%m-%d")
TODAY_S = TODAY.strftime("%Y-%m-%d")


def type_text(m: FinanceModals, text: str) -> None:
    for ch in text:
        m.handle_key(ord(ch))


def _ledger(root: Path) -> FinanceTuiPanel:
    panel = FinanceTuiPanel(store=FinanceStore(root_dir=root))
    panel.log_day_bullets(PAST, "coffee 4.50\nbook 12")
    panel.log_day_bullets(YESTERDAY, "gas 40")
    return panel


class TestSaveReturnsTheSavedDay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.panel = _ledger(Path(self.tmp.name) / "finances")
        self.m = FinanceModals(panel_getter=lambda: self.panel, today=lambda: TODAY)

    def day_index(self, value: str) -> int:
        return [d["date"] for d in self.panel.get_daily_ledger()].index(value)

    def test_save_to_a_past_day_selects_that_day_and_the_new_entry(self):
        self.m.open_add(PAST)
        type_text(self.m, "lunch 12.40")
        res = self.m.handle_key(CTRL_S)
        self.assertTrue(res.closed)
        self.assertEqual(res.select_day, self.day_index(PAST))
        items = self.panel.get_daily_ledger()[res.select_day]["items"]
        self.assertIn("lunch", str(items[res.select_item]).lower())

    def test_save_that_makes_a_new_newest_day_selects_it(self):
        self.m.open_add()  # today, no entries yet
        type_text(self.m, "lunch 12.40")
        res = self.m.handle_key(CTRL_S)
        self.assertEqual(self.panel.get_daily_ledger()[0]["date"], TODAY_S)
        self.assertEqual(res.select_day, 0)

    def test_partial_save_also_selects_the_day(self):
        self.m.open_add(PAST)
        type_text(self.m, "lunch 12.40")
        self.m.handle_key(10)
        type_text(self.m, "boba seven")
        self.m.handle_key(CTRL_S)  # arms the skip confirm
        res = self.m.handle_key(CTRL_S)
        self.assertFalse(res.closed)
        self.assertEqual(res.select_day, self.day_index(PAST))

    def test_mock_panel_without_a_day_list_selects_nothing(self):
        panel = MagicMock()
        panel.log_day_bullets.return_value = [1]
        m = FinanceModals(panel_getter=lambda: panel, today=lambda: TODAY)
        m.open_add(PAST)
        type_text(m, "lunch 12")
        res = m.handle_key(CTRL_S)
        self.assertEqual((res.select_day, res.select_item), (None, None))


class TestTuiFollowsTheSavedDay(unittest.TestCase):
    @patch("curses.color_pair", return_value=0)
    @patch("curses.curs_set")
    def setUp(self, *_):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stdscr = MagicMock()
        self.stdscr.getmaxyx.return_value = (46, 95)
        with patch.object(CockpitTUI, "refresh_data"), patch.object(CockpitTUI, "_init_curses"):
            self.tui = CockpitTUI(self.stdscr)
        self.tui.telemetry = CockpitTelemetry(lm_studio_url="http://127.0.0.1:9/v1/models",
                                              status_path=Path(self.tmp.name) / "s.json",
                                              opencode_db=Path(self.tmp.name) / "none.db")
        self.tui.finance_panel = _ledger(Path(self.tmp.name) / "finances")
        self.tui.refresh_data()
        self.tui.current_tab = "money"

    def open_past_day(self) -> None:
        dates = [d["date"] for d in self.tui.finance_days]
        self.tui.selected_day_idx = dates.index(PAST)
        self.tui._open_selected_day()
        self.assertEqual(self.tui.finances_view_mode, "day")

    def save(self, text: str) -> None:
        m = self.tui._get_modals()
        type_text(m, text)
        self.tui._apply_modal_result(m.handle_key(CTRL_S))

    def test_a_on_a_past_day_starts_on_that_day_and_stays_there(self):
        self.open_past_day()
        self.tui.handle_finance_key(ord("a"), 46)
        self.assertEqual(self.tui._get_modals().date_buffer, PAST)
        self.save("lunch 12.40")
        day = self.tui._current_day()
        self.assertEqual((self.tui.finances_view_mode, day["date"]), ("day", PAST))
        self.assertIn("lunch", str(day["items"][self.tui.selected_item_idx]).lower())

    def test_moving_the_box_to_a_new_day_moves_the_screen_too(self):
        self.open_past_day()
        self.tui.handle_finance_key(ord("a"), 46)
        self.tui._get_modals().shift_day(4)  # to today, which has no entries yet
        self.save("lunch 12.40")
        self.assertEqual(self.tui.finance_days[0]["date"], TODAY_S)
        self.assertEqual(self.tui._current_day()["date"], TODAY_S)

    def test_a_on_the_day_list_starts_on_today(self):
        self.tui._set_finance_mode("daily")
        self.tui.selected_day_idx = [d["date"] for d in self.tui.finance_days].index(PAST)
        self.tui.handle_finance_key(ord("a"), 46)
        self.assertEqual(self.tui._get_modals().date_buffer, TODAY_S)


if __name__ == "__main__":
    unittest.main()
