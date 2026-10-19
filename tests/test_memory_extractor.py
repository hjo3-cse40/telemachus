"""Unit tests for Continual MemoryExtractor and PrivacyGate."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from telemachus.memory.cache import MemoryCache
from telemachus.memory.extractor import (
    MemoryExtractor,
    fallback_regex_candidates,
    is_jaccard_duplicate,
    parse_extraction_json,
    strip_think,
)
from telemachus.memory.models import MemoryEntry, PrivacyTier
from telemachus.memory.privacy_gate import PrivacyGate
from telemachus.memory.vault import MemoryVault


class TestPrivacyGate(unittest.TestCase):
    """Verifies deterministic classification and tier routing."""

    def test_sensitive_finances_routed_to_tier_2(self):
        tier, reason, _ = PrivacyGate.classify("My checking account balance is $655.26.")
        self.assertEqual(tier, PrivacyTier.TIER_2_PRIVATE)
        self.assertIn("sensitive pattern", reason)

        tier, _, _ = PrivacyGate.classify("Venmo transfer of $370 pending.")
        self.assertEqual(tier, PrivacyTier.TIER_2_PRIVATE)

        tier, _, _ = PrivacyGate.classify("Biweekly paycheck hits on Thursday.")
        self.assertEqual(tier, PrivacyTier.TIER_2_PRIVATE)

    def test_pii_and_schedule_routed_to_tier_2(self):
        tier, _, _ = PrivacyGate.classify("User attends UCSC taking CMPM 120.")
        self.assertEqual(tier, PrivacyTier.TIER_2_PRIVATE)

        tier, _, _ = PrivacyGate.classify("User commutes from Menlo Park to Santa Cruz.")
        self.assertEqual(tier, PrivacyTier.TIER_2_PRIVATE)

        tier, _, _ = PrivacyGate.classify("User's full name is Alex Kim.")
        self.assertEqual(tier, PrivacyTier.TIER_2_PRIVATE)

    def test_coding_style_routed_to_tier_0(self):
        tier, reason, conf = PrivacyGate.classify("Never use em-dashes in drafted responses.")
        self.assertEqual(tier, PrivacyTier.TIER_0_PUBLIC)
        self.assertGreaterEqual(conf, 0.90)

        tier, _, _ = PrivacyGate.classify("Prefer camelCase for JavaScript variables.")
        self.assertEqual(tier, PrivacyTier.TIER_0_PUBLIC)

        tier, _, _ = PrivacyGate.classify("Preferred terminal font is JetBrains Mono.")
        self.assertEqual(tier, PrivacyTier.TIER_0_PUBLIC)

    def test_project_routed_to_tier_1(self):
        tier, _, _ = PrivacyGate.classify("Working on the Telemachus agent harness.", category="project")
        self.assertEqual(tier, PrivacyTier.TIER_1_PROJECT)


class TestExtractorComponents(unittest.TestCase):
    """Verifies extraction parsing, stripping, fallbacks, and deduplication."""

    def test_strip_think(self):
        raw = "<think>Analyzing user text...\nFound name Sam.</think>[{\"text\": \"User's name is Sam.\", \"category\": \"identity\"}]"
        cleaned = strip_think(raw)
        self.assertEqual(cleaned, "[{\"text\": \"User's name is Sam.\", \"category\": \"identity\"}]")

    def test_parse_extraction_json(self):
        raw = "```json\n[{\"text\": \"User attends UCSC.\", \"category\": \"fact\"}]\n```"
        parsed = parse_extraction_json(raw)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["text"], "User attends UCSC.")

        # With think and commentary
        noisy = "<think>Let me see.</think> Here is the output:\n[{\"text\": \"User prefers Python.\", \"category\": \"preference\"}] Done!"
        parsed_noisy = parse_extraction_json(noisy)
        self.assertEqual(len(parsed_noisy), 1)
        self.assertEqual(parsed_noisy[0]["text"], "User prefers Python.")

    def test_fallback_regex_candidates(self):
        messages = [
            {"role": "user", "content": "My name is Alex Kim and I live in Santa Cruz."},
            {"role": "assistant", "content": "Nice to meet you Alex!"},
            {"role": "user", "content": "I prefer dark theme for all IDEs."},
        ]
        candidates = fallback_regex_candidates(messages)
        self.assertTrue(any("Alex Kim" in c["text"] for c in candidates))
        self.assertTrue(any("Santa Cruz" in c["text"] for c in candidates))

    def test_is_jaccard_duplicate(self):
        existing = [
            MemoryEntry(text="The user goes to UCSC and takes CMPM 120."),
            MemoryEntry(text="User prefers dark mode."),
        ]
        # Overlapping tokens
        self.assertTrue(is_jaccard_duplicate("User attends UCSC and takes CMPM 120", existing, threshold=0.55))
        # Distinct
        self.assertFalse(is_jaccard_duplicate("User loves playing basketball on weekends", existing))


class TestMemoryExtractorPipeline(unittest.TestCase):
    """End-to-end extraction and routing pipeline."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_extractor_")
        self.vault = MemoryVault(root_dir=self.test_dir)
        self.vault.initialize()
        self.cache = MemoryCache(vault=self.vault)
        self.extractor = MemoryExtractor(
            vault=self.vault,
            cache=self.cache,
            completion_url="http://127.0.0.1:9999/v1/chat/completions",
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_extract_and_store_fallback_private(self):
        messages = [
            {"role": "user", "content": "My class is CMPM 120 on Tuesdays and Thursdays."},
            {"role": "assistant", "content": "Got it, I will remember your CMPM 120 class schedule."},
        ]

        saved = self.extractor.extract_and_store(messages)
        self.assertGreaterEqual(len(saved), 1)

        # Check saved tier
        first = saved[0]
        self.assertEqual(first.tier, PrivacyTier.TIER_2_PRIVATE)

        # Verify it was written to private facts file
        private_facts = self.vault.load_facts(tier=PrivacyTier.TIER_2_PRIVATE)
        self.assertTrue(any("CMPM" in f.text for f in private_facts))

        # Verify it is in cache
        cached = self.cache.search("CMPM 120")
        self.assertGreaterEqual(len(cached), 1)

    def test_extract_and_store_fallback_public(self):
        messages = [
            {"role": "user", "content": "I prefer dark theme for code editors."},
            {"role": "assistant", "content": "I will keep that in mind."},
        ]

        saved = self.extractor.extract_and_store(messages)
        self.assertGreaterEqual(len(saved), 1)

        first = saved[0]
        self.assertEqual(first.tier, PrivacyTier.TIER_0_PUBLIC)

        # Verify it was written to public facts file
        public_facts = self.vault.load_facts(tier=PrivacyTier.TIER_0_PUBLIC)
        self.assertTrue(any("dark theme" in f.text for f in public_facts))


if __name__ == "__main__":
    unittest.main()
