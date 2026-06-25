from django.core.management.base import BaseCommand
from django.utils import timezone

from tickets.models import SurveyInvitation
from tickets.tasks import send_survey_emails_task


class Command(BaseCommand):
    """Dispatch due scheduled post-event survey invitations.

    Run on a schedule (Render cron, every 5 minutes). The DB is the source of
    truth for scheduling — no task sits in worker memory across deploys. The
    send task only mails rows whose scheduled time has passed and clears them by
    stamping sent_at, so a double cron run is harmless (already-sent rows no
    longer match).
    """
    help = "Dispatch scheduled post-event survey invitations that are due."

    def add_arguments(self, parser):
        parser.add_argument(
            '--sync',
            action='store_true',
            help='Run inline instead of enqueueing Celery tasks (debugging).',
        )

    def handle(self, *args, **options):
        now = timezone.now()
        sync = options.get('sync')

        def dispatch(event_id, organization_id):
            args = [str(event_id), str(organization_id)]
            if sync:
                send_survey_emails_task.apply(args=args)
            else:
                send_survey_emails_task.delay(*args)

        # One dispatch per (event, org) with at least one due, unsent invitation.
        due_pairs = SurveyInvitation.objects.filter(
            sent_at__isnull=True,
            scheduled_send_at__isnull=False,
            scheduled_send_at__lte=now,
        ).values_list('event_id', 'organization_id').distinct()

        dispatched = 0
        for event_id, organization_id in list(due_pairs):
            dispatch(event_id, organization_id)
            dispatched += 1

        self.stdout.write(
            self.style.SUCCESS(f"Survey scheduler: dispatched {dispatched} due event(s).")
        )
