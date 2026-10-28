"""Layout for the Cockpit Finances tab ("Runway, in plain English").

Everything here is pure: it turns finance data into lines of styled text segments and never
touches curses. `tui.py` maps style names to curses attributes and draws the lines, which keeps
the layout testable and keeps `tui.py` from growing further.

A style is a name from STYLES, optionally followed by "+b" for bold (for example "yellow+b").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from telemachus.cockpit.finance_panel import RunwayInfo

Seg = Tuple[str, str]
Line = List[Seg]

STYLES = ("dim", "sub", "fg", "snow", "line", "cyan", "blue", "mag", "green", "yellow", "sel")

WIDE_MIN_COLS = 100
MAX_RECENT_ROWS = 10  # keeps the recent-days box about as tall as the bills box on tall terminals
MASKED = "***.**"

_GLYPHS: Dict[str, Tuple[str, str, str]] = {
    "0": ("█▀█", "█ █", "▀▀▀"),
    "1": ("▀█ ", " █ ", "▀▀▀"),
    "2": ("▀▀█", "█▀▀", "▀▀▀"),
    "3": ("▀▀█", " ▀█", "▀▀▀"),
    "4": ("█ █", "▀▀█", "  ▀"),
    "5": ("█▀▀", "▀▀█", "▀▀▀"),
    "6": ("█▀▀", "█▀█", "▀▀▀"),
    "7": ("▀▀█", "  █", "  ▀"),
    "8": ("█▀█", "█▀█", "▀▀▀"),
    "9": ("█▀█", "▀▀█", "▀▀▀"),
    ".": (" ", " ", "▀"),
    ",": (" ", " ", "▄"),
    "-": ("   ", "▀▀▀", "   "),
    "*": ("▄▀▄", "▀█▀", "▀ ▀"),
}


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------


def money(cents: int, mask: bool = False, signed: bool = False, whole: bool = False) -> str:
    """'$1,234.56'. `signed` adds +/-; `whole` drops the cents."""
    if mask:
        return f"${MASKED}"
    sign = ""
    if cents < 0:
        sign = "-"
    elif signed and cents > 0:
        sign = "+"
    value = abs(cents) / 100.0
    body = f"{round(value):,}" if whole else f"{value:,.2f}"
    return f"{sign}${body}"


def plain(cents: int, mask: bool = False, signed: bool = False) -> str:
    """Amount without the dollar sign, for table columns."""
    return money(cents, mask, signed).replace("$", "")


def _date(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def fmt_day(value: str, comma: bool = False) -> str:
    """'Fri Oct 30' (or 'Fri, Oct 30'). Falls back to the raw text."""
    dt = _date(value)
    if dt is None:
        return value
    sep = ", " if comma else " "
    return f"{dt:%a}{sep}{dt:%b} {dt.day}"


def clean_name(name: str) -> str:
    """Drops emoji and other non-ASCII glyphs (they render at inconsistent widths)."""
    return "".join(ch for ch in name if ord(ch) < 128).strip()


def line_width(line: Line) -> int:
    return sum(len(text) for text, _ in line)


def rpad(text: str, n: int) -> str:
    return text[:n] if len(text) >= n else text + " " * (n - len(text))


def lpad(text: str, n: int) -> str:
    return text[:n] if len(text) >= n else " " * (n - len(text)) + text


def fit(line: Line, width: int) -> Line:
    """Clips or pads a line to exactly `width` cells."""
    out: Line = []
    used = 0
    for text, style in line:
        if used + len(text) <= width:
            out.append((text, style))
            used += len(text)
            continue
        take = width - used
        if take > 0:
            out.append((text[:take], style))
        used = width
        break
    if used < width:
        out.append((" " * (width - used), "fg"))
    return out


def restyle(line: Line, style: str) -> Line:
    return [(text, style) for text, _ in line]


def big_digits(text: str) -> List[str]:
    """Three-row block digits."""
    rows = ["", "", ""]
    for i, ch in enumerate(text):
        glyph = _GLYPHS.get(ch, ("   ", "   ", "   "))
        for r in range(3):
            rows[r] += (" " if i else "") + glyph[r]
    return rows


def box(title: str, body: List[Line], width: int, trailing: str = "", min_body: int = 0) -> List[Line]:
    """Rounded box with a title (and optional right-hand note) in its top edge."""
    inner = width - 4
    title_seg = title[: max(0, inner - 2)]
    trail = trailing[: max(0, inner - len(title_seg) - 4)] if trailing else ""
    dashes = width - 5 - len(title_seg) - (len(trail) + 3 if trail else 0)
    top: Line = [("╭─ ", "line"), (title_seg, "cyan+b"), (" " + "─" * max(1, dashes), "line")]
    if trail:
        top += [(" ", "line"), (trail, "dim"), (" ─", "line")]
    top.append(("╮", "line"))
    rows = list(body)
    while len(rows) < min_body:
        rows.append([])
    middle = [[("│ ", "line")] + fit(r, inner) + [(" │", "line")] for r in rows]
    return [top] + middle + [[("╰" + "─" * (width - 2) + "╯", "line")]]


def hcat(blocks: Sequence[Tuple[List[Line], int]], gap: int = 2) -> List[Line]:
    """Places blocks side by side; shorter blocks are padded with blanks."""
    height = max(len(lines) for lines, _ in blocks)
    rows: List[Line] = []
    for i in range(height):
        row: Line = []
        for j, (lines, w) in enumerate(blocks):
            if j and gap:
                row.append((" " * gap, "fg"))
            row += fit(lines[i] if i < len(lines) else [], w)
        rows.append(row)
    return rows


def progress_bar(pct: float, width: int) -> Line:
    filled = max(0, min(width, int(round(pct / 100.0 * width))))
    return [("━" * filled, "mag"), ("─" * (width - filled), "line")]


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


@dataclass
class Rendered:
    """Lines to draw plus which line index maps to which selectable row."""

    lines: List[Line]
    day_rows: Dict[int, int] = field(default_factory=dict)  # line index -> day index
    item_rows: Dict[int, int] = field(default_factory=dict)  # line index -> item index
    day_scroll_offset: int = 0


# ---------------------------------------------------------------------------
# Overview screen
# ---------------------------------------------------------------------------


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def hero_box(d: FinanceViewData, width: int) -> List[Line]:
    rw = d.runway
    if rw is None:
        return box(
            "UNTIL PAYDAY",
            [[("No paycheck plan is set up yet.", "dim")], [("Add paycheck_plan to setup.yaml.", "dim")]],
            width,
        )
    short = rw.left_cents < 0
    amount_text = MASKED if d.mask else f"{abs(rw.left_cents) / 100.0:,.2f}"
    digits = big_digits(amount_text)
    style = "yellow+b" if short else "snow+b"
    payday = fmt_day(rw.next_paycheck, comma=True)
    body: List[Line] = [
        [("Bills before payday are more than checking by" if short else "You have", "dim")],
        [("$ ", style), (digits[0], style)],
        [("  ", "fg"), (digits[1], style)],
        [("  ", "fg"), (digits[2], style)],
        [],
    ]
    if short:
        body.append([("with ", "fg"), (_plural(rw.days_left, "day"), "snow+b"), (f" until payday on {payday}.", "fg")])
    else:
        body.append([("to last ", "fg"), (_plural(rw.days_left, "day"), "snow+b"), (f", until payday on {payday}.", "fg")])
        body.append([("That's ", "fg"), (f"{money(rw.per_day_cents, d.mask)} a day", "cyan+b"), (" after bills.", "fg")])
    pace = money(rw.pace_cents, d.mask)
    if rw.run_out_date:
        body.append([("▲ ", "yellow"), (f"Lately {pace}/day, which hits $0 by {fmt_day(rw.run_out_date)}.", "yellow")])
    elif rw.pace_cents > rw.per_day_cents > 0:
        body.append([("▲ ", "yellow"), (f"Lately {pace}/day, above your budget.", "yellow")])
    elif rw.pace_cents > 0:
        body.append([("Lately ", "dim"), (f"{pace}/day, within budget.", "green")])
    return box("UNTIL PAYDAY", body, width)


def runway_track(
    start: datetime,
    end: datetime,
    today: datetime,
    due_by_date: Dict[str, int],
    paid_by_date: Dict[str, int],
    inner: int,
    mask: bool = False,
) -> List[Line]:
    """Four lines (months, day numbers, the track, amounts) from `start` to `end`.

    Bills still due are marked with a yellow triangle, bills already paid with a green check, `today`
    with a blue dot and paydays with a diamond. Returns [] when the chart would not fit in `inner` cells.
    """
    slots = (end - start).days + 1
    if slots < 2:
        return []
    step = 4 if slots * 4 - 3 <= inner else 3
    if slots * step - (step - 1) > inner:
        return []
    cells = slots * step
    months = [" "] * cells
    labels = [" "] * cells
    mark_chars = [" "] * cells
    mark_styles = ["yellow"] * cells
    track: Line = []

    def put(buf: List[str], slot: int, text: str) -> bool:
        pos = slot * step
        if pos + len(text) > cells or any(c != " " for c in buf[pos : pos + len(text)]):
            return False
        buf[pos : pos + len(text)] = list(text)
        return True

    def amount_text(cents: int) -> str:
        return "-" + ("**" if mask else f"{round(cents / 100):d}")

    def slot_of(day: str) -> Optional[int]:
        dt = _date(day)
        return (dt - start).days if dt else None

    due_slots = {i: c for day, c in due_by_date.items() if (i := slot_of(day)) is not None}
    paid_slots = {i: c for day, c in paid_by_date.items() if (i := slot_of(day)) is not None}
    for i in range(slots):
        dt = start + timedelta(days=i)
        put(labels, i, str(dt.day))
        if i == 0 or dt.day == 1:
            put(months, i, f"{dt:%b}")
        if i == slots - 1 or (i == 0 and start != today):
            track.append(("◆", "green+b"))
        elif dt == today:
            track.append(("●", "blue+b"))
        elif i in due_slots:
            track.append(("▼", "yellow+b"))
        elif i in paid_slots:
            track.append(("✓", "green+b"))
        else:
            track.append(("┼", "line"))
        if i < slots - 1:
            track.append(("─" * (step - 1), "line"))
    for slot, cents in paid_slots.items():
        if 0 < slot < slots - 1 and put(mark_chars, slot, amount_text(cents)):
            for k in range(slot * step, slot * step + len(amount_text(cents))):
                mark_styles[k] = "green"
    for slot, cents in due_slots.items():
        if 0 < slot < slots - 1:
            put(mark_chars, slot, amount_text(cents))
    if start != today:
        put(mark_chars, 0, "pay")
    put(mark_chars, slots - 1, "pay")
    marks: Line = []
    for ch, st in zip(mark_chars, mark_styles):
        if marks and marks[-1][1] == st:
            marks[-1] = (marks[-1][0] + ch, st)
        else:
            marks.append((ch, st))
    return [[("".join(months), "dim")], [("".join(labels), "sub")], track, marks]


def _wrap_items(items: List[Line], width: int) -> List[Line]:
    """Packs items (joined by ' + ') into lines no wider than `width`."""
    rows: List[Line] = []
    cur: Line = []
    for item in items:
        extra = line_width(item) + (3 if cur else 0)
        if cur and line_width(cur) + extra > width:
            rows.append(cur)
            cur = []
            extra = line_width(item)
        if cur:
            cur.append((" + ", "dim"))
        cur.extend(item)
    if cur:
        rows.append(cur)
    return rows


def build_cycle_detail(d: FinanceViewData, width: int, rows: int) -> List[Line]:
    """Bills view section: the whole pay cycle (last payday to next payday), chart plus what is due each day."""
    pc = d.paycheck
    rw = d.runway
    if pc is None:
        return []
    start = _date(pc.cycle_start_date or "") or (_date(rw.today) if rw else None)
    end = _date(pc.next_paycheck_date)
    if start is None or end is None:
        return []
    today = (_date(rw.today) if rw else None) or start
    m = d.mask
    span = f"{fmt_day(start.strftime('%Y-%m-%d'))} → {fmt_day(pc.next_paycheck_date)}"
    # Shorten the label before ever cutting off the date range.
    title = next(
        (t for t in (f"Cycle Action: Bills Due Before Paycheck ({span})", f"Bills Due Before Paycheck ({span})", f"Cycle ({span})", span) if len(t) <= width),
        span,
    )
    lines: List[Line] = [fit([(title, "yellow+b")], width)]

    all_bills = sorted(list(getattr(pc, "paid_bills", [])) + list(pc.bills), key=lambda b: b["due_date"])
    due_by_date: Dict[str, int] = {}
    paid_by_date: Dict[str, int] = {}
    for b in all_bills:
        bucket = due_by_date if b.get("status") == "due" else paid_by_date
        if b["amount_cents"] > 0:
            bucket[b["due_date"]] = bucket.get(b["due_date"], 0) + b["amount_cents"]

    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for b in all_bills:
        by_date.setdefault(b["due_date"], []).append(b)
    detail_rows: List[Line] = []
    date_w, total_w = 11, 11
    indent = date_w + total_w + 1
    item_w = max(10, width - indent)
    detail_rows.append(fit([
        (rpad(fmt_day(start.strftime("%Y-%m-%d")), date_w), "sub"),
        (rpad("payday", total_w), "green+b"),
        (" ", "fg"),
        ("cycle starts", "dim"),
    ], width))
    for day in sorted(by_date):
        group = by_date[day]
        due_total = sum(b["amount_cents"] for b in group if b.get("status") == "due")
        paid_total = sum(b["amount_cents"] for b in group if b.get("status") != "due")
        if due_total > 0:
            total: Seg = (lpad(money(due_total, m), total_w - 1) + " ", "yellow+b")
        elif paid_total > 0:
            total = (lpad("✓ " + money(paid_total, m), total_w - 1) + " ", "green")
        else:
            total = (lpad("✓ paid", total_w - 1) + " ", "green")
        items: List[Line] = []
        for b in group:
            name = clean_name(b["name"])
            if b.get("status") == "due":
                items.append([(name + " ", "fg"), (money(b["amount_cents"], m), "yellow")])
            elif due_total > 0 and b["amount_cents"] > 0:
                # Mixed day: say which items are already paid, since the total only counts what is still due.
                items.append([(name + " ", "dim"), (money(b["amount_cents"], m), "green"), (" paid", "dim")])
            elif b["amount_cents"] > 0:
                items.append([(name + " ", "dim"), (money(b["amount_cents"], m), "green")])
            else:
                items.append([(name, "dim")])
        wrapped = _wrap_items(items, item_w)
        past = bool(_date(day) and _date(day) < today)
        first = True
        for chunk in wrapped:
            head: Line = (
                [(rpad(fmt_day(day), date_w), "dim" if past else "sub"), total, (" ", "fg")]
                if first
                else [(" " * indent, "fg")]
            )
            detail_rows.append(fit(head + chunk, width))
            first = False
    detail_rows.append(fit([
        (rpad(fmt_day(pc.next_paycheck_date), date_w), "sub"),
        (rpad("payday", total_w), "green+b"),
        (" ", "fg"),
        (money(pc.paycheck_amount_cents, m, signed=True), "green"),
    ], width))
    total_due = sum(due_by_date.values())
    total_paid = sum(paid_by_date.values())
    summary: Line = [
        ("Still due ", "dim"), (money(total_due, m), "yellow+b"),
        ("   Paid this cycle ", "dim"), (money(total_paid, m), "green"),
    ]

    track = runway_track(start, end, today, due_by_date, paid_by_date, width, m)
    needed_with_chart = 1 + len(track) + 1 + len(detail_rows) + 1
    if track and needed_with_chart <= rows:
        lines += track + [[]]
    lines += detail_rows
    if len(lines) + 1 < rows:
        lines += [summary]
    return lines


def runway_box(d: FinanceViewData, width: int) -> List[Line]:
    rw = d.runway
    inner = width - 4
    if rw is None:
        return box("RUNWAY", [[("Runway needs a paycheck plan.", "dim")]], width)
    start = _date(rw.today)
    end = _date(rw.next_paycheck)
    body: List[Line] = []
    if start and end:
        track = runway_track(start, end, start, dict(rw.bill_marks), {}, inner, d.mask)
        if track:
            body += [[]] + track + [[]]
    bills_total = money(rw.bills_due_cents, d.mask)
    body.append([(money(rw.checking_cents, d.mask), "fg+b"), (" in checking today.", "dim")])
    if rw.bill_marks:
        last = fmt_day(rw.bill_marks[-1][0], comma=True)
        body.append([("Bills take ", "dim"), (bills_total, "yellow+b"), (f" by {last},", "dim")])
        body.append([("leaving ", "dim"), (money(rw.left_cents, d.mask), "snow+b"), (f" for {_plural(rw.days_left, 'day')}.", "dim")])
    else:
        body.append([("No bills due before payday.", "dim")])
    return box("RUNWAY", body, width, trailing="checking")


def accounts_lines(d: FinanceViewData, width: int) -> List[Line]:
    m = d.mask
    accounts: Line = [
        (" Checking ", "dim"), (money(d.checking_cents, m), "fg+b"),
        ("     HYSA ", "dim"), (money(d.savings_cents, m), "fg+b"),
        ("     Visa ", "dim"), (money(d.visa_cents, m), "fg+b"),
        ("     Net worth ", "dim"), (money(d.net_worth_cents, m), "fg+b"),
    ]
    lines = [fit(accounts, width)]
    goals = d.goals[:2]
    name_w = min(16, max([len(clean_name(g.name)) for g in goals] + [8]))
    bar_w = 18 if width >= WIDE_MIN_COLS else 10
    for g in goals:
        pct = "**%" if m else f"{g.progress_pct:.0f}%"
        detail = f"{money(g.saved_cents, m, whole=True)} of {money(g.target_cents, m, whole=True)}"
        line: Line = [(" " + rpad(clean_name(g.name), name_w) + " ", "dim"), *progress_bar(g.progress_pct, bar_w)]
        line += [(lpad(pct, 6) + "  ", "fg"), (detail, "dim")]
        lines.append(fit(line, width))
    return lines


def bills_box(d: FinanceViewData, width: int, max_body: int) -> List[Line]:
    rw = d.runway
    pc = d.paycheck
    earlier_paid = list(getattr(pc, "paid_bills", [])) if pc else []
    if pc is None or not (pc.bills or earlier_paid):
        return box("BILLS BEFORE PAYDAY", [[("No bills before payday.", "green")]], width)
    inner = width - 4
    name_w = max(8, inner - 12 - 9 - 9)
    header = rpad("DATE", 12) + rpad("BILL", name_w) + lpad("AMOUNT", 9) + lpad("LEFT", 9)
    body: List[Line] = [[(header, "dim")]]
    running = rw.checking_cents if rw else 0
    paid: List[Dict[str, Any]] = list(earlier_paid)
    for b in pc.bills:
        if b.get("status") != "due":
            paid.append(b)
            continue
        running -= b["amount_cents"]
        name = clean_name(b["name"]) + (" (est.)" if b.get("is_estimate") else "")
        body.append([
            (rpad(fmt_day(b["due_date"]), 12), "sub"),
            (rpad(name, name_w), "fg"),
            (lpad(plain(b["amount_cents"], d.mask), 9), "yellow"),
            (lpad(plain(running, d.mask), 9), "sub"),
        ])
    if paid:
        body.append([("─" * inner, "line")])
        for b in paid:
            body.append([
                (rpad(fmt_day(b["due_date"]), 12), "dim"),
                (rpad(clean_name(b["name"]), name_w), "dim"),
                (lpad(f"paid {plain(b['amount_cents'], d.mask)}" if b["amount_cents"] > 0 else "paid", 9), "green"),
            ])
    body = body[:max_body]
    due = rw.bills_due_cents if rw else 0
    return box("BILLS BEFORE PAYDAY", body, width, trailing=f"{money(due, d.mask)} due", min_body=min(max_body, 9))


def recent_days_box(d: FinanceViewData, width: int, body_rows: int) -> Tuple[List[Line], Dict[int, int], int]:
    """Returns (box lines, {line index within box: day index}, scroll offset used)."""
    rw = d.runway
    days = d.days
    inner = width - 4
    budget = rw.per_day_cents if rw and rw.per_day_cents > 0 else 0
    month = (rw.today[:7] if rw else datetime.now().strftime("%Y-%m"))
    month_label = (_date(month + "-01") or datetime.now()).strftime("%b")
    trailing = f"{month_label}: {money(rw.month_spent_cents, d.mask, whole=True)} spent" if rw else ""
    if not days:
        body = [[("No days logged yet.", "dim")], [("Press a to log a day.", "dim")]]
        return box("RECENT DAYS", body, width, trailing), {}, 0

    list_rows = max(1, min(MAX_RECENT_ROWS, body_rows - 2))
    sel = max(0, min(d.selected_day_idx, len(days) - 1))
    offset = max(0, min(d.day_scroll_offset, max(0, len(days) - list_rows)))
    if sel < offset:
        offset = sel
    elif sel >= offset + list_rows:
        offset = sel - list_rows + 1
    visible = days[offset : offset + list_rows]
    peak = max([x["outflow_cents"] for x in visible] + [1])
    bar_w = max(6, min(22, inner - 12 - 9 - 14))
    body: List[Line] = []
    hits: Dict[int, int] = {}
    for i, day in enumerate(visible):
        idx = offset + i
        out = day["outflow_cents"]
        filled = max(1, round(out / peak * bar_w)) if out > 0 else 0
        over = budget > 0 and out > budget
        notes: Line = []
        if day.get("savings_cents", 0) > 0:
            notes.append((("+**" if d.mask else f"+{round(day['savings_cents'] / 100):,}") + " sav", "mag"))
        if day.get("inflow_cents", 0) > 0:
            notes.append(((" " if notes else "") + ("+**" if d.mask else f"+{round(day['inflow_cents'] / 100):,}") + " pay", "green"))
        row: Line = [
            (rpad(fmt_day(day["date"]), 12), "snow+b" if idx == sel else "sub"),
            ("▇" * filled, "yellow" if over else "blue"),
            (" " * (bar_w - filled), "fg"),
            (lpad(plain(out, d.mask), 9), "snow+b" if idx == sel else "fg"),
            ("  ", "fg"),
            *notes,
        ]
        if idx == sel:
            row = restyle(fit(row, inner), "sel")
        hits[len(body) + 1] = idx  # +1 for the box's top edge
        body.append(row)
    body.append([])
    legend: Line = []
    if budget > 0:
        legend += [("▇", "yellow"), (f" over {money(budget, d.mask)}/day   ", "dim"), ("▇", "blue"), (" under", "dim"), ("   ", "fg")]
    legend.append(("↵ open day", "dim"))
    body.append(legend)
    return box("RECENT DAYS", body, width, trailing), hits, offset


def build_overview(d: FinanceViewData, cols: int, rows: int) -> Rendered:
    """Main Finances screen. `rows` is the number of lines available."""
    lines: List[Line] = []
    day_rows: Dict[int, int] = {}
    offset = d.day_scroll_offset

    if cols >= WIDE_MIN_COLS:
        left_w = (cols - 2) // 2
        right_w = cols - 2 - left_w
        top = hcat([(hero_box(d, left_w), left_w), (runway_box(d, right_w), right_w)])
        lines += top + [[]]
        lines += accounts_lines(d, cols)
        lines.append([])
        remaining = max(6, rows - len(lines))
        bills = bills_box(d, left_w, max_body=remaining - 2)
        recent, hits, offset = recent_days_box(d, right_w, body_rows=remaining - 2)
        base = len(lines)
        lines += hcat([(bills, left_w), (recent, right_w)])
        day_rows = {base + i: idx for i, idx in hits.items()}
    else:
        lines += hero_box(d, cols)
        lines += accounts_lines(d, cols)
        lines.append([])
        remaining = rows - len(lines)
        bills_rows = min(9, max(3, remaining // 2))
        bills = bills_box(d, cols, max_body=bills_rows)
        lines += bills
        remaining = rows - len(lines)
        if remaining >= 6:
            recent, hits, offset = recent_days_box(d, cols, body_rows=remaining - 2)
            base = len(lines)
            lines += recent
            day_rows = {base + i: idx for i, idx in hits.items()}
    return Rendered(lines=lines[:rows], day_rows={k: v for k, v in day_rows.items() if k < rows}, day_scroll_offset=offset)


# ---------------------------------------------------------------------------
# Day screen (items of one day)
# ---------------------------------------------------------------------------


def build_day(d: FinanceViewData, cols: int, rows: int) -> Rendered:
    if not d.days or not (0 <= d.selected_day_idx < len(d.days)):
        return Rendered(lines=[[("No day selected. Press esc to go back.", "dim")]])
    day = d.days[d.selected_day_idx]
    m = d.mask
    head: Line = [
        (" ‹ esc   ", "dim"),
        (fmt_day(day["date"], comma=True), "snow+b"),
        (f"   Spent {money(day['outflow_cents'], m)}", "fg"),
        (f"   Inflow {money(day['inflow_cents'], m)}", "fg"),
    ]
    if day.get("savings_cents", 0) > 0:
        head.append((f"   Saved {money(day['savings_cents'], m)}", "mag"))
    items = day["items"]
    inner = cols - 4
    desc_w = max(8, inner - 2 - 12 - 14 - 12)
    body: List[Line] = [[("  " + rpad("ACCOUNT", 12) + rpad("CATEGORY", 14) + rpad("DESCRIPTION", desc_w) + lpad("AMOUNT", 12), "dim")]]
    item_rows: Dict[int, int] = {}
    sel = min(d.selected_item_idx, max(0, len(items) - 1))
    for i, it in enumerate(items[: max(1, rows - 6)]):
        amt = it["amount_cents"]
        if it.get("is_income"):
            amt_text, amt_style = money(amt, m, signed=True), "green"
        elif it.get("is_savings"):
            amt_text, amt_style = money(-amt, m, signed=True), "mag"
        else:
            amt_text, amt_style = money(-amt, m, signed=True), "fg"
        row: Line = [
            ("  ", "fg"),
            (rpad(f"[{it['account'][:8]}]", 12), "dim"),
            (rpad(str(it["category"])[:13], 14), "sub"),
            (rpad(str(it["payee"] or it["memo"] or "")[:desc_w], desc_w), "fg"),
            (lpad(amt_text, 12), amt_style),
        ]
        if i == sel:
            row = restyle(fit(row, inner), "sel")
        item_rows[len(body) + 1] = i
        body.append(row)
    if not items:
        body.append([("  No transactions on this day.", "dim")])
    lines: List[Line] = [head, []]
    base = len(lines)
    lines += box("DAY", body, cols, trailing=f"{len(items)} item{'s' if len(items) != 1 else ''}")
    return Rendered(lines=lines[:rows], item_rows={base + k: v for k, v in item_rows.items() if base + k < rows})


# ---------------------------------------------------------------------------
# The math screen
# ---------------------------------------------------------------------------


def build_math(d: FinanceViewData, cols: int, rows: int) -> Rendered:
    rw = d.runway
    ov = d.overview
    m = d.mask

    def row(label: str, value: str, width: int, style: str = "fg", label_style: str = "sub") -> Line:
        return [(rpad(label, width - 12), label_style), (lpad(value, 12), style)]

    def rule(width: int) -> Line:
        return [("─" * width, "line")]

    wide = cols >= WIDE_MIN_COLS
    left_w = (cols - 2) // 2 if wide else cols
    right_w = cols - 2 - left_w if wide else cols
    li = left_w - 4
    ri = right_w - 4

    period: List[Line] = []
    if rw:
        period += [
            row("Checking today", plain(rw.checking_cents, m), li),
            row("Bills before payday", plain(-rw.bills_due_cents, m), li, "yellow"),
            rule(li),
            row("Left until payday", plain(rw.left_cents, m), li, "snow+b"),
            row(f"Per day, {_plural(rw.days_left, 'day')}", plain(rw.per_day_cents, m), li, "cyan+b"),
            [],
            row("Grocery, dining, gas allowance", plain(-rw.allowance_cents, m), li, "dim"),
            row("If allowances are fully spent", plain(rw.after_allowances_cents, m), li,
                "yellow+b" if rw.after_allowances_cents < 0 else "green+b"),
            [],
        ]
        if rw.after_allowances_cents < 0:
            period.append([("Your allowances add up to more than is left.", "dim")])
            if rw.per_day_cents > 0:
                period.append([(f"Spending under {money(rw.per_day_cents, m)}/day keeps you above $0.", "dim")])
        else:
            period.append([("Your allowances fit within what is left.", "dim")])
    else:
        period.append([("No paycheck plan is set up yet.", "dim")])

    month: List[Line] = []
    if ov:
        def budget(name: str, actual: int, planned: int) -> Line:
            left = planned - actual
            style = "yellow" if planned > 0 and left < planned * 0.2 else "fg"
            return row(f"{name}  {plain(actual, m)} of {plain(planned, m).split('.')[0]}", f"{plain(left, m)} left", ri, style)

        month += [
            row("Income", plain(ov.income_cents, m), ri),
            row("Saved", plain(ov.savings_target_cents, m), ri, "mag"),
            row("Fixed bills + PG&E estimate", plain(ov.all_bills_cents, m), ri),
            rule(ri),
            budget("Groceries", ov.groceries_actual_cents, ov.groceries_planned_cents),
            budget("Gas", ov.gas_actual_cents, ov.gas_planned_cents),
        ]
        if ov.dining_planned_cents > 0:
            month.append(budget("Dining", ov.dining_actual_cents, ov.dining_planned_cents))
        month += [
            rule(ri),
            row("Flex left this month", plain(ov.remaining_cushion_cents, m, signed=True), ri,
                "green+b" if ov.remaining_cushion_cents >= 0 else "yellow+b"),
            [],
            [("Flex is monthly budget not yet spent. It is not", "dim")],
            [("the same as the money left until payday.", "dim")],
        ]
    else:
        month.append([("No monthly overview yet.", "dim")])

    month_name = ((rw and _date(rw.today)) or datetime.now()).strftime("%B").upper()
    a = box("THIS PAY PERIOD", period, left_w, min_body=12)
    b = box(month_name, month, right_w, min_body=12)
    lines = hcat([(a, left_w), (b, right_w)]) if wide else a + [[]] + b
    return Rendered(lines=lines[:rows])
