"""Data Importer for bootstrapping the unified memory vault from Odysseus and Cursor."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from telemachus.memory.cache import MemoryCache
from telemachus.memory.models import MemoryEntry, PrivacyTier
from telemachus.memory.vault import MemoryVault

logger = logging.getLogger(__name__)


class MemoryImporter:
    """Imports existing memories and rules into the canonical vault."""

    def __init__(self, vault: MemoryVault, cache: MemoryCache):
        self.vault = vault
        self.cache = cache

    def import_odysseus(self, memory_json_path: Path | str) -> int:
        """Imports memories from Odysseus data/memory.json."""
        path = Path(memory_json_path).expanduser().resolve()
        if not path.exists():
            logger.warning(f"Odysseus memory file not found: {path}")
            return 0

        with open(path, "r", encoding="utf-8") as f:
            raw_entries = json.load(f)

        count = 0
        for item in raw_entries:
            if not isinstance(item, dict):
                continue

            text = item.get("text", "").strip()
            if not text:
                continue

            category = item.get("category", "fact")
            # Determine privacy tier based on content
            tier = self._classify_odysseus_entry(text, category)

            entry = MemoryEntry(
                id=item.get("id"),
                text=text,
                category=category,
                tier=tier,
                source="odysseus",
                timestamp=item.get("timestamp", 0),
                uses=item.get("uses", 0),
                pinned=item.get("pinned", False),
                metadata={"owner": item.get("owner", ""), "session_id": item.get("session_id", "")},
            )

            self.vault.append_fact(entry)
            self.cache.upsert_entry(entry)
            count += 1

        self.vault.log_event("ODYSSEUS_IMPORTED", {"source_path": str(path), "imported_count": count})
        return count

    def import_cursor_rules(self, cursor_rules_dir: Path | str) -> int:
        """Imports rules from ~/.cursor/rules/*.mdc into the canonical vault."""
        path = Path(cursor_rules_dir).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            logger.warning(f"Cursor rules directory not found: {path}")
            return 0

        count = 0
        for mdc_file in path.glob("*.mdc"):
            content = mdc_file.read_text(encoding="utf-8")
            stem = mdc_file.stem

            if stem in ("no-em-dashes", "no-cursor-commit-attribution"):
                # Global public rules
                self.vault.write_doc(f"rules/{stem}.md", content, tier=PrivacyTier.TIER_0_PUBLIC)
                count += 1
            elif stem == "local-llm-setup":
                # Local hardware / LLM setup
                self.vault.write_doc("profile/hardware.md", content, tier=PrivacyTier.TIER_2_PRIVATE)
                count += 1
            elif stem == "job-search-resume":
                # Confidential career / resume tracking
                self.vault.write_doc("profile/career.md", content, tier=PrivacyTier.TIER_2_PRIVATE)
                count += 1
            elif stem == "airlock-harness":
                self.vault.write_doc("projects/airlock.md", content, tier=PrivacyTier.TIER_1_PROJECT)
                count += 1

        self.cache.sync_from_vault(self.vault)
        self.vault.log_event("CURSOR_RULES_IMPORTED", {"rules_dir": str(path), "imported_count": count})
        return count

    def _classify_odysseus_entry(self, text: str, category: str) -> PrivacyTier:
        """Classifies an Odysseus entry into Tier 0, 1, or 2."""
        text_lower = text.lower()

        # Sensitive indicators -> Tier 2
        private_terms = ["ucsc", "cmpm", "class", "school", "calendar", "locally", "no data leaves", "schedule", "rent", "bills"]
        if any(term in text_lower for term in private_terms):
            return PrivacyTier.TIER_2_PRIVATE

        if category == "project":
            return PrivacyTier.TIER_1_PROJECT

        return PrivacyTier.TIER_2_PRIVATE
