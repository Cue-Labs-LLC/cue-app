"""Prepaid SMS credit wallet: cost estimation + atomic debit / credit / refund.

The org's balance lives on ``Organization.sms_credit_balance_cents`` and every
change is mirrored to an ``SMSCreditTransaction`` ledger row inside the same
transaction. All mutations lock the org row (``select_for_update``) so concurrent
sends / top-ups can't race the balance negative or double-spend.

Money is integer cents end to end (matches Stripe). The per-segment price
(``settings.SMS_PRICE_PER_SEGMENT_CENTS``) is a Decimal so sub-cent rates work; a
campaign's cost is rounded to whole cents once.
"""

from decimal import Decimal, ROUND_CEILING

from django.conf import settings
from django.db import transaction


class InsufficientCreditsError(Exception):
    """Raised when an org's wallet can't cover a charge."""
    def __init__(self, required_cents, balance_cents):
        self.required_cents = required_cents
        self.balance_cents = balance_cents
        super().__init__(
            f'Insufficient SMS credits: need {required_cents}¢, have {balance_cents}¢.'
        )


def price_per_segment_cents() -> Decimal:
    return Decimal(getattr(settings, 'SMS_PRICE_PER_SEGMENT_CENTS', Decimal('3')))


def estimate_campaign_cost_cents(recipient_count: int, body: str) -> int:
    """Cost in whole cents to send ``body`` to ``recipient_count`` recipients.

    Uses the same GSM-7/UCS-2 segment math as the composer + Twilio billing, plus
    the auto-appended STOP footer (what actually goes out). Rounded once at the end.
    """
    from tickets.sms import sms_segment_info, with_stop_footer

    if recipient_count <= 0:
        return 0
    _, segments = sms_segment_info(with_stop_footer(body or ''))
    total = Decimal(recipient_count) * Decimal(segments) * price_per_segment_cents()
    # Round UP: billing must never under-charge, and ceil keeps a positive
    # sub-cent total (e.g. a tiny audience at a sub-cent rate) from rounding to
    # $0.00 and sending free.
    return int(total.to_integral_value(rounding=ROUND_CEILING))


def plan_campaign_footers(organization, body, phones, *, as_of):
    """Plan the per-recipient STOP-footer decision and the exact campaign cost.

    Returns ``(cost_cents, plan)`` where ``plan = {phone: (stop_disclosed, segments)}``.
    A phone may omit the footer iff it was disclosed within SMS_FOOTER_DISCLOSURE_DAYS
    of ``as_of`` (``recently_disclosed_phones``); first-ever phones always include it.

    Used by both the confirm preview and the charge so the displayed cost equals the
    debit, and the persisted decision is what the send task honors (charged == sent
    for the footer dimension). ``phones`` is iterated as given — duplicates are charged
    per occurrence because each is a separate send. Segments are computed on ``body``
    (not per-recipient tracked-link rewrites), matching existing billing.
    """
    from tickets.models import SMSMessageRecipient
    from tickets.sms import apply_stop_footer, sms_segment_info

    disclosed = SMSMessageRecipient.recently_disclosed_phones(organization, phones, as_of)
    plan = {}
    total_segments = 0
    for phone in phones:
        text, present = apply_stop_footer(body or '', include=phone not in disclosed)
        _, seg = sms_segment_info(text)
        plan[phone] = (present, seg)
        total_segments += seg
    cents = (Decimal(total_segments) * price_per_segment_cents()
             ).to_integral_value(rounding=ROUND_CEILING)
    return int(cents), plan


def estimate_campaign_cost_tokens(recipient_count: int, body: str) -> int:
    """Cost in tokens (1 token = 1 SMS segment) to send ``body`` to recipients.

    Exact: recipients × segments. This is what we *display*; the cents charged
    (estimate_campaign_cost_cents) may round up at sub-cent prices, so the token
    count is computed from the segment count directly, not from cents ÷ price."""
    from tickets.sms import sms_segment_info, with_stop_footer
    if recipient_count <= 0:
        return 0
    _, segments = sms_segment_info(with_stop_footer(body or ''))
    return recipient_count * segments


def _record(org, *, kind, amount_cents, balance_after, campaign=None,
            stripe_checkout_session_id=None, description='', created_by=None):
    from tickets.models import SMSCreditTransaction
    return SMSCreditTransaction.objects.create(
        organization=org, kind=kind, amount_cents=amount_cents,
        balance_after_cents=balance_after, campaign=campaign,
        stripe_checkout_session_id=stripe_checkout_session_id,
        description=description, created_by=created_by,
    )


def charge(org_id, amount_cents, *, campaign=None, description='', created_by=None):
    """Atomically debit the org's wallet. Raises InsufficientCreditsError if the
    balance can't cover it (nothing is written). Returns the new balance."""
    from tickets.models import Organization, SMSCreditTransaction
    amount_cents = int(amount_cents)
    if amount_cents <= 0:
        return Organization.objects.get(id=org_id).sms_credit_balance_cents
    with transaction.atomic():
        org = Organization.objects.select_for_update().get(id=org_id)
        if org.sms_credit_balance_cents < amount_cents:
            raise InsufficientCreditsError(amount_cents, org.sms_credit_balance_cents)
        org.sms_credit_balance_cents -= amount_cents
        org.save(update_fields=['sms_credit_balance_cents'])
        _record(org, kind=SMSCreditTransaction.Kind.CHARGE, amount_cents=-amount_cents,
                balance_after=org.sms_credit_balance_cents, campaign=campaign,
                description=description, created_by=created_by)
    return org.sms_credit_balance_cents


def credit(org_id, amount_cents, *, kind=None, campaign=None,
           stripe_checkout_session_id=None, description='', created_by=None):
    """Atomically add credits (top-up / refund / adjustment). When a
    stripe_checkout_session_id is given, this is idempotent: a retry with the same
    id is a no-op (returns the current balance) thanks to the unique constraint."""
    from tickets.models import Organization, SMSCreditTransaction
    amount_cents = int(amount_cents)
    kind = kind or SMSCreditTransaction.Kind.TOPUP
    if amount_cents <= 0:
        return Organization.objects.get(id=org_id).sms_credit_balance_cents
    with transaction.atomic():
        if stripe_checkout_session_id and SMSCreditTransaction.objects.filter(
            stripe_checkout_session_id=stripe_checkout_session_id,
        ).exists():
            # Already credited (webhook retry / duplicate) — no-op.
            return Organization.objects.get(id=org_id).sms_credit_balance_cents
        org = Organization.objects.select_for_update().get(id=org_id)
        org.sms_credit_balance_cents += amount_cents
        org.save(update_fields=['sms_credit_balance_cents'])
        _record(org, kind=kind, amount_cents=amount_cents,
                balance_after=org.sms_credit_balance_cents, campaign=campaign,
                stripe_checkout_session_id=stripe_checkout_session_id,
                description=description, created_by=created_by)
    return org.sms_credit_balance_cents


def refund_campaign(campaign, *, description='Campaign canceled'):
    """Credit back the net amount still charged for a campaign (e.g. when a
    scheduled campaign is canceled before sending). Idempotent and atomic: the
    org row is locked while we read the campaign's ledger net and write the
    compensating credit, so two concurrent refunds can't both pay out. Returns
    refunded cents."""
    from tickets.models import Organization, SMSCreditTransaction
    with transaction.atomic():
        org = Organization.objects.select_for_update().get(id=campaign.organization_id)
        net = sum(
            t.amount_cents for t in
            SMSCreditTransaction.objects.filter(campaign=campaign)
        )  # charges negative, refunds positive
        refundable = -net  # outstanding debit, if any
        if refundable <= 0:
            return 0
        org.sms_credit_balance_cents += refundable
        org.save(update_fields=['sms_credit_balance_cents'])
        _record(org, kind=SMSCreditTransaction.Kind.REFUND, amount_cents=refundable,
                balance_after=org.sms_credit_balance_cents, campaign=campaign,
                description=description)
    return refundable
