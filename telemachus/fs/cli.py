"""CLI tool for testing and exercising Telemachus Filesystem Guard."""
from __future__ import annotations

import argparse
import json
import sys

from telemachus.fs.editor import edit_file, run_command, safe_delete, write_file
from telemachus.fs.explorer import grep_text, list_dir, read_file, search_files, stat_path
from telemachus.fs.guard import LayaGuard
from telemachus.fs.models import FileActionType


def main():
    parser = argparse.ArgumentParser(description="Telemachus Guarded Filesystem CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = subparsers.add_parser("list", help="List directory contents")
    p_list.add_argument("path", nargs="?", default=".", help="Directory to list")
    p_list.add_argument("--depth", type=int, default=2, help="Max depth")
    p_list.add_argument("--hidden", action="store_true", help="Include hidden files")

    # read
    p_read = subparsers.add_parser("read", help="Read file content")
    p_read.add_argument("path", help="Path to file")
    p_read.add_argument("--start", type=int, default=1, help="Start line")
    p_read.add_argument("--end", type=int, default=None, help="End line")

    # search
    p_search = subparsers.add_parser("search", help="Find files by name")
    p_search.add_argument("pattern", help="Filename pattern to search")
    p_search.add_argument("--root", default=".", help="Root directory")

    # grep
    p_grep = subparsers.add_parser("grep", help="Search file contents")
    p_grep.add_argument("query", help="Text query to search for")
    p_grep.add_argument("--root", default=".", help="Root directory")
    p_grep.add_argument("--ext", default=None, help="File extension filter (e.g. *.py)")

    # check (guard test)
    p_check = subparsers.add_parser("check", help="Evaluate an action against LayaGuard")
    p_check.add_argument("action", choices=["read", "edit", "write", "delete", "execute"])
    p_check.add_argument("target", help="Target path or shell command")
    p_check.add_argument("--intent", default=None, help="Stated user intent")

    # delete
    p_delete = subparsers.add_parser("delete", help="Safely move path to macOS Trash")
    p_delete.add_argument("path", help="Path to delete")
    p_delete.add_argument("--intent", default=None, help="User intent description")
    p_delete.add_argument("--confirm", action="store_true", help="Confirm deletion if elevated")

    args = parser.parse_args()

    if args.command == "list":
        res = list_dir(args.path, max_depth=args.depth, show_hidden=args.hidden)
        print(json.dumps(res, indent=2))

    elif args.command == "read":
        res = read_file(args.path, start_line=args.start, end_line=args.end)
        if "error" in res:
            print(f"Error: {res['error']}", file=sys.stderr)
            sys.exit(1)
        print(f"File: {res['path']} (lines {res['start_line']}-{res['end_line']}/{res['total_lines']}):\n")
        print(res["content"])

    elif args.command == "search":
        res = search_files(args.pattern, root_dir=args.root)
        print(json.dumps(res, indent=2))

    elif args.command == "grep":
        res = grep_text(args.query, root_dir=args.root, file_pattern=args.ext)
        print(json.dumps(res, indent=2))

    elif args.command == "check":
        guard = LayaGuard()
        action_map = {
            "read": FileActionType.READ,
            "edit": FileActionType.EDIT,
            "write": FileActionType.WRITE,
            "delete": FileActionType.DELETE,
            "execute": FileActionType.EXECUTE,
        }
        dec = guard.evaluate(action_map[args.action], target=args.target, user_intent=args.intent)
        print(json.dumps(dec.to_dict(), indent=2))

    elif args.command == "delete":
        res = safe_delete(args.path, user_intent=args.intent, confirmed=args.confirm)
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
