"""Tier 3: Git Auto-Checkpointing & Snapshot Mechanism.

Automatically captures repository state or lightweight checkpoints prior
to multi-file mutations, ensuring instant rollback capability.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time
from typing import Optional

from telemachus.fs import guard_log
from telemachus.fs.boundaries import resolve_clean_path
from telemachus.fs.models import GitCheckpointResult


def find_git_root(path: str | Path) -> Optional[Path]:
    """Finds the root of the Git repository enclosing the given path."""
    current = resolve_clean_path(path)
    if current.is_file():
        current = current.parent

    while current != current.parent:
        if (current / ".git").is_dir():
            return current
        current = current.parent

    return None


def create_git_checkpoint(
    path: str | Path, message: Optional[str] = None
) -> GitCheckpointResult:
    """Takes a lightweight Git stash or temporary checkpoint tag if inside a git repository.

    A successful checkpoint is also written to the guard event log (never raises).
    """
    result = _create_git_checkpoint(path, message)
    _log_checkpoint(result, message)
    return result


def _log_checkpoint(result: GitCheckpointResult, message: Optional[str]) -> None:
    if not result.success or not result.checkpoint_id:
        return
    try:
        repo = Path(result.repo_path).name or result.repo_path
        clean = "clean" in (result.message or "").lower()
        note = (message or "").strip() or "checkpoint"
        guard_log.record(
            "CHECKPOINT",
            target=f"{repo} @ {result.checkpoint_id[:7]}",
            rule=guard_log.RULE_CHECKPOINT,
            detail=f"{note} (tree clean)" if clean else note,
            action="checkpoint",
        )
    except Exception:
        pass


def _create_git_checkpoint(path: str | Path, message: Optional[str] = None) -> GitCheckpointResult:
    git_root = find_git_root(path)
    if not git_root:
        return GitCheckpointResult(
            success=False,
            repo_path=str(path),
            message="Target is not within a Git repository; skipped checkpoint.",
        )

    msg = message or f"telemachus-checkpoint-{int(time.time())}"
    try:
        # Check if there are any working changes to stash
        status_res = subprocess.run(
            ["git", "-C", str(git_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        if not status_res.stdout.strip():
            # Working tree is already clean
            head_res = subprocess.run(
                ["git", "-C", str(git_root), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            head_commit = head_res.stdout.strip()
            return GitCheckpointResult(
                success=True,
                repo_path=str(git_root),
                checkpoint_id=head_commit,
                message=f"Git tree is clean at commit {head_commit}.",
            )

        # Create a stash checkpoint with keep-index
        stash_res = subprocess.run(
            ["git", "-C", str(git_root), "stash", "create", msg],
            capture_output=True,
            text=True,
            check=False,
        )
        stash_sha = stash_res.stdout.strip()
        if stash_sha:
            # Store reference in stash reflog
            subprocess.run(
                ["git", "-C", str(git_root), "stash", "store", "-m", msg, stash_sha],
                capture_output=True,
                text=True,
                check=False,
            )
            return GitCheckpointResult(
                success=True,
                repo_path=str(git_root),
                checkpoint_id=stash_sha,
                message=f"Created Git stash checkpoint '{msg}' ({stash_sha[:8]}).",
            )

        return GitCheckpointResult(
            success=True,
            repo_path=str(git_root),
            message="Git checkpoint completed without pending diffs.",
        )
    except Exception as e:
        return GitCheckpointResult(
            success=False,
            repo_path=str(git_root),
            message=f"Failed to create git checkpoint: {e}",
        )
