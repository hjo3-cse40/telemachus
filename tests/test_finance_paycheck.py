"""Unit tests for Bi-Weekly Paycheck Allocation Engine."""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from telemachus.finance.models import PaycheckPlan
from telemachus.finance.recurring import RecurringEngine
from telemachus.finance.setup import apply_setup_yaml
from telemachus.finance.store import FinanceStore
from telemachus.mcp.server import handle_tool_call


class TestFinancePaycheck(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="telemachus_test_paycheck_")
        self.store = FinanceStore(root_dir=self.test_dir)
        self.recurring = RecurringEngine(self.store)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_paycheck_plan_via_yaml(self):
        yaml_path = Path(self.test_dir) / "test_setup.yaml"
        sample_yaml = """
as_of_date: "2026-10-01"
starting_balances:
  wells_fargo_checking: 1500.00
  capital_one_savings: 5000.00
  visa_credit_card: 0.00

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

monthly_targets:
  month: "2026-10"
  income: 2700.00
  savings_target: 1000.00
  groceries_planned: 300.00
  gas_planned: 150.00
  pge_estimate: 75.00

paycheck_plan:
  paycheck_amount: 1350.00
  savings_per_paycheck: 500.00
  frequency: "biweekly"
  anchor_date: "2026-10-15"
"""
        yaml_path.write_text(sample_yaml, encoding="utf-8")
        summary = apply_setup_yaml(yaml_path=yaml_path, store=self.store)
        self.assertTrue(summary.get("paycheck_plan_configured"))

        plan = self.store.get_paycheck_plan()
        self.assertIsNotNone(plan)
        self.assertEqual(plan.paycheck_amount_cents, 135000)
        self.assertEqual(plan.savings_per_paycheck_cents, 50000)
        self.assertEqual(plan.net_checking_cents, 85000)
        self.assertEqual(plan.anchor_date, "2026-10-15")

        # Test allocation for cycle starting 2026-10-15
        alloc = self.recurring.get_paycheck_allocation(as_of_date="2026-10-15")
        self.assertEqual(alloc.paycheck_date, "2026-10-15")
        self.assertEqual(alloc.next_paycheck_date, "2026-10-29")
        self.assertEqual(alloc.days_in_cycle, 14)
        self.assertEqual(alloc.paycheck_amount_cents, 135000)
        self.assertEqual(alloc.savings_transfer_cents, 50000)
        self.assertEqual(alloc.net_checking_cents, 85000)

        # In cycle Oct 15 - Oct 28:
        # Due Oct 15: Wi-Fi ($35.73), PG&E estimate (~$90.00)
        # Due Oct 25: Spotify ($11.99)
        # Variable needs: 50% groceries ($150.00) + 50% gas ($75.00) = $225.00
        self.assertEqual(alloc.groceries_allocated_cents, 15000)
        self.assertEqual(alloc.gas_allocated_cents, 7500)
        self.assertEqual(alloc.variable_needs_cents, 22500)
        self.assertGreater(alloc.bills_due_cents, 0)
        self.assertGreater(alloc.safe_to_spend_cents, 0)

        # Verify net arithmetic invariant:
        # safe_to_spend = max(0, net_checking - bills_due - variable_needs)
        expected_safe = max(0, 85000 - alloc.bills_due_cents - 22500)
        self.assertEqual(alloc.safe_to_spend_cents, expected_safe)

    def test_plan_breakdown_with_dining(self):
        yaml_path = Path(self.test_dir) / "test_setup.yaml"
        sample_yaml = """
as_of_date: "2026-10-01"
starting_balances:
  wells_fargo_checking: 2000.00
  capital_one_savings: 6000.00
  visa_credit_card: 0.00

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

monthly_targets:
  month: "2026-10"
  income: 2700.00
  savings_target: 1000.00
  groceries_planned: 300.00
  dining_planned: 100.00
  gas_planned: 150.00
  pge_estimate: 75.00

paycheck_plan:
  paycheck_amount: 1350.00
  savings_per_paycheck: 500.00
  frequency: "biweekly"
  anchor_date: "2026-10-15"
"""
        yaml_path.write_text(sample_yaml, encoding="utf-8")
        apply_setup_yaml(yaml_path=yaml_path, store=self.store)

        overview = self.store.get_monthly_overview("2026-10")
        self.assertEqual(overview.dining_planned_cents, 10000)

        # Total Personal: 2700 - 1000 - 260.71 - 75 - 300 - 100 - 150 = 814.29 -> 81429
        self.assertEqual(overview.total_personal_cents, 81429)

        # Paycheck allocation with dining
        alloc = self.recurring.get_paycheck_allocation(as_of_date="2026-10-15")
        self.assertEqual(alloc.groceries_allocated_cents, 15000)
        self.assertEqual(alloc.dining_allocated_cents, 5000)
        self.assertEqual(alloc.gas_allocated_cents, 7500)
        # Variable needs: 150 + 50 + 75 = 275.00 -> 27500
        self.assertEqual(alloc.variable_needs_cents, 27500)


if __name__ == "__main__":
    unittest.main()
