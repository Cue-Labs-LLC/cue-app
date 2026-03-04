"""Management command to backfill venue address normalization."""

import time

from django.conf import settings
from django.core.management.base import BaseCommand

from tickets.address_utils import geocode_venue_address, normalize_venue_address_fields
from tickets.models import Venue

_ADDRESS_FIELDS = ('street_address', 'city', 'state', 'postal_code', 'country')


class Command(BaseCommand):
    help = 'Normalize existing venue addresses (Python normalization + optional geocoding).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            default=False,
            help='Write changes to the database (default: dry-run).',
        )
        parser.add_argument(
            '--org-slug',
            dest='org_slug',
            default=None,
            help='Limit to venues belonging to a specific organization slug.',
        )
        parser.add_argument(
            '--skip-geocode',
            action='store_true',
            default=False,
            help='Skip Google Geocoding API calls; apply Python normalization only.',
        )

    def handle(self, *args, **options):
        apply = options['apply']
        org_slug = options['org_slug']
        skip_geocode = options['skip_geocode']

        dry_run = not apply
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY-RUN mode — no changes will be saved.'))

        api_key = getattr(settings, 'GOOGLE_MAPS_API_KEY', '') if not skip_geocode else ''

        qs = Venue.objects.all().order_by('id')
        if org_slug:
            qs = qs.filter(organization__slug=org_slug)

        total = qs.count()
        self.stdout.write(f'Processing {total} venue(s)...\n')

        n_geocoded = 0
        n_python_only = 0
        n_unchanged = 0
        n_errors = 0

        for venue in qs:
            original = {f: getattr(venue, f) for f in _ADDRESS_FIELDS}

            try:
                normalize_venue_address_fields(venue)

                used_geocode = False
                if api_key and venue.street_address:
                    geocode_venue_address(venue, api_key)
                    used_geocode = True
                    time.sleep(0.2)

            except Exception as exc:
                self.stderr.write(
                    self.style.ERROR(f'  ERROR on venue {venue.pk} "{venue.name}": {exc}')
                )
                n_errors += 1
                continue

            after = {f: getattr(venue, f) for f in _ADDRESS_FIELDS}
            changed_fields = [f for f in _ADDRESS_FIELDS if original[f] != after[f]]

            if not changed_fields:
                n_unchanged += 1
                continue

            self.stdout.write(f'  Venue: {venue.pk} — "{venue.name}" ({venue.city})')
            for f in changed_fields:
                self.stdout.write(f'    {f}: {original[f]!r} → {after[f]!r}')

            if apply:
                Venue.objects.filter(pk=venue.pk).update(
                    **{f: getattr(venue, f) for f in _ADDRESS_FIELDS}
                )

            if used_geocode:
                n_geocoded += 1
            else:
                n_python_only += 1

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done. geocoded={n_geocoded}, python-only={n_python_only}, '
            f'unchanged={n_unchanged}, errors={n_errors}'
        ))
        if dry_run and (n_geocoded + n_python_only) > 0:
            self.stdout.write(self.style.WARNING(
                'Re-run with --apply to write changes.'
            ))
