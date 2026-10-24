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
from pathlib import Path
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


def cmd_setup(store: FinanceStore, args: argparse.Namespace) -> int:
    from telemachus.finance.setup import (
        DEFAULT_SETUP_YAML_PATH,
        apply_setup_yaml,
        create_setup_yaml_template,
    )

    if getattr(args, "create_template", False):
        path = create_setup_yaml_template(args.file)
        print(f"📄 Setup template ready at: {path}")
        print("   Edit this file with your numbers, then run: python3 -m telemachus.finance setup")
        return 0

    path = args.file or DEFAULT_SETUP_YAML_PATH
    if not Path(path).exists():
        path = create_setup_yaml_template(path)
        print(f"📄 Generated setup template at: {path}")
        print("   Please edit this template with your numbers, then re-run: python3 -m telemachus.finance setup")
        return 0

    try:
        summary = apply_setup_yaml(yaml_path=path, store=store)
        print("\n✅ Successfully applied financial configuration:")
        print(f"   • Starting balances initialized: {summary['balances_set']}")
        if summary.get("balances_details"):
            configured_accs = ", ".join(b["account"].split(":")[-1] for b in summary["balances_details"])
            print(f"     Accounts configured:           {configured_accs}")
        print(f"   • Recurring rules updated:       {summary['rules_updated']}")
        if summary.get("monthly_overview_updated"):
            fixed_dollars = summary['fixed_bills_total_cents'] / 100.0
            print(f"   • Monthly overview configured:   {summary['month']} (Fixed bills total: ${fixed_dollars:,.2f})")
        if summary.get("paycheck_plan_configured"):
            print("   • Paycheck plan configured:      Bi-weekly allocation & HYSA rules active")
        print("\n💡 Run 'python3 -m telemachus.finance status' or 'python3 -m telemachus.finance paycheck' to inspect.")
        return 0
    except Exception as e:
        print(f"❌ Error applying setup configuration: {e}")
        return 1


def cmd_import_text(store: FinanceStore, args: argparse.Namespace) -> int:
    from telemachus.finance.import_text import import_text_document

    default_year = getattr(args, "year", None) or datetime.now().year

    if args.file:
        p = Path(args.file).expanduser().resolve()
        if not p.exists():
            print(f"❌ File not found: {p}")
            return 1
        if p.suffix.lower() == ".rtf":
            import subprocess
            try:
                proc = subprocess.run(
                    ["textutil", "-convert", "txt", str(p), "-stdout"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                text = proc.stdout
            except Exception:
                text = p.read_text(encoding="utf-8", errors="ignore")
        else:
            text = p.read_text(encoding="utf-8", errors="ignore")
    else:
        print("📥 Reading text from stdin (paste text and press Ctrl+D when done)...")
        text = sys.stdin.read()

    res = import_text_document(text, store=store, default_year=default_year)
    print("\n✅ Successfully imported text document:")
    print(f"   • Transactions logged: {res['transactions_logged']}")
    print(f"   • Days covered:        {res['days_count']} (From {res['dates'][0] if res['dates'] else 'N/A'} to {res['dates'][-1] if res['dates'] else 'N/A'})")
    print("\n💡 Run 'python3 -m telemachus.finance plan' or open Cockpit Tab [4: Finances] to inspect.")
    return 0


def cmd_clear_day(store: FinanceStore, args: argparse.Namespace) -> int:
    target_date = args.date or datetime.now().strftime("%Y-%m-%d")
    count = store.delete_day_transactions(target_date)
    exporter = FinanceExporter(store)
    exporter.export_to_beancount()
    print(f"\n🗑️  Successfully deleted {count} transaction(s) for {target_date} (opening balances preserved).")
    return 0


def cmd_clear_imported(store: FinanceStore, args: argparse.Namespace) -> int:
    count = store.delete_all_imported_transactions()
    exporter = FinanceExporter(store)
    exporter.export_to_beancount()
    print(f"\n🗑️  Successfully deleted {count} transaction(s). Opening balances and setup configuration preserved.")
    return 0


def cmd_delete_tx(store: FinanceStore, args: argparse.Namespace) -> int:
    success = store.delete_transaction(args.id)
    if success:
        print(f"\n🗑️ Successfully deleted transaction {args.id}.")
        return 0
    else:
        print(f"\n❌ Transaction {args.id} not found.")
        return 1


def cmd_deposit_savings(store: FinanceStore, args: argparse.Namespace) -> int:
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    amount_cents = round(args.amount * 100)
    postings = [
        Posting(account_id="Assets:Checking:WellsFargo", amount_cents=-amount_cents, memo=args.memo or "Transfer to HYSA"),
        Posting(account_id="Assets:Savings:CapitalOne", amount_cents=amount_cents, memo=args.memo or "HYSA Deposit"),
    ]
    tx = Transaction.create(
        date=date,
        payee="Capital One HYSA Deposit",
        postings=postings,
        memo=args.memo or "Transfer to HYSA",
        source="cli",
    )
    logged = store.log_transaction(tx)
    try:
        FinanceExporter(store).export_to_beancount()
    except Exception:
        pass
    print(f"\n✅ Logged ${args.amount:,.2f} savings deposit for {date}:")
    print(f"   • From: Wells Fargo Checking (-${args.amount:,.2f})")
    print(f"   • To:   Capital One Savings (+${args.amount:,.2f})")
    print(f"   • Tx ID: {logged.id[:8]}")
    return 0


def cmd_breakdown(store: FinanceStore, recurring: RecurringEngine, args: argparse.Namespace) -> int:
    now_str = getattr(args, "date", None) or datetime.now().strftime("%Y-%m-%d")
    month_str = now_str[:7]
    overview = store.get_monthly_overview(month_str)
    alloc = recurring.get_paycheck_allocation(as_of_date=now_str)
    balances = store.get_all_balances(as_of_date=now_str)
    masked = args.mask

    def _f(cents: int, label: str = "VAL") -> str:
        return _format_currency(cents, masked, label)

    print("\n" + "=" * 70)
    print("📊 COMPREHENSIVE FINANCIAL BREAKDOWN & BI-WEEKLY ALLOCATION")
    print("=" * 70)
    print(f"As of: {now_str} | Month: {month_str} [{overview.status.upper()}]")
    print(f"Liquid Capital: {_f(balances['liquid_assets_cents'], 'LIQUID_ASSETS')} | Net Worth: {_f(balances['net_worth_cents'], 'NET_WORTH')}")
    print("-" * 70)

    print(f"\n🗓️  MONTHLY BREAKDOWN ({month_str}):")
    print(f"  • Monthly Take-Home Income:    {_f(overview.income_cents, 'INCOME'):>12}")
    print(f"  • Monthly HYSA Savings Target: {_f(overview.savings_target_cents, 'SAVINGS'):>12} (Target transfer to Capital One)")
    print(f"  • Fixed Bills & PG&E:          {_f(overview.all_bills_cents, 'ALL_BILLS'):>12} (Fixed: {_f(overview.fixed_bills_cents)}, PG&E: {_f(overview.pge_cents)})")
    print(f"  • Groceries Allocation:        {_f(overview.groceries_planned_cents, 'GROCERIES'):>12} (Spent: {_f(overview.groceries_actual_cents)}, Left: {_f(overview.groceries_left_cents)})")
    if overview.dining_planned_cents > 0:
        print(f"  • Dining / Eating Out:         {_f(overview.dining_planned_cents, 'DINING'):>12} (Spent: {_f(overview.dining_actual_cents)}, Left: {_f(overview.dining_left_cents)})")
    print(f"  • Car Gas Allocation:          {_f(overview.gas_planned_cents, 'GAS'):>12} (Spent: {_f(overview.gas_actual_cents)}, Left: {_f(overview.gas_left_cents)})")
    print("  " + "-" * 50)
    print(f"  • Total Personal Allowance:    {_f(overview.total_personal_cents, 'TOTAL_PERSONAL'):>12} (Budgeted discretionary)")
    print(f"  • Remaining Flex Cushion:      {_f(overview.remaining_cushion_cents, 'REMAINING_CUSHION'):>12} (Unspent variable funds)")

    print(f"\n💵 NEXT TWO WEEKS ALLOCATION ({alloc.paycheck_date} to {alloc.next_paycheck_date}):")
    print(f"  • Bi-Weekly Paycheck:          {_f(alloc.paycheck_amount_cents, 'PAYCHECK'):>12}")
    print(f"  • Immediate HYSA Savings:     -{_f(alloc.savings_transfer_cents, 'HYSA_SAVINGS'):>12} (Moved straight to Capital One)")
    print(f"  • Net Checking Inflow:         {_f(alloc.net_checking_cents, 'NET_CHECKING'):>12}")
    print("  " + "-" * 50)
    print(f"  • Upcoming Bills in Window:   -{_f(alloc.bills_due_cents, 'BILLS_DUE'):>12} ({len(alloc.bills)} bills scheduled)")
    for b in alloc.bills:
        print(f"      - {b['due_date']}  {b['name']:<24} {_f(b['amount_cents']):>10}")
    print(f"  • 2-Week Groceries Allocation:-{_f(alloc.groceries_allocated_cents, 'GROCERIES_2W'):>12}")
    if alloc.dining_allocated_cents > 0:
        print(f"  • 2-Week Dining Out Buffer:   -{_f(alloc.dining_allocated_cents, 'DINING_2W'):>12}")
    print(f"  • 2-Week Car Gas Allocation:  -{_f(alloc.gas_allocated_cents, 'GAS_2W'):>12}")
    print("=" * 70)
    print(f"🎯 SAFE-TO-SPEND CUSHION FOR NEXT 2 WEEKS: {_f(alloc.safe_to_spend_cents, 'SAFE_TO_SPEND'):>10}")
    print("=" * 70 + "\n")
    return 0


def cmd_paycheck(recurring: RecurringEngine, args: argparse.Namespace) -> int:
    alloc = recurring.get_paycheck_allocation(as_of_date=args.date)
    masked = args.mask

    def _f(cents: int, label: str = "VAL") -> str:
        return _format_currency(cents, masked, label)

    print("\n💵 BI-WEEKLY PAYCHECK ALLOCATION & CASH FLOW")
    print("=" * 65)
    print(f"Cycle Window:          {alloc.paycheck_date} to {alloc.next_paycheck_date} ({alloc.days_in_cycle} days)")
    print(f"Paycheck Take-Home:    {_f(alloc.paycheck_amount_cents, 'PAYCHECK'):>14}")
    print(f"HYSA Savings Transfer: {_f(alloc.savings_transfer_cents, 'HYSA_SAVINGS'):>14} (Moved straight to Capital One)")
    print(f"Net Checking Inflow:   {_f(alloc.net_checking_cents, 'NET_CHECKING'):>14}")
    print("-" * 65)
    print(f"Bills Due in Window:   {_f(alloc.bills_due_cents, 'BILLS_DUE'):>14} ({len(alloc.bills)} bills scheduled)")
    for b in alloc.bills:
        print(f"   • {b['due_date']}  {b['name']:<24} {_f(b['amount_cents']):>14}")
    print(f"2-Week Variable Buffer:{_f(alloc.variable_needs_cents, 'VARIABLE_BUFFER'):>14} (Groceries: {_f(alloc.groceries_allocated_cents)}, Gas: {_f(alloc.gas_allocated_cents)})")
    print("=" * 65)
    print(f"SAFE-TO-SPEND CUSHION: {_f(alloc.safe_to_spend_cents, 'SAFE_TO_SPEND'):>14}")
    print("=" * 65 + "\n")
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

    # setup
    p_setup = subparsers.add_parser("setup", parents=[common_parser], help="Apply or create initial setup configuration from setup.yaml")
    p_setup.add_argument("--file", help="Path to custom setup.yaml (defaults to ~/.memory/private/finances/setup.yaml)")
    p_setup.add_argument("--create", dest="create_template", action="store_true", help="Generate template if not present")

    # paycheck
    p_pay = subparsers.add_parser("paycheck", parents=[common_parser], help="Inspect bi-weekly paycheck cash flow allocation and safe-to-spend")
    p_pay.add_argument("--date", help="Paycheck cycle date to inspect (defaults to today)")

    # breakdown / plan
    p_plan = subparsers.add_parser("plan", parents=[common_parser], help="Comprehensive financial situation, monthly breakdown, and 2-week allocation")
    p_plan.add_argument("--date", help="Date or cycle to inspect (defaults to today)")

    p_break = subparsers.add_parser("breakdown", parents=[common_parser], help="Alias for 'plan'")
    p_break.add_argument("--date", help="Date or cycle to inspect (defaults to today)")

    # import-text
    p_imp = subparsers.add_parser("import-text", parents=[common_parser], help="Batch import historical daily bullet notes from a text file")
    p_imp.add_argument("--file", help="Path to text document (or read from stdin)")
    p_imp.add_argument("--year", type=int, default=datetime.now().year, help="Default year for Day/Month dates (defaults to current year)")

    # clear-day
    p_clear = subparsers.add_parser("clear-day", parents=[common_parser], help="Clear all transactions for a specific date")
    p_clear.add_argument("--date", help="Date to clear (defaults to today YYYY-MM-DD)")

    # clear-imported / reset
    p_clrimp = subparsers.add_parser("clear-imported", parents=[common_parser], help="Clear all imported/logged transactions, preserving setup opening balances")
    p_reset = subparsers.add_parser("reset-transactions", parents=[common_parser], help="Alias for 'clear-imported'")

    # delete-tx
    p_deltx = subparsers.add_parser("delete-tx", parents=[common_parser], help="Delete a specific transaction by ID")
    p_deltx.add_argument("--id", required=True, help="Transaction ID")

    # deposit-savings
    p_dep = subparsers.add_parser("deposit-savings", parents=[common_parser], help="Log a savings transfer from checking to Capital One HYSA")
    p_dep.add_argument("amount", type=float, help="Amount in dollars (e.g. 500 or 500.00)")
    p_dep.add_argument("--date", help="Date of deposit (defaults to today YYYY-MM-DD)")
    p_dep.add_argument("--memo", default="HYSA Deposit", help="Optional memo")

    # export
    p_exp = subparsers.add_parser("export", parents=[common_parser], help="Export to Beancount and CSV formats")
    p_exp.add_argument("--beancount", action="store_true", help="Export to journal.beancount")
    p_exp.add_argument("--csv", action="store_true", help="Export to finances_export.csv")

    args = parser.parse_args()

    store = FinanceStore(root_dir=args.vault_dir)
    recurring = RecurringEngine(store)

    if args.command == "setup":
        return cmd_setup(store, args)
    elif args.command in ("plan", "breakdown"):
        return cmd_breakdown(store, recurring, args)
    elif args.command == "import-text":
        return cmd_import_text(store, args)
    elif args.command == "clear-day":
        return cmd_clear_day(store, args)
    elif args.command in ("clear-imported", "reset-transactions"):
        return cmd_clear_imported(store, args)
    elif args.command == "delete-tx":
        return cmd_delete_tx(store, args)
    elif args.command == "deposit-savings":
        return cmd_deposit_savings(store, args)
    elif args.command == "paycheck":
        return cmd_paycheck(recurring, args)
    elif args.command == "status":
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
