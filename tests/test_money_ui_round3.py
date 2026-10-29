"""Money UI review, round 3: one clear per-day story, one status rule on Bills, a 'N more' row
under VISA ACTIVITY, wide panes never showing less than 95 columns, RECENT DAYS after a resize,
and Math scrolling instead of dropping sections. Pure builders, synthetic data only."""
from __future__ import annotations

import re
import unittest

from telemachus.cockpit import finance_view as fv
from telemachus.cockpit.finance_screens import build_bills, build_day, build_math
from telemachus.cockpit.finance_view import (
    BAR,
    VIEW_PREFS,
    build_overview,
    money_keys,
)
from telemachus.cockpit.theme import line_text

from tests.test_money_view import CARDS, make


def flat(rendered) -> str:
    return "\n".join(line_text(line) for line in rendered.lines)


def over_allowance_data(**kw):
    """Cash is fine, but the last week's groceries, dining and gas are over the $18.57 a day."""
    d = make(**kw)
    for day in d.days[:4]:
        day["allowance_spent_cents"] = 4000
    return d


class TestOnePerDayStory(unittest.TestCase):
    def test_legend_names_what_the_bars_compare(self):
        text = flat(build_overview(make(), 94, 40))
        self.assertIn("groceries, dining, gas under $18.57 a day", text)
        self.assertNotIn("allowance a day", text)

    def test_no_green_on_track_while_the_allowance_is_over(self):
        d = over_allowance_data()
        r = build_overview(d, 94, 40)
        hero = next(l for l in r.lines if "after bills" in line_text(l))
        self.assertIn("▲ food & gas over $18.57 a day", line_text(hero))
        self.assertNotIn("on track", line_text(hero))
        self.assertFalse(any("green" in style for _, style in hero))

    def test_on_track_when_cash_and_allowance_are_fine(self):
        self.assertIn("● on track", flat(build_overview(make(), 94, 40)))

    def test_math_pace_explains_the_allowance_status(self):
        VIEW_PREFS["math_scroll"] = 0
        text = flat(build_math(over_allowance_data(), 94, 200))
        words = " ".join(text.split())
        self.assertIn("groceries, dining and gas average", words)
        self.assertIn("over the $18.57 allowance", words)

    def test_wide_overview_flex_note_does_not_point_at_itself(self):
        text = flat(build_overview(make(), 179, 50))
        self.assertIn("Flex left this month", text)
        self.assertNotIn("on the overview", text)


class TestBillsStatusRule(unittest.TestCase):
    def setUp(self):
        VIEW_PREFS["show_hidden_bills"] = False

    def rows(self, d, cols=94, rows=40):
        lines = [line_text(l) for l in build_bills(d, cols, rows).lines]
        return lines

    def test_status_column_is_this_month_on_every_row(self):
        lines = self.rows(make(card="owed"))
        head = next(t for t in lines if t.lstrip().startswith("DUE"))
        self.assertIn("OCTOBER", head)
        self.assertNotIn("STATUS", head)
        visa = next(t for t in lines if "Credit Card (Visa)" in t)
        gym = next(t for t in lines if "Gym Membership" in t)
        # The card's balance is due next month: its row shows October's payment, like the gym.
        self.assertRegex(visa, r"✓ paid +200\.00")
        self.assertNotIn("Nov", visa)
        self.assertIn("✓ paid", gym)
        # The next due date still shows, on the balance line under the total.
        self.assertTrue(any(re.search(r"\+ Visa balance, due Nov 2, not a fixed bill +123\.45", t) for t in lines))

    def test_card_due_this_month_reads_like_the_other_bills(self):
        d = make(card="owed")
        d.card = dict(CARDS["owed"], next_due_date="2026-10-30", paid_this_month_cents=0)
        visa = next(t for t in self.rows(d) if "Credit Card (Visa)" in t)
        self.assertRegex(visa, r"▲ due in 2 days +123\.45")

    def test_card_with_nothing_paid_and_nothing_due_this_month(self):
        d = make(card="owed")
        d.card = dict(CARDS["owed"], paid_this_month_cents=0)
        visa = next(t for t in self.rows(d) if "Credit Card (Visa)" in t)
        self.assertIn("nothing due", visa)


class TestVisaActivityMore(unittest.TestCase):
    def test_cut_list_ends_with_a_more_row_that_adds_up(self):
        from telemachus.cockpit.finance_screens import _card_activity

        d = make(card="owed")
        lines = [line_text(l) for l in _card_activity(d, 80, 7, False)]
        self.assertEqual(len(lines), 7)
        start = 0
        more = next(t for t in lines[start:] if re.search(r"· \d+ more", t))
        n = int(re.search(r"· (\d+) more", more).group(1))
        shown = [t for t in lines[start + 1: lines.index(more)] if re.search(r"[A-Z][a-z]{2} [A-Z][a-z]{2} +\d", t)]
        items = fv_card_items(d)
        self.assertEqual(len(shown) + n, len(items))
        rest = items[len(shown):]
        charged = sum(it["amount_cents"] for _, it in rest if it["kind"] != "card_payment")
        self.assertIn(fv.num(charged), more)
        self.assertIn("charged", more)

    def test_short_column_still_says_how_many_are_left(self):
        text = flat(build_bills(make(card="owed"), 119, 40))
        if "VISA ACTIVITY" in text:
            self.assertRegex(text.split("VISA ACTIVITY")[1], r"· \d+ more")


def fv_card_items(d):
    from telemachus.cockpit.finance_screens import _card_items

    return _card_items(d)


def bar_lengths(rendered) -> dict:
    out = {}
    for k in rendered.day_rows:
        text = line_text(rendered.lines[k])
        m = re.search(r"([A-Z][a-z]{2} [A-Z][a-z]{2} +\d+)\s+(" + BAR + r"+)", text)
        if m:
            out[" ".join(m.group(1).split())] = len(m.group(2))
    return out


class TestWiderNeverShowsLess(unittest.TestCase):
    def test_overview_bars_and_notes_at_least_as_at_95(self):
        base = build_overview(make(), 94, 44)
        base_bars = bar_lengths(base)
        for cols in (119, 139, 179):
            r = build_overview(make(), cols, 44)
            text = flat(r)
            self.assertIn("⇄ 200 to savings", text, cols)
            for day, n in bar_lengths(r).items():
                if day in base_bars:
                    self.assertGreaterEqual(n, base_bars[day], f"{cols}: {day}")

    def test_bills_before_payday_keeps_its_left_column(self):
        for cols in (94, 119, 179):
            text = flat(build_overview(make(), cols, 44))
            head = next(t for t in text.splitlines() if "DATE" in t and "BILL" in t)
            self.assertIn("LEFT", head, cols)

    def test_day_view_keeps_the_item_counts(self):
        d = make(sel=3)  # Oct 23: payday, savings and a purchase, so its notes are the longest
        for cols in (94, 119, 179):
            text = flat(build_day(d, cols, 44))
            row = next(t for t in text.splitlines() if "Fri Oct 23" in t and "pay" in t)
            self.assertIn("3 items", row, cols)
            self.assertIn("⇄ 200 to savings", row, cols)


class TestRecentDaysAfterResize(unittest.TestCase):
    def test_a_stale_offset_never_hides_today(self):
        d = make(sel=3)
        d.day_scroll_offset = 4  # what a short pane left behind
        r = build_overview(d, 94, 40)
        self.assertIn(0, r.day_rows.values())  # the newest day is on screen again
        self.assertEqual(r.day_scroll_offset, 0)

    def test_rows_above_are_announced(self):
        d = make(sel=12)
        r = build_overview(d, 94, 22)
        self.assertGreater(r.day_scroll_offset, 0)
        self.assertIn(f"{r.day_scroll_offset} newer above", flat(r))


class TestMathScrolls(unittest.TestCase):
    TITLES = ("THIS PAY PERIOD", "VISA", "OCTOBER", "WHY TWO NUMBERS", "PACE", "PAY PERIODS", "SPENT BY CATEGORY")

    def setUp(self):
        VIEW_PREFS["math_scroll"] = 0

    def tearDown(self):
        VIEW_PREFS["math_scroll"] = 0
        VIEW_PREFS["math_scrollable"] = False

    def test_every_section_is_reachable_on_a_short_pane(self):
        for cols, rows in ((94, 18), (59, 18), (139, 18)):
            VIEW_PREFS["math_scroll"] = 0
            seen = ""
            for _ in range(120):
                r = build_math(make(), cols, rows)
                self.assertLessEqual(len(r.lines), rows)
                seen += flat(r) + "\n"
                fv.scroll_math(1)
            for title in self.TITLES:
                self.assertIn(title, seen, f"{cols}x{rows}")

    def test_scroll_hint_and_footer_only_when_needed(self):
        short = flat(build_math(make(), 94, 18))
        self.assertIn("below  j/k scroll", short.splitlines()[0])
        footer = [h for g in money_keys("math") for h in g.hints if h.footer]
        self.assertIn("j/k", [h.keys for h in footer])
        tall = flat(build_math(make(), 94, 200))
        self.assertNotIn("j/k scroll", tall)
        for title in self.TITLES:
            self.assertIn(title, tall)
        footer = [h for g in money_keys("math") for h in g.hints if h.footer]
        self.assertNotIn("j/k", [h.keys for h in footer])

    def test_scroll_is_clamped(self):
        fv.scroll_math(500)
        r = build_math(make(), 94, 18)
        last = VIEW_PREFS["math_scroll"]
        self.assertLess(last, 500)
        self.assertIn("above", line_text(r.lines[0]))
        self.assertNotIn("below", line_text(r.lines[0]))
        fv.scroll_math(-1)
        self.assertEqual(VIEW_PREFS["math_scroll"], last - 1)

    def test_down_key_scrolls_math(self):
        self.assertEqual(fv.money_action("math", fv._DOWN), "down")
        self.assertEqual(fv.money_action("math", ord("j")), "down")


if __name__ == "__main__":
    unittest.main()
