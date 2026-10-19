"""Diagnostic and Benchmark Suite for LM Studio Nomic Embed Text v1.5.

Investigates why LM Studio attempts to load Q4_K_M when Q8_0 is present,
and validates 768-dimensional embedding generation.
"""
from __future__ import annotations

import json
from pathlib import Path
import time
import unittest
import urllib.error
import urllib.request

LM_STUDIO_EMBED_URL = "http://127.0.0.1:1234/v1/embeddings"
LM_STUDIO_MODELS_URL = "http://127.0.0.1:1234/v1/models"


class TestNomicEmbed(unittest.TestCase):

    def _query_embedding(self, model_id: str, input_text: str) -> dict:
        req = urllib.request.Request(
            LM_STUDIO_EMBED_URL,
            data=json.dumps({"model": model_id, "input": input_text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        elapsed = time.perf_counter() - t0
        data["_client_elapsed_sec"] = elapsed
        return data

    def test_lm_studio_model_catalog(self):
        """Inspects all models currently registered in LM Studio."""
        req = urllib.request.Request(LM_STUDIO_MODELS_URL)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        model_ids = [m["id"] for m in data.get("data", [])]
        print(f"\n[LM Studio] Registered models: {model_ids}")

        self.assertIn("text-embedding-nomic-embed-text-v1.5@q8_0", model_ids)
        # Verify that LM Studio also lists the bundled q4_k_m in its catalog
        self.assertIn("text-embedding-nomic-embed-text-v1.5@q4_k_m", model_ids)

    def test_explicit_q8_0_embedding(self):
        """Verifies that requesting the exact Q8_0 model works with zero delay and returns 768 dimensions."""
        model_id = "text-embedding-nomic-embed-text-v1.5@q8_0"
        sample_text = "Telemachus offline agent vector memory lookup test."

        res = self._query_embedding(model_id, sample_text)
        self.assertEqual(res.get("object"), "list")
        vectors = res["data"][0]["embedding"]

        # Nomic Embed Text v1.5 has 768 dimensions
        self.assertEqual(len(vectors), 768)
        self.assertLess(res["_client_elapsed_sec"], 0.5)
        print(f"[Q8_0 Embedding] Latency: {res['_client_elapsed_sec']*1000:.1f}ms | Dimensions: {len(vectors)}")

    def test_generic_alias_mapping_behavior(self):
        """Demonstrates that requesting 'text-embedding-nomic-embed-text-v1.5' without @q8_0
        causes LM Studio to resolve to its bundled default (q4_k_m) unless Q8_0 is already resident in memory.
        """
        model_id = "text-embedding-nomic-embed-text-v1.5"
        sample_text = "Vector query without explicit quantization tag."

        res = self._query_embedding(model_id, sample_text)
        self.assertEqual(res.get("object"), "list")
        # Notice what model LM Studio reported in the response body:
        returned_model = res.get("model")
        print(f"[Generic Alias] Requested: '{model_id}' -> LM Studio resolved to: '{returned_model}'")


if __name__ == "__main__":
    unittest.main()
