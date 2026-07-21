"""
EventSummaryService — generates LLM-powered event debriefs via streaming.

Produces a forward-looking analysis that *synthesizes* across an event's sales,
attendee segments, survey sentiment, and finances — surfacing what worked, what
underperformed, and concrete next steps — rather than restating the figures already
visible on the event detail page. Streams tokens via SSE, persists the result to
Event.ai_summary, and records token usage for billing.
"""

import hashlib
import json
import logging

from django.conf import settings
from django.utils import timezone

from tickets.models import AITokenUsage, TICKETING_TYPE_DIRECT
from tickets.services.ai_metering import TokenUsageAccumulator, record_ai_token_usage

logger = logging.getLogger(__name__)


EVENT_SUMMARY_PROMPT = """\
You are an event-performance analyst for "{org_name}". You are writing a post-event \
debrief for the organizer, who can already see every raw number on their dashboard. \
Your job is NOT to repeat those numbers back to them — it is to connect the dots \
*across* sales, attendee segments, survey sentiment, and finances and tell them what \
to do next. Treat all submitted survey responses as one whole — never distinguish how \
the survey was collected. Among survey signals, the NPS score is the single most \
important measure of attendee satisfaction: lead with it, anchor your read of how the \
crowd felt on it, and weight it above star rating, ratings breakdown, and comments. \
Then use the structured answers (what attendees enjoyed, requested improvements, crowd \
vibe, venue feel, discovery channel) and comments to explain the "why" behind that NPS \
and to ground your recommendations — they support the NPS story rather than compete with \
it. When a Check-In section is present, factor the door check-in / no-show rate into \
your analysis and recommendations.

Write four short markdown sections, in this order:

## Headline
One sentence: the overall verdict on how this event performed.

## What worked
2-4 bullets of genuine, cross-section wins. Each bullet must connect at least two \
different signals (e.g. a ticket tier that sold out AND drove the highest NPS; strong \
margin despite a high venue cost; a customer segment that over-indexed on attendance). \
Cite the specific number only as evidence for the insight, never as the point itself.

## What underperformed
2-4 bullets connecting weak signals across sections (e.g. slow sales in a tier that \
also drew low ratings; thin margin driven by a specific expense category; a segment \
that under-attended). If something looks fine, say so briefly rather than inventing a \
problem.

## Recommended next steps
2-4 concrete, specific actions for the organizer's NEXT event, each tied to an insight \
above (pricing, capacity, marketing channel, segment targeting, expense control, survey \
follow-up). Make them actionable, not generic advice.

Rules: Be concise and professional. No emojis. Use only the data provided — do not \
invent numbers. If a data section is empty, work with what you have rather than \
dwelling on the gap. Prioritize synthesis over recitation: if a bullet just restates a \
single figure from the data below, cut it.{allocation_caveat}

---
EVENT DATA:
Event: {event_name}
Venue: {venue_name}, {venue_city}
Date: {event_date}
Capacity: {capacity}

Ticket Sales:
- Total Orders: {total_orders}
- Total Tickets: {total_tickets}
- Unique Customers: {total_customers}
- Capacity Utilization: {utilization_pct}

{ticket_breakdown_heading}
{ticket_type_lines}

Attendee Segments (RFM):
{attendee_segment_lines}

Revenue:
- Gross Ticket Revenue: ${ticket_revenue}
- Platform Fees: ${ticket_fees}
- Net Ticket Revenue: ${net_ticket_revenue}
- Additional Income: ${total_additional_income}
- Total Revenue: ${total_revenue}

Expenses (Total: ${total_expenses}):
{expense_lines}

Profit/Loss: ${profit} (Margin: {margin_pct})

Survey Results (all attendees who submitted a survey):
- Total Responses: {survey_response_count}
- Average Star Rating: {avg_star_rating}
- NPS Score: {nps_score} (from {nps_rated_count} rated responses)
- Promoters / Passives / Detractors: {promoters_pct}% / {passives_pct}% / {detractors_pct}%
- Rating Breakdown: {overall_rating_breakdown}
- Recent Comments:
{comment_lines}
{survey_structured_section}{checkin_section}"""


class EventSummaryService:
    """Generates LLM-powered event summaries, scoped to an organization."""

    def __init__(self, organization, user=None):
        self.organization = organization
        self.user = user

    def stream_summary(self, event, event_data, rate_limit_key=None):
        """Generator yielding SSE-formatted chunks. Saves result to event.ai_summary.

        When ``rate_limit_key`` is given, the per-org hourly counter is incremented
        only after a summary is successfully produced, so failed attempts (e.g. a
        missing OpenAI key) don't consume the budget.
        """
        from langchain_openai import ChatOpenAI

        prompt = self._build_prompt(event, event_data)
        model_name = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')

        try:
            llm = ChatOpenAI(
                model=model_name,
                api_key=getattr(settings, 'OPENAI_API_KEY', ''),
                temperature=0.3,
                streaming=True,
                stream_usage=True,
            )
        except Exception as e:
            logger.error("Failed to initialize LLM for event summary: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'content': 'The AI summary service is not available right now. Please check that the OpenAI API key is configured.'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        full_response = ""
        usage = TokenUsageAccumulator()
        try:
            for chunk in llm.stream([{"role": "user", "content": prompt}]):
                usage.add(chunk)
                if chunk.content:
                    full_response += chunk.content
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk.content})}\n\n"
        except Exception as e:
            logger.error("Event summary streaming error: %s", e)
            error_msg = "An error occurred while generating the summary. Please try again."
            yield f"data: {json.dumps({'type': 'error', 'content': error_msg})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

        # Persist the summary and record token usage for billing
        if full_response.strip():
            self._persist_summary(event, full_response, prompt, model_name, usage.total())

            # Count only successful generations against the hourly rate limit.
            if rate_limit_key:
                try:
                    from django.core.cache import cache as django_cache
                    current = django_cache.get(rate_limit_key, 0) or 0
                    django_cache.set(rate_limit_key, current + 1, timeout=3600)
                except Exception:
                    pass

    def generate_summary(self, event, event_data):
        """Generate and persist an event summary without streaming.

        Used by the daily auto-regeneration task, where there is no client to
        stream to. Returns the summary text on success, or None if generation
        failed (e.g. missing API key). Persists ai_summary, its timestamp, and
        the input fingerprint, and records token usage for billing — mirroring
        the streaming path.
        """
        from langchain_openai import ChatOpenAI

        prompt = self._build_prompt(event, event_data)
        model_name = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')

        try:
            llm = ChatOpenAI(
                model=model_name,
                api_key=getattr(settings, 'OPENAI_API_KEY', ''),
                temperature=0.3,
            )
            response = llm.invoke([{"role": "user", "content": prompt}])
        except Exception as e:
            logger.error("Event summary generation failed for event %s: %s", event.id, e)
            return None

        text = (response.content or "").strip()
        if not text:
            logger.warning("Event summary generation returned empty text for event %s", event.id)
            return None

        self._persist_summary(event, text, prompt, model_name, response)
        return text

    def input_fingerprint(self, event, event_data):
        """Return a stable fingerprint of the data that feeds the summary.

        The prompt string is a deterministic function of the event and its
        computed stats, so hashing it captures exactly what the summary depends
        on — and nothing else. The daily job compares this against the stored
        Event.ai_summary_input_hash to detect whether the summary is stale.
        """
        prompt = self._build_prompt(event, event_data)
        return self._fingerprint(prompt)

    @staticmethod
    def _fingerprint(prompt):
        return hashlib.sha256(prompt.encode('utf-8')).hexdigest()

    def _persist_summary(self, event, text, prompt, model_name, usage):
        """Save a generated summary + its fingerprint and record token usage."""
        event.ai_summary = text
        event.ai_summary_generated_at = timezone.now()
        event.ai_summary_input_hash = self._fingerprint(prompt)
        event.save(update_fields=[
            'ai_summary', 'ai_summary_generated_at', 'ai_summary_input_hash',
        ])

        record_ai_token_usage(
            organization=self.organization,
            feature=AITokenUsage.FEATURE_EVENT_SUMMARY,
            model_name=model_name,
            user=self.user,
            usage=usage,
            metadata={'event_id': str(event.id)},
        )

    def _build_prompt(self, event, event_data):
        """Format the prompt template with event data."""
        # Direct events have real ticket allocations; external (CSV-upload) events
        # don't carry allocation/availability totals, so we must not let the model
        # reason about sell-through for them.
        is_direct = event.ticketing_type == TICKETING_TYPE_DIRECT

        # Ticket type breakdown
        breakdown = event_data.get('ticket_type_breakdown', [])
        if breakdown:
            ticket_type_lines = "\n".join(
                f"- {item['label']}: {item['count']} sold"
                for item in breakdown
            )
        else:
            ticket_type_lines = "- No ticket type data available"

        if is_direct:
            ticket_breakdown_heading = "Ticket Type Breakdown:"
            allocation_caveat = ""
        else:
            ticket_breakdown_heading = (
                "Ticket Type Breakdown (tickets sold; allocation totals not available):"
            )
            allocation_caveat = (
                " Ticket allocation and availability totals are unknown for this event — "
                "only the number of tickets sold is known. Do NOT draw any conclusion about "
                "sell-through rate, percent sold, capacity utilization, or whether any tier "
                "sold out."
            )

        # Attendee segment breakdown
        segments = event_data.get('attendee_segments', [])
        if segments:
            attendee_segment_lines = "\n".join(
                f"- {s['segment']}: {s['count']} ({s['pct']}%)"
                for s in segments
            )
        else:
            attendee_segment_lines = "- No segment data available"

        # Expense breakdown
        expenses_by_category = event_data.get('expenses_by_category', [])
        if expenses_by_category:
            from tickets.models import EventExpense
            category_labels = dict(EventExpense.CATEGORY_CHOICES)
            expense_lines = "\n".join(
                f"- {category_labels.get(item['category'], item['category']).title()}: ${item['total']:,.2f}"
                for item in expenses_by_category
            )
        else:
            expense_lines = "- No expenses recorded"

        # Survey data — all submitted responses treated as one whole
        survey = event_data.get('survey_results')
        if survey:
            survey_response_count = survey.get('total', 0)
            nps_rated_count = survey.get('nps_total', 0)
            promoters_pct = survey.get('promoters_pct', 0)
            passives_pct = survey.get('passives_pct', 0)
            detractors_pct = survey.get('detractors_pct', 0)
            avg_star_rating = f"{survey['avg_star_rating']}/5" if survey.get('avg_star_rating') else "N/A"
            nps_score = str(survey['nps_score']) if survey.get('nps_score') is not None else "N/A"
            rating_breakdown = survey.get('overall_rating_breakdown', [])
            if rating_breakdown:
                overall_rating_breakdown = ', '.join(
                    f"{r['overall_rating']} ({r['count']})" for r in rating_breakdown
                )
            else:
                overall_rating_breakdown = "N/A"
            comments = survey.get('recent_comments', [])
            if comments:
                comment_lines = "\n".join(
                    f'- "{c["text"]}" — {c["author"]}'
                    for c in comments
                )
            else:
                comment_lines = "- No comments"
        else:
            survey_response_count = 0
            nps_rated_count = 0
            promoters_pct = passives_pct = detractors_pct = 0
            avg_star_rating = "N/A"
            nps_score = "N/A"
            overall_rating_breakdown = "N/A"
            comment_lines = "- No survey data available"

        # Structured survey answers (multi/single-select questions). Presented as
        # ordinary survey feedback — never labeled by collection channel.
        structured = (survey or {}).get('ext_structured') or {}
        structured_specs = [
            ('enjoyed', 'Top things enjoyed'),
            ('genres', 'Top genres'),
            ('improvements', 'Most requested improvements'),
            ('crowd_vibe', 'Crowd vibe'),
            ('venue_feel', 'Venue feel'),
            ('pre_event_info', 'Pre-event info'),
            ('found_out_how', 'How attendees discovered the event'),
        ]
        structured_lines = []
        for key, label in structured_specs:
            items = structured.get(key) or []
            if items:
                parts = ', '.join(f"{i['label']} ({i['count']})" for i in items)
                structured_lines.append(f"- {label}: {parts}")
        survey_structured_section = (
            "\n".join(structured_lines) + "\n" if structured_lines else ""
        )

        # Check-in (door attendance) — only present for direct events past start
        checkin = event_data.get('checkin') or {}
        if checkin.get('show') and checkin.get('total_tickets'):
            type_lines = "\n".join(
                f"- {t['label']}: {t['checked_in']}/{t['total']} ({t['percent']}%)"
                for t in checkin.get('by_type', [])
            ) or "- No per-type data"
            checkin_section = (
                "\nCheck-In (door attendance):\n"
                f"- Checked In: {checkin['checked_in']} of {checkin['total_tickets']} "
                f"({checkin['percent']}%)\n"
                f"{type_lines}\n"
            )
        else:
            checkin_section = ""

        # Capacity utilization — only meaningful for direct events with a real allocation.
        capacity = event.capacity
        total_tickets = event_data['total_tickets']
        if not is_direct:
            utilization_pct = "Not available — uploaded events don't include ticket allocation totals"
        elif capacity and capacity > 0:
            utilization_pct = f"{total_tickets / capacity * 100:.1f}%"
        else:
            utilization_pct = "N/A (no capacity set)"

        # Margin
        margin_pct = event_data.get('margin_pct')
        margin_str = f"{margin_pct:.1f}%" if margin_pct is not None else "N/A"

        return EVENT_SUMMARY_PROMPT.format(
            org_name=self.organization.name,
            event_name=event.name,
            venue_name=event.venue.name,
            venue_city=event.venue.city,
            event_date=event.start_date,
            capacity=capacity or "Not set",
            total_orders=event_data['total_orders'],
            total_tickets=total_tickets,
            total_customers=event_data['total_customers'],
            utilization_pct=utilization_pct,
            ticket_breakdown_heading=ticket_breakdown_heading,
            allocation_caveat=allocation_caveat,
            ticket_type_lines=ticket_type_lines,
            attendee_segment_lines=attendee_segment_lines,
            ticket_revenue=f"{event_data['ticket_revenue']:,.2f}",
            ticket_fees=f"{event_data['ticket_fees']:,.2f}",
            net_ticket_revenue=f"{event_data['net_ticket_revenue']:,.2f}",
            total_additional_income=f"{event_data['total_additional_income']:,.2f}",
            total_revenue=f"{event_data['total_revenue']:,.2f}",
            total_expenses=f"{event_data['total_expenses']:,.2f}",
            expense_lines=expense_lines,
            profit=f"{event_data['profit']:,.2f}",
            margin_pct=margin_str,
            survey_response_count=survey_response_count,
            nps_rated_count=nps_rated_count,
            promoters_pct=promoters_pct,
            passives_pct=passives_pct,
            detractors_pct=detractors_pct,
            avg_star_rating=avg_star_rating,
            nps_score=nps_score,
            overall_rating_breakdown=overall_rating_breakdown,
            survey_structured_section=survey_structured_section,
            checkin_section=checkin_section,
            comment_lines=comment_lines,
        )
