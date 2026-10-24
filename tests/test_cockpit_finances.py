"""Unit tests for Cockpit Finance Panel, Text Importer, and Daily Inspector."""
from __future__ import annotations

import curses
from datetime import datetime
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from telemachus.cockpit.finance_panel import FinanceTuiPanel
from telemachus.cockpit.tui import CockpitTUI
from telemachus.finance.import_text import (
    import_text_document,
    parse_bullet_line,
    parse_date_header,
)
from telemachus.finance.models import RecurringRule, RuleType
from telemachus.finance.recurring import RecurringEngine
from telemachus.finance.store import FinanceStore


class TestImportText(unittest.TestCase):
    """Verifies Notion bullet list and date header parsing."""

    def test_parse_bullet_expense(self):
        # Format: - category - amount
        item = parse_bullet_line("- groceries - 25.50")
        self.assertIsNotNone(item)
        self.assertEqual(item["amount_cents"], 2550)
        self.assertEqual(item["category"], "groceries")
        self.assertIn("groceries", item["memo"].lower())

        # Format: item - amount
        item2 = parse_bullet_line("gas - 45")
        self.assertIsNotNone(item2)
        self.assertEqual(item2["amount_cents"], 4500)
        self.assertEqual(item2["category"], "gas")

        # Format: - payee - amount
        item3 = parse_bullet_line("- chipotle - 14.85")
        self.assertIsNotNone(item3)
        self.assertEqual(item3["amount_cents"], 1485)
        self.assertEqual(item3["category"], "dining")

    def test_parse_bullet_income(self):
        # Format: payday + 1350
        item = parse_bullet_line("payday + 1350")
        self.assertIsNotNone(item)
        self.assertEqual(item["amount_cents"], 135000)
        self.assertEqual(item["account_id"], "Assets:Checking:WellsFargo")
        self.assertEqual(item["category"], "payday")

        # Format: + 50 refund
        item2 = parse_bullet_line("+ 50 refund from friend")
        self.assertIsNotNone(item2)
        self.assertEqual(item2["amount_cents"], 5000)
        self.assertEqual(item2["category"], "payday")

    def test_parse_bullet_savings(self):
        # Format: savings + 500
        item = parse_bullet_line("savings + 500")
        self.assertIsNotNone(item)
        self.assertEqual(item["amount_cents"], 50000)
        self.assertEqual(item["category"], "savings")
        self.assertEqual(item["account_id"], "Assets:Savings:CapitalOne")

        # Format: savings deposit 300
        item2 = parse_bullet_line("savings deposit 300")
        self.assertIsNotNone(item2)
        self.assertEqual(item2["amount_cents"], 30000)
        self.assertEqual(item2["category"], "savings")

    def test_parse_date_headers(self):
        self.assertEqual(parse_date_header("2026-10-23"), "2026-10-23")
        self.assertEqual(parse_date_header("October 23, 2026"), "2026-10-23")
        self.assertEqual(parse_date_header("## Oct 23"), f"{datetime.now().year}-10-23")
        self.assertEqual(parse_date_header("10/23/2026"), "2026-10-23")
        self.assertIsNone(parse_date_header("just a random sentence without dates"))

        # Day/Month formats
        self.assertEqual(parse_date_header("23/10"), "2026-10-23")
        self.assertEqual(parse_date_header("15/8"), "2026-08-15")
        self.assertEqual(parse_date_header("15/08"), "2026-08-15")
        self.assertEqual(parse_date_header("01/07"), "2026-07-01")
        self.assertEqual(parse_date_header("1/7"), "2026-07-01")
        self.assertEqual(parse_date_header("Monday 23/10"), "2026-10-23")
        self.assertEqual(parse_date_header("## 15/08:"), "2026-08-15")
        self.assertEqual(parse_date_header("23 Oct"), "2026-10-23")
        self.assertEqual(parse_date_header("15th August"), "2026-08-15")

    def test_import_text_document_day_month_format(self):
        temp_dir = tempfile.TemporaryDirectory()
        try:
            store = FinanceStore(root_dir=Path(temp_dir.name) / "finances")
            doc = """
            # Summer to Fall Finances
            
            15/8
            - groceries - 40.00
            - gas - 30.00
            
            23/8
            - taqueria - 15.00
            
            01/09
            payday + 1350.00
            - rent - 0.00
            
            23/10
            - dinner - 45.00
            savings + 600.00
            """
            res = import_text_document(doc, store=store, default_year=2026)
            self.assertEqual(res["total_days"], 4)
            self.assertEqual(res["total_transactions"], 7)
            self.assertIn("2026-08-15", res["dates"])
            self.assertIn("2026-08-23", res["dates"])
            self.assertIn("2026-09-01", res["dates"])
            self.assertIn("2026-10-23", res["dates"])

            # Verify sorting: dates are sorted chronologically
            self.assertEqual(res["dates"], ["2026-08-15", "2026-08-23", "2026-09-01", "2026-10-23"])

            # Test delete_all_imported_transactions
            deleted = store.delete_all_imported_transactions()
            self.assertEqual(deleted, 7)
            self.assertEqual(len(store.list_transactions()), 0)
        finally:
            temp_dir.cleanup()

    def test_import_text_document(self):
        temp_dir = tempfile.TemporaryDirectory()
        try:
            store = FinanceStore(root_dir=Path(temp_dir.name) / "finances")
            doc = """
            # Daily Finances
            
            2026-08-15
            - groceries - 42.10
            - gas - 35.00
            payday + 1400.00
            
            2026-08-16
            - chipotle - 15.25
            - coffee - 4.50
            """
            res = import_text_document(store, doc)
            self.assertEqual(res["total_days"], 2)
            self.assertEqual(res["total_transactions"], 5)
            self.assertEqual(res["total_inflow_cents"], 140000)
            self.assertEqual(res["total_outflow_cents"], 4210 + 3500 + 1525 + 450)

            txs = store.list_transactions(limit=10)
            self.assertEqual(len(txs), 5)
        finally:
            temp_dir.cleanup()


class TestFinanceTuiPanel(unittest.TestCase):
    """Verifies FinanceTuiPanel data retrieval, ledger grouping, and caching."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault_path = Path(self.temp_dir.name) / "finances"
        self.store = FinanceStore(root_dir=self.vault_path)
        self.panel = FinanceTuiPanel(store=self.store)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_log_day_bullets_and_ledger(self):
        bullets = "- groceries - 30.00\n- gas - 40.00\npayday + 1500.00"
        logged = self.panel.log_day_bullets("2026-10-20", bullets)
        self.assertEqual(len(logged), 3)

        # Check daily ledger
        days = self.panel.get_daily_ledger()
        self.assertEqual(len(days), 1)
        day = days[0]
        self.assertEqual(day["date"], "2026-10-20")
        self.assertEqual(day["inflow_cents"], 150000)
        self.assertEqual(day["outflow_cents"], 7000)
        self.assertEqual(day["net_cents"], 150000 - 7000)
        self.assertEqual(len(day["items"]), 3)

    def test_multi_day_sorting(self):
        self.panel.log_day_bullets("2026-10-18", "- groceries - 20")
        self.panel.log_day_bullets("2026-10-22", "- gas - 50")
        self.panel.log_day_bullets("2026-10-20", "- dining - 15")

        days = self.panel.get_daily_ledger()
        self.assertEqual(len(days), 3)
        # Should be sorted descending by date
        self.assertEqual(days[0]["date"], "2026-10-22")
        self.assertEqual(days[1]["date"], "2026-10-20")
        self.assertEqual(days[2]["date"], "2026-10-18")

    def test_edit_and_delete_day(self):
        logged = self.panel.log_day_bullets("2026-10-23", "- groceries - 30.00\npayday + 1200.00")
        self.assertEqual(len(logged), 2)

        days = self.panel.get_daily_ledger()
        item = days[0]["items"][0]
        b_str = self.panel.item_to_bullet(item)
        self.assertIn("30", b_str)

        # Edit item
        updated = self.panel.edit_transaction(item["id"], "2026-10-23", "- groceries - 45.00")
        self.assertIsNotNone(updated)

        days_after = self.panel.get_daily_ledger()
        self.assertEqual(days_after[0]["outflow_cents"], 4500)

        # Delete entire day
        count = self.panel.delete_day("2026-10-23")
        self.assertEqual(count, 2)
        days_empty = self.panel.get_daily_ledger()
        self.assertEqual(len(days_empty), 0)


class TestCockpitTUIFinances(unittest.TestCase):
    """Verifies TUI layout methods, masking, and modal state management."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault_path = Path(self.temp_dir.name) / "finances"
        self.store = FinanceStore(root_dir=self.vault_path)

        # Mock curses stdscr
        self.mock_stdscr = MagicMock()
        self.mock_stdscr.getmaxyx.return_value = (35, 90)

        # Patch FinanceTuiPanel to use our test store
        self.panel = FinanceTuiPanel(store=self.store)
        self.tui = CockpitTUI.__new__(CockpitTUI)
        self.tui.stdscr = self.mock_stdscr
        self.tui.finance_panel = self.panel
        self.tui.telemetry = MagicMock()
        self.tui.email_panel = MagicMock()
        self.tui.safety_panel = MagicMock()
        self.tui.current_tab = 4
        self.tui.selected_email_idx = 0
        self.tui.email_scroll_offset = 0
        self.tui.notification = ""
        self.tui.notification_expiry = 0.0
        self.tui.emails = []
        self.tui.email_stats = {}
        self.tui.guard_status = {}
        self.tui.memory_stats = {}
        self.tui.memory_events = []
        self.tui.finance_days = []
        self.tui.finance_overview = None
        self.tui.finance_paycheck = None
        self.tui.finance_balances = {}
        self.tui.finances_focus = "days"
        self.tui.selected_day_idx = 0
        self.tui.day_scroll_offset = 0
        self.tui.selected_item_idx = 0
        self.tui.mask_amounts = False
        self.tui.is_adding_finances = False
        self.tui.is_editing_finances = False
        self.tui.editing_stage = "item"
        self.tui.editing_tx_id = None
        self.tui.editing_date = ""
        self.tui.editing_orig_text = ""
        self.tui.editing_buffer = ""
        self.tui.modal_stage = "date"
        self.tui.modal_date_buffer = ""
        self.tui.modal_line_buffer = ""
        self.tui.modal_batch_lines = []

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_money_formatting_and_masking(self):
        self.assertEqual(self.tui._fmt_money(12345, mask=False), "$123.45")
        self.assertEqual(self.tui._fmt_money(12345, mask=True), "$***.**")
        self.assertEqual(self.tui._fmt_money(12345, mask=False, signed=True), "+$123.45")
        self.assertEqual(self.tui._fmt_money(-12345, mask=False, signed=True), "-$123.45")

    def test_modal_workflow(self):
        self.tui.open_add_modal()
        self.assertTrue(self.tui.is_adding_finances)
        self.assertEqual(self.tui.modal_stage, "date")

        # Step 1: Type date and press Enter
        self.tui.modal_date_buffer = "2026-10-23"
        self.tui.handle_modal_key(10)  # Enter
        self.assertEqual(self.tui.modal_stage, "bullets")
        self.assertEqual(self.tui.modal_date_buffer, "2026-10-23")

        # Step 2: Add first item
        for char in "- groceries - 35.50":
            self.tui.handle_modal_key(ord(char))
        self.assertEqual(self.tui.modal_line_buffer, "- groceries - 35.50")
        self.tui.handle_modal_key(10)  # Enter adds item
        self.assertEqual(len(self.tui.modal_batch_lines), 1)
        self.assertEqual(self.tui.modal_line_buffer, "")

        # Step 3: Add second item
        for char in "gas - 25.00":
            self.tui.handle_modal_key(ord(char))
        self.tui.handle_modal_key(10)  # Enter adds item
        self.assertEqual(len(self.tui.modal_batch_lines), 2)

        # Step 4: Empty Enter commits all lines
        self.tui.handle_modal_key(10)
        self.assertFalse(self.tui.is_adding_finances)

        # Check that transactions were saved into store
        txs = self.store.list_transactions(limit=10)
        self.assertEqual(len(txs), 2)

    @unittest.mock.patch("curses.color_pair", return_value=0)
    def test_render_finances_view_does_not_crash(self, mock_cp):
        # Seed test data
        self.panel.log_day_bullets("2026-10-23", "- groceries - 50.00\npayday + 1200.00")
        self.tui.refresh_data()
        self.assertEqual(len(self.tui.finance_days), 1)

        # Calling draw_finances_view should render without throwing
        self.tui.draw_finances_view(35, 90)
        self.assertTrue(self.mock_stdscr.addstr.called)

    @unittest.mock.patch("curses.color_pair", return_value=0)
    def test_item_table_amount_signs_and_alignment(self, mock_cp):
        # Log dinner expense, savings transfer, and payday income
        bullets = "- dinner - 45.00\nsavings + 600.00\npayday + 1350.00"
        self.panel.log_day_bullets("2026-10-23", bullets)
        self.tui.refresh_data()
        self.tui.draw_finances_view(35, 90)

        # Inspect addstr calls
        addstr_args = [call[0] for call in self.mock_stdscr.addstr.call_args_list]

        # Verify expense (dinner) is formatted as -$45.00 (not +$45.00)
        has_negative_dinner = any("-$45.00" in str(arg) for call_arg in addstr_args for arg in call_arg)
        has_positive_dinner = any("+$45.00" in str(arg) for call_arg in addstr_args for arg in call_arg)
        self.assertTrue(has_negative_dinner, "Expense should show negative sign: -$45.00")
        self.assertFalse(has_positive_dinner, "Expense should NOT show positive sign")

        # Verify savings transfer is formatted as -$600.00 (not arrow ->$600.00)
        has_negative_savings = any("-$600.00" in str(arg) for call_arg in addstr_args for arg in call_arg)
        has_arrow_savings = any("→" in str(arg) for call_arg in addstr_args for arg in call_arg)
        self.assertTrue(has_negative_savings, "Savings should show negative sign: -$600.00")
        self.assertFalse(has_arrow_savings, "Savings should not display arrow symbol")

        # Verify income is formatted as +$1,350.00
        has_positive_payday = any("+$1,350.00" in str(arg) for call_arg in addstr_args for arg in call_arg)
        self.assertTrue(has_positive_payday, "Income should show positive sign: +$1,350.00")

    @unittest.mock.patch("curses.color_pair", return_value=0)
    def test_draw_add_day_modal_both_stages(self, mock_cp):
        # Test stage 1: date
        self.tui.open_add_modal()
        self.tui.draw_add_day_modal(35, 90)
        self.assertTrue(self.mock_stdscr.addstr.called)

        # Test stage 2: bullets
        self.tui.modal_stage = "bullets"
        self.tui.modal_batch_lines = ["- groceries - 25.50", "gas - 40"]
        self.tui.modal_line_buffer = "payday + 1"
        self.mock_stdscr.reset_mock()
        self.tui.draw_add_day_modal(35, 90)
        self.assertTrue(self.mock_stdscr.addstr.called)

    @unittest.mock.patch("curses.color_pair", return_value=0)
    def test_edit_modal_and_delete_in_tui(self, mock_cp):
        self.panel.log_day_bullets("2026-10-23", "- groceries - 50.00\npayday + 1200.00")
        self.tui.refresh_data()
        self.tui.finances_focus = "items"
        self.tui.selected_item_idx = 0

        # Open edit modal on item 0
        self.tui.open_edit_modal()
        self.assertTrue(self.tui.is_editing_finances)
        self.assertEqual(self.tui.editing_stage, "item")

        # Cycle account with Tab
        init_acc = self.tui.editing_account_idx
        self.tui.handle_edit_modal_key(9)  # Tab
        self.assertEqual(self.tui.editing_account_idx, (init_acc + 1) % 3)

        # Edit buffer and commit
        self.tui.editing_buffer = "- groceries - 22.50"
        self.tui.handle_edit_modal_key(10)
        self.assertFalse(self.tui.is_editing_finances)

        # Verify update
        days = self.panel.get_daily_ledger()
        self.assertEqual(days[0]["outflow_cents"], 2250)

        # Test draw edit modal stages
        self.tui.is_editing_finances = True
        self.tui.editing_stage = "day_menu"
        self.tui.draw_edit_modal(35, 90)
        self.tui.editing_stage = "item"
        self.tui.draw_edit_modal(35, 90)
        self.assertTrue(self.mock_stdscr.addstr.called)

        # Test delete selected finance
        self.tui.delete_selected_finance()
        days_after = self.panel.get_daily_ledger()
        self.assertEqual(len(days_after[0]["items"]), 1)


if __name__ == "__main__":
    unittest.main()
