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
from tickets.sms import sms_segment_info, with_stop_footer

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
    "- Match cadence to the runway: space touches out when the event is far off, and "
    "tighten them as it nears. Never recommend more touches than the timeline supports; "
    "3-5 total is typical. Do not over-message.\n"
    "- Give each touch a distinct job and angle (announce -> value/proof -> urgency), "
    "so no two messages feel repetitive.\n"
    "- One clear call-to-action per message. Always include the ticket link when one is "
    "provided.\n"
    "- Keep each message short — ideally a single GSM-7 segment (~160 chars) INCLUDING "
    "the 'Reply STOP' footer that is appended automatically, so leave room for it. Do "
    "NOT write the STOP footer yourself.\n"
    "- Tailor tone to the audience (VIPs vs. new subscribers vs. lapsed customers), but "
    "always within the organizer's established brand voice.\n"
    "- No emojis, UNLESS the organizer's own voice samples use them — then match their "
    "usage. Never invent facts — only use the dates, prices, names, and links "
    "provided. If a detail is missing, write around it rather than guessing.\n"
    "- Personalize with the audience's context, but do not fabricate personal data.\n"
)


class PlanStep(BaseModel):
    purpose: Literal[
        'announcement', 'early_bird', 'social_proof', 'reminder',
        'last_chance', 'thank_you', 're_engagement',
    ] = Field(description="The job this touch does in the sequence.")
    audience: str = Field(
        description="Short label for who this touch targets (e.g. 'VIP & Loyal', 'All subscribers')."
    )
    timing: str = Field(
        description=(
            "Human-readable send timing relative to the event date or a segment cadence, "
            "e.g. 'T-14 days', 'Day of · 5pm', '3 days after last purchase'."
        )
    )
    message: str = Field(
        description=(
            "The SMS body to send (max ~300 chars). Do NOT include a 'Reply STOP' footer — "
            "it is appended automatically."
        )
    )
    rationale: str = Field(
        description="One sentence: why this touch, to this audience, at this time helps."
    )


class CampaignPlan(BaseModel):
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
    from tickets.models import SMSCampaign, SMSMessageRecipient
    from tickets.sms_views import _sms_buy_stats

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

    rows = []
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

    rows.sort(key=lambda r: (r['ctr'], r['orders']), reverse=True)
    return rows[:limit]


def _recent_campaign_bodies(organization, limit=8):
    """Recent SENT campaign message bodies — the organizer's brand voice samples.

    Ranked by recency (not performance) so the model mirrors how the org writes *now*.
    Deduped on exact body; returns [] when there's no history.
    """
    from tickets.models import SMSCampaign

    bodies = (
        SMSCampaign.objects
        .filter(organization=organization, deleted_at__isnull=True,
                status=SMSCampaign.Status.SENT)
        .exclude(body='')
        .order_by('-sent_at', '-created_at')
        .values_list('body', flat=True)[:40]
    )
    seen = set()
    out = []
    for body in bodies:
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
        'days_until_event': days_until,
        'venue': getattr(event.venue, 'name', '') if event.venue_id else '',
        'city': getattr(event.venue, 'city', '') if event.venue_id else '',
        'market': getattr(event.market, 'name', '') if event.market_id else '',
        'capacity': event.capacity,
    }


def _build_step_criteria(base_criteria, event):
    """The filter_criteria a launched step should prefill the composer with.

    v1: reuse the plan's base audience for every step. (The model's per-step ``audience``
    label is advisory; the organizer can still narrow the audience in the composer.)
    Event plans resolve to the event's attendees.
    """
    if event is not None:
        return {'event_id': str(event.id)}
    return dict(base_criteria or {})


def generate_campaign_plan(organization, *, event=None, criteria=None, objective='',
                           ticket_url='', user=None):
    """Generate a structured multi-touch SMS plan. Returns a dict:

        {'strategy_summary': str, 'steps': [ {order, purpose, audience_label,
         audience_criteria, timing_label, body, rationale, segments, encoding}, ... ],
         'model_name': str}

    Raises SMSStrategistError when the LLM can't be initialized/called.
    """
    from langchain_openai import ChatOpenAI

    model_name = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')

    if event is not None:
        target = {'type': 'event', 'event': _event_context(event)}
    else:
        target = {'type': 'segment', 'segment': _segment_context(organization, criteria)}

    context = {
        'organization': organization.name,
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

    base_criteria = _build_step_criteria(criteria, event)
    steps = []
    for i, step in enumerate(result.steps):
        encoding, segments = sms_segment_info(with_stop_footer(step.message))
        steps.append({
            'order': i,
            'purpose': step.purpose,
            'audience_label': step.audience,
            'audience_criteria': base_criteria,
            'timing_label': step.timing,
            'body': step.message,
            'rationale': step.rationale,
            'segments': segments,
            'encoding': encoding,
            'launched_campaign_id': None,
        })

    return {
        'strategy_summary': result.strategy_summary,
        'steps': steps,
        'model_name': model_name,
    }


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    return str(value)
