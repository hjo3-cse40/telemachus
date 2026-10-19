"""Ingress Tool Pruning Router using Nomic Embed v1.5 Q8_0 & Domain Classification."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.request

from telemachus.config.loader import find_project_root

DEFAULT_EMBED_URL = "http://127.0.0.1:1234/v1/embeddings"
MODEL_ID = "text-embedding-nomic-embed-text-v1.5@q8_0"

EMAIL_KEYWORDS = {
    "email", "inbox", "mail", "draft", "unread", "sender", "recipient", "subject",
    "attachment", "triage", "newsletter", "spam", "messages", "mailbox"
}

FS_KEYWORDS = {
    "file", "folder", "directory", "dir", "path", "read", "write", "edit", "grep",
    "search", "delete", "trash", "remove", "cat", "stat", "ls", "tree", "command", "bash"
}

MEMORY_KEYWORDS = {
    "memory", "remember", "recall", "fact", "preference", "preferences", "schedule",
    "class", "classes", "profile", "identity", "know about me", "save memory"
}


def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


class IngressToolRouter:
    """Classifies user intent and semantically prunes MCP tool schemas."""

    def __init__(
        self,
        embed_url: str = DEFAULT_EMBED_URL,
        cache_path: Optional[Path] = None,
    ):
        self.embed_url = embed_url
        if cache_path is None:
            cache_dir = find_project_root() / "data"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = cache_dir / "tool_embeddings.json"
        self.cache_path = Path(cache_path)
        self.tool_vectors: Dict[str, List[float]] = {}
        self._load_cached_embeddings()

    def _query_embedding(self, text: str) -> Optional[List[float]]:
        try:
            payload = json.dumps({"model": MODEL_ID, "input": text}).encode("utf-8")
            req = urllib.request.Request(
                self.embed_url,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["data"][0]["embedding"]
        except Exception:
            return None

    def _load_cached_embeddings(self) -> None:
        if self.cache_path.exists():
            try:
                self.tool_vectors = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except Exception:
                self.tool_vectors = {}

    def _save_cached_embeddings(self) -> None:
        try:
            self.cache_path.write_text(json.dumps(self.tool_vectors), encoding="utf-8")
        except Exception:
            pass

    def warm_tool_cache(self, tools: List[Dict[str, Any]]) -> None:
        """Pre-computes embeddings for all registered tool descriptions."""
        updated = False
        for t in tools:
            name = t.get("name") or t.get("function", {}).get("name", "")
            desc = t.get("description") or t.get("function", {}).get("description", "")
            if name and name not in self.tool_vectors:
                embed_text = f"Tool '{name}': {desc}"
                vec = self._query_embedding(embed_text)
                if vec:
                    self.tool_vectors[name] = vec
                    updated = True

        if updated:
            self._save_cached_embeddings()

    def classify_domain_heuristic(self, prompt: str) -> str:
        """Fast ~0.1ms domain classification using tokenized keywords."""
        p_lower = prompt.lower().strip()

        # Pure conversational or code synthesis queries
        general_patterns = [
            r"^what\s+(is|are|was|were)\b",
            r"^explain\b",
            r"^tell\s+me\b",
            r"^how\s+(do|does|can|to)\b",
            r"^write\s+a\s+(python\s+)?(function|script|snippet|code|class|poem|essay)\b",
            r"^solve\b",
            r"^calculate\b",
            r"^summarize\s+this\s+text\b",
        ]
        is_general_pattern = any(re.search(pat, p_lower) for pat in general_patterns)

        # Check for explicit filesystem mentions (e.g. file, folder, path, specific extensions, slashes, or file actions)
        has_explicit_fs = bool(
            re.search(r"\b(file|files|folder|folders|directory|directories|path|paths|repo|repository|disk|workspace)\b", p_lower)
            or re.search(r"\b\w+\.(py|js|ts|json|txt|md|yaml|yml|log|csv|html|cfg|ini|sh)\b", p_lower)
            or re.search(r"[a-zA-Z0-9_\-\.]/[a-zA-Z0-9_\-\.]+", p_lower)  # Path with slash e.g. data/synthetic_fs
            or re.search(r"\b(grep|ls|cat|rm|touch|mkdir|cd|chmod|find|search|locate|read|inspect)\b", p_lower)
        )

        has_email = bool(
            re.search(r"\b(email|emails|inbox|mail|draft|unread|mailbox|triage|sender|recipient)\b", p_lower)
        )

        has_memory = bool(
            re.search(r"\b(memory|remember|recall|fact|facts|preference|preferences|schedule|class|classes|profile|identity)\b", p_lower)
        )

        if is_general_pattern and not (has_explicit_fs or has_email or has_memory):
            return "GENERAL"

        domains_detected = []
        if has_email:
            domains_detected.append("EMAIL")
        if has_explicit_fs:
            domains_detected.append("FILESYSTEM")
        if has_memory:
            domains_detected.append("MEMORY")

        if len(domains_detected) == 1:
            return domains_detected[0]
        elif len(domains_detected) > 1:
            return "HYBRID"
        return "GENERAL"

    def prune_tools(
        self,
        user_prompt: str,
        tools: List[Dict[str, Any]],
        max_tools: int = 5,
        min_similarity: float = 0.42,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Filters input tool list to only the most relevant tools for the prompt."""
        if not tools:
            return [], {"domain": "NONE", "scores": {}, "strategy": "empty_input"}

        self.warm_tool_cache(tools)
        domain = self.classify_domain_heuristic(user_prompt)

        # Step 1: Embed user prompt
        prompt_vec = self._query_embedding(user_prompt)
        if not prompt_vec:
            # If embedding endpoint is unreachable, filter safely by domain heuristic
            if domain == "EMAIL":
                filtered = [t for t in tools if "email_" in (t.get("name") or t.get("function", {}).get("name", "")).lower()]
                return filtered, {"domain": domain, "strategy": "heuristic_fallback"}
            elif domain == "FILESYSTEM":
                filtered = [t for t in tools if "fs_" in (t.get("name") or t.get("function", {}).get("name", "")).lower()]
                return filtered, {"domain": domain, "strategy": "heuristic_fallback"}
            elif domain == "MEMORY":
                filtered = [t for t in tools if "memory_" in (t.get("name") or t.get("function", {}).get("name", "")).lower()]
                return filtered, {"domain": domain, "strategy": "heuristic_fallback"}
            return tools, {"domain": domain, "strategy": "unpruned_fallback"}

        # Step 2: Compute cosine similarity against all known tools
        scores: Dict[str, float] = {}
        for t in tools:
            name = t.get("name") or t.get("function", {}).get("name", "")
            if name in self.tool_vectors:
                scores[name] = cosine_similarity(prompt_vec, self.tool_vectors[name])
            else:
                scores[name] = 0.0

        sorted_tools = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        # Step 3: Check if prompt needs ANY tools
        eff_min_sim = 0.52 if domain == "GENERAL" else min_similarity
        top_score = sorted_tools[0][1] if sorted_tools else 0.0
        if domain == "GENERAL" and top_score < eff_min_sim:
            # Pure conversational, mathematical, or coding logic turn -> drop all tools
            return [], {
                "domain": "GENERAL",
                "top_score": round(top_score, 3),
                "strategy": "drop_all_tools",
                "original_count": len(tools),
                "pruned_count": 0,
                "scores": {k: round(v, 3) for k, v in sorted_tools[:5]},
            }

        # Step 4: Domain-constrained top-K selection
        selected_names = set()
        if domain == "EMAIL":
            # Always ensure core email inspection tools are included
            for name, _ in sorted_tools:
                if "email_list" in name.lower() or "email_show" in name.lower():
                    selected_names.add(name)
            # Add top matching email tools up to max_tools
            for name, score in sorted_tools:
                if "email_" in name.lower() and len(selected_names) < max_tools:
                    selected_names.add(name)
        elif domain == "FILESYSTEM":
            # Always ensure fs_read is included so the model can inspect file contents
            for name, _ in sorted_tools:
                if "fs_read" in name.lower():
                    selected_names.add(name)
            # Add top matching fs tools up to max_tools
            for name, score in sorted_tools:
                if "fs_" in name.lower() and len(selected_names) < max_tools:
                    selected_names.add(name)
        elif domain == "MEMORY":
            # Always ensure memory_search is included
            for name, _ in sorted_tools:
                if "memory_search" in name.lower():
                    selected_names.add(name)
            # Add top matching memory tools up to max_tools
            for name, score in sorted_tools:
                if "memory_" in name.lower() and len(selected_names) < max_tools:
                    selected_names.add(name)
        else:
            # HYBRID or GENERAL with high similarity
            for name, score in sorted_tools:
                if score >= min_similarity and len(selected_names) < max_tools:
                    selected_names.add(name)

        # If domain heuristic was confident but semantic score fell slightly short, ensure domain tools are present
        if not selected_names:
            if domain == "EMAIL":
                selected_names = {
                    (t.get("name") or t.get("function", {}).get("name", ""))
                    for t in tools if "email_" in (t.get("name") or t.get("function", {}).get("name", "")).lower()
                }
            elif domain == "FILESYSTEM":
                selected_names = {
                    (t.get("name") or t.get("function", {}).get("name", ""))
                    for t in tools if "fs_" in (t.get("name") or t.get("function", {}).get("name", "")).lower()
                }
            elif domain == "MEMORY":
                selected_names = {
                    (t.get("name") or t.get("function", {}).get("name", ""))
                    for t in tools if "memory_" in (t.get("name") or t.get("function", {}).get("name", "")).lower()
                }
            else:
                # Default to top 2 semantic matches
                selected_names = {name for name, _ in sorted_tools[:2]}

        pruned = [
            t for t in tools
            if (t.get("name") or t.get("function", {}).get("name", "")) in selected_names
        ]

        metadata = {
            "domain": domain,
            "top_score": round(top_score, 3),
            "selected_tools": list(selected_names),
            "scores": {k: round(v, 3) for k, v in sorted_tools[:6]},
            "original_count": len(tools),
            "pruned_count": len(pruned),
        }
        return pruned, metadata
