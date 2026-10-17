# Local Agent Workflow: System Architecture

## 1. Executive Summary & Objective

This project establishes a completely private, local AI agent workflow designed for an **18 GB unified memory macOS workstation (Apple Silicon)**.

Rather than relying on closed cloud models for sensitive personal and system data, this workflow pairs:
1. **Primary Reasoning & Generation Engine**: `qwen/qwen3.5-9b` (4-bit MLX, up to 64k context window) served locally (e.g. via LM Studio or MLX local endpoint on `http://127.0.0.1:1234/v1`).
2. **Decision, Routing & Safety Model**: **Laya** (`convaiinnovations/laya`, ~421M ModernBERT-based classifier/router, ~50–100 ms inference on MPS/CPU, ~800MB footprint).
3. **Execution Harness**: **OpenCode Interpreter** (`opencode` CLI) configured with MCP (Model Context Protocol) and local provider hooks.
4. **Instant Retrieval Engine**: `nomic-embed` vector index for zero-latency local context lookups.

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
                  |     Prompt + Pruned Context + Retrival       |
                  +------------------------+---------------------+
                                           |
                                           v
                  +----------------------------------------------+
                  |            QWEN 3.5 9B (MLX 4-bit)           |
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

---

## 2. Hardware Budget & Memory Allocations (18 GB Mac)

On Apple Silicon with 18 GB unified memory, memory headroom is strict. OS and window server consume ~2.5–3.5 GB. 

| Component | Architecture / Format | Memory Footprint | Role |
| :--- | :--- | :--- | :--- |
| **macOS & System Display** | System reserved | ~3.0 GB | WindowServer, OS buffers |
| **Qwen 3.5 9B Weights** | 4-bit MLX (`qwen3.5-9b`) | ~5.6 GB | General language & agentic reasoning |
| **KV Cache (Dynamic)** | 4-bit / 8-bit cache (up to 64k) | ~4.0 – 6.5 GB | Conversation history & loaded documents |
| **Laya Decision Model** | ModernBERT (~421M params, PyTorch MPS) | ~0.8 GB | Ingress routing, tool selection, egress guard |
| **nomic-embed** | Lightweight local embedding | ~0.3 GB | Fast semantic vector search & retrieval |
| **OpenCode CLI & Runtime** | Node/Bun + Python MCP processes | ~0.4 GB | CLI harness, session management |
| **Total Estimated Peak** | | **~14.5 – 16.5 GB** | **Fits safely inside 18 GB with zero swap thrashing** |

### Critical Rule for Context & Tool Schemas:
Loading 30+ full MCP tool JSON schemas into Qwen's prompt consumes **3,000–6,000 tokens per turn**. On a 9B model, this degrades tool-calling accuracy and bloats the KV cache.
**Laya's primary job is Ingress Pruning**: Laya decides which 1–3 tools are actually needed for the current prompt turn, injecting only those schemas into OpenCode.

---

## 3. Why OpenCode Interpreter over Odysseus?

- **Streamlined CLI UX**: OpenCode provides a battle-tested TUI, interactive session resume (`-c`), fork (`--fork`), ACP (Agent Client Protocol), and native MCP client support.
- **Provider Agnostic**: OpenCode connects cleanly to LM Studio / local OpenAI-compatible endpoints with configurable context and sampling parameters (configured in `~/.config/opencode/opencode.jsonc`).
- **Separation of Concerns**: Odysseus was a full monolithic environment; OpenCode serves strictly as the orchestration and tool execution runtime, letting Laya handle the cognitive guardrails and routing.
