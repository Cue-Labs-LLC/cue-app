"""Management command to remove all ticket orders and checkout sessions for an event."""

from django.core.management.base import BaseCommand, CommandError

from tickets.models import Event, Organization, StripeCheckoutSession, TicketOrder


class Command(BaseCommand):
    help = (
        'Delete all TicketOrders (and their Tickets) and StripeCheckoutSessions '
        'for a given event, resetting it to zero tickets sold.'
    )

    def add_arguments(self, parser):
        parser.add_argument('event_id', help='UUID of the event to clear.')
        parser.add_argument(
            '--org-slug',
            dest='org_slug',
            default=None,
            help='Optional org slug to confirm the event belongs to the expected org.',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            default=False,
            help='Write changes to the database (default: dry-run).',
        )

    def handle(self, *args, **options):
        event_id = options['event_id']
        org_slug = options['org_slug']
        apply = options['apply']

        try:
            qs = Event.objects.select_related('organization')
            if org_slug:
                qs = qs.filter(organization__slug=org_slug)
            event = qs.get(pk=event_id)
        except Event.DoesNotExist:
            raise CommandError(
                f'Event "{event_id}" not found'
                + (f' under org "{org_slug}"' if org_slug else '') + '.'
            )

        order_count = TicketOrder.objects.filter(event=event).count()
        session_count = StripeCheckoutSession.objects.filter(event=event).count()

        self.stdout.write(f'Event      : {event.name} ({event.pk})')
        self.stdout.write(f'Org        : {event.organization.name} ({event.organization.slug})')
        self.stdout.write(f'Orders     : {order_count} (Tickets cascade-deleted automatically)')
        self.stdout.write(f'Checkout sessions: {session_count}')

        if not apply:
            self.stdout.write(self.style.WARNING(
                '\nDRY-RUN — no changes saved. Re-run with --apply to delete.'
            ))
            return

        TicketOrder.objects.filter(event=event).delete()
        StripeCheckoutSession.objects.filter(event=event).delete()

        self.stdout.write(self.style.SUCCESS(
            f'\nDeleted {order_count} order(s) and {session_count} checkout session(s) '
            f'for event "{event.name}".'
        ))
