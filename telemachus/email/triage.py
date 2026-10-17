"""Triage and classification engine powered by Laya + Deterministic Rules."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Callable, List, Optional

from telemachus.email.models import EmailMessage, TriageClassification

# High-precision deterministic patterns that safeguard critical records
IMPORTANT_DOC_PATTERN = re.compile(
    r"\b(w[- ]?2|1099|tax return|irs\b|bank statement|credit card statement|wire transfer|"
    r"loan agreement|mortgage|vehicle registration|insurance policy|legal notice|court|"
    r"contract|receipt|invoice|billing statement|annual statement)\b",
    re.I,
)

URGENT_PATTERN = re.compile(
    r"\b(action required|security alert|account suspended|immediate attention|deadline|"
    r"payment overdue|unauthorized login|fraud alert|due today|due tomorrow)\b",
    re.I,
)


def run_laya_worker_batch(
    messages: List[EmailMessage], python_bin: Optional[str] = None
) -> List[dict]:
    """Runs a batch of messages through the Laya worker subprocess."""
    if not messages:
        return []

    from telemachus.config.loader import load_env
    load_env()
    python_path = python_bin or os.environ.get(
        "LAYA_PYTHON", str(Path.home() / "laya/.venv/bin/python")
    )
    if not Path(python_path).is_file():
        return []

    worker_script = Path(__file__).parent / "laya_worker.py"
    if not worker_script.is_file():
        return []

    records = [
        {
            "account_id": m.account_id,
            "uid": m.uid,
            "from": m.sender,
            "subject": m.subject,
            "body": m.body_text[:1500],
            "attachments": [a["filename"] for a in m.attachments],
        }
        for m in messages
    ]

    try:
        proc = subprocess.Popen(
            [python_path, str(worker_script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait for worker ready on stderr
        while True:
            err_line = proc.stderr.readline()
            if not err_line or "LAYA_READY" in err_line:
                break

        payload = json.dumps({"records": records}) + "\n"
        stdout, _ = proc.communicate(input=payload, timeout=60)
        out_json = json.loads(stdout)
        return out_json.get("results", [])
    except Exception:
        return []


def triage_messages(
    messages: List[EmailMessage],
    python_bin: Optional[str] = None,
    predictor: Optional[Callable[[List[dict]], List[dict]]] = None,
) -> List[TriageClassification]:
    """Triages a list of EmailMessage objects using Laya + deterministic guardrails."""
    if not messages:
        return []

    if predictor:
        records = [
            {
                "account_id": m.account_id,
                "uid": m.uid,
                "from": m.sender,
                "subject": m.subject,
                "body": m.body_text[:1500],
                "attachments": [a["filename"] for a in m.attachments],
            }
            for m in messages
        ]
        laya_results = predictor(records)
    else:
        laya_results = run_laya_worker_batch(messages, python_bin=python_bin)

    laya_by_uid = {r["uid"]: r for r in laya_results if "uid" in r}

    triaged: List[TriageClassification] = []

    for msg in messages:
        combined_text = f"{msg.subject} {msg.body_text[:2000]}"
        laya_data = laya_by_uid.get(msg.uid, {})

        # Default fallback categories if Laya worker is unavailable
        category = laya_data.get("category", "unclear")
        attention = laya_data.get("attention", "routine")
        preservation = laya_data.get("preservation", "review_first")
        confidence = float(laya_data.get("category_confidence", 0.70))
        latency_ms = float(laya_data.get("latency_ms", 0.0))
        reasons = []

        # Tier 0 Rule: Deterministic High-Confidence Document Safeguard
        if IMPORTANT_DOC_PATTERN.search(combined_text):
            category = "important_documents"
            preservation = "preserve"
            confidence = max(confidence, 0.95)
            reasons.append("Matched financial/legal/receipt record keyword pattern")

        # Tier 0 Rule: Deterministic Urgency Detection
        if URGENT_PATTERN.search(combined_text):
            attention = "urgent_review"
            reasons.append("Matched security/deadline urgency pattern")

        # Fallback heuristics when Laya model is not installed or offline
        if not laya_data and category == "unclear":
            if msg.list_id or msg.list_unsubscribe:
                category = "newsletter_marketing"
                preservation = "cleanup_candidate"
                confidence = 0.85
                reasons.append("List-ID or unsubscribe header detected")
            else:
                category = "personal_work"
                preservation = "preserve"
                confidence = 0.65

        triaged.append(
            TriageClassification(
                account_id=msg.account_id,
                uid=msg.uid,
                subject=msg.subject,
                sender=msg.sender,
                category=category,
                attention=attention,
                preservation=preservation,
                confidence=confidence,
                reasons=reasons,
                latency_ms=latency_ms,
            )
        )

    return triaged
