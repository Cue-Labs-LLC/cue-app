"""Seed data to exercise the charge-at-send SMS billing behavior.

Charging moved from schedule time to SEND time: confirming/scheduling a message no longer
debits the wallet — each message is charged when it actually dispatches, for the segments that
actually go out, and a message the wallet can't cover at send fails instead of sending.

Creates a demo org with a known SMS token balance, a handful of opted-in subscribers, an event,
and two draft plans:

* "[CHARGE DEMO] Ready to send now" — 3 draft messages whose send time is already past, so
  "Confirm & schedule all" sends them immediately and you watch the wallet drop AT SEND.
* "[CHARGE DEMO] Scheduled for later" — 3 future-dated draft messages, so "Confirm & schedule
  all" schedules them with NO debit (charging is deferred to when they send).

Sends only "succeed" in dev when the server runs with ``E2E_TEST_MODE=True`` (no Twilio call);
otherwise every send fails and the full charge is refunded (net $0), which still demonstrates
"bill only what actually sends".

Two modes (mirrors seed_plan_delete_lock_demo):

* Default — a dedicated demo org (slug ``charge-at-send-demo``) with its own owner login.
  ``--cleanup`` deletes the whole org.
* ``--in-org <email-or-slug>`` — seeds clearly-labeled demo rows into an existing org (uses your
  real login). ``--cleanup --in-org <...>`` removes only those demo rows.

Flags:
* ``--balance-tokens N`` — starting wallet balance in tokens (default 100).
* ``--broke`` — start the wallet at 0 tokens, to watch sends FAIL for lack of credits.

Usage::

    python manage.py seed_charge_at_send_demo
    python manage.py seed_charge_at_send_demo --in-org info@cueup.co
    python manage.py seed_charge_at_send_demo --broke
    python manage.py seed_charge_at_send_demo --cleanup
    python manage.py seed_charge_at_send_demo --in-org info@cueup.co --cleanup
"""
from datetime import time as _time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tickets.models import (
    Customer, Event, Organization, SMSCampaign, SMSCampaignPlan, UserProfile, Venue,
)
from tickets.services.sms_credits import price_per_segment_cents

DEFAULT_SLUG = 'charge-at-send-demo'
OWNER_USERNAME = 'charge-at-send-demo-owner'
OWNER_PASSWORD = 'chargedemo123'
OWNER_EMAIL = 'charge-at-send-demo-owner@example.com'

DEMO_PREFIX = '[CHARGE DEMO] '
VENUE_NAME = DEMO_PREFIX + 'Demo Hall'
EVENT_NAME = DEMO_PREFIX + 'Solstice Festival'
NOW_PLAN_NAME = DEMO_PREFIX + 'Ready to send now'
LATER_PLAN_NAME = DEMO_PREFIX + 'Scheduled for later'
PLAN_NAMES = (NOW_PLAN_NAME, LATER_PLAN_NAME)
# 5 fictional (555-01xx) opted-in subscribers so a send is a few tokens, not one.
DEMO_PHONES = [f'+1310555013{i}' for i in range(5)]
CUSTOMER_EMAILS = [f'charge-at-send-demo-fan{i}@example.com' for i in range(5)]


class Command(BaseCommand):
    help = "Seed plans + subscribers to test charge-at-send SMS billing."

    def add_arguments(self, parser):
        parser.add_argument('--slug', default=DEFAULT_SLUG, help='Dedicated demo org slug.')
        parser.add_argument(
            '--in-org', dest='in_org',
            help='Seed demo rows into an EXISTING org (owner email or slug). Cleanup only '
                 'removes the demo rows.',
        )
        parser.add_argument('--cleanup', action='store_true',
                            help='Remove seeded data instead of creating it.')
        parser.add_argument('--balance-tokens', type=int, default=100,
                            help='Starting wallet balance in tokens (default 100).')
        parser.add_argument('--broke', action='store_true',
                            help='Start the wallet at 0 tokens (to watch sends fail).')

    def handle(self, *args, **options):
        in_org = options.get('in_org')
        if options['cleanup']:
            return self._cleanup_in_org(in_org) if in_org else self._cleanup_org(options['slug'])

        if in_org:
            org = self._resolve_existing_org(in_org)
            owner = self._org_owner(org)
            scoped = True
            if not org.ai_sms_strategist_enabled:
                raise CommandError(
                    f'Org "{org.name}" does not have ai_sms_strategist_enabled - enable it first '
                    f'(the plans UI is gated on it).'
                )
        else:
            org, _ = Organization.objects.get_or_create(
                slug=options['slug'], defaults={'name': 'Charge At Send Demo'},
            )
            fields = []
            if not org.sms_marketing_enabled:
                org.sms_marketing_enabled = True
                fields.append('sms_marketing_enabled')
            if not org.ai_sms_strategist_enabled:
                org.ai_sms_strategist_enabled = True
                fields.append('ai_sms_strategist_enabled')
            if fields:
                org.save(update_fields=fields)
            owner = self._ensure_demo_owner(org)
            scoped = False

        tokens = 0 if options['broke'] else options['balance_tokens']
        balance_cents = int(Decimal(tokens) * price_per_segment_cents())
        org.sms_credit_balance_cents = balance_cents
        org.save(update_fields=['sms_credit_balance_cents'])

        venue = self._venue(org)
        event = self._event(org, venue)
        self._customers(org)

        now_plan = self._now_plan(org, owner, event)
        later_plan = self._later_plan(org, owner, event)
        self._report(org, owner, now_plan, later_plan, tokens, balance_cents, scoped, in_org)

    # --- step + plan builders -----------------------------------------------------

    def _step(self, order, purpose, body, rationale, send_at, timing_label):
        return {
            'order': order,
            'purpose': purpose,
            'audience_label': 'All SMS subscribers',
            'audience_criteria': {'all_subscribers': True},
            'timing_label': timing_label,
            'send_at': send_at.isoformat(),
            'body': body,
            'rationale': rationale,
            'segments': 1,
            'encoding': 'GSM-7',
            'launched_campaign_id': None,
            'launched_at': None,
        }

    def _now_plan(self, org, owner, event):
        """3 draft messages already 'due' — Confirm & schedule all sends them now (charge-at-send)."""
        now = timezone.now()
        steps = [
            self._step(0, 'announcement',
                       'Solstice Festival hits LA on 8/28! Grab your tickets now.',
                       'Announce the event and drive early sales.',
                       now - timedelta(minutes=5), 'Due now'),
            self._step(1, 'reminder',
                       "Solstice Festival is selling fast — lock in your spot at The Echo.",
                       'Mid-cycle urgency nudge.',
                       now - timedelta(minutes=4), 'Due now'),
            self._step(2, 'last_chance',
                       "Final call for Solstice Festival at The Echo on 8/28. Don't miss out!",
                       'Last-chance push.',
                       now - timedelta(minutes=3), 'Due now'),
        ]
        return self._upsert_plan(
            org, owner, event, NOW_PLAN_NAME, steps,
            objective='Sell out the remaining tickets',
            summary='Three due-now messages. "Confirm & schedule all" sends them immediately, so '
                    'you can watch the wallet debit AT SEND (not at schedule).',
            status=SMSCampaignPlan.Status.DRAFT,
        )

    def _later_plan(self, org, owner, event):
        """3 future-dated draft messages — Confirm & schedule all schedules with NO debit."""
        now = timezone.now()
        steps = [
            self._step(0, 'announcement',
                       'Save the date — Solstice Festival lands at The Echo on 8/28.',
                       'Early save-the-date.',
                       now + timedelta(days=2), 'In 2 days'),
            self._step(1, 'reminder',
                       'Tickets for Solstice Festival on 8/28 are moving — grab yours today.',
                       'Mid-cycle nudge.',
                       now + timedelta(days=10), 'In 10 days'),
            self._step(2, 'last_chance',
                       'Last chance for Solstice Festival at The Echo on 8/28!',
                       'Final urgency push.',
                       now + timedelta(days=19), 'In 19 days'),
        ]
        return self._upsert_plan(
            org, owner, event, LATER_PLAN_NAME, steps,
            objective='Warm up the audience ahead of on-sale',
            summary='Three future-dated messages. "Confirm & schedule all" schedules them with NO '
                    'charge — each is billed only when it later sends.',
            status=SMSCampaignPlan.Status.DRAFT,
        )

    def _upsert_plan(self, org, owner, event, name, steps, *, objective, summary, status):
        plan, _ = SMSCampaignPlan.objects.get_or_create(
            organization=org, name=name,
            defaults={
                'created_by': owner, 'event': event, 'objective': objective,
                'strategy_summary': summary, 'steps': steps, 'status': status,
            },
        )
        # Idempotent re-seed: reset to the demo state (also clears any launched campaigns from a
        # prior test run so the plan is all-draft again).
        plan.created_by = owner
        plan.event = event
        plan.objective = objective
        plan.strategy_summary = summary
        plan.steps = steps
        plan.status = status
        plan.enabled = True
        plan.save(update_fields=['created_by', 'event', 'objective', 'strategy_summary',
                                 'steps', 'status', 'enabled', 'updated_at'])
        # Drop campaigns spawned by earlier confirm/send runs so re-seeding is a clean slate.
        SMSCampaign.objects.filter(organization=org, plan=plan).delete()
        return plan

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
            username=OWNER_USERNAME, defaults={'email': OWNER_EMAIL},
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

    def _event(self, org, venue):
        start_date = timezone.localdate() + timedelta(days=21)
        event, _ = Event.objects.get_or_create(
            organization=org, name=EVENT_NAME, venue=venue,
            defaults={'start_date': start_date, 'start_time': _time(21, 0)},
        )
        return event

    def _customers(self, org):
        for i, (email, phone) in enumerate(zip(CUSTOMER_EMAILS, DEMO_PHONES)):
            Customer.objects.update_or_create(
                organization=org, email=email,
                defaults={'name': f'Demo Fan {i}', 'phone': phone, 'sms_opt_in': True},
            )

    # --- reporting ----------------------------------------------------------------

    def _report(self, org, owner, now_plan, later_plan, tokens, balance_cents, scoped, in_org):
        w = self.stdout.write
        s = self.style
        price = price_per_segment_cents()
        per_send = len(DEMO_PHONES)  # 1 segment × 5 recipients = 5 tokens per message
        w(s.SUCCESS('\nCharge-at-send demo seeded.'))
        w(f'  Org:      {org.name} (slug: {org.slug})')
        if not scoped:
            w(f'  Login:    {OWNER_EMAIL} / {OWNER_PASSWORD}')
        w(f'  Wallet:   {tokens} tokens ({balance_cents}¢ at {price}¢/segment)')
        w(f'  Audience: {per_send} opted-in subscribers → {per_send} tokens per 1-segment message')
        w(f'  Plans:    "{now_plan.name}"  and  "{later_plan.name}"')
        w('')
        w(s.MIGRATE_HEADING('Try these (open the plan detail page for each):'))
        w(s.HTTP_INFO('  1. No charge at schedule'))
        w(f'     Open "{later_plan.name}" → Confirm & schedule all → confirm.')
        w('     The messages become Scheduled but the wallet is UNCHANGED — nothing is charged '
          'until they send.')
        w(s.HTTP_INFO('  2. Charge AT send'))
        w(f'     Open "{now_plan.name}" (its messages are due now) → Confirm & schedule all → confirm.')
        w(f'     Each message sends immediately and the wallet drops by ~{per_send} tokens per '
          'message (Finance → SMS ledger shows a CHARGE row per campaign).')
        w('     NOTE: run the dev server with E2E_TEST_MODE=True so sends "succeed" and the charge '
          'sticks. Without it, sends fail and the full charge is refunded (net $0) — which shows '
          '"bill only what actually sends".')
        w(s.HTTP_INFO('  3. Fail at send (insufficient funds)'))
        w('     Re-seed broke:  python manage.py seed_charge_at_send_demo'
          + (f' --in-org {in_org}' if in_org else '') + ' --broke')
        w(f'     Then Confirm & schedule all on "{now_plan.name}" → each campaign is marked FAILED '
          '("Not enough SMS tokens…") and nothing is charged or sent.')
        w(s.HTTP_INFO('  4. Refund the unsent portion (opt-out)'))
        w('     Before sending, suppress one subscriber, e.g. in a shell:')
        w(f"       PhoneSuppression.objects.create(phone='{DEMO_PHONES[0]}', organization=None, "
          "reason=PhoneSuppression.Reason.MANUAL)")
        w(f'     Then send "{now_plan.name}" → charged the full snapshot at send-start, then '
          'refunded the opted-out recipient so the net debit equals what actually went out.')
        w('')
        cleanup = 'python manage.py seed_charge_at_send_demo --cleanup'
        if in_org:
            cleanup += f' --in-org {in_org}'
        w(s.WARNING(f'Cleanup when done:  {cleanup}'))
        w('')

    # --- cleanup ------------------------------------------------------------------

    def _cleanup_org(self, slug):
        org = Organization.objects.filter(slug=slug).first()
        if not org:
            self.stdout.write(self.style.WARNING(f'No demo org with slug "{slug}".'))
            return
        User.objects.filter(username=OWNER_USERNAME).delete()
        org.delete()
        self.stdout.write(self.style.SUCCESS(f'Deleted demo org "{slug}" and its data.'))

    def _cleanup_in_org(self, ident):
        org = self._resolve_existing_org(ident)
        plans = SMSCampaignPlan.objects.filter(organization=org, name__in=PLAN_NAMES)
        SMSCampaign.objects.filter(organization=org, plan__in=plans).delete()
        plans.delete()
        Customer.objects.filter(organization=org, email__in=CUSTOMER_EMAILS).delete()
        Event.objects.filter(organization=org, name=EVENT_NAME).delete()
        Venue.objects.filter(organization=org, name=VENUE_NAME).delete()
        self.stdout.write(self.style.SUCCESS(
            f'Removed [CHARGE DEMO] rows from org "{org.name}".'))
