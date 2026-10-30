"""Telemachus Cockpit TUI: the curses shell.

This file only does input, state, layout and drawing. What each screen shows comes from pure view
modules that return lines of (text, style) segments:

    home_view.build_home        Home tab
    finance_view.build_*        Money tab (overview, day, math, bills)
    memory_view.build_memory    Memory tab
    guard_view.build_guard      Guard tab
    finance_modals.FinanceModals  Add / Edit boxes (drawing and keys)
    shell.*                     header, tabs, footer, toast, help overlay
    theme.*                     colors, styles, glyphs, line helpers

Screen rows: 0 header, 1 tabs, 2 underline, 3 blank, body, toast row, footer row (see shell.py).
"""
from __future__ import annotations

import curses
from datetime import datetime
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from telemachus.cockpit import art_city, finance_view, guard_view, home_view, memory_view, shell, tmux_helper
from telemachus.cockpit import theme as theme_mod
from telemachus.cockpit.email_panel import EmailTriagePanel
from telemachus.cockpit.finance_modals import FinanceModals, ModalResult, selection_after_delete
from telemachus.cockpit.finance_panel import EDIT_CATEGORIES, FinanceTuiPanel  # noqa: F401 (re-export)
from telemachus.cockpit.finance_view import (
    FinanceViewData,
    Rendered,
    build_bills,
    build_day,
    build_math,
    build_overview,
    money_action,
    money_keys,
)
from telemachus.cockpit.launcher import home_overridden
from telemachus.cockpit.safety_memory_panel import SafetyMemoryPanel
from telemachus.cockpit.shell import Frame, Layout, Overlay, Panels
from telemachus.cockpit.telemetry import CockpitTelemetry, HeaderInfo
from telemachus.cockpit.theme import THEME, GLYPHS, Line, attr, cell_width, fit, init_colors, section

_VS16 = "️"

# Tabs drawn by a view module with the load / build / handle_key / handle_click protocol.
# Money is special: its state lives here (older tests drive it directly).
VIEWS = {"home": home_view, "memory": memory_view, "guard": guard_view}

TAB_BY_KEY = {ord("1"): "home", ord("2"): "money", ord("3"): "memory", ord("4"): "guard"}
_LEGACY_TAB_NUMBERS = {1: "home", 2: "memory", 3: "guard", 4: "money"}  # old order: Daily Ops, Memory, Guard, Finances
KEY_BTAB = getattr(curses, "KEY_BTAB", 353)
# ncurses built with mouse version 1 (the macOS system ncurses 6.0) has no button 5: a wheel-down
# report reaches the app as KEY_MOUSE, but getmouse() then fails. There that failure means wheel down.
HAS_BUTTON5 = hasattr(curses, "BUTTON5_PRESSED")
SCROLL_DOWN = getattr(curses, "BUTTON5_PRESSED", 0)
CLICK_MASK = curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED | getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
ANIM_INTERVAL = 0.125  # header spinner, at most 8 frames a second
IDLE_HEADER_INTERVAL = 0.5
DATA_REFRESH_S = 10.0
TOAST_S = 3.0
CTRL_C = 3
CTRL_C_TOAST = "Press q to quit. Ctrl-C does not close the cockpit."
TRIAGE_PROMPT = "Triage my unread emails and summarize urgent items using telemachus-email"
# Shell names, to tell a shell prompt from a program running in the OpenCode pane.
SHELL_COMMANDS = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "nu", "elvish", "xonsh"})
_PANE_ID = re.compile(r"^%\d+$")
# While the money ledger cannot be opened, the TUI tries again this often (and on r). One try can
# wait about 5 s on a locked file, so it is not done on every data refresh.
FINANCE_RETRY_S = 30.0


def open_finance_panel() -> Tuple[Optional[FinanceTuiPanel], Optional[str]]:
    """(panel, None), or (None, reason) when the money ledger cannot be opened: a damaged file,
    a file another program holds locked, or no permission. The other tabs keep working."""
    try:
        return FinanceTuiPanel(), None
    except Exception as e:  # sqlite3.DatabaseError, OperationalError, OSError, a bad setup file
        reason = " ".join(str(e).split()) or type(e).__name__
        return None, reason


class _SafeScreen:
    """Delegates to a curses window but strips the emoji presentation selector from text."""

    def __init__(self, win: Any):
        self._win = win

    def __getattr__(self, name: str) -> Any:
        return getattr(self._win, name)

    def addstr(self, *args: Any) -> Any:
        # Signatures: (text[, attr]) or (y, x, text[, attr])
        args_list = list(args)
        text_idx = 0 if isinstance(args_list[0], str) else 2
        args_list[text_idx] = args_list[text_idx].replace(_VS16, "")
        return self._win.addstr(*args_list)


def _env_path(name: str) -> Optional[Path]:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


# Without an env override, the email index and trash audit live in the repo's data/ folder,
# which HOME does not move. A run with a sandbox HOME reads $HOME/.telemachus/data instead,
# so a screenshot run never opens the real mail index or trash log.
SANDBOX_DATA_DIR = Path(".telemachus") / "data"


def _data_path(env_name: str, file_name: str) -> Optional[Path]:
    """Env override, else a file under the sandbox HOME, else None (the panel's own default)."""
    path = _env_path(env_name)
    if path is None and home_overridden():
        path = Path.home() / SANDBOX_DATA_DIR / file_name
    return path


def _modal_proxy(name: str) -> property:
    """Old CockpitTUI attribute names for modal state, now kept on the FinanceModals controller."""

    def getter(self: "CockpitTUI") -> Any:
        return getattr(self._get_modals(), name)

    def setter(self: "CockpitTUI", value: Any) -> None:
        setattr(self._get_modals(), name, value)

    return property(getter, setter)


class CockpitTUI:
    """Curses-driven Terminal Cockpit ("Quiet" shell; Golden Hour or Tokyo Night theme)."""

    # Old attribute names (tests and older callers) map onto the FinanceModals controller.
    modal_stage = _modal_proxy("add_stage")
    modal_date_buffer = _modal_proxy("date_buffer")
    modal_line_buffer = _modal_proxy("line_buffer")
    modal_batch_lines = _modal_proxy("batch_lines")
    editing_stage = _modal_proxy("edit_stage")
    editing_tx_id = _modal_proxy("tx_id")
    editing_date = _modal_proxy("edit_date")
    editing_orig_text = _modal_proxy("orig_text")
    editing_buffer = _modal_proxy("edit_buffer")
    editing_account_idx = _modal_proxy("account_idx")
    editing_category_idx = _modal_proxy("category_idx")
    editing_current_category = _modal_proxy("current_category")

    def __init__(self, stdscr: Any):
        self.stdscr = _SafeScreen(stdscr)
        # Env overrides point the panels at other files (synthetic data for screenshots and tests).
        self.telemetry = CockpitTelemetry(lm_studio_url=os.environ.get("TELEMACHUS_LMSTUDIO_URL") or "http://127.0.0.1:1234/v1/models")
        self.email_panel = EmailTriagePanel(db_path=_data_path("TELEMACHUS_EMAIL_DB", "email_index.sqlite3"))
        self.safety_panel = SafetyMemoryPanel(audit_log=_data_path("TELEMACHUS_TRASH_AUDIT", "trash_audit.jsonl"))
        self.finance_panel, self.finance_open_error = open_finance_panel()
        self._finance_retry_at = time.time() + FINANCE_RETRY_S

        self.current_tab = "home"
        self.help_open = False
        self.help_scroll = 0
        self._help_scroll_max = 0
        self.notification = ""
        self.notification_kind = "info"
        self.notification_expiry = 0.0

        # Golden Hour city: sky mode (s key, saved in cockpit.json) and where the art sits now.
        self.sky_mode = self._load_sky()
        self._art_t0 = time.time()
        self._art_rect: Optional[Tuple[int, int, int, int]] = None  # (y, x, rows, cols) on screen
        self._chrome_w: Optional[int] = None  # header / tabs / toast / footer width beside a window

        # Per-tab data (from each view's load()) and per-tab UI state (selection, scroll, ...).
        self.view_data: Dict[str, Any] = {}
        self.view_state: Dict[str, Dict[str, Any]] = {tab: {} for tab in VIEWS}

        # Money tab state
        self.finance_days: List[Dict[str, Any]] = []
        self.finance_overview: Any = None
        self.finance_cashflow: Any = None
        self.finance_paycheck: Any = None
        self.finance_runway: Any = None
        self.finance_balances: Dict[str, Any] = {}
        self.finance_goals: List[Any] = []
        self.finance_bills_breakdown: Dict[str, Any] = {}
        self.finance_card: Optional[Dict[str, Any]] = None
        self.finance_has_data = True
        self.finances_view_mode: str = "daily"  # "daily" (overview), "day", "math" or "bills"
        self.finances_focus: str = "days"  # "days" or "items"
        self.selected_day_idx = 0
        self.day_scroll_offset = 0
        self.selected_item_idx = 0
        self.selected_bill_idx = 0
        self.bills_scroll_offset = 0
        self.mask_amounts = False
        self._finance_day_rows: Dict[int, int] = {}  # screen row -> day index (mouse)
        self._finance_item_rows: Dict[int, int] = {}  # screen row -> item index
        self._finance_bill_rows: Dict[int, int] = {}  # screen row -> bill index

        self._modals = FinanceModals(panel_getter=lambda: self.finance_panel, day_getter=self._current_day,
                                     mask_getter=lambda: bool(getattr(self, "mask_amounts", False)))
        self._tab_spans: List[Tuple[int, int, str]] = []
        self._body_targets: Dict[int, Tuple[str, int]] = {}
        self._frame_keys: List[shell.KeyGroup] = []
        self._last_header: Optional[Line] = None
        self._header_active = False

        self._last_size = self.stdscr.getmaxyx()
        self._settle_repaint_at = 0.0
        self._debug_path = os.environ.get("TELEMACHUS_TUI_DEBUG")

        self._init_curses()
        self.refresh_data()

    # ------------------------------------------------------------------
    # Tab id (accepts the old numbers 1-4 too)
    # ------------------------------------------------------------------

    @property
    def current_tab(self) -> str:
        return self.__dict__.get("_current_tab", "home")

    @current_tab.setter
    def current_tab(self, value: Any) -> None:
        if isinstance(value, int):
            value = _LEGACY_TAB_NUMBERS.get(value, "home")
        self.__dict__["_current_tab"] = value if value in shell.TAB_IDS else "home"

    # ------------------------------------------------------------------
    # Modal controller and old modal API
    # ------------------------------------------------------------------

    def _get_modals(self) -> FinanceModals:
        m = self.__dict__.get("_modals")
        if m is None:
            m = FinanceModals(panel_getter=lambda: self.finance_panel, day_getter=self._current_day,
                              mask_getter=lambda: bool(getattr(self, "mask_amounts", False)))
            self.__dict__["_modals"] = m
        return m

    @property
    def is_adding_finances(self) -> bool:
        return self._get_modals().kind == "add"

    @is_adding_finances.setter
    def is_adding_finances(self, value: bool) -> None:
        m = self._get_modals()
        if value:
            m.kind = "add"
        elif m.kind == "add":
            m.kind = None

    @property
    def is_editing_finances(self) -> bool:
        return self._get_modals().kind == "edit"

    @is_editing_finances.setter
    def is_editing_finances(self, value: bool) -> None:
        m = self._get_modals()
        if value:
            m.kind = "edit"
            if m.day is None:
                m.day = self._current_day()
        elif m.kind == "edit":
            m.kind = None

    def _current_day(self) -> Optional[Dict[str, Any]]:
        days = getattr(self, "finance_days", []) or []
        idx = getattr(self, "selected_day_idx", 0)
        return days[idx] if 0 <= idx < len(days) else None

    def open_add_modal(self) -> None:
        self._get_modals().open_add((self._current_day() or {}).get("date") if self.finances_view_mode == "day" else None)

    def open_edit_modal(self) -> None:
        day = self._current_day()
        if day is None:
            self.set_notification("No day selected to edit.", "warn")
            return
        items = day.get("items", [])
        item_idx = self.selected_item_idx if (self.finances_focus == "items" or len(items) == 1) else None
        err = self._get_modals().open_edit(day, item_idx)
        if err:
            self.set_notification(err, "warn")

    def _apply_modal_result(self, res: ModalResult) -> None:
        if res.changed:
            self.refresh_data()  # first: select_day / select_item are indexes in the fresh day list
        if res.select_item is not None:
            self.selected_item_idx = res.select_item
        if res.select_day is not None:
            self.selected_day_idx = res.select_day
        if res.day_gone:
            self._set_finance_mode("daily")
        if res.toast:
            self.set_notification(res.toast, res.toast_kind)

    # ------------------------------------------------------------------
    # Setup, data, notifications
    # ------------------------------------------------------------------

    def _hard_reset_screen(self) -> None:
        """Re-sync curses with the real terminal after a burst of resizes."""
        try:
            sys.stdout.write("\x1b[r")  # reset any stale scroll region
            sys.stdout.flush()
            curses.endwin()
            self.stdscr.refresh()  # re-enters curses mode with a fresh full repaint
            self.stdscr.clearok(True)
            self.stdscr.redrawwin()
        except curses.error:
            pass

    def _debug(self, msg: str) -> None:
        """Opt-in frame log: set TELEMACHUS_TUI_DEBUG=/path/to/log to record redraw causes."""
        if not getattr(self, "_debug_path", None):
            return
        try:
            with open(self._debug_path, "a", encoding="utf-8") as f:
                f.write(f"{time.time():.3f} {msg}\n")
        except OSError:
            pass

    def _init_curses(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass  # the terminal cannot hide the cursor (no civis, e.g. TERM=vt100)
        try:
            curses.set_escdelay(25)
        except Exception:
            pass
        self.stdscr.timeout(50)  # wait up to 50 ms for input: yields the CPU when idle
        self.stdscr.keypad(True)
        try:
            # No REPORT_MOUSE_POSITION: motion events would fire on every cursor move
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        except Exception:
            pass
        init_colors()
        self._disable_flow_control()

    @staticmethod
    def _disable_flow_control() -> None:
        """Turns off XON/XOFF so ctrl-S reaches the app (it saves the Add box). endwin restores the tty."""
        try:
            import termios

            fd = sys.stdin.fileno()
            if not os.isatty(fd):
                return
            a = termios.tcgetattr(fd)
            a[0] &= ~termios.IXON
            termios.tcsetattr(fd, termios.TCSANOW, a)
            curses.def_prog_mode()  # keep -ixon when a refresh after endwin (resize) restores the mode
        except Exception:
            pass

    def _panels(self) -> Panels:
        return Panels(
            email_panel=getattr(self, "email_panel", None),
            safety_panel=getattr(self, "safety_panel", None),
            finance_panel=getattr(self, "finance_panel", None),
            telemetry=getattr(self, "telemetry", None),
        )

    def _views(self) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
        """(view_data, view_state), created on first use (tests build the TUI without __init__)."""
        if "view_data" not in self.__dict__:
            self.view_data = {}
        if "view_state" not in self.__dict__:
            self.view_state = {tab: {} for tab in VIEWS}
        return self.view_data, self.view_state

    def _retry_finance_open(self, now_: bool) -> None:
        """Tries to open the money ledger again when it failed at start (now_, or every 30 s)."""
        if not getattr(self, "finance_open_error", None):
            return
        if not now_ and time.time() < getattr(self, "_finance_retry_at", 0.0):
            return
        self._finance_retry_at = time.time() + FINANCE_RETRY_S
        panel, err = open_finance_panel()
        if panel is not None:
            self.finance_panel, self.finance_open_error = panel, None
            self.set_notification("Money ledger opened.", "ok")
        else:
            self.finance_open_error = err

    def refresh_data(self, retry_finance: bool = False) -> None:
        """Reloads the latest state from SQLite, the memory vault and the system."""
        self._retry_finance_open(retry_finance)
        data, _ = self._views()
        panels = self._panels()
        for tab, module in VIEWS.items():
            try:
                data[tab] = module.load(panels)
            except Exception as e:
                self.set_notification(f"Refresh error ({tab}): {e}", "error")
        if getattr(self, "finance_open_error", None):
            home = data.get("home")
            if home is not None:
                home.money = {"available": False, "error": True}  # Home: "Could not read the finance vault."
            return  # nothing to read for Money; its screen shows the reason
        try:
            # Remember the selection by date and entry id: a newer day shifts every index.
            key = finance_view.selection_key(getattr(self, "finance_days", None) or [], self.selected_day_idx,
                                             self.selected_item_idx, self.finances_focus)
            self.finance_panel.refresh()
            self.finance_days = self.finance_panel.get_daily_ledger()
            self.finance_overview = self.finance_panel.get_monthly_overview()
            self.finance_cashflow = self.finance_panel.get_cash_flow_summary()
            self.finance_paycheck = self.finance_panel.get_paycheck_allocation()
            self.finance_runway = self.finance_panel.get_runway()
            self.finance_balances = self.finance_panel.get_balances()
            self.finance_goals = self.finance_panel.get_goals_progress()
            self.finance_bills_breakdown = self.finance_panel.get_monthly_bills_breakdown()
            get_card = getattr(self.finance_panel, "get_card_summary", None)
            self.finance_card = get_card() if callable(get_card) else None
            self.finance_has_data = home_view._has_finance_data(self.finance_panel)
            err = getattr(self.finance_panel, "get_setup_error", lambda: None)()
            self.finance_setup_error = err if isinstance(err, str) and err else None

            day_idx, item_idx = finance_view.find_selection(self.finance_days, key[0], key[1])
            if day_idx is not None:
                self.selected_day_idx = day_idx
                if item_idx is not None:
                    self.selected_item_idx = item_idx
            if self.finance_days and self.selected_day_idx >= len(self.finance_days):
                self.selected_day_idx = len(self.finance_days) - 1
        except Exception as e:
            self.set_notification(f"Refresh error: {e}", "error")

    def set_notification(self, msg: str, kind: str = "info", duration: float = TOAST_S) -> None:
        """Shows a toast (right side, row above the footer). kind: ok, warn, error, info."""
        self.notification = msg
        self.notification_kind = kind
        self.notification_expiry = time.time() + duration
        # A toast belongs to the tab that showed it: a tab change clears it.
        self.notification_tab = getattr(self, "current_tab", None)

    @staticmethod
    def opencode_pane(env: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], str]:
        """(pane id, "") of the OpenCode pane the launcher set up, or (None, reason).

        Only the pane id the launcher passed in TMUX_PANE_RIGHT counts, only inside tmux, and
        never this pane itself: typing into a guessed pane could run a command somewhere else,
        and typing into the cockpit's own pane would feed the keys back in as new key presses.
        """
        env = os.environ if env is None else env
        target = (env.get("TMUX_PANE_RIGHT") or "").strip()
        if not env.get("TMUX") or not target:
            return None, "OpenCode pane not found. Start the cockpit with the launcher."
        if not _PANE_ID.match(target) or target == (env.get("TMUX_PANE") or "").strip():
            return None, "OpenCode pane not found. Start the cockpit with the launcher."
        return target, ""

    @staticmethod
    def _prog(word: str) -> str:
        return os.path.basename((word or "").strip()).lstrip("-").lower()

    @classmethod
    def opencode_text(cls, prompt: str, pane_command: str, foreground: Sequence[str] = ()) -> Optional[str]:
        """What to type into the OpenCode pane, or None when something else runs there.

        foreground: command lines of the pane's foreground processes (ps). The launcher starts
        OpenCode under `sh -c`, which has no job control, so tmux names the shell as the pane's
        command even while the OpenCode TUI runs; the process list shows OpenCode itself.
        OpenCode TUI running: the plain prompt (it is a chat box). Shell prompt: `opencode run ...`.
        """
        progs = [cls._prog(w) for args in foreground for w in str(args).split()[:2]]
        if "opencode" in cls._prog(pane_command) or any("opencode" in p for p in progs):
            return prompt
        name = cls._prog(pane_command)
        others = [cls._prog(str(a).split()[0]) for a in foreground if str(a).split()]
        if name in SHELL_COMMANDS and all(p in SHELL_COMMANDS for p in others):
            return f"opencode run {shlex.quote(prompt)}"
        return None

    @staticmethod
    def _foreground_commands(tty: str) -> List[str]:
        """Command lines of the foreground processes on a terminal (empty when ps fails)."""
        if not tty:
            return []
        try:
            res = subprocess.run(["ps", "-t", os.path.basename(tty), "-o", "stat=,args="],
                                 capture_output=True, text=True, timeout=2)
        except Exception:
            return []
        out = []
        for line in res.stdout.splitlines():
            stat, _, args = line.strip().partition(" ")
            if "+" in stat and args.strip():
                out.append(args.strip())
        return out

    def trigger_opencode_action(self, action_prompt: str) -> None:
        """Types a task into the OpenCode pane next to the dashboard (launcher split only)."""
        target, why = self.opencode_pane()
        if target is None:
            self.set_notification(why, "warn")
            return
        try:
            res = subprocess.run(
                ["tmux", "display-message", "-p", "-t", target, "#{pane_tty} #{pane_current_command}"],
                capture_output=True, text=True, timeout=2,
            )
            if res.returncode != 0:
                self.set_notification("OpenCode pane is gone. Start the cockpit again.", "warn")
                return
            tty, _, command = res.stdout.strip().partition(" ")
            text = self.opencode_text(action_prompt, command, self._foreground_commands(tty))
            if text is None:
                self.set_notification(f"OpenCode pane is busy ({command or 'unknown'}). Nothing sent.", "warn")
                return
            # -l types the text as is (no key names); Enter goes as a separate key.
            sent = subprocess.run(["tmux", "send-keys", "-t", target, "-l", text], capture_output=True, text=True, timeout=2)
            if sent.returncode == 0:
                sent = subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], capture_output=True, text=True, timeout=2)
            if sent.returncode == 0:
                self.set_notification(f"Sent to OpenCode: {action_prompt[:35]}...", "ok")
            else:
                self.set_notification("Could not type into the OpenCode pane.", "warn")
        except Exception as e:
            self.set_notification(f"Action trigger failed: {e}", "error")

    # ------------------------------------------------------------------
    # Views (data in, Frame out)
    # ------------------------------------------------------------------

    def _finance_view_data(self) -> FinanceViewData:
        acc = (getattr(self, "finance_balances", {}) or {}).get("account_balances", {})

        def pick(exact: str, words: tuple) -> int:
            if acc.get(exact) is not None:
                return acc[exact]
            return next((v for k, v in acc.items() if any(w in k.lower() for w in words)), 0)

        return FinanceViewData(
            runway=getattr(self, "finance_runway", None),
            checking_cents=pick("Assets:Checking:WellsFargo", ("check", "wells")),
            savings_cents=pick("Assets:Savings:CapitalOne", ("sav", "capital", "hysa")),
            visa_cents=pick("Liabilities:CreditCard:Visa", ("visa", "credit", "card")),
            net_worth_cents=(getattr(self, "finance_balances", {}) or {}).get("net_worth_cents", 0),
            goals=getattr(self, "finance_goals", None) or [],
            days=self.finance_days,
            overview=self.finance_overview,
            paycheck=self.finance_paycheck,
            mask=self.mask_amounts,
            selected_day_idx=self.selected_day_idx,
            selected_item_idx=self.selected_item_idx,
            day_scroll_offset=self.day_scroll_offset,
            bills_breakdown=getattr(self, "finance_bills_breakdown", {}) or {},
            selected_bill_idx=getattr(self, "selected_bill_idx", 0),
            bills_scroll_offset=getattr(self, "bills_scroll_offset", 0),
            card=getattr(self, "finance_card", None),
            has_data=bool(getattr(self, "finance_has_data", True)),
            setup_error=getattr(self, "finance_setup_error", None),
        )

    @staticmethod
    def build_money_unavailable(reason: str, width: int) -> Frame:
        """Money tab when the ledger file cannot be opened. Other tabs keep working."""
        lines: List[Line] = [section("MONEY", width), []]
        lines.append(fit([("  " + GLYPHS["over"] + " ", "yellow"), ("Could not open the money ledger.", "yellow+b")], width))
        for part in shell.wrap_text(reason, max(10, width - 4)):
            lines.append(fit([("    " + part, "muted")], width))
        lines.append([])
        tip = ("Home, Memory and Guard still work. If another program is using finances.sqlite3, "
               "wait and press r. If the file is damaged, restore it from a backup.")
        for part in shell.wrap_text(tip, max(10, width - 2)):
            lines.append(fit([("  " + part, "text")], width))
        keys = [shell.KeyGroup.of("MONEY", shell.KeyHint("r", "try again", "open the money ledger again"))]
        return Frame(lines=lines, keys=keys)

    def _build_money(self, lay: Layout) -> Frame:
        """Money tab. Unless finance_view.FULL_WIDTH is set, the builders get the width inside the
        2-cell gutter and the TUI adds the gutter."""
        reason = getattr(self, "finance_open_error", None)
        if reason:
            return self.build_money_unavailable(reason, lay.width)
        mode = self.finances_view_mode
        data = self._finance_view_data()
        full = bool(getattr(finance_view, "FULL_WIDTH", False))
        cols = lay.width if full else max(10, lay.width - 2)
        rows = lay.body_height
        if mode == "bills":
            # j/k only reach the bills on screen; h can hide some, so clamp on every redraw.
            n = finance_view.bill_count(data)
            self.selected_bill_idx = max(0, min(max(0, n - 1), self.selected_bill_idx))
            data.selected_bill_idx = self.selected_bill_idx
            rendered: Rendered = build_bills(data, cols, rows)
            self.bills_scroll_offset = rendered.bills_scroll_offset
        elif mode == "day":
            rendered = build_day(data, cols, rows)
        elif mode == "math":
            rendered = build_math(data, cols, rows)
        else:
            rendered = build_overview(data, cols, rows)
            self.day_scroll_offset = rendered.day_scroll_offset
        top = lay.body_top
        self._finance_day_rows = {top + k: v for k, v in rendered.day_rows.items()}
        self._finance_item_rows = {top + k: v for k, v in rendered.item_rows.items()}
        self._finance_bill_rows = {top + k: v for k, v in rendered.bill_rows.items()}
        targets: Dict[int, Tuple[str, int]] = {}
        targets.update({k: ("day", v) for k, v in rendered.day_rows.items()})
        targets.update({k: ("item", v) for k, v in rendered.item_rows.items()})
        targets.update({k: ("bill", v) for k, v in rendered.bill_rows.items()})
        lines = [list(line) for line in rendered.lines] if full else [[("  ", "text")] + list(line) for line in rendered.lines]
        return Frame(lines=lines, keys=money_keys(mode, bool(getattr(self, "mask_amounts", False)), lay.width), rows=targets)

    def _build_body(self, lay: Layout) -> Frame:
        w, h = lay.width, lay.body_height
        tab = self.current_tab
        if tab == "money":
            return self._build_money(lay)
        module = VIEWS.get(tab, home_view)
        data, states = self._views()
        state = states.setdefault(tab, {})
        view_data = data.get(tab)
        if view_data is None:
            view_data = module.DATA_CLASS()
        if tab == "home":
            state["mask"] = bool(getattr(self, "mask_amounts", False))  # Home hides MONEY amounts too
        frame = module.build(view_data, state, w, h)
        state.update(frame.state)
        return frame

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _put(self, y: int, x: int, line: Line, width: int) -> None:
        """Draws one line at (y, x), clipped or padded to exactly `width` cells."""
        if width <= 0 or y < 0:
            return
        cx = x
        for text, style in fit(line, width):
            try:
                self.stdscr.addstr(y, cx, text, attr(style))
            except curses.error:
                pass
            cx += cell_width(text)

    @staticmethod
    def _load_sky() -> str:
        try:
            return tmux_helper.load_look()["sky"]
        except Exception:
            return art_city.DEFAULT_SKY

    def _chrome_width(self, lay: Layout) -> int:
        """Width of the header, tabs, toast and footer: shorter when the city window is on the right."""
        w = getattr(self, "_chrome_w", None)
        return w if w and 0 < w < lay.width else lay.width

    def _art_geometry(self, frame: Frame, lay: Layout) -> Tuple[Optional[Tuple[int, int, int, int]], Optional[int]]:
        """(art rect on screen, chrome width) for the room the view left (Frame.art)."""
        art = getattr(frame, "art", None)
        if not art or not THEME.art_ready():
            return None, None
        if art[0] == "window":
            cols = int(art[1])
            x0 = lay.width - cols  # the last screen column stays unwritten, as everywhere
            if x0 <= home_view.ART_GAP:
                return None, None
            return (0, x0, lay.rows, cols), x0 - home_view.ART_GAP
        if art[0] == "strip":
            row, rows = int(art[1]), int(art[2])
            if rows <= 0:
                return None, None
            return (lay.body_top + row, 0, rows, lay.width), None
        return None, None

    def _in_art(self, y: int, x: int) -> bool:
        rect = getattr(self, "_art_rect", None)
        if not rect:
            return False
        ry, rx, rows, cols = rect
        return ry <= y < ry + rows and rx <= x < rx + cols

    def _draw_art(self, now: float) -> None:
        """Draws the city into the art rect: one run of cells per color pair, from the art pool."""
        rect = getattr(self, "_art_rect", None)
        if not rect:
            return
        y0, x0, rows, cols = rect
        hour = art_city.hour_for(getattr(self, "sky_mode", art_city.DEFAULT_SKY), now)
        cells = art_city.render(cols, rows, now - getattr(self, "_art_t0", now), hour)
        THEME.begin_art_frame()
        for j, row in enumerate(cells):
            x, run, key = x0, [], None
            for ch, fg, bg in row:
                if (fg, bg) != key and run:
                    self._art_put(y0 + j, x, "".join(run), key)
                    x += len(run)
                    run = []
                key = (fg, bg)
                run.append(ch)
            if run:
                self._art_put(y0 + j, x, "".join(run), key)

    def _art_put(self, y: int, x: int, text: str, key: Optional[Tuple[int, int]]) -> None:
        try:
            self.stdscr.addstr(y, x, text, THEME.art_attr(*key) if key else 0)
        except curses.error:
            pass

    def _update_art_only(self, now: float) -> None:
        """Redraws only the city (8 frames a second), never while a box or the help card is open."""
        if not getattr(self, "_art_rect", None):
            return
        if self._get_modals().active or getattr(self, "help_open", False):
            return
        self._draw_art(now)
        self.stdscr.refresh()

    def _draw_header(self, lay: Layout, now: float) -> None:
        info: HeaderInfo = self.telemetry.header_info(now)
        self._header_active = info.active
        w = self._chrome_width(lay)
        line = shell.build_header(info, w, now, datetime.now().strftime("%H:%M"))
        self._put(shell.HEADER_ROW, 0, line, w)
        self._last_header = line

    def _draw_tabs(self, lay: Layout) -> None:
        w = self._chrome_width(lay)
        row, under, spans = shell.build_tabs(self.current_tab, w)
        self._tab_spans = spans
        self._put(shell.TAB_ROW, 0, row, w)
        self._put(shell.UNDERLINE_ROW, 0, under, w)

    def _draw_body(self, frame: Frame, lay: Layout, dim: bool = False) -> None:
        for i, line in enumerate(frame.lines[: lay.body_height]):
            if dim:  # a box is open: the screen behind it steps back
                line = [(t, "dim") for t, _ in line]
            self._put(lay.body_top + i, 0, line, lay.width)
        self._body_targets = {lay.body_top + k: v for k, v in frame.rows.items() if k < lay.body_height}
        self._frame_keys = list(frame.keys)

    def _help_overlay(self, lay: Layout) -> Overlay:
        ov = shell.build_help(self._frame_keys, lay.width, lay.body_top, lay.body_height,
                              scroll=getattr(self, "help_scroll", 0))
        self.help_scroll = ov.scroll
        self._help_scroll_max = ov.scroll_max
        return ov

    def _draw_overlay(self, ov: Optional[Overlay], lay: Layout) -> None:
        if ov is None:
            return
        room = lay.width - ov.left
        for i, line in enumerate(ov.lines):
            y = ov.top + i
            if y >= lay.rows:
                break
            self._put(y, ov.left, line, min(ov.width, room))

    def _draw_toast(self, lay: Layout) -> None:
        if self.notification and getattr(self, "notification_tab", None) not in (None, self.current_tab):
            self.notification = ""
        text = self.notification if self.notification and time.time() < self.notification_expiry else ""
        w = self._chrome_width(lay)
        self._put(lay.toast_row, 0, shell.build_toast(text, getattr(self, "notification_kind", "info"), w), w)

    def _footer_groups(self) -> Tuple[List[shell.KeyGroup], bool]:
        if self._get_modals().active:
            return self._get_modals().keys(), False
        if getattr(self, "help_open", False):
            if getattr(self, "_help_scroll_max", 0):
                return shell.HELP_SCROLL_KEYS, False
            return shell.HELP_OPEN_KEYS, False
        if self._view_captures_keys():
            return self._frame_keys, False  # typing a search: no "q quit"
        return self._frame_keys, True

    def _draw_footer(self, lay: Layout) -> None:
        groups, with_global = self._footer_groups()
        w = self._chrome_width(lay)
        self._put(lay.footer_row, 0, shell.build_footer(groups, w, with_global), w)

    def render(self) -> None:
        """Full frame: header, tabs, body, overlays, toast, footer."""
        max_y, max_x = self.stdscr.getmaxyx()
        lay = shell.layout(max_y, max_x)
        self.stdscr.erase()
        self._art_rect, self._chrome_w = None, None
        if max_y < shell.MIN_ROWS or max_x < shell.MIN_COLS:
            for i, line in enumerate(shell.too_small(lay.width, max_y)):
                self._put(i, 0, line, lay.width)
            self.stdscr.refresh()
            return
        now = time.time()
        # The body first: it says whether the theme's art takes the right side of every row.
        frame = self._build_body(lay)
        self._art_rect, self._chrome_w = self._art_geometry(frame, lay)
        self._draw_header(lay, now)
        self._draw_tabs(lay)
        modal = self._get_modals().active
        help_open = bool(getattr(self, "help_open", False)) and not modal
        # A box or the help card is open: the screen behind it steps back.
        self._draw_body(frame, lay, dim=modal or help_open)
        self._draw_art(now)  # still while a box or the help card is open (they draw over it)
        if modal:
            self._draw_overlay(self._get_modals().render(max_x, max_y), lay)
        elif help_open:
            self._draw_overlay(self._help_overlay(lay), lay)
        self._draw_toast(lay)
        self._draw_footer(lay)
        self.stdscr.refresh()

    def _update_header_only(self, now: float) -> None:
        """Redraws row 0 only (clock, LM Studio spinner). Skips the write when nothing changed."""
        max_y, max_x = self.stdscr.getmaxyx()
        if max_y < shell.MIN_ROWS or max_x < shell.MIN_COLS:
            return
        lay = shell.layout(max_y, max_x)
        info = self.telemetry.header_info(now)
        self._header_active = info.active
        w = self._chrome_width(lay)
        line = shell.build_header(info, w, now, datetime.now().strftime("%H:%M"))
        if line == self._last_header:
            return
        self._put(shell.HEADER_ROW, 0, line, w)
        self._last_header = line
        self.stdscr.refresh()

    def _update_toast_only(self) -> None:
        max_y, max_x = self.stdscr.getmaxyx()
        if max_y < shell.MIN_ROWS or max_x < shell.MIN_COLS:
            return
        self._draw_toast(shell.layout(max_y, max_x))
        self.stdscr.refresh()

    # ------------------------------------------------------------------
    # Money keys and mouse
    # ------------------------------------------------------------------

    def delete_selected_finance(self) -> None:
        day = self._current_day()
        if day is None:
            return
        items = day.get("items", [])
        if not items:
            return
        if self.finances_focus == "items":
            idx = min(self.selected_item_idx, len(items) - 1)
            target = items[idx]
            self.finance_panel.delete_transaction(target["id"])
            self.refresh_data()
            amount = "" if self.mask_amounts else f" (${abs(target['amount_cents']) / 100:.2f})"
            day_idx, self.selected_item_idx, gone = selection_after_delete(self.finance_days, day["date"], idx)
            self.selected_day_idx = day_idx
            msg = f"Deleted {target['payee']}{amount}."
            if gone:
                self._set_finance_mode("daily")
                msg += f" No entries left for {day['date']}. Back to the days."
            self.set_notification(msg, "ok")
        else:
            count = self.finance_panel.delete_day(day["date"])
            self.refresh_data()
            self.selected_day_idx = selection_after_delete(self.finance_days, day["date"], 0)[0]
            self.set_notification(f"Cleared all {count} transactions for {day['date']}.", "ok")

    def _arm_delete(self, target: tuple) -> None:
        day = self._current_day()
        items = (day or {}).get("items", [])
        if not items:
            return
        if self.finances_focus == "items":
            it = items[min(self.selected_item_idx, len(items) - 1)]
            what = str(it.get("payee", "this entry"))
            if not self.mask_amounts:
                what += f" ${abs(int(it.get('amount_cents', 0) or 0)) / 100:,.2f}"
        else:
            what = f"all {len(items)} entries of {day.get('date', 'this day')}"
        self.__dict__["_delete_armed"] = (target, time.time() + TOAST_S + 1)
        self.set_notification(f"Press d again to delete {what}.", "warn", TOAST_S + 1)

    def _ui_state(self) -> tuple:
        """Snapshot of everything a mouse event can change on screen."""
        return (
            self.current_tab,
            getattr(self, "help_open", False),
            getattr(self, "help_scroll", 0),
            repr(self._views()[1]),
            self.selected_day_idx,
            self.selected_item_idx,
            self.selected_bill_idx,
            self.finances_view_mode,
            self.finances_focus,
            self.notification,
        )

    def _set_finance_mode(self, mode: str) -> None:
        self.finances_view_mode = mode
        self.finances_focus = "items" if mode == "day" else "days"

    def _move_finance_selection(self, delta: int, avail_rows: int = 10) -> None:
        """Moves the highlighted row on whichever Money screen is showing."""
        mode = self.finances_view_mode
        if mode == "bills":
            n = finance_view.bill_count(self.finance_bills_breakdown or {})
            if n:
                self.selected_bill_idx = max(0, min(n - 1, self.selected_bill_idx + delta))
        elif mode == "day":
            day = self._current_day()
            items = day["items"] if day else []
            if items:
                self.selected_item_idx = max(0, min(len(items) - 1, self.selected_item_idx + delta))
        elif mode == "math":
            finance_view.scroll_math(delta)
        elif mode == "daily":
            if self.finance_days:
                new_idx = max(0, min(len(self.finance_days) - 1, self.selected_day_idx + delta))
                if new_idx != self.selected_day_idx:
                    self.selected_day_idx = new_idx
                    self.selected_item_idx = 0

    def _handle_finance_click(self, my: int) -> None:
        mode = self.finances_view_mode
        if mode == "daily" and my in self._finance_day_rows:
            if self._finance_day_rows[my] == self.selected_day_idx:
                self._open_selected_day()
            else:
                self.selected_day_idx = self._finance_day_rows[my]
                self.selected_item_idx = 0
        elif mode == "day" and my in self._finance_item_rows:
            self.selected_item_idx = self._finance_item_rows[my]
        elif mode == "bills" and my in getattr(self, "_finance_bill_rows", {}):
            self.selected_bill_idx = self._finance_bill_rows[my]

    def _open_selected_day(self) -> None:
        if self.finance_days:
            self.selected_item_idx = 0
            self._set_finance_mode("day")

    def handle_finance_key(self, ch: int, max_y: int) -> None:
        """Runs the action finance_view.money_action() maps this key to."""
        action = money_action(self.finances_view_mode, ch)
        if getattr(self, "finance_open_error", None) and action != "mask":
            if action in ("add", "edit", "delete", "delete_hint", "open_day", "bills", "math"):
                self.set_notification("Money ledger is not open. Press r to try again.", "warn")
            return
        armed = self.__dict__.pop("_delete_armed", None)
        if action == "delete":
            # d asks first (like ⌃D in the edit box): press d again within a few seconds.
            # The date and entry id, not the indexes: a refresh between the two presses can move
            # another entry onto the same index.
            target = finance_view.selection_key(self.finance_days or [], self.selected_day_idx,
                                                self.selected_item_idx, self.finances_focus)
            if not (armed and armed[0] == target and time.time() < armed[1]):
                self._arm_delete(target)
                return
        if action == "mask":
            self.mask_amounts = not self.mask_amounts
            self.set_notification(f"Amounts {'hidden' if self.mask_amounts else 'shown'}.")
        elif action in ("bills", "math"):
            self._set_finance_mode(action)
        elif action == "toggle_hidden_bills":
            prefs = finance_view.VIEW_PREFS
            prefs["show_hidden_bills"] = not prefs.get("show_hidden_bills", False)
        elif action == "back":
            self._set_finance_mode("daily")
        elif action in ("up", "down"):
            self._move_finance_selection(-1 if action == "up" else 1, max(1, max_y - 12))
        elif action == "open_day":
            self._open_selected_day()
        elif action == "prev_day":  # older day; days are newest first
            if self.selected_day_idx < len(self.finance_days) - 1:
                self.selected_day_idx += 1
                self.selected_item_idx = 0
        elif action == "next_day":
            if self.selected_day_idx > 0:
                self.selected_day_idx -= 1
                self.selected_item_idx = 0
        elif action == "add":
            self.open_add_modal()
        elif action == "edit":
            self.open_edit_modal()
        elif action == "delete":
            self.delete_selected_finance()
        elif action == "delete_hint":
            self.set_notification("Press enter to open the day, then d to delete an item (e, then ⌃D, clears the day).", "warn")

    def run_command(self, cmd: Optional[str]) -> None:
        """Carries out a command returned by a view (see shell.COMMANDS)."""
        if not cmd:
            return
        if cmd == "triage":
            self.trigger_opencode_action("Triage my unread emails and summarize urgent items using telemachus-email")
        elif cmd == "mask":  # Home m key: the same hide-amounts toggle as on Money
            self.handle_finance_key(ord("m"), 0)
        elif cmd == "sky":  # Home s key: next sky mode for the city, remembered in cockpit.json
            self.sky_mode = art_city.next_sky(getattr(self, "sky_mode", art_city.DEFAULT_SKY))
            try:
                tmux_helper.save_settings({"sky": self.sky_mode})
            except Exception:
                pass
            word = art_city.SKY_WORDS[self.sky_mode]
            note = f" ({datetime.now():%H:%M} now)" if self.sky_mode == "clock" else ""
            self.set_notification(f"Sky: {word}{note}", "info")
        elif cmd == "refresh":
            self.refresh_data(retry_finance=True)
            self.set_notification("Refreshed.", "ok")
        elif cmd == "open_trash":
            try:
                subprocess.run(["open", os.path.expanduser("~/.Trash")], capture_output=True, timeout=3)
            except Exception as e:
                self.set_notification(f"Could not open the Trash: {e}", "error")
        elif cmd.startswith("toast:"):
            _, kind, text = (cmd.split(":", 2) + ["", ""])[:3]
            self.set_notification(text, kind or "info")
        elif cmd.startswith("tab:"):
            self.current_tab = cmd[4:]

    def _view_key_press(self, ch: int) -> None:
        tab = self.current_tab
        module = VIEWS.get(tab)
        if module is None:
            return
        data, states = self._views()
        view_data = data.get(tab) or module.DATA_CLASS()
        self.run_command(module.handle_key(ch, states.setdefault(tab, {}), view_data))

    def _view_captures_keys(self) -> bool:
        module = VIEWS.get(self.current_tab)
        capture = getattr(module, "captures_keys", None)
        if not callable(capture):
            return False
        try:
            return bool(capture(self._views()[1].get(self.current_tab, {})))
        except Exception:
            return False

    def _cycle_tab(self, step: int) -> None:
        ids = shell.TAB_IDS
        self.current_tab = ids[(ids.index(self.current_tab) + step) % len(ids)]

    def handle_mouse(self) -> None:
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            if HAS_BUTTON5:
                return
            # Mouse version 1: the only report getmouse() cannot decode is wheel down.
            if self.help_open:
                self._scroll_help(1)
            else:
                self._scroll(1)
            return
        try:
            if self.help_open:
                if bstate & curses.BUTTON4_PRESSED:
                    self._scroll_help(-1)
                elif bstate & SCROLL_DOWN:
                    self._scroll_help(1)
                elif bstate & CLICK_MASK:
                    self._close_help()
                return
            if bstate & curses.BUTTON4_PRESSED:
                self._scroll(-1)
                return
            if bstate & SCROLL_DOWN:
                self._scroll(1)
                return
            if not bstate & CLICK_MASK:
                return  # ignore releases so one click acts once
            if self._in_art(my, mx):
                return  # the city is a picture: clicks on it do nothing
            if my in (shell.TAB_ROW, shell.UNDERLINE_ROW):
                tab = shell.tab_at(self._tab_spans, mx)
                if tab:
                    self.current_tab = tab
                return
            target = self._body_targets.get(my)
            if not target:
                return
            if self.current_tab == "money":
                self._handle_finance_click(my)
                return
            module = VIEWS.get(self.current_tab)
            if module is not None:
                data, states = self._views()
                view_data = data.get(self.current_tab) or module.DATA_CLASS()
                self.run_command(module.handle_click(target, states.setdefault(self.current_tab, {}), view_data))
        except Exception:
            pass

    def _scroll_help(self, delta: int) -> None:
        self.help_scroll = max(0, min(getattr(self, "_help_scroll_max", 0), getattr(self, "help_scroll", 0) + delta))

    def _close_help(self) -> None:
        self.help_open = False
        self.help_scroll = 0

    def _scroll(self, delta: int) -> None:
        """Trackpad scroll: same as k / j on the current tab."""
        if self.current_tab == "money":
            self._move_finance_selection(delta)
        else:
            self._view_key_press(ord("k") if delta < 0 else ord("j"))

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _view_key(self) -> tuple:
        return (self.current_tab, self.finances_view_mode, self._get_modals().kind, self.help_open)

    def _translate_ctrl_c(self, ch: int) -> Optional[int]:
        """Ctrl-C is a "cancel" reflex, not "quit": in a box, the help or a search it acts as esc
        (an Add box with typed lines still asks "esc again"). Elsewhere it only shows a hint, so
        the dashboard pane of the tmux split never closes by accident. None = key used up."""
        if ch != CTRL_C:
            return ch
        if self._get_modals().active or self.help_open or self._view_captures_keys():
            return 27
        self.set_notification(CTRL_C_TOAST, "info")
        return None

    def handle_key(self, ch: int) -> bool:
        """Handles one key. Returns False when the app should quit."""
        key = self._translate_ctrl_c(ch)
        if key is None:
            return True
        ch = key
        modals = self._get_modals()
        if modals.active:
            self._apply_modal_result(modals.handle_key(ch))
            return True
        if self.help_open:
            if ch in (27, ord("?"), ord("q"), ord("Q"), 10, 13):
                self._close_help()
            elif ch in (ord("j"), curses.KEY_DOWN):
                self._scroll_help(1)
            elif ch in (ord("k"), curses.KEY_UP):
                self._scroll_help(-1)
            elif ch in (ord(" "), curses.KEY_NPAGE):
                self._scroll_help(5)
            elif ch == curses.KEY_PPAGE:
                self._scroll_help(-5)
            return True
        if self._view_captures_keys():
            # The view takes every key as text (Memory search): q, ?, r, 1-4 and tab are typed.
            self._view_key_press(ch)
            return True
        if ch in (ord("q"), ord("Q")):
            return False
        if ch == ord("?"):
            self.help_open = True
            self.help_scroll = 0
        elif ch in TAB_BY_KEY:
            self.current_tab = TAB_BY_KEY[ch]
        elif ch == 9:
            self._cycle_tab(1)
        elif ch == KEY_BTAB:
            self._cycle_tab(-1)
        elif ch in (ord("r"), ord("R")):
            failed = bool(getattr(self, "finance_open_error", None))
            self.refresh_data(retry_finance=True)
            if getattr(self, "finance_open_error", None):
                self.set_notification("Money ledger still cannot be opened.", "warn")
            elif not failed:
                self.set_notification("Refreshed.", "ok")
            # else: the retry already said "Money ledger opened."
        elif self.current_tab == "money":
            self.handle_finance_key(ch, self.stdscr.getmaxyx()[0])
        else:
            self._view_key_press(ch)
        return True

    def _on_sigint(self, signum: int, frame: Any) -> None:
        # curses runs in cbreak mode, so ctrl-C arrives as SIGINT, not as a key. Turn it into one.
        self._sigint = True

    def run(self) -> None:
        self.telemetry.start()
        old_handler = None
        try:
            old_handler = signal.signal(signal.SIGINT, self._on_sigint)
        except (ValueError, OSError):  # not the main thread
            pass
        try:
            self._loop()
        finally:
            self.telemetry.stop()
            if old_handler is not None:
                try:
                    signal.signal(signal.SIGINT, old_handler)
                except (ValueError, OSError):
                    pass

    def _loop(self) -> None:
        last_refresh = time.time()
        prev_view = self._view_key()
        needs_render = True
        next_header = 0.0
        next_art = 0.0

        while True:
            now = time.time()

            # Tab / screen / box changes: full clear so nothing from the old screen can linger.
            view = self._view_key()
            if view != prev_view:
                self.stdscr.clearok(True)
                self.stdscr.clear()
                prev_view = view
                needs_render = True

            # Deferred full repaint after a resize burst
            if self._settle_repaint_at and now >= self._settle_repaint_at:
                self._settle_repaint_at = 0.0
                self._hard_reset_screen()
                needs_render = True

            if now - last_refresh > DATA_REFRESH_S:
                self.refresh_data()
                last_refresh = now
                needs_render = True

            toast_expired = bool(self.notification) and now >= self.notification_expiry
            if toast_expired:
                self.notification = ""

            if needs_render:
                start = time.time()
                self.render()
                self._debug(f"frame {self.stdscr.getmaxyx()} draw_ms={(time.time() - start) * 1000:.1f}")
                needs_render = False
                next_header = now + (ANIM_INTERVAL if self._header_active else IDLE_HEADER_INTERVAL)
                next_art = now + ANIM_INTERVAL
            else:
                if toast_expired:
                    self._update_toast_only()
                if now >= next_header:
                    # Only row 0 changes here, so the body never flickers while the spinner runs.
                    self._update_header_only(now)
                    next_header = now + (ANIM_INTERVAL if self._header_active else IDLE_HEADER_INTERVAL)
                if getattr(self, "_art_rect", None) and now >= next_art:
                    # Only the city changes here; it is drawn whole, so its recycled pairs stay right.
                    self._update_art_only(now)
                    next_art = now + ANIM_INTERVAL

            try:
                ch = self.stdscr.getch()
            except curses.error:
                ch = -1
            if self.__dict__.pop("_sigint", False):
                if ch != -1:
                    curses.ungetch(ch)
                ch = self._translate_ctrl_c(CTRL_C)
                if ch is None:
                    needs_render = True
                    continue
            if ch == -1:
                continue
            self._debug(f"key {ch}")
            needs_render = True

            if ch == curses.KEY_RESIZE:
                # Only call resizeterm() when the real tty size differs from what curses has.
                # Calling it unconditionally queues another KEY_RESIZE every time (endless loop).
                try:
                    tty = os.get_terminal_size(sys.stdout.fileno())
                    if (tty.lines, tty.columns) != tuple(self.stdscr.getmaxyx()):
                        curses.resizeterm(tty.lines, tty.columns)
                except (OSError, ValueError, curses.error):
                    pass
                new_size = self.stdscr.getmaxyx()
                if new_size == self._last_size:
                    needs_render = False  # tmux repeats resize signals with no real change
                    continue
                self._last_size = new_size
                self.stdscr.clearok(True)
                self.stdscr.clear()
                # tmux resizes in bursts while attaching; repaint once more after it settles.
                self._settle_repaint_at = time.time() + 0.4
                continue

            if ch == curses.KEY_MOUSE and not self._get_modals().active:
                before = self._ui_state()
                self.handle_mouse()
                needs_render = self._ui_state() != before
                continue

            if self._get_modals().active and ch not in (curses.KEY_RESIZE, curses.KEY_MOUSE):
                # Read every waiting key first: a paste arrives as one burst, and a blank line in it
                # must not count as "enter on an empty line" (save).
                keys, pending = self._drain_keys(ch)
                if len(keys) > 1:
                    self._apply_modal_result(self._get_modals().handle_keys(keys))
                else:
                    self._apply_modal_result(self._get_modals().handle_key(ch))
                for extra in pending:
                    curses.ungetch(extra)
                continue

            if not self.handle_key(ch):
                break

    def _drain_keys(self, first: int) -> Tuple[List[int], List[int]]:
        """(keys of this burst, keys to handle after it). Stops at a resize or mouse event."""
        keys = [first]
        pending: List[int] = []
        try:
            self.stdscr.nodelay(True)
            while len(keys) < 4096:
                k = self.stdscr.getch()
                if k == -1:
                    break
                if k in (curses.KEY_RESIZE, curses.KEY_MOUSE):
                    pending.append(k)
                    break
                keys.append(k)
        except curses.error:
            pass
        finally:
            self.stdscr.timeout(50)
        return keys, pending


def main() -> None:
    """Terminal entry point. Picks the theme first: TELEMACHUS_THEME, then cockpit.json, then Golden Hour."""
    try:
        theme_mod.use_theme(tmux_helper.theme_name())
    except Exception:
        theme_mod.use_theme(theme_mod.DEFAULT_THEME)
    try:
        curses.wrapper(lambda scr: CockpitTUI(scr).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
