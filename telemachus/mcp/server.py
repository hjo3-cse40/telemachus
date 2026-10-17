"""Stdio MCP (Model Context Protocol) Server for Telemachus Email Subsystem.

Exposes private email operations (status, listing, inspection, Laya triage, safe drafting)
directly to OpenCode Interpreter or any MCP client over stdio JSON-RPC.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from telemachus.config.loader import get_account, get_enabled_accounts, load_accounts, load_env
from telemachus.email.client import IMAPClient
from telemachus.email.draft import create_draft
from telemachus.email.triage import triage_messages

PROTOCOL_VERSION = "2024-11-05"


class WarmLayaBridge:
    """Maintains a persistent, warm Laya worker subprocess to eliminate cold-start latency."""

    def __init__(self, python_path: Optional[str] = None):
        self.python_path = python_path or os.environ.get(
            "LAYA_PYTHON", str(Path.home() / "laya/.venv/bin/python")
        )
        self.worker_script = Path(__file__).resolve().parent.parent / "email" / "laya_worker.py"
        self.process: Optional[subprocess.Popen] = None
        self.lock = threading.RLock()

    def _ensure_running(self) -> bool:
        with self.lock:
            if self.process and self.process.poll() is None:
                return True
            if not Path(self.python_path).is_file() or not self.worker_script.is_file():
                return False

        try:
            sys.stderr.write("[Telemachus MCP] Warming persistent Laya MPS model...\n")
            sys.stderr.flush()
            self.process = subprocess.Popen(
                [self.python_path, str(self.worker_script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            while True:
                line = self.process.stderr.readline()
                if not line or "LAYA_READY" in line:
                    break

            def _drain():
                while self.process and self.process.poll() is None:
                    try:
                        line = self.process.stderr.readline()
                        if not line:
                            break
                    except Exception:
                        break

            threading.Thread(target=_drain, daemon=True).start()
            sys.stderr.write("[Telemachus MCP] Laya MPS model is warm and resident in memory.\n")
            sys.stderr.flush()
            return True
        except Exception as e:
            sys.stderr.write(f"[Telemachus MCP] Failed to start Laya worker: {e}\n")
            sys.stderr.flush()
            self.process = None
            return False

    def predict(self, records: List[dict]) -> List[dict]:
        with self.lock:
            if not self._ensure_running() or not self.process:
                return []
            try:
                payload = json.dumps({"records": records}) + "\n"
                assert self.process.stdin is not None
                assert self.process.stdout is not None
                self.process.stdin.write(payload)
                self.process.stdin.flush()
                out_line = self.process.stdout.readline()
                if not out_line:
                    return []
                resp = json.loads(out_line)
                return resp.get("results", [])
            except Exception as e:
                sys.stderr.write(f"[Telemachus MCP] Laya inference error: {e}\n")
                sys.stderr.flush()
                if self.process:
                    try:
                        self.process.kill()
                    except Exception:
                        pass
                    self.process = None
                return []

    def close(self):
        with self.lock:
            if self.process:
                try:
                    self.process.terminate()
                except Exception:
                    pass
                self.process = None


# Global warm bridge
laya_bridge = WarmLayaBridge()

TOOLS_DEFINITIONS = [
    {
        "name": "email_status",
        "description": "Get current connection status and unread/total message counts across all configured email accounts.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "email_list",
        "description": "List recent email headers (UID, sender, subject, date, unread flags) from an account without marking them as read.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": "Account ID to check (e.g. 'yahoo', 'gmail_1'). Defaults to 'yahoo'.",
                    "default": "yahoo",
                },
                "folder": {
                    "type": "string",
                    "description": "Mailbox folder to inspect (default: INBOX).",
                    "default": "INBOX",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of recent emails to list (1-25). Default: 10.",
                    "default": 10,
                },
                "unread_only": {
                    "type": "boolean",
                    "description": "If true, only returns unread (unseen) messages.",
                    "default": False,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "email_show",
        "description": "Read the full cleaned text body, sender, recipients, and attachments of a specific email by UID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "The unique message UID to retrieve.",
                },
                "account": {
                    "type": "string",
                    "description": "Account ID. Defaults to 'yahoo'.",
                    "default": "yahoo",
                },
                "folder": {
                    "type": "string",
                    "description": "Folder containing the email. Defaults to 'INBOX'.",
                    "default": "INBOX",
                },
            },
            "required": ["uid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "email_triage",
        "description": "Triage recent emails using the fast local Laya decision model (MPS ModernBERT). Classifies emails into categories (important_documents, personal_work, transactional, newsletter_marketing, suspicious_spam) and assigns priority/attention recommendations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": "Account ID to triage. Defaults to 'yahoo'.",
                    "default": "yahoo",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of recent emails to triage (default: 10).",
                    "default": 10,
                },
                "unread_only": {
                    "type": "boolean",
                    "description": "If true, only triages unread messages.",
                    "default": False,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "email_draft",
        "description": "Safely creates and saves an email draft to the account's remote 'Drafts' folder for human review. Does NOT send any email.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address.",
                },
                "subject": {
                    "type": "string",
                    "description": "Subject of the email draft.",
                },
                "body": {
                    "type": "string",
                    "description": "Body content of the draft.",
                },
                "account": {
                    "type": "string",
                    "description": "Account ID to create draft in. Defaults to 'yahoo'.",
                    "default": "yahoo",
                },
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
        },
    },
]


def handle_tool_call(name: str, arguments: Dict[str, Any]) -> str:
    """Executes a tool and returns the formatted text output."""
    if name == "email_status":
        accounts = load_accounts()
        output = ["Telemachus Mailbox Status:"]
        for acc in accounts:
            if not acc.enabled:
                output.append(f"- [{acc.id}] {acc.name}: DISABLED")
                continue
            if not acc.email or not acc.password:
                output.append(f"- [{acc.id}] {acc.name}: Missing credentials in .env")
                continue
            try:
                with IMAPClient(acc, timeout=10) as client:
                    st = client.get_mailbox_status(acc.inbox_folder)
                    output.append(
                        f"- [{acc.id}] {acc.name} ({acc.email}): Connected OK | Total: {st['total']} | Unseen: {st['unseen']}"
                    )
            except Exception as e:
                output.append(f"- [{acc.id}] {acc.name}: Connection Error: {e}")
        return "\n".join(output)

    elif name == "email_list":
        acc_id = arguments.get("account", "yahoo")
        folder = arguments.get("folder", "INBOX")
        limit = min(25, max(1, int(arguments.get("limit", 10))))
        unread_only = bool(arguments.get("unread_only", False))

        acc = get_account(acc_id)
        if not acc or not acc.enabled or not acc.password:
            return f"Error: Account '{acc_id}' is not available or not configured."

        with IMAPClient(acc) as client:
            headers = client.list_recent_headers(folder=folder, limit=limit, unread_only=unread_only)
            if not headers:
                return f"No messages found in folder '{folder}'."

            lines = [f"Recent emails for [{acc.id}] (Folder: {folder}, Limit: {len(headers)}):"]
            for h in headers:
                flags_str = f" [Flags: {', '.join(h.flags)}]" if h.flags else ""
                date_str = h.date.strftime("%Y-%m-%d %H:%M") if h.date else h.date_str[:16]
                lines.append(f"- UID {h.uid}: From '{h.sender}' on {date_str} | Subject: \"{h.subject}\"{flags_str}")
            return "\n".join(lines)

    elif name == "email_show":
        uid = str(arguments["uid"])
        acc_id = arguments.get("account", "yahoo")
        folder = arguments.get("folder", "INBOX")

        acc = get_account(acc_id)
        if not acc:
            return f"Error: Account '{acc_id}' not found."

        with IMAPClient(acc) as client:
            msg = client.fetch_message(uid, folder=folder)
            if not msg:
                return f"Message UID {uid} not found in folder '{folder}'."

            attach_str = f"\nAttachments: {[a['filename'] for a in msg.attachments]}" if msg.attachments else ""
            return (
                f"UID: {msg.uid} (Account: {msg.account_id})\n"
                f"From: {msg.sender}\n"
                f"To: {', '.join(msg.recipients)}\n"
                f"Date: {msg.date_str}\n"
                f"Subject: {msg.subject}{attach_str}\n\n"
                f"--- Message Body ---\n{msg.body_text}"
            )

    elif name == "email_triage":
        acc_id = arguments.get("account", "yahoo")
        limit = min(25, max(1, int(arguments.get("limit", 10))))
        unread_only = bool(arguments.get("unread_only", False))

        acc = get_account(acc_id)
        if not acc:
            return f"Error: Account '{acc_id}' not found."

        with IMAPClient(acc) as client:
            headers = client.list_recent_headers(folder=acc.inbox_folder, limit=limit, unread_only=unread_only)
            messages = []
            for h in headers:
                m = client.fetch_message(h.uid, folder=acc.inbox_folder)
                if m:
                    messages.append(m)

        if not messages:
            return "No emails available to triage."

        results = triage_messages(messages, predictor=laya_bridge.predict)
        lines = [f"Laya Triage Results for [{acc.id}] ({len(results)} emails evaluated):"]
        for r in results:
            action = "KEEP" if r.preservation == "preserve" else ("CLEANUP" if r.preservation == "cleanup_candidate" else "REVIEW")
            reasons_str = f" ({', '.join(r.reasons)})" if r.reasons else ""
            lines.append(
                f"- UID {r.uid}: [{r.category.upper()}] (Conf: {r.confidence:.2f}) | Priority: {r.attention.upper()} | Rec: {action}{reasons_str}\n"
                f"  From: {r.sender[:40]} | Subject: \"{r.subject}\""
            )
        return "\n".join(lines)

    elif name == "email_draft":
        acc_id = arguments.get("account", "yahoo")
        to = arguments["to"]
        subject = arguments["subject"]
        body = arguments["body"]

        acc = get_account(acc_id)
        if not acc:
            return f"Error: Account '{acc_id}' not found."

        res = create_draft(account=acc, to=to, subject=subject, body=body)
        return (
            f"Draft created successfully!\n"
            f"Account: {res['account_id']}\n"
            f"Saved to: {res['folder']}\n"
            f"To: {res['to']}\n"
            f"Subject: {res['subject']}\n"
            f"Status: Saved to remote drafts folder. Review and send from your mail client."
        )

    else:
        raise ValueError(f"Unknown tool: {name}")


def send_response(req_id: Any, result: Any) -> None:
    msg = {"jsonrpc": "2.0", "id": req_id, "result": result}
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def send_error(req_id: Any, code: int, message: str) -> None:
    msg = {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def run_server() -> None:
    """Runs the stdio JSON-RPC loop."""
    load_env()
    # Pre-warm Laya in background thread to avoid blocking server init
    threading.Thread(target=laya_bridge._ensure_running, daemon=True).start()

    sys.stderr.write("[Telemachus MCP] Server listening on stdio...\n")
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            send_error(None, -32700, "Parse error")
            continue

        req_id = req.get("id")
        method = req.get("method")

        # Handle notifications (no id)
        if req_id is None:
            if method == "notifications/initialized":
                sys.stderr.write("[Telemachus MCP] Client initialized.\n")
                sys.stderr.flush()
            continue

        if method == "initialize":
            send_response(
                req_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "telemachus-email", "version": "0.1.0"},
                },
            )
        elif method == "ping":
            send_response(req_id, {})
        elif method == "tools/list":
            send_response(req_id, {"tools": TOOLS_DEFINITIONS})
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            try:
                res_text = handle_tool_call(name, args)
                send_response(
                    req_id,
                    {"content": [{"type": "text", "text": res_text}], "isError": False},
                )
            except Exception as e:
                send_response(
                    req_id,
                    {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True},
                )
        else:
            send_error(req_id, -32601, f"Method not found: {method}")


if __name__ == "__main__":
    run_server()
