"""
Management command to regenerate the Upcoming Events Google Doc.

Usage:
    python manage.py generate_event_doc
    python manage.py generate_event_doc --org-slug familiar-faces
    python manage.py generate_event_doc --dry-run
    python manage.py generate_event_doc --doc-id 1AbCdE...
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tickets.models import Organization
from tickets.services.google_docs import EventDocFormatter, GoogleDocWriter


class Command(BaseCommand):
    help = 'Regenerate the Upcoming Events Google Doc for an organization'

    def add_arguments(self, parser):
        parser.add_argument(
            '--org-slug',
            type=str,
            default='default',
            help='Organization slug (default: "default")',
        )
        parser.add_argument(
            '--doc-id',
            type=str,
            default='',
            help='Google Doc ID override (default: from GOOGLE_DOC_ID setting)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Generate content and print to stdout without writing to Google Docs',
        )

    def handle(self, *args, **options):
        org_slug = options['org_slug']
        doc_id = options['doc_id'] or settings.GOOGLE_DOC_ID
        dry_run = options['dry_run']

        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist:
            raise CommandError(f"Organization with slug '{org_slug}' not found.")

        self.stdout.write(f"Generating event doc for: {org.name}")

        formatter = EventDocFormatter(org)
        events = formatter.get_upcoming_events()
        content = formatter.generate_full_document()

        self.stdout.write(f"Found {len(events)} upcoming event(s)")

        if dry_run:
            self.stdout.write(self.style.WARNING("\n--- DRY RUN OUTPUT ---\n"))
            self.stdout.write(content)
            self.stdout.write(self.style.WARNING("\n--- END DRY RUN ---"))
            return

        if not doc_id:
            raise CommandError(
                "No Google Doc ID configured. "
                "Set GOOGLE_DOC_ID environment variable or use --doc-id."
            )

        writer = GoogleDocWriter(doc_id)
        result = writer.update_document(content)

        if result['success']:
            self.stdout.write(self.style.SUCCESS(
                f"Successfully updated Google Doc. "
                f"Wrote {result['characters_written']} characters."
            ))
        else:
            raise CommandError(f"Failed to update Google Doc: {result['error']}")
