#!/usr/bin/env python3
"""Live check: does the real model cite memories and refuse to guess?

Needs LM Studio on :1234 with a chat model loaded. Seeds a temporary vault (never
~/.memory), gives the model the memory_search and memory_source tools, runs each
question as a small tool loop with thinking ON, and checks the final answer:
- questions with a stored answer: the answer must cite an [M#] tag;
- questions without one: the answer must say there is no record (no invented fact).

    .venv/bin/python tests/benchmarks/live_recall_citation.py [--json-out FILE]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from telemachus.memory.cache import MemoryCache  # noqa: E402
from telemachus.memory.extractor import MemoryExtractor  # noqa: E402
from telemachus.memory.models import MemoryEntry  # noqa: E402
from telemachus.memory.recall import recall, render_recall, render_source  # noqa: E402
from telemachus.memory.vault import MemoryVault  # noqa: E402

URL = "http://127.0.0.1:1234/v1/chat/completions"
SYSTEM = (
    "You are Telemachus, a local assistant. Use memory_search before you answer any question about the user, "
    "the people they work with, or their documents. Follow the INSTRUCTION line in every tool result."
)
TOOLS = [
    {"type": "function", "function": {
        "name": "memory_search",
        "description": ("Searches the local memory vault. Returns STATUS (MATCH, WEAK_MATCH or NO_MATCH), an INSTRUCTION, "
                        "and results tagged [M1], [M2] with source and quote. Cite tags for facts you use; on NO_MATCH say you "
                        "have no record instead of guessing. Use short keyword queries."),
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "memory_source",
        "description": "Reads the full source of one memory by id: quote, where it came from, and the source text around it.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}},
]
SEED = [
    ("User's director, Kim, prefers short emails in formal Korean.",
     {"kind": "self", "quote": "Kim likes short emails in formal Korean", "source_ref": "you said in chat, 2026-10-12 09:30"}),
    ("User signs Korean emails with '홍길동 드림'.",
     {"kind": "self", "quote": "I always end Korean mail with 홍길동 드림", "source_ref": "you said in chat, 2026-10-10 14:02"}),
    ("Dana Park prefers Tuesday calls.",
     {"kind": "document", "about": "Dana Park", "quote": "I prefer Tuesday calls if possible",
      "source_ref": "pasted in chat 2026-10-15 11:20"}),
    ("Jordan will send the sample wafers by Oct 20.",
     {"kind": "document", "about": "Jordan", "quote": "Jordan will send the sample wafers by Oct 20",
      "source_ref": "telemachus_fs_read:/Users/example/notes/oct8.md"}),
]
QUESTIONS = [
    ("What email style does Kim prefer?", "cite"),
    ("How do I sign off Korean emails?", "cite"),
    ("When does Dana like to have calls?", "cite"),
    ("When are the sample wafers coming?", "cite"),
    ("What is Jordan's phone number?", "refuse"),
    ("What is Kim's birthday?", "refuse"),
]
REFUSAL = re.compile(r"(no (stored )?record|don't have|do not have|not (stored|saved|in (my|the) memory)|no information|couldn't find|could not find|not found|no memory|unable to find|isn't (stored|in)|not sure)", re.I)


def chat(messages):
    body = {"model": MemoryExtractor().resolve_model(), "messages": messages, "tools": TOOLS,
            "temperature": 0.6, "top_p": 0.95, "top_k": 20, "presence_penalty": 1.5, "max_tokens": 6000}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="telemachus_recall_")
    vault = MemoryVault(tmp)
    vault.initialize()
    cache = MemoryCache(vault=vault)
    for text, meta in SEED:
        e = MemoryEntry(text=text, metadata=meta)
        vault.append_fact(e) if meta["kind"] == "self" else vault.append_document_fact(e)
        cache.upsert_entry(e)

    rows = []
    for question, expect in QUESTIONS:
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
        tool_log, reasoning = [], []
        t0 = time.time()
        answer = ""
        for _ in range(5):
            resp = chat(messages)
            msg = resp["choices"][0]["message"]
            if msg.get("reasoning_content"):
                reasoning.append(msg["reasoning_content"])
            calls = msg.get("tool_calls") or []
            messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            if not calls:
                answer = msg.get("content") or ""
                break
            for c in calls:
                fn = c["function"]
                a = json.loads(fn.get("arguments") or "{}")
                if fn["name"] == "memory_search":
                    out = render_recall(recall(cache, a.get("query", "")))
                else:
                    out = render_source(vault, cache, a.get("id", ""))
                tool_log.append({"tool": fn["name"], "args": a, "status": out.splitlines()[0]})
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": out})
        cited = bool(re.search(r"\[M\d+\]", answer))
        refused = bool(REFUSAL.search(answer))
        ok = cited if expect == "cite" else (refused and not cited)
        think = "\n".join(reasoning)
        row = {"question": question, "expect": expect, "ok": ok, "cited": cited, "refused": refused,
               "seconds": round(time.time() - t0, 1), "tools": tool_log, "answer": answer.strip(),
               "thinking_mentions_source": bool(re.search(r"(\[M\d|source|quote)", think, re.I)),
               "thinking_chars": len(think)}
        rows.append(row)
        print(f"{'OK  ' if ok else 'FAIL'} {question:42} {row['seconds']:6.1f}s tools={[t['tool'] + ':' + t['status'][8:20] for t in tool_log]}")
        print(f"      answer: {answer.strip()[:220]!r}")
        print(f"      thinking reads source/quote: {row['thinking_mentions_source']} ({len(think)} chars)")
    print(f"\n{sum(r['ok'] for r in rows)}/{len(rows)} OK")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
