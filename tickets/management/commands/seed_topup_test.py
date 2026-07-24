"""Seed a marketing-SMS plan whose next step costs MORE tokens than the org's wallet
holds, so you can exercise the "Not enough SMS tokens" banner top-up (one-click for a
saved card, or the inline Stripe card modal when there's no saved card).

Resolves the org from a user's email (default info@cueup.co), enables the SMS marketing +
AI strategist flags, seeds opted-in test subscribers, drops the wallet below the step's
cost, and creates a one-step "Top-up Test Plan" scheduled a few days out (so the confirm
button reads "Confirm & schedule", matching the real flow).

Usage::

    python manage.py seed_topup_test                          # info@cueup.co
    python manage.py seed_topup_test --email me@example.com
    python manage.py seed_topup_test --org-slug my-org
    python manage.py seed_topup_test --recipients 60 --balance-tokens 10
    python manage.py seed_topup_test --cleanup

Idempotent: re-running reuses/refreshes the seeded rows. The modal (no-saved-card) path
needs Stripe TEST keys in the environment (STRIPE_SECRET_KEY + STRIPE_PUBLISHABLE_KEY);
without them the banner shows the shortfall but no top-up button. The one-click path
additionally needs a real saved card on the org (complete one modal top-up first).
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from tickets.models import Customer, Organization, SMSCampaignPlan, UserProfile

User = get_user_model()

PLAN_NAME = 'Top-up Test Plan'
SEGMENT = 'champions'
EMAIL_PREFIX = 'topup-test-'
PHONE_PREFIX = '+1555010'  # + a 4-digit index → valid +1 (US) E.164, unlikely to collide


class Command(BaseCommand):
    help = "Seed an SMS plan step that costs more tokens than the wallet holds (banner top-up test)."

    def add_arguments(self, parser):
        parser.add_argument('--email', default='info@cueup.co',
                            help='User whose organization to seed (default info@cueup.co).')
        parser.add_argument('--org-slug', default=None,
                            help='Seed this org by slug instead of resolving from --email.')
        parser.add_argument('--recipients', type=int, default=40,
                            help='Opted-in test subscribers to create (default 40).')
        parser.add_argument('--balance-tokens', type=int, default=10,
                            help='Wallet balance to set, in tokens (default 10). Keep it below '
                                 'the recipient count so the step is unaffordable.')
        parser.add_argument('--days-out', type=int, default=3,
                            help='Schedule the step this many days out (default 3).')
        parser.add_argument('--cleanup', action='store_true',
                            help='Remove the seeded plan + test subscribers instead of creating them.')

    @transaction.atomic
    def handle(self, *args, **opts):
        from tickets.services.sms_credits import price_per_segment_cents
        from tickets.templatetags.tickets_extras import tokens as tokens_of

        org = self._resolve_org(opts)

        if opts['cleanup']:
            return self._cleanup(org)

        # 1) Feature flags the plan page + preview require.
        changed = []
        if not org.sms_marketing_enabled:
            org.sms_marketing_enabled = True
            changed.append('sms_marketing_enabled')
        if not org.ai_sms_strategist_enabled:
            org.ai_sms_strategist_enabled = True
            changed.append('ai_sms_strategist_enabled')

        # 2) Opted-in subscribers with US phones + the segment the plan targets.
        n = max(1, opts['recipients'])
        for i in range(n):
            Customer.objects.update_or_create(
                organization=org, email=f'{EMAIL_PREFIX}{i}@example.com',
                defaults={
                    'name': f'Top-up Tester {i}',
                    'phone': f'{PHONE_PREFIX}{i:04d}',
                    'sms_opt_in': True,
                    'sms_opt_in_date': timezone.now(),
                    'rfm_segment': SEGMENT,
                },
            )

        # 3) Drop the wallet below the step's cost so the banner fires.
        price = price_per_segment_cents()
        balance_cents = int(max(0, opts['balance_tokens']) * price)
        org.sms_credit_balance_cents = balance_cents
        changed.append('sms_credit_balance_cents')
        # Clear any saved card so the no-saved-card MODAL path is what shows by default.
        for f in ('stripe_pm_id', 'stripe_pm_brand', 'stripe_pm_last4',
                  'stripe_pm_exp_month', 'stripe_pm_exp_year'):
            setattr(org, f, None if f.endswith(('month', 'year', 'id')) else '')
        org.save()

        # 4) One unlaunched, scheduled step targeting the segment.
        send_at = (timezone.now() + timedelta(days=opts['days_out'])).replace(microsecond=0)
        step = {
            'order': 0,
            'purpose': 'reminder',
            'audience_label': 'Champions',
            'audience_criteria': {'rfm_segment': [SEGMENT]},
            'timing_label': send_at.strftime('%a, %b %-d · %-I:%M %p'),
            'send_at': send_at.isoformat(),
            'body': ("TOMORROW NIGHT! Tempo at Melrose House — 10PM. "
                     "Afrobeats, Dancehall & more. Don't miss out! https://cueup.co/t/demo"),
            'rationale': 'A reminder the day before keeps the event top-of-mind for last-minute buyers.',
            'segments': 1,
            'encoding': 'GSM-7',
            'launched_campaign_id': None,
        }
        plan, _ = SMSCampaignPlan.objects.update_or_create(
            organization=org, name=PLAN_NAME,
            defaults={
                'objective': 'Test the not-enough-tokens banner top-up',
                'strategy_summary': ('Seeded plan for QA: this reminder costs more SMS tokens '
                                     'than the wallet holds, so the confirm panel surfaces the '
                                     'inline top-up.'),
                'steps': [step],
                'status': SMSCampaignPlan.Status.DRAFT,
            },
        )

        cost_tokens = n  # 1 segment/recipient at this body length
        self._print_summary(org, plan, n, balance_cents, cost_tokens, tokens_of, price, changed)

    # ------------------------------------------------------------------ helpers

    def _resolve_org(self, opts):
        if opts.get('org_slug'):
            org = Organization.objects.filter(slug=opts['org_slug']).first()
            if not org:
                raise CommandError(f'No organization with slug "{opts["org_slug"]}".')
            return org
        email = opts['email']
        user = User.objects.filter(email__iexact=email).first()
        if not user:
            raise CommandError(
                f'No user with email "{email}". Pass --email <your login> or '
                f'--org-slug <slug>.'
            )
        profile = UserProfile.objects.filter(user=user).select_related('organization').first()
        if not profile or not profile.organization_id:
            raise CommandError(
                f'User "{email}" has no organization. Pass --org-slug <slug> instead.'
            )
        return profile.organization

    def _cleanup(self, org):
        plans = SMSCampaignPlan.objects.filter(organization=org, name=PLAN_NAME).delete()
        custs = Customer.objects.filter(
            organization=org, email__startswith=EMAIL_PREFIX,
        ).delete()
        self.stdout.write(self.style.SUCCESS(
            f'Removed seeded rows for "{org.name}": plans={plans[0]}, subscribers={custs[0]}. '
            f'(Wallet balance + feature flags left as-is.)'
        ))

    def _print_summary(self, org, plan, n, balance_cents, cost_tokens, tokens_of, price, changed):
        path = f'/marketing/sms/plan/{plan.id}/'
        site = (getattr(settings, 'SITE_URL', '') or '').rstrip('/')
        url = f'{site}{path}' if site else path
        stripe_ready = bool(getattr(settings, 'STRIPE_SECRET_KEY', ''))
        pub_ready = bool(getattr(settings, 'STRIPE_PUBLISHABLE_KEY', ''))

        self.stdout.write(self.style.SUCCESS(
            f'\nSeeded top-up test for "{org.name}".\n'
            f'  Subscribers: {n} opted-in (segment "{SEGMENT}")\n'
            f'  Wallet balance: {tokens_of(balance_cents)} tokens '
            f'(${balance_cents / 100:.2f})\n'
            f'  Step cost: ~{cost_tokens} tokens  →  short by '
            f'~{max(0, cost_tokens - tokens_of(balance_cents))} tokens\n'
            f'  Plan: {plan.name} ({plan.id})\n'
            f'  Open: {url}'
        ))
        self.stdout.write(
            '\nHow to test: open the plan, click "Confirm & Schedule" on the step. The panel '
            'shows the shortfall and a "Buy tokens" button that opens the inline Stripe card '
            'modal (test card 4242 4242 4242 4242, any future expiry/CVC). After paying, the '
            'panel refreshes and the send button enables; a saved card then unlocks one-click.'
        )
        if not (stripe_ready and pub_ready):
            missing = ', '.join(k for k, ok in [
                ('STRIPE_SECRET_KEY', stripe_ready), ('STRIPE_PUBLISHABLE_KEY', pub_ready)
            ] if not ok)
            self.stdout.write(self.style.WARNING(
                f'\nHeads up: {missing} not set — the banner will show the shortfall but no '
                f'top-up button. Set Stripe TEST keys to exercise the purchase.'
            ))
        if changed:
            self.stdout.write(f'\nUpdated org fields: {", ".join(dict.fromkeys(changed))}.')
        self.stdout.write('Re-run with --cleanup to remove the seeded plan + subscribers.')
