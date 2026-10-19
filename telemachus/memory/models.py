"""Data models and sensitivity taxonomy for the unified memory system."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import time
from typing import Any, Dict, Optional
import uuid


class PrivacyTier(str, Enum):
    """Data sensitivity and distribution tier."""
    TIER_0_PUBLIC = "tier_0_public"          # Safe for all cloud models (code style, formatting, git conventions)
    TIER_1_PROJECT = "tier_1_project"        # Project & architecture context (selective cloud safe, sanitized)
    TIER_2_PRIVATE = "tier_2_private"        # Strictly local only (PII, finances, schedule, health, credentials)

    @classmethod
    def from_str(cls, val: str) -> PrivacyTier:
        for member in cls:
            if member.value.lower() == val.lower() or member.name.lower() == val.lower():
                return member
        return cls.TIER_2_PRIVATE

    def is_cloud_safe(self) -> bool:
        """Only Tier 0 and Tier 1 (if explicitly allowed) are cloud safe."""
        return self in (PrivacyTier.TIER_0_PUBLIC, PrivacyTier.TIER_1_PROJECT)


@dataclass
class MemoryEntry:
    """Atomic memory unit representing a fact, preference, rule, or project state."""
    text: str
    category: str = "fact"                   # fact, preference, project, identity, hardware, financial, style
    tier: PrivacyTier = PrivacyTier.TIER_2_PRIVATE
    source: str = "user"                     # user, auto, odysseus, cursor, telemachus
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: int = field(default_factory=lambda: int(time.time()))
    uses: int = 0
    pinned: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["tier"] = self.tier.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> MemoryEntry:
        tier_val = data.get("tier", PrivacyTier.TIER_2_PRIVATE.value)
        tier = PrivacyTier.from_str(tier_val) if isinstance(tier_val, str) else tier_val
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            text=data.get("text", ""),
            category=data.get("category", "fact"),
            tier=tier,
            source=data.get("source", "user"),
            timestamp=data.get("timestamp", int(time.time())),
            uses=data.get("uses", 0),
            pinned=data.get("pinned", False),
            metadata=data.get("metadata", {}),
        )
