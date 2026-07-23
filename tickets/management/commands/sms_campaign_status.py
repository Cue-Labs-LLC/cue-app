from django.core.management.base import BaseCommand

from tickets.models import SMSCampaign


class Command(BaseCommand):
    """Report scheduled marketing-SMS campaigns that are due or stuck — read only.

    This command physically cannot send: it imports no dispatch path and only
    reads ``SMSCampaign.objects.due()`` / ``.stuck()`` — the exact same helpers
    the ``send_due_sms_campaigns`` scheduler acts on, so the counts here match
    what a real run would dispatch. Safe to run anytime for operational checks.
    """
    help = "Show due and stuck marketing SMS campaigns without dispatching anything."

    def add_arguments(self, parser):
        parser.add_argument(
            '--list',
            action='store_true',
            help='List each campaign id and its scheduled/started time.',
        )

    def handle(self, *args, **options):
        show_list = options.get('list')

        due = SMSCampaign.objects.due()
        stuck = SMSCampaign.objects.stuck()

        due_count = due.count()
        stuck_count = stuck.count()

        self.stdout.write(
            self.style.WARNING(
                f"SMS scheduler status: {due_count} due, {stuck_count} stuck. "
                f"Nothing dispatched (read-only)."
            )
        )

        if show_list:
            for cid, org_id, name, scheduled_at in due.values_list(
                'id', 'organization_id', 'name', 'scheduled_at'
            ):
                self.stdout.write(f"  due    {cid}  org={org_id}  {scheduled_at:%Y-%m-%d %H:%M}  {name}")
            for cid, org_id, name, started_at in stuck.values_list(
                'id', 'organization_id', 'name', 'started_at'
            ):
                self.stdout.write(f"  stuck  {cid}  org={org_id}  {started_at:%Y-%m-%d %H:%M}  {name}")
