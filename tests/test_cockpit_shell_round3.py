"""Shell / tmux fixes, round 3: stable header fields while LM Studio works, pinned 256-color text and
selection colors, a damaged or locked money ledger, ctrl-C as cancel, and the launcher pane that
keeps an error on screen. Synthetic data only."""
from __future__ import annotations

import curses
import os
from pathlib import Path
import shlex
import signal
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from telemachus.cockpit import launcher as L, shell, theme
from telemachus.cockpit import tui as tui_mod
from telemachus.cockpit.telemetry import CockpitTelemetry, HeaderInfo
from telemachus.cockpit.tui import CTRL_C, CTRL_C_TOAST, CockpitTUI

try:
    from tests.cockpit_isolation import isolate_module
except ImportError:
    from cockpit_isolation import isolate_module

# CockpitTUI() opens the finance ledger and memory vault: keep them in a temp dir, never Sam's.
setUpModule, tearDownModule = isolate_module()

MODEL = "qwen3.5-9b-instruct-mlx"


def text(line) -> str:
    return theme.line_text(line)


def info(lm: str, **kw) -> HeaderInfo:
    base = dict(model=MODEL, ctx_used=44, ctx_max=100, ram_used_gb=20.0, ram_total_gb=24, think_tokens=1200, tps=42.0)
    base.update(kw)
    return HeaderInfo(lm=lm, **base)


BUSY = [("reading", {"elapsed_s": 76}), ("reading", {"elapsed_s": 6000}), ("writing", {"tokens": 7}),
        ("writing", {"tokens": 12345}), ("writing", {"tokens": 999_000, "tps": 999.0})]


class TestHeaderKeepsFields(unittest.TestCase):
    def header(self, i: HeaderInfo, width: int) -> str:
        line = shell.build_header(i, width, 0.0, "22:20")
        self.assertLessEqual(theme.line_width(line), width)
        return text(line)

    def test_ram_stays_while_lm_studio_works_at_95_columns(self):
        ready = self.header(info("ready"), 94)
        self.assertIn("ram 20.0/24G", ready)
        for lm, extra in BUSY:
            out = self.header(info(lm, **extra), 94)
            self.assertIn("ram 20.0/24G", out, out)
            self.assertIn("ctx 44%", out, out)
            self.assertTrue(out.endswith("22:20"), out)

    def test_every_field_that_fits_beside_ready_stays(self):
        for cols in range(40, 141):
            ready = self.header(info("ready"), cols - 1)
            for lm, extra in BUSY:
                out = self.header(info(lm, **extra), cols - 1)
                for word in ("ctx", "ram"):
                    if word in ready:
                        self.assertIn(word, out, (cols, lm, extra, out))

    def test_short_name_has_a_fixed_width_while_tokens_grow(self):
        for cols in (60, 70, 80, 95):
            starts = set()
            ctx_cols = set()
            for lm, extra in BUSY:
                out = self.header(info(lm, **extra), cols - 1)
                starts.add(next(i for i, c in enumerate(out) if c in theme.SPINNER))
                if "ctx" in out:
                    ctx_cols.add(out.index("ctx"))
            self.assertEqual(len(starts), 1, (cols, starts))
            self.assertLessEqual(len(ctx_cols), 1, (cols, ctx_cols))

    def test_full_name_when_there_is_room(self):
        out = self.header(info("writing", tokens=7), 139)
        self.assertIn(MODEL + " ", out)
        self.assertIn("ram", out)


class TestPinnedColors(unittest.TestCase):
    def test_256_text_is_not_cyan_and_sel_is_navy(self):
        t = theme.Theme()
        t._setup_256()
        self.assertEqual(t._color_of["cyan"], 117)
        self.assertEqual(t._color_of["text"], 189)  # #d7d7ff lavender white, not pale blue 153
        self.assertEqual(t._color_of["sel"], 17)  # navy, not gray 237
        self.assertNotEqual(t._color_of["sel"], t._color_of["card"])

    def test_8_color_selected_row_uses_bright_green(self):
        t = theme.Theme()
        t._setup_basic(16)
        t._default_bg = -1
        self.assertEqual(t.color_numbers("green", "sel"), (10, curses.COLOR_BLUE))
        self.assertEqual(t.color_numbers("green", ""), (curses.COLOR_GREEN, -1))

    def test_8_color_terminal_bolds_green_on_the_selected_row(self):
        t = theme.Theme()
        t._setup_basic(8)
        t._default_bg = -1
        with patch("curses.init_pair"), patch("curses.color_pair", return_value=0):
            t._max_pairs = 64
            self.assertTrue(t.attr("green@sel") & curses.A_BOLD)
            self.assertFalse(t.attr("green") & curses.A_BOLD)


class _TuiBase(unittest.TestCase):
    def make_tui(self) -> CockpitTUI:
        self.stdscr = MagicMock()
        self.stdscr.getmaxyx.return_value = (46, 95)
        with patch.object(CockpitTUI, "_init_curses"), patch("curses.color_pair", return_value=0):
            tui = CockpitTUI(self.stdscr)
        tui.telemetry = CockpitTelemetry(lm_studio_url="http://127.0.0.1:9/v1/models",
                                         status_path=Path(self.tmp.name) / "s.json",
                                         opencode_db=Path(self.tmp.name) / "none.db")
        return tui

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)


class TestBrokenLedger(_TuiBase):
    def setUp(self):
        super().setUp()
        self.fin = Path(self.tmp.name) / "fin"
        self.fin.mkdir()
        (self.fin / "finances.sqlite3").write_bytes(os.urandom(8000))
        env = patch.dict(os.environ, {"TELEMACHUS_FINANCE_DIR": str(self.fin),
                                      "TELEMACHUS_SETUP_YAML": str(self.fin / "setup.yaml")})
        env.start()
        self.addCleanup(env.stop)

    def test_damaged_file_does_not_crash_and_other_tabs_work(self):
        tui = self.make_tui()
        self.assertIsNone(tui.finance_panel)
        self.assertIn("not a database", tui.finance_open_error)
        self.assertEqual(tui.view_data["home"].money, {"available": False, "error": True})
        tui.current_tab = "money"
        lay = shell.layout(46, 95)
        body = "\n".join(text(line) for line in tui._build_body(lay).lines)
        self.assertIn("Could not open the money ledger.", body)
        self.assertIn("not a database", body)
        tui.handle_key(ord("a"))
        self.assertFalse(tui._get_modals().active)
        self.assertIn("not open", tui.notification)
        for tab in ("1", "3", "4"):
            tui.handle_key(ord(tab))
            with patch("curses.color_pair", return_value=0):
                tui.render()

    def test_r_opens_the_ledger_once_it_is_fixed(self):
        tui = self.make_tui()
        tui.handle_key(ord("r"))
        self.assertIn("still cannot", tui.notification)
        (self.fin / "finances.sqlite3").unlink()
        tui.current_tab = "money"
        tui.handle_key(ord("r"))
        self.assertIsNone(tui.finance_open_error)
        self.assertIsNotNone(tui.finance_panel)
        self.assertEqual(tui.notification, "Money ledger opened.")

    def test_locked_file_does_not_crash(self):
        locked = sqlite3.OperationalError("database is locked")
        with patch.object(tui_mod, "FinanceTuiPanel", side_effect=locked):
            tui = self.make_tui()
        self.assertEqual(tui.finance_open_error, "database is locked")

    def test_retry_waits_between_tries(self):
        tui = self.make_tui()
        with patch.object(tui_mod, "open_finance_panel", return_value=(None, "x")) as op:
            tui.refresh_data()
            op.assert_not_called()  # the start just failed: no 5 s wait on every refresh
            tui._finance_retry_at = 0.0
            tui.refresh_data()
            op.assert_called_once()


class TestCtrlC(_TuiBase):
    def test_ctrl_c_never_quits_outside_a_box(self):
        tui = self.make_tui()
        for tab in ("home", "money", "memory", "guard"):
            tui.current_tab = tab
            self.assertTrue(tui.handle_key(CTRL_C))
            self.assertEqual(tui.notification, CTRL_C_TOAST)

    def test_ctrl_c_in_the_add_box_asks_before_dropping_lines(self):
        tui = self.make_tui()
        tui.current_tab = "money"
        tui.handle_key(ord("a"))
        modals = tui._get_modals()
        self.assertEqual(modals.kind, "add")
        for ch in "lunch 9.99":
            tui.handle_key(ord(ch))
        tui.handle_key(CTRL_C)
        self.assertTrue(modals.active, "the first ctrl-C must only arm the 'esc again' confirm")
        self.assertEqual(modals.confirm, "cancel")
        tui.handle_key(CTRL_C)
        self.assertFalse(modals.active)

    def test_ctrl_c_closes_help(self):
        tui = self.make_tui()
        tui.handle_key(ord("?"))
        tui.handle_key(CTRL_C)
        self.assertFalse(tui.help_open)

    def test_sigint_becomes_a_key_in_the_loop(self):
        tui = self.make_tui()
        tui._sigint = True
        self.stdscr.getch.side_effect = [-1, ord("q")]
        with patch("curses.color_pair", return_value=0):
            tui._loop()  # returns on q
        self.assertEqual(tui.notification, CTRL_C_TOAST)

    def test_run_installs_and_restores_the_handler(self):
        tui = self.make_tui()
        before = signal.getsignal(signal.SIGINT)
        seen = []
        with patch.object(tui, "_loop", side_effect=lambda: seen.append(signal.getsignal(signal.SIGINT))):
            tui.run()
        self.assertEqual(seen, [tui._on_sigint])
        self.assertEqual(signal.getsignal(signal.SIGINT), before)


class TestLauncherPaneKeepsErrors(unittest.TestCase):
    def run_pane(self, python: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            launcher = L.CockpitLauncher(python=python)
            launcher.project_root = Path(tmp)
            cmd = launcher.dashboard_command()
            return subprocess.run(["sh", "-c", cmd], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)

    def test_error_stays_on_screen(self):
        res = self.run_pane("/usr/bin/false")
        self.assertIn("dashboard stopped (exit 1)", res.stdout)
        self.assertIn("Press enter", res.stdout)

    def test_q_quit_closes_the_pane_quietly(self):
        res = self.run_pane("/usr/bin/true")
        self.assertEqual(res.stdout, "")
        self.assertEqual(res.returncode, 0)

    def test_command_is_valid_sh(self):
        cmd = L.CockpitLauncher(python="/venv/bin/python").dashboard_command()
        subprocess.run(["sh", "-n", "-c", cmd], check=True)
        self.assertIn(shlex.quote("/venv/bin/python") + " -m telemachus.cockpit dashboard", cmd)


if __name__ == "__main__":
    unittest.main()
