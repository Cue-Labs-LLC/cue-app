"""
Signal handlers that keep Event.computed_total_revenue in sync with
TicketOrder and EventIncome changes.

Uses transaction.on_commit() so the refresh runs after each chunk's
transaction commits rather than mid-write. A per-thread dedup set
collapses multiple signals for the same event within one transaction
into a single DB update.
"""
import threading
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

_pending_refresh = threading.local()


def refresh_event_total_revenue(event_id):
    """Recompute and store total revenue for a single event."""
    from .models import Event, EventIncome, TicketOrder

    order_total = TicketOrder.objects.filter(event_id=event_id).aggregate(
        t=Coalesce(Sum('total_amount'), Decimal('0.00'))
    )['t']
    income_total = EventIncome.objects.filter(
        event_id=event_id, deleted_at__isnull=True
    ).aggregate(
        t=Coalesce(Sum('amount'), Decimal('0.00'))
    )['t']
    Event.objects.filter(pk=event_id).update(
        computed_total_revenue=order_total + income_total
    )


def _schedule_refresh(event_id):
    """
    Deduplicate refresh calls within a transaction.

    Multiple TicketOrder creates for the same event (e.g. CSV import chunk)
    collapse to a single refresh_event_total_revenue call after commit.
    """
    if not hasattr(_pending_refresh, 'event_ids'):
        _pending_refresh.event_ids = set()
    if event_id in _pending_refresh.event_ids:
        return
    _pending_refresh.event_ids.add(event_id)

    def _do_refresh():
        _pending_refresh.event_ids.discard(event_id)
        refresh_event_total_revenue(event_id)

    transaction.on_commit(_do_refresh)


@receiver(post_save, sender='tickets.TicketOrder')
@receiver(post_delete, sender='tickets.TicketOrder')
def on_ticket_order_change(sender, instance, **kwargs):
    _schedule_refresh(instance.event_id)


@receiver(post_save, sender='tickets.EventIncome')
@receiver(post_delete, sender='tickets.EventIncome')
def on_event_income_change(sender, instance, **kwargs):
    _schedule_refresh(instance.event_id)
