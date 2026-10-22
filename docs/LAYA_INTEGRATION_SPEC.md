# Laya Decision Model & Guardrail Specification

## 1. Role of Laya (`convaiinnovations/laya`)

In this architecture, **Qwen 3.5 9B** is the generative reasoning engine, and **Laya** (~421M ModernBERT classifier/router) is the fast deterministic decision coprocessor.

### Why Offload to Laya?
1. **Speed & Latency**: Laya executes in **50–100 ms** on Apple Silicon (MPS / CPU), compared to 1–4 seconds for Qwen 9B to generate multi-token reasoning for simple yes/no or routing decisions.
2. **Context Window & KV Cache Preservation**: Passing 30+ MCP tool schemas into Qwen's prompt floods the context window with 3,000–6,000 tokens per turn. Laya prunes the toolset before Qwen ever sees the prompt.
3. **Deterministic Bias Reduction**: Laya uses bidirectional option-order de-biasing, making classification stable and immune to prompt drift or sycophancy.

---

## 2. Dual-Pass Pipeline: Ingress & Egress

### A. Ingress (Pre-Prompt Routing)
When a user prompt enters OpenCode:
1. **Laya Router** evaluates the user input against tool domains:
   - `EMAIL`: Inbox scanning, priority triaging, drafting.
   - `FILESYSTEM`: File inspection, edits, shell execution.
   - `FINANCE`: Arithmetic ledger, balance verification, forecasts.
   - `WEB_SEARCH`: Query synthesis, live browsing, anti-bot fetching.
2. **Dynamic Tool Pruning**: OpenCode is fed *only* the tool schemas matching the classified domains. The rest are hidden from the prompt.
3. **Retrieval Trigger**: If historical memory or specific docs are needed, Laya signals `nomic-embed` to fetch top-k chunks into the system prompt.

### B. Egress (Pre-Execution Safety Gating)
Before any tool or bash command is executed on the local system:
1. OpenCode intercepts the tool name, arguments, and command string.
2. Laya evaluates the proposed action against the original user prompt.

---

## 3. Solving the False-Positive Dilemma for System Safety

### The Problem: Why Traditional Guardrails Fail
- **Naive Regex / String Matching**: Blocks `rm -rf build/` or `rm -rf dist/` even when the user literally said "delete the build folder".
- **Pure LLM Prompts ("Is this command safe?")**: Overly conservative; LLMs see `rm`, `truncate`, or `kill` and panic, hallucinating risk even on intentional actions.

### The Solution: The 3-Tier "Intent-Action Alignment" Tuning Strategy

```
                          [ Proposed Command / Action ]
                                       |
                                       v
               +-----------------------------------------------+
               |     TIER 0: HARD BOUNDARY CHECKS (Zero Cost)  |
               | - Target path outside home/workspace?         |
               | - Modifying /System, /usr, /bin, /etc, ~/.ssh?|
               +-----------------------+-----------------------+
                                       |
                     +-----------------+-----------------+
                     | Safe Boundary                     | System Boundary Violation
                     v                                   v
        +----------------------------+            +--------------------+
        | TIER 1: LAYA INTENT MATCH  |            |   IMMEDIATE BLOCK  |
        | Does action match prompt?  |            +--------------------+
        +--------------+-------------+
                       |
        +--------------+--------------+
        | High Match                  | Low Match / Ambiguous
        v                             v
+------------------------+    +-------------------------------+
| TIER 2: SOFT RECOVERY  |    | TIER 3: INTERACTIVE ELEVATION |
| Execute using `trash`  |    | Prompt user confirmation in   |
| instead of unbacked rm |    | OpenCode TUI with exact diff  |
+------------------------+    +-------------------------------+
```

### 1. Tier 0: Blast-Radius & Boundary Constraints (Deterministic)
- **Hard Whitelist / Sandbox**: Within project directories (`~/code/*`, `~/scratch/*`), destructive actions have low consequence.
- **Hard Blacklist (No Exceptions)**:
  - System roots: `/`, `/System`, `/usr`, `/Library`, `/private/etc`
  - Secrets & Credentials: `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.config/opencode/*.dat`, `.env` files outside active workspace.
  - Dangerous commands: `mkfs`, `dd if=`, `format`, `chmod -R 777 /`.

### 2. Tier 1: Semantic Intent-Action Alignment (Laya Classification)
Instead of asking Laya *"Is this action dangerous?"*, we formulate a 2-way question:
- **Question 1**: *"Does the proposed action directly fulfill the user's explicit request?"*
- **Question 2**: *"Does the action affect files or resources outside what the user requested?"*

**Scenarios**:
- User: *"Clean up the test png files in my downloads folder"*  
  Command: `trash ~/Downloads/test_*.png`  
  $\rightarrow$ Intent match = High (0.95), Outside scope = Low (0.05). **Result: AUTO-PERMIT**.
- User: *"Format the table in notes.txt"*  
  Command: `rm notes.txt`  
  $\rightarrow$ Intent match = Low (0.12), Outside scope = High. **Result: ALERT & BLOCK**.

### 3. Tier 2: The macOS `trash` Safety Net (Eliminate Loss Risk)
On macOS, permanent deletion via `rm -rf` cannot be undone. 
- Replace `rm` in agent tool execution with `trash` (via macOS native `osascript` or `trash-cli`).
- If an intentional file deletion occurs, files are moved to the user's macOS Trash bin. If a mistake ever slips past, the user can restore it with Cmd+Z or via Trash.

### 4. Tier 3: Selective User Confirmation (Only on Discrepancies)
If Laya's alignment confidence is between $0.4$ and $0.8$:
- OpenCode pauses and prompts the user:
  `[Laya Alert] Qwen proposed: 'rm -f database.sqlite3'. Your prompt was: 'Reset the demo data'. Confirm (y/n)?`
- This ensures false alarms only appear when there is genuine semantic ambiguity.
