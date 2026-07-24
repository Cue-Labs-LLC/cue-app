"""One-time §6.3 launch broadcast: "Tap to Pay on iPhone is here".

Sends Apple's verbatim Value-Proposition push to every organizer who has at
least one stored device token. This is a manual, one-time broadcast — run it
once when Cue goes live with Tap to Pay (or when Apple grants the entitlement
and you're ready to launch).

Usage:
    python manage.py send_launch_push               # dry-run: report the recipient count
    python manage.py send_launch_push --confirm     # actually enqueue the broadcast
"""

from django.core.management.base import BaseCommand

from tickets.models import DeviceToken
from tickets.services.push_notifications.payloads import LAUNCH_ANNOUNCEMENT
from tickets.tasks import send_push_notification_task


class Command(BaseCommand):
    help = "Broadcast the one-time Apple §6.3 'Tap to Pay is here' launch push to all device tokens."

    def add_arguments(self, parser):
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Actually enqueue the broadcast. Without this flag the command is a dry run.',
        )

    def handle(self, *args, **options):
        token_ids = list(DeviceToken.objects.values_list('id', flat=True))
        total = len(token_ids)

        if not options['confirm']:
            self.stdout.write(self.style.WARNING(
                f"Dry run: would send the launch push to {total} device token(s). "
                f"Re-run with --confirm to enqueue."
            ))
            return

        for token_id in token_ids:
            send_push_notification_task.delay(str(token_id), LAUNCH_ANNOUNCEMENT)

        self.stdout.write(self.style.SUCCESS(
            f"Enqueued launch push to {total} device token(s)."
        ))
