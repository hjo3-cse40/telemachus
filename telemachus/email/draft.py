"""Safe email drafting subsystem for Telemachus.

Drafts are written strictly to the account's remote IMAP Drafts folder.
Sending is never performed programmatically; the user must review and send from their mail client.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage as PyEmailMessage
from email.utils import formatdate, make_msgid
import imaplib
import time
from typing import Any, Dict, Optional

from telemachus.email.models import AccountConfig


def create_draft(
    account: AccountConfig,
    to: str,
    subject: str,
    body: str,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    drafts_folder: Optional[str] = None,
) -> Dict[str, Any]:
    """Appends an RFC822 draft message to the IMAP Drafts folder.

    Never sends an email.
    """
    if not account.email or not account.password:
        raise ValueError(f"Account {account.id} missing credentials")
    if not to.strip():
        raise ValueError("Recipient 'to' cannot be empty")
    if not subject.strip():
        subject = "(No Subject)"

    # Build RFC5322 message
    msg = PyEmailMessage()
    msg["From"] = account.email
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(time.time(), localtime=True)
    msg["Message-ID"] = make_msgid(domain="telemachus.local")

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    # Set content
    msg.set_content(body)

    target_folder = drafts_folder or account.drafts_folder

    # Connect via IMAP
    conn: Optional[imaplib.IMAP4_SSL | imaplib.IMAP4] = None
    try:
        if account.use_ssl:
            conn = imaplib.IMAP4_SSL(account.imap_host, account.imap_port)
        else:
            conn = imaplib.IMAP4(account.imap_host, account.imap_port)

        status, _ = conn.login(account.email, account.password)
        if status != "OK":
            raise RuntimeError(f"IMAP login failed for {account.email}")

        raw_bytes = msg.as_bytes()
        # IMAP APPEND flags: (\Draft \Seen)
        flags = "(\\Draft \\Seen)"
        append_time = imaplib.Time2Internaldate(time.time())

        # Attempt append to drafts folder
        status, resp = conn.append(f'"{target_folder}"', flags, append_time, raw_bytes)
        if status != "OK":
            # If target folder name is different (e.g. Yahoo sometimes uses "Drafts" or "drafts")
            status, resp = conn.append(target_folder, flags, append_time, raw_bytes)
            if status != "OK":
                raise RuntimeError(f"Failed to append draft to {target_folder}: {resp}")

        return {
            "success": True,
            "account_id": account.id,
            "folder": target_folder,
            "to": to,
            "subject": subject,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "message": f"Draft saved safely to '{target_folder}'. Review and send from your mail client.",
        }
    finally:
        if conn:
            try:
                conn.logout()
            except Exception:
                pass
