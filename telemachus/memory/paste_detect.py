"""Deterministic detection of pasted material inside a user chat message.

A user message often holds two things: the user's own instruction ("help me reply to
this") and pasted material (an email, notes, a log). Only the user's own words may
become facts about the user. This module splits a message into blocks and marks each
block OWN, PASTED or UNSURE with rules that need no model.

Two kinds of evidence, with different results:

- STRONG markers prove a paste: mail headers (English and Korean), "Original Message" /
  "Forwarded message", "On ... wrote:", quoted ">" lines, chat-log timestamps, log
  lines, code fences, document headings, opencode file attachments. The block is PASTED
  and so is everything after it (people type the instruction first and paste below).
  A short request at the very end ("what should I reply?") is UNSURE: a pasted email
  often ends with a request too.
- WEAK signs only raise doubt: a letter greeting, a sign-off with a name, a greeting
  to another person, a long block, text next to a request, a message over the length
  budget. Such blocks are UNSURE.

How the intake uses the labels: PASTED text only gives facts about other people.
UNSURE text can give facts about the user, but they always go to the review list.
Only facts quoted from OWN text are saved without review. A wrong guess is then never
silent: doubtful text is neither lost nor saved without the user's decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import List, Optional

OWN = "own"
PASTED = "pasted"
UNSURE = "unsure"

# ----- strong markers (one line each) ---------------------------------------------------

_HEADER = re.compile(r"^\s*(from|to|cc|bcc|sent|date|subject|reply-to)\s*:\s*\S", re.I)
_KO_HEADER = re.compile(r"^\s*(보낸\s*사람|받는\s*사람|참조|숨은\s*참조|제목|날짜|보낸\s*날짜)\s*:\s*\S")
_ORIGINAL = re.compile(r"^\s*-{2,}\s*(original message|forwarded message|원본 메시지|전달된 메시지)", re.I)
_WROTE = re.compile(r"^\s*(on\s.{4,120}\swrote:|.{2,80}님이 작성:)\s*$", re.I)
_QUOTE = re.compile(r"^\s*>")
_CHATLOG = re.compile(
    r"^\s*\[(\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?|오전\s*\d{1,2}:\d{2}|오후\s*\d{1,2}:\d{2})\]"
    r"|^\s*\[(카카오톡|kakaotalk)\]"                       # KakaoTalk export line
    r"|^\s*\S{1,20}\s+(오전|오후)\s*\d{1,2}:\d{2}\s*$",     # "김민재 오후 2:31"
    re.I,
)
# Slack "Dana Park 10:02 AM". Case-sensitive: the name needs capital letters, so a
# note like "lunch with dana 12:30 pm" is not a chat log.
_SLACK = re.compile(r"^\s*[A-Z][\w.'-]*(\s+[A-Z][\w.'-]*){0,2}\s+\d{1,2}:\d{2}\s*(AM|PM|am|pm)\s*$")
_LOGLINE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}[ t]\d{2}:\d{2}:\d{2}")
_FENCE = re.compile(r"^\s*```")
_DOC_HEADING = re.compile(
    r"^\s*(meeting notes|minutes|attendees|agenda|section\s+\d|article\s+\d|회의록|참석자|제\s*\d+\s*조)", re.I)
# opencode puts @file attachments into the user message as text: a "Called the Read
# tool" line, <file> tags, and "00001| " line numbers.
_OPENCODE_FILE = re.compile(r"^\s*(</?file>|\d{5}\| |called the \w+ tool with the following input)", re.I)

# ----- weak signs (block level) -----------------------------------------------------------

_SALUTATION = re.compile(
    r"^\s*((?i:dear\s+\S|hi\s+(all|team|everyone)\b|hello\s+(all|team)\b)|안녕하세요"
    # "Hi Jordan," / "Hello Ms. Kim" alone on its line: the first line of a letter.
    # The name must start with a capital letter, so this part is case-sensitive.
    r"|(?i:hi|hello|hey|good morning)\s+[A-Z][\w.'-]*(\s+[A-Z][\w.'-]*){0,2},?\s*$"
    r"|\S{1,10}\s*님,?\s*$)",
)
_CLOSING = re.compile(
    r"^\s*(best( regards)?|kind regards|regards|sincerely|thanks|thank you|cheers|감사합니다|감사드립니다)[,.!]?\s*$",
    re.I,
)
# A name on its own line: one to three words, no sentence punctuation.
_NAME_LINE = re.compile(r"^\s*[^\s.?!:]{1,20}(\s+[^\s.?!:]{1,20}){0,2}\s*$")
# Anchored and bounded: an unanchored "\S+...$" takes quadratic time on a long line.
_KO_SIGNOFF = re.compile(r"^\s*\S{1,20}\s*(드림|올림)\s*$")
# "Hi Leo, great to meet you": a message written to another person. Greetings to the
# assistant itself, or to nobody in particular, are the user's own.
_GREETING_TO_PERSON = re.compile(r"^\s*(?i:hi|hello|hey|dear|good morning)\s+([A-Z][\w.'-]*)\s*[,!]")
ASSISTANT_NAMES = {"claude", "telemachus", "qwen", "assistant", "there", "gpt", "chatgpt", "all", "team", "everyone"}

# A request to the assistant: ends with "?" or ":", starts with a request verb, or ends
# with a Korean request ending.
_REQUEST = re.compile(
    r"([?:]\s*$"
    r"|^\s*(hi claude,?\s*)?(can you|could you|would you|please|pls|reply|respond|answer|draft|write|rewrite|"
    r"summari[sz]e|translate|fix|tighten|shorten|explain|check|review|help|make|turn|what|how|why|is|does|tell|thoughts|"
    r"add|save|remember|note|keep|put|log)\b"
    r"|(줘|줄래|주세요|해봐|알려|뭐야|뭐지|뭐예요|할까|될까)[.!?]?(\s|$)"
    r"|저장|기억해|메모해)",
    re.I,
)
# ...that points at other text: "reply to this:", "what does this clause mean?".
_REFERENCE = re.compile(
    r"(:\s*$|\b(this|these|the following|below|above|attached|reply|respond|answer)\b"
    r"|이거|이것|이걸|이 메일|이 글|이 내용|이 문장|아래|위의|답장|답변)",
    re.I,
)
REQUEST_MAX_CHARS = 300

# A chat message to an assistant is short. Longer blocks without markers are UNSURE,
# and so is unmarked text after the first OWN_TOTAL_MAX_CHARS of a message.
OWN_MAX_CHARS = 400
OWN_TOTAL_MAX_CHARS = 800


@dataclass
class Block:
    text: str
    label: str
    reasons: List[str] = field(default_factory=list)


@dataclass
class PasteAnalysis:
    blocks: List[Block]

    def _join(self, label: str) -> str:
        return "\n\n".join(b.text for b in self.blocks if b.label == label)

    @property
    def own_text(self) -> str:
        return self._join(OWN)

    @property
    def pasted_text(self) -> str:
        return self._join(PASTED)

    @property
    def unsure_text(self) -> str:
        return self._join(UNSURE)

    @property
    def has_paste(self) -> bool:
        return any(b.label == PASTED for b in self.blocks)


def _strong_markers(line: str) -> List[str]:
    found = []
    for name, pat in (
        ("mail_header", _HEADER), ("mail_header_ko", _KO_HEADER), ("original_message", _ORIGINAL),
        ("wrote_line", _WROTE), ("quote_line", _QUOTE), ("chat_log", _CHATLOG), ("chat_log", _SLACK),
        ("log_line", _LOGLINE), ("code_fence", _FENCE), ("doc_heading", _DOC_HEADING),
        ("opencode_file", _OPENCODE_FILE),
    ):
        if pat.search(line) and name not in found:
            found.append(name)
    return found


def _has_named_closing(lines: List[str]) -> bool:
    """A sign-off followed by a name ("Thanks,\\nDana") or a Korean "X 드림" line.

    Also with a title line: "Sincerely / Helen Brooks / Director of Partnerships", and a
    Korean sign-off one line above the end: "박서윤 드림 / 누리반도체 품질팀".
    A lone "thanks" or "감사합니다" is ordinary politeness in a chat and does not count.
    """
    if any(_KO_SIGNOFF.search(l) for l in lines[-2:]):
        return True
    for k in (2, 3):
        if len(lines) >= k and _CLOSING.search(lines[-k]) and all(
                _NAME_LINE.search(l) or len(l) <= 40 for l in lines[-k + 1:]):
            return True
    return False


def _weak_signs(block: str) -> List[str]:
    lines = [l for l in block.splitlines() if l.strip()]
    signs: List[str] = []
    if not lines:
        return signs
    if _SALUTATION.search(lines[0]):
        signs.append("salutation")
    m = _GREETING_TO_PERSON.search(lines[0])
    if m and m.group(1).lower() not in ASSISTANT_NAMES:
        signs.append("greeting_to_person")
    if _has_named_closing(lines):
        signs.append("named_closing")
    return signs


def _is_request(text: str) -> bool:
    return len(text) <= REQUEST_MAX_CHARS and bool(_REQUEST.search(text))


def _split_blocks(text: str) -> List[str]:
    return [b.strip("\n") for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]


def _first_strong_line(lines: List[str]) -> Optional[int]:
    return next((i for i, l in enumerate(lines) if _strong_markers(l)), None)


def analyze(text: str) -> PasteAnalysis:
    """Labels each block of a user message as OWN, PASTED or UNSURE."""
    blocks: List[Block] = []
    in_paste = False
    raw_blocks = _split_blocks(text)
    for idx, raw in enumerate(raw_blocks):
        lines = raw.splitlines()
        if in_paste:
            # A short request at the very end may be the user's own question, or the last
            # line of the pasted email ("Please send the PO by Friday."). UNSURE: review.
            last = idx == len(raw_blocks) - 1
            if last and len(raw) <= REQUEST_MAX_CHARS and not _strong_markers(lines[0]) and (
                    _is_request(raw) or _REFERENCE.search(raw)):
                blocks.append(Block(raw, UNSURE, ["request_after_paste"]))
            else:
                blocks.append(Block(raw, PASTED, ["after_paste_marker"]))
            continue
        first = _first_strong_line(lines)
        if first is not None:
            # "reply to this\nFrom: Dana ...": the lines before the marker are the user's.
            head = "\n".join(lines[:first]).strip()
            if head:
                blocks.extend(_classify_unmarked(head))
            body = "\n".join(lines[first:])
            blocks.append(Block(body, PASTED, _strong_markers(lines[first])))
            in_paste = True
            continue
        blocks.extend(_classify_unmarked(raw))

    # A signed letter without headers: the blocks above a named closing are its body.
    # They become UNSURE (review), not PASTED, so a wrong guess loses nothing.
    # When the signed block starts with its own greeting, the letter is that block only.
    for i, b in enumerate(blocks):
        if "named_closing" in b.reasons and not (
                "salutation" in b.reasons or "greeting_to_person" in b.reasons):
            for j in range(i - 1, -1, -1):
                if blocks[j].label != OWN or "request" in blocks[j].reasons:
                    break
                blocks[j] = Block(blocks[j].text, UNSURE, blocks[j].reasons + ["above_named_closing"])

    # A letter greeting starts a letter: the blocks below it are its body.
    for i, b in enumerate(blocks):
        if "salutation" in b.reasons or "greeting_to_person" in b.reasons:
            for j in range(i + 1, len(blocks)):
                if blocks[j].label != OWN or "request" in blocks[j].reasons:
                    break
                blocks[j] = Block(blocks[j].text, UNSURE, blocks[j].reasons + ["below_salutation"])

    # Text next to a request is usually the material the request is about:
    # "<pasted text>\n\nthoughts?" or "can you help with this?\n\n<context>".
    own = [i for i, b in enumerate(blocks) if b.label == OWN]
    requests = [i for i in own if "request" in blocks[i].reasons]
    # Two requests ("reply to this:" + a pasted "Can we push to Friday?"): the one that
    # points at other text is the user's; the others are material.
    pointing = [i for i in requests if _REFERENCE.search(blocks[i].text)]
    if pointing and len(requests) > len(pointing):
        requests = pointing
    if requests and len(blocks) >= 2:
        for i in own:
            if i not in requests:
                blocks[i] = Block(blocks[i].text, UNSURE, blocks[i].reasons + ["beside_request"])

    # Length budget for the whole message: many short unmarked paragraphs are a paste.
    own = [i for i, b in enumerate(blocks) if b.label == OWN]
    if sum(len(blocks[i].text) for i in own) > OWN_TOTAL_MAX_CHARS:
        for i in own[1:]:
            blocks[i] = Block(blocks[i].text, UNSURE, blocks[i].reasons + ["over_message_budget"])
    return PasteAnalysis(blocks)


def _classify_unmarked(block: str) -> List[Block]:
    """One block without strong markers -> one or two blocks."""
    lines = [l for l in block.splitlines() if l.strip()]
    # "reply to this pls\n<pasted lines>" typed with single line breaks (Shift+Enter).
    if len(lines) >= 2 and _is_request(lines[0]) and _REFERENCE.search(lines[0]):
        rest = "\n".join(lines[1:])
        return [Block(lines[0], OWN, ["short_chat_message", "request"]),
                Block(rest, UNSURE, ["lines_after_request"])]
    if len(lines) >= 2 and _is_request(lines[-1]) and _REFERENCE.search(lines[-1]):
        head = "\n".join(lines[:-1])
        return [Block(head, UNSURE, ["lines_before_request"]),
                Block(lines[-1], OWN, ["short_chat_message", "request"])]
    signs = _weak_signs(block)
    if signs:
        return [Block(block, UNSURE, signs)]
    if len(block) > OWN_MAX_CHARS:
        return [Block(block, UNSURE, ["long_without_markers"])]
    if len(lines) >= 5:
        return [Block(block, UNSURE, ["many_lines_without_markers"])]
    reasons = ["short_chat_message"]
    if _is_request(block) and (_REFERENCE.search(block) or len(block) <= 80):
        reasons.append("request")
    return [Block(block, OWN, reasons)]
