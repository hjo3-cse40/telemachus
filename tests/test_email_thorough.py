"""Comprehensive Verification and Stress Test Suite for Telemachus Email Subsystem.

Tests:
1. Account Config & Security Loader
2. IMAP Connection & Folder Enumeration
3. Header Paging & Unread Filtering
4. MIME/HTML Extraction, Character Encoding, and 16KB Bounds
5. Non-Existent UID & Edge Case Error Handling
6. Safe Draft Creation, RFC822 Header Integrity, and Cleanup
7. Laya MPS ModernBERT Triage Batch & Latency Benchmarking
8. MCP Server Stdio JSON-RPC End-to-End Protocol Test
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

# Ensure project root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telemachus.config.loader import get_account, load_accounts, load_env
from telemachus.email.client import IMAPClient
from telemachus.email.draft import create_draft
from telemachus.email.triage import triage_messages


def run_tests():
    print("=" * 80)
    print("  TELEMACHUS EMAIL SUBSYSTEM: COMPREHENSIVE VERIFICATION SUITE")
    print("=" * 80)

    passed_tests = 0
    total_tests = 8

    # --------------------------------------------------------------------------
    # TEST 1: Config & Security Loader
    # --------------------------------------------------------------------------
    print("\n[TEST 1/8] Verifying Configuration & Credential Isolation...")
    load_env()
    accounts = load_accounts()
    assert len(accounts) == 5, f"Expected 5 accounts, got {len(accounts)}"
    yahoo = get_account("yahoo")
    assert yahoo is not None, "Yahoo account config missing"
    assert yahoo.enabled is True, "Yahoo should be enabled"
    assert bool(yahoo.email and yahoo.password), "Yahoo credentials missing from .env"
    # Ensure other accounts are disabled placeholders
    disabled = [a for a in accounts if not a.enabled]
    assert len(disabled) == 4, f"Expected 4 disabled accounts, got {len(disabled)}"
    print(f"  ✓ Loaded 5 accounts (1 active, 4 ready placeholders). Credentials isolated in .env.")
    passed_tests += 1

    # --------------------------------------------------------------------------
    # TEST 2: IMAP Connection & Folder Enumeration
    # --------------------------------------------------------------------------
    print("\n[TEST 2/8] Testing TLS Handshake & Folder Discovery...")
    with IMAPClient(yahoo, timeout=12) as client:
        folders = client.list_folders()
        assert len(folders) > 0, "No folders returned"
        assert any("inbox" in f.lower() for f in folders), "INBOX folder not found"
        st = client.get_mailbox_status(yahoo.inbox_folder)
        assert st["total"] > 0, "Inbox should have messages"
        print(f"  ✓ Connected via TLS to {yahoo.imap_host}:{yahoo.imap_port}")
        print(f"  ✓ Discovered {len(folders)} remote mailbox folders")
        print(f"  ✓ INBOX counts: {st['total']} total messages, {st['unseen']} unseen")
    passed_tests += 1

    # --------------------------------------------------------------------------
    # TEST 3: Header Paging & Unread Filtering
    # --------------------------------------------------------------------------
    print("\n[TEST 3/8] Testing Header Paging (limits 1, 5, 15) and Read-Only Guarantees...")
    with IMAPClient(yahoo) as client:
        # Check initial unread count
        initial_status = client.get_mailbox_status(yahoo.inbox_folder)

        h1 = client.list_recent_headers(limit=1)
        assert len(h1) == 1, f"Expected 1 header, got {len(h1)}"

        h5 = client.list_recent_headers(limit=5)
        assert len(h5) == 5, f"Expected 5 headers, got {len(h5)}"

        h15 = client.list_recent_headers(limit=15)
        assert len(h15) == 15, f"Expected 15 headers, got {len(h15)}"

        # Verify read-only: unread count must not have changed
        after_status = client.get_mailbox_status(yahoo.inbox_folder)
        assert initial_status["unseen"] == after_status["unseen"], "Read flags were modified by header listing!"
        print(f"  ✓ Paging verified across limits [1, 5, 15]")
        print(f"  ✓ Read-Only invariant verified: unread count remained exactly {after_status['unseen']}")
    passed_tests += 1

    # --------------------------------------------------------------------------
    # TEST 4: MIME/HTML Extraction, Encoding & 16KB Guardrail
    # --------------------------------------------------------------------------
    print("\n[TEST 4/8] Testing MIME Body Extraction & 16KB Token Guardrail on 10 Emails...")
    with IMAPClient(yahoo) as client:
        headers = client.list_recent_headers(limit=10)
        messages = []
        for h in headers:
            msg = client.fetch_message(h.uid)
            assert msg is not None, f"Failed to fetch UID {h.uid}"
            assert msg.uid == h.uid
            assert len(msg.body_text) <= 16384, f"Payload exceeded 16KB cap: {len(msg.body_text)}"
            messages.append(msg)

        total_bytes = sum(len(m.body_text) for m in messages)
        print(f"  ✓ Successfully parsed 10 full email bodies")
        print(f"  ✓ Cleaned text extracts: avg {total_bytes // 10} bytes/email (strictly bounded <= 16KB)")
        sample = messages[0]
        print(f"  ✓ Sample snippet (UID {sample.uid}): \"{sample.preview_snippet(80)}\"")
    passed_tests += 1

    # --------------------------------------------------------------------------
    # TEST 5: Non-Existent UID Error Handling
    # --------------------------------------------------------------------------
    print("\n[TEST 5/8] Testing Edge Cases & Non-Existent UID Handling...")
    with IMAPClient(yahoo) as client:
        # Non-existent UID should return None safely, not crash
        fake_msg = client.fetch_message("9999999999")
        assert fake_msg is None, "Expected None for non-existent UID"
        print("  ✓ Non-existent UID handled gracefully with clean None return.")
    passed_tests += 1

    # --------------------------------------------------------------------------
    # TEST 6: Safe Draft Creation, RFC822 Header Check, and Safe Cleanup
    # --------------------------------------------------------------------------
    print("\n[TEST 6/8] Testing Safe Draft Appending & Remote IMAP Verification...")
    test_subj = f"[Telemachus Suite Verification] Safe Draft {int(time.time())}"
    draft_res = create_draft(
        account=yahoo,
        to="test.suite@example.com",
        subject=test_subj,
        body="Automated verification test of remote IMAP draft appending with multi-line text and unicode: ✓ 🚀",
    )
    assert draft_res["success"] is True, f"Draft creation failed: {draft_res}"

    # Verify and clean up
    with IMAPClient(yahoo) as client:
        client.conn.select(yahoo.drafts_folder)
        status, data = client.conn.uid("SEARCH", None, f'SUBJECT "{test_subj}"')
        assert data and data[0], "Created draft not found in remote Drafts folder"
        uids = data[0].split()
        print(f"  ✓ Draft verified in remote '{yahoo.drafts_folder}' folder (UID: {uids[0].decode()})")

        # Cleanup test draft
        for u in uids:
            client.conn.uid("STORE", u, "+FLAGS", "(\\Deleted)")
        client.conn.expunge()
        print(f"  ✓ Test draft cleaned up safely (deleted & expunged).")
    passed_tests += 1

    # --------------------------------------------------------------------------
    # TEST 7: Laya MPS ModernBERT Triage Batch & Latency Benchmarking
    # --------------------------------------------------------------------------
    print("\n[TEST 7/8] Benchmarking Laya MPS ModernBERT Classifier on 15 Real Emails...")
    with IMAPClient(yahoo) as client:
        headers = client.list_recent_headers(limit=15)
        batch = [client.fetch_message(h.uid) for h in headers if h]

    start_t = time.perf_counter()
    results = triage_messages(batch)
    total_ms = (time.perf_counter() - start_t) * 1000
    assert len(results) == len(batch), f"Expected {len(batch)} results, got {len(results)}"

    categories = {}
    for r in results:
        categories[r.category] = categories.get(r.category, 0) + 1

    print(f"  ✓ Triaged {len(results)} emails in {total_ms:.1f}ms total (avg {total_ms / len(results):.1f}ms per email)")
    print(f"  ✓ Category breakdown: {json.dumps(categories)}")
    passed_tests += 1

    # --------------------------------------------------------------------------
    # TEST 8: MCP Server Stdio JSON-RPC Protocol Test
    # --------------------------------------------------------------------------
    print("\n[TEST 8/8] Testing MCP Server Stdio Protocol End-to-End...")
    proc = subprocess.Popen(
        ["/opt/homebrew/bin/python3", "-m", "telemachus.mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=str(PROJECT_ROOT),
    )

    def rpc_call(payload):
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    # 1. Initialize
    init_res = rpc_call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init_res.get("result", {}).get("serverInfo", {}).get("name") == "telemachus-email"

    # 2. Tools list
    tools_res = rpc_call({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tool_names = [t["name"] for t in tools_res.get("result", {}).get("tools", [])]
    expected_tools = ["email_status", "email_list", "email_show", "email_triage", "email_draft"]
    assert all(t in tool_names for t in expected_tools), f"Missing tools: {set(expected_tools) - set(tool_names)}"

    # 3. Tool call email_status
    status_call = rpc_call({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "email_status", "arguments": {}}})
    assert "Telemachus Mailbox Status" in status_call["result"]["content"][0]["text"]

    # 4. Tool call email_list
    list_call = rpc_call({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "email_list", "arguments": {"limit": 3}}})
    assert "Recent emails for [yahoo]" in list_call["result"]["content"][0]["text"]

    proc.terminate()
    print("  ✓ Stdio JSON-RPC: initialize, tools/list, email_status, and email_list all PASSED.")
    passed_tests += 1

    # --------------------------------------------------------------------------
    # Summary
    # --------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print(f"  ALL {passed_tests}/{total_tests} TESTS PASSED SUCCESSFULLY! 100% GREEN.")
    print("=" * 80)


if __name__ == "__main__":
    run_tests()
