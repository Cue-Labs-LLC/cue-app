"""Seed unconfirmed marketing matches + AI recommendation for manual modal testing.

Creates one unconfirmed row per channel (Meta Ads / Mailchimp / SlickText) on a
past event, then runs the recommendation generator so a `marketing_unconfirmed`
recommendation appears on the Action Center.

Usage::

    python manage.py seed_unconfirmed_marketing_matches --organization-id <uuid>
    python manage.py seed_unconfirmed_marketing_matches --organization-id <uuid> --event-id <uuid>
    python manage.py seed_unconfirmed_marketing_matches --organization-slug my-org --count 3
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tickets.models import (
    Event,
    EventEmailCampaign,
    EventExpense,
    EventSMSCampaign,
    Organization,
)
from tickets.tasks import generate_org_ai_opportunities_task


class Command(BaseCommand):
    help = "Seed unconfirmed marketing matches for testing the Review-and-confirm modal."

    def add_arguments(self, parser):
        parser.add_argument('--organization-id', dest='organization_id')
        parser.add_argument('--organization-slug', dest='organization_slug')
        parser.add_argument(
            '--event-id',
            dest='event_id',
            help='Attach matches to this event. Defaults to the most recent past event in window.',
        )
        parser.add_argument(
            '--count',
            dest='count',
            type=int,
            default=1,
            help='Number of unconfirmed rows per channel (default 1).',
        )
        parser.add_argument(
            '--skip-generate',
            action='store_true',
            help='Skip running the recommendation generator after seeding.',
        )

    def handle(self, *args, **options):
        org = self._resolve_org(options)
        event = self._resolve_event(org, options.get('event_id'))
        count = max(1, int(options.get('count') or 1))

        now = timezone.now()
        suffix = now.strftime('%Y%m%d%H%M%S')

        meta_created = self._create_meta_ads(event, count, suffix)
        mailchimp_created = self._create_mailchimp(event, count, suffix, now)
        slicktext_created = self._create_slicktext(event, count, suffix, now)

        self.stdout.write(self.style.SUCCESS(
            f'Seeded on event "{event.name}" ({event.id}): '
            f'Meta Ads={meta_created}, Mailchimp={mailchimp_created}, SlickText={slicktext_created}'
        ))

        if options.get('skip_generate'):
            self.stdout.write('Skipping recommendation generation (--skip-generate set).')
            return

        generated = generate_org_ai_opportunities_task(str(org.id))
        self.stdout.write(self.style.SUCCESS(
            f'Generated/updated {generated} recommendations for {org.name}. '
            f'Visit /actions/ and look for "{event.name}".'
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
        today = timezone.localdate()
        window_start = today - timedelta(days=90)
        qs = Event.objects.filter(
            organization=org,
            deleted_at__isnull=True,
            start_date__gte=window_start,
        ).order_by('-start_date')
        if event_id:
            event = qs.filter(id=event_id).first()
            if not event:
                raise CommandError(
                    f'Event {event_id} not found in org {org.name} or outside the 90-day window. '
                    f'The detector only flags events from the last 90 days.'
                )
            return event
        event = qs.filter(start_date__lt=today).first() or qs.first()
        if not event:
            raise CommandError(
                f'No events in the last 90 days for org {org.name}. Create one first or pass --event-id.'
            )
        return event

    def _create_meta_ads(self, event, count, suffix):
        created = 0
        for i in range(count):
            external_id = f'seed-meta-{suffix}-{i}'
            if EventExpense.objects.filter(
                event=event, source='meta_ads', external_id=external_id, deleted_at__isnull=True,
            ).exists():
                continue
            EventExpense.objects.create(
                event=event,
                category='marketing',
                description=f'Seed Meta Ads #{i+1}',
                amount=Decimal('250.00') + Decimal(i * 50),
                source='meta_ads',
                external_id=external_id,
                external_metadata={
                    'campaign_name': f'Seed Promo {i+1} — {event.name[:40]}',
                    'objective': 'OUTCOME_TRAFFIC',
                },
                manual_attributed_orders=10 + i,
                manual_attributed_revenue=Decimal('480.00') + Decimal(i * 25),
                expense_date=event.start_date,
            )
            created += 1
        return created

    def _create_mailchimp(self, event, count, suffix, now):
        created = 0
        for i in range(count):
            external_id = f'seed-mc-{suffix}-{i}'
            if EventEmailCampaign.objects.filter(
                event=event, source='mailchimp', external_id=external_id, deleted_at__isnull=True,
            ).exists():
                continue
            EventEmailCampaign.objects.create(
                event=event,
                source='mailchimp',
                external_id=external_id,
                campaign_title=f'Seed Newsletter #{i+1}',
                subject_line=f'Don\'t miss "{event.name}"',
                send_time=now - timedelta(days=7 - i),
                emails_sent=2000 + i * 250,
                opens=600 + i * 50,
                unique_opens=580 + i * 50,
                clicks=120 + i * 10,
                unique_clicks=115 + i * 10,
                ecommerce_orders=8 + i,
                ecommerce_revenue=Decimal('540.00') + Decimal(i * 60),
            )
            created += 1
        return created

    def _create_slicktext(self, event, count, suffix, now):
        created = 0
        for i in range(count):
            external_id = f'seed-st-{suffix}-{i}'
            if EventSMSCampaign.objects.filter(
                event=event, source='slicktext', external_id=external_id, deleted_at__isnull=True,
            ).exists():
                continue
            EventSMSCampaign.objects.create(
                event=event,
                source='slicktext',
                external_id=external_id,
                name=f'Seed SMS Blast #{i+1}',
                message=f'Doors at 8 for {event.name}. Tap for tickets.',
                send_time=now - timedelta(days=2 - i if i < 2 else 0),
                audience_size=900 + i * 100,
                clicks=80 + i * 10,
                unique_clicks=78 + i * 10,
                orders=5 + i,
                revenue=Decimal('325.00') + Decimal(i * 40),
            )
            created += 1
        return created
