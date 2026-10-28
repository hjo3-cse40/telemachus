"""Tests for the next-paycheck window and TUI text-width helpers (synthetic data only)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from telemachus.cockpit.finance_panel import FinanceTuiPanel
from telemachus.cockpit.tui import _char_width, _clip_pad
from telemachus.finance.models import PaycheckPlan
from telemachus.finance.recurring import RecurringEngine
from telemachus.finance.setup import apply_setup_yaml
from telemachus.finance.store import FinanceStore


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


class TestPreviousPaycheckDate(unittest.TestCase):
    @staticmethod
    def _prev(plan: PaycheckPlan, today: str) -> str:
        result = RecurringEngine._previous_paycheck_date(plan, datetime.strptime(today, "%Y-%m-%d"))
        return result.strftime("%Y-%m-%d")

    def test_biweekly_mid_cycle(self):
        self.assertEqual(self._prev(_plan(), "2026-10-26"), "2026-10-22")

    def test_on_payday_the_cycle_starts_today(self):
        self.assertEqual(self._prev(_plan(), "2026-11-05"), "2026-11-05")

    def test_day_before_next_payday(self):
        self.assertEqual(self._prev(_plan(), "2026-11-04"), "2026-10-22")

    def test_semimonthly(self):
        plan = _plan("semimonthly")
        self.assertEqual(self._prev(plan, "2026-10-26"), "2026-10-15")
        self.assertEqual(self._prev(plan, "2026-10-09"), "2026-10-01")


_SETUP_YAML = """
as_of_date: "2026-10-01"
starting_balances:
  wells_fargo_checking: 1000.00
fixed_bills:
  spotify:
    amount: 9.50
    due_day: 25
  gym:
    amount: 26.99
    due_day: 24
  wifi:
    amount: 23.23
    due_day: 30
paycheck_plan:
  paycheck_amount: 1246.64
  savings_per_paycheck: 600.00
  frequency: "biweekly"
  anchor_date: "2026-10-22"
"""


class TestPaidEarlierInCycle(unittest.TestCase):
    """A bill paid since the last payday must stay visible after its due date passes."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.yaml_path = Path(self.temp_dir.name) / "setup.yaml"
        self.yaml_path.write_text(_SETUP_YAML, encoding="utf-8")
        self.store = FinanceStore(root_dir=self.temp_dir.name)
        self.store.setup_yaml_path = self.yaml_path
        apply_setup_yaml(yaml_path=self.yaml_path, store=self.store)
        self.engine = RecurringEngine(self.store)
        self.panel = FinanceTuiPanel(self.store)

    def _alloc(self, as_of: str):
        return self.engine.get_paycheck_allocation(as_of_date=as_of, yaml_path=self.yaml_path)

    def test_paid_bill_stays_listed_the_day_after_its_due_date(self):
        self.panel.log_day_bullets("2026-10-25", "spotify - 9.50")
        pc = self._alloc("2026-10-26")
        self.assertEqual([(b["name"], b["due_date"], b["amount_cents"]) for b in pc.paid_bills],
                         [("Spotify", "2026-10-25", 950)])

    def test_paid_bills_do_not_change_upcoming_list_or_totals(self):
        self.panel.log_day_bullets("2026-10-25", "spotify - 9.50")
        pc = self._alloc("2026-10-26")
        self.assertTrue(all(b["due_date"] >= "2026-10-26" for b in pc.bills))
        # Still due: WiFi $23.23 plus the default $75.00 PG&E estimate the engine adds when PG&E isn't configured.
        self.assertEqual(pc.bills_due_cents, 2323 + 7500)

    def test_unpaid_past_bill_is_not_listed_as_paid(self):
        pc = self._alloc("2026-10-26")
        self.assertEqual(pc.paid_bills, [])

    def test_bill_due_before_the_last_payday_is_not_included(self):
        self.panel.log_day_bullets("2026-10-21", "gym - 26.99")  # gym's due day is the 24th, so 10-24 is in the cycle
        pc = self._alloc("2026-10-26")
        self.assertEqual([b["name"] for b in pc.paid_bills if b["due_date"] < "2026-10-22"], [])

    def test_nothing_looks_back_on_payday_itself(self):
        self.panel.log_day_bullets("2026-10-25", "spotify - 9.50")
        pc = self._alloc("2026-11-05")
        self.assertEqual(pc.paid_bills, [])

    def test_cycle_start_is_the_last_payday(self):
        self.assertEqual(self._alloc("2026-10-26").cycle_start_date, "2026-10-22")
        self.assertEqual(self._alloc("2026-11-05").cycle_start_date, "2026-11-05")
        self.assertEqual(self._alloc("2026-10-26").to_dict()["cycle_start_date"], "2026-10-22")

    def test_paid_bills_are_in_the_dict_output(self):
        self.panel.log_day_bullets("2026-10-25", "spotify - 9.50")
        self.assertEqual(self._alloc("2026-10-26").to_dict()["paid_bills"][0]["name"], "Spotify")


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
