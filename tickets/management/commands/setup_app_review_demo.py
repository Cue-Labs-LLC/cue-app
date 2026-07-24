"""
One-time setup for an App Store review demo merchant.

Creates (idempotently) the User + Organization + Venue + Event +
SaleableTicketType that the Apple reviewer will exercise, all linked to the
fixed test phone documented in App Store Connect review notes.

What this command DOES NOT do (deliberate manual steps):
  - Stripe Connect KYC. After this command finishes, log into the web
    dashboard as the demo user (email + password printed at the end), tap
    "Set up payments" → complete Stripe Express onboarding with Stripe's
    identity-test SSN/EIN values (see https://stripe.com/docs/connect/testing).
    Stop when `card_payments` is `active`.
  - Editing settings.APP_REVIEW_TEST_PHONES. The phone you pass must already
    be in that dict; the command verifies and warns otherwise.

Idempotent: safe to re-run. Existing rows are reused; nothing is destroyed.

Example:
    python manage.py setup_app_review_demo --password 'Reviewer-Demo-2026'
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.authtoken.models import Token

from tickets.models import (
    EVENT_STATUS_LIVE,
    Event,
    Organization,
    OrganizationMembership,
    SaleableTicketType,
    TICKETING_TYPE_DIRECT,
    UserProfile,
    Venue,
)


User = get_user_model()


class Command(BaseCommand):
    help = 'One-time setup of the App Store review demo merchant.'

    def add_arguments(self, parser):
        parser.add_argument('--phone', default='+15555550100',
                            help='Demo phone (must be a key in APP_REVIEW_TEST_PHONES).')
        parser.add_argument('--email', default='app-review-demo@cueup.co',
                            help="Demo user's email — used for web dashboard login.")
        parser.add_argument('--password', default=None,
                            help="Optional password for the demo user's web login "
                                 "(handy for completing Stripe onboarding from the "
                                 "dashboard). If omitted, a brand-new user is created "
                                 "phone-only (login via phone-OTP). Ignored with "
                                 "--skip-user.")
        parser.add_argument('--skip-user', action='store_true',
                            help='Skip creating/updating the User, UserProfile, and '
                                 'OrganizationMembership. Only the Venue/Event/TicketType '
                                 'are written. Useful when the org already has its operator '
                                 'wired up (e.g., via phone-OTP signup).')
        parser.add_argument('--first-name', default='App Review')
        parser.add_argument('--last-name', default='Demo')
        parser.add_argument('--org-name', default='Demo Events Co')
        parser.add_argument('--org-slug', default=None,
                            help='If set, reuse the existing org with this slug '
                                 'instead of creating/looking up by --org-name. '
                                 'Useful when the org was auto-minted by phone-OTP signup.')
        parser.add_argument('--venue-name', default='The Venue')
        parser.add_argument('--venue-city', default='San Francisco')
        parser.add_argument('--venue-state', default='CA')
        parser.add_argument('--event-name', default='Demo Event')
        parser.add_argument('--ticket-price', type=Decimal, default=Decimal('1.00'))
        parser.add_argument('--ticket-name', default='General Admission')
        parser.add_argument('--days-out', type=int, default=25,
                            help='Days from today to schedule the demo event.')

    @transaction.atomic
    def handle(self, *args, **opts):
        phone = opts['phone']
        skip_user = opts['skip_user']

        if not skip_user and phone not in getattr(settings, 'APP_REVIEW_TEST_PHONES', {}):
            self.stdout.write(self.style.WARNING(
                f"Heads up: {phone} is NOT in settings.APP_REVIEW_TEST_PHONES. "
                f"Add it (with a fixed OTP code) before submitting to App Review, "
                f"or the bypass won't trigger and the reviewer can't sign in."
            ))

        org = self._get_or_create_org(opts['org_name'], opts['org_slug'])

        user = None
        if not skip_user:
            profile = self._reconcile_operator(phone, org, opts)
            user = profile.user
            self._get_or_create_membership(user, org)
        else:
            self.stdout.write(self.style.NOTICE(
                '  Skipping User/UserProfile/Membership (--skip-user).'
            ))

        venue = self._get_or_create_venue(org, opts['venue_name'],
                                          opts['venue_city'], opts['venue_state'])
        event = self._get_or_create_event(org, venue, opts['event_name'],
                                          opts['days_out'])
        ticket_type = self._get_or_create_ticket_type(
            event, opts['ticket_name'], opts['ticket_price']
        )

        token = None
        if user is not None:
            token, _ = Token.objects.get_or_create(user=user)

        self._print_summary(
            phone=phone,
            user=user,
            org=org,
            venue=venue,
            event=event,
            ticket_type=ticket_type,
            password=opts['password'],
            token=token,
        )

    # ------------------------------------------------------------------ creators

    def _get_or_create_org(self, name, slug=None):
        if slug:
            try:
                org = Organization.objects.get(slug=slug)
            except Organization.DoesNotExist:
                raise CommandError(
                    f'No organization found with slug "{slug}". '
                    f'Drop --org-slug to create a new org by name, or pass an existing slug.'
                )
            self._note('Organization', org, created=False)
            return org
        org, created = Organization.objects.get_or_create(
            name=name,
            defaults={'slug': self._unique_slug(name)},
        )
        self._note('Organization', org, created)
        return org

    def _reconcile_operator(self, phone, org, opts):
        """Ensure the phone's operator profile exists and owns `org`.

        Phone-first, because `UserProfile.phone_number` is unique. In production
        the reviewer's phone-OTP sign-in has usually already auto-created an
        org-less profile that claims this phone; minting a second profile with
        the same phone would raise IntegrityError. So: if a profile already
        claims the phone, reuse its user and just attach the org. Only create a
        fresh user when no profile claims the phone yet.
        """
        profile = (
            UserProfile.objects
            .select_related('user')
            .filter(phone_number=phone)
            .first()
        )
        if profile is not None:
            user = profile.user
            # Optionally upgrade a phone-only user so it can also log into the
            # web dashboard (useful for completing Stripe onboarding). Never
            # clobber values that are already set.
            self._apply_login_upgrades(user, opts, force_password=bool(opts['password']))
            self._note('User (existing phone)', user, created=False)
        else:
            user = self._create_operator_user(opts)
            profile = UserProfile.objects.create(user=user, phone_number=phone)
            self._note('UserProfile', profile, created=True)

        # Attach org + roles + terms on both paths; idempotent on re-run.
        changed = False
        if profile.organization_id != org.pk:
            profile.organization = org
            changed = True
        if profile.role != UserProfile.Role.ORGANIZER:
            profile.role = UserProfile.Role.ORGANIZER
            changed = True
        if profile.org_role != UserProfile.OrgRole.OWNER:
            profile.org_role = UserProfile.OrgRole.OWNER
            changed = True
        if profile.phone_number != phone:
            profile.phone_number = phone
            changed = True
        if getattr(profile, 'terms_accepted_at', None) is None:
            profile.terms_accepted_at = timezone.now()
            changed = True
        if changed:
            profile.save()
            self._note('UserProfile linked to org', profile, created=False)
        return profile

    def _create_operator_user(self, opts):
        """Create the operator User (email-based). Password optional: when
        omitted the account is phone-only (login via phone-OTP)."""
        email = opts['email']
        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                'username': slugify(email.split('@')[0]) or 'app-review-demo',
                'first_name': opts['first_name'],
                'last_name': opts['last_name'],
            },
        )
        if opts['password']:
            user.set_password(opts['password'])
        elif created:
            user.set_unusable_password()
        if not user.first_name:
            user.first_name = opts['first_name']
        if not user.last_name:
            user.last_name = opts['last_name']
        user.save()
        self._note('User', user, created)
        return user

    def _apply_login_upgrades(self, user, opts, *, force_password):
        """Fill in email/name/password on an existing (often phone-only) user
        without overwriting anything already set."""
        changed = False
        if force_password:
            user.set_password(opts['password'])
            changed = True
        if opts['email'] and not user.email:
            user.email = opts['email']
            changed = True
        if opts['first_name'] and not user.first_name:
            user.first_name = opts['first_name']
            changed = True
        if opts['last_name'] and not user.last_name:
            user.last_name = opts['last_name']
            changed = True
        if changed:
            user.save()

    def _get_or_create_membership(self, user, org):
        membership, created = OrganizationMembership.objects.get_or_create(
            user=user,
            organization=org,
            defaults={'org_role': UserProfile.OrgRole.OWNER},
        )
        if membership.org_role != UserProfile.OrgRole.OWNER:
            membership.org_role = UserProfile.OrgRole.OWNER
            membership.save(update_fields=['org_role'])
        self._note('OrganizationMembership', membership, created)
        return membership

    def _get_or_create_venue(self, org, name, city, state):
        venue, created = Venue.objects.get_or_create(
            organization=org,
            name=name,
            city=city,
            defaults={'state': state, 'country': 'United States'},
        )
        self._note('Venue', venue, created)
        return venue

    def _get_or_create_event(self, org, venue, name, days_out):
        event, created = Event.objects.get_or_create(
            organization=org,
            name=name,
            defaults={
                'venue': venue,
                'start_date': (timezone.localdate() + timedelta(days=days_out)),
                'start_time': timezone.now().time().replace(hour=20, minute=0, second=0, microsecond=0),
                'ticketing_type': TICKETING_TYPE_DIRECT,
                'status': EVENT_STATUS_LIVE,
                'summary': 'App Store reviewer demo — sell a $1 ticket via Tap to Pay.',
            },
        )
        # Make sure a previously-created event still meets the demo invariants.
        changed = False
        if event.ticketing_type != TICKETING_TYPE_DIRECT:
            event.ticketing_type = TICKETING_TYPE_DIRECT
            changed = True
        if event.status != EVENT_STATUS_LIVE:
            event.status = EVENT_STATUS_LIVE
            changed = True
        if event.start_date < timezone.localdate():
            event.start_date = timezone.localdate() + timedelta(days=days_out)
            changed = True
        if changed:
            event.save()
        self._note('Event', event, created)
        return event

    def _get_or_create_ticket_type(self, event, name, price):
        tt, created = SaleableTicketType.objects.get_or_create(
            event=event,
            name=name,
            defaults={'price': price, 'is_active': True},
        )
        if tt.price != price or not tt.is_active:
            tt.price = price
            tt.is_active = True
            tt.save(update_fields=['price', 'is_active'])
        self._note('SaleableTicketType', tt, created)
        return tt

    # ------------------------------------------------------------------ helpers

    def _unique_slug(self, name):
        base = slugify(name) or 'app-review-demo'
        slug = base
        i = 1
        while Organization.objects.filter(slug=slug).exists():
            i += 1
            slug = f'{base}-{i}'
        return slug

    def _note(self, label, obj, created):
        verb = 'Created' if created else 'Reusing'
        style = self.style.SUCCESS if created else self.style.NOTICE
        self.stdout.write(style(f'  {verb} {label}: {obj} (pk={obj.pk})'))

    def _print_summary(self, *, phone, user, org, venue, event, ticket_type,
                       password, token):
        otp = settings.APP_REVIEW_TEST_PHONES.get(phone, '(not configured)')
        secret = getattr(settings, 'STRIPE_SECRET_KEY', '') or ''
        live_mode = secret.startswith('sk_live')
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(self.style.SUCCESS('App Review demo merchant ready.'))
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write('')
        if user is not None:
            self.stdout.write('iOS app sign-in (phone-OTP):')
            self.stdout.write(f'  Phone: {phone}')
            self.stdout.write(f'  OTP:   {otp}')
            self.stdout.write(f'  Token: {token.key if token else "(none)"}')
            self.stdout.write('')
            self.stdout.write('Web dashboard login (optional — for Stripe onboarding):')
            self.stdout.write(f'  Email:    {user.email or "(none set)"}')
            if password:
                self.stdout.write(f'  Password: {password}')
            else:
                self.stdout.write('  Password: (phone-only user — no web password set; '
                                  'pass --password to enable web login)')
            self.stdout.write('')
        else:
            self.stdout.write(self.style.NOTICE(
                'User/profile/membership were skipped (--skip-user). '
                'Make sure the org already has an operator user wired up.'
            ))
            self.stdout.write('')
        self.stdout.write('Resources created:')
        self.stdout.write(f'  Org:    {org.name} (slug={org.slug}, pk={org.pk})')
        self.stdout.write(f'  Venue:  {venue}')
        self.stdout.write(f'  Event:  {event.name} on {event.start_date} (public_id={event.public_id})')
        self.stdout.write(f'  Ticket: {ticket_type.name} @ ${ticket_type.price}')
        self.stdout.write('')
        self.stdout.write('Next steps (Stripe Connect — required for Tap to Pay):')
        self.stdout.write(f'  Stripe mode: {"LIVE" if live_mode else "TEST"} '
                          f'(from STRIPE_SECRET_KEY).')
        self.stdout.write('  1. Sign in on the iOS app (or web) and start Stripe Express onboarding.')
        if live_mode:
            self.stdout.write('  2. LIVE mode: complete KYC with REAL business/identity/bank details.')
            self.stdout.write('     Test SSN/routing values will NOT be accepted in live mode.')
        else:
            self.stdout.write('  2. TEST mode: use Stripe identity-test values for SSN/routing so KYC')
            self.stdout.write('     auto-approves (https://stripe.com/docs/connect/testing).')
        self.stdout.write('  3. Verify card_payments=active:')
        self.stdout.write(f"       python manage.py request_card_payments_capability --org {org.slug}")
        self.stdout.write("  4. /merchant/status/ should then report tap_to_pay.status='enabled'.")
        self.stdout.write('  5. Document the phone+OTP in App Store Connect → App Review notes.')
        self.stdout.write('')
