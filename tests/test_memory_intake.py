"""End-to-end tests for the memory intake (source split, paste check, Laya, quotes, failures)."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_lmstudio import FakeLMStudio, facts  # noqa: E402

from telemachus.memory.cache import MemoryCache  # noqa: E402
from telemachus.memory.extractor import MemoryExtractor  # noqa: E402
from telemachus.memory.health import MemoryHealth  # noqa: E402
from telemachus.memory.intake import MemoryIntake, clean_document_text, previous_turn_documents  # noqa: E402
from telemachus.memory.laya_gate import LayaUnavailable  # noqa: E402
from telemachus.memory.recall import recall, render_recall, render_source  # noqa: E402
from telemachus.memory.vault import MemoryVault  # noqa: E402


class FakeGate:
    """Scripted Laya: p_own / p_durable per call, or raises LayaUnavailable."""

    def __init__(self, p_own=0.5, p_durable=0.5, fail=False):
        self.p_own, self.p_durable, self.fail = p_own, p_durable, fail
        self.calls = []

    def classify(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise LayaUnavailable("worker did not start (test)")
        return [{
            "model": "english",
            "authorship": {"probabilities": {"own_words": self.p_own, "pasted_material": 1 - self.p_own}},
            "durability": {"probabilities": {"durable_personal_fact": self.p_durable,
                                             "no_durable_fact": 1 - self.p_durable}},
            "latency_ms": 1.0,
        } for _ in texts]


def user(text):
    return {"role": "user", "content": text}


class IntakeTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="test_intake_")
        self.vault = MemoryVault(root_dir=self.dir)
        self.cache = MemoryCache(vault=self.vault)
        self.lm = FakeLMStudio()
        self.extractor = MemoryExtractor(completion_url=self.lm.completion_url)

    def tearDown(self):
        self.lm.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def intake(self, gate=None, use_laya=False):
        return MemoryIntake(vault=self.vault, cache=self.cache, extractor=self.extractor,
                            gate=gate, use_laya=use_laya)

    def events(self, name):
        rows = [json.loads(l) for l in self.vault.events_file.read_text().splitlines() if l.strip()]
        return [r for r in rows if r["event"] == name]


class TestSourceSplit(IntakeTestBase):
    def test_tool_round_is_not_a_new_turn(self):
        msgs = [user("I prefer short emails."),
                {"role": "assistant", "content": "", "tool_calls": []},
                {"role": "tool", "tool_call_id": "x", "content": "I prefer long emails."}]
        report = self.intake().process_turn(msgs)
        self.assertEqual(report.status, "skipped")
        self.assertEqual(self.lm.requests, [], "a tool round must not cost an LLM call")

    def test_own_fact_saved_with_quote_and_source(self):
        self.lm.replies = [facts({"text": "User prefers short emails.", "quote": "I prefer short emails",
                                  "category": "preference"})]
        report = self.intake().process_turn([user("I prefer short emails with bullet points.")])
        self.assertEqual(report.saved, ["User prefers short emails."])
        stored = self.vault.load_facts()
        self.assertEqual(stored[0].metadata["quote"], "I prefer short emails")
        self.assertEqual(stored[0].metadata["kind"], "self")
        # The extraction prompt contains only the user's words.
        self.assertIn("I prefer short emails with bullet points.", self.lm.requests[0]["messages"][1]["content"])

    def test_only_newest_user_message_is_sent(self):
        self.lm.replies = ["[]"]
        msgs = [user("Old message about my schedule."),
                {"role": "assistant", "content": "Assistant text that must not be sent."},
                {"role": "tool", "tool_call_id": "t", "content": "tool text that must not be sent"},
                user("thanks")]
        self.intake().process_turn(msgs)
        sent = self.lm.requests[0]["messages"][1]["content"]
        self.assertIn("thanks", sent)
        self.assertNotIn("Assistant text", sent)
        self.assertNotIn("tool text", sent)
        self.assertNotIn("Old message", sent)

    def test_same_turn_is_processed_once(self):
        self.lm.replies = ["[]", "[]"]
        intake = self.intake()
        intake.process_turn([user("I work Mondays.")])
        second = intake.process_turn([user("I work Mondays.")])
        self.assertEqual(second.status, "skipped")
        self.assertEqual(len(self.lm.requests), 1)


class TestPastedMaterial(IntakeTestBase):
    PASTE = ("help me reply to this, I want to push the call to Wednesday:\n\n"
             "From: Dana Park <dana@acme.example>\nSubject: call\n\n"
             "I prefer Tuesday calls. Does 2pm work?\n\nBest,\nDana")

    def test_pasted_first_person_never_becomes_a_user_fact(self):
        self.lm.replies = [
            "[]",  # self facts from the own part
            facts({"text": "Dana prefers Tuesday calls.", "about": "Dana Park", "quote": "I prefer Tuesday calls"}),
        ]
        report = self.intake().process_turn([user(self.PASTE)])
        self.assertEqual(self.vault.load_facts(), [])
        own_prompt = self.lm.requests[0]["messages"][1]["content"]
        self.assertIn("push the call to Wednesday", own_prompt)
        self.assertNotIn("Tuesday", own_prompt, "pasted text must not reach the self-fact prompt")
        docs = self.vault.load_document_facts()
        self.assertEqual([d.text for d in docs], ["Dana prefers Tuesday calls."])
        self.assertEqual(docs[0].metadata["about"], "Dana Park")
        self.assertEqual(report.document_facts, ["Dana prefers Tuesday calls."])

    def test_own_and_unsure_text_are_separate_prompt_sections(self):
        self.lm.replies = ["[]", "[]"]
        text = ("I always keep replies under five sentences. Can you trim this draft?\n\n"
                "Hi Jordan,\n\nThanks so much for the update on the wafers.")
        self.intake().process_turn([user(text)])
        prompt = self.lm.requests[0]["messages"][1]["content"]
        own_at = prompt.index("The user's own words:")
        pasted_at = prompt.index("may have pasted")
        self.assertLess(own_at, prompt.index("I always keep replies"))
        self.assertLess(prompt.index("I always keep replies"), pasted_at)
        self.assertLess(pasted_at, prompt.index("Thanks so much"))

    def test_document_fact_is_cited_in_recall(self):
        self.lm.replies = [
            "[]",
            facts({"text": "Dana prefers Tuesday calls.", "about": "Dana Park", "quote": "I prefer Tuesday calls"}),
        ]
        self.intake().process_turn([user(self.PASTE)])
        text = render_recall(recall(self.cache, "Dana call preference"))
        self.assertIn("STATUS: MATCH", text)
        self.assertIn("[M1] Dana prefers Tuesday calls.", text)
        self.assertIn("from a document, about Dana Park", text)
        self.assertIn('quote: "I prefer Tuesday calls"', text)
        mem_id = recall(self.cache, "Dana")["items"][0]["id"]
        source = render_source(self.vault, self.cache, mem_id)
        self.assertIn("STATUS: FOUND", source)
        self.assertIn("Does 2pm work?", source, "the stored source text must be readable")


class TestQuoteRejection(IntakeTestBase):
    def test_invented_quote_is_rejected_loudly(self):
        self.lm.replies = [facts({"text": "User lives in Paris.", "quote": "I live in Paris", "category": "identity"})]
        report = self.intake().process_turn([user("I prefer short emails.")])
        self.assertEqual(report.saved, [])
        self.assertEqual(self.vault.load_facts(), [])
        self.assertEqual(len(report.rejected), 1)
        self.assertTrue(self.events("CANDIDATE_REJECTED"))
        self.assertEqual(MemoryHealth(self.vault).summary()["components"]["quote_check"]["total_rejections"], 1)

    def test_invented_document_quote_is_rejected(self):
        self.lm.replies = ["[]", facts({"text": "Dana wants a discount.", "about": "Dana",
                                        "quote": "we need a 20% discount"})]
        report = self.intake().process_turn([user(TestPastedMaterial.PASTE)])
        self.assertEqual(self.vault.load_document_facts(), [])
        self.assertIn("quote not found", report.rejected[0]["reason"])

    def test_english_fact_for_korean_quote_goes_to_review(self):
        self.lm.replies = [facts({"text": "User writes polite emails to customers.",
                                  "quote": "나는 고객한테는 항상 존댓말 써", "category": "style"})]
        report = self.intake().process_turn([user("나는 고객한테는 항상 존댓말 써.")])
        self.assertEqual(report.saved, [])
        self.assertEqual(report.queued_for_review, ["User writes polite emails to customers."])
        self.assertIn("different languages", self.vault.load_pending()[0].metadata["review_reason"])

    def test_fact_not_supported_by_its_quote_is_rejected(self):
        self.lm.replies = [facts({"text": "User lives in Paris.", "quote": "I prefer short emails", "category": "identity"})]
        report = self.intake().process_turn([user("I prefer short emails.")])
        self.assertEqual(report.saved, [])
        self.assertIn("not supported by its quote", report.rejected[0]["reason"])

    def test_approximate_quote_goes_to_review(self):
        self.lm.replies = [facts({"text": "User prefers short emails.", "quote": "I prefers short email",
                                  "category": "preference"})]
        report = self.intake().process_turn([user("I prefer short emails with bullets.")])
        self.assertEqual(report.saved, [])
        self.assertEqual(report.queued_for_review, ["User prefers short emails."])
        pending = self.vault.load_pending()
        self.assertEqual(pending[0].metadata["quote_match"], "close")
        # Approval moves it into memory and makes it searchable.
        self.vault.resolve_pending(pending[0].id[:8], approve=True)
        self.cache.upsert_entry(self.vault.load_facts()[0])
        self.assertEqual(recall(self.cache, "short emails")["status"], "MATCH")


class TestLoudFailures(IntakeTestBase):
    def test_lmstudio_down_keeps_turn_and_reports(self):
        self.extractor = MemoryExtractor(completion_url="http://127.0.0.1:9/v1/chat/completions")
        report = self.intake().process_turn([user("I prefer short emails.")])
        self.assertEqual(report.status, "failed")
        health = MemoryHealth(self.vault).summary()
        self.assertEqual(health["status"], "DEGRADED")
        self.assertIn("extraction_llm", health["degraded_components"])
        self.assertEqual(len(self.vault.load_failed_turns()), 1)
        self.assertTrue(self.events("MEMORY_FAILURE"))

    def test_retry_after_recovery(self):
        intake = self.intake()
        self.lm.replies = [500]
        self.assertEqual(intake.process_turn([user("I prefer short emails.")]).status, "failed")
        self.lm.replies = [facts({"text": "User prefers short emails.", "quote": "I prefer short emails",
                                  "category": "preference"})]
        reports = intake.retry_failed()
        self.assertEqual(reports[0].saved, ["User prefers short emails."])
        self.assertEqual(self.vault.load_failed_turns(), [])
        self.assertEqual(MemoryHealth(self.vault).summary()["status"], "OK")

    def test_retry_that_fails_again_keeps_one_record(self):
        intake = self.intake()
        self.lm.replies = [500, 500]
        intake.process_turn([user("I prefer short emails.")])
        intake.retry_failed()
        self.assertEqual(len(self.vault.load_failed_turns()), 1)

    def test_document_extraction_failure_is_reported_and_retried_next_time(self):
        self.lm.replies = ["[]", 500]
        report = self.intake().process_turn([user(TestPastedMaterial.PASTE)])
        self.assertTrue(any(e.startswith("document") for e in report.errors))
        self.assertIn("document_extraction", MemoryHealth(self.vault).summary()["degraded_components"])


class TestLaya(IntakeTestBase):
    def test_low_durability_skips_the_llm_call(self):
        gate = FakeGate(p_durable=0.1)
        report = self.intake(gate=gate, use_laya=True).process_turn([user("fix the typo in paragraph two")])
        self.assertEqual(self.lm.requests, [])
        self.assertIn("p_durable", report.reason)

    def test_borderline_durability_still_calls_the_llm(self):
        self.lm.replies = ["[]"]
        self.intake(gate=FakeGate(p_durable=0.3), use_laya=True).process_turn([user("I work Mondays.")])
        self.assertEqual(len(self.lm.requests), 1)

    def test_laya_down_is_reported_and_extraction_continues(self):
        self.lm.replies = [facts({"text": "User works Mondays.", "quote": "I work Mondays", "category": "schedule"})]
        report = self.intake(gate=FakeGate(fail=True), use_laya=True).process_turn([user("I work Mondays.")])
        self.assertEqual(report.saved, ["User works Mondays."])
        self.assertEqual(report.laya["status"], "unavailable")
        self.assertIn("laya_gate", MemoryHealth(self.vault).summary()["degraded_components"])

    def test_korean_text_does_not_ask_laya(self):
        gate = FakeGate(p_durable=0.0)
        self.lm.replies = ["[]"]
        self.intake(gate=gate, use_laya=True).process_turn([user("나는 월요일에 일해.")])
        self.assertEqual(gate.calls, [])
        self.assertEqual(len(self.lm.requests), 1, "Korean text must reach the LLM even if Laya says skip")

    def test_unsure_text_goes_to_review_even_when_laya_says_own(self):
        # Laya's p_own is recorded but decides nothing: long unmarked text is UNSURE.
        long_own = "I keep every customer reply under five sentences and I never use exclamation marks. " * 6
        self.lm.replies = [facts({"text": "User never uses exclamation marks.",
                                  "quote": "I never use exclamation marks", "category": "style"})]
        report = self.intake(gate=FakeGate(p_own=0.99, p_durable=0.9), use_laya=True).process_turn([user(long_own)])
        self.assertEqual(report.saved, [])
        self.assertEqual(report.queued_for_review, ["User never uses exclamation marks."])
        self.assertIn("could not confirm as yours", self.vault.load_pending()[0].metadata["review_reason"])
        self.assertEqual(report.laya["p_own"], 0.99)

    def test_unsure_text_without_laya_never_becomes_a_user_fact(self):
        long_text = "We are thrilled to share that our metrology suite now supports 3D NAND. " * 8
        self.lm.replies = ["[]", facts({"text": "The suite supports 3D NAND.", "about": "general",
                                        "quote": "our metrology suite now supports 3D NAND"})]
        report = self.intake().process_turn([user(long_text)])
        self.assertEqual(self.vault.load_facts(), [])
        self.assertEqual(report.document_facts, ["The suite supports 3D NAND."])


class TestToolDocuments(IntakeTestBase):
    def _turn_with_read(self, tool_name, content):
        return [
            user("read the notes file"),
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": tool_name, "arguments": json.dumps({"path": "/tmp/notes.md"})}}]},
            {"role": "tool", "tool_call_id": "c1", "content": content},
            {"role": "assistant", "content": "Here is a summary."},
            user("thanks"),
        ]

    NOTES = ("File: /tmp/notes.md (lines 1-3 of 3):\n\n"
             "   1: Meeting notes, Oct 8. Helen prefers email over phone for all scheduling questions.\n"
             "   2: Jordan will send the sample wafers by Oct 20 and needs the PO number first.\n"
             "   3: Next review is on Nov 3 with the whole team, in person at the Austin office.\n")

    def test_line_numbers_and_header_are_removed(self):
        cleaned = clean_document_text(self.NOTES)
        self.assertNotIn("File:", cleaned)
        self.assertTrue(cleaned.startswith("Meeting notes"))

    def test_doc_tool_results_become_cited_document_facts(self):
        docs = previous_turn_documents(self._turn_with_read("telemachus_fs_read", self.NOTES))
        self.assertEqual(docs[0][0], "telemachus_fs_read:/tmp/notes.md")
        # Tool documents of the previous turn are processed first, then the new message.
        self.lm.replies = [facts({"text": "Helen prefers email over phone.", "about": "Helen",
                                  "quote": "Helen prefers email over phone"}), "[]"]
        report = self.intake().process_turn(self._turn_with_read("telemachus_fs_read", self.NOTES))
        self.assertEqual(report.document_facts, ["Helen prefers email over phone."])
        fact = self.vault.load_document_facts()[0]
        self.assertEqual(fact.metadata["source_ref"], "telemachus_fs_read:/tmp/notes.md")

    def test_non_document_tools_are_ignored(self):
        self.assertEqual(previous_turn_documents(self._turn_with_read("telemachus_fs_run", self.NOTES)), [])


if __name__ == "__main__":
    unittest.main()
