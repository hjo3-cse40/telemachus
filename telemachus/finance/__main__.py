"""CLI entry point for Telemachus Financial Operations.

Commands:
  python3 -m telemachus.finance status
  python3 -m telemachus.finance log --payee "Safeway" --amount 45.20 --from Liabilities:CreditCard:Visa --to Expenses:Food:Groceries
  python3 -m telemachus.finance balance [--mask]
  python3 -m telemachus.finance list [--limit 20] [--mask]
  python3 -m telemachus.finance summary [--start YYYY-MM-DD --end YYYY-MM-DD] [--mask]
  python3 -m telemachus.finance bills [--days 30] [--mask]
  python3 -m telemachus.finance export [--csv] [--beancount] [--mask]
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import sys
from typing import Optional

from telemachus.finance.export import FinanceExporter
from telemachus.finance.masking import FinanceMasker
from telemachus.finance.models import Posting, Transaction
from telemachus.finance.recurring import RecurringEngine
from telemachus.finance.store import FinanceStore


def _format_currency(cents: int, masked: bool = False, label: str = "VAL") -> str:
    if masked:
        return f"<{label}>"
    dollars = cents / 100.0
    return f"${dollars:,.2f}"


def cmd_status(store: FinanceStore, recurring: RecurringEngine, args: argparse.Namespace) -> int:
    balances = store.get_all_balances()
    cash_flow = recurring.get_committed_cash_flow(days_ahead=30)
    masked = args.mask

    print("\n🛡️  TELEMACHUS FINANCIAL ENGINE (Phase 1)")
    print(f"Master Database: {store.db_path}")
    print("=" * 60)
    print(f"Liquid Capital:    {_format_currency(cash_flow['liquid_capital_cents'], masked, 'LIQUID_ASSETS')}")
    print(f"Committed (30d):   {_format_currency(cash_flow['committed_cents'], masked, 'COMMITTED_30D')}")
    print(f"Safe-to-Spend:     {_format_currency(cash_flow['safe_to_spend_cents'], masked, 'SAFE_TO_SPEND')}")
    print(f"Pending Bills:     {cash_flow['pending_bills_count']} obligations due in next 30 days")
    print("=" * 60)

    print("\nAccount Balances:")
    for acc_id, bal_cents in balances["account_balances"].items():
        tag = acc_id.split(":")[-1].upper()
        print(f"  {acc_id:<36} {_format_currency(bal_cents, masked, tag):>14}")
    print()
    return 0


def cmd_log(store: FinanceStore, args: argparse.Namespace) -> int:
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    amount_cents = round(args.amount * 100)

    # Double entry:
    # Outflow from source account (Credit -> negative)
    # Inflow to destination account (Debit -> positive)
    postings = [
        Posting(account_id=args.from_account, amount_cents=-amount_cents, memo=args.memo),
        Posting(account_id=args.to_account, amount_cents=amount_cents, memo=args.memo),
    ]

    tx = Transaction.create(
        date=date,
        payee=args.payee,
        postings=postings,
        memo=args.memo or "",
        source="cli",
    )

    logged = store.log_transaction(tx)
    # Auto-update beancount projection
    exporter = FinanceExporter(store)
    exporter.export_to_beancount()

    print(f"✅ Successfully logged transaction [{logged.id[:8]}]:")
    print(f"   Date:   {logged.date}")
    print(f"   Payee:  {logged.payee}")
    print(f"   Amount: ${args.amount:,.2f}")
    print(f"   From:   {args.from_account}")
    print(f"   To:     {args.to_account}")
    if args.memo:
        print(f"   Memo:   {args.memo}")
    return 0


def cmd_list(store: FinanceStore, args: argparse.Namespace) -> int:
    txs = store.list_transactions(limit=args.limit)
    masked = args.mask

    print(f"\n📜 Recent Transactions (Showing top {len(txs)}):")
    print("-" * 75)
    print(f"{'DATE':<12} {'PAYEE':<24} {'CATEGORY':<24} {'AMOUNT':>12}")
    print("-" * 75)

    for tx in txs:
        # Destination expense/asset posting
        dest_p = [p for p in tx.postings if p.amount_cents > 0]
        cat = dest_p[0].account_id if dest_p else tx.postings[0].account_id
        cents = dest_p[0].amount_cents if dest_p else tx.postings[0].amount_cents
        tag = cat.split(":")[-1].upper()
        amt_str = _format_currency(cents, masked, tag)
        print(f"{tx.date:<12} {tx.payee[:22]:<24} {cat[:22]:<24} {amt_str:>12}")
    print("-" * 75)
    return 0


def cmd_balance(store: FinanceStore, args: argparse.Namespace) -> int:
    balances = store.get_all_balances()
    masked = args.mask

    print("\n🏛️  Balance Sheet (Deterministic exact cents):")
    print("=" * 55)
    print(f"{'ACCOUNT':<38} {'BALANCE':>14}")
    print("-" * 55)
    for acc, cents in balances["account_balances"].items():
        tag = acc.split(":")[-1].upper()
        print(f"{acc:<38} {_format_currency(cents, masked, tag):>14}")
    print("=" * 55)
    print(f"{'TOTAL LIQUID ASSETS':<38} {_format_currency(balances['liquid_assets_cents'], masked, 'LIQUID_TOTAL'):>14}")
    print(f"{'TOTAL LIABILITIES':<38} {_format_currency(balances['liabilities_cents'], masked, 'LIABILITIES_TOTAL'):>14}")
    print(f"{'NET WORTH':<38} {_format_currency(balances['net_worth_cents'], masked, 'NET_WORTH'):>14}")
    print("=" * 55 + "\n")
    return 0


def cmd_summary(store: FinanceStore, args: argparse.Namespace) -> int:
    now = datetime.now()
    start = args.start or now.strftime("%Y-%m-01")
    end = args.end or now.strftime("%Y-%m-%d")
    summary = store.get_cash_flow_summary(start, end)
    masked = args.mask

    print(f"\n📊 Cash Flow Summary: {start} to {end} ({summary.evaluated_days} days)")
    print("=" * 55)
    print(f"Total Inflows:    {_format_currency(summary.inflow_cents, masked, 'INFLOWS')}")
    print(f"Total Outflows:   {_format_currency(summary.outflow_cents, masked, 'OUTFLOWS')}")
    print(f"Net Savings:      {_format_currency(summary.net_savings_cents, masked, 'NET_SAVINGS')} ({summary.savings_rate_pct}% savings rate)")
    print(f"Daily Burn Rate:  {_format_currency(summary.daily_burn_rate_cents, masked, 'DAILY_BURN')} / day")
    print("-" * 55)
    print("Category Breakdown:")
    for cat, cents in summary.category_breakdown.items():
        tag = cat.split(":")[-1].upper()
        print(f"  {cat:<36} {_format_currency(cents, masked, tag):>14}")
    print("=" * 55 + "\n")
    return 0


def cmd_bills(recurring: RecurringEngine, args: argparse.Namespace) -> int:
    obligations = recurring.get_upcoming_obligations(days_ahead=args.days)
    masked = args.mask

    print(f"\n📅 Upcoming Obligations Radar (Next {args.days} Days):")
    print("=" * 75)
    print(f"{'DUE DATE':<12} {'OBLIGATION':<26} {'AMOUNT':>12} {'TYPE':<10} {'STATUS':<10}")
    print("-" * 75)
    for b in obligations:
        tag = b["name"].replace(" ", "_").upper()
        amt = _format_currency(b["amount_cents"], masked, tag)
        print(f"{b['due_date']:<12} {b['name'][:24]:<26} {amt:>12} {b['rule_type'].title():<10} [{b['status'].upper():<8}]")
    print("=" * 75 + "\n")
    return 0


def cmd_export(store: FinanceStore, args: argparse.Namespace) -> int:
    exporter = FinanceExporter(store)
    masked = args.mask

    if args.csv:
        path = exporter.export_to_csv(masked=masked)
        print(f"📄 Exported CSV spreadsheet: {path}")

    if args.beancount or not args.csv:
        path = exporter.export_to_beancount(masked=masked)
        print(f"📑 Exported Beancount plain-text ledger: {path}")

    return 0


def main() -> int:
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument("--vault-dir", help="Path to finances vault root (overrides default)")
    common_parser.add_argument("--mask", action="store_true", help="Redact raw dollar amounts using semantic privacy tokens")

    parser = argparse.ArgumentParser(
        description="Telemachus Personal Financial Operations Engine",
        parents=[common_parser],
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = subparsers.add_parser("status", parents=[common_parser], help="View current financial status and cash flow")

    # balance
    p_bal = subparsers.add_parser("balance", parents=[common_parser], help="View current balance sheet down to the cent")

    # list
    p_list = subparsers.add_parser("list", parents=[common_parser], help="List recent transactions")
    p_list.add_argument("--limit", type=int, default=15, help="Number of transactions to display")

    # summary
    p_sum = subparsers.add_parser("summary", parents=[common_parser], help="View income vs expense cash flow summary")
    p_sum.add_argument("--start", help="Start date (YYYY-MM-DD)")
    p_sum.add_argument("--end", help="End date (YYYY-MM-DD)")

    # bills
    p_bills = subparsers.add_parser("bills", parents=[common_parser], help="Inspect upcoming bills radar and PG&E estimates")
    p_bills.add_argument("--days", type=int, default=30, help="Days ahead to evaluate")

    # log
    p_log = subparsers.add_parser("log", parents=[common_parser], help="Log a balanced double-entry transaction")
    p_log.add_argument("--payee", required=True, help="Payee or merchant name")
    p_log.add_argument("--amount", type=float, required=True, help="Amount in dollars (e.g. 45.20)")
    p_log.add_argument("--from", dest="from_account", default="Liabilities:CreditCard:Visa", help="Funding account")
    p_log.add_argument("--to", dest="to_account", default="Expenses:Personal:Discretionary", help="Destination expense or asset")
    p_log.add_argument("--date", help="Transaction date (YYYY-MM-DD, default today)")
    p_log.add_argument("--memo", help="Optional memo or description")

    # export
    p_exp = subparsers.add_parser("export", parents=[common_parser], help="Export to Beancount and CSV formats")
    p_exp.add_argument("--beancount", action="store_true", help="Export to journal.beancount")
    p_exp.add_argument("--csv", action="store_true", help="Export to finances_export.csv")

    args = parser.parse_args()

    store = FinanceStore(root_dir=args.vault_dir)
    recurring = RecurringEngine(store)

    if args.command == "status":
        return cmd_status(store, recurring, args)
    elif args.command == "balance":
        return cmd_balance(store, args)
    elif args.command == "list":
        return cmd_list(store, args)
    elif args.command == "summary":
        return cmd_summary(store, args)
    elif args.command == "bills":
        return cmd_bills(recurring, args)
    elif args.command == "log":
        return cmd_log(store, args)
    elif args.command == "export":
        return cmd_export(store, args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
