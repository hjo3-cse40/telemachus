"""Money UI review fixes: one amount edge per screen column (Bills), capped table widths on wide
panes (Bills, Day), timeline labels that never show an amount without its bill name, the goal line
on narrow panes, and the refresh-safe selection helpers. Pure builders, synthetic data only."""
from __future__ import annotations

import re
import unittest
from dataclasses import replace

from telemachus.cockpit.finance_panel import compute_runway
from telemachus.cockpit.finance_screens import TABLE_MAX, bills_edge, items_width
from telemachus.cockpit.finance_view import (
    BAR,
    build_bills,
    build_day,
    build_overview,
    find_selection,
    goal_lines,
    runway_strip,
    selection_key,
)
from telemachus.cockpit.theme import line_text

from tests.test_money_view import TODAY, _pc, make

AMOUNT = re.compile(r"[+-]?(?:\d[\d,]*\.\d\d|•••)")


def lines_of(rendered) -> list:
    return [line_text(line) for line in rendered.lines]


def amount_edges(lines, start: int, end: int) -> set:
    """End columns of the last amount in each line's [start, end) slice (section titles skipped:
    their totals sit at the end of the rule, not in the amount column)."""
    edges = set()
    for text in lines:
        part = text[start:end]
        if "──" in part and not part.strip().startswith("─"):
            continue
        found = list(AMOUNT.finditer(part))
        if found:
            edges.add(start + found[-1].end())
    return edges


def with_close_bills(mask: bool = False):
    """The fixture plus a Visa payment the day after the gym, the case where labels crowd."""
    d = make(mask=mask)
    pc = _pc()
    pc.bills = list(pc.bills) + [{"name": "Credit Card Payment (Visa)", "due_date": "2026-11-02",
                                  "amount_cents": 43200, "status": "due"}]
    return replace(d, paycheck=pc, runway=compute_runway(TODAY, pc, d.days))


class TestBillsAmountEdge(unittest.TestCase):
    def test_one_column_has_one_amount_edge(self):
        for cols, rows in ((94, 46), (80, 60), (60, 70)):
            for mask in (False, True):
                lines = lines_of(build_bills(make(mask=mask), cols, rows))
                body = [t for t in lines if "BILL CALENDAR" not in t]
                edges = amount_edges(body, 0, cols)
                self.assertEqual(len(edges), 1, f"{cols}x{rows} mask={mask}: {sorted(edges)}")

    def test_each_wide_column_has_one_amount_edge(self):
        for cols, rows in ((120, 40), (180, 50)):
            d = make()
            left_w = bills_edge(d, cols - 2 - 62)
            lines = lines_of(build_bills(d, cols, rows))
            cal = next(i for i, t in enumerate(lines) if "BILL CALENDAR" in t)
            body = lines[:cal]
            self.assertEqual(amount_edges(body, 0, left_w), {left_w}, f"left {cols}x{rows}")
            self.assertEqual(amount_edges(body, left_w + 2, cols), {left_w + 2 + 62}, f"right {cols}x{rows}")

    def test_budget_rows_end_on_the_amount_not_a_word(self):
        text = "\n".join(lines_of(build_bills(make(), 94, 46)))
        self.assertRegex(text, r"over +129\.41")
        self.assertNotIn("  paid\n", text)  # Visa payments no longer trail a word after the amount


class TestWideTablesStayCompact(unittest.TestCase):
    def test_bills_left_column_is_capped(self):
        d = make()
        edge = bills_edge(d, 180 - 2 - 62)
        self.assertLessEqual(edge, TABLE_MAX)
        lines = lines_of(build_bills(d, 180, 50))
        row = next(t for t in lines if "Gym Membership" in t)
        status, amount = row.index("✓ paid"), row.index("25.00")
        self.assertLess(amount - status, 25, row)

    def test_day_items_are_compact_and_nearby_bars_get_the_room(self):
        d = make()
        day = d.days[0]
        self.assertLess(items_width(day, 120), 80)
        lines = lines_of(build_day(d, 180, 40))
        row = next(t for t in lines if t.lstrip("▸ ").startswith("checking") and "lunch" in t)
        self.assertLess(row.index("-13.15") - row.index("lunch"), 30, row)
        longest_bar = max(t.count(BAR) for t in lines)
        self.assertGreaterEqual(longest_bar, 20)


class TestTimelineLabels(unittest.TestCase):
    def label_rows(self, d, width, nrows):
        rows = [line_text(r) for r in runway_strip(d, width, nrows)]
        track = next(i for i, t in enumerate(rows) if "◆" in t)
        out = rows[track + 1:]
        if nrows >= 3:
            out = out[1:]  # the day numbers
        return out

    def test_no_amount_without_a_name(self):
        for mask in (False, True):
            d = with_close_bills(mask)
            for width in (45, 60, 70, 80, 94, 120):
                for nrows in (2, 3, 4, 5):
                    for row in self.label_rows(d, width, nrows):
                        for chunk in re.split(r" {2,}", row.strip()):
                            if chunk.startswith("-"):
                                self.assertRegex(chunk, r"^-\S+ [A-Za-z&]", f"{width} {nrows} {mask}: {row!r}")

    def test_close_bills_get_a_second_row_and_both_names(self):
        d = with_close_bills()
        text = "\n".join(line_text(r) for r in runway_strip(d, 94, 5))
        self.assertIn("-25 Gym", text)
        self.assertIn("-432 Visa", text)
        self.assertIn("Oct", text)  # five rows keep the months
        four = [line_text(r) for r in runway_strip(d, 94, 4)]
        self.assertEqual(len(four), 4)
        self.assertIn("-25 Gym", "\n".join(four))

    def test_masked_labels_keep_the_rows_of_the_plain_ones(self):
        plain = runway_strip(with_close_bills(), 94, 5)
        masked = runway_strip(with_close_bills(True), 94, 5)
        self.assertEqual(len(plain), len(masked))
        self.assertIn("Visa", "\n".join(line_text(r) for r in masked))

    def test_two_rows_keep_the_amounts(self):
        two = [line_text(r) for r in runway_strip(with_close_bills(), 70, 2)]
        self.assertEqual(len(two), 2)
        self.assertIn("◆", two[0])
        self.assertIn("-432 Visa", two[1])

    def test_overview_keeps_both_names_at_120(self):
        text = "\n".join(lines_of(build_overview(with_close_bills(), 120, 40)))
        self.assertIn("-25 Gym", text)
        self.assertIn("-432 Visa", text)


class TestGoalLine(unittest.TestCase):
    def test_goal_line_never_cuts_an_amount(self):
        for mask in (False, True):
            d = make(mask=mask)
            target = "$••• of $•••" if mask else "$200 of $600"
            for width in range(40, 90):
                text = line_text(goal_lines(d, width, inline=True)[0]).rstrip()
                self.assertLessEqual(len(text), width)
                self.assertIn(target, text, f"width={width} mask={mask}")
                self.assertTrue(text.endswith(target) or text.endswith("this month"), text)

    def test_goal_line_drops_this_month_before_the_bar(self):
        d = make()
        wide = line_text(goal_lines(d, 94, inline=True)[0])
        self.assertIn("this month", wide)
        self.assertIn("━", wide)
        mid = line_text(goal_lines(d, 52, inline=True)[0])
        self.assertNotIn("this month", mid)
        self.assertIn("━", mid)

    def test_plain_and_masked_pick_the_same_layout(self):
        for width in range(40, 90):
            plain = line_text(goal_lines(make(), width, inline=True)[0])
            masked = line_text(goal_lines(make(mask=True), width, inline=True)[0])
            self.assertEqual(plain.count("━") + plain.count("─"), masked.count("━") + masked.count("─"))
            self.assertEqual("this month" in plain, "this month" in masked)


class TestSelectionHelpers(unittest.TestCase):
    def test_key_names_the_entry_and_survives_a_newer_day(self):
        days = make().days
        key = selection_key(days, 2, 0, "items")
        self.assertEqual(key, ("2026-10-26", days[2]["items"][0]["id"], "items"))
        newer = [{"date": "2026-10-29", "items": [{"id": "x", "payee": "parking"}]}] + days
        self.assertEqual(find_selection(newer, key[0], key[1]), (3, 0))

    def test_gone_entry_or_day(self):
        days = make().days
        self.assertEqual(find_selection(days, "2026-10-26", "nope"), (2, None))
        self.assertEqual(find_selection(days, "2026-10-27", None), (1, None))
        self.assertEqual(find_selection(days, "2026-01-01", "x"), (None, None))
        self.assertEqual(selection_key([], 0, 0), (None, None, "items"))
        self.assertEqual(selection_key(days, 0, 0, "days")[1], None)


if __name__ == "__main__":
    unittest.main()
