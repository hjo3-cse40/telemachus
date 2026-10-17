"""Filesystem Exploration and Navigation Tools.

Optimized for token efficiency on local models (Qwen 3.5 9B) by filtering
vendor noise (node_modules, .git, .venv), truncating binary data, and supporting
precise line windowing.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from telemachus.fs.boundaries import evaluate_tier0_path, resolve_clean_path

IGNORE_DIRECTORIES = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    ".cache",
    "build",
    "dist",
}

BINARY_EXTENSIONS = {
    ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".tar",
    ".gz", ".mp3", ".mp4", ".mov", ".iso", ".sqlite3", ".db", ".dylib",
    ".so", ".a", ".o", ".bin", ".wasm",
}


def is_binary_file(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    try:
        with open(path, "rb") as f:
            chunk = f.read(1024)
            if b"\x00" in chunk:
                return True
    except Exception:
        pass
    return False


def format_bytes(size: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024.0:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024.0
    return f"{size:.1f} TB"


def list_dir(
    path: str = ".",
    max_depth: int = 2,
    show_hidden: bool = False,
    limit: int = 100,
) -> Dict[str, Any]:
    """Lists files and directories with depth controls and noise suppression."""
    root = resolve_clean_path(path)
    ok, reason = evaluate_tier0_path(root, is_write=False)
    if not ok:
        return {"error": reason, "path": str(root)}

    if not root.exists():
        return {"error": f"Path '{root}' does not exist.", "path": str(root)}

    if root.is_file():
        stat = root.stat()
        return {
            "type": "file",
            "path": str(root),
            "size_bytes": stat.st_size,
            "size_formatted": format_bytes(stat.st_size),
            "modified": time.ctime(stat.st_mtime),
        }

    entries = []
    root_level = len(root.parts)

    for current_dir, dirs, files in os.walk(root):
        cur_path = Path(current_dir)
        depth = len(cur_path.parts) - root_level
        if depth > max_depth:
            dirs.clear()
            continue

        # Filter out ignored directories in-place
        dirs[:] = [
            d for d in dirs
            if d not in IGNORE_DIRECTORIES and (show_hidden or not d.startswith("."))
        ]

        # Process dirs at current level
        for d in dirs:
            dir_full = cur_path / d
            rel = dir_full.relative_to(root)
            entries.append(
                {
                    "type": "dir",
                    "name": d,
                    "rel_path": str(rel),
                    "full_path": str(dir_full),
                    "depth": depth + 1,
                }
            )
            if len(entries) >= limit:
                break

        if len(entries) >= limit:
            break

        # Process files
        for f in files:
            if not show_hidden and f.startswith("."):
                continue
            file_full = cur_path / f
            rel = file_full.relative_to(root)
            try:
                size = file_full.stat().st_size
                entries.append(
                    {
                        "type": "file",
                        "name": f,
                        "rel_path": str(rel),
                        "full_path": str(file_full),
                        "size_bytes": size,
                        "size_formatted": format_bytes(size),
                        "depth": depth + 1,
                    }
                )
            except Exception:
                pass

            if len(entries) >= limit:
                break

        if len(entries) >= limit:
            break

    return {
        "root": str(root),
        "total_returned": len(entries),
        "entries": entries,
    }


def read_file(
    path: str,
    start_line: int = 1,
    end_line: Optional[int] = None,
    max_bytes: int = 64000,
) -> Dict[str, Any]:
    """Reads a text file with line range windowing."""
    p = resolve_clean_path(path)
    ok, reason = evaluate_tier0_path(p, is_write=False)
    if not ok:
        return {"error": reason, "path": str(p)}

    if not p.is_file():
        return {"error": f"File '{p}' does not exist or is not a regular file."}

    if is_binary_file(p):
        return {
            "error": "Binary file detected; reading raw content is suppressed.",
            "path": str(p),
            "size": p.stat().st_size,
        }

    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total_lines = len(lines)
        s_idx = max(1, start_line)
        e_idx = min(total_lines, end_line) if end_line is not None else min(total_lines, s_idx + 300)

        selected = lines[s_idx - 1 : e_idx]
        formatted = "".join(f"{s_idx + i:4d}: {line}" for i, line in enumerate(selected))

        if len(formatted.encode("utf-8")) > max_bytes:
            formatted = formatted[:max_bytes] + "\n... [truncated due to size limit]"

        return {
            "path": str(p),
            "total_lines": total_lines,
            "start_line": s_idx,
            "end_line": e_idx,
            "content": formatted,
        }
    except Exception as e:
        return {"error": f"Failed to read file '{p}': {e}"}


def search_files(
    pattern: str,
    root_dir: str = ".",
    max_results: int = 50,
) -> Dict[str, Any]:
    """Finds files matching a glob pattern or regex."""
    root = resolve_clean_path(root_dir)
    ok, reason = evaluate_tier0_path(root, is_write=False)
    if not ok:
        return {"error": reason, "root": str(root)}

    results = []
    regex = None
    try:
        regex = re.compile(pattern, re.I)
    except Exception:
        pass

    for cur_dir, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRECTORIES and not d.startswith(".")]

        for f in files:
            file_path = Path(cur_dir) / f
            rel = file_path.relative_to(root)
            rel_str = str(rel)

            matched = False
            if regex and (regex.search(f) or regex.search(rel_str)):
                matched = True
            elif pattern.lower() in f.lower() or pattern.lower() in rel_str.lower():
                matched = True

            if matched:
                try:
                    size = file_path.stat().st_size
                    results.append(
                        {
                            "name": f,
                            "relative_path": rel_str,
                            "full_path": str(file_path),
                            "size": format_bytes(size),
                        }
                    )
                except Exception:
                    pass

            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break

    return {
        "pattern": pattern,
        "root": str(root),
        "match_count": len(results),
        "matches": results,
    }


def grep_text(
    query: str,
    root_dir: str = ".",
    file_pattern: Optional[str] = None,
    max_results: int = 50,
    is_regex: bool = False,
) -> Dict[str, Any]:
    """Searches file contents for matching queries or regular expressions."""
    root = resolve_clean_path(root_dir)
    ok, reason = evaluate_tier0_path(root, is_write=False)
    if not ok:
        return {"error": reason, "root": str(root)}

    pattern = re.compile(query, re.I) if is_regex else re.compile(re.escape(query), re.I)
    ext_filter = None
    if file_pattern and file_pattern.startswith("*."):
        ext_filter = file_pattern[1:].lower()

    matches = []
    for cur_dir, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRECTORIES and not d.startswith(".")]

        for f in files:
            if f.startswith("."):
                continue
            if ext_filter and not f.lower().endswith(ext_filter):
                continue

            file_path = Path(cur_dir) / f
            if is_binary_file(file_path):
                continue

            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
                    for line_idx, line in enumerate(fh, 1):
                        if pattern.search(line):
                            matches.append(
                                {
                                    "file": str(file_path.relative_to(root)),
                                    "line_number": line_idx,
                                    "line_text": line.strip()[:200],
                                }
                            )
                            if len(matches) >= max_results:
                                break
            except Exception:
                pass

            if len(matches) >= max_results:
                break
        if len(matches) >= max_results:
            break

    return {
        "query": query,
        "root": str(root),
        "total_matches": len(matches),
        "matches": matches,
    }


def stat_path(path: str) -> Dict[str, Any]:
    """Retrieves file/folder metadata, permissions, and git tracking status."""
    p = resolve_clean_path(path)
    ok, reason = evaluate_tier0_path(p, is_write=False)
    if not ok:
        return {"error": reason, "path": str(p)}

    if not p.exists():
        return {"error": f"Path '{p}' does not exist.", "path": str(p)}

    st = p.stat()
    return {
        "path": str(p),
        "name": p.name,
        "is_dir": p.is_dir(),
        "is_file": p.is_file(),
        "is_symlink": p.is_symlink(),
        "size_bytes": st.st_size,
        "size_formatted": format_bytes(st.st_size),
        "mode_octal": oct(st.st_mode)[-3:],
        "created": time.ctime(st.st_ctime),
        "modified": time.ctime(st.st_mtime),
    }
