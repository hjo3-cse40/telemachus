"""Local SQLite storage for offline-indexed email metadata and Laya classifications."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List, Optional

from telemachus.config.loader import find_project_root


class EmailIndexStore:
    """Thread-safe SQLite store for indexed and classified emails."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_dir = find_project_root() / "data"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = db_dir / "email_index.sqlite3"
        self.db_path = Path(db_path)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS indexed_messages (
                    account_id TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    sender TEXT,
                    subject TEXT,
                    date_str TEXT,
                    date_epoch INTEGER,
                    snippet TEXT,
                    has_attachments INTEGER DEFAULT 0,
                    flags TEXT,
                    list_id TEXT,
                    category TEXT,
                    attention TEXT,
                    preservation TEXT,
                    confidence REAL,
                    reasons TEXT,
                    indexed_at TEXT,
                    PRIMARY KEY (account_id, folder, uid)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_msg_category
                ON indexed_messages(account_id, category)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_msg_attention
                ON indexed_messages(account_id, attention)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_msg_date
                ON indexed_messages(account_id, date_epoch DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS index_cursors (
                    account_id TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    highest_uid INTEGER DEFAULT 0,
                    lowest_uid INTEGER DEFAULT 0,
                    total_scanned INTEGER DEFAULT 0,
                    last_sync_at TEXT,
                    PRIMARY KEY (account_id, folder)
                )
                """
            )
            conn.commit()

    def get_indexed_uids(self, account_id: str, folder: str) -> set[str]:
        """Returns set of all UIDs already indexed for this account and folder."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT uid FROM indexed_messages WHERE account_id=? AND folder=?",
                (account_id, folder),
            ).fetchall()
            return {row["uid"] for row in rows}

    def save_classified_batch(self, records: List[Dict[str, Any]]) -> int:
        """Atomically saves a batch of classified records."""
        if not records:
            return 0
        now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._get_connection() as conn:
            for r in records:
                conn.execute(
                    """
                    INSERT INTO indexed_messages (
                        account_id, folder, uid, sender, subject, date_str,
                        date_epoch, snippet, has_attachments, flags, list_id,
                        category, attention, preservation, confidence, reasons, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, folder, uid) DO UPDATE SET
                        category=excluded.category,
                        attention=excluded.attention,
                        preservation=excluded.preservation,
                        confidence=excluded.confidence,
                        reasons=excluded.reasons,
                        indexed_at=excluded.indexed_at
                    """,
                    (
                        r["account_id"],
                        r["folder"],
                        r["uid"],
                        r.get("sender", ""),
                        r.get("subject", ""),
                        r.get("date_str", ""),
                        r.get("date_epoch", 0),
                        r.get("snippet", ""),
                        1 if r.get("has_attachments") else 0,
                        json.dumps(r.get("flags", [])),
                        r.get("list_id", ""),
                        r.get("category", "unclear"),
                        r.get("attention", "routine"),
                        r.get("preservation", "review_first"),
                        float(r.get("confidence", 0.0)),
                        json.dumps(r.get("reasons", [])),
                        now_str,
                    ),
                )
            conn.commit()
        return len(records)

    def search(
        self,
        account_id: Optional[str] = None,
        query: Optional[str] = None,
        category: Optional[str] = None,
        attention: Optional[str] = None,
        limit: int = 15,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Searches indexed messages using SQL filters."""
        sql = "SELECT * FROM indexed_messages WHERE 1=1"
        params: List[Any] = []

        if account_id:
            sql += " AND account_id = ?"
            params.append(account_id)
        if category:
            sql += " AND category = ?"
            params.append(category)
        if attention:
            sql += " AND attention = ?"
            params.append(attention)
        if query:
            sql += " AND (subject LIKE ? OR snippet LIKE ? OR sender LIKE ?)"
            wildcard = f"%{query}%"
            params.extend([wildcard, wildcard, wildcard])

        sql += " ORDER BY date_epoch DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def get_stats(self, account_id: Optional[str] = None) -> Dict[str, Any]:
        """Returns statistics on the indexed mailbox."""
        sql_base = "FROM indexed_messages"
        params: List[Any] = []
        if account_id:
            sql_base += " WHERE account_id = ?"
            params.append(account_id)

        with self._get_connection() as conn:
            total_count = conn.execute(f"SELECT COUNT(*) {sql_base}", params).fetchone()[0]

            cat_rows = conn.execute(
                f"SELECT category, COUNT(*) as cnt {sql_base} GROUP BY category ORDER BY cnt DESC",
                params,
            ).fetchall()
            categories = {row["category"]: row["cnt"] for row in cat_rows}

            attn_rows = conn.execute(
                f"SELECT attention, COUNT(*) as cnt {sql_base} GROUP BY attention ORDER BY cnt DESC",
                params,
            ).fetchall()
            attention = {row["attention"]: row["cnt"] for row in attn_rows}

            earliest_row = conn.execute(
                f"SELECT MIN(date_str), MAX(date_str) {sql_base}",
                params,
            ).fetchone()
            earliest = earliest_row[0] if earliest_row else None
            latest = earliest_row[1] if earliest_row else None

            return {
                "account_id": account_id or "all",
                "total_indexed": total_count,
                "categories": categories,
                "attention_breakdown": attention,
                "earliest_date": earliest,
                "latest_date": latest,
            }
