"""
EventSummaryService — generates LLM-powered event debriefs via streaming.

Produces a forward-looking analysis that *synthesizes* across an event's sales,
attendee segments, survey sentiment, and finances — surfacing what worked, what
underperformed, and concrete next steps — rather than restating the figures already
visible on the event detail page. Streams tokens via SSE, persists the result to
Event.ai_summary, and records token usage for billing.
"""

import json
import logging

from django.conf import settings
from django.utils import timezone

from tickets.models import AITokenUsage
from tickets.services.ai_metering import TokenUsageAccumulator, record_ai_token_usage

logger = logging.getLogger(__name__)


EVENT_SUMMARY_PROMPT = """\
You are an event-performance analyst for "{org_name}". You are writing a post-event \
debrief for the organizer, who can already see every raw number on their dashboard. \
Your job is NOT to repeat those numbers back to them — it is to connect the dots \
*across* sales, attendee segments, survey sentiment, and finances and tell them what \
to do next.

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
single figure from the data below, cut it.

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

Ticket Type Breakdown:
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

Survey Results:
- Invitations Sent: {survey_invitations_count}
- Responses Received: {survey_responses_count}
- External Survey Responses: {ext_survey_count}
- Average Star Rating: {avg_star_rating}
- NPS Score: {nps_score}
- Overall Rating Breakdown: {overall_rating_breakdown}
- Recent Comments:
{comment_lines}
"""


class EventSummaryService:
    """Generates LLM-powered event summaries, scoped to an organization."""

    def __init__(self, organization, user=None):
        self.organization = organization
        self.user = user

    def stream_summary(self, event, event_data):
        """Generator yielding SSE-formatted chunks. Saves result to event.ai_summary."""
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
            event.ai_summary = full_response
            event.ai_summary_generated_at = timezone.now()
            event.save(update_fields=['ai_summary', 'ai_summary_generated_at'])

            record_ai_token_usage(
                organization=self.organization,
                feature=AITokenUsage.FEATURE_EVENT_SUMMARY,
                model_name=model_name,
                user=self.user,
                usage=usage.total(),
                metadata={'event_id': str(event.id)},
            )

    def _build_prompt(self, event, event_data):
        """Format the prompt template with event data."""
        # Ticket type breakdown
        breakdown = event_data.get('ticket_type_breakdown', [])
        if breakdown:
            ticket_type_lines = "\n".join(
                f"- {item['label']}: {item['count']} sold"
                for item in breakdown
            )
        else:
            ticket_type_lines = "- No ticket type data available"

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

        # Survey data
        survey = event_data.get('survey_results')
        if survey:
            avg_star_rating = f"{survey['avg_star_rating']}/5" if survey.get('avg_star_rating') else "N/A"
            nps_score = str(survey['nps_score']) if survey.get('nps_score') is not None else "N/A"
            ext_survey_count = survey.get('ext_response_count', 0)
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
            avg_star_rating = "N/A"
            nps_score = "N/A"
            ext_survey_count = 0
            overall_rating_breakdown = "N/A"
            comment_lines = "- No survey data available"

        # Capacity utilization
        capacity = event.capacity
        total_tickets = event_data['total_tickets']
        if capacity and capacity > 0:
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
            survey_invitations_count=event_data['survey_invitations_count'],
            survey_responses_count=event_data['survey_responses_count'],
            ext_survey_count=ext_survey_count,
            avg_star_rating=avg_star_rating,
            nps_score=nps_score,
            overall_rating_breakdown=overall_rating_breakdown,
            comment_lines=comment_lines,
        )
