"""Memory intake: decides what a chat turn may add to memory, and where.

Runs once per new user turn (the proxy calls it when the last message has role "user").

1. Source split (code, exact). Only the text of the user's newest message can become
   facts about the user. Tool output and assistant text never can.
2. Paste check (paste_detect, code). The message is split into OWN, PASTED and UNSURE
   blocks. PASTED text goes to the document path only. OWN and UNSURE text go to the
   self-fact extraction; a fact quoted from UNSURE text always goes to review.
3. Laya (optional, weak, measured). It skips the LLM call when p_durable <
   DURABILITY_SKIP_BELOW and the message has no UNSURE text. Its authorship score
   (p_own) is recorded with each decision as calibration data; it decides nothing,
   because zero-shot it measured too weak to decide.
4. Grounded extraction (extractor). Each fact needs a quote that code finds in the
   source text, and at least half of the fact's words must be in the quote. Otherwise
   it is rejected. A fact is saved without review only when its quote is an exact
   match in OWN text and covers at least 80% of the fact's words. Everything else that
   passes goes to review: approximate quote, UNSURE text, another language, negation
   differs, extra words, or close to a stored fact (a possible update).
5. Documents. Pasted text and the documents that tools read in the previous turn
   (fs_read, read, email_show) give facts about other people, saved to the document
   store with the source path and quote. They never enter the user profile. A document
   fact that reads like an instruction (to an assistant, to forward, BCC, or to a URL or
   email address) goes to review: document text is third-party text.

Every failure is reported to MemoryHealth (health.json, events.jsonl, WARNING log), and
a turn that could not be processed is kept in failed_turns.jsonl for a retry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from telemachus.memory.cache import MemoryCache
from telemachus.memory.extractor import (
    DOC_MAX_CHARS,
    MIN_SUPPORT,
    ExtractionError,
    MemoryExtractor,
    support_share,
    verify_quote,
)
from telemachus.memory.health import MemoryHealth
from telemachus.memory.laya_gate import LayaUnavailable, get_gate, laya_enabled
from telemachus.memory.models import MemoryEntry, PrivacyTier
from telemachus.memory.paste_detect import OWN, PASTED, UNSURE, Block, analyze
from telemachus.memory.privacy_gate import PrivacyGate
from telemachus.memory.vault import MemoryVault

logger = logging.getLogger("telemachus.memory.intake")

# From tests/benchmarks/laya_memory_calibration.py on the synthetic set (zero-shot,
# English checkpoint, v2 wording). Real durable facts scored >= 0.28; 0.2 keeps a margin.
DURABILITY_SKIP_BELOW = 0.2
# A fact is saved without review only when at least this share of its words is in
# the quote. Between MIN (fact_supported) and this, it goes to review: the model added
# words ("as a senior manager in Seoul") that the quote does not support.
AUTO_SAVE_SUPPORT = 0.8

# Tools whose results are documents the user is working with.
DOC_TOOL_SUFFIXES = ("fs_read", "email_show")
DOC_TOOL_EXACT = {"read"}
DOC_MIN_CHARS = 200
MAX_DOCS_PER_TURN = 2
# Jaccard similarity to a stored fact: at or above DUPLICATE_AT the fact is the same;
# between UPDATE_AT and DUPLICATE_AT it may be a changed fact ("short" -> "long" emails).
DUPLICATE_AT = 0.85
UPDATE_AT = 0.6

HANGUL = re.compile(r"[\uac00-\ud7a3]")
# Line-number prefixes of the two document tools: fs_read "{n:4d}: " and opencode read "00001| ".
_LINE_NUMBER = re.compile(r"^(?: {0,5}\d{1,6}: |\d{5}\| )", re.M)
_TOOL_HEADER = re.compile(r"^(File: [^\n]*\(lines \d+-\d+ of \d+\):|--- Message Body ---)[ \t]*$", re.M)
_NEGATION = re.compile(
    r"\b(not|never|no|none|nobody|nothing|no longer|without|dislikes?|hates?|avoids?|"
    r"(do|does|did|ca|wo|is|are|was|were|could|would|should|have|has)n'?t|cannot)\b"
    # Korean: 않/못/싫/없, and 안 as its own word or before a verb (안 해, 안해, 안가).
    # Not inside a word: 동안, 제안, 안녕, 안내 stay positive.
    r"|않|못|싫|없|(?<![\uac00-\ud7a3])안(?=\s|해|하|돼|되|가|와|써|쓰|먹|봐|보)",
    re.I,
)
# Document text that tries to steer the assistant or route data: it speaks to an AI,
# overrides instructions, or adds hidden recipients. Ordinary mentions of AI, links or
# email addresses are not enough (they are in most work email).
_INSTRUCTION_LIKE = re.compile(
    r"\b(ignore (all|any|the|previous|prior)\b|system prompt|(assistant|ai|claude|chatgpt|telemachus)\s*[:,]\s|"
    r"you are (an?|the) (ai|assistant|model)|instructions? (to|for) (the |any )?(ai|assistant|model)|"
    r"bcc|forward (all|every|each|any)\b)",
    re.I,
)


@dataclass
class IntakeReport:
    """What the intake did with one turn. Logged and returned for tests."""
    status: str = "processed"          # processed | skipped | failed
    reason: str = ""
    blocks: List[Dict[str, Any]] = field(default_factory=list)
    laya: Optional[Dict[str, Any]] = None
    saved: List[str] = field(default_factory=list)
    queued_for_review: List[str] = field(default_factory=list)
    rejected: List[Dict[str, str]] = field(default_factory=list)
    document_facts: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def _str(value: Any) -> str:
    """LLM output fields can be lists, numbers or null. Only a string counts."""
    return value.strip() if isinstance(value, str) else ""


def message_text(message: Dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        # Parts marked synthetic are added by the harness (file attachments, summaries),
        # not typed by the user.
        return "\n".join(
            p["text"] for p in content
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
            and not p.get("synthetic")
        ).strip()
    return content.strip() if isinstance(content, str) else ""


def clean_document_text(text: str) -> str:
    """Removes tool framing: the header line and the line-number prefixes of document tools.

    Prefixes are removed only when most lines carry one, so "10:30 call with Helen" in a
    normal document stays as it is.
    """
    text = _TOOL_HEADER.sub("", text)
    lines = [l for l in text.splitlines() if l.strip()]
    if lines and sum(1 for l in lines if _LINE_NUMBER.match(l)) >= 0.6 * len(lines):
        text = _LINE_NUMBER.sub("", text)
    return text.strip()


def _cross_language(fact: str, quote: str) -> bool:
    """True when one side has Hangul and the other has none: word overlap cannot be checked."""
    return bool(HANGUL.search(fact)) != bool(HANGUL.search(quote))


def _negation_differs(fact: str, quote: str) -> bool:
    return bool(_NEGATION.search(fact)) != bool(_NEGATION.search(quote))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_doc_tool(name: str) -> bool:
    n = (name or "").lower()
    return n in DOC_TOOL_EXACT or any(n.endswith(s) for s in DOC_TOOL_SUFFIXES)


def _closest(text: str, existing: List[MemoryEntry]) -> Tuple[float, Optional[MemoryEntry]]:
    new = set(re.findall(r"\w+", text.lower()))
    best, best_entry = 0.0, None
    for e in existing:
        old = set(re.findall(r"\w+", e.text.lower()))
        if new and old:
            j = len(new & old) / len(new | old)
            if j > best:
                best, best_entry = j, e
    return best, best_entry


def previous_turn_documents(messages: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """Returns (source_label, text) for document-tool results since the previous user message."""
    if not messages:
        return []
    last_user = len(messages) - 1
    prev_user = next((i for i in range(last_user - 1, -1, -1) if messages[i].get("role") == "user"), -1)
    calls: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    docs: List[Tuple[str, str]] = []
    for m in messages[prev_user + 1:last_user]:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                calls[tc.get("id", "")] = (fn.get("name", ""), args if isinstance(args, dict) else {})
        elif m.get("role") == "tool":
            name, args = calls.get(m.get("tool_call_id", ""), ("", {}))
            if not _is_doc_tool(name):
                continue
            text = clean_document_text(message_text(m))
            if len(text) < DOC_MIN_CHARS or text.startswith("Error"):
                continue
            target = args.get("path") or args.get("filePath") or args.get("file_path") or args.get("uid") or "?"
            docs.append((f"{name}:{target}", text))
    return docs


class MemoryIntake:
    def __init__(
        self,
        vault: Optional[MemoryVault] = None,
        cache: Optional[MemoryCache] = None,
        extractor: Optional[MemoryExtractor] = None,
        gate=None,
        use_laya: Optional[bool] = None,
    ):
        self.vault = vault or MemoryVault()
        self.vault.initialize()
        self.cache = cache or MemoryCache(vault=self.vault)
        self.extractor = extractor or MemoryExtractor()
        self.health = MemoryHealth(self.vault)
        self.use_laya = laya_enabled() if use_laya is None else use_laya
        self._gate = gate

    @property
    def gate(self):
        if self._gate is None:
            self._gate = get_gate()
        return self._gate

    # ----- entry point --------------------------------------------------------------

    def process_turn(self, messages: List[Dict[str, Any]], force: bool = False) -> IntakeReport:
        """Never raises an Exception. Any failure is recorded (health, events,
        failed_turns) and reported. force=True skips the turn dedup (used by retry)."""
        report = IntakeReport()
        if not messages or not isinstance(messages[-1], dict) or messages[-1].get("role") != "user":
            report.status, report.reason = "skipped", "not a new user turn"
            return report
        try:
            user_text = message_text(messages[-1])
        except Exception as e:
            self._safe_fail("intake_crash", f"message_text: {type(e).__name__}: {e}")
            report.status, report.reason = "failed", str(e)
            return report

        # Documents first: they belong to the previous turn, so a repeated short message
        # ("thanks") must not hide them. Each document has its own dedup by content.
        try:
            for label, text in previous_turn_documents(messages)[:MAX_DOCS_PER_TURN]:
                self._document_facts(label, text, report)
        except Exception as e:
            self._safe_fail("intake_crash", f"documents: {type(e).__name__}: {e}")
            report.errors.append(f"documents crashed: {e}")

        if not user_text:
            report.status, report.reason = "skipped", "empty user message"
            return report
        turn_hash = _sha("turn:" + user_text)
        try:
            if not force and not self.cache.mark_seen(turn_hash, "turn"):
                report.status, report.reason = "skipped", "turn already processed"
                return report
            self._process_user_text(user_text, turn_hash, report)
        except ExtractionError as e:
            self._fail_turn(turn_hash, user_text, "extraction_llm", str(e), report)
            return report
        except Exception as e:
            # A bug or a locked database must not lose the turn: keep it for retry.
            self._fail_turn(turn_hash, user_text, "intake_crash", f"{type(e).__name__}: {e}", report)
            return report
        try:
            if not report.errors:
                self.health.ok("intake_crash")
            self.vault.log_event("INTAKE_TURN", {
                "turn_hash": turn_hash[:12], "blocks": report.blocks, "saved": report.saved,
                "review": report.queued_for_review, "rejected": report.rejected,
                "document_facts": report.document_facts, "laya": report.laya, "errors": report.errors,
                "reason": report.reason,
            })
        except Exception as e:
            logger.warning(f"[memory] could not log the intake turn: {e}")
        return report

    def _process_user_text(self, user_text: str, turn_hash: str, report: IntakeReport) -> None:
        analysis = analyze(user_text)
        self._apply_laya(analysis.blocks, report)
        report.blocks = [{"label": b.label, "reasons": b.reasons, "chars": len(b.text)} for b in analysis.blocks]

        own_text = analysis.own_text
        # Self facts are read from OWN and UNSURE text, in message order. Facts quoted
        # from UNSURE text go to review: code could not confirm the text as the user's.
        self_text = "\n\n".join(b.text for b in analysis.blocks if b.label in (OWN, UNSURE))
        if self_text:
            self._self_facts(self_text, own_text, analysis.unsure_text, turn_hash, report)

        pasted = "\n\n".join(b.text for b in analysis.blocks if b.label in (PASTED, UNSURE))
        if pasted:
            self._document_facts(f"pasted in chat {time.strftime('%Y-%m-%d %H:%M')}", pasted, report)

    # ----- steps --------------------------------------------------------------------

    def _apply_laya(self, blocks: List[Block], report: IntakeReport) -> None:
        """Asks Laya once about the whole non-pasted text. Hangul text skips Laya: both
        checkpoints measured at chance on Korean."""
        if not self.use_laya:
            report.laya = {"status": "disabled"}
            return
        text = "\n\n".join(b.text for b in blocks if b.label in (OWN, UNSURE))
        if not text or HANGUL.search(text):
            report.laya = {"status": "not_needed"}
            return
        try:
            result = self.gate.classify([text])[0]
        except LayaUnavailable as e:
            self.health.fail("laya_gate", str(e))
            report.laya = {"status": "unavailable", "error": str(e)}
            report.errors.append(f"laya: {e}")
            return
        self.health.ok("laya_gate")
        report.laya = {
            "status": "ok",
            "p_durable": round(result["durability"]["probabilities"]["durable_personal_fact"], 3),
            # Recorded for calibration only; not used for any decision.
            "p_own": round(result["authorship"]["probabilities"]["own_words"], 3),
        }

    def _self_facts(self, self_text: str, own_text: str, unsure_text: str, turn_hash: str,
                    report: IntakeReport) -> None:
        has_unsure = bool(unsure_text)
        laya = report.laya or {}
        if (laya.get("status") == "ok" and laya["p_durable"] < DURABILITY_SKIP_BELOW and not has_unsure):
            report.reason = f"laya: no lasting fact (p_durable={laya['p_durable']} < {DURABILITY_SKIP_BELOW})"
            return
        candidates = self.extractor.extract_self_facts(own_text, unsure_text)
        self.health.ok("extraction_llm")
        source_key = _sha("own:" + self_text)[:32]
        source_ref = f"you said in chat, {time.strftime('%Y-%m-%d %H:%M')}"
        if candidates:
            self.vault.save_source(source_key, self_text, source_ref)
        existing = self.vault.load_facts() + self.vault.load_pending()
        for cand in candidates[:3]:
            if not isinstance(cand, dict):
                continue
            text, quote = _str(cand.get("text")), _str(cand.get("quote"))
            if not text:
                continue
            match = verify_quote(quote, self_text)
            if match is None:
                self._reject(report, text, f"quote not found in the user's words: {quote!r}")
                continue
            review_reasons = []
            share = support_share(text, quote)
            if share < MIN_SUPPORT:
                if not _cross_language(text, quote):
                    self._reject(report, text, f"fact is not supported by its quote: {quote!r}")
                    continue
                review_reasons.append("fact and quote are in different languages; code cannot check the fact")
            elif share < AUTO_SAVE_SUPPORT:
                review_reasons.append(f"the fact adds words that are not in its quote ({share:.0%} supported)")
            if _negation_differs(text, quote):
                review_reasons.append("the fact and its quote differ in negation (not, never, can't, 안)")
            similarity, similar = _closest(text, existing)
            if similarity >= DUPLICATE_AT:
                report.rejected.append({"text": text, "reason": f"same as stored fact: {similar.text!r}"})
                self.vault.log_event("FACT_DUPLICATE", {"text": text, "existing_id": similar.id})
                continue
            if similarity >= UPDATE_AT:
                review_reasons.append(f"close to a stored fact, may be an update: {similar.text!r} (id {similar.id[:8]})")
            if match == "close":
                review_reasons.append("quote matches the user's words only approximately")
            # Automatic save only for a quote that lies exactly in text code confirmed as
            # the user's own. "close" is not enough: it allows a shifted window.
            if verify_quote(quote, own_text) != "exact":
                if has_unsure:
                    review_reasons.append("the quote is in text that code could not confirm as yours "
                                          "(it may be pasted)")
                elif match == "exact":
                    review_reasons.append("the quote is not inside your own text")
            category = (_str(cand.get("category")) or "other").lower()
            tier, privacy_reason, _ = PrivacyGate.classify(text, category=category)
            entry = MemoryEntry(
                text=text, category=category, tier=tier, source="chat",
                metadata={
                    "kind": "self", "quote": quote, "quote_match": match,
                    "source_ref": source_ref, "source_key": source_key,
                    "turn_hash": turn_hash[:12], "privacy_reason": privacy_reason,
                    "laya": report.laya, "support": round(share, 2),
                },
            )
            existing.append(entry)
            if review_reasons:
                self.vault.append_pending(entry, "; ".join(dict.fromkeys(review_reasons)))
                report.queued_for_review.append(text)
            else:
                self.vault.append_fact(entry)
                self.cache.upsert_entry(entry)
                report.saved.append(text)

    def _document_facts(self, label: str, text: str, report: IntakeReport) -> None:
        doc_hash = _sha("doc:" + text)
        if not self.cache.mark_seen(doc_hash, "document"):
            return
        try:
            self._document_facts_unsafe(label, text, doc_hash, report)
        except Exception as e:
            # Tool documents are not sent again in later turns: keep the document.
            error = e if isinstance(e, ExtractionError) else f"{type(e).__name__}: {e}"
            try:
                self.cache.unmark_seen(doc_hash)
                self.vault.append_failed_turn(doc_hash, text, str(error), kind="document", label=label)
            finally:
                self._safe_fail("document_extraction", str(error), {"source": label})
                report.errors.append(f"document {label}: {error}")

    def _document_facts_unsafe(self, label: str, text: str, doc_hash: str, report: IntakeReport) -> None:
        candidates = self.extractor.extract_document_facts(text, label)
        self.health.ok("document_extraction")
        if candidates:
            self.vault.save_source(doc_hash[:32], text, label)
        seen_text = text[:DOC_MAX_CHARS]   # the part the model read
        for cand in candidates[:5]:
            if not isinstance(cand, dict):
                continue
            fact, quote = _str(cand.get("text")), _str(cand.get("quote"))
            about = _str(cand.get("about")) or "general"
            if not fact:
                continue
            match = verify_quote(quote, seen_text)
            if match is None:
                self._reject(report, fact, f"quote not found in {label}: {quote!r}", component="document_quote_check")
                continue
            review_reasons = []
            share = support_share(fact, quote, ignore=about)
            if share < MIN_SUPPORT:
                if not _cross_language(fact, quote):
                    self._reject(report, fact, f"fact is not supported by its quote: {quote!r}",
                                 component="document_quote_check")
                    continue
                review_reasons.append("fact and quote are in different languages; code cannot check the fact")
            elif share < AUTO_SAVE_SUPPORT:
                review_reasons.append(f"the fact adds words that are not in its quote ({share:.0%} supported)")
            if _negation_differs(fact, quote):
                review_reasons.append("the fact and its quote differ in negation (not, never, can't, 안)")
            if match == "close":
                review_reasons.append("quote matches the document only approximately")
            if _INSTRUCTION_LIKE.search(fact) or _INSTRUCTION_LIKE.search(quote):
                review_reasons.append("document text that reads like an instruction to the assistant")
            entry = MemoryEntry(
                text=fact, category=f"about:{about}", tier=PrivacyTier.TIER_2_PRIVATE, source=f"doc:{label}",
                metadata={"kind": "document", "about": about, "quote": quote, "quote_match": match,
                          "source_ref": label, "source_key": doc_hash[:32], "support": round(share, 2)},
            )
            if review_reasons:
                self.vault.append_pending(entry, "; ".join(dict.fromkeys(review_reasons)))
                report.queued_for_review.append(fact)
            else:
                self.vault.append_document_fact(entry)
                self.cache.upsert_entry(entry)
                report.document_facts.append(fact)

    def _reject(self, report: IntakeReport, text: str, reason: str, component: str = "quote_check") -> None:
        report.rejected.append({"text": text, "reason": reason})
        self.health.note(component, f"rejected {text!r}: {reason}")

    def _safe_fail(self, component: str, error: str, details: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.health.fail(component, error, details)
        except Exception as e:
            logger.warning(f"[memory] {component} FAILED ({error}) and health.json could not be written: {e}")

    def _fail_turn(self, turn_hash: str, user_text: str, component: str, error: str, report: IntakeReport) -> None:
        report.status, report.reason = "failed", error
        report.errors.append(error)
        try:
            # Forget the turn hash so that `python -m telemachus.memory retry` can run it again.
            self.cache.unmark_seen(turn_hash)
            self.vault.append_failed_turn(turn_hash, user_text, error)
        except Exception as e:
            logger.warning(f"[memory] could not keep the failed turn for retry: {e}. Turn text: {user_text[:200]!r}")
        self._safe_fail(component, error)

    # ----- retry --------------------------------------------------------------------

    def retry_failed(self) -> List[IntakeReport]:
        """Runs failed turns and documents again.

        The old record is removed only after its retry has finished, so a stop in the
        middle (Ctrl-C during a slow LLM call) keeps it. A new failure writes a new
        record with a new record_id.
        """
        reports = []
        for item in self.vault.load_failed_turns():
            if item.get("kind") == "document":
                report = IntakeReport()
                self.cache.unmark_seen(item["turn_hash"])
                self._document_facts(item.get("label") or "document", item["user_text"], report)
                if report.errors:
                    report.status, report.reason = "failed", report.errors[-1]
            else:
                report = self.process_turn([{"role": "user", "content": item["user_text"]}], force=True)
            self.vault.clear_failed_records([item.get("record_id")])
            reports.append(report)
        return reports
