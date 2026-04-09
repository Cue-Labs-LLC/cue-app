"""
tickets/signals.py

Keeps Event.computed_total_revenue, cached_ticket_count, cached_paid_ticket_count,
and cached_paid_ticket_sum in sync with TicketOrder, EventIncome, and Ticket changes.

Uses transaction.on_commit() so updates run after each transaction commits, never
on a partial write. The dedup set from the previous version has been removed: it was
rollback-unsafe (a rolled-back transaction left event_ids in the thread-local set
permanently, silently skipping future refreshes). Multiple on_commit registrations
for the same event within one transaction result in multiple DB updates after commit,
which is the safe tradeoff.

IMPORTANT: Revenue aggregation and ticket stats use SEPARATE queries.
Sum('total_amount') runs on TicketOrder rows only. Count/Sum on 'tickets' requires
a TicketOrder→Ticket JOIN (1:many). Combining them in one aggregate inflates revenue:
a $100 order with 5 tickets would sum as $500.
"""
from decimal import Decimal

from django.core.cache import cache as django_cache
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver


def refresh_event_stats(event_id):
    """Recompute and store all denormalized stats for a single event."""
    from .models import Event, EventIncome, TicketOrder
    from .views import _event_stats_cache_key

    # Revenue: TicketOrder-only aggregate (no Ticket join — see module docstring)
    order_total = TicketOrder.objects.filter(event_id=event_id).aggregate(
        t=Coalesce(Sum('total_amount'), Decimal('0.00'))
    )['t']
    income_total = EventIncome.objects.filter(
        event_id=event_id, deleted_at__isnull=True
    ).aggregate(
        t=Coalesce(Sum('amount'), Decimal('0.00'))
    )['t']

    # Ticket stats: separate aggregate with TicketOrder JOIN Ticket
    ticket_stats = TicketOrder.objects.filter(event_id=event_id).aggregate(
        total_tickets=Coalesce(Count('tickets'), 0),
        paid_ticket_count=Coalesce(
            Count('tickets', filter=Q(tickets__price__gt=0, refunded_at__isnull=True)),
            0,
        ),
        paid_ticket_sum=Coalesce(
            Sum('tickets__price', filter=Q(tickets__price__gt=0, refunded_at__isnull=True)),
            Decimal('0.00'),
        ),
    )

    Event.objects.filter(pk=event_id).update(
        computed_total_revenue=order_total + income_total,
        cached_ticket_count=ticket_stats['total_tickets'],
        cached_paid_ticket_count=ticket_stats['paid_ticket_count'],
        cached_paid_ticket_sum=ticket_stats['paid_ticket_sum'],
    )

    # Invalidate event_stats cache so the next page load recomputes fresh
    django_cache.delete(_event_stats_cache_key(event_id))

    # Invalidate event_list cache so stats are live (especially for direct ticketing)
    event = Event.objects.filter(pk=event_id).select_related('organization').first()
    if event:
        from .views import _invalidate_event_list_cache
        _invalidate_event_list_cache(event.organization)


def _schedule_refresh(event_id):
    transaction.on_commit(lambda: refresh_event_stats(event_id))


@receiver(post_save, sender='tickets.TicketOrder')
@receiver(post_delete, sender='tickets.TicketOrder')
def on_ticket_order_change(sender, instance, **kwargs):
    _schedule_refresh(instance.event_id)


@receiver(post_save, sender='tickets.EventIncome')
@receiver(post_delete, sender='tickets.EventIncome')
def on_event_income_change(sender, instance, **kwargs):
    _schedule_refresh(instance.event_id)


@receiver(post_save, sender='tickets.Ticket')
@receiver(post_delete, sender='tickets.Ticket')
def on_ticket_change(sender, instance, **kwargs):
    """Covers direct ticket price changes (e.g., via admin). CSV uses bulk_create → no signal."""
    if instance.ticket_order_id:
        from .models import TicketOrder
        event_id = TicketOrder.objects.filter(
            pk=instance.ticket_order_id
        ).values_list('event_id', flat=True).first()
        if event_id:
            _schedule_refresh(event_id)


@receiver(post_save, sender='tickets.EventExpense')
@receiver(post_delete, sender='tickets.EventExpense')
def on_expense_change(sender, instance, **kwargs):
    """Expense changes affect total_expenses and expenses display in cached stats."""
    from .views import _event_stats_cache_key
    django_cache.delete(_event_stats_cache_key(instance.event_id))


@receiver(post_save, sender='tickets.SurveyResponse')
def on_survey_response_change(sender, instance, **kwargs):
    """New survey response affects survey_responses_count and survey_results."""
    from .views import _event_stats_cache_key
    django_cache.delete(_event_stats_cache_key(instance.event_id))


@receiver(post_save, sender='tickets.ExternalSurveyResponse')
def on_external_survey_response_change(sender, instance, **kwargs):
    """New external survey response affects ext_count and NPS results.
    Note: survey_event_link() uses queryset.update() which bypasses this signal —
    that view handles invalidation manually."""
    from .views import _event_stats_cache_key
    django_cache.delete(_event_stats_cache_key(instance.event_id))


@receiver(post_save, sender='tickets.SaleableTicketType')
@receiver(post_delete, sender='tickets.SaleableTicketType')
def on_saleable_ticket_type_change(sender, instance, **kwargs):
    """Ticket type changes affect ticket_type_breakdown for direct events."""
    from .views import _event_stats_cache_key
    django_cache.delete(_event_stats_cache_key(instance.event_id))
