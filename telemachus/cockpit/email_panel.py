"""Email Triage Panel reader for Telemachus Cockpit.

Queries the local offline SQLite email index (data/email_index.sqlite3),
maps Laya ModernBERT classifications into priority badges, and formats the triage feed.
"""
from __future__ import annotations

import logging
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional

from telemachus.config.loader import find_project_root

logger = logging.getLogger(__name__)


class EmailTriagePanel:
    """Reads and formats email triage records for terminal display."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            root = find_project_root()
            main_db = root / "data" / "email_index.sqlite3"
            synth_db = root / "data" / "synthetic_email_index.sqlite3"
            db_path = main_db if main_db.exists() else synth_db
        self.db_path = Path(db_path)

    def _get_connection(self) -> Optional[sqlite3.Connection]:
        if not self.db_path.exists():
            return None
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def get_summary_stats(self) -> Dict[str, int]:
        """Returns counts by category and urgency."""
        stats = {"total": 0, "urgent": 0, "action": 0, "newsletter": 0, "spam": 0}
        conn = self._get_connection()
        if not conn:
            return stats

        try:
            with conn:
                cur = conn.cursor()
                cur.execute("SELECT category, attention, count(*) FROM indexed_messages GROUP BY category, attention")
                for cat, att, count in cur.fetchall():
                    stats["total"] += count
                    cat = (cat or "").lower()
                    att = (att or "").lower()
                    if "important" in cat or "review" in att or "urgent" in att:
                        stats["urgent"] += count
                    elif "transactional" in cat or "action" in att:
                        stats["action"] += count
                    elif "newsletter" in cat:
                        stats["newsletter"] += count
                    elif "spam" in cat:
                        stats["spam"] += count
        except Exception as e:
            logger.debug(f"Email triage stats failed: {e}")
        finally:
            conn.close()
        return stats

    def get_triage_items(self, limit: int = 25) -> List[Dict[str, Any]]:
        """Fetches prioritized email items sorted by urgency and date."""
        conn = self._get_connection()
        if not conn:
            return []

        items = []
        try:
            with conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT account_id, sender, subject, date_str, category, attention, snippet
                    FROM indexed_messages
                    ORDER BY
                        CASE
                            WHEN category = 'important_documents' THEN 1
                            WHEN attention = 'important_review' THEN 2
                            WHEN category = 'transactional' THEN 3
                            WHEN category = 'newsletter_marketing' THEN 4
                            ELSE 5
                        END ASC,
                        date_epoch DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                for row in cur.fetchall():
                    cat = (row["category"] or "").lower()
                    att = (row["attention"] or "").lower()

                    if "important" in cat or "review" in att:
                        badge = "URGENT"
                        color_code = "\033[1;31m"  # Red / Bold
                    elif "transactional" in cat:
                        badge = "ACTION"
                        color_code = "\033[1;33m"  # Amber / Yellow
                    elif "newsletter" in cat:
                        badge = "DIGEST"
                        color_code = "\033[1;34m"  # Blue
                    else:
                        badge = "INFO"
                        color_code = "\033[1;36m"  # Cyan

                    sender = row["sender"] or "Unknown"
                    # Clean sender display: extract display name or email prefix
                    if "<" in sender:
                        sender_clean = sender.split("<")[0].strip().strip('"')
                        if not sender_clean:
                            sender_clean = sender.split("<")[1].split(">")[0].strip()
                    else:
                        sender_clean = sender

                    items.append({
                        "badge": badge,
                        "color_code": color_code,
                        "sender": sender_clean[:22],
                        "subject": (row["subject"] or "No subject")[:38],
                        "date": (row["date_str"] or "")[:12],
                        "category": row["category"],
                        "snippet": row["snippet"] or "",
                    })
        except Exception as e:
            logger.debug(f"Email triage items fetch failed: {e}")
        finally:
            conn.close()
        return items
