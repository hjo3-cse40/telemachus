"""Credit card (Visa) logic: typed syntax, ledger, spending totals, runway, edit/delete (synthetic data only)."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest

from telemachus.cockpit.finance_panel import FinanceTuiPanel, compute_runway
from telemachus.finance.cards import (
    CHECKING,
    SAVINGS,
    VISA,
    build_card_summary,
    card_due_day,
    classify_transaction,
    next_due_date,
)
from telemachus.finance.import_text import (
    explain_bullet_line,
    import_text_document,
    parse_bullet_line,
    preview_bullet_line,
)
from telemachus.finance.recurring import RecurringEngine
from telemachus.finance.setup import apply_setup_yaml
from telemachus.finance.store import FinanceStore

# Paydays (biweekly from the anchor): 2026-10-06, 2026-10-20, 2026-11-03. Card due day 2.
# TODAY is a payday, so the window runs 10-20 .. 11-02 and contains the card due date (11-02).
TODAY = "2026-10-20"
SETUP_YAML = """
as_of_date: "2026-10-01"
starting_balances:
  wells_fargo_checking: 1000.00
  capital_one_savings: 500.00
  visa_credit_card: 0.00
fixed_bills:
  pge:
    amount: 0.00
    due_day: 28
  credit_card:
    amount: 0.00
    due_day: 2
monthly_targets:
  month: "2026-10"
  income: 2960.00
  savings_target: 400.00
  groceries_planned: 300.00
  dining_planned: 100.00
  gas_planned: 100.00
paycheck_plan:
  paycheck_amount: 1480.00
  savings_per_paycheck: 200.00
  frequency: "biweekly"
  anchor_date: "2026-10-06"
"""


def _kind(line):
    item, err = explain_bullet_line(line)
    if item is None:
        return ("error", err)
    return (item["kind"], item["account_short"])


class TestCardSyntax(unittest.TestCase):
    """Every syntax Sam may type, plus tricky lines."""

    def test_card_purchase_markers(self):
        cases = {
            "- groceries - 25.50 visa": ("groceries", 2550, "groceries"),
            "visa: lunch 13": ("lunch", 1300, "dining"),
            "gas 40 (card)": ("gas", 4000, "gas"),
            "- dinner - 32 on card": ("dinner", 3200, "dining"),
            "cc coffee 5.75": ("coffee", 575, "dining"),
            "lunch 13 @visa": ("lunch", 1300, "dining"),
            "(visa) boba 6.50": ("boba", 650, "personal"),
            "- dinner - 32 with card": ("dinner", 3200, "dining"),
            "lunch 13 paid with card": ("lunch", 1300, "dining"),
            "- groceries - 25.50 VISA": ("groceries", 2550, "groceries"),
            "sushi 22 credit": ("sushi", 2200, "personal"),
            "- dinner - 32 on visa": ("dinner", 3200, "dining"),
            "groceries 30 credit card": ("groceries", 3000, "groceries"),
            "lunch on visa with sam 13": ("lunch with sam", 1300, "dining"),
            "car payment visa 300": ("car payment", 30000, "personal"),
        }
        for line, (payee, cents, cat) in cases.items():
            with self.subTest(line=line):
                item = parse_bullet_line(line)
                self.assertIsNotNone(item)
                self.assertEqual(item["kind"], "spend")
                self.assertEqual(item["account_short"], "Visa")
                self.assertEqual(item["from_account"], VISA)
                self.assertEqual(item["payee"], payee)
                self.assertEqual(item["amount_cents"], cents)
                self.assertEqual(item["category"], cat)

    def test_spend_without_marker_is_checking(self):
        for line in ("- groceries - 25.50", "groceries 25.50", "coffee $5.75", "- dinner - 32"):
            with self.subTest(line=line):
                item = parse_bullet_line(line)
                self.assertEqual((item["kind"], item["account_short"], item["from_account"]), ("spend", "Checking", CHECKING))

    def test_payment_phrases(self):
        cases = {
            "credit card payment 200": 20000,
            "cc payment 150": 15000,
            "visa payment 300": 30000,
            "card payment 75": 7500,
            "pay visa 300": 30000,
            "paid visa 300": 30000,
            "pay off card 120": 12000,
            "paid off visa 410.22": 41022,
            "visa payoff 50": 5000,
            "card payoff 60": 6000,
            "- credit card payment - 250": 25000,
            "made payment to visa 200": 20000,
            "pay the visa $1,200.50": 120050,
            "credit card 300": 30000,
            "- visa bill - 99": 9900,
        }
        for line, cents in cases.items():
            with self.subTest(line=line):
                item = parse_bullet_line(line)
                self.assertIsNotNone(item)
                self.assertEqual(item["kind"], "card_payment")
                self.assertEqual(item["category"], "credit_card_payment")
                self.assertEqual(item["to_account"], VISA)
                self.assertEqual(item["from_account"], CHECKING)
                self.assertEqual(item["amount_cents"], cents)

    def test_payment_from_savings(self):
        item = parse_bullet_line("pay visa from savings 300")
        self.assertEqual((item["kind"], item["from_account"], item["account_short"]), ("card_payment", SAVINGS, "Savings"))

    def test_payment_needs_an_amount(self):
        for line in ("card payoff", "pay off card", "pay visa all", "visa payment"):
            with self.subTest(line=line):
                item, err = explain_bullet_line(line)
                self.assertIsNone(item)
                self.assertIn("amount", err.lower())
                self.assertIsNone(parse_bullet_line(line))

    def test_purchase_markers_never_make_a_payment_and_back(self):
        self.assertEqual(_kind("- dinner - 32 paid with card"), ("spend", "Visa"))
        self.assertEqual(_kind("lunch 13 paid on visa"), ("spend", "Visa"))
        self.assertEqual(_kind("dentist payment on card 80"), ("spend", "Visa"))
        self.assertEqual(_kind("pay rent visa 1200"), ("spend", "Visa"))
        self.assertEqual(_kind("visa payment 300"), ("card_payment", "Checking"))
        self.assertEqual(_kind("credit card payment 300"), ("card_payment", "Checking"))

    def test_tricky_lines(self):
        # "credit union" is a bank, not the card: income to checking.
        item = parse_bullet_line("credit union deposit 50")
        self.assertEqual((item["kind"], item["account_short"], item["amount_cents"]), ("income", "Checking", 5000))
        self.assertEqual(item["payee"], "credit union deposit")
        # Whole words only.
        self.assertEqual(_kind("cardamom 4"), ("spend", "Checking"))
        self.assertEqual(_kind("visalia gas 30"), ("spend", "Checking"))
        self.assertEqual(_kind("accc 5"), ("spend", "Checking"))
        # Things you buy that contain the word "card": checking unless a marker is added.
        self.assertEqual(_kind("vegas card game 20"), ("spend", "Checking"))
        self.assertEqual(parse_bullet_line("vegas card game 20")["payee"], "vegas card game")
        self.assertEqual(parse_bullet_line("vegas card game 20")["category"], "personal")
        self.assertEqual(_kind("gift card 25"), ("spend", "Checking"))
        gift = parse_bullet_line("gift card 25 on visa")
        self.assertEqual((gift["account_short"], gift["payee"]), ("Visa", "gift card"))
        # Travel visa fee, not the card.
        self.assertEqual(_kind("visa fee 160"), ("spend", "Checking"))
        # Debit means checking; the word leaves the name.
        debit = parse_bullet_line("groceries 25 debit card")
        self.assertEqual((debit["account_short"], debit["payee"]), ("Checking", "groceries"))

    def test_errors_for_card_refunds_and_card_savings(self):
        self.assertEqual(_kind("refund + 20 visa")[0], "error")
        self.assertEqual(_kind("savings 500 visa")[0], "error")
        self.assertEqual(_kind("groceries")[0], "error")
        self.assertEqual(explain_bullet_line(""), (None, None))

    def test_income_and_savings_unchanged(self):
        self.assertEqual(_kind("payday + 1480"), ("income", "Checking"))
        self.assertEqual(parse_bullet_line("payday + 1,480.00")["amount_cents"], 148000)
        self.assertEqual(_kind("+ 1350 payday"), ("income", "Checking"))
        sav = parse_bullet_line("savings + 500")
        self.assertEqual((sav["kind"], sav["to_account"], sav["from_account"]), ("transfer", SAVINGS, CHECKING))

    def test_preview_never_writes_and_describes_the_line(self):
        p = preview_bullet_line("- groceries - 25.50 visa")
        self.assertTrue(p["ok"])
        self.assertIsNone(p["error"])
        self.assertEqual(
            (p["payee"], p["amount_cents"], p["category_label"], p["account_short"], p["kind"]),
            ("groceries", 2550, "Groceries", "Visa", "spend"),
        )
        self.assertIn("Visa", p["summary_text"])
        pay = preview_bullet_line("pay visa 300")
        self.assertEqual((pay["kind"], pay["account_short"], pay["category_label"]), ("card_payment", "Checking", "Card payment"))
        self.assertIn("not spending", pay["summary_text"])
        bad = preview_bullet_line("pay off card")
        self.assertFalse(bad["ok"])
        self.assertTrue(bad["error"])
        blank = preview_bullet_line("   ")
        self.assertEqual((blank["ok"], blank["error"], blank["summary_text"]), (False, None, ""))
        for line in ("- groceries - 25.50", "payday + 1480", "savings + 50"):
            self.assertNotIn("—", preview_bullet_line(line)["summary_text"])


class TestCardFlowEndToEnd(unittest.TestCase):
    """Sam's full scenario, checking every number at each step."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "fin"
        root.mkdir()
        (root / "setup.yaml").write_text(SETUP_YAML, encoding="utf-8")
        self.store = FinanceStore(root_dir=root)
        apply_setup_yaml(yaml_path=root / "setup.yaml", store=self.store)
        self.panel = FinanceTuiPanel(store=self.store)
        self.engine = RecurringEngine(self.store)

    # helpers
    def checking(self):
        return self.store.get_account_balance(CHECKING)

    def owed(self):
        return self.store.get_account_balance(VISA)

    def day(self, d):
        self.panel.refresh(force=True)
        return next((x for x in self.panel.get_daily_ledger() if x["date"] == d), None)

    def spent(self, d):
        x = self.day(d)
        return x["outflow_cents"] if x else 0

    def overview(self):
        return self.store.get_monthly_overview("2026-10")

    def cash_outflow(self):
        return self.store.get_cash_flow_summary("2026-10-01", "2026-10-31").outflow_cents

    def runway(self):
        return self.panel.get_runway(today=TODAY)

    def card_bill(self):
        alloc = self.engine.get_paycheck_allocation(as_of_date=TODAY)
        bills = [b for b in alloc.bills if b.get("account_id") == VISA]
        self.assertEqual(len(bills), 1)
        return alloc, bills[0]

    def item(self, d, kind=None, payee=None):
        for it in self.day(d)["items"]:
            if (kind is None or it["kind"] == kind) and (payee is None or it["payee"] == payee):
                return it
        raise AssertionError(f"no item {kind} {payee} on {d}")

    def test_full_scenario(self):
        # 0. Start: checking $1,000, nothing owed.
        self.assertEqual(self.checking(), 100000)
        self.assertEqual(self.owed(), 0)
        rw = self.runway()
        self.assertEqual((rw.checking_cents, rw.bills_due_cents, rw.left_cents), (100000, 0, 100000))
        alloc, bill = self.card_bill()
        self.assertEqual((bill["status"], bill["amount_cents"]), ("paid", 0))
        self.assertEqual(self.panel.get_card_summary(today=TODAY)["status"], "clear")

        # 1. Checking purchase.
        self.panel.log_day_bullets("2026-10-18", "- groceries - 40")
        self.assertEqual(self.checking(), 96000)
        self.assertEqual(self.owed(), 0)
        self.assertEqual(self.spent("2026-10-18"), 4000)
        self.assertEqual(self.overview().groceries_actual_cents, 4000)
        self.assertEqual(self.item("2026-10-18")["account_short"], "Checking")

        # 2. Card purchases: owed goes up, checking does not move, spending counts them.
        self.panel.log_day_bullets("2026-10-19", "- groceries - 25.50 visa\nvisa: lunch 13\ngas 40 (card)")
        self.assertEqual(self.checking(), 96000)
        self.assertEqual(self.owed(), 7850)
        self.assertEqual(self.spent("2026-10-19"), 7850)
        self.assertEqual(self.day("2026-10-19")["card_spend_cents"], 7850)
        ov = self.overview()
        self.assertEqual((ov.groceries_actual_cents, ov.dining_actual_cents, ov.gas_actual_cents), (6550, 1300, 4000))
        self.assertEqual(self.cash_outflow(), 11850)
        for it in self.day("2026-10-19")["items"]:
            self.assertEqual((it["kind"], it["account_short"], it["account"]), ("spend", "Visa", "Visa"))
        # Runway: card due 11-02 is before payday 11-03, so the owed balance is subtracted like a bill.
        rw = self.runway()
        self.assertEqual((rw.checking_cents, rw.bills_due_cents, rw.left_cents), (96000, 7850, 88150))
        self.assertIn(("2026-11-02", 7850), rw.bill_marks)
        alloc, bill = self.card_bill()
        self.assertEqual((bill["status"], bill["amount_cents"]), ("due", 7850))
        card = self.panel.get_card_summary(today=TODAY)
        self.assertEqual(card["owed_cents"], 7850)
        self.assertEqual(card["charged_since_last_payment_cents"], 7850)
        self.assertEqual(card["status"], "owed")
        self.assertTrue(card["due_before_payday"])
        self.assertEqual(card["next_due_date"], "2026-11-02")
        self.assertEqual(card["due_day"], 2)
        self.assertIsNone(card["last_payment"])

        # 3. Partial payment from checking: owed and checking drop, spending does not change.
        self.panel.log_day_bullets(TODAY, "visa payment 50")
        self.assertEqual(self.owed(), 2850)
        self.assertEqual(self.checking(), 91000)
        self.assertEqual(self.spent(TODAY), 0)
        self.assertEqual(self.day(TODAY)["card_payment_cents"], 5000)
        self.assertEqual(self.spent("2026-10-19"), 7850)
        self.assertEqual(self.cash_outflow(), 11850)
        self.assertEqual(self.overview().groceries_actual_cents, 6550)
        pay_item = self.item(TODAY, kind="card_payment")
        self.assertEqual((pay_item["account_short"], pay_item["amount_cents"]), ("Checking", 5000))
        # Not double-subtracted: checking fell by 50 and the bill fell by 50, so "left" is unchanged.
        rw = self.runway()
        self.assertEqual((rw.checking_cents, rw.bills_due_cents, rw.left_cents), (91000, 2850, 88150))
        self.assertEqual(rw.month_spent_cents, 11850)
        card = self.panel.get_card_summary(today=TODAY)
        self.assertEqual(card["last_payment"], {"date": TODAY, "amount_cents": 5000})
        self.assertEqual(card["paid_this_month_cents"], 5000)
        self.assertEqual(card["charged_since_last_payment_cents"], 0)

        # 4. Full payoff.
        self.panel.log_day_bullets(TODAY, "paid off visa 28.50")
        self.assertEqual(self.owed(), 0)
        self.assertEqual(self.checking(), 88150)
        alloc, bill = self.card_bill()
        self.assertEqual((bill["status"], bill["amount_cents"]), ("paid", 0))
        rw = self.runway()
        self.assertEqual((rw.bills_due_cents, rw.left_cents), (0, 88150))
        card = self.panel.get_card_summary(today=TODAY)
        self.assertEqual((card["status"], card["paid_this_month_cents"]), ("clear", 7850))
        self.assertEqual(card["last_payment"], {"date": TODAY, "amount_cents": 7850})
        self.assertFalse(card["due_before_payday"])

        # 5. Overpay: owed goes negative (a credit), the card bill stays $0, never extra cash.
        self.panel.log_day_bullets(TODAY, "pay visa 10")
        self.assertEqual(self.owed(), -1000)
        self.assertEqual(self.checking(), 87150)
        self.assertEqual(self.store.get_all_balances()["net_worth_cents"], 87150 + 50000 + 1000)
        alloc, bill = self.card_bill()
        self.assertEqual((bill["status"], bill["amount_cents"]), ("paid", 0))
        rw = self.runway()
        self.assertEqual((rw.checking_cents, rw.bills_due_cents, rw.left_cents), (87150, 0, 87150))
        self.assertEqual(self.panel.get_card_summary(today=TODAY)["status"], "credit")
        self.assertEqual(self.spent(TODAY), 0)
        self.assertEqual(self.cash_outflow(), 11850)

        # 6. Delete the overpayment: owed and checking come back.
        over = [it for it in self.day(TODAY)["items"] if it["kind"] == "card_payment" and it["amount_cents"] == 1000][0]
        self.assertTrue(self.panel.delete_transaction(over["id"]))
        self.assertEqual((self.owed(), self.checking()), (0, 88150))

        # 7. Delete the partial payment: owed goes back up, the bill is due again, left unchanged.
        partial = [it for it in self.day(TODAY)["items"] if it["kind"] == "card_payment" and it["amount_cents"] == 5000][0]
        self.assertTrue(self.panel.delete_transaction(partial["id"]))
        self.assertEqual((self.owed(), self.checking()), (5000, 93150))
        rw = self.runway()
        self.assertEqual((rw.bills_due_cents, rw.left_cents), (5000, 88150))

        # 8. Edit the checking groceries over to the card, then back.
        groc = self.item("2026-10-18", payee="groceries")
        self.panel.edit_transaction(groc["id"], "2026-10-18", self.panel.item_to_bullet(groc), account_choice="Visa")
        self.assertEqual((self.owed(), self.checking()), (9000, 97150))
        self.assertEqual(self.spent("2026-10-18"), 4000)
        self.assertEqual(self.overview().groceries_actual_cents, 6550)
        moved = self.item("2026-10-18", payee="groceries")
        self.assertEqual((moved["kind"], moved["account_short"]), ("spend", "Visa"))
        self.assertEqual(self.panel.item_to_bullet(moved), "- groceries - 40 visa")
        self.panel.edit_transaction(moved["id"], "2026-10-18", self.panel.item_to_bullet(moved), account_choice="Checking")
        self.assertEqual((self.owed(), self.checking()), (5000, 93150))
        self.assertEqual(self.item("2026-10-18", payee="groceries")["account_short"], "Checking")
        self.assertEqual(self.cash_outflow(), 11850)

        # 9. Editing a payment's amount keeps it a payment (plain text keeps the original kind).
        payoff = self.item(TODAY, kind="card_payment")
        self.panel.edit_transaction(payoff["id"], TODAY, "30", account_choice="Checking")
        self.assertEqual((self.owed(), self.checking()), (4850, 93000))
        self.assertEqual(self.item(TODAY)["kind"], "card_payment")
        self.assertEqual(self.spent(TODAY), 0)
        # The Visa choice on a payment is ignored (the card cannot pay itself).
        payoff = self.item(TODAY, kind="card_payment")
        self.panel.edit_transaction(payoff["id"], TODAY, self.panel.item_to_bullet(payoff), account_choice="Visa")
        self.assertEqual((self.owed(), self.checking()), (4850, 93000))

    def test_savings_transfers_and_payments_are_not_spending(self):
        self.panel.log_day_bullets(TODAY, "savings + 100\nvisa payment 20\n- coffee - 5\npayday + 1480")
        d = self.day(TODAY)
        self.assertEqual(d["outflow_cents"], 500)
        self.assertEqual(d["savings_cents"], 10000)
        self.assertEqual(d["card_payment_cents"], 2000)
        self.assertEqual(d["inflow_cents"], 148000)
        kinds = sorted(it["kind"] for it in d["items"])
        self.assertEqual(kinds, ["card_payment", "income", "spend", "transfer"])
        cf = self.store.get_cash_flow_summary("2026-10-01", "2026-10-31")
        self.assertEqual((cf.outflow_cents, cf.inflow_cents), (500, 148000))
        self.assertEqual(cf.category_breakdown, {"Expenses:Food:Dining": 500})
        rw = self.runway()
        self.assertEqual(rw.month_spent_cents, 500)
        # Paycheck allocation keeps the planned numbers; payments are not bills or allowances.
        alloc = self.engine.get_paycheck_allocation(as_of_date=TODAY)
        self.assertEqual(alloc.checking_balance_cents, self.checking())

    def test_lately_per_day_counts_card_spend_but_not_payments(self):
        self.panel.log_day_bullets("2026-10-16", "- lunch - 14 visa")
        self.panel.log_day_bullets("2026-10-17", "- coffee - 7")
        self.panel.log_day_bullets("2026-10-18", "visa payment 14")
        rw = self.runway()
        self.assertEqual(rw.pace_cents, round(2100 / 7))

    def test_identical_lines_on_one_day_both_count(self):
        self.panel.log_day_bullets(TODAY, "- coffee - 5 visa")
        self.panel.log_day_bullets(TODAY, "- coffee - 5 visa")
        self.assertEqual(self.owed(), 1000)
        self.assertEqual(self.spent(TODAY), 1000)

    def test_import_document_still_idempotent_and_totals_exclude_transfers(self):
        doc = "10/15\n- groceries - 20 visa\nvisa payment 20\nsavings + 50\npayday + 100\n"
        res = import_text_document(doc, store=self.store, default_year=2026)
        self.assertEqual(res["total_outflow_cents"], 2000)
        self.assertEqual(res["total_inflow_cents"], 10000)
        import_text_document(doc, store=self.store, default_year=2026)
        self.assertEqual(self.owed(), 0)
        self.assertEqual(self.checking(), 100000 - 2000 - 5000 + 10000)

    def test_bills_breakdown_and_obligations_use_the_live_balance_once(self):
        self.panel.log_day_bullets("2026-10-19", "- dinner - 60 visa")
        bd = self.engine.get_all_recurring_bills_breakdown(month="2026-10")
        cards = [b for b in bd["bills"] if b["account_id"] == VISA]
        self.assertEqual(len(cards), 1)
        self.assertEqual((cards[0]["amount_cents"], cards[0]["status"], cards[0]["due_day"]), (6000, "due", 2))
        self.assertNotIn(6000, [bd["fixed_bills_total_cents"]])
        # 60 days ahead covers two due dates; the balance must be counted only once.
        obligations = self.engine.get_upcoming_obligations(days_ahead=60)
        card_amounts = [o["amount_cents"] for o in obligations if o["to_account"] == VISA]
        self.assertGreaterEqual(len(card_amounts), 2)
        self.assertEqual(sum(card_amounts), 6000)
        committed = self.engine.get_committed_cash_flow(days_ahead=60)
        self.assertGreaterEqual(committed["committed_cents"], 6000)

    def test_card_bill_not_subtracted_when_due_after_payday(self):
        self.panel.log_day_bullets("2026-10-05", "- dinner - 60 visa")
        # On 10-06 (payday) the window runs to 10-19: no card due date inside.
        rw = self.panel.get_runway(today="2026-10-06")
        self.assertEqual(rw.bills_due_cents, 0)
        card = self.panel.get_card_summary(today="2026-10-06")
        self.assertEqual(card["next_due_date"], "2026-11-02")
        self.assertFalse(card["due_before_payday"])

    def test_cached_card_summary_matches_contract_keys(self):
        self.panel.log_day_bullets("2026-10-19", "- dinner - 60 visa")
        card = self.panel.get_card_summary()
        self.assertEqual(set(card), {"owed_cents", "charged_since_last_payment_cents", "paid_this_month_cents",
                                     "last_payment", "due_day", "next_due_date", "status", "due_before_payday"})
        self.assertEqual(card["owed_cents"], 6000)


class TestEditFlowKinds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FinanceStore(root_dir=Path(self.tmp.name) / "fin")
        self.panel = FinanceTuiPanel(store=self.store)

    def items(self):
        self.panel.refresh(force=True)
        return self.panel.get_daily_ledger()[0]["items"]

    def test_round_trip_every_kind(self):
        self.panel.log_day_bullets(
            TODAY,
            "- groceries - 25.50 visa\n- dinner - 12\nvisa payment 40\npay visa from savings 15\n"
            "savings + 30\npayday + 1480",
        )
        for it in self.items():
            with self.subTest(item=it["payee"], kind=it["kind"]):
                bullet = self.panel.item_to_bullet(it)
                parsed = parse_bullet_line(bullet)
                self.assertEqual(parsed["kind"], it["kind"])
                self.assertEqual(parsed["account_short"], it["account_short"])
                self.assertEqual(parsed["amount_cents"], it["amount_cents"])

    def test_edit_with_unchanged_box_keeps_everything(self):
        self.panel.log_day_bullets(TODAY, "- groceries - 25.50 visa\nvisa payment 40\nsavings + 30\npayday + 1480\n- tea - 3")
        before = {
            "checking": self.store.get_account_balance(CHECKING),
            "visa": self.store.get_account_balance(VISA),
            "savings": self.store.get_account_balance(SAVINGS),
        }
        labels = {"Checking": "Checking", "Visa": "Visa", "Savings": "CapitalOne"}
        for it in list(self.items()):
            self.panel.edit_transaction(it["id"], TODAY, self.panel.item_to_bullet(it), account_choice=labels[it["account"]])
        after = {
            "checking": self.store.get_account_balance(CHECKING),
            "visa": self.store.get_account_balance(VISA),
            "savings": self.store.get_account_balance(SAVINGS),
        }
        self.assertEqual(before, after)
        self.assertEqual(sorted(i["kind"] for i in self.items()), ["card_payment", "income", "spend", "spend", "transfer"])

    def test_income_edit_with_checking_box_does_not_break(self):
        # Old bug: the account choice became the source, so income went Checking -> Checking.
        self.panel.log_day_bullets(TODAY, "payday + 1480")
        it = self.items()[0]
        self.panel.edit_transaction(it["id"], TODAY, "payday + 1500", account_choice="Checking")
        self.assertEqual(self.store.get_account_balance(CHECKING), 150000)
        self.assertEqual(self.items()[0]["kind"], "income")

    def test_typed_card_word_in_edit_moves_to_card_when_box_unchanged(self):
        self.panel.log_day_bullets(TODAY, "- lunch - 13")
        it = self.items()[0]
        self.panel.edit_transaction(it["id"], TODAY, "- lunch - 13 visa", account_choice="Checking")
        self.assertEqual(self.store.get_account_balance(VISA), 1300)
        self.assertEqual(self.store.get_account_balance(CHECKING), 0)

    def test_category_choice_turns_a_payment_into_spending(self):
        self.panel.log_day_bullets(TODAY, "visa payment 40")
        it = self.items()[0]
        self.panel.edit_transaction(it["id"], TODAY, "- printer - 40", account_choice="Checking", category_choice="personal")
        self.assertEqual(self.items()[0]["kind"], "spend")
        self.assertEqual(self.store.get_account_balance(VISA), 0)

    def test_new_text_kind_word_wins(self):
        self.panel.log_day_bullets(TODAY, "- coffee - 5")
        it = self.items()[0]
        self.panel.edit_transaction(it["id"], TODAY, "visa payment 5", account_choice="Checking")
        self.assertEqual(self.items()[0]["kind"], "card_payment")


class TestCardHelpers(unittest.TestCase):
    def test_due_day_and_next_due_date(self):
        self.assertEqual(card_due_day(None), 2)
        self.assertEqual(card_due_day({"fixed_bills": {"visa": {"due_day": 15}}}), 15)
        # Switched off: no due day at all (it used to fall back to the 2nd and subtract the balance).
        self.assertIsNone(card_due_day({"fixed_bills": {"credit_card": {"due_day": 9, "active": False}}}))
        self.assertEqual(next_due_date(2, date(2026, 10, 20)), date(2026, 11, 2))
        self.assertEqual(next_due_date(2, date(2026, 11, 2)), date(2026, 11, 2))
        self.assertEqual(next_due_date(31, date(2026, 2, 10)), date(2026, 2, 28))
        self.assertEqual(next_due_date(2, date(2026, 12, 20)), date(2027, 1, 2))

    def test_summary_empty(self):
        s = build_card_summary([], 0, date(2026, 10, 20), 2, date(2026, 11, 3))
        self.assertEqual(s["status"], "clear")
        self.assertIsNone(s["last_payment"])
        self.assertFalse(s["due_before_payday"])

    def test_classify_opening_balance(self):
        from telemachus.finance.models import Posting, Transaction

        tx = Transaction.create("2026-10-01", "Opening Balance", [Posting(VISA, -500), Posting("Equity:OpeningBalances", 500)])
        self.assertEqual(classify_transaction(tx)["kind"], "opening")

    def test_compute_runway_uses_spend_only(self):
        from telemachus.finance.models import PaycheckWindowAllocation

        alloc = PaycheckWindowAllocation(
            paycheck_date=TODAY, next_paycheck_date="2026-11-03", days_in_cycle=14,
            paycheck_amount_cents=0, savings_transfer_cents=0, net_checking_cents=0, bills_due_cents=0,
            bills=[], groceries_allocated_cents=0, dining_allocated_cents=0, gas_allocated_cents=0,
            variable_needs_cents=0, safe_to_spend_cents=0,
            checking_balance_cents=10000, checking_after_bills_cents=10000,
        )
        days = [{"date": TODAY, "outflow_cents": 700, "card_payment_cents": 5000, "savings_cents": 900}]
        rw = compute_runway(TODAY, alloc, days)
        self.assertEqual(rw.pace_cents, 100)
        self.assertEqual(rw.month_spent_cents, 700)


if __name__ == "__main__":
    unittest.main()
