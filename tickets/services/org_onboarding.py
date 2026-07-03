"""New-organization initialization (external-first onboarding).

Feature flags come from model defaults (`external_events_enabled`,
`sms_marketing_enabled`), so they need no per-path code. This helper handles the
one side effect that cannot be a model default: seeding trial SMS credits through
the wallet ledger. It is called from every production org-creation path
(`create_organization` and `_ensure_organization_for_user`).
"""

import logging

from tickets.services import sms_credits

logger = logging.getLogger(__name__)

# Trial SMS credits granted to a brand-new org, in TOKENS (1 token = 1 SMS
# segment). Converted to cents at seed time using the current per-segment price,
# mirroring how token-pack purchases are priced (sms_views.py).
TRIAL_SMS_CREDIT_TOKENS = 500

_TRIAL_CREDIT_DESCRIPTION = 'Trial credit'


def initialize_new_organization(org):
    """Seed trial SMS credits for a newly created org (idempotent, non-fatal).

    - Idempotent: a second call for the same org is a no-op (ADJUSTMENT rows have
      no unique constraint, so we guard on an existing trial-credit ledger row).
    - Non-fatal: a wallet failure must never break org creation. The org already
      exists by the time this runs; we log and move on.

    Call this AFTER the org row is committed — `credit()` takes a
    `select_for_update()` lock on the org.
    """
    from tickets.models import SMSCreditTransaction

    try:
        already_seeded = SMSCreditTransaction.objects.filter(
            organization=org,
            kind=SMSCreditTransaction.Kind.ADJUSTMENT,
            description=_TRIAL_CREDIT_DESCRIPTION,
        ).exists()
        if already_seeded:
            return
        amount_cents = int(TRIAL_SMS_CREDIT_TOKENS * sms_credits.price_per_segment_cents())
        sms_credits.credit(
            str(org.pk),
            amount_cents,
            kind=SMSCreditTransaction.Kind.ADJUSTMENT,
            description=_TRIAL_CREDIT_DESCRIPTION,
        )
    except Exception:
        # Trial credits are a nice-to-have, not part of the org-creation contract.
        logger.exception('Failed to seed trial SMS credits for org %s', org.pk)
