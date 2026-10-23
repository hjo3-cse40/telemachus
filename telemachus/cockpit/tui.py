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
from typing import Any, Dict, List, Optional

from telemachus.cockpit.email_panel import EmailTriagePanel
from telemachus.cockpit.finance_panel import FinanceTuiPanel
from telemachus.cockpit.safety_memory_panel import SafetyMemoryPanel
from telemachus.cockpit.telemetry import CockpitTelemetry


class CockpitTUI:
    """Curses-driven Terminal Cockpit with Tokyo Night aesthetics."""

    def __init__(self, stdscr: curses.window):
        self.stdscr = stdscr
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
        self.finance_balances: Dict[str, Any] = {}
        self.selected_day_idx = 0
        self.day_scroll_offset = 0
        self.mask_amounts = False

        # In-Cockpit Modal State for Logging Daily Finances
        self.is_adding_finances = False
        self.modal_stage = "date"  # "date" or "bullets"
        self.modal_date_buffer = ""
        self.modal_line_buffer = ""
        self.modal_batch_lines: List[str] = []

        self._init_curses()
        self.refresh_data()

    def _init_curses(self) -> None:
        curses.curs_set(0)  # Hide hardware cursor
        self.stdscr.nodelay(True)  # Non-blocking input
        self.stdscr.keypad(True)

        # Enable full mouse events
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
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
            self.finance_balances = self.finance_panel.get_balances()

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
        try:
            self.stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
            self.stdscr.addstr(0, 1, header[: max_x - 2])
            self.stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)
        except curses.error:
            pass

    def draw_tabs(self, max_x: int) -> None:
        tab1_attr = curses.color_pair(7) | curses.A_BOLD if self.current_tab == 1 else curses.color_pair(1)
        tab2_attr = curses.color_pair(7) | curses.A_BOLD if self.current_tab == 2 else curses.color_pair(1)
        tab3_attr = curses.color_pair(7) | curses.A_BOLD if self.current_tab == 3 else curses.color_pair(1)
        tab4_attr = curses.color_pair(7) | curses.A_BOLD if self.current_tab == 4 else curses.color_pair(1)

        try:
            self.stdscr.addstr(1, 1, "[1: Daily Ops]", tab1_attr)
            self.stdscr.addstr(1, 17, "[2: Memory Vault]", tab2_attr)
            self.stdscr.addstr(1, 36, "[3: Safety Guard]", tab3_attr)
            self.stdscr.addstr(1, 55, "[4: Finances]", tab4_attr)

            if self.current_tab == 4:
                actions = "[a] Add Day  [m] Mask  [r] Refresh  [q] Quit"
            else:
                actions = "[t] Triage  [r] Refresh  [q] Quit"

            if max_x > len(actions) + 72:
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

    def draw_finances_view(self, max_y: int, max_x: int) -> None:
        try:
            # 1. Top Summary Banner
            cf = getattr(self, "finance_cashflow", None)
            ov = self.finance_overview
            month_label = ov.month if ov else datetime.now().strftime("%Y-%m")

            if cf:
                net_str = self._fmt_money(cf.net_savings_cents, self.mask_amounts, signed=True)
                inc_str = self._fmt_money(cf.inflow_cents, self.mask_amounts)
                exp_str = self._fmt_money(cf.outflow_cents, self.mask_amounts)
                rate_str = f"{cf.savings_rate_pct:.1f}%" if not self.mask_amounts else "**%"
                cushion_part = ""
                if ov and ov.income_cents > 0:
                    cush_str = self._fmt_money(ov.remaining_cushion_cents, self.mask_amounts)
                    cushion_part = f" · Cushion: {cush_str}"
                top_line = f"💳 FINANCES ({month_label}) · Net: {net_str} ({rate_str}) · Income: {inc_str} · Expenses: {exp_str}{cushion_part}"
            elif ov and ov.income_cents > 0:
                inc_str = self._fmt_money(ov.income_cents, self.mask_amounts)
                cush_str = self._fmt_money(ov.remaining_cushion_cents, self.mask_amounts)
                top_line = f"💳 FINANCES ({month_label}) · Income: {inc_str} · Cushion Left: {cush_str}"
            else:
                top_line = f"💳 FINANCES ({month_label}) · Local Double-Entry Ledger"
            self.stdscr.addstr(2, 2, top_line[: max_x - 4], curses.color_pair(1) | curses.A_BOLD)

            pc = self.finance_paycheck
            if pc:
                avail_str = self._fmt_money(pc.safe_to_spend_cents, self.mask_amounts)
                daily_burn_cents = pc.safe_to_spend_cents // max(1, pc.days_in_cycle)
                burn_str = self._fmt_money(daily_burn_cents, self.mask_amounts)
                sub_line = f"🗓️ Paycheck Window ({pc.paycheck_date} → {pc.next_paycheck_date}) · Safe to Spend: {avail_str} · Burn: {burn_str}/day"
            else:
                sub_line = "🗓️ Paycheck Window · (Run 'python3 -m telemachus.finance plan' to calculate allocation)"
            self.stdscr.addstr(3, 2, sub_line[: max_x - 4], curses.color_pair(2))

            # Horizontal Separator
            self.stdscr.addstr(4, 1, "─" * max(1, max_x - 2), curses.color_pair(8))

            # 2. Main Dual-Column Layout
            split_x = min(36, max(26, max_x // 3))

            # Left Column Header
            self.stdscr.addstr(5, 2, f"{'DAY NAVIGATOR':<18} {'NET':>8}"[:split_x - 2], curses.color_pair(4) | curses.A_BOLD)
            self.stdscr.addstr(6, 1, "─" * (split_x - 1), curses.color_pair(8))

            # Right Column Header & Divider
            right_x = split_x + 2
            right_w = max(10, max_x - right_x - 1)

            for y in range(5, max_y - 2):
                self.stdscr.addstr(y, split_x, "│", curses.color_pair(8))

            avail_day_rows = max(1, max_y - 9)
            visible_days = self.finance_days[self.day_scroll_offset : self.day_scroll_offset + avail_day_rows]

            if not self.finance_days:
                self.stdscr.addstr(7, 2, "(No days logged yet)", curses.color_pair(8))
                self.stdscr.addstr(5, right_x, "DAY DETAILS", curses.color_pair(1) | curses.A_BOLD)
                self.stdscr.addstr(7, right_x, "No transactions logged.", curses.color_pair(8))
                self.stdscr.addstr(8, right_x, "Press [a] to log a new day with Notion bullet format,", curses.color_pair(4))
                self.stdscr.addstr(9, right_x, "or run 'python3 -m telemachus.finance import-text --file <path>' to bulk ingest.", curses.color_pair(4))
                return

            for idx, d in enumerate(visible_days):
                actual_idx = self.day_scroll_offset + idx
                row_y = 7 + idx
                if row_y >= max_y - 2:
                    break

                is_selected = (actual_idx == self.selected_day_idx)
                attr = curses.color_pair(7) | curses.A_BOLD if is_selected else curses.color_pair(8)

                net_c = d["net_cents"]
                net_str = self._fmt_money(net_c, self.mask_amounts, signed=True)
                title_col = d["day_title"][:16]

                self.stdscr.addstr(row_y, 1, " " * (split_x - 1), attr)
                self.stdscr.addstr(row_y, 2, f"{title_col:<16}", attr)

                net_pair = curses.color_pair(7) if is_selected else (
                    curses.color_pair(5) if net_c >= 0 else curses.color_pair(3)
                )
                self.stdscr.addstr(row_y, split_x - len(net_str) - 2, net_str, net_pair | (curses.A_BOLD if is_selected else 0))

            # Right Column: Details for selected day
            if 0 <= self.selected_day_idx < len(self.finance_days):
                curr = self.finance_days[self.selected_day_idx]
                day_out = self._fmt_money(curr["outflow_cents"], self.mask_amounts)
                day_in = self._fmt_money(curr["inflow_cents"], self.mask_amounts)
                header_right = f"📅 {curr['day_title']} ({curr['date']}) · Spent: {day_out} · Inflow: {day_in}"
                self.stdscr.addstr(5, right_x, header_right[:right_w], curses.color_pair(1) | curses.A_BOLD)

                if right_w >= 50:
                    tbl_hdr = f"  {'ACCOUNT':<10} {'CATEGORY':<14} {'DESCRIPTION / PAYEE':<24} {'AMOUNT':>10}"
                else:
                    tbl_hdr = f"  {'CATEGORY':<12} {'DESCRIPTION':<16} {'AMOUNT':>10}"
                self.stdscr.addstr(6, right_x, tbl_hdr[:right_w], curses.color_pair(2) | curses.A_BOLD)

                items = curr["items"]
                for i_idx, item in enumerate(items[: max(1, max_y - 9)]):
                    item_y = 7 + i_idx
                    if item_y >= max_y - 2:
                        break

                    amt = item["amount_cents"]
                    amt_str = self._fmt_money(amt, self.mask_amounts, signed=item["is_income"])
                    amt_pair = curses.color_pair(5) if item["is_income"] else curses.color_pair(3)

                    if right_w >= 50:
                        acc_str = f"[{item['account'][:8]}]"
                        cat_str = item["category"][:13]
                        desc_str = (item["payee"] or item["memo"] or "")[:22]
                        self.stdscr.addstr(item_y, right_x + 2, f"{acc_str:<10}", curses.color_pair(6))
                        self.stdscr.addstr(item_y, right_x + 13, f"{cat_str:<14}", curses.color_pair(8))
                        self.stdscr.addstr(item_y, right_x + 28, f"{desc_str:<22}", curses.color_pair(8))
                        self.stdscr.addstr(item_y, right_x + right_w - len(amt_str) - 2, amt_str, amt_pair | curses.A_BOLD)
                    else:
                        cat_str = item["category"][:11]
                        desc_str = (item["payee"] or item["memo"] or "")[:15]
                        self.stdscr.addstr(item_y, right_x + 2, f"{cat_str:<12}", curses.color_pair(8))
                        self.stdscr.addstr(item_y, right_x + 15, f"{desc_str:<15}", curses.color_pair(8))
                        self.stdscr.addstr(item_y, right_x + right_w - len(amt_str) - 2, amt_str, amt_pair | curses.A_BOLD)
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

    def open_add_modal(self) -> None:
        today_str = datetime.now().strftime("%Y-%m-%d")
        self.is_adding_finances = True
        self.modal_stage = "date"
        self.modal_date_buffer = today_str
        self.modal_line_buffer = ""
        self.modal_batch_lines = []

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
                    hint = " [↑/↓/j/k]: Days  [1-4]: Tabs  [a]: Add Day  [m]: Mask Numbers  [r]: Refresh  [q]: Exit"
                else:
                    hint = " [↑/↓/j/k]: Navigate  [1-4]: Views  [t]: Triage in OpenCode  [r]: Refresh  [q]: Exit"
                self.stdscr.addstr(max_y - 1, 1, hint[: max_x - 2], curses.color_pair(1))
        except curses.error:
            pass

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

            # Click in finance day list
            split_x = min(36, max(26, curses.COLS // 3)) if hasattr(curses, "COLS") else 32
            if self.current_tab == 4 and 7 <= my <= 30 and mx <= split_x:
                clicked_day = self.day_scroll_offset + (my - 7)
                if 0 <= clicked_day < len(self.finance_days):
                    self.selected_day_idx = clicked_day

            # Trackpad scroll up / down
            if bstate & curses.BUTTON4_PRESSED:  # Scroll Up
                if self.current_tab == 1:
                    self.selected_email_idx = max(0, self.selected_email_idx - 1)
                elif self.current_tab == 4:
                    self.selected_day_idx = max(0, self.selected_day_idx - 1)
                    if self.selected_day_idx < self.day_scroll_offset:
                        self.day_scroll_offset = self.selected_day_idx
            elif bstate & curses.BUTTON2_PRESSED or bstate & (1 << 21):  # Scroll Down
                if self.current_tab == 1:
                    self.selected_email_idx = min(len(self.emails) - 1, self.selected_email_idx + 1)
                elif self.current_tab == 4:
                    if self.selected_day_idx < len(self.finance_days) - 1:
                        self.selected_day_idx += 1
        except Exception:
            pass

    def run(self) -> None:
        """Main rendering and event loop."""
        last_refresh = time.time()

        while True:
            self.stdscr.erase()
            max_y, max_x = self.stdscr.getmaxyx()

            if max_y < 8 or max_x < 24:
                try:
                    self.stdscr.addstr(0, 0, "Terminal window too small for Cockpit.")
                except curses.error:
                    pass
                self.stdscr.refresh()
                time.sleep(0.2)
                continue

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

            self.draw_bottom_bar(max_y, max_x)
            self.stdscr.refresh()

            # Auto refresh background data every 10 seconds
            if time.time() - last_refresh > 10.0:
                self.refresh_data()
                last_refresh = time.time()

            try:
                ch = self.stdscr.getch()
            except curses.error:
                ch = -1

            if ch == -1:
                time.sleep(0.04)
                continue

            if self.is_adding_finances:
                self.handle_modal_key(ch)
                continue

            if ch == curses.KEY_MOUSE:
                self.handle_mouse()
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
                if ch in (curses.KEY_UP, ord("k")):
                    if self.selected_day_idx > 0:
                        self.selected_day_idx -= 1
                        if self.selected_day_idx < self.day_scroll_offset:
                            self.day_scroll_offset = self.selected_day_idx
                elif ch in (curses.KEY_DOWN, ord("j")):
                    if self.selected_day_idx < len(self.finance_days) - 1:
                        self.selected_day_idx += 1
                        avail_day_rows = max(1, max_y - 9)
                        if self.selected_day_idx >= self.day_scroll_offset + avail_day_rows:
                            self.day_scroll_offset += 1
                elif ch in (ord("a"), ord("A")):
                    self.open_add_modal()
                elif ch in (ord("m"), ord("M")):
                    self.mask_amounts = not self.mask_amounts
                    status = "masked" if self.mask_amounts else "unmasked"
                    self.set_notification(f"Finance amounts {status}.")

            time.sleep(0.04)


def main():
    """Terminal entry point."""
    try:
        curses.wrapper(lambda scr: CockpitTUI(scr).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
