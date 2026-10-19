"""Unit tests for grounded memory extraction, quote checks and PrivacyGate."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_lmstudio import FakeLMStudio, facts  # noqa: E402

from telemachus.memory.extractor import (  # noqa: E402
    ExtractionError,
    MemoryExtractor,
    fact_supported,
    fallback_regex_candidates,
    is_jaccard_duplicate,
    parse_extraction_json,
    strip_think,
    support_share,
    verify_quote,
)
from telemachus.memory.models import MemoryEntry, PrivacyTier  # noqa: E402
from telemachus.memory.privacy_gate import PrivacyGate  # noqa: E402


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


class TestQuoteChecks(unittest.TestCase):
    def test_verify_quote(self):
        src = "Hi. I prefer short emails with bullet points, always."
        self.assertEqual(verify_quote("I prefer short emails", src), "exact")
        self.assertEqual(verify_quote("i PREFER   short emails", src), "exact")
        self.assertEqual(verify_quote("I prefers short email", src), "close")
        self.assertIsNone(verify_quote("I prefer long emails", src))
        self.assertIsNone(verify_quote("Tuesday calls work best", src))
        self.assertIsNone(verify_quote("short", src), "a one-word quote proves nothing")
        self.assertIsNone(verify_quote("", src))

    def test_quote_with_ellipsis_checks_each_part(self):
        src = "our team switched from Slack to Teams last month. Also I'm in Korea Standard Time half the year."
        self.assertEqual(verify_quote("our team switched from Slack to Teams ... I'm in Korea Standard Time", src), "exact")
        self.assertIsNone(verify_quote("our team switched from Slack to Teams ... I live in Paris now", src))

    def test_short_prefix_does_not_support(self):
        self.assertEqual(support_share("User is an astronaut.", "I am at home as usual"), 0.0)

    def test_verify_quote_korean_particles(self):
        src = "저는 한국어 메일을 쓸 때 항상 '홍길동 드림'으로 끝냅니다."
        self.assertIn(verify_quote("홍길동 드림으로 끝냅니다", src), ("exact", "close"))
        self.assertIsNone(verify_quote("영어 메일은 짧게 씁니다", src))

    def test_fact_supported(self):
        self.assertTrue(fact_supported("User prefers short emails.", "I prefer short emails"))
        self.assertFalse(fact_supported("User lives in Paris.", "I prefer short emails"))
        self.assertTrue(fact_supported("사용자는 한국어 메일을 홍길동 드림으로 끝낸다.", "한국어 메일을 쓸 때 항상 홍길동 드림으로 끝냅니다"))
        # The fact adds "Korean emails", which the short quote does not say: below the
        # automatic-save level (0.8), so it goes to review.
        self.assertLess(support_share("사용자는 한국어 메일을 홍길동 드림으로 끝낸다.", "홍길동 드림으로 끝냅니다"), 0.8)
        # A paraphrase keeps some words: review, not rejection.
        self.assertTrue(fact_supported(
            "User prefers emails to start directly with the point without the standard greeting.",
            'When you write emails for me, never open with "I hope this email finds you well." '
            "Just get to the point in the first sentence."))


class TestGroundedExtractor(unittest.TestCase):
    def setUp(self):
        self.lm = FakeLMStudio()
        self.extractor = MemoryExtractor(completion_url=self.lm.completion_url)

    def tearDown(self):
        self.lm.close()

    def test_request_uses_loaded_model_and_turns_thinking_off(self):
        self.lm.replies = [facts({"text": "User likes tea.", "quote": "I like tea", "category": "preference"})]
        out = self.extractor.extract_self_facts("I like tea a lot.")
        self.assertEqual(out[0]["quote"], "I like tea")
        body = self.lm.requests[0]
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(body["model"], "qwen/qwen3.5-9b")
        self.assertIn("I like tea a lot.", body["messages"][1]["content"])

    def test_no_loaded_chat_model_fails_without_a_request(self):
        self.lm.loaded = [m for m in self.lm.loaded if m["type"] == "embeddings"]
        with self.assertRaises(ExtractionError) as ctx:
            self.extractor.extract_self_facts("I like tea.")
        self.assertIn("No chat model is loaded", str(ctx.exception))
        self.assertEqual(self.lm.requests, [], "must not send a request that could JIT-load a model")

    def test_configured_model_not_loaded_fails(self):
        extractor = MemoryExtractor(completion_url=self.lm.completion_url, model_name="some/other-model")
        with self.assertRaises(ExtractionError):
            extractor.extract_self_facts("I like tea.")
        self.assertEqual(self.lm.requests, [])

    def test_unreachable_server_fails_loudly(self):
        extractor = MemoryExtractor(completion_url="http://127.0.0.1:9/v1/chat/completions")
        with self.assertRaises(ExtractionError):
            extractor.extract_self_facts("I like tea.")

    def test_http_error_and_garbage_output_raise(self):
        self.lm.replies = [500, "Sure! Here are the facts you asked for."]
        with self.assertRaises(ExtractionError):
            self.extractor.extract_self_facts("I like tea.")
        with self.assertRaises(ExtractionError):
            self.extractor.extract_self_facts("I like tea.")

    def test_empty_array_is_a_valid_answer(self):
        self.lm.replies = ["[]", "```json\n[]\n```"]
        self.assertEqual(self.extractor.extract_self_facts("thanks"), [])
        self.assertEqual(self.extractor.extract_self_facts("thanks"), [])


if __name__ == "__main__":
    unittest.main()
