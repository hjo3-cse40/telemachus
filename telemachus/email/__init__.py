"""Telemachus Email Subsystem."""
from .client import IMAPClient
from .draft import create_draft
from .models import AccountConfig, EmailHeader, EmailMessage, TriageClassification
from .triage import triage_messages

__all__ = [
    "IMAPClient",
    "create_draft",
    "AccountConfig",
    "EmailHeader",
    "EmailMessage",
    "TriageClassification",
    "triage_messages",
]
