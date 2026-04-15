from datetime import date, timedelta
from decimal import Decimal

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tickets.models import (
    Event,
    Organization,
    SaleableTicketType,
    StripeCheckoutSession,
    Ticket,
    TicketOrder,
    UserProfile,
    Venue,
)

try:
    from playwright.async_api import async_playwright
except ModuleNotFoundError:  # pragma: no cover
    async_playwright = None


@override_settings(
    E2E_TEST_MODE=True,
    ALLOWED_HOSTS=['*'],
    STRIPE_PUBLISHABLE_KEY='pk_test_e2e',
    STRIPE_SECRET_KEY='sk_test_e2e',
)
class BrowserFlowTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if async_playwright is None:
            raise RuntimeError('playwright must be installed to run browser tests')

    def setUp(self):
        self.organization = Organization.objects.create(name='E2E Org', slug='e2e-org')
        self.venue = Venue.objects.create(
            organization=self.organization,
            name='Test Venue',
            city='Los Angeles',
        )
        self.event = Event.objects.create(
            organization=self.organization,
            name='Browser Test Event',
            venue=self.venue,
            start_date=date.today() + timedelta(days=14),
            ticketing_type='direct',
            status='live',
        )
        self.ticket_type = SaleableTicketType.objects.create(
            event=self.event,
            name='General Admission',
            price=Decimal('25.00'),
            quantity_limit=100,
            is_active=True,
        )
        self.returning_user = User.objects.create(
            username='returning-attendee',
            email='returning@example.com',
            first_name='Riley',
            last_name='Returning',
        )
        self.returning_user.set_unusable_password()
        self.returning_user.save()
        UserProfile.objects.create(
            user=self.returning_user,
            role=UserProfile.Role.ATTENDEE,
            phone_number='+14155550101',
            marketing_opt_in=True,
            terms_accepted_at=timezone.now(),
        )

    def run_browser(self, scenario):
        async_to_sync(scenario)()

    async def _run_in_browser(self, path, callback):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            async def abort_route(route):
                await route.abort()

            await context.route('**/*facebook*', abort_route)
            await context.route('**/*luckyorange*', abort_route)
            page = await context.new_page()
            try:
                await page.goto(f'{self.live_server_url}{path}')
                await callback(page)
            finally:
                await context.close()
                await browser.close()

    def test_phone_signup_flow_creates_attendee(self):
        phone = '4155550102'
        email = 'signup-browser@example.com'

        async def scenario():
            async def callback(page):
                await page.locator('[data-testid="phone-number-input"]').fill(phone)
                await page.get_by_role('button', name='Send code').click()

                await page.wait_for_url('**/login/verify/')
                await page.locator('[data-testid="otp-code-input"]').fill('000000')
                await page.get_by_role('button', name='Verify').click()

                await page.wait_for_url('**/login/complete-profile/')
                await page.locator('[data-testid="first-name-input"]').fill('Casey')
                await page.locator('[data-testid="last-name-input"]').fill('Signup')
                await page.locator('[data-testid="email-input"]').fill(email)
                await page.locator('#id_gender').select_option('other')
                await page.locator('#id_terms_accepted').check()
                await page.locator('#id_marketing_opt_in').check()
                await page.get_by_role('button', name='Complete setup').click()

                await page.wait_for_url('**/login/verify-email/')
                await page.locator('[data-testid="otp-code-input"]').fill('000000')
                await page.get_by_role('button', name='Verify').click()

                await page.wait_for_url('**/my-tickets/')

            await self._run_in_browser(reverse('tickets:login'), callback)

        self.run_browser(scenario)

        created_user = User.objects.get(email=email)
        created_profile = created_user.profile
        self.assertEqual(created_user.first_name, 'Casey')
        self.assertEqual(created_profile.phone_number, '+14155550102')
        self.assertEqual(created_profile.role, UserProfile.Role.ATTENDEE)

    def test_phone_login_flow_reuses_existing_attendee(self):
        async def scenario():
            async def callback(page):
                await page.locator('[data-testid="phone-number-input"]').fill('4155550101')
                await page.get_by_role('button', name='Send code').click()

                await page.wait_for_url('**/login/verify/')
                await page.locator('[data-testid="otp-code-input"]').fill('000000')
                await page.get_by_role('button', name='Verify').click()

                await page.wait_for_url('**/my-tickets/')

            await self._run_in_browser(reverse('tickets:login'), callback)

        self.run_browser(scenario)

        self.assertEqual(
            UserProfile.objects.filter(phone_number='+14155550101').count(),
            1,
        )

    def test_ticket_purchase_flow_creates_completed_order(self):
        buyer_phone = '4155550103'
        buyer_email = 'buyer-browser@example.com'

        async def scenario():
            async def callback(page):
                await page.locator('.js-buy-trigger').first.click()

                await page.locator('#modal-phone-input').wait_for(state='visible')
                await page.locator('#modal-phone-input').fill(buyer_phone)
                await page.locator('#modal-phone-btn').click()

                await page.locator('#modal-otp-input').wait_for(state='visible')
                await page.locator('#modal-otp-input').fill('000000')
                await page.locator('#modal-otp-btn').click()

                await page.locator('#modal-first-name').wait_for(state='visible')
                await page.locator('#modal-first-name').fill('Parker')
                await page.locator('#modal-last-name').fill('Buyer')
                await page.locator('#modal-email').fill(buyer_email)
                await page.locator('#modal-profile-btn').click()

                await page.locator('#buyModal').wait_for(state='visible')
                await page.locator('#buyModal input[type="number"]').first.fill('1')
                await page.locator('#buyModal button[type="submit"]').click()

                await page.wait_for_url('**/checkout/')
                await page.locator('#pay-btn').click()
                await page.wait_for_url('**/my-tickets/')

            await self._run_in_browser(
                reverse('tickets:public_event_buy', args=[self.event.public_id]),
                callback,
            )

        self.run_browser(scenario)

        session = StripeCheckoutSession.objects.get(buyer_email=buyer_email)
        self.assertEqual(session.status, StripeCheckoutSession.Status.COMPLETED)
        order = TicketOrder.objects.get(stripe_checkout_session=session)
        self.assertEqual(order.total_amount, Decimal('25.00'))
        self.assertEqual(order.customer.email, buyer_email)
        self.assertEqual(Ticket.objects.filter(ticket_order=order).count(), 1)


class E2ETestGuardTests(TestCase):
    def test_test_fulfill_endpoint_is_unavailable_when_flag_disabled(self):
        org = Organization.objects.create(name='Guard Org', slug='guard-org')
        venue = Venue.objects.create(organization=org, name='Guard Venue', city='LA')
        event = Event.objects.create(
            organization=org,
            name='Guard Event',
            venue=venue,
            start_date=date.today() + timedelta(days=7),
            ticketing_type='direct',
            status='live',
        )

        with override_settings(E2E_TEST_MODE=False):
            response = self.client.post(
                reverse('tickets:e2e_complete_payment', args=[event.public_id]),
                content_type='application/json',
                data='{}',
            )

        self.assertEqual(response.status_code, 404)
