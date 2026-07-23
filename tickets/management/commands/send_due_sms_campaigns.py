from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from tickets.models import SMSCampaign
from tickets.tasks import send_sms_campaign_task


class Command(BaseCommand):
    """Dispatch due scheduled marketing-SMS campaigns and recover stuck ones.

    Run on a schedule (Render cron, every 5 minutes). The DB is the source of
    truth for scheduling — no task sits in worker memory across deploys. The
    orchestrator's atomic claim makes a double cron run harmless.
    """
    help = "Dispatch scheduled marketing SMS campaigns that are due; recover stuck sends."

    # A campaign stuck in 'sending' longer than this (worker died mid-send) is
    # re-dispatched; the orchestrator resends only still-queued recipients. Set
    # comfortably above the worst-case wall-clock of a healthy max-size send
    # (SMS_CAMPAIGN_MAX_RECIPIENTS under Twilio throttling) so recovery never
    # fires alongside a slow-but-live send and double-dispatches it.
    STUCK_MINUTES = 30

    def add_arguments(self, parser):
        parser.add_argument(
            '--sync',
            action='store_true',
            help='Run inline instead of enqueueing Celery tasks (debugging).',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report due and stuck campaigns without dispatching anything.',
        )

    def handle(self, *args, **options):
        now = timezone.now()
        sync = options.get('sync')
        dry_run = options.get('dry_run')

        def dispatch(campaign_id):
            if dry_run:
                return
            if sync:
                send_sms_campaign_task.apply(args=[str(campaign_id)])
            else:
                send_sms_campaign_task.delay(str(campaign_id))

        due = SMSCampaign.objects.filter(
            status=SMSCampaign.Status.SCHEDULED,
            scheduled_at__lte=now,
        ).values_list('id', flat=True)
        due_count = 0
        for cid in list(due):
            dispatch(cid)
            due_count += 1

        stuck = SMSCampaign.objects.filter(
            status=SMSCampaign.Status.SENDING,
            started_at__lte=now - timedelta(minutes=self.STUCK_MINUTES),
        ).values_list('id', flat=True)
        stuck_count = 0
        for cid in list(stuck):
            dispatch(cid)
            stuck_count += 1

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"SMS scheduler (dry run): {due_count} due, {stuck_count} stuck. "
                    f"Nothing dispatched."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"SMS scheduler: dispatched {due_count} due, recovered {stuck_count} stuck."
                )
            )
