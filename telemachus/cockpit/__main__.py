"""CLI entry point for Telemachus Cockpit."""
import argparse
import sys

from telemachus.cockpit.launcher import CockpitLauncher
from telemachus.cockpit.telemetry import CockpitTelemetry
from telemachus.cockpit.tui import main as tui_main


def cmd_launch(args):
    launcher = CockpitLauncher(session_name=args.session)
    sys.exit(launcher.launch(attach=not args.no_attach))


def cmd_dashboard(args):
    tui_main()


def cmd_telemetry(args):
    telemetry = CockpitTelemetry()
    print(telemetry.format_single_line_header())


def main():
    parser = argparse.ArgumentParser(description="Telemachus Terminal Cockpit")
    subparsers = parser.add_subparsers(dest="subcommand")

    # launch (default)
    p_launch = subparsers.add_parser("launch", help="Launch split tmux cockpit with OpenCode")
    p_launch.add_argument("--session", default="telemachus-cockpit", help="tmux session name")
    p_launch.add_argument("--no-attach", action="store_true", help="Do not attach to session")
    p_launch.set_defaults(func=cmd_launch)

    # dashboard
    p_dash = subparsers.add_parser("dashboard", help="Run standalone TUI dashboard in current terminal")
    p_dash.set_defaults(func=cmd_dashboard)

    # telemetry
    p_tel = subparsers.add_parser("telemetry", help="Print single-line telemetry header")
    p_tel.set_defaults(func=cmd_telemetry)

    args = parser.parse_args()
    if not args.subcommand:
        # Default behavior: launch cockpit session
        cmd_launch(argparse.Namespace(session="telemachus-cockpit", no_attach=False))
    else:
        args.func(args)


if __name__ == "__main__":
    main()
