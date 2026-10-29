"""Integration tests for the TUI wiring added after the view rebuild: Money footer width and mask
label, day stepping, Bills selection limits, Memory search key capture, Home mask, the dimmed
screen behind a box, paste bursts, and the no-data Money screen. Synthetic data only."""
from __future__ import annotations

import curses
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from telemachus.cockpit import finance_view, shell, theme
from telemachus.cockpit.finance_screens import _period_lines
from telemachus.cockpit.memory_view import MemoryData
from telemachus.cockpit.telemetry import CockpitTelemetry
from telemachus.cockpit.tui import CockpitTUI

try:
    from tests.cockpit_isolation import isolate_module
except ImportError:  # run from inside tests/
    from cockpit_isolation import isolate_module

# CockpitTUI() opens the finance ledger and memory vault: keep them in a temp dir, never Sam's.
setUpModule, tearDownModule = isolate_module()

from tests.test_money_view import _bills, _days, make


def footer_text(tui: CockpitTUI, width: int) -> str:
    groups, with_global = tui._footer_groups()
    return theme.line_text(shell.build_footer(groups, width, with_global))


class _Base(unittest.TestCase):
    @patch("curses.color_pair", return_value=0)
    @patch("curses.curs_set")
    def setUp(self, *_):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stdscr = MagicMock()
        self.stdscr.getmaxyx.return_value = (46, 95)
        with patch.object(CockpitTUI, "refresh_data"), patch.object(CockpitTUI, "_init_curses"):
            self.tui = CockpitTUI(self.stdscr)
        self.tui.telemetry = CockpitTelemetry(lm_studio_url="http://127.0.0.1:9/v1/models",
                                              status_path=Path(self.tmp.name) / "s.json",
                                              opencode_db=Path(self.tmp.name) / "none.db")
        self.tui.finance_days = _days()
        self.tui.finance_bills_breakdown = _bills()
        finance_view.VIEW_PREFS["show_hidden_bills"] = False

    def drawn(self) -> str:
        return "".join(str(c[0][2]) for c in self.stdscr.addstr.call_args_list if len(c[0]) >= 3)


class TestMoneyWiring(_Base):
    @patch("curses.color_pair", return_value=0)
    def test_footer_trims_to_the_width_and_says_show_when_masked(self, _):
        self.tui.current_tab = "money"
        self.stdscr.getmaxyx.return_value = (30, 80)
        self.tui.render()
        text = footer_text(self.tui, 79)
        self.assertIn("v bills", text)
        self.assertIn("q quit", text)
        self.tui.handle_key(ord("m"))
        self.tui.render()
        self.assertIn("m show $", footer_text(self.tui, 200))

    def test_left_and_right_step_days_on_the_day_screen(self):
        self.tui.current_tab = "money"
        self.tui.handle_key(10)  # open the newest day
        self.assertEqual(self.tui.finances_view_mode, "day")
        self.tui.selected_item_idx = 1
        self.tui.handle_key(curses.KEY_LEFT)  # older
        self.assertEqual((self.tui.selected_day_idx, self.tui.selected_item_idx), (1, 0))
        self.tui.handle_key(curses.KEY_RIGHT)  # newer
        self.assertEqual(self.tui.selected_day_idx, 0)
        self.tui.handle_key(curses.KEY_RIGHT)  # already the newest
        self.assertEqual(self.tui.selected_day_idx, 0)

    @patch("curses.color_pair", return_value=0)
    def test_bills_selection_stays_on_shown_bills(self, _):
        self.tui.current_tab = "money"
        self.tui.handle_key(ord("v"))
        shown = finance_view.bill_count(self.tui.finance_bills_breakdown)
        self.assertLess(shown, len(self.tui.finance_bills_breakdown["bills"]))
        for _ in range(20):
            self.tui.handle_key(ord("j"))
        self.assertEqual(self.tui.selected_bill_idx, shown - 1)
        self.tui.handle_key(ord("k"))
        self.assertEqual(self.tui.selected_bill_idx, shown - 2)
        # Show hidden bills, go to the last one, hide them again: the selection is clamped on redraw.
        self.tui.handle_key(ord("h"))
        for _ in range(20):
            self.tui.handle_key(ord("j"))
        self.tui.handle_key(ord("h"))
        self.tui.render()
        self.assertEqual(self.tui.selected_bill_idx, shown - 1)

    @patch("curses.color_pair", return_value=0)
    def test_new_vault_shows_the_setup_hint_not_default_numbers(self, _):
        self.tui.current_tab = "money"
        self.tui.finance_has_data = False
        self.tui.render()
        drawn = self.drawn()
        self.assertIn("No finance data yet.", drawn)
        self.assertNotIn("short", drawn)


class TestMemorySearchCapture(_Base):
    def test_q_and_digits_are_typed_while_searching(self):
        self.tui.view_data["memory"] = MemoryData()
        self.tui.current_tab = "memory"
        self.tui.handle_key(ord("/"))
        self.assertTrue(self.tui._view_captures_keys())
        for ch in "q2r?":
            self.assertTrue(self.tui.handle_key(ord(ch)))  # never quits
        self.assertEqual(self.tui.current_tab, "memory")
        self.assertFalse(self.tui.help_open)
        self.assertEqual(self.tui.view_state["memory"].get("query"), "q2r?")
        self.assertNotIn("q quit", footer_text(self.tui, 120))
        self.tui.handle_key(27)  # esc clears the search
        self.assertFalse(self.tui._view_captures_keys())
        self.assertFalse(self.tui.handle_key(ord("q")))


class TestHomeMaskAndBoxes(_Base):
    @patch("curses.color_pair", return_value=0)
    def test_home_gets_the_mask_flag(self, _):
        self.tui.mask_amounts = True
        self.tui.current_tab = "home"
        self.tui.render()
        self.assertTrue(self.tui.view_state["home"]["mask"])

    @patch("curses.color_pair", return_value=0)
    def test_screen_behind_an_open_box_is_dim(self, _):
        self.tui.current_tab = "money"
        styles = []
        orig = self.tui._put

        def spy(y, x, line, width):
            if self.tui.layout_body_rows[0] <= y < self.tui.layout_body_rows[1] and x == 0:
                styles.extend(s for _, s in line)
            return orig(y, x, line, width)

        lay = shell.layout(46, 95)
        self.tui.layout_body_rows = (lay.body_top, lay.body_top + lay.body_height)
        self.tui.open_add_modal()
        with patch.object(self.tui, "_put", side_effect=spy):
            self.tui.render()
        self.assertTrue(styles)
        self.assertEqual(set(styles), {"dim"})

    def test_paste_burst_with_a_blank_line_does_not_save(self):
        self.tui.current_tab = "money"
        self.tui.open_add_modal()
        panel = MagicMock()
        self.tui.finance_panel = panel
        keys = [ord(c) for c in "lunch 12"] + [10, 10] + [ord(c) for c in "coffee 4"]
        self.stdscr.getch.side_effect = keys[1:] + [-1]
        burst, pending = self.tui._drain_keys(keys[0])
        self.assertEqual((burst, pending), (keys, []))
        self.tui._apply_modal_result(self.tui._get_modals().handle_keys(burst))
        self.assertTrue(self.tui._get_modals().active)
        panel.log_day_bullets.assert_not_called()
        self.assertEqual(self.tui._get_modals().batch_lines, ["lunch 12"])

    def test_drain_stops_at_a_resize(self):
        self.stdscr.getch.side_effect = [ord("b"), curses.KEY_RESIZE, ord("c"), -1]
        burst, pending = self.tui._drain_keys(ord("a"))
        self.assertEqual(burst, [ord("a"), ord("b")])
        self.assertEqual(pending, [curses.KEY_RESIZE])


class TestScreensFixes(unittest.TestCase):
    def test_pay_periods_never_cut_the_per_day_number(self):
        d = make()
        for width in (40, 48, 56, 70, 94):
            lines = _period_lines(d, width, 10, False)
            for line in lines[1:]:
                text = theme.line_text(line).rstrip()
                self.assertTrue(text.endswith("a day") or text.endswith("/day"), (width, text))
                self.assertLessEqual(theme.line_width(line), width)

    def test_long_help_text_wraps_instead_of_being_cut(self):
        # Was: cut with an ellipsis. The help card is where the full key text is read, so it wraps.
        long_text = "a very long help text that cannot fit in one column of the keys card, " * 2
        group = shell.KeyGroup.of("X", shell.KeyHint("d", "delete", long_text))
        ov = shell.build_help([group], 60, 4, 30)
        joined = "\n".join(theme.line_text(line) for line in ov.lines)
        self.assertNotIn("…", joined)
        words = " ".join(w for line in ov.lines for w in theme.line_text(line).strip("│╭╮╰╯─ ").split())
        self.assertIn(" ".join(long_text.split()), words)
        self.assertTrue(all(theme.line_width(line) == ov.width for line in ov.lines))


if __name__ == "__main__":
    unittest.main()
