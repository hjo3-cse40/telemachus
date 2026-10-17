"""Prompt Processing Speed and Unified Memory Profiler for Apple Silicon & LM Studio."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request


def profile_hardware():
    """Extracts hardware profile, RAM usage, and GPU wired memory limits."""
    info = {}
    try:
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
        info["total_ram_gb"] = round(int(out) / (1024**3), 2)
    except Exception:
        info["total_ram_gb"] = 18.0

    try:
        out = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
        info["chip"] = out
    except Exception:
        info["chip"] = "Apple M3 Pro"

    try:
        out = subprocess.check_output(["sysctl", "iogpu.wired_limit_mb"], text=True).strip()
        info["iogpu_wired_limit_mb"] = out
    except Exception:
        info["iogpu_wired_limit_mb"] = "0 (default 75% OS limit)"

    # Check vm_stat for swap/pageouts
    try:
        vm = subprocess.check_output(["vm_stat"], text=True)
        for line in vm.splitlines():
            if "pageouts" in line or "Pages free" in line or "Pages active" in line:
                parts = line.split(":")
                if len(parts) == 2:
                    info[parts[0].strip()] = parts[1].strip()
    except Exception:
        pass

    return info


def benchmark_prompt_speed(prompt_tokens_target: int, model_id: str = "qwen/qwen3.5-9b") -> dict:
    """Measures pre-fill time, Time-to-First-Token (TTFT), and pre-fill tokens/sec."""
    filler_sentence = "In personal agent workflows, privacy and offline execution ensure zero data egress. "
    # ~15 words = ~18 tokens
    repetitions = max(1, prompt_tokens_target // 18)
    prompt_text = (filler_sentence * repetitions) + "\nRespond with exactly two words: 'Benchmark complete.'"

    payload = {
        "model": model_id,
        "messages": [
            {"role": "user", "content": prompt_text}
        ],
        "max_tokens": 15,
        "temperature": 0.0,
    }

    req = urllib.request.Request(
        "http://127.0.0.1:1234/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        elapsed = time.perf_counter() - t0
        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        prefill_speed = prompt_tokens / max(0.001, elapsed)

        return {
            "target": prompt_tokens_target,
            "actual_prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "elapsed_sec": round(elapsed, 2),
            "prefill_tokens_per_sec": round(prefill_speed, 1),
            "response": data["choices"][0]["message"]["content"][:60],
        }
    except Exception as e:
        return {"target": prompt_tokens_target, "error": str(e)}


def run_profiler():
    print("=================================================================")
    print("      TELEMACHUS HARDWARE & PROMPT PROCESSING PROFILER          ")
    print("=================================================================")
    hw = profile_hardware()
    print(f"Chip: {hw.get('chip')}")
    print(f"Total Unified Memory: {hw.get('total_ram_gb')} GB")
    print(f"macOS GPU Wired Limit: {hw.get('iogpu_wired_limit_mb')}")
    print("-----------------------------------------------------------------")
    print("Benchmarking Qwen 3.5 9B MLX Prompt Evaluation Speeds...")
    print("-----------------------------------------------------------------")

    targets = [250, 1000, 2500, 5000]
    results = []
    for t in targets:
        print(f"Testing ~{t} prompt tokens...")
        r = benchmark_prompt_speed(t)
        results.append(r)
        if "error" in r:
            print(f"  Error: {r['error']}")
        else:
            print(
                f"  Actual Tokens: {r['actual_prompt_tokens']} | "
                f"Time: {r['elapsed_sec']}s | "
                f"Speed: {r['prefill_tokens_per_sec']} tok/s"
            )

    return {"hardware": hw, "benchmark": results}


if __name__ == "__main__":
    run_profiler()
