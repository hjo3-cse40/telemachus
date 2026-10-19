"""CLI entry point for managing the Telemachus unified memory vault."""
import argparse
import json
from pathlib import Path
import sys

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

    results = cache.search(args.query, tier_limit=tier_limit, limit=args.limit)
    print(f"Found {len(results)} matches for '{args.query}':")
    for idx, r in enumerate(results, 1):
        pin_tag = "[PINNED] " if r.pinned else ""
        print(f"{idx}. [{r.tier.value.upper()}] [{r.category}] {pin_tag}{r.text}")


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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
