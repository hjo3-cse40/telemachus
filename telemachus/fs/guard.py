"""Unified Filesystem & Command Guardrail Engine.

Integrates:
- Tier 0: Deterministic boundaries & blast radius checks
- Tier 1: Laya ModernBERT Intent-Action alignment pass
- Tier 2: Soft recovery via macOS Trash
- Tier 3: Selective interactive elevation & Git checkpointing
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from telemachus.config.loader import load_env
from telemachus.fs.boundaries import (
    evaluate_tier0_command,
    evaluate_tier0_path,
    resolve_clean_path,
)
from telemachus.fs import guard_log
from telemachus.fs.checkpoint import create_git_checkpoint
from telemachus.fs.models import (
    FileActionType,
    GitCheckpointResult,
    GuardDecision,
    GuardVerdict,
    TierLevel,
    TrashResult,
)
from telemachus.fs.trash import move_to_trash


class WarmLayaFsBridge:
    """Persistent resident worker process for Laya Filesystem Guard on MPS."""

    def __init__(self, python_path: Optional[str] = None):
        load_env()
        self.python_path = python_path or os.environ.get(
            "LAYA_PYTHON", str(Path.home() / "laya/.venv/bin/python")
        )
        self.worker_script = Path(__file__).resolve().parent / "laya_worker.py"
        self.process: Optional[subprocess.Popen] = None
        self.lock = threading.RLock()

    def _ensure_running(self) -> bool:
        with self.lock:
            if self.process and self.process.poll() is None:
                return True
            if not Path(self.python_path).is_file() or not self.worker_script.is_file():
                return False

        try:
            self.process = subprocess.Popen(
                [self.python_path, str(self.worker_script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            while True:
                line = self.process.stderr.readline()
                if not line or "LAYA_FS_READY" in line:
                    break

            def _drain():
                while self.process and self.process.poll() is None:
                    try:
                        line = self.process.stderr.readline()
                        if not line:
                            break
                    except Exception:
                        break

            threading.Thread(target=_drain, daemon=True).start()
            return True
        except Exception:
            self.process = None
            return False

    def evaluate_record(
        self, action_type: str, target: str, user_intent: str, details: str = ""
    ) -> Optional[Dict[str, Any]]:
        with self.lock:
            if not self._ensure_running() or not self.process:
                return None
            try:
                payload = json.dumps(
                    {
                        "records": [
                            {
                                "action_type": action_type,
                                "target": target,
                                "user_intent": user_intent,
                                "details": details,
                            }
                        ]
                    }
                ) + "\n"
                assert self.process.stdin is not None
                assert self.process.stdout is not None
                self.process.stdin.write(payload)
                self.process.stdin.flush()
                out_line = self.process.stdout.readline()
                if not out_line:
                    return None
                data = json.loads(out_line)
                results = data.get("results", [])
                return results[0] if results else None
            except Exception:
                if self.process:
                    try:
                        self.process.kill()
                    except Exception:
                        pass
                    self.process = None
                return None

    def close(self):
        with self.lock:
            if self.process:
                try:
                    self.process.terminate()
                except Exception:
                    pass
                self.process = None


# Global warm bridge for fast repeated evaluation (<50ms)
_global_laya_fs_bridge: Optional[WarmLayaFsBridge] = None


def get_laya_fs_bridge() -> WarmLayaFsBridge:
    global _global_laya_fs_bridge
    if _global_laya_fs_bridge is None:
        _global_laya_fs_bridge = WarmLayaFsBridge()
    return _global_laya_fs_bridge


def _rule_name(decision: GuardDecision) -> str:
    """Short rule name for the guard event log."""
    if decision.verdict == GuardVerdict.NEEDS_CONFIRMATION:
        return guard_log.RULE_CONFIRM
    if decision.tier == TierLevel.TIER_0_BOUNDARY:
        return guard_log.RULE_COMMAND if decision.action_type == FileActionType.EXECUTE else guard_log.RULE_PATH
    if decision.tier == TierLevel.TIER_1_INTENT_ALIGNMENT:
        return guard_log.RULE_INTENT
    if decision.tier == TierLevel.TIER_2_SOFT_RECOVERY:
        return guard_log.RULE_SOFT_DELETE
    return guard_log.RULE_CONFIRM


def _log_decision(decision: GuardDecision, intent_ms: Optional[float]) -> None:
    """Writes one guard decision to the event log. Never raises."""
    try:
        kind = {
            GuardVerdict.BLOCK: "BLOCK",
            GuardVerdict.NEEDS_CONFIRMATION: "CONFIRM",
        }.get(decision.verdict, "ALLOW")
        guard_log.record(
            kind,
            target=decision.target,
            rule=_rule_name(decision),
            detail=decision.reason,
            latency_ms=intent_ms,
            action=decision.action_type.value,
        )
    except Exception:
        pass


class LayaGuard:
    """The central coordinator enforcing Tier 0 through Tier 3 guardrails."""

    def __init__(self, bridge: Optional[WarmLayaFsBridge] = None):
        self.bridge = bridge or get_laya_fs_bridge()

    def evaluate(
        self,
        action_type: FileActionType,
        target: str,
        user_intent: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> GuardDecision:
        """Evaluates any proposed file operation or shell command against the 4 tiers.

        Every decision is also written to the guard event log (telemachus.fs.guard_log),
        with the intent-check time when the Laya worker answered. Logging never raises.
        """
        timing: Dict[str, float] = {}
        decision = self._evaluate(action_type, target, user_intent, details, timing)
        _log_decision(decision, timing.get("intent_ms"))
        return decision

    def _evaluate(
        self,
        action_type: FileActionType,
        target: str,
        user_intent: Optional[str],
        details: Optional[Dict[str, Any]],
        timing: Dict[str, float],
    ) -> GuardDecision:
        details_map = details or {}
        is_write = action_type in (
            FileActionType.EDIT,
            FileActionType.WRITE,
            FileActionType.DELETE,
        )

        # -------------------------------------------------------------
        # TIER 0: Deterministic Boundary Checks (Zero Cost, Immediate)
        # -------------------------------------------------------------
        if action_type == FileActionType.EXECUTE:
            ok, reason = evaluate_tier0_command(target)
            if not ok:
                return GuardDecision(
                    verdict=GuardVerdict.BLOCK,
                    tier=TierLevel.TIER_0_BOUNDARY,
                    reason=reason,
                    action_type=action_type,
                    target=target,
                    confidence=1.0,
                    intent=user_intent,
                )
        else:
            # Path based actions
            ok, reason = evaluate_tier0_path(target, is_write=is_write)
            if not ok:
                return GuardDecision(
                    verdict=GuardVerdict.BLOCK,
                    tier=TierLevel.TIER_0_BOUNDARY,
                    reason=reason,
                    action_type=action_type,
                    target=target,
                    confidence=1.0,
                    intent=user_intent,
                )

        # Read-only actions that passed Tier 0 require no further friction
        if action_type in (
            FileActionType.LIST,
            FileActionType.READ,
            FileActionType.SEARCH,
            FileActionType.GREP,
            FileActionType.STAT,
            FileActionType.CHECKPOINT,
        ):
            return GuardDecision(
                verdict=GuardVerdict.PERMIT,
                tier=TierLevel.TIER_0_BOUNDARY,
                reason="Read-only operation verified safe by Tier 0 boundaries.",
                action_type=action_type,
                target=target,
                confidence=1.0,
                intent=user_intent,
            )

        # -------------------------------------------------------------
        # TIER 1: Laya ModernBERT Intent-Action Alignment Pass
        # -------------------------------------------------------------
        if user_intent and user_intent.strip():
            details_str = json.dumps(details_map)
            started = time.perf_counter()
            laya_eval = self.bridge.evaluate_record(
                action_type=action_type.value,
                target=target,
                user_intent=user_intent,
                details=details_str,
            )
            if laya_eval:
                timing["intent_ms"] = (time.perf_counter() - started) * 1000.0

            if laya_eval:
                alignment = laya_eval.get("intent_alignment")
                align_conf = float(laya_eval.get("alignment_confidence", 0.0))
                blast = laya_eval.get("blast_radius")
                risk = laya_eval.get("action_risk")

                # Low alignment or mismatched action
                if alignment == "unaligned_mismatch" and align_conf >= 0.5:
                    return GuardDecision(
                        verdict=GuardVerdict.BLOCK,
                        tier=TierLevel.TIER_1_INTENT_ALIGNMENT,
                        reason=(
                            f"Laya blocked action: Proposed {action_type.value} does not align "
                            f"with stated user request '{user_intent}' (confidence: {align_conf:.2f})."
                        ),
                        action_type=action_type,
                        target=target,
                        confidence=align_conf,
                        intent=user_intent,
                        details=laya_eval,
                    )

                # Excessive scope violation
                if blast == "excessive_scope" and align_conf >= 0.6:
                    return GuardDecision(
                        verdict=GuardVerdict.NEEDS_CONFIRMATION,
                        tier=TierLevel.TIER_3_ELEVATION,
                        reason=(
                            f"Laya flagged excessive scope: Target '{target}' appears broader "
                            f"than requested scope '{user_intent}'."
                        ),
                        action_type=action_type,
                        target=target,
                        confidence=align_conf,
                        intent=user_intent,
                        details=laya_eval,
                    )

                # Ambiguity or moderate confidence on destructive actions
                if risk == "destructive_loss" and align_conf < 0.8:
                    return GuardDecision(
                        verdict=GuardVerdict.NEEDS_CONFIRMATION,
                        tier=TierLevel.TIER_3_ELEVATION,
                        reason=(
                            f"Destructive deletion detected with ambiguous intent alignment "
                            f"({align_conf:.2f}). Elevation required."
                        ),
                        action_type=action_type,
                        target=target,
                        confidence=align_conf,
                        intent=user_intent,
                        details=laya_eval,
                    )

                # High alignment permits action with Tier 2/3 notes
                return GuardDecision(
                    verdict=GuardVerdict.PERMIT,
                    tier=TierLevel.TIER_1_INTENT_ALIGNMENT,
                    reason=f"Permitted by Laya: Intent match verified ({align_conf:.2f}).",
                    action_type=action_type,
                    target=target,
                    confidence=align_conf,
                    intent=user_intent,
                    details=laya_eval,
                )

        # Fallback heuristic if Laya worker or user_intent is omitted
        if action_type == FileActionType.DELETE:
            return GuardDecision(
                verdict=GuardVerdict.PERMIT,
                tier=TierLevel.TIER_2_SOFT_RECOVERY,
                reason="Permitted under Tier 2: Deletion will be routed through macOS Trash.",
                action_type=action_type,
                target=target,
                confidence=0.8,
                intent=user_intent,
            )

        return GuardDecision(
            verdict=GuardVerdict.PERMIT,
            tier=TierLevel.TIER_0_BOUNDARY,
            reason="Permitted: Passed Tier 0 boundaries.",
            action_type=action_type,
            target=target,
            confidence=0.9,
            intent=user_intent,
        )

    def safe_delete(
        self,
        path: str | Path,
        user_intent: Optional[str] = None,
        confirmed: bool = False,
    ) -> Tuple[bool, str, Optional[TrashResult]]:
        """Evaluates and executes a safe deletion via macOS Trash."""
        decision = self.evaluate(
            action_type=FileActionType.DELETE,
            target=str(path),
            user_intent=user_intent,
        )

        if decision.verdict == GuardVerdict.BLOCK:
            return False, f"Deletion blocked by guard: {decision.reason}", None

        if decision.verdict == GuardVerdict.NEEDS_CONFIRMATION and not confirmed:
            return False, f"Confirmation required: {decision.reason}", None

        # Execute safe deletion using Tier 2 macOS Trash
        trash_res = move_to_trash(path)
        if trash_res.success:
            return True, trash_res.message, trash_res
        return False, trash_res.message, trash_res
