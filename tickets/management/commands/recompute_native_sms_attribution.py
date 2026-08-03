"""Recompute post-send conversion attribution for native (Cue) SMS campaigns.

Backfills SMSCampaign.attributed_orders/revenue by crediting each campaign with the
recipients who bought its linked event within SMS_ATTRIBUTION_WINDOW_DAYS of their send
(last-touch across overlapping sends; refunds excluded). Run after deploying native SMS
attribution to fill in existing campaigns immediately, or any time you want to refresh
outside the normal CSV-import / checkout / event-detail recompute.

Usage::

    python manage.py recompute_native_sms_attribution
    python manage.py recompute_native_sms_attribution --organization-id <uuid>
    python manage.py recompute_native_sms_attribution --organization-slug my-org
"""
from django.core.management.base import BaseCommand, CommandError

from tickets.models import Organization
from tickets.services.marketing.sms_attribution import NativeSMSAttributionCalculator


class Command(BaseCommand):
    help = "Recompute post-send conversion attribution for native (Cue) SMS campaigns."

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
            changed = NativeSMSAttributionCalculator(org).recompute_all()
            status = 'updated' if changed else 'no changes'
            self.stdout.write(f"{org.name}: {status}")

        self.stdout.write(self.style.SUCCESS(
            f"Recomputed native SMS attribution for {len(orgs)} org(s)."
        ))
