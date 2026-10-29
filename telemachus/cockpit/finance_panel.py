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

from telemachus.finance.cards import (
    CHECKING,
    KIND_CARD_PAYMENT,
    KIND_INCOME,
    KIND_OPENING,
    KIND_SPEND,
    KIND_TRANSFER,
    SAVINGS,
    VISA,
    classify_transaction,
)
from telemachus.finance.import_text import explain_bullet_line, parse_bullet_line, preview_bullet_line
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
    ("Rent", "rent"),
]


PACE_WINDOW_DAYS = 7

# Expense accounts covered by the grocery + dining + gas allowance.
ALLOWANCE_ACCOUNTS = frozenset({
    "Expenses:Food:Groceries", "Expenses:Groceries",
    "Expenses:Food:Dining", "Expenses:Dining",
    "Expenses:Transport:Gas", "Expenses:Gas",
})


# Bill accounts that never count toward the "lately $X a day" pace (bills are subtracted on
# their own, so a rent payment must not make the pace look high for a week).
BILL_ACCOUNT_PREFIXES = ("Expenses:Housing:", "Expenses:Utilities:", "Expenses:Subscriptions:", "Expenses:Family:")


def bill_spend_cents(tx: Any, bill_accounts: Any = ()) -> int:
    """Bill payments in one transaction: built-in bill accounts plus the setup.yaml bill accounts."""
    return sum(
        p.amount_cents for p in (getattr(tx, "postings", None) or [])
        if p.account_id.startswith("Expenses:")
        and (p.account_id.startswith(BILL_ACCOUNT_PREFIXES) or p.account_id in bill_accounts)
    )


def allowance_spend_cents(tx: Any) -> int:
    """Groceries, dining and gas spent in one transaction (bills and other spending excluded)."""
    return sum(
        p.amount_cents for p in (getattr(tx, "postings", None) or []) if p.account_id in ALLOWANCE_ACCOUNTS
    )


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
    period_days: int = 14  # length of the whole pay period (last payday to next payday)
    # allowance_cents spread over the whole pay period (a fixed budget per day, e.g. 260 / 14).
    # Compare it with a day's allowance_spent_cents (groceries, dining, gas), not with all spending.
    daily_allowance_cents: int = 0


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

    # Pace = everyday spending only: bill payments (rent, wifi, ...) are left out, because the
    # bills still due are subtracted on their own.
    window_start = today_dt - timedelta(days=PACE_WINDOW_DAYS - 1)
    spent = 0
    for d in days:
        try:
            d_dt = datetime.strptime(d["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if window_start <= d_dt <= today_dt:
            spent += d.get("pace_spent_cents", d["outflow_cents"])
    pace = _rounded_div(spent, PACE_WINDOW_DAYS)

    due_by_date: Dict[str, int] = {}
    for b in paycheck.bills:
        if b.get("status") == "due" and b.get("amount_cents", 0) > 0:
            due_by_date[b["due_date"]] = due_by_date.get(b["due_date"], 0) + b["amount_cents"]

    # Day-by-day balance: bills due today come out first (they are part of left_cents too),
    # then each later day loses the pace plus that day's bills.
    run_out: Optional[str] = None
    balance = checking - due_by_date.get(today, 0)
    if balance < 0:
        run_out = today
    elif pace > 0 or due_by_date:
        for offset in range(1, days_left + 1):
            day = (today_dt + timedelta(days=offset)).isoformat()
            balance -= pace + due_by_date.get(day, 0)
            if balance < 0:
                run_out = day
                break

    month_prefix = today[:7]
    # Entries logged ahead (a later day this month) are not spent yet.
    month_spent = sum(d["outflow_cents"] for d in days if d["date"].startswith(month_prefix) and d["date"] <= today)

    period_days = 14
    try:
        start_dt = datetime.strptime(paycheck.cycle_start_date, "%Y-%m-%d").date()
        if 0 < (next_dt - start_dt).days <= 31:
            period_days = (next_dt - start_dt).days
    except (TypeError, ValueError, AttributeError):
        pass
    allowance = paycheck.variable_needs_cents
    daily_allowance = _rounded_div(allowance, period_days) if allowance > 0 else 0

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
        period_days=period_days,
        daily_allowance_cents=daily_allowance,
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
        self._cached_card: Optional[Dict[str, Any]] = None
        self._setup_error: Optional[str] = None
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

        self._setup_error = self._read_setup_error()

        # 1. Monthly Overview & Paycheck Plan & Cash Flow
        try:
            # As of today, like "Oct: $X spent": entries logged ahead are not spent yet.
            self._cached_overview = self.store.get_monthly_overview(month_str, as_of_date=now_str)
            self._cached_cashflow = self.store.get_cash_flow_summary(start_date, min(end_date, now_str))
            # No paycheck plan: no payday, so no until-payday numbers (never a guessed payday).
            # A broken setup.yaml: the bills are unknown, so no "left after bills" either.
            self._cached_paycheck = (
                self.recurring.get_paycheck_allocation(as_of_date=now_str)
                if self.recurring.has_paycheck_plan() and not self._setup_error else None
            )
            # As of today: entries logged on a later day do not count yet.
            self._cached_balances = self.store.get_all_balances(as_of_date=now_str, setup_always=True)
        except Exception as e:
            logger.debug(f"Error refreshing overview/paycheck: {e}")

        # 2. Daily Ledger: every entry since the start of the month (or the pace window, if earlier),
        # plus the newest 300 for older history. Totals must not depend on how many items exist.
        try:
            today_dt = datetime.strptime(now_str, "%Y-%m-%d").date()
            range_start = min(today_dt.replace(day=1), today_dt - timedelta(days=PACE_WINDOW_DAYS - 1))
            all_txs = self.store.list_transactions(start_date=range_start.isoformat(), limit=10_000_000)
            seen_ids = {t.id for t in all_txs}
            all_txs += [t for t in self.store.list_transactions(limit=300) if t.id not in seen_ids]
            bill_accounts = set(self.recurring.bill_accounts())
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
                outflow_cents = 0  # spending only: expense postings, any funding account
                savings_cents = 0
                card_payment_cents = 0
                card_spend_cents = 0
                allowance_spent_cents = 0  # groceries + dining + gas only
                bill_paid_cents = 0  # rent, wifi, gym, ... (left out of the pace)
                items: List[Dict[str, Any]] = []

                for t in txs:
                    info = classify_transaction(t)
                    kind = info["kind"]
                    if kind == KIND_OPENING:
                        continue
                    amount_cents = info["amount_cents"]
                    if kind == KIND_INCOME:
                        inflow_cents += amount_cents
                    elif kind == KIND_SPEND:
                        outflow_cents += amount_cents
                        allowance_spent_cents += allowance_spend_cents(t)
                        bill_paid_cents += bill_spend_cents(t, bill_accounts)
                        if info["account_short"] == "Visa":
                            card_spend_cents += amount_cents
                    elif kind == KIND_CARD_PAYMENT:
                        # Moves money from checking to the card: not spending.
                        card_payment_cents += amount_cents
                    elif info["is_savings"]:
                        savings_cents += amount_cents

                    items.append({
                        "id": t.id,
                        "payee": t.payee,
                        "category": info["category"],
                        "account": info["account_short"],
                        # "Checking" | "Visa" | "Savings": who paid (spend, payment, transfer) or
                        # where it landed (income).
                        "account_short": info["account_short"],
                        "kind": kind,  # "spend" | "income" | "card_payment" | "transfer"
                        "amount_cents": amount_cents,
                        "is_income": info["is_income"],
                        "is_savings": info["is_savings"],
                        # True for groceries, dining and gas (what the daily allowance covers)
                        "is_allowance": kind == KIND_SPEND and allowance_spend_cents(t) > 0,
                        "memo": t.memo,
                    })

                if not items:
                    continue

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
                    "spent_cents": outflow_cents,
                    "savings_cents": savings_cents,
                    "card_payment_cents": card_payment_cents,
                    "card_spend_cents": card_spend_cents,
                    "allowance_spent_cents": allowance_spent_cents,
                    "bill_paid_cents": bill_paid_cents,
                    # everyday spending: what the "lately $X a day" pace averages
                    "pace_spent_cents": max(0, outflow_cents - bill_paid_cents),
                    "net_cents": inflow_cents - outflow_cents,
                    "items": items,
                })

            self._cached_days = days_list
            self._cache_time = time.time()
        except Exception as e:
            logger.debug(f"Error refreshing daily ledger: {e}")

        # 3. Card summary (uses the cached paycheck for the next payday)
        try:
            next_pay = self._cached_paycheck.next_paycheck_date if self._cached_paycheck else None
            self._cached_card = self.recurring.get_card_summary(as_of_date=now_str, next_paycheck_date=next_pay)
        except Exception as e:
            logger.debug(f"Error refreshing card summary: {e}")
            self._cached_card = None

    def _read_setup_error(self) -> Optional[str]:
        path = self.store.get_setup_yaml_path()
        if not path:
            return None
        try:
            from telemachus.finance.setup import setup_yaml_error

            return setup_yaml_error(path)
        except Exception as e:
            return f"setup.yaml cannot be read: {e}"

    def get_setup_error(self) -> Optional[str]:
        """One line saying why setup.yaml cannot be read, or None. While it is set, the
        until-payday numbers are hidden (get_runway is None): the bills in the file are unknown."""
        self.refresh()
        return self._setup_error

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
        if today is None:
            paycheck = self._cached_paycheck
        elif self._setup_error:
            paycheck = None
        elif self.recurring.has_paycheck_plan():
            paycheck = self.recurring.get_paycheck_allocation(as_of_date=today_str)
        else:
            paycheck = None
        if paycheck is None:
            return None
        try:
            return compute_runway(today_str, paycheck, self._cached_days)
        except ValueError:
            return None

    def get_balances(self) -> Dict[str, Any]:
        self.refresh()
        return self._cached_balances

    def get_card_summary(self, today: Optional[str] = None) -> Dict[str, Any]:
        """Visa card state for the Money tab (CONTRACT.md section 7).

        Keys: owed_cents (int, positive = owed, negative = credit), charged_since_last_payment_cents,
        paid_this_month_cents, last_payment (None or {"date": "YYYY-MM-DD", "amount_cents": int}),
        due_day (int), next_due_date ("YYYY-MM-DD"), status ("clear" | "owed" | "credit"),
        due_before_payday (bool: the until-payday math subtracts the balance owed).
        Overdue (a due date passed with no payment logged): next_due_date is that past date,
        before today, and due_before_payday is True.
        """
        if today is not None:
            return self.recurring.get_card_summary(as_of_date=today)
        self.refresh()
        if self._cached_card is not None:
            return dict(self._cached_card)
        return {
            "owed_cents": 0,
            "charged_since_last_payment_cents": 0,
            "paid_this_month_cents": 0,
            "last_payment": None,
            "due_day": None,
            "next_due_date": None,
            "status": "clear",
            "due_before_payday": False,
        }

    def preview_bullet_line(self, line: str) -> Dict[str, Any]:
        """Live preview of one typed line (nothing is written). See import_text.preview_bullet_line.

        A line that pays a custom setup.yaml bill ("phone bill 80") is shown as that bill.
        """
        out = preview_bullet_line(line)
        if out.get("ok") and out.get("kind") == KIND_SPEND and out.get("category_label") == "Discretionary":
            match = self.recurring.bill_account_for_text(out.get("payee") or "")
            if match:
                old_label = out["category_label"]
                out["category_label"] = match[1]
                out["summary_text"] = match[1] + out["summary_text"][len(old_label):]
        return out

    def match_bill(self, item: Dict[str, Any]) -> None:
        """Points a plain spend line at the setup.yaml bill it pays (custom keys like phone), so
        the bill shows as paid and is not subtracted again."""
        if item.get("kind") != KIND_SPEND or item.get("category") != "personal" or item.get("to_account"):
            return
        match = self.recurring.bill_account_for_text(item.get("payee") or "")
        if match:
            self.recurring.ensure_bill_account(match[0])
            item["to_account"] = match[0]

    def log_day_bullets(self, date_str: str, bullet_text: str) -> List[Transaction]:
        """Parses lines of bullet text and logs all items for date_str.

        Every typed line is its own entry: two identical lines both count (Sam logs each item).
        Lines that cannot be read are skipped; use preview_bullet_line to show why.
        """
        lines = bullet_text.splitlines()
        batch_items = []
        for line in lines:
            item = parse_bullet_line(line)
            if item:
                item["date"] = date_str
                self.match_bill(item)
                batch_items.append(item)

        if not batch_items:
            return []

        logged = self.store.log_batch_transactions(batch_items, default_date=date_str, allow_duplicates=True)
        self.refresh(force=True)
        return logged

    def delete_transaction(self, tx_id: str) -> bool:
        """Deletes a transaction and invalidates caches.

        Deleting a card payment puts its amount back on the card (owed goes up) and back in checking.
        """
        success = self.store.delete_transaction(tx_id)
        if success:
            self.refresh(force=True)
        return success

    def delete_day(self, date_str: str) -> int:
        """Deletes all transactions for date_str and refreshes caches."""
        count = self.store.delete_day_transactions(date_str)
        self.refresh(force=True)
        return count

    @staticmethod
    def _account_from_choice(account_choice: Optional[str]) -> Optional[str]:
        """Edit-box account label -> "Checking" | "Visa" | "Savings" (None when not given)."""
        if not account_choice:
            return None
        choice = account_choice.lower()
        if "checking" in choice or "wells" in choice:
            return "Checking"
        if "visa" in choice or "card" in choice or "credit" in choice:
            return "Visa"
        if "savings" in choice or "capital" in choice:
            return "Savings"
        return None

    def edit_transaction(
        self,
        tx_id: str,
        date_str: str,
        new_bullet_line: str,
        account_choice: Optional[str] = None,
        category_choice: Optional[str] = None,
    ) -> Optional[Transaction]:
        """Replaces a transaction with a newly parsed bullet plus the Edit box's account/category.

        Kind (spend, income, card payment, savings transfer):
        - Text with a kind word wins ("visa payment 200", "savings + 50", "payday + 1480").
        - Plain text ("250", "bonus 200") keeps the original kind, so editing a payment's amount
          never turns it into spending.
        - Picking a category (category_choice, e.g. "personal") makes it a purchase.

        Account (account_choice "Checking" | "Visa" | "CapitalOne"):
        - Purchase: the account that paid. Checking <-> Visa moves the charge between checking and
          the card. A card word typed in the text ("lunch 13 visa") counts when the box's account
          was left unchanged.
        - Income: the account the money lands in.
        - Card payment: the account that paid the card (Checking or Savings); Visa is ignored.
        - Savings transfer: always from checking.
        The new entry is logged before the old one is deleted, so a failure keeps the old entry.
        """
        parsed, _err = explain_bullet_line(new_bullet_line)
        if not parsed:
            return None
        parsed["date"] = date_str

        original = self.store.get_transaction(tx_id)
        orig = classify_transaction(original) if original else None
        orig_kind = orig["kind"] if orig else None
        orig_acct = orig["account_short"] if orig else None
        choice = self._account_from_choice(account_choice)
        user_changed = choice is not None and choice != orig_acct
        kind = parsed["kind"]

        if not category_choice:
            self.match_bill(parsed)
        if category_choice:
            kind = KIND_SPEND
            parsed["category"] = category_choice
            # Drop any destination the text implied (e.g. a "savings" word) so the chosen category wins.
            for key in ("to_account", "account_id"):
                parsed.pop(key, None)
        elif kind == KIND_SPEND and orig_kind in (KIND_CARD_PAYMENT, KIND_INCOME) or (
            kind == KIND_SPEND and orig_kind == KIND_TRANSFER and orig
            and (orig["is_savings"] or orig["account_short"] == "Savings")
        ):
            kind = orig_kind
            text_payee = parsed.get("payee")
            for key in ("to_account", "account_id", "from_account"):
                parsed.pop(key, None)
            if kind == KIND_CARD_PAYMENT:
                parsed.update(category="credit_card_payment", to_account=VISA, from_account=CHECKING, payee="Visa payment")
                if orig_acct == "Savings":
                    parsed["from_account"] = SAVINGS
            elif kind == KIND_INCOME:
                parsed.update(category="payday", account_id=CHECKING, payee=text_payee or "Payday Payroll")
            elif orig["is_savings"]:
                parsed.update(category="savings", to_account=SAVINGS, account_id=SAVINGS, from_account=CHECKING)
            else:
                parsed.update(category="savings_withdrawal", to_account=CHECKING, from_account=SAVINGS,
                              payee="Savings to checking")

        accounts = {"Checking": CHECKING, "Visa": VISA, "Savings": SAVINGS}
        if kind == KIND_SPEND:
            text_says_card = parsed.get("from_account") == VISA
            if user_changed:
                parsed["from_account"] = accounts[choice]
            elif text_says_card and orig_acct != "Visa":
                parsed["from_account"] = VISA
            elif choice:
                parsed["from_account"] = accounts[choice]
        elif kind == KIND_INCOME:
            if choice:
                parsed.pop("account_id", None)
                parsed["to_account"] = accounts[choice]
                # Interest keeps its own income account; everything else is pay.
                if not str(parsed.get("from_account") or "").startswith("Income:"):
                    parsed["from_account"] = "Income:Salary"
        elif kind == KIND_CARD_PAYMENT:
            if choice in ("Checking", "Savings") and (user_changed or parsed.get("from_account") == CHECKING):
                parsed["from_account"] = accounts[choice]
            parsed["to_account"] = VISA
        elif parsed.get("from_account") != SAVINGS:
            # Into savings: always from checking. (Savings -> checking keeps its direction.)
            parsed["from_account"] = CHECKING

        logged = self.store.log_batch_transactions([parsed], default_date=date_str, allow_duplicates=True)
        self.store.delete_transaction(tx_id)
        if logged and original is not None and original.date == date_str:
            # Same day: the edited entry keeps the old one's place among that day's entries.
            self.store.restore_position(logged[0].id, original.seq, original.created_at)
            logged[0].created_at, logged[0].seq = original.created_at, original.seq
        self.refresh(force=True)
        return logged[0] if logged else None

    def item_to_bullet(self, item: Dict[str, Any]) -> str:
        """Converts a day item back into a bullet line that parses to the same kind and account."""
        amt = abs(item["amount_cents"]) / 100.0
        amt_str = f"{amt:.2f}".rstrip("0").rstrip(".") if amt == int(amt) else f"{amt:.2f}"
        cat = item.get("category") or "personal"
        payee = item.get("payee") or ""
        kind = item.get("kind")
        acct = item.get("account_short") or item.get("account") or ""

        if kind == KIND_CARD_PAYMENT or (kind is None and cat == "Credit Card"):
            return f"visa payment {amt_str}" + (" from savings" if acct == "Savings" else "")
        if kind == KIND_TRANSFER and acct == "Savings" and not item.get("is_savings"):
            return f"savings to checking {amt_str}"
        if kind == KIND_TRANSFER or cat.lower() == "savings" or item.get("is_savings"):
            return f"savings + {amt_str}"
        if kind == KIND_INCOME or item.get("is_income"):
            return f"{payee or 'payday'} + {amt_str}"
        if payee and payee.lower() != cat.lower():
            line = f"- {payee} - {amt_str}"
        else:
            line = f"- {cat.lower()} - {amt_str}"
        if acct == "Visa":
            line += " visa"
        elif acct == "Savings":
            line += " from savings"
        return line

    def get_goals_progress(self, month: Optional[str] = None) -> List[Any]:
        """Returns visual GoalProgress objects for monthly tracking bars."""
        return self.store.get_goals_progress(month=month)

    def get_monthly_bills_breakdown(self, month: Optional[str] = None) -> Dict[str, Any]:
        """Returns recurring bills sorted by due date and monthly overview with Notion formulas."""
        return self.recurring.get_all_recurring_bills_breakdown(month=month)
