"""Telemachus Personal Financial Operations Subsystem.

Deterministic double-entry bookkeeping engine with SQLite master store,
plain-text accounting projection, and rolling statistical variable bill estimations.
"""
from __future__ import annotations

from telemachus.finance.export import FinanceExporter
from telemachus.finance.masking import FinanceMasker
from telemachus.finance.models import (
    DEFAULT_USER_ACCOUNTS,
    DEFAULT_USER_RECURRING_RULES,
    Account,
    AccountType,
    BillInstance,
    BillStatus,
    CashFlowSummary,
    Frequency,
    Posting,
    RecurringRule,
    RuleType,
    Transaction,
)
from telemachus.finance.recurring import RecurringEngine
from telemachus.finance.store import (
    AccountNotFoundError,
    DuplicateTransactionError,
    FinanceStore,
    UnbalancedTransactionError,
)

__all__ = [
    "FinanceStore",
    "RecurringEngine",
    "FinanceExporter",
    "FinanceMasker",
    "Account",
    "AccountType",
    "Posting",
    "Transaction",
    "RecurringRule",
    "BillInstance",
    "RuleType",
    "Frequency",
    "BillStatus",
    "CashFlowSummary",
    "DEFAULT_USER_ACCOUNTS",
    "DEFAULT_USER_RECURRING_RULES",
    "UnbalancedTransactionError",
    "AccountNotFoundError",
    "DuplicateTransactionError",
]
