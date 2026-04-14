# TODOS

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
