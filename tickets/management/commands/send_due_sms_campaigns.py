from django.core.management.base import BaseCommand

from tickets.models import SMSCampaign
from tickets.tasks import send_sms_campaign_task


class Command(BaseCommand):
    """Dispatch due scheduled marketing-SMS campaigns and recover stuck ones.

    Run on a schedule (Render cron, every 5 minutes). The DB is the source of
    truth for scheduling — no task sits in worker memory across deploys. The
    orchestrator's atomic claim makes a double cron run harmless.

    This command DISPATCHES. To inspect what's pending without sending anything,
    use the read-only ``sms_campaign_status`` command instead. Both read the same
    ``SMSCampaign.objects.due()`` / ``.stuck()`` helpers so they never disagree.
    """
    help = "Dispatch scheduled marketing SMS campaigns that are due; recover stuck sends."

    def add_arguments(self, parser):
        parser.add_argument(
            '--sync',
            action='store_true',
            help='Run inline instead of enqueueing Celery tasks (debugging).',
        )

    def handle(self, *args, **options):
        sync = options.get('sync')

        def dispatch(campaign_id):
            if sync:
                send_sms_campaign_task.apply(args=[str(campaign_id)])
            else:
                send_sms_campaign_task.delay(str(campaign_id))

        due_count = 0
        for cid in SMSCampaign.objects.due().values_list('id', flat=True):
            dispatch(cid)
            due_count += 1

        stuck_count = 0
        for cid in SMSCampaign.objects.stuck().values_list('id', flat=True):
            dispatch(cid)
            stuck_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"SMS scheduler: dispatched {due_count} due, recovered {stuck_count} stuck."
            )
        )
