"""Tests for the plain-English Finances tab: runway math, layout lines, and key navigation."""
from __future__ import annotations

import curses
import unittest
from unittest.mock import MagicMock, patch

from telemachus.cockpit.finance_panel import _rounded_div, compute_runway
from telemachus.cockpit.finance_view import (
    FinanceViewData,
    build_day,
    build_math,
    build_overview,
    money,
    runway_strip,
)
from telemachus.cockpit.tui import CockpitTUI

try:
    from tests.cockpit_isolation import isolate_module
except ImportError:  # run from inside tests/
    from cockpit_isolation import isolate_module

# CockpitTUI() opens the finance ledger and memory vault: keep them in a temp dir, never Sam's.
setUpModule, tearDownModule = isolate_module()
from telemachus.finance.models import PaycheckWindowAllocation


def _paycheck(checking: int = 59036) -> PaycheckWindowAllocation:
    """Mirrors the real numbers from Oct 25: $590.36 checking, $265.22 of bills before Nov 5."""
    return PaycheckWindowAllocation(
        paycheck_date="2026-10-25",
        next_paycheck_date="2026-11-05",
        days_in_cycle=11,
        paycheck_amount_cents=124664,
        savings_transfer_cents=60000,
        net_checking_cents=64664,
        bills_due_cents=26522,
        bills=[
            {"name": "Spotify", "due_date": "2026-10-25", "amount_cents": 0, "status": "paid"},
            {"name": "WiFi", "due_date": "2026-10-30", "amount_cents": 2323, "status": "due"},
            {"name": "PG&E", "due_date": "2026-10-30", "amount_cents": 1500, "status": "due", "is_estimate": True},
            {"name": "Kahram", "due_date": "2026-11-01", "amount_cents": 20000, "status": "due"},
            {"name": "Gym", "due_date": "2026-11-01", "amount_cents": 2699, "status": "due"},
        ],
        groceries_allocated_cents=12500,
        dining_allocated_cents=11250,
        gas_allocated_cents=12500,
        variable_needs_cents=36250,
        safe_to_spend_cents=1892,
        checking_balance_cents=checking,
        checking_after_bills_cents=checking - 26522,
    )


# Seven days of spending ending Oct 25, totalling $277.05 (about $39.58 a day).
_DAYS = [
    {"date": "2026-10-25", "outflow_cents": 950, "inflow_cents": 0, "savings_cents": 0},
    {"date": "2026-10-24", "outflow_cents": 7793, "inflow_cents": 0, "savings_cents": 0},
    {"date": "2026-10-23", "outflow_cents": 6903, "inflow_cents": 0, "savings_cents": 60000},
    {"date": "2026-10-22", "outflow_cents": 8475, "inflow_cents": 124664, "savings_cents": 0},
    {"date": "2026-10-21", "outflow_cents": 1205, "inflow_cents": 0, "savings_cents": 0},
    {"date": "2026-10-20", "outflow_cents": 2379, "inflow_cents": 0, "savings_cents": 0},
    {"date": "2026-10-18", "outflow_cents": 99999, "inflow_cents": 0, "savings_cents": 0},  # outside the 7-day window
]


class TestComputeRunway(unittest.TestCase):
    def test_left_until_payday_and_per_day(self):
        rw = compute_runway("2026-10-25", _paycheck(), _DAYS)
        self.assertEqual(rw.left_cents, 32514)
        self.assertEqual(rw.days_left, 11)
        self.assertEqual(rw.per_day_cents, 2956)  # 325.14 / 11 = 29.558, rounded

    def test_pace_only_counts_the_last_seven_days(self):
        rw = compute_runway("2026-10-25", _paycheck(), _DAYS)
        self.assertEqual(rw.pace_cents, 3958)  # 277.05 / 7 = 39.578, rounded

    def test_run_out_date_accounts_for_bills_on_their_due_dates(self):
        rw = compute_runway("2026-10-25", _paycheck(), _DAYS)
        self.assertEqual(rw.run_out_date, "2026-11-03")

    def test_no_run_out_when_pace_is_low(self):
        slow = [{"date": "2026-10-25", "outflow_cents": 700, "inflow_cents": 0, "savings_cents": 0}]
        rw = compute_runway("2026-10-25", _paycheck(), slow)
        self.assertEqual(rw.pace_cents, 100)
        self.assertIsNone(rw.run_out_date)

    def test_allowance_check_matches_the_math_screen(self):
        rw = compute_runway("2026-10-25", _paycheck(), _DAYS)
        self.assertEqual(rw.allowance_cents, 36250)
        self.assertEqual(rw.after_allowances_cents, 32514 - 36250)

    def test_bill_marks_group_by_date_and_skip_paid(self):
        rw = compute_runway("2026-10-25", _paycheck(), _DAYS)
        self.assertEqual(rw.bill_marks, [("2026-10-30", 3823), ("2026-11-01", 22699)])

    def test_month_spent_only_counts_this_month(self):
        rw = compute_runway("2026-10-25", _paycheck(), _DAYS)
        self.assertEqual(rw.month_spent_cents, sum(d["outflow_cents"] for d in _DAYS))

    def test_rounding_is_symmetric_around_zero(self):
        self.assertEqual(_rounded_div(32514, 11), 2956)
        self.assertEqual(_rounded_div(-32514, 11), -2956)
        self.assertEqual(_rounded_div(0, 7), 0)

    def test_negative_left_when_bills_exceed_checking(self):
        rw = compute_runway("2026-10-25", _paycheck(checking=10000), _DAYS)
        self.assertEqual(rw.left_cents, 10000 - 26522)


def _flat(lines) -> str:
    return "\n".join("".join(text for text, _ in line) for line in lines)


def _data(checking: int = 59036, mask: bool = False) -> FinanceViewData:
    pc = _paycheck(checking)
    return FinanceViewData(
        runway=compute_runway("2026-10-25", pc, _DAYS),
        checking_cents=checking,
        savings_cents=330367,
        visa_cents=0,
        net_worth_cents=389403,
        goals=[],
        days=[dict(d, day_title=d["date"], net_cents=0, items=[]) for d in _DAYS],
        overview=None,
        paycheck=pc,
        mask=mask,
    )


class TestLayout(unittest.TestCase):
    """The overview reads as plain sentences (ClearNumber.dc.html): no block digits, no boxes."""

    def test_big_number_is_plain_text_with_days_and_payday(self):
        text = _flat(build_overview(_data(), 112, 36).lines)
        self.assertIn("$325.14   left for 11 days", text)
        self.assertIn("payday Thu Nov 5", text)
        self.assertIn("$29.56 a day after bills", text)
        self.assertNotIn("█", text)  # the old block digits are gone

    def test_status_says_over_when_the_pace_runs_out_before_payday(self):
        text = _flat(build_overview(_data(), 112, 36).lines)
        self.assertIn("lately $39.58 a day", text)
        self.assertIn("over, $0 by Tue Nov 3", text)

    def test_status_says_on_track_when_the_pace_fits(self):
        data = _data()
        # The days must match the runway: the status also checks the days' groceries, dining and
        # gas against the daily allowance (the bar colors), so a $1-a-day runway over days that
        # spent $39.58 a day would be over the allowance.
        data.days = [{"date": "2026-10-25", "outflow_cents": 700, "inflow_cents": 0, "savings_cents": 0}]
        data.runway = compute_runway("2026-10-25", _paycheck(), data.days)
        self.assertIn("● on track", _flat(build_overview(data, 112, 36).lines))

    def test_lines_never_exceed_the_width(self):
        for cols in (60, 79, 90, 112, 130, 160):
            for line in build_overview(_data(), cols, 40).lines:
                self.assertLessEqual(sum(len(t) for t, _ in line), cols, f"cols={cols}")

    def test_runway_strip_is_shown_at_every_width(self):
        for cols in (60, 90, 112, 140):
            text = _flat(build_overview(_data(), cols, 40).lines)
            self.assertIn("◆", text, f"cols={cols}")
            self.assertIn("●", text, f"cols={cols}")

    def test_bills_before_payday_list_due_then_paid_with_full_amounts(self):
        text = _flat(build_overview(_data(), 112, 36).lines)
        self.assertIn("BILLS BEFORE PAYDAY", text)
        self.assertIn("$265.22 due", text)
        self.assertIn("200.00", text)
        self.assertIn("Spotify", text)  # the paid $0 row
        self.assertIn("✓", text)

    def test_short_when_bills_exceed_checking(self):
        text = _flat(build_overview(_data(checking=10000), 112, 36).lines)
        self.assertIn("$165.22 short", text)
        self.assertIn("bills before payday are more than checking", text)
        self.assertNotIn("left for", text)

    def test_masking_hides_every_amount(self):
        text = _flat(build_overview(_data(mask=True), 112, 36).lines)
        for secret in ("325.14", "590.36", "29.56", "39.58", "265.22", "3,303.67"):
            self.assertNotIn(secret, text)
        self.assertIn("•••", text)
        self.assertIn("amounts hidden", text)

    def test_math_screen_shows_the_working(self):
        text = _flat(build_math(_data(), 112, 30).lines)
        self.assertIn("Money ▸ Math", text)
        self.assertIn("Left until payday", text)
        self.assertIn("325.14", text)
        self.assertIn("-37.36", text)
        self.assertIn("allowances add up to more than is left", text)

    def test_money_formats(self):
        self.assertEqual(money(-1500), "-$15.00")
        self.assertEqual(money(123456, signed=True), "+$1,234.56")
        self.assertEqual(money(123456, whole=True), "$1,235")
        self.assertEqual(money(5, mask=True), "$•••")

    def test_empty_day_screen_does_not_crash(self):
        data = _data()
        data.days = []
        self.assertIn("No day selected", _flat(build_day(data, 100, 30).lines))


class TestRunwayStrip(unittest.TestCase):
    def test_strip_marks_today_due_days_and_payday(self):
        lines = runway_strip(_data(), 90, 4)
        self.assertEqual(len(lines), 4)
        track = "".join(t for t, _ in lines[1])
        self.assertEqual(track.count("●"), 1)  # today
        self.assertEqual(track.count("▼"), 2)  # Oct 30 and Nov 1
        self.assertEqual(track.count("◆"), 1)  # payday
        amounts = "".join(t for t, _ in lines[3])
        self.assertIn("-38", amounts)
        self.assertIn("+1,247", amounts)

    def test_fewer_rows_drop_months_then_amounts(self):
        self.assertEqual(len(runway_strip(_data(), 90, 3)), 3)
        two = runway_strip(_data(), 90, 2)
        self.assertEqual(len(two), 2)
        self.assertIn("◆", "".join(t for t, _ in two[0]))

    def test_strip_squeezes_instead_of_disappearing(self):
        for cols in (30, 40, 60):
            lines = runway_strip(_data(), cols, 4)
            self.assertTrue(lines, f"cols={cols}")
            for line in lines:
                self.assertLessEqual(sum(len(t) for t, _ in line), cols)

    def test_masked_strip_hides_amounts(self):
        amounts = "".join(t for t, _ in runway_strip(_data(mask=True), 90, 4)[3])
        self.assertNotIn("38", amounts)
        self.assertIn("•••", amounts)


class TestKeyNavigation(unittest.TestCase):
    @patch("curses.color_pair", return_value=0)
    @patch("curses.curs_set")
    def setUp(self, *_):
        self.stdscr = MagicMock()
        self.stdscr.getmaxyx.return_value = (40, 112)
        with patch.object(CockpitTUI, "refresh_data"), patch.object(CockpitTUI, "_init_curses"):
            self.tui = CockpitTUI(self.stdscr)
        self.tui.current_tab = 4
        self.tui.finance_days = [
            {"date": f"2026-10-2{i}", "day_title": "d", "net_cents": 0, "inflow_cents": 0, "outflow_cents": 100,
             "savings_cents": 0,
             "items": [{"id": "a", "payee": "A", "amount_cents": 100}, {"id": "b", "payee": "B", "amount_cents": 200}]}
            for i in range(5, 2, -1)
        ]

    def key(self, ch):
        self.tui.handle_finance_key(ch, 40)

    def test_enter_opens_day_and_escape_returns(self):
        self.key(10)
        self.assertEqual(self.tui.finances_view_mode, "day")
        self.assertEqual(self.tui.finances_focus, "items")
        self.key(27)
        self.assertEqual(self.tui.finances_view_mode, "daily")
        self.assertEqual(self.tui.finances_focus, "days")

    def test_i_opens_the_math_screen_and_escape_leaves_it(self):
        # i used to toggle (open and back); only esc goes back now.
        self.key(ord("i"))
        self.assertEqual(self.tui.finances_view_mode, "math")
        self.key(ord("i"))
        self.assertEqual(self.tui.finances_view_mode, "math")
        self.key(27)
        self.assertEqual(self.tui.finances_view_mode, "daily")

    def test_v_toggles_bills_and_escape_leaves_it(self):
        self.key(ord("v"))
        self.assertEqual(self.tui.finances_view_mode, "bills")
        self.key(27)
        self.assertEqual(self.tui.finances_view_mode, "daily")

    def test_j_and_k_move_days_in_overview_and_items_in_day(self):
        self.key(ord("j"))
        self.assertEqual(self.tui.selected_day_idx, 1)
        self.key(ord("k"))
        self.assertEqual(self.tui.selected_day_idx, 0)
        self.key(10)
        self.key(ord("j"))
        self.assertEqual(self.tui.selected_item_idx, 1)
        self.key(ord("j"))
        self.assertEqual(self.tui.selected_item_idx, 1)  # clamped to the last item

    def test_d_in_overview_never_deletes_a_day(self):
        self.tui.finance_panel = MagicMock()
        self.key(ord("d"))
        self.tui.finance_panel.delete_day.assert_not_called()
        self.tui.finance_panel.delete_transaction.assert_not_called()
        self.assertIn("open the day", self.tui.notification)

    def test_d_in_day_screen_deletes_the_selected_item(self):
        self.tui.finance_panel = MagicMock()
        self.tui.finance_panel.delete_transaction.return_value = True
        self.key(10)
        self.key(ord("j"))
        with patch.object(CockpitTUI, "refresh_data"):
            self.key(ord("d"))  # first press only asks (it used to delete at once, with no undo)
            self.tui.finance_panel.delete_transaction.assert_not_called()
            self.assertIn("Press d again", self.tui.notification)
            self.key(ord("d"))
        self.tui.finance_panel.delete_transaction.assert_called_once_with("b")

    def test_mask_toggle(self):
        self.key(ord("m"))
        self.assertTrue(self.tui.mask_amounts)

    def test_clicking_a_selected_day_opens_it(self):
        self.tui._finance_day_rows = {10: 0, 11: 1}
        self.tui._handle_finance_click(11)
        self.assertEqual(self.tui.selected_day_idx, 1)
        self.assertEqual(self.tui.finances_view_mode, "daily")
        self.tui._handle_finance_click(11)
        self.assertEqual(self.tui.finances_view_mode, "day")


if __name__ == "__main__":
    unittest.main()
