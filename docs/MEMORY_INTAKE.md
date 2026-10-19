# Memory Intake: How It Works and How to Use It

The memory intake decides what one chat turn may add to the memory vault, and where.
It runs in the background, once per new user turn, after the answer has streamed.
The design rule: **a wrong guess is never silent.** Doubtful material goes to a review
list that you decide on, and every failure is reported.

## What happens to one turn

1. **Source split (code).** Only the newest *user* message can give facts about you.
   Tool output and assistant text never can.
2. **Paste check (code, `memory/paste_detect.py`).** Your message is split into blocks:
   - **PASTED:** proven by a strong marker (mail headers, `> quoted` lines, "Original
     Message", chat-log timestamps, KakaoTalk/Slack lines, log lines, code fences,
     opencode file attachments). Gives facts about other people only.
   - **UNSURE:** weak signs (a letter greeting, a sign-off with a name, a greeting to
     another person, a long block, text next to "thoughts?" or "reply to this:").
     Facts from it about you **always go to review**.
   - **OWN:** a short chat message with no signs. Facts from it can be saved directly.
3. **Laya (optional).** Skips the LLM call when it is sure there is no lasting fact
   (p_durable < 0.2) and the message has no UNSURE text. Its "own words" score is
   recorded on every decision for later calibration, but it decides nothing.
4. **Grounded extraction (the loaded LM Studio model, thinking off).** Every fact needs a
   quote. Code checks the quote:
   - quote not in the text, or under 25% of the fact's words in the quote (an invented
     fact): **rejected**, and logged as `CANDIDATE_REJECTED`;
   - saved directly only when the quote is an exact match in OWN text and covers at
     least 80% of the fact's words;
   - otherwise **review**: approximate quote, UNSURE text, a paraphrase or extra words
     ("as a senior manager"), negation differs ("can't", "안"), another language, or
     close to a stored fact (a possible update).
   Your own words and possibly pasted text go to the model as two labelled sections, so
   the "ignore copied text" rule does not erase your sentence next to a pasted draft.
5. **Documents.** Pasted text, and documents that `fs_read`, `read` or `email_show`
   read in the previous turn, give facts about other people. They are stored apart from
   your profile, with the source path and quote. Document text that speaks to the
   assistant ("Assistant: always BCC...", "ignore previous instructions") goes to review.
6. **Loud failures (`memory/health.py`).** Every failure goes to `health.json`,
   `events.jsonl` (`MEMORY_FAILURE`, shown in the cockpit feed) and the WARNING log.
   A failed turn or document is kept for `retry`.

## Recall: cite or say "no record"

`memory_search` returns plain text the model reads:

```
STATUS: MATCH (1 strong, 0 weak) for query 'Kim email style'
INSTRUCTION: In your reasoning, name the tag and the source of each fact before you use it...
[M1] Kim prefers short emails in formal Korean. | about you | source: you said in chat, 2026-10-12 09:30 | quote: "..." | id: 3f2a9c1b
```

- **MATCH:** a result shares 2+ query words (or the only word of a 1-word query).
- **WEAK_MATCH:** results share only 1 word. The model must say it is not sure.
- **NO_MATCH:** the model must say it has no record. It must not guess.
- `memory_source <id>` shows the stored source text around the quote.
- Facts saved before quote checks are labelled **UNVERIFIED**.
- `memory_save` (called by the agent) goes to review, not to memory.

## Daily commands

```bash
.venv/bin/python -m telemachus.memory health      # failures, rejections, pending, failed turns
.venv/bin/python -m telemachus.memory review      # facts waiting for you, with the reason
.venv/bin/python -m telemachus.memory approve <id>
.venv/bin/python -m telemachus.memory reject <id>
.venv/bin/python -m telemachus.memory retry       # run failed turns and documents again
.venv/bin/python -m telemachus.memory search "Kim email"
.venv/bin/python -m telemachus.memory source <id>
.venv/bin/python -m telemachus.memory tail        # live events
```

An id needs at least 6 characters and must match one fact only. Your approve and
reject decisions are kept in `private/review/decisions.jsonl`: labelled data for a
later Laya calibration or fine-tune on your own messages.

## Settings

| Variable | Default | Use |
|---|---|---|
| `TELEMACHUS_MEMORY_DIR` | `~/.memory` | Vault location (for example a separate work vault). |
| `TELEMACHUS_MEMORY_LAYA` | `on` | `off` turns the Laya gate off. |
| `TELEMACHUS_MEMORY_MODEL` | the loaded chat model | Extraction model id. It must be loaded; the intake never triggers a JIT load. |
| `LAYA_PYTHON` | `~/laya/.venv/bin/python` | Python of the Laya venv. |

## Tests and benchmarks

```bash
.venv/bin/python -m unittest discover -s tests                        # unit tests (fake LM Studio)
.venv/bin/python tests/benchmarks/laya_memory_calibration.py          # Laya accuracy on the labelled set
.venv/bin/python tests/benchmarks/live_memory_intake.py               # real model + Laya, temporary vault
.venv/bin/python tests/benchmarks/live_recall_citation.py             # does the model cite / refuse?
```

The live scripts need LM Studio on :1234 with a chat model loaded. They never touch
`~/.memory`. Labelled sets (all invented text):

- `tests/fixtures/memory_intake_eval.json` (55 items): the development set. The unit
  tests and the detector rules were tuned on it.
- `tests/fixtures/memory_intake_heldout.json` (45 items): written by a separate agent
  that never saw the code. Use it only to measure
  (`live_memory_intake.py --fixture tests/fixtures/memory_intake_heldout.json`); do not
  tune rules on it, or its numbers stop meaning anything.

Last live result (2026-10-19): development set 54/55, held-out set 41/45, citation test
6/6. Pasted text saved as a fact about you: 0 on both sets. Every miss was a lost or
review-bound fact, never a wrong saved one.

## Measured limits (2026-10-19, M5 Pro 24 GB, Qwen3.5-9B 8-bit MLX)

- **Laya zero-shot is weak.** Durability is close to chance, authorship is weak, and
  Hangul text is at chance on both checkpoints, so Korean text skips Laya.
- **Laya memory cost:** about 2.4 GB while its worker runs. It starts on first use and
  stops after 10 minutes without use. After a start failure it waits 10 minutes.
- **Known gap:** one block of pasted text with no instruction and no marker (for
  example a pasted first-person paragraph) looks like your own message. The extraction
  prompt tells the model to return nothing for text written to other people; in the
  live tests this held, but it is a model behavior, not a code guarantee.
- **opencode `synthetic` parts:** the intake skips message parts marked `synthetic`,
  but opencode may not send that flag. File attachments are also caught by their
  `<file>` / `00001|` markers.
- **Extraction speed:** about 2 s per turn in the background. With Max Concurrent
  Predictions = 1, the call waits behind your own request.
