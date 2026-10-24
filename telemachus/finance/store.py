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
    GoalProgress,
    MonthlyOverview,
    PaycheckPlan,
    Posting,
    RecurringRule,
    RuleType,
    Transaction,
)

logger = logging.getLogger(__name__)

def get_finance_root() -> Path:
    """Returns canonical finance root directory, allowing dynamic env override."""
    return Path(
        os.environ.get("TELEMACHUS_FINANCE_DIR") or (Path.home() / ".memory" / "private" / "finances")
    ).expanduser().resolve()

DEFAULT_FINANCE_ROOT = get_finance_root()


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
        self.root_dir = Path(root_dir).expanduser().resolve() if root_dir else get_finance_root()
        self.db_path = self.root_dir / "finances.sqlite3"
        self.lock_path = self.root_dir / ".lock"
        self.setup_yaml_path: Optional[Path] = None
        self._init_storage()

    def get_setup_yaml_path(self) -> Optional[Path]:
        """Resolves the setup.yaml path corresponding to this store."""
        if self.setup_yaml_path and self.setup_yaml_path.exists():
            return self.setup_yaml_path
        if os.environ.get("TELEMACHUS_SETUP_YAML"):
            p = Path(os.environ["TELEMACHUS_SETUP_YAML"]).expanduser().resolve()
            if p.exists():
                return p
        sibling_yaml = self.root_dir / "setup.yaml"
        if sibling_yaml.exists():
            return sibling_yaml
        if self.root_dir == get_finance_root():
            default_p = get_finance_root() / "setup.yaml"
            if default_p.exists():
                return default_p
        return None

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

                    CREATE TABLE IF NOT EXISTS monthly_overview (
                        month TEXT PRIMARY KEY,
                        status TEXT NOT NULL DEFAULT 'open',
                        income_cents INTEGER NOT NULL DEFAULT 0,
                        savings_target_cents INTEGER NOT NULL DEFAULT 0,
                        fixed_bills_cents INTEGER NOT NULL DEFAULT 26071,
                        pge_cents INTEGER NOT NULL DEFAULT 0,
                        groceries_planned_cents INTEGER NOT NULL DEFAULT 0,
                        dining_planned_cents INTEGER NOT NULL DEFAULT 0,
                        gas_planned_cents INTEGER NOT NULL DEFAULT 0,
                        notes TEXT,
                        updated_at INTEGER NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS paycheck_plan (
                        id TEXT PRIMARY KEY DEFAULT 'default',
                        paycheck_amount_cents INTEGER NOT NULL,
                        savings_per_paycheck_cents INTEGER NOT NULL,
                        frequency TEXT NOT NULL DEFAULT 'biweekly',
                        anchor_date TEXT NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                """)
                # Migrations for existing databases
                try:
                    conn.execute("ALTER TABLE monthly_overview ADD COLUMN dining_planned_cents INTEGER NOT NULL DEFAULT 0;")
                except sqlite3.OperationalError:
                    pass

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
        """Seeds default user accounts using INSERT OR IGNORE."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
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

    def delete_day_transactions(self, date_str: str, include_setup: bool = False) -> int:
        """Deletes all transactions for a specific date (defaults to preserving setup opening balances)."""
        with self._write_lock():
            with self._get_connection() as conn:
                if include_setup:
                    res = conn.execute("DELETE FROM transactions WHERE date = ?;", (date_str,))
                else:
                    res = conn.execute(
                        "DELETE FROM transactions WHERE date = ? AND (source != 'setup' OR source IS NULL);",
                        (date_str,),
                    )
                conn.commit()
                return res.rowcount

    def delete_all_imported_transactions(self) -> int:
        """Deletes all imported and logged transactions, preserving initial setup balances."""
        with self._write_lock():
            with self._get_connection() as conn:
                res = conn.execute("DELETE FROM transactions WHERE source != 'setup' OR source IS NULL;")
                conn.commit()
                return res.rowcount

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

    # ==========================================================================
    # Monthly Overview (Matching Notion Formulas Exactly)
    # ==========================================================================
    def set_monthly_overview(
        self,
        month: str,
        income_cents: int,
        savings_target_cents: int,
        groceries_planned_cents: int,
        gas_planned_cents: int,
        dining_planned_cents: int = 0,
        fixed_bills_cents: int = 26071,
        pge_cents: int = 0,
        status: str = "open",
        notes: str = "",
    ) -> MonthlyOverview:
        """Sets or updates baseline budget targets for a specific month."""
        now = int(time.time())
        with self._write_lock():
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO monthly_overview (
                        month, status, income_cents, savings_target_cents,
                        fixed_bills_cents, pge_cents, groceries_planned_cents,
                        dining_planned_cents, gas_planned_cents, notes, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(month) DO UPDATE SET
                        status = excluded.status,
                        income_cents = excluded.income_cents,
                        savings_target_cents = excluded.savings_target_cents,
                        fixed_bills_cents = excluded.fixed_bills_cents,
                        pge_cents = excluded.pge_cents,
                        groceries_planned_cents = excluded.groceries_planned_cents,
                        dining_planned_cents = excluded.dining_planned_cents,
                        gas_planned_cents = excluded.gas_planned_cents,
                        notes = excluded.notes,
                        updated_at = excluded.updated_at;
                    """,
                    (
                        month,
                        status,
                        income_cents,
                        savings_target_cents,
                        fixed_bills_cents,
                        pge_cents,
                        groceries_planned_cents,
                        dining_planned_cents,
                        gas_planned_cents,
                        notes,
                        now,
                    ),
                )
                conn.commit()
        return self.get_monthly_overview(month)

    def get_monthly_overview(self, month: Optional[str] = None) -> MonthlyOverview:
        """Fetches monthly overview with actuals dynamically calculated from postings."""
        if not month:
            from datetime import datetime
            month = datetime.now().strftime("%Y-%m")

        start_date = f"{month}-01"
        import calendar
        y, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(y, m)
        end_date = f"{month}-{last_day:02d}"

        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM monthly_overview WHERE month = ?;", (month,)).fetchone()

            # Dynamic actuals from postings
            groc_row = conn.execute(
                """
                SELECT COALESCE(SUM(p.amount_cents), 0) as total
                FROM postings p
                JOIN transactions t ON p.transaction_id = t.id
                WHERE p.account_id IN ('Expenses:Food:Groceries', 'Expenses:Groceries')
                  AND t.date >= ? AND t.date <= ?;
                """,
                (start_date, end_date),
            ).fetchone()
            groceries_actual = int(groc_row["total"])

            dining_row = conn.execute(
                """
                SELECT COALESCE(SUM(p.amount_cents), 0) as total
                FROM postings p
                JOIN transactions t ON p.transaction_id = t.id
                WHERE p.account_id IN ('Expenses:Food:Dining', 'Expenses:Dining')
                  AND t.date >= ? AND t.date <= ?;
                """,
                (start_date, end_date),
            ).fetchone()
            dining_actual = int(dining_row["total"])

            gas_row = conn.execute(
                """
                SELECT COALESCE(SUM(p.amount_cents), 0) as total
                FROM postings p
                JOIN transactions t ON p.transaction_id = t.id
                WHERE p.account_id IN ('Expenses:Transport:Gas', 'Expenses:Gas')
                  AND t.date >= ? AND t.date <= ?;
                """,
                (start_date, end_date),
            ).fetchone()
            gas_actual = int(gas_row["total"])

            inc_row = conn.execute(
                """
                SELECT -COALESCE(SUM(p.amount_cents), 0) as total
                FROM postings p
                JOIN transactions t ON p.transaction_id = t.id
                WHERE p.account_id LIKE 'Income:%'
                  AND t.date >= ? AND t.date <= ?;
                """,
                (start_date, end_date),
            ).fetchone()
            actual_income = max(0, int(inc_row["total"]))

            pge_row = conn.execute(
                """
                SELECT COALESCE(SUM(p.amount_cents), 0) as total
                FROM postings p
                JOIN transactions t ON p.transaction_id = t.id
                WHERE p.account_id = 'Expenses:Utilities:PGE'
                  AND t.date >= ? AND t.date <= ?;
                """,
                (start_date, end_date),
            ).fetchone()
            pge_actual = int(pge_row["total"])

            if row:
                income = row["income_cents"] or actual_income
                pge = row["pge_cents"] or pge_actual
                dining_planned = row["dining_planned_cents"] if "dining_planned_cents" in row.keys() else 0
                return MonthlyOverview(
                    month=row["month"],
                    status=row["status"],
                    income_cents=income,
                    savings_target_cents=row["savings_target_cents"],
                    fixed_bills_cents=row["fixed_bills_cents"],
                    pge_cents=pge,
                    groceries_planned_cents=row["groceries_planned_cents"],
                    groceries_actual_cents=groceries_actual,
                    dining_planned_cents=dining_planned,
                    dining_actual_cents=dining_actual,
                    gas_planned_cents=row["gas_planned_cents"],
                    gas_actual_cents=gas_actual,
                    notes=row["notes"] or "",
                    updated_at=row["updated_at"],
                )
            else:
                return MonthlyOverview(
                    month=month,
                    status="open",
                    income_cents=actual_income,
                    savings_target_cents=0,
                    fixed_bills_cents=26071,
                    pge_cents=pge_actual,
                    groceries_planned_cents=0,
                    groceries_actual_cents=groceries_actual,
                    dining_planned_cents=0,
                    dining_actual_cents=dining_actual,
                    gas_planned_cents=0,
                    gas_actual_cents=gas_actual,
                    notes="",
                    updated_at=int(time.time()),
                )

    # ==========================================================================
    # Goals Progress Tracker (Screenshot 4)
    # ==========================================================================
    def get_goals_progress(self, month: Optional[str] = None) -> List[GoalProgress]:
        """Calculates progress toward monthly Savings and Kahram payment targets."""
        if not month:
            from datetime import datetime
            month = datetime.now().strftime("%Y-%m")

        overview = self.get_monthly_overview(month)
        start_date = f"{month}-01"
        import calendar
        y, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(y, m)
        end_date = f"{month}-{last_day:02d}"

        goals: List[GoalProgress] = []

        # Read setup.yaml for live targets and baseline already_paid amounts
        setup_data = None
        yaml_p = self.get_setup_yaml_path()
        if yaml_p:
            try:
                from telemachus.finance.setup import load_setup_yaml, _parse_cents
                setup_data = load_setup_yaml(yaml_p)
            except Exception:
                pass

        # 1. Savings Goal Target and Baseline
        savings_target = overview.savings_target_cents
        savings_baseline = None

        if setup_data:
            # A. Check starting_balances for Capital One Savings balance (as requested)
            sb = setup_data.get("starting_balances") or {}
            for k, v in sb.items():
                clean_k = k.lower().replace("-", "_").replace(" ", "_")
                if any(term in clean_k for term in ("capital", "capitol", "savings", "hysa")):
                    if not any(excl in clean_k for excl in ("checking", "visa", "credit")):
                        val = _parse_cents(v)
                        if val is not None:
                            savings_baseline = val
                            break

            # B. Check goals.savings
            goals_cfg = setup_data.get("goals") or {}
            sav_cfg = goals_cfg.get("savings") or goals_cfg.get("saving") or {}
            if isinstance(sav_cfg, dict):
                t = _parse_cents(sav_cfg.get("target") or sav_cfg.get("amount") or sav_cfg.get("goal"))
                if t is not None:
                    savings_target = t
                if savings_baseline is None:
                    b = _parse_cents(
                        sav_cfg.get("already_saved")
                        or sav_cfg.get("saved")
                        or sav_cfg.get("current")
                        or sav_cfg.get("already_paid")
                        or sav_cfg.get("paid")
                        or sav_cfg.get("actual")
                    )
                    if b is not None:
                        savings_baseline = b
            elif isinstance(sav_cfg, (int, float, str)):
                t = _parse_cents(sav_cfg)
                if t is not None:
                    savings_target = t

            # C. Check top-level setup_data.get("savings") or setup_data.get("savings_goal")
            top_sav = setup_data.get("savings") or setup_data.get("savings_goal") or {}
            if isinstance(top_sav, dict):
                t = _parse_cents(top_sav.get("target") or top_sav.get("amount"))
                if t is not None and savings_target == 0:
                    savings_target = t
                if savings_baseline is None:
                    b = _parse_cents(
                        top_sav.get("already_saved")
                        or top_sav.get("saved")
                        or top_sav.get("current")
                        or top_sav.get("already_paid")
                        or top_sav.get("paid")
                        or top_sav.get("actual")
                    )
                    if b is not None:
                        savings_baseline = b

            # D. Check monthly_targets for savings_target / already_saved
            mt = setup_data.get("monthly_targets") or {}
            if isinstance(mt, dict):
                if savings_target == 0:
                    t = _parse_cents(mt.get("savings_target") or mt.get("savings"))
                    if t is not None:
                        savings_target = t
                if savings_baseline is None:
                    b = _parse_cents(
                        mt.get("already_saved")
                        or mt.get("savings_saved")
                        or mt.get("savings_actual")
                    )
                    if b is not None:
                        savings_baseline = b

        if savings_target <= 0:
            savings_target = 100000  # Default $1,000.00 monthly savings target

        # 2. Brother / Kahram Payment Goal Target and Baseline
        kahram_target = 10000
        kahram_baseline = None
        if setup_data:
            goals_cfg = setup_data.get("goals") or {}
            bro_cfg = (
                goals_cfg.get("brother")
                or goals_cfg.get("kahram")
                or goals_cfg.get("brother_payment")
                or {}
            )
            if isinstance(bro_cfg, dict):
                t = _parse_cents(bro_cfg.get("target") or bro_cfg.get("amount") or bro_cfg.get("goal"))
                if t is not None:
                    kahram_target = t
                b = _parse_cents(
                    bro_cfg.get("already_paid")
                    or bro_cfg.get("paid")
                    or bro_cfg.get("current")
                    or bro_cfg.get("already_saved")
                    or bro_cfg.get("saved")
                    or bro_cfg.get("actual")
                )
                if b is not None:
                    kahram_baseline = b
            elif isinstance(bro_cfg, (int, float, str)):
                t = _parse_cents(bro_cfg)
                if t is not None:
                    kahram_target = t

            # Fallback to fixed_bills.kahram or fixed_bills.brother if not in goals section
            fb = setup_data.get("fixed_bills") or {}
            k_bill = fb.get("kahram") or fb.get("brother") or {}
            if isinstance(k_bill, dict):
                if kahram_target == 10000:
                    t = _parse_cents(k_bill.get("amount") or k_bill.get("target"))
                    if t is not None:
                        kahram_target = t
                if kahram_baseline is None:
                    b = _parse_cents(
                        k_bill.get("already_paid")
                        or k_bill.get("paid")
                        or k_bill.get("current")
                        or k_bill.get("actual")
                    )
                    if b is not None:
                        kahram_baseline = b

            # Also check top-level setup_data.get("brother") or setup_data.get("kahram")
            top_bro = setup_data.get("brother") or setup_data.get("kahram") or {}
            if isinstance(top_bro, dict):
                if kahram_target == 10000:
                    t = _parse_cents(top_bro.get("target") or top_bro.get("amount"))
                    if t is not None:
                        kahram_target = t
                if kahram_baseline is None:
                    b = _parse_cents(
                        top_bro.get("already_paid")
                        or top_bro.get("paid")
                        or top_bro.get("current")
                        or top_bro.get("actual")
                    )
                    if b is not None:
                        kahram_baseline = b

        # 3. Determine Progress Amounts (Prioritizing setup.yaml source of truth)
        if savings_baseline is not None:
            saved_cents = max(0, savings_baseline)
        else:
            try:
                saved_cents = max(0, self.get_account_balance("Assets:Savings:CapitalOne"))
            except Exception:
                with self._get_connection() as conn:
                    sav_row = conn.execute(
                        """
                        SELECT COALESCE(SUM(p.amount_cents), 0) as total
                        FROM postings p
                        JOIN transactions t ON p.transaction_id = t.id
                        WHERE p.account_id = 'Assets:Savings:CapitalOne'
                          AND t.date >= ? AND t.date <= ?;
                        """,
                        (start_date, end_date),
                    ).fetchone()
                    saved_cents = max(0, int(sav_row["total"]))

        goals.append(
            GoalProgress(
                name="💰 Savings Target",
                target_cents=savings_target,
                saved_cents=saved_cents,
                status="completed" if saved_cents >= savings_target else "open",
            )
        )

        if kahram_baseline is not None:
            total_kahram_paid = max(0, kahram_baseline)
        else:
            with self._get_connection() as conn:
                kahram_row = conn.execute(
                    """
                    SELECT COALESCE(SUM(p.amount_cents), 0) as total
                    FROM postings p
                    JOIN transactions t ON p.transaction_id = t.id
                    WHERE p.account_id IN ('Expenses:Family:Kahram', 'Expenses:Family:Brother')
                      AND t.date >= ? AND t.date <= ?;
                    """,
                    (start_date, end_date),
                ).fetchone()
                total_kahram_paid = max(0, int(kahram_row["total"]))

        if kahram_target == 10000:
            with self._get_connection() as conn:
                rule_row = conn.execute("SELECT base_amount_cents FROM recurring_rules WHERE id = 'rule_kahram';").fetchone()
                if rule_row:
                    kahram_target = int(rule_row["base_amount_cents"])

        goals.append(
            GoalProgress(
                name="🏠 Brother (Kahram) Payment",
                target_cents=kahram_target,
                saved_cents=total_kahram_paid,
                status="completed" if total_kahram_paid >= kahram_target else "open",
            )
        )

        return goals

    # ==========================================================================
    # Quick Batch Ingestion (Matching Daily List Bullet Points)
    # ==========================================================================
    def log_batch_transactions(
        self,
        items: List[Dict[str, Any]],
        default_date: Optional[str] = None,
    ) -> List[Transaction]:
        """Atomically ingests a batch of daily spending and income bullet items."""
        from datetime import datetime
        today_str = default_date or datetime.now().strftime("%Y-%m-%d")

        CATEGORY_MAP = {
            "groceries": ("Expenses:Food:Groceries", "Assets:Checking:WellsFargo"),
            "grocery": ("Expenses:Food:Groceries", "Assets:Checking:WellsFargo"),
            "dining": ("Expenses:Food:Dining", "Assets:Checking:WellsFargo"),
            "dining out": ("Expenses:Food:Dining", "Assets:Checking:WellsFargo"),
            "eating out": ("Expenses:Food:Dining", "Assets:Checking:WellsFargo"),
            "lunch": ("Expenses:Food:Dining", "Assets:Checking:WellsFargo"),
            "dinner": ("Expenses:Food:Dining", "Assets:Checking:WellsFargo"),
            "coffee": ("Expenses:Food:Dining", "Assets:Checking:WellsFargo"),
            "gas": ("Expenses:Transport:Gas", "Assets:Checking:WellsFargo"),
            "fuel": ("Expenses:Transport:Gas", "Assets:Checking:WellsFargo"),
            "payday": ("Assets:Checking:WellsFargo", "Income:Salary"),
            "salary": ("Assets:Checking:WellsFargo", "Income:Salary"),
            "income": ("Assets:Checking:WellsFargo", "Income:Salary"),
            "kahram": ("Expenses:Family:Kahram", "Assets:Checking:WellsFargo"),
            "brother": ("Expenses:Family:Kahram", "Assets:Checking:WellsFargo"),
            "gym": ("Expenses:Subscriptions:Gym", "Assets:Checking:WellsFargo"),
            "spotify": ("Expenses:Subscriptions:Spotify", "Assets:Checking:WellsFargo"),
            "icloud": ("Expenses:Subscriptions:AppleCloud", "Assets:Checking:WellsFargo"),
            "wifi": ("Expenses:Utilities:WiFi", "Assets:Checking:WellsFargo"),
            "pge": ("Expenses:Utilities:PGE", "Assets:Checking:WellsFargo"),
            "personal": ("Expenses:Personal:Discretionary", "Assets:Checking:WellsFargo"),
            "savings": ("Assets:Savings:CapitalOne", "Assets:Checking:WellsFargo"),
            "saving": ("Assets:Savings:CapitalOne", "Assets:Checking:WellsFargo"),
            "hysa": ("Assets:Savings:CapitalOne", "Assets:Checking:WellsFargo"),
            "capital one": ("Assets:Savings:CapitalOne", "Assets:Checking:WellsFargo"),
            "savings deposit": ("Assets:Savings:CapitalOne", "Assets:Checking:WellsFargo"),
            "savings transfer": ("Assets:Savings:CapitalOne", "Assets:Checking:WellsFargo"),
            "credit_card_payment": ("Liabilities:CreditCard:Visa", "Assets:Checking:WellsFargo"),
            "credit card payment": ("Liabilities:CreditCard:Visa", "Assets:Checking:WellsFargo"),
            "credit card": ("Liabilities:CreditCard:Visa", "Assets:Checking:WellsFargo"),
            "cc payment": ("Liabilities:CreditCard:Visa", "Assets:Checking:WellsFargo"),
            "visa payment": ("Liabilities:CreditCard:Visa", "Assets:Checking:WellsFargo"),
            "card payment": ("Liabilities:CreditCard:Visa", "Assets:Checking:WellsFargo"),
        }

        logged_txs = []
        for item in items:
            raw_cat = str(item.get("category", "")).strip().lower()
            payee = item.get("payee") or raw_cat.title()
            amount_val = float(item["amount"])
            amount_cents = round(abs(amount_val) * 100)
            date = item.get("date") or today_str
            memo = item.get("memo", "")

            if raw_cat in CATEGORY_MAP:
                target_acc, source_acc = CATEGORY_MAP[raw_cat]
            else:
                target_acc = item.get("to_account") or "Expenses:Personal:Discretionary"
                source_acc = item.get("from_account") or "Assets:Checking:WellsFargo"

            if item.get("from_account"):
                source_acc = item["from_account"]
            if item.get("to_account"):
                target_acc = item["to_account"]

            if raw_cat in ("payday", "salary", "income") or (amount_val > 0 and target_acc == "Assets:Checking:WellsFargo"):
                postings = [
                    Posting(account_id=source_acc, amount_cents=-amount_cents, memo=memo),
                    Posting(account_id=target_acc, amount_cents=amount_cents, memo=memo),
                ]
            else:
                postings = [
                    Posting(account_id=source_acc, amount_cents=-amount_cents, memo=memo),
                    Posting(account_id=target_acc, amount_cents=amount_cents, memo=memo),
                ]

            tx = Transaction.create(
                date=date,
                payee=payee,
                postings=postings,
                memo=memo,
                source="batch_bullet_ingestion",
            )
            logged = self.log_transaction(tx)
            logged_txs.append(logged)

        return logged_txs

    # ==========================================================================
    # Paycheck Planning
    # ==========================================================================
    def set_paycheck_plan(self, plan: PaycheckPlan) -> PaycheckPlan:
        """Saves or updates the active bi-weekly/semi-monthly paycheck plan."""
        now = int(time.time())
        with self._write_lock():
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO paycheck_plan (
                        id, paycheck_amount_cents, savings_per_paycheck_cents,
                        frequency, anchor_date, updated_at
                    ) VALUES ('default', ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        paycheck_amount_cents = excluded.paycheck_amount_cents,
                        savings_per_paycheck_cents = excluded.savings_per_paycheck_cents,
                        frequency = excluded.frequency,
                        anchor_date = excluded.anchor_date,
                        updated_at = excluded.updated_at;
                    """,
                    (
                        plan.paycheck_amount_cents,
                        plan.savings_per_paycheck_cents,
                        plan.frequency,
                        plan.anchor_date,
                        now,
                    ),
                )
                conn.commit()
        plan.updated_at = now
        return plan

    def get_paycheck_plan(self) -> Optional[PaycheckPlan]:
        """Returns the active paycheck plan if configured."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM paycheck_plan WHERE id = 'default';").fetchone()
            if not row:
                return None
            return PaycheckPlan(
                paycheck_amount_cents=row["paycheck_amount_cents"],
                savings_per_paycheck_cents=row["savings_per_paycheck_cents"],
                frequency=row["frequency"],
                anchor_date=row["anchor_date"],
                updated_at=row["updated_at"],
            )

