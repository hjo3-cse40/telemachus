"""Export engine projecting SQLite financial state into Plain Text Accounting (Beancount) and CSV."""
from __future__ import annotations

import csv
from datetime import datetime
import io
import os
from pathlib import Path
from typing import Optional

from telemachus.finance.masking import FinanceMasker
from telemachus.finance.store import FinanceStore


class FinanceExporter:
    """Projects SQLite double-entry state into human-readable Beancount and CSV spreadsheets."""

    def __init__(self, store: FinanceStore):
        self.store = store
        self.masker = FinanceMasker()

    def export_to_beancount(
        self,
        output_path: Optional[Path | str] = None,
        masked: bool = False,
    ) -> Path:
        """Projects full ledger to standard Beancount Plain Text Accounting format."""
        target = (
            Path(output_path).expanduser().resolve()
            if output_path
            else self.store.root_dir / "journal.beancount"
        )
        target.parent.mkdir(parents=True, exist_ok=True)

        lines: list[str] = [
            ";; ============================================================================",
            ";; Telemachus Plain Text Accounting Ledger (Beancount Projection)",
            f";; Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            ";; Single Source of Truth: SQLite (finances.sqlite3)",
            ";; ============================================================================\n",
            'option "title" "Telemachus Personal Financial Ledger"',
            'option "operating_currency" "USD"\n',
        ]

        # Declare Open Accounts
        accounts = self.store.list_accounts(active_only=False)
        lines.append(";; Open Account Directives")
        for acc in accounts:
            lines.append(f"2026-01-01 open {acc.id} {acc.currency}")
        lines.append("")

        # Export All Transactions in chronological order
        txs = self.store.list_transactions(limit=100000)
        # Reverse to get oldest first for beancount chronological flow
        txs.reverse()

        lines.append(";; Transactions & Postings")
        for tx in txs:
            payee_clean = tx.payee.replace('"', '\\"')
            memo_clean = (self.masker.mask_text(tx.memo) if masked else tx.memo).replace('"', '\\"')
            header = f'{tx.date} * "{payee_clean}"'
            if memo_clean:
                header += f' "{memo_clean}"'
            lines.append(header)

            for p in tx.postings:
                dollars = p.amount_cents / 100.0
                sign = "" if dollars >= 0 else "-"
                abs_val = abs(dollars)
                val_str = f"{sign}{abs_val:.2f} USD"
                if masked:
                    val_str = f"<{p.account_id.split(':')[-1].upper()}_VAL>"
                lines.append(f"  {p.account_id:<36} {val_str:>14}")
            lines.append("")

        content = "\n".join(lines) + "\n"
        target.write_text(content, encoding="utf-8")
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass

        return target

    def export_to_csv(
        self,
        output_path: Optional[Path | str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        masked: bool = False,
    ) -> Path:
        """Exports transactions to a standard CSV spreadsheet compatible with Apple Numbers & Excel."""
        target = (
            Path(output_path).expanduser().resolve()
            if output_path
            else self.store.root_dir / "finances_export.csv"
        )
        target.parent.mkdir(parents=True, exist_ok=True)

        txs = self.store.list_transactions(start_date=start_date, end_date=end_date, limit=100000)

        with open(target, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Date",
                "Transaction ID",
                "Payee",
                "Memo",
                "Account",
                "Amount (USD)",
                "Status",
            ])

            for tx in txs:
                memo = self.masker.mask_text(tx.memo) if masked else tx.memo
                for p in tx.postings:
                    dollars = p.amount_cents / 100.0
                    amt_str = f"{dollars:.2f}" if not masked else f"<{p.account_id.split(':')[-1].upper()}>"
                    writer.writerow([
                        tx.date,
                        tx.id,
                        tx.payee,
                        memo,
                        p.account_id,
                        amt_str,
                        tx.status,
                    ])

        try:
            os.chmod(target, 0o600)
        except OSError:
            pass

        return target
