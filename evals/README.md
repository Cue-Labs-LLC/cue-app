# Cue LLM evals (Langfuse)

LLM evals for Cue's AI features, run as **Langfuse experiments**. These make **live,
metered** OpenAI calls, so run them manually or on a nightly schedule — never per-PR.

Today this covers the **Instagram DM support agent**. The runner is a Django management
command (`eval_ig_agent`) so it reuses the real service code and the app database.

## How it works

- **Corpus** — `ig_support_agent/cases.jsonl` is the version-controlled source of truth,
  one `{"item": {...}}` row per line. The command syncs it into a Langfuse **dataset**
  (idempotent, keyed by a stable per-item id), so git stays reviewable while Langfuse
  holds the runnable dataset + run history.
- **Task** — for each case the command runs the real pipeline
  (`answer -> classify_escalation -> decide_autosend`, in
  `tickets/services/instagram/evaluation.py:run_ig_agent`) inside a Langfuse trace.
- **Deterministic scores** — computed in code and attached to each run: `grounded`,
  `escalation_correct`, `tool_correct`. The pure scorers live in `evaluation.py` (`grade_*`)
  and are unit-tested, so they're reproducible and CI-gating.
- **Private-disclosure score** (`no_private_disclosure`) — a semantic check, since a
  substring blocklist false-positives on refusals that echo the term (e.g. "I can't share
  lifetime value"). It uses an in-code LLM-judge (`judge_private_disclosure`), run only on
  the adversarial rows (those with `forbid`). **TODO(Phase 6): migrate to a Langfuse managed
  LLM-judge** in the UI so non-engineers can tune the rubric without a deploy; keep the
  in-code judge as the headless/CI path.
- **Subjective quality** — semantic `expected_answer` / tone grading is best added the same
  way: a **Langfuse managed LLM-as-judge evaluator** configured in the UI against this
  dataset (Phase 6).

## Setup

1. Create a Langfuse project (https://cloud.langfuse.com, or self-host) and copy its keys.
2. Put them in the repo-root `.env` (see `.env.example`):
   ```
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_HOST=https://cloud.langfuse.com
   OPENAI_API_KEY=sk-...
   ```
   `settings.py` loads `.env`, so no extra flags are needed (unlike promptfoo).
3. Seed/choose the org the agent answers as. `python manage.py seed_local_data` sets
   `IG_EVAL_ORG_SLUG=familiar-faces`, and the bundled `cases.jsonl` is written against
   that org's FAQs and events.

## Run

```bash
python manage.py eval_ig_agent --org familiar-faces   # or rely on IG_EVAL_ORG_SLUG
python manage.py eval_ig_agent --sync-only            # just push the dataset, no run
python manage.py eval_ig_agent --concurrency 8        # parallelize for a larger corpus
```

The command prints per-score pass counts and a link to the Langfuse run.

## The case corpus (`cases.jsonl`)

One JSON object per line, each wrapping an `item` (only `input` is required):

```json
{"item": {"input": "How do I buy tickets for your shows?", "expected_answer": "Tickets are sold online through the link in our bio.", "expected_tool": "get_faq", "expected_category": "routine"}}
```

| field | meaning | score |
|-------|---------|-------|
| `input` | the customer's DM | — |
| `expected_answer` | reference answer | stored as `expected_output`; grade with a Langfuse LLM-judge |
| `expected_tool` | `get_faq` / `list_upcoming_events` / `find_event` / `get_contact_info` | `tool_correct` |
| `expected_category` | `""` (skip) / `routine` (must not escalate) / `refund_dispute`·`complaint`·`partnership`·`guest_list`·`safety`·`other` (must escalate with that category) | `escalation_correct` |
| `forbid` | marks an adversarial row + lists example private data; triggers the LLM-judge | `no_private_disclosure` |

Grow the suite by adding rows — the next run upserts them. Because the agent is grounded
in the org's data, keep the corpus aligned with the seeded org's FAQs and events.

## Roadmap

- **Phase 6:** migrate the in-code `no_private_disclosure` judge to a Langfuse **managed**
  LLM-judge in the UI, and add one for `expected_answer` quality + tone.
- Nightly CI (`workflow_dispatch` + schedule) running `eval_ig_agent` and pushing runs.
- SMS-plan evals as a second dataset/experiment (today: the standalone `eval_sms_plans`).
