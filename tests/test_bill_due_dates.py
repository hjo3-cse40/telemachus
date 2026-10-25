"""Regression tests: end-of-month bills, month-turn payments, and estimate labelling (synthetic data only)."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from telemachus.finance.models import Posting, Transaction
from telemachus.finance.recurring import RecurringEngine
from telemachus.finance.setup import apply_setup_yaml
from telemachus.finance.store import FinanceStore
from telemachus.mcp.server import _bill_amount

SETUP_YAML = """
starting_balances:
  wells_fargo_checking: 1000.00

fixed_bills:
  wifi:
    amount: 20.00
    due_day: 30
    active: true
  pge:
    amount: 15.00
    due_day: 30
    active: true

monthly_targets:
  month: "2026-10"
  pge_estimate: 15.00

paycheck_plan:
  paycheck_amount: 1000.00
  savings_per_paycheck: 500.00
  frequency: "biweekly"
  anchor_date: "2026-10-22"
"""


class TestEndOfMonthBills(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.yaml_path = Path(self.temp_dir.name) / "setup.yaml"
        self.yaml_path.write_text(SETUP_YAML, encoding="utf-8")
        self.store = FinanceStore(root_dir=self.temp_dir.name)
        self.store.setup_yaml_path = self.yaml_path
        apply_setup_yaml(yaml_path=self.yaml_path, store=self.store)
        self.engine = RecurringEngine(self.store, yaml_path=self.yaml_path)

    def _pay(self, date: str, account: str, cents: int) -> None:
        self.store.log_transaction(
            Transaction.create(
                date=date,
                payee=account,
                postings=[Posting("Assets:Checking:WellsFargo", -cents), Posting(account, cents)],
                memo="test",
            )
        )

    def _window_bill(self, as_of: str, name_part: str):
        alloc = self.engine.get_paycheck_allocation(as_of_date=as_of, yaml_path=self.yaml_path)
        return next(b for b in alloc.bills if name_part in b["name"])

    def test_pge_and_wifi_due_on_the_30th(self):
        for part in ("PG&E", "WiFi"):
            self.assertEqual(self._window_bill("2026-10-25", part)["due_date"], "2026-10-30")

    def test_month_turn_payment_does_not_mark_next_month_paid(self):
        # Paid on Oct 1 for September's bill; October's bill (due Oct 30) is still unpaid.
        self._pay("2026-10-01", "Expenses:Utilities:PGE", 1056)
        self._pay("2026-10-01", "Expenses:Utilities:WiFi", 2000)
        self.assertEqual(self._window_bill("2026-10-25", "PG&E")["status"], "due")
        self.assertEqual(self._window_bill("2026-10-25", "WiFi")["status"], "due")

    def test_payment_near_due_date_marks_paid(self):
        # Paying the October bill on Nov 1 (2 days after the 30th) counts for October.
        self._pay("2026-11-01", "Expenses:Utilities:PGE", 1500)
        self.assertTrue(self.engine._is_bill_paid_for_due_date("Expenses:Utilities:PGE", "2026-10-30"))
        self.assertFalse(self.engine._is_bill_paid_for_due_date("Expenses:Utilities:PGE", "2026-11-30"))

    def test_day_30_clamps_to_end_of_february(self):
        # Paydays fall every 14 days from 2026-10-22, so 2027-02-18 is a payday window start
        # that runs past Feb 28 only if the window is long enough; use a start date near month end.
        alloc = self.engine.get_paycheck_allocation(as_of_date="2027-02-25", yaml_path=self.yaml_path)
        pge = [b for b in alloc.bills if "PG&E" in b["name"]]
        self.assertEqual([b["due_date"] for b in pge], ["2027-02-28"])
        self.assertEqual(RecurringEngine._due_date_for_month(30, "2027-02"), "2027-02-28")

    def test_pge_is_flagged_estimate_and_excluded_from_fixed_total(self):
        self.assertTrue(self._window_bill("2026-10-25", "PG&E")["is_estimate"])
        self.assertFalse(self._window_bill("2026-10-25", "WiFi")["is_estimate"])
        breakdown = self.engine.get_all_recurring_bills_breakdown("2026-10", yaml_path=self.yaml_path)
        self.assertEqual(breakdown["fixed_bills_total_cents"], 2000)  # wifi only
        pge = next(b for b in breakdown["bills"] if b["key"] == "pge")
        self.assertTrue(pge["is_variable"])

    def test_estimate_is_visibly_marked_in_tool_output(self):
        self.assertEqual(_bill_amount(1500, True), "~$15.00 (ESTIMATE)")
        self.assertEqual(_bill_amount(2000, False), "$20.00")


if __name__ == "__main__":
    unittest.main()
