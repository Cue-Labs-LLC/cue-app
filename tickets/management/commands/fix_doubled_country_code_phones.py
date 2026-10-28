"""One-time fix for customer phone numbers with a doubled US country code.

Some Customer.phone values were stored with an extra leading "1" after the
country code — e.g. "+118433188176" instead of "+18433188176". These fail SMS
delivery because is_plausible_e164() rejects any +1 number that isn't exactly
11 digits total.

The fix is provably safe: a valid US/Canada (NANP) E.164 number is "+1" + 10
digits, and NANP area codes never start with 0 or 1 (and there is no country
code "11"). So any "+11..." value is malformed, and stripping the extra "1"
yields a valid number. This command rewrites only values matching exactly
"+11" + 10 digits whose result passes is_plausible_e164; anything else is
reported, not changed.

Dry-run by default; use --apply to write changes.

    python manage.py fix_doubled_country_code_phones --org-slug my-org
    python manage.py fix_doubled_country_code_phones --org-slug my-org --apply
"""

import logging
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tickets.models import Customer, Organization
from tickets.sms import is_plausible_e164

logger = logging.getLogger(__name__)

# Exactly "+11" followed by 10 digits (the doubled-country-code shape). The
# captured group is the real 10-digit national number.
_DOUBLED_CC_RE = re.compile(r'^\+11(\d{10})$')


class Command(BaseCommand):
    help = (
        "Fix customer phones stored with a doubled US country code (e.g. "
        "'+118433188176' -> '+18433188176'). Dry-run by default; use --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--org-slug',
            dest='org_slug',
            required=True,
            help='Slug of the organization whose customers should be fixed.',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            default=False,
            help='Write changes to the database (default: dry-run).',
        )

    def handle(self, *args, **options):
        org_slug = options['org_slug']
        apply = options['apply']
        dry_run = not apply

        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist:
            raise CommandError(f'Organization "{org_slug}" not found.')

        if dry_run:
            self.stdout.write(self.style.WARNING(
                'DRY-RUN mode - no changes will be saved.'))

        qs = (Customer.objects
              .filter(organization=org, phone__startswith='+11')
              .order_by('id'))

        planned = []       # (customer, old_phone, new_phone)
        conflicts = []     # (customer, old_phone, new_phone, conflict_pk)
        not_matched = []   # (customer, old_phone)

        for customer in qs:
            old_phone = customer.phone
            m = _DOUBLED_CC_RE.match(old_phone)
            if not m:
                not_matched.append((customer, old_phone))
                continue

            new_phone = '+1' + m.group(1)
            if not is_plausible_e164(new_phone):
                # Defensive — the regex already guarantees a valid shape.
                not_matched.append((customer, old_phone))
                continue

            conflict_pk = (Customer.objects
                           .filter(organization=org, phone=new_phone)
                           .exclude(pk=customer.pk)
                           .values_list('pk', flat=True)
                           .first())
            if conflict_pk is not None:
                conflicts.append((customer, old_phone, new_phone, conflict_pk))
                continue

            planned.append((customer, old_phone, new_phone))

        for customer, old_phone, new_phone in planned:
            self.stdout.write(f'  {customer.pk}: {old_phone!r} -> {new_phone!r}')

        if conflicts:
            self.stdout.write(self.style.WARNING(
                '\nConflicts (target phone already used by another customer in '
                'this org — left unchanged for manual merge):'))
            for customer, old_phone, new_phone, conflict_pk in conflicts:
                self.stdout.write(
                    f'  {customer.pk}: {old_phone!r} -> {new_phone!r} '
                    f'conflicts with customer {conflict_pk}')

        if not_matched:
            self.stdout.write(self.style.WARNING(
                '\nCandidates starting with "+11" that were NOT auto-fixable '
                '(wrong length/shape — review manually):'))
            for customer, old_phone in not_matched:
                self.stdout.write(f'  {customer.pk}: {old_phone!r}')

        if apply and planned:
            with transaction.atomic():
                for customer, _old_phone, new_phone in planned:
                    Customer.objects.filter(pk=customer.pk).update(phone=new_phone)

        verb = 'Fixed' if apply else 'Would fix'
        self.stdout.write(self.style.SUCCESS(
            f'\n{verb}: {len(planned)} | conflicts: {len(conflicts)} | '
            f'not auto-fixable: {len(not_matched)}'))

        if dry_run and planned:
            self.stdout.write(self.style.WARNING(
                'Re-run with --apply to write these changes.'))
