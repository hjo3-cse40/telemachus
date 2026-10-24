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
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid

from telemachus.finance.models import (
    DEFAULT_USER_RECURRING_RULES,
    BillInstance,
    BillStatus,
    Frequency,
    PaycheckPlan,
    PaycheckWindowAllocation,
    RecurringRule,
    RuleType,
    Transaction,
)
from telemachus.finance.store import FinanceStore


class RecurringEngine:
    """Manages recurring bills and deterministic forward cash flow commitments."""

    def __init__(self, store: FinanceStore, yaml_path: Optional[Path | str] = None):
        self.store = store
        self.yaml_path = Path(yaml_path).expanduser().resolve() if yaml_path else None
        self._seed_default_rules()

    def _seed_default_rules(self) -> None:
        """Seeds default user recurring rules using INSERT OR IGNORE."""
        with self.store._write_lock():
            with self.store._get_connection() as conn:
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

    # ==========================================================================
    # Paycheck Cash Flow Partitioning
    # ==========================================================================
    def _is_bill_paid_in_month(self, account_id: str, month: str) -> bool:
        """Checks if any payment transaction was logged for this account in the specified month."""
        start_date = f"{month}-01"
        import calendar
        y, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(y, m)
        end_date = f"{month}-{last_day:02d}"

        with self.store._get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) as cnt
                FROM postings p
                JOIN transactions t ON p.transaction_id = t.id
                WHERE p.account_id = ? AND t.date >= ? AND t.date <= ? AND p.amount_cents > 0;
                """,
                (account_id, start_date, end_date),
            ).fetchone()
            return int(row["cnt"]) > 0 if row else False

    def get_paycheck_allocation(
        self, as_of_date: Optional[str] = None, yaml_path: Optional[Path | str] = None
    ) -> PaycheckWindowAllocation:
        """Calculates deterministic cash flow partition for the 2-week window starting from as_of_date.
        
        Evaluates exact calendar due dates within the 14-day window against setup.yaml:
        1. Net Take-Home Paycheck Inflow
        2. Immediate HYSA Savings Transfer (off the top)
        3. Bills due specifically within this 14-day window (full amounts, credit card balance check, already_paid deduction)
        4. 2-Week Variable Allowance (50% of monthly groceries, dining & gas)
        5. Safe-to-Spend Discretionary Remainder
        """
        now_str = as_of_date or datetime.now().strftime("%Y-%m-%d")
        now_dt = datetime.strptime(now_str, "%Y-%m-%d")

        # 1. Load setup.yaml live as primary source of truth if available
        setup_data = None
        target_yaml = yaml_path or self.yaml_path or self.store.get_setup_yaml_path()
        if target_yaml:
            try:
                from telemachus.finance.setup import load_setup_yaml, _parse_cents
                setup_data = load_setup_yaml(target_yaml)
            except Exception:
                pass
        if "_parse_cents" not in locals():
            _parse_cents = lambda x: int(round(float(x) * 100)) if x is not None else None

        # 2. Retrieve or derive PaycheckPlan
        plan = None
        if setup_data and setup_data.get("paycheck_plan"):
            cfg = setup_data["paycheck_plan"]
            amt = _parse_cents(cfg.get("paycheck_amount"))
            sav = _parse_cents(cfg.get("savings_per_paycheck"))
            if amt and sav is not None:
                plan = PaycheckPlan(
                    paycheck_amount_cents=amt,
                    savings_per_paycheck_cents=sav,
                    frequency=str(cfg.get("frequency", "biweekly")).lower(),
                    anchor_date=str(cfg.get("anchor_date", now_str)),
                )

        if not plan:
            plan = self.store.get_paycheck_plan()

        if not plan:
            current_month = now_dt.strftime("%Y-%m")
            overview = self.store.get_monthly_overview(current_month)
            derived_income = round(overview.income_cents / 2) if overview.income_cents > 0 else 135000
            derived_savings = round(overview.savings_target_cents / 2) if overview.savings_target_cents > 0 else 50000
            plan = PaycheckPlan(
                paycheck_amount_cents=derived_income,
                savings_per_paycheck_cents=derived_savings,
                frequency="biweekly",
                anchor_date=now_str,
            )

        # 3. 14-Day Evaluation Window: starting from now_dt
        days_in_cycle = 14
        start_dt = now_dt
        end_dt = start_dt + timedelta(days=days_in_cycle - 1)
        paycheck_date_str = start_dt.strftime("%Y-%m-%d")
        end_window_str = end_dt.strftime("%Y-%m-%d")
        next_paycheck_str = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d")

        # 4. Map of active bills from setup.yaml (or database fallback)
        configured_bills = {}
        if setup_data and setup_data.get("fixed_bills"):
            fb = setup_data["fixed_bills"]
            for k, v in fb.items():
                if isinstance(v, dict) and v.get("active", True):
                    configured_bills[k] = v

        # Default standard bill metadata
        STD_BILL_ACCOUNTS = {
            "kahram": ("Expenses:Family:Kahram", "Brother Payment (Kahram)", 1),
            "brother": ("Expenses:Family:Kahram", "Brother Payment", 1),
            "gym": ("Expenses:Subscriptions:Gym", "Gym Membership", 1),
            "wifi": ("Expenses:Utilities:WiFi", "WiFi / Internet", 28),
            "pge": ("Expenses:Utilities:PGE", "PG&E Electric & Gas", 28),
            "apple_icloud": ("Expenses:Subscriptions:AppleCloud", "Apple iCloud", 11),
            "spotify": ("Expenses:Subscriptions:Spotify", "Spotify", 25),
            "rent": ("Expenses:Home:Rent", "Apartment Rent", 1),
            "credit_card": ("Liabilities:CreditCard:Visa", "Credit Card Payment (Visa)", 2),
        }

        # Build list of active bill specs
        bill_specs = []
        seen_keys = set()
        for k, v in configured_bills.items():
            clean_k = k.lower().replace("-", "_").replace(" ", "_")
            seen_keys.add(clean_k)
            acc, default_name, default_day = STD_BILL_ACCOUNTS.get(clean_k, (f"Expenses:{k.title()}", k.title(), 1))
            due_day = int(v.get("due_day", default_day))
            amt = _parse_cents(v.get("amount", 0.0)) or 0
            already_paid = _parse_cents(v.get("already_paid"))
            bill_specs.append({
                "key": clean_k,
                "name": v.get("name") or default_name,
                "account_id": acc,
                "due_day": due_day,
                "amount_cents": amt,
                "already_paid_cents": already_paid,
                "is_active": bool(v.get("active", True)),
            })

        # Ensure Credit Card is evaluated
        if "credit_card" not in seen_keys:
            bill_specs.append({
                "key": "credit_card",
                "name": "Credit Card Payment (Visa)",
                "account_id": "Liabilities:CreditCard:Visa",
                "due_day": 2,
                "amount_cents": 0,
                "already_paid_cents": None,
                "is_active": True,
            })

        # Ensure PG&E is evaluated from monthly_targets if not in fixed_bills
        if "pge" not in seen_keys:
            pge_est = 7500
            if setup_data and setup_data.get("monthly_targets"):
                pge_val = _parse_cents(setup_data["monthly_targets"].get("pge_estimate"))
                if pge_val:
                    pge_est = pge_val
            bill_specs.append({
                "key": "pge",
                "name": "PG&E Electric & Gas",
                "account_id": "Expenses:Utilities:PGE",
                "due_day": 28,
                "amount_cents": pge_est,
                "already_paid_cents": None,
                "is_active": True,
            })

        # 5. Smart Due-Date Evaluation across the 14 days
        window_bills = []
        other_bills = []
        handled_keys_in_window = set()

        for day_offset in range(days_in_cycle):
            check_date = start_dt + timedelta(days=day_offset)
            d_str = check_date.strftime("%Y-%m-%d")
            m_str = check_date.strftime("%Y-%m")
            d_day = check_date.day

            for spec in bill_specs:
                if not spec["is_active"]:
                    continue
                if spec["due_day"] == d_day:
                    key = spec["key"]
                    handled_keys_in_window.add(key)

                    # A. Brother Payment (Kahram)
                    if key in ("kahram", "brother"):
                        target_cents = spec["amount_cents"] or 15000
                        # Check goals section for overrides
                        if setup_data and setup_data.get("goals"):
                            bro_goal = (setup_data["goals"].get("brother") or setup_data["goals"].get("kahram") or {})
                            if isinstance(bro_goal, dict):
                                gt = _parse_cents(bro_goal.get("target") or bro_goal.get("amount") or bro_goal.get("goal"))
                                if gt is not None:
                                    target_cents = gt
                                if spec["already_paid_cents"] is None:
                                    gp = _parse_cents(
                                        bro_goal.get("already_paid")
                                        or bro_goal.get("paid")
                                        or bro_goal.get("current")
                                        or bro_goal.get("actual")
                                    )
                                    if gp is not None:
                                        spec["already_paid_cents"] = gp

                        baseline = spec.get("already_paid_cents")
                        if baseline is not None:
                            total_paid = baseline
                        else:
                            # Calculate actual payments in this month from database
                            with self.store._get_connection() as conn:
                                k_row = conn.execute(
                                    """
                                    SELECT COALESCE(SUM(p.amount_cents), 0) as total
                                    FROM postings p
                                    JOIN transactions t ON p.transaction_id = t.id
                                    WHERE p.account_id IN ('Expenses:Family:Kahram', 'Expenses:Family:Brother')
                                      AND t.date >= ? AND t.date <= ?;
                                    """,
                                    (f"{m_str}-01", f"{m_str}-31"),
                                ).fetchone()
                                actual_paid_month = int(k_row["total"]) if k_row else 0
                            total_paid = actual_paid_month

                        rem = max(0, target_cents - total_paid)

                        if rem == 0:
                            status_str = "paid"
                            amt_due = 0
                            note_str = f"Already paid in full (${total_paid / 100:.2f} / ${target_cents / 100:.2f}) - No payment needed"
                        else:
                            status_str = "due"
                            amt_due = rem
                            note_str = f"${rem / 100:.2f} remaining due (${total_paid / 100:.2f} paid of ${target_cents / 100:.2f})"

                        window_bills.append({
                            "name": spec["name"],
                            "due_date": d_str,
                            "amount_cents": amt_due,
                            "status": status_str,
                            "note": note_str,
                            "notes": note_str,
                            "account_id": spec["account_id"],
                        })

                    # B. Credit Card Payment (Visa)
                    elif key == "credit_card":
                        card_bal = self.store.get_account_balance("Liabilities:CreditCard:Visa")
                        if card_bal <= 0:
                            status_str = "paid"
                            amt_due = 0
                            note_str = "Paid in full ($0.00 balance due)"
                        else:
                            status_str = "due"
                            amt_due = card_bal
                            note_str = f"${card_bal / 100:.2f} current balance due"

                        window_bills.append({
                            "name": spec["name"],
                            "due_date": d_str,
                            "amount_cents": amt_due,
                            "status": status_str,
                            "note": note_str,
                            "notes": note_str,
                            "account_id": spec["account_id"],
                        })

                    # C. Standard Fixed Bills (WiFi, PG&E, Gym, Rent, Subscriptions)
                    else:
                        is_paid = self._is_bill_paid_in_month(spec["account_id"], m_str)
                        if is_paid:
                            status_str = "paid"
                            amt_due = 0
                            note_str = "Already paid for this cycle"
                        else:
                            status_str = "due"
                            amt_due = spec["amount_cents"]
                            note_str = "Due in this window"

                        window_bills.append({
                            "name": spec["name"],
                            "due_date": d_str,
                            "amount_cents": amt_due,
                            "status": status_str,
                            "note": note_str,
                            "notes": note_str,
                            "account_id": spec["account_id"],
                        })

        # Collect bills outside this 14-day window for clear context
        for spec in bill_specs:
            if not spec["is_active"] or spec["key"] in handled_keys_in_window or spec["key"] == "credit_card":
                continue
            out_note = f"Due on day {spec['due_day']} (outside this 2-week window)"
            other_bills.append({
                "name": spec["name"],
                "due_day": spec["due_day"],
                "amount_cents": spec["amount_cents"],
                "status": "outside_window",
                "note": out_note,
                "notes": out_note,
            })

        # 6. Calculate Totals
        bills_due_cents = sum(b["amount_cents"] for b in window_bills if b.get("status") == "due")

        # 7. Allocate 2-week variable needs (Groceries, Dining, Gas)
        month_str = start_dt.strftime("%Y-%m")
        overview = self.store.get_monthly_overview(month_str)
        groceries_planned = overview.groceries_planned_cents
        dining_planned = overview.dining_planned_cents
        gas_planned = overview.gas_planned_cents

        if setup_data and setup_data.get("monthly_targets"):
            mt = setup_data["monthly_targets"]
            g = _parse_cents(mt.get("groceries_planned"))
            if g is not None:
                groceries_planned = g
            d = _parse_cents(mt.get("dining_planned"))
            if d is not None:
                dining_planned = d
            f = _parse_cents(mt.get("gas_planned"))
            if f is not None:
                gas_planned = f

        groceries_alloc = round(groceries_planned / 2)
        dining_alloc = round(dining_planned / 2)
        gas_alloc = round(gas_planned / 2)
        variable_needs_cents = groceries_alloc + dining_alloc + gas_alloc

        # 8. Compute Net Checking and Safe-to-Spend
        net_checking_cents = max(0, plan.paycheck_amount_cents - plan.savings_per_paycheck_cents)
        safe_to_spend_cents = max(0, net_checking_cents - bills_due_cents - variable_needs_cents)

        return PaycheckWindowAllocation(
            paycheck_date=paycheck_date_str,
            next_paycheck_date=next_paycheck_str,
            days_in_cycle=days_in_cycle,
            paycheck_amount_cents=plan.paycheck_amount_cents,
            savings_transfer_cents=plan.savings_per_paycheck_cents,
            net_checking_cents=net_checking_cents,
            bills_due_cents=bills_due_cents,
            bills=window_bills,
            other_bills=other_bills,
            groceries_allocated_cents=groceries_alloc,
            dining_allocated_cents=dining_alloc,
            gas_allocated_cents=gas_alloc,
            variable_needs_cents=variable_needs_cents,
            safe_to_spend_cents=safe_to_spend_cents,
            notes=f"2-Week allocation from {paycheck_date_str} to {end_window_str}",
        )

    def get_all_recurring_bills_breakdown(
        self, month: Optional[str] = None, yaml_path: Optional[Path | str] = None
    ) -> Dict[str, Any]:
        """Returns comprehensive breakdown of recurring bills sorted deterministically by due date (1-31).
        
        Matches Notion '1) Recurring Fixed Bills' table with live status (Paid vs Due)
        and '2) Monthly Overview' Notion budget formulas.
        """
        if not month:
            month = datetime.now().strftime("%Y-%m")
        start_date = f"{month}-01"
        import calendar
        y, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(y, m)
        end_date = f"{month}-{last_day:02d}"

        # 1. Load setup.yaml live
        target_yaml = yaml_path or self.yaml_path or self.store.get_setup_yaml_path()
        setup_data = None
        if target_yaml:
            try:
                from telemachus.finance.setup import load_setup_yaml, _parse_cents
                setup_data = load_setup_yaml(target_yaml)
            except Exception:
                pass
        if "_parse_cents" not in locals():
            _parse_cents = lambda x: int(round(float(x) * 100)) if x is not None else None

        # 2. Extract configured bills from setup.yaml (or database fallback)
        configured_bills = {}
        if setup_data and setup_data.get("fixed_bills"):
            fb = setup_data["fixed_bills"]
            for k, v in fb.items():
                if isinstance(v, dict):
                    configured_bills[k] = v

        STD_BILL_METADATA = {
            "kahram": ("Expenses:Family:Kahram", "Kahram payment", "Fixed Bill", 1),
            "brother": ("Expenses:Family:Kahram", "Brother Payment", "Fixed Bill", 1),
            "gym": ("Expenses:Subscriptions:Gym", "Gym Membership", "Fixed Bill", 1),
            "apple_icloud": ("Expenses:Subscriptions:AppleCloud", "Apple iCloud", "Subscription", 11),
            "spotify": ("Expenses:Subscriptions:Spotify", "Spotify", "Subscription", 25),
            "wifi": ("Expenses:Utilities:WiFi", "Wi-Fi", "Fixed Bill", 28),
            "pge": ("Expenses:Utilities:PGE", "PG&E Electric & Gas", "Utility (Variable)", 28),
            "credit_card": ("Liabilities:CreditCard:Visa", "Credit Card (Visa)", "Card Balance", 2),
            "rent": ("Expenses:Home:Rent", "Apartment Rent", "Fixed Bill", 1),
        }

        bills: List[Dict[str, Any]] = []
        seen_keys = set()

        for k, v in configured_bills.items():
            clean_k = k.lower().replace("-", "_").replace(" ", "_")
            seen_keys.add(clean_k)
            acc, def_name, def_cat, def_day = STD_BILL_METADATA.get(
                clean_k, (f"Expenses:{k.title()}", k.title(), "Fixed Bill", 1)
            )
            due_day = int(v.get("due_day", def_day))
            amt = _parse_cents(v.get("amount", 0.0)) or 0
            category = v.get("category") or def_cat
            name = v.get("name") or def_name
            is_active = bool(v.get("active", True))

            # Status determination for month
            notes = v.get("notes") or ""
            status = "due"
            status_text = "⏳ Due"

            if clean_k in ("kahram", "brother"):
                # Check already_paid from setup
                already_paid = _parse_cents(v.get("already_paid"))
                if already_paid is None and setup_data and setup_data.get("goals"):
                    bg = setup_data["goals"].get("brother") or setup_data["goals"].get("kahram") or {}
                    if isinstance(bg, dict):
                        already_paid = _parse_cents(bg.get("already_paid") or bg.get("paid"))
                if already_paid is not None:
                    paid_tot = already_paid
                else:
                    with self.store._get_connection() as conn:
                        k_row = conn.execute(
                            """
                            SELECT COALESCE(SUM(p.amount_cents), 0) as total
                            FROM postings p
                            JOIN transactions t ON p.transaction_id = t.id
                            WHERE p.account_id IN ('Expenses:Family:Kahram', 'Expenses:Family:Brother')
                              AND t.date >= ? AND t.date <= ?;
                            """,
                            (start_date, end_date),
                        ).fetchone()
                        paid_tot = int(k_row["total"]) if k_row else 0
                if paid_tot >= amt and amt > 0:
                    status = "paid"
                    status_text = "✅ Paid"
                elif paid_tot > 0:
                    status = "due"
                    status_text = f"⏳ Rem: ${(amt - paid_tot)/100:.2f}"
            else:
                is_paid = self._is_bill_paid_in_month(acc, month)
                if is_paid:
                    status = "paid"
                    status_text = "✅ Paid"

            bills.append({
                "key": clean_k,
                "name": name,
                "category": category,
                "due_day": due_day,
                "amount_cents": amt,
                "status": status,
                "status_text": status_text,
                "account_id": acc,
                "is_active": is_active,
                "notes": notes,
                "is_variable": False,
            })

        # Add PG&E variable bill if not explicitly added
        if "pge" not in seen_keys:
            pge_est = 7500
            if setup_data and setup_data.get("monthly_targets"):
                p = _parse_cents(setup_data["monthly_targets"].get("pge_estimate"))
                if p:
                    pge_est = p
            pge_paid = self._is_bill_paid_in_month("Expenses:Utilities:PGE", month)
            bills.append({
                "key": "pge",
                "name": "PG&E Electric & Gas",
                "category": "Utility (Variable)",
                "due_day": 28,
                "amount_cents": pge_est,
                "status": "paid" if pge_paid else "due",
                "status_text": "✅ Paid" if pge_paid else "⏳ Due",
                "account_id": "Expenses:Utilities:PGE",
                "is_active": True,
                "notes": "Estimated monthly baseline",
                "is_variable": True,
            })

        # Add Credit Card if not explicitly added
        if "credit_card" not in seen_keys:
            card_bal = self.store.get_account_balance("Liabilities:CreditCard:Visa")
            cc_status = "paid" if card_bal <= 0 else "due"
            cc_text = "✅ $0" if card_bal <= 0 else f"⏳ Bal: ${card_bal/100:.2f}"
            bills.append({
                "key": "credit_card",
                "name": "Credit Card (Visa)",
                "category": "Card Balance",
                "due_day": 2,
                "amount_cents": max(0, card_bal),
                "status": cc_status,
                "status_text": cc_text,
                "account_id": "Liabilities:CreditCard:Visa",
                "is_active": True,
                "notes": "Due on 2nd of month",
                "is_variable": False,
            })

        # Sort bills strictly by due_day (1 to 31)
        bills.sort(key=lambda b: (b["due_day"], b["name"]))

        # Fixed Bills SUM: sum of active fixed bills (excluding variable PG&E and credit card)
        fixed_sum_cents = sum(
            b["amount_cents"]
            for b in bills
            if b["is_active"] and not b["is_variable"] and b["key"] != "credit_card"
        )

        overview = self.store.get_monthly_overview(month)

        return {
            "month": month,
            "bills": bills,
            "fixed_bills_total_cents": fixed_sum_cents,
            "overview": overview,
        }

