"""Laya Safety Guard & Continual Memory Panel for Telemachus Cockpit.

Combines real-time file safety boundaries, trash recovery metrics,
and the Odysseus continual learning memory vault feed.
"""
from __future__ import annotations

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple

from telemachus.cockpit.email_panel import demo_mode
from telemachus.config.loader import find_project_root
from telemachus.fs import guard_log
from telemachus.memory.cache import MemoryCache
from telemachus.memory.models import MemoryEntry, PrivacyTier
from telemachus.memory.vault import DEFAULT_VAULT_ROOT, MemoryVault

PROTECTED_PATHS = ["~/.ssh", "~/.gnupg", ".env", "/System"]
TIER_KEYS = {"private": PrivacyTier.TIER_2_PRIVATE, "project": PrivacyTier.TIER_1_PROJECT, "public": PrivacyTier.TIER_0_PUBLIC}


def tier_word(tier: Any) -> str:
    """private, project or public."""
    value = getattr(tier, "value", tier) or ""
    if "public" in value:
        return "public"
    if "project" in value:
        return "project"
    return "private"


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def fact_row(entry: MemoryEntry) -> Dict[str, Any]:
    """A memory entry as a plain dict for the cockpit views."""
    meta = entry.metadata or {}
    return {
        "id": entry.id,
        "text": " ".join((entry.text or "").split()),
        "category": entry.category or "fact",
        "tier": tier_word(entry.tier),
        "source": entry.source or "",
        "ts": int(guard_log.clean_ts(entry.timestamp) or 0),  # 0 = unknown (bad or missing time)
        "uses": _int(entry.uses),
        "pinned": bool(entry.pinned),
        "about": meta.get("about") or "",
        "quote": " ".join(str(meta.get("quote") or "").split()),
        "kind": meta.get("kind") or "",
    }


def laya_worker_found() -> bool:
    """True when the Laya intent-check worker can start (same paths LayaGuard uses).

    Reads LAYA_PYTHON from the environment, then from the project .env (that one key only;
    nothing is copied into the environment).
    """
    py = os.environ.get("LAYA_PYTHON")
    if not py:
        try:
            env_file = find_project_root() / ".env"
            if env_file.is_file():
                with open(env_file, "r", encoding="utf-8") as f:
                    for line in f:
                        key, sep, val = line.strip().partition("=")
                        if sep and key.strip() == "LAYA_PYTHON":
                            py = val.strip().strip("'\"")
                            break
        except Exception:
            py = None
    py = py or str(Path.home() / "laya/.venv/bin/python")
    worker = Path(guard_log.__file__).resolve().parent / "laya_worker.py"
    return Path(py).expanduser().is_file() and worker.is_file()

logger = logging.getLogger(__name__)


class SafetyMemoryPanel:
    """Provides safety boundaries, trash metrics, and memory vault event feeds."""

    def __init__(self, vault_root: Optional[Path] = None, audit_log: Optional[Path] = None,
                 guard_events: Optional[Path] = None):
        self.vault = MemoryVault(vault_root or DEFAULT_VAULT_ROOT)
        # None = guard_log.log_path() at read time (~/.memory/guard_events.jsonl or TELEMACHUS_GUARD_LOG)
        self.guard_events = Path(guard_events) if guard_events else None
        self.cache = MemoryCache(vault=self.vault)
        if audit_log is None and demo_mode():
            # demo or fake HOME: the real trash audit names real files; use one under HOME
            self.audit_log = Path.home() / ".telemachus" / "trash_audit.jsonl"
        elif audit_log is None:
            self.audit_log = find_project_root() / "data" / "trash_audit.jsonl"
        else:
            self.audit_log = Path(audit_log)

    def _trash_audit(self) -> Tuple[int, str]:
        """(all-time line count, name of the last item) of the trash audit log.

        Re-read only when the file's size or time changed (the cockpit refreshes every 10 s).
        The count is all-time; the cockpit shows the guard log's count for today instead.
        """
        try:
            st = self.audit_log.stat()
        except OSError:
            return 0, "None"
        stamp = (st.st_mtime, st.st_size)
        cached = getattr(self, "_trash_cache", None)
        if cached and cached[0] == stamp:
            return cached[1]
        count, last_item = 0, "None"
        try:
            last_raw = ""
            with open(self.audit_log, "r", encoding="utf-8") as f:
                for ln in f:
                    if ln.strip():
                        count += 1
                        last_raw = ln
            if last_raw:
                orig = json.loads(last_raw).get("original_path", "")
                last_item = Path(orig).name if orig else "item"
        except Exception as e:
            logger.debug(f"Failed reading trash audit log: {e}")
        self._trash_cache = (stamp, (count, last_item))
        return count, last_item

    def get_guard_status(self) -> Dict[str, Any]:
        """Returns Laya Egress Guard status and protected boundaries."""
        trash_count, last_trash_item = self._trash_audit()

        log = guard_log.summary(path=self.guard_events)
        return {
            "protected_paths": list(PROTECTED_PATHS),
            "trash_substitutions": trash_count,  # all-time; views show log["trash_today"]
            "trash_audit_found": self.audit_log.exists(),
            "last_trash_item": last_trash_item,
            # Only a time the guard measured (intent check by the Laya worker); None = hide it.
            "router_latency_ms": log.get("last_intent_latency_ms"),
            "egress_guard_mode": "ACTIVE (Strict)",
            "intent_available": laya_worker_found(),
            "log": log,
            "log_found": bool(log.get("total")),
            "events": guard_log.read_recent(200, path=self.guard_events),
        }

    def get_memory_stats(self) -> Dict[str, Any]:
        """Queries Unified Memory Vault stats across privacy tiers."""
        try:
            stats = dict(self.cache.get_stats())
            stats.setdefault("available", True)
            return stats
        except Exception:
            # Vault unreadable: report that instead of made-up counts.
            return {"available": False, "total_memories": 0, "by_tier": {}, "by_category": {}}

    def get_recent_events(self, limit: int = 8) -> List[Dict[str, Any]]:
        """Returns the latest memory extraction & ingestion events."""
        events_file = self.vault.events_file
        if not events_file.exists():
            return []

        events = []
        try:
            with open(events_file, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
                for raw in lines[-limit:]:
                    try:
                        data = json.loads(raw)
                        ts = data.get("timestamp", 0)
                        event_type = data.get("event", "EVENT")
                        details = data.get("details", {})
                        time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "--:--"

                        tier = details.get("tier", "private")
                        if "public" in tier:
                            badge = "PUB"
                            color = "\033[1;32m"  # Green
                        elif "project" in tier:
                            badge = "PRJ"
                            color = "\033[1;34m"  # Blue
                        else:
                            badge = "PRV"
                            color = "\033[1;35m"  # Magenta

                        cat = details.get("category", "")
                        desc = details.get("path") or details.get("id") or event_type
                        if len(desc) > 20:
                            desc = desc[:18] + ".."

                        events.append({
                            "time": time_str,
                            "type": event_type,
                            "badge": badge,
                            "color": color,
                            "category": cat,
                            "desc": desc,
                        })
                    except Exception:
                        continue
        except Exception as e:
            logger.debug(f"Failed to read memory events log: {e}")

        events.reverse()
        return events

    # ----- Facts for the Home and Memory tabs (read-only) --------------------------------

    def get_recent_facts(self, limit: int = 200, tier: Optional[str] = None) -> List[Dict[str, Any]]:
        """Newest facts first. tier: None (all), private, project or public."""
        try:
            entries = self.cache.get_recent(limit=limit, tier_limit=TIER_KEYS.get(tier or ""))
        except Exception as e:
            logger.debug(f"Recent facts failed: {e}")
            return []
        return [fact_row(e) for e in entries]

    def search_facts(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Full-text search over every fact (the cache's FTS index), best match first."""
        if not query.strip():
            return []
        try:
            return [fact_row(e) for e in self.cache.search(query, limit=limit)]
        except Exception as e:
            logger.debug(f"Fact search failed: {e}")
            return []

    def get_pending_count(self) -> Optional[int]:
        """Facts waiting for review (python -m telemachus.memory review). None when unreadable."""
        try:
            return len(self.vault.load_pending())
        except Exception:
            return None
