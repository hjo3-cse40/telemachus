"""Money tab views (finance_view.py, finance_screens.py): every screen at every size, card states,
masked mode, hidden bills, day item kinds and the key map. Pure builders, no curses, no real data."""
from __future__ import annotations

import re
import unittest

from telemachus.cockpit import finance_view as fv
from telemachus.cockpit.finance_panel import compute_runway
from telemachus.cockpit.finance_view import (
    VIEW_PREFS,
    FinanceViewData,
    build_bills,
    build_day,
    build_math,
    build_overview,
    day_spent,
    money_action,
    money_keys,
)
from telemachus.cockpit.theme import line_text, line_width
from telemachus.finance.models import GoalProgress, MonthlyOverview, PaycheckWindowAllocation

TODAY = "2026-10-28"


def _pc() -> PaycheckWindowAllocation:
    return PaycheckWindowAllocation(
        paycheck_date="2026-10-23",
        next_paycheck_date="2026-11-06",
        days_in_cycle=9,
        paycheck_amount_cents=148000,
        savings_transfer_cents=20000,
        net_checking_cents=128000,
        bills_due_cents=8500,
        bills=[
            {"name": "PG&E Electric & Gas", "due_date": "2026-10-28", "amount_cents": 6000, "status": "due", "is_estimate": True},
            {"name": "Gym Membership", "due_date": "2026-11-01", "amount_cents": 2500, "status": "due"},
            {"name": "Kahram payment", "due_date": "2026-11-01", "amount_cents": 0, "status": "due"},
        ],
        groceries_allocated_cents=14000,
        dining_allocated_cents=6000,
        gas_allocated_cents=6000,
        variable_needs_cents=26000,
        safe_to_spend_cents=0,
        checking_balance_cents=428515,
        paid_bills=[{"name": "Spotify", "due_date": "2026-10-25", "amount_cents": 1199, "status": "paid"}],
        cycle_start_date="2026-10-23",
    )


def _item(payee, cents, account="Checking", kind="spend", category="Dining"):
    return {"id": payee + str(cents), "payee": payee, "category": category, "account": account,
            "account_short": account, "kind": kind, "amount_cents": cents, "is_income": kind == "income",
            "is_savings": kind == "transfer", "memo": ""}


def _days():
    days = [
        {"date": "2026-10-28", "items": [_item("dinner", 2240, "Visa"), _item("lunch", 1315), _item("coffee", 557)]},
        {"date": "2026-10-27", "items": [_item("visa payment", 4000, kind="card_payment", category="Credit Card")]},
        {"date": "2026-10-26", "items": [_item("groceries", 5894, category="Groceries")]},
        {"date": "2026-10-23", "items": [_item("payday", 148000, kind="income", category="Income"),
                                         _item("savings", 20000, "Checking", "transfer", "Savings"),
                                         _item("boba", 671)]},
    ]
    for i in range(30):  # older history so tall screens have days to fill with
        items = [_item("lunch", 1000 + 137 * i, "Visa" if i % 2 else "Checking")]
        if 30 - i == 25:
            items.append(_item("payday", 148000, kind="income", category="Income"))
        days.append({"date": f"2026-09-{30 - i:02d}", "items": items})
    for day in days:
        day["outflow_cents"] = sum(it["amount_cents"] for it in day["items"] if it["kind"] in ("spend", "card_payment"))
        day["inflow_cents"] = sum(it["amount_cents"] for it in day["items"] if it["kind"] == "income")
        day["savings_cents"] = sum(it["amount_cents"] for it in day["items"] if it["kind"] == "transfer")
        day["day_title"] = day["date"]
        day["net_cents"] = 0
    return days


def _bills():
    def bill(key, name, day, cents, status="paid", active=True, category="Fixed Bill", variable=False):
        return {"key": key, "name": name, "category": category, "due_day": day, "amount_cents": cents,
                "status": status, "status_text": "✅ Paid" if status == "paid" else "⏳ Due",
                "is_active": active, "notes": "", "is_variable": variable}

    return {
        "bills": [
            bill("rent", "Apartment Rent", 1, 0, "due", active=False),
            bill("gym", "Gym Membership", 1, 2500),
            bill("kahram", "Kahram payment", 1, 0, "due"),
            bill("credit_card", "Credit Card (Visa)", 2, 0, category="Card Balance"),
            bill("spotify", "Spotify", 25, 1199, category="Subscription"),
            bill("pge", "PG&E Electric & Gas", 28, 6000, "due", category="Utility (Variable)", variable=True),
        ],
        "fixed_bills_total_cents": 3699,
        "overview": None,
    }


def _overview():
    return MonthlyOverview(month="2026-10", income_cents=296000, savings_target_cents=60000, fixed_bills_cents=7798,
                           pge_cents=6000, groceries_planned_cents=28000, groceries_actual_cents=40941,
                           dining_planned_cents=12000, dining_actual_cents=64714, gas_planned_cents=12000,
                           gas_actual_cents=8931)


CARDS = {
    "clear": {"owed_cents": 0, "charged_since_last_payment_cents": 0, "paid_this_month_cents": 4000,
              "last_payment": {"date": "2026-10-27", "amount_cents": 4000}, "due_day": 2, "next_due_date": None,
              "status": "clear", "due_before_payday": False},
    "owed": {"owed_cents": 12345, "charged_since_last_payment_cents": 12345, "paid_this_month_cents": 20000,
             "last_payment": {"date": "2026-10-03", "amount_cents": 20000}, "due_day": 2,
             "next_due_date": "2026-11-02", "status": "owed", "due_before_payday": True},
    "credit": {"owed_cents": -1200, "charged_since_last_payment_cents": 0, "paid_this_month_cents": 0,
               "last_payment": None, "due_day": 2, "next_due_date": None, "status": "credit", "due_before_payday": False},
}


def make(card="owed", mask=False, sel=0, item=0, bill=0) -> FinanceViewData:
    pc = _pc()
    days = _days()
    return FinanceViewData(
        runway=compute_runway(TODAY, pc, days), checking_cents=428515, savings_cents=420000, visa_cents=12345,
        net_worth_cents=836170,
        # The store gives the savings goal as this month's moves to savings ($200 on Oct 23).
        goals=[GoalProgress("💰 Savings Target", 60000, 20000, "open"),
               GoalProgress("🏠 Brother (Kahram) Payment", 0, 0, "open")],
        days=days, overview=_overview(), paycheck=pc, mask=mask, selected_day_idx=sel, selected_item_idx=item,
        bills_breakdown=_bills(), selected_bill_idx=bill, card=CARDS.get(card) if card else None,
    )


def flat(rendered) -> str:
    return "\n".join(line_text(line) for line in rendered.lines)


BUILDERS = {"overview": build_overview, "day": build_day, "math": build_math, "bills": build_bills}
KEY_TEXT = {
    "overview": ("UNTIL PAYDAY", "◆", "RECENT DAYS", "Visa"),
    "day": ("Money ▸ Wed Oct 28", "ITEMS", "dinner"),
    "math": ("Money ▸ Math", "THIS PAY PERIOD", "Left until payday"),
    "bills": ("Money ▸ Bills", "RECURRING BILLS", "Gym Membership"),
}


class TestEverySize(unittest.TestCase):
    def setUp(self):
        VIEW_PREFS["show_hidden_bills"] = False

    def test_no_line_exceeds_the_width_and_key_content_is_present(self):
        for term_cols in (60, 80, 95, 120, 160):
            for term_rows in (24, 46):
                cols, rows = term_cols - 1, term_rows - 6  # the shell's body area
                for name, build in BUILDERS.items():
                    for mask in (False, True):
                        r = build(make(mask=mask), cols, rows)
                        where = f"{name} {term_cols}x{term_rows} mask={mask}"
                        self.assertLessEqual(len(r.lines), rows, where)
                        for line in r.lines:
                            self.assertLessEqual(line_width(line), cols, where)
                        text = flat(r)
                        for needle in KEY_TEXT[name]:
                            self.assertIn(needle, text, where)

    def test_tiny_sizes_never_crash_or_overflow(self):
        for cols in (20, 35, 50):
            for rows in (1, 4, 9):
                for build in BUILDERS.values():
                    r = build(make(), cols, rows)
                    self.assertLessEqual(len(r.lines), rows)
                    self.assertTrue(all(line_width(l) <= cols for l in r.lines))

    def test_recent_days_fill_a_taller_screen(self):
        short = build_overview(make(), 94, 18)
        tall = build_overview(make(), 94, 40)
        self.assertGreater(len(tall.day_rows), len(short.day_rows))
        self.assertGreaterEqual(len(short.day_rows), 1)
        self.assertEqual(len(tall.lines), 40)  # no big empty area at the bottom

    def test_wide_overview_uses_two_columns(self):
        # 179, not 139: two columns need a RECENT DAYS column with full bars and notes next to a
        # 64-cell left column; at 139 they would have to shrink the bars (a money_ui finding).
        r = build_overview(make(), 179, 44)
        first = line_text(r.lines[0])
        self.assertIn("UNTIL PAYDAY", first)
        self.assertIn("RECENT DAYS", first)
        self.assertGreater(len(r.day_rows), 30)

    def test_runway_strip_survives_95_columns(self):
        text = flat(build_overview(make(), 94, 40))
        self.assertIn("today → payday", text)
        self.assertIn("-60 PG&E", text)


class TestOverviewFixes(unittest.TestCase):
    def test_inactive_goal_is_hidden_and_savings_goal_reads_right(self):
        text = flat(build_overview(make(), 94, 40))
        self.assertNotIn("Brother", text)
        self.assertNotIn("$4,200 of $600", text)
        self.assertIn("Savings goal", text)
        # A monthly target compares with this month's moves to savings ($200 on Oct 23), not with
        # the $4,200 balance (it used to say "met", which was the bug; the store fix gives the
        # monthly number, the view says "this month").
        self.assertNotIn("met", text.split("GOALS")[1].split("BILLS")[0])
        self.assertIn("33%  $200 of $600 this month", text)

    def test_paid_bill_shows_its_full_amount(self):
        text = flat(build_overview(make(), 94, 40))
        self.assertRegex(text, r"✓ paid +11\.99")

    def test_zero_dollar_due_bill_is_not_listed(self):
        text = flat(build_overview(make(), 94, 40))
        bills_part = text.split("BILLS BEFORE PAYDAY")[1].split("RECENT DAYS")[0]
        self.assertNotIn("Kahram", bills_part)

    def test_card_payment_is_not_spending(self):
        days = _days()
        self.assertEqual(day_spent(days[1]), 0)  # only a Visa payment that day
        self.assertEqual(day_spent(days[0]), 2240 + 1315 + 557)
        self.assertEqual(day_spent(days[3]), 671)  # payday and savings do not count

    def test_recent_rows_tag_visa_and_card_payments(self):
        r = build_overview(make(), 94, 40)
        rows = {re.search(r"[A-Z][a-z]{2} ([A-Z][a-z]{2} \d+)", line_text(r.lines[k])).group(1): line_text(r.lines[k])
                for k in r.day_rows}
        self.assertIn("visa", rows["Oct 28"])
        self.assertIn("⇄ Visa payment", rows["Oct 27"])
        self.assertIn("no spending", rows["Oct 27"])
        self.assertIn("+1,480 pay", rows["Oct 23"])

    def test_day_rows_map_to_the_right_days(self):
        d = make(sel=2)
        r = build_overview(d, 94, 40)
        for line_idx, day_idx in r.day_rows.items():
            self.assertIn(fv.fmt_day(d.days[day_idx]["date"]), line_text(r.lines[line_idx]))
        selected = [k for k, v in r.day_rows.items() if v == 2][0]
        self.assertTrue(line_text(r.lines[selected]).startswith("▸ "))

    def test_selection_scrolls_into_view(self):
        d = make(sel=33)
        r = build_overview(d, 94, 18)
        self.assertIn(33, r.day_rows.values())
        self.assertGreater(r.day_scroll_offset, 0)

    def test_gap_days_show_as_no_spending(self):
        text = flat(build_overview(make(), 94, 40))
        self.assertIn("Sun Oct 25  ·  no spending", text)


class TestCard(unittest.TestCase):
    def card_text(self, card, mask=False):
        return flat(build_overview(make(card=card, mask=mask), 94, 40))

    def test_owed_due_before_payday_is_yellow_and_says_so(self):
        d = make(card="owed")
        r = build_overview(d, 94, 40)
        line = next(l for l in r.lines if line_text(l).startswith("  Visa"))
        text = line_text(line)
        self.assertIn("▲ owed $123.45", text)
        self.assertIn("due Mon Nov 2", text)
        self.assertIn("$200.00 paid this month", text)
        self.assertTrue(any("yellow" in style for t, style in line if "123.45" in t))

    def test_clear_card_is_paid_off(self):
        text = self.card_text("clear")
        self.assertIn("✓ paid off", text)
        self.assertIn("$40.00 paid this month", text)

    def test_credit_card_balance(self):
        self.assertIn("credit $12.00", self.card_text("credit"))

    def test_no_summary_falls_back_to_the_balance(self):
        self.assertIn("$123.45 balance", self.card_text(None))

    def test_masked_card_hides_amounts(self):
        text = self.card_text("owed", mask=True)
        self.assertNotIn("123.45", text)
        self.assertIn("owed $•••", text)

    def test_bills_and_math_mention_the_card(self):
        self.assertIn("Visa payment", flat(build_bills(make(card="owed"), 94, 40)))
        math = flat(build_math(make(card="owed"), 94, 40))
        self.assertIn("VISA", math)
        self.assertRegex(math, r"Owed after the payment on Sat Oct 3 +0\.00")
        self.assertRegex(math, r"\+ Charged since +123\.45")
        self.assertRegex(math, r"= Owed now +123\.45")
        self.assertIn("▲ Due Mon Nov 2, before payday", math)

    def test_math_scrolls_to_the_card_when_short_of_rows(self):
        # Math used to drop the VISA block (and more) on a short pane, with no way to reach it.
        # Now every section stays and the screen scrolls (j/k) under the pinned crumb.
        VIEW_PREFS["math_scroll"] = 0
        try:
            first = flat(build_math(make(card="owed"), 79, 24))
            self.assertIn("below  j/k scroll", first)
            seen = first
            for _ in range(60):
                fv.scroll_math(1)
                seen += flat(build_math(make(card="owed"), 79, 24))
            self.assertIn("Charged since", seen)
            self.assertIn("= Owed now", seen)
            self.assertIn("WHY TWO NUMBERS", seen)
        finally:
            VIEW_PREFS["math_scroll"] = 0

    def test_math_card_states(self):
        self.assertIn("Paid off. Nothing is owed on the Visa.", flat(build_math(make(card="clear"), 94, 40)))
        self.assertIn("Credit $12.00: the next charges use it first.", flat(build_math(make(card="credit"), 94, 40)))
        self.assertNotIn("VISA ", flat(build_math(make(card=None), 94, 40)))


class TestFillAndWide(unittest.TestCase):
    """Taller and wider screens show more useful content instead of empty rows."""

    def setUp(self):
        VIEW_PREFS["show_hidden_bills"] = False

    def blank_tail(self, rendered, rows):
        lines = [line_text(l).strip() for l in rendered.lines] + [""] * (rows - len(rendered.lines))
        tail = 0
        for text in reversed(lines):
            if text:
                break
            tail += 1
        return tail

    def test_wide_bills_fill_both_columns(self):
        r = build_bills(make(), 139, 44)
        text = flat(r)
        self.assertIn("VISA ACTIVITY", text)
        self.assertIn("SPENT BY CATEGORY", text)
        self.assertIn("BILL CALENDAR", text)
        self.assertLessEqual(self.blank_tail(r, 44), 3)

    def test_visa_activity_lists_card_purchases_and_payments(self):
        r = build_bills(make(), 139, 44)
        text = flat(r)
        self.assertIn("dinner", text.split("VISA ACTIVITY")[1])
        self.assertIn("⇄ payment", text)
        self.assertIn("owed $123.45", text)

    def test_wide_math_fills_with_categories_and_pay_periods(self):
        r = build_math(make(), 139, 44)
        text = flat(r)
        self.assertIn("SPENT BY CATEGORY", text)
        self.assertIn("PAY PERIODS", text)
        self.assertIn("Fri Oct 23 → today", text)
        self.assertLessEqual(self.blank_tail(r, 44), 8)

    def test_math_keeps_section_gaps_at_95x46(self):
        lines = [line_text(l) for l in build_math(make(), 94, 40).lines]
        for title in ("VISA", "OCTOBER", "WHY TWO NUMBERS"):
            idx = next(i for i, t in enumerate(lines) if t.startswith("  " + title + " "))
            self.assertEqual(lines[idx - 1].strip(), "", title)

    def test_bill_calendar_marks_the_card_only_in_its_due_month(self):
        cal = [line_text(l) for l in build_bills(make(), 94, 46).lines]
        idx = next(i for i, t in enumerate(cal) if "BILL CALENDAR" in t)
        track = next(t for t in cal[idx + 1: idx + 3] if "┼" in t)
        self.assertNotEqual(track.strip()[3], "▼")  # day 2: the Visa is due Nov 2, not Oct 2

    def test_narrow_overview_uses_compact_lines(self):
        text = flat(build_overview(make(), 59, 18))
        self.assertIn("/day after bills", text)
        self.assertIn("Checking $4,285.15  Savings $4,200.00  Net $8,361.70", text)
        self.assertIn("● on track", text)


class TestFooterKeys(unittest.TestCase):
    def footer(self, mode, width, mask=False):
        from telemachus.cockpit.shell import build_footer

        return line_text(build_footer(money_keys(mode, mask, width), width))

    def test_back_and_view_keys_survive_narrow_footers(self):
        for width in (59, 79):
            self.assertIn("v bills", self.footer("daily", width))
            self.assertIn("a add", self.footer("daily", width))
            self.assertIn("esc back", self.footer("day", width))
            self.assertIn("esc back", self.footer("bills", width))
            self.assertIn("esc back", self.footer("math", width))
            for mode in ("daily", "day", "bills", "math"):
                self.assertLessEqual(len(self.footer(mode, width).rstrip()), width)

    def test_wide_footer_keeps_every_hint(self):
        text = self.footer("daily", 94)
        for hint in ("j/k move", "↵ open day", "a add", "e edit", "v bills", "i math", "m hide $"):
            self.assertIn(hint, text)

    def test_trimmed_hints_stay_in_help(self):
        groups = money_keys("daily", False, 59)
        everything = [h.keys for g in groups for h in g.hints]
        self.assertIn("j/k", everything)
        self.assertIn("i", everything)

    def test_bill_count_follows_the_hidden_toggle(self):
        VIEW_PREFS["show_hidden_bills"] = False
        self.assertEqual(fv.bill_count(_bills()), 4)
        VIEW_PREFS["show_hidden_bills"] = True
        self.assertEqual(fv.bill_count(_bills()), 6)
        VIEW_PREFS["show_hidden_bills"] = False
        self.assertEqual(fv.bill_count(make()), 4)


class TestMasked(unittest.TestCase):
    def test_every_amount_is_hidden_on_every_screen(self):
        amount = re.compile(r"\d[\d,]*\.\d\d|\$\d[\d,]*\.")
        for name, build in BUILDERS.items():
            text = flat(build(make(mask=True), 94, 40))
            self.assertIsNone(amount.search(text), f"{name}: {amount.search(text)}")
            self.assertIn("•••", text)

    def test_rows_are_the_same_masked_at_every_size(self):
        for name, build in BUILDERS.items():
            for cols, rows in ((59, 18), (79, 24), (94, 40), (139, 44)):
                plain, masked = build(make(), cols, rows), build(make(mask=True), cols, rows)
                where = f"{name} {cols}x{rows}"
                self.assertEqual(len(plain.lines), len(masked.lines), where)
                self.assertEqual(plain.day_rows, masked.day_rows, where)
                self.assertEqual(plain.item_rows, masked.item_rows, where)
                self.assertEqual(plain.bill_rows, masked.bill_rows, where)
                for a, b in zip(plain.lines, masked.lines):
                    self.assertEqual(line_text(a).strip() == "", line_text(b).strip() == "", where)

    def test_layout_is_unchanged_when_masked(self):
        plain = build_overview(make(), 94, 40)
        masked = build_overview(make(mask=True), 94, 40)
        self.assertEqual(len(plain.lines), len(masked.lines))
        self.assertEqual(plain.day_rows, masked.day_rows)
        for a, b in zip(plain.lines, masked.lines):
            for title in ("UNTIL PAYDAY", "ACCOUNTS", "GOALS", "BILLS BEFORE PAYDAY", "RECENT DAYS", "Checking"):
                self.assertEqual(title in line_text(a), title in line_text(b), title)


class TestDayScreen(unittest.TestCase):
    def test_items_kinds_and_signs(self):
        text = flat(build_day(make(sel=3), 94, 40))
        # Plain table amounts, like the other Money tables (it used to be '-$6.71').
        self.assertIn("+1,480.00", text)
        self.assertIn("⇄ savings", text)
        self.assertRegex(text, r"savings +-200\.00")
        self.assertRegex(text, r"boba +-6\.71")
        self.assertNotIn("-$", text.split("ITEMS")[1].split("NEARBY")[0])
        self.assertIn("spent $6.71", text)

    def test_card_payment_row_and_visa_account(self):
        text = flat(build_day(make(sel=1), 94, 40))
        self.assertIn("⇄ Visa payment", text)
        self.assertIn("spent $0.00", text)
        text = flat(build_day(make(sel=0), 94, 40))
        self.assertIn("visa", text)
        self.assertIn("$22.40 on the Visa", text)

    def test_item_rows_map_to_items_and_mark_the_selection(self):
        r = build_day(make(sel=0, item=1), 94, 40)
        self.assertEqual(sorted(r.item_rows.values()), [0, 1, 2])
        line = [k for k, v in r.item_rows.items() if v == 1][0]
        self.assertTrue(line_text(r.lines[line]).startswith("▸ "))
        self.assertIn("lunch", line_text(r.lines[line]))

    def test_nearby_days_and_not_yet(self):
        text = flat(build_day(make(sel=0), 94, 40))
        self.assertIn("NEARBY DAYS", text)
        self.assertIn("Thu Oct 29  not yet", text)


class TestBillsScreen(unittest.TestCase):
    def setUp(self):
        VIEW_PREFS["show_hidden_bills"] = False

    def tearDown(self):
        VIEW_PREFS["show_hidden_bills"] = False

    def test_inactive_and_zero_bills_are_hidden_with_a_toggle_line(self):
        text = flat(build_bills(make(), 94, 40))
        table = text.split("THIS MONTH")[0]
        self.assertNotIn("01    Apartment Rent", table)
        self.assertIn("2 inactive or $0 bills hidden: Apartment Rent, Kahram payment", table)
        self.assertIn("h to show", table)
        self.assertNotIn("$0.0 ", table)

    def test_h_shows_hidden_bills(self):
        # The key mapper has no side effect; the TUI flips the preference.
        self.assertEqual(money_action("bills", ord("h")), "toggle_hidden_bills")
        self.assertFalse(VIEW_PREFS["show_hidden_bills"])
        VIEW_PREFS["show_hidden_bills"] = True
        text = flat(build_bills(make(), 94, 40))
        self.assertIn("Apartment Rent", text.split("THIS MONTH")[0].split("DUE")[1])
        self.assertIn("inactive", text)
        self.assertIn("h to hide", text)
        self.assertEqual(money_keys("bills")[0].hints[1].label, "hide inactive")

    def test_status_amounts_and_totals(self):
        text = flat(build_bills(make(card=None), 94, 40))
        self.assertIn("due today", text)
        self.assertIn("✓ paid", text)
        self.assertIn("Utility, estimate", text)
        self.assertIn("$96.99 a month", text)
        self.assertIn("4 bills", text)

    def test_this_month_budgets_and_coming_up(self):
        text = flat(build_bills(make(), 94, 46))
        self.assertIn("Left for spending", text)
        self.assertIn("2,222.02", text)
        # The left / over word sits before the amount, so the amount ends on the screen's amount edge.
        self.assertRegex(text, r"over +129\.41")
        self.assertRegex(text, r"left +30\.69")
        self.assertIn("COMING UP", text)
        self.assertIn("+1,480.00", text)

    def test_bill_rows_map_to_shown_bills(self):
        r = build_bills(make(bill=2), 94, 40)
        self.assertEqual(sorted(r.bill_rows.values()), [0, 1, 2, 3])
        line = [k for k, v in r.bill_rows.items() if v == 2][0]
        self.assertTrue(line_text(r.lines[line]).startswith("▸ "))


class TestKeys(unittest.TestCase):
    def test_v_and_i_only_open_and_esc_goes_back(self):
        # v and i used to mean both open and back; now only esc goes back.
        self.assertEqual(money_action("daily", ord("v")), "bills")
        self.assertEqual(money_action("math", ord("v")), "bills")
        self.assertIsNone(money_action("bills", ord("v")))
        self.assertEqual(money_action("bills", ord("i")), "math")
        self.assertIsNone(money_action("math", ord("i")))
        self.assertEqual(money_action("bills", 27), "back")
        self.assertEqual(money_action("math", 27), "back")
        self.assertIsNone(money_action("daily", 27))

    def test_day_keys(self):
        self.assertEqual(money_action("day", 27), "back")
        self.assertEqual(money_action("day", fv._LEFT), "prev_day")
        self.assertEqual(money_action("day", fv._RIGHT), "next_day")
        self.assertEqual(money_action("day", ord("d")), "delete")

    def test_footers_say_how_to_go_back(self):
        def footer(mode, mask=False):
            return " ".join(f"{h.keys} {h.label}" for g in money_keys(mode, mask) for h in g.hints if h.footer)

        self.assertIn("esc back", footer("bills"))
        self.assertIn("esc back", footer("math"))
        self.assertNotIn("esc/", footer("bills") + footer("math"))
        self.assertIn("esc back", footer("day"))
        self.assertIn("←/→ day", footer("day"))
        self.assertIn("v bills", footer("daily"))
        self.assertIn("m show $", footer("daily", mask=True))


def _with_visa_bill(d: FinanceViewData) -> FinanceViewData:
    """The demo's case: a $432 Visa payment due the day after the gym, before payday."""
    pc = d.paycheck
    pc.bills.append({"name": "Credit Card Payment (Visa)", "due_date": "2026-11-02", "amount_cents": 43200,
                     "status": "due"})
    pc.bills_due_cents += 43200
    d.runway = compute_runway(TODAY, pc, d.days)
    return d


class TestMoneyUiFindings(unittest.TestCase):
    """Fixes from the money_ui review (each test names the finding)."""

    def setUp(self):
        VIEW_PREFS["show_hidden_bills"] = False

    def tearDown(self):
        VIEW_PREFS["show_hidden_bills"] = False

    def test_visa_credit_after_payment_is_not_shown_as_negative_owed(self):
        d = make(card="owed")
        d.card = dict(CARDS["owed"], owed_cents=2000, charged_since_last_payment_cents=7000,
                      last_payment={"date": "2026-10-01", "amount_cents": 5000})
        math = flat(build_math(d, 94, 60))
        self.assertNotIn("Owed after the payment", math)
        self.assertNotIn("-50.00", math)
        self.assertRegex(math, r"Charged since the payment on Thu Oct 1 +70\.00")
        self.assertRegex(math, r"− Credit left by that payment +50\.00")
        self.assertRegex(math, r"= Owed now +20\.00")

    def test_bills_total_adds_up_and_the_card_balance_has_its_own_line(self):
        text = flat(build_bills(make(card="owed"), 94, 40))
        table = text.split("THIS MONTH")[0]
        self.assertIn("$96.99 a month", table)
        # Only the rows in the total are counted: gym, Spotify, PG&E (25.00 + 11.99 + 60.00).
        self.assertRegex(table, r"3 bills  ·  paid 36\.99  ·  due 60\.00 +96\.99")
        self.assertRegex(table, r"\+ Visa balance, due Nov 2, not a fixed bill +123\.45")

    def test_paid_off_card_counts_as_a_zero_bill(self):
        text = flat(build_bills(make(card="clear"), 94, 40)).split("THIS MONTH")[0]
        self.assertIn("4 bills", text)
        self.assertNotIn("Visa balance", text)

    def test_timeline_labels_the_biggest_bill_first(self):
        for cols in (59, 94, 119, 179):
            for mask in (False, True):
                d = _with_visa_bill(make(mask=mask))
                text = flat(build_overview(d, cols, 44))
                self.assertIn(("-••• Visa" if mask else "-432 Visa"), text, f"{cols} mask={mask}")

    def test_timeline_spacing_does_not_change_with_m(self):
        plain = build_overview(_with_visa_bill(make()), 94, 44)
        masked = build_overview(_with_visa_bill(make(mask=True)), 94, 44)
        track = [line_text(l) for l in plain.lines if "today → payday" in line_text(l)]
        track_m = [line_text(l) for l in masked.lines if "today → payday" in line_text(l)]
        self.assertEqual(track, track_m)

    def test_bill_calendar_shows_the_last_day_and_every_payday(self):
        d = make()
        d.days.insert(3, {"date": "2026-10-09", "items": [_item("payday", 148000, kind="income", category="Income")],
                          "outflow_cents": 0, "inflow_cents": 148000, "savings_cents": 0})
        for cols in (94, 119, 179):
            lines = [line_text(l) for l in build_bills(d, cols, 60).lines]
            idx = next(i for i, t in enumerate(lines) if "BILL CALENDAR" in t)
            track = next(t for t in lines[idx + 1:] if "┼" in t)
            labels = lines[lines.index(track) + 1]
            names = lines[lines.index(track) + 2]
            self.assertTrue(labels.rstrip().endswith("31"), f"{cols}: {labels!r}")
            step = (len(track.rstrip()) - 3) // 30  # 31 marks: the first at column 2
            self.assertEqual(track[2 + 8 * step], "◆", f"{cols}: Oct 9 payday")
            self.assertIn("Gym", names)
            self.assertNotIn("gym", names)

    def test_bills_and_day_titles_have_a_blank_row_after_them(self):
        for cols, rows in ((94, 40), (69, 20)):
            lines = [line_text(l) for l in build_bills(make(), cols, rows).lines]
            idx = next(i for i, t in enumerate(lines) if "RECURRING BILLS" in t)
            self.assertEqual(lines[idx + 1].strip(), "", f"bills {cols}x{rows}")
        lines = [line_text(l) for l in build_day(make(), 94, 40).lines]
        for title in ("ITEMS", "NEARBY DAYS"):
            idx = next(i for i, t in enumerate(lines) if t.startswith("  " + title))
            self.assertEqual(lines[idx + 1].strip(), "", title)

    def test_narrow_overview_labels_inline_goals(self):
        lines = [line_text(l) for l in build_overview(make(), 69, 20).lines]
        if not any(t.startswith("  GOALS") for t in lines):
            goal = next(t for t in lines if "Savings" in t and "this month" in t)
            self.assertTrue(goal.startswith("  Goal      Savings"), goal)

    def test_wide_rows_keep_their_notes_and_masking_keeps_the_same_notes(self):
        for cols in (94, 119, 179):
            plain = build_overview(make(), cols, 44)
            masked = build_overview(make(mask=True), cols, 44)
            for k, idx in plain.day_rows.items():
                a, b = line_text(plain.lines[k]), line_text(masked.lines[k])
                for note in ("pay", "visa", "⇄ Visa payment", "to savings"):
                    self.assertEqual(note in a, note in b, f"{cols} {a!r} / {b!r}")
            row = next(line_text(plain.lines[k]) for k, v in plain.day_rows.items() if v == 3)
            self.assertIn("+1,480 pay", row)
            if cols != 119:  # the 56-cell right column has room for the first note only
                self.assertIn("200 to savings", row)

    def test_wide_day_keeps_the_category_column(self):
        text = flat(build_day(make(), 119, 34))
        self.assertIn("CATEGORY", text)

    def test_long_category_does_not_run_into_the_description(self):
        d = make(sel=3)
        d.days[3]["items"][2]["category"] = "Discretionary"
        for cols in (94, 119):
            text = flat(build_day(d, cols, 40))
            self.assertIn("Discretionary  boba", text)

    def test_back_key_stays_in_small_footers(self):
        from telemachus.cockpit.shell import build_footer

        for mode in ("day", "bills", "math"):
            for width in (49, 59):
                self.assertIn("esc back", line_text(build_footer(money_keys(mode, False, width), width)), mode)
        day = line_text(build_footer(money_keys("day", False, 69), 69))
        self.assertIn("j/k move", day)
        self.assertNotIn("d delete", day)

    def test_small_bills_screen_shows_more_bill_rows(self):
        r = build_bills(make(), 49, 9)
        self.assertGreaterEqual(len(r.bill_rows), 2)
        texts = [line_text(l) for l in r.lines]
        self.assertNotEqual(texts[1].strip(), "")  # no blank row after the crumb when rows are short

    def test_h_keeps_the_selected_bill_on_its_row(self):
        from telemachus.cockpit.finance_screens import bill_display

        d = make()
        before = [b["key"] for b in bill_display(d)[0]]
        d.show_hidden_bills = True
        after = [b["key"] for b in bill_display(d)[0]]
        self.assertEqual(after[: len(before)], before)
        self.assertEqual(set(after[len(before):]), {"rent", "kahram"})

    def test_savings_goal_is_met_by_this_months_transfers(self):
        d = make()
        d.goals[0] = GoalProgress("💰 Savings Target", 60000, 60000, "completed")
        text = flat(build_overview(d, 94, 40))
        self.assertIn("met  $600 of $600 this month", text)
        math = flat(build_math(d, 94, 60))
        self.assertIn("Plan to save", math)
        self.assertNotIn("Saved", math)

    def test_recent_bars_do_not_change_while_scrolling(self):
        first = build_overview(make(sel=8), 94, 30)  # Sat Sep 26 at the bottom of the list
        later = build_overview(make(sel=4), 94, 30)  # scrolled back up: Sep 26 at the bottom
        rows_a = {v: line_text(first.lines[k]).lstrip("▸ ") for k, v in first.day_rows.items()}
        rows_b = {v: line_text(later.lines[k]).lstrip("▸ ") for k, v in later.day_rows.items()}
        common = set(rows_a) & set(rows_b)
        self.assertTrue(common)
        for i in common:
            self.assertEqual(rows_a[i].count("▇"), rows_b[i].count("▇"), i)

    def test_recent_title_follows_the_selected_month(self):
        self.assertIn("Oct: $", flat(build_overview(make(sel=0), 94, 18)))
        self.assertIn("Sep: $", flat(build_overview(make(sel=10), 94, 18)))

    def test_coming_up_lists_a_bill_due_today(self):
        text = flat(build_bills(make(), 94, 60))
        coming = text.split("COMING UP")[1]
        self.assertIn("Wed Oct 28   PG&E Electric & Gas", coming)

    def test_bills_before_payday_are_in_date_order_and_skip_zero_paid(self):
        d = make()
        d.paycheck.paid_bills.append({"name": "Credit Card Payment (Visa)", "due_date": "2026-10-24",
                                      "amount_cents": 0, "status": "paid"})
        text = flat(build_overview(d, 94, 40))
        part = text.split("BILLS BEFORE PAYDAY")[1].split("RECENT DAYS")[0]
        self.assertLess(part.index("Spotify"), part.index("PG&E"))
        self.assertNotIn("Credit Card Payment", part)

    def test_legend_without_an_allowance_has_no_leading_gap(self):
        d = make()
        d.runway = None
        lines = [line_text(l) for l in build_overview(d, 94, 40).lines]
        legend = [t for t in lines if "open day" in t]
        self.assertTrue(all(t.startswith("  ↵ open day") for t in legend), legend)


if __name__ == "__main__":
    unittest.main()
