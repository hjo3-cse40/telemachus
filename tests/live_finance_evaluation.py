"""Live verification of Financial MCP tools and Qwen 3.5 9B reasoning against Ground Truth."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import urllib.request

# Create test sandbox and set environment variable BEFORE importing telemachus modules
test_dir = tempfile.mkdtemp(prefix="telemachus_ground_truth_test_")
os.environ["TELEMACHUS_FINANCE_DIR"] = test_dir

from telemachus.finance.models import Posting, Transaction
from telemachus.finance.recurring import RecurringEngine
from telemachus.finance.setup import apply_setup_yaml
from telemachus.finance.store import FinanceStore
from telemachus.mcp.server import handle_tool_call


def run_test():
    try:
        store = FinanceStore(root_dir=test_dir)
        recurring = RecurringEngine(store)

        # 1. Seed Ground Truth via setup.yaml
        yaml_content = """
as_of_date: "2026-10-01"
starting_balances:
  wells_fargo_checking: 1500.00
  capital_one_savings: 5000.00
  visa_credit_card: 0.00

fixed_bills:
  kahram:
    amount: 150.00
    due_day: 1
    active: true
  gym:
    amount: 60.00
    due_day: 1
    active: true
  apple_icloud:
    amount: 2.99
    due_day: 11
    active: true
  spotify:
    amount: 11.99
    due_day: 25
    active: true
  wifi:
    amount: 35.73
    due_day: 15
    active: true
  rent:
    amount: 0.00
    due_day: 1
    active: false

monthly_targets:
  month: "2026-10"
  income: 2700.00
  savings_target: 1000.00
  groceries_planned: 300.00
  gas_planned: 150.00
  pge_estimate: 75.00
  notes: "October Ground Truth Scenario"
"""
        yaml_file = Path(test_dir) / "setup.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")
        apply_setup_yaml(yaml_path=yaml_file, store=store)

        # 2. Seed Gas and Specific Date Transactions
        # Gas 1: Oct 5 - $30.00 Chevron
        # Gas 2: Oct 12 - $45.00 Shell
        # Gas 3: Oct 18 - $55.00 Costco (most recent)
        # Total Gas = $130.00 / 3 = $43.33 average
        # Oct 15 Spending:
        #   - Lunch at Taqueria: $14.50
        #   - Trader Joe's: $42.20
        #   Total on Oct 15 = $56.70
        transactions = [
            Transaction.create(
                date="2026-10-05",
                payee="Chevron",
                postings=[
                    Posting("Liabilities:CreditCard:Visa", -3000),
                    Posting("Expenses:Transport:Gas", 3000),
                ],
                memo="Gas fillup",
            ),
            Transaction.create(
                date="2026-10-12",
                payee="Shell",
                postings=[
                    Posting("Liabilities:CreditCard:Visa", -4500),
                    Posting("Expenses:Transport:Gas", 4500),
                ],
                memo="Highway gas",
            ),
            Transaction.create(
                date="2026-10-15",
                payee="Taqueria",
                postings=[
                    Posting("Liabilities:CreditCard:Visa", -1450),
                    Posting("Expenses:Food:Dining", 1450),
                ],
                memo="Lunch burrito",
            ),
            Transaction.create(
                date="2026-10-15",
                payee="Trader Joe's",
                postings=[
                    Posting("Liabilities:CreditCard:Visa", -4220),
                    Posting("Expenses:Food:Groceries", 4220),
                ],
                memo="Weekly pantry staples",
            ),
            Transaction.create(
                date="2026-10-18",
                payee="Costco Gasoline",
                postings=[
                    Posting("Liabilities:CreditCard:Visa", -5500),
                    Posting("Expenses:Transport:Gas", 5500),
                ],
                memo="Full tank premium gas",
            ),
        ]
        for t in transactions:
            store.log_transaction(t)

        print("=" * 70)
        print("🎯 GROUND TRUTH SCENARIO INITIALIZED IN SANDBOX")
        print("=" * 70)

        # ----------------------------------------------------------------------
        # MCP TOOL INVOCATIONS
        # ----------------------------------------------------------------------
        q1 = "Given my account balance right now, how much do I have for personal spendings?"
        tool_out_1 = handle_tool_call("finance_monthly_overview", {"month": "2026-10"})

        q2 = "How much did I pay for gas this last time?"
        tool_out_2 = handle_tool_call("finance_query_transactions", {"category": "gas", "limit": 1})

        q3 = "How much have I been paying for gas on average since I've been tracking?"
        tool_out_3 = handle_tool_call("finance_query_transactions", {"category": "gas"})

        q4 = "On this exact date (2026-10-15), what was my financial spending?"
        tool_out_4 = handle_tool_call("finance_query_transactions", {"date": "2026-10-15"})

        tests = [
            ("Q1 (Personal Spendings)", q1, tool_out_1, "$914.29"),
            ("Q2 (Last Gas Payment)", q2, tool_out_2, "$55.00"),
            ("Q3 (Average Gas)", q3, tool_out_3, "$43.33"),
            ("Q4 (Spending on Oct 15)", q4, tool_out_4, "$56.70"),
        ]

        print("\n" + "=" * 70)
        print("🤖 RUNNING LIVE INFERENCE WITH QWEN 3.5 9B (LM Studio)")
        print("=" * 70)

        for label, prompt, context, expected in tests:
            print(f"\n--- {label} ---")
            print(f"Target Ground Truth: {expected}")
            print("Tool Context:")
            print(context)

            payload = {
                "model": "qwen/qwen3.5-9b",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are Telemachus, a private financial assistant. "
                            "You answer user queries strictly using the provided deterministic financial ledger data. "
                            "State the direct dollar amount clearly in your first sentence. Do not alter or recalculate the numbers."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Financial Ledger Data:\n{context}\n\nUser Question: {prompt}",
                    },
                ],
                "temperature": 0.0,
                "max_tokens": 1200,
            }

            req = urllib.request.Request(
                "http://127.0.0.1:1234/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                choice = res_data["choices"][0]["message"]
                content = choice.get("content") or ""
                reasoning = choice.get("reasoning_content") or ""
                print(f"\nQwen 3.5 9B Response:\n{content.strip()}")
                if expected in content or expected in reasoning:
                    print(f"✅ Ground truth verified! Contains {expected}")
                else:
                    print(f"⚠️  Verification check: expected {expected}")

    finally:
        del os.environ["TELEMACHUS_FINANCE_DIR"]
        shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == "__main__":
    run_test()
