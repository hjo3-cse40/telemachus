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

- [ ] **1. Emails**:
  - Connects locally to your mailbox (Apple Mail bridge / direct IMAP).
  - Uses Laya triage to classify spam, newsletters, and priority threads.
  - Automatically drafts replies directly to your `Drafts` folder for review.
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

### 2. Verify OpenCode Configuration
Your configuration is located at `~/.config/opencode/opencode.jsonc`:
```bash
opencode --version
opencode models lmstudio
```

### 3. Launch Telemachus
Start OpenCode in this workspace:
```bash
opencode
```
To resume the previous session:
```bash
opencode -c
```
