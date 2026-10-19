"""Continual Memory Extraction Engine ported and adapted from Odysseus.

Features:
1. Transcript Flattener: Formats multi-turn chat into an analytical prompt for local Qwen 3.5 9B.
2. Reasoning Stripper: Strips <think>...</think> tags and isolates JSON arrays.
3. Deterministic Regex Fallback: Captures durable facts even if LLM extraction fails or is offline.
4. PrivacyGate Classification: Auto-routes memories into private/ or public_projected/ before writing.
5. Multi-Tier Deduplication: Avoids saving duplicate facts using Jaccard similarity.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
import urllib.error
import urllib.request

from telemachus.memory.cache import MemoryCache
from telemachus.memory.models import MemoryEntry, PrivacyTier
from telemachus.memory.privacy_gate import PrivacyGate
from telemachus.memory.vault import MemoryVault

logger = logging.getLogger(__name__)

DEFAULT_COMPLETION_URL = "http://127.0.0.1:1234/v1/chat/completions"
DEFAULT_MODEL = "qwen/qwen3.5-9b"
CONTEXT_WINDOW = 6

EXTRACT_SYSTEM_PROMPT = (
    "You are a memory extraction assistant. Analyze the conversation and extract ONLY "
    "durable personal facts about the user that would be useful across future conversations.\n\n"
    "Good examples: name, job title, school, recurring classes, hardware setup, strong preferences, projects.\n"
    "Bad examples: what they asked about today, temporary mood, assistant explanations, one-off code bugs.\n\n"
    "Rules:\n"
    "- MAX 2 facts per conversation — only the most important\n"
    "- Only extract facts the USER stated or clearly implied\n"
    "- Each fact must be a single short sentence (under 18 words)\n"
    "- Return a JSON array of objects with 'text' and 'category' fields.\n"
    "Categories: 'identity', 'preference', 'fact', 'project', 'hardware', 'financial'\n"
    "Return ONLY valid JSON, no markdown fences."
)


def strip_think(text: str) -> str:
    """Strips <think>...</think> reasoning blocks from model responses."""
    if not text:
        return ""
    # Remove complete think tags
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Remove dangling unclosed think tag if present
    if "<think>" in cleaned:
        cleaned = cleaned.split("<think>", 1)[0]
    return cleaned.strip()


def parse_extraction_json(raw: str) -> List[Dict[str, Any]]:
    """Robustly parses LLM JSON array output, handling reasoning tags and markdown fences."""
    text = strip_think(raw).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    start_idx = text.find("[")
    end_idx = text.rfind("]")
    if 0 <= start_idx < end_idx:
        text = text[start_idx : end_idx + 1]

    try:
        data = json.loads(text)
        if isinstance(data, list):
            valid = []
            for item in data:
                if isinstance(item, dict) and item.get("text"):
                    valid.append(item)
            return valid
    except Exception as e:
        logger.debug(f"JSON parsing failed on extracted text: {e}")
    return []


def fallback_regex_candidates(messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Deterministic regex extraction fallback from user statements."""
    candidates: List[Dict[str, str]] = []
    seen: Set[str] = set()

    def add(text: str, cat: str):
        cleaned = text.strip()
        if not cleaned or cleaned.lower() in seen:
            return
        seen.add(cleaned.lower())
        candidates.append({"text": cleaned, "category": cat})

    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        if not isinstance(content, str):
            continue

        # Name
        m = re.search(r"\bmy name is\s+([A-Za-z][A-Za-z0-9 .'\-]{1,50})\b", content, re.I)
        if m:
            add(f"User's name is {m.group(1).strip()}.", "identity")

        # Location
        m = re.search(r"\bi (?:live in|am from|'m from)\s+([^.!?\n]{2,80})", content, re.I)
        if m:
            add(f"User lives in {m.group(1).strip()}.", "identity")

        # Preferences & dislikes
        m = re.search(r"\bi (prefer|like|love|hate|do not like|don't like)\s+([^.!?\n]{4,100})", content, re.I)
        if m:
            verb = m.group(1).lower()
            val = m.group(2).strip()
            if verb in ("hate", "do not like", "don't like"):
                add(f"User dislikes {val}.", "preference")
            else:
                add(f"User prefers {val}.", "preference")

        # School / Classes
        m = re.search(r"\b(?:my class is|taking class|i take|i am taking)\s+([A-Za-z]{2,5}\s*\d{1,4}[A-Za-z]?)", content, re.I)
        if m:
            add(f"User is taking {m.group(1).strip().upper()}.", "fact")

        # Favorites
        m = re.search(r"\bmy favorite\s+([a-zA-Z\s]{3,30})\s+is\s+([^.!?\n]{2,80})", content, re.I)
        if m:
            add(f"User's favorite {m.group(1).strip()} is {m.group(2).strip()}.", "preference")

    return candidates[:2]


def is_jaccard_duplicate(new_text: str, existing_memories: List[MemoryEntry], threshold: float = 0.60) -> bool:
    """Checks if new_text is overly similar to any existing memory using token Jaccard overlap."""
    new_tokens = set(re.findall(r"\w+", new_text.lower()))
    if not new_tokens:
        return False

    for mem in existing_memories:
        old_tokens = set(re.findall(r"\w+", mem.text.lower()))
        if not old_tokens:
            continue
        intersection = new_tokens & old_tokens
        union = new_tokens | old_tokens
        if len(intersection) / len(union) >= threshold:
            return True
    return False


class MemoryExtractor:
    """Orchestrates continual conversation fact extraction and privacy classification."""

    def __init__(
        self,
        vault: Optional[MemoryVault] = None,
        cache: Optional[MemoryCache] = None,
        completion_url: str = DEFAULT_COMPLETION_URL,
        model_name: str = DEFAULT_MODEL,
    ):
        self.vault = vault or MemoryVault()
        self.cache = cache or MemoryCache(vault=self.vault)
        self.completion_url = completion_url
        self.model_name = model_name

    def flatten_transcript(self, messages: List[Dict[str, Any]]) -> str:
        """Flattens recent conversation turns into a single analytic block."""
        recent = messages[-CONTEXT_WINDOW:] if len(messages) > CONTEXT_WINDOW else messages
        lines = ["Conversation transcript to analyze:\n"]
        for m in recent:
            role = m.get("role", "unknown").capitalize()
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            if content and isinstance(content, str):
                # Truncate very long single turns to prevent token bloat
                lines.append(f"{role}: {content[:500].strip()}")
        lines.append("\nExtract durable facts about the user as a JSON array of {text, category} objects.")
        return "\n".join(lines)

    def query_llm_extraction(self, transcript_prompt: str, timeout: float = 20.0) -> List[Dict[str, Any]]:
        """Invokes local Qwen 3.5 9B to extract facts."""
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": transcript_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 1024,
        }

        try:
            req = urllib.request.Request(
                self.completion_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                choice = data.get("choices", [{}])[0]
                content = choice.get("message", {}).get("content", "")
                return parse_extraction_json(content)
        except Exception as e:
            logger.debug(f"Extraction LLM call failed or timed out: {e}")
            return []

    def extract_and_store(self, messages: List[Dict[str, Any]]) -> List[MemoryEntry]:
        """Runs the complete extraction pipeline on a conversation turn."""
        if not messages or len(messages) < 1:
            return []

        # Step 1: Collect candidates (deterministic regex + LLM)
        fallback_candidates = fallback_regex_candidates(messages)

        transcript = self.flatten_transcript(messages)
        llm_candidates = self.query_llm_extraction(transcript)

        # Merge candidates (LLM candidates first, then fallback)
        all_candidates = llm_candidates + [
            c for c in fallback_candidates
            if not any(c["text"].lower() == lc.get("text", "").lower() for lc in llm_candidates)
        ]

        if not all_candidates:
            return []

        # Load existing memories for deduplication
        existing_memories = self.vault.load_facts()
        saved_entries: List[MemoryEntry] = []

        for cand in all_candidates[:3]:
            raw_text = cand.get("text", "").strip()
            cat = cand.get("category", "fact")
            if not raw_text or len(raw_text) < 5:
                continue

            # Deduplication check
            if is_jaccard_duplicate(raw_text, existing_memories):
                logger.debug(f"Skipping duplicate fact: '{raw_text}'")
                continue

            # Step 2: PrivacyGate Classification
            tier, reason, confidence = PrivacyGate.classify(raw_text, category=cat)

            entry = MemoryEntry(
                text=raw_text,
                category=cat,
                tier=tier,
                source="continual_extractor",
                metadata={"privacy_reason": reason, "privacy_confidence": confidence},
            )

            # Step 3: Write to Vault and Cache
            self.vault.append_fact(entry)
            self.cache.upsert_entry(entry)
            existing_memories.append(entry)
            saved_entries.append(entry)

            self.vault.log_event(
                "AUTO_EXTRACTED",
                {
                    "id": entry.id,
                    "text": entry.text,
                    "tier": entry.tier.value,
                    "category": entry.category,
                    "reason": reason,
                },
            )

        return saved_entries
