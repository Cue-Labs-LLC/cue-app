"""Create + charge + dispatch a marketing SMS campaign — the single money path.

Both the composer (``sms_views.sms_campaign_create``) and the AI plan page's inline
confirm (``sms_views.sms_plan_confirm_step``) route their send through
``finalize_campaign_send`` so "charged == sent" logic lives in exactly one place: the
audience is materialized, the per-recipient STOP-footer/cost is planned, and the
campaign + frozen recipient snapshot + wallet debit are written in one atomic
transaction. A send-now campaign is dispatched on commit; a scheduled one is left for
the */5 cron to pick up.
"""

import logging
from dataclasses import dataclass

from django.db import IntegrityError, transaction

logger = logging.getLogger(__name__)


class CampaignSendError(Exception):
    """Base for audience problems that block a send (mapped to UX by the caller)."""


class AudienceEmptyError(CampaignSendError):
    """The resolved audience has no contactable (opted-in, non-suppressed) recipients."""


class AudienceTooLargeError(CampaignSendError):
    """The resolved audience exceeds the per-campaign recipient cap."""

    def __init__(self, count, cap):
        self.count = count
        self.cap = cap
        super().__init__(f'Audience of {count} exceeds the cap of {cap}.')


@dataclass
class CampaignSendResult:
    """Outcome of a finalize call. ``created`` is False on an idempotent replay
    (the same idempotency_key already produced a campaign) — no second charge."""
    campaign: object
    recipient_count: int
    cost_cents: int
    cost_tokens: int
    scheduled: bool
    created: bool


def finalize_campaign_send(org, *, name, body, criteria, manual_include_ids, event,
                           scheduled, send_at, user, idempotency_key, cap):
    """Materialize → cost → atomically create+charge → dispatch. Returns a
    ``CampaignSendResult``.

    Raises ``AudienceTooLargeError`` / ``AudienceEmptyError`` for audience problems and
    ``InsufficientCreditsError`` (from ``sms_credits.charge``) when the wallet can't
    cover the cost — nothing is written in either case. Idempotent on
    ``idempotency_key``: a duplicate returns the existing campaign with ``created=False``.
    """
    from tickets.models import SMSCampaign, SMSMessageRecipient
    from tickets.sms import extract_first_url
    from tickets.sms_views import _mint_campaign_tracking_link, send_sms_campaign_task
    from tickets.services.sms_credits import charge, plan_campaign_footers

    # Resolve the audience up front so cap/empty are rejected before any write.
    recipients = SMSCampaign(
        organization=org, filter_criteria=criteria,
        manual_include_ids=manual_include_ids,
    ).materialize(org, cap=cap + 1)
    count = len(recipients)
    if count > cap:
        raise AudienceTooLargeError(count, cap)
    if count == 0:
        raise AudienceEmptyError()

    # Anchor the per-recipient footer/disclosure decision (and therefore the cost) on
    # the actual send time; reused for both the displayed cost and the charge so
    # displayed == charged.
    cost_cents, footer_plan = plan_campaign_footers(
        org, body, [r['phone'] for r in recipients], as_of=send_at,
    )
    cost_tokens = sum(footer_plan[r['phone']][1] for r in recipients)

    # Idempotency: if this exact confirm already produced a campaign, return it
    # rather than charging/sending again.
    existing = SMSCampaign.objects.filter(
        organization=org, idempotency_key=idempotency_key,
    ).first()
    if existing:
        return CampaignSendResult(
            campaign=existing, recipient_count=existing.audience_size,
            cost_cents=cost_cents, cost_tokens=cost_tokens,
            scheduled=scheduled, created=False,
        )

    try:
        with transaction.atomic():
            # Only name + body come off the composer form; every other field is set
            # explicitly here, so the campaign is built the same way regardless of
            # whether the caller is the composer or the inline plan confirm.
            campaign = SMSCampaign(
                organization=org, created_by=user, event=event,
                name=name, body=body,
                filter_criteria=criteria, manual_include_ids=manual_include_ids,
            )
            campaign.link_url = extract_first_url(campaign.body)
            # Per-campaign attribution: swap any shared event ticket link for one
            # unique to this campaign (mutates body/link_url; same-length token so the
            # already-estimated segment count/charge is unchanged).
            _mint_campaign_tracking_link(org, campaign)
            campaign.idempotency_key = idempotency_key
            campaign.status = SMSCampaign.Status.SCHEDULED
            campaign.scheduled_at = send_at
            campaign.audience_size = count
            campaign.save()
            # Freeze the audience now so charged == what sends. The orchestrator reuses
            # these rows (it only re-resolves when none exist) and re-checks opt-out
            # per recipient at send.
            SMSMessageRecipient.objects.bulk_create([
                SMSMessageRecipient(
                    campaign=campaign, customer_id=r['customer_id'], phone=r['phone'],
                    stop_disclosed=footer_plan[r['phone']][0],
                    segments=footer_plan[r['phone']][1],
                ) for r in recipients
            ], batch_size=500)
            # Charge inside the same transaction — campaign + snapshot + debit are
            # all-or-nothing (no uncharged campaign can exist). Raises
            # InsufficientCreditsError, which rolls the whole thing back.
            charge(org.id, cost_cents, campaign=campaign,
                   description=f'Campaign: {campaign.name}', created_by=user)
    except IntegrityError:
        # Concurrent duplicate confirm (same idempotency_key) — the other request won.
        existing = SMSCampaign.objects.filter(
            organization=org, idempotency_key=idempotency_key,
        ).first()
        if existing:
            return CampaignSendResult(
                campaign=existing, recipient_count=existing.audience_size,
                cost_cents=cost_cents, cost_tokens=cost_tokens,
                scheduled=scheduled, created=False,
            )
        raise

    # Always persisted as SCHEDULED (send-now → scheduled for now). Dispatch the
    # immediate send on commit; the */5 cron is a safety net if that dispatch is lost.
    if not scheduled:
        transaction.on_commit(
            lambda cid=str(campaign.id): send_sms_campaign_task.delay(cid)
        )

    return CampaignSendResult(
        campaign=campaign, recipient_count=count,
        cost_cents=cost_cents, cost_tokens=cost_tokens,
        scheduled=scheduled, created=True,
    )
