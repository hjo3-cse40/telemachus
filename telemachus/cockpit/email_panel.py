"""Email Triage Panel reader for Telemachus Cockpit.

Queries the local offline SQLite email index (data/email_index.sqlite3),
maps Laya ModernBERT classifications into priority badges, and formats the triage feed.

TELEMACHUS_EMAIL_DB points the panel at another index (tests and screenshots use a synthetic one).
Without it, demo mode (TELEMACHUS_DEMO=1 or a HOME that is not the real one) reads only the
synthetic index, never data/email_index.sqlite3.
Each item has "kind": urgent, action, fyi or spam (the Home tab shows URGENT / ACTION / FYI).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional

from telemachus.config.loader import find_project_root

logger = logging.getLogger(__name__)


def email_kind(category: str, attention: str) -> str:
    """urgent, action, fyi or spam, from the Laya category and attention labels."""
    cat = (category or "").lower()
    att = (attention or "").lower()
    if "spam" in cat:
        return "spam"
    if "urgent" in att or "important_review" in att or att == "review":
        return "urgent"
    if "important" in cat or "action" in att or "transactional" in cat:
        return "action"
    return "fyi"


# Group order on the Home tab, and the same rule as email_kind() in SQL.
KIND_RANK = {"urgent": 0, "action": 1, "fyi": 2, "spam": 3}
_CAT = "lower(coalesce(category, ''))"
_ATT = "lower(coalesce(attention, ''))"
KIND_RANK_SQL = (
    f"CASE WHEN instr({_CAT}, 'spam') > 0 THEN 3"
    f" WHEN instr({_ATT}, 'urgent') > 0 OR instr({_ATT}, 'important_review') > 0 OR {_ATT} = 'review' THEN 0"
    f" WHEN instr({_CAT}, 'important') > 0 OR instr({_ATT}, 'action') > 0 OR instr({_CAT}, 'transactional') > 0 THEN 1"
    " ELSE 2 END"
)
DEMO_ENV = "TELEMACHUS_DEMO"


def demo_mode() -> bool:
    """True when the cockpit must not read the project's real data files (data/email_index.sqlite3,
    data/trash_audit.jsonl): TELEMACHUS_DEMO=1, or HOME is not the user's real home folder (the
    screenshot recipe runs with a fake HOME). TELEMACHUS_DEMO=0 turns it off."""
    flag = os.environ.get(DEMO_ENV, "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    try:
        import pwd
        real = pwd.getpwuid(os.getuid()).pw_dir
        return os.path.realpath(str(Path.home())) != os.path.realpath(real)
    except Exception:
        return False


def _epoch(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


ITEM_COLUMNS = ("account_id", "uid", "sender", "subject", "date_str", "date_epoch", "category", "attention", "snippet")


class EmailTriagePanel:
    """Reads and formats email triage records for terminal display."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None and os.environ.get("TELEMACHUS_EMAIL_DB"):
            db_path = Path(os.environ["TELEMACHUS_EMAIL_DB"]).expanduser()
        if db_path is None:
            root = find_project_root()
            main_db = root / "data" / "email_index.sqlite3"
            synth_db = root / "data" / "synthetic_email_index.sqlite3"
            # demo or fake HOME: never the real inbox, only the synthetic one (or none)
            db_path = main_db if main_db.exists() and not demo_mode() else synth_db
        self.db_path = Path(db_path)
        self.last_error: Optional[str] = None  # set by get_triage_items() when its query failed

    def index_exists(self) -> bool:
        """True when the email index file exists (it may still be empty)."""
        return self.db_path.exists()

    def _get_connection(self) -> Optional[sqlite3.Connection]:
        if not self.db_path.exists():
            return None
        # Read-only: the cockpit never changes the index. Plain open as a fallback (WAL edge cases).
        try:
            conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        except sqlite3.Error:
            conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def get_summary_stats(self) -> Dict[str, int]:
        """Returns counts by category and urgency."""
        stats = {"total": 0, "urgent": 0, "action": 0, "newsletter": 0, "spam": 0,
                 "kind_urgent": 0, "kind_action": 0, "kind_fyi": 0, "kind_spam": 0}
        conn = self._get_connection()
        if not conn:
            return stats

        try:
            with conn:
                cur = conn.cursor()
                cur.execute("SELECT category, attention, count(*) FROM indexed_messages GROUP BY category, attention")
                for cat, att, count in cur.fetchall():
                    stats["total"] += count
                    stats["kind_" + email_kind(cat, att)] += count
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
        """Fetches mail sorted the way the Home tab groups it: urgent, action, FYI, spam, and
        newest first inside each group.

        Columns the index does not have read as empty (older or synthetic indexes). When the
        query still fails, the error text is kept in self.last_error (None after a good read),
        so the view can tell "nothing to triage" from "could not read".
        """
        self.last_error = None
        conn = self._get_connection()
        if not conn:
            return []

        items = []
        try:
            with conn:
                cur = conn.cursor()
                have = {r[1] for r in cur.execute("PRAGMA table_info(indexed_messages)").fetchall()}
                if not have:
                    return []  # no table yet: the stats say 0 too (empty state, not an error)
                cols = ", ".join(c if c in have else f"NULL AS {c}" for c in ITEM_COLUMNS)
                cur.execute(
                    f"""
                    SELECT * FROM (SELECT {cols} FROM indexed_messages)
                    ORDER BY {KIND_RANK_SQL} ASC, date_epoch DESC
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
                        "kind": email_kind(cat, att),
                        "color_code": color_code,
                        "sender": " ".join(sender_clean.split())[:60],
                        "subject": " ".join((row["subject"] or "No subject").split())[:200],
                        "date": (row["date_str"] or "")[:12],
                        "epoch": row["date_epoch"],
                        "uid": row["uid"],
                        "category": row["category"],
                        "snippet": " ".join((row["snippet"] or "").split())[:600],
                    })
        except Exception as e:
            logger.debug(f"Email triage items fetch failed: {e}")
            self.last_error = str(e) or e.__class__.__name__
            items = []
        finally:
            conn.close()
        # Same groups as email_kind(); newest first inside a group (the SQL already does this,
        # this keeps the order right if the two ever drift apart).
        items.sort(key=lambda it: (KIND_RANK.get(it["kind"], 2), -_epoch(it.get("epoch"))))
        return items
