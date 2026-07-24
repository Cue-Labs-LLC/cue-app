from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from tickets.models import SMSCampaign, SMSMessageRecipient


class Command(BaseCommand):
    """Report scheduled marketing-SMS campaigns that are due, upcoming, or stuck — read only.

    This command physically cannot send: it imports no dispatch path and only
    reads ``SMSCampaign.objects.due()`` / ``.upcoming()`` / ``.stuck()`` — the same
    helpers the ``send_due_sms_campaigns`` scheduler acts on, so the counts here
    match what a real run would dispatch. ``upcoming`` (scheduled but not yet due)
    is the common state for a freshly confirmed campaign whose audience is already
    frozen as QUEUED recipients; it is shown so that state isn't mistaken for a
    stuck or missing send. Safe to run anytime for operational checks.
    """
    help = "Show due, upcoming, and stuck marketing SMS campaigns without dispatching anything."

    def add_arguments(self, parser):
        parser.add_argument(
            '--list',
            action='store_true',
            help='List each campaign id, org, time, name, and frozen recipient count.',
        )

    def handle(self, *args, **options):
        show_list = options.get('list')

        due = SMSCampaign.objects.due()
        upcoming = SMSCampaign.objects.upcoming()
        stuck = SMSCampaign.objects.stuck()

        due_count = due.count()
        upcoming_count = upcoming.count()
        stuck_count = stuck.count()

        self.stdout.write(
            self.style.WARNING(
                f"SMS scheduler status: {due_count} due, {upcoming_count} upcoming, "
                f"{stuck_count} stuck. Nothing dispatched (read-only)."
            )
        )

        if show_list:
            for cid, org_id, name, scheduled_at, queued in due.annotate(
                queued=Count('recipients', filter=Q(recipients__status=SMSMessageRecipient.Status.QUEUED))
            ).values_list('id', 'organization_id', 'name', 'scheduled_at', 'queued'):
                self.stdout.write(
                    f"  due       {cid}  org={org_id}  {scheduled_at:%Y-%m-%d %H:%M}  "
                    f"{queued} queued  {name}"
                )
            for cid, org_id, name, scheduled_at, queued in upcoming.annotate(
                queued=Count('recipients', filter=Q(recipients__status=SMSMessageRecipient.Status.QUEUED))
            ).values_list('id', 'organization_id', 'name', 'scheduled_at', 'queued'):
                self.stdout.write(
                    f"  upcoming  {cid}  org={org_id}  {scheduled_at:%Y-%m-%d %H:%M}  "
                    f"{queued} queued  {name}"
                )
            for cid, org_id, name, started_at, queued in stuck.annotate(
                queued=Count('recipients', filter=Q(recipients__status=SMSMessageRecipient.Status.QUEUED))
            ).values_list('id', 'organization_id', 'name', 'started_at', 'queued'):
                self.stdout.write(
                    f"  stuck     {cid}  org={org_id}  {started_at:%Y-%m-%d %H:%M}  "
                    f"{queued} queued  {name}"
                )
