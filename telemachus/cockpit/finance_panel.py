"""Cockpit Finance Panel & Daily Inspector Data Provider.

Provides:
- Daily grouped ledger navigation (Day Navigator + Day Line Items).
- Month-to-date and 2-week paycheck allocation metrics for Cockpit TUI.
- Fast bullet-list parsing and ingestion directly from curses input.
- Database mtime caching to prevent redundant queries on 25 FPS render loops.
"""
from __future__ import annotations

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

    def get_paycheck_allocation(self) -> Optional[PaycheckWindowAllocation]:
        self.refresh()
        return self._cached_paycheck

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
    ) -> Optional[Transaction]:
        """Edits an existing transaction by replacing it with a newly parsed bullet and optional account override."""
        parsed = parse_bullet_line(new_bullet_line)
        if not parsed:
            return None
        parsed["date"] = date_str

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

    def get_paycheck_allocation(self, as_of_date: Optional[str] = None) -> Any:
        """Returns 14-day paycheck window allocation."""
        from telemachus.finance.recurring import RecurringEngine
        engine = RecurringEngine(self.store)
        return engine.get_paycheck_allocation(as_of_date=as_of_date)
