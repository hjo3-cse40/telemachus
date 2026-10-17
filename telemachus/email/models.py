"""Data models for Telemachus email operations."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class AccountConfig:
    """Configuration for an email account."""
    id: str
    name: str
    provider: str
    email: str
    password: str
    imap_host: str
    imap_port: int = 993
    use_ssl: bool = True
    inbox_folder: str = "INBOX"
    drafts_folder: str = "Drafts"
    poll_limit: int = 10
    enabled: bool = True


@dataclass
class EmailHeader:
    """Lightweight email header summary."""
    account_id: str
    uid: str
    sender: str
    subject: str
    date_str: str
    date: Optional[datetime] = None
    flags: List[str] = field(default_factory=list)
    has_attachments: bool = False
    list_id: str = ""
    list_unsubscribe: str = ""


@dataclass
class EmailMessage:
    """Full parsed email message structure."""
    account_id: str
    uid: str
    sender: str
    recipients: List[str]
    subject: str
    date_str: str
    body_text: str
    body_html: str = ""
    body_truncated: bool = False
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    list_id: str = ""
    list_unsubscribe: str = ""

    def preview_snippet(self, max_chars: int = 140) -> str:
        """Returns a single-line clean preview snippet."""
        clean = " ".join(self.body_text.split())
        if len(clean) > max_chars:
            return clean[:max_chars].rstrip() + "..."
        return clean or "(no body content)"


@dataclass
class TriageClassification:
    """Structured decision output from Laya classification pass."""
    account_id: str
    uid: str
    subject: str
    sender: str
    category: str           # important_documents, personal_work, transactional, newsletter_marketing, suspicious_spam, unclear
    attention: str          # urgent_review, important_review, routine, unclear
    preservation: str       # preserve, cleanup_candidate, review_first
    confidence: float
    reasons: List[str] = field(default_factory=list)
    latency_ms: float = 0.0
