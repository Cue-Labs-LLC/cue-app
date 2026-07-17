"""Mark survey invitations whose recipient address is undeliverable.

Invalid emails (blank, or junk like the Apple "Hide My Email" placeholder text
that can slip in via CSV import) get denormalized onto SurveyInvitation.email and
then fail at send time with a 501 "Recipient syntax error". Because the send task
only stamps sent_at on success, those rows stay eligible and the worker retries the
same doomed send on every dispatch, spamming the error log indefinitely.

This command finds unsent, not-yet-failed invitations whose email won't validate
and stamps send_failed_at / send_error so they drop out of the eligibility query.
Idempotent and safe to re-run. Dry-run by default; use --apply to write.
"""

import logging

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.utils import timezone

from tickets.models import Organization, SurveyInvitation

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Mark unsent survey invitations whose email is invalid/undeliverable so the '
        'worker stops retrying them forever. Dry-run by default; use --apply to write.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--org',
            dest='org_slug',
            default=None,
            help='Limit to a specific organization slug (default: all orgs).',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            default=False,
            help='Write send_failed_at/send_error (default: dry-run).',
        )

    def handle(self, *args, **options):
        org = None
        if options['org_slug']:
            try:
                org = Organization.objects.get(slug=options['org_slug'])
            except Organization.DoesNotExist:
                raise CommandError(f'Organization "{options["org_slug"]}" not found.')

        apply = options['apply']
        if not apply:
            self.stdout.write(self.style.WARNING(
                'DRY-RUN — no changes will be saved. Re-run with --apply to write.\n'
            ))

        # Only rows that are still eligible to be sent: never sent, not already
        # marked failed. Those are the ones stuck in the retry loop.
        qs = SurveyInvitation.objects.filter(
            sent_at__isnull=True,
            send_failed_at__isnull=True,
        )
        if org is not None:
            qs = qs.filter(organization=org)

        marked = 0
        for invitation in qs.iterator():
            try:
                validate_email(invitation.email)
                continue  # Deliverable — leave it alone.
            except ValidationError:
                pass

            self.stdout.write(
                f'  {"MARK" if apply else "WOULD MARK"}  invitation {invitation.id} '
                f'(event={invitation.event_id}, email={invitation.email!r})'
            )
            if apply:
                invitation.send_failed_at = timezone.now()
                invitation.send_error = 'invalid_email'
                invitation.save(update_fields=['send_failed_at', 'send_error'])
            marked += 1

        self.stdout.write('')
        if apply:
            self.stdout.write(self.style.SUCCESS(
                f'Done. Marked {marked} unsendable invitation(s).'
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f'DRY-RUN complete. Would mark {marked} unsendable invitation(s).'
            ))
            if marked:
                self.stdout.write('Re-run with --apply to save changes.')
