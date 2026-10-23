"""Unit test suite for Telemachus Double-Entry Financial Engine (Phase 1)."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from telemachus.finance.export import FinanceExporter
from telemachus.finance.masking import FinanceMasker
from telemachus.finance.models import (
    Account,
    AccountType,
    BillStatus,
    Posting,
    RecurringRule,
    RuleType,
    Transaction,
)
from telemachus.finance.recurring import RecurringEngine
from telemachus.finance.store import (
    AccountNotFoundError,
    FinanceStore,
    UnbalancedTransactionError,
)


class TestFinanceEngine(unittest.TestCase):
    """Tests the core double-entry engine, recurring calculations, and masking proxy."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault_path = Path(self.temp_dir.name) / "finances"
        self.store = FinanceStore(root_dir=self.vault_path)
        self.recurring = RecurringEngine(self.store)
        self.masker = FinanceMasker()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_01_storage_initialization_and_permissions(self):
        """Verifies 0700 directory permissions and table initialization."""
        self.assertTrue(self.vault_path.is_dir())
        mode = oct(os.stat(self.vault_path).st_mode & 0o777)
        self.assertEqual(mode, "0o700")

        # Verify default accounts were seeded
        accounts = self.store.list_accounts()
        acc_ids = {a.id for a in accounts}
        self.assertIn("Assets:Checking:WellsFargo", acc_ids)
        self.assertIn("Assets:Savings:CapitalOne", acc_ids)
        self.assertIn("Liabilities:CreditCard:Visa", acc_ids)
        self.assertIn("Expenses:Utilities:PGE", acc_ids)
        self.assertIn("Expenses:Food:Groceries", acc_ids)
        self.assertIn("Expenses:Food:Dining", acc_ids)
        self.assertIn("Income:Salary", acc_ids)

    def test_02_double_entry_balance_invariant(self):
        """Validates that unbalanced transactions are strictly rejected."""
        # Unbalanced: $50 credit from checking, but only $40 debit to groceries
        unbalanced_tx = Transaction.create(
            date="2026-10-22",
            payee="Safeway",
            postings=[
                Posting("Assets:Checking:WellsFargo", -5000),
                Posting("Expenses:Food:Groceries", 4000),
            ],
            memo="Unbalanced test",
        )
        self.assertFalse(unbalanced_tx.is_balanced())
        with self.assertRaises(UnbalancedTransactionError):
            self.store.log_transaction(unbalanced_tx)

    def test_03_balanced_transaction_and_integer_math(self):
        """Tests that balanced transactions are committed and balances are exact to the cent."""
        # 1. Initial Deposit / Paycheck: $3,200.00
        paycheck = Transaction.create(
            date="2026-10-15",
            payee="Employer Payroll",
            postings=[
                Posting("Income:Salary", -320000),  # Credit income
                Posting("Assets:Checking:WellsFargo", 320000),  # Debit asset
            ],
            memo="Bi-weekly direct deposit",
        )
        self.store.log_transaction(paycheck)

        # 2. Spend at Safeway: $64.20 on Visa Credit Card
        safeway = Transaction.create(
            date="2026-10-16",
            payee="Safeway",
            postings=[
                Posting("Liabilities:CreditCard:Visa", -6420),  # Credit liability (increase debt)
                Posting("Expenses:Food:Groceries", 6420),  # Debit expense
            ],
            memo="Weekly groceries",
        )
        self.store.log_transaction(safeway)

        # 3. Transfer to High Yield Savings: $500.00
        savings_transfer = Transaction.create(
            date="2026-10-17",
            payee="Internal Transfer",
            postings=[
                Posting("Assets:Checking:WellsFargo", -50000),  # Outflow
                Posting("Assets:Savings:CapitalOne", 50000),   # Inflow
            ],
            memo="Transfer to Capital One HYSA",
        )
        self.store.log_transaction(savings_transfer)

        # Verify Balances
        wf_bal = self.store.get_account_balance("Assets:Checking:WellsFargo")
        cap_bal = self.store.get_account_balance("Assets:Savings:CapitalOne")
        visa_bal = self.store.get_account_balance("Liabilities:CreditCard:Visa")
        salary_bal = self.store.get_account_balance("Income:Salary")
        groc_bal = self.store.get_account_balance("Expenses:Food:Groceries")

        self.assertEqual(wf_bal, 270000)   # $3,200 - $500 = $2,700.00
        self.assertEqual(cap_bal, 50000)   # $500.00
        self.assertEqual(visa_bal, 6420)   # Owes $64.20
        self.assertEqual(salary_bal, 320000) # $3,200 earned
        self.assertEqual(groc_bal, 6420)   # $64.20 spent

        # Check Net Worth Summary
        summary = self.store.get_all_balances()
        self.assertEqual(summary["liquid_assets_cents"], 320000)  # 2,700 + 500 = 3,200
        self.assertEqual(summary["liabilities_cents"], 6420)      # 64.20 debt
        self.assertEqual(summary["net_worth_cents"], 313580)      # 3,200 - 64.20 = $3,135.80

    def test_04_cash_flow_summary_calculation(self):
        """Verifies inflow, outflow, savings rate, and daily burn aggregations."""
        # Income: $2,000
        self.store.log_transaction(
            Transaction.create(
                date="2026-10-01",
                payee="Payroll",
                postings=[
                    Posting("Income:Salary", -200000),
                    Posting("Assets:Checking:WellsFargo", 200000),
                ],
            )
        )
        # Expense: $300 Dining
        self.store.log_transaction(
            Transaction.create(
                date="2026-10-05",
                payee="Restaurants",
                postings=[
                    Posting("Liabilities:CreditCard:Visa", -30000),
                    Posting("Expenses:Food:Dining", 30000),
                ],
            )
        )
        # Expense: $100 Utilities
        self.store.log_transaction(
            Transaction.create(
                date="2026-10-10",
                payee="Internet Provider",
                postings=[
                    Posting("Assets:Checking:WellsFargo", -10000),
                    Posting("Expenses:Utilities:Internet", 10000),
                ],
            )
        )

        summary = self.store.get_cash_flow_summary("2026-10-01", "2026-10-31")
        self.assertEqual(summary.inflow_cents, 200000)
        self.assertEqual(summary.outflow_cents, 40000)
        self.assertEqual(summary.net_savings_cents, 160000)
        self.assertEqual(summary.savings_rate_pct, 80.0)
        self.assertEqual(summary.category_breakdown["Expenses:Food:Dining"], 30000)
        self.assertEqual(summary.category_breakdown["Expenses:Utilities:Internet"], 10000)

    def test_05_variable_pge_bill_estimation(self):
        """Tests rolling average, standard deviation, and seasonal buffer for PG&E."""
        # Log 6 past months of PG&E payments: $60, $70, $95, $110, $82, $65
        pge_history = [
            ("2026-05-15", 6000),
            ("2026-06-15", 7000),
            ("2026-07-15", 9500),
            ("2026-08-15", 11000),
            ("2026-09-15", 8200),
            ("2026-10-15", 6500),
        ]
        for dt, cents in pge_history:
            self.store.log_transaction(
                Transaction.create(
                    date=dt,
                    payee="PG&E Utility",
                    postings=[
                        Posting("Assets:Checking:WellsFargo", -cents),
                        Posting("Expenses:Utilities:PGE", cents),
                    ],
                )
            )

        # Estimate upcoming November bill
        est_cents, stats = self.recurring.compute_variable_estimate("rule_pge", "2026-11", lookback_months=6)

        self.assertEqual(stats["count"], 6)
        self.assertEqual(stats["min_cents"], 6000)
        self.assertEqual(stats["max_cents"], 11000)
        self.assertGreater(stats["std_dev_cents"], 0)
        # Conservative estimate should be >= min and <= max
        self.assertTrue(6000 <= est_cents <= 11000)

    def test_06_bill_lifecycle_and_reconciliation(self):
        """Tests SCHEDULED -> BILLED -> PAID progression and transaction reconciliation."""
        cycle = "2026-11"
        # 1. Scheduled
        instance = self.recurring.ensure_bill_instance("rule_internet", cycle)
        self.assertEqual(instance.status, BillStatus.SCHEDULED)
        self.assertEqual(instance.effective_amount_cents, 5000)

        # 2. Statement arrived (Billed at $52.50 due to taxes)
        billed_instance = self.recurring.set_actual_bill_amount("rule_internet", cycle, 5250)
        self.assertEqual(billed_instance.status, BillStatus.BILLED)
        self.assertEqual(billed_instance.effective_amount_cents, 5250)

        # 3. Payment occurs in checking account
        self.store.log_transaction(
            Transaction.create(
                date="2026-11-05",
                payee="Sonic Internet",
                postings=[
                    Posting("Assets:Checking:WellsFargo", -5250),
                    Posting("Expenses:Utilities:Internet", 5250),
                ],
            )
        )

        # Reconcile
        self.recurring.reconcile_with_transactions(lookback_days=30)
        updated = self.recurring.ensure_bill_instance("rule_internet", cycle)
        self.assertEqual(updated.status, BillStatus.PAID)
        self.assertEqual(updated.actual_amount_cents, 5250)

    def test_07_committed_cash_flow_and_safe_to_spend(self):
        """Verifies liquid capital minus upcoming commitments yields safe-to-spend buffer."""
        # Deposit $1,500 into Checking
        self.store.log_transaction(
            Transaction.create(
                date="2026-10-20",
                payee="Payroll",
                postings=[
                    Posting("Income:Salary", -150000),
                    Posting("Assets:Checking:WellsFargo", 150000),
                ],
            )
        )

        cash_flow = self.recurring.get_committed_cash_flow(days_ahead=30)
        self.assertEqual(cash_flow["liquid_capital_cents"], 150000)
        self.assertGreater(cash_flow["committed_cents"], 0)
        self.assertEqual(
            cash_flow["safe_to_spend_cents"],
            cash_flow["liquid_capital_cents"] - cash_flow["committed_cents"],
        )

    def test_08_privacy_masking_proxy(self):
        """Validates that sensitive identifiers and dollar amounts are tokenized cleanly."""
        # 1. Text sanitization
        text = "My SSN is 123-45-6789 and card is 4111 2222 3333 4444"
        masked_text = self.masker.mask_text(text)
        self.assertNotIn("123-45-6789", masked_text)
        self.assertNotIn("4111", masked_text)
        self.assertIn("<SSN_REDACTED_***-6789>", masked_text)
        self.assertIn("<CARD_ENDING_4444>", masked_text)

        # 2. Balance dictionary masking
        raw_balances = {
            "liquid_assets_cents": 540000,
            "liabilities_cents": 12000,
            "net_worth_cents": 528000,
            "account_balances": {
                "Assets:Checking:WellsFargo": 420000,
                "Liabilities:CreditCard:Visa": 12000,
            },
            "as_of_date": "2026-10-22",
        }
        masked_bal = self.masker.mask_balance_dict(raw_balances)
        self.assertEqual(masked_bal["liquid_assets"], "<TOTAL_LIQUID_ASSETS>")
        self.assertEqual(masked_bal["net_worth"], "<NET_WORTH>")
        self.assertEqual(
            masked_bal["account_tokens"]["Assets:Checking:WellsFargo"],
            "<BALANCE_WELLSFARGO>",
        )

    def test_09_beancount_and_csv_export(self):
        """Verifies plain-text Beancount projection and CSV export generation."""
        self.store.log_transaction(
            Transaction.create(
                date="2026-10-22",
                payee="Trader Joe's",
                postings=[
                    Posting("Liabilities:CreditCard:Visa", -4520),
                    Posting("Expenses:Food:Groceries", 4520),
                ],
                memo="Weekly food",
            )
        )

        exporter = FinanceExporter(self.store)
        beancount_path = exporter.export_to_beancount()
        self.assertTrue(beancount_path.exists())
        beancount_content = beancount_path.read_text()
        self.assertIn("open Assets:Checking:WellsFargo USD", beancount_content)
        self.assertIn('2026-10-22 * "Trader Joe\'s" "Weekly food"', beancount_content)
        self.assertIn("45.20 USD", beancount_content)

        csv_path = exporter.export_to_csv()
        self.assertTrue(csv_path.exists())
        csv_content = csv_path.read_text()
        self.assertIn("Trader Joe's", csv_content)
        self.assertIn("45.20", csv_content)


if __name__ == "__main__":
    unittest.main()
