"""End-to-End Before & After Benchmark: Ingress Tool Pruning vs. Full Tool Ingestion.

Benchmarks prompt tokens, TTFT latency, and tool selection accuracy:
- BEFORE: Sending all 18 MCP tools directly to LM Studio (localhost:1234)
- AFTER: Sending through Telemachus Ingress Proxy (localhost:1235) with Laya + Nomic pruning
"""
from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import unittest
import urllib.request

from telemachus.ingress.proxy import IngressProxyServer, run_proxy
from telemachus.mcp.server import TOOLS_DEFINITIONS


def mcp_to_openai_tools(mcp_tools: list) -> list:
    out = []
    for t in mcp_tools:
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
            },
        })
    return out


class TestIngressBenchmark(unittest.TestCase):
    server: IngressProxyServer = None
    server_thread: threading.Thread = None

    @classmethod
    def setUpClass(cls):
        # Start Ingress Proxy on port 1235 in background thread
        cls.server = run_proxy(listen_host="127.0.0.1", listen_port=1235, upstream_host="127.0.0.1", upstream_port=1234)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()

    def _execute_turn(self, endpoint_url: str, prompt: str, tools: list) -> dict:
        """Executes a single LLM turn and measures prompt tokens and elapsed latency."""
        payload = {
            "model": "qwen/qwen3.5-9b",
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 40,
            "temperature": 0.0,
        }
        if tools:
            payload["tools"] = tools

        req = urllib.request.Request(
            endpoint_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        elapsed = time.perf_counter() - t0

        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        tool_calls = msg.get("tool_calls", [])
        tool_name = tool_calls[0]["function"]["name"] if tool_calls else "NO_TOOL_CALL"
        usage = data.get("usage", {})

        return {
            "elapsed_sec": elapsed,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "called_tool": tool_name,
            "response_snippet": msg.get("content", "")[:60] if msg.get("content") else "",
        }

    def test_before_and_after_comparison(self):
        """Runs side-by-side benchmark comparing unpruned vs. pruned performance."""
        test_cases = [
            {
                "id": "EMAIL_TURN",
                "prompt": "Check my inbox status and list any unread emails.",
                "expected_tool_prefix": "email_",
            },
            {
                "id": "FS_TURN",
                "prompt": "Search for files named README or config in the current directory.",
                "expected_tool_prefix": "fs_",
            },
            {
                "id": "GENERAL_TURN",
                "prompt": "Explain the difference between TCP and UDP in one sentence.",
                "expected_tool_prefix": "NO_TOOL_CALL",
            },
        ]

        benchmark_results = []
        direct_url = "http://127.0.0.1:1234/v1/chat/completions"
        proxy_url = "http://127.0.0.1:1235/v1/chat/completions"

        print("\n" + "=" * 78)
        print("   TELEMACHUS INGRESS PROXY: BEFORE vs. AFTER PERFORMANCE BENCHMARK")
        print("=" * 78)

        all_tools = mcp_to_openai_tools(TOOLS_DEFINITIONS)

        for case in test_cases:
            prompt = case["prompt"]
            cid = case["id"]
            print(f"\n[Scenario: {cid}] Prompt: \"{prompt}\"")

            # 1. BEFORE: Direct to LM Studio with all 18 tools
            print("  Running BEFORE (18 tools unpruned)...")
            res_before = self._execute_turn(direct_url, prompt, all_tools)

            # 2. AFTER: Via Ingress Proxy (pruned by Laya + Nomic)
            print("  Running AFTER  (Ingress proxy pruned)...")
            res_after = self._execute_turn(proxy_url, prompt, all_tools)

            tokens_saved = res_before["prompt_tokens"] - res_after["prompt_tokens"]
            pct_saved = (tokens_saved / max(1, res_before["prompt_tokens"])) * 100
            latency_diff = res_before["elapsed_sec"] - res_after["elapsed_sec"]
            speedup = res_before["elapsed_sec"] / max(0.001, res_after["elapsed_sec"])

            row = {
                "id": cid,
                "prompt": prompt,
                "before_tokens": res_before["prompt_tokens"],
                "after_tokens": res_after["prompt_tokens"],
                "tokens_saved": tokens_saved,
                "pct_saved": round(pct_saved, 1),
                "before_sec": round(res_before["elapsed_sec"], 2),
                "after_sec": round(res_after["elapsed_sec"], 2),
                "speedup": round(speedup, 2),
                "before_tool": res_before["called_tool"],
                "after_tool": res_after["called_tool"],
            }
            benchmark_results.append(row)

            print(f"  --> Prompt Tokens: {row['before_tokens']} -> {row['after_tokens']} (-{row['pct_saved']}%)")
            print(f"  --> Latency:       {row['before_sec']}s -> {row['after_sec']}s ({row['speedup']}x faster)")
            print(f"  --> Tool Choice:   Before='{row['before_tool']}' | After='{row['after_tool']}'")

        print("\n" + "=" * 78)
        print("SUMMARY TABLE:")
        print(f"{'Scenario':<14} | {'Before Tok':<10} | {'After Tok':<10} | {'Tokens Saved':<12} | {'Speedup':<8}")
        print("-" * 78)
        for r in benchmark_results:
            print(f"{r['id']:<14} | {r['before_tokens']:<10} | {r['after_tokens']:<10} | {r['pct_saved']}% ({r['tokens_saved']}) | {r['speedup']}x")
        print("=" * 78)

        # Assertions
        for r in benchmark_results:
            # Token reduction must be at least 40% across all scenarios
            self.assertGreaterEqual(r["pct_saved"], 40.0)


if __name__ == "__main__":
    unittest.main()
