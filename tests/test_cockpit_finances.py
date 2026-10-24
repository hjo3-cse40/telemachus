"""Unit tests for Cockpit Finance Panel, Text Importer, and Daily Inspector."""
from __future__ import annotations

import curses
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from telemachus.cockpit.finance_panel import FinanceTuiPanel
from telemachus.cockpit.telemetry import CockpitTelemetry
from telemachus.cockpit.tui import CockpitTUI
from telemachus.finance.import_text import (
    import_text_document,
    parse_bullet_line,
    parse_date_header,
)
from telemachus.finance.models import Posting, RecurringRule, RuleType, Transaction
from telemachus.finance.recurring import RecurringEngine
from telemachus.finance.setup import apply_setup_yaml
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

    def test_parse_bullet_credit_card_payment(self):
        # Format: credit card payment - 250
        item = parse_bullet_line("credit card payment - 250")
        self.assertIsNotNone(item)
        self.assertEqual(item["amount_cents"], 25000)
        self.assertEqual(item["category"], "credit_card_payment")
        self.assertEqual(item["from_account"], "Assets:Checking:WellsFargo")
        self.assertEqual(item["to_account"], "Liabilities:CreditCard:Visa")

        # Format: - cc payment - 150
        item2 = parse_bullet_line("- cc payment - 150")
        self.assertIsNotNone(item2)
        self.assertEqual(item2["amount_cents"], 15000)
        self.assertEqual(item2["category"], "credit_card_payment")
        self.assertEqual(item2["from_account"], "Assets:Checking:WellsFargo")
        self.assertEqual(item2["to_account"], "Liabilities:CreditCard:Visa")

        # Format: credit card 300
        item3 = parse_bullet_line("credit card 300")
        self.assertIsNotNone(item3)
        self.assertEqual(item3["amount_cents"], 30000)
        self.assertEqual(item3["category"], "credit_card_payment")

    def test_parse_date_headers(self):
        self.assertEqual(parse_date_header("2026-10-23"), "2026-10-23")
        self.assertEqual(parse_date_header("October 23, 2026"), "2026-10-23")
        self.assertEqual(parse_date_header("## Oct 23"), f"{datetime.now().year}-10-23")
        self.assertEqual(parse_date_header("10/23/2026"), "2026-10-23")
        self.assertIsNone(parse_date_header("just a random sentence without dates"))

        # MM/DD formats (standard 7/30 to 10/23)
        self.assertEqual(parse_date_header("7/30"), "2026-07-30")
        self.assertEqual(parse_date_header("8/1"), "2026-08-01")
        self.assertEqual(parse_date_header("08/01"), "2026-08-01")
        self.assertEqual(parse_date_header("8/15"), "2026-08-15")
        self.assertEqual(parse_date_header("9/12"), "2026-09-12")
        self.assertEqual(parse_date_header("09/12"), "2026-09-12")
        self.assertEqual(parse_date_header("10/12"), "2026-10-12")
        self.assertEqual(parse_date_header("10/23"), "2026-10-23")
        self.assertEqual(parse_date_header("Monday 10/23"), "2026-10-23")
        self.assertEqual(parse_date_header("## 8/15:"), "2026-08-15")
        self.assertEqual(parse_date_header("23 Oct"), "2026-10-23")
        self.assertEqual(parse_date_header("15th August"), "2026-08-15")

        # 12/September variations
        self.assertEqual(parse_date_header("12/September"), "2026-09-12")
        self.assertEqual(parse_date_header("September 12"), "2026-09-12")
        self.assertEqual(parse_date_header("September/12"), "2026-09-12")
        self.assertEqual(parse_date_header("12/Sep"), "2026-09-12")
        self.assertEqual(parse_date_header("12/Sept"), "2026-09-12")
        self.assertEqual(parse_date_header("12-September"), "2026-09-12")
        self.assertEqual(parse_date_header("9/12/2026"), "2026-09-12")
        self.assertEqual(parse_date_header("7/30/2026"), "2026-07-30")

    def test_import_text_document_day_month_format(self):
        temp_dir = tempfile.TemporaryDirectory()
        try:
            store = FinanceStore(root_dir=Path(temp_dir.name) / "finances")
            doc = """
            # Summer to Fall Finances
            
            7/30
            - groceries - 40.00
            - gas - 30.00
            
            8/15
            - taqueria - 15.00
            
            9/12
            payday + 1350.00
            - rent - 0.00
            
            10/23
            - dinner - 45.00
            savings + 600.00
            """
            res = import_text_document(doc, store=store, default_year=2026)
            self.assertEqual(res["total_days"], 4)
            self.assertEqual(res["total_transactions"], 7)
            self.assertIn("2026-07-30", res["dates"])
            self.assertIn("2026-08-15", res["dates"])
            self.assertIn("2026-09-12", res["dates"])
            self.assertIn("2026-10-23", res["dates"])

            # Verify sorting: dates are sorted chronologically
            self.assertEqual(res["dates"], ["2026-07-30", "2026-08-15", "2026-09-12", "2026-10-23"])

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

    def test_all_payments_default_to_checking_and_credit_card_payment(self):
        bullets = (
            "- groceries - 50.00\n"
            "- dinner - 35.00\n"
            "- credit card payment - 200.00\n"
            "savings + 400.00\n"
            "payday + 1500.00"
        )
        self.panel.log_day_bullets("2026-10-23", bullets)
        days = self.panel.get_daily_ledger()
        self.assertEqual(len(days), 1)
        day = days[0]
        # Inflow: payday 1500
        self.assertEqual(day["inflow_cents"], 150000)
        # Outflow: 50 groceries + 35 dinner + 200 cc payment = 285
        self.assertEqual(day["outflow_cents"], 28500)
        # Savings: 400
        self.assertEqual(day["savings_cents"], 40000)

        # Verify all items show funding account as 'Checking'
        items = day["items"]
        for itm in items:
            self.assertEqual(itm["account"], "Checking")

        # Verify credit card payment category
        cc_item = next(i for i in items if "Credit Card" in i["category"] or "credit card" in i["payee"].lower())
        self.assertEqual(cc_item["amount_cents"], 20000)
        self.assertFalse(cc_item["is_income"])

        # Verify postings in store: checking balance was deducted
        chk_bal = self.store.get_account_balance("Assets:Checking:WellsFargo")
        # Checking: +1500 (payday) - 50 (groceries) - 35 (dinner) - 200 (cc payment) - 400 (savings) = +815
        self.assertEqual(chk_bal, 81500)


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

    def test_two_week_due_dates_across_month_boundary_with_credit_card_and_already_paid(self):
        """Verifies 14-day window properly spans Oct 24 - Nov 06, evaluating late Oct bills, Nov 1-2 bills,
        and querying live credit card balance and Kahram baseline already_paid."""
        yaml_path = Path(self.temp_dir.name) / "test_setup.yaml"
        sample_yaml = """
as_of_date: "2026-10-24"
starting_balances:
  wells_fargo_checking: 1500.00
  capital_one_savings: 5000.00
  visa_credit_card: 45.00

fixed_bills:
  kahram:
    amount: 150.00
    due_day: 1
    already_paid: 50.00
    active: true
  gym:
    amount: 60.00
    due_day: 1
    active: true
  wifi:
    amount: 35.73
    due_day: 28
    active: true
  pge:
    amount: 85.00
    due_day: 28
    active: true
  apple_icloud:
    amount: 2.99
    due_day: 11
    active: true
  spotify:
    amount: 11.99
    due_day: 20
    active: true

goals:
  brother:
    target: 150.00
    already_paid: 50.00
  savings:
    target: 1000.00
    already_saved: 400.00

paycheck_plan:
  paycheck_amount: 1350.00
  savings_per_paycheck: 500.00
  frequency: "biweekly"
  anchor_date: "2026-10-24"

monthly_targets:
  month: "2026-10"
  income: 2700.00
  savings_target: 1000.00
  groceries_planned: 300.00
  dining_planned: 100.00
  gas_planned: 150.00
  pge_estimate: 85.00
"""
        yaml_path.write_text(sample_yaml, encoding="utf-8")
        apply_setup_yaml(yaml_path=yaml_path, store=self.store)

        # Confirm Visa liability balance is $45.00 (4500 cents)
        cc_bal = self.store.get_account_balance("Liabilities:CreditCard:Visa")
        self.assertEqual(cc_bal, 4500)

        recurring = RecurringEngine(self.store, yaml_path=yaml_path)
        alloc = recurring.get_paycheck_allocation(as_of_date="2026-10-24")

        # Cycle is 2026-10-24 through 2026-11-06 (14 days)
        self.assertEqual(alloc.paycheck_date, "2026-10-24")
        self.assertEqual(alloc.next_paycheck_date, "2026-11-07")
        self.assertEqual(alloc.days_in_cycle, 14)

        # Income & Savings
        self.assertEqual(alloc.paycheck_amount_cents, 135000)
        self.assertEqual(alloc.savings_transfer_cents, 50000)
        self.assertEqual(alloc.net_checking_cents, 85000)

        # Variable needs: (300/2) + (100/2) + (150/2) = 150 + 50 + 75 = 275.00
        self.assertEqual(alloc.variable_needs_cents, 27500)

        # Bills Due within window:
        # 1. WiFi (Oct 28): $35.73 -> 3573
        # 2. PG&E (Oct 28): $85.00 -> 8500
        # 3. Kahram / Brother (Nov 1): $150.00 - $50.00 (already_paid) = $100.00 -> 10000
        # 4. Gym (Nov 1): $60.00 -> 6000
        # 5. Credit Card (Nov 2): $45.00 -> 4500 (from live Visa balance)
        # Expected total bills due: 3573 + 8500 + 10000 + 6000 + 4500 = 32573 ($325.73)
        bill_names = [b["name"] for b in alloc.bills_due]
        self.assertTrue(any("WiFi" in n for n in bill_names))
        self.assertTrue(any("PG&E" in n for n in bill_names))
        self.assertTrue(any("Brother" in n or "Kahram" in n for n in bill_names))
        self.assertTrue(any("Gym" in n for n in bill_names))
        self.assertTrue(any("Credit Card" in n for n in bill_names))

        # Check Brother bill note mentions already paid
        bro_bill = [b for b in alloc.bills_due if "Brother" in b["name"] or "Kahram" in b["name"]][0]
        self.assertEqual(bro_bill["amount_cents"], 10000)
        self.assertIn("$50.00 paid", bro_bill["notes"])

        # Check Credit card bill note mentions balance
        cc_bill = [b for b in alloc.bills_due if "Credit Card" in b["name"]][0]
        self.assertEqual(cc_bill["amount_cents"], 4500)
        self.assertIn("$45.00 current balance due", cc_bill["notes"])

        self.assertEqual(alloc.bills_due_cents, 32573)

        # Check out-of-window bills are segregated in other_bills
        other_names = [b["name"] for b in alloc.other_bills]
        self.assertTrue(any("iCloud" in n for n in other_names))
        self.assertTrue(any("Spotify" in n for n in other_names))

        # Safe-to-spend: 850.00 - 325.73 - 275.00 = 249.27 (24927 cents)
        self.assertEqual(alloc.safe_to_spend_cents, 24927)

    def test_goal_progress_tracker_with_already_paid(self):
        """Verifies live ASCII progress bars for monthly Savings and Brother payment targets."""
        yaml_path = Path(self.temp_dir.name) / "test_setup.yaml"
        sample_yaml = """
goals:
  brother:
    target: 150.00
    already_paid: 75.00
  savings:
    target: 1000.00
    already_saved: 500.00
"""
        yaml_path.write_text(sample_yaml, encoding="utf-8")
        self.store.setup_yaml_path = yaml_path

        goals = self.store.get_goals_progress("2026-10")
        savings_goal = [g for g in goals if "Savings" in g.name][0]
        brother_goal = [g for g in goals if "Brother" in g.name or "Kahram" in g.name][0]

        self.assertEqual(savings_goal.target_cents, 100000)
        self.assertEqual(savings_goal.saved_cents, 50000)
        self.assertEqual(savings_goal.progress_pct, 50.0)
        self.assertIn("50.0%", savings_goal.progress_bar)

        self.assertEqual(brother_goal.target_cents, 15000)
        self.assertEqual(brother_goal.saved_cents, 7500)
        self.assertEqual(brother_goal.progress_pct, 50.0)
        self.assertIn("50.0%", brother_goal.progress_bar)

    @unittest.mock.patch("curses.color_pair", return_value=0)
    def test_cockpit_tui_finance_view_rendering_with_cards_and_bars(self, mock_cp):
        """Verifies Cockpit Tab [4: Finances] renders summary cards, progress bars, and allocation."""
        yaml_path = Path(self.temp_dir.name) / "test_setup.yaml"
        sample_yaml = """
goals:
  brother:
    target: 150.00
    already_paid: 100.00
  savings:
    target: 1000.00
    already_saved: 800.00
paycheck_plan:
  paycheck_amount: 1350.00
  savings_per_paycheck: 500.00
  frequency: "biweekly"
  anchor_date: "2026-10-24"
"""
        yaml_path.write_text(sample_yaml, encoding="utf-8")
        self.store.setup_yaml_path = yaml_path

        self.tui.refresh_data()
        self.tui.current_tab = 4
        self.mock_stdscr.reset_mock()
        self.tui.draw_finances_view(35, 90)

        addstr_calls = [c[0] for c in self.mock_stdscr.addstr.call_args_list]
        combined_text = " ".join(str(arg) for call in addstr_calls for arg in call)

        self.assertIn("BALANCES", combined_text)
        self.assertIn("2-WEEK WINDOW", combined_text)
        self.assertIn("Savings", combined_text)
        self.assertIn("Brother", combined_text)
        self.assertIn("Left:", combined_text)

    def test_telemetry_lm_studio_status_parsing(self):
        """Verifies LM Studio live status indicators in telemetry banner."""
        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as f:
            status_file = Path(f.name)

        try:
            telem = CockpitTelemetry()
            # 1. Processing Prompt state
            status_file.write_text(
                json.dumps({
                    "status": "processing_prompt",
                    "model": "qwen3.5-9b",
                    "start_time": time.time(),
                    "tokens_produced": 0,
                    "speed_tps": 0.0,
                }),
                encoding="utf-8",
            )
            st = telem.get_llm_status(status_path=status_file)
            self.assertEqual(st["status"], "processing_prompt")
            header = telem.format_single_line_header(max_width=120, status_path=status_file)
            self.assertIn("LM Studio: Processing Prompt...", header)

            # 2. Producing Tokens state
            status_file.write_text(
                json.dumps({
                    "status": "producing_tokens",
                    "model": "qwen3.5-9b",
                    "start_time": time.time() - 2.0,
                    "tokens_produced": 48,
                    "speed_tps": 24.0,
                }),
                encoding="utf-8",
            )
            st2 = telem.get_llm_status(status_path=status_file)
            self.assertEqual(st2["status"], "producing_tokens")
            header2 = telem.format_single_line_header(max_width=120, status_path=status_file)
            self.assertIn("Producing (48 tok · 24.0 t/s)", header2)

            # 3. Idle / Ready state
            status_file.write_text(
                json.dumps({
                    "status": "idle",
                    "model": "qwen3.5-9b",
                    "start_time": time.time(),
                    "tokens_produced": 0,
                    "speed_tps": 0.0,
                }),
                encoding="utf-8",
            )
            st3 = telem.get_llm_status(status_path=status_file)
            self.assertEqual(st3["status"], "idle")
            header3 = telem.format_single_line_header(max_width=120, status_path=status_file)
            self.assertIn("MODEL:", header3)
        finally:
            if status_file.exists():
                status_file.unlink()

    def test_savings_goal_from_starting_balances_capital_one(self):
        """Verifies Savings goal progress pulls directly from starting_balances.capital_one_savings."""
        yaml_path = Path(self.temp_dir.name) / "test_setup.yaml"
        sample_yaml = """
starting_balances:
  wells_fargo_checking: 1200.00
  capitol_one_saving: 750.00
  visa_credit_card: 0.00

monthly_targets:
  savings_target: 1000.00

goals:
  brother:
    target: 150.00
    already_paid: 150.00
"""
        yaml_path.write_text(sample_yaml, encoding="utf-8")
        self.store.setup_yaml_path = yaml_path

        goals = self.store.get_goals_progress("2026-10")
        savings_goal = [g for g in goals if "Savings" in g.name][0]
        brother_goal = [g for g in goals if "Brother" in g.name or "Kahram" in g.name][0]

        # Savings should go directly off capitol_one_saving starting balance ($750.00)
        self.assertEqual(savings_goal.saved_cents, 75000)
        self.assertEqual(savings_goal.target_cents, 100000)
        self.assertEqual(savings_goal.progress_pct, 75.0)
        self.assertIn("75.0%", savings_goal.progress_bar)

        # Brother payment is 100% completed
        self.assertEqual(brother_goal.saved_cents, 15000)
        self.assertEqual(brother_goal.status, "completed")


if __name__ == "__main__":
    unittest.main()
