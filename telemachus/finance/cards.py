"""Credit card (Visa) logic shared by the ledger views and the cockpit.

How money moves (double-entry, cents):

- Checking purchase:  Expense +X, Checking -X. Spending goes up, checking goes down.
- Card purchase:      Expense +X, Visa -X. Spending goes up, the card balance owed goes up,
                      checking does not change.
- Card payment:       Visa +X, Checking -X. The amount owed goes down and checking goes down.
                      This is a transfer between your own accounts, NOT spending.
- Overpayment:        the Visa balance goes below zero, which is a credit on the card.
- Savings transfer:   Savings +X, Checking -X. A transfer, NOT spending.

The store reports a liability balance as positive when money is owed, so
`store.get_account_balance(VISA)` is the amount owed (negative = credit).

Spending is always the sum of Expense postings, whatever account paid for them.
Card payments and savings transfers never count as spending.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime
import re
from typing import Any, Dict, Iterable, List, Optional

CHECKING = "Assets:Checking:WellsFargo"
SAVINGS = "Assets:Savings:CapitalOne"
VISA = "Liabilities:CreditCard:Visa"

# setup.yaml fixed_bills keys that mean the credit card.
CARD_BILL_KEYS = ("credit_card", "visa", "visa_credit_card", "credit_card_payment")
DEFAULT_CARD_DUE_DAY = 2

# Transaction kinds (same strings as the cockpit day items).
KIND_SPEND = "spend"
KIND_INCOME = "income"
KIND_CARD_PAYMENT = "card_payment"
KIND_TRANSFER = "transfer"
KIND_OPENING = "opening"


def parse_due_day(value: Any, default: int) -> int:
    """A setup.yaml due_day as a day 1-31. "2nd" is 2; a missing or unreadable value is `default`,
    so one typo never stops the bill math. The bill engine and the card summary both use this,
    so they always agree on the card's due day."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        day = int(value)
    else:
        m = re.match(r"\s*([0-9]{1,2})", str(value))
        if not m:
            return default
        day = int(m.group(1))
    return max(1, min(31, day))


def is_card_account(account_id: str) -> bool:
    return account_id.startswith("Liabilities:CreditCard") or "Visa" in account_id


def account_short(account_id: str) -> str:
    """Short name of a funding account: "Checking", "Visa" or "Savings" (else the last part)."""
    if not account_id:
        return "Checking"
    if "Checking" in account_id:
        return "Checking"
    if is_card_account(account_id):
        return "Visa"
    if "Savings" in account_id:
        return "Savings"
    if account_id.startswith("Income:"):
        return "Checking"
    return account_id.split(":")[-1]


def _postings(tx: Any) -> List[Any]:
    return list(getattr(tx, "postings", None) or [])


def classify_transaction(tx: Any) -> Dict[str, Any]:
    """Describes one ledger transaction for display and for spending totals.

    Returns {kind, account_short, category, amount_cents, is_income, is_savings}.
    - kind "spend": amount is the sum of Expense postings; account_short is the account that paid
      (Checking, Visa or Savings).
    - kind "income": amount is the income received; account_short is where it landed.
    - kind "card_payment": amount is how much the card balance went down; account_short is the payer.
    - kind "transfer": money moved between your own accounts (for example to savings).
    - kind "opening": setup opening balances.
    """
    ps = _postings(tx)
    expense = [p for p in ps if p.account_id.startswith("Expenses:")]
    income = [p for p in ps if p.account_id.startswith("Income:")]
    equity = [p for p in ps if p.account_id.startswith("Equity:")]
    card = [p for p in ps if is_card_account(p.account_id)]

    def first(pred) -> Optional[Any]:
        for p in ps:
            if pred(p):
                return p
        return None

    if income:
        amount = -sum(p.amount_cents for p in income)
        dest = first(lambda p: p.amount_cents > 0 and not p.account_id.startswith("Income:"))
        return {
            "kind": KIND_INCOME,
            "account_short": account_short(dest.account_id) if dest else "Checking",
            "category": "Income",
            "amount_cents": amount,
            "is_income": True,
            "is_savings": False,
        }

    if expense:
        amount = sum(p.amount_cents for p in expense)
        biggest = max(expense, key=lambda p: abs(p.amount_cents))
        payer = first(lambda p: p.amount_cents < 0 and not p.account_id.startswith("Expenses:"))
        return {
            "kind": KIND_SPEND,
            "account_short": account_short(payer.account_id) if payer else "Checking",
            "category": biggest.account_id.split(":")[-1],
            "amount_cents": amount,
            "is_income": False,
            "is_savings": False,
        }

    if equity:
        amount = max((abs(p.amount_cents) for p in ps), default=0)
        other = first(lambda p: not p.account_id.startswith("Equity:"))
        return {
            "kind": KIND_OPENING,
            "account_short": account_short(other.account_id) if other else "Checking",
            "category": "Opening Balance",
            "amount_cents": amount,
            "is_income": False,
            "is_savings": False,
        }

    if card:
        card_delta = sum(p.amount_cents for p in card)
        if card_delta > 0:
            payer = first(lambda p: p.amount_cents < 0 and not is_card_account(p.account_id))
            return {
                "kind": KIND_CARD_PAYMENT,
                "account_short": account_short(payer.account_id) if payer else "Checking",
                "category": "Credit Card",
                "amount_cents": card_delta,
                "is_income": False,
                "is_savings": False,
            }
        # Money moved off the card into an account (cash advance): a transfer, owed goes up.
        return {
            "kind": KIND_TRANSFER,
            "account_short": "Visa",
            "category": "Cash advance",
            "amount_cents": -card_delta,
            "is_income": False,
            "is_savings": False,
        }

    to_savings = [p for p in ps if p.account_id == SAVINGS and p.amount_cents > 0]
    if to_savings:
        payer = first(lambda p: p.amount_cents < 0)
        return {
            "kind": KIND_TRANSFER,
            "account_short": account_short(payer.account_id) if payer else "Checking",
            "category": "Savings",
            "amount_cents": sum(p.amount_cents for p in to_savings),
            "is_income": False,
            "is_savings": True,
        }

    dest = first(lambda p: p.amount_cents > 0)
    payer = first(lambda p: p.amount_cents < 0)
    return {
        "kind": KIND_TRANSFER,
        "account_short": account_short(payer.account_id) if payer else "Checking",
        "category": "Transfer",
        "amount_cents": dest.amount_cents if dest else 0,
        "is_income": False,
        "is_savings": False,
    }


def card_charge_cents(tx: Any) -> int:
    """How much this transaction put on the card as a purchase (0 if none)."""
    info = classify_transaction(tx)
    if info["kind"] != KIND_SPEND:
        return 0
    return -sum(p.amount_cents for p in _postings(tx) if is_card_account(p.account_id))


def card_payment_cents(tx: Any) -> int:
    """How much this transaction paid off the card (0 if it is not a card payment)."""
    info = classify_transaction(tx)
    return info["amount_cents"] if info["kind"] == KIND_CARD_PAYMENT else 0


def card_bill_entries(setup_data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """setup.yaml fixed_bills entries that mean the card (active or not)."""
    fb = (setup_data or {}).get("fixed_bills") or {}
    out = []
    if isinstance(fb, dict):
        for k, v in fb.items():
            clean = str(k).strip().lower().replace("-", "_").replace(" ", "_")
            if clean in CARD_BILL_KEYS and isinstance(v, dict):
                out.append(v)
    return out


def card_bill_switched_off(setup_data: Optional[Dict[str, Any]]) -> bool:
    """True when setup.yaml lists the card bill and every card entry has active: false."""
    entries = card_bill_entries(setup_data)
    return bool(entries) and not any(bool(v.get("active", True)) for v in entries)


def card_due_day(setup_data: Optional[Dict[str, Any]]) -> Optional[int]:
    """Card due day from setup.yaml fixed_bills (credit_card / visa keys), else the 2nd.

    None when the card bill is switched off (active: false): nothing is due on any day.
    """
    if card_bill_switched_off(setup_data):
        return None
    for v in card_bill_entries(setup_data):
        # Inactive entries are ignored, the same way the bill engine ignores them.
        if bool(v.get("active", True)) and v.get("due_day") is not None:
            return parse_due_day(v["due_day"], DEFAULT_CARD_DUE_DAY)
    return DEFAULT_CARD_DUE_DAY


def next_due_date(due_day: int, today: date) -> date:
    """The card due date on or after `today` (day clamped to the month length)."""
    def in_month(y: int, m: int) -> date:
        return date(y, m, min(due_day, calendar.monthrange(y, m)[1]))

    this = in_month(today.year, today.month)
    if this >= today:
        return this
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return in_month(y, m)


def previous_due_date(due_day: int, today: date) -> date:
    """The last card due date before `today` (day clamped to the month length)."""
    def in_month(y: int, m: int) -> date:
        return date(y, m, min(due_day, calendar.monthrange(y, m)[1]))

    this = in_month(today.year, today.month)
    if this < today:
        return this
    y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return in_month(y, m)


def build_card_summary(
    transactions: Iterable[Any],
    owed_cents: int,
    today: date,
    due_day: Optional[int],
    next_payday: Optional[date],
    overdue_since: Optional[date] = None,
) -> Dict[str, Any]:
    """Pure card summary for the Money tab (see CONTRACT.md section 7).

    `transactions` are every non-opening transaction that touches the card (any order).
    `due_before_payday` is True when money is owed and the next due date comes before the
    next payday; that is exactly when the until-payday math subtracts the card balance.
    `overdue_since`: the due date that passed with no payment logged (see
    RecurringEngine.card_overdue_since). Then `next_due_date` is that past date (before
    `today`, which is how a view tells "overdue") and `due_before_payday` is True: the
    until-payday math subtracts the balance today.
    """
    month_prefix = today.strftime("%Y-%m")
    today_iso = today.isoformat()
    charges = []
    payments = []
    for tx in transactions:
        if str(tx.date) > today_iso:
            continue  # dated after `today`: not part of the card as of today
        # Order inside a day: created_at (seconds), then ledger insertion order (seq), so a
        # charge typed after a payment in the same Add batch counts as "since the payment".
        key = (tx.date, getattr(tx, "created_at", 0) or 0, getattr(tx, "seq", 0) or 0)
        c = card_charge_cents(tx)
        if c:
            charges.append((key, c))
        pay = card_payment_cents(tx)
        if pay:
            payments.append((key, pay))

    payments.sort()
    last_payment = None
    if payments:
        last_key, _amt = payments[-1]
        last_date = last_key[0]
        # Several payments on the same day count as one payment of their total.
        same_day = sum(a for (k, a) in payments if k[0] == last_date)
        last_payment = {"date": last_date, "amount_cents": same_day}
        # Charges logged after the last payment (same day counts when logged later).
        charged_since = sum(a for (k, a) in charges if k > last_key)
    else:
        charged_since = sum(a for (_k, a) in charges)

    paid_this_month = sum(a for (k, a) in payments if k[0].startswith(month_prefix))

    nd = next_due_date(due_day, today) if due_day else None
    status = "owed" if owed_cents > 0 else ("credit" if owed_cents < 0 else "clear")
    due_before = bool(owed_cents > 0 and nd is not None and next_payday is not None and nd < next_payday)
    if owed_cents > 0 and due_day and overdue_since is not None and overdue_since < today:
        nd = overdue_since
        due_before = True

    return {
        "owed_cents": int(owed_cents),
        "charged_since_last_payment_cents": int(charged_since),
        "paid_this_month_cents": int(paid_this_month),
        "last_payment": last_payment,
        "due_day": due_day,
        "next_due_date": nd.isoformat() if nd else None,
        "status": status,
        "due_before_payday": due_before,
    }


def parse_iso(d: str) -> Optional[date]:
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


__all__ = [
    "CHECKING",
    "SAVINGS",
    "VISA",
    "CARD_BILL_KEYS",
    "DEFAULT_CARD_DUE_DAY",
    "KIND_SPEND",
    "KIND_INCOME",
    "KIND_CARD_PAYMENT",
    "KIND_TRANSFER",
    "KIND_OPENING",
    "account_short",
    "is_card_account",
    "classify_transaction",
    "card_charge_cents",
    "card_payment_cents",
    "card_due_day",
    "card_bill_entries",
    "card_bill_switched_off",
    "next_due_date",
    "previous_due_date",
    "parse_due_day",
    "build_card_summary",
    "parse_iso",
]
