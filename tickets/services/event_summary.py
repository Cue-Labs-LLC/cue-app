"""
EventSummaryService — generates LLM-powered event summaries via streaming.

Produces a structured analysis of event results, attendee feedback, and
financial performance. Streams tokens via SSE and persists the result
to Event.ai_summary.
"""

import json
import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


EVENT_SUMMARY_PROMPT = """\
You are an event analytics assistant for "{org_name}".
Analyze the following event data and provide a concise summary with three sections:

## Event Results
Summarize attendance, ticket sales, capacity utilization, and ticket type mix.

## Attendee Feedback
Summarize survey results including star ratings, NPS score, and notable attendee comments. \
If no survey data is available, note that.

## Financial Performance
Summarize revenue breakdown, expenses by category, profit/loss, and margins. \
Highlight anything notable (very high/low numbers, strong or weak margins).

Keep the tone professional and actionable. Be concise — aim for 3-5 sentences per section. \
Use markdown formatting (headers, bold, bullets). If any data section is empty or has no data, \
note that briefly and move on.

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

    def __init__(self, organization):
        self.organization = organization

    def stream_summary(self, event, event_data):
        """Generator yielding SSE-formatted chunks. Saves result to event.ai_summary."""
        from langchain_openai import ChatOpenAI

        prompt = self._build_prompt(event, event_data)

        try:
            llm = ChatOpenAI(
                model=getattr(settings, 'OPENAI_MODEL', 'gpt-4o'),
                api_key=getattr(settings, 'OPENAI_API_KEY', ''),
                temperature=0.3,
                streaming=True,
            )
        except Exception as e:
            logger.error("Failed to initialize LLM for event summary: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'content': 'The AI summary service is not available right now. Please check that the OpenAI API key is configured.'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        full_response = ""
        try:
            for chunk in llm.stream([{"role": "user", "content": prompt}]):
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

        # Persist the summary
        if full_response.strip():
            event.ai_summary = full_response
            event.ai_summary_generated_at = timezone.now()
            event.save(update_fields=['ai_summary', 'ai_summary_generated_at'])

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
