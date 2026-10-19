"""Unit tests for Telemachus Unified Memory Vault, Cache, and Importer."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from telemachus.memory.cache import MemoryCache
from telemachus.memory.importer import MemoryImporter
from telemachus.memory.models import MemoryEntry, PrivacyTier
from telemachus.memory.vault import MemoryVault


class TestMemoryVaultAndCache(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_memory_vault_")
        self.vault = MemoryVault(root_dir=self.test_dir)
        self.vault.initialize()
        self.cache = MemoryCache(vault=self.vault)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_vault_initialization_and_permissions(self):
        self.assertTrue(self.vault.root_dir.exists())
        self.assertTrue(self.vault.private_dir.exists())
        self.assertTrue(self.vault.public_dir.exists())
        self.assertTrue(self.vault.events_file.exists())

        # Verify 0700 permissions on private directory
        mode = oct(self.vault.private_dir.stat().st_mode)[-3:]
        self.assertEqual(mode, "700")

    def test_append_and_load_facts(self):
        entry_private = MemoryEntry(
            text="User goes to UCSC CMPM 120",
            category="fact",
            tier=PrivacyTier.TIER_2_PRIVATE,
            pinned=True,
        )
        entry_public = MemoryEntry(
            text="Avoid em dashes in all markdown copy",
            category="style",
            tier=PrivacyTier.TIER_0_PUBLIC,
            pinned=True,
        )

        self.vault.append_fact(entry_private)
        self.vault.append_fact(entry_public)

        # Verify loading all
        all_facts = self.vault.load_facts()
        self.assertEqual(len(all_facts), 2)

        # Verify loading by tier
        private_facts = self.vault.load_facts(tier=PrivacyTier.TIER_2_PRIVATE)
        self.assertEqual(len(private_facts), 1)
        self.assertEqual(private_facts[0].text, "User goes to UCSC CMPM 120")

        public_facts = self.vault.load_facts(tier=PrivacyTier.TIER_0_PUBLIC)
        self.assertEqual(len(public_facts), 1)
        self.assertEqual(public_facts[0].text, "Avoid em dashes in all markdown copy")

    def test_write_and_read_docs(self):
        content = "# Identity\n\nUser is Alex Kim.\n"
        doc_path = self.vault.write_doc("profile/identity.md", content, tier=PrivacyTier.TIER_2_PRIVATE)
        self.assertTrue(doc_path.exists())

        read_back = self.vault.read_doc("profile/identity.md", tier=PrivacyTier.TIER_2_PRIVATE)
        self.assertEqual(read_back, content)

        docs = self.vault.list_docs(tier=PrivacyTier.TIER_2_PRIVATE)
        self.assertIn("profile/identity.md", docs)

    def test_path_traversal_prevention(self):
        with self.assertRaises(ValueError):
            self.vault.write_doc("../evil.md", "malicious content", tier=PrivacyTier.TIER_2_PRIVATE)

    def test_cache_sync_and_search_with_tier_gating(self):
        entry_priv = MemoryEntry(
            id="fact-priv-1",
            text="Checking account balance is 655 dollars",
            category="finance",
            tier=PrivacyTier.TIER_2_PRIVATE,
        )
        entry_pub = MemoryEntry(
            id="fact-pub-1",
            text="Never attribute git commits or PRs to Cursor",
            category="style",
            tier=PrivacyTier.TIER_0_PUBLIC,
        )

        self.cache.upsert_entry(entry_priv)
        self.cache.upsert_entry(entry_pub)

        # Search without limit sees private
        res_all = self.cache.search("Checking")
        self.assertEqual(len(res_all), 1)
        self.assertEqual(res_all[0].id, "fact-priv-1")

        # Cloud-safe search (TIER_0_PUBLIC limit) MUST NOT see private finances
        res_cloud = self.cache.search("Checking", tier_limit=PrivacyTier.TIER_0_PUBLIC)
        self.assertEqual(len(res_cloud), 0)

        # Cloud-safe search DOES see public rules
        res_pub = self.cache.search("Cursor", tier_limit=PrivacyTier.TIER_0_PUBLIC)
        self.assertEqual(len(res_pub), 1)
        self.assertEqual(res_pub[0].id, "fact-pub-1")

    def test_importer_odysseus(self):
        # Create a mock Odysseus memory.json
        mock_odysseus = [
            {
                "id": "mock-1",
                "text": "The user has a recurring class called CMPM 120 at UCSC.",
                "category": "fact",
                "timestamp": 1791489377,
                "uses": 5,
                "pinned": False,
                "owner": "test_user",
            },
            {
                "id": "mock-2",
                "text": "The user is working on item interactions for a game engine.",
                "category": "project",
                "timestamp": 1791509197,
                "uses": 2,
                "pinned": False,
                "owner": "test_user",
            },
        ]
        mock_file = Path(self.test_dir) / "mock_odysseus.json"
        mock_file.write_text(json.dumps(mock_odysseus), encoding="utf-8")

        importer = MemoryImporter(self.vault, self.cache)
        count = importer.import_odysseus(mock_file)
        self.assertEqual(count, 2)

        # Search for imported class
        results = self.cache.search("CMPM 120")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].tier, PrivacyTier.TIER_2_PRIVATE)

        # Search for project
        proj_results = self.cache.search("game engine")
        self.assertEqual(len(proj_results), 1)
        self.assertEqual(proj_results[0].tier, PrivacyTier.TIER_1_PROJECT)

    def test_event_logging(self):
        self.vault.log_event("TEST_EVENT", {"detail": "hello world"})
        events = [json.loads(line) for line in self.vault.events_file.read_text().splitlines() if line.strip()]
        self.assertTrue(any(e["event"] == "TEST_EVENT" for e in events))


if __name__ == "__main__":
    unittest.main()
