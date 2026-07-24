import logging

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from tickets.models import Event, SurveyInvitation
from tickets.tasks import send_survey_emails_task

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Drive the full automatic post-event survey lifecycle.

    Run on a schedule (Render cron, every 5 minutes). Two phases:

    1. ARM — for every event with an auto-send schedule whose anchor (start/end)
       has passed, create the survey invitations stamped with their computed send
       time. This is what makes "auto-send" actually send: the host no longer has
       to open the Send-survey dialog. Arming waits until the event's anchor has
       passed so the recipient list is settled, and gives the host a cancel window
       before the configured offset elapses.

    2. DISPATCH — mail any scheduled invitation whose send time has arrived.

    The DB is the source of truth for both phases — no task sits in worker memory
    across deploys. Both phases are idempotent: arming skips events that already
    have invitations (or were opted out via cancel), and the send task only mails
    rows whose time has passed and clears them by stamping sent_at, so a double
    cron run is harmless.
    """
    help = "Arm and dispatch scheduled post-event survey invitations."

    def add_arguments(self, parser):
        parser.add_argument(
            '--sync',
            action='store_true',
            help='Run inline instead of enqueueing Celery tasks (debugging).',
        )
        parser.add_argument(
            '--no-arm',
            action='store_true',
            help='Skip the arming phase; only dispatch already-scheduled invitations.',
        )

    def handle(self, *args, **options):
        sync = options.get('sync')

        if not options.get('no_arm'):
            armed = self._arm_due_events()
            self.stdout.write(
                self.style.SUCCESS(f"Survey scheduler: armed {armed} event(s).")
            )

        dispatched = self._dispatch_due_invitations(sync)
        self.stdout.write(
            self.style.SUCCESS(f"Survey scheduler: dispatched {dispatched} due event(s).")
        )

    def _arm_due_events(self):
        """Create scheduled invitations for auto-send events past their anchor.

        Returns the number of events armed this run.
        """
        # Import view helpers lazily — they pull in the heavy views module, and we
        # only need them when arming.
        from tickets.views import (
            _clone_effective_to_event,
            _compute_survey_send_at,
            _survey_recipients,
        )

        from django.db.models import Exists, OuterRef

        now = timezone.now()

        # Narrow the scan as much as the schema allows: live, not-yet-handled
        # events whose start date is already in the past and that don't already
        # have an invitation (idempotency — once armed/sent, never touch again).
        # The per-event guards below handle the rest (resolved schedule from the
        # org fallback, anchor timing, recipients).
        candidates = (
            Event.objects
            .filter(
                deleted_at__isnull=True,
                survey_auto_send_opted_out=False,
                start_date__lte=now.date(),
            )
            .annotate(_has_invite=Exists(
                SurveyInvitation.objects.filter(event=OuterRef('pk'))
            ))
            .filter(_has_invite=False)
            .select_related('organization')
        )

        armed = 0
        for event in candidates.iterator():
            schedule = event.resolved_survey_schedule()
            if not schedule:
                continue  # No auto-send configured for this event or its org.

            # Wait until the anchor (event start/end) has passed so the recipient
            # list is settled before we freeze invitations.
            anchor = schedule.get('anchor', 'end')
            anchor_dt = (event.start_datetime() if anchor == 'start'
                         else event.end_datetime())
            if now < anchor_dt:
                continue

            try:
                scheduled_send_at = _compute_survey_send_at(
                    event, schedule['offset_type'], schedule['offset_value'],
                    schedule['time_of_day'], anchor,
                )
            except ValueError:
                logger.warning(
                    "Skipping auto-arm for event %s: invalid survey schedule.",
                    event.id,
                )
                continue

            org = event.organization
            # Materialize now: after bulk_create the recipients queryset (which
            # excludes customers that already have an invitation) re-evaluates to
            # empty, so capture the list and count before creating.
            recipients = list(_survey_recipients(event, org))
            if not recipients:
                continue

            with transaction.atomic():
                _clone_effective_to_event(event)
                SurveyInvitation.objects.bulk_create([
                    SurveyInvitation(
                        event=event,
                        customer=customer,
                        organization=org,
                        email=customer.email,
                        scheduled_send_at=scheduled_send_at,
                    )
                    for customer in recipients
                ])

            # bulk_create bypasses post_save signals — invalidate the cached stats
            # so the surveys tab reflects the freshly scheduled send.
            self._invalidate_event_caches(event.id)
            armed += 1
            logger.info(
                "Auto-armed survey for event %s (%d recipient(s), sending %s).",
                event.id, len(recipients), scheduled_send_at.isoformat(),
            )

        return armed

    def _dispatch_due_invitations(self, sync):
        """Mail every scheduled invitation whose send time has passed.

        Returns the number of (event, org) pairs dispatched.
        """
        now = timezone.now()

        def dispatch(event_id, organization_id):
            args = [str(event_id), str(organization_id)]
            if sync:
                send_survey_emails_task.apply(args=args)
            else:
                send_survey_emails_task.delay(*args)

        # One dispatch per (event, org) with at least one due, unsent invitation.
        # Rows stamped send_failed_at are permanently unsendable — without this
        # filter an event whose remaining invitations all failed would be
        # re-dispatched (as a no-op) on every cron run forever.
        due_pairs = SurveyInvitation.objects.filter(
            sent_at__isnull=True,
            send_failed_at__isnull=True,
            scheduled_send_at__isnull=False,
            scheduled_send_at__lte=now,
        ).values_list('event_id', 'organization_id').distinct()

        dispatched = 0
        for event_id, organization_id in list(due_pairs):
            dispatch(event_id, organization_id)
            dispatched += 1
        return dispatched

    @staticmethod
    def _invalidate_event_caches(event_id):
        from tickets.views import (
            _event_stats_cache_key,
            _invalidate_event_upload_stats_cache,
        )
        from django.core.cache import cache as django_cache

        django_cache.delete(_event_stats_cache_key(event_id))
        _invalidate_event_upload_stats_cache(event_id)
