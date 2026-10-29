"""Tier 2: Soft Recovery and macOS Trash Safety Net.

Replaces unbacked permanent deletions (`rm`, `os.remove`, `shutil.rmtree`) with
macOS Trash operations so that accidental deletions are fully recoverable via Finder or Cmd+Z.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
from typing import Optional

from telemachus.fs import guard_log
from telemachus.fs.boundaries import resolve_clean_path
from telemachus.fs.models import TrashResult

# TELEMACHUS_TRASH_AUDIT moves the audit log (tests and screenshots use a scratch file).
DEFAULT_AUDIT_LOG = Path(
    os.environ.get("TELEMACHUS_TRASH_AUDIT")
    or (Path(__file__).resolve().parent.parent.parent / "data" / "trash_audit.jsonl")
).expanduser()


def move_to_trash(target: str | Path, audit_log: Optional[Path] = None) -> TrashResult:
    """Safely moves a file or directory to macOS Trash.
    
    Uses macOS native `/usr/bin/trash` or osascript fallback.
    Records the operation to an audit log.
    """
    resolved = resolve_clean_path(target)
    if not resolved.exists():
        return TrashResult(
            success=False,
            original_path=str(resolved),
            message=f"Path '{resolved}' does not exist.",
            timestamp=time.time(),
        )

    # 1. Primary: /usr/bin/trash
    trash_bin = "/usr/bin/trash"
    if Path(trash_bin).is_file() and os.access(trash_bin, os.X_OK):
        try:
            res = subprocess.run(
                [trash_bin, str(resolved)],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0:
                result = TrashResult(
                    success=True,
                    original_path=str(resolved),
                    message=f"Moved '{resolved}' to macOS Trash via /usr/bin/trash.",
                    timestamp=time.time(),
                )
                _record_audit(result, audit_log or DEFAULT_AUDIT_LOG)
                return result
        except Exception:
            pass

    # 2. Fallback: AppleScript Finder delete
    try:
        apple_script = f'tell application "Finder" to delete POSIX file "{resolved}"'
        res = subprocess.run(
            ["/usr/bin/osascript", "-e", apple_script],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            result = TrashResult(
                success=True,
                original_path=str(resolved),
                message=f"Moved '{resolved}' to macOS Trash via AppleScript.",
                timestamp=time.time(),
            )
            _record_audit(result, audit_log or DEFAULT_AUDIT_LOG)
            return result
    except Exception:
        pass

    # 3. Last-resort fallback: rename into ~/.Trash
    trash_dir = Path.home() / ".Trash"
    if trash_dir.is_dir():
        try:
            dest = trash_dir / f"{resolved.name}_{int(time.time())}"
            resolved.rename(dest)
            result = TrashResult(
                success=True,
                original_path=str(resolved),
                trash_path=str(dest),
                message=f"Moved '{resolved}' to ~/.Trash folder directly.",
                timestamp=time.time(),
            )
            _record_audit(result, audit_log or DEFAULT_AUDIT_LOG)
            return result
        except Exception as e:
            return TrashResult(
                success=False,
                original_path=str(resolved),
                message=f"Failed to move to Trash: {e}",
                timestamp=time.time(),
            )

    return TrashResult(
        success=False,
        original_path=str(resolved),
        message="Unable to safely move file to Trash (no supported trash mechanism available).",
        timestamp=time.time(),
    )


def _record_audit(result: TrashResult, log_path: Path):
    """Appends trash action to audit log and to the guard event log (neither ever raises)."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(result.to_dict()) + "\n")
    except Exception:
        pass
    guard_log.record(
        "TRASH",
        target=result.original_path,
        rule=guard_log.RULE_SOFT_DELETE,
        detail="moved to Trash",
        action="delete",
        ts=result.timestamp or None,
    )
