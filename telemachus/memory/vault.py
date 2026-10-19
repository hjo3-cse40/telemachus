"""Canonical Filesystem Memory Vault.

Manages physical directories, strict OS-level permissions (0700 on private),
advisory file locking, atomic writes, and human-readable Markdown/JSONL storage.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
from pathlib import Path
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from telemachus.memory.models import MemoryEntry, PrivacyTier

logger = logging.getLogger(__name__)

# TELEMACHUS_MEMORY_DIR moves the vault (tests, or a separate work vault).
DEFAULT_VAULT_ROOT = Path(os.environ.get("TELEMACHUS_MEMORY_DIR") or (Path.home() / ".memory")).expanduser()


class MemoryVault:
    """Manages the canonical on-disk memory vault."""

    def __init__(self, root_dir: Optional[Path | str] = None):
        self.root_dir = Path(root_dir).expanduser().resolve() if root_dir else DEFAULT_VAULT_ROOT
        self.private_dir = self.root_dir / "private"
        self.public_dir = self.root_dir / "public_projected"
        self.projects_dir = self.root_dir / "projects"
        self.events_file = self.root_dir / "events.jsonl"
        self.private_facts_file = self.private_dir / "facts" / "facts.jsonl"
        self.public_facts_file = self.public_dir / "facts" / "facts.jsonl"
        # Facts that documents and pasted text state about other people. Never part of the user profile.
        self.document_facts_file = self.private_dir / "documents" / "facts.jsonl"
        # Candidates that need a human decision before they become facts.
        self.review_dir = self.private_dir / "review"
        self.pending_file = self.review_dir / "pending.jsonl"
        self.decisions_file = self.review_dir / "decisions.jsonl"
        self.failed_turns_file = self.review_dir / "failed_turns.jsonl"

    def initialize(self) -> None:
        """Creates vault directories and sets air-gapped permissions."""
        self.root_dir.mkdir(parents=True, exist_ok=True)

        # Private tree: strictly chmod 0700 (owner read/write/execute only)
        self.private_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.private_dir, 0o700)
        except OSError as e:
            logger.warning(f"Could not set 0700 permissions on {self.private_dir}: {e}")

        (self.private_dir / "profile").mkdir(parents=True, exist_ok=True)
        (self.private_dir / "facts").mkdir(parents=True, exist_ok=True)
        (self.private_dir / ".cache").mkdir(parents=True, exist_ok=True)
        (self.private_dir / "documents").mkdir(parents=True, exist_ok=True)
        self.review_dir.mkdir(parents=True, exist_ok=True)

        # Public projected tree
        self.public_dir.mkdir(parents=True, exist_ok=True)
        (self.public_dir / "facts").mkdir(parents=True, exist_ok=True)
        (self.public_dir / "rules").mkdir(parents=True, exist_ok=True)

        # Projects tree
        self.projects_dir.mkdir(parents=True, exist_ok=True)

        # Ensure events log exists
        if not self.events_file.exists():
            self.events_file.touch(mode=0o600, exist_ok=True)

    def log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        """Appends an event to the append-only events.jsonl audit trail."""
        payload = {
            "timestamp": int(time.time()),
            "event": event_type,
            "details": details,
        }
        self._append_jsonl(self.events_file, payload)

    def append_fact(self, entry: MemoryEntry) -> None:
        """Appends an atomic fact to either private or public facts.jsonl based on tier."""
        target_file = (
            self.public_facts_file
            if entry.tier == PrivacyTier.TIER_0_PUBLIC
            else self.private_facts_file
        )
        target_file.parent.mkdir(parents=True, exist_ok=True)
        self._append_jsonl(target_file, entry.to_dict())
        self.log_event("FACT_APPENDED", {"id": entry.id, "tier": entry.tier.value, "category": entry.category})

    def load_facts(self, tier: Optional[PrivacyTier] = None) -> List[MemoryEntry]:
        """Loads facts from JSONL files, optionally filtered by tier."""
        entries: List[MemoryEntry] = []

        files_to_read = []
        if tier is None or tier != PrivacyTier.TIER_0_PUBLIC:
            if self.private_facts_file.exists():
                files_to_read.append(self.private_facts_file)

        if tier is None or tier == PrivacyTier.TIER_0_PUBLIC or tier == PrivacyTier.TIER_1_PROJECT:
            if self.public_facts_file.exists():
                files_to_read.append(self.public_facts_file)

        for path in files_to_read:
            for line in self._read_jsonl_lines(path):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    entry = MemoryEntry.from_dict(data)
                    if tier is None or entry.tier == tier:
                        entries.append(entry)
                except Exception as e:
                    logger.warning(f"Malformed JSONL line in {path}: {e}")

        return entries

    def append_document_fact(self, entry: MemoryEntry) -> None:
        """Appends a fact that a document states. Always private, kept apart from the user profile."""
        entry.tier = PrivacyTier.TIER_2_PRIVATE
        entry.metadata.setdefault("kind", "document")
        self._append_jsonl(self.document_facts_file, entry.to_dict())
        self.log_event("DOCUMENT_FACT_APPENDED", {
            "id": entry.id, "about": entry.metadata.get("about"), "source": entry.source,
        })

    def load_document_facts(self) -> List[MemoryEntry]:
        return self._load_entries(self.document_facts_file)

    # ----- Review queue -------------------------------------------------------------------
    # The proxy thread appends while the CLI (another process) rewrites. Every append and
    # every read-modify-replace of these files holds one lock file, so none is lost.

    @contextlib.contextmanager
    def _review_lock(self):
        self.review_dir.mkdir(parents=True, exist_ok=True)
        with open(self.review_dir / ".lock", "a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def append_pending(self, entry: MemoryEntry, reason: str) -> None:
        """Puts a candidate fact in the review queue instead of memory."""
        entry.metadata["review_reason"] = reason
        with self._review_lock():
            self._append_jsonl(self.pending_file, entry.to_dict())
        self.log_event("REVIEW_QUEUED", {"id": entry.id, "text": entry.text, "reason": reason})

    def load_pending(self) -> List[MemoryEntry]:
        return self._load_entries(self.pending_file)

    def find_pending(self, entry_id: str) -> Tuple[Optional[MemoryEntry], str]:
        """Exact id, or a unique prefix of at least 6 characters. Returns (entry, error)."""
        entry_id = (entry_id or "").strip()
        pending = self.load_pending()
        exact = [e for e in pending if e.id == entry_id]
        if exact:
            return exact[0], ""
        if len(entry_id) < 6:
            return None, "Give at least 6 characters of the id."
        matches = [e for e in pending if e.id.startswith(entry_id)]
        if len(matches) > 1:
            return None, f"{len(matches)} pending facts start with {entry_id!r}; give more characters."
        return (matches[0], "") if matches else (None, f"No pending fact with id {entry_id!r}.")

    def resolve_pending(self, entry_id: str, approve: bool) -> Optional[MemoryEntry]:
        """Approves (moves to memory) or rejects one pending candidate. Logs the decision.

        Decisions are kept in decisions.jsonl: they are labelled examples for a later
        Laya calibration or fine-tune on the user's own data.
        """
        with self._review_lock():
            match, _ = self.find_pending(entry_id)
            if match is None:
                return None
            remaining = [e for e in self.load_pending() if e.id != match.id]
            self._rewrite_jsonl(self.pending_file, [e.to_dict() for e in remaining])
        decision = {"id": match.id, "approved": approve, "entry": match.to_dict(), "timestamp": int(time.time())}
        self._append_jsonl(self.decisions_file, decision)
        if approve:
            match.metadata["reviewed"] = True
            if match.metadata.get("kind") == "document":
                self.append_document_fact(match)
            else:
                self.append_fact(match)
        self.log_event("REVIEW_RESOLVED", {"id": match.id, "approved": approve})
        return match

    # ----- Source texts (so that a cited fact can be read in context) ----------------------

    def save_source(self, key: str, text: str, label: str) -> None:
        """Stores the text a fact came from: the user's own words or a document."""
        path = self.private_dir / "documents" / "sources" / f"{key}.json"
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"label": label, "text": text, "timestamp": int(time.time())}, ensure_ascii=False))
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def read_source(self, key: str) -> Optional[Dict[str, Any]]:
        if not key or not re.fullmatch(r"[0-9a-f]{16,64}", key):
            return None
        path = self.private_dir / "documents" / "sources" / f"{key}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def append_failed_turn(self, turn_hash: str, user_text: str, error: str,
                           kind: str = "turn", label: str = "") -> None:
        """Keeps a turn (or a document) that the pipeline could not process, for a retry."""
        with self._review_lock():
            self._append_jsonl(self.failed_turns_file, {
                "record_id": uuid.uuid4().hex, "turn_hash": turn_hash, "user_text": user_text,
                "error": error[:500], "kind": kind, "label": label, "timestamp": int(time.time()),
            })

    def load_failed_turns(self) -> List[Dict[str, Any]]:
        out = []
        for line in self._read_jsonl_lines(self.failed_turns_file):
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def clear_failed_records(self, record_ids: List[Optional[str]]) -> None:
        """Removes failed records by record_id (a retry removes only what it ran)."""
        drop = {r for r in record_ids if r}
        with self._review_lock():
            keep = [t for t in self.load_failed_turns() if t.get("record_id") not in drop]
            self._rewrite_jsonl(self.failed_turns_file, keep)

    def clear_failed_turns(self, turn_hashes: List[str]) -> None:
        drop = set(turn_hashes)
        with self._review_lock():
            keep = [t for t in self.load_failed_turns() if t.get("turn_hash") not in drop]
            self._rewrite_jsonl(self.failed_turns_file, keep)

    def _load_entries(self, path: Path) -> List[MemoryEntry]:
        entries = []
        for line in self._read_jsonl_lines(path):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(MemoryEntry.from_dict(json.loads(line)))
            except Exception as e:
                logger.warning(f"Malformed JSONL line in {path}: {e}")
        return entries

    def write_doc(
        self,
        rel_path: str,
        content: str,
        tier: PrivacyTier = PrivacyTier.TIER_2_PRIVATE,
    ) -> Path:
        """Atomically writes a Markdown document to the appropriate tier folder."""
        base_dir = self.public_dir if tier == PrivacyTier.TIER_0_PUBLIC else self.private_dir
        dest_path = (base_dir / rel_path).resolve()

        # Security check: ensure path stays within base directory
        if not str(dest_path).startswith(str(base_dir)):
            raise ValueError(f"Path traversal detected: {rel_path} outside {base_dir}")

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.with_suffix(".tmp")

        with open(tmp_path, "w", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

        os.replace(tmp_path, dest_path)
        if tier == PrivacyTier.TIER_2_PRIVATE:
            try:
                os.chmod(dest_path, 0o600)
            except OSError:
                pass

        self.log_event("DOC_WRITTEN", {"path": rel_path, "tier": tier.value})
        return dest_path

    def read_doc(
        self,
        rel_path: str,
        tier: PrivacyTier = PrivacyTier.TIER_2_PRIVATE,
    ) -> Optional[str]:
        """Reads a document from the vault."""
        base_dir = self.public_dir if tier == PrivacyTier.TIER_0_PUBLIC else self.private_dir
        dest_path = (base_dir / rel_path).resolve()
        if not dest_path.exists():
            return None

        with open(dest_path, "r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return f.read()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def list_docs(self, tier: PrivacyTier = PrivacyTier.TIER_2_PRIVATE) -> List[str]:
        """Lists relative document paths within a tier directory."""
        base_dir = self.public_dir if tier == PrivacyTier.TIER_0_PUBLIC else self.private_dir
        if not base_dir.exists():
            return []
        results = []
        for p in base_dir.rglob("*.md"):
            if p.is_file():
                results.append(str(p.relative_to(base_dir)))
        return sorted(results)

    # Internal safe file helpers
    def _append_jsonl(self, file_path: Path, data: Dict[str, Any]) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(data, ensure_ascii=False) + "\n"
        with open(file_path, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _rewrite_jsonl(self, file_path: Path, rows: List[Dict[str, Any]]) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        # A unique temp name: two writers never share one temp file.
        fd, tmp = tempfile.mkstemp(dir=file_path.parent, prefix=file_path.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, file_path)

    def _read_jsonl_lines(self, file_path: Path) -> List[str]:
        if not file_path.exists():
            return []
        with open(file_path, "r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return f.readlines()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
