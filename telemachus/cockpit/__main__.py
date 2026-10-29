"""CLI entry point for Telemachus Cockpit.

  python -m telemachus.cockpit [launch] [--split N] [--stacked | --side] [--no-attach]
  python -m telemachus.cockpit dashboard          TUI only, in this terminal
  python -m telemachus.cockpit telemetry [--tmux] one status line (--tmux: tmux status-right text)
"""
import argparse
import sys

from telemachus.cockpit.launcher import DEFAULT_SESSION, DEFAULT_SOCKET, CockpitLauncher

SUBCOMMANDS = ("launch", "dashboard", "telemetry")


def _percent(text: str) -> int:
    try:
        value = int(str(text).rstrip("%"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a percent: {text}")
    if not 15 <= value <= 85:
        raise argparse.ArgumentTypeError("use a value from 15 to 85")
    return value


def add_launch_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--session", default=DEFAULT_SESSION, help="tmux session name")
    p.add_argument(
        "--socket", default=DEFAULT_SOCKET,
        help="name of the cockpit's own tmux server (tmux -L NAME); default: telemachus",
    )
    p.add_argument("--no-attach", action="store_true", help="build the session but do not attach")
    p.add_argument(
        "--split", type=_percent, default=None, metavar="PCT",
        help="dashboard share of the window in percent (15-85); default: the last size you used",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--stacked", dest="stacked", action="store_const", const=True, default=None,
                      help="dashboard on top, OpenCode below")
    mode.add_argument("--side", dest="stacked", action="store_const", const=False,
                      help="dashboard on the left, OpenCode on the right")


def cmd_launch(args):
    launcher = CockpitLauncher(
        session_name=args.session,
        socket_name=args.socket,
        split=args.split,
        stacked=args.stacked,
    )
    sys.exit(launcher.launch(attach=not args.no_attach))


def cmd_dashboard(args):
    from telemachus.cockpit.tui import main as tui_main

    tui_main()


def cmd_telemetry(args):
    if args.tmux:
        from telemachus.cockpit import tmux_helper

        print(tmux_helper.status_line())
        return
    from telemachus.cockpit.telemetry import CockpitTelemetry

    print(CockpitTelemetry().format_single_line_header())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telemachus-cockpit",
        description="Telemachus Terminal Cockpit. With no subcommand it runs `launch`.",
    )
    subparsers = parser.add_subparsers(dest="subcommand")

    p_launch = subparsers.add_parser("launch", help="split tmux cockpit: dashboard + OpenCode (default)")
    add_launch_arguments(p_launch)
    p_launch.set_defaults(func=cmd_launch)

    p_dash = subparsers.add_parser("dashboard", help="run only the TUI dashboard in this terminal")
    p_dash.set_defaults(func=cmd_dashboard)

    p_tel = subparsers.add_parser("telemetry", help="print one telemetry line")
    p_tel.add_argument("--tmux", action="store_true", help="tmux status-right text (fast, never hangs)")
    p_tel.set_defaults(func=cmd_telemetry)
    return parser


def normalize_argv(argv):
    """No subcommand means `launch`, so `telemachus-cockpit --split 50` works."""
    argv = list(argv)
    if not argv:
        return ["launch"]
    if argv[0] in ("-h", "--help") or argv[0] in SUBCOMMANDS:
        return argv
    return ["launch"] + argv


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(normalize_argv(sys.argv[1:] if argv is None else argv))
    args.func(args)


if __name__ == "__main__":
    main()
