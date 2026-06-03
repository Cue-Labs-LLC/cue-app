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
