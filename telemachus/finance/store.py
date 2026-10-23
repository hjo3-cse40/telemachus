"""Canonical SQLite Double-Entry Bookkeeping Engine for Telemachus.

Enforces:
- Exact 64-bit signed integer cents (no floating-point drift).
- Double-entry invariants: sum of postings per transaction == 0.
- Physical air-gap: default storage in ~/.memory/private/finances (chmod 0700).
- SQLite WAL mode for high-concurrency non-blocking reads from Cockpit.
- Advisory file locking for safe concurrent writes.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, Generator, List, Optional, Tuple

from telemachus.finance.models import (
    DEFAULT_USER_ACCOUNTS,
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

logger = logging.getLogger(__name__)

DEFAULT_FINANCE_ROOT = Path(
    os.environ.get("TELEMACHUS_FINANCE_DIR") or (Path.home() / ".memory" / "private" / "finances")
).expanduser().resolve()


class UnbalancedTransactionError(ValueError):
    """Raised when the sum of debits and credits in a transaction does not balance to 0."""
    pass


class AccountNotFoundError(KeyError):
    """Raised when referencing an account that does not exist in the chart of accounts."""
    pass


class DuplicateTransactionError(ValueError):
    """Raised when an identical transaction is posted within the deduplication window."""
    pass


class FinanceStore:
    """Transactional SQLite engine managing double-entry financial state."""

    def __init__(self, root_dir: Optional[Path | str] = None):
        self.root_dir = Path(root_dir).expanduser().resolve() if root_dir else DEFAULT_FINANCE_ROOT
        self.db_path = self.root_dir / "finances.sqlite3"
        self.lock_path = self.root_dir / ".lock"
        self._init_storage()

    def _init_storage(self) -> None:
        """Ensures the directory exists with 0700 permissions and initializes database tables."""
        self.root_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root_dir, 0o700)
        except OSError as e:
            logger.warning(f"Could not set 0700 on finance directory {self.root_dir}: {e}")

        # Initialize SQLite tables
        with self._write_lock():
            with self._get_connection() as conn:
                conn.executescript("""
                    PRAGMA journal_mode = WAL;
                    PRAGMA synchronous = NORMAL;
                    PRAGMA foreign_keys = ON;
                    PRAGMA busy_timeout = 5000;

                    CREATE TABLE IF NOT EXISTS accounts (
                        id TEXT PRIMARY KEY,
                        account_type TEXT NOT NULL,
                        currency TEXT NOT NULL DEFAULT 'USD',
                        description TEXT,
                        is_active INTEGER NOT NULL DEFAULT 1
                    );

                    CREATE TABLE IF NOT EXISTS transactions (
                        id TEXT PRIMARY KEY,
                        date TEXT NOT NULL,
                        payee TEXT NOT NULL,
                        memo TEXT,
                        source TEXT NOT NULL DEFAULT 'cli',
                        status TEXT NOT NULL DEFAULT 'posted',
                        idempotency_hash TEXT UNIQUE,
                        created_at INTEGER NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS postings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        transaction_id TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
                        account_id TEXT NOT NULL REFERENCES accounts(id),
                        amount_cents INTEGER NOT NULL,
                        memo TEXT,
                        sequence INTEGER NOT NULL DEFAULT 0
                    );

                    CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date DESC);
                    CREATE INDEX IF NOT EXISTS idx_transactions_payee ON transactions(payee);
                    CREATE INDEX IF NOT EXISTS idx_postings_account ON postings(account_id);
                    CREATE INDEX IF NOT EXISTS idx_postings_tx ON postings(transaction_id);

                    CREATE TABLE IF NOT EXISTS recurring_rules (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        rule_type TEXT NOT NULL,
                        frequency TEXT NOT NULL,
                        due_day_of_month INTEGER NOT NULL,
                        base_amount_cents INTEGER NOT NULL,
                        from_account TEXT NOT NULL REFERENCES accounts(id),
                        to_account TEXT NOT NULL REFERENCES accounts(id),
                        is_autopay INTEGER NOT NULL DEFAULT 0,
                        lead_time_days INTEGER NOT NULL DEFAULT 5,
                        is_active INTEGER NOT NULL DEFAULT 1,
                        notes TEXT
                    );

                    CREATE TABLE IF NOT EXISTS bill_instances (
                        id TEXT PRIMARY KEY,
                        rule_id TEXT NOT NULL REFERENCES recurring_rules(id) ON DELETE CASCADE,
                        billing_cycle TEXT NOT NULL,
                        due_date TEXT NOT NULL,
                        estimated_amount_cents INTEGER NOT NULL,
                        actual_amount_cents INTEGER,
                        status TEXT NOT NULL DEFAULT 'scheduled',
                        matched_transaction_id TEXT REFERENCES transactions(id) ON DELETE SET NULL,
                        updated_at INTEGER NOT NULL
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS idx_bill_instance_cycle 
                        ON bill_instances(rule_id, billing_cycle);
                """)

        if self.db_path.exists():
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass

        # Seed default user accounts if table is empty
        self._seed_default_accounts()

    @contextlib.contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def _write_lock(self) -> Generator[None, None, None]:
        """Advisory file lock to serialize concurrent write access."""
        lock_file = open(self.lock_path, "a+")
        try:
            try:
                os.chmod(self.lock_path, 0o600)
            except OSError:
                pass
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def _seed_default_accounts(self) -> None:
        """Seeds default user accounts if chart of accounts is empty."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as count FROM accounts;")
            count = cursor.fetchone()["count"]
            if count == 0:
                for acc in DEFAULT_USER_ACCOUNTS:
                    cursor.execute(
                        """
                        INSERT OR IGNORE INTO accounts (id, account_type, currency, description, is_active)
                        VALUES (?, ?, ?, ?, ?);
                        """,
                        (acc.id, acc.account_type.value, acc.currency, acc.description, 1 if acc.is_active else 0),
                    )
                conn.commit()

    # ==========================================================================
    # Account Management
    # ==========================================================================
    def add_account(self, account: Account) -> None:
        """Adds a new account to the chart of accounts."""
        with self._write_lock():
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO accounts (id, account_type, currency, description, is_active)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        account_type = excluded.account_type,
                        currency = excluded.currency,
                        description = excluded.description,
                        is_active = excluded.is_active;
                    """,
                    (account.id, account.account_type.value, account.currency, account.description, 1 if account.is_active else 0),
                )
                conn.commit()

    def get_account(self, account_id: str) -> Optional[Account]:
        """Fetches an account by ID."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?;", (account_id,)).fetchone()
            if row:
                return Account(
                    id=row["id"],
                    account_type=AccountType(row["account_type"]),
                    currency=row["currency"],
                    description=row["description"] or "",
                    is_active=bool(row["is_active"]),
                )
            return None

    def list_accounts(self, active_only: bool = True) -> List[Account]:
        """Lists all accounts in the chart of accounts."""
        with self._get_connection() as conn:
            query = "SELECT * FROM accounts"
            params: list[Any] = []
            if active_only:
                query += " WHERE is_active = 1"
            query += " ORDER BY account_type, id;"
            rows = conn.execute(query, params).fetchall()
            return [
                Account(
                    id=r["id"],
                    account_type=AccountType(r["account_type"]),
                    currency=r["currency"],
                    description=r["description"] or "",
                    is_active=bool(r["is_active"]),
                )
                for r in rows
            ]

    # ==========================================================================
    # Transaction Management (Strict Double-Entry)
    # ==========================================================================
    def log_transaction(self, transaction: Transaction) -> Transaction:
        """Atomically validates and logs a double-entry transaction.
        
        Enforces:
        - Exact sum of postings == 0
        - Valid accounts exist
        - Idempotency uniqueness
        """
        if not transaction.is_balanced():
            offset = transaction.balance_offset_cents()
            raise UnbalancedTransactionError(
                f"Cannot log transaction {transaction.id}: Postings are unbalanced by {offset} cents. "
                f"Sum of postings must equal exactly zero."
            )

        if not transaction.postings:
            raise ValueError(f"Transaction {transaction.id} must have at least two postings.")

        now = int(time.time())
        created_at = transaction.created_at or now

        with self._write_lock():
            with self._get_connection() as conn:
                cursor = conn.cursor()

                # Verify all referenced accounts exist
                for p in transaction.postings:
                    row = cursor.execute("SELECT 1 FROM accounts WHERE id = ?;", (p.account_id,)).fetchone()
                    if not row:
                        raise AccountNotFoundError(f"Account '{p.account_id}' does not exist in chart of accounts.")

                # Check idempotency hash to prevent accidental duplicate entries
                if transaction.idempotency_hash:
                    existing = cursor.execute(
                        "SELECT id FROM transactions WHERE idempotency_hash = ?;",
                        (transaction.idempotency_hash,),
                    ).fetchone()
                    if existing:
                        logger.info(f"Idempotent transaction already exists: {existing['id']}")
                        return self.get_transaction(existing["id"]) or transaction

                # Insert master transaction record
                cursor.execute(
                    """
                    INSERT INTO transactions (id, date, payee, memo, source, status, idempotency_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        transaction.id,
                        transaction.date,
                        transaction.payee,
                        transaction.memo,
                        transaction.source,
                        transaction.status,
                        transaction.idempotency_hash,
                        created_at,
                    ),
                )

                # Insert individual posting legs
                for seq, p in enumerate(transaction.postings):
                    cursor.execute(
                        """
                        INSERT INTO postings (transaction_id, account_id, amount_cents, memo, sequence)
                        VALUES (?, ?, ?, ?, ?);
                        """,
                        (transaction.id, p.account_id, p.amount_cents, p.memo, seq),
                    )

                conn.commit()

        return transaction

    def get_transaction(self, tx_id: str) -> Optional[Transaction]:
        """Retrieves a transaction and its postings by ID."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM transactions WHERE id = ?;", (tx_id,)).fetchone()
            if not row:
                return None

            postings_rows = conn.execute(
                "SELECT * FROM postings WHERE transaction_id = ? ORDER BY sequence;",
                (tx_id,),
            ).fetchall()

            postings = [
                Posting(
                    account_id=p["account_id"],
                    amount_cents=p["amount_cents"],
                    memo=p["memo"],
                )
                for p in postings_rows
            ]

            return Transaction(
                id=row["id"],
                date=row["date"],
                payee=row["payee"],
                memo=row["memo"] or "",
                source=row["source"],
                status=row["status"],
                postings=postings,
                idempotency_hash=row["idempotency_hash"],
                created_at=row["created_at"],
            )

    def list_transactions(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        account_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Transaction]:
        """Lists transactions matching date and/or account criteria."""
        with self._get_connection() as conn:
            query = """
                SELECT DISTINCT t.id, t.date, t.payee, t.memo, t.source, t.status, t.idempotency_hash, t.created_at
                FROM transactions t
            """
            joins = []
            conditions = []
            params: list[Any] = []

            if account_id:
                joins.append("JOIN postings p ON t.id = p.transaction_id")
                conditions.append("p.account_id = ?")
                params.append(account_id)

            if start_date:
                conditions.append("t.date >= ?")
                params.append(start_date)

            if end_date:
                conditions.append("t.date <= ?")
                params.append(end_date)

            if joins:
                query += " " + " ".join(joins)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            query += " ORDER BY t.date DESC, t.created_at DESC LIMIT ? OFFSET ?;"
            params.extend([limit, offset])

            rows = conn.execute(query, params).fetchall()

            results: List[Transaction] = []
            for r in rows:
                p_rows = conn.execute(
                    "SELECT * FROM postings WHERE transaction_id = ? ORDER BY sequence;",
                    (r["id"],),
                ).fetchall()
                postings = [
                    Posting(
                        account_id=p["account_id"],
                        amount_cents=p["amount_cents"],
                        memo=p["memo"],
                    )
                    for p in p_rows
                ]
                results.append(
                    Transaction(
                        id=r["id"],
                        date=r["date"],
                        payee=r["payee"],
                        memo=r["memo"] or "",
                        source=r["source"],
                        status=r["status"],
                        postings=postings,
                        idempotency_hash=r["idempotency_hash"],
                        created_at=r["created_at"],
                    )
                )
            return results

    def delete_transaction(self, tx_id: str) -> bool:
        """Deletes a transaction and its postings."""
        with self._write_lock():
            with self._get_connection() as conn:
                res = conn.execute("DELETE FROM transactions WHERE id = ?;", (tx_id,))
                conn.commit()
                return res.rowcount > 0

    # ==========================================================================
    # Balances & Aggregations (Deterministic Calculations)
    # ==========================================================================
    def get_account_balance(self, account_id: str, as_of_date: Optional[str] = None) -> int:
        """Returns the exact net balance of an account in integer cents.
        
        For ASSETS and EXPENSES: Debits are positive (Balance = sum of postings).
        For LIABILITIES, INCOME, EQUITY: Credits are negative in postings, so we invert sign 
        to return positive balances (e.g. Credit Card owes $400 -> 40000 cents).
        """
        account = self.get_account(account_id)
        if not account:
            raise AccountNotFoundError(f"Account '{account_id}' not found.")

        with self._get_connection() as conn:
            query = """
                SELECT COALESCE(SUM(p.amount_cents), 0) as balance_cents
                FROM postings p
                JOIN transactions t ON p.transaction_id = t.id
                WHERE p.account_id = ?
            """
            params: list[Any] = [account_id]
            if as_of_date:
                query += " AND t.date <= ?"
                params.append(as_of_date)

            row = conn.execute(query, params).fetchone()
            raw_cents = int(row["balance_cents"])

            # Invert sign for Liabilities, Income, and Equity for human-readable reporting
            if account.account_type in (AccountType.LIABILITY, AccountType.INCOME, AccountType.EQUITY):
                return -raw_cents
            return raw_cents

    def get_all_balances(self, as_of_date: Optional[str] = None) -> Dict[str, Any]:
        """Computes comprehensive balance summary across all accounts down to the cent."""
        accounts = self.list_accounts(active_only=True)
        liquid_assets_cents = 0
        liabilities_cents = 0
        account_balances: Dict[str, int] = {}

        for acc in accounts:
            bal = self.get_account_balance(acc.id, as_of_date)
            account_balances[acc.id] = bal
            if acc.account_type == AccountType.ASSET:
                liquid_assets_cents += bal
            elif acc.account_type == AccountType.LIABILITY:
                liabilities_cents += bal

        net_worth_cents = liquid_assets_cents - liabilities_cents

        return {
            "liquid_assets_cents": liquid_assets_cents,
            "liabilities_cents": liabilities_cents,
            "net_worth_cents": net_worth_cents,
            "account_balances": account_balances,
            "as_of_date": as_of_date or time.strftime("%Y-%m-%d"),
        }

    def get_cash_flow_summary(self, start_date: str, end_date: str) -> CashFlowSummary:
        """Calculates exact deterministic cash flow, savings rate, and category rollups."""
        with self._get_connection() as conn:
            # Query all expense postings in window
            expense_rows = conn.execute(
                """
                SELECT p.account_id, SUM(p.amount_cents) as total_cents
                FROM postings p
                JOIN transactions t ON p.transaction_id = t.id
                JOIN accounts a ON p.account_id = a.id
                WHERE a.account_type = 'EXPENSE'
                  AND t.date >= ? AND t.date <= ?
                GROUP BY p.account_id
                ORDER BY total_cents DESC;
                """,
                (start_date, end_date),
            ).fetchall()

            # Query all income postings in window (credits in double-entry are negative, so we negate)
            income_row = conn.execute(
                """
                SELECT -COALESCE(SUM(p.amount_cents), 0) as total_income_cents
                FROM postings p
                JOIN transactions t ON p.transaction_id = t.id
                JOIN accounts a ON p.account_id = a.id
                WHERE a.account_type = 'INCOME'
                  AND t.date >= ? AND t.date <= ?;
                """,
                (start_date, end_date),
            ).fetchone()

            inflow_cents = max(0, int(income_row["total_income_cents"]))
            category_breakdown: Dict[str, int] = {}
            outflow_cents = 0

            for r in expense_rows:
                cents = int(r["total_cents"])
                category_breakdown[r["account_id"]] = cents
                outflow_cents += cents

            net_savings_cents = inflow_cents - outflow_cents
            savings_rate_pct = (
                round((net_savings_cents / inflow_cents) * 100.0, 1)
                if inflow_cents > 0
                else 0.0
            )

            # Compute evaluated days
            from datetime import datetime
            d1 = datetime.strptime(start_date, "%Y-%m-%d")
            d2 = datetime.strptime(end_date, "%Y-%m-%d")
            evaluated_days = max(1, (d2 - d1).days + 1)
            daily_burn_rate_cents = round(outflow_cents / evaluated_days)

            return CashFlowSummary(
                start_date=start_date,
                end_date=end_date,
                inflow_cents=inflow_cents,
                outflow_cents=outflow_cents,
                net_savings_cents=net_savings_cents,
                savings_rate_pct=savings_rate_pct,
                category_breakdown=category_breakdown,
                daily_burn_rate_cents=daily_burn_rate_cents,
                evaluated_days=evaluated_days,
            )
