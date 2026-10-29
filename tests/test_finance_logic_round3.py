"""Finance logic fixes, round 3: card payments written as a move, bill names inside purchases,
card due days like "10th", overdue card balances, "+" lines on the card, savings interest,
budgets counting entries logged ahead, and percent / foreign currency amounts.

Synthetic data only: every test builds its own vault in a temp folder.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest

from telemachus.cockpit.finance_panel import FinanceTuiPanel
from telemachus.finance.cards import (
    CHECKING,
    SAVINGS,
    VISA,
    build_card_summary,
    card_due_day,
    previous_due_date,
)
from telemachus.finance.import_text import explain_bullet_line, preview_bullet_line
from telemachus.finance.setup import apply_setup_yaml
from telemachus.finance.store import FinanceStore

TODAY = "2026-10-28"  # paydays 2026-10-23, 2026-11-06, 2026-11-20 (biweekly)

SETUP = """as_of_date: "{as_of}"
starting_balances:
  wells_fargo_checking: {checking}
  capital_one_savings: 500.00
  visa_credit_card: {visa}
fixed_bills:
  visa:
    amount: 0
    due_day: {due}
    active: true
{bills}monthly_targets:
  groceries_planned: 280.00
  dining_planned: 120.00
  gas_planned: 120.00
paycheck_plan:
  paycheck_amount: 1480.00
  savings_per_paycheck: 0
  frequency: "biweekly"
  anchor_date: "2026-10-23"
"""


class _Vault(unittest.TestCase):
    def make(self, checking="1000.00", visa="0", due="2", bills="", as_of="2026-10-20") -> FinanceTuiPanel:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = FinanceStore(root_dir=root)
        (root / "setup.yaml").write_text(
            SETUP.format(checking=checking, visa=visa, due=due, bills=bills, as_of=as_of), encoding="utf-8"
        )
        apply_setup_yaml(root / "setup.yaml", store=store)
        self.store = store
        return FinanceTuiPanel(store)

    def bal(self, account: str, as_of: str = "2026-12-31") -> int:
        return self.store.get_account_balance(account, as_of_date=as_of, setup_always=True)


def parse(line: str):
    item, err = explain_bullet_line(line)
    return item, err


class TestCardPaymentAsMove(_Vault):
    MOVES = [
        "checking to visa 300", "wf to visa 300", "checking -> visa 300", "visa from checking 300",
        "transfer to visa 300", "transfer 300 to credit card", "xfer to visa 300", "moved 300 to card",
        "sent 300 to visa", "bill pay visa 300", "visa statement 300", "credit card statement 300",
        "pay cards 300", "pay off both cards 300",
    ]

    def test_moves_to_the_card_are_payments(self):
        for line in self.MOVES:
            with self.subTest(line=line):
                item, err = parse(line)
                self.assertIsNone(err)
                self.assertEqual(item["kind"], "card_payment")
                self.assertEqual(item["account_short"], "Checking")
                self.assertEqual(item["to_account"], VISA)
                self.assertEqual(item["amount_cents"], 30000)

    def test_savings_to_the_card_is_paid_from_savings(self):
        item, _ = parse("savings to visa 300")
        self.assertEqual((item["kind"], item["from_account"]), ("card_payment", SAVINGS))

    def test_purchases_and_other_moves_are_unchanged(self):
        cases = {
            "uber to visa office 20": ("spend", "Checking"),
            "sent 20 to mom": ("spend", "Checking"),
            "lunch 13 visa": ("spend", "Visa"),
            "groceries 25 paid visa": ("spend", "Visa"),
            "birthday cards 12": ("spend", "Checking"),
            "playing cards 8 visa": ("spend", "Visa"),
            "savings to checking 100": ("transfer", "Savings"),
            "checking to savings 100": ("transfer", "Checking"),
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                item, err = parse(line)
                self.assertIsNone(err)
                self.assertEqual((item["kind"], item["account_short"]), want)

    def test_logged_move_lowers_owed_and_checking(self):
        p = self.make(visa="300")
        p.log_day_bullets(TODAY, "checking to visa 300")
        self.assertEqual(self.bal(VISA), 0)
        self.assertEqual(self.bal(CHECKING), 70000)
        self.assertEqual(p.get_runway(TODAY).month_spent_cents, 0)


class TestCardPlusLines(unittest.TestCase):
    def test_plus_on_the_card_is_not_a_payment(self):
        for line in ("visa + 20", "- visa + 20", "card + 20", "cc + 20", "visa credit + 15"):
            with self.subTest(line=line):
                item, err = parse(line)
                self.assertIsNone(item)
                self.assertIn("refunds are not supported", err)
                self.assertIn("pay visa", err)
                self.assertNotIn(". ", err)  # one sentence: the Add box shows all of it

    def test_card_words_and_an_amount_stay_a_payment(self):
        for line in ("credit card 300", "- visa - 300", "pay visa + 300"):
            with self.subTest(line=line):
                self.assertEqual(parse(line)[0]["kind"], "card_payment")


class TestSavingsInterest(_Vault):
    def test_interest_is_money_into_savings(self):
        for line in ("hysa interest + 3.50", "savings interest + 3.50", "capital one interest 3.50"):
            with self.subTest(line=line):
                item, err = parse(line)
                self.assertIsNone(err)
                self.assertEqual(item["kind"], "income")
                self.assertEqual(item["account_short"], "Savings")
                self.assertEqual(item["to_account"], SAVINGS)
                self.assertEqual(item["amount_cents"], 350)

    def test_interest_with_a_minus_is_an_error(self):
        item, err = parse("- savings interest - 3")
        self.assertIsNone(item)
        self.assertIn("plus", err)

    def test_logged_interest_leaves_checking_alone(self):
        p = self.make()
        p.log_day_bullets(TODAY, "hysa interest + 3.50")
        self.assertEqual(self.bal(CHECKING), 100000)
        self.assertEqual(self.bal(SAVINGS), 50350)
        self.assertIn("to Savings", preview_bullet_line("hysa interest + 3.50")["summary_text"])

    def test_interest_round_trips_through_edit(self):
        p = self.make()
        p.log_day_bullets(TODAY, "hysa interest + 3.50")
        p.refresh(force=True)
        item = p.get_daily_ledger()[0]["items"][0]
        line = p.item_to_bullet(item)
        p.edit_transaction(item["id"], TODAY, line.replace("3.50", "4"), account_choice="Savings")
        self.assertEqual(self.bal(CHECKING), 100000)
        self.assertEqual(self.bal(SAVINGS), 50400)
        self.assertEqual(self.store.get_account_balance("Income:Interest"), 400)


class TestNotDollars(unittest.TestCase):
    def test_percent_and_foreign_currency_are_rejected(self):
        for line in ("- tip 15%", "- ¥1500 ramen", "₩15000 kimbap", "€20 lunch", "20€ lunch",
                     "ramen 1500 yen", "pay visa ₩300000"):
            with self.subTest(line=line):
                out = preview_bullet_line(line)
                self.assertFalse(out["ok"])
                self.assertTrue(out["error"])

    def test_a_percent_note_next_to_the_amount_is_fine(self):
        item, err = parse("20% off shoes 40")
        self.assertIsNone(err)
        self.assertEqual(item["amount_cents"], 4000)

    def test_dollar_amounts_still_work(self):
        for line, cents in (("coffee $5.75", 575), ("lunch 12.5$", 1250), ("rent 1,200.50", 120050)):
            with self.subTest(line=line):
                self.assertEqual(parse(line)[0]["amount_cents"], cents)


PHONE = "  phone:\n    amount: 80\n    due_day: 3\n    active: true\n"
CAR = "  car_insurance:\n    name: Car Insurance (Geico)\n    amount: 120\n    due_day: 4\n    active: true\n"


class TestBillNameInsidePurchase(_Vault):
    def test_phone_case_does_not_pay_the_phone_bill(self):
        p = self.make(bills=PHONE)
        before = p.get_runway(TODAY)
        self.assertEqual(before.bill_marks, [("2026-11-03", 8000)])
        p.log_day_bullets(TODAY, "phone case 20 visa")
        after = p.get_runway(TODAY)
        self.assertIn(("2026-11-03", 8000), after.bill_marks)
        self.assertEqual(after.left_cents, before.left_cents - 2000)  # the card now owes $20, due Nov 2

    def test_bill_lines_still_pay_the_bill(self):
        p = self.make(bills=PHONE + CAR)
        for line in ("phone bill 80", "phone 80", "pay phone bill 80", "phone autopay 80", "phone bill oct 80",
                     "car insurance 120", "geico car insurance payment 120"):
            with self.subTest(line=line):
                item, _ = parse(line)
                p.match_bill(item)
                self.assertTrue(item.get("to_account", "").startswith("Expenses:"), line)

    def test_purchases_that_mention_a_bill_stay_purchases(self):
        p = self.make(bills=PHONE + CAR)
        for line in ("phone case 20", "car wash 15", "phone charger 25 visa", "insurance card holder 9"):
            with self.subTest(line=line):
                item, _ = parse(line)
                p.match_bill(item)
                self.assertIsNone(item.get("to_account"))
                self.assertEqual(p.preview_bullet_line(line)["category_label"], "Discretionary")


class TestCardDueDayText(_Vault):
    def test_card_due_day_reads_ordinals(self):
        for raw, day in (("10th", 10), ("5th", 5), (" 10", 10), (10.0, 10), ("x", 2)):
            with self.subTest(raw=raw):
                self.assertEqual(card_due_day({"fixed_bills": {"visa": {"due_day": raw}}}), day)
        self.assertEqual(card_due_day({"fixed_bills": {" Visa ": {"due_day": "7th"}}}), 7)

    def test_summary_and_bills_agree_on_10th(self):
        p = self.make(visa="300", due='"10th"')
        self.assertEqual(p.get_runway(TODAY).bill_marks, [])
        card = p.get_card_summary(TODAY)
        self.assertEqual((card["next_due_date"], card["due_before_payday"]), ("2026-11-10", False))

    def test_summary_and_bills_agree_on_5th(self):
        p = self.make(visa="300", due='"5th"')
        self.assertEqual(p.get_runway(TODAY).bill_marks, [("2026-11-05", 30000)])
        card = p.get_card_summary(TODAY)
        self.assertEqual((card["next_due_date"], card["due_before_payday"]), ("2026-11-05", True))


class TestOverdueCard(_Vault):
    def test_unpaid_balance_stays_in_the_bills_after_the_due_date(self):
        p = self.make(visa="300")
        on_due = p.get_runway("2026-11-02")
        self.assertEqual(on_due.bill_marks, [("2026-11-02", 30000)])
        late = p.get_runway("2026-11-03")
        self.assertEqual(late.bill_marks, [("2026-11-03", 30000)])
        self.assertEqual(late.left_cents, on_due.left_cents)
        card = p.get_card_summary("2026-11-03")
        self.assertEqual((card["next_due_date"], card["due_before_payday"]), ("2026-11-02", True))
        bills = p.get_paycheck_allocation("2026-11-03").bills
        self.assertEqual([b.get("overdue_since") for b in bills if b["account_id"] == VISA], ["2026-11-02"])

    def test_a_payment_clears_it(self):
        p = self.make(visa="300")
        p.log_day_bullets("2026-11-03", "pay visa 300")
        self.assertEqual(p.get_runway("2026-11-03").bill_marks, [])
        card = p.get_card_summary("2026-11-03")
        self.assertEqual((card["next_due_date"], card["due_before_payday"]), ("2026-12-02", False))

    def test_paid_before_the_due_date_is_not_overdue(self):
        p = self.make(visa="300")
        p.log_day_bullets("2026-10-30", "pay visa 300\nlunch 20 visa")
        self.assertEqual(p.get_runway("2026-11-03").bill_marks, [])
        self.assertEqual(p.get_card_summary("2026-11-03")["next_due_date"], "2026-12-02")

    def test_setup_after_the_due_date_is_not_overdue(self):
        # The opening balance is dated after the Nov 2 due date: it may be new charges.
        p = self.make(visa="300", as_of="2026-11-03")
        self.assertEqual(p.get_runway("2026-11-04").bill_marks, [])
        self.assertEqual(p.get_card_summary("2026-11-04")["next_due_date"], "2026-12-02")

    def test_build_card_summary_overdue_shape(self):
        s = build_card_summary([], 30000, date(2026, 11, 3), 2, date(2026, 11, 6),
                               overdue_since=date(2026, 11, 2))
        self.assertEqual((s["next_due_date"], s["due_before_payday"], s["status"]), ("2026-11-02", True, "owed"))
        self.assertEqual(set(s), {"owed_cents", "charged_since_last_payment_cents", "paid_this_month_cents",
                                  "last_payment", "due_day", "next_due_date", "status", "due_before_payday"})

    def test_previous_due_date(self):
        self.assertEqual(previous_due_date(2, date(2026, 11, 3)), date(2026, 11, 2))
        self.assertEqual(previous_due_date(2, date(2026, 11, 2)), date(2026, 10, 2))
        self.assertEqual(previous_due_date(31, date(2026, 3, 1)), date(2026, 2, 28))
        self.assertEqual(previous_due_date(5, date(2026, 1, 3)), date(2025, 12, 5))


class TestBudgetsLoggedAhead(_Vault):
    def test_budgets_skip_entries_dated_later_this_month(self):
        p = self.make()
        p.log_day_bullets("2026-10-26", "groceries 50 visa\ncoffee 5")
        p.log_day_bullets("2026-10-27", "pay visa 50")
        p.log_day_bullets(TODAY, "dinner 30 visa")
        p.log_day_bullets("2026-10-30", "groceries 100 visa")  # logged ahead
        ov = self.store.get_monthly_overview("2026-10", as_of_date=TODAY)
        self.assertEqual(ov.groceries_actual_cents, 5000)
        self.assertEqual(ov.dining_actual_cents, 3500)
        self.assertEqual(ov.groceries_actual_cents + ov.dining_actual_cents, p.get_runway(TODAY).month_spent_cents)
        # Without as_of_date the whole month counts (other callers keep that meaning).
        self.assertEqual(self.store.get_monthly_overview("2026-10").groceries_actual_cents, 15000)


if __name__ == "__main__":
    unittest.main()
