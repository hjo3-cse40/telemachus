"""Shell / tmux fixes: sandbox data paths, wheel down on mouse-version-1 ncurses, terminals that
cannot hide the cursor, and the env vars the launcher forwards into tmux panes. Synthetic data only."""
from __future__ import annotations

import curses
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from telemachus.cockpit import launcher as L
from telemachus.cockpit import tui as tui_mod
from telemachus.cockpit.telemetry import CockpitTelemetry
from telemachus.cockpit.tui import CockpitTUI

try:
    from tests.cockpit_isolation import isolate_module
except ImportError:
    from cockpit_isolation import isolate_module
setUpModule, tearDownModule = isolate_module()


def _real_home() -> str:
    import pwd

    return pwd.getpwuid(os.getuid()).pw_dir


class TestHomeOverridden(unittest.TestCase):
    def test_real_home_is_not_overridden(self):
        self.assertFalse(L.home_overridden({"HOME": _real_home()}))

    def test_other_home_is_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(L.home_overridden({"HOME": tmp}))

    def test_unset_home_is_not_overridden(self):
        self.assertFalse(L.home_overridden({}))


class TestSandboxDataPaths(unittest.TestCase):
    """A sandbox HOME must never fall back to the repo's data/ (the real mail index and trash log)."""

    def test_sandbox_home_without_overrides_uses_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp}
            with patch.dict(os.environ, env):
                os.environ.pop("TELEMACHUS_EMAIL_DB", None)
                os.environ.pop("TELEMACHUS_TRASH_AUDIT", None)
                mail = tui_mod._data_path("TELEMACHUS_EMAIL_DB", "email_index.sqlite3")
                trash = tui_mod._data_path("TELEMACHUS_TRASH_AUDIT", "trash_audit.jsonl")
            self.assertEqual(mail, Path(tmp) / ".telemachus" / "data" / "email_index.sqlite3")
            self.assertEqual(trash, Path(tmp) / ".telemachus" / "data" / "trash_audit.jsonl")

    def test_env_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp, "TELEMACHUS_EMAIL_DB": "/x/mail.sqlite3"}):
                self.assertEqual(tui_mod._data_path("TELEMACHUS_EMAIL_DB", "email_index.sqlite3"),
                                 Path("/x/mail.sqlite3"))

    def test_real_home_keeps_the_panel_default(self):
        with patch.dict(os.environ, {"HOME": _real_home()}):
            os.environ.pop("TELEMACHUS_EMAIL_DB", None)
            self.assertIsNone(tui_mod._data_path("TELEMACHUS_EMAIL_DB", "email_index.sqlite3"))

    @patch("curses.color_pair", return_value=0)
    def test_tui_panels_use_the_sandbox(self, *_):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}):
                os.environ.pop("TELEMACHUS_EMAIL_DB", None)
                os.environ.pop("TELEMACHUS_TRASH_AUDIT", None)
                stdscr = MagicMock()
                stdscr.getmaxyx.return_value = (40, 95)
                with patch.object(CockpitTUI, "refresh_data"), patch.object(CockpitTUI, "_init_curses"):
                    tui = CockpitTUI(stdscr)
            repo_data = Path(__file__).resolve().parents[1] / "data"
            for path in (tui.email_panel.db_path, tui.safety_panel.audit_log):
                self.assertTrue(str(path).startswith(tmp), path)
                self.assertNotEqual(path.parent, repo_data)


class _TuiCase(unittest.TestCase):
    @patch("curses.color_pair", return_value=0)
    def setUp(self, *_):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stdscr = MagicMock()
        self.stdscr.getmaxyx.return_value = (40, 95)
        with patch.object(CockpitTUI, "refresh_data"), patch.object(CockpitTUI, "_init_curses"):
            self.tui = CockpitTUI(self.stdscr)
        self.tui.telemetry = CockpitTelemetry(lm_studio_url="http://127.0.0.1:9/v1/models",
                                              status_path=Path(self.tmp.name) / "s.json",
                                              opencode_db=Path(self.tmp.name) / "none.db")


class TestWheelDown(_TuiCase):
    """macOS system ncurses 6.0 (mouse version 1) has no button 5: getmouse() fails on wheel down."""

    def test_getmouse_error_is_wheel_down_on_mouse_v1(self):
        with patch.object(tui_mod, "HAS_BUTTON5", False), \
                patch("curses.getmouse", side_effect=curses.error("getmouse() returned ERR")), \
                patch.object(self.tui, "_scroll") as scroll:
            self.tui.handle_mouse()
        scroll.assert_called_once_with(1)

    def test_getmouse_error_scrolls_the_help_card(self):
        self.tui.help_open = True
        with patch.object(tui_mod, "HAS_BUTTON5", False), \
                patch("curses.getmouse", side_effect=curses.error("ERR")), \
                patch.object(self.tui, "_scroll_help") as scroll_help, \
                patch.object(self.tui, "_scroll") as scroll:
            self.tui.handle_mouse()
        scroll_help.assert_called_once_with(1)
        scroll.assert_not_called()

    def test_getmouse_error_is_ignored_with_button5(self):
        with patch.object(tui_mod, "HAS_BUTTON5", True), \
                patch("curses.getmouse", side_effect=curses.error("ERR")), \
                patch.object(self.tui, "_scroll") as scroll:
            self.tui.handle_mouse()
        scroll.assert_not_called()

    def test_wheel_up_still_scrolls_up(self):
        with patch("curses.getmouse", return_value=(0, 10, 10, 0, curses.BUTTON4_PRESSED)), \
                patch.object(self.tui, "_scroll") as scroll:
            self.tui.handle_mouse()
        scroll.assert_called_once_with(-1)

    def test_scroll_down_mask_never_matches_a_v1_button4_bit(self):
        # The old fallback (1 << 21) is BUTTON4_DOUBLE_CLICKED on mouse version 1.
        if not tui_mod.HAS_BUTTON5:
            self.assertEqual(tui_mod.SCROLL_DOWN, 0)


class TestCursorHide(_TuiCase):
    def test_init_survives_a_terminal_without_civis(self):
        with patch("curses.curs_set", side_effect=curses.error("curs_set() returned ERR")), \
                patch("curses.set_escdelay"), patch("curses.mousemask"), patch("curses.mouseinterval"), \
                patch.object(tui_mod, "init_colors"), patch.object(CockpitTUI, "_disable_flow_control"):
            self.tui._init_curses()  # must not raise
        self.stdscr.keypad.assert_called_with(True)


class TestForwardedEnv(unittest.TestCase):
    def test_memory_dir_and_guard_log_are_forwarded(self):
        env = {"TELEMACHUS_MEMORY_DIR": "/x/mem", "TELEMACHUS_GUARD_LOG": "/x/guard.jsonl",
               "HOME": _real_home()}
        with patch.dict(os.environ, env, clear=True):
            prefix = L.forwarded_env_prefix()
        self.assertIn("TELEMACHUS_MEMORY_DIR=/x/mem", prefix)
        self.assertIn("TELEMACHUS_GUARD_LOG=/x/guard.jsonl", prefix)
        self.assertNotIn("HOME=", prefix)  # the real HOME is left to tmux

    def test_sandbox_home_is_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": tmp}, clear=True):
                prefix = L.forwarded_env_prefix()
                launcher = L.CockpitLauncher(settings_path=Path(tmp) / "cockpit.json", python="/venv/bin/python")
                dash = launcher.dashboard_command()
        self.assertTrue(prefix.startswith("env HOME="), prefix)
        self.assertIn(tmp, prefix)
        self.assertIn(prefix, dash)


if __name__ == "__main__":
    unittest.main()
