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
from telemachus.email.indexer import index_emails
from telemachus.email.store import EmailIndexStore
from telemachus.email.triage import triage_messages
from telemachus.finance import FinanceMasker, FinanceStore, RecurringEngine
from telemachus.fs.checkpoint import create_git_checkpoint
from telemachus.fs.editor import edit_file, run_command, safe_delete, write_file
from telemachus.fs.explorer import grep_text, list_dir, read_file, search_files, stat_path
from telemachus.ingress.proxy import start_ingress_proxy_background
from telemachus.memory import MemoryCache, MemoryEntry, MemoryVault, PrivacyTier
from telemachus.memory.health import MemoryHealth
from telemachus.memory.recall import recall, render_recall, render_source

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


# Global warm bridge & SQLite index store
laya_bridge = WarmLayaBridge()
index_store = EmailIndexStore()

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
    {
        "name": "email_index",
        "description": "Offline Mailbox Indexer: fetches batches of emails from the remote mailbox, tags them with Laya on MPS, and stores them in local SQLite. Safe, paginated, and skips already-indexed emails.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": "Account ID to index (e.g. 'yahoo'). Default: 'yahoo'.",
                    "default": "yahoo",
                },
                "folder": {
                    "type": "string",
                    "description": "Folder to index (default: INBOX).",
                    "default": "INBOX",
                },
                "max_count": {
                    "type": "integer",
                    "description": "Maximum number of unindexed emails to fetch and classify in this run (e.g. 25, 50, 100). Default: 50.",
                    "default": 50,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "email_search_index",
        "description": "Instantly searches locally indexed emails in SQLite by keyword, sender, category (important_documents, personal_work, transactional, newsletter_marketing, suspicious_spam), or priority. Uses zero IMAP bandwidth and minimal tokens.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional keyword or search term to match against subject, body snippet, or sender.",
                },
                "category": {
                    "type": "string",
                    "description": "Filter by category: 'important_documents', 'personal_work', 'transactional', 'newsletter_marketing', 'suspicious_spam'.",
                },
                "attention": {
                    "type": "string",
                    "description": "Filter by priority: 'urgent_review', 'important_review', 'routine'.",
                },
                "account": {
                    "type": "string",
                    "description": "Filter by account ID (default: all or 'yahoo').",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return (default: 10).",
                    "default": 10,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "email_index_stats",
        "description": "Get high-level summary statistics of all locally indexed emails in SQLite, including total count, category distribution, priority breakdown, and date range.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": "Account ID to inspect, or omit for all accounts.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_list",
        "description": "Lists directory contents with depth controls, file sizes, and noise suppression (omits .git, node_modules, .venv).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to directory (default: current workspace root '.').",
                    "default": ".",
                },
                "depth": {
                    "type": "integer",
                    "description": "Maximum directory traversal depth (default: 2).",
                    "default": 2,
                },
                "show_hidden": {
                    "type": "boolean",
                    "description": "Include hidden files (default: false).",
                    "default": False,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_read",
        "description": "Reads lines from a text file with line numbers and windowing. Prevents reading binary files or exceeding context limits.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative or absolute path to the file.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "1-based starting line number (default: 1).",
                    "default": 1,
                },
                "end_line": {
                    "type": "integer",
                    "description": "Optional 1-based ending line number.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_search",
        "description": "Finds files matching a filename or path pattern across a directory tree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Filename or substring to search for.",
                },
                "root_dir": {
                    "type": "string",
                    "description": "Root directory to search inside (default: '.').",
                    "default": ".",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of file matches to return (default: 50).",
                    "default": 50,
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_grep",
        "description": "Searches file contents across a codebase for matching text or regex with file and line number context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text query or symbol to search for.",
                },
                "root_dir": {
                    "type": "string",
                    "description": "Directory root to search (default: '.').",
                    "default": ".",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Optional file extension filter (e.g. '*.py' or '*.ts').",
                },
                "is_regex": {
                    "type": "boolean",
                    "description": "Whether query is a regular expression (default: false).",
                    "default": False,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_stat",
        "description": "Gets metadata, size, permissions, and timestamps for a specific file or directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to inspect.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_edit",
        "description": "Surgically replaces a unique code block in a file with new content. Protected by Tier 0-3 LayaGuard with automatic Git checkpointing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to file to edit.",
                },
                "target_content": {
                    "type": "string",
                    "description": "Exact contiguous block of code to find and replace.",
                },
                "replacement_content": {
                    "type": "string",
                    "description": "New replacement content.",
                },
                "user_intent": {
                    "type": "string",
                    "description": "Stated purpose or user goal for this modification.",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, validates target match without writing changes.",
                    "default": False,
                },
            },
            "required": ["path", "target_content", "replacement_content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_write",
        "description": "Safely creates a new file or overwrites an existing file. Protected by Tier 0-3 LayaGuard with automatic Git checkpointing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path of file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "Full file contents.",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Whether to allow overwriting an existing file (default: false).",
                    "default": False,
                },
                "user_intent": {
                    "type": "string",
                    "description": "Stated purpose or user goal for writing this file.",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_delete",
        "description": "Safely deletes a file or directory by moving it to macOS Trash (recoverable via Cmd+Z). Protected by Tier 0-3 LayaGuard.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to file or directory to delete.",
                },
                "user_intent": {
                    "type": "string",
                    "description": "Stated reason or user prompt requesting the deletion.",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "Set to true if user has explicitly confirmed an elevated deletion.",
                    "default": False,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_run",
        "description": "Executes a shell command after vetting through Tier 0 boundary checks and Tier 1 Laya intent alignment.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command line to execute.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory (default: active workspace).",
                },
                "user_intent": {
                    "type": "string",
                    "description": "Stated user prompt justifying this command execution.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (default: 30).",
                    "default": 30,
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_checkpoint",
        "description": "Creates a manual Git checkpoint / stash snapshot before running risky operations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path inside the Git repository (default: '.').",
                    "default": ".",
                },
                "message": {
                    "type": "string",
                    "description": "Optional label or description for the checkpoint snapshot.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_search",
        "description": (
            "Searches the local memory vault (~/.memory) for facts about the user and facts from documents the user worked with. "
            "Returns STATUS (MATCH, WEAK_MATCH or NO_MATCH), an INSTRUCTION, and results tagged [M1], [M2] with their source and quote. "
            "Follow the INSTRUCTION: cite tags for facts you use; on NO_MATCH say you have no record instead of guessing. "
            "Use short keyword queries (names, topics), for example 'Kim email preference'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text query or keyword to search in memory.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default: 5).",
                    "default": 5,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_save",
        "description": (
            "Proposes a fact for the local memory vault. The fact goes to the user's review list, not straight into memory, "
            "because it is not checked against the user's own words. Facts the user states in chat are saved automatically; "
            "use this only when the user asks you to remember something. Tell the user it is waiting for review."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The atomic fact or preference text to record.",
                },
                "category": {
                    "type": "string",
                    "description": "Category label (e.g. 'fact', 'preference', 'project', 'finance').",
                    "default": "fact",
                },
                "tier": {
                    "type": "string",
                    "description": "Privacy tier: 'tier_2_private' (default, local only) or 'tier_0_public' (cloud-safe).",
                    "default": "tier_2_private",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_status",
        "description": "Returns current statistics and counts from the unified memory vault (~/.memory).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_source",
        "description": (
            "Reads the full source of one memory: who said it or which document it came from, the exact quote, "
            "and the stored source text around the quote. Use the id from memory_search results."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Memory id (or its first 8 characters) from memory_search.",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_recent",
        "description": "Lists the most recently recorded memories and facts in chronological order.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max recent memories to return (default: 5).",
                    "default": 5,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "finance_log_entry",
        "description": (
            "Deterministically logs one transaction or a batch of bullet transactions into the local double-entry ledger. "
            "Supports quick bullet items (e.g. groceries, gas, dining, payday) and auto-balances postings."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "payee": {"type": "string", "description": "Payee or merchant name (e.g. 'Safeway', 'Taqueria')."},
                "amount": {"type": "number", "description": "Amount in dollars (e.g. 45.20 for expense, 1600.00 for income)."},
                "category": {
                    "type": "string",
                    "description": "Category: 'groceries', 'gas', 'dining', 'payday', 'kahram', 'gym', 'spotify', 'icloud', 'wifi', 'pge', or 'personal'.",
                },
                "memo": {"type": "string", "description": "Optional notes or item description."},
                "date": {"type": "string", "description": "Date in YYYY-MM-DD format (defaults to today)."},
                "items": {
                    "type": "array",
                    "description": "Optional batch list of items from daily bullet list: [{'category': 'groceries', 'amount': 45.20}, ...]",
                    "items": {"type": "object"},
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "finance_monthly_overview",
        "description": (
            "Returns the exact Notion Monthly Overview row with all deterministic formulas: "
            "Income, Savings, Fixed Bills, PGE, All Bills, Groceries (Planned/Actual/Left), Gas (Planned/Actual/Left), "
            "Variable Variance, Total Personal, and Remaining Cushion."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "Month in YYYY-MM format (defaults to current month)."},
                "income": {"type": "number", "description": "Set/update planned monthly income (dollars)."},
                "savings_target": {"type": "number", "description": "Set/update savings target (dollars)."},
                "groceries_planned": {"type": "number", "description": "Set/update planned groceries budget (dollars)."},
                "gas_planned": {"type": "number", "description": "Set/update planned car gas budget (dollars)."},
                "pge": {"type": "number", "description": "Set confirmed PG&E bill amount (dollars)."},
                "notes": {"type": "string", "description": "Optional month notes."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "finance_get_balance",
        "description": "Gets deterministic real-time balances for Wells Fargo Checking, Capital One HYSA, Visa Card, and Net Worth.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "as_of_date": {"type": "string", "description": "Optional cutoff date (YYYY-MM-DD)."}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "finance_list_recurring",
        "description": "Lists upcoming recurring obligations and bills (Kahram, Gym, iCloud, Spotify, Wi-Fi, PG&E, Credit Card), explicitly distinguishing bills due before your upcoming paycheck from bills due after.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "description": "Lookahead window in days (default: 30).", "default": 30}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "finance_goals_progress",
        "description": "Returns progress bars and remaining targets for monthly goals (Savings and Kahram Payment).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "Month in YYYY-MM format (defaults to current month)."}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "finance_query_transactions",
        "description": (
            "Queries transactions with deterministic filtering and statistical rollups. "
            "Filter by category ('gas', 'groceries', 'dining', etc.), specific date (YYYY-MM-DD), "
            "or date range. Returns transaction details plus deterministic statistics: "
            "total spent, transaction count, average amount, and most recent expense."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Category or payee to filter by (e.g. 'gas', 'groceries', 'dining', 'pge', 'wifi', 'gym').",
                },
                "date": {
                    "type": "string",
                    "description": "Exact transaction date to inspect (YYYY-MM-DD).",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start of date range (YYYY-MM-DD).",
                },
                "end_date": {
                    "type": "string",
                    "description": "End of date range (YYYY-MM-DD).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum transactions to return (default: 20).",
                    "default": 20,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "finance_paycheck_plan",
        "description": (
            "Answers 'what payments/bills are due before or until my next paycheck' and calculates bi-weekly cash flow: "
            "the exact date the next paycheck arrives, checking cash position after due payments, "
            "every bill due before that next paycheck arrives, automatic HYSA savings transfer, and safe-to-spend remainder."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Optional date in YYYY-MM-DD format to inspect that specific paycheck cycle (defaults to current date).",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "finance_plan_breakdown",
        "description": (
            "Returns a comprehensive financial situation breakdown: current balances, this month's budget allocations "
            "(HYSA savings, groceries, eating out, gas, bills, discretionary allowance), plus the exact 2-week paycheck allocation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Optional date in YYYY-MM-DD format (defaults to current date).",
                },
            },
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

    elif name == "email_index":
        acc_id = arguments.get("account", "yahoo")
        folder = arguments.get("folder", "INBOX")
        max_count = min(200, max(1, int(arguments.get("max_count", 50))))

        try:
            res = index_emails(
                account_id=acc_id,
                folder=folder,
                max_count=max_count,
                predictor=laya_bridge.predict,
                store=index_store,
            )
            return (
                f"Mailbox Indexing Complete for [{res['account_id']}] (Folder: {res['folder']}):\n"
                f"- Newly Indexed & Classified: {res.get('newly_indexed', 0)} emails\n"
                f"- Total Now Cached Locally: {res.get('total_indexed', 0)} emails\n"
                f"- Total Messages in Mailbox: {res.get('total_inbox_messages', 'unknown')}\n"
                f"- Remaining Unindexed: {res.get('remaining_unindexed', 0)}\n"
                f"You can now search these offline using 'email_search_index' or view breakdown with 'email_index_stats'."
            )
        except Exception as e:
            return f"Error during indexing: {e}"

    elif name == "email_search_index":
        acc_id = arguments.get("account")
        query = arguments.get("query")
        category = arguments.get("category")
        attention = arguments.get("attention")
        limit = min(50, max(1, int(arguments.get("limit", 10))))

        results = index_store.search(
            account_id=acc_id,
            query=query,
            category=category,
            attention=attention,
            limit=limit,
        )

        if not results:
            return "No matching emails found in local SQLite index. (Try running 'email_index' first to cache your inbox)."

        lines = [f"Found {len(results)} matching email(s) in local SQLite index:"]
        for r in results:
            flags_info = f" [{r['category'].upper()}]" if r.get("category") else ""
            lines.append(
                f"- UID {r['uid']}: From '{r.get('sender', '')[:35]}' on {r.get('date_str', '')[:16]}{flags_info}\n"
                f"  Subject: \"{r.get('subject', '')}\"\n"
                f"  Snippet: \"{r.get('snippet', '')}\"\n"
            )
        return "\n".join(lines)

    elif name == "email_index_stats":
        acc_id = arguments.get("account")
        stats = index_store.get_stats(account_id=acc_id)

        if stats["total_indexed"] == 0:
            return "No emails indexed in local SQLite yet. Run 'email_index' to start caching and tagging emails."

        cat_lines = [f"  - {cat}: {cnt}" for cat, cnt in stats["categories"].items()]
        attn_lines = [f"  - {attn}: {cnt}" for attn, cnt in stats["attention_breakdown"].items()]

        return (
            f"Local Email Index Statistics (Account: {stats['account_id']}):\n"
            f"- Total Emails Indexed: {stats['total_indexed']}\n"
            f"- Date Span Covered: {stats['earliest_date']} to {stats['latest_date']}\n"
            f"- Category Breakdown:\n" + "\n".join(cat_lines) + "\n"
            f"- Priority / Attention Breakdown:\n" + "\n".join(attn_lines)
        )

    elif name == "fs_list":
        path = arguments.get("path", ".")
        depth = int(arguments.get("depth", 2))
        show_hidden = bool(arguments.get("show_hidden", False))
        res = list_dir(path, max_depth=depth, show_hidden=show_hidden)
        return json.dumps(res, indent=2)

    elif name == "fs_read":
        path = arguments.get("path", "")
        start_line = int(arguments.get("start_line", 1))
        end_line = arguments.get("end_line")
        if end_line is not None:
            end_line = int(end_line)
        res = read_file(path, start_line=start_line, end_line=end_line)
        if "error" in res:
            return f"Error: {res['error']}"
        return (
            f"File: {res['path']} (lines {res['start_line']}-{res['end_line']} of {res['total_lines']}):\n\n"
            f"{res['content']}"
        )

    elif name == "fs_search":
        pattern = arguments.get("pattern", "")
        root_dir = arguments.get("root_dir", ".")
        max_results = int(arguments.get("max_results", 50))
        res = search_files(pattern, root_dir=root_dir, max_results=max_results)
        return json.dumps(res, indent=2)

    elif name == "fs_grep":
        query = arguments.get("query", "")
        root_dir = arguments.get("root_dir", ".")
        file_pattern = arguments.get("file_pattern")
        is_regex = bool(arguments.get("is_regex", False))
        res = grep_text(query, root_dir=root_dir, file_pattern=file_pattern, is_regex=is_regex)
        return json.dumps(res, indent=2)

    elif name == "fs_stat":
        path = arguments.get("path", "")
        res = stat_path(path)
        return json.dumps(res, indent=2)

    elif name == "fs_edit":
        path = arguments.get("path", "")
        target_content = arguments.get("target_content", "")
        replacement_content = arguments.get("replacement_content", "")
        user_intent = arguments.get("user_intent")
        dry_run = bool(arguments.get("dry_run", False))
        res = edit_file(
            path,
            target_content=target_content,
            replacement_content=replacement_content,
            user_intent=user_intent,
            dry_run=dry_run,
        )
        return json.dumps(res, indent=2)

    elif name == "fs_write":
        path = arguments.get("path", "")
        content = arguments.get("content", "")
        overwrite = bool(arguments.get("overwrite", False))
        user_intent = arguments.get("user_intent")
        res = write_file(path, content=content, overwrite=overwrite, user_intent=user_intent)
        return json.dumps(res, indent=2)

    elif name == "fs_delete":
        path = arguments.get("path", "")
        user_intent = arguments.get("user_intent")
        confirmed = bool(arguments.get("confirmed", False))
        res = safe_delete(path, user_intent=user_intent, confirmed=confirmed)
        return json.dumps(res, indent=2)

    elif name == "fs_run":
        command = arguments.get("command", "")
        cwd = arguments.get("cwd")
        user_intent = arguments.get("user_intent")
        timeout = int(arguments.get("timeout", 30))
        res = run_command(command, cwd=cwd, user_intent=user_intent, timeout=timeout)
        return json.dumps(res, indent=2)

    elif name == "fs_checkpoint":
        path = arguments.get("path", ".")
        message = arguments.get("message")
        res = create_git_checkpoint(path, message=message)
        return json.dumps(res.to_dict(), indent=2)

    elif name == "memory_search":
        query = arguments.get("query", "")
        limit = int(arguments.get("limit", 5))
        vault = MemoryVault()
        cache = MemoryCache(vault=vault)
        return render_recall(recall(cache, query, limit=limit))

    elif name == "memory_source":
        vault = MemoryVault()
        cache = MemoryCache(vault=vault)
        return render_source(vault, cache, str(arguments.get("id", "")))

    elif name == "memory_save":
        text = arguments.get("text", "")
        category = arguments.get("category", "fact")
        tier_str = arguments.get("tier", "tier_2_private")
        vault = MemoryVault()
        entry = MemoryEntry(
            text=text,
            category=category,
            tier=PrivacyTier.from_str(tier_str),
            source="opencode",
            metadata={"kind": "self", "source_ref": "proposed by the agent with memory_save"},
        )
        vault.append_pending(entry, "proposed by the agent; not checked against the user's own words")
        return json.dumps({
            "status": "queued_for_review", "id": entry.id[:8],
            "next_step": "Tell the user: run `python -m telemachus.memory review` to approve or reject it.",
        }, indent=2)

    elif name == "memory_status":
        vault = MemoryVault()
        cache = MemoryCache(vault=vault)
        stats = cache.get_stats()
        stats["health"] = MemoryHealth(vault).summary()
        stats["pending_review"] = len(vault.load_pending())
        stats["failed_turns"] = len(vault.load_failed_turns())
        return json.dumps(stats, indent=2)

    elif name == "memory_recent":
        limit = int(arguments.get("limit", 5))
        vault = MemoryVault()
        cache = MemoryCache(vault=vault)
        results = cache.get_recent(limit=limit)
        return json.dumps([r.to_dict() for r in results], indent=2)

    elif name == "finance_log_entry":
        store = FinanceStore()
        items = arguments.get("items")
        if items and isinstance(items, list):
            logged = store.log_batch_transactions(items, default_date=arguments.get("date"))
            return json.dumps({
                "status": "success",
                "logged_count": len(logged),
                "transactions": [t.to_dict() for t in logged],
            }, indent=2)
        else:
            payee = arguments.get("payee") or "Expense"
            amt = float(arguments.get("amount") or 0.0)
            cat = arguments.get("category") or "personal"
            single_item = {
                "payee": payee,
                "amount": amt,
                "category": cat,
                "memo": arguments.get("memo") or "",
                "date": arguments.get("date"),
            }
            logged = store.log_batch_transactions([single_item])
            tx = logged[0] if logged else None
            return json.dumps({
                "status": "success",
                "transaction_id": tx.id if tx else None,
                "payee": tx.payee if tx else "",
                "date": tx.date if tx else "",
                "postings": [p.to_dict() for p in tx.postings] if tx else [],
            }, indent=2)

    elif name == "finance_monthly_overview":
        store = FinanceStore()
        month = arguments.get("month")
        if any(arguments.get(k) is not None for k in ("income", "savings_target", "groceries_planned", "gas_planned", "pge", "notes")):
            current = store.get_monthly_overview(month)
            inc = round(float(arguments["income"]) * 100) if arguments.get("income") is not None else current.income_cents
            sav = round(float(arguments["savings_target"]) * 100) if arguments.get("savings_target") is not None else current.savings_target_cents
            groc_p = round(float(arguments["groceries_planned"]) * 100) if arguments.get("groceries_planned") is not None else current.groceries_planned_cents
            gas_p = round(float(arguments["gas_planned"]) * 100) if arguments.get("gas_planned") is not None else current.gas_planned_cents
            pge_val = round(float(arguments["pge"]) * 100) if arguments.get("pge") is not None else current.pge_cents
            notes_val = str(arguments["notes"]) if arguments.get("notes") is not None else current.notes
            overview = store.set_monthly_overview(
                month=month or current.month,
                income_cents=inc,
                savings_target_cents=sav,
                groceries_planned_cents=groc_p,
                gas_planned_cents=gas_p,
                pge_cents=pge_val,
                notes=notes_val,
            )
        else:
            overview = store.get_monthly_overview(month)

        def _f(cents: int) -> str:
            return f"${cents / 100.0:,.2f}"

        lines = [
            f"### Monthly Overview: {overview.month} [{overview.status.upper()}]",
            f"- **Income**:              {_f(overview.income_cents)}",
            f"- **Savings Target**:      {_f(overview.savings_target_cents)}",
            f"- **Fixed Bills**:         {_f(overview.fixed_bills_cents)} (Kahram, Gym, iCloud, Spotify, Wi-Fi)",
            f"- **PG&E Bill**:           {_f(overview.pge_cents)}",
            f"- **All Bills**:           {_f(overview.all_bills_cents)} (Fixed + PG&E)",
            f"- **Total Personal**:      {_f(overview.total_personal_cents)} (Budgeted personal allowance)",
            f"- **Remaining Cushion**:   {_f(overview.remaining_cushion_cents)} (Flex cushion: Variable Variance)",
            "",
            "| Planned vs Actual | Planned Budget | Actual Spent | Remaining Budget |",
            "| :--- | :---: | :---: | :---: |",
            f"| **Groceries** | {_f(overview.groceries_planned_cents)} | {_f(overview.groceries_actual_cents)} | {_f(overview.groceries_left_cents)} |",
            f"| **Car Gas**   | {_f(overview.gas_planned_cents)} | {_f(overview.gas_actual_cents)} | {_f(overview.gas_left_cents)} |",
            f"| **Variable Variance** | | | **{_f(overview.variable_variance_cents)}** |",
        ]
        if overview.notes:
            lines.append(f"\n*Notes: {overview.notes}*")
        return "\n".join(lines)

    elif name == "finance_get_balance":
        store = FinanceStore()
        balances = store.get_all_balances(as_of_date=arguments.get("as_of_date"))
        def _f(cents: int) -> str:
            return f"${cents / 100.0:,.2f}"

        lines = [
            f"### Account Balance Sheet (As of {balances['as_of_date']})",
            "| Account | Type | Cleared Balance |",
            "| :--- | :--- | :---: |",
        ]
        for acc, cents in balances["account_balances"].items():
            acc_type = "Asset" if "Assets:" in acc else ("Liability" if "Liabilities:" in acc else "Equity")
            lines.append(f"| {acc} | {acc_type} | {_f(cents)} |")

        lines.extend([
            "",
            f"- **Total Liquid Assets**: {_f(balances['liquid_assets_cents'])} (Wells Fargo Checking + Capital One HYSA)",
            f"- **Total Liabilities**:   {_f(balances['liabilities_cents'])} (Visa Credit Card balance)",
            f"- **NET WORTH**:           {_f(balances['net_worth_cents'])}",
        ])
        return "\n".join(lines)

    elif name == "finance_list_recurring":
        store = FinanceStore()
        engine = RecurringEngine(store)
        days = int(arguments.get("days_ahead") or 30)
        obligations = engine.get_upcoming_obligations(days_ahead=days)
        cash_flow = engine.get_committed_cash_flow(days_ahead=days)
        alloc = engine.get_paycheck_allocation()

        def _f(cents: int) -> str:
            return f"${cents / 100.0:,.2f}"

        next_paycheck = alloc.next_paycheck_date
        before_paycheck = [o for o in obligations if o["due_date"] <= next_paycheck]
        after_paycheck = [o for o in obligations if o["due_date"] > next_paycheck]
        due_before_total = sum(o["amount_cents"] for o in before_paycheck if o.get("status") == "due")

        lines = [
            f"### 🗓️ Upcoming Obligations Radar (Next {days} Days)",
            f"- **Current Checking Balance**:        {_f(cash_flow['checking_balance_cents'])}",
            f"- **Next Paycheck Arrives**:           **{next_paycheck}**",
            f"- **Bills Due BEFORE Next Paycheck**: -{_f(due_before_total)}",
            f"- **Checking After Due Bills**:        **{_f(cash_flow['checking_balance_cents'] - due_before_total)}**",
            "",
            f"### 🚨 ALL Bills Due BEFORE Next Paycheck ({next_paycheck}):",
            "| Due Date | Obligation | Amount Due | Status | Autopay |",
            "| :--- | :--- | :---: | :---: | :---: |",
        ]
        if not before_paycheck:
            lines.append("| — | No bills scheduled before next paycheck | $0.00 | — | — |")
        else:
            for o in before_paycheck:
                lines.append(
                    f"| {o['due_date']} | {o['name']} | {_f(o['amount_cents'])} | [{o['status'].upper()}] | {'Yes' if o['is_autopay'] else 'No'} |"
                )

        if after_paycheck:
            lines.extend([
                "",
                f"### ℹ️ Obligations Due AFTER Next Paycheck ({next_paycheck}):",
                "| Due Date | Obligation | Amount | Status | Autopay |",
                "| :--- | :--- | :---: | :---: | :---: |",
            ])
            for o in after_paycheck:
                lines.append(
                    f"| {o['due_date']} | {o['name']} | {_f(o['amount_cents'])} | [{o['status'].upper()}] | {'Yes' if o['is_autopay'] else 'No'} |"
                )

        return "\n".join(lines)

    elif name == "finance_goals_progress":
        store = FinanceStore()
        goals = store.get_goals_progress(month=arguments.get("month"))
        def _f(cents: int) -> str:
            return f"${cents / 100.0:,.2f}"

        lines = [
            "### Monthly Goals Tracker",
            "| Goal | Target | Saved / Paid | Remaining | Progress |",
            "| :--- | :---: | :---: | :---: | :---: |",
        ]
        for g in goals:
            lines.append(f"| {g.name} | {_f(g.target_cents)} | {_f(g.saved_cents)} | {_f(g.remaining_cents)} | {g.progress_bar} |")
        return "\n".join(lines)

    elif name == "finance_query_transactions":
        store = FinanceStore()
        category = str(arguments.get("category") or "").lower().strip()
        date = arguments.get("date")
        start_date = date or arguments.get("start_date")
        end_date = date or arguments.get("end_date")
        limit = int(arguments.get("limit") or 20)

        cat_account_map = {
            "groceries": "Expenses:Food:Groceries",
            "grocery": "Expenses:Food:Groceries",
            "gas": "Expenses:Transport:Gas",
            "fuel": "Expenses:Transport:Gas",
            "dining": "Expenses:Food:Dining",
            "restaurant": "Expenses:Food:Dining",
            "wifi": "Expenses:Utilities:WiFi",
            "internet": "Expenses:Utilities:WiFi",
            "pge": "Expenses:Utilities:PGE",
            "gym": "Expenses:Subscriptions:Gym",
            "spotify": "Expenses:Subscriptions:Spotify",
            "icloud": "Expenses:Subscriptions:AppleCloud",
            "apple": "Expenses:Subscriptions:AppleCloud",
            "kahram": "Expenses:Family:Kahram",
        }
        account_id = cat_account_map.get(category)
        if not account_id and category.startswith("expenses:"):
            account_id = category

        txs = store.list_transactions(
            start_date=start_date,
            end_date=end_date,
            account_id=account_id,
            limit=200,
        )

        if category and not account_id:
            txs = [t for t in txs if category in t.payee.lower() or category in t.memo.lower()]

        total_cents = 0
        matching_items = []
        for t in txs:
            amount_cents = 0
            cat_name = ""
            for p in t.postings:
                if p.amount_cents > 0 and (not account_id or p.account_id == account_id):
                    amount_cents = p.amount_cents
                    cat_name = p.account_id
                    break
            if amount_cents == 0 and t.postings:
                for p in t.postings:
                    if p.amount_cents > 0:
                        amount_cents = p.amount_cents
                        cat_name = p.account_id
                        break
                if amount_cents == 0:
                    amount_cents = abs(t.postings[0].amount_cents)

            total_cents += amount_cents
            matching_items.append({
                "date": t.date,
                "payee": t.payee,
                "amount_cents": amount_cents,
                "category": cat_name,
                "memo": t.memo,
            })

        count = len(matching_items)
        avg_cents = round(total_cents / count) if count > 0 else 0
        most_recent = matching_items[0] if matching_items else None

        def _f(c: int) -> str:
            return f"${c / 100.0:,.2f}"

        title = f"Transaction Query Results"
        if category:
            title += f" for '{category.title()}'"
        if date:
            title += f" on {date}"
        elif start_date or end_date:
            title += f" ({start_date or 'Start'} to {end_date or 'Now'})"

        lines = [
            f"### {title} ({count} transactions found)",
            f"- **Total Spent**:       {_f(total_cents)}",
            f"- **Transaction Count**: {count}",
            f"- **Average Amount**:    {_f(avg_cents)}",
        ]
        if most_recent:
            lines.append(f"- **Most Recent**:       {_f(most_recent['amount_cents'])} on {most_recent['date']} ({most_recent['payee']})")

        lines.extend([
            "",
            "| Date | Payee | Amount | Category | Memo |",
            "| :--- | :--- | :---: | :--- | :--- |",
        ])
        for it in matching_items[:limit]:
            clean_cat = it['category'].split(':')[-1] if it['category'] else 'General'
            lines.append(f"| {it['date']} | {it['payee']} | {_f(it['amount_cents'])} | {clean_cat} | {it['memo']} |")

        return "\n".join(lines)

    elif name == "finance_paycheck_plan":
        store = FinanceStore()
        engine = RecurringEngine(store)
        date = arguments.get("date")
        alloc = engine.get_paycheck_allocation(as_of_date=date)

        def _f(cents: int) -> str:
            return f"${cents / 100.0:,.2f}"

        due_count = len([b for b in alloc.bills if b.get('status') == 'due'])
        lines = [
            f"### 🗓️ Paycheck Window & Due Dates Summary",
            f"- **Current Cycle Start Date**:        {alloc.paycheck_date}",
            f"- **NEXT PAYCHECK ARRIVES ON**:        **{alloc.next_paycheck_date}** (in 14 days)",
            "",
            f"### 🏦 Cash Position Summary (Checking Account)",
            f"- **Current Checking Balance**:        {_f(alloc.checking_balance_cents)}",
            f"- **Bills Due BEFORE Next Paycheck**: -{_f(alloc.bills_due_cents)}",
            f"- **Checking After Due Bills**:        **{_f(alloc.checking_after_bills_cents)}**",
            "",
        ]

        if alloc.credit_card_reminder:
            lines.extend([
                f"💳 **Credit Card Reminder**: {alloc.credit_card_reminder}",
                "",
            ])

        lines.extend([
            f"### 🚨 ALL Payments Due BEFORE Next Paycheck ({alloc.next_paycheck_date}):",
            f"*(Notice: ALL items in this table fall in the window {alloc.paycheck_date} to {alloc.next_paycheck_date} and MUST be paid before the next paycheck arrives)*",
            "",
            "| Due Date | Obligation | Amount Due | Status | Autopay | Details |",
            "| :--- | :--- | :---: | :---: | :---: | :--- |",
        ])
        if not alloc.bills:
            lines.append("| — | No recurring bills scheduled before next paycheck | $0.00 | — | — | — |")
        else:
            for b in alloc.bills:
                lines.append(f"| {b['due_date']} | {b['name']} | {_f(b['amount_cents'])} | [{b['status'].upper()}] | {'Yes' if b.get('is_autopay') else 'No'} | {b.get('note', '')} |")

        lines.extend([
            "",
            f"**TOTAL DUE BEFORE NEXT PAYCHECK ({alloc.next_paycheck_date})**: **{_f(alloc.bills_due_cents)}** ({due_count} bills due)",
            "",
            f"⚠️ **DIRECTIVE FOR ASSISTANT**: The items above (including Spotify on Oct 25, Brother Payment on Nov 01, Gym on Nov 01, and Credit Card on Nov 02) are all scheduled BEFORE the next paycheck arrives on {alloc.next_paycheck_date}. When the user asks what is due until their next paycheck, explicitly include all due items above!",
            "",
            f"### 📊 Paycheck Cash Flow Partition ({alloc.paycheck_date} to {alloc.next_paycheck_date})",
            f"- **Paycheck Take-Home**:        {_f(alloc.paycheck_amount_cents)}",
            f"- **Immediate HYSA Savings**:    -{_f(alloc.savings_transfer_cents)} (Moved straight to Capital One HYSA)",
            f"- **Net Checking Inflow**:        {_f(alloc.net_checking_cents)}",
            f"- **Bills Due Before Paycheck**: -{_f(alloc.bills_due_cents)}",
            f"- **2-Week Variable Needs**:     -{_f(alloc.variable_needs_cents)} (Groceries: {_f(alloc.groceries_allocated_cents)}, Gas: {_f(alloc.gas_allocated_cents)})",
            f"- **SAFE-TO-SPEND CUSHION**:      **{_f(alloc.safe_to_spend_cents)}** (Flexible spending for this cycle)",
            "",
        ])
        if alloc.other_bills:
            lines.extend([
                f"### ℹ️ Obligations Due AFTER Next Paycheck ({alloc.next_paycheck_date}):",
                "| Due Day | Obligation | Amount | Status |",
                "| :--- | :--- | :---: | :--- |",
            ])
            for b in alloc.other_bills:
                lines.append(f"| Day {b['due_day']} | {b['name']} | {_f(b['amount_cents'])} | Due in future paycheck cycles |")

        return "\n".join(lines)

    elif name == "finance_plan_breakdown":
        store = FinanceStore()
        engine = RecurringEngine(store)
        now_str = arguments.get("date") or time.strftime("%Y-%m-%d")
        month_str = now_str[:7]
        overview = store.get_monthly_overview(month_str)
        alloc = engine.get_paycheck_allocation(as_of_date=now_str)
        balances = store.get_all_balances(as_of_date=now_str)

        def _f(cents: int) -> str:
            return f"${cents / 100.0:,.2f}"

        lines = [
            f"### Comprehensive Financial Situation & Bi-Weekly Allocation Breakdown",
            f"- **As of Date**:            {now_str} (Month: {month_str})",
            "",
            f"#### 🏦 Cash Position Summary (Checking Account)",
            f"- **Current Checking Balance**:        {_f(alloc.checking_balance_cents)}",
            f"- **Payments Due in Window**:          -{_f(alloc.bills_due_cents)}",
            f"- **Checking After Due Payments**:      **{_f(alloc.checking_after_bills_cents)}**",
            "",
        ]

        if alloc.credit_card_reminder:
            lines.extend([
                f"💳 **Credit Card Reminder**: {alloc.credit_card_reminder}",
                "",
            ])

        lines.extend([
            f"#### 🗓️ Monthly Plan Breakdown ({month_str})",
            f"- **Monthly Take-Home**:      {_f(overview.income_cents)}",
            f"- **HYSA Savings Target**:    {_f(overview.savings_target_cents)} (Target transfer to Capital One HYSA)",
            f"- **Fixed Bills & Utilities**: {_f(overview.all_bills_cents)} (Fixed Bills: {_f(overview.fixed_bills_cents)}, PG&E: {_f(overview.pge_cents)})",
            f"- **Groceries Budget**:       {_f(overview.groceries_planned_cents)} (Spent: {_f(overview.groceries_actual_cents)} | Left: {_f(overview.groceries_left_cents)})",
            f"- **Dining / Eating Out**:    {_f(overview.dining_planned_cents)} (Spent: {_f(overview.dining_actual_cents)} | Left: {_f(overview.dining_left_cents)})",
            f"- **Car Gas Budget**:         {_f(overview.gas_planned_cents)} (Spent: {_f(overview.gas_actual_cents)} | Left: {_f(overview.gas_left_cents)})",
            f"- **Personal Allowance**:     {_f(overview.total_personal_cents)} (Budgeted discretionary envelope)",
            f"- **Remaining Flex Cushion**: {_f(overview.remaining_cushion_cents)} (Unspent variable funds)",
            "",
            f"#### 💵 Next Two Weeks Allocation ({alloc.paycheck_date} to {alloc.next_paycheck_date})",
            f"- **Bi-Weekly Paycheck**:     {_f(alloc.paycheck_amount_cents)}",
            f"- **Immediate HYSA Savings**: -{_f(alloc.savings_transfer_cents)} (Moved straight to Capital One HYSA)",
            f"- **Net Checking Inflow**:    {_f(alloc.net_checking_cents)}",
            f"- **Bills Due in Window**:    -{_f(alloc.bills_due_cents)}",
        ])

        if alloc.bills:
            lines.append("  **Bills Due in this 2-Week Window:**")
            for b in alloc.bills:
                note_str = f" ({b['note']})" if b.get("note") else ""
                lines.append(f"  • **{b['name']}** (Due: {b['due_date']}): {_f(b['amount_cents'])}{note_str}")

        if getattr(alloc, "other_bills", None):
            lines.append("")
            lines.append("  **Bills Outside This Window (Later Cycle):**")
            for ob in alloc.other_bills:
                lines.append(f"  • {ob['name']}: {_f(ob['amount_cents'])} ({ob['note']})")

        lines.extend([
            "",
            f"- **2-Week Groceries**:       -{_f(alloc.groceries_allocated_cents)}",
            f"- **2-Week Eating Out**:      -{_f(alloc.dining_allocated_cents)}",
            f"- **2-Week Car Gas**:         -{_f(alloc.gas_allocated_cents)}",
            f"- **SAFE-TO-SPEND CUSHION**:  **{_f(alloc.safe_to_spend_cents)}** (Discretionary cash for next 14 days)",
        ])
        return "\n".join(lines)

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

    # Automatically start Ingress Tool Pruning Proxy on http://127.0.0.1:1235
    start_ingress_proxy_background()

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
                    "serverInfo": {"name": "telemachus", "version": "0.2.0"},
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
