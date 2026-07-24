"""Daily carrier-throughput guard for marketing SMS.

The brand's A2P 10DLC registration has a T-Mobile daily cap (2,000 message segments/day at
a low trust score) that is shared across EVERY org on the Twilio account. Blowing past it
gets traffic blanket-filtered (Error 30007). These helpers let the composer BLOCK a send
that would exceed the cap — up front, so the organizer can trim the recipient list — rather
than discovering the cap by getting filtered.

The block is day-aware: a send is measured against the budget for its own send day
(``daily_capacity_for``), i.e. the cap minus what's already been sent today plus what's
already scheduled for that day. A thin send-time guard fails a campaign outright (never
partially, never deferred) if a race slips a day over budget.

We can't cheaply tell which recipients are on T-Mobile, so the guard counts ALL segments
against SMS_DAILY_SEGMENT_CAP. That's conservative (it may block slightly earlier than
strictly necessary), which is the safe direction.
"""
from datetime import datetime, time

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone


def daily_segment_cap():
    """Account-wide daily segment ceiling, or None when the guard is disabled (cap <= 0)."""
    cap = getattr(settings, 'SMS_DAILY_SEGMENT_CAP', 2000)
    return cap if cap and cap > 0 else None


def _start_of_today():
    """Aware datetime for local midnight today (the daily budget resets here)."""
    return timezone.make_aware(datetime.combine(timezone.localdate(), time.min))


def segments_used_today(now=None):
    """Total SMS segments actually handed to carriers today, across ALL orgs.

    Counts SMSMessageRecipient.segments for rows with a twilio_sid (Twilio accepted the
    send) whose sent_at falls on or after local midnight. The segment count is the one
    frozen/charged at schedule time, which matches what the carrier billed against the cap.
    """
    from tickets.models import SMSMessageRecipient

    start = _start_of_today() if now is None else timezone.make_aware(
        datetime.combine(timezone.localdate(now), time.min)
    )
    agg = (
        SMSMessageRecipient.objects
        .filter(sent_at__gte=start)
        .exclude(twilio_sid='')
        .aggregate(total=Sum('segments'))
    )
    return agg['total'] or 0


def remaining_daily_budget(now=None):
    """Segments still sendable today before hitting the cap. None = guard disabled (no limit).

    Never negative — if today's usage already exceeds the cap, returns 0. Used by the
    send-time last-resort guard; the compose-time block uses ``daily_capacity_for``.
    """
    cap = daily_segment_cap()
    if cap is None:
        return None
    return max(0, cap - segments_used_today(now=now))


def segments_scheduled_for(local_date, exclude_campaign_id=None):
    """Segments already committed to `local_date` by not-yet-sent scheduled campaigns, across
    ALL orgs. Scheduled campaigns freeze their recipient rows (with `segments` set) at compose
    time, so summing those is an accurate forecast of that day's booked load. `exclude_campaign_id`
    drops one campaign (e.g. the one being re-checked)."""
    from tickets.models import SMSCampaign, SMSMessageRecipient

    start = timezone.make_aware(datetime.combine(local_date, time.min))
    end = start + timezone.timedelta(days=1)
    qs = SMSMessageRecipient.objects.filter(
        campaign__status=SMSCampaign.Status.SCHEDULED,
        campaign__scheduled_at__gte=start,
        campaign__scheduled_at__lt=end,
    )
    if exclude_campaign_id:
        qs = qs.exclude(campaign_id=exclude_campaign_id)
    return qs.aggregate(total=Sum('segments'))['total'] or 0


def daily_capacity_for(send_at, exclude_campaign_id=None):
    """Segments available for a send at `send_at` before its day hits the cap. None = guard
    disabled (no limit). Day-aware: subtracts what's already sent today (only if `send_at` is
    today) plus what's already scheduled for that day."""
    cap = daily_segment_cap()
    if cap is None:
        return None
    d = timezone.localdate(send_at)
    used = segments_used_today() if d == timezone.localdate() else 0
    booked = segments_scheduled_for(d, exclude_campaign_id=exclude_campaign_id)
    return max(0, cap - used - booked)


def fit_within_budget(segment_counts, budget):
    """Count how many recipients (in the given order) fit within `budget` segments.

    Shared by the send orchestrator (to decide what to dispatch today) and the composer
    (to warn how many will defer) so the warning matches what actually happens. The first
    recipient always fits when budget > 0, so a near-cap send still makes progress even if
    its first message is multi-segment. `budget` None → no limit (all fit).
    """
    if budget is None:
        return len(segment_counts)
    if budget <= 0:
        return 0
    used = fit = 0
    for seg in segment_counts:
        seg = seg or 1  # every message is at least one segment
        if used + seg > budget and fit:
            break
        used += seg
        fit += 1
        if used >= budget:
            break
    return fit
