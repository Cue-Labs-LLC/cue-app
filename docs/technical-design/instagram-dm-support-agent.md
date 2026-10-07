# Technical Design Document — Instagram DM AI Support Agent

**Status:** Approved · **Scope:** new feature + eval-tooling standardization · **Delivery:** phased, test-first (each phase independently shippable)

> This is a TDD, not a single-shot build. Each phase below lists the tests to write **first**, the implementation scope, and acceptance criteria. Phases land in order; most are demoable on their own. This document is the living guide for the phased implementation — each phase maps to a PR; check off phases in the Progress tracker (§8) as they land.

---

## 1. Context & problem

Organizers field the same Instagram DMs constantly — "when/where is it," "is it sold out," "can I get a refund." We want an AI support agent that reads an incoming DM, **auto-answers** routine FAQ questions, and **escalates** sensitive ones (refunds, complaints, safety, partnerships, guest list) to a human via an in-app inbox + notification.

The codebase is well-prepared: an existing LangGraph ReAct agent (`tickets/services/chat/`) with org-scoped tools, a Meta OAuth integration (`meta_ads`, same Facebook app Instagram messaging uses), an inbound-webhook + HMAC pattern (`typeform_webhook`, `twilio_sms_inbound_webhook`), an integration registry, and an `OrganizationAPIKey` whose docstring names "Instagram DM Agent." A static markdown KB (`tickets/kb/`) from a **legacy** attempt is deleted as part of this work.

### Goals
- Auto-answer high-confidence FAQ DMs; escalate the rest with a human-review inbox.
- Multi-tenant: per-org editable FAQ + per-org opt-in.
- Demoable before Meta App Review clears (stub transport).
- Standardize all AI/LLM evaluation on Langfuse (hosted datasets + experiment runs).

### Non-goals
- Linking IG senders to `Customer` records (no IG handle on `Customer` today — left null, future work).
- Rich media / story replies / comment automation (text DMs only in v1).
- Outbound proactive messaging (reply-only; respects Meta's 24h window).

## 2. Confirmed decisions
- **Transport:** transport-agnostic core — abstract *sender* now, **StubSender** for demo, native **Meta Instagram Messaging API** as production target behind App Review.
- **Autonomy:** hybrid by confidence — auto-send high-confidence routine answers; queue the rest.
- **Escalation:** in-app Cue inbox **and** notification (email via existing SendGrid + APNs push).
- **Knowledge:** per-org **editable FAQ** model + settings UI; agent reads org FAQ + live `Event` data.
- **Evaluation:** standardize all **LLM** evals on **Langfuse** (hosted datasets + experiment runs) — the new IG agent (built eval-first) and the existing `eval_sms_plans`. The deterministic, non-LLM `validate_segments` backtest is **explicitly out of scope** and stays as its own standalone management command (Langfuse adds no value to a pure statistical backtest). See Phase 6. *(Amendment 2026-10-06: this track was originally promptfoo; switched to Langfuse. Deterministic scores are computed in code; subjective answer/tone quality is a Langfuse managed LLM-as-judge evaluator configured in the UI.)*

## 3. Architecture at a glance

Inbound IG DM → Meta webhook (`webhooks/instagram/`, verify `X-Hub-Signature-256`) → normalize → Celery task → persist inbound → ReAct answer agent (org FAQ + event tools) → structured escalation classifier → **auto-send** (sender abstraction) **or** **queue** (draft) → on escalation fire email + push → human resolves in in-app inbox.

New service code: `tickets/services/instagram/` (mirrors `tickets/services/chat/`). Views: `tickets/integrations/instagram.py`.

---

## 4. Detailed design reference

Shared reference the phases point to. (Models use existing `BaseModel`/`AuditBaseModel`; all queries org-scoped per the multi-tenancy rule.)

### 4.1 Data model (`tickets/models.py`)
- **`OrgFAQ(AuditBaseModel)`** — per-org Q&A. `organization` FK CASCADE `related_name='faqs'`; `question` CharField(300); `answer` TextField; `topic` CharField(60, blank, db_index); `is_published` Bool(True); `sort_order` Int(0). `Meta.indexes=[Index(['organization','is_published'])]`, `ordering=['sort_order','created_at']`.
- **`InstagramConversation(BaseModel)`** — one thread per (org, IG user). (Can't reuse `ChatMessage`: its `user` FK→auth.User is non-null.) `organization` FK CASCADE `related_name='ig_conversations'`; `ig_user_id` CharField(64, db_index); `ig_username` CharField(80, blank); `customer` FK→Customer null SET_NULL; `status` `open|awaiting_human|resolved` (default `open`); `last_message_at` DateTime(db_index); `assigned_to` FK→auth.User null SET_NULL. `Meta.unique_together=[('organization','ig_user_id')]`, `indexes=[Index(['organization','status','last_message_at'])]`.
- **`InstagramMessage(BaseModel)`** — inbound+outbound timeline; `status` encodes auto-send/queued/escalation (no separate escalation model). `conversation` FK CASCADE `related_name='messages'`; `organization` FK CASCADE (denormalized); `direction` `inbound|outbound`; `author` `customer|agent|human`; `content` TextField(blank); `provider_message_id` CharField(128, blank, db_index) (Meta `mid`, dedupe); `status` `received|auto_sent|pending_review|approved_sent|failed|discarded` (default `received`); `confidence` Float null; `escalation_category` CharField(40, blank); `escalation_reason` CharField(300, blank); `reviewed_by` FK→auth.User null SET_NULL; `reviewed_at` DateTime null; `token_count` Int(0). `Meta.ordering=['created_at']`, `indexes=[Index(['conversation','created_at']), Index(['provider_message_id'])]`. **(D5)** partial `UniqueConstraint(['organization','provider_message_id'], condition=~Q(provider_message_id=''))` for retry-safe idempotency. **(D6)** `save()` sets `organization_id` from `conversation` so the two can't diverge (tenancy invariant).
- **`AITokenUsage`** constant (~models.py:3157): add `FEATURE_IG_SUPPORT_AGENT='ig_support_agent'` + choice; distinguish calls via `metadata={'stage':'answer'|'classify'}`.
- **`Organization`** fields (beside `meta_ads_*`): `instagram_page_access_token` CharField(512); `instagram_business_account_id` CharField(64); `instagram_page_id` CharField(64); `instagram_username` CharField(80); `instagram_token_expires_at` DateTime null; `instagram_support_agent_enabled` Bool(False). **(D11)** `instagram_business_account_id` carries a partial `UniqueConstraint` (where non-blank) — inbound webhooks route to an org by it, so it must map to at most one org.

### 4.2 Key interfaces (`tickets/services/instagram/`)
- `sender.py`: `@dataclass SendResult(ok, provider_message_id='', error='')`; `InstagramSender(ABC).send_text(recipient_id, text)->SendResult`; `get_sender(organization)` selects **by `settings.INSTAGRAM_SENDER_BACKEND`** (`stub`|`graph`), **not by token presence (D10)** — when backend is `graph` and the token is missing/expired, it must mark the send `failed` loudly, never silently fall back to `StubSender` (that would mark messages `auto_sent` while the customer gets nothing).
- `graph_client.py`: `InstagramGraphClient` + `GraphAPISender`. Send API is **POST** `graph.facebook.com/{FACEBOOK_GRAPH_API_VERSION}/{IG_ID}/messages` `{recipient:{id},message:{text}}` + token. **Do not reuse `MetaAdsClient` (GET-only).** New `requests.post` client; no new dependency.
- `inbound.py`: `@dataclass NormalizedInbound(ig_account_id, sender_id, text, provider_message_id, timestamp)`; `normalize_meta_payload(body)->list[NormalizedInbound]` (walks `entry[].messaging[]`, skips echoes/reactions/read-receipts/non-text). Org lookup by `instagram_business_account_id`.
- `tools.py` — **a dedicated, minimal, customer-safe tool set. Do NOT import or reuse any helper from `tickets/services/chat/tools.py`** — those are organizer/MCP-internal and leak private data (`_search_events` returns per-event revenue + order counts; `_get_event_detail` returns a full P&L: revenue, expenses, net profit, expense breakdown; others expose customer PII, LTV, RFM segments). The IG agent gets purpose-built helpers that select only explicitly public fields. `build_ig_tools(organization)` returns **exactly this allowlist**, org-bound via closure:
  - `get_faq(topic: str = "")` — published org FAQ (`OrgFAQ.objects.filter(organization=org, is_published=True, deleted_at__isnull=True)`, optional topic/question icontains).
  - `list_upcoming_events(limit: int = 5)` — `status='published'`, `deleted_at__isnull=True`, future events. Per event ONLY: name, date, start time (+tz), venue name & city, ticket link. No counts/revenue/capacity.
  - `find_event(query: str)` — published events matched by name/city/date. Returns the same public fields plus published `summary`/`description`, `ticket_link`, and a derived availability **hint** ("tickets available" / "check the link") with **no numbers**.
  - `get_contact_info()` *(optional)* — public org contact only (name, website/`instagram_url`, support email if such a field exists) for routing/escalation phrasing.
  - **Hard rule:** no tool returns money, order/customer counts, capacity numbers, other customers' data, PII, LTV/RFM, or internal config (pixel ids, scanner pin, tokens); draft/deleted events are never surfaced. A test pins the exact tool-name allowlist so an internal tool can't be added by accident, plus output-scrubbing tests (see Phase 2).
- `prompts.py`: customer-facing system prompt + escalation taxonomy (behavioral content migrated from the deleted `kb_*.md`).
- `agent.py`: `InstagramSupportAgentService(organization)` (no user). `answer(conversation, inbound_text)->AnswerResult(text, usage, tool_calls)` via non-streaming `create_react_agent(ChatOpenAI(OPENAI_MODEL, temperature=0.3), build_ig_tools(org)).invoke(...)`; meters `record_ai_token_usage(feature=FEATURE_IG_SUPPORT_AGENT, user=None, metadata={'stage':'answer'})`. **(D12)** feeds the last-N `InstagramMessage` rows of the conversation into the prompt (multi-turn history); returns which tools fired (for the D14 groundedness gate).
- `classifier.py`: `class EscalationDecision(BaseModel){should_escalate, confidence(0..1), category:Literal['routine','refund_dispute','complaint','partnership','guest_list','safety','other'], reason}`; `classify_escalation(org, question, draft_answer)` via `ChatOpenAI(temperature=0).with_structured_output(EscalationDecision, include_raw=True)` (pattern from `sms_strategist.py`), meters stage=`classify`. Gate: `auto_send = (not should_escalate) and category=='routine' and confidence >= settings.IG_AGENT_AUTOSEND_MIN_CONFIDENCE` **and grounded (D14)** and **under the daily cap (D4)**. **(D14) groundedness:** the answer must be derived from a successful `get_faq`/`find_event`/`list_upcoming_events` tool call (`AnswerResult.tool_calls` non-empty); a "no tool hit" answer is treated as not-auto-sendable → queue for review (a fluent answer with no tool grounding is the hallucination shape). **(D4) daily cap:** if the org is at `settings.IG_AGENT_DAILY_ANSWER_CAP` auto-answers for the day, queue instead of auto-send.

### 4.3 Config (`ltv_updater/settings.py`)
Reuse `FACEBOOK_APP_ID/SECRET`, `FACEBOOK_GRAPH_API_VERSION`, `OPENAI_MODEL`, SendGrid. Add `INSTAGRAM_WEBHOOK_VERIFY_TOKEN`, `INSTAGRAM_SENDER_BACKEND` (`'stub'`|`'graph'`, default `'stub'`; D10 selects the sender from this, never from token presence), `IG_AGENT_AUTOSEND_MIN_CONFIDENCE` (float, default `0.8`), `IG_AGENT_DAILY_ANSWER_CAP` (int, default e.g. `200`; D4 — 0 disables the cap), `IG_AGENT_COALESCE_WINDOW_SECONDS` (int, default e.g. `8`; D12 debounce). For the eval harness add `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/`LANGFUSE_HOST` (default `https://cloud.langfuse.com`) — eval-only. One new Python dep: `langfuse` (eval harness only; the agent/classifier runtime adds none).

---

## 5. Phased delivery (test-first)

### Phase 0 — Design doc + data model & foundations
- **Step 1 (doc):** this document committed at `docs/technical-design/instagram-dm-support-agent.md` — the guiding artifact for everything below.
- **Tests first** (`tickets/tests_instagram.py`): `OrgFAQ` soft-delete + published default; `InstagramConversation` unique `(org, ig_user_id)`; `InstagramMessage` status/direction defaults + `provider_message_id` index; org-scoping sanity.
- **Build:** the three models + `AITokenUsage` constant + six `Organization` fields; one additive migration (no data migration).
- **Accept:** doc committed; `makemigrations` clean, `migrate` applies, model tests green. No user-facing change.

### Phase 1 — Per-org FAQ editor (settings)
- **Tests first:** FAQ CRUD org-scoping + `require_admin`; `is_published`/`sort_order` behavior; unpublished excluded from the published queryset.
- **Build:** `OrgFAQForm` (crispy) in `forms.py`; `instagram_faq_list/create/edit/delete` in `tickets/integrations/instagram.py`; templates extend `base.html`; URLs under `settings/integrations/instagram/faq/...`; registry entry (`key='instagram'`, `is_connected=lambda org: bool(org.instagram_page_access_token and org.instagram_business_account_id)`).
- **Accept / demo:** organizer adds/edits/reorders FAQs in settings; integration card shows "not connected."

### Phase 2 — Answer pipeline (offline, no transport)
- **Tests first (tool safety is the priority):**
  - **Allowlist pin:** `{t.name for t in build_ig_tools(org)}` equals exactly `{get_faq, list_upcoming_events, find_event, get_contact_info}` — fails if any internal tool is added.
  - **Output scrubbing:** seed an org with a published event that has revenue/expenses/orders + customers with LTV/RFM; assert IG tool outputs contain none of it (no `$`, "Revenue"/"Profit", no customer emails/names, no LTV/segment strings, no capacity numbers).
  - **Visibility:** `list_upcoming_events`/`find_event` never return draft or soft-deleted events, and only future events for the listing; `get_faq` returns only published, org-scoped FAQs.
  - `agent.answer` with mocked `ChatOpenAI` returns text + usage; `classify_escalation` gating (routine high-confidence → auto; refund → escalate) with mocked structured output; two `AITokenUsage` rows (`feature='ig_support_agent'`, `user=None`, stages `answer`/`classify`).
- **Build:** `tickets/services/instagram/` package: `prompts.py`, `tools.py` (the dedicated customer-safe tools in §4.2 — no imports from chat `tools.py`), `agent.py`, `classifier.py`. Migrate `kb_*.md` behavioral content into prompts here. **Eval-first harness lands here too:** `evaluation.py` (harness-agnostic `run_ig_agent` + pure `grade_*` scorers), the `eval_ig_agent` Langfuse experiment command, and the `evals/ig_support_agent/cases.jsonl` corpus (routine / sensitive / **D7 adversarial prompt-injection** cases).
- **Accept / demo:** `answer_ig_faq` management shim calls `answer()` + `classify_escalation()` + the auto-send gate over a question and prints the decision. `eval_ig_agent --sync-only` pushes the dataset; a full run scores a seeded org. No sending.

### Phase 3 — Transport + inbound + orchestration + webhook
- **Tests first:** webhook GET verify (match/mismatch), POST signature valid/invalid (`X-Hub-Signature-256`); `normalize_meta_payload` (echo/reaction skip, dedupe by `provider_message_id`); end-to-end with `INSTAGRAM_SENDER_BACKEND='stub'` → routine → `auto_sent` row + stub send called; refund → `pending_review` + `conv.status='awaiting_human'`; idempotency (no double-send on webhook/Celery retry).
- **Build:** `sender.py` (interface, `StubSender`, `get_sender`), `inbound.py`, `process_instagram_inbound_task` (`@shared_task(bind, max_retries=2)`), `instagram_webhook` (`@csrf_exempt`, GET verify + POST signature + enqueue), URLs; a small admin "simulate inbound DM" action for demoing without Meta.
- **Accept / demo:** POST a synthetic signed payload (or use the simulate action) → routine auto-"sent" via stub, sensitive queued.

### Phase 4 — Inbox UI + escalation notifications
- **Tests first:** inbox list/detail org-scoping + `require_admin`; draft approve sends via sender and flips `pending_review`→`approved_sent` (sets `reviewed_by/at`); human reply creates `author='human'` outbound; escalation enqueues `notify_instagram_escalation_task`.
- **Build:** `instagram_inbox`, `instagram_conversation_detail`, `instagram_message_send`, `instagram_draft_approve` views + templates; `notify_instagram_escalation_task` (email via SendGrid + push via `push_notifications.dispatch` with new `INSTAGRAM_ESCALATION` payload); inbox/conversation URLs.
- **Accept / demo:** escalated DM appears in inbox, email+push fire, organizer approves/edits a draft and it "sends" (stub).

### Phase 5 — Remove legacy KB
- **Tests first:** chat `build_tools` still constructs; grep guard that nothing imports the KB.
- **Build:** delete `tickets/kb/*` + dir; remove `_get_knowledge_base`, the `get_knowledge_base` `@tool`, its list entry, and unused `import os` in `tickets/services/chat/tools.py`. (Behavioral content already re-homed in Phase 2 prompts.)
- **Accept:** suite green; analytics chat agent unaffected except loss of the KB tool.

### Phase 6 — Langfuse eval standardization
The IG-agent eval harness itself **already landed with Phase 2** (eval-first): the
`eval_ig_agent` command, `evaluation.py` (`run_ig_agent` + pure `grade_*` scorers), and
the `evals/ig_support_agent/cases.jsonl` corpus, syncing to a Langfuse dataset
(`ig-support-agent`) and attaching deterministic boolean scores (`grounded`,
`escalation_correct`, `tool_correct`) plus an in-code LLM-judge score
(`no_private_disclosure`) for the D7 adversarial cases. Phase 6 is what remains to
*standardize*:
- **Build:**
  - **Managed LLM-judge:** configure Langfuse LLM-as-judge evaluators in the UI against the `ig-support-agent` dataset, and **migrate the in-code `no_private_disclosure` judge** to a managed one (non-engineers tune the rubric without a deploy; the in-code judge stays as the headless/CI path). Add one for `expected_answer` quality + tone too.
  - **CI:** `.github/workflows/evals.yml` on `workflow_dispatch` + nightly `schedule` (live metered calls → never per-PR), with `LANGFUSE_*` + `OPENAI_API_KEY` secrets, running `python manage.py eval_ig_agent`. LangSmith env tracing stays.
  - **SMS plans — SEPARATE TRACK (D1):** migrate `eval_sms_plans` (`grade_plan`/`_judge`/`_judge_agent`) onto Langfuse as a second dataset/experiment, retiring the runner after parity. **Its own branch/effort**, not part of this feature. Not blocked by and does not block the IG agent.
  - **Out of scope:** `validate_segments` is **not** migrated — it's a deterministic, non-LLM statistical backtest and stays its own standalone management command (`SegmentDiagnostics` unchanged). Langfuse covers LLM evals only.
  - (Optional — event-summary quality via `EventSummaryService.generate_summary`, same pattern, if you want AI debrief quality tracked too.)
- **Accept:** `eval_ig_agent` run appears in Langfuse with scores + the managed judge; SMS parity vs the old command confirmed before deleting its runner.

### Phase 7 — Meta OAuth + Graph sender (production, gated by App Review)
- **Tests first:** OAuth `state` CSRF + callback persists `instagram_*` fields (mock Graph); `GraphAPISender.send_text` POST (mock `requests`); send failure → `status='failed'` surfaced in inbox; token-expiry reconnect prompt.
- **Build:** `instagram_settings/connect/callback/disconnect/toggle_agent` (mirror `meta_ads.py`; scopes `instagram_basic, instagram_manage_messages, pages_messaging, pages_manage_metadata, pages_show_list, business_management`; reuse `exchange_code_for_token`/`exchange_for_long_lived_token`; subscribe Page to `messages` via `POST /{page_id}/subscribed_apps`); `InstagramGraphClient` + `GraphAPISender`; callback URL at `settings/instagram/callback/`.
- **Rollout:** submit App Review (external, multi-week); flip `INSTAGRAM_SENDER_BACKEND='graph'` per env once approved; `instagram_support_agent_enabled` stays off by default (orgs opt in after reviewing FAQ quality).
- **Accept:** real IG DM round-trip in a Meta-approved environment.

**Demo milestones:** Phases 1–6 are fully demoable on the stub backend pre-App-Review; Phase 7 is the only one blocked on Meta.

---

## 6. Risks
- **Graph Send API is POST** — new client, not `MetaAdsClient` (GET-only).
- **24h messaging window:** human inbox replies may fall outside Meta's window → surface Graph errors as `status='failed'`.
- **Retries/double-send:** Meta + Celery both retry → dedupe on `provider_message_id`, idempotent auto-send.
- **Data leakage / prompt injection:** inbound DM is untrusted and the agent speaks to the public, so the tool surface is the main attack surface. Mitigate with the dedicated allowlisted customer-safe tools (§4.2 — never the organizer/MCP chat tools), org bound via closures (LLM never picks the org), the allowlist-pin + output-scrubbing tests, and classifier `temperature=0`. No customer/revenue/financial/PII tool is ever reachable from this agent.
- **Customer identity:** no IG handle on `Customer` → `conversation.customer` null initially.
- **Eval cost/flakiness:** Langfuse experiment runs make live/metered LLM calls and the agent is non-deterministic → manual/nightly only; track pass-rate trends across runs rather than gating on a single run.
- **Grader migration parity:** confirm Langfuse reproduces current SMS pass/fail before deleting the `eval_sms_plans` runner.
- **App Review timeline** external — Phases 1–6 intentionally don't depend on it.

## 7. Run commands
`python manage.py makemigrations tickets && python manage.py migrate && python manage.py test tickets` → `python manage.py eval_ig_agent --org <slug>` (needs `LANGFUSE_*` + `OPENAI_API_KEY`; `--sync-only` to just push the dataset) → view the run in Langfuse.

## 8. Progress tracker
- [x] **P0** — Design doc committed + data model + migration
- [x] **P1** — Per-org FAQ editor (settings) + registry entry
- [ ] **P2** — Answer pipeline + customer-safe tools + classifier (tool-safety tests) + Langfuse eval harness (eval-first)
- [ ] **P3** — Transport abstraction + StubSender + inbound + orchestration + webhook
- [ ] **P4** — Inbox UI + escalation notifications (email + push)
- [ ] **P5** — Remove legacy KB
- [ ] **P6** — Langfuse eval standardization (managed LLM-judge + CI; SMS plans; segments excluded). IG-agent harness itself shipped in P2.
- [ ] **P7** — Meta OAuth + Graph sender (gated by App Review)

## 9. Review hardening (from /plan-eng-review, 2026-10-05)

Decisions from the engineering review, mapped to where they land. P0 items are already implemented + tested.

| # | Decision | Lands in | Status |
|---|----------|----------|--------|
| D1 | Split the `eval_sms_plans` Langfuse migration into its own track; IG-agent eval stays (Phase 2) | Scope / P6 | folded |
| D4 | Per-org daily auto-answer cap (`IG_AGENT_DAILY_ANSWER_CAP`) → queue past cap | P3 | planned |
| D5 | Partial-unique `provider_message_id` for retry-safe idempotency | **P0** | **done** |
| D6 | `InstagramMessage.save()` coerces org from conversation (tenancy invariant) | **P0** | **done** |
| D7 | Adversarial prompt-injection cases in the IG-agent eval | P2 | planned |
| D9 | `HUMAN_AGENT` tag + permission for escalated/human replies; inbox window indicator | P4 + P7 | planned |
| D10 | `get_sender()` selects by `INSTAGRAM_SENDER_BACKEND`, never stub-send in prod; missing token → loud `failed` | P3 | planned |
| D11 | Partial-unique `Organization.instagram_business_account_id` + already-claimed connect handling | **P0** (constraint) + P7 (connect) | **constraint done**, connect planned |
| D12 | Load conversation history into the agent + debounce/coalesce fragmented inbound DMs | P2 (history) + P3 (coalesce) | planned |
| D13 | Celery beat token-refresh job + expiry alert (inbox/email) | P7 | planned |
| D14 | Groundedness gate: auto-send only when a FAQ/event tool actually fired (no tool hit → queue) | P2/P3 | planned |
| #6 | Verify current Instagram-Login permission model; add **Business Verification** + IG Professional account + per-org "allow message access" as explicit P7 pre-reqs | P7 | planned |

**Test additions folded into the plan (completeness/regression):**
- P0 (done): duplicate `provider_message_id` → `IntegrityError`; blank ids coexist; `save()` org coercion; `instagram_business_account_id` unique-when-set.
- P3: concurrent duplicate webhook delivery → one message + one send (D5 race); daily-cap-reached → queued (D4); groundedness no-tool-hit → queued (D14).
- P4: human "approve & send" outside the window → `failed` surfaced in inbox (not silent).
- P2: adversarial injection eval cases (D7).

## 10. NOT in scope (considered, deferred)
- **`eval_sms_plans` → Langfuse migration** — its own track (D1); valuable but unrelated to the IG agent.
- **`validate_segments` under Langfuse** — deterministic non-LLM backtest; stays a standalone command.
- **Linking IG senders to `Customer`** — no IG handle on `Customer`; `conversation.customer` stays null in v1.
- **Rich media / story replies / comment automation** — text DMs only in v1.
- **Outbound proactive messaging** — reply-only in v1.
- **Phase 0.5 Meta feasibility spike (considered in D2, declined)** — App Review handled at P7 as the known long-pole.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | errored | model unsupported for account; fell back to Claude subagent |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | issues_found | 13 findings raised, 13 resolved (D1–D14 + #6); 3 folded into P0 code |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CROSS-MODEL:** Outside voice (Claude subagent, Codex unavailable) raised 8 gaps the 4-section review missed; 7 accepted (D9–D14, #6), 1 tension on autonomy (draft-first vs auto-send) resolved by the user keeping auto-send (D8=B) with the mandatory groundedness gate (D14) as the safety compensation.
- **VERDICT:** ENG CLEARED — plan hardened, P0 implemented + 18 tests green. Ready to implement P1 onward. Design/CEO reviews optional (recommend a light design pass before P1's FAQ-editor + P4's inbox UI).

NO UNRESOLVED DECISIONS
