"""Add / Edit money entry boxes for the Cockpit (drawing and key handling in one controller).

The controller never touches curses or the TUI. The TUI calls:

    modals = FinanceModals(panel_getter=lambda: finance_panel)
    modals.open_add(date)                    # Add box: one step, day prefilled (today when date is None)
    err = modals.open_edit(day, item_idx)    # Edit box; item_idx None + several items = pick an item first
    res = modals.handle_key(ch)              # -> ModalResult; the TUI applies it
    res = modals.handle_keys([ch, ...])      # a burst read in one go = a paste (newlines never save)
    modals.paste(text)                       # optional: bracketed paste text, several lines at once
    box = modals.render(cols, rows)          # -> Overlay (lines + screen position) or None
    groups = modals.keys()                   # footer keys while the box is open

ModalResult fields: closed (box closed), changed (ledger changed: refresh data), toast + toast_kind
(message for the toast row), select_item (move the item selection in the Day screen), select_day
(move the day selection; index in the fresh day list, so apply it after the refresh), day_gone (the
day has no entries left: go back to the day overview). selection_after_delete(days, date, item_idx) gives the same answer for deletes
made outside the box.

Add box: every typed line gets a live preview (category, account, kind, signed amount). Lines that do
not parse show red with a short reason and are not saved. Tab switches the default account
(Checking / Visa) for lines that do not name one. Edit box: Account, Category and Text fields; Tab
moves between them; arrows change the focused choice.

Hidden amounts (mask_getter returns True, the Money "m" toggle): every amount the box shows on its own
(previews, totals, the day menu, toasts) reads $•••, and the numbers in the Edit text that came from
the ledger read •••. What Sam types in the box stays visible, so he can check his own keys.
"""
from __future__ import annotations

import inspect
import re
import unicodedata
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from telemachus.cockpit.finance_panel import EDIT_CATEGORIES

try:
    import curses

    KEY_ENTER = curses.KEY_ENTER
    KEY_BACKSPACE = curses.KEY_BACKSPACE
    KEY_LEFT = curses.KEY_LEFT
    KEY_RIGHT = curses.KEY_RIGHT
    KEY_UP = curses.KEY_UP
    KEY_DOWN = curses.KEY_DOWN
    KEY_DC = curses.KEY_DC
    KEY_BTAB = curses.KEY_BTAB
    KEY_MOUSE = curses.KEY_MOUSE
    KEY_RESIZE = curses.KEY_RESIZE
except ImportError:  # pragma: no cover
    KEY_ENTER, KEY_BACKSPACE, KEY_LEFT, KEY_RIGHT, KEY_UP, KEY_DOWN, KEY_DC, KEY_BTAB, KEY_MOUSE, KEY_RESIZE = (
        343, 263, 260, 261, 259, 258, 330, 353, 409, 410)

from telemachus.cockpit.shell import KeyGroup, KeyHint, Overlay
from telemachus.cockpit.theme import GLYPHS, Line, cell_width, clip, fit, line_width, money_text, resolve

# Edit box account choices. "Savings" is the Capital One account (finance_panel maps both names).
ACCOUNTS = ("Checking", "Visa", "Savings")
# Add box default account for lines without a marker.
ADD_ACCOUNTS = ("Checking", "Visa")
EDIT_FIELDS = ("account", "category", "text")

ESC = 27
CTRL_D = 4
CTRL_S = 19
CTRL_U = 21
TAB = 9
ENTER_KEYS = (10, 13, KEY_ENTER)
BACKSPACE_KEYS = (KEY_BACKSPACE, 127, 8)
CURSOR = "█"

# A burst of keys (read in one go) counts as a paste when it has this many text characters, or when
# text follows a newline in it. Shorter bursts are keys typed while the screen was busy.
PASTE_MIN_TEXT = 8
_PASTE_START = [ord(c) for c in "[200~"]
_PASTE_END = [ord(c) for c in "[201~"]

BOX_MAX_W = 74
BOX_MIN_W = 30

# Category keys from the bullet parser -> labels shown in the preview.
_CATEGORY_LABELS: Dict[str, str] = {key: label for label, key in EDIT_CATEGORIES if key}
_CATEGORY_LABELS.update({
    "payday": "Income",
    "salary": "Income",
    "income": "Income",
    "savings": "Savings",
    "credit_card_payment": "Card payment",
})

_KIND_NAMES = {"card_payment": "payment", "payment": "payment", "spend": "spend", "income": "income",
               "transfer": "transfer"}


@dataclass
class ModalResult:
    closed: bool = False
    changed: bool = False
    toast: Optional[str] = None
    toast_kind: str = "info"  # ok | warn | error | info
    select_item: Optional[int] = None
    select_day: Optional[int] = None  # move the day selection (index in the fresh day list)
    day_gone: bool = False  # the edited day has no entries left: go back to the day overview


@dataclass
class LinePreview:
    """What one typed line will become. amount_cents is signed: negative = money out."""

    text: str
    ok: bool
    category: str = ""
    account: str = ""  # Checking | Visa | Savings
    kind: str = ""  # spend | payment | income | transfer
    amount_cents: int = 0
    reason: str = ""  # why the line cannot be saved (ok False)
    marked: bool = False  # the line names its own account ("visa", "on card", "debit", ...)
    defaulted: bool = False  # a spend with no marker that takes the default account (Visa)
    to_checking: bool = False  # a savings -> checking move: money in to checking, shown as +


# ---------------------------------------------------------------------------
# Line preview: card logic's preview_bullet_line, with a local fallback
# ---------------------------------------------------------------------------

_DEBIT_RE = re.compile(r"(?<![A-Za-z])debit(?![A-Za-z])", re.IGNORECASE)
# A trailing account word the Edit box can drop when Sam picks another account by hand.
_TRAILING_MARKER_RE = re.compile(
    r"\s+(?:(?:paid\s+with|put\s+on|on|with|via|using)\s+)?[@(\[]?(?:visa|card|cc|credit|debit)(?:\s+card)?[)\]]?\s*$",
    re.IGNORECASE)
VISA_MARKER = " on visa"  # appended to an unmarked spend when Visa is the default account


def _card_preview(text: str, panel: Any = None) -> Optional[Dict[str, Any]]:
    """panel.preview_bullet_line(text) when the panel has it (it names custom setup.yaml bills,
    "phone bill 80" -> Phone), else import_text.preview_bullet_line(text), else None."""
    fn = getattr(panel, "preview_bullet_line", None) if panel is not None else None
    if callable(fn):
        try:
            raw = fn(text)
        except Exception:
            raw = None
        if isinstance(raw, dict):
            return raw
    try:
        from telemachus.finance import import_text
    except Exception:  # pragma: no cover
        return None
    fn = getattr(import_text, "preview_bullet_line", None)
    if not callable(fn):
        return None
    try:
        raw = fn(text)
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _no_amount_reason(text: str) -> str:
    words = re.sub(r"[^A-Za-z ]", " ", text).split()
    example = words[0].lower() if words else "lunch"
    return f"No amount found. Add a number, like: {example} 6.50"


def _fallback(text: str) -> LinePreview:
    """Preview from parse_bullet_line only (used when preview_bullet_line is missing)."""
    from telemachus.finance.import_text import parse_bullet_line

    try:
        item = parse_bullet_line(text)
    except Exception:
        item = None
    if not item:
        if not re.search(r"\d", text):
            return LinePreview(text=text, ok=False, reason=_no_amount_reason(text))
        return LinePreview(text=text, ok=False, reason="Could not read this line. Example: groceries 25.50")
    cents = int(item.get("amount_cents") or round(float(item.get("amount", 0)) * 100))
    cat = str(item.get("category", "personal")).lower()
    kind = str(item.get("kind") or "")
    if not kind:
        kind = {"payday": "income", "salary": "income", "income": "income", "savings": "transfer",
                "credit_card_payment": "card_payment"}.get(cat, "spend")
    account = str(item.get("account_short") or "")
    if not account:
        account = "Visa" if "Visa" in str(item.get("from_account", "")) else "Checking"
    return LinePreview(text=text, ok=cents > 0, category=_CATEGORY_LABELS.get(cat, cat.title()), account=account,
                       kind=_KIND_NAMES.get(kind, "spend"), amount_cents=cents,
                       reason="" if cents > 0 else "Amount must be more than $0.")


def preview_line(text: str, default_account: str = "Checking", panel: Any = None) -> LinePreview:
    """Preview of one typed line. default_account ("Checking" or "Visa") applies to spends that
    do not name an account; "debit" in the line keeps it on checking. With a panel, its
    preview_bullet_line is used (custom bills by name)."""
    raw = text.strip()
    if not raw:
        return LinePreview(text=raw, ok=False, reason="")
    got = _card_preview(raw, panel)
    to_checking = False
    if got is None:
        p = _fallback(raw)
    elif not got.get("ok"):
        reason = str(got.get("error") or "Could not read this line.")
        if reason.lower().startswith("no amount found") and not re.search(r"\d", raw):
            reason = _no_amount_reason(raw)  # an example built from the typed word reads easier
        p = LinePreview(text=raw, ok=False, reason=reason)
    else:
        cents = abs(int(got.get("amount_cents") or 0))
        cat = str(got.get("category_label") or "Discretionary")
        p = LinePreview(text=raw, ok=True, category=_CATEGORY_LABELS.get(cat.lower(), cat),
                        account=str(got.get("account_short") or "Checking"),
                        kind=_KIND_NAMES.get(str(got.get("kind") or "spend"), "spend"), amount_cents=cents)
        to_checking = p.kind == "transfer" and (
            str(got.get("from_account") or "").startswith("Assets:Savings")
            or str(got.get("category_label") or "") == "Savings withdrawal")
    if not p.ok:
        return p
    if p.kind == "transfer" and not to_checking and p.account == "Savings":
        # Fallback path: parse_bullet_line gives account_short Savings for a withdrawal.
        to_checking = p.category == "Savings withdrawal"
    p.to_checking = to_checking
    p.amount_cents = abs(p.amount_cents) if p.kind == "income" or to_checking else -abs(p.amount_cents)
    if p.kind == "spend":
        p.marked = p.account != "Checking" or bool(_DEBIT_RE.search(raw))
        if not p.marked and default_account == "Visa":
            p.account = "Visa"
            p.defaulted = True
    return p


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


_NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d*)?")


def _money(cents: int, mask: bool = False, signed: bool = False) -> str:
    """money_text, but a masked amount keeps its sign: '-$•••' (like the Money screens)."""
    if not mask:
        return money_text(cents, signed=signed)
    sign = "-" if cents < 0 else ("+" if signed and cents > 0 else "")
    return (sign if signed else "") + "$" + GLYPHS["hidden"]


def mask_numbers(text: str) -> str:
    """'- lunch - 13.15' -> '- lunch - •••' (every number, with its $ sign, becomes the hidden glyph)."""
    return _NUMBER_RE.sub(GLYPHS["hidden"], text)


def _typed_char_ok(text: str) -> bool:
    """True for typed text the box can show without jitter: printable one-cell characters, and
    two-cell letters, digits and punctuation (Korean, Chinese, Japanese, full-width forms). Emoji and
    other wide symbols are dropped (their width differs between terminals)."""
    if not text or not text.isprintable():
        return False
    for c in text:
        w = cell_width(c)
        if w == 1:
            continue
        if w != 2 or ord(c) > 0xFFFF or unicodedata.category(c)[0] not in "LNP":
            return False
    return True


def _tail(text: str, width: int) -> str:
    """The end of `text` in at most `width` cells (never splits a wide character)."""
    out: List[str] = []
    used = 0
    for ch in reversed(text):
        w = cell_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(reversed(out))


def _clean(text: str) -> str:
    """Drops emoji and other wide-or-odd symbols so rows never jitter."""
    return "".join(c for c in str(text) if ord(c) <= 0xFFFF and c != "️").strip()


def _on_bg(line: Sequence[Tuple[str, str]], bg: str) -> Line:
    """Puts segments on background `bg`, but keeps a background a segment already has."""
    out: Line = []
    for text, style in line:
        base = resolve(style)
        out.append((text, base if "@" in base else f"{base}@{bg}"))
    return out


def _right(left: Line, right: Line, width: int) -> Line:
    """Left part, then `right` flush with the right edge; the left part is cut first."""
    rw = line_width(right)
    if rw >= width:
        return fit(right, width)
    room = width - rw - 1
    left = fit(left, room) if line_width(left) > room else list(left)
    return left + [(" " * (width - line_width(left) - rw), "text")] + list(right)


def _keys_row(hints: Sequence[Tuple[str, ...]], width: int) -> Line:
    """'↵ save   tab next   esc cancel' in the box. hints = (key, label, key_style[, drop]): when the row
    is too wide, the hint with the highest drop number goes first (0 = never dropped)."""
    items = [tuple(h) + (0,) * (4 - len(h)) for h in hints]
    while True:
        out: Line = []
        for i, (k, label, style, _drop) in enumerate(items):
            if i:
                out.append(("   ", "text"))
            out += [(k, style), (" " + label, "red" if style == "red" else "muted")]
        droppable = [h for h in items if h[3]]
        if line_width(out) <= width or not droppable:
            return fit(out, width) if line_width(out) > width else out
        items.remove(max(droppable, key=lambda h: h[3]))


def _day_name(value: str) -> str:
    """'Wed Oct 28' for '2026-10-28' (the value itself when it is not a date)."""
    try:
        d = datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return str(value)
    return f"{d:%a %b} {d.day}"


def _date_label(value: str, today: date_cls) -> Tuple[str, str]:
    """('today, Wed Oct 28', style). Future days are yellow (attention)."""
    try:
        d = datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return (value or "?", "red+b")
    name = f"{d:%a %b} {d.day}"
    delta = (d - today).days
    if delta == 0:
        return (f"today, {name}", "text+b")
    if delta == -1:
        return (f"yesterday, {name}", "text+b")
    if delta == 1:
        return (f"tomorrow, {name}", "yellow+b")
    if delta < 0:
        return (f"{name}, {-delta} days ago", "text+b")
    return (f"{name}, in {delta} days", "yellow+b")


def selection_after_delete(days: Sequence[Dict[str, Any]], date: str, item_idx: int) -> Tuple[int, int, bool]:
    """Where the selection goes after entries of `date` were deleted. `days` is the fresh day list
    (newest first, only days with entries). Returns (day_idx, item_idx, gone): the same day when it
    still has entries (item index clamped), else gone=True and the nearest remaining day (the next
    older one, or the newest when none is older) with item 0."""
    for i, d in enumerate(days):
        if d.get("date") == date:
            n = len(d.get("items") or [])
            return i, max(0, min(item_idx, n - 1)), False
    older = [i for i, d in enumerate(days) if str(d.get("date", "")) < date]
    return (older[0] if older else max(0, len(days) - 1)), 0, True


def _day_list(panel: Any) -> List[Dict[str, Any]]:
    try:
        return list(panel.get_daily_ledger() or [])
    except Exception:
        return []


def _looks_like_paste(keys: Sequence[int]) -> bool:
    """True for a burst that reads as pasted text (see FinanceModals.handle_keys)."""
    text = 0
    after_newline = False
    seen_newline = False
    for k in keys:
        if k in ENTER_KEYS:
            seen_newline = True
        elif 32 <= k <= 126 or 0xC0 <= k <= 0xF7:  # a character (a UTF-8 lead byte counts once)
            text += 1
            after_newline = after_newline or seen_newline
    return text >= PASTE_MIN_TEXT or after_newline


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class FinanceModals:
    """State and behavior of the Add and Edit boxes. Only one box is open at a time."""

    def __init__(
        self,
        panel_getter: Callable[[], Any],
        day_getter: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
        today: Optional[Callable[[], date_cls]] = None,
        mask_getter: Optional[Callable[[], bool]] = None,
    ):
        self._panel_getter = panel_getter
        self._day_getter = day_getter
        self._mask_getter = mask_getter
        self._today_fn = today or (lambda: datetime.now().date())
        self.kind: Optional[str] = None  # None | "add" | "edit"
        # Add box. add_stage stays for the old proxies; the box has one step ("bullets").
        self.add_stage = "bullets"
        self.date_buffer = ""
        self.line_buffer = ""
        self.batch_lines: List[str] = []
        self.default_account = "Checking"
        self.confirm: Optional[str] = None  # armed confirm: "cancel" | "skip" | "delete" | "clear_day"
        # Edit box
        self.edit_stage = "item"  # "item" or "day_menu"
        self.day: Optional[Dict[str, Any]] = None
        self.tx_id: Optional[str] = None
        self.edit_date = ""
        self.orig_text = ""
        self.edit_buffer = ""
        self.account_idx = 0
        self.orig_account_idx = 0
        self.orig_kind = "spend"  # kind of the entry being edited: spend | payment | income | transfer
        self.category_idx = 0  # index into EDIT_CATEGORIES; 0 = Auto
        self.current_category = ""
        self.edit_field = "text"  # focused field: account | category | text
        self.menu_idx = 0  # highlighted item in the day menu
        self.from_menu = False  # the item was picked from the day menu (esc goes back to it)
        self.item_label = ""
        self._label_from_text = False  # item_label is the entry text (no payee): mask its numbers
        self._ledger_end = 0  # edit_buffer[:_ledger_end] is still the ledger's text (masked when hidden)
        # Input decoding
        self._in_paste = False  # True while handle_keys runs a burst of several keys
        self._utf8: List[int] = []
        self._utf8_need = 0

    # ------------------------------------------------------------------
    # Open / close
    # ------------------------------------------------------------------

    @property
    def panel(self) -> Any:
        return self._panel_getter()

    @property
    def active(self) -> bool:
        return self.kind is not None

    @property
    def mask(self) -> bool:
        """True while the Money screen hides amounts ("m")."""
        if self._mask_getter is None:
            return False
        try:
            return bool(self._mask_getter())
        except Exception:
            return False

    def _label(self) -> str:
        """The entry name for toasts and the delete confirm; never shows a hidden amount."""
        label = self.item_label or "the entry"
        return mask_numbers(label) if self.mask and self._label_from_text else label

    def edit_text_shown(self) -> str:
        """The Edit text as drawn: with amounts hidden, numbers that came from the ledger read •••."""
        buf = self.edit_buffer
        if not self.mask:
            return buf
        end = max(0, min(self._ledger_end, len(buf)))
        return mask_numbers(buf[:end]) + buf[end:]

    def close(self) -> None:
        self.kind = None
        self.confirm = None

    def _today(self) -> date_cls:
        return self._today_fn()

    def open_add(self, date: Optional[str] = None) -> None:
        self.kind = "add"
        self.add_stage = "bullets"
        self.date_buffer = date or self._today().strftime("%Y-%m-%d")
        self.line_buffer = ""
        self.batch_lines = []
        self.default_account = "Checking"  # each box starts on checking; tab switches
        self.confirm = None
        self._utf8 = []

    def open_edit(self, day: Optional[Dict[str, Any]], item_idx: Optional[int] = None) -> Optional[str]:
        """Opens the Edit box for one day. item_idx None (and more than one item) = day menu first.

        Returns an error message (and stays closed) when there is nothing to edit.
        """
        if not day:
            return "No day selected to edit."
        items = day.get("items", [])
        if not items:
            return f"No transactions to edit for {day.get('date', '')}."
        self.kind = "edit"
        self.day = day
        self.edit_date = day["date"]
        self.confirm = None
        self.from_menu = False
        self._utf8 = []
        if item_idx is not None or len(items) == 1:
            idx = max(0, min(item_idx or 0, len(items) - 1))
            self.menu_idx = idx
            self.begin_item_edit(items[idx])
        else:
            self.edit_stage = "day_menu"
            self.edit_buffer = ""
            self.menu_idx = 0
        return None

    def begin_item_edit(self, target: Dict[str, Any]) -> None:
        """Loads one ledger item into the Edit box (text, account, category)."""
        self.edit_stage = "item"
        self.edit_field = "text"
        self.confirm = None
        self.tx_id = target["id"]
        bullet = self.panel.item_to_bullet(target)
        self.orig_text = bullet
        self.edit_buffer = bullet
        payee = _clean(target.get("payee") or "")
        self.item_label = payee or _clean(bullet)
        self._label_from_text = not payee
        self._ledger_end = len(bullet)
        # Auto = let the text decide. A purchase whose category is not what the text gives (a category
        # picked by hand before) opens on that category, so saving keeps it.
        self.category_idx = 0
        self.current_category = str(target.get("category") or "")
        kind = str(target.get("kind") or ("income" if target.get("is_income") else "spend"))
        if kind == "spend" and self.current_category:
            auto = preview_line(bullet)
            cur = self.current_category.strip().lower()
            if not (auto.ok and auto.kind == "spend" and auto.category.lower() == cur):
                for i, (label, key) in enumerate(EDIT_CATEGORIES):
                    if key and label.lower() == cur:
                        self.category_idx = i
                        break
        acc = str(target.get("account_short") or target.get("account", "")).lower()
        if "visa" in acc or "card" in acc:
            self.account_idx = 1
        elif "savings" in acc or "capital" in acc:
            self.account_idx = 2
        else:
            self.account_idx = 0
        self.orig_account_idx = self.account_idx
        self.orig_kind = _KIND_NAMES.get(kind, "spend")

    def _items(self) -> List[Dict[str, Any]]:
        day = self.day
        if day is None and self._day_getter:
            day = self._day_getter()
        return list((day or {}).get("items", []))

    # ------------------------------------------------------------------
    # Input decoding (UTF-8 bytes from getch) and pastes
    # ------------------------------------------------------------------

    def _decode(self, ch: int) -> Optional[str]:
        """Text for a printable key, assembling UTF-8 byte runs. None = not text (yet)."""
        if 32 <= ch <= 126:
            self._utf8 = []
            return chr(ch)
        if 0xC0 <= ch <= 0xF7:
            self._utf8 = [ch]
            self._utf8_need = 1 if ch < 0xE0 else (2 if ch < 0xF0 else 3)
            return None
        if 0x80 <= ch <= 0xBF and self._utf8:
            self._utf8.append(ch)
            if len(self._utf8) - 1 < self._utf8_need:
                return None
            raw = bytes(self._utf8)
            self._utf8 = []
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
            # Fancy dashes and bullets from notes apps become plain ones the parser knows.
            for dash in ("\u2013", "\u2014", "\u2212", "\u2022"):  # en, em, minus sign, bullet
                text = text.replace(dash, "-")
            return text if _typed_char_ok(text) else None
        return None

    def paste(self, text: str) -> ModalResult:
        """Bracketed paste: each pasted line becomes its own entry line (Add) or text (Edit)."""
        if self.kind == "add":
            parts = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            for i, part in enumerate(parts):
                self.line_buffer += part.replace("\t", " ")
                if i < len(parts) - 1 and self.line_buffer.strip():
                    self.batch_lines.append(self.line_buffer.strip())
                    self.line_buffer = ""
            self.confirm = None
        elif self.kind == "edit" and self.edit_stage == "item":
            before = self._marker_account()
            self.edit_buffer += " ".join(p.strip() for p in text.splitlines() if p.strip())
            self.edit_field = "text"
            self.confirm = None
            self._sync_account(before)
        return ModalResult()

    # ------------------------------------------------------------------
    # Keys
    # ------------------------------------------------------------------

    def handle_keys(self, keys: Sequence[int]) -> ModalResult:
        """A burst of keys read in one go (the TUI drains its input queue). Stops at the key that
        closes the box.

        Keys between bracketed paste markers (esc [ 2 0 0 ~ ... esc [ 2 0 1 ~) are a paste; the
        markers are dropped. Without markers the burst is a paste only when it looks like one: at
        least PASTE_MIN_TEXT text characters, or text after a newline. Inside a paste newlines only
        add lines and never save, tabs become spaces and [ ] are text. A short burst (keys typed
        while the screen was busy) works like the same keys one by one.
        """
        keys = list(keys)
        tagged: List[Tuple[int, bool]] = []  # (key, inside bracketed paste)
        bracketed = False
        inside = False
        i = 0
        while i < len(keys):
            if keys[i] == ESC and keys[i + 1: i + 6] in (_PASTE_START, _PASTE_END):
                inside = keys[i + 1: i + 6] == _PASTE_START
                bracketed = True
                i += 6
                continue
            tagged.append((keys[i], inside))
            i += 1
        loose = [k for k, ins in tagged if not ins]
        loose_paste = not bracketed and _looks_like_paste(loose)
        total = ModalResult()
        try:
            for ch, ins in tagged:
                self._in_paste = ins or loose_paste
                res = self.handle_key(ch)
                total.changed = total.changed or res.changed
                if res.toast:
                    total.toast, total.toast_kind = res.toast, res.toast_kind
                if res.select_item is not None:
                    total.select_item = res.select_item
                if res.select_day is not None:
                    total.select_day = res.select_day
                total.day_gone = total.day_gone or res.day_gone
                if res.closed:
                    total.closed = True
                    break
        finally:
            self._in_paste = False
        return total

    def handle_key(self, ch: int) -> ModalResult:
        if ch in (KEY_MOUSE, KEY_RESIZE):
            return ModalResult()  # a click, scroll or resize never types, saves or disarms a confirm
        rapid = self._in_paste
        if self.kind == "add":
            return self._add_key(ch, rapid)
        if self.kind == "edit":
            if self.edit_stage == "day_menu":
                return self._menu_key(ch)
            return self._edit_key(ch, rapid)
        return ModalResult()

    # Add -----------------------------------------------------------------

    def _dirty(self) -> bool:
        return bool(self.batch_lines or self.line_buffer.strip())

    def all_lines(self) -> List[str]:
        """Saved-on-commit lines: the finished ones plus the line being typed."""
        lines = list(self.batch_lines)
        if self.line_buffer.strip():
            lines.append(self.line_buffer.strip())
        return lines

    def previews(self) -> List[LinePreview]:
        return [preview_line(t, self.default_account, panel=self.panel) for t in self.all_lines()]

    def shift_day(self, step: int) -> None:
        try:
            d = datetime.strptime(self.date_buffer, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            d = self._today()
        self.date_buffer = (d + timedelta(days=step)).strftime("%Y-%m-%d")

    def _add_key(self, ch: int, rapid: bool) -> ModalResult:
        if ch == ESC:
            if not self._dirty():
                self.close()
                return ModalResult(closed=True)
            if self.confirm == "cancel":
                n = len(self.all_lines())
                self.close()
                return ModalResult(closed=True, toast=f"Dropped {n} unsaved {'line' if n == 1 else 'lines'}.")
            self.confirm = "cancel"
            return ModalResult()
        if ch == CTRL_S:
            return self._save_add()
        armed, self.confirm = self.confirm, None
        if ch in ENTER_KEYS:
            line = self.line_buffer.strip()
            if line:
                self.batch_lines.append(line)
                self.line_buffer = ""
                return ModalResult()
            if rapid:
                return ModalResult()  # blank line inside a paste: never saves
            if self.batch_lines:
                self.confirm = armed if armed == "skip" else None
                return self._save_add()
            return ModalResult(toast="Type an entry first, like: lunch 13.15")
        if ch in BACKSPACE_KEYS:
            if self.line_buffer:
                self.line_buffer = self.line_buffer[:-1]
            elif self.batch_lines:
                self.line_buffer = self.batch_lines.pop()
            return ModalResult()
        if ch == CTRL_U:
            self.line_buffer = ""
            return ModalResult()
        if ch == KEY_LEFT or (ch == ord("[") and not rapid):
            self.shift_day(-1)
            return ModalResult()
        if ch == KEY_RIGHT or (ch == ord("]") and not rapid):
            self.shift_day(1)
            return ModalResult()
        if ch in (TAB, KEY_BTAB):
            if rapid and ch == TAB:
                self.line_buffer += " "
                return ModalResult()
            i = ADD_ACCOUNTS.index(self.default_account) if self.default_account in ADD_ACCOUNTS else 0
            self.default_account = ADD_ACCOUNTS[(i + 1) % len(ADD_ACCOUNTS)]
            return ModalResult()
        text = self._decode(ch)
        if text:
            self.line_buffer += text
        return ModalResult()

    def _save_add(self) -> ModalResult:
        previews = self.previews()
        if not previews:
            return ModalResult(toast="Nothing to save yet. Type an entry, like: lunch 13.15")
        good = [p for p in previews if p.ok]
        bad = len(previews) - len(good)
        if not good:
            self.confirm = None
            return ModalResult(toast="Nothing to save: every line needs a fix.", toast_kind="error")
        if bad and self.confirm != "skip":
            self.confirm = "skip"
            return ModalResult()
        if self.line_buffer.strip():
            self.batch_lines.append(self.line_buffer.strip())
            self.line_buffer = ""
        date = self.date_buffer
        try:
            logged = self._log_lines(date, good)
        except Exception as exc:  # keep the lines so nothing typed is lost
            self.confirm = None
            return ModalResult(toast=f"Could not save: {exc}", toast_kind="error")
        count = len(logged) if isinstance(logged, (list, tuple)) else len(good)
        select_day, select_item = self._follow_saved(date, logged)
        when = _day_name(date)
        noun = "entry" if count == 1 else "entries"
        on_visa = sum(1 for p in good if p.kind == "spend" and p.account == "Visa")
        visa_note = f" ({on_visa} on Visa)" if on_visa else ""
        if bad:
            # The box stays open with only the lines that need a fix; the last one is ready to edit.
            keep = [p.text for p in previews if not p.ok]
            self.batch_lines, self.line_buffer = keep[:-1], keep[-1]
            self.confirm = None
            left = "line still needs" if bad == 1 else "lines still need"
            return ModalResult(changed=True, toast_kind="warn", select_day=select_day, select_item=select_item,
                               toast=f"Saved {count} {noun}{visa_note} for {when}. {bad} {left} a fix.")
        self.close()
        return ModalResult(closed=True, changed=True, toast=f"Saved {count} {noun}{visa_note} for {when}.", toast_kind="ok",
                           select_day=select_day, select_item=select_item)

    def _follow_saved(self, date: str, logged: Any) -> Tuple[Optional[int], Optional[int]]:
        """(day index, item index) of the first saved entry in the fresh day list, so the Money screen
        moves to the day the entries went to. (None, None) when that day is not in the list."""
        ids = set()
        if isinstance(logged, (list, tuple)):
            for tx in logged:
                tx_id = getattr(tx, "id", None) or (tx.get("id") if isinstance(tx, dict) else None)
                if tx_id:
                    ids.add(tx_id)
        for di, d in enumerate(_day_list(self.panel)):
            if d.get("date") != date:
                continue
            for ii, it in enumerate(d.get("items") or []):
                if it.get("id") in ids:
                    return di, ii
            return di, 0
        return None, None

    def _log_lines(self, date: str, good: List[LinePreview]) -> Any:
        """Writes the good lines through panel.log_day_bullets(date, text).

        A spend that takes the Visa default gets the " on visa" marker the bullet parser knows, so
        there is one write path. If the parser would read the marked line differently, that line
        goes in through the store with the Visa account set directly.
        """
        panel = self.panel
        if any(p.defaulted for p in good):
            try:
                params = inspect.signature(panel.log_day_bullets).parameters
            except (TypeError, ValueError):
                params = {}
            if "default_account" in params:  # card logic took the default over
                return panel.log_day_bullets(date, "\n".join(p.text for p in good), default_account=self.default_account)
        texts: List[str] = []
        direct: List[LinePreview] = []
        for p in good:
            if not p.defaulted:
                texts.append(p.text)
                continue
            marked = p.text + VISA_MARKER
            q = preview_line(marked, panel=self.panel)
            if q.ok and q.kind == "spend" and q.account == "Visa" and q.amount_cents == p.amount_cents:
                texts.append(marked)
            else:
                direct.append(p)
        logged: List[Any] = []
        if texts:
            logged += list(panel.log_day_bullets(date, "\n".join(texts)) or [])
        if direct:
            from telemachus.finance.import_text import parse_bullet_line

            items = []
            for p in direct:
                item = parse_bullet_line(p.text)
                if item:
                    item.update(date=date, from_account="Liabilities:CreditCard:Visa", account_short="Visa")
                    match_bill = getattr(panel, "match_bill", None)
                    if callable(match_bill):
                        match_bill(item)  # a custom bill paid by card still marks the bill paid
                    items.append(item)
            if items:
                logged += list(panel.store.log_batch_transactions(items, default_date=date, allow_duplicates=True))
                refresh = getattr(panel, "refresh", None)
                if callable(refresh):
                    refresh(force=True)
        return logged

    # Edit: day menu -------------------------------------------------------

    def _menu_key(self, ch: int) -> ModalResult:
        items = self._items()
        if ch == ESC:
            self.close()
            return ModalResult(closed=True, toast="Edit cancelled.")
        if ch == CTRL_D:
            if self.confirm == "clear_day":
                count = self.panel.delete_day(self.edit_date)
                self.close()
                day_idx, _item, _gone = selection_after_delete(_day_list(self.panel), self.edit_date, 0)
                return ModalResult(closed=True, changed=True, toast_kind="ok", select_item=0, select_day=day_idx,
                                   day_gone=True,
                                   toast=f"Cleared {count} entries for {_day_name(self.edit_date)}. Back to the days.")
            self.confirm = "clear_day"
            self.edit_buffer = ""
            return ModalResult()
        self.confirm = None
        if ch in ENTER_KEYS:
            cmd = self.edit_buffer.strip()
            self.edit_buffer = ""
            idx = int(cmd) - 1 if cmd.isdigit() else self.menu_idx
            if not 0 <= idx < len(items):
                return ModalResult(toast=f"Pick a number from 1 to {len(items)}.", toast_kind="warn")
            self.menu_idx = idx
            self.begin_item_edit(items[idx])
            self.from_menu = True
            return ModalResult(select_item=idx)
        if ch in (KEY_UP, ord("k")):
            self.menu_idx = max(0, self.menu_idx - 1)
            self.edit_buffer = ""
        elif ch in (KEY_DOWN, ord("j")):
            self.menu_idx = min(max(0, len(items) - 1), self.menu_idx + 1)
            self.edit_buffer = ""
        elif ch in BACKSPACE_KEYS:
            self.edit_buffer = self.edit_buffer[:-1]
        elif 48 <= ch <= 57:
            self.edit_buffer = (self.edit_buffer + chr(ch))[-3:]
            num = int(self.edit_buffer)
            if 1 <= num <= len(items):
                self.menu_idx = num - 1
            else:
                self.edit_buffer = chr(ch)
                if 1 <= ch - 48 <= len(items):
                    self.menu_idx = ch - 49
        return ModalResult()

    # Edit: one item -------------------------------------------------------

    def edit_preview(self) -> LinePreview:
        """Preview of the Edit text with the chosen account and category applied.

        Mirrors FinanceTuiPanel.edit_transaction: an account picked by hand (different from the
        entry's own) wins; otherwise a card word in the text can move a purchase to Visa, and a
        card payment's "from savings" counts. Typing a marker also moves the field (_sync_account)
        and picking an account by hand drops a trailing marker (_strip_marker), so the field and
        the preview agree.
        """
        p = preview_line(self.edit_buffer)
        if not p.ok:
            return p
        label, key = EDIT_CATEGORIES[self.category_idx]
        if key:
            p.category = label
            if p.kind != "spend":
                p.kind = "spend"
                p.amount_cents = -abs(p.amount_cents)
        elif p.kind == "spend" and self.orig_kind != "spend":
            # Plain text keeps the entry's kind: editing a payment to "30" stays a payment.
            p.kind = self.orig_kind
            p.category = {"payment": "Card payment", "income": "Income", "transfer": "Savings"}[self.orig_kind]
            p.amount_cents = abs(p.amount_cents) if p.kind == "income" else -abs(p.amount_cents)
            p.account = ACCOUNTS[self.orig_account_idx] if p.kind == "payment" else "Checking"
            p.marked = False
        choice = ACCOUNTS[self.account_idx]
        changed = self.account_idx != self.orig_account_idx
        if p.kind == "spend":
            text_card = p.marked and p.account == "Visa" and ACCOUNTS[self.orig_account_idx] != "Visa"
            p.account = choice if changed or not text_card else "Visa"
        elif p.kind == "income":
            p.account = choice
        elif p.kind == "payment":
            if choice in ("Checking", "Savings") and (changed or p.account == "Checking"):
                p.account = choice
        else:
            p.account = "Checking"
        return p

    def allowed_accounts(self) -> Tuple[str, ...]:
        """Accounts the Account field offers for the kind the text has now."""
        p = self.edit_preview()
        kind = p.kind if p.ok else "spend"
        return {"spend": ACCOUNTS, "income": ("Checking", "Savings"), "payment": ("Checking", "Savings"),
                "transfer": ("Checking",)}.get(kind, ACCOUNTS)

    def _marker_account(self) -> Optional[str]:
        p = preview_line(self.edit_buffer)
        return p.account if p.ok and p.kind == "spend" and p.marked else None

    def _sync_account(self, before: Optional[str]) -> None:
        """After a text change: a marker that appeared or changed moves the Account field to it."""
        now = self._marker_account()
        if now and now != before and now in ACCOUNTS:
            self.account_idx = ACCOUNTS.index(now)

    def _strip_marker(self) -> None:
        """Drops a trailing account word ("visa", "on card", "(debit)") that disagrees with the field."""
        marker = self._marker_account()
        if not marker or marker == ACCOUNTS[self.account_idx]:
            return
        cut = _TRAILING_MARKER_RE.sub("", self.edit_buffer)
        if cut != self.edit_buffer and preview_line(cut).ok:
            self.edit_buffer = cut
            self._ledger_end = min(self._ledger_end, len(cut))

    def _move_field(self, step: int) -> None:
        i = EDIT_FIELDS.index(self.edit_field) if self.edit_field in EDIT_FIELDS else 2
        self.edit_field = EDIT_FIELDS[(i + step) % len(EDIT_FIELDS)]

    def _change_choice(self, field_name: str, step: int) -> ModalResult:
        if field_name == "account":
            allowed = self.allowed_accounts()
            if len(allowed) < 2:
                return ModalResult(toast="Savings transfers always come from checking.", toast_kind="warn")
            cur = ACCOUNTS[self.account_idx]
            i = allowed.index(cur) if cur in allowed else (-1 if step > 0 else 0)
            self.account_idx = ACCOUNTS.index(allowed[(i + step) % len(allowed)])
            self._strip_marker()
        else:
            self.category_idx = (self.category_idx + step) % len(EDIT_CATEGORIES)
        return ModalResult()

    def _edit_key(self, ch: int, rapid: bool) -> ModalResult:
        if ch == ESC:
            if self.from_menu and len(self._items()) > 1:
                self.edit_stage = "day_menu"
                self.edit_buffer = ""
                self.confirm = None
                return ModalResult()
            self.close()
            return ModalResult(closed=True, toast="Edit cancelled.")
        if ch in (CTRL_D, KEY_DC):
            if self.confirm == "delete":
                self.panel.delete_transaction(self.tx_id)
                self.close()
                day_idx, item_idx, gone = selection_after_delete(_day_list(self.panel), self.edit_date, self.menu_idx)
                toast = f"Deleted {self._label()}."
                if gone:
                    toast += f" No entries left for {_day_name(self.edit_date)}. Back to the days."
                return ModalResult(closed=True, changed=True, toast=toast, toast_kind="ok",
                                   select_item=item_idx, select_day=day_idx, day_gone=gone)
            self.confirm = "delete"
            return ModalResult()
        self.confirm = None
        if ch in ENTER_KEYS:
            if rapid:
                return ModalResult()  # newline inside a paste: never saves
            return self._save_edit()
        if ch == TAB:
            if rapid:
                self.edit_buffer += " "
            else:
                self._move_field(1)
            return ModalResult()
        if ch == KEY_BTAB:
            self._move_field(-1)
            return ModalResult()
        if ch in (KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT):
            step = 1 if ch in (KEY_DOWN, KEY_RIGHT) else -1
            # In the text field the arrows have no cursor to move, so they pick the category.
            target = "category" if self.edit_field == "text" else self.edit_field
            return self._change_choice(target, step)
        before = self._marker_account()
        if ch in BACKSPACE_KEYS:
            self.edit_buffer = self.edit_buffer[:-1]
        elif ch == CTRL_U:
            self.edit_buffer = ""
        else:
            text = self._decode(ch)
            if not text:
                return ModalResult()
            self.edit_buffer += text
        self._ledger_end = min(self._ledger_end, len(self.edit_buffer))
        self.edit_field = "text"
        self._sync_account(before)
        return ModalResult()

    def _save_edit(self) -> ModalResult:
        new_text = self.edit_buffer.strip()
        if not new_text:
            return ModalResult(toast="The text is empty. Type an entry, or press ⌃D to delete it.", toast_kind="warn")
        p = self.edit_preview()
        if not p.ok:
            return ModalResult(toast=f"Cannot save: {p.reason}", toast_kind="error")
        cat_label, cat_key = EDIT_CATEGORIES[self.category_idx]
        # The panel applies the same account rules as edit_preview (payment: Visa ignored; transfer: checking).
        acc = ACCOUNTS[self.account_idx]
        res = self.panel.edit_transaction(self.tx_id, self.edit_date, new_text, account_choice=acc, category_choice=cat_key)
        self.close()
        if res:
            # The edit logs a new entry (new id) that can sit elsewhere in the day: follow it.
            new_id = getattr(res, "id", None) or (res.get("id") if isinstance(res, dict) else None)
            select_day = select_item = None
            for di, d in enumerate(_day_list(self.panel)):
                if d.get("date") != self.edit_date:
                    continue
                select_day = di
                ids = [it.get("id") for it in d.get("items") or []]
                if new_id in ids:
                    select_item = ids.index(new_id)
                break
            return ModalResult(closed=True, changed=True, toast_kind="ok", select_item=select_item, select_day=select_day,
                               toast=f"Saved: {p.category} · {p.account} · {_money(p.amount_cents, self.mask, signed=True)}")
        return ModalResult(closed=True, changed=True, toast="Could not read that line. Example: groceries 25.50", toast_kind="error")

    # ------------------------------------------------------------------
    # Footer keys
    # ------------------------------------------------------------------

    def keys(self) -> List[KeyGroup]:
        esc = KeyHint("esc", "cancel", "cancel (asks first when lines are not saved)")
        if self.kind == "add":
            return [KeyGroup.of(
                "ENTRY",
                KeyHint("↵", "new line", "add the line (on an empty line: save all)"),
                KeyHint("⌃S", "save all", "save all lines"),
                KeyHint("←/→", "day", "previous / next day (also [ and ])"),
                KeyHint("tab", "account", "default account for lines without one: Checking / Visa"),
                KeyHint("⌫", "", "on an empty line: bring the previous line back", footer=False),
                KeyHint("⌃U", "", "clear the line", footer=False),
                esc,
            )]
        if self.kind == "edit" and self.edit_stage == "day_menu":
            return [KeyGroup.of(
                "ENTRY",
                KeyHint("↵", "edit", "edit the picked entry"),
                KeyHint("↑/↓", "pick", "pick an entry (also j/k, or its number)"),
                KeyHint("⌃D", "clear day", "clear the whole day (press again to confirm)"),
                KeyHint("esc", "cancel", "cancel"),
            )]
        if self.kind == "edit":
            back = "back" if self.from_menu and len(self._items()) > 1 else "cancel"
            return [KeyGroup.of(
                "ENTRY",
                KeyHint("↵", "save", "save"),
                KeyHint("tab", "next field", "next field (shift-tab: previous)"),
                KeyHint("↑/↓", "choose", "change the focused choice (in the text: category)"),
                KeyHint("⌃D", "delete", "delete this entry (press again to confirm)"),
                KeyHint("esc", back, back),
            )]
        return []

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def render(self, cols: int, rows: int) -> Optional[Overlay]:
        """Box lines and screen position for a terminal of `cols` x `rows`, or None when closed."""
        if not self.active:
            return None
        usable_w = max(4, cols - 1)  # the shell never writes the last column
        box_w = min(BOX_MAX_W, max(BOX_MIN_W, usable_w - 4), usable_w)
        body_top = 4 if rows >= 16 else 2
        max_h = max(5, rows - 2 - body_top)  # keep the toast and footer rows visible
        cw = max(1, box_w - 6)
        if self.kind == "add":
            body, title, border = self._add_body(cw, max_h - 2), "Add entries", "cyan"
        elif self.edit_stage == "day_menu":
            body = self._menu_body(cw, max_h - 2)
            title, border = f"Edit · {_day_name(self.edit_date)}", "yellow"
        else:
            body = self._edit_body(cw, max_h - 2)
            title, border = f"Edit entry · {_day_name(self.edit_date)}", "yellow"
        body = body[: max_h - 2]
        lines = [self._border_top(title, box_w, border)]
        for content, bg in body:
            inner = _on_bg([("  ", "text")] + fit(content, cw) + [("  ", "text")], bg)
            lines.append([("│", f"{border}@card")] + _on_bg(inner, "card") + [("│", f"{border}@card")])
        lines.append([(GLYPHS["bl"] + "─" * (box_w - 2) + GLYPHS["br"], f"{border}@card")])
        box_h = len(lines)
        room = rows - 2 - body_top
        # Anchor the top as if the box were its usual size, so it grows downward while lines go in.
        usual_h = min(room, 23)
        top = body_top + max(0, (room - usual_h) // 3)
        top = max(body_top, min(top, rows - 2 - box_h))
        left = max(0, (usable_w - box_w) // 2)
        return Overlay(lines=[fit(l, box_w) for l in lines], top=top, left=left, width=box_w)

    @staticmethod
    def _border_top(title: str, box_w: int, border: str) -> Line:
        title = clip(title, max(0, box_w - 6))
        head = GLYPHS["tl"] + "─ "
        dashes = max(0, box_w - cell_width(head) - cell_width(title) - 2)
        return [(head, f"{border}@card"), (title, f"{border}+b@card"), (" " + "─" * dashes + GLYPHS["tr"], f"{border}@card")]

    # Shared pieces -------------------------------------------------------

    @staticmethod
    def _preview_row(p: LinePreview, cw: int, current: bool, mark: bool = True, mask: bool = False) -> Line:
        """'  → Groceries · Checking · spend      -$25.50  ✓' or the red reason."""
        if not p.ok:
            style = "muted" if current else "red"
            glyph = "· " if current else GLYPHS["fail"] + " "
            reason = p.reason
            if cell_width(reason) > cw - 4 and ". " in reason:
                reason = reason.split(". ", 1)[0] + "."  # the first sentence when the box is narrow
            return fit([("  " + glyph + reason, style)], cw)
        amount_style = "green" if p.amount_cents > 0 else "text"
        right: Line = [(_money(p.amount_cents, mask, signed=True), amount_style)]
        if mark:
            right += [("  " + GLYPHS["done"], "green")]
        acc_style = "text" if p.account != "Checking" else "muted"
        variants = [
            [(p.category, "muted"), (" · ", "muted"), (p.account, acc_style), (" · " + p.kind, "muted")],
            [(p.category, "muted"), (" · ", "muted"), (p.account, acc_style)],
            [(p.category, "muted")],
        ]
        room = cw - line_width(right) - 2 - 4
        left = variants[-1]
        for v in variants:
            if line_width(v) <= room:
                left = v
                break
        return _right([("  → ", "muted")] + left, right, cw)

    @staticmethod
    def _segmented(names: Sequence[str], chosen: str, focused: bool) -> Line:
        out: Line = []
        for i, name in enumerate(names):
            if i:
                out.append(("   ", "text"))
            if name == chosen:
                out.append((f"[ {name} ]", "ink+b@cyan" if focused else "text+b@sel"))
            else:
                out.append((name, "muted"))
        return out

    # Add body ------------------------------------------------------------

    def _add_body(self, cw: int, max_rows: int) -> List[Tuple[Line, str]]:
        blank: Tuple[Line, str] = ([], "card")
        today = self._today()
        label, label_style = _date_label(self.date_buffer, today)
        day_left: Line = [("Day      ", "muted"), (GLYPHS["left"] + " ", "dim"), (label, label_style), (" " + GLYPHS["right"], "dim")]
        acct_left: Line = [("Account  ", "muted")] + self._segmented(ADD_ACCOUNTS, self.default_account, True)
        wide = cw >= 56
        head = [
            blank,
            (_right(day_left, [("←/→ change day", "muted")] if wide else [], cw), "card"),
            (_right(acct_left, [("tab to switch", "muted")] if wide else [], cw), "card"),
            blank,
        ]

        # Entry rows: each line, then its preview; the typed line last.
        entries: List[Tuple[Line, str]] = []
        for text in self.batch_lines:
            p = preview_line(text, self.default_account, panel=self.panel)
            entries.append((fit([(_clean(text), "text")], cw), "card"))
            entries.append((self._preview_row(p, cw, current=False, mask=self.mask), "card"))
        typed = self.line_buffer
        if typed:
            shown = typed if cell_width(typed) < cw - 2 else "…" + _tail(typed, max(1, cw - 4))
            entries.append(([(shown, "text+b"), (CURSOR, "cyan")], "card"))
            if typed.strip():
                entries.append((self._preview_row(preview_line(typed, self.default_account, panel=self.panel), cw, current=True, mask=self.mask), "card"))
            else:
                entries.append(([], "card"))
        else:
            entries.append(([(CURSOR, "cyan"), (" type an entry, like: lunch 13.15", "muted")], "card"))
            entries.append(([], "card"))

        # Summary of everything that will be saved.
        previews = self.previews()
        good = [p for p in previews if p.ok]
        bad = len(previews) - len(good)
        left: Line = [(f"{len(good)} ready", "green" if good else "muted")]
        if bad:
            left += [("  ·  ", "muted"), (f"{bad} {'needs' if bad == 1 else 'need'} a fix", "red")]
        summary = (_right(left, self._totals(good, cw - line_width(left) - 2, self.mask), cw), "card")

        # Bottom row: keys, or the armed confirm question.
        n_unsaved = len(self.all_lines())
        if self.confirm == "cancel":
            msg = f"{GLYPHS['over']} esc again to drop {n_unsaved} unsaved {'line' if n_unsaved == 1 else 'lines'}"
            bottom: Line = [(msg, "yellow")]
        elif self.confirm == "skip":
            noun = "line needs" if bad == 1 else "lines need"
            # ↵ saves only on an empty line; with text on the line it starts a new line instead.
            again = "⌃S again" if self.line_buffer.strip() else "↵ or ⌃S again"
            bottom = [(f"{GLYPHS['over']} {bad} {noun} a fix. {again} saves the other {len(good)}, keeps {'it' if bad == 1 else 'them'}", "yellow")]
        else:
            bottom = _keys_row([("↵", "new line", "cyan"), ("⌃S", "save all", "cyan", 1), ("←/→", "day", "cyan", 3),
                                ("tab", "account", "cyan", 2), ("esc", "cancel", "cyan")], cw)
        rule: Line = [("─" * cw, "dim")]
        tail = [blank, (rule, "card"), summary, blank, (bottom, "card"), blank]

        # Fit: keep the head, the tail and the newest entries. Small screens drop blank rows first.
        entry_room = max_rows - len(head) - len(tail)
        if entry_room < 4:
            head = head[1:3]
            tail = [(rule, "card"), summary, (bottom, "card")]
            entry_room = max_rows - len(head) - len(tail)
        entry_room = max(2, entry_room)
        min_area = min(entry_room, 6)  # stable height while the first lines go in
        if len(entries) > entry_room:
            hidden_lines = (len(entries) - entry_room + 1 + 1) // 2
            keep = entries[-(entry_room - 1):] if entry_room > 1 else entries[-1:]
            if len(keep) % 2:  # never start on a preview row
                keep = keep[1:]
            more = len(self.batch_lines) - (len(keep) - 2) // 2
            hidden_lines = max(hidden_lines, more)
            entries = [([(f"{GLYPHS['over']} {hidden_lines} earlier {'line' if hidden_lines == 1 else 'lines'}", "muted")], "card")] + keep
        while len(entries) < min_area:
            entries.append(blank)
        return head + entries + tail

    @staticmethod
    def _totals(good: Sequence[LinePreview], room: int, mask: bool = False) -> Line:
        """'-$25.50 spent  ·  $100.00 to Visa  ·  +$1,480.00 in': one number per kind, because a card
        purchase, a card payment and a savings move do not add up to one account. Parts that do not
        fit in `room` are dropped from the end."""
        def total(kind: str, to_checking: bool = False) -> int:
            return sum(abs(p.amount_cents) for p in good if p.kind == kind and p.to_checking == to_checking)

        parts: List[Line] = []
        if total("spend"):
            parts.append([(_money(-total("spend"), mask, signed=True), "text"), (" spent", "muted")])
        if total("income"):
            parts.append([(_money(total("income"), mask, signed=True), "green"), (" in", "muted")])
        if total("payment"):
            parts.append([(_money(total("payment"), mask), "text"), (" to Visa", "muted")])
        if total("transfer"):
            parts.append([(_money(total("transfer"), mask), "text"), (" to savings", "muted")])
        if total("transfer", to_checking=True):
            parts.append([(_money(total("transfer", to_checking=True), mask), "text"), (" to checking", "muted")])
        while parts:
            out: Line = []
            for i, part in enumerate(parts):
                out += ([("  ·  ", "muted")] if i else []) + part
            if line_width(out) <= room:
                return out
            parts.pop()
        return []

    # Edit body -----------------------------------------------------------

    def _label_row(self, name: str, field_name: str, hint: str, cw: int) -> Tuple[Line, str]:
        focused = self.edit_field == field_name
        left: Line = [(name, "cyan+b" if focused else "muted+b")]
        return (_right(left, [(hint, "muted")] if cw >= 40 else [], cw), "card")

    def _edit_body(self, cw: int, max_rows: int) -> List[Tuple[Line, str]]:
        blank: Tuple[Line, str] = ([], "card")
        p = self.edit_preview()
        allowed = self.allowed_accounts()
        locked = len(allowed) < 2
        kind_hint = {"income": "lands in", "payment": "paid from"}.get(p.kind if p.ok else "", "")
        if locked:
            account_hint = "always checking"
        elif self.edit_field == "account":
            account_hint = "←/→ to switch"
        else:
            account_hint = "tab here, then ←/→"
        if kind_hint and cw >= 40:
            account_hint = f"{kind_hint}  ·  {account_hint}"
        acc_label = self._label_row("ACCOUNT", "account", account_hint, cw)
        shown = p.account if p.ok and p.account in ACCOUNTS else ACCOUNTS[self.account_idx]
        if shown != ACCOUNTS[self.account_idx] and cw >= 40:
            # The text still decides (a card word the box could not drop): say so.
            acc_label = (_right([acc_label[0][0]], [(f"the text says {shown}", "yellow")], cw), "card")
        seg: Line = []
        for i, name in enumerate(ACCOUNTS):
            if i:
                seg.append(("   ", "text"))
            if name == shown:
                style = "muted+b" if locked else ("ink+b@cyan" if self.edit_field == "account" else "text+b@sel")
                seg.append((f"[ {name} ]", style))
            else:
                seg.append((name, "muted" if name in allowed else "dim"))
        rows_acc = [acc_label, (fit(seg, cw), "card")]

        # Category list: Auto first, a window that keeps the choice in view.
        auto_p = preview_line(self.edit_buffer, panel=self.panel)
        auto_label = "Auto, from the text" + (f": {auto_p.category}" if auto_p.ok else "")
        cur = (self.current_category or "").strip().lower()
        cat_rows: List[Tuple[Line, str]] = []
        for i, (name, key) in enumerate(EDIT_CATEGORIES):
            label = auto_label if i == 0 else name
            chosen = i == self.category_idx
            tag: Line = [("current", "muted")] if i and name.lower() == cur else []
            if chosen:
                marker = (GLYPHS["sel"] + " ", "cyan")
                body = _right([marker, (label, "text+b")], tag, cw)
                cat_rows.append((body, "sel"))
            else:
                cat_rows.append((_right([("  " + label, "muted")], tag, cw), "card"))

        typed = self.edit_buffer
        text_focus = self.edit_field == "text"
        drawn = self.edit_text_shown()
        shown = drawn if cell_width(drawn) < cw - 3 else "…" + _tail(drawn, max(1, cw - 5))
        text_row: Line = [("› ", "cyan" if text_focus else "dim"), (shown, "text+b")] + ([(CURSOR, "cyan")] if text_focus else [])
        if typed.strip():
            prev_row = self._preview_row(p, cw, current=False, mask=self.mask)
        else:
            prev_row = fit([("  · type an entry, like: lunch 13.15", "muted")], cw)

        if self.confirm == "delete":
            del_row: Line = [(f"{GLYPHS['over']} ⌃D again deletes {self._label() if self.item_label else 'this entry'}", "yellow")]
        else:
            del_row = [("⌃D", "red"), (" delete this entry", "red")]
        back = "back" if self.from_menu and len(self._items()) > 1 else "cancel"
        keys = _keys_row([("↵", "save", "cyan"), ("tab", "next field", "cyan", 1), ("↑/↓", "choose", "cyan", 2), ("esc", back, "cyan")], cw)

        # Row budget: fixed rows plus a category window of 3 to 6 rows.
        fixed = 16
        list_h = max(3, min(6, max_rows - fixed))
        compact = max_rows - fixed < 3
        if compact:
            list_h = max(2, min(5, max_rows - 11))
        start = max(0, min(self.category_idx - list_h // 2, len(cat_rows) - list_h))
        window = cat_rows[start: start + list_h]
        cat_hint = "↑/↓ to pick"
        if start > 0 or start + list_h < len(cat_rows):
            cat_hint = f"{self.category_idx + 1}/{len(cat_rows)}  ↑/↓ to pick"

        if compact:
            return ([rows_acc[1]]
                    + [self._label_row("CATEGORY", "category", cat_hint, cw)] + window
                    + [(text_row, "card"), (prev_row, "card"), (keys, "card"), (del_row, "card")])
        return ([blank] + rows_acc + [blank]
                + [self._label_row("CATEGORY", "category", cat_hint, cw)] + window + [blank]
                + [self._label_row("TEXT", "text", "", cw), (text_row, "card"), (prev_row, "card"), blank]
                + [(keys, "card"), ([("─" * cw, "dim")], "card"), (del_row, "card"), blank])

    # Day menu body ---------------------------------------------------------

    def _menu_body(self, cw: int, max_rows: int) -> List[Tuple[Line, str]]:
        blank: Tuple[Line, str] = ([], "card")
        items = self._items()
        n = len(items)
        head = [blank, (_right([("Which entry?", "muted+b")], [(f"{n} entries", "muted")], cw), "card"), blank]
        rows: List[Tuple[Line, str]] = []
        num_w = len(str(n))
        names = [_clean(it.get("payee") or it.get("category") or "entry") for it in items]
        name_w = min(max((cell_width(x) for x in names), default=0), max(8, cw // 3))
        for i, it in enumerate(items):
            cents = int(it.get("amount_cents") or 0)
            kind = str(it.get("kind") or ("income" if it.get("is_income") else "spend"))
            signed = cents if kind == "income" else -abs(cents)
            amount: Line = [(_money(signed, self.mask, signed=True), "green" if signed > 0 else "text")]
            meta = f"{_clean(it.get('category') or '')} · {it.get('account_short') or it.get('account') or ''}".strip(" ·")
            chosen = i == self.menu_idx
            show_meta = cw >= 50 and bool(meta)
            name = names[i]
            if show_meta:
                name = name if cell_width(name) <= name_w else clip(name, name_w - 1) + "…"
                name += " " * (name_w - cell_width(name))
            left: Line = [
                (GLYPHS["sel"] + " " if chosen else "  ", "cyan"),
                (f"{i + 1:>{num_w}}  ", "muted"),
                (name, "text+b" if chosen else "text"),
            ]
            if show_meta:
                left.append(("   " + meta, "muted"))
            rows.append((_right(left, amount, cw), "sel" if chosen else "card"))
        if self.confirm == "clear_day":
            bottom: Line = [(f"{GLYPHS['over']} ⌃D again clears all {n} entries for this day", "yellow")]
        else:
            bottom = _keys_row([("↵", "edit", "cyan"), ("↑/↓", "pick", "cyan", 1), ("⌃D", "clear day", "red", 2), ("esc", "cancel", "cyan")], cw)
        tail = [blank, (bottom, "card"), blank]
        room = max(1, max_rows - len(head) - len(tail))
        if len(rows) > room:
            start = max(0, min(self.menu_idx - room // 2, len(rows) - room))
            rows = rows[start: start + room]
        return head + rows + tail


__all__ = [
    "ACCOUNTS",
    "ADD_ACCOUNTS",
    "FinanceModals",
    "LinePreview",
    "ModalResult",
    "mask_numbers",
    "preview_line",
]
