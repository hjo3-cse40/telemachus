"""Setup and initialization wizard for Telemachus Finances.

Parses user-edited setup.yaml to initialize opening account balances,
recurring bill schedules, and monthly budget targets in SQLite.
"""
from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Any, Dict, Optional
import yaml

from telemachus.finance.export import FinanceExporter
from telemachus.finance.models import (
    DEFAULT_USER_RECURRING_RULES,
    AccountType,
    Frequency,
    PaycheckPlan,
    Posting,
    RecurringRule,
    RuleType,
    Transaction,
)
from telemachus.finance.recurring import RecurringEngine
from telemachus.finance.store import DEFAULT_FINANCE_ROOT, FinanceStore

logger = logging.getLogger(__name__)

DEFAULT_SETUP_YAML_PATH = DEFAULT_FINANCE_ROOT / "setup.yaml"

SETUP_YAML_TEMPLATE = """# ==============================================================================
# TELEMACHUS FINANCIAL ENGINE - INITIAL SETUP & CONFIGURATION
# Location: ~/.memory/private/finances/setup.yaml
#
# PRIVACY NOTICE:
# This file is strictly private to your machine (chmod 0600).
# Cloud models and AI agents are programmatically forbidden from reading this file.
# After editing your numbers, apply the changes by running:
#   python3 -m telemachus.finance setup
# ==============================================================================

# As-of date for starting balances (YYYY-MM-DD). If omitted, defaults to today.
as_of_date: "2026-10-01"

# ------------------------------------------------------------------------------
# 1. Starting Account Balances
# ------------------------------------------------------------------------------
starting_balances:
  # Primary checking account for income deposits and paying bills
  wells_fargo_checking: 0.00      # Account: Assets:Checking:WellsFargo

  # High Yield Savings Account (Capital One)
  capital_one_savings: 0.00       # Account: Assets:Savings:CapitalOne

  # Primary credit card current balance (enter amount currently owed, e.g. 250.00)
  visa_credit_card: 0.00          # Account: Liabilities:CreditCard:Visa

# ------------------------------------------------------------------------------
# 2. Monthly Fixed Recurring Bills
# ------------------------------------------------------------------------------
fixed_bills:
  # Monthly payment to Kahram (due on the 1st)
  kahram:
    amount: 150.00
    due_day: 1
    active: true

  # Gym membership (due on the 1st)
  gym:
    amount: 60.00
    due_day: 1
    active: true

  # Apple iCloud storage subscription (due on the 11th)
  apple_icloud:
    amount: 2.99
    due_day: 11
    active: true

  # Spotify music subscription (due on the 25th)
  spotify:
    amount: 11.99
    due_day: 25
    active: true

  # Home Wi-Fi internet bill (due on the 15th)
  wifi:
    amount: 35.73
    due_day: 15
    active: true

  # Apartment Rent (currently $0 / paid by dad until year ends; active: false)
  rent:
    amount: 0.00
    due_day: 1
    active: false

# ------------------------------------------------------------------------------
# 3. Monthly Budget & Cash Flow Targets
# ------------------------------------------------------------------------------
monthly_targets:
  # Target month to configure (e.g. 2026-10). If omitted, applies to current month.
  month: "2026-10"

  # Expected monthly net take-home income
  income: 2700.00

  # Target amount to transfer/save to Capital One HYSA each month
  savings_target: 1000.00

  # Planned budget for groceries for the month
  groceries_planned: 300.00

  # Planned budget for dining out / restaurants (optional, default 0.00)
  dining_planned: 100.00

  # Planned budget for gas/fuel for the month
  gas_planned: 150.00

  # PG&E baseline estimate for the month (electric/gas)
  pge_estimate: 75.00

  # Optional notes for this month
  notes: "Initial budget configured via setup.yaml"

# ------------------------------------------------------------------------------
# 4. Paycheck Allocation & Timing (Bi-Weekly Cash Flow)
# ------------------------------------------------------------------------------
paycheck_plan:
  # Net take-home amount received every two weeks (e.g. 1350.00)
  paycheck_amount: 1350.00

  # Consistent amount moved straight to Capital One HYSA upon paycheck deposit
  savings_per_paycheck: 500.00

  # Pay schedule frequency: "biweekly" (every 14 days) or "semimonthly" (1st & 15th)
  frequency: "biweekly"

  # Reference date of your most recent or upcoming paycheck (YYYY-MM-DD)
  anchor_date: "2026-10-15"
"""


def create_setup_yaml_template(target_path: Optional[Path | str] = None) -> Path:
    """Creates initial setup.yaml template with 0600 permissions if not present."""
    path = Path(target_path).expanduser().resolve() if target_path else DEFAULT_SETUP_YAML_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass

    if not path.exists():
        path.write_text(SETUP_YAML_TEMPLATE, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        logger.info(f"Created template at {path}")
    return path


def _parse_cents(val: Any) -> Optional[int]:
    """Robustly parses numeric amounts (float, int, or string like '$1,250.00') into integer cents."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(round(float(val) * 100))
    s = str(val).strip().replace("$", "").replace(",", "")
    try:
        return int(round(float(s) * 100))
    except (ValueError, TypeError):
        return None


def _resolve_account_id(key: str, store: FinanceStore) -> Optional[str]:
    """Resolves arbitrary user key from setup.yaml to a valid double-entry account ID."""
    # 1. Exact match with an existing account
    acc = store.get_account(key)
    if acc:
        return acc.id

    # 2. Case-insensitive exact match
    accounts = store.list_accounts()
    for a in accounts:
        if a.id.lower() == key.lower():
            return a.id

    # 3. Normalized alias matching
    clean = key.lower().replace("-", "_").replace(" ", "_")

    # Checking aliases
    if any(k in clean for k in ("checking", "wells", "wf", "primary_checking")):
        return "Assets:Checking:WellsFargo"

    # Savings / HYSA aliases
    if any(k in clean for k in ("savings", "capital_one", "capitalone", "hysa")):
        return "Assets:Savings:CapitalOne"

    # Credit card / Visa aliases
    if any(k in clean for k in ("visa", "credit", "card", "cc")):
        return "Liabilities:CreditCard:Visa"

    # 4. Partial match against existing account IDs
    for a in accounts:
        if clean in a.id.lower().replace(":", "_"):
            return a.id

    return None


def load_setup_yaml(yaml_path: Optional[Path | str] = None) -> Optional[Dict[str, Any]]:
    """Safely loads configuration data directly from setup.yaml if available."""
    path = Path(yaml_path).expanduser().resolve() if yaml_path else DEFAULT_SETUP_YAML_PATH
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.debug(f"Failed to load setup.yaml from {path}: {e}")
        return None


def apply_setup_yaml(yaml_path: Optional[Path | str] = None, store: Optional[FinanceStore] = None) -> Dict[str, Any]:
    """Applies configuration from setup.yaml to SQLite store and recurring engine.
    
    This function processes:
    1. Starting balances -> opening balance double-entry transactions (idempotent).
    2. Fixed recurring bills -> updates recurring_rules table base amounts and due dates.
    3. Monthly targets -> updates monthly_overview row for current/specified month.
    """
    path = Path(yaml_path).expanduser().resolve() if yaml_path else DEFAULT_SETUP_YAML_PATH
    if not path.exists():
        raise FileNotFoundError(f"Setup file not found at {path}. Run 'python3 -m telemachus.finance setup --create' first.")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    store = store or FinanceStore()
    store.setup_yaml_path = path
    recurring = RecurringEngine(store, yaml_path=path)

    as_of_date = str(data.get("as_of_date") or time.strftime("%Y-%m-%d"))
    summary: Dict[str, Any] = {
        "balances_set": 0,
        "balances_details": [],
        "rules_updated": 0,
        "monthly_overview_updated": False,
        "month": "",
        "fixed_bills_total_cents": 0,
    }

    # 1. Process Starting Balances
    starting_balances = (
        data.get("starting_balances")
        or data.get("starting_balance")
        or data.get("balances")
        or data.get("accounts")
        or {}
    )

    for key, raw_val in starting_balances.items():
        if raw_val is None:
            continue
        cents = _parse_cents(raw_val)
        if cents is None:
            logger.warning(f"Could not parse numeric amount for {key}: {raw_val}")
            continue

        acc_id = _resolve_account_id(key, store)
        if not acc_id:
            logger.warning(f"Skipping unknown account in setup.yaml: {key}")
            continue

        acc = store.get_account(acc_id)
        if not acc:
            logger.warning(f"Skipping unknown account in setup.yaml: {acc_id}")
            continue

        # Delete ANY existing opening balance transactions for this account to guarantee idempotency
        with store._write_lock():
            with store._get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT DISTINCT t.id FROM transactions t
                    JOIN postings p ON t.id = p.transaction_id
                    WHERE t.payee = 'Opening Balance' AND t.source = 'setup' AND p.account_id = ?;
                    """,
                    (acc_id,),
                ).fetchall()
                for r in rows:
                    conn.execute("DELETE FROM transactions WHERE id = ?;", (r["id"],))
                conn.commit()

        # Check for historical transactions prior to or on as_of_date to avoid double-counting
        with store._get_connection() as conn:
            hist_row = conn.execute(
                """
                SELECT COALESCE(SUM(p.amount_cents), 0) as hist_cents, MIN(t.date) as min_date
                FROM postings p
                JOIN transactions t ON p.transaction_id = t.id
                WHERE p.account_id = ? AND t.source != 'setup' AND t.date <= ?;
                """,
                (acc_id, as_of_date),
            ).fetchone()
            hist_cents = int(hist_row["hist_cents"]) if hist_row else 0
            min_date = hist_row["min_date"] if hist_row and hist_row["min_date"] else None

        tx_date = as_of_date
        if min_date and min_date < as_of_date:
            tx_date = min_date
            adjusted_asset_cents = cents - hist_cents
            adjusted_liab_cents = -cents - hist_cents
        else:
            adjusted_asset_cents = cents
            adjusted_liab_cents = -cents

        # Build balanced transaction
        if acc.account_type == AccountType.ASSET:
            postings = [
                Posting(account_id=acc_id, amount_cents=adjusted_asset_cents, memo="Starting opening balance"),
                Posting(account_id="Equity:OpeningBalances", amount_cents=-adjusted_asset_cents, memo="Equity offset"),
            ]
        elif acc.account_type == AccountType.LIABILITY:
            postings = [
                Posting(account_id=acc_id, amount_cents=adjusted_liab_cents, memo="Starting card balance"),
                Posting(account_id="Equity:OpeningBalances", amount_cents=-adjusted_liab_cents, memo="Equity offset"),
            ]
        else:
            postings = [
                Posting(account_id=acc_id, amount_cents=adjusted_asset_cents, memo="Starting balance"),
                Posting(account_id="Equity:OpeningBalances", amount_cents=-adjusted_asset_cents, memo="Equity offset"),
            ]

        tx = Transaction.create(
            date=tx_date,
            payee="Opening Balance",
            postings=postings,
            memo=f"Initial setup opening balance for {acc_id}",
            source="setup",
        )
        store.log_transaction(tx)
        summary["balances_set"] += 1
        summary["balances_details"].append({"account": acc_id, "amount_cents": cents})
    # 2. Process Fixed Recurring Bills
    fixed_bills = data.get("fixed_bills", {})
    rule_map = {
        "kahram": "rule_kahram",
        "gym": "rule_gym",
        "apple_icloud": "rule_apple_cloud",
        "spotify": "rule_spotify",
        "wifi": "rule_wifi",
        "rent": "rule_rent",
    }

    total_fixed_cents = 0

    for key, cfg in fixed_bills.items():
        if not isinstance(cfg, dict):
            continue
        rule_id = rule_map.get(key, f"rule_{key}")
        due_day = int(cfg.get("due_day", 1))
        is_active = bool(cfg.get("active", True))
        amount_cents = _parse_cents(cfg.get("amount", 0.0)) or 0

        existing_rule = recurring.get_rule(rule_id)
        if existing_rule:
            updated_rule = RecurringRule(
                id=existing_rule.id,
                name=existing_rule.name,
                rule_type=existing_rule.rule_type,
                frequency=existing_rule.frequency,
                due_day_of_month=due_day,
                base_amount_cents=amount_cents,
                from_account=existing_rule.from_account,
                to_account=existing_rule.to_account,
                is_autopay=existing_rule.is_autopay,
                lead_time_days=existing_rule.lead_time_days,
                is_active=is_active,
                notes=existing_rule.notes,
            )
            recurring.add_rule(updated_rule)
            summary["rules_updated"] += 1
            if is_active:
                total_fixed_cents += amount_cents

    summary["fixed_bills_total_cents"] = total_fixed_cents

    # 3. Process Monthly Targets
    monthly_targets = data.get("monthly_targets", {})
    if monthly_targets:
        month = str(monthly_targets.get("month") or time.strftime("%Y-%m"))
        income_cents = _parse_cents(monthly_targets.get("income", 0.0)) or 0
        savings_target_cents = _parse_cents(monthly_targets.get("savings_target", 0.0)) or 0
        groceries_planned_cents = _parse_cents(monthly_targets.get("groceries_planned", 0.0)) or 0
        dining_planned_cents = _parse_cents(monthly_targets.get("dining_planned", 0.0)) or 0
        gas_planned_cents = _parse_cents(monthly_targets.get("gas_planned", 0.0)) or 0
        pge_cents = _parse_cents(monthly_targets.get("pge_estimate", 0.0)) or 0
        notes = str(monthly_targets.get("notes") or "Configured via setup.yaml")

        store.set_monthly_overview(
            month=month,
            income_cents=income_cents,
            savings_target_cents=savings_target_cents,
            groceries_planned_cents=groceries_planned_cents,
            dining_planned_cents=dining_planned_cents,
            gas_planned_cents=gas_planned_cents,
            fixed_bills_cents=total_fixed_cents,
            pge_cents=pge_cents,
            status="open",
            notes=notes,
        )
        summary["monthly_overview_updated"] = True
        summary["month"] = month

    # 4. Process Paycheck Plan (if provided)
    paycheck_cfg = data.get("paycheck_plan")
    if paycheck_cfg and isinstance(paycheck_cfg, dict):
        p_amount = _parse_cents(paycheck_cfg.get("paycheck_amount", 0.0)) or 0
        p_savings = _parse_cents(paycheck_cfg.get("savings_per_paycheck", 0.0)) or 0
        p_freq = str(paycheck_cfg.get("frequency", "biweekly")).lower().strip()
        p_anchor = str(paycheck_cfg.get("anchor_date") or time.strftime("%Y-%m-%d"))

        plan = PaycheckPlan(
            paycheck_amount_cents=p_amount,
            savings_per_paycheck_cents=p_savings,
            frequency=p_freq,
            anchor_date=p_anchor,
        )
        store.set_paycheck_plan(plan)
        summary["paycheck_plan_configured"] = True

    # 5. Auto-update Beancount ledger
    try:
        exporter = FinanceExporter(store)
        exporter.export_to_beancount()
    except Exception as e:
        logger.debug(f"Beancount export skipped: {e}")

    return summary
