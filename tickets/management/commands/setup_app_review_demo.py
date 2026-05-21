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
        parser.add_argument('--password', required=True,
                            help="Password for the demo user's web login. "
                                 "Pick something only you and Stripe-onboarding-time-you know.")
        parser.add_argument('--first-name', default='App Review')
        parser.add_argument('--last-name', default='Demo')
        parser.add_argument('--org-name', default='App Review Demo, LLC')
        parser.add_argument('--venue-name', default='App Review Demo Venue')
        parser.add_argument('--venue-city', default='Los Angeles')
        parser.add_argument('--venue-state', default='CA')
        parser.add_argument('--event-name', default='App Review Demo Show')
        parser.add_argument('--ticket-price', type=Decimal, default=Decimal('1.00'))
        parser.add_argument('--ticket-name', default='General Admission')
        parser.add_argument('--days-out', type=int, default=30,
                            help='Days from today to schedule the demo event.')

    @transaction.atomic
    def handle(self, *args, **opts):
        phone = opts['phone']
        if phone not in getattr(settings, 'APP_REVIEW_TEST_PHONES', {}):
            self.stdout.write(self.style.WARNING(
                f"Heads up: {phone} is NOT in settings.APP_REVIEW_TEST_PHONES. "
                f"Add it (with a fixed OTP code) before submitting to App Review, "
                f"or the bypass won't trigger and the reviewer can't sign in."
            ))

        org = self._get_or_create_org(opts['org_name'])
        user = self._get_or_create_user(
            email=opts['email'],
            password=opts['password'],
            first_name=opts['first_name'],
            last_name=opts['last_name'],
        )
        self._get_or_create_profile(user, phone, org)
        self._get_or_create_membership(user, org)
        venue = self._get_or_create_venue(org, opts['venue_name'],
                                          opts['venue_city'], opts['venue_state'])
        event = self._get_or_create_event(org, venue, opts['event_name'],
                                          opts['days_out'])
        ticket_type = self._get_or_create_ticket_type(
            event, opts['ticket_name'], opts['ticket_price']
        )

        self._print_summary(
            phone=phone,
            user=user,
            org=org,
            venue=venue,
            event=event,
            ticket_type=ticket_type,
            password=opts['password'],
        )

    # ------------------------------------------------------------------ creators

    def _get_or_create_org(self, name):
        org, created = Organization.objects.get_or_create(
            name=name,
            defaults={'slug': self._unique_slug(name)},
        )
        self._note('Organization', org, created)
        return org

    def _get_or_create_user(self, *, email, password, first_name, last_name):
        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                'username': slugify(email.split('@')[0]) or 'app-review-demo',
                'first_name': first_name,
                'last_name': last_name,
            },
        )
        # Always reset the password on this command — operator may have
        # forgotten the previous one.
        user.set_password(password)
        if not user.first_name:
            user.first_name = first_name
        if not user.last_name:
            user.last_name = last_name
        user.save()
        self._note('User', user, created)
        return user

    def _get_or_create_profile(self, user, phone, org):
        profile, created = UserProfile.objects.get_or_create(
            user=user,
            defaults={
                'organization': org,
                'role': UserProfile.Role.ORGANIZER,
                'org_role': UserProfile.OrgRole.OWNER,
                'phone_number': phone,
                'terms_accepted_at': timezone.now(),
            },
        )
        # Keep the linkage healthy even on a re-run.
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
        if changed:
            profile.save()
        self._note('UserProfile', profile, created)
        return profile

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

    def _print_summary(self, *, phone, user, org, venue, event, ticket_type, password):
        otp = settings.APP_REVIEW_TEST_PHONES.get(phone, '(not configured)')
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(self.style.SUCCESS('App Review demo merchant ready.'))
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write('')
        self.stdout.write('iOS app review credentials (paste into App Store Connect):')
        self.stdout.write(f'  Phone: {phone}')
        self.stdout.write(f'  OTP:   {otp}')
        self.stdout.write('')
        self.stdout.write('Web dashboard credentials (for YOU to complete Stripe KYC):')
        self.stdout.write(f'  URL:      /login/email/  (or /admin/ if you want to impersonate)')
        self.stdout.write(f'  Email:    {user.email}')
        self.stdout.write(f'  Password: {password}')
        self.stdout.write('')
        self.stdout.write('Resources created:')
        self.stdout.write(f'  Org:    {org.name} (slug={org.slug}, pk={org.pk})')
        self.stdout.write(f'  Venue:  {venue}')
        self.stdout.write(f'  Event:  {event.name} on {event.start_date} (public_id={event.public_id})')
        self.stdout.write(f'  Ticket: {ticket_type.name} @ ${ticket_type.price}')
        self.stdout.write('')
        self.stdout.write('Next steps:')
        self.stdout.write('  1. Log into the web dashboard as the demo user above.')
        self.stdout.write('  2. Go to Finance → Connect Stripe → complete Express onboarding.')
        self.stdout.write('     Use Stripe identity-test values for SSN/EIN/bank to get live-mode')
        self.stdout.write('     KYC auto-approved (https://stripe.com/docs/connect/testing).')
        self.stdout.write('  3. Verify card_payments=active:')
        self.stdout.write(f"       python manage.py request_card_payments_capability --org {org.slug}")
        self.stdout.write('  4. From the iOS app, sign in with the phone+OTP above. /merchant/status/')
        self.stdout.write("     should now report tap_to_pay.status='enabled'.")
        self.stdout.write('  5. Document the phone+OTP in App Store Connect → App Review notes.')
        self.stdout.write('')
