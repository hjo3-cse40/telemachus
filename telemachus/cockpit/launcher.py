"""tmux launcher for the Telemachus cockpit.

Layout: the dashboard (curses TUI) and OpenCode share one tmux window, side by side
(dashboard on the left) or stacked (dashboard on top).

By default the cockpit runs on its own tmux server (``tmux -L telemachus``), so its options,
status bar and key bindings never reach Sam's other tmux sessions. When the launcher runs inside
an existing tmux, it opens a new window there and sets only window and session options
(no key bindings, no global options).

Resizing (own server only, no prefix needed):
  Alt+arrows       move the split by 5 cells (Shift+Alt+arrows by 1)
  Alt+h / Alt+l    focus the dashboard / OpenCode (also Alt+[ and Alt+])
  Alt+z            zoom the active pane full screen and back
  Alt+1/2/3        dashboard 35% / 50% / 65%
  Alt+=            even split
  Alt+space        side by side <-> stacked
  mouse            drag the border; click a pane to focus it

The last split (size and side/stacked) is saved by a tmux hook to
``~/.config/telemachus/cockpit.json`` and used on the next launch.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from telemachus.cockpit import tmux_helper
from telemachus.cockpit.tmux_helper import C, clamp_pct, load_settings
from telemachus.config.loader import find_project_root


DEFAULT_SOCKET = "telemachus"
DEFAULT_SESSION = "telemachus-cockpit"
WINDOW_NAME = "cockpit"
PRESETS = (35, 50, 65)
STEP_BIG = 5
STEP_SMALL = 1
STATUS_INTERVAL_S = 2
HINT_S = 8

# Env vars that choose which data the dashboard reads. tmux does not pass the caller's
# environment to an already-running server, so they are forwarded explicitly, otherwise the
# dashboard silently falls back to the real vault / mail index.
FORWARDED_ENV_VARS = (
    "TELEMACHUS_FINANCE_DIR",
    "TELEMACHUS_SETUP_YAML",
    "TELEMACHUS_EMAIL_DB",
    "TELEMACHUS_TRASH_AUDIT",
    "TELEMACHUS_MEMORY_DIR",
    "TELEMACHUS_GUARD_LOG",
    "TELEMACHUS_LMSTUDIO_URL",
    "TELEMACHUS_COLORS",
    "TELEMACHUS_TUI_DEBUG",
    "TELEMACHUS_DEMO",
)

# Pane and window options the hooks and key bindings use (user options, prefixed with @).
ROLE = "@telemachus_role"
OPT_STACKED = "@telemachus_stacked"
OPT_PCT_H = "@telemachus_pct_h"
OPT_PCT_V = "@telemachus_pct_v"
OPT_SAVED = "@telemachus_saved"
OPT_KEEP = "@telemachus_keep"
OPT_GEN = "@telemachus_gen"
HOOK = "window-layout-changed"
# Inside an existing tmux: marks cockpit windows, and a session hook (fixed high index, so it never
# replaces one of Sam's own hooks) that gives the session its old mouse setting back once the
# last cockpit window is gone.
OPT_COCKPIT_WINDOW = "@telemachus_cockpit"
MOUSE_HOOK = "window-unlinked[4271]"

# Shown on the right of the status bar for HINT_S seconds after a launch. Not a display-message:
# while tmux shows a message it does not draw pane output, so the dashboard stayed blank for the
# whole 8 s. status-right reads % (strftime), so it is doubled.
KEY_HINT = "alt+arrows resize · alt+h/l focus · alt+z zoom · alt+1/2/3 35/50/65%% · alt+= even · alt+space stack"


def home_overridden(env: Optional[Dict[str, str]] = None) -> bool:
    """True when HOME is set to a folder other than the user's real home (a sandbox or test run)."""
    env = os.environ if env is None else env
    home = env.get("HOME")
    if not home:
        return False
    try:
        import pwd

        real = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):
        return False
    return os.path.realpath(home) != os.path.realpath(real)


def forwarded_env_prefix() -> str:
    """Shell prefix like `env VAR='value' ` for the forwarded vars that are set, else ''.

    HOME goes too when it was overridden: a new tmux window takes the tmux server's HOME,
    so a sandbox run would otherwise read the real ~/.memory and ~/.config.
    """
    names = (("HOME",) if home_overridden() else ()) + FORWARDED_ENV_VARS
    pairs = [f"{k}={shlex.quote(os.environ[k])}" for k in names if os.environ.get(k)]
    return f"env {' '.join(pairs)} " if pairs else ""


def tmux_dq(text: str) -> str:
    """A tmux double-quoted string (tmux expands $VAR and reads backslashes inside these)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def format_literal(text: str) -> str:
    """Text placed inside a #{?...} branch: # and , are format characters."""
    return text.replace("#", "##").replace(",", "#,")


@dataclass
class LayoutChoice:
    """How the window is split. pct = the dashboard's share of the width (or height when stacked)."""

    stacked: bool = False
    split_pct: int = tmux_helper.DEFAULT_SPLIT_PCT
    stacked_pct: int = tmux_helper.DEFAULT_STACKED_PCT

    @property
    def pct(self) -> int:
        return self.stacked_pct if self.stacked else self.split_pct


def resolve_layout(settings: Dict, split: Optional[int] = None, stacked: Optional[bool] = None) -> LayoutChoice:
    """Saved settings, then the command-line flags on top. --split applies to the chosen mode."""
    choice = LayoutChoice(
        stacked=bool(settings.get("stacked", False)),
        split_pct=clamp_pct(settings.get("split_pct"), tmux_helper.DEFAULT_SPLIT_PCT),
        stacked_pct=clamp_pct(settings.get("stacked_pct"), tmux_helper.DEFAULT_STACKED_PCT),
    )
    if stacked is not None:
        choice.stacked = stacked
    if split is not None:
        if choice.stacked:
            choice.stacked_pct = clamp_pct(split, choice.stacked_pct)
        else:
            choice.split_pct = clamp_pct(split, choice.split_pct)
    return choice


# ---------------------------------------------------------------------------
# tmux formats used by the layout hook
# ---------------------------------------------------------------------------


def _on_dashboard(expr: str) -> str:
    """Evaluates `expr` in the dashboard pane's context (loops over the window's panes).
    The dashboard is "the pane that is not OpenCode": the OpenCode pane gets its role before the
    split, so the very first layout change (the split itself) is already measured."""
    return "#{P:#{?#{!=:#{" + ROLE + "},opencode}," + expr + ",}}"


def _ceil_pct(size: str, total: str) -> str:
    """ceil(size * 100 / total). Rounding up makes `resize-pane -x N%` give back the same size."""
    return "#{e|/|:#{e|+|:#{e|*|:#{" + size + "},100},#{e|-|:#{" + total + "},1}},#{" + total + "}}"


# Only measure when both panes are there and nothing is zoomed (a zoomed pane fills the window).
_MEASURABLE = "#{&&:#{==:#{window_panes},2},#{!=:#{window_zoomed_flag},1}}"
_SAVE_KEY = "#{" + OPT_STACKED + "}/#{" + OPT_PCT_H + "}/#{" + OPT_PCT_V + "}"


class CockpitLauncher:
    """Builds the cockpit window. Command builders are pure so tests can check them without tmux."""

    def __init__(
        self,
        session_name: str = DEFAULT_SESSION,
        socket_name: Optional[str] = DEFAULT_SOCKET,
        split: Optional[int] = None,
        stacked: Optional[bool] = None,
        settings_path: Optional[Path] = None,
        python: Optional[str] = None,
    ):
        self.session_name = session_name
        self.socket_name = socket_name or DEFAULT_SOCKET
        self.project_root = find_project_root()
        self.tmux_bin = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
        self.python = python or sys.executable
        self.helper_path = Path(tmux_helper.__file__).resolve()
        # Explicit path (or TELEMACHUS_COCKPIT_SETTINGS) is passed on to the tmux hook.
        env_path = os.environ.get("TELEMACHUS_COCKPIT_SETTINGS")
        self.settings_path = Path(settings_path) if settings_path else (Path(env_path) if env_path else None)
        self.layout = resolve_layout(load_settings(self.settings_path), split=split, stacked=stacked)

    # -- basics -------------------------------------------------------------

    def is_tmux_available(self) -> bool:
        return os.path.exists(self.tmux_bin)

    @staticmethod
    def inside_tmux(env: Optional[Dict[str, str]] = None) -> bool:
        env = os.environ if env is None else env
        return bool(env.get("TMUX"))

    def tmux_base(self, private: bool = True) -> List[str]:
        """[tmux, -L, socket] on the cockpit's own server; plain [tmux] for the caller's server."""
        return [self.tmux_bin, "-L", self.socket_name] if private else [self.tmux_bin]

    def _run(self, args: Sequence[str], private: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(self.tmux_base(private) + list(args), capture_output=True, text=True)

    def helper_command(self, *args: str) -> str:
        """Shell command that runs tmux_helper.py by path (fast: skips the cockpit package import)."""
        env = forwarded_env_prefix()
        if self.settings_path:
            env = (env or "env ") + f"TELEMACHUS_COCKPIT_SETTINGS={shlex.quote(str(self.settings_path))} "
        parts = [shlex.quote(self.python), "-I", shlex.quote(str(self.helper_path))] + [a for a in args]
        return env + " ".join(parts)

    # -- pane commands ------------------------------------------------------

    def dashboard_command(self) -> str:
        """q closes the pane (exit 0). If the dashboard stops with an error, the pane stays open with
        the error on screen until enter, so it is not lost along with the pane.

        `trap : INT` keeps this shell alive through a ctrl-C (the dashboard turns ctrl-C into a key);
        without it the shell would skip the error message once the dashboard exits. A trap with a
        command is reset to the default in the child, so the dashboard still gets SIGINT."""
        root = shlex.quote(str(self.project_root))
        run = f"{forwarded_env_prefix()}{shlex.quote(self.python)} -m telemachus.cockpit dashboard"
        on_error = (
            "rc=$?; printf '\\n  dashboard stopped (exit %s). Press enter to close this pane.\\n' \"$rc\"; read -r _"
        )
        return f"cd {root} && trap : INT && {{ {run} || {{ {on_error}; }}; }}"

    def opencode_command(self) -> str:
        # Keep the pane open when OpenCode exits, so it can be started again.
        greeting = (
            "printf '\\033[?25h\\033[5 q'; "
            "printf '\\033[1;36m  telemachus\\033[0m  \\033[38;5;103mOpenCode harness\\033[0m\\n'; "
            "printf '\\033[38;5;103m  alt+h dashboard  alt+l OpenCode  alt+z zoom  alt+arrows resize\\033[0m\\n\\n'; "
        )
        return greeting + 'opencode; exec "${SHELL:-/bin/zsh}" -l'

    def create_commands(self, private: bool, cols: int, rows: int) -> List[str]:
        """First command: the OpenCode pane in a new session (own server) or a new window (inside tmux).
        It prints `<pane_id> <window_id> <session_id>`."""
        fmt = "#{pane_id} #{window_id} #{session_id}"
        root = str(self.project_root)
        if private:
            return [
                "new-session", "-d", "-s", self.session_name, "-n", WINDOW_NAME,
                "-x", str(cols), "-y", str(rows), "-c", root, "-P", "-F", fmt,
                self.opencode_command(),
            ]
        return ["new-window", "-n", WINDOW_NAME, "-c", root, "-P", "-F", fmt, self.opencode_command()]

    def split_command(self, opencode_pane: str) -> List[str]:
        """Dashboard pane before (left of / above) the OpenCode pane. Prints its pane id."""
        direction = "-v" if self.layout.stacked else "-h"
        return [
            "split-window", "-b", direction, "-l", f"{self.layout.pct}%",
            "-t", opencode_pane, "-c", str(self.project_root),
            "-e", f"TMUX_PANE_RIGHT={opencode_pane}",
            "-P", "-F", "#{pane_id}",
            self.dashboard_command(),
        ]

    # -- options ------------------------------------------------------------

    def server_option_commands(self) -> List[List[str]]:
        """Global options. Only ever sent to the cockpit's own server (-L), never the default one."""
        status_right = (
            "#(" + self.helper_command("status").replace("%", "%%") + ")"
            + "   #[fg=" + C["muted"] + "]%H:%M "
        )
        status_left = (
            "#[fg=" + C["cyan"] + ",bold] telemachus #[nobold,fg=" + C["muted"] + "]"
            "#{?#{==:#{" + ROLE + "},dashboard},dashboard,opencode}"
            "#{?window_zoomed_flag, #[fg=" + C["yellow"] + "]zoomed,}#[default]"
        )
        return [
            ["set-option", "-g", "mouse", "on"],
            ["set-option", "-s", "escape-time", "10"],
            ["set-option", "-g", "status", "on"],
            ["set-option", "-g", "status-position", "top"],
            ["set-option", "-g", "status-interval", str(STATUS_INTERVAL_S)],
            ["set-option", "-g", "status-justify", "left"],
            ["set-option", "-g", "status-style", f"bg={C['bg']},fg={C['text']}"],
            ["set-option", "-g", "status-left-length", "40"],
            ["set-option", "-g", "status-right-length", "120"],
            ["set-option", "-g", "status-left", status_left],
            ["set-option", "-g", "status-right", status_right],
            ["set-option", "-g", "window-status-format", ""],
            ["set-option", "-g", "window-status-current-format", ""],
            ["set-option", "-g", "window-status-separator", ""],
            ["set-option", "-g", "message-style", f"bg={C['bg']},fg={C['cyan']}"],
        ]

    def hint_commands(self, session: str) -> List[List[str]]:
        """Own server: the key hint replaces this session's status-right for HINT_S seconds, then
        a delayed background command unsets it, so the global status-right shows again."""
        return [
            ["set-option", "-t", session, "status-right", f"#[fg={C['muted']}]{KEY_HINT} "],
            ["run-shell", "-b", "-d", str(HINT_S), "-C", f"set-option -u -t {session} status-right"],
        ]

    def window_option_commands(self, window: str) -> List[List[str]]:
        """Window-scoped options: borders, layout state and the hook that remembers the split."""
        lay = self.layout
        return [
            ["set-option", "-w", "-t", window, "pane-border-style", f"fg={C['dim']}"],
            ["set-option", "-w", "-t", window, "pane-active-border-style", f"fg={C['cyan']}"],
            ["set-option", "-w", "-t", window, OPT_STACKED, "1" if lay.stacked else "0"],
            ["set-option", "-w", "-t", window, OPT_PCT_H, str(lay.split_pct)],
            ["set-option", "-w", "-t", window, OPT_PCT_V, str(lay.stacked_pct)],
            ["set-option", "-w", "-t", window, OPT_SAVED, ""],
            ["set-option", "-w", "-t", window, OPT_GEN, "0"],
        ] + [["set-hook", "-w", "-t", window, f"{HOOK}[{i}]", cmd] for i, cmd in enumerate(self.layout_hook_commands())]

    def layout_hook_commands(self) -> List[str]:
        """tmux commands run (in order) after every layout change: measure the dashboard, then
        save. The save runs in the background and is debounced through @telemachus_gen."""
        stacked = _on_dashboard("#{==:#{pane_width},#{window_width}}")
        pct_h = _on_dashboard(_ceil_pct("pane_width", "window_width"))
        pct_v = _on_dashboard(_ceil_pct("pane_height", "window_height"))
        changed = "#{!=:" + _SAVE_KEY + ",#{" + OPT_SAVED + "}}"
        save = format_literal(
            self.helper_command("save-layout", shlex.quote(self.tmux_bin))
        ) + " '#{socket_path}' '#{window_id}' '#{" + OPT_GEN + "}'"
        return [
            f"set-option -wF {OPT_STACKED} " + tmux_dq("#{?" + _MEASURABLE + "," + stacked + ",#{" + OPT_STACKED + "}}"),
            f"set-option -wF {OPT_PCT_H} " + tmux_dq(
                "#{?#{&&:" + _MEASURABLE + ",#{==:#{" + OPT_STACKED + "},0}}," + pct_h + ",#{" + OPT_PCT_H + "}}"
            ),
            f"set-option -wF {OPT_PCT_V} " + tmux_dq(
                "#{?#{&&:" + _MEASURABLE + ",#{==:#{" + OPT_STACKED + "},1}}," + pct_v + ",#{" + OPT_PCT_V + "}}"
            ),
            f"set-option -wF {OPT_GEN} " + tmux_dq(
                "#{?" + changed + ",#{e|+|:#{" + OPT_GEN + "},1},#{" + OPT_GEN + "}}"
            ),
            "run-shell -b " + tmux_dq("#{?" + changed + "," + save + ",true}"),
            f"set-option -wF {OPT_SAVED} " + tmux_dq(_SAVE_KEY),
        ]

    @staticmethod
    def pane_role_command(pane: str, role: str) -> List[str]:
        """Pane user option read by the status bar ("dashboard" / "opencode") and the hook."""
        return ["set-option", "-p", "-t", pane, ROLE, role]

    @staticmethod
    def rerun_hook_command(window: str) -> List[str]:
        return ["set-hook", "-w", "-R", "-t", window, HOOK]

    def key_binding_commands(self) -> List[List[str]]:
        """No-prefix keys (root table). Only for the cockpit's own server: key tables are server-wide."""
        top = "'{top-left}'"
        cmds: List[List[str]] = []
        for key, flag in (("Left", "-L"), ("Right", "-R"), ("Up", "-U"), ("Down", "-D")):
            cmds.append(["bind-key", "-n", f"M-{key}", "resize-pane", flag, str(STEP_BIG)])
            cmds.append(["bind-key", "-n", f"M-S-{key}", "resize-pane", flag, str(STEP_SMALL)])
        for key in ("M-h", "M-["):
            cmds.append(["bind-key", "-n", key, "select-pane", "-t", "{top-left}"])
        for key in ("M-l", "M-]"):
            cmds.append(["bind-key", "-n", key, "select-pane", "-t", "{bottom-right}"])
        cmds.append(["bind-key", "-n", "M-z", "resize-pane", "-Z"])
        for n, pct in enumerate(PRESETS, start=1):
            cmds.append([
                "bind-key", "-n", f"M-{n}", "if-shell", "-F", "#{" + OPT_STACKED + "}",
                f"resize-pane -t {top} -y {pct}%", f"resize-pane -t {top} -x {pct}%",
            ])
        cmds.append([
            "bind-key", "-n", "M-=", "if-shell", "-F", "#{" + OPT_STACKED + "}",
            "select-layout even-vertical", "select-layout even-horizontal",
        ])
        # Toggle keeps each mode's own remembered size. The size is copied first because the
        # layout hook runs right after select-layout and would overwrite it with 50%.
        to_side = (
            f"set-option -wF {OPT_KEEP} '#{{{OPT_PCT_H}}}' ; select-layout even-horizontal ; "
            f"run-shell -C \"resize-pane -t {top} -x #{{{OPT_KEEP}}}%\""
        )
        to_stacked = (
            f"set-option -wF {OPT_KEEP} '#{{{OPT_PCT_V}}}' ; select-layout even-vertical ; "
            f"run-shell -C \"resize-pane -t {top} -y #{{{OPT_KEEP}}}%\""
        )
        cmds.append(["bind-key", "-n", "M-Space", "if-shell", "-F", "#{" + OPT_STACKED + "}", to_side, to_stacked])
        return cmds

    def mouse_commands(self, session: str, window: str, mouse_was_on: bool, mouse_was_local: bool) -> List[List[str]]:
        """Inside an existing tmux only. Mouse is a session option (tmux has no window-level
        mouse), and it is needed to drag the border. When it was off, it is turned on for this
        session and a hook turns it back (or unsets it) when the last cockpit window closes."""
        cmds = [["set-option", "-w", "-t", window, OPT_COCKPIT_WINDOW, "1"]]
        if mouse_was_on:
            return cmds
        restore = f"set-option -t {session} mouse off" if mouse_was_local else f"set-option -u -t {session} mouse"
        hook = (
            "if-shell -F '#{==:#{W:#{" + OPT_COCKPIT_WINDOW + "}},}' "
            f"'{restore} ; set-hook -u -t {session} {MOUSE_HOOK}'"
        )
        return cmds + [
            ["set-option", "-t", session, "mouse", "on"],
            ["set-hook", "-t", session, MOUSE_HOOK, hook],
        ]

    def _mouse_state(self, session: str) -> Tuple[bool, bool]:
        """(mouse is on now, the session has its own mouse setting) on the caller's tmux."""
        on = self._run(["display-message", "-p", "-t", session, "#{mouse}"], False).stdout.strip() == "1"
        local = bool(self._run(["show-options", "-q", "-t", session, "mouse"], False).stdout.strip())
        return on, local

    # -- launch -------------------------------------------------------------

    def _terminal_size(self) -> Tuple[int, int]:
        size = shutil.get_terminal_size((200, 50))
        return max(40, size.columns), max(12, size.lines - 1)  # one row for the status bar

    def _clear_old_session(self) -> None:
        """Own server only: remove the old cockpit. When nothing else lives there, restart the
        server too, so it picks up this launch's environment."""
        res = self._run(["list-sessions", "-F", "#{session_name}"])
        if res.returncode != 0:
            return
        names = [n for n in res.stdout.split() if n]
        if names and all(n == self.session_name for n in names):
            self._run(["kill-server"])
        elif self.session_name in names:
            self._run(["kill-session", "-t", f"={self.session_name}"])

    def attach_argv(self) -> List[str]:
        return self.tmux_base(True) + ["attach-session", "-t", f"={self.session_name}"]

    def launch(self, attach: bool = True) -> int:
        """Builds the cockpit. Returns a shell exit code (or execs `tmux attach`)."""
        if not self.is_tmux_available():
            print(f"Error: tmux is required but was not found at '{self.tmux_bin}'.", file=sys.stderr)
            return 1
        private = not self.inside_tmux()
        if private:
            self._clear_old_session()

        cols, rows = self._terminal_size()
        res = self._run(self.create_commands(private, cols, rows), private)
        if res.returncode != 0 or len(res.stdout.split()) < 3:
            print(f"Failed to create the tmux {'session' if private else 'window'}: {res.stderr.strip()}", file=sys.stderr)
            return res.returncode or 1
        opencode_pane, window, session = res.stdout.split()[:3]

        if private:
            for cmd in self.server_option_commands():
                self._run(cmd)
        else:
            # Mouse (session option) for dragging the border, given back when the cockpit closes.
            # Nothing global is changed.
            on, local = self._mouse_state(session)
            for cmd in self.mouse_commands(session, window, on, local):
                self._run(cmd, private)
        for cmd in self.window_option_commands(window):
            self._run(cmd, private)
        self._run(self.pane_role_command(opencode_pane, "opencode"), private)

        res = self._run(self.split_command(opencode_pane), private)
        if res.returncode != 0 or not res.stdout.strip():
            print(f"Failed to split the cockpit window: {res.stderr.strip()}", file=sys.stderr)
            return res.returncode or 1
        dashboard_pane = res.stdout.strip().split()[0]
        self._run(self.pane_role_command(dashboard_pane, "dashboard"), private)
        # Measure once more now that both roles are set (also saves the starting split).
        self._run(self.rerun_hook_command(window), private)
        if private:
            for cmd in self.key_binding_commands():
                self._run(cmd)
            for cmd in self.hint_commands(session):
                self._run(cmd)
        # Focus the dashboard so number keys reach the TUI, not OpenCode.
        self._run(["select-pane", "-t", dashboard_pane], private)

        if not private:
            msg = (
                "cockpit opened as a new window in this tmux. Drag the border to resize. "
                "The alt resize keys and the status bar are only set up when you start the cockpit "
                "outside tmux (it then runs on its own tmux server)."
            )
            print(msg)  # no display-message: tmux would not draw the new panes while it shows
            return 0

        if not attach:
            print(f"cockpit ready. Attach with: tmux -L {self.socket_name} attach -t {self.session_name}")
            return 0
        argv = self.attach_argv()
        os.execvp(argv[0], argv)
        return 0  # not reached
