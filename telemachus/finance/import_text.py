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

# Regex patterns for date headers
DATE_PATTERNS = [
    # YYYY-MM-DD (e.g. 2026-10-23)
    re.compile(r"^\s*#*\s*(\d{4}-\d{2}-\d{2})\b"),
    # Month Day, Year (e.g. July 15, 2026 or Jul 15, 2026)
    re.compile(r"^\s*#*\s*([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})\b"),
    # Month Day without year (e.g. July 15 or Jul 15)
    re.compile(r"^\s*#*\s*([A-Za-z]+)\s+(\d{1,2})\b"),
    # MM/DD/YYYY (e.g. 07/15/2026)
    re.compile(r"^\s*#*\s*(\d{1,2})/(\d{1,2})/(\d{4})\b"),
]

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
    """Attempts to match a date header line and returns YYYY-MM-DD or None."""
    line = line.strip()
    if not line:
        return None

    # 1. YYYY-MM-DD
    m1 = DATE_PATTERNS[0].match(line)
    if m1:
        return m1.group(1)

    # 2. Month Day, Year
    m2 = DATE_PATTERNS[1].match(line)
    if m2:
        m_name, day, year = m2.group(1).lower(), int(m2.group(2)), int(m2.group(3))
        m_num = MONTH_NAMES.get(m_name)
        if m_num:
            return f"{year:04d}-{m_num:02d}-{day:02d}"

    # 3. Month Day (use default_year)
    m3 = DATE_PATTERNS[2].match(line)
    if m3:
        m_name, day = m3.group(1).lower(), int(m3.group(2))
        m_num = MONTH_NAMES.get(m_name)
        if m_num:
            return f"{default_year:04d}-{m_num:02d}-{day:02d}"

    # 4. MM/DD/YYYY
    m4 = DATE_PATTERNS[3].match(line)
    if m4:
        m_num, day, year = int(m4.group(1)), int(m4.group(2)), int(m4.group(3))
        return f"{year:04d}-{m_num:02d}-{day:02d}"

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

    # Category matching
    if any(k in cat_lower for k in ("grocery", "groceries", "safeway", "trader", "costco food", "supermarket")):
        category = "groceries"
    elif any(k in cat_lower for k in ("gas", "fuel", "chevron", "shell", "gasoline")):
        category = "gas"
    elif any(k in cat_lower for k in ("lunch", "dinner", "dining", "taqueria", "burrito", "chipotle", "restaurant", "food", "coffee", "starbucks")):
        category = "dining"
    elif any(k in cat_lower for k in ("wifi", "wi-fi", "internet", "comcast", "att")):
        category = "wifi"
    elif any(k in cat_lower for k in ("pge", "pg&e", "electric", "power", "utility")):
        category = "pge"
    elif "gym" in cat_lower:
        category = "gym"
    elif "spotify" in cat_lower:
        category = "spotify"
    elif any(k in cat_lower for k in ("icloud", "apple")):
        category = "icloud"
    elif any(k in cat_lower for k in ("kahram", "brother")):
        category = "kahram"
    else:
        category = "personal"

    return {
        "payee": desc_clean or category.title(),
        "amount": amount,
        "amount_cents": int(round(amount * 100)),
        "category": category,
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
