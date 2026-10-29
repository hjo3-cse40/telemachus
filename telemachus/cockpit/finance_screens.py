"""Money sub-screens: Day, Math and Bills (breadcrumb 'Money ▸ ...', esc goes back).

Pure builders like finance_view.build_overview: (FinanceViewData, cols, rows) -> Rendered, with
the 2-cell gutter included. Each one fits any width and height: optional parts drop out in a fixed
order when rows run short, and lists scroll so the selected row stays visible.
"""
from __future__ import annotations

import calendar
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from telemachus.cockpit.finance_view import (
    BAR,
    GUTTER,
    NARROW_MAX,
    VIEW_PREFS,
    WIDE_MIN,
    FinanceViewData,
    Rendered,
    _date,
    _finish,
    _place,
    _plural,
    _runs,
    _status,
    _today,
    allowance_pace,
    card_overdue,
    bar_peak,
    blank,
    clean_name,
    crumb,
    daily_allowance,
    day_entries,
    day_row,
    day_spent,
    day_totals,
    days_list_width,
    fmt_day,
    hcat,
    is_card,
    item_account,
    item_kind,
    marked,
    money,
    notes_room,
    num,
    over_allowance,
    pack,
    pack_steady,
    short_bill_name,
)
from telemachus.cockpit.theme import GLYPHS, Line, cell_width, clip, fit, line_width, lpad, rpad, section, spread

AMT_W = 12  # amount column of the calculation rows


def _stack(groups: List[List[Line]], gaps: bool = True) -> List[Line]:
    out: List[Line] = []
    for g in groups:
        if not g:
            continue
        if out and gaps:
            out.append(blank())
        out += g
    return out


def _wrap_text(text: str, width: int, style: str = "muted") -> List[Line]:
    words = [([(w, style)], [(" ", "text")]) for w in text.split()]
    return pack(words, width)


# ---------------------------------------------------------------------------
# Day screen
# ---------------------------------------------------------------------------


def _item_cells(it: Dict[str, Any], mask: bool) -> Tuple[str, str, str, str, str]:
    """(account, category, description, amount text, amount style) for one entry."""
    kind = item_kind(it)
    amt = int(it.get("amount_cents", 0) or 0)
    account = item_account(it).lower() or "checking"
    desc = clean_name(it.get("payee") or it.get("memo") or "")
    category = clean_name(it.get("category", ""))
    if kind == "income":
        return account, category, desc, num(amt, mask, signed=True), "green"
    if kind == "card_payment":
        # Leaves checking but is not spending: the day total leaves it out.
        return account, "Card payment", f"{GLYPHS['swap']} Visa payment", num(-amt, mask, signed=True), "muted"
    if kind == "transfer":
        return account, category or "Savings", f"{GLYPHS['swap']} {desc or 'savings'}", num(-amt, mask, signed=True), "muted"
    return account, category, desc, num(-amt, mask, signed=True), "muted" if mask else "text"


def _day_summary(d: FinanceViewData, day: Dict[str, Any], width: int) -> List[Line]:
    t = day_totals(day)
    n = len(day.get("items") or [])
    allowance = daily_allowance(d)

    def items(m: bool) -> List[Tuple[Line, Line]]:
        dot: Line = [("  " + GLYPHS["dot"] + "  ", "muted")]
        out: List[Tuple[Line, Line]] = [([("spent " + money(t["spent"], m), "text+b")], [])]
        out.append(([(_plural(n, "item"), "muted")], dot))
        # Only groceries, dining and gas count against the allowance (not bills or subscriptions).
        counted = int(day.get("allowance_spent_cents", t["spent"]) or 0)
        if allowance > 0 and counted > 0:
            over = counted > allowance
            word = "over" if over else "under"
            # When bills or other spending are in the day, say which part counts.
            part = f" ({money(counted, m)} counts)" if counted != t["spent"] else ""
            out.append(([(f"{word} the {money(allowance, m)} daily allowance{part}", "yellow" if over else "green")], dot))
        if t["income"] > 0:
            out.append(([(f"{money(t['income'], m, signed=True)} in", "green")], dot))
        if t["card_spend"] > 0:
            out.append(([(f"{money(t['card_spend'], m)} on the Visa", "muted")], dot))
        if t["card_payment"] > 0:
            out.append(([(f"{GLYPHS['swap']} {money(t['card_payment'], m)} Visa payment", "muted")], dot))
        if t["transfer"] > 0:
            out.append(([(f"{GLYPHS['swap']} {money(t['transfer'], m)} to savings", "muted")], dot))
        return out

    return pack_steady(items, d.mask, width)


ITEMS_MIN = 56  # narrowest the ITEMS table gets in the two-column Day screen
ITEMS_NOTE: Line = [("12 items", "muted")]  # room kept after each nearby day for its item count


def _item_columns(day: Dict[str, Any], width: int, natural: bool = False) -> Tuple[int, int, int, int]:
    """(account, category, description, amount) widths. `natural`: the description is only as wide
    as the longest one needs, so a wide pane does not leave a gap before the amounts."""
    inner = width - len(GUTTER)
    acct_w = 11 if inner >= 60 else 10
    # The category column keeps 2 blank cells before the description ('Discretionary' is 13).
    cat_w = 15 if inner >= 52 else 0
    amt_w = 12
    desc_w = max(6, min(60, inner - acct_w - cat_w - amt_w))
    if natural:
        longest = max([cell_width(_item_cells(it, False)[2]) for it in day.get("items") or []] + [0])
        need = max(longest + 2, ITEMS_MIN - len(GUTTER) - acct_w - cat_w - amt_w)
        desc_w = max(6, min(desc_w, need))
    return acct_w, cat_w, desc_w, amt_w


def items_width(day: Dict[str, Any], width: int) -> int:
    """Natural width of the ITEMS table (gutter included), at most `width` and TABLE_MAX."""
    return min(width, TABLE_MAX, len(GUTTER) + sum(_item_columns(day, width, natural=True)))


def _item_table(d: FinanceViewData, day: Dict[str, Any], width: int, list_rows: int,
                header: bool, total: bool, natural: bool = False) -> Tuple[List[Line], Dict[int, int]]:
    m = d.mask
    items = day.get("items") or []
    acct_w, cat_w, desc_w, amt_w = _item_columns(day, width, natural)
    table_w = len(GUTTER) + acct_w + cat_w + desc_w + amt_w
    lines: List[Line] = []
    if header:
        head = rpad("ACCOUNT", acct_w) + (rpad("CATEGORY", cat_w) if cat_w else "") + rpad("DESCRIPTION", desc_w) + lpad("AMOUNT", amt_w)
        lines.append(fit([(GUTTER + head, "muted")], width))
    hits: Dict[int, int] = {}
    if not items:
        lines.append(fit([(GUTTER + "No entries on this day. Press a to add some.", "muted")], width))
    sel = max(0, min(d.selected_item_idx, len(items) - 1)) if items else 0
    list_rows = max(1, list_rows)
    offset = 0 if sel < list_rows else sel - list_rows + 1
    for i in range(offset, min(len(items), offset + list_rows)):
        acct, cat, desc, amt, style = _item_cells(items[i], m)
        selected = i == sel
        content: Line = [(rpad(acct, acct_w), "muted")]
        if cat_w:
            content.append((rpad(rpad(cat, cat_w - 2), cat_w), "text"))
        content += [(rpad(desc, desc_w), "text+b" if selected else "text"), (lpad(amt, amt_w), style)]
        hits[len(lines)] = i
        lines.append(marked(content, selected, width))
    if total and items:
        t = day_totals(day)
        lines.append(fit([(GUTTER + "─" * (table_w - len(GUTTER)), "dim")], width))
        more = f"  {GLYPHS['dot']}  {len(items) - list_rows} more, j/k to scroll" if len(items) > list_rows else ""
        if not more and (t["card_payment"] or t["transfer"]) and table_w >= 60:
            more = f"  {GLYPHS['dot']}  {GLYPHS['swap']} rows are moves, not spending"
        left: Line = [(GUTTER + _plural(len(items), "item"), "text+b"), (more, "muted")]
        # Income and moves are listed above but not in this total, so it says what it adds up.
        right: Line = [("spent  ", "muted"),
                       (num(-t["spent"], m, signed=True) if t["spent"] else num(0, m), "text+b")]
        lines.append(fit(spread(left, right, table_w), width))
    return lines, hits


def _nearby(d: FinanceViewData, width: int, n: int) -> List[Line]:
    """Days around the open day, newest first (the open day is marked)."""
    days = d.days
    sel = d.selected_day_idx
    entries = day_entries(d)
    today = _today(d)
    sel_date = _date(days[sel].get("date"))
    if sel_date is not None and sel_date >= today:
        entries.insert(0, ("future", (sel_date + timedelta(days=1)).strftime("%Y-%m-%d"), 0))
    pos = next((k for k, e in enumerate(entries) if e[0] == "day" and e[1] == sel), 0)
    start = max(0, min(pos - 2, len(entries) - n))
    window = entries[start: start + n]
    peak = bar_peak(d)
    # Room for the longest day's notes plus "12 items" after the amount (bars keep MIN_BAR cells).
    notes_w = notes_room(d, width, ITEMS_NOTE)
    out: List[Line] = []
    for kind, ref, cnt in window:
        if kind == "day":
            count = len(days[ref].get("items") or [])
            out.append(day_row(d, days[ref], width, ref == sel, peak, [(_plural(count, "item"), "muted")], notes_w=notes_w))
        elif kind == "future":
            out.append(marked([(rpad(fmt_day(ref), 12), "muted"), ("not yet", "muted")], False, width))
        elif kind == "today":
            out.append(marked([(rpad(fmt_day(ref), 12), "text"), (GLYPHS["dot"] + "  nothing logged yet today", "muted")], False, width))
        else:
            text = GLYPHS["dot"] + "  no spending" + (f" for {cnt} days" if cnt > 1 else "")
            out.append(marked([(rpad(fmt_day(ref), 12), "muted"), (text, "muted")], False, width))
    return out


def _day_note(d: FinanceViewData, allowance: int, width: int) -> List[Line]:
    rw = d.runway
    dot: Line = [("  " + GLYPHS["dot"] + "  ", "muted")]
    return pack_steady(lambda m: [
        ([(f"Daily allowance {money(allowance, m)} (groceries, dining, gas)", "muted")], []),
        ([(f"cash after bills {money(rw.per_day_cents if rw else 0, m)} a day", "muted")], dot)], d.mask, width)


def build_day(d: FinanceViewData, cols: int, rows: int) -> Rendered:
    """One day's entries, with the nearby days under them."""
    width, height = max(20, cols), max(1, rows)
    if not d.days or not (0 <= d.selected_day_idx < len(d.days)):
        lines = [crumb("Day", width, d.mask), blank(), [(GUTTER + "No day selected. Press esc to go back.", "muted")]]
        return Rendered(lines=_finish(lines, width, height))
    day = d.days[d.selected_day_idx]
    items = day.get("items") or []
    summary = _day_summary(d, day, width)
    allowance = daily_allowance(d)
    note: List[Line] = []
    if d.runway is not None and allowance > 0:
        note = _day_note(d, allowance, width)

    near_need = days_list_width(d, ITEMS_NOTE)
    if width >= WIDE_MIN and width - 2 - near_need >= ITEMS_MIN:
        # Two columns: the entries (left, as wide as the table needs) and the nearby days (right,
        # everything left over, so the day bars get room on a wide pane). Only when the nearby
        # days get their full bars and notes ('4 items' included); otherwise one column, as at 95.
        right_min = min(width - 2 - ITEMS_MIN, max(near_need, min(70, max(56, int((width - 2) * 0.45)))))
        left_w = min(width - 2 - right_min, max(ITEMS_MIN, items_width(day, width - 2 - right_min)))
        right_w = width - 2 - left_w
        top = [crumb(fmt_day(day.get("date", "")), width, d.mask), blank()] + _day_summary(d, day, width)
        top.append(blank())
        room = height - len(top) - 1 - 1 - (2 if items else 0)
        tgap = room - 1 >= max(1, len(items))  # a blank row after each title when the items still fit
        gap_rows = [blank()] if tgap else []
        room -= len(gap_rows)
        table, hits = _item_table(d, day, left_w, max(1, min(max(1, len(items)), room)), True, True, natural=True)
        left = [section("ITEMS", left_w)] + gap_rows + table
        note_l = _day_note(d, allowance, left_w) if note else []
        if note_l and len(top) + len(left) + 1 + len(note_l) <= height:
            left += [blank()] + note_l
        right = [section("NEARBY DAYS", right_w)] + gap_rows + _nearby(d, right_w, max(2, height - len(top) - 1 - len(gap_rows)))
        base = len(top) + 1 + len(gap_rows)
        return Rendered(lines=_finish(top + hcat(left, left_w, right, right_w), width, height),
                        item_rows={base + k: v for k, v in hits.items() if base + k < height})

    # (title gaps, crumb gap, header, total, nearby, note, section gaps) from roomy to tight. Blank
    # rows after titles (as in the mockups) go after the note but before anything else.
    levels = [
        (True, True, True, True, True, True, True),
        (True, True, True, True, True, False, True),
        (False, True, True, True, True, False, True),
        (False, True, True, True, False, False, True),
        (False, False, True, True, False, False, True),
        (False, False, False, True, False, False, True),
        (False, False, False, True, False, False, False),
        (False, False, False, False, False, False, False),
    ]
    for li, (tgap, crumb_gap, header, total, nearby, show_note, gaps) in enumerate(levels):
        top: List[Line] = [crumb(fmt_day(day.get("date", "")), width, d.mask)]
        if crumb_gap:
            top.append(blank())
        top += summary
        if gaps:
            top.append(blank())
        top.append(section("ITEMS", width))
        if tgap:
            top.append(blank())
        fixed = len(top) + (1 if header else 0) + (2 if total and items else 0)
        tail: List[Line] = []
        if show_note and note:
            tail = [blank()] + note
        near_min = (4 + (1 if tgap else 0)) if nearby else 0  # blank + title (+ blank) + at least 2 days
        room = height - fixed - len(tail) - near_min
        need_items = max(1, len(items))
        if room >= need_items or li == len(levels) - 1:
            list_rows = max(1, min(need_items, room))
            table, hits = _item_table(d, day, width, list_rows, header, total)
            base = len(top)
            lines = top + table
            if nearby:
                head = [blank(), section("NEARBY DAYS", width)] + ([blank()] if tgap else [])
                n = height - len(lines) - len(tail) - len(head)
                lines += head + _nearby(d, width, max(2, n))
            lines += tail
            return Rendered(lines=_finish(lines, width, height),
                            item_rows={base + k: v for k, v in hits.items() if base + k < height})
    return Rendered(lines=[])  # pragma: no cover


# ---------------------------------------------------------------------------
# Math screen
# ---------------------------------------------------------------------------


def _label_w(width: int, edge: Optional[int] = None) -> int:
    """Label column of the calculation rows. With `edge`, the amounts end on that column."""
    if edge is not None:
        return max(12, min(width, edge) - len(GUTTER) - 2 - AMT_W)
    return max(12, min(40, width - len(GUTTER) - 2 - AMT_W))


def calc_edge(width: int) -> int:
    """The column where calculation amounts end when no edge is given."""
    return len(GUTTER) + 2 + _label_w(width) + AMT_W


def _calc(op: str, label: str, value: str, width: int, value_style: str = "text",
          bold: bool = False, note: Optional[Tuple[str, str]] = None, edge: Optional[int] = None) -> Line:
    lw = _label_w(width, edge)
    row: Line = [(GUTTER + (op or " ") + " ", "muted"), (rpad(label, lw), "text+b" if bold else "muted"),
                 (lpad(value, AMT_W), value_style + ("+b" if bold and "+" not in value_style else ""))]
    if note and line_width(row) + 3 + cell_width(note[0]) <= width:
        row += [("   " + note[0], note[1])]
    return fit(row, width)


def _rule(width: int, edge: Optional[int] = None) -> Line:
    return fit([(" " * (len(GUTTER) + 2 + _label_w(width, edge)), "text"), ("─" * AMT_W, "dim")], width)


def _neg_style(cents: int, mask: bool, good: str = "text") -> str:
    if mask:
        return "muted"
    return "red" if cents < 0 else good


def _bill_names_note(d: FinanceViewData) -> str:
    pc = d.paycheck
    parts = []
    for b in getattr(pc, "bills", []) or []:
        if b.get("status") == "due" and b.get("amount_cents", 0) > 0:
            late = " overdue" if b.get("overdue_since") else ""
            parts.append(f"{short_bill_name(b.get('name', ''), lower=False)} {num(b['amount_cents'], d.mask)}{late}")
    return ", ".join(parts)


def _pay_period(d: FinanceViewData, width: int, inner_gaps: bool, card_line: bool = True) -> List[Line]:
    rw = d.runway
    m = d.mask
    if rw is None and getattr(d, "setup_error", None):
        from telemachus.cockpit.finance_view import setup_error_lines

        return setup_error_lines(d.setup_error, width)
    if rw is None:
        return [fit([(GUTTER + "No paycheck plan yet. Add paycheck_plan to setup.yaml.", "muted")], width)]
    val = "muted" if m else "text"
    names = _bill_names_note(d)
    lines = [
        _calc("", "Checking today", num(rw.checking_cents, m), width, val),
        _calc("−", "Bills before payday", num(rw.bills_due_cents, m), width, val, note=(names, "muted") if names else None),
        _rule(width),
        _calc("=", "Left until payday", num(rw.left_cents, m), width, _neg_style(rw.left_cents, m), bold=True),
        _calc("÷", _plural(rw.days_left, "day"), num(rw.per_day_cents, m), width, val, note=("a day", "muted")),
    ]
    if inner_gaps:
        lines.append(blank())
    lines += [
        _calc("−", "Grocery, dining, gas allowance", num(rw.allowance_cents, m), width, val),
        _rule(width),
        _calc("=", "If allowances are fully spent", num(rw.after_allowances_cents, m), width,
              _neg_style(rw.after_allowances_cents, m), bold=True),
    ]
    if inner_gaps:
        lines.append(blank())
    if rw.after_allowances_cents < 0:
        lines += pack([([(GLYPHS["over"] + " Your allowances add up to more than is left until payday.", "yellow")], [])], width)
        if rw.per_day_cents > 0:
            lines += pack([([(f"Spending under {money(rw.per_day_cents, m)} a day keeps you above $0.", "muted")], [])], width)
    else:
        lines += pack([([(GLYPHS["done"] + " Your allowances fit in what is left until payday.", "green")], [])], width)
    card = d.card or {}
    owed = int(card.get("owed_cents", 0) or 0)
    if card_line and card.get("status") == "owed" and owed > 0:
        soon = bool(card.get("due_before_payday"))
        if card_overdue(card, _today(d)):
            text = f"Visa: {money(owed, m)} owed, overdue since {fmt_day(card['next_due_date'])}."
        else:
            when = f", due {fmt_day(card['next_due_date'])}" if card.get("next_due_date") else ""
            text = f"Visa: {money(owed, m)} owed{when}" + (", before payday." if soon else ".")
        lines += pack([([((GLYPHS["over"] + " ") if soon else "", "yellow"), (text, "yellow" if soon else "muted")], [])], width)
    return lines


def _month_lines(d: FinanceViewData, width: int, inner_gaps: bool, budgets: bool = True) -> List[Line]:
    ov = (d.bills_breakdown or {}).get("overview") or d.overview
    m = d.mask
    if not ov:
        return [fit([(GUTTER + "No monthly overview yet.", "muted")], width)]
    val = "muted" if m else "text"
    left_for_spending = ov.income_cents - ov.savings_target_cents - ov.all_bills_cents
    lines = [
        _calc("", "Income", num(ov.income_cents, m), width, "muted" if m else "green"),
        _calc("−", "Plan to save", num(ov.savings_target_cents, m), width, val),
        _calc("−", "Fixed bills and PG&E estimate", num(ov.all_bills_cents, m), width, val),
        _rule(width),
        _calc("=", "Left for spending", num(left_for_spending, m), width, _neg_style(left_for_spending, m), bold=True),
    ]
    if not budgets:
        return lines
    if inner_gaps:
        lines.append(blank())
    for name, actual, planned in _budgets(ov):
        label = f"{rpad(name, 11)} {lpad(num(actual, m), 8)} of {num(planned, m)}"
        lines.append(_calc("", label, num(planned - actual, m), width, _neg_style(planned - actual, m)))
    lines += [
        _rule(width),
        _calc("=", "Flex left this month", num(ov.remaining_cushion_cents, m), width,
              _neg_style(ov.remaining_cushion_cents, m, "green"), bold=True),
    ]
    return lines


def _flex_note(d: FinanceViewData, flex: int, width: int) -> List[Line]:
    """Under a negative flex (Bills, and the wide overview): says it is the month's budgets, not the
    cash until payday, so it does not seem to contradict the until-payday numbers. It names no
    screen, since the wide overview shows it next to those numbers. Math does not need it: its
    WHY TWO NUMBERS block says the same."""
    if flex >= 0 or d.runway is None:
        return []
    for text in ("Over the month's budgets. The cash until payday is a separate number.",
                 "Budgets only, not the cash until payday.",
                 "Budgets only, not cash."):
        if len(GUTTER) + cell_width(text) <= width:
            return [fit([(GUTTER + text, "muted")], width)]
    return []


def _budgets(ov: Any) -> List[Tuple[str, int, int]]:
    out = [("Groceries", ov.groceries_actual_cents, ov.groceries_planned_cents),
           ("Gas", ov.gas_actual_cents, ov.gas_planned_cents)]
    if ov.dining_planned_cents > 0 or ov.dining_actual_cents > 0:
        out.append(("Dining", ov.dining_actual_cents, ov.dining_planned_cents))
    return out


def _month_name(d: FinanceViewData) -> str:
    ov = (d.bills_breakdown or {}).get("overview") or d.overview
    dt = _date(f"{getattr(ov, 'month', '')}-01") if ov else None
    return (dt or _today(d)).strftime("%B").upper()


def _period_trailing(d: FinanceViewData) -> str:
    rw = d.runway
    if rw is None:
        return ""
    start = getattr(d.paycheck, "cycle_start_date", "") or ""
    span = f"{fmt_day(start)} → {fmt_day(rw.next_paycheck)}, " if start else ""
    return f"{span}{_plural(rw.days_left, 'day')} left"


WHY_TEXT = ("Flex is what is left of this month's budgets. Left until payday is the cash in checking "
            "after bills. You can be over budget and still have cash.")


def _pace_lines(d: FinanceViewData, width: int, title_gap: bool) -> List[Line]:
    """How 'lately $X a day' and the on track / over status are worked out."""
    rw = d.runway
    if rw is None:
        return []
    m = d.mask
    val = "muted" if m else "text"
    today = _today(d)
    start = today - timedelta(days=6)
    spent = 0
    for x in d.days or []:
        dt = _date(x.get("date"))
        if dt is not None and start <= dt <= today:
            # The same sum compute_runway divides for 'lately' (bill payments left out).
            spent += int(x.get("pace_spent_cents", x.get("outflow_cents", 0)) or 0)
    lines: List[Line] = [section("PACE", width, "last 7 days")]
    if title_gap:
        lines.append(blank())
    lines += [
        _calc("", f"Spent {fmt_day(start.strftime('%Y-%m-%d'))} to {fmt_day(today.strftime('%Y-%m-%d'))}",
              num(spent, m), width, val),
        _calc("÷", "7 days", num(rw.pace_cents, m), width, val, bold=True, note=("a day", "muted")),
        _calc("", "Left per day until payday", num(rw.per_day_cents, m), width, val, note=("a day", "muted")),
    ]
    status = _status(d)

    def text(mk: bool) -> str:
        if rw.left_cents < 0:
            return "Bills before payday are more than checking."
        if rw.run_out_date:
            return f"At this pace checking reaches $0 by {fmt_day(rw.run_out_date)}."
        if rw.per_day_cents > 0 and rw.pace_cents > rw.per_day_cents:
            return "You spend more a day than is left a day."
        if over_allowance(d):
            return (f"Cash is fine, but groceries, dining and gas average {money(allowance_pace(d), mk)} a day, "
                    f"over the {money(daily_allowance(d), mk)} allowance.")
        return "You spend less a day than is left a day."

    def items(mk: bool) -> List[Tuple[Line, Line]]:
        words = text(mk).split()
        return [(status, [])] + [([(w, "muted")], [("  " if i == 0 else " ", "text")]) for i, w in enumerate(words)]

    if status:
        lines += pack_steady(items, m, width)
    return lines


def _fit(avail: int, gap: int, required: List[List[Line]], optional: List[Tuple[str, List[Line]]],
         flex: Any = None, flex_min: int = 3) -> Tuple[bool, Dict[str, List[Line]]]:
    """Picks blocks for one column of `avail` rows.

    Required blocks always go in (returns False when they do not fit). Optional blocks are taken in
    priority order while they fit. `flex` = (name, fn(rows) -> lines), or a list of them, share
    whatever rows are left, in order, each when at least `flex_min` rows are free.
    """
    used = sum(len(b) for b in required if b) + gap * max(0, sum(1 for b in required if b) - 1)
    ok = used <= avail
    chosen: Dict[str, List[Line]] = {}
    for name, block in optional:
        cost = len(block) + (gap if used else 0)
        if block and used + cost <= avail:
            chosen[name] = block
            used += cost
    flexes = [] if flex is None else (flex if isinstance(flex, list) else [flex])
    for name, fn in flexes:
        room = avail - used - (gap if used else 0)
        if room >= flex_min:
            block = fn(room)
            if block:
                chosen[name] = block
                used += len(block) + (gap if used else 0)
    return ok, chosen


def _paydays(d: FinanceViewData) -> List[str]:
    """Dates with a paycheck in the logged days, newest first."""
    pay = int(getattr(d.paycheck, "paycheck_amount_cents", 0) or 0)
    out = []
    for day in d.days or []:
        income = sum(int(it.get("amount_cents", 0) or 0) for it in day.get("items") or [] if item_kind(it) == "income")
        if income > 0 and (pay <= 0 or income * 2 >= pay):
            out.append(str(day.get("date", "")))
    return out


def _period_lines(d: FinanceViewData, width: int, max_rows: int, title_gap: bool) -> List[Line]:
    """PAY PERIODS: spending in this and the earlier pay periods, a day, to compare the pace."""
    paydays = sorted(set(_paydays(d)), reverse=True)
    if len(paydays) < 2 or max_rows < 3:
        return []
    m = d.mask
    today = _today(d)
    allowance = daily_allowance(d)
    spent_by_day = {str(x.get("date", "")): day_spent(x) for x in d.days or []}
    # The over/under color compares only allowance spending (groceries, dining, gas).
    allow_by_day = {str(x.get("date", "")): int(x.get("allowance_spent_cents", day_spent(x)) or 0)
                    for x in d.days or []}
    rows: List[Tuple[str, Optional[str], int, int, int, bool]] = []
    ends = [None] + paydays[:-1]
    for start_s, end_s in zip(paydays, ends):
        start = _date(start_s)
        if start is None:
            continue
        end = _date(end_s) if end_s else None
        last = (end - timedelta(days=1)) if end else today
        n = max(1, (last - start).days + 1)
        last_s = last.strftime("%Y-%m-%d")
        spent = sum(v for k, v in spent_by_day.items() if start_s <= k <= last_s)
        counted = sum(v for k, v in allow_by_day.items() if start_s <= k <= last_s)
        rows.append((start_s, end_s, n, spent, counted, end is None))
    if not rows:
        return []
    head: List[Line] = [section("PAY PERIODS", width)]
    if title_gap and max_rows >= 5:
        head.append(blank())
    lines = list(head)

    def short_day(value: str) -> str:
        dt = _date(value)
        return f"{dt:%b} {dt.day}" if dt else value

    def make(compact: bool, total: bool = True) -> List[Line]:
        day_fmt = short_day if compact else fmt_day
        labels = [f"{day_fmt(r[0])} → " + (day_fmt(r[1]) if r[1] else "today") for r in rows]
        label_w = max(cell_width(x) for x in labels) + 2
        out: List[Line] = []
        for label, (_, _, n, spent, counted, current) in zip(labels, rows[: max(1, max_rows - len(head))]):
            per = int(round(spent / n))
            over = allowance > 0 and int(round(counted / n)) > allowance
            days_text = _plural(n, "day") + (" so far" if current and not compact else "")
            row: Line = [(GUTTER + rpad(label, label_w), "text" if current else "muted"),
                         (rpad(days_text, 8 if compact else 14), "muted")]
            if total:
                row.append((lpad(num(spent, m), 9 if compact else 10), "muted" if m else "text"))
            row += [(lpad(num(per, m), 9 if compact else 10), "muted" if m else ("yellow" if over else "text")),
                    ("/day" if compact else " a day", "muted")]
            out.append(row)
        return out

    # Narrow column: short dates, then no period total, so the per-day number is never cut.
    for compact, total in ((False, True), (True, True), (True, False)):
        body = make(compact, total)
        if all(line_width(x) <= width for x in body):
            break
    lines += [fit(x, width) for x in body]
    return lines


def _card_math(d: FinanceViewData, width: int, title_gap: bool) -> List[Line]:
    """VISA: how the amount owed builds up since the last payment, and whether it is due before payday."""
    card = d.card
    if not card:
        return []
    m = d.mask
    val = "muted" if m else "text"
    status = str(card.get("status") or "clear")
    owed = int(card.get("owed_cents", 0) or 0)
    when = card.get("next_due_date")
    soon = bool(card.get("due_before_payday"))
    overdue = card_overdue(card, _today(d))
    trailing = (f"overdue since {fmt_day(when)}" if overdue else f"due {fmt_day(when)}") if when and status == "owed" else ""
    lines: List[Line] = [section("VISA", width, trailing, "yellow" if soon else "muted")]
    if title_gap:
        lines.append(blank())
    paid = int(card.get("paid_this_month_cents", 0) or 0)
    if status == "credit" or owed < 0:
        lines += _wrap_text(f"{GLYPHS['done']} Credit {money(abs(owed), m)}: the next charges use it first.", width, "green")
    elif status != "owed" or owed <= 0:
        lines += _wrap_text(f"{GLYPHS['done']} Paid off. Nothing is owed on the Visa.", width, "green")
    else:
        charged = int(card.get("charged_since_last_payment_cents", 0) or 0)
        last = card.get("last_payment") or {}
        before = owed - charged
        after = f" after the payment on {fmt_day(last['date'])}" if last.get("date") else " before these charges"
        if before < 0:
            # The payment left a credit: the charges use it up first.
            lines += [
                _calc("", f"Charged since the payment on {fmt_day(last['date'])}" if last.get("date")
                      else "Charged", num(charged, m), width, val),
                _calc("−", "Credit left by that payment" if last.get("date") else "Credit before these charges",
                      num(-before, m), width, val),
            ]
        else:
            lines += [
                _calc("", "Owed" + after, num(before, m), width, val),
                _calc("+", "Charged since", num(charged, m), width, val),
            ]
        lines += [
            _rule(width),
            _calc("=", "Owed now", num(owed, m), width, "muted" if m else ("yellow" if soon else "text"), bold=True),
        ]
        if when:
            if overdue:
                text = f"Overdue since {fmt_day(when)} with no payment logged, so it is in the bills before payday."
            elif soon:
                text = f"Due {fmt_day(when)}, before payday, so it is in the bills before payday."
            else:
                text = f"Due {fmt_day(when)}, after payday, so it waits for the next pay period."
            lines += _wrap_text((GLYPHS["over"] + " " if soon else "") + text, width, "yellow" if soon else "muted")
    if paid > 0:
        lines += pack([([(f"{money(paid, m)} paid to the card this month.", "muted")], [])], width)
    return lines


def _month_categories(d: FinanceViewData) -> List[Tuple[str, int, int]]:
    """(category, spent, part on the Visa) for this month's spending, largest first."""
    month = _today(d).strftime("%Y-%m")
    totals: Dict[str, List[int]] = {}
    today_s = _today(d).strftime("%Y-%m-%d")
    for day in d.days or []:
        ds = str(day.get("date", ""))
        if not ds.startswith(month) or ds > today_s:
            continue  # entries dated later this month are not spent yet
        for it in day.get("items") or []:
            if item_kind(it) != "spend":
                continue
            name = clean_name(it.get("category", "")) or "Other"
            amt = int(it.get("amount_cents", 0) or 0)
            row = totals.setdefault(name, [0, 0])
            row[0] += amt
            if is_card(it):
                row[1] += amt
    return sorted(((k, v[0], v[1]) for k, v in totals.items()), key=lambda r: -r[1])


def _category_lines(d: FinanceViewData, width: int, max_rows: int, title_gap: bool,
                    edge: Optional[int] = None) -> List[Line]:
    """SPENT BY CATEGORY: this month's spending per category as bars (fits in `max_rows`).

    Rows are name, bar, share, amount; the amount ends on `edge` (default: the calculation rows'
    edge), so it lines up with the other amounts in the column."""
    cats = _month_categories(d)
    if not cats or max_rows < 2:
        return []
    m = d.mask
    total = sum(c[1] for c in cats)
    title = section(f"SPENT BY CATEGORY {GLYPHS['dot']} {_month_name(d)}", width, f"{money(total, m)} so far")
    head = [title] + ([blank()] if title_gap and max_rows >= 4 else [])
    n = max(1, max_rows - len(head))
    shown = cats[:n]
    if len(cats) > n and n >= 2:
        shown = cats[: n - 1]
        rest = cats[n - 1:]
        shown.append((f"{len(rest)} more", sum(c[1] for c in rest), sum(c[2] for c in rest)))
    name_w = max(10, min(16, max(cell_width(c[0]) for c in shown) + 2))
    end = min(width, edge if edge is not None else calc_edge(width))
    # Cells between the name and the share; the bar takes up to 24 of them.
    area = max(4, end - len(GUTTER) - name_w - 6 - 10)
    bar_w = min(24, area)
    peak = max(c[1] for c in shown) or 1
    lines = list(head)
    for name, spent, on_card in shown:
        filled = max(1, int(round(spent / peak * bar_w))) if spent > 0 else 0
        pct = f"{spent * 100.0 / total:.0f}%" if total else ""
        row: Line = [(GUTTER + rpad(name, name_w), "text"), (BAR * filled, "blue"), (" " * (area - filled), "text"),
                     (lpad(pct, 6), "muted"), (lpad(num(spent, m), 10), "muted" if m else "text")]
        lines.append(fit(row, width))
    return lines


MATH_CATS_ROWS = 9  # SPENT BY CATEGORY on Math: title, gap and 7 rows (the last may be 'N more')
MATH_PERIOD_ROWS = 8  # PAY PERIODS on Math: title, gap and up to 6 periods


def build_math(d: FinanceViewData, cols: int, rows: int) -> Rendered:
    """How the numbers are worked out: this pay period, the Visa, this month, why the two numbers
    differ, the pace, the pay periods and spending by category.

    Every section is always there. When they do not all fit, spacing tightens first; when they
    still do not fit, the sections scroll under the pinned 'Money ▸ Math' line (j/k, kept in
    VIEW_PREFS["math_scroll"]) and that line says how many rows are above and below. Nothing is
    dropped, so a short pane can still reach every section."""
    width, height = max(20, cols), max(1, rows)
    ov = (d.bills_breakdown or {}).get("overview") or d.overview
    status = str(getattr(ov, "status", "") or "")
    wide = width >= WIDE_MIN
    col_w = (width - 2) // 2 if wide else width
    right_w = width - 2 - col_w if wide else width

    def body_for(tgap: bool, igap: bool, sgap: bool) -> List[Line]:
        def titled(title: Line, body: List[Line]) -> List[Line]:
            return [title] + ([blank()] if tgap else []) + body

        card = _card_math(d, col_w, tgap)
        # The pay period ends with a one-line Visa note only when there is no VISA block.
        pay = titled(section("THIS PAY PERIOD", col_w, _period_trailing(d)), _pay_period(d, col_w, igap, not card))
        month = titled(section(_month_name(d), right_w, status), _month_lines(d, right_w, igap, True))
        why = titled(section("WHY TWO NUMBERS", right_w), _wrap_text(WHY_TEXT, min(right_w, 100)))
        pace = _pace_lines(d, col_w, tgap)
        periods = _period_lines(d, col_w, MATH_PERIOD_ROWS, tgap)
        cats = _category_lines(d, right_w, MATH_CATS_ROWS, tgap)
        if wide:
            return hcat(_stack([pay, card, pace, periods], sgap), col_w, _stack([month, why, cats], sgap), right_w)
        return _stack([pay, card, month, why, pace, periods, cats], sgap)

    # (title gaps, inner gaps, section gaps, crumb gap), roomy to tight.
    levels = [(True, True, True, True), (False, True, True, True), (False, False, True, True),
              (False, False, True, False), (False, False, False, False)]
    for tgap, igap, sgap, cgap in levels:
        top = [crumb("Math", width, d.mask)] + ([blank()] if cgap else [])
        body = body_for(tgap, igap, sgap)
        if len(top) + len(body) <= height:
            VIEW_PREFS["math_scroll"] = 0
            VIEW_PREFS["math_scrollable"] = False
            return Rendered(lines=_finish(top + body, width, height))

    # Scrolls. The spacing stays as on a 95x46 pane (blank rows between sections and blocks).
    cgap = height >= 8
    body = body_for(False, True, True)
    window = max(1, height - 1 - (1 if cgap else 0))
    offset = max(0, min(int(VIEW_PREFS.get("math_scroll", 0) or 0), len(body) - window))
    VIEW_PREFS["math_scroll"] = offset
    VIEW_PREFS["math_scrollable"] = True
    below = len(body) - offset - window
    parts = ([f"{offset} above"] if offset else []) + ([f"{below} below"] if below > 0 else [])
    note = f" {GLYPHS['dot']} ".join(parts) + "  j/k scroll"
    top = [crumb("Math", width, d.mask, note)] + ([blank()] if cgap else [])
    return Rendered(lines=_finish(top + body[offset: offset + window], width, height))


# ---------------------------------------------------------------------------
# Bills screen
# ---------------------------------------------------------------------------

_CATEGORY_NAMES = {
    "fixed bill": "Fixed bill",
    "subscription": "Subscription",
    "utility (variable)": "Utility, estimate",
    "card balance": "Card payment",
}


def _category(b: Dict[str, Any]) -> str:
    raw = clean_name(b.get("category", ""))
    name = _CATEGORY_NAMES.get(raw.lower(), raw)
    if b.get("is_variable") and "estimate" not in name.lower():
        name = (name + ", estimate") if name else "Estimate"
    return name


def _is_hidden(b: Dict[str, Any]) -> bool:
    """Inactive bills and $0 bills (other than the card) stay out of the list until h is pressed."""
    if not b.get("is_active", True):
        return True
    return int(b.get("amount_cents", 0) or 0) <= 0 and b.get("key") != "credit_card"


def bill_display(d: FinanceViewData) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], bool]:
    """(bills shown, bills hidden, whether hidden bills are shown). Bills sort by due day; hidden
    bills (when h shows them) come after the others, so h never moves the row a bill sits on."""
    bills = list((d.bills_breakdown or {}).get("bills") or [])
    show = VIEW_PREFS.get("show_hidden_bills", False) if d.show_hidden_bills is None else d.show_hidden_bills

    def order(b: Dict[str, Any]) -> Tuple[int, str]:
        return int(b.get("due_day", 0) or 0), clean_name(b.get("name", ""))

    hidden = sorted((b for b in bills if _is_hidden(b)), key=order)
    shown = sorted((b for b in bills if not _is_hidden(b)), key=order) + (hidden if show else [])
    return shown, hidden, show


def _card_bill(b: Dict[str, Any], d: FinanceViewData) -> bool:
    return b.get("key") == "credit_card" and bool(d.card)


def _bill_amount(b: Dict[str, Any], d: FinanceViewData) -> int:
    if _card_bill(b, d):
        return max(0, int((d.card or {}).get("owed_cents", 0) or 0))
    return int(b.get("amount_cents", 0) or 0)


def _card_due_this_month(d: FinanceViewData, today: datetime) -> Optional[datetime]:
    """The card's next due date when it falls in this month and a balance is owed, else None."""
    card = d.card or {}
    when = _date(card.get("next_due_date"))
    if card.get("status") != "owed" or int(card.get("owed_cents", 0) or 0) <= 0 or when is None:
        return None
    return when if (when.year, when.month) == (today.year, today.month) else None


def _due_words(day: int, today: datetime) -> Tuple[str, str]:
    if day == today.day:
        return "due today", "yellow"
    if day < today.day:
        return f"{GLYPHS['over']} past due", "yellow"
    left = day - today.day
    return ("due tomorrow" if left == 1 else f"due in {left} days"), "muted"


def _bill_status(b: Dict[str, Any], d: FinanceViewData, today: datetime) -> Tuple[str, str]:
    """This month's status, the same rule on every row (the column is titled with the month).

    The card row too: its payment this month, or its due date when that is this month. A balance
    due next month is not this month's status; it gets its own '+ Visa balance, due ...' line
    under the total, next to COMING UP."""
    m = d.mask
    if not b.get("is_active", True):
        return "inactive", "muted"
    if _card_bill(b, d):
        card = d.card or {}
        if card_overdue(card, today):
            return f"{GLYPHS['over']} overdue", "yellow"
        when = _card_due_this_month(d, today)
        if when is not None:
            text, style = _due_words(when.day, today)
            if card.get("due_before_payday") and style != "yellow":
                return f"{GLYPHS['over']} {text}", "yellow"
            return text, style
        if int(card.get("paid_this_month_cents", 0) or 0) > 0:
            return f"{GLYPHS['done']} paid", "green"
        if card.get("status") == "credit" or int(card.get("owed_cents", 0) or 0) < 0:
            return f"{GLYPHS['done']} credit", "green"
        if card.get("status") == "owed" and int(card.get("owed_cents", 0) or 0) > 0:
            return "nothing due", "muted"  # owed now, but the payment is due next month
        return f"{GLYPHS['done']} paid off", "green"
    if b.get("status") == "paid":
        return f"{GLYPHS['done']} paid", "green"
    text = clean_name(b.get("status_text", ""))
    if "Rem:" in text:
        try:
            rem = int(round(float(text.split("$", 1)[1].replace(",", "")) * 100))
            return f"{GLYPHS['over']} {money(rem, m)} left", "yellow"
        except (IndexError, ValueError):
            pass
    return _due_words(int(b.get("due_day", 0) or 0), today)


def _row_amount(b: Dict[str, Any], d: FinanceViewData, today: datetime) -> int:
    """Amount on a bill's row, for the same month as its status. The card row shows what was paid
    to it this month, or the balance when that is due this month (neither is in the fixed total)."""
    if not _card_bill(b, d):
        return _bill_amount(b, d)
    card = d.card or {}
    if _card_due_this_month(d, today) is not None:
        return _bill_amount(b, d)
    return max(0, int(card.get("paid_this_month_cents", 0) or 0))


TABLE_MAX = 95  # widest a table gets (gutter included); wider panes leave the rest blank
EDGE_MIN = 64  # narrowest amount edge for the Bills screen sections (when the pane allows it)


def _bill_columns(d: FinanceViewData, width: int) -> Tuple[int, int, int, int, int]:
    """(due, name, category, status, amount) widths at their natural size for `width`.

    Measured over every bill (shown or hidden, plain and masked), so pressing h or m never moves
    a column. The category column drops first, then the name shrinks, when the pane is narrow."""
    bills = list((d.bills_breakdown or {}).get("bills") or [])
    today = _today(d)
    inner = width - len(GUTTER)
    due_w, amt_w = 6, 10
    longest = max([cell_width(clean_name(b.get("name", ""))) for b in bills] + [12])
    name_w = max(12, min(28, longest + 2))
    cat_w = max([cell_width(_category(b)) for b in bills] + [len("CATEGORY")]) + 2
    status = [cell_width(_bill_status(b, replace(d, mask=mk), today)[0]) for b in bills for mk in (False, True)]
    status_w = max(12, min(40, max(status + [0]) + 2))
    if due_w + name_w + cat_w + status_w + amt_w > inner:
        cat_w = 0
    if due_w + name_w + cat_w + status_w + amt_w > inner:
        name_w = max(8, inner - due_w - status_w - amt_w)
    return due_w, name_w, cat_w, status_w, amt_w


def bills_edge(d: FinanceViewData, width: int) -> int:
    """The column where every amount on the Bills screen ends (gutter included): the bill table's
    natural width, at least EDGE_MIN and at most TABLE_MAX, never more than `width`."""
    natural = len(GUTTER) + sum(_bill_columns(d, width))
    return min(width, TABLE_MAX, max(natural, EDGE_MIN))


def _bills_table(d: FinanceViewData, width: int, list_rows: int, header: bool, title_gap: bool,
                 edge: Optional[int] = None) -> Tuple[List[Line], Dict[int, int], int]:
    m = d.mask
    shown, hidden, show = bill_display(d)
    today = _today(d)
    due_w, name_w, cat_w, status_w, amt_w = _bill_columns(d, width)
    table_w = min(width, edge if edge is not None else bills_edge(d, width))
    # The status column takes up the slack, so the amounts end on the table edge.
    status_w = max(4, table_w - len(GUTTER) - due_w - name_w - cat_w - amt_w)

    def card_balance(b: Dict[str, Any]) -> bool:
        # The card balance is not a fixed monthly bill: it gets its own line under the total.
        return b.get("key") == "credit_card" and _bill_amount(b, d) > 0

    def counted(b: Dict[str, Any]) -> bool:
        return bool(b.get("is_active", True)) and not _is_hidden(b) and not card_balance(b)

    month_total = sum(_bill_amount(b, d) for b in shown if counted(b))
    lines: List[Line] = [section("RECURRING BILLS", width, f"{money(month_total, m)} a month")]
    if title_gap:
        lines.append(blank())
    if header:
        # The status column is this month's status on every row, so its title is the month.
        head = rpad("DUE", due_w) + rpad("BILL", name_w) + (rpad("CATEGORY", cat_w) if cat_w else "") + rpad(_month_name(d), status_w) + lpad("AMOUNT", amt_w)
        lines.append(fit([(GUTTER + head, "muted")], width))
    hits: Dict[int, int] = {}
    if not shown:
        lines.append(fit([(GUTTER + "No recurring bills set up. Add fixed_bills to setup.yaml.", "muted")], width))
    sel = max(0, min(d.selected_bill_idx, len(shown) - 1)) if shown else 0
    list_rows = max(1, list_rows)
    offset = max(0, min(d.bills_scroll_offset, max(0, len(shown) - list_rows)))
    if sel < offset:
        offset = sel
    elif sel >= offset + list_rows:
        offset = sel - list_rows + 1
    for i in range(offset, min(len(shown), offset + list_rows)):
        b = shown[i]
        selected = i == sel
        off = _is_hidden(b)
        status, sstyle = _bill_status(b, d, today)
        base = "muted" if off else "text"
        content: Line = [(rpad(f"{int(b.get('due_day', 0) or 0):02d}", due_w), base),
                         # A cut name still keeps one blank cell before the next column.
                         (rpad(clip(clean_name(b.get("name", "")), name_w - 1), name_w), base + ("+b" if selected and not off else ""))]
        if cat_w:
            content.append((rpad(_category(b), cat_w), "muted"))
        content += [(rpad(status, status_w), sstyle),
                    (lpad(num(_row_amount(b, d, today), m), amt_w), "muted" if (m or off or _card_bill(b, d)) else "text")]
        hits[len(lines)] = i
        lines.append(marked(content, selected, width))
    if shown:
        paid = sum(_bill_amount(b, d) for b in shown if counted(b) and b.get("status") == "paid")
        due = sum(_bill_amount(b, d) for b in shown if counted(b) and b.get("status") != "paid")
        count = sum(1 for b in shown if counted(b))
        lines.append(fit([(GUTTER + "─" * (table_w - len(GUTTER)), "dim")], width))
        total: Line = [(num(month_total, m), "text+b")]
        # The notes drop whole (paid first, then due) rather than cut an amount.
        notes = [f"  {GLYPHS['dot']}  paid {num(paid, m)}  {GLYPHS['dot']}  due {num(due, m)}",
                 f"  {GLYPHS['dot']}  due {num(due, m)}", ""]
        room = table_w - line_width(total) - 2 - len(GUTTER) - cell_width(_plural(count, "bill"))
        note = next((t for t in notes if cell_width(t) <= room), "")
        left: Line = [(GUTTER + _plural(count, "bill"), "text+b"), (note, "muted")]
        lines.append(fit(spread(left, total, table_w), width))
        for b in shown:
            if card_balance(b) and not _is_hidden(b):
                due_at = _date((d.card or {}).get("next_due_date"))
                word = "overdue since" if card_overdue(d.card, _today(d)) else "due"
                when = f"{word} {due_at:%b} {due_at.day}" if due_at else ""
                options = [f"{GUTTER}+ Visa balance, {when}, not a fixed bill"] if when else []
                options += [f"{GUTTER}+ Visa balance, not a fixed bill", f"{GUTTER}+ Visa balance"]
                value: Line = [(num(_bill_amount(b, d), m), "muted")]
                room = table_w - line_width(value) - 2
                text = next((t for t in options if cell_width(t) <= room), options[-1])
                lines.append(fit(spread([(text, "muted")], value, table_w), width))
    if hidden:
        names = ", ".join(clean_name(b.get("name", "")) for b in hidden)
        if show:
            options = [f"{GUTTER}Showing {_plural(len(hidden), 'inactive or $0 bill')} at the end",
                       f"{GUTTER}Showing {len(hidden)} hidden"]
            key = [("h", "cyan"), (" to hide", "muted")]
        else:
            options = [f"{GUTTER}{_plural(len(hidden), 'inactive or $0 bill')} hidden: {names}",
                       f"{GUTTER}{_plural(len(hidden), 'inactive or $0 bill')} hidden",
                       f"{GUTTER}{len(hidden)} hidden"]
            key = [("h", "cyan"), (" to show", "muted")]
        # A note, not an amount row: the key follows the text, which may run past the table edge.
        room = width - line_width(key) - 3
        text = next((t for t in options if cell_width(t) <= room), options[-1])
        lines.append(fit([(text, "muted"), ("   ", "text")] + key, width))
    return lines, hits, offset


BUDGET_AMT_W = 10
BUDGET_FIXED = 12 + 9 + 12 + 2 + 4 + BUDGET_AMT_W  # name, spent, ' of plan', gap, 'over', amount


def _budget_lines(d: FinanceViewData, width: int, title_gap: bool, edge: Optional[int] = None) -> List[Line]:
    """BUDGETS: one bar per budget, then the flex total. The left / over amount ends on `edge`
    (default: as far as a 20-cell bar reaches), in line with the other amounts in the column."""
    ov = (d.bills_breakdown or {}).get("overview") or d.overview
    if not ov:
        return []
    m = d.mask
    budgets = _budgets(ov)
    spent = sum(a for _, a, _ in budgets)
    planned = sum(p for _, _, p in budgets)
    lines: List[Line] = [section("BUDGETS", width, f"{money(spent, m)} spent of {money(planned, m)}")]
    if title_gap:
        lines.append(blank())
    if edge is None:
        edge = len(GUTTER) + BUDGET_FIXED + max(6, min(20, width - len(GUTTER) - BUDGET_FIXED))
    end = min(width, edge)
    # Narrow panes drop ' of plan', then the bar, so the left / over amount is never cut.
    fixed = BUDGET_FIXED
    show_plan = end - len(GUTTER) - fixed >= 6
    if not show_plan:
        fixed -= 12
    area = max(0, end - len(GUTTER) - fixed)
    bar_w = min(20, area) if area >= 4 else 0
    for name, actual, plan in budgets:
        ratio = actual / plan if plan > 0 else (2.0 if actual > 0 else 0.0)
        color = "green" if ratio <= 1 else ("yellow" if ratio <= 1.5 else "red")
        filled = max(0, min(bar_w, int(round(min(1.0, ratio) * bar_w))))
        left = plan - actual
        over = left < 0
        row: Line = [(GUTTER + rpad(name, 12), "text"), ("█" * filled, color), ("░" * (bar_w - filled), "dim"),
                     (" " * (area - bar_w), "text"),
                     (lpad(num(actual, m), 9), "muted" if m else "text"),
                     (f" of {rpad(num(plan, m), 8)}" if show_plan else "", "muted"),
                     ("  " + ("over" if over else "left"), color if over else "muted"),
                     (lpad(num(abs(left), m), BUDGET_AMT_W), "muted" if m else (color if over else "text"))]
        lines.append(fit(row, width))
    flex = ov.remaining_cushion_cents
    lines += [_rule(width, end),
              _calc("=", "Flex left this month", num(flex, m), width, _neg_style(flex, m, "green"), bold=True, edge=end)]
    return lines + _flex_note(d, flex, width)


def _this_month(d: FinanceViewData, width: int, title_gap: bool, edge: Optional[int] = None) -> List[Line]:
    ov = (d.bills_breakdown or {}).get("overview") or d.overview
    if not ov:
        return []
    m = d.mask
    val = "muted" if m else "text"
    status = str(getattr(ov, "status", "") or "")
    left = ov.income_cents - ov.savings_target_cents - ov.fixed_bills_cents - ov.pge_cents
    lines: List[Line] = [section(f"THIS MONTH {GLYPHS['dot']} {_month_name(d)}", width, status)]
    if title_gap:
        lines.append(blank())
    lines += [
        _calc("", "Income", num(ov.income_cents, m), width, "muted" if m else "green", edge=edge),
        _calc("−", "Plan to save", num(ov.savings_target_cents, m), width, val, edge=edge),
        _calc("−", "Fixed bills", num(ov.fixed_bills_cents, m), width, val, edge=edge),
        _calc("−", "PG&E estimate" if status != "closed" else "PG&E", num(ov.pge_cents, m), width, val, edge=edge),
        _rule(width, edge),
        _calc("=", "Left for spending", num(left, m), width, _neg_style(left, m), bold=True, edge=edge),
    ]
    return lines


def _coming_up(d: FinanceViewData, width: int, title_gap: bool, edge: Optional[int] = None) -> List[Line]:
    """COMING UP: bills due before payday, then payday. Amounts end on `edge` (default: after a
    30-cell name column)."""
    rw = d.runway
    pc = d.paycheck
    if rw is None or pc is None:
        return []
    m = d.mask
    today = rw.today
    rows: List[Tuple[str, Line]] = []
    if edge is None:
        edge = len(GUTTER) + 13 + max(12, min(30, width - len(GUTTER) - 13 - 12)) + 12
    name_w = max(8, min(width, edge) - len(GUTTER) - 13 - 12)

    def name_cell(text: str, style: str, flag: str = "") -> Line:
        # A flag (the yellow ▲) sits after the name, so the amount stays the last thing on the row.
        if flag and cell_width(text) + 2 + cell_width(flag) <= name_w:
            return [(text, style), (rpad("  " + flag, name_w - cell_width(text)), "yellow")]
        return [(rpad(text, name_w), style)]

    for b in getattr(pc, "bills", []) or []:
        if b.get("status") != "due" or b.get("amount_cents", 0) <= 0 or b.get("due_date", "") < today:
            continue
        if d.card and "credit card" in clean_name(b.get("name", "")).lower():
            continue
        rows.append((b["due_date"], name_cell(clean_name(b.get("name", "")), "text")
                     + [(lpad(num(b["amount_cents"], m), 12), "muted" if m else "text")]))
    card = d.card or {}
    owed = int(card.get("owed_cents", 0) or 0)
    when = card.get("next_due_date") or ""
    if card.get("status") == "owed" and owed > 0 and when and when <= rw.next_paycheck:
        # An overdue card (due date already passed) shows under today: it comes out now.
        late = when < today
        rows.append((today if late else when, name_cell("Visa overdue" if late else "Visa payment", "text", GLYPHS["over"])
                     + [(lpad(num(owed, m), 12), "yellow" if not m else "muted")]))
    rows.sort(key=lambda r: r[0])
    rows.append((rw.next_paycheck, name_cell("payday", "green")
                 + [(lpad(num(getattr(pc, "paycheck_amount_cents", 0), m, signed=True), 12), "green")]))
    lines: List[Line] = [section("COMING UP", width, f"until payday {fmt_day(rw.next_paycheck)}")]
    if title_gap:
        lines.append(blank())
    for when_s, seg in rows:
        lines.append(fit([(GUTTER + rpad(fmt_day(when_s), 13), "muted")] + seg, width))
    return lines


def _bill_calendar(d: FinanceViewData, width: int, title_gap: bool) -> List[Line]:
    """The month as a strip: ▼ bill due, ✓ paid, ◆ payday, ● today, with short names under it."""
    today = _today(d)
    last = calendar.monthrange(today.year, today.month)[1]
    avail = max(1, width - len(GUTTER))
    # One cell more than the last mark, so a two-digit last day ('31') still has room for its label.
    step = next((s for s in (4, 3, 2) if (last - 1) * s + 2 <= avail), 1)
    cells = min(avail, (last - 1) * step + 2)
    by_day: Dict[int, List[Dict[str, Any]]] = {}
    for b in bill_display(d)[0]:
        if _is_hidden(b) or (_bill_amount(b, d) <= 0 and b.get("status") != "paid"):
            continue
        if _card_bill(b, d):
            # The card's next due day is often next month: mark it only when it falls in this one.
            due = _date((d.card or {}).get("next_due_date"))
            if _bill_amount(b, d) > 0 and (due is None or (due.year, due.month) != (today.year, today.month)):
                continue
        by_day.setdefault(int(b.get("due_day", 0) or 0), []).append(b)
    # Every payday this month: the logged paychecks, the start of this pay period and the next one.
    paydays = set()
    pc = d.paycheck
    for value in [getattr(pc, "cycle_start_date", ""), getattr(pc, "next_paycheck_date", "")] + _paydays(d):
        dt = _date(value)
        if dt is not None and dt.year == today.year and dt.month == today.month:
            paydays.add(dt.day)
    track: Line = []
    for day in range(1, last + 1):
        bills = by_day.get(day, [])
        if day in paydays:
            track.append((GLYPHS["payday"], "green+b"))
        elif day == today.day:
            track.append((GLYPHS["ok"], "cyan+b"))
        elif any(b.get("status") != "paid" for b in bills):
            track.append((GLYPHS["bill"], "yellow+b"))
        elif bills:
            track.append((GLYPHS["done"], "green"))
        else:
            track.append(("┼", "dim"))
        if day < last and step > 1:
            track.append(("─" * (step - 1), "dim"))
    labels, lst = [" "] * cells, ["muted"] * cells
    names, nst = [" "] * cells, ["muted"] * cells
    for day in range(1, last + 1):
        pos = (day - 1) * step
        _place(labels, lst, pos, str(day), "text" if day == today.day else "muted", pad_after=0)
        if day in paydays:
            _place(names, nst, pos, "Pay", "green")
        for b in by_day.get(day, []):
            due = b.get("status") != "paid"
            if _place(names, nst, pos, short_bill_name(b.get("name", ""), lower=False), "yellow" if due else "green"):
                break
    legend = f"{GLYPHS['bill']} due  {GLYPHS['done']} paid  {GLYPHS['payday']} payday  {GLYPHS['ok']} today"
    lines: List[Line] = [section("BILL CALENDAR", width, legend if width > NARROW_MAX else today.strftime("%B"))]
    if title_gap:
        lines.append(blank())
    lines += [fit([(GUTTER, "text")] + fit(track, cells), width),
              fit([(GUTTER, "text")] + _runs(labels, lst), width),
              fit([(GUTTER, "text")] + _runs(names, nst), width)]
    return lines


def _card_items(d: FinanceViewData) -> List[Tuple[str, Dict[str, Any]]]:
    """(date, item) for Visa purchases and card payments, newest first."""
    out = []
    for day in d.days or []:
        for it in day.get("items") or []:
            kind = item_kind(it)
            if (kind == "spend" and is_card(it)) or kind == "card_payment":
                out.append((str(day.get("date", "")), it))
    return out


def _card_trailing(d: FinanceViewData) -> Tuple[str, str]:
    card = d.card or {}
    m = d.mask
    owed = int(card.get("owed_cents", 0) or 0)
    if not card:
        return "", "muted"
    if card.get("status") == "credit" or owed < 0:
        return f"credit {money(abs(owed), m)}", "green"
    if card.get("status") != "owed" or owed <= 0:
        return f"{GLYPHS['done']} paid off", "green"
    when = card.get("next_due_date")
    word = "overdue since" if card_overdue(card, _today(d)) else "due"
    text = f"owed {money(owed, m)}" + (f" {GLYPHS['dot']} {word} {fmt_day(when)}" if when else "")
    return text, "yellow" if card.get("due_before_payday") else "muted"


def _card_activity(d: FinanceViewData, width: int, max_rows: int, title_gap: bool,
                   edge: Optional[int] = None) -> List[Line]:
    """VISA ACTIVITY: card purchases and card payments, newest first (fits in `max_rows`).
    Amounts end on `edge` (default: the column's right edge)."""
    if max_rows < 2 or (not d.card and not d.visa_cents):
        return []
    m = d.mask
    trailing, tstyle = _card_trailing(d)
    lines: List[Line] = [section("VISA ACTIVITY", width, trailing, tstyle)]
    if title_gap and max_rows >= 4:
        lines.append(blank())
    items = _card_items(d)
    if not items:
        lines += _wrap_text("No card purchases logged. Add visa to a line to put it on the card, "
                            "for example: groceries 25.50 visa", width)
        return lines[:max_rows]
    inner = min(width, edge if edge is not None else width) - len(GUTTER)
    cat_w = 15 if inner >= 56 else 0
    amt_w = AMT_W
    # The description fills the row, so amounts end on the same column as the sections above.
    desc_w = max(8, inner - 12 - cat_w - amt_w)
    room = max(1, max_rows - len(lines))
    # A cut list ends with a '· 24 more' row, so the rows shown are not read as all of them.
    shown = items if len(items) <= room else items[: room - 1]
    for date, it in shown:
        amt = int(it.get("amount_cents", 0) or 0)
        if item_kind(it) == "card_payment":
            row: Line = [(GUTTER + rpad(fmt_day(date), 12), "muted"),
                         (rpad(f"{GLYPHS['swap']} payment", desc_w), "green")]
            if cat_w:
                row.append((rpad("from " + (item_account(it).lower() or "checking"), cat_w), "muted"))
            row += [(lpad(num(amt, m), amt_w), "muted" if m else "green")]
        else:
            row = [(GUTTER + rpad(fmt_day(date), 12), "text"),
                   (rpad(clean_name(it.get("payee") or it.get("memo") or ""), desc_w), "text")]
            if cat_w:
                row.append((rpad(clean_name(it.get("category", "")), cat_w), "muted"))
            row += [(lpad(num(amt, m), amt_w), "muted" if m else "text")]
        lines.append(fit(row, width))
    if len(shown) < len(items):
        lines.append(_more_row(d, items[len(shown):], bool(shown), min(width, edge if edge is not None else width)))
    return lines


def _more_row(d: FinanceViewData, rest: List[Tuple[str, Dict[str, Any]]], some_shown: bool, width: int) -> Line:
    """'· 24 more (3 payments), charged      152.58' for card rows that did not fit. The total
    ends on the column's amount edge like every other amount; shorter words on a narrow column."""
    charged = sum(int(it.get("amount_cents", 0) or 0) for _, it in rest if item_kind(it) != "card_payment")
    payments = [it for _, it in rest if item_kind(it) == "card_payment"]
    n = len(rest)
    head = f"{GLYPHS['dot']} {n} {'more' if some_shown else 'card entries'}"
    if charged or not payments:
        amount, word = charged, "charged"
        forms = ([f"{head} ({_plural(len(payments), 'payment')}), {word}"] if payments else []) + [f"{head}, {word}", head]
    else:
        amount = sum(int(it.get("amount_cents", 0) or 0) for it in payments)
        forms = [f"{head} payments", head]
    value: Line = [(lpad(num(amount, d.mask), AMT_W), "muted")]
    room = width - AMT_W - 1
    text = next((t for t in forms if len(GUTTER) + cell_width(t) <= room), forms[-1])
    return fit(spread([(GUTTER + text, "muted")], value, width), width)


def build_bills(d: FinanceViewData, cols: int, rows: int) -> Rendered:
    """Recurring bills, this month's math, budgets, what is coming up before payday, the month's
    bill calendar and the Visa activity filling what is left."""
    width, height = max(20, cols), max(1, rows)
    shown, _, _ = bill_display(d)
    need = max(1, len(shown))

    def done(top: List[Line], lines: List[Line], hits: Dict[int, int], offset: int) -> Rendered:
        base = len(top)
        return Rendered(lines=_finish(lines, width, height),
                        bill_rows={base + k: v for k, v in hits.items() if base + k < height},
                        bills_scroll_offset=offset)

    if width >= WIDE_MIN:
        # Two columns (bills, coming up, Visa activity | this month, budgets, categories); the
        # month's bill calendar runs under both. The left column is as wide as the bill table
        # needs (at most TABLE_MAX), so rows never stretch across a wide pane; what is left over
        # stays blank on the right. Each column has one amount edge: its right side.
        right_w = 62
        left_w = bills_edge(d, width - 2 - right_w)
        used = left_w + 2 + right_w
        top = [crumb("Bills", width, d.mask), blank()]
        for tgap in (True, False):
            g = 1
            under = _coming_up(d, left_w, tgap, left_w)
            probe, _, _ = _bills_table(d, left_w, need, True, tgap, left_w)
            extra = len(probe) - need
            cal = _bill_calendar(d, used, tgap)
            body_h = height - len(top)
            if len(probe) + (len(under) + g if under else 0) + g + len(cal) > body_h:
                cal = []
            col_h = body_h - (len(cal) + g if cal else 0)
            room = col_h - extra - (len(under) + g if under else 0)
            if room >= need or not tgap:
                if room < min(need, 3):
                    under = []
                    room = col_h - extra
                table, hits, offset = _bills_table(d, left_w, max(1, min(need, room)), True, tgap, left_w)
                # Left: bills, coming up, then the Visa activity in the rows left over. Right: this
                # month and the budgets, then spending by category in the rows left over.
                _, extra_l = _fit(col_h, g, [table, under], [],
                                  ("card", lambda n: _card_activity(d, left_w, n, tgap, left_w)), flex_min=4)
                _, side = _fit(col_h, g, [], [("month", _this_month(d, right_w, tgap, right_w)),
                                              ("budgets", _budget_lines(d, right_w, tgap, right_w))],
                               ("cats", lambda n: _category_lines(d, right_w, n, tgap, right_w)), flex_min=4)
                right = _stack([side.get("month", []), side.get("budgets", []), side.get("cats", [])])
                left = _stack([table, under, extra_l.get("card", [])])
                lines = top + hcat(left, left_w, right, right_w)
                if cal:
                    lines += [blank()] + cal
                return done(top, lines, hits, offset)

    # One column. The table always shows (it scrolls when rows run short); then sections come in
    # by priority (budgets, this month, coming up, calendar, Visa activity) while they fit, shown
    # in the mockup order. Blank rows between sections go last.
    best: Optional[Tuple[Tuple[int, ...], List[Line], List[Line], Dict[int, int], int]] = None
    edge = bills_edge(d, width)  # every amount on the screen ends on this column
    levels = ((True, True, True, True), (False, True, True, True), (False, False, True, True),
              (False, False, False, True), (False, False, False, False))
    for li, (tgap, cgap, sgap, header) in enumerate(levels):
        top = [crumb("Bills", width, d.mask)] + ([blank()] if cgap else [])
        g = 1 if sgap else 0
        probe, _, _ = _bills_table(d, width, need, header, tgap, edge)
        extra = len(probe) - need
        avail = height - len(top)
        rows_for_table = max(1, min(need, avail - extra))
        table_h = extra + rows_for_table
        optional = [("budgets", _budget_lines(d, width, tgap, edge)), ("month", _this_month(d, width, tgap, edge)),
                    ("coming", _coming_up(d, width, tgap, edge)), ("calendar", _bill_calendar(d, width, tgap))]
        _, chosen = _fit(avail - table_h, g, [], optional,
                         ("card", lambda n: _card_activity(d, width, n, tgap, edge)), flex_min=4)
        # _fit counts no gap before its first block; the table sits above it.
        while chosen and table_h + g + sum(len(b) + g for b in chosen.values()) - g > avail:
            chosen.pop(list(chosen)[-1])
        table, hits, offset = _bills_table(d, width, rows_for_table, header, tgap, edge)
        rest = [chosen.get(n, []) for n in ("month", "budgets", "coming", "calendar", "card")]
        lines = top + _stack([table] + rest, sgap)
        # Most bill rows first, then blank rows between sections and after titles (the mockup
        # spacing), then the most sections.
        score = (rows_for_table, g, int(tgap), len(chosen), -li)
        if best is None or score > best[0]:
            best = (score, top, lines, hits, offset)
    if best is not None:
        return done(best[1], best[2], best[3], best[4])
    return Rendered(lines=[])  # pragma: no cover
