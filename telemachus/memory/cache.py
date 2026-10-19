"""High-speed SQLite Cache & Search Index with WAL mode & FTS5 full-text search."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, List, Optional

from telemachus.memory.models import MemoryEntry, PrivacyTier
from telemachus.memory.vault import DEFAULT_VAULT_ROOT, MemoryVault

logger = logging.getLogger(__name__)

# Question words and fillers that match almost every memory and add only noise to BM25.
SEARCH_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "about", "at", "by",
    "is", "are", "was", "were", "be", "do", "does", "did", "can", "should", "would", "will",
    "what", "who", "whom", "which", "when", "where", "why", "how",
    "i", "me", "my", "you", "your", "he", "she", "they", "them", "it", "this", "that",
    "tell", "know", "any", "some", "am",
    # Korean question words and fillers.
    "것", "뭐", "무엇", "어떤", "누구", "언제", "어디", "어떻게", "왜", "나", "내", "저", "제",
}

# Korean particles at the end of a word ("김민재가", "메일을"). One is removed from a
# query word when two or more syllables remain, so the stem matches stored text.
_KO_PARTICLES = sorted(
    ["에게서", "에서", "에게", "한테", "으로", "이랑", "까지", "부터", "처럼", "보다",
     "은", "는", "이", "가", "을", "를", "의", "에", "께", "와", "과", "도", "로", "랑", "만"],
    key=len, reverse=True,
)
_HANGUL = re.compile(r"[\uac00-\ud7a3]")


def _strip_particle(tok: str) -> str:
    for p in _KO_PARTICLES:
        if tok.endswith(p) and len(tok) - len(p) >= 2:
            return tok[: -len(p)]
    return tok


def build_fts_query(query: str) -> str:
    """Turns a natural-language query into an FTS5 OR query of quoted prefix terms.

    A raw sentence fails in FTS5: every word must match (implicit AND) and punctuation
    such as '?' is a syntax error. Each kept word becomes "word"*, joined with OR, so
    BM25 ranks memories by how many query words they share. A trailing plural 's' is
    dropped so "requests" also finds "request".
    """
    # One-character tokens ("s" from "Jordan's") would prefix-match almost every word.
    tokens = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1]
    kept = [t for t in tokens if t not in SEARCH_STOPWORDS] or tokens
    terms: List[str] = []
    for tok in kept:
        if _HANGUL.search(tok):
            tok = _strip_particle(tok)
        elif len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        # A light stem: "preference" -> "prefer" finds "prefers", "delivery" -> "delive"
        # finds "deliver". Hangul words are short already and keep their form.
        if len(tok) > 6 and not re.search(r"[\uac00-\ud7a3]", tok):
            tok = tok[:6]
        if tok not in terms:
            terms.append(tok)
    return " OR ".join(f'"{t}"*' for t in terms)


def query_terms(query: str) -> List[str]:
    """The search terms that build_fts_query uses, without FTS syntax."""
    fts = build_fts_query(query)
    return [part.strip().strip('*').strip('"') for part in fts.split(" OR ")] if fts else []


def _term_matches(term: str, word: str) -> bool:
    """A query term matches a word of the text.

    Short Latin terms (4 letters or fewer) must be the whole word or its plural, so
    "ana" does not match "analysis". Longer terms are stems (build_fts_query cuts them
    to 6 letters) and match as a prefix. Hangul terms match as a prefix in either
    direction, because particles change the end of the word.
    """
    if _HANGUL.search(term):
        return word.startswith(term) or (len(word) >= 2 and term.startswith(word))
    if len(term) <= 4:
        return word == term or word == term + "s" or word == term + "es"
    return word.startswith(term)


def term_hits(terms: List[str], text: str) -> int:
    """Number of query terms that match a word in text."""
    words = re.findall(r"\w+", text.lower())
    return sum(1 for t in terms if any(_term_matches(t, w) for w in words))


def _fts_text(entry: MemoryEntry) -> str:
    meta = entry.metadata or {}
    parts = [entry.text, meta.get("quote") or "", meta.get("about") or ""]
    return "\n".join(p for p in parts if p)


class MemoryCache:
    """Manages the SQLite cache and FTS5 search index."""

    def __init__(self, db_path: Optional[Path | str] = None, vault: Optional[MemoryVault] = None):
        if db_path:
            self.db_path = Path(db_path).resolve()
        elif vault:
            self.db_path = vault.private_dir / ".cache" / "index.sqlite3"
        else:
            self.db_path = DEFAULT_VAULT_ROOT / "private" / ".cache" / "index.sqlite3"

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
                content_hash TEXT,
                metadata_json TEXT
            );
            """)
            # Older caches were created without metadata_json (source, quote, kind).
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(memories)")}
            if "metadata_json" not in cols:
                try:
                    conn.execute("ALTER TABLE memories ADD COLUMN metadata_json TEXT;")
                except sqlite3.OperationalError as e:
                    # Another process (MCP server or proxy) added it at the same moment.
                    if "duplicate column" not in str(e).lower():
                        raise
            # Turns and documents that the intake already processed (dedup across requests).
            conn.execute("""
            CREATE TABLE IF NOT EXISTS intake_seen (
                hash TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                timestamp INTEGER NOT NULL
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
        metadata_json = json.dumps(entry.metadata or {}, ensure_ascii=False, sort_keys=True)
        content_hash = hashlib.sha256(
            f"{entry.id}:{entry.text}:{entry.category}:{entry.tier.value}:{metadata_json}".encode()
        ).hexdigest()

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
            INSERT INTO memories (id, text, category, tier, source, timestamp, uses, pinned, doc_path, content_hash, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                text = excluded.text,
                category = excluded.category,
                tier = excluded.tier,
                source = excluded.source,
                timestamp = excluded.timestamp,
                uses = MAX(memories.uses, excluded.uses),
                pinned = excluded.pinned,
                doc_path = excluded.doc_path,
                content_hash = excluded.content_hash,
                metadata_json = excluded.metadata_json;
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
                metadata_json,
            ))

            # Update FTS5
            conn.execute("DELETE FROM memories_fts WHERE id = ?", (entry.id,))
            conn.execute(
                "INSERT INTO memories_fts (id, text, category) VALUES (?, ?, ?)",
                # The quote and the "about" name are searchable too: a Korean quote under an
                # English fact, or a person's name, must still find the fact.
                (entry.id, _fts_text(entry), entry.category),
            )

    def search(
        self,
        query: str,
        tier_limit: Optional[PrivacyTier] = None,
        limit: int = 10,
    ) -> List[MemoryEntry]:
        """Performs full-text keyword search across memories, respecting privacy bounds."""
        clean_query = query.strip()
        fts_query = build_fts_query(clean_query)
        if not fts_query:
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
                cursor = conn.execute(sql, (fts_query, *allowed_tiers, limit))
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
        all_facts = vault.load_facts() + vault.load_document_facts()
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

    def mark_seen(self, content_hash: str, kind: str) -> bool:
        """Records a processed turn or document. Returns False if it was already recorded."""
        import time as _time
        with self._get_connection() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO intake_seen (hash, kind, timestamp) VALUES (?, ?, ?)",
                (content_hash, kind, int(_time.time())),
            )
            return cur.rowcount == 1

    def unmark_seen(self, content_hash: str) -> None:
        """Forgets a processed hash, so that a failed turn can run again."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM intake_seen WHERE hash = ?", (content_hash,))

    def get_by_id(self, entry_id: str) -> Optional[MemoryEntry]:
        """Returns one memory by full id or a unique id prefix (at least 6 characters)."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (entry_id,)).fetchone()
            if row is None and len(entry_id) >= 6:
                rows = conn.execute("SELECT * FROM memories WHERE id LIKE ?", (entry_id + "%",)).fetchall()
                row = rows[0] if len(rows) == 1 else None
        return self._row_to_entry(row) if row else None

    def _row_to_entry(self, row: sqlite3.Row) -> MemoryEntry:
        try:
            metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        except Exception:
            metadata = {}
        return MemoryEntry(
            id=row["id"],
            text=row["text"],
            category=row["category"],
            tier=PrivacyTier.from_str(row["tier"]),
            source=row["source"],
            timestamp=row["timestamp"],
            uses=row["uses"],
            pinned=bool(row["pinned"]),
            metadata=metadata,
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
