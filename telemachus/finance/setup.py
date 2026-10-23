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

  # Planned budget for gas/fuel for the month
  gas_planned: 150.00

  # PG&E baseline estimate for the month (electric/gas)
  pge_estimate: 75.00

  # Optional notes for this month
  notes: "Initial budget configured via setup.yaml"
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
    recurring = RecurringEngine(store)

    as_of_date = str(data.get("as_of_date") or time.strftime("%Y-%m-%d"))
    summary: Dict[str, Any] = {
        "balances_set": 0,
        "rules_updated": 0,
        "monthly_overview_updated": False,
        "month": "",
        "fixed_bills_total_cents": 0,
    }

    # 1. Process Starting Balances
    starting_balances = data.get("starting_balances", {})
    account_map = {
        "wells_fargo_checking": "Assets:Checking:WellsFargo",
        "capital_one_savings": "Assets:Savings:CapitalOne",
        "visa_credit_card": "Liabilities:CreditCard:Visa",
    }

    for key, val in starting_balances.items():
        if val is None:
            continue
        cents = int(round(float(val) * 100))
        acc_id = account_map.get(key, key)
        
        # Verify account exists
        acc = store.get_account(acc_id)
        if not acc:
            logger.warning(f"Skipping unknown account in setup.yaml: {acc_id}")
            continue

        # Look for existing opening balance transaction to ensure idempotency
        existing_txs = store.list_transactions(limit=100)
        existing_opening_tx = None
        for tx in existing_txs:
            if tx.payee == "Opening Balance" and tx.source == "setup":
                for p in tx.postings:
                    if p.account_id == acc_id:
                        existing_opening_tx = tx
                        break
            if existing_opening_tx:
                break

        if existing_opening_tx:
            store.delete_transaction(existing_opening_tx.id)

        # Build balanced transaction
        # For Assets: Debit Asset (+), Credit Equity:OpeningBalances (-)
        # For Liabilities: Credit Liability (-), Debit Equity:OpeningBalances (+)
        if acc.account_type == AccountType.ASSET:
            postings = [
                Posting(account_id=acc_id, amount_cents=cents, memo="Starting opening balance"),
                Posting(account_id="Equity:OpeningBalances", amount_cents=-cents, memo="Equity offset"),
            ]
        elif acc.account_type == AccountType.LIABILITY:
            postings = [
                Posting(account_id=acc_id, amount_cents=-cents, memo="Starting card balance"),
                Posting(account_id="Equity:OpeningBalances", amount_cents=cents, memo="Equity offset"),
            ]
        else:
            postings = [
                Posting(account_id=acc_id, amount_cents=cents, memo="Opening balance"),
                Posting(account_id="Equity:OpeningBalances", amount_cents=-cents, memo="Equity offset"),
            ]

        tx = Transaction.create(
            date=as_of_date,
            payee="Opening Balance",
            postings=postings,
            memo=f"Initial setup opening balance for {acc_id}",
            source="setup",
        )
        store.log_transaction(tx)
        summary["balances_set"] += 1

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
        amount = cfg.get("amount", 0.0)
        due_day = int(cfg.get("due_day", 1))
        is_active = bool(cfg.get("active", True))
        amount_cents = int(round(float(amount) * 100))

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
        income_cents = int(round(float(monthly_targets.get("income", 0.0)) * 100))
        savings_target_cents = int(round(float(monthly_targets.get("savings_target", 0.0)) * 100))
        groceries_planned_cents = int(round(float(monthly_targets.get("groceries_planned", 0.0)) * 100))
        gas_planned_cents = int(round(float(monthly_targets.get("gas_planned", 0.0)) * 100))
        pge_cents = int(round(float(monthly_targets.get("pge_estimate", 0.0)) * 100))
        notes = str(monthly_targets.get("notes") or "Configured via setup.yaml")

        store.set_monthly_overview(
            month=month,
            income_cents=income_cents,
            savings_target_cents=savings_target_cents,
            groceries_planned_cents=groceries_planned_cents,
            gas_planned_cents=gas_planned_cents,
            fixed_bills_cents=total_fixed_cents,
            pge_cents=pge_cents,
            status="open",
            notes=notes,
        )
        summary["monthly_overview_updated"] = True
        summary["month"] = month

    # 4. Auto-update Beancount ledger
    try:
        exporter = FinanceExporter(store)
        exporter.export_to_beancount()
    except Exception as e:
        logger.debug(f"Beancount export skipped: {e}")

    return summary
