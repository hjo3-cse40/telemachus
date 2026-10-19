# Local Agent Workflow: Tooling Roadmap & Recommendations

This document defines the implementation strategy for the 4 core local tools requested, plus recommended high-leverage expansions and skills to minimize cognitive load on local models.

---

## 1. Core Tool 1: Emails (Private, On-Device Triage & Drafting)

### Requirements
- Look through inbox without exposing credentials or message contents to external clouds.
- Filter spam / marketing newsletters.
- Prioritize important messages (threads requiring user response, urgent flags).
- Draft email replies for human review (never auto-send).

### Technical Implementation
- **Transport**: Local IMAP/JMAP bridge with TLS directly to mail provider (e.g. Apple Mail local SQLite/AppleScript integration, or direct IMAP with app-specific password stored in macOS Keychain).
- **Laya Integration**: Adapt the proven logic from `email_laya_review.py` (`classify_record`, priority scoring) to score incoming subjects and snippets.
- **Tools Exposed to OpenCode**:
  1. `email_list_unread(limit=20, folder="INBOX")`
  2. `email_get_thread(message_id)`
  3. `email_triage_inbox(filter_spam=True)`: Runs Laya pass over unread headers, sorts into `Urgent`, `Action Required`, `Newsletter`, `Spam`.
  4. `email_create_draft(to, subject, body_markdown, in_reply_to=None)`: Writes directly to `Drafts` folder. User sends via their mail client.

---

## 2. Core Tool 2: Computer Files (Full Access + Laya Guardrail)

### Requirements
- Full read/write/inspect capability across the computer.
- Safe execution: prevent catastrophic deletions or accidental damage.
- Minimize false positives so normal file operations aren't blocked.

### Technical Implementation
- **OpenCode Builtins + Guard Middleware**:
  - OpenCode's native file inspection (`read_file`, `write_file`, `list_directory`).
  - Shell execution wrapped in the **Laya Egress Guard** (described in [LAYA_INTEGRATION_SPEC.md](./LAYA_INTEGRATION_SPEC.md)).
- **Key Enhancements**:
  1. **`trash` substitution**: Replaces permanent `rm` commands with system Trash moves.
  2. **Git Auto-Checkpointing**: Before large file operations in code repositories, take a lightweight git stash or temporary commit (`git commit -m "Auto-checkpoint before agent turn"`).
  3. **Protected Path Shield**: Deterministic block on `/System`, `/Library/Preferences`, `~/.ssh`, `~/.gnupg`.

---

## 3. Core Tool 3: Finances (Deterministic Math + LLM Planning)

### Requirements
- Keep accurate number counts and financial records.
- Prevent LLM arithmetic hallucinations (rounding errors, ledger drift).
- Reason and infer future projections (savings rate, runway, expense trends).

### Technical Implementation: The Dual-Layer Finance Engine
LLMs should **never** do raw accounting math. Instead:
- **Layer 1: Deterministic Engine (Plain Text Accounting / SQLite)**
  - Backed by SQLite or standard Plain Text Accounting (e.g., `beancount` or `hledger`).
  - Strict double-entry ledger bookkeeping.
  - Generates exact balance sheets, category breakdowns, and totals down to the cent.
- **Layer 2: Qwen 3.5 9B Inference & Planning**
  - Qwen consumes the pre-calculated deterministic tables.
  - Generates insights: *"You spent 18% more on dining this month than average; if you cut that by half, your 6-month emergency fund goal will be reached 3 weeks sooner."*
- **Tools Exposed to OpenCode**:
  1. `finance_log_entry(date, account, amount, category, memo)`
  2. `finance_get_balance(account, as_of_date)`
  3. `finance_report_summary(start_date, end_date, group_by="category")`
  4. `finance_forecast(monthly_savings, annual_growth_rate, years)`

---

## 4. Core Tool 4: Web Search (Anti-Bot Resilient Browsing)

### Requirements
- Reliable web retrieval for up-to-date documentation and information.
- Bypasses Cloudflare, Akamai, and anti-bot blocks that plague standard AI web scrapers.
- Clean text extraction to prevent flooding Qwen's context with 50,000 tokens of raw HTML.

### Technical Implementation
- **Anti-Bot Defense Strategy**:
  1. **Local Browser Bridge (Chrome/Brave/Safari Remote Debugging)**:
     - Run a local headless browser using Playwright with `playwright-stealth` OR connect directly to an existing browser instance via Chrome DevTools Protocol (`--remote-debugging-port=9222`).
     - Reuses real browser TLS fingerprints and active session cookies.
  2. **Search Aggregator**:
     - Local SearXNG instance or Brave Search API / DuckDuckGo HTML scraper with desktop User-Agents.
  3. **Content Distillation (Token Saver)**:
     - Use `trafilatura` or `readability-lxml` to strip scripts, styles, navigation bars, and cookie banners.
     - Returns concise, dense Markdown (~500–1,500 tokens instead of 40,000 HTML tokens), preserving Qwen's KV cache.
- **Tools Exposed to OpenCode**:
  1. `web_search(query, max_results=5)`
  2. `web_fetch_page(url, extract_markdown=True)`

---

## 5. Core Tool 5: Job Application Tracker & Autofill Hub (Future Domain)

### Requirements
- Connect seamlessly to `~/task-tracker.csv`.
- Track items, status, and priority across pipeline stages.
- Auto-sync recruiter / platform emails (Ashby, Greenhouse, Lever, Indeed) directly to job status.
- Parse job descriptions, tailor answers using verified project stories (SemiAI CRM, Airlock harness, Argus), and freeze PDFs in `resumes/by-application/`.

### Tools Exposed to OpenCode
1. `job_list_active(status_filter=None)`
2. `job_update_status(company, role, new_status, notes=None)`
3. `job_tailor_application(jd_url_or_text, target_resume="Documents/Resumes/resume.tex")`
4. `job_stage_dossier(company, role, answers_dict)`

---

## 6. Telemachus Terminal Cockpit & Interactive Station (UI Roadmap)

### Architecture
- **Harness**: Native OpenCode interactive CLI session in primary right pane.
- **Left Panel (Textual TUI)**: Dual-pane dashboard for Email Triage and Laya Safety / Trash monitor.
- **Top Unified Telemetry Bar**: Single header line reporting:
  - Active Model Loaded (e.g. `Qwen 3.5 9B MLX 4-bit`)
  - Context Window Usage (e.g. `14,820 / 65,536 tokens`)
  - Unified Memory Allocation (e.g. `RAM: 11.2 / 18.0 GB - MLX 5.8G | Laya 0.8G | OS 4.6G`)
  - Generation Speed (e.g. `41.5 t/s`)
- **Interaction**: Mouse mode enabled (`set -g mouse on` in tmux) for clickable table rows, border dragging, and trackpad scrolling, with an electric cyan/neon glowing prompt cursor.

---

## 7. Recommended Additional Tools & Skills to Consider

To further reduce manual work and keep local model overhead minimal:

1. **Local Calendar & Reminder Bridge**:
   - Query upcoming events and create reminders locally via macOS EventKit / AppleScript.
2. **Context Compression Skill (`/compact` style)**:
   - When a session approaches 32k tokens, trigger a background summarization pass so the KV cache doesn't push the 18GB memory envelope into swap.
3. **Local Clipboard Sync (`pbcopy` / `pbpaste`)**:
   - Allows the agent to ingest whatever is on your clipboard or write drafted responses directly to clipboard for quick paste.
4. **Git Safe-Branching Automation**:
   - Whenever working on code repositories, automatically ensure work happens on a feature branch, never modifying `main` directly.

