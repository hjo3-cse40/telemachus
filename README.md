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
2. **Ingress Tool Pruning Proxy**: Automated streaming proxy (`http://127.0.0.1:1235/v1`) powered by Laya domain routing and `nomic-embed-text-v1.5@q8_0`. Automatically prunes 18+ MCP tool schemas down to 3–5 relevant tools (or 0 for general queries), cutting prompt token overhead by 70–99% per turn.
3. **Generative Intelligence**: `qwen/qwen3.5-9b` (4-bit MLX, 64k context window) running locally via **LM Studio** (`http://127.0.0.1:1234/v1`).
4. **Decision & Safety Coprocessor**: **Laya** (`convaiinnovations/laya`, ~421M ModernBERT-based classifier/router on MPS). Executes in ~50–100 ms to evaluate Intent-Action alignment and enforce blast-radius safety.
5. **Retrieval**: `nomic-embed-text-v1.5@q8_0` (768-dim GGUF) for semantic indexing, vector lookups, and fast tool cosine similarity.

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
  - Stdio MCP server registered in OpenCode (`telemachus`).
- [x] **2. Computer Files (with Laya Guard)**:
  - Enables local file editing, search, navigation, and shell execution.
  - Tier 0 deterministic boundaries protect system roots (`/System`, `/usr`, `/Library`, `/etc`) and sensitive credentials (`~/.ssh`, `~/.gnupg`, foreign `.env`).
  - Tier 1 Laya ModernBERT classifier on Apple Silicon MPS with 2-pass order de-biasing verifies Intent-Action alignment.
  - Tier 2 soft recovery replaces permanent `rm` with macOS native `/usr/bin/trash` (recoverable via Cmd+Z or Trash bin).
  - Tier 3 automatic Git checkpointing before modifications, with interactive elevation for ambiguous actions.
  - 10 new MCP tools registered in OpenCode (`telemachus` MCP server).
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

## 📁 Computer Filesystem & Laya Guardrail Subsystem

The filesystem subsystem provides complete, token-efficient workspace navigation and surgical file editing guarded by the 4-tier Laya Guardrail architecture.

### The 4-Tier Guardrail Pipeline
1. **Tier 0: Deterministic Boundary Shield (Zero Latency)**: Blocks operations attempting to modify core system paths (`/System`, `/usr`, `/etc`) or access sensitive security secrets (`~/.ssh`, `~/.gnupg`, `~/.aws`, foreign `.env` files). Catastrophic shell commands (`mkfs`, `fdisk`, `chmod -R 777 /`) are rejected immediately.
2. **Tier 1: Intent-Action Alignment (Laya ModernBERT on MPS)**: Laya evaluates proposed mutations against the user's explicit prompt using 2-pass option-order de-biasing to ensure the model doesn't alter files outside what was requested.
3. **Tier 2: Soft Recovery via macOS Trash**: Any file or directory deletion is routed through macOS native `/usr/bin/trash` instead of permanent `rm` or `unlink`. Every deleted item is recoverable via Cmd+Z or the macOS Trash bin, and logged to `data/trash_audit.jsonl`.
4. **Tier 3: Git Auto-Checkpointing & Elevation**: Automatically captures a lightweight Git stash checkpoint before editing files in a repository. If Laya detects ambiguity or excessive blast radius, an elevation requirement is returned.

### OpenCode MCP Tools (Filesystem)

| Tool | Parameters | Description |
| :--- | :--- | :--- |
| `fs_list` | `path`, `depth`, `show_hidden` | List directory contents with depth controls, file sizes, and noise suppression (omits `.git`, `node_modules`, `.venv`). |
| `fs_read` | `path`, `start_line`, `end_line` | Read text file with line numbers and windowing. Prevents reading binary files or flooding model context. |
| `fs_search` | `pattern`, `root_dir`, `max_results` | Find files by name or path pattern across a directory tree. |
| `fs_grep` | `query`, `root_dir`, `file_pattern`, `is_regex` | Search text or code patterns across files with line number context. |
| `fs_stat` | `path` | Get file size, octal permissions, and timestamps. |
| `fs_edit` | `path`, `target_content`, `replacement_content`, `user_intent`, `dry_run` | Surgically replace a unique code block with new content. Protected by LayaGuard with Git checkpointing. |
| `fs_write` | `path`, `content`, `overwrite`, `user_intent` | Safely write or create a file with overwrite protection and Git checkpointing. |
| `fs_delete` | `path`, `user_intent`, `confirmed` | Safely delete a file by moving it to macOS Trash (recoverable via Cmd+Z). Protected by LayaGuard. |
| `fs_run` | `command`, `cwd`, `user_intent`, `timeout` | Execute a shell command vetted by Tier 0 and Tier 1 safety gates. |
| `fs_checkpoint` | `path`, `message` | Manually capture a lightweight Git stash snapshot before risky tasks. |

### CLI Command Reference

Test and exercise filesystem tools directly:

```bash
# List directory with depth control
python3 -m telemachus.fs list . --depth 2

# Read lines with windowing
python3 -m telemachus.fs read telemachus/fs/models.py --start 1 --end 25

# Locate files by pattern
python3 -m telemachus.fs search boundaries

# Search text across codebase
python3 -m telemachus.fs grep GuardVerdict --ext "*.py"

# Evaluate an action against LayaGuard
python3 -m telemachus.fs check read ~/.ssh/id_rsa
python3 -m telemachus.fs check execute "chmod -R 777 /"

# Safely delete via macOS Trash
python3 -m telemachus.fs delete /tmp/test_file.txt --intent "delete test file"
```

### Running Test Suite

```bash
python3 -m unittest tests/test_fs_guard.py -v
```

---

## 📊 Empirical Benchmarks & Token Optimization

Telemachus includes synthetic datasets and rigorous benchmarks to stress-test context memory limits, needle-in-the-haystack (NIAH) retrieval, and prompt token efficiency on Apple Silicon unified memory.

### 1. Ingress Tool Pruning & Token Savings

#### The Problem: Tool Schema Bloat
When an MCP client registers all 18 Telemachus tools (email operations, filesystem management, guardrails), their JSON schemas consume **2,961 tokens (~10.6 KB)**. In a standard setup, this massive payload is prepended to the system prompt on **every single turn**. On local models like `qwen/qwen3.5-9b` (MLX 4-bit) running on an Apple Silicon M3 Pro (~140–500 tok/s prefill rate), this adds an unavoidable **~8.5 second prefill latency** before the model can generate its first response token, quickly eating into the context window.

#### The Solution: Intelligent Ingress Tool Pruning
Telemachus routes all client completions through an automated streaming Ingress Proxy (`http://127.0.0.1:1235/v1` $\rightarrow$ `1234`). Before the payload reaches Qwen, the proxy dynamically inspects user intent and strips away unused tool definitions:

| Scenario / Prompt Turn | Unpruned (Before) | Pruned via Ingress (After) | Tokens Saved | Context Reduction | Selected Tools |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **Email Turn**<br>_`"Check my mailbox status and list unread emails"`_ | 3,030 tokens | **801 tokens** | **-2,229 tokens** | **-73.6%** | `email_status`, `email_list`, `email_show`, `email_draft` |
| **Filesystem Turn**<br>_`"Search for files named README or config"`_ | 3,032 tokens | **847 tokens** | **-2,185 tokens** | **-72.1%** | `fs_list`, `fs_read`, `fs_search`, `fs_grep`, `fs_stat` |
| **General Turn**<br>_`"Explain the difference between TCP and UDP"`_ | 3,032 tokens | **22 tokens** | **-3,010 tokens** | **-99.3%** | *(All 18 tools dropped)* |

* **Real-World Operational Impact**: In live multi-turn testing covering complex email index querying and traversing a 15-level deep directory structure, the entire multi-step workflow consumed only **~14,000 tokens total (23% of the 64k window)**, preserving 77% headroom for sustained reasoning.

### 2. How Tools are Called & Routed

The Ingress architecture guarantees seamless, zero-maintenance tool selection:

1. **Automated Daemon Startup**: The Ingress Proxy starts automatically in a background daemon thread whenever the Telemachus MCP server launches. No manual proxy scripts or terminal tabs are required—simply run `opencode`.
2. **Multi-Turn Context Extraction**: Short follow-up messages (e.g., *"now read it"*, *"show me more"*, *"index them"*) preserve the active tool domain by analyzing the last 3 conversational turns.
3. **Prefix Normalization**: OpenCode automatically prefixes MCP tool names (e.g. `telemachus_fs_read`, `telemachus_email_list`). The router transparently strips and restores prefixes so matching is seamless regardless of the client format.
4. **Combo Guarantees**: Prevents tool looping. For example, when filesystem operations are detected, `fs_read` is always guaranteed alongside `fs_search` and `fs_grep`, ensuring the model can immediately inspect discovered files without getting stuck.
5. **Hybrid Intent & Vector Routing**: Uses fast keyword heuristics for zero-latency domain detection, falling back to 768-dimensional cosine similarity via `nomic-embed-text-v1.5@q8_0` with pre-computed tool embeddings (`data/tool_embeddings.json`).

### 3. Synthetic Benchmark Datasets

We provide two isolated synthetic sandboxes for rigorous context limit and NIAH testing:

* **Synthetic Filesystem (`data/synthetic_fs/`)**:
  - **Scale**: 527 generated files across wide catalogs (500 items in a single directory) and a 15-level deep hierarchy.
  - **Needles**: Contains `odysseus_plan.txt` buried at level 15, plus a 4,000-line haystack file containing 5 depth-calibrated canary needles (0%, 25%, 50%, 75%, 100% file depth).
  - **Edge Cases**: Includes Latin-1 encoding, binary null bytes, zero-byte files, and noisy ignore targets (`.git`, `node_modules`).
* **Synthetic Mailbox (`data/synthetic_email_index.sqlite3`)**:
  - **Scale**: 1,000 realistic categorized emails (transactional, documents, newsletters, spam).
  - **Needles**: Injected canary references, including flight bookings (`NZ742-AUCKLAND-2026-CONFIRMED`), tax documents, and urgent invoices.
  - **Efficiency**: Allows SQLite FTS queries across 1,000+ emails in 5ms using <60 tokens, compared to 73,000 tokens for brute-force context dumping.

### 4. Running the Synthetic Benchmark Suite

You can generate the synthetic datasets and execute the benchmark test suites with:

```bash
# Generate synthetic filesystem test data (data/synthetic_fs/)
python3 tests/benchmarks/fs_generator.py

# Generate synthetic mailbox index (data/synthetic_email_index.sqlite3)
python3 tests/benchmarks/email_generator.py

# Run filesystem limits and NIAH retrieval tests
PYTHONPATH=. python3 -m unittest tests/benchmarks/test_fs_limits.py

# Run email context scaling and search benchmarks
PYTHONPATH=. python3 -m unittest tests/benchmarks/test_email_limits.py

# Run Nomic embedding diagnostics and speed benchmarks
PYTHONPATH=. python3 -m unittest tests/benchmarks/test_nomic_embed.py

# Run live Ingress Before vs. After prompt token benchmark
PYTHONPATH=. python3 -m unittest tests/benchmarks/test_ingress_benchmark.py
```

---

## 📚 Detailed Documentation

Comprehensive specifications are maintained in the [`docs/`](./docs) directory:

- [**System Architecture**](./docs/SYSTEM_ARCHITECTURE.md): Complete breakdown of component interactions, memory budgeting, and token economics on Apple Silicon.
- [**Laya Integration Spec**](./docs/LAYA_INTEGRATION_SPEC.md): How Laya handles Ingress tool pruning, Egress safety gating, and solves the false-positive alert problem.
- [**Memory Intake**](./docs/MEMORY_INTAKE.md): What may become a memory (own words only, quote-checked), the review list, loud failures, cited recall, and the daily `review` / `health` / `retry` commands.
- [**Tooling Roadmap**](./docs/TOOLING_ROADMAP.md): Detailed technical blueprints for the 4 core tools, plus recommended expansions (clipboard bridge, calendar sync, context compression).
- [**Workflow Status & Log**](./docs/WORKFLOW_STATUS.md): Living checklist of active experiments, tool implementation progress, and architectural decisions.

---

## 🚀 Quick Start

### 1. Start LM Studio Local Server
Ensure LM Studio is serving `qwen/qwen3.5-9b` at:
```bash
http://127.0.0.1:1234/v1
```

### 2. Verify OpenCode Configuration & Ingress Proxy
OpenCode connects to Telemachus through the automated Ingress Tool Pruning Proxy on port `1235` (which forwards to LM Studio on port `1234`).

Ensure `~/.config/opencode/opencode.jsonc` contains:
```jsonc
{
  "provider": {
    "lmstudio": {
      "options": { "baseURL": "http://127.0.0.1:1235/v1" }
    }
  },
  "mcp": {
    "telemachus": {
      "type": "local",
      "command": ["/opt/homebrew/bin/python3", "-m", "telemachus.mcp"],
      "environment": {
        "PYTHONPATH": "/path/to/telemachus"
      }
    }
  }
}
```
*(The Ingress Proxy starts automatically whenever OpenCode connects to Telemachus MCP).*

### 3. Launch Telemachus (CLI Harness Only)
Start OpenCode directly in this workspace:
```bash
opencode
```
To resume the previous session:
```bash
opencode -c
```

---

## 🎛️ Telemachus Terminal Cockpit (v1)

Launch the unified Tokyo Night split cockpit pairing the visual operational dashboard with the OpenCode CLI harness:

```bash
# Launch the full split tmux cockpit (interactive dashboard + OpenCode harness)
./bin/telemachus-cockpit

# Or via Python module:
python3 -m telemachus.cockpit
```

### Cockpit Controls & Features
* **Mouse & Trackpad**: Full mouse mode enabled (`set -g mouse on`). Click any email row to view its preview snippet, drag borders to resize panes, and scroll chat histories with trackpad momentum.
* **Top Telemetry Header**: Real-time monitor displaying active model (`qwen/qwen3.5-9b`), context budget (`14,820 / 65,536 tokens`), unified RAM breakdown (`11.2 / 18.0 GB - MLX 5.8G · Laya 0.8G · OS 4.6G`), and generation speed (`41.5 t/s`).
* **Interactive Views**:
  - `[1] Daily Ops`: Split view with the Email Triage Queue on top, and Laya Safety Guard + Odysseus Continual Memory feed on bottom.
  - `[2] Memory Vault`: Browse and inspect the 59+ categorized memories across Privacy Tiers (`Private (0700)`, `Project`, `Public`).
  - `[3] Safety Guard`: Deep inspection of protected path shields, trash audit history, and git auto-checkpoints.
* **Hotkeys**:
  - `[j] / [k]` or `[↑] / [↓]`: Navigate table rows.
  - `[1] / [2] / [3]`: Switch view tabs.
  - `[t]`: Dispatch an email triage prompt turn directly to the adjacent OpenCode pane.
  - `[r]`: Refresh data from local stores.
  - `[q]`: Quit cockpit.

