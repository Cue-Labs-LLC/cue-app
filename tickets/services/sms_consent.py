"""Shared bulk SMS opt-in logic.

Single home for "flip the SMS opt-in flag on a set of customers", used by the
customer list's bulk action bar. Kept alongside the other bulk helpers (see
`services/tagging.py`) so they stay consistent.
"""

from django.utils import timezone


def set_sms_opt_in(customers_qs, *, opt_in, organization=None):
    """Flip `sms_opt_in` for every customer in `customers_qs` (already org-scoped).

    Returns ``(changed_count, skipped_count)``. ``changed_count`` is how many
    customers actually flipped; filtering on the current value means already-in-state
    customers keep their original `sms_opt_in_date` and don't inflate the count.
    Opting out leaves `sms_opt_in_date` in place as a historical record and always
    reports ``skipped_count == 0``.

    Opting in skips customers with no phone number — they can never receive SMS,
    so marking them as subscribers would only inflate the count. This mirrors
    `SMSCampaign.candidate_customers`, which builds its audience with
    `.exclude(phone='')`.

    Opting in also **excludes suppressed numbers** (anyone who texted STOP, tracked
    in `PhoneSuppression`) when `organization` is given: an organizer cannot re-subscribe
    a number that opted out — only the recipient texting START can. These are reported
    as `skipped_count` so the caller can surface them, and they are NOT counted as
    changed. Suppression is keyed by normalized phone, so the match is resolved in Python
    (mirrors `SMSCampaign.materialize`).
    """
    if not opt_in:
        return customers_qs.filter(sms_opt_in=True).update(sms_opt_in=False), 0

    candidates = customers_qs.filter(sms_opt_in=False).exclude(phone='')
    skipped = 0
    if organization is not None:
        # Local imports mirror SMSCampaign.materialize — avoids a models/services cycle.
        from tickets.models import PhoneSuppression
        from tickets.sms import normalize_phone
        suppressed = PhoneSuppression.suppressed_phones(organization)
        if suppressed:
            blocked_ids = [
                c.id for c in candidates.only('id', 'phone')
                if normalize_phone(c.phone) in suppressed
            ]
            if blocked_ids:
                skipped = len(blocked_ids)
                candidates = candidates.exclude(id__in=blocked_ids)
    changed = candidates.update(sms_opt_in=True, sms_opt_in_date=timezone.now())
    return changed, skipped
