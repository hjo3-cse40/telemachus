"""Telemetry for the Cockpit header: LM Studio state, context use, RAM and speed.

Rule: never show a number that was not measured. Every value here is either real or None, and
the header hides a field (or shows a dash) when it is None.

Sources
- LM Studio reachability, loaded model and its context length: GET /api/v0/models on 127.0.0.1:1234
  (falls back to /v1/models). Done in a background thread every 2 s with a 0.5 s timeout, so the
  UI loop never waits on the network.
- Live state (reading prompt / writing tokens): ~/.memory/llm_status.json, written by
  telemachus/ingress/proxy.py (LLMStreamMonitor). Re-read only when its mtime changes.
- Context used: the latest OpenCode step-finish part (tokens input + cache read + cache write) in
  ~/.local/share/opencode/opencode.db, read in the background thread.
- RAM: psutil (total minus available).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import math
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0
HTTP_TIMEOUT_S = 0.5
# A prompt can take minutes to read on a laptop (long context), so "reading" goes stale slowly.
READING_STALE_S = 300.0
# While tokens stream the proxy rewrites the file many times a second; a long gap means it stopped.
WRITING_STALE_S = 20.0


@dataclass
class HeaderInfo:
    """Everything the header row shows. None = not measured, so hide it."""

    lm: str = "unknown"  # "unknown" | "offline" | "no_model" | "ready" | "reading" | "writing"
    model: str = ""
    elapsed_s: Optional[float] = None  # reading: seconds since the request started
    tokens: Optional[int] = None  # writing: tokens produced so far
    tps: Optional[float] = None  # writing: live speed; otherwise the last measured speed
    ctx_used: Optional[int] = None
    ctx_max: Optional[int] = None
    ram_used_gb: Optional[float] = None
    ram_total_gb: Optional[float] = None
    think_tokens: Optional[int] = None  # reasoning tokens of the latest OpenCode step

    @property
    def active(self) -> bool:
        return self.lm in ("reading", "writing")

    @property
    def ctx_pct(self) -> Optional[int]:
        if not self.ctx_used or not self.ctx_max:
            return None
        return max(0, min(100, int(round(self.ctx_used * 100 / self.ctx_max))))


@dataclass
class _PollResult:
    reachable: Optional[bool] = None  # None until the first poll finishes
    models: List[str] = field(default_factory=list)  # loaded LLMs (or listed ids for /v1)
    ctx_max: Optional[int] = None
    ctx_used: Optional[int] = None
    think_tokens: Optional[int] = None
    ram_used_gb: Optional[float] = None
    ram_total_gb: Optional[float] = None
    polled_at: float = 0.0


def short_model_name(model_id: str, limit: int = 24) -> str:
    """'qwen/qwen3.5-9b' -> 'qwen3.5-9b'. Cut to `limit` characters."""
    name = (model_id or "").split("/")[-1].strip()
    if len(name) > limit:
        name = name[: limit - 1] + "…"
    return name


class CockpitTelemetry:
    """Collects header telemetry. Network and database reads happen in a background thread."""

    def __init__(
        self,
        lm_studio_url: str = "http://127.0.0.1:1234/v1/models",
        proxy_url: str = "http://127.0.0.1:1235",
        status_path: Optional[Path] = None,
        opencode_db: Optional[Path] = None,
    ):
        self.lm_studio_url = lm_studio_url
        parts = urlsplit(lm_studio_url)
        self.lm_base = f"{parts.scheme or 'http'}://{parts.netloc or '127.0.0.1:1234'}"
        self.proxy_url = proxy_url
        self.status_path = status_path or (Path.home() / ".memory" / "llm_status.json")
        self.opencode_db = opencode_db or (Path.home() / ".local" / "share" / "opencode" / "opencode.db")

        self._lock = threading.Lock()
        self._poll = _PollResult()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        # status file cache (re-read only when mtime changes)
        self._status_mtime: float = -1.0
        self._status_raw: Dict[str, Any] = {}
        self._last_tps: Optional[float] = None  # last speed measured this session (survives reset_idle)

        # legacy caches kept for get_active_model / get_memory_breakdown / get_thinking_tokens
        self._cached_model: Optional[str] = None
        self._model_cache_time: float = 0.0
        self._cached_memory: Optional[Dict[str, Any]] = None
        self._memory_cache_time: float = 0.0
        self._cached_thinking: Optional[Dict[str, Any]] = None
        self._thinking_cache_time: float = 0.0

    # ------------------------------------------------------------------
    # Background poller
    # ------------------------------------------------------------------

    def start(self, interval: float = POLL_INTERVAL_S) -> None:
        """Starts the background poller (idempotent). The thread is a daemon and never blocks exit."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.poll_once()
                except Exception as e:  # never let the poller die
                    logger.debug(f"telemetry poll failed: {e}")
                self._stop.wait(interval)

        self._thread = threading.Thread(target=loop, name="cockpit-telemetry", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> None:
        """One round of slow reads (HTTP, SQLite, psutil). Safe to call from any thread."""
        reachable, models, ctx_max = self._probe_lm_studio()
        ctx_used, think = self._read_opencode_step()
        ram_used, ram_total = self._read_ram()
        with self._lock:
            self._poll = _PollResult(
                reachable=reachable,
                models=models,
                ctx_max=ctx_max,
                ctx_used=ctx_used,
                think_tokens=think,
                ram_used_gb=ram_used,
                ram_total_gb=ram_total,
                polled_at=time.time(),
            )

    def _get_json(self, url: str) -> Any:
        req = urllib.request.Request(url, headers={"User-Agent": "TelemachusTelemetry"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _probe_lm_studio(self) -> Tuple[bool, List[str], Optional[int]]:
        """(reachable, loaded model ids, context length of the first loaded model)."""
        try:
            data = self._get_json(self.lm_base + "/api/v0/models")
            rows = data.get("data", []) if isinstance(data, dict) else []
            loaded = [
                m for m in rows
                if m.get("state") == "loaded" and str(m.get("type", "llm")).lower() in ("llm", "vlm")
            ]
            ctx = None
            if loaded:
                first = loaded[0]
                # Prefer the length the model was loaded with; fall back to its max length.
                ctx = first.get("loaded_context_length") or first.get("max_context_length")
                ctx = int(ctx) if isinstance(ctx, (int, float)) and ctx > 0 else None
            return True, [str(m.get("id", "")) for m in loaded if m.get("id")], ctx
        except urllib.error.HTTPError:
            pass  # server is up but has no /api/v0: try the OpenAI route below
        except Exception:
            return False, [], None
        try:
            data = self._get_json(self.lm_base + "/v1/models")
            ids = [str(m.get("id", "")) for m in data.get("data", []) if "embed" not in str(m.get("id", "")).lower()]
            return True, [i for i in ids if i][:1], None
        except urllib.error.HTTPError:
            return True, [], None
        except Exception:
            return False, [], None

    def _read_opencode_step(self) -> Tuple[Optional[int], Optional[int]]:
        """(context tokens used, reasoning tokens) of the latest OpenCode step, or (None, None)."""
        db = self.opencode_db
        if not db.exists():
            return None, None
        try:
            import sqlite3

            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
            try:
                row = conn.execute(
                    "SELECT data FROM part WHERE data LIKE '%step-finish%' ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
            if not row:
                return None, None
            tokens = json.loads(row[0]).get("tokens", {}) or {}
            cache = tokens.get("cache", {}) or {}
            used = int(tokens.get("input", 0) or 0) + int(cache.get("read", 0) or 0) + int(cache.get("write", 0) or 0)
            think = int(tokens.get("reasoning", 0) or 0)
            return (used or None), (think or None)
        except Exception as e:
            logger.debug(f"opencode step read failed: {e}")
            return None, None

    def _read_ram(self) -> Tuple[Optional[float], Optional[float]]:
        if psutil is None:
            return None, None
        try:
            vm = psutil.virtual_memory()
            total = vm.total / (1024 ** 3)
            used = (vm.total - vm.available) / (1024 ** 3)
            return round(used, 1), round(total, 1)
        except Exception:
            return None, None

    # ------------------------------------------------------------------
    # Live status file
    # ------------------------------------------------------------------

    def _read_status_file(self, path: Path) -> Tuple[Dict[str, Any], float]:
        """(data, mtime). Re-reads only when the mtime changed. Missing file = ({}, 0)."""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}, 0.0
        if path == self.status_path and mtime == self._status_mtime:
            return self._status_raw, mtime
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        if path == self.status_path:
            self._status_mtime, self._status_raw = mtime, data
        return data, mtime

    def get_llm_status(self, status_path: Optional[Path] = None, now: Optional[float] = None) -> Dict[str, Any]:
        """Live LM Studio status from the ingress proxy. Stale active states read as idle."""
        target = Path(status_path) if status_path else self.status_path
        data, mtime = self._read_status_file(target)
        if not data:
            return {"status": "idle"}
        now = time.time() if now is None else now
        st = data.get("status", "idle")
        # start_time is written by the proxy, but the file can hold anything: never trust its type.
        fresh = max(mtime, _float_or_none(data.get("start_time")) or 0.0)
        if st == "processing_prompt" and now - fresh > READING_STALE_S:
            return {"status": "idle", **_last_fields(data)}
        if st == "producing_tokens" and now - mtime > WRITING_STALE_S:
            return {"status": "idle", **_last_fields(data)}
        return dict(data)

    # ------------------------------------------------------------------
    # Header snapshot
    # ------------------------------------------------------------------

    def header_info(self, now: Optional[float] = None) -> HeaderInfo:
        """Cheap: combines the latest poll with the live status file. Safe to call at 8 fps."""
        now = time.time() if now is None else now
        with self._lock:
            poll = self._poll
        status = self.get_llm_status(now=now)
        st = status.get("status", "idle")
        model = short_model_name(poll.models[0]) if poll.models else ""
        if not model and status.get("model"):
            model = short_model_name(str(status.get("model")))
        info = HeaderInfo(
            model=model,
            ram_used_gb=poll.ram_used_gb,
            ram_total_gb=poll.ram_total_gb,
            think_tokens=poll.think_tokens,
        )
        if st == "processing_prompt":
            info.lm = "reading"
            start = _float_or_none(status.get("start_time"))
            info.elapsed_s = max(0.0, now - start) if start else None
        elif st == "producing_tokens":
            info.lm = "writing"
            info.tokens = _int_or_none(status.get("tokens_produced"))
            info.tps = _float_or_none(status.get("speed_tps"))
        elif poll.reachable is None:
            info.lm = "unknown"
        elif not poll.reachable:
            info.lm = "offline"
        else:
            info.lm = "ready" if poll.models else "no_model"
        if info.lm == "writing" and info.tps:
            self._last_tps = info.tps
        if info.lm in ("ready", "no_model", "reading"):
            last = _float_or_none(status.get("last_speed_tps"))
            if last:
                self._last_tps = last
            info.tps = last or self._last_tps
        if info.lm != "offline":
            info.ctx_used, info.ctx_max = poll.ctx_used, poll.ctx_max
        return info

    # ------------------------------------------------------------------
    # Older API (CLI one-liner and tests). Real values only.
    # ------------------------------------------------------------------

    def get_active_model(self, max_age: float = 120.0) -> str:
        """Loaded model id, or 'offline' when LM Studio does not answer. Cached for max_age s."""
        now = time.time()
        if self._cached_model is not None and (now - self._model_cache_time) < max_age:
            return self._cached_model
        reachable, models, _ = self._probe_lm_studio()
        if not reachable:
            name = "offline"
        elif models:
            name = models[0]
        else:
            name = "no model loaded"
        self._cached_model = name
        self._model_cache_time = now
        return name

    def get_memory_breakdown(self, max_age: float = 2.0) -> Dict[str, Any]:
        """Real RAM use from psutil. 'available' is False (and numbers 0) when it cannot be read."""
        now = time.time()
        if self._cached_memory is not None and (now - self._memory_cache_time) < max_age:
            return self._cached_memory
        used, total = self._read_ram()
        if used is None or not total:
            res: Dict[str, Any] = {"available": False, "total_gb": 0.0, "used_gb": 0.0, "used_pct": 0}
        else:
            res = {"available": True, "total_gb": total, "used_gb": used, "used_pct": int(round(used * 100 / total))}
        self._cached_memory = res
        self._memory_cache_time = now
        return res

    def get_context_budget(self) -> Dict[str, Any]:
        """Context use of the latest OpenCode step vs the loaded model's context length.

        Any field is None when it was not measured (no OpenCode step, LM Studio offline, or no
        context length reported).
        """
        with self._lock:
            poll = self._poll
        used, mx = poll.ctx_used, poll.ctx_max
        if poll.polled_at == 0.0:
            used, _ = self._read_opencode_step()
            _, _, mx = self._probe_lm_studio()
        pct = int(round(used * 100 / mx)) if used and mx else None
        return {"used_tokens": used, "max_tokens": mx, "used_pct": None if pct is None else min(100, pct)}

    def get_inference_speed(self) -> Optional[float]:
        """Live t/s while writing, else the last measured t/s from the status file, else None."""
        status = self.get_llm_status()
        if status.get("status") == "producing_tokens":
            return _float_or_none(status.get("speed_tps"))
        return _float_or_none(status.get("last_speed_tps"))

    def get_thinking_tokens(self, max_age: float = 2.0) -> Dict[str, Any]:
        """Reasoning tokens of recent OpenCode steps. Cached for max_age seconds."""
        now = time.time()
        if self._cached_thinking is not None and (now - self._thinking_cache_time) < max_age:
            return self._cached_thinking
        last_reasoning = 0
        total_reasoning = 0
        db = self.opencode_db
        if db.exists():
            try:
                import sqlite3

                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
                try:
                    rows = conn.execute(
                        "SELECT data FROM part WHERE data LIKE '%step-finish%' ORDER BY rowid DESC LIMIT 10"
                    ).fetchall()
                finally:
                    conn.close()
                if rows:
                    last_reasoning = json.loads(rows[0][0]).get("tokens", {}).get("reasoning", 0) or 0
                    for r in rows:
                        total_reasoning += json.loads(r[0]).get("tokens", {}).get("reasoning", 0) or 0
            except Exception as e:
                logger.debug(f"Failed to query reasoning tokens from opencode.db: {e}")
        res = {"enabled": True, "last_turn_tokens": last_reasoning, "recent_total_tokens": total_reasoning}
        self._cached_thinking = res
        self._thinking_cache_time = now
        return res

    def format_single_line_header(self, max_width: int = 120, status_path: Optional[Path] = None) -> str:
        """Plain one-line header for `python -m telemachus.cockpit telemetry`. Unknown values show '-'."""
        llm_stat = self.get_llm_status(status_path=status_path)
        state = llm_stat.get("status")
        model = short_model_name(self.get_active_model(), 22)
        ctx = self.get_context_budget()
        mem = self.get_memory_breakdown()
        speed = self.get_inference_speed()
        think = self.get_thinking_tokens()
        think_tok = f" · Think: {think['last_turn_tokens']}t" if think["last_turn_tokens"] > 0 else " · Think: -"

        if state == "processing_prompt":
            badge = "[LM Studio: Processing Prompt...]"
        elif state == "producing_tokens":
            toks = _int_or_none(llm_stat.get("tokens_produced")) or 0
            spd = _float_or_none(llm_stat.get("speed_tps"))
            spd_txt = f"{spd:.1f} t/s" if spd is not None else "- t/s"
            badge = f"[LM Studio: Producing ({toks} tok · {spd_txt})]"
        else:
            badge = f"[MODEL: {model}{think_tok}]"

        ctx_txt = (
            f"{ctx['used_tokens']:,}/{ctx['max_tokens']:,} ({ctx['used_pct']}%)" if ctx["used_pct"] is not None else "-"
        )
        ram_txt = f"{mem['used_gb']:.1f}/{mem['total_gb']:.1f}G ({mem['used_pct']}%)" if mem.get("available") else "-"
        speed_txt = f"{speed:.1f} t/s" if speed is not None else "-"
        line = f"{badge} | CTX: {ctx_txt} | RAM: {ram_txt} | SPEED: {speed_txt}"
        if len(line) > max_width:
            ctx_short = f"{ctx['used_pct']}%" if ctx["used_pct"] is not None else "-"
            ram_short = f"{mem['used_gb']:.1f}/{mem['total_gb']:.1f}G" if mem.get("available") else "-"
            line = f"{badge} | CTX: {ctx_short} | RAM: {ram_short} | SPEED: {speed_txt}"
        return line


def _int_or_none(v: Any) -> Optional[int]:
    """A count from the status file: a whole number >= 0, else None (bad type, negative, inf, nan)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if n >= 0 else None


def _float_or_none(v: Any) -> Optional[float]:
    """A positive finite number from the status file, else None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _last_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: data[k] for k in ("last_speed_tps", "last_tokens", "model") if k in data}
