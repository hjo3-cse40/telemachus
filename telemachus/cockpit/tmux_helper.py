"""Small helpers that the cockpit's tmux server runs: the status line and the layout saver.

tmux calls this file directly by path (``python -I tmux_helper.py ...``) every 2 seconds, so it
must start fast and never hang. Running it by path skips ``telemachus/cockpit/__init__.py``,
which imports the whole TUI (about 100 ms). Only the standard library is needed (psutil is used
for RAM when the venv has it).

Commands:
  status                           print the tmux status-right text (LM Studio state + RAM)
  save-layout TMUX SOCKET WINDOW GEN
                                   store the last split in the settings file. Debounced: waits
                                   a moment, then saves only if no newer layout change happened
                                   (GEN still current) and reads the values from tmux itself.

The settings file is ``$XDG_CONFIG_HOME/telemachus/cockpit.json`` (default ``~/.config``), or the
path in ``TELEMACHUS_COCKPIT_SETTINGS``.
Only the keys below are touched; other keys in the file are kept.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Settings ------------------------------------------------------------------

DEFAULT_SPLIT_PCT = 48  # dashboard width in side-by-side mode (today's look: OpenCode 52%)
DEFAULT_STACKED_PCT = 50  # dashboard height in stacked mode
MIN_PCT = 15
MAX_PCT = 85

# Status line ----------------------------------------------------------------

PROBE_MAX_AGE_S = 4.0  # reuse the LM Studio answer for this long (tmux asks every 2 s)
HARD_DEADLINE_S = 2  # the whole status command gives up after this many seconds

# Tokyo Night colors (same roles as the TUI: theme.py)
C = {
    "cyan": "#7dcfff",
    "text": "#c0caf5",
    "muted": "#8a91b8",
    "dim": "#565f89",
    "green": "#9ece6a",
    "yellow": "#e0af68",
    "red": "#f7768e",
    "blue": "#7aa2f7",
    "bg": "#1a1b26",
}
SEP = "   "


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "telemachus"


def settings_path() -> Path:
    """TELEMACHUS_COCKPIT_SETTINGS overrides the path (tests and test runs use it)."""
    override = os.environ.get("TELEMACHUS_COCKPIT_SETTINGS")
    return Path(override) if override else config_dir() / "cockpit.json"


def cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "telemachus" / "tmux-status.json"


def clamp_pct(value: Any, default: int) -> int:
    """An int percent in MIN_PCT..MAX_PCT, or `default` when the value is not a number."""
    try:
        pct = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(MIN_PCT, min(MAX_PCT, pct))


def load_settings(path: Optional[Path] = None) -> Dict[str, Any]:
    """{'split_pct': int, 'stacked_pct': int, 'stacked': bool}. Missing or broken file = defaults."""
    path = path or settings_path()
    raw: Dict[str, Any] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            raw = data
    except (OSError, ValueError):
        pass
    return {
        "split_pct": clamp_pct(raw.get("split_pct"), DEFAULT_SPLIT_PCT),
        "stacked_pct": clamp_pct(raw.get("stacked_pct"), DEFAULT_STACKED_PCT),
        "stacked": bool(raw.get("stacked", False)),
    }


def save_settings(updates: Dict[str, Any], path: Optional[Path] = None) -> bool:
    """Merges `updates` into the settings file (atomic write). Returns True when the file changed."""
    path = path or settings_path()
    current: Dict[str, Any] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            current = data
    except (OSError, ValueError):
        pass
    merged = dict(current)
    merged.update(updates)
    if merged == current:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def save_layout(stacked: str, pct_h: str, pct_v: str, path: Optional[Path] = None) -> bool:
    """Called by the tmux hook with the raw option values. Bad values keep what the file has."""
    current = load_settings(path)
    updates: Dict[str, Any] = {}
    if str(stacked).strip() in ("0", "1"):
        updates["stacked"] = str(stacked).strip() == "1"
    for key, raw in (("split_pct", pct_h), ("stacked_pct", pct_v)):
        if str(raw).strip():
            updates[key] = clamp_pct(raw, current[key])
    if not updates:
        return False
    return save_settings(updates, path)


SAVE_DELAY_S = 0.3  # a mouse drag fires many layout changes; only the last one is saved
TMUX_QUERY_TIMEOUT_S = 1.0
LAYOUT_QUERY = "#{@telemachus_gen} #{@telemachus_stacked} #{@telemachus_pct_h} #{@telemachus_pct_v}"


def save_layout_from_tmux(tmux_bin: str, socket: str, window: str, gen: str,
                          path: Optional[Path] = None, delay: float = SAVE_DELAY_S) -> bool:
    """Debounced save run by the layout hook. Each hook run bumps @telemachus_gen, so a run that
    sees a newer gen after its delay exits and leaves the save to the newer run."""
    import subprocess

    if delay > 0:
        time.sleep(delay)
    try:
        res = subprocess.run(
            [tmux_bin, "-S", socket, "display-message", "-p", "-t", window, LAYOUT_QUERY],
            capture_output=True, text=True, timeout=TMUX_QUERY_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    fields = res.stdout.split()
    if res.returncode != 0 or len(fields) != 4 or fields[0] != str(gen).strip():
        return False
    return save_layout(fields[1], fields[2], fields[3], path)


# Telemetry -----------------------------------------------------------------


def _telemetry_module():
    """telemetry.py without importing the cockpit package (its __init__ pulls in the TUI)."""
    name = "telemachus.cockpit.telemetry"
    if name in sys.modules:
        return sys.modules[name]
    alias = "_telemachus_cockpit_telemetry"
    if alias in sys.modules:
        return sys.modules[alias]
    spec = importlib.util.spec_from_file_location(alias, Path(__file__).with_name("telemetry.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod  # dataclasses look the module up while the class is built
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _probe_cached(tel: Any, now: float, max_age: float = PROBE_MAX_AGE_S) -> Tuple[bool, list, Optional[int]]:
    """LM Studio probe, reused from a small cache file for `max_age` seconds."""
    cpath = cache_path()
    try:
        data = json.loads(cpath.read_text(encoding="utf-8"))
        if data.get("base") == tel.lm_base and 0 <= now - float(data.get("at", 0)) < max_age:
            return bool(data.get("reachable")), list(data.get("models") or []), data.get("ctx_max")
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    reachable, models, ctx = tel._probe_lm_studio()
    try:
        cpath.parent.mkdir(parents=True, exist_ok=True)
        tmp = cpath.with_name(f".{cpath.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps({"at": now, "base": tel.lm_base, "reachable": reachable, "models": models, "ctx_max": ctx}),
            encoding="utf-8",
        )
        os.replace(tmp, cpath)
    except OSError:
        pass
    return reachable, models, ctx


def header_info(now: Optional[float] = None) -> Any:
    """HeaderInfo for the status line: cached LM Studio probe, live status file, RAM."""
    mod = _telemetry_module()
    now = time.time() if now is None else now
    url = os.environ.get("TELEMACHUS_LMSTUDIO_URL") or "http://127.0.0.1:1234/v1/models"
    tel = mod.CockpitTelemetry(lm_studio_url=url)
    reachable, models, ctx = _probe_cached(tel, now)
    ram_used, ram_total = tel._read_ram()
    tel._poll = mod._PollResult(
        reachable=reachable, models=models, ctx_max=ctx, ram_used_gb=ram_used, ram_total_gb=ram_total, polled_at=now
    )
    return tel.header_info(now=now)


def _esc(text: str) -> str:
    """tmux reads #[...] and #{...} in #() output; a literal # must be doubled."""
    return str(text).replace("#", "##")


def _fg(color: str) -> str:
    return f"#[fg={C[color]}]"


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 10000 else f"{n:,}"


def format_status(info: Any) -> str:
    """tmux status-right text for a HeaderInfo. Unmeasured values are left out, never invented."""
    parts = []
    model = _esc(info.model) if getattr(info, "model", "") else ""
    lm = getattr(info, "lm", "unknown")
    if lm == "writing":
        s = f"{_fg('cyan')}● "
        if model:
            s += f"{_fg('text')}{model} "
        s += f"{_fg('cyan')}writing"
        if info.tokens is not None:
            s += f" {_fmt_tokens(int(info.tokens))} tok"
        if info.tps:
            s += f"{_fg('muted')} · {info.tps:.0f} t/s"
        parts.append(s)
    elif lm == "reading":
        s = f"{_fg('yellow')}● "
        if model:
            s += f"{_fg('text')}{model} "
        s += f"{_fg('yellow')}reading prompt"
        if info.elapsed_s is not None:
            s += f" {int(info.elapsed_s)}s"
        parts.append(s)
    elif lm == "ready":
        parts.append(f"{_fg('green')}● {_fg('text')}{model} {_fg('muted')}ready")
    elif lm == "no_model":
        parts.append(f"{_fg('yellow')}○ {_fg('muted')}no model loaded")
    elif lm == "offline":
        parts.append(f"{_fg('muted')}○ LM Studio offline")
    else:
        parts.append(f"{_fg('muted')}○ LM Studio -")
    if info.ram_used_gb is not None and info.ram_total_gb is not None:
        parts.append(f"{_fg('muted')}ram {_fg('text')}{info.ram_used_gb:.1f}/{info.ram_total_gb:.0f}G")
    return SEP.join(parts) + "#[default]"


def status_line() -> str:
    try:
        return format_status(header_info())
    except Exception:
        return f"{_fg('muted')}○ LM Studio -#[default]"


def _deadline(_signum: int, _frame: Any) -> None:
    sys.stdout.write(f"{_fg('muted')}○ LM Studio -#[default]\n")
    sys.stdout.flush()
    os._exit(0)


def main(argv: Optional[list] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "status"
    if cmd == "status":
        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, _deadline)
            signal.alarm(HARD_DEADLINE_S)
        print(status_line())
        return 0
    if cmd == "save-layout" and len(args) == 5:
        save_layout_from_tmux(*args[1:5])
        return 0
    print("usage: tmux_helper.py status | save-layout TMUX SOCKET WINDOW GEN", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
