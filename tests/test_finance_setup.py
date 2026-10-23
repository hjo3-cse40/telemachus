"""Unit tests for Telemachus Finance Setup Wizard."""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from telemachus.finance.models import AccountType
from telemachus.finance.recurring import RecurringEngine
from telemachus.finance.setup import apply_setup_yaml, create_setup_yaml_template
from telemachus.finance.store import FinanceStore


class TestFinanceSetup(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="telemachus_test_setup_")
        self.store = FinanceStore(root_dir=self.test_dir)
        self.recurring = RecurringEngine(self.store)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_create_template(self):
        yaml_path = Path(self.test_dir) / "test_setup.yaml"
        created = create_setup_yaml_template(yaml_path)
        self.assertTrue(created.exists())
        self.assertEqual((created.stat().st_mode & 0o777), 0o600)

    def test_apply_setup_yaml(self):
        yaml_path = Path(self.test_dir) / "test_setup.yaml"
        sample_yaml = """
as_of_date: "2026-10-01"
starting_balances:
  wells_fargo_checking: 1540.25
  capital_one_savings: 5000.00
  visa_credit_card: 215.50

fixed_bills:
  kahram:
    amount: 150.00
    due_day: 1
    active: true
  gym:
    amount: 60.00
    due_day: 1
    active: true
  apple_icloud:
    amount: 2.99
    due_day: 11
    active: true
  spotify:
    amount: 11.99
    due_day: 25
    active: true
  wifi:
    amount: 35.73
    due_day: 15
    active: true
  rent:
    amount: 0.00
    due_day: 1
    active: false

monthly_targets:
  month: "2026-10"
  income: 2700.00
  savings_target: 1000.00
  groceries_planned: 300.00
  gas_planned: 150.00
  pge_estimate: 75.00
  notes: "Test setup"
"""
        yaml_path.write_text(sample_yaml, encoding="utf-8")

        summary = apply_setup_yaml(yaml_path=yaml_path, store=self.store)
        self.assertEqual(summary["balances_set"], 3)
        self.assertEqual(summary["rules_updated"], 6)
        self.assertTrue(summary["monthly_overview_updated"])
        self.assertEqual(summary["month"], "2026-10")
        # 150 + 60 + 2.99 + 11.99 + 35.73 = 260.71 -> 26071 cents
        self.assertEqual(summary["fixed_bills_total_cents"], 26071)

        # Verify account balances in store
        wf_bal = self.store.get_account_balance("Assets:Checking:WellsFargo")
        self.assertEqual(wf_bal, 154025)

        hysa_bal = self.store.get_account_balance("Assets:Savings:CapitalOne")
        self.assertEqual(hysa_bal, 500000)

        visa_bal = self.store.get_account_balance("Liabilities:CreditCard:Visa")
        self.assertEqual(visa_bal, 21550)

        # Net worth = (1540.25 + 5000.00) - 215.50 = 6324.75 -> 632475
        all_bal = self.store.get_all_balances()
        self.assertEqual(all_bal["net_worth_cents"], 632475)

        # Verify recurring rules
        wifi_rule = self.recurring.get_rule("rule_wifi")
        self.assertIsNotNone(wifi_rule)
        self.assertEqual(wifi_rule.base_amount_cents, 3573)
        self.assertEqual(wifi_rule.due_day_of_month, 15)

        # Verify monthly overview
        overview = self.store.get_monthly_overview("2026-10")
        self.assertEqual(overview.income_cents, 270000)
        self.assertEqual(overview.savings_target_cents, 100000)
        self.assertEqual(overview.fixed_bills_cents, 26071)
        self.assertEqual(overview.pge_cents, 7500)
        self.assertEqual(overview.all_bills_cents, 33571)
        self.assertEqual(overview.groceries_planned_cents, 30000)
        self.assertEqual(overview.gas_planned_cents, 15000)
        # Total Personal: 2700 - 1000 - 260.71 - 75 - 300 - 150 = 914.29 -> 91429
        self.assertEqual(overview.total_personal_cents, 91429)

    def test_idempotent_reapplication(self):
        """Applying setup multiple times should update existing opening balance without duplicating."""
        yaml_path = Path(self.test_dir) / "test_setup.yaml"
        sample_yaml_1 = """
as_of_date: "2026-10-01"
starting_balances:
  wells_fargo_checking: 1000.00
"""
        yaml_path.write_text(sample_yaml_1, encoding="utf-8")
        apply_setup_yaml(yaml_path=yaml_path, store=self.store)
        bal_1 = self.store.get_account_balance("Assets:Checking:WellsFargo")
        self.assertEqual(bal_1, 100000)

        sample_yaml_2 = """
as_of_date: "2026-10-01"
starting_balances:
  wells_fargo_checking: 1200.00
"""
        yaml_path.write_text(sample_yaml_2, encoding="utf-8")
        apply_setup_yaml(yaml_path=yaml_path, store=self.store)
        bal_2 = self.store.get_account_balance("Assets:Checking:WellsFargo")
        self.assertEqual(bal_2, 120000)


if __name__ == "__main__":
    unittest.main()
