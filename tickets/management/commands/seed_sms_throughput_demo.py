"""Seed data to exercise the marketing-SMS throughput guards: the day-aware daily carrier-cap
BLOCK at compose time, pacing, urgency-first dispatch, malformed-number validation, and the
rare send-time fail+refund. Seeds two scheduled campaigns — one booking tomorrow's carrier
budget — so composing a second same-day send trips the block.

Two modes:

* Default — a dedicated demo org (slug ``sms-throughput-demo``) with its own owner login and
  a funded wallet. Safe to nuke on ``--cleanup`` (deletes the whole org).

* ``--in-org <email-or-slug>`` — seeds ONLY clearly-labeled demo rows into an existing org so
  you can test with your real login. Every seeded row is prefixed/keyed as demo, campaigns
  target ONLY the demo customers (never your real audience), and NO wallet charge is made.
  ``--cleanup --in-org <…>`` removes only those demo rows — never the org, your user, or any
  real customer/event.

Usage::

    python manage.py seed_sms_throughput_demo
    python manage.py seed_sms_throughput_demo --in-org info@cueup.co
    python manage.py seed_sms_throughput_demo --in-org info@cueup.co --cleanup

Run the dispatcher with E2E_TEST_MODE so ``send_sms`` returns a fake SID instead of
calling Twilio (the demo numbers are not real).
"""
from datetime import time as _time, timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tickets.models import (
    Customer, Event, Organization, SMSCampaign, SMSMessageRecipient, UserProfile, Venue,
)
from tickets.services.sms_campaigns import finalize_campaign_send
from tickets.services.sms_credits import credit

DEFAULT_SLUG = 'sms-throughput-demo'
OWNER_USERNAME = 'sms-demo-owner'
OWNER_PASSWORD = 'smsdemo123'
# Demo rows carry these markers so scoped cleanup can find them without touching real data.
GOOD_EMAIL_TMPL = 'sms-demo-good-{i}@example.com'
BAD_EMAILS = ('sms-demo-bad-1@example.com', 'sms-demo-bad-2@example.com')
DEMO_PREFIX = '[SMS DEMO] '
VENUE_NAME = DEMO_PREFIX + 'Demo Hall'
TONIGHT_NAME = DEMO_PREFIX + 'Tonight Headliner'
FUTURE_NAME = DEMO_PREFIX + 'Future Fest'
CAMPAIGN_KEYS = ('seed-tonight', 'seed-evergreen', 'seed-deferred')


class Command(BaseCommand):
    help = "Seed demo campaigns to test SMS pacing, the daily cap, urgency, and at-risk."

    def add_arguments(self, parser):
        parser.add_argument('--slug', default=DEFAULT_SLUG, help='Dedicated demo org slug.')
        parser.add_argument(
            '--in-org', dest='in_org',
            help='Seed demo rows into an EXISTING org (by owner email or slug) without charging '
                 'its wallet. Cleanup only removes the demo rows.',
        )
        parser.add_argument('--recipients', type=int, default=15, help='Valid opted-in demo customers.')
        parser.add_argument(
            '--due', action='store_true',
            help='Make the blasts due NOW (and the deferred campaign "stuck") for an immediate '
                 'dispatch test. Default schedules them in the future so the cron never '
                 'auto-dispatches them. Run this right before send_due_sms_campaigns.',
        )
        parser.add_argument('--cleanup', action='store_true', help='Remove seeded data instead of creating it.')

    def handle(self, *args, **options):
        in_org = options.get('in_org')
        if options['cleanup']:
            return self._cleanup_in_org(in_org) if in_org else self._cleanup_org(options['slug'])

        n = max(3, options['recipients'])
        now = timezone.now()
        today = timezone.localdate()
        due = options['due']
        # Default: the blasts are SCHEDULED — "Tonight Blast" for tomorrow (so it books that
        # day's carrier budget, setting up the day-aware compose-time block) and "Evergreen"
        # for a far-off day. The cron won't dispatch them. With --due they're due now, so the
        # dispatcher runs them (and, under a low cap, the send-time fail+refund fires).
        tonight_at = now - timedelta(minutes=2) if due else now + timedelta(days=1)
        evergreen_at = now - timedelta(minutes=1) if due else now + timedelta(days=30)

        if in_org:
            org = self._resolve_existing_org(in_org)
            owner = self._org_owner(org)
            scoped = True
            if not org.sms_marketing_enabled:
                raise CommandError(
                    f'Org "{org.name}" does not have sms_marketing_enabled — enable it first '
                    f'(the SMS composer is gated on it).'
                )
        else:
            org, _ = Organization.objects.get_or_create(
                slug=options['slug'], defaults={'name': 'SMS Throughput Demo'},
            )
            if not org.sms_marketing_enabled:
                org.sms_marketing_enabled = True
                org.save(update_fields=['sms_marketing_enabled'])
            owner = self._ensure_demo_owner(org)
            credit(org.id, 50_000, description='Seed: SMS throughput demo top-up', created_by=owner)
            scoped = False

        venue = self._venue(org)
        tonight = self._event(org, venue, TONIGHT_NAME, today + timedelta(days=1))
        future = self._event(org, venue, FUTURE_NAME, today + timedelta(days=30))

        good_ids = self._good_customers(org, n)
        self._bad_customers(org)
        bad_ids = [str(c.id) for c in Customer.objects.filter(organization=org, email__in=BAD_EMAILS)]
        target_ids = good_ids + bad_ids

        tonight_camp = self._due_campaign(
            org, owner, tonight, 'Tonight Blast',
            'Doors 9PM tonight — grab tickets: https://cueup.co/demo', target_ids,
            send_at=tonight_at, key='seed-tonight', scoped=scoped,
        )
        evergreen_camp = self._due_campaign(
            org, owner, future, 'Evergreen Blast',
            'Future Fest tickets are live: https://cueup.co/demo', target_ids,
            send_at=evergreen_at, key='seed-evergreen', scoped=scoped,
        )

        org.refresh_from_db()
        self._report(org, owner, tonight_camp, evergreen_camp,
                     len(good_ids), len(bad_ids), scoped, in_org, due)

    # --- org / owner resolution ---------------------------------------------------

    def _resolve_existing_org(self, ident):
        user = User.objects.filter(email__iexact=ident).first() or \
            User.objects.filter(username__iexact=ident).first()
        if user:
            prof = UserProfile.objects.filter(user=user).select_related('organization').first()
            if prof and prof.organization:
                return prof.organization
        org = Organization.objects.filter(slug=ident).first()
        if org:
            return org
        raise CommandError(f'No org found for "{ident}" (tried user email/username and org slug).')

    def _org_owner(self, org):
        prof = (UserProfile.objects.filter(
            organization=org, org_role=UserProfile.OrgRole.OWNER,
        ).select_related('user').first()
            or UserProfile.objects.filter(organization=org).select_related('user').first())
        return prof.user if prof else None

    def _ensure_demo_owner(self, org):
        owner, created = User.objects.get_or_create(
            username=OWNER_USERNAME, defaults={'email': 'sms-demo-owner@example.com'},
        )
        if created:
            owner.set_password(OWNER_PASSWORD)
            owner.save()
        UserProfile.objects.get_or_create(
            user=owner, defaults={'organization': org, 'org_role': UserProfile.OrgRole.OWNER},
        )
        return owner

    # --- builders -----------------------------------------------------------------

    def _venue(self, org):
        venue, _ = Venue.objects.get_or_create(
            organization=org, name=VENUE_NAME, defaults={'city': 'Los Angeles'},
        )
        return venue

    def _event(self, org, venue, name, start_date):
        event, _ = Event.objects.get_or_create(
            organization=org, name=name, venue=venue,
            defaults={'start_date': start_date, 'start_time': _time(21, 0)},
        )
        if event.start_date != start_date:  # keep "tomorrow" relative on re-seed
            event.start_date = start_date
            event.save(update_fields=['start_date'])
        return event

    def _good_customers(self, org, n):
        ids = []
        for i in range(n):
            c, _ = Customer.objects.get_or_create(
                organization=org, email=GOOD_EMAIL_TMPL.format(i=i),
                defaults={'name': f'[DEMO] Fan {i}', 'phone': f'+1555{i:07d}', 'sms_opt_in': True},
            )
            ids.append(str(c.id))
        return ids

    def _bad_customers(self, org):
        for email, phone in zip(BAD_EMAILS, ('116267769618', '15551234')):
            Customer.objects.get_or_create(
                organization=org, email=email,
                defaults={'name': '[DEMO] Malformed Number', 'phone': phone, 'sms_opt_in': True},
            )

    def _due_campaign(self, org, owner, event, name, body, target_ids, *, send_at, key, scoped):
        if scoped:
            # In an existing org: freeze the audience directly (no wallet charge, targets only
            # the demo customers). materialize() still applies validation + dedupe + suppression.
            camp, created = SMSCampaign.objects.get_or_create(
                organization=org, idempotency_key=key,
                defaults={
                    'name': name, 'body': body, 'event': event, 'created_by': owner,
                    'manual_include_ids': target_ids,
                    'status': SMSCampaign.Status.SCHEDULED, 'scheduled_at': send_at,
                },
            )
            if created:
                recipients = camp.materialize(org, cap=5000)
                SMSMessageRecipient.objects.bulk_create([
                    SMSMessageRecipient(
                        campaign=camp, customer_id=r['customer_id'], phone=r['phone'],
                        segments=1, stop_disclosed=True,
                    ) for r in recipients
                ])
                camp.audience_size = len(recipients)
                camp.save(update_fields=['audience_size'])
            elif camp.scheduled_at != send_at:
                # Re-run: retime so --due / default can be toggled without a cleanup.
                camp.scheduled_at = send_at
                camp.status = SMSCampaign.Status.SCHEDULED
                camp.save(update_fields=['scheduled_at', 'status'])
            return camp
        # Dedicated demo org: use the real charged path.
        return finalize_campaign_send(
            org, name=name, body=body, criteria={}, manual_include_ids=target_ids,
            event=event, scheduled=True, send_at=send_at, user=owner,
            idempotency_key=key, cap=5000,
        ).campaign

    # --- output -------------------------------------------------------------------

    def _report(self, org, owner, tonight, evergreen, good, bad, scoped, in_org, due):
        s, w = self.style, self.stdout.write
        state = 'DUE now' if due else 'SCHEDULED (future — cron will NOT auto-send)'
        w(s.SUCCESS(f'\nSeeded SMS throughput demo into "{org.name}" (slug={org.slug}).'))
        if scoped:
            login = getattr(owner, 'email', None) or getattr(owner, 'username', '<owner>')
            w(f'  Mode:   scoped into your existing org — no wallet charge, demo rows only.')
            w(f'  Login:  your own account ({login}); wallet untouched at {org.sms_credit_balance_cents}¢')
        else:
            w(f'  Login:  {OWNER_USERNAME} / {OWNER_PASSWORD}   Wallet: {org.sms_credit_balance_cents}¢')
        w(f'  Customers: {good} valid + {bad} malformed demo customers (real audience untouched)')
        w('')
        w('  Campaigns:')
        w(f'    {state}  "{tonight.name}"   audience={tonight.audience_size}  scheduled for TOMORROW')
        w(f'    {state}  "{evergreen.name}" audience={evergreen.audience_size}  scheduled +30 days')
        w('')
        args = f'--in-org {in_org} ' if scoped else ''
        cap_hint = max(1, tonight.audience_size + 2)  # a cap that leaves room for < a 2nd full send
        w(s.WARNING('  How to test the compose-time block:'))
        w('   • Validation:  the audiences show 15, not 17 — the 2 malformed numbers were dropped.')
        w(f'   • Day-aware block: "{tonight.name}" already books {tonight.audience_size} segments for')
        w('     TOMORROW. With a low cap, composing a SECOND send to the demo audience for tomorrow')
        w('     is blocked with a "reduce the recipient list" prompt. In the running dev server:')
        w(f'        set  SMS_DAILY_SEGMENT_CAP={cap_hint}  (e.g. in .env), restart, then in the SMS')
        w('        composer target the demo customers, schedule for tomorrow, and confirm → blocked.')
        if due:
            w('   • Send-time fail (rare race): blasts are DUE now. With a low cap, the dispatcher')
            w('     fails the over-budget campaign and refunds it (no partial send, no defer):')
            w('        SMS_DAILY_SEGMENT_CAP=5 E2E_TEST_MODE=True \\')
            w('          python manage.py send_due_sms_campaigns --sync')
            w('       The failed campaign shows its reason on the detail page.')
            w('')
            w(s.WARNING('   Note: blasts are DUE now — a running cron/worker could dispatch them.'
                        ' Re-run without --due to park them safely again.'))
        else:
            w('   • Blasts are parked in the future so nothing auto-sends. To also demo the rare')
            w('     send-time fail+refund, re-seed with --due, then run the dispatcher (low cap):')
            w(f'        python manage.py seed_sms_throughput_demo {args}--due')
            w('        SMS_DAILY_SEGMENT_CAP=5 E2E_TEST_MODE=True \\')
            w('          python manage.py send_due_sms_campaigns --sync')
        w('')
        w(f'  Remove when done:  python manage.py seed_sms_throughput_demo {args}--cleanup\n')

    # --- cleanup ------------------------------------------------------------------

    def _cleanup_in_org(self, ident):
        """Scoped cleanup: delete ONLY demo-marked rows. Never the org, user, or real data."""
        org = self._resolve_existing_org(ident)
        camps = SMSCampaign.objects.filter(organization=org, idempotency_key__in=CAMPAIGN_KEYS)
        n_camp = camps.count()
        camps.delete()  # recipients cascade
        from django.db.models import Q
        n_cust = Customer.objects.filter(organization=org).filter(
            Q(email__startswith='sms-demo-good-') | Q(email__in=BAD_EMAILS)
        ).delete()[0]
        Event.objects.filter(organization=org, name__startswith=DEMO_PREFIX).delete()
        Venue.objects.filter(organization=org, name=VENUE_NAME).delete()
        self.stdout.write(self.style.SUCCESS(
            f'Removed demo rows from "{org.name}": {n_camp} campaigns, {n_cust} customers, '
            f'+ demo events/venue. Org and real data untouched.'
        ))

    def _cleanup_org(self, slug):
        from tickets.models import SMSCreditTransaction
        org = Organization.objects.filter(slug=slug).first()
        if not org:
            self.stdout.write(self.style.WARNING(f'No org with slug "{slug}"; nothing to clean up.'))
            return
        org_id = org.id
        SMSCampaign.objects.filter(organization=org).delete()
        SMSCreditTransaction.objects.filter(organization=org).delete()
        Customer.objects.filter(organization=org).delete()
        Event.objects.filter(organization=org).delete()
        Venue.objects.filter(organization=org).delete()
        User.objects.filter(username=OWNER_USERNAME).delete()
        org.delete()
        self.stdout.write(self.style.SUCCESS(f'Removed demo org {org_id} (slug={slug}) and its data.'))
