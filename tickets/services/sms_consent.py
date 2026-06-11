"""Shared bulk SMS opt-in logic.

Single home for "flip the SMS opt-in flag on a set of customers", used by the
customer list's bulk action bar. Kept alongside the other bulk helpers (see
`services/tagging.py`) so they stay consistent.
"""

from django.utils import timezone


def set_sms_opt_in(customers_qs, *, opt_in):
    """Flip `sms_opt_in` for every customer in `customers_qs` (already org-scoped).

    Returns the number of customers actually changed. Filtering on the current
    value means already-opted-in customers keep their original `sms_opt_in_date`,
    and the count reflects real changes rather than the size of the selection.
    Opting out leaves `sms_opt_in_date` in place as a historical record.

    Opting in skips customers with no phone number — they can never receive SMS,
    so marking them as subscribers would only inflate the count. This mirrors
    `SMSCampaign.candidate_customers`, which builds its audience with
    `.exclude(phone='')`.
    """
    if opt_in:
        return customers_qs.filter(sms_opt_in=False).exclude(phone='').update(
            sms_opt_in=True, sms_opt_in_date=timezone.now(),
        )
    return customers_qs.filter(sms_opt_in=True).update(sms_opt_in=False)
