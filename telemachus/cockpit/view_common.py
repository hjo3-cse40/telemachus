"""Small pure helpers shared by the Home, Memory and Guard views.

Time words, word wrap, list windows, selected rows, empty states and the guard check rows.
"""
from __future__ import annotations

from datetime import datetime
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from telemachus.fs.guard_log import clean_ts
from telemachus.cockpit.theme import GLYPHS, GUTTER, Line, cell_width, clip, fit, line_width, with_bg

ENTER_KEYS = (10, 13, 343)  # \n, \r, curses.KEY_ENTER
UP_KEYS = (259, ord("k"))  # curses.KEY_UP
DOWN_KEYS = (258, ord("j"))  # curses.KEY_DOWN
PAGE_UP = 339
PAGE_DOWN = 338
HOME_KEY = 262
END_KEY = 360
ESC = 27
BACKSPACE_KEYS = (8, 127, 263)  # ^H, DEL, curses.KEY_BACKSPACE


def safe_ts(ts: Any) -> Optional[float]:
    """A Unix time in seconds that datetime can show, or None (text, inf, nan, 0, far future).
    Millisecond stamps are scaled down. Data files are never trusted to hold a clean number."""
    return clean_ts(ts)


def safe_dt(ts: Any) -> Optional[datetime]:
    """datetime for a stored time, or None when it is not a usable time."""
    t = clean_ts(ts)
    return datetime.fromtimestamp(t) if t is not None else None


def now_or(ts: Optional[float]) -> float:
    return safe_ts(ts) or time.time()


def ago(ts: Optional[float], now: float) -> str:
    """'now', '12m', '3h', '2d', '5w'. Empty when unknown."""
    ts = safe_ts(ts)
    if ts is None:
        return ""
    s = max(0, int(now - ts))
    if s < 60:
        return "now"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    if s < 86400 * 14:
        return f"{s // 86400}d"
    return f"{s // (86400 * 7)}w"


def ago_words(ts: Optional[float], now: float) -> str:
    """'12m ago', 'just now'."""
    a = ago(ts, now)
    if not a:
        return ""
    return "just now" if a == "now" else a + " ago"


def clock(ts: Optional[float], now: float, seconds: bool = False) -> str:
    """'14:05' for today, '10/27' for an earlier day. Same width either way."""
    d = safe_dt(ts)
    if d is None:
        return "--:--:--" if seconds else "--:--"
    n = safe_dt(now) or datetime.now()
    if d.date() == n.date():
        return d.strftime("%H:%M:%S" if seconds else "%H:%M")
    return d.strftime("%b %d" if seconds else "%m/%d")


def wrap(text: str, width: int, max_lines: int) -> List[str]:
    """Word wrap in terminal cells. The last line ends with … when text was cut."""
    words = (text or "").split()
    if width <= 0 or max_lines <= 0 or not words:
        return []
    lines: List[str] = []
    cur = ""
    i = 0
    while i < len(words):
        w = words[i]
        cand = (cur + " " + w) if cur else w
        if cell_width(cand) <= width:
            cur = cand
            i += 1
            continue
        if not cur:  # one word longer than the line
            cur = clip(w, width)
            i += 1
        lines.append(cur)
        cur = ""
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if i < len(words) and lines:
        last = lines[-1]
        lines[-1] = clip(last, max(0, width - 1)).rstrip() + "…"
    return lines


def window(count: int, sel: int, offset: int, rows: int) -> Tuple[int, int]:
    """Clamped (selection, scroll offset) so the selection stays visible."""
    if count <= 0 or rows <= 0:
        return 0, 0
    sel = max(0, min(int(sel), count - 1))
    offset = max(0, min(int(offset), max(0, count - rows)))
    if sel < offset:
        offset = sel
    elif sel >= offset + rows:
        offset = sel - rows + 1
    return sel, offset


def move(ch: int, sel: int, count: int, page: int = 10) -> Optional[int]:
    """New selection for a list key, or None when the key is not a move key."""
    if count <= 0:
        return None
    if ch in UP_KEYS:
        return max(0, sel - 1)
    if ch in DOWN_KEYS:
        return min(count - 1, sel + 1)
    if ch == PAGE_UP:
        return max(0, sel - page)
    if ch == PAGE_DOWN:
        return min(count - 1, sel + page)
    if ch in (HOME_KEY, ord("g")):
        return 0
    if ch in (END_KEY, ord("G")):
        return count - 1
    return None


def row(body: Line, width: int, selected: bool) -> Line:
    """A list row: gutter (▸ when selected) + body, exactly `width` cells, @sel when selected."""
    lead: Line = [(GLYPHS["sel"] + " ", "cyan+b")] if selected else [(GUTTER, "text")]
    full = lead + list(body)
    if line_width(full) > width and width > 4:
        line = fit(full, width - 1) + [("…", "muted")]  # cut rows end with …
    else:
        line = fit(full, width)
    return with_bg(line, "sel") if selected else line


def centered(parts: Line, width: int) -> Line:
    pad = max(0, (width - line_width(parts)) // 2)
    return fit([(" " * pad, "text")] + list(parts), width)


def empty_state(message: str, width: int, command: str = "", before: str = "Run  ", after: str = "",
                extra: str = "") -> List[Line]:
    """Centered muted message, and a 'Run  <command>  ...' line with the command in cyan."""
    lines: List[Line] = [centered([(message, "muted")], width)]
    if extra:
        # A narrow column (next to the Golden Hour city) wraps the hint instead of cutting it.
        for part in (wrap(extra, width - 2, 3) if cell_width(extra) > width - 2 else [extra]):
            lines.append(centered([(part, "muted")], width))
    if command:
        parts: Line = [(before, "muted"), (command, "cyan")]
        if after:
            parts.append((after, "muted"))
        if line_width(parts) > width - 2:
            parts = [(command, "cyan")]
        lines.append(centered(parts, width))
    return lines


def bar(parts: Sequence[Tuple[int, str]], width: int, glyph: str = "━") -> Line:
    """Proportion bar: each (count, style) gets a share of `width` cells (at least 1 when > 0)."""
    total = sum(max(0, c) for c, _ in parts)
    if total <= 0 or width <= 0:
        return [(glyph * max(0, width), "dim")]
    shares = []
    for c, style in parts:
        shares.append([max(0, c) * width / total, style, c])
    cells = [int(s[0]) for s in shares]
    for i, s in enumerate(shares):
        if s[2] > 0 and cells[i] == 0:
            cells[i] = 1
    # hand out the rounding rest to the biggest remainders, take overflow from the biggest share
    while sum(cells) < width:
        i = max(range(len(shares)), key=lambda k: (shares[k][0] - cells[k]) if shares[k][2] > 0 else -1)
        cells[i] += 1
    while sum(cells) > width:
        i = max(range(len(cells)), key=lambda k: cells[k])
        cells[i] -= 1
    return [(glyph * n, s[1]) for n, s in zip(cells, shares) if n > 0]


# ---------------------------------------------------------------------------
# Guard checks (shared by the Home GUARD line and the Guard tab)
# ---------------------------------------------------------------------------


def guard_checks(g: Dict[str, Any], now: float) -> List[Dict[str, Any]]:
    """The four guard checks with a live number each. Only measured values are shown."""
    log = g.get("log") or {}
    protected = "  ".join(g.get("protected_paths") or [])
    path_blocks = log.get("path_blocks_today")
    lat = g.get("router_latency_ms")
    intent_on = bool(g.get("intent_available"))
    intent_blocks = log.get("intent_blocks_today") or 0
    if isinstance(lat, (int, float)):
        intent_num = f"last check {lat:.0f} ms"
    else:
        intent_num = "no check yet" if intent_on else "-"
    trash_today = log.get("trash_today") or 0  # from the guard log, like the other "today" counts
    last_cp = log.get("last_checkpoint_ts")
    return [
        {
            "name": "path shield",
            "on": True,
            "number": f"blocked {path_blocks or 0} today" if g.get("log_found") else "nothing blocked",
            "note": protected,
        },
        {
            "name": "intent check",
            "on": intent_on,
            "number": intent_num,
            "note": f"ModernBERT, blocked {intent_blocks} today" if intent_on else "Laya worker not found",
        },
        {
            "name": "soft delete",
            "on": True,
            "number": f"trashed {trash_today} today",
            "note": "rm goes to the Trash",
        },
        {
            "name": "git checkpoints",
            "on": True,
            "number": f"last {ago_words(last_cp, now)}" if last_cp else "none yet",
            "note": "before each edit",
        },
    ]
