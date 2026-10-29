"""Tests for the Cockpit foundation: theme, shell (header, tabs, footer, help), telemetry header
states, the finance modal controller, and the TUI shell keys. Synthetic data only."""
from __future__ import annotations

import curses
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from telemachus.cockpit import shell, theme
from telemachus.cockpit.finance_modals import FinanceModals
from telemachus.cockpit.finance_panel import FinanceTuiPanel
from telemachus.cockpit.guard_view import GuardData, build_guard
from telemachus.cockpit.home_view import HomeData, build_home
from telemachus.cockpit.memory_view import MemoryData, build_memory
from telemachus.cockpit.telemetry import CockpitTelemetry, HeaderInfo, _PollResult
from telemachus.cockpit.tui import CockpitTUI

try:
    from tests.cockpit_isolation import isolate_module
except ImportError:  # run from inside tests/
    from cockpit_isolation import isolate_module

# CockpitTUI() opens the finance ledger and memory vault: keep them in a temp dir, never Sam's.
setUpModule, tearDownModule = isolate_module()

FAKE_NUMBERS = ("14,820", "14820", "41.5", "58.2", "65,536")


def text(line) -> str:
    return theme.line_text(line)


class TestTheme(unittest.TestCase):
    def test_aliases_resolve_to_canonical_styles(self):
        self.assertEqual(theme.resolve("sel"), "text+b@sel")
        self.assertEqual(theme.resolve("snow+b"), "text+b")
        self.assertEqual(theme.resolve("fg"), "text")
        self.assertEqual(theme.resolve("sub"), "muted")
        self.assertEqual(theme.resolve("line"), "dim")
        self.assertEqual(theme.resolve("mag+b"), "magenta+b")
        self.assertEqual(theme.resolve("dim"), "dim")
        self.assertEqual(theme.resolve("yellow+b@card"), "yellow+b@card")
        self.assertEqual(theme.resolve("nonsense"), "text")

    def test_nearest_xterm_colors(self):
        self.assertEqual(theme.nearest_xterm("#7dcfff"), 117)  # cyan
        self.assertEqual(theme.nearest_xterm("#565f89"), 60)  # dim
        for hx in list(theme.PALETTE.values()) + list(theme.BACKGROUNDS.values()):
            self.assertTrue(16 <= theme.nearest_xterm(hx) <= 255)

    def test_fit_counts_cells_not_characters(self):
        line = theme.fit([("ab", "text"), ("漢字", "cyan")], 5)
        self.assertEqual(theme.line_width(line), 5)
        self.assertEqual(text(line), "ab漢 ")

    def test_section_rule_is_exact_width(self):
        line = theme.section("BILLS BEFORE PAYDAY", 60, "$85.00 due")
        self.assertEqual(theme.line_width(line), 60)
        self.assertTrue(text(line).startswith("  BILLS BEFORE PAYDAY ─"))
        self.assertTrue(text(line).endswith("$85.00 due"))

    def test_attr_works_before_curses_starts(self):
        self.assertEqual(theme.attr("text+b") & curses.A_BOLD, curses.A_BOLD)

    def test_glyphs_are_single_cell(self):
        for name, g in theme.GLYPHS.items():
            self.assertEqual(theme.cell_width(g), len(g), name)
        for g in theme.SPINNER:
            self.assertEqual(theme.cell_width(g), 1)


class TestHeader(unittest.TestCase):
    NOW = 1_000_000.0

    def header(self, info: HeaderInfo, width: int = 94) -> str:
        line = shell.build_header(info, width, self.NOW, "14:32")
        self.assertLessEqual(theme.line_width(line), width)
        out = text(line)
        for fake in FAKE_NUMBERS:
            self.assertNotIn(fake, out)
        return out

    def test_ready_shows_model_dot_and_real_numbers(self):
        out = self.header(HeaderInfo(lm="ready", model="qwen3.5-9b", tps=41.2, ctx_used=14000, ctx_max=65536,
                                     ram_used_gb=7.1, ram_total_gb=24.0))
        self.assertIn("qwen3.5-9b ● ready", out)
        self.assertIn("ctx 21%", out)
        self.assertIn("ram 7.1/24G", out)
        self.assertIn("41 t/s", out)
        self.assertTrue(out.rstrip().endswith("14:32"))
        self.assertLess(out.index("ctx"), out.index("ram"))

    def test_offline_hides_ctx_and_speed(self):
        out = self.header(HeaderInfo(lm="offline", tps=40.0, ctx_used=100, ctx_max=1000, ram_used_gb=4.1, ram_total_gb=24.0))
        self.assertIn("○ LM Studio offline", out)
        self.assertNotIn("ctx", out)
        self.assertNotIn("t/s", out)
        self.assertIn("ram 4.1/24G", out)

    def test_unmeasured_fields_are_hidden(self):
        out = self.header(HeaderInfo(lm="ready", model="m"))
        self.assertNotIn("ctx", out)
        self.assertNotIn("ram", out)
        self.assertNotIn("t/s", out)

    def test_reading_shows_spinner_and_elapsed(self):
        out = self.header(HeaderInfo(lm="reading", model="m", elapsed_s=3.4))
        self.assertIn("reading prompt 3s", out)
        self.assertTrue(any(f in out for f in theme.SPINNER))
        self.assertEqual(shell._elapsed(75), "1m 15s")

    def test_writing_shows_live_tokens_and_speed(self):
        out = self.header(HeaderInfo(lm="writing", model="m", tokens=1482, tps=38.4, ram_used_gb=7.0, ram_total_gb=24.0))
        self.assertIn("writing 1.5k tok · 38 t/s", out)
        self.assertEqual(out.count("t/s"), 1)  # no second speed field while writing

    def test_spinner_moves_at_eight_frames_a_second(self):
        frames = {shell.spinner_frame(self.NOW + i * 0.125) for i in range(10)}
        self.assertEqual(len(frames), 10)
        self.assertEqual(shell.spinner_frame(self.NOW), shell.spinner_frame(self.NOW + 0.01))

    def test_narrow_header_drops_fields_but_keeps_indicator(self):
        info = HeaderInfo(lm="ready", model="qwen3.5-9b-instruct-mlx", tps=41, ctx_used=1, ctx_max=10,
                          ram_used_gb=7.1, ram_total_gb=24.0, think_tokens=1200)
        for width in (30, 45, 60, 94, 139):
            out = self.header(info, width)
            self.assertIn("telemachus", out)
            self.assertIn("●", out)


class TestTabsFooterHelp(unittest.TestCase):
    def test_underline_sits_under_the_active_name(self):
        row, under, spans = shell.build_tabs("money", 94)
        self.assertTrue(text(row).startswith("  1 Home    2 Money    3 Memory    4 Guard"))
        start = text(row).index("Money")
        self.assertEqual(text(under)[start : start + 5], "━━━━━")
        self.assertEqual(text(under).count("━"), 5)
        self.assertEqual(shell.tab_at(spans, start + 1), "money")
        self.assertEqual(shell.tab_at(spans, text(row).index("Guard")), "guard")
        self.assertIsNone(shell.tab_at(spans, 0))

    def test_footer_groups_and_global_keys(self):
        groups = [
            shell.KeyGroup.of("A", shell.KeyHint("j/k", "move"), shell.KeyHint("x", "hidden", footer=False)),
            shell.KeyGroup.of("B", shell.KeyHint("v", "bills")),
        ]
        out = text(shell.build_footer(groups, 94))
        self.assertEqual(out.rstrip(), "  j/k move   ·   v bills   ·   ? keys  q quit")
        narrow = text(shell.build_footer(groups, 36))
        self.assertIn("? keys", narrow)
        self.assertNotIn("v bills", narrow)

    def test_help_lists_every_key_from_the_same_data(self):
        groups = [shell.KeyGroup.of("MONEY", shell.KeyHint("a", "add", "add entries"),
                                    shell.KeyHint("⌃S", "save", "save all", footer=False))]
        ov = shell.build_help(groups, 94, shell.BODY_TOP, 40)
        body = "\n".join(text(l) for l in ov.lines)
        for needle in ("Keys", "esc close", "EVERYWHERE", "MONEY", "add entries", "save all", "switch tab", "this help"):
            self.assertIn(needle, body)
        self.assertTrue(all(theme.line_width(l) == ov.width for l in ov.lines))

    def test_toast_is_right_aligned(self):
        out = text(shell.build_toast("saved 3 items", "ok", 60))
        self.assertEqual(len(out), 60)
        self.assertTrue(out.rstrip().endswith("✓ saved 3 items"))

    def test_layout_reserves_toast_and_footer_rows(self):
        lay = shell.layout(46, 95)
        self.assertEqual((lay.width, lay.body_top, lay.body_height, lay.toast_row, lay.footer_row), (94, 4, 40, 44, 45))


class TestTelemetryHeaderInfo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.status = Path(self.tmp.name) / "llm_status.json"
        self.t = CockpitTelemetry(lm_studio_url="http://127.0.0.1:9/v1/models", status_path=self.status,
                                  opencode_db=Path(self.tmp.name) / "none.db")

    def write(self, data):
        self.status.write_text(json.dumps(data), encoding="utf-8")

    def test_unknown_before_first_poll_then_offline(self):
        self.assertEqual(self.t.header_info().lm, "unknown")
        self.t.poll_once()  # nothing listens on port 9
        info = self.t.header_info()
        self.assertEqual(info.lm, "offline")
        self.assertIsNone(info.ctx_pct)

    def test_ready_uses_loaded_model_and_context(self):
        self.t._poll = _PollResult(reachable=True, models=["qwen/qwen3.5-9b"], ctx_max=65536, ctx_used=16384,
                                   polled_at=time.time())
        self.write({"status": "idle", "last_speed_tps": 40.6})
        info = self.t.header_info()
        self.assertEqual((info.lm, info.model, info.ctx_pct, info.tps), ("ready", "qwen3.5-9b", 25, 40.6))

    def test_reading_and_writing_from_status_file(self):
        self.t._poll = _PollResult(reachable=True, models=["m"], polled_at=time.time())
        now = time.time()
        self.write({"status": "processing_prompt", "start_time": now - 4})
        info = self.t.header_info(now)
        self.assertEqual(info.lm, "reading")
        self.assertAlmostEqual(info.elapsed_s, 4, delta=0.5)
        self.write({"status": "producing_tokens", "tokens_produced": 99, "speed_tps": 33.0, "start_time": now})
        os.utime(self.status, (now + 1, now + 1))  # force a new mtime so the cache re-reads
        info = self.t.header_info(now + 1)
        self.assertEqual((info.lm, info.tokens, info.tps), ("writing", 99, 33.0))

    def test_stalled_writing_reads_as_idle(self):
        now = time.time()
        self.write({"status": "producing_tokens", "tokens_produced": 5, "speed_tps": 9.0, "start_time": now - 100})
        os.utime(self.status, (now - 60, now - 60))
        self.assertEqual(self.t.get_llm_status(now=now)["status"], "idle")

    def test_no_made_up_numbers(self):
        self.assertIsNone(self.t.get_inference_speed())
        ctx = self.t.get_context_budget()
        self.assertIsNone(ctx["used_pct"])


class TestFinanceModals(unittest.TestCase):
    def test_add_flow_commits_through_the_panel(self):
        panel = MagicMock()
        panel.log_day_bullets.return_value = [1, 2]
        m = FinanceModals(panel_getter=lambda: panel)
        m.open_add("2026-10-23")
        self.assertTrue(m.active)
        m.handle_key(10)  # confirm date
        for ch in "gas - 40":
            m.handle_key(ord(ch))
        m.handle_key(10)
        res = m.handle_key(19)  # Ctrl+S
        self.assertTrue(res.closed and res.changed)
        self.assertEqual(res.toast_kind, "ok")
        panel.log_day_bullets.assert_called_once_with("2026-10-23", "gas - 40")
        self.assertFalse(m.active)

    def test_edit_opens_day_menu_then_item(self):
        panel = MagicMock()
        panel.item_to_bullet.side_effect = lambda it: f"- {it['payee']} - 1"
        day = {"date": "2026-10-23", "items": [{"id": "a", "payee": "A", "account": "Visa"}, {"id": "b", "payee": "B"}]}
        m = FinanceModals(panel_getter=lambda: panel)
        self.assertIsNotNone(m.open_edit({"date": "x", "items": []}))
        self.assertIsNone(m.open_edit(day))
        self.assertEqual(m.edit_stage, "day_menu")
        m.handle_key(ord("1"))
        res = m.handle_key(10)
        self.assertEqual((m.edit_stage, m.tx_id, m.account_idx, res.select_item), ("item", "a", 1, 0))

    def test_render_returns_box_inside_the_screen(self):
        m = FinanceModals(panel_getter=MagicMock)
        self.assertIsNone(m.render(95, 46))
        m.open_add()
        ov = m.render(95, 46)
        self.assertLessEqual(ov.left + ov.width, 95)
        self.assertTrue(all(theme.line_width(l) == ov.width for l in ov.lines))
        self.assertTrue(m.keys())


class TestViews(unittest.TestCase):
    def test_views_fit_the_body(self):
        home = build_home(HomeData(emails=[{"badge": "URGENT", "sender": "S", "subject": "Hi", "date": "Oct 1", "snippet": "x"}]), {}, 94, 40)
        self.assertIn(("email", 0), home.rows.values())  # the new Home has no header row above the list
        for frame in (home, build_memory(MemoryData({"total_memories": 3, "by_tier": {}, "by_category": {"a": 3}}), {}, 94, 40),
                      build_guard(GuardData({"trash_substitutions": 2}), {}, 94, 40)):
            self.assertLessEqual(len(frame.lines), 40)
            self.assertTrue(all(theme.line_width(l) <= 94 for l in frame.lines))

    def test_unmeasured_gate_latency_is_hidden(self):
        home = build_home(HomeData(guard_status={"router_latency_ms": None, "protected_paths": []}), {}, 94, 40)
        self.assertNotIn("ModernBERT Gate", "\n".join(text(l) for l in home.lines))


class TestViewProtocol(unittest.TestCase):
    def test_each_view_module_has_the_protocol(self):
        from telemachus.cockpit import guard_view, home_view, memory_view

        for mod in (home_view, memory_view, guard_view):
            for name in ("load", "build", "handle_key", "handle_click", "DATA_CLASS"):
                self.assertTrue(hasattr(mod, name), f"{mod.__name__}.{name}")
            frame = mod.build(mod.DATA_CLASS(), {}, 94, 40)
            self.assertIsInstance(frame, shell.Frame)

    def test_money_keymap(self):
        from telemachus.cockpit.finance_view import money_action

        self.assertEqual(money_action("daily", ord("v")), "bills")
        self.assertEqual(money_action("bills", 27), "back")
        self.assertEqual(money_action("daily", 10), "open_day")
        self.assertEqual(money_action("daily", ord("d")), "delete_hint")
        self.assertEqual(money_action("day", ord("d")), "delete")
        self.assertIsNone(money_action("math", ord("a")))


class TestCardSummaryStub(unittest.TestCase):
    def test_zero_summary_shape(self):
        with tempfile.TemporaryDirectory() as d:
            from telemachus.finance.store import FinanceStore

            panel = FinanceTuiPanel(store=FinanceStore(root_dir=Path(d) / "fin"))
            card = panel.get_card_summary()
        self.assertEqual(set(card), {"owed_cents", "charged_since_last_payment_cents", "paid_this_month_cents",
                                     "last_payment", "due_day", "next_due_date", "status", "due_before_payday"})
        self.assertEqual(card["status"], "clear")


class TestShellKeys(unittest.TestCase):
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

    def test_number_keys_use_the_new_tab_order(self):
        for key, tab in (("2", "money"), ("3", "memory"), ("4", "guard"), ("1", "home")):
            self.tui.handle_key(ord(key))
            self.assertEqual(self.tui.current_tab, tab)

    def test_tab_and_shift_tab_cycle(self):
        self.tui.handle_key(9)
        self.assertEqual(self.tui.current_tab, "money")
        self.tui.handle_key(curses.KEY_BTAB)
        self.tui.handle_key(curses.KEY_BTAB)
        self.assertEqual(self.tui.current_tab, "guard")

    def test_question_mark_toggles_help_and_q_closes_it_first(self):
        self.tui.handle_key(ord("?"))
        self.assertTrue(self.tui.help_open)
        self.assertTrue(self.tui.handle_key(ord("q")))  # closes help, does not quit
        self.assertFalse(self.tui.help_open)
        self.assertFalse(self.tui.handle_key(ord("q")))

    @patch("curses.color_pair", return_value=0)
    def test_toast_stays_on_the_tab_that_showed_it(self, _):
        def drawn():
            self.stdscr.reset_mock()
            self.tui.render()
            return "".join(str(c[0][2]) for c in self.stdscr.addstr.call_args_list if len(c[0]) >= 3)

        self.tui.current_tab = "money"
        self.tui.set_notification("Dropped 2 unsaved lines.", "warn")
        self.assertIn("Dropped 2 unsaved lines.", drawn())
        self.tui.handle_key(ord("3"))
        self.assertNotIn("Dropped 2 unsaved lines.", drawn())
        self.tui.handle_key(ord("2"))
        self.assertNotIn("Dropped 2 unsaved lines.", drawn())  # it does not come back

    def test_old_numeric_tab_ids_still_work(self):
        self.tui.current_tab = 4  # old Finances number
        self.assertEqual(self.tui.current_tab, "money")

    @patch("curses.color_pair", return_value=0)
    def test_full_render_of_every_tab(self, _):
        for tab in shell.TAB_IDS:
            self.tui.current_tab = tab
            self.stdscr.reset_mock()
            self.tui.render()
            drawn = "".join(str(c[0][2]) for c in self.stdscr.addstr.call_args_list if len(c[0]) >= 3)
            self.assertIn("telemachus", drawn)
            self.assertIn("? keys  q quit", drawn)
        self.tui.help_open = True
        self.tui.render()
        self.tui.help_open = False
        self.tui.current_tab = "money"
        self.tui.open_add_modal()
        self.tui.render()
        self.assertTrue(self.tui.is_adding_finances)


class TestMouse(TestShellKeys):
    @patch("curses.color_pair", return_value=0)
    def test_click_on_tab_name_and_email_row(self, _):
        self.tui.view_data["home"] = HomeData(
            emails=[{"badge": "INFO", "sender": "S", "subject": f"m{i}", "date": "", "snippet": ""} for i in range(3)])
        self.tui.render()
        guard_x = next(s for s, _, t in self.tui._tab_spans if t == "guard")
        with patch("curses.getmouse", return_value=(0, guard_x + 2, shell.TAB_ROW, 0, curses.BUTTON1_PRESSED)):
            self.tui.handle_mouse()
        self.assertEqual(self.tui.current_tab, "guard")
        self.tui.current_tab = "home"
        self.tui.render()
        row = next(y for y, target in self.tui._body_targets.items() if target == ("email", 2))
        with patch("curses.getmouse", return_value=(0, 10, row, 0, curses.BUTTON1_RELEASED)):
            self.tui.handle_mouse()
        self.assertEqual(self.tui.view_state["home"].get("sel", 0), 0)  # a release alone does nothing
        with patch("curses.getmouse", return_value=(0, 10, row, 0, curses.BUTTON1_PRESSED)):
            self.tui.handle_mouse()
        self.assertEqual(self.tui.view_state["home"]["sel"], 2)
        self.tui.handle_key(ord("k"))
        self.assertEqual(self.tui.view_state["home"]["sel"], 1)


if __name__ == "__main__":
    unittest.main()
