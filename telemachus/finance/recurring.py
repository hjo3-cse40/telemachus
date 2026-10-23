"""Recurring Obligations & Bill Management Engine for Telemachus.

Handles:
- Fixed recurring subscriptions and obligations (Internet, Brother, Gym, Spotify, Apple Cloud, Rent).
- Variable recurring bills (PG&E) using rolling statistical averages and seasonal buffers.
- 3-step lifecycle: SCHEDULED (estimated) -> BILLED (statement arrived) -> PAID (matched transaction).
- Committed cash flow and Safe-to-Spend runway calculations.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import math
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid

from telemachus.finance.models import (
    DEFAULT_USER_RECURRING_RULES,
    BillInstance,
    BillStatus,
    Frequency,
    RecurringRule,
    RuleType,
    Transaction,
)
from telemachus.finance.store import FinanceStore


class RecurringEngine:
    """Manages recurring bills and deterministic forward cash flow commitments."""

    def __init__(self, store: FinanceStore):
        self.store = store
        self._seed_default_rules()

    def _seed_default_rules(self) -> None:
        """Seeds default user recurring rules if table is empty."""
        with self.store._get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) as count FROM recurring_rules;").fetchone()["count"]
            if count == 0:
                with self.store._write_lock():
                    for rule in DEFAULT_USER_RECURRING_RULES:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO recurring_rules (
                                id, name, rule_type, frequency, due_day_of_month,
                                base_amount_cents, from_account, to_account, is_autopay,
                                lead_time_days, is_active, notes
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (
                                rule.id,
                                rule.name,
                                rule.rule_type.value,
                                rule.frequency.value,
                                rule.due_day_of_month,
                                rule.base_amount_cents,
                                rule.from_account,
                                rule.to_account,
                                1 if rule.is_autopay else 0,
                                rule.lead_time_days,
                                1 if rule.is_active else 0,
                                rule.notes,
                            ),
                        )
                    conn.commit()

    def add_rule(self, rule: RecurringRule) -> None:
        """Adds or updates a recurring rule."""
        with self.store._write_lock():
            with self.store._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO recurring_rules (
                        id, name, rule_type, frequency, due_day_of_month,
                        base_amount_cents, from_account, to_account, is_autopay,
                        lead_time_days, is_active, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        rule_type = excluded.rule_type,
                        frequency = excluded.frequency,
                        due_day_of_month = excluded.due_day_of_month,
                        base_amount_cents = excluded.base_amount_cents,
                        from_account = excluded.from_account,
                        to_account = excluded.to_account,
                        is_autopay = excluded.is_autopay,
                        lead_time_days = excluded.lead_time_days,
                        is_active = excluded.is_active,
                        notes = excluded.notes;
                    """,
                    (
                        rule.id,
                        rule.name,
                        rule.rule_type.value,
                        rule.frequency.value,
                        rule.due_day_of_month,
                        rule.base_amount_cents,
                        rule.from_account,
                        rule.to_account,
                        1 if rule.is_autopay else 0,
                        rule.lead_time_days,
                        1 if rule.is_active else 0,
                        rule.notes,
                    ),
                )
                conn.commit()

    def get_rule(self, rule_id: str) -> Optional[RecurringRule]:
        """Fetches a recurring rule by ID."""
        with self.store._get_connection() as conn:
            row = conn.execute("SELECT * FROM recurring_rules WHERE id = ?;", (rule_id,)).fetchone()
            if row:
                return RecurringRule(
                    id=row["id"],
                    name=row["name"],
                    rule_type=RuleType(row["rule_type"]),
                    frequency=Frequency(row["frequency"]),
                    due_day_of_month=row["due_day_of_month"],
                    base_amount_cents=row["base_amount_cents"],
                    from_account=row["from_account"],
                    to_account=row["to_account"],
                    is_autopay=bool(row["is_autopay"]),
                    lead_time_days=row["lead_time_days"],
                    is_active=bool(row["is_active"]),
                    notes=row["notes"] or "",
                )
            return None

    def list_rules(self, active_only: bool = True) -> List[RecurringRule]:
        """Lists recurring rules."""
        with self.store._get_connection() as conn:
            query = "SELECT * FROM recurring_rules"
            if active_only:
                query += " WHERE is_active = 1"
            query += " ORDER BY due_day_of_month;"
            rows = conn.execute(query).fetchall()
            return [
                RecurringRule(
                    id=r["id"],
                    name=r["name"],
                    rule_type=RuleType(r["rule_type"]),
                    frequency=Frequency(r["frequency"]),
                    due_day_of_month=r["due_day_of_month"],
                    base_amount_cents=r["base_amount_cents"],
                    from_account=r["from_account"],
                    to_account=r["to_account"],
                    is_autopay=bool(r["is_autopay"]),
                    lead_time_days=r["lead_time_days"],
                    is_active=bool(r["is_active"]),
                    notes=r["notes"] or "",
                )
                for r in rows
            ]

    # ==========================================================================
    # Variable Bill Estimation (PG&E Engine)
    # ==========================================================================
    def compute_variable_estimate(
        self,
        rule_id: str,
        billing_cycle: str,
        lookback_months: int = 6,
    ) -> Tuple[int, Dict[str, Any]]:
        """Calculates a deterministic rolling moving average + seasonal buffer for a variable bill.
        
        Returns:
            (conservative_estimate_cents, statistics_dict)
        """
        rule = self.get_rule(rule_id)
        if not rule:
            raise ValueError(f"Recurring rule '{rule_id}' not found.")

        # Query historical postings to target expense account
        with self.store._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT t.date, p.amount_cents
                FROM postings p
                JOIN transactions t ON p.transaction_id = t.id
                WHERE p.account_id = ?
                  AND p.amount_cents > 0
                ORDER BY t.date DESC
                LIMIT ?;
                """,
                (rule.to_account, lookback_months),
            ).fetchall()

        if not rows:
            # Fall back to base amount if no history
            return rule.base_amount_cents, {
                "count": 0,
                "mean_cents": rule.base_amount_cents,
                "median_cents": rule.base_amount_cents,
                "std_dev_cents": 0,
                "min_cents": rule.base_amount_cents,
                "max_cents": rule.base_amount_cents,
                "conservative_estimate_cents": rule.base_amount_cents,
                "source": "baseline_default",
            }

        amounts = [int(r["amount_cents"]) for r in rows]
        n = len(amounts)
        mean = sum(amounts) / n
        sorted_amounts = sorted(amounts)
        median = (
            sorted_amounts[n // 2]
            if n % 2 != 0
            else (sorted_amounts[n // 2 - 1] + sorted_amounts[n // 2]) // 2
        )
        variance = sum((x - mean) ** 2 for x in amounts) / n
        std_dev = math.sqrt(variance)

        # Recency-weighted average (most recent gets highest weight)
        weights = list(range(n, 0, -1))
        weighted_sum = sum(a * w for a, w in zip(amounts, weights))
        recency_weighted_avg = weighted_sum / sum(weights)

        # Conservative estimate: recency average + half a standard deviation buffer
        conservative = round(recency_weighted_avg + (0.5 * std_dev))

        stats = {
            "count": n,
            "mean_cents": round(mean),
            "median_cents": median,
            "std_dev_cents": round(std_dev),
            "min_cents": min(amounts),
            "max_cents": max(amounts),
            "recency_weighted_cents": round(recency_weighted_avg),
            "conservative_estimate_cents": conservative,
            "source": f"rolling_{n}_month_history",
        }
        return conservative, stats

    # ==========================================================================
    # Bill Instance Generation & Lifecycle
    # ==========================================================================
    def ensure_bill_instance(
        self,
        rule_id: str,
        billing_cycle: str,
        due_date: Optional[str] = None,
    ) -> BillInstance:
        """Retrieves or generates a bill instance for a specific billing cycle."""
        rule = self.get_rule(rule_id)
        if not rule:
            raise ValueError(f"Rule '{rule_id}' not found.")

        with self.store._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM bill_instances WHERE rule_id = ? AND billing_cycle = ?;",
                (rule_id, billing_cycle),
            ).fetchone()

            if row:
                return BillInstance(
                    id=row["id"],
                    rule_id=row["rule_id"],
                    billing_cycle=row["billing_cycle"],
                    due_date=row["due_date"],
                    estimated_amount_cents=row["estimated_amount_cents"],
                    actual_amount_cents=row["actual_amount_cents"],
                    status=BillStatus(row["status"]),
                    matched_transaction_id=row["matched_transaction_id"],
                    updated_at=row["updated_at"],
                )

        # Compute due date based on cycle and rule due day
        if not due_date:
            year, month = map(int, billing_cycle.split("-"))
            # Ensure day doesn't exceed month's days
            import calendar
            _, max_days = calendar.monthrange(year, month)
            day = min(rule.due_day_of_month, max_days)
            due_date = f"{year:04d}-{month:02d}-{day:02d}"

        # Determine estimated amount
        if rule.rule_type == RuleType.VARIABLE:
            est_cents, _ = self.compute_variable_estimate(rule_id, billing_cycle)
        else:
            est_cents = rule.base_amount_cents

        instance = BillInstance(
            id=str(uuid.uuid4()),
            rule_id=rule_id,
            billing_cycle=billing_cycle,
            due_date=due_date,
            estimated_amount_cents=est_cents,
            actual_amount_cents=None,
            status=BillStatus.SCHEDULED,
            updated_at=int(time.time()),
        )

        with self.store._write_lock():
            with self.store._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO bill_instances (
                        id, rule_id, billing_cycle, due_date, estimated_amount_cents,
                        actual_amount_cents, status, matched_transaction_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        instance.id,
                        instance.rule_id,
                        instance.billing_cycle,
                        instance.due_date,
                        instance.estimated_amount_cents,
                        instance.actual_amount_cents,
                        instance.status.value,
                        instance.matched_transaction_id,
                        instance.updated_at,
                    ),
                )
                conn.commit()

        return instance

    def set_actual_bill_amount(self, rule_id: str, billing_cycle: str, actual_cents: int) -> BillInstance:
        """Locks in the confirmed statement amount (Stage 2: BILLED)."""
        instance = self.ensure_bill_instance(rule_id, billing_cycle)
        instance.actual_amount_cents = actual_cents
        instance.status = BillStatus.BILLED
        instance.updated_at = int(time.time())

        with self.store._write_lock():
            with self.store._get_connection() as conn:
                conn.execute(
                    """
                    UPDATE bill_instances
                    SET actual_amount_cents = ?, status = 'billed', updated_at = ?
                    WHERE id = ?;
                    """,
                    (actual_cents, instance.updated_at, instance.id),
                )
                conn.commit()
        return instance

    def reconcile_with_transactions(self, lookback_days: int = 45) -> List[BillInstance]:
        """Scans recent transactions to match and mark bill instances as PAID."""
        reconciled = []
        rules = {r.id: r for r in self.list_rules(active_only=False)}

        today = datetime.now()
        start_date = (today - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        recent_txs = self.store.list_transactions(start_date=start_date, limit=100)

        with self.store._write_lock():
            with self.store._get_connection() as conn:
                cursor = conn.cursor()

                for tx in recent_txs:
                    for posting in tx.postings:
                        # Find rule matching this account
                        matching_rules = [r for r in rules.values() if r.to_account == posting.account_id]
                        if not matching_rules or posting.amount_cents <= 0:
                            continue

                        rule = matching_rules[0]
                        tx_cycle = tx.date[:7]  # YYYY-MM

                        # Find or create instance
                        inst_row = cursor.execute(
                            "SELECT * FROM bill_instances WHERE rule_id = ? AND billing_cycle = ?;",
                            (rule.id, tx_cycle),
                        ).fetchone()

                        if inst_row and inst_row["status"] == "paid":
                            continue

                        # Mark as paid
                        now = int(time.time())
                        if inst_row:
                            cursor.execute(
                                """
                                UPDATE bill_instances
                                SET status = 'paid', actual_amount_cents = ?, matched_transaction_id = ?, updated_at = ?
                                WHERE id = ?;
                                """,
                                (posting.amount_cents, tx.id, now, inst_row["id"]),
                            )
                        else:
                            cursor.execute(
                                """
                                INSERT INTO bill_instances (
                                    id, rule_id, billing_cycle, due_date, estimated_amount_cents,
                                    actual_amount_cents, status, matched_transaction_id, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, 'paid', ?, ?);
                                """,
                                (
                                    str(uuid.uuid4()),
                                    rule.id,
                                    tx_cycle,
                                    tx.date,
                                    posting.amount_cents,
                                    posting.amount_cents,
                                    tx.id,
                                    now,
                                ),
                            )
                conn.commit()

        return reconciled

    # ==========================================================================
    # Obligations Watchdog & Committed Cash Flow
    # ==========================================================================
    def get_upcoming_obligations(self, days_ahead: int = 30) -> List[Dict[str, Any]]:
        """Returns sorted list of upcoming obligations with status and historical context."""
        self.reconcile_with_transactions()
        now = datetime.now()
        cutoff = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        current_cycle = now.strftime("%Y-%m")
        next_cycle = (now.replace(day=28) + timedelta(days=4)).strftime("%Y-%m")

        rules = self.list_rules(active_only=True)
        obligations = []

        for rule in rules:
            for cycle in [current_cycle, next_cycle]:
                instance = self.ensure_bill_instance(rule.id, cycle)
                if instance.due_date <= cutoff:
                    # Calculate historical average for context
                    if rule.rule_type == RuleType.VARIABLE:
                        hist_cents, _ = self.compute_variable_estimate(rule.id, cycle)
                    else:
                        hist_cents = rule.base_amount_cents

                    obligations.append({
                        "id": instance.id,
                        "rule_id": rule.id,
                        "name": rule.name,
                        "due_date": instance.due_date,
                        "amount_cents": instance.effective_amount_cents,
                        "hist_avg_cents": hist_cents,
                        "status": instance.status.value,
                        "rule_type": rule.rule_type.value,
                        "is_autopay": rule.is_autopay,
                        "from_account": rule.from_account,
                        "to_account": rule.to_account,
                    })

        obligations.sort(key=lambda x: x["due_date"])
        return obligations

    def get_committed_cash_flow(self, days_ahead: int = 30) -> Dict[str, Any]:
        """Calculates deterministic liquid capital, committed upcoming bills, and Safe-to-Spend buffer."""
        balances = self.store.get_all_balances()
        liquid_cents = balances["liquid_assets_cents"]

        upcoming = self.get_upcoming_obligations(days_ahead=days_ahead)
        # Sum only unpaid bills
        committed_cents = sum(
            item["amount_cents"]
            for item in upcoming
            if item["status"] in (BillStatus.SCHEDULED.value, BillStatus.BILLED.value)
        )
        safe_to_spend_cents = max(0, liquid_cents - committed_cents)

        return {
            "as_of_date": datetime.now().strftime("%Y-%m-%d"),
            "days_ahead": days_ahead,
            "liquid_capital_cents": liquid_cents,
            "committed_cents": committed_cents,
            "safe_to_spend_cents": safe_to_spend_cents,
            "upcoming_bills_count": len(upcoming),
            "pending_bills_count": sum(1 for b in upcoming if b["status"] != "paid"),
            "obligations": upcoming,
        }
