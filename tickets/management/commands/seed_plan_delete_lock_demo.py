"""Seed AI campaign plans to test the "can't delete once sent" lock.

Creates two plans so you can see both sides of the rule on the plans list and the plan
detail page:

* "Sent - delete locked" - a 3-step plan whose first message has already been SENT (a real
  SMSCampaign row, status=sent, linked to the step). Its Delete button is hidden on both the
  list and the detail header, the sent step's trash icon is gone, and POSTing the delete/remove
  endpoints is refused server-side.

* "Draft - deletable" - a 3-step all-draft plan that still deletes normally, for contrast.

Two modes:

* Default - a dedicated demo org (slug ``plan-delete-demo``) with its own owner login.
  ``--cleanup`` deletes the whole org.

* ``--in-org <email-or-slug>`` - seeds ONLY clearly-labeled demo rows into an existing org so
  you can test with your real login. ``--cleanup --in-org <...>`` removes only those demo rows.

Usage::

    python manage.py seed_plan_delete_lock_demo
    python manage.py seed_plan_delete_lock_demo --in-org info@cueup.co
    python manage.py seed_plan_delete_lock_demo --in-org info@cueup.co --cleanup
"""
from datetime import time as _time, timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tickets.models import (
    Customer, Event, Organization, SMSCampaign, SMSCampaignPlan, UserProfile, Venue,
)

DEFAULT_SLUG = 'plan-delete-demo'
OWNER_USERNAME = 'plan-delete-demo-owner'
OWNER_PASSWORD = 'plandemo123'
OWNER_EMAIL = 'plan-delete-demo-owner@example.com'
# Demo rows carry these markers so scoped cleanup can find them without touching real data.
DEMO_PREFIX = '[PLAN DEMO] '
VENUE_NAME = DEMO_PREFIX + 'Demo Hall'
EVENT_NAME = DEMO_PREFIX + 'Solstice Festival'
CUSTOMER_EMAIL = 'plan-delete-demo-fan@example.com'
SENT_PLAN_NAME = DEMO_PREFIX + 'Sent - delete locked'
DRAFT_PLAN_NAME = DEMO_PREFIX + 'Draft - deletable'
PLAN_NAMES = (SENT_PLAN_NAME, DRAFT_PLAN_NAME)
SENT_CAMPAIGN_KEY = 'plan-delete-demo-sent'


class Command(BaseCommand):
    help = "Seed plans to test the 'can't delete a plan once a message has been sent' lock."

    def add_arguments(self, parser):
        parser.add_argument('--slug', default=DEFAULT_SLUG, help='Dedicated demo org slug.')
        parser.add_argument(
            '--in-org', dest='in_org',
            help='Seed demo rows into an EXISTING org (by owner email or slug). Cleanup only '
                 'removes the demo rows.',
        )
        parser.add_argument('--cleanup', action='store_true',
                            help='Remove seeded data instead of creating it.')

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
                slug=options['slug'],
                defaults={'name': 'Plan Delete Lock Demo'},
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

        venue = self._venue(org)
        event = self._event(org, venue)
        self._customer(org)

        sent_plan = self._sent_plan(org, owner, event)
        draft_plan = self._draft_plan(org, owner, event)
        self._report(org, owner, sent_plan, draft_plan, scoped, in_org)

    # --- step + plan builders -----------------------------------------------------

    def _step(self, order, purpose, body, rationale, send_at, timing_label,
              *, launched_campaign_id=None, launched_at=None):
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
            'launched_campaign_id': launched_campaign_id,
            'launched_at': launched_at.isoformat() if launched_at else None,
        }

    def _sent_plan(self, org, owner, event):
        """A plan whose first message is already SENT - so it can't be deleted."""
        now = timezone.now()
        # A real SENT campaign the first step points at (this is what trips the lock).
        campaign, _ = SMSCampaign.objects.get_or_create(
            organization=org, idempotency_key=SENT_CAMPAIGN_KEY,
            defaults={
                'name': DEMO_PREFIX + 'Announcement (sent)',
                'body': 'TEMPO returns to LA on 8/28! Afrobeat, Amapiano & Dancehall all night. '
                        'Grab your tix now!',
                'event': event, 'created_by': owner,
                'status': SMSCampaign.Status.SENT, 'sent_at': now - timedelta(days=1),
                'audience_size': 1,
            },
        )
        steps = [
            self._step(
                0, 'announcement',
                campaign.body,
                'Announces the event early to build awareness and give subscribers time to plan.',
                now - timedelta(days=1),
                'Sent yesterday',
                launched_campaign_id=str(campaign.id), launched_at=now - timedelta(days=1),
            ),
            self._step(
                1, 'social_proof',
                "LA's buzzing for TEMPO at Melrose House on 8/28! Secure your spot before it's too late.",
                'Leverages excitement to encourage timely ticket purchases.',
                now + timedelta(days=13), 'In 13 days',
            ),
            self._step(
                2, 'last_chance',
                "Final call for TEMPO at Melrose House on 8/28! Don't miss out - secure your tickets now.",
                "Creates urgency with 'Final call' to encourage last-minute sales.",
                now + timedelta(days=20), 'In 20 days',
            ),
        ]
        return self._upsert_plan(
            org, owner, event, SENT_PLAN_NAME, steps,
            objective='Sell out the remaining tickets',
            summary='A three-touch countdown: announce the event, follow with social proof, '
                    'then close with a last-chance urgency push.',
            status=SMSCampaignPlan.Status.IN_PROGRESS,
        )

    def _draft_plan(self, org, owner, event):
        """An all-draft plan that still deletes normally, for contrast."""
        now = timezone.now()
        steps = [
            self._step(0, 'announcement',
                       'Save the date - TEMPO lands at Melrose House on 8/28. Tickets drop soon!',
                       'Early save-the-date to prime the audience.',
                       now + timedelta(days=2), 'In 2 days'),
            self._step(1, 'reminder',
                       'Tickets for TEMPO on 8/28 are moving fast - lock yours in today.',
                       'Mid-cycle nudge for fence-sitters.',
                       now + timedelta(days=10), 'In 10 days'),
            self._step(2, 'last_chance',
                       'Last chance for TEMPO at Melrose House on 8/28. Grab tix before they sell out!',
                       'Final urgency push.',
                       now + timedelta(days=19), 'In 19 days'),
        ]
        return self._upsert_plan(
            org, owner, event, DRAFT_PLAN_NAME, steps,
            objective='Build the audience ahead of on-sale',
            summary='A gentle three-touch warm-up with nothing sent yet - fully deletable.',
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
        # Idempotent re-seed: refresh the steps/status so re-running always lands the demo state.
        plan.created_by = owner
        plan.event = event
        plan.objective = objective
        plan.strategy_summary = summary
        plan.steps = steps
        plan.status = status
        plan.save(update_fields=['created_by', 'event', 'objective', 'strategy_summary',
                                 'steps', 'status', 'updated_at'])
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

    def _customer(self, org):
        Customer.objects.get_or_create(
            organization=org, email=CUSTOMER_EMAIL,
            defaults={'name': '[DEMO] Fan', 'phone': '+13105550001',
                      'sms_opt_in': True, 'rfm_segment': 'VIP'},
        )

    # --- output -------------------------------------------------------------------

    def _report(self, org, owner, sent_plan, draft_plan, scoped, in_org):
        s, w = self.style, self.stdout.write
        w(s.SUCCESS(f'\nSeeded plan-delete-lock demo into "{org.name}" (slug={org.slug}).'))
        if scoped:
            login = getattr(owner, 'email', None) or getattr(owner, 'username', '<owner>')
            w(f'  Mode:  scoped into your existing org - demo rows only.')
            w(f'  Login: your own account ({login})')
        else:
            w(f'  Login: {OWNER_USERNAME} / {OWNER_PASSWORD}')
        w('')
        w('  Plans (Marketing -> Plans):')
        w(f'    LOCKED  "{sent_plan.name}"  - first message is SENT; no Delete button, sent step has no trash.')
        w(f'    OK      "{draft_plan.name}" - all draft; deletes normally.')
        w('')
        w(s.WARNING('  How to test:'))
        w(f'   • Open the plans list: {self._url("tickets:sms_plan_list")}')
        w(f'     - "{sent_plan.name}" row shows NO trash icon.')
        w(f'     - "{draft_plan.name}" row DOES show a trash icon and deletes.')
        w(f'   • Open the locked plan detail: /marketing/sms/plan/{sent_plan.id}/')
        w('     - No "Delete" button in the header; the "Sent" (first) message has no trash icon,')
        w('       while the two draft messages still do.')
        w('   • Server-side guard: POST the delete endpoint directly and it redirects back with an')
        w('     error instead of deleting (the UI just hides the affordance).')
        w('')
        args = f'--in-org {in_org} ' if scoped else ''
        w(f'  Remove when done:  python manage.py seed_plan_delete_lock_demo {args}--cleanup\n')

    def _url(self, name):
        from django.urls import reverse
        try:
            return reverse(name)
        except Exception:
            return f'<{name}>'

    # --- cleanup ------------------------------------------------------------------

    def _cleanup_in_org(self, ident):
        """Scoped cleanup: delete ONLY demo-marked rows. Never the org, user, or real data."""
        org = self._resolve_existing_org(ident)
        n_plan = SMSCampaignPlan.objects.filter(organization=org, name__in=PLAN_NAMES).delete()[0]
        n_camp = SMSCampaign.objects.filter(
            organization=org, idempotency_key=SENT_CAMPAIGN_KEY,
        ).delete()[0]
        n_cust = Customer.objects.filter(organization=org, email=CUSTOMER_EMAIL).delete()[0]
        Event.objects.filter(organization=org, name=EVENT_NAME).delete()
        Venue.objects.filter(organization=org, name=VENUE_NAME).delete()
        self.stdout.write(self.style.SUCCESS(
            f'Removed demo rows from "{org.name}": {n_plan} plans, {n_camp} campaigns, '
            f'{n_cust} customers, + demo event/venue. Org and real data untouched.'
        ))

    def _cleanup_org(self, slug):
        org = Organization.objects.filter(slug=slug).first()
        if not org:
            self.stdout.write(self.style.WARNING(f'No org with slug "{slug}"; nothing to clean up.'))
            return
        org_id = org.id
        SMSCampaignPlan.objects.filter(organization=org).delete()
        SMSCampaign.objects.filter(organization=org).delete()
        Customer.objects.filter(organization=org).delete()
        Event.objects.filter(organization=org).delete()
        Venue.objects.filter(organization=org).delete()
        User.objects.filter(username=OWNER_USERNAME).delete()
        org.delete()
        self.stdout.write(self.style.SUCCESS(
            f'Removed demo org {org_id} (slug={slug}) and its data.'
        ))
