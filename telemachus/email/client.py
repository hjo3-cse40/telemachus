"""Safe, bounded IMAP client for Telemachus."""
from __future__ import annotations

import email
from email.header import decode_header
from email.parser import BytesParser
from email.policy import default
from email.utils import parsedate_to_datetime
import html
from html.parser import HTMLParser
import imaplib
import re
from typing import Any, Dict, List, Optional, Tuple

from telemachus.email.models import AccountConfig, EmailHeader, EmailMessage

MAX_FETCH_BYTES = 16384  # 16KB max per email to protect KV cache and memory


class _TextHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("p", "br", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden > 0:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def get_text(self) -> str:
        raw = " ".join("".join(self.parts).split())
        return html.unescape(raw)


def clean_html_to_text(html_content: str) -> str:
    """Converts HTML email body to clean readable plain text."""
    parser = _TextHTMLParser()
    try:
        parser.feed(html_content)
        return parser.get_text()
    except Exception:
        clean = re.sub(r"<[^>]+>", " ", html_content)
        return " ".join(html.unescape(clean).split())


def decode_mime_header(header_value: Optional[str]) -> str:
    """Decodes MIME encoded header words like =?utf-8?b?...?="""
    if not header_value:
        return ""
    try:
        fragments = decode_header(header_value)
        decoded = []
        for text, charset in fragments:
            if isinstance(text, bytes):
                decoded.append(text.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(str(text))
        return " ".join("".join(decoded).split())
    except Exception:
        return str(header_value)


class IMAPClient:
    """Thread-safe, read-only safe IMAP client."""

    def __init__(self, account: AccountConfig, timeout: int = 15):
        self.account = account
        self.timeout = timeout
        self.conn: Optional[imaplib.IMAP4_SSL | imaplib.IMAP4] = None

    def __enter__(self) -> IMAPClient:
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()

    def connect(self) -> None:
        """Establishes TLS IMAP connection and logs in."""
        if not self.account.email or not self.account.password:
            raise ValueError(f"Account {self.account.id} is missing username or password")

        if self.account.use_ssl:
            self.conn = imaplib.IMAP4_SSL(self.account.imap_host, self.account.imap_port)
        else:
            self.conn = imaplib.IMAP4(self.account.imap_host, self.account.imap_port)

        status, resp = self.conn.login(self.account.email, self.account.password)
        if status != "OK":
            raise RuntimeError(f"Login failed for {self.account.email}: {resp}")

    def disconnect(self) -> None:
        """Safely logs out and closes the IMAP socket."""
        if self.conn:
            try:
                self.conn.logout()
            except Exception:
                pass
            finally:
                self.conn = None

    def list_folders(self) -> List[str]:
        """Lists available mailbox folders."""
        if not self.conn:
            self.connect()
        assert self.conn is not None

        status, folder_lines = self.conn.list()
        if status != "OK" or not folder_lines:
            return []

        folders = []
        for line in folder_lines:
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")
            # Extract folder name after delimiter
            match = re.search(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]+)"\s+(?P<name>.+)', line)
            if match:
                folder_name = match.group("name").strip().strip('"')
                folders.append(folder_name)
            else:
                parts = line.split(' "/" ')
                if len(parts) > 1:
                    folders.append(parts[-1].strip().strip('"'))
                else:
                    folders.append(line)
        return folders

    def get_mailbox_status(self, folder: Optional[str] = None) -> Dict[str, Any]:
        """Returns message counts for the given folder."""
        if not self.conn:
            self.connect()
        assert self.conn is not None

        target_folder = folder or self.account.inbox_folder
        status, data = self.conn.status(f'"{target_folder}"', "(MESSAGES UNSEEN RECENT)")
        if status != "OK" or not data:
            return {"folder": target_folder, "total": 0, "unseen": 0, "recent": 0}

        resp_text = data[0].decode("utf-8", errors="replace") if isinstance(data[0], bytes) else str(data[0])
        total = int(m.group(1)) if (m := re.search(r"MESSAGES\s+(\d+)", resp_text)) else 0
        unseen = int(m.group(1)) if (m := re.search(r"UNSEEN\s+(\d+)", resp_text)) else 0
        recent = int(m.group(1)) if (m := re.search(r"RECENT\s+(\d+)", resp_text)) else 0

        return {"folder": target_folder, "total": total, "unseen": unseen, "recent": recent}

    def list_recent_headers(
        self, folder: Optional[str] = None, limit: Optional[int] = None, unread_only: bool = False
    ) -> List[EmailHeader]:
        """Fetches header summaries in read-only mode without touching message read flags."""
        if not self.conn:
            self.connect()
        assert self.conn is not None

        target_folder = folder or self.account.inbox_folder
        max_limit = limit or self.account.poll_limit

        status, _ = self.conn.select(f'"{target_folder}"', readonly=True)
        if status != "OK":
            raise ValueError(f"Cannot select folder {target_folder}")

        search_criteria = "UNSEEN" if unread_only else "ALL"
        status, data = self.conn.uid("SEARCH", None, search_criteria)
        if status != "OK" or not data or not data[0]:
            return []

        uids = [x for x in data[0].split() if x]
        if not uids:
            return []

        # Latest messages are at the end
        selected_uids = uids[-max_limit:]
        selected_uids.reverse()

        uid_set = b",".join(selected_uids).decode()
        status, fetch_data = self.conn.uid("FETCH", uid_set, "(UID FLAGS RFC822.HEADER)")
        if status != "OK" or not fetch_data:
            return []

        headers: List[EmailHeader] = []
        for i in range(0, len(fetch_data)):
            item = fetch_data[i]
            if not isinstance(item, tuple) or len(item) < 2:
                continue

            meta = item[0].decode("ascii", "replace") if isinstance(item[0], bytes) else str(item[0])
            raw_header = item[1]

            uid_match = re.search(r"\bUID\s+(\d+)\b", meta)
            uid = uid_match.group(1) if uid_match else "unknown"

            flags_match = re.search(r"FLAGS\s*\(([^)]*)\)", meta)
            flags = flags_match.group(1).split() if flags_match else []

            msg = BytesParser(policy=default).parsebytes(raw_header)
            subject = decode_mime_header(str(msg.get("Subject", "(no subject)")))
            sender = decode_mime_header(str(msg.get("From", "(unknown sender)")))
            date_str = str(msg.get("Date", ""))

            parsed_dt = None
            try:
                parsed_dt = parsedate_to_datetime(date_str)
            except Exception:
                pass

            list_id = str(msg.get("List-ID", ""))
            list_unsub = str(msg.get("List-Unsubscribe", ""))

            headers.append(
                EmailHeader(
                    account_id=self.account.id,
                    uid=uid,
                    sender=sender,
                    subject=subject,
                    date_str=date_str,
                    date=parsed_dt,
                    flags=flags,
                    list_id=list_id,
                    list_unsubscribe=list_unsub,
                )
            )

        return headers

    def fetch_message(self, uid: str, folder: Optional[str] = None) -> Optional[EmailMessage]:
        """Fetches full message body and metadata using BODY.PEEK to preserve read state."""
        if not self.conn:
            self.connect()
        assert self.conn is not None

        target_folder = folder or self.account.inbox_folder
        status, _ = self.conn.select(f'"{target_folder}"', readonly=True)
        if status != "OK":
            raise ValueError(f"Cannot select folder {target_folder}")

        fetch_query = f"(UID FLAGS RFC822.SIZE BODY.PEEK[]<0.{MAX_FETCH_BYTES}>)"
        status, fetch_data = self.conn.uid("FETCH", str(uid), fetch_query)
        if status != "OK" or not fetch_data:
            return None

        for item in fetch_data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue

            meta = item[0].decode("ascii", "replace") if isinstance(item[0], bytes) else str(item[0])
            raw_bytes = item[1]

            flags_match = re.search(r"FLAGS\s*\(([^)]*)\)", meta)
            flags = flags_match.group(1).split() if flags_match else []

            size_match = re.search(r"RFC822.SIZE\s+(\d+)", meta)
            total_size = int(size_match.group(1)) if size_match else len(raw_bytes)
            is_truncated = total_size > len(raw_bytes)

            msg = BytesParser(policy=default).parsebytes(raw_bytes)
            subject = decode_mime_header(str(msg.get("Subject", "(no subject)")))
            sender = decode_mime_header(str(msg.get("From", "(unknown sender)")))
            date_str = str(msg.get("Date", ""))

            recipients = []
            for h in ("To", "Cc"):
                if val := msg.get(h):
                    recipients.append(decode_mime_header(str(val)))

            # Extract body
            plain_bodies = []
            html_bodies = []
            attachments = []

            for part in msg.walk():
                if part.is_multipart():
                    continue
                content_type = part.get_content_type()
                filename = part.get_filename()
                if filename or part.get_content_disposition() == "attachment":
                    attachments.append({"filename": filename or "unknown", "content_type": content_type})
                    continue

                try:
                    payload = part.get_content()
                    if isinstance(payload, str):
                        if content_type == "text/plain":
                            plain_bodies.append(payload)
                        elif content_type == "text/html":
                            html_bodies.append(payload)
                except Exception:
                    pass

            if plain_bodies:
                body_text = "\n\n".join(plain_bodies)
            elif html_bodies:
                body_text = clean_html_to_text("\n\n".join(html_bodies))
            else:
                body_text = "(no readable text content)"

            return EmailMessage(
                account_id=self.account.id,
                uid=str(uid),
                sender=sender,
                recipients=recipients,
                subject=subject,
                date_str=date_str,
                body_text=body_text[:MAX_FETCH_BYTES],
                body_html=html_bodies[0] if html_bodies else "",
                body_truncated=is_truncated,
                attachments=attachments,
                flags=flags,
                list_id=str(msg.get("List-ID", "")),
                list_unsubscribe=str(msg.get("List-Unsubscribe", "")),
            )

        return None
