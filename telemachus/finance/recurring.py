"""Recurring Obligations & Bill Management Engine for Telemachus.

Handles:
- Fixed recurring subscriptions and obligations (Internet, Brother, Gym, Spotify, Apple Cloud, Rent).
- Variable recurring bills (PG&E) using rolling statistical averages and seasonal buffers.
- 3-step lifecycle: SCHEDULED (estimated) -> BILLED (statement arrived) -> PAID (matched transaction).
- Committed cash flow and Safe-to-Spend runway calculations.

Credit card rule (until-payday math is checking-based cash):
- A card purchase does not lower checking, so it does not change checking-based numbers on the day
  it happens. It raises the Visa balance owed.
- The whole balance owed on the card is treated as due on the card's next due day (setup.yaml
  fixed_bills credit_card/visa due_day, default the 2nd). Statement closing dates are not tracked,
  so this is conservative: charges made after a statement closes are counted a month early.
- When that due day falls before the next payday, the amount owed is subtracted like a bill.
- A card payment lowers checking AND the amount owed by the same amount, so it is never
  subtracted twice. A partial payment leaves the rest due; a full payoff marks the bill paid;
  an overpayment leaves a credit (negative owed), which counts as $0 due, never as extra cash.
- The card bill is counted at most once per window, even when the window spans two due days.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta
import math
from pathlib import Path
import re
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
    is_variable_bill,
)
# parse_due_day lives in cards (shared with the card summary); kept importable from here.
from telemachus.finance.cards import CARD_BILL_KEYS, VISA, parse_due_day  # noqa: F401
from telemachus.finance.store import FinanceStore

RENT_ACCOUNT = "Expenses:Housing:Rent"


def bill_key(key: Any) -> str:
    """setup.yaml fixed_bills key in one spelling: "Car Insurance" and "car-insurance" are car_insurance."""
    return str(key).strip().lower().replace("-", "_").replace(" ", "_")


def custom_bill_account(key: Any) -> str:
    """Expense account of a fixed bill with no built-in account: phone -> Expenses:Phone,
    car_insurance -> Expenses:CarInsurance (the same name in setup, the bill engine and the cockpit)."""
    parts = [p for p in bill_key(key).split("_") if p]
    return "Expenses:" + ("".join(p[:1].upper() + p[1:] for p in parts) or "Bill")


# Words a line that pays a setup.yaml bill may have besides the bill's own name ("phone bill",
# "pay phone bill oct", "phone autopay").
_BILL_LINE_WORDS = {
    "bill", "bills", "payment", "payments", "pay", "paid", "pmt", "autopay", "autopaid", "auto",
    "monthly", "month", "this", "the", "my", "for", "of", "due", "statement", "invoice",
    "jan", "january", "feb", "february", "mar", "march", "apr", "april", "may", "jun", "june",
    "jul", "july", "aug", "august", "sep", "sept", "september", "oct", "october", "nov",
    "november", "dec", "december",
}

ESTIMATE_NOTE = "Estimate from past bills; the real amount is only known when the statement arrives"


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
    def get_upcoming_obligations(
        self, days_ahead: int = 30, yaml_path: Optional[Path | str] = None
    ) -> List[Dict[str, Any]]:
        """Returns sorted list of upcoming obligations strictly within [today, today + days_ahead].
        
        Reads setup.yaml live as primary source of truth, matching exact due dates and amounts.
        """
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d")
        cutoff = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

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

        if setup_data and setup_data.get("fixed_bills") and isinstance(setup_data.get("fixed_bills"), dict):
            configured_bills = setup_data["fixed_bills"]
            STD_BILL_METADATA = {
                "kahram": ("Expenses:Family:Kahram", "Brother Payment", "Fixed Bill", 1),
                "brother": ("Expenses:Family:Kahram", "Brother Payment", "Fixed Bill", 1),
                "gym": ("Expenses:Subscriptions:Gym", "Gym Membership", "Fixed Bill", 1),
                "apple_icloud": ("Expenses:Subscriptions:AppleCloud", "Apple iCloud", "Subscription", 11),
                "spotify": ("Expenses:Subscriptions:Spotify", "Spotify", "Subscription", 25),
                "wifi": ("Expenses:Utilities:WiFi", "Wi-Fi", "Fixed Bill", 28),
                "pge": ("Expenses:Utilities:PGE", "PG&E Electric & Gas", "Utility (Variable)", 28),
                "credit_card": ("Liabilities:CreditCard:Visa", "Credit Card (Visa)", "Card Balance", 2),
                "visa": ("Liabilities:CreditCard:Visa", "Credit Card (Visa)", "Card Balance", 2),
                "visa_credit_card": ("Liabilities:CreditCard:Visa", "Credit Card (Visa)", "Card Balance", 2),
                "credit_card_payment": ("Liabilities:CreditCard:Visa", "Credit Card (Visa)", "Card Balance", 2),
                "rent": (RENT_ACCOUNT, "Apartment Rent", "Fixed Bill", 1),
            }

            bill_specs = []
            seen_keys = set()
            for k, v in configured_bills.items():
                if not isinstance(v, dict) or not bool(v.get("active", True)):
                    continue
                clean_k = bill_key(k)
                seen_keys.add(clean_k)
                acc, def_name, def_cat, def_day = STD_BILL_METADATA.get(
                    clean_k, (custom_bill_account(k), str(k).replace("_", " ").title(), "Fixed Bill", 1)
                )
                due_day = parse_due_day(v.get("due_day"), def_day)
                amt = _parse_cents(v.get("amount", 0.0)) or 0
                is_estimate = is_variable_bill(clean_k, v)
                if is_estimate and not amt:
                    amt = _parse_cents((setup_data.get("monthly_targets") or {}).get("pge_estimate")) or 0
                bill_specs.append({
                    "id": f"bill_{clean_k}",
                    "name": v.get("name") or def_name,
                    "account_id": acc,
                    "due_day": due_day,
                    "amount_cents": amt,
                    "is_active": True,
                    "is_estimate": is_estimate,
                    "category": v.get("category") or def_cat,
                })

            pge_est = _parse_cents((setup_data.get("monthly_targets") or {}).get("pge_estimate")) or 0
            if "pge" not in seen_keys and pge_est > 0:
                bill_specs.append({
                    "id": "bill_pge",
                    "name": "PG&E Electric & Gas",
                    "account_id": "Expenses:Utilities:PGE",
                    "due_day": 28,
                    "amount_cents": pge_est,
                    "is_active": True,
                    "is_estimate": True,
                    "category": "Utility (Variable)",
                })

            # Any card key in setup.yaml (credit_card, visa, ...) is the same live-balance card bill.
            for spec in bill_specs:
                if spec["id"].replace("bill_", "", 1) in CARD_BILL_KEYS:
                    spec["id"] = "bill_credit_card"
                    spec["account_id"] = VISA
            from telemachus.finance.cards import card_bill_switched_off

            if not any(k in seen_keys for k in CARD_BILL_KEYS) and not card_bill_switched_off(setup_data):
                card_bal = max(0, self.store.get_account_balance(VISA))
                bill_specs.append({
                    "id": "bill_credit_card",
                    "name": "Credit Card (Visa)",
                    "account_id": "Liabilities:CreditCard:Visa",
                    "due_day": 2,
                    "amount_cents": card_bal,
                    "is_active": True,
                    "category": "Card Balance",
                })

            obligations = []
            card_counted = False
            for day_offset in range(days_ahead):
                check_date = now + timedelta(days=day_offset)
                d_str = check_date.strftime("%Y-%m-%d")
                m_str = check_date.strftime("%Y-%m")
                d_day = check_date.day

                for spec in bill_specs:
                    if self._effective_due_day(spec["due_day"], check_date) == d_day:
                        if spec["id"] == "bill_credit_card":
                            # Whole balance owed is due on the first due day ahead; later due
                            # days in the range show $0 so it is never counted twice.
                            card_bal = self.store.get_account_balance(VISA)
                            if card_counted:
                                card_bal = 0
                            status = "paid" if card_bal <= 0 else "due"
                            amt = max(0, card_bal)
                            card_counted = True
                        elif spec["name"] == "Brother Payment" or "brother" in spec["id"]:
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
                                paid_this_month = int(k_row["total"]) if k_row else 0
                            amt = max(0, spec["amount_cents"] - paid_this_month)
                            status = "paid" if amt == 0 else "due"
                        else:
                            is_paid = self._is_bill_paid_for_due_date(spec["account_id"], d_str)
                            status = "paid" if is_paid else "due"
                            amt = spec["amount_cents"] if not is_paid else 0

                        obligations.append({
                            "id": f"{spec['id']}_{d_str}",
                            "rule_id": spec["id"],
                            "name": spec["name"],
                            "due_date": d_str,
                            "amount_cents": amt,
                            "hist_avg_cents": spec["amount_cents"],
                            "is_estimate": bool(spec.get("is_estimate")),
                            "status": status,
                            "rule_type": "variable" if spec.get("is_estimate") else "fixed",
                            "is_autopay": True,
                            "from_account": "Assets:Checking:WellsFargo",
                            "to_account": spec["account_id"],
                        })

            obligations.sort(key=lambda x: x["due_date"])
            return obligations

        # Fallback to database rules (ensuring only future dates are returned)
        self.reconcile_with_transactions()
        current_cycle = now.strftime("%Y-%m")
        next_cycle = (now.replace(day=28) + timedelta(days=4)).strftime("%Y-%m")

        rules = self.list_rules(active_only=True)
        obligations = []

        for rule in rules:
            for cycle in [current_cycle, next_cycle]:
                instance = self.ensure_bill_instance(rule.id, cycle)
                if now_str <= instance.due_date <= cutoff:
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
                        "is_estimate": (
                            rule.rule_type == RuleType.VARIABLE
                            and instance.actual_amount_cents is None
                        ),
                        "status": instance.status.value,
                        "rule_type": rule.rule_type.value,
                        "is_autopay": rule.is_autopay,
                        "from_account": rule.from_account,
                        "to_account": rule.to_account,
                    })

        obligations.sort(key=lambda x: x["due_date"])
        return obligations

    def get_committed_cash_flow(self, days_ahead: int = 30) -> Dict[str, Any]:
        """Calculates deterministic checking cash flow, committed upcoming bills, and projected remainder.
        
        Strictly uses Wells Fargo Checking account (excluding Capital One Savings per user instruction).
        """
        chk_cents = self.store.get_account_balance("Assets:Checking:WellsFargo")
        upcoming = self.get_upcoming_obligations(days_ahead=days_ahead)
        # Sum only unpaid bills
        committed_cents = sum(
            item["amount_cents"]
            for item in upcoming
            if item["status"] in (BillStatus.SCHEDULED.value, BillStatus.BILLED.value, "due")
        )
        checking_after_bills_cents = chk_cents - committed_cents
        safe_to_spend_cents = max(0, checking_after_bills_cents)

        return {
            "as_of_date": datetime.now().strftime("%Y-%m-%d"),
            "days_ahead": days_ahead,
            "checking_balance_cents": chk_cents,
            "liquid_capital_cents": chk_cents,  # Exclude savings: cash position is checking only
            "committed_cents": committed_cents,
            "checking_after_bills_cents": checking_after_bills_cents,
            "safe_to_spend_cents": safe_to_spend_cents,
            "upcoming_bills_count": len(upcoming),
            "pending_bills_count": sum(1 for b in upcoming if b["status"] != "paid"),
            "obligations": upcoming,
        }

    # ==========================================================================
    # Paycheck Cash Flow Partitioning
    # ==========================================================================
    @staticmethod
    def _effective_due_day(due_day: int, on_date: datetime) -> int:
        """Clamps a due day to the month's length (day 30 -> Feb 28/29)."""
        return min(due_day, calendar.monthrange(on_date.year, on_date.month)[1])

    @staticmethod
    def _due_date_for_month(due_day: int, month: str) -> str:
        y, m = map(int, month.split("-"))
        day = min(due_day, calendar.monthrange(y, m)[1])
        return f"{y:04d}-{m:02d}-{day:02d}"

    def _is_bill_paid_for_due_date(self, account_id: str, due_date: str, as_of: Optional[str] = None) -> bool:
        """True if a payment was logged within ~half a month of this due date.

        as_of: payments dated later do not count yet (checking does not show them yet either).

        Bills are often paid at the turn of the month (e.g. the 1st) for the
        previous month's statement, so a calendar-month match wrongly credits
        that payment to the new month. Windows centred on each due date tile
        the calendar without overlapping.
        """
        due = datetime.strptime(due_date, "%Y-%m-%d")
        start_date = (due - timedelta(days=15)).strftime("%Y-%m-%d")
        end_date = (due + timedelta(days=14)).strftime("%Y-%m-%d")
        if as_of and as_of < end_date:
            end_date = as_of
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

    def _paid_amount_for_due_date(self, account_id: str, due_date: str, as_of: Optional[str] = None) -> int:
        """Amount of the payment logged closest to this due date (same window as _is_bill_paid_for_due_date)."""
        due = datetime.strptime(due_date, "%Y-%m-%d")
        start_date = (due - timedelta(days=15)).strftime("%Y-%m-%d")
        end_date = (due + timedelta(days=14)).strftime("%Y-%m-%d")
        if as_of and as_of < end_date:
            end_date = as_of
        with self.store._get_connection() as conn:
            row = conn.execute(
                """
                SELECT p.amount_cents AS amount
                FROM postings p
                JOIN transactions t ON p.transaction_id = t.id
                WHERE p.account_id = ? AND t.date >= ? AND t.date <= ? AND p.amount_cents > 0
                ORDER BY ABS(julianday(t.date) - julianday(?)) ASC
                LIMIT 1;
                """,
                (account_id, start_date, end_date, due_date),
            ).fetchone()
            return int(row["amount"]) if row else 0

    @staticmethod
    def _previous_paycheck_date(plan: PaycheckPlan, today: datetime) -> datetime:
        """Most recent payday on or before `today` (the start of the current pay cycle)."""
        if plan.frequency == "semimonthly":
            return today.replace(day=15) if today.day >= 15 else today.replace(day=1)
        return RecurringEngine._next_paycheck_date(plan, today) - timedelta(days=14)

    def _is_bill_paid_in_month(self, account_id: str, month: str) -> bool:
        """Checks if any payment transaction was logged for this account in the specified month."""
        start_date = f"{month}-01"
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

    @staticmethod
    def _next_paycheck_date(plan: PaycheckPlan, today: datetime) -> datetime:
        """First payday strictly after `today`, derived from the plan's anchor and frequency.

        On a payday itself, today's paycheck has already landed, so the cycle
        runs to the following payday.
        """
        if plan.frequency == "semimonthly":
            for day in (1, 15):
                candidate = today.replace(day=day)
                if candidate > today:
                    return candidate
            first_next = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
            return first_next
        try:
            anchor = datetime.strptime(plan.anchor_date, "%Y-%m-%d")
        except ValueError:
            return today + timedelta(days=14)
        elapsed = (today - anchor).days
        periods = elapsed // 14 + 1
        return anchor + timedelta(days=14 * periods)

    def card_owed_as_of(self, as_of_date: str) -> int:
        """Visa balance owed on `as_of_date`: entries dated later do not count yet.

        Setup opening balances always count (they may be dated on the day setup ran).
        """
        return self.store.get_account_balance(VISA, as_of_date=as_of_date, setup_always=True)

    def card_overdue_since(self, as_of_date: str, due_day: Optional[int]) -> Optional[str]:
        """The card due date (YYYY-MM-DD) that passed unpaid, or None.

        Overdue when all hold: money is owed today, money was already owed on the last due date
        before today (a setup balance dated after that day does not count, it may be new
        charges), and no card payment is logged after the due date before it, up to today.
        Without this, an unpaid balance would drop out of the bills the day after its due date.
        """
        from telemachus.finance.cards import previous_due_date

        if not due_day:
            return None
        today = datetime.strptime(as_of_date, "%Y-%m-%d").date()
        if self.card_owed_as_of(as_of_date) <= 0:
            return None
        last_due = previous_due_date(due_day, today)
        if self.store.get_account_balance(VISA, as_of_date=last_due.isoformat()) <= 0:
            return None
        prev_due = previous_due_date(due_day, last_due)
        with self.store._get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM postings p
                JOIN transactions t ON p.transaction_id = t.id
                WHERE p.account_id = ? AND p.amount_cents > 0
                  AND (t.source IS NULL OR t.source != 'setup')
                  AND t.date > ? AND t.date <= ?;
                """,
                (VISA, prev_due.isoformat(), as_of_date),
            ).fetchone()
        if row and int(row["cnt"]) > 0:
            return None
        return last_due.isoformat()

    # Built-in bill accounts (the bullet parser already has a category for each of these).
    _BUILTIN_BILL_ACCOUNTS = {
        "kahram": "Expenses:Family:Kahram", "brother": "Expenses:Family:Kahram",
        "gym": "Expenses:Subscriptions:Gym", "wifi": "Expenses:Utilities:WiFi",
        "pge": "Expenses:Utilities:PGE", "apple_icloud": "Expenses:Subscriptions:AppleCloud",
        "spotify": "Expenses:Subscriptions:Spotify", "rent": RENT_ACCOUNT,
    }

    def bill_accounts(self) -> Dict[str, str]:
        """{account id: bill name} of every active fixed bill in setup.yaml (the card excluded)."""
        target_yaml = self.yaml_path or self.store.get_setup_yaml_path()
        if not target_yaml:
            return {}
        try:
            from telemachus.finance.setup import load_setup_yaml
            data = load_setup_yaml(target_yaml) or {}
        except Exception:
            return {}
        fb = data.get("fixed_bills")
        out: Dict[str, str] = {}
        if not isinstance(fb, dict):
            return out
        for k, v in fb.items():
            key = bill_key(k)
            if key in CARD_BILL_KEYS or not isinstance(v, dict) or not bool(v.get("active", True)):
                continue
            acc = self._BUILTIN_BILL_ACCOUNTS.get(key) or custom_bill_account(key)
            out[acc] = str(v.get("name") or key.replace("_", " ").title())
        return out

    def bill_account_for_text(self, text: str) -> Optional[Tuple[str, str]]:
        """(account id, bill name) of the setup.yaml fixed bill that a typed line pays, or None.

        Matches the bill's key ("phone", "car insurance") or its name, as whole words. Built-in
        bills (gym, wifi, rent, ...) are already found by the bullet parser's categories; this
        finds the rest, so "phone bill 80" marks the phone bill paid instead of being counted
        again as a bill still due.

        Every other word must be a bill word ("bill", "payment", "autopay", a month, ...) or a
        word of that bill's name, so a purchase that only mentions the bill ("phone case 20",
        "car wash 15") stays a purchase and never marks the bill paid.
        """
        low = " " + re.sub(r"[-_]+", " ", (text or "").lower()) + " "
        words = set(re.findall(r"[a-z&']+", low))
        target_yaml = self.yaml_path or self.store.get_setup_yaml_path()
        if not target_yaml:
            return None
        try:
            from telemachus.finance.setup import load_setup_yaml
            data = load_setup_yaml(target_yaml) or {}
        except Exception:
            return None
        fb = data.get("fixed_bills")
        if not isinstance(fb, dict):
            return None
        for k, v in fb.items():
            key = bill_key(k)
            if key in CARD_BILL_KEYS or not isinstance(v, dict) or not bool(v.get("active", True)):
                continue
            name = str(v.get("name") or key.replace("_", " ").title())
            phrases = {key.replace("_", " "), re.sub(r"[-_]+", " ", name.lower())}
            allowed = _BILL_LINE_WORDS | set(re.findall(r"[a-z&']+", " ".join(phrases)))
            if not words <= allowed:
                continue
            for ph in phrases:
                ph = re.sub(r"\s+", " ", ph).strip()
                if ph and re.search(rf"(?<![a-z0-9]){re.escape(ph)}(?![a-z0-9])", low):
                    acc = self._BUILTIN_BILL_ACCOUNTS.get(key) or custom_bill_account(key)
                    return acc, name
        return None

    def ensure_bill_account(self, account_id: str) -> None:
        """Adds an Expense account for a custom bill when the chart of accounts lacks it."""
        if self.store.get_account(account_id):
            return
        from telemachus.finance.models import Account, AccountType
        try:
            self.store.add_account(Account(account_id, AccountType.EXPENSE, description="Bill (setup.yaml)"))
        except Exception:
            if not self.store.get_account(account_id):
                raise

    def has_paycheck_plan(self, yaml_path: Optional[Path | str] = None) -> bool:
        """True when payday is known: a paycheck_plan in setup.yaml or a saved plan in the ledger.

        Without one, get_paycheck_allocation guesses (payday 14 days from today), so the cockpit
        hides the until-payday numbers instead of showing made-up ones.
        """
        target_yaml = yaml_path or self.yaml_path or self.store.get_setup_yaml_path()
        if target_yaml:
            try:
                from telemachus.finance.setup import load_setup_yaml, _parse_cents
                data = load_setup_yaml(target_yaml) or {}
                cfg = data.get("paycheck_plan") or {}
                if isinstance(cfg, dict) and _parse_cents(cfg.get("paycheck_amount")):
                    return True
            except Exception:
                pass
        try:
            return self.store.get_paycheck_plan() is not None
        except Exception:
            return False

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
        start_dt = now_dt
        next_paycheck_dt = self._next_paycheck_date(plan, start_dt)
        days_in_cycle = (next_paycheck_dt - start_dt).days
        end_dt = next_paycheck_dt - timedelta(days=1)
        paycheck_date_str = start_dt.strftime("%Y-%m-%d")
        end_window_str = end_dt.strftime("%Y-%m-%d")
        next_paycheck_str = next_paycheck_dt.strftime("%Y-%m-%d")

        # 4. Map of active bills from setup.yaml (or database fallback)
        configured_bills = {}
        if setup_data and setup_data.get("fixed_bills") and isinstance(setup_data.get("fixed_bills"), dict):
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
            "rent": (RENT_ACCOUNT, "Apartment Rent", 1),
            "credit_card": ("Liabilities:CreditCard:Visa", "Credit Card Payment (Visa)", 2),
            "visa": ("Liabilities:CreditCard:Visa", "Credit Card Payment (Visa)", 2),
            "visa_credit_card": ("Liabilities:CreditCard:Visa", "Credit Card Payment (Visa)", 2),
            "credit_card_payment": ("Liabilities:CreditCard:Visa", "Credit Card Payment (Visa)", 2),
        }

        # Build list of active bill specs
        bill_specs = []
        seen_keys = set()
        for k, v in configured_bills.items():
            clean_k = bill_key(k)
            seen_keys.add(clean_k)
            acc, default_name, default_day = STD_BILL_ACCOUNTS.get(clean_k, (custom_bill_account(k), str(k).replace("_", " ").title(), 1))
            due_day = parse_due_day(v.get("due_day"), default_day)
            amt = _parse_cents(v.get("amount", 0.0)) or 0
            is_estimate = is_variable_bill(clean_k, v)
            if is_estimate and not amt:
                amt = _parse_cents((setup_data.get("monthly_targets") or {}).get("pge_estimate")) or 0
            already_paid = _parse_cents(v.get("already_paid"))
            bill_specs.append({
                "key": clean_k,
                "name": v.get("name") or default_name,
                "account_id": acc,
                "due_day": due_day,
                "amount_cents": amt,
                "already_paid_cents": already_paid,
                "is_estimate": is_estimate,
                "is_active": bool(v.get("active", True)),
            })

        # Ensure Credit Card is evaluated (not when setup.yaml switches the card bill off)
        from telemachus.finance.cards import card_bill_switched_off

        has_cc = any(k in seen_keys for k in ("credit_card", "visa", "visa_credit_card", "credit_card_payment"))
        if not has_cc and not card_bill_switched_off(setup_data):
            bill_specs.append({
                "key": "credit_card",
                "name": "Credit Card Payment (Visa)",
                "account_id": "Liabilities:CreditCard:Visa",
                "due_day": 2,
                "amount_cents": 0,
                "already_paid_cents": None,
                "is_active": True,
            })

        # Ensure PG&E is evaluated from monthly_targets if not in fixed_bills (only with a configured
        # estimate: never a made-up amount)
        pge_est = 0
        if setup_data and setup_data.get("monthly_targets"):
            pge_est = _parse_cents(setup_data["monthly_targets"].get("pge_estimate")) or 0
        if "pge" not in seen_keys and pge_est > 0:
            bill_specs.append({
                "key": "pge",
                "name": "PG&E Electric & Gas",
                "account_id": "Expenses:Utilities:PGE",
                "due_day": 28,
                "amount_cents": pge_est,
                "already_paid_cents": None,
                "is_estimate": True,
                "is_active": True,
            })

        # 5. Smart Due-Date Evaluation across the 14 days
        window_bills = []
        other_bills = []
        handled_keys_in_window = set()
        card_counted = False

        # 5a. A card balance whose due date passed unpaid is still owed: it comes out today.
        card_spec = next((sp for sp in bill_specs if sp["is_active"] and sp["key"] in CARD_BILL_KEYS), None)
        overdue_since = self.card_overdue_since(now_str, card_spec["due_day"]) if card_spec else None
        if card_spec and overdue_since:
            card_bal = self.card_owed_as_of(now_str)
            card_counted = True
            handled_keys_in_window.add(card_spec["key"])
            note_str = f"Card payment was due {overdue_since} and no payment is logged: ${card_bal / 100:,.2f} overdue"
            window_bills.append({
                "name": card_spec["name"],
                "due_date": now_str,
                "amount_cents": card_bal,
                "status": "due",
                "overdue_since": overdue_since,
                "note": note_str,
                "notes": note_str,
                "account_id": card_spec["account_id"],
                "is_autopay": True,
            })

        for day_offset in range(days_in_cycle):
            check_date = start_dt + timedelta(days=day_offset)
            d_str = check_date.strftime("%Y-%m-%d")
            m_str = check_date.strftime("%Y-%m")
            d_day = check_date.day

            for spec in bill_specs:
                if not spec["is_active"]:
                    continue
                if self._effective_due_day(spec["due_day"], check_date) == d_day:
                    key = spec["key"]
                    handled_keys_in_window.add(key)

                    # A. Brother Payment (Kahram) - Strictly pull monthly installment amount, not multi-month debt total
                    if key in ("kahram", "brother"):
                        monthly_amount = spec["amount_cents"]
                        if monthly_amount <= 0:
                            continue  # no amount set: nothing to show or subtract

                        # Calculate actual payments in this month from database
                        with self.store._get_connection() as conn:
                            k_row = conn.execute(
                                """
                                SELECT COALESCE(SUM(p.amount_cents), 0) as total
                                FROM postings p
                                JOIN transactions t ON p.transaction_id = t.id
                                WHERE p.account_id IN ('Expenses:Family:Kahram', 'Expenses:Family:Brother')
                                  AND t.date >= ? AND t.date <= ? AND t.date <= ?;
                                """,
                                (f"{m_str}-01", f"{m_str}-31", now_str),
                            ).fetchone()
                            actual_paid_month = int(k_row["total"]) if k_row else 0

                        paid_this_month = actual_paid_month
                        if spec.get("already_paid_cents") is not None:
                            paid_this_month = max(paid_this_month, spec["already_paid_cents"])

                        rem = max(0, monthly_amount - paid_this_month)

                        if rem == 0:
                            status_str = "paid"
                            amt_due = 0
                            note_str = f"Monthly installment paid in full (${paid_this_month / 100:.2f} / ${monthly_amount / 100:.2f})"
                        else:
                            status_str = "due"
                            amt_due = rem
                            note_str = f"${rem / 100:.2f} monthly payment due (${paid_this_month / 100:.2f} paid of ${monthly_amount / 100:.2f})"

                        window_bills.append({
                            "name": spec["name"],
                            "due_date": d_str,
                            "amount_cents": amt_due,
                            "status": status_str,
                            "note": note_str,
                            "notes": note_str,
                            "account_id": spec["account_id"],
                            "is_autopay": False,
                        })

                    # B. Credit Card Payment (Visa) - Always remind about payment date in this window
                    elif key in CARD_BILL_KEYS:
                        # See the module docstring: the whole balance owed is due on this day.
                        card_bal = self.card_owed_as_of(now_str)
                        if card_counted:
                            continue
                        card_counted = True
                        if card_bal < 0:
                            status_str = "paid"
                            amt_due = 0
                            note_str = f"Card payment due {d_str}: nothing owed (${-card_bal / 100:,.2f} credit)"
                        elif card_bal == 0:
                            status_str = "paid"
                            amt_due = 0
                            note_str = f"Card payment due {d_str}: $0.00 current balance due, paid in full"
                        else:
                            status_str = "due"
                            amt_due = card_bal
                            note_str = f"Card payment due {d_str}: ${card_bal / 100:,.2f} current balance due"

                        window_bills.append({
                            "name": spec["name"],
                            "due_date": d_str,
                            "amount_cents": amt_due,
                            "status": status_str,
                            "note": note_str,
                            "notes": note_str,
                            "account_id": spec["account_id"],
                            "is_autopay": True,
                        })

                    # C. Standard Fixed Bills (WiFi, PG&E, Gym, Rent, Subscriptions)
                    else:
                        is_paid = self._is_bill_paid_for_due_date(spec["account_id"], d_str, as_of=now_str)
                        if is_paid:
                            status_str = "paid"
                            amt_due = 0
                            note_str = "Already paid for this cycle"
                        else:
                            status_str = "due"
                            amt_due = spec["amount_cents"]
                            note_str = ESTIMATE_NOTE if spec.get("is_estimate") else "Due in this window"

                        window_bills.append({
                            "name": spec["name"],
                            "due_date": d_str,
                            "amount_cents": amt_due,
                            "is_estimate": bool(spec.get("is_estimate")) and not is_paid,
                            "status": status_str,
                            "note": note_str,
                            "notes": note_str,
                            "account_id": spec["account_id"],
                            "is_autopay": True,
                        })

        # 5b. Look back to the last payday for bills that came due earlier in this cycle and were paid,
        # so they stay visible until the next paycheck. Only per-date-checkable bills are included
        # (not the Kahram monthly installment or the credit card, which have no per-date paid test).
        paid_bills = []
        cycle_start_dt = self._previous_paycheck_date(plan, start_dt)
        for day_offset in range((start_dt - cycle_start_dt).days):
            check_date = cycle_start_dt + timedelta(days=day_offset)
            d_str = check_date.strftime("%Y-%m-%d")
            for spec in bill_specs:
                if not spec["is_active"] or spec["key"] in ("kahram", "brother", "credit_card", "visa", "visa_credit_card", "credit_card_payment"):
                    continue
                if self._effective_due_day(spec["due_day"], check_date) != check_date.day:
                    continue
                if not self._is_bill_paid_for_due_date(spec["account_id"], d_str, as_of=now_str):
                    continue
                paid_amt = self._paid_amount_for_due_date(spec["account_id"], d_str, as_of=now_str)
                paid_bills.append({
                    "name": spec["name"],
                    "due_date": d_str,
                    "amount_cents": paid_amt,
                    "status": "paid",
                    "note": "Paid earlier this cycle",
                    "notes": "Paid earlier this cycle",
                    "account_id": spec["account_id"],
                    "is_autopay": True,
                })

        # Collect bills outside this 14-day window for clear context
        for spec in bill_specs:
            if not spec["is_active"] or spec["key"] in handled_keys_in_window or spec["key"] in CARD_BILL_KEYS:
                continue
            out_note = f"Due on day {spec['due_day']} (outside this 2-week window)"
            other_bills.append({
                "name": spec["name"],
                "due_day": spec["due_day"],
                "amount_cents": spec["amount_cents"],
                "is_estimate": bool(spec.get("is_estimate")),
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

        # 8. Cash Position: Checking account ONLY (exclude Capital One Savings per user direction),
        # as of today: an entry logged ahead (a card payment or a paycheck on a later day) does not
        # count yet, the same way the card bill uses card_owed_as_of(today).
        chk_cents = self.store.get_account_balance(
            "Assets:Checking:WellsFargo", as_of_date=now_str, setup_always=True
        )
        checking_after_bills_cents = chk_cents - bills_due_cents

        # 9. Credit card reminder extraction
        cc_reminder = ""
        for b in window_bills:
            if b.get("account_id") == VISA:
                cc_reminder = f"Payment date is on {b['due_date']} ({b.get('note', '')})"
                break

        # 10. Compute Net Checking and Safe-to-Spend for this cycle
        net_checking_cents = max(0, plan.paycheck_amount_cents - plan.savings_per_paycheck_cents)
        safe_to_spend_cents = max(0, net_checking_cents - bills_due_cents - variable_needs_cents)

        notes_str = f"2-Week allocation from {paycheck_date_str} to {end_window_str}"
        if cc_reminder:
            notes_str += f" │ {cc_reminder}"

        return PaycheckWindowAllocation(
            paycheck_date=paycheck_date_str,
            next_paycheck_date=next_paycheck_str,
            days_in_cycle=days_in_cycle,
            paycheck_amount_cents=plan.paycheck_amount_cents,
            savings_transfer_cents=plan.savings_per_paycheck_cents,
            net_checking_cents=net_checking_cents,
            bills_due_cents=bills_due_cents,
            checking_balance_cents=chk_cents,
            checking_after_bills_cents=checking_after_bills_cents,
            credit_card_reminder=cc_reminder,
            bills=window_bills,
            other_bills=other_bills,
            paid_bills=paid_bills,
            cycle_start_date=cycle_start_dt.strftime("%Y-%m-%d"),
            groceries_allocated_cents=groceries_alloc,
            dining_allocated_cents=dining_alloc,
            gas_allocated_cents=gas_alloc,
            variable_needs_cents=variable_needs_cents,
            safe_to_spend_cents=safe_to_spend_cents,
            notes=notes_str,
        )

    def get_card_summary(
        self, as_of_date: Optional[str] = None, next_paycheck_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """Visa card state for the cockpit (shape in CONTRACT.md section 7).

        owed = live Visa balance (positive owed, negative credit). due_before_payday is True exactly
        when get_paycheck_allocation subtracts the card balance as a bill before the next payday.
        """
        from telemachus.finance.cards import build_card_summary, card_due_day

        today_str = as_of_date or datetime.now().strftime("%Y-%m-%d")
        today = datetime.strptime(today_str, "%Y-%m-%d").date()
        setup_data = None
        target_yaml = self.yaml_path or self.store.get_setup_yaml_path()
        if target_yaml:
            try:
                from telemachus.finance.setup import load_setup_yaml
                setup_data = load_setup_yaml(target_yaml)
            except Exception:
                setup_data = None
        due_day = card_due_day(setup_data)
        # As of `today`: entries dated later do not count yet.
        owed = self.card_owed_as_of(today_str)
        overdue = self.card_overdue_since(today_str, due_day)
        txs = [
            t for t in self.store.list_transactions(account_id=VISA, end_date=today_str, limit=100000)
            if t.source != "setup"
        ]
        next_payday = None
        try:
            if not next_paycheck_date and self.has_paycheck_plan():
                next_paycheck_date = self.get_paycheck_allocation(as_of_date=today_str).next_paycheck_date
            next_payday = datetime.strptime(next_paycheck_date, "%Y-%m-%d").date()
        except Exception:
            next_payday = None
        return build_card_summary(
            txs, owed, today, due_day, next_payday,
            overdue_since=datetime.strptime(overdue, "%Y-%m-%d").date() if overdue else None,
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
        if setup_data and setup_data.get("fixed_bills") and isinstance(setup_data.get("fixed_bills"), dict):
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
            "visa": ("Liabilities:CreditCard:Visa", "Credit Card (Visa)", "Card Balance", 2),
            "visa_credit_card": ("Liabilities:CreditCard:Visa", "Credit Card (Visa)", "Card Balance", 2),
            "credit_card_payment": ("Liabilities:CreditCard:Visa", "Credit Card (Visa)", "Card Balance", 2),
            "rent": (RENT_ACCOUNT, "Apartment Rent", "Fixed Bill", 1),
        }

        bills: List[Dict[str, Any]] = []
        seen_keys = set()

        for k, v in configured_bills.items():
            clean_k = bill_key(k)
            seen_keys.add(clean_k)
            acc, def_name, def_cat, def_day = STD_BILL_METADATA.get(
                clean_k, (custom_bill_account(k), str(k).replace("_", " ").title(), "Fixed Bill", 1)
            )
            due_day = parse_due_day(v.get("due_day"), def_day)
            amt = _parse_cents(v.get("amount", 0.0)) or 0
            category = v.get("category") or def_cat
            name = v.get("name") or def_name
            is_active = bool(v.get("active", True))

            # Status determination for month
            notes = v.get("notes") or ""
            status = "due"
            status_text = "⏳ Due"

            if clean_k in CARD_BILL_KEYS:
                # The card bill is the live balance owed, not a fixed amount (see module docstring).
                acc = VISA
                card_bal = self.card_owed_as_of(datetime.now().strftime("%Y-%m-%d"))
                amt = max(0, card_bal)
                status = "due" if card_bal > 0 else "paid"
                status_text = f"⏳ Bal: ${card_bal/100:,.2f}" if card_bal > 0 else ("✅ $0" if card_bal == 0 else f"✅ Credit ${-card_bal/100:,.2f}")
                clean_k = "credit_card"
            elif clean_k in ("kahram", "brother"):
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
                is_paid = self._is_bill_paid_for_due_date(acc, self._due_date_for_month(due_day, month))
                if is_paid:
                    status = "paid"
                    status_text = "✅ Paid"

            is_variable = is_variable_bill(clean_k, v)
            if is_variable:
                if not amt:
                    amt = _parse_cents((setup_data.get("monthly_targets") or {}).get("pge_estimate")) or 0
                notes = notes or ESTIMATE_NOTE

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
                "is_variable": is_variable,
            })

        # Add PG&E variable bill if not explicitly added (only with a configured estimate)
        pge_est = 0
        if setup_data and setup_data.get("monthly_targets"):
            pge_est = _parse_cents(setup_data["monthly_targets"].get("pge_estimate")) or 0
        if "pge" not in seen_keys and pge_est > 0:
            pge_paid = self._is_bill_paid_for_due_date(
                "Expenses:Utilities:PGE", self._due_date_for_month(28, month)
            )
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
        if not any(k in seen_keys for k in CARD_BILL_KEYS):
            card_bal = self.card_owed_as_of(datetime.now().strftime("%Y-%m-%d"))
            cc_status = "paid" if card_bal <= 0 else "due"
            cc_text = f"⏳ Bal: ${card_bal/100:,.2f}" if card_bal > 0 else ("✅ $0" if card_bal == 0 else f"✅ Credit ${-card_bal/100:,.2f}")
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

        overview = self.store.get_monthly_overview(month, yaml_path=target_yaml)

        return {
            "month": month,
            "bills": bills,
            "fixed_bills_total_cents": fixed_sum_cents,
            "overview": overview,
        }

