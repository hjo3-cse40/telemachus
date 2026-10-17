# Telemachus 🛡️

> **A 100% private, local AI agent workflow designed for personal daily operations on an 18 GB Apple Silicon Mac.**

Telemachus is a custom personal harness built to run agentic daily workflows—managing emails, file automation, financial planning, and resilient web research—entirely offline, without transmitting sensitive personal data to external cloud providers.

---

## 🏛️ System Architecture

Telemachus operates on a **dual-model cooperative architecture** to stay responsive within an 18 GB unified memory budget:

```
                      +------------------------------------------+
                      |         User Input / Prompt Turn         |
                      +--------------------+---------------------+
                                           |
                                           v
                  +----------------------------------------------+
                  |         LAYA INGRESS DECISION GATE           |
                  |  - Classify intent (Email/Files/Finance/Web) |
                  |  - Prune tools (Only inject relevant schemas)|
                  |  - Vector lookup via nomic-embed             |
                  +------------------------+---------------------+
                                           |
                                           v
                  +----------------------------------------------+
                  |            OPENCODE INTERPRETER              |
                  |      (Harness: Orchestration & State)        |
                  |                                              |
                  |     Prompt + Pruned Context + Retrieval      |
                  +------------------------+---------------------+
                                           |
                                           v
                  +----------------------------------------------+
                  |            QWEN 3.5 9B (MLX 4-bit)           |
                  |        (Served locally via LM Studio)        |
                  |   - Deep reasoning & token generation        |
                  |   - Formulates action / function call        |
                  +------------------------+---------------------+
                                           |
                                           v
                  +----------------------------------------------+
                  |         LAYA EGRESS GUARDIAN GATE            |
                  |  - Evaluates Proposed Tool Action            |
                  |  - Computes Intent-Action Consistency        |
                  |  - Checks Blast Radius & System Boundaries   |
                  +------------+--------------------+------------+
                               |                    |
                 [Permitted / Safe]          [High Risk / Mismatch]
                               |                    |
                               v                    v
                  +--------------------+   +---------------------+
                  |   MCP Tool Exec    |   | User Confirmation   |
                  | (Email/FS/Fin/Web) |   | or Trash Safeguard  |
                  +--------------------+   +---------------------+
```

1. **Harness & Orchestration**: [OpenCode Interpreter](https://opencode.ai) (`opencode` CLI) manages sessions, user interactions, and MCP tool execution.
2. **Generative Intelligence**: `qwen/qwen3.5-9b` (4-bit MLX, 64k context window) running locally via **LM Studio** (`http://127.0.0.1:1234/v1`).
3. **Decision & Safety Coprocessor**: **Laya** (`convaiinnovations/laya`, ~421M ModernBERT-based classifier/router on MPS). Executes in ~50–100 ms to prune tool schemas before inference and verify system safety before execution.
4. **Retrieval**: `nomic-embed` for semantic indexing and fast context lookups.

---

## 🎯 Scope & Non-Goals

### In Scope
- **Personal Daily Operations**: Inbox filtering, reply drafts, personal ledger bookkeeping, system maintenance, and document synthesis.
- **Strict Privacy**: Zero telemetry, zero external model queries. Financials and private emails never leave the local machine.
- **Safe Full-Disk Access**: Full automation on local files with intelligent intent-verification and recoverable deletions via macOS Trash.
- **Hardware Efficiency**: Tailored specifically to run seamlessly within 18 GB unified memory without memory paging.

### Non-Goals
- Not a commercial product or multi-tenant SaaS framework.
- Not a generic framework aiming to support dozens of arbitrary cloud APIs.
- No autonomous email sending: drafts are always staged in `Drafts` for human approval.

---

## 🛠️ The 4 Core Local Tools

- [x] **1. Emails (Shipped & Available in OpenCode MCP)**:
  - Multi-account connection (1 active Yahoo, 4 Gmail placeholders ready) over direct TLS IMAP.
  - Safe, non-destructive read-only fetching (`BODY.PEEK`) with strict 16KB per-email caps to protect model context.
  - Safe drafting engine saving RFC822 drafts directly to remote `Drafts` folder (never sends).
  - Fast local ModernBERT triage on Apple Silicon MPS with 2-pass order de-biasing.
  - Offline SQLite mailbox indexer (`data/email_index.sqlite3`) for querying thousands of historical emails in <50 tokens.
  - Stdio MCP server registered in OpenCode (`telemachus-email`).
- [ ] **2. Computer Files (with Laya Guard)**:
  - Enables local file editing, search, and shell execution.
  - Hard system boundaries protect root (`/System`, `/usr`, `~/.ssh`).
  - Replaces permanent `rm` with macOS `trash` for reversible file operations.
  - Laya verifies that destructive actions match explicit user intent to eliminate false-positive alerts.
- [ ] **3. Finances**:
  - Double-entry bookkeeping engine (SQLite / Plain Text Accounting) ensures arithmetic calculations are 100% exact.
  - Qwen 3.5 9B focuses on reasoning, budget forecasts, and financial scenario planning.
- [ ] **4. Anti-Bot Web Search**:
  - Connects via stealth browser / Chrome DevTools Protocol to bypass Cloudflare/Akamai bot blocking.
  - Uses `trafilatura` to distill clean Markdown, avoiding token bloat in the 64k context window.

---

## 📬 Email Subsystem & MCP Integration

The email subsystem is fully operational and exposed both via CLI and as a native MCP server for OpenCode.

### Architecture Highlights
* **Configuration & Credential Isolation**: Accounts are defined in [`config/accounts.yaml`](./config/accounts.yaml), while credentials live strictly in a git-ignored `.env` file (see [`.env.example`](./.env.example)).
* **Non-Destructive IMAP**: Always uses `BODY.PEEK[]` and read-only folder inspection so unread flags are preserved.
* **Safe Drafting**: Writes drafts directly to the remote `Drafts` folder; never makes outbound SMTP connections.
* **Laya MPS ModernBERT Triage**: Classifies emails into `important_documents`, `transactional`, `newsletter_marketing`, and `suspicious_spam`.
* **Offline SQLite Indexer**: Crawls and stores historical emails into local SQLite (`data/email_index.sqlite3`) so Qwen can query thousands of emails deterministically via SQL without flooding the 64k context window.

### OpenCode MCP Tools

When using OpenCode (`opencode`), the `telemachus-email` MCP server provides:

| Tool | Purpose |
| :--- | :--- |
| `email_status` | Check mailbox connection and unread counts across all accounts. |
| `email_list` | List recent email headers in read-only mode without touching read flags. |
| `email_show` | Read full cleaned text and attachment list for a specific message UID. |
| `email_triage` | Classify recent unread emails with Laya on MPS and suggest priority. |
| `email_draft` | Save an email draft into your remote `Drafts` folder for human review. |
| `email_index` | Crawl and cache unindexed emails into local SQLite with Laya tags. |
| `email_search_index` | Instantly search local SQLite index by keyword, sender, category, or priority. |
| `email_index_stats` | View high-level statistics and category breakdown of all indexed emails. |

### CLI Command Reference

You can also run all email operations directly from terminal:

```bash
# Check mailbox status and connections
python3 -m telemachus.email status

# List recent emails (read-only)
python3 -m telemachus.email list --limit 10
python3 -m telemachus.email list --limit 10 --unread

# Read full content of a specific email
python3 -m telemachus.email show <UID>

# Run Laya AI triage on recent emails
python3 -m telemachus.email triage --limit 10

# Safely save a draft to your Drafts folder
python3 -m telemachus.email draft \
  --to "user@example.com" \
  --subject "Draft Subject" \
  --body "Draft body content."

# Index the next 50 unindexed emails into local SQLite
python3 -m telemachus.email index --max 50

# Search locally indexed emails in SQLite
python3 -m telemachus.email search -q "PayPal"
python3 -m telemachus.email search -c transactional

# View index statistics
python3 -m telemachus.email stats
```

### Running the Test Suite

A comprehensive 8-step test suite validates connectivity, read-only guarantees, draft creation/cleanup, Laya classification, and MCP JSON-RPC protocol:

```bash
python3 tests/test_email_thorough.py
```

---

## 📚 Detailed Documentation

Comprehensive specifications are maintained in the [`docs/`](./docs) directory:

- [**System Architecture**](./docs/SYSTEM_ARCHITECTURE.md): Complete breakdown of component interactions, memory budgeting, and token economics on Apple Silicon.
- [**Laya Integration Spec**](./docs/LAYA_INTEGRATION_SPEC.md): How Laya handles Ingress tool pruning, Egress safety gating, and solves the false-positive alert problem.
- [**Tooling Roadmap**](./docs/TOOLING_ROADMAP.md): Detailed technical blueprints for the 4 core tools, plus recommended expansions (clipboard bridge, calendar sync, context compression).
- [**Workflow Status & Log**](./docs/WORKFLOW_STATUS.md): Living checklist of active experiments, tool implementation progress, and architectural decisions.

---

## 🚀 Quick Start

### 1. Start LM Studio Local Server
Ensure LM Studio is serving `qwen/qwen3.5-9b` at:
```bash
http://127.0.0.1:1234/v1
```

### 2. Verify OpenCode Configuration & MCP Server
Verify OpenCode connects to LM Studio and the Telemachus email MCP server:
```bash
opencode mcp list
```
*(Should show `● ✓ telemachus-email connected`).*

### 3. Launch Telemachus
Start OpenCode in this workspace:
```bash
opencode
```
To resume the previous session:
```bash
opencode -c
```
