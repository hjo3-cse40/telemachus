"""Tests for the plain-English Finances tab: runway math, layout lines, and key navigation."""
from __future__ import annotations

import curses
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from telemachus.cockpit.finance_panel import _rounded_div, compute_runway
from telemachus.cockpit.finance_view import (
    FinanceViewData,
    big_digits,
    build_cycle_detail,
    build_day,
    build_math,
    build_overview,
    money,
    runway_track,
)
from telemachus.cockpit.tui import CockpitTUI
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
    def test_hero_reads_as_a_sentence(self):
        text = _flat(build_overview(_data(), 112, 36).lines)
        self.assertIn("You have", text)
        self.assertIn("to last 11 days, until payday on Thu, Nov 5.", text)
        self.assertIn("$29.56 a day", text)
        self.assertIn("Lately $39.58/day, which hits $0 by Tue Nov 3", text)

    def test_every_line_fits_the_requested_width(self):
        for cols in (80, 90, 100, 112, 140):
            for line in build_overview(_data(), cols, 40).lines:
                self.assertLessEqual(sum(len(t) for t, _ in line), cols, f"cols={cols}")

    def test_wide_layout_has_runway_and_narrow_does_not(self):
        self.assertIn("RUNWAY", _flat(build_overview(_data(), 112, 36).lines))
        self.assertNotIn("RUNWAY", _flat(build_overview(_data(), 90, 36).lines))

    def test_paid_bills_move_below_the_due_list(self):
        text = _flat(build_overview(_data(), 112, 36).lines)
        self.assertIn("Spotify", text)
        self.assertIn("paid", text)
        self.assertIn("$265.22 due", text)

    def test_negative_left_switches_the_sentence(self):
        text = _flat(build_overview(_data(checking=10000), 112, 36).lines)
        self.assertIn("Bills before payday are more than checking by", text)
        self.assertNotIn("You have", text)

    def test_masking_hides_every_amount(self):
        text = _flat(build_overview(_data(mask=True), 112, 36).lines)
        for secret in ("590.36", "325.14", "265.22", "3,303.67"):
            self.assertNotIn(secret, text)
        self.assertIn("***.**", text)

    def test_math_screen_explains_both_cushion_numbers(self):
        text = _flat(build_math(_data(), 112, 30).lines)
        self.assertIn("Left until payday", text)
        self.assertIn("325.14", text)
        self.assertIn("-37.36", text)
        self.assertIn("allowances add up to more than is left", text)

    def test_block_digits_are_three_rows_and_equal_width(self):
        rows = big_digits("325.14")
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({len(r) for r in rows}), 1)

    def test_money_formats(self):
        self.assertEqual(money(-1500), "-$15.00")
        self.assertEqual(money(123456, signed=True), "+$1,234.56")
        self.assertEqual(money(123456, whole=True), "$1,235")
        self.assertEqual(money(5, mask=True), "$***.**")

    def test_empty_day_screen_does_not_crash(self):
        data = _data()
        data.days = []
        self.assertIn("No day selected", _flat(build_day(data, 100, 30).lines))


def _cycle_data(width_mask: bool = False) -> FinanceViewData:
    pc = _paycheck()
    pc.cycle_start_date = "2026-10-22"
    pc.paid_bills = [{"name": "Spotify", "due_date": "2026-10-25", "amount_cents": 950, "status": "paid"}]
    pc.bills = [dict(b) for b in pc.bills if b["name"] != "Spotify"]
    data = _data(mask=width_mask)
    data.paycheck = pc
    data.runway = compute_runway("2026-10-26", pc, _DAYS)
    return data


class TestCycleDetail(unittest.TestCase):
    def test_title_shows_the_whole_date_range(self):
        text = _flat(build_cycle_detail(_cycle_data(), 100, 30))
        self.assertIn("Bills Due Before Paycheck (Thu Oct 22 → Thu Nov 5)", text)

    def test_each_day_lists_the_bills_that_add_up_to_its_total(self):
        text = _flat(build_cycle_detail(_cycle_data(), 100, 30))
        self.assertIn("$38.23", text)
        self.assertIn("WiFi $23.23 + PG&E $15.00", text)
        self.assertIn("$226.99", text)
        self.assertIn("Kahram $200.00 + Gym $26.99", text)

    def test_paid_bill_before_today_is_shown_with_what_was_paid(self):
        text = _flat(build_cycle_detail(_cycle_data(), 100, 30))
        self.assertIn("Sun Oct 25", text)
        self.assertIn("✓ $9.50", text)

    def test_totals_line_splits_due_and_paid(self):
        text = _flat(build_cycle_detail(_cycle_data(), 100, 30))
        self.assertIn("Still due $265.22", text)
        self.assertIn("Paid this cycle $9.50", text)

    def test_chart_covers_the_full_cycle_and_drops_when_there_is_no_room(self):
        with_chart = _flat(build_cycle_detail(_cycle_data(), 100, 30))
        self.assertIn("◆", with_chart)  # payday markers
        self.assertIn("✓", with_chart)  # paid marker on the track
        tight = build_cycle_detail(_cycle_data(), 100, 9)
        self.assertNotIn("●", _flat(tight))  # no chart (today dot) when rows are short
        self.assertIn("WiFi $23.23", _flat(tight))  # but the breakdown is kept

    def test_long_bill_lists_wrap_instead_of_overflowing(self):
        for line in build_cycle_detail(_cycle_data(), 48, 30):
            self.assertLessEqual(sum(len(t) for t, _ in line), 48)

    def test_masking_hides_amounts(self):
        text = _flat(build_cycle_detail(_cycle_data(width_mask=True), 100, 30))
        for secret in ("38.23", "226.99", "9.50", "265.22"):
            self.assertNotIn(secret, text)

    def test_no_paycheck_plan_returns_nothing(self):
        data = _cycle_data()
        data.paycheck = None
        self.assertEqual(build_cycle_detail(data, 100, 30), [])


class TestRunwayTrack(unittest.TestCase):
    def test_track_spans_every_day_and_marks_today_paid_and_due(self):
        start, end, today = datetime(2026, 10, 22), datetime(2026, 11, 5), datetime(2026, 10, 26)
        lines = runway_track(start, end, today, {"2026-10-30": 3823}, {"2026-10-25": 950}, 60, False)
        self.assertEqual(len(lines), 4)
        track = "".join(t for t, _ in lines[2])
        self.assertEqual(track.count("◆"), 2)  # both paydays
        self.assertEqual(track.count("●"), 1)  # today
        self.assertEqual(track.count("▼"), 1)  # one due date
        self.assertEqual(track.count("✓"), 1)  # one paid date
        self.assertIn("-38", "".join(t for t, _ in lines[3]))

    def test_returns_nothing_when_too_narrow(self):
        start, end = datetime(2026, 10, 22), datetime(2026, 11, 5)
        self.assertEqual(runway_track(start, end, start, {}, {}, 20, False), [])


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

    def test_i_toggles_the_math_screen(self):
        self.key(ord("i"))
        self.assertEqual(self.tui.finances_view_mode, "math")
        self.key(ord("i"))
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
