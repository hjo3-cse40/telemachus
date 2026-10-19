"""High-speed SQLite Cache & Search Index with WAL mode & FTS5 full-text search."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional

from telemachus.memory.models import MemoryEntry, PrivacyTier
from telemachus.memory.vault import MemoryVault

logger = logging.getLogger(__name__)


class MemoryCache:
    """Manages the SQLite cache and FTS5 search index."""

    def __init__(self, db_path: Optional[Path | str] = None, vault: Optional[MemoryVault] = None):
        if db_path:
            self.db_path = Path(db_path).resolve()
        elif vault:
            self.db_path = vault.private_dir / ".cache" / "index.sqlite3"
        else:
            self.db_path = Path.home() / ".memory" / "private" / ".cache" / "index.sqlite3"

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                category TEXT NOT NULL,
                tier TEXT NOT NULL,
                source TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                uses INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER NOT NULL DEFAULT 0,
                doc_path TEXT,
                content_hash TEXT
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_tier ON memories(tier);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_cat ON memories(category);")

            # Create FTS5 virtual table for full-text search
            conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                id UNINDEXED,
                text,
                category
            );
            """)

    def upsert_entry(self, entry: MemoryEntry, doc_path: Optional[str] = None) -> None:
        """Upserts a MemoryEntry into the SQLite index and updates FTS5."""
        content_hash = hashlib.sha256(f"{entry.id}:{entry.text}:{entry.category}".encode()).hexdigest()

        with self._get_connection() as conn:
            # Check existing
            existing = conn.execute("SELECT content_hash FROM memories WHERE id = ?", (entry.id,)).fetchone()
            if existing and existing["content_hash"] == content_hash:
                # Up to date, just update uses if higher
                conn.execute(
                    "UPDATE memories SET uses = MAX(uses, ?), pinned = ? WHERE id = ?",
                    (entry.uses, 1 if entry.pinned else 0, entry.id),
                )
                return

            conn.execute("""
            INSERT INTO memories (id, text, category, tier, source, timestamp, uses, pinned, doc_path, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                text = excluded.text,
                category = excluded.category,
                tier = excluded.tier,
                source = excluded.source,
                timestamp = excluded.timestamp,
                uses = MAX(memories.uses, excluded.uses),
                pinned = excluded.pinned,
                doc_path = excluded.doc_path,
                content_hash = excluded.content_hash;
            """, (
                entry.id,
                entry.text,
                entry.category,
                entry.tier.value,
                entry.source,
                entry.timestamp,
                entry.uses,
                1 if entry.pinned else 0,
                doc_path,
                content_hash,
            ))

            # Update FTS5
            conn.execute("DELETE FROM memories_fts WHERE id = ?", (entry.id,))
            conn.execute(
                "INSERT INTO memories_fts (id, text, category) VALUES (?, ?, ?)",
                (entry.id, entry.text, entry.category),
            )

    def search(
        self,
        query: str,
        tier_limit: Optional[PrivacyTier] = None,
        limit: int = 10,
    ) -> List[MemoryEntry]:
        """Performs full-text keyword search across memories, respecting privacy bounds."""
        clean_query = query.strip().replace('"', '""')
        if not clean_query:
            return []

        allowed_tiers: List[str] = []
        if tier_limit == PrivacyTier.TIER_0_PUBLIC:
            allowed_tiers = [PrivacyTier.TIER_0_PUBLIC.value]
        elif tier_limit == PrivacyTier.TIER_1_PROJECT:
            allowed_tiers = [PrivacyTier.TIER_0_PUBLIC.value, PrivacyTier.TIER_1_PROJECT.value]
        else:
            # Full local access
            allowed_tiers = [
                PrivacyTier.TIER_0_PUBLIC.value,
                PrivacyTier.TIER_1_PROJECT.value,
                PrivacyTier.TIER_2_PRIVATE.value,
            ]

        placeholders = ",".join("?" for _ in allowed_tiers)
        results: List[MemoryEntry] = []

        with self._get_connection() as conn:
            try:
                # FTS5 search with BM25 ranking
                sql = f"""
                SELECT m.* FROM memories m
                JOIN memories_fts fts ON m.id = fts.id
                WHERE memories_fts MATCH ? AND m.tier IN ({placeholders})
                ORDER BY m.pinned DESC, bm25(memories_fts) ASC, m.uses DESC
                LIMIT ?
                """
                cursor = conn.execute(sql, (clean_query, *allowed_tiers, limit))
                for row in cursor.fetchall():
                    results.append(self._row_to_entry(row))
            except sqlite3.OperationalError as e:
                logger.debug(f"FTS5 match failed, falling back to LIKE: {e}")
                # Fallback to standard LIKE
                like_param = f"%{clean_query}%"
                sql = f"""
                SELECT m.* FROM memories m
                WHERE (m.text LIKE ? OR m.category LIKE ?) AND m.tier IN ({placeholders})
                ORDER BY m.pinned DESC, m.uses DESC
                LIMIT ?
                """
                cursor = conn.execute(sql, (like_param, like_param, *allowed_tiers, limit))
                for row in cursor.fetchall():
                    results.append(self._row_to_entry(row))

        return results

    def get_recent(self, limit: int = 10, tier_limit: Optional[PrivacyTier] = None) -> List[MemoryEntry]:
        """Returns the most recently recorded memories."""
        allowed_tiers = [
            PrivacyTier.TIER_0_PUBLIC.value,
            PrivacyTier.TIER_1_PROJECT.value,
            PrivacyTier.TIER_2_PRIVATE.value,
        ] if tier_limit is None else [tier_limit.value]

        placeholders = ",".join("?" for _ in allowed_tiers)
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"SELECT * FROM memories WHERE tier IN ({placeholders}) ORDER BY timestamp DESC LIMIT ?",
                (*allowed_tiers, limit),
            )
            return [self._row_to_entry(r) for r in cursor.fetchall()]

    def sync_from_vault(self, vault: MemoryVault) -> int:
        """Syncs all facts and profile markdown files from the vault into SQLite cache."""
        count = 0
        all_facts = vault.load_facts()
        for fact in all_facts:
            self.upsert_entry(fact)
            count += 1

        # Index profile documents as section entries
        for tier in [PrivacyTier.TIER_0_PUBLIC, PrivacyTier.TIER_2_PRIVATE]:
            for doc_name in vault.list_docs(tier=tier):
                content = vault.read_doc(doc_name, tier=tier)
                if not content:
                    continue
                # Split markdown into sections by headers
                sections = self._parse_markdown_sections(doc_name, content, tier)
                for sec_entry in sections:
                    self.upsert_entry(sec_entry, doc_path=doc_name)
                    count += 1

        return count

    def get_stats(self) -> Dict[str, Any]:
        """Returns database statistics."""
        with self._get_connection() as conn:
            total = conn.execute("SELECT COUNT(*) as c FROM memories").fetchone()["c"]
            by_tier = {}
            for row in conn.execute("SELECT tier, COUNT(*) as c FROM memories GROUP BY tier"):
                by_tier[row["tier"]] = row["c"]
            by_cat = {}
            for row in conn.execute("SELECT category, COUNT(*) as c FROM memories GROUP BY category"):
                by_cat[row["category"]] = row["c"]
        return {"total_memories": total, "by_tier": by_tier, "by_category": by_cat}

    def _row_to_entry(self, row: sqlite3.Row) -> MemoryEntry:
        return MemoryEntry(
            id=row["id"],
            text=row["text"],
            category=row["category"],
            tier=PrivacyTier.from_str(row["tier"]),
            source=row["source"],
            timestamp=row["timestamp"],
            uses=row["uses"],
            pinned=bool(row["pinned"]),
        )

    def _parse_markdown_sections(self, doc_name: str, content: str, tier: PrivacyTier) -> List[MemoryEntry]:
        entries: List[MemoryEntry] = []
        lines = content.splitlines()
        current_title = Path(doc_name).stem
        current_lines: List[str] = []

        for line in lines:
            if line.startswith("#"):
                if current_lines:
                    text_chunk = "\n".join(current_lines).strip()
                    if text_chunk:
                        entry_id = hashlib.sha256(f"{doc_name}:{current_title}:{text_chunk}".encode()).hexdigest()[:16]
                        entries.append(MemoryEntry(
                            id=f"doc_{entry_id}",
                            text=f"[{current_title}] {text_chunk}",
                            category=Path(doc_name).stem,
                            tier=tier,
                            source=f"doc:{doc_name}",
                            pinned=True,
                        ))
                    current_lines = []
                current_title = line.lstrip("#").strip()
            else:
                current_lines.append(line)

        if current_lines:
            text_chunk = "\n".join(current_lines).strip()
            if text_chunk:
                entry_id = hashlib.sha256(f"{doc_name}:{current_title}:{text_chunk}".encode()).hexdigest()[:16]
                entries.append(MemoryEntry(
                    id=f"doc_{entry_id}",
                    text=f"[{current_title}] {text_chunk}",
                    category=Path(doc_name).stem,
                    tier=tier,
                    source=f"doc:{doc_name}",
                    pinned=True,
                ))

        return entries
