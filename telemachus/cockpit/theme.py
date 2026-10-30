"""Colors, styles, glyphs and the line model for the Cockpit TUI.

Themes
------
Two themes share one set of color roles: "golden-hour" (warm text and a city window on Home whose
sky follows the clock; the default) and "tokyo-night" (the quiet look before it). ``use_theme()``
swaps PALETTE / BACKGROUNDS / PINNED_256 in place, so every module that imported them sees the
active theme. The module starts in Tokyo Night so imports and tests stay stable; the TUI's main()
picks the configured theme (TELEMACHUS_THEME, then "theme" in cockpit.json, then Golden Hour).

The line model
--------------
A segment is ``(text, style)``. A line is a list of segments. Views build lines and never touch
curses; ``tui.py`` turns each style into a curses attribute with :func:`attr` and draws the text.

Style strings
-------------
``"<fg>[+<flags>][@<bg>]"``, for example ``"cyan"``, ``"text+b"``, ``"muted@sel"``, ``"yellow+b@card"``.

* fg: one of COLOR_NAMES (``cyan text muted dim green yellow red blue magenta``) or ``ink`` (the dark
  background color, for text on a light fill).
* flags: ``b`` bold, ``u`` underline, ``r`` reverse. Several flags can follow (``"+bu"``).
* bg: ``sel`` (selected row), ``card`` (modal card), ``panel`` (help card), or any fg color name
  (``"ink+b@cyan"`` is an inverse cyan badge). No bg = the terminal's own background.

Old finance_view names still work (see ALIASES): ``fg`` = text, ``snow`` = bold text, ``sub`` = muted,
``line`` = dim, ``mag`` = magenta, ``sel`` = bold text on the selected-row background.

Color roles (one job per color): cyan = focus, active tab, keys; text = values; muted = labels and
hints; dim = lines and borders only, never text you must read; green = good; yellow = attention;
red = problem; blue = data (bars, charts); magenta = private memory tier only.
"""
from __future__ import annotations

import os
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # curses is optional so the pure helpers stay importable everywhere
    import curses
except ImportError:  # pragma: no cover
    curses = None  # type: ignore[assignment]

Seg = Tuple[str, str]
Line = List[Seg]

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

PALETTE: Dict[str, str] = {
    "cyan": "#7dcfff",
    "text": "#c0caf5",
    "muted": "#8a91b8",
    "dim": "#565f89",
    "green": "#9ece6a",
    "yellow": "#e0af68",
    "red": "#f7768e",
    "blue": "#7aa2f7",
    "magenta": "#bb9af7",
    "ink": "#1a1b26",  # the window background, used as text color on a light fill
}
BACKGROUNDS: Dict[str, str] = {
    "sel": "#283457",  # selected row
    "card": "#24283b",  # modal cards (Add / Edit)
    "panel": "#1f2335",  # help overlay card
}
COLOR_NAMES: Tuple[str, ...] = ("cyan", "text", "muted", "dim", "green", "yellow", "red", "blue", "magenta")

# Every theme fills the same roles (see the module docstring). Golden Hour uses exact xterm-256
# slots, so 256-color mode draws it with no rounding (slot numbers in the comments).
THEMES: Dict[str, Dict[str, Any]] = {
    "tokyo-night": {
        "palette": dict(PALETTE),
        "backgrounds": dict(BACKGROUNDS),
        "pinned_256": {"text": 189, "sel": 17},
        "art": False,
    },
    "golden-hour": {
        "palette": {
            "cyan": "#ffaf5f",  # 215 amber: focus, keys, active tab, brand
            "text": "#ffd7af",  # 223 peach cream: values
            "muted": "#d7afaf",  # 181 dusty rose: labels and hints
            "dim": "#875f87",  # 96 mauve: lines and borders only
            "green": "#afd787",  # 150 sage: good
            "yellow": "#ffd787",  # 222 pale gold: attention
            "red": "#ff5f5f",  # 203 coral red: problem
            "blue": "#afafff",  # 147 periwinkle: data (bars, charts)
            "magenta": "#d787ff",  # 177 violet: private memory tier
            "ink": "#262626",  # 235: text on a light fill
        },
        "backgrounds": {
            "sel": "#5f005f",  # 53 deep plum: selected row
            "card": "#303030",  # 236: Add / Edit cards
            "panel": "#262626",  # 235: help card
        },
        "pinned_256": {},
        "art": True,
    },
}
DEFAULT_THEME = "golden-hour"
THEME_NAMES: Tuple[str, ...] = tuple(THEMES)
_ACTIVE: Dict[str, str] = {"name": "tokyo-night"}

ALIASES: Dict[str, str] = {
    "fg": "text",
    "snow": "text+b",
    "sub": "muted",
    "line": "dim",
    "mag": "magenta",
    "sel": "text+b@sel",
    "inv": "ink+b@cyan",
}

# ---------------------------------------------------------------------------
# Glyphs (all one cell wide; no emoji anywhere in the TUI)
# ---------------------------------------------------------------------------

GLYPHS: Dict[str, str] = {
    "ok": "●",  # ok, today, ready
    "off": "○",  # off, offline
    "done": "✓",  # paid, done
    "fail": "✕",  # failed, error
    "sel": "▸",  # selected row marker
    "bill": "▼",  # bill, money out
    "payday": "◆",
    "over": "▲",  # over budget, warning
    "tab": "━",  # active tab underline
    "rule": "─",  # section rule
    "vrule": "│",  # column split
    "tl": "╭",
    "tr": "╮",
    "bl": "╰",
    "br": "╯",
    "swap": "⇄",
    "hidden": "•••",  # masked amount
    "bullet": "•",
    "bar": "▇",
    "dot": "·",
    "left": "◂",
    "right": "▸",
}
# Braille spinner: one cell wide in every terminal (the ◐◓◑◒ circles are ambiguous width).
SPINNER: Tuple[str, ...] = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

GUTTER = "  "  # every body row starts with a 2-cell gutter (the ▸ marker sits in it)
GROUP_SEP = "   ·   "  # between footer key groups

# ---------------------------------------------------------------------------
# Cell widths
# ---------------------------------------------------------------------------

_VS16 = "️"  # emoji presentation selector; terminals disagree on its width, so it is dropped


def char_width(ch: str) -> int:
    """Terminal cell width of one character (0, 1 or 2)."""
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def cell_width(text: str) -> int:
    return sum(char_width(c) for c in text.replace(_VS16, ""))


def clip(text: str, width: int) -> str:
    """Cuts text to at most `width` cells (never splits a wide character)."""
    out: List[str] = []
    used = 0
    for ch in text.replace(_VS16, ""):
        w = char_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def clip_pad(text: str, width: int) -> str:
    """Cuts text to `width` cells and pads it with spaces to exactly `width`."""
    cut = clip(text, max(0, width))
    return cut + " " * max(0, width - cell_width(cut))


def rpad(text: str, n: int) -> str:
    """Left-aligned in n cells."""
    return clip_pad(text, n)


def lpad(text: str, n: int) -> str:
    """Right-aligned in n cells."""
    cut = clip(text, max(0, n))
    return " " * max(0, n - cell_width(cut)) + cut


# ---------------------------------------------------------------------------
# Line helpers (pure)
# ---------------------------------------------------------------------------


def line_width(line: Sequence[Seg]) -> int:
    return sum(cell_width(t) for t, _ in line)


def line_text(line: Sequence[Seg]) -> str:
    return "".join(t for t, _ in line)


def fit(line: Sequence[Seg], width: int, fill: str = "text") -> Line:
    """Clips or pads a line to exactly `width` cells. Padding uses style `fill`."""
    out: Line = []
    used = 0
    for text, style in line:
        w = cell_width(text)
        if used + w <= width:
            if text:
                out.append((text, style))
            used += w
            continue
        cut = clip(text, width - used)
        if cut:
            out.append((cut, style))
        used += cell_width(cut)
        break
    if used < width:
        out.append((" " * (width - used), fill))
    return out


def with_bg(line: Sequence[Seg], bg: str) -> Line:
    """Puts every segment on background `bg` (keeps each segment's color and flags)."""
    out: Line = []
    for text, style in line:
        base = resolve(style)
        fg_part = base.split("@", 1)[0]
        out.append((text, f"{fg_part}@{bg}"))
    return out


def spread(left: Sequence[Seg], right: Sequence[Seg], width: int, fill: str = "text") -> Line:
    """Left part, then the right part flush with the right edge. The left part is cut first."""
    rw = line_width(right)
    if rw >= width:
        return fit(right, width, fill)
    lw = min(line_width(left), width - rw - (1 if left else 0))
    out = fit(left, max(0, lw), fill) if lw > 0 else []
    gap = width - line_width(out) - rw
    return out + [(" " * gap, fill)] + list(right)


def section(title: str, width: int, trailing: str = "", trailing_style: str = "muted", gutter: str = GUTTER) -> Line:
    """Section rule: '  TITLE ────────────  trailing'. The rule is dim; the title is muted bold."""
    head: Line = [(gutter, "text"), (title + " ", "muted+b")]
    tail: Line = [("  " + trailing, trailing_style)] if trailing else []
    dashes = width - line_width(head) - line_width(tail)
    if dashes < 2 and tail:
        tail = []
        dashes = width - line_width(head)
    return fit(head + [("─" * max(0, dashes), "dim")] + tail, width)


def money_text(cents: int, mask: bool = False, signed: bool = False, whole: bool = False) -> str:
    """'$1,234.56'; masked amounts show the hidden glyph instead of digits."""
    if mask:
        return "$" + GLYPHS["hidden"]
    sign = "-" if cents < 0 else ("+" if signed and cents > 0 else "")
    value = abs(cents) / 100.0
    body = f"{round(value):,}" if whole else f"{value:,.2f}"
    return f"{sign}${body}"


# ---------------------------------------------------------------------------
# Style resolution and curses attributes
# ---------------------------------------------------------------------------


def resolve(style: str) -> str:
    """Expands aliases and returns the canonical 'fg+flags@bg' form."""
    style = style or "text"
    bg = ""
    if "@" in style:
        style, bg = style.split("@", 1)
    base, _, flags = style.partition("+")
    if base in ALIASES:
        a = ALIASES[base]
        if "@" in a:
            a, a_bg = a.split("@", 1)
            bg = bg or a_bg
        base, _, a_flags = a.partition("+")
        flags = a_flags + flags
    if base not in PALETTE:
        base = "text"
    flags = "".join(sorted(set(f for f in flags if f in "bur")))
    return base + (f"+{flags}" if flags else "") + (f"@{bg}" if bg else "")


def parse(style: str) -> Tuple[str, str, str]:
    """(fg, flags, bg) for a style string."""
    canon = resolve(style)
    bg = ""
    if "@" in canon:
        canon, bg = canon.split("@", 1)
    fg, _, flags = canon.partition("+")
    return fg, flags, bg


def _hex_rgb(value: str) -> Tuple[int, int, int]:
    v = value.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


_CUBE = (0, 95, 135, 175, 215, 255)


def _xterm_rgb(index: int) -> Tuple[int, int, int]:
    if 16 <= index <= 231:
        i = index - 16
        return _CUBE[i // 36], _CUBE[(i // 6) % 6], _CUBE[i % 6]
    level = 8 + 10 * (index - 232)
    return level, level, level


def nearest_xterm(hex_color: str) -> int:
    """Closest xterm-256 index (16-255; 0-15 are themed by the terminal, so they are skipped)."""
    r, g, b = _hex_rgb(hex_color)
    best, best_d = 16, float("inf")
    for idx in range(16, 256):
        r2, g2, b2 = _xterm_rgb(idx)
        rm = (r + r2) / 2
        d = (2 + rm / 256) * (r - r2) ** 2 + 4 * (g - g2) ** 2 + (2 + (255 - rm) / 256) * (b - b2) ** 2
        if d < best_d:
            best, best_d = idx, d
    return best


# 256-color slots chosen by hand where the plain nearest match breaks a color role.
# text: the nearest match is 153 (#afd7ff), a pale blue that hardly differs from cyan 117 (#87d7ff),
#   so "cyan = focus" is lost. 189 (#d7d7ff) keeps the lavender-white of Tokyo Night.
# sel: the nearest match is 237, a neutral gray. 17 (#00005f) keeps the navy row highlight and lifts
#   muted text on a selected row from 3.3:1 to 5.2:1.
PINNED_256: Dict[str, int] = {"text": 189, "sel": 17}

# 8-color mode draws the selected row on blue. Green, red and magenta are hard to read on blue,
# so on that row they use their bright slots (16-color terminals) or bold (8-color terminals).
SEL_BRIGHT_8: Dict[str, int] = {"green": 10, "red": 9, "magenta": 13}


# Python's curses.color_pair() keeps only 8 bits of the pair number, so pairs 1-255 are all that can
# be drawn, whatever COLOR_PAIRS says (the macOS ncurses reports 32767; pair 256 would turn into
# A_STANDOUT with pair 0). MAX_PAIRS caps the allocator at that real limit.
MAX_PAIRS = 256
# When a theme has art, text styles use pairs 1..ART_FIRST_PAIR-1 and the art pool the rest. Text
# styles need about 40 pairs; the city uses up to about 90 at once.
ART_FIRST_PAIR = 121


class Theme:
    """Maps style strings to curses attributes. Pairs are created on first use.

    Art (the Golden Hour city) gets its own pool of pairs at the top of the range. Its colors change
    with the hour, so a pair the current art frame does not use is recycled for a new color combo.
    That is safe because the art area is drawn completely on every art frame."""

    def __init__(self) -> None:
        self.mode = "none"  # "exact", "256", "8", "mono" or "none" (curses not started)
        self._color_of: Dict[str, int] = {}
        self._pairs: Dict[Tuple[int, int], int] = {}
        self._attr_cache: Dict[str, int] = {}
        self._next_pair = 1
        self._max_pairs = 0
        self._reset_art()

    def _reset_art(self) -> None:
        self._art_pairs: Dict[Tuple[int, int], int] = {}  # (fg slot, bg slot) -> pair number
        self._art_seen: Dict[Tuple[int, int], int] = {}  # combo -> last art frame that used it
        self._art_frame = 0
        self._art_next = ART_FIRST_PAIR  # next never-used pair number of the pool

    # -- setup ---------------------------------------------------------------

    def init(self) -> str:
        """Call once after curses starts. Picks the best color mode the terminal supports."""
        self._color_of.clear()
        self._pairs.clear()
        self._attr_cache.clear()
        self._next_pair = 1
        self._reset_art()
        if curses is None or not curses.has_colors():
            self.mode = "mono"
            return self.mode
        curses.start_color()
        try:
            curses.use_default_colors()
            default_bg = -1
        except curses.error:
            default_bg = curses.COLOR_BLACK
        self._default_bg = default_bg
        self._max_pairs = min(getattr(curses, "COLOR_PAIRS", 64), MAX_PAIRS)
        forced = os.environ.get("TELEMACHUS_COLORS", "").strip().lower()
        colors = getattr(curses, "COLORS", 8)
        if forced in ("8", "basic"):
            self._setup_basic(colors)
        elif forced == "exact" and colors >= 256 and curses.can_change_color():
            # Opt-in: redefines palette slots 240-251. This changes the terminal window's palette while
            # the cockpit runs, which also recolors the OpenCode pane in a tmux split, so it is off by default.
            self._setup_exact()
        elif colors >= 256:
            self._setup_256()
        else:
            self._setup_basic(colors)
        return self.mode

    def _all_colors(self) -> Dict[str, str]:
        both = dict(PALETTE)
        both.update(BACKGROUNDS)
        return both

    def _setup_256(self) -> None:
        self.mode = "256"
        for name, hx in self._all_colors().items():
            self._color_of[name] = PINNED_256.get(name) or nearest_xterm(hx)

    def _setup_exact(self) -> None:
        self.mode = "exact"
        slot = 240
        for name, hx in self._all_colors().items():
            r, g, b = _hex_rgb(hx)
            try:
                curses.init_color(slot, r * 1000 // 255, g * 1000 // 255, b * 1000 // 255)
                self._color_of[name] = slot
            except curses.error:
                self._color_of[name] = nearest_xterm(hx)
            slot += 1

    def _setup_basic(self, colors: int) -> None:
        self.mode = "8"
        c = curses
        bright_black = 8 if colors >= 16 else c.COLOR_WHITE
        self._color_of.update({
            "cyan": c.COLOR_CYAN,
            "text": -1,
            "muted": c.COLOR_WHITE,
            "dim": bright_black,
            "green": c.COLOR_GREEN,
            "yellow": c.COLOR_YELLOW,
            "red": c.COLOR_RED,
            "blue": c.COLOR_BLUE,
            "magenta": c.COLOR_MAGENTA,
            "ink": c.COLOR_BLACK,
            "sel": c.COLOR_BLUE,
            "card": -1,
            "panel": -1,
        })
        self._dim_needs_attr = colors < 16
        self._basic_colors = colors

    # -- lookup --------------------------------------------------------------

    def _text_pair_limit(self) -> int:
        """First pair number text styles may not use (the art pool starts there)."""
        limit = max(2, self._max_pairs)
        return min(limit, ART_FIRST_PAIR) if self.art_ready() else limit

    def _pair(self, fg: int, bg: int) -> int:
        key = (fg, bg)
        if key in self._pairs:
            return self._pairs[key]
        if self._next_pair >= self._text_pair_limit():
            return 0
        n = self._next_pair
        try:
            curses.init_pair(n, fg, bg)
        except curses.error:
            return 0
        self._next_pair += 1
        self._pairs[key] = n
        return n

    # Text color used when a style's text and background map to the same terminal color
    # (8-color mode: blue bars on the blue selected row). First one that differs from the bg.
    CLASH_FALLBACK = ("cyan", "text", "ink")

    def color_numbers(self, fg: str, bg: str) -> Tuple[int, int]:
        """(fg, bg) terminal color numbers for a parsed style. -1 = the terminal default.
        Never returns the same number twice: that would draw invisible text."""
        fg_n = self._color_of.get(fg, -1)
        bg_n = self._color_of.get(bg, -1) if bg else -1
        if self.mode == "8" and bg == "sel" and fg in SEL_BRIGHT_8 and getattr(self, "_basic_colors", 8) >= 16:
            fg_n = SEL_BRIGHT_8[fg]
        if bg_n == -1:
            bg_n = getattr(self, "_default_bg", -1)
        if bg_n != -1 and fg_n == bg_n:
            for name in self.CLASH_FALLBACK:
                alt = self._color_of.get(name, -1)
                if alt != bg_n:
                    fg_n = alt
                    break
        return fg_n, bg_n

    def attr(self, style: str) -> int:
        cached = self._attr_cache.get(style)
        if cached is not None:
            return cached
        fg, flags, bg = parse(style)
        value = 0
        if curses is not None:
            if "b" in flags:
                value |= curses.A_BOLD
            if "u" in flags:
                value |= curses.A_UNDERLINE
            if "r" in flags:
                value |= curses.A_REVERSE
            if self.mode in ("256", "exact", "8"):
                fg_n, bg_n = self.color_numbers(fg, bg)
                if fg_n == -1 and bg_n == -1:
                    pair = 0
                else:
                    pair = self._pair(fg_n, bg_n)
                value |= curses.color_pair(pair) if pair else 0
                if self.mode == "8" and fg == "dim" and getattr(self, "_dim_needs_attr", False):
                    value |= curses.A_DIM
                if (self.mode == "8" and bg == "sel" and fg in SEL_BRIGHT_8
                        and getattr(self, "_basic_colors", 8) < 16):
                    value |= curses.A_BOLD  # most 8-color terminals draw bold as the bright color
            elif self.mode == "mono":
                if bg == "sel" or fg == "ink":
                    value |= curses.A_REVERSE
                if fg == "dim":
                    value |= curses.A_DIM
        self._attr_cache[style] = value
        return value

    # -- art pool --------------------------------------------------------------

    def art_ready(self) -> bool:
        """True when the active theme has art and curses runs in 256-color mode with room for the pool."""
        return bool(THEMES[_ACTIVE["name"]]["art"]) and self.mode == "256" and self._max_pairs > ART_FIRST_PAIR + 16

    def begin_art_frame(self) -> None:
        """Call before drawing a whole art frame; combos the frame does not use become recyclable."""
        self._art_frame += 1

    def art_attr(self, fg: int, bg: int) -> int:
        """Attribute for an art cell with xterm slots fg / bg. 0 (default colors) when no pair is left."""
        if curses is None or not self.art_ready():
            return 0
        key = (fg, bg)
        n = self._art_pairs.get(key)
        if n is None:
            n = self._art_take()
            if not n:
                return 0
            try:
                curses.init_pair(n, fg, bg)
            except curses.error:
                return 0
            self._art_pairs[key] = n
        self._art_seen[key] = self._art_frame
        return curses.color_pair(n)

    def _art_take(self) -> int:
        """A pair number for a new art combo: a never-used one, else the stalest combo that the
        current frame does not use. 0 when every pool pair is in use by this frame."""
        if self._art_next < self._max_pairs:
            n = self._art_next
            self._art_next += 1
            return n
        victim: Optional[Tuple[int, int]] = None
        for key, frame in self._art_seen.items():
            if frame < self._art_frame and (victim is None or frame < self._art_seen[victim]):
                victim = key
        if victim is None:
            return 0
        self._art_seen.pop(victim, None)
        return self._art_pairs.pop(victim)

    def art_pairs_in_use(self) -> int:
        return len(self._art_pairs)


THEME = Theme()


def use_theme(name: Optional[str]) -> str:
    """Makes `name` the active theme (unknown names fall back to DEFAULT_THEME). Swaps PALETTE,
    BACKGROUNDS and PINNED_256 in place, then rebuilds the color pairs when curses already runs."""
    key = name if name in THEMES else DEFAULT_THEME
    spec = THEMES[key]
    PALETTE.clear()
    PALETTE.update(spec["palette"])
    BACKGROUNDS.clear()
    BACKGROUNDS.update(spec["backgrounds"])
    PINNED_256.clear()
    PINNED_256.update(spec["pinned_256"])
    _ACTIVE["name"] = key
    if THEME.mode not in ("none", "mono"):
        THEME.init()
    else:
        THEME._attr_cache.clear()
    return key


def theme_name() -> str:
    """The active theme's name."""
    return _ACTIVE["name"]


def art_enabled() -> bool:
    """True when views should leave room for the theme's art. Before curses starts (tests, line
    builders) this follows the theme alone; once it runs, the color mode must allow the art too."""
    if not THEMES[_ACTIVE["name"]]["art"]:
        return False
    return THEME.mode == "none" or THEME.art_ready()


def init_colors() -> str:
    """Starts colors for the running curses screen; returns the mode used."""
    return THEME.init()


def attr(style: str) -> int:
    """curses attribute for a style string. Before init it returns only bold/underline/reverse flags."""
    return THEME.attr(style)


def kind_style(kind: Optional[str]) -> Tuple[str, str]:
    """(glyph, style) for a toast or status kind: ok, warn, error, info."""
    return {
        "ok": (GLYPHS["done"] + " ", "green"),
        "warn": (GLYPHS["over"] + " ", "yellow"),
        "error": (GLYPHS["fail"] + " ", "red"),
    }.get(kind or "info", ("", "muted"))
