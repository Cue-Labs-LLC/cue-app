"""AI SMS Campaign Strategist.

Given an event or a customer segment, recommends a multi-touch SMS campaign
*sequence* — a set of timed touches (announcement, reminder, last-chance, …), each
with its target audience, suggested send timing, a rationale, and the written message
text. Grounded in the event/segment's data, the org's own top-performing prior
campaigns, and SMS sequencing best practices.

Advisory only: the plan sends nothing. Each step is launched individually into the
existing composer where the organizer confirms cost and sends. Mirrors the structured
LLM + token-metering pattern in ``services/marketing/ai_narrative.py``.
"""

import json
import logging
from decimal import Decimal
from typing import Literal, Optional

from django.conf import settings
from django.db.models import Count, Q
from pydantic import BaseModel, Field

from tickets.models import AITokenUsage
from tickets.services.ai_metering import record_ai_token_usage
from tickets.sms import (
    contains_emoji, sms_segment_info, strip_authored_stop_footer, strip_emoji,
    with_stop_footer,
)

logger = logging.getLogger(__name__)


class SMSStrategistError(Exception):
    """Raised when the AI campaign plan cannot be generated (config/LLM failure)."""


PURPOSE_CHOICES = (
    'announcement', 'early_bird', 'social_proof', 'reminder',
    'last_chance', 'thank_you', 're_engagement',
)


SYSTEM_PROMPT = (
    "You are an expert SMS marketing strategist for an event-ticketing platform. "
    "Given an event or a customer segment, design a concise multi-touch SMS campaign "
    "SEQUENCE that maximizes ticket sales (or re-engagement) without annoying "
    "subscribers, and write each message.\n\n"
    "MATCH THE BRAND VOICE. The context includes 'brand_voice_samples' — real messages "
    "this organizer has sent before. Study them closely and write every new message so it "
    "sounds like it came from the same brand: mirror their sentence length and rhythm, "
    "capitalization habits, punctuation, level of formality, energy, slang/abbreviations, "
    "how they address the reader, how they refer to their events/venue, and any recurring "
    "phrases or sign-offs. The goal is that the organizer reads your drafts and thinks "
    "'that sounds exactly like us.' Do not impose a generic marketing voice over theirs. "
    "If there are no voice samples, default to a warm, plainspoken tone and keep it "
    "brand-neutral.\n\n"
    "Best practices you MUST follow:\n"
    "- Match cadence to the runway using each step's 'offset_days' (see its schema): "
    "space touches out when the event is far off, and tighten them as it nears. For event "
    "plans, offset_days counts DOWN to the event and must never exceed the days remaining "
    "(context gives 'days_until_event' and 'today'); order steps from the largest offset "
    "to the smallest. Never recommend more touches than the timeline supports; 3-5 total "
    "is typical. Do not over-message.\n"
    "- Give each touch a distinct job and angle (announce -> value/proof -> urgency), "
    "so no two messages feel repetitive.\n"
    "- One clear call-to-action per message. Always include the ticket link when one is "
    "provided.\n"
    "- Keep each message to a SINGLE GSM-7 segment by default. A segment is 160 chars, "
    "and the platform automatically appends a 'Reply STOP to opt out' footer that costs "
    "~23 of those chars — so the body you write MUST be <= ~137 GSM-7 chars (aim for ~130 "
    "to leave a safety margin). Do NOT write the STOP footer yourself. Only exceed one "
    "segment if the message is genuinely impossible to convey shorter.\n"
    "- Write in plain GSM-7 characters to stay one segment. Use a straight hyphen '-' "
    "(never an em-dash '—' or en-dash '–'), straight quotes ' and \" (never curly "
    "quotes ' ' \" \"), and three dots '...' (never the '…' character). Fancy "
    "punctuation silently forces the whole message into UCS-2 encoding, which halves the "
    "per-segment budget to 70 chars and can TRIPLE the send cost of a short message.\n"
    "- Tailor tone to the audience (VIPs vs. new subscribers vs. lapsed customers), but "
    "always within the organizer's established brand voice.\n"
    "- Do NOT use emojis unless the organizer's own brand_voice_samples visibly contain "
    "emojis — then match their usage. Emojis force UCS-2 encoding and cost extra, so "
    "never add them on your own initiative. Never invent facts — only use the dates, "
    "prices, names, and links provided. If a detail is missing, write around it rather "
    "than guessing.\n"
    "- Personalize with the audience's context, but do not fabricate personal data.\n"
    "- Title the plan by its DISTINGUISHING ANGLE (see the 'title' field), so the organizer "
    "can tell several plans for the same event apart at a glance — not by repeating the "
    "event name.\n"
)


class PlanStep(BaseModel):
    purpose: Literal[
        'announcement', 'early_bird', 'social_proof', 'reminder',
        'last_chance', 'thank_you', 're_engagement',
    ] = Field(description="The job this touch does in the sequence.")
    audience: str = Field(
        description="Short label for who this touch targets (e.g. 'VIP & Loyal', 'All subscribers')."
    )
    offset_days: int = Field(
        description=(
            "Whole days from the anchor, as an integer >= 0. For an EVENT plan this is days "
            "BEFORE the event (0 = the day of the event, 14 = two weeks before) and must not "
            "exceed the days remaining until the event. For a SEGMENT plan (no event date) "
            "this is days AFTER the campaign starts (0 = send on day one, 3 = three days later)."
        )
    )
    send_time: str = Field(
        description=(
            "Local send time in 24-hour HH:MM (e.g. '18:00'). Pick a sensible hour for the "
            "audience — late afternoon or early evening usually works best for consumer events. "
            "For a day-of-event touch (offset_days = 0), the send time MUST be before the "
            "event's start_time — never schedule a reminder for after doors open."
        )
    )
    message: str = Field(
        description=(
            "The SMS body to send. Keep it to a single GSM-7 segment by default: <= ~137 "
            "chars (aim for ~130), because a 'Reply STOP' footer costing ~23 chars is "
            "appended automatically to fill the 160-char segment. Do NOT include a "
            "'Reply STOP' footer — it is appended automatically."
        )
    )
    rationale: str = Field(
        description="One sentence: why this touch, to this audience, at this time helps."
    )


class CampaignPlan(BaseModel):
    title: str = Field(
        description=(
            "A short, specific title (max ~60 chars) that lets the organizer tell this plan "
            "apart from OTHER plans for the same event/segment. Capture the distinct angle — "
            "audience, objective, urgency, or timing (e.g. 'LA sellout sprint - 4-day "
            "urgency', 'VIP early-bird re-engagement', 'Final-week last-chance push'). Do NOT "
            "just repeat the event name, and do NOT include the word 'Plan'."
        )
    )
    strategy_summary: str = Field(
        description="2-3 sentences describing the overall sequencing approach and why it fits."
    )
    steps: list[PlanStep] = Field(
        description="The ordered sequence of touches (typically 3-5), first to last."
    )


def _top_prior_campaigns(organization, limit=5):
    """Return the org's best-performing SENT campaigns as compact 'what worked' examples.

    Ranked by click-through, then attributed orders. Degrades to [] when there's no
    history. Reuses ``_sms_buy_stats`` for first-party order/revenue attribution.
    """
    from tickets.models import SMSCampaign, SMSMessageRecipient, EventSMSCampaign
    from tickets.sms_views import _sms_buy_stats

    rows = []

    # Native Cue sends.
    campaigns = list(
        SMSCampaign.objects
        .filter(organization=organization, deleted_at__isnull=True,
                status=SMSCampaign.Status.SENT)
        .annotate(
            sent_count=Count('recipients', filter=Q(
                recipients__status__in=[
                    SMSMessageRecipient.Status.SENT,
                    SMSMessageRecipient.Status.DELIVERED,
                ],
            )),
            click_count=Count('recipients', filter=Q(recipients__click_count__gt=0)),
        )
        .order_by('-sent_at')[:40]
    )
    for c in campaigns:
        sent = c.sent_count or 0
        clicks = c.click_count or 0
        ctr = round(clicks / sent, 3) if sent else 0.0
        try:
            buy = _sms_buy_stats(organization, c) or {}
        except Exception:
            buy = {}
        rows.append({
            'body': c.body,
            'audience': c.audience_summary(organization),
            'sent': sent,
            'ctr': ctr,
            'orders': int(buy.get('orders') or 0),
        })

    # External SlickText broadcasts — often the bulk of an org's history.
    externals = list(
        EventSMSCampaign.objects
        .filter(event__organization=organization, deleted_at__isnull=True)
        .exclude(message='')
        .select_related('event')
        .order_by('-send_time')[:40]
    )
    for c in externals:
        audience = c.effective_audience or 0
        clicks = c.effective_clicks or 0
        ctr = round(clicks / audience, 3) if audience else 0.0
        rows.append({
            'body': c.message,
            'audience': f"SlickText · {c.event.name}",
            'sent': audience,
            'ctr': ctr,
            'orders': int(c.effective_orders or 0),
        })

    rows.sort(key=lambda r: (r['ctr'], r['orders']), reverse=True)
    return rows[:limit]


def _recent_campaign_bodies(organization, limit=8):
    """Recent campaign message bodies — the organizer's brand voice samples.

    Pulls from BOTH native Cue sends (SMSCampaign) and external SlickText broadcasts
    (EventSMSCampaign) — many orgs' entire history lives in SlickText, so ignoring it
    would leave the model with nothing to mirror. Ranked by recency (not performance) so
    the model matches how the org writes *now*. Deduped on exact text; [] when no history.
    """
    from tickets.models import SMSCampaign, EventSMSCampaign

    # (sort_key, text) tuples; sort_key is a POSIX timestamp (0.0 when unknown) so we can
    # order across the two sources without comparing None datetimes.
    items = []
    for body, sent_at in (
        SMSCampaign.objects
        .filter(organization=organization, deleted_at__isnull=True,
                status=SMSCampaign.Status.SENT)
        .exclude(body='')
        .values_list('body', 'sent_at')[:60]
    ):
        items.append((sent_at.timestamp() if sent_at else 0.0, body))
    for message, send_time in (
        EventSMSCampaign.objects
        .filter(event__organization=organization, deleted_at__isnull=True)
        .exclude(message='')
        .values_list('message', 'send_time')[:60]
    ):
        items.append((send_time.timestamp() if send_time else 0.0, message))

    items.sort(key=lambda x: x[0], reverse=True)
    seen = set()
    out = []
    for _, body in items:
        key = (body or '').strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(body)
        if len(out) >= limit:
            break
    return out


def _segment_context(organization, criteria):
    """Describe a segment audience for the prompt: label, resolved size, avg LTV."""
    from django.db.models import Avg
    from tickets.models import SMSCampaign
    from tickets.services.customer_filters import filter_customers

    label = SMSCampaign(organization=organization, filter_criteria=criteria or {}) \
        .audience_summary(organization)
    try:
        qs = filter_customers(organization, criteria or {})
        agg = qs.aggregate(n=Count('id'), avg_ltv=Avg('lifetime_value'))
        size = agg['n'] or 0
        avg_ltv = float(agg['avg_ltv']) if agg['avg_ltv'] is not None else None
    except Exception:
        size, avg_ltv = None, None
    return {'label': label, 'size': size, 'avg_ltv': avg_ltv}


def _event_context(event):
    """Describe an event for the prompt, including days until the event."""
    from django.utils import timezone

    days_until = None
    try:
        days_until = (event.start_date - timezone.localdate()).days
    except Exception:
        pass
    return {
        'name': event.name,
        'date': str(event.start_date),
        'start_time': event.start_time.strftime('%H:%M') if event.start_time else None,
        'days_until_event': days_until,
        'venue': getattr(event.venue, 'name', '') if event.venue_id else '',
        'city': getattr(event.venue, 'city', '') if event.venue_id else '',
        'market': getattr(event.market, 'name', '') if event.market_id else '',
        'capacity': event.capacity,
    }


def _build_step_criteria(organization, base_criteria, event):
    """The filter_criteria a launched step should prefill the composer with.

    Event plans default to the market covering the event's venue (city > state > country)
    when one exists — a geographic campaign to everyone who's come to shows in that market
    beats blasting the whole list for a local event. When no market matches the venue, fall
    back to ALL of the org's SMS subscribers. Either way the campaign stays linked to the
    plan's event for attribution + the ticket link (see ``_plan_step_event``). The organizer
    can narrow any step to the event's ticket buyers or another segment via the audience
    editor. Segment plans reuse the plan's base audience.
    """
    if event is not None:
        from tickets.services.markets import MarketBuilder

        market = MarketBuilder(organization).resolve_for_venue(getattr(event, 'venue', None))
        if market is not None:
            return {'market_ids': [str(market.id)]}
        return {'all_subscribers': True}
    return dict(base_criteria or {})


def plan_audience_label(organization, criteria):
    """Audience label for a plan step, in the SAME wording the composer uses so the plan
    view and the New Campaign page stay consistent ('All SMS subscribers', 'Ticket buyers
    for {event}', or a segment/tag/market summary)."""
    from tickets.models import Event, SMSCampaign

    criteria = criteria or {}
    if criteria.get('all_subscribers'):
        return 'All SMS subscribers'
    event_id = criteria.get('event_id')
    if event_id:
        ev = Event.objects.filter(organization=organization, id=event_id).first()
        return f'Ticket buyers for {ev.name}' if ev else 'Ticket buyers for this event'
    label = SMSCampaign(organization=organization, filter_criteria=criteria).audience_summary(organization)
    return label if label and label != 'No audience' else 'All SMS subscribers'


def generate_campaign_plan(organization, *, event=None, criteria=None, objective='',
                           ticket_url='', user=None):
    """Generate a structured multi-touch SMS plan. Returns a dict:

        {'title': str, 'strategy_summary': str, 'steps': [ {order, purpose, audience_label,
         audience_criteria, timing_label, body, rationale, segments, encoding}, ... ],
         'model_name': str}

    Raises SMSStrategistError when the LLM can't be initialized/called.
    """
    from langchain_openai import ChatOpenAI

    model_name = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')

    from django.utils import timezone
    if event is not None:
        target = {'type': 'event', 'event': _event_context(event)}
    else:
        target = {'type': 'segment', 'segment': _segment_context(organization, criteria)}

    context = {
        'organization': organization.name,
        'today': str(timezone.localdate()),
        'target': target,
        'objective': objective or '(none stated)',
        'ticket_link': ticket_url or '(none — invite them to your ticket page)',
        # Recent messages verbatim — the organizer's brand voice to mirror.
        'brand_voice_samples': _recent_campaign_bodies(organization) or '(no prior messages — use a warm, brand-neutral tone)',
        # Top performers with metrics — what has driven results for this org.
        'prior_campaigns': _top_prior_campaigns(organization) or '(no send history yet)',
    }
    user_content = (
        "Design the SMS campaign sequence for the following context. Write the messages "
        "in this organizer's own brand voice (see brand_voice_samples). Use only the data "
        "provided — do not invent dates, prices, or links.\n\n"
        f"{json.dumps(context, default=_json_default)}"
    )

    try:
        llm = ChatOpenAI(
            model=model_name,
            api_key=getattr(settings, 'OPENAI_API_KEY', ''),
            temperature=0.7,
            stream_usage=True,
        )
        structured_llm = llm.with_structured_output(CampaignPlan, include_raw=True)
        raw_result = structured_llm.invoke([
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': user_content},
        ])
    except Exception as exc:
        logger.error("SMS strategist LLM call failed: %s", exc)
        raise SMSStrategistError(
            "The AI strategist is not available right now. Check that the OpenAI API "
            "key is configured and try again."
        ) from exc

    if isinstance(raw_result, dict) and {'raw', 'parsed', 'parsing_error'} <= set(raw_result):
        record_ai_token_usage(
            organization=organization,
            feature=AITokenUsage.FEATURE_SMS_PLAN,
            model_name=model_name,
            user=user,
            usage=raw_result.get('raw'),
            metadata={'event_id': str(event.id) if event is not None else None},
        )
        if raw_result.get('parsing_error'):
            raise SMSStrategistError("The AI returned an unreadable plan. Please try again.")
        result = raw_result.get('parsed')
    else:
        result = raw_result

    if not isinstance(result, CampaignPlan):
        if hasattr(CampaignPlan, 'model_validate'):
            result = CampaignPlan.model_validate(result)
        else:
            result = CampaignPlan.parse_obj(result)

    base_criteria = _build_step_criteria(organization, criteria, event)
    # Label the audience from the ACTUAL criteria the composer will use — not the LLM's
    # free-text guess — using the composer's terminology so the plan view and the New
    # Campaign page always agree.
    base_label = plan_audience_label(organization, base_criteria)
    org_tz = organization.get_timezone()

    # Only allow emoji in the drafts if the org's OWN recent messages use them; otherwise
    # strip them, enforcing the org's voice and keeping messages in cheap GSM-7. The
    # samples degrade to a placeholder string when there's no history — treat that as
    # no-emoji.
    samples = context['brand_voice_samples']
    voice_uses_emoji = isinstance(samples, list) and any(contains_emoji(s) for s in samples)

    steps = []
    for i, step in enumerate(result.steps):
        # Clean the drafted copy before storing/counting: drop any self-authored
        # 'Reply STOP…' footer (the real one is appended at send time) and, unless the
        # brand voice uses emoji, remove emoji so the message stays one GSM-7 segment.
        body = strip_authored_stop_footer(step.message)
        if not voice_uses_emoji:
            body = strip_emoji(body)
        encoding, segments = sms_segment_info(with_stop_footer(body))
        send_at, timing_label = _compute_step_schedule(step, event, org_tz)
        steps.append({
            'order': i,
            'purpose': step.purpose,
            'audience_label': base_label,
            'audience_criteria': base_criteria,
            'offset_days': max(0, int(step.offset_days or 0)),
            'send_time': step.send_time,
            'send_at': send_at,
            'timing_label': timing_label,
            'body': body,
            'rationale': step.rationale,
            'segments': segments,
            'encoding': encoding,
            'launched_campaign_id': None,
        })

    return {
        'title': result.title,
        'strategy_summary': result.strategy_summary,
        'steps': steps,
        'model_name': model_name,
    }


# Django date-format string for a step's send datetime. The trailing "T" prints the
# timezone abbreviation (e.g. PDT), so the organizer sees which zone the time is in.
SCHEDULE_LABEL_FORMAT = "D, M j · g:i A T"

# Lead time used when a step's ideal send time has already passed today — the nudged
# schedule lands this many minutes in the future rather than in the past.
PAST_STEP_LEAD_MINUTES = 15

# A step must never be scheduled at or after the event has started (e.g. a "doors open
# soon" text landing after doors). When the computed send would fall at/after the event's
# start time, pull it back to this many minutes before the event begins.
EVENT_START_LEAD_MINUTES = 60


def format_send_label(dt):
    """Human label for a send datetime, including the timezone (e.g. 'Mon, Jul 6 · 6:00 PM PDT')."""
    from django.utils import formats
    return formats.date_format(dt, SCHEDULE_LABEL_FORMAT)


def _compute_step_schedule(step, event, tz):
    """Turn a step's structured offset + send time into an absolute datetime + label.

    ``tz`` is the org's timezone. Event plans anchor on the event date (offset = days
    before); segment plans anchor on today (offset = days after campaign start).
    Returns (iso_datetime, display_label) e.g.
    ('2026-07-06T18:00:00-07:00', 'Mon, Jul 6 · 6:00 PM PDT').
    """
    from datetime import datetime, time as dtime, timedelta
    from django.utils import timezone

    now = timezone.now().astimezone(tz)
    today = now.date()
    try:
        hh, mm = (step.send_time or '').split(':')
        send_time = dtime(int(hh), int(mm))
    except (ValueError, TypeError):
        send_time = dtime(18, 0)

    offset = max(0, int(step.offset_days or 0))
    if event is not None:
        send_date = event.start_date - timedelta(days=offset)
        if send_date < today:  # runway shorter than the model assumed — don't schedule in the past
            send_date = today
    else:
        send_date = today + timedelta(days=offset)

    dt = datetime.combine(send_date, send_time, tzinfo=tz)

    # Never schedule a step at/after the event has started — a day-of "doors open soon"
    # text sent after doors is worse than useless. When the event has a known start time
    # and the computed send would land at/after it, pull the send back to a lead before
    # doors so the message still goes out pre-event. Applied before the past guard so an
    # already-started/imminent event still falls through to a sendable "now".
    if event is not None and getattr(event, 'start_time', None):
        event_start = datetime.combine(event.start_date, event.start_time, tzinfo=tz)
        if dt >= event_start:
            dt = event_start - timedelta(minutes=EVENT_START_LEAD_MINUTES)

    # Never schedule in the past. The date guard above keeps the day >= today, but a
    # plan generated in the evening for a near-term event can still land the send time
    # earlier today (e.g. a 4:00 PM touch built at 6:30 PM). Nudge those to a near-future
    # slot so the first touch is always sendable; the organizer can adjust in the composer.
    earliest = (now + timedelta(minutes=PAST_STEP_LEAD_MINUTES)).replace(second=0, microsecond=0)
    if dt < earliest:
        dt = earliest
    return dt.isoformat(), format_send_label(dt)


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    return str(value)
