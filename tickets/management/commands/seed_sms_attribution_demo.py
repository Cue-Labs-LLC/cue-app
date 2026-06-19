"""Seed a demo SlickText broadcast + UTM-tagged ticket order to exercise SMS attribution.

Creates one confirmed EventSMSCampaign and a non-refunded TicketOrder whose
`attribution` carries the SlickText UTMs (utm_source=slicktext, utm_id=<external_id>),
then runs SMSAttributionCalculator and prints the resulting cue-attributed numbers.
This is the no-shell-paste version of the manual end-to-end test.

Usage::

    python manage.py seed_sms_attribution_demo --organization-slug my-org
    python manage.py seed_sms_attribution_demo --organization-id <uuid> --event-id <uuid>
    python manage.py seed_sms_attribution_demo --organization-slug my-org --cleanup

After seeding, open the event detail page (the SlickText row shows the attributed
orders/revenue and an "Attribution: Cue-tracked (UTM)" label) and Marketing -> Overview
(the SMS channel card reflects the revenue). Re-run with --cleanup to remove the
seeded rows.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tickets.models import Customer, Event, EventSMSCampaign, Organization, TicketOrder
from tickets.services.marketing.sms_attribution import SMSAttributionCalculator

EXTERNAL_ID = 'demo-sms-attr'
ORDER_NUMBER = 'DEMO-SMS-ATTR-1'
CUSTOMER_EMAIL = 'sms-attr-demo@example.com'


class Command(BaseCommand):
    help = "Seed a demo SlickText broadcast + matching UTM order to test SMS attribution."

    def add_arguments(self, parser):
        parser.add_argument('--organization-id', dest='organization_id')
        parser.add_argument('--organization-slug', dest='organization_slug')
        parser.add_argument(
            '--event-id',
            dest='event_id',
            help='Attach to this event. Defaults to the most recent event in the org.',
        )
        parser.add_argument(
            '--amount',
            dest='amount',
            default='45.00',
            help='Order total to attribute (default 45.00).',
        )
        parser.add_argument(
            '--cleanup',
            action='store_true',
            help='Remove the demo broadcast, order, and customer instead of creating them.',
        )

    def handle(self, *args, **options):
        org = self._resolve_org(options)
        event = self._resolve_event(org, options.get('event_id'))

        if options.get('cleanup'):
            return self._cleanup(org, event)

        now = timezone.now()
        camp, _ = EventSMSCampaign.objects.update_or_create(
            event=event, source='slicktext', external_id=EXTERNAL_ID,
            deleted_at__isnull=True,
            defaults={
                'name': 'Demo SMS Broadcast',
                'message': f'Doors at 8 for {event.name}. Tap for tickets.',
                'send_time': now - timedelta(days=1),
                'audience_size': 800,
                'unique_clicks': 60,
                # SlickText's own orders/revenue stay empty — the point is that Cue
                # fills them from real ticket orders below.
                'orders': 0,
                'revenue': Decimal('0.00'),
                'confirmed_at': now,
            },
        )

        customer, _ = Customer.objects.get_or_create(
            organization=org, email=CUSTOMER_EMAIL,
            defaults={'name': 'SMS Attr Demo', 'phone': ''},
        )
        amount = Decimal(options.get('amount') or '45.00')
        TicketOrder.objects.update_or_create(
            event=event, order_number=ORDER_NUMBER,
            defaults={
                'customer': customer,
                'external_order_number': ORDER_NUMBER,
                'order_date': now,
                'total_amount': amount,
                'refunded_at': None,
                'attribution': {'utm_source': 'slicktext', 'utm_id': EXTERNAL_ID},
            },
        )

        SMSAttributionCalculator(org).recompute_event(event)
        camp.refresh_from_db()

        self.stdout.write(self.style.SUCCESS(
            f'Seeded on event "{event.name}" ({event.id}).\n'
            f'  Broadcast: {camp.name} (external_id={camp.external_id})\n'
            f'  cue_attributed_orders={camp.cue_attributed_orders} '
            f'cue_attributed_revenue={camp.cue_attributed_revenue}\n'
            f'  effective_orders={camp.effective_orders} '
            f'effective_revenue={camp.effective_revenue} '
            f'attribution_source={camp.attribution_source}'
        ))
        if camp.attribution_source != 'cue':
            self.stdout.write(self.style.WARNING(
                'Expected attribution_source=cue — check that the order saved with the '
                'utm_source=slicktext / utm_id attribution.'
            ))
        self.stdout.write(
            f'Open the event detail page for "{event.name}" and Marketing -> Overview to '
            f'see it in the UI. Re-run with --cleanup to remove the demo rows.'
        )

    def _cleanup(self, org, event):
        orders = TicketOrder.objects.filter(event=event, order_number=ORDER_NUMBER).delete()
        camps = EventSMSCampaign.objects.filter(
            event=event, source='slicktext', external_id=EXTERNAL_ID,
        ).delete()
        custs = Customer.objects.filter(organization=org, email=CUSTOMER_EMAIL).delete()
        self.stdout.write(self.style.SUCCESS(
            f'Removed demo rows: orders={orders[0]}, broadcasts={camps[0]}, customers={custs[0]}.'
        ))

    def _resolve_org(self, options):
        org_id = options.get('organization_id')
        org_slug = options.get('organization_slug')
        if not org_id and not org_slug:
            raise CommandError('Provide --organization-id or --organization-slug.')
        qs = Organization.objects.all()
        if org_id:
            qs = qs.filter(id=org_id)
        if org_slug:
            qs = qs.filter(slug=org_slug)
        org = qs.first()
        if not org:
            raise CommandError('Organization not found.')
        return org

    def _resolve_event(self, org, event_id):
        qs = Event.objects.filter(organization=org, deleted_at__isnull=True).order_by('-start_date')
        if event_id:
            event = qs.filter(id=event_id).first()
            if not event:
                raise CommandError(f'Event {event_id} not found in org {org.name}.')
            return event
        event = qs.first()
        if not event:
            raise CommandError(f'No events for org {org.name}. Create one first or pass --event-id.')
        return event
