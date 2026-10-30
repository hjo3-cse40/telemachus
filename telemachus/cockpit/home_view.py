"""Home tab: "Today at a glance" (TodaySummary.dc.html, EmptyInbox.dc.html).

Four blocks: MONEY (one line from the finance data), INBOX (triage rows with a 2-line preview of
the selected mail), MEMORY (latest facts) and GUARD (one status line). Wide panes (120+ cells)
show two columns: INBOX on the left, the other blocks on the right.

Golden Hour theme: Home leaves room for the city (art_city). A wide pane (130+ cells) gives the
city a 48-column window on the right of the whole pane (Frame.art = ("window", 48)) and packs the
blocks into the rest; a narrow pane with 30+ body rows gets a strip of city under the blocks
(("strip", row, rows)). The s key cycles the sky (the "sky" command; the TUI keeps the mode).

View protocol (same for home_view, memory_view, guard_view):
    load(panels: shell.Panels) -> HomeData          called on every data refresh (10 s, r key)
    build_home(data, state, width, height) -> Frame pure; state is this tab's dict kept by the TUI
    handle_key(ch, state, data) -> Optional[str]    updates state; may return a shell command
    handle_click(target, state, data) -> Optional[str]

State keys: sel (selected mail), sel_key (that mail's key; the selection follows the mail when a
refresh adds rows above it), scroll, open (longer preview), mask (set by the TUI from the Money
tab's hide-amounts toggle; hides the MONEY amounts here too).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import calendar
import time
from typing import Any, Dict, List, Optional, Tuple

from telemachus.cockpit import theme as theme_mod
from telemachus.cockpit import view_common as vc
from telemachus.cockpit.finance_view import mask_badge, setup_error_lines
from telemachus.cockpit.shell import Frame, KeyGroup, KeyHint, Panels
from telemachus.cockpit.theme import GLYPHS, GUTTER, Line, cell_width, clip, fit, line_text, line_width, lpad, money_text, rpad, section

LIST_KEYS = KeyGroup.of(
    "LISTS",
    KeyHint("j/k", "move", "move through the inbox"),
    KeyHint("↵", "open", "longer preview of the selected mail"),
)
TRIAGE_KEY = KeyHint("t", "triage", "triage email in OpenCode (right pane)")
SKY_KEY = KeyHint("s", "sky", "change the city sky: my clock, golden hour, dusk, night, day")

# Room for the Golden Hour city (see the module docstring).
ART_WINDOW_MIN = 130  # body width from which the city gets a window on the right
ART_WINDOW_COLS = 48
ART_GAP = 2  # blank columns between the blocks and the window
TWO_COLUMN_ART_MIN = 96  # next to the window, two columns from this content width
ART_LEFT_W = 56  # left column next to the window: keeps the inbox sender column (54+ inner cells)
ART_STRIP_MIN_ROWS = 30  # body rows from which a narrow pane gets a strip of city under Home
ART_STRIP_MAX_ROWS = 14
ART_STRIP_KEEP_ROWS = 22  # body rows the blocks always keep above the strip
HOME_KEYS: List[KeyGroup] = [LIST_KEYS, KeyGroup.of("HOME", TRIAGE_KEY)]


def home_keys(d: "HomeData", mask: bool = False) -> List[KeyGroup]:
    """Footer and help keys for what Home shows now: list keys only when there is mail to move
    through, t when mail is indexed, m (same key as on Money) when the MONEY block has amounts."""
    has_mail = bool(_inbox(d))
    groups: List[KeyGroup] = [LIST_KEYS] if has_mail else []
    hints: List[KeyHint] = []
    if has_mail or (d.email_stats or {}).get("total"):
        hints.append(TRIAGE_KEY)
    if (d.money or {}).get("available"):
        hints.append(KeyHint("m", "show $" if mask else "hide $", "show or hide amounts (Money too)"))
    if theme_mod.art_enabled():
        hints.append(SKY_KEY)
    if hints:
        groups.append(KeyGroup.of("HOME", *hints))
    return groups

KIND_WORD = {"urgent": "URGENT", "action": "ACTION", "fyi": "FYI"}
KIND_STYLE = {"urgent": "red+b", "action": "yellow", "fyi": "blue"}
BADGE_KIND = {"URGENT": "urgent", "ACTION": "action", "DIGEST": "fyi", "INFO": "fyi"}
TIER_WORD = {"private": "PRIVATE", "project": "PROJECT", "public": "PUBLIC"}
TIER_STYLE = {"private": "magenta", "project": "blue", "public": "green"}

TWO_COLUMN_MIN = 120  # body width at which the blocks go side by side
KIND_RANK = {"urgent": 0, "action": 1, "fyi": 2, "spam": 3}
FACTS_LOADED = 40  # enough to fill a tall two-column Home


@dataclass
class HomeData:
    emails: List[Dict[str, Any]] = field(default_factory=list)  # EmailTriagePanel.get_triage_items()
    email_stats: Dict[str, int] = field(default_factory=dict)  # EmailTriagePanel.get_summary_stats()
    email_index_found: bool = True  # False: no index file yet (empty state with the index command)
    email_error: bool = False  # True: the index has mail but the list query failed
    guard_status: Dict[str, Any] = field(default_factory=dict)  # SafetyMemoryPanel.get_guard_status()
    memory_stats: Dict[str, Any] = field(default_factory=dict)  # SafetyMemoryPanel.get_memory_stats()
    memory_events: List[Dict[str, Any]] = field(default_factory=list)  # SafetyMemoryPanel.get_recent_events()
    facts: List[Dict[str, Any]] = field(default_factory=list)  # SafetyMemoryPanel.get_recent_facts()
    money: Optional[Dict[str, Any]] = None  # money_summary(); None = not loaded (block hidden)
    now: float = 0.0


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def _short_bill_name(name: str) -> str:
    """'PG&E Electric & Gas' -> 'PG&E', 'Credit Card (Visa)' -> 'Credit Card'."""
    name = " ".join(str(name or "").split("(")[0].split())
    name = "".join(ch for ch in name if cell_width(ch) == 1 or ch.isalnum()).strip()
    if cell_width(name) > 14:
        name = name.split(" ")[0]
    return name or "Bill"


def _due_date(day: int, today: date) -> date:
    """Next date with this day of the month (today counts), clamped to short months."""
    last = calendar.monthrange(today.year, today.month)[1]
    d = date(today.year, today.month, min(max(1, day), last))
    if d >= today:
        return d
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return date(y, m, min(max(1, day), calendar.monthrange(y, m)[1]))


LIST_BILLS_MAX = 4  # bill rows in the two-column Home; the rest become "+N more"


def next_month(today: date) -> str:
    """'YYYY-MM' of the month after `today` (the month a bill falls in once this month's due day passed)."""
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return f"{y:04d}-{m:02d}"


def _status_by_key(breakdown: Optional[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for b in (breakdown or {}).get("bills", []) or []:
        if isinstance(b, dict):
            out[str(b.get("key") or b.get("name") or "")] = str(b.get("status") or "")
    return out


def money_summary(runway: Any, breakdown: Optional[Dict[str, Any]], card: Optional[Dict[str, Any]],
                  today: date, mask: bool = False,
                  next_breakdown: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The one-line MONEY block, from FinanceTuiPanel data. Pure.

    available=False when there is no paycheck plan (runway is None).
    bills: every unpaid active bill with an amount (card handled apart), soonest first, as dicts
    {name, cents, date ("YYYY-MM-DD"), before_payday}. The view shows only the ones before payday.
    breakdown is this month's bills breakdown; its status is for this month's due date. A bill whose
    next due date is next month takes its status from next_breakdown (next month's breakdown), so a
    bill paid early is not listed as due.
    """
    if runway is None:
        return {"available": False, "mask": mask}
    payday = datetime.strptime(runway.next_paycheck, "%Y-%m-%d").date()
    later_status = _status_by_key(next_breakdown)
    bills: List[Dict[str, Any]] = []
    for b in (breakdown or {}).get("bills", []) or []:
        try:
            if not b.get("is_active", True) or int(b.get("amount_cents") or 0) <= 0:
                continue
            if b.get("key") == "credit_card" or "card" in str(b.get("category", "")).lower():
                continue
            due = _due_date(int(b.get("due_day") or 1), today)
            if due.month == today.month and due.year == today.year:
                status = b.get("status")
            else:
                status = later_status.get(str(b.get("key") or b.get("name") or ""))
            if status == "paid":
                continue
            bills.append({
                "name": _short_bill_name(b.get("name", "")),
                "cents": int(b["amount_cents"]),
                "date": due.isoformat(),
                "before_payday": due < payday,
            })
        except (TypeError, ValueError, KeyError):
            continue
    bills.sort(key=lambda x: x["date"])
    owed = None
    if card and card.get("status") == "owed" and int(card.get("owed_cents") or 0) > 0:
        owed = {
            "cents": int(card["owed_cents"]),
            "due": card.get("next_due_date"),
            "before_payday": bool(card.get("due_before_payday")),
            # build_card_summary sets a past due date when the payment is overdue
            "overdue": bool(card.get("next_due_date")) and str(card.get("next_due_date")) < today.isoformat(),
        }
    return {
        "available": True,
        "mask": mask,
        "left_cents": int(runway.left_cents),
        "per_day_cents": int(runway.per_day_cents),
        "days_left": int(runway.days_left),
        "payday": runway.next_paycheck,
        "bills": bills,
        "card": owed,
    }


def _has_finance_data(fp: Any) -> bool:
    """False for a brand-new vault (no setup.yaml and no entries): the runway would only show
    defaults, so Home shows the setup hint instead."""
    store = getattr(fp, "store", None)
    get_setup = getattr(store, "get_setup_yaml_path", None)
    try:
        if callable(get_setup) and get_setup():
            return True
        return bool(fp.get_daily_ledger())
    except Exception:
        return True


def load(panels: Panels) -> HomeData:
    """Reads everything the Home tab shows. Called by the TUI on each data refresh."""
    ep, sp, fp = panels.email_panel, panels.safety_panel, panels.finance_panel
    money: Optional[Dict[str, Any]] = None
    if fp is not None:
        try:
            get_card = getattr(fp, "get_card_summary", None)
            get_err = getattr(fp, "get_setup_error", None)
            setup_error = get_err() if callable(get_err) else None
            today = date.today()
            runway = fp.get_runway() if _has_finance_data(fp) else None
            later = None
            if runway is not None:
                try:
                    later = fp.get_monthly_bills_breakdown(month=next_month(today))
                except Exception:
                    later = None
            money = money_summary(
                runway,
                fp.get_monthly_bills_breakdown(),
                get_card() if callable(get_card) else None,
                today,
                mask=bool(getattr(panels, "mask", False)),
                next_breakdown=later,
            )
            if setup_error and isinstance(setup_error, str):
                money["setup_error"] = setup_error
        except Exception:
            money = {"available": False, "error": True}
    exists = getattr(ep, "index_exists", None)
    get_facts = getattr(sp, "get_recent_facts", None)
    emails = list(ep.get_triage_items(limit=60))
    return HomeData(
        emails=emails,
        email_error=bool(getattr(ep, "last_error", None)),
        email_stats=dict(ep.get_summary_stats()),
        email_index_found=bool(exists()) if callable(exists) else True,
        guard_status=dict(sp.get_guard_status()),
        memory_stats=dict(sp.get_memory_stats()),
        memory_events=list(sp.get_recent_events(limit=8)),
        facts=list(get_facts(limit=FACTS_LOADED)) if callable(get_facts) else [],
        money=money,
        now=time.time(),
    )


def _kind(item: Dict[str, Any]) -> str:
    return item.get("kind") or BADGE_KIND.get(str(item.get("badge", "")), "fyi")


def _epoch(item: Dict[str, Any]) -> float:
    return vc.safe_ts(item.get("epoch")) or 0.0  # bad times sort last, never crash


def _inbox(d: HomeData) -> List[Dict[str, Any]]:
    """Mail the Home tab lists: everything but spam, urgent then action then FYI, newest first
    inside each group (the age column then reads in order)."""
    mail = [e for e in d.emails if _kind(e) != "spam"]
    return sorted(mail, key=lambda e: (KIND_RANK.get(_kind(e), 2), -_epoch(e)))


def _mail_key(item: Dict[str, Any]) -> str:
    """Stable id of one mail row (the index has no single unique column for it)."""
    return "|".join(str(item.get(k, "")) for k in ("uid", "epoch", "sender", "subject"))


def _selection(state: Dict[str, Any], keys: List[str]) -> Tuple[int, bool]:
    """(selected index, lost). The selection follows the selected mail by its key, so mail that
    arrives above it on a refresh does not move the highlight. lost = that mail is gone; the
    index is then kept (clamped) and the open preview should close."""
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


# ---------------------------------------------------------------------------
# Keys and clicks
# ---------------------------------------------------------------------------


def handle_key(ch: int, state: Dict[str, Any], d: HomeData) -> Optional[str]:
    """j/k move the mail selection, enter opens a longer preview, t sends a triage task to OpenCode,
    m hides or shows the MONEY amounts (the "mask" command; the TUI keeps one toggle for Home and Money)."""
    keys = [_mail_key(e) for e in _inbox(d)]
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
    if ch in (ord("t"), ord("T")):
        return "triage"
    if ch in (ord("m"), ord("M")):
        return "mask"
    if ch in (ord("s"), ord("S")) and theme_mod.art_enabled():
        return "sky"
    return None


def handle_click(target: Tuple[str, int], state: Dict[str, Any], d: HomeData) -> Optional[str]:
    """One click selects a mail; a click on the selected one opens or closes its preview."""
    kind, idx = target
    if kind == "email":
        keys = [_mail_key(e) for e in _inbox(d)]
        if not 0 <= idx < len(keys):
            return None
        if _selection(state, keys)[0] == idx:
            state["open"] = not state.get("open", False)
        else:
            state["open"] = False
        _select(state, keys, idx)
    return None


# ---------------------------------------------------------------------------
# Blocks (each returns lines sized to `width`; the gutter is included)
# ---------------------------------------------------------------------------


def _day_word(day_iso: str, today: date) -> str:
    d = date.fromisoformat(day_iso)
    delta = (d - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if 1 < delta < 7:
        return d.strftime("%a")
    return d.strftime("%b ") + str(d.day)


def _payday_words(payday_iso: str, today: date) -> str:
    d = date.fromisoformat(payday_iso)
    if d == today:
        return "payday today"
    return "until payday " + d.strftime("%a %b ") + str(d.day)


def _fit_first(head: Line, tails: List[Line], width: int) -> Line:
    """head + the first tail that fits; head alone when none does; head cut with … when even
    that is too wide. Never cuts a line mid-word without a mark."""
    for tail in tails:
        if line_width(head) + line_width(tail) <= width:
            return fit(head + tail, width)
    if line_width(head) <= width:
        return fit(head, width)
    return fit(fit(head, max(0, width - 1)) + [("…", "muted")], width)


def _money_items(m: Dict[str, Any], today: date, mask: bool) -> List[Dict[str, Any]]:
    """Bills and the card that are due before payday (the section says "until payday"), soonest
    first. Each item: name, amount, word ("today", "Sun", "Mon", "owed"), word_style, cents, card.
    Every item uses the same day word; the card has no extra "due"."""
    items: List[Tuple[str, int, Dict[str, Any]]] = []
    for b in m.get("bills", []):
        if not b.get("before_payday", True):
            continue
        word = _day_word(b["date"], today)
        items.append((b["date"], 0, {
            "name": b["name"], "amount": money_text(b["cents"], mask), "word": word,
            "word_style": "yellow" if word in ("today", "tomorrow") else "muted",
            "cents": int(b["cents"]), "card": False,
        }))
    card = m.get("card")
    if card and (card.get("before_payday") or not card.get("due")):
        if card.get("due") and card.get("overdue"):
            d = date.fromisoformat(str(card["due"]))
            word = "overdue since " + d.strftime("%a %b ") + str(d.day)
            style = "yellow"
            key = str(card["due"])
        elif card.get("due"):
            word = _day_word(str(card["due"]), today)
            style = "yellow"
            key = str(card["due"])
        else:
            word, style, key = "owed", "muted", "9999-12-31"
        items.append((key, 1, {"name": "Visa", "amount": money_text(card["cents"], mask),
                               "word": word, "word_style": style,
                               "cents": int(card["cents"]), "card": True}))
    items.sort(key=lambda t: (t[0], t[1]))
    return [it for _, _, it in items]


def _priority(items: List[Dict[str, Any]]) -> List[int]:
    """Indexes of `items` in the order they earn a place on a short line: the card, then the
    biggest amount, then the soonest. The line still shows the chosen ones in date order."""
    return sorted(range(len(items)), key=lambda i: (not items[i]["card"], -items[i]["cents"], i))


def _money_title(width: int, payday: str, mask: bool) -> Line:
    """'MONEY ──── payday words', with the yellow '● amounts hidden' badge at the end when the
    amounts are hidden (the same badge as on the Money tab). Narrow: the badge wins over the words."""
    badge, style = mask_badge(mask)
    if not badge:
        return section("MONEY", width, payday)
    tail: Line = [("  " + badge, style)]
    room = width - line_width(tail)
    # section() keeps the trailing words when at least 2 dashes fit; else drop the words.
    keeps = room - len(GUTTER) - cell_width("MONEY ") - (2 + cell_width(payday)) >= 2
    return fit(section("MONEY", room, payday if keeps else "") + tail, width)


def money_block(m: Optional[Dict[str, Any]], width: int, today: date, mask: Optional[bool] = None,
                list_bills: bool = False) -> List[Line]:
    """mask: state["mask"] from the TUI (the m key) wins over the mask stored at load time.

    list_bills: one bill per row with amounts right-aligned (two-column Home has the room);
    otherwise the bills share one "next" line and the ones that do not fit become "+N more".
    """
    if m is None:
        return []
    if not m.get("available"):
        if m.get("setup_error"):
            return [section("MONEY", width)] + setup_error_lines(str(m["setup_error"]), width)
        if m.get("error"):
            return [section("MONEY", width), fit([(GUTTER + "Could not read the finance vault.", "yellow")], width)]
        return [section("MONEY", width), []] + vc.empty_state(
            "No finance data yet.", width, "python3 -m telemachus.finance setup", after="  to start.")
    mask = bool(m.get("mask")) if mask is None else bool(mask)
    lines: List[Line] = [_money_title(width, _payday_words(m["payday"], today), mask)]
    left = m["left_cents"]
    days = m["days_left"]
    day_word = "day" if days == 1 else "days"
    if left >= 0:
        head: Line = [(GUTTER, "text"), (money_text(left, mask), "text+b"), (" left after bills", "muted")]
        per_day = money_text(m["per_day_cents"], mask)
        tails = [
            [("  ·  ", "dim"), (per_day, "text"), (f" a day for {days} {day_word}", "muted")],
            [("  ·  ", "dim"), (per_day, "text"), (" a day", "muted")],
        ]
    else:
        head = [(GUTTER, "text"), (money_text(-left, mask), "red+b"), (" short after bills", "red")]
        tails = [[("  ·  ", "dim"), (f"{days} {day_word} to payday", "muted")]]
    lines.append(_fit_first(head, tails, width))

    items = _money_items(m, today, mask)
    label = GUTTER + "next   "
    if not items:
        lines.append(fit([(label, "muted"), ("no bills before payday", "muted")], width))
        return lines
    if list_bills:
        rows_max = LIST_BILLS_MAX
        keep = items if len(items) <= rows_max else [
            items[i] for i in sorted(_priority(items)[: rows_max - 1])]
        name_w = min(16, max(cell_width(it["name"]) for it in keep))
        amt_w = max(cell_width(it["amount"]) for it in keep)
        for i, it in enumerate(keep):
            row: Line = [(label if i == 0 else " " * cell_width(label), "muted"),
                         (rpad(_ellipsize(it["name"], name_w), name_w) + "  ", "text"),
                         (lpad(it["amount"], amt_w), "text"),
                         ("   " + it["word"], it["word_style"])]
            lines.append(_fit_first(row, [], width))
        if len(keep) < len(items):
            lines.append(fit([(" " * cell_width(label) + f"+{len(items) - len(keep)} more", "muted")], width))
        return lines

    def seg(it: Dict[str, Any]) -> Line:
        return [(it["name"] + " ", "text"), (it["amount"], "text"), (" " + it["word"], it["word_style"])]

    def line_for(chosen: List[int]) -> Line:
        out: Line = [(label, "muted")]
        for n, i in enumerate(sorted(chosen)):  # date order on screen
            out += ([("   ·   ", "dim")] if n else []) + seg(items[i])
        if len(chosen) < len(items):
            out.append((f"   +{len(items) - len(chosen)} more", "muted"))
        return out

    # the card and big bills claim a place first, so a narrow line never hides them in "+N more"
    chosen: List[int] = []
    for i in _priority(items):
        if line_width(line_for(chosen + [i])) <= width:
            chosen.append(i)
    if not chosen:  # not even one bill fits: cut the most important one with …
        it = items[_priority(items)[0]]
        return lines + [_fit_first([(label, "muted"), (it["name"] + " ", "text"), (it["amount"], "text")], [], width)]
    lines.append(fit(line_for(chosen), width))
    return lines


def _inbox_trailing(d: HomeData, n: int) -> str:
    st = d.email_stats or {}
    u = st.get("kind_urgent", st.get("urgent", 0)) or 0
    a = st.get("kind_action", st.get("action", 0)) or 0
    parts = []
    if u:
        parts.append(f"{u} urgent")
    if a:
        parts.append(f"{a} action")
    total = st.get("total", 0) or n
    parts.append(f"{total:,} indexed" if parts else f"{total:,} messages")
    return " · ".join(parts)


def _ellipsize(text: str, width: int) -> str:
    """Cuts text to `width` cells with a trailing … when it does not fit."""
    if cell_width(text) <= width:
        return text
    return clip(text, max(0, width - 1)) + "…"


def _email_row(item: Dict[str, Any], width: int, selected: bool, now: float) -> Line:
    kind = _kind(item)
    word = KIND_WORD.get(kind, "FYI")
    age = vc.ago(item.get("epoch"), now) if item.get("epoch") else ""
    if not age:
        age = str(item.get("date", ""))[5:10] if item.get("date") else ""
    inner = width - 2
    sender_w = 18 if inner >= 70 else (13 if inner >= 54 else 0)
    age_w = 5 if inner >= 46 and age else 0
    subj_w = max(1, inner - 8 - sender_w - age_w)
    style = "text+b" if selected else "text"
    body: Line = [(rpad(word, 6) + "  ", KIND_STYLE.get(kind, "blue"))]
    if sender_w:
        body.append((rpad(_ellipsize(str(item.get("sender", "")), sender_w - 2), sender_w - 2) + "  ", style))
    body.append((rpad(_ellipsize(str(item.get("subject", "")), subj_w), subj_w), style))
    if age_w:
        body.append((lpad(age, age_w), "text" if selected else "muted"))
    return vc.row(body, width, selected)


def inbox_block(d: HomeData, state: Dict[str, Any], width: int, rows: int, preview_lines: int,
                base: int) -> Tuple[List[Line], Dict[int, Tuple[str, int]], int]:
    """(lines, click rows, scroll offset). `rows` = mail rows to show."""
    inbox = _inbox(d)
    lines: List[Line] = [section("INBOX", width, _inbox_trailing(d, len(inbox)))]
    hits: Dict[int, Tuple[str, int]] = {}
    if not inbox:
        lines.append([])
        if d.email_error:
            # The index counts mail but the list could not be read: say so instead of "nothing".
            lines.append(vc.centered([("Could not read the mail list.", "yellow")], width))
            lines.append(vc.centered([("The email index may be damaged or from an older version.", "muted")], width))
        elif not d.email_index_found or not (d.email_stats or {}).get("total"):
            lines += vc.empty_state("No mail indexed yet.", width, "python3 -m telemachus.email index",
                                    after="  to fill it.")
        else:
            lines += vc.empty_state("Nothing to triage.", width)
        return lines, hits, 0
    sel, offset = vc.window(len(inbox), state.get("sel", 0), state.get("scroll", 0), max(1, rows))
    now = vc.now_or(d.now)
    for i, item in enumerate(inbox[offset: offset + max(1, rows)]):
        idx = offset + i
        hits[base + len(lines)] = ("email", idx)
        lines.append(_email_row(item, width, idx == sel, now))
    if preview_lines > 0:
        item = inbox[sel]
        indent = 10  # under the sender column
        text = str(item.get("snippet") or "").strip() or "No preview available."
        if state.get("open"):
            head = f"From {item.get('sender', '')}"
            sent = vc.safe_dt(item.get("epoch"))
            if sent is not None:
                head += "   " + sent.strftime("%a %b %d %H:%M")
            elif item.get("date"):
                head += f"   {item.get('date')}"
            text = f"{item.get('subject', '')}.  {text}"
            lines.append(fit([(" " * indent, "text"), (head, "muted")], width))
            body = vc.wrap(text, width - indent - 2, max(1, preview_lines - 1))
        else:
            body = vc.wrap(text, width - indent - 2, preview_lines)
        for ln in body:
            lines.append(fit([(" " * indent + ln, "muted" if not state.get("open") else "text")], width))
    return lines, hits, offset


def _time_short(ts: Optional[float], now: float) -> str:
    return vc.clock(ts, now)


def memory_block(d: HomeData, width: int, rows: int) -> List[Line]:
    st = d.memory_stats or {}
    available = st.get("available", True)
    total = int(st.get("total_memories") or 0) if available else None
    trailing = f"{total:,} facts" if isinstance(total, int) else "vault not readable"
    lines: List[Line] = [section("MEMORY", width, trailing)]
    if not available:
        lines.append(fit([(GUTTER + "The memory vault could not be read.", "yellow")], width))
        return lines
    if not d.facts:
        lines.append([])
        lines += vc.empty_state("No memories yet.", width, extra="Facts appear here after your first OpenCode session.")
        return lines
    now = vc.now_or(d.now)
    room = width - 2 - 7 - 9
    for f in d.facts[: max(0, rows)]:
        tier = f.get("tier", "private")
        text = str(f.get("text", ""))
        if cell_width(text) > room:
            text = clip(text, max(0, room - 1)).rstrip() + "…"
        lines.append(fit([
            (GUTTER + _time_short(f.get("ts"), now) + "  ", "muted"),
            (rpad(TIER_WORD.get(tier, "PRIVATE"), 7) + "  ", TIER_STYLE.get(tier, "magenta")),
            (text, "text"),
        ], width))
    return lines


def guard_line(g: Dict[str, Any], width: int, now: float) -> Line:
    checks = vc.guard_checks(g, now)
    on = sum(1 for c in checks if c["on"])
    log = g.get("log") or {}
    parts: Line = [(GUTTER, "text")]
    if on == len(checks):
        parts += [(GLYPHS["ok"] + " ", "green"), ("all checks on", "text")]
    else:
        parts += [(GLYPHS["off"] + " ", "yellow"), (f"{on} of {len(checks)} checks on", "yellow")]
    extras: List[Line] = [
        [(str(log.get("blocked_today") or 0), "text"), (" blocked today", "muted")],
        [(str(log.get("trash_today") or 0), "text"), (" trashed today", "muted")],
    ]
    cp_words = vc.ago_words(log.get("last_checkpoint_ts"), now)  # "12m ago", "just now"
    extras.append([("last checkpoint ", "muted"), (cp_words, "text")] if cp_words
                  else [("no checkpoint yet", "muted")])
    for ex in extras:
        if line_width(parts) + 3 + line_width(ex) > width:
            break
        parts += [("   ", "text")] + ex
    return fit(parts, width)


def guard_block(d: HomeData, width: int) -> List[Line]:
    return [section("GUARD", width), guard_line(d.guard_status or {}, width, vc.now_or(d.now))]


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def _grow(avail: int, wants: List[Tuple[str, int]], caps: Dict[str, int]) -> Dict[str, int]:
    """Gives rows to blocks in order of `wants` (name, cap step) until `avail` runs out."""
    got = {k: 0 for k in caps}
    for name, step in wants:
        target = min(caps[name], step)
        add = max(0, min(target - got[name], avail))
        got[name] += add
        avail -= add
    return got


def _single_column(d: HomeData, state: Dict[str, Any], width: int, height: int, today: date):
    inbox = _inbox(d)
    money = money_block(d.money, width, today, state.get("mask"))
    guard = guard_block(d, width)
    n_mail = len(inbox)
    n_facts = len(d.facts) if (d.memory_stats or {}).get("available", True) else 1
    preview = 2 if n_mail else 0
    if state.get("open") and n_mail:
        preview = 6
    inbox_fixed = 1 + (preview if n_mail else 5)
    mem_fixed = 1 if d.facts else (5 if (d.memory_stats or {}).get("available", True) else 2)
    show_money, show_mem, show_guard = bool(money), True, True

    def fixed() -> int:
        blocks = [show_money, True, show_mem, show_guard]
        gaps = max(0, sum(blocks) - 1)
        return ((len(money) if show_money else 0) + inbox_fixed + (mem_fixed if show_mem else 0)
                + (len(guard) if show_guard else 0) + gaps)

    avail = height - fixed()
    if avail < 2 and show_mem:
        show_mem = False
        avail = height - fixed()
    if avail < 1 and preview > 1:
        inbox_fixed -= preview - 1
        preview = 1
        avail = height - fixed()
    if avail < 1 and show_guard:
        show_guard = False
        avail = height - fixed()
    caps = {"mail": n_mail, "mem": (n_facts if d.facts else 0) if show_mem else 0}
    caps_mem_all = caps["mem"]
    got = _grow(max(0, avail), [("mail", 3), ("mem", 2), ("mail", 8), ("mem", 4), ("mail", 15),
                                ("mem", 8), ("mail", n_mail), ("mem", caps_mem_all)], caps)

    lines: List[Line] = []
    hits: Dict[int, Tuple[str, int]] = {}
    offset = 0
    if show_money:
        lines += money
        lines.append([])
    block, h, offset = inbox_block(d, state, width, max(1, got["mail"]), preview, len(lines))
    hits.update(h)
    lines += block
    if show_mem:
        lines.append([])
        lines += memory_block(d, width, got["mem"])
    if show_guard:
        lines.append([])
        lines += guard
    return lines, hits, offset


def _two_columns(d: HomeData, state: Dict[str, Any], width: int, height: int, today: date,
                 left_w: Optional[int] = None):
    """Left (wider): INBOX, then GUARD (its line needs the width). Right: MONEY with one bill per
    row, then MEMORY filling the rest of the height. `left_w` overrides the 11:9 split."""
    gap = 3
    left_w = left_w if left_w else (width - gap) * 11 // 20
    right_w = width - gap - left_w
    inbox = _inbox(d)
    preview = 0
    if inbox:
        preview = 6 if state.get("open") else 2
    guard = guard_block(d, left_w)
    rows = max(1, height - 1 - preview - 1 - len(guard))
    left, hits, offset = inbox_block(d, state, left_w, min(len(inbox), rows) if inbox else 1, preview, 0)
    left += [[]] + guard

    right: List[Line] = []
    money = money_block(d.money, right_w, today, state.get("mask"), list_bills=True)
    if money:
        right += money + [[]]
    mem_rows = max(0, height - len(right) - 1)
    right += memory_block(d, right_w, mem_rows)

    lines: List[Line] = []
    for i in range(min(height, max(len(left), len(right)))):
        l = fit(left[i] if i < len(left) else [], left_w)
        r = fit(right[i] if i < len(right) else [], right_w)
        lines.append(l + [(" " * gap, "text")] + r)
    return lines, hits, offset


def build_home(d: HomeData, state: Optional[Dict[str, Any]], width: int, height: int) -> Frame:
    """Home body. `width` includes the 2-cell gutter; `height` is the number of body rows."""
    state = dict(state) if state is not None else {}
    keys = [_mail_key(e) for e in _inbox(d)]
    sel, lost = _selection(state, keys)
    _select(state, keys, sel)
    if lost:
        state["open"] = False
    today = datetime.fromtimestamp(vc.now_or(d.now)).date()
    art = art_layout(width, height)
    content_w, content_h = width, height
    if art and art[0] == "window":
        content_w = width - art[1] - ART_GAP
    elif art and art[0] == "strip":
        content_h = art[1] - 1
    if content_w >= TWO_COLUMN_MIN:
        lines, hits, offset = _two_columns(d, state, content_w, content_h, today)
    elif art and art[0] == "window" and content_w >= TWO_COLUMN_ART_MIN:
        lines, hits, offset = _two_columns(d, state, content_w, content_h, today, left_w=ART_LEFT_W)
    else:
        lines, hits, offset = _single_column(d, state, content_w, content_h, today)
    lines = [fit(l, content_w) for l in lines[: max(0, content_h)]]
    if art and art[0] == "strip":
        # The strip starts one row under the last block and fills the pane: a short Home gets more city.
        used = len(lines)
        while used and not line_text(lines[used - 1]).strip():
            used -= 1
        lines = lines[:used]
        row = used + 1
        art = ("strip", row, height - row)
    out = {"scroll": offset, "sel": sel, "sel_key": state.get("sel_key")}
    if lost:
        out["open"] = False
    return Frame(lines=lines, keys=home_keys(d, bool(state.get("mask"))),
                 rows={k: v for k, v in hits.items() if k < content_h}, state=out, art=art)


def art_layout(width: int, height: int) -> Optional[Tuple[Any, ...]]:
    """Where Home puts the theme's city for a body of width x height cells (see Frame.art), or None
    when the theme has no art or the pane is too small for it."""
    if not theme_mod.art_enabled():
        return None
    if width >= ART_WINDOW_MIN:
        return ("window", ART_WINDOW_COLS)
    if height >= ART_STRIP_MIN_ROWS:
        rows = min(ART_STRIP_MAX_ROWS, height - ART_STRIP_KEEP_ROWS)
        return ("strip", height - rows, rows)
    return None


DATA_CLASS = HomeData
build = build_home
