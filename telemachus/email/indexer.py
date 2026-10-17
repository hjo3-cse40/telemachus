"""Offline background indexer for batch processing and local SQLite storage."""
from __future__ import annotations

from datetime import datetime
import time
from typing import Any, Callable, Dict, List, Optional

from telemachus.config.loader import get_account
from telemachus.email.client import IMAPClient
from telemachus.email.models import AccountConfig, EmailMessage
from telemachus.email.store import EmailIndexStore
from telemachus.email.triage import triage_messages


def _parse_epoch(date_str: str) -> int:
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        return int(dt.timestamp())
    except Exception:
        return 0


def index_emails(
    account_id: str = "yahoo",
    folder: str = "INBOX",
    max_count: int = 50,
    batch_size: int = 10,
    predictor: Optional[Callable[[List[dict]], List[dict]]] = None,
    store: Optional[EmailIndexStore] = None,
) -> Dict[str, Any]:
    """Scans and indexes unindexed emails into local SQLite with Laya classifications.

    Safe and non-destructive: uses IMAP read-only mode, preserves unread flags.
    """
    acc = get_account(account_id)
    if not acc or not acc.enabled or not acc.password:
        raise ValueError(f"Account '{account_id}' is disabled or not configured.")

    if store is None:
        store = EmailIndexStore()

    target_folder = folder or acc.inbox_folder
    indexed_uids = store.get_indexed_uids(acc.id, target_folder)

    with IMAPClient(acc) as client:
        # Search all UIDs in folder
        status, _ = client.conn.select(f'"{target_folder}"', readonly=True)
        if status != "OK":
            raise ValueError(f"Cannot select folder {target_folder}")

        status, data = client.conn.uid("SEARCH", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return {
                "account_id": acc.id,
                "folder": target_folder,
                "scanned": 0,
                "newly_indexed": 0,
                "total_indexed": len(indexed_uids),
                "message": "No emails found in mailbox.",
            }

        all_uids = [x.decode() for x in data[0].split() if x]
        # Reverse to get newest emails first
        all_uids.reverse()

        # Filter out UIDs already in local SQLite
        needed_uids = [u for u in all_uids if u not in indexed_uids]
        target_uids = needed_uids[:max_count]

        if not target_uids:
            return {
                "account_id": acc.id,
                "folder": target_folder,
                "scanned": len(all_uids),
                "newly_indexed": 0,
                "total_indexed": len(indexed_uids),
                "message": "All requested emails are already indexed locally in SQLite.",
            }

        newly_indexed_count = 0

        # Process in batches
        for i in range(0, len(target_uids), batch_size):
            chunk = target_uids[i : i + batch_size]
            messages: List[EmailMessage] = []
            for uid in chunk:
                msg = client.fetch_message(uid, folder=target_folder)
                if msg:
                    messages.append(msg)

            if not messages:
                continue

            # Classify batch with Laya
            triaged = triage_messages(messages, predictor=predictor)
            triaged_map = {r.uid: r for r in triaged}

            # Prepare records for SQLite store
            records: List[Dict[str, Any]] = []
            for msg in messages:
                t = triaged_map.get(msg.uid)
                category = t.category if t else "unclear"
                attention = t.attention if t else "routine"
                preservation = t.preservation if t else "review_first"
                confidence = t.confidence if t else 0.5
                reasons = t.reasons if t else []

                records.append(
                    {
                        "account_id": acc.id,
                        "folder": target_folder,
                        "uid": msg.uid,
                        "sender": msg.sender,
                        "subject": msg.subject,
                        "date_str": msg.date_str,
                        "date_epoch": _parse_epoch(msg.date_str),
                        "snippet": msg.preview_snippet(160),
                        "has_attachments": bool(msg.attachments),
                        "flags": msg.flags,
                        "list_id": msg.list_id,
                        "category": category,
                        "attention": attention,
                        "preservation": preservation,
                        "confidence": confidence,
                        "reasons": reasons,
                    }
                )

            saved = store.save_classified_batch(records)
            newly_indexed_count += saved

        total_now = len(store.get_indexed_uids(acc.id, target_folder))
        return {
            "account_id": acc.id,
            "folder": target_folder,
            "total_inbox_messages": len(all_uids),
            "newly_indexed": newly_indexed_count,
            "total_indexed": total_now,
            "remaining_unindexed": max(0, len(needed_uids) - newly_indexed_count),
        }
