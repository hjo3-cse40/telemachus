"""Tmux Session Orchestrator for Telemachus Cockpit.

Constructs the Tokyo Night split session with native mouse interaction,
top status bar telemetry, and the OpenCode execution harness.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from typing import Optional

from telemachus.config.loader import find_project_root


class CockpitLauncher:
    """Manages the tmux cockpit lifecycle."""

    def __init__(self, session_name: str = "telemachus-cockpit"):
        self.session_name = session_name
        self.project_root = find_project_root()
        self.tmux_bin = shutil.which("tmux") or "/opt/homebrew/bin/tmux"

    def is_tmux_available(self) -> bool:
        return os.path.exists(self.tmux_bin)

    def _run_tmux(self, *args: str) -> subprocess.CompletedProcess:
        cmd = [self.tmux_bin] + list(args)
        return subprocess.run(cmd, capture_output=True, text=True)

    def build_tmux_config_commands(self) -> list[list[str]]:
        """Returns Tokyo Night theme commands and mouse settings."""
        return [
            # Enable mouse mode for trackpad scrolling and clicking
            ["set-option", "-g", "mouse", "on"],
            # Top status bar position
            ["set-option", "-g", "status-position", "top"],
            # Status bar styling
            ["set-option", "-g", "status-style", "bg=#1a1b26,fg=#c0caf5"],
            ["set-option", "-g", "status-left-length", "40"],
            ["set-option", "-g", "status-right-length", "90"],
            ["set-option", "-g", "status-left", "#[fg=#7dcfff,bold] 🛡️ TELEMACHUS #[fg=#414868]| "],
            ["set-option", "-g", "status-right", "#[fg=#bb9af7]%H:%M #[fg=#414868]| #[fg=#9ece6a]18GB Mac #[fg=#7dcfff]● Qwen 3.5 9B #[fg=#7aa2f7]● Laya MPS "],
            # Tokyo Night Borders
            ["set-option", "-g", "pane-border-style", "fg=#24283b"],
            ["set-option", "-g", "pane-active-border-style", "fg=#7dcfff"],
        ]

    def launch(self, attach: bool = True) -> int:
        """Launches the split cockpit session."""
        if not self.is_tmux_available():
            print(f"Error: tmux is required but was not found at '{self.tmux_bin}'.", file=sys.stderr)
            return 1

        in_tmux = "TMUX" in os.environ

        # Kill existing session if not inside tmux
        if not in_tmux:
            self._run_tmux("kill-session", "-t", self.session_name)

        target = self.session_name
        py_bin = sys.executable

        # 1. Start tmux session in background with Left Pane running TUI dashboard
        tui_cmd = f"cd '{self.project_root}' && {py_bin} -m telemachus.cockpit.tui"
        if in_tmux:
            # Create a new window inside current tmux
            res = self._run_tmux("new-window", "-n", "cockpit", tui_cmd)
        else:
            res = self._run_tmux("new-session", "-d", "-s", target, "-n", "cockpit", tui_cmd)

        if res.returncode != 0:
            print(f"Failed to create tmux session/window: {res.stderr}", file=sys.stderr)
            return res.returncode

        # 2. Apply theme and mouse configurations
        for conf_cmd in self.build_tmux_config_commands():
            self._run_tmux(*conf_cmd)

        # 3. Split right pane (52% width) for OpenCode harness with neon cursor
        opencode_greeting = (
            "printf '\\033[?25h\\033[5 q'; "
            "echo '\\033[1;36m================================================\\033[0m'; "
            "echo '\\033[1;35m  Telemachus Execution Harness (OpenCode CLI)   \\033[0m'; "
            "echo '\\033[1;36m================================================\\033[0m'; "
            "echo 'Connected to local Telemachus MCP & Ingress Proxy.'; "
            "echo 'Type your prompt turn or press Ctrl+B then left arrow to switch panes.'; "
            "echo ''; "
            "opencode || exec /bin/zsh"
        )
        split_target = f"{target}:0.0" if not in_tmux else ""
        split_args = ["split-window", "-h", "-p", "52", "-c", str(self.project_root)]
        if split_target:
            split_args.extend(["-t", split_target])
        split_args.append(opencode_greeting)
        self._run_tmux(*split_args)

        # 4. Attach to session
        if attach and not in_tmux:
            os.execvp(self.tmux_bin, [self.tmux_bin, "attach-session", "-t", target])

        return 0
