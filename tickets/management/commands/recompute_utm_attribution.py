"""Recompute Cue-tracked (UTM) attribution for linked Meta campaigns.

Backfills EventExpense.cue_attributed_orders/revenue from UTM params captured on
native ticket orders. Run after deploying UTM capture, or any time you want to
refresh first-party attribution outside the normal event-detail render.

Usage::

    python manage.py recompute_utm_attribution
    python manage.py recompute_utm_attribution --organization-id <uuid>
    python manage.py recompute_utm_attribution --organization-slug my-org
"""
from django.core.management.base import BaseCommand, CommandError

from tickets.models import Organization
from tickets.services.marketing.utm_attribution import UTMAttributionCalculator


class Command(BaseCommand):
    help = "Recompute Cue-tracked UTM attribution for linked Meta campaigns."

    def add_arguments(self, parser):
        parser.add_argument('--organization-id', dest='organization_id')
        parser.add_argument('--organization-slug', dest='organization_slug')

    def handle(self, *args, **options):
        orgs = Organization.objects.all()
        if options.get('organization_id'):
            orgs = orgs.filter(id=options['organization_id'])
        elif options.get('organization_slug'):
            orgs = orgs.filter(slug=options['organization_slug'])

        orgs = list(orgs)
        if not orgs:
            raise CommandError("No matching organizations found.")

        for org in orgs:
            changed = UTMAttributionCalculator(org).recompute_all()
            status = 'updated' if changed else 'no changes'
            self.stdout.write(f"{org.name}: {status}")

        self.stdout.write(self.style.SUCCESS(f"Recomputed UTM attribution for {len(orgs)} org(s)."))
