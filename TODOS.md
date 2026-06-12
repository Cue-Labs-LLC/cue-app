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
