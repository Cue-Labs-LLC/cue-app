"""
Repair TicketOrders whose customer belongs to a different org than the event.

This happened because Customer.objects.get_or_create keyed only on email (not
organization) in the Stripe checkout handlers, so a customer record from another
org could be returned and attached to a new order.

For each mismatched order:
  1. Find or create a Customer in the correct org with the same email/name.
  2. Re-point the order to that customer.
  3. Recalculate LTV for both the old (donor) customer and the new one.

Run with --apply to commit changes; default is a safe dry-run.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F

from tickets.models import Customer, Organization, TicketOrder


class Command(BaseCommand):
    help = 'Re-assign TicketOrders attached to cross-org customers to the correct org.'

    def add_arguments(self, parser):
        parser.add_argument(
            'org_slug',
            nargs='?',
            default=None,
            help='Limit to a single org slug (default: all orgs).',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            default=False,
            help='Write changes to the database (default: dry-run).',
        )

    def handle(self, *args, **options):
        apply = options['apply']
        org_slug = options['org_slug']

        qs = (
            TicketOrder.objects
            .exclude(customer__organization=F('event__organization'))
            .select_related('customer', 'event', 'event__organization')
            .order_by('event__organization__slug', 'customer__email')
        )
        if org_slug:
            qs = qs.filter(event__organization__slug=org_slug)

        orders = list(qs)

        if not orders:
            self.stdout.write(self.style.SUCCESS('No mismatched orders found.'))
            return

        self.stdout.write(f'Found {len(orders)} mismatched order(s):')
        self.stdout.write('')

        # Group for display
        for order in orders:
            correct_org = order.event.organization
            self.stdout.write(
                f'  Order {order.order_number}  |  '
                f'event org: {correct_org.slug}  |  '
                f'customer email: {order.customer.email}  |  '
                f'customer org: {order.customer.organization.slug}'
            )

        if not apply:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                'DRY-RUN — no changes saved. Re-run with --apply to fix.'
            ))
            return

        fixed = 0
        donors_to_update = set()  # customers that lost orders — need LTV recalc

        with transaction.atomic():
            for order in orders:
                correct_org = order.event.organization
                old_customer = order.customer

                new_customer, _ = Customer.objects.get_or_create(
                    email=old_customer.email,
                    organization=correct_org,
                    defaults={'name': old_customer.name},
                )

                order.customer = new_customer
                order.save(update_fields=['customer'])

                donors_to_update.add(old_customer.pk)
                new_customer.update_ltv()
                fixed += 1

            # Recalculate LTV for customers who lost orders
            for pk in donors_to_update:
                try:
                    Customer.objects.get(pk=pk).update_ltv()
                except Customer.DoesNotExist:
                    pass

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Fixed {fixed} order(s). LTV recalculated for affected customers.'
        ))
        self.stdout.write(
            'If RFM scores look stale, run the RFM recalculation from the dashboard.'
        )
