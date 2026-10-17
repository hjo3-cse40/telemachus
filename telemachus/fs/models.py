"""Data models and enum definitions for Telemachus Filesystem Guardrail."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class FileActionType(str, Enum):
    LIST = "list"
    READ = "read"
    SEARCH = "search"
    GREP = "grep"
    STAT = "stat"
    EDIT = "edit"
    WRITE = "write"
    DELETE = "delete"
    EXECUTE = "execute"
    CHECKPOINT = "checkpoint"


class GuardVerdict(str, Enum):
    PERMIT = "permit"
    BLOCK = "block"
    NEEDS_CONFIRMATION = "needs_confirmation"


class TierLevel(int, Enum):
    TIER_0_BOUNDARY = 0
    TIER_1_INTENT_ALIGNMENT = 1
    TIER_2_SOFT_RECOVERY = 2
    TIER_3_ELEVATION = 3


@dataclass
class GuardDecision:
    verdict: GuardVerdict
    tier: TierLevel
    reason: str
    action_type: FileActionType
    target: str
    confidence: float = 1.0
    intent: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "tier": self.tier.value,
            "reason": self.reason,
            "action_type": self.action_type.value,
            "target": self.target,
            "confidence": self.confidence,
            "intent": self.intent,
            "details": self.details,
        }


@dataclass
class TrashResult:
    success: bool
    original_path: str
    trash_path: Optional[str] = None
    message: str = ""
    timestamp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class GitCheckpointResult:
    success: bool
    repo_path: str
    checkpoint_id: Optional[str] = None
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
