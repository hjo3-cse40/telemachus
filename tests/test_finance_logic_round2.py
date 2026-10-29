"""Finance logic fixes, round 2: entries logged ahead, bills paid by typing, savings direction,
income words, card payment notes, card refunds, bad due days, pace, flex total, setup typos.

Synthetic data only: every test builds its own vault in a temp folder.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from telemachus.cockpit.finance_panel import FinanceTuiPanel, compute_runway
from telemachus.finance.cards import CHECKING, SAVINGS, VISA
from telemachus.finance.import_text import explain_bullet_line, preview_bullet_line
from telemachus.finance.models import MonthlyOverview
from telemachus.finance.recurring import RecurringEngine, custom_bill_account, parse_due_day
from telemachus.finance.setup import apply_setup_yaml, setup_yaml_error
from telemachus.finance.store import FinanceStore

TODAY = "2026-10-28"  # next payday 2026-11-06 (biweekly from 2026-10-23)

SETUP = """as_of_date: "2026-09-01"
starting_balances:
  wells_fargo_checking: {checking}
  capital_one_savings: 500.00
  visa_credit_card: {visa}
fixed_bills:
  credit_card:
    due_day: 2
    active: true
{bills}monthly_targets:
  income: 2960.00
  savings_target: 600.00
  groceries_planned: 280.00
  dining_planned: 120.00
  gas_planned: 120.00
paycheck_plan:
  paycheck_amount: 1480.00
  savings_per_paycheck: 200.00
  frequency: "biweekly"
  anchor_date: "2026-10-23"
"""


def bill(key: str, amount: str, due_day: str = "1") -> str:
    return f"  {key}:\n    amount: {amount}\n    due_day: {due_day}\n    active: true\n"


class _Vault(unittest.TestCase):
    def make(self, checking: str = "1000.00", visa: str = "0", bills: str = "") -> FinanceTuiPanel:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        store = FinanceStore(root_dir=self.root)
        (self.root / "setup.yaml").write_text(
            SETUP.format(checking=checking, visa=visa, bills=bills), encoding="utf-8"
        )
        apply_setup_yaml(self.root / "setup.yaml", store=store)
        self.store = store
        return FinanceTuiPanel(store)


class TestEntriesLoggedAhead(_Vault):
    def test_future_card_payment_is_not_subtracted_twice(self):
        p = self.make(visa="300")
        before = p.get_runway(today=TODAY)
        self.assertEqual((before.checking_cents, before.bills_due_cents, before.left_cents), (100000, 30000, 70000))
        p.log_day_bullets("2026-11-01", "pay visa 300")
        after = p.get_runway(today=TODAY)
        self.assertEqual((after.checking_cents, after.bills_due_cents, after.left_cents), (100000, 30000, 70000))

    def test_paycheck_logged_ahead_does_not_count_today(self):
        p = self.make()
        p.log_day_bullets("2026-11-06", "payday + 1480")
        rw = p.get_runway(today=TODAY)
        self.assertEqual((rw.checking_cents, rw.left_cents, rw.per_day_cents), (100000, 100000, 11111))
        # On payday it counts.
        self.assertEqual(p.get_runway(today="2026-11-06").checking_cents, 248000)

    def test_bill_paid_ahead_stays_due_until_that_day(self):
        p = self.make(checking="2000.00", bills=bill("rent", "1200", "1"))
        p.log_day_bullets("2026-11-01", "rent 1200")
        rw = p.get_runway(today=TODAY)
        # Checking has not dropped yet, so the rent is still due: left is unchanged (800).
        self.assertEqual((rw.checking_cents, rw.bills_due_cents, rw.left_cents), (200000, 120000, 80000))

    def test_balances_leave_out_later_entries(self):
        p = self.make()
        p.log_day_bullets("2099-01-01", "payday + 1480\npay visa 50")
        bal = p.get_balances()["account_balances"]
        self.assertEqual(bal[CHECKING], 100000)
        self.assertEqual(bal[VISA], 0)

    def test_month_spent_leaves_out_later_days(self):
        p = self.make()
        p.log_day_bullets("2026-10-27", "lunch 12")
        p.log_day_bullets("2026-10-30", "lunch 20")
        pay = p.get_paycheck_allocation(as_of_date=TODAY)
        rw = compute_runway(TODAY, pay, p.get_daily_ledger())
        self.assertEqual(rw.month_spent_cents, 1200)


class TestBillsPaidByTyping(_Vault):
    def test_rent_typed_marks_the_rent_bill_paid(self):
        p = self.make(checking="2000.00", bills=bill("rent", "1200"))
        self.assertEqual(p.get_runway(today=TODAY).left_cents, 80000)
        p.log_day_bullets(TODAY, "rent 1200")
        rw = p.get_runway(today=TODAY)
        self.assertEqual((rw.checking_cents, rw.bills_due_cents, rw.left_cents), (80000, 0, 80000))
        alloc = p.get_paycheck_allocation(as_of_date=TODAY)
        rent = [b for b in alloc.bills if b["name"] == "Apartment Rent"]
        self.assertEqual(rent[0]["status"], "paid")

    def test_custom_bills_typed_mark_the_bill_paid(self):
        for key, line in (("phone", "phone bill 80"), ("car_insurance", "car insurance 80"),
                          ("netflix", "- Netflix - 80")):
            with self.subTest(key=key):
                p = self.make(checking="2000.00", bills=bill(key, "80"))
                self.assertEqual(p.get_runway(today=TODAY).left_cents, 192000)
                p.log_day_bullets(TODAY, line)
                rw = p.get_runway(today=TODAY)
                self.assertEqual((rw.checking_cents, rw.bills_due_cents, rw.left_cents), (192000, 0, 192000))

    def test_preview_names_the_bill(self):
        p = self.make(bills=bill("phone", "80"))
        out = p.preview_bullet_line("phone bill 80")
        self.assertTrue(out["ok"])
        self.assertEqual(out["category_label"], "Phone")
        self.assertTrue(out["summary_text"].startswith("Phone $80.00"), out["summary_text"])

    def test_rent_parses_as_rent_but_rental_does_not(self):
        self.assertEqual(explain_bullet_line("rent 1200")[0]["category"], "rent")
        self.assertEqual(explain_bullet_line("rental car 50")[0]["category"], "personal")

    def test_bill_name_used_by_setup_and_engine_is_the_same(self):
        self.assertEqual(custom_bill_account("phone"), "Expenses:Phone")
        self.assertEqual(custom_bill_account("Car Insurance"), "Expenses:CarInsurance")
        self.assertEqual(custom_bill_account("car-insurance"), "Expenses:CarInsurance")


class TestSetupCustomBills(_Vault):
    def test_setup_with_a_custom_bill_does_not_crash(self):
        self.make(bills=bill("phone", "80") + bill("car_insurance", "90"))
        self.assertIsNotNone(self.store.get_account("Expenses:Phone"))
        self.assertIsNotNone(self.store.get_account("Expenses:CarInsurance"))
        rule = RecurringEngine(self.store).get_rule("rule_phone")
        self.assertEqual((rule.base_amount_cents, rule.due_day_of_month), (8000, 1))

    def test_setup_with_null_or_text_due_day(self):
        self.make(bills=bill("phone", "80", "null") + bill("gym", "25", '"2nd"'))
        eng = RecurringEngine(self.store)
        self.assertEqual(eng.get_rule("rule_phone").due_day_of_month, 1)
        self.assertEqual(eng.get_rule("rule_gym").due_day_of_month, 2)


class TestBadDueDay(_Vault):
    def test_bad_due_day_keeps_the_until_payday_block(self):
        for raw, want_day in (('"2nd"', "2026-11-02"), ("null", "2026-11-01"), ('"soon"', "2026-11-01")):
            with self.subTest(due_day=raw):
                p = self.make(bills=bill("gym", "25", raw))
                rw = p.get_runway(today=TODAY)
                self.assertIsNotNone(rw)
                self.assertIn((want_day, 2500), rw.bill_marks)
                bills = p.get_monthly_bills_breakdown(month="2026-10")["bills"]
                self.assertTrue(any(b["key"] == "gym" for b in bills))

    def test_parse_due_day(self):
        self.assertEqual(parse_due_day("2nd", 1), 2)
        self.assertEqual(parse_due_day(None, 28), 28)
        self.assertEqual(parse_due_day("x", 5), 5)
        self.assertEqual(parse_due_day(45, 1), 31)
        self.assertEqual(parse_due_day(0, 1), 1)
        self.assertEqual(parse_due_day(True, 3), 3)


class TestSavingsDirection(_Vault):
    def test_money_out_of_savings(self):
        for line in ("withdraw savings 50", "savings to checking 50", "transfer from savings 50",
                     "moved 50 from savings to checking", "savings withdrawal 50", "took 50 out of savings"):
            with self.subTest(line=line):
                item, err = explain_bullet_line(line)
                self.assertIsNone(err)
                self.assertEqual((item["kind"], item["from_account"], item["to_account"]),
                                 ("transfer", SAVINGS, CHECKING))
                self.assertIn("Savings withdrawal $50.00", preview_bullet_line(line)["summary_text"])

    def test_money_into_savings_is_unchanged(self):
        for line in ("savings + 500", "- savings - 500", "savings deposit 500", "transfer to savings 500",
                     "checking to savings 500"):
            with self.subTest(line=line):
                item, _ = explain_bullet_line(line)
                self.assertEqual((item["kind"], item["from_account"], item["to_account"]),
                                 ("transfer", CHECKING, SAVINGS))

    def test_spend_from_savings(self):
        item, _ = explain_bullet_line("venmo 20 from savings")
        self.assertEqual((item["kind"], item["from_account"], item["payee"]), ("spend", SAVINGS, "venmo"))

    def test_withdrawal_moves_money_the_right_way(self):
        p = self.make()
        p.log_day_bullets(TODAY, "savings + 100\nsavings to checking 40")
        self.assertEqual(self.store.get_account_balance(CHECKING), 100000 - 10000 + 4000)
        self.assertEqual(self.store.get_account_balance(SAVINGS), 50000 + 10000 - 4000)
        self.assertEqual(self.store.get_monthly_overview("2026-10").savings_actual_cents, 6000)
        # The edit box line for the withdrawal parses back to a withdrawal.
        day = [d for d in p.get_daily_ledger() if d["date"] == TODAY][0]
        out = [i for i in day["items"] if i["account_short"] == "Savings"][0]
        line = p.item_to_bullet(out)
        self.assertEqual(explain_bullet_line(line)[0]["from_account"], SAVINGS)
        # Editing only the amount keeps it a withdrawal.
        p.edit_transaction(out["id"], TODAY, "60")
        self.assertEqual(self.store.get_account_balance(SAVINGS), 50000 + 10000 - 6000)


class TestIncomeWords(unittest.TestCase):
    def kind(self, line):
        item, err = explain_bullet_line(line)
        return item["kind"] if item else err

    def test_money_out_even_with_income_words(self):
        for line in ("- security deposit - 500", "security deposit 500 debit", "deposit for apartment 800",
                     "- ticket deposit - 50", "- bottle deposit - 0.60", "payday loan payment 200",
                     "salary sacrifice 50"):
            with self.subTest(line=line):
                self.assertEqual(self.kind(line), "spend")

    def test_income_still_works(self):
        for line in ("payday + 1480", "payday 1480", "salary 2000", "paycheck 1480", "direct deposit 1480",
                     "deposit 500", "credit union deposit 50", "bonus + 200"):
            with self.subTest(line=line):
                self.assertEqual(self.kind(line), "income")

    def test_payday_with_a_minus_is_an_error(self):
        item, err = explain_bullet_line("- payday - 1480")
        self.assertIsNone(item)
        self.assertIn("payday + 1480", err)


class TestCardPaymentNotes(unittest.TestCase):
    def test_counts_are_notes(self):
        cases = {"pay visa for 2 months 250": 25000, "paid 2 visa bills 500": 50000,
                 "visa 2 payment 300": 30000, "pay card 1 of 2 150": 15000,
                 "pay visa 300 (statement 1,250.00)": 30000, "paid visa 250 for 2 months": 25000}
        for line, want in cases.items():
            with self.subTest(line=line):
                item, err = explain_bullet_line(line)
                self.assertIsNone(err)
                self.assertEqual((item["kind"], item["amount_cents"]), ("card_payment", want))

    def test_two_plain_numbers_is_an_error(self):
        item, err = explain_bullet_line("pay visa 300 and 20")
        self.assertIsNone(item)
        self.assertIn("More than one number", err)


class TestRefunds(unittest.TestCase):
    def test_card_refunds_are_not_charged(self):
        for line in ("visa refund 20", "refund 20 visa", "returned shoes 40 visa", "coffee 5 visa refund",
                     "refund + 20 visa"):
            with self.subTest(line=line):
                item, err = explain_bullet_line(line)
                self.assertIsNone(item)
                self.assertIn("refunds are not supported", err)

    def test_checking_refund_needs_a_plus(self):
        self.assertIsNone(explain_bullet_line("refund 20")[0])
        self.assertEqual(explain_bullet_line("amazon refund + 30")[0]["kind"], "income")


class TestPaceLeavesOutBills(_Vault):
    def test_rent_does_not_raise_the_pace(self):
        p = self.make(checking="1600.00", bills=bill("rent", "1200", "26"))
        p.log_day_bullets("2026-10-26", "rent 1200\ncoffee 5")
        p.log_day_bullets("2026-10-27", "coffee 5")
        p.log_day_bullets("2026-10-28", "coffee 5\nphone 3")
        pay = p.get_paycheck_allocation(as_of_date=TODAY)
        rw = compute_runway(TODAY, pay, p.get_daily_ledger())
        self.assertEqual(rw.pace_cents, round(1800 / 7))
        self.assertIsNone(rw.run_out_date)
        day = [d for d in p.get_daily_ledger() if d["date"] == "2026-10-26"][0]
        self.assertEqual((day["outflow_cents"], day["pace_spent_cents"]), (120500, 500))


class TestFlexTotal(unittest.TestCase):
    def test_flex_equals_the_budget_rows(self):
        ov = MonthlyOverview(month="2026-10", groceries_planned_cents=28000, gas_planned_cents=12000,
                             gas_actual_cents=3000, dining_planned_cents=12000)
        self.assertEqual(ov.remaining_cushion_cents, 28000 + 9000 + 12000)

    def test_dining_with_no_plan_counts_as_over(self):
        ov = MonthlyOverview(month="2026-10", dining_actual_cents=1200)
        self.assertEqual(ov.remaining_cushion_cents, -1200)


class TestSetupTypo(_Vault):
    def test_broken_setup_yaml_hides_left_and_says_why(self):
        p = self.make(bills=bill("gym", "25"))
        self.assertIsNotNone(p.get_runway(today=TODAY))
        self.assertIsNone(p.get_setup_error())
        with open(self.root / "setup.yaml", "a", encoding="utf-8") as f:
            f.write("extra: [1,2\n")
        err = setup_yaml_error(self.root / "setup.yaml")
        self.assertTrue(err.startswith("setup.yaml line "), err)
        p.refresh(force=True)
        self.assertEqual(p.get_setup_error(), err)
        self.assertIsNone(p.get_runway())
        self.assertIsNone(p.get_runway(today=TODAY))

    def test_setup_that_is_not_settings(self):
        p = self.make()
        (self.root / "setup.yaml").write_text("- just\n- a list\n", encoding="utf-8")
        self.assertIn("key: value", setup_yaml_error(self.root / "setup.yaml"))


if __name__ == "__main__":
    unittest.main()
