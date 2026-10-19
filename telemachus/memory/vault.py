"""Canonical Filesystem Memory Vault.

Manages physical directories, strict OS-level permissions (0700 on private),
advisory file locking, atomic writes, and human-readable Markdown/JSONL storage.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional

from telemachus.memory.models import MemoryEntry, PrivacyTier

logger = logging.getLogger(__name__)

DEFAULT_VAULT_ROOT = Path.home() / ".memory"


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

    def _read_jsonl_lines(self, file_path: Path) -> List[str]:
        if not file_path.exists():
            return []
        with open(file_path, "r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return f.readlines()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
