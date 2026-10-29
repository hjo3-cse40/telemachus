"""Text Document & Historical Bullet List Importer for Telemachus Finances.

Parses text notes, daily logs, and multi-month historical lists into balanced
double-entry SQLite transactions. Supports Notion bullet format with +/- syntax.
"""
from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import unicodedata

from telemachus.finance.export import FinanceExporter
from telemachus.finance.models import Transaction
from telemachus.finance.store import FinanceStore

logger = logging.getLogger(__name__)

MONTH_NAMES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def parse_date_header(line: str, default_year: int = 2026) -> Optional[str]:
    """Attempts to match a date header line and returns YYYY-MM-DD or None.
    
    Robustly supports:
    - Day/Month format: 23/10, 15/8, 15/08, 01/07, 1/7
    - Day/Month/Year: 23/10/2026, 15/8/26, 15-08-2026, 23.10.2026
    - Standard ISO: 2026-10-23, 2026/10/23
    - Day Month name: 23 Oct, 23rd October, 15 August 2026
    - Month Day: July 15, October 23, 2026
    - Prefixes: Day of week (Monday 23/10), markdown headers (## 15/08:), bullets, brackets
    """
    line = line.strip()
    if not line:
        return None

    # Strip markdown headers, bullets, asterisks, brackets, quotes
    clean = re.sub(r"^[#*•\-_–—\s\[\(]+", "", line).strip()
    clean = re.sub(r"[:\s*\]\)]+$", "", clean).strip()

    # Strip day of week prefixes: 'Monday', 'Mon', 'Monday,', 'Mon.'
    clean = re.sub(
        r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[.,]?\s*",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip()

    # 1. YYYY-MM-DD or YYYY/MM/DD
    m1 = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", clean)
    if m1:
        y, m, d = int(m1.group(1)), int(m1.group(2)), int(m1.group(3))
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"

    # 2. MonthName with Day or Day with MonthName (e.g. September 12, 12/September, Sep 12, 12 Sep)
    # Case A: MonthName first (e.g. September/12, September 12, Sep 12, 2026)
    m_md = re.match(r"^([A-Za-z]+)[-/.\s]+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*[-/.\s]*(\d{2,4}))?$", clean)
    if m_md:
        m_name = m_md.group(1).lower()
        d = int(m_md.group(2))
        m = MONTH_NAMES.get(m_name)
        if m and 1 <= d <= 31:
            raw_y = m_md.group(3)
            y = (int(raw_y) + 2000 if int(raw_y) < 100 else int(raw_y)) if raw_y else default_year
            return f"{y:04d}-{m:02d}-{d:02d}"

    # Case B: Day first with MonthName (e.g. 12/September, 12 Sep, 12-September, 12th August 2026)
    m_dm = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?[-/.\s]+([A-Za-z]+)(?:[-/.\s]+(\d{2,4}))?$", clean)
    if m_dm:
        d = int(m_dm.group(1))
        m_name = m_dm.group(2).lower()
        m = MONTH_NAMES.get(m_name)
        if m and 1 <= d <= 31:
            raw_y = m_dm.group(3)
            y = (int(raw_y) + 2000 if int(raw_y) < 100 else int(raw_y)) if raw_y else default_year
            return f"{y:04d}-{m:02d}-{d:02d}"

    # 3. Numeric dates with year: e.g. 7/30/2026, 09/12/2026, 10/23/26
    m_y = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$", clean)
    if m_y:
        n1, n2, raw_y = int(m_y.group(1)), int(m_y.group(2)), int(m_y.group(3))
        y = (2000 + raw_y) if raw_y < 100 else raw_y
        # MM/DD/YYYY format (standard for 7/30 to 10/23 range)
        if 1 <= n1 <= 12 and 1 <= n2 <= 31:
            return f"{y:04d}-{n1:02d}-{n2:02d}"
        elif 1 <= n2 <= 12 and 1 <= n1 <= 31:
            return f"{y:04d}-{n2:02d}-{n1:02d}"

    # 4. Numeric Month/Day without year: e.g. 7/30, 8/1, 9/12, 10/12, 10/23
    m_num = re.match(r"^(\d{1,2})[-/.](\d{1,2})$", clean)
    if m_num:
        n1, n2 = int(m_num.group(1)), int(m_num.group(2))
        # MM/DD format: n1 is Month (7, 8, 9, 10), n2 is Day (1 to 31)
        if 1 <= n1 <= 12 and 1 <= n2 <= 31:
            return f"{default_year:04d}-{n1:02d}-{n2:02d}"
        elif 1 <= n2 <= 12 and 1 <= n1 <= 31:
            # If n1 > 12, then n1 was day and n2 was month (e.g. 30/7)
            return f"{default_year:04d}-{n2:02d}-{n1:02d}"

    return None


# ==============================================================================
# Bullet line parsing (spend, card purchase, card payment, savings, income)
# ==============================================================================
#
# Cheat sheet (case does not matter; markers can go anywhere in the line):
#   - groceries - 25.50            spend from checking
#   - groceries - 25.50 visa       card purchase (also: card, cc, credit, @visa, (visa), (card),
#   visa: lunch 13                  on visa, on card, with card, paid with card, put on card)
#   visa payment 300               card payment from checking (also: credit card payment, cc payment,
#   pay visa 300                    card payment, pay off card, paid off visa, visa payoff, visa bill)
#   pay visa from savings 300      card payment from savings
#   savings + 500                  transfer checking -> savings
#   savings to checking 100        transfer savings -> checking (also: withdraw savings 50,
#                                   transfer from savings 100, moved 100 out of savings)
#   venmo 20 from savings          spend paid from savings
#   hysa interest + 3.50           interest paid into savings (income; checking does not change)
#   checking to visa 300           card payment (also: transfer to visa 300, visa from checking 300,
#                                   sent 300 to visa, bill pay visa 300, visa statement 300,
#                                   pay off both cards 300)
#   payday + 1480                  income to checking
#   rent 1200                      rent (marks the rent bill paid; other setup.yaml bills match
#                                   by their key or name in the cockpit, e.g. "phone bill 80")
#
# Tricky lines (decisions):
#   "credit card 300" / "- visa - 300": only card words plus an amount = a card payment
#     (nobody logs a purchase with no name; this keeps the old "credit card 300" meaning).
#   "cardamom 4", "visalia gas 30": card words must be whole words, so these are checking spends.
#   "credit union deposit 50": "credit union" is not the card; this is income.
#   "vegas card game 20", "gift card 25": "card game", "gift card", "birthday card"... are things
#     you buy, not the card. Add a marker to put them on the card: "gift card 25 on visa".
#   "visa fee 160", "visa application 160": a travel visa, not the card (add "on card" to charge it).
#   "debit", "on debit", "debit card": checking (the marker is removed from the name).
#   "refund + 20 visa", "visa refund 20", "returned shoes 40 visa", "visa + 20": card refunds
#     and credits are not supported yet (clear error). A checking refund needs a plus:
#     "amazon refund + 30".
#   "tip 15%", "¥1500 ramen", "1500 yen": not dollars (error, nothing is saved).
#   "deposit", "payday", "salary", "paycheck" mean income, except with a minus before the amount
#     or a debit marker ("- security deposit - 500"), a "thing" deposit ("security deposit 500",
#     "deposit for apartment 800") or "payday loan", "salary sacrifice". "- payday - 1480" is an
#     error: payday is money in.
#   "pay off card" with no amount: error, an amount is required.
#   "visa gift card 25", "prepaid visa card 25", "bus card 20", "starbucks card reload 25": things
#     you buy, paid from checking. Add "on visa" to charge them.
#   "groceries 25 paid visa": a purchase on the card. "paid visa 300" (pay word first) is a payment.
#
# Amounts (see _find_amount):
#   ".99", "$.99", "12.5", "12.5$" and "1,200.50" are read as written.
#   Dates ("10/24", "oct 2"), ordinals ("2nd"), times ("10:30") and numbers inside words
#     ("7-eleven", "h2o") are not amounts. A number that starts the line before a word is part
#     of the name ("99 ranch 40" is $40).
#   These need a fix (error, nothing is saved): two amounts ("dinner 30 split 15", "rent 1 200.50"),
#     a comma for cents ("12,5"), 3+ decimals ("25.555", "0.001"), broken numbers ("13.15.2",
#     "1e3", "25.5o"), counts ("coffee 10 x2") and $0.
#   Card payments may carry notes with numbers: counts ("for 2 months", "2 visa bills", "1 of 2")
#     and numbers in brackets are notes, so the one number left is the amount. When two plain
#     numbers are left ("pay visa 300 and 20"), it is an error.

CHECKING_ACCOUNT = "Assets:Checking:WellsFargo"
SAVINGS_ACCOUNT = "Assets:Savings:CapitalOne"
VISA_ACCOUNT = "Liabilities:CreditCard:Visa"
INTEREST_ACCOUNT = "Income:Interest"

_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])@?[A-Za-z][A-Za-z0-9&']*")
_RUN_RE = re.compile(r"[0-9.,]*[0-9][0-9.,]*")
_ORDINAL_RE = re.compile(r"(?:st|nd|rd|th)(?![A-Za-z])", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?|\.[0-9]{1,2}")

_CARD_WORDS = {"visa", "card", "cc", "credit", "creditcard", "visacard"}
_PAY_VERBS = {"pay", "paid", "paying", "pays", "payed", "autopay", "autopaid"}
_PAY_NOUNS = {"payment", "payments", "pmt", "pymt", "payoff", "paydown"}
_AFTER_VERB = {"off", "down", "the", "my", "back", "to", "toward", "towards", "in", "full", "entire", "whole", "balance", "of"}
_AFTER_NOUN = {"to", "for", "toward", "towards", "the", "my", "of", "off"}
_BEFORE_LEAD_NOUN = {"make", "made", "making", "send", "sent", "a", "the", "my"}
_BEFORE_PAY_NOUN = {"bill", "balance", "statement"}
_PREPOSITIONS = {"on", "with", "via", "using", "by", "w", "thru", "through"}
_PURCHASE_VERBS = {"paid", "pay", "charged", "charge", "put", "bought"}
_SAVINGS_RE = re.compile(r"saving|hysa|capital\s*one", re.IGNORECASE)
# Money leaving savings: "from savings", "savings to checking", "withdraw", "took out".
_SAV = r"(?:savings?|hysa|capital\s*one)"
_FROM_SAVINGS_RE = re.compile(
    rf"\b(?:from|out\s+of|outta|off)\s+(?:my\s+|the\s+)?{_SAV}"
    rf"|{_SAV}(?:\s+account)?\s*(?:to|->|→|=>|into)\s*(?:my\s+|the\s+)?(?:checking|wells|wf)\b"
    r"|\bwithdr[ae]w\w*|\b(?:took|take|taking)\s+out\b|\bpull(?:ed|ing)?\b",
    re.IGNORECASE,
)
# Words of a savings -> checking line that are not a payee.
_SAVINGS_MOVE_WORDS = {
    "from", "out", "of", "outta", "off", "my", "the", "savings", "saving", "hysa", "capital", "one",
    "to", "into", "checking", "wells", "fargo", "wf", "account", "back", "money", "cash", "a",
    "withdraw", "withdrew", "withdrawal", "withdrawals", "withdrawn", "withdrawing", "took", "take",
    "taking", "pull", "pulled", "pulling", "transfer", "transferred", "transfers", "xfer", "move",
    "moved", "moving",
}
_INCOME_WORDS = {"payday", "salary", "paycheck", "payroll"}
_DEPOSIT_WORDS = {"deposit", "deposited"}
# "payday loan", "salary sacrifice": money out.
_NOT_INCOME_NEXT = {"loan", "loans", "advance", "advances", "sacrifice", "deduction", "deductions", "fee", "fees"}
# "security deposit", "bottle deposit": a deposit you pay.
_DEPOSIT_OUT_PREV = {
    "security", "ticket", "tickets", "bottle", "bottles", "pet", "damage", "rental", "apartment", "holding",
    "booking", "reservation", "cleaning", "key", "can", "cans", "container", "crv", "down", "utility",
    "lease", "room", "hotel", "car", "bike", "equipment", "venue", "deposit",
}
_DEPOSIT_OUT_NEXT = {"for", "on", "at", "fee"}
_REFUND_WORDS = {"refund", "refunds", "refunded", "chargeback", "returned"}
# A number followed by one of these is a count, not the amount ("for 2 months", "2 visa bills").
_COUNT_WORDS = {
    "month", "months", "mo", "mos", "bill", "bills", "payment", "payments", "statement", "statements",
    "time", "times", "week", "weeks", "day", "days", "year", "years", "installment", "installments",
    "paycheck", "paychecks", "cards", "people", "items",
}

# "card" is not the credit card in these phrases.
_CARD_PREV_EXCLUDE = {
    "gift", "birthday", "bday", "greeting", "greetings", "sim", "sd", "memory", "graphics", "video",
    "business", "library", "post", "thank", "you", "playing", "game", "id", "report", "sound", "tarot",
    "pokemon", "trading", "xmas", "christmas", "holiday", "wedding", "well", "debit", "transit",
    "clipper", "metro", "membership", "loyalty", "rewards", "key", "prepaid", "bus", "train", "subway",
    "parking", "laundry", "phone",
}
_CARD_NEXT_EXCLUDE = {
    "game", "games", "reader", "readers", "table", "shop", "holder", "sleeve", "sleeves", "trick", "tricks",
    "stock", "reload", "reloads", "refill", "top", "topup",
}
_CREDIT_PREV_EXCLUDE = {"store", "tax", "bank", "extra", "class", "course", "school", "account"}
_CREDIT_NEXT_EXCLUDE = {"union", "score", "report", "check", "hours", "hour", "units", "unit"}
_VISA_NEXT_EXCLUDE = {
    "fee", "fees", "application", "appointment", "interview", "photo", "photos", "office", "renewal", "stamp",
    "gift", "giftcard", "giftcards", "prepaid",
}
_VISA_PREV_EXCLUDE = {"gift", "prepaid"}
# Words that may come before a pay verb in a card payment ("i paid visa 300", "just paid the card").
_BEFORE_LEAD_VERB = _BEFORE_LEAD_NOUN | {"i", "just", "finally", "also", "today", "yesterday", "auto", "bill", "online"}
# A move of money to the card is a payment: "checking to visa 300", "transfer 300 to credit card",
# "visa from checking 300". Every other word of the line must be one of these.
_MOVE_VERBS = {
    "transfer", "transferred", "transferring", "transfers", "xfer", "xferred", "move", "moved", "moving",
    "sent", "send", "sending",
}
_MOVE_SOURCES = {"checking", "wf", "wells", "fargo", "bank", "savings", "saving", "hysa", "capital", "one"}
_MOVE_FILLER = {"from", "to", "into", "toward", "towards", "my", "the", "a", "i", "just", "online", "today",
                "yesterday", "also", "over", "money", "account"}
# "pay cards 40", "pay off both cards 300": every card the parser knows is the Visa.
_PLURAL_CARD_LEAD = {"both", "all", "credit"}
# Interest paid into savings is money in, not a transfer out of checking.
_INTEREST_WORDS = {"interest", "dividend", "dividends", "apy"}
# Currency words after a number ("1500 yen"): the amount is not in dollars.
_CURRENCY_WORDS = {"yen", "jpy", "won", "krw", "eur", "euro", "euros", "gbp", "yuan", "rmb", "cny", "peso", "pesos"}

CATEGORY_LABELS = {
    "groceries": "Groceries",
    "dining": "Dining",
    "gas": "Gas",
    "wifi": "WiFi",
    "pge": "PG&E",
    "gym": "Gym",
    "spotify": "Spotify",
    "icloud": "iCloud",
    "kahram": "Brother",
    "rent": "Rent",
    "personal": "Discretionary",
    "savings_withdrawal": "Savings withdrawal",
    "savings": "Savings",
    "credit_card_payment": "Card payment",
    "payday": "Income",
    "interest": "Interest",
}


class _Tok:
    __slots__ = ("word", "start", "end")

    def __init__(self, word: str, start: int, end: int):
        self.word = word
        self.start = start
        self.end = end


def _tokens(text: str) -> List[_Tok]:
    out = []
    for m in _TOKEN_RE.finditer(text):
        out.append(_Tok(m.group(0).lstrip("@").lower(), m.start(), m.end()))
    return out


def _card_nouns(toks: List[_Tok]) -> List[Tuple[int, int]]:
    """(first, last) token indexes of each mention of the credit card."""
    nouns: List[Tuple[int, int]] = []
    n = len(toks)
    i = 0
    while i < n:
        w = toks[i].word
        prev = toks[i - 1].word if i > 0 else ""
        nxt = toks[i + 1].word if i + 1 < n else ""
        if w == "credit" and nxt in ("card", "cards"):
            if prev not in _CARD_PREV_EXCLUDE:
                nouns.append((i, i + 1))
            i += 2
            continue
        if w in _CARD_WORDS:
            skip = (
                (w == "card" and (prev in _CARD_PREV_EXCLUDE or nxt in _CARD_NEXT_EXCLUDE))
                or (w == "credit" and (prev in _CREDIT_PREV_EXCLUDE or nxt in _CREDIT_NEXT_EXCLUDE))
                or (w == "visa" and (nxt in _VISA_NEXT_EXCLUDE or prev in _VISA_PREV_EXCLUDE))
            )
            if skip and w == "visa" and nxt in ("card", "cards"):
                # "prepaid visa card": the whole phrase is the thing bought.
                i += 2
                continue
            if not skip:
                nouns.append((i, i))
        i += 1
    return nouns


def _foreign_letters(text: str) -> bool:
    """True when `text` has letters outside ASCII (Korean, accents). _TOKEN_RE sees only ASCII
    words, so these count as description text that the token checks would miss."""
    return any(c.isalpha() and not c.isascii() for c in text)


def _is_payment(toks: List[_Tok], nouns: List[Tuple[int, int]], text: str = "") -> bool:
    """True when the line pays the card (a transfer), not a purchase made with it.

    `text` is the line the tokens came from. Non-ASCII words in it ("커피 4.50 visa") are a
    description, so the line is a purchase on the card, like "coffee 4.50 visa"."""
    n = len(toks)
    if _pays_plural_cards(toks) and not _foreign_letters(text):
        return True
    if not nouns:
        return False
    starts = {a for a, _b in nouns}
    ends = {b for _a, b in nouns}
    if _moves_to_card(toks, nouns, text):
        return True
    for i, t in enumerate(toks):
        # (a) "pay visa", "paid off the card", "pay my credit card". Only card words and filler may
        # come first, so "groceries 25 paid visa" stays a purchase on the card.
        in_card = {k for a, b in nouns for k in range(a, b + 1)}
        if (t.word in _PAY_VERBS and all(k in in_card or toks[k].word in _BEFORE_LEAD_VERB for k in range(i))
                and not _foreign_letters(text[:t.start])):
            j = i + 1
            while j < n and toks[j].word in _AFTER_VERB and j not in starts:
                j += 1
            if j in starts:
                return True
        # (c) "payment to visa", "payoff card", "made payment to visa" (the pay word leads the line,
        # so "car payment visa 300" stays a purchase of a car payment on the card)
        if (t.word in _PAY_NOUNS and all(x.word in _BEFORE_LEAD_NOUN for x in toks[:i])
                and not _foreign_letters(text[:t.start])):
            j = i + 1
            while j < n and toks[j].word in _AFTER_NOUN and j not in starts:
                j += 1
            if j in starts:
                return True
    # (b) "visa payment", "cc payment", "card payoff", "visa bill", "visa pay off"
    for b in ends:
        j = b + 1
        if j < n and toks[j].word in _BEFORE_PAY_NOUN:
            return True
        if j < n and (toks[j].word in _PAY_NOUNS or toks[j].word in _PAY_VERBS):
            return True
    # (d) only card words (plus "bill"/"balance") and an amount: "credit card 300". A plus before
    # the amount ("visa + 20", "visa credit + 15") means money in, so it is not a payment.
    in_nouns = set()
    for a, b in nouns:
        in_nouns.update(range(a, b + 1))
    others = [t for k, t in enumerate(toks) if k not in in_nouns and t.word not in _BEFORE_PAY_NOUN]
    if re.search(r"\+\s*\$?\s*[0-9.]", text):
        return False
    return not others and not _foreign_letters(text)


def _pays_plural_cards(toks: List[_Tok]) -> bool:
    """"pay cards 40", "paid off both cards 300", "pay all credit cards from savings 200"."""
    n = len(toks)
    for i, t in enumerate(toks):
        if t.word not in _PAY_VERBS or not all(toks[k].word in _BEFORE_LEAD_VERB for k in range(i)):
            continue
        j = i + 1
        while j < n and (toks[j].word in _AFTER_VERB or toks[j].word in _PLURAL_CARD_LEAD):
            j += 1
        if j < n and toks[j].word == "cards":
            return all(x.word in _MOVE_SOURCES or x.word in _MOVE_FILLER for x in toks[j + 1:])
        return False
    return False


def _moves_to_card(toks: List[_Tok], nouns: List[Tuple[int, int]], text: str) -> bool:
    """True for money moved to the card: "checking to visa 300", "checking -> visa 300",
    "transfer 300 to credit card", "sent 300 to visa", "visa from checking 300".

    Every word that is not the card must be a move word, an account or filler, so a purchase
    with a name ("uber to visa office 20") never matches."""
    in_card = {k for a, b in nouns for k in range(a, b + 1)}
    allowed = _MOVE_VERBS | _MOVE_SOURCES | _MOVE_FILLER
    if any(t.word not in allowed for k, t in enumerate(toks) if k not in in_card):
        return False
    if _foreign_letters(text):
        return False
    for a, b in nouns:
        # The card is the target: "to visa", "into my card", "-> visa".
        k = a - 1
        while k >= 0 and toks[k].word in ("my", "the"):
            k -= 1
        if k >= 0 and toks[k].word in ("to", "into", "toward", "towards"):
            return True
        gap = text[toks[k].end if k >= 0 else 0:toks[a].start]
        if any(arrow in gap for arrow in ("->", "→", "=>")):
            return True
        # The card takes money from an account: "visa from checking 300".
        if b + 1 < len(toks) and toks[b + 1].word == "from":
            rest = [x.word for x in toks[b + 2:] if x.word not in ("my", "the")]
            if rest and rest[0] in _MOVE_SOURCES:
                return True
    return False


def _strip_spans(text: str, spans: List[Tuple[int, int]]) -> str:
    out = text
    for a, b in sorted(spans, reverse=True):
        out = out[:a] + " " + out[b:]
    out = re.sub(r"\(\s*\)|\[\s*\]", " ", out)
    out = re.sub(r"\s+", " ", out).strip()
    out = re.sub(r"^[\s:\-–—,]+", "", out)
    return out.strip()


def _marker_spans(text: str, toks: List[_Tok], groups: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Character spans to remove for each marker, with "on"/"with"/"(...)"/":" around it."""
    spans = []
    for a, b in groups:
        start, end = toks[a].start, toks[b].end
        k = a - 1
        if k >= 0 and toks[k].word in _PREPOSITIONS and text[toks[k].end:start].strip() in ("", "/"):
            start = toks[k].start
            k -= 1
            if k >= 0 and toks[k].word in _PURCHASE_VERBS and text[toks[k].end:start].strip() == "":
                start = toks[k].start
        elif k >= 0 and toks[k].word in _PURCHASE_VERBS and text[toks[k].end:start].strip() == "":
            # "paid visa", "charged card"
            start = toks[k].start
        # "(visa)" / "[card]"
        before = text[:start].rstrip()
        after_m = re.match(r"\s*[)\]]", text[end:])
        if before.endswith(("(", "[")) and after_m:
            start = len(before) - 1
            end = end + after_m.end()
        # "visa:" / "visa -"
        colon = re.match(r"\s*:", text[end:])
        if colon:
            end += colon.end()
        spans.append((start, end))
    return spans


def _debit_groups(toks: List[_Tok]) -> List[Tuple[int, int]]:
    groups = []
    for i, t in enumerate(toks):
        if t.word == "debit":
            j = i + 1 if i + 1 < len(toks) and toks[i + 1].word in ("card", "cards") else i
            groups.append((i, j))
    return groups


class _Num:
    __slots__ = ("start", "end", "cents", "weak")

    def __init__(self, start: int, end: int, cents: int, weak: bool):
        self.start = start
        self.end = end
        self.cents = cents
        self.weak = weak  # next to a month name ("oct 2"): used only when nothing else is there


def _other_currency(ch: str) -> bool:
    """True for a currency sign that is not the dollar (¥ € £ ₩ ...)."""
    return bool(ch) and ch != "$" and unicodedata.category(ch) == "Sc"


def _scan_numbers(text: str) -> Tuple[List[_Num], Optional[str], List[str], List[str]]:
    """Amount candidates in `text`, a problem message (or None), numbers glued to letters and
    percents ("15%": never an amount)."""
    cands: List[_Num] = []
    glued: List[str] = []
    percents: List[str] = []
    problem: Optional[str] = None
    n = len(text)
    for m in _RUN_RE.finditer(text):
        s, e, tok = m.start(), m.end(), m.group(0)
        while tok and tok[-1] in ".,":  # sentence punctuation: "lunch 13."
            tok, e = tok[:-1], e - 1
        while tok and tok[0] == ",":
            tok, s = tok[1:], s + 1
        if not tok or not any(ch.isdigit() for ch in tok):
            continue
        prev = text[s - 1] if s > 0 else ""
        nxt = text[e] if e < n else ""
        # Not a dollar amount: "15%", "¥1500", "20 €", "1500 yen"
        sign_before = text[:s].rstrip()[-1:]
        after = text[e:].lstrip()
        if after.startswith("%"):
            percents.append(tok)
            continue
        word_after = re.match(r"[A-Za-z]+", after)
        if (_other_currency(sign_before) or _other_currency(after[:1])
                or (word_after and word_after.group(0).lower() in _CURRENCY_WORDS)):
            problem = problem or f"Only dollar amounts work. Write {tok} in dollars."
            continue
        # Dates and times: "10/24", "10:30"
        if prev in ("/", "\\") or nxt in ("/", "\\"):
            continue
        if (prev == ":" and s >= 2 and text[s - 2].isdigit()) or (nxt == ":" and e + 1 < n and text[e + 1].isdigit()):
            continue
        # Ordinals: "2nd", "21st"
        if _ORDINAL_RE.match(text, e):
            continue
        # Counts: "x2", "2x"
        lone_x_before = prev in ("x", "X", "×") and (s < 2 or not text[s - 2].isalnum())
        lone_x_after = nxt in ("x", "X", "×") and (e + 1 >= n or not text[e + 1].isalnum())
        if lone_x_before or lone_x_after:
            problem = problem or "Write the total amount, not a count like x2."
            continue
        # Inside a word: "h2o", "1e3", "25.5o"
        if prev.isalpha() or nxt.isalpha():
            w_end = text.find(" ", e)
            glued.append(text[text.rfind(" ", 0, s) + 1:w_end if w_end >= 0 else n])
            continue
        # Part of a name: "7-eleven"
        if nxt in ("-", "–") and e + 1 < n and text[e + 1].isalpha():
            continue
        if not _AMOUNT_RE.fullmatch(tok):
            if re.fullmatch(r"[0-9]+,[0-9]{1,2}", tok):
                problem = problem or f"Use a dot for cents: {tok.replace(',', '.')}, not {tok}."
            elif re.fullmatch(r"[0-9]*\.[0-9]{3,}", tok):
                problem = problem or f"Use at most 2 decimals for cents (found {tok})."
            else:
                problem = problem or f"Check the amount: {tok} is not a number like 25.50."
            continue
        whole, _dot, frac = tok.partition(".")
        cents = int(whole or "0") * 100 + int((frac + "00")[:2] or "0")
        before = re.search(r"([A-Za-z]+)\.?\s*$", text[:s])
        after = re.match(r"\s*([A-Za-z]+)", text[e:])
        weak = bool(
            (before and before.group(1).lower() in MONTH_NAMES)
            or (after and after.group(1).lower() in MONTH_NAMES)
        )
        cands.append(_Num(s, e, cents, weak))
    return cands, problem, glued, percents


def _find_amount(text: str, first: bool = False) -> Tuple[Optional[_Num], Optional[str]]:
    """The amount in `text`: (number, None), (None, error message) or (None, None) when there is none.

    first=True (card payments) takes the first amount; otherwise exactly one amount must be left.
    """
    cands, problem, glued, percents = _scan_numbers(text)
    if problem:
        return None, problem
    pool = [c for c in cands if not c.weak] or cands
    if not pool:
        if percents:
            return None, f"{percents[0]}% is a percent, not an amount. Write the dollars: tip 4.50"
        if glued:
            return None, f"Check the amount: '{glued[0].strip()}' mixes digits and letters."
        return None, None
    if first:
        if len(pool) > 1 and not text[pool[0].end:pool[1].start].strip(" $"):
            # "pay visa 1 200": two numbers side by side, not an amount and a note
            shown = " and ".join(text[c.start:c.end] for c in pool[:2])
            return None, f"More than one number ({shown}). Keep only the amount."
        if len(pool) > 1:
            # Notes are counts and bracketed numbers; exactly one number must be left.
            kept = [c for c in pool if not _is_note_number(text, c)]
            if len(kept) == 1:
                return kept[0], None
            shown = " and ".join(text[c.start:c.end] for c in (kept or pool)[:2])
            return None, f"More than one number ({shown}). Keep only the amount."
        return pool[0], None
    if len(pool) > 1:
        lead = pool[0]
        rest = text[lead.end:]
        if not text[:lead.start].strip(" -–—:*$") and re.match(r"\s+[A-Za-z]", rest):
            pool = pool[1:]  # "99 ranch 40": the first number is part of the name
    if len(pool) > 1:
        shown = " and ".join(text[c.start:c.end] for c in pool[:2])
        return None, f"More than one number ({shown}). Keep only the amount."
    return pool[0], None


def _is_note_number(text: str, num: _Num) -> bool:
    """True for a number that is a note, not the amount: inside brackets ("(statement 1,250)"),
    a count ("for 2 months", "2 visa bills", "visa 2 payment") or "1 of 2"."""
    before = text[:num.start]
    if before.count("(") > before.count(")") or before.count("[") > before.count("]"):
        return True
    after = text[num.end:]
    if re.match(r"\s*of\s+\$?[0-9]", after, re.IGNORECASE) or re.search(r"[0-9]\s+of\s*$", before, re.IGNORECASE):
        return True
    words = re.findall(r"[A-Za-z]+", after)
    m = re.match(r"\s+[A-Za-z]", after)
    if not m:
        return False
    for w in words:
        w = w.lower()
        if w in _CARD_WORDS:
            continue
        return w in _COUNT_WORDS
    return False


def _cut_amount(text: str, num: _Num) -> str:
    """`text` without the amount and the sign or $ around it."""
    a, b = num.start, num.end
    pre = re.search(r"[-–—+]?\s*\$?\s*$", text[:a])
    if pre:
        a = pre.start()
    post = re.match(r"\s*\$", text[b:])
    if post:
        b += post.end()
    out = text[:a] + " " + text[b:]
    out = re.sub(r"\(\s*\)|\[\s*\]", " ", out)
    return re.sub(r"\s+", " ", out).strip(" -–—:+,.\t")


def _amount(text: str) -> Optional[float]:
    """First amount in a line (kept for callers that only need a number)."""
    num, _err = _find_amount(text, first=True)
    return num.cents / 100.0 if num else None


def _normalize_numbers(text: str) -> str:
    # "1,480.00" -> "1480.00"
    return re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)


def explain_bullet_line(line: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parses one bullet line. Returns (item, None), (None, error message) or (None, None) for blank lines.

    The item dict feeds FinanceStore.log_batch_transactions and also carries
    "kind" ("spend" | "income" | "card_payment" | "transfer") and "account_short"
    ("Checking" | "Visa" | "Savings"; for a payment, the account that paid).
    """
    raw = (line or "").strip()
    if not raw or raw.startswith("#"):
        return None, None

    # Strip leading bullet dashes / asterisks
    cleaned = re.sub(r"^[\s*•\-–—]+\s*", "", raw)
    if not cleaned:
        return None, None
    cleaned = _normalize_numbers(cleaned)

    toks = _tokens(cleaned)
    nouns = _card_nouns(toks)

    # 1. Card payment: checking (or savings) pays the card. A transfer, not spending.
    if _is_payment(toks, nouns, cleaned):
        num, err = _find_amount(cleaned, first=True)
        if err:
            return None, err
        if num is None:
            return None, "Add the amount paid. Example: pay visa 300"
        if num.cents <= 0:
            return None, "Amount must be more than $0."
        amount = num.cents / 100.0
        from_savings = bool(_SAVINGS_RE.search(cleaned))
        return {
            "payee": "Visa payment",
            "amount": amount,
            "amount_cents": num.cents,
            "category": "credit_card_payment",
            "to_account": VISA_ACCOUNT,
            "from_account": SAVINGS_ACCOUNT if from_savings else CHECKING_ACCOUNT,
            "memo": raw,
            "kind": "card_payment",
            "account_short": "Savings" if from_savings else "Checking",
        }, None

    # 2. Card purchase markers ("visa", "on card", "(cc)", ...) and debit markers come out of the name.
    on_card = bool(nouns)
    groups = list(nouns) + _debit_groups(toks)
    text = _strip_spans(cleaned, _marker_spans(cleaned, toks, groups)) if groups else cleaned

    # One amount for the rest of the line
    num, err = _find_amount(text)
    if err:
        return None, err
    if num is not None and num.cents <= 0:
        return None, "Amount must be more than $0."
    amount = num.cents / 100.0 if num else 0.0
    amount_cents = num.cents if num else 0
    desc_all = _cut_amount(text, num) if num else text

    plus = bool(num is not None and re.search(r"\+[\s$]*$", text[:num.start]))
    # "- name - amount": an explicit minus means money out.
    minus = bool(num is not None and re.search(r"[-–—][\s$]*$", text[:num.start]))
    debit = bool(_debit_groups(toks))
    words = [t.word for t in _tokens(text)]

    # 3. Savings transfer (e.g. "savings + 500", "savings deposit 500", "- savings - 500"), or money
    # out of savings ("savings to checking 100", "withdraw savings 50", "venmo 20 from savings").
    if _SAVINGS_RE.search(text):
        if on_card:
            return None, "Savings transfers come from checking. Drop the card word."
        if num is None:
            return None, "No amount found. Example: savings + 500"
        if any(w in _INTEREST_WORDS for w in words):
            # Interest paid into savings: money in, checking does not change.
            if minus:
                return None, "Savings interest is money in. Write it with a plus: savings interest + 3.50"
            return {
                "payee": desc_all or "Savings interest",
                "amount": amount,
                "amount_cents": amount_cents,
                "category": "interest",
                "account_id": SAVINGS_ACCOUNT,
                "to_account": SAVINGS_ACCOUNT,
                "from_account": INTEREST_ACCOUNT,
                "memo": raw,
                "kind": "income",
                "account_short": "Savings",
            }, None
        if _FROM_SAVINGS_RE.search(text):
            payee_words = [w for w in words if w not in _SAVINGS_MOVE_WORDS]
            if not payee_words:
                return {
                    "payee": "Savings to checking",
                    "amount": amount,
                    "amount_cents": amount_cents,
                    "category": "savings_withdrawal",
                    "to_account": CHECKING_ACCOUNT,
                    "from_account": SAVINGS_ACCOUNT,
                    "memo": raw,
                    "kind": "transfer",
                    "account_short": "Savings",
                }, None
            # Something bought or paid straight from savings ("venmo 20 from savings").
            payee = re.sub(
                rf"\b(?:from|out\s+of|outta|off)\s+(?:my\s+|the\s+)?{_SAV}\b|\b{_SAV}\b",
                " ", desc_all, flags=re.IGNORECASE,
            )
            payee = re.sub(r"\s+", " ", payee).strip(" -–—:+,.")
            category = _spend_category(payee.lower())
            return {
                "payee": payee or category.title(),
                "amount": amount,
                "amount_cents": amount_cents,
                "category": category,
                "from_account": SAVINGS_ACCOUNT,
                "memo": raw,
                "kind": "spend",
                "account_short": "Savings",
            }, None
        return {
            "payee": desc_all or "Capital One HYSA Deposit",
            "amount": amount,
            "amount_cents": amount_cents,
            "category": "savings",
            "account_id": SAVINGS_ACCOUNT,
            "to_account": SAVINGS_ACCOUNT,
            "from_account": CHECKING_ACCOUNT,
            "memo": raw,
            "kind": "transfer",
            "account_short": "Checking",
        }, None

    # 4. Income: plus sign before the amount, or payday / salary / deposit words (checking only).
    # A minus before the amount or a debit marker means money out, whatever the words.
    refund = any(w in _REFUND_WORDS for w in words)
    if on_card and plus and not refund:
        # "visa + 20": a plus on the card reads as a refund or credit (one sentence, so the Add box
        # shows all of it).
        return None, "Card refunds are not supported; to pay the card: pay visa 20"
    if on_card and (plus or refund):
        return None, "Card refunds are not supported yet. Edit or delete the original purchase."
    income_word = _income_word(words)
    if not on_card and refund and not plus and not minus:
        return None, "Write a refund with a plus: refund + 20"
    if not on_card and income_word in _INCOME_WORDS and not plus and (minus or debit):
        return None, f"{income_word.title()} is money in. Write it with a plus: {income_word} + 1480"
    if not on_card and (plus or (income_word and not minus and not debit)):
        if num is None:
            return None, "No amount found. Example: payday + 1480"
        return {
            "payee": desc_all or "Payday Payroll",
            "amount": amount,
            "amount_cents": amount_cents,
            "category": "payday",
            "account_id": CHECKING_ACCOUNT,
            "memo": raw,
            "kind": "income",
            "account_short": "Checking",
        }, None

    # 5. Spend: "groceries - 20", "groceries 20", "coffee $5.75", "lunch 12.5 with sam"
    if num is None:
        return None, "No amount found. Example: - groceries - 25.50"
    desc_clean = desc_all
    category = _spend_category(desc_clean.lower())
    return {
        "payee": desc_clean or category.title(),
        "amount": amount,
        "amount_cents": amount_cents,
        "category": category,
        "from_account": VISA_ACCOUNT if on_card else CHECKING_ACCOUNT,
        "memo": raw,
        "kind": "spend",
        "account_short": "Visa" if on_card else "Checking",
    }, None


def _income_word(words: List[str]) -> Optional[str]:
    """The word that makes a line income ("payday", "salary", "deposit", ...), or None.

    "payday loan", "salary sacrifice", "security deposit" and "deposit for apartment" are money out.
    """
    for i, w in enumerate(words):
        prv = words[i - 1] if i > 0 else ""
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if w in _INCOME_WORDS and nxt not in _NOT_INCOME_NEXT:
            return w
        if w in _DEPOSIT_WORDS and prv not in _DEPOSIT_OUT_PREV and nxt not in _DEPOSIT_OUT_NEXT:
            return w
    return None


def _spend_category(cat_lower: str) -> str:
    """Category key from the description (keywords must start a word, so "att" does not match
    "mattress" and "gas" does not match "Vegas"; "att" must be a whole word)."""
    def has(*words: str) -> bool:
        return any(re.search(rf"\b{re.escape(w)}", cat_lower) for w in words)

    if has("grocery", "groceries", "safeway", "trader", "costco food", "supermarket"):
        return "groceries"
    if has("gas", "fuel", "chevron", "shell", "gasoline"):
        return "gas"
    if has("lunch", "dinner", "dining", "taqueria", "burrito", "chipotle", "restaurant", "food", "coffee", "starbucks"):
        return "dining"
    if has("wifi", "wi-fi", "internet", "comcast") or re.search(r"\batt\b", cat_lower):
        return "wifi"
    if has("pge", "pg&e", "electric", "power", "utility"):
        return "pge"
    if has("gym"):
        return "gym"
    if has("spotify"):
        return "spotify"
    if has("icloud", "apple"):
        return "icloud"
    if has("kahram", "brother"):
        return "kahram"
    if re.search(r"\brent\b", cat_lower):
        return "rent"
    return "personal"


def parse_bullet_line(line: str) -> Optional[Dict[str, Any]]:
    """Parses a single bullet line with minus (-) or plus (+) syntax. None when it cannot be read.

    Examples:
      - groceries - 42.20            checking
      - groceries - 42.20 visa       on the card
      payday + 1350
      visa payment 300               checking pays the card
    Use explain_bullet_line() to get the reason a line was rejected.
    """
    item, _err = explain_bullet_line(line)
    return item


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def preview_bullet_line(line: str) -> Dict[str, Any]:
    """What a typed line will do, without writing anything (live preview in the Add box).

    Returns {ok, error, payee, amount_cents, category_label, account_short, kind, summary_text}.
    Blank input: ok False, error None, summary_text "".
    """
    item, err = explain_bullet_line(line)
    if item is None:
        return {
            "ok": False,
            "error": err,
            "payee": "",
            "amount_cents": 0,
            "category_label": "",
            "account_short": "",
            "kind": "",
            "summary_text": err or "",
        }
    kind = item["kind"]
    cents = item["amount_cents"]
    label = CATEGORY_LABELS.get(item["category"], str(item["category"]).title())
    acct = item["account_short"]
    if kind == "card_payment":
        summary = f"Card payment {_money(cents)} from {acct}: owed goes down, not spending"
    elif kind == "transfer" and item.get("from_account") == SAVINGS_ACCOUNT:
        summary = f"Savings withdrawal {_money(cents)} to Checking: not spending"
    elif kind == "transfer":
        summary = f"Savings transfer {_money(cents)} from {acct}: not spending"
    elif kind == "income":
        summary = f"Income {_money(cents)} to {acct}"
    elif acct == "Visa":
        summary = f"{label} {_money(cents)} on Visa: owed goes up, checking unchanged"
    else:
        summary = f"{label} {_money(cents)} from {acct}"
    return {
        "ok": True,
        "error": None,
        "payee": item["payee"],
        "amount_cents": cents,
        "category_label": label,
        "account_short": acct,
        "kind": kind,
        "summary_text": summary,
        "from_account": item.get("from_account") or "",
    }


def import_text_document(
    text: Any,
    store: Optional[Any] = None,
    default_year: int = 2026,
) -> Dict[str, Any]:
    """Parses a multi-date text document and ingests all transactions into SQLite."""
    # Allow passing (store, text) or (text, store)
    if not isinstance(text, str) and isinstance(store, str):
        text, store = store, text

    actual_store = store or FinanceStore()
    lines = text.splitlines()

    current_date = datetime.now().strftime("%Y-%m-%d")
    total_parsed = 0
    logged_count = 0
    days_seen = set()
    all_batch_items: List[Dict[str, Any]] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check if line is a date header
        header_date = parse_date_header(stripped, default_year=default_year)
        if header_date:
            current_date = header_date
            days_seen.add(current_date)
            continue

        # Check if line has an inline date prefix before the bullet (e.g. "12/September - groceries - 25.50" or "23/10 - coffee - 5")
        date_pfx_m = re.match(r"^(\d{1,2}[-/.\s]+[A-Za-z0-9]+(?:[-/.\s]+\d{2,4})?)\s*[:\-–—]\s*(.*)$", stripped)
        if date_pfx_m:
            pfx_date = parse_date_header(date_pfx_m.group(1), default_year=default_year)
            if pfx_date:
                current_date = pfx_date
                days_seen.add(current_date)
                stripped = date_pfx_m.group(2).strip()

        # Try parsing line as bullet transaction
        item = parse_bullet_line(stripped)
        if item:
            item["date"] = current_date
            all_batch_items.append(item)
            days_seen.add(current_date)
            total_parsed += 1

    total_inflow_cents = 0
    total_outflow_cents = 0

    if all_batch_items:
        for itm in all_batch_items:
            amt_c = itm.get("amount_cents", int(round(itm["amount"] * 100)))
            kind = itm.get("kind")
            if itm.get("category") in ("payday", "salary", "income", "interest"):
                total_inflow_cents += amt_c
            elif kind in (None, "spend") and itm.get("category") not in ("savings", "credit_card_payment"):
                # Card payments and savings transfers move money; they are not spending.
                total_outflow_cents += amt_c

        logged = actual_store.log_batch_transactions(all_batch_items)
        logged_count = len(logged)
        try:
            FinanceExporter(actual_store).export_to_beancount()
        except Exception:
            pass

    return {
        "total_lines": len(lines),
        "items_parsed": total_parsed,
        "transactions_logged": logged_count,
        "total_days": len(days_seen),
        "days_count": len(days_seen),
        "total_transactions": logged_count,
        "total_inflow_cents": total_inflow_cents,
        "total_outflow_cents": total_outflow_cents,
        "dates": sorted(list(days_seen)),
    }
