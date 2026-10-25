"""Tests for the next-paycheck window and TUI text-width helpers (synthetic data only)."""
from __future__ import annotations

from datetime import datetime
import unittest

from telemachus.cockpit.tui import _char_width, _clip_pad
from telemachus.finance.models import PaycheckPlan
from telemachus.finance.recurring import RecurringEngine


def _plan(frequency: str = "biweekly", anchor: str = "2026-10-22") -> PaycheckPlan:
    return PaycheckPlan(
        paycheck_amount_cents=100000,
        savings_per_paycheck_cents=40000,
        frequency=frequency,
        anchor_date=anchor,
    )


def _next(plan: PaycheckPlan, today: str) -> str:
    result = RecurringEngine._next_paycheck_date(plan, datetime.strptime(today, "%Y-%m-%d"))
    return result.strftime("%Y-%m-%d")


class TestNextPaycheckDate(unittest.TestCase):
    def test_biweekly_from_anchor(self):
        self.assertEqual(_next(_plan(), "2026-10-24"), "2026-11-05")

    def test_biweekly_day_after_anchor(self):
        self.assertEqual(_next(_plan(), "2026-10-23"), "2026-11-05")

    def test_on_payday_rolls_to_following_payday(self):
        self.assertEqual(_next(_plan(), "2026-11-05"), "2026-11-19")

    def test_day_before_payday(self):
        self.assertEqual(_next(_plan(), "2026-11-04"), "2026-11-05")

    def test_on_anchor_day(self):
        self.assertEqual(_next(_plan(), "2026-10-22"), "2026-11-05")

    def test_today_before_anchor(self):
        self.assertEqual(_next(_plan(), "2026-10-15"), "2026-10-22")

    def test_semimonthly(self):
        plan = _plan("semimonthly")
        self.assertEqual(_next(plan, "2026-10-24"), "2026-11-01")
        self.assertEqual(_next(plan, "2026-10-01"), "2026-10-15")
        self.assertEqual(_next(plan, "2026-12-20"), "2027-01-01")

    def test_bad_anchor_falls_back_to_14_days(self):
        self.assertEqual(_next(_plan(anchor="not-a-date"), "2026-10-24"), "2026-11-07")


class TestTuiTextWidth(unittest.TestCase):
    def test_clip_pad_exact_width_ascii(self):
        self.assertEqual(_clip_pad("abc", 6), "abc   ")
        self.assertEqual(_clip_pad("abcdef", 3), "abc")

    def test_clip_pad_counts_wide_emoji_as_two_cells(self):
        out = _clip_pad("\U0001F4B0 hi", 8)
        self.assertEqual(sum(_char_width(c) for c in out), 8)

    def test_clip_pad_never_splits_wide_char(self):
        out = _clip_pad("a\U0001F4B0", 2)
        self.assertEqual(sum(_char_width(c) for c in out), 2)
        self.assertEqual(out, "a ")

    def test_variation_selector_removed(self):
        self.assertEqual(_clip_pad("\U0001F6E1\ufe0f", 3).count("\ufe0f"), 0)


if __name__ == "__main__":
    unittest.main()
