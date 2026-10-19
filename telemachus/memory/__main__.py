"""CLI entry point for managing the Telemachus unified memory vault."""
import argparse
import json
from pathlib import Path
import sys
import time

from telemachus.memory.cache import MemoryCache
from telemachus.memory.importer import MemoryImporter
from telemachus.memory.models import PrivacyTier
from telemachus.memory.vault import DEFAULT_VAULT_ROOT, MemoryVault


def cmd_init(args):
    vault = MemoryVault(args.vault_dir)
    vault.initialize()
    cache = MemoryCache(vault=vault)
    print(f"Initialized Unified Memory Vault at: {vault.root_dir}")
    print(f"Private dir permissions: {oct(vault.private_dir.stat().st_mode)[-3:]}")
    print(f"SQLite cache ready at: {cache.db_path}")


def cmd_import(args):
    vault = MemoryVault(args.vault_dir)
    vault.initialize()
    cache = MemoryCache(vault=vault)
    importer = MemoryImporter(vault, cache)

    odysseus_count = 0
    odysseus_path = Path(args.odysseus_path) if args.odysseus_path else Path("~/.odysseus/data/memory.json")
    if odysseus_path.exists():
        odysseus_count = importer.import_odysseus(odysseus_path)
        print(f"Imported {odysseus_count} memories from Odysseus ({odysseus_path})")

    cursor_count = 0
    cursor_dir = Path(args.cursor_dir) if args.cursor_dir else Path.home() / ".cursor" / "rules"
    if cursor_dir.exists():
        cursor_count = importer.import_cursor_rules(cursor_dir)
        print(f"Imported {cursor_count} rule documents from Cursor ({cursor_dir})")

    stats = cache.get_stats()
    print("Import complete! Current cache stats:")
    print(json.dumps(stats, indent=2))


def cmd_search(args):
    vault = MemoryVault(args.vault_dir)
    cache = MemoryCache(vault=vault)
    tier_limit = PrivacyTier.from_str(args.tier) if args.tier else None

    from telemachus.memory.recall import recall, render_recall
    print(render_recall(recall(cache, args.query, limit=args.limit, tier_limit=tier_limit)))


def cmd_status(args):
    vault = MemoryVault(args.vault_dir)
    cache = MemoryCache(vault=vault)
    stats = cache.get_stats()
    print("=== Unified Memory Vault Status ===")
    print(f"Vault Root: {vault.root_dir}")
    print(f"Private Root: {vault.private_dir} (Mode: {oct(vault.private_dir.stat().st_mode)[-3:] if vault.private_dir.exists() else 'N/A'})")
    print(f"Total memories in cache: {stats['total_memories']}")
    print(f"By Tier: {stats['by_tier']}")
    print(f"By Category: {stats['by_category']}")


def cmd_recent(args):
    import time
    vault = MemoryVault(args.vault_dir)
    cache = MemoryCache(vault=vault)
    tier_limit = PrivacyTier.from_str(args.tier) if args.tier else None
    memories = cache.get_recent(limit=args.limit, tier_limit=tier_limit)
    print(f"=== Last {len(memories)} Recorded Memories ===")
    for idx, m in enumerate(memories, 1):
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.timestamp))
        tier_badge = "🔒 [PRIVATE]" if m.tier == PrivacyTier.TIER_2_PRIVATE else ("📁 [PROJECT]" if m.tier == PrivacyTier.TIER_1_PROJECT else "🌐 [PUBLIC]")
        pin_tag = "📌 " if m.pinned else ""
        print(f"{idx}. {time_str} | {tier_badge} [{m.category}] {pin_tag}{m.text}")


def cmd_tail(args):
    import time
    vault = MemoryVault(args.vault_dir)
    if not vault.events_file.exists():
        print(f"No events log found at {vault.events_file}")
        return

    print(f"=== Live Memory Events Stream ({vault.events_file}) ===")
    print("Listening for new memory extractions (Press Ctrl+C to exit)...\n")

    def format_event(raw_line: str) -> str:
        try:
            ev = json.loads(raw_line)
            t = time.strftime("%H:%M:%S", time.localtime(ev.get("timestamp", 0)))
            name = ev.get("event", "UNKNOWN")
            details = ev.get("details", {})
            return f"[{t}] {name:15} | {details}"
        except Exception:
            return raw_line

    with open(vault.events_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
        for line in lines[-args.lines:]:
            if line.strip():
                print(format_event(line.strip()))
        f.seek(0, 2)
        try:
            while True:
                line = f.readline()
                if line and line.strip():
                    print(format_event(line.strip()))
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nStopped.")


def cmd_review(args):
    vault = MemoryVault(args.vault_dir)
    pending = vault.load_pending()
    if not pending:
        print("Review list is empty.")
        return
    print(f"=== {len(pending)} fact(s) waiting for review ===")
    for e in pending:
        meta = e.metadata or {}
        print(f"\n[{e.id[:8]}] {e.text}")
        print(f"   kind: {meta.get('kind', 'self')}   category: {e.category}   source: {meta.get('source_ref') or e.source}")
        if meta.get("quote"):
            print(f"   quote: \"{meta['quote']}\" (match: {meta.get('quote_match')})")
        print(f"   why it needs review: {meta.get('review_reason')}")
    print("\nApprove: python -m telemachus.memory approve <id>   Reject: python -m telemachus.memory reject <id>")


def _resolve(args, approve: bool):
    vault = MemoryVault(args.vault_dir)
    cache = MemoryCache(vault=vault)
    found, error = vault.find_pending(args.id)
    entry = vault.resolve_pending(found.id, approve=approve) if found else None
    if entry is None:
        print(error or f"No pending fact with id {args.id!r}.")
        sys.exit(1)
    if approve:
        cache.upsert_entry(entry)
        print(f"Approved and saved: {entry.text}")
    else:
        print(f"Rejected: {entry.text}")


def cmd_approve(args):
    _resolve(args, approve=True)


def cmd_reject(args):
    _resolve(args, approve=False)


def cmd_health(args):
    from telemachus.memory.health import MemoryHealth
    vault = MemoryVault(args.vault_dir)
    summary = MemoryHealth(vault).summary()
    print(f"Memory pipeline: {summary['status']}")
    for name, state in sorted(summary["components"].items()):
        flag = "OK" if state.get("ok", True) else "FAILING"
        line = f"  {name:22} {flag:8} failures={state.get('total_failures', 0)} ok={state.get('total_successes', 0)}"
        if state.get("total_rejections"):
            line += f" rejected={state['total_rejections']}"
        print(line)
        if not state.get("ok", True):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(state.get("last_error_at") or 0))
            print(f"      last error ({when}): {state.get('last_error')}")
    print(f"Pending review: {len(vault.load_pending())}   Failed turns waiting for retry: {len(vault.load_failed_turns())}")


def cmd_retry(args):
    from telemachus.memory.intake import MemoryIntake
    vault = MemoryVault(args.vault_dir)
    intake = MemoryIntake(vault=vault, cache=MemoryCache(vault=vault))
    reports = intake.retry_failed()
    if not reports:
        print("No failed turns.")
    for r in reports:
        print(f"{r.status}: saved={r.saved} review={r.queued_for_review} reason={r.reason}")


def cmd_source(args):
    from telemachus.memory.recall import render_source
    vault = MemoryVault(args.vault_dir)
    print(render_source(vault, MemoryCache(vault=vault), args.id))


def main():
    parser = argparse.ArgumentParser(description="Telemachus Unified Memory Manager")
    parser.add_argument("--vault-dir", default=None, help="Root directory for memory vault (default: ~/.memory)")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # init
    p_init = subparsers.add_parser("init", help="Initialize memory vault and directories")
    p_init.set_defaults(func=cmd_init)

    # import
    p_import = subparsers.add_parser("import", help="Import memories from Odysseus and Cursor")
    p_import.add_argument("--odysseus-path", default=None, help="Path to Odysseus memory.json")
    p_import.add_argument("--cursor-dir", default=None, help="Path to ~/.cursor/rules")
    p_import.set_defaults(func=cmd_import)

    # search
    p_search = subparsers.add_parser("search", help="Search memories by keyword")
    p_search.add_argument("query", help="Keyword or phrase to search")
    p_search.add_argument("--tier", default=None, help="Limit to tier: tier_0_public, tier_1_project, tier_2_private")
    p_search.add_argument("--limit", type=int, default=10, help="Max results to return")
    p_search.set_defaults(func=cmd_search)

    # recent
    p_recent = subparsers.add_parser("recent", help="Show recently recorded memories")
    p_recent.add_argument("--tier", default=None, help="Limit to tier")
    p_recent.add_argument("--limit", type=int, default=10, help="Max memories to show (default: 10)")
    p_recent.set_defaults(func=cmd_recent)

    # tail
    p_tail = subparsers.add_parser("tail", help="Live tail memory extraction events")
    p_tail.add_argument("-n", "--lines", type=int, default=10, help="Number of previous events to show (default: 10)")
    p_tail.set_defaults(func=cmd_tail)

    # status
    p_status = subparsers.add_parser("status", help="Show vault status and statistics")
    p_status.set_defaults(func=cmd_status)

    p_review = subparsers.add_parser("review", help="List facts waiting for your review")
    p_review.set_defaults(func=cmd_review)
    p_approve = subparsers.add_parser("approve", help="Approve a pending fact (moves it into memory)")
    p_approve.add_argument("id", help="Pending fact id (first 8 characters are enough)")
    p_approve.set_defaults(func=cmd_approve)
    p_reject = subparsers.add_parser("reject", help="Reject a pending fact")
    p_reject.add_argument("id", help="Pending fact id (first 8 characters are enough)")
    p_reject.set_defaults(func=cmd_reject)
    p_health = subparsers.add_parser("health", help="Show failures and rejections in the memory pipeline")
    p_health.set_defaults(func=cmd_health)
    p_retry = subparsers.add_parser("retry", help="Run failed turns through the intake again")
    p_retry.set_defaults(func=cmd_retry)
    p_source = subparsers.add_parser("source", help="Show one memory with its source and quote")
    p_source.add_argument("id", help="Memory id (first 8 characters are enough)")
    p_source.set_defaults(func=cmd_source)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
