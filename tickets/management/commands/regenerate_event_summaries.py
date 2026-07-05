"""Daily scan that regenerates stale AI event summaries.

Enqueues one Celery task per candidate event; each task decides — using a
fingerprint of the summary's input data — whether the underlying data actually
changed and the summary needs regenerating. Candidates are limited to events
that already have a summary, whose org has both the display toggle and the
auto-regenerate opt-in enabled, and that ended within the configured lookback
window. The precise "has the event ended?" check is done per-event in the task
(it needs the event's timezone), so this command uses a coarse date filter only.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from tickets.models import Event
from tickets.tasks import regenerate_event_summary_task


class Command(BaseCommand):
    help = "Regenerate AI event summaries whose underlying data changed."

    def add_arguments(self, parser):
        parser.add_argument(
            '--event-id',
            dest='event_id',
            help='Restrict to a single event UUID.',
        )
        parser.add_argument(
            '--organization-id',
            dest='organization_id',
            help='Restrict to a single organization UUID.',
        )
        parser.add_argument(
            '--lookback-days',
            dest='lookback_days',
            type=int,
            help='Override EVENT_SUMMARY_REGEN_LOOKBACK_DAYS for this run.',
        )
        parser.add_argument(
            '--sync',
            action='store_true',
            help='Run inline instead of enqueueing Celery tasks.',
        )

    def handle(self, *args, **options):
        today = timezone.localdate()
        lookback_days = options.get('lookback_days')
        if lookback_days is None:
            lookback_days = getattr(settings, 'EVENT_SUMMARY_REGEN_LOOKBACK_DAYS', 180)
        cutoff = today - timedelta(days=lookback_days)

        qs = (
            Event.objects.filter(
                deleted_at__isnull=True,
                organization__ai_event_summary_enabled=True,
                organization__ai_event_summary_auto_regenerate=True,
            )
            .exclude(ai_summary='')
            # Coarse "already started/ended" bound; the task confirms the exact
            # end using end_datetime(). start_date <= end_date, so filtering on
            # start_date is a safe superset.
            .filter(start_date__lte=today)
            # Recent-window guardrail: skip events whose end anchor is older than
            # the cutoff. end_date falls back to start_date when null.
            .filter(
                Q(end_date__gte=cutoff)
                | Q(end_date__isnull=True, start_date__gte=cutoff)
            )
            .order_by('-start_date')
        )

        if options.get('event_id'):
            qs = qs.filter(id=options['event_id'])
        if options.get('organization_id'):
            qs = qs.filter(organization_id=options['organization_id'])

        count = 0
        for event_id in qs.values_list('id', flat=True).iterator():
            count += 1
            if options['sync']:
                result = regenerate_event_summary_task(str(event_id))
                self.stdout.write(f'{event_id}: {result}')
            else:
                regenerate_event_summary_task.delay(str(event_id))

        verb = 'processed' if options['sync'] else 'queued'
        self.stdout.write(self.style.SUCCESS(f'{verb} {count} event(s).'))
