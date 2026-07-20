# TODOS

## Mobile: Event Detail Meta Strip Wrapping

**What:** The `detail-meta` strip below the event name (venue · date · capacity) can wrap to 2+ lines on mobile at 390px when event names are long or all three items are present.

**Why:** Long event names push the meta strip down and it wraps mid-token (e.g., "Jan 15, 2025 ·" on one line, "7:00 PM" on the next). Looks sloppy on small screens.

**Pros:** Small effort. Improve readability for any event with a long name or all three metadata items.

**Cons:** Requires deciding what to truncate or omit on mobile (capacity is lowest priority).

**Context:** Discovered during design review of event detail mobile declutter (plan: merry-zooming-lobster). The `detail-meta` / `detail-meta-item` / `detail-meta-divider` CSS classes are in `dashboard.css`. Fix candidates: truncate venue name, hide capacity on mobile, or use a 2-line layout (date on line 1, venue on line 2).

**Depends on:** Event detail mobile declutter shipped.

---

## Multi-Org: Member Removal

**What:** Add `member_remove` view that deletes an `OrganizationMembership` row for a given user+org.

**Why:** Users can currently be invited and have their role changed, but there's no way for an org owner to remove someone from their organization. With multi-org support, this gap is more visible: a user locked to org A can't be expelled back to a no-org state, and org A's data stays visible to them indefinitely.

**Pros:** Closes the obvious missing CRUD operation for membership management. Required before any meaningful org access control story.

**Cons:** Need to decide what happens to the user's session if they're currently on the removed org — should auto-switch to another membership or show org_required. Small edge case: what if you remove the last owner?

**Context:** As of the multi-org PR, `OrganizationMembership` is the authoritative membership table. Deletion is straightforward — `membership.delete()`. The view should be POST-only, owner-only, with a guard against removing yourself. Also note: the fast-path `get_organization()` trusts `_org_id` from session without verifying current membership — this becomes a live auth hole once member removal exists. Fix: add a membership check in the fast path, or call `clear_org_cache()` server-side when removing a user.

**Depends on:** Multi-org PR merged.

---

## Multi-Org: Deprecation Migration (0097)

**What:** Add migration 0097 to drop `UserProfile.organization` (ForeignKey) and `UserProfile.org_role` (CharField) once the multi-org transition is verified in production.

**Why:** The legacy columns exist purely for backward compatibility during the rollout. Once `OrganizationMembership` is the single source of truth and all read paths go through it, these columns are dead weight. They also mislead future contributors into thinking `profile.org_role` is current.

**Pros:** Removes the dual-write complexity. Single source of truth. Schema matches the mental model.

**Cons:** Requires verifying that all read paths (admin querysets, api_views._get_org_from_user, account_profile.html) have been updated to use the membership table before removing the columns.

**Context:** Current plan intentionally defers this to avoid blocking deployment. Trigger: 2 weeks in production with no regressions reported, and a manual audit confirming admin + API + template reads all use `OrganizationMembership`. Also update admin.py `get_queryset` methods that currently filter by `profile.organization_id`.

**Depends on:** Multi-org PR merged and stable.

---

## Multi-Org: Global Role Scope Concern

**What:** `member_role_update` currently allows an org owner to change a user's global `UserProfile.role` (organizer/attendee). With multi-org, one org's owner can demote a user's role across all their orgs.

**Why:** `UserProfile.role` is a global attribute, not per-org. If org A's owner changes someone from organizer → attendee, they lose organizer permissions in org B too.

**Pros of fixing:** Orgs can't interfere with each other's members. Correct access model.

**Cons:** Adds complexity. Global role changes would need to come from a separate superuser/system flow.

**Context:** Pre-existing behavior, but multi-org makes the blast radius wider. For MVP this is acceptable (most users will be in 1 org). Post-launch, consider making `role` changes owner+superuser only, or removing global role changes from `member_role_update` entirely (org_role is the only per-org permission that matters day-to-day).

**Depends on:** Multi-org PR merged.

---

## Marketing SMS: Per-Org Sender Numbers

**What:** Provision and verify a dedicated Twilio number (toll-free or 10DLC) per organization so opt-out and sender identity are truly per-tenant, instead of the single shared `+18449544410`.

**Why:** v1 marketing SMS sends from one shared verified toll-free number via a Messaging Service. With a shared sender, Twilio enforces STOP globally (a recipient who replies STOP is blocked from the number entirely, across all orgs), and one org's spam complaints can get the shared number carrier-filtered, degrading delivery for every tenant (noisy neighbor). Recipients also see an unfamiliar number with no org identity.

**Pros:** True per-tenant opt-out, sender identity, and blast-radius isolation. Matches the per-organizer consent promise already on the checkout/consent pages.

**Cons:** Each number needs its own toll-free or 10DLC verification (carrier approval lead time), plus provisioning cost and a per-org number management UI.

**Context:** The native SMS feature was deliberately built shared-number-first. `PhoneSuppression.organization` is already nullable specifically to support per-org suppression once per-org numbers exist (null = global/shared, set = per-org). When numbers go per-org, route sends through the org's own Messaging Service and start writing org-scoped suppression rows.

**Depends on:** Native marketing SMS shipped + per-org TF/10DLC verification process.

---

## Marketing SMS: Link Click + Revenue Attribution

**What:** Instrument `SMSCampaign.link_url` with a tracked redirect and tie clicks/conversions back to orders, mirroring the click/revenue fields the SlickText `EventSMSCampaign` path already surfaces.

**Why:** v1 stores `link_url` on a campaign but nothing reads it — there's no way to show ROI (clicks, attributed orders, revenue) for native sends, even though the marketing dashboard shows exactly those metrics for external SlickText campaigns.

**Pros:** Proves marketing ROI for native SMS; brings native campaigns to parity with the external-campaign reporting users already see.

**Cons:** Needs a redirect endpoint + click model + an attribution window joining clicks to subsequent orders. Some modeling decisions (attribution window, last-touch vs any-touch).

**Context:** `SMSCampaign.link_url` exists but is uninstrumented. `EventSMSCampaign` already has `clicks`, `unique_clicks`, `click_rate`, `orders`, `revenue` fields to mirror. A tracked redirect (e.g. `/r/<token>/`) that records a click then 302s to `link_url` is the natural starting point.

**Depends on:** Native marketing SMS shipped.

---

## SMS: Roll buy-link revenue into Marketing Overview + campaign list

**What:** Extend `_native_sms_summary` (`tickets/services/marketing/analytics.py`) so attributed tickets/revenue from SMS buy-links roll into the windowed Marketing Overview and the campaign-list performance band, alongside the existing clicks/delivery stats.

**Why:** The buy-link tracking PR surfaces attribution only on each campaign's detail page (deliberate scope decision). The overview and list aggregates don't yet reflect SMS-driven ticket sales or revenue, so the channel looks like it earns nothing at the rollup level.

**Pros:** Makes SMS revenue visible where organizers compare channels. The query already exists (`_sms_buy_stats` in `tickets/sms_views.py`) — this is mostly an aggregate over `StripeCheckoutSession` joined via the campaign's `TrackingLink`.

**Cons:** Touches the cached analytics layer (30/90/365-day windows, 10-min cache) — needs window-scoped aggregation and cache-key care. Decide gross vs net consistently with the detail page (net).

**Context:** SMS ticket links (PR #188) insert a `/track/<token>/` link into the body; at save each campaign mints its **own** `TrackingLink` (named `SMS · <campaign>`) via `_mint_campaign_tracking_link`, so attribution is per-campaign. Completed `StripeCheckoutSession` rows carry that `tracking_link`. The detail page resolves the token out of `campaign.link_url` and shows tickets + net revenue (`amount_total_cents - platform_fee_cents`, COMPLETED only, matches `views.py:4537`). Mirror the existing `EventSMSCampaign` orders/revenue fields the overview already renders.

**Depends on:** PR #188 (shipped on main).

---

## SMS: Harden tracking-link attribution against session loss

**What:** Stop relying on the session as the *only* carrier of the `tracking_ref_<event_id>` value during checkout. Thread the ref through the login/signup `next=` redirect and re-apply it, and/or stamp it onto a pending cart/order record at first touch so attribution survives even if the session is replaced.

**Why:** Attribution is stored server-side in `request.session['tracking_ref_<event_id>']` (`views.py:9221`) and read at `create_payment_intent` (`views.py:9934`) — the `?ref` in the URL is just a courier. This works today: an anonymous buyer who logs in or creates an account keeps the ref because Django's `login()` uses `session.cycle_key()` (preserves data). Confirmed working in manual testing. But there are edge cases where the session is dropped and the sale goes unattributed.

**Pros:** Closes the remaining attribution gaps. Makes per-campaign revenue numbers trustworthy even on the unhappy auth paths.

**Cons:** More moving parts in the checkout/login redirect chain; needs care to not double-attribute. Low urgency — the common path already works.

**Edge cases that currently lose the ref:**
- Buyer already authenticated as user A then logs in as a *different* user B mid-checkout → Django `login()` calls `session.flush()`, data lost.
- Login/signup that starts a brand-new session (different browser/tab, cleared cookies, incognito).
- Buyer bounced to login *before* ever loading the buy page with `?ref` (so the session was never stamped).

**Context:** Buy/checkout flow: `/track/<token>/` (`track_link_redirect`, sets session ref) → `/e/<id>/?ref=<token>` (`public_event_buy`, re-stamps session, guarded by `if ref:` so a reload without `?ref` doesn't clear it) → checkout → `create_payment_intent` binds `StripeCheckoutSession.tracking_link`. All checkout auth uses Django's standard `auth_login` (e.g. `attendee_signup_view` `views.py:10691`). A first step is to audit whether the checkout login redirect preserves `next=`/`?ref` through its hops.

**Depends on:** none (incremental hardening of shipped behavior).

---

## Loyalty: Attendee "My Points" Page

**What:** Read-only attendee-facing page at `/my-points/` showing, per organization, the customer's points balance, lifetime points, status tier badge, and recent ledger history, plus nav links.

**Why:** It's the customer-facing half of the loyalty points loop — "watch your points grow" is what makes points motivating. Phase 1 shipped earn + status (organizer-facing only); customers currently earn silently.

**Pros:** Completes the "Get Familiar" points vision; cheap — read-only views over data Phase 1 already creates; design fully settled during eng review.

**Cons:** None structural. Email-based customer resolution means phone-only or mismatched-email accounts won't see their history (same limitation as My Tickets).

**Context:** Mirror `my_tickets` (views.py): `@login_required` only (no `@require_org`), resolve `Customer` rows by `email=request.user.email.lower()` filtered to orgs with an active, points-enabled, non-deleted `LoyaltyProgram`; show last ~20 `points_transactions` per org. Template extends `attendee_base.html` (`.a-*` classes); nav links in `partials/_attendee_nav.html` and base.html's My Account block. Deferred from Phase 1 by eng review decision D1.

**Depends on:** Loyalty points Phase 1 shipped.

---

## Loyalty: Nightly Cron — Points Sweep + Tier Recalc

**What:** A Render `type: cron` service running a management command that, per org with an active points-enabled program, runs the idempotent points backfill sweep then the tier recalc task.

**Why:** Two Phase-1 limitations close at once: (a) award failures swallowed at order hooks self-heal within 24h (the sweep finds orders with no EARN row); (b) customers who cross a `min_lifetime_points` threshold get promoted without the organizer pressing Recalculate.

**Pros:** Self-healing ledger; fresher tiers; follows the established cron pattern (`ltv-updater-ai-opportunities` in render.yaml) instead of adding Celery Beat.

**Cons:** One more Render service; sweep cost scales with org order history (mitigated: batched queries, idempotent re-runs only insert missing rows).

**Context:** `backfill_loyalty_points_task(program_id)` already does the sweep + chains `recalculate_loyalty_tiers_task`, claims `recalc_in_progress` while running, and stamps `Organization.loyalty_points_backfilled_at`. The management command just needs to iterate orgs with an active points program and enqueue it (mirror `generate_ai_opportunities.py` with --sync and --organization-id flags). Eng review decision D10.

**Depends on:** Loyalty points Phase 1 shipped.

---

## Payments: Hide refund button for in-person (direct-charge) orders

**What:** `order_detail` still renders the refund button for in-person orders, but `refund_order` now rejects `charge_flow='direct'` sessions with "refund from your Stripe dashboard" — a guaranteed dead-end click. Add a view/template guard mirroring the endpoint rule and show the dashboard note instead.

**Why:** Every in-person refund attempt currently hits an error message after a click that looked actionable. Cheap fix, real support-ticket saver.

**Pros:** UI and endpoint enforce the same rule; organizers get told the actual refund path up front.

**Cons:** One more template conditional to maintain until in-app direct refunds ship (see below — that TODO supersedes this one).

**Context:** From the destination-charges eng review (decision D10, deferred by choice). The endpoint guard lives in `refund_order` (tickets/views.py, charge_flow check right after the eligibility guard). The order page's refund eligibility logic is in `order_detail` around views.py:5271. `StripeCheckoutSession.charge_flow` tells you everything you need.

**Depends on:** Destination-charges PR merged.

---

## Payments: Connected-account refund discovery in backfill_refund_state

**What:** Add a `--connected` mode to `backfill_refund_state` that iterates orgs with `stripe_account_id`, lists refunds on each connected account, and replays them through `_sync_charge_refund` — healing missed `charge.refunded` events for in-person (direct) charges.

**Why:** Online refunds have webhook + backfill as belt-and-braces. In-person refunds arrive on the connect webhook endpoint only; if an event is ever missed, the order stays marked paid forever with no recovery path.

**Pros:** Completes the healing story for every charge flow; reuses `_sync_charge_refund` verbatim, just a different iteration source. Stripe retains refund history, so late healing always works.

**Cons:** Per-org Stripe API iteration; needs care with pagination and rate limits.

**Context:** From the destination-charges eng review (Codex finding, decision D16: defer until after cutover). `_find_session_for_payment_intent` already matches direct sessions by PI id since the sell flow stores `stripe_session_id=pi_…`.

**Depends on:** Destination-charges PR merged + cutover complete.

---

## Payments: Refund-time warning when connected balance can't cover the clawback

**What:** Before issuing a refund in `refund_order`, check `_get_connected_balance_cents(org)` and require an explicit confirm step when the organizer's available balance is less than the transfer reversal the refund will trigger ("this refund will overdraw your payout balance").

**Why:** If an organizer withdraws everything and then refunds, the reversal drives their Stripe balance negative and Cue is liable until future sales cover it. Today this is silent (logged only); a confirm step turns platform credit exposure into an informed organizer decision.

**Pros:** Fewer surprise negative balances; clear paper trail when the organizer proceeds anyway.

**Cons:** Adds a Stripe call + confirm step to the refund flow; can't catch refunds initiated from the Stripe dashboard.

**Context:** From the destination-charges eng review (decision D17). The reversal math lives in `_reverse_transfer_for_refund` (tickets/views.py); the pending reversal for a session is `min(new cumulative refund, transfer_cents) − Transfer.amount_reversed`. Monitor negative-balance frequency first — if it never happens, deprioritize.

**Depends on:** Destination-charges PR merged; monitoring data on negative connected balances.

---

## Payments: In-app refunds for in-person (direct-charge) orders

**What:** Extend `refund_order` to support `charge_flow='direct'` sessions via `Refund.create(payment_intent=…, stripe_account=org.stripe_account_id)`, honoring the documented fee policy (Cue keeps the application fee on in-person refunds — decision D14).

**Why:** In-person refunds are currently dashboard-only — the single remaining workflow that forces organizers into Stripe. One refund surface regardless of how the sale happened.

**Pros:** Feature parity for in-person sales; supersedes the "hide refund button" TODO above; the connect-endpoint `charge.refunded` routing already syncs the resulting state.

**Cons:** Needs connected-account error handling (insufficient organizer balance) and a decision on partial-refund UX for card-present payments.

**Context:** From the destination-charges eng review (decision D18). Direct sessions now exist with `charge_flow='direct'`, PI id in `stripe_session_id`, and `platform_fee_cents` from `application_fee_amount`. The `_sync_charge_refund` reversal block no-ops for direct charges (no transfer), so only the Refund.create call site needs the `stripe_account` parameter. Do NOT pass `refund_application_fee` — keeping the fee is the documented policy.

**Depends on:** Destination-charges PR merged; D14 fee policy stays as documented.

---

## Ticket Allocation: Filter drill-down by ticket-type key, not name

**What:** The "ticket name → orders of that type" drill-down (`saleable_ticket_type_orders` in views.py, reached from the Ticket Allocation card on event_detail) filters orders by ticket-type **name** (`tickets__ticket_type=tt.name`). Give `Ticket` a real key to its `SaleableTicketType` and filter by that key instead.

**Why:** `Ticket.ticket_type` is a denormalized `CharField` (the name string), and `SaleableTicketType.name` is not unique per event. If an organizer has two ticket types sharing a name on one event, clicking either name returns the **union** of both types' orders — while the allocation card's per-type `quantity_sold` counts stay independent. So the counts and the drill-down can disagree in that edge case.

**Pros:** Truly independent drill-down per ticket type; counts and order lists always agree. Also unlocks accurate per-type analytics elsewhere.

**Cons:** Requires a schema change: add a nullable `saleable_ticket_type` FK on `Ticket`, a data migration to backfill existing rows by name (best-effort, ambiguous for same-named types), and populating it at purchase time. Direct purchases already know the `tt_id` at creation (`api_views.py:1630`, where `ticket_type=item['name']` is set and `item['tt_id']` is in scope), so new rows are cheap to populate; CSV-imported (external) events have no `SaleableTicketType` so the FK stays null there.

**Context:** Shipped with the clickable-ticket-name drill-down (plan: clickable ticket names → orders). Name-based filtering was accepted as the v1 to avoid a migration. The in-person sell path (`api_views.py` ~1627) and the Stripe webhook purchase path both create `Ticket` rows from `SaleableTicketType`; both have the `tt_id`/snapshot available to populate the FK.

**Depends on:** Clickable ticket-name drill-down shipped.

## SMS: Lock charged/scheduled campaign fields in admin

**What:** Add `get_readonly_fields` to the SMS campaign admin so that once a campaign leaves DRAFT (scheduled/charged/sending/sent), `body`, `link_url`, `scheduled_at`, and `filter_criteria` become read-only.

**Why:** Campaigns are charged and have their audience + per-recipient disclosure decisions frozen at schedule time (`sms_views.py:sms_campaign_create`). The Django admin still allows editing a SCHEDULED campaign's `body`/`link_url`/`scheduled_at` (`admin.py:1226`). One admin edit after charge desyncs the segment math (pre-existing) and, after the conditional-STOP-footer change, also desyncs the persisted `stop_disclosed`/`segments` decision and moves the 30-day disclosure anchor out from under the charge.

**Pros:** Closes a real desync footgun for both billing and compliance with ~10 lines. No data migration.

**Cons:** Admins occasionally fix typos in scheduled bodies; locking removes that. Could scope the lock to only the billing-relevant fields and leave `name` editable.

**Context:** Surfaced by Codex outside-voice review during the conditional-STOP-footer plan (plan: serene-church). Pre-existing (not introduced by that change) but widened by it. Trusted/internal admin, so deferred rather than blocking. Start in `tickets/admin.py` `SMSCampaignAdmin` with `get_readonly_fields(self, request, obj)` returning the locked set when `obj and obj.status != SMSCampaign.Status.DRAFT`.

**Depends on:** Nothing. Independent of the conditional-footer PR.

---

## Markets: Drop legacy `city` serialization aliases and unify the `?market=` param contract

**What:** In a dedicated follow-up (once templates/tests are migrated off them), remove the back-compat cruft left by the city→Market conversion: (a) the `'city': market_name` key emitted by every converted report (views.py `customer_ltv_by_market`, `repeat_customers`, `profitability_overview`; `market_trend_calculator._build_market`; `external_survey/analytics` `city_breakdown`; `marketing/analytics`), (b) the redundant `market_label` field (always equal to `market_name`), (c) the stale `city_raw` key in survey analytics (now holds a market_id), and (d) the inconsistent `?market=` value contract — `survey_analytics` filters by market **UUID** while `sms_campaign_list` still filters by market **name**.

**Why:** `explicit > clever`. A dict key literally named `city` that holds a Market label misleads every future reader, and the same 4-key dict (`market_id`/`market_name`/`market_label`/`city`) is copy-pasted across ~6 sites. The split `?market=` contract (name vs UUID under one param name) is a latent trap: any shared link-builder or future consolidation silently misbehaves (outside-voice review, confidence 4).

**Pros:** One canonical `market_id` + `market_name` shape across all reports; kills the redundant field and the misleading alias; one consistent query-param contract.

**Cons:** Cross-cutting rename touching 6 services/views + their templates + tests. Consumers must migrate in lockstep (templates read `city`, survey links use `city_raw`, LTV sort uses `?sort=city`) or links/sorts break. Best done as its own PR, not squeezed into the shipped conversion.

**Context:** Deferred during /plan-eng-review of "Tie market reporting to Market entity" (PR #302, decision D2). The `city` aliases were intentionally kept in that PR so templates/tests didn't change in the same diff (see `.context/plans/tie-market-reporting-to-market-entity.md`, "Interfaces And Output"). Start by grepping `'city':` and `market_label` in the six converted paths, and `?market=`/`?city=`/`?sort=city` in `survey_analytics.html`, `ltv_by_market.html`, and `sms_campaign_list`.

**Depends on:** "Tie market reporting to Market entity" (PR #302) shipped.

---

## Market Membership Recency Window

**What:** Add a recency dimension to market membership in `filter_customers` — e.g. "in market X" = has an order for an event in X within the last N months (org-configurable or fixed 24 months), instead of ever-bought-there-once-forever.

**Why:** Today a 2019 one-time Austin buyer is a permanent Austin SMS target and inflates every Austin segment count. In an RFM product, market membership is the only slicer with no recency component, so market audiences and market segment counts get staler as the org ages.

**Pros:** Market SMS audiences match who actually lives/attends there now; segment-by-market analytics reflect the current customer base; aligns market membership with the recency philosophy of RFM.

**Cons:** Changes campaign audience sizes materially (needs product sign-off on the window); every market surface (customers list, segments page, SMS preview/materialization) must use the same window or the reconciliation problems return; needs a "why did my audience shrink" explanation in the UI.

**Context:** Raised by the outside-voice review of the market-segments branch (decision D21, 2026-07-02, `.context/plans/market-specific-customer-segments.md`). Current semantics: `filter_customers` market block matches ANY `TicketOrder` → event → market, no date bound. Related risk captured in the same review: markets are auto-reassigned by MarketBuilder/CSV import, so a *scheduled* campaign's market audience can drift or empty between save and send with no zero-recipient warning at send time — a recency window doesn't fix that, but any redesign here should consider a send-time zero-recipient guard in the same pass.

**Depends on:** Market-specific customer segments branch shipped; product decision on window length.

## Segments-by-Market Breakdown: Versioned-Key Cache

**What:** Cache the `_market_segment_breakdown` result (and optionally the market-scoped segment stats) in `customer_segments` using the `event_list`-style versioned cache key pattern (`{view}:{version}:{org_id}`), with a version-bump invalidation helper.

**Why:** The breakdown is a full GROUP BY over every org `TicketOrder` joined through Customer and Event, recomputed on every segments page view. Fine at current org sizes; becomes the slowest thing on the page for a very large org. Caching was considered and deliberately deferred during the market-segments eng review (decision D12, 2026-07-02) to avoid an unmeasured-problem invalidation tax.

**Trigger:** An org exceeds ~200k ticket orders, or the segments page renders slower than ~500ms server-side.

**Pros:** Page stays fast at any scale; follows an established, proven pattern in this codebase.

**Cons:** Five-plus invalidation points must be wired and kept correct — CSV upload success, Market create/edit/delete, event market reassignment (MarketBuilder), RFM recalc completion. Each is a stale-analytics bug if missed.

**Context:** `tickets/views.py:_market_segment_breakdown` and `customer_segments`; cache pattern documented in CLAUDE.md "Caching" (see `_invalidate_event_list_cache`). Note the zero-market gating (task T5 of the same review) already eliminates the cost entirely for orgs with no markets, so this TODO only matters for large market-adopting orgs.

**Depends on:** Market-specific customer segments branch shipped; a measured slow page.

---

## Multi-Market Selection in SMS Audience Builder

**What:** Expose the already-supported `market_ids` criteria as a multi-select in the SMS campaign audience builder ("VIP in Austin OR Seattle"), including multi-value handling in the live preview and `audience_summary`.

**Why:** `filter_customers` and `SMSCampaign.audience_summary` fully support `market_ids` (list of UUIDs and/or `__none__`), but nothing in the UI produces it — it was kept deliberately during the market-segments eng review (decision D1, 2026-07-02) as forward-compatibility for exactly this feature. This TODO is the recorded consumer; without it the plumbing is dead API.

**Pros:** Small UI change (ChoiceField → MultipleChoiceField or checkbox group) over plumbing that is already written and tested end-to-end; persisted `filter_criteria` format already accommodates it.

**Cons:** Audience summary and preview UX need multi-value treatment; the compose form's market group gets visually heavier; needs the same org-scoping and `__none__` gating as the single-select.

**Context:** `tickets/services/customer_filters.py` (`market_ids` handling), `tickets/forms.py SMSCampaignForm.market_id`, `tickets/templates/tickets/marketing/sms/campaign_form.html` (markets audience group). Constraint from the same review (decision D14): markets refine an audience but can never be the sole selector — a tag or segment is still required, regardless of how many markets are picked.

**Depends on:** Market-specific customer segments branch shipped.

## Onboarding: Extract shared real-customers helper (DRY)

**What:** Extract a single `real_customers(org)` queryset helper (or a `PLACEHOLDER_EMAIL_SUFFIX` constant + `.exclude()` mixin) and migrate the ~7 sites that repeat `.exclude(email__endswith='@placeholder.local')`.

**Why:** The synthetic in-person placeholder customer (`csv_processor.py:497`) must be excluded from every customer-facing analytic. The literal `@placeholder.local` is currently copy-pasted across `views.py` (1869, 3035, 3050, 3096, 3634), `csv_processor.py`, and `tasks.py:498`. Each new consumer (the onboarding "imported data" predicate is the newest) re-copies it; one missed exclusion is a silent analytics bug.

**Pros:** One definition of "real customer"; new features can't forget the exclusion. Aligns with DRY-aggressive preference.

**Cons:** Touches ~7 analytics views, several of which were just reworked (segment queries, commits #303-#310). Refactor + regression risk not worth bundling into a feature PR.

**Context:** Deferred from the onboarding eng review (decision D3=3B, 2026-07-02) specifically to avoid churning recently-moved segment-query code. Do it as its own PR with the existing analytics tests as the safety net.

**Depends on:** Onboarding external-first branch shipped.

---

## Onboarding: Self-serve vs invite-only (waitlist + ungated API path)

**What:** Decide and implement the real self-serve funnel: either auto-approve external-first web orgs at `create_organization`, gate the currently-ungated `_ensure_organization_for_user` API path, or both.

**Why:** Today `create_organization` (web, external-first) is hard-gated behind `OrganizerWaitlist` APPROVED for non-superusers, while `_ensure_organization_for_user` (`api_views.py:937`, mobile Stripe-Connect) creates orgs with NO gate. So the only ungated org-creation door is the Stripe-first path — inverted from the external-first strategy. The onboarding plan polishes an invite-only front door and calls it a wedge; it is not self-serve until this is resolved.

**Pros:** Turns the external-first onboarding work into an actual self-serve funnel. Removes the strategic inconsistency.

**Cons:** Auto-approve + trial credits + zero verification invites throwaway-org SMS spam (needs abuse guard first). Gating the API path may break the mobile Stripe onboarding flow — needs care.

**Context:** Accepted-and-documented as out of scope in the onboarding eng review (finding 7.5 / Open Q2, 2026-07-02). Tied to the trial-credit-abuse open question — solve verification/abuse before flipping to auto-approve.

**Depends on:** Onboarding external-first branch shipped; trial-credit amount decided; consent (T1) landed.

---

## Onboarding: Org-level timezone

**What:** Add a `timezone` field to `Organization` + `OrgProfileForm`, and default new event forms to it.

**Why:** Timezone is currently set per-event only, so a brand-new org creates its first events with no sensible default and ambiguous TZ context. Related to a known class of UTC-date bugs (see `effective-status-utc-date-bug` learning) where `django_tz.localdate()` / per-event timezone is the right tool.

**Pros:** Removes a real correctness footgun for new organizers; small, self-contained change.

**Cons:** Must decide precedence (org default vs per-event override) and backfill existing orgs' default (likely from their events or `TIME_ZONE`).

**Context:** Raised during onboarding office-hours/eng review as a parallel nice-to-have (2026-07-02), explicitly NOT part of external-first onboarding. Use `event.timezone` for minute precision and `django_tz.localdate()` for day-granular comparisons (see views.py:11719 comment).

**Depends on:** —

## Onboarding: SMS consent-collection surface (Option C)

**What:** Build a dedicated way for organizers to *collect* marketing-SMS consent from customers, rather than only mapping it from a CSV column or asserting it manually. E.g., a shareable public opt-in link/page, an opt-in checkbox at ticket purchase, or a "text START to..." keyword flow.

**Why:** The onboarding "Send your first SMS campaign" step is consent-gated (imported contacts default to `sms_opt_in=False`, and texting non-consented contacts violates TCPA/carrier rules). Today consent can only be (a) mapped from a CSV consent column on import, or (b) set manually via the customer-list bulk action for customers the org already has documented consent for. The "Review consent" step now shows an explainer pointing at those (commit 068a6ae), but there is no first-party way to *gather new* consent. Without it, an org whose export lacks a consent column has no compliant path to a sendable audience beyond re-importing.

**Pros:** Closes the loop on the SMS revenue path (Cue monetizes SMS tokens); gives organizers a compliant, auditable consent source; makes the "send first campaign" activation step reachable for everyone, not just orgs with consent already in their data.

**Cons:** Real scope — needs a public opt-in page/route, consent record-keeping (timestamp, source, IP/double-opt-in for defensibility), and likely Twilio keyword/webhook handling for STOP/START. Compliance-sensitive; get the record-keeping right.

**Context:** Deferred from the onboarding design review (D5/6B) and the "Review consent" UX fix (2026-07-03). The manual/import paths exist today: `set_sms_opt_in` (tickets/services/sms_consent.py), `customers_bulk_sms_status` (tickets/sms_views.py), and CSV consent mapping (`customer_sms_opt_in` in csv_processor.py / the "SMS Marketing Consent" format row). Consent state lives on `Customer.sms_opt_in` / `sms_opt_in_date`; campaign audiences gate on it (models.py). A public opt-in surface is the missing piece.

**Depends on:** —

---

## Subscribe: Phone-only signups (customer-identity workstream)

**What:** Allow the public subscribe page to accept phone-only signups (no email) without splitting one person into two `Customer` rows. Requires: normalize `phone` to E.164 in `Customer.save()` + backfill existing rows; a `(organization, phone)` partial-unique constraint migration preceded by a dup-phone audit/cleanup; and a shared `resolve_customer(org, email, phone)` helper that checkout (`views.py:10890`), webhook fulfillment (`views.py:11763`), and CSV import (`csv_processor.py:492`) all route through, deduping by email OR normalized phone.

**Why:** The subscribe MVP requires email (eng-review decision A) because the app keys `Customer` identity on `(org, email)` everywhere; phone-only would fork identity. This is the deliberate way to get the original phone-first wedge back once signup volume justifies it.

**Pros:** Unlocks phone-only audience capture (lower signup friction); centralizes customer identity behind one idempotent resolver (matches the `two-org-creation-paths` learning).

**Cons:** Reaches into money-path code (checkout); the `(org,phone)` migration can fail on existing duplicate phones; needs its own review. ~1 week.

**Context:** Deferred from /plan-eng-review of the subscribe page (2026-07-05). Codex outside voice flagged customer identity as the biggest risk. `Customer.save()` normalizes email but NOT phone (models.py:106); no merge utility exists. Design doc: owenbarton-obarton-audience-subscribe-page-design-20260705-192530.md.

**Depends on:** Subscribe page (email-keyed) shipped.

---

## Consent: Backfill checkout SMS consent into SMSConsentRecord ledger

**What:** Have ticket checkout write an `SMSConsentRecord` when it flips `sms_opt_in=True` (`views.py:10896`, `11769`), so the provable-consent ledger covers every origination surface, not just `/subscribe/`.

**Why:** After the subscribe page ships, consent proof is inconsistent: subscribe has an immutable record (IP, user-agent, exact disclosure), checkout still flips a boolean with no record. A TCPA audit wants one consistent proof model. (Codex outside-voice Finding 4.)

**Pros:** Uniform, defensible consent audit trail across the app; reuses the `SMSConsentRecord` model.

**Cons:** Touches the checkout/payment path; must capture the checkout consent disclosure text + IP at that point.

**Context:** Raised in /plan-eng-review of the subscribe page (2026-07-05). The `SMSConsentRecord` model + `source` field are designed to accept a `checkout` source with no schema change.

**Depends on:** `SMSConsentRecord` model (subscribe page) shipped.

---

## Subscribe: CAPTCHA abuse escalation

**What:** Add a CAPTCHA (hCaptcha / Cloudflare Turnstile) to the subscribe form as a second abuse layer above rate-limiting + Twilio Fraud Guard.

**Why:** The public OTP endpoint spends real money per send; rate-limit + Fraud Guard is the MVP floor, CAPTCHA is the escalation if bot traffic appears. (Eng-review decision 2A.)

**Pros:** Strong bot mitigation on a paid endpoint.

**Cons:** Friction on the funnel; a JS dependency; premature before any observed abuse.

**Context:** Deferred from /plan-eng-review of the subscribe page (2026-07-05), gated on observing real abuse in the rate-limit metrics.

**Depends on:** Subscribe page shipped + abuse observed.

---

## Subscribe: Preference / manage-subscriptions center

**What:** A page where a subscriber can view and update their SMS (and later email) preferences without having to text STOP — e.g. a tokenized `/preferences/<token>/` link.

**Why:** Turns the consent ledger into a real audience primitive and gives subscribers a self-serve opt-down path (better than all-or-nothing STOP).

**Pros:** Better subscriber UX; supports future email channel; builds on the `SMSConsentRecord` + `opted_out_at` lifecycle already in the model.

**Cons:** Not needed until there is an audience to manage; needs secure tokenized access.

**Context:** Deferred from /plan-eng-review of the subscribe page (2026-07-05). `SMSConsentRecord.opted_out_at` is already carried for this.

**Depends on:** Subscribe page shipped.

---

## Design: Create a DESIGN.md via /design-consultation

**What:** Run /design-consultation to produce a real DESIGN.md — a named design system (typography scale, color tokens, spacing, component vocabulary, motion) for Cue.

**Why:** No DESIGN.md exists today. The subscribe page had to calibrate against implicit CLAUDE.md conventions (dashboard.css, Outfit/Sora, dark mode, the DISTILLED_AESTHETICS_PROMPT). A named system makes every future design review faster and more consistent, and gives implementers exact tokens instead of vibes.

**Pros:** Consistency across pages; faster design reviews; concrete tokens for implementers.

**Cons:** Time to run the consultation; someone must own keeping DESIGN.md current.

**Context:** Flagged in /plan-design-review of the subscribe page (2026-07-05), which rated design-system alignment 8/10 only because it leaned on implicit conventions. Existing references: dashboard.css, base.html, public_org_profile.html.

**Depends on:** —

---

## SMS: At-most-once send (atomic pre-send claim or Twilio idempotency)

**What:** Close the one remaining true-duplicate path in marketing-SMS sending: make a recipient send atomic so a message can never go to Twilio twice.

**Why:** The double-text fix (drop the `queued` regression + gate re-send/finalize on `twilio_sid`) closes the observed bug, but a residual race survives: if a worker dies after `send_sms()` reaches Twilio but before `send_sms_chunk_task` persists the SID (`tickets/tasks.py:1064-1083`), the row stays `(status=queued, twilio_sid='')` and the recovery cron re-sends it. Same window if a slow-but-healthy send overlaps a recovery re-dispatch and two chunk tasks load the same unsent row before either saves its SID. The `twilio_sid` guard can't catch this because the SID isn't persisted yet.

**Pros:** True at-most-once delivery; removes the last way a recipient can be texted twice; makes the recovery cron fully safe even under worker death / overlap.

**Cons:** Real design work. A compare-and-swap claim (`filter(id=.., status=QUEUED, twilio_sid='').update(...)` before calling Twilio) risks the opposite failure — under-send if the send then fails — so it needs a claimed/leased state + reaper. Twilio's Messages API idempotency support needs verifying before relying on a per-(campaign,recipient) key.

**Context:** Surfaced by the Codex outside-voice review during the double-text fix (plan: `.context/plans/sms-double-text-fix.md`). The observed 2026-07-20 incident did NOT hit this path (waves were cleanly 15+ min apart, no overlap) — it was purely the `queued`-callback regression. `STUCK_MINUTES` was raised 15→30 in `send_due_sms_campaigns.py` as a cheap mitigation against the overlap trigger, but that is not a real guarantee. Options to evaluate: (a) atomic pre-send claim with a lease + reaper for failed claims; (b) deterministic Twilio idempotency key per (campaign, recipient).

**Depends on:** Double-text fix shipped.

---

## Webhooks: Transactional Outbox for Guaranteed Enqueue

**What:** Replace best-effort `deliver_webhook_task.delay()` with a transactional outbox: write a `pending` delivery row in the same DB transaction as the domain write, then a poller/beat task publishes and marks them sent.

**Why:** Today `dispatch()` enqueues after commit and only logs if `.delay()` fails (broker outage, serializer error). The business action succeeds but the webhook is lost forever with no retry path. Flagged by the Codex outside-voice review (finding #4).

**Pros:** True at-least-once delivery even across broker outages. Also gives a natural place to enforce ordering and backpressure.

**Cons:** New table + periodic publisher + dedup logic; more moving parts. Broker outages are rare, so this is reliability insurance, not a hot-path fix.

**Context:** Enqueue failures are currently logged at error level in `tickets/services/webhooks/dispatch.py` (search "delivery lost") so they're alertable in the interim. The delivery task, signing, and `WebhookDelivery` log already exist; an outbox would add a `WebhookOutbox` (or reuse `WebhookDelivery` with a `pending` state) plus a Celery-beat publisher.

**Depends on:** Nothing; independent follow-up.

---

## Webhooks: WebhookDelivery Retention / Pruning

**What:** Add a periodic task to delete `WebhookDelivery` rows older than N days (configurable, e.g. `WEBHOOK_DELIVERY_RETENTION_DAYS`).

**Why:** One row is written per delivery attempt with a full payload snapshot. A high-volume org (many orders) grows this table unbounded. It's an append-only audit log with no cleanup.

**Pros:** Bounds table growth and admin query cost. Small, isolated task.

**Cons:** Deletes audit history past the window — pick the window carefully (support/debugging needs).

**Context:** `WebhookDelivery` (tickets/models.py) is indexed on `(success, created_at)` and `(organization, event_type, created_at)`, so a windowed delete is cheap. Model this on the existing SMS/loyalty cleanup patterns if present.

**Depends on:** Nothing.

---

## Webhooks: Self-Serve Endpoint Management UI

**What:** Org-facing pages under `settings/integrations/webhooks/` to list, create, edit, delete, rotate-secret, and test-send `WebhookEndpoint`s, plus a delivery-log viewer.

**Why:** Endpoints are currently managed only via Django admin (not org-friendly). Orgs can't self-serve webhook setup.

**Pros:** Real product feature; matches the existing integrations hub. Test-send + delivery viewer make debugging self-service.

**Cons:** Full UI surface (templates, forms, views, URLs); the largest remaining chunk of the webhook feature.

**Context:** Backend (models, dispatch, signing, delivery log, triggers, admin) already ships. UI would reuse `WebhookEndpointForm` and the `settings/integrations/` conventions. HMAC verification docs for consumers should ship alongside (sign base is `timestamp.event_type.delivery_id.body`, header `X-Cue-Signature: t=…,v1=…`).

**Depends on:** Nothing.
