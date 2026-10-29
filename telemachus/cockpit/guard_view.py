"""Guard tab (Guard.dc.html).

Status line, one row per check with a live number, and RECENT EVENTS from the guard event log
(telemachus.fs.guard_log: blocks, confirmations, allows, trash moves, git checkpoints).
Only measured numbers are shown; the intent-check time appears once the guard has recorded one.

Keys: j/k move, enter shows the full event, o opens the Trash.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from telemachus.cockpit import view_common as vc
from telemachus.cockpit.shell import Frame, KeyGroup, KeyHint, Panels
from telemachus.cockpit.theme import GLYPHS, GUTTER, Line, cell_width, clip, fit, line_width, rpad, section

GUARD_KEYS: List[KeyGroup] = [
    KeyGroup.of(
        "LISTS",
        KeyHint("j/k", "move", "move through the events"),
        KeyHint("↵", "details", "show the whole event"),
    ),
    KeyGroup.of(
        "GUARD",
        KeyHint("o", "open trash", "open the Trash in Finder"),
    ),
]

KIND_STYLE = {
    "BLOCK": "red+b",
    "CONFIRM": "yellow+b",
    "TRASH": "yellow",
    "CHECKPOINT": "blue",
    "ALLOW": "muted",
}
KIND_WORD = {"CONFIRM": "ASK"}


@dataclass
class GuardData:
    guard_status: Dict[str, Any] = field(default_factory=dict)  # SafetyMemoryPanel.get_guard_status()
    now: float = 0.0


def load(panels: Panels) -> GuardData:
    """Reads what this tab shows. Called by the TUI on each data refresh."""
    return GuardData(guard_status=dict(panels.safety_panel.get_guard_status()), now=time.time())


def _events(d: GuardData) -> List[Dict[str, Any]]:
    return list((d.guard_status or {}).get("events") or [])


def _event_key(ev: Dict[str, Any]) -> str:
    """Stable id of one event (the log has no id column)."""
    return "|".join(str(ev.get(k, "")) for k in ("ts", "kind", "target", "rule", "action"))


def _selection(state: Dict[str, Any], keys: List[str]) -> Tuple[int, bool]:
    """(selected index, lost). The selection follows the selected event by its key, so events
    logged above it on a refresh do not move the highlight. lost = that event is gone (rotated
    out); the index is then kept (clamped) and the open details close."""
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


def handle_key(ch: int, state: Dict[str, Any], d: GuardData) -> Optional[str]:
    keys = [_event_key(e) for e in _events(d)]
    n = len(keys)
    sel, lost = _selection(state, keys)
    if lost:
        state["open"] = False
    _select(state, keys, sel)
    new = vc.move(ch, sel, n)
    if new is not None:
        if new != sel:
            state["open"] = False
        _select(state, keys, new)
        return None
    if ch in vc.ENTER_KEYS:
        if n:
            state["open"] = not state.get("open", False)
        return None
    if ch == vc.ESC:
        state["open"] = False
        return None
    if ch in (ord("o"), ord("O")):
        return "open_trash"
    return None


def handle_click(target: Tuple[str, int], state: Dict[str, Any], d: GuardData) -> Optional[str]:
    kind, idx = target
    if kind == "event":
        keys = [_event_key(e) for e in _events(d)]
        if not 0 <= idx < len(keys):
            return None
        if _selection(state, keys)[0] == idx:
            state["open"] = not state.get("open", False)
        else:
            state["open"] = False
        _select(state, keys, idx)
    return None


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _status_line(g: Dict[str, Any], width: int, now: float) -> Line:
    checks = vc.guard_checks(g, now)
    on = sum(1 for c in checks if c["on"])
    log = g.get("log") or {}
    if on == len(checks):
        line: Line = [(GUTTER, "text"), (f"{GLYPHS['ok']} all {on} checks on", "green+b")]
    else:
        line = [(GUTTER, "text"), (f"{GLYPHS['off']} {on} of {len(checks)} checks on", "yellow+b")]
    extras: List[Line] = []
    if g.get("log_found"):
        blocked = log.get("blocked_today") or 0
        extras.append([(f"{blocked} blocked today", "red" if blocked else "muted")])
        last = log.get("last_event_ts")
        if last:
            extras.append([("last event ", "muted"), (vc.clock(last, now), "text")])
    else:
        extras.append([("no events logged yet", "muted")])
    for i, ex in enumerate(extras):
        sep: Line = [("      ", "text")] if i == 0 else [("   ·   ", "dim")]
        if line_width(line) + line_width(sep) + line_width(ex) > width:
            break
        line += sep + ex
    return fit(line, width)


def _check_rows(g: Dict[str, Any], width: int, now: float) -> List[Line]:
    inner = width - 2
    name_w, state_w = 20, 6
    num_w = 24 if inner >= 70 else 20
    note = inner >= 60
    out: List[Line] = []
    for c in vc.guard_checks(g, now):
        dot = (GLYPHS["ok"] + " ", "green") if c["on"] else (GLYPHS["off"] + " ", "yellow")
        row: Line = [
            (GUTTER, "text"), dot,
            (rpad(c["name"], name_w - 2), "text"),
            (rpad("on" if c["on"] else "off", state_w), "green" if c["on"] else "yellow"),
            (rpad(c["number"], num_w), "text"),
        ]
        if note:
            room = width - line_width(row)
            text = c["note"]
            if cell_width(text) > room:
                # keep whole items ("~/.ssh  ~/.gnupg") when the note is a list
                parts = text.split("  ")
                while len(parts) > 1 and cell_width("  ".join(parts)) > room:
                    parts.pop()
                text = "  ".join(parts)
                if cell_width(text) > room:
                    text = clip(text, max(0, room - 1)) + "…"
            row.append((text, "muted"))
        out.append(fit(row, width))
    return out


def _what(ev: Dict[str, Any]) -> Tuple[str, str]:
    """(main text, muted note) for the WHAT column."""
    kind = ev.get("kind", "")
    target = str(ev.get("target", ""))
    action = str(ev.get("action", ""))
    rule = str(ev.get("rule", ""))
    if kind == "CHECKPOINT":
        return target, str(ev.get("detail", ""))
    if kind == "TRASH":
        return target, "moved to Trash"
    main = f"{action} {target}".strip() if action and action not in ("execute",) else target
    if kind == "ALLOW":
        return main, ""
    return main, rule


def _event_row(ev: Dict[str, Any], width: int, selected: bool, now: float) -> Line:
    kind = str(ev.get("kind", ""))
    main, note = _what(ev)
    style = KIND_STYLE.get(kind, "muted")
    body: Line = [
        (rpad(vc.clock(ev.get("ts"), now, seconds=True), 10), "text" if selected else "muted"),
        (rpad(KIND_WORD.get(kind, kind), 12), style),
    ]
    main_style = "muted" if kind == "ALLOW" and not selected else ("text+b" if selected else "text")
    body.append((main, main_style))
    if note:
        body.append(("   " + note, "muted"))
    return vc.row(body, width, selected)


# Engine reasons that have a plain sentence. The detail line already starts with the verb
# ("Allowed:"), so these never repeat it ("Allowed: Permitted: ...").
_PLAIN_REASONS = (
    (re.compile(r"^Permitted: Passed Tier 0 boundaries\.?$"), "inside the safe paths."),
    (re.compile(r"^Read-only operation verified safe by Tier 0 boundaries\.?$"), "read only, inside the safe paths."),
    (re.compile(r"^Permitted by Laya: Intent match verified \(([\d.]+)\)\.?$"),
     r"the intent check says it matches your request (\1)."),
    (re.compile(r"^Permitted under Tier 2: Deletion will be routed through macOS Trash\.?$"),
     "the delete goes to the Trash, so it can be undone."),
)
# Engine prefixes dropped from any other reason.
_PREFIX = re.compile(r"^(?:Tier 0 Violation|Laya blocked action|Laya flagged excessive scope|Permitted[^:]{0,40}):\s*")


def _plain_reason(text: str) -> str:
    """The engine reason as plain words: known reasons get a plain sentence, and engine
    prefixes such as 'Tier 0 Violation: ' or 'Permitted by Laya: ' are dropped."""
    text = text.strip()
    for pattern, plain in _PLAIN_REASONS:
        if pattern.match(text):
            return pattern.sub(plain, text)
    m = _PREFIX.match(text)
    if m:
        text = text[m.end():]
        return text[:1].upper() + text[1:]
    return text


def _detail_lines(ev: Dict[str, Any], width: int, n: int, now: float) -> List[Line]:
    kind = str(ev.get("kind", ""))
    verb = {"BLOCK": "Blocked", "CONFIRM": "Needs your OK", "ALLOW": "Allowed", "TRASH": "Moved to Trash",
            "CHECKPOINT": "Checkpoint"}.get(kind, kind.title())
    detail = _plain_reason(str(ev.get("detail", "")))
    text = f"{verb}: {detail}" if detail else verb
    open_ = n > 2
    text_n = n - 1 if open_ else n
    lines = [fit([(GUTTER + ln, "text" if open_ else "muted")], width) for ln in vc.wrap(text, width - 4, max(1, text_n))]
    if open_:
        bits = []
        if ev.get("action"):
            bits.append("action " + str(ev["action"]))
        if ev.get("rule"):
            bits.append("rule " + str(ev["rule"]))
        if isinstance(ev.get("latency_ms"), (int, float)):
            bits.append(f"intent check {ev['latency_ms']:.0f} ms")
        when = vc.safe_dt(ev.get("ts"))
        if when is not None:
            bits.append(when.strftime("%b %d %H:%M:%S"))
        lines = lines[: n - 1]
        if cell_width(str(ev.get("target", ""))) > width - 26:  # the list row cut it: show it whole
            lines = lines[: n - 2] + [fit([(GUTTER + "target " + str(ev.get("target")), "muted")], width)]
        lines.append(fit([(GUTTER + "   ".join(bits), "muted")], width))
    return lines


def build_guard(d: GuardData, state: Optional[Dict[str, Any]], width: int, height: int) -> Frame:
    """Body lines. `state` is this tab's dict kept by the TUI (selection, scroll)."""
    state = dict(state) if state is not None else {}
    g = d.guard_status or {}
    now = vc.now_or(d.now)
    lines: List[Line] = [_status_line(g, width, now), [], section("CHECKS", width)]
    lines += _check_rows(g, width, now)
    lines.append([])
    events = _events(d)
    log = g.get("log") or {}
    rows: Dict[int, Tuple[str, int]] = {}
    if not events:
        lines.append(section("RECENT EVENTS", width))
        lines.append([])
        lines += vc.empty_state("No guard events yet.", width,
                                extra="Blocks, Trash moves and checkpoints from the fs tools show up here.")
        return Frame(lines=[fit(l, width) for l in lines[:height]], keys=GUARD_KEYS,
                     state={"sel": 0, "scroll": 0, "sel_key": None})

    total = int(log.get("total") or len(events))
    today = int(log.get("today") or 0)
    lines.append(section("RECENT EVENTS", width, f"{total:,} logged · {today:,} today"))
    lines.append(fit([(GUTTER + rpad("TIME", 10) + rpad("KIND", 12) + "WHAT", "muted")], width))
    keys = [_event_key(e) for e in events]
    want, lost = _selection(state, keys)
    if lost:
        state["open"] = False
    detail_n = 5 if state.get("open") else 2
    detail_h = 1 + detail_n
    list_rows = height - len(lines) - detail_h
    if list_rows < 3:
        detail_n = 1
        detail_h = 2
        list_rows = max(1, height - len(lines) - detail_h)
    sel, offset = vc.window(len(events), want, state.get("scroll", 0), list_rows)
    for i, ev in enumerate(events[offset: offset + list_rows]):
        idx = offset + i
        rows[len(lines)] = ("event", idx)
        lines.append(_event_row(ev, width, idx == sel, now))
    lines.append(fit([(GUTTER + "─" * max(0, width - 4), "dim")], width))
    lines += _detail_lines(events[sel], width, detail_n, now)
    lines = [fit(l, width) for l in lines[:height]]
    out = {"sel": sel, "scroll": offset, "sel_key": keys[sel]}
    if lost:
        out["open"] = False
    return Frame(lines=lines, keys=GUARD_KEYS, rows={k: v for k, v in rows.items() if k < height},
                 state=out)


DATA_CLASS = GuardData
build = build_guard
