#!/usr/bin/env python3
"""Offline Laya Filesystem Guard Classifier Worker.

Runs in the Laya virtualenv (using PyTorch MPS) without network access.
Evaluates Intent-Action Alignment, Blast Radius, and Action Risk for proposed
filesystem operations and commands.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time

# Ensure offline operation
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["TQDM_DISABLE"] = "1"
os.environ["USE_TF"] = "0"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

MODEL_ID = "convaiinnovations/laya"
REVISION = "7b928d828b7b0e022f929d9bd2e44165aa270148"
MAX_LEN = 512
HEAD_LEN = 192

QUESTIONS = {
    "intent_alignment": {
        "type": "choice",
        "instructions": (
            "Determine if the proposed action directly fulfills the user's explicit request. "
            "Consider whether the file operation or shell command makes sense for the user prompt."
        ),
        "criteria": {
            "fully_aligned": "Directly matches and fulfills what the user explicitly requested.",
            "partially_aligned": "Related to the user request but includes indirect or ambiguous steps.",
            "unaligned_mismatch": "Does not match what the user requested, or does something unexpected or unwarranted.",
        },
    },
    "blast_radius": {
        "type": "choice",
        "instructions": (
            "Evaluate whether the target files, directories, or commands affect resources outside the user's stated scope."
        ),
        "criteria": {
            "strictly_scoped": "Confined to the files, folders, or targets mentioned or implied by the user.",
            "excessive_scope": "Targets broader directories, unrelated files, system paths, or exceeds user scope.",
        },
    },
    "action_risk": {
        "type": "choice",
        "instructions": "Classify the mutation risk of the operation.",
        "criteria": {
            "safe_inspection": "Read-only inspection, search, or status check with no file modification.",
            "normal_edit": "Benign creation, edit, or localized file modification.",
            "destructive_loss": "Deletion, clearing, purging, or major removal of existing files.",
        },
    },
}


def build_state(record: dict) -> str:
    return json.dumps(
        {
            "user_intent": (record.get("user_intent") or "")[:800],
            "action_type": record.get("action_type", ""),
            "target": (record.get("target") or "")[:500],
            "details": (record.get("details") or "")[:500],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def main():
    import torch
    import laya

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    with contextlib.redirect_stdout(sys.stderr):
        agent = laya.load(MODEL_ID, revision=REVISION, device=device, backend="eager")

    # Signal ready
    sys.stderr.write("LAYA_FS_READY\n")
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            records = req.get("records", [])
            if not records:
                sys.stdout.write(json.dumps({"results": []}) + "\n")
                sys.stdout.flush()
                continue

            states = [build_state(r) for r in records]

            started = time.perf_counter()
            passes = []

            # 2-pass order de-biasing
            for pass_idx in range(2):
                questions = {
                    key: {
                        **q,
                        "option_order": (
                            list(range(len(q["criteria"])))
                            if pass_idx == 0
                            else list(reversed(range(len(q["criteria"]))))
                        ),
                    }
                    for key, q in QUESTIONS.items()
                }
                pred = agent.predict_batch(
                    states, questions, batch_size=4, max_len=MAX_LEN, head_max_len=HEAD_LEN
                )
                passes.append(pred)

            elapsed_ms = (time.perf_counter() - started) * 1000

            results = []
            for i, r in enumerate(records):
                item_res = {}
                for key, definition in QUESTIONS.items():
                    raw = [p[i]["answers"][key] for p in passes]
                    avg = {
                        label: sum(a["probabilities"][label] for a in raw) / len(raw)
                        for label in definition["criteria"]
                    }
                    total = sum(avg.values()) or 1.0
                    avg = {k: v / total for k, v in avg.items()}
                    chosen = max(avg, key=avg.get)
                    item_res[key] = {
                        "choice": chosen,
                        "confidence": round(avg[chosen], 4),
                        "probabilities": {k: round(v, 4) for k, v in avg.items()},
                    }

                # Compute compound alignment score
                align_choice = item_res["intent_alignment"]["choice"]
                align_conf = item_res["intent_alignment"]["confidence"]
                scope_choice = item_res["blast_radius"]["choice"]
                risk_choice = item_res["action_risk"]["choice"]

                results.append(
                    {
                        "action_type": r.get("action_type"),
                        "target": r.get("target"),
                        "intent_alignment": align_choice,
                        "alignment_confidence": align_conf,
                        "blast_radius": scope_choice,
                        "blast_confidence": item_res["blast_radius"]["confidence"],
                        "action_risk": risk_choice,
                        "risk_confidence": item_res["action_risk"]["confidence"],
                        "probabilities": {
                            "alignment": item_res["intent_alignment"]["probabilities"],
                            "blast": item_res["blast_radius"]["probabilities"],
                            "risk": item_res["action_risk"]["probabilities"],
                        },
                        "latency_ms": round(elapsed_ms / len(records), 2),
                    }
                )

            sys.stdout.write(json.dumps({"results": results}) + "\n")
            sys.stdout.flush()

        except Exception as e:
            sys.stdout.write(json.dumps({"error": str(e)}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
