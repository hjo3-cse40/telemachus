"""Guarded File Editing and Execution Engine.

Every mutating operation (edit, write, delete, shell exec) is passed through
the 4-tier LayaGuard before any changes occur on the filesystem.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any, Dict, Optional

from telemachus.fs.boundaries import evaluate_tier0_path, resolve_clean_path
from telemachus.fs.checkpoint import create_git_checkpoint
from telemachus.fs.guard import LayaGuard
from telemachus.fs.models import FileActionType, GuardVerdict

_guard = LayaGuard()


def edit_file(
    path: str,
    target_content: str,
    replacement_content: str,
    user_intent: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Surgically edits a file by replacing target_content with replacement_content."""
    p = resolve_clean_path(path)
    if not p.is_file():
        return {"success": False, "error": f"File '{p}' does not exist or is not a file."}

    # Evaluate guardrails
    decision = _guard.evaluate(
        action_type=FileActionType.EDIT,
        target=str(p),
        user_intent=user_intent,
        details={
            "target_len": len(target_content),
            "replacement_len": len(replacement_content),
        },
    )

    if decision.verdict == GuardVerdict.BLOCK:
        return {
            "success": False,
            "error": f"Edit blocked by guard: {decision.reason}",
            "decision": decision.to_dict(),
        }

    if decision.verdict == GuardVerdict.NEEDS_CONFIRMATION:
        return {
            "success": False,
            "needs_confirmation": True,
            "prompt": decision.reason,
            "decision": decision.to_dict(),
        }

    # Read current content
    try:
        with open(p, "r", encoding="utf-8") as f:
            current_text = f.read()
    except Exception as e:
        return {"success": False, "error": f"Failed to read file '{p}': {e}"}

    count = current_text.count(target_content)
    if count == 0:
        return {
            "success": False,
            "error": "target_content not found in file. Ensure exact matching whitespace and indentation.",
        }
    if count > 1:
        return {
            "success": False,
            "error": f"target_content matched {count} occurrences. Please provide a more specific, unique block of code.",
        }

    new_text = current_text.replace(target_content, replacement_content, 1)

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "path": str(p),
            "changes_verified": True,
            "decision": decision.to_dict(),
        }

    # Create git checkpoint before applying edit
    chk = create_git_checkpoint(p, message=f"Before editing {p.name}")

    # Atomic write via temp file
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=p.parent, prefix=".tmp_edit_")
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_f:
            tmp_f.write(new_text)
        os.replace(tmp_path, p)
        return {
            "success": True,
            "path": str(p),
            "message": f"Successfully updated '{p.name}'.",
            "git_checkpoint": chk.to_dict(),
            "decision": decision.to_dict(),
        }
    except Exception as e:
        return {"success": False, "error": f"Atomic edit failed: {e}"}


def write_file(
    path: str,
    content: str,
    overwrite: bool = False,
    user_intent: Optional[str] = None,
) -> Dict[str, Any]:
    """Safely creates a new file or overwrites an existing file."""
    p = resolve_clean_path(path)
    exists = p.exists()

    if exists and not overwrite:
        return {
            "success": False,
            "error": f"File '{p}' already exists. Pass overwrite=True to replace it.",
        }

    decision = _guard.evaluate(
        action_type=FileActionType.WRITE,
        target=str(p),
        user_intent=user_intent,
        details={"content_len": len(content), "overwriting": exists},
    )

    if decision.verdict == GuardVerdict.BLOCK:
        return {
            "success": False,
            "error": f"Write blocked by guard: {decision.reason}",
            "decision": decision.to_dict(),
        }

    if decision.verdict == GuardVerdict.NEEDS_CONFIRMATION:
        return {
            "success": False,
            "needs_confirmation": True,
            "prompt": decision.reason,
            "decision": decision.to_dict(),
        }

    # Auto-checkpoint if modifying existing file in git repo
    chk = None
    if exists:
        chk = create_git_checkpoint(p, message=f"Before overwriting {p.name}")

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=p.parent, prefix=".tmp_write_")
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_f:
            tmp_f.write(content)
        os.replace(tmp_path, p)
        return {
            "success": True,
            "path": str(p),
            "bytes_written": len(content.encode("utf-8")),
            "git_checkpoint": chk.to_dict() if chk else None,
            "decision": decision.to_dict(),
        }
    except Exception as e:
        return {"success": False, "error": f"Failed to write file '{p}': {e}"}


def safe_delete(
    path: str, user_intent: Optional[str] = None, confirmed: bool = False
) -> Dict[str, Any]:
    """Moves a file or folder to macOS Trash with Laya Guardrail gating."""
    p = resolve_clean_path(path)
    if not p.exists():
        return {"success": False, "error": f"Path '{p}' does not exist."}

    # Auto-checkpoint git repo before deletion
    chk = create_git_checkpoint(p, message=f"Before deleting {p.name}")

    success, msg, trash_res = _guard.safe_delete(
        p, user_intent=user_intent, confirmed=confirmed
    )
    return {
        "success": success,
        "message": msg,
        "path": str(p),
        "needs_confirmation": not success and "Confirmation required" in msg,
        "trash_result": trash_res.to_dict() if trash_res else None,
        "git_checkpoint": chk.to_dict(),
    }


def run_command(
    command: str,
    cwd: Optional[str] = None,
    user_intent: Optional[str] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """Runs a shell command after vetting through Tier 0 and Tier 1 guards."""
    work_dir = resolve_clean_path(cwd or Path.cwd())

    # Tier 0 & Tier 1 evaluation
    decision = _guard.evaluate(
        action_type=FileActionType.EXECUTE,
        target=command,
        user_intent=user_intent,
        details={"cwd": str(work_dir)},
    )

    if decision.verdict == GuardVerdict.BLOCK:
        return {
            "success": False,
            "error": f"Command blocked by guard: {decision.reason}",
            "decision": decision.to_dict(),
        }

    if decision.verdict == GuardVerdict.NEEDS_CONFIRMATION:
        return {
            "success": False,
            "needs_confirmation": True,
            "prompt": decision.reason,
            "decision": decision.to_dict(),
        }

    # Checkpoint if command could modify workspace
    chk = None
    if any(k in command for k in ["git", "rm", "mv", "cp", "npm", "pip", "make"]):
        chk = create_git_checkpoint(work_dir, message=f"Before running: {command[:40]}")

    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[:32000],
            "stderr": proc.stderr[:16000],
            "command": command,
            "cwd": str(work_dir),
            "git_checkpoint": chk.to_dict() if chk else None,
            "decision": decision.to_dict(),
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": f"Command timed out after {timeout} seconds.",
            "command": command,
        }
    except Exception as e:
        return {"success": False, "error": f"Command execution failed: {e}"}
