# TODOS

## Receipts: Drop the orphan `ReceiptSend` model

**What:** Remove the `ReceiptSend` model + migration `0119_receiptsend_taptopaytermsacceptance.py` from the codebase, after exporting historical rows for posterity.

**Why:** As of the scanner_receipt rewrite (Stripe-sent receipts), nothing writes to `ReceiptSend`. The table is dead weight and confuses future readers ("why does this model exist if no code touches it?").

**Pros:** Removes dead schema. One less model to maintain. Cleaner mental model: receipts are entirely a Stripe concern now.

**Cons:** Destructive migration on prod data. Need to export the historical rows first (Stripe-receipt audit trail from the old Django-sent flow).

**Context:** The `ReceiptSend` model lives in `tickets/models.py` (~line 2576-2600). It was kept intact during the Stripe-receipt migration to preserve the audit trail. Drop steps:
1. Export current rows: `python manage.py dumpdata tickets.ReceiptSend --indent 2 > receiptsend-archive-YYYYMMDD.json` and store in S3 or commit to a private archive.
2. Confirm with stakeholders (support, compliance) that the archive is sufficient.
3. Write `XXXX_drop_receiptsend.py` migration calling `migrations.DeleteModel('ReceiptSend')`.
4. Search for any lingering admin.py / serializers references and remove.

**Depends on:** scanner_receipt Stripe-wrapper rewrite shipped + at least one quarter of receipt usage data captured by Stripe for compliance.

---

## Receipts: Attach Stripe Customer to PaymentIntent before modify

**What:** Before calling `stripe.PaymentIntent.modify(..., receipt_email=...)`, look up or create a Stripe `Customer` by email and attach via `customer=cus_xxx`.

**Why:** Right now each receipt is a one-off. With a Stripe Customer attached, repeat buyers get a unified "my receipts" view in Stripe's hosted UI. Better buyer experience at near-zero cost.

**Pros:** Improves the receipt UX for repeat buyers. Documented Stripe pattern. Small, contained change inside `scanner_receipt`.

**Cons:** Adds one Stripe API call to the hot path (extra ~200ms p99). Stripe doesn't have a literal "create_or_retrieve" — implement as `Customer.list(email=...)` → create if empty.

**Context:** Spec's "Behavioral notes" section calls this out as "optional but improves the email… not needed for v1 but worth keeping in mind." Implementation lives in `tickets/api_views.py:scanner_receipt`. The Stripe call to add goes before the `PaymentIntent.modify` call, on the Connect account: `stripe.Customer.list(email=contact, limit=1, stripe_account=org.stripe_account_id)` then create if empty, then pass `customer=customer.id` into `modify`.

**Depends on:** scanner_receipt Stripe-wrapper rewrite shipped.

---

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
