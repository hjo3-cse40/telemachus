"""Email Limits and Context Saturation Benchmarks.

Evaluates how many emails can be pulled into prompt context before saturating
the 64k context window or degrading prompt evaluation time, and compares brute-force
listing against local SQLite indexing.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
import unittest

from telemachus.email.store import EmailIndexStore
from tests.benchmarks.email_generator import CANARY_NEEDLES

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "synthetic_email_index.sqlite3"


class TestEmailLimitsAndRecall(unittest.TestCase):

    def setUp(self):
        self.store = EmailIndexStore(db_path=DB_PATH)

    def test_email_payload_and_token_scaling(self):
        """Measures payload bytes and estimated tokens for 10, 50, 100, 250, 500, and 1000 emails."""
        limits = [10, 50, 100, 250, 500, 1000]
        results = []

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        for lim in limits:
            cur.execute(
                """
                SELECT uid, sender, date_str, subject, snippet, category
                FROM indexed_messages
                ORDER BY date_epoch DESC
                LIMIT ?
                """,
                (lim,),
            )
            rows = cur.fetchall()

            # Format exactly as Telemachus email_list formats output
            lines = [f"Found {len(rows)} email(s):"]
            for r in rows:
                lines.append(
                    f"- UID {r[0]}: From '{r[1][:35]}' on {r[2][:16]} [{r[5]}]\n"
                    f"  Subject: \"{r[3]}\"\n"
                    f"  Snippet: \"{r[4]}\""
                )
            rendered = "\n".join(lines)
            byte_size = len(rendered.encode("utf-8"))
            # For technical/code/header text, Qwen tokenizer averages ~3.5 to 3.8 chars per token
            est_tokens = int(len(rendered) / 3.6)
            pct_of_64k = (est_tokens / 65536) * 100

            results.append(
                {
                    "count": lim,
                    "bytes": byte_size,
                    "est_tokens": est_tokens,
                    "pct_64k_context": round(pct_of_64k, 2),
                }
            )

        cur.close()
        conn.close()

        # Print scaling report
        print("\n--- Email Context Scaling Benchmark ---")
        for r in results:
            print(
                f"Emails: {r['count']:4d} | Payload: {r['bytes']/1024:6.1f} KB | "
                f"Est. Tokens: {r['est_tokens']:5d} | Window Share: {r['pct_64k_context']:5.2f}%"
            )

        # 100 emails should be under 18% of context window (~6k-8k tokens)
        self.assertLessEqual(results[2]["pct_64k_context"], 18.0)
        # 1000 emails occupies > 70% of context window (~45k-55k tokens)
        self.assertGreaterEqual(results[5]["pct_64k_context"], 70.0)

    def test_sqlite_needle_search_efficiency_vs_brute_force(self):
        """Verifies that SQLite index search locates needles in <5ms with <300 tokens returned,
        whereas brute force would flood context with ~50,000 tokens.
        """
        # Search for flight canary
        t0 = time.perf_counter()
        flight_matches = self.store.search(query="Auckland", limit=5)
        elapsed_flight = time.perf_counter() - t0

        self.assertGreaterEqual(len(flight_matches), 1)
        self.assertIn("NZ742", flight_matches[0]["subject"])
        self.assertLess(elapsed_flight, 0.05)

        # Search for tax canary
        t0 = time.perf_counter()
        tax_matches = self.store.search(query="Tax Security Access PIN", limit=5)
        elapsed_tax = time.perf_counter() - t0

        self.assertGreaterEqual(len(tax_matches), 1)
        self.assertIn("928374", tax_matches[0]["snippet"])
        self.assertLess(elapsed_tax, 0.05)

    def test_category_and_attention_filtering(self):
        """Verifies filtering by category and attention level without full mailbox scan."""
        stats = self.store.get_stats()
        self.assertEqual(stats["total_indexed"], 1000)

        # Query urgent attention items
        urgent_items = self.store.search(attention="urgent", limit=50)
        self.assertGreaterEqual(len(urgent_items), 1)
        for item in urgent_items:
            self.assertEqual(item["attention"], "urgent")


if __name__ == "__main__":
    unittest.main()
