"""
Data migration: populate cached_ticket_count, cached_paid_ticket_count,
and cached_paid_ticket_sum for all existing events.

Uses inline ORM on historical models — no instance methods (they don't exist
on historical models). Mirrors the pattern from 0086_event_list_perf_indexes.
"""
from decimal import Decimal

from django.db import migrations
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce


def backfill_event_cached_stats(apps, schema_editor):
    Event = apps.get_model('tickets', 'Event')
    TicketOrder = apps.get_model('tickets', 'TicketOrder')

    for event in Event.objects.filter(deleted_at__isnull=True).iterator(chunk_size=50):
        ticket_stats = TicketOrder.objects.filter(event_id=event.pk).aggregate(
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
        event.cached_ticket_count = ticket_stats['total_tickets']
        event.cached_paid_ticket_count = ticket_stats['paid_ticket_count']
        event.cached_paid_ticket_sum = ticket_stats['paid_ticket_sum']
        event.save(update_fields=[
            'cached_ticket_count',
            'cached_paid_ticket_count',
            'cached_paid_ticket_sum',
        ])


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0087_event_perf_cached_stats'),
    ]

    operations = [
        migrations.RunPython(backfill_event_cached_stats, migrations.RunPython.noop),
    ]
