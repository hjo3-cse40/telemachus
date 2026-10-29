"""The "Quiet" shell around every Cockpit screen: header, tabs, footer keys, toast and help overlay.

All builders are pure (data in, lines out). Screen layout, top to bottom:

    row 0        header      telemachus, LM Studio indicator, ctx, ram, speed ... clock
    row 1        tabs        1 Home    2 Money    3 Memory    4 Guard
    row 2        underline   dim rule with a cyan ━ under the active tab name
    row 3        blank
    rows 4..H-3  body        lines from the active view (BODY_TOP .. H - 3)
    row H-2      toast       right-aligned, timed (blank otherwise)
    row H-1      footer      key hints of the active screen, grouped by "   ·   "
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from telemachus.cockpit.telemetry import HeaderInfo
from telemachus.cockpit.theme import (
    GLYPHS,
    GROUP_SEP,
    GUTTER,
    SPINNER,
    Line,
    cell_width,
    clip,
    fit,
    kind_style,
    line_width,
    lpad,
    rpad,
    with_bg,
)

HEADER_ROW = 0
TAB_ROW = 1
UNDERLINE_ROW = 2
BODY_TOP = 4
BOTTOM_ROWS = 2  # toast row + footer row
MIN_ROWS = 10
MIN_COLS = 30

TABS: Tuple[Tuple[str, str], ...] = (
    ("home", "Home"),
    ("money", "Money"),
    ("memory", "Memory"),
    ("guard", "Guard"),
)
TAB_IDS: Tuple[str, ...] = tuple(t for t, _ in TABS)
TAB_GAP = 4  # spaces between tabs


# ---------------------------------------------------------------------------
# Key hints (one source for the footer and the help overlay)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyHint:
    keys: str  # shown in cyan, e.g. "j/k", "↵", "⌃S", "esc"
    label: str  # short footer word, e.g. "move"
    help: str = ""  # longer text for the help overlay (defaults to label)
    footer: bool = True  # False = help overlay only

    @property
    def help_text(self) -> str:
        return self.help or self.label


@dataclass(frozen=True)
class KeyGroup:
    title: str  # section heading in the help overlay, e.g. "LISTS", "MONEY"
    hints: Tuple[KeyHint, ...]

    @staticmethod
    def of(title: str, *hints: KeyHint) -> "KeyGroup":
        return KeyGroup(title, tuple(hints))


GLOBAL_KEYS = KeyGroup.of(
    "EVERYWHERE",
    KeyHint("1-4", "tabs", "switch tab", footer=False),
    KeyHint("tab", "next tab", "next tab, shift: back", footer=False),
    KeyHint("r", "refresh", "refresh", footer=False),
    KeyHint("?", "keys", "this help"),
    KeyHint("q", "quit", "quit"),
)
HELP_OPEN_KEYS = [KeyGroup.of("", KeyHint("esc", "close"), KeyHint("?", "close"))]
HELP_SCROLL_KEYS = [KeyGroup.of("", KeyHint("j/k", "scroll"), KeyHint("esc", "close"), KeyHint("?", "close"))]


@dataclass
class Frame:
    """What a view returns for the body area.

    lines: body lines, drawn from column 0 (views include the 2-cell gutter themselves).
    keys: footer / help key groups for this screen (the shell adds GLOBAL_KEYS).
    rows: body line index -> (target kind, index) for mouse clicks, e.g. {5: ("email", 2)}.
    state: values the view adjusted and the TUI should keep, e.g. {"email_scroll_offset": 3}.
    """

    lines: List[Line]
    keys: List[KeyGroup] = field(default_factory=list)
    rows: Dict[int, Tuple[str, int]] = field(default_factory=dict)
    state: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Panels:
    """Data sources handed to each view's load(). Views pick what they need."""

    email_panel: Any = None  # EmailTriagePanel
    safety_panel: Any = None  # SafetyMemoryPanel
    finance_panel: Any = None  # FinanceTuiPanel
    telemetry: Any = None  # CockpitTelemetry


# Commands a view's handle_key / handle_click may return for the TUI to carry out.
# "toast:<kind>:<text>" shows a toast (kind: ok, warn, error, info); "tab:<id>" switches tab.
COMMANDS = ("triage", "refresh", "open_trash", "toast:", "tab:")


def toast_command(text: str, kind: str = "info") -> str:
    return f"toast:{kind}:{text}"


@dataclass
class Overlay:
    """A box drawn over the body: lines (each exactly `width` cells) at screen row `top`, col `left`."""

    lines: List[Line]
    top: int
    left: int
    width: int
    scroll: int = 0  # help card: first content row shown (clamped)
    scroll_max: int = 0  # help card: largest useful scroll (0 = everything fits)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    rows: int  # terminal rows
    cols: int  # terminal columns
    width: int  # drawable width (cols - 1: the last column is never written, curses would wrap)
    body_top: int
    body_height: int
    toast_row: int
    footer_row: int


def layout(rows: int, cols: int) -> Layout:
    width = max(1, cols - 1)
    body_h = max(0, rows - BODY_TOP - BOTTOM_ROWS)
    return Layout(rows, cols, width, BODY_TOP, body_h, rows - 2, rows - 1)


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


def _tok(n: int) -> str:
    """Token count in at most 5 cells: 999, 1.2k, 12k, 999k, 1.2M, 999M, 999M+."""
    n = max(0, int(n))
    if n < 1000:
        return str(n)
    if n < 9950:
        return f"{n / 1000:.1f}k"
    if n < 999_500:
        return f"{n / 1000:.0f}k"
    if n < 9_950_000:
        return f"{n / 1e6:.1f}M"
    if n < 999_500_000:
        return f"{n / 1e6:.0f}M"
    return "999M+"


def _rate(tps: float) -> str:
    """Tokens a second in at most 4 cells (a broken status file can hold any number)."""
    return f"{tps:.0f}" if tps < 999.5 else ">999"


def _elapsed(s: float) -> str:
    s = int(s)
    if s >= 100 * 60:
        return f"{min(s // 3600, 99)}h {s % 3600 // 60:02d}m"
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s"


def spinner_frame(now: float, fps: float = 8.0) -> str:
    return SPINNER[int(now * fps) % len(SPINNER)]


# While LM Studio works, the state text after the spinner takes this many cells (spaces fill the
# rest), so ctx and ram do not move each time the token count gains a digit:
# " writing 999k tok · 999 t/s" is 27 cells.
ACTIVE_TEXT_W = 27
# A shortened model name keeps at least this many characters before the "…".
MIN_MODEL_CHARS = 4
# Fields that do not depend on the LM Studio state. When they fit beside "● ready" they also stay
# while LM Studio reads or writes: the model name gets shorter instead (display order numbers).
STABLE_FIELDS = (1, 2)  # ctx, ram


def lm_indicator(info: HeaderInfo, now: float) -> Line:
    """The LM Studio indicator: model name plus a state dot or spinner."""
    model: Line = [(info.model + " ", "text")] if info.model else []
    return model + _lm_state(info, now)


def _lm_state(info: HeaderInfo, now: float) -> Line:
    """The state part of the indicator (dot or spinner plus words), without the model name."""
    if info.lm == "unknown":
        return [(GLYPHS["off"], "dim"), (" checking LM Studio", "muted")]
    if info.lm == "offline":
        return [(GLYPHS["off"], "dim"), (" LM Studio offline", "muted")]
    if info.lm == "no_model":
        # Not green: a chat would fail until a model is loaded.
        return [(GLYPHS["off"], "yellow"), (" no model loaded", "yellow")]
    if info.lm in ("reading", "writing"):
        if info.lm == "reading":
            txt, style = " reading prompt", "yellow"
            if info.elapsed_s is not None:
                txt += " " + _elapsed(info.elapsed_s)
        else:
            txt, style = " writing", "cyan"
            if info.tokens is not None:
                txt += f" {_tok(info.tokens)} tok"
            if info.tps is not None:
                txt += f" · {_rate(info.tps)} t/s"
        return [(spinner_frame(now), style + "+b"), (txt, style)]
    return [(GLYPHS["ok"], "green"), (" ready", "muted")]


def _model_seg(model: str, room: int) -> Line:
    """The model name in at most `room` cells (its trailing space included), cut with '…'."""
    if not model:
        return []
    if cell_width(model) + 1 <= room:
        return [(model + " ", "text")]
    keep = max(MIN_MODEL_CHARS, room - 2)
    return [(clip(model, keep) + "… ", "text")]


def _fit_header(
    brand_w: int,
    model: str,
    state: Line,
    fields: Sequence[Tuple[int, int, Line]],
    protected: Sequence[int],
    clock_w: int,
    width: int,
) -> Tuple[List[Tuple[int, int, Line]], Line, bool]:
    """(kept fields, model segment, keep clock) for one header layout.

    Order of cuts: fields that are not protected (highest drop priority first), then the model
    name down to MIN_MODEL_CHARS, then (only with protected fields) the whole model name, then
    the protected fields, then the clock."""
    kept = list(fields)

    def base(fs: Sequence[Tuple[int, int, Line]], clk: int) -> int:
        return brand_w + line_width(state) + sum(line_width(f) for _, _, f in fs) + (clk + 3 if clk else 0)

    model_full = cell_width(model) + 1 if model else 0
    model_min = min(model_full, MIN_MODEL_CHARS + 2)
    while base(kept, clock_w) + model_full > width:
        free = [i for i, f in enumerate(kept) if f[0] not in protected]
        if not free:
            break
        kept.pop(max(free, key=lambda i: kept[i][1]))
    while kept and base(kept, clock_w) + model_min > width:
        if any(f[0] in protected for f in kept) and base(kept, clock_w) <= width:
            model_min = 0  # a measured field beats a 4-letter stub of the model name
            break
        kept.pop(max(range(len(kept)), key=lambda i: kept[i][1]))
    clk = clock_w
    if base(kept, clk) + model_min > width:
        clk = 0
    model_seg = _model_seg(model, width - base(kept, clk)) if model_min else []
    return kept, model_seg, bool(clk)


def header_fields(info: HeaderInfo) -> List[Tuple[int, int, Line]]:
    """Optional header fields as (display order, drop priority, segments).

    Display order is ctx, ram, speed, hint, think. When space runs out the highest drop priority
    goes first: think, hint, speed, ram, then ctx.
    """
    out: List[Tuple[int, int, Line]] = []
    pct = info.ctx_pct
    if pct is not None and info.lm not in ("offline", "unknown"):
        out.append((1, 2, [("   ctx ", "muted"), (f"{pct}%", "text")]))
    if info.ram_used_gb is not None and info.ram_total_gb:
        out.append((2, 3, [("   ram ", "muted"), (f"{info.ram_used_gb:.1f}/{info.ram_total_gb:.0f}G", "text")]))
    if info.tps is not None and info.lm in ("ready", "no_model", "reading"):
        out.append((3, 4, [(f"   {_rate(info.tps)} t/s", "text")]))
    if info.lm == "offline":
        out.append((4, 5, [("   start LM Studio to chat", "muted")]))
    elif info.lm == "no_model":
        out.append((4, 5, [("   load one in LM Studio", "muted")]))
    if info.think_tokens and info.lm in ("ready", "reading", "writing"):
        out.append((5, 6, [(f"   {_tok(info.think_tokens)} think tok", "muted")]))
    return out


def build_header(info: HeaderInfo, width: int, now: float, clock: str) -> Line:
    """Header row. Fields that do not fit are dropped (think, hint, speed, ram, ctx), then the model
    name is shortened, then the clock goes.

    While LM Studio reads or writes, the state text takes ACTIVE_TEXT_W cells, so ctx and ram keep
    their column while the token count grows. ctx and ram stay whenever they fit beside "● ready":
    the model name is shortened to a fixed width instead, so ram does not blink off each time a
    chat runs. The padding never costs a field or the clock."""
    brand: Line = [(GUTTER + "telemachus", "cyan+b"), ("   ", "text")]
    brand_w = line_width(brand)
    state = _lm_state(info, now)
    fields = list(header_fields(info))
    clock_w = cell_width(clock) if clock else 0

    if info.active:
        stable = [f for f in fields if f[0] in STABLE_FIELDS]
        ready_state = [(GLYPHS["ok"], "green"), (" ready", "muted")]
        in_ready, _, _ = _fit_header(brand_w, info.model, ready_state, stable, (), clock_w, width)
        protected = tuple(f[0] for f in in_ready)
        plain = _fit_header(brand_w, info.model, state, fields, protected, clock_w, width)
        text_w = line_width(state[1:])
        padded_state = state + ([(" " * (ACTIVE_TEXT_W - text_w), "text")] if text_w < ACTIVE_TEXT_W else [])
        padded = _fit_header(brand_w, info.model, padded_state, fields, protected, clock_w, width)
        same = [f[0] for f in padded[0]] == [f[0] for f in plain[0]] and padded[2] == plain[2]
        if same:
            state = padded_state
        kept, model_seg, keep_clock = padded if same else plain
    else:
        kept, model_seg, keep_clock = _fit_header(brand_w, info.model, state, fields, (), clock_w, width)

    right: Line = [(clock, "muted")] if keep_clock else []
    left: Line = brand + model_seg + state
    for _, _, f in sorted(kept, key=lambda x: x[0]):
        left += f
    gap = width - line_width(left) - line_width(right)
    if gap < 0:
        return fit(left, width)
    return left + [(" " * gap, "text")] + right


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


def _tab_row(active: str, gap: int, numbers: bool) -> Tuple[Line, List[Tuple[int, int, str]], Tuple[int, int]]:
    row: Line = [(GUTTER, "text")]
    spans: List[Tuple[int, int, str]] = []
    name_span: Tuple[int, int] = (0, 0)
    x = len(GUTTER)
    for i, (tab_id, name) in enumerate(TABS):
        if i:
            row.append((" " * gap, "text"))
            x += gap
        start = x
        if numbers:
            num = f"{i + 1} "
            row.append((num, "dim"))
            x += len(num)
        is_active = tab_id == active
        row.append((name, "text+b" if is_active else "muted"))
        if is_active:
            name_span = (x, x + len(name))
        x += len(name)
        spans.append((start, x, tab_id))
    return row, spans, name_span


def build_tabs(active: str, width: int) -> Tuple[Line, Line, List[Tuple[int, int, str]]]:
    """(tab row, underline row, click spans [(x_start, x_end_exclusive, tab_id)]).
    On a narrow pane the gaps shrink first, then the numbers go, so every tab name stays whole."""
    tries = [(g, True) for g in range(TAB_GAP, 0, -1)] + [(g, False) for g in (2, 1)]
    for gap, numbers in tries:
        row, spans, name_span = _tab_row(active, gap, numbers)
        if line_width(row) <= width:
            break
    a, b = name_span
    under: Line = [(GUTTER, "text"), ("─" * max(0, a - len(GUTTER)), "dim")]
    if b > a:
        under.append((GLYPHS["tab"] * (b - a), "cyan"))
    under.append(("─" * max(0, width - max(b, len(GUTTER))), "dim"))
    return fit(row, width), fit(under, width), spans


def tab_at(spans: Sequence[Tuple[int, int, str]], x: int) -> Optional[str]:
    for start, end, tab_id in spans:
        if start <= x < end:
            return tab_id
    return None


# ---------------------------------------------------------------------------
# Footer and toast
# ---------------------------------------------------------------------------


def _group_segments(group: KeyGroup) -> Line:
    out: Line = []
    for h in group.hints:
        if not h.footer:
            continue
        if out:
            out.append(("  ", "text"))
        out += [(h.keys, "cyan"), (" " + h.label, "muted")]
    return out


def build_footer(groups: Sequence[KeyGroup], width: int, with_global: bool = True) -> Line:
    """'  j/k move  ↵ open   ·   v bills   ·   ? keys  q quit'. When too wide, middle groups drop
    from the right; the global group (? keys, q quit) always stays. Then the first group drops its
    inner hints."""
    all_groups = list(groups) + ([GLOBAL_KEYS] if with_global else [])
    parts = [p for p in (_group_segments(g) for g in all_groups) if p]
    if not parts:
        return fit([], width)

    def join(ps: List[Line]) -> Line:
        out: Line = [(GUTTER, "text")]
        for i, p in enumerate(ps):
            if i:
                out.append((GROUP_SEP, "dim"))
            out += p
        return out

    line = join(parts)
    while line_width(line) > width and len(parts) > 2:
        parts.pop(-2)
        line = join(parts)
    # Still too wide: the first group drops hints from the inside out and keeps its first and last
    # (in the Add box '↵ new line' and 'esc cancel' stay; the others go).
    if line_width(line) > width:
        first = next(g for g in all_groups if _group_segments(g))
        hints = [h for h in first.hints if h.footer]
        while line_width(line) > width and len(hints) > 2:
            hints.pop(-2)
            parts[0] = _group_segments(KeyGroup(first.title, tuple(hints)))
            line = join(parts)
    return fit(line, width)


def build_toast(text: str, kind: str, width: int) -> Line:
    """Right-aligned toast (the row above the footer). kind: ok, warn, error, info."""
    if not text:
        return fit([], width)
    glyph, style = kind_style(kind)
    body = glyph + text
    room = max(0, width - 2)
    if cell_width(body) > room:
        body = clip(body, max(0, room - 1)) + "…"
    pad = width - 2 - cell_width(body)
    return fit([(" " * max(0, pad), "text"), (body, style), ("  ", "text")], width)


# ---------------------------------------------------------------------------
# Help overlay
# ---------------------------------------------------------------------------


HELP_KEY_W = 6  # key column of the help card ("esc", "←/→", "⌃S" and a gap)


def _help_section(group: KeyGroup, col_w: int) -> List[Line]:
    """One help section. Long help text wraps onto more rows (indented under the text), so
    nothing is cut with '…'."""
    rows: List[Line] = [[(GUTTER + group.title, "muted+b")]] if group.title else []
    indent = len(GUTTER) + HELP_KEY_W
    room = max(4, col_w - indent - 1)  # one cell margin before the next column
    for h in group.hints:
        parts = wrap_text(h.help_text, room)
        if cell_width(h.keys) >= HELP_KEY_W:  # long key name: own row, text below it
            rows.append([(GUTTER + h.keys, "cyan")])
        else:
            first = parts.pop(0)
            rows.append([(GUTTER + rpad(h.keys, HELP_KEY_W), "cyan"), (first, "text")])
        rows.extend([[(" " * indent + p, "text")] for p in parts])
    return [fit(r, col_w) for r in rows]


def _help_rows(sections: List[List[Line]], inner: int, two_col: bool) -> List[Line]:
    if not two_col:
        out: List[Line] = []
        for i, sec in enumerate(sections):
            if i:
                out.append([])
            out.extend(sec)
        return out
    col_w = inner // 2
    left: List[Line] = []
    right: List[Line] = []
    for sec in sections:
        target = left if len(left) <= len(right) else right
        if target:
            target.append([])
        target.extend(sec)
    return [
        fit(left[i] if i < len(left) else [], col_w) + fit(right[i] if i < len(right) else [], inner - col_w)
        for i in range(max(len(left), len(right)))
    ]


def build_help(groups: Sequence[KeyGroup], width: int, body_top: int, body_height: int, scroll: int = 0) -> Overlay:
    """'Keys' card listing every key of the current screen plus the global ones (from the same
    KeyGroup data as the footer, so it never drifts). Two columns when there is room.

    Long text wraps. When the card is taller than the body it moves up one row (onto the blank
    row under the tabs), drops its blank padding rows, and then scrolls: the last row says how
    many rows are hidden and that j/k scroll. Overlay.scroll / scroll_max give the clamped offset.
    """
    all_groups = [GLOBAL_KEYS] + [g for g in groups if g.hints]
    # The card takes most of the pane (2 cells each side), so descriptions get the room and wrap less.
    card_w = min(max(40, width - 4), 100)
    card_w = min(card_w, width)
    inner = max(1, card_w - 2)
    two_col = inner >= 56 and len(all_groups) > 1
    col_w = inner // 2 if two_col else inner
    content = _help_rows([_help_section(g, col_w) for g in all_groups], inner, two_col)

    top_row = body_top
    room = max(1, body_height - 2)  # rows inside the borders
    body: List[Line] = [[]] + content + [[]]
    scroll_max = 0
    if len(body) > room:
        top_row = max(0, body_top - 1)
        room += body_top - top_row
        body = list(content)
    if len(body) > room:
        view = max(1, room - 1)  # last row is the "more" marker
        scroll_max = max(0, len(body) - view)
        scroll = max(0, min(scroll, scroll_max))
        shown = body[scroll:scroll + view]
        below = len(body) - scroll - len(shown)
        if below > 0:
            marker = f"{GUTTER}▾ {below} more {'row' if below == 1 else 'rows'}   j/k scroll"
        else:
            marker = f"{GUTTER}▴ end   k scroll up"
        body = shown + [[(marker, "muted")]]
    else:
        scroll = 0

    title = "Keys"
    trail = "esc close"
    dashes = card_w - 3 - len(title) - 2 - len(trail) - 3  # "╭─ " title " ─…─ " trail " ─╮"
    top: Line = [
        (GLYPHS["tl"] + "─ ", "cyan"),
        (title, "cyan+b"),
        (" " + "─" * max(1, dashes) + " ", "cyan"),
        (trail, "muted"),
        (" ─" + GLYPHS["tr"], "cyan"),
    ]
    lines: List[Line] = [fit(top, card_w)]
    for row in body:
        lines.append([("│", "cyan")] + with_bg(fit(row, inner), "panel") + [("│", "cyan")])
    lines.append([(GLYPHS["bl"] + "─" * (card_w - 2) + GLYPHS["br"], "cyan")])
    left_x = max(0, (width - card_w) // 2)
    return Overlay(lines=lines, top=top_row, left=left_x, width=card_w, scroll=scroll, scroll_max=scroll_max)


TOO_SMALL_TEXT = f"Make the pane a little bigger (at least {MIN_COLS} x {MIN_ROWS})."


def wrap_text(text: str, width: int) -> List[str]:
    """Word wrap to `width` cells. A word longer than a line is cut into pieces."""
    width = max(1, width)
    out: List[str] = []
    cur = ""
    for word in text.split():
        while cell_width(word) > width:
            if cur:
                out.append(cur)
                cur = ""
            piece = clip(word, width)
            out.append(piece)
            word = word[len(piece):]
        if not word:
            continue
        if not cur:
            cur = word
        elif cell_width(cur) + 1 + cell_width(word) <= width:
            cur += " " + word
        else:
            out.append(cur)
            cur = word
    if cur or not out:
        out.append(cur)
    return out


def too_small(width: int, rows: int = MIN_ROWS) -> List[Line]:
    """The message shown instead of the cockpit when the pane is too small, wrapped to fit."""
    gutter = GUTTER if width >= 20 else ""
    lines = wrap_text(TOO_SMALL_TEXT, width - len(gutter))
    return [fit([(gutter + t, "muted")], width) for t in lines[: max(1, rows)]]


__all__ = [
    "BODY_TOP",
    "COMMANDS",
    "Panels",
    "toast_command",
    "Frame",
    "GLOBAL_KEYS",
    "HELP_OPEN_KEYS",
    "KeyGroup",
    "KeyHint",
    "Layout",
    "Overlay",
    "TABS",
    "TAB_IDS",
    "build_footer",
    "build_header",
    "build_help",
    "build_tabs",
    "build_toast",
    "layout",
    "lm_indicator",
    "lpad",
    "spinner_frame",
    "tab_at",
]
