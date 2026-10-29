"""Memory tab (Memory.dc.html). Read-only view of the memory vault.

Top: tier counts with a proportion bar, the search line and the tier filter.
RECENT: newest facts (or search matches) with a selection. Bottom: the selected fact in full.

Keys: j/k move, / search (live filter while typing; enter keeps it, esc clears it),
enter opens the full fact, f cycles the tier filter (all, private, project, public).

The search is a live filter over the loaded facts (every word typed must start a word of the
fact, its quote or its category), plus the cache's full-text search for older facts that were not
loaded. Nothing here writes to the vault.

While the search line is active the TUI must send every key here (see captures_keys()).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from telemachus.cockpit import view_common as vc
from telemachus.cockpit.shell import Frame, KeyGroup, KeyHint, Panels
from telemachus.cockpit.theme import GLYPHS, GUTTER, Line, cell_width, clip, fit, line_width, rpad, section, spread

LOAD_LIMIT = 2000  # newest facts kept in memory for the live filter
TIERS = ("all", "private", "project", "public")
TIER_SHORT = {"private": "PRV", "project": "PRJ", "public": "PUB"}
TIER_STYLE = {"private": "magenta", "project": "blue", "public": "green"}

MEMORY_KEYS: List[KeyGroup] = [
    KeyGroup.of(
        "LISTS",
        KeyHint("j/k", "move", "move through the facts"),
        KeyHint("/", "search", "search facts (live filter)"),
        KeyHint("↵", "open", "show the whole fact"),
        KeyHint("f", "filter tier", "filter by tier: all, private, project, public"),
    ),
]
SEARCH_KEYS: List[KeyGroup] = [
    KeyGroup.of(
        "SEARCH",
        KeyHint("type", "filter", "type to filter the facts"),
        KeyHint("↵", "keep", "keep the filter and move in the list"),
        KeyHint("esc", "clear", "clear the search"),
    ),
]


@dataclass
class MemoryData:
    memory_stats: Dict[str, Any] = field(default_factory=dict)  # SafetyMemoryPanel.get_memory_stats()
    memory_events: List[Dict[str, Any]] = field(default_factory=list)  # SafetyMemoryPanel.get_recent_events()
    facts: List[Dict[str, Any]] = field(default_factory=list)  # newest first (safety_memory_panel.fact_row)
    pending: Optional[int] = None  # facts waiting for review
    searcher: Optional[Callable[[str, int], List[Dict[str, Any]]]] = None  # full-text search (cache)
    now: float = 0.0


def load(panels: Panels) -> MemoryData:
    """Reads what this tab shows. Called by the TUI on each data refresh."""
    sp = panels.safety_panel
    get_facts = getattr(sp, "get_recent_facts", None)
    get_pending = getattr(sp, "get_pending_count", None)
    return MemoryData(
        memory_stats=dict(sp.get_memory_stats()),
        memory_events=list(sp.get_recent_events(limit=8)),
        facts=list(get_facts(limit=LOAD_LIMIT)) if callable(get_facts) else [],
        pending=get_pending() if callable(get_pending) else None,
        searcher=getattr(sp, "search_facts", None),
        now=time.time(),
    )


# ---------------------------------------------------------------------------
# Filtering (pure)
# ---------------------------------------------------------------------------

_WORD = re.compile(r"\w+")


def _words(fact: Dict[str, Any]) -> List[str]:
    text = " ".join(str(fact.get(k) or "") for k in ("text", "quote", "about", "category"))
    return _WORD.findall(text.lower())


def matches(fact: Dict[str, Any], query: str) -> bool:
    """Every word of the query starts some word of the fact (so 'pre dark' finds
    'prefers dark Tokyo Night colors')."""
    terms = _WORD.findall(query.lower())
    if not terms:
        return True
    words = _words(fact)
    return all(any(w.startswith(t) for w in words) for t in terms)


def visible_facts(d: MemoryData, state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The facts the list shows: tier filter, then the search (live filter + full-text extras)."""
    tier = state.get("tier", "all")
    query = str(state.get("query", "")).strip()
    facts = d.facts
    if query:
        facts = [f for f in facts if matches(f, query)]
        seen = {f.get("id") for f in facts}
        for f in state.get("extra", []) or []:
            if f.get("id") not in seen and matches(f, query):
                facts.append(f)
                seen.add(f.get("id"))
    if tier != "all":
        facts = [f for f in facts if f.get("tier") == tier]
    return facts


def captures_keys(state: Dict[str, Any]) -> bool:
    """True while the search line takes typing: the TUI should send every key here."""
    return bool(state.get("searching"))


def _run_fulltext(state: Dict[str, Any], d: MemoryData) -> None:
    """Adds full-text matches for facts older than the loaded ones (only when some were not loaded)."""
    state["extra"] = []
    query = str(state.get("query", "")).strip()
    total = (d.memory_stats or {}).get("total_memories") or 0
    if not query or d.searcher is None or total <= len(d.facts) or len(query) < 2:
        return
    try:
        state["extra"] = list(d.searcher(query, 50))
    except Exception:
        state["extra"] = []


# ---------------------------------------------------------------------------
# Keys and clicks
# ---------------------------------------------------------------------------


def _typed_text(ch: int, state: Dict[str, Any]) -> Optional[str]:
    """Text for one getch() value while searching. getch() gives UTF-8 bytes one at a time, so
    "é" or Korean arrive as a lead byte (0xC0-0xF7) and continuation bytes (0x80-0xBF); they
    are collected in state["_utf8"] (like FinanceModals._decode). None = not text (yet)."""
    from telemachus.cockpit.finance_modals import _typed_char_ok

    if 32 <= ch <= 126:
        state["_utf8"] = []
        return chr(ch)
    if 0xC0 <= ch <= 0xF7:
        state["_utf8"] = [ch]
        return None
    buf = state.get("_utf8") or []
    if 0x80 <= ch <= 0xBF and buf:
        buf = buf + [ch]
        lead = buf[0]
        need = 1 if lead < 0xE0 else (2 if lead < 0xF0 else 3)
        if len(buf) - 1 < need:
            state["_utf8"] = buf
            return None
        state["_utf8"] = []
        try:
            text = bytes(buf).decode("utf-8")
        except UnicodeDecodeError:
            return None
        return text if _typed_char_ok(text) else None
    state["_utf8"] = []
    return None


def _fact_key(f: Dict[str, Any]) -> str:
    """Stable id of one fact: its vault id, or its text and time for rows without one."""
    fid = f.get("id")
    return f"id:{fid}" if fid else f"t:{f.get('ts', '')}|{f.get('text', '')}"


def _selection(state: Dict[str, Any], keys: List[str]) -> Tuple[int, bool]:
    """(selected index, lost). The selection follows the selected fact by its key, so facts
    added above it on a refresh do not move the highlight. lost = that fact is no longer in
    the list; the index is then kept (clamped) and the open fact closes."""
    n = len(keys)
    try:
        sel = int(state.get("sel", 0) or 0)
    except (TypeError, ValueError):
        sel = 0
    key = state.get("sel_key")
    if key is not None and key in keys:
        return keys.index(key), False
    return (max(0, min(sel, n - 1)) if n else 0), key is not None


def _select(state: Dict[str, Any], keys: List[str], idx: int) -> None:
    state["sel"] = idx
    state["sel_key"] = keys[idx] if 0 <= idx < len(keys) else None


def _move_selection(ch: int, state: Dict[str, Any], d: MemoryData) -> Optional[int]:
    """Moves the selection for a list key. Returns the old index when ch was a move key, else None."""
    keys = [_fact_key(f) for f in visible_facts(d, state)]
    sel, lost = _selection(state, keys)
    if lost:
        state["open"] = False
    new = vc.move(ch, sel, len(keys))
    if new is None:
        _select(state, keys, sel)
        return None
    _select(state, keys, new)
    return sel


def handle_key(ch: int, state: Dict[str, Any], d: MemoryData) -> Optional[str]:
    if state.get("searching"):
        q = str(state.get("query", ""))
        if ch == vc.ESC:
            state.update(searching=False, query="", extra=[], sel=0, sel_key=None, scroll=0)
        elif ch in vc.ENTER_KEYS:
            state["searching"] = False
        elif ch in vc.BACKSPACE_KEYS:
            state["query"] = q[:-1]
            state.update(sel=0, sel_key=None)
            _run_fulltext(state, d)
        elif ch in vc.UP_KEYS[:1] + vc.DOWN_KEYS[:1]:  # arrow keys still move while typing
            _move_selection(ch, state, d)
        else:
            text = _typed_text(ch, state)
            if text and len(q) < 60:
                state["query"] = q + text
                state.update(sel=0, sel_key=None)
                _run_fulltext(state, d)
        return None

    old = _move_selection(ch, state, d)
    n = len(visible_facts(d, state))
    if old is not None:
        if state.get("sel") != old:
            state["open"] = False
        return None
    if ch == ord("/"):
        state["searching"] = True
        state["open"] = False
        return None
    if ch in vc.ENTER_KEYS:
        if n:
            state["open"] = not state.get("open", False)
        return None
    if ch == vc.ESC:
        if state.get("open"):
            state["open"] = False
        elif state.get("query"):
            state.update(query="", extra=[], sel=0, sel_key=None, scroll=0)
        return None
    if ch in (ord("f"), ord("F")):
        cur = state.get("tier", "all")
        state["tier"] = TIERS[(TIERS.index(cur) + 1) % len(TIERS)] if cur in TIERS else "all"
        state.update(sel=0, sel_key=None, scroll=0, open=False)
        return None
    return None


def handle_click(target: Tuple[str, int], state: Dict[str, Any], d: MemoryData) -> Optional[str]:
    kind, idx = target
    if kind == "fact":
        keys = [_fact_key(f) for f in visible_facts(d, state)]
        if not 0 <= idx < len(keys):
            return None
        if _selection(state, keys)[0] == idx:
            state["open"] = not state.get("open", False)
        else:
            state["open"] = False
        _select(state, keys, idx)
    elif kind == "search":
        state["searching"] = True
    elif kind == "tier":
        return handle_key(ord("f"), state, d)
    return None


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _counts(st: Dict[str, Any]) -> Tuple[int, int, int, int]:
    tiers = st.get("by_tier", {}) or {}
    prv = int(tiers.get("tier_2_private", 0) or 0)
    prj = int(tiers.get("tier_1_project", 0) or 0)
    pub = int(tiers.get("tier_0_public", 0) or 0)
    total = int(st.get("total_memories", prv + prj + pub) or 0)
    return total, prv, prj, pub


def _summary_lines(d: MemoryData, width: int) -> List[Line]:
    st = d.memory_stats or {}
    total, prv, prj, pub = _counts(st)
    head: Line = [
        (GUTTER, "text"),
        (f"PRV {prv:,}", "magenta+b"), ("   ", "text"),
        (f"PRJ {prj:,}", "blue+b"), ("   ", "text"),
        (f"PUB {pub:,}", "green+b"),
        (f"   {total:,} facts", "muted"),
    ]
    if d.pending:
        review: Line = [("   ", "text"), (f"{d.pending} to review", "yellow")]
        hint: Line = [("  ·  ", "dim"), ("run ", "muted"), ("python3 -m telemachus.memory review", "cyan")]
        if line_width(head) + line_width(review) <= width:
            head += review
            if line_width(head) + line_width(hint) <= width:
                head += hint
    lines = [fit(head, width)]
    bar_w = max(1, width - 4)
    lines.append(fit([(GUTTER, "text")] + vc.bar([(prv, "magenta"), (prj, "blue"), (pub, "green")], bar_w), width))
    return lines


def _search_line(state: Dict[str, Any], width: int, found: int) -> Line:
    tier = state.get("tier", "all")
    query = str(state.get("query", ""))
    right: Line = [("tier: ", "muted"), (tier, "cyan" if tier != "all" else "text"), ("  ", "text")]
    if state.get("searching"):
        left: Line = [(GUTTER, "text"), ("/ ", "cyan+b"), (query, "text+b"), ("▏", "cyan")]
        if query:
            left.append((f"   {found:,} found", "muted"))
    elif query:
        left = [(GUTTER, "text"), ("/ ", "cyan"), (query, "text"), (f"   {found:,} found", "muted"),
                ("   esc clears", "muted")]
    else:
        left = [(GUTTER, "text"), ("/", "cyan"), (" search facts", "muted")]
    return spread(left, right, width)


def _meta_lines(f: Dict[str, Any], width: int, full: bool = False) -> List[Line]:
    """'private   source opencode chat   saved 10/28 14:02   used 6 times'; wraps to a second line
    when open (category and id are added then)."""
    parts: List[str] = []
    if f.get("source"):
        parts.append("source " + str(f["source"]))
    saved = vc.safe_dt(f.get("ts"))
    if saved is not None:
        parts.append("saved " + saved.strftime("%m/%d %H:%M"))
    uses = int(f.get("uses") or 0)
    parts.append("used once" if uses == 1 else f"used {uses} times")
    if f.get("pinned"):
        parts.append("pinned")
    if f.get("about"):
        parts.append("about " + str(f["about"]))
    if full:
        if f.get("category"):
            parts.append("category " + str(f["category"]))
        if f.get("id"):
            parts.append("id " + str(f["id"])[:8])
    tier = f.get("tier", "private")
    lines: List[Line] = []
    cur: Line = [(GUTTER, "text"), (tier, TIER_STYLE.get(tier, "magenta"))]
    for part in parts:
        seg = ("   " + part, "muted")
        if line_width(cur) + cell_width(seg[0]) > width and len(cur) > 2 and full and len(lines) == 0:
            lines.append(fit(cur, width))
            cur = [(GUTTER + " " * len(tier), "text"), seg]
        else:
            cur.append(seg)
    lines.append(fit(cur, width))
    return lines


def build_memory(d: MemoryData, state: Optional[Dict[str, Any]], width: int, height: int) -> Frame:
    """Body lines. `state` is this tab's dict kept by the TUI (selection, scroll, search)."""
    state = dict(state) if state is not None else {}
    st = d.memory_stats or {}
    keys = SEARCH_KEYS if state.get("searching") else MEMORY_KEYS
    if not st.get("available", True):
        lines = [section("MEMORY", width, "~/.memory"), [],
                 fit([(GUTTER + "The memory vault could not be read.", "yellow")], width)]
        return Frame(lines=lines[:height], keys=keys)

    now = vc.now_or(d.now)
    facts = visible_facts(d, state)
    rows: Dict[int, Tuple[str, int]] = {}
    lines: List[Line] = _summary_lines(d, width)
    rows[len(lines)] = ("search", 0)
    lines.append(_search_line(state, width, len(facts)))
    lines.append([])

    total = _counts(st)[0]
    title = "RESULTS" if state.get("query") else "RECENT"
    trailing = f"{len(facts):,} of {total:,}" if facts else ""
    lines.append(section(title, width, trailing))

    if not facts:
        lines.append([])
        if state.get("query"):
            lines += vc.empty_state(f'No fact matches "{state.get("query")}".', width,
                                    extra="Press esc to clear the search.")
        elif state.get("tier", "all") != "all":
            lines += vc.empty_state(f"No {state.get('tier')} facts.", width, extra="Press f to change the tier.")
        else:
            lines += vc.empty_state("No memories yet.", width,
                                    extra="Facts appear here after your first OpenCode session.")
        return Frame(lines=[fit(l, width) for l in lines[:height]], keys=keys, rows=rows,
                     state={"sel": 0, "scroll": 0, "sel_key": None})

    fact_keys = [_fact_key(f) for f in facts]
    want, lost = _selection(state, fact_keys)
    if lost:
        state["open"] = False
    inner = width - 2
    cat_w = 12 if inner >= 60 else 0
    head = rpad("TIME", 7) + rpad("TIER", 6) + (rpad("CATEGORY", cat_w) if cat_w else "") + "FACT"
    lines.append(fit([(GUTTER + head, "muted")], width))

    # Detail area under the list: rule + fact text (wrapped) + meta line.
    detail_text_lines = 6 if state.get("open") else 2
    detail_h = 1 + detail_text_lines + (2 if state.get("open") else 1)
    list_rows = height - len(lines) - detail_h
    if list_rows < 3:
        detail_text_lines = max(1, detail_text_lines - (3 - list_rows))
        detail_h = 1 + detail_text_lines + (2 if state.get("open") else 1)
        list_rows = max(1, height - len(lines) - detail_h)
    sel, offset = vc.window(len(facts), want, state.get("scroll", 0), list_rows)
    for i, f in enumerate(facts[offset: offset + list_rows]):
        idx = offset + i
        tier = f.get("tier", "private")
        selected = idx == sel
        body: Line = [
            (rpad(vc.clock(f.get("ts"), now), 7), "text" if selected else "muted"),
            (rpad(TIER_SHORT.get(tier, "PRV"), 6), TIER_STYLE.get(tier, "magenta")),
        ]
        if cat_w:
            body.append((rpad(str(f.get("category", "")), cat_w - 1) + " ", "muted"))
        text = str(f.get("text", ""))
        if f.get("pinned"):
            text = GLYPHS["ok"] + " " + text
        room = width - 2 - line_width(body) - 1
        if cell_width(text) > room:
            text = clip(text, max(0, room - 1)).rstrip() + "…"
        body.append((text, "text+b" if selected else "text"))
        rows[len(lines)] = ("fact", idx)
        lines.append(vc.row(body, width, selected))

    # The selected fact in full, right under the list.
    f = facts[sel]
    lines.append(fit([(GUTTER + "─" * max(0, width - 4), "dim")], width))
    text = str(f.get("text", ""))
    if state.get("open") and f.get("quote") and f.get("quote") != text:
        text += f'   (quote: "{f.get("quote")}")'
    for ln in vc.wrap(text, width - 4, detail_text_lines):
        lines.append(fit([(GUTTER + ln, "text")], width))
    lines += _meta_lines(f, width, bool(state.get("open")))
    lines = [fit(l, width) for l in lines[:height]]
    out = {"sel": sel, "scroll": offset, "sel_key": fact_keys[sel]}
    if lost:
        out["open"] = False
    return Frame(lines=lines, keys=keys, rows={k: v for k, v in rows.items() if k < height},
                 state=out)


DATA_CLASS = MemoryData
build = build_memory
