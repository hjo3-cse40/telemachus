"""Text Document & Historical Bullet List Importer for Telemachus Finances.

Parses text notes, daily logs, and multi-month historical lists into balanced
double-entry SQLite transactions. Supports Notion bullet format with +/- syntax.
"""
from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from telemachus.finance.export import FinanceExporter
from telemachus.finance.models import Transaction
from telemachus.finance.store import FinanceStore

logger = logging.getLogger(__name__)

MONTH_NAMES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def parse_date_header(line: str, default_year: int = 2026) -> Optional[str]:
    """Attempts to match a date header line and returns YYYY-MM-DD or None.
    
    Robustly supports:
    - Day/Month format: 23/10, 15/8, 15/08, 01/07, 1/7
    - Day/Month/Year: 23/10/2026, 15/8/26, 15-08-2026, 23.10.2026
    - Standard ISO: 2026-10-23, 2026/10/23
    - Day Month name: 23 Oct, 23rd October, 15 August 2026
    - Month Day: July 15, October 23, 2026
    - Prefixes: Day of week (Monday 23/10), markdown headers (## 15/08:), bullets, brackets
    """
    line = line.strip()
    if not line:
        return None

    # Strip markdown headers, bullets, asterisks, brackets, quotes
    clean = re.sub(r"^[#*•\-_–—\s\[\(]+", "", line).strip()
    clean = re.sub(r"[:\s*\]\)]+$", "", clean).strip()

    # Strip day of week prefixes: 'Monday', 'Mon', 'Monday,', 'Mon.'
    clean = re.sub(
        r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[.,]?\s*",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip()

    # 1. YYYY-MM-DD or YYYY/MM/DD
    m1 = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", clean)
    if m1:
        y, m, d = int(m1.group(1)), int(m1.group(2)), int(m1.group(3))
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"

    # 2. MonthName with Day or Day with MonthName (e.g. September 12, 12/September, Sep 12, 12 Sep)
    # Case A: MonthName first (e.g. September/12, September 12, Sep 12, 2026)
    m_md = re.match(r"^([A-Za-z]+)[-/.\s]+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*[-/.\s]*(\d{2,4}))?$", clean)
    if m_md:
        m_name = m_md.group(1).lower()
        d = int(m_md.group(2))
        m = MONTH_NAMES.get(m_name)
        if m and 1 <= d <= 31:
            raw_y = m_md.group(3)
            y = (int(raw_y) + 2000 if int(raw_y) < 100 else int(raw_y)) if raw_y else default_year
            return f"{y:04d}-{m:02d}-{d:02d}"

    # Case B: Day first with MonthName (e.g. 12/September, 12 Sep, 12-September, 12th August 2026)
    m_dm = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?[-/.\s]+([A-Za-z]+)(?:[-/.\s]+(\d{2,4}))?$", clean)
    if m_dm:
        d = int(m_dm.group(1))
        m_name = m_dm.group(2).lower()
        m = MONTH_NAMES.get(m_name)
        if m and 1 <= d <= 31:
            raw_y = m_dm.group(3)
            y = (int(raw_y) + 2000 if int(raw_y) < 100 else int(raw_y)) if raw_y else default_year
            return f"{y:04d}-{m:02d}-{d:02d}"

    # 3. Numeric dates with year: e.g. 7/30/2026, 09/12/2026, 10/23/26
    m_y = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$", clean)
    if m_y:
        n1, n2, raw_y = int(m_y.group(1)), int(m_y.group(2)), int(m_y.group(3))
        y = (2000 + raw_y) if raw_y < 100 else raw_y
        # MM/DD/YYYY format (standard for 7/30 to 10/23 range)
        if 1 <= n1 <= 12 and 1 <= n2 <= 31:
            return f"{y:04d}-{n1:02d}-{n2:02d}"
        elif 1 <= n2 <= 12 and 1 <= n1 <= 31:
            return f"{y:04d}-{n2:02d}-{n1:02d}"

    # 4. Numeric Month/Day without year: e.g. 7/30, 8/1, 9/12, 10/12, 10/23
    m_num = re.match(r"^(\d{1,2})[-/.](\d{1,2})$", clean)
    if m_num:
        n1, n2 = int(m_num.group(1)), int(m_num.group(2))
        # MM/DD format: n1 is Month (7, 8, 9, 10), n2 is Day (1 to 31)
        if 1 <= n1 <= 12 and 1 <= n2 <= 31:
            return f"{default_year:04d}-{n1:02d}-{n2:02d}"
        elif 1 <= n2 <= 12 and 1 <= n1 <= 31:
            # If n1 > 12, then n1 was day and n2 was month (e.g. 30/7)
            return f"{default_year:04d}-{n2:02d}-{n1:02d}"

    return None


def parse_bullet_line(line: str) -> Optional[Dict[str, Any]]:
    """Parses single bullet line with minus (-) or plus (+) syntax.
    
    Examples:
      - groceries - 42.20
      - gas - 35
      - taqueria - 14.50
      payday + 1350
      + 1350 payday
      - chipotle - 12.80 (lunch)
    """
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None

    # Strip leading bullet dashes / asterisks
    cleaned = re.sub(r"^[\s*•\-–—]+\s*", "", raw)
    if not cleaned:
        return None

    # Look for Savings deposit / transfer (e.g. "savings + 500", "savings deposit 500", "- savings - 500")
    if any(k in cleaned.lower() for k in ("saving", "savings", "hysa", "capital one")):
        num_m = re.search(r"\$?\s*([0-9]+(?:\.[0-9]{1,2})?)", cleaned)
        if num_m:
            amount = float(num_m.group(1))
            desc = re.sub(r"[\+\-–—]?\s*\$?[0-9]+(?:\.[0-9]{1,2})?", "", cleaned).strip(" -:\t")
            return {
                "payee": desc or "Capital One HYSA Deposit",
                "amount": amount,
                "amount_cents": int(round(amount * 100)),
                "category": "savings",
                "account_id": "Assets:Savings:CapitalOne",
                "to_account": "Assets:Savings:CapitalOne",
                "from_account": "Assets:Checking:WellsFargo",
                "memo": raw,
            }

    # Look for Credit Card payment (e.g. "credit card payment - 200", "cc payment 150", "- credit card - 500")
    if any(k in cleaned.lower() for k in ("credit card", "cc payment", "visa payment", "card payment")):
        num_m = re.search(r"\$?\s*([0-9]+(?:\.[0-9]{1,2})?)", cleaned)
        if num_m:
            amount = float(num_m.group(1))
            desc = re.sub(r"[\+\-–—]?\s*\$?[0-9]+(?:\.[0-9]{1,2})?", "", cleaned).strip(" -:\t")
            return {
                "payee": desc or "Credit Card Payment",
                "amount": amount,
                "amount_cents": int(round(amount * 100)),
                "category": "credit_card_payment",
                "to_account": "Liabilities:CreditCard:Visa",
                "from_account": "Assets:Checking:WellsFargo",
                "memo": raw,
            }

    # Look for plus format (Inflow / Payday)
    plus_match = re.search(r"\+\s*\$?([0-9]+(?:\.[0-9]{1,2})?)", cleaned)
    if plus_match or "payday" in cleaned.lower() or "salary" in cleaned.lower() or "deposit" in cleaned.lower():
        amt_str = plus_match.group(1) if plus_match else re.search(r"([0-9]+(?:\.[0-9]{1,2})?)", cleaned).group(1)
        amount = float(amt_str)
        # Remove number and symbols from description
        desc = re.sub(r"\+\s*\$?[0-9]+(?:\.[0-9]{1,2})?", "", cleaned).strip(" -:\t")
        return {
            "payee": desc or "Payday Payroll",
            "amount": amount,
            "amount_cents": int(round(amount * 100)),
            "category": "payday",
            "account_id": "Assets:Checking:WellsFargo",
            "memo": raw,
        }

    # Look for minus format (Outflow / Expense): e.g. "groceries - 20" or "groceries 20"
    minus_match = re.search(r"[-–—]?\s*\$?\s*([0-9]+(?:\.[0-9]{1,2})?)\s*$", cleaned)
    if minus_match:
        amt_str = minus_match.group(1)
        amount = float(amt_str)
        desc = cleaned[: minus_match.start()].strip(" -–—:\t")
    else:
        # Generic number extraction at end or middle
        num_match = re.search(r"\$?\s*([0-9]+\.[0-9]{2}|[0-9]+)\b", cleaned)
        if not num_match:
            return None
        amount = float(num_match.group(1))
        desc = cleaned[: num_match.start()].strip(" -–—:\t") + " " + cleaned[num_match.end():].strip(" -–—:\t")

    desc_clean = desc.strip()
    cat_lower = desc_clean.lower()

    # Category matching (keywords must start a word, so "att" no longer matches "mattress"
    # and "gas" no longer matches "Vegas"; "att" must be a whole word)
    def has(*words: str) -> bool:
        return any(re.search(rf"\b{re.escape(w)}", cat_lower) for w in words)

    if has("grocery", "groceries", "safeway", "trader", "costco food", "supermarket"):
        category = "groceries"
    elif has("gas", "fuel", "chevron", "shell", "gasoline"):
        category = "gas"
    elif has("lunch", "dinner", "dining", "taqueria", "burrito", "chipotle", "restaurant", "food", "coffee", "starbucks"):
        category = "dining"
    elif has("wifi", "wi-fi", "internet", "comcast") or re.search(r"\batt\b", cat_lower):
        category = "wifi"
    elif has("pge", "pg&e", "electric", "power", "utility"):
        category = "pge"
    elif has("gym"):
        category = "gym"
    elif has("spotify"):
        category = "spotify"
    elif has("icloud", "apple"):
        category = "icloud"
    elif has("kahram", "brother"):
        category = "kahram"
    elif has("credit card", "credit card payment", "cc payment", "visa payment", "card payment"):
        category = "credit_card_payment"
    else:
        category = "personal"

    return {
        "payee": desc_clean or category.title(),
        "amount": amount,
        "amount_cents": int(round(amount * 100)),
        "category": category,
        "from_account": "Assets:Checking:WellsFargo",
        "memo": raw,
    }


def import_text_document(
    text: Any,
    store: Optional[Any] = None,
    default_year: int = 2026,
) -> Dict[str, Any]:
    """Parses a multi-date text document and ingests all transactions into SQLite."""
    # Allow passing (store, text) or (text, store)
    if not isinstance(text, str) and isinstance(store, str):
        text, store = store, text

    actual_store = store or FinanceStore()
    lines = text.splitlines()

    current_date = datetime.now().strftime("%Y-%m-%d")
    total_parsed = 0
    logged_count = 0
    days_seen = set()
    all_batch_items: List[Dict[str, Any]] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check if line is a date header
        header_date = parse_date_header(stripped, default_year=default_year)
        if header_date:
            current_date = header_date
            days_seen.add(current_date)
            continue

        # Check if line has an inline date prefix before the bullet (e.g. "12/September - groceries - 25.50" or "23/10 - coffee - 5")
        date_pfx_m = re.match(r"^(\d{1,2}[-/.\s]+[A-Za-z0-9]+(?:[-/.\s]+\d{2,4})?)\s*[:\-–—]\s*(.*)$", stripped)
        if date_pfx_m:
            pfx_date = parse_date_header(date_pfx_m.group(1), default_year=default_year)
            if pfx_date:
                current_date = pfx_date
                days_seen.add(current_date)
                stripped = date_pfx_m.group(2).strip()

        # Try parsing line as bullet transaction
        item = parse_bullet_line(stripped)
        if item:
            item["date"] = current_date
            all_batch_items.append(item)
            days_seen.add(current_date)
            total_parsed += 1

    total_inflow_cents = 0
    total_outflow_cents = 0

    if all_batch_items:
        for itm in all_batch_items:
            amt_c = itm.get("amount_cents", int(round(itm["amount"] * 100)))
            if itm.get("category") in ("payday", "salary", "income"):
                total_inflow_cents += amt_c
            else:
                total_outflow_cents += amt_c

        logged = actual_store.log_batch_transactions(all_batch_items)
        logged_count = len(logged)
        try:
            FinanceExporter(actual_store).export_to_beancount()
        except Exception:
            pass

    return {
        "total_lines": len(lines),
        "items_parsed": total_parsed,
        "transactions_logged": logged_count,
        "total_days": len(days_seen),
        "days_count": len(days_seen),
        "total_transactions": logged_count,
        "total_inflow_cents": total_inflow_cents,
        "total_outflow_cents": total_outflow_cents,
        "dates": sorted(list(days_seen)),
    }
