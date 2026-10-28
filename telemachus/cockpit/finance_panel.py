"""Cockpit Finance Panel & Daily Inspector Data Provider.

Provides:
- Daily grouped ledger navigation (Day Navigator + Day Line Items).
- Month-to-date and 2-week paycheck allocation metrics for Cockpit TUI.
- Fast bullet-list parsing and ingestion directly from curses input.
- Database mtime caching to prevent redundant queries on 25 FPS render loops.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple

from telemachus.finance.import_text import parse_bullet_line
from telemachus.finance.models import (
    CashFlowSummary,
    MonthlyOverview,
    PaycheckWindowAllocation,
    Transaction,
)
from telemachus.finance.recurring import RecurringEngine
from telemachus.finance.store import FinanceStore, get_finance_root

logger = logging.getLogger(__name__)

# Categories offered in the edit modal: (label shown, key understood by log_batch_transactions).
# "Auto" (None) lets the bullet text decide, as when adding a transaction.
EDIT_CATEGORIES: List[Tuple[str, Optional[str]]] = [
    ("Auto", None),
    ("Discretionary", "personal"),
    ("Groceries", "groceries"),
    ("Dining", "dining"),
    ("Gas", "gas"),
    ("WiFi", "wifi"),
    ("PG&E", "pge"),
    ("Gym", "gym"),
    ("Spotify", "spotify"),
    ("iCloud", "icloud"),
    ("Brother", "kahram"),
]


PACE_WINDOW_DAYS = 7


@dataclass
class RunwayInfo:
    """How far the money in checking goes before the next paycheck.

    All amounts are cents. `left_cents` is checking minus the bills still due before payday.
    """

    today: str
    next_paycheck: str
    days_left: int
    checking_cents: int
    bills_due_cents: int
    left_cents: int
    per_day_cents: int
    pace_cents: int  # average daily spend over the last PACE_WINDOW_DAYS days
    run_out_date: Optional[str]  # first day the balance would go below zero at the current pace
    allowance_cents: int  # grocery + dining + gas allowance for this pay period
    after_allowances_cents: int  # left_cents minus allowance_cents
    bill_marks: List[Tuple[str, int]] = field(default_factory=list)  # (due date, cents) still due
    month_spent_cents: int = 0


def _rounded_div(cents: int, n: int) -> int:
    """Integer division rounded to the nearest cent (half away from zero)."""
    q, r = divmod(abs(cents), n)
    if r * 2 >= n:
        q += 1
    return q if cents >= 0 else -q


def compute_runway(
    today: str,
    paycheck: PaycheckWindowAllocation,
    days: List[Dict[str, Any]],
) -> RunwayInfo:
    """Pure calculation behind the plain-English hero on the Finances tab."""
    today_dt = datetime.strptime(today, "%Y-%m-%d").date()
    next_dt = datetime.strptime(paycheck.next_paycheck_date, "%Y-%m-%d").date()
    days_left = max(1, (next_dt - today_dt).days)

    checking = paycheck.checking_balance_cents
    left = checking - paycheck.bills_due_cents
    per_day = _rounded_div(left, days_left)

    window_start = today_dt - timedelta(days=PACE_WINDOW_DAYS - 1)
    spent = 0
    for d in days:
        try:
            d_dt = datetime.strptime(d["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if window_start <= d_dt <= today_dt:
            spent += d["outflow_cents"]
    pace = _rounded_div(spent, PACE_WINDOW_DAYS)

    due_by_date: Dict[str, int] = {}
    for b in paycheck.bills:
        if b.get("status") == "due" and b.get("amount_cents", 0) > 0:
            due_by_date[b["due_date"]] = due_by_date.get(b["due_date"], 0) + b["amount_cents"]

    run_out: Optional[str] = None
    if pace > 0:
        balance = checking
        for offset in range(1, days_left + 1):
            day = (today_dt + timedelta(days=offset)).isoformat()
            balance -= pace + due_by_date.get(day, 0)
            if balance < 0:
                run_out = day
                break

    month_prefix = today[:7]
    month_spent = sum(d["outflow_cents"] for d in days if d["date"].startswith(month_prefix))

    return RunwayInfo(
        today=today,
        next_paycheck=paycheck.next_paycheck_date,
        days_left=days_left,
        checking_cents=checking,
        bills_due_cents=paycheck.bills_due_cents,
        left_cents=left,
        per_day_cents=per_day,
        pace_cents=pace,
        run_out_date=run_out,
        allowance_cents=paycheck.variable_needs_cents,
        after_allowances_cents=left - paycheck.variable_needs_cents,
        bill_marks=sorted(due_by_date.items()),
        month_spent_cents=month_spent,
    )


class FinanceTuiPanel:
    """Manages finance data feeds, caching, and mutations for the Cockpit TUI."""

    def __init__(self, store: Optional[FinanceStore] = None):
        self.store = store or FinanceStore()
        self.recurring = RecurringEngine(self.store)

        self._last_mtime: float = 0.0
        self._last_setup_mtime: float = 0.0
        self._cached_days: List[Dict[str, Any]] = []
        self._cached_overview: Optional[MonthlyOverview] = None
        self._cached_cashflow: Optional[CashFlowSummary] = None
        self._cached_paycheck: Optional[PaycheckWindowAllocation] = None
        self._cached_balances: Dict[str, Any] = {}
        self._cache_time: float = 0.0

    def _is_cache_stale(self) -> bool:
        """Checks if SQLite database file or setup.yaml modified or cache TTL expired (10s)."""
        now = time.time()
        if now - self._cache_time > 10.0:
            return True
        db_path = self.store.db_path
        if db_path.exists():
            try:
                mtime = db_path.stat().st_mtime
                if mtime != self._last_mtime:
                    self._last_mtime = mtime
                    return True
            except OSError:
                pass
        setup_p = self.store.get_setup_yaml_path()
        if setup_p and setup_p.exists():
            try:
                s_mtime = setup_p.stat().st_mtime
                if s_mtime != self._last_setup_mtime:
                    self._last_setup_mtime = s_mtime
                    return True
            except OSError:
                pass
        return False

    def refresh(self, force: bool = False) -> None:
        """Reloads all financial rollups from SQLite."""
        if not force and not self._is_cache_stale():
            return

        now_str = datetime.now().strftime("%Y-%m-%d")
        month_str = now_str[:7]
        start_date = f"{month_str}-01"
        end_date = f"{month_str}-31"

        # 1. Monthly Overview & Paycheck Plan & Cash Flow
        try:
            self._cached_overview = self.store.get_monthly_overview(month_str)
            self._cached_cashflow = self.store.get_cash_flow_summary(start_date, end_date)
            self._cached_paycheck = self.recurring.get_paycheck_allocation(as_of_date=now_str)
            self._cached_balances = self.store.get_all_balances()
        except Exception as e:
            logger.debug(f"Error refreshing overview/paycheck: {e}")

        # 2. Daily Ledger (last 60 days of transactions grouped by date)
        try:
            all_txs = self.store.list_transactions(limit=300)
            grouped: Dict[str, List[Transaction]] = {}
            for t in all_txs:
                if t.source == "setup" and t.payee == "Opening Balance":
                    # Skip synthetic opening balance in daily spending browser
                    continue
                grouped.setdefault(t.date, []).append(t)

            days_list: List[Dict[str, Any]] = []
            for date_key in sorted(grouped.keys(), reverse=True):
                txs = grouped[date_key]
                inflow_cents = 0
                outflow_cents = 0
                savings_cents = 0
                items: List[Dict[str, Any]] = []

                for t in txs:
                    amount_cents = 0
                    cat_name = "Personal"
                    account_short = "Checking"
                    is_income = False
                    is_savings_transfer = False

                    source_postings = [p for p in t.postings if p.amount_cents < 0]
                    target_postings = [p for p in t.postings if p.amount_cents > 0]

                    # 1. Determine funding account from negative posting (source)
                    if source_postings:
                        src_acc = source_postings[0].account_id
                        if "Checking" in src_acc:
                            account_short = "Checking"
                        elif "Visa" in src_acc or "CreditCard" in src_acc:
                            account_short = "Visa"
                        elif "Savings" in src_acc:
                            account_short = "Savings"
                        elif "Income:" in src_acc:
                            account_short = "Checking"

                    # 2. Determine category and amount from positive posting (destination)
                    if target_postings:
                        tgt_p = target_postings[0]
                        amount_cents = tgt_p.amount_cents
                        tgt_acc = tgt_p.account_id

                        if tgt_acc == "Assets:Savings:CapitalOne":
                            cat_name = "Savings"
                            is_savings_transfer = True
                        elif tgt_acc.startswith("Expenses:"):
                            cat_name = tgt_acc.split(":")[-1]
                        elif tgt_acc.startswith("Liabilities:CreditCard") or "CreditCard" in tgt_acc or "Visa" in tgt_acc:
                            cat_name = "Credit Card"
                        elif tgt_acc.startswith("Assets:Checking"):
                            is_income = True
                            cat_name = "Income"
                        elif tgt_acc.startswith("Income:"):
                            is_income = True
                            cat_name = "Income"
                    elif source_postings:
                        amount_cents = abs(source_postings[0].amount_cents)

                    if is_income:
                        inflow_cents += amount_cents
                    elif is_savings_transfer:
                        savings_cents += amount_cents
                    else:
                        outflow_cents += amount_cents

                    items.append({
                        "id": t.id,
                        "payee": t.payee,
                        "category": cat_name,
                        "account": account_short,
                        "amount_cents": amount_cents,
                        "is_income": is_income,
                        "is_savings": is_savings_transfer,
                        "memo": t.memo,
                    })

                try:
                    dt = datetime.strptime(date_key, "%Y-%m-%d")
                    day_title = dt.strftime("%a, %b %d")
                except Exception:
                    day_title = date_key

                days_list.append({
                    "date": date_key,
                    "day_title": day_title,
                    "inflow_cents": inflow_cents,
                    "outflow_cents": outflow_cents,
                    "savings_cents": savings_cents,
                    "net_cents": inflow_cents - outflow_cents,
                    "items": items,
                })

            self._cached_days = days_list
            self._cache_time = time.time()
        except Exception as e:
            logger.debug(f"Error refreshing daily ledger: {e}")

    def get_daily_ledger(self) -> List[Dict[str, Any]]:
        self.refresh()
        return self._cached_days

    def get_monthly_overview(self) -> Optional[MonthlyOverview]:
        self.refresh()
        return self._cached_overview

    def get_cash_flow_summary(self) -> Optional[CashFlowSummary]:
        self.refresh()
        return self._cached_cashflow

    def get_paycheck_allocation(self, as_of_date: Optional[str] = None) -> Optional[PaycheckWindowAllocation]:
        """Cached allocation for today, or a fresh computation for an explicit as_of_date."""
        if as_of_date:
            return self.recurring.get_paycheck_allocation(as_of_date=as_of_date)
        self.refresh()
        return self._cached_paycheck

    def get_runway(self, today: Optional[str] = None) -> Optional[RunwayInfo]:
        """Runway to the next paycheck, or None when no paycheck plan is configured."""
        self.refresh()
        today_str = today or datetime.now().strftime("%Y-%m-%d")
        paycheck = self._cached_paycheck if today is None else self.recurring.get_paycheck_allocation(as_of_date=today_str)
        if paycheck is None:
            return None
        try:
            return compute_runway(today_str, paycheck, self._cached_days)
        except ValueError:
            return None

    def get_balances(self) -> Dict[str, Any]:
        self.refresh()
        return self._cached_balances

    def log_day_bullets(self, date_str: str, bullet_text: str) -> List[Transaction]:
        """Parses lines of bullet text and logs all items for date_str."""
        lines = bullet_text.splitlines()
        batch_items = []
        for line in lines:
            item = parse_bullet_line(line)
            if item:
                item["date"] = date_str
                batch_items.append(item)

        if not batch_items:
            return []

        logged = self.store.log_batch_transactions(batch_items, default_date=date_str)
        self.refresh(force=True)
        return logged

    def delete_transaction(self, tx_id: str) -> bool:
        """Deletes a transaction and invalidates caches."""
        success = self.store.delete_transaction(tx_id)
        if success:
            self.refresh(force=True)
        return success

    def delete_day(self, date_str: str) -> int:
        """Deletes all transactions for date_str and refreshes caches."""
        count = self.store.delete_day_transactions(date_str)
        self.refresh(force=True)
        return count

    def edit_transaction(
        self,
        tx_id: str,
        date_str: str,
        new_bullet_line: str,
        account_choice: Optional[str] = None,
        category_choice: Optional[str] = None,
    ) -> Optional[Transaction]:
        """Edits an existing transaction by replacing it with a newly parsed bullet and optional account/category overrides.

        category_choice is a key from EDIT_CATEGORIES (e.g. "personal" for Discretionary); None keeps the
        category the bullet text implies.
        """
        parsed = parse_bullet_line(new_bullet_line)
        if not parsed:
            return None
        parsed["date"] = date_str

        if category_choice:
            parsed["category"] = category_choice
            # Drop any destination the text implied (e.g. a "savings" word) so the chosen category wins.
            parsed.pop("to_account", None)
            parsed.pop("account_id", None)

        if account_choice:
            choice = account_choice.lower()
            if "checking" in choice or "wells" in choice:
                parsed["from_account"] = "Assets:Checking:WellsFargo"
            elif "visa" in choice or "card" in choice:
                parsed["from_account"] = "Liabilities:CreditCard:Visa"
            elif "savings" in choice or "capital" in choice:
                parsed["from_account"] = "Assets:Savings:CapitalOne"

        self.store.delete_transaction(tx_id)
        logged = self.store.log_batch_transactions([parsed], default_date=date_str)
        self.refresh(force=True)
        return logged[0] if logged else None

    def item_to_bullet(self, item: Dict[str, Any]) -> str:
        """Converts an item dict back into a clean bullet representation."""
        amt = abs(item["amount_cents"]) / 100.0
        amt_str = f"{amt:.2f}".rstrip("0").rstrip(".") if amt == int(amt) else f"{amt:.2f}"
        cat = item.get("category") or "personal"
        payee = item.get("payee") or ""

        if cat.lower() == "savings" or "savings" in payee.lower() or "capitalone" in item.get("account", "").lower():
            return f"savings + {amt_str}"
        elif item.get("is_income"):
            return f"{payee or 'payday'} + {amt_str}"
        elif payee and payee.lower() != cat.lower():
            return f"- {payee} - {amt_str}"
        return f"- {cat.lower()} - {amt_str}"

    def get_goals_progress(self, month: Optional[str] = None) -> List[Any]:
        """Returns visual GoalProgress objects for monthly tracking bars."""
        return self.store.get_goals_progress(month=month)

    def get_monthly_bills_breakdown(self, month: Optional[str] = None) -> Dict[str, Any]:
        """Returns recurring bills sorted by due date and monthly overview with Notion formulas."""
        return self.recurring.get_all_recurring_bills_breakdown(month=month)
