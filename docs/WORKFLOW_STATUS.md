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
- [ ] **Email Agent Tool**
  - [ ] Connect to local Mail / IMAP with app-specific password or Apple Mail bridge
  - [ ] Adapt Laya classifier from `email_laya_review.py` for inbox triage & spam scoring
  - [ ] Implement safe drafting tool (`Drafts` folder output, no auto-send)
- [ ] **Computer Filesystem Tool with Laya Guard**
  - [ ] Hook into OpenCode execution cycle / MCP proxy
  - [ ] Implement Tier 0 boundary checks (protect `/System`, `~/.ssh`, etc.)
  - [ ] Implement Tier 1 Intent-Action alignment pass with Laya
  - [ ] Replace hard `rm` with macOS `trash`
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
