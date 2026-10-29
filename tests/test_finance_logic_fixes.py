"""Finance logic fixes: bullet amounts, card order and dates, runway, bills, goals, defaults.

Synthetic data only: every test builds its own vault in a temp folder.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from telemachus.cockpit.finance_panel import FinanceTuiPanel, compute_runway
from telemachus.finance.cards import CHECKING, VISA, card_due_day
from telemachus.finance.import_text import explain_bullet_line, preview_bullet_line
from telemachus.finance.setup import apply_setup_yaml
from telemachus.finance.store import FinanceStore

SETUP = """as_of_date: "2026-09-01"
starting_balances:
  wells_fargo_checking: {checking}
  capital_one_savings: 500.00
  visa_credit_card: 0.00
fixed_bills:
{card}  gym:
    amount: 25.00
    due_day: 1
    active: true
monthly_targets:
  income: 2960.00
  savings_target: 600.00
  groceries_planned: 280.00
  dining_planned: 120.00
  gas_planned: 120.00
  pge_estimate: 60.00
paycheck_plan:
  paycheck_amount: 1480.00
  savings_per_paycheck: 200.00
  frequency: "biweekly"
  anchor_date: "2026-10-23"
"""


def card_block(due_day: int, amount: str = "0", active: str = "true") -> str:
    return f"  credit_card:\n    amount: {amount}\n    due_day: {due_day}\n    active: {active}\n"


class _Vault(unittest.TestCase):
    def make(self, card: str = "", checking: str = "1000.00", setup: bool = True) -> FinanceTuiPanel:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = FinanceStore(root_dir=root)
        if setup:
            (root / "setup.yaml").write_text(SETUP.format(checking=checking, card=card), encoding="utf-8")
            apply_setup_yaml(root / "setup.yaml", store=store)
        self.store = store
        return FinanceTuiPanel(store)


class TestBulletAmounts(unittest.TestCase):
    def cents(self, line: str) -> int:
        item, err = explain_bullet_line(line)
        self.assertIsNone(err, f"{line!r}: {err}")
        return item["amount_cents"]

    def test_amounts_are_read_as_written(self):
        cases = {
            "gum .99": 99, "- gum - .99": 99, "gum $.99": 99,
            "pay visa .50": 50, "payday + .50": 50,
            "lunch 12.5 with sam": 1250, "coffee 5.5 at starbucks visa": 550, "Taxi 12.5$": 1250,
            "pay visa 300 (2nd payment)": 30000, "visa payment 300 (oct 2)": 30000,
            "pay visa 300 on 10/24": 30000, "paid visa 250 for 2 months": 25000,
            "lunch 13 on 10/24": 1300, "$1,200.50": 120050, "99 ranch 40": 4000, "- 7-eleven - 5": 500,
            "groceries-25": 2500, "payday + 1,480.00": 148000,
        }
        for line, want in cases.items():
            self.assertEqual(self.cents(line), want, line)

    def test_payee_keeps_the_words_around_the_amount(self):
        self.assertEqual(explain_bullet_line("lunch 12.5 with sam")[0]["payee"], "lunch with sam")
        self.assertEqual(explain_bullet_line("coffee 5.5 at starbucks visa")[0]["payee"], "coffee at starbucks")

    def test_unclear_lines_need_a_fix_instead_of_saving_a_wrong_amount(self):
        for line in ("groceries 25.555", "tip 0.001", "rent 1 200.50", "amazon 12,5", "- gas - 12,5",
                     "coffee 10 x2", "lunch 1e3", "lunch 13.15.2", "groceries 25.5o",
                     "- dinner - 30 split 15", "visa payment 1 200"):
            item, err = explain_bullet_line(line)
            self.assertIsNone(item, line)
            self.assertTrue(err, line)
            self.assertFalse(preview_bullet_line(line)["ok"], line)

    def test_zero_is_rejected_for_every_kind(self):
        for line in ("groceries 0", "payday + 0", "savings + 0", "rent -0", "pay visa 0"):
            item, err = explain_bullet_line(line)
            self.assertIsNone(item, line)
            self.assertEqual(err, "Amount must be more than $0.", line)

    def test_gift_and_transit_cards_are_bought_from_checking(self):
        for line in ("Visa gift card 25", "visa giftcard 25", "prepaid visa card 25", "bus card 20",
                     "starbucks card reload 25"):
            item, _ = explain_bullet_line(line)
            self.assertEqual((item["kind"], item["account_short"]), ("spend", "Checking"), line)
        item, _ = explain_bullet_line("gift card 25 on visa")
        self.assertEqual(item["account_short"], "Visa")

    def test_purchase_paid_with_the_card_is_not_a_card_payment(self):
        item, _ = explain_bullet_line("- groceries 25 paid visa")
        self.assertEqual((item["kind"], item["account_short"], item["payee"]), ("spend", "Visa", "groceries"))
        for line in ("paid visa 300", "i paid visa 300", "visa: paid off 300"):
            self.assertEqual(explain_bullet_line(line)[0]["kind"], "card_payment", line)


class TestCardOrderAndDates(_Vault):
    def test_charge_after_a_payment_in_the_same_batch_counts_since_the_payment(self):
        p = self.make(card=card_block(2))
        p.log_day_bullets("2026-10-20", "lunch 30 visa")
        p.log_day_bullets("2026-10-25", "pay visa 30\ncoffee 6 visa")
        cs = p.get_card_summary("2026-10-28")
        self.assertEqual((cs["owed_cents"], cs["charged_since_last_payment_cents"]), (600, 600))

    def test_two_saves_in_the_same_second_keep_their_order(self):
        p = self.make(card=card_block(2))
        p.log_day_bullets("2026-10-25", "pay visa 30")
        p.log_day_bullets("2026-10-25", "coffee 6 visa")
        self.assertEqual(p.get_card_summary("2026-10-28")["charged_since_last_payment_cents"], 600)

    def test_batch_items_list_in_typed_order(self):
        p = self.make()
        p.log_day_bullets("2026-10-25", "coffee 1\ncoffee 2\ncoffee 3")
        txs = self.store.list_transactions(start_date="2026-10-25", end_date="2026-10-25")
        self.assertEqual([t.postings[0].amount_cents for t in txs if t.source != "setup"][:3], [-100, -200, -300])

    def test_card_summary_ignores_entries_dated_later(self):
        p = self.make(card=card_block(2))
        p.log_day_bullets("2026-09-30", "dinner 100 visa\npay visa 40")
        p.log_day_bullets("2026-10-01", "pay visa 60")
        self.assertEqual(p.get_card_summary("2026-09-30")["owed_cents"], 6000)
        self.assertEqual(p.get_card_summary("2026-10-01")["owed_cents"], 0)


class TestInactiveCard(_Vault):
    def test_switched_off_card_is_never_due(self):
        p = self.make(card=card_block(20, active="false"))
        p.log_day_bullets("2026-10-20", "lunch 50 visa")
        pa = p.get_paycheck_allocation(as_of_date="2026-10-28")
        self.assertFalse([b for b in pa.bills if b.get("account_id") == VISA])
        self.assertIsNone(p.get_card_summary("2026-10-28")["due_day"])
        self.assertIsNone(card_due_day({"fixed_bills": {"visa": {"due_day": 9, "active": False}}}))
        self.assertEqual(card_due_day({}), 2)


class TestRunwayAndBills(_Vault):
    def test_bills_due_today_count_toward_the_run_out_date(self):
        p = self.make(card=card_block(28), checking="600.00")
        p.log_day_bullets("2026-10-20", "lunch 440 visa")
        pa = p.get_paycheck_allocation(as_of_date="2026-10-28")
        rw = compute_runway("2026-10-28", pa, [{"date": "2026-10-27", "outflow_cents": 14000}])
        # Card (440.00) and PG&E (60.00) are due today: 100.00 left, gone at $20 a day by Nov 1
        self.assertEqual(rw.bill_marks[0], ("2026-10-28", 50000))
        self.assertEqual(rw.run_out_date, "2026-11-01")
        # Today's bills alone exceed checking: run out today
        rw2 = compute_runway("2026-10-28", dataclasses.replace(pa, checking_balance_cents=1000), [])
        self.assertEqual(rw2.run_out_date, "2026-10-28")

    def test_card_bill_amount_is_not_a_fixed_bill(self):
        p = self.make(card=card_block(20, amount="300.00"))
        overview = self.store.get_monthly_overview("2026-10")
        breakdown = p.get_monthly_bills_breakdown("2026-10")
        self.assertEqual(overview.fixed_bills_cents, 2500)
        self.assertEqual(breakdown["fixed_bills_total_cents"], 2500)

    def test_daily_allowance_is_the_period_allowance_over_the_whole_period(self):
        p = self.make()
        pa = p.get_paycheck_allocation(as_of_date="2026-10-28")
        rw = compute_runway("2026-10-28", pa, [])
        self.assertEqual(rw.allowance_cents, 26000)
        self.assertEqual(rw.period_days, 14)
        self.assertEqual(rw.daily_allowance_cents, 1857)  # 260.00 / 14, not 260.00 / 9 days left
        # Same value the day before payday
        rw_late = compute_runway("2026-11-05", p.get_paycheck_allocation(as_of_date="2026-11-05"), [])
        self.assertEqual(rw_late.daily_allowance_cents, 1857)

    def test_days_split_allowance_spending_from_bills(self):
        p = self.make()
        today = datetime.now().strftime("%Y-%m-%d")
        p.log_day_bullets(today, "spotify 11.99\ndinner 25.81")
        day = next(d for d in p.get_daily_ledger() if d["date"] == today)
        self.assertEqual(day["spent_cents"], 3780)
        self.assertEqual(day["allowance_spent_cents"], 2581)
        self.assertEqual({it["payee"]: it["is_allowance"] for it in day["items"]},
                         {"spotify": False, "dinner": True})

    def test_month_totals_do_not_stop_at_300_entries(self):
        p = self.make(checking="50000")
        today = datetime.now().date()
        first = today.replace(day=1)
        n_days = (today - first).days + 1
        per_day = max(1, 320 // n_days + 1)
        items = []
        for k in range(n_days):
            d = (first + timedelta(days=k)).isoformat()
            items += [{"category": "dining", "payee": "coffee", "amount": 5.0, "date": d} for _ in range(per_day)]
        self.store.log_batch_transactions(items, allow_duplicates=True)
        p.refresh(force=True)
        want = per_day * n_days * 500
        month = today.strftime("%Y-%m")
        got = sum(d["spent_cents"] for d in p.get_daily_ledger() if d["date"].startswith(month))
        self.assertEqual(got, want)
        self.assertEqual(p.get_runway().month_spent_cents, want)


class TestGoalsAndDefaults(_Vault):
    def test_savings_goal_counts_transfers_this_month_not_the_balance(self):
        p = self.make()
        goal = next(g for g in self.store.get_goals_progress("2026-10") if "Savings" in g.name)
        self.assertEqual((goal.saved_cents, goal.target_cents, goal.status), (0, 60000, "open"))
        p.log_day_bullets("2026-10-23", "savings + 200")
        goal = next(g for g in self.store.get_goals_progress("2026-10") if "Savings" in g.name)
        self.assertEqual(goal.saved_cents, 20000)
        self.assertEqual(self.store.get_monthly_overview("2026-10").savings_actual_cents, 20000)

    def test_inactive_brother_bill_has_no_goal(self):
        self.make(card="  kahram:\n    amount: 150.00\n    due_day: 1\n    active: false\n")
        names = [g.name for g in self.store.get_goals_progress("2026-10")]
        self.assertFalse([n for n in names if "Kahram" in n])

    def test_no_setup_means_no_made_up_numbers(self):
        p = self.make(setup=False)
        p.log_day_bullets(datetime.now().strftime("%Y-%m-%d"), "lunch 5")
        p.refresh(force=True)
        self.assertIsNone(p.get_paycheck_allocation())
        self.assertIsNone(p.get_runway())
        self.assertEqual(self.store.get_goals_progress(), [])
        keys = [b["key"] for b in p.get_monthly_bills_breakdown()["bills"]]
        self.assertNotIn("pge", keys)
        self.assertEqual(self.store.get_monthly_overview().fixed_bills_cents, 0)
        self.assertEqual(self.store.get_account_balance(CHECKING), -500)


if __name__ == "__main__":
    unittest.main()
