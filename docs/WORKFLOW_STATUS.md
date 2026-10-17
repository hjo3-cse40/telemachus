# Local Agent Workflow: Status & Memory Log

*Last Updated: 2026-10-16*

## Current Status & Configuration

- **Primary Model**: `qwen/qwen3.5-9b` (MLX 4-bit) running via LM Studio at `http://127.0.0.1:1234/v1`
- **Harness**: OpenCode Interpreter (`opencode` CLI v1.18.35)
- **Decision Engine**: `convaiinnovations/laya` (ModernBERT, PyTorch MPS)
- **Retrieval Engine**: `nomic-embed`
- **Hardware Profile**: macOS, 18 GB Unified Memory (strictly budget KV cache & active parameters)

---

## Tooling Implementation Checklist

### Core Domains
- [x] **Email Agent Tool (Shipped to main)**
  - [x] Connect to local Mail / IMAP with app-specific password via TLS
  - [x] Multi-account architecture (`config/accounts.yaml` + private `.env`)
  - [x] Adapt Laya classifier (`convaiinnovations/laya` ModernBERT on MPS) for inbox triage & category scoring
  - [x] Implement safe drafting tool (`Drafts` folder output, no auto-send)
  - [x] Implement offline SQLite indexer (`data/email_index.sqlite3`) for large historical inboxes
  - [x] Wire all tools into OpenCode MCP server (`telemachus-email`) and standalone CLI
- [x] **Computer Filesystem Tool with Laya Guard (Implemented & Tested)**
  - [x] Hook into OpenCode execution cycle / MCP server (`telemachus`)
  - [x] Implement Tier 0 boundary checks (protect `/System`, `~/.ssh`, `~/.gnupg`, foreign `.env`)
  - [x] Implement Tier 1 Intent-Action alignment pass with Laya ModernBERT on MPS
  - [x] Replace hard `rm` with macOS native `trash` (/usr/bin/trash) and audit logging
  - [x] Implement token-bounded navigation (`fs_list`, `fs_read`, `fs_search`, `fs_grep`, `fs_stat`)
  - [x] Implement surgical editing (`fs_edit`) and atomic writes (`fs_write`) with Git auto-checkpoints
  - [x] Pass comprehensive test suite (25 tests covering boundaries, trash, checkpoints, explorer, editor, MCP)
- [ ] **Deterministic Finance Tool**
  - [ ] Setup local SQLite / Plain Text Accounting (ledger/beancount) double-entry storage
  - [ ] Build deterministic balance & cashflow calculator
  - [ ] Connect Qwen 3.5 9B prompt interface for conversational reasoning & forecasting
- [ ] **Anti-Bot Web Search Tool**
  - [ ] Setup stealth headless browser / Chrome DevTools Protocol bridge
  - [ ] Integrate local search aggregator (SearXNG / Brave / DuckDuckGo)
  - [ ] Connect `trafilatura` for clean HTML-to-Markdown distillation

---

## Session Memory & Decision Log

- **Decision 1**: Chose OpenCode CLI as the primary harness instead of continuing monolithic Odysseus development. OpenCode provides an established TUI and standard MCP support while keeping code execution isolated.
- **Decision 2**: Dual-Model approach: Do not burden Qwen 3.5 9B with routing or multi-tool schema overhead. Use Laya (50ms ModernBERT) to filter schemas *before* Qwen's turn, and verify safety *before* execution.
- **Decision 3**: Tuning false positives: Abandon naive keyword blocking. Evaluate Intent-Action Alignment with Laya and use macOS Trash as an undoable safety net for deletions.
- **Decision 4**: Email Multi-Account Strategy: Implemented `config/accounts.yaml` separating non-secret account definitions from private `.env` credentials. Validated Yahoo connection first with read-only IMAP inspections, 2-pass Laya ModernBERT categorization, and safe draft-only generation before adding Gmail accounts.
- **Decision 5**: Offline SQLite Indexer: Rather than feeding hundreds of emails into Qwen's prompt and blowing the 64k context envelope, emails are crawled in batches into local SQLite with Laya ModernBERT classification tags. Qwen queries the index deterministically in under 50 tokens.
- **Decision 6**: 4-Tier Guardrail for Filesystem & Shell: Built `telemachus/fs/` integrating Tier 0 zero-cost boundaries (protecting system roots and credentials like `~/.ssh`), Tier 1 Laya ModernBERT 2-pass intent alignment on MPS, Tier 2 macOS native `/usr/bin/trash` soft recovery, and Tier 3 Git auto-checkpointing and elevation confirmation. All 10 tools (`fs_list`, `fs_read`, `fs_search`, `fs_grep`, `fs_stat`, `fs_edit`, `fs_write`, `fs_delete`, `fs_run`, `fs_checkpoint`) are directly wired into the `telemachus` MCP server for OpenCode.
