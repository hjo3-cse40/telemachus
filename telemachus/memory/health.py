"""Loud failure reporting for the memory pipeline.

Every component (LM Studio extraction, Laya gate, quote verification) reports here.
A failure is written to three places so that it cannot pass without notice:
1. ``private/.cache/health.json``: last error and failure counters per component.
2. ``events.jsonl``: a ``MEMORY_FAILURE`` event, which the cockpit memory feed shows.
3. The logger at WARNING level (the proxy log), not DEBUG.
``memory_status`` and ``python -m telemachus.memory health`` read the health file.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from typing import Any, Dict, Optional

from telemachus.memory.vault import MemoryVault

logger = logging.getLogger("telemachus.memory.health")

# A component counts as degraded while its most recent result is a failure.
COMPONENTS = ("extraction_llm", "laya_gate", "quote_check", "document_extraction")


class MemoryHealth:
    """Reads and writes the per-component health file."""

    def __init__(self, vault: MemoryVault):
        self.vault = vault
        self.path = vault.private_dir / ".cache" / "health.json"

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _update(self, component: str, fn) -> Dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".lock")
        with open(lock_path, "a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = self._load()
                state = data.get(component, {
                    "ok": True,
                    "consecutive_failures": 0,
                    "total_failures": 0,
                    "total_successes": 0,
                    "last_error": None,
                    "last_error_at": None,
                    "last_ok_at": None,
                })
                fn(state)
                data[component] = state
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                os.replace(tmp, self.path)
                return state
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def ok(self, component: str) -> None:
        def apply(state):
            state["ok"] = True
            state["consecutive_failures"] = 0
            state["total_successes"] = state.get("total_successes", 0) + 1
            state["last_ok_at"] = int(time.time())
        self._update(component, apply)

    def fail(self, component: str, error: str, details: Optional[Dict[str, Any]] = None) -> None:
        def apply(state):
            state["ok"] = False
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            state["total_failures"] = state.get("total_failures", 0) + 1
            state["last_error"] = error[:500]
            state["last_error_at"] = int(time.time())
        state = self._update(component, apply)
        payload = {"component": component, "error": error[:500],
                   "consecutive_failures": state["consecutive_failures"]}
        if details:
            payload.update(details)
        self.vault.log_event("MEMORY_FAILURE", payload)
        logger.warning(f"[memory] {component} FAILED ({state['consecutive_failures']}x in a row): {error[:300]}")

    def note(self, component: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        """Counts a rejected candidate (for example a quote that is not in the source).

        This is the pipeline doing its job, not a broken component, so it does not set
        DEGRADED. It is still logged at WARNING and written to events.jsonl.
        """
        def apply(state):
            state["total_rejections"] = state.get("total_rejections", 0) + 1
            state["last_rejection"] = message[:300]
            state["last_rejection_at"] = int(time.time())
        self._update(component, apply)
        payload = {"component": component, "message": message[:300]}
        if details:
            payload.update(details)
        self.vault.log_event("CANDIDATE_REJECTED", payload)
        logger.warning(f"[memory] {component}: {message[:300]}")

    def summary(self) -> Dict[str, Any]:
        data = self._load()
        degraded = {k: v for k, v in data.items() if not v.get("ok", True)}
        return {
            "status": "DEGRADED" if degraded else "OK",
            "degraded_components": {
                k: {"last_error": v.get("last_error"), "consecutive_failures": v.get("consecutive_failures"),
                    "last_error_at": v.get("last_error_at")}
                for k, v in degraded.items()
            },
            "components": data,
        }
