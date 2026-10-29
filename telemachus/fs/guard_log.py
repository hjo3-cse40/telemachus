"""Append-only guard event log (read by the cockpit Guard tab).

One JSON object per line in ~/.memory/guard_events.jsonl (or the vault set by
TELEMACHUS_MEMORY_DIR). TELEMACHUS_GUARD_LOG moves the file; "off" turns logging off.

Each event: {"ts": float, "kind": str, "target": str, "rule": str, "detail": str,
"latency_ms": float (only when measured), "action": str}.

Kinds: BLOCK, CONFIRM (needs confirmation), ALLOW, TRASH, CHECKPOINT.

Rules for this module:
- record() never raises. A logging problem must never break the guarded operation.
- The file is capped (MAX_BYTES). When full it moves to guard_events.jsonl.1 (one old copy).
- Targets are shortened and obvious secrets (tokens, passwords, long keys) are masked.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

ENV_PATH = "TELEMACHUS_GUARD_LOG"
FILE_NAME = "guard_events.jsonl"
MAX_BYTES = 256 * 1024
TARGET_MAX = 120
DETAIL_MAX = 240
# Text is cut to this many characters (per kept end) BEFORE masking, so a huge command can never
# make record() slow. Far more than the kept text, so a secret's name stays next to its value.
REDACT_WINDOW = 1000
KINDS = ("BLOCK", "CONFIRM", "ALLOW", "TRASH", "CHECKPOINT")

_lock = threading.Lock()
_cache: Dict[str, Tuple[Tuple[float, int, float, int], List[Dict[str, Any]]]] = {}

# Values that look like secrets: NAME=value where the name holds a secret word (also inside
# DB_PASSWORD, OPENAI_API_KEY, GITHUB_TOKEN, --password=x), --password x flags, bearer tokens,
# user:pass in curl -u and in URLs, mysql -pXXX, sshpass -p x, and long runs of key-like
# characters. A value in quotes is masked whole.
_SECRET_WORD = (r"(?:api[_-]?key|token|secret|password|passwd|pwd|auth(?!or)|authorization"
                r"|access[_-]?key|private[_-]?key|credentials?)")
_VALUE = r"(\"[^\"]*\"|'[^']*'|\S+)"
# The name parts around the secret word are bounded ({0,40}): with open-ended runs a long word
# full of secret words ("secret_" * 500) took cubic time and stalled the guarded operation.
_SECRET_PAIR = re.compile(
    r"(?i)((?<![A-Za-z0-9])[A-Za-z0-9_]{0,40}?" + _SECRET_WORD + r"[A-Za-z0-9_]{0,40}\s*[=:]\s*)"
    r"(?!(?:bearer|basic)\s)" + _VALUE  # "Authorization: Bearer x": the bearer rule masks x
)
_SECRET_FLAG = re.compile(
    r"(?i)((?<![A-Za-z0-9-])--?(?:password|passwd|token|api[_-]?key|secret|access[_-]?key)\s+)" + _VALUE
)
_BEARER = re.compile(r"(?i)\b(bearer\s+|authorization\s*:\s*basic\s+)([^\s\"']+)")
_USER_PASS = re.compile(r"((?<!\S)(?:-u\s*|--user[=\s]+)['\"]?[^\s:'\"]+:)([^\s'\"]+)")
# Starts only at the beginning of a scheme-like run, so "a+a+a+..." stays linear.
_URL_PASS = re.compile(r"(?i)((?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(?=@)")
# The run between the command and -p is bounded ({0,500}) so many "mysql" words stay fast.
_MYSQL_P = re.compile(
    r"(?i)(\b(?:mysql(?:dump|admin|import|check)?|mariadb(?:-dump)?)\b[^|;&]{0,500}?(?<!\S)-p)(?=[^\s-])(\S+)"
)
_SSHPASS_P = re.compile(r"(?i)(\bsshpass\s+-p\s*)(\S+)")
# 32+ key characters with at least one digit and one letter (paths have "/" so they never match).
# Starts only at the beginning of a run (not at every "+"), so long runs stay linear.
_LONG_KEY = re.compile(r"(?<![A-Za-z0-9+_])(?=[A-Za-z0-9+_]*\d)(?=[A-Za-z0-9+_]*[A-Za-z])[A-Za-z0-9+_]{32,}={0,2}")


def log_path() -> Optional[Path]:
    """Where events go, or None when logging is off. Read on every call (tests change HOME)."""
    override = os.environ.get(ENV_PATH)
    if override is not None:
        if override.strip().lower() in ("", "0", "off", "none"):
            return None
        return Path(override).expanduser()
    root = os.environ.get("TELEMACHUS_MEMORY_DIR") or str(Path.home() / ".memory")
    return Path(root).expanduser() / FILE_NAME


def redact(text: str) -> str:
    """Masks values that look like secrets."""
    for pattern in (_BEARER, _SECRET_PAIR, _SECRET_FLAG, _USER_PASS, _URL_PASS, _MYSQL_P, _SSHPASS_P):
        text = pattern.sub(lambda m: m.group(1) + "***", text)
    return _LONG_KEY.sub("***", text)


def _tilde_one(text: str, home: str) -> str:
    """Replaces `home` with ~ only as a whole path: '/Users/sam/x' and '/Users/sam' change,
    '/Users/sam2/x' and '/backup/Users/sam/x' do not."""
    pattern = r"(?<![^\s'\"`(=:,\[])" + re.escape(home) + r"(?=/|$|[\s'\"`),;:\]])"
    return re.sub(pattern, "~", text)


def _home_tilde(text: str) -> str:
    try:
        home = str(Path.home()).rstrip("/")
    except Exception:
        return text
    if home:
        text = _tilde_one(text, home)
        if home.startswith("/private/"):
            text = _tilde_one(text, home[len("/private"):])
        elif os.path.realpath(home) != home:
            text = _tilde_one(text, os.path.realpath(home))
    return text


def _one_line(text: Any, head: int, tail: int = 0) -> str:
    """Whitespace collapsed, cut to the first `head` and last `tail` characters before any
    regex work (a rough cut first, so split() stays cheap on a huge text)."""
    raw = str(text or "")
    if len(raw) > 2 * (head + tail):
        raw = raw[: 2 * head] + " " + (raw[-2 * tail:] if tail else "")
    out = " ".join(raw.split())
    if len(out) > head + tail:
        out = out[:head] + (" " + out[-tail:] if tail else "")
    return out


def short_target(target: Any, limit: int = TARGET_MAX) -> str:
    """One line, home folder as ~, secrets masked, cut in the middle when long."""
    text = redact(_home_tilde(_one_line(target, REDACT_WINDOW, REDACT_WINDOW)))
    if len(text) > limit:
        keep = limit - 1
        text = text[: keep // 2] + "…" + text[-(keep - keep // 2):]
    return text


def _clean(text: Any, limit: int) -> str:
    out = redact(_home_tilde(_one_line(text, max(REDACT_WINDOW, 4 * limit))))
    return out if len(out) <= limit else out[: limit - 1] + "…"


def record(
    kind: str,
    target: Any = "",
    rule: str = "",
    detail: Any = "",
    latency_ms: Optional[float] = None,
    action: str = "",
    ts: Optional[float] = None,
) -> bool:
    """Appends one event. Returns True when it was written. Never raises."""
    try:
        path = log_path()
        if path is None:
            return False
        event: Dict[str, Any] = {
            "ts": round(float(ts if ts is not None else time.time()), 3),
            "kind": str(kind).upper(),
            "target": short_target(target),
            "rule": _clean(rule, 60),
            "detail": _clean(detail, DETAIL_MAX),
        }
        if action:
            event["action"] = _clean(action, 24)
        if latency_ms is not None:
            event["latency_ms"] = round(float(latency_ms), 1)
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_full(path, len(line.encode("utf-8")))
            fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        return True
    except Exception:
        return False


def _rotate_if_full(path: Path, incoming: int) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size + incoming <= MAX_BYTES:
        return
    try:
        os.replace(str(path), str(path) + ".1")
    except OSError:
        pass


def _read_file(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                ev = _checked(ev)
                if ev is not None:
                    out.append(ev)
    except OSError:
        pass
    return out


# Larger than this in seconds is past the year 5000: the writer used milliseconds.
_MS_CUTOFF = 1e11


def clean_ts(value: Any) -> Optional[float]:
    """A usable Unix time in seconds, or None. Millisecond (and microsecond) stamps are scaled
    down; text, bool, inf, nan, 0 and times datetime cannot show give None."""
    if isinstance(value, bool):
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(ts) or ts <= 0:
        return None
    for _ in range(2):
        if ts > _MS_CUTOFF:
            ts /= 1000.0
    try:
        datetime.fromtimestamp(ts)
    except (OverflowError, ValueError, OSError):
        return None
    return ts


def _checked(ev: Any) -> Optional[Dict[str, Any]]:
    """One log line made safe to show, or None to skip it. A bad line hides only itself.

    ts becomes seconds (see clean_ts), kind an upper-case word, text fields strings with secrets
    masked again (lines written before a masking rule existed), latency_ms a finite number or gone.
    """
    if not isinstance(ev, dict) or "kind" not in ev:
        return None
    ts = clean_ts(ev.get("ts"))
    if ts is None:
        return None
    out = dict(ev)
    out["ts"] = ts
    out["kind"] = str(ev.get("kind") or "").upper()
    for key in ("target", "rule", "detail", "action"):
        if key in out:
            # Cut first: lines are capped when written, but an old or hand-edited line may not be.
            out[key] = redact(str(out[key] if out[key] is not None else "")[: 2 * REDACT_WINDOW])
    lat = out.get("latency_ms")
    if "latency_ms" in out and not (isinstance(lat, (int, float)) and not isinstance(lat, bool)
                                    and math.isfinite(lat) and lat >= 0):
        del out["latency_ms"]
    return out


def _stamp(path: Path) -> Tuple[float, int]:
    try:
        st = path.stat()
        return st.st_mtime, st.st_size
    except OSError:
        return 0.0, -1


def read_all(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every event in the log and its one old copy, oldest first. Never raises.

    Re-reads the files only when their size or time changed.
    """
    try:
        path = path or log_path()
        if path is None:
            return []
        old = Path(str(path) + ".1")
        key = str(path)
        stamp = _stamp(old) + _stamp(path)
        hit = _cache.get(key)
        if hit and hit[0] == stamp:
            return list(hit[1])
        events = _read_file(old) + _read_file(path)
        events.sort(key=lambda e: e["ts"])  # every ts passed clean_ts()
        _cache[key] = (stamp, events)
        return list(events)
    except Exception:
        return []


def read_recent(limit: int = 50, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Newest events first."""
    events = read_all(path)
    return list(reversed(events[-max(0, limit):])) if limit else []


def _day_start(now: float) -> float:
    d = datetime.fromtimestamp(now)
    return datetime(d.year, d.month, d.day).timestamp()


def summary(now: Optional[float] = None, path: Optional[Path] = None) -> Dict[str, Any]:
    """Counts for the Guard tab. All numbers come from the log; None means not seen yet."""
    now = time.time() if now is None else now
    events = read_all(path)
    start = _day_start(now)
    today = [e for e in events if e["ts"] >= start]

    def count(kind: str, rule: Optional[str] = None, evs: Optional[List[Dict[str, Any]]] = None) -> int:
        return sum(1 for e in (today if evs is None else evs)
                   if e.get("kind") == kind and (rule is None or e.get("rule") == rule))

    def last(pred) -> Optional[Dict[str, Any]]:
        for e in reversed(events):
            if pred(e):
                return e
        return None

    latency_ev = last(lambda e: isinstance(e.get("latency_ms"), (int, float)))
    checkpoint_ev = last(lambda e: e.get("kind") == "CHECKPOINT")
    return {
        "total": len(events),
        "today": len(today),
        "blocked_today": count("BLOCK"),
        "path_blocks_today": sum(1 for e in today if e.get("kind") == "BLOCK" and e.get("rule") in ("path shield", "command shield")),
        "intent_blocks_today": count("BLOCK", "intent check"),
        "confirm_today": count("CONFIRM"),
        "trash_today": count("TRASH"),
        "checkpoints_today": count("CHECKPOINT"),
        "last_event_ts": float(events[-1]["ts"]) if events else None,
        "last_checkpoint_ts": float(checkpoint_ev["ts"]) if checkpoint_ev else None,
        "last_intent_latency_ms": float(latency_ev["latency_ms"]) if latency_ev else None,
        "last_intent_ts": float(latency_ev["ts"]) if latency_ev else None,
    }


# Names shown in the cockpit for the guard tiers.
RULE_PATH = "path shield"
RULE_COMMAND = "command shield"
RULE_INTENT = "intent check"
RULE_SOFT_DELETE = "soft delete"
RULE_CONFIRM = "needs confirmation"
RULE_CHECKPOINT = "git checkpoint"
