"""Auto-seed `field_map` on TypeformFormSubscriptions that never had one saved,
and re-project the mapping over existing responses so structured columns
(nps_score, overall_rating, text_feedback, …) populate retroactively.

Idempotent: subscriptions where `field_map` is already saved (including the
explicit empty dict that means "ignore everything") are left untouched.

Usage:
    python manage.py backfill_typeform_field_maps
    python manage.py backfill_typeform_field_maps --dry-run
    python manage.py backfill_typeform_field_maps --org familiar-faces
"""

from django.core.cache import cache as django_cache
from django.core.management.base import BaseCommand

from tickets.models import Organization, TypeformFormSubscription


class Command(BaseCommand):
    help = (
        'Backfill TypeformFormSubscription.field_map (and re-project existing '
        'responses) for any active subscription that was never configured. '
        'Safe to re-run.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Print what would change without writing.',
        )
        parser.add_argument(
            '--org',
            dest='org_slug',
            default=None,
            help='Limit to a specific organization slug.',
        )

    def handle(self, *args, **options):
        from tickets.services.typeform.client import TypeformAPIError, TypeformClient
        from tickets.services.typeform.field_mapping import auto_field_map
        from tickets.services.typeform.ingest import apply_field_map_to_subscription
        from tickets.views import (
            _event_stats_cache_key,
            _invalidate_event_upload_stats_cache,
        )

        dry_run = options['dry_run']
        org_slug = options['org_slug']

        subs = TypeformFormSubscription.objects.filter(
            is_active=True, field_map__isnull=True,
        ).select_related('organization')
        if org_slug:
            try:
                org = Organization.objects.get(slug=org_slug)
            except Organization.DoesNotExist:
                self.stderr.write(self.style.ERROR(f'No organization with slug {org_slug!r}.'))
                return
            subs = subs.filter(organization=org)

        subs = list(subs)
        if not subs:
            self.stdout.write('No subscriptions need backfilling.')
            return

        prefix = '[dry-run] ' if dry_run else ''
        self.stdout.write(f'{prefix}Found {len(subs)} subscription(s) with field_map=None.')

        total_backfilled = 0
        for sub in subs:
            org = sub.organization
            label = f'{org.slug}/{sub.form_id} "{sub.form_title}"'

            if not org.typeform_access_token:
                self.stdout.write(self.style.WARNING(
                    f'  {label}: skipped — org has no Typeform credentials.'
                ))
                continue

            client = TypeformClient(access_token=org.typeform_access_token)
            try:
                definition = client.get_form(sub.form_id)
            except TypeformAPIError as exc:
                self.stdout.write(self.style.WARNING(
                    f'  {label}: skipped — could not fetch form definition: {exc}'
                ))
                continue

            suggested = auto_field_map(definition or {})
            if not suggested:
                self.stdout.write(
                    f'  {label}: no auto-mapping suggested by heuristics, skipping.'
                )
                continue

            self.stdout.write(
                f'  {label}: {prefix}saving field_map with {len(suggested)} field(s) '
                f'({", ".join(sorted(set(suggested.values())))}).'
            )
            if dry_run:
                continue

            sub.field_map = suggested
            sub.save(update_fields=['field_map'])

            backfilled, affected_event_ids = apply_field_map_to_subscription(
                sub, suggested, limit=500,
            )
            for eid in affected_event_ids:
                django_cache.delete(_event_stats_cache_key(eid))
                _invalidate_event_upload_stats_cache(eid)

            total_backfilled += backfilled
            self.stdout.write(
                f'    re-projected {backfilled} existing response(s); '
                f'invalidated stats for {len(affected_event_ids)} event(s).'
            )

        verb = 'Would re-project' if dry_run else 'Re-projected'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {total_backfilled} response(s) across {len(subs)} subscription(s).'
        ))
