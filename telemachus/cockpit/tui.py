"""Telemachus Terminal Cockpit TUI (v1).

A high-performance curses-based terminal cockpit in the Tokyo Night palette.
Provides live Email Triage, Laya Guard telemetry, Unified Memory Vault events,
and full mouse / keyboard interaction.
"""
from __future__ import annotations

import curses
from datetime import datetime
import os
import subprocess
import sys
import time
import unicodedata
from typing import Any, Dict, List, Optional

from telemachus.cockpit.email_panel import EmailTriagePanel
from telemachus.cockpit.finance_panel import EDIT_CATEGORIES, FinanceTuiPanel
from telemachus.cockpit.finance_view import FinanceViewData, build_cycle_detail, build_day, build_math, build_overview
from telemachus.cockpit.safety_memory_panel import SafetyMemoryPanel
from telemachus.cockpit.telemetry import CockpitTelemetry

# Variation selector 16 asks for emoji presentation. Terminals, tmux and ncurses
# disagree on the resulting width, which desyncs the cursor column and makes
# lines jitter on every repaint. Dropping it keeps one stable width everywhere.
_VS16 = "\ufe0f"


def _char_width(ch: str) -> int:
    """Terminal cell width of a single character (0, 1 or 2)."""
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _clip_pad(text: str, width: int) -> str:
    """Clips text to `width` terminal cells and pads with spaces to exactly `width`."""
    out: List[str] = []
    used = 0
    for ch in text.replace(_VS16, ""):
        w = _char_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out) + " " * (width - used)


class _SafeScreen:
    """Delegates to a curses window but normalizes all text written via addstr."""

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


class CockpitTUI:
    """Curses-driven Terminal Cockpit with Tokyo Night aesthetics."""

    def __init__(self, stdscr: curses.window):
        self.stdscr = _SafeScreen(stdscr)
        self.telemetry = CockpitTelemetry()
        self.email_panel = EmailTriagePanel()
        self.safety_panel = SafetyMemoryPanel()
        self.finance_panel = FinanceTuiPanel()

        self.current_tab = 1  # 1: Daily Ops, 2: Memory Vault, 3: Safety Guard, 4: Finances
        self.selected_email_idx = 0
        self.email_scroll_offset = 0
        self.notification = ""
        self.notification_expiry = 0.0

        self.emails: List[Dict[str, Any]] = []
        self.email_stats: Dict[str, int] = {}
        self.guard_status: Dict[str, Any] = {}
        self.memory_stats: Dict[str, Any] = {}
        self.memory_events: List[Dict[str, Any]] = []

        # Finance Tab State
        self.finance_days: List[Dict[str, Any]] = []
        self.finance_overview: Any = None
        self.finance_cashflow: Any = None
        self.finance_paycheck: Any = None
        self.finance_runway: Any = None
        self._finance_day_rows: Dict[int, int] = {}  # screen row -> day index (for mouse clicks)
        self._finance_item_rows: Dict[int, int] = {}  # screen row -> item index
        self.finance_balances: Dict[str, Any] = {}
        self.finance_goals: List[Any] = []
        self.finance_bills_breakdown: Dict[str, Any] = {}
        self.finances_view_mode: str = "daily"  # "daily" (overview), "day", "math" or "bills"
        self.finances_focus: str = "days"  # "days" or "items"
        self.selected_day_idx = 0
        self.day_scroll_offset = 0
        self.selected_item_idx = 0
        self.selected_bill_idx = 0
        self.bills_scroll_offset = 0
        self.mask_amounts = False

        # In-Cockpit Modal State for Logging Daily Finances
        self.is_adding_finances = False
        self.modal_stage = "date"  # "date" or "bullets"
        self.modal_date_buffer = ""
        self.modal_line_buffer = ""
        self.modal_batch_lines: List[str] = []

        # In-Cockpit Modal State for Editing Daily Finances
        self.is_editing_finances = False
        self.editing_stage = "item"  # "item" or "day_menu"
        self.editing_tx_id: Optional[str] = None
        self.editing_date = ""
        self.editing_orig_text = ""
        self.editing_buffer = ""
        self.editing_account_idx = 0
        self.editing_category_idx = 0  # index into EDIT_CATEGORIES; 0 = Auto
        self.editing_current_category = ""

        self._last_size = self.stdscr.getmaxyx()
        self._settle_repaint_at = 0.0
        self._debug_path = os.environ.get("TELEMACHUS_TUI_DEBUG")

        self._init_curses()
        self.refresh_data()

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
        if not self._debug_path:
            return
        try:
            with open(self._debug_path, "a", encoding="utf-8") as f:
                f.write(f"{time.time():.3f} {msg}\n")
        except OSError:
            pass

    def _init_curses(self) -> None:
        curses.curs_set(0)  # Hide hardware cursor
        try:
            curses.set_escdelay(25)  # Fast escape sequence resolution (25ms)
        except Exception:
            pass
        self.stdscr.timeout(50)  # Wait up to 50ms for input in poll() - yields CPU to OS
        self.stdscr.keypad(True)

        # Enable full mouse events
        try:
            # No REPORT_MOUSE_POSITION: motion events would fire on every cursor move
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        except Exception:
            pass

        # Color initialization
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()

            # Pair definitions
            curses.init_pair(1, curses.COLOR_CYAN, -1)     # Neon Cyan (Headers / Active)
            curses.init_pair(2, curses.COLOR_MAGENTA, -1)  # Purple / Accent
            curses.init_pair(3, curses.COLOR_RED, -1)      # Red / Urgent
            curses.init_pair(4, curses.COLOR_YELLOW, -1)   # Yellow / Amber / Action
            curses.init_pair(5, curses.COLOR_GREEN, -1)    # Green / Safe / Public
            curses.init_pair(6, curses.COLOR_BLUE, -1)     # Blue / Digest / Project
            curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_CYAN) # Inverted Cyan (Selection)
            curses.init_pair(8, curses.COLOR_WHITE, -1)    # Muted White / Body
            curses.init_pair(9, curses.COLOR_BLACK, curses.COLOR_MAGENTA) # Inverted Purple
            curses.init_pair(10, curses.COLOR_BLACK, curses.COLOR_WHITE)  # Inverted White / Modal Card
            curses.init_pair(11, curses.COLOR_YELLOW, -1)  # Amber / Warning / Due Soon

    def _draw_line_padded(self, y: int, x: int, width: int, text: str, attr: int = 0) -> None:
        """Writes text clamped to width, padding trailing space to eliminate ghosting."""
        if width <= 0 or y < 0:
            return
        try:
            max_y, max_x = self.stdscr.getmaxyx()
            if y >= max_y:
                return
            # Ensure we never write into the final terminal column to prevent curses auto-wrap
            safe_w = min(width, max(1, max_x - x - 1))
            padded = _clip_pad(text, safe_w)
            self.stdscr.addstr(y, x, padded, attr)
        except curses.error:
            pass

    def refresh_data(self) -> None:
        """Reloads latest state from SQLite, memory vault, and system."""
        try:
            self.emails = self.email_panel.get_triage_items(limit=30)
            self.email_stats = self.email_panel.get_summary_stats()
            self.guard_status = self.safety_panel.get_guard_status()
            self.memory_stats = self.safety_panel.get_memory_stats()
            self.memory_events = self.safety_panel.get_recent_events(limit=8)

            # Finances refresh
            self.finance_panel.refresh()
            self.finance_days = self.finance_panel.get_daily_ledger()
            self.finance_overview = self.finance_panel.get_monthly_overview()
            self.finance_cashflow = self.finance_panel.get_cash_flow_summary()
            self.finance_paycheck = self.finance_panel.get_paycheck_allocation()
            self.finance_runway = self.finance_panel.get_runway()
            self.finance_balances = self.finance_panel.get_balances()
            self.finance_goals = self.finance_panel.get_goals_progress()
            self.finance_bills_breakdown = self.finance_panel.get_monthly_bills_breakdown()

            if self.finance_days and self.selected_day_idx >= len(self.finance_days):
                self.selected_day_idx = len(self.finance_days) - 1
        except Exception as e:
            self.set_notification(f"Refresh error: {e}")

    def set_notification(self, msg: str, duration: float = 3.0) -> None:
        self.notification = msg
        self.notification_expiry = time.time() + duration

    def trigger_opencode_action(self, action_prompt: str) -> None:
        """Pipes an action command to the adjacent OpenCode tmux pane."""
        target_pane = os.environ.get("TMUX_PANE_RIGHT") or "telemachus:0.1"
        try:
            cmd = ["tmux", "send-keys", "-t", target_pane, f"opencode run '{action_prompt}'", "Enter"]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                self.set_notification(f"⚡ Dispatched to OpenCode: {action_prompt[:35]}...")
            else:
                self.set_notification("Notice: Not inside split tmux session (task logged).")
        except Exception as e:
            self.set_notification(f"Action trigger failed: {e}")

    def draw_top_telemetry(self, max_x: int) -> None:
        header = self.telemetry.format_single_line_header(max_width=max_x - 2)
        llm_stat = getattr(self.telemetry, "get_llm_status", lambda: {"status": "idle"})()
        st = llm_stat.get("status")
        if st == "processing_prompt":
            color_attr = curses.color_pair(4) | curses.A_BOLD  # Yellow/Amber for prompt processing
        elif st == "producing_tokens":
            color_attr = curses.color_pair(1) | curses.A_BOLD  # Neon Cyan for active producing
        else:
            color_attr = curses.color_pair(1) | curses.A_BOLD

        self._draw_line_padded(0, 0, max_x - 1, " " + header, color_attr)

    def draw_tabs(self, max_x: int) -> None:
        tab1_attr = curses.color_pair(7) | curses.A_BOLD if self.current_tab == 1 else curses.color_pair(1)
        tab2_attr = curses.color_pair(7) | curses.A_BOLD if self.current_tab == 2 else curses.color_pair(1)
        tab3_attr = curses.color_pair(7) | curses.A_BOLD if self.current_tab == 3 else curses.color_pair(1)
        tab4_attr = curses.color_pair(7) | curses.A_BOLD if self.current_tab == 4 else curses.color_pair(1)

        try:
            # Wipe line 1 completely across max_x to prevent any leftover text from bleeding
            self.stdscr.addstr(1, 0, " " * max(1, max_x - 1), curses.color_pair(8))
            self.stdscr.addstr(1, 1, "[1: Daily Ops]", tab1_attr)
            self.stdscr.addstr(1, 17, "[2: Memory Vault]", tab2_attr)
            self.stdscr.addstr(1, 36, "[3: Safety Guard]", tab3_attr)
            self.stdscr.addstr(1, 55, "[4: Finances]", tab4_attr)

            actions = "" if self.current_tab == 4 else "[t] Triage  [r] Refresh  [q] Quit"

            if actions and max_x > len(actions) + 72:
                self.stdscr.addstr(1, max_x - len(actions) - 2, actions, curses.color_pair(2))
        except curses.error:
            pass

    def draw_daily_ops(self, max_y: int, max_x: int) -> None:
        split_y = max(8, int(max_y * 0.55))

        # ---------------- EMAIL TRIAGE SECTION ----------------
        try:
            tot = self.email_stats.get("total", 0)
            urg = self.email_stats.get("urgent", 0)
            act = self.email_stats.get("action", 0)
            title = f" 📥 EMAIL TRIAGE ({tot} msgs · {urg} urg · {act} act) "
            self.stdscr.addstr(2, 2, title[: max_x - 4], curses.color_pair(1) | curses.A_BOLD)

            # Table Header
            if max_x >= 65:
                col_hdr = f"  {'BADGE':<9} {'SENDER':<18} {'SUBJECT':<28} {'DATE':<10}"
            else:
                col_hdr = f"  {'BADGE':<8} {'SENDER':<14} {'SUBJECT'}"
            self.stdscr.addstr(3, 1, col_hdr[: max_x - 2], curses.color_pair(2) | curses.A_BOLD)
            self.stdscr.addstr(4, 1, "─" * max(1, max_x - 2), curses.color_pair(8))

            email_rows_avail = max(1, split_y - 6)
            start_row = 5

            visible_emails = self.emails[self.email_scroll_offset : self.email_scroll_offset + email_rows_avail]
            for idx, item in enumerate(visible_emails):
                actual_idx = self.email_scroll_offset + idx
                is_selected = (actual_idx == self.selected_email_idx)
                row_y = start_row + idx
                if row_y >= split_y - 1:
                    break

                badge = f"[{item['badge']}]"
                badge_pair = curses.color_pair(3) if "URGENT" in badge else (
                    curses.color_pair(4) if "ACTION" in badge else curses.color_pair(6)
                )

                attr = curses.color_pair(7) if is_selected else curses.color_pair(8)
                self.stdscr.addstr(row_y, 1, " " * (max_x - 2), attr)
                self.stdscr.addstr(row_y, 2, badge[:8], badge_pair | (curses.A_BOLD if not is_selected else 0))

                if max_x >= 65:
                    self.stdscr.addstr(row_y, 12, item['sender'][:16], attr)
                    subj_avail = max(1, max_x - 42)
                    self.stdscr.addstr(row_y, 30, item['subject'][:subj_avail], attr)
                    if max_x >= 80:
                        self.stdscr.addstr(row_y, max_x - 12, item['date'][:10], attr)
                else:
                    self.stdscr.addstr(row_y, 11, item['sender'][:12], attr)
                    subj_avail = max(1, max_x - 26)
                    self.stdscr.addstr(row_y, 24, item['subject'][:subj_avail], attr)

            # Selected Snippet Preview Box
            if self.emails and 0 <= self.selected_email_idx < len(self.emails):
                sel = self.emails[self.selected_email_idx]
                snip = sel.get("snippet", "No preview available.")
                preview_line = f"🔎 Preview: {snip}"
                if split_y - 1 > 4:
                    self.stdscr.addstr(split_y - 1, 2, preview_line[: max_x - 4], curses.color_pair(1))

        except curses.error:
            pass

        # ---------------- BOTTOM SPLIT: SAFETY & MEMORY ----------------
        try:
            self.stdscr.addstr(split_y, 1, "═" * max(1, max_x - 2), curses.color_pair(1))

            if max_x >= 70:
                col_width = (max_x - 3) // 2

                # Left Col: Laya Safety Guard
                self.stdscr.addstr(split_y + 1, 2, "🛡️ LAYA SAFETY GUARD", curses.color_pair(1) | curses.A_BOLD)
                prot = ", ".join(self.guard_status.get("protected_paths", []))
                self.stdscr.addstr(split_y + 2, 2, f"• Protected: {prot}"[:col_width], curses.color_pair(5))
                trash_num = self.guard_status.get("trash_substitutions", 0)
                self.stdscr.addstr(split_y + 3, 2, f"• Trash Safety: {trash_num} items"[:col_width], curses.color_pair(8))
                lat = self.guard_status.get("router_latency_ms", 58.2)
                self.stdscr.addstr(split_y + 4, 2, f"• ModernBERT Gate: {lat:.1f}ms"[:col_width], curses.color_pair(4))

                # Right Col: Continual Memory Stream
                right_x = col_width + 3
                tot_mem = self.memory_stats.get("total_memories", 59)
                tiers = self.memory_stats.get("by_tier", {})
                prv = tiers.get("tier_2_private", 49)
                prj = tiers.get("tier_1_project", 4)
                pub = tiers.get("tier_0_public", 6)
                mem_title = f"🧠 MEMORY ({tot_mem} facts · {prv}🔒 {prj}📁 {pub}🌐)"
                self.stdscr.addstr(split_y + 1, right_x, mem_title[: max_x - right_x - 1], curses.color_pair(2) | curses.A_BOLD)

                row_offset = 0
                for ev in self.memory_events[: max(1, max_y - split_y - 3)]:
                    ev_line = f"• {ev['time']} [{ev['badge']}] {ev['type']} ({ev['desc']})"
                    b_color = curses.color_pair(5) if ev["badge"] == "PUB" else (
                        curses.color_pair(6) if ev["badge"] == "PRJ" else curses.color_pair(2)
                    )
                    self.stdscr.addstr(split_y + 2 + row_offset, right_x, ev_line[: max_x - right_x - 1], b_color)
                    row_offset += 1
            else:
                # Narrow terminal: compact stacked view
                self.stdscr.addstr(split_y + 1, 2, "🛡️ LAYA & MEMORY", curses.color_pair(1) | curses.A_BOLD)
                trash_num = self.guard_status.get("trash_substitutions", 0)
                tot_mem = self.memory_stats.get("total_memories", 59)
                line1 = f"• Trash: {trash_num} | Vault: {tot_mem} facts"
                self.stdscr.addstr(split_y + 2, 2, line1[: max_x - 3], curses.color_pair(8))
                if self.memory_events and max_y > split_y + 4:
                    ev = self.memory_events[0]
                    ev_line = f"• Latest: [{ev['badge']}] {ev['type']} ({ev['desc']})"
                    self.stdscr.addstr(split_y + 3, 2, ev_line[: max_x - 3], curses.color_pair(2))
        except curses.error:
            pass


    def draw_memory_vault_view(self, max_y: int, max_x: int) -> None:
        try:
            self.stdscr.addstr(3, 2, "🧠 UNIFIED MEMORY VAULT EXPLORER (~/.memory/)", curses.color_pair(2) | curses.A_BOLD)
            tiers = self.memory_stats.get("by_tier", {})
            by_cat = self.memory_stats.get("by_category", {})
            stats_line = (
                f"Tiers: {tiers.get('tier_2_private', 0)} Private (0700) | "
                f"{tiers.get('tier_1_project', 0)} Project | "
                f"{tiers.get('tier_0_public', 0)} Public  |  Categories: {len(by_cat)}"
            )
            self.stdscr.addstr(4, 2, stats_line, curses.color_pair(8))
            self.stdscr.addstr(5, 1, "─" * (max_x - 2), curses.color_pair(8))

            row = 6
            for cat, count in list(by_cat.items())[: max_y - 8]:
                self.stdscr.addstr(row, 2, f"📁 [{cat.upper()}] - {count} indexed memories", curses.color_pair(1))
                row += 1
        except curses.error:
            pass

    def draw_safety_guard_view(self, max_y: int, max_x: int) -> None:
        try:
            self.stdscr.addstr(3, 2, "🛡️ LAYA 4-TIER EGRESS GUARD & SECURITY STATUS", curses.color_pair(1) | curses.A_BOLD)
            self.stdscr.addstr(5, 2, "Tier 0: Deterministic Path Shield", curses.color_pair(5) | curses.A_BOLD)
            self.stdscr.addstr(6, 4, "• Strictly Blocked: /System, /Library, ~/.ssh, ~/.gnupg, foreign .env files", curses.color_pair(8))
            self.stdscr.addstr(8, 2, "Tier 1: Intent-Action Consistency (ModernBERT MPS)", curses.color_pair(4) | curses.A_BOLD)
            self.stdscr.addstr(9, 4, "• Evaluates tool parameters against classified intent in under 60ms", curses.color_pair(8))
            self.stdscr.addstr(11, 2, "Tier 2: Soft Recovery Deletions", curses.color_pair(2) | curses.A_BOLD)
            self.stdscr.addstr(12, 4, f"• Hard 'rm' replaced by /usr/bin/trash ({self.guard_status.get('trash_substitutions', 0)} audit items)", curses.color_pair(8))
            self.stdscr.addstr(14, 2, "Tier 3: Git Auto-Checkpoints & Elevation Confirmation", curses.color_pair(1) | curses.A_BOLD)
            self.stdscr.addstr(15, 4, "• Surgical atomic writes with pre-turn git snapshots", curses.color_pair(8))
        except curses.error:
            pass

    def _fmt_money(self, cents: int, mask: bool = False, signed: bool = False) -> str:
        if mask:
            return "$***.**"
        sign = ""
        if signed and cents != 0:
            sign = "+" if cents > 0 else "-"
        abs_val = abs(cents) / 100.0
        return f"{sign}${abs_val:,.2f}"

    def _draw_bills_screen(self, max_y: int, max_x: int) -> None:
        """Recurring Bills view: legacy balances header, then the bills table and monthly overview."""
        try:
            # 1. Top Balances Deck (Checking, HYSA Savings, Outstanding Visa, Net Worth)
            acc_bals = self.finance_balances.get("account_balances", {})
            chk_cents = acc_bals.get("Assets:Checking:WellsFargo")
            if chk_cents is None:
                chk_cents = next((v for k, v in acc_bals.items() if "check" in k.lower() or "wells" in k.lower()), 0)

            sav_cents = acc_bals.get("Assets:Savings:CapitalOne")
            if sav_cents is None:
                sav_cents = next((v for k, v in acc_bals.items() if "sav" in k.lower() or "capital" in k.lower() or "hysa" in k.lower()), 0)

            visa_cents = acc_bals.get("Liabilities:CreditCard:Visa")
            if visa_cents is None:
                visa_cents = next((v for k, v in acc_bals.items() if "visa" in k.lower() or "credit" in k.lower() or "card" in k.lower()), 0)

            nw_cents = self.finance_balances.get("net_worth_cents", 0)
            pc = self.finance_paycheck
            bills_due_cents = pc.bills_due_cents if pc else 0
            chk_post_cents = chk_cents - bills_due_cents

            chk_str = self._fmt_money(chk_cents, self.mask_amounts)
            chk_post_str = self._fmt_money(chk_post_cents, self.mask_amounts)
            sav_str = self._fmt_money(sav_cents, self.mask_amounts)
            visa_str = self._fmt_money(visa_cents, self.mask_amounts)
            nw_str = self._fmt_money(nw_cents, self.mask_amounts)

            # Row 2: Balance Sheet (Checking, HYSA, Visa, Net Worth)
            if max_x >= 90:
                vis_label = "Visa: "
                vis_pair = curses.color_pair(5) if visa_cents <= 0 else curses.color_pair(4)
                line_bal = (
                    f" 🏛️ BALANCES   Checking: {chk_str}  │  HYSA: {sav_str}  │  "
                    f"{vis_label}{visa_str}  │  Net Worth: {nw_str}"
                )
            else:
                line_bal = f" 💰 BALANCES: Chk: {chk_str} │ HYSA: {sav_str} │ Visa: {visa_str} │ NW: {nw_str}"
            self._draw_line_padded(2, 1, max_x - 2, line_bal, curses.color_pair(1) | curses.A_BOLD)

            # Row 3: 2-Week Window & Paycheck Runway (Zero duplicate checking balance)
            if pc:
                safe_spend_cents = pc.safe_to_spend_cents
                burn_cents = safe_spend_cents // max(1, pc.days_in_cycle)
                safe_str = self._fmt_money(safe_spend_cents, self.mask_amounts)
                burn_str = self._fmt_money(burn_cents, self.mask_amounts)
                bills_str = self._fmt_money(bills_due_cents, self.mask_amounts)
                w_start = pc.paycheck_date[5:] if len(pc.paycheck_date) >= 10 else pc.paycheck_date
                w_end = pc.next_paycheck_date

                # Compute days until next check
                try:
                    dt_next = datetime.strptime(w_end, "%Y-%m-%d")
                    dt_now = datetime.now()
                    days_left = max(0, (dt_next.date() - dt_now.date()).days)
                    days_label = f"in {days_left}d" if days_left > 0 else "TODAY"
                except Exception:
                    days_label = "active"

                cushion_pair_flag = "🛡️" if chk_post_cents >= 30000 else ("⚠️" if chk_post_cents >= 0 else "🚨")
                line_2w = (
                    f" 💵 2-WEEK WINDOW (Next Paycheck: {w_end} · {days_label}) │ "
                    f"Bills Due: -{bills_str} │ Cushion: {chk_post_str} │ {cushion_pair_flag} Safe-to-Spend: {safe_str} ({burn_str}/d)"
                )
            else:
                line_2w = " 💵 2-WEEK WINDOW │ 14-day allocation active"
            
            cush_attr = curses.color_pair(5) if chk_post_cents >= 0 else curses.color_pair(3)
            self._draw_line_padded(3, 1, max_x - 2, line_2w, curses.color_pair(4) | curses.A_BOLD)

            # Rows 4 & 5: Milestone Goal Progress Gauges (Unclipped, dynamically sized)
            goals = getattr(self, "finance_goals", []) or []
            if goals:
                for g_idx, goal in enumerate(goals[:2]):
                    gy = 4 + g_idx
                    pct_str = f"{goal.progress_pct:.1f}%" if not self.mask_amounts else "**%"
                    saved_str = self._fmt_money(goal.saved_cents, self.mask_amounts)
                    tgt_str = self._fmt_money(goal.target_cents, self.mask_amounts)
                    rem_str = self._fmt_money(goal.remaining_cents, self.mask_amounts)
                    pfx = " 🎯 GOALS   " if g_idx == 0 else "            "
                    
                    # Dynamically calculate name and bar width to prevent any overflow/wrap
                    name_w = min(18, max(12, max_x - 76))
                    bar_w = min(10, max(4, max_x - 78))
                    filled = int(round((goal.progress_pct / 100.0) * bar_w))
                    filled = min(bar_w, max(0, filled))
                    bar_str = f"[{'█' * filled}{'░' * (bar_w - filled)}]"
                    status_pair = curses.color_pair(5) if goal.status == "completed" else curses.color_pair(2)
                    goal_line = f"{pfx}{goal.name[:name_w]:<{name_w}} {bar_str} {pct_str:>5} ({saved_str} / {tgt_str} · Left: {rem_str})"
                    self._draw_line_padded(gy, 1, max_x - 2, goal_line, status_pair | curses.A_BOLD)
            else:
                ov = self.finance_overview
                sav_goal = ov.savings_target_cents if ov else 0
                sav_goal_str = self._fmt_money(sav_goal, self.mask_amounts)
                cushion_cents = ov.remaining_cushion_cents if ov else 0
                cushion_str = self._fmt_money(cushion_cents, self.mask_amounts)
                self._draw_line_padded(4, 1, max_x - 2, f" 🎯 TARGETS:   Savings Goal: {sav_goal_str}/mo │ Cushion Left: {cushion_str}", curses.color_pair(2) | curses.A_BOLD)
                month_label = ov.month if ov else datetime.now().strftime("%Y-%m")
                self._draw_line_padded(5, 1, max_x - 2, f" 📊 CASH FLOW ({month_label}): Inflow active", curses.color_pair(4) | curses.A_BOLD)

            # Row 6: Interactive Mode Switcher Divider
            div_left = "── Recurring Bills & Budget  ([v] back to overview) "
            divider_line = div_left + "─" * max(0, (max_x - 2) - len(div_left))
            self._draw_line_padded(6, 1, max_x - 2, divider_line, curses.color_pair(8))

            self.draw_bills_overview_view(max_y, max_x)
        except curses.error:
            pass

    def _finance_view_data(self) -> FinanceViewData:
        acc = self.finance_balances.get("account_balances", {})

        def pick(exact: str, words: tuple) -> int:
            if acc.get(exact) is not None:
                return acc[exact]
            return next((v for k, v in acc.items() if any(w in k.lower() for w in words)), 0)

        return FinanceViewData(
            runway=self.finance_runway,
            checking_cents=pick("Assets:Checking:WellsFargo", ("check", "wells")),
            savings_cents=pick("Assets:Savings:CapitalOne", ("sav", "capital", "hysa")),
            visa_cents=pick("Liabilities:CreditCard:Visa", ("visa", "credit", "card")),
            net_worth_cents=self.finance_balances.get("net_worth_cents", 0),
            goals=self.finance_goals or [],
            days=self.finance_days,
            overview=self.finance_overview,
            paycheck=self.finance_paycheck,
            mask=self.mask_amounts,
            selected_day_idx=self.selected_day_idx,
            selected_item_idx=self.selected_item_idx,
            day_scroll_offset=self.day_scroll_offset,
        )

    def _style_attr(self, style: str) -> int:
        name, _, flag = style.partition("+")
        bold = curses.A_BOLD if flag == "b" else 0
        if name == "dim":
            return curses.color_pair(8) | curses.A_DIM
        if name == "line":
            return curses.color_pair(8) | curses.A_DIM
        if name == "sel":
            return curses.color_pair(7) | curses.A_BOLD
        pair = {"sub": 8, "fg": 8, "snow": 8, "cyan": 1, "blue": 6, "mag": 2, "green": 5, "yellow": 4}.get(name, 8)
        return curses.color_pair(pair) | bold | (curses.A_BOLD if name == "snow" else 0)

    def _draw_styled_lines(self, y0: int, x0: int, lines: List[Any], max_y: int, max_x: int) -> None:
        """Draws lines of (text, style) segments; each line is clipped at the screen edge."""
        limit_x = max_x - 1  # never write into the last column (curses auto-wrap)
        for i, line in enumerate(lines):
            y = y0 + i
            if y >= max_y - 1:
                break
            x = x0
            try:
                self.stdscr.addstr(y, x0, " " * max(0, limit_x - x0), curses.color_pair(8))
            except curses.error:
                pass
            for text, style in line:
                room = limit_x - x
                if room <= 0:
                    break
                clipped = _clip_pad(text, min(room, sum(_char_width(c) for c in text)))
                try:
                    self.stdscr.addstr(y, x, clipped, self._style_attr(style))
                except curses.error:
                    pass
                x += sum(_char_width(c) for c in clipped)

    def draw_finances_view(self, max_y: int, max_x: int) -> None:
        """Finances tab: overview (default), one day's items, the math, or recurring bills."""
        mode = self.finances_view_mode
        if mode == "bills":
            self._draw_bills_screen(max_y, max_x)
            return
        data = self._finance_view_data()
        rows = max_y - 3  # rows 0-1 are the header and tabs, the last row is the hint bar
        cols = max_x - 2  # one blank column on each side
        if mode == "day":
            rendered = build_day(data, cols, rows)
        elif mode == "math":
            rendered = build_math(data, cols, rows)
        else:
            rendered = build_overview(data, cols, rows)
            self.day_scroll_offset = rendered.day_scroll_offset
        self._finance_day_rows = {2 + k: v for k, v in rendered.day_rows.items()}
        self._finance_item_rows = {2 + k: v for k, v in rendered.item_rows.items()}
        self._draw_styled_lines(2, 1, rendered.lines, max_y, max_x)

    def draw_bills_overview_view(self, max_y: int, max_x: int) -> None:
        """Renders the Recurring Fixed Bills table (sorted by due date) & Monthly Overview (Notion formulas)."""
        bb = getattr(self, "finance_bills_breakdown", {}) or {}
        bills = bb.get("bills", [])
        fixed_sum_cents = bb.get("fixed_bills_total_cents", 0)
        overview = bb.get("overview") or self.finance_overview
        pc = self.finance_paycheck

        # Dimensions: Dual column layout
        split_x = min(54, max(36, int(max_x * 0.48)))
        right_x = split_x + 2
        right_w = max(10, max_x - right_x - 1)

        try:
            # Vertical divider between tables
            for y in range(7, max_y - 2):
                try:
                    self.stdscr.addstr(y, split_x, "│", curses.color_pair(8))
                except curses.error:
                    pass

            # -------------------------------------------------------------
            # Left Column: Recurring Bills Table (Sorted by Due Date 1-31)
            # -------------------------------------------------------------
            left_hdr = " [v] RECURRING BILLS (By Due Date) "
            self._draw_line_padded(7, 1, split_x - 1, left_hdr, curses.color_pair(7) | curses.A_BOLD)

            # Table subheader at Line 8
            if split_x >= 50:
                hdr_text = f"  {'DUE':<4} {'BILL / OBLIGATION':<18} {'CATEGORY':<11} {'STATUS':<7} {'AMOUNT':>7}"
            else:
                hdr_text = f"  {'DUE':<4} {'BILL':<14} {'STATUS':<7} {'AMOUNT':>7}"
            self._draw_line_padded(8, 1, split_x - 1, hdr_text, curses.color_pair(2) | curses.A_BOLD)

            # Available rows for bills list
            avail_rows = max(1, max_y - 12)
            visible_bills = bills[self.bills_scroll_offset : self.bills_scroll_offset + avail_rows]

            for idx, b in enumerate(visible_bills):
                actual_idx = self.bills_scroll_offset + idx
                row_y = 9 + idx
                if row_y >= max_y - 3:
                    break

                is_sel = (actual_idx == self.selected_bill_idx)
                due_day_str = f"{b['due_day']:02d}"
                name_str = b["name"][:18]
                cat_str = b["category"][:11]
                stat_text = b["status_text"][:7]
                amt_str = self._fmt_money(b["amount_cents"], self.mask_amounts)

                if split_x >= 50:
                    row_line = f"  {due_day_str:<4} {name_str:<18} {cat_str:<11} {stat_text:<7} {amt_str:>7}"
                else:
                    row_line = f"  {due_day_str:<4} {name_str[:14]:<14} {stat_text[:7]:<7} {amt_str:>7}"

                if is_sel:
                    self._draw_line_padded(row_y, 1, split_x - 1, row_line, curses.color_pair(7) | curses.A_BOLD)
                else:
                    self._draw_line_padded(row_y, 1, split_x - 1, row_line, curses.color_pair(8))

            # Blank out empty rows in left column down to max_y - 3
            for ey in range(9 + len(visible_bills), max_y - 3):
                self._draw_line_padded(ey, 1, split_x - 1, "", curses.color_pair(8))

            # Left column bottom sum (Line max_y - 2)
            self._draw_line_padded(max_y - 3, 1, split_x - 1, "─" * (split_x - 1), curses.color_pair(8))
            sum_str = self._fmt_money(fixed_sum_cents, self.mask_amounts)
            sum_line = f"  SUM: {sum_str} Fixed Bills (Excl. PG&E)"
            self._draw_line_padded(max_y - 2, 1, split_x - 1, sum_line, curses.color_pair(5) | curses.A_BOLD)

            # -------------------------------------------------------------
            # Right Column: Monthly Overview & Notion Formulas
            # -------------------------------------------------------------
            self._draw_line_padded(7, right_x, right_w, "📊 MONTHLY OVERVIEW & FORMULAS (Notion)", curses.color_pair(1) | curses.A_BOLD)
            self._draw_line_padded(8, right_x, right_w, "─" * right_w, curses.color_pair(8))

            if overview:
                m_label = overview.month
                st_label = overview.status.upper()
                inc_str = self._fmt_money(overview.income_cents, self.mask_amounts)
                sav_str = self._fmt_money(overview.savings_target_cents, self.mask_amounts)
                fix_str = self._fmt_money(overview.fixed_bills_cents, self.mask_amounts)
                pge_str = self._fmt_money(overview.pge_cents, self.mask_amounts)
                all_b_str = self._fmt_money(overview.all_bills_cents, self.mask_amounts)

                g_act_str = self._fmt_money(overview.groceries_actual_cents, self.mask_amounts)
                g_plan_str = self._fmt_money(overview.groceries_planned_cents, self.mask_amounts)
                g_left_str = self._fmt_money(overview.groceries_left_cents, self.mask_amounts, signed=True)

                gas_act_str = self._fmt_money(overview.gas_actual_cents, self.mask_amounts)
                gas_plan_str = self._fmt_money(overview.gas_planned_cents, self.mask_amounts)
                gas_left_str = self._fmt_money(overview.gas_left_cents, self.mask_amounts, signed=True)

                pers_str = self._fmt_money(overview.total_personal_cents, self.mask_amounts)
                var_str = self._fmt_money(overview.variable_variance_cents, self.mask_amounts, signed=True)
                cush_str = self._fmt_money(overview.remaining_cushion_cents, self.mask_amounts, signed=True)

                # Row 9: Month & Status
                self._draw_line_padded(9, right_x, right_w, f"📅 Month: {m_label} │ Status: [{st_label}]", curses.color_pair(2) | curses.A_BOLD)

                # Inflow & Macro Deductions (Rows 10-14)
                self._draw_line_padded(10, right_x, right_w, f"  💵 Monthly Income:          {inc_str:>12}", curses.color_pair(5))
                self._draw_line_padded(11, right_x, right_w, f"  🎯 Savings Target:         -{sav_str:>12}", curses.color_pair(2))
                self._draw_line_padded(12, right_x, right_w, f"  📌 Fixed Bills Total:       -{fix_str:>12}", curses.color_pair(3))
                self._draw_line_padded(13, right_x, right_w, f"  ⚡ PG&E (Estimate/Actual):  -{pge_str:>12}", curses.color_pair(4))
                self._draw_line_padded(14, right_x, right_w, f"  🏷️ All Bills (Fixed + PG&E): {all_b_str:>12}", curses.color_pair(8) | curses.A_BOLD)

                # Variable Expenses Section (Rows 15-20)
                if max_y >= 22:
                    self._draw_line_padded(15, right_x, right_w, "  " + "─" * max(1, right_w - 4), curses.color_pair(8))
                    
                    # Helper for mini gauges
                    def _mini_gauge(actual_c: int, planned_c: int, w: int = 8) -> str:
                        if planned_c <= 0:
                            return "[" + "░" * w + "]"
                        f = min(w, max(0, int(round((actual_c / planned_c) * w))))
                        return f"[{'█' * f}{'░' * (w - f)}]"

                    g_gauge = _mini_gauge(overview.groceries_actual_cents, overview.groceries_planned_cents)
                    gas_gauge = _mini_gauge(overview.gas_actual_cents, overview.gas_planned_cents)

                    self._draw_line_padded(16, right_x, right_w, f"  🛒 Groceries: {g_gauge} {g_act_str} / {g_plan_str} (Left: {g_left_str})", curses.color_pair(8))
                    self._draw_line_padded(17, right_x, right_w, f"  ⛽ Gas/Fuel:  {gas_gauge} {gas_act_str} / {gas_plan_str} (Left: {gas_left_str})", curses.color_pair(8))
                    self._draw_line_padded(18, right_x, right_w, "  " + "─" * max(1, right_w - 4), curses.color_pair(8))

                    # Notion Formula Outputs (Rows 19-21)
                    self._draw_line_padded(19, right_x, right_w, f"  👤 Planned Total Personal:  {pers_str:>12}", curses.color_pair(1) | curses.A_BOLD)
                    var_pair = curses.color_pair(5) if overview.variable_variance_cents >= 0 else curses.color_pair(3)
                    self._draw_line_padded(20, right_x, right_w, f"  📈 Variable Variance:       {var_str:>12}", var_pair | curses.A_BOLD)
                    cush_pair = curses.color_pair(5) if overview.remaining_cushion_cents >= 0 else curses.color_pair(3)
                    self._draw_line_padded(21, right_x, right_w, f"  🛡️ Remaining Cushion:       {cush_str:>12}", cush_pair | curses.A_BOLD)

                    # Section 4: Upcoming 7-Day Cash Call Warning (Rows 22+)
                    curr_y = 22
                    if pc and (pc.bills or getattr(pc, "paid_bills", None)) and max_y >= 26:
                        self._draw_line_padded(curr_y, right_x, right_w, "  " + "─" * max(1, right_w - 4), curses.color_pair(8))
                        curr_y += 1
                        # Whole pay cycle (last payday to next payday): runway chart plus what is due each day
                        spare = 1 if overview.notes else 0
                        detail = build_cycle_detail(self._finance_view_data(), right_w - 3, max_y - 2 - curr_y - spare)
                        self._draw_styled_lines(curr_y, right_x + 2, detail, max_y - 1, right_x + 2 + right_w - 2)
                        curr_y += min(len(detail), max(0, max_y - 2 - curr_y))

                    if overview.notes and curr_y < max_y - 2:
                        self._draw_line_padded(curr_y, right_x, right_w, f"  📝 Notes: {overview.notes}", curses.color_pair(4))
                        curr_y += 1

                    # Wipe any remaining rows down to max_y - 2
                    for ey in range(curr_y, max_y - 2):
                        self._draw_line_padded(ey, right_x, right_w, "", curses.color_pair(8))

        except curses.error:
            pass
        except curses.error:
            pass

    def draw_add_day_modal(self, max_y: int, max_x: int) -> None:
        box_w = min(68, max(40, max_x - 6))
        box_h = min(18, max(12, max_y - 6))
        start_x = max(2, (max_x - box_w) // 2)
        start_y = max(2, (max_y - box_h) // 2)

        try:
            # Clear background of modal
            for y in range(start_y, start_y + box_h):
                self.stdscr.addstr(y, start_x, " " * box_w, curses.color_pair(8))

            # Outer border
            self.stdscr.addstr(start_y, start_x, "┌" + "─" * (box_w - 2) + "┐", curses.color_pair(1) | curses.A_BOLD)
            for y in range(start_y + 1, start_y + box_h - 1):
                self.stdscr.addstr(y, start_x, "│", curses.color_pair(1) | curses.A_BOLD)
                self.stdscr.addstr(y, start_x + box_w - 1, "│", curses.color_pair(1) | curses.A_BOLD)
            self.stdscr.addstr(start_y + box_h - 1, start_x, "└" + "─" * (box_w - 2) + "┘", curses.color_pair(1) | curses.A_BOLD)

            # Title
            title = " ➕ LOG DAILY FINANCES "
            self.stdscr.addstr(start_y, start_x + 3, title, curses.color_pair(7) | curses.A_BOLD)

            if self.modal_stage == "date":
                self.stdscr.addstr(start_y + 2, start_x + 3, "STEP 1: Enter Date for Financial Day", curses.color_pair(2) | curses.A_BOLD)
                prompt = f"Date (YYYY-MM-DD): [{self.modal_date_buffer}█]"
                self.stdscr.addstr(start_y + 4, start_x + 3, prompt[: box_w - 6], curses.color_pair(4) | curses.A_BOLD)
                self.stdscr.addstr(start_y + 6, start_x + 3, "Press [Enter] to confirm date, or [Esc] to cancel.", curses.color_pair(8))
            else:
                self.stdscr.addstr(start_y + 1, start_x + 3, f"Date: {self.modal_date_buffer}", curses.color_pair(5) | curses.A_BOLD)
                self.stdscr.addstr(start_y + 2, start_x + 3, "Type bullet items (e.g. '- groceries - 25.50' or 'payday + 1350'):", curses.color_pair(8))

                lines_avail = max(1, box_h - 8)
                disp_lines = self.modal_batch_lines[-lines_avail:]
                curr_y = start_y + 3
                for idx, line in enumerate(disp_lines):
                    line_text = f"  ✓ {line}"
                    self.stdscr.addstr(curr_y + idx, start_x + 3, line_text[: box_w - 6], curses.color_pair(6))

                input_y = start_y + box_h - 3
                prompt = f"> {self.modal_line_buffer}█"
                self.stdscr.addstr(input_y, start_x + 3, prompt[: box_w - 6], curses.color_pair(4) | curses.A_BOLD)

                hints = "[Enter] Add item  [Ctrl+S / Empty Enter] Save All  [Esc] Cancel"
                self.stdscr.addstr(start_y + box_h - 2, start_x + 3, hints[: box_w - 6], curses.color_pair(2))
        except curses.error:
            pass

    def draw_edit_modal(self, max_y: int, max_x: int) -> None:
        box_w = min(68, max(44, max_x - 6))
        box_h = min(18, max(12, max_y - 6))
        start_x = max(2, (max_x - box_w) // 2)
        start_y = max(2, (max_y - box_h) // 2)

        try:
            # Clear background
            for y in range(start_y, start_y + box_h):
                self.stdscr.addstr(y, start_x, " " * box_w, curses.color_pair(8))

            # Outer border
            self.stdscr.addstr(start_y, start_x, "┌" + "─" * (box_w - 2) + "┐", curses.color_pair(4) | curses.A_BOLD)
            for y in range(start_y + 1, start_y + box_h - 1):
                self.stdscr.addstr(y, start_x, "│", curses.color_pair(4) | curses.A_BOLD)
                self.stdscr.addstr(y, start_x + box_w - 1, "│", curses.color_pair(4) | curses.A_BOLD)
            self.stdscr.addstr(start_y + box_h - 1, start_x, "└" + "─" * (box_w - 2) + "┘", curses.color_pair(4) | curses.A_BOLD)

            # Title
            title = " ✏️ EDIT FINANCES "
            self.stdscr.addstr(start_y, start_x + 3, title, curses.color_pair(7) | curses.A_BOLD)

            if self.editing_stage == "day_menu":
                self.stdscr.addstr(start_y + 1, start_x + 3, f"Date: {self.editing_date}", curses.color_pair(1) | curses.A_BOLD)
                self.stdscr.addstr(start_y + 2, start_x + 3, "Select item to edit, or type 'c' to clear entire day:", curses.color_pair(8))

                curr_day = self.finance_days[self.selected_day_idx]
                items = curr_day.get("items", [])
                lines_avail = max(1, box_h - 7)
                for idx, itm in enumerate(items[:lines_avail]):
                    b_str = self.finance_panel.item_to_bullet(itm)
                    line_disp = f"  [{idx + 1}] {b_str}"
                    self.stdscr.addstr(start_y + 3 + idx, start_x + 3, line_disp[:box_w - 6], curses.color_pair(6))

                prompt_y = start_y + box_h - 3
                prompt = f"Enter # (1-{len(items)}) or 'c' to clear: {self.editing_buffer}█"
                self.stdscr.addstr(prompt_y, start_x + 3, prompt[:box_w - 6], curses.color_pair(4) | curses.A_BOLD)

                hints = "[Enter] Select   [c] Clear Day   [Esc] Cancel"
                self.stdscr.addstr(start_y + box_h - 2, start_x + 3, hints[:box_w - 6], curses.color_pair(2))

            elif self.editing_stage == "item":
                self.stdscr.addstr(start_y + 1, start_x + 3, f"Date: {self.editing_date}", curses.color_pair(1) | curses.A_BOLD)

                # Interactive Account Selector
                acc_names = ["Checking", "Visa", "CapitalOne"]
                acc_tokens = []
                for i, name in enumerate(acc_names):
                    if i == self.editing_account_idx:
                        acc_tokens.append(f"▶ [{name}]")
                    else:
                        acc_tokens.append(f"  {name} ")
                acc_line = "Account: " + "  ".join(acc_tokens)
                self.stdscr.addstr(start_y + 2, start_x + 3, acc_line[:box_w - 6], curses.color_pair(4) | curses.A_BOLD)
                self.stdscr.addstr(start_y + 3, start_x + 3, "(Press [Tab] to cycle account: Checking ⇄ Visa ⇄ CapitalOne)", curses.color_pair(8))

                cat_label = EDIT_CATEGORIES[self.editing_category_idx][0]
                cat_line = f"Category: ◀ {cat_label} ▶"
                if self.editing_category_idx == 0 and self.editing_current_category:
                    cat_line += f"   (now: {self.editing_current_category}, [←/→] to pick)"
                self.stdscr.addstr(start_y + 4, start_x + 3, cat_line[:box_w - 6], curses.color_pair(4) | curses.A_BOLD)

                self.stdscr.addstr(start_y + 5, start_x + 3, "Notion Bullet:", curses.color_pair(2) | curses.A_BOLD)
                input_y = start_y + 6
                prompt = f"> {self.editing_buffer}█"
                self.stdscr.addstr(input_y, start_x + 3, prompt[:box_w - 6], curses.color_pair(1) | curses.A_BOLD)

                self.stdscr.addstr(start_y + 8, start_x + 3, "Examples: - groceries - 25.50   gas - 40   savings - 500", curses.color_pair(8))

                hints_top = "[Enter] Save   [Tab] Account   [←/→] Category"
                hints = "[Ctrl+D] or [Del] Delete this item   [Esc] Cancel"
                self.stdscr.addstr(start_y + box_h - 3, start_x + 3, hints_top[:box_w - 6], curses.color_pair(2))
                self.stdscr.addstr(start_y + box_h - 2, start_x + 3, hints[:box_w - 6], curses.color_pair(2))
        except curses.error:
            pass

    def open_add_modal(self) -> None:
        today_str = datetime.now().strftime("%Y-%m-%d")
        self.is_adding_finances = True
        self.modal_stage = "date"
        self.modal_date_buffer = today_str
        self.modal_line_buffer = ""
        self.modal_batch_lines = []

    def open_edit_modal(self) -> None:
        if not self.finance_days or not (0 <= self.selected_day_idx < len(self.finance_days)):
            self.set_notification("No day selected to edit.")
            return

        curr_day = self.finance_days[self.selected_day_idx]
        items = curr_day.get("items", [])
        if not items:
            self.set_notification(f"No transactions to edit for {curr_day['date']}.")
            return

        self.is_editing_finances = True
        self.editing_date = curr_day["date"]

        if self.finances_focus == "items" or len(items) == 1:
            idx = min(self.selected_item_idx, len(items) - 1)
            self._begin_item_edit(items[idx])
        else:
            self.editing_stage = "day_menu"
            self.editing_buffer = ""

    def _begin_item_edit(self, target: Dict[str, Any]) -> None:
        """Loads one ledger item into the edit modal (text, account, category)."""
        self.editing_stage = "item"
        self.editing_tx_id = target["id"]
        bullet_text = self.finance_panel.item_to_bullet(target)
        self.editing_orig_text = bullet_text
        self.editing_buffer = bullet_text
        # Auto = let the text decide; the user can pick a category explicitly with Left/Right.
        self.editing_category_idx = 0
        self.editing_current_category = str(target.get("category") or "")

        acc_str = str(target.get("account", "")).lower()
        if "checking" in acc_str or "wells" in acc_str:
            self.editing_account_idx = 0
        elif "visa" in acc_str or "card" in acc_str:
            self.editing_account_idx = 1
        elif "savings" in acc_str or "capital" in acc_str:
            self.editing_account_idx = 2
        else:
            self.editing_account_idx = 0

    def handle_modal_key(self, ch: int) -> None:
        if ch in (27,):  # ESC
            self.is_adding_finances = False
            self.set_notification("Cancelled daily entry.")
            return

        if self.modal_stage == "date":
            if ch in (10, 13, curses.KEY_ENTER):
                date_clean = self.modal_date_buffer.strip()
                if not date_clean:
                    date_clean = datetime.now().strftime("%Y-%m-%d")
                self.modal_date_buffer = date_clean
                self.modal_stage = "bullets"
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if self.modal_date_buffer:
                    self.modal_date_buffer = self.modal_date_buffer[:-1]
            elif 32 <= ch <= 126:
                self.modal_date_buffer += chr(ch)

        elif self.modal_stage == "bullets":
            if ch == 19:  # Ctrl+S
                self._commit_modal_bullets()
            elif ch in (10, 13, curses.KEY_ENTER):
                line = self.modal_line_buffer.strip()
                if line:
                    self.modal_batch_lines.append(line)
                    self.modal_line_buffer = ""
                else:
                    if self.modal_batch_lines:
                        self._commit_modal_bullets()
                    else:
                        self.is_adding_finances = False
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if self.modal_line_buffer:
                    self.modal_line_buffer = self.modal_line_buffer[:-1]
            elif 32 <= ch <= 126:
                self.modal_line_buffer += chr(ch)

    def handle_edit_modal_key(self, ch: int) -> None:
        if ch in (27,):  # ESC
            self.is_editing_finances = False
            self.set_notification("Edit cancelled.")
            return

        curr_day = self.finance_days[self.selected_day_idx]
        items = curr_day.get("items", [])

        if self.editing_stage == "day_menu":
            if ch in (10, 13, curses.KEY_ENTER):
                cmd = self.editing_buffer.strip().lower()
                if cmd in ("c", "clear", "del", "delete"):
                    count = self.finance_panel.delete_day(self.editing_date)
                    self.is_editing_finances = False
                    self.refresh_data()
                    self.set_notification(f"🗑️ Cleared all {count} transactions for {self.editing_date}.")
                    return
                elif cmd.isdigit():
                    num = int(cmd)
                    if 1 <= num <= len(items):
                        self.selected_item_idx = num - 1
                        self._begin_item_edit(items[num - 1])
                        return
                    else:
                        self.set_notification(f"Enter a number between 1 and {len(items)}")
                        self.editing_buffer = ""
                        return
                else:
                    self.set_notification("Type item number (e.g. 1) or 'c' to clear day.")
                    self.editing_buffer = ""
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if self.editing_buffer:
                    self.editing_buffer = self.editing_buffer[:-1]
            elif 32 <= ch <= 126:
                self.editing_buffer += chr(ch)

        elif self.editing_stage == "item":
            if ch in (9, ord("\t")):  # Tab cycles account
                self.editing_account_idx = (self.editing_account_idx + 1) % 3
                acc_name = ["Checking", "Visa", "CapitalOne"][self.editing_account_idx]
                self.set_notification(f"Funding Account changed to: {acc_name}")
                return
            elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT):
                step = 1 if ch == curses.KEY_RIGHT else -1
                self.editing_category_idx = (self.editing_category_idx + step) % len(EDIT_CATEGORIES)
                self.set_notification(f"Category: {EDIT_CATEGORIES[self.editing_category_idx][0]}")
                return
            elif ch in (10, 13, curses.KEY_ENTER):
                new_text = self.editing_buffer.strip()
                if not new_text:
                    self.is_editing_finances = False
                    return
                acc_choice = ["Checking", "Visa", "CapitalOne"][self.editing_account_idx]
                cat_label, cat_key = EDIT_CATEGORIES[self.editing_category_idx]
                res = self.finance_panel.edit_transaction(
                    self.editing_tx_id,
                    self.editing_date,
                    new_text,
                    account_choice=acc_choice,
                    category_choice=cat_key,
                )
                self.is_editing_finances = False
                self.refresh_data()
                if res:
                    cat_note = f" -> {cat_label}" if cat_key else ""
                    self.set_notification(f"✅ Updated transaction: [{acc_choice}]{cat_note} {new_text}")
                else:
                    self.set_notification("❌ Could not parse bullet. Example: - groceries - 25.50")
            elif ch in (4, curses.KEY_DC) or (ch in (ord("d"), ord("D")) and not self.editing_buffer):
                # Ctrl+D / Delete key always delete; plain 'd' is text while the buffer is pre-filled.
                self.finance_panel.delete_transaction(self.editing_tx_id)
                self.is_editing_finances = False
                self.refresh_data()
                self.set_notification("🗑️ Deleted transaction.")
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if self.editing_buffer:
                    self.editing_buffer = self.editing_buffer[:-1]
            elif 32 <= ch <= 126:
                self.editing_buffer += chr(ch)

    def delete_selected_finance(self) -> None:
        if not self.finance_days or not (0 <= self.selected_day_idx < len(self.finance_days)):
            return
        curr_day = self.finance_days[self.selected_day_idx]
        items = curr_day.get("items", [])
        if not items:
            return

        if self.finances_focus == "items":
            idx = min(self.selected_item_idx, len(items) - 1)
            target = items[idx]
            self.finance_panel.delete_transaction(target["id"])
            self.refresh_data()
            if self.selected_item_idx >= len(items) - 1:
                self.selected_item_idx = max(0, len(items) - 2)
            self.set_notification(f"🗑️ Deleted {target['payee']} (${abs(target['amount_cents'])/100:.2f})")
        else:
            count = self.finance_panel.delete_day(curr_day["date"])
            self.refresh_data()
            self.set_notification(f"🗑️ Cleared all {count} transactions for {curr_day['date']}.")

    def _commit_modal_bullets(self) -> None:
        if self.modal_line_buffer.strip():
            self.modal_batch_lines.append(self.modal_line_buffer.strip())
            self.modal_line_buffer = ""

        if not self.modal_batch_lines:
            self.is_adding_finances = False
            return

        date_str = self.modal_date_buffer
        text = "\n".join(self.modal_batch_lines)
        logged = self.finance_panel.log_day_bullets(date_str, text)
        self.is_adding_finances = False
        self.refresh_data()
        self.set_notification(f"✅ Logged {len(logged)} items for {date_str} successfully!")

    def draw_bottom_bar(self, max_y: int, max_x: int) -> None:
        try:
            if self.notification and time.time() < self.notification_expiry:
                self.stdscr.addstr(max_y - 1, 1, f"🔔 {self.notification}"[: max_x - 2], curses.color_pair(4) | curses.A_BOLD)
            else:
                if self.current_tab == 4:
                    mode = self.finances_view_mode
                    if mode == "bills":
                        hint = " [j/k] move   [v] overview   [m] mask   [r] refresh   [q] quit"
                    elif mode == "day":
                        hint = " [j/k] move   [e] edit   [d] delete   [a] add   [esc] back   [m] mask   [q] quit"
                    elif mode == "math":
                        hint = " [esc] back   [m] mask   [q] quit"
                    else:
                        hint = " [j/k] move  [enter] open  [i] math  [a] add  [e] edit  [v] bills  [m] mask  [q] quit"
                else:
                    hint = " [↑/↓/j/k]: Navigate  [1-4]: Views  [t]: Triage in OpenCode  [r]: Refresh  [q]: Exit"
                self.stdscr.addstr(max_y - 1, 1, hint[: max_x - 2], curses.color_pair(1))
        except curses.error:
            pass

    def _ui_state(self) -> tuple:
        """Snapshot of everything a mouse event can change on screen."""
        return (
            self.current_tab,
            self.selected_email_idx,
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
        """Moves the highlighted row for whichever Finances screen is showing."""
        mode = self.finances_view_mode
        if mode == "bills":
            bills = (self.finance_bills_breakdown or {}).get("bills", [])
            if not bills:
                return
            self.selected_bill_idx = max(0, min(len(bills) - 1, self.selected_bill_idx + delta))
            if self.selected_bill_idx < self.bills_scroll_offset:
                self.bills_scroll_offset = self.selected_bill_idx
            elif self.selected_bill_idx >= self.bills_scroll_offset + avail_rows:
                self.bills_scroll_offset = self.selected_bill_idx - avail_rows + 1
        elif mode == "day":
            items = self.finance_days[self.selected_day_idx]["items"] if self.finance_days else []
            if items:
                self.selected_item_idx = max(0, min(len(items) - 1, self.selected_item_idx + delta))
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
        elif mode == "bills" and 9 <= my <= 35:
            bills = (self.finance_bills_breakdown or {}).get("bills", [])
            clicked = self.bills_scroll_offset + (my - 9)
            if 0 <= clicked < len(bills):
                self.selected_bill_idx = clicked

    def _open_selected_day(self) -> None:
        if self.finance_days:
            self.selected_item_idx = 0
            self._set_finance_mode("day")

    def handle_finance_key(self, ch: int, max_y: int) -> None:
        mode = self.finances_view_mode
        if ch in (ord("m"), ord("M")):
            self.mask_amounts = not self.mask_amounts
            self.set_notification(f"Finance amounts {'masked' if self.mask_amounts else 'unmasked'}.")
        elif ch in (ord("v"), ord("V")):
            self._set_finance_mode("daily" if mode == "bills" else "bills")
        elif ch in (ord("i"), ord("I")) and mode != "bills":
            self._set_finance_mode("daily" if mode == "math" else "math")
        elif ch == 27 and mode in ("day", "math", "bills"):
            self._set_finance_mode("daily")
        elif ch in (curses.KEY_UP, ord("k")):
            self._move_finance_selection(-1, max(1, max_y - 12))
        elif ch in (curses.KEY_DOWN, ord("j")):
            self._move_finance_selection(1, max(1, max_y - 12))
        elif mode == "daily" and ch in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT, ord("l")):
            self._open_selected_day()
        elif mode == "day" and ch in (curses.KEY_LEFT, ord("h")):
            self._set_finance_mode("daily")
        elif mode in ("daily", "day"):
            if ch in (ord("a"), ord("A")):
                self.open_add_modal()
            elif ch in (ord("e"), ord("E")):
                self.open_edit_modal()
            elif ch in (ord("d"), ord("D")):
                if mode == "day":
                    self.delete_selected_finance()
                else:
                    self.set_notification("Press enter to open the day, then d to delete an item (or e to clear the day).")

    def handle_mouse(self) -> None:
        try:
            _, mx, my, _, bstate = curses.getmouse()
            # Click on top tabs
            if my == 1:
                if 1 <= mx <= 15:
                    self.current_tab = 1
                elif 17 <= mx <= 34:
                    self.current_tab = 2
                elif 36 <= mx <= 53:
                    self.current_tab = 3
                elif 55 <= mx <= 70:
                    self.current_tab = 4
                return

            # Click or scroll in email list
            if self.current_tab == 1 and 5 <= my <= 20:
                clicked_idx = self.email_scroll_offset + (my - 5)
                if clicked_idx < len(self.emails):
                    self.selected_email_idx = clicked_idx

            # Click in finance view
            if self.current_tab == 4:
                self._handle_finance_click(my)

            # Trackpad scroll up / down
            if bstate & curses.BUTTON4_PRESSED:  # Scroll Up
                if self.current_tab == 1:
                    self.selected_email_idx = max(0, self.selected_email_idx - 1)
                elif self.current_tab == 4:
                    self._move_finance_selection(-1)
            elif bstate & curses.BUTTON2_PRESSED or bstate & (1 << 21):  # Scroll Down
                if self.current_tab == 1:
                    self.selected_email_idx = min(len(self.emails) - 1, self.selected_email_idx + 1)
                elif self.current_tab == 4:
                    self._move_finance_selection(1)
        except Exception:
            pass

    def run(self) -> None:
        """Main rendering and event loop."""
        last_refresh = time.time()
        last_telemetry = time.time()
        prev_tab = self.current_tab
        prev_view_mode = getattr(self, "finances_view_mode", "daily")
        prev_modal_state = (self.is_adding_finances, self.is_editing_finances)
        needs_render = True

        while True:
            now = time.time()

            # Mode / Tab / Modal transition detection
            curr_mode = getattr(self, "finances_view_mode", "daily")
            curr_modal_state = (self.is_adding_finances, self.is_editing_finances)
            if self.current_tab != prev_tab or curr_mode != prev_view_mode or curr_modal_state != prev_modal_state:
                self.stdscr.clearok(True)
                self.stdscr.clear()
                prev_tab = self.current_tab
                prev_view_mode = curr_mode
                prev_modal_state = curr_modal_state
                needs_render = True

            # Deferred full repaint after a resize burst
            if self._settle_repaint_at and now >= self._settle_repaint_at:
                self._settle_repaint_at = 0.0
                self._hard_reset_screen()
                needs_render = True

            # Telemetry & Clock tick (every 1.0 second)
            if now - last_telemetry >= 1.0:
                last_telemetry = now
                needs_render = True

            # Auto refresh background data every 10 seconds
            if now - last_refresh > 10.0:
                self.refresh_data()
                last_refresh = now
                needs_render = True

            # Notification auto-expiry check
            if self.notification and now >= self.notification_expiry:
                self.notification = ""
                needs_render = True

            # Render frame only when state or periodic timer requires it
            if needs_render:
                frame_start = time.time()
                max_y, max_x = self.stdscr.getmaxyx()

                if max_y < 8 or max_x < 24:
                    try:
                        self.stdscr.erase()
                        self.stdscr.addstr(0, 0, "Terminal window too small for Cockpit.")
                        self.stdscr.refresh()
                    except curses.error:
                        pass
                else:
                    self.stdscr.erase()
                    self.draw_top_telemetry(max_x)
                    self.draw_tabs(max_x)

                    if self.current_tab == 1:
                        self.draw_daily_ops(max_y, max_x)
                    elif self.current_tab == 2:
                        self.draw_memory_vault_view(max_y, max_x)
                    elif self.current_tab == 3:
                        self.draw_safety_guard_view(max_y, max_x)
                    elif self.current_tab == 4:
                        self.draw_finances_view(max_y, max_x)

                    if self.is_adding_finances:
                        self.draw_add_day_modal(max_y, max_x)
                    elif self.is_editing_finances:
                        self.draw_edit_modal(max_y, max_x)

                    self.draw_bottom_bar(max_y, max_x)
                    self.stdscr.refresh()

                self._debug(f"frame {max_y}x{max_x} draw_ms={(time.time() - frame_start) * 1000:.1f}")
                needs_render = False

            # Wait up to 50ms for user input (OS poll - zero CPU when idle)
            try:
                ch = self.stdscr.getch()
            except curses.error:
                ch = -1

            if ch == -1:
                continue
            self._debug(f"key {ch}")

            # Key received! Flag next frame for render (mouse events decide below)
            needs_render = ch != curses.KEY_MOUSE

            if ch == curses.KEY_RESIZE:
                # Only call curses.resizeterm() when the real tty size differs from what curses
                # has. Calling it unconditionally queues another KEY_RESIZE every time, which
                # loops forever (screen flashing, "4" and "q" never reach the handler).
                try:
                    tty = os.get_terminal_size(sys.stdout.fileno())
                    if (tty.lines, tty.columns) != tuple(self.stdscr.getmaxyx()):
                        curses.resizeterm(tty.lines, tty.columns)
                except (OSError, ValueError, curses.error):
                    pass
                new_size = self.stdscr.getmaxyx()
                if new_size == self._last_size:
                    # tmux can send repeated resize signals with no real change.
                    # A full clear() here would flash the whole screen each time.
                    needs_render = False
                    continue
                self._last_size = new_size
                self.stdscr.clearok(True)
                self.stdscr.clear()
                # tmux resizes and re-splits panes in bursts while attaching. Repaint once more
                # after the burst settles, so the screen can't stay out of sync with curses.
                self._settle_repaint_at = time.time() + 0.4
                continue

            if self.is_adding_finances:
                self.handle_modal_key(ch)
                continue
            elif self.is_editing_finances:
                self.handle_edit_modal_key(ch)
                continue

            if ch == curses.KEY_MOUSE:
                before = self._ui_state()
                self.handle_mouse()
                needs_render = self._ui_state() != before
            elif ch in (ord("q"), ord("Q")):
                break
            elif ch in (ord("1"),):
                self.current_tab = 1
            elif ch in (ord("2"),):
                self.current_tab = 2
            elif ch in (ord("3"),):
                self.current_tab = 3
            elif ch in (ord("4"),):
                self.current_tab = 4
            elif ch in (ord("r"), ord("R")):
                self.refresh_data()
                self.set_notification("Data refreshed from local stores.")
            elif self.current_tab == 1:
                if ch in (curses.KEY_UP, ord("k")):
                    if self.selected_email_idx > 0:
                        self.selected_email_idx -= 1
                        if self.selected_email_idx < self.email_scroll_offset:
                            self.email_scroll_offset = self.selected_email_idx
                elif ch in (curses.KEY_DOWN, ord("j")):
                    if self.selected_email_idx < len(self.emails) - 1:
                        self.selected_email_idx += 1
                        split_y = max(10, int(max_y * 0.58))
                        avail = split_y - 6
                        if self.selected_email_idx >= self.email_scroll_offset + avail:
                            self.email_scroll_offset += 1
                elif ch in (ord("t"), ord("T")):
                    self.trigger_opencode_action("Triage my unread emails and summarize urgent items using telemachus-email")
            elif self.current_tab == 4:
                self.handle_finance_key(ch, max_y)


def main():
    """Terminal entry point."""
    try:
        curses.wrapper(lambda scr: CockpitTUI(scr).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
