"""Money tab: overview screen, shared helpers, the key map and key hints.

Everything here is pure: finance data in, styled lines out; nothing touches curses. The Day, Math
and Bills screens live in finance_screens.py and use the helpers below.

Every builder takes the full body width (FULL_WIDTH = True) and draws its own 2-cell gutter, where
the cyan ▸ of a selected row sits. Widths: narrow (under 80) is one compact column, normal (80-119)
is the mockup layout, wide (120 and up) uses two columns. Heights: sections shrink in a fixed order
until the screen fits, and the recent-days list fills whatever rows are left.

Masked mode ("m") swaps every amount for ••• and keeps the layout as it is.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from telemachus.cockpit.finance_panel import RunwayInfo
from telemachus.cockpit.shell import KeyGroup, KeyHint
from telemachus.cockpit.theme import (
    GLYPHS,
    GUTTER,
    Line,
    Seg,
    cell_width,
    fit,
    line_width,
    lpad,
    rpad,
    section,
    spread,
    with_bg,
)

# Widths are body widths (terminal columns - 1): a 79-column pane is narrow, a 120-column pane is wide.
NARROW_MAX = 78  # up to here: one compact column
WIDE_MIN = 119  # from here: two columns
HIDDEN = GLYPHS["hidden"]  # masked amount
BAR = GLYPHS["bar"]
MINUS = "-"

# View-only state that lives across renders (the TUI owns no state for it): the h toggle on Bills
# and the Math scroll position (rows scrolled down; build_math keeps it in range).
VIEW_PREFS: Dict[str, Any] = {"show_hidden_bills": False, "math_scroll": 0, "math_scrollable": False}


def scroll_math(delta: int) -> None:
    """j/k on Math: scrolls by `delta` rows. build_math clamps it to what the pane needs."""
    VIEW_PREFS["math_scroll"] = max(0, int(VIEW_PREFS.get("math_scroll", 0) or 0) + delta)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def money(cents: int, mask: bool = False, signed: bool = False, whole: bool = False) -> str:
    """'$1,234.56'. `signed` adds +/-; `whole` drops the cents; `mask` shows '$•••'."""
    sign = MINUS if cents < 0 else ("+" if signed and cents > 0 else "")
    if mask:
        return (sign if signed else "") + "$" + HIDDEN
    value = abs(cents) / 100.0
    body = f"{round(value):,}" if whole else f"{value:,.2f}"
    return f"{sign}${body}"


def num(cents: int, mask: bool = False, signed: bool = False, whole: bool = False) -> str:
    """Table amount without the dollar sign: '4,285.15', '-129.41', masked '•••'."""
    if mask:
        sign = (MINUS if cents < 0 else "+" if cents > 0 else "") if signed else ""
        return sign + HIDDEN
    return money(cents, False, signed, whole).replace("$", "", 1)


plain = num  # older name


def _date(value: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def fmt_day(value: str, comma: bool = False) -> str:
    """'Fri Oct 30' (or 'Fri, Oct 30'). Falls back to the raw text."""
    dt = _date(value)
    if dt is None:
        return str(value)
    sep = ", " if comma else " "
    return f"{dt:%a}{sep}{dt:%b} {dt.day}"


def clean_name(name: Any) -> str:
    """Drops emoji and other non-ASCII glyphs (they render at inconsistent widths)."""
    return " ".join("".join(ch for ch in str(name or "") if ord(ch) < 128).split())


def short_bill_name(name: str, lower: bool = True) -> str:
    """'PG&E Electric & Gas' -> 'PG&E', 'Gym Membership' -> 'gym' (or 'Gym' with lower=False)."""
    full = clean_name(name)
    if "credit card" in full.lower() or "visa" in full.lower():
        return "visa" if lower else "Visa"
    word = (full.split() or [""])[0]
    if sum(c.isupper() for c in word) >= 2 or "&" in word:
        return word
    return word.lower() if lower else word


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _today(d: "FinanceViewData") -> datetime:
    if d.runway is not None:
        dt = _date(d.runway.today)
        if dt:
            return dt
    now = datetime.now()
    return datetime(now.year, now.month, now.day)


def card_overdue(card: Optional[Dict[str, Any]], today: datetime) -> bool:
    """True when money is owed on the card and its due date passed with no payment logged.
    cards.build_card_summary then sets next_due_date to that past date (before today)."""
    if not card or card.get("status") != "owed" or int(card.get("owed_cents", 0) or 0) <= 0:
        return False
    when = str(card.get("next_due_date") or "")
    return bool(when) and when < today.strftime("%Y-%m-%d")


def pack(items: Sequence[Tuple[Line, Line]], width: int, indent: str = GUTTER, cont: Optional[str] = None,
         widths: Optional[Sequence[int]] = None) -> List[Line]:
    """Packs (chunk, separator) items into lines no wider than `width`.

    The separator goes before its chunk when the chunk joins a line that already has text.
    Continuation lines start with `cont` (defaults to `indent`). A chunk wider than a line is cut.
    `widths` (one per item) overrides the measured chunk widths when choosing line breaks.
    """
    cont = indent if cont is None else cont
    lines: List[Line] = []
    cur: Line = [(indent, "text")]
    used = cell_width(indent)
    has = False
    for i, (chunk, sep) in enumerate(items):
        if not chunk:
            continue
        cw = max(line_width(chunk), widths[i]) if widths is not None and i < len(widths) else line_width(chunk)
        need = cw + (line_width(sep) if has else 0)
        if has and used + need > width:
            lines.append(cur)
            cur, has, used = [(cont, "text")], False, cell_width(cont)
            need = cw
        if has:
            cur += list(sep)
        cur += list(chunk)
        used += need
        has = True
    if has:
        lines.append(cur)
    return [fit(line, width) for line in lines]


def pack_steady(make: Callable[[bool], List[Tuple[Line, Line]]], mask: bool, width: int,
                indent: str = GUTTER, cont: Optional[str] = None) -> List[Line]:
    """pack() whose line breaks do not depend on masking.

    `make(mask)` returns the items; each chunk is measured as the wider of its masked and plain
    form, so pressing m never moves a line break (Masked.dc.html: same layout, amounts hidden).
    """
    shown, other = make(mask), make(not mask)
    widths = [max(line_width(a[0]), line_width(b[0])) for a, b in zip(shown, other)]
    return pack(shown, width, indent, cont, widths)


def fits_one_line(make: Callable[[bool], List[Tuple[Line, Line]]], width: int, indent: str = GUTTER) -> bool:
    return len(pack_steady(make, False, width, indent)) <= 1


def marked(content: Line, selected: bool, width: int) -> Line:
    """Gutter plus content: a selected row gets the cyan ▸ and the selected-row background."""
    gutter: Seg = (GLYPHS["sel"] + " ", "cyan+b") if selected else (GUTTER, "text")
    line = fit([gutter] + list(content), width)
    return with_bg(line, "sel") if selected else line


def blank() -> Line:
    return []


def hcat(left: List[Line], lw: int, right: List[Line], rw: int, gap: int = 2) -> List[Line]:
    """Two columns side by side; the shorter one is padded with blank rows."""
    rows: List[Line] = []
    for i in range(max(len(left), len(right))):
        row = fit(left[i] if i < len(left) else [], lw) + [(" " * gap, "text")]
        row += fit(right[i] if i < len(right) else [], rw)
        rows.append(row)
    return rows


def crumb(title: str, width: int, mask: bool = False, note: str = "") -> Line:
    """'  Money ▸ Bills' with 'esc back' (and the hidden-amounts badge) on the right.

    `note` (Math's '12 below  j/k scroll') goes before them while it fits; it drops first."""
    left: Line = [(GUTTER + "Money ", "muted"), (GLYPHS["sel"] + " ", "muted"), (title, "text+b")]
    right: Line = []
    if mask:
        right += [(GLYPHS["ok"] + " amounts hidden", "yellow"), ("   ", "text")]
    right += [("esc", "cyan"), (" back", "muted")]
    if note and line_width(left) + 3 + cell_width(note) + 3 + line_width(right) <= width:
        right = [(note, "yellow"), ("   ", "text")] + right
    return spread(left, right, width)


def mask_badge(mask: bool) -> Tuple[str, str]:
    return (GLYPHS["ok"] + " amounts hidden", "yellow") if mask else ("", "muted")


# ---------------------------------------------------------------------------
# Day items: what counts as spending
# ---------------------------------------------------------------------------


def item_kind(it: Dict[str, Any]) -> str:
    """'spend' | 'income' | 'card_payment' | 'transfer' (falls back to the older item flags)."""
    kind = it.get("kind")
    if kind:
        return str(kind)
    if it.get("is_income"):
        return "income"
    if it.get("is_savings"):
        return "transfer"
    if str(it.get("category", "")).lower() == "credit card":
        return "card_payment"
    return "spend"


def item_account(it: Dict[str, Any]) -> str:
    return str(it.get("account_short") or it.get("account") or "")


def is_card(it: Dict[str, Any]) -> bool:
    return item_account(it).lower() in ("visa", "card", "credit card")


def day_spent(day: Dict[str, Any]) -> int:
    """Money spent that day. Card payments, savings transfers and income do not count."""
    items = day.get("items") or []
    if not items:
        return int(day.get("outflow_cents", 0) or 0)
    return sum(int(it.get("amount_cents", 0) or 0) for it in items if item_kind(it) == "spend")


def day_totals(day: Dict[str, Any]) -> Dict[str, int]:
    out = {"spent": day_spent(day), "income": 0, "card_payment": 0, "transfer": 0, "card_spend": 0}
    for it in day.get("items") or []:
        kind = item_kind(it)
        amt = int(it.get("amount_cents", 0) or 0)
        if kind in ("income", "card_payment", "transfer"):
            out[kind] += amt
        elif is_card(it):
            out["card_spend"] += amt
    if not day.get("items"):
        out["income"] = int(day.get("inflow_cents", 0) or 0)
        out["transfer"] = int(day.get("savings_cents", 0) or 0)
    return out


def day_notes(day: Dict[str, Any], mask: bool) -> List[Tuple[Line, Line]]:
    """Short notes after a day's amount: payday, visa tag, card payment, savings."""
    t = day_totals(day)
    notes: List[Tuple[Line, Line]] = []
    sep: Line = [("  ", "text")]
    if t["income"] > 0:
        notes.append(([(f"{num(t['income'], mask, signed=True, whole=True)} pay", "green")], sep))
    if t["card_spend"] > 0:
        notes.append(([("visa", "muted")], sep))
    if t["card_payment"] > 0:
        notes.append(([(f"{GLYPHS['swap']} Visa payment {num(t['card_payment'], mask, whole=True)}", "muted")], sep))
    if t["transfer"] > 0:
        notes.append(([(f"{GLYPHS['swap']} {num(t['transfer'], mask, whole=True)} to savings", "muted")], sep))
    return notes


PACE_DAYS = 7  # the 'lately' window (finance_panel.PACE_WINDOW_DAYS)


def day_counted(day: Dict[str, Any]) -> int:
    """The part of a day's spending that counts against the allowance (groceries, dining, gas)."""
    return int(day.get("allowance_spent_cents", day_spent(day)) or 0)


def allowance_pace(d: "FinanceViewData") -> int:
    """Groceries, dining and gas a day over the last 7 days (what the bar colors compare)."""
    today = _today(d)
    start = today - timedelta(days=PACE_DAYS - 1)
    total = 0
    for x in d.days or []:
        dt = _date(x.get("date"))
        if dt is not None and start <= dt <= today:
            total += day_counted(x)
    return int(round(total / PACE_DAYS))


def daily_allowance(d: "FinanceViewData") -> int:
    """Grocery, dining and gas allowance per day of the pay period (bar colors use it)."""
    rw = d.runway
    if rw is None:
        return 0
    fixed = int(getattr(rw, "daily_allowance_cents", 0) or 0)
    if fixed > 0:
        return fixed
    days = getattr(rw, "period_days", 0) or 14  # whole pay period, not the days left
    if rw.allowance_cents > 0:
        return int(round(rw.allowance_cents / max(1, days)))
    return max(0, rw.per_day_cents)


# ---------------------------------------------------------------------------
# Data the screens need
# ---------------------------------------------------------------------------


@dataclass
class FinanceViewData:
    runway: Optional[RunwayInfo]
    checking_cents: int
    savings_cents: int
    visa_cents: int
    net_worth_cents: int
    goals: List[Any]
    days: List[Dict[str, Any]]
    overview: Any
    paycheck: Any
    mask: bool = False
    selected_day_idx: int = 0
    selected_item_idx: int = 0
    day_scroll_offset: int = 0
    # Bills screen
    bills_breakdown: Dict[str, Any] = field(default_factory=dict)  # FinanceTuiPanel.get_monthly_bills_breakdown()
    selected_bill_idx: int = 0
    bills_scroll_offset: int = 0
    # Card summary (FinanceTuiPanel.get_card_summary()); None until the panel provides one
    card: Optional[Dict[str, Any]] = None
    # None = use VIEW_PREFS["show_hidden_bills"] (the h key on the Bills screen)
    show_hidden_bills: Optional[bool] = None
    # False for a brand-new vault (no setup.yaml, no entries): every number would be a default,
    # so the screens show the setup hint instead.
    has_data: bool = True
    # FinanceTuiPanel.get_setup_error(): why setup.yaml cannot be read. While set, runway is None.
    setup_error: Optional[str] = None


@dataclass
class Rendered:
    """Lines to draw plus which line index maps to which selectable row."""

    lines: List[Line]
    day_rows: Dict[int, int] = field(default_factory=dict)  # line index -> day index
    item_rows: Dict[int, int] = field(default_factory=dict)  # line index -> item index
    day_scroll_offset: int = 0
    bill_rows: Dict[int, int] = field(default_factory=dict)  # line index -> bill index
    bills_scroll_offset: int = 0


def _finish(lines: List[Line], width: int, rows: int) -> List[Line]:
    """Every line exactly `width` cells, never more than `rows` lines."""
    return [fit(line, width) for line in lines[: max(0, rows)]]


# ---------------------------------------------------------------------------
# Overview: UNTIL PAYDAY (hero + runway strip)
# ---------------------------------------------------------------------------


def _status(d: FinanceViewData) -> Line:
    rw = d.runway
    if rw is None:
        return []
    if rw.left_cents < 0:
        return [(GLYPHS["over"] + " short", "red+b")]
    if rw.run_out_date:
        return [(f"{GLYPHS['over']} over, $0 by {fmt_day(rw.run_out_date)}", "yellow")]
    if rw.per_day_cents > 0 and rw.pace_cents > rw.per_day_cents:
        return [(GLYPHS["over"] + " over", "yellow")]
    # Cash is fine, but the day bars compare groceries, dining and gas with the allowance: a green
    # 'on track' over a column of yellow bars would contradict them.
    if over_allowance(d):
        # Name what is over, so it does not read as being about the cash-a-day number beside it.
        return [(f"{GLYPHS['over']} food & gas over {money(daily_allowance(d), d.mask)} a day", "yellow")]
    return [(GLYPHS["ok"] + " on track", "green")]


def over_allowance(d: FinanceViewData) -> bool:
    allowance = daily_allowance(d)
    return allowance > 0 and allowance_pace(d) > allowance


def setup_error_lines(err: str, width: int, indent: str = GUTTER) -> List[Line]:
    """'▲ setup.yaml line 4: ... · fix setup.yaml to see the money until payday', in yellow,
    wrapped on words. Shown where the until-payday numbers go while setup.yaml cannot be read."""
    text = f"{GLYPHS['over']} {err} {GLYPHS['dot']} fix setup.yaml to see the money until payday"
    items = [([(w, "yellow")], [(" ", "yellow")]) for w in text.split()]
    return pack(items, width, indent, cont=indent + "  ")


def hero_lines(d: FinanceViewData, width: int) -> List[Line]:
    """The plain big number and the per-day line under it.

    When the per-day line does not fit, a shorter form ('$466.68/day after bills') is tried before
    the line wraps. Line breaks are the same with and without masking.
    """
    rw = d.runway
    if rw is None and d.setup_error:
        return setup_error_lines(d.setup_error, width)
    if rw is None:
        return pack([([("No paycheck plan yet.", "muted")], []),
                     ([("Add paycheck_plan to setup.yaml to see the days until payday.", "muted")], [(" ", "text")])], width)
    payday = fmt_day(rw.next_paycheck)
    gap: Line = [("   ", "text")]
    dot: Line = [("  " + GLYPHS["dot"] + "  ", "dim")]
    short = rw.left_cents < 0

    def first(m: bool) -> List[Tuple[Line, Line]]:
        num_style = "muted+b" if m else ("red+b" if short else "text+b")
        if short:
            return [
                ([(money(abs(rw.left_cents), m), num_style), (" short", "red+b")], []),
                ([("bills before payday are more than checking", "muted")], gap),
                ([(f"payday {payday}", "muted")], [(" " + GLYPHS["dot"] + " ", "muted")]),
            ]
        return [
            ([(money(rw.left_cents, m), num_style)], []),
            ([(f"left for {_plural(rw.days_left, 'day')}", "muted")], gap),
            ([(f"payday {payday}", "muted")], [(" " + GLYPHS["dot"] + " ", "muted")]),
        ]

    status = _status(d) if not short else []  # "short" is already in the first line

    def second(m: bool, compact: bool = False) -> List[Tuple[Line, Line]]:
        amount_style = "muted" if m else "text"
        per = "/day" if compact else " a day"
        sep = [(" " + GLYPHS["dot"] + " ", "dim")] if compact else dot
        out: List[Tuple[Line, Line]] = []
        if not short:
            out.append(([(money(rw.per_day_cents, m), amount_style), (per + " after bills", "muted")], []))
        if rw.pace_cents > 0:
            out.append(([("lately ", "muted"), (money(rw.pace_cents, m), amount_style), (per, "muted")], sep))
        if status:
            out.append((status, gap))
        return out

    make2: Callable[[bool], List[Tuple[Line, Line]]] = second
    if not fits_one_line(second, width) and fits_one_line(lambda m: second(m, True), width):
        make2 = lambda m: second(m, True)  # noqa: E731
    return pack_steady(first, d.mask, width) + pack_steady(make2, d.mask, width)


def _place(buf: List[str], styles: List[str], pos: int, text: str, style: str, pad_after: int = 1) -> bool:
    """Writes text into a char buffer when the cells (plus a gap after) are free."""
    end = pos + len(text)
    if pos < 0 or end > len(buf):
        return False
    if any(c != " " for c in buf[max(0, pos - 1): min(len(buf), end + pad_after)]):
        return False
    buf[pos:end] = list(text)
    styles[pos:end] = [style] * len(text)
    return True


def _runs(buf: List[str], styles: List[str]) -> Line:
    out: Line = []
    for ch, st in zip(buf, styles):
        if out and out[-1][1] == st:
            out[-1] = (out[-1][0] + ch, st)
        else:
            out.append((ch, st))
    return out


STRIP_LEGEND = "   today " + "→" + " payday"


def runway_strip(d: FinanceViewData, width: int, nrows: int = 4) -> List[Line]:
    """Compact strip from today to payday.

    Rows (nrows=4): months, the track (● today, ▼ bill due, ✓ paid, ◆ payday), day numbers, and the
    amounts (each bill as amount plus short name, '-25 Gym'; the paycheck at the end). When close
    bills do not all fit on one amounts row, a second amounts row follows (nrows=5) or takes the
    place of the months row (nrows=4). nrows=3 is the track, day numbers and one amounts row; nrows=2 is the track and one amounts row
    (the amounts matter more than the day numbers). Always fits: the day spacing is 4 cells (wider
    when that is what it takes to label every bill in full), down to 1 on a narrow pane.

    A bill amount never shows without its name: next to another label, a bare '-25' would read as
    part of it ('-25 -432 Visa'). The biggest bills are labelled first; a bill with no room left
    keeps its ▼ and is listed under BILLS BEFORE PAYDAY.
    """
    rw = d.runway
    if rw is None or nrows < 2:
        return []
    start, end = _date(rw.today), _date(rw.next_paycheck)
    if start is None or end is None or end <= start:
        return []
    slots = (end - start).days + 1
    avail = max(1, width - len(GUTTER))
    m = d.mask

    pc = d.paycheck
    due: Dict[str, int] = {}
    names: Dict[str, List[str]] = {}
    for day, cents in rw.bill_marks:
        due[day] = due.get(day, 0) + cents
    paid: Dict[str, int] = {}
    for b in list(getattr(pc, "bills", []) or []):
        when = b.get("due_date", "")
        if b.get("amount_cents", 0) <= 0:
            continue
        if b.get("status") == "due":
            names.setdefault(when, []).append(short_bill_name(b.get("name", ""), lower=False))
        elif when >= rw.today:
            paid[when] = paid.get(when, 0) + b["amount_cents"]

    def amounts(step: int, cells: int, m: bool, nrows_: int = 1) -> Tuple[List[Tuple[List[str], List[str]]], bool]:
        """Up to `nrows_` amounts rows for one day spacing, and whether every bill got a label."""
        rows_ = [([" "] * cells, ["yellow"] * cells) for _ in range(max(1, nrows_))]
        full = True
        # Paycheck amount first, flush with the ◆, so bill labels never crowd it out.
        if pc is not None and getattr(pc, "paycheck_amount_cents", 0):
            text = num(pc.paycheck_amount_cents, m, signed=True, whole=True)
            _place(rows_[0][0], rows_[0][1], max(0, cells - len(text)), text, "green", pad_after=0)
        days = [x for x in set(due) | set(paid) if _date(x) is not None]
        # Biggest first (the amount, not the masked text, decides).
        days.sort(key=lambda x: (-(due.get(x, 0) or paid.get(x, 0)), x))
        for day in days:
            slot = (_date(day) - start).days
            if not 0 <= slot < slots - 1:
                continue
            if day in due:
                amount = num(-due[day], m, signed=True, whole=True)
                label = names.get(day, [])
                text = f"{amount} {label[0]}" + ("+" if len(label) > 1 else "") if label else amount
                style = "yellow"
            else:
                text, style = GLYPHS["done"] + num(paid[day], m, whole=True), "green"
            # Two blank cells after a label, so the next label never reads as part of it.
            if not any(_place(buf, st, slot * step, text, style, pad_after=2) for buf, st in rows_):
                full = False
        return rows_, full

    def width_for(step: int) -> int:
        return (slots - 1) * step + 1

    step = next((s for s in (4, 3, 2) if width_for(s) <= avail), 1)
    if step == 4 and not amounts(4, width_for(4), False)[1]:
        # Wider spacing only when it is what it takes to label every bill on one row. The plain
        # amounts decide, so pressing m never changes the spacing.
        for wider in (5, 6, 7, 8):
            if width_for(wider) + len(STRIP_LEGEND) > avail:
                break
            if amounts(wider, width_for(wider), False)[1]:
                step = wider
                break
    cells = min(avail, width_for(step))
    # A second amounts row (instead of the months row) when one row cannot label every bill. The
    # plain amounts decide here too, so m never changes which rows show.
    two_rows = nrows >= 4 and not amounts(step, cells, False)[1]
    label_rows, _ = amounts(step, cells, m, 2 if two_rows else 1)

    track: Line = []
    for i in range(slots):
        day = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        if i == 0:
            track.append((GLYPHS["ok"], "cyan+b"))
        elif i == slots - 1:
            track.append((GLYPHS["payday"], "green+b"))
        elif day in due:
            track.append((GLYPHS["bill"], "yellow+b"))
        elif day in paid:
            track.append((GLYPHS["done"], "green"))
        else:
            track.append(("┼", "dim"))
        if i < slots - 1 and step > 1:
            track.append(("─" * (step - 1), "dim"))
    track = fit(track, cells)
    if cells + len(STRIP_LEGEND) <= avail:
        track += [(STRIP_LEGEND, "muted")]

    months, mst = [" "] * cells, ["muted"] * cells
    labels, lst = [" "] * cells, ["muted"] * cells
    for i in range(slots):
        dt = start + timedelta(days=i)
        if i == 0 or dt.day == 1:
            _place(months, mst, i * step, f"{dt:%b}", "muted")
        _place(labels, lst, i * step, str(dt.day), "muted", pad_after=1)

    rows: List[Line] = []
    if nrows >= 5 or (nrows >= 4 and not two_rows):
        rows.append([(GUTTER, "text")] + _runs(months, mst))
    rows.append([(GUTTER, "text")] + track)
    if nrows >= 3:
        rows.append([(GUTTER, "text")] + _runs(labels, lst))
    for buf, st in label_rows:
        rows.append([(GUTTER, "text")] + _runs(buf, st))
    return [fit(r, width) for r in rows]


# ---------------------------------------------------------------------------
# Overview: ACCOUNTS (+ the card line) and GOALS
# ---------------------------------------------------------------------------

LABEL_W = 10  # 'Checking  '


def account_lines(d: FinanceViewData, width: int) -> List[Line]:
    """'Checking  $4,285.15    Savings  $4,200.00    Net  $8,485.15' (tighter when narrow)."""

    def items(m: bool, compact: bool = False) -> List[Tuple[Line, Line]]:
        val = "muted" if m else "text"
        gap: Line = [("  " if compact else "   ", "text")]
        lab = " " if compact else "  "

        def amount(cents: int, pad: bool) -> str:
            # Plain amounts keep a fixed width so hiding them (m) does not shift the line.
            return rpad(money(cents, m), 10) if pad and not compact else money(cents, m)

        return [
            ([("Checking" + lab, "muted"), (amount(d.checking_cents, True), val)], []),
            ([("Savings" + lab, "muted"), (amount(d.savings_cents, True), val)], gap),
            ([("Net" + lab, "muted"), (amount(d.net_worth_cents, False), val)], gap),
        ]

    if not fits_one_line(items, width) and fits_one_line(lambda m: items(m, True), width):
        return pack_steady(lambda m: items(m, True), d.mask, width)
    return pack_steady(items, d.mask, width)


def card_lines(d: FinanceViewData, width: int, label: str = "Visa", detail: bool = False,
               one_line: bool = False) -> List[Line]:
    """'Visa      owed $123.45 · due Mon Nov 2 · $200.00 paid this month' (yellow ▲ before payday).

    `one_line` drops the extra notes (paid this month, charged since) when they would wrap.
    """
    card = d.card
    head = GUTTER + rpad(label, LABEL_W)
    cont = " " * len(head)

    def items(m: bool) -> List[Tuple[Line, Line]]:
        dot: Line = [(" " + GLYPHS["dot"] + " ", "muted")]
        out: List[Tuple[Line, Line]] = []
        if not card:
            if d.visa_cents:
                out.append(([(money(d.visa_cents, m), "muted" if m else "text"), (" balance", "muted")], []))
            else:
                out.append(([(GLYPHS["done"] + " paid off", "green")], []))
            return out
        status = str(card.get("status") or "clear")
        owed = int(card.get("owed_cents", 0) or 0)
        soon = bool(card.get("due_before_payday"))
        if status == "credit" or owed < 0:
            out.append(([("credit ", "muted"), (money(abs(owed), m), "muted" if m else "green")], []))
        elif status == "owed" and owed > 0:
            style = "yellow+b" if soon and not m else ("muted" if m else "text")
            chunk: Line = [(GLYPHS["over"] + " ", "yellow")] if soon else []
            chunk += [("owed ", "muted"), (money(owed, m), style)]
            out.append((chunk, []))
            when = card.get("next_due_date")
            if when:
                text = (f"overdue since {fmt_day(when)}" if card_overdue(card, _today(d))
                        else f"due {fmt_day(when)}" + (", before payday" if soon else ""))
                out.append(([(text, "yellow" if soon else "muted")], dot))
        else:
            out.append(([(GLYPHS["done"] + " paid off", "green")], []))
        paid = int(card.get("paid_this_month_cents", 0) or 0)
        if paid > 0:
            out.append(([(f"{money(paid, m)} paid this month", "muted")], dot))
        charged = int(card.get("charged_since_last_payment_cents", 0) or 0)
        if detail and charged > 0 and status == "owed":
            out.append(([(f"{money(charged, m)} charged since the last payment", "muted")], dot))
        return out

    lines = pack_steady(items, d.mask, width, indent=head, cont=cont)
    if one_line and len(lines) > 1:
        lines = pack_steady(lambda m: items(m)[:2], d.mask, width, indent=head, cont=cont)
    return lines


def active_goals(goals: Iterable[Any]) -> List[Any]:
    """Goals worth showing: a target above $0 and not switched off."""
    out = []
    for g in goals or []:
        if getattr(g, "target_cents", 0) <= 0:
            continue
        if str(getattr(g, "status", "open")).lower() in ("inactive", "off", "disabled"):
            continue
        out.append(g)
    return out


def goal_name(g: Any) -> str:
    name = clean_name(getattr(g, "name", ""))
    if "saving" in name.lower():
        return "Savings goal"
    for tail in (" Payment", " Target", " Goal"):
        if name.endswith(tail):
            name = name[: -len(tail)]
    return name or "Goal"


def is_savings_goal(g: Any) -> bool:
    return goal_name(g) == "Savings goal"


def goal_lines(d: FinanceViewData, width: int, inline: bool = False) -> List[Line]:
    """One line per active goal: name, a 10-cell bar, then the progress.

    The savings goal is a monthly target: store.get_goals_progress() gives what was moved into
    savings this month (not the savings balance), so the line says 'this month'. `inline` (goals
    under ACCOUNTS, no GOALS title) starts each line with a 'Goal' label lined up with the account
    labels.
    """
    goals = active_goals(d.goals)
    if not goals:
        return []
    m = d.mask

    def title(g: Any) -> str:
        name = goal_name(g)
        return name[: -len(" goal")] if inline and name.endswith(" goal") else name

    name_w = min(18, max(cell_width(title(g)) for g in goals)) + 2

    def row(g: Any, mask: bool, bar_w: int, this_month: bool, label_w: int) -> Line:
        target = int(g.target_cents)
        monthly = is_savings_goal(g)
        saved = int(g.saved_cents)
        met = saved >= target
        if monthly:
            pct = 100.0 if met else max(0.0, saved * 100.0 / target)
        else:
            pct = 100.0 if met else max(0.0, min(100.0, float(getattr(g, "progress_pct", saved * 100.0 / target))))
        lead_in = GUTTER + rpad("Goal", label_w) if inline else GUTTER
        line: Line = [(lead_in, "muted"), (rpad(title(g), name_w), "text")]
        if bar_w:
            filled = bar_w if met else max(0, min(bar_w, int(round(pct / 100.0 * bar_w))))
            line += [("━" * filled, "green" if met else "blue"), ("─" * (bar_w - filled), "dim")]
            lead = " met  " if met else lpad(f"{pct:.0f}%", 5) + "  "
        else:
            lead = "met  " if met else f"{pct:.0f}%  "
        line += [(lead, "green+b" if met else "text"), (money(saved, mask, whole=True), "muted" if mask else "text"),
                 (f" of {money(target, mask, whole=True)}" + (" this month" if monthly and this_month else ""), "muted")]
        return line

    # Narrow panes drop ' this month', then shorten the bar, then drop it and tighten the 'Goal'
    # label, so the amounts are never cut. Measured masked and plain, so m never changes the line.
    variants = [(10, True, LABEL_W), (10, False, LABEL_W), (5, False, LABEL_W), (0, False, LABEL_W), (0, False, 5)]
    for bar_w, this_month, label_w in variants:
        if all(line_width(row(g, mk, bar_w, this_month, label_w)) <= width for g in goals for mk in (False, True)):
            break
    return [fit(row(g, m, bar_w, this_month, label_w), width) for g in goals]


# ---------------------------------------------------------------------------
# Overview: BILLS BEFORE PAYDAY
# ---------------------------------------------------------------------------


def _cycle_bills(d: FinanceViewData) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(still due, already paid) bills of this pay cycle. $0 due bills and a $0 card are left out."""
    pc = d.paycheck
    if pc is None:
        return [], []
    due: List[Dict[str, Any]] = []
    paid: List[Dict[str, Any]] = list(getattr(pc, "paid_bills", []) or [])
    for b in getattr(pc, "bills", []) or []:
        if b.get("status") == "due":
            if b.get("amount_cents", 0) > 0:
                due.append(b)
        else:
            paid.append(b)
    # A card with nothing owed shows as a $0 'paid' bill: that says nothing useful (the ACCOUNTS
    # Visa line already says paid off). Other paid bills stay, with or without an amount.
    paid = [b for b in paid if int(b.get("amount_cents", 0) or 0) > 0 or short_bill_name(b.get("name", "")) != "visa"]
    due.sort(key=lambda b: b.get("due_date", ""))
    paid.sort(key=lambda b: b.get("due_date", ""))
    return due, paid


def bills_section(d: FinanceViewData, width: int, header: bool = True, paid_rows: bool = True,
                  max_rows: Optional[int] = None) -> List[Line]:
    rw = d.runway
    m = d.mask
    due, paid = _cycle_bills(d)
    total_due = rw.bills_due_cents if rw else sum(b["amount_cents"] for b in due)
    trailing = f"{money(total_due, m)} due" if due else "nothing due"
    if paid and not paid_rows:
        trailing += f" {GLYPHS['dot']} {len(paid)} paid"
    lines: List[Line] = [section("BILLS BEFORE PAYDAY", width, trailing)]
    if d.paycheck is None:
        lines.append(fit([(GUTTER + "No paycheck plan, so there is no pay period to check.", "muted")], width))
        return lines
    if not due and not (paid and paid_rows):
        lines.append(fit([(GUTTER + GLYPHS["done"] + " No bills before payday.", "green")], width))
        return lines
    inner = width - len(GUTTER)
    date_w, amt_w = 12, 10
    # The running LEFT column shows whenever the bill names keep 24 cells (so a 64-cell column of
    # the two-column layout has it too); only a narrow pane drops it.
    left_w = 12 if inner - date_w - amt_w - 12 >= 24 else 0
    name_w = max(8, min(50, inner - date_w - amt_w - left_w))
    if header:
        head = rpad("DATE", date_w) + rpad("BILL", name_w) + lpad("AMOUNT", amt_w) + (lpad("LEFT", left_w) if left_w else "")
        lines.append(fit([(GUTTER + head, "muted")], width))
    today = rw.today if rw else _today(d).strftime("%Y-%m-%d")
    running = rw.checking_cents if rw else d.checking_cents
    body: List[Line] = []
    # One list by date: a bill paid on Oct 25 comes before one due on Nov 2.
    rows = [(x.get("due_date", ""), 0, x) for x in due]
    if paid_rows:
        rows += [(x.get("due_date", ""), 1, x) for x in paid]
    rows.sort(key=lambda r: (r[0], r[1]))
    for _, is_paid, b in rows:
        if is_paid:
            amount = num(b["amount_cents"], m) if b.get("amount_cents", 0) > 0 else ""
            tail = f"paid {lpad(amount, 8)}" if amount else "paid"
            row = [(GUTTER + rpad(fmt_day(b.get("due_date", "")), date_w), "muted"),
                   (rpad(clean_name(b.get("name", "")), name_w + amt_w - len(tail) - 2), "muted"),
                   (GLYPHS["done"] + " ", "green"), (tail, "muted")]
            body.append(fit(row, width))
            continue
        running -= b["amount_cents"]
        name = clean_name(b.get("name", "")) + (" (est.)" if b.get("is_estimate") else "")
        tag = "  today" if b.get("due_date") == today else ""
        row: Line = [(GUTTER + rpad(fmt_day(b.get("due_date", "")), date_w), "text")]
        row += [(rpad(name, name_w - len(tag)) if tag else rpad(name, name_w), "text")]
        if tag:
            row += [(tag, "yellow")]
        row += [(lpad(num(b["amount_cents"], m), amt_w), "muted" if m else "text")]
        if left_w:
            row += [(lpad(num(running, m), left_w), "muted")]
        body.append(fit(row, width))
    if max_rows is not None and len(body) > max_rows:
        body = body[: max(0, max_rows)]
    return lines + body


# ---------------------------------------------------------------------------
# Overview: RECENT DAYS
# ---------------------------------------------------------------------------


def day_entries(d: FinanceViewData) -> List[Tuple[str, Any, int]]:
    """Display rows, newest first: ('day', index, 0), ('gap', date, days) or ('today', date, 0)."""
    days = d.days or []
    today = _today(d).strftime("%Y-%m-%d")
    out: List[Tuple[str, Any, int]] = []
    if not days or days[0].get("date", "") < today:
        out.append(("today", today, 0))
        prev = _date(today)
    else:
        prev = None
    for i, day in enumerate(days):
        cur = _date(day.get("date"))
        if prev is not None and cur is not None:
            gap = (prev - cur).days - 1
            if 1 <= gap <= 2:
                for k in range(1, gap + 1):
                    out.append(("gap", (prev - timedelta(days=k)).strftime("%Y-%m-%d"), 1))
            elif gap > 2:
                out.append(("gap", (prev - timedelta(days=1)).strftime("%Y-%m-%d"), gap))
        out.append(("day", i, 0))
        if cur is not None:
            prev = cur
    return out


def _bar_layout(width: int, notes_w: Optional[int] = None) -> Tuple[int, int]:
    """(bar width, amount width) for a day row. `notes_w` = cells kept for the notes after the amount."""
    amt_w = 9
    notes = notes_w if notes_w is not None else (22 if width > NARROW_MAX else 16)
    bar = max(4, min(BAR_FULL, width - len(GUTTER) - 12 - amt_w - notes))
    return bar, amt_w


BAR_FULL = 30  # longest day bar: what a 95-column pane draws
MIN_BAR = 8  # a day bar never shrinks below this to make room for notes


def _note_cells(day: Dict[str, Any], extra: Optional[Line] = None) -> List[int]:
    """Cells each note of a day takes (separator included), the wider of masked and plain."""
    out = []
    for (a, sa), (b, sb) in zip(day_notes(day, False), day_notes(day, True)):
        out.append(max(line_width(sa) + line_width(a), line_width(sb) + line_width(b)))
    if extra:
        out.append(2 + line_width(extra))
    return out


def notes_room(d: "FinanceViewData", width: int, extra: Optional[Line] = None) -> int:
    """Cells for the notes column: what the longest day's notes need (every loaded day, so the bars
    keep their width while scrolling), but never so much that bars drop under MIN_BAR."""
    room = width - len(GUTTER) - 12 - 9 - MIN_BAR
    return max(0, min(max(_notes_need(d, extra), 4), room))


def _notes_need(d: "FinanceViewData", extra: Optional[Line] = None) -> int:
    return max([sum(_note_cells(x, extra)) for x in d.days or []] + [0])


def days_list_width(d: "FinanceViewData", extra: Optional[Line] = None) -> int:
    """Width a days list needs (gutter included) for full BAR_FULL bars and every note. A side
    column narrower than this would show less than a 95-column pane, so the two-column layouts
    are used only when their days column gets at least this much."""
    return len(GUTTER) + 12 + BAR_FULL + 9 + max(_notes_need(d, extra), 4)


def bar_peak(d: "FinanceViewData") -> int:
    """The day that fills a whole bar: the biggest loaded day, so a bar never changes length while
    the list scrolls."""
    return max([day_spent(x) for x in d.days or []] + [1])


def day_row(d: FinanceViewData, day: Dict[str, Any], width: int, selected: bool, peak: int,
            notes_extra: Optional[Line] = None, notes_w: Optional[int] = None) -> Line:
    """'Wed Oct 28  ▇▇▇▇▇▇▇▇        31.98  visa  +1,480 pay' (bar yellow when over the allowance).

    Notes go in order while they fit and stop at the first one that does not, measured in their
    wider (masked or plain) form, so pressing m never changes which notes show.
    """
    m = d.mask
    spent = day_spent(day)
    # The bar color compares only groceries, dining and gas with the allowance.
    counted = int(day.get("allowance_spent_cents", spent) or 0)
    allowance = daily_allowance(d)
    bar_w, amt_w = _bar_layout(width, notes_w)
    content: Line = [(rpad(fmt_day(day.get("date", "")), 12), "text+b" if selected else "text")]
    if spent > 0:
        filled = max(1, min(bar_w, round(spent / max(1, peak) * bar_w)))
        over = allowance > 0 and counted > allowance
        content += [(BAR * filled, "yellow" if over else "blue"), (" " * (bar_w - filled), "text"),
                    (lpad(num(spent, m), amt_w), "muted" if m else ("text+b" if selected else "text"))]
    else:
        content += [(rpad(GLYPHS["dot"] + "  no spending", bar_w + amt_w), "muted")]
    notes = day_notes(day, m)
    if notes_extra:
        notes.append((notes_extra, [("  ", "text")]))
    used = len(GUTTER) + line_width(content)
    for (chunk, sep), need in zip(notes, _note_cells(day, notes_extra)):
        if used + need > width:
            break
        content += list(sep) + list(chunk)
        used += need
    return marked(content, selected, width)


def recent_legend(d: FinanceViewData, width: int) -> Line:
    """'▇ groceries, dining, gas under $18.57 a day   ▇ over   ↵ open day'.

    It names what the bar colors compare: only groceries, dining and gas against the daily
    allowance, so it is not read as a second 'a day after bills' number. Shorter forms on narrow
    panes; measured masked and plain, so m never changes which form shows."""
    allowance = daily_allowance(d)
    if allowance <= 0:
        return [(GUTTER, "text"), ("↵", "cyan"), (" open day", "muted")]
    over: Line = [("   ", "text"), (BAR, "yellow"), (" over", "muted")]
    forms = [lambda a: f" groceries, dining, gas under {a} a day",
             lambda a: f" food and gas under {a}/day",
             lambda a: f" under {a}/day"]
    for i, form in enumerate(forms):
        widest = max(cell_width(form(money(allowance, mk))) for mk in (False, True))
        if len(GUTTER) + 1 + widest + line_width(over) <= width or i == len(forms) - 1:
            break
    line: Line = [(GUTTER, "text"), (BAR, "blue"), (rpad(form(money(allowance, d.mask)), widest), "muted")] + over
    if line_width(line) + 14 <= width:
        line += [("   ", "text"), ("↵", "cyan"), (" open day", "muted")]
    return line


def recent_section(d: FinanceViewData, width: int, avail: int, legend: bool = True) -> Tuple[List[Line], Dict[int, int], int]:
    """RECENT DAYS: title, day rows (fills `avail` rows), legend. Returns (lines, {line: day}, offset)."""
    m = d.mask
    days = d.days or []
    sel = max(0, min(d.selected_day_idx, len(days) - 1)) if days else 0
    # The month total follows the selected day, so scrolling into September shows September.
    month_of = (_date(days[sel].get("date")) if days else None) or _today(d)
    month_key = month_of.strftime("%Y-%m")
    spent_month = sum(day_spent(x) for x in days if str(x.get("date", "")).startswith(month_key))
    trailing = f"{month_of:%b}: {money(spent_month, m, whole=True)} spent"
    legend_line = recent_legend(d, width) if legend else []
    list_rows = max(1, avail - 1 - (1 if legend_line else 0))
    if not days:
        lines: List[Line] = [section("RECENT DAYS", width, trailing)]
        lines.append(fit([(GUTTER + "No days logged yet. Press a to add one.", "muted")], width))
        return lines, {}, 0
    entries = day_entries(d)
    sel_pos = next((k for k, e in enumerate(entries) if e[0] == "day" and e[1] == sel), 0)
    # The window starts as high as it can while the selected day stays in it, so today and the
    # newest days are on screen unless the selection needs the room. No offset is carried over
    # from an earlier render: after a pane shrinks and grows back, the list starts at the top again.
    offset = max(0, sel_pos - list_rows + 1)
    if offset:
        # Rows above the window: the title says so (it costs no row).
        above = f"{offset} newer above {GLYPHS['dot']} {trailing}"
        if len(GUTTER) + cell_width("RECENT DAYS ") + 2 + 2 + cell_width(above) <= width:
            trailing = above
    lines = [section("RECENT DAYS", width, trailing)]
    visible = entries[offset: offset + list_rows]
    peak = bar_peak(d)
    notes_w = notes_room(d, width)
    hits: Dict[int, int] = {}
    for kind, ref, n in visible:
        if kind == "day":
            hits[len(lines)] = ref
            lines.append(day_row(d, days[ref], width, ref == sel, peak, notes_w=notes_w))
        elif kind == "today":
            lines.append(marked([(rpad(fmt_day(ref), 12), "text"),
                                 (GLYPHS["dot"] + "  nothing logged yet today", "muted")], False, width))
        else:
            text = GLYPHS["dot"] + "  no spending" + (f" for {n} days" if n > 1 else "")
            lines.append(marked([(rpad(fmt_day(ref), 12), "muted"), (text, "muted")], False, width))
    if legend_line:
        lines.append(fit(legend_line, width))
    return lines, hits, offset


# ---------------------------------------------------------------------------
# Overview: putting it together
# ---------------------------------------------------------------------------


@dataclass
class _Opts:
    goals_inline: bool = False  # goals go under ACCOUNTS (no GOALS title), labelled 'Goal'
    card_one_line: bool = False  # the Visa line drops its extra notes rather than wrap
    hero_gap: bool = True  # blank row between the UNTIL PAYDAY title and the number
    legend: bool = True  # recent-days legend
    paid_rows: bool = True  # paid bills listed under the due ones
    strip_rows: int = 5  # runway strip rows at most (5, 4, 3 or 2; 5 only with two amounts rows)
    bills_header: bool = True  # DATE / BILL / AMOUNT header
    strip_gap: bool = True  # blank row between the per-day line and the strip
    min_recent: int = 5  # day rows the recent list needs before the top shrinks further
    section_gaps: bool = True  # blank row between sections
    bills: bool = True  # BILLS BEFORE PAYDAY section


_SHRINK_STEPS: Tuple[Tuple[str, Any], ...] = (
    ("strip_rows", 4),
    ("goals_inline", True),
    ("hero_gap", False),
    ("card_one_line", True),
    ("legend", False),
    ("paid_rows", False),
    ("strip_rows", 3),
    ("bills_header", False),
    ("min_recent", 3),
    ("strip_rows", 2),
    ("strip_gap", False),
    ("section_gaps", False),
    ("min_recent", 2),
    ("bills", False),
    ("min_recent", 1),
)


def _levels() -> Iterable[_Opts]:
    o = _Opts()
    yield replace(o)
    for name, value in _SHRINK_STEPS:
        setattr(o, name, value)
        yield replace(o)


def _top(d: FinanceViewData, width: int, o: _Opts) -> List[Line]:
    """UNTIL PAYDAY, ACCOUNTS (with the card), GOALS and BILLS BEFORE PAYDAY."""
    badge = mask_badge(d.mask)
    until: List[Line] = [section("UNTIL PAYDAY", width, badge[0], badge[1])]
    if o.hero_gap:
        until.append(blank())
    until += hero_lines(d, width)
    strip = runway_strip(d, width, o.strip_rows)
    if strip:
        if o.strip_gap:
            until.append(blank())
        until += strip
    accounts = [section("ACCOUNTS", width)] + account_lines(d, width) + card_lines(
        d, width, detail=width >= 100, one_line=o.card_one_line)
    goals = goal_lines(d, width, inline=o.goals_inline)
    groups: List[List[Line]] = [until]
    if goals and o.goals_inline:
        accounts += goals
        groups.append(accounts)
    else:
        groups.append(accounts)
        if goals:
            groups.append([section("GOALS", width)] + goals)
    if o.bills:
        groups.append(bills_section(d, width, header=o.bills_header, paid_rows=o.paid_rows))
    out: List[Line] = []
    for g in groups:
        if out and o.section_gaps:
            out.append(blank())
        out += g
    return out


def _column(d: FinanceViewData, width: int, height: int, with_recent: bool) -> Tuple[List[Line], Dict[int, int], int]:
    levels = list(_levels())
    for i, o in enumerate(levels):
        top = _top(d, width, o)
        last = i == len(levels) - 1
        if not with_recent:
            if len(top) <= height or last:
                return top[:height], {}, d.day_scroll_offset
            continue
        gap = 1 if o.section_gaps else 0
        room = height - len(top) - gap
        if room >= 1 + o.min_recent + (1 if o.legend else 0) or last:
            if room < 2:
                return top[:height], {}, d.day_scroll_offset
            recent, hits, offset = recent_section(d, width, room, legend=o.legend)
            base = len(top) + gap
            lines = top + ([blank()] if gap else []) + recent
            return lines[:height], {base + k: v for k, v in hits.items() if base + k < height}, offset
    return [], {}, 0  # pragma: no cover


def no_data_lines(width: int, title: Optional[Line] = None) -> List[Line]:
    """Empty state for a vault with no setup and no entries (no default numbers shown)."""
    from telemachus.cockpit.view_common import centered, empty_state

    lines: List[Line] = [title] if title else [section("UNTIL PAYDAY", width)]
    lines += [blank()] + empty_state("No finance data yet.", width, "python3 -m telemachus.finance setup",
                                     after="  to start.")
    if not title:  # a adds entries on the overview only
        lines += [blank(), centered([("or press ", "muted"), ("a", "cyan"), (" to log a first entry", "muted")], width)]
    return lines


OVERVIEW_LEFT_MIN = 64  # narrowest left column of the two-column overview


def build_overview(d: FinanceViewData, cols: int, rows: int) -> Rendered:
    """Money overview ('Today at a glance' for money). `cols` includes the gutter."""
    width, height = max(20, cols), max(1, rows)
    if not d.has_data:
        return Rendered(lines=_finish(no_data_lines(width), width, height))
    right_need = days_list_width(d)
    if width >= WIDE_MIN and width - 2 - right_need >= OVERVIEW_LEFT_MIN:
        # Two columns only when RECENT DAYS gets its full bars and notes and the left column keeps
        # the 95-column content (bills with their LEFT column); otherwise one column, as at 95.
        right_w = max(right_need, min(72, int((width - 2) * 0.45)))
        left_w = width - 2 - right_w
        left, _, _ = _column(d, left_w, height, with_recent=False)
        from telemachus.cockpit.finance_screens import _budget_lines, _this_month

        # Extra rows on the left go to this month's budgets, then the month's math.
        for extra in (_budget_lines(d, left_w, title_gap=False), _this_month(d, left_w, title_gap=False)):
            if extra and len(left) + 1 + len(extra) <= height:
                left = left + [blank()] + extra
        right, hits, offset = recent_section(d, right_w, height)
        lines = hcat(left, left_w, right, right_w)
        return Rendered(lines=_finish(lines, width, height), day_rows={k: v for k, v in hits.items() if k < height},
                        day_scroll_offset=offset)
    lines, hits, offset = _column(d, width, height, with_recent=True)
    return Rendered(lines=_finish(lines, width, height), day_rows=hits, day_scroll_offset=offset)


# ---------------------------------------------------------------------------
# The other screens (finance_screens.py). Imported on use, so either module can load first.
# ---------------------------------------------------------------------------


def build_day(d: FinanceViewData, cols: int, rows: int) -> Rendered:
    from telemachus.cockpit import finance_screens

    return finance_screens.build_day(d, cols, rows)


def build_math(d: FinanceViewData, cols: int, rows: int) -> Rendered:
    from telemachus.cockpit import finance_screens

    if not d.has_data:
        width = max(20, cols)
        return Rendered(lines=_finish(no_data_lines(width, crumb("Math", width, d.mask)), width, max(1, rows)))

    return finance_screens.build_math(d, cols, rows)


def build_bills(d: FinanceViewData, cols: int, rows: int) -> Rendered:
    from telemachus.cockpit import finance_screens

    if not d.has_data:
        width = max(20, cols)
        return Rendered(lines=_finish(no_data_lines(width, crumb("Bills", width, d.mask)), width, max(1, rows)))

    return finance_screens.build_bills(d, cols, rows)


# ---------------------------------------------------------------------------
# Footer and help keys for each Money screen
# ---------------------------------------------------------------------------

ENTRY_KEYS = KeyGroup.of(
    "ENTRY",
    KeyHint("↵", "new line", "add line, blank saves", footer=False),
    KeyHint("⌃S", "save all", "save all", footer=False),
    KeyHint("tab", "account", "account / next field", footer=False),
    KeyHint("←/→", "day", "change day (add box)", footer=False),
    KeyHint("⌃D", "delete", "delete (edit box)", footer=False),
    KeyHint("esc", "cancel", "cancel", footer=False),
)


# Footer hints that leave the footer first when it is too narrow (they stay in the ? help).
# The back key (esc) is never in these lists, so it always stays in the footer.
_FOOTER_DROP = {
    "daily": ("j/k", "e", "i", "m", "a"),
    "day": ("d", "a", "e", "m", "←/→", "j/k"),
    "bills": ("i", "m", "j/k", "h"),
    "math": ("m", "v", "j/k"),
}


def _footer_width(groups: Sequence[KeyGroup]) -> int:
    """Cells the shell footer needs for these groups plus '? keys  q quit' (shell.build_footer)."""
    from telemachus.cockpit.shell import GLOBAL_KEYS

    parts = []
    for g in list(groups) + [GLOBAL_KEYS]:
        shown = [h for h in g.hints if h.footer]
        if shown:
            parts.append(sum(cell_width(h.keys) + 1 + cell_width(h.label) for h in shown) + 2 * (len(shown) - 1))
    return len(GUTTER) + sum(parts) + 7 * max(0, len(parts) - 1)


def _trim_footer(mode: str, groups: List[KeyGroup], width: int) -> List[KeyGroup]:
    """Moves the least needed hints to the help overlay only, until every group fits the footer."""
    for keys in _FOOTER_DROP.get(mode, ()):
        if _footer_width(groups) <= width:
            break
        groups = [KeyGroup(g.title, tuple(replace(h, footer=False) if h.keys == keys else h for h in g.hints))
                  for g in groups]
    return groups


def money_keys(mode: str, mask: bool = False, width: Optional[int] = None) -> List[KeyGroup]:
    """Key groups for a Money screen ("daily" overview, "day", "math", "bills").

    `mask` flips 'm hide $' to 'm show $'. With `width` (the footer width), hints the footer has
    no room for move to the help overlay only, so the back key and the view keys stay visible.
    """
    hide = KeyHint("m", "show $" if mask else "hide $", "show or hide amounts")
    if mode == "day":
        groups = [
            KeyGroup.of("DAY", KeyHint("j/k", "move", "move between entries"),
                        KeyHint("←/→", "day", "older or newer day"),
                        KeyHint("a", "add", "add entries"), KeyHint("e", "edit", "edit entry"),
                        KeyHint("d", "delete", "delete (press twice)")),
            KeyGroup.of("VIEW", KeyHint("esc", "back", "back to the overview"), hide),
            ENTRY_KEYS,
        ]
    elif mode == "math":
        # j/k scroll Math when it is taller than the pane (build_math sets math_scrollable); the
        # footer shows the hint only then, the ? help always.
        scroll = KeyHint("j/k", "scroll", "scroll", footer=bool(VIEW_PREFS.get("math_scrollable")))
        groups = [KeyGroup.of("MATH", scroll),
                  KeyGroup.of("VIEW", KeyHint("esc", "back", "back to the overview"), KeyHint("v", "bills", "bills"), hide)]
    elif mode == "bills":
        shown = VIEW_PREFS.get("show_hidden_bills", False)
        groups = [
            KeyGroup.of("BILLS", KeyHint("j/k", "move", "move"),
                        KeyHint("h", "hide inactive" if shown else "show hidden", "show or hide inactive and $0 bills")),
            KeyGroup.of("VIEW", KeyHint("esc", "back", "back to the overview"), KeyHint("i", "math", "math", footer=False), hide),
        ]
    else:
        groups = [
            KeyGroup.of("MONEY", KeyHint("j/k", "move", "move"), KeyHint("↵", "open day", "open day"),
                        KeyHint("a", "add", "add entries"), KeyHint("e", "edit", "edit entry"),
                        KeyHint("d", "delete", "delete (in a day)", footer=False)),
            KeyGroup.of("VIEWS", KeyHint("v", "bills", "bills"), KeyHint("i", "math", "math"), hide),
            ENTRY_KEYS,
        ]
    if width is not None:
        groups = _trim_footer(mode, groups, width)
    return groups


def bill_count(d_or_breakdown: Any) -> int:
    """Rows on the Bills screen (hidden bills count only while h shows them). For j/k limits."""
    from telemachus.cockpit.finance_screens import bill_display

    d = d_or_breakdown
    if not isinstance(d, FinanceViewData):
        d = FinanceViewData(None, 0, 0, 0, 0, [], [], None, None, bills_breakdown=d_or_breakdown or {})
    return len(bill_display(d)[0])


# ---------------------------------------------------------------------------
# Money keymap: key -> action name. The TUI carries out the action. Actions: mask, bills, math,
# back, up, down, open_day, prev_day, next_day, add, edit, delete, delete_hint. None = ignore.
# ---------------------------------------------------------------------------

# The TUI passes the full drawable width and draws lines from column 0; the builders draw the
# 2-cell gutter themselves (the ▸ marker sits in it).
FULL_WIDTH = True

try:
    import curses as _curses

    _UP, _DOWN, _LEFT, _RIGHT, _ENTER = _curses.KEY_UP, _curses.KEY_DOWN, _curses.KEY_LEFT, _curses.KEY_RIGHT, _curses.KEY_ENTER
except ImportError:  # pragma: no cover
    _UP, _DOWN, _LEFT, _RIGHT, _ENTER = 259, 258, 260, 261, 343


def money_action(mode: str, ch: int) -> Optional[str]:
    """Action for key `ch` on Money screen `mode` ("daily", "day", "math", "bills").

    v opens Bills and i opens Math (from any Money screen; on its own screen the key does nothing),
    and only esc goes back to the overview, so no key means both open and back. On Bills, h gives
    "toggle_hidden_bills" (the TUI flips VIEW_PREFS["show_hidden_bills"]); this function changes nothing.
    """
    if ch in (ord("m"), ord("M")):
        return "mask"
    if ch in (ord("v"), ord("V")):
        return None if mode == "bills" else "bills"
    if ch in (ord("i"), ord("I")):
        return None if mode == "math" else "math"
    if ch == 27:
        return "back" if mode in ("day", "math", "bills") else None
    if ch in (_UP, ord("k")):
        return "up"
    if ch in (_DOWN, ord("j")):
        return "down"
    if mode == "bills" and ch in (ord("h"), ord("H")):
        return "toggle_hidden_bills"
    if mode == "daily" and ch in (10, 13, _ENTER, _RIGHT, ord("l")):
        return "open_day"
    if mode == "day":
        if ch in (_LEFT, ord("h")):
            return "prev_day"
        if ch in (_RIGHT, ord("l")):
            return "next_day"
    if mode in ("daily", "day"):
        if ch in (ord("a"), ord("A")):
            return "add"
        if ch in (ord("e"), ord("E")):
            return "edit"
        if ch in (ord("d"), ord("D")):
            return "delete" if mode == "day" else "delete_hint"
    return None


# ---------------------------------------------------------------------------
# Selection that survives a refresh (the TUI keeps indexes; new days shift them)
# ---------------------------------------------------------------------------


def selection_key(days: Sequence[Dict[str, Any]], day_idx: int, item_idx: int,
                  focus: str = "items") -> Tuple[Optional[str], Optional[str], str]:
    """(date, transaction id, focus) of the selected day and entry. Unlike the indexes, it still
    names the same entry after a refresh brings in a newer day or another process edits the ledger.
    The id is None when the day has no entries or the focus is on the days list."""
    if not days or not 0 <= day_idx < len(days):
        return None, None, focus
    day = days[day_idx]
    items = day.get("items") or []
    tx_id = None
    if focus == "items" and items:
        tx_id = items[max(0, min(item_idx, len(items) - 1))].get("id")
    return str(day.get("date", "")), tx_id, focus


def find_selection(days: Sequence[Dict[str, Any]], date: Optional[str],
                   tx_id: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """(day index, item index) of `date` and `tx_id` in freshly loaded days. The item index is None
    when the entry is gone (or no id was given); both are None when the day is gone."""
    if not date:
        return None, None
    for i, day in enumerate(days or []):
        if str(day.get("date", "")) != date:
            continue
        if tx_id is None:
            return i, None
        for k, it in enumerate(day.get("items") or []):
            if it.get("id") == tx_id:
                return i, k
        return i, None
    return None, None
