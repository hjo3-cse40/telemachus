"""Tier 0: Deterministic Boundary and Blast Radius Shield.

Enforces zero-cost, hard boundaries on system paths, sensitive credentials,
and destructive shell commands.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import List, Optional, Set, Tuple

# System directories that must NEVER be modified or touched by agent tools
SYSTEM_DIRECTORIES: Set[str] = {
    "/",
    "/System",
    "/usr",
    "/bin",
    "/sbin",
    "/Library",
    "/private/etc",
    "/private/var/root",
    "/etc",
    "/dev",
    "/net",
}

TEMP_DIRECTORIES: Set[str] = {
    "/tmp",
    "/private/tmp",
    "/var/tmp",
    "/private/var/tmp",
    "/var/folders",
    "/private/var/folders",
}

# Credential and security paths that must NEVER be read or modified
SENSITIVE_SECRET_PATTERNS: List[re.Pattern] = [
    re.compile(r"/\.ssh(/.*)?$"),
    re.compile(r"/\.gnupg(/.*)?$"),
    re.compile(r"/\.aws(/.*)?$"),
    re.compile(r"/\.kube(/.*)?$"),
    re.compile(r"/Library/Keychains(/.*)?$"),
    re.compile(r"/\.(bash|zsh)_history$"),
    re.compile(r"/\.config/opencode/.*(\.dat|session|secret)"),
    # Hard Privacy Shield for Private Financial Records and Setup YAML
    re.compile(r"/\.memory/private/finances/(setup\.ya?ml|.*\.sqlite3.*)$"),
]

# Destructive system commands that should never be run under any circumstances
FORBIDDEN_COMMAND_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b(mkfs|fdisk|parted|diskutil\s+(erase|reformat|partitionDisk))\b", re.I),
    re.compile(r"\bdd\s+if=.*of=/dev/", re.I),
    re.compile(r"\brm\s+(-[rfRF]+\s+)+(/\s*$|~/?\s*$|/\*|~\/\*)"),
    re.compile(r"\bchmod\s+(-R\s+)?(777|000)\s+/", re.I),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.I),  # fork bomb
    re.compile(r">\s*/dev/sd[a-z]", re.I),
]


def resolve_clean_path(path: str | Path) -> Path:
    """Expands user tildes and resolves symlinks into a canonical absolute path."""
    p = Path(path).expanduser()
    try:
        return p.resolve()
    except Exception:
        return p.absolute()


def is_system_path(path: str | Path) -> bool:
    """Checks if a path resolves inside protected macOS/UNIX system root directories."""
    resolved = resolve_clean_path(path)
    path_str = str(resolved)
    
    # Temp/scratch paths are allowed
    for temp_dir in TEMP_DIRECTORIES:
        if path_str == temp_dir or path_str.startswith(temp_dir.rstrip("/") + "/"):
            return False

    # Check exact match or subdirectories of system dirs
    for sys_dir in SYSTEM_DIRECTORIES:
        if path_str == sys_dir or path_str.startswith(sys_dir.rstrip("/") + "/"):
            return True
    return False


def is_sensitive_secret(path: str | Path) -> bool:
    """Checks if a path resolves to user secrets, keys, or auth databases."""
    resolved = resolve_clean_path(path)
    path_str = str(resolved)
    
    for pattern in SENSITIVE_SECRET_PATTERNS:
        if pattern.search(path_str):
            return True
            
    # Foreign .env files outside active workspaces
    if resolved.name == ".env":
        # Check if it's outside current working directory / workspace
        try:
            cwd = Path.cwd().resolve()
            if not path_str.startswith(str(cwd)):
                return True
        except Exception:
            pass

    return False


def evaluate_tier0_path(path: str | Path, is_write: bool = False) -> Tuple[bool, str]:
    """Evaluates whether a target path is permissible under Tier 0 rules.
    
    Returns (permitted: bool, reason: str).
    """
    resolved = resolve_clean_path(path)
    path_str = str(resolved)

    # 1. Check for sensitive secrets (blocked for both read and write)
    if is_sensitive_secret(resolved):
        return False, f"Tier 0 Violation: Access to sensitive security credential path '{path_str}' is forbidden."

    # 2. Check for system paths
    if is_system_path(resolved):
        if is_write:
            return False, f"Tier 0 Violation: Modification to system directory '{path_str}' is forbidden."
        # Read-only of system dirs is also heavily restricted unless it's standard /usr/bin or /opt/homebrew
        if not (path_str.startswith("/usr/local") or path_str.startswith("/opt/homebrew") or path_str == "/"):
            # Prevent inspecting raw system files
            if path_str.startswith(("/System", "/private/var/root", "/private/etc", "/Library", "/etc")):
                return False, f"Tier 0 Violation: Inspection of core system tree '{path_str}' is restricted."

    return True, "Path conforms to Tier 0 boundary checks."


def evaluate_tier0_command(command: str) -> Tuple[bool, str]:
    """Evaluates whether a shell command violates Tier 0 rules."""
    cmd_clean = command.strip()
    
    for pattern in FORBIDDEN_COMMAND_PATTERNS:
        if pattern.search(cmd_clean):
            return False, f"Tier 0 Violation: Command matches catastrophic signature: {pattern.pattern}"
            
    # Check if raw `rm -rf` is being used on home or root
    if re.search(r"\brm\s+.*-(r|f)", cmd_clean):
        # We enforce Tier 2 trash replacement or explicit safety checks
        if "/" in cmd_clean or "~" in cmd_clean:
            # Check if attempting root or home wipe
            tokens = cmd_clean.split()
            for token in tokens:
                resolved = resolve_clean_path(token)
                if str(resolved) in ("/", str(Path.home())):
                    return False, "Tier 0 Violation: Catastrophic deletion targeted at root or home directory."

    return True, "Command conforms to Tier 0 boundary checks."
