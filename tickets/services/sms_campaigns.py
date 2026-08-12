"""Create + dispatch a marketing SMS campaign — the single send path.

Both the composer (``sms_views.sms_campaign_create``) and the AI plan page's inline
confirm (``sms_views.sms_plan_confirm_step``) route their send through
``finalize_campaign_send``: the audience is materialized, the per-recipient
STOP-footer/cost is planned, and the campaign + frozen recipient snapshot are written in
one atomic transaction. The wallet is NOT debited here — charging happens at SEND time
(``tasks.send_sms_campaign_task``) for the segments that actually go out, so the frozen
snapshot's segments are what that charge is computed from. A send-now campaign is
dispatched on commit; a scheduled one is left for the */5 cron to pick up.
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


class DailyCapExceededError(CampaignSendError):
    """The send would push its day past the shared daily carrier cap. Blocked at compose time
    so the organizer can trim the list rather than have delivery silently deferred."""

    def __init__(self, count, allowed, cap, send_date):
        self.count = count
        self.allowed = allowed  # recipients that would fit the day's remaining budget
        self.cap = cap
        self.send_date = send_date
        super().__init__(f'Send of {count} exceeds the daily cap ({allowed} of {cap} available).')

    def user_message(self):
        when = self.send_date.strftime('%b %d')
        if self.allowed <= 0:
            return (
                f'The daily send limit for {when} is already fully booked. '
                f'Reschedule this campaign for another day.'
            )
        return (
            f'This send of {self.count} recipients would exceed the daily send limit for '
            f'{when}. Reduce the recipient list to about {self.allowed} or fewer, or schedule '
            f'it for another day.'
        )


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


@dataclass
class SplitResult:
    """Outcome of a two-batch split: as many recipients as fit the send day go out in
    ``batch1``; the overflow is scheduled for ``batch2`` the next day. Either campaign
    may be ``None`` (e.g. the send day is already fully booked → no ``batch1``).
    ``leftover_count`` is recipients that fit neither day and were NOT scheduled."""
    batch1: object
    batch2: object
    batch1_count: int
    batch2_count: int
    leftover_count: int


def finalize_campaign_send(org, *, name, body, criteria, manual_include_ids, event,
                           scheduled, send_at, user, idempotency_key, cap):
    """Materialize → estimate → atomically create + freeze snapshot → dispatch. Returns a
    ``CampaignSendResult``.

    Does NOT charge the wallet: the debit happens at SEND time (``send_sms_campaign_task``)
    for the segments that actually go out. Raises ``AudienceTooLargeError`` /
    ``AudienceEmptyError`` for audience problems (nothing written). Idempotent on
    ``idempotency_key``: a duplicate returns the existing campaign with ``created=False``.
    """
    from django.utils import timezone
    from tickets.models import SMSCampaign, SMSMessageRecipient
    from tickets.sms import extract_first_url
    from tickets.sms_views import _mint_campaign_tracking_link, send_sms_campaign_task
    from tickets.services.sms_credits import plan_campaign_footers
    from tickets.services.sms_limits import daily_capacity_for, daily_segment_cap, fit_within_budget

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
    # rather than charging/sending again (and without re-running the cap block below,
    # which could spuriously reject a legitimate replay).
    existing = SMSCampaign.objects.filter(
        organization=org, idempotency_key=idempotency_key,
    ).first()
    if existing:
        return CampaignSendResult(
            campaign=existing, recipient_count=existing.audience_size,
            cost_cents=cost_cents, cost_tokens=cost_tokens,
            scheduled=scheduled, created=False,
        )

    # Block a send that would push its send day past the shared daily carrier cap — up front,
    # so the organizer trims the list, rather than deferring delivery to the next day. Day-aware:
    # measured against the cap minus what's sent today + already scheduled for that day.
    capacity = daily_capacity_for(send_at)
    if capacity is not None:
        segs = [footer_plan[r['phone']][1] for r in recipients]
        allowed = fit_within_budget(segs, capacity)
        if allowed < count:
            raise DailyCapExceededError(count, allowed, daily_segment_cap(), timezone.localdate(send_at))

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
            # No charge here: the wallet is debited at SEND time (see
            # send_sms_campaign_task), for the segments that actually go out. The frozen
            # per-recipient segments above are what that charge is computed from, so the
            # send-time debit equals this schedule-time estimate (minus any opt-outs).
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


#: SMSCampaign.name is CharField(max_length=200); the overflow batch appends this suffix.
_PART2_SUFFIX = ' (part 2)'
_CAMPAIGN_NAME_MAX = 200


def _part2_name(name):
    """Name for the overflow batch: ``"<name> (part 2)"``, with the base truncated so the
    result never exceeds ``SMSCampaign.name``'s max_length (a long name would otherwise
    pass the composer form but fail the batch-2 DB write on Postgres, after batch 1 has
    already committed and charged)."""
    base = name[:_CAMPAIGN_NAME_MAX - len(_PART2_SUFFIX)]
    return f'{base}{_PART2_SUFFIX}'


def finalize_campaign_split(org, *, name, body, criteria, manual_include_ids, event,
                            scheduled, send_at, batch2_send_at, user, cap,
                            idempotency_key_1, idempotency_key_2):
    """Split a send that would exceed the shared daily carrier cap into two batches:
    as many recipients as fit ``send_at``'s day go out in batch 1, the overflow is
    scheduled for ``batch2_send_at`` (the next day). Any recipients that fit neither
    day are reported as ``leftover_count`` and NOT scheduled.

    Each batch is created through ``finalize_campaign_send`` with an explicit
    ``manual_include_ids`` subset (criteria cleared), so "charged == sent",
    per-campaign idempotency, and the authoritative daily-cap check all still run in
    one place. Each batch is charged at its own send (charge-at-send), so there is no
    schedule-time wallet check here; the usual audience errors still apply. Returns a
    ``SplitResult``.

    Replay safety (the two campaigns are NOT one atomic unit — batch 1 can commit while
    batch 2 fails on a race, and the organizer then retries): each batch is anchored to
    its idempotency key. If a key already produced a campaign, that campaign's frozen
    recipients ARE that batch — they are taken verbatim and EXCLUDED from the pool the
    other batch is drawn from. Without this, a retry would recompute membership from live
    capacity, and batch 1's own booking (now consuming today's budget) would collapse
    ``allowed_today`` to 0 and shove batch-1 recipients back into batch 2 — double-charging
    and double-sending them. Membership is also deterministic because
    ``candidate_customers`` orders by ``(created_at, id)``.
    """
    from tickets.models import SMSCampaign
    from tickets.services.sms_credits import plan_campaign_footers
    from tickets.services.sms_limits import daily_capacity_for, fit_within_budget

    # Idempotency anchors: any campaign already created under each key on a prior
    # (possibly partial) run. Their frozen membership overrides live-capacity sizing.
    existing1 = SMSCampaign.objects.filter(
        organization=org, idempotency_key=idempotency_key_1).first()
    existing2 = SMSCampaign.objects.filter(
        organization=org, idempotency_key=idempotency_key_2).first()

    def _committed_ids(campaign):
        return {
            str(cid) for cid in
            campaign.recipients.values_list('customer_id', flat=True)
            if cid is not None
        }

    # Resolve the full audience once, deterministically (candidate_customers is ordered),
    # so "first N fit today" resolves the same set on every call, including replays.
    recipients = SMSCampaign(
        organization=org, filter_criteria=criteria,
        manual_include_ids=manual_include_ids,
    ).materialize(org, cap=cap + 1)
    count = len(recipients)
    if count > cap:
        raise AudienceTooLargeError(count, cap)
    if count == 0:
        raise AudienceEmptyError()

    # Batch 1: if it already exists, its frozen recipients ARE batch 1; the pool batch 2
    # draws from excludes them. Otherwise size it against the send day's remaining budget.
    if existing1:
        b1_ids = _committed_ids(existing1)
        batch1 = [r for r in recipients if r['customer_id'] in b1_ids]
        pool = [r for r in recipients if r['customer_id'] not in b1_ids]
    else:
        phones_today = [r['phone'] for r in recipients]
        _, plan_today = plan_campaign_footers(org, body, phones_today, as_of=send_at)
        segs_today = [plan_today[p][1] for p in phones_today]
        cap1 = daily_capacity_for(send_at)
        allowed_today = fit_within_budget(segs_today, cap1) if cap1 is not None else count
        batch1 = recipients[:allowed_today]
        pool = recipients[allowed_today:]

    # Batch 2 (drawn from the batch-1-excluded pool): frozen membership on replay,
    # otherwise what fits the next day's budget (footer/segments re-anchored on that day,
    # since a phone's STOP-footer disclosure can age out between the two days).
    if existing2:
        b2_ids = _committed_ids(existing2)
        batch2 = [r for r in pool if r['customer_id'] in b2_ids]
        leftover = 0  # a committed run; leftover isn't meaningful to recompute on replay
    else:
        phones_next = [r['phone'] for r in pool]
        _, plan_next = plan_campaign_footers(org, body, phones_next, as_of=batch2_send_at)
        segs_next = [plan_next[p][1] for p in phones_next]
        cap2 = daily_capacity_for(batch2_send_at)
        fits_next = fit_within_budget(segs_next, cap2) if cap2 is not None else len(pool)
        batch2 = pool[:fits_next]
        leftover = len(pool) - len(batch2)

    # No wallet pre-check: each batch is charged at its own SEND time (see
    # send_sms_campaign_task), so there's no schedule-time debit to protect against a partial
    # split. A batch whose wallet can't cover it at send fails then (never sends unpaid).

    # Create batch 1 (today), then batch 2 (next day). Each batch is pinned to its
    # explicit customer-id set; finalize's own cap check passes because each batch was
    # sized to fit its day (or is a frozen replay). Batch 2 is always a scheduled send.
    # A pre-existing batch is reused as-is (finalize is key-idempotent anyway).
    batch1_campaign = existing1
    if batch1 and not existing1:
        r1 = finalize_campaign_send(
            org, name=name, body=body, criteria={},
            manual_include_ids=[str(r['customer_id']) for r in batch1], event=event,
            scheduled=scheduled, send_at=send_at, user=user,
            idempotency_key=idempotency_key_1, cap=cap,
        )
        batch1_campaign = r1.campaign

    batch2_campaign = existing2
    if batch2 and not existing2:
        r2 = finalize_campaign_send(
            org, name=_part2_name(name), body=body, criteria={},
            manual_include_ids=[str(r['customer_id']) for r in batch2], event=event,
            scheduled=True, send_at=batch2_send_at, user=user,
            idempotency_key=idempotency_key_2, cap=cap,
        )
        batch2_campaign = r2.campaign

    return SplitResult(
        batch1=batch1_campaign, batch2=batch2_campaign,
        batch1_count=len(batch1), batch2_count=len(batch2),
        leftover_count=leftover,
    )
