"""Command-line interface for Telemachus email operations."""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from telemachus.config.loader import get_account, get_enabled_accounts, load_accounts
from telemachus.email.client import IMAPClient
from telemachus.email.draft import create_draft
from telemachus.email.triage import triage_messages


def cmd_status(args: argparse.Namespace) -> None:
    """Displays connection and mailbox status for all accounts."""
    accounts = load_accounts()
    print("=" * 76)
    print(" Telemachus Mailbox Status")
    print("=" * 76)

    for acc in accounts:
        status_str = "ENABLED" if acc.enabled else "DISABLED"
        print(f"\nAccount: [{acc.id}] {acc.name} ({status_str})")
        print(f"  Provider: {acc.provider.upper()} | User: {acc.email or '(not set)'}")

        if not acc.enabled:
            print("  Status: Account is disabled in config/accounts.yaml.")
            continue

        if not acc.email or not acc.password:
            print("  Status: Missing email or password in .env.")
            continue

        try:
            with IMAPClient(acc, timeout=10) as client:
                st = client.get_mailbox_status(acc.inbox_folder)
                print(f"  Connection: OK (TLS {acc.imap_host}:{acc.imap_port})")
                print(f"  Inbox Messages: {st['total']} total | {st['unseen']} unseen")
        except Exception as e:
            print(f"  Connection FAILED: {type(e).__name__}: {e}")

    print("\n" + "=" * 76)


def cmd_list(args: argparse.Namespace) -> None:
    """Lists recent email headers for an account."""
    acc = get_account(args.account)
    if not acc:
        print(f"Error: Account '{args.account}' not found.")
        sys.exit(1)
    if not acc.enabled or not acc.password:
        print(f"Error: Account '{args.account}' is disabled or missing credentials.")
        sys.exit(1)

    print(f"Fetching recent {args.limit} messages from {acc.name} ({acc.inbox_folder})...\n")
    try:
        with IMAPClient(acc) as client:
            headers = client.list_recent_headers(
                folder=args.folder or acc.inbox_folder,
                limit=args.limit,
                unread_only=args.unread,
            )
            if not headers:
                print("No messages found.")
                return

            print(f"{'UID':<8} | {'FROM':<30} | {'DATE':<16} | {'SUBJECT'}")
            print("-" * 88)
            for h in headers:
                clean_from = (h.sender[:28] + "..") if len(h.sender) > 30 else h.sender
                clean_date = h.date.strftime("%Y-%m-%d %H:%M") if h.date else (h.date_str[:16])
                clean_subj = h.subject[:45]
                print(f"{h.uid:<8} | {clean_from:<30} | {clean_date:<16} | {clean_subj}")
    except Exception as e:
        print(f"Error fetching headers: {type(e).__name__}: {e}")
        sys.exit(1)


def cmd_show(args: argparse.Namespace) -> None:
    """Displays full email content by UID."""
    acc = get_account(args.account)
    if not acc:
        print(f"Error: Account '{args.account}' not found.")
        sys.exit(1)

    with IMAPClient(acc) as client:
        msg = client.fetch_message(args.uid, folder=args.folder or acc.inbox_folder)
        if not msg:
            print(f"Message UID {args.uid} not found.")
            return

        print("=" * 76)
        print(f"Subject : {msg.subject}")
        print(f"From    : {msg.sender}")
        print(f"To      : {', '.join(msg.recipients)}")
        print(f"Date    : {msg.date_str}")
        print(f"UID     : {msg.uid} (Account: {msg.account_id})")
        if msg.attachments:
            print(f"Attach  : {[a['filename'] for a in msg.attachments]}")
        print("=" * 76)
        print("\n" + msg.body_text + "\n")
        print("=" * 76)


def cmd_triage(args: argparse.Namespace) -> None:
    """Triages recent emails using Laya classifier."""
    acc = get_account(args.account)
    if not acc:
        print(f"Error: Account '{args.account}' not found.")
        sys.exit(1)

    print(f"Running Laya Triage on latest {args.limit} emails from [{acc.id}] {acc.name}...")
    with IMAPClient(acc) as client:
        headers = client.list_recent_headers(
            folder=args.folder or acc.inbox_folder,
            limit=args.limit,
            unread_only=args.unread,
        )
        messages = []
        for h in headers:
            m = client.fetch_message(h.uid, folder=args.folder or acc.inbox_folder)
            if m:
                messages.append(m)

    if not messages:
        print("No messages to triage.")
        return

    print(f"Loaded {len(messages)} messages. Evaluating with Laya (MPS ModernBERT)...")
    results = triage_messages(messages)

    print("\n" + "=" * 96)
    print(f"{'UID':<8} | {'CATEGORY':<20} | {'ATTENTION':<16} | {'ACTION':<10} | {'SUBJECT'}")
    print("=" * 96)
    for r in results:
        subj = (r.subject[:35] + "..") if len(r.subject) > 37 else r.subject
        action = "KEEP" if r.preservation == "preserve" else ("CLEAN" if r.preservation == "cleanup_candidate" else "REVIEW")
        print(f"{r.uid:<8} | {r.category:<20} | {r.attention:<16} | {action:<10} | {subj}")

    print("=" * 96)
    print("Classification Summary:")
    categories = {}
    for r in results:
        categories[r.category] = categories.get(r.category, 0) + 1
    for cat, count in sorted(categories.items()):
        print(f"  - {cat}: {count}")
    print("=" * 96)


def cmd_draft(args: argparse.Namespace) -> None:
    """Safely creates a draft in the remote Drafts folder."""
    acc = get_account(args.account)
    if not acc:
        print(f"Error: Account '{args.account}' not found.")
        sys.exit(1)

    print(f"Creating draft for {args.to} under account '{acc.name}'...")
    res = create_draft(
        account=acc,
        to=args.to,
        subject=args.subject,
        body=args.body,
        drafts_folder=args.drafts_folder,
    )
    print("\nDraft created successfully:")
    print(f"  Account : {res['account_id']}")
    print(f"  Folder  : {res['folder']}")
    print(f"  To      : {res['to']}")
    print(f"  Subject : {res['subject']}")
    print(f"  Notice  : {res['message']}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m telemachus.email",
        description="Telemachus Email Subsystem & Laya Triage CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = subparsers.add_parser("status", help="Show mailbox status and connections")
    p_status.set_defaults(func=cmd_status)

    # list
    p_list = subparsers.add_parser("list", help="List recent email headers")
    p_list.add_argument("--account", default="yahoo", help="Account ID (default: yahoo)")
    p_list.add_argument("--folder", default=None, help="IMAP folder (default: INBOX)")
    p_list.add_argument("--limit", type=int, default=5, help="Number of emails (default: 5)")
    p_list.add_argument("--unread", action="store_true", help="Fetch unread only")
    p_list.set_defaults(func=cmd_list)

    # show
    p_show = subparsers.add_parser("show", help="Display full email body by UID")
    p_show.add_argument("uid", help="Message UID")
    p_show.add_argument("--account", default="yahoo", help="Account ID (default: yahoo)")
    p_show.add_argument("--folder", default=None, help="IMAP folder (default: INBOX)")
    p_show.set_defaults(func=cmd_show)

    # triage
    p_triage = subparsers.add_parser("triage", help="Triage recent emails with Laya")
    p_triage.add_argument("--account", default="yahoo", help="Account ID (default: yahoo)")
    p_triage.add_argument("--folder", default=None, help="IMAP folder (default: INBOX)")
    p_triage.add_argument("--limit", type=int, default=5, help="Number of emails to triage")
    p_triage.add_argument("--unread", action="store_true", help="Triage unread only")
    p_triage.set_defaults(func=cmd_triage)

    # draft
    p_draft = subparsers.add_parser("draft", help="Safely save a draft in Drafts folder")
    p_draft.add_argument("--account", default="yahoo", help="Account ID (default: yahoo)")
    p_draft.add_argument("--to", required=True, help="Recipient email address")
    p_draft.add_argument("--subject", required=True, help="Draft subject")
    p_draft.add_argument("--body", required=True, help="Draft body text")
    p_draft.add_argument("--drafts-folder", default=None, help="Drafts folder name")
    p_draft.set_defaults(func=cmd_draft)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
