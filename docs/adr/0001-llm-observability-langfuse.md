# ADR 0001 — Standardize LLM observability + evals on Langfuse

- **Status:** Accepted
- **Date:** 2026-10-08
- **Deciders:** Eng lead (AI platform)
- **Supersedes:** ad-hoc LangSmith env-only tracing

## Context

The app runs several LangChain/LangGraph agents that touch customer data:

- the **Instagram DM support agent** + escalation **classifier** (customer DMs — PII, and
  potentially minors' data),
- the organizer **chat agent**,
- the **SMS strategist** generation.

Two LLM-observability tools were present:

- **Langfuse** — an explicit dependency (`langfuse>=3.0`) powering the version-controlled
  **eval harness** for the IG agent (`eval_ig_agent`): datasets, experiments, and managed
  LLM-as-judge scorers.
- **LangSmith** — not a declared dependency and not imported anywhere; purely an **env
  toggle** (`LANGSMITH_TRACING`) that LangChain reads to stream traces to LangSmith Cloud.
  Documented as a *global* switch covering the SMS eval judge, chat agent, and strategist.

Two external LLM-observability vendors is vendor sprawl, and the LangSmith path had a
governance gap: flipping one env var exfiltrates prompts + customer DM content to a second
third-party cloud with no code review. Production DM runs were **not traced at all** (Langfuse
was eval-only), so operators could not inspect why a live conversation escalated.

## Decision

Standardize on **Langfuse** as the single LLM-observability + eval platform, and remove
LangSmith as a sanctioned tool.

1. Add `tickets/services/ai_tracing.trace_config()` — a reusable helper that attaches a
   Langfuse `CallbackHandler` (with `run_name`, `tags`, `session_id`, `metadata`) to a
   LangChain `.invoke`/`.stream`. It **no-ops** unless `LANGFUSE_PUBLIC_KEY` +
   `LANGFUSE_SECRET_KEY` are set, so it is safe to leave wired in every environment.
2. Instrument the production call sites that LangSmith nominally covered: IG answer agent +
   classifier (grouped per conversation via `langfuse_session_id`), chat agent, SMS strategist.
3. Remove `LANGSMITH_*` from `.env.example` and CLAUDE.md so external tracing is no longer a
   silent env flip but a reviewed, code-level integration pointed at one destination.

## Rationale (why Langfuse, not LangSmith)

- **Data residency / PII (decisive):** traces contain customer DM content. Langfuse is
  open-source and **self-hostable** (`LANGFUSE_HOST`), so traces can stay inside our own
  boundary. LangSmith is SaaS-first.
- **Close the ungoverned-egress gap:** enabling external tracing now requires a deliberate,
  reviewed change rather than a one-line env toggle.
- **One governed standard:** a single vendor = one security review, one DPA, one SBOM entry,
  one dashboard/runbook, lower onboarding cost.
- **Leverage what exists:** the eval harness already runs on Langfuse; tracing + evals in one
  tool is a cleaner operational story.

## Consequences

- We lose LangSmith's *zero-code* env-only tracing; tracing is now explicit via
  `trace_config()` at each call site (small, reviewable additions).
- New call sites must opt in by spreading `**trace_config(...)` into their Runnable call.
- Before enabling in production, decide **self-host vs. cloud** with security/legal, given the
  PII in traces. Default `LANGFUSE_HOST` is the public cloud — override it for in-boundary.
- LangChain can still technically emit to LangSmith if someone sets `LANGCHAIN_TRACING_V2`
  directly; that is now undocumented/unsanctioned and should be caught in config review.

## Alternatives considered

- **Keep both:** rejected — vendor sprawl + ungoverned second data-egress path for marginal
  convenience.
- **Standardize on LangSmith:** rejected — SaaS-first (weaker data-residency story) and would
  require rebuilding the existing Langfuse eval harness.
- **OpenTelemetry-only, backend-agnostic:** attractive for portability and a good future step
  (Langfuse ingests OTel). Deferred to keep this change small; `trace_config()` centralizes the
  wiring so a later OTel swap touches one file.
