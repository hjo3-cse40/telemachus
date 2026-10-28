"""Synthetic demo data for the Finances tab.

Builds a fully fake finance vault (accounts, bills, paycheck plan, ~6 weeks of
spending) so the cockpit can be developed or screenshotted without touching
real personal data. Everything is dated relative to today so the runway and
bill views always look "live". Output is deterministic for a given date.

Safety: refuses to write into the real finance vault.
"""
from __future__ import annotations

from datetime import date, timedelta
import os
from pathlib import Path
import random
from typing import Any, Dict, List, Optional

from telemachus.finance.import_text import import_text_document
from telemachus.finance.setup import apply_setup_yaml
from telemachus.finance.store import FinanceStore

HISTORY_DAYS = 45
PAY_INTERVAL_DAYS = 14
PAYCHECK_DOLLARS = 1480.00
DEMO_SEED = 7

# (due_day, payee, dollars) paid on their due day when it falls inside the history window.
DEMO_BILLS = [(1, "gym", 25.00), (11, "icloud", 0.99), (25, "spotify", 11.99), (15, "wifi", 40.00)]

# Weighted spending menu: (payee, low, high, weight)
DEMO_SPEND = [
    ("groceries", 18.0, 72.0, 3),
    ("coffee", 4.5, 7.5, 4),
    ("lunch", 9.0, 17.0, 4),
    ("dinner out", 22.0, 48.0, 2),
    ("gas", 28.0, 46.0, 1),
    ("boba", 5.5, 8.0, 2),
]

SETUP_YAML = """# Synthetic demo data. Not real. Safe to share.
as_of_date: "{as_of}"
starting_balances:
  wells_fargo_checking: 1850.00
  capital_one_savings: 4200.00
  visa_credit_card: 0.00
fixed_bills:
  kahram:
    amount: 0.00
    due_day: 1
    active: false
  gym:
    amount: 25.00
    due_day: 1
    active: true
  apple_icloud:
    amount: 0.99
    due_day: 11
    active: true
  spotify:
    amount: 11.99
    due_day: 25
    active: true
  wifi:
    amount: 40.00
    due_day: 15
    active: true
  rent:
    amount: 0.00
    due_day: 1
    active: false
monthly_targets:
  month: "{month}"
  income: 2960.00
  savings_target: 600.00
  groceries_planned: 280.00
  dining_planned: 120.00
  gas_planned: 120.00
  pge_estimate: 60.00
  notes: "Synthetic demo vault"
paycheck_plan:
  paycheck_amount: {paycheck:.2f}
  savings_per_paycheck: 200.00
  frequency: "biweekly"
  anchor_date: "{anchor}"
"""


class UnsafeDemoTargetError(RuntimeError):
    """Raised when the demo seeder is pointed at something that could hold real data."""


def real_vault_path() -> Path:
    return (Path.home() / ".memory" / "private" / "finances").resolve()


def _check_target(target: Path, force: bool) -> None:
    resolved = target.resolve()
    real = real_vault_path()
    env_dir = os.environ.get("TELEMACHUS_FINANCE_DIR")
    protected = {real}
    if env_dir:
        protected.add(Path(env_dir).expanduser().resolve())
    # Any location inside (or equal to) a protected vault is off limits, even with --force.
    for p in protected:
        if resolved == p or p in resolved.parents or resolved in p.parents:
            raise UnsafeDemoTargetError(
                f"Refusing to write demo data to {resolved}: it overlaps a real finance vault ({p})."
            )
    if (resolved / "finances.sqlite3").exists() and not force:
        raise FileExistsError(
            f"{resolved} already has a finance database. Re-run with --force to replace it (demo folders only)."
        )


def _last_payday(today: date) -> date:
    """Most recent payday: a few days ago, so the cycle is mid-way and some bills are already paid."""
    return today - timedelta(days=5)


def build_demo_text(today: date, rng: random.Random) -> str:
    """Daily bullet notes in the importer's format, oldest day first."""
    start = today - timedelta(days=HISTORY_DAYS)
    anchor = _last_payday(today)
    lines: List[str] = []
    day = start + timedelta(days=1)
    while day <= today:
        entries: List[str] = []
        if (anchor - day).days % PAY_INTERVAL_DAYS == 0:
            entries.append(f"+ {PAYCHECK_DOLLARS:.2f} payday")
        for due_day, payee, amount in DEMO_BILLS:
            if day.day == due_day:
                entries.append(f"- {payee} - {amount:.2f}")
        menu = [m for m in DEMO_SPEND for _ in range(m[3])]
        for _ in range(rng.choice([0, 1, 1, 2, 2, 3])):
            payee, low, high, _w = rng.choice(menu)
            entries.append(f"- {payee} - {rng.uniform(low, high):.2f}")
        if entries:
            lines.append(day.isoformat())
            lines.extend(entries)
            lines.append("")
        day += timedelta(days=1)
    return "\n".join(lines)


def seed_demo(target: Path | str, today: Optional[date] = None, force: bool = False) -> Dict[str, Any]:
    """Creates a synthetic finance vault at `target`. Returns a small summary dict."""
    target = Path(target).expanduser()
    _check_target(target, force)
    today = today or date.today()

    target.mkdir(parents=True, exist_ok=True)
    if force:
        for name in ("finances.sqlite3", "finances.sqlite3-wal", "finances.sqlite3-shm"):
            (target / name).unlink(missing_ok=True)

    start = today - timedelta(days=HISTORY_DAYS)
    yaml_path = target / "setup.yaml"
    yaml_path.write_text(
        SETUP_YAML.format(
            as_of=start.isoformat(),
            month=today.strftime("%Y-%m"),
            paycheck=PAYCHECK_DOLLARS,
            anchor=_last_payday(today).isoformat(),
        ),
        encoding="utf-8",
    )

    store = FinanceStore(root_dir=target)
    apply_setup_yaml(yaml_path=yaml_path, store=store)
    text = build_demo_text(today, random.Random(DEMO_SEED))
    result = import_text_document(text, store=store, default_year=today.year)
    return {
        "path": str(target.resolve()),
        "transactions": result["transactions_logged"],
        "days": result["days_count"],
        "as_of": start.isoformat(),
    }
