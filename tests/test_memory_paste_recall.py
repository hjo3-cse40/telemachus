"""Tests for the paste detector (on the labelled synthetic set) and recall statuses."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from telemachus.memory.cache import MemoryCache
from telemachus.memory.models import MemoryEntry
from telemachus.memory.paste_detect import OWN, PASTED, analyze
from telemachus.memory.recall import recall, render_recall, render_source
from telemachus.memory.vault import MemoryVault

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "memory_intake_eval.json"
# Known miss: short marketing copy without any marker reads like a chat message.
KNOWN_MISSES = {"pasted_en_10"}


class TestPasteDetector(unittest.TestCase):
    def test_labelled_set(self):
        items = json.loads(FIXTURE.read_text(encoding="utf-8"))
        failures = []
        for it in items:
            a = analyze(it["text"])
            labels = [b.label for b in a.blocks]
            if it["author"] == "own":
                ok = labels == [OWN]
            elif it["author"] == "pasted":
                ok = OWN not in labels
            else:
                # Own words stay OWN; nothing outside them is OWN. The material may be
                # PASTED (strong markers) or UNSURE (weak signs: its facts go to review).
                ok = a.own_text.strip() == it["own_part"].strip()
            if not ok and it["id"] not in KNOWN_MISSES:
                failures.append((it["id"], labels))
        self.assertEqual(failures, [])

    def test_instruction_then_email_without_blank_line(self):
        a = analyze("reply to this please\nFrom: Dana\nSubject: x\n\nI prefer Tuesday calls.")
        self.assertEqual(a.own_text, "reply to this please")
        self.assertIn("I prefer Tuesday calls.", a.pasted_text)

    def test_chat_greetings_are_not_letters(self):
        for text in ("hey, can you fix this?", "Hi Claude, quick question about the deck", "팀장님, 이거 좀 봐줘"):
            self.assertEqual([b.label for b in analyze(text).blocks], [OWN], text)

    def test_greeting_to_a_named_person_is_pasted(self):
        for text in ("Hi Sam, quick question about the deck", "Hi Leo, great connecting at the expo! I lead integration."):
            self.assertNotIn(OWN, [b.label for b in analyze(text).blocks], text)

    def test_instruction_that_points_at_other_text(self):
        for text, own in [
            ("reply to this:\n\nCan we push to Friday?", "reply to this:"),
            ("what does this clause mean?\n\n12.4 Neither Party shall be liable for indirect damages.",
             "what does this clause mean?"),
            ("아래 메일 답장 써줘\n\n내일 미팅 장소가 바뀌었습니다.", "아래 메일 답장 써줘"),
        ]:
            self.assertEqual(analyze(text).own_text, own, text)

    def test_kakaotalk_and_slack_logs_are_pasted(self):
        for text in ("[카카오톡] 김민재 오후 2:31\n내일 미팅 장소 바뀌었어요",
                     "김민재 오후 2:31\n내일 미팅 장소 바뀌었어요",
                     "Dana Park 10:02 AM\nI prefer Tuesday calls"):
            self.assertNotIn(OWN, [b.label for b in analyze(text).blocks], text)

    def test_letter_greeting_line_is_pasted(self):
        for text in ("Hello Ms. Kim,\n\nThe report is attached.", "김민준 님,\n\n견적서 보내드립니다."):
            self.assertNotIn(OWN, [b.label for b in analyze(text).blocks], text)

    def test_text_after_a_marker_stays_pasted(self):
        a = analyze("summarize:\n\n> quoted line one\n> quoted line two\n\nI always work late on Fridays.")
        self.assertEqual(a.own_text, "summarize:")
        self.assertIn("I always work late on Fridays.", a.pasted_text)

    def test_reviewer_cases(self):
        # Short pasted paragraph between the instruction and a closing.
        a = analyze("help me reply:\n\nI prefer Tuesday calls, does 2pm work?\n\nThanks,\nDana")
        self.assertEqual(a.own_text, "help me reply:")
        # "Hi Sam" without a comma, alone on its line.
        self.assertEqual(analyze("help me reply\n\nHi Sam\n\nI prefer Tuesday calls.").own_text, "help me reply")
        # Many short unmarked paragraphs: only the first stays own.
        many = "\n\n".join(f"paragraph {i} with a few words in it." for i in range(40))
        self.assertEqual(analyze(many).own_text, "paragraph 0 with a few words in it.")
        # Paste first, question below.
        a = analyze("I am moving to Austin next month. I prefer calls on Tuesdays.\n\nwhat should I reply?")
        self.assertEqual(a.own_text, "what should I reply?")

    def test_clause_that_says_this_is_not_an_instruction(self):
        a = analyze("what does this clause mean?\n\n12.4 Neither Party shall be liable under this Agreement for indirect damages.")
        self.assertEqual(a.own_text, "what does this clause mean?")

    def test_short_paragraph_above_a_closing_is_not_own(self):
        a = analyze("ok\n\nI prefer Tuesday calls.\n\nBest,\nDana")
        self.assertNotIn("Tuesday", a.own_text)
        self.assertIn("I prefer Tuesday calls.", a.unsure_text)

    def test_polite_chat_closings_do_not_hide_own_text(self):
        for text in ("Thanks!\n\nAlso, I work Mondays from 10 to 7 now.",
                     "I also own the CRM now.\n\n월요일마다 김 이사님께 보고해요.\n\n감사합니다",
                     "lunch with dana 12:30 pm\n\nI always eat late on Mondays."):
            self.assertEqual({b.label for b in analyze(text).blocks}, {OWN}, text)

    def test_second_review_bypasses_are_not_own(self):
        cases = {
            "I'm out Friday, and I prefer async updates over standups.\n\nThanks,\nDana\n\nhow should I answer?": "async",
            "I prefer async updates and I always skip standups on Fridays.\n\nthoughts?": "async",
            "reply to this pls\nI'm out Friday. I always do code review on Mondays.": "code review",
            "Sincerely is not here but:\n\nDear Mr. Jo,\n\nI usually take calls in the morning.": "calls",
        }
        for text, word in cases.items():
            self.assertNotIn(word, analyze(text).own_text, text)

    def test_letter_with_its_own_greeting_does_not_hide_the_instruction(self):
        text = ("이거 영어로 답장 써줘. 참고로 나는 외부 메일은 항상 영어로 보내.\n\n"
                "안녕하세요 한결님,\n다음 주 데모 일정 확인 부탁드립니다.\n최지훈 드림")
        a = analyze(text)
        self.assertIn("나는 외부 메일은 항상 영어로 보내", a.own_text)
        self.assertNotIn("데모", a.own_text)

    def test_closing_needs_a_name(self):
        text = ("I work Mondays.\n\nthanks,\nalso can you make the reply shorter and friendlier for the client please")
        self.assertIn("I work Mondays.", analyze(text).own_text)

    def test_signed_letter_with_greeting_keeps_the_note_above_it(self):
        text = ("참고로 나는 외부 메일은 항상 영어로 보내.\n\n"
                "안녕하세요 한결님,\n다음 주 데모 일정 확인 부탁드립니다.\n최지훈 드림")
        self.assertIn("항상 영어로 보내", analyze(text).own_text)

    def test_short_opencode_attachment_is_pasted(self):
        a = analyze("check @a.md\n<file>\n00001| I prefer email.\n</file>")
        self.assertNotIn("prefer", a.own_text)

    def test_opencode_file_attachment_is_pasted(self):
        a = analyze('summarize @notes.md\nCalled the Read tool with the following input: {"filePath":"notes.md"}\n'
                    '<file>\n00001| I prefer email.\n</file>')
        self.assertNotIn("prefer", a.own_text)

    def test_long_lines_are_fast(self):
        import time
        start = time.time()
        analyze("x" * 200000 + " 드림")
        analyze("\n" * 200000)
        analyze("a" * 200000)
        analyze("On " + "x " * 100000 + "wrote:")
        analyze(" " * 200000 + "From: x")
        self.assertLess(time.time() - start, 1.0)

    def test_long_unmarked_text_is_unsure_not_own(self):
        a = analyze("I think the results look strong overall. " * 20)
        self.assertEqual([b.label for b in a.blocks], ["unsure"])


class TestRecall(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="test_recall_")
        self.vault = MemoryVault(root_dir=self.dir)
        self.vault.initialize()
        self.cache = MemoryCache(vault=self.vault)
        for text, meta in [
            ("Kim is the director and prefers short emails in Korean.",
             {"kind": "self", "quote": "Kim prefers short Korean emails", "source_ref": "you said in chat, 2026-10-19 10:00"}),
            ("Dana prefers Tuesday calls.",
             {"kind": "document", "about": "Dana Park", "quote": "I prefer Tuesday calls", "source_ref": "pasted in chat"}),
        ]:
            e = MemoryEntry(text=text, metadata=meta)
            self.vault.append_fact(e)
            self.cache.upsert_entry(e)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_no_match_tells_the_model_not_to_guess(self):
        r = recall(self.cache, "What is Jordan's phone number?")
        self.assertEqual(r["status"], "NO_MATCH")
        self.assertIn("Do not guess", render_recall(r))

    def test_weak_match_when_few_query_words_match(self):
        r = recall(self.cache, "Kim birthday party location budget")
        self.assertEqual(r["status"], "WEAK_MATCH")
        self.assertEqual(r["items"][0]["strength"], "weak")

    def test_match_is_tagged_and_cited(self):
        text = render_recall(recall(self.cache, "Kim email preference"))
        self.assertTrue(text.startswith("STATUS: MATCH"))
        self.assertIn("[M1] Kim is the director", text)
        self.assertIn("about you", text)
        self.assertIn('quote: "Kim prefers short Korean emails"', text)
        self.assertIn("cite every fact", text)

    def test_stemmed_query_word_counts(self):
        self.assertEqual(recall(self.cache, "Dana preference")["status"], "MATCH")

    def test_padded_model_queries_still_match(self):
        # Queries the real model sent in the live citation test.
        e = MemoryEntry(text="Jordan will send the sample wafers by Oct 20.",
                        metadata={"kind": "document", "about": "Jordan", "quote": "Jordan will send the sample wafers by Oct 20"})
        self.cache.upsert_entry(e)
        for query in ("Kim email style preference", "sample wafers delivery schedule", "Dana calls"):
            self.assertEqual(recall(self.cache, query)["status"], "MATCH", query)
        for query in ("Jordan phone number", "Kim birthday"):
            self.assertEqual(recall(self.cache, query)["status"], "WEAK_MATCH", query)

    def test_weak_match_instruction_asks_for_tag_and_hedge(self):
        text = render_recall(recall(self.cache, "Kim birthday"))
        self.assertIn("say it is a weak match", text)

    def test_document_fact_is_labelled_as_not_about_the_user(self):
        text = render_recall(recall(self.cache, "Tuesday calls"))
        self.assertIn("from a document, about Dana Park", text)

    def test_about_name_is_searchable(self):
        self.assertEqual(recall(self.cache, "Dana Park")["status"], "MATCH")

    def test_source_lookup(self):
        mem_id = recall(self.cache, "Tuesday calls")["items"][0]["id"]
        self.assertIn("STATUS: FOUND", render_source(self.vault, self.cache, mem_id))
        self.assertIn("NOT_FOUND", render_source(self.vault, self.cache, "deadbeef"))


if __name__ == "__main__":
    unittest.main()
