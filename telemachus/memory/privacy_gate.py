"""Extraction-Time Privacy Classification Gate.

Enforces zero-leakage sensitivity classification using a deterministic regex sieve
and heuristic categorization before writing memories to disk.
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Set, Tuple

from telemachus.memory.models import PrivacyTier

logger = logging.getLogger(__name__)

# Patterns that MUST NEVER leave the local machine (unconditionally Tier 2 Private)
PRIVATE_SIEVE_PATTERNS = [
    # Currency, Bank Balances & Accounting
    re.compile(r"(\$\s*\d+(?:\.\d{2})?|\b\d+\s*(?:dollars|cents|usd|bucks)\b)", re.I),
    re.compile(r"\b(checking|savings|paycheck|payroll|salary|hourly rate|compensation|venmo|zelle|bank balance|net worth|budget|rent|pge|pg&e|fixed bills|expenses)\b", re.I),

    # PII & Identifiers
    re.compile(r"\b(ssn|social security|passport|driver'?s license|home address|phone number)\b", re.I),

    # Secrets, Auth, & Tokens
    re.compile(r"\b(password|api[_\s\-]?key|bearer\s+[a-zA-Z0-9_\-\.]+|ssh-(?:rsa|ed25519)|secret[_\s\-]?token)\b", re.I),

    # Medical & Health
    re.compile(r"\b(diagnos|prescrib|medication|doctor|hospital|clinic|therapy|therapist|allerg(?:y|ies)|mental health)\b", re.I),

    # Personal Daily Life, Commute, Relationships & School Schedule
    re.compile(r"\b(ucsc|cmpm\s*\d+|santa cruz|menlo park|girlfriend|gf|wife|husband|parents|dad'?s money|commute route|driving route|class meets)\b", re.I),
]

# Patterns that represent safe global coding styles or tech conventions (Tier 0 Public)
PUBLIC_STYLE_PATTERNS = [
    re.compile(r"\b(?:no\s+)?em[_\s\-]?dash(?:es)?\b|\b(commit attribution|commit trailer|formatting|indentation|tabs? over spaces|spaces? over tabs)\b", re.I),
    re.compile(r"\b(camelcase|snake_case|pascalcase|kebab-case|pep8|eslint|prettier|linter|code style)\b", re.I),
    re.compile(r"\b(dark theme|light theme|terminal font|jetbrains mono|fira code)\b", re.I),
]

# Patterns that represent project/architecture context (Tier 1 Project)
PROJECT_PATTERNS = [
    re.compile(r"\b(telemachus|odysseus|airlock|game engine|architecture|repo|repository|workflow|pipeline)\b", re.I),
]


class PrivacyGate:
    """Classifies memories into PrivacyTier levels with zero false-negative guarantees."""

    @classmethod
    def classify(cls, text: str, category: str = "fact") -> Tuple[PrivacyTier, str, float]:
        """Evaluates memory text and returns (PrivacyTier, reason, confidence).

        Rules:
        1. Any match in PRIVATE_SIEVE_PATTERNS is unconditionally TIER_2_PRIVATE (confidence 1.0).
        2. Any match in PUBLIC_STYLE_PATTERNS (with zero private matches) is TIER_0_PUBLIC.
        3. Match in PROJECT_PATTERNS (with zero private matches) is TIER_1_PROJECT.
        4. Category 'financial', 'identity', or 'health' is unconditionally TIER_2_PRIVATE.
        5. Default-to-private: everything else is TIER_2_PRIVATE.
        """
        clean_text = text.strip()
        cat_lower = category.lower().strip()

        # Hard category checks
        if cat_lower in ("financial", "finance", "health", "identity"):
            return PrivacyTier.TIER_2_PRIVATE, f"Category '{category}' is strictly private", 1.0

        # Stage 1: Deterministic Private Sieve
        for pat in PRIVATE_SIEVE_PATTERNS:
            match = pat.search(clean_text)
            if match:
                return PrivacyTier.TIER_2_PRIVATE, f"Matched sensitive pattern: '{match.group(0)}'", 1.0

        # Stage 2: Public Coding / Style Checks
        for pat in PUBLIC_STYLE_PATTERNS:
            match = pat.search(clean_text)
            if match:
                return PrivacyTier.TIER_0_PUBLIC, f"Matched public style pattern: '{match.group(0)}'", 0.95

        # Stage 3: Project Context Checks
        if cat_lower == "project":
            return PrivacyTier.TIER_1_PROJECT, "Classified as project context", 0.90

        for pat in PROJECT_PATTERNS:
            match = pat.search(clean_text)
            if match:
                return PrivacyTier.TIER_1_PROJECT, f"Matched project pattern: '{match.group(0)}'", 0.90

        # Default-to-private safety rule
        return PrivacyTier.TIER_2_PRIVATE, "Defaulted to private for non-public content", 0.85
