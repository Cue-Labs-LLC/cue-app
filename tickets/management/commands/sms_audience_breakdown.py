from django.core.management.base import BaseCommand, CommandError

from tickets.models import Customer, Organization, PhoneSuppression
from tickets.sms import is_plausible_e164, normalize_phone, sms_country_allowed


class Command(BaseCommand):
    """Explain why an org's marketing-SMS audience is smaller than its subscriber count.

    Read only. Replays exactly the drop logic in ``SMSCampaign.candidate_customers`` +
    ``materialize`` against the "everyone subscribed" universe (``sms_opt_in=True``),
    attributing each subscriber who won't receive a text to its first failing reason:
    blank phone, global suppression (STOP/bounce), invalid E.164 format, non-allowed
    country, or duplicate phone. The remainder is the deliverable unique-recipient count
    a broadcast to all subscribers would actually reach.

    Every check here mirrors production (``PhoneSuppression.suppressed_phones``,
    ``normalize_phone``, ``is_plausible_e164``, ``sms_country_allowed``,
    ``SMS_ALLOWED_COUNTRY_PREFIXES``), so the bottom-line number matches a real send's
    audience for the same universe. It dispatches nothing and mutates nothing.
    """
    help = "Break down why subscribed customers don't all become SMS recipients (read only)."

    def add_arguments(self, parser):
        parser.add_argument(
            'org',
            help='Organization name (exact) or id (UUID).',
        )
        parser.add_argument(
            '--samples',
            type=int,
            default=0,
            help='Print up to N example phone numbers for each drop reason.',
        )

    def _resolve_org(self, ref):
        org = Organization.objects.filter(name=ref).first()
        if org:
            return org
        org = Organization.objects.filter(id=ref).first()
        if org:
            return org
        raise CommandError(f"No organization matches {ref!r} by name or id.")

    def handle(self, *args, **options):
        org = self._resolve_org(options['org'])
        n_samples = options['samples']
        suppressed = PhoneSuppression.suppressed_phones(org)

        subs = Customer.objects.filter(organization=org, sms_opt_in=True)
        total = subs.count()

        counts = {'blank': 0, 'suppressed': 0, 'invalid': 0, 'country': 0, 'dup': 0}
        samples = {k: [] for k in counts}
        seen = set()
        deliverable = 0

        for c in subs.only('id', 'phone').iterator():
            raw = (c.phone or '').strip()
            reason = None
            phone = normalize_phone(raw) if raw else ''
            if not phone:
                reason = 'blank'
            elif phone in suppressed:
                reason = 'suppressed'
            elif not is_plausible_e164(phone):
                reason = 'invalid'
            elif not sms_country_allowed(phone):
                reason = 'country'
            elif phone in seen:
                reason = 'dup'

            if reason is None:
                seen.add(phone)
                deliverable += 1
                continue
            counts[reason] += 1
            if len(samples[reason]) < n_samples:
                samples[reason].append(raw or '(empty)')

        dropped = total - deliverable
        pct = (100 * dropped / total) if total else 0.0

        rows = [
            ('Subscribed (sms_opt_in=True)', total),
            ('  - blank / no phone', counts['blank']),
            ('  - globally suppressed (STOP/bounce)', counts['suppressed']),
            ('  - invalid E.164 format', counts['invalid']),
            ('  - non-allowed country', counts['country']),
            ('  - duplicate phone', counts['dup']),
            ('= deliverable unique recipients', deliverable),
        ]
        self.stdout.write(f"Org: {org.name} ({org.id})")
        width = max(len(label) for label, _ in rows)
        for label, value in rows:
            self.stdout.write(f"{label.ljust(width)}  {value:>7}")
        self.stdout.write(f"Total dropped: {dropped} ({pct:.1f}%)")

        if n_samples:
            self.stdout.write("")
            for key, label in (
                ('blank', 'blank'), ('suppressed', 'suppressed'),
                ('invalid', 'invalid format'), ('country', 'non-allowed country'),
                ('dup', 'duplicate'),
            ):
                if samples[key]:
                    self.stdout.write(f"{label} examples: {', '.join(samples[key])}")
