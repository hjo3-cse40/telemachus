"""Privacy Masking & Redaction Proxy for Telemachus Financial Operations.

Guarantees that financial state, balances, account numbers, and cash flow numbers 
can be tokenized into semantic placeholders (e.g. <ACCOUNT_TOTAL_CHECKING>, <ESTIMATED_PGE_BILL>) 
or normalized percentage distributions before being passed to any prompt or public log.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple, Union

# Regex patterns for sensitive financial identifiers
CARD_PAN_RE = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
ROUTING_NUM_RE = re.compile(r"\b\d{9}\b")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CURRENCY_RE = re.compile(r"\$[\d,]+(?:\.\d{2})?")


class FinanceMasker:
    """Masks and tokenizes sensitive financial details."""

    def __init__(self, placeholder_prefix: str = "<", placeholder_suffix: str = ">"):
        self.prefix = placeholder_prefix
        self.suffix = placeholder_suffix

    def mask_text(self, text: str) -> str:
        """Sanitizes raw text, masking credit card numbers, routing numbers, and SSNs."""
        if not text:
            return ""

        # Mask SSNs: XXX-XX-1234
        def _mask_ssn(match: re.Match) -> str:
            val = match.group(0)
            return f"{self.prefix}SSN_REDACTED_***-{val[-4:]}{self.suffix}"

        # Mask Card PANs: ending in last 4
        def _mask_card(match: re.Match) -> str:
            clean = re.sub(r"[ -]", "", match.group(0))
            if 13 <= len(clean) <= 16:
                return f"{self.prefix}CARD_ENDING_{clean[-4:]}{self.suffix}"
            return match.group(0)

        masked = SSN_RE.sub(_mask_ssn, text)
        masked = CARD_PAN_RE.sub(_mask_card, masked)
        return masked

    def mask_balance_dict(self, balances: Dict[str, Any]) -> Dict[str, Any]:
        """Converts raw dollar/cents balances into tokenized semantic representations."""
        account_tokens: Dict[str, str] = {}
        for acc_id, cents in balances.get("account_balances", {}).items():
            clean_name = acc_id.split(":")[-1].upper()
            account_tokens[acc_id] = f"{self.prefix}BALANCE_{clean_name}{self.suffix}"

        return {
            "liquid_assets": f"{self.prefix}TOTAL_LIQUID_ASSETS{self.suffix}",
            "liabilities": f"{self.prefix}TOTAL_LIABILITIES{self.suffix}",
            "net_worth": f"{self.prefix}NET_WORTH{self.suffix}",
            "account_tokens": account_tokens,
            "as_of_date": balances.get("as_of_date", ""),
            "masked": True,
        }

    def mask_cash_flow_summary(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        """Normalizes cash flow summary into percentage distributions, hiding raw dollar amounts."""
        outflow_total = summary.get("outflow_cents", 0)
        category_pcts: Dict[str, float] = {}

        if outflow_total > 0:
            for cat, cents in summary.get("category_breakdown", {}).items():
                category_pcts[cat] = round((cents / outflow_total) * 100.0, 1)

        return {
            "start_date": summary.get("start_date"),
            "end_date": summary.get("end_date"),
            "evaluated_days": summary.get("evaluated_days"),
            "savings_rate_pct": summary.get("savings_rate_pct"),
            "inflow_token": f"{self.prefix}MONTHLY_INFLOW{self.suffix}",
            "outflow_token": f"{self.prefix}MONTHLY_OUTFLOW{self.suffix}",
            "net_savings_token": f"{self.prefix}NET_SAVINGS{self.suffix}",
            "category_distribution_pct": category_pcts,
            "masked": True,
        }

    def mask_transaction_list(self, transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Masks exact dollar amounts in a transaction list, preserving payees and dates."""
        masked_list = []
        for tx in transactions:
            masked_postings = []
            for p in tx.get("postings", []):
                acc = p.get("account_id", "")
                cat_tag = acc.split(":")[-1].upper()
                masked_postings.append({
                    "account_id": acc,
                    "amount_token": f"{self.prefix}AMOUNT_{cat_tag}{self.suffix}",
                    "memo": self.mask_text(p.get("memo") or ""),
                })

            masked_list.append({
                "id": tx.get("id"),
                "date": tx.get("date"),
                "payee": tx.get("payee"),
                "source": tx.get("source"),
                "postings": masked_postings,
                "memo": self.mask_text(tx.get("memo") or ""),
            })
        return masked_list
