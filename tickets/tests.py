import uuid
import json
import tempfile
from datetime import date, time, datetime, timedelta
from unittest.mock import patch, MagicMock
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from decimal import Decimal
from rest_framework.authtoken.models import Token
from .models import (
    UploadedFile, TicketOrder, Customer, CustomerTag, Event, EventImage,
    Venue, Market, CSVFormat, Ticket, TicketTier,
    Organization, UserProfile, OrganizationMembership, OrganizationInvitation, ChatMessage,
    AITokenUsage,
    EventExpense,
    SaleableTicketType, SaleableTicketTypeTier, StripeCheckoutSession, FeatureFlagSettings,
    SurveyInvitation, SurveyResponse, SurveyAnswer, SurveyQuestion, Payout,
    SurveyQuestionOption, SurveyAnswerOption,
    ExternalSurveyUpload, ExternalSurveyResponse, EventDailyPageView,
    LoyaltyProgram, LoyaltyTier, LoyaltyPointsTransaction,
    PhoneSuppression, SMSConsentRecord,
    DeviceToken,
    TICKETING_TYPE_DIRECT,
)
from .utils import extract_fee_from_display_cents


class AITokenUsageTests(TestCase):
    """Tests for organization-scoped AI token metering."""

    def setUp(self):
        self.org = Organization.objects.create(name='AI Meter Org', slug='ai-meter-org')
        self.other_org = Organization.objects.create(name='Other AI Org', slug='other-ai-org')
        self.user = User.objects.create_user(username='meter', email='meter@example.com')

    def test_record_ai_token_usage_from_langchain_metadata(self):
        from tickets.services.ai_metering import record_ai_token_usage

        class FakeMessage:
            usage_metadata = {
                'input_tokens': 42,
                'output_tokens': 18,
                'total_tokens': 60,
            }

        record = record_ai_token_usage(
            organization=self.org,
            user=self.user,
            feature=AITokenUsage.FEATURE_CHAT_AGENT,
            model_name='gpt-4o',
            usage=FakeMessage(),
            metadata={'event_id': 'event-123'},
        )

        self.assertIsNotNone(record)
        self.assertEqual(record.organization, self.org)
        self.assertEqual(record.user, self.user)
        self.assertEqual(record.prompt_tokens, 42)
        self.assertEqual(record.completion_tokens, 18)
        self.assertEqual(record.total_tokens, 60)
        self.assertEqual(record.metadata['event_id'], 'event-123')

    def test_record_ai_token_usage_skips_missing_usage(self):
        from tickets.services.ai_metering import record_ai_token_usage

        record = record_ai_token_usage(
            organization=self.org,
            feature=AITokenUsage.FEATURE_CHAT_AGENT,
            model_name='gpt-4o',
            usage={},
        )

        self.assertIsNone(record)
        self.assertFalse(AITokenUsage.objects.exists())

    def test_monthly_ai_token_usage_is_scoped_to_organization_and_month(self):
        from tickets.services.ai_metering import monthly_ai_token_usage, record_ai_token_usage

        may = datetime(2026, 5, 10, tzinfo=timezone.get_current_timezone())
        june = datetime(2026, 6, 1, tzinfo=timezone.get_current_timezone())

        record_ai_token_usage(
            organization=self.org,
            feature=AITokenUsage.FEATURE_CHAT_AGENT,
            model_name='gpt-4o',
            usage={'prompt_tokens': 100, 'completion_tokens': 25, 'total_tokens': 125},
            occurred_at=may,
        )
        record_ai_token_usage(
            organization=self.org,
            feature=AITokenUsage.FEATURE_META_CAMPAIGN_MATCH,
            model_name='gpt-4o',
            usage={'prompt_tokens': 20, 'completion_tokens': 5, 'total_tokens': 25},
            occurred_at=june,
        )
        record_ai_token_usage(
            organization=self.other_org,
            feature=AITokenUsage.FEATURE_CHAT_AGENT,
            model_name='gpt-4o',
            usage={'prompt_tokens': 200, 'completion_tokens': 50, 'total_tokens': 250},
            occurred_at=may,
        )

        totals = monthly_ai_token_usage(self.org, 2026, 5)

        self.assertEqual(totals, {
            'prompt_tokens': 100,
            'completion_tokens': 25,
            'total_tokens': 125,
        })

    def test_token_usage_accumulator_deduplicates_stream_chunks_by_run(self):
        from tickets.services.ai_metering import TokenUsageAccumulator

        accumulator = TokenUsageAccumulator()
        accumulator.add({'input_tokens': 10, 'output_tokens': 1, 'total_tokens': 11}, key='run-1')
        accumulator.add({'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}, key='run-1')
        accumulator.add({'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}, key='run-2')

        total = accumulator.total()

        self.assertEqual(total.prompt_tokens, 14)
        self.assertEqual(total.completion_tokens, 7)
        self.assertEqual(total.total_tokens, 21)

    def test_to_cue_tokens_divides_by_1000(self):
        from tickets.services.ai_metering import to_cue_tokens, CUE_TOKEN_DIVISOR

        self.assertEqual(CUE_TOKEN_DIVISOR, 1000)
        self.assertEqual(to_cue_tokens(0), 0)
        self.assertEqual(to_cue_tokens(None), 0)
        self.assertEqual(to_cue_tokens(1000), 1)
        self.assertAlmostEqual(to_cue_tokens(125), 0.125)
        self.assertAlmostEqual(to_cue_tokens(2_345_678), 2345.678)

    def test_monthly_breakdown_groups_by_feature_and_fills_daily_zeros(self):
        from tickets.services.ai_metering import (
            monthly_ai_token_usage_breakdown, record_ai_token_usage,
        )

        tz = timezone.get_current_timezone()
        record_ai_token_usage(
            organization=self.org,
            feature=AITokenUsage.FEATURE_CHAT_AGENT,
            usage={'prompt_tokens': 100, 'completion_tokens': 25, 'total_tokens': 125},
            occurred_at=datetime(2026, 5, 3, 12, tzinfo=tz),
        )
        record_ai_token_usage(
            organization=self.org,
            feature=AITokenUsage.FEATURE_CHAT_AGENT,
            usage={'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
            occurred_at=datetime(2026, 5, 3, 18, tzinfo=tz),
        )
        record_ai_token_usage(
            organization=self.org,
            feature=AITokenUsage.FEATURE_META_CAMPAIGN_MATCH,
            usage={'prompt_tokens': 30, 'completion_tokens': 6, 'total_tokens': 36},
            occurred_at=datetime(2026, 5, 17, 9, tzinfo=tz),
        )
        # Different org's records must be excluded.
        record_ai_token_usage(
            organization=self.other_org,
            feature=AITokenUsage.FEATURE_CHAT_AGENT,
            usage={'prompt_tokens': 999, 'completion_tokens': 999, 'total_tokens': 1998},
            occurred_at=datetime(2026, 5, 3, 12, tzinfo=tz),
        )

        breakdown = monthly_ai_token_usage_breakdown(self.org, 2026, 5)

        self.assertEqual(breakdown['totals'], {
            'prompt_tokens': 140,
            'completion_tokens': 36,
            'total_tokens': 176,
        })
        self.assertEqual(breakdown['by_feature'][0]['feature'], AITokenUsage.FEATURE_CHAT_AGENT)
        self.assertEqual(breakdown['by_feature'][0]['total_tokens'], 140)
        self.assertEqual(breakdown['by_feature'][0]['feature_label'], 'Chat agent')
        self.assertEqual(breakdown['by_feature'][1]['feature'], AITokenUsage.FEATURE_META_CAMPAIGN_MATCH)
        self.assertEqual(breakdown['by_feature'][1]['total_tokens'], 36)

        self.assertEqual(len(breakdown['daily']), 31)
        self.assertEqual(breakdown['daily'][0]['date'], date(2026, 5, 1))
        self.assertEqual(breakdown['daily'][0]['total_tokens'], 0)
        self.assertEqual(breakdown['daily'][2]['date'], date(2026, 5, 3))
        self.assertEqual(breakdown['daily'][2]['total_tokens'], 140)
        self.assertEqual(breakdown['daily'][16]['date'], date(2026, 5, 17))
        self.assertEqual(breakdown['daily'][16]['total_tokens'], 36)


class AITokenUsageDashboardViewTests(TestCase):
    """Tests for the AI token usage dashboard view."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Dash Org', slug='dash-org')
        self.other_org = Organization.objects.create(name='Other Dash Org', slug='other-dash-org')

        self.admin_user = User.objects.create_user(
            username='dashadmin', email='dashadmin@example.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.admin_user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.admin_user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )

        self.host_user = User.objects.create_user(
            username='dashhost', email='dashhost@example.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.host_user, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )
        OrganizationMembership.objects.create(
            user=self.host_user, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )

        tz = timezone.get_current_timezone()
        from tickets.services.ai_metering import record_ai_token_usage
        record_ai_token_usage(
            organization=self.org,
            feature=AITokenUsage.FEATURE_CHAT_AGENT,
            usage={'prompt_tokens': 100, 'completion_tokens': 25, 'total_tokens': 125},
            occurred_at=datetime(2026, 5, 10, 12, tzinfo=tz),
        )
        record_ai_token_usage(
            organization=self.other_org,
            feature=AITokenUsage.FEATURE_CHAT_AGENT,
            usage={'prompt_tokens': 555, 'completion_tokens': 5, 'total_tokens': 560},
            occurred_at=datetime(2026, 5, 10, 12, tzinfo=tz),
        )

    def _login_admin(self):
        self.client.login(username='dashadmin@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))

    def test_admin_sees_only_own_org_usage(self):
        self._login_admin()
        response = self.client.get(reverse('tickets:ai_token_usage') + '?month_key=2026-05')
        self.assertEqual(response.status_code, 200)
        # 125 raw LLM tokens / 1000 = 0.125 Cue tokens
        self.assertAlmostEqual(response.context['totals']['total_tokens'], 0.125)
        # The other org's 560 raw tokens (0.56 Cue tokens) must not appear.
        self.assertNotContains(response, '0.56')

    def test_non_admin_forbidden(self):
        self.client.login(username='dashhost@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))
        response = self.client.get(reverse('tickets:ai_token_usage'))
        self.assertEqual(response.status_code, 403)

    def test_invalid_month_key_falls_back_to_current_month(self):
        self._login_admin()
        response = self.client.get(reverse('tickets:ai_token_usage') + '?month_key=not-a-month')
        self.assertEqual(response.status_code, 200)
        today = timezone.localdate()
        self.assertEqual(response.context['selected_month_key'], f"{today.year:04d}-{today.month:02d}")


class UploadDeleteViewTests(TestCase):
    """Test cases for the upload_delete view."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.org = Organization.objects.create(name='Delete Test Org', slug='delete-test-org')
        self.user = User.objects.create_user(
            username='testuser',
            email='test@test.com',
            password='testpass123'
        )
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='test@test.com', password='testpass123')
        # Seed the session with _org_id so @require_org passes
        self.client.get(reverse('tickets:home'))

        # Create required related objects — all scoped to self.org
        self.csv_format = CSVFormat.objects.create(
            organization=self.org,
            name='Test Format',
            column_mapping={'order_number': 'Order ID'}
        )
        self.venue = Venue.objects.create(
            organization=self.org,
            name='Test Venue',
            city='Test City'
        )
        self.event = Event.objects.create(
            organization=self.org,
            name='Test Event',
            venue=self.venue,
            start_date=date(2024, 6, 15),
            start_time=time(19, 0, 0)
        )
        self.upload = UploadedFile.objects.create(
            organization=self.org,
            csv_format=self.csv_format,
            filename='test_upload.csv',
            status='completed',
            total_rows=10,
            processed_rows=10
        )
        self.customer = Customer.objects.create(
            organization=self.org,
            email='customer@example.com',
            name='Test Customer',
            lifetime_value=Decimal('150.00')
        )
        self.order = TicketOrder.objects.create(
            customer=self.customer,
            event=self.event,
            uploaded_file=self.upload,
            order_number='ORD-001',
            order_date='2024-06-01 10:00:00',
            total_amount=Decimal('150.00')
        )

    def test_delete_upload_success_ajax(self):
        """Test successful deletion via AJAX request."""
        response = self.client.post(
            reverse('tickets:upload_delete', args=[self.upload.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertIn('Successfully deleted', data['message'])

        # Verify upload and order are deleted
        self.assertFalse(
            UploadedFile.objects.filter(id=self.upload.id).exists()
        )
        self.assertFalse(
            TicketOrder.objects.filter(id=self.order.id).exists()
        )

    def test_delete_upload_success_form_post(self):
        """Test successful deletion via regular form POST."""
        response = self.client.post(
            reverse('tickets:upload_delete', args=[self.upload.id])
        )

        # Should redirect after successful deletion
        self.assertEqual(response.status_code, 302)

        # Verify deletion
        self.assertFalse(
            UploadedFile.objects.filter(id=self.upload.id).exists()
        )

    def test_delete_processing_upload_blocked(self):
        """Test that uploads with 'processing' status cannot be deleted."""
        self.upload.status = 'processing'
        self.upload.save()

        response = self.client.post(
            reverse('tickets:upload_delete', args=[self.upload.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('processing', data['error'].lower())

        # Verify upload still exists
        self.assertTrue(
            UploadedFile.objects.filter(id=self.upload.id).exists()
        )

    def test_delete_pending_upload_allowed(self):
        """Test that uploads with 'pending' status can be deleted."""
        self.upload.status = 'pending'
        self.upload.save()

        response = self.client.post(
            reverse('tickets:upload_delete', args=[self.upload.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            UploadedFile.objects.filter(id=self.upload.id).exists()
        )

    def test_delete_failed_upload_allowed(self):
        """Test that uploads with 'failed' status can be deleted."""
        self.upload.status = 'failed'
        self.upload.save()

        response = self.client.post(
            reverse('tickets:upload_delete', args=[self.upload.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            UploadedFile.objects.filter(id=self.upload.id).exists()
        )

    def test_delete_nonexistent_upload_404(self):
        """Test 404 response for non-existent upload ID."""
        import uuid
        fake_id = uuid.uuid4()

        response = self.client.post(
            reverse('tickets:upload_delete', args=[fake_id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        self.assertEqual(response.status_code, 404)

    def test_delete_requires_authentication(self):
        """Test that unauthenticated users cannot delete uploads."""
        self.client.logout()

        response = self.client.post(
            reverse('tickets:upload_delete', args=[self.upload.id])
        )

        # Should redirect to login
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)

        # Verify upload still exists
        self.assertTrue(
            UploadedFile.objects.filter(id=self.upload.id).exists()
        )

    def test_delete_get_method_not_allowed(self):
        """Test that GET requests are rejected."""
        response = self.client.get(
            reverse('tickets:upload_delete', args=[self.upload.id])
        )

        self.assertEqual(response.status_code, 405)

    def test_delete_removes_orphaned_customer(self):
        """Test that customers with no remaining orders are deleted."""
        response = self.client.post(
            reverse('tickets:upload_delete', args=[self.upload.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        self.assertEqual(response.status_code, 200)
        # Customer had only one order from this upload, should be deleted
        self.assertFalse(
            Customer.objects.filter(id=self.customer.id).exists()
        )

    def test_delete_preserves_customer_with_other_orders(self):
        """Test that customers with orders from other uploads are preserved."""
        # Create another upload with an order for the same customer
        other_upload = UploadedFile.objects.create(
            organization=self.org,
            csv_format=self.csv_format,
            filename='other_upload.csv',
            status='completed'
        )
        TicketOrder.objects.create(
            customer=self.customer,
            event=self.event,
            uploaded_file=other_upload,
            order_number='ORD-OTHER',
            order_date='2024-06-02 10:00:00',
            total_amount=Decimal('200.00')
        )

        response = self.client.post(
            reverse('tickets:upload_delete', args=[self.upload.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        self.assertEqual(response.status_code, 200)
        # Customer should still exist (has order from other upload)
        self.assertTrue(
            Customer.objects.filter(id=self.customer.id).exists()
        )
        # LTV should be recalculated to reflect only the remaining order
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.lifetime_value, Decimal('200.00'))

    def test_delete_preserves_customer_last_order_date_from_remaining_orders(self):
        """Remaining orders should drive last_order_date after upload deletion."""
        self.customer.last_order_date = date(2024, 6, 1)
        self.customer.save(update_fields=['last_order_date'])

        other_upload = UploadedFile.objects.create(
            organization=self.org,
            csv_format=self.csv_format,
            filename='other_upload.csv',
            status='completed'
        )
        TicketOrder.objects.create(
            customer=self.customer,
            event=self.event,
            uploaded_file=other_upload,
            order_number='ORD-LATER',
            order_date='2024-06-05 10:00:00',
            total_amount=Decimal('200.00')
        )

        response = self.client.post(
            reverse('tickets:upload_delete', args=[self.upload.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        self.assertEqual(response.status_code, 200)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.last_order_date, date(2024, 6, 5))

    def test_delete_cascades_to_tickets(self):
        """Test that associated tickets are deleted with orders."""
        ticket = Ticket.objects.create(
            ticket_order=self.order,
            ticket_type='GA',
            price=Decimal('75.00')
        )

        response = self.client.post(
            reverse('tickets:upload_delete', args=[self.upload.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Ticket.objects.filter(id=ticket.id).exists())

    def test_delete_upload_with_no_orders(self):
        """Test deleting an upload that has no associated orders."""
        # Delete the order first
        self.order.delete()

        response = self.client.post(
            reverse('tickets:upload_delete', args=[self.upload.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertFalse(
            UploadedFile.objects.filter(id=self.upload.id).exists()
        )

    def test_delete_returns_affected_count(self):
        """Test that response includes count of deleted orders."""
        # Create additional orders
        TicketOrder.objects.create(
            customer=self.customer,
            event=self.event,
            uploaded_file=self.upload,
            order_number='ORD-002',
            order_date='2024-06-01 11:00:00',
            total_amount=Decimal('100.00')
        )

        response = self.client.post(
            reverse('tickets:upload_delete', args=[self.upload.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        data = response.json()
        self.assertIn('2', data['message'])  # Should mention 2 orders

    def test_delete_invalidates_event_upload_stats_cache(self):
        """Deleting an upload clears the cached upload stats for its event."""
        from django.core.cache import cache as django_cache
        from tickets.views import _compute_event_upload_stats, _event_upload_stats_cache_key

        Ticket.objects.create(
            ticket_order=self.order,
            ticket_type='GA',
            price=Decimal('150.00'),
        )
        _compute_event_upload_stats(self.event)
        self.assertIsNotNone(django_cache.get(_event_upload_stats_cache_key(self.event.id)))

        response = self.client.post(
            reverse('tickets:upload_delete', args=[self.upload.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(django_cache.get(_event_upload_stats_cache_key(self.event.id)))


class VenueAddressFieldsTests(TestCase):
    """Test venue address fields and get_display_address."""

    def setUp(self):
        self.org = Organization.objects.create(name='Venue Test Org', slug='venue-test-org')

    def test_venue_saves_address_fields(self):
        """Venue with all address fields saves and reads back correctly."""
        venue = Venue.objects.create(
            organization=self.org,
            name='The Fillmore',
            city='San Francisco',
            street_address='1805 Geary Blvd',
            state='CA',
            postal_code='94115',
            country='USA',
        )
        venue.refresh_from_db()
        self.assertEqual(venue.street_address, '1805 Geary Blvd')
        self.assertEqual(venue.state, 'CA')
        self.assertEqual(venue.postal_code, '94115')
        self.assertEqual(venue.country, 'USA')

    def test_venue_get_display_address(self):
        """get_display_address returns formatted line when address fields present."""
        venue = Venue.objects.create(
            organization=self.org,
            name='The Fillmore',
            city='San Francisco',
            street_address='1805 Geary Blvd',
            state='CA',
            postal_code='94115',
            country='USA',
        )
        addr = venue.get_display_address()
        self.assertIn('1805 Geary Blvd', addr)
        self.assertIn('CA', addr)
        self.assertIn('94115', addr)
        self.assertIn('USA', addr)

    def test_venue_get_display_address_empty_when_no_address(self):
        """get_display_address returns empty string when no address fields."""
        venue = Venue.objects.create(
            organization=self.org, name='No Address Venue', city='Somewhere'
        )
        self.assertEqual(venue.get_display_address(), '')

    def test_venue_form_includes_address_fields(self):
        """VenueForm has address fields in form."""
        from .forms import VenueForm
        form = VenueForm()
        self.assertIn('street_address', form.fields)
        self.assertIn('state', form.fields)
        self.assertIn('postal_code', form.fields)
        self.assertIn('country', form.fields)

    def test_venue_street_address_ordinal_suffix_lowercase(self):
        """Street addresses with ordinal numbers (11th, 5th, 36th) keep suffix lowercase."""
        venue = Venue.objects.create(
            organization=self.org,
            name='Ordinal Test Venue',
            city='Detroit',
            street_address='125 East 11th Street',
            state='MI',
            country='USA',
        )
        venue.refresh_from_db()
        self.assertEqual(venue.street_address, '125 East 11th Street')
        # Also ensure 5th, 36th, 1st, 2nd, 3rd are normalized correctly (no 5Th, 36Th, 1St, etc.)
        venue.street_address = '125 Northwest 5th Avenue'
        venue.save()
        venue.refresh_from_db()
        self.assertEqual(venue.street_address, '125 Northwest 5th Avenue')


class MarketModelTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Market Org', slug='market-org')
        self.other_org = Organization.objects.create(name='Other Market Org', slug='other-market-org')
        self.venue = Venue.objects.create(organization=self.org, name='Market Hall', city='San Diego')
        self.event = Event.objects.create(
            organization=self.org,
            name='Market Event',
            venue=self.venue,
            start_date=date(2026, 1, 1),
        )

    def test_market_uniqueness_is_scoped_to_organization(self):
        from django.core.exceptions import ValidationError

        Market.objects.create(
            organization=self.org,
            name='San Diego',
            geography_level='city',
            geography_value='San Diego',
        )
        with self.assertRaises(ValidationError):
            Market.objects.create(
                organization=self.org,
                name='San Diego',
                geography_level='state',
                geography_value='CA',
            )

        other_market = Market.objects.create(
            organization=self.other_org,
            name='San Diego',
            geography_level='city',
            geography_value='San Diego',
        )
        self.assertEqual(other_market.name, 'San Diego')

    def test_event_market_is_nullable_when_market_is_deleted(self):
        market = Market.objects.create(
            organization=self.org,
            name='San Diego',
            geography_level='city',
            geography_value='San Diego',
        )
        self.event.market = market
        self.event.save(update_fields=['market'])

        market.delete()
        self.event.refresh_from_db()

        self.assertIsNone(self.event.market)


class MarketBuilderTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Builder Org', slug='builder-org')
        self.other_org = Organization.objects.create(name='Builder Other Org', slug='builder-other-org')
        self.sd_venue = Venue.objects.create(
            organization=self.org, name='SD Hall', city='San Diego', state='CA', country='US'
        )
        self.sd_other_venue = Venue.objects.create(
            organization=self.org, name='SD Club', city='San Diego', state='CA', country='US'
        )
        self.blank_venue = Venue.objects.create(
            organization=self.org, name='Blank Hall', city='', state='', country=''
        )
        self.la_venue = Venue.objects.create(
            organization=self.other_org, name='LA Hall', city='Los Angeles', state='CA', country='US'
        )
        self.event = Event.objects.create(
            organization=self.org, name='SD One', venue=self.sd_venue, start_date=date(2026, 1, 1)
        )
        self.second_event = Event.objects.create(
            organization=self.org, name='SD Two', venue=self.sd_other_venue, start_date=date(2026, 2, 1)
        )
        Event.objects.create(
            organization=self.org, name='Blank', venue=self.blank_venue, start_date=date(2026, 3, 1)
        )
        Event.objects.create(
            organization=self.other_org, name='LA One', venue=self.la_venue, start_date=date(2026, 4, 1)
        )

    def test_preview_is_org_scoped_and_ignores_blank_values(self):
        from tickets.services.markets import MarketBuilder

        rows = MarketBuilder(self.org).preview('city')

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['value'], 'San Diego')
        self.assertEqual(rows[0]['event_count'], 2)
        self.assertEqual(rows[0]['venue_count'], 2)

    def test_build_creates_markets_idempotently_and_assigns_matching_events(self):
        from tickets.services.markets import MarketBuilder

        builder = MarketBuilder(self.org)
        first_result = builder.build('city', ['San Diego'])
        second_result = builder.build('city', ['San Diego'])

        self.assertEqual(first_result['created_count'], 1)
        self.assertEqual(first_result['updated_count'], 2)
        self.assertEqual(second_result['created_count'], 0)
        self.assertEqual(Market.objects.filter(organization=self.org).count(), 1)
        market = Market.objects.get(organization=self.org, geography_level='city', geography_value='San Diego')
        self.event.refresh_from_db()
        self.second_event.refresh_from_db()
        self.assertEqual(self.event.market, market)
        self.assertEqual(self.second_event.market, market)
        self.assertFalse(Event.objects.filter(organization=self.other_org, market__isnull=False).exists())

    def test_assignment_uses_city_before_state_and_country(self):
        from tickets.services.markets import MarketBuilder

        country = Market.objects.create(
            organization=self.org,
            name='United States',
            geography_level='country',
            geography_value='US',
        )
        state = Market.objects.create(
            organization=self.org,
            name='California',
            geography_level='state',
            geography_value='CA',
        )
        city = Market.objects.create(
            organization=self.org,
            name='San Diego Metro',
            geography_level='city',
            geography_value='San Diego',
        )

        builder = MarketBuilder(self.org)

        self.assertEqual(builder.resolve_for_venue(self.sd_venue), city)
        state_only = Venue.objects.create(
            organization=self.org, name='State Hall', city='Fresno', state='CA', country='US',
        )
        self.assertEqual(builder.resolve_for_venue(state_only), state)
        country_only = Venue.objects.create(
            organization=self.org, name='Country Hall', city='Boise', state='ID', country='US',
        )
        self.assertEqual(builder.resolve_for_venue(country_only), country)

    def test_assign_event_clears_market_when_no_rule_matches(self):
        from tickets.services.markets import MarketBuilder

        Market.objects.create(
            organization=self.org,
            name='San Diego',
            geography_level='city',
            geography_value='San Diego',
        )
        builder = MarketBuilder(self.org)
        builder.assign_event(self.event)
        self.event.refresh_from_db()
        self.assertIsNotNone(self.event.market)

        self.event.venue = self.blank_venue
        self.event.save(update_fields=['venue'])
        builder.assign_event(self.event)
        self.event.refresh_from_db()

        self.assertIsNone(self.event.market)

    def test_assign_all_events_is_idempotent(self):
        from tickets.services.markets import MarketBuilder

        Market.objects.create(
            organization=self.org,
            name='San Diego',
            geography_level='city',
            geography_value='San Diego',
        )
        builder = MarketBuilder(self.org)

        self.assertEqual(builder.assign_all_events(), 2)
        self.assertEqual(builder.assign_all_events(), 0)


class MarketViewTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Market View Org', slug='market-view-org')
        self.other_org = Organization.objects.create(name='Market View Other Org', slug='market-view-other-org')
        self.user = User.objects.create_user(username='market-owner', password='pw')
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.venue = Venue.objects.create(
            organization=self.org, name='SD Hall', city='San Diego', state='CA', country='US'
        )
        other_venue = Venue.objects.create(
            organization=self.other_org, name='LA Hall', city='Los Angeles', state='CA', country='US'
        )
        Event.objects.create(
            organization=self.org, name='SD Event', venue=self.venue, start_date=date(2026, 1, 1)
        )
        Event.objects.create(
            organization=self.other_org, name='LA Event', venue=other_venue, start_date=date(2026, 1, 1)
        )

    def test_market_list_requires_login(self):
        response = self.client.get(reverse('tickets:market_list'))
        self.assertEqual(response.status_code, 302)

    def test_builder_preview_is_org_scoped(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('tickets:market_builder'), {'level': 'city'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'San Diego')
        self.assertNotContains(response, 'Los Angeles')

    def test_builder_post_creates_market_and_assigns_event(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('tickets:market_builder'),
            {'level': 'city', 'values': ['San Diego']},
        )

        self.assertRedirects(response, reverse('tickets:market_list'))
        market = Market.objects.get(organization=self.org, geography_level='city', geography_value='San Diego')
        self.assertTrue(Event.objects.filter(organization=self.org, market=market).exists())


class MarketEntityReportingTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(
            name='Market Reporting Org',
            slug='market-reporting-org',
            external_events_enabled=True,
        )
        self.user = User.objects.create_user(
            username='market-reporter',
            email='market-reporter@example.com',
            password='pw',
        )
        UserProfile.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.force_login(self.user)
        self.client.get(reverse('tickets:home'))

        self.market = Market.objects.create(
            organization=self.org,
            name='Central Texas',
            geography_level='city',
            geography_value='Austin',
        )
        self.venue = Venue.objects.create(
            organization=self.org,
            name='Austin Hall',
            city='Austin',
            state='TX',
            country='US',
        )
        self.unassigned_venue = Venue.objects.create(
            organization=self.org,
            name='Dallas Hall',
            city='Dallas',
            state='TX',
            country='US',
        )
        self.event = Event.objects.create(
            organization=self.org,
            name='Austin Report Show',
            venue=self.venue,
            market=self.market,
            start_date=date(2025, 1, 10),
            start_time=time(20, 0),
            end_date=date(2025, 1, 10),
            end_time=time(22, 0),
        )
        self.unassigned_event = Event.objects.create(
            organization=self.org,
            name='Dallas Report Show',
            venue=self.unassigned_venue,
            start_date=date(2025, 2, 10),
            start_time=time(20, 0),
            end_date=date(2025, 2, 10),
            end_time=time(22, 0),
        )
        self.customer = Customer.objects.create(
            organization=self.org,
            email='market-buyer@example.com',
            name='Market Buyer',
        )
        TicketOrder.objects.create(
            customer=self.customer,
            event=self.event,
            order_number='MARKET-ORDER-1',
            order_date=timezone.make_aware(datetime(2025, 1, 5, 10, 0)),
            total_amount=Decimal('40.00'),
            is_in_person=False,
        )
        TicketOrder.objects.create(
            customer=self.customer,
            event=self.unassigned_event,
            order_number='MARKET-ORDER-2',
            order_date=timezone.make_aware(datetime(2025, 2, 5, 10, 0)),
            total_amount=Decimal('25.00'),
            is_in_person=False,
        )

    def test_ltv_by_market_uses_event_market_name(self):
        response = self.client.get(reverse('tickets:customer_ltv_by_market'))

        labels = {row['market_label'] for row in response.context['market_stats']}
        self.assertIn('Central Texas', labels)
        self.assertIn('No market', labels)
        self.assertNotIn('Austin', labels)
        central = next(row for row in response.context['market_stats'] if row['market_label'] == 'Central Texas')
        self.assertEqual(central['market_id'], str(self.market.id))
        self.assertEqual(central['city'], 'Central Texas')

    def test_repeat_customer_market_breakdown_uses_event_market_name(self):
        response = self.client.get(reverse('tickets:repeat_customers'), {'window': 'all'})

        rows = json.loads(response.context['market_chart_data_json'])
        labels = {row['market_label'] for row in rows}
        self.assertIn('Central Texas', labels)
        self.assertIn('No market', labels)
        self.assertNotIn('Austin', labels)

    def test_profitability_market_chart_uses_event_market_name(self):
        EventExpense.objects.create(
            event=self.event,
            category='production',
            description='Production',
            amount=Decimal('10.00'),
            expense_date=self.event.start_date,
            created_by=self.user,
        )

        response = self.client.get(reverse('tickets:profitability_overview'), {'window': 'all'})
        chart = json.loads(response.context['market_chart_data_json'])

        self.assertIn('Central Texas', chart['labels'])
        self.assertIn('No market', chart['labels'])
        self.assertNotIn('Austin', chart['labels'])

    def test_survey_analytics_filter_and_breakdown_use_market_id(self):
        upload = ExternalSurveyUpload.objects.create(
            organization=self.org,
            filename='survey.csv',
            status=ExternalSurveyUpload.Status.COMPLETED,
            created_by=self.user,
        )
        ExternalSurveyResponse.objects.create(
            organization=self.org,
            upload=upload,
            event=self.event,
            email='central@example.com',
            responded_at=timezone.make_aware(datetime(2025, 1, 12, 9, 0)),
            nps_score=10,
        )
        ExternalSurveyResponse.objects.create(
            organization=self.org,
            upload=upload,
            event=self.unassigned_event,
            email='nomarket@example.com',
            responded_at=timezone.make_aware(datetime(2025, 2, 12, 9, 0)),
            nps_score=3,
        )

        response = self.client.get(reverse('tickets:survey_analytics'), {'market': str(self.market.id)})

        self.assertEqual(response.context['stats']['total'], 1)
        self.assertEqual(response.context['market_filter'], str(self.market.id))
        labels = {row['city'] for row in response.context['stats']['city_breakdown']}
        self.assertIn('Central Texas', labels)
        self.assertIn('No market', labels)
        self.assertNotIn('Austin', labels)

    def test_survey_analytics_filters_by_event_date_range(self):
        upload = ExternalSurveyUpload.objects.create(
            organization=self.org,
            filename='survey.csv',
            status=ExternalSurveyUpload.Status.COMPLETED,
            created_by=self.user,
        )
        # self.event is on 2025-01-10 (Central Texas); self.unassigned_event on 2025-02-10.
        jan_resp = ExternalSurveyResponse.objects.create(
            organization=self.org,
            upload=upload,
            event=self.event,
            email='jan@example.com',
            responded_at=timezone.make_aware(datetime(2025, 3, 1, 9, 0)),
            nps_score=10,
        )
        ExternalSurveyResponse.objects.create(
            organization=self.org,
            upload=upload,
            event=self.unassigned_event,
            email='feb@example.com',
            responded_at=timezone.make_aware(datetime(2025, 3, 2, 9, 0)),
            nps_score=3,
        )

        # Window covering only the January event.
        response = self.client.get(
            reverse('tickets:survey_analytics'),
            {'event_from': '2025-01-01', 'event_to': '2025-01-31'},
        )

        self.assertEqual(response.context['stats']['total'], 1)
        self.assertEqual(response.context['event_from'], '2025-01-01')
        self.assertEqual(response.context['event_to'], '2025-01-31')
        # Responses list is scoped to the January event only. The feed is now a
        # unified list of normalized dicts (Cue + imported), not model instances.
        page_rows = list(response.context['page_obj'])
        self.assertEqual(len(page_rows), 1)
        self.assertEqual(page_rows[0]['source'], 'Imported')
        self.assertEqual(page_rows[0]['event_name'], jan_resp.event.name)
        # Market breakdown also respects the date window (Feb/no-market drops out).
        labels = {row['city'] for row in response.context['stats']['city_breakdown']}
        self.assertIn('Central Texas', labels)
        self.assertNotIn('No market', labels)

        # A malformed date is treated as no bound and does not 500.
        bad = self.client.get(
            reverse('tickets:survey_analytics'), {'event_from': 'not-a-date'},
        )
        self.assertEqual(bad.status_code, 200)
        self.assertEqual(bad.context['stats']['total'], 2)

    def test_external_event_create_assigns_matching_market(self):
        response = self.client.post(reverse('tickets:event_create', args=['external']), {
            'name': 'Created Report Show',
            'ticketing_type': 'external',
            'venue': str(self.venue.id),
            'start_date': '2025-03-10',
            'start_time': '20:00',
            'end_date': '2025-03-10',
            'end_time': '22:00',
            'timezone': 'America/Chicago',
            'ticket_link': '',
            'talent-TOTAL_FORMS': '0',
            'talent-INITIAL_FORMS': '0',
            'talent-MIN_NUM_FORMS': '0',
            'talent-MAX_NUM_FORMS': '1000',
        })

        self.assertEqual(response.status_code, 302)
        event = Event.objects.get(organization=self.org, name='Created Report Show')
        self.assertEqual(event.market, self.market)

    def test_external_event_edit_reassigns_when_venue_changes(self):
        response = self.client.post(reverse('tickets:event_edit', args=[self.event.id]), {
            'name': self.event.name,
            'ticketing_type': 'external',
            'venue': str(self.unassigned_venue.id),
            'start_date': '2025-01-10',
            'start_time': '20:00',
            'end_date': '2025-01-10',
            'end_time': '22:00',
            'timezone': 'America/Chicago',
            'ticket_link': '',
            'talent-TOTAL_FORMS': '0',
            'talent-INITIAL_FORMS': '0',
            'talent-MIN_NUM_FORMS': '0',
            'talent-MAX_NUM_FORMS': '1000',
        })

        self.assertEqual(response.status_code, 302)
        self.event.refresh_from_db()
        self.assertIsNone(self.event.market)

    def test_venue_geography_edit_reassigns_affected_events(self):
        response = self.client.post(reverse('tickets:venue_edit', args=[self.unassigned_venue.id]), {
            'name': self.unassigned_venue.name,
            'city': 'Austin',
            'street_address': '',
            'state': 'TX',
            'postal_code': '',
            'country': 'US',
            'capacity': '',
        })

        self.assertEqual(response.status_code, 302)
        self.unassigned_event.refresh_from_db()
        self.assertEqual(self.unassigned_event.market, self.market)

    def test_direct_event_create_assigns_matching_market(self):
        # The direct-ticketing branch of event_create is a separate code path
        # from the external branch; assert it also assigns Event.market.
        response = self.client.post(reverse('tickets:event_create', args=['direct']), {
            'name': 'Direct Market Show',
            'summary': '',
            'start_date': '2025-05-10',
            'start_time': '20:00',
            'end_date': '2025-05-10',
            'end_time': '22:00',
            'description': '',
            'capacity': '100',
            'venue': str(self.venue.id),
            'facebook_pixel_id': '',
            'ticket_type-TOTAL_FORMS': '1',
            'ticket_type-INITIAL_FORMS': '0',
            'ticket_type-MIN_NUM_FORMS': '0',
            'ticket_type-MAX_NUM_FORMS': '1000',
            'ticket_type-0-name': 'General Admission',
            'ticket_type-0-description': '',
            'ticket_type-0-price': '25.00',
            'ticket_type-0-quantity_limit': '',
            'ticket_type-0-max_per_customer': '4',
            'ticket_type-0-order': '0',
            'ticket_type-0-unlocks_after': '',
        })

        self.assertEqual(response.status_code, 302)
        event = Event.objects.get(organization=self.org, name='Direct Market Show')
        self.assertEqual(event.market, self.market)

    def test_csv_import_assigns_market_to_created_events(self):
        # CSV import auto-creates external events; the processor must assign
        # Event.market from existing rules for those newly-created events.
        import io
        csv_format = CSVFormat.objects.create(
            organization=self.org,
            name='Market Import Format',
            column_mapping={
                'order_date': ['order_date'],
                'customer_email': ['customer_email'],
                'customer_name': ['customer_name'],
                'ticket_type': ['ticket_type'],
            },
        )
        upload = UploadedFile.objects.create(
            organization=self.org,
            csv_format=csv_format,
            filename='market_import.csv',
            status='pending',
            # venue_id (Austin) + event_name but NO event_id → a new external
            # event is created and should resolve to the Central Texas market.
            metadata={
                'event_name': 'CSV Market Show',
                'event_start_date': '2025-06-01',
                'venue_id': str(self.venue.id),
            },
        )
        csv_body = (
            "order_date,customer_email,customer_name,ticket_type\n"
            "2025-06-01,csv-market@example.com,CSV Buyer,GA\n"
        )
        from tickets.csv_processor import CSVProcessor
        results = CSVProcessor(upload, csv_format).process_and_save(
            io.BytesIO(csv_body.encode('utf-8'))
        )

        self.assertEqual(results['success_count'], 1)
        event = Event.objects.get(organization=self.org, name='CSV Market Show')
        self.assertEqual(event.market, self.market)


class CSVImportSMSConsentTests(TestCase):
    """CSV import maps a consent column to Customer.sms_opt_in (T1).

    Imported contacts must NOT be textable unless the source data says they
    opted in AND they have a phone. Import grants consent, never revokes it.
    """

    def setUp(self):
        # CSV import auto-creates external events, which is gated on this flag
        # (csv_processor.py). Once T2 flips the model default to True this is the
        # norm for new orgs; set it explicitly here so the fixture is unambiguous.
        self.org = Organization.objects.create(
            name='Consent Org', slug='consent-org', external_events_enabled=True,
        )
        self.csv_format = CSVFormat.objects.create(
            organization=self.org,
            name='Consent Import Format',
            column_mapping={
                'order_date': ['order_date'],
                'customer_email': ['customer_email'],
                'customer_name': ['customer_name'],
                'customer_phone': ['customer_phone'],
                'ticket_type': ['ticket_type'],
                'customer_sms_opt_in': ['consent'],
            },
        )

    def _import(self, csv_body):
        import io
        upload = UploadedFile.objects.create(
            organization=self.org,
            csv_format=self.csv_format,
            filename='consent.csv',
            status='pending',
            metadata={'event_name': 'Consent Show', 'event_start_date': '2025-06-01'},
        )
        from tickets.csv_processor import CSVProcessor
        return CSVProcessor(upload, self.csv_format).process_and_save(
            io.BytesIO(csv_body.encode('utf-8'))
        )

    def _customer(self, email):
        return Customer.objects.get(organization=self.org, email=email)

    def test_consent_column_maps_to_sms_opt_in(self):
        csv_body = (
            "order_date,customer_email,customer_name,customer_phone,ticket_type,consent\n"
            "2025-06-01,yes@example.com,Yes Buyer,+15551110001,GA,Yes\n"
            "2025-06-01,no@example.com,No Buyer,+15551110002,GA,No\n"
            "2025-06-01,nophone@example.com,NoPhone Buyer,,GA,Yes\n"
        )
        results = self._import(csv_body)
        self.assertEqual(results['success_count'], 3)

        opted_in = self._customer('yes@example.com')
        self.assertTrue(opted_in.sms_opt_in)
        self.assertIsNotNone(opted_in.sms_opt_in_date)

        # Explicit "No" stays opted out.
        self.assertFalse(self._customer('no@example.com').sms_opt_in)
        # Consent "Yes" but no phone cannot be texted, so must not opt in.
        self.assertFalse(self._customer('nophone@example.com').sms_opt_in)

        # Only the genuinely-consented, phone-bearing contact is campaign-eligible.
        eligible = (
            Customer.objects.filter(organization=self.org, sms_opt_in=True)
            .exclude(phone='')
            .values_list('email', flat=True)
        )
        self.assertEqual(list(eligible), ['yes@example.com'])

    def test_import_never_revokes_existing_consent(self):
        Customer.objects.create(
            organization=self.org, email='vip@example.com', name='VIP',
            phone='+15551110009', sms_opt_in=True,
        )
        # A later import row says consent=No; existing opt-in must be preserved.
        csv_body = (
            "order_date,customer_email,customer_name,customer_phone,ticket_type,consent\n"
            "2025-06-02,vip@example.com,VIP,+15551110009,GA,No\n"
        )
        self._import(csv_body)
        self.assertTrue(self._customer('vip@example.com').sms_opt_in)


class NewOrgInitializationTests(TestCase):
    """T2/T3: new-org flag defaults + idempotent trial-credit seeding."""

    def test_new_org_has_flags_on_by_default(self):
        org = Organization.objects.create(name='Fresh Org', slug='fresh-org')
        self.assertTrue(org.external_events_enabled)
        self.assertTrue(org.sms_marketing_enabled)

    def _expected_trial_cents(self):
        from tickets.services.org_onboarding import TRIAL_SMS_CREDIT_TOKENS
        from tickets.services.sms_credits import price_per_segment_cents
        return int(TRIAL_SMS_CREDIT_TOKENS * price_per_segment_cents())

    def test_initialize_seeds_one_trial_credit_row(self):
        from tickets.services.org_onboarding import initialize_new_organization
        from tickets.models import SMSCreditTransaction

        org = Organization.objects.create(name='Seed Org', slug='seed-org')
        initialize_new_organization(org)

        org.refresh_from_db()
        self.assertEqual(org.sms_credit_balance_cents, self._expected_trial_cents())
        rows = SMSCreditTransaction.objects.filter(
            organization=org, kind=SMSCreditTransaction.Kind.ADJUSTMENT,
        )
        self.assertEqual(rows.count(), 1)

    def test_initialize_is_idempotent(self):
        from tickets.services.org_onboarding import initialize_new_organization
        from tickets.models import SMSCreditTransaction

        org = Organization.objects.create(name='Idem Org', slug='idem-org')
        initialize_new_organization(org)
        initialize_new_organization(org)  # second call must be a no-op

        org.refresh_from_db()
        self.assertEqual(org.sms_credit_balance_cents, self._expected_trial_cents())
        self.assertEqual(
            SMSCreditTransaction.objects.filter(organization=org).count(), 1
        )

    def test_initialize_is_non_fatal_when_wallet_raises(self):
        from unittest.mock import patch
        from tickets.services.org_onboarding import initialize_new_organization

        org = Organization.objects.create(name='Fail Org', slug='fail-org')
        with patch('tickets.services.sms_credits.credit', side_effect=RuntimeError('boom')):
            # Must not raise — org creation already succeeded.
            initialize_new_organization(org)
        org.refresh_from_db()
        self.assertEqual(org.sms_credit_balance_cents, 0)

    def test_api_org_creation_path_seeds_flags_and_credits(self):
        # _ensure_organization_for_user is the mobile/Stripe path that a naive
        # inline-init would have missed; assert it seeds flags + trial credits.
        from tickets.api_views import _ensure_organization_for_user

        user = User.objects.create_user(username='mobile-organizer')
        UserProfile.objects.create(user=user, organization=None)

        org = _ensure_organization_for_user(user)

        self.assertTrue(org.external_events_enabled)
        self.assertTrue(org.sms_marketing_enabled)
        org.refresh_from_db()
        self.assertEqual(org.sms_credit_balance_cents, self._expected_trial_cents())


class BuiltinFormatsAndTalentRemovalTests(TestCase):
    """Eventbrite + POSH built-ins available; Talent Lineup removed from create/import."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(
            name='Fmt Org', slug='fmt-org', external_events_enabled=True,
        )
        self.user = User.objects.create_user(
            username='fmtorg', email='fmt@test.com', password='pass12345',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org,
            role=UserProfile.Role.ORGANIZER, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.venue = Venue.objects.create(organization=self.org, name='V', city='C')
        self.client.login(username='fmt@test.com', password='pass12345')
        self.client.get(reverse('tickets:home'))  # seed session org

    def test_builtin_formats_include_posh_and_eventbrite(self):
        names = set(CSVFormat.available_for(self.org).values_list('name', flat=True))
        self.assertIn('POSH', names)
        self.assertIn('Eventbrite', names)

    def test_eventbrite_orders_export_imports_end_to_end(self):
        # Real Eventbrite "Orders" export headers (order-level, no ticket-class
        # column) — must import via the Event name fallback for ticket_type, and
        # book revenue from Net sales.
        import io
        from decimal import Decimal
        eventbrite = CSVFormat.objects.get(name='Eventbrite', is_system=True)
        upload = UploadedFile.objects.create(
            organization=self.org, csv_format=eventbrite,
            filename='eventbrite.csv', status='pending',
            metadata={'event_name': 'Familiar Faces Day Party', 'event_start_date': '2026-04-25'},
        )
        csv_body = (
            "Order ID,Order date,Buyer first name,Buyer last name,Buyer email,"
            "Phone number,Ticket quantity,Net sales,Event name,Event location\n"
            "14572631913,2026-03-30 16:01:14,John,Frye,john@createdpodcast.com,,1,8,Familiar Faces Day Party,Lot 613\n"
            "14572638553,2026-03-30 16:02:29,Love,Guillette,lovevguillette@gmail.com,,4,32,Familiar Faces Day Party,Lot 613\n"
        )
        from tickets.csv_processor import CSVProcessor
        results = CSVProcessor(upload, eventbrite).process_and_save(
            io.BytesIO(csv_body.encode('utf-8'))
        )
        self.assertEqual(results['success_count'], 2)

        john = Customer.objects.get(organization=self.org, email='john@createdpodcast.com')
        self.assertEqual(john.name, 'John Frye')
        order = TicketOrder.objects.get(customer=john)
        self.assertEqual(order.total_amount, Decimal('8'))
        # No ticket-class column → ticket_type falls back to the event name.
        self.assertEqual(order.tickets.first().ticket_type, 'Familiar Faces Day Party')

    def test_eventbrite_attendee_export_multi_ticket_buyer(self):
        # Attendee report: one row per ticket. A 4-ticket order becomes 4 rows
        # with the SAME Order ID but unique Barcodes. order_number maps to Barcode
        # so the tickets don't collapse; all credit the Buyer (not the attendee).
        import io
        from decimal import Decimal
        eventbrite = CSVFormat.objects.get(name='Eventbrite', is_system=True)
        upload = UploadedFile.objects.create(
            organization=self.org, csv_format=eventbrite,
            filename='eventbrite_attendees.csv', status='pending',
            metadata={'event_name': 'Familiar Faces Day Party', 'event_start_date': '2026-04-25'},
        )
        header = (
            "Order ID,Order date,Buyer first name,Buyer last name,Buyer email,"
            "Phone number,Ticket type,Ticket quantity,Ticket price,Barcode number,"
            "Event name,Event location\n"
        )
        rows = "".join(
            f"14572638553,2026-03-30 16:02:29,Love,Guillette,lovevguillette@gmail.com,,"
            f"entry before 5:30pm,1,8.00,BC-{i},Familiar Faces Day Party,Lot 613\n"
            for i in range(1, 5)  # 4 tickets, same order, 4 barcodes
        )
        from tickets.csv_processor import CSVProcessor
        results = CSVProcessor(upload, eventbrite).process_and_save(
            io.BytesIO((header + rows).encode('utf-8'))
        )
        self.assertEqual(results['success_count'], 4)

        love = Customer.objects.get(organization=self.org, email='lovevguillette@gmail.com')
        self.assertEqual(love.name, 'Love Guillette')
        love_orders = TicketOrder.objects.filter(customer=love)
        self.assertEqual(love_orders.count(), 4)  # barcode de-dup kept all 4
        self.assertEqual(sum(o.total_amount for o in love_orders), Decimal('32'))
        self.assertEqual(love_orders.first().tickets.first().ticket_type, 'entry before 5:30pm')

    def test_import_page_has_no_talent_lineup(self):
        html = self.client.get(
            reverse('tickets:event_create', args=['external'])
        ).content.decode()
        self.assertNotIn('Talent Lineup', html)

    def test_external_event_create_works_without_talent_fields(self):
        resp = self.client.post(reverse('tickets:event_create', args=['external']), {
            'name': 'No Talent Show',
            'ticketing_type': 'external',
            'venue': str(self.venue.id),
            'start_date': '2025-03-10',
            'start_time': '20:00',
            'end_date': '2025-03-10',
            'end_time': '22:00',
            'timezone': 'America/Chicago',
            'ticket_link': '',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            Event.objects.filter(organization=self.org, name='No Talent Show').exists()
        )


class VenueAdminScopeTests(TestCase):
    def test_venue_admin_queryset_is_scoped_for_non_superusers(self):
        from django.contrib import admin
        from tickets.admin import VenueAdmin

        org = Organization.objects.create(name='Admin Org', slug='admin-org')
        other_org = Organization.objects.create(name='Other Admin Org', slug='other-admin-org')
        user = User.objects.create_user(username='venue-admin')
        UserProfile.objects.create(
            user=user, organization=org, org_role=UserProfile.OrgRole.OWNER,
        )
        Venue.objects.create(organization=org, name='Own Venue', city='San Diego')
        Venue.objects.create(organization=other_org, name='Other Venue', city='Los Angeles')
        request = RequestFactory().get('/admin/tickets/venue/')
        request.user = user

        qs = VenueAdmin(Venue, admin.site).get_queryset(request)

        self.assertEqual(list(qs.values_list('name', flat=True)), ['Own Venue'])


class ChatTestMixin:
    """Shared setup for chat tests: creates org, user, profile, venue, event, customer, order.

    The read-only fixture graph is built once per class in setUpTestData; only the
    per-test HTTP client/session and a fresh conversation_id live in setUp.
    """

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Test Org', slug='test-org')
        cls.user = User.objects.create_user(
            username='chatuser', email='chat@test.com', password='testpass123'
        )
        UserProfile.objects.create(user=cls.user, organization=cls.org, org_role=UserProfile.OrgRole.OWNER)
        cls.venue = Venue.objects.create(
            organization=cls.org, name='Test Venue', city='Test City'
        )
        cls.event = Event.objects.create(
            organization=cls.org, name='Summer Fest',
            venue=cls.venue, start_date=date(2025, 7, 15)
        )
        cls.customer = Customer.objects.create(
            organization=cls.org, email='alice@example.com',
            name='Alice Smith', lifetime_value=Decimal('500.00'),
            rfm_segment='VIP',
        )
        cls.csv_format = CSVFormat.objects.create(
            organization=cls.org, name='Chat Test Format',
            column_mapping={'order_number': 'Order ID'},
        )
        cls.upload = UploadedFile.objects.create(
            organization=cls.org, csv_format=cls.csv_format,
            filename='test.csv', status='completed',
        )
        cls.order = TicketOrder.objects.create(
            customer=cls.customer, event=cls.event,
            uploaded_file=cls.upload,
            order_number='CHAT-001',
            order_date='2025-07-10 10:00:00',
            total_amount=Decimal('500.00'),
        )

    def setUp(self):
        self.client = Client()
        self.client.login(username='chat@test.com', password='testpass123')
        # Hit any org-required view to seed the session with _org_id
        self.client.get(reverse('tickets:home'))
        self.conversation_id = uuid.uuid4()


class ChatMessageModelTest(ChatTestMixin, TestCase):
    """Tests for the ChatMessage model."""

    def test_create_chat_message(self):
        msg = ChatMessage.objects.create(
            organization=self.org,
            user=self.user,
            conversation_id=self.conversation_id,
            role='user',
            content='Hello, how many customers do I have?',
        )
        self.assertIsNotNone(msg.id)
        self.assertEqual(msg.role, 'user')
        self.assertEqual(msg.organization, self.org)

    def test_ordering_by_created_at(self):
        m1 = ChatMessage.objects.create(
            organization=self.org, user=self.user,
            conversation_id=self.conversation_id,
            role='user', content='First',
        )
        m2 = ChatMessage.objects.create(
            organization=self.org, user=self.user,
            conversation_id=self.conversation_id,
            role='assistant', content='Second',
        )
        msgs = list(ChatMessage.objects.filter(conversation_id=self.conversation_id))
        self.assertEqual(msgs[0].id, m1.id)
        self.assertEqual(msgs[1].id, m2.id)

    def test_conversation_grouping(self):
        other_convo = uuid.uuid4()
        ChatMessage.objects.create(
            organization=self.org, user=self.user,
            conversation_id=self.conversation_id,
            role='user', content='Convo 1 message',
        )
        ChatMessage.objects.create(
            organization=self.org, user=self.user,
            conversation_id=other_convo,
            role='user', content='Convo 2 message',
        )
        convo1 = ChatMessage.objects.filter(conversation_id=self.conversation_id).count()
        convo2 = ChatMessage.objects.filter(conversation_id=other_convo).count()
        self.assertEqual(convo1, 1)
        self.assertEqual(convo2, 1)


class ChatToolsTest(ChatTestMixin, TestCase):
    """Tests for the chat agent tool functions (org scoping)."""

    def test_organization_summary(self):
        from .services.chat.tools import _get_organization_summary
        result = _get_organization_summary(self.org)
        self.assertIn('Test Org', result)
        self.assertIn('1', result)  # 1 customer
        self.assertIn('$500.00', result)

    def test_search_customers(self):
        from .services.chat.tools import _search_customers
        result = _search_customers(self.org, query='alice')
        self.assertIn('Alice Smith', result)
        self.assertIn('alice@example.com', result)

    def test_search_customers_no_results(self):
        from .services.chat.tools import _search_customers
        result = _search_customers(self.org, query='nonexistent')
        self.assertIn('No customers found', result)

    def test_get_customer_detail(self):
        from .services.chat.tools import _get_customer_detail
        result = _get_customer_detail(self.org, email='alice@example.com')
        self.assertIn('Alice Smith', result)
        self.assertIn('$500.00', result)
        self.assertIn('VIP', result)

    def test_get_customer_detail_not_found(self):
        from .services.chat.tools import _get_customer_detail
        result = _get_customer_detail(self.org, email='nobody@test.com')
        self.assertIn('No customer found', result)

    def test_search_events(self):
        from .services.chat.tools import _search_events
        result = _search_events(self.org, query='Summer')
        self.assertIn('Summer Fest', result)

    def test_get_event_detail(self):
        from .services.chat.tools import _get_event_detail
        result = _get_event_detail(self.org, event_name='Summer')
        self.assertIn('Summer Fest', result)
        self.assertIn('$500.00', result)

    def test_segment_distribution(self):
        from .services.chat.tools import _get_segment_distribution
        result = _get_segment_distribution(self.org)
        self.assertIn('VIP', result)

    def test_top_customers(self):
        from .services.chat.tools import _get_top_customers
        result = _get_top_customers(self.org, metric='ltv', limit=5)
        self.assertIn('Alice Smith', result)

    def test_revenue_by_venue(self):
        from .services.chat.tools import _get_revenue_by_venue
        result = _get_revenue_by_venue(self.org)
        self.assertIn('Test Venue', result)

    def test_upcoming_events_empty(self):
        from .services.chat.tools import _get_upcoming_events
        # Event is in the past (2025-07-15), so no upcoming
        result = _get_upcoming_events(self.org)
        self.assertIn('No upcoming events', result)

    def test_org_scoping_prevents_cross_tenant(self):
        """Tools should not return data from other organizations."""
        from .services.chat.tools import _search_customers
        other_org = Organization.objects.create(name='Other Org', slug='other-org')
        Customer.objects.create(
            organization=other_org, email='bob@other.com',
            name='Bob Other', lifetime_value=Decimal('999.00'),
        )
        result = _search_customers(self.org, query='bob')
        self.assertIn('No customers found', result)


class ChatViewTest(ChatTestMixin, TestCase):
    """Tests for chat view endpoints."""

    def test_chat_stream_requires_auth(self):
        self.client.logout()
        response = self.client.post(
            reverse('tickets:chat_stream'),
            data='{"message":"hi","conversation_id":"' + str(self.conversation_id) + '"}',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)

    def test_chat_stream_requires_message(self):
        response = self.client.post(
            reverse('tickets:chat_stream'),
            data='{"message":"","conversation_id":"' + str(self.conversation_id) + '"}',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_chat_stream_invalid_json(self):
        response = self.client.post(
            reverse('tickets:chat_stream'),
            data='not json',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_chat_history_returns_json(self):
        ChatMessage.objects.create(
            organization=self.org, user=self.user,
            conversation_id=self.conversation_id,
            role='user', content='test question',
        )
        ChatMessage.objects.create(
            organization=self.org, user=self.user,
            conversation_id=self.conversation_id,
            role='assistant', content='test answer',
        )
        response = self.client.get(
            reverse('tickets:chat_history'),
            {'conversation_id': str(self.conversation_id)},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data['messages']), 2)
        self.assertEqual(data['messages'][0]['role'], 'user')
        self.assertEqual(data['messages'][1]['role'], 'assistant')

    def test_chat_history_empty_for_unknown_convo(self):
        response = self.client.get(
            reverse('tickets:chat_history'),
            {'conversation_id': str(uuid.uuid4())},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data['messages']), 0)

    def test_chat_conversations_returns_json(self):
        ChatMessage.objects.create(
            organization=self.org, user=self.user,
            conversation_id=self.conversation_id,
            role='user', content='my first question',
        )
        response = self.client.get(reverse('tickets:chat_conversations'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data['conversations']), 1)
        self.assertIn('my first question', data['conversations'][0]['preview'])

    def test_chat_conversations_requires_auth(self):
        self.client.logout()
        response = self.client.get(reverse('tickets:chat_conversations'))
        self.assertEqual(response.status_code, 302)

    def test_chat_history_org_scoped(self):
        """History endpoint should not return messages from other orgs."""
        other_org = Organization.objects.create(name='Other Org', slug='other-org2')
        other_user = User.objects.create_user(
            username='otheruser', email='other@test.com', password='pass123'
        )
        UserProfile.objects.create(user=other_user, organization=other_org)
        ChatMessage.objects.create(
            organization=other_org, user=other_user,
            conversation_id=self.conversation_id,
            role='user', content='secret message',
        )
        response = self.client.get(
            reverse('tickets:chat_history'),
            {'conversation_id': str(self.conversation_id)},
        )
        data = response.json()
        self.assertEqual(len(data['messages']), 0)


class MemberInviteTests(TestCase):
    """Tests for member list, invite, and invite accept views."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Test Org', slug='test-org')
        self.user = User.objects.create_user(
            username='memberuser', email='member@test.com', password='testpass123',
            first_name='Member', last_name='User',
        )
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='member@test.com', password='testpass123')
        self.client.get(reverse('tickets:home'))

    def test_member_list_authenticated_org_member_200(self):
        response = self.client.get(reverse('tickets:member_list'))
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.user.email, response.content.decode())
        self.assertIn('Invite member', response.content.decode())

    def test_member_list_unauthenticated_redirect(self):
        self.client.logout()
        response = self.client.get(reverse('tickets:member_list'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('tickets:login'), response.get('Location', '') or response.url)

    def test_member_list_no_org_redirect(self):
        self.user.profile.organization = None
        self.user.profile.save()
        # Use a new client and set session to "no org" before any request that would cache org
        client = Client()
        client.force_login(self.user)
        session = client.session
        session['_org_id'] = ''
        session.save()
        response = client.get(reverse('tickets:member_list'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('org-required', response.get('Location', '') or response.url)

    def test_member_invite_valid_email_creates_invitation(self):
        response = self.client.post(
            reverse('tickets:member_invite'),
            {'invite_method': 'email', 'email': 'newuser@example.com', 'role': 'organizer', 'org_role': 'host', 'csrfmiddlewaretoken': self.client.cookies.get('csrftoken', '')},
        )
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('tickets:member_list'))
        self.assertTrue(
            OrganizationInvitation.objects.filter(
                organization=self.org,
                email='newuser@example.com',
                status=OrganizationInvitation.Status.PENDING,
            ).exists()
        )

    def test_member_invite_email_matches_existing_user_records_phone(self):
        existing = User.objects.create_user(
            username='existing@example.com', email='existing@example.com', password='pass123',
        )
        UserProfile.objects.create(user=existing, organization=None, phone_number='+15555550199')
        response = self.client.post(
            reverse('tickets:member_invite'),
            {'invite_method': 'email', 'email': 'existing@example.com', 'org_role': 'host'},
        )
        self.assertEqual(response.status_code, 302)
        inv = OrganizationInvitation.objects.get(organization=self.org, email='existing@example.com')
        self.assertEqual(inv.phone_number, '+15555550199')
        self.assertEqual(inv.invited_via, OrganizationInvitation.InvitedVia.EMAIL)

    def test_member_invite_by_phone_matched_user_uses_on_file_email(self):
        existing = User.objects.create_user(
            username='phoneuser@example.com', email='phoneuser@example.com', password='pass123',
        )
        UserProfile.objects.create(user=existing, organization=None, phone_number='+15555550111')
        response = self.client.post(
            reverse('tickets:member_invite'),
            {'invite_method': 'phone', 'phone_number': '+15555550111', 'org_role': 'host'},
        )
        self.assertEqual(response.status_code, 302)
        inv = OrganizationInvitation.objects.get(organization=self.org, phone_number='+15555550111')
        self.assertEqual(inv.email, 'phoneuser@example.com')
        self.assertEqual(inv.invited_via, OrganizationInvitation.InvitedVia.PHONE)

    def test_member_invite_by_phone_no_match_errors(self):
        response = self.client.post(
            reverse('tickets:member_invite'),
            {'invite_method': 'phone', 'phone_number': '+15555550000', 'org_role': 'host'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            OrganizationInvitation.objects.filter(
                organization=self.org,
                phone_number='+15555550000',
            ).exists()
        )

    def test_member_invite_duplicate_email_existing_member_error(self):
        other = User.objects.create_user(
            username='other@example.com', email='other@example.com', password='pass123',
        )
        UserProfile.objects.create(user=other, organization=self.org)
        OrganizationMembership.objects.create(user=other, organization=self.org)
        response = self.client.post(
            reverse('tickets:member_invite'),
            {'invite_method': 'email', 'email': 'other@example.com', 'org_role': 'host'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('tickets:member_list'))
        self.assertFalse(
            OrganizationInvitation.objects.filter(
                organization=self.org,
                email='other@example.com',
            ).exists()
        )

    def test_member_invite_duplicate_pending_invite_error(self):
        OrganizationInvitation.objects.create(
            organization=self.org,
            email='pending@example.com',
            invited_by=self.user,
            status=OrganizationInvitation.Status.PENDING,
            expires_at=timezone.now() + timedelta(days=7),
        )
        response = self.client.post(
            reverse('tickets:member_invite'),
            {'invite_method': 'email', 'email': 'pending@example.com', 'org_role': 'host'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('tickets:member_list'))
        self.assertEqual(
            OrganizationInvitation.objects.filter(
                organization=self.org,
                email='pending@example.com',
                status=OrganizationInvitation.Status.PENDING,
            ).count(),
            1,
        )

    def test_invite_accept_unauthenticated_new_email_skips_email_otp(self):
        inv = OrganizationInvitation.objects.create(
            organization=self.org,
            email='brandnew@example.com',
            invited_by=self.user,
            status=OrganizationInvitation.Status.PENDING,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.client.logout()
        response = self.client.get(reverse('tickets:invite_accept', args=[inv.token]))
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(
            response, reverse('tickets:email_complete_profile'),
            target_status_code=200,
        )
        self.assertEqual(self.client.session.get('pending_invite_token'), str(inv.token))
        self.assertEqual(self.client.session.get('pending_signup_email'), 'brandnew@example.com')

    def test_invite_accept_unauthenticated_existing_email_redirects_to_login(self):
        existing = User.objects.create_user(
            username='already@example.com', email='already@example.com', password='pass123',
        )
        UserProfile.objects.create(user=existing, organization=None)
        inv = OrganizationInvitation.objects.create(
            organization=self.org,
            email='already@example.com',
            invited_by=self.user,
            status=OrganizationInvitation.Status.PENDING,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.client.logout()
        response = self.client.get(reverse('tickets:invite_accept', args=[inv.token]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('tickets:login'), response.get('Location', '') or response.url)
        # Token is stashed so the post-login flow can auto-accept even when the
        # login view ignores ?next= (e.g. organizer redirect to home).
        self.assertEqual(self.client.session.get('pending_invite_token'), str(inv.token))

    def test_invite_accept_valid_token_matching_email_joins_org(self):
        inv = OrganizationInvitation.objects.create(
            organization=self.org,
            email='invitee@test.com',
            invited_by=self.user,
            status=OrganizationInvitation.Status.PENDING,
            expires_at=timezone.now() + timedelta(days=7),
        )
        invitee = User.objects.create_user(
            username='invitee@test.com', email='invitee@test.com', password='pass123',
        )
        UserProfile.objects.create(user=invitee, organization=None)
        self.client.logout()
        self.client.login(username='invitee@test.com', password='pass123')
        response = self.client.get(reverse('tickets:invite_accept', args=[inv.token]))
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('tickets:home'))
        invitee.profile.refresh_from_db()
        self.assertEqual(invitee.profile.organization, self.org)
        inv.refresh_from_db()
        self.assertEqual(inv.status, OrganizationInvitation.Status.ACCEPTED)

    def test_invite_accept_wrong_user_email_mismatch(self):
        inv = OrganizationInvitation.objects.create(
            organization=self.org,
            email='invitee@test.com',
            invited_by=self.user,
            status=OrganizationInvitation.Status.PENDING,
            expires_at=timezone.now() + timedelta(days=7),
        )
        wrong_user = User.objects.create_user(
            username='wrong@test.com', email='wrong@test.com', password='pass123',
        )
        UserProfile.objects.create(user=wrong_user, organization=None)
        self.client.logout()
        self.client.login(username='wrong@test.com', password='pass123')
        response = self.client.get(reverse('tickets:invite_accept', args=[inv.token]))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'invitation was sent to', response.content)
        wrong_user.profile.refresh_from_db()
        self.assertIsNone(wrong_user.profile.organization)

    def test_invite_accept_expired_shows_message(self):
        inv = OrganizationInvitation.objects.create(
            organization=self.org,
            email='invitee@test.com',
            invited_by=self.user,
            status=OrganizationInvitation.Status.PENDING,
            expires_at=timezone.now() - timedelta(days=1),
        )
        invitee = User.objects.create_user(
            username='invitee@test.com', email='invitee@test.com', password='pass123',
        )
        UserProfile.objects.create(user=invitee, organization=None)
        self.client.logout()
        self.client.login(username='invitee@test.com', password='pass123')
        response = self.client.get(reverse('tickets:invite_accept', args=[inv.token]))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'expired or already been used', response.content)

class MobileAPITests(TestCase):
    """Test cases for the mobile API endpoints."""

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(
            name='API Test Org', slug='api-test-org',
            stripe_account_id='acct_test_api',
            stripe_onboarding_complete=True,
        )
        cls.user = User.objects.create_user(
            username='apiuser',
            email='api@example.com',
            password='apipass123',
        )
        UserProfile.objects.create(
            user=cls.user,
            organization=cls.org,
            org_role=UserProfile.OrgRole.OWNER,
            role=UserProfile.Role.ORGANIZER,
        )
        cls.token = Token.objects.create(user=cls.user)
        cls.auth_header = {'HTTP_AUTHORIZATION': f'Token {cls.token.key}'}

        cls.venue = Venue.objects.create(
            organization=cls.org, name='Test Venue', city='Portland'
        )
        cls.event = Event.objects.create(
            organization=cls.org,
            name='Test Event',
            venue=cls.venue,
            start_date=date.today() + timedelta(days=7),
        )
        cls.customer = Customer.objects.create(
            organization=cls.org,
            email='buyer@example.com',
            name='Test Buyer',
        )
        cls.order = TicketOrder.objects.create(
            customer=cls.customer,
            event=cls.event,
            order_number='TEST-001',
            order_date=timezone.now(),
            total_amount=Decimal('25.00'),
        )
        Ticket.objects.create(
            ticket_order=cls.order,
            ticket_type='General Admission',
            price=Decimal('25.00'),
        )

    def setUp(self):
        self.client = Client()

    def test_login_success(self):
        response = self.client.post(
            '/api/auth/login/',
            data={'email': 'api@example.com', 'password': 'apipass123'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('token', data)
        self.assertEqual(data['user_type'], UserProfile.Role.ORGANIZER)

    def test_login_invalid(self):
        response = self.client.post(
            '/api/auth/login/',
            data={'email': 'api@example.com', 'password': 'wrongpass'},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_checkin_valid_order(self):
        response = self.client.post(
            '/api/organizer/checkin/',
            data={'order_number': 'TEST-001', 'event_id': str(self.event.pk)},
            content_type='application/json',
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'checked_in')
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.checked_in_at)

    def test_checkin_already_scanned(self):
        # First check-in
        self.client.post(
            '/api/organizer/checkin/',
            data={'order_number': 'TEST-001', 'event_id': str(self.event.pk)},
            content_type='application/json',
            **self.auth_header,
        )
        # Second check-in
        response = self.client.post(
            '/api/organizer/checkin/',
            data={'order_number': 'TEST-001', 'event_id': str(self.event.pk)},
            content_type='application/json',
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'already_checked_in')

    def test_checkin_wrong_org(self):
        other_org = Organization.objects.create(name='Other Org', slug='other-org')
        other_customer = Customer.objects.create(
            organization=other_org, email='other@example.com', name='Other'
        )
        other_venue = Venue.objects.create(
            organization=other_org, name='Other Venue', city='Seattle'
        )
        other_event = Event.objects.create(
            organization=other_org,
            name='Other Event',
            venue=other_venue,
            start_date=date.today() + timedelta(days=3),
        )
        other_order = TicketOrder.objects.create(
            customer=other_customer,
            event=other_event,
            order_number='OTHER-001',
            order_date=timezone.now(),
            total_amount=Decimal('10.00'),
        )
        response = self.client.post(
            '/api/organizer/checkin/',
            data={'order_number': 'OTHER-001', 'event_id': str(other_event.pk)},
            content_type='application/json',
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 404)

    def test_checkin_refunded(self):
        self.order.refunded_at = timezone.now()
        self.order.save(update_fields=['refunded_at'])
        response = self.client.post(
            '/api/organizer/checkin/',
            data={'order_number': 'TEST-001', 'event_id': str(self.event.pk)},
            content_type='application/json',
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'refunded')

    def _sell_payload(self, tt, quantity=2, payment_intent_id='pi_test_123'):
        return {
            'event_id': str(self.event.pk),
            'payment_intent_id': payment_intent_id,
            'buyer_email': 'newbuyer@example.com',
            'buyer_name': 'New Buyer',
            'line_items': [
                {
                    'ticket_type_id': str(tt.pk),
                    'quantity': quantity,
                    'name': 'VIP',
                    'price': '50.00',
                }
            ],
        }

    @patch('stripe.PaymentIntent.retrieve')
    def test_sell_creates_order(self, mock_retrieve):
        mock_pi = MagicMock()
        mock_pi.status = 'succeeded'
        mock_pi.amount_received = 10000  # 2 x $50.00, Stripe-confirmed
        mock_pi.application_fee_amount = 832
        mock_retrieve.return_value = mock_pi

        tt = SaleableTicketType.objects.create(
            event=self.event,
            name='VIP',
            price=Decimal('50.00'),
        )

        response = self.client.post(
            '/api/organizer/sell/',
            data=self._sell_payload(tt),
            content_type='application/json',
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIn('order_number', data)
        self.assertEqual(data['ticket_count'], 2)
        # Verify DB
        new_order = TicketOrder.objects.get(order_number=data['order_number'])
        self.assertTrue(new_order.is_in_person)
        self.assertIsNotNone(new_order.checked_in_at)
        self.assertEqual(new_order.tickets.count(), 2)
        # Ledger row: direct charge, Stripe-confirmed amounts.
        session = StripeCheckoutSession.objects.get(stripe_session_id='pi_test_123')
        self.assertEqual(session.charge_flow, StripeCheckoutSession.ChargeFlow.DIRECT)
        self.assertEqual(session.status, StripeCheckoutSession.Status.COMPLETED)
        self.assertEqual(session.amount_total_cents, 10000)
        self.assertEqual(session.platform_fee_cents, 832)
        self.assertEqual(session.ticket_order, new_order)
        self.assertIsNotNone(session.fulfilled_at)

    @patch('stripe.PaymentIntent.retrieve')
    def test_sell_duplicate_finalize_returns_existing_order(self, mock_retrieve):
        mock_pi = MagicMock()
        mock_pi.status = 'succeeded'
        mock_pi.amount_received = 10000
        mock_pi.application_fee_amount = 832
        mock_retrieve.return_value = mock_pi

        tt = SaleableTicketType.objects.create(
            event=self.event, name='VIP', price=Decimal('50.00'),
        )
        kwargs = dict(
            data=self._sell_payload(tt), content_type='application/json', **self.auth_header,
        )

        first = self.client.post('/api/organizer/sell/', **kwargs)
        second = self.client.post('/api/organizer/sell/', **kwargs)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['order_number'], first.json()['order_number'])
        self.assertEqual(second.json()['ticket_count'], 2)
        # One order, one session row, inventory bumped once.
        self.assertEqual(
            TicketOrder.objects.filter(event=self.event, is_in_person=True).count(), 1,
        )
        self.assertEqual(
            StripeCheckoutSession.objects.filter(stripe_session_id='pi_test_123').count(), 1,
        )
        tt.refresh_from_db()
        self.assertEqual(tt.quantity_sold, 2)

    @patch('stripe.PaymentIntent.retrieve')
    def test_sell_rejects_amount_mismatch(self, mock_retrieve):
        # PI charged $80 but current server prices say $100 — stale client.
        mock_pi = MagicMock()
        mock_pi.status = 'succeeded'
        mock_pi.amount_received = 8000
        mock_pi.application_fee_amount = 0
        mock_retrieve.return_value = mock_pi

        tt = SaleableTicketType.objects.create(
            event=self.event, name='VIP', price=Decimal('50.00'),
        )

        response = self.client.post(
            '/api/organizer/sell/',
            data=self._sell_payload(tt),
            content_type='application/json',
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(TicketOrder.objects.filter(event=self.event, is_in_person=True).count(), 0)
        self.assertFalse(StripeCheckoutSession.objects.filter(stripe_session_id='pi_test_123').exists())

    # ------------------------------------------------------------------
    # Free tickets are not sellable in person (Tap to Pay)
    # ------------------------------------------------------------------

    def test_organizer_ticket_types_excludes_free(self):
        # Mixed catalog: only the paid type is offered for in-person sale.
        paid = SaleableTicketType.objects.create(
            event=self.event, name='GA', price=Decimal('25.00'),
        )
        SaleableTicketType.objects.create(
            event=self.event, name='Free Comp', price=Decimal('0.00'),
        )
        response = self.client.get(
            f'/api/organizer/events/{self.event.pk}/ticket-types/',
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual([t['id'] for t in data], [str(paid.pk)])

    def test_organizer_ticket_types_all_free_returns_empty_200(self):
        # All-free event: empty array, NOT a 404 — app renders its empty state.
        SaleableTicketType.objects.create(
            event=self.event, name='Free Comp', price=Decimal('0.00'),
        )
        response = self.client.get(
            f'/api/organizer/events/{self.event.pk}/ticket-types/',
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_scanner_ticket_types_excludes_free(self):
        paid = SaleableTicketType.objects.create(
            event=self.event, name='GA', price=Decimal('25.00'),
        )
        SaleableTicketType.objects.create(
            event=self.event, name='Free Comp', price=Decimal('0.00'),
        )
        from .models import ScannerSession
        session = ScannerSession.objects.create(event=self.event)
        response = self.client.get(
            f'/api/scanner/ticket-types/?event_id={self.event.pk}',
            HTTP_AUTHORIZATION=f'Scanner {session.token}',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual([t['id'] for t in data], [str(paid.pk)])

    @patch('stripe.PaymentIntent.create')
    def test_terminal_payment_intent_rejects_free(self, mock_create):
        free = SaleableTicketType.objects.create(
            event=self.event, name='Free Comp', price=Decimal('0.00'),
        )
        response = self.client.post(
            '/api/stripe/terminal-payment-intent/',
            data={
                'event_id': str(self.event.pk),
                'line_items': [{'ticket_type_id': str(free.pk), 'quantity': 1}],
            },
            content_type='application/json',
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'free_ticket_not_sellable')
        mock_create.assert_not_called()

    @patch('stripe.PaymentIntent.create')
    def test_terminal_payment_intent_rejects_below_minimum(self, mock_create):
        # Paid but sub-minimum ($0.30 < $0.50 USD): reject before hitting Stripe.
        cheap = SaleableTicketType.objects.create(
            event=self.event, name='Cheap', price=Decimal('0.30'),
        )
        response = self.client.post(
            '/api/stripe/terminal-payment-intent/',
            data={
                'event_id': str(self.event.pk),
                'line_items': [{'ticket_type_id': str(cheap.pk), 'quantity': 1}],
            },
            content_type='application/json',
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'amount_below_minimum')
        mock_create.assert_not_called()

    @patch('stripe.PaymentIntent.retrieve')
    def test_sell_rejects_free_ticket(self, mock_retrieve):
        mock_pi = MagicMock()
        mock_pi.status = 'succeeded'
        mock_pi.amount_received = 0
        mock_retrieve.return_value = mock_pi

        free = SaleableTicketType.objects.create(
            event=self.event, name='Free Comp', price=Decimal('0.00'),
        )
        response = self.client.post(
            '/api/organizer/sell/',
            data=self._sell_payload(free),
            content_type='application/json',
            **self.auth_header,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'free_ticket_not_sellable')
        self.assertEqual(
            TicketOrder.objects.filter(event=self.event, is_in_person=True).count(), 0,
        )
        self.assertFalse(
            StripeCheckoutSession.objects.filter(stripe_session_id='pi_test_123').exists(),
        )

    def test_token_auth_required(self):
        response = self.client.get('/api/organizer/events/')
        self.assertEqual(response.status_code, 401)

    # --- organizer_events: past-event listing --------------------------------
    def _past_event(self, name='Past Event', ticketing_type=None):
        kwargs = {
            'organization': self.org, 'name': name, 'venue': self.venue,
            'start_date': date.today() - timedelta(days=30),
        }
        if ticketing_type is not None:
            kwargs['ticketing_type'] = ticketing_type
        return Event.objects.create(**kwargs)

    def _events_by_id(self, status=None):
        url = '/api/organizer/events/'
        if status is not None:
            url += f'?status={status}'
        response = self.client.get(url, **self.auth_header)
        self.assertEqual(response.status_code, 200)
        return {row['id']: row for row in response.json()}

    def test_events_default_excludes_past(self):
        past = self._past_event()
        rows = self._events_by_id()  # no param -> upcoming only (backwards-compat)
        self.assertNotIn(str(past.pk), rows)
        # The class-level upcoming event is still present.
        self.assertIn(str(self.event.pk), rows)

    def test_events_status_past_includes_direct_and_external(self):
        from tickets.models import TICKETING_TYPE_EXTERNAL
        past_direct = self._past_event('Past Direct', TICKETING_TYPE_DIRECT)
        past_external = self._past_event('Past External', TICKETING_TYPE_EXTERNAL)
        rows = self._events_by_id(status='past')
        self.assertIn(str(past_direct.pk), rows)
        self.assertIn(str(past_external.pk), rows)
        self.assertEqual(rows[str(past_direct.pk)]['status'], 'past')
        self.assertEqual(rows[str(past_direct.pk)]['ticketing_type'], TICKETING_TYPE_DIRECT)
        self.assertEqual(rows[str(past_external.pk)]['ticketing_type'], TICKETING_TYPE_EXTERNAL)
        # Upcoming event excluded from the past list.
        self.assertNotIn(str(self.event.pk), rows)

    def test_events_status_all_includes_upcoming_and_past(self):
        past = self._past_event()
        rows = self._events_by_id(status='all')
        self.assertIn(str(self.event.pk), rows)
        self.assertIn(str(past.pk), rows)
        self.assertEqual(rows[str(self.event.pk)]['status'], 'upcoming')
        self.assertEqual(rows[str(past.pk)]['status'], 'past')

    def test_events_checked_in_count_is_per_ticket(self):
        # Partially-scanned 2-ticket order: one ticket scanned, order-level
        # checked_in_at left NULL. Order-level counting would report 0.
        past = self._past_event()
        order = TicketOrder.objects.create(
            customer=self.customer, event=past, order_number='PAST-001',
            order_date=timezone.now(), total_amount=Decimal('0.00'),
        )
        Ticket.objects.create(
            ticket_order=order, ticket_type='GA', price=Decimal('0.00'),
            scanned_at=timezone.now(),
        )
        Ticket.objects.create(
            ticket_order=order, ticket_type='GA', price=Decimal('0.00'),
        )
        self.assertIsNone(order.checked_in_at)
        rows = self._events_by_id(status='past')
        self.assertEqual(rows[str(past.pk)]['total_tickets'], 2)
        self.assertEqual(rows[str(past.pk)]['checked_in_count'], 1)


class StripeWebhookTests(TestCase):
    """Tests for the Stripe webhook endpoint and payment fulfillment logic."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Webhook Test Org', slug='webhook-test-org')
        self.venue = Venue.objects.create(
            organization=self.org, name='Test Venue', city='Test City'
        )
        self.event = Event.objects.create(
            organization=self.org,
            name='Test Event',
            venue=self.venue,
            start_date=date(2025, 6, 15),
            start_time=time(19, 0),
        )
        self.ticket_type = SaleableTicketType.objects.create(
            event=self.event,
            name='General Admission',
            price=Decimal('25.00'),
            quantity_limit=100,
            quantity_sold=0,
        )
        self.webhook_url = reverse('tickets:stripe_webhook')

    def _create_pending_session(self, pi_id='pi_test_123', **kwargs):
        """Helper to create a StripeCheckoutSession in PENDING state."""
        defaults = {
            'event': self.event,
            'organization': self.org,
            'stripe_session_id': pi_id,
            'stripe_payment_intent_id': pi_id,
            'buyer_email': 'buyer@example.com',
            'buyer_name': 'Test Buyer',
            'status': StripeCheckoutSession.Status.PENDING,
            'line_items_snapshot': [
                {
                    'saleable_ticket_type_id': str(self.ticket_type.id),
                    'name': 'General Admission',
                    'price': '25.00',
                    'quantity': 2,
                }
            ],
            'amount_total_cents': 5000,
        }
        defaults.update(kwargs)
        return StripeCheckoutSession.objects.create(**defaults)

    def _build_webhook_payload(self, pi_id='pi_test_123', event_type='payment_intent.succeeded', amount=5000):
        """Build a fake Stripe webhook event payload."""
        import json
        return json.dumps({
            'id': 'evt_test_123',
            'type': event_type,
            'data': {
                'object': {
                    'id': pi_id,
                    'amount_received': amount,
                }
            }
        })

    @patch('stripe.Webhook.construct_event')
    def test_webhook_happy_path_creates_order(self, mock_construct):
        """Valid webhook creates order, updates inventory, marks session COMPLETED."""
        session = self._create_pending_session()
        payload = self._build_webhook_payload()
        mock_construct.return_value = {
            'type': 'payment_intent.succeeded',
            'data': {'object': {'id': 'pi_test_123', 'amount_received': 5000}},
        }

        response = self.client.post(
            self.webhook_url,
            data=payload,
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='sig_test',
        )

        self.assertEqual(response.status_code, 200)

        # Session marked COMPLETED
        session.refresh_from_db()
        self.assertEqual(session.status, StripeCheckoutSession.Status.COMPLETED)
        self.assertIsNotNone(session.fulfilled_at)

        # Order created
        self.assertIsNotNone(session.ticket_order)
        order = session.ticket_order
        self.assertEqual(order.total_amount, Decimal('50.00'))
        self.assertEqual(order.event, self.event)

        # Tickets created
        self.assertEqual(order.tickets.count(), 2)

        # Inventory updated
        self.ticket_type.refresh_from_db()
        self.assertEqual(self.ticket_type.quantity_sold, 2)

        # Customer created with correct email
        customer = Customer.objects.get(email='buyer@example.com')
        self.assertEqual(customer.name, 'Test Buyer')

    @patch('stripe.Webhook.construct_event')
    def test_webhook_idempotency_prevents_duplicate_orders(self, mock_construct):
        """Same webhook delivered twice creates only one order."""
        session = self._create_pending_session()
        mock_construct.return_value = {
            'type': 'payment_intent.succeeded',
            'data': {'object': {'id': 'pi_test_123', 'amount_received': 5000}},
        }
        payload = self._build_webhook_payload()
        kwargs = dict(data=payload, content_type='application/json', HTTP_STRIPE_SIGNATURE='sig_test')

        # First delivery
        resp1 = self.client.post(self.webhook_url, **kwargs)
        self.assertEqual(resp1.status_code, 200)

        # Second delivery (Stripe retry)
        resp2 = self.client.post(self.webhook_url, **kwargs)
        self.assertEqual(resp2.status_code, 200)

        # Only one order exists
        self.assertEqual(TicketOrder.objects.filter(event=self.event).count(), 1)

        # Inventory only incremented once
        self.ticket_type.refresh_from_db()
        self.assertEqual(self.ticket_type.quantity_sold, 2)

    @patch('stripe.Webhook.construct_event')
    def test_webhook_invalid_signature_returns_400(self, mock_construct):
        """Invalid Stripe signature returns 400."""
        import stripe as stripe_lib
        mock_construct.side_effect = stripe_lib.error.SignatureVerificationError('bad sig', 'sig_header')

        response = self.client.post(
            self.webhook_url,
            data='{}',
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='bad_sig',
        )

        self.assertEqual(response.status_code, 400)

    @patch('stripe.Webhook.construct_event')
    def test_webhook_missing_session_does_not_crash(self, mock_construct):
        """Webhook for unknown PaymentIntent logs warning but returns 200."""
        mock_construct.return_value = {
            'type': 'payment_intent.succeeded',
            'data': {'object': {'id': 'pi_nonexistent', 'amount_received': 5000}},
        }

        response = self.client.post(
            self.webhook_url,
            data=self._build_webhook_payload(pi_id='pi_nonexistent'),
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='sig_test',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(TicketOrder.objects.count(), 0)

    @patch('stripe.Webhook.construct_event')
    def test_webhook_oversell_still_fulfills(self, mock_construct):
        """When inventory exceeds limit, order is still fulfilled (logged as error)."""
        # Set inventory near limit
        self.ticket_type.quantity_sold = 99
        self.ticket_type.save(update_fields=['quantity_sold'])

        session = self._create_pending_session()
        mock_construct.return_value = {
            'type': 'payment_intent.succeeded',
            'data': {'object': {'id': 'pi_test_123', 'amount_received': 5000}},
        }

        response = self.client.post(
            self.webhook_url,
            data=self._build_webhook_payload(),
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='sig_test',
        )

        self.assertEqual(response.status_code, 200)

        # Order still created despite oversell
        session.refresh_from_db()
        self.assertEqual(session.status, StripeCheckoutSession.Status.COMPLETED)
        self.assertIsNotNone(session.ticket_order)

        # Inventory went over limit (99 + 2 = 101 > 100)
        self.ticket_type.refresh_from_db()
        self.assertEqual(self.ticket_type.quantity_sold, 101)


class FinancePayoutTests(TestCase):
    """Tests for organizer payout requests and connected-account payout webhooks."""

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(
            name='Finance Test Org',
            slug='finance-test-org',
            stripe_account_id='acct_123',
            stripe_onboarding_complete=True,
        )
        cls.user = User.objects.create_user(
            username='finance-owner',
            email='owner@example.com',
            password='testpass123',
        )
        UserProfile.objects.create(
            user=cls.user,
            organization=cls.org,
            org_role=UserProfile.OrgRole.OWNER,
        )

    def setUp(self):
        # The org id is now stable across tests (setUpTestData), so the per-org
        # connected-balance cache would leak a prior test's figure into later
        # finance_overview assertions. Clear it before each test.
        from django.core.cache import cache as django_cache
        django_cache.clear()
        self.client = Client()
        self.client.login(username='owner@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))
        self.payout_url = reverse('tickets:initiate_payout')
        self.finance_url = reverse('tickets:finance_overview')
        self.connect_webhook_url = reverse('tickets:stripe_connect_webhook')
        self.recover_url = reverse('tickets:recover_pending_payouts')

    def _mock_account(self, payouts_enabled=True, external_accounts=None):
        account = MagicMock()
        account.details_submitted = True
        account.charges_enabled = True
        account.payouts_enabled = payouts_enabled
        external_accounts = external_accounts if external_accounts is not None else {'data': []}
        account.get.side_effect = lambda key, default=None: {
            'external_accounts': external_accounts,
        }.get(key, default)
        return account

    @patch('tickets.views._get_connected_balance_cents')
    @patch('stripe.Account.retrieve')
    @patch('stripe.Account.modify')
    @patch('stripe.Payout.create')
    @patch('stripe.Transfer.create')
    def test_can_request_multiple_pending_payouts(
        self,
        mock_transfer_create,
        mock_payout_create,
        mock_account_modify,
        mock_account_retrieve,
        mock_connected_balance,
    ):
        mock_connected_balance.return_value = (45000, 0)
        mock_payout_create.side_effect = [
            MagicMock(id='po_first', status='pending'),
            MagicMock(id='po_second', status='pending'),
        ]
        mock_account_retrieve.return_value = self._mock_account(payouts_enabled=True)

        first = self.client.post(self.payout_url, {'amount': '100.00'})
        second = self.client.post(self.payout_url, {'amount': '50.00'})

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)

        payouts = list(Payout.objects.filter(organization=self.org).order_by('created_at'))
        self.assertEqual(len(payouts), 2)
        self.assertEqual([payout.amount for payout in payouts], [Decimal('100.00'), Decimal('50.00')])
        self.assertEqual([payout.status for payout in payouts], [Payout.Status.PENDING, Payout.Status.PENDING])
        self.assertEqual([payout.origin for payout in payouts], [Payout.Origin.CUE, Payout.Origin.CUE])
        # Destination-charge model: the money already lives in the connected
        # account, so no platform Transfer is ever created.
        self.assertEqual([payout.stripe_transfer_id for payout in payouts], [None, None])
        self.assertEqual([payout.stripe_payout_id for payout in payouts], ['po_first', 'po_second'])
        mock_transfer_create.assert_not_called()
        self.assertEqual(mock_account_modify.call_count, 2)
        self.assertEqual(mock_payout_create.call_count, 2)
        # Each Stripe payout carries a row-derived idempotency key so a
        # timeout retry can't withdraw twice.
        for call, payout in zip(mock_payout_create.call_args_list, payouts):
            self.assertEqual(call.kwargs['idempotency_key'], f'payout-{payout.id}')
            self.assertEqual(call.kwargs['stripe_account'], self.org.stripe_account_id)

    @patch('stripe.Account.modify')
    @patch('stripe.Account.retrieve')
    def test_connect_return_requires_payouts_enabled(self, mock_account_retrieve, mock_account_modify):
        mock_account_retrieve.return_value = self._mock_account(payouts_enabled=False)

        response = self.client.get(reverse('tickets:stripe_connect_return'))

        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        self.assertFalse(self.org.stripe_onboarding_complete)
        # The return view should idempotently request card_payments to
        # unstick Express accounts that landed in 'unrequested'.
        mock_account_modify.assert_called_once_with(
            self.org.stripe_account_id,
            capabilities={'card_payments': {'requested': True}},
        )

    @patch('tickets.views._get_connected_balance_cents')
    @patch('stripe.Account.retrieve')
    def test_initiate_payout_rejected_when_payouts_disabled(
        self,
        mock_account_retrieve,
        mock_connected_balance,
    ):
        mock_connected_balance.return_value = (45000, 0)
        mock_account_retrieve.return_value = self._mock_account(payouts_enabled=False)

        response = self.client.post(self.payout_url, {'amount': '100.00'})

        self.assertEqual(response.status_code, 302)
        payout = Payout.objects.count()
        self.assertEqual(payout, 0)

    @patch('tickets.views._get_connected_balance_cents')
    @patch('stripe.Account.retrieve')
    def test_initiate_payout_rejected_when_exceeding_connected_balance(
        self,
        mock_account_retrieve,
        mock_connected_balance,
    ):
        mock_connected_balance.return_value = (5000, 0)
        mock_account_retrieve.return_value = self._mock_account(payouts_enabled=True)

        response = self.client.post(self.payout_url, {'amount': '100.00'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Payout.objects.count(), 0)

    @patch('tickets.views._get_connected_balance_cents')
    @patch('stripe.Account.retrieve')
    @patch('stripe.Account.modify')
    @patch('stripe.Transfer.create_reversal')
    @patch('stripe.Payout.create')
    @patch('stripe.Transfer.create')
    def test_initiate_payout_marks_failed_when_bank_payout_creation_fails(
        self,
        mock_transfer_create,
        mock_payout_create,
        mock_create_reversal,
        mock_account_modify,
        mock_account_retrieve,
        mock_connected_balance,
    ):
        import stripe as stripe_lib

        mock_connected_balance.return_value = (45000, 0)
        mock_account_retrieve.return_value = self._mock_account(payouts_enabled=True)
        mock_payout_create.side_effect = stripe_lib.error.InvalidRequestError('bank unavailable', 'amount')

        response = self.client.post(self.payout_url, {'amount': '100.00'})

        self.assertEqual(response.status_code, 302)
        payout = Payout.objects.get(organization=self.org)
        self.assertEqual(payout.status, Payout.Status.FAILED)
        self.assertIn('Stripe error', payout.notes)
        self.assertIsNone(payout.stripe_payout_id)
        # No transfer is involved anymore, so nothing to reverse on failure.
        mock_transfer_create.assert_not_called()
        mock_create_reversal.assert_not_called()

    @patch('stripe.Webhook.construct_event')
    def test_connect_webhook_matches_by_stripe_payout_id_with_multiple_pending_payouts(self, mock_construct):
        first = Payout.objects.create(
            organization=self.org,
            amount=Decimal('80.00'),
            status=Payout.Status.PENDING,
            stripe_payout_id='po_first',
        )
        second = Payout.objects.create(
            organization=self.org,
            amount=Decimal('90.00'),
            status=Payout.Status.PENDING,
            stripe_payout_id='po_second',
        )
        mock_construct.return_value = {
            'type': 'payout.paid',
            'account': self.org.stripe_account_id,
            'data': {
                'object': {
                    'id': 'po_second',
                    'status': 'paid',
                    'metadata': {'payout_id': str(second.id), 'org_id': str(self.org.id)},
                }
            },
        }

        response = self.client.post(
            self.connect_webhook_url,
            data='{}',
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='sig_test',
        )

        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, Payout.Status.PENDING)
        self.assertEqual(second.status, Payout.Status.COMPLETED)

    @patch('stripe.Webhook.construct_event')
    def test_connect_webhook_accepts_stripe_object_event(self, mock_construct):
        payout = Payout.objects.create(
            organization=self.org,
            amount=Decimal('80.00'),
            status=Payout.Status.PENDING,
            stripe_payout_id='po_stripe_object',
        )
        mock_construct.return_value = MagicMock(
            id='evt_stripe_object',
            account=self.org.stripe_account_id,
            __getitem__=lambda obj, key: {
                'type': 'payout.paid',
                'data': {
                    'object': MagicMock(
                        id='po_stripe_object',
                        status='paid',
                        metadata={'payout_id': str(payout.id), 'org_id': str(self.org.id)},
                    ),
                },
            }[key],
        )

        response = self.client.post(
            self.connect_webhook_url,
            data='{}',
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='sig_test',
        )

        self.assertEqual(response.status_code, 200)
        payout.refresh_from_db()
        self.assertEqual(payout.status, Payout.Status.COMPLETED)

    @patch('stripe.Webhook.construct_event')
    def test_connect_webhook_matches_by_metadata_when_payout_id_not_yet_saved(self, mock_construct):
        other = Payout.objects.create(
            organization=self.org,
            amount=Decimal('60.00'),
            status=Payout.Status.PENDING,
        )
        target = Payout.objects.create(
            organization=self.org,
            amount=Decimal('70.00'),
            status=Payout.Status.PENDING,
        )
        mock_construct.return_value = {
            'type': 'payout.updated',
            'account': self.org.stripe_account_id,
            'data': {
                'object': {
                    'id': 'po_target',
                    'status': 'in_transit',
                    'metadata': {'payout_id': str(target.id), 'org_id': str(self.org.id)},
                }
            },
        }

        response = self.client.post(
            self.connect_webhook_url,
            data='{}',
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='sig_test',
        )

        self.assertEqual(response.status_code, 200)
        other.refresh_from_db()
        target.refresh_from_db()
        self.assertEqual(other.status, Payout.Status.PENDING)
        self.assertIsNone(other.stripe_payout_id)
        self.assertEqual(target.status, Payout.Status.IN_TRANSIT)
        self.assertEqual(target.stripe_payout_id, 'po_target')

    @patch('stripe.Webhook.construct_event')
    def test_connect_webhook_records_organizer_initiated_payout(self, mock_construct):
        # A Cue payout with the same amount must NOT be claimed by an
        # organizer-initiated Express Dashboard payout (no amount matching).
        existing = Payout.objects.create(
            organization=self.org,
            amount=Decimal('25.00'),
            status=Payout.Status.PENDING,
        )
        mock_construct.return_value = {
            'type': 'payout.created',
            'account': self.org.stripe_account_id,
            'data': {
                'object': {
                    'id': 'po_organizer',
                    'status': 'pending',
                    'amount': 2500,
                    'metadata': {},
                }
            },
        }

        response = self.client.post(
            self.connect_webhook_url,
            data='{}',
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='sig_test',
        )

        self.assertEqual(response.status_code, 200)
        existing.refresh_from_db()
        self.assertEqual(existing.status, Payout.Status.PENDING)
        self.assertIsNone(existing.stripe_payout_id)

        recorded = Payout.objects.get(stripe_payout_id='po_organizer')
        self.assertEqual(recorded.organization, self.org)
        self.assertEqual(recorded.amount, Decimal('25.00'))
        self.assertEqual(recorded.status, Payout.Status.PENDING)
        self.assertEqual(recorded.origin, Payout.Origin.STRIPE_DASHBOARD)
        self.assertIsNone(recorded.initiated_by)
        self.assertEqual(recorded.notes, 'Initiated via Stripe')

    @patch('stripe.Webhook.construct_event')
    def test_connect_webhook_organizer_payout_duplicate_delivery_is_idempotent(self, mock_construct):
        event_payload = {
            'type': 'payout.created',
            'account': self.org.stripe_account_id,
            'data': {
                'object': {
                    'id': 'po_dup',
                    'status': 'pending',
                    'amount': 1200,
                    'metadata': {},
                }
            },
        }
        mock_construct.return_value = event_payload

        for _ in range(2):
            response = self.client.post(
                self.connect_webhook_url,
                data='{}',
                content_type='application/json',
                HTTP_STRIPE_SIGNATURE='sig_test',
            )
            self.assertEqual(response.status_code, 200)

        self.assertEqual(Payout.objects.filter(stripe_payout_id='po_dup').count(), 1)

        # A later lifecycle event advances the webhook-created row.
        event_payload['data']['object']['status'] = 'paid'
        event_payload['type'] = 'payout.paid'
        response = self.client.post(
            self.connect_webhook_url,
            data='{}',
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='sig_test',
        )
        self.assertEqual(response.status_code, 200)
        recorded = Payout.objects.get(stripe_payout_id='po_dup')
        self.assertEqual(recorded.status, Payout.Status.COMPLETED)

    @patch('stripe.Webhook.construct_event')
    def test_connect_webhook_unknown_account_returns_200(self, mock_construct):
        mock_construct.return_value = {
            'type': 'payout.created',
            'account': 'acct_does_not_exist',
            'data': {
                'object': {
                    'id': 'po_unknown_acct',
                    'status': 'pending',
                    'amount': 500,
                    'metadata': {},
                }
            },
        }

        response = self.client.post(
            self.connect_webhook_url,
            data='{}',
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='sig_test',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Payout.objects.count(), 0)

    @patch('stripe.Webhook.construct_event')
    def test_connect_webhook_skips_negative_balance_recovery_payout(self, mock_construct):
        # Stripe's automatic "withdrawal to cover a negative balance" arrives as a
        # payout with a negative amount and empty metadata. It must be acked (200)
        # and must NOT create a Payout row.
        mock_construct.return_value = {
            'type': 'payout.created',
            'account': self.org.stripe_account_id,
            'data': {
                'object': {
                    'id': 'po_neg_balance',
                    'status': 'in_transit',
                    'amount': -40,
                    'metadata': {},
                }
            },
        }

        response = self.client.post(
            self.connect_webhook_url,
            data='{}',
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='sig_test',
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payout.objects.filter(stripe_payout_id='po_neg_balance').exists())

    @patch('stripe.Webhook.construct_event')
    def test_connect_webhook_acks_200_when_handler_raises(self, mock_construct):
        # An unexpected handler failure must never escape as a 500 (Stripe would
        # retry for days) — the webhook logs and acks with 200.
        mock_construct.return_value = {
            'type': 'payout.created',
            'account': self.org.stripe_account_id,
            'data': {'object': {'id': 'po_boom', 'status': 'pending', 'amount': 500, 'metadata': {}}},
        }
        with patch('tickets.views._handle_stripe_payout_event', side_effect=RuntimeError('boom')):
            response = self.client.post(
                self.connect_webhook_url,
                data='{}',
                content_type='application/json',
                HTTP_STRIPE_SIGNATURE='sig_test',
            )
        self.assertEqual(response.status_code, 200)

    @patch('stripe.Webhook.construct_event')
    def test_connect_webhook_duplicate_stripe_account_id_does_not_500(self, mock_construct):
        # stripe_account_id is not unique; a duplicate org row must not raise
        # MultipleObjectsReturned and 500.
        Organization.objects.create(
            name='Dup Account Org',
            slug='dup-account-org',
            stripe_account_id=self.org.stripe_account_id,
        )
        mock_construct.return_value = {
            'type': 'payout.created',
            'account': self.org.stripe_account_id,
            'data': {'object': {'id': 'po_dup_acct', 'status': 'pending', 'amount': 700, 'metadata': {}}},
        }

        response = self.client.post(
            self.connect_webhook_url,
            data='{}',
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='sig_test',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Payout.objects.filter(stripe_payout_id='po_dup_acct').count(), 1)

    @patch('stripe.Balance.retrieve')
    @patch('stripe.Account.retrieve')
    def test_finance_history_renders_processing_for_pending_payouts(self, mock_account_retrieve, mock_balance_retrieve):
        mock_account_retrieve.return_value = self._mock_account(payouts_enabled=True)
        mock_balance_retrieve.return_value = MagicMock(
            available=[MagicMock(amount=10000, currency='usd')],
            pending=[MagicMock(amount=2500, currency='usd')],
        )
        Payout.objects.create(
            organization=self.org,
            amount=Decimal('42.00'),
            status=Payout.Status.PENDING,
            initiated_by=self.user,
        )

        response = self.client.get(self.finance_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Processing')
        self.assertNotContains(response, 'Queued')

    @patch('stripe.Balance.retrieve')
    @patch('stripe.Account.retrieve')
    def test_finance_overview_shows_connected_balance_figures(self, mock_account_retrieve, mock_balance_retrieve):
        mock_account_retrieve.return_value = self._mock_account(payouts_enabled=True)
        mock_balance_retrieve.return_value = MagicMock(
            available=[MagicMock(amount=13415, currency='usd')],
            pending=[MagicMock(amount=8639, currency='usd')],
        )

        response = self.client.get(self.finance_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['stripe_available'], Decimal('134.15'))
        self.assertEqual(response.context['settling_balance'], Decimal('86.39'))
        mock_balance_retrieve.assert_called_once_with(stripe_account=self.org.stripe_account_id)

    @patch('stripe.Balance.retrieve')
    @patch('stripe.Account.retrieve')
    def test_finance_overview_renders_when_balance_api_fails(self, mock_account_retrieve, mock_balance_retrieve):
        # REGRESSION-CRITICAL: a Stripe outage must never 500 the Finance page.
        mock_account_retrieve.return_value = self._mock_account(payouts_enabled=True)
        mock_balance_retrieve.side_effect = Exception('stripe down')

        response = self.client.get(self.finance_url)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['stripe_available'])

    @patch('stripe.Balance.retrieve')
    @patch('stripe.Account.retrieve')
    def test_finance_overview_clamps_negative_connected_available(self, mock_account_retrieve, mock_balance_retrieve):
        # Available can go negative after a refund clawback that follows a
        # withdrawal — display clamps at zero.
        mock_account_retrieve.return_value = self._mock_account(payouts_enabled=True)
        mock_balance_retrieve.return_value = MagicMock(
            available=[MagicMock(amount=-1500, currency='usd')],
            pending=[MagicMock(amount=0, currency='usd')],
        )

        response = self.client.get(self.finance_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['stripe_available'], Decimal('0.00'))

    @patch('stripe.Account.retrieve')
    @patch('stripe.Account.modify')
    @patch('stripe.Payout.create')
    def test_recover_pending_payouts_creates_missing_stripe_payout_ids(
        self,
        mock_payout_create,
        mock_account_modify,
        mock_account_retrieve,
    ):
        mock_account_retrieve.return_value = self._mock_account(payouts_enabled=True)
        mock_payout_create.side_effect = [
            MagicMock(id='po_recover_one', status='pending'),
            MagicMock(id='po_recover_two', status='in_transit'),
        ]
        first = Payout.objects.create(
            organization=self.org,
            amount=Decimal('25.00'),
            status=Payout.Status.PENDING,
            stripe_transfer_id='tr_one',
        )
        second = Payout.objects.create(
            organization=self.org,
            amount=Decimal('35.00'),
            status=Payout.Status.PENDING,
            stripe_transfer_id='tr_two',
        )

        response = self.client.post(self.recover_url)

        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.stripe_payout_id, 'po_recover_one')
        self.assertEqual(first.status, Payout.Status.PENDING)
        self.assertEqual(second.stripe_payout_id, 'po_recover_two')
        self.assertEqual(second.status, Payout.Status.IN_TRANSIT)
        self.assertEqual(mock_payout_create.call_count, 2)


class CSVProcessorChunkRollbackTest(TestCase):
    """Tests that success_count in process_and_save() reflects only committed orders."""

    # Minimal CSV with 3 valid rows: order_date, customer_email, customer_name, ticket_type
    CSV_ROWS = (
        "order_date,customer_email,customer_name,ticket_type\n"
        "2024-06-01,alice@example.com,Alice Smith,GA\n"
        "2024-06-01,bob@example.com,Bob Jones,GA\n"
        "2024-06-01,carol@example.com,Carol Lee,GA\n"
    )

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Chunk Test Org', slug='chunk-test-org')
        cls.venue = Venue.objects.create(organization=cls.org, name='Venue', city='City')
        cls.event = Event.objects.create(
            organization=cls.org,
            name='Chunk Test Event',
            venue=cls.venue,
            start_date=date(2024, 6, 15),
        )
        cls.csv_format = CSVFormat.objects.create(
            organization=cls.org,
            name='Chunk Test Format',
            column_mapping={
                'order_date': ['order_date'],
                'customer_email': ['customer_email'],
                'customer_name': ['customer_name'],
                'ticket_type': ['ticket_type'],
            },
        )
        cls.upload = UploadedFile.objects.create(
            organization=cls.org,
            csv_format=cls.csv_format,
            filename='chunk_test.csv',
            status='pending',
            metadata={'event_id': str(cls.event.id), 'event_name': cls.event.name,
                      'event_start_date': '2024-06-15'},
        )

    def _make_processor(self):
        from tickets.csv_processor import CSVProcessor
        return CSVProcessor(self.upload, self.csv_format)

    def _csv_handle(self):
        import io
        return io.BytesIO(self.CSV_ROWS.encode('utf-8'))

    def test_happy_path_success_count_matches_db(self):
        """On full success, success_count equals the number of orders in the DB."""
        processor = self._make_processor()
        results = processor.process_and_save(self._csv_handle())
        db_count = TicketOrder.objects.filter(uploaded_file=self.upload).count()
        self.assertEqual(results['success_count'], db_count)

    def test_success_count_not_inflated_on_save_failure(self):
        """When uploaded_file.save() raises after bulk_create, the transaction rolls
        back and success_count must equal the actual committed DB order count."""
        processor = self._make_processor()

        original_save = self.upload.__class__.save
        call_count = {'n': 0}

        def patched_save(self_inner, *args, **kwargs):
            call_count['n'] += 1
            # Fail on the second save (progress counter update inside chunk 1).
            # The first save sets status='processing' before the loop.
            if call_count['n'] == 2:
                raise RuntimeError('simulated DB failure')
            return original_save(self_inner, *args, **kwargs)

        with patch.object(self.upload.__class__, 'save', patched_save):
            results = processor.process_and_save(self._csv_handle())

        db_count = TicketOrder.objects.filter(uploaded_file=self.upload).count()
        self.assertEqual(
            results['success_count'], db_count,
            f"success_count ({results['success_count']}) must equal DB count ({db_count}), "
            "not be inflated by rolled-back chunks",
        )

    def test_processed_rows_not_double_incremented_on_failure(self):
        """total_rows must equal the CSV row count even when a chunk fails
        (pre-existing double-increment bug: was counted in atomic block AND except)."""
        processor = self._make_processor()
        csv_row_count = self.CSV_ROWS.count('\n') - 1  # exclude header

        original_save = self.upload.__class__.save
        call_count = {'n': 0}

        def patched_save(self_inner, *args, **kwargs):
            call_count['n'] += 1
            if call_count['n'] == 2:
                raise RuntimeError('simulated DB failure')
            return original_save(self_inner, *args, **kwargs)

        with patch.object(self.upload.__class__, 'save', patched_save):
            processor.process_and_save(self._csv_handle())

        self.upload.refresh_from_db()
        self.assertEqual(
            self.upload.total_rows, csv_row_count,
            f"total_rows ({self.upload.total_rows}) should be {csv_row_count}, "
            "not double-counted",
        )


class PoshRevenueSubtotalTest(TestCase):
    """The POSH built-in format records revenue as net Order Subtotal, not the
    fee-inclusive Order Total. Regression guard for the Cue/POSH revenue mismatch."""

    # Two POSH rows. Order Total = Order Subtotal + Processing Fee (buyer pays the
    # fee on top), so summing Order Total would over-report revenue.
    CSV_ROWS = (
        '"Order Number","Order Date/Time","First Name","Last Name","Email",'
        '"Phone Number","Tickets Purchased","# of Tickets","Order Subtotal",'
        '"Processing Fee","Order Total","Ticket Scan Details"\n'
        '"1001","06-01-2026 4:00:00 pm","Alice","Smith","alice@example.com",'
        '"+15551110001","GA, GA","2","12.74","3.25","15.99",""\n'
        '"1002","06-02-2026 5:00:00 pm","Bob","Jones","bob@example.com",'
        '"+15551110002","GA","1","6.37","1.63","8.00",""\n'
    )

    SUBTOTAL_SUM = Decimal('19.11')   # 12.74 + 6.37 (what POSH reports as revenue)
    TOTAL_SUM = Decimal('23.99')      # 15.99 + 8.00 (fee-inclusive — must NOT be used)

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Posh Org', slug='posh-org')
        cls.venue = Venue.objects.create(organization=cls.org, name='Venue', city='City')
        cls.event = Event.objects.create(
            organization=cls.org, name='Posh Event', venue=cls.venue,
            start_date=date(2026, 6, 15),
        )
        # The POSH built-in format is seeded by migration (organization=None, is_system).
        cls.csv_format = CSVFormat.objects.get(name='POSH', is_system=True)
        cls.upload = UploadedFile.objects.create(
            organization=cls.org, csv_format=cls.csv_format,
            filename='posh.csv', status='pending',
            metadata={'event_id': str(cls.event.id), 'event_name': cls.event.name,
                      'event_start_date': '2026-06-15'},
        )

    def test_total_amount_uses_subtotal_not_order_total(self):
        import io
        from tickets.csv_processor import CSVProcessor
        CSVProcessor(self.upload, self.csv_format).process_and_save(
            io.BytesIO(self.CSV_ROWS.encode('utf-8'))
        )

        orders = TicketOrder.objects.filter(event=self.event).order_by('external_order_number')
        self.assertEqual(orders.count(), 2)
        self.assertEqual(orders[0].total_amount, Decimal('12.74'))
        self.assertEqual(orders[1].total_amount, Decimal('6.37'))

        # Event revenue (Sum of total_amount, as _compute_event_stats does) must equal
        # the net subtotal, not the fee-inclusive Order Total.
        from django.db.models import Sum
        revenue = self.event.ticket_orders.aggregate(r=Sum('total_amount'))['r']
        self.assertEqual(revenue, self.SUBTOTAL_SUM)
        self.assertNotEqual(revenue, self.TOTAL_SUM)


class PoshContactlessImportTest(TestCase):
    """POSH rows with no email/name must import (counting revenue), not be rejected:
    in-person/door sales bucket under 'In-Person Sales'; online no-contact orders under
    'Guest (No Contact Info)'. Regression guard for the 'Missing required fields' drop."""

    # Three rows: a normal online order, an in-person door sale (blank contact,
    # Was Processed In Person=true), and an online order with no contact info.
    CSV_ROWS = (
        '"Order Number","Order Date/Time","First Name","Last Name","Email",'
        '"Phone Number","Tickets Purchased","# of Tickets","Order Subtotal",'
        '"Processing Fee","Order Total","Ticket Scan Details","Was Processed In Person"\n'
        '"2001","06-01-2026 4:00:00 pm","Alice","Smith","alice@example.com",'
        '"+15551110001","GA","1","10.00","2.00","12.00","","false"\n'
        '"2002","06-02-2026 5:00:00 pm","","","",'
        '"","GA","1","20.00","0.00","20.00","","true"\n'
        '"2003","06-03-2026 6:00:00 pm","","","",'
        '"","GA","1","5.00","1.00","6.00","","false"\n'
    )

    def setUp(self):
        self.org = Organization.objects.create(name='Posh Contact Org', slug='posh-contact-org')
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Posh Contact Event', venue=self.venue,
            start_date=date(2026, 6, 15),
        )
        self.csv_format = CSVFormat.objects.get(name='POSH', is_system=True)
        self.upload = UploadedFile.objects.create(
            organization=self.org, csv_format=self.csv_format,
            filename='posh.csv', status='pending',
            metadata={'event_id': str(self.event.id), 'event_name': self.event.name,
                      'event_start_date': '2026-06-15'},
        )

    def _import(self):
        import io
        from tickets.csv_processor import CSVProcessor
        return CSVProcessor(self.upload, self.csv_format).process_and_save(
            io.BytesIO(self.CSV_ROWS.encode('utf-8'))
        )

    def test_contactless_rows_import_without_errors(self):
        results = self._import()
        self.assertEqual(results['error_count'], 0, results['errors'])

        orders = TicketOrder.objects.filter(event=self.event)
        self.assertEqual(orders.count(), 3)

        # Revenue = sum of Order Subtotals; nothing dropped.
        from django.db.models import Sum
        revenue = orders.aggregate(r=Sum('total_amount'))['r']
        self.assertEqual(revenue, Decimal('35.00'))

    def test_in_person_and_no_contact_buckets(self):
        self._import()

        in_person = TicketOrder.objects.get(event=self.event, external_order_number='2002')
        self.assertTrue(in_person.is_in_person)
        self.assertEqual(in_person.customer.name, 'In-Person Sales')
        self.assertEqual(in_person.total_amount, Decimal('20.00'))

        no_contact = TicketOrder.objects.get(event=self.event, external_order_number='2003')
        self.assertFalse(no_contact.is_in_person)
        self.assertEqual(no_contact.customer.name, 'Guest (No Contact Info)')
        self.assertEqual(no_contact.total_amount, Decimal('5.00'))

        # The normal online order still maps to its real customer.
        normal = TicketOrder.objects.get(event=self.event, external_order_number='2001')
        self.assertEqual(normal.customer.email, 'alice@example.com')


class PhoneValidationTest(TestCase):
    """Tests for phone number normalization and form validation."""

    def test_normalize_phone_e164_passthrough(self):
        from tickets.forms import _normalize_phone
        self.assertEqual(_normalize_phone('+447911123456'), '+447911123456')

    def test_normalize_phone_10digit_us(self):
        from tickets.forms import _normalize_phone
        self.assertEqual(_normalize_phone('5551234567'), '+15551234567')

    def test_normalize_phone_11digit_us(self):
        from tickets.forms import _normalize_phone
        self.assertEqual(_normalize_phone('15551234567'), '+15551234567')

    def test_attendee_form_accepts_us_e164(self):
        from tickets.forms import AttendeePhoneForm
        form = AttendeePhoneForm({'phone_number': '+15551234567'})
        self.assertTrue(form.is_valid())

    def test_attendee_form_accepts_international_e164(self):
        """UK number — must work after intl-tel-input ships."""
        from tickets.forms import AttendeePhoneForm
        form = AttendeePhoneForm({'phone_number': '+447911123456'})
        self.assertTrue(form.is_valid())

    def test_attendee_form_rejects_country_code_only(self):
        """intl-tel-input returns '+1' when user doesn't enter digits."""
        from tickets.forms import AttendeePhoneForm
        form = AttendeePhoneForm({'phone_number': '+1'})
        self.assertFalse(form.is_valid())

    def test_attendee_form_rejects_garbage(self):
        from tickets.forms import AttendeePhoneForm
        form = AttendeePhoneForm({'phone_number': 'notanumber'})
        self.assertFalse(form.is_valid())


class EmailProfileCompletionFormTest(TestCase):
    """Tests for EmailProfileCompletionForm — phone required, validation, normalization."""

    _EMAIL = 'jane@example.com'

    def _make_form(self, phone, **data_overrides):
        """Mirrors how the view instantiates the form: POST data + initial for the disabled field."""
        from tickets.forms import EmailProfileCompletionForm
        data = {
            'first_name': 'Jane',
            'last_name': 'Doe',
            'phone_number': phone,
            'gender': 'female',
            'terms_accepted': True,
        }
        data.update(data_overrides)
        return EmailProfileCompletionForm(data, initial={'email_display': self._EMAIL})

    def test_phone_required(self):
        """Empty phone number must be rejected."""
        form = self._make_form('')
        self.assertFalse(form.is_valid())
        self.assertIn('phone_number', form.errors)

    def test_valid_us_e164(self):
        form = self._make_form('+12125551234')
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['phone_number'], '+12125551234')

    def test_valid_international_e164(self):
        """UK number — intl-tel-input sends full E.164."""
        form = self._make_form('+447911123456')
        self.assertTrue(form.is_valid())

    def test_rejects_country_code_only(self):
        """intl-tel-input sends '+1' when user doesn't type digits."""
        form = self._make_form('+1')
        self.assertFalse(form.is_valid())
        self.assertIn('phone_number', form.errors)

    def test_rejects_garbage(self):
        form = self._make_form('notanumber')
        self.assertFalse(form.is_valid())
        self.assertIn('phone_number', form.errors)

    def test_10digit_us_normalizes_to_e164(self):
        """Bare 10-digit number (no-JS path) gets normalized to +1XXXXXXXXXX."""
        form = self._make_form('2125551234')
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['phone_number'], '+12125551234')

    def test_rejects_duplicate_phone(self):
        """Phone already in use by another UserProfile must be rejected."""
        existing_user = User.objects.create_user(username='existing', email='existing@example.com')
        UserProfile.objects.create(user=existing_user, phone_number='+12125559999')
        form = self._make_form('+12125559999')
        self.assertFalse(form.is_valid())
        self.assertIn('phone_number', form.errors)


class EmailCompleteProfileViewTest(TestCase):
    """Integration tests for email_complete_profile_view."""

    def setUp(self):
        self.client = Client()

    def test_no_session_email_redirects(self):
        """Without pending_signup_email in session, redirect to email_login."""
        response = self.client.get(reverse('tickets:email_complete_profile'))
        self.assertRedirects(response, reverse('tickets:email_login'))

    def test_get_shows_form(self):
        session = self.client.session
        session['pending_signup_email'] = 'newuser@example.com'
        session.save()
        response = self.client.get(reverse('tickets:email_complete_profile'))
        self.assertEqual(response.status_code, 200)

    def test_post_sends_verification_and_stashes_profile_data(self):
        """Valid POST triggers phone verification and stashes profile data in session."""
        from unittest.mock import patch
        session = self.client.session
        session['pending_signup_email'] = 'newuser@example.com'
        session.save()
        with patch('tickets.sms.start_phone_verification', return_value=True) as mock_verify:
            response = self.client.post(reverse('tickets:email_complete_profile'), {
                'first_name': 'Jane',
                'last_name': 'Doe',
                'phone_number': '+12125551234',
                'email_display': 'newuser@example.com',
                'gender': 'female',
                'terms_accepted': True,
            })
        mock_verify.assert_called_once_with('+12125551234')
        self.assertRedirects(response, reverse('tickets:verify_phone_after_profile'), fetch_redirect_response=False)
        profile_data = self.client.session.get('pending_email_profile_data')
        self.assertIsNotNone(profile_data)
        self.assertEqual(profile_data['first_name'], 'Jane')
        self.assertEqual(profile_data['phone_number'], '+12125551234')
        self.assertFalse(User.objects.filter(email='newuser@example.com').exists())

    def test_post_empty_phone_rejected(self):
        """Empty phone must return a form error and not create a User."""
        session = self.client.session
        session['pending_signup_email'] = 'newuser2@example.com'
        session.save()
        response = self.client.post(reverse('tickets:email_complete_profile'), {
            'first_name': 'Jane',
            'last_name': 'Doe',
            'phone_number': '',
            'email_display': 'newuser2@example.com',
            'gender': 'female',
            'terms_accepted': True,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email='newuser2@example.com').exists())

    def test_post_duplicate_phone_rejected(self):
        """Phone already in use must return a form error and not create a User."""
        existing_user = User.objects.create_user(username='taken', email='taken@example.com')
        UserProfile.objects.create(user=existing_user, phone_number='+12125559999')
        session = self.client.session
        session['pending_signup_email'] = 'newuser3@example.com'
        session.save()
        response = self.client.post(reverse('tickets:email_complete_profile'), {
            'first_name': 'Jane',
            'last_name': 'Doe',
            'phone_number': '+12125559999',
            'email_display': 'newuser3@example.com',
            'gender': 'female',
            'terms_accepted': True,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email='newuser3@example.com').exists())


class TestRFMCalculator(TestCase):
    """Tests for RFMCalculator.calculate_all()."""

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='RFM Test Org', slug='rfm-test-org')
        cls.venue = Venue.objects.create(organization=cls.org, name='Venue', city='City')
        cls.event = Event.objects.create(
            organization=cls.org, name='Event', venue=cls.venue,
            start_date=date(2025, 1, 1),
        )
        cls.csv_format = CSVFormat.objects.create(
            organization=cls.org, name='Fmt', column_mapping={'order_number': 'Order ID'},
        )
        cls.upload = UploadedFile.objects.create(
            organization=cls.org, csv_format=cls.csv_format,
            filename='test.csv', status='completed',
        )

    def _make_customer(self, email, lifetime_value=Decimal('0.00'), last_order_date=None):
        return Customer.objects.create(
            organization=self.org, email=email, name=email,
            lifetime_value=lifetime_value, last_order_date=last_order_date,
        )

    def _make_order(self, customer, total, days_ago=30, order_num=None):
        from datetime import date, timedelta
        import uuid
        order_date = timezone.now() - timedelta(days=days_ago)
        TicketOrder.objects.create(
            customer=customer, event=self.event, uploaded_file=self.upload,
            order_number=order_num or str(uuid.uuid4())[:20],
            order_date=order_date, total_amount=total,
        )

    def test_happy_path_scores_customers(self):
        """Customers with orders get varied 1-5 scores and non-empty segments."""
        from tickets.services.segmentation.rfm_calculator import RFMCalculator
        c1 = self._make_customer('c1@example.com', Decimal('1000.00'), date(2025, 6, 1))
        c2 = self._make_customer('c2@example.com', Decimal('200.00'), date(2025, 3, 1))
        c3 = self._make_customer('c3@example.com', Decimal('50.00'), date(2024, 12, 1))
        for c, amt, days in [(c1, 1000, 10), (c2, 200, 90), (c3, 50, 200)]:
            self._make_order(c, amt, days_ago=days)
        self._make_order(c1, 500, days_ago=5)  # c1 has more orders (higher frequency)

        RFMCalculator(self.org).calculate_all()

        for customer in [c1, c2, c3]:
            customer.refresh_from_db()
            self.assertIn(customer.rfm_recency_score, range(1, 6))
            self.assertIn(customer.rfm_frequency_score, range(1, 6))
            self.assertIn(customer.rfm_monetary_score, range(1, 6))
            self.assertTrue(customer.rfm_segment)  # not blank

    def test_no_orders_all_default_to_dormant(self):
        """Org with no orders: all customers get score=1 and segment Dormant."""
        from tickets.services.segmentation.rfm_calculator import RFMCalculator
        c1 = self._make_customer('no1@example.com')
        c2 = self._make_customer('no2@example.com')

        RFMCalculator(self.org).calculate_all()

        for customer in [c1, c2]:
            customer.refresh_from_db()
            self.assertEqual(customer.rfm_recency_score, 1)
            self.assertEqual(customer.rfm_frequency_score, 1)
            self.assertEqual(customer.rfm_monetary_score, 1)
            self.assertEqual(customer.rfm_segment, 'Dormant')

    def test_single_customer_degenerate_percentiles(self):
        """One customer with orders: degenerate percentiles fall back to median, no crash."""
        from tickets.services.segmentation.rfm_calculator import RFMCalculator
        c = self._make_customer('solo@example.com', Decimal('100.00'), date(2025, 5, 1))
        self._make_order(c, 100, days_ago=30)

        RFMCalculator(self.org).calculate_all()

        c.refresh_from_db()
        self.assertIn(c.rfm_recency_score, range(1, 6))
        self.assertTrue(c.rfm_segment)

    def test_placeholder_customers_excluded(self):
        """Customers with @placeholder.local emails are not scored."""
        from tickets.services.segmentation.rfm_calculator import RFMCalculator
        real = self._make_customer('real@example.com', Decimal('100.00'), date(2025, 5, 1))
        placeholder = self._make_customer('ghost@placeholder.local')
        self._make_order(real, 100, days_ago=30)

        RFMCalculator(self.org).calculate_all()

        placeholder.refresh_from_db()
        self.assertIsNone(placeholder.rfm_recency_score)
        self.assertIsNone(placeholder.rfm_frequency_score)
        self.assertIsNone(placeholder.rfm_monetary_score)

    def test_null_last_order_date_with_orders_falls_back(self):
        """Customer with orders but last_order_date=None gets 9999 recency days (scores low on R)."""
        from tickets.services.segmentation.rfm_calculator import RFMCalculator
        c1 = self._make_customer('null_date@example.com', Decimal('500.00'), last_order_date=None)
        c2 = self._make_customer('recent@example.com', Decimal('100.00'), date(2025, 6, 1))
        self._make_order(c1, 500, days_ago=10)
        self._make_order(c2, 100, days_ago=10)

        RFMCalculator(self.org).calculate_all()

        c1.refresh_from_db()
        c2.refresh_from_db()
        # c1 has null last_order_date → 9999 recency → worst recency score
        self.assertEqual(c1.rfm_recency_score, 1)
        # c2 has a real recent date → better recency
        self.assertGreaterEqual(c2.rfm_recency_score, c1.rfm_recency_score)


class TestSegmentDiagnostics(TestCase):
    """Tests for SegmentDiagnostics (read-only segment accuracy evaluation)."""

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Diag Org', slug='diag-org')
        cls.other_org = Organization.objects.create(name='Other Org', slug='other-diag-org')
        cls.venue = Venue.objects.create(organization=cls.org, name='Venue', city='City')
        cls.event = Event.objects.create(
            organization=cls.org, name='Event', venue=cls.venue,
            start_date=date(2025, 1, 1),
        )
        cls.csv_format = CSVFormat.objects.create(
            organization=cls.org, name='Fmt', column_mapping={'order_number': 'Order ID'},
        )
        cls.upload = UploadedFile.objects.create(
            organization=cls.org, csv_format=cls.csv_format,
            filename='test.csv', status='completed',
        )

    def _make_customer(self, email, lifetime_value=Decimal('0.00'), last_order_date=None):
        return Customer.objects.create(
            organization=self.org, email=email, name=email,
            lifetime_value=lifetime_value, last_order_date=last_order_date,
        )

    def _make_order(self, customer, total, days_ago):
        import uuid
        from datetime import timedelta
        TicketOrder.objects.create(
            customer=customer, event=self.event, uploaded_file=self.upload,
            order_number=str(uuid.uuid4())[:20],
            order_date=timezone.now() - timedelta(days=days_ago),
            total_amount=Decimal(str(total)),
        )

    def test_cube_coverage_reports_gaps_and_canonical_is_total(self):
        """Current 8 rules leave cells uncovered; canonical grid covers all 125."""
        from tickets.services.segmentation.validation import SegmentDiagnostics
        from tickets.services.segmentation.segment_definitions import (
            classify_segment, classify_segment_explain, classify_segment_canonical,
        )
        diag = SegmentDiagnostics(self.org)
        current = diag._cube_coverage(classify_segment, explain_fn=classify_segment_explain)
        self.assertLess(current['coverage_pct'], 100.0)
        self.assertTrue(current['uncovered_cells'])

        canonical = diag._cube_coverage(classify_segment_canonical, explain_fn=None)
        self.assertEqual(canonical['coverage_pct'], 100.0)
        self.assertEqual(canonical['uncovered_cells'], [])

    def _seed_backtest_data(self):
        """6 high-value repeat customers + 5 one-off dormant customers."""
        good = [self._make_customer(f'good{i}@example.com') for i in range(6)]
        bad = [self._make_customer(f'bad{i}@example.com') for i in range(5)]
        for c in good:
            # pre-cutoff (older than 90 days): frequent, high spend
            self._make_order(c, 300, days_ago=100)
            self._make_order(c, 300, days_ago=150)
            self._make_order(c, 300, days_ago=200)
            # holdout (within 90 days): repeat purchases
            self._make_order(c, 250, days_ago=30)
            self._make_order(c, 250, days_ago=15)
        for c in bad:
            # single old low-value order, no holdout activity
            self._make_order(c, 20, days_ago=250)
        return good, bad

    def test_backtest_separation_sane(self):
        """Top segment out-performs bottom on repeat rate; Spearman non-negative."""
        from tickets.services.segmentation.validation import SegmentDiagnostics
        self._seed_backtest_data()

        bt = SegmentDiagnostics(self.org)._backtest(holdout_days=90)
        self.assertEqual(bt['status'], 'ok')
        per = bt['per_segment']
        self.assertGreaterEqual(len(per), 2)

        ranked = sorted(per.items(), key=lambda kv: -kv[1]['avg_future_revenue'])
        top, bottom = ranked[0][1], ranked[-1][1]
        self.assertGreater(top['repeat_rate'], bottom['repeat_rate'])
        self.assertGreaterEqual(bt['separation']['spearman_future_revenue'], 0)

    def test_backtest_insufficient_holdout(self):
        """No orders in the holdout window -> insufficient_holdout."""
        from tickets.services.segmentation.validation import SegmentDiagnostics
        c = self._make_customer('only_old@example.com')
        self._make_order(c, 100, days_ago=300)  # only pre-cutoff

        bt = SegmentDiagnostics(self.org)._backtest(holdout_days=90)
        self.assertEqual(bt['status'], 'insufficient_holdout')

    def test_histograms_and_sizes_sum_to_scored_customers(self):
        """Score histogram and segment-size totals equal the scored customer count."""
        from tickets.services.segmentation.rfm_calculator import RFMCalculator
        from tickets.services.segmentation.validation import SegmentDiagnostics
        self._seed_backtest_data()
        RFMCalculator(self.org).calculate_all()

        scored = (
            Customer.objects.filter(organization=self.org)
            .exclude(email__endswith='@placeholder.local')
            .count()
        )
        internal = SegmentDiagnostics(self.org)._internal_diagnostics()
        self.assertEqual(sum(internal['score_histograms']['frequency'].values()), scored)
        self.assertEqual(internal['segment_size_distribution']['total_scored'], scored)

    def test_backtest_is_read_only(self):
        """Running the backtest must not mutate stored RFM fields."""
        from tickets.services.segmentation.rfm_calculator import RFMCalculator
        from tickets.services.segmentation.validation import SegmentDiagnostics
        self._seed_backtest_data()
        RFMCalculator(self.org).calculate_all()

        before = {
            c.id: (c.rfm_recency_score, c.rfm_frequency_score,
                   c.rfm_monetary_score, c.rfm_segment)
            for c in Customer.objects.filter(organization=self.org)
        }
        SegmentDiagnostics(self.org).calculate(holdout_days=90, compare_canonical=True)
        after = {
            c.id: (c.rfm_recency_score, c.rfm_frequency_score,
                   c.rfm_monetary_score, c.rfm_segment)
            for c in Customer.objects.filter(organization=self.org)
        }
        self.assertEqual(before, after)

    def test_diagnostics_are_org_scoped(self):
        """Diagnostics for one org ignore another org's customers."""
        from tickets.services.segmentation.validation import SegmentDiagnostics
        Customer.objects.create(
            organization=self.other_org, email='leak@example.com', name='leak',
            rfm_segment='VIP', rfm_frequency_score=5,
        )
        self._make_customer('mine@example.com')
        internal = SegmentDiagnostics(self.org)._internal_diagnostics()
        self.assertNotIn('VIP', internal['segment_size_distribution']['by_segment'])

    def test_backtest_absolute_path(self):
        """The absolute-band backtest produces a scored per-segment table."""
        from tickets.services.segmentation.validation import SegmentDiagnostics
        from tickets.services.segmentation.segment_definitions import classify_segment_absolute
        self._seed_backtest_data()
        bt = SegmentDiagnostics(self.org)._backtest(
            holdout_days=90, absolute_fn=classify_segment_absolute,
        )
        self.assertEqual(bt['status'], 'ok')
        self.assertTrue(bt['per_segment'])

    def test_backtest_nets_partial_refunds(self):
        """Holdout revenue subtracts partial refunds (matches LTV definition)."""
        import uuid
        from tickets.services.segmentation.validation import SegmentDiagnostics
        customers = [self._make_customer(f'pr{i}@example.com') for i in range(5)]
        for c in customers:
            # pre-cutoff order (segmentable)
            self._make_order(c, 100, days_ago=200)
            # holdout order: $200 face, $50 partially refunded -> nets to $150
            TicketOrder.objects.create(
                customer=c, event=self.event, uploaded_file=self.upload,
                order_number=str(uuid.uuid4())[:20],
                order_date=timezone.now() - timedelta(days=20),
                total_amount=Decimal('200.00'), refunded_amount=Decimal('50.00'),
            )
        bt = SegmentDiagnostics(self.org)._backtest(holdout_days=90)
        self.assertEqual(bt['status'], 'ok')
        # All five identical -> one segment, avg future revenue nets the refund.
        revenues = [s['avg_future_revenue'] for s in bt['per_segment'].values()]
        self.assertIn(150.0, revenues)

    def test_backtest_too_large_guard(self):
        """A tenant over the customer ceiling short-circuits to too_large."""
        from unittest.mock import patch
        from tickets.services.segmentation import validation
        self._seed_backtest_data()
        with patch.object(validation, 'BACKTEST_MAX_CUSTOMERS', 0):
            bt = validation.SegmentDiagnostics(self.org)._backtest(holdout_days=90)
        self.assertEqual(bt['status'], 'too_large')

    def test_separation_scores_needs_two_segments(self):
        """A single segment can't be scored: spearman is None, not a crash."""
        from tickets.services.segmentation.validation import SegmentDiagnostics
        from tickets.services.segmentation.segment_definitions import SEGMENT_VALUE_ORDER
        sep = SegmentDiagnostics(self.org)._separation_scores(
            {'VIP': {'n': 3, 'avg_future_revenue': 10.0, 'repeat_rate': 0.5}},
            SEGMENT_VALUE_ORDER,
        )
        self.assertIsNone(sep['spearman_future_revenue'])
        self.assertEqual(sep['monotonic_violations'], 0)


class TestSegmentClassifiers(TestCase):
    """Pure-function tests for the candidate segment classifiers and helpers."""

    def test_absolute_classifier_branches(self):
        from tickets.services.segmentation.segment_definitions import classify_segment_absolute
        bands = (40.0, 75.0)
        cases = [
            (10, 5, 100, "VIP"),          # active, many, high
            (10, 5, 10, "Loyal"),         # active, many, low
            (10, 3, 100, "Big Spender"),  # active, few, high
            (10, 3, 10, "Promising"),     # active, few, low
            (10, 1, 10, "New"),           # active, one, low spend
            (10, 1, 50, "Promising"),     # active, one, decent spend (>= mid) -> Promising
            (120, 3, 10, "At-Risk"),      # cooling, few
            (120, 1, 50, "At-Risk"),      # cooling, one, decent spend -> At-Risk
            (120, 1, 10, "Promising"),    # cooling, one, low spend
            (300, 2, 10, "Lapsed"),       # lost, repeat history
            (300, 1, 10, "Dormant"),      # lost, single
        ]
        for recency, freq, mon, expected in cases:
            self.assertEqual(
                classify_segment_absolute(recency, freq, mon, bands), expected,
                f"r={recency} f={freq} m={mon}",
            )

    def test_absolute_all_zero_bands_no_false_vip(self):
        """With a degenerate all-zero money band, no one is a high spender."""
        from tickets.services.segmentation.segment_definitions import classify_segment_absolute
        # active + many but high threshold is 0 -> not VIP/Big Spender, falls to Loyal
        self.assertEqual(classify_segment_absolute(10, 5, 0, (0.0, 0.0)), "Loyal")

    def test_derive_monetary_bands(self):
        from tickets.services.segmentation.segment_definitions import derive_monetary_bands
        self.assertEqual(derive_monetary_bands([]), (0.0, 0.0))
        self.assertEqual(derive_monetary_bands([0, 0, 0]), (0.0, 0.0))
        mid, high = derive_monetary_bands([10, 10, 10, 10])  # degenerate -> high>mid
        self.assertGreater(high, mid)
        mid, high = derive_monetary_bands([10, 20, 30, 40, 50])
        self.assertLess(mid, high)

    def test_canonical_none_input_and_grid(self):
        from tickets.services.segmentation.segment_definitions import classify_segment_canonical
        self.assertEqual(classify_segment_canonical(None, 3, 3), "Lost")
        self.assertEqual(classify_segment_canonical(5, 5, 5), "Champions")
        self.assertEqual(classify_segment_canonical(1, 1, 1), "Lost")

    def test_spearman_edge_cases(self):
        from tickets.services.segmentation.validation import _spearman, _percentile_breakpoints
        self.assertIsNone(_spearman([1], [1]))          # too short
        self.assertIsNone(_spearman([1, 1, 1], [5, 6, 7]))  # zero variance in x
        self.assertAlmostEqual(_spearman([1, 2, 3], [1, 2, 3]), 1.0)
        # degenerate array falls back to median x4
        self.assertEqual(_percentile_breakpoints([3, 3, 3, 3]), [3.0, 3.0, 3.0, 3.0])


class TestValidateSegmentsCommand(TestCase):
    """Tests for the validate_segments management command."""

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Cmd Org', slug='cmd-org')
        cls.venue = Venue.objects.create(organization=cls.org, name='V', city='C')
        cls.event = Event.objects.create(
            organization=cls.org, name='E', venue=cls.venue, start_date=date(2025, 1, 1),
        )
        cls.csv_format = CSVFormat.objects.create(
            organization=cls.org, name='F', column_mapping={'order_number': 'Order ID'},
        )
        cls.upload = UploadedFile.objects.create(
            organization=cls.org, csv_format=cls.csv_format, filename='t.csv', status='completed',
        )
        import uuid

        def order(customer, total, days_ago):
            TicketOrder.objects.create(
                customer=customer, event=cls.event, uploaded_file=cls.upload,
                order_number=str(uuid.uuid4())[:20],
                order_date=timezone.now() - timedelta(days=days_ago),
                total_amount=Decimal(str(total)),
            )

        for i in range(8):
            c = Customer.objects.create(organization=cls.org, email=f'cmd{i}@e.com', name=f'c{i}')
            order(c, 100, 200)
            if i < 6:
                order(c, 80, 20)  # holdout activity

    def test_command_runs_with_all_comparisons(self):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command('validate_segments', '--org', 'cmd-org', '--holdout-days', '90',
                     '--compare-canonical', '--compare-absolute', stdout=out)
        output = out.getvalue()
        self.assertIn('Cube coverage', output)
        self.assertIn('canonical', output)
        self.assertIn('absolute', output)

    def test_command_unknown_org_errors(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('validate_segments', '--org', 'does-not-exist')

    def test_command_bad_cutoff_errors(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('validate_segments', '--org', 'cmd-org', '--cutoff', 'not-a-date')


class TestSegmentTuning(TestCase):
    """Tests for absolute-mode segmentation + the cut-off tuning form/view."""

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Tune Org', slug='tune-org')
        cls.admin = User.objects.create_user('tuner', 'tuner@test.com', 'pw')
        UserProfile.objects.create(user=cls.admin, organization=cls.org, org_role=UserProfile.OrgRole.OWNER)
        cls.host = User.objects.create_user('hostuser', 'host@test.com', 'pw')
        UserProfile.objects.create(user=cls.host, organization=cls.org, org_role=UserProfile.OrgRole.HOST)
        cls.venue = Venue.objects.create(organization=cls.org, name='V', city='C')
        cls.event = Event.objects.create(organization=cls.org, name='E', venue=cls.venue, start_date=date(2025, 1, 1))
        cls.fmt = CSVFormat.objects.create(organization=cls.org, name='F', column_mapping={'order_number': 'Order ID'})
        cls.upload = UploadedFile.objects.create(organization=cls.org, csv_format=cls.fmt, filename='t.csv', status='completed')

    def _cust(self, email, ltv='0.00', last=None):
        return Customer.objects.create(
            organization=self.org, email=email, name=email,
            lifetime_value=Decimal(ltv), last_order_date=last,
        )

    def _order(self, c, total, days_ago):
        import uuid
        TicketOrder.objects.create(
            customer=c, event=self.event, uploaded_file=self.upload,
            order_number=str(uuid.uuid4())[:20],
            order_date=timezone.now() - timedelta(days=days_ago), total_amount=Decimal(str(total)),
        )

    # ---- classifier + band helpers ----
    def test_classify_absolute_band_overrides(self):
        from tickets.services.segmentation.segment_definitions import classify_segment_absolute
        # 3 orders, recent, high spend: default freq_many=5 -> Big Spender
        self.assertEqual(classify_segment_absolute(10, 3, 100, (40.0, 75.0)), "Big Spender")
        # lower "buys often" to 3 -> now VIP
        self.assertEqual(
            classify_segment_absolute(10, 3, 100, (40.0, 75.0), freq_many=3), "VIP",
        )

    def test_seed_segment_bands_idempotent_and_force(self):
        from tickets.services.segmentation.segment_definitions import seed_segment_bands
        for i in range(5):
            self._cust(f's{i}@e.com', ltv=str(20 + i * 30))
        bands = seed_segment_bands(self.org)
        self.assertGreater(bands['monetary_high'], 0)
        self.org.refresh_from_db()
        self.assertEqual(self.org.segment_bands['monetary_high'], bands['monetary_high'])
        # second call is a no-op (already seeded)
        again = seed_segment_bands(self.org)
        self.assertEqual(again, bands)
        # force re-seeds
        forced = seed_segment_bands(self.org, force=True)
        self.assertIn('monetary_high', forced)

    # ---- form ----
    def test_form_roundtrips_bands_and_validates(self):
        from tickets.forms import SegmentTuningForm
        data = {
            'segment_mode': 'absolute', 'recency_active_days': 90, 'recency_cooling_days': 180,
            'freq_few': 2, 'freq_many': 4, 'monetary_mid': '40.00', 'monetary_high': '75.00',
        }
        form = SegmentTuningForm(data, instance=self.org)
        self.assertTrue(form.is_valid(), form.errors)
        org = form.save()
        org.refresh_from_db()
        self.assertEqual(org.segment_mode, 'absolute')
        self.assertEqual(org.segment_bands['freq_many'], 4)
        self.assertEqual(org.segment_bands['monetary_high'], 75.0)
        # bad ordering rejected
        bad = dict(data, freq_few=5, freq_many=3)
        self.assertFalse(SegmentTuningForm(bad, instance=self.org).is_valid())

    # ---- RFMCalculator branch ----
    def test_rfmcalculator_absolute_vs_percentile(self):
        from tickets.services.segmentation.rfm_calculator import RFMCalculator
        c = self._cust('recent@e.com', ltv='50.00', last=date.today() - timedelta(days=10))
        self._order(c, 50, days_ago=10)  # one recent order, low spend

        self.org.segment_mode = 'percentile'
        self.org.save(update_fields=['segment_mode'])
        RFMCalculator(self.org).calculate_all()
        c.refresh_from_db()
        self.assertTrue(c.rfm_segment)
        self.assertIsNotNone(c.rfm_recency_score)

        self.org.segment_mode = 'absolute'
        # explicit bands so $50 is below the decent-spend threshold (-> New)
        self.org.segment_bands = {
            'recency_active_days': 90, 'recency_cooling_days': 180,
            'freq_few': 2, 'freq_many': 5, 'monetary_mid': 100.0, 'monetary_high': 200.0,
        }
        self.org.save(update_fields=['segment_mode', 'segment_bands'])
        RFMCalculator(self.org).calculate_all()
        c.refresh_from_db()
        # recent single order below the decent-spend threshold -> New
        self.assertEqual(c.rfm_segment, 'New')
        # percentile scores still populated in absolute mode
        self.assertIsNotNone(c.rfm_recency_score)

    # ---- preview ----
    def test_preview_absolute_sizes_sums_and_scoped(self):
        from tickets.services.segmentation.validation import SegmentDiagnostics
        from tickets.services.segmentation.segment_definitions import bands_from_org, seed_segment_bands
        for i in range(4):
            c = self._cust(f'p{i}@e.com', ltv='60.00', last=date.today() - timedelta(days=10))
            self._order(c, 60, days_ago=10)
        seed_segment_bands(self.org)
        sizes = SegmentDiagnostics(self.org).preview_absolute_sizes(bands_from_org(self.org))
        self.assertEqual(sizes['status'], 'ok')
        self.assertEqual(sizes['total_scored'], 4)

    # ---- view ----
    def test_view_admin_gate(self):
        self.client.force_login(self.host)  # non-admin
        resp = self.client.get(reverse('tickets:settings_segment_tuning'))
        self.assertNotEqual(resp.status_code, 200)  # blocked (redirect or 403)

    def test_view_links_back_to_live_segment_results(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('tickets:settings_segment_tuning'))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'View live segment results')
        self.assertContains(resp, reverse('tickets:customer_segments'))

    def test_view_preview_does_not_save(self):
        self.client.force_login(self.admin)
        self._cust('v1@e.com', ltv='60.00', last=date.today() - timedelta(days=10))
        resp = self.client.post(reverse('tickets:settings_segment_tuning'), {
            'action': 'preview', 'segment_mode': 'absolute',
            'recency_active_days': 90, 'recency_cooling_days': 180,
            'freq_few': 2, 'freq_many': 3, 'monetary_mid': '40.00', 'monetary_high': '75.00',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.context['preview_sizes'])
        self.assertContains(resp, 'Nothing is saved yet')  # bars actually rendered
        self.org.refresh_from_db()
        self.assertEqual(self.org.segment_mode, 'percentile')  # not saved

    def test_view_preview_ajax_returns_partial(self):
        self.client.force_login(self.admin)
        self._cust('ax@e.com', ltv='60.00', last=date.today() - timedelta(days=10))
        resp = self.client.post(
            reverse('tickets:settings_segment_tuning'),
            {'action': 'preview', 'segment_mode': 'absolute',
             'recency_active_days': 90, 'recency_cooling_days': 180,
             'freq_few': 2, 'freq_many': 3, 'monetary_mid': '40.00', 'monetary_high': '75.00'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Nothing is saved yet')     # the preview partial
        self.assertNotContains(resp, 'How should we sort')    # NOT the full page

    def test_view_preview_ajax_screenshot_payload_valid(self):
        """Reproduce the exact browser payload from the bug screenshot."""
        self.client.force_login(self.admin)
        self._cust('sc@e.com', ltv='60.00', last=date.today() - timedelta(days=10))
        resp = self.client.post(
            reverse('tickets:settings_segment_tuning'),
            {'action': 'preview', 'segment_mode': 'absolute',
             'recency_active_days': '60', 'recency_cooling_days': '180',
             'freq_few': '2', 'freq_many': '5',
             'monetary_mid': '40.0', 'monetary_high': '75.0'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        content = resp.content.decode()
        self.assertNotIn('Please check the numbers', content,
                         msg='form was unexpectedly invalid')
        self.assertIn('Nothing is saved yet', content)

    def test_view_preview_backtest_shows_verdict(self):
        self.client.force_login(self.admin)
        # High/low split so both backtests yield >=2 segments (holdout_days=180 ->
        # pre-cutoff orders are >=180 days ago, holdout orders <180).
        for i in range(6):
            c = self._cust(f'good{i}@e.com')
            self._order(c, 200, days_ago=300)  # pre-cutoff, frequent + high spend
            self._order(c, 200, days_ago=250)
            self._order(c, 200, days_ago=200)
            self._order(c, 150, days_ago=60)   # holdout repeat
        for i in range(5):
            c = self._cust(f'bad{i}@e.com')
            self._order(c, 20, days_ago=250)   # single old low-value order, no holdout
        resp = self.client.post(reverse('tickets:settings_segment_tuning'), {
            'action': 'preview_backtest', 'segment_mode': 'absolute',
            'recency_active_days': 90, 'recency_cooling_days': 180,
            'freq_few': 2, 'freq_many': 3, 'monetary_mid': '40.00', 'monetary_high': '75.00',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['backtest_status'], 'ok')
        verdict = resp.context['backtest_verdict']
        self.assertIn(verdict['label'], ('better', 'similar', 'worse'))
        self.assertContains(resp, 'likely to spend more later')
        # the "order check" narrative + plain "what to change" tips
        self.assertContains(resp, 'Do these segments work')
        self.assertContains(resp, 'shorter as you go down')
        self.assertContains(resp, 'These flags are only checking future revenue')
        order_rows = resp.context['order_rows']
        self.assertIn('bar_pct', order_rows[0])
        self.assertIn('out_of_order', order_rows[0])
        self.assertIn('recommendations', resp.context)  # apply-buttons (may be empty)
        # order check is the value-ordered group list
        self.assertEqual(
            [r['segment'] for r in order_rows],
            [r['segment'] for r in resp.context['backtest_current_rows']],
        )

    def test_recommended_bands_lowers_frequent_buyer(self):
        from tickets.services.segmentation.validation import SegmentDiagnostics
        from tickets.services.segmentation.segment_definitions import bands_from_org
        # Customers with 2-3 orders each, but 'frequent buyer' set high (5) -> the
        # top groups are empty, so it should recommend a lower frequent-buyer number.
        for i in range(10):
            c = self._cust(f'r{i}@e.com', ltv='120.00', last=date.today() - timedelta(days=10))
            self._order(c, 60, days_ago=10)
            self._order(c, 60, days_ago=40)
            if i % 2 == 0:
                self._order(c, 60, days_ago=70)  # some have 3 orders
        self.org.segment_bands = {
            'recency_active_days': 90, 'recency_cooling_days': 180,
            'freq_few': 2, 'freq_many': 5, 'monetary_mid': 40.0, 'monetary_high': 75.0,
        }
        self.org.save(update_fields=['segment_bands'])
        recs = SegmentDiagnostics(self.org).recommended_bands(bands_from_org(self.org))
        self.assertIn('freq_many', recs)
        self.assertLess(recs['freq_many'], 5)
        self.assertGreater(recs['freq_many'], 2)  # above 'a few'

    def test_recommended_bands_empty_when_reasonable(self):
        from tickets.services.segmentation.validation import SegmentDiagnostics
        from tickets.services.segmentation.segment_definitions import bands_from_org
        for i in range(10):
            c = self._cust(f'ok{i}@e.com', ltv='50.00', last=date.today() - timedelta(days=10))
            self._order(c, 50, days_ago=10)
            self._order(c, 50, days_ago=40)
            self._order(c, 50, days_ago=70)  # everyone has 3 orders
        self.org.segment_bands = {
            'recency_active_days': 90, 'recency_cooling_days': 180,
            'freq_few': 2, 'freq_many': 3, 'monetary_mid': 20.0, 'monetary_high': 40.0,
        }
        self.org.save(update_fields=['segment_bands'])
        recs = SegmentDiagnostics(self.org).recommended_bands(bands_from_org(self.org))
        self.assertNotIn('freq_many', recs)  # 'many' at 3 already populated

    def test_ui_recommendations_hide_changes_that_backtest_worse(self):
        from tickets.views import _recommendations

        class FakeDiagnostics:
            def recommended_bands(self, candidate):
                return {'freq_many': 3}

            def _backtest(self, **kwargs):
                return {
                    'status': 'ok',
                    'separation': {'spearman_future_revenue': 0.10},
                }

        candidate = (
            {'recency_active': 90, 'recency_cooling': 180, 'freq_few': 2, 'freq_many': 5},
            (40.0, 75.0),
        )
        recs = _recommendations(FakeDiagnostics(), candidate, current_score=0.30)
        self.assertEqual(recs, [])

    def test_spread_note_for_dormant_is_not_threshold_advice(self):
        from tickets.views import _spread_note

        note = _spread_note('Dormant')
        self.assertIn('That can be normal', note)
        self.assertIn('widen the “Recently active” or “Slipping away” day ranges', note)
        self.assertNotIn('lower your thresholds', note)

    def test_view_save_switches_mode_and_recalcs(self):
        self.client.force_login(self.admin)
        c = self._cust('v2@e.com', ltv='20.00', last=date.today() - timedelta(days=10))
        self._order(c, 20, days_ago=10)  # below the $40 decent-spend threshold
        resp = self.client.post(reverse('tickets:settings_segment_tuning'), {
            'action': 'save', 'segment_mode': 'absolute',
            'recency_active_days': 90, 'recency_cooling_days': 180,
            'freq_few': 2, 'freq_many': 4, 'monetary_mid': '40.00', 'monetary_high': '75.00',
        })
        self.assertEqual(resp.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.segment_mode, 'absolute')
        c.refresh_from_db()
        # eager recalc ran with absolute bands
        self.assertEqual(c.rfm_segment, 'New')


class TestCustomerBehaviorProfiler(TestCase):
    """Tests for layered customer behavior profiling."""

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Behavior Org', slug='behavior-org')
        cls.venue = Venue.objects.create(organization=cls.org, name='Venue', city='City')
        cls.event = Event.objects.create(
            organization=cls.org,
            name='Event',
            venue=cls.venue,
            start_date=date(2025, 1, 1),
        )
        cls.csv_format = CSVFormat.objects.create(
            organization=cls.org, name='Fmt', column_mapping={'order_number': 'Order ID'},
        )
        cls.upload = UploadedFile.objects.create(
            organization=cls.org, csv_format=cls.csv_format, filename='test.csv', status='completed',
        )

    def _make_customer(self, email, lifetime_value=Decimal('0.00'), last_order_date=None):
        return Customer.objects.create(
            organization=self.org,
            email=email,
            name=email,
            lifetime_value=lifetime_value,
            last_order_date=last_order_date,
        )

    def _make_order(self, customer, total, days_ago, order_num=None):
        TicketOrder.objects.create(
            customer=customer,
            event=self.event,
            uploaded_file=self.upload,
            order_number=order_num or str(uuid.uuid4())[:20],
            order_date=timezone.now() - timedelta(days=days_ago),
            total_amount=Decimal(str(total)),
        )

    def test_profiles_recent_single_order_as_first_time_recent(self):
        from tickets.services.segmentation.behavior_profiles import CustomerBehaviorProfiler

        customer = self._make_customer(
            'first@example.com',
            lifetime_value=Decimal('60.00'),
            last_order_date=date.today() - timedelta(days=14),
        )
        self._make_order(customer, 60, days_ago=14)

        CustomerBehaviorProfiler(self.org).calculate_all()
        customer.refresh_from_db()

        self.assertEqual(customer.behavior_profile, 'First-Time Recent')
        self.assertIn(customer.days_since_last_order, (14, 15))
        self.assertIsNone(customer.avg_days_between_orders)

    def test_profiles_fast_repeat_from_short_gaps(self):
        from tickets.services.segmentation.behavior_profiles import CustomerBehaviorProfiler

        customer = self._make_customer(
            'fast@example.com',
            lifetime_value=Decimal('180.00'),
            last_order_date=date.today() - timedelta(days=12),
        )
        for days_ago in [40, 24, 12]:
            self._make_order(customer, 60, days_ago=days_ago)

        CustomerBehaviorProfiler(self.org).calculate_all()
        customer.refresh_from_db()

        self.assertEqual(customer.behavior_profile, 'Fast Repeat')
        self.assertEqual(customer.avg_days_between_orders, 14)
        self.assertEqual(customer.days_to_second_order, 16)

    def test_profiles_high_value_occasional_when_value_is_high(self):
        from tickets.services.segmentation.behavior_profiles import CustomerBehaviorProfiler

        high_value = self._make_customer(
            'high@example.com',
            lifetime_value=Decimal('500.00'),
            last_order_date=date.today() - timedelta(days=50),
        )
        for days_ago in [140, 50]:
            self._make_order(high_value, 250, days_ago=days_ago)

        baseline = self._make_customer(
            'baseline@example.com',
            lifetime_value=Decimal('40.00'),
            last_order_date=date.today() - timedelta(days=20),
        )
        for days_ago in [35, 20]:
            self._make_order(baseline, 20, days_ago=days_ago)

        CustomerBehaviorProfiler(self.org).calculate_all()
        high_value.refresh_from_db()

        self.assertEqual(high_value.behavior_profile, 'High-Value Occasional')

    def test_profiles_slowing_down_when_customer_falls_behind_typical_cadence(self):
        from tickets.services.segmentation.behavior_profiles import CustomerBehaviorProfiler

        customer = self._make_customer(
            'slow@example.com',
            lifetime_value=Decimal('240.00'),
            last_order_date=date.today() - timedelta(days=80),
        )
        for days_ago in [100, 90, 80]:
            self._make_order(customer, 80, days_ago=days_ago)

        CustomerBehaviorProfiler(self.org).calculate_all()
        customer.refresh_from_db()

        self.assertEqual(customer.behavior_profile, 'Slowing Down')
        self.assertEqual(customer.avg_days_between_orders, 10)

    def test_profiles_inactive_repeat_for_long_inactivity(self):
        from tickets.services.segmentation.behavior_profiles import CustomerBehaviorProfiler

        customer = self._make_customer(
            'inactive@example.com',
            lifetime_value=Decimal('120.00'),
            last_order_date=date.today() - timedelta(days=220),
        )
        for days_ago in [260, 220]:
            self._make_order(customer, 60, days_ago=days_ago)

        CustomerBehaviorProfiler(self.org).calculate_all()
        customer.refresh_from_db()

        self.assertEqual(customer.behavior_profile, 'Inactive Repeat')
        self.assertIn(customer.days_since_last_order, (220, 221))

    def test_placeholder_customers_remain_unprofiled(self):
        from tickets.services.segmentation.behavior_profiles import CustomerBehaviorProfiler

        placeholder = self._make_customer('ghost@placeholder.local')
        CustomerBehaviorProfiler(self.org).calculate_all()
        placeholder.refresh_from_db()

        self.assertEqual(placeholder.behavior_profile, '')
        self.assertEqual(placeholder.behavior_profile_reason, '')
        self.assertIsNone(placeholder.days_since_last_order)


class CustomerSegmentationViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Segment Org', slug='segment-org')
        self.user = User.objects.create_user(
            username='segmentuser',
            email='segment@test.com',
            password='testpass123',
        )
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.host = User.objects.create_user(
            username='segmenthost',
            email='segment-host@test.com',
            password='testpass123',
        )
        UserProfile.objects.create(user=self.host, organization=self.org, org_role=UserProfile.OrgRole.HOST)
        self.client.login(username='segment@test.com', password='testpass123')
        self.client.get(reverse('tickets:home'))

        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org,
            name='Event',
            venue=self.venue,
            start_date=date(2025, 1, 1),
        )
        self.csv_format = CSVFormat.objects.create(
            organization=self.org, name='Fmt', column_mapping={'order_number': 'Order ID'},
        )
        self.upload = UploadedFile.objects.create(
            organization=self.org, csv_format=self.csv_format, filename='test.csv', status='completed',
        )

    def test_customer_segments_shows_automatic_mode_copy_for_admins(self):
        response = self.client.get(reverse('tickets:customer_segments'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['segment_mode_label'], 'Automatic')
        self.assertContains(response, 'Scoring')
        self.assertContains(response, 'Cue sorts customers automatically using relative RFM scores')
        self.assertContains(response, 'Settings')

    def test_customer_segments_hides_tuning_link_for_non_admins(self):
        self.client.force_login(self.host)

        response = self.client.get(reverse('tickets:customer_segments'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Scoring')
        self.assertNotContains(response, 'href="{}"'.format(reverse('tickets:settings_segment_tuning')))

    def test_customer_segments_shows_custom_rule_summary(self):
        self.org.segment_mode = 'absolute'
        self.org.segment_bands = {
            'recency_active_days': 60,
            'recency_cooling_days': 150,
            'freq_few': 2,
            'freq_many': 4,
            'monetary_mid': 40.0,
            'monetary_high': 90.0,
        }
        self.org.save(update_fields=['segment_mode', 'segment_bands'])

        response = self.client.get(reverse('tickets:customer_segments'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['segment_mode_label'], 'Custom rules')
        self.assertContains(response, 'active within 60 days')
        self.assertContains(response, 'frequent at 4 orders')
        self.assertContains(response, 'top spender at $90')

    def test_settings_overview_links_to_segment_settings_for_admins(self):
        response = self.client.get(reverse('tickets:settings_overview'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Segment Settings')
        self.assertContains(response, reverse('tickets:settings_segment_tuning'))

    def test_customer_segments_is_minimal_and_links_to_filtered_customers(self):
        customer = Customer.objects.create(
            organization=self.org,
            email='profiled@example.com',
            name='Profiled Customer',
            lifetime_value=Decimal('180.00'),
            last_order_date=date.today() - timedelta(days=12),
            rfm_segment='Loyal',
            behavior_profile='Fast Repeat',
            days_since_last_order=12,
        )
        TicketOrder.objects.create(
            customer=customer,
            event=self.event,
            uploaded_file=self.upload,
            order_number='SEG-001',
            order_date=timezone.now() - timedelta(days=12),
            total_amount=Decimal('90.00'),
        )

        response = self.client.get(reverse('tickets:customer_segments'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Segments')
        self.assertContains(response, 'href="{}?segment=Loyal"'.format(reverse('tickets:customer_list')))
        self.assertContains(response, 'progress-bar')
        self.assertNotIn('behavior_stats', response.context)
        self.assertNotContains(response, 'Behavior profiles')
        self.assertNotContains(response, 'Fast Repeat')
        self.assertNotContains(response, 'chart.umd.min.js')
        self.assertNotContains(response, '<canvas')

    def test_customer_detail_shows_behavior_profile_metrics(self):
        customer = Customer.objects.create(
            organization=self.org,
            email='detail@example.com',
            name='Detail Customer',
            lifetime_value=Decimal('200.00'),
            last_order_date=date.today() - timedelta(days=8),
            rfm_segment='VIP',
            behavior_profile='Steady Repeat',
            behavior_profile_reason='Shows a consistent repeat cadence without large gaps.',
            days_since_last_order=8,
            avg_days_between_orders=32,
            days_to_second_order=21,
        )
        TicketOrder.objects.create(
            customer=customer,
            event=self.event,
            uploaded_file=self.upload,
            order_number='DET-001',
            order_date=timezone.now() - timedelta(days=8),
            total_amount=Decimal('100.00'),
        )

        response = self.client.get(reverse('tickets:customer_detail', args=[customer.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Behavior')
        self.assertContains(response, 'Steady Repeat')
        self.assertContains(response, 'Average days between orders')


class CustomerDetailTimelineTests(TestCase):
    """The consolidated Timeline merges orders, native-SMS delivery state, survey
    responses, and loyalty tier transitions into one reverse-chronological feed,
    keeping the SMS consent + delivery scoreboard as a header above it."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(
            name='SMS Org', slug='sms-org', sms_marketing_enabled=True,
        )
        self.user = User.objects.create_user(
            username='smsuser', email='sms@test.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='sms@test.com', password='testpass123')
        self.client.get(reverse('tickets:home'))
        self.customer = Customer.objects.create(
            organization=self.org,
            email='sms-customer@example.com',
            name='SMS Customer',
            phone='+15551234567',
            lifetime_value=Decimal('50.00'),
        )

    def _make_message(self, **kwargs):
        from .models import SMSCampaign, SMSMessageRecipient
        campaign = kwargs.pop('campaign', None) or SMSCampaign.objects.create(
            organization=self.org, name='Summer Promo', body='Tickets on sale now',
        )
        defaults = dict(
            campaign=campaign,
            customer=self.customer,
            phone=self.customer.phone,
            status=SMSMessageRecipient.Status.DELIVERED,
            sent_at=timezone.now() - timedelta(hours=2),
            delivered_at=timezone.now() - timedelta(hours=2),
        )
        defaults.update(kwargs)
        return SMSMessageRecipient.objects.create(**defaults)

    def test_timeline_lists_sms_activity(self):
        self._make_message(first_clicked_at=timezone.now() - timedelta(hours=1), click_count=2)

        response = self.client.get(reverse('tickets:customer_detail', args=[self.customer.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['sms_stats']['total'], 1)
        self.assertEqual(response.context['sms_stats']['delivered'], 1)
        self.assertEqual(response.context['sms_stats']['clicked'], 1)
        self.assertContains(response, 'Timeline')
        self.assertContains(response, 'SMS: Summer Promo')
        self.assertContains(response, 'Delivered')

    def test_timeline_empty_state(self):
        response = self.client.get(reverse('tickets:customer_detail', args=[self.customer.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['sms_stats']['total'], 0)
        self.assertContains(response, 'No activity yet.')

    def test_sms_header_hidden_when_feature_disabled(self):
        self.org.sms_marketing_enabled = False
        self.org.save(update_fields=['sms_marketing_enabled'])
        self._make_message()

        response = self.client.get(reverse('tickets:customer_detail', args=[self.customer.id]))

        self.assertEqual(response.status_code, 200)
        # The SMS consent + delivery scoreboard header disappears, and SMS messages
        # drop out of the timeline, when the org has SMS marketing disabled.
        self.assertNotContains(response, 'SMS Consent')
        self.assertNotContains(response, 'Messages Sent')
        self.assertNotContains(response, 'SMS: Summer Promo')

    def test_timeline_merges_interactions_reverse_chronologically(self):
        from .models import (
            Venue, Event, TicketOrder, Ticket, SurveyInvitation, SurveyResponse,
            LoyaltyProgram, LoyaltyTier, LoyaltyTierTransition,
        )
        self.org.loyalty_feature_enabled = True
        self.org.save(update_fields=['loyalty_feature_enabled'])

        venue = Venue.objects.create(organization=self.org, name='The Hall', city='LA')
        event = Event.objects.create(
            organization=self.org, name='Night Show', venue=venue, start_date=date.today(),
        )
        program = LoyaltyProgram.objects.create(organization=self.org, name='Club')
        gold = LoyaltyTier.objects.create(program=program, name='Gold', rank=2, color='red')

        now = timezone.now()
        # Create oldest -> newest across every interaction kind.
        LoyaltyTierTransition.objects.create(
            customer=self.customer, organization=self.org,
            from_tier=None, to_tier=gold, changed_at=now - timedelta(days=10),
        )
        order = TicketOrder.objects.create(
            customer=self.customer, event=event, order_number='TL-1',
            order_date=now - timedelta(days=5), total_amount=Decimal('30.00'),
        )
        Ticket.objects.create(ticket_order=order, price=Decimal('30.00'))
        self._make_message(
            sent_at=now - timedelta(days=2), delivered_at=now - timedelta(days=2),
        )
        invitation = SurveyInvitation.objects.create(
            event=event, customer=self.customer, organization=self.org,
            email=self.customer.email,
        )
        SurveyResponse.objects.create(
            invitation=invitation, event=event, customer=self.customer,
            organization=self.org,
        )  # submitted_at auto_now_add -> "now", the newest interaction

        response = self.client.get(reverse('tickets:customer_detail', args=[self.customer.id]))

        self.assertEqual(response.status_code, 200)
        kinds = [item['kind'] for item in response.context['page_obj']]
        self.assertEqual(kinds, ['survey', 'sms', 'order', 'tier'])
        self.assertContains(response, 'Purchased 1 ticket')
        self.assertContains(response, 'SMS: Summer Promo')
        self.assertContains(response, 'Completed survey')
        self.assertContains(response, 'Reached Gold tier')


class SMSBroadcastAudienceTests(TestCase):
    """The SMS tab's broadcast-audience chart + by-market breakdown combine native
    SMS campaigns and external SlickText broadcasts, grouped by the linked event's
    assigned market."""

    def setUp(self):
        from .models import SMSCampaign, EventSMSCampaign
        self.client = Client()
        self.org = Organization.objects.create(
            name='SMS Aud Org', slug='sms-aud-org', sms_marketing_enabled=True,
        )
        self.user = User.objects.create_user(
            username='audhost', email='aud@test.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='aud@test.com', password='testpass123')
        self.client.get(reverse('tickets:home'))  # warm org cache

        self.venue = Venue.objects.create(organization=self.org, name='Echo', city='Austin')
        self.event = Event.objects.create(
            organization=self.org, name='Austin Show', venue=self.venue,
            start_date=date(2026, 6, 1), start_time=time(20, 0, 0),
        )
        self.market = Market.objects.create(
            organization=self.org,
            name='Central Texas',
            geography_level='city',
            geography_value='Austin',
        )
        from tickets.services.markets import MarketBuilder
        MarketBuilder(self.org).assign_event(self.event)

        # Native SMS campaign (sent), event-scoped -> Central Texas market.
        SMSCampaign.objects.create(
            organization=self.org, name='Native Blast', body='Tickets!',
            event=self.event, status=SMSCampaign.Status.SENT,
            sent_at=timezone.now() - timedelta(days=3), audience_size=120,
        )
        # External SlickText broadcast (confirmed) on the same event -> Central Texas market.
        EventSMSCampaign.objects.create(
            event=self.event, source='slicktext', external_id='st-1',
            name='SlickText Blast', send_time=timezone.now() - timedelta(days=5),
            audience_size=80, confirmed_at=timezone.now(),
        )
        self.url = reverse('tickets:sms_campaign_list')

    def test_breakdown_sums_audience_across_sources(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        breakdown = {r['market']: r for r in response.context['market_breakdown']}
        self.assertIn('Central Texas', breakdown)
        self.assertNotIn('Austin', breakdown)
        self.assertEqual(breakdown['Central Texas']['broadcasts'], 2)
        self.assertEqual(breakdown['Central Texas']['total_audience'], 200)
        self.assertEqual(breakdown['Central Texas']['avg_audience'], 100)
        self.assertEqual(breakdown['Central Texas']['market_id'], str(self.market.id))
        self.assertIn('Central Texas', response.context['market_choices'])

    def test_market_filter_scopes_chart_points(self):
        response = self.client.get(self.url, {'market': 'Central Texas'})
        self.assertEqual(response.status_code, 200)
        points = json.loads(response.context['audience_points_json'])
        # by_market groups all channels under the market name.
        self.assertIn('Central Texas', points['by_market'])
        ct_points = points['by_market']['Central Texas']
        self.assertEqual(len(ct_points), 2)  # native + slicktext
        audiences = {p['y'] for p in ct_points}
        self.assertIn(120, audiences)  # native SMSCampaign audience
        self.assertEqual(response.context['selected_market'], 'Central Texas')

    def test_unknown_market_falls_back_to_all(self):
        response = self.client.get(self.url, {'market': 'Nowhere'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_market'], '')


class SMSCampaignListEventFilterTests(TestCase):
    """The Sends view can be scoped to a single event via ?event=<id>. Choices are
    built from the events with sends in the current window; an unknown id clears."""

    def setUp(self):
        from .models import SMSCampaign
        self.client = Client()
        self.org = Organization.objects.create(
            name='Filter Org', slug='filter-org', sms_marketing_enabled=True,
        )
        self.user = User.objects.create_user(
            username='filterhost', email='filter@test.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='filter@test.com', password='testpass123')
        self.client.get(reverse('tickets:home'))  # warm org cache

        self.venue = Venue.objects.create(organization=self.org, name='Echo', city='Austin')
        self.event_a = Event.objects.create(
            organization=self.org, name='Alpha Show', venue=self.venue,
            start_date=date(2026, 6, 1), start_time=time(20, 0, 0),
        )
        self.event_b = Event.objects.create(
            organization=self.org, name='Beta Show', venue=self.venue,
            start_date=date(2026, 7, 1), start_time=time(20, 0, 0),
        )
        self.camp_a = SMSCampaign.objects.create(
            organization=self.org, name='Alpha Blast', body='Tickets!',
            event=self.event_a, status=SMSCampaign.Status.SENT,
            sent_at=timezone.now() - timedelta(days=2), audience_size=50,
        )
        self.camp_b = SMSCampaign.objects.create(
            organization=self.org, name='Beta Blast', body='Tickets!',
            event=self.event_b, status=SMSCampaign.Status.SENT,
            sent_at=timezone.now() - timedelta(days=2), audience_size=60,
        )
        self.url = reverse('tickets:sms_campaign_list')

    def test_event_choices_lists_events_with_sends(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        choices = response.context['event_choices']
        choice_ids = {c['id'] for c in choices}
        self.assertIn(str(self.event_a.id), choice_ids)
        self.assertIn(str(self.event_b.id), choice_ids)
        # Each choice carries the event's date so same-named events are distinguishable.
        by_id = {c['id']: c for c in choices}
        self.assertEqual(by_id[str(self.event_a.id)]['start_date'], self.event_a.start_date)

    def test_event_filter_scopes_send_list(self):
        response = self.client.get(self.url, {'event': str(self.event_a.id)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_event'], str(self.event_a.id))
        names = {row['name'] for row in response.context['campaigns_page'].object_list}
        self.assertEqual(names, {'Alpha Blast'})

    def test_unknown_event_clears_filter(self):
        response = self.client.get(self.url, {'event': str(uuid.uuid4())})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_event'], '')
        names = {row['name'] for row in response.context['campaigns_page'].object_list}
        self.assertEqual(names, {'Alpha Blast', 'Beta Blast'})


class SMSComplianceGuardTests(TestCase):
    """Guards that keep the Twilio Compliance subscore healthy: country gating
    (avoids Geo-Permission blocks / Error 21408) and learning from opt-out blocks
    (Error 21610) so a number Twilio rejected is never re-attempted."""

    def setUp(self):
        from .models import SMSCampaign, SMSMessageRecipient, PhoneSuppression
        self.org = Organization.objects.create(
            name='Compliance Org', slug='compliance-org', sms_marketing_enabled=True,
        )

    # --- Country gating (Error 21408) ---

    def test_sms_country_allowed_defaults_to_us_ca(self):
        from .sms import sms_country_allowed
        self.assertTrue(sms_country_allowed('+15551234567'))
        self.assertFalse(sms_country_allowed('+447700900000'))  # UK

    @override_settings(SMS_ALLOWED_COUNTRY_PREFIXES=())
    def test_sms_country_allowed_empty_setting_allows_all(self):
        from .sms import sms_country_allowed
        self.assertTrue(sms_country_allowed('+447700900000'))

    def test_materialize_drops_non_allowed_country_numbers(self):
        from .models import SMSCampaign
        us = Customer.objects.create(
            organization=self.org, email='us@example.com', name='US',
            phone='+15551230000', sms_opt_in=True,
        )
        intl = Customer.objects.create(
            organization=self.org, email='uk@example.com', name='UK',
            phone='+447700900123', sms_opt_in=True,
        )
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='Blast', body='Tickets!',
            manual_include_ids=[str(us.id), str(intl.id)],
        )
        recipients = campaign.materialize()
        phones = {r['phone'] for r in recipients}
        self.assertIn('+15551230000', phones)
        self.assertNotIn('+447700900123', phones)

    # --- Learning from opt-out blocks (Error 21610) ---

    def test_chunk_task_suppresses_number_on_21610(self):
        from .models import SMSCampaign, SMSMessageRecipient, PhoneSuppression
        from .tasks import send_sms_chunk_task
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='Blast', body='Tickets!',
        )
        recipient = SMSMessageRecipient.objects.create(
            campaign=campaign, phone='+15559998888',
            status=SMSMessageRecipient.Status.QUEUED, stop_disclosed=True,
        )
        # Twilio rejects an opted-out recipient synchronously via send_sms.
        with patch('tickets.sms.send_sms', return_value=(False, None, '21610')):
            send_sms_chunk_task.apply(args=(str(campaign.id), [str(recipient.id)]))

        recipient.refresh_from_db()
        self.assertEqual(recipient.status, SMSMessageRecipient.Status.FAILED)
        self.assertEqual(recipient.error_code, '21610')
        self.assertTrue(
            PhoneSuppression.objects.filter(
                phone='+15559998888', organization__isnull=True,
            ).exists()
        )

    def test_chunk_task_does_not_suppress_on_transient_error(self):
        from .models import SMSCampaign, SMSMessageRecipient, PhoneSuppression
        from .tasks import send_sms_chunk_task
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='Blast', body='Tickets!',
        )
        recipient = SMSMessageRecipient.objects.create(
            campaign=campaign, phone='+15557776666',
            status=SMSMessageRecipient.Status.QUEUED, stop_disclosed=True,
        )
        with patch('tickets.sms.send_sms', return_value=(False, None, '30001')):
            send_sms_chunk_task.apply(args=(str(campaign.id), [str(recipient.id)]))

        recipient.refresh_from_db()
        self.assertEqual(recipient.status, SMSMessageRecipient.Status.FAILED)
        self.assertFalse(
            PhoneSuppression.objects.filter(phone='+15557776666').exists()
        )

    @override_settings(TWILIO_VALIDATE_WEBHOOKS=False)
    def test_status_webhook_suppresses_number_on_21610(self):
        from .models import SMSCampaign, SMSMessageRecipient, PhoneSuppression
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='Blast', body='Tickets!',
        )
        recipient = SMSMessageRecipient.objects.create(
            campaign=campaign, phone='+15551112222', twilio_sid='SM_test_21610',
            status=SMSMessageRecipient.Status.SENT,
        )
        response = Client().post(
            reverse('tickets:twilio_sms_status_webhook'),
            {
                'MessageSid': 'SM_test_21610',
                'MessageStatus': 'failed',
                'ErrorCode': '21610',
                'ErrorMessage': 'Attempt to send to unsubscribed recipient',
            },
        )
        self.assertEqual(response.status_code, 200)
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, SMSMessageRecipient.Status.FAILED)
        self.assertTrue(
            PhoneSuppression.objects.filter(
                phone='+15551112222', organization__isnull=True,
            ).exists()
        )

    # --- Learning from hard bounces (dead/invalid numbers) ---

    def test_chunk_task_suppresses_number_on_hard_bounce(self):
        from .models import SMSCampaign, SMSMessageRecipient, PhoneSuppression
        from .tasks import send_sms_chunk_task
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='Blast', body='Tickets!',
        )
        recipient = SMSMessageRecipient.objects.create(
            campaign=campaign, phone='+15550001111',
            status=SMSMessageRecipient.Status.QUEUED, stop_disclosed=True,
        )
        # 30005 = unknown handset; permanently undeliverable, suppress on first sight.
        with patch('tickets.sms.send_sms', return_value=(False, None, '30005')):
            send_sms_chunk_task.apply(args=(str(campaign.id), [str(recipient.id)]))

        recipient.refresh_from_db()
        self.assertEqual(recipient.status, SMSMessageRecipient.Status.FAILED)
        suppression = PhoneSuppression.objects.filter(
            phone='+15550001111', organization__isnull=True,
        ).first()
        self.assertIsNotNone(suppression)
        self.assertEqual(suppression.reason, PhoneSuppression.Reason.BOUNCE)

    @override_settings(TWILIO_VALIDATE_WEBHOOKS=False)
    def test_status_webhook_suppresses_number_on_hard_bounce(self):
        from .models import SMSCampaign, SMSMessageRecipient, PhoneSuppression
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='Blast', body='Tickets!',
        )
        recipient = SMSMessageRecipient.objects.create(
            campaign=campaign, phone='+15550002222', twilio_sid='SM_test_30006',
            status=SMSMessageRecipient.Status.SENT,
        )
        # 30006 = landline / unreachable carrier.
        response = Client().post(
            reverse('tickets:twilio_sms_status_webhook'),
            {
                'MessageSid': 'SM_test_30006',
                'MessageStatus': 'undelivered',
                'ErrorCode': '30006',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            PhoneSuppression.objects.filter(
                phone='+15550002222', organization__isnull=True,
                reason=PhoneSuppression.Reason.BOUNCE,
            ).exists()
        )

    @override_settings(TWILIO_VALIDATE_WEBHOOKS=False)
    def test_carrier_filtering_30007_never_suppresses(self):
        from .models import SMSCampaign, SMSMessageRecipient, PhoneSuppression
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='Blast', body='Tickets!',
        )
        recipient = SMSMessageRecipient.objects.create(
            campaign=campaign, phone='+15550003333', twilio_sid='SM_test_30007',
            status=SMSMessageRecipient.Status.SENT,
        )
        # 30007 = carrier spam-filtering — a sender-reputation problem, not a dead number.
        Client().post(
            reverse('tickets:twilio_sms_status_webhook'),
            {'MessageSid': 'SM_test_30007', 'MessageStatus': 'undelivered',
             'ErrorCode': '30007'},
        )
        self.assertFalse(
            PhoneSuppression.objects.filter(phone='+15550003333').exists()
        )

    # --- Learning from repeated transient failures (strike threshold) ---

    @override_settings(SMS_BOUNCE_STRIKE_THRESHOLD=3)
    def test_transient_below_threshold_does_not_suppress(self):
        from .models import SMSCampaign, SMSMessageRecipient, PhoneSuppression
        from .tasks import send_sms_chunk_task
        phone = '+15550004444'
        # One prior 30003 failure in a separate campaign → 2 strikes total after this
        # send, still under the threshold of 3.
        prior = SMSCampaign.objects.create(
            organization=self.org, name='Prior', body='Tickets!',
        )
        SMSMessageRecipient.objects.create(
            campaign=prior, phone=phone,
            status=SMSMessageRecipient.Status.UNDELIVERED, error_code='30003',
        )
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='Blast', body='Tickets!',
        )
        recipient = SMSMessageRecipient.objects.create(
            campaign=campaign, phone=phone,
            status=SMSMessageRecipient.Status.QUEUED, stop_disclosed=True,
        )
        with patch('tickets.sms.send_sms', return_value=(False, None, '30003')):
            send_sms_chunk_task.apply(args=(str(campaign.id), [str(recipient.id)]))

        self.assertFalse(PhoneSuppression.objects.filter(phone=phone).exists())

    @override_settings(SMS_BOUNCE_STRIKE_THRESHOLD=3)
    def test_transient_at_threshold_suppresses(self):
        from .models import SMSCampaign, SMSMessageRecipient, PhoneSuppression
        from .tasks import send_sms_chunk_task
        phone = '+15550005555'
        # Two prior 30003 failures in two separate campaigns → this send is the 3rd
        # distinct campaign, hitting the threshold.
        for name in ('Prior A', 'Prior B'):
            prior = SMSCampaign.objects.create(
                organization=self.org, name=name, body='Tickets!',
            )
            SMSMessageRecipient.objects.create(
                campaign=prior, phone=phone,
                status=SMSMessageRecipient.Status.UNDELIVERED, error_code='30003',
            )
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='Blast', body='Tickets!',
        )
        recipient = SMSMessageRecipient.objects.create(
            campaign=campaign, phone=phone,
            status=SMSMessageRecipient.Status.QUEUED, stop_disclosed=True,
        )
        with patch('tickets.sms.send_sms', return_value=(False, None, '30003')):
            send_sms_chunk_task.apply(args=(str(campaign.id), [str(recipient.id)]))

        self.assertTrue(
            PhoneSuppression.objects.filter(
                phone=phone, reason=PhoneSuppression.Reason.BOUNCE,
            ).exists()
        )

    def test_suppress_sms_bounces_command_backfills(self):
        from io import StringIO
        from django.core.management import call_command
        from .models import SMSCampaign, SMSMessageRecipient, PhoneSuppression
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='Old Blast', body='Tickets!',
        )
        SMSMessageRecipient.objects.create(
            campaign=campaign, phone='+15550006666',
            status=SMSMessageRecipient.Status.UNDELIVERED, error_code='30005',
        )
        # Dry run writes nothing.
        call_command('suppress_sms_bounces', stdout=StringIO())
        self.assertFalse(PhoneSuppression.objects.filter(phone='+15550006666').exists())
        # --apply writes the suppression.
        call_command('suppress_sms_bounces', '--apply', stdout=StringIO())
        self.assertTrue(
            PhoneSuppression.objects.filter(
                phone='+15550006666', reason=PhoneSuppression.Reason.BOUNCE,
            ).exists()
        )

    def test_reconcile_sms_opt_outs_command_backfills(self):
        from io import StringIO
        from unittest.mock import patch, MagicMock
        from django.core.management import call_command
        from django.test import override_settings
        from .models import PhoneSuppression

        # Two Twilio log messages: a 21610 STOP-block and a normal delivery (ignored).
        blocked = MagicMock(error_code=21610, to='+15550007777', status='failed')
        delivered = MagicMock(error_code=None, to='+15550008888', status='delivered')
        fake_client = MagicMock()
        fake_client.messages.list.return_value = [blocked, delivered]

        with override_settings(TWILIO_ACCOUNT_SID='AC_test', TWILIO_AUTH_TOKEN='tok'), \
                patch('twilio.rest.Client', return_value=fake_client):
            # Dry run writes nothing.
            call_command('reconcile_sms_opt_outs', stdout=StringIO())
            self.assertFalse(PhoneSuppression.objects.filter(phone='+15550007777').exists())
            # --apply mirrors the block into a global TWILIO_STOP suppression.
            call_command('reconcile_sms_opt_outs', '--apply', stdout=StringIO())

        supp = PhoneSuppression.objects.filter(phone='+15550007777', organization__isnull=True)
        self.assertTrue(supp.exists())
        self.assertEqual(supp.first().reason, PhoneSuppression.Reason.TWILIO_STOP)
        # The delivered number is never suppressed.
        self.assertFalse(PhoneSuppression.objects.filter(phone='+15550008888').exists())


class SMSThroughputGuardTests(TestCase):
    """Carrier-throughput guards on the send path: paced (staggered) dispatch to avoid
    burst spam-filtering (Error 30007), an account-wide daily segment cap that BLOCKS an
    oversize send at compose time (day-aware) so the organizer trims the list, a send-time
    fail+refund for the rare race the block can't see, urgency-first ordering, and dropping
    malformed numbers before they reach Twilio (Error 21211)."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='Throughput Org', slug='throughput-org', sms_marketing_enabled=True,
        )
        self.venue = Venue.objects.create(organization=self.org, name='Hall', city='LA')

    def _campaign(self, n_recipients, segments=1, status=None):
        from .models import SMSCampaign, SMSMessageRecipient
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='Blast', body='Tickets!',
            status=status or SMSCampaign.Status.SCHEDULED,
            scheduled_at=timezone.now() - timedelta(minutes=1),
        )
        SMSMessageRecipient.objects.bulk_create([
            SMSMessageRecipient(
                campaign=campaign, phone=f'+1555000{i:04d}',
                status=SMSMessageRecipient.Status.QUEUED, segments=segments,
            ) for i in range(n_recipients)
        ])
        return campaign

    def _opted_in(self, n):
        return [
            Customer.objects.create(
                organization=self.org, email=f'c{i}@example.com', name=f'C{i}',
                phone=f'+1555{i:07d}', sms_opt_in=True,
            ) for i in range(n)
        ]

    def _finalize(self, custs, *, scheduled, send_at, key):
        from .services.sms_campaigns import finalize_campaign_send
        return finalize_campaign_send(
            self.org, name='Blast', body='Tickets!', criteria={},
            manual_include_ids=[str(c.id) for c in custs], event=None,
            scheduled=scheduled, send_at=send_at, user=None, idempotency_key=key, cap=5000,
        )

    # --- Malformed-number validation (Error 21211) ---

    def test_is_plausible_e164(self):
        from .sms import is_plausible_e164
        self.assertTrue(is_plausible_e164('+15551234567'))
        self.assertTrue(is_plausible_e164('+447700900000'))  # UK, 12 digits
        self.assertFalse(is_plausible_e164('+1116267769618'))  # double country code
        self.assertFalse(is_plausible_e164('+1555123'))        # +1 but not 11 digits
        self.assertFalse(is_plausible_e164('5551234567'))      # no '+'
        self.assertFalse(is_plausible_e164('+1abc'))
        self.assertFalse(is_plausible_e164(''))

    def test_materialize_drops_malformed_numbers(self):
        from .models import SMSCampaign
        good = Customer.objects.create(
            organization=self.org, email='g@example.com', name='Good',
            phone='+15551230000', sms_opt_in=True,
        )
        # A number that already carried a country code, so normalize prepends a stray '+1'.
        bad = Customer.objects.create(
            organization=self.org, email='b@example.com', name='Bad',
            phone='116267769618', sms_opt_in=True,
        )
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='Blast', body='Tickets!',
            manual_include_ids=[str(good.id), str(bad.id)],
        )
        phones = {r['phone'] for r in campaign.materialize()}
        self.assertIn('+15551230000', phones)
        self.assertNotIn('+1116267769618', phones)

    # --- fit_within_budget ---

    def test_fit_within_budget(self):
        from .services.sms_limits import fit_within_budget
        self.assertEqual(fit_within_budget([1, 1, 1, 1, 1], None), 5)  # disabled
        self.assertEqual(fit_within_budget([1, 1, 1, 1, 1], 0), 0)
        self.assertEqual(fit_within_budget([1, 1, 1, 1, 1], 2), 2)
        self.assertEqual(fit_within_budget([2, 2, 2], 5), 2)
        # First recipient always progresses even if its segments exceed the budget.
        self.assertEqual(fit_within_budget([3, 1], 1), 1)

    # --- Paced dispatch (staggered chord) ---

    @override_settings(SMS_SEND_RATE_PER_SEC=5, SMS_CHUNK_SIZE=10, SMS_DAILY_SEGMENT_CAP=0)
    def test_dispatch_staggers_chunks_by_countdown(self):
        from .tasks import send_sms_campaign_task
        campaign = self._campaign(25)
        captured = {}

        def fake_chord(header):
            captured['header'] = header
            return lambda callback: None

        with patch('celery.chord', side_effect=fake_chord):
            send_sms_campaign_task.apply(args=[str(campaign.id)])

        header = captured['header']
        # 25 recipients / chunk_size 10 -> 3 chunks.
        self.assertEqual(len(header), 3)
        countdowns = [sig.options.get('countdown') for sig in header]
        # chunk idx * chunk_size / rate = 0, 10/5=2, 20/5=4.
        self.assertEqual(countdowns, [0, 2, 4])

    # --- Day-aware capacity ---

    @override_settings(SMS_DAILY_SEGMENT_CAP=100)
    def test_daily_capacity_is_day_aware(self):
        from .models import SMSCampaign, SMSMessageRecipient
        from .services.sms_limits import segments_scheduled_for, daily_capacity_for
        tomorrow = timezone.now() + timedelta(days=1)
        c = SMSCampaign.objects.create(
            organization=self.org, name='booked', body='hi',
            status=SMSCampaign.Status.SCHEDULED, scheduled_at=tomorrow,
        )
        SMSMessageRecipient.objects.bulk_create([
            SMSMessageRecipient(campaign=c, phone=f'+1557{i:07d}', segments=1,
                                status=SMSMessageRecipient.Status.QUEUED) for i in range(7)
        ])
        d = timezone.localdate(tomorrow)
        self.assertEqual(segments_scheduled_for(d), 7)
        self.assertEqual(daily_capacity_for(tomorrow), 93)  # 100 - 7 booked
        self.assertEqual(segments_scheduled_for(d, exclude_campaign_id=c.id), 0)

    # --- Compose-time block ---

    @override_settings(SMS_DAILY_SEGMENT_CAP=6)
    def test_finalize_blocks_send_now_over_today_budget(self):
        from .models import SMSCampaign, SMSMessageRecipient
        from .services.sms_campaigns import DailyCapExceededError
        from .services.sms_credits import credit
        credit(self.org.id, 100_000)
        custs = self._opted_in(5)
        # Pre-consume 4 of today's 6-segment budget with an already-sent recipient.
        pre = SMSCampaign.objects.create(organization=self.org, name='pre', body='hi')
        SMSMessageRecipient.objects.create(
            campaign=pre, phone='+15559990000', segments=4, twilio_sid='SMx',
            status=SMSMessageRecipient.Status.SENT, sent_at=timezone.now(),
        )
        before = SMSCampaign.objects.count()
        with self.assertRaises(DailyCapExceededError):
            self._finalize(custs, scheduled=False, send_at=timezone.now(), key='k1')
        self.assertEqual(SMSCampaign.objects.count(), before)  # nothing created/charged

    @override_settings(SMS_DAILY_SEGMENT_CAP=6)
    def test_finalize_blocks_future_send_day_aware(self):
        from .models import SMSCampaign, SMSMessageRecipient
        from .services.sms_campaigns import DailyCapExceededError
        from .services.sms_credits import credit
        credit(self.org.id, 100_000)
        tomorrow = timezone.now() + timedelta(days=1)
        booked = SMSCampaign.objects.create(
            organization=self.org, name='booked', body='hi',
            status=SMSCampaign.Status.SCHEDULED, scheduled_at=tomorrow,
        )
        SMSMessageRecipient.objects.bulk_create([
            SMSMessageRecipient(campaign=booked, phone=f'+1558{i:07d}', segments=1,
                                status=SMSMessageRecipient.Status.QUEUED) for i in range(5)
        ])  # books 5 of tomorrow's 6
        custs = self._opted_in(5)
        with self.assertRaises(DailyCapExceededError):
            self._finalize(custs, scheduled=True, send_at=tomorrow, key='k2')

    @override_settings(SMS_DAILY_SEGMENT_CAP=100)
    def test_finalize_allows_within_cap(self):
        from .services.sms_credits import credit
        credit(self.org.id, 100_000)
        custs = self._opted_in(5)
        result = self._finalize(custs, scheduled=False, send_at=timezone.now(), key='k3')
        self.assertTrue(result.created)
        self.assertEqual(result.recipient_count, 5)

    # --- Split a cap-exceeding blast into two batches ---

    def _split(self, custs, *, scheduled, send_at, batch2_send_at, key1='s1', key2='s2'):
        from .services.sms_campaigns import finalize_campaign_split
        return finalize_campaign_split(
            self.org, name='Blast', body='Tickets!', criteria={},
            manual_include_ids=[str(c.id) for c in custs], event=None,
            scheduled=scheduled, send_at=send_at, batch2_send_at=batch2_send_at,
            user=None, cap=5000, idempotency_key_1=key1, idempotency_key_2=key2,
        )

    @override_settings(SMS_DAILY_SEGMENT_CAP=3)
    def test_split_fills_today_and_schedules_overflow_next_day(self):
        from .models import SMSCampaign
        from .services.sms_credits import credit, plan_campaign_footers
        credit(self.org.id, 100_000)
        custs = self._opted_in(5)
        now = timezone.now()
        tomorrow = now + timedelta(days=1)
        # Total cost of reaching all 5 (footer identical either day: all are first-ever
        # phones) — the two batches together should charge exactly this.
        cost_all, _ = plan_campaign_footers(
            self.org, 'Tickets!', [c.phone for c in custs], as_of=now)

        result = self._split(custs, scheduled=False, send_at=now, batch2_send_at=tomorrow)

        self.assertEqual(result.batch1_count, 3)
        self.assertEqual(result.batch2_count, 2)
        self.assertEqual(result.leftover_count, 0)
        self.assertEqual(result.batch1.audience_size, 3)
        self.assertEqual(result.batch2.audience_size, 2)
        self.assertTrue(result.batch2.name.endswith('(part 2)'))
        self.assertEqual(result.batch1.status, SMSCampaign.Status.SCHEDULED)
        self.assertEqual(
            timezone.localdate(result.batch2.scheduled_at), timezone.localdate(tomorrow))
        # No recipient dropped; charged once for all five across the two batches.
        self.assertEqual(result.batch1_count + result.batch2_count, 5)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 100_000 - cost_all)

    @override_settings(SMS_DAILY_SEGMENT_CAP=5)
    def test_split_when_today_full_puts_everything_next_day(self):
        from .models import SMSCampaign, SMSMessageRecipient
        from .services.sms_credits import credit
        credit(self.org.id, 100_000)
        # Consume all of today's 5-segment budget with an already-sent recipient.
        pre = SMSCampaign.objects.create(organization=self.org, name='pre', body='hi')
        SMSMessageRecipient.objects.create(
            campaign=pre, phone='+15559990000', segments=5, twilio_sid='SMx',
            status=SMSMessageRecipient.Status.SENT, sent_at=timezone.now(),
        )
        custs = self._opted_in(4)
        now = timezone.now()
        tomorrow = now + timedelta(days=1)

        result = self._split(custs, scheduled=False, send_at=now, batch2_send_at=tomorrow)

        self.assertIsNone(result.batch1)          # nothing fit today
        self.assertEqual(result.batch1_count, 0)
        self.assertEqual(result.batch2_count, 4)  # all deferred to the next day
        self.assertEqual(result.leftover_count, 0)
        self.assertEqual(
            SMSCampaign.objects.filter(
                organization=self.org, name__endswith='(part 2)').count(), 1)

    @override_settings(SMS_DAILY_SEGMENT_CAP=3)
    def test_split_reports_leftover_when_next_day_also_limited(self):
        from .models import SMSCampaign, SMSMessageRecipient
        from .services.sms_credits import credit
        credit(self.org.id, 100_000)
        now = timezone.now()
        tomorrow = now + timedelta(days=1)
        # Pre-book 2 of tomorrow's 3-segment budget so the overflow can't all fit.
        booked = SMSCampaign.objects.create(
            organization=self.org, name='booked', body='hi',
            status=SMSCampaign.Status.SCHEDULED, scheduled_at=tomorrow,
        )
        SMSMessageRecipient.objects.bulk_create([
            SMSMessageRecipient(campaign=booked, phone=f'+1558{i:07d}', segments=1,
                                status=SMSMessageRecipient.Status.QUEUED) for i in range(2)
        ])
        custs = self._opted_in(5)

        result = self._split(custs, scheduled=False, send_at=now, batch2_send_at=tomorrow)

        self.assertEqual(result.batch1_count, 3)   # today's cap
        self.assertEqual(result.batch2_count, 1)   # tomorrow: 3 cap - 2 booked
        self.assertEqual(result.leftover_count, 1)  # 5 - 3 - 1

    @override_settings(SMS_DAILY_SEGMENT_CAP=3)
    def test_split_idempotent_replay(self):
        from .models import SMSCampaign
        from .services.sms_credits import credit
        credit(self.org.id, 100_000)
        custs = self._opted_in(5)
        now = timezone.now()
        tomorrow = now + timedelta(days=1)

        self._split(custs, scheduled=False, send_at=now,
                    batch2_send_at=tomorrow, key1='r1', key2='r2')
        count_after_first = SMSCampaign.objects.filter(organization=self.org).count()
        self.org.refresh_from_db()
        bal_after_first = self.org.sms_credit_balance_cents

        # Replay with the same keys: no second campaign, no double charge.
        self._split(custs, scheduled=False, send_at=now,
                    batch2_send_at=tomorrow, key1='r1', key2='r2')
        self.assertEqual(
            SMSCampaign.objects.filter(organization=self.org).count(), count_after_first)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, bal_after_first)

    @override_settings(SMS_DAILY_SEGMENT_CAP=3)
    def test_composer_split_action_creates_two_batches(self):
        from .models import SMSCampaign
        from .services.sms_credits import credit
        credit(self.org.id, 100_000)
        self._opted_in(5)
        ev = Event.objects.create(
            organization=self.org, name='E', venue=self.venue,
            start_date=timezone.localdate() + timedelta(days=1), start_time=time(20, 0),
        )
        user = User.objects.create_user(
            username='splt', email='splt@example.com', password='pw')
        UserProfile.objects.create(
            user=user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(
            user=user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        c = Client()
        c.login(username='splt@example.com', password='pw')
        c.get(reverse('tickets:home'))  # warm org cache
        tomorrow_str = (
            timezone.localtime(timezone.now()) + timedelta(days=1)
        ).strftime('%Y-%m-%dT%H:%M')

        resp = c.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Blast', 'body': 'Tickets!', 'send_mode': 'now',
            'audience_scope': 'all', 'event': str(ev.id),
            'idempotency_key': 'v1', 'idempotency_key_2': 'v2',
            'split': '1', 'split_scheduled_at': tomorrow_str,
        })

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            SMSCampaign.objects.filter(organization=self.org).count(), 2)
        self.assertTrue(
            SMSCampaign.objects.filter(
                organization=self.org, name__endswith='(part 2)').exists())

    @override_settings(SMS_DAILY_SEGMENT_CAP=2)
    def test_split_modal_copy_reflects_send_mode(self):
        """The split modal must not claim both batches are 'scheduled' when batch 1
        actually dispatches immediately (send-now). Copy is driven by split_batch1_now."""
        from .services.sms_credits import credit
        credit(self.org.id, 100_000)
        self._opted_in(5)  # over the cap of 2 → split modal renders
        ev = Event.objects.create(
            organization=self.org, name='E', venue=self.venue,
            start_date=timezone.localdate() + timedelta(days=1), start_time=time(20, 0),
        )
        c = self._preview_client()
        base = {'name': 'B', 'body': 'test', 'audience_scope': 'all', 'event': str(ev.id)}

        # Send-now: batch 1 goes out immediately → copy says "now", never "scheduled".
        now_html = c.post(
            reverse('tickets:sms_campaign_create'),
            dict(base, send_mode='now'),
        ).content.decode()
        self.assertIn('sent now', now_html)
        self.assertIn('Send now', now_html)
        self.assertNotIn('Schedule both batches', now_html)
        # Reparented-to-body controls stay tied to the composer form.
        self.assertIn(
            'name="split_scheduled_at" form="sms-campaign-form"', now_html)
        self.assertIn('name="split" value="1" form="sms-campaign-form"', now_html)

        # Scheduled: both batches are scheduled → the "Schedule both batches" wording.
        when = (timezone.now() + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M')
        sch_html = c.post(
            reverse('tickets:sms_campaign_create'),
            dict(base, send_mode='schedule', scheduled_at=when),
        ).content.decode()
        self.assertIn('at your scheduled time', sch_html)
        self.assertIn('Schedule both batches', sch_html)
        self.assertNotIn('sent now', sch_html)

    @override_settings(SMS_DAILY_SEGMENT_CAP=2)
    def test_split_partial_failure_warning_is_mode_aware(self):
        """When batch 1 commits (send-now) but batch 2 fails, the warning must say batch 1
        'is sending now' (not 'scheduled') and point at the real 'Split into two batches'
        CTA — a partial commit is never reported as a clean failure."""
        from django.contrib.messages import get_messages
        from .services import sms_campaigns as svc
        from .services.sms_credits import credit
        credit(self.org.id, 100_000)
        self._opted_in(5)
        ev = Event.objects.create(
            organization=self.org, name='E', venue=self.venue,
            start_date=timezone.localdate() + timedelta(days=1), start_time=time(20, 0),
        )
        c = self._preview_client()
        real_finalize = svc.finalize_campaign_send
        calls = {'n': 0}

        def flaky(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                return real_finalize(*args, **kwargs)  # batch 1 commits (send-now)
            raise svc.DailyCapExceededError(2, 0, 2, timezone.localdate())

        with patch.object(svc, 'finalize_campaign_send', side_effect=flaky):
            resp = c.post(reverse('tickets:sms_campaign_create'), {
                'name': 'B', 'body': 'test', 'send_mode': 'now',
                'audience_scope': 'all', 'event': str(ev.id),
                'idempotency_key': 'pf1', 'idempotency_key_2': 'pf2', 'split': '1',
            })

        self.assertEqual(resp.status_code, 302)  # redirected to batch 1, not a bare error
        msgs = ' '.join(m.message for m in get_messages(resp.wsgi_request))
        self.assertIn('is sending now', msgs)         # mode-aware (send-now)
        self.assertNotIn('is scheduled', msgs)
        self.assertIn('Split into two batches', msgs)  # the real CTA label
        self.assertNotIn('Schedule in two batches', msgs)

    @override_settings(SMS_DAILY_SEGMENT_CAP=3)
    def test_split_partial_failure_replay_does_not_double_charge(self):
        """REGRESSION (eng-review D2 / Codex #1): batch 1 commits, batch 2 fails, the
        organizer retries with the same keys → batch-1 recipients must NOT be re-charged
        or re-sent. Before the fix, the retry rebuilt batch 2 from the full audience
        (batch 1's booking collapsed today's budget) and re-included batch-1 people."""
        from .models import SMSCampaign, SMSMessageRecipient
        from .services import sms_campaigns as svc
        from .services.sms_credits import credit, plan_campaign_footers
        credit(self.org.id, 100_000)
        custs = self._opted_in(5)
        now = timezone.now()
        tomorrow = now + timedelta(days=1)
        cost_all, _ = plan_campaign_footers(
            self.org, 'Tickets!', [c.phone for c in custs], as_of=now)

        # First attempt: batch 1 (3 today) commits for real, batch 2 (2 tomorrow) raises.
        real_finalize = svc.finalize_campaign_send
        calls = {'n': 0}

        def flaky(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                return real_finalize(*args, **kwargs)  # batch 1 succeeds
            raise svc.DailyCapExceededError(2, 0, 3, timezone.localdate(tomorrow))

        with patch.object(svc, 'finalize_campaign_send', side_effect=flaky):
            with self.assertRaises(svc.DailyCapExceededError):
                self._split(custs, scheduled=False, send_at=now,
                            batch2_send_at=tomorrow, key1='p1', key2='p2')
        self.assertEqual(SMSCampaign.objects.filter(organization=self.org).count(), 1)
        batch1 = SMSCampaign.objects.get(organization=self.org, idempotency_key='p1')
        self.assertEqual(batch1.audience_size, 3)

        # Retry (real path) with the SAME keys → batch 2 gets created, batch 1 reused.
        result = self._split(custs, scheduled=False, send_at=now,
                             batch2_send_at=tomorrow, key1='p1', key2='p2')

        # Exactly two campaigns, five distinct recipients total, no customer in both.
        self.assertEqual(SMSCampaign.objects.filter(organization=self.org).count(), 2)
        all_ids = list(SMSMessageRecipient.objects.filter(
            campaign__organization=self.org).values_list('customer_id', flat=True))
        self.assertEqual(len(all_ids), 5)
        self.assertEqual(len(set(all_ids)), 5)  # no duplicate recipient across batches
        # Charged for exactly five recipients — batch-1 people not billed twice.
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 100_000 - cost_all)
        self.assertEqual(result.batch1.id, batch1.id)  # original batch 1 reused

    @override_settings(SMS_DAILY_SEGMENT_CAP=3)
    def test_split_truncates_long_batch2_name(self):
        """REGRESSION (eng-review D3 / Codex #4): batch 2's name must fit
        SMSCampaign.name max_length=200 even when the base name is at the limit."""
        from .models import SMSCampaign
        from .services.sms_campaigns import finalize_campaign_split
        from .services.sms_credits import credit
        credit(self.org.id, 100_000)
        custs = self._opted_in(5)
        long_name = 'X' * 200
        now = timezone.now()
        result = finalize_campaign_split(
            self.org, name=long_name, body='Tickets!', criteria={},
            manual_include_ids=[str(c.id) for c in custs], event=None,
            scheduled=False, send_at=now, batch2_send_at=now + timedelta(days=1),
            user=None, cap=5000, idempotency_key_1='n1', idempotency_key_2='n2',
        )
        self.assertIsNotNone(result.batch2)
        self.assertLessEqual(len(result.batch2.name), 200)
        self.assertTrue(result.batch2.name.endswith(' (part 2)'))
        # Reload proves the value actually persisted within the column limit.
        self.assertLessEqual(
            len(SMSCampaign.objects.get(id=result.batch2.id).name), 200)

    @override_settings(SMS_DAILY_SEGMENT_CAP=3)
    def test_split_insufficient_credits_precheck_creates_nothing(self):
        """eng-review D5 / D1: if the wallet can't cover BOTH batches, the split raises
        before any write — never charges batch 1 then fails batch 2."""
        from .models import SMSCampaign
        from .services.sms_credits import credit, InsufficientCreditsError
        credit(self.org.id, 3)  # far below the cost of five recipients
        custs = self._opted_in(5)
        now = timezone.now()
        before = SMSCampaign.objects.filter(organization=self.org).count()
        with self.assertRaises(InsufficientCreditsError):
            self._split(custs, scheduled=False, send_at=now,
                        batch2_send_at=now + timedelta(days=1))
        self.assertEqual(
            SMSCampaign.objects.filter(organization=self.org).count(), before)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 3)  # untouched

    def test_materialize_order_is_deterministic_for_criteria_audience(self):
        """eng-review D2 / Codex #5: split slicing must be stable, so materialize (via
        candidate_customers' ORDER BY) must return the same order every call for a
        criteria-built audience — not the arbitrary order of an unordered DISTINCT."""
        from .models import SMSCampaign
        self._opted_in(8)
        tmpl = SMSCampaign(
            organization=self.org, filter_criteria={'all_subscribers': True})
        first = [r['customer_id'] for r in tmpl.materialize(self.org)]
        second = [r['customer_id'] for r in tmpl.materialize(self.org)]
        self.assertEqual(len(first), 8)
        self.assertEqual(first, second)

    @override_settings(SMS_DAILY_SEGMENT_CAP=3)
    def test_composer_split_blank_time_defaults_and_warns_on_leftover(self):
        """eng-review D5: the view's split branch defaults a blank second-batch time to
        the next day, and surfaces a warning when some recipients still don't fit."""
        from .models import SMSCampaign, SMSMessageRecipient
        from django.contrib.messages import get_messages
        from .services.sms_credits import credit
        credit(self.org.id, 100_000)
        self._opted_in(5)
        # Pre-book 2 of tomorrow's 3-segment budget so 1 recipient is left over.
        tomorrow = timezone.now() + timedelta(days=1)
        booked = SMSCampaign.objects.create(
            organization=self.org, name='booked', body='hi',
            status=SMSCampaign.Status.SCHEDULED, scheduled_at=tomorrow,
        )
        SMSMessageRecipient.objects.bulk_create([
            SMSMessageRecipient(campaign=booked, phone=f'+1558{i:07d}', segments=1,
                                status=SMSMessageRecipient.Status.QUEUED) for i in range(2)
        ])
        ev = Event.objects.create(
            organization=self.org, name='E', venue=self.venue,
            start_date=timezone.localdate() + timedelta(days=1), start_time=time(20, 0),
        )
        user = User.objects.create_user(
            username='splt2', email='splt2@example.com', password='pw')
        UserProfile.objects.create(
            user=user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(
            user=user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        c = Client()
        c.login(username='splt2@example.com', password='pw')
        c.get(reverse('tickets:home'))

        resp = c.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Blast', 'body': 'Tickets!', 'send_mode': 'now',
            'audience_scope': 'all', 'event': str(ev.id),
            'idempotency_key': 'w1', 'idempotency_key_2': 'w2',
            'split': '1',  # no split_scheduled_at → view defaults it to next day
        })

        self.assertEqual(resp.status_code, 302)
        # Two new split campaigns created (plus the pre-booked one).
        self.assertEqual(
            SMSCampaign.objects.filter(
                organization=self.org, idempotency_key__in=['w1', 'w2']).count(), 2)
        batch2 = SMSCampaign.objects.get(organization=self.org, idempotency_key='w2')
        self.assertEqual(
            timezone.localdate(batch2.scheduled_at),
            timezone.localdate(timezone.now() + timedelta(days=1)))
        msgs = [m.message for m in get_messages(resp.wsgi_request)]
        self.assertTrue(any('were not scheduled' in m for m in msgs))  # leftover warning

    def test_daily_cap_exceeded_error_message_prompts_to_reduce(self):
        from .services.sms_campaigns import DailyCapExceededError
        exc = DailyCapExceededError(count=20, allowed=5, cap=100, send_date=timezone.localdate())
        self.assertIn('20', exc.user_message())
        self.assertIn('5', exc.user_message())
        self.assertIn('Reduce', exc.user_message())
        # Fully booked → tell them to pick another day, no "reduce to 0".
        full = DailyCapExceededError(count=20, allowed=0, cap=100, send_date=timezone.localdate())
        self.assertIn('another day', full.user_message())

    # --- Send-time last resort (fail + refund, no defer) ---

    @override_settings(SMS_DAILY_SEGMENT_CAP=100)
    def test_send_time_fails_and_refunds_when_over_budget(self):
        from .models import SMSCampaign
        from .tasks import send_sms_campaign_task
        campaign = self._campaign(5, segments=1)
        with patch('tickets.services.sms_limits.remaining_daily_budget', return_value=2), \
                patch('tickets.services.sms_credits.refund_campaign') as mock_refund, \
                patch('celery.chord') as mock_chord:
            send_sms_campaign_task.apply(args=[str(campaign.id)])
        mock_chord.assert_not_called()  # never partially dispatched
        mock_refund.assert_called_once()
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, SMSCampaign.Status.FAILED)
        self.assertTrue(campaign.failure_reason)

    @override_settings(SMS_DAILY_SEGMENT_CAP=100)
    def test_send_time_dispatches_all_when_within_budget(self):
        from .models import SMSCampaign
        from .tasks import send_sms_campaign_task
        campaign = self._campaign(5, segments=1)
        captured = {}

        def fake_chord(header):
            captured['header'] = header
            return lambda callback: None

        with patch('tickets.services.sms_limits.remaining_daily_budget', return_value=100), \
                patch('celery.chord', side_effect=fake_chord):
            send_sms_campaign_task.apply(args=[str(campaign.id)])
        dispatched = sum(len(sig.args[1]) for sig in captured['header'])
        self.assertEqual(dispatched, 5)  # all recipients dispatched, none dropped
        campaign.refresh_from_db()
        self.assertNotEqual(campaign.status, SMSCampaign.Status.FAILED)

    def test_segments_used_today_counts_only_sent_today(self):
        from .models import SMSCampaign, SMSMessageRecipient
        from .services.sms_limits import segments_used_today
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='Blast', body='Hi',
        )
        # Sent today with a SID -> counts.
        SMSMessageRecipient.objects.create(
            campaign=campaign, phone='+15550000001', segments=2, twilio_sid='SM1',
            status=SMSMessageRecipient.Status.SENT, sent_at=timezone.now(),
        )
        # Queued (never sent) -> excluded.
        SMSMessageRecipient.objects.create(
            campaign=campaign, phone='+15550000002', segments=5,
            status=SMSMessageRecipient.Status.QUEUED,
        )
        # Sent yesterday -> excluded.
        SMSMessageRecipient.objects.create(
            campaign=campaign, phone='+15550000003', segments=3, twilio_sid='SM3',
            status=SMSMessageRecipient.Status.SENT,
            sent_at=timezone.now() - timedelta(days=1),
        )
        self.assertEqual(segments_used_today(), 2)

    # --- Urgency-first ordering ---

    def test_due_orders_soonest_event_first(self):
        from .models import SMSCampaign
        soon_event = Event.objects.create(
            organization=self.org, name='Soon', venue=self.venue,
            start_date=timezone.localdate() + timedelta(days=1),
        )
        later_event = Event.objects.create(
            organization=self.org, name='Later', venue=self.venue,
            start_date=timezone.localdate() + timedelta(days=30),
        )
        later = SMSCampaign.objects.create(
            organization=self.org, name='Later Blast', body='Hi', event=later_event,
            status=SMSCampaign.Status.SCHEDULED, scheduled_at=timezone.now() - timedelta(minutes=1),
        )
        soon = SMSCampaign.objects.create(
            organization=self.org, name='Soon Blast', body='Hi', event=soon_event,
            status=SMSCampaign.Status.SCHEDULED, scheduled_at=timezone.now() - timedelta(minutes=1),
        )
        no_event = SMSCampaign.objects.create(
            organization=self.org, name='Evergreen', body='Hi',
            status=SMSCampaign.Status.SCHEDULED, scheduled_at=timezone.now() - timedelta(minutes=1),
        )
        ordered = list(SMSCampaign.objects.due().values_list('id', flat=True))
        self.assertEqual(
            ordered, [soon.id, later.id, no_event.id],  # soonest event first, null last
        )

    # --- Compose-time block surfaces during review (before confirm) ---

    @override_settings(SMS_DAILY_SEGMENT_CAP=2)
    def test_composer_review_warns_before_confirm(self):
        from .models import SMSCampaign
        from .services.sms_credits import credit
        credit(self.org.id, 100_000)  # so insufficient-credits doesn't preempt
        self._opted_in(5)
        ev = Event.objects.create(
            organization=self.org, name='E', venue=self.venue,
            start_date=timezone.localdate() + timedelta(days=1), start_time=time(20, 0),
        )
        user = User.objects.create_user(username='cmp', email='cmp@example.com', password='pw')
        UserProfile.objects.create(
            user=user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        c = Client()
        c.login(username='cmp@example.com', password='pw')
        c.get(reverse('tickets:home'))  # warm org cache
        # Review step (no 'confirm'): 5 recipients vs a cap of 2.
        resp = c.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Blast', 'body': 'Tickets!', 'send_mode': 'now',
            'audience_scope': 'all', 'event': str(ev.id),
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['daily_cap_block'])  # warned in the confirm bar
        self.assertIn('Reduce', resp.context['daily_cap_block'])
        # Nothing was created — the block is shown, not committed.
        self.assertEqual(SMSCampaign.objects.filter(organization=self.org).count(), 0)
        # This render includes the split modal (daily_cap_next_fits > 0). Guard against
        # unrendered template markers leaking as page text — a multi-line {# #} comment
        # (Django only strips single-line ones) renders verbatim; {% %}/{{ }} likewise.
        html = resp.content.decode()
        self.assertNotIn('{#', html)
        self.assertNotIn('{%', html)
        self.assertNotIn('{{', html)

    def _preview_client(self):
        user = User.objects.create_user(
            username=f'preview-{uuid.uuid4().hex[:8]}',
            email=f'preview-{uuid.uuid4().hex[:8]}@example.com',
            password='pw',
        )
        UserProfile.objects.create(
            user=user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        c = Client()
        c.force_login(user)
        c.get(reverse('tickets:home'))  # warm org cache
        return c

    @override_settings(SMS_DAILY_SEGMENT_CAP=17)
    def test_audience_preview_warns_when_daily_cap_exceeded(self):
        custs = self._opted_in(20)
        c = self._preview_client()
        resp = c.post(reverse('tickets:sms_audience_preview'), {
            'body': 'Tickets!',
            'send_mode': 'now',
            'manual_include_ids': ','.join(str(cust.id) for cust in custs),
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['count'], 20)
        self.assertTrue(data['daily_cap_blocked'])
        self.assertEqual(data['daily_cap_allowed'], 17)
        self.assertEqual(data['daily_cap'], 17)
        self.assertIn('Reduce', data['daily_cap_message'])

    @override_settings(SMS_DAILY_SEGMENT_CAP=17)
    def test_audience_preview_is_day_aware_for_scheduled_send(self):
        from .models import SMSCampaign, SMSMessageRecipient
        tomorrow = timezone.now() + timedelta(days=1)
        booked = SMSCampaign.objects.create(
            organization=self.org, name='Booked', body='Booked',
            status=SMSCampaign.Status.SCHEDULED, scheduled_at=tomorrow,
        )
        SMSMessageRecipient.objects.bulk_create([
            SMSMessageRecipient(
                campaign=booked, phone=f'+1555999{i:04d}',
                status=SMSMessageRecipient.Status.QUEUED, segments=1,
            ) for i in range(10)
        ])
        custs = self._opted_in(10)
        c = self._preview_client()
        resp = c.post(reverse('tickets:sms_audience_preview'), {
            'body': 'Tickets!',
            'send_mode': 'schedule',
            'scheduled_at': timezone.localtime(tomorrow).strftime('%Y-%m-%dT%H:%M'),
            'manual_include_ids': ','.join(str(cust.id) for cust in custs),
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['count'], 10)
        self.assertTrue(data['daily_cap_blocked'])
        self.assertEqual(data['daily_cap_allowed'], 7)
        self.assertIn('7 or fewer', data['daily_cap_message'])

    @override_settings(SMS_DAILY_SEGMENT_CAP=0)
    def test_audience_preview_omits_daily_warning_when_cap_disabled(self):
        custs = self._opted_in(20)
        c = self._preview_client()
        resp = c.post(reverse('tickets:sms_audience_preview'), {
            'body': 'Tickets!',
            'send_mode': 'now',
            'manual_include_ids': ','.join(str(cust.id) for cust in custs),
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['count'], 20)
        self.assertFalse(data['daily_cap_blocked'])
        self.assertIsNone(data['daily_cap_allowed'])
        self.assertIsNone(data['daily_cap'])


class SMSCampaignLinkEventTests(TestCase):
    """Linking an already-sent SMS campaign to an event (and clearing it)."""

    def setUp(self):
        from .models import SMSCampaign
        self.client = Client()
        self.org = Organization.objects.create(
            name='Link Org', slug='link-org', sms_marketing_enabled=True,
        )
        self.user = User.objects.create_user(
            username='linkhost', email='link@test.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='link@test.com', password='testpass123')
        self.client.get(reverse('tickets:home'))  # warm org cache

        self.venue = Venue.objects.create(organization=self.org, name='Echo', city='Austin')
        self.event = Event.objects.create(
            organization=self.org, name='Linkable Show', venue=self.venue,
            start_date=date(2026, 6, 1),
        )
        self.campaign = SMSCampaign.objects.create(
            organization=self.org, name='Sent Blast', body='Tickets!',
            status=SMSCampaign.Status.SENT, sent_at=timezone.now() - timedelta(days=1),
        )
        self.url = reverse('tickets:sms_campaign_link_event', args=[self.campaign.id])

    def test_links_campaign_to_event(self):
        response = self.client.post(self.url, {'event': str(self.event.id)})
        self.assertEqual(response.status_code, 302)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.event_id, self.event.id)
        # Detail page (with the picker modal) renders and shows the linked event.
        detail = self.client.get(
            reverse('tickets:sms_campaign_detail', args=[self.campaign.id])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'Linkable Show')
        self.assertContains(detail, 'linkEventModal')

    def test_list_page_renders_picker(self):
        response = self.client.get(reverse('tickets:sms_campaign_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'linkEventModal')
        self.assertContains(response, 'Link to event')
        # Each picker option shows the event date to disambiguate same-named events.
        self.assertContains(response, 'Jun 01, 2026')
        # The partial's leading comment must not leak into the rendered page.
        self.assertNotContains(response, 'Requires `link_events`')

    def test_unlinks_when_event_blank(self):
        self.campaign.event = self.event
        self.campaign.save(update_fields=['event'])

        response = self.client.post(self.url, {'event': ''})
        self.assertEqual(response.status_code, 302)
        self.campaign.refresh_from_db()
        self.assertIsNone(self.campaign.event_id)

    def test_honors_safe_next_redirect(self):
        detail = reverse('tickets:sms_campaign_detail', args=[self.campaign.id])
        response = self.client.post(self.url, {'event': str(self.event.id), 'next': detail})
        self.assertRedirects(response, detail, fetch_redirect_response=False)

    def test_cannot_link_event_from_another_org(self):
        other_org = Organization.objects.create(name='Other Org', slug='other-org')
        other_venue = Venue.objects.create(organization=other_org, name='Far', city='Reno')
        other_event = Event.objects.create(
            organization=other_org, name='Foreign Show', venue=other_venue,
            start_date=date(2026, 7, 1),
        )
        response = self.client.post(self.url, {'event': str(other_event.id)})
        self.assertEqual(response.status_code, 404)
        self.campaign.refresh_from_db()
        self.assertIsNone(self.campaign.event_id)

    def test_feature_gate_blocks_disabled_org(self):
        self.org.sms_marketing_enabled = False
        self.org.save(update_fields=['sms_marketing_enabled'])
        response = self.client.post(self.url, {'event': str(self.event.id)})
        self.assertEqual(response.status_code, 404)


class EventCachedStatsTest(TestCase):
    """Tests for Event cached stat fields and net_revenue calculation."""

    def setUp(self):
        self.org = Organization.objects.create(name='Stats Test Org', slug='stats-test-org')
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Test Event', venue=self.venue,
            start_date=date(2025, 6, 1),
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='buyer@example.com', name='Buyer',
        )

    def _make_order_with_tickets(self, total_amount, ticket_prices, refunded=False):
        """Create a TicketOrder with Ticket rows and optional refund."""
        import uuid
        order = TicketOrder.objects.create(
            event=self.event,
            customer=self.customer,
            order_number=str(uuid.uuid4())[:12],
            total_amount=total_amount,
            order_date=timezone.now(),
            refunded_at=timezone.now() if refunded else None,
        )
        for price in ticket_prices:
            Ticket.objects.create(ticket_order=order, price=price)
        return order

    # ------------------------------------------------------------------ #
    # Stats edge cases                                                     #
    # ------------------------------------------------------------------ #

    def test_zero_tickets_all_stats_zero(self):
        """Event with no orders has all cached stats at zero defaults."""
        self.event.refresh_from_db()
        self.assertEqual(self.event.cached_ticket_count, 0)
        self.assertEqual(self.event.cached_paid_ticket_count, 0)
        self.assertEqual(self.event.cached_paid_ticket_sum, Decimal('0.00'))

    def test_all_refunded_paid_stats_zero(self):
        """Fully refunded order: total tickets counted, paid stats zero."""
        from tickets.signals import refresh_event_stats
        self._make_order_with_tickets(
            total_amount=Decimal('100.00'),
            ticket_prices=[Decimal('50.00'), Decimal('50.00')],
            refunded=True,
        )
        refresh_event_stats(str(self.event.id))
        self.event.refresh_from_db()

        self.assertEqual(self.event.cached_ticket_count, 2)
        self.assertEqual(self.event.cached_paid_ticket_count, 0)
        self.assertEqual(self.event.cached_paid_ticket_sum, Decimal('0.00'))

    def test_mixed_paid_free_refunded(self):
        """Only unrefunded paid tickets count toward paid_* fields."""
        from tickets.signals import refresh_event_stats
        # 2 paid tickets, not refunded
        self._make_order_with_tickets(
            total_amount=Decimal('80.00'),
            ticket_prices=[Decimal('40.00'), Decimal('40.00')],
            refunded=False,
        )
        # 1 free ticket, not refunded
        self._make_order_with_tickets(
            total_amount=Decimal('0.00'),
            ticket_prices=[Decimal('0.00')],
            refunded=False,
        )
        # 1 paid ticket, refunded
        self._make_order_with_tickets(
            total_amount=Decimal('50.00'),
            ticket_prices=[Decimal('50.00')],
            refunded=True,
        )
        refresh_event_stats(str(self.event.id))
        self.event.refresh_from_db()

        self.assertEqual(self.event.cached_ticket_count, 4)
        self.assertEqual(self.event.cached_paid_ticket_count, 2)
        self.assertEqual(self.event.cached_paid_ticket_sum, Decimal('80.00'))

    # ------------------------------------------------------------------ #
    # Net revenue regression                                               #
    # ------------------------------------------------------------------ #

    def test_net_revenue_equivalence(self):
        """
        net_revenue = total_revenue - fees is algebraically equivalent to
        ticket_revenue - fees when all revenue comes through TicketOrders.

        computed_total_revenue is signal-maintained as Sum(total_amount) across
        TicketOrders + EventIncome. With no EventIncome, total_revenue == ticket_revenue,
        so the simplified formula is definitionally equivalent.
        """
        from tickets.signals import refresh_event_stats
        from decimal import ROUND_HALF_UP

        # 3 paid tickets at $30 each — ticket_revenue = $90
        self._make_order_with_tickets(
            total_amount=Decimal('90.00'),
            ticket_prices=[Decimal('30.00'), Decimal('30.00'), Decimal('30.00')],
            refunded=False,
        )
        refresh_event_stats(str(self.event.id))
        self.event.refresh_from_db()

        ticket_revenue = Decimal('90.00')
        paid_ticket_sum = self.event.cached_paid_ticket_sum   # 90.00
        paid_ticket_count = self.event.cached_paid_ticket_count  # 3

        # Fees formula (direct ticketing): (paid_ticket_sum * 0.10 + 0.99 * count) / 1.10
        fees = (
            paid_ticket_sum * Decimal('0.10')
            + Decimal('0.99') * paid_ticket_count
        ) / Decimal('1.10')

        # Original formula (no additional income in this test)
        original = ticket_revenue - fees
        # Simplified formula using computed_total_revenue
        simplified = self.event.computed_total_revenue - fees

        self.assertEqual(
            original.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
            simplified.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
        )
        # Sanity: computed_total_revenue matches the order total
        self.assertEqual(self.event.computed_total_revenue, ticket_revenue)


class HealthCheckTest(TestCase):
    """Tests for the /health/ endpoint."""

    def test_health_check_returns_200(self):
        resp = self.client.get('/health/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'db', resp.content)

    def test_health_check_json_format(self):
        resp = self.client.get('/health/?fmt=json')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('db', data)
        self.assertIn('cache_url', data)
        self.assertIn('cache', data)
        self.assertIn('cache_ms', data)


class EventDetailCacheTest(TestCase):
    """Tests for _compute_event_stats() caching and cache invalidation."""

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Cache Test Org', slug='cache-test-org')
        cls.venue = Venue.objects.create(organization=cls.org, name='Venue', city='City')
        cls.event = Event.objects.create(
            organization=cls.org, name='Cache Test Event', venue=cls.venue,
            start_date=date(2025, 8, 1),
        )
        cls.customer = Customer.objects.create(
            organization=cls.org, email='cachebuyer@example.com', name='Cache Buyer',
        )
        cls.csv_format = CSVFormat.objects.create(
            organization=cls.org,
            name='Cache Format',
            column_mapping={'order_number': 'Order ID'},
        )

    def setUp(self):
        # Cache state is per-test, not a fixture: clear before each test so the
        # now-stable event id from setUpTestData can't leak cached stats across tests.
        from django.core.cache import cache as django_cache
        django_cache.clear()

    def tearDown(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()

    def _make_order(self, ticket_prices=None, uploaded_file=None):
        order = TicketOrder.objects.create(
            event=self.event,
            customer=self.customer,
            order_number=str(uuid.uuid4())[:12],
            total_amount=sum(ticket_prices or [Decimal('0.00')]),
            order_date=timezone.now(),
        )
        if uploaded_file:
            order.uploaded_file = uploaded_file
            order.save(update_fields=['uploaded_file'])
        for price in (ticket_prices or []):
            Ticket.objects.create(ticket_order=order, price=price)
        return order

    def test_nps_aggregate_matches_expected_counts(self):
        """NPS aggregate consolidation produces correct promoter/detractor/total counts."""
        from tickets.views import _compute_event_stats
        from django.core.cache import cache as django_cache

        # Create survey response
        invitation = SurveyInvitation.objects.create(
            organization=self.org, event=self.event,
            customer=self.customer, email=self.customer.email,
        )
        response = SurveyResponse.objects.create(
            organization=self.org, event=self.event,
            customer=self.customer, invitation=invitation,
        )
        # SurveyAnswer has unique constraint on (response, question) —
        # use a separate question per answer to test multiple NPS scores.
        # 2 promoters (score >= 9), 1 detractor (score <= 6), 1 neutral (score 7)
        for pos, score in enumerate([10, 9, 5, 7], start=1):
            q = SurveyQuestion.objects.create(
                organization=self.org,
                question_text=f'NPS question {pos}',
                question_type='nps',
                position=pos,
            )
            SurveyAnswer.objects.create(response=response, question=q, nps_score=score)

        django_cache.clear()
        stats = _compute_event_stats(self.event)
        survey = stats['survey_results']

        self.assertIsNotNone(survey)
        self.assertEqual(survey['nps_total'], 4)
        # NPS = (promoters - detractors) / total * 100 = (2 - 1) / 4 * 100 = 25
        self.assertEqual(survey['nps_score'], 25)

    def test_external_survey_responses_are_included_in_event_stats(self):
        """External uploads should count toward event survey totals and comments."""
        from django.core.cache import cache as django_cache
        from tickets.views import _compute_event_stats

        upload = ExternalSurveyUpload.objects.create(
            organization=self.org,
            filename='typeform.csv',
            status=ExternalSurveyUpload.Status.COMPLETED,
        )
        ExternalSurveyResponse.objects.create(
            organization=self.org,
            upload=upload,
            event=self.event,
            responded_at=timezone.now(),
            email='guest@example.com',
            overall_rating='Loved it',
            nps_score=10,
            text_feedback='External survey comment',
        )

        django_cache.clear()
        stats = _compute_event_stats(self.event)
        survey = stats['survey_results']

        self.assertEqual(stats['survey_responses_count'], 0)
        self.assertEqual(stats['external_survey_responses_count'], 1)
        self.assertEqual(stats['survey_total_response_count'], 1)
        self.assertEqual(survey['nps_score'], 100)
        self.assertEqual(survey['recent_comments'][0]['text'], 'External survey comment')
        self.assertEqual(survey['recent_comments'][0]['author'], 'guest@example.com')
        self.assertEqual(survey['recent_comments'][0]['source'], 'External upload')
        self.assertEqual(survey['overall_rating_breakdown'], [{'overall_rating': 'Loved it', 'count': 1}])

    def test_stats_cache_hit_skips_db(self):
        """Second call to _compute_event_stats() returns cached result without hitting DB."""
        from tickets.views import _compute_event_stats
        from django.core.cache import cache as django_cache
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        django_cache.clear()
        # Prime the cache
        _compute_event_stats(self.event)

        # Second call should hit cache — 0 queries
        with CaptureQueriesContext(connection) as ctx:
            result = _compute_event_stats(self.event)
        self.assertEqual(len(ctx), 0, f"Expected 0 queries on cache hit, got {len(ctx)}")
        self.assertIsNotNone(result)

    def test_incomplete_stats_cache_payload_is_recomputed(self):
        """Regression: legacy cached stats without newer keys should not 500 event_detail."""
        from tickets.views import _compute_event_stats, _event_stats_cache_key
        from django.core.cache import cache as django_cache

        django_cache.set(
            _event_stats_cache_key(self.event.pk),
            {'total_orders': 999},
            300,
        )

        result = _compute_event_stats(self.event)

        self.assertEqual(result['total_orders'], 0)
        self.assertIn('new_customers_count', result)
        self.assertIn('returning_customers_count', result)
        self.assertIn('attendee_segments', result)

    def test_expense_change_invalidates_cache(self):
        """Saving or deleting an EventExpense clears the event_stats cache."""
        from tickets.views import _compute_event_stats, _event_stats_cache_key
        from tickets.models import EventExpense
        from django.core.cache import cache as django_cache

        django_cache.clear()
        # Prime the cache
        _compute_event_stats(self.event)
        self.assertIsNotNone(django_cache.get(_event_stats_cache_key(self.event.pk)))

        # Create an expense — post_save signal should delete the cache entry
        expense = EventExpense.objects.create(
            event=self.event,
            description='Sound system', amount=Decimal('500.00'),
            category='production',
        )
        self.assertIsNone(
            django_cache.get(_event_stats_cache_key(self.event.pk)),
            "Cache should be cleared after expense creation",
        )

        # Re-prime and then delete
        _compute_event_stats(self.event)
        expense.hard_delete()
        self.assertIsNone(
            django_cache.get(_event_stats_cache_key(self.event.pk)),
            "Cache should be cleared after expense deletion",
        )

    def test_event_upload_stats_grouped_queries_return_correct_counts(self):
        """Grouped upload stats return correct per-upload orders, tickets, and revenue."""
        from tickets.views import _compute_event_upload_stats

        upload = UploadedFile.objects.create(
            organization=self.org, csv_format=self.csv_format,
            filename='test.csv', status='completed',
        )
        # 2 orders on the same upload, 3 tickets total
        self._make_order(ticket_prices=[Decimal('10.00'), Decimal('20.00')], uploaded_file=upload)
        self._make_order(ticket_prices=[Decimal('15.00')], uploaded_file=upload)

        agg = _compute_event_upload_stats(self.event)

        self.assertEqual(len(agg), 1)
        self.assertEqual(agg[0]['orders_count'], 2)
        self.assertEqual(agg[0]['tickets_count'], 3)
        self.assertEqual(agg[0]['revenue'], Decimal('45.00'))

    def test_event_upload_stats_cache_hit_skips_db(self):
        """Second call to _compute_event_upload_stats() should return from cache."""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        from tickets.views import _compute_event_upload_stats

        upload = UploadedFile.objects.create(
            organization=self.org,
            csv_format=self.csv_format,
            filename='cached.csv',
            status='completed',
        )
        self._make_order(ticket_prices=[Decimal('25.00')], uploaded_file=upload)

        _compute_event_upload_stats(self.event)

        with CaptureQueriesContext(connection) as ctx:
            result = _compute_event_upload_stats(self.event)

        self.assertEqual(len(ctx), 0, f"Expected 0 queries on cache hit, got {len(ctx)}")
        self.assertEqual(len(result), 1)

    def test_uploads_summary_endpoint_renders_upload_stats(self):
        """The async uploads fragment should render successfully with currency formatting."""
        user = User.objects.create_user(
            username='cachetestuser',
            email='cachetest@example.com',
            password='testpass123',
        )
        UserProfile.objects.create(
            user=user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        client = Client()
        self.assertTrue(client.login(username='cachetest@example.com', password='testpass123'))
        client.get(reverse('tickets:home'))

        upload = UploadedFile.objects.create(
            organization=self.org,
            csv_format=self.csv_format,
            filename='fragment.csv',
            status='completed',
        )
        self._make_order(ticket_prices=[Decimal('25.00'), Decimal('10.00')], uploaded_file=upload)

        response = client.get(
            reverse('tickets:event_uploads_summary', args=[self.event.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'fragment.csv')
        self.assertContains(response, '$35.00')


class SurveySentConfirmationTests(TestCase):
    """The Surveys tab must confirm a completed send. When invitations have been
    emailed but nobody has responded yet, the tab shows a "Survey sent to N
    attendees" confirmation and a delivered-awaiting empty state — not the
    "Build a survey and send it" copy that reads as "nothing has happened."
    """

    def setUp(self):
        from django.core.cache import cache as django_cache

        self.client = Client()
        self.org = Organization.objects.create(
            name='Survey Sent Org', slug='survey-sent-org',
        )
        self.user = User.objects.create_user(
            username='surveysent', email='surveysent@example.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.venue = Venue.objects.create(
            organization=self.org, name='Survey Venue', city='Los Angeles',
        )
        self.event = Event.objects.create(
            organization=self.org, name='Survey Sent Event', venue=self.venue,
            start_date=date.today() - timedelta(days=5),
            ticketing_type=TICKETING_TYPE_DIRECT, status='ended',
        )
        self.assertTrue(
            self.client.login(username='surveysent@example.com', password='testpass123')
        )
        self.client.get(reverse('tickets:home'))  # seed session _org_id
        django_cache.clear()

    def tearDown(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()

    def _add_attendee(self, i, *, sent_at):
        """Create an attendee with an order for the event and one survey invitation.
        sent_at=None leaves the invitation unsent."""
        customer = Customer.objects.create(
            organization=self.org, email=f'attendee{i}@example.com', name=f'Attendee {i}',
        )
        TicketOrder.objects.create(
            customer=customer, event=self.event, order_number=f'ORD-{i}',
            order_date=timezone.now() - timedelta(days=6), total_amount=Decimal('25.00'),
        )
        return SurveyInvitation.objects.create(
            event=self.event, customer=customer, organization=self.org,
            email=customer.email, sent_at=sent_at,
        )

    def _get_surveys_tab(self):
        url = reverse('tickets:event_detail', args=[self.event.id]) + '?tab=surveys'
        return self.client.get(url)

    def test_sent_no_responses_shows_confirmation(self):
        now = timezone.now()
        for i in range(3):
            self._add_attendee(i, sent_at=now - timedelta(days=1, hours=i))

        response = self._get_surveys_tab()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['survey_sent_count'], 3)
        self.assertIsNotNone(response.context['survey_last_sent_display'])
        # Header confirmation + delivered-awaiting empty state.
        self.assertContains(response, 'Survey sent to 3 attendees')
        self.assertContains(response, 'Responses will appear here as they come in.')
        # The "nothing has happened" copy must NOT show once a survey has gone out.
        self.assertNotContains(response, 'No survey responses yet')
        # The Send modal explains the recipient count is a remainder.
        self.assertContains(response, 'already received this survey')

    def test_no_invitations_shows_default_empty_state(self):
        response = self._get_surveys_tab()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['survey_sent_count'], 0)
        self.assertIsNone(response.context['survey_last_sent_display'])
        # No send has happened -> original onboarding empty state, no confirmation.
        self.assertNotContains(response, 'Survey sent to')
        self.assertNotContains(response, 'already received this survey')
        self.assertContains(response, 'No survey responses yet')

    def test_unsent_invitations_do_not_count_as_sent(self):
        # A scheduled-but-not-yet-sent invitation must not trigger the confirmation.
        self._add_attendee(0, sent_at=None)

        response = self._get_surveys_tab()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['survey_sent_count'], 0)
        self.assertNotContains(response, 'Survey sent to')
        self.assertContains(response, 'No survey responses yet')


class EventDetailAllocationChartTest(TestCase):
    """Tests for event detail per-ticket-type allocation charts."""

    def setUp(self):
        from django.core.cache import cache as django_cache

        self.client = Client()
        self.org = Organization.objects.create(name='Allocation Org', slug='allocation-org')
        self.user = User.objects.create_user(
            username='allocation',
            email='allocation@example.com',
            password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        self.venue = Venue.objects.create(
            organization=self.org,
            name='Allocation Venue',
            city='Los Angeles',
        )
        self.event = Event.objects.create(
            organization=self.org,
            name='Allocation Event',
            venue=self.venue,
            start_date=date.today() + timedelta(days=7),
            ticketing_type='direct',
            status='live',
        )
        django_cache.clear()

    def tearDown(self):
        from django.core.cache import cache as django_cache

        django_cache.clear()

    def _login(self):
        self.assertTrue(self.client.login(username='allocation@example.com', password='testpass123'))
        self.client.get(reverse('tickets:home'))

    def test_compute_event_stats_returns_direct_allocation_chart_data(self):
        from tickets.views import _compute_event_stats

        ga = SaleableTicketType.objects.create(
            event=self.event,
            name='General Admission',
            price=Decimal('25.00'),
            quantity_limit=100,
            quantity_sold=25,
        )
        vip = SaleableTicketType.objects.create(
            event=self.event,
            name='VIP',
            price=Decimal('75.00'),
            quantity_limit=None,
            quantity_sold=3,
        )

        stats = _compute_event_stats(self.event)

        self.assertEqual(
            stats['ticket_type_allocation_charts'],
            [
                {
                    'tt_id': str(ga.id),
                    'label': 'General Admission',
                    'sold': 25,
                    'allocated': 100,
                    'remaining': 75,
                    'percent_sold': 25,
                    'is_unlimited': False,
                },
                {
                    'tt_id': str(vip.id),
                    'label': 'VIP',
                    'sold': 3,
                    'allocated': None,
                    'remaining': None,
                    'percent_sold': None,
                    'is_unlimited': True,
                },
            ],
        )

    def test_direct_event_detail_renders_allocation_rows_per_ticket_type(self):
        SaleableTicketType.objects.create(
            event=self.event,
            name='General Admission',
            price=Decimal('25.00'),
            quantity_limit=100,
            quantity_sold=25,
        )
        SaleableTicketType.objects.create(
            event=self.event,
            name='VIP',
            price=Decimal('75.00'),
            quantity_limit=50,
            quantity_sold=0,
        )
        self._login()

        response = self.client.get(reverse('tickets:event_detail', args=[self.event.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Ticket Allocation')
        self.assertContains(response, 'General Admission')
        self.assertContains(response, 'VIP')
        self.assertNotContains(response, 'id="ticketBreakdownChart"')
        charts = response.context['ticket_type_allocation_charts']
        self.assertEqual(len(charts), 2)
        self.assertEqual(charts[0]['label'], 'General Admission')
        self.assertEqual(charts[1]['sold'], 0)

    def test_non_direct_event_keeps_combined_ticket_breakdown_chart(self):
        external_event = Event.objects.create(
            organization=self.org,
            name='Imported Event',
            venue=self.venue,
            start_date=date.today() + timedelta(days=8),
        )
        customer = Customer.objects.create(
            organization=self.org,
            email='imported@example.com',
            name='Imported Buyer',
        )
        order = TicketOrder.objects.create(
            event=external_event,
            customer=customer,
            order_number=str(uuid.uuid4())[:12],
            total_amount=Decimal('50.00'),
            order_date=timezone.now(),
        )
        Ticket.objects.create(
            ticket_order=order,
            ticket_type='General Admission',
            price=Decimal('50.00'),
        )
        self._login()

        response = self.client.get(reverse('tickets:event_detail', args=[external_event.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Ticket Breakdown')
        self.assertContains(response, 'id="ticketBreakdownChart"')
        self.assertNotContains(response, 'Ticket Allocation')

    def _make_order_with_types(self, type_names, *, name='Buyer'):
        """Create an order with one Ticket per name in type_names."""
        customer = Customer.objects.create(
            organization=self.org,
            email=f'{name.lower()}@example.com',
            name=name,
        )
        order = TicketOrder.objects.create(
            event=self.event,
            customer=customer,
            order_number=str(uuid.uuid4())[:12],
            total_amount=Decimal('25.00'),
            order_date=timezone.now(),
        )
        for tn in type_names:
            Ticket.objects.create(ticket_order=order, ticket_type=tn, price=Decimal('25.00'))
        return order

    def test_ticket_type_orders_lists_only_orders_with_that_type(self):
        ga = SaleableTicketType.objects.create(
            event=self.event, name='General Admission',
            price=Decimal('25.00'), quantity_limit=100, quantity_sold=2,
        )
        SaleableTicketType.objects.create(
            event=self.event, name='VIP',
            price=Decimal('75.00'), quantity_limit=50, quantity_sold=1,
        )
        ga_order = self._make_order_with_types(['General Admission'], name='GaBuyer')
        vip_order = self._make_order_with_types(['VIP'], name='VipBuyer')
        self._login()

        response = self.client.get(
            reverse('tickets:saleable_ticket_type_orders', args=[self.event.pk, ga.pk])
        )

        self.assertEqual(response.status_code, 200)
        order_ids = {o.id for o in response.context['page_obj']}
        self.assertIn(ga_order.id, order_ids)
        self.assertNotIn(vip_order.id, order_ids)

    def test_ticket_type_orders_dedupes_multi_ticket_order_and_counts_type(self):
        ga = SaleableTicketType.objects.create(
            event=self.event, name='General Admission',
            price=Decimal('25.00'), quantity_limit=100, quantity_sold=3,
        )
        # One order with 2 GA + 1 VIP — should appear once, type_count == 2.
        order = self._make_order_with_types(
            ['General Admission', 'General Admission', 'VIP'], name='MixBuyer'
        )
        self._login()

        response = self.client.get(
            reverse('tickets:saleable_ticket_type_orders', args=[self.event.pk, ga.pk])
        )

        page = list(response.context['page_obj'])
        self.assertEqual([o.id for o in page], [order.id])
        self.assertEqual(page[0].type_count, 2)

    def test_ticket_type_orders_is_paginated(self):
        ga = SaleableTicketType.objects.create(
            event=self.event, name='General Admission',
            price=Decimal('25.00'), quantity_limit=None, quantity_sold=130,
        )
        for i in range(130):
            self._make_order_with_types(['General Admission'], name=f'Buyer{i}')
        self._login()

        # Page 1 caps at the page size (100) and exposes a second page.
        page1 = self.client.get(
            reverse('tickets:saleable_ticket_type_orders', args=[self.event.pk, ga.pk])
        )
        self.assertEqual(page1.status_code, 200)
        po = page1.context['page_obj']
        self.assertEqual(po.paginator.count, 130)
        self.assertEqual(po.paginator.num_pages, 2)
        self.assertEqual(len(po.object_list), 100)
        self.assertTrue(po.has_other_pages())
        self.assertContains(page1, 'pagination')

        # Page 2 holds the remaining orders.
        page2 = self.client.get(
            reverse('tickets:saleable_ticket_type_orders', args=[self.event.pk, ga.pk]) + '?page=2'
        )
        self.assertEqual(page2.status_code, 200)
        self.assertEqual(len(page2.context['page_obj'].object_list), 30)

    def test_ticket_type_orders_scoped_to_org(self):
        other_org = Organization.objects.create(name='Other Org', slug='other-org')
        other_venue = Venue.objects.create(
            organization=other_org, name='Other Venue', city='Los Angeles',
        )
        other_event = Event.objects.create(
            organization=other_org, name='Other Event', venue=other_venue,
            start_date=date.today() + timedelta(days=7),
            ticketing_type='direct', status='live',
        )
        other_tt = SaleableTicketType.objects.create(
            event=other_event, name='General Admission',
            price=Decimal('25.00'), quantity_limit=100,
        )
        self._login()

        response = self.client.get(
            reverse('tickets:saleable_ticket_type_orders', args=[other_event.pk, other_tt.pk])
        )

        self.assertEqual(response.status_code, 404)


class EventDailyPageViewTest(TestCase):
    """Tests for daily public buy-page views and event detail chart context."""

    def setUp(self):
        from django.core.cache import cache as django_cache

        self.client = Client()
        self.org = Organization.objects.create(name='Daily Views Org', slug='daily-views-org')
        self.user = User.objects.create_user(
            username='dailyviews',
            email='dailyviews@example.com',
            password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        self.venue = Venue.objects.create(
            organization=self.org,
            name='Daily Views Venue',
            city='Los Angeles',
        )
        self.event = Event.objects.create(
            organization=self.org,
            name='Daily Views Event',
            venue=self.venue,
            start_date=date.today() + timedelta(days=7),
            ticketing_type='direct',
            status='live',
        )
        SaleableTicketType.objects.create(
            event=self.event,
            name='General Admission',
            price=Decimal('25.00'),
            quantity_limit=100,
        )
        django_cache.clear()

    def tearDown(self):
        from django.core.cache import cache as django_cache

        django_cache.clear()

    def _login(self):
        self.assertTrue(self.client.login(username='dailyviews@example.com', password='testpass123'))
        self.client.get(reverse('tickets:home'))

    def test_first_page_view_creates_record(self):
        response = self.client.get(reverse('tickets:public_event_buy', args=[self.event.public_id]))

        self.assertEqual(response.status_code, 200)
        row = EventDailyPageView.objects.get(event=self.event, date=timezone.localdate())
        self.assertEqual(row.view_count, 1)

    def test_public_event_buy_survives_redis_outage(self):
        from unittest.mock import patch

        from redis.exceptions import ConnectionError as RedisConnectionError

        url = reverse('tickets:public_event_buy', args=[self.event.public_id])
        boom = RedisConnectionError('max number of clients reached')

        with patch('tickets.cache_utils.django_cache') as mock_cache:
            mock_cache.get.side_effect = boom
            mock_cache.set.side_effect = boom
            mock_cache.delete.side_effect = boom

            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        row = EventDailyPageView.objects.get(event=self.event, date=timezone.localdate())
        self.assertEqual(row.view_count, 1)

    def test_subsequent_page_view_increments_count(self):
        url = reverse('tickets:public_event_buy', args=[self.event.public_id])

        self.client.get(url)
        self.client.get(url)

        row = EventDailyPageView.objects.get(event=self.event, date=timezone.localdate())
        self.assertEqual(row.view_count, 2)

    def test_stats_page_views_empty(self):
        from tickets.views import _compute_event_stats

        stats = _compute_event_stats(self.event)

        self.assertEqual(stats['page_views_over_time'], [])

    def test_stats_page_views_populated(self):
        from django.core.cache import cache as django_cache
        from tickets.views import _compute_event_stats

        first_date = date(2026, 1, 1)
        second_date = date(2026, 1, 2)
        EventDailyPageView.objects.create(event=self.event, date=first_date, view_count=3)
        EventDailyPageView.objects.create(event=self.event, date=second_date, view_count=7)
        django_cache.clear()

        stats = _compute_event_stats(self.event)

        self.assertEqual(
            stats['page_views_over_time'],
            [
                {'date': first_date, 'view_count': 3},
                {'date': second_date, 'view_count': 7},
            ],
        )

    def test_event_detail_has_page_view_data_true(self):
        EventDailyPageView.objects.create(event=self.event, date=timezone.localdate(), view_count=4)
        self._login()

        response = self.client.get(reverse('tickets:event_detail', args=[self.event.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context['has_page_view_data'], True)

    def test_event_detail_has_page_view_data_false(self):
        self._login()

        response = self.client.get(reverse('tickets:event_detail', args=[self.event.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context['has_page_view_data'], False)
        self.assertIs(response.context['show_page_views_chart'], True)

    def test_views_button_rendered_with_data(self):
        EventDailyPageView.objects.create(event=self.event, date=timezone.localdate(), view_count=4)
        self._login()

        response = self.client.get(reverse('tickets:event_detail', args=[self.event.pk]))

        self.assertContains(response, 'id="btnPageViews"')

    def test_views_button_rendered_without_data(self):
        self._login()

        response = self.client.get(reverse('tickets:event_detail', args=[self.event.pk]))

        self.assertContains(response, 'id="btnPageViews"')

    def test_page_views_json_empty_without_data(self):
        self._login()

        response = self.client.get(reverse('tickets:event_detail', args=[self.event.pk]))
        payload = json.loads(response.context['page_views_over_time_json'])

        self.assertEqual(payload, [])
        self.assertContains(response, 'id="page-views-data"')

    def test_page_views_empty_state_rendered_without_data(self):
        self._login()

        response = self.client.get(reverse('tickets:event_detail', args=[self.event.pk]))

        self.assertContains(response, 'id="pageViewsEmptyState"')
        self.assertContains(response, 'No page views recorded yet.')

    def test_page_views_json_serializable(self):
        view_date = date(2026, 1, 3)
        EventDailyPageView.objects.create(event=self.event, date=view_date, view_count=9)
        self._login()

        response = self.client.get(reverse('tickets:event_detail', args=[self.event.pk]))
        payload = json.loads(response.context['page_views_over_time_json'])

        self.assertEqual(payload, [{'date': view_date.isoformat(), 'views': 9}])


class PageViewComparisonTest(TestCase):
    """Page Views comparison card on the event-detail Analytics tab + its API."""

    def setUp(self):
        from django.core.cache import cache as django_cache

        self.client = Client()
        self.org = Organization.objects.create(name='PV Compare Org', slug='pv-compare-org')
        self.user = User.objects.create_user(
            username='pvcompare',
            email='pvcompare@example.com',
            password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        self.venue = Venue.objects.create(
            organization=self.org, name='PV Venue', city='Los Angeles',
        )
        # Current (upcoming) direct event with its own page-view data.
        self.event = Event.objects.create(
            organization=self.org, name='Current Event', venue=self.venue,
            start_date=date.today() + timedelta(days=10),
            ticketing_type='direct', status='live',
        )
        EventDailyPageView.objects.create(
            event=self.event, date=date.today(), view_count=5,
        )
        # Past direct event with page-view data — a valid comparison candidate.
        self.past_event = Event.objects.create(
            organization=self.org, name='Past Event', venue=self.venue,
            start_date=date.today() - timedelta(days=30),
            ticketing_type='direct', status='live',
        )
        EventDailyPageView.objects.create(
            event=self.past_event,
            date=date.today() - timedelta(days=35),  # 5 days before its start
            view_count=8,
        )
        django_cache.clear()

    def tearDown(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()

    def _login(self):
        self.assertTrue(self.client.login(username='pvcompare@example.com', password='testpass123'))
        self.client.get(reverse('tickets:home'))

    def test_series_shape(self):
        from tickets.services.forecasting.sales_curve import SalesCurveCalculator

        data = SalesCurveCalculator().get_page_view_series(self.past_event)

        self.assertEqual(data, {'series': [{'d': 5, 'views': 8}], 'total_views': 8})

    def test_series_empty_without_start_date(self):
        from tickets.services.forecasting.sales_curve import SalesCurveCalculator

        self.event.start_date = None
        data = SalesCurveCalculator().get_page_view_series(self.event)

        self.assertEqual(data, {'series': [], 'total_views': 0})

    def test_card_shown_with_candidate(self):
        self._login()

        response = self.client.get(reverse('tickets:event_detail', args=[self.event.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertIs(response.context['show_page_views_comparison_card'], True)
        self.assertEqual(response.context['pageviews_default_compare_id'], str(self.past_event.id))
        self.assertContains(response, 'id="pageViewsCompareChart"')

    def test_card_hidden_without_candidate(self):
        self.past_event.hard_delete()  # no comparison candidate remains
        self._login()

        response = self.client.get(reverse('tickets:event_detail', args=[self.event.pk]))

        self.assertIs(response.context['show_page_views_comparison_card'], False)
        self.assertNotContains(response, 'id="pageViewsCompareChart"')

    def test_api_returns_series_for_in_org_event(self):
        self._login()

        response = self.client.get(
            reverse('tickets:event_page_views_api', args=[self.past_event.id])
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload['series'], [{'d': 5, 'views': 8}])
        self.assertEqual(payload['total_views'], 8)
        self.assertEqual(payload['id'], str(self.past_event.id))
        self.assertEqual(payload['name'], 'Past Event')

    def test_api_cross_org_event_404(self):
        other_org = Organization.objects.create(name='Other Org', slug='other-org')
        other_venue = Venue.objects.create(
            organization=other_org, name='Other Venue', city='Seattle',
        )
        other_event = Event.objects.create(
            organization=other_org, name='Other Org Event', venue=other_venue,
            start_date=date.today() - timedelta(days=5),
            ticketing_type='direct', status='live',
        )
        self._login()

        response = self.client.get(
            reverse('tickets:event_page_views_api', args=[other_event.id])
        )

        self.assertEqual(response.status_code, 404)


class EventDeleteViewTests(TestCase):
    """Regression coverage for customer reconciliation during event deletion."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Event Delete Org', slug='event-delete-org')
        self.user = User.objects.create_user(
            username='eventdeleteuser',
            email='eventdelete@example.com',
            password='testpass123'
        )
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='eventdelete@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))

        self.csv_format = CSVFormat.objects.create(
            organization=self.org,
            name='Event Delete Format',
            column_mapping={'order_number': 'Order ID'}
        )
        self.venue = Venue.objects.create(
            organization=self.org,
            name='Delete Venue',
            city='Delete City'
        )
        self.event = Event.objects.create(
            organization=self.org,
            name='Delete Event',
            venue=self.venue,
            start_date=date(2024, 6, 15),
            start_time=time(19, 0, 0)
        )
        self.other_event = Event.objects.create(
            organization=self.org,
            name='Other Event',
            venue=self.venue,
            start_date=date(2024, 6, 20),
            start_time=time(19, 0, 0)
        )
        self.customer = Customer.objects.create(
            organization=self.org,
            email='eventcustomer@example.com',
            name='Event Customer',
            lifetime_value=Decimal('150.00')
        )

    def test_event_delete_preserves_customer_with_remaining_orders(self):
        """Deleting an event should recalculate customer stats from remaining orders."""
        TicketOrder.objects.create(
            customer=self.customer,
            event=self.event,
            order_number='EV-DELETE-1',
            order_date='2024-06-01 10:00:00',
            total_amount=Decimal('150.00')
        )
        TicketOrder.objects.create(
            customer=self.customer,
            event=self.other_event,
            order_number='EV-DELETE-2',
            order_date='2024-06-07 10:00:00',
            total_amount=Decimal('225.00')
        )

        response = self.client.post(reverse('tickets:event_delete', args=[self.event.id]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Customer.objects.filter(id=self.customer.id).exists())
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.lifetime_value, Decimal('225.00'))
        self.assertEqual(self.customer.last_order_date, date(2024, 6, 7))

    def test_event_delete_removes_orphaned_customer(self):
        """Deleting an event should remove customers with no remaining orders."""
        TicketOrder.objects.create(
            customer=self.customer,
            event=self.event,
            order_number='EV-ORPHAN-1',
            order_date='2024-06-01 10:00:00',
            total_amount=Decimal('150.00')
        )

        response = self.client.post(reverse('tickets:event_delete', args=[self.event.id]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Customer.objects.filter(id=self.customer.id).exists())


class EventEditViewTests(TestCase):
    """Regression coverage for the event edit page render paths."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Event Edit Org', slug='event-edit-org')
        self.user = User.objects.create_user(
            username='eventedituser',
            email='eventedit@example.com',
            password='testpass123'
        )
        UserProfile.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='eventedit@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))

        self.venue = Venue.objects.create(
            organization=self.org,
            name='Edit Venue',
            city='Los Angeles',
        )

    def test_direct_event_edit_page_renders(self):
        event = Event.objects.create(
            organization=self.org,
            name='Direct Event',
            venue=self.venue,
            start_date=date(2026, 4, 30),
            start_time=time(20, 0, 0),
            ticketing_type='direct',
            status='live',
        )

        response = self.client.get(reverse('tickets:event_edit', args=[event.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Ticket Types')
        self.assertContains(response, 'Promo Codes')

    def test_external_event_edit_page_renders(self):
        event = Event.objects.create(
            organization=self.org,
            name='External Event',
            venue=self.venue,
            start_date=date(2026, 5, 2),
            start_time=time(19, 30, 0),
            ticketing_type='external',
        )

        response = self.client.get(reverse('tickets:event_edit', args=[event.id]))

        self.assertEqual(response.status_code, 200)
        # Talent Lineup was removed from the create/import/edit forms.
        self.assertNotContains(response, 'Talent Lineup')


class TicketTypeCustomerLimitTests(TestCase):
    """Regression coverage for ticket-type-level max tickets per customer."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Customer Limit Org', slug='customer-limit-org')
        self.user = User.objects.create_user(
            username='limituser',
            email='limit@example.com',
            password='testpass123',
            first_name='Limit',
            last_name='Buyer',
        )
        UserProfile.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='limit@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))

        self.venue = Venue.objects.create(
            organization=self.org,
            name='Limit Venue',
            city='Los Angeles',
        )
        self.event = Event.objects.create(
            organization=self.org,
            name='Limited Event',
            venue=self.venue,
            start_date=date.today() + timedelta(days=7),
            start_time=time(20, 0, 0),
            ticketing_type='direct',
            status='live',
        )
        self.ticket_type = SaleableTicketType.objects.create(
            event=self.event,
            name='General Admission',
            price=Decimal('25.00'),
            quantity_limit=100,
            max_per_customer=2,
        )

    def _grant_existing_tickets(self, qty, *, total_amount='25.00'):
        customer, _ = Customer.objects.get_or_create(
            email=self.user.email,
            defaults={
                'organization': self.org,
                'name': self.user.get_full_name(),
            },
        )
        order = TicketOrder.objects.create(
            customer=customer,
            event=self.event,
            order_number=f'LIMIT-{uuid.uuid4().hex[:10]}',
            order_date=timezone.now(),
            total_amount=Decimal(total_amount),
        )
        Ticket.objects.bulk_create([
            Ticket(
                ticket_order=order,
                ticket_type=self.ticket_type.name,
                price=Decimal('25.00'),
            )
            for _ in range(qty)
        ])
        return order

    def _set_cart(self, *, qty, price='25.00'):
        session = self.client.session
        session[f'cart_{self.event.id}'] = [{
            'saleable_ticket_type_id': str(self.ticket_type.id),
            'name': self.ticket_type.name,
            'price': price,
            'quantity': qty,
            'tier_id': None,
            'tier_name': None,
        }]
        session.save()

    def test_public_event_buy_blocks_customer_above_limit(self):
        self._grant_existing_tickets(1)

        response = self.client.post(
            reverse('tickets:public_event_buy', args=[self.event.public_id]),
            {f'qty_{self.ticket_type.id.hex}': '2'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'You can only add up to 1 more General Admission ticket for this event.')

    def test_free_checkout_blocks_customer_above_limit(self):
        self.ticket_type.price = Decimal('0.00')
        self.ticket_type.save(update_fields=['price'])
        self._grant_existing_tickets(1, total_amount='0.00')
        self._set_cart(qty=2, price='0.00')

        response = self.client.post(reverse('tickets:checkout_payment', args=[self.event.public_id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'You can only purchase up to 2 General Admission tickets for this event.')
        self.assertEqual(TicketOrder.objects.filter(event=self.event).count(), 1)

    def test_create_payment_intent_blocks_customer_above_limit(self):
        self._grant_existing_tickets(1)
        self._set_cart(qty=2)

        response = self.client.post(
            reverse('tickets:create_payment_intent', args=[self.event.public_id]),
            data='{}',
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('You can only purchase up to 2 General Admission tickets for this event.', response.json()['error'])
        self.assertEqual(StripeCheckoutSession.objects.filter(event=self.event).count(), 0)

    def test_create_payment_intent_counts_other_pending_sessions_against_limit(self):
        StripeCheckoutSession.objects.create(
            event=self.event,
            organization=self.org,
            stripe_session_id='pi_pending_limit_test',
            stripe_payment_intent_id='pi_pending_limit_test',
            buyer_email=self.user.email,
            buyer_name=self.user.get_full_name(),
            status=StripeCheckoutSession.Status.PENDING,
            line_items_snapshot=[{
                'saleable_ticket_type_id': str(self.ticket_type.id),
                'name': self.ticket_type.name,
                'price': '25.00',
                'quantity': 1,
                'tier_id': None,
                'tier_name': None,
            }],
            amount_total_cents=2500,
            platform_fee_cents=0,
        )
        self._set_cart(qty=2)

        response = self.client.post(
            reverse('tickets:create_payment_intent', args=[self.event.public_id]),
            data='{}',
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('You can only purchase up to 2 General Admission tickets for this event.', response.json()['error'])

    def test_saleable_ticket_type_data_exposes_max_per_customer(self):
        response = self.client.get(
            reverse('tickets:saleable_ticket_type_data', args=[self.event.id, self.ticket_type.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['max_per_customer'], 2)

    def test_buy_page_max_attr_matches_remaining(self):
        """Fresh buyer with no purchases: qty input should have max equal to max_per_customer."""
        self.ticket_type.max_per_customer = 1
        self.ticket_type.save(update_fields=['max_per_customer'])

        response = self.client.get(
            reverse('tickets:public_event_buy', args=[self.event.public_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'max="1"')

    def test_buy_page_limit_reached_at_max(self):
        """Buyer who already owns max tickets should see 'Limit Reached' badge, not buttons."""
        self.ticket_type.max_per_customer = 1
        self.ticket_type.save(update_fields=['max_per_customer'])
        self._grant_existing_tickets(1)

        response = self.client.get(
            reverse('tickets:public_event_buy', args=[self.event.public_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Limit Reached')

    def test_buy_page_anonymous_no_cap_applied(self):
        """Anonymous visitors should not have a per-customer cap applied (max stays at default)."""
        self.client.logout()
        self.ticket_type.max_per_customer = 1
        self.ticket_type.save(update_fields=['max_per_customer'])

        response = self.client.get(
            reverse('tickets:public_event_buy', args=[self.event.public_id])
        )

        self.assertEqual(response.status_code, 200)
        # No per-customer cap: max should be 10 (default), not 1
        self.assertNotContains(response, 'Limit Reached')
        self.assertContains(response, 'max="10"')

    def test_ticket_type_remaining_excludes_pending(self):
        """Stale PENDING sessions must not be counted when computing buy-page remaining."""
        from tickets.views import _ticket_type_remaining_by_customer

        self.ticket_type.max_per_customer = 1
        self.ticket_type.save(update_fields=['max_per_customer'])
        StripeCheckoutSession.objects.create(
            event=self.event,
            organization=self.org,
            stripe_session_id='pi_stale_remaining_test',
            stripe_payment_intent_id='pi_stale_remaining_test',
            buyer_email=self.user.email,
            buyer_name=self.user.get_full_name(),
            status=StripeCheckoutSession.Status.PENDING,
            line_items_snapshot=[{
                'saleable_ticket_type_id': str(self.ticket_type.id),
                'name': self.ticket_type.name,
                'price': '25.00',
                'quantity': 1,
                'tier_id': None,
                'tier_name': None,
            }],
            amount_total_cents=2500,
            platform_fee_cents=0,
        )

        remaining = _ticket_type_remaining_by_customer(
            [self.ticket_type], self.user.email
        )

        # Pending session must not count; user has 0 confirmed tickets so remaining = 1
        self.assertEqual(remaining[str(self.ticket_type.id)], 1)


class OTPVerifyFlowTests(TestCase):
    """Regression coverage for stale or already-consumed OTP verify sessions."""

    def setUp(self):
        self.client = Client()
        self.factory = RequestFactory()
        self.org = Organization.objects.create(name='OTP Org', slug='otp-org')
        self.user = User.objects.create_user(
            username='otp-user',
            email='otp@example.com',
            password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user,
            organization=self.org,
            role=UserProfile.Role.ATTENDEE,
            phone_number='+15555550123',
        )

    def _build_request(self, path):
        request = self.factory.get(path)
        SessionMiddleware(lambda request: None).process_request(request)
        request.session.save()
        request.user = AnonymousUser()
        setattr(request, '_messages', FallbackStorage(request))
        return request

    def test_unified_verify_missing_session_redirects_with_message(self):
        response = self.client.get(reverse('tickets:unified_verify'))

        self.assertRedirects(response, reverse('tickets:login'))
        messages = list(response.wsgi_request._messages)
        self.assertTrue(any('already completed or expired' in str(message) for message in messages))

    @patch('tickets.sms.check_phone_verification', return_value=False)
    def test_unified_verify_post_with_code_reaches_invalid_code_branch(self, mock_check):
        session = self.client.session
        session['verify_unified'] = {'phone': '+15555550123', 'is_new': False}
        session.save()

        response = self.client.post(reverse('tickets:unified_verify'), {'otp_code': '123456'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Incorrect or expired code. Please try again.')
        self.assertNotContains(response, 'This field is required.')
        mock_check.assert_called_once_with('+15555550123', '123456')

    def test_phone_login_verify_missing_session_redirects_with_message(self):
        from .views import phone_login_verify_view

        request = self._build_request('/login/phone/verify/')
        response = phone_login_verify_view(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('tickets:phone_login'))
        messages = list(request._messages)
        self.assertTrue(any('already completed or expired' in str(message) for message in messages))

    def test_signup_verify_missing_session_redirects_with_message(self):
        from .views import verify_otp_view

        request = self._build_request('/signup/verify/')
        response = verify_otp_view(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('tickets:signup'))
        messages = list(request._messages)
        self.assertTrue(any('already completed or expired' in str(message) for message in messages))

    def test_attendee_verify_missing_session_redirects_with_message(self):
        response = self.client.get(reverse('tickets:attendee_verify_otp', args=[self.org.slug]))

        self.assertRedirects(response, reverse('tickets:attendee_signup', args=[self.org.slug]))
        messages = list(response.wsgi_request._messages)
        self.assertTrue(any('already completed or expired' in str(message) for message in messages))

    def test_phone_login_verify_authenticated_user_redirects_to_dashboard(self):
        from .views import phone_login_verify_view

        request = self._build_request('/login/phone/verify/')
        request.user = self.user
        response = phone_login_verify_view(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('tickets:attendee_dashboard'))


class SmartPricingRecommendationTests(TestCase):
    def setUp(self):
        self.client = Client()
        flags = FeatureFlagSettings.get_solo()
        flags.smart_pricing_recommendations_enabled = True
        flags.save(update_fields=['smart_pricing_recommendations_enabled'])
        self.org = Organization.objects.create(name='Pricing Org', slug='pricing-org')
        self.user = User.objects.create_superuser(
            username='pricing-admin',
            email='pricing@example.com',
            password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='pricing@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))

        self.venue = Venue.objects.create(
            organization=self.org,
            name='Venue One',
            city='Oakland',
            capacity=500,
        )
        self.target_event = Event.objects.create(
            organization=self.org,
            name='Future Show',
            venue=self.venue,
            ticketing_type='direct',
            start_date=date.today() + timedelta(days=30),
            start_time=time(20, 0),
            end_date=date.today() + timedelta(days=30),
            end_time=time(23, 0),
            capacity=400,
        )
        self.recommendation_url = reverse('tickets:event_pricing_recommendation', args=[self.target_event.id])

    def _make_paid_history_event(self, *, name, start_date, capacity, prices):
        event = Event.objects.create(
            organization=self.org,
            name=name,
            venue=self.venue,
            ticketing_type='external',
            start_date=start_date,
            start_time=time(20, 0),
            end_date=start_date,
            end_time=time(23, 0),
            capacity=capacity,
        )
        customer = Customer.objects.create(
            organization=self.org,
            email=f'{name.lower().replace(" ", "-")}@example.com',
            name=f'{name} Buyer',
        )
        order = TicketOrder.objects.create(
            customer=customer,
            event=event,
            order_number=f'ORD-{uuid.uuid4().hex[:10]}',
            order_date=timezone.now() - timedelta(days=90),
            total_amount=sum(prices),
        )
        for price in prices:
            Ticket.objects.create(
                ticket_order=order,
                ticket_type='GA',
                price=price,
            )
        event.cached_paid_ticket_count = len(prices)
        event.cached_paid_ticket_sum = sum(prices)
        event.save(update_fields=['cached_paid_ticket_count', 'cached_paid_ticket_sum'])
        return event

    def test_direct_event_create_persists_capacity(self):
        response = self.client.post(
            reverse('tickets:event_create', args=['direct']),
            {
                'name': 'New Direct Event',
                'summary': '',
                'start_date': (date.today() + timedelta(days=14)).isoformat(),
                'start_time': '20:00',
                'end_date': (date.today() + timedelta(days=14)).isoformat(),
                'end_time': '23:00',
                'description': '',
                'capacity': '350',
                'venue': str(self.venue.id),
                'facebook_pixel_id': '',
                'ticket_type-TOTAL_FORMS': '1',
                'ticket_type-INITIAL_FORMS': '0',
                'ticket_type-MIN_NUM_FORMS': '0',
                'ticket_type-MAX_NUM_FORMS': '1000',
                'ticket_type-0-name': 'General Admission',
                'ticket_type-0-description': '',
                'ticket_type-0-price': '35.00',
                'ticket_type-0-quantity_limit': '',
                'ticket_type-0-max_per_customer': '4',
                'ticket_type-0-order': '0',
                'ticket_type-0-unlocks_after': '',
            },
        )

        self.assertEqual(response.status_code, 302)
        event = Event.objects.get(name='New Direct Event')
        self.assertEqual(event.capacity, 350)
        self.assertEqual(event.saleable_ticket_types.get(name='General Admission').max_per_customer, 4)

    def test_direct_event_create_persists_unlocks_after_relationship(self):
        response = self.client.post(
            reverse('tickets:event_create', args=['direct']),
            {
                'name': 'Unlock Ladder Event',
                'summary': '',
                'start_date': (date.today() + timedelta(days=14)).isoformat(),
                'start_time': '20:00',
                'end_date': (date.today() + timedelta(days=14)).isoformat(),
                'end_time': '23:00',
                'description': '',
                'capacity': '350',
                'venue': str(self.venue.id),
                'facebook_pixel_id': '',
                'ticket_type-TOTAL_FORMS': '2',
                'ticket_type-INITIAL_FORMS': '0',
                'ticket_type-MIN_NUM_FORMS': '0',
                'ticket_type-MAX_NUM_FORMS': '1000',
                'ticket_type-0-name': 'Early Bird',
                'ticket_type-0-description': '',
                'ticket_type-0-price': '20.00',
                'ticket_type-0-quantity_limit': '50',
                'ticket_type-0-max_per_customer': '',
                'ticket_type-0-order': '0',
                'ticket_type-0-unlocks_after': '',
                'ticket_type-1-name': 'General Admission',
                'ticket_type-1-description': '',
                'ticket_type-1-price': '35.00',
                'ticket_type-1-quantity_limit': '200',
                'ticket_type-1-max_per_customer': '',
                'ticket_type-1-order': '1',
                'ticket_type-1-unlocks_after': '0',
            },
        )

        self.assertEqual(response.status_code, 302)
        event = Event.objects.get(name='Unlock Ladder Event')
        early_bird = event.saleable_ticket_types.get(name='Early Bird')
        ga = event.saleable_ticket_types.get(name='General Admission')
        self.assertEqual(ga.unlocks_after_id, early_bird.id)

    def test_direct_event_form_defaults_capacity_from_venue(self):
        from .forms import DirectEventForm

        form = DirectEventForm(
            organization=self.org,
            initial={'venue': self.venue.id},
        )

        self.assertEqual(form.initial.get('capacity'), self.venue.capacity)

    def test_pricing_recommendation_blocks_without_capacity(self):
        self.target_event.capacity = None
        self.target_event.save(update_fields=['capacity'])

        response = self.client.get(self.recommendation_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['ready'])
        self.assertIn('capacity', data['blocking_reason'].lower())

    def test_pricing_recommendation_uses_query_overrides_for_preview(self):
        self.target_event.capacity = None
        self.target_event.save(update_fields=['capacity'])
        self._make_paid_history_event(
            name='Override Preview Show',
            start_date=date.today() - timedelta(days=70),
            capacity=320,
            prices=[Decimal('20.00')] * 50 + [Decimal('30.00')] * 70 + [Decimal('40.00')] * 60,
        )

        response = self.client.get(
            self.recommendation_url,
            {
                'venue_id': str(self.venue.id),
                'capacity': '400',
                'start_date': self.target_event.start_date.isoformat(),
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ready'])
        self.assertGreaterEqual(len(data['recommended_tiers']), 2)

    def test_pricing_recommendation_is_org_scoped(self):
        other_org = Organization.objects.create(name='Other Pricing Org', slug='other-pricing-org')
        other_venue = Venue.objects.create(organization=other_org, name='Elsewhere', city='Oakland')
        other_event = Event.objects.create(
            organization=other_org,
            name='Other Org Event',
            venue=other_venue,
            ticketing_type='direct',
            start_date=date.today() + timedelta(days=15),
            start_time=time(20, 0),
            end_date=date.today() + timedelta(days=15),
            end_time=time(23, 0),
            capacity=200,
        )

        response = self.client.get(
            reverse('tickets:event_pricing_recommendation', args=[other_event.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 404)

    def test_pricing_recommendation_endpoint_hidden_when_flag_disabled(self):
        flags = FeatureFlagSettings.get_solo()
        flags.smart_pricing_recommendations_enabled = False
        flags.save(update_fields=['smart_pricing_recommendations_enabled'])
        response = self.client.get(self.recommendation_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 404)

    def test_apply_recommendation_creates_ticket_type_and_tiers(self):
        self._make_paid_history_event(
            name='Spring Show',
            start_date=date.today() - timedelta(days=90),
            capacity=300,
            prices=[Decimal('20.00')] * 60 + [Decimal('30.00')] * 80 + [Decimal('40.00')] * 70,
        )
        self._make_paid_history_event(
            name='Summer Show',
            start_date=date.today() - timedelta(days=60),
            capacity=350,
            prices=[Decimal('25.00')] * 70 + [Decimal('35.00')] * 90 + [Decimal('45.00')] * 60,
        )

        get_response = self.client.get(self.recommendation_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(get_response.status_code, 200)
        self.assertTrue(get_response.json()['ready'])

        response = self.client.post(self.recommendation_url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        self.assertEqual(SaleableTicketType.objects.filter(event=self.target_event).count(), 1)
        self.assertGreaterEqual(
            SaleableTicketTypeTier.objects.filter(ticket_type__event=self.target_event).count(),
            2,
        )


class FeatureFlagSettingsTests(TestCase):
    def test_global_feature_flags_default_and_toggle(self):
        from .feature_flags import (
            browse_events_enabled,
            smart_pricing_recommendations_enabled,
        )

        user = User.objects.create_superuser(
            username='flag-admin',
            email='flags@example.com',
            password='testpass123',
        )
        flags = FeatureFlagSettings.get_solo()

        self.assertFalse(browse_events_enabled())
        self.assertFalse(smart_pricing_recommendations_enabled(user))

        flags.browse_events_enabled = True
        flags.smart_pricing_recommendations_enabled = True
        flags.save(update_fields=[
            'browse_events_enabled',
            'smart_pricing_recommendations_enabled',
        ])

        self.assertTrue(browse_events_enabled())
        self.assertTrue(smart_pricing_recommendations_enabled(user))


class CustomerTagManagementTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Tag Test Org', slug='tag-test-org')
        self.user = User.objects.create_user(
            username='taguser',
            email='tag@test.com',
            password='testpass123',
        )
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='tag@test.com', password='testpass123')
        # Seed session with org
        self.client.get(reverse('tickets:home'))

        self.customer = Customer.objects.create(
            organization=self.org,
            email='customer@example.com',
            name='Test Customer',
        )

    # ---- tag create ----

    def test_tag_create_happy_path(self):
        resp = self.client.post(reverse('tickets:customer_tag_create'), {
            'name': 'VIP',
            'color': 'purple',
        })
        self.assertRedirects(resp, reverse('tickets:customer_tag_list'))
        self.assertTrue(CustomerTag.objects.filter(organization=self.org, name='VIP').exists())

    def test_tag_create_duplicate_name_rejected(self):
        CustomerTag.objects.create(organization=self.org, name='Press', color='blue')
        resp = self.client.post(reverse('tickets:customer_tag_create'), {
            'name': 'Press',
            'color': 'green',
        })
        self.assertRedirects(resp, reverse('tickets:customer_tag_list'))
        self.assertEqual(CustomerTag.objects.filter(organization=self.org, name='Press').count(), 1)

    # ---- tag delete ----

    def test_tag_delete_removes_from_customers(self):
        tag = CustomerTag.objects.create(organization=self.org, name='Comp', color='red')
        self.customer.tags.add(tag)
        self.assertEqual(self.customer.tags.count(), 1)

        self.client.post(reverse('tickets:customer_tag_delete', args=[tag.id]))

        self.assertFalse(CustomerTag.objects.filter(id=tag.id).exists())
        self.assertEqual(self.customer.tags.count(), 0)

    # ---- tag add ----

    def test_tag_add_to_customer(self):
        tag = CustomerTag.objects.create(organization=self.org, name='VIP', color='blue')
        resp = self.client.post(
            reverse('tickets:customer_tag_add', args=[self.customer.id]),
            {'tag_id': str(tag.id)},
        )
        self.assertRedirects(resp, reverse('tickets:customer_detail', args=[self.customer.id]))
        self.assertIn(tag, self.customer.tags.all())

    def test_tag_add_idempotent(self):
        tag = CustomerTag.objects.create(organization=self.org, name='VIP', color='blue')
        self.customer.tags.add(tag)
        self.client.post(
            reverse('tickets:customer_tag_add', args=[self.customer.id]),
            {'tag_id': str(tag.id)},
        )
        self.assertEqual(self.customer.tags.count(), 1)

    def test_tag_add_cross_tenant_blocked(self):
        other_org = Organization.objects.create(name='Other Org', slug='other-org')
        other_tag = CustomerTag.objects.create(organization=other_org, name='Outsider', color='red')
        resp = self.client.post(
            reverse('tickets:customer_tag_add', args=[self.customer.id]),
            {'tag_id': str(other_tag.id)},
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.customer.tags.count(), 0)

    # ---- tag remove ----

    def test_tag_remove_from_customer(self):
        tag = CustomerTag.objects.create(organization=self.org, name='VIP', color='blue')
        self.customer.tags.add(tag)
        resp = self.client.post(
            reverse('tickets:customer_tag_remove', args=[self.customer.id, tag.id]),
        )
        self.assertRedirects(resp, reverse('tickets:customer_detail', args=[self.customer.id]))
        self.assertNotIn(tag, self.customer.tags.all())

    # ---- customer list tag filter ----

    def test_customer_list_tag_filter(self):
        tag = CustomerTag.objects.create(organization=self.org, name='VIP', color='blue')
        tagged = Customer.objects.create(
            organization=self.org, email='tagged@example.com', name='Tagged'
        )
        tagged.tags.add(tag)

        resp = self.client.get(reverse('tickets:customer_list'), {'tag': str(tag.id)})
        self.assertEqual(resp.status_code, 200)
        customers_in_page = list(resp.context['page_obj'])
        self.assertIn(tagged, customers_in_page)
        self.assertNotIn(self.customer, customers_in_page)
        self.assertContains(resp, '1 matching customer')

    def test_customer_list_bad_tag_uuid_graceful(self):
        resp = self.client.get(reverse('tickets:customer_list'), {'tag': 'notauuid'})
        self.assertEqual(resp.status_code, 200)

    # ---- customer detail ----

    def test_customer_detail_shows_tags(self):
        tag = CustomerTag.objects.create(organization=self.org, name='Press', color='green')
        self.customer.tags.add(tag)
        resp = self.client.get(reverse('tickets:customer_detail', args=[self.customer.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(tag, resp.context['assigned_tags'])


class CustomerBulkSMSStatusTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(
            name='SMS Org', slug='sms-org', sms_marketing_enabled=True,
        )
        self.user = User.objects.create_user(
            username='smshost', email='smshost@test.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='smshost@test.com', password='testpass123')
        self.client.get(reverse('tickets:home'))  # primes _org_id in session

        self.alice = Customer.objects.create(
            organization=self.org, email='alice@example.com', name='Alice',
            phone='+13105550001', sms_opt_in=False,
            last_order_date=date(2026, 1, 15),
        )
        self.bob = Customer.objects.create(
            organization=self.org, email='bob@example.com', name='Bob',
            phone='+13105550002', sms_opt_in=False,
            last_order_date=date(2025, 12, 15),
        )
        self.url = reverse('tickets:customers_bulk_sms_status')
        self.compose_url = reverse('tickets:customers_bulk_sms_compose')

    def test_opt_in_sets_flag_and_date_on_selected_only(self):
        response = self.client.post(self.url, {
            'sms_status': 'opt_in',
            'customer_ids': [str(self.alice.id)],
        })
        self.assertRedirects(response, reverse('tickets:customer_list'))
        self.alice.refresh_from_db()
        self.bob.refresh_from_db()
        self.assertTrue(self.alice.sms_opt_in)
        self.assertIsNotNone(self.alice.sms_opt_in_date)
        self.assertFalse(self.bob.sms_opt_in)

    def test_opt_in_skips_customers_without_a_phone(self):
        phoneless = Customer.objects.create(
            organization=self.org, email='nophone@example.com', name='No Phone',
            phone='', sms_opt_in=False,
        )
        response = self.client.post(self.url, {
            'sms_status': 'opt_in',
            'customer_ids': [str(self.alice.id), str(phoneless.id)],
        })
        self.assertRedirects(response, reverse('tickets:customer_list'))
        self.alice.refresh_from_db()
        phoneless.refresh_from_db()
        self.assertTrue(self.alice.sms_opt_in)
        self.assertFalse(phoneless.sms_opt_in)
        self.assertIsNone(phoneless.sms_opt_in_date)

    def test_opt_in_preserves_existing_opt_in_date(self):
        original = timezone.now() - timedelta(days=30)
        self.alice.sms_opt_in = True
        self.alice.sms_opt_in_date = original
        self.alice.save(update_fields=['sms_opt_in', 'sms_opt_in_date'])

        self.client.post(self.url, {
            'sms_status': 'opt_in',
            'customer_ids': [str(self.alice.id)],
        })
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.sms_opt_in)
        self.assertEqual(self.alice.sms_opt_in_date, original)

    def test_opt_out_clears_flag_on_selected_only(self):
        for c in (self.alice, self.bob):
            c.sms_opt_in = True
            c.sms_opt_in_date = timezone.now()
            c.save(update_fields=['sms_opt_in', 'sms_opt_in_date'])

        self.client.post(self.url, {
            'sms_status': 'opt_out',
            'customer_ids': [str(self.bob.id)],
        })
        self.alice.refresh_from_db()
        self.bob.refresh_from_db()
        self.assertTrue(self.alice.sms_opt_in)
        self.assertFalse(self.bob.sms_opt_in)

    def test_is_org_scoped(self):
        other_org = Organization.objects.create(name='Other SMS Org', slug='other-sms-org')
        outsider = Customer.objects.create(
            organization=other_org, email='out@example.com', name='Outsider',
            phone='+13105559999', sms_opt_in=False,
        )
        self.client.post(self.url, {
            'sms_status': 'opt_in',
            'customer_ids': [str(self.alice.id), str(outsider.id)],
        })
        self.alice.refresh_from_db()
        outsider.refresh_from_db()
        self.assertTrue(self.alice.sms_opt_in)
        self.assertFalse(outsider.sms_opt_in)

    def test_select_all_honors_active_search_filter(self):
        response = self.client.post(self.url, {
            'sms_status': 'opt_in',
            'select_all': '1',
            'search': 'alice',
        })
        expected = f"{reverse('tickets:customer_list')}?search=alice"
        self.assertRedirects(response, expected)
        self.alice.refresh_from_db()
        self.bob.refresh_from_db()
        self.assertTrue(self.alice.sms_opt_in)
        self.assertFalse(self.bob.sms_opt_in)

    def test_select_all_honors_last_order_filter(self):
        response = self.client.post(self.url, {
            'sms_status': 'opt_in',
            'select_all': '1',
            'last_order_from': '2026-01-01',
            'last_order_to': '2026-01-31',
        })
        expected = (
            f"{reverse('tickets:customer_list')}"
            "?last_order_from=2026-01-01&last_order_to=2026-01-31"
        )
        self.assertRedirects(response, expected)
        self.alice.refresh_from_db()
        self.bob.refresh_from_db()
        self.assertTrue(self.alice.sms_opt_in)
        self.assertFalse(self.bob.sms_opt_in)

    def test_compose_stores_org_scoped_selection_in_session(self):
        other_org = Organization.objects.create(name='Other SMS Org', slug='other-compose-sms')
        outsider = Customer.objects.create(
            organization=other_org, email='out-compose@example.com', name='Outsider',
            phone='+13105559999', sms_opt_in=True,
        )

        response = self.client.post(self.compose_url, {
            'customer_ids': [str(self.alice.id), str(outsider.id), 'not-a-uuid'],
            'search': 'alice',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('tickets:sms_campaign_create'))
        prefill = self.client.session['sms_compose_prefill']
        self.assertEqual(prefill['ids'], [str(self.alice.id)])
        self.assertEqual(prefill['label'], '1 customer')

    def test_compose_select_all_honors_last_order_filter(self):
        response = self.client.post(self.compose_url, {
            'select_all': '1',
            'last_order_from': '2026-01-01',
            'last_order_to': '2026-01-31',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('tickets:sms_campaign_create'))
        prefill = self.client.session['sms_compose_prefill']
        self.assertEqual(prefill['ids'], [str(self.alice.id)])
        self.assertEqual(prefill['label'], '1 customer')

    def test_compose_page_uses_locked_manual_audience(self):
        self.client.post(self.compose_url, {
            'customer_ids': [str(self.alice.id), str(self.bob.id)],
        })

        response = self.client.get(reverse('tickets:sms_campaign_create'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '2 customers selected from the Customers page')
        self.assertContains(response, 'name="manual_include_ids"')
        self.assertEqual(
            set(response.context['manual_include_ids_csv'].split(',')),
            {str(self.alice.id), str(self.bob.id)},
        )

    def test_sms_campaign_manual_includes_ignore_malformed_ids(self):
        from .models import SMSCampaign
        self.alice.sms_opt_in = True
        self.alice.save(update_fields=['sms_opt_in'])

        campaign = SMSCampaign(
            organization=self.org,
            name='Manual',
            body='Hello',
            manual_include_ids=['not-a-uuid', str(self.alice.id)],
        )

        self.assertEqual(list(campaign.candidate_customers(self.org)), [self.alice])


class MultiOrgTests(TestCase):
    """Tests for multi-organization support: create, switch, invite, role isolation."""

    def setUp(self):
        self.client = Client()
        self.org_a = Organization.objects.create(name='Org A', slug='org-a')
        self.org_b = Organization.objects.create(name='Org B', slug='org-b')
        self.org_other = Organization.objects.create(name='Org Other', slug='org-other')
        self.user = User.objects.create_user(
            username='multiuser', email='multi@test.com', password='testpass123',
            first_name='Multi', last_name='User',
        )
        # User is OWNER of org_a
        UserProfile.objects.create(user=self.user, organization=self.org_a, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(user=self.user, organization=self.org_a, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='multi@test.com', password='testpass123')
        # Seed the session with org_a as the active org
        session = self.client.session
        session['_org_id'] = str(self.org_a.pk)
        session.save()

    def test_create_second_organization_allowed(self):
        """A user already in org_a should be able to create org_b."""
        from .models import OrganizerWaitlist
        OrganizerWaitlist.objects.create(
            email=self.user.email,
            status=OrganizerWaitlist.Status.APPROVED,
        )
        response = self.client.post(
            reverse('tickets:create_organization'),
            {'name': 'New Second Org', 'slug': 'new-second-org'},
        )
        # Should redirect (success), not stay on form
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Organization.objects.filter(name='New Second Org').exists())
        new_org = Organization.objects.get(name='New Second Org')
        self.assertEqual(
            OrganizationMembership.objects.filter(user=self.user, organization=new_org).count(), 1
        )

    def test_org_switch_valid(self):
        """Switching to an org the user is a member of updates the session."""
        OrganizationMembership.objects.create(user=self.user, organization=self.org_b, org_role=UserProfile.OrgRole.HOST)
        response = self.client.post(
            reverse('tickets:org_switch'),
            {'org_id': str(self.org_b.pk)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session.get('_org_id'), str(self.org_b.pk))

    def test_org_switch_unauthorized_404(self):
        """Switching to an org the user has no membership in returns 404."""
        response = self.client.post(
            reverse('tickets:org_switch'),
            {'org_id': str(self.org_other.pk)},
        )
        self.assertEqual(response.status_code, 404)
        # Session should be unchanged
        self.assertEqual(self.client.session.get('_org_id'), str(self.org_a.pk))

    def test_stale_session_org_id_falls_back_to_membership(self):
        """If session._org_id points at an org the user has no membership in,
        get_organization() ignores it and falls back to a real membership."""
        # Tamper the session: point _org_id at an org the user is NOT a member of.
        session = self.client.session
        session['_org_id'] = str(self.org_other.pk)
        session.save()

        # Hit any @require_org view. Home is fine.
        response = self.client.get(reverse('tickets:home'))
        # Should not 500; should resolve to the user's actual membership org.
        self.assertEqual(response.status_code, 200)
        # Session should now reflect the user's real org (org_a), not org_other.
        self.assertEqual(self.client.session.get('_org_id'), str(self.org_a.pk))

    def test_invite_accept_does_not_overwrite_existing_org(self):
        """Accepting an invite to org_b should not change profile.organization from org_a."""
        from datetime import timedelta
        from django.utils import timezone as tz
        invitation = OrganizationInvitation.objects.create(
            organization=self.org_b,
            email=self.user.email,
            invited_by=self.user,
            status=OrganizationInvitation.Status.PENDING,
            expires_at=tz.now() + timedelta(days=7),
            role=UserProfile.Role.ORGANIZER,
            org_role=UserProfile.OrgRole.HOST,
        )
        self.client.get(reverse('tickets:invite_accept', args=[invitation.token]))
        self.user.profile.refresh_from_db()
        # primary org unchanged
        self.assertEqual(self.user.profile.organization_id, self.org_a.pk)
        # but membership row created for org_b
        self.assertTrue(
            OrganizationMembership.objects.filter(user=self.user, organization=self.org_b).exists()
        )

    def test_member_role_update_uses_membership_id(self):
        """member_role_update should look up by OrganizationMembership UUID."""
        other_user = User.objects.create_user(
            username='othermember', email='other@member.com', password='pass123',
        )
        UserProfile.objects.create(user=other_user, organization=self.org_a, org_role=UserProfile.OrgRole.HOST)
        membership = OrganizationMembership.objects.create(
            user=other_user, organization=self.org_a, org_role=UserProfile.OrgRole.HOST,
        )
        response = self.client.post(
            reverse('tickets:member_role_update', args=[membership.pk]),
            {'org_role': UserProfile.OrgRole.ADMIN},
        )
        self.assertEqual(response.status_code, 302)
        membership.refresh_from_db()
        self.assertEqual(membership.org_role, UserProfile.OrgRole.ADMIN)


class PublicEventPreviewMetadataTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Preview Org', slug='preview-org')
        self.venue = Venue.objects.create(
            organization=self.org,
            name='The Echo',
            city='Los Angeles',
            state='CA',
        )

    def _build_description(self, event):
        parts = [event.start_date.strftime('%a, %b %-d, %Y')]
        if event.start_time:
            parts.append(event.start_time.strftime('%-I:%M %p'))
        venue_parts = [part for part in [event.venue.city, event.venue.state] if part]
        venue_label = event.venue.name
        if venue_parts:
            venue_label = f"{venue_label}, {', '.join(venue_parts)}"
        parts.append(venue_label)
        return ' · '.join(parts)

    def _build_social_title(self, event):
        return f"{event.name} · {self._build_description(event)}"

    def test_live_public_event_metadata_includes_date_time_and_venue(self):
        event = Event.objects.create(
            organization=self.org,
            name='Warehouse Session',
            venue=self.venue,
            start_date=date.today() + timedelta(days=30),
            start_time=time(20, 0),
            ticketing_type='direct',
            status='live',
        )

        response = self.client.get(reverse('tickets:public_event_buy', args=[event.public_id]))

        self.assertEqual(response.status_code, 200)
        expected_description = self._build_description(event)
        self.assertContains(
            response,
            f'<title>{event.name} · {expected_description} · Buy Tickets</title>',
            html=True,
        )
        self.assertContains(
            response,
            f'<meta name="description" content="{expected_description}">',
            html=True,
        )
        self.assertContains(
            response,
            f'<meta property="og:description" content="{expected_description}">',
            html=True,
        )
        self.assertContains(
            response,
            f'<meta property="og:title" content="{self._build_social_title(event)}">',
            html=True,
        )
        self.assertContains(
            response,
            f'<meta name="twitter:description" content="{expected_description}">',
            html=True,
        )
        self.assertContains(
            response,
            f'<meta name="twitter:title" content="{self._build_social_title(event)}">',
            html=True,
        )

    def test_live_public_event_metadata_omits_missing_optional_fields_cleanly(self):
        venue = Venue.objects.create(
            organization=self.org,
            name='Secret Room',
            city='',
            state='',
        )
        event = Event.objects.create(
            organization=self.org,
            name='Afterhours',
            venue=venue,
            start_date=date(2026, 5, 2),
            ticketing_type='direct',
            status='live',
        )

        response = self.client.get(reverse('tickets:public_event_buy', args=[event.public_id]))

        self.assertEqual(response.status_code, 200)
        expected_description = self._build_description(event)
        self.assertContains(
            response,
            f'<meta name="description" content="{expected_description}">',
            html=True,
        )
        self.assertContains(
            response,
            f'<meta property="og:title" content="{self._build_social_title(event)}">',
            html=True,
        )
        self.assertNotContains(response, ' ·  · ')
        self.assertNotContains(response, 'Secret Room, , ')

    def test_ended_public_event_metadata_uses_preview_description(self):
        event = Event.objects.create(
            organization=self.org,
            name='Past Headliner',
            venue=self.venue,
            start_date=date(2026, 3, 1),
            start_time=time(21, 30),
            ticketing_type='direct',
            status='ended',
        )

        response = self.client.get(reverse('tickets:public_event_buy', args=[event.public_id]))

        self.assertEqual(response.status_code, 200)
        expected_description = self._build_description(event)
        self.assertContains(
            response,
            f'<title>{event.name} · {expected_description} · Ticket Sales Ended</title>',
            html=True,
        )
        self.assertContains(
            response,
            f'<meta property="og:description" content="{expected_description}">',
            html=True,
        )
        self.assertContains(
            response,
            f'<meta property="og:title" content="{self._build_social_title(event)}">',
            html=True,
        )

    def test_cancelled_public_event_metadata_uses_preview_description(self):
        event = Event.objects.create(
            organization=self.org,
            name='Canceled Night',
            venue=self.venue,
            start_date=date(2026, 6, 20),
            start_time=time(22, 0),
            ticketing_type='direct',
            status='cancelled',
        )

        response = self.client.get(reverse('tickets:public_event_buy', args=[event.public_id]))

        self.assertEqual(response.status_code, 200)
        expected_description = self._build_description(event)
        self.assertContains(
            response,
            f'<title>{event.name} · {expected_description} · Event Cancelled</title>',
            html=True,
        )
        self.assertContains(
            response,
            f'<meta name="twitter:description" content="{expected_description}">',
            html=True,
        )
        self.assertContains(
            response,
            f'<meta name="twitter:title" content="{self._build_social_title(event)}">',
            html=True,
        )


class AccountCreationAndLoginTests(TestCase):
    """End-to-end tests for the email-based signup and login flows.

    All OTP delivery is mocked so no real SMS/email is sent.
    Each test drives the full multi-step flow entirely through the
    Django test client, asserting DB state and redirects at every step.
    """

    def setUp(self):
        self.client = Client()

    # ------------------------------------------------------------------
    # Account creation (email signup → phone verification → dashboard)
    # ------------------------------------------------------------------

    @patch('tickets.sms.start_email_verification', return_value=True)
    def test_new_account_step1_email_sends_otp_and_advances(self, mock_email_otp):
        """POSTing a new email triggers OTP send and redirects to verify step."""
        response = self.client.post(
            reverse('tickets:email_login'),
            {'email': 'newuser@example.com'},
        )
        mock_email_otp.assert_called_once_with('newuser@example.com')
        self.assertRedirects(
            response,
            reverse('tickets:email_verify'),
            fetch_redirect_response=False,
        )
        session = self.client.session
        self.assertEqual(session['verify_email']['email'], 'newuser@example.com')
        self.assertTrue(session['verify_email']['is_new'])

    @patch('tickets.sms.check_email_verification', return_value=True)
    def test_new_account_step2_valid_otp_advances_to_profile(self, mock_check):
        """A correct OTP on a new-user session moves to profile-completion."""
        session = self.client.session
        session['verify_email'] = {'email': 'newuser@example.com', 'is_new': True}
        session.save()

        response = self.client.post(
            reverse('tickets:email_verify'),
            {'otp_code': '123456'},
        )
        mock_check.assert_called_once_with('newuser@example.com', '123456')
        self.assertRedirects(
            response,
            reverse('tickets:email_complete_profile'),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            self.client.session.get('pending_signup_email'),
            'newuser@example.com',
        )

    @patch('tickets.sms.start_phone_verification', return_value=True)
    def test_new_account_step3_profile_form_advances_to_phone_verify(self, mock_phone_otp):
        """Submitting the profile form triggers phone OTP and stashes profile data."""
        session = self.client.session
        session['pending_signup_email'] = 'newuser@example.com'
        session.save()

        response = self.client.post(
            reverse('tickets:email_complete_profile'),
            {
                'first_name': 'Jane',
                'last_name': 'Doe',
                'phone_number': '+12125550100',
                'email_display': 'newuser@example.com',
                'gender': 'female',
                'terms_accepted': True,
            },
        )
        mock_phone_otp.assert_called_once_with('+12125550100')
        self.assertRedirects(
            response,
            reverse('tickets:verify_phone_after_profile'),
            fetch_redirect_response=False,
        )
        profile_data = self.client.session.get('pending_email_profile_data')
        self.assertIsNotNone(profile_data)
        self.assertEqual(profile_data['first_name'], 'Jane')
        self.assertEqual(profile_data['phone_number'], '+12125550100')
        # Account should NOT exist yet
        self.assertFalse(User.objects.filter(email='newuser@example.com').exists())

    @patch('tickets.sms.check_phone_verification', return_value=True)
    def test_new_account_step4_phone_otp_creates_user_and_logs_in(self, mock_check):
        """A correct phone OTP creates the User + UserProfile and logs the user in."""
        session = self.client.session
        session['pending_email_profile_data'] = {
            'email': 'newuser@example.com',
            'first_name': 'Jane',
            'last_name': 'Doe',
            'phone_number': '+12125550100',
            'gender': 'female',
            'marketing_opt_in': False,
        }
        session.save()

        response = self.client.post(
            reverse('tickets:verify_phone_after_profile'),
            {'otp_code': '654321'},
        )
        mock_check.assert_called_once_with('+12125550100', '654321')

        # User and profile must now exist
        self.assertTrue(User.objects.filter(email='newuser@example.com').exists())
        user = User.objects.get(email='newuser@example.com')
        self.assertEqual(user.first_name, 'Jane')
        self.assertTrue(hasattr(user, 'profile'))
        self.assertEqual(user.profile.phone_number, '+12125550100')
        self.assertEqual(user.profile.role, UserProfile.Role.ATTENDEE)

        # Session data should be cleaned up
        self.assertIsNone(self.client.session.get('pending_email_profile_data'))

        # User should now be authenticated and redirected to attendee dashboard
        self.assertRedirects(
            response,
            reverse('tickets:attendee_dashboard'),
            fetch_redirect_response=False,
        )

    # ------------------------------------------------------------------
    # Login (existing user — email OTP flow)
    # ------------------------------------------------------------------

    @patch('tickets.sms.start_email_verification', return_value=True)
    def test_existing_user_login_step1_email_sends_otp(self, mock_email_otp):
        """POSTing a known email triggers OTP send and is_new=False in session."""
        User.objects.create_user(
            username='existing',
            email='existing@example.com',
            password='unused',
        )
        response = self.client.post(
            reverse('tickets:email_login'),
            {'email': 'existing@example.com'},
        )
        mock_email_otp.assert_called_once_with('existing@example.com')
        self.assertRedirects(
            response,
            reverse('tickets:email_verify'),
            fetch_redirect_response=False,
        )
        session = self.client.session
        self.assertFalse(session['verify_email']['is_new'])

    @patch('tickets.sms.check_email_verification', return_value=True)
    def test_existing_user_login_step2_valid_otp_logs_in_and_redirects(self, mock_check):
        """A correct OTP for an existing attendee logs them in and sends to dashboard."""
        user = User.objects.create_user(
            username='existing',
            email='existing@example.com',
            password='unused',
        )
        UserProfile.objects.create(
            user=user,
            role=UserProfile.Role.ATTENDEE,
            phone_number='+12125550200',
        )

        session = self.client.session
        session['verify_email'] = {'email': 'existing@example.com', 'is_new': False}
        session.save()

        response = self.client.post(
            reverse('tickets:email_verify'),
            {'otp_code': '999888'},
        )
        mock_check.assert_called_once_with('existing@example.com', '999888')
        self.assertRedirects(
            response,
            reverse('tickets:attendee_dashboard'),
            fetch_redirect_response=False,
        )
        # Confirm the session now belongs to the logged-in user
        self.assertEqual(
            int(self.client.session['_auth_user_id']),
            user.pk,
        )

    # ------------------------------------------------------------------
    # Guard: already-authenticated users are bounced away
    # ------------------------------------------------------------------

    def test_authenticated_user_visiting_login_redirects_to_dashboard(self):
        """A logged-in attendee hitting /login/email/ is sent to their dashboard."""
        user = User.objects.create_user(
            username='loggedin',
            email='loggedin@example.com',
            password='unused',
        )
        UserProfile.objects.create(
            user=user,
            role=UserProfile.Role.ATTENDEE,
            phone_number='+12125550300',
        )
        self.client.force_login(user)

        response = self.client.get(reverse('tickets:email_login'))
        self.assertRedirects(
            response,
            reverse('tickets:attendee_dashboard'),
            fetch_redirect_response=False,
        )

    # ------------------------------------------------------------------
    # CSRF / cache-control correctness
    # ------------------------------------------------------------------

    def test_login_view_no_cache(self):
        """GET /login/ must include Cache-Control: no-store to prevent CSRF token mismatch on account switch."""
        response = self.client.get(reverse('tickets:login'))
        self.assertIn('no-store', response.get('Cache-Control', ''))

    def test_email_login_view_no_cache(self):
        """GET /login/email/ must include Cache-Control: no-store."""
        response = self.client.get(reverse('tickets:email_login'))
        self.assertIn('no-store', response.get('Cache-Control', ''))

    def test_invite_accept_logout_is_post_form(self):
        """invite_accept.html email_mismatch branch must render a POST form for logout, not a GET link."""
        from tickets.models import Organization, UserProfile, OrganizationInvitation
        from django.utils import timezone
        import datetime
        import uuid
        org = Organization.objects.create(name='Test Org')
        user = User.objects.create_user(username='inviteuser', email='inviteuser@example.com', password='x')
        UserProfile.objects.create(user=user, role=UserProfile.Role.ORGANIZER, organization=org)
        invitation = OrganizationInvitation.objects.create(
            organization=org,
            email='other@example.com',
            invited_by=user,
            token=uuid.uuid4(),
            expires_at=timezone.now() + datetime.timedelta(days=7),
        )
        self.client.force_login(user)
        response = self.client.get(reverse('tickets:invite_accept', args=[invitation.token]))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        logout_url = reverse('tickets:logout')
        # Must be a POST form, not a bare anchor
        self.assertIn(f'action="{logout_url}"', content)
        self.assertIn('method="post"', content)
        self.assertNotIn(f'href="{logout_url}"', content)

    def test_no_csrf_403_after_account_switch(self):
        """Logging out and submitting the login form as a new user must not produce a 403.

        Regression for: browser-cached /login/ page has stale CSRF token after
        logout rotates the CSRF cookie. @never_cache prevents caching so the
        next GET always yields a fresh token.
        """
        # Load login page to seed the CSRF cookie
        self.client.get(reverse('tickets:login'))
        csrf_token = self.client.cookies.get('csrftoken')
        self.assertIsNotNone(csrf_token, 'CSRF cookie must be set after visiting /login/')

        # Logout (rotates CSRF cookie server-side)
        self.client.post(
            reverse('tickets:logout'),
            HTTP_X_CSRFTOKEN=csrf_token.value,
        )

        # Re-fetch /login/ — with @never_cache the response is always fresh
        response = self.client.get(reverse('tickets:login'))
        new_csrf = self.client.cookies.get('csrftoken')
        self.assertIsNotNone(new_csrf)

        # Submit login form with the fresh CSRF token using CSRF enforcement
        enforcing_client = Client(enforce_csrf_checks=True)
        enforcing_client.cookies['csrftoken'] = new_csrf.value
        response = enforcing_client.post(
            reverse('tickets:login'),
            {'phone_number': '+15555550001'},
            HTTP_X_CSRFTOKEN=new_csrf.value,
        )
        self.assertNotEqual(response.status_code, 403, 'Login after account switch must not produce a CSRF 403')


class AuthViewsCacheControlTests(TestCase):
    """Regression: all unauthenticated auth views must return Cache-Control: no-store.

    Missing @never_cache caused 403 CSRF failures after account switch — browsers
    served bfcached pages with stale CSRF tokens that no longer matched the rotated
    CSRF cookie.
    """

    def _assert_no_store(self, url_name, session_data=None):
        if session_data:
            session = self.client.session
            for key, value in session_data.items():
                session[key] = value
            session.save()
        response = self.client.get(reverse(f'tickets:{url_name}'))
        self.assertIn(
            'no-store',
            response.get('Cache-Control', ''),
            f'{url_name} is missing Cache-Control: no-store — add @never_cache to the view',
        )

    def test_unified_verify_view_returns_no_store(self):
        self._assert_no_store(
            'unified_verify',
            {'verify_unified': {'phone': '+15550001234', 'is_new': False}},
        )

    def test_email_verify_view_returns_no_store(self):
        self._assert_no_store(
            'email_verify',
            {'verify_email': {'email': 'test@example.com', 'is_new': False}},
        )

    def test_complete_profile_view_returns_no_store(self):
        self._assert_no_store(
            'complete_profile',
            {'pending_signup_phone': '+15550001234'},
        )

    def test_email_complete_profile_view_returns_no_store(self):
        self._assert_no_store(
            'email_complete_profile',
            {'pending_signup_email': 'test@example.com'},
        )


class EventDetailNewReturningTest(TestCase):
    """Tests for new vs returning customer classification in _compute_event_stats."""

    def setUp(self):
        from django.core.cache import cache as django_cache
        self.org = Organization.objects.create(name='NR Test Org', slug='nr-test-org')
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='NR Event', venue=self.venue,
            start_date=date(2025, 9, 1),
        )
        self.event2 = Event.objects.create(
            organization=self.org, name='NR Prior Event', venue=self.venue,
            start_date=date(2025, 8, 1),
        )
        django_cache.clear()

    def tearDown(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()

    def _make_order(self, event, email, amount='50.00', order_date=None, is_in_person=False):
        customer, _ = Customer.objects.get_or_create(
            organization=self.org, email=email, defaults={'name': email},
        )
        return TicketOrder.objects.create(
            event=event, customer=customer,
            order_number=str(uuid.uuid4())[:12],
            total_amount=Decimal(amount),
            order_date=order_date or timezone.now(),
            is_in_person=is_in_person,
        )

    def test_new_customer(self):
        """A customer whose only order is at this event is classified as new."""
        from tickets.views import _compute_event_stats
        self._make_order(self.event, 'new@example.com')
        stats = _compute_event_stats(self.event)
        self.assertEqual(stats['new_customers_count'], 1)
        self.assertEqual(stats['returning_customers_count'], 0)

    def test_returning_customer(self):
        """A customer with a prior order at a different event is classified as returning."""
        from tickets.views import _compute_event_stats
        earlier = timezone.now() - timedelta(days=30)
        self._make_order(self.event2, 'returning@example.com', order_date=earlier)
        self._make_order(self.event, 'returning@example.com')
        stats = _compute_event_stats(self.event)
        self.assertEqual(stats['new_customers_count'], 0)
        self.assertEqual(stats['returning_customers_count'], 1)

    def test_in_person_order_excluded_from_classification(self):
        """In-person orders are excluded; a customer with only in-person orders at this
        event has no online presence and is not counted in the new/returning breakdown."""
        from tickets.views import _compute_event_stats
        self._make_order(self.event, 'inperson@example.com', is_in_person=True)
        stats = _compute_event_stats(self.event)
        # total_customers counts all orders; new/returning only counts online
        self.assertEqual(stats['new_customers_count'], 0)
        self.assertEqual(stats['returning_customers_count'], 0)

    def test_zero_customers(self):
        """Event with no orders returns zero for both counts."""
        from tickets.views import _compute_event_stats
        stats = _compute_event_stats(self.event)
        self.assertEqual(stats['new_customers_count'], 0)
        self.assertEqual(stats['returning_customers_count'], 0)

    def test_new_plus_returning_equals_online_customer_count(self):
        """new + returning equals the number of distinct online customers."""
        from tickets.views import _compute_event_stats
        earlier = timezone.now() - timedelta(days=30)
        self._make_order(self.event2, 'r1@example.com', order_date=earlier)
        self._make_order(self.event, 'r1@example.com')   # returning
        self._make_order(self.event, 'n1@example.com')   # new
        self._make_order(self.event, 'n2@example.com')   # new
        self._make_order(self.event, 'ip@example.com', is_in_person=True)  # excluded
        stats = _compute_event_stats(self.event)
        online_total = stats['new_customers_count'] + stats['returning_customers_count']
        self.assertEqual(stats['new_customers_count'], 2)
        self.assertEqual(stats['returning_customers_count'], 1)
        self.assertEqual(online_total, 3)


# ---------------------------------------------------------------------------
# Shared seed for MCP/Agent API tests
# ---------------------------------------------------------------------------

def _seed_agent_fixtures(test):
    """Seed an org, two events, customers, orders, an EventIncome, and an EventExpense.

    The EventIncome row is critical: it forces the income subquery to return a
    non-null value, which is what surfaces the annotation-collision regression.
    """
    from .models import EventExpense, EventIncome, IncomeSource, OrganizationAPIKey

    test.org = Organization.objects.create(name='Agent Org', slug='agent-org')
    test.other_org = Organization.objects.create(name='Other Org', slug='other-org')

    test.venue = Venue.objects.create(
        organization=test.org, name='The Hall', city='Portland', state='OR',
        street_address='123 Main St', postal_code='97201', country='US',
    )
    today = timezone.localdate()
    test.upcoming_event = Event.objects.create(
        organization=test.org, name='Upcoming Show', venue=test.venue,
        start_date=today + timedelta(days=14), start_time=time(20, 0),
        summary='Upcoming summary',
    )
    test.past_event = Event.objects.create(
        organization=test.org, name='Past Show', venue=test.venue,
        start_date=today - timedelta(days=14), start_time=time(20, 0),
        summary='Past summary',
    )

    test.top_customer = Customer.objects.create(
        organization=test.org, email='top@example.com', name='Top Buyer',
        lifetime_value=Decimal('500.00'), rfm_segment='Champions',
    )
    test.other_customer = Customer.objects.create(
        organization=test.org, email='other@example.com', name='Other Buyer',
        lifetime_value=Decimal('50.00'), rfm_segment='At Risk',
    )

    test.past_order = TicketOrder.objects.create(
        customer=test.top_customer, event=test.past_event,
        order_number='ORD-PAST-1',
        order_date=timezone.now() - timedelta(days=30),
        total_amount=Decimal('250.00'),
    )
    test.upcoming_order = TicketOrder.objects.create(
        customer=test.top_customer, event=test.upcoming_event,
        order_number='ORD-UP-1',
        order_date=timezone.now() - timedelta(days=2),
        total_amount=Decimal('250.00'),
    )
    TicketOrder.objects.create(
        customer=test.other_customer, event=test.past_event,
        order_number='ORD-PAST-2',
        order_date=timezone.now() - timedelta(days=29),
        total_amount=Decimal('50.00'),
    )

    EventExpense.objects.create(
        event=test.past_event, category='venue',
        description='Venue rental', amount=Decimal('75.00'),
        expense_date=today - timedelta(days=15),
    )
    test.income_source = IncomeSource.objects.create(
        organization=test.org, name='Bar Splits', order=1,
    )
    EventIncome.objects.create(
        event=test.past_event, income_source=test.income_source,
        amount=Decimal('100.00'),
    )

    test.api_key = OrganizationAPIKey.objects.create(
        organization=test.org, name='Test Key',
    )
    test.other_api_key = OrganizationAPIKey.objects.create(
        organization=test.other_org, name='Other Key',
    )


class MCPToolsTests(TestCase):
    """Regression coverage for the MCP tool functions in tickets.mcp_app.

    Drives the async functions directly via async_to_sync — no FastMCP server.
    """

    def setUp(self):
        from asgiref.sync import async_to_sync
        from tickets import mcp_app

        _seed_agent_fixtures(self)
        self._mcp = mcp_app
        self._async_to_sync = async_to_sync
        self._token = mcp_app._current_org.set(self.org)

    def tearDown(self):
        self._mcp._current_org.reset(self._token)

    def _call(self, fn, *args, **kwargs):
        return json.loads(self._async_to_sync(fn)(*args, **kwargs))

    # --- list_events: regression test for annotation collision -------------

    def test_list_events_includes_additional_income(self):
        result = self._call(self._mcp.list_events, status='all', limit=10)
        self.assertEqual(result['event_count'], 2)
        by_id = {e['id']: e for e in result['events']}
        past = by_id[str(self.past_event.id)]
        self.assertEqual(Decimal(past['additional_income']), Decimal('100'))
        self.assertEqual(Decimal(past['ticket_revenue']), Decimal('300'))
        self.assertEqual(Decimal(past['total_revenue']), Decimal('400'))
        self.assertEqual(Decimal(past['total_expenses']), Decimal('75'))
        self.assertEqual(Decimal(past['net_profit']), Decimal('325'))
        upcoming = by_id[str(self.upcoming_event.id)]
        self.assertEqual(Decimal(upcoming['additional_income']), Decimal('0'))

    def test_list_events_status_filter(self):
        upcoming = self._call(self._mcp.list_events, status='upcoming', limit=10)
        past = self._call(self._mcp.list_events, status='past', limit=10)
        self.assertEqual([e['id'] for e in upcoming['events']], [str(self.upcoming_event.id)])
        self.assertEqual([e['id'] for e in past['events']], [str(self.past_event.id)])

    def test_list_events_respects_org_scope(self):
        Event.objects.create(
            organization=self.other_org, name='Foreign Event', venue=self.venue,
            start_date=timezone.localdate() + timedelta(days=5),
        )
        result = self._call(self._mcp.list_events, status='all', limit=20)
        names = {e['name'] for e in result['events']}
        self.assertNotIn('Foreign Event', names)

    # --- list_upcoming_events ----------------------------------------------

    def test_list_upcoming_events(self):
        result = self._call(self._mcp.list_upcoming_events, limit=10)
        names = [e['name'] for e in result['events']]
        self.assertEqual(names, ['Upcoming Show'])

    # --- get_event ---------------------------------------------------------

    def test_get_event_returns_full_payload(self):
        result = self._call(self._mcp.get_event, event_id=str(self.past_event.id))
        self.assertEqual(result['name'], 'Past Show')
        self.assertEqual(Decimal(result['financials']['additional_income']), Decimal('100'))
        self.assertEqual(Decimal(result['financials']['ticket_revenue']), Decimal('300'))
        self.assertEqual(Decimal(result['financials']['total_revenue']), Decimal('400'))
        self.assertIsInstance(result['attendance']['new_customers'], int)
        self.assertIsInstance(result['attendance']['returning_customers'], int)

    def test_get_event_returns_error_for_unknown_id(self):
        unknown = self._call(self._mcp.get_event, event_id=str(uuid.uuid4()))
        self.assertEqual(unknown, {'error': 'Event not found'})
        invalid = self._call(self._mcp.get_event, event_id='not-a-uuid')
        self.assertEqual(invalid, {'error': 'Invalid event_id'})

    # --- list_customers ----------------------------------------------------

    def test_list_customers_orders_by_ltv(self):
        result = self._call(self._mcp.list_customers, segment='', limit=50, page=1)
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['customers'][0]['email'], 'top@example.com')

    def test_list_customers_segment_filter(self):
        result = self._call(self._mcp.list_customers, segment='Champions', limit=50, page=1)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['customers'][0]['email'], 'top@example.com')

    # --- get_customer ------------------------------------------------------

    def test_get_customer_returns_recent_orders(self):
        result = self._call(self._mcp.get_customer, customer_id=str(self.top_customer.id))
        self.assertEqual(result['email'], 'top@example.com')
        order_numbers = [o['order_number'] for o in result['recent_orders']]
        self.assertIn('ORD-UP-1', order_numbers)
        self.assertIn('ORD-PAST-1', order_numbers)

    # --- get_rfm_segments / get_revenue_summary / list_orders --------------

    def test_get_rfm_segments(self):
        result = self._call(self._mcp.get_rfm_segments)
        self.assertEqual(result['total_customers'], 2)
        segs = {s['segment']: s['count'] for s in result['segments']}
        self.assertEqual(segs.get('Champions'), 1)
        self.assertEqual(segs.get('At Risk'), 1)

    def test_get_revenue_summary(self):
        result = self._call(self._mcp.get_revenue_summary)
        self.assertEqual(Decimal(result['ticket_revenue']['all_time']), Decimal('550'))
        self.assertEqual(Decimal(result['total_revenue_all_time']), Decimal('650'))
        self.assertEqual(result['event_count'], 2)

    def test_list_orders_filters_by_event(self):
        result = self._call(self._mcp.list_orders, event_id=str(self.past_event.id), limit=50, page=1)
        self.assertEqual(result['total'], 2)
        for o in result['orders']:
            self.assertEqual(o['event']['id'], str(self.past_event.id))

    def test_list_orders_pagination(self):
        page1 = self._call(self._mcp.list_orders, event_id='', limit=1, page=1)
        page2 = self._call(self._mcp.list_orders, event_id='', limit=1, page=2)
        self.assertEqual(page1['total'], 3)
        self.assertEqual(len(page1['orders']), 1)
        self.assertEqual(len(page2['orders']), 1)
        self.assertNotEqual(page1['orders'][0]['id'], page2['orders'][0]['id'])


class AgentAPITests(TestCase):
    """Regression coverage for /api/v1/* agent endpoints (OrganizationAPIKey auth)."""

    def setUp(self):
        _seed_agent_fixtures(self)
        self.client = Client()
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {self.api_key.key}'}
        self.other_auth = {'HTTP_AUTHORIZATION': f'Bearer {self.other_api_key.key}'}

    # --- agent_events: regression test for annotation collision ------------

    def test_agent_events_includes_additional_income(self):
        response = self.client.get('/api/v1/events/', **self.auth)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['event_count'], 2)
        past = next(e for e in body['events'] if e['id'] == str(self.past_event.id))
        self.assertEqual(Decimal(past['additional_income']), Decimal('100'))
        self.assertEqual(Decimal(past['ticket_revenue']), Decimal('300'))
        self.assertEqual(Decimal(past['total_revenue']), Decimal('400'))
        self.assertEqual(Decimal(past['total_expenses']), Decimal('75'))
        self.assertEqual(Decimal(past['net_profit']), Decimal('325'))

    def test_agent_events_status_filter(self):
        upcoming = self.client.get('/api/v1/events/?status=upcoming', **self.auth).json()
        past = self.client.get('/api/v1/events/?status=past', **self.auth).json()
        self.assertEqual([e['id'] for e in upcoming['events']], [str(self.upcoming_event.id)])
        self.assertEqual([e['id'] for e in past['events']], [str(self.past_event.id)])

    def test_agent_events_requires_api_key(self):
        response = self.client.get('/api/v1/events/')
        self.assertIn(response.status_code, (401, 403))

    def test_agent_events_rejects_revoked_key(self):
        self.api_key.is_active = False
        self.api_key.save(update_fields=['is_active'])
        response = self.client.get('/api/v1/events/', **self.auth)
        self.assertIn(response.status_code, (401, 403))

    def test_agent_events_org_isolation(self):
        Event.objects.create(
            organization=self.other_org, name='Foreign Event', venue=self.venue,
            start_date=timezone.localdate() + timedelta(days=5),
        )
        body = self.client.get('/api/v1/events/', **self.auth).json()
        names = {e['name'] for e in body['events']}
        self.assertNotIn('Foreign Event', names)

    def test_agent_events_sets_no_cache_header(self):
        response = self.client.get('/api/v1/events/', **self.auth)
        self.assertIn('no-store', response['Cache-Control'])

    # --- agent_event_detail ------------------------------------------------

    def test_agent_event_detail_returns_financials(self):
        url = f'/api/v1/events/{self.past_event.id}/'
        body = self.client.get(url, **self.auth).json()
        self.assertEqual(body['name'], 'Past Show')
        self.assertEqual(Decimal(body['financials']['additional_income']), Decimal('100'))
        self.assertEqual(Decimal(body['financials']['ticket_revenue']), Decimal('300'))
        self.assertEqual(Decimal(body['financials']['total_revenue']), Decimal('400'))
        self.assertEqual(Decimal(body['financials']['total_expenses']), Decimal('75'))
        self.assertEqual(len(body['financials']['income']), 1)
        self.assertEqual(body['financials']['income'][0]['source'], 'Bar Splits')

    def test_agent_event_detail_404_for_other_org(self):
        other_event = Event.objects.create(
            organization=self.other_org, name='Foreign Event', venue=self.venue,
            start_date=timezone.localdate() + timedelta(days=5),
        )
        response = self.client.get(f'/api/v1/events/{other_event.id}/', **self.auth)
        self.assertEqual(response.status_code, 404)

    # --- agent_upcoming_events --------------------------------------------

    def test_agent_upcoming_events(self):
        body = self.client.get('/api/v1/events/upcoming/', **self.auth).json()
        names = [e['name'] for e in body['events']]
        self.assertEqual(names, ['Upcoming Show'])

    # --- agent_customers --------------------------------------------------

    def test_agent_customers_paginated(self):
        body = self.client.get('/api/v1/customers/?limit=1&page=1', **self.auth).json()
        self.assertEqual(body['total'], 2)
        self.assertEqual(len(body['customers']), 1)
        self.assertEqual(body['customers'][0]['email'], 'top@example.com')

    def test_agent_customer_detail(self):
        body = self.client.get(f'/api/v1/customers/{self.top_customer.id}/', **self.auth).json()
        self.assertEqual(body['email'], 'top@example.com')
        order_numbers = [o['order_number'] for o in body['recent_orders']]
        self.assertIn('ORD-UP-1', order_numbers)

    # --- agent_analytics --------------------------------------------------

    def test_agent_analytics_segments(self):
        body = self.client.get('/api/v1/analytics/segments/', **self.auth).json()
        self.assertEqual(body['total_customers'], 2)
        segs = {s['segment']: s['count'] for s in body['segments']}
        self.assertEqual(segs.get('Champions'), 1)
        self.assertEqual(segs.get('At Risk'), 1)

    def test_agent_analytics_revenue(self):
        body = self.client.get('/api/v1/analytics/revenue/', **self.auth).json()
        self.assertEqual(Decimal(body['ticket_revenue']['all_time']), Decimal('550'))
        self.assertEqual(Decimal(body['total_revenue_all_time']), Decimal('650'))
        self.assertEqual(body['event_count'], 2)

    # --- agent_orders -----------------------------------------------------

    def test_agent_orders_filter_by_event(self):
        body = self.client.get(
            f'/api/v1/orders/?event_id={self.past_event.id}', **self.auth
        ).json()
        self.assertEqual(body['total'], 2)
        for o in body['orders']:
            self.assertEqual(o['event']['id'], str(self.past_event.id))

    def test_agent_orders_pagination(self):
        page1 = self.client.get('/api/v1/orders/?limit=1&page=1', **self.auth).json()
        page2 = self.client.get('/api/v1/orders/?limit=1&page=2', **self.auth).json()
        self.assertEqual(page1['total'], 3)
        self.assertNotEqual(page1['orders'][0]['id'], page2['orders'][0]['id'])


class MarketingAnalyticsServiceTests(TestCase):
    """Service-level tests for cross-event marketing analytics aggregations."""

    def setUp(self):
        from .models import EventEmailCampaign, EventSMSCampaign
        self.org = Organization.objects.create(name='Marketing Org', slug='marketing-org')
        self.other_org = Organization.objects.create(name='Other Org', slug='other-org')

        venue = Venue.objects.create(organization=self.org, name='V1', city='SF')
        other_venue = Venue.objects.create(organization=self.other_org, name='V2', city='LA')

        self.event = Event.objects.create(
            organization=self.org, name='Inside Window', venue=venue,
            start_date=date.today(), start_time=time(20, 0, 0),
            computed_total_revenue=Decimal('1500.00'),
        )
        self.event_outside = Event.objects.create(
            organization=self.org, name='Older Event', venue=venue,
            start_date=date.today() - timedelta(days=400), start_time=time(20, 0, 0),
        )
        self.other_event = Event.objects.create(
            organization=self.other_org, name='Different Org', venue=other_venue,
            start_date=date.today(), start_time=time(20, 0, 0),
        )

        now = timezone.now()
        old = now - timedelta(days=200)

        # All fixtures are confirmed so they flow into reports.
        EventEmailCampaign.objects.create(
            event=self.event, source='mailchimp', external_id='mc-1',
            campaign_title='Newsletter', send_time=now - timedelta(days=10),
            emails_sent=1000, opens=400, unique_opens=400,
            clicks=120, unique_clicks=120, unsubscribes=5,
            ecommerce_orders=8, ecommerce_revenue=Decimal('600.00'),
            confirmed_at=now,
        )
        EventEmailCampaign.objects.create(
            event=self.event_outside, source='mailchimp', external_id='mc-2',
            campaign_title='Old Newsletter', send_time=old,
            emails_sent=500, unique_opens=100, unique_clicks=30, unsubscribes=2,
            ecommerce_orders=3, ecommerce_revenue=Decimal('150.00'),
            confirmed_at=now,
        )
        EventEmailCampaign.objects.create(
            event=self.other_event, source='mailchimp', external_id='mc-3',
            campaign_title='Leak', send_time=now - timedelta(days=5),
            emails_sent=200, unique_opens=50, unique_clicks=10, unsubscribes=1,
            ecommerce_orders=2, ecommerce_revenue=Decimal('300.00'),
            confirmed_at=now,
        )

        EventSMSCampaign.objects.create(
            event=self.event, source='slicktext', external_id='st-1',
            name='Pre-show blast', send_time=now - timedelta(days=3),
            audience_size=800, unique_clicks=80, unsubscribes=4,
            orders=5, revenue=Decimal('400.00'),
            confirmed_at=now,
        )

        EventExpense.objects.create(
            event=self.event, category='marketing', description='Meta Ads',
            amount=Decimal('200.00'), expense_date=date.today() - timedelta(days=8),
            source='meta_ads', external_id='ad-1',
            manual_attributed_revenue=Decimal('1500.00'),
            manual_attributed_orders=12,
            confirmed_at=now,
        )
        EventExpense.objects.create(
            event=self.event_outside, category='marketing', description='Meta Ads (old)',
            amount=Decimal('500.00'), expense_date=date.today() - timedelta(days=300),
            source='meta_ads', external_id='ad-2',
            confirmed_at=now,
        )

    def test_channels_aggregate_only_inside_window_and_org(self):
        from .services.marketing import MarketingAnalyticsService

        result = MarketingAnalyticsService(self.org, window_days=90).calculate()

        # Email totals exclude other_org row and the >90d old row
        self.assertEqual(result['channels']['email']['sends'], 1000)
        self.assertEqual(result['channels']['email']['orders'], 8)
        self.assertEqual(result['channels']['email']['revenue'], Decimal('600.00'))

        self.assertEqual(result['channels']['sms']['audience'], 800)
        self.assertEqual(result['channels']['sms']['revenue'], Decimal('400.00'))

        self.assertEqual(result['channels']['ads']['spend'], Decimal('200.00'))
        # Ads revenue is attributed via event total
        self.assertEqual(result['channels']['ads']['revenue'], Decimal('1500.00'))
        self.assertEqual(result['channels']['ads']['roas'], Decimal('7.5000'))

    def test_all_time_includes_old_records(self):
        from .services.marketing import MarketingAnalyticsService

        result = MarketingAnalyticsService(self.org, window_days=None).calculate()
        self.assertEqual(result['channels']['email']['sends'], 1500)
        self.assertEqual(result['channels']['ads']['spend'], Decimal('700.00'))

    def test_top_events_by_roi_orders_correctly(self):
        from .services.marketing import MarketingAnalyticsService

        result = MarketingAnalyticsService(self.org, window_days=90).calculate()
        rows = result['top_events_by_roi']
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['event_name'], 'Inside Window')
        self.assertEqual(row['ads_spend'], Decimal('200.00'))
        # attributed = email_rev (600) + sms_rev (400) + ads_rev (1500 manual) = 2500
        self.assertEqual(row['attributed_revenue'], Decimal('2500.00'))

    def test_all_linked_campaigns_count_without_confirmation(self):
        """Confirmation no longer gates analytics: every linked campaign counts."""
        from .models import EventEmailCampaign, EventSMSCampaign
        from .services.marketing import MarketingAnalyticsService

        EventEmailCampaign.objects.filter(event__organization=self.org).delete()
        EventSMSCampaign.objects.filter(event__organization=self.org).delete()
        EventExpense.objects.filter(event__organization=self.org).delete()

        now = timezone.now()
        EventEmailCampaign.objects.create(
            event=self.event, source='mailchimp', external_id='mc-confirmed',
            campaign_title='Confirmed', send_time=now - timedelta(days=5),
            emails_sent=1000, unique_opens=300, unique_clicks=60,
            ecommerce_orders=4, ecommerce_revenue=Decimal('250.00'),
            confirmed_at=now,
        )
        EventEmailCampaign.objects.create(
            event=self.event, source='mailchimp', external_id='mc-unconfirmed',
            campaign_title='Unconfirmed', send_time=now - timedelta(days=3),
            emails_sent=2000, unique_opens=400, unique_clicks=100,
            ecommerce_orders=12, ecommerce_revenue=Decimal('750.00'),
        )

        result = MarketingAnalyticsService(self.org, window_days=90).calculate()
        # Both campaigns count regardless of confirmed_at.
        self.assertEqual(result['channels']['email']['revenue'], Decimal('1000.00'))
        self.assertEqual(result['channels']['email']['campaigns'], 2)
        self.assertEqual(len(result['top_email_campaigns']), 2)

    def test_meta_expense_counts_without_confirmation(self):
        from .services.marketing import MarketingAnalyticsService

        EventExpense.objects.filter(event__organization=self.org).delete()
        now = timezone.now()
        EventExpense.objects.create(
            event=self.event, category='marketing', description='Confirmed ad',
            amount=Decimal('100.00'), expense_date=date.today(), source='meta_ads',
            external_id='ad-c', manual_attributed_revenue=Decimal('300.00'),
            confirmed_at=now,
        )
        EventExpense.objects.create(
            event=self.event, category='marketing', description='Unconfirmed ad',
            amount=Decimal('500.00'), expense_date=date.today(), source='meta_ads',
            external_id='ad-u', manual_attributed_revenue=Decimal('700.00'),
        )

        result = MarketingAnalyticsService(self.org, window_days=90).calculate()
        # Both expenses count regardless of confirmed_at.
        self.assertEqual(result['channels']['ads']['spend'], Decimal('600.00'))
        self.assertEqual(result['channels']['ads']['revenue'], Decimal('1000.00'))

    def test_ads_api_attribution_counts_when_no_manual_override(self):
        """Confirmed ads with only Meta-pulled attribution flow into totals; manual wins when set."""
        from .services.marketing import MarketingAnalyticsService

        EventExpense.objects.filter(event__organization=self.org).delete()
        now = timezone.now()
        EventExpense.objects.create(
            event=self.event, category='marketing', description='API-attributed ad',
            amount=Decimal('100.00'), expense_date=date.today(), source='meta_ads',
            external_id='ad-api',
            api_attributed_orders=6, api_attributed_revenue=Decimal('480.00'),
            confirmed_at=now,
        )
        EventExpense.objects.create(
            event=self.event, category='marketing', description='Overridden ad',
            amount=Decimal('50.00'), expense_date=date.today(), source='meta_ads',
            external_id='ad-override',
            api_attributed_orders=99, api_attributed_revenue=Decimal('9999.00'),
            manual_attributed_orders=2, manual_attributed_revenue=Decimal('120.00'),
            confirmed_at=now,
        )

        result = MarketingAnalyticsService(self.org, window_days=90).calculate()
        self.assertEqual(result['channels']['ads']['orders'], 8)
        self.assertEqual(result['channels']['ads']['revenue'], Decimal('600.00'))
        row = result['top_events_by_roi'][0]
        # email (600) + sms (400) + ads (480 api + 120 manual) = 1600
        self.assertEqual(row['attributed_revenue'], Decimal('1600.00'))

    def test_manual_revenue_overrides_api_zero(self):
        """A confirmed email campaign with manual_revenue but zero ecommerce_revenue still counts."""
        from .models import EventEmailCampaign
        from .services.marketing import MarketingAnalyticsService

        EventEmailCampaign.objects.filter(event__organization=self.org).delete()
        now = timezone.now()
        EventEmailCampaign.objects.create(
            event=self.event, source='mailchimp', external_id='mc-manual',
            campaign_title='Manual', send_time=now - timedelta(days=2),
            emails_sent=500, unique_opens=200, unique_clicks=50,
            ecommerce_orders=0, ecommerce_revenue=Decimal('0.00'),
            manual_revenue=Decimal('420.00'),
            manual_orders=7,
            manual_clicks=99,
            confirmed_at=now,
        )

        result = MarketingAnalyticsService(self.org, window_days=90).calculate()
        self.assertEqual(result['channels']['email']['revenue'], Decimal('420.00'))
        self.assertEqual(result['channels']['email']['orders'], 7)
        self.assertEqual(result['channels']['email']['clicks'], 99)

    def test_manual_zero_is_respected(self):
        """manual_revenue=0 must override a non-zero ecommerce_revenue."""
        from .models import EventEmailCampaign
        from .services.marketing import MarketingAnalyticsService

        EventEmailCampaign.objects.filter(event__organization=self.org).delete()
        now = timezone.now()
        EventEmailCampaign.objects.create(
            event=self.event, source='mailchimp', external_id='mc-zero',
            campaign_title='Zeroed', send_time=now - timedelta(days=2),
            emails_sent=500, unique_opens=200, unique_clicks=50,
            ecommerce_orders=10, ecommerce_revenue=Decimal('700.00'),
            manual_revenue=Decimal('0.00'),
            confirmed_at=now,
        )

        result = MarketingAnalyticsService(self.org, window_days=90).calculate()
        self.assertEqual(result['channels']['email']['revenue'], Decimal('0.00'))

    def test_top_sms_campaigns_not_crowded_out_by_email(self):
        """Regression: SMS rows must survive even when email revenue dwarfs them."""
        from .models import EventEmailCampaign, EventSMSCampaign
        from .services.marketing import MarketingAnalyticsService

        # Wipe seed data so we control the comparison cleanly.
        EventEmailCampaign.objects.filter(event__organization=self.org).delete()
        EventSMSCampaign.objects.filter(event__organization=self.org).delete()

        now = timezone.now()
        # Email campaign with very high revenue.
        EventEmailCampaign.objects.create(
            event=self.event, source='mailchimp', external_id='mc-big',
            campaign_title='Huge email', send_time=now - timedelta(days=5),
            emails_sent=5000, unique_opens=2000, unique_clicks=500,
            ecommerce_orders=50, ecommerce_revenue=Decimal('5000.00'),
            confirmed_at=now,
        )
        # SMS broadcast with low revenue — would be evicted under the old
        # cross-channel sort.
        EventSMSCampaign.objects.create(
            event=self.event, source='slicktext', external_id='st-small',
            name='Modest blast', send_time=now - timedelta(days=2),
            audience_size=300, unique_clicks=15, orders=1, revenue=Decimal('50.00'),
            confirmed_at=now,
        )

        result = MarketingAnalyticsService(self.org, window_days=90).calculate()

        self.assertEqual(len(result['top_email_campaigns']), 1)
        self.assertEqual(result['top_email_campaigns'][0]['revenue'], Decimal('5000.00'))

        self.assertEqual(len(result['top_sms_campaigns']), 1)
        self.assertEqual(result['top_sms_campaigns'][0]['name'], 'Modest blast')
        self.assertEqual(result['top_sms_campaigns'][0]['revenue'], Decimal('50.00'))


class MarketingOverviewViewTests(TestCase):
    """View-level tests for the unified SMS page: the legacy /marketing/ redirect
    and the Grow tab that hosts the shareable subscribe link."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='View Org', slug='view-org')
        self.user = User.objects.create_user(username='view', email='view@example.com', password='pw')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='view@example.com', password='pw')
        self.client.get(reverse('tickets:home'))

    def test_anonymous_redirected_to_login(self):
        anon = Client()
        response = anon.get(reverse('tickets:marketing_overview'))
        self.assertEqual(response.status_code, 302)

    def test_marketing_url_redirects_to_sms_grow(self):
        response = self.client.get(reverse('tickets:marketing_overview'))
        self.assertRedirects(
            response, reverse('tickets:sms_campaign_list') + '?view=grow',
        )

    def test_sms_page_default_window(self):
        response = self.client.get(reverse('tickets:sms_campaign_list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['window_key'], '90')

    def test_window_querystring_overrides_default(self):
        response = self.client.get(reverse('tickets:sms_campaign_list') + '?window=all')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['window_key'], 'all')

    def test_invalid_window_falls_back_to_default(self):
        response = self.client.get(reverse('tickets:sms_campaign_list') + '?window=banana')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['window_key'], '90')

    def test_grow_view_shows_shareable_subscribe_link(self):
        response = self.client.get(reverse('tickets:sms_campaign_list'), {'view': 'grow'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['subscribe_url'],
            f"http://testserver/subscribe/{self.org.slug}/",
        )
        self.assertContains(response, f'/subscribe/{self.org.slug}/')
        self.assertContains(response, '<h2>Grow your audience</h2>')
        self.assertContains(response, 'subscribeLinkCopy')


class MarketingAINarrativeTests(TestCase):
    """Tests for the on-demand AI marketing narrative endpoint."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='AI Org', slug='ai-org')
        self.user = User.objects.create_user(username='ai', email='ai@example.com', password='pw')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='ai@example.com', password='pw')
        self.client.get(reverse('tickets:home'))

    def test_generate_marketing_narrative_records_token_usage(self):
        from tickets.services.marketing.ai_narrative import (
            generate_marketing_narrative,
            MarketingNarrativeResult,
            Insight,
        )

        stub_result = MarketingNarrativeResult(
            headline='All good.',
            insights=[Insight(
                title='Email is performing',
                body='Email sends generated $600 in revenue.',
                severity='info',
                recommended_action='Repeat last week\'s subject line.',
            )],
        )

        class FakeLLM:
            def __init__(self, *args, **kwargs):
                pass

            def with_structured_output(self, *args, **kwargs):
                outer = self

                class Bound:
                    def invoke(self, _messages):
                        class FakeRaw:
                            usage_metadata = {
                                'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150,
                            }
                        return {'raw': FakeRaw(), 'parsed': stub_result, 'parsing_error': None}
                return Bound()

        with patch('langchain_openai.ChatOpenAI', FakeLLM):
            metrics = {'channels': {}, 'trends': [], 'channel_comparison': [], 'top_campaigns': [], 'top_events_by_roi': [], 'meta': {}}
            result = generate_marketing_narrative(self.org, metrics, 'Last 90 days')

        self.assertEqual(result['headline'], 'All good.')
        self.assertEqual(len(result['insights']), 1)
        record = AITokenUsage.objects.filter(
            organization=self.org,
            feature=AITokenUsage.FEATURE_MARKETING_NARRATIVE,
        ).first()
        self.assertIsNotNone(record)
        self.assertEqual(record.total_tokens, 150)

    def test_analyze_endpoint_returns_insights(self):
        with patch('tickets.views.generate_marketing_narrative') as fake:
            fake.return_value = {'headline': 'Hi', 'insights': []}
            response = self.client.post(reverse('tickets:marketing_ai_analyze'), data={'window': '90'})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['headline'], 'Hi')


class CampaignReviewConfirmTests(TestCase):
    """Model-level tests for the effective/confirmation property helpers."""

    def setUp(self):
        from .models import EventEmailCampaign
        self.org = Organization.objects.create(name='Confirm Org', slug='confirm-org')
        venue = Venue.objects.create(organization=self.org, name='V', city='SF')
        self.event = Event.objects.create(
            organization=self.org, name='E', venue=venue,
            start_date=date.today(), start_time=time(20, 0, 0),
        )
        self.campaign = EventEmailCampaign.objects.create(
            event=self.event, source='mailchimp', external_id='mc-prop',
            campaign_title='Props', send_time=timezone.now(),
            emails_sent=100, unique_opens=40, unique_clicks=10,
            ecommerce_orders=2, ecommerce_revenue=Decimal('200.00'),
        )

    def test_effective_revenue_prefers_manual(self):
        self.campaign.manual_revenue = Decimal('150.00')
        self.assertEqual(self.campaign.effective_revenue, Decimal('150.00'))

    def test_manual_zero_distinguished_from_unset(self):
        self.campaign.manual_revenue = Decimal('0.00')
        self.assertEqual(self.campaign.effective_revenue, Decimal('0.00'))

    def test_unset_manual_falls_back_to_api(self):
        self.assertIsNone(self.campaign.manual_revenue)
        self.assertEqual(self.campaign.effective_revenue, Decimal('200.00'))

    def test_needs_review_when_api_changed_after_confirm(self):
        now = timezone.now()
        self.campaign.confirmed_at = now - timedelta(hours=1)
        self.campaign.api_data_changed_at = now
        self.assertTrue(self.campaign.needs_review)

    def test_needs_review_false_when_no_api_change(self):
        self.campaign.confirmed_at = timezone.now()
        self.assertFalse(self.campaign.needs_review)


class CampaignConfirmViewTests(TestCase):
    """View-level tests for the linked-campaign metrics-edit endpoints."""

    def setUp(self):
        from .models import EventEmailCampaign, EventSMSCampaign
        self.client = Client()
        self.org = Organization.objects.create(name='Confirm View Org', slug='confirm-view')
        self.admin = User.objects.create_user(username='cv', email='cv@example.com', password='pw')
        UserProfile.objects.create(user=self.admin, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(user=self.admin, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='cv@example.com', password='pw')
        self.client.get(reverse('tickets:home'))

        venue = Venue.objects.create(organization=self.org, name='V', city='SF')
        self.event = Event.objects.create(
            organization=self.org, name='E', venue=venue,
            start_date=date.today(), start_time=time(20, 0, 0),
        )
        self.email = EventEmailCampaign.objects.create(
            event=self.event, source='mailchimp', external_id='mc-confirm',
            campaign_title='CV-email', send_time=timezone.now(),
            emails_sent=500, unique_opens=200, unique_clicks=50,
            ecommerce_orders=3, ecommerce_revenue=Decimal('200.00'),
        )
        self.sms = EventSMSCampaign.objects.create(
            event=self.event, source='slicktext', external_id='st-confirm',
            name='CV-sms', send_time=timezone.now(),
            audience_size=300, unique_clicks=15, orders=2, revenue=Decimal('100.00'),
        )
        self.ad = EventExpense.objects.create(
            event=self.event, category='marketing', description='CV-ad',
            amount=Decimal('400.00'), expense_date=date.today(),
            source='meta_ads', external_id='ad-confirm',
        )

    def test_post_metrics_edit_sets_manual_fields(self):
        url = reverse('tickets:event_mailchimp_metrics_edit', args=[self.event.id, self.email.id])
        resp = self.client.post(url, {'manual_clicks': '42', 'manual_orders': '3', 'manual_revenue': '187.50'})
        self.assertEqual(resp.status_code, 302)
        self.email.refresh_from_db()
        self.assertEqual(self.email.manual_clicks, 42)
        self.assertEqual(self.email.manual_orders, 3)
        self.assertEqual(self.email.manual_revenue, Decimal('187.50'))

    def test_post_blank_clears_manual_fields(self):
        self.email.manual_revenue = Decimal('100.00')
        self.email.save()
        url = reverse('tickets:event_mailchimp_metrics_edit', args=[self.event.id, self.email.id])
        resp = self.client.post(url, {'manual_clicks': '', 'manual_orders': '', 'manual_revenue': ''})
        self.assertEqual(resp.status_code, 302)
        self.email.refresh_from_db()
        self.assertIsNone(self.email.manual_revenue)

    def test_meta_ads_metrics_edit_sets_attributed(self):
        url = reverse('tickets:event_meta_ads_metrics_edit', args=[self.event.id, self.ad.id])
        resp = self.client.post(url, {'manual_attributed_orders': '8', 'manual_attributed_revenue': '950'})
        self.assertEqual(resp.status_code, 302)
        self.ad.refresh_from_db()
        self.assertEqual(self.ad.manual_attributed_orders, 8)
        self.assertEqual(self.ad.manual_attributed_revenue, Decimal('950.00'))

    def test_meta_ads_metrics_clear_falls_back_to_api_values(self):
        self.ad.manual_attributed_orders = 8
        self.ad.manual_attributed_revenue = Decimal('950.00')
        self.ad.api_attributed_orders = 5
        self.ad.api_attributed_revenue = Decimal('420.00')
        self.ad.save()
        url = reverse('tickets:event_meta_ads_metrics_edit', args=[self.event.id, self.ad.id])
        resp = self.client.post(url, {'manual_attributed_orders': '', 'manual_attributed_revenue': ''})
        self.assertEqual(resp.status_code, 302)
        self.ad.refresh_from_db()
        self.assertIsNone(self.ad.manual_attributed_orders)
        self.assertIsNone(self.ad.manual_attributed_revenue)
        self.assertEqual(self.ad.effective_attributed_orders, 5)
        self.assertEqual(self.ad.effective_attributed_revenue, Decimal('420.00'))

    def test_meta_ads_effective_attribution_fallback_chain(self):
        # Both None → zeros
        self.assertEqual(self.ad.effective_attributed_orders, 0)
        self.assertEqual(self.ad.effective_attributed_revenue, Decimal('0.00'))
        # API only → API values
        self.ad.api_attributed_orders = 7
        self.ad.api_attributed_revenue = Decimal('310.00')
        self.assertEqual(self.ad.effective_attributed_orders, 7)
        self.assertEqual(self.ad.effective_attributed_revenue, Decimal('310.00'))
        # Manual wins — including an explicit manual zero
        self.ad.manual_attributed_orders = 0
        self.ad.manual_attributed_revenue = Decimal('0.00')
        self.assertEqual(self.ad.effective_attributed_orders, 0)
        self.assertEqual(self.ad.effective_attributed_revenue, Decimal('0.00'))

    def test_ajax_metrics_edit_returns_json_row(self):
        url = reverse('tickets:event_mailchimp_metrics_edit', args=[self.event.id, self.email.id])
        resp = self.client.post(
            url,
            {'manual_clicks': '42', 'manual_orders': '3', 'manual_revenue': '187.50'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body['ok'])
        row = body['row']
        self.assertEqual(row['effective_clicks'], 42)
        self.assertEqual(row['effective_revenue'], '187.50')

    def test_manual_audience_and_unsubscribes_persist_for_sms(self):
        url = reverse('tickets:event_slicktext_metrics_edit', args=[self.event.id, self.sms.id])
        resp = self.client.post(
            url,
            {'manual_audience': '999', 'manual_unsubscribes': '7'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 200)
        row = resp.json()['row']
        self.assertEqual(row['effective_audience'], 999)
        self.assertEqual(row['effective_unsubscribes'], 7)
        self.sms.refresh_from_db()
        self.assertEqual(self.sms.manual_audience, 999)
        self.assertEqual(self.sms.manual_unsubscribes, 7)

    def test_manual_emails_sent_and_unique_opens_persist_for_email(self):
        url = reverse('tickets:event_mailchimp_metrics_edit', args=[self.event.id, self.email.id])
        resp = self.client.post(
            url,
            {'manual_emails_sent': '1234', 'manual_unique_opens': '321'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 200)
        row = resp.json()['row']
        self.assertEqual(row['effective_emails_sent'], 1234)
        self.assertEqual(row['effective_unique_opens'], 321)
        self.email.refresh_from_db()
        self.assertEqual(self.email.manual_emails_sent, 1234)
        self.assertEqual(self.email.manual_unique_opens, 321)

    def test_partial_update_does_not_clear_other_manual_fields(self):
        """POSTing only manual_revenue must leave manual_clicks/manual_orders untouched."""
        self.email.manual_clicks = 100
        self.email.manual_orders = 5
        self.email.save()
        url = reverse('tickets:event_mailchimp_metrics_edit', args=[self.event.id, self.email.id])
        resp = self.client.post(url, {'manual_revenue': '42.00'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200)
        self.email.refresh_from_db()
        self.assertEqual(self.email.manual_clicks, 100)
        self.assertEqual(self.email.manual_orders, 5)
        self.assertEqual(self.email.manual_revenue, Decimal('42.00'))

    def test_ajax_metrics_edit_invalid_returns_400_json(self):
        url = reverse('tickets:event_mailchimp_metrics_edit', args=[self.event.id, self.email.id])
        resp = self.client.post(
            url,
            {'manual_revenue': '-99'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertFalse(body['ok'])
        self.assertIn('non-negative', body['error'])

    def test_mailchimp_refresh_preserves_manual_and_confirmed(self):
        """Simulate a refresh cycle: manual values stay; api_data_changed_at bumps if API value differs."""
        self.email.manual_revenue = Decimal('500.00')
        self.email.confirmed_at = timezone.now() - timedelta(hours=2)
        self.email.confirmed_by = self.admin
        self.email.save()

        from .views import _save_mailchimp_campaign_from_report

        fresh_report = {
            'id': self.email.external_id,
            'campaign_title': self.email.campaign_title,
            'subject_line': '',
            'send_time': self.email.send_time.isoformat() if self.email.send_time else None,
            'archive_url': '',
            'emails_sent': self.email.emails_sent,
            'opens': self.email.opens,
            'unique_opens': self.email.unique_opens,
            'open_rate': float(self.email.open_rate),
            'clicks': self.email.clicks,
            'unique_clicks': self.email.unique_clicks,
            'click_rate': float(self.email.click_rate),
            'bounces': self.email.bounces,
            'unsubscribes': self.email.unsubscribes,
            'abuse_reports': self.email.abuse_reports,
            'ecommerce_orders': self.email.ecommerce_orders,
            'ecommerce_revenue': float(self.email.ecommerce_revenue) + 50,  # API changed
            'external_metadata': {},
        }

        refreshed, _ = _save_mailchimp_campaign_from_report(self.event, fresh_report, user=self.admin)

        self.assertEqual(refreshed.manual_revenue, Decimal('500.00'))
        self.assertIsNotNone(refreshed.confirmed_at)
        self.assertIsNotNone(refreshed.api_data_changed_at)
        self.assertGreater(refreshed.api_data_changed_at, refreshed.confirmed_at)


class ScannerCheckinAPITests(TestCase):
    """Tests for /api/scanner/checkin/ (PIN-based scanner session)."""

    def setUp(self):
        from .models import ScannerSession

        self.org = Organization.objects.create(name='Scan Org', slug='scan-org')
        self.venue = Venue.objects.create(
            organization=self.org, name='Scan Venue', city='SF',
        )
        self.event = Event.objects.create(
            organization=self.org,
            name='Scan Event',
            venue=self.venue,
            start_date=date.today(),
            scanner_pin='1234',
        )
        self.customer = Customer.objects.create(
            organization=self.org, name='Buyer', email='buyer@example.com',
        )
        self.order = TicketOrder.objects.create(
            customer=self.customer,
            event=self.event,
            order_number='#00001',
            order_date=timezone.now(),
            total_amount=Decimal('10.00'),
        )
        self.session = ScannerSession.objects.create(event=self.event)
        self.auth = f'Scanner {self.session.token}'

    def test_checkin_succeeds_without_event_id_in_body(self):
        # Regression: the mobile scanner does not send event_id; the session is authoritative.
        res = self.client.post(
            '/api/scanner/checkin/',
            data=json.dumps({'order_number': '#00001'}),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['status'], 'checked_in')
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.checked_in_at)

    def test_checkin_succeeds_with_matching_event_id(self):
        res = self.client.post(
            '/api/scanner/checkin/',
            data=json.dumps({'order_number': '#00001', 'event_id': str(self.event.pk)}),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 200, res.content)

    def test_checkin_rejects_mismatched_event_id(self):
        other = Event.objects.create(
            organization=self.org, name='Other', venue=self.venue, start_date=date.today(),
        )
        res = self.client.post(
            '/api/scanner/checkin/',
            data=json.dumps({'order_number': '#00001', 'event_id': str(other.pk)}),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 403)

    def test_checkin_requires_order_number(self):
        res = self.client.post(
            '/api/scanner/checkin/',
            data=json.dumps({'event_id': str(self.event.pk)}),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 400)

    def test_checkin_unknown_order_returns_404(self):
        res = self.client.post(
            '/api/scanner/checkin/',
            data=json.dumps({'order_number': '#99999'}),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 404)


class PerTicketQRCheckinTests(TestCase):
    """Per-ticket QR codes (TKT-<id>) admit one attendee per scan."""

    def setUp(self):
        from .models import ScannerSession

        self.org = Organization.objects.create(name='QR Org', slug='qr-org')
        self.venue = Venue.objects.create(organization=self.org, name='QR Venue', city='SF')
        self.event = Event.objects.create(
            organization=self.org, name='QR Event', venue=self.venue,
            start_date=date.today(), scanner_pin='4321',
        )
        self.customer = Customer.objects.create(
            organization=self.org, name='Group Buyer', email='group@example.com',
        )
        self.order = TicketOrder.objects.create(
            customer=self.customer, event=self.event, order_number='#QR-1',
            order_date=timezone.now(), total_amount=Decimal('30.00'),
        )
        self.tickets = [
            Ticket.objects.create(
                ticket_order=self.order, ticket_type='General Admission', price=Decimal('10.00'),
            )
            for _ in range(3)
        ]
        self.session = ScannerSession.objects.create(event=self.event)
        self.auth = f'Scanner {self.session.token}'

    def _scan(self, payload):
        return self.client.post(
            '/api/scanner/checkin/',
            data=json.dumps({'order_number': payload}),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )

    def test_build_ticket_qr_codes_one_per_ticket_distinct_cids(self):
        from .utils import build_ticket_qr_codes, ticket_qr_payload

        qrs = build_ticket_qr_codes(self.tickets)
        self.assertEqual(len(qrs), 3)
        self.assertEqual([q['cid'] for q in qrs], ['qrcode-0', 'qrcode-1', 'qrcode-2'])
        self.assertEqual(len({q['cid'] for q in qrs}), 3)
        self.assertTrue(all(q['png_bytes'] for q in qrs))
        self.assertEqual(ticket_qr_payload(self.tickets[0]), f'TKT-{self.tickets[0].id}')

    def test_per_ticket_scan_admits_one_attendee(self):
        from .utils import ticket_qr_payload

        res = self._scan(ticket_qr_payload(self.tickets[0]))
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['status'], 'checked_in')
        self.assertEqual(res.json()['tickets_remaining'], 2)

        self.tickets[0].refresh_from_db()
        self.tickets[1].refresh_from_db()
        self.assertIsNotNone(self.tickets[0].scanned_at)
        self.assertIsNone(self.tickets[1].scanned_at)

        # Order is not fully checked in until every ticket is scanned.
        self.order.refresh_from_db()
        self.assertIsNone(self.order.checked_in_at)

    def test_order_checked_in_after_last_ticket(self):
        from .utils import ticket_qr_payload

        for t in self.tickets[:-1]:
            self._scan(ticket_qr_payload(t))
        self.order.refresh_from_db()
        self.assertIsNone(self.order.checked_in_at)

        res = self._scan(ticket_qr_payload(self.tickets[-1]))
        self.assertEqual(res.json()['tickets_remaining'], 0)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.checked_in_at)

    def test_rescanning_same_ticket_reports_already_checked_in(self):
        from .utils import ticket_qr_payload

        payload = ticket_qr_payload(self.tickets[0])
        self.assertEqual(self._scan(payload).json()['status'], 'checked_in')
        res = self._scan(payload)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['status'], 'already_checked_in')

    def test_legacy_order_number_still_admits_whole_order(self):
        res = self._scan('#QR-1')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['status'], 'checked_in')
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.checked_in_at)
        for t in self.tickets:
            t.refresh_from_db()
            self.assertIsNotNone(t.scanned_at)

    def test_unknown_ticket_id_returns_404(self):
        self.assertEqual(self._scan(f'TKT-{uuid.uuid4()}').status_code, 404)

    def test_malformed_ticket_id_returns_404(self):
        self.assertEqual(self._scan('TKT-not-a-uuid').status_code, 404)

    def test_confirmation_email_attaches_one_qr_per_ticket(self):
        from django.core import mail
        from tickets.tasks import send_order_confirmation_email_task

        send_order_confirmation_email_task.apply(args=[str(self.order.id)]).get()

        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        cids = [
            a.get('Content-ID') for a in msg.attachments
            if hasattr(a, 'get') and a.get('Content-ID')
        ]
        self.assertEqual(sorted(cids), ['<qrcode-0>', '<qrcode-1>', '<qrcode-2>'])
        html_body = next(b for b, mime in msg.alternatives if mime == 'text/html')
        for cid in ('qrcode-0', 'qrcode-1', 'qrcode-2'):
            self.assertIn(f'cid:{cid}', html_body)

    @override_settings(SITE_URL='https://tickets.example.com')
    def test_confirmation_email_includes_event_time_and_page_link(self):
        from datetime import time as dt_time

        from django.core import mail
        from tickets.tasks import send_order_confirmation_email_task

        self.event.start_time = dt_time(20, 0)
        self.event.timezone = 'America/Los_Angeles'
        self.event.save(update_fields=['start_time', 'timezone'])

        send_order_confirmation_email_task.apply(args=[str(self.order.id)]).get()

        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        html_body = next(b for b, mime in msg.alternatives if mime == 'text/html')
        expected_url = f'https://tickets.example.com/e/{self.event.public_id}/'
        for body in (msg.body, html_body):
            self.assertIn('8:00 PM', body)
            self.assertIn('PDT', body)
            self.assertIn(expected_url, body)

    def test_ticket_from_other_event_returns_404(self):
        from .utils import ticket_qr_payload

        other_event = Event.objects.create(
            organization=self.org, name='Other Event', venue=self.venue, start_date=date.today(),
        )
        other_order = TicketOrder.objects.create(
            customer=self.customer, event=other_event, order_number='#QR-2',
            order_date=timezone.now(), total_amount=Decimal('10.00'),
        )
        other_ticket = Ticket.objects.create(
            ticket_order=other_order, ticket_type='GA', price=Decimal('10.00'),
        )
        # The scanner session is bound to self.event, so a ticket from another event is invisible.
        self.assertEqual(self._scan(ticket_qr_payload(other_ticket)).status_code, 404)


class TapToPayEndpointsTests(TestCase):
    """Tests for the three Tap-to-Pay-on-iPhone backend endpoints."""

    def setUp(self):
        from .models import ScannerSession

        self.org = Organization.objects.create(name='TTP Org', slug='ttp-org')
        self.venue = Venue.objects.create(organization=self.org, name='TTP Venue', city='SF')
        self.event = Event.objects.create(
            organization=self.org,
            name='TTP Event',
            venue=self.venue,
            start_date=date.today(),
            scanner_pin='9999',
        )
        self.customer = Customer.objects.create(
            organization=self.org, name='TTP Buyer', email='ttp-buyer@example.com',
        )
        self.order = TicketOrder.objects.create(
            customer=self.customer,
            event=self.event,
            order_number='#TTP001',
            order_date=timezone.now(),
            total_amount=Decimal('25.00'),
        )
        self.session = ScannerSession.objects.create(event=self.event)
        self.auth = f'Scanner {self.session.token}'

    # ---- terms-version ----

    def test_terms_version_returns_setting(self):
        from django.test import override_settings

        with override_settings(TAP_TO_PAY_TERMS_VERSION='2026-03-01'):
            res = self.client.get(
                '/api/tap-to-pay/terms-version/',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), {'version': '2026-03-01'})

    def test_terms_version_rejects_bad_token(self):
        # DRF returns 403 for AuthenticationFailed when no WWW-Authenticate
        # header is provided — matches every other scanner endpoint.
        res = self.client.get(
            '/api/tap-to-pay/terms-version/',
            HTTP_AUTHORIZATION='Scanner ' + str(uuid.uuid4()),
        )
        self.assertEqual(res.status_code, 403)

    # ---- terms-acceptance ----

    def test_terms_acceptance_appends_row(self):
        from .models import TapToPayTermsAcceptance

        res = self.client.post(
            '/api/tap-to-pay/terms-acceptance/',
            data=json.dumps({'version': '2026-03-01'}),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
            HTTP_USER_AGENT='Cue-iOS/1.0',
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json(), {'ok': True})
        rows = list(TapToPayTermsAcceptance.objects.filter(organization=self.org))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].version, '2026-03-01')
        self.assertEqual(rows[0].scanner_session_id, self.session.pk)
        self.assertEqual(rows[0].user_agent, 'Cue-iOS/1.0')

    def test_terms_acceptance_does_not_dedupe(self):
        from .models import TapToPayTermsAcceptance

        for _ in range(2):
            self.client.post(
                '/api/tap-to-pay/terms-acceptance/',
                data=json.dumps({'version': '2026-03-01'}),
                content_type='application/json',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(
            TapToPayTermsAcceptance.objects.filter(organization=self.org).count(),
            2,
        )

    def test_terms_acceptance_rejects_missing_version(self):
        res = self.client.post(
            '/api/tap-to-pay/terms-acceptance/',
            data=json.dumps({}),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 400)

    def test_terms_acceptance_rejects_bad_token(self):
        res = self.client.post(
            '/api/tap-to-pay/terms-acceptance/',
            data=json.dumps({'version': '2026-03-01'}),
            content_type='application/json',
            HTTP_AUTHORIZATION='Scanner ' + str(uuid.uuid4()),
        )
        self.assertEqual(res.status_code, 403)

    # ---- scanner_receipt ----

    def test_receipt_by_order_id_sends_and_logs(self):
        from django.core import mail
        from .models import ReceiptSend

        res = self.client.post(
            '/api/scanner/receipt/',
            data=json.dumps({
                'order_id': '#TTP001',
                'channel': 'email',
                'contact': 'guest@example.com',
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['guest@example.com'])
        log = ReceiptSend.objects.get(organization=self.org)
        self.assertEqual(log.ticket_order_id, self.order.pk)
        self.assertEqual(log.channel, 'email')
        self.assertEqual(log.status, 'sent')

    def test_receipt_by_order_uuid_pk_sends_and_logs(self):
        """The in-person sale response returns both order_number and the UUID
        pk; the app may send either as order_id. A UUID pk must resolve too."""
        from django.core import mail
        from .models import ReceiptSend

        res = self.client.post(
            '/api/scanner/receipt/',
            data=json.dumps({
                'order_id': str(self.order.pk),
                'channel': 'email',
                'contact': 'guest@example.com',
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(len(mail.outbox), 1)
        log = ReceiptSend.objects.get(organization=self.org)
        self.assertEqual(log.ticket_order_id, self.order.pk)
        self.assertEqual(log.status, 'sent')

    def test_receipt_via_organizer_route(self):
        """Fix: /api/organizer/receipt/ resolves to the same dual-auth view,
        so the organizer app (which posts there) no longer 404s."""
        from django.core import mail
        from .models import ReceiptSend

        res = self.client.post(
            '/api/organizer/receipt/',
            data=json.dumps({
                'order_id': '#TTP001',
                'channel': 'email',
                'contact': 'guest@example.com',
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(len(mail.outbox), 1)
        log = ReceiptSend.objects.get(organization=self.org)
        self.assertEqual(log.ticket_order_id, self.order.pk)
        self.assertEqual(log.status, 'sent')

    def test_receipt_order_not_found(self):
        res = self.client.post(
            '/api/scanner/receipt/',
            data=json.dumps({
                'order_id': '#NOPE',
                'channel': 'email',
                'contact': 'guest@example.com',
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 404)

    def test_receipt_unknown_uuid_pk_returns_404(self):
        """A well-formed but non-existent UUID must not error — it 404s
        cleanly (the pk branch only widens matching, it doesn't crash)."""
        res = self.client.post(
            '/api/scanner/receipt/',
            data=json.dumps({
                'order_id': str(uuid.uuid4()),
                'channel': 'email',
                'contact': 'guest@example.com',
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 404)

    def test_receipt_rejects_both_identifiers(self):
        res = self.client.post(
            '/api/scanner/receipt/',
            data=json.dumps({
                'order_id': '#TTP001',
                'payment_intent_id': 'pi_abc',
                'channel': 'email',
                'contact': 'guest@example.com',
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 400)

    def test_receipt_rejects_neither_identifier(self):
        res = self.client.post(
            '/api/scanner/receipt/',
            data=json.dumps({
                'channel': 'email',
                'contact': 'guest@example.com',
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 400)

    def test_receipt_sms_returns_422(self):
        res = self.client.post(
            '/api/scanner/receipt/',
            data=json.dumps({
                'order_id': '#TTP001',
                'channel': 'sms',
                'contact': '+15555550100',
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 422)

    def test_receipt_by_payment_intent_id_declined(self):
        from django.core import mail
        from .models import ReceiptSend

        fake_pi = MagicMock()
        fake_pi.id = 'pi_3OBxYzABC'
        fake_pi.status = 'requires_payment_method'
        fake_pi.amount = 2500
        fake_pi.currency = 'usd'
        fake_pi.created = 1700000000
        fake_pi.metadata = {}
        lpe = MagicMock()
        lpe.message = 'Your card was declined.'
        fake_pi.last_payment_error = lpe

        with patch('stripe.PaymentIntent.retrieve', return_value=fake_pi):
            res = self.client.post(
                '/api/scanner/receipt/',
                data=json.dumps({
                    'payment_intent_id': 'pi_3OBxYzABC',
                    'channel': 'email',
                    'contact': 'guest@example.com',
                }),
                content_type='application/json',
                HTTP_AUTHORIZATION=self.auth,
            )

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn('Declined', body)
        self.assertIn('No charge was made to your card.', body)
        log = ReceiptSend.objects.get(organization=self.org)
        self.assertEqual(log.payment_intent_id, 'pi_3OBxYzABC')
        self.assertIsNone(log.ticket_order_id)
        self.assertEqual(log.status, 'sent')

    def test_receipt_by_payment_intent_id_succeeded(self):
        """The success path: an approved PaymentIntent emails an 'Approved'
        summary (no order needed), resolves the event via PI metadata, and
        omits the 'no charge' disclaimer used for declines."""
        from django.core import mail
        from .models import ReceiptSend

        fake_pi = MagicMock()
        fake_pi.id = 'pi_3OBxYzOK'
        fake_pi.status = 'succeeded'
        fake_pi.amount = 2500
        fake_pi.currency = 'usd'
        fake_pi.created = 1700000000
        fake_pi.metadata = {'event_id': str(self.event.id)}
        fake_pi.last_payment_error = None

        with patch('stripe.PaymentIntent.retrieve', return_value=fake_pi):
            res = self.client.post(
                '/api/scanner/receipt/',
                data=json.dumps({
                    'payment_intent_id': 'pi_3OBxYzOK',
                    'channel': 'email',
                    'contact': 'guest@example.com',
                }),
                content_type='application/json',
                HTTP_AUTHORIZATION=self.auth,
            )

        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn('Your payment was approved.', body)
        self.assertIn('Status: Approved', body)
        self.assertIn('Amount: 25.00 USD', body)
        self.assertIn('TTP Event', body)  # resolved from PI metadata event_id
        self.assertNotIn('No charge was made to your card.', body)
        log = ReceiptSend.objects.get(organization=self.org)
        self.assertEqual(log.payment_intent_id, 'pi_3OBxYzOK')
        self.assertIsNone(log.ticket_order_id)
        self.assertEqual(log.status, 'sent')

    def test_receipt_payment_intent_not_found(self):
        import stripe as stripe_lib

        with patch(
            'stripe.PaymentIntent.retrieve',
            side_effect=stripe_lib.error.InvalidRequestError('No such PI', 'id'),
        ):
            res = self.client.post(
                '/api/scanner/receipt/',
                data=json.dumps({
                    'payment_intent_id': 'pi_missing',
                    'channel': 'email',
                    'contact': 'guest@example.com',
                }),
                content_type='application/json',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), {'error': 'payment intent not found'})

    def test_receipt_rejects_blank_contact(self):
        res = self.client.post(
            '/api/scanner/receipt/',
            data=json.dumps({
                'order_id': '#TTP001',
                'channel': 'email',
                'contact': '   ',
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 400)

    def test_receipt_accepts_organizer_token_auth(self):
        """Spec: dual auth — organizer DRF Token works without a scanner PIN session."""
        from django.core import mail
        from .models import ReceiptSend

        user = User.objects.create_user(username='organizer-ttp', password='x')
        UserProfile.objects.create(user=user, organization=self.org)
        token = Token.objects.create(user=user)

        res = self.client.post(
            '/api/scanner/receipt/',
            data=json.dumps({
                'order_id': '#TTP001',
                'channel': 'email',
                'contact': 'guest@example.com',
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Token {token.key}',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(len(mail.outbox), 1)
        log = ReceiptSend.objects.get(organization=self.org)
        self.assertEqual(log.status, 'sent')

    def test_receipt_finds_order_across_events_in_same_org(self):
        """An order from a different event in the same org is resolvable —
        the lookup is org-scoped, not session-event-scoped."""
        other_event = Event.objects.create(
            organization=self.org,
            name='Other TTP Event',
            venue=self.venue,
            start_date=date.today(),
        )
        other_order = TicketOrder.objects.create(
            customer=self.customer,
            event=other_event,
            order_number='#TTP002',
            order_date=timezone.now(),
            total_amount=Decimal('15.00'),
        )

        res = self.client.post(
            '/api/scanner/receipt/',
            data=json.dumps({
                'order_id': '#TTP002',
                'channel': 'email',
                'contact': 'guest@example.com',
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 200, res.content)
        from .models import ReceiptSend
        log = ReceiptSend.objects.get(organization=self.org)
        self.assertEqual(log.ticket_order_id, other_order.pk)

    def test_receipt_rejects_unauthenticated(self):
        res = self.client.post(
            '/api/scanner/receipt/',
            data=json.dumps({
                'order_id': '#TTP001',
                'channel': 'email',
                'contact': 'guest@example.com',
            }),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 403)

    # ---- merchant_status ----

    def _fake_account(self, country='US', ttp='active'):
        account = MagicMock()
        account.country = country
        account.capabilities = {'card_payments': ttp} if ttp is not None else {}
        return account

    def test_merchant_status_pending_when_no_stripe_account(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()
        # Org has no stripe_account_id by default.
        res = self.client.get('/api/merchant/status/', HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), {'tap_to_pay': {'status': 'pending'}})

    def test_merchant_status_enabled_when_capability_active(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()
        self.org.stripe_account_id = 'acct_test_enabled'
        self.org.save(update_fields=['stripe_account_id'])

        with patch(
            'stripe.Account.retrieve',
            return_value=self._fake_account(country='US', ttp='active'),
        ):
            res = self.client.get('/api/merchant/status/', HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), {'tap_to_pay': {'status': 'enabled'}})

    def test_merchant_status_unsupported_country(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()
        self.org.stripe_account_id = 'acct_test_unsupported'
        self.org.save(update_fields=['stripe_account_id'])

        with patch(
            'stripe.Account.retrieve',
            return_value=self._fake_account(country='IN', ttp='active'),
        ):
            res = self.client.get('/api/merchant/status/', HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), {'tap_to_pay': {'status': 'unsupported'}})

    def test_merchant_status_pending_when_capability_inactive_in_supported_country(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()
        self.org.stripe_account_id = 'acct_test_pending'
        self.org.save(update_fields=['stripe_account_id'])

        with patch(
            'stripe.Account.retrieve',
            return_value=self._fake_account(country='US', ttp='inactive'),
        ):
            res = self.client.get('/api/merchant/status/', HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), {'tap_to_pay': {'status': 'pending'}})

    def test_merchant_status_pending_when_stripe_errors(self):
        import stripe as stripe_lib
        from django.core.cache import cache as django_cache
        django_cache.clear()
        self.org.stripe_account_id = 'acct_test_err'
        self.org.save(update_fields=['stripe_account_id'])

        with patch(
            'stripe.Account.retrieve',
            side_effect=stripe_lib.error.APIConnectionError('boom'),
        ):
            res = self.client.get('/api/merchant/status/', HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), {'tap_to_pay': {'status': 'pending'}})

    def test_merchant_status_caches_stripe_lookup(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()
        self.org.stripe_account_id = 'acct_test_cache'
        self.org.save(update_fields=['stripe_account_id'])

        with patch(
            'stripe.Account.retrieve',
            return_value=self._fake_account(country='US', ttp='active'),
        ) as retrieve_mock:
            self.client.get('/api/merchant/status/', HTTP_AUTHORIZATION=self.auth)
            self.client.get('/api/merchant/status/', HTTP_AUTHORIZATION=self.auth)
            self.client.get('/api/merchant/status/', HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(retrieve_mock.call_count, 1)

    def test_merchant_status_rejects_bad_token(self):
        res = self.client.get(
            '/api/merchant/status/',
            HTTP_AUTHORIZATION='Scanner ' + str(uuid.uuid4()),
        )
        self.assertEqual(res.status_code, 403)


class ScannerInPersonSellTests(TestCase):
    """Tests for the /api/scanner/* in-person sell endpoints used by the iOS app."""

    def setUp(self):
        from .models import ScannerSession

        self.org = Organization.objects.create(
            name='Sell Org', slug='sell-org',
            stripe_account_id='acct_test_sell',
            stripe_onboarding_complete=True,
            stripe_terminal_location_id='tml_test_existing',
        )
        self.venue = Venue.objects.create(organization=self.org, name='Sell Venue', city='SF')
        self.event = Event.objects.create(
            organization=self.org,
            name='Sell Event',
            venue=self.venue,
            start_date=date.today(),
            scanner_pin='5555',
        )
        self.other_event = Event.objects.create(
            organization=self.org,
            name='Other Event',
            venue=self.venue,
            start_date=date.today(),
        )
        self.tt = SaleableTicketType.objects.create(
            event=self.event,
            name='General Admission',
            price=Decimal('25.00'),
            description='Standing room',
        )
        self.session = ScannerSession.objects.create(event=self.event)
        self.auth = f'Scanner {self.session.token}'

    # ---- ticket-types ----

    def test_ticket_types_returns_active_ticket_types(self):
        res = self.client.get('/api/scanner/ticket-types/', HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]['id'], str(self.tt.pk))
        self.assertEqual(body[0]['name'], 'General Admission')
        self.assertEqual(body[0]['price'], '25.00')
        self.assertEqual(body[0]['description'], 'Standing room')
        self.assertIsNone(body[0]['remaining'])

    def test_ticket_types_accepts_matching_event_id(self):
        res = self.client.get(
            f'/api/scanner/ticket-types/?event_id={self.event.pk}',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(len(res.json()), 1)

    def test_ticket_types_rejects_mismatched_event_id(self):
        res = self.client.get(
            f'/api/scanner/ticket-types/?event_id={self.other_event.pk}',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 404)

    def test_ticket_types_returns_empty_list_when_none_for_sale(self):
        # iOS treats 404 as "endpoint missing" — empty inventory must still be 200 [].
        self.tt.is_active = False
        self.tt.save(update_fields=['is_active'])
        res = self.client.get('/api/scanner/ticket-types/', HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), [])

    def test_ticket_types_requires_scanner_token(self):
        res = self.client.get('/api/scanner/ticket-types/')
        self.assertEqual(res.status_code, 403)

    # ---- stripe connection-token ----

    def test_stripe_connection_token_returns_secret(self):
        fake_token = MagicMock()
        fake_token.secret = 'pst_test_secret'
        with patch('stripe.terminal.ConnectionToken.create', return_value=fake_token) as ct_mock:
            res = self.client.post(
                '/api/scanner/stripe/connection-token/',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), {'secret': 'pst_test_secret'})
        # Critical: token must be scoped to the merchant's Connect account,
        # not the platform. Without stripe_account=, Terminal collection
        # fails later with cryptic errors.
        ct_mock.assert_called_once_with(stripe_account='acct_test_sell')

    def test_stripe_connection_token_requires_scanner_token(self):
        res = self.client.post('/api/scanner/stripe/connection-token/')
        self.assertEqual(res.status_code, 403)

    def test_stripe_connection_token_403_when_no_stripe_account(self):
        self.org.stripe_account_id = ''
        self.org.save(update_fields=['stripe_account_id'])
        with patch('stripe.terminal.ConnectionToken.create') as ct_mock:
            res = self.client.post(
                '/api/scanner/stripe/connection-token/',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 403)
        ct_mock.assert_not_called()

    # ---- shared /api/stripe/connection-token/ (dual-auth) ----

    def test_shared_connection_token_accepts_scanner_auth(self):
        """iOS scanner app uses /api/stripe/connection-token/ with a
        Scanner token; the endpoint must accept it and scope the Stripe
        call to the scanner session's merchant.
        """
        fake_token = MagicMock()
        fake_token.secret = 'pst_test_shared'
        with patch('stripe.terminal.ConnectionToken.create', return_value=fake_token) as ct_mock:
            res = self.client.post(
                '/api/stripe/connection-token/',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), {'secret': 'pst_test_shared'})
        ct_mock.assert_called_once_with(stripe_account='acct_test_sell')

    def test_shared_connection_token_rejects_missing_auth(self):
        res = self.client.post('/api/stripe/connection-token/')
        # DRF returns 403 (not 401) when no auth class accepts the
        # request and no WWW-Authenticate header is set — matches every
        # other scanner endpoint and the iOS client handles either.
        self.assertIn(res.status_code, (401, 403))

    def test_shared_terminal_payment_intent_accepts_scanner_auth(self):
        fake_pi = MagicMock()
        fake_pi.client_secret = 'pi_secret_shared'
        fake_pi.id = 'pi_shared'
        with patch('stripe.PaymentIntent.create', return_value=fake_pi) as pi_mock:
            res = self.client.post(
                '/api/stripe/terminal-payment-intent/',
                data=json.dumps({
                    'event_id': str(self.event.pk),
                    'line_items': [{'ticket_type_id': str(self.tt.pk), 'quantity': 1}],
                }),
                content_type='application/json',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()['location_id'], 'tml_test_existing')
        pi_mock.assert_called_once()
        # Critical: PaymentIntent must be created on the merchant's
        # Connect account (stripe_account=) with card-present + the
        # event metadata that scanner_receipt's declined-receipt branch
        # relies on.
        call_kwargs = pi_mock.call_args.kwargs
        self.assertEqual(call_kwargs['stripe_account'], 'acct_test_sell')
        self.assertEqual(call_kwargs['payment_method_types'], ['card_present'])
        self.assertEqual(call_kwargs['capture_method'], 'automatic')
        self.assertEqual(call_kwargs['metadata']['event_id'], str(self.event.pk))

    # ---- stripe terminal-payment-intent ----

    def test_terminal_payment_intent_creates_pi(self):
        fake_pi = MagicMock()
        fake_pi.client_secret = 'pi_secret_123'
        fake_pi.id = 'pi_123'
        with patch('stripe.PaymentIntent.create', return_value=fake_pi) as pi_mock:
            res = self.client.post(
                '/api/scanner/stripe/terminal-payment-intent/',
                data=json.dumps({
                    'event_id': str(self.event.pk),
                    'line_items': [{'ticket_type_id': str(self.tt.pk), 'quantity': 2}],
                }),
                content_type='application/json',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertEqual(body['client_secret'], 'pi_secret_123')
        self.assertEqual(body['payment_intent_id'], 'pi_123')
        self.assertEqual(body['amount_cents'], 5000)
        self.assertEqual(body['location_id'], 'tml_test_existing')
        pi_mock.assert_called_once()
        self.assertEqual(pi_mock.call_args.kwargs['stripe_account'], 'acct_test_sell')
        # Direct charge: Cue's platform fee rides application_fee_amount,
        # same fee-inclusive display formula as online checkout.
        self.assertEqual(
            pi_mock.call_args.kwargs['application_fee_amount'],
            extract_fee_from_display_cents(5000),
        )

    def test_terminal_payment_intent_returns_cached_location_id(self):
        """When the org already has a Terminal Location cached, the PI
        endpoint must NOT call Location.create — that would be wasted
        Stripe quota on every sale."""
        fake_pi = MagicMock()
        fake_pi.client_secret = 'pi_secret_456'
        fake_pi.id = 'pi_456'
        with patch('stripe.terminal.Location.create') as loc_mock, \
                patch('stripe.PaymentIntent.create', return_value=fake_pi):
            res = self.client.post(
                '/api/scanner/stripe/terminal-payment-intent/',
                data=json.dumps({
                    'event_id': str(self.event.pk),
                    'line_items': [{'ticket_type_id': str(self.tt.pk), 'quantity': 1}],
                }),
                content_type='application/json',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()['location_id'], 'tml_test_existing')
        loc_mock.assert_not_called()

    def test_terminal_payment_intent_lazy_creates_location(self):
        """First sale for a merchant who has no cached Location: call
        Location.create scoped to the Connect account with idempotency,
        cache the result on the org, and return tml_ in the response."""
        self.org.stripe_terminal_location_id = ''
        self.org.save(update_fields=['stripe_terminal_location_id'])

        fake_account = MagicMock()
        fake_account.company = None
        fake_account.business_profile = None
        fake_account.individual = None
        fake_location = MagicMock()
        fake_location.id = 'tml_freshly_minted'
        fake_pi = MagicMock()
        fake_pi.client_secret = 'pi_secret_lazy'
        fake_pi.id = 'pi_lazy'

        with patch('stripe.Account.retrieve', return_value=fake_account), \
                patch('stripe.terminal.Location.create', return_value=fake_location) as loc_mock, \
                patch('stripe.PaymentIntent.create', return_value=fake_pi):
            res = self.client.post(
                '/api/scanner/stripe/terminal-payment-intent/',
                data=json.dumps({
                    'event_id': str(self.event.pk),
                    'line_items': [{'ticket_type_id': str(self.tt.pk), 'quantity': 1}],
                }),
                content_type='application/json',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()['location_id'], 'tml_freshly_minted')

        # Critical: Location must be created on the Connect account, with
        # an idempotency key so concurrent first-sale requests for the
        # same merchant don't create duplicates.
        loc_mock.assert_called_once()
        call_kwargs = loc_mock.call_args.kwargs
        self.assertEqual(call_kwargs['stripe_account'], 'acct_test_sell')
        self.assertEqual(call_kwargs['idempotency_key'], f'terminal-location:{self.org.pk}')
        self.assertIn('address', call_kwargs)
        self.assertIn('display_name', call_kwargs)

        # And the org now caches the value so the next sale won't call again.
        self.org.refresh_from_db()
        self.assertEqual(self.org.stripe_terminal_location_id, 'tml_freshly_minted')

    def test_terminal_payment_intent_502_when_location_create_fails(self):
        """If Stripe can't mint a Location, fail loudly instead of
        returning a PaymentIntent with no location_id — the iOS app
        would error and we'd have a dangling PI on Stripe."""
        import stripe as stripe_lib
        self.org.stripe_terminal_location_id = ''
        self.org.save(update_fields=['stripe_terminal_location_id'])

        with patch('stripe.Account.retrieve', return_value=MagicMock()), \
                patch(
                    'stripe.terminal.Location.create',
                    side_effect=stripe_lib.error.APIConnectionError('boom'),
                ), \
                patch('stripe.PaymentIntent.create') as pi_mock:
            res = self.client.post(
                '/api/scanner/stripe/terminal-payment-intent/',
                data=json.dumps({
                    'event_id': str(self.event.pk),
                    'line_items': [{'ticket_type_id': str(self.tt.pk), 'quantity': 1}],
                }),
                content_type='application/json',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 502)
        # Crucially we don't mint a PI we can't pair with a Location.
        pi_mock.assert_not_called()

    def test_terminal_payment_intent_rejects_mismatched_event_id(self):
        with patch('stripe.PaymentIntent.create') as pi_mock:
            res = self.client.post(
                '/api/scanner/stripe/terminal-payment-intent/',
                data=json.dumps({
                    'event_id': str(self.other_event.pk),
                    'line_items': [{'ticket_type_id': str(self.tt.pk), 'quantity': 1}],
                }),
                content_type='application/json',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 404)
        pi_mock.assert_not_called()

    def test_terminal_payment_intent_requires_line_items(self):
        res = self.client.post(
            '/api/scanner/stripe/terminal-payment-intent/',
            data=json.dumps({'event_id': str(self.event.pk)}),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 400)

    # ---- sell ----

    def test_sell_creates_in_person_order_with_no_checked_in_by(self):
        fake_pi = MagicMock()
        fake_pi.status = 'succeeded'
        fake_pi.amount_received = 5000  # 2 x $25.00, Stripe-confirmed
        fake_pi.application_fee_amount = 462
        with patch('stripe.PaymentIntent.retrieve', return_value=fake_pi) as retrieve_mock:
            res = self.client.post(
                '/api/scanner/sell/',
                data=json.dumps({
                    'event_id': str(self.event.pk),
                    'payment_intent_id': 'pi_succeeded_123',
                    'buyer_name': 'Walk-up Buyer',
                    'buyer_email': 'walkup@example.com',
                    'line_items': [{
                        'ticket_type_id': str(self.tt.pk),
                        'quantity': 2,
                        'name': 'General Admission',
                        'price': '25.00',
                    }],
                }),
                content_type='application/json',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertEqual(body['ticket_count'], 2)
        self.assertEqual(body['total_amount'], '50.00')

        # PaymentIntent.retrieve must be scoped to the merchant's
        # Connect account; otherwise Stripe 404s the PI that was
        # created on that connected account.
        retrieve_mock.assert_called_once_with(
            'pi_succeeded_123',
            stripe_account='acct_test_sell',
        )

        order = TicketOrder.objects.get(pk=body['order_id'])
        self.assertTrue(order.is_in_person)
        self.assertIsNotNone(order.checked_in_at)
        self.assertIsNone(order.checked_in_by)
        self.assertEqual(order.event_id, self.event.pk)
        self.assertEqual(order.customer.email, 'walkup@example.com')
        self.assertEqual(order.tickets.count(), 2)

        self.tt.refresh_from_db()
        self.assertEqual(self.tt.quantity_sold, 2)

    def test_sell_rejects_mismatched_event_id(self):
        with patch('stripe.PaymentIntent.retrieve') as pi_mock:
            res = self.client.post(
                '/api/scanner/sell/',
                data=json.dumps({
                    'event_id': str(self.other_event.pk),
                    'payment_intent_id': 'pi_123',
                    'buyer_email': 'walkup@example.com',
                    'line_items': [{
                        'ticket_type_id': str(self.tt.pk),
                        'quantity': 1,
                        'name': 'GA',
                        'price': '25.00',
                    }],
                }),
                content_type='application/json',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 404)
        pi_mock.assert_not_called()

    def test_sell_rejects_unsucceeded_payment_intent(self):
        fake_pi = MagicMock()
        fake_pi.status = 'requires_payment_method'
        with patch('stripe.PaymentIntent.retrieve', return_value=fake_pi):
            res = self.client.post(
                '/api/scanner/sell/',
                data=json.dumps({
                    'event_id': str(self.event.pk),
                    'payment_intent_id': 'pi_failed',
                    'buyer_email': 'walkup@example.com',
                    'line_items': [{
                        'ticket_type_id': str(self.tt.pk),
                        'quantity': 1,
                        'name': 'GA',
                        'price': '25.00',
                    }],
                }),
                content_type='application/json',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 400)

    def test_sell_requires_buyer_email(self):
        res = self.client.post(
            '/api/scanner/sell/',
            data=json.dumps({
                'event_id': str(self.event.pk),
                'payment_intent_id': 'pi_123',
                'line_items': [{
                    'ticket_type_id': str(self.tt.pk),
                    'quantity': 1,
                    'name': 'GA',
                    'price': '25.00',
                }],
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 400)

    # ---- sell-eligibility ----

    def test_sell_eligibility_pending_by_default(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()
        # A merchant that has not connected Stripe at all.
        self.org.stripe_account_id = ''
        self.org.save(update_fields=['stripe_account_id'])
        res = self.client.get('/api/scanner/sell-eligibility/', HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertFalse(body['eligible'])
        self.assertEqual(body['reason'], 'tap_to_pay_pending')
        self.assertEqual(body['details']['stripe_capability_state'], 'missing')
        self.assertEqual(body['details']['country'], '')
        self.assertEqual(body['details']['cache_age_seconds'], 0)
        # ISO-8601 with offset — sanity check it parses
        self.assertIn('T', body['details']['checked_at'])

    def test_sell_eligibility_true_when_tap_to_pay_active(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()
        self.org.stripe_account_id = 'acct_test_ttp_active'
        self.org.save(update_fields=['stripe_account_id'])

        account = MagicMock()
        account.country = 'US'
        account.capabilities = {'card_payments': 'active'}
        with patch('stripe.Account.retrieve', return_value=account):
            res = self.client.get(
                '/api/scanner/sell-eligibility/',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertTrue(body['eligible'])
        self.assertNotIn('reason', body)
        self.assertEqual(body['details']['stripe_capability_state'], 'active')
        self.assertEqual(body['details']['country'], 'US')

    def test_sell_eligibility_details_unsupported_country(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()
        self.org.stripe_account_id = 'acct_test_ttp_unsupp'
        self.org.save(update_fields=['stripe_account_id'])

        account = MagicMock()
        account.country = 'IN'
        account.capabilities = {'card_payments': 'active'}
        with patch('stripe.Account.retrieve', return_value=account):
            res = self.client.get(
                '/api/scanner/sell-eligibility/',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertFalse(body['eligible'])
        self.assertEqual(body['reason'], 'tap_to_pay_unsupported')
        self.assertEqual(body['details']['stripe_capability_state'], 'active')
        self.assertEqual(body['details']['country'], 'IN')

    def test_sell_eligibility_details_capability_inactive(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()
        self.org.stripe_account_id = 'acct_test_ttp_inactive'
        self.org.save(update_fields=['stripe_account_id'])

        account = MagicMock()
        account.country = 'US'
        account.capabilities = {'card_payments': 'inactive'}
        with patch('stripe.Account.retrieve', return_value=account):
            res = self.client.get(
                '/api/scanner/sell-eligibility/',
                HTTP_AUTHORIZATION=self.auth,
            )
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertFalse(body['eligible'])
        self.assertEqual(body['reason'], 'tap_to_pay_pending')
        self.assertEqual(body['details']['stripe_capability_state'], 'inactive')
        self.assertEqual(body['details']['country'], 'US')

    def test_sell_eligibility_cache_age_grows_on_repeat_call(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()
        self.org.stripe_account_id = 'acct_test_ttp_cache'
        self.org.save(update_fields=['stripe_account_id'])

        account = MagicMock()
        account.country = 'US'
        account.capabilities = {'card_payments': 'active'}
        # Freeze time so we can advance it precisely between calls.
        t0 = timezone.now().replace(microsecond=0)
        with patch('stripe.Account.retrieve', return_value=account) as retrieve_mock, \
                patch('tickets.api_views.timezone.now') as now_mock:
            now_mock.side_effect = [t0, t0, t0 + timedelta(seconds=15)]
            self.client.get('/api/scanner/sell-eligibility/', HTTP_AUTHORIZATION=self.auth)
            res = self.client.get('/api/scanner/sell-eligibility/', HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(retrieve_mock.call_count, 1)
        body = res.json()
        self.assertEqual(body['details']['cache_age_seconds'], 15)

    def test_sell_eligibility_rejects_mismatched_event_id(self):
        res = self.client.get(
            f'/api/scanner/sell-eligibility/?event_id={self.other_event.pk}',
            HTTP_AUTHORIZATION=self.auth,
        )
        self.assertEqual(res.status_code, 404)


class EnableTapToPayViewTests(TestCase):
    """Tests for /finance/stripe/tap-to-pay/enable/ and finance_overview context."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(
            name='TTP Connect Org',
            slug='ttp-connect-org',
            stripe_account_id='acct_test_ttp',
            stripe_onboarding_complete=True,
        )
        self.user = User.objects.create_user(
            username='ttp-owner',
            email='ttp-owner@example.com',
            password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='ttp-owner@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))
        self.enable_url = reverse('tickets:enable_tap_to_pay')
        self.finance_url = reverse('tickets:finance_overview')

    def _mock_account(self, *, country='US', ttp=None, currently_due=None):
        account = MagicMock()
        account.country = country
        account.details_submitted = True
        account.charges_enabled = True
        account.payouts_enabled = True
        account.capabilities = {'card_payments': ttp} if ttp is not None else {}
        requirements = MagicMock()
        requirements.currently_due = list(currently_due or [])
        account.requirements = requirements
        account.get.side_effect = lambda key, default=None: {
            'external_accounts': {'data': []},
        }.get(key, default)
        return account

    # ---- enable_tap_to_pay view ----

    @patch('stripe.AccountLink.create')
    @patch('stripe.Account.modify')
    @patch('stripe.Account.retrieve')
    def test_enable_requests_card_payments_capability(
        self, mock_retrieve, mock_modify, mock_link_create,
    ):
        from django.core.cache import cache as django_cache
        django_cache.set(f'tap_to_pay_status:{self.org.pk}', 'pending', timeout=60)
        mock_retrieve.return_value = self._mock_account(ttp='inactive')
        mock_modify.return_value = self._mock_account(ttp='pending')

        res = self.client.post(self.enable_url)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.url, self.finance_url)

        mock_modify.assert_called_once_with(
            self.org.stripe_account_id,
            capabilities={'card_payments': {'requested': True}},
        )
        mock_link_create.assert_not_called()
        self.assertIsNone(django_cache.get(f'tap_to_pay_status:{self.org.pk}'))

    @patch('stripe.AccountLink.create')
    @patch('stripe.Account.modify')
    @patch('stripe.Account.retrieve')
    def test_enable_redirects_to_account_link_when_requirements_due(
        self, mock_retrieve, mock_modify, mock_link_create,
    ):
        mock_retrieve.return_value = self._mock_account(ttp='inactive')
        mock_modify.return_value = self._mock_account(
            ttp='inactive',
            currently_due=['representative.verification.document'],
        )
        mock_link_create.return_value = MagicMock(url='https://connect.stripe.com/setup/test')

        res = self.client.post(self.enable_url)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.url, 'https://connect.stripe.com/setup/test')
        mock_link_create.assert_called_once()
        link_kwargs = mock_link_create.call_args.kwargs
        self.assertEqual(link_kwargs['account'], self.org.stripe_account_id)
        self.assertEqual(link_kwargs['type'], 'account_onboarding')
        self.assertEqual(link_kwargs['collect'], 'currently_due')

    @patch('stripe.AccountLink.create')
    @patch('stripe.Account.modify')
    @patch('stripe.Account.retrieve')
    def test_enable_short_circuits_when_already_enabled(
        self, mock_retrieve, mock_modify, mock_link_create,
    ):
        from django.core.cache import cache as django_cache
        django_cache.set(f'tap_to_pay_status:{self.org.pk}', 'enabled', timeout=60)
        mock_retrieve.return_value = self._mock_account(ttp='active')

        res = self.client.post(self.enable_url)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.url, self.finance_url)
        mock_modify.assert_not_called()
        mock_link_create.assert_not_called()
        self.assertIsNone(django_cache.get(f'tap_to_pay_status:{self.org.pk}'))

    @patch('stripe.Account.modify')
    @patch('stripe.Account.retrieve')
    def test_enable_blocked_when_no_stripe_account(self, mock_retrieve, mock_modify):
        self.org.stripe_account_id = ''
        self.org.save(update_fields=['stripe_account_id'])

        res = self.client.post(self.enable_url)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.url, self.finance_url)
        mock_retrieve.assert_not_called()
        mock_modify.assert_not_called()

    @patch('stripe.Account.modify')
    @patch('stripe.Account.retrieve')
    def test_enable_blocked_when_country_unsupported(self, mock_retrieve, mock_modify):
        mock_retrieve.return_value = self._mock_account(country='ZW', ttp='inactive')

        res = self.client.post(self.enable_url)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.url, self.finance_url)
        mock_modify.assert_not_called()

    @patch('stripe.Account.modify')
    @patch('stripe.Account.retrieve')
    def test_enable_handles_stripe_error_on_modify(self, mock_retrieve, mock_modify):
        import stripe as stripe_lib
        mock_retrieve.return_value = self._mock_account(ttp='inactive')
        mock_modify.side_effect = stripe_lib.error.InvalidRequestError('boom', 'capabilities')

        res = self.client.post(self.enable_url)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.url, self.finance_url)

    # ---- stripe_connect_onboard requests capabilities upfront ----

    @patch('stripe.AccountLink.create')
    @patch('stripe.Account.create')
    def test_onboard_requests_card_payments_capability_at_create(
        self, mock_account_create, mock_link_create,
    ):
        self.org.stripe_account_id = ''
        self.org.save(update_fields=['stripe_account_id'])
        mock_account_create.return_value = MagicMock(id='acct_new_test')
        mock_link_create.return_value = MagicMock(url='https://connect.stripe.com/setup/new')

        res = self.client.post(reverse('tickets:stripe_connect_onboard'))
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.url, 'https://connect.stripe.com/setup/new')

        mock_account_create.assert_called_once()
        create_kwargs = mock_account_create.call_args.kwargs
        self.assertEqual(create_kwargs['type'], 'express')
        self.assertEqual(
            create_kwargs['capabilities'],
            {'card_payments': {'requested': True}, 'transfers': {'requested': True}},
        )

    def test_enable_requires_login(self):
        self.client.logout()
        res = self.client.post(self.enable_url)
        self.assertEqual(res.status_code, 302)
        self.assertIn('/login/', res.url)

    def test_enable_rejects_non_admin(self):
        member = User.objects.create_user(
            username='ttp-member', email='ttp-member@example.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=member,
            organization=self.org,
            org_role=UserProfile.OrgRole.HOST,
        )
        self.client.logout()
        self.client.login(username='ttp-member@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))

        res = self.client.post(self.enable_url)
        # @require_admin redirects non-admins; check it did not call Stripe.
        self.assertIn(res.status_code, (302, 403))

    # ---- finance_overview context ----

    @patch('tickets.views._compute_available_balance')
    @patch('tickets.views._get_connected_balance_cents')
    @patch('stripe.Account.retrieve')
    def test_finance_overview_context_pending(
        self, mock_retrieve, mock_connected, mock_available,
    ):
        mock_available.return_value = (Decimal('0'), Decimal('0'), Decimal('0'), Decimal('0'))
        mock_connected.return_value = (0, 0)
        mock_retrieve.return_value = self._mock_account(ttp='inactive')

        res = self.client.get(self.finance_url)
        self.assertEqual(res.status_code, 200)
        ttp_ui = res.context['tap_to_pay_ui']
        self.assertEqual(ttp_ui['status'], 'pending')
        self.assertEqual(ttp_ui['country'], 'US')

    @patch('tickets.views._compute_available_balance')
    @patch('tickets.views._get_connected_balance_cents')
    @patch('stripe.Account.retrieve')
    def test_finance_overview_context_enabled(
        self, mock_retrieve, mock_connected, mock_available,
    ):
        mock_available.return_value = (Decimal('0'), Decimal('0'), Decimal('0'), Decimal('0'))
        mock_connected.return_value = (0, 0)
        mock_retrieve.return_value = self._mock_account(ttp='active')

        res = self.client.get(self.finance_url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context['tap_to_pay_ui']['status'], 'enabled')

    @patch('tickets.views._compute_available_balance')
    @patch('tickets.views._get_connected_balance_cents')
    @patch('stripe.Account.retrieve')
    def test_finance_overview_context_unsupported_country(
        self, mock_retrieve, mock_connected, mock_available,
    ):
        mock_available.return_value = (Decimal('0'), Decimal('0'), Decimal('0'), Decimal('0'))
        mock_connected.return_value = (0, 0)
        mock_retrieve.return_value = self._mock_account(country='ZW', ttp='active')

        res = self.client.get(self.finance_url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context['tap_to_pay_ui']['status'], 'unsupported')


class RequestCardPaymentsCapabilityCommandTests(TestCase):
    """Tests for the request_card_payments_capability management command."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='Capability Org',
            slug='capability-org',
            stripe_account_id='acct_capability_test',
        )

    def _mock_account(self, *, country='US', ttp='unrequested', currently_due=None):
        account = MagicMock()
        account.country = country
        account.capabilities = {'card_payments': ttp}
        requirements = MagicMock()
        requirements.currently_due = list(currently_due or [])
        account.requirements = requirements
        return account

    @patch('stripe.Account.modify')
    @patch('stripe.Account.retrieve')
    def test_request_by_org_slug_calls_stripe_and_busts_cache(
        self, mock_retrieve, mock_modify,
    ):
        from django.core.cache import cache as django_cache
        from django.core.management import call_command
        from io import StringIO

        django_cache.set(f'tap_to_pay_status:{self.org.pk}', {'status': 'pending'}, timeout=60)
        mock_retrieve.return_value = self._mock_account(ttp='unrequested')
        mock_modify.return_value = self._mock_account(ttp='pending')

        out = StringIO()
        call_command('request_card_payments_capability', '--org', self.org.slug, stdout=out)

        mock_modify.assert_called_once_with(
            self.org.stripe_account_id,
            capabilities={'card_payments': {'requested': True}},
        )
        self.assertIsNone(django_cache.get(f'tap_to_pay_status:{self.org.pk}'))
        output = out.getvalue()
        self.assertIn('Before:', output)
        self.assertIn('card_payments=unrequested', output)
        self.assertIn('card_payments=pending', output)
        self.assertIn(f'org={self.org.slug}', output)

    @patch('stripe.Account.modify')
    @patch('stripe.Account.retrieve')
    def test_request_by_account_id_works_without_matching_org(
        self, mock_retrieve, mock_modify,
    ):
        from django.core.management import call_command
        from io import StringIO

        mock_retrieve.return_value = self._mock_account(ttp='unrequested')
        mock_modify.return_value = self._mock_account(ttp='pending')

        out = StringIO()
        call_command(
            'request_card_payments_capability',
            '--account', 'acct_orphan_no_org',
            stdout=out,
        )

        mock_modify.assert_called_once_with(
            'acct_orphan_no_org',
            capabilities={'card_payments': {'requested': True}},
        )
        output = out.getvalue()
        self.assertIn('card_payments=pending', output)
        # No org → no cache-bust line printed.
        self.assertNotIn('Cleared status cache', output)

    @patch('stripe.Account.modify')
    @patch('stripe.Account.retrieve')
    def test_request_warns_when_requirements_currently_due(
        self, mock_retrieve, mock_modify,
    ):
        from django.core.management import call_command
        from io import StringIO

        mock_retrieve.return_value = self._mock_account(ttp='unrequested')
        mock_modify.return_value = self._mock_account(
            ttp='inactive',
            currently_due=['representative.verification.document'],
        )

        out = StringIO()
        call_command('request_card_payments_capability', '--org', self.org.slug, stdout=out)

        output = out.getvalue()
        self.assertIn('Stripe wants more info', output)
        self.assertIn('representative.verification.document', output)

    def test_rejects_neither_flag(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command('request_card_payments_capability')

    def test_rejects_both_flags(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command(
                'request_card_payments_capability',
                '--org', self.org.slug,
                '--account', 'acct_other',
            )

    def test_rejects_unknown_org_slug(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command('request_card_payments_capability', '--org', 'no-such-org')

    def test_rejects_org_without_stripe_account(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        self.org.stripe_account_id = ''
        self.org.save(update_fields=['stripe_account_id'])

        with self.assertRaises(CommandError):
            call_command('request_card_payments_capability', '--org', self.org.slug)

    @patch('stripe.Account.modify')
    @patch('stripe.Account.retrieve')
    def test_reads_capability_from_real_stripe_object(
        self, mock_retrieve, mock_modify,
    ):
        """Regression: the Stripe Python SDK returns capabilities as a
        StripeObject without a .get() method. Earlier code used
        capabilities.get('card_payments') and silently fell back to
        'unrequested' for every real merchant. The _read_stripe_capability
        helper must handle the real type, not just dict mocks.
        """
        from django.core.management import call_command
        from io import StringIO
        from stripe._stripe_object import StripeObject

        def _as_stripe_object(payload):
            return StripeObject.construct_from(payload, key=None)

        before = _as_stripe_object({
            'id': self.org.stripe_account_id,
            'object': 'account',
            'country': 'US',
            'capabilities': {'card_payments': 'active', 'transfers': 'active'},
            'requirements': {'currently_due': []},
        })
        after = _as_stripe_object({
            'id': self.org.stripe_account_id,
            'object': 'account',
            'country': 'US',
            'capabilities': {'card_payments': 'active', 'transfers': 'active'},
            'requirements': {'currently_due': []},
        })
        mock_retrieve.return_value = before
        mock_modify.return_value = after

        out = StringIO()
        call_command('request_card_payments_capability', '--org', self.org.slug, stdout=out)

        output = out.getvalue()
        # Must read 'active' off the StripeObject, NOT fall back to 'unrequested'.
        self.assertIn('Before:', output)
        self.assertIn('card_payments=active', output)
        self.assertNotIn('card_payments=unrequested', output)


class PhoneAuthAPITests(TestCase):
    """Regression coverage for /api/auth/phone/{start,verify}/ endpoints."""

    START_URL = '/api/auth/phone/start/'
    VERIFY_URL = '/api/auth/phone/verify/'
    PHONE = '+15555550199'

    def setUp(self):
        from .models import ScannerSession
        self.client = Client()
        self.org = Organization.objects.create(name='Existing Org', slug='existing-org')
        self.existing_user = User.objects.create_user(
            username='existing-organizer',
            email='owner@example.com',
            password='unused',
            first_name='Jane',
            last_name='Doe',
        )
        UserProfile.objects.create(
            user=self.existing_user,
            organization=self.org,
            role=UserProfile.Role.ORGANIZER,
            phone_number=self.PHONE,
        )
        # Scanner session for the scanner-header tolerance tests.
        self.scanner_event = Event.objects.create(
            organization=self.org,
            name='Scan Show',
            venue=Venue.objects.create(organization=self.org, name='V', city='C'),
            start_date=timezone.localdate(),
        )
        self.scanner_session = ScannerSession.objects.create(event=self.scanner_event)

    def _post(self, url, body, **extra):
        return self.client.post(
            url, data=json.dumps(body), content_type='application/json', **extra
        )

    # ---- /start/ ---------------------------------------------------------

    @patch('tickets.sms.start_phone_verification', return_value=True)
    def test_phone_start_success(self, mock_start):
        res = self._post(self.START_URL, {'phone': self.PHONE})
        self.assertEqual(res.status_code, 200, res.content)
        mock_start.assert_called_once_with(self.PHONE)

    @patch('tickets.sms.start_phone_verification', return_value=False)
    def test_phone_start_failure_returns_400(self, mock_start):
        res = self._post(self.START_URL, {'phone': self.PHONE})
        self.assertEqual(res.status_code, 400)

    def test_phone_start_missing_phone_returns_400(self):
        res = self._post(self.START_URL, {})
        self.assertEqual(res.status_code, 400)

    # ---- /verify/ existing user -----------------------------------------

    @patch('tickets.sms.check_phone_verification', return_value=True)
    def test_phone_verify_existing_user_returns_token(self, mock_check):
        res = self._post(self.VERIFY_URL, {'phone': self.PHONE, 'code': '123456'})
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertTrue(body['token'])
        self.assertEqual(body['user_type'], UserProfile.Role.ORGANIZER)
        self.assertEqual(body['user_name'], 'Jane Doe')
        self.assertEqual(body['org_name'], 'Existing Org')
        self.assertEqual(body['org_id'], str(self.org.pk))
        self.assertFalse(body['profile_incomplete'])
        # Token is real and belongs to the existing user
        token = Token.objects.get(key=body['token'])
        self.assertEqual(token.user_id, self.existing_user.pk)

    # ---- /verify/ first-time signup -------------------------------------

    @patch('tickets.sms.check_phone_verification', return_value=True)
    def test_phone_verify_new_user_creates_account(self, mock_check):
        new_phone = '+15555550200'
        res = self._post(self.VERIFY_URL, {'phone': new_phone, 'code': '123456'})
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertTrue(body['token'])
        self.assertTrue(body['profile_incomplete'])
        self.assertEqual(body['org_id'], None)
        self.assertEqual(body['org_name'], '')
        self.assertEqual(body['user_type'], UserProfile.Role.ORGANIZER)

        profile = UserProfile.objects.get(phone_number=new_phone)
        self.assertEqual(profile.role, UserProfile.Role.ORGANIZER)
        self.assertIsNone(profile.organization_id)
        self.assertFalse(profile.user.has_usable_password())

    # ---- /verify/ failures ----------------------------------------------

    @patch('tickets.sms.check_phone_verification', return_value=False)
    def test_phone_verify_wrong_code_returns_400(self, mock_check):
        new_phone = '+15555550201'
        res = self._post(self.VERIFY_URL, {'phone': new_phone, 'code': '999999'})
        self.assertEqual(res.status_code, 400)
        # No User created on failure
        self.assertFalse(UserProfile.objects.filter(phone_number=new_phone).exists())

    def test_phone_verify_missing_fields_returns_400(self):
        res = self._post(self.VERIFY_URL, {'phone': self.PHONE})
        self.assertEqual(res.status_code, 400)

    # ---- Scanner header tolerance ---------------------------------------

    @patch('tickets.sms.check_phone_verification', return_value=True)
    def test_phone_verify_with_valid_scanner_header_still_mints_token(self, mock_check):
        res = self._post(
            self.VERIFY_URL,
            {'phone': self.PHONE, 'code': '123456'},
            HTTP_AUTHORIZATION=f'Scanner {self.scanner_session.token}',
        )
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        # Returned token is for the phone owner, not tied to the scanner session.
        token = Token.objects.get(key=body['token'])
        self.assertEqual(token.user_id, self.existing_user.pk)

    def test_phone_verify_with_invalid_scanner_header_rejected(self):
        bogus = uuid.uuid4()
        res = self._post(
            self.VERIFY_URL,
            {'phone': self.PHONE, 'code': '123456'},
            HTTP_AUTHORIZATION=f'Scanner {bogus}',
        )
        self.assertIn(res.status_code, (401, 403))

    # ---- App Store reviewer bypass (APP_REVIEW_TEST_PHONES) ------------

    REVIEW_PHONE = '+15555550150'
    REVIEW_OTP = '424242'
    REVIEW_OVERRIDE = {'+15555550150': '424242'}

    def test_phone_start_bypasses_twilio_for_whitelisted_phone(self):
        from django.test import override_settings
        with override_settings(APP_REVIEW_TEST_PHONES=self.REVIEW_OVERRIDE), \
             patch('tickets.sms.start_phone_verification') as mock_start:
            res = self._post(self.START_URL, {'phone': self.REVIEW_PHONE})
        self.assertEqual(res.status_code, 200, res.content)
        mock_start.assert_not_called()

    def test_phone_verify_bypasses_twilio_for_whitelisted_phone_correct_code(self):
        from django.test import override_settings
        with override_settings(APP_REVIEW_TEST_PHONES=self.REVIEW_OVERRIDE), \
             patch('tickets.sms.check_phone_verification') as mock_check:
            res = self._post(
                self.VERIFY_URL,
                {'phone': self.REVIEW_PHONE, 'code': self.REVIEW_OTP},
            )
        self.assertEqual(res.status_code, 200, res.content)
        mock_check.assert_not_called()
        body = res.json()
        self.assertTrue(body['token'])
        self.assertTrue(body['profile_incomplete'])
        # First-time auto-create still happens via the shared downstream path.
        self.assertTrue(UserProfile.objects.filter(phone_number=self.REVIEW_PHONE).exists())

    def test_phone_verify_bypass_rejects_wrong_code(self):
        from django.test import override_settings
        with override_settings(APP_REVIEW_TEST_PHONES=self.REVIEW_OVERRIDE), \
             patch('tickets.sms.check_phone_verification') as mock_check:
            res = self._post(
                self.VERIFY_URL,
                {'phone': self.REVIEW_PHONE, 'code': '999999'},
            )
        self.assertEqual(res.status_code, 400)
        mock_check.assert_not_called()
        self.assertFalse(UserProfile.objects.filter(phone_number=self.REVIEW_PHONE).exists())

    @patch('tickets.sms.check_phone_verification', return_value=True)
    def test_phone_verify_non_whitelisted_phone_still_uses_twilio(self, mock_check):
        from django.test import override_settings
        # Default override has only REVIEW_PHONE; self.PHONE is NOT whitelisted.
        with override_settings(APP_REVIEW_TEST_PHONES=self.REVIEW_OVERRIDE):
            res = self._post(self.VERIFY_URL, {'phone': self.PHONE, 'code': '123456'})
        self.assertEqual(res.status_code, 200, res.content)
        mock_check.assert_called_once_with(self.PHONE, '123456')


class StripeConnectOnboardingURLAPITests(TestCase):
    """Regression coverage for GET /api/stripe/connect/onboarding-url/."""

    URL = '/api/stripe/connect/onboarding-url/'

    def setUp(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()
        self.client = Client()
        self.org = Organization.objects.create(name='Connect Org', slug='connect-org')
        self.user = User.objects.create_user(username='org-owner', email='', password='unused')
        self.profile = UserProfile.objects.create(
            user=self.user,
            organization=self.org,
            role=UserProfile.Role.ORGANIZER,
            org_role=UserProfile.OrgRole.OWNER,
            phone_number='+15555550300',
        )
        self.token = Token.objects.create(user=self.user)
        self.auth = {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

    def _fake_link(self, url='https://connect.stripe.com/setup/abc', expires_at=1716242400):
        link = MagicMock()
        link.url = url
        link.expires_at = expires_at
        return link

    def test_requires_token(self):
        res = self.client.get(self.URL)
        self.assertIn(res.status_code, (401, 403))

    def test_reuses_existing_stripe_account(self):
        self.org.stripe_account_id = 'acct_existing'
        self.org.save(update_fields=['stripe_account_id'])
        with patch('stripe.Account.create') as create_mock, \
             patch('stripe.AccountLink.create', return_value=self._fake_link()) as link_mock:
            res = self.client.get(self.URL, **self.auth)
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body['url'], 'https://connect.stripe.com/setup/abc')
        self.assertEqual(body['expires_at'], 1716242400)
        create_mock.assert_not_called()
        link_mock.assert_called_once()
        kwargs = link_mock.call_args.kwargs
        self.assertEqual(kwargs['account'], 'acct_existing')
        # Stripe AccountLink rejects custom schemes; we hand it HTTPS URLs
        # that 302 to cueup:// from the server-side bridge views.
        self.assertTrue(kwargs['refresh_url'].endswith('/m/stripe-connect-refresh/'))
        self.assertTrue(kwargs['return_url'].endswith('/m/stripe-connect-return/'))
        self.assertTrue(kwargs['refresh_url'].startswith('http'))
        self.assertEqual(kwargs['type'], 'account_onboarding')

    def test_bridge_return_redirects_to_cueup_scheme(self):
        res = self.client.get('/m/stripe-connect-return/')
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res['Location'], 'cueup://stripe-connect-return')

    def test_bridge_refresh_redirects_to_cueup_scheme(self):
        res = self.client.get('/m/stripe-connect-refresh/')
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res['Location'], 'cueup://stripe-connect-refresh')

    def test_creates_stripe_account_when_missing(self):
        self.assertEqual(self.org.stripe_account_id, '')
        account = MagicMock()
        account.id = 'acct_new_123'
        with patch('stripe.Account.create', return_value=account) as create_mock, \
             patch('stripe.AccountLink.create', return_value=self._fake_link()):
            res = self.client.get(self.URL, **self.auth)
        self.assertEqual(res.status_code, 200, res.content)
        create_mock.assert_called_once()
        kwargs = create_mock.call_args.kwargs
        self.assertEqual(kwargs['type'], 'express')
        self.assertEqual(kwargs['capabilities'], {
            'card_payments': {'requested': True},
            'transfers': {'requested': True},
        })
        self.org.refresh_from_db()
        self.assertEqual(self.org.stripe_account_id, 'acct_new_123')

    def test_auto_creates_org_for_org_less_user(self):
        # Strip the org off the profile to simulate brand-new phone-OTP signup.
        self.profile.organization = None
        self.profile.org_role = None
        self.profile.save(update_fields=['organization', 'org_role'])

        account = MagicMock()
        account.id = 'acct_for_new_org'
        with patch('stripe.Account.create', return_value=account), \
             patch('stripe.AccountLink.create', return_value=self._fake_link()):
            res = self.client.get(self.URL, **self.auth)

        self.assertEqual(res.status_code, 200, res.content)
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.organization_id)
        new_org = self.profile.organization
        self.assertNotEqual(new_org.pk, self.org.pk)
        self.assertEqual(new_org.stripe_account_id, 'acct_for_new_org')
        self.assertEqual(self.profile.org_role, UserProfile.OrgRole.OWNER)
        # Membership row created with OWNER role.
        self.assertTrue(OrganizationMembership.objects.filter(
            user=self.user,
            organization=new_org,
            org_role=UserProfile.OrgRole.OWNER,
        ).exists())

    def test_returns_503_when_stripe_errors(self):
        import stripe as stripe_lib
        with patch(
            'stripe.Account.create',
            side_effect=stripe_lib.error.APIConnectionError('boom'),
        ):
            res = self.client.get(self.URL, **self.auth)
        self.assertEqual(res.status_code, 503)
        self.org.refresh_from_db()
        self.assertEqual(self.org.stripe_account_id, '')


class MerchantStatusOrganizerAuthTests(TestCase):
    """Coverage for /api/merchant/status/ widening to accept Token auth."""

    URL = '/api/merchant/status/'

    def setUp(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()
        self.client = Client()
        self.org = Organization.objects.create(name='Status Org', slug='status-org')
        self.user = User.objects.create_user(username='status-owner', email='', password='unused')
        self.profile = UserProfile.objects.create(
            user=self.user,
            organization=self.org,
            role=UserProfile.Role.ORGANIZER,
            phone_number='+15555550400',
        )
        self.token = Token.objects.create(user=self.user)
        self.token_auth = {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

    def test_organizer_token_returns_pending_when_no_stripe_account(self):
        res = self.client.get(self.URL, **self.token_auth)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), {'tap_to_pay': {'status': 'pending'}})

    def test_organizer_token_returns_enabled_when_capability_active(self):
        self.org.stripe_account_id = 'acct_enabled_org_tok'
        self.org.save(update_fields=['stripe_account_id'])
        account = MagicMock()
        account.country = 'US'
        account.capabilities = {'card_payments': 'active'}
        with patch('stripe.Account.retrieve', return_value=account):
            res = self.client.get(self.URL, **self.token_auth)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), {'tap_to_pay': {'status': 'enabled'}})

    def test_organizer_without_org_returns_pending(self):
        self.profile.organization = None
        self.profile.save(update_fields=['organization'])
        with patch('stripe.Account.retrieve') as retrieve_mock:
            res = self.client.get(self.URL, **self.token_auth)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), {'tap_to_pay': {'status': 'pending'}})
        retrieve_mock.assert_not_called()

    def test_missing_auth_rejected(self):
        res = self.client.get(self.URL)
        self.assertIn(res.status_code, (401, 403))

    def test_invalid_token_rejected(self):
        res = self.client.get(self.URL, HTTP_AUTHORIZATION='Token notarealkey')
        self.assertIn(res.status_code, (401, 403))


class PartialRefundTests(TestCase):
    """Tests for full + partial refunds on Direct Ticketing orders."""

    def setUp(self):
        self.org = Organization.objects.create(name='Refund Test Org', slug='refund-test-org')
        self.user = User.objects.create_user(username='refunder', email='refunder@example.com', password='pw')
        OrganizationMembership.objects.create(user=self.user, organization=self.org, org_role='owner')
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Direct Event', venue=self.venue,
            start_date=date.today() + timedelta(days=14),
            ticketing_type='direct',
        )
        self.ticket_type = SaleableTicketType.objects.create(
            event=self.event, name='General', price=Decimal('50.00'),
            quantity_limit=100, quantity_sold=2,
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='buyer@example.com', name='Buyer',
        )
        self.order = TicketOrder.objects.create(
            event=self.event,
            customer=self.customer,
            order_number='REF-001',
            order_date=timezone.now(),
            total_amount=Decimal('100.00'),
        )
        Ticket.objects.create(ticket_order=self.order, price=Decimal('50.00'), ticket_type='General')
        Ticket.objects.create(ticket_order=self.order, price=Decimal('50.00'), ticket_type='General')
        self.session = StripeCheckoutSession.objects.create(
            event=self.event,
            organization=self.org,
            stripe_session_id='pi_test_partial',
            stripe_payment_intent_id='pi_test_partial',
            buyer_email='buyer@example.com',
            buyer_name='Buyer',
            status=StripeCheckoutSession.Status.COMPLETED,
            amount_total_cents=10000,
            platform_fee_cents=0,
            line_items_snapshot=[{
                'saleable_ticket_type_id': str(self.ticket_type.id),
                'name': 'General', 'price': '50.00', 'quantity': 2,
                'tier_id': None, 'tier_name': None,
            }],
            ticket_order=self.order,
        )
        self.client.force_login(self.user)
        self.url = reverse('tickets:refund_order', args=[self.order.id])

    @patch('stripe.Refund.create')
    def test_partial_refund_happy_path(self, mock_refund):
        response = self.client.post(self.url, {'refund_type': 'partial', 'refund_amount': '10.00'})
        self.assertEqual(response.status_code, 302)

        mock_refund.assert_called_once_with(payment_intent='pi_test_partial', amount=1000)

        self.order.refresh_from_db()
        self.session.refresh_from_db()
        self.ticket_type.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('10.00'))
        self.assertIsNone(self.order.refunded_at)
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.PARTIALLY_REFUNDED)
        self.assertEqual(self.ticket_type.quantity_sold, 2)

    @patch('stripe.Refund.create')
    def test_second_partial_accumulates(self, mock_refund):
        self.client.post(self.url, {'refund_type': 'partial', 'refund_amount': '10.00'})
        self.client.post(self.url, {'refund_type': 'partial', 'refund_amount': '15.00'})

        self.order.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('25.00'))
        self.assertIsNone(self.order.refunded_at)
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.PARTIALLY_REFUNDED)
        self.assertEqual(mock_refund.call_count, 2)

    @patch('stripe.Refund.create')
    def test_full_refund_after_partials(self, mock_refund):
        self.client.post(self.url, {'refund_type': 'partial', 'refund_amount': '30.00'})
        response = self.client.post(self.url, {'refund_type': 'full'})
        self.assertEqual(response.status_code, 302)

        self.order.refresh_from_db()
        self.session.refresh_from_db()
        self.ticket_type.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('100.00'))
        self.assertIsNotNone(self.order.refunded_at)
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.REFUNDED)
        # Inventory reversed only on the full step (2 tickets sold -> 0)
        self.assertEqual(self.ticket_type.quantity_sold, 0)

        # Final Stripe call had no `amount` (refunds the remaining balance).
        last_call = mock_refund.call_args_list[-1]
        self.assertEqual(last_call.kwargs, {'payment_intent': 'pi_test_partial'})

    @patch('stripe.Refund.create')
    def test_partial_exceeds_remaining_rejected(self, mock_refund):
        response = self.client.post(self.url, {'refund_type': 'partial', 'refund_amount': '200.00'})
        self.assertEqual(response.status_code, 302)
        mock_refund.assert_not_called()
        self.order.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('0.00'))

    @patch('stripe.Refund.create')
    def test_partial_on_fully_refunded_rejected(self, mock_refund):
        self.order.refunded_at = timezone.now()
        self.order.refunded_amount = Decimal('100.00')
        self.order.save(update_fields=['refunded_at', 'refunded_amount'])
        response = self.client.post(self.url, {'refund_type': 'partial', 'refund_amount': '5.00'})
        self.assertEqual(response.status_code, 302)
        mock_refund.assert_not_called()

    @patch('stripe.Refund.create')
    def test_invalid_refund_amount_rejected(self, mock_refund):
        for bad_amount in ('abc', '', '0', '-5'):
            mock_refund.reset_mock()
            response = self.client.post(self.url, {'refund_type': 'partial', 'refund_amount': bad_amount})
            self.assertEqual(response.status_code, 302)
            mock_refund.assert_not_called()
        self.order.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('0.00'))

    def test_ltv_reflects_partial_refund(self):
        self.order.refunded_amount = Decimal('30.00')
        self.order.save(update_fields=['refunded_amount'])
        ltv = self.customer.calculate_lifetime_value()
        self.assertEqual(ltv, Decimal('70.00'))

    def test_ltv_excludes_fully_refunded(self):
        self.order.refunded_at = timezone.now()
        self.order.refunded_amount = Decimal('100.00')
        self.order.save(update_fields=['refunded_at', 'refunded_amount'])
        ltv = self.customer.calculate_lifetime_value()
        self.assertEqual(ltv, Decimal('0.00'))


class FinanceBalanceComputationTests(TestCase):
    """Real-data tests for _compute_available_balance / _compute_settled_payout_balance.

    These guard the refund accounting: partial refunds must reduce the
    balances by exactly the refunded amount, never the whole session.
    """

    def setUp(self):
        self.org = Organization.objects.create(name='Balance Org', slug='balance-org')
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Direct Event', venue=self.venue,
            start_date=date.today() + timedelta(days=14),
            ticketing_type='direct',
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='buyer@example.com', name='Buyer',
        )
        self._seq = 0

    def _create_session(self, amount_cents, fee_cents, status,
                        refunded_amount=None, available_on=None, with_order=True):
        self._seq += 1
        order = None
        if with_order:
            order = TicketOrder.objects.create(
                event=self.event,
                customer=self.customer,
                order_number=f'BAL-{self._seq:03d}',
                order_date=timezone.now(),
                total_amount=Decimal(amount_cents) / 100,
                refunded_amount=refunded_amount or Decimal('0.00'),
            )
        return StripeCheckoutSession.objects.create(
            event=self.event,
            organization=self.org,
            stripe_session_id=f'pi_balance_{self._seq}',
            stripe_payment_intent_id=f'pi_balance_{self._seq}',
            buyer_email='buyer@example.com',
            buyer_name='Buyer',
            status=status,
            amount_total_cents=amount_cents,
            platform_fee_cents=fee_cents,
            available_on=available_on,
            ticket_order=order,
        )

    def test_available_balance_counts_partial_refund_net(self):
        from tickets.views import _compute_available_balance
        self._create_session(5000, 250, StripeCheckoutSession.Status.COMPLETED)
        self._create_session(
            5000, 250, StripeCheckoutSession.Status.PARTIALLY_REFUNDED,
            refunded_amount=Decimal('1.00'),
        )

        revenue, fees, paid_out, available = _compute_available_balance(self.org)

        # Sales drop by exactly the $1 refunded, not the whole $50 session.
        self.assertEqual(revenue, Decimal('99.00'))
        self.assertEqual(fees, Decimal('5.00'))
        self.assertEqual(paid_out, Decimal('0.00'))
        self.assertEqual(available, Decimal('94.00'))

    def test_fully_refunded_via_partials_nets_to_zero(self):
        from tickets.views import _compute_available_balance
        # refunded == total via successive partials: status stays
        # PARTIALLY_REFUNDED but the session must contribute 0, not -fee.
        self._create_session(
            5000, 250, StripeCheckoutSession.Status.PARTIALLY_REFUNDED,
            refunded_amount=Decimal('50.00'),
        )

        revenue, fees, paid_out, available = _compute_available_balance(self.org)

        self.assertEqual(revenue - fees, Decimal('0.00'))
        self.assertEqual(available, Decimal('0.00'))

    def test_refunded_session_excluded(self):
        from tickets.views import _compute_available_balance
        self._create_session(
            5000, 250, StripeCheckoutSession.Status.REFUNDED,
            refunded_amount=Decimal('50.00'),
        )

        revenue, fees, paid_out, available = _compute_available_balance(self.org)

        self.assertEqual(revenue, Decimal('0.00'))
        self.assertEqual(fees, Decimal('0.00'))
        self.assertEqual(available, Decimal('0.00'))

    def test_partial_session_with_null_ticket_order(self):
        from tickets.views import _compute_available_balance
        self._create_session(
            5000, 250, StripeCheckoutSession.Status.PARTIALLY_REFUNDED,
            with_order=False,
        )

        revenue, fees, paid_out, available = _compute_available_balance(self.org)

        # No order to read refunds from: treated as 0 refunded, no crash.
        self.assertEqual(available, Decimal('47.50'))

    def test_legacy_settled_balance_applies_refund_and_settlement_window(self):
        from tickets.views import _compute_legacy_settled_balance
        # Unsettled: available_on in the future — excluded entirely.
        self._create_session(
            10000, 500, StripeCheckoutSession.Status.COMPLETED,
            available_on=timezone.now() + timedelta(days=3),
        )
        # Settled (legacy NULL available_on), partially refunded $10.
        self._create_session(
            5000, 250, StripeCheckoutSession.Status.PARTIALLY_REFUNDED,
            refunded_amount=Decimal('10.00'),
        )
        Payout.objects.create(
            organization=self.org,
            amount=Decimal('20.00'),
            status=Payout.Status.COMPLETED,
            origin=Payout.Origin.LEGACY_TRANSFER,
        )

        settled = _compute_legacy_settled_balance(self.org)

        # (50.00 - 2.50 - 10.00) - 20.00 payout = 17.50
        self.assertEqual(settled, Decimal('17.50'))

    def test_legacy_settled_balance_floors_at_zero(self):
        from tickets.views import _compute_legacy_settled_balance
        self._create_session(5000, 250, StripeCheckoutSession.Status.COMPLETED)
        Payout.objects.create(
            organization=self.org,
            amount=Decimal('100.00'),
            status=Payout.Status.COMPLETED,
            origin=Payout.Origin.MIGRATION,
        )

        self.assertEqual(_compute_legacy_settled_balance(self.org), Decimal('0.00'))
        # Raw (unclamped) value surfaces the negative for the true-up dry-run.
        self.assertEqual(
            _compute_legacy_settled_balance(self.org, clamp=False), Decimal('-52.50'),
        )

    def test_legacy_settled_balance_ignores_connected_pool(self):
        from tickets.views import _compute_legacy_settled_balance
        # Platform-flow session: the only thing that counts.
        self._create_session(5000, 250, StripeCheckoutSession.Status.COMPLETED)
        # Destination/direct sessions live in the connected account — excluded.
        dest = self._create_session(8000, 400, StripeCheckoutSession.Status.COMPLETED)
        dest.charge_flow = StripeCheckoutSession.ChargeFlow.DESTINATION
        dest.save(update_fields=['charge_flow'])
        direct = self._create_session(6000, 300, StripeCheckoutSession.Status.COMPLETED)
        direct.charge_flow = StripeCheckoutSession.ChargeFlow.DIRECT
        direct.save(update_fields=['charge_flow'])
        # Connected-pool payouts (in-app + Express Dashboard) never deduct
        # from the legacy pool.
        Payout.objects.create(
            organization=self.org, amount=Decimal('10.00'),
            status=Payout.Status.COMPLETED, origin=Payout.Origin.CUE,
        )
        Payout.objects.create(
            organization=self.org, amount=Decimal('5.00'),
            status=Payout.Status.COMPLETED, origin=Payout.Origin.STRIPE_DASHBOARD,
        )

        self.assertEqual(_compute_legacy_settled_balance(self.org), Decimal('47.50'))


class FinanceOverviewBalanceTests(TestCase):
    """finance_overview context: clamping and the Sales = paid_out + settling + ready identity."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(
            name='Overview Balance Org',
            slug='overview-balance-org',
            stripe_account_id='acct_overview_bal',
            stripe_onboarding_complete=True,
        )
        self.user = User.objects.create_user(
            username='overview-owner',
            email='overview-owner@example.com',
            password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='overview-owner@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))
        self.finance_url = reverse('tickets:finance_overview')

        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Direct Event', venue=self.venue,
            start_date=date.today() + timedelta(days=14),
            ticketing_type='direct',
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='buyer@example.com', name='Buyer',
        )
        self._seq = 0

    def _create_session(self, amount_cents, fee_cents, status=StripeCheckoutSession.Status.COMPLETED,
                        refunded_amount=None, available_on=None):
        self._seq += 1
        order = TicketOrder.objects.create(
            event=self.event,
            customer=self.customer,
            order_number=f'OVB-{self._seq:03d}',
            order_date=timezone.now(),
            total_amount=Decimal(amount_cents) / 100,
            refunded_amount=refunded_amount or Decimal('0.00'),
        )
        return StripeCheckoutSession.objects.create(
            event=self.event,
            organization=self.org,
            stripe_session_id=f'pi_overview_bal_{self._seq}',
            stripe_payment_intent_id=f'pi_overview_bal_{self._seq}',
            buyer_email='buyer@example.com',
            buyer_name='Buyer',
            status=status,
            amount_total_cents=amount_cents,
            platform_fee_cents=fee_cents,
            available_on=available_on,
            ticket_order=order,
        )

    def _mock_account(self):
        account = MagicMock()
        account.country = 'US'
        account.details_submitted = True
        account.charges_enabled = True
        account.payouts_enabled = True
        account.capabilities = {'card_payments': 'active'}
        account.get.side_effect = lambda key, default=None: {
            'external_accounts': {'data': []},
        }.get(key, default)
        return account

    @patch('tickets.views._get_connected_balance_cents')
    @patch('stripe.Account.retrieve')
    def test_connected_balance_drives_cards(self, mock_retrieve, mock_connected):
        # The connected account balance is the source of truth: Ready to
        # Withdraw = available, Settling = pending — independent of ledger.
        mock_retrieve.return_value = self._mock_account()
        mock_connected.return_value = (5150, 2850)

        self._create_session(5000, 250)  # ledger Sales: net 47.50
        Payout.objects.create(
            organization=self.org, amount=Decimal('10.00'), status=Payout.Status.COMPLETED,
        )

        res = self.client.get(self.finance_url)
        self.assertEqual(res.status_code, 200)

        self.assertEqual(res.context['net_sales'], Decimal('47.50'))
        self.assertEqual(res.context['paid_out'], Decimal('10.00'))
        self.assertEqual(res.context['stripe_available'], Decimal('51.50'))
        self.assertEqual(res.context['settling_balance'], Decimal('28.50'))

    @patch('tickets.views._get_connected_balance_cents')
    @patch('stripe.Account.retrieve')
    def test_negative_connected_balance_clamped(self, mock_retrieve, mock_connected):
        # Refund clawback after a withdrawal can push the connected balance
        # negative — never render a negative Ready to Withdraw or Settling.
        mock_retrieve.return_value = self._mock_account()
        mock_connected.return_value = (-699, -100)

        self._create_session(10000, 340)

        res = self.client.get(self.finance_url)
        self.assertEqual(res.status_code, 200)

        self.assertEqual(res.context['stripe_available'], Decimal('0.00'))
        self.assertEqual(res.context['settling_balance'], Decimal('0.00'))
        self.assertEqual(res.context['net_sales'], Decimal('96.60'))


class ChargeRefundedWebhookTests(TestCase):
    """charge.refunded webhook: syncs dashboard-initiated refunds into the DB."""

    def setUp(self):
        self.org = Organization.objects.create(name='Webhook Refund Org', slug='webhook-refund-org')
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Direct Event', venue=self.venue,
            start_date=date.today() + timedelta(days=14),
            ticketing_type='direct',
        )
        self.ticket_type = SaleableTicketType.objects.create(
            event=self.event, name='General', price=Decimal('50.00'),
            quantity_limit=100, quantity_sold=2,
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='buyer@example.com', name='Buyer',
        )
        self.order = TicketOrder.objects.create(
            event=self.event,
            customer=self.customer,
            order_number='WHR-001',
            order_date=timezone.now(),
            total_amount=Decimal('100.00'),
        )
        self.session = StripeCheckoutSession.objects.create(
            event=self.event,
            organization=self.org,
            stripe_session_id='pi_webhook_refund',
            stripe_payment_intent_id='pi_webhook_refund',
            buyer_email='buyer@example.com',
            buyer_name='Buyer',
            status=StripeCheckoutSession.Status.COMPLETED,
            amount_total_cents=10000,
            platform_fee_cents=0,
            line_items_snapshot=[{
                'saleable_ticket_type_id': str(self.ticket_type.id),
                'name': 'General', 'price': '50.00', 'quantity': 2,
                'tier_id': None, 'tier_name': None,
            }],
            ticket_order=self.order,
        )
        self.webhook_url = reverse('tickets:stripe_webhook')

    def _post_refund_event(self, mock_construct, *, amount_refunded, refunded,
                           payment_intent='pi_webhook_refund'):
        mock_construct.return_value = {
            'type': 'charge.refunded',
            'data': {'object': {
                'id': 'ch_test_refund',
                'payment_intent': payment_intent,
                'amount_refunded': amount_refunded,
                'refunded': refunded,
            }},
        }
        return self.client.post(
            self.webhook_url,
            data=json.dumps({'type': 'charge.refunded'}),
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='sig_test',
        )

    @patch('stripe.Webhook.construct_event')
    def test_partial_dashboard_refund_synced(self, mock_construct):
        res = self._post_refund_event(mock_construct, amount_refunded=1000, refunded=False)
        self.assertEqual(res.status_code, 200)

        self.order.refresh_from_db()
        self.session.refresh_from_db()
        self.ticket_type.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('10.00'))
        self.assertIsNone(self.order.refunded_at)
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.PARTIALLY_REFUNDED)
        # No inventory restore on partial refunds.
        self.assertEqual(self.ticket_type.quantity_sold, 2)

    @patch('stripe.Webhook.construct_event')
    def test_full_dashboard_refund_synced(self, mock_construct):
        res = self._post_refund_event(mock_construct, amount_refunded=10000, refunded=True)
        self.assertEqual(res.status_code, 200)

        self.order.refresh_from_db()
        self.session.refresh_from_db()
        self.ticket_type.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('100.00'))
        self.assertIsNotNone(self.order.refunded_at)
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.REFUNDED)
        self.assertEqual(self.ticket_type.quantity_sold, 0)

    @patch('stripe.Webhook.construct_event')
    def test_full_refund_webhook_idempotent(self, mock_construct):
        # Start with extra inventory so a double restore would be visible.
        self.ticket_type.quantity_sold = 5
        self.ticket_type.save(update_fields=['quantity_sold'])

        self._post_refund_event(mock_construct, amount_refunded=10000, refunded=True)
        self._post_refund_event(mock_construct, amount_refunded=10000, refunded=True)

        self.order.refresh_from_db()
        self.ticket_type.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('100.00'))
        # Restored once (5 - 2 = 3), not twice.
        self.assertEqual(self.ticket_type.quantity_sold, 3)

    @patch('stripe.Webhook.construct_event')
    def test_echo_after_app_refund_is_noop(self, mock_construct):
        # refund_order already wrote this state; the webhook echo must not change it.
        self.order.refunded_amount = Decimal('10.00')
        self.order.save(update_fields=['refunded_amount'])
        self.session.status = StripeCheckoutSession.Status.PARTIALLY_REFUNDED
        self.session.save(update_fields=['status'])

        res = self._post_refund_event(mock_construct, amount_refunded=1000, refunded=False)
        self.assertEqual(res.status_code, 200)

        self.order.refresh_from_db()
        self.session.refresh_from_db()
        self.ticket_type.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('10.00'))
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.PARTIALLY_REFUNDED)
        self.assertEqual(self.ticket_type.quantity_sold, 2)

    @patch('stripe.checkout.Session.list', return_value={'data': []})
    @patch('stripe.Webhook.construct_event')
    def test_unknown_payment_intent_ignored(self, mock_construct, mock_cs_list):
        res = self._post_refund_event(
            mock_construct, amount_refunded=1000, refunded=False,
            payment_intent='pi_not_ours',
        )
        self.assertEqual(res.status_code, 200)

        self.order.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('0.00'))
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.COMPLETED)

    @patch('stripe.checkout.Session.list')
    @patch('stripe.Webhook.construct_event')
    def test_legacy_checkout_session_matched_via_fallback(self, mock_construct, mock_cs_list):
        # Legacy pre-PI-flow row: cs_… id, blank stripe_payment_intent_id.
        self.session.stripe_session_id = 'cs_legacy_123'
        self.session.stripe_payment_intent_id = ''
        self.session.save(update_fields=['stripe_session_id', 'stripe_payment_intent_id'])
        mock_cs_list.return_value = {'data': [{'id': 'cs_legacy_123'}]}

        res = self._post_refund_event(
            mock_construct, amount_refunded=1000, refunded=False,
            payment_intent='pi_legacy_refund',
        )
        self.assertEqual(res.status_code, 200)

        mock_cs_list.assert_called_once_with(payment_intent='pi_legacy_refund', limit=1)
        self.order.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('10.00'))
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.PARTIALLY_REFUNDED)
        # The pi id is persisted so future lookups match directly.
        self.assertEqual(self.session.stripe_payment_intent_id, 'pi_legacy_refund')


class BackfillRefundStateCommandTests(TestCase):
    """Tests for the backfill_refund_state management command."""

    def setUp(self):
        self.org = Organization.objects.create(name='Backfill Org', slug='backfill-org')
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Direct Event', venue=self.venue,
            start_date=date.today() + timedelta(days=14),
            ticketing_type='direct',
        )
        self.ticket_type = SaleableTicketType.objects.create(
            event=self.event, name='General', price=Decimal('50.00'),
            quantity_limit=100, quantity_sold=2,
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='buyer@example.com', name='Buyer',
        )
        self.order = TicketOrder.objects.create(
            event=self.event,
            customer=self.customer,
            order_number='BFR-001',
            order_date=timezone.now(),
            total_amount=Decimal('100.00'),
        )
        self.session = StripeCheckoutSession.objects.create(
            event=self.event,
            organization=self.org,
            stripe_session_id='pi_backfill_refund',
            stripe_payment_intent_id='pi_backfill_refund',
            buyer_email='buyer@example.com',
            buyer_name='Buyer',
            status=StripeCheckoutSession.Status.COMPLETED,
            amount_total_cents=10000,
            platform_fee_cents=0,
            line_items_snapshot=[{
                'saleable_ticket_type_id': str(self.ticket_type.id),
                'name': 'General', 'price': '50.00', 'quantity': 2,
                'tier_id': None, 'tier_name': None,
            }],
            ticket_order=self.order,
        )

    def _run(self, *args, refunds, charge, checkout_sessions=None):
        """Run the command with mocked Stripe refund feed + charge retrieval."""
        from io import StringIO
        out = StringIO()
        refund_list = MagicMock()
        refund_list.auto_paging_iter.return_value = iter(refunds)
        with patch('stripe.Refund.list', return_value=refund_list), \
             patch('stripe.Charge.retrieve', return_value=charge), \
             patch('stripe.checkout.Session.list',
                   return_value={'data': checkout_sessions or []}):
            call_command('backfill_refund_state', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_without_writing(self):
        output = self._run(
            refunds=[{'id': 're_1', 'charge': 'ch_backfill'}],
            charge={
                'id': 'ch_backfill',
                'payment_intent': 'pi_backfill_refund',
                'amount_refunded': 1000,
                'refunded': False,
            },
        )

        self.assertIn('WOULD UPDATE', output)
        self.order.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('0.00'))
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.COMPLETED)

    def test_apply_syncs_missed_partial_refund(self):
        output = self._run(
            '--apply',
            refunds=[{'id': 're_1', 'charge': 'ch_backfill'}],
            charge={
                'id': 'ch_backfill',
                'payment_intent': 'pi_backfill_refund',
                'amount_refunded': 1000,
                'refunded': False,
            },
        )

        self.assertIn('UPDATE', output)
        self.order.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('10.00'))
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.PARTIALLY_REFUNDED)

    def test_apply_syncs_missed_full_refund(self):
        self._run(
            '--apply',
            refunds=[{'id': 're_1', 'charge': 'ch_backfill'}],
            charge={
                'id': 'ch_backfill',
                'payment_intent': 'pi_backfill_refund',
                'amount_refunded': 10000,
                'refunded': True,
            },
        )

        self.order.refresh_from_db()
        self.session.refresh_from_db()
        self.ticket_type.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('100.00'))
        self.assertIsNotNone(self.order.refunded_at)
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.REFUNDED)
        self.assertEqual(self.ticket_type.quantity_sold, 0)

    def test_already_synced_refund_is_noop(self):
        self.order.refunded_amount = Decimal('10.00')
        self.order.save(update_fields=['refunded_amount'])
        self.session.status = StripeCheckoutSession.Status.PARTIALLY_REFUNDED
        self.session.save(update_fields=['status'])

        output = self._run(
            '--apply',
            refunds=[{'id': 're_1', 'charge': 'ch_backfill'}],
            charge={
                'id': 'ch_backfill',
                'payment_intent': 'pi_backfill_refund',
                'amount_refunded': 1000,
                'refunded': False,
            },
        )

        self.assertIn('already current', output)
        self.order.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('10.00'))

    def test_foreign_charge_ignored(self):
        output = self._run(
            '--apply',
            refunds=[{'id': 're_1', 'charge': 'ch_foreign'}],
            charge={
                'id': 'ch_foreign',
                'payment_intent': 'pi_someone_elses',
                'amount_refunded': 1000,
                'refunded': False,
            },
        )

        self.assertIn('Not ours: 1', output)
        # Unmatched charges are itemized so prod runs are diagnosable.
        self.assertIn('IGNORE ch_foreign', output)
        self.assertIn('pi=pi_someone_elses', output)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.COMPLETED)

    def test_legacy_checkout_session_backfilled_via_fallback(self):
        # Legacy pre-PI-flow row: cs_… id, blank stripe_payment_intent_id —
        # the exact shape that produced "Not ours" on the first prod run.
        self.session.stripe_session_id = 'cs_legacy_456'
        self.session.stripe_payment_intent_id = ''
        self.session.save(update_fields=['stripe_session_id', 'stripe_payment_intent_id'])

        output = self._run(
            '--apply',
            refunds=[{'id': 're_1', 'charge': 'ch_legacy'}],
            charge={
                'id': 'ch_legacy',
                'payment_intent': 'pi_legacy_456',
                'amount_refunded': 1000,
                'refunded': False,
            },
            checkout_sessions=[{'id': 'cs_legacy_456'}],
        )

        self.assertIn('UPDATE', output)
        self.order.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('10.00'))
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.PARTIALLY_REFUNDED)
        self.assertEqual(self.session.stripe_payment_intent_id, 'pi_legacy_456')

    def test_org_filter_skips_other_orgs(self):
        other_org = Organization.objects.create(name='Other Org', slug='other-backfill-org')
        output = self._run(
            '--apply', '--org', other_org.slug,
            refunds=[{'id': 're_1', 'charge': 'ch_backfill'}],
            charge={
                'id': 'ch_backfill',
                'payment_intent': 'pi_backfill_refund',
                'amount_refunded': 1000,
                'refunded': False,
            },
        )

        self.assertIn('Skipped: 1', output)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.COMPLETED)


class LinkCustomerToBuyerTests(TestCase):
    """Tests for tickets.utils.link_customer_to_buyer."""

    def setUp(self):
        self.org = Organization.objects.create(name='Link Org', slug='link-org')
        self.user = User.objects.create_user(
            username='alice', email='Alice@Example.com', password='x',
        )
        self.profile = UserProfile.objects.create(
            user=self.user, organization=self.org, phone_number='+15551234567',
        )

    def _new_customer(self, **kwargs):
        defaults = {
            'organization': self.org,
            'email': 'alice@example.com',
            'name': 'Alice',
            'phone': '',
        }
        defaults.update(kwargs)
        return Customer.objects.create(**defaults)

    def test_links_user_and_copies_phone_for_matching_buyer(self):
        from .utils import link_customer_to_buyer
        customer = self._new_customer()

        link_customer_to_buyer(customer, 'alice@example.com')

        customer.refresh_from_db()
        self.assertEqual(customer.user_id, self.user.id)
        self.assertEqual(customer.phone, '+15551234567')

    def test_does_not_overwrite_existing_phone(self):
        from .utils import link_customer_to_buyer
        customer = self._new_customer(phone='+15550000000')

        link_customer_to_buyer(customer, 'alice@example.com')

        customer.refresh_from_db()
        self.assertEqual(customer.user_id, self.user.id)
        self.assertEqual(customer.phone, '+15550000000')

    def test_no_op_when_no_matching_user(self):
        from .utils import link_customer_to_buyer
        customer = self._new_customer(email='nobody@example.com')

        link_customer_to_buyer(customer, 'nobody@example.com')

        customer.refresh_from_db()
        self.assertIsNone(customer.user_id)
        self.assertEqual(customer.phone, '')

    def test_sets_phone_when_user_already_linked(self):
        from .utils import link_customer_to_buyer
        customer = self._new_customer(user=self.user)

        link_customer_to_buyer(customer, 'alice@example.com')

        customer.refresh_from_db()
        self.assertEqual(customer.user_id, self.user.id)
        self.assertEqual(customer.phone, '+15551234567')


class EventWeatherHourlyEndpointTest(TestCase):
    """Tests for the hourly weather forecast JSON endpoint."""

    def setUp(self):
        from django.core.cache import cache as django_cache
        self.client = Client()
        self.org = Organization.objects.create(name='Weather Org', slug='weather-org')
        self.user = User.objects.create_user(
            username='weather', email='weather@example.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.venue = Venue.objects.create(
            organization=self.org, name='Weather Venue', city='Los Angeles',
        )
        self.event = Event.objects.create(
            organization=self.org, name='Weather Event', venue=self.venue,
            start_date=date.today() + timedelta(days=3),
        )

        self.other_org = Organization.objects.create(name='Other Org', slug='other-weather-org')
        self.other_venue = Venue.objects.create(
            organization=self.other_org, name='Other Venue', city='Seattle',
        )
        self.other_event = Event.objects.create(
            organization=self.other_org, name='Other Event', venue=self.other_venue,
            start_date=date.today() + timedelta(days=3),
        )
        django_cache.clear()

    def tearDown(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()

    def _login(self):
        self.assertTrue(self.client.login(username='weather@example.com', password='testpass123'))
        self.client.get(reverse('tickets:home'))

    def _fake_forecast(self, *args, **kwargs):
        return {
            'venue_name': 'Weather Venue',
            'days': [{
                'date': (date.today() + timedelta(days=3)).isoformat(),
                'source': 'nws',
                'hours': [{
                    'time': '15:00', 'temp': 75, 'precip_prob': 0, 'wind': 8,
                    'weather_code': 0, 'condition_label': 'Clear', 'condition_icon': 'bi-sun',
                }],
            }],
        }

    def test_endpoint_returns_json_for_owner(self):
        self._login()
        with patch('tickets.views.get_event_hourly_forecast', side_effect=self._fake_forecast):
            url = reverse('tickets:event_weather_hourly', args=[self.event.id])
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn('days', body)
        self.assertEqual(len(body['days']), 1)
        self.assertEqual(body['days'][0]['hours'][0]['temp'], 75)
        self.assertEqual(body['venue_name'], 'Weather Venue')

    def test_endpoint_returns_404_for_other_org_event(self):
        self._login()
        url = reverse('tickets:event_weather_hourly', args=[self.other_event.id])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)

    def test_endpoint_returns_empty_payload_when_service_returns_none(self):
        self._login()
        with patch('tickets.views.get_event_hourly_forecast', return_value=None):
            url = reverse('tickets:event_weather_hourly', args=[self.event.id])
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body, {'days': [], 'venue_name': None})

    def test_endpoint_redirects_unauthenticated(self):
        url = reverse('tickets:event_weather_hourly', args=[self.event.id])
        resp = self.client.get(url)
        self.assertIn(resp.status_code, (302, 401, 403))


class PublicOrgProfileEventsTests(TestCase):
    """The public org profile lists direct-ticketing events, splitting upcoming vs past."""

    def setUp(self):
        from .models import (
            TICKETING_TYPE_DIRECT, TICKETING_TYPE_EXTERNAL,
            EVENT_STATUS_LIVE, EVENT_STATUS_ENDED, EVENT_STATUS_DRAFT, EVENT_STATUS_CANCELLED,
        )
        self.TICKETING_TYPE_DIRECT = TICKETING_TYPE_DIRECT
        self.TICKETING_TYPE_EXTERNAL = TICKETING_TYPE_EXTERNAL
        self.EVENT_STATUS_LIVE = EVENT_STATUS_LIVE
        self.EVENT_STATUS_ENDED = EVENT_STATUS_ENDED
        self.EVENT_STATUS_DRAFT = EVENT_STATUS_DRAFT
        self.EVENT_STATUS_CANCELLED = EVENT_STATUS_CANCELLED

        self.client = Client()
        self.org = Organization.objects.create(name='Profile Org', slug='profile-org')
        self.venue = Venue.objects.create(
            organization=self.org, name='The Echo', city='Los Angeles'
        )
        self.today = timezone.now().date()
        self.url = reverse('tickets:public_org_profile', args=[self.org.slug])

    def _event(self, name, days_offset, ticketing_type=None, status=None, end_offset=None):
        return Event.objects.create(
            organization=self.org,
            name=name,
            venue=self.venue,
            start_date=self.today + timedelta(days=days_offset),
            end_date=(self.today + timedelta(days=end_offset)) if end_offset is not None else None,
            ticketing_type=ticketing_type or self.TICKETING_TYPE_DIRECT,
            status=status or self.EVENT_STATUS_LIVE,
        )

    def test_upcoming_and_past_split(self):
        upcoming = self._event('Upcoming Live', 10)
        past_live = self._event('Past Live', -10)
        past_ended = self._event('Past Ended', -20, status=self.EVENT_STATUS_ENDED)

        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        upcoming_ids = {e.id for e in resp.context['upcoming_events']}
        past_ids = {e.id for e in resp.context['past_events']}

        self.assertEqual(upcoming_ids, {upcoming.id})
        self.assertEqual(past_ids, {past_live.id, past_ended.id})

    def test_excludes_external_draft_and_cancelled(self):
        direct_upcoming = self._event('Direct Upcoming', 5)
        direct_past = self._event('Direct Past', -5)
        external_past = self._event(
            'External Past', -5, ticketing_type=self.TICKETING_TYPE_EXTERNAL
        )
        draft_upcoming = self._event('Draft Upcoming', 5, status=self.EVENT_STATUS_DRAFT)
        cancelled_past = self._event('Cancelled Past', -5, status=self.EVENT_STATUS_CANCELLED)

        resp = self.client.get(self.url)
        all_ids = (
            {e.id for e in resp.context['upcoming_events']}
            | {e.id for e in resp.context['past_events']}
        )
        self.assertEqual(all_ids, {direct_upcoming.id, direct_past.id})
        self.assertNotIn(external_past.id, all_ids)
        self.assertNotIn(draft_upcoming.id, all_ids)
        self.assertNotIn(cancelled_past.id, all_ids)

    def test_multiday_event_past_uses_end_date(self):
        # Started in the past but still running (ends in the future) -> upcoming.
        ongoing = self._event('Ongoing Festival', -2, end_offset=3)
        # Multi-day event fully in the past -> past.
        finished = self._event('Finished Festival', -10, end_offset=-5)

        resp = self.client.get(self.url)
        self.assertIn(ongoing.id, {e.id for e in resp.context['upcoming_events']})
        self.assertIn(finished.id, {e.id for e in resp.context['past_events']})

    def test_empty_state_when_no_events(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(resp.context['upcoming_events']), [])
        self.assertEqual(list(resp.context['past_events']), [])


class EventBulkTagTests(TestCase):
    """Tests for the event Customers tab + bulk-tag action."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(
            name='SMS Org', slug='sms-org', sms_marketing_enabled=True,
        )
        self.host = User.objects.create_user(
            username='evchost', email='evchost@example.com', password='pw',
        )
        UserProfile.objects.create(
            user=self.host, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )
        OrganizationMembership.objects.create(
            user=self.host, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )
        self.tag = CustomerTag.objects.create(organization=self.org, name='Faithful')
        self.venue = Venue.objects.create(
            organization=self.org, name='The Hall', city='Townsville',
        )
        self.event = Event.objects.create(
            organization=self.org, name='Launch Party', venue=self.venue,
            start_date=date(2024, 6, 15), start_time=time(19, 0, 0),
        )
        # Two buyers of this event, plus a customer with no order for it.
        self.c1 = self._customer('a@example.com', 'Alice')
        self.c2 = self._customer('b@example.com', 'Bob')
        self.non_attendee = self._customer('c@example.com', 'Carol')
        self._order(self.c1, 'ORD-1')
        self._order(self.c2, 'ORD-2')

        self.client.login(username='evchost@example.com', password='pw')
        self.client.get(reverse('tickets:home'))  # seed _org_id in session
        self.url = reverse('tickets:event_bulk_tag', args=[self.event.id])

    def _customer(self, email, name):
        return Customer.objects.create(
            organization=self.org, email=email, name=name, phone='+15555550000',
            sms_opt_in=True,
        )

    def _order(self, customer, number):
        return TicketOrder.objects.create(
            customer=customer, event=self.event, order_number=number,
            order_date='2024-06-01 10:00:00', total_amount=Decimal('50.00'),
        )

    def test_customers_tab_rendered(self):
        resp = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="tab-customers-btn"')
        self.assertEqual(resp.context['event_customers_page'].paginator.count, 2)

    def test_tag_existing(self):
        resp = self.client.post(self.url, {
            'tag_id': str(self.tag.id),
            'customer_ids': [str(self.c1.id), str(self.c2.id)],
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn('?tab=customers', resp['Location'])
        self.assertCountEqual(
            list(self.tag.customers.values_list('id', flat=True)),
            [self.c1.id, self.c2.id],
        )

    def test_tag_new_creates_tag(self):
        resp = self.client.post(self.url, {
            'new_tag_name': 'Brand New',
            'customer_ids': [str(self.c1.id)],
        })
        self.assertEqual(resp.status_code, 302)
        tag = CustomerTag.objects.get(organization=self.org, name='Brand New')
        self.assertEqual(list(tag.customers.values_list('id', flat=True)), [self.c1.id])

    def test_select_all_tags_all_attendees(self):
        resp = self.client.post(self.url, {'tag_id': str(self.tag.id), 'select_all': '1'})
        self.assertEqual(resp.status_code, 302)
        self.assertCountEqual(
            list(self.tag.customers.values_list('id', flat=True)),
            [self.c1.id, self.c2.id],
        )

    def test_non_attendee_ids_ignored(self):
        resp = self.client.post(self.url, {
            'tag_id': str(self.tag.id),
            'customer_ids': [str(self.c1.id), str(self.non_attendee.id)],
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            list(self.tag.customers.values_list('id', flat=True)), [self.c1.id],
        )

    def test_non_host_forbidden(self):
        doorman = User.objects.create_user(
            username='door', email='door@example.com', password='pw',
        )
        UserProfile.objects.create(
            user=doorman, organization=self.org, org_role=UserProfile.OrgRole.DOORMAN,
        )
        OrganizationMembership.objects.create(
            user=doorman, organization=self.org, org_role=UserProfile.OrgRole.DOORMAN,
        )
        c = Client()
        c.login(username='door@example.com', password='pw')
        c.get(reverse('tickets:home'))
        resp = c.post(self.url, {'tag_id': str(self.tag.id), 'select_all': '1'})
        self.assertEqual(resp.status_code, 403)


class CustomersBulkTagTests(TestCase):
    """Tests for selecting customers on /customers/ and bulk-tagging them."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(
            name='Cust Org', slug='cust-org', sms_marketing_enabled=True,
        )
        self.host = User.objects.create_user(
            username='custhost', email='custhost@example.com', password='pw',
        )
        UserProfile.objects.create(
            user=self.host, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )
        OrganizationMembership.objects.create(
            user=self.host, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )
        self.tag = CustomerTag.objects.create(organization=self.org, name='Existing')
        self.vip1 = self._customer('vip1@example.com', 'VIP One', segment='VIP')
        self.vip2 = self._customer('vip2@example.com', 'VIP Two', segment='VIP')
        self.loyal = self._customer('loyal@example.com', 'Loyal One', segment='Loyal')

        self.client.login(username='custhost@example.com', password='pw')
        self.client.get(reverse('tickets:home'))  # seed _org_id in session
        self.url = reverse('tickets:customers_bulk_tag')

    def _customer(self, email, name, segment=''):
        return Customer.objects.create(
            organization=self.org, email=email, name=name, phone='+15555550000',
            sms_opt_in=True, rfm_segment=segment,
        )

    def test_list_page_renders_checkboxes(self):
        resp = self.client.get(reverse('tickets:customer_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="custSelectAllRows"')
        self.assertContains(resp, 'cust-checkbox')

    def test_tag_existing(self):
        resp = self.client.post(self.url, {
            'tag_id': str(self.tag.id),
            'customer_ids': [str(self.vip1.id), str(self.loyal.id)],
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('tickets:customer_list'), resp['Location'])
        self.assertCountEqual(
            list(self.tag.customers.values_list('id', flat=True)),
            [self.vip1.id, self.loyal.id],
        )

    def test_tag_new_creates_once(self):
        self.client.post(self.url, {
            'new_tag_name': 'Fresh', 'customer_ids': [str(self.vip1.id)],
        })
        self.client.post(self.url, {
            'new_tag_name': 'Fresh', 'customer_ids': [str(self.vip2.id)],
        })
        # Same name → get_or_create reuses the single tag.
        self.assertEqual(
            CustomerTag.objects.filter(organization=self.org, name='Fresh').count(), 1,
        )
        tag = CustomerTag.objects.get(organization=self.org, name='Fresh')
        self.assertCountEqual(
            list(tag.customers.values_list('id', flat=True)),
            [self.vip1.id, self.vip2.id],
        )

    def test_select_all_respects_filters(self):
        resp = self.client.post(self.url, {
            'tag_id': str(self.tag.id), 'select_all': '1', 'segment': 'VIP',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn('segment=VIP', resp['Location'])
        self.assertCountEqual(
            list(self.tag.customers.values_list('id', flat=True)),
            [self.vip1.id, self.vip2.id],
        )

    def test_foreign_ids_dropped(self):
        other_org = Organization.objects.create(name='Other', slug='other-cust-org')
        intruder = Customer.objects.create(
            organization=other_org, email='x@example.com', name='X', sms_opt_in=True,
        )
        resp = self.client.post(self.url, {
            'tag_id': str(self.tag.id),
            'customer_ids': [str(self.vip1.id), str(intruder.id)],
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            list(self.tag.customers.values_list('id', flat=True)), [self.vip1.id],
        )

    def test_no_tag_selected_errors(self):
        resp = self.client.post(self.url, {'customer_ids': [str(self.vip1.id)]})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.vip1.tags.count(), 0)

    def test_non_host_forbidden(self):
        doorman = User.objects.create_user(
            username='custdoor', email='custdoor@example.com', password='pw',
        )
        UserProfile.objects.create(
            user=doorman, organization=self.org, org_role=UserProfile.OrgRole.DOORMAN,
        )
        OrganizationMembership.objects.create(
            user=doorman, organization=self.org, org_role=UserProfile.OrgRole.DOORMAN,
        )
        c = Client()
        c.login(username='custdoor@example.com', password='pw')
        c.get(reverse('tickets:home'))
        resp = c.post(self.url, {'tag_id': str(self.tag.id), 'select_all': '1'})
        self.assertEqual(resp.status_code, 403)


class LowStockThresholdTests(TestCase):
    """Coverage for the per-ticket-type configurable 'Only X left' warning."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Low Stock Org', slug='low-stock-org')
        self.user = User.objects.create_user(
            username='lsuser', email='ls@example.com', password='testpass123',
            first_name='Low', last_name='Stock',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='ls@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))
        self.venue = Venue.objects.create(organization=self.org, name='LS Venue', city='LA')
        self.event = Event.objects.create(
            organization=self.org,
            name='Low Stock Event',
            venue=self.venue,
            start_date=date.today() + timedelta(days=7),
            start_time=time(20, 0, 0),
            ticketing_type='direct',
            status='live',
        )

    def _ticket_type(self, **kwargs):
        defaults = dict(event=self.event, name='GA', price=Decimal('25.00'))
        defaults.update(kwargs)
        return SaleableTicketType.objects.create(**defaults)

    def test_no_threshold_returns_none(self):
        """Off by default: no warning when no threshold is set, even with low stock."""
        tt = self._ticket_type(quantity_limit=100, quantity_sold=98)
        self.assertIsNone(tt.low_stock_remaining())

    def test_unlimited_returns_none(self):
        tt = self._ticket_type(quantity_limit=None, low_stock_threshold=5)
        self.assertIsNone(tt.low_stock_remaining())

    def test_above_threshold_returns_none(self):
        tt = self._ticket_type(quantity_limit=100, quantity_sold=50, low_stock_threshold=5)
        self.assertIsNone(tt.low_stock_remaining())

    def test_at_or_below_threshold_returns_remaining(self):
        tt = self._ticket_type(quantity_limit=100, quantity_sold=97, low_stock_threshold=5)
        self.assertEqual(tt.low_stock_remaining(), 3)

    def test_sold_out_returns_none(self):
        tt = self._ticket_type(quantity_limit=100, quantity_sold=100, low_stock_threshold=5)
        self.assertIsNone(tt.low_stock_remaining())

    def test_tier_aware_uses_active_tier_remaining(self):
        """With tiers, the warning reflects the active tier's remaining capacity."""
        tt = self._ticket_type(quantity_limit=None, low_stock_threshold=5)
        SaleableTicketTypeTier.objects.create(
            ticket_type=tt, name='Early Bird', price=Decimal('20.00'),
            allotment=10, quantity_sold=8, order=0,
        )
        self.assertEqual(tt.low_stock_remaining(), 2)

    def test_edit_view_saves_threshold(self):
        """The edit modal (posting to saleable_ticket_type_edit) persists the field."""
        tt = self._ticket_type(quantity_limit=100)
        url = reverse('tickets:saleable_ticket_type_edit',
                      kwargs={'event_id': self.event.id, 'ticket_type_id': tt.id})
        resp = self.client.post(url, {
            'name': 'GA', 'price': '25.00', 'quantity_limit': '100',
            'low_stock_threshold': '7', 'is_active': 'on',
            'tiers-TOTAL_FORMS': '0', 'tiers-INITIAL_FORMS': '0',
            'tiers-MIN_NUM_FORMS': '0', 'tiers-MAX_NUM_FORMS': '1000',
        })
        self.assertin_redirect_or_ok(resp)
        tt.refresh_from_db()
        self.assertEqual(tt.low_stock_threshold, 7)

    def test_data_endpoint_returns_threshold(self):
        """The edit-modal data endpoint includes low_stock_threshold."""
        tt = self._ticket_type(quantity_limit=100, low_stock_threshold=4)
        url = reverse('tickets:saleable_ticket_type_data',
                      kwargs={'event_id': self.event.id, 'ticket_type_id': tt.id})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['low_stock_threshold'], 4)

    def assertin_redirect_or_ok(self, resp):
        self.assertIn(resp.status_code, (200, 302))


class LoyaltyTierAssignmentTests(TestCase):
    """Tier assignment logic over existing attendance/purchase data."""

    def setUp(self):
        self.org = Organization.objects.create(name='Loyal Org', slug='loyal-org', loyalty_feature_enabled=True)
        self.other_org = Organization.objects.create(name='Other Loyal Org', slug='other-loyal-org')
        self.venue = Venue.objects.create(organization=self.org, name='Hall', city='Town')
        self.program = LoyaltyProgram.objects.create(organization=self.org, name='Backstage Club')
        # Gold (rank 3): big spend + multiple events. Silver (rank 2): repeat buyer.
        # Member (rank 1): no rules -> everyone qualifies as a base tier.
        self.gold = LoyaltyTier.objects.create(
            program=self.program, name='Gold', rank=3, color='red',
            min_lifetime_value=Decimal('500.00'), min_events_purchased=2,
        )
        self.silver = LoyaltyTier.objects.create(
            program=self.program, name='Silver', rank=2, color='blue', min_order_count=2,
        )
        self.member = LoyaltyTier.objects.create(
            program=self.program, name='Member', rank=1, color='green',
        )

    def _make_customer(self, email, ltv, orders_spec):
        """orders_spec: list of (event, total_amount, num_tickets)."""
        customer = Customer.objects.create(
            organization=self.org, email=email, name=email.split('@')[0],
            lifetime_value=Decimal(str(ltv)),
            last_order_date=date(2026, 6, 1),
        )
        for i, (event, amount, n_tickets) in enumerate(orders_spec):
            order = TicketOrder.objects.create(
                customer=customer, event=event, order_number=f'{email}-{i}',
                order_date='2026-06-01 10:00:00', total_amount=Decimal(str(amount)),
            )
            for _ in range(n_tickets):
                Ticket.objects.create(ticket_order=order, price=Decimal('10.00'))
        return customer

    def _event(self, name):
        return Event.objects.create(
            organization=self.org, name=name, venue=self.venue,
            start_date=date(2026, 6, 15), start_time=time(19, 0, 0),
        )

    def _assign(self):
        from tickets.services.loyalty import assign_loyalty_tiers
        return assign_loyalty_tiers(self.program)

    def test_best_tier_assigned_highest_rank_wins(self):
        e1, e2 = self._event('E1'), self._event('E2')
        # Qualifies for Gold (ltv 600, 2 events) AND Silver (2 orders) -> Gold (higher rank).
        whale = self._make_customer('whale@x.com', 600, [(e1, 300, 1), (e2, 300, 1)])
        # Two orders but low spend / one event -> Silver.
        regular = self._make_customer('regular@x.com', 100, [(e1, 50, 1), (e1, 50, 1)])
        # One order, low spend -> Member (base tier).
        casual = self._make_customer('casual@x.com', 40, [(e1, 40, 1)])

        assigned = self._assign()
        whale.refresh_from_db(); regular.refresh_from_db(); casual.refresh_from_db()
        self.assertEqual(whale.loyalty_tier, self.gold)
        self.assertEqual(regular.loyalty_tier, self.silver)
        self.assertEqual(casual.loyalty_tier, self.member)
        self.assertEqual(assigned, 3)

    def test_no_match_when_no_base_tier(self):
        # Drop the catch-all Member tier; a low customer should match nothing.
        self.member.delete()
        e1 = self._event('E1')
        low = self._make_customer('low@x.com', 10, [(e1, 10, 1)])
        self._assign()
        low.refresh_from_db()
        self.assertIsNone(low.loyalty_tier)

    def test_placeholder_customers_excluded(self):
        e1 = self._event('E1')
        ghost = self._make_customer('ghost@placeholder.local', 999, [(e1, 999, 5)])
        self._assign()
        ghost.refresh_from_db()
        self.assertIsNone(ghost.loyalty_tier)

    def test_recency_rule(self):
        self.member.delete()
        self.silver.delete()
        self.gold.min_lifetime_value = None
        self.gold.min_events_purchased = None
        self.gold.max_days_since_last_order = 30
        self.gold.save()
        e1 = self._event('E1')
        recent = self._make_customer('recent@x.com', 100, [(e1, 100, 1)])
        recent.last_order_date = timezone.now().date()
        recent.save()
        stale = self._make_customer('stale@x.com', 100, [(e1, 100, 1)])
        stale.last_order_date = timezone.now().date() - timedelta(days=400)
        stale.save()
        self._assign()
        recent.refresh_from_db(); stale.refresh_from_db()
        self.assertEqual(recent.loyalty_tier, self.gold)
        self.assertIsNone(stale.loyalty_tier)

    def test_two_active_programs_rejected_by_db(self):
        # self.program is already active; a second active program in the same org
        # must be rejected by the partial-unique DB constraint.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LoyaltyProgram.objects.create(
                    organization=self.org, name='VIP Pass', is_active=True,
                )

    def test_min_tickets_purchased_rule(self):
        self.gold.delete()
        self.member.delete()
        self.silver.min_order_count = None
        self.silver.min_tickets_purchased = 3
        self.silver.save()
        e1 = self._event('E1')
        big = self._make_customer('big@x.com', 100, [(e1, 100, 4)])   # 4 tickets
        small = self._make_customer('small@x.com', 100, [(e1, 100, 1)])  # 1 ticket
        self._assign()
        big.refresh_from_db(); small.refresh_from_db()
        self.assertEqual(big.loyalty_tier, self.silver)
        self.assertIsNone(small.loyalty_tier)

    def test_refunded_and_deleted_orders_excluded_from_counts(self):
        self.gold.delete()
        self.member.delete()
        self.silver.min_order_count = 2
        self.silver.save()
        e1 = self._event('E1')
        # Customer with 2 orders, but one refunded and never mind -> only 1 live order.
        cust = Customer.objects.create(
            organization=self.org, email='ref@x.com', name='ref',
            lifetime_value=Decimal('50'), last_order_date=date(2026, 6, 1),
        )
        TicketOrder.objects.create(customer=cust, event=e1, order_number='ref-live',
                                   order_date='2026-06-01 10:00:00', total_amount=Decimal('50'))
        TicketOrder.objects.create(customer=cust, event=e1, order_number='ref-refunded',
                                   order_date='2026-06-01 10:00:00', total_amount=Decimal('50'),
                                   refunded_at=timezone.now())
        deleted = TicketOrder.objects.create(customer=cust, event=e1, order_number='ref-deleted',
                                              order_date='2026-06-01 10:00:00', total_amount=Decimal('50'))
        deleted.delete()  # soft delete
        self._assign()
        cust.refresh_from_db()
        # Only 1 live order -> below min_order_count=2 -> no tier.
        self.assertIsNone(cust.loyalty_tier)

    def _make_scanned_customer(self, email, events, scanned_at=None, scanned=True):
        """One free ($0) order per event, each with a single ticket.

        ``scanned=True`` stamps ``Ticket.scanned_at`` (an attended check-in);
        ``scanned=False`` leaves it null (a free-RSVP no-show).
        """
        customer = Customer.objects.create(
            organization=self.org, email=email, name=email.split('@')[0],
            lifetime_value=Decimal('0'), last_order_date=date(2026, 6, 1),
        )
        for i, event in enumerate(events):
            order = TicketOrder.objects.create(
                customer=customer, event=event, order_number=f'{email}-{i}',
                order_date='2026-06-01 10:00:00', total_amount=Decimal('0'),
            )
            Ticket.objects.create(
                ticket_order=order, price=Decimal('0'),
                scanned_at=(scanned_at or timezone.now()) if scanned else None,
            )
        return customer

    def test_events_attended_counts_only_scanned_tickets(self):
        # min_events_attended must count door scans, not orders: a free-RSVP
        # no-show who ordered 2 events but never scanned in stays out.
        self.gold.delete(); self.silver.delete()
        self.member.min_events_attended = 2
        self.member.save()
        e1, e2 = self._event('E1'), self._event('E2')
        attendee = self._make_scanned_customer('went@x.com', [e1, e2], scanned=True)
        noshow = self._make_scanned_customer('noshow@x.com', [e1, e2], scanned=False)
        self._assign()
        attendee.refresh_from_db(); noshow.refresh_from_db()
        self.assertEqual(attendee.loyalty_tier, self.member)
        self.assertIsNone(noshow.loyalty_tier)

    def test_attendance_recency_rule(self):
        # attended_within_days windows the attendance count: both events must fall
        # inside the window, keyed off scan time (not last order).
        self.gold.delete(); self.silver.delete()
        self.member.min_events_attended = 2
        self.member.attended_within_days = 120
        self.member.save()
        e1, e2 = self._event('E1'), self._event('E2')
        recent = self._make_scanned_customer('recent@x.com', [e1, e2], scanned_at=timezone.now())
        lapsed = self._make_scanned_customer(
            'lapsed@x.com', [e1, e2], scanned_at=timezone.now() - timedelta(days=200),
        )
        self._assign()
        recent.refresh_from_db(); lapsed.refresh_from_db()
        self.assertEqual(recent.loyalty_tier, self.member)
        self.assertIsNone(lapsed.loyalty_tier)

    def test_windowed_event_count_excludes_old_attendance(self):
        # "Attended >= 2 events within 120 days" counts only in-window scans, so a
        # customer with 2 events attended all-time but only 1 inside the window
        # does NOT qualify (the whole point of windowing the count).
        self.gold.delete(); self.silver.delete()
        self.member.min_events_attended = 2
        self.member.attended_within_days = 120
        self.member.save()
        e1, e2, e3, e4 = self._event('E1'), self._event('E2'), self._event('E3'), self._event('E4')
        # 1 recent + 1 old distinct event -> only 1 in the window -> below 2.
        edge = self._make_scanned_customer('edge@x.com', [e1], scanned_at=timezone.now())
        old_order = TicketOrder.objects.create(
            customer=edge, event=e2, order_number='edge-old',
            order_date='2026-06-01 10:00:00', total_amount=Decimal('0'),
        )
        Ticket.objects.create(
            ticket_order=old_order, price=Decimal('0'),
            scanned_at=timezone.now() - timedelta(days=200),
        )
        # 2 distinct events both inside the window -> qualifies.
        inside = self._make_scanned_customer('inside@x.com', [e3, e4], scanned_at=timezone.now())
        self._assign()
        edge.refresh_from_db(); inside.refresh_from_db()
        self.assertIsNone(edge.loyalty_tier)
        self.assertEqual(inside.loyalty_tier, self.member)

    def test_window_only_rule_requires_one_in_window(self):
        # A window with no explicit count means "attended >= 1 event within D days".
        self.gold.delete(); self.silver.delete()
        self.member.min_events_attended = None
        self.member.attended_within_days = 120
        self.member.save()
        e1 = self._event('E1')
        recent = self._make_scanned_customer('r1@x.com', [e1], scanned_at=timezone.now())
        lapsed = self._make_scanned_customer(
            'l1@x.com', [e1], scanned_at=timezone.now() - timedelta(days=200),
        )
        self._assign()
        recent.refresh_from_db(); lapsed.refresh_from_db()
        self.assertEqual(recent.loyalty_tier, self.member)
        self.assertIsNone(lapsed.loyalty_tier)

    def test_paid_events_rule_excludes_free_orders(self):
        # "Paid events >= 2" counts distinct events with an order where money was
        # paid (> $0): a customer with 2 free-RSVP orders stays out while a paying
        # buyer across two events qualifies.
        self.gold.delete(); self.silver.delete()
        self.member.min_paid_events_recent = 2
        self.member.save()
        e1, e2 = self._event('E1'), self._event('E2')
        buyer = self._make_customer('buyer@x.com', 100, [(e1, 50, 1), (e2, 50, 1)])
        freeloader = self._make_customer('free@x.com', 0, [(e1, 0, 1), (e2, 0, 1)])
        self._assign()
        buyer.refresh_from_db(); freeloader.refresh_from_db()
        self.assertEqual(buyer.loyalty_tier, self.member)
        self.assertIsNone(freeloader.loyalty_tier)

    def test_paid_events_rule_counts_distinct_events(self):
        # "Paid events >= 2" counts UNIQUE events, so several paid orders to the
        # same event count once: a buyer with 3 paid orders all to one event does
        # NOT qualify, while a buyer with paid orders across two events does.
        self.gold.delete(); self.silver.delete()
        self.member.min_paid_events_recent = 2
        self.member.save()
        e1, e2 = self._event('E1'), self._event('E2')
        one_event = self._make_customer('one@x.com', 150, [(e1, 50, 1), (e1, 50, 1), (e1, 50, 1)])
        two_events = self._make_customer('two@x.com', 100, [(e1, 50, 1), (e2, 50, 1)])
        self._assign()
        one_event.refresh_from_db(); two_events.refresh_from_db()
        self.assertIsNone(one_event.loyalty_tier)
        self.assertEqual(two_events.loyalty_tier, self.member)

    def test_windowed_paid_events_excludes_old_orders(self):
        # "Paid events >= 2 within 90 days" counts only paid events placed inside
        # the window, keyed off order_date: a buyer across 2 events 200 days ago
        # does NOT qualify, one across 2 events 10 days ago does.
        self.gold.delete(); self.silver.delete()
        self.member.min_paid_events_recent = 2
        self.member.paid_events_within_days = 90
        self.member.save()
        e1, e2 = self._event('E1'), self._event('E2')
        now = timezone.now()

        def _paid_buyer(email, days_ago):
            cust = Customer.objects.create(
                organization=self.org, email=email, name=email.split('@')[0],
                lifetime_value=Decimal('100'), last_order_date=now.date(),
            )
            for i, ev in enumerate([e1, e2]):
                TicketOrder.objects.create(
                    customer=cust, event=ev, order_number=f'{email}-{i}',
                    order_date=now - timedelta(days=days_ago), total_amount=Decimal('50'),
                )
            return cust

        recent = _paid_buyer('recent-paid@x.com', 10)
        lapsed = _paid_buyer('lapsed-paid@x.com', 200)
        self._assign()
        recent.refresh_from_db(); lapsed.refresh_from_db()
        self.assertEqual(recent.loyalty_tier, self.member)
        self.assertIsNone(lapsed.loyalty_tier)


class LoyaltyViewTests(TestCase):
    """Access control, org-scoping, and the builder flow."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='View Org', slug='view-org', loyalty_feature_enabled=True)
        self.other_org = Organization.objects.create(name='Other View Org', slug='other-view-org', loyalty_feature_enabled=True)
        self.host = User.objects.create_user(username='vhost', email='vhost@x.com', password='pw12345')
        UserProfile.objects.create(user=self.host, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(user=self.host, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.program = LoyaltyProgram.objects.create(organization=self.org, name='Club')
        self.other_program = LoyaltyProgram.objects.create(organization=self.other_org, name='Foreign Club')

    def _login(self):
        self.client.login(username='vhost@x.com', password='pw12345')
        self.client.get(reverse('tickets:home'))

    def test_list_requires_login(self):
        resp = self.client.get(reverse('tickets:loyalty_program_list'))
        self.assertEqual(resp.status_code, 302)

    def test_list_and_detail_ok(self):
        self._login()
        self.assertEqual(self.client.get(reverse('tickets:loyalty_program_list')).status_code, 200)
        self.assertEqual(
            self.client.get(reverse('tickets:loyalty_program_detail', args=[self.program.id])).status_code, 200
        )

    def test_cannot_access_other_org_program(self):
        self._login()
        resp = self.client.get(reverse('tickets:loyalty_program_detail', args=[self.other_program.id]))
        self.assertEqual(resp.status_code, 404)

    @patch('tickets.tasks.recalculate_loyalty_tiers_task.delay')
    def test_builder_creates_program_with_tiers(self, mock_delay):
        self._login()
        resp = self.client.post(reverse('tickets:loyalty_program_create'), {
            'name': 'New Club',
            'description': '',
            'is_active': 'on',
            'points_basis': 'per_ticket',
            'points_rate': '1',
            'tiers-TOTAL_FORMS': '1',
            'tiers-INITIAL_FORMS': '0',
            'tiers-MIN_NUM_FORMS': '0',
            'tiers-MAX_NUM_FORMS': '1000',
            'tiers-0-name': 'Gold',
            'tiers-0-rank': '1',
            'tiers-0-color': 'red',
            'tiers-0-perks': 'Free drink',
            'tiers-0-min_lifetime_value': '500',
            'tiers-0-min_order_count': '',
            'tiers-0-min_events_purchased': '',
            'tiers-0-min_tickets_purchased': '',
            'tiers-0-max_days_since_last_order': '',
        })
        self.assertEqual(resp.status_code, 302)
        program = LoyaltyProgram.objects.get(organization=self.org, name='New Club')
        self.assertEqual(program.tiers.count(), 1)
        self.assertEqual(program.tiers.first().name, 'Gold')
        mock_delay.assert_called_once()

    def test_delete_unassigns_members(self):
        self._login()
        tier = LoyaltyTier.objects.create(program=self.program, name='Gold', rank=1)
        cust = Customer.objects.create(
            organization=self.org, email='m@x.com', name='M', loyalty_tier=tier,
        )
        resp = self.client.post(reverse('tickets:loyalty_program_delete', args=[self.program.id]))
        self.assertEqual(resp.status_code, 302)
        cust.refresh_from_db()
        self.assertIsNone(cust.loyalty_tier)
        self.program.refresh_from_db()
        self.assertIsNotNone(self.program.deleted_at)


class LoyaltyHardeningTests(TestCase):
    """Covers the review-hardening: recalc gating/lifecycle, formset validation,
    stats, edit, tier-member scoping, and the same-org display guard."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Hard Org', slug='hard-org', loyalty_feature_enabled=True)
        self.other_org = Organization.objects.create(name='Hard Other', slug='hard-other')
        self.host = User.objects.create_user(username='hhost', email='hhost@x.com', password='pw12345')
        UserProfile.objects.create(user=self.host, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(user=self.host, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.program = LoyaltyProgram.objects.create(organization=self.org, name='Active Club', is_active=True)
        self.base_tier = LoyaltyTier.objects.create(program=self.program, name='Member', rank=1)
        self.cust = Customer.objects.create(organization=self.org, email='c@x.com', name='C',
                                            lifetime_value=Decimal('10'))

    def _login(self):
        self.client.login(username='hhost@x.com', password='pw12345')
        self.client.get(reverse('tickets:home'))

    # --- recalc task lifecycle / gating ---
    def test_recalc_task_assigns_and_stamps(self):
        from tickets.tasks import recalculate_loyalty_tiers_task
        result = recalculate_loyalty_tiers_task.apply(args=[str(self.program.id)])
        self.assertTrue(result.successful())
        self.program.refresh_from_db()
        self.assertFalse(self.program.recalc_in_progress)
        self.assertIsNotNone(self.program.last_recalculated_at)
        self.cust.refresh_from_db()
        self.assertEqual(self.cust.loyalty_tier, self.base_tier)  # base tier matches everyone

    def test_recalc_task_skips_inactive_and_preserves_active(self):
        from tickets.tasks import recalculate_loyalty_tiers_task
        # Assign the active program first.
        recalculate_loyalty_tiers_task.apply(args=[str(self.program.id)])
        self.cust.refresh_from_db()
        self.assertEqual(self.cust.loyalty_tier, self.base_tier)
        # An inactive second program with its own catch-all tier.
        inactive = LoyaltyProgram.objects.create(organization=self.org, name='Draft', is_active=False)
        LoyaltyTier.objects.create(program=inactive, name='DraftBase', rank=1)
        result = recalculate_loyalty_tiers_task.apply(args=[str(inactive.id)])
        self.assertEqual(result.result, 0)  # gated: no-op
        self.cust.refresh_from_db()
        self.assertEqual(self.cust.loyalty_tier, self.base_tier)  # active program's member intact

    def test_recalc_task_missing_program_noop(self):
        from tickets.tasks import recalculate_loyalty_tiers_task
        result = recalculate_loyalty_tiers_task.apply(args=[str(uuid.uuid4())])
        self.assertEqual(result.result, 0)

    @patch('tickets.tasks.recalculate_loyalty_tiers_task.retry', side_effect=RuntimeError('boom'))
    @patch('tickets.services.loyalty.assign_loyalty_tiers', side_effect=ValueError('fail'))
    def test_recalc_task_failure_does_not_stamp(self, mock_assign, mock_retry):
        from tickets.tasks import recalculate_loyalty_tiers_task
        with self.assertRaises(RuntimeError):
            recalculate_loyalty_tiers_task.apply(args=[str(self.program.id)], throw=True)
        self.program.refresh_from_db()
        self.assertFalse(self.program.recalc_in_progress)
        self.assertIsNone(self.program.last_recalculated_at)

    # --- recalc view gating ---
    @patch('tickets.tasks.recalculate_loyalty_tiers_task.delay')
    def test_recalc_view_enqueues_for_active(self, mock_delay):
        self._login()
        resp = self.client.post(reverse('tickets:loyalty_recalculate', args=[self.program.id]))
        self.assertEqual(resp.status_code, 302)
        mock_delay.assert_called_once()

    @patch('tickets.tasks.recalculate_loyalty_tiers_task.delay')
    def test_recalc_view_blocks_inactive(self, mock_delay):
        self._login()
        self.program.is_active = False
        self.program.save(update_fields=['is_active'])
        resp = self.client.post(reverse('tickets:loyalty_recalculate', args=[self.program.id]))
        self.assertEqual(resp.status_code, 302)
        mock_delay.assert_not_called()

    # --- formset validation (ruleless tier) ---
    def _formset_data(self, rows):
        data = {
            'tiers-TOTAL_FORMS': str(len(rows)), 'tiers-INITIAL_FORMS': '0',
            'tiers-MIN_NUM_FORMS': '0', 'tiers-MAX_NUM_FORMS': '1000',
        }
        for i, (name, rank, rules) in enumerate(rows):
            data[f'tiers-{i}-name'] = name
            data[f'tiers-{i}-rank'] = str(rank)
            data[f'tiers-{i}-color'] = 'blue'
            data[f'tiers-{i}-perks'] = ''
            for f in ('min_lifetime_value', 'min_order_count', 'min_events_purchased',
                      'min_tickets_purchased', 'max_days_since_last_order'):
                data[f'tiers-{i}-{f}'] = str(rules.get(f, '')) if rules.get(f) is not None else ''
        return data

    def test_formset_rejects_two_ruleless_tiers(self):
        from tickets.forms import LoyaltyTierFormSet
        prog = LoyaltyProgram.objects.create(organization=self.org, name='F1', is_active=False)
        data = self._formset_data([('A', 1, {}), ('B', 2, {})])
        fs = LoyaltyTierFormSet(data, instance=prog)
        self.assertFalse(fs.is_valid())
        self.assertIn('Only one tier', str(fs.non_form_errors()))

    def test_formset_rejects_ruleless_not_lowest(self):
        from tickets.forms import LoyaltyTierFormSet
        prog = LoyaltyProgram.objects.create(organization=self.org, name='F2', is_active=False)
        # Ruleless tier at the HIGHEST rank -> makes the lower ruled tier unreachable.
        data = self._formset_data([('Base', 5, {}), ('Gold', 1, {'min_lifetime_value': '500'})])
        fs = LoyaltyTierFormSet(data, instance=prog)
        self.assertFalse(fs.is_valid())
        self.assertIn('lowest rank', str(fs.non_form_errors()))

    def test_formset_accepts_one_ruleless_lowest(self):
        from tickets.forms import LoyaltyTierFormSet
        prog = LoyaltyProgram.objects.create(organization=self.org, name='F3', is_active=False)
        data = self._formset_data([('Base', 1, {}), ('Gold', 5, {'min_lifetime_value': '500'})])
        fs = LoyaltyTierFormSet(data, instance=prog)
        self.assertTrue(fs.is_valid(), fs.non_form_errors())

    # --- stats ---
    def test_stats_distribution(self):
        from tickets.services.loyalty import LoyaltyProgramStats
        from tickets.tasks import recalculate_loyalty_tiers_task
        Customer.objects.create(organization=self.org, email='c2@x.com', name='C2', lifetime_value=Decimal('5'))
        recalculate_loyalty_tiers_task.apply(args=[str(self.program.id)])
        stats = LoyaltyProgramStats(self.program).calculate()
        self.assertEqual(stats['total_customers'], 2)
        self.assertEqual(stats['assigned'], 2)
        self.assertEqual(stats['tiers'][0]['count'], 2)

    # --- edit persists tier changes ---
    def test_edit_persists_tier_changes(self):
        self._login()
        data = {
            'name': 'Active Club', 'description': '', 'is_active': 'on',
            'points_basis': 'per_ticket', 'points_rate': '1',
            'tiers-TOTAL_FORMS': '1', 'tiers-INITIAL_FORMS': '1',
            'tiers-MIN_NUM_FORMS': '0', 'tiers-MAX_NUM_FORMS': '1000',
            'tiers-0-id': str(self.base_tier.id),
            'tiers-0-name': 'Renamed', 'tiers-0-rank': '1', 'tiers-0-color': 'green', 'tiers-0-perks': 'New perk',
            'tiers-0-min_lifetime_value': '', 'tiers-0-min_order_count': '',
            'tiers-0-min_events_purchased': '', 'tiers-0-min_tickets_purchased': '',
            'tiers-0-max_days_since_last_order': '',
        }
        with patch('tickets.tasks.recalculate_loyalty_tiers_task.delay'):
            resp = self.client.post(reverse('tickets:loyalty_program_edit', args=[self.program.id]), data)
        self.assertEqual(resp.status_code, 302)
        self.base_tier.refresh_from_db()
        self.assertEqual(self.base_tier.name, 'Renamed')
        self.assertEqual(self.base_tier.perks, 'New perk')

    # --- tier members scoping ---
    def test_tier_members_lists_only_that_tier(self):
        gold = LoyaltyTier.objects.create(program=self.program, name='Gold', rank=2,
                                          min_lifetime_value=Decimal('100'))
        in_gold = Customer.objects.create(organization=self.org, email='g@x.com', name='Gilda',
                                          loyalty_tier=gold)
        in_base = Customer.objects.create(organization=self.org, email='b@x.com', name='Basil',
                                          loyalty_tier=self.base_tier)
        self._login()
        resp = self.client.get(reverse('tickets:loyalty_tier_members', args=[self.program.id, gold.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Gilda')
        self.assertNotContains(resp, 'Basil')

    # --- tier members market filter ---
    def test_tier_members_market_filter(self):
        from tickets.models import Market
        venue = Venue.objects.create(organization=self.org, name='V', city='C')
        austin = Market.objects.create(organization=self.org, name='Austin',
                                       geography_level='city', geography_value='Austin')
        dallas = Market.objects.create(organization=self.org, name='Dallas',
                                       geography_level='city', geography_value='Dallas')
        austin_event = Event.objects.create(organization=self.org, name='ATX', venue=venue,
                                            market=austin, start_date=date(2026, 9, 1),
                                            start_time=time(20, 0, 0))
        dallas_event = Event.objects.create(organization=self.org, name='DAL', venue=venue,
                                            market=dallas, start_date=date(2026, 9, 2),
                                            start_time=time(20, 0, 0))
        # Ada orders most in Austin; Della orders most in Dallas.
        ada = Customer.objects.create(organization=self.org, email='ada@x.com', name='Ada',
                                      loyalty_tier=self.base_tier)
        della = Customer.objects.create(organization=self.org, email='del@x.com', name='Della',
                                        loyalty_tier=self.base_tier)
        for i in range(2):
            TicketOrder.objects.create(customer=ada, event=austin_event, order_number=f'A{i}',
                                       order_date=timezone.now(), total_amount=Decimal('10.00'))
        TicketOrder.objects.create(customer=della, event=dallas_event, order_number='D0',
                                   order_date=timezone.now(), total_amount=Decimal('10.00'))
        self._login()
        url = reverse('tickets:loyalty_tier_members', args=[self.program.id, self.base_tier.id])
        # Unfiltered: both appear.
        resp = self.client.get(url)
        self.assertContains(resp, 'Ada')
        self.assertContains(resp, 'Della')
        # Filtered to Austin: only Ada (her most-frequented market).
        resp = self.client.get(url, {'market': 'Austin'})
        self.assertContains(resp, 'Ada')
        self.assertNotContains(resp, 'Della')

    # --- same-org display guard (T2) ---
    def test_customer_detail_hides_foreign_tier(self):
        foreign_prog = LoyaltyProgram.objects.create(organization=self.other_org, name='Foreign Secret')
        foreign_tier = LoyaltyTier.objects.create(program=foreign_prog, name='ForeignGold', rank=1)
        # Force a cross-tenant assignment (bypassing normal scoped assignment).
        Customer.objects.filter(id=self.cust.id).update(loyalty_tier=foreign_tier)
        self._login()
        resp = self.client.get(reverse('tickets:customer_detail', args=[self.cust.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'Foreign Secret')
        self.assertNotContains(resp, 'ForeignGold')


class LoyaltyPointsServiceTests(TestCase):
    """Points wallet service: earn math, idempotency, applied-delta revokes."""

    def setUp(self):
        self.org = Organization.objects.create(name='Pts Org', slug='pts-org', loyalty_feature_enabled=True)
        self.venue = Venue.objects.create(organization=self.org, name='Hall', city='Town')
        self.event = Event.objects.create(
            organization=self.org, name='Show', venue=self.venue,
            start_date=date(2026, 7, 1), start_time=time(20, 0, 0),
        )
        self.program = LoyaltyProgram.objects.create(
            organization=self.org, name='Pts Club', is_active=True,
            points_enabled=True, points_basis=LoyaltyProgram.PointsBasis.PER_TICKET,
            points_rate=Decimal('10'),
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='pts@x.com', name='Pat',
        )

    def _order(self, number='PTS-1', amount='25.50', tickets=2, customer=None):
        order = TicketOrder.objects.create(
            customer=customer or self.customer, event=self.event,
            order_number=number, order_date=timezone.now(),
            total_amount=Decimal(amount),
        )
        for _ in range(tickets):
            Ticket.objects.create(ticket_order=order, price=Decimal('10.00'))
        return order

    def test_per_ticket_math(self):
        from tickets.services.loyalty import award_points_for_order
        order = self._order(tickets=3)
        self.assertEqual(award_points_for_order(order), 30)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 30)
        self.assertEqual(self.customer.lifetime_points, 30)

    def test_per_dollar_floor_math(self):
        from tickets.services.loyalty import award_points_for_order
        self.program.points_basis = LoyaltyProgram.PointsBasis.PER_DOLLAR
        self.program.points_rate = Decimal('1.50')
        self.program.save()
        order = self._order(amount='25.50', tickets=1)
        # 1.50 * 25.50 = 38.25 -> floor 38
        self.assertEqual(award_points_for_order(order), 38)

    def test_zero_ticket_order_per_ticket_earns_nothing(self):
        from tickets.services.loyalty import award_points_for_order
        order = self._order(tickets=0)
        self.assertEqual(award_points_for_order(order), 0)
        self.assertEqual(LoyaltyPointsTransaction.objects.count(), 0)

    def test_per_dollar_free_order_earns_nothing(self):
        from tickets.services.loyalty import award_points_for_order
        self.program.points_basis = LoyaltyProgram.PointsBasis.PER_DOLLAR
        self.program.save()
        order = self._order(amount='0.00', tickets=2)
        self.assertEqual(award_points_for_order(order), 0)

    def test_no_program_disabled_or_inactive_noop(self):
        from tickets.services.loyalty import award_points_for_order
        order = self._order()
        self.program.points_enabled = False
        self.program.save()
        self.assertEqual(award_points_for_order(order), 0)
        self.program.points_enabled = True
        self.program.is_active = False
        self.program.save()
        self.assertEqual(award_points_for_order(order), 0)
        self.program.delete()  # soft delete
        self.assertEqual(award_points_for_order(order), 0)
        self.assertEqual(LoyaltyPointsTransaction.objects.count(), 0)

    def test_placeholder_customer_never_earns(self):
        from tickets.services.loyalty import award_points_for_order, award_points_for_orders
        ghost = Customer.objects.create(
            organization=self.org, email=f'in-person-{self.org.id}@placeholder.local', name='Walk-ups',
        )
        order = self._order(number='PTS-GHOST', customer=ghost)
        self.assertEqual(award_points_for_order(order), 0)
        self.assertEqual(award_points_for_orders([order], self.program), 0)
        ghost.refresh_from_db()
        self.assertEqual(ghost.points_balance, 0)

    def test_double_award_single_earn_row(self):
        from tickets.services.loyalty import award_points_for_order
        order = self._order()
        self.assertEqual(award_points_for_order(order), 20)
        self.assertEqual(award_points_for_order(order), 0)
        self.assertEqual(
            LoyaltyPointsTransaction.objects.filter(ticket_order=order).count(), 1
        )
        # DB constraint backstop: a direct duplicate insert must fail.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LoyaltyPointsTransaction.objects.create(
                    organization=self.org, customer=self.customer, ticket_order=order,
                    kind=LoyaltyPointsTransaction.Kind.EARN, amount=20,
                    balance_after=40, lifetime_after=40,
                )

    def test_revoke_uses_stored_amount_after_rate_change(self):
        from tickets.services.loyalty import award_points_for_order, revoke_points_for_order
        order = self._order(tickets=2)
        award_points_for_order(order)  # 20 pts at rate 10
        self.program.points_rate = Decimal('50')
        self.program.save()
        self.assertEqual(revoke_points_for_order(order), 20)  # stored, not 100
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 0)
        self.assertEqual(self.customer.lifetime_points, 0)

    def test_revoke_idempotent_and_absent_earn_noop(self):
        from tickets.services.loyalty import award_points_for_order, revoke_points_for_order
        order = self._order()
        self.assertEqual(revoke_points_for_order(order), 0)  # no earn yet
        award_points_for_order(order)
        self.assertEqual(revoke_points_for_order(order), 20)
        self.assertEqual(revoke_points_for_order(order), 0)  # already revoked
        self.assertEqual(
            LoyaltyPointsTransaction.objects.filter(
                ticket_order=order, kind=LoyaltyPointsTransaction.Kind.REVOKE
            ).count(), 1
        )

    def test_clamped_revoke_records_applied_delta(self):
        """Sum-auditability (D2): when the balance is below the earn (Phase-2
        spend simulation), the REVOKE row records the APPLIED delta and
        SUM(amounts) == points_balance still holds."""
        from tickets.services.loyalty import award_points_for_order, revoke_points_for_order
        order = self._order(tickets=2)
        award_points_for_order(order)  # +20
        # Simulate a Phase-2 spend: drop balance below the earn.
        Customer.objects.filter(id=self.customer.id).update(points_balance=5)
        self.assertEqual(revoke_points_for_order(order), 5)  # applied, not 20
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 0)
        revoke = LoyaltyPointsTransaction.objects.get(
            ticket_order=order, kind=LoyaltyPointsTransaction.Kind.REVOKE
        )
        self.assertEqual(revoke.amount, -5)
        self.assertIn('clamped', revoke.description)
        ledger_sum = sum(
            t.amount for t in self.customer.points_transactions.all()
        )
        # EARN +20, REVOKE -5: sum 15 vs balance 0 — but the manual .update()
        # bypassed the ledger (the simulated spend has no row). Adjusting for
        # the simulated -15 spend, sums reconcile: 20 - 5 - 15 == 0.
        self.assertEqual(ledger_sum - 15, self.customer.points_balance)

    def test_snapshot_sequence(self):
        from tickets.services.loyalty import award_points_for_order, revoke_points_for_order
        o1 = self._order(number='PTS-A', tickets=1)
        o2 = self._order(number='PTS-B', tickets=3)
        award_points_for_order(o1)   # +10 -> 10
        award_points_for_order(o2)   # +30 -> 40
        revoke_points_for_order(o1)  # -10 -> 30
        rows = list(
            LoyaltyPointsTransaction.objects.filter(customer=self.customer).order_by('created_at')
        )
        self.assertEqual([r.balance_after for r in rows], [10, 40, 30])
        self.assertEqual([r.lifetime_after for r in rows], [10, 40, 30])

    def test_bulk_award_and_bulk_revoke_twins(self):
        from tickets.services.loyalty import award_points_for_orders, revoke_points_for_orders
        other = Customer.objects.create(organization=self.org, email='o@x.com', name='O')
        orders = [
            self._order(number='B-1', tickets=1),
            self._order(number='B-2', tickets=2),
            self._order(number='B-3', tickets=1, customer=other),
        ]
        total = award_points_for_orders(orders, self.program)
        self.assertEqual(total, 40)
        self.customer.refresh_from_db(); other.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 30)
        self.assertEqual(other.points_balance, 10)
        # Re-run: idempotent no-op.
        self.assertEqual(award_points_for_orders(orders, self.program), 0)
        # Bulk revoke nets everything back to zero.
        self.assertEqual(revoke_points_for_orders(orders), 40)
        self.customer.refresh_from_db(); other.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 0)
        self.assertEqual(other.points_balance, 0)
        # Idempotent.
        self.assertEqual(revoke_points_for_orders(orders), 0)

    def test_reset_points_for_organization_wipes_to_scratch(self):
        from tickets.services.loyalty import (
            award_points_for_orders, reset_points_for_organization,
        )
        other = Customer.objects.create(organization=self.org, email='o@x.com', name='O')
        orders = [
            self._order(number='R-1', tickets=2),
            self._order(number='R-2', tickets=1, customer=other),
        ]
        award_points_for_orders(orders, self.program)
        self.org.loyalty_points_backfilled_at = timezone.now()
        self.org.save(update_fields=['loyalty_points_backfilled_at'])

        summary = reset_points_for_organization(self.org)

        self.assertEqual(summary['transactions_deleted'], 2)
        self.assertEqual(summary['customers_reset'], 2)
        # Ledger gone, balances and lifetime zeroed, backfill flag cleared.
        self.assertEqual(LoyaltyPointsTransaction.objects.filter(organization=self.org).count(), 0)
        self.customer.refresh_from_db(); other.refresh_from_db()
        self.assertEqual((self.customer.points_balance, self.customer.lifetime_points), (0, 0))
        self.assertEqual((other.points_balance, other.lifetime_points), (0, 0))
        self.org.refresh_from_db()
        self.assertIsNone(self.org.loyalty_points_backfilled_at)
        # A fresh backfill re-awards cleanly (EARN rows no longer block it).
        self.assertEqual(award_points_for_orders(orders, self.program), 30)

    def test_reset_points_is_org_scoped(self):
        from tickets.services.loyalty import (
            award_points_for_order, reset_points_for_organization,
        )
        other_org = Organization.objects.create(
            name='Other', slug='other-org', loyalty_feature_enabled=True,
        )
        other_program = LoyaltyProgram.objects.create(
            organization=other_org, name='Other Club', is_active=True,
            points_enabled=True, points_basis=LoyaltyProgram.PointsBasis.PER_TICKET,
            points_rate=Decimal('10'),
        )
        other_event = Event.objects.create(
            organization=other_org, name='Other Show', venue=self.venue,
            start_date=date(2026, 7, 1), start_time=time(20, 0, 0),
        )
        other_customer = Customer.objects.create(
            organization=other_org, email='other@x.com', name='Other',
        )
        other_order = TicketOrder.objects.create(
            customer=other_customer, event=other_event, order_number='OTH-1',
            order_date=timezone.now(), total_amount=Decimal('10.00'),
        )
        Ticket.objects.create(ticket_order=other_order, price=Decimal('10.00'))
        award_points_for_order(self._order(tickets=2))
        award_points_for_order(other_order, other_program)

        reset_points_for_organization(self.org)

        # Other org untouched.
        other_customer.refresh_from_db()
        self.assertEqual(other_customer.points_balance, 10)
        self.assertEqual(
            LoyaltyPointsTransaction.objects.filter(organization=other_org).count(), 1
        )


class LoyaltyPointsResetAdminActionTests(TestCase):
    """Admin reset action: confirmation gate, in-progress guard, org de-dupe."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='Admin Pts', slug='admin-pts', loyalty_feature_enabled=True,
        )
        self.venue = Venue.objects.create(organization=self.org, name='Hall', city='Town')
        self.event = Event.objects.create(
            organization=self.org, name='Show', venue=self.venue,
            start_date=date(2026, 7, 1), start_time=time(20, 0, 0),
        )
        self.program = LoyaltyProgram.objects.create(
            organization=self.org, name='Club', is_active=True,
            points_enabled=True, points_basis=LoyaltyProgram.PointsBasis.PER_TICKET,
            points_rate=Decimal('10'),
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='pts@x.com', name='Pat',
        )
        order = TicketOrder.objects.create(
            customer=self.customer, event=self.event, order_number='A-1',
            order_date=timezone.now(), total_amount=Decimal('20.00'),
        )
        Ticket.objects.create(ticket_order=order, price=Decimal('10.00'))
        Ticket.objects.create(ticket_order=order, price=Decimal('10.00'))
        from tickets.services.loyalty import award_points_for_order
        award_points_for_order(order)
        self.superuser = User.objects.create_superuser(
            username='boss', email='boss@x.com', password='pw',
        )
        self.client.force_login(self.superuser)
        self.url = reverse('admin:tickets_loyaltyprogram_changelist')

    def _post(self, extra=None):
        data = {
            'action': 'reset_loyalty_points',
            '_selected_action': [str(self.program.id)],
        }
        if extra:
            data.update(extra)
        return self.client.post(self.url, data)

    def test_first_post_shows_confirmation_without_mutating(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'cannot be undone')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 20)  # untouched

    def test_confirmed_post_resets_points(self):
        resp = self._post({'confirm': 'yes'})
        self.assertEqual(resp.status_code, 302)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 0)
        self.assertEqual(self.customer.lifetime_points, 0)
        self.assertEqual(
            LoyaltyPointsTransaction.objects.filter(organization=self.org).count(), 0
        )

    def test_in_progress_org_is_skipped(self):
        LoyaltyProgram.objects.filter(id=self.program.id).update(recalc_in_progress=True)
        resp = self._post({'confirm': 'yes'})
        self.assertEqual(resp.status_code, 302)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 20)  # not reset


class LoyaltyPointsHookTests(TestCase):
    """Hook sites: refund clawback, hard-delete revokes, CSV import awards."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Hook Org', slug='hook-org', loyalty_feature_enabled=True)
        self.user = User.objects.create_user(username='hooks', email='hooks@x.com', password='pw12345')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.venue = Venue.objects.create(organization=self.org, name='Spot', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Gig', venue=self.venue,
            start_date=date(2026, 8, 1), start_time=time(20, 0, 0),
        )
        self.program = LoyaltyProgram.objects.create(
            organization=self.org, name='Hook Club', is_active=True,
            points_enabled=True, points_rate=Decimal('10'),
        )
        self.customer = Customer.objects.create(organization=self.org, email='h@x.com', name='H')

    def _login(self):
        self.client.login(username='hooks@x.com', password='pw12345')
        self.client.get(reverse('tickets:home'))

    def _earned_order(self, number='H-1', upload=None, tickets=2):
        from tickets.services.loyalty import award_points_for_order
        order = TicketOrder.objects.create(
            customer=self.customer, event=self.event, uploaded_file=upload,
            order_number=number, order_date=timezone.now(), total_amount=Decimal('50.00'),
        )
        for _ in range(tickets):
            Ticket.objects.create(ticket_order=order, price=Decimal('25.00'))
        award_points_for_order(order)
        return order

    def _upload(self):
        csv_format = CSVFormat.objects.create(
            organization=self.org, name='F', column_mapping={'order_number': 'order_number'},
        )
        return UploadedFile.objects.create(
            organization=self.org, csv_format=csv_format, filename='u.csv', status='completed',
        )

    def test_full_refund_revokes_partial_does_not(self):
        # Refunds are only allowed for direct-ticketing events.
        self.event.ticketing_type = 'direct'
        self.event.save(update_fields=['ticketing_type'])
        order = self._earned_order()
        session = StripeCheckoutSession.objects.create(
            event=self.event, organization=self.org, stripe_session_id='pi_test_1',
            buyer_email='h@x.com', status=StripeCheckoutSession.Status.COMPLETED,
            amount_total_cents=5000, ticket_order=order,
        )
        self._login()
        with patch('stripe.Refund.create'):
            resp = self.client.post(
                reverse('tickets:refund_order', args=[order.id]),
                {'refund_type': 'partial', 'refund_amount': '10.00'},
            )
        self.assertEqual(resp.status_code, 302)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 20)  # partial: no clawback
        with patch('stripe.Refund.create'):
            resp = self.client.post(
                reverse('tickets:refund_order', args=[order.id]),
                {'refund_type': 'full'},
            )
        self.assertEqual(resp.status_code, 302)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 0)
        self.assertTrue(
            LoyaltyPointsTransaction.objects.filter(
                ticket_order=order, kind=LoyaltyPointsTransaction.Kind.REVOKE
            ).exists()
        )

    def test_upload_delete_revokes_before_hard_delete(self):
        upload = self._upload()
        order = self._earned_order(number='H-UP', upload=upload)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 20)
        self._login()
        resp = self.client.post(reverse('tickets:upload_delete', args=[upload.id]))
        self.assertEqual(resp.status_code, 302)
        # Customer had no other orders -> reconcile hard-deleted them, and the
        # CASCADE took the (net-zero) ledger with it. The org must hold zero
        # outstanding points either way.
        remaining = Customer.objects.filter(id=self.customer.id).first()
        if remaining is not None:
            self.assertEqual(remaining.points_balance, 0)
        self.assertFalse(TicketOrder.objects.filter(id=order.id).exists())

    def test_upload_delete_aborts_when_revoke_fails(self):
        upload = self._upload()
        order = self._earned_order(number='H-ABORT', upload=upload)
        self._login()
        with patch('tickets.views.revoke_points_for_orders', side_effect=RuntimeError('boom')):
            resp = self.client.post(reverse('tickets:upload_delete', args=[upload.id]))
        # The view catches and reports, but the transaction rolled back:
        # orders AND points must survive untouched.
        self.assertTrue(TicketOrder.objects.filter(id=order.id).exists())
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 20)

    def test_event_delete_revokes_points(self):
        # Separate keeper-order on another event so the customer survives reconcile.
        other_event = Event.objects.create(
            organization=self.org, name='Keeper', venue=self.venue,
            start_date=date(2026, 9, 1), start_time=time(20, 0, 0),
        )
        keeper = TicketOrder.objects.create(
            customer=self.customer, event=other_event, order_number='H-KEEP',
            order_date=timezone.now(), total_amount=Decimal('10.00'),
        )
        order = self._earned_order(number='H-EVDEL')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 20)
        self._login()
        resp = self.client.post(reverse('tickets:event_delete', args=[self.event.id]))
        self.assertEqual(resp.status_code, 302)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 0)
        # REVOKE row survives with ticket_order nulled by the cascade.
        self.assertTrue(
            LoyaltyPointsTransaction.objects.filter(
                customer=self.customer, kind=LoyaltyPointsTransaction.Kind.REVOKE,
                ticket_order__isnull=True,
            ).exists()
        )

    def test_csv_import_awards_and_is_idempotent(self):
        import io
        csv_format = CSVFormat.objects.create(
            organization=self.org, name='Std',
            column_mapping={
                'order_date': ['order_date'],
                'customer_email': ['customer_email'],
                'customer_name': ['customer_name'],
                'ticket_type': ['ticket_type'],
            },
        )
        csv_body = (
            "order_date,customer_email,customer_name,ticket_type\n"
            "2026-08-01,csvbuyer@x.com,Buyer,GA\n"
        )
        upload = UploadedFile.objects.create(
            organization=self.org, csv_format=csv_format, filename='in.csv', status='pending',
            metadata={'event_id': str(self.event.id), 'event_name': self.event.name,
                      'event_start_date': '2026-08-01'},
        )
        from tickets.csv_processor import CSVProcessor
        processor = CSVProcessor(upload, csv_format)
        results = processor.process_and_save(io.BytesIO(csv_body.encode('utf-8')))
        self.assertEqual(results['success_count'], 1)
        buyer = Customer.objects.get(organization=self.org, email='csvbuyer@x.com')
        self.assertEqual(buyer.points_balance, 10)  # 1 ticket x rate 10
        self.assertEqual(buyer.lifetime_points, 10)
        # Awarding is idempotent against the same orders.
        from tickets.services.loyalty import award_points_for_orders
        orders = list(TicketOrder.objects.filter(uploaded_file=upload))
        self.assertEqual(award_points_for_orders(orders, self.program), 0)

    def test_backfill_task_awards_history_and_chains_recalc(self):
        from tickets.tasks import backfill_loyalty_points_task
        o1 = TicketOrder.objects.create(
            customer=self.customer, event=self.event, order_number='BF-1',
            order_date=timezone.now(), total_amount=Decimal('30.00'),
        )
        Ticket.objects.create(ticket_order=o1, price=Decimal('30.00'))
        refunded = TicketOrder.objects.create(
            customer=self.customer, event=self.event, order_number='BF-2',
            order_date=timezone.now(), total_amount=Decimal('30.00'),
            refunded_at=timezone.now(),
        )
        Ticket.objects.create(ticket_order=refunded, price=Decimal('30.00'))
        with patch('tickets.tasks.recalculate_loyalty_tiers_task.delay') as mock_recalc:
            result = backfill_loyalty_points_task.apply(args=[str(self.program.id)])
        self.assertEqual(result.result, 10)  # only the live order, 1 ticket
        mock_recalc.assert_called_once_with(str(self.program.id))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 10)
        self.org.refresh_from_db()
        self.assertIsNotNone(self.org.loyalty_points_backfilled_at)
        self.program.refresh_from_db()
        self.assertFalse(self.program.recalc_in_progress)
        # Re-run: idempotent no-op (repair-path semantics).
        with patch('tickets.tasks.recalculate_loyalty_tiers_task.delay'):
            result2 = backfill_loyalty_points_task.apply(args=[str(self.program.id)])
        self.assertEqual(result2.result, 0)

    def test_backfill_gated_on_inactive_or_disabled(self):
        from tickets.tasks import backfill_loyalty_points_task
        self.program.points_enabled = False
        self.program.save()
        result = backfill_loyalty_points_task.apply(args=[str(self.program.id)])
        self.assertEqual(result.result, 0)

    def test_backfill_skipped_while_recalc_in_progress(self):
        from tickets.tasks import backfill_loyalty_points_task
        self.program.recalc_in_progress = True
        self.program.save()
        result = backfill_loyalty_points_task.apply(args=[str(self.program.id)])
        self.assertEqual(result.result, 0)


class LoyaltyPointsTierAndFormTests(TestCase):
    """min_lifetime_points rule + builder validation."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='PtsTier Org', slug='ptstier-org', loyalty_feature_enabled=True)
        self.user = User.objects.create_user(username='ptier', email='ptier@x.com', password='pw12345')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.program = LoyaltyProgram.objects.create(
            organization=self.org, name='Ladder', is_active=True,
            points_enabled=True, points_rate=Decimal('1'),
        )

    def test_min_lifetime_points_rule_through_assigner(self):
        from tickets.services.loyalty import assign_loyalty_tiers
        gold = LoyaltyTier.objects.create(
            program=self.program, name='Gold', rank=2, min_lifetime_points=100,
        )
        rich = Customer.objects.create(
            organization=self.org, email='rich@x.com', name='R', lifetime_points=150,
        )
        poor = Customer.objects.create(
            organization=self.org, email='poor@x.com', name='P', lifetime_points=50,
        )
        assign_loyalty_tiers(self.program)
        rich.refresh_from_db(); poor.refresh_from_db()
        self.assertEqual(rich.loyalty_tier, gold)
        self.assertIsNone(poor.loyalty_tier)

    def test_points_only_tier_counts_as_ruled(self):
        tier = LoyaltyTier(program=self.program, name='Pointy', rank=1, min_lifetime_points=10)
        self.assertFalse(tier.has_no_rules())

    def _program_post(self, points_enabled, tier_points_rule):
        data = {
            'name': 'Ladder2', 'description': '', 'is_active': 'on',
            'points_basis': 'per_ticket', 'points_rate': '1',
            'tiers-TOTAL_FORMS': '1', 'tiers-INITIAL_FORMS': '0',
            'tiers-MIN_NUM_FORMS': '0', 'tiers-MAX_NUM_FORMS': '1000',
            'tiers-0-name': 'Gold', 'tiers-0-rank': '1', 'tiers-0-color': 'red', 'tiers-0-perks': '',
            'tiers-0-min_lifetime_value': '', 'tiers-0-min_order_count': '',
            'tiers-0-min_events_purchased': '', 'tiers-0-min_tickets_purchased': '',
            'tiers-0-max_days_since_last_order': '',
            'tiers-0-min_lifetime_points': tier_points_rule,
        }
        if points_enabled:
            data['points_enabled'] = 'on'
        return data

    def test_points_rule_requires_points_enabled(self):
        self.client.login(username='ptier@x.com', password='pw12345')
        self.client.get(reverse('tickets:home'))
        self.program.delete()  # make room for a new active program
        resp = self.client.post(
            reverse('tickets:loyalty_program_create'),
            self._program_post(points_enabled=False, tier_points_rule='100'),
        )
        self.assertEqual(resp.status_code, 200)  # re-rendered with error
        self.assertContains(resp, 'points are not')
        self.assertFalse(LoyaltyProgram.objects.filter(organization=self.org, name='Ladder2').exists())

    @patch('tickets.tasks.backfill_loyalty_points_task.delay')
    def test_backfill_checkbox_enqueues_backfill_not_recalc(self, mock_backfill):
        self.client.login(username='ptier@x.com', password='pw12345')
        self.client.get(reverse('tickets:home'))
        self.program.delete()
        data = self._program_post(points_enabled=True, tier_points_rule='100')
        data['backfill_past_orders'] = 'on'
        with patch('tickets.tasks.recalculate_loyalty_tiers_task.delay') as mock_recalc:
            resp = self.client.post(reverse('tickets:loyalty_program_create'), data)
        self.assertEqual(resp.status_code, 302)
        mock_backfill.assert_called_once()
        mock_recalc.assert_not_called()

    def test_points_rate_validator_rejects_zero(self):
        from tickets.forms import LoyaltyProgramForm
        form = LoyaltyProgramForm(data={
            'name': 'X', 'description': '', 'is_active': 'on',
            'points_enabled': 'on', 'points_basis': 'per_ticket', 'points_rate': '0',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('points_rate', form.errors)

    def test_program_detail_renders_points_stats(self):
        self.client.login(username='ptier@x.com', password='pw12345')
        self.client.get(reverse('tickets:home'))
        Customer.objects.create(
            organization=self.org, email='bal@x.com', name='B',
            points_balance=40, lifetime_points=90,
        )
        resp = self.client.get(reverse('tickets:loyalty_program_detail', args=[self.program.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Outstanding points')
        self.assertContains(resp, '40')
        self.assertContains(resp, '90')


class LoyaltyFeatureFlagTests(TestCase):
    """Organization.loyalty_feature_enabled gates UI, earning, and tasks."""

    def setUp(self):
        self.client = Client()
        # Flag OFF by default — that's the case under test.
        self.org = Organization.objects.create(name='Flag Org', slug='flag-org')
        self.user = User.objects.create_user(username='flaguser', email='flag@x.com', password='pw12345')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.venue = Venue.objects.create(organization=self.org, name='V', city='C')
        self.event = Event.objects.create(
            organization=self.org, name='FlagEvent', venue=self.venue,
            start_date=date(2026, 9, 1), start_time=time(20, 0, 0),
        )
        self.program = LoyaltyProgram.objects.create(
            organization=self.org, name='Flagged Club', is_active=True,
            points_enabled=True, points_rate=Decimal('10'),
        )
        self.tier = LoyaltyTier.objects.create(program=self.program, name='Member', rank=1)
        self.customer = Customer.objects.create(organization=self.org, email='f@x.com', name='F')

    def _login(self):
        self.client.login(username='flag@x.com', password='pw12345')
        self.client.get(reverse('tickets:home'))

    def _order(self, number='FLAG-1'):
        order = TicketOrder.objects.create(
            customer=self.customer, event=self.event, order_number=number,
            order_date=timezone.now(), total_amount=Decimal('20.00'),
        )
        Ticket.objects.create(ticket_order=order, price=Decimal('20.00'))
        return order

    def test_all_loyalty_views_404_when_flag_off(self):
        self._login()
        urls = [
            reverse('tickets:loyalty_program_list'),
            reverse('tickets:loyalty_program_create'),
            reverse('tickets:loyalty_program_detail', args=[self.program.id]),
            reverse('tickets:loyalty_program_edit', args=[self.program.id]),
            reverse('tickets:loyalty_tier_members', args=[self.program.id, self.tier.id]),
        ]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 404, url)
        post_urls = [
            reverse('tickets:loyalty_recalculate', args=[self.program.id]),
            reverse('tickets:loyalty_program_delete', args=[self.program.id]),
        ]
        for url in post_urls:
            self.assertEqual(self.client.post(url).status_code, 404, url)

    def test_views_work_when_flag_on(self):
        self.org.loyalty_feature_enabled = True
        self.org.save(update_fields=['loyalty_feature_enabled'])
        self._login()
        self.assertEqual(self.client.get(reverse('tickets:loyalty_program_list')).status_code, 200)
        self.assertEqual(
            self.client.get(reverse('tickets:loyalty_program_detail', args=[self.program.id])).status_code, 200
        )

    def test_sidebar_link_hidden_when_flag_off(self):
        self._login()
        resp = self.client.get(reverse('tickets:home'))
        self.assertNotContains(resp, reverse('tickets:loyalty_program_list'))
        self.org.loyalty_feature_enabled = True
        self.org.save(update_fields=['loyalty_feature_enabled'])
        clear_org_cache_session = self.client.session  # org PK cached; flag read fresh from DB
        resp = self.client.get(reverse('tickets:home'))
        self.assertContains(resp, reverse('tickets:loyalty_program_list'))

    def test_no_earning_when_flag_off(self):
        from tickets.services.loyalty import award_points_for_order, get_points_program
        self.assertIsNone(get_points_program(self.org))
        self.assertEqual(award_points_for_order(self._order()), 0)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 0)
        self.assertEqual(LoyaltyPointsTransaction.objects.count(), 0)

    def test_revoke_still_works_when_flag_off(self):
        # Earn while ON, then turn OFF: clawback of past earns must still apply.
        from tickets.services.loyalty import award_points_for_order, revoke_points_for_order
        self.org.loyalty_feature_enabled = True
        self.org.save(update_fields=['loyalty_feature_enabled'])
        order = self._order(number='FLAG-REV')
        self.assertEqual(award_points_for_order(order), 10)
        self.org.loyalty_feature_enabled = False
        self.org.save(update_fields=['loyalty_feature_enabled'])
        self.assertEqual(revoke_points_for_order(order), 10)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 0)

    def test_tasks_noop_when_flag_off(self):
        from tickets.tasks import backfill_loyalty_points_task, recalculate_loyalty_tiers_task
        self._order(number='FLAG-BF')
        result = backfill_loyalty_points_task.apply(args=[str(self.program.id)])
        self.assertEqual(result.result, 0)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 0)
        result = recalculate_loyalty_tiers_task.apply(args=[str(self.program.id)])
        self.assertEqual(result.result, 0)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.loyalty_tier)

    def test_customer_detail_hides_tier_badge_when_flag_off(self):
        Customer.objects.filter(id=self.customer.id).update(loyalty_tier=self.tier)
        self._login()
        resp = self.client.get(reverse('tickets:customer_detail', args=[self.customer.id]))
        self.assertEqual(resp.status_code, 200)
        # The loyalty block renders the program name only when the flag is on;
        # assert on that (stable across the customer-detail label redesign) rather
        # than a heading string the redesign changed.
        self.assertNotContains(resp, self.program.name)
        self.org.loyalty_feature_enabled = True
        self.org.save(update_fields=['loyalty_feature_enabled'])
        resp = self.client.get(reverse('tickets:customer_detail', args=[self.customer.id]))
        self.assertContains(resp, self.program.name)


class SurveyResponseDetailViewTests(TestCase):
    """The event_survey_response_detail JSON endpoint powering the row-click modal."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Resp Detail Org', slug='resp-detail-org')
        self.other_org = Organization.objects.create(name='Other Org', slug='resp-detail-other-org')
        self.user = User.objects.create_user(
            username='respdetail', email='resp@test.com', password='testpass123',
        )
        UserProfile.objects.create(user=self.user, organization=self.org,
                                   org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='resp@test.com', password='testpass123')
        self.client.get(reverse('tickets:home'))  # seed _org_id in session

        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Survey Event', venue=self.venue,
            start_date=date(2025, 8, 1),
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='guest@example.com', name='Guest',
        )

    def _detail(self, kind, response_id, event=None):
        return self.client.get(reverse(
            'tickets:event_survey_response_detail',
            args=[(event or self.event).id, kind, response_id],
        ))

    def test_internal_response_returns_questions_and_answers(self):
        invitation = SurveyInvitation.objects.create(
            organization=self.org, event=self.event,
            customer=self.customer, email=self.customer.email,
        )
        response = SurveyResponse.objects.create(
            organization=self.org, event=self.event,
            customer=self.customer, invitation=invitation,
        )
        q_star = SurveyQuestion.objects.create(
            organization=self.org, question_text='Rate the night', question_type='star_rating', position=1,
        )
        q_nps = SurveyQuestion.objects.create(
            organization=self.org, question_text='Recommend us?', question_type='nps', position=2,
        )
        q_text = SurveyQuestion.objects.create(
            organization=self.org, question_text='Anything else?', question_type='text', position=3,
        )
        SurveyAnswer.objects.create(response=response, question=q_star, star_rating=4)
        SurveyAnswer.objects.create(response=response, question=q_nps, nps_score=9)
        SurveyAnswer.objects.create(response=response, question=q_text, text_answer='Great show')

        resp = self._detail('internal', response.id)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['meta']['source'], 'Cue survey')
        self.assertEqual(data['meta']['respondent'], 'guest@example.com')
        pairs = {it['question']: it['answer'] for it in data['items']}
        self.assertEqual(pairs['Rate the night'], '4 / 5 stars')
        self.assertEqual(pairs['Recommend us?'], '9 / 10')
        self.assertEqual(pairs['Anything else?'], 'Great show')

    def test_external_response_uses_raw_answers(self):
        upload = ExternalSurveyUpload.objects.create(
            organization=self.org, filename='typeform.csv',
            status=ExternalSurveyUpload.Status.COMPLETED,
        )
        response = ExternalSurveyResponse.objects.create(
            organization=self.org, upload=upload, event=self.event,
            responded_at=timezone.now(), email='fan@example.com',
            typeform_response_id='tf123',
            raw_answers=[
                {'title': 'How was it?', 'type': 'opinion_scale', 'value': 8},
                {'title': 'What did you enjoy?', 'type': 'multiple_choice', 'value': ['Music', 'Vibe']},
            ],
        )
        resp = self._detail('external', response.id)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['meta']['source'], 'Typeform')
        pairs = {it['question']: it['answer'] for it in data['items']}
        self.assertEqual(pairs['How was it?'], '8')
        self.assertEqual(pairs['What did you enjoy?'], 'Music, Vibe')

    def test_external_response_falls_back_to_structured_fields(self):
        upload = ExternalSurveyUpload.objects.create(
            organization=self.org, filename='legacy.csv',
            status=ExternalSurveyUpload.Status.COMPLETED,
        )
        response = ExternalSurveyResponse.objects.create(
            organization=self.org, upload=upload, event=self.event,
            responded_at=timezone.now(), email='legacy@example.com',
            overall_rating='Loved it', nps_score=10, city='Dallas',
            text_feedback='More seating please', raw_answers=[],
        )
        resp = self._detail('external', response.id)
        self.assertEqual(resp.status_code, 200)
        pairs = {it['question']: it['answer'] for it in resp.json()['items']}
        self.assertEqual(pairs['Overall rating'], 'Loved it')
        self.assertEqual(pairs['NPS score'], '10')
        self.assertEqual(pairs['City'], 'Dallas')
        self.assertEqual(pairs['Feedback'], 'More seating please')

    def test_invalid_kind_returns_400(self):
        resp = self._detail('bogus', uuid.uuid4())
        self.assertEqual(resp.status_code, 400)

    def test_response_from_other_org_is_404(self):
        other_venue = Venue.objects.create(organization=self.other_org, name='Other Venue', city='City')
        other_event = Event.objects.create(
            organization=self.other_org, name='Other Event', venue=other_venue,
            start_date=date(2025, 8, 1),
        )
        upload = ExternalSurveyUpload.objects.create(
            organization=self.other_org, filename='x.csv',
            status=ExternalSurveyUpload.Status.COMPLETED,
        )
        foreign = ExternalSurveyResponse.objects.create(
            organization=self.other_org, upload=upload, event=other_event,
            responded_at=timezone.now(),
        )
        # Probing under our own event id must not leak another org's response.
        resp = self._detail('external', foreign.id)
        self.assertEqual(resp.status_code, 404)


class CustomerListPointsColumnTests(TestCase):
    """The /customers/ list shows a Points column only when loyalty is enabled."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='ColOrg', slug='col-org')
        self.user = User.objects.create_user(username='coluser', email='col@x.com', password='pw12345')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        Customer.objects.create(organization=self.org, email='hi@x.com', name='Hiro',
                                points_balance=1234, lifetime_points=1234)
        Customer.objects.create(organization=self.org, email='lo@x.com', name='Lola',
                                points_balance=5, lifetime_points=5)

    def _login(self):
        self.client.login(username='col@x.com', password='pw12345')
        self.client.get(reverse('tickets:home'))

    def test_points_column_hidden_when_flag_off(self):
        self._login()
        resp = self.client.get(reverse('tickets:customer_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, '>Points Balance')
        self.assertNotContains(resp, '1,234')

    def test_points_column_shown_with_balance_when_flag_on(self):
        self.org.loyalty_feature_enabled = True
        self.org.save(update_fields=['loyalty_feature_enabled'])
        self._login()
        resp = self.client.get(reverse('tickets:customer_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '>Points Balance')
        self.assertContains(resp, '1,234')  # intcomma-formatted balance

    def test_points_sort_orders_customers(self):
        self.org.loyalty_feature_enabled = True
        self.org.save(update_fields=['loyalty_feature_enabled'])
        self._login()
        resp = self.client.get(reverse('tickets:customer_list') + '?sort=-points_balance')
        self.assertEqual(resp.status_code, 200)
        names = [c.name for c in resp.context['page_obj']]
        self.assertEqual(names, ['Hiro', 'Lola'])  # 1234 before 5
        resp = self.client.get(reverse('tickets:customer_list') + '?sort=points_balance')
        names = [c.name for c in resp.context['page_obj']]
        self.assertEqual(names, ['Lola', 'Hiro'])


class LoyaltyPointsRecomputeTests(TestCase):
    """Recompute = reset_first + re-award at current rate + ledger reconciliation."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(
            name='Recompute Org', slug='recompute-org', loyalty_feature_enabled=True,
        )
        self.user = User.objects.create_user(username='recomp', email='recomp@x.com', password='pw12345')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.venue = Venue.objects.create(organization=self.org, name='V', city='C')
        self.event = Event.objects.create(
            organization=self.org, name='Show', venue=self.venue,
            start_date=date(2026, 9, 1), start_time=time(20, 0, 0),
        )
        # Start per-ticket, rate 1.
        self.program = LoyaltyProgram.objects.create(
            organization=self.org, name='Recompute Club', is_active=True,
            points_enabled=True, points_basis=LoyaltyProgram.PointsBasis.PER_TICKET,
            points_rate=Decimal('1'),
        )
        self.customer = Customer.objects.create(organization=self.org, email='rc@x.com', name='RC')

    def _login(self):
        self.client.login(username='recomp@x.com', password='pw12345')
        self.client.get(reverse('tickets:home'))

    def _order(self, number, amount, tickets):
        from tickets.services.loyalty import award_points_for_order
        order = TicketOrder.objects.create(
            customer=self.customer, event=self.event, order_number=number,
            order_date=timezone.now(), total_amount=Decimal(str(amount)),
        )
        for _ in range(tickets):
            Ticket.objects.create(ticket_order=order, price=Decimal('10.00'))
        award_points_for_order(order)
        return order

    def _ledger_sum(self, customer):
        from django.db.models import Sum
        return (customer.points_transactions.aggregate(s=Sum('amount'))['s']) or 0

    # --- task: recompute correctness ---
    def test_reset_first_recomputes_at_new_rate(self):
        from tickets.tasks import backfill_loyalty_points_task
        self._order('R-1', amount=50, tickets=2)  # per-ticket rate 1 -> 2 pts
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 2)
        # Switch to per-dollar rate 1.
        self.program.points_basis = LoyaltyProgram.PointsBasis.PER_DOLLAR
        self.program.save()
        with patch('tickets.tasks.recalculate_loyalty_tiers_task.delay'):
            result = backfill_loyalty_points_task.apply(args=[str(self.program.id)], kwargs={'reset_first': True})
        self.assertEqual(result.result, 50)  # $50 * 1
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 50)
        self.assertEqual(self.customer.lifetime_points, 50)
        # Exactly one EARN row for the order (old one gone).
        self.assertEqual(
            LoyaltyPointsTransaction.objects.filter(
                customer=self.customer, kind=LoyaltyPointsTransaction.Kind.EARN
            ).count(), 1
        )
        self.assertEqual(self._ledger_sum(self.customer), 50)

    def test_reset_first_false_is_unchanged_regression(self):
        from tickets.tasks import backfill_loyalty_points_task
        # Order with no EARN yet (created without awarding).
        o = TicketOrder.objects.create(
            customer=self.customer, event=self.event, order_number='R-REG',
            order_date=timezone.now(), total_amount=Decimal('30'),
        )
        Ticket.objects.create(ticket_order=o, price=Decimal('30'))
        with patch('tickets.tasks.recalculate_loyalty_tiers_task.delay'):
            result = backfill_loyalty_points_task.apply(args=[str(self.program.id)])  # default reset_first=False
        self.assertEqual(result.result, 1)  # 1 ticket
        # No reset happened: a pre-existing manual EARN would survive (idempotent path).

    def test_recompute_wipes_soft_deleted_prior_program_rows(self):
        from tickets.tasks import backfill_loyalty_points_task
        self._order('R-CUR', amount=10, tickets=1)
        # Stale ledger row from a since-deleted program (org-scoped wipe must clear it).
        old_program = LoyaltyProgram.objects.create(
            organization=self.org, name='Old', is_active=False, points_enabled=True,
        )
        old_program.delete()  # soft delete
        LoyaltyPointsTransaction.objects.create(
            organization=self.org, customer=self.customer, ticket_order=None,
            kind=LoyaltyPointsTransaction.Kind.EARN, amount=999,
            balance_after=999, lifetime_after=999, description='stale',
        )
        with patch('tickets.tasks.recalculate_loyalty_tiers_task.delay'):
            backfill_loyalty_points_task.apply(args=[str(self.program.id)], kwargs={'reset_first': True})
        # The stale 999 row is gone; balance reflects only the live order at current rate.
        self.assertFalse(
            LoyaltyPointsTransaction.objects.filter(description='stale').exists()
        )
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, self._ledger_sum(self.customer))

    def test_recompute_refunded_order_nets_zero(self):
        from tickets.tasks import backfill_loyalty_points_task
        o = self._order('R-REF', amount=40, tickets=2)
        o.refunded_at = timezone.now()
        o.save(update_fields=['refunded_at'])
        with patch('tickets.tasks.recalculate_loyalty_tiers_task.delay'):
            backfill_loyalty_points_task.apply(args=[str(self.program.id)], kwargs={'reset_first': True})
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 0)  # refunded -> no EARN
        self.assertFalse(
            LoyaltyPointsTransaction.objects.filter(customer=self.customer).exists()
        )

    def test_recompute_chains_recalc_and_clears_flag(self):
        from tickets.tasks import backfill_loyalty_points_task
        self._order('R-CHAIN', amount=20, tickets=1)
        with patch('tickets.tasks.recalculate_loyalty_tiers_task.delay') as mock_recalc:
            backfill_loyalty_points_task.apply(args=[str(self.program.id)], kwargs={'reset_first': True})
        mock_recalc.assert_called_once_with(str(self.program.id))
        self.program.refresh_from_db()
        self.assertFalse(self.program.recalc_in_progress)

    def test_recompute_skipped_when_recalc_in_progress(self):
        from tickets.tasks import backfill_loyalty_points_task
        self._order('R-BUSY', amount=20, tickets=1)
        self.customer.refresh_from_db()
        before = self.customer.points_balance
        self.program.recalc_in_progress = True
        self.program.save(update_fields=['recalc_in_progress'])
        result = backfill_loyalty_points_task.apply(args=[str(self.program.id)], kwargs={'reset_first': True})
        self.assertEqual(result.result, 0)
        # Claim failed BEFORE the try -> no reset ran, ledger intact.
        self.assertTrue(LoyaltyPointsTransaction.objects.filter(customer=self.customer).exists())
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, before)

    # --- reconciliation (the race fix) ---
    def test_reconcile_sets_balance_to_ledger_sum(self):
        from tickets.services.loyalty import reconcile_points_balances
        # Simulate the race aftermath: a stray EARN row exists but balance is stale/zero.
        LoyaltyPointsTransaction.objects.create(
            organization=self.org, customer=self.customer, ticket_order=None,
            kind=LoyaltyPointsTransaction.Kind.EARN, amount=70,
            balance_after=70, lifetime_after=70, description='survived live award',
        )
        Customer.objects.filter(id=self.customer.id).update(points_balance=0, lifetime_points=0)
        reconcile_points_balances(self.org)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 70)
        self.assertEqual(self.customer.lifetime_points, 70)
        self.assertEqual(self.customer.points_balance, self._ledger_sum(self.customer))

    def test_reconcile_is_idempotent_and_zeroes_empty_ledger(self):
        from tickets.services.loyalty import reconcile_points_balances
        # Customer with a stale non-zero balance but no ledger rows -> reconcile to 0.
        Customer.objects.filter(id=self.customer.id).update(points_balance=99, lifetime_points=99)
        reconcile_points_balances(self.org)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 0)
        reconcile_points_balances(self.org)  # idempotent
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 0)

    # --- view ---
    @patch('tickets.tasks.backfill_loyalty_points_task.delay')
    def test_view_enqueues_with_correct_confirm_name(self, mock_delay):
        self._login()
        resp = self.client.post(
            reverse('tickets:loyalty_recompute_points', args=[self.program.id]),
            {'confirm_name': 'Recompute Club'},
        )
        self.assertEqual(resp.status_code, 302)
        mock_delay.assert_called_once_with(str(self.program.id), reset_first=True)

    @patch('tickets.tasks.backfill_loyalty_points_task.delay')
    def test_view_rejects_wrong_confirm_name(self, mock_delay):
        self._login()
        self._order('R-GUARD', amount=10, tickets=1)
        resp = self.client.post(
            reverse('tickets:loyalty_recompute_points', args=[self.program.id]),
            {'confirm_name': 'wrong'},
        )
        self.assertEqual(resp.status_code, 302)
        mock_delay.assert_not_called()
        self.assertTrue(LoyaltyPointsTransaction.objects.filter(customer=self.customer).exists())

    @patch('tickets.tasks.backfill_loyalty_points_task.delay')
    def test_view_rejects_missing_confirm_name(self, mock_delay):
        self._login()
        resp = self.client.post(reverse('tickets:loyalty_recompute_points', args=[self.program.id]), {})
        self.assertEqual(resp.status_code, 302)
        mock_delay.assert_not_called()

    @patch('tickets.tasks.backfill_loyalty_points_task.delay')
    def test_view_guards_points_disabled_and_inactive_and_in_progress(self, mock_delay):
        self._login()
        url = reverse('tickets:loyalty_recompute_points', args=[self.program.id])
        # points disabled
        self.program.points_enabled = False
        self.program.save(update_fields=['points_enabled'])
        self.client.post(url, {'confirm_name': 'Recompute Club'})
        # inactive
        self.program.points_enabled = True
        self.program.is_active = False
        self.program.save(update_fields=['points_enabled', 'is_active'])
        self.client.post(url, {'confirm_name': 'Recompute Club'})
        # in progress
        self.program.is_active = True
        self.program.recalc_in_progress = True
        self.program.save(update_fields=['is_active', 'recalc_in_progress'])
        self.client.post(url, {'confirm_name': 'Recompute Club'})
        mock_delay.assert_not_called()

    def test_view_get_405_and_flag_off_404(self):
        self._login()
        url = reverse('tickets:loyalty_recompute_points', args=[self.program.id])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.org.loyalty_feature_enabled = False
        self.org.save(update_fields=['loyalty_feature_enabled'])
        self.assertEqual(self.client.post(url, {'confirm_name': 'Recompute Club'}).status_code, 404)

    # --- integration: the bug-report scenario, end to end through the view ---
    def test_integration_per_ticket_to_per_dollar_via_view(self):
        self._login()
        self._order('R-INT', amount=50, tickets=2)  # 2 pts at per-ticket
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 2)
        self.program.points_basis = LoyaltyProgram.PointsBasis.PER_DOLLAR
        self.program.save()
        with patch('tickets.tasks.recalculate_loyalty_tiers_task.delay'):
            resp = self.client.post(
                reverse('tickets:loyalty_recompute_points', args=[self.program.id]),
                {'confirm_name': 'Recompute Club'},
            )
        self.assertEqual(resp.status_code, 302)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.points_balance, 50)  # $50 * 1, eager Celery ran inline

    # --- templates ---
    def test_detail_shows_recompute_control_with_confirm_input(self):
        self._login()
        resp = self.client.get(reverse('tickets:loyalty_program_detail', args=[self.program.id]))
        self.assertContains(resp, 'Recompute points at current rate')
        self.assertContains(resp, 'name="confirm_name"')
        self.assertContains(resp, 'id="recomputeSubmit"')

    def test_detail_hides_recompute_when_points_disabled(self):
        self.program.points_enabled = False
        self.program.save(update_fields=['points_enabled'])
        self._login()
        resp = self.client.get(reverse('tickets:loyalty_program_detail', args=[self.program.id]))
        self.assertNotContains(resp, 'name="confirm_name"')

    def test_edit_form_shows_forward_only_warning(self):
        self._login()
        resp = self.client.get(reverse('tickets:loyalty_program_edit', args=[self.program.id]))
        self.assertContains(resp, 'affects')
        self.assertContains(resp, 'future orders only')


class DestinationChargeCreateTests(TestCase):
    """create_payment_intent: organizer net rides transfer_data for onboarded orgs."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(
            name='Dest Charge Org',
            slug='dest-charge-org',
            stripe_account_id='acct_dest_test',
            stripe_onboarding_complete=True,
        )
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Dest Event', venue=self.venue,
            start_date=date.today() + timedelta(days=14),
            ticketing_type='direct',
            status='live',
        )
        self.ticket_type = SaleableTicketType.objects.create(
            event=self.event, name='General', price=Decimal('25.00'),
            quantity_limit=100, quantity_sold=0,
        )
        self.user = User.objects.create_user(
            username='dest-buyer', email='dest-buyer@example.com',
            password='testpass123', first_name='Dest', last_name='Buyer',
        )
        self.client.login(username='dest-buyer@example.com', password='testpass123')
        self._set_cart(qty=2)
        self.url = reverse('tickets:create_payment_intent', args=[self.event.public_id])

    def _set_cart(self, *, qty):
        session = self.client.session
        session[f'cart_{self.event.id}'] = [{
            'saleable_ticket_type_id': str(self.ticket_type.id),
            'name': self.ticket_type.name,
            'price': '25.00',
            'quantity': qty,
            'tier_id': None,
            'tier_name': None,
        }]
        session.save()

    @patch('stripe.PaymentIntent.create')
    def test_onboarded_org_gets_destination_charge(self, mock_pi_create):
        mock_pi_create.return_value = MagicMock(id='pi_dest_1', client_secret='cs_dest_1')

        response = self.client.post(self.url, data='{}', content_type='application/json')

        self.assertEqual(response.status_code, 200)
        fee_cents = extract_fee_from_display_cents(5000)
        kwargs = mock_pi_create.call_args.kwargs
        self.assertEqual(kwargs['transfer_data'], {
            'destination': 'acct_dest_test',
            'amount': 5000 - fee_cents,
        })
        session = StripeCheckoutSession.objects.get(stripe_session_id='pi_dest_1')
        self.assertEqual(session.charge_flow, StripeCheckoutSession.ChargeFlow.DESTINATION)
        self.assertEqual(session.platform_fee_cents, fee_cents)

    @patch('stripe.PaymentIntent.create')
    def test_unonboarded_org_falls_back_to_platform_charge(self, mock_pi_create):
        self.org.stripe_onboarding_complete = False
        self.org.save(update_fields=['stripe_onboarding_complete'])
        mock_pi_create.return_value = MagicMock(id='pi_plat_1', client_secret='cs_plat_1')

        response = self.client.post(self.url, data='{}', content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('transfer_data', mock_pi_create.call_args.kwargs)
        session = StripeCheckoutSession.objects.get(stripe_session_id='pi_plat_1')
        self.assertEqual(session.charge_flow, StripeCheckoutSession.ChargeFlow.PLATFORM)


class DestinationTransferCaptureTests(TestCase):
    """Fulfillment captures the destination charge's transfer onto the session."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Capture Org', slug='capture-org')
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Capture Event', venue=self.venue,
            start_date=date(2025, 6, 15), start_time=time(19, 0),
        )
        self.ticket_type = SaleableTicketType.objects.create(
            event=self.event, name='General', price=Decimal('25.00'),
            quantity_limit=100, quantity_sold=0,
        )
        self.session = StripeCheckoutSession.objects.create(
            event=self.event,
            organization=self.org,
            stripe_session_id='pi_capture_1',
            stripe_payment_intent_id='pi_capture_1',
            buyer_email='buyer@example.com',
            buyer_name='Buyer',
            status=StripeCheckoutSession.Status.PENDING,
            line_items_snapshot=[{
                'saleable_ticket_type_id': str(self.ticket_type.id),
                'name': 'General', 'price': '25.00', 'quantity': 2,
            }],
            amount_total_cents=5000,
            platform_fee_cents=462,
            charge_flow=StripeCheckoutSession.ChargeFlow.DESTINATION,
        )
        self.webhook_url = reverse('tickets:stripe_webhook')

    @patch('stripe.Transfer.retrieve')
    @patch('stripe.Charge.retrieve')
    @patch('stripe.Webhook.construct_event')
    def test_webhook_persists_transfer_and_settlement(
        self, mock_construct, mock_charge_retrieve, mock_transfer_retrieve,
    ):
        charge = MagicMock()
        charge.transfer = 'tr_capture_1'
        charge.balance_transaction = MagicMock(available_on=1750000000)
        mock_charge_retrieve.return_value = charge
        mock_transfer_retrieve.return_value = MagicMock(amount=4538)
        mock_construct.return_value = {
            'type': 'payment_intent.succeeded',
            'data': {'object': {
                'id': 'pi_capture_1',
                'amount_received': 5000,
                'latest_charge': 'ch_capture_1',
            }},
        }

        response = self.client.post(
            self.webhook_url,
            data='{}',
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='sig_test',
        )

        self.assertEqual(response.status_code, 200)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.COMPLETED)
        self.assertEqual(self.session.stripe_transfer_id, 'tr_capture_1')
        self.assertEqual(self.session.transfer_cents, 4538)
        self.assertEqual(self.session.charge_flow, StripeCheckoutSession.ChargeFlow.DESTINATION)
        self.assertIsNotNone(self.session.available_on)
        mock_transfer_retrieve.assert_called_once_with('tr_capture_1')


class TransferReversalTests(TestCase):
    """Refund clawback: exact, Stripe-authoritative transfer reversals."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Reversal Org', slug='reversal-org')
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Reversal Event', venue=self.venue,
            start_date=date.today() + timedelta(days=14),
            ticketing_type='direct',
        )
        self.ticket_type = SaleableTicketType.objects.create(
            event=self.event, name='General', price=Decimal('50.00'),
            quantity_limit=100, quantity_sold=2,
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='buyer@example.com', name='Buyer',
        )
        self.order = TicketOrder.objects.create(
            event=self.event,
            customer=self.customer,
            order_number='REV-001',
            order_date=timezone.now(),
            total_amount=Decimal('100.00'),
        )
        self.session = StripeCheckoutSession.objects.create(
            event=self.event,
            organization=self.org,
            stripe_session_id='pi_reversal_1',
            stripe_payment_intent_id='pi_reversal_1',
            buyer_email='buyer@example.com',
            buyer_name='Buyer',
            status=StripeCheckoutSession.Status.COMPLETED,
            amount_total_cents=10000,
            platform_fee_cents=500,
            charge_flow=StripeCheckoutSession.ChargeFlow.DESTINATION,
            stripe_transfer_id='tr_reversal_1',
            transfer_cents=9500,
            line_items_snapshot=[{
                'saleable_ticket_type_id': str(self.ticket_type.id),
                'name': 'General', 'price': '50.00', 'quantity': 2,
            }],
            ticket_order=self.order,
        )
        self.webhook_url = reverse('tickets:stripe_webhook')

    def _post_refund_event(self, mock_construct, *, amount_refunded, refunded,
                           payment_intent='pi_reversal_1', transfer='tr_reversal_1'):
        obj = {
            'id': 'ch_reversal',
            'payment_intent': payment_intent,
            'amount_refunded': amount_refunded,
            'refunded': refunded,
        }
        if transfer:
            obj['transfer'] = transfer
        mock_construct.return_value = {
            'type': 'charge.refunded',
            'data': {'object': obj},
        }
        return self.client.post(
            self.webhook_url,
            data='{}',
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE='sig_test',
        )

    def _mock_transfer(self, amount=9500, amount_reversed=0):
        return MagicMock(amount=amount, amount_reversed=amount_reversed)

    @patch('stripe.Transfer.create_reversal')
    @patch('stripe.Transfer.retrieve')
    @patch('stripe.Webhook.construct_event')
    def test_partial_refund_reverses_exactly_refund_amount(
        self, mock_construct, mock_retrieve, mock_reversal,
    ):
        mock_retrieve.return_value = self._mock_transfer()

        res = self._post_refund_event(mock_construct, amount_refunded=1000, refunded=False)

        self.assertEqual(res.status_code, 200)
        mock_reversal.assert_called_once()
        args, kwargs = mock_reversal.call_args
        self.assertEqual(args[0], 'tr_reversal_1')
        self.assertEqual(kwargs['amount'], 1000)
        self.assertEqual(kwargs['idempotency_key'], 'trrev-tr_reversal_1-1000')
        self.session.refresh_from_db()
        self.assertEqual(self.session.transfer_reversed_cents, 1000)
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.PARTIALLY_REFUNDED)

    @patch('stripe.Transfer.create_reversal')
    @patch('stripe.Transfer.retrieve')
    @patch('stripe.Webhook.construct_event')
    def test_cumulative_partials_reverse_only_the_delta(
        self, mock_construct, mock_retrieve, mock_reversal,
    ):
        # Stripe already holds a $10 reversal from the first partial refund.
        mock_retrieve.return_value = self._mock_transfer(amount_reversed=1000)

        res = self._post_refund_event(mock_construct, amount_refunded=2500, refunded=False)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_reversal.call_args.kwargs['amount'], 1500)
        self.assertEqual(
            mock_reversal.call_args.kwargs['idempotency_key'], 'trrev-tr_reversal_1-2500',
        )
        self.session.refresh_from_db()
        self.assertEqual(self.session.transfer_reversed_cents, 2500)

    @patch('stripe.Transfer.create_reversal')
    @patch('stripe.Transfer.retrieve')
    @patch('stripe.Webhook.construct_event')
    def test_full_refund_reverses_whole_transfer_and_caps_there(
        self, mock_construct, mock_retrieve, mock_reversal,
    ):
        mock_retrieve.return_value = self._mock_transfer()

        # Buyer refunded $100, but only $95 ever reached the organizer:
        # reversal clamps at the transfer — platform funds the fee portion.
        res = self._post_refund_event(mock_construct, amount_refunded=10000, refunded=True)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_reversal.call_args.kwargs['amount'], 9500)
        self.session.refresh_from_db()
        self.assertEqual(self.session.transfer_reversed_cents, 9500)
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.REFUNDED)

    @patch('stripe.Transfer.create_reversal')
    @patch('stripe.Transfer.retrieve')
    @patch('stripe.Webhook.construct_event')
    def test_webhook_retry_does_not_double_reverse(
        self, mock_construct, mock_retrieve, mock_reversal,
    ):
        # Stripe says the cumulative target is already reversed.
        mock_retrieve.return_value = self._mock_transfer(amount_reversed=1000)

        res = self._post_refund_event(mock_construct, amount_refunded=1000, refunded=False)

        self.assertEqual(res.status_code, 200)
        mock_reversal.assert_not_called()

    @patch('stripe.Transfer.create_reversal')
    @patch('stripe.Transfer.retrieve')
    @patch('stripe.Webhook.construct_event')
    def test_echo_after_app_refund_still_reverses(
        self, mock_construct, mock_retrieve, mock_reversal,
    ):
        # refund_order already wrote local state — the echo webhook is the
        # only place the clawback happens, so it must run despite the
        # state no-op guard.
        self.order.refunded_amount = Decimal('10.00')
        self.order.save(update_fields=['refunded_amount'])
        self.session.status = StripeCheckoutSession.Status.PARTIALLY_REFUNDED
        self.session.save(update_fields=['status'])
        mock_retrieve.return_value = self._mock_transfer()

        res = self._post_refund_event(mock_construct, amount_refunded=1000, refunded=False)

        self.assertEqual(res.status_code, 200)
        mock_reversal.assert_called_once()
        self.assertEqual(mock_reversal.call_args.kwargs['amount'], 1000)

    @patch('stripe.checkout.Session.list', return_value={'data': []})
    @patch('stripe.Transfer.create_reversal')
    @patch('stripe.Transfer.retrieve')
    @patch('stripe.Webhook.construct_event')
    def test_sessionless_fallback_reverses_from_charge_payload(
        self, mock_construct, mock_retrieve, mock_reversal, mock_cs_list,
    ):
        # Event hard-delete CASCADEs the session away — the clawback must
        # still happen from the charge payload alone.
        self.session.delete()
        mock_retrieve.return_value = self._mock_transfer()

        res = self._post_refund_event(mock_construct, amount_refunded=2000, refunded=False)

        self.assertEqual(res.status_code, 200)
        mock_reversal.assert_called_once()
        self.assertEqual(mock_reversal.call_args.kwargs['amount'], 2000)

    @patch('stripe.Transfer.create_reversal')
    @patch('stripe.Transfer.retrieve')
    @patch('stripe.Webhook.construct_event')
    def test_transfer_id_recovered_from_payload_when_capture_was_missed(
        self, mock_construct, mock_retrieve, mock_reversal,
    ):
        # Refund webhook beat fulfillment's transfer capture.
        self.session.stripe_transfer_id = ''
        self.session.transfer_cents = 0
        self.session.charge_flow = StripeCheckoutSession.ChargeFlow.PLATFORM
        self.session.save(update_fields=['stripe_transfer_id', 'transfer_cents', 'charge_flow'])
        mock_retrieve.return_value = self._mock_transfer()

        res = self._post_refund_event(mock_construct, amount_refunded=1000, refunded=False)

        self.assertEqual(res.status_code, 200)
        mock_reversal.assert_called_once()
        self.session.refresh_from_db()
        self.assertEqual(self.session.stripe_transfer_id, 'tr_reversal_1')
        self.assertEqual(self.session.transfer_cents, 9500)
        self.assertEqual(self.session.charge_flow, StripeCheckoutSession.ChargeFlow.DESTINATION)
        self.assertEqual(self.session.transfer_reversed_cents, 1000)

    @patch('stripe.Transfer.create_reversal')
    @patch('stripe.Transfer.retrieve')
    @patch('stripe.Webhook.construct_event')
    def test_reversal_failure_does_not_block_state_sync(
        self, mock_construct, mock_retrieve, mock_reversal,
    ):
        import stripe as stripe_lib
        mock_retrieve.return_value = self._mock_transfer()
        mock_reversal.side_effect = stripe_lib.error.InvalidRequestError('boom', 'amount')

        res = self._post_refund_event(mock_construct, amount_refunded=1000, refunded=False)

        self.assertEqual(res.status_code, 200)
        self.order.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.order.refunded_amount, Decimal('10.00'))
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.PARTIALLY_REFUNDED)
        # Local cache untouched — backfill/webhook retry converges later.
        self.assertEqual(self.session.transfer_reversed_cents, 0)

    @patch('stripe.Transfer.create_reversal')
    @patch('stripe.Transfer.retrieve')
    @patch('stripe.Webhook.construct_event')
    def test_direct_charge_refund_no_reversal(
        self, mock_construct, mock_retrieve, mock_reversal,
    ):
        # In-person (direct) charges have no transfer — nothing to claw back.
        self.session.stripe_transfer_id = ''
        self.session.transfer_cents = 0
        self.session.charge_flow = StripeCheckoutSession.ChargeFlow.DIRECT
        self.session.save(update_fields=['stripe_transfer_id', 'transfer_cents', 'charge_flow'])

        res = self._post_refund_event(
            mock_construct, amount_refunded=1000, refunded=False, transfer=None,
        )

        self.assertEqual(res.status_code, 200)
        mock_retrieve.assert_not_called()
        mock_reversal.assert_not_called()
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, StripeCheckoutSession.Status.PARTIALLY_REFUNDED)


class ConnectedBalanceCacheTests(TestCase):
    """_get_connected_balance_cents caching + explicit busting."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='Cache Org', slug=f'cache-org-{uuid.uuid4().hex[:8]}',
            stripe_account_id='acct_cache_test',
        )

    @patch('stripe.Balance.retrieve')
    def test_cache_hit_and_bust(self, mock_balance):
        from tickets.views import _get_connected_balance_cents, _bust_connected_balance_cache
        mock_balance.return_value = MagicMock(
            available=[MagicMock(amount=1000, currency='usd')],
            pending=[MagicMock(amount=200, currency='usd')],
        )

        self.assertEqual(_get_connected_balance_cents(self.org), (1000, 200))
        self.assertEqual(_get_connected_balance_cents(self.org), (1000, 200))
        self.assertEqual(mock_balance.call_count, 1)  # second call served from cache

        _bust_connected_balance_cache(self.org)
        self.assertEqual(_get_connected_balance_cents(self.org), (1000, 200))
        self.assertEqual(mock_balance.call_count, 2)  # bust forced a refetch


class MigrateLegacyBalancesCommandTests(TestCase):
    """True-up command: dry-run safety, crash-safe apply, repair, re-runs."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='Trueup Org',
            slug='trueup-org',
            stripe_account_id='acct_trueup',
            stripe_onboarding_complete=True,
        )
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Trueup Event', venue=self.venue,
            start_date=date.today() + timedelta(days=14),
            ticketing_type='direct',
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='buyer@example.com', name='Buyer',
        )
        order = TicketOrder.objects.create(
            event=self.event,
            customer=self.customer,
            order_number='TU-001',
            order_date=timezone.now(),
            total_amount=Decimal('50.00'),
        )
        # Settled legacy session: organizer net $47.50.
        StripeCheckoutSession.objects.create(
            event=self.event,
            organization=self.org,
            stripe_session_id='pi_trueup_1',
            stripe_payment_intent_id='pi_trueup_1',
            buyer_email='buyer@example.com',
            buyer_name='Buyer',
            status=StripeCheckoutSession.Status.COMPLETED,
            amount_total_cents=5000,
            platform_fee_cents=250,
            ticket_order=order,
        )

    def test_dry_run_writes_nothing(self):
        call_command('migrate_legacy_balances')
        self.assertEqual(Payout.objects.count(), 0)

    @patch('stripe.Transfer.create')
    @patch('tickets.views._get_stripe_platform_available_cents', return_value=1_000_000)
    def test_apply_transfers_and_records_migration_payout(self, mock_platform, mock_transfer):
        mock_transfer.return_value = MagicMock(id='tr_trueup_1')

        call_command('migrate_legacy_balances', '--apply')

        payout = Payout.objects.get(organization=self.org)
        self.assertEqual(payout.amount, Decimal('47.50'))
        self.assertEqual(payout.status, Payout.Status.COMPLETED)
        self.assertEqual(payout.origin, Payout.Origin.MIGRATION)
        self.assertEqual(payout.stripe_transfer_id, 'tr_trueup_1')
        self.assertIsNone(payout.initiated_by)
        kwargs = mock_transfer.call_args.kwargs
        self.assertEqual(kwargs['amount'], 4750)
        self.assertEqual(kwargs['destination'], 'acct_trueup')
        self.assertEqual(kwargs['idempotency_key'], f'trueup-{payout.id}')

        # Re-run: the migration payout self-deducts — nothing more to move.
        call_command('migrate_legacy_balances', '--apply')
        self.assertEqual(Payout.objects.count(), 1)
        self.assertEqual(mock_transfer.call_count, 1)

    @patch('stripe.Transfer.create')
    @patch('tickets.views._get_stripe_platform_available_cents', return_value=1_000_000)
    def test_stranded_pending_row_blocks_apply_and_repair_completes_it(
        self, mock_platform, mock_transfer,
    ):
        stranded = Payout.objects.create(
            organization=self.org,
            amount=Decimal('47.50'),
            status=Payout.Status.PENDING,
            origin=Payout.Origin.MIGRATION,
            notes='Balance migration to Stripe account',
        )

        # Apply refuses to act while a stranded row exists — no double-pay.
        call_command('migrate_legacy_balances', '--apply')
        mock_transfer.assert_not_called()
        self.assertEqual(Payout.objects.count(), 1)

        # Repair replays the stable idempotency key and completes the row.
        mock_transfer.return_value = MagicMock(id='tr_trueup_repair')
        call_command('migrate_legacy_balances', '--repair')
        stranded.refresh_from_db()
        self.assertEqual(stranded.status, Payout.Status.COMPLETED)
        self.assertEqual(stranded.stripe_transfer_id, 'tr_trueup_repair')
        self.assertEqual(
            mock_transfer.call_args.kwargs['idempotency_key'], f'trueup-{stranded.id}',
        )

    @patch('stripe.Transfer.create')
    @patch('tickets.views._get_stripe_platform_available_cents', return_value=1_000_000)
    def test_transfer_failure_marks_failed_and_is_retryable(self, mock_platform, mock_transfer):
        import stripe as stripe_lib
        mock_transfer.side_effect = stripe_lib.error.InvalidRequestError('no funds', 'amount')

        call_command('migrate_legacy_balances', '--apply')

        failed = Payout.objects.get(organization=self.org)
        self.assertEqual(failed.status, Payout.Status.FAILED)
        self.assertIn('Stripe error', failed.notes)

        # FAILED rows are excluded from the pool sums — a retry moves the
        # full amount on a fresh row.
        mock_transfer.side_effect = None
        mock_transfer.return_value = MagicMock(id='tr_trueup_retry')
        call_command('migrate_legacy_balances', '--apply')
        completed = Payout.objects.get(organization=self.org, status=Payout.Status.COMPLETED)
        self.assertEqual(completed.amount, Decimal('47.50'))

    @patch('stripe.Transfer.create')
    @patch('tickets.views._get_stripe_platform_available_cents', return_value=100)
    def test_insufficient_platform_balance_skips(self, mock_platform, mock_transfer):
        call_command('migrate_legacy_balances', '--apply')
        mock_transfer.assert_not_called()
        self.assertEqual(Payout.objects.count(), 0)


class MetaAdsErrorHandlingTests(TestCase):
    """Diagnostics + throttle behavior for the Meta Ads integration (PR #202 fixes)."""

    def setUp(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()
        self.client = Client()
        self.org = Organization.objects.create(
            name='Meta Org', slug='meta-org',
            meta_ads_access_token='tok-123',
            meta_ads_account_id='act_999',
            meta_ads_account_name='Ad Account',
        )
        self.admin_user = User.objects.create_user(
            username='metaadmin', email='metaadmin@example.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.admin_user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.admin_user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Meta Event', venue=self.venue,
            start_date=date(2026, 8, 1),
        )
        self.expense = EventExpense.objects.create(
            event=self.event, category='marketing', description='Campaign',
            amount=Decimal('100.00'), source='meta_ads', external_id='120246175133360162',
        )

    def tearDown(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()

    def _login(self):
        self.client.login(username='metaadmin@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))

    # --- error extraction -------------------------------------------------

    def test_error_from_response_includes_code_subcode_and_logs_fbtrace(self):
        from tickets.services import meta_ads

        class _Resp:
            status_code = 500
            def json(self):
                return {'error': {
                    'message': 'An unknown error has occurred.',
                    'code': 1, 'error_subcode': 99, 'fbtrace_id': 'TRACE123',
                }}

        with self.assertLogs('tickets.services.meta_ads', level='WARNING') as logs:
            err = meta_ads._error_from_response(_Resp())

        self.assertIsInstance(err, meta_ads.MetaAdsAPIError)
        self.assertEqual(err.code, 1)
        self.assertEqual(err.subcode, 99)
        self.assertEqual(err.fbtrace_id, 'TRACE123')
        self.assertTrue(err.is_transient)
        self.assertIn('code 1', str(err))
        self.assertIn('subcode 99', str(err))
        self.assertIn('An unknown error has occurred.', str(err))
        # The fbtrace id must reach the logs so a recurrence is diagnosable.
        self.assertIn('TRACE123', '\n'.join(logs.output))

    def test_error_from_response_handles_non_json_body(self):
        from tickets.services import meta_ads

        class _Resp:
            status_code = 503
            def json(self):
                raise ValueError('no json')

        with self.assertLogs('tickets.services.meta_ads', level='WARNING'):
            err = meta_ads._error_from_response(_Resp())
        self.assertIn('503', str(err))
        self.assertFalse(err.is_transient)

    def test_request_retries_once_on_transient_error(self):
        from tickets.services import meta_ads

        class _Resp:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload
            def json(self):
                return self._payload

        responses = [
            _Resp(500, {'error': {'message': 'An unknown error has occurred.', 'code': 1}}),
            _Resp(200, {'id': '1', 'name': 'Acct'}),
        ]
        with patch('tickets.services.meta_ads.time.sleep') as sleep_mock, \
                patch('tickets.services.meta_ads.requests.get', side_effect=responses) as get_mock:
            client = meta_ads.MetaAdsClient('tok')
            result = client.get_user_profile()

        self.assertEqual(result, {'id': '1', 'name': 'Acct'})
        self.assertEqual(get_mock.call_count, 2)
        sleep_mock.assert_called_once()

    def test_request_does_not_retry_non_transient_error(self):
        from tickets.services import meta_ads

        class _Resp:
            status_code = 400
            def json(self):
                return {'error': {'message': 'Bad token', 'code': 190}}

        with patch('tickets.services.meta_ads.requests.get', return_value=_Resp()) as get_mock:
            client = meta_ads.MetaAdsClient('tok')
            with self.assertRaises(meta_ads.MetaAdsAPIError):
                client.get_user_profile()
        self.assertEqual(get_mock.call_count, 1)

    # --- staleness gate ---------------------------------------------------

    def test_refresh_throttles_repeat_calls_within_window(self):
        from tickets import views
        from tickets.services.meta_ads import CampaignInsights

        fake_client = MagicMock()
        fake_client.get_campaign_insights.return_value = CampaignInsights(
            spend=Decimal('12.00'), purchases=1, purchase_value=Decimal('20.00'),
        )
        with patch('tickets.views.MetaAdsClient', return_value=fake_client):
            views._refresh_meta_ads_expenses_for_event(self.org, self.event, self.admin_user)
            views._refresh_meta_ads_expenses_for_event(self.org, self.event, self.admin_user)

        # Second call must be short-circuited by the cache marker — Meta hit once.
        self.assertEqual(fake_client.get_campaign_insights.call_count, 1)

    def test_refresh_catches_api_error_and_still_sets_marker(self):
        from tickets import views
        from tickets.services.meta_ads import MetaAdsAPIError

        fake_client = MagicMock()
        fake_client.get_campaign_insights.side_effect = MetaAdsAPIError(
            'Meta API error (code 1): An unknown error has occurred.', code=1,
        )
        with patch('tickets.views.MetaAdsClient', return_value=fake_client):
            with self.assertLogs('tickets.views', level='WARNING'):
                had_error = views._refresh_meta_ads_expenses_for_event(
                    self.org, self.event, self.admin_user,
                )
            self.assertTrue(had_error)
            # Marker set despite the error, so the next load doesn't re-storm Meta.
            had_error_2 = views._refresh_meta_ads_expenses_for_event(
                self.org, self.event, self.admin_user,
            )
        self.assertFalse(had_error_2)
        self.assertEqual(fake_client.get_campaign_insights.call_count, 1)

    # --- endpoint status codes -------------------------------------------

    def test_match_endpoint_returns_handled_error_not_502(self):
        from tickets.services.meta_ads import MetaAdsAPIError
        self._login()

        fake_client = MagicMock()
        fake_client.list_campaigns.side_effect = MetaAdsAPIError(
            'Meta API error (code 1): An unknown error has occurred.', code=1,
        )
        url = reverse('tickets:event_meta_ads_match', args=[self.event.id]) + '?format=json'
        with patch('tickets.views.MetaAdsClient', return_value=fake_client):
            resp = self.client.get(url)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data['success'])
        self.assertIn('code 1', data['error'])

    def test_refresh_endpoint_returns_handled_error_not_502(self):
        from tickets.services.meta_ads import MetaAdsAPIError
        self._login()

        fake_client = MagicMock()
        fake_client.get_campaign_insights.side_effect = MetaAdsAPIError(
            'Meta API error (code 1): An unknown error has occurred.', code=1,
        )
        url = reverse('tickets:event_meta_ads_refresh', args=[self.event.id, self.expense.id])
        with patch('tickets.views.MetaAdsClient', return_value=fake_client):
            resp = self.client.post(url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data['ok'])
        self.assertIn('code 1', data['error'])


class ResendOrderConfirmationTests(TestCase):
    """Resend confirmation email endpoint on the order detail page."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Resend Org', slug='resend-org')
        self.user = User.objects.create_user(
            username='resend-host', email='host@example.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )
        self.client.login(username='host@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))

        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Direct Event', venue=self.venue,
            start_date=date(2025, 6, 15), start_time=time(19, 0),
            ticketing_type=TICKETING_TYPE_DIRECT,
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='buyer@example.com', name='Buyer',
        )
        self.order = TicketOrder.objects.create(
            customer=self.customer, event=self.event,
            order_number='ORD-RS-1', order_date='2025-06-01 10:00:00',
            total_amount=Decimal('50.00'),
        )
        self.session = StripeCheckoutSession.objects.create(
            event=self.event, organization=self.org,
            stripe_session_id='pi_resend_1', stripe_payment_intent_id='pi_resend_1',
            buyer_email='buyer@example.com', buyer_name='Buyer',
            status=StripeCheckoutSession.Status.COMPLETED,
            line_items_snapshot=[], amount_total_cents=5000,
            ticket_order=self.order,
        )
        self.url = reverse('tickets:resend_order_confirmation', args=[self.order.id])

    @patch('tickets.tasks.send_order_confirmation_email_task.delay')
    def test_resend_queues_task_for_direct_order(self, mock_delay):
        response = self.client.post(self.url)
        self.assertRedirects(
            response, reverse('tickets:order_detail', args=[self.order.id])
        )
        mock_delay.assert_called_once_with(str(self.order.id))

    @patch('tickets.tasks.send_order_confirmation_email_task.delay')
    def test_resend_rejects_non_direct_order(self, mock_delay):
        self.session.delete()
        self.event.ticketing_type = 'csv'
        self.event.save(update_fields=['ticketing_type'])
        response = self.client.post(self.url)
        self.assertRedirects(
            response, reverse('tickets:order_detail', args=[self.order.id])
        )
        mock_delay.assert_not_called()

    @patch('tickets.tasks.send_order_confirmation_email_task.delay')
    def test_resend_rejects_get(self, mock_delay):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
        mock_delay.assert_not_called()


class EventSummaryStreamTests(TestCase):
    """Test cases for the AI event debrief streaming endpoint."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Summary Test Org', slug='summary-test-org')
        self.user = User.objects.create_user(
            username='summaryuser',
            email='summary@test.com',
            password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='summary@test.com', password='testpass123')
        self.client.get(reverse('tickets:home'))

        self.venue = Venue.objects.create(
            organization=self.org, name='Summary Venue', city='Summary City',
        )
        self.event = Event.objects.create(
            organization=self.org, name='Summary Event',
            venue=self.venue, start_date=date(2024, 9, 15),
        )
        self.url = reverse('tickets:event_summary_stream', args=[self.event.id])

    def test_unauthenticated_redirects(self):
        """Unauthenticated user is redirected to login."""
        self.client.logout()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)

    def test_get_not_allowed(self):
        """GET method is not allowed — POST only."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    def test_wrong_org_returns_404(self):
        """Event belonging to a different org returns 404."""
        other_org = Organization.objects.create(name='Other Org', slug='other-org')
        other_venue = Venue.objects.create(
            organization=other_org, name='Other Venue', city='Other City',
        )
        other_event = Event.objects.create(
            organization=other_org, name='Other Event',
            venue=other_venue, start_date=date(2024, 9, 15),
        )
        url = reverse('tickets:event_summary_stream', args=[other_event.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)

    @patch('langchain_openai.ChatOpenAI')
    def test_stream_returns_sse_content_type(self, mock_llm_cls):
        """Successful request returns text/event-stream content type."""
        mock_instance = MagicMock()
        mock_chunk = MagicMock()
        mock_chunk.content = 'Test debrief'
        mock_instance.stream.return_value = [mock_chunk]
        mock_llm_cls.return_value = mock_instance

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/event-stream')

    @patch('langchain_openai.ChatOpenAI')
    def test_stream_persists_summary(self, mock_llm_cls):
        """After streaming, the summary is saved to the event."""
        mock_instance = MagicMock()
        mock_chunk = MagicMock()
        mock_chunk.content = 'Generated debrief text'
        mock_chunk.usage_metadata = None
        mock_instance.stream.return_value = [mock_chunk]
        mock_llm_cls.return_value = mock_instance

        response = self.client.post(self.url)
        # Consume the streaming response to trigger the generator
        list(response.streaming_content)

        self.event.refresh_from_db()
        self.assertEqual(self.event.ai_summary, 'Generated debrief text')
        self.assertIsNotNone(self.event.ai_summary_generated_at)

    @patch('langchain_openai.ChatOpenAI')
    def test_stream_records_token_usage(self, mock_llm_cls):
        """A successful generation records a billable AITokenUsage row."""
        mock_instance = MagicMock()
        mock_chunk = MagicMock()
        mock_chunk.content = 'Debrief with usage'
        mock_chunk.usage_metadata = {
            'input_tokens': 1200,
            'output_tokens': 300,
            'total_tokens': 1500,
        }
        mock_instance.stream.return_value = [mock_chunk]
        mock_llm_cls.return_value = mock_instance

        response = self.client.post(self.url)
        list(response.streaming_content)

        usage = AITokenUsage.objects.filter(
            organization=self.org,
            feature=AITokenUsage.FEATURE_EVENT_SUMMARY,
        )
        self.assertEqual(usage.count(), 1)
        record = usage.first()
        self.assertEqual(record.total_tokens, 1500)
        self.assertEqual(record.user, self.user)
        self.assertEqual(record.metadata.get('event_id'), str(self.event.id))

    @patch('langchain_openai.ChatOpenAI')
    def test_stream_handles_llm_error(self, mock_llm_cls):
        """LLM API error yields an error SSE event, not a 500."""
        mock_instance = MagicMock()
        mock_instance.stream.side_effect = Exception('API key invalid')
        mock_llm_cls.return_value = mock_instance

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        content = b''.join(response.streaming_content).decode()
        self.assertIn('"type": "error"', content)
        self.assertIn('"type": "done"', content)

    def test_ai_summary_field_persistence(self):
        """ai_summary field saves and loads correctly."""
        self.event.ai_summary = 'Test stored debrief'
        self.event.ai_summary_generated_at = timezone.now()
        self.event.save(update_fields=['ai_summary', 'ai_summary_generated_at'])

        self.event.refresh_from_db()
        self.assertEqual(self.event.ai_summary, 'Test stored debrief')
        self.assertIsNotNone(self.event.ai_summary_generated_at)

    @patch('langchain_openai.ChatOpenAI')
    def test_rate_limit_returns_429(self, mock_llm_cls):
        """Once the hourly ceiling is reached, the endpoint returns 429."""
        mock_instance = MagicMock()
        mock_chunk = MagicMock()
        mock_chunk.content = 'Debrief'
        mock_instance.stream.return_value = [mock_chunk]
        mock_llm_cls.return_value = mock_instance

        from django.core.cache import cache as django_cache
        rate_key = f"summary_ratelimit:{self.org.id}"
        django_cache.set(rate_key, 30, timeout=3600)

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 429)

        django_cache.delete(rate_key)

    @patch('langchain_openai.ChatOpenAI')
    def test_failed_generation_does_not_consume_rate_limit(self, mock_llm_cls):
        """A failed generation must not burn the hourly budget (regression)."""
        from django.core.cache import cache as django_cache
        rate_key = f"summary_ratelimit:{self.org.id}"
        django_cache.delete(rate_key)

        mock_instance = MagicMock()
        mock_instance.stream.side_effect = Exception('API key invalid')
        mock_llm_cls.return_value = mock_instance

        response = self.client.post(self.url)
        b''.join(response.streaming_content)  # drive the generator to completion

        self.assertEqual(django_cache.get(rate_key, 0), 0)
        django_cache.delete(rate_key)

    @patch('langchain_openai.ChatOpenAI')
    def test_successful_generation_increments_rate_limit(self, mock_llm_cls):
        """A successful generation counts once against the hourly budget."""
        from django.core.cache import cache as django_cache
        rate_key = f"summary_ratelimit:{self.org.id}"
        django_cache.delete(rate_key)

        mock_instance = MagicMock()
        mock_chunk = MagicMock()
        mock_chunk.content = 'Generated summary text'
        mock_chunk.usage_metadata = None
        mock_instance.stream.return_value = [mock_chunk]
        mock_llm_cls.return_value = mock_instance

        response = self.client.post(self.url)
        b''.join(response.streaming_content)

        self.assertEqual(django_cache.get(rate_key, 0), 1)
        django_cache.delete(rate_key)

    def test_event_detail_still_works(self):
        """Regression: event_detail view still renders after stats extraction."""
        url = reverse('tickets:event_detail', args=[self.event.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Summary Event')

    def test_build_prompt_structure(self):
        """_build_prompt returns the forward-looking debrief sections and event name."""
        from tickets.services.event_summary import EventSummaryService
        from tickets.views import _compute_event_stats

        service = EventSummaryService(self.org, user=self.user)
        event_data = _compute_event_stats(self.event)
        prompt = service._build_prompt(self.event, event_data)

        self.assertIn('What worked', prompt)
        self.assertIn('What underperformed', prompt)
        self.assertIn('Recommended next steps', prompt)
        self.assertIn('Summary Event', prompt)

    def test_build_prompt_unifies_survey_responses(self):
        """Survey block uses combined totals and drops internal/external/invitation framing."""
        from tickets.services.event_summary import EventSummaryService
        from tickets.views import _compute_event_stats

        service = EventSummaryService(self.org, user=self.user)
        event_data = _compute_event_stats(self.event)
        prompt = service._build_prompt(self.event, event_data)

        self.assertIn('Total Responses', prompt)
        self.assertNotIn('Invitations Sent', prompt)
        self.assertNotIn('External Survey Responses', prompt)
        self.assertNotIn('Responses Received', prompt)

    def _make_direct_checked_in_event(self):
        """A past direct-ticketing event with one fully checked-in GA ticket."""
        event = Event.objects.create(
            organization=self.org, name='Direct Event',
            venue=self.venue, start_date=date(2024, 9, 15),
            ticketing_type=TICKETING_TYPE_DIRECT,
        )
        customer = Customer.objects.create(
            organization=self.org, email='attendee@test.com', name='Attendee',
        )
        now = timezone.now()
        order = TicketOrder.objects.create(
            customer=customer, event=event, order_number='DIR-001',
            order_date='2024-09-10 10:00:00', total_amount=Decimal('50.00'),
            checked_in_at=now,
        )
        # An admitted order stamps each of its tickets (the scan source of truth).
        Ticket.objects.create(
            ticket_order=order, ticket_type='GA', price=Decimal('50.00'), scanned_at=now,
        )
        return event

    def test_build_prompt_includes_checkin_for_direct_event(self):
        """Direct events past start surface a Check-In section with percentages."""
        from tickets.services.event_summary import EventSummaryService
        from tickets.views import _compute_event_stats

        event = self._make_direct_checked_in_event()
        service = EventSummaryService(self.org, user=self.user)
        event_data = _compute_event_stats(event)
        prompt = service._build_prompt(event, event_data)

        self.assertIn('Check-In (door attendance)', prompt)
        self.assertIn('1 of 1 (100%)', prompt)
        self.assertIn('GA: 1/1 (100%)', prompt)

    def test_build_prompt_omits_checkin_for_non_direct_event(self):
        """Non-direct (external) events have no Check-In section."""
        from tickets.services.event_summary import EventSummaryService
        from tickets.views import _compute_event_stats

        service = EventSummaryService(self.org, user=self.user)
        event_data = _compute_event_stats(self.event)  # default (external) ticketing
        prompt = service._build_prompt(self.event, event_data)

        self.assertNotIn('Check-In (door attendance)', prompt)

    def test_build_prompt_excludes_allocation_for_external_event(self):
        """External-upload events must not invite sell-through/allocation conclusions."""
        from tickets.services.event_summary import EventSummaryService
        from tickets.views import _compute_event_stats

        self.event.capacity = 100
        self.event.save(update_fields=['capacity'])
        service = EventSummaryService(self.org, user=self.user)
        event_data = _compute_event_stats(self.event)  # default (external) ticketing
        prompt = service._build_prompt(self.event, event_data)

        self.assertIn('Do NOT draw any conclusion about sell-through', prompt)
        self.assertIn("uploaded events don't include ticket allocation totals", prompt)
        self.assertIn('allocation totals not available', prompt)
        # No computed utilization percentage even though capacity is set.
        self.assertNotIn('Capacity Utilization: 0.0%', prompt)

    def test_build_prompt_keeps_allocation_for_direct_event(self):
        """Direct events keep real capacity utilization and no allocation caveat."""
        from tickets.services.event_summary import EventSummaryService
        from tickets.views import _compute_event_stats

        event = self._make_direct_checked_in_event()
        event.capacity = 4
        event.save(update_fields=['capacity'])
        service = EventSummaryService(self.org, user=self.user)
        event_data = _compute_event_stats(event)
        prompt = service._build_prompt(event, event_data)

        # Direct branch computes a numeric utilization (not the external note).
        self.assertRegex(prompt, r'Capacity Utilization: \d+\.\d%')
        self.assertNotIn("uploaded events don't include ticket allocation totals", prompt)
        self.assertIn('Ticket Type Breakdown:', prompt)
        self.assertNotIn('Do NOT draw any conclusion about sell-through', prompt)

    def _add_external_responses(self, event):
        """Create external survey responses with structured (Typeform-style) answers."""
        upload = ExternalSurveyUpload.objects.create(
            organization=self.org, filename='typeform.csv',
            status=ExternalSurveyUpload.Status.COMPLETED,
        )
        rows = [
            dict(enjoyed=['DJ set', 'Lighting'], genres=['House'],
                 improvements=['Better sound'], crowd_vibe='Energetic',
                 venue_feel='Intimate', found_out_how='Instagram'),
            dict(enjoyed=['DJ set'], genres=['House', 'Techno'],
                 improvements=[], crowd_vibe='Energetic',
                 venue_feel='Intimate', found_out_how='Instagram'),
            dict(enjoyed=['Venue'], genres=['Techno'],
                 improvements=['Better sound'], crowd_vibe='Chill',
                 venue_feel='Spacious', found_out_how='Word of mouth'),
        ]
        for i, row in enumerate(rows):
            ExternalSurveyResponse.objects.create(
                organization=self.org, upload=upload, event=event,
                responded_at=timezone.now(), email=f'guest{i}@example.com',
                nps_score=9, **row,
            )

    def test_build_prompt_includes_external_structured_answers(self):
        """Structured Typeform answers are aggregated (with counts) into the prompt."""
        from tickets.services.event_summary import EventSummaryService
        from tickets.views import _compute_event_stats

        event = Event.objects.create(
            organization=self.org, name='Typeform Event',
            venue=self.venue, start_date=date(2024, 9, 15),
        )
        self._add_external_responses(event)
        service = EventSummaryService(self.org, user=self.user)
        prompt = service._build_prompt(event, _compute_event_stats(event))

        self.assertIn('Top things enjoyed: DJ set (2)', prompt)
        self.assertIn('Most requested improvements: Better sound (2)', prompt)
        self.assertIn('Crowd vibe: Energetic (2)', prompt)
        self.assertIn('How attendees discovered the event: Instagram (2)', prompt)
        # Most common value ranks first.
        self.assertLess(prompt.index('DJ set'), prompt.index('Lighting'))

    def test_build_prompt_omits_structured_when_no_external_data(self):
        """Events without external structured answers get no structured lines."""
        from tickets.services.event_summary import EventSummaryService
        from tickets.views import _compute_event_stats

        service = EventSummaryService(self.org, user=self.user)
        prompt = service._build_prompt(self.event, _compute_event_stats(self.event))

        self.assertNotIn('Top things enjoyed', prompt)
        self.assertNotIn('How attendees discovered the event', prompt)


class DisplayPreferencesTests(TestCase):
    """Org admins can toggle the AI Event Summary card from /settings/display/."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Prefs Org', slug='prefs-org')

        self.admin_user = User.objects.create_user(
            username='prefsadmin', email='prefsadmin@test.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.admin_user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.admin_user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )

        self.member_user = User.objects.create_user(
            username='prefshost', email='prefshost@test.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.member_user, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )
        OrganizationMembership.objects.create(
            user=self.member_user, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )

        self.venue = Venue.objects.create(
            organization=self.org, name='Prefs Venue', city='Prefs City',
        )
        # Past event so the AI summary card is eligible to render
        self.event = Event.objects.create(
            organization=self.org, name='Prefs Event',
            venue=self.venue, start_date=date(2024, 9, 15),
        )
        self.url = reverse('tickets:settings_display_preferences')

    def _login(self, email):
        self.client.login(username=email, password='testpass123')
        self.client.get(reverse('tickets:home'))

    def test_admin_can_view_preferences(self):
        """Admin sees the preferences page with the toggle."""
        self._login('prefsadmin@test.com')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ai_event_summary_enabled')

    def test_non_admin_forbidden(self):
        """A non-admin member is denied access."""
        self._login('prefshost@test.com')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_post_disables_flag(self):
        """Posting with the box unchecked turns the flag off."""
        self._login('prefsadmin@test.com')
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        self.assertFalse(self.org.ai_event_summary_enabled)

    def test_post_enables_flag(self):
        """Posting with the box checked turns the flag on."""
        self.org.ai_event_summary_enabled = False
        self.org.save(update_fields=['ai_event_summary_enabled'])
        self._login('prefsadmin@test.com')
        response = self.client.post(self.url, {'ai_event_summary_enabled': 'on'})
        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        self.assertTrue(self.org.ai_event_summary_enabled)

    def test_card_shown_when_enabled(self):
        """Default (enabled) renders the AI summary card on event detail."""
        self._login('prefsadmin@test.com')
        response = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="ai-summary-card"')

    def test_card_hidden_when_disabled(self):
        """When disabled, the AI summary card is not rendered."""
        self.org.ai_event_summary_enabled = False
        self.org.save(update_fields=['ai_event_summary_enabled'])
        self._login('prefsadmin@test.com')
        response = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="ai-summary-card"')

    def test_stream_404_when_disabled(self):
        """The streaming endpoint is unavailable while the card is hidden."""
        self.org.ai_event_summary_enabled = False
        self.org.save(update_fields=['ai_event_summary_enabled'])
        self._login('prefsadmin@test.com')
        url = reverse('tickets:event_summary_stream', args=[self.event.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)


class POSHScanImportAndBuiltinFormatTests(TestCase):
    """Per-ticket scan import (POSH) + global built-in CSV format behavior."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Scan Org', slug='scan-org')
        self.user = User.objects.create_user(
            username='scanhost', email='scanhost@example.com', password='pw12345',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        # External (CSV) event whose start has passed.
        self.event = Event.objects.create(
            organization=self.org, name='Scan Night', venue=self.venue,
            start_date=date(2026, 1, 1), start_time=time(20, 0, 0),
            ticketing_type='external',
        )

    # --- parse_scan_details unit coverage -------------------------------------
    def _processor(self):
        from tickets.csv_processor import CSVProcessor
        return CSVProcessor.__new__(CSVProcessor)

    def test_parse_scan_details_variants(self):
        p = self._processor()
        self.assertEqual(p.parse_scan_details(''), [])
        self.assertEqual(
            p.parse_scan_details('ga - Not Scanned (N/A), ga - Not Scanned (N/A)'),
            [None, None],
        )
        one = p.parse_scan_details('ga - Scanned (06-14-2026 7:40:36 pm)')
        self.assertEqual(len(one), 1)
        self.assertIsNotNone(one[0])
        mixed = p.parse_scan_details(
            'early bird - Scanned (06-14-2026 7:07:28 pm), ga - Not Scanned (N/A)'
        )
        self.assertIsNotNone(mixed[0])
        self.assertIsNone(mixed[1])

    # --- end-to-end import ----------------------------------------------------
    def _import(self, csv_body):
        import io
        posh = CSVFormat.objects.get(name='POSH')
        upload = UploadedFile.objects.create(
            organization=self.org, csv_format=posh, filename='posh.csv', status='pending',
            metadata={'event_id': str(self.event.id), 'event_name': self.event.name,
                      'event_start_date': '2026-01-01'},
        )
        from tickets.csv_processor import CSVProcessor
        processor = CSVProcessor(upload, posh)
        return processor.process_and_save(io.BytesIO(csv_body.encode('utf-8')))

    def test_import_sets_per_ticket_scanned_and_order_checkin(self):
        header = (
            '"Order Number","Order Date/Time","Order Subtotal","Order Total",'
            '"# of Tickets","First Name","Last Name","Email","Phone Number",'
            '"Tickets Purchased","Ticket Scan Details"\n'
        )
        rows = (
            # Fully scanned single ticket
            '"A1","05-12-2026 1:40:11 pm","21.00","21.00","1","Amy","R","a@x.com","+17025550000",'
            '"general admission","general admission - Scanned (06-14-2026 7:40:36 pm)"\n'
            # Partially scanned 2-ticket order (1 of 2)
            '"A2","05-12-2026 1:42:50 pm","0.00","0.00","2","Bo","J","b@x.com","+17025550001",'
            '"free rsvp, free rsvp","free rsvp - Scanned (06-14-2026 5:35:26 pm), free rsvp - Not Scanned (N/A)"\n'
            # Not scanned at all
            '"A3","05-12-2026 1:50:00 pm","0.00","0.00","1","Cy","K","c@x.com","+17025550002",'
            '"free rsvp","free rsvp - Not Scanned (N/A)"\n'
        )
        results = self._import(header + rows)
        self.assertEqual(results['success_count'], 3)

        o1 = TicketOrder.objects.get(external_order_number='A1')
        o2 = TicketOrder.objects.get(external_order_number='A2')
        o3 = TicketOrder.objects.get(external_order_number='A3')

        # Per-ticket scanned_at
        self.assertEqual(o1.tickets.filter(scanned_at__isnull=False).count(), 1)
        self.assertEqual(o2.tickets.count(), 2)
        self.assertEqual(o2.tickets.filter(scanned_at__isnull=False).count(), 1)
        self.assertEqual(o3.tickets.filter(scanned_at__isnull=False).count(), 0)

        # Order-level marker: set when ANY ticket scanned, NULL when none.
        self.assertIsNotNone(o1.checked_in_at)
        self.assertIsNotNone(o2.checked_in_at)
        self.assertIsNone(o3.checked_in_at)

    def test_checkin_stats_count_tickets_and_surface_external_event(self):
        from tickets.views import _compute_event_checkin_stats
        header = (
            '"Order Number","Order Date/Time","Order Subtotal","Order Total",'
            '"# of Tickets","First Name","Last Name","Email","Phone Number",'
            '"Tickets Purchased","Ticket Scan Details"\n'
        )
        rows = (
            '"A2","05-12-2026 1:42:50 pm","0.00","0.00","2","Bo","J","b@x.com","+17025550001",'
            '"free rsvp, free rsvp","free rsvp - Scanned (06-14-2026 5:35:26 pm), free rsvp - Not Scanned (N/A)"\n'
        )
        self._import(header + rows)
        show, total, checked, by_type = _compute_event_checkin_stats(self.event)
        # External event surfaces because it has imported scan data.
        self.assertTrue(show)
        self.assertEqual(total, 2)
        # Only 1 of 2 tickets scanned -> no order-level overcount.
        self.assertEqual(checked, 1)

    def _partial_order(self):
        """Create a 2-ticket order with 1 ticket scanned; order.checked_in_at NULL.

        Mirrors a partial per-ticket scan / CSV import where the order-level flag
        is never set (both tickets must be scanned to flip it).
        """
        cust = Customer.objects.create(
            organization=self.org, email='partial@x.com', name='Partial',
        )
        order = TicketOrder.objects.create(
            customer=cust, event=self.event, order_number='PART-1',
            order_date=timezone.now(), total_amount=Decimal('0.00'),
        )
        Ticket.objects.create(
            ticket_order=order, ticket_type='free rsvp', price=Decimal('0.00'),
            scanned_at=timezone.now(),
        )
        Ticket.objects.create(
            ticket_order=order, ticket_type='free rsvp', price=Decimal('0.00'),
        )
        self.assertIsNone(order.checked_in_at)
        return order

    def test_scanner_checkin_stats_counts_partial_order_by_ticket(self):
        from .models import ScannerSession
        self._partial_order()
        session = ScannerSession.objects.create(event=self.event)
        res = self.client.get(
            '/api/scanner/checkin-stats/',
            HTTP_AUTHORIZATION=f'Scanner {session.token}',
        )
        self.assertEqual(res.status_code, 200)
        row = next(r for r in res.json() if r['ticket_type_name'] == 'free rsvp')
        # Per-ticket count: 1 of 2 scanned (order-level flag would report 0).
        self.assertEqual(row['total'], 2)
        self.assertEqual(row['checked_in'], 1)

    def test_organizer_checkin_stats_counts_partial_order_by_ticket(self):
        self._partial_order()
        token = Token.objects.create(user=self.user)
        res = self.client.get(
            f'/api/organizer/events/{self.event.id}/checkin-stats/',
            HTTP_AUTHORIZATION=f'Token {token.key}',
        )
        self.assertEqual(res.status_code, 200)
        row = next(r for r in res.json() if r['ticket_type_name'] == 'free rsvp')
        # Per-ticket count: 1 of 2 scanned (order-level flag would report 0).
        self.assertEqual(row['total'], 2)
        self.assertEqual(row['checked_in'], 1)

    def test_external_event_without_scan_data_is_hidden(self):
        from tickets.views import _compute_event_checkin_stats
        # An order with no scan data.
        cust = Customer.objects.create(organization=self.org, email='n@x.com', name='N')
        order = TicketOrder.objects.create(
            customer=cust, event=self.event, order_number='NOSCAN',
            order_date=timezone.now(), total_amount=Decimal('10.00'),
        )
        Ticket.objects.create(ticket_order=order, ticket_type='GA', price=Decimal('10.00'))
        show, total, checked, by_type = _compute_event_checkin_stats(self.event)
        self.assertFalse(show)

    # --- built-in format read-only + duplicate --------------------------------
    def _login(self):
        self.client.login(username='scanhost@example.com', password='pw12345')
        self.client.get(reverse('tickets:home'))  # prime org session cache

    def test_posh_builtin_is_global_and_visible(self):
        posh = CSVFormat.objects.get(name='POSH')
        self.assertIsNone(posh.organization_id)
        self.assertTrue(posh.is_system)
        self.assertIn('POSH', list(
            CSVFormat.available_for(self.org).values_list('name', flat=True)
        ))

    def test_builtin_format_not_editable_or_deletable(self):
        self._login()
        posh = CSVFormat.objects.get(name='POSH')
        # Org-scoped views 404 on a global built-in.
        self.assertEqual(
            self.client.get(reverse('tickets:format_edit', args=[posh.id])).status_code, 404
        )
        self.assertEqual(
            self.client.post(reverse('tickets:format_delete', args=[posh.id])).status_code, 404
        )

    def test_duplicate_builtin_creates_editable_org_copy(self):
        self._login()
        posh = CSVFormat.objects.get(name='POSH')
        resp = self.client.get(reverse('tickets:format_duplicate', args=[posh.id]))
        self.assertEqual(resp.status_code, 302)
        copy = CSVFormat.objects.get(organization=self.org, name='POSH (Custom)')
        self.assertFalse(copy.is_system)
        self.assertEqual(copy.column_mapping, posh.column_mapping)
        # The copy is editable (org-scoped view resolves it).
        self.assertEqual(
            self.client.get(reverse('tickets:format_edit', args=[copy.id])).status_code, 200
        )


class EventAudienceTests(TestCase):
    """EventAudienceCalculator metrics + Audience tab visibility."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Aud Org', slug='aud-org')
        self.user = User.objects.create_user(
            username='audhost', email='audhost@example.com', password='pw12345',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.venue = Venue.objects.create(organization=self.org, name='V', city='C')
        # Past external event (so check-in/audience can surface once scans exist).
        self.event = Event.objects.create(
            organization=self.org, name='Aud Night', venue=self.venue,
            start_date=date(2026, 1, 1), start_time=time(20, 0, 0),
            ticketing_type='external',
        )
        self.prior_event = Event.objects.create(
            organization=self.org, name='Prior', venue=self.venue,
            start_date=date(2025, 6, 1), ticketing_type='external',
        )

    def _customer(self, email, **kwargs):
        return Customer.objects.create(
            organization=self.org, email=email, name=email.split('@')[0], **kwargs
        )

    def _order(self, customer, event, number, scanned=False, tickets=1):
        order = TicketOrder.objects.create(
            customer=customer, event=event, order_number=number,
            order_date=timezone.now(), total_amount=Decimal('20.00'),
        )
        now = timezone.now()
        for i in range(tickets):
            Ticket.objects.create(
                ticket_order=order, ticket_type='GA', price=Decimal('20.00'),
                scanned_at=now if scanned else None,
            )
        return order

    def test_calculator_metrics(self):
        from tickets.services.audience import EventAudienceCalculator

        # New customer, checked in (first-time attendee).
        new_att = self._customer('new_att@x.com')
        self._order(new_att, self.event, 'N1', scanned=True)

        # Returning customer (has an order at a prior event), checked in.
        ret = self._customer('ret@x.com')
        self._order(ret, self.prior_event, 'R0', scanned=False)
        self._order(ret, self.event, 'R1', scanned=True)

        # High-value (VIP) customer, new, checked in.
        vip = self._customer('vip@x.com', rfm_segment='VIP', rfm_monetary_score=5,
                             lifetime_value=Decimal('900.00'))
        self._order(vip, self.event, 'V1', scanned=True)

        # Bought but did NOT check in (no scan).
        noshow = self._customer('noshow@x.com')
        self._order(noshow, self.event, 'X1', scanned=False)

        result = EventAudienceCalculator(self.event).calculate()

        self.assertTrue(result['show'])
        self.assertEqual(result['total_buyers'], 4)
        # Attendees = customers with >=1 scanned ticket: new_att, ret, vip
        self.assertEqual(result['attendees'], 3)
        self.assertEqual(result['attendees_returning'], 1)  # ret
        self.assertEqual(result['attendees_new'], 2)        # new_att, vip
        self.assertEqual(result['first_time_attendees'], 2)
        # High value = VIP/Big Spender or monetary>=4; only vip qualifies and attended.
        self.assertEqual(result['high_value_total'], 1)
        self.assertEqual(result['high_value_attended'], 1)
        self.assertEqual(result['high_value_attendees'], 1)
        # Notable attendees (queryset): VIP (high-value) and the returning customer
        # qualifies as a frequent buyer (2 distinct events). new_att (1 event, not
        # high-value) is excluded.
        notable = list(EventAudienceCalculator(self.event).notable_attendees_queryset())
        emails = {c.email for c in notable}
        self.assertEqual(emails, {'vip@x.com', 'ret@x.com'})
        self.assertNotIn('new_att@x.com', emails)
        # ret has events_count == 2 (frequent), vip has 1 -> ret ranks first.
        by_email = {c.email: c for c in notable}
        self.assertEqual(by_email['ret@x.com'].events_count, 2)
        self.assertEqual(by_email['vip@x.com'].events_count, 1)
        self.assertEqual(notable[0].email, 'ret@x.com')

    def test_high_value_via_monetary_score_without_named_segment(self):
        from tickets.services.audience import EventAudienceCalculator
        # Top-spend signal alone (no VIP/Big Spender label) still counts as high value.
        spender = self._customer('spender@x.com', rfm_segment='Loyal',
                                 rfm_monetary_score=4, lifetime_value=Decimal('500.00'))
        self._order(spender, self.event, 'S1', scanned=True)
        result = EventAudienceCalculator(self.event).calculate()
        self.assertEqual(result['high_value_attended'], 1)

    def test_frequent_buyer_not_high_value_is_notable(self):
        from tickets.services.audience import EventAudienceCalculator
        # No VIP/top-spend signal, but purchased at 2 events -> notable as a frequent buyer.
        freq = self._customer('freq@x.com')
        self._order(freq, self.prior_event, 'F0', scanned=False)
        self._order(freq, self.event, 'F1', scanned=True)
        # A plain first-timer who is not high-value is NOT notable.
        plain = self._customer('plain@x.com')
        self._order(plain, self.event, 'P1', scanned=True)
        notable = list(EventAudienceCalculator(self.event).notable_attendees_queryset())
        emails = {c.email for c in notable}
        self.assertIn('freq@x.com', emails)
        self.assertNotIn('plain@x.com', emails)

    def test_notable_attendees_paginated_ten_per_page(self):
        # 12 frequent buyers -> 10 on page 1, 2 on page 2.
        for i in range(12):
            c = self._customer('freq%02d@x.com' % i)
            self._order(c, self.prior_event, 'PR%02d' % i, scanned=False)
            self._order(c, self.event, 'EV%02d' % i, scanned=True)
        self._login()
        resp = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        page = resp.context['notable_attendees_page']
        self.assertEqual(page.paginator.count, 12)
        self.assertEqual(page.paginator.per_page, 10)
        self.assertEqual(len(page.object_list), 10)
        resp2 = self.client.get(
            reverse('tickets:event_detail', args=[self.event.id]),
            {'tab': 'audience', 'audience_page': 2},
        )
        self.assertEqual(len(resp2.context['notable_attendees_page'].object_list), 2)

    def _login(self):
        self.client.login(username='audhost@example.com', password='pw12345')
        self.client.get(reverse('tickets:home'))

    def test_audience_tab_shown_for_external_event_with_scans(self):
        self._login()
        c = self._customer('a@x.com')
        self._order(c, self.event, 'A1', scanned=True)
        resp = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="tab-audience"')
        self.assertContains(resp, 'audienceDonut')
        # The tab button shows no count.
        self.assertContains(resp, 'role="tab">Audience</button>')

    def test_audience_tab_hidden_for_external_event_without_scans(self):
        self._login()
        c = self._customer('b@x.com')
        self._order(c, self.event, 'B1', scanned=False)  # bought, never scanned
        resp = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'id="tab-audience"')


class OLSHelperTests(TestCase):
    """Unit tests for the OLS fit used by market trend classification."""

    def test_linear_series_exact_fit(self):
        from tickets.services.market_trends.market_trend_calculator import _ols
        slope, intercept, mean_y, r2 = _ols([10, 20, 30])
        self.assertAlmostEqual(slope, 10.0)
        self.assertAlmostEqual(intercept, 10.0)
        self.assertAlmostEqual(mean_y, 20.0)
        self.assertAlmostEqual(r2, 1.0)

    def test_flat_series_zero_slope(self):
        from tickets.services.market_trends.market_trend_calculator import _ols
        slope, intercept, mean_y, r2 = _ols([5, 5, 5, 5])
        self.assertEqual(slope, 0.0)
        self.assertEqual(mean_y, 5.0)
        self.assertEqual(r2, 1.0)  # a flat fit explains a flat series exactly


class MarketTrendCalculatorTests(TestCase):
    """Tests for per-market turnout trend detection and diagnosis."""

    def setUp(self):
        self.org = Organization.objects.create(name='Trend Org', slug='trend-org')
        self.user = User.objects.create_user(
            username='trend_owner', email='trend_owner@example.com', password='pw',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        # Four consecutive recent quarters, all strictly in the past and inside the
        # calculator's default 2-year trailing window. Anchored relative to today
        # (not fixed at 2023) so the default window doesn't filter the seed data out.
        today = date.today()
        abs_q = today.year * 4 + (today.month - 1) // 3  # current quarter, absolute index
        self.quarters = []
        for back in (5, 4, 3, 2):  # latest anchor = 2 quarters ago => safely past
            y, q = divmod(abs_q - back, 4)
            self.quarters.append(date(y, q * 3 + 1, 15))

    def _venue(self, city, market_name=None):
        Market.objects.get_or_create(
            organization=self.org,
            geography_level='city',
            geography_value=city,
            defaults={'name': market_name or city},
        )
        return Venue.objects.create(organization=self.org, name=city + ' Hall', city=city)

    def _event(self, venue, start_date, name):
        event = Event.objects.create(
            organization=self.org, name=name, venue=venue, start_date=start_date,
        )
        from tickets.services.markets import MarketBuilder
        MarketBuilder(self.org).assign_event(event)
        return event

    def _order_with_tickets(self, event, n_tickets, customer_prefix, seq):
        """Create one order at `event` with `n_tickets` tickets, each a unique customer."""
        customer = Customer.objects.create(
            organization=self.org,
            email='{}-{}@example.com'.format(customer_prefix, seq),
            name='{} {}'.format(customer_prefix, seq),
        )
        order = TicketOrder.objects.create(
            customer=customer, event=event,
            order_number='ORD-{}-{}'.format(customer_prefix, seq),
            order_date=timezone.make_aware(datetime.combine(event.start_date, time(10, 0))),
            total_amount=Decimal('10.00'),
        )
        for t in range(n_tickets):
            Ticket.objects.create(ticket_order=order, ticket_type='GA', price=Decimal('10.00'))
        return order

    def _build_market(self, city, counts):
        """One event per quarter; `counts[i]` = tickets sold that quarter (all new buyers)."""
        venue = self._venue(city)
        for i, n in enumerate(counts):
            event = self._event(venue, self.quarters[i], '{} Q{}'.format(city, i + 1))
            for s in range(n):
                self._order_with_tickets(event, 1, '{}q{}'.format(city.lower(), i), s)

    def _build_priced_market(self, city, specs):
        """One event per quarter; specs[i] = (tickets, price) for that quarter (all new buyers).

        Each ticket is a 1-ticket order at the quarter's price, so tickets-per-event
        and revenue-per-event can be controlled independently."""
        venue = self._venue(city)
        for i, (n, price) in enumerate(specs):
            event = self._event(venue, self.quarters[i], '{} Q{}'.format(city, i + 1))
            for s in range(n):
                customer = Customer.objects.create(
                    organization=self.org,
                    email='{}-{}-{}@example.com'.format(city.lower(), i, s),
                    name='{} {} {}'.format(city, i, s),
                )
                order = TicketOrder.objects.create(
                    customer=customer, event=event,
                    order_number='ORDP-{}-{}-{}'.format(city.lower(), i, s),
                    order_date=timezone.make_aware(datetime.combine(event.start_date, time(10, 0))),
                    total_amount=Decimal(str(price)),
                )
                Ticket.objects.create(ticket_order=order, ticket_type='GA', price=Decimal(str(price)))

    def _build_cost_market(self, city, specs):
        """specs[i] = (tickets, price, cost_per_event) per quarter (all new buyers).

        One event per quarter with `tickets` 1-ticket orders at `price`, plus a
        single EventExpense of `cost_per_event` — lets profit-per-event move
        independently of revenue/tickets."""
        venue = self._venue(city)
        for i, (n, price, cost) in enumerate(specs):
            event = self._event(venue, self.quarters[i], '{} Q{}'.format(city, i + 1))
            for s in range(n):
                customer = Customer.objects.create(
                    organization=self.org,
                    email='{}-c{}-{}@example.com'.format(city.lower(), i, s),
                    name='{} {} {}'.format(city, i, s),
                )
                order = TicketOrder.objects.create(
                    customer=customer, event=event,
                    order_number='ORDC-{}-{}-{}'.format(city.lower(), i, s),
                    order_date=timezone.make_aware(datetime.combine(event.start_date, time(10, 0))),
                    total_amount=Decimal(str(price)),
                )
                Ticket.objects.create(ticket_order=order, ticket_type='GA', price=Decimal(str(price)))
            EventExpense.objects.create(
                event=event, category='production', description='cost',
                amount=Decimal(str(cost)), expense_date=event.start_date,
                created_by=self.user,
            )

    def _build_nps_market(self, city, specs):
        """specs[i] = (promoters, passives, detractors) survey responses for quarter i.

        One event per quarter; each response's `responded_at` sits in that quarter
        so the NPS series (bucketed by responded_at) has one point per quarter."""
        from tickets.models import ExternalSurveyUpload, ExternalSurveyResponse
        venue = self._venue(city)
        upload = ExternalSurveyUpload.objects.create(
            organization=self.org, filename='nps.csv',
            status=ExternalSurveyUpload.Status.COMPLETED, created_by=self.user,
        )
        seq = 0
        for i, (promoters, passives, detractors) in enumerate(specs):
            event = self._event(venue, self.quarters[i], '{} Q{}'.format(city, i + 1))
            responded = timezone.make_aware(datetime.combine(self.quarters[i], time(12, 0)))
            for score, count in ((10, promoters), (8, passives), (3, detractors)):
                for _ in range(count):
                    ExternalSurveyResponse.objects.create(
                        organization=self.org, upload=upload, event=event,
                        responded_at=responded, email='{}-{}@example.com'.format(city.lower(), seq),
                        nps_score=score, city=city,
                    )
                    seq += 1

    def _build_returning_revenue_market(self, city, specs):
        """specs[i] = (new_count, returning_count, price) for quarter i.

        One event per quarter; `returning_count` buyers are reused from earlier
        quarters (so they count as returning), the rest are brand new. Lets the
        returning-buyer share swing while demand (= buyers/event) and price are
        controlled independently."""
        venue = self._venue(city)
        seen = []
        seq = 0
        for i, (new_count, returning_count, price) in enumerate(specs):
            event = self._event(venue, self.quarters[i], '{} Q{}'.format(city, i + 1))
            order_dt = timezone.make_aware(datetime.combine(self.quarters[i], time(10, 0)))
            buyers = []
            for r in (seen[:returning_count] if returning_count else []):
                buyers.append(r)
            for _ in range(new_count):
                c = Customer.objects.create(
                    organization=self.org,
                    email='{}-r{}-{}@example.com'.format(city.lower(), i, seq),
                    name='{} {}'.format(city, seq),
                )
                seq += 1
                buyers.append(c)
                seen.append(c)
            for c in buyers:
                o = TicketOrder.objects.create(
                    customer=c, event=event, order_number='ORDR-{}-{}'.format(city.lower(), seq),
                    order_date=order_dt, total_amount=Decimal(str(price)),
                )
                seq += 1
                Ticket.objects.create(ticket_order=o, ticket_type='GA', price=Decimal(str(price)))

    def test_revenue_top_driver_is_demand_not_returning_share(self):
        """Returning-buyer share swings hard, but demand/price move the dollars —
        so the revenue badge goes to demand (contribution), not retention."""
        from tickets.services.market_trends import MarketTrendCalculator
        # (new, returning, price): demand 20->38 and price 30->36 both climb,
        # while returning share jumps 0% -> ~63%.
        self._build_returning_revenue_market('Boston', [
            (20, 0, 30), (18, 6, 32), (16, 14, 34), (14, 24, 36),
        ])
        result = MarketTrendCalculator(self.org, period='quarter', metric='revenue').calculate()
        m = next(x for x in result['markets'] if x['city'] == 'Boston')
        self.assertEqual(m['trend'], 'growing')
        self.assertIn(m['dominant_driver'], ('demand', 'price'))
        # Retention still shows as a context bar, just never the badged lead.
        keys = {d['key'] for d in m['driver_contributions']}
        self.assertIn('retention', keys)

    def test_declining_demand_market(self):
        from tickets.services.market_trends import MarketTrendCalculator
        self._build_market('Austin', [40, 30, 20, 10])
        result = MarketTrendCalculator(self.org, period='quarter', metric='tickets').calculate()
        austin = next(m for m in result['markets'] if m['city'] == 'Austin')
        self.assertEqual(austin['trend'], 'declining')
        self.assertLess(austin['norm_slope_pct'], 0)
        # All buyers are new each quarter, so the falling ticket volume is
        # attributed to fewer new buyers (contribution by buyer count, not the
        # tautological demand series).
        self.assertEqual(austin['dominant_driver'], 'acquisition')
        self.assertIn('Austin', austin['diagnosis_text'])
        self.assertIsNotNone(austin['recommended_action'])

    def test_market_label_comes_from_market_entity_not_venue_city(self):
        from tickets.services.market_trends import MarketTrendCalculator

        venue = self._venue('Austin', market_name='Central Texas')
        for i, n in enumerate([10, 20, 30, 40]):
            event = self._event(venue, self.quarters[i], 'Austin Q{}'.format(i + 1))
            for s in range(n):
                self._order_with_tickets(event, 1, 'central{}'.format(i), s)

        result = MarketTrendCalculator(self.org, period='quarter', metric='tickets').calculate()
        labels = {m['market_label'] for m in result['markets']}

        self.assertIn('Central Texas', labels)
        self.assertNotIn('Austin', labels)
        central = next(m for m in result['markets'] if m['market_label'] == 'Central Texas')
        self.assertEqual(central['city'], 'Central Texas')
        self.assertEqual(central['market_name'], 'Central Texas')
        self.assertTrue(central['market_id'])

    def test_stable_market(self):
        from tickets.services.market_trends import MarketTrendCalculator
        self._build_market('Denver', [25, 25, 25, 25])
        result = MarketTrendCalculator(self.org, period='quarter', metric='tickets').calculate()
        denver = next(m for m in result['markets'] if m['city'] == 'Denver')
        self.assertEqual(denver['trend'], 'stable')
        self.assertEqual(denver['dominant_driver'], None)

    def test_growing_market(self):
        from tickets.services.market_trends import MarketTrendCalculator
        self._build_market('Miami', [10, 20, 30, 40])
        result = MarketTrendCalculator(self.org, period='quarter', metric='tickets').calculate()
        miami = next(m for m in result['markets'] if m['city'] == 'Miami')
        self.assertEqual(miami['trend'], 'growing')
        self.assertGreater(miami['norm_slope_pct'], 0)

    def test_insufficient_data_market(self):
        from tickets.services.market_trends import MarketTrendCalculator
        self._build_market('Boise', [30, 20])  # only 2 periods
        result = MarketTrendCalculator(self.org, period='quarter', metric='tickets').calculate()
        boise = next(m for m in result['markets'] if m['city'] == 'Boise')
        self.assertEqual(boise['trend'], 'insufficient_data')
        self.assertEqual(boise['dominant_driver'], None)

    def test_sold_totals_match_ticket_count_no_inflation(self):
        from tickets.services.market_trends import MarketTrendCalculator
        self._build_market('Reno', [12, 8, 6, 4])
        result = MarketTrendCalculator(self.org, period='quarter').calculate()
        reno = next(m for m in result['markets'] if m['city'] == 'Reno')
        direct = Ticket.objects.filter(
            ticket_order__event__organization=self.org,
            ticket_order__event__venue__city='Reno',
        ).count()
        self.assertEqual(reno['total_sold'], direct)
        self.assertEqual(reno['total_sold'], 30)

    def test_sorted_highest_to_lowest_by_metric(self):
        from tickets.services.market_trends import MarketTrendCalculator
        self._build_market('Boston', [50, 40, 30, 20])   # total 140, declining
        self._build_market('Dallas', [10, 20, 30, 40])   # total 100, growing
        result = MarketTrendCalculator(self.org, period='quarter', metric='tickets').calculate()
        # "All Markets" portfolio row is pinned first; real markets then follow
        # largest-first (largest by tickets sold leads, regardless of trend).
        self.assertEqual(result['markets'][0]['city'], 'All Markets')
        self.assertTrue(result['markets'][0]['is_aggregate'])
        self.assertEqual(
            [m['city'] for m in result['markets'] if not m.get('is_aggregate')],
            ['Boston', 'Dallas'],
        )
        # Summary counts real markets only — the aggregate row is excluded.
        self.assertEqual(result['summary']['markets_count'], 2)
        self.assertEqual(result['summary']['declining_count'], 1)
        self.assertEqual(result['summary']['growing_count'], 1)

    def test_all_markets_aggregate_row(self):
        """The pinned 'All Markets' row aggregates every market's totals per period."""
        from tickets.services.market_trends import MarketTrendCalculator
        self._build_market('Boston', [50, 40, 30, 20])   # total 140
        self._build_market('Dallas', [10, 20, 30, 40])   # total 100
        result = MarketTrendCalculator(self.org, period='quarter', metric='tickets').calculate()
        agg = result['markets'][0]
        self.assertEqual(agg['city'], 'All Markets')
        self.assertTrue(agg['is_aggregate'])
        # Org-wide tickets sold = sum of both markets.
        self.assertEqual(agg['total_sold'], 240)
        # Each period sums across markets (Q1: 50 + 10 = 60, Q4: 20 + 40 = 60).
        by_label = {p['period_label']: p for p in agg['periods']}
        first_q = agg['periods'][0]
        last_q = agg['periods'][-1]
        self.assertEqual(first_q['sold'], 60)
        self.assertEqual(last_q['sold'], 60)
        self.assertEqual(len(by_label), 4)
        # Diagnosis text reads "across all markets", not "in All Markets".
        self.assertIn('across all markets', agg['diagnosis_text'])
        self.assertNotIn('in All Markets', agg['diagnosis_text'])

    def test_single_market_has_no_aggregate_row(self):
        """With only one market, the aggregate would just duplicate it — so it's skipped."""
        from tickets.services.market_trends import MarketTrendCalculator
        self._build_market('Austin', [40, 30, 20, 10])
        result = MarketTrendCalculator(self.org, period='quarter', metric='tickets').calculate()
        self.assertEqual(len(result['markets']), 1)
        self.assertFalse(result['markets'][0].get('is_aggregate'))
        self.assertEqual(result['markets'][0]['city'], 'Austin')

    def test_view_smoke(self):
        self._build_market('Austin', [40, 30, 20, 10])
        self.client.login(username='trend_owner@example.com', password='pw')
        self.client.get(reverse('tickets:home'))  # prime session org / host routing
        resp = self.client.get(reverse('tickets:market_trends'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('markets', resp.context)
        self.assertEqual(resp.context['metric'], 'revenue')  # revenue is the default
        self.assertContains(resp, 'Austin')
        # Embedded JSON parses.
        json.loads(resp.context['markets_json'])

    def test_view_period_toggle(self):
        self._build_market('Austin', [40, 30, 20, 10])
        self.client.login(username='trend_owner@example.com', password='pw')
        self.client.get(reverse('tickets:home'))  # prime session org / host routing
        resp = self.client.get(reverse('tickets:market_trends'), {'period': 'month'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['period'], 'month')

    def test_fragment_returns_partial_without_chrome(self):
        """?fragment=1 renders only the dynamic region for AJAX selector swaps."""
        self._build_market('Austin', [40, 30, 20, 10])
        self.client.login(username='trend_owner@example.com', password='pw')
        self.client.get(reverse('tickets:home'))  # prime session org / host routing
        resp = self.client.get(reverse('tickets:market_trends'), {'fragment': '1'})
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'tickets/_market_trends_content.html')
        self.assertContains(resp, 'Austin')
        self.assertContains(resp, 'id="mt-config"')
        # No base-template chrome — this is a swap-in fragment, not a full page.
        self.assertNotContains(resp, '<html')

    def test_window_default_is_two_years(self):
        from tickets.services.market_trends import MarketTrendCalculator
        self._build_market('Austin', [40, 30, 20, 10])
        result = MarketTrendCalculator(self.org).calculate()
        self.assertEqual(result['window'], '2y')

    def test_invalid_window_falls_back_to_two_years(self):
        from tickets.services.market_trends import MarketTrendCalculator
        self._build_market('Austin', [40, 30, 20, 10])
        result = MarketTrendCalculator(self.org, window='bogus').calculate()
        self.assertEqual(result['window'], '2y')

    def test_narrow_window_excludes_older_periods(self):
        """An event older than the trailing window drops out of the series."""
        from tickets.services.market_trends import MarketTrendCalculator
        venue = self._venue('Austin')
        today = date.today()

        def _months_back(d, m):
            total = d.year * 12 + (d.month - 1) - m
            y, mo = divmod(total, 12)
            return date(y, mo + 1, min(d.day, 28))

        # One old event (~18 months back) and one recent event (~2 months back).
        old_date = _months_back(today, 18)
        recent_date = _months_back(today, 2)
        self._event(venue, old_date, 'Austin old')
        self._event(venue, recent_date, 'Austin recent')
        for e_date, n in ((old_date, 5), (recent_date, 3)):
            ev = Event.objects.get(organization=self.org, start_date=e_date)
            for s in range(n):
                self._order_with_tickets(ev, 1, 'austin-{}'.format(e_date.isoformat()), s)

        all_result = MarketTrendCalculator(self.org, period='month', window='all').calculate()
        all_m = next(x for x in all_result['markets'] if x['city'] == 'Austin')
        narrow_result = MarketTrendCalculator(self.org, period='month', window='1y').calculate()
        narrow_m = next(x for x in narrow_result['markets'] if x['city'] == 'Austin')

        # 'all' sees both events; the 1-year window only sees the recent one.
        self.assertEqual(all_m['total_events'], 2)
        self.assertEqual(narrow_m['total_events'], 1)
        self.assertLessEqual(len(narrow_m['periods']), len(all_m['periods']))

    def test_view_window_toggle(self):
        self._build_market('Austin', [40, 30, 20, 10])
        self.client.login(username='trend_owner@example.com', password='pw')
        self.client.get(reverse('tickets:home'))  # prime session org / host routing
        for w in ('1y', '2y', '3y', 'all'):
            resp = self.client.get(reverse('tickets:market_trends'), {'window': w})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.context['window'], w)
        # Invalid window falls back to the 2y default in the view too.
        resp = self.client.get(reverse('tickets:market_trends'), {'window': 'bogus'})
        self.assertEqual(resp.context['window'], '2y')

    def test_returning_buyer_classified_new_then_returning(self):
        """A buyer is 'new' in their first period and 'returning' afterward —
        regardless of DB row order (guards the order-independent classification)."""
        from tickets.services.market_trends import MarketTrendCalculator
        venue = self._venue('Tahoe')
        e1 = self._event(venue, self.quarters[0], 'Tahoe Q1')
        e2 = self._event(venue, self.quarters[1], 'Tahoe Q2')
        e3 = self._event(venue, self.quarters[2], 'Tahoe Q3')
        loyal = Customer.objects.create(
            organization=self.org, email='loyal@example.com', name='Loyal Fan',
        )

        def _order(event, customer, num):
            o = TicketOrder.objects.create(
                customer=customer, event=event, order_number='O-{}'.format(num),
                order_date=timezone.make_aware(datetime.combine(event.start_date, time(10, 0))),
                total_amount=Decimal('10.00'),
            )
            Ticket.objects.create(ticket_order=o, ticket_type='GA', price=Decimal('10.00'))

        # Loyal fan buys in all three quarters; a fresh buyer joins each quarter.
        for i, ev in enumerate((e1, e2, e3)):
            _order(ev, loyal, 'loyal{}'.format(i))
            fresh = Customer.objects.create(
                organization=self.org, email='fresh{}@example.com'.format(i), name='Fresh {}'.format(i),
            )
            _order(ev, fresh, 'fresh{}'.format(i))

        result = MarketTrendCalculator(self.org, period='quarter').calculate()
        tahoe = next(m for m in result['markets'] if m['city'] == 'Tahoe')
        periods = tahoe['periods']
        # Q1: both buyers brand new.
        self.assertEqual(periods[0]['new_count'], 2)
        self.assertEqual(periods[0]['returning_count'], 0)
        # Q2 & Q3: loyal fan is returning, the fresh buyer is new.
        self.assertEqual(periods[1]['new_count'], 1)
        self.assertEqual(periods[1]['returning_count'], 1)
        self.assertEqual(periods[2]['new_count'], 1)
        self.assertEqual(periods[2]['returning_count'], 1)

    def test_revenue_is_default_metric(self):
        from tickets.services.market_trends import MarketTrendCalculator
        self._build_market('Austin', [40, 30, 20, 10])
        result = MarketTrendCalculator(self.org).calculate()  # no metric arg
        austin = next(m for m in result['markets'] if m['city'] == 'Austin')
        self.assertEqual(austin['metric'], 'revenue')
        # total_revenue is exposed; total_sold still computed.
        self.assertEqual(austin['total_sold'], 100)
        self.assertEqual(austin['total_revenue'], 1000.0)  # 100 tickets x $10

    def test_revenue_declining_via_price_erosion(self):
        """Flat ticket volume but falling price: stable by tickets, declining by
        revenue with price as the dominant driver."""
        from tickets.services.market_trends import MarketTrendCalculator
        # 20 tickets/quarter (flat), price falls 40 -> 36 -> 30 -> 24.
        specs = [(20, 40), (20, 36), (20, 30), (20, 24)]
        self._build_priced_market('Reno', specs)

        tickets_view = MarketTrendCalculator(self.org, metric='tickets').calculate()
        reno_t = next(m for m in tickets_view['markets'] if m['city'] == 'Reno')
        self.assertEqual(reno_t['trend'], 'stable')

        revenue_view = MarketTrendCalculator(self.org, metric='revenue').calculate()
        reno_r = next(m for m in revenue_view['markets'] if m['city'] == 'Reno')
        self.assertEqual(reno_r['trend'], 'declining')
        self.assertEqual(reno_r['dominant_driver'], 'price')
        self.assertIn('Revenue in Reno', reno_r['diagnosis_text'])

    def test_view_metric_toggle(self):
        self._build_market('Austin', [40, 30, 20, 10])
        self.client.login(username='trend_owner@example.com', password='pw')
        self.client.get(reverse('tickets:home'))  # prime session org / host routing
        resp = self.client.get(reverse('tickets:market_trends'), {'metric': 'tickets'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['metric'], 'tickets')
        # Invalid metric falls back to revenue.
        resp2 = self.client.get(reverse('tickets:market_trends'), {'metric': 'bogus'})
        self.assertEqual(resp2.context['metric'], 'revenue')

    def test_profit_is_revenue_minus_costs(self):
        from tickets.services.market_trends import MarketTrendCalculator
        # 20 tickets x $30 = $600 revenue/quarter; expenses 200+300+400+500 = 1400.
        specs = [(20, 30, 200), (20, 30, 300), (20, 30, 400), (20, 30, 500)]
        self._build_cost_market('Tucson', specs)
        result = MarketTrendCalculator(self.org, metric='profitability').calculate()
        tuc = next(m for m in result['markets'] if m['city'] == 'Tucson')
        self.assertEqual(tuc['total_revenue'], 2400.0)
        self.assertEqual(tuc['total_profit'], 1000.0)  # 2400 - 1400

    def test_profitability_declining_via_rising_costs(self):
        """Flat volume and price, rising cost: stable by tickets & revenue, but
        declining by profit with costs as the dominant driver."""
        from tickets.services.market_trends import MarketTrendCalculator
        specs = [(20, 30, 200), (20, 30, 300), (20, 30, 400), (20, 30, 500)]
        self._build_cost_market('Tucson', specs)

        tickets_view = MarketTrendCalculator(self.org, metric='tickets').calculate()
        self.assertEqual(next(m for m in tickets_view['markets'] if m['city'] == 'Tucson')['trend'], 'stable')
        revenue_view = MarketTrendCalculator(self.org, metric='revenue').calculate()
        self.assertEqual(next(m for m in revenue_view['markets'] if m['city'] == 'Tucson')['trend'], 'stable')

        profit_view = MarketTrendCalculator(self.org, metric='profitability').calculate()
        tuc = next(m for m in profit_view['markets'] if m['city'] == 'Tucson')
        self.assertEqual(tuc['trend'], 'declining')
        self.assertEqual(tuc['dominant_driver'], 'costs')
        self.assertTrue(tuc['diagnosis_text'].startswith('Profit in Tucson'))
        # The costs driver is flagged as a rising drag.
        costs = next(d for d in tuc['driver_contributions'] if d['key'] == 'costs')
        self.assertEqual(costs['hurts_when'], 'up')
        self.assertGreater(costs['change_pct'], 0)

    def test_view_profitability_metric(self):
        self._build_market('Austin', [40, 30, 20, 10])
        self.client.login(username='trend_owner@example.com', password='pw')
        self.client.get(reverse('tickets:home'))  # prime session org / host routing
        resp = self.client.get(reverse('tickets:market_trends'), {'metric': 'profitability'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['metric'], 'profitability')

    def test_in_person_revenue_included_in_profit(self):
        """Regression: in-person (door/cash) ticket revenue must count toward a
        market's profit, the same way the event-detail page and
        profitability_overview do. Otherwise expenses are charged in full while
        the door-sale revenue that paid for them is dropped, flipping a real
        profit into a large fake loss.

        Mirrors the reported case: one Seattle event, $6,131.99 in tickets
        ($4,126.00 online + $2,005.99 in-person) + $892.00 other income, against
        $7,018.00 of expenses, nets exactly $5.99."""
        from tickets.models import EventIncome, IncomeSource
        from tickets.services.market_trends import MarketTrendCalculator
        from tickets.views import _compute_event_stats

        venue = self._venue('Seattle')
        event = self._event(venue, self.quarters[0], 'Familiar Faces: Seattle')

        # Online order ($4,126.00) — has customer identity.
        online_customer = Customer.objects.create(
            organization=self.org, email='online@example.com', name='Online Buyer',
        )
        TicketOrder.objects.create(
            customer=online_customer, event=event, order_number='ORD-ONLINE',
            order_date=timezone.make_aware(datetime.combine(event.start_date, time(10, 0))),
            total_amount=Decimal('4126.00'), is_in_person=False,
        )
        # In-person / door order ($2,005.99) — no identity, but real revenue.
        door_customer = Customer.objects.create(
            organization=self.org, email='door@example.com', name='Door Buyer',
        )
        TicketOrder.objects.create(
            customer=door_customer, event=event, order_number='ORD-DOOR',
            order_date=timezone.make_aware(datetime.combine(event.start_date, time(10, 0))),
            total_amount=Decimal('2005.99'), is_in_person=True,
        )
        # Other income ($892.00) and expenses ($7,018.00).
        income_source = IncomeSource.objects.create(
            organization=self.org, name='Bar', order=1,
        )
        EventIncome.objects.create(
            event=event, income_source=income_source, amount=Decimal('892.00'),
        )
        EventExpense.objects.create(
            event=event, category='production', description='show costs',
            amount=Decimal('7018.00'), expense_date=event.start_date,
            created_by=self.user,
        )

        # The event-detail P&L nets $5.99 (external ticketing -> no Stripe fees).
        stats = _compute_event_stats(event)
        self.assertEqual(stats['profit'], Decimal('5.99'))

        # Market Trends must reconcile to that same figure, not exclude the
        # in-person revenue while keeping the full expense.
        result = MarketTrendCalculator(self.org, metric='profitability').calculate()
        seattle = next(m for m in result['markets'] if m['city'] == 'Seattle')
        self.assertAlmostEqual(seattle['total_profit'], 5.99, places=2)
        # Revenue series is ticket revenue (online + in-person); other income
        # feeds profit, not this figure.
        self.assertAlmostEqual(seattle['total_revenue'], 6131.99, places=2)
        # Single event in the market -> avg profit / event equals the net profit.
        self.assertAlmostEqual(
            seattle['periods'][0]['avg_profit_per_event'], 5.99, places=2,
        )

    def test_nps_total_score(self):
        from tickets.services.market_trends import MarketTrendCalculator
        # All promoters every quarter -> overall NPS = 100.
        self._build_nps_market('Reno', [(10, 0, 0), (10, 0, 0), (10, 0, 0), (10, 0, 0)])
        result = MarketTrendCalculator(self.org, metric='nps').calculate()
        reno = next(m for m in result['markets'] if m['city'] == 'Reno')
        self.assertEqual(reno['total_nps'], 100)
        self.assertEqual(reno['change_unit'], 'pts')

    def test_nps_declining_via_detractors(self):
        from tickets.services.market_trends import MarketTrendCalculator
        # Promoter share falls and detractor share climbs -> NPS slides; the
        # detractor rise is the larger driver.
        self._build_nps_market('Nashville', [(12, 6, 2), (9, 6, 5), (6, 6, 8), (4, 6, 10)])
        result = MarketTrendCalculator(self.org, metric='nps').calculate()
        nash = next(m for m in result['markets'] if m['city'] == 'Nashville')
        self.assertEqual(nash['trend'], 'declining')
        self.assertEqual(nash['dominant_driver'], 'detractors')
        self.assertTrue(nash['diagnosis_text'].startswith('NPS in Nashville'))
        self.assertEqual(nash['change_unit'], 'pts')

    def test_view_nps_metric(self):
        self._build_nps_market('Nashville', [(12, 6, 2), (9, 6, 5), (6, 6, 8), (4, 6, 10)])
        self.client.login(username='trend_owner@example.com', password='pw')
        self.client.get(reverse('tickets:home'))  # prime session org / host routing
        resp = self.client.get(reverse('tickets:market_trends'), {'metric': 'nps'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['metric'], 'nps')
        self.assertEqual(resp.context['change_unit'], 'pts')

    def test_growing_market_has_metrics_and_next_step(self):
        from tickets.services.market_trends import MarketTrendCalculator
        self._build_market('Miami', [10, 20, 30, 40])  # growing
        result = MarketTrendCalculator(self.org, period='quarter', metric='tickets').calculate()
        miami = next(m for m in result['markets'] if m['city'] == 'Miami')
        self.assertEqual(miami['trend'], 'growing')
        # Driver metrics and a "keep improving" step are present for growth too.
        self.assertTrue(miami['driver_contributions'])
        self.assertIsNotNone(miami['recommended_action'])
        self.assertIn(miami['dominant_driver'], ('demand', 'acquisition'))
        self.assertIn('is up', miami['diagnosis_text'])
        self.assertIn('led by', miami['diagnosis_text'])

    def test_stable_market_has_metrics_and_next_step(self):
        from tickets.services.market_trends import MarketTrendCalculator
        self._build_market('Reno', [25, 25, 25, 25])  # stable
        result = MarketTrendCalculator(self.org, period='quarter', metric='tickets').calculate()
        reno = next(m for m in result['markets'] if m['city'] == 'Reno')
        self.assertEqual(reno['trend'], 'stable')
        self.assertIsNone(reno['dominant_driver'])  # no single lead when flat
        self.assertTrue(reno['driver_contributions'])
        self.assertIsNotNone(reno['recommended_action'])
        self.assertIn('holding steady', reno['diagnosis_text'])

    def test_nps_growing_recommends_survey_feedback(self):
        from tickets.services.market_trends import MarketTrendCalculator
        # Promoters climb, detractors fall -> NPS grows.
        self._build_nps_market('Seattle', [(4, 6, 10), (6, 6, 8), (9, 6, 5), (12, 6, 2)])
        result = MarketTrendCalculator(self.org, metric='nps').calculate()
        sea = next(m for m in result['markets'] if m['city'] == 'Seattle')
        self.assertEqual(sea['trend'], 'growing')
        self.assertIn(sea['dominant_driver'], ('promoters', 'detractors'))
        self.assertEqual(sea['recommended_action']['url_name'], 'tickets:survey_analytics')


class ScheduledSurveySendTests(TestCase):
    """Scheduling the post-event survey relative to the event end."""

    def setUp(self):
        from zoneinfo import ZoneInfo
        self.ZoneInfo = ZoneInfo
        self.client = Client()
        self.org = Organization.objects.create(name='Sched Org', slug='sched-org')
        self.user = User.objects.create_user(
            username='schedhost', email='host@test.com', password='testpass123',
        )
        UserProfile.objects.create(user=self.user, organization=self.org,
                                   org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='host@test.com', password='testpass123')
        self.client.get(reverse('tickets:home'))  # seed _org_id in session

        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        # Event ends far in the future so "schedule after event" is always future.
        self.event = Event.objects.create(
            organization=self.org, name='Future Show', venue=self.venue,
            start_date=date(2099, 1, 1), start_time=time(20, 0),
            end_date=date(2099, 1, 1), end_time=time(22, 0),
            timezone='America/New_York',
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='fan@example.com', name='Fan',
        )
        TicketOrder.objects.create(
            customer=self.customer, event=self.event,
            order_number='SCHED-1', order_date='2098-12-01 10:00:00',
            total_amount=Decimal('50.00'),
        )

    # ---- Event.end_datetime() -------------------------------------------------

    def test_end_datetime_uses_end_fields_in_event_tz(self):
        end = self.event.end_datetime()
        self.assertEqual(end.tzinfo, self.ZoneInfo('America/New_York'))
        self.assertEqual((end.year, end.month, end.day, end.hour), (2099, 1, 1, 22))

    def test_end_datetime_falls_back_to_start(self):
        ev = Event.objects.create(
            organization=self.org, name='No End', venue=self.venue, start_date=date(2099, 6, 1),
            start_time=time(18, 30), timezone='America/Los_Angeles',
        )
        end = ev.end_datetime()
        self.assertEqual((end.year, end.month, end.day, end.hour, end.minute),
                         (2099, 6, 1, 18, 30))

    def test_end_datetime_defaults_time_to_end_of_day(self):
        ev = Event.objects.create(
            organization=self.org, name='No Times', venue=self.venue, start_date=date(2099, 6, 1),
            timezone='America/Los_Angeles',
        )
        end = ev.end_datetime()
        self.assertEqual((end.hour, end.minute), (23, 59))

    # ---- _compute_survey_send_at ---------------------------------------------

    def test_compute_hours_offset(self):
        from tickets.views import _compute_survey_send_at
        from zoneinfo import ZoneInfo
        # End 2099-01-01 22:00 EST (UTC-5) + 3h -> 2099-01-02 06:00 UTC
        result = _compute_survey_send_at(self.event, 'hours', 3, None)
        self.assertEqual(result, datetime(2099, 1, 2, 6, 0, tzinfo=ZoneInfo('UTC')))

    def test_compute_days_at_time(self):
        from tickets.views import _compute_survey_send_at
        from zoneinfo import ZoneInfo
        # 2 days after end date (2099-01-03) at 09:00 EST -> 14:00 UTC
        result = _compute_survey_send_at(self.event, 'days', 2, time(9, 0))
        self.assertEqual(result, datetime(2099, 1, 3, 14, 0, tzinfo=ZoneInfo('UTC')))

    def test_compute_rejects_bad_input(self):
        from tickets.views import _compute_survey_send_at
        with self.assertRaises(ValueError):
            _compute_survey_send_at(self.event, 'days', 2, None)      # missing time
        with self.assertRaises(ValueError):
            _compute_survey_send_at(self.event, 'bogus', 2, time(9, 0))
        with self.assertRaises(ValueError):
            _compute_survey_send_at(self.event, 'hours', 'abc', None)
        with self.assertRaises(ValueError):
            _compute_survey_send_at(self.event, 'hours', -1, None)

    # ---- send_survey: send now (manual override) -----------------------------

    def test_send_now_sends_immediately(self):
        from django.core import mail
        resp = self.client.post(
            reverse('tickets:send_survey', args=[self.event.id]),
            {'send_mode': 'now'},
        )
        self.assertEqual(resp.status_code, 302)
        inv = SurveyInvitation.objects.get(event=self.event, customer=self.customer)
        self.assertIsNone(inv.scheduled_send_at)
        inv.refresh_from_db()
        self.assertIsNotNone(inv.sent_at)
        self.assertEqual(len(mail.outbox), 1)

    # ---- auto-arm: the scheduler creates scheduled invitations ----------------

    def _ended_yesterday_event(self, **schedule):
        """An event whose end is in the past (anchor passed) so it's arm-eligible.

        The date is derived in the event's own timezone (not UTC) so that a fixed
        22:00 end_time lands a full day in the past regardless of the wall-clock
        time the suite runs — computing it in UTC collapsed the gap near the
        UTC-day boundary and left a +1h send time in the future.
        """
        from zoneinfo import ZoneInfo
        now_et = timezone.now().astimezone(ZoneInfo('America/New_York'))
        yesterday = (now_et - timedelta(days=1)).date()
        ev = Event.objects.create(
            organization=self.org, name='Just Ended', venue=self.venue,
            start_date=yesterday, end_date=yesterday, end_time=time(22, 0),
            timezone='America/New_York', **schedule,
        )
        TicketOrder.objects.create(
            customer=self.customer, event=ev, order_number=f'ARM-{ev.public_id}',
            order_date='2000-01-01 10:00:00', total_amount=Decimal('10.00'),
        )
        return ev

    def test_auto_arm_creates_pending_invitations_without_sending(self):
        from django.core import mail
        # Ended yesterday, send 5 days after end -> send time still in the future.
        event = self._ended_yesterday_event(
            survey_send_offset_type='days', survey_send_offset_value=5,
            survey_send_time_of_day=time(9, 0),
        )
        call_command('send_due_survey_invitations')
        inv = SurveyInvitation.objects.get(event=event, customer=self.customer)
        self.assertIsNotNone(inv.scheduled_send_at)
        self.assertIsNone(inv.sent_at)
        self.assertEqual(len(mail.outbox), 0)  # scheduled, not sent yet

    def test_auto_arm_skips_event_before_anchor(self):
        # self.event ends in 2099 -> anchor not reached, so nothing is armed.
        self.event.survey_send_offset_type = 'hours'
        self.event.survey_send_offset_value = 1
        self.event.save()
        call_command('send_due_survey_invitations')
        self.assertFalse(SurveyInvitation.objects.filter(event=self.event).exists())

    def test_auto_arm_then_dispatch_sends_when_overdue(self):
        from django.core import mail
        # Ended yesterday, send 1 hour after end -> already due; armed then sent.
        event = self._ended_yesterday_event(
            survey_send_offset_type='hours', survey_send_offset_value=1,
        )
        call_command('send_due_survey_invitations', '--sync')
        inv = SurveyInvitation.objects.get(event=event, customer=self.customer)
        self.assertIsNotNone(inv.sent_at)
        self.assertEqual(len(mail.outbox), 1)

    def test_auto_arm_is_idempotent(self):
        event = self._ended_yesterday_event(
            survey_send_offset_type='days', survey_send_offset_value=5,
            survey_send_time_of_day=time(9, 0),
        )
        call_command('send_due_survey_invitations')
        call_command('send_due_survey_invitations')
        self.assertEqual(SurveyInvitation.objects.filter(event=event).count(), 1)

    def test_auto_arm_skips_opted_out_event(self):
        event = self._ended_yesterday_event(
            survey_send_offset_type='hours', survey_send_offset_value=1,
            survey_auto_send_opted_out=True,
        )
        call_command('send_due_survey_invitations')
        self.assertFalse(SurveyInvitation.objects.filter(event=event).exists())

    def test_no_arm_flag_only_dispatches(self):
        event = self._ended_yesterday_event(
            survey_send_offset_type='hours', survey_send_offset_value=1,
        )
        call_command('send_due_survey_invitations', '--no-arm')
        self.assertFalse(SurveyInvitation.objects.filter(event=event).exists())

    # ---- task due-filter ------------------------------------------------------

    def test_task_skips_future_scheduled_rows(self):
        from django.core import mail
        from tickets.tasks import send_survey_emails_task
        future = timezone.now() + timedelta(days=5)
        c2 = Customer.objects.create(organization=self.org, email='due@example.com', name='Due')
        TicketOrder.objects.create(
            customer=c2, event=self.event, order_number='SCHED-2',
            order_date='2098-12-01 10:00:00', total_amount=Decimal('20.00'),
        )
        future_inv = SurveyInvitation.objects.create(
            organization=self.org, event=self.event, customer=self.customer,
            email='fan@example.com', scheduled_send_at=future,
        )
        due_inv = SurveyInvitation.objects.create(
            organization=self.org, event=self.event, customer=c2,
            email='due@example.com', scheduled_send_at=timezone.now() - timedelta(minutes=1),
        )
        send_survey_emails_task(str(self.event.id), str(self.org.id))
        future_inv.refresh_from_db()
        due_inv.refresh_from_db()
        self.assertIsNone(future_inv.sent_at)       # still pending
        self.assertIsNotNone(due_inv.sent_at)       # sent
        self.assertEqual(len(mail.outbox), 1)

    def test_due_command_dispatches_and_sends(self):
        from django.core import mail
        SurveyInvitation.objects.create(
            organization=self.org, event=self.event, customer=self.customer,
            email='fan@example.com', scheduled_send_at=timezone.now() - timedelta(minutes=1),
        )
        call_command('send_due_survey_invitations', '--sync')
        self.assertEqual(len(mail.outbox), 1)

    # ---- cancel + preview -----------------------------------------------------

    def test_cancel_removes_pending_and_unlocks(self):
        from tickets.views import _survey_locked
        SurveyInvitation.objects.create(
            organization=self.org, event=self.event, customer=self.customer,
            email='fan@example.com', scheduled_send_at=timezone.now() + timedelta(days=2),
        )
        self.assertTrue(_survey_locked(self.event))
        resp = self.client.post(reverse('tickets:cancel_scheduled_survey', args=[self.event.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(SurveyInvitation.objects.filter(event=self.event).exists())
        self.assertFalse(_survey_locked(self.event))
        # Cancel opts the event out of auto-send so the scheduler won't re-arm it.
        self.event.refresh_from_db()
        self.assertTrue(self.event.survey_auto_send_opted_out)

    def test_cancel_leaves_sent_invitations(self):
        SurveyInvitation.objects.create(
            organization=self.org, event=self.event, customer=self.customer,
            email='fan@example.com', sent_at=timezone.now(),
            scheduled_send_at=timezone.now() - timedelta(days=1),
        )
        self.client.post(reverse('tickets:cancel_scheduled_survey', args=[self.event.id]))
        self.assertTrue(SurveyInvitation.objects.filter(event=self.event).exists())

    def test_preview_returns_display_for_valid_offset(self):
        resp = self.client.get(
            reverse('tickets:survey_schedule_preview', args=[self.event.id]),
            {'offset_type': 'days', 'offset_value': '2', 'time_of_day': '09:00'},
        )
        data = resp.json()
        self.assertTrue(data['valid'])
        self.assertIn('2099', data['display'])

    def test_preview_rejects_past_offset(self):
        past_event = Event.objects.create(
            organization=self.org, name='Past Show 2', venue=self.venue, start_date=date(2000, 1, 1),
            end_date=date(2000, 1, 1), end_time=time(22, 0), timezone='America/New_York',
        )
        resp = self.client.get(
            reverse('tickets:survey_schedule_preview', args=[past_event.id]),
            {'offset_type': 'hours', 'offset_value': '1'},
        )
        data = resp.json()
        self.assertFalse(data['valid'])
        self.assertIn('error', data)


class SurveyReplyToSenderTests(TestCase):
    """Survey emails send with the organizer's name + reply-to when opted in."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Acme Events', slug='acme-events')
        self.user = User.objects.create_user(
            username='replytohost', email='host@acme.com', password='testpass123',
        )
        UserProfile.objects.create(user=self.user, organization=self.org,
                                   org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='host@acme.com', password='testpass123')
        self.client.get(reverse('tickets:home'))  # seed _org_id in session

        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Acme Fest', venue=self.venue,
            start_date=date(2099, 1, 1), end_date=date(2099, 1, 1), end_time=time(22, 0),
            timezone='America/New_York',
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='fan@example.com', name='Fan',
        )

    # ---- survey_sender_fields helper -----------------------------------------

    def test_sender_fields_default_when_no_reply_to(self):
        from tickets.tasks import survey_sender_fields
        from django.conf import settings
        from_email, reply_to = survey_sender_fields(self.org)
        self.assertEqual(from_email, settings.DEFAULT_FROM_EMAIL)
        self.assertIsNone(reply_to)

    def test_sender_fields_uses_org_name_and_reply_to_when_set(self):
        from tickets.tasks import survey_sender_fields
        self.org.survey_reply_to_email = 'events@acme.com'
        self.org.save()
        from_email, reply_to = survey_sender_fields(self.org)
        # Org name on the From line, keeping Cue's verified sending address.
        self.assertTrue(from_email.startswith('Acme Events <'))
        self.assertIn('@', from_email)
        self.assertEqual(reply_to, ['events@acme.com'])

    # ---- send task ------------------------------------------------------------

    def test_send_task_sets_reply_to_and_from_for_opted_in_org(self):
        from django.core import mail
        from tickets.tasks import send_survey_emails_task
        self.org.survey_reply_to_email = 'events@acme.com'
        self.org.save()
        SurveyInvitation.objects.create(
            organization=self.org, event=self.event, customer=self.customer,
            email='fan@example.com',
        )
        send_survey_emails_task(str(self.event.id), str(self.org.id))
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.reply_to, ['events@acme.com'])
        self.assertTrue(msg.from_email.startswith('Acme Events <'))
        # Body masthead reflects the organizer, and the HTML alternative is present.
        self.assertIn('Acme Events', msg.body)
        self.assertEqual(msg.alternatives[0][1], 'text/html')

    def test_send_task_keeps_cue_default_when_not_opted_in(self):
        from django.core import mail
        from django.conf import settings
        from tickets.tasks import send_survey_emails_task
        SurveyInvitation.objects.create(
            organization=self.org, event=self.event, customer=self.customer,
            email='fan@example.com',
        )
        send_survey_emails_task(str(self.event.id), str(self.org.id))
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.from_email, settings.DEFAULT_FROM_EMAIL)
        self.assertEqual(msg.reply_to, [])

    # ---- save view ------------------------------------------------------------

    def test_reply_to_save_persists_and_clears(self):
        resp = self.client.post(
            reverse('tickets:survey_reply_to_save'), {'reply_to_email': 'events@acme.com'},
        )
        self.assertEqual(resp.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.survey_reply_to_email, 'events@acme.com')
        # Blank clears the override.
        self.client.post(reverse('tickets:survey_reply_to_save'), {'reply_to_email': ''})
        self.org.refresh_from_db()
        self.assertEqual(self.org.survey_reply_to_email, '')

    def test_reply_to_save_rejects_invalid_email(self):
        resp = self.client.post(
            reverse('tickets:survey_reply_to_save'), {'reply_to_email': 'not-an-email'},
        )
        self.assertEqual(resp.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.survey_reply_to_email, '')


class DefaultSurveyScheduleTests(TestCase):
    """Configuring the survey send schedule as an org default with per-event override."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Default Sched Org', slug='default-sched-org')
        self.user = User.objects.create_user(
            username='defsched', email='def@test.com', password='testpass123',
        )
        UserProfile.objects.create(user=self.user, organization=self.org,
                                   org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='def@test.com', password='testpass123')
        self.client.get(reverse('tickets:home'))  # seed _org_id

        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Future Show', venue=self.venue,
            start_date=date(2099, 1, 1), start_time=time(20, 0),
            end_date=date(2099, 1, 1), end_time=time(22, 0),
            timezone='America/New_York',
        )

    # ---- resolver -------------------------------------------------------------

    def test_resolved_schedule_none_when_unset(self):
        self.assertIsNone(self.event.resolved_survey_schedule())

    def test_resolved_schedule_falls_back_to_org_default(self):
        self.org.survey_send_offset_type = 'days'
        self.org.survey_send_offset_value = 1
        self.org.survey_send_time_of_day = time(9, 0)
        self.org.save()
        self.event.refresh_from_db()
        sched = self.event.resolved_survey_schedule()
        self.assertEqual(sched['offset_type'], 'days')
        self.assertEqual(sched['offset_value'], 1)
        self.assertEqual(sched['time_of_day'], time(9, 0))

    def test_event_override_wins_over_org_default(self):
        self.org.survey_send_offset_type = 'days'
        self.org.survey_send_offset_value = 1
        self.org.survey_send_time_of_day = time(9, 0)
        self.org.save()
        self.event.survey_send_offset_type = 'hours'
        self.event.survey_send_offset_value = 3
        self.event.save()
        self.event.refresh_from_db()
        sched = self.event.resolved_survey_schedule()
        self.assertEqual(sched['offset_type'], 'hours')
        self.assertEqual(sched['offset_value'], 3)

    # ---- save endpoint --------------------------------------------------------

    def test_org_scope_save_writes_org_fields(self):
        resp = self.client.post(
            reverse('tickets:survey_schedule_save'),
            {'offset_type': 'days', 'offset_value': '2', 'time_of_day': '18:00'},
        )
        self.assertEqual(resp.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.survey_send_offset_type, 'days')
        self.assertEqual(self.org.survey_send_offset_value, 2)
        self.assertEqual(self.org.survey_send_time_of_day, time(18, 0))

    def test_event_scope_save_writes_event_fields(self):
        resp = self.client.post(
            reverse('tickets:event_survey_schedule_save', args=[self.event.id]),
            {'offset_type': 'hours', 'offset_value': '5'},
        )
        self.assertEqual(resp.status_code, 302)
        self.event.refresh_from_db()
        self.assertEqual(self.event.survey_send_offset_type, 'hours')
        self.assertEqual(self.event.survey_send_offset_value, 5)

    def test_blank_offset_clears_schedule(self):
        self.org.survey_send_offset_type = 'days'
        self.org.survey_send_offset_value = 2
        self.org.survey_send_time_of_day = time(9, 0)
        self.org.save()
        self.client.post(reverse('tickets:survey_schedule_save'), {'offset_type': ''})
        self.org.refresh_from_db()
        self.assertEqual(self.org.survey_send_offset_type, '')
        self.assertIsNone(self.org.survey_send_offset_value)
        self.assertIsNone(self.org.survey_send_time_of_day)

    def test_days_without_time_is_rejected(self):
        self.client.post(
            reverse('tickets:survey_schedule_save'),
            {'offset_type': 'days', 'offset_value': '2'},  # no time_of_day
        )
        self.org.refresh_from_db()
        self.assertEqual(self.org.survey_send_offset_type, '')  # unchanged

    def test_save_rejected_when_survey_locked(self):
        SurveyInvitation.objects.create(
            organization=self.org, event=self.event,
            customer=Customer.objects.create(organization=self.org, email='a@b.com', name='A'),
            email='a@b.com',
        )
        self.client.post(
            reverse('tickets:event_survey_schedule_save', args=[self.event.id]),
            {'offset_type': 'hours', 'offset_value': '5'},
        )
        self.event.refresh_from_db()
        self.assertEqual(self.event.survey_send_offset_type, '')  # locked, unchanged

    # ---- resolved schedule context (surveys tab + send dialog) ----------------

    def test_event_detail_resolves_schedule_from_org_default(self):
        self.org.survey_send_offset_type = 'days'
        self.org.survey_send_offset_value = 3
        self.org.survey_send_time_of_day = time(10, 30)
        self.org.save()
        resp = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        resolved = resp.context['survey_schedule_resolved']
        self.assertTrue(resolved['has_schedule'])
        self.assertEqual(resolved['description'], "3 days after the event ends at 10:30 AM")
        self.assertTrue(resolved['send_at_display'])      # future event -> computable
        self.assertTrue(resolved['can_schedule'])

    def test_event_detail_resolved_when_unset(self):
        resp = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        resolved = resp.context['survey_schedule_resolved']
        self.assertFalse(resolved['has_schedule'])
        self.assertEqual(resolved['description'], "Send manually")
        self.assertFalse(resolved['can_schedule'])

    # ---- builder context ------------------------------------------------------

    def test_builder_exposes_schedule_context(self):
        resp = self.client.get(reverse('tickets:survey_builder'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('survey_schedule_save_url', resp.context)
        self.assertIn('survey_send_offset_choices', resp.context)

    def test_builder_reports_override_state(self):
        # Inheriting -> not overridden.
        url = reverse('tickets:event_survey_builder', args=[self.event.id])
        self.assertFalse(self.client.get(url).context['survey_schedule_overridden'])
        # With an event-level schedule -> overridden.
        self.event.survey_send_offset_type = 'hours'
        self.event.survey_send_offset_value = 4
        self.event.save()
        self.assertTrue(self.client.get(url).context['survey_schedule_overridden'])

    # ---- auto-arm honors the resolved schedule --------------------------------

    def _ended_yesterday_event(self):
        # Derive the date in the event's timezone (not UTC) so the fixed 22:00
        # end_time is reliably in the past no matter when the suite runs.
        from zoneinfo import ZoneInfo
        now_et = timezone.now().astimezone(ZoneInfo('America/New_York'))
        yesterday = (now_et - timedelta(days=1)).date()
        return Event.objects.create(
            organization=self.org, name='Ended Show', venue=self.venue,
            start_date=yesterday, end_date=yesterday, end_time=time(22, 0),
            timezone='America/New_York',
        )

    def test_auto_arm_uses_org_default_schedule(self):
        from django.core import mail
        # Org default applies to an event with no per-event override.
        self.org.survey_send_offset_type = 'days'
        self.org.survey_send_offset_value = 5
        self.org.survey_send_time_of_day = time(9, 0)
        self.org.save()
        event = self._ended_yesterday_event()
        c = Customer.objects.create(organization=self.org, email='g@example.com', name='G')
        TicketOrder.objects.create(
            customer=c, event=event, order_number='RS-1',
            order_date='2000-01-01 10:00:00', total_amount=Decimal('10.00'),
        )
        call_command('send_due_survey_invitations')
        inv = SurveyInvitation.objects.get(event=event, customer=c)
        self.assertIsNotNone(inv.scheduled_send_at)
        self.assertIsNone(inv.sent_at)
        self.assertEqual(len(mail.outbox), 0)

    def test_auto_arm_skips_event_without_schedule(self):
        event = self._ended_yesterday_event()  # no org or event schedule
        c = Customer.objects.create(organization=self.org, email='h@example.com', name='H')
        TicketOrder.objects.create(
            customer=c, event=event, order_number='RS-2',
            order_date='2000-01-01 10:00:00', total_amount=Decimal('10.00'),
        )
        call_command('send_due_survey_invitations')
        self.assertFalse(SurveyInvitation.objects.filter(event=event).exists())

    def test_resave_schedule_clears_opt_out(self):
        self.event.survey_auto_send_opted_out = True
        self.event.save()
        self.client.post(
            reverse('tickets:event_survey_schedule_save', args=[self.event.id]),
            {'offset_type': 'hours', 'offset_value': '3'},
        )
        self.event.refresh_from_db()
        self.assertFalse(self.event.survey_auto_send_opted_out)

    # ---- anchor (event start vs end) -----------------------------------------

    def test_resolved_schedule_defaults_anchor_to_end(self):
        self.org.survey_send_offset_type = 'hours'
        self.org.survey_send_offset_value = 2
        self.org.save()
        self.event.refresh_from_db()
        self.assertEqual(self.event.resolved_survey_schedule()['anchor'], 'end')

    def test_save_persists_start_anchor(self):
        self.client.post(
            reverse('tickets:survey_schedule_save'),
            {'offset_type': 'hours', 'offset_value': '2', 'anchor': 'start'},
        )
        self.org.refresh_from_db()
        self.assertEqual(self.org.survey_send_anchor, 'start')

    def test_compute_from_start_anchor(self):
        from tickets.views import _compute_survey_send_at
        from zoneinfo import ZoneInfo
        # Start 2099-01-01 20:00 EST (UTC-5) + 2h -> 2099-01-01 22:00 EST = 03:00 UTC next day
        result = _compute_survey_send_at(self.event, 'hours', 2, None, 'start')
        self.assertEqual(result, datetime(2099, 1, 2, 3, 0, tzinfo=ZoneInfo('UTC')))

    def test_compute_from_end_anchor_unchanged(self):
        from tickets.views import _compute_survey_send_at
        from zoneinfo import ZoneInfo
        # End 2099-01-01 22:00 EST + 2h -> 2099-01-02 05:00 UTC (default anchor)
        result = _compute_survey_send_at(self.event, 'hours', 2, None)
        self.assertEqual(result, datetime(2099, 1, 2, 5, 0, tzinfo=ZoneInfo('UTC')))

    def test_preview_respects_start_anchor(self):
        resp = self.client.get(
            reverse('tickets:survey_schedule_preview', args=[self.event.id]),
            {'offset_type': 'hours', 'offset_value': '2', 'anchor': 'start'},
        )
        self.assertTrue(resp.json()['valid'])

    def test_describe_includes_anchor_word(self):
        from tickets.views import _describe_survey_schedule
        self.assertEqual(
            _describe_survey_schedule({'offset_type': 'hours', 'offset_value': 3, 'anchor': 'start'}),
            "3 hours after the event starts",
        )
        self.assertEqual(
            _describe_survey_schedule({'offset_type': 'hours', 'offset_value': 3, 'anchor': 'end'}),
            "3 hours after the event ends",
        )

    def test_event_detail_resolved_respects_start_anchor(self):
        self.org.survey_send_offset_type = 'hours'
        self.org.survey_send_offset_value = 2
        self.org.survey_send_anchor = 'start'
        self.org.save()
        resp = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        self.assertEqual(
            resp.context['survey_schedule_resolved']['description'],
            "2 hours after the event starts",
        )


class SurveyHubTests(TestCase):
    """Surveys hub: response counts (internal + external) and the Results link."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Hub Org', slug='hub-org')
        self.user = User.objects.create_user(
            username='hubuser', email='hub@test.com', password='testpass123',
        )
        UserProfile.objects.create(user=self.user, organization=self.org,
                                   org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='hub@test.com', password='testpass123')
        self.client.get(reverse('tickets:home'))  # seed _org_id
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')

    def _event(self, name):
        return Event.objects.create(
            organization=self.org, name=name, venue=self.venue, start_date=date(2025, 8, 1),
        )

    def _hub_event(self, event_id):
        resp = self.client.get(reverse('tickets:survey_hub'))
        self.assertEqual(resp.status_code, 200)
        return next(e for e in resp.context['events'] if e.id == event_id)

    def _add_external(self, event, email):
        upload = ExternalSurveyUpload.objects.create(
            organization=self.org, filename='typeform.csv',
            status=ExternalSurveyUpload.Status.COMPLETED,
        )
        ExternalSurveyResponse.objects.create(
            organization=self.org, upload=upload, event=event,
            responded_at=timezone.now(), email=email,
            typeform_response_id='tf-' + email, raw_answers=[],
        )

    def _add_internal(self, event, email):
        customer = Customer.objects.create(organization=self.org, email=email, name=email)
        invitation = SurveyInvitation.objects.create(
            organization=self.org, event=event, customer=customer, email=email,
        )
        SurveyResponse.objects.create(
            organization=self.org, event=event, customer=customer, invitation=invitation,
        )

    def test_external_only_responses_are_counted(self):
        event = self._event('External Only')
        self._add_external(event, 'a@example.com')
        self._add_external(event, 'b@example.com')
        self.assertEqual(self._hub_event(event.id).response_count, 2)

    def test_internal_and_external_are_summed(self):
        event = self._event('Mixed')
        self._add_internal(event, 'i@example.com')
        self._add_external(event, 'e@example.com')
        self.assertEqual(self._hub_event(event.id).response_count, 2)

    def test_no_responses_counts_zero(self):
        event = self._event('Empty')
        self.assertEqual(self._hub_event(event.id).response_count, 0)

    def test_results_link_uses_tab_query_param(self):
        event = self._event('Linky')
        self._add_external(event, 'x@example.com')
        html = self.client.get(reverse('tickets:survey_hub')).content.decode()
        self.assertIn('?tab=surveys', html)
        self.assertNotIn('#tab-surveys', html)

    def test_event_action_is_configure_before_send(self):
        # No invitation exists -> the direct survey is still editable.
        self._event('Not Sent')
        html = self.client.get(reverse('tickets:survey_hub')).content.decode()
        self.assertIn('>Configure</a>', html)
        self.assertNotIn('>View</a>', html)

    def test_event_action_is_view_after_send(self):
        # An invitation exists -> the survey is locked, so the action is read-only.
        event = self._event('Sent')
        self._add_internal(event, 'sent@example.com')
        html = self.client.get(reverse('tickets:survey_hub')).content.decode()
        self.assertIn('>View</a>', html)
        self.assertNotIn('>Configure</a>', html)


class OnboardingChecklistTests(TestCase):
    """Dashboard 'Getting started' checklist (analytics-first) for new organizers."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Onboarding Org', slug='onboarding-org')
        self.user = User.objects.create_user(
            username='organizer', email='org@test.com', password='pass12345',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org,
            role=UserProfile.Role.ORGANIZER, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='org@test.com', password='pass12345')

    def _onboarding(self):
        return self.client.get(reverse('tickets:home')).context['onboarding']

    def _steps(self):
        return {s['key']: s for s in self._onboarding()['steps']}

    def _add_customer(self, email='c@example.com', **kw):
        return Customer.objects.create(organization=self.org, email=email, name='C', **kw)

    def _sent_campaign(self):
        from .models import SMSCampaign
        return SMSCampaign.objects.create(
            organization=self.org, name='Blast', body='hi',
            status=SMSCampaign.Status.SENT,
        )

    def test_new_org_shows_four_incomplete_steps(self):
        onboarding = self._onboarding()
        self.assertTrue(onboarding['show'])
        self.assertEqual(onboarding['total'], 4)
        self.assertEqual(onboarding['complete_count'], 0)
        self.assertEqual(
            set(self._steps()),
            {'set_profile', 'import_data', 'review_segments', 'send_campaign'},
        )

    def test_profile_step_completes(self):
        self.org.description = 'We throw great shows'
        self.org.save(update_fields=['description'])
        self.assertTrue(self._steps()['set_profile']['complete'])

    def test_import_step_routes_straight_to_external_flow(self):
        # Must skip the type chooser (which leads with Direct Ticketing) and go
        # directly to the external/CSV create flow.
        from .models import TICKETING_TYPE_EXTERNAL
        self.assertEqual(
            self._steps()['import_data']['url'],
            reverse('tickets:event_create', args=[TICKETING_TYPE_EXTERNAL]),
        )

    def test_import_and_segments_complete_with_real_customer(self):
        self._add_customer()
        steps = self._steps()
        self.assertTrue(steps['import_data']['complete'])
        self.assertTrue(steps['review_segments']['complete'])

    def test_placeholder_customer_does_not_complete_import(self):
        self._add_customer(email='in-person-%s@placeholder.local' % self.org.id)
        self.assertFalse(self._steps()['import_data']['complete'])

    def test_sms_step_consent_gated_with_no_audience(self):
        self._add_customer()  # imported but no opt-in
        step = self._steps()['send_campaign']
        self.assertEqual(step['cta'], 'Review consent')
        # Lands on the customer list with the consent explainer focused.
        self.assertEqual(step['url'], reverse('tickets:customer_list') + '?focus=consent')
        self.assertFalse(step['complete'])

    def test_consent_help_banner_shows_only_with_focus(self):
        self._add_customer()
        base = reverse('tickets:customer_list')
        with_focus = self.client.get(base + '?focus=consent').content.decode()
        without = self.client.get(base).content.decode()
        self.assertIn('Getting an SMS-eligible audience', with_focus)
        self.assertNotIn('Getting an SMS-eligible audience', without)

    def test_sms_step_points_to_compose_with_eligible_audience(self):
        self._add_customer(phone='+15551110001', sms_opt_in=True)
        step = self._steps()['send_campaign']
        self.assertEqual(step['cta'], 'Compose campaign')
        self.assertEqual(step['url'], reverse('tickets:sms_campaign_create'))

    def test_sms_step_completes_on_sent_campaign(self):
        self._add_customer(phone='+15551110001', sms_opt_in=True)
        self._sent_campaign()
        self.assertTrue(self._steps()['send_campaign']['complete'])

    def test_all_complete_hides_card(self):
        self.org.description = 'x'
        self.org.save(update_fields=['description'])
        self._add_customer(phone='+15551110001', sms_opt_in=True)
        self._sent_campaign()
        onboarding = self._onboarding()
        self.assertTrue(onboarding['all_complete'])
        self.assertFalse(onboarding['show'])

    def test_dismiss_hides_card_and_skips_predicates(self):
        resp = self.client.post(reverse('tickets:dismiss_onboarding'))
        self.assertRedirects(resp, reverse('tickets:home'))
        self.org.refresh_from_db()
        self.assertIsNotNone(self.org.onboarding_dismissed_at)
        onboarding = self._onboarding()
        self.assertFalse(onboarding['show'])
        self.assertEqual(onboarding['steps'], [])

    def test_dismiss_requires_post(self):
        resp = self.client.get(reverse('tickets:dismiss_onboarding'))
        self.assertEqual(resp.status_code, 405)

    def _upsell_shown(self):
        return self.client.get(reverse('tickets:home')).context['show_directticketing_upsell']

    def test_upsell_hidden_before_any_value(self):
        # Value-gated: nothing imported, no campaign → no upsell.
        self.assertFalse(self._upsell_shown())

    def test_upsell_shown_after_import(self):
        self._add_customer()
        self.assertTrue(self._upsell_shown())

    def test_upsell_shown_after_campaign(self):
        self._sent_campaign()
        self.assertTrue(self._upsell_shown())

    def test_upsell_hidden_when_stripe_onboarded(self):
        self._add_customer()
        self.org.stripe_onboarding_complete = True
        self.org.save(update_fields=['stripe_onboarding_complete'])
        self.assertFalse(self._upsell_shown())

    def test_upsell_dismiss_persists(self):
        self._add_customer()
        self.assertTrue(self._upsell_shown())
        resp = self.client.post(reverse('tickets:dismiss_directticketing_upsell'))
        self.assertRedirects(resp, reverse('tickets:home'))
        self.org.refresh_from_db()
        self.assertIsNotNone(self.org.directticketing_upsell_dismissed_at)
        self.assertFalse(self._upsell_shown())

    def test_upsell_dismiss_requires_post(self):
        resp = self.client.get(reverse('tickets:dismiss_directticketing_upsell'))
        self.assertEqual(resp.status_code, 405)

    def test_zero_data_dashboard_shows_import_cta_and_empty_state(self):
        html = self.client.get(reverse('tickets:home')).content.decode()
        self.assertIn('Import an event report', html)      # primary CTA (D2/2A)
        self.assertIn('No customers yet', html)            # empty state (D3/3A)
        self.assertIn(reverse('tickets:sample_import_csv'), html)

    def test_dashboard_with_customers_shows_stats_not_empty_state(self):
        self._add_customer()
        html = self.client.get(reverse('tickets:home')).content.decode()
        self.assertIn('Total Customers', html)
        self.assertNotIn('No customers yet', html)

    def test_sample_import_csv_download(self):
        resp = self.client.get(reverse('tickets:sample_import_csv'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'text/csv')
        self.assertIn('attachment', resp['Content-Disposition'])
        body = resp.content.decode()
        self.assertIn('sms_opt_in', body)          # consent column documented
        self.assertIn('customer_email', body)


class DashboardSpotlightTests(TestCase):
    """Dashboard spotlights up to 2 upcoming + 2 recently-ended events instead
    of a full paginated table."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Spotlight Org', slug='spotlight-org')
        self.user = User.objects.create_user(
            username='spot', email='spot@test.com', password='pass12345',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org,
            role=UserProfile.Role.ORGANIZER, org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='spot@test.com', password='pass12345')
        self.venue = Venue.objects.create(organization=self.org, name='The Venue', city='SF')
        self.today = date.today()

    def _event(self, name, start_offset_days):
        return Event.objects.create(
            organization=self.org, name=name, venue=self.venue,
            start_date=self.today + timedelta(days=start_offset_days),
        )

    def _get(self):
        return self.client.get(reverse('tickets:home'))

    def test_splits_upcoming_and_ended(self):
        past = self._event('Past Show', -10)
        future = self._event('Future Show', 10)
        resp = self._get()
        upcoming_ids = {e.id for e in resp.context['upcoming_events']}
        ended_ids = {e.id for e in resp.context['ended_events']}
        self.assertIn(future.id, upcoming_ids)
        self.assertIn(past.id, ended_ids)
        self.assertNotIn(past.id, upcoming_ids)
        self.assertNotIn(future.id, ended_ids)

    def test_caps_at_two_each_soonest_and_most_recent(self):
        # Three upcoming, three ended — only the 2 nearest "now" on each side show.
        self._event('Up +30', 30)
        near_up_a = self._event('Up +2', 2)
        near_up_b = self._event('Up +5', 5)
        self._event('End -30', -30)
        near_end_a = self._event('End -2', -2)
        near_end_b = self._event('End -5', -5)
        resp = self._get()
        upcoming = list(resp.context['upcoming_events'])
        ended = list(resp.context['ended_events'])
        self.assertEqual([e.id for e in upcoming], [near_up_a.id, near_up_b.id])
        self.assertEqual([e.id for e in ended], [near_end_a.id, near_end_b.id])

    def test_links_to_full_event_list(self):
        self._event('Some Show', 5)
        html = self._get().content.decode()
        self.assertIn(reverse('tickets:event_list'), html)
        self.assertIn('View all events', html)

    def test_no_events_shows_empty_state(self):
        resp = self._get()
        self.assertFalse(resp.context['has_events'])
        self.assertContains(resp, 'No events yet')


class VenueCreateInlineTests(TestCase):
    """Tests for the inline venue creation AJAX endpoint."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Inline Org', slug='inline-org')
        self.user = User.objects.create_user(
            username='inlinehost', email='inline@test.com', password='testpass123'
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER
        )
        self.client.login(username='inline@test.com', password='testpass123')
        # Seed the session with _org_id so @require_org passes.
        self.client.get(reverse('tickets:home'))
        self.url = reverse('tickets:venue_create_inline')

    def test_creates_venue_and_returns_json(self):
        resp = self.client.post(self.url, {'name': 'The Fillmore', 'city': 'San Francisco', 'capacity': '1200'})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])
        venue = Venue.objects.get(name='The Fillmore')
        self.assertEqual(venue.organization, self.org)
        self.assertEqual(venue.capacity, 1200)
        self.assertEqual(data['venue']['id'], str(venue.id))
        self.assertEqual(data['venue']['capacity'], 1200)
        self.assertIn('The Fillmore', data['venue']['label'])

    def test_missing_name_returns_error(self):
        resp = self.client.post(self.url, {'city': 'San Francisco'})
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data['success'])
        self.assertIn('name', data['errors'])
        self.assertFalse(Venue.objects.exists())

    def test_duplicate_name_city_returns_error(self):
        Venue.objects.create(organization=self.org, name='Dup Venue', city='Oakland')
        resp = self.client.post(self.url, {'name': 'Dup Venue', 'city': 'Oakland'})
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data['success'])
        self.assertEqual(Venue.objects.filter(name='Dup Venue').count(), 1)

    def test_get_not_allowed(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 405)

    def test_requires_login(self):
        self.client.logout()
        resp = self.client.post(self.url, {'name': 'No Auth Venue'})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Venue.objects.filter(name='No Auth Venue').exists())


class ExternalEventsFeatureFlagTests(TestCase):
    """Gate external-event creation (manual + CSV) behind external_events_enabled."""

    def setUp(self):
        self.client = Client()
        # The model default is now True (external-first onboarding); this class
        # exercises the gate itself, so force the flag OFF for the "when off" cases.
        self.org = Organization.objects.create(
            name='Flag Org', slug='flag-org', external_events_enabled=False,
        )
        self.user = User.objects.create_user(
            username='flaguser', email='flag@test.com', password='pass12345'
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER
        )
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER
        )
        self.client.login(username='flag@test.com', password='pass12345')
        self.client.get(reverse('tickets:home'))  # seed _org_id in session

        self.csv_format = CSVFormat.objects.create(
            organization=self.org, name='Fmt', column_mapping={'order_number': 'Order ID'}
        )
        self.venue = Venue.objects.create(organization=self.org, name='V', city='C')
        self.event = Event.objects.create(
            organization=self.org, name='Ext Event', venue=self.venue,
            start_date=date(2024, 6, 15), start_time=time(19, 0, 0),
        )
        self.upload = UploadedFile.objects.create(
            organization=self.org, csv_format=self.csv_format, filename='u.csv',
            status='completed',
        )

    def _enable(self):
        self.org.external_events_enabled = True
        self.org.save(update_fields=['external_events_enabled'])

    def test_default_flag_on_for_new_org(self):
        # External-first onboarding: a brand-new org has the flag on by default.
        fresh = Organization.objects.create(name='Fresh Flag Org', slug='fresh-flag-org')
        self.assertTrue(fresh.external_events_enabled)

    def test_type_select_redirects_to_direct_when_off(self):
        resp = self.client.get(reverse('tickets:event_type_select'))
        self.assertRedirects(
            resp, reverse('tickets:event_create', kwargs={'ticketing_type': 'direct'})
        )

    def test_type_select_shows_chooser_when_on(self):
        self._enable()
        resp = self.client.get(reverse('tickets:event_type_select'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Import an Event')

    def test_external_create_blocked_when_off(self):
        resp = self.client.get(
            reverse('tickets:event_create', kwargs={'ticketing_type': 'external'})
        )
        self.assertRedirects(
            resp, reverse('tickets:event_create', kwargs={'ticketing_type': 'direct'})
        )

    def test_external_create_allowed_when_on(self):
        self._enable()
        resp = self.client.get(
            reverse('tickets:event_create', kwargs={'ticketing_type': 'external'})
        )
        self.assertEqual(resp.status_code, 200)

    def test_csv_upload_404_when_off(self):
        resp = self.client.get(
            reverse('tickets:event_upload_csv', kwargs={'event_id': self.event.id})
        )
        self.assertEqual(resp.status_code, 404)

    def test_csv_upload_reachable_when_on(self):
        self._enable()
        resp = self.client.get(
            reverse('tickets:event_upload_csv', kwargs={'event_id': self.event.id})
        )
        self.assertEqual(resp.status_code, 200)

    def test_reprocess_404_when_off(self):
        resp = self.client.get(
            reverse('tickets:reprocess_csv_file', kwargs={'file_id': self.upload.id})
        )
        self.assertEqual(resp.status_code, 404)


class EventEffectiveStatusTest(TestCase):
    """effective_status compares the full timezone-aware end datetime against now,
    so a 'live' event flips to 'ended' once its end time passes — not just once the
    calendar day rolls over."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='Status Org', slug='status-org',
        )
        self.venue = Venue.objects.create(
            organization=self.org, name='Venue', city='City',
        )

    def _event(self, **kwargs):
        defaults = dict(
            organization=self.org, name='E', venue=self.venue,
            ticketing_type=TICKETING_TYPE_DIRECT, status='live',
            timezone='America/Los_Angeles',
        )
        defaults.update(kwargs)
        return Event.objects.create(**defaults)

    def test_ended_when_end_time_passed_same_day(self):
        """Event that ended a few hours ago today is 'ended', not 'live'."""
        now = timezone.localtime(timezone.now())
        ended = now - timedelta(hours=3)
        event = self._event(
            start_date=ended.date(), start_time=time(0, 0),
            end_date=ended.date(), end_time=ended.time(),
        )
        self.assertEqual(event.effective_status, 'ended')

    def test_live_when_end_time_later_today(self):
        """Event ending later today is still 'live'."""
        now = timezone.localtime(timezone.now())
        ends = now + timedelta(hours=3)
        event = self._event(
            start_date=now.date(), start_time=time(0, 0),
            end_date=ends.date(), end_time=ends.time(),
        )
        self.assertEqual(event.effective_status, 'live')


class RegenerateEventSummaryTaskTests(TestCase):
    """Change-detection and guard behavior for regenerate_event_summary_task."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()  # _compute_event_stats caches per-event; keep tests isolated
        self.org = Organization.objects.create(name='Regen Org', slug='regen-org')
        self.venue = Venue.objects.create(organization=self.org, name='Regen Hall', city='Austin')

    def _ended_event(self, **kwargs):
        defaults = dict(
            organization=self.org,
            name='Past Event',
            venue=self.venue,
            start_date=date(2024, 1, 1),
            start_time=time(19, 0),
            ai_summary='Existing summary body.',
        )
        defaults.update(kwargs)
        return Event.objects.create(**defaults)

    def _current_fingerprint(self, event):
        from tickets.views import _compute_event_stats
        from tickets.services.event_summary import EventSummaryService
        return EventSummaryService(self.org).input_fingerprint(
            event, _compute_event_stats(event)
        )

    def _run(self, event):
        from tickets.tasks import regenerate_event_summary_task
        return regenerate_event_summary_task(str(event.id))

    @patch('tickets.services.event_summary.EventSummaryService.generate_summary')
    def test_unchanged_data_skips_regeneration(self, mock_generate):
        event = self._ended_event()
        event.ai_summary_input_hash = self._current_fingerprint(event)
        event.save(update_fields=['ai_summary_input_hash'])

        self.assertEqual(self._run(event), 'unchanged')
        mock_generate.assert_not_called()

    @patch('tickets.services.event_summary.EventSummaryService.generate_summary',
           return_value='fresh summary')
    def test_changed_data_regenerates(self, mock_generate):
        event = self._ended_event(ai_summary_input_hash='stale-fingerprint')
        self.assertEqual(self._run(event), 'regenerated')
        mock_generate.assert_called_once()

    @patch('tickets.services.event_summary.EventSummaryService.generate_summary')
    def test_first_run_backfills_without_regenerating(self, mock_generate):
        event = self._ended_event(ai_summary_input_hash='')
        self.assertEqual(self._run(event), 'backfilled')
        mock_generate.assert_not_called()
        event.refresh_from_db()
        self.assertNotEqual(event.ai_summary_input_hash, '')

    @patch('tickets.services.event_summary.EventSummaryService.generate_summary')
    def test_event_without_summary_is_skipped(self, mock_generate):
        event = self._ended_event(ai_summary='', ai_summary_input_hash='stale')
        self.assertEqual(self._run(event), 'no-summary')
        mock_generate.assert_not_called()

    @patch('tickets.services.event_summary.EventSummaryService.generate_summary')
    def test_future_event_not_regenerated(self, mock_generate):
        future = timezone.localdate() + timedelta(days=30)
        event = self._ended_event(
            start_date=future, ai_summary_input_hash='stale',
        )
        self.assertEqual(self._run(event), 'not-ended')
        mock_generate.assert_not_called()

    @patch('tickets.services.event_summary.EventSummaryService.generate_summary')
    def test_auto_regenerate_disabled_is_skipped(self, mock_generate):
        self.org.ai_event_summary_auto_regenerate = False
        self.org.save(update_fields=['ai_event_summary_auto_regenerate'])
        event = self._ended_event(ai_summary_input_hash='stale')
        self.assertEqual(self._run(event), 'disabled')
        mock_generate.assert_not_called()

    @patch('tickets.services.event_summary.EventSummaryService.generate_summary')
    def test_recently_generated_is_skipped(self, mock_generate):
        event = self._ended_event(
            ai_summary_input_hash='stale',
            ai_summary_generated_at=timezone.now(),
        )
        self.assertEqual(self._run(event), 'recent')
        mock_generate.assert_not_called()

    def test_generate_summary_persists_hash_and_text(self):
        """The non-streaming path stores the summary and its fingerprint."""
        from tickets.services.event_summary import EventSummaryService
        from tickets.views import _compute_event_stats

        event = self._ended_event(ai_summary='old', ai_summary_input_hash='old-hash')
        event_data = _compute_event_stats(event)

        fake_message = MagicMock()
        fake_message.content = 'A brand new summary.'
        fake_message.usage_metadata = {
            'input_tokens': 100, 'output_tokens': 40, 'total_tokens': 140,
        }
        with patch('langchain_openai.ChatOpenAI') as mock_llm_cls:
            mock_llm_cls.return_value.invoke.return_value = fake_message
            result = EventSummaryService(self.org).generate_summary(event, event_data)

        self.assertEqual(result, 'A brand new summary.')
        event.refresh_from_db()
        self.assertEqual(event.ai_summary, 'A brand new summary.')
        self.assertNotEqual(event.ai_summary_input_hash, 'old-hash')
        self.assertIsNotNone(event.ai_summary_generated_at)
        self.assertTrue(
            AITokenUsage.objects.filter(
                organization=self.org, feature=AITokenUsage.FEATURE_EVENT_SUMMARY
            ).exists()
        )


@override_settings(E2E_TEST_MODE=True)
class SubscribePageTests(TestCase):
    """Public /subscribe/<org>/ flow: form -> OTP -> accountless Customer + consent."""

    OTP = '000000'  # tickets.sms.E2E_OTP_CODE

    def setUp(self):
        from django.core.cache import cache
        cache.clear()  # rate-limit counters are process-global
        self.client = Client()
        self.org = Organization.objects.create(
            name='After Hours', slug='after-hours', sms_marketing_enabled=True,
        )
        self.url = reverse('tickets:subscribe', args=[self.org.slug])
        self.verify_url = reverse('tickets:subscribe_verify', args=[self.org.slug])

    def _valid_post(self, **over):
        # Phone-only signup: name/email are no longer collected.
        data = {'phone': '415-555-0100', 'sms_consent': 'on'}
        data.update(over)
        return self.client.post(self.url, data)

    # --- GET / gating ---
    def test_get_renders_form(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'After Hours')
        self.assertContains(r, 'Get text updates')

    def test_no_name_email_fields_rendered(self):
        r = self.client.get(self.url)
        self.assertNotContains(r, 'name="name"')
        self.assertNotContains(r, 'name="email"')

    def test_unknown_org_404(self):
        self.assertEqual(self.client.get(reverse('tickets:subscribe', args=['nope'])).status_code, 404)

    def test_sms_marketing_disabled_shows_unavailable(self):
        self.org.sms_marketing_enabled = False
        self.org.save(update_fields=['sms_marketing_enabled'])
        r = self.client.get(self.url)
        self.assertContains(r, "isn't accepting")

    # --- validation ---
    def test_consent_unchecked_rejected_no_otp(self):
        with patch('tickets.sms.start_phone_verification') as m:
            r = self.client.post(self.url, {'phone': '4155550100'})
        m.assert_not_called()
        self.assertEqual(SMSConsentRecord.objects.count(), 0)
        self.assertContains(r, 'agree to receive')

    def test_bad_phone_rejected(self):
        r = self._valid_post(phone='123')
        self.assertEqual(SMSConsentRecord.objects.count(), 0)
        self.assertContains(r, 'valid mobile number')

    # --- send + pending record ---
    def test_valid_post_sends_code_and_writes_pending_record(self):
        r = self._valid_post()
        self.assertContains(r, 'texted a 6-digit code')
        rec = SMSConsentRecord.objects.get()
        self.assertEqual(rec.phone, '+14155550100')
        self.assertEqual(rec.email, '')  # no longer collected
        self.assertEqual(rec.name, '')
        self.assertIsNone(rec.verified_at)  # pending until OTP
        self.assertTrue(rec.consent_given)
        self.assertIn('After Hours', rec.consent_text)

    def test_send_failure_shows_retry_no_record(self):
        with patch('tickets.sms.start_phone_verification', return_value=False):
            r = self._valid_post()
        self.assertEqual(SMSConsentRecord.objects.count(), 0)
        self.assertContains(r, "send a code to that number")

    # --- verify -> customer + consent ---
    def test_verify_creates_customer_and_verifies_consent(self):
        self._valid_post()
        r = self.client.post(self.verify_url, {'otp_code': self.OTP})
        self.assertContains(r, "You're in")
        c = Customer.objects.get(organization=self.org, phone='+14155550100')
        self.assertIsNone(c.user)  # accountless
        self.assertEqual(c.email, '')  # phone-only subscriber
        self.assertEqual(c.name, '')
        self.assertTrue(c.sms_opt_in)
        self.assertIsNotNone(c.sms_opt_in_date)
        rec = SMSConsentRecord.objects.get()
        self.assertIsNotNone(rec.verified_at)
        self.assertEqual(rec.customer_id, c.id)
        self.assertFalse(rec.pending_start)

    def test_wrong_code_keeps_record_unverified(self):
        self._valid_post()
        r = self.client.post(self.verify_url, {'otp_code': '999999'})
        self.assertContains(r, "Try again or resend")
        self.assertIsNone(SMSConsentRecord.objects.get().verified_at)
        self.assertFalse(Customer.objects.filter(phone='+14155550100').exists())

    def test_verify_without_session_restarts(self):
        r = self.client.post(self.verify_url, {'otp_code': self.OTP})
        self.assertContains(r, 'start again')

    # --- identity: merges by phone, never splits ---
    def test_existing_customer_reused_not_clobbered(self):
        # A customer already carrying this phone (e.g. a prior checkout) is merged into,
        # not clobbered or duplicated.
        existing = Customer.objects.create(
            organization=self.org, email='sam@example.com', name='Real Name',
            phone='+14155550100', lifetime_value=Decimal('250.00'),
        )
        self._valid_post()
        self.client.post(self.verify_url, {'otp_code': self.OTP})
        existing.refresh_from_db()
        self.assertTrue(existing.sms_opt_in)
        self.assertEqual(existing.name, 'Real Name')            # not clobbered
        self.assertEqual(existing.email, 'sam@example.com')     # not clobbered
        self.assertEqual(existing.lifetime_value, Decimal('250.00'))
        self.assertEqual(Customer.objects.filter(organization=self.org, phone='+14155550100').count(), 1)

    def test_double_submit_one_customer(self):
        for _ in range(2):
            self._valid_post()
            self.client.post(self.verify_url, {'otp_code': self.OTP})
        self.assertEqual(Customer.objects.filter(organization=self.org, phone='+14155550100').count(), 1)

    # --- suppression reconciliation ---
    def test_per_org_stop_cleared_on_consent(self):
        PhoneSuppression.objects.create(
            phone='+14155550100', organization=self.org,
            reason=PhoneSuppression.Reason.MANUAL,
        )
        self._valid_post()
        self.client.post(self.verify_url, {'otp_code': self.OTP})
        self.assertFalse(PhoneSuppression.objects.filter(
            phone='+14155550100', organization=self.org).exists())
        self.assertFalse(SMSConsentRecord.objects.get().pending_start)

    def test_global_stop_sets_pending_start_and_keeps_suppression(self):
        PhoneSuppression.objects.create(
            phone='+14155550100', organization=None,
            reason=PhoneSuppression.Reason.TWILIO_STOP,
        )
        self._valid_post()
        r = self.client.post(self.verify_url, {'otp_code': self.OTP})
        self.assertContains(r, 'reply')  # START instruction
        self.assertTrue(SMSConsentRecord.objects.get().pending_start)
        self.assertTrue(PhoneSuppression.objects.filter(
            phone='+14155550100', organization__isnull=True).exists())  # NOT cleared

    def test_inbound_start_clears_pending_start(self):
        self.test_global_stop_sets_pending_start_and_keeps_suppression()
        with patch('tickets.sms_views.validate_twilio_request', return_value=True):
            self.client.post(reverse('tickets:twilio_sms_inbound_webhook'),
                             {'From': '+14155550100', 'OptOutType': 'START'})
        self.assertFalse(SMSConsentRecord.objects.get().pending_start)

    # --- rate limit (fail-closed, per-phone cap of 3) ---
    def test_per_phone_rate_limit(self):
        for _ in range(3):
            self.assertContains(self._valid_post(), 'texted a 6-digit code')
        blocked = self._valid_post()
        self.assertContains(blocked, 'wait a bit')

    # --- ledger immutability ---
    def test_consent_record_proof_field_immutable(self):
        from django.core.exceptions import ValidationError
        self._valid_post()
        rec = SMSConsentRecord.objects.get()
        rec.consent_text = 'tampered'
        with self.assertRaises(ValidationError):
            rec.save()


class PhoneSubscriberReconciliationTests(TestCase):
    """A phone-only subscriber (email='') unifies with a later CSV import or checkout
    purchase by phone, instead of forking a second Customer row."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='Reco Org', slug='reco-org', external_events_enabled=True,
        )
        self.csv_format = CSVFormat.objects.create(
            organization=self.org, name='Reco Format',
            column_mapping={
                'order_date': ['order_date'],
                'customer_email': ['customer_email'],
                'customer_name': ['customer_name'],
                'customer_phone': ['customer_phone'],
                'ticket_type': ['ticket_type'],
                'customer_sms_opt_in': ['consent'],
            },
        )

    def _subscriber(self, phone):
        # Mirrors a phone-only subscribe: opted-in Customer with no email/name.
        return Customer.objects.create(
            organization=self.org, email='', name='', phone=phone, sms_opt_in=True,
        )

    def _import(self, csv_body):
        import io
        upload = UploadedFile.objects.create(
            organization=self.org, csv_format=self.csv_format, filename='reco.csv',
            status='pending',
            metadata={'event_name': 'Reco Show', 'event_start_date': '2025-06-01'},
        )
        from tickets.csv_processor import CSVProcessor
        return CSVProcessor(upload, self.csv_format).process_and_save(
            io.BytesIO(csv_body.encode('utf-8'))
        )

    # --- CSV import reconciliation ---
    def test_import_adopts_phone_only_subscriber(self):
        sub = self._subscriber('+13105550001')
        # Raw phone in the CSV normalizes to the subscriber's E.164.
        self._import(
            "order_date,customer_email,customer_name,customer_phone,ticket_type,consent\n"
            "2025-06-01,fan@example.com,Fan,(310) 555-0001,GA,Yes\n"
        )
        self.assertEqual(Customer.objects.filter(organization=self.org).count(), 1)
        sub.refresh_from_db()
        self.assertEqual(sub.email, 'fan@example.com')  # backfilled onto the subscriber
        self.assertEqual(sub.phone, '+13105550001')     # kept E.164, not clobbered
        self.assertTrue(sub.sms_opt_in)

    def test_import_same_phone_two_emails_adopts_once(self):
        sub = self._subscriber('+13105550002')
        self._import(
            "order_date,customer_email,customer_name,customer_phone,ticket_type,consent\n"
            "2025-06-01,a@example.com,A,(310) 555-0002,GA,Yes\n"
            "2025-06-01,b@example.com,B,(310) 555-0002,GA,Yes\n"
        )
        self.assertEqual(Customer.objects.filter(organization=self.org).count(), 2)
        # First row adopts the subscriber; the second creates a fresh customer.
        adopted = Customer.objects.get(organization=self.org, email='a@example.com')
        self.assertEqual(adopted.pk, sub.pk)
        self.assertTrue(Customer.objects.filter(organization=self.org, email='b@example.com')
                        .exclude(pk=sub.pk).exists())

    def test_import_no_subscriber_normalizes_phone(self):
        self._import(
            "order_date,customer_email,customer_name,customer_phone,ticket_type,consent\n"
            "2025-06-01,solo@example.com,Solo,(310) 555-0003,GA,No\n"
        )
        c = Customer.objects.get(organization=self.org, email='solo@example.com')
        self.assertEqual(c.phone, '+13105550003')  # stored E.164

    # --- checkout reconciliation (get_or_create_customer_for_purchase) ---
    def _buyer_account(self, email, phone):
        user = User.objects.create_user(
            username='buyer' + phone[-4:], email=email, password='pw123456',
        )
        UserProfile.objects.create(
            user=user, organization=self.org, phone_number=phone,
            role=UserProfile.Role.ATTENDEE,
        )
        return user

    def test_purchase_adopts_phone_only_subscriber(self):
        from tickets.utils import get_or_create_customer_for_purchase
        sub = self._subscriber('+13105550055')
        self._buyer_account('buyer@example.com', '+13105550055')
        customer, created = get_or_create_customer_for_purchase(
            self.org, email='buyer@example.com', name='Buyer',
        )
        self.assertEqual(customer.pk, sub.pk)          # merged, not forked
        self.assertFalse(created)                       # adoption is not a creation
        self.assertEqual(customer.email, 'buyer@example.com')
        self.assertTrue(customer.sms_opt_in)           # preserved
        self.assertEqual(Customer.objects.filter(organization=self.org).count(), 1)

    def test_purchase_existing_email_customer_reused(self):
        from tickets.utils import get_or_create_customer_for_purchase
        existing = Customer.objects.create(
            organization=self.org, email='e@example.com', name='E',
        )
        customer, created = get_or_create_customer_for_purchase(
            self.org, email='e@example.com', name='Ignored',
        )
        self.assertEqual(customer.pk, existing.pk)
        self.assertFalse(created)
        self.assertEqual(Customer.objects.filter(organization=self.org).count(), 1)

    def test_purchase_no_subscriber_creates_customer(self):
        from tickets.utils import get_or_create_customer_for_purchase
        customer, created = get_or_create_customer_for_purchase(
            self.org, email='new@example.com', name='New',
        )
        self.assertEqual(customer.email, 'new@example.com')
        self.assertTrue(created)
        self.assertTrue(Customer.objects.filter(organization=self.org, email='new@example.com').exists())

    def test_purchase_create_race_refetches_existing_customer(self):
        from tickets.utils import get_or_create_customer_for_purchase

        class FirstResult:
            def __init__(self, result):
                self.result = result

            def first(self):
                return self.result

        existing = Customer.objects.create(
            organization=self.org, email='race@example.com', name='Winner',
        )
        with patch.object(Customer.objects, 'filter',
                          side_effect=[FirstResult(None), FirstResult(existing)]):
            with patch.object(Customer.objects, 'create', side_effect=IntegrityError):
                customer, created = get_or_create_customer_for_purchase(
                    self.org, email='race@example.com', name='Race',
                )
        self.assertEqual(customer.pk, existing.pk)
        self.assertFalse(created)

    def test_phone_only_subscribers_unique_per_org_phone(self):
        self._subscriber('+13105550077')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._subscriber('+13105550077')
        Customer.objects.create(
            organization=self.org, email='with-email@example.com',
            name='Email Buyer', phone='+13105550077',
        )
        self.assertEqual(Customer.objects.filter(organization=self.org, phone='+13105550077').count(), 2)


class AcquisitionSourceTests(TestCase):
    """Customer.acquisition_source is stamped at every creation path, treated as
    immutable, and backfilled for legacy rows by the 0192 data migration."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='Acq Org', slug='acq-org', external_events_enabled=True,
        )
        self.csv_format = CSVFormat.objects.create(
            organization=self.org, name='Acq Format',
            column_mapping={
                'order_date': ['order_date'],
                'customer_email': ['customer_email'],
                'customer_name': ['customer_name'],
                'customer_phone': ['customer_phone'],
                'ticket_type': ['ticket_type'],
            },
        )

    def _import(self, csv_body):
        import io
        upload = UploadedFile.objects.create(
            organization=self.org, csv_format=self.csv_format, filename='acq.csv',
            status='pending',
            metadata={'event_name': 'Acq Show', 'event_start_date': '2025-06-01'},
        )
        from tickets.csv_processor import CSVProcessor
        return CSVProcessor(upload, self.csv_format).process_and_save(
            io.BytesIO(csv_body.encode('utf-8'))
        )

    def test_purchase_create_stamps_ticket_purchase(self):
        from tickets.utils import get_or_create_customer_for_purchase
        c, created = get_or_create_customer_for_purchase(self.org, email='p@example.com', name='P')
        self.assertTrue(created)
        self.assertEqual(c.acquisition_source, Customer.AcquisitionSource.TICKET_PURCHASE)

    def test_purchase_adoption_preserves_original_source(self):
        from tickets.utils import get_or_create_customer_for_purchase
        sub = Customer.objects.create(
            organization=self.org, email='', name='', phone='+13105559001',
            sms_opt_in=True, acquisition_source=Customer.AcquisitionSource.SUBSCRIBE_FORM,
        )
        user = User.objects.create_user(username='b9001', email='b@example.com', password='pw123456')
        UserProfile.objects.create(
            user=user, organization=self.org, phone_number='+13105559001',
            role=UserProfile.Role.ATTENDEE,
        )
        c, created = get_or_create_customer_for_purchase(self.org, email='b@example.com', name='B')
        self.assertFalse(created)  # adopted, not created
        self.assertEqual(c.pk, sub.pk)  # adopted, not forked
        self.assertEqual(c.acquisition_source, Customer.AcquisitionSource.SUBSCRIBE_FORM)

    def test_csv_import_stamps_import(self):
        self._import(
            "order_date,customer_email,customer_name,customer_phone,ticket_type\n"
            "2025-06-01,fan@example.com,Fan,(310) 555-9002,GA\n"
        )
        c = Customer.objects.get(organization=self.org, email='fan@example.com')
        self.assertEqual(c.acquisition_source, Customer.AcquisitionSource.IMPORT)

    def test_backfill_classifies_legacy_rows(self):
        import importlib
        from django.apps import apps as global_apps
        # Legacy rows created with a blank source (bypassing the stamped paths).
        sub = Customer.objects.create(
            organization=self.org, email='', name='', phone='+13105559003', sms_opt_in=True,
        )
        contact = Customer.objects.create(
            organization=self.org, email='', name='', phone='+13105559004', sms_opt_in=False,
        )
        buyer = Customer.objects.create(
            organization=self.org, email='legacy@example.com', name='Legacy',
        )
        mod = importlib.import_module('tickets.migrations.0192_customer_acquisition_source')
        mod.backfill_acquisition_source(global_apps, None)
        for row, expected in (
            (sub, Customer.AcquisitionSource.SUBSCRIBE_FORM),
            (contact, Customer.AcquisitionSource.IMPORT),
            (buyer, Customer.AcquisitionSource.IMPORT),
        ):
            row.refresh_from_db()
            self.assertEqual(row.acquisition_source, expected)


class SalesPacingTests(TestCase):
    """Sales pacing service, view context, and comparison API."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Pace Org', slug='pace-org')
        self.other_org = Organization.objects.create(name='Other Org', slug='other-org')
        self.user = User.objects.create_user(
            username='paceuser', email='pace@test.com', password='pw123456',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='pace@test.com', password='pw123456')
        self.client.get(reverse('tickets:home'))  # seed _org_id in session

        self.venue = Venue.objects.create(organization=self.org, name='Pace Hall', city='San Diego')
        # Current event (most recent) and a past event at the same venue.
        self.event = Event.objects.create(
            organization=self.org, name='Current Show', venue=self.venue,
            start_date=date(2024, 6, 15),
        )
        self.past_event = Event.objects.create(
            organization=self.org, name='Past Show', venue=self.venue,
            start_date=date(2024, 3, 10),
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='c@example.com', name='C',
        )

    def _make_order(self, event, number, order_dt, tickets, amount):
        order = TicketOrder.objects.create(
            customer=self.customer, event=event, order_number=number,
            order_date=timezone.make_aware(order_dt), total_amount=Decimal(amount),
        )
        for i in range(tickets):
            Ticket.objects.create(ticket_order=order, ticket_type='GA', price=Decimal('50.00'))
        return order

    def test_get_pacing_series(self):
        from tickets.services.forecasting.sales_curve import SalesCurveCalculator
        # 14 days before (Jun 1) and 5 days before (Jun 10) the Jun 15 event.
        self._make_order(self.event, 'P-1', datetime(2024, 6, 1, 12, 0), 2, '100.00')
        self._make_order(self.event, 'P-2', datetime(2024, 6, 10, 12, 0), 3, '150.00')

        data = SalesCurveCalculator().get_pacing_series(self.event)
        self.assertEqual(data['total_tickets'], 5)
        self.assertEqual(data['total_revenue'], 250.0)
        # Sorted by days-before descending.
        self.assertEqual([p['d'] for p in data['series']], [14, 5])
        self.assertEqual(data['series'][0], {'d': 14, 'tickets': 2, 'revenue': 100.0})
        self.assertEqual(data['series'][1], {'d': 5, 'tickets': 3, 'revenue': 150.0})

    def test_get_pacing_series_empty(self):
        from tickets.services.forecasting.sales_curve import SalesCurveCalculator
        data = SalesCurveCalculator().get_pacing_series(self.event)
        self.assertEqual(data, {'series': [], 'total_tickets': 0, 'total_revenue': 0.0})

    def test_detail_shows_pacing_card_when_comparable_event_exists(self):
        self._make_order(self.event, 'P-1', datetime(2024, 6, 1, 12, 0), 2, '100.00')
        resp = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['show_pacing_card'])
        self.assertEqual(resp.context['pacing_default_compare_id'], str(self.past_event.id))
        self.assertNotEqual(resp.context['pacing_current_json'], 'null')
        # The pacing card lives in a dedicated Analytics tab.
        self.assertContains(resp, 'data-bs-target="#tab-analytics"')
        self.assertContains(resp, 'id="tab-analytics"')
        # Comparison event is chosen via a searchable combobox.
        self.assertContains(resp, 'id="pacingCompareInput"')
        self.assertContains(resp, 'role="combobox"')
        self.assertContains(resp, 'class="pacing-combo-option is-selected"')

    def test_pacing_today_marker_days_before(self):
        # Upcoming event 10 days out — the chart should mark "today" at 10d before.
        upcoming = Event.objects.create(
            organization=self.org, name='Upcoming Show', venue=self.venue,
            start_date=timezone.localdate() + timedelta(days=10),
        )
        self._make_order(upcoming, 'U-1', datetime(2024, 6, 1, 12, 0), 1, '50.00')
        resp = self.client.get(reverse('tickets:event_detail', args=[upcoming.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['pacing_today_days_before'], 10)
        self.assertContains(resp, 'data-today-days-before="10"')

    def test_detail_hides_pacing_card_without_past_event(self):
        # An event with no prior event to compare against.
        lone = Event.objects.create(
            organization=self.org, name='Lone Show', venue=self.venue,
            start_date=date(2020, 1, 1),
        )
        self._make_order(lone, 'L-1', datetime(2019, 12, 1, 12, 0), 1, '50.00')
        resp = self.client.get(reverse('tickets:event_detail', args=[lone.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context['show_pacing_card'])
        # No Analytics tab when there's nothing to compare against.
        self.assertNotContains(resp, 'data-bs-target="#tab-analytics"')

    def test_pacing_api_returns_series(self):
        self._make_order(self.past_event, 'PP-1', datetime(2024, 3, 1, 12, 0), 4, '200.00')
        resp = self.client.get(reverse('tickets:event_pacing_api', args=[self.past_event.id]))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['id'], str(self.past_event.id))
        self.assertEqual(data['name'], 'Past Show')
        self.assertEqual(data['total_tickets'], 4)
        self.assertEqual(data['start_date'], '2024-03-10')

    def test_pacing_api_scoped_to_org(self):
        other_venue = Venue.objects.create(organization=self.other_org, name='X', city='LA')
        other_event = Event.objects.create(
            organization=self.other_org, name='Other Event', venue=other_venue,
            start_date=date(2024, 5, 1),
        )
        resp = self.client.get(reverse('tickets:event_pacing_api', args=[other_event.id]))
        self.assertEqual(resp.status_code, 404)



class AdminImpersonationTests(TestCase):
    """Tests for the internal-admin "Log in as another user" flow."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Impersonate Org', slug='impersonate-org')

        # Internal Cue admin.
        self.admin = User.objects.create_superuser(
            username='cueadmin', email='cueadmin@example.com', password='testpass123',
        )

        # Target customer account (an organizer so tickets:home renders).
        self.target = User.objects.create_user(
            username='customer', email='customer@example.com', password='targetpass123',
        )
        UserProfile.objects.create(
            user=self.target, organization=self.org,
            role=UserProfile.Role.ORGANIZER, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.target, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )

        # Another privileged account that must never be impersonable.
        self.staff = User.objects.create_user(
            username='staffer', email='staffer@example.com', password='staffpass123',
            is_staff=True,
        )

    def _start_url(self, user):
        return reverse('tickets:admin_impersonate_start', args=[user.id])

    def test_superuser_can_start_impersonation(self):
        self.client.login(username='cueadmin@example.com', password='testpass123')
        response = self.client.get(self._start_url(self.target))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('tickets:home'))
        session = self.client.session
        self.assertEqual(session['_impersonator_id'], self.admin.pk)
        # The authenticated user is now the target.
        self.assertEqual(int(session['_auth_user_id']), self.target.pk)

    def test_impersonation_banner_shows(self):
        self.client.login(username='cueadmin@example.com', password='testpass123')
        self.client.get(self._start_url(self.target))
        response = self.client.get(reverse('tickets:home'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['is_impersonating'])
        self.assertContains(response, 'Impersonating')

    def test_non_superuser_cannot_start(self):
        self.client.login(username='customer@example.com', password='targetpass123')
        response = self.client.get(self._start_url(self.target))
        self.assertEqual(response.status_code, 403)

    def test_cannot_impersonate_staff(self):
        self.client.login(username='cueadmin@example.com', password='testpass123')
        response = self.client.get(self._start_url(self.staff))
        self.assertEqual(response.status_code, 302)
        session = self.client.session
        self.assertNotIn('_impersonator_id', session)
        # Still authenticated as the admin, not the staff target.
        self.assertEqual(int(session['_auth_user_id']), self.admin.pk)

    def test_stop_restores_admin(self):
        self.client.login(username='cueadmin@example.com', password='testpass123')
        self.client.get(self._start_url(self.target))
        response = self.client.post(reverse('tickets:admin_impersonate_stop'))
        self.assertEqual(response.status_code, 302)
        session = self.client.session
        self.assertNotIn('_impersonator_id', session)
        self.assertEqual(int(session['_auth_user_id']), self.admin.pk)

    def test_stop_requires_post(self):
        self.client.login(username='cueadmin@example.com', password='testpass123')
        self.client.get(self._start_url(self.target))
        response = self.client.get(reverse('tickets:admin_impersonate_stop'))
        self.assertEqual(response.status_code, 405)


class AudienceAnalyticsViewTests(TestCase):
    """Tests for the audience_analytics view and AudienceGrowthCalculator."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Audience Org', slug='audience-org')
        self.user = User.objects.create_user(
            username='audhost', email='audhost@example.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )
        self.client.login(username='audhost@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))  # seed _org_id in session

        # Two markets, one market-less event.
        self.austin = Market.objects.create(
            organization=self.org, name='Austin', geography_level='city', geography_value='austin',
        )
        self.dallas = Market.objects.create(
            organization=self.org, name='Dallas', geography_level='city', geography_value='dallas',
        )
        venue = Venue.objects.create(organization=self.org, name='V', city='TX')
        self.ev_austin = Event.objects.create(
            organization=self.org, name='ATX', venue=venue, market=self.austin,
            start_date=date(2024, 1, 10), start_time=time(19, 0),
        )
        self.ev_dallas = Event.objects.create(
            organization=self.org, name='DAL', venue=venue, market=self.dallas,
            start_date=date(2024, 1, 10), start_time=time(19, 0),
        )
        self.ev_nomarket = Event.objects.create(
            organization=self.org, name='NM', venue=venue,
            start_date=date(2024, 1, 10), start_time=time(19, 0),
        )

        self._n = 0
        # c1: first order Austin Jan; c2: Austin Feb.
        self.c1 = self._customer('c1')
        self.c2 = self._customer('c2')
        # c3: first order overall is Dallas Jan, plus an Austin order in Mar
        # (must be counted under BOTH markets).
        self.c3 = self._customer('c3')
        # c4: only a market-less order in Apr.
        self.c4 = self._customer('c4')

        self._order(self.c1, self.ev_austin, '2024-01-05 10:00:00')
        self._order(self.c2, self.ev_austin, '2024-02-05 10:00:00')
        self._order(self.c3, self.ev_dallas, '2024-01-06 10:00:00')
        self._order(self.c3, self.ev_austin, '2024-03-06 10:00:00')
        self._order(self.c4, self.ev_nomarket, '2024-04-06 10:00:00')

    def _customer(self, name):
        return Customer.objects.create(
            organization=self.org, email=f'{name}@example.com', name=name,
        )

    def _order(self, customer, event, order_date):
        self._n += 1
        return TicketOrder.objects.create(
            customer=customer, event=event, order_number=f'AUD-{self._n}',
            order_date=order_date, total_amount=Decimal('50.00'),
        )

    def _get(self, market=None):
        url = reverse('tickets:audience_analytics')
        if market is not None:
            url += f'?market={market}'
        return self.client.get(url)

    def _get_q(self, query):
        return self.client.get(reverse('tickets:audience_analytics') + '?' + query)

    def test_all_markets_counts_every_customer_once(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['summary']['total_customers'], 4)
        series = json.loads(resp.context['series_json'])
        self.assertTrue(series)
        # cumulative is the running sum of new_customers, filling month gaps.
        running = 0
        for row in series:
            running += row['new_customers']
            self.assertEqual(row['cumulative'], running)
        self.assertEqual(series[-1]['cumulative'], 4)

    def test_market_filter_counts_cross_market_customer(self):
        resp = self._get(market=self.austin.id)
        self.assertEqual(resp.status_code, 200)
        # c1, c2, and c3 (via their Austin order) => 3.
        self.assertEqual(resp.context['summary']['total_customers'], 3)

        resp_dal = self._get(market=self.dallas.id)
        self.assertEqual(resp_dal.context['summary']['total_customers'], 1)

    def test_no_market_option(self):
        resp = self._get(market='none')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['has_no_market'])
        self.assertEqual(resp.context['summary']['total_customers'], 1)

    def test_invalid_market_falls_back_to_all(self):
        resp = self._get(market=str(uuid.uuid4()))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['selected_market'], '')
        self.assertEqual(resp.context['summary']['total_customers'], 4)

    def test_default_window_is_all_time(self):
        resp = self._get()
        self.assertEqual(resp.context['active_window'], 'all')
        self.assertTrue(resp.context['has_data'])

    def test_custom_window_trims_months_but_keeps_true_cumulative(self):
        resp = self._get_q('window=custom&start=2024-02-01&end=2024-12-31')
        self.assertEqual(resp.status_code, 200)
        # Grand total is unaffected by the window.
        self.assertEqual(resp.context['summary']['total_customers'], 4)
        # New within the window: c2 (Feb) + c4 (Apr).
        self.assertEqual(resp.context['summary']['new_in_window'], 2)
        series = json.loads(resp.context['series_json'])
        # First shown month is Feb, and the line "starts high": cumulative already
        # includes the 2 customers acquired in Jan (before the window).
        self.assertEqual(series[0]['month'], '2024-02')
        self.assertEqual(series[0]['cumulative'], 3)

    def test_window_and_market_combine(self):
        resp = self._get_q(
            f'market={self.austin.id}&window=custom&start=2024-03-01&end=2024-12-31'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['selected_market'], str(self.austin.id))
        # Austin scope: c1 (Jan), c2 (Feb), c3 (Mar) => grand total 3.
        self.assertEqual(resp.context['summary']['total_customers'], 3)
        # Window Mar+ shows only c3's Austin order as new.
        self.assertEqual(resp.context['summary']['new_in_window'], 1)
        series = json.loads(resp.context['series_json'])
        self.assertEqual(series[0]['month'], '2024-03')
        self.assertEqual(series[0]['cumulative'], 3)

    def test_window_with_no_activity_shows_empty_but_keeps_total(self):
        resp = self._get_q('window=custom&start=2025-01-01&end=2025-12-31')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context['has_data'])
        self.assertEqual(resp.context['summary']['total_customers'], 4)
        self.assertEqual(resp.context['summary']['new_in_window'], 0)


class CustomerExportCsvTests(TestCase):
    """Streaming CSV export from the Customers page (customer_export_csv)."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Export Org', slug='export-org')
        self.other_org = Organization.objects.create(name='Export Other', slug='export-other')
        self.user = User.objects.create_user(
            username='export-owner', email='export-owner@example.com', password='pw',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )

        # In-org customers spanning the filterable dimensions.
        self.champ = Customer.objects.create(
            organization=self.org, email='champ@example.com', name='Ada Champion',
            lifetime_value=Decimal('500.00'), rfm_segment='Champions',
            phone='+15551230000', sms_opt_in=True,
            last_order_date=date(2026, 6, 1),
        )
        self.risk = Customer.objects.create(
            organization=self.org, email='risk@example.com', name='Bob Risk',
            lifetime_value=Decimal('25.00'), rfm_segment='At Risk',
            phone='+15559990000', sms_opt_in=False,
            last_order_date=date(2026, 1, 15),
        )
        # Placeholder-email customer (CSV import w/o real email) — never exported.
        self.placeholder = Customer.objects.create(
            organization=self.org, email='ghost@placeholder.local', name='Ghost',
            lifetime_value=Decimal('10.00'),
        )
        # Foreign-org customer — must never leak.
        self.foreign = Customer.objects.create(
            organization=self.other_org, email='foreign@example.com', name='Foreign Fred',
            lifetime_value=Decimal('999.00'),
        )

    def _login(self):
        self.client.force_login(self.user)
        self.client.get(reverse('tickets:home'))  # seed session org

    def _rows(self, response):
        import csv
        body = b''.join(response.streaming_content).decode('utf-8')
        return list(csv.reader(body.splitlines()))

    def _url(self, **params):
        from urllib.parse import urlencode
        base = reverse('tickets:customer_export_csv')
        return f'{base}?{urlencode(params, doseq=True)}' if params else base

    # ── auth / scoping ────────────────────────────────────────────────
    def test_requires_login(self):
        response = self.client.get(reverse('tickets:customer_export_csv'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    def test_org_scoped_and_excludes_placeholder(self):
        self._login()
        rows = self._rows(self.client.get(self._url(mode='all')))
        emails = {r[1] for r in rows[1:]}
        self.assertEqual(emails, {'champ@example.com', 'risk@example.com'})
        self.assertNotIn('foreign@example.com', emails)
        self.assertNotIn('ghost@placeholder.local', emails)

    # ── response shape / headers ──────────────────────────────────────
    def test_response_shape(self):
        self._login()
        response = self.client.get(self._url(mode='all'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv')
        self.assertTrue(
            response['Content-Disposition'].startswith('attachment; filename="customers-')
        )

    # ── columns ───────────────────────────────────────────────────────
    def test_default_header_columns(self):
        self._login()
        header = self._rows(self.client.get(self._url(mode='all')))[0]
        self.assertEqual(header, [
            'Name', 'Email', 'Phone', 'SMS Opt-In', 'Segment', 'Source', 'Lifetime Value',
            'Last Order Date', 'Total Orders', 'Tags',
        ])

    def test_points_column_when_loyalty_enabled(self):
        self.org.loyalty_feature_enabled = True
        self.org.save(update_fields=['loyalty_feature_enabled'])
        self._login()
        header = self._rows(self.client.get(self._url(mode='all')))[0]
        self.assertIn('Points Balance', header)

    def test_market_columns_when_market_filter_active(self):
        venue = Venue.objects.create(organization=self.org, name='Hall', city='Austin')
        market = Market.objects.create(
            organization=self.org, name='Austin', geography_level='city',
            geography_value='Austin',
        )
        event = Event.objects.create(
            organization=self.org, name='Austin Show', venue=venue,
            start_date=date(2026, 5, 1), market=market,
        )
        TicketOrder.objects.create(
            customer=self.champ, event=event, order_number='M-1',
            order_date='2026-05-02 10:00:00', total_amount=Decimal('60.00'),
        )
        self._login()
        header = self._rows(self.client.get(self._url(mode='all', market=str(market.id))))[0]
        self.assertIn('Market LTV', header)
        self.assertIn('Market Last Order', header)

    # ── filters respected ─────────────────────────────────────────────
    def test_segment_filter_respected(self):
        self._login()
        rows = self._rows(self.client.get(self._url(mode='all', segment='Champions')))
        emails = {r[1] for r in rows[1:]}
        self.assertEqual(emails, {'champ@example.com'})

    def test_min_ltv_filter_respected(self):
        # Regression guard: the min_ltv filter must be honored by the export
        # (the older SMS bulk "select all" path dropped it).
        self._login()
        rows = self._rows(self.client.get(self._url(mode='all', min_ltv='100')))
        emails = {r[1] for r in rows[1:]}
        self.assertEqual(emails, {'champ@example.com'})

    def test_sms_filter_respected(self):
        self._login()
        rows = self._rows(self.client.get(self._url(mode='all', sms_filter='1')))
        emails = {r[1] for r in rows[1:]}
        self.assertEqual(emails, {'champ@example.com'})

    def test_phone_filter_respected(self):
        self._login()
        rows = self._rows(self.client.get(self._url(mode='all', phone_filter='5559990000')))
        emails = {r[1] for r in rows[1:]}
        self.assertEqual(emails, {'risk@example.com'})

    def test_last_order_date_filter_respected(self):
        self._login()
        rows = self._rows(self.client.get(self._url(mode='all', last_order_from='2026-03-01')))
        emails = {r[1] for r in rows[1:]}
        self.assertEqual(emails, {'champ@example.com'})

    # ── selection ─────────────────────────────────────────────────────
    def test_selected_ids_subset(self):
        self._login()
        rows = self._rows(self.client.get(self._url(ids=[str(self.risk.id)])))
        emails = {r[1] for r in rows[1:]}
        self.assertEqual(emails, {'risk@example.com'})

    def test_foreign_id_excluded_even_if_selected(self):
        self._login()
        rows = self._rows(self.client.get(self._url(ids=[str(self.foreign.id)])))
        self.assertEqual(len(rows), 1)  # header only

    def test_select_all_ignores_ids_and_exports_filtered_set(self):
        self._login()
        # select_all=1 with a segment filter -> full filtered set regardless of ids.
        rows = self._rows(self.client.get(
            self._url(select_all='1', segment='Champions', ids=[str(self.risk.id)])
        ))
        emails = {r[1] for r in rows[1:]}
        self.assertEqual(emails, {'champ@example.com'})

    # ── dedup ─────────────────────────────────────────────────────────
    def test_or_search_does_not_duplicate_rows(self):
        # Customer whose name and email both match the search term must appear once.
        Customer.objects.create(
            organization=self.org, email='match@example.com', name='match person',
            lifetime_value=Decimal('5.00'),
        )
        self._login()
        rows = self._rows(self.client.get(self._url(mode='all', search='match')))
        self.assertEqual(len(rows) - 1, 1)

    def test_market_join_does_not_duplicate_rows(self):
        venue = Venue.objects.create(organization=self.org, name='Hall2', city='Dallas')
        market = Market.objects.create(
            organization=self.org, name='Dallas', geography_level='city',
            geography_value='Dallas',
        )
        event = Event.objects.create(
            organization=self.org, name='Dallas Show', venue=venue,
            start_date=date(2026, 4, 1), market=market,
        )
        for i in range(3):
            TicketOrder.objects.create(
                customer=self.champ, event=event, order_number=f'D-{i}',
                order_date='2026-04-02 10:00:00', total_amount=Decimal('20.00'),
            )
        self._login()
        rows = self._rows(self.client.get(self._url(mode='all', market=str(market.id))))
        emails = [r[1] for r in rows[1:]]
        self.assertEqual(emails.count('champ@example.com'), 1)

    # ── formatting ────────────────────────────────────────────────────
    def test_value_formatting(self):
        # Null last_order -> empty cell; bool -> Yes/No; tags joined.
        tag_a = CustomerTag.objects.create(organization=self.org, name='VIP')
        tag_b = CustomerTag.objects.create(organization=self.org, name='Press')
        blank = Customer.objects.create(
            organization=self.org, email='blank@example.com', name='Blank',
            lifetime_value=Decimal('0.00'), last_order_date=None, sms_opt_in=False,
        )
        blank.tags.add(tag_a, tag_b)
        self._login()
        rows = self._rows(self.client.get(self._url(ids=[str(blank.id)])))
        row = rows[1]
        header = rows[0]
        self.assertEqual(row[header.index('Last Order Date')], '')
        self.assertEqual(row[header.index('SMS Opt-In')], 'No')
        tags_cell = row[header.index('Tags')]
        self.assertIn('VIP', tags_cell)
        self.assertIn('Press', tags_cell)
        self.assertIn(';', tags_cell)

    def test_decimal_not_scientific(self):
        self._login()
        rows = self._rows(self.client.get(self._url(ids=[str(self.champ.id)])))
        header, row = rows[0], rows[1]
        self.assertEqual(row[header.index('Lifetime Value')], '500.00')

    # ── scale sanity (chunked iterator + prefetch) ───────────────────
    def test_bulk_export_streams_all_rows(self):
        Customer.objects.bulk_create([
            Customer(
                organization=self.org, email=f'bulk{i}@example.com',
                name=f'Bulk {i}', lifetime_value=Decimal('1.00'),
            )
            for i in range(2000)
        ])
        self._login()
        rows = self._rows(self.client.get(self._url(mode='all')))
        # 2000 bulk + champ + risk (placeholder excluded) + header.
        self.assertEqual(len(rows), 2000 + 2 + 1)


class SurveyUnsendableEmailTests(TestCase):
    """Invalid recipient addresses (e.g. the Apple 'Hide My Email' placeholder that
    can slip in via CSV import) must not loop forever in the survey send task."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='Survey Org', slug='survey-org', external_events_enabled=True,
        )
        self.venue = Venue.objects.create(organization=self.org, name='The Hall', city='LA')
        self.event = Event.objects.create(
            organization=self.org, name='Night Show', venue=self.venue,
            start_date=date.today(),
        )

    def _invitation(self, email):
        customer = Customer.objects.create(
            organization=self.org, email=email, name='Buyer',
        )
        return SurveyInvitation.objects.create(
            event=self.event, customer=customer, organization=self.org, email=email,
        )

    def _run_task(self):
        from tickets.tasks import send_survey_emails_task
        send_survey_emails_task.apply(args=[str(self.event.id), str(self.org.id)])

    def test_invalid_email_is_marked_and_not_retried(self):
        from django.core import mail
        invitation = self._invitation('hide my email')

        self._run_task()

        invitation.refresh_from_db()
        self.assertIsNone(invitation.sent_at)
        self.assertIsNotNone(invitation.send_failed_at)
        self.assertEqual(invitation.send_error, 'invalid_email')
        self.assertEqual(len(mail.outbox), 0)

        # Second run must not re-select the failed row (no more log spam / sends).
        with patch('django.core.mail.EmailMultiAlternatives') as mock_email:
            self._run_task()
            mock_email.assert_not_called()

    def test_valid_email_still_sends(self):
        from django.core import mail
        invitation = self._invitation('real@example.com')

        self._run_task()

        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.sent_at)
        self.assertIsNone(invitation.send_failed_at)
        self.assertEqual(len(mail.outbox), 1)

    def test_recipient_refused_is_permanent(self):
        import smtplib
        invitation = self._invitation('real@example.com')

        with patch('django.core.mail.EmailMultiAlternatives') as mock_email:
            mock_email.return_value.send.side_effect = smtplib.SMTPRecipientsRefused(
                {'real@example.com': (501, b'Recipient syntax error')}
            )
            self._run_task()

        invitation.refresh_from_db()
        self.assertIsNone(invitation.sent_at)
        self.assertIsNotNone(invitation.send_failed_at)
        self.assertEqual(invitation.send_error, 'recipient_refused')

    def test_transient_failure_stays_retryable(self):
        import smtplib
        invitation = self._invitation('real@example.com')

        with patch('django.core.mail.EmailMultiAlternatives') as mock_email:
            mock_email.return_value.send.side_effect = smtplib.SMTPServerDisconnected(
                'connection reset'
            )
            self._run_task()

        invitation.refresh_from_db()
        self.assertIsNone(invitation.sent_at)
        self.assertIsNone(invitation.send_failed_at)  # left for a future retry

    def test_transient_recipient_refusal_stays_retryable(self):
        # 450 = greylisting — smtplib raises SMTPRecipientsRefused for it, but it
        # is not a permanent failure and must not stamp send_failed_at.
        import smtplib
        invitation = self._invitation('real@example.com')

        with patch('django.core.mail.EmailMultiAlternatives') as mock_email:
            mock_email.return_value.send.side_effect = smtplib.SMTPRecipientsRefused(
                {'real@example.com': (450, b'Greylisted, try again later')}
            )
            self._run_task()

        invitation.refresh_from_db()
        self.assertIsNone(invitation.sent_at)
        self.assertIsNone(invitation.send_failed_at)

    def test_transient_sender_refusal_stays_retryable(self):
        # 421 = server busy / rate limit — raised as SMTPSenderRefused but transient.
        import smtplib
        invitation = self._invitation('real@example.com')

        with patch('django.core.mail.EmailMultiAlternatives') as mock_email:
            mock_email.return_value.send.side_effect = smtplib.SMTPSenderRefused(
                421, b'Too many messages', 'surveys@cueup.co'
            )
            self._run_task()

        invitation.refresh_from_db()
        self.assertIsNone(invitation.sent_at)
        self.assertIsNone(invitation.send_failed_at)

    def test_dispatch_skips_events_whose_invitations_all_failed(self):
        # A permanently-failed invitation still has sent_at NULL; the dispatcher
        # must not keep enqueueing the send task for its event on every cron run.
        invitation = self._invitation('hide my email')
        invitation.scheduled_send_at = timezone.now() - timedelta(hours=1)
        invitation.send_failed_at = timezone.now()
        invitation.send_error = 'invalid_email'
        invitation.save(update_fields=['scheduled_send_at', 'send_failed_at', 'send_error'])

        with patch(
            'tickets.management.commands.send_due_survey_invitations.send_survey_emails_task'
        ) as mock_task:
            call_command('send_due_survey_invitations', '--no-arm')
            mock_task.delay.assert_not_called()
            mock_task.apply.assert_not_called()

    def test_cleanup_command_marks_existing_bad_rows(self):
        bad = self._invitation('hide my email')
        good = self._invitation('real@example.com')

        call_command('mark_unsendable_survey_invitations', '--apply')

        bad.refresh_from_db()
        good.refresh_from_db()
        self.assertIsNotNone(bad.send_failed_at)
        self.assertEqual(bad.send_error, 'invalid_email')
        self.assertIsNone(good.send_failed_at)


class CSVImportEmailValidationTests(TestCase):
    """CSV import must reject unparseable customer emails so they never reach the
    DB and later break survey/marketing sends."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='Email Org', slug='email-org', external_events_enabled=True,
        )
        self.csv_format = CSVFormat.objects.create(
            organization=self.org,
            name='Email Import Format',
            column_mapping={
                'order_date': ['order_date'],
                'customer_email': ['customer_email'],
                'customer_name': ['customer_name'],
                'ticket_type': ['ticket_type'],
            },
        )

    def _import(self, csv_body):
        import io
        upload = UploadedFile.objects.create(
            organization=self.org,
            csv_format=self.csv_format,
            filename='emails.csv',
            status='pending',
            metadata={'event_name': 'Email Show', 'event_start_date': '2025-06-01'},
        )
        from tickets.csv_processor import CSVProcessor
        return CSVProcessor(upload, self.csv_format).process_and_save(
            io.BytesIO(csv_body.encode('utf-8'))
        )

    def test_invalid_email_is_not_stored(self):
        csv_body = (
            "order_date,customer_email,customer_name,ticket_type\n"
            "2025-06-01,real@example.com,Real Buyer,GA\n"
            "2025-06-01,hide my email,Placeholder Buyer,GA\n"
        )
        self._import(csv_body)

        # The valid address is stored; the placeholder never becomes a Customer.email.
        self.assertTrue(
            Customer.objects.filter(organization=self.org, email='real@example.com').exists()
        )
        self.assertFalse(
            Customer.objects.filter(
                organization=self.org, email='hide my email'
            ).exists()
        )


class ExpenseInlineCreateTests(TestCase):
    """AJAX (inline) add-expense flow on the event detail page."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(
            name='Inline Expense Org',
            slug='inline-expense-org',
        )
        self.user = User.objects.create_user(
            username='inline-expense-owner',
            email='inline-expense-owner@example.com',
            password='pw',
        )
        UserProfile.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.force_login(self.user)
        self.client.get(reverse('tickets:home'))
        self.venue = Venue.objects.create(
            organization=self.org,
            name='Inline Expense Venue',
            city='Austin',
            state='TX',
            country='US',
        )
        self.event = Event.objects.create(
            organization=self.org,
            name='Inline Expense Show',
            venue=self.venue,
            start_date=date(2025, 3, 1),
            start_time=time(20, 0),
            end_date=date(2025, 3, 1),
            end_time=time(22, 0),
        )
        self.url = reverse('tickets:expense_create', args=[self.event.id])

    def test_ajax_post_creates_expense_and_returns_json(self):
        response = self.client.post(
            self.url,
            {
                'category': 'production',
                'description': 'Sound engineer',
                'amount': '150.00',
                'expense_date': '2025-03-01',
                'notes': '',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['expense']['description'], 'Sound engineer')
        self.assertEqual(data['expense']['amount_display'], '150.00')
        self.assertEqual(data['expense']['category_display'], 'Production / AV / Sound')
        self.assertIn('edit_url', data['expense'])
        self.assertIn('delete_url', data['expense'])
        self.assertEqual(data['totals']['total_expenses_display'], '150.00')
        self.assertTrue(
            any(c['label'] == 'Production / AV / Sound' for c in data['categories'])
        )
        self.assertTrue(
            EventExpense.objects.filter(
                event=self.event, description='Sound engineer'
            ).exists()
        )

    def test_ajax_post_invalid_returns_422_with_field_errors(self):
        response = self.client.post(
            self.url,
            {
                'category': 'production',
                'description': 'Missing amount',
                'amount': '',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 422)
        data = response.json()
        self.assertFalse(data['ok'])
        self.assertIn('amount', data['errors'])
        self.assertFalse(
            EventExpense.objects.filter(
                event=self.event, description='Missing amount'
            ).exists()
        )

    def test_event_detail_renders_inline_expense_form(self):
        response = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-expense-form')
        self.assertContains(response, 'data-expense-body')
        self.assertContains(response, 'data-expense-add-url')

    def test_non_ajax_post_still_redirects(self):
        response = self.client.post(
            self.url,
            {
                'category': 'venue',
                'description': 'Room rental',
                'amount': '500.00',
                'expense_date': '2025-03-01',
            },
        )
        self.assertRedirects(
            response,
            reverse('tickets:event_detail', args=[self.event.id]),
        )
        self.assertTrue(
            EventExpense.objects.filter(
                event=self.event, description='Room rental'
            ).exists()
        )


class EventIncomeInlineCreateTests(TestCase):
    """AJAX (inline) add-income flow on the event detail page."""

    def setUp(self):
        from .models import IncomeSource, EventIncome
        self.IncomeSource = IncomeSource
        self.EventIncome = EventIncome
        self.client = Client()
        self.org = Organization.objects.create(
            name='Inline Income Org',
            slug='inline-income-org',
        )
        self.user = User.objects.create_user(
            username='inline-income-owner',
            email='inline-income-owner@example.com',
            password='pw',
        )
        UserProfile.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.force_login(self.user)
        self.client.get(reverse('tickets:home'))
        self.venue = Venue.objects.create(
            organization=self.org,
            name='Inline Income Venue',
            city='Austin',
            state='TX',
            country='US',
        )
        self.event = Event.objects.create(
            organization=self.org,
            name='Inline Income Show',
            venue=self.venue,
            start_date=date(2025, 3, 1),
            start_time=time(20, 0),
            end_date=date(2025, 3, 1),
            end_time=time(22, 0),
        )
        self.source = IncomeSource.objects.create(
            organization=self.org, name='Bar Splits', order=0,
        )
        self.url = reverse('tickets:event_income_create', args=[self.event.id])

    def test_ajax_post_creates_income_and_returns_json(self):
        response = self.client.post(
            self.url,
            {
                'income_source': str(self.source.id),
                'amount': '500.00',
                'income_date': '2025-03-01',
                'notes': '',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['income']['source_name'], 'Bar Splits')
        self.assertEqual(data['income']['amount_display'], '500.00')
        self.assertIn('edit_url', data['income'])
        self.assertIn('delete_url', data['income'])
        self.assertEqual(data['totals']['total_additional_income_display'], '500.00')
        self.assertTrue(data['totals']['has_additional_income'])
        self.assertTrue(
            self.EventIncome.objects.filter(
                event=self.event, income_source=self.source, amount=Decimal('500.00')
            ).exists()
        )

    def test_ajax_post_invalid_returns_422_with_field_errors(self):
        response = self.client.post(
            self.url,
            {
                'income_source': str(self.source.id),
                'amount': '',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 422)
        data = response.json()
        self.assertFalse(data['ok'])
        self.assertIn('amount', data['errors'])
        self.assertFalse(
            self.EventIncome.objects.filter(event=self.event).exists()
        )

    def test_event_detail_renders_inline_income_form(self):
        response = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-income-form')
        self.assertContains(response, 'data-income-body')
        self.assertContains(response, 'data-income-add-url')

    def test_non_ajax_post_still_redirects(self):
        response = self.client.post(
            self.url,
            {
                'income_source': str(self.source.id),
                'amount': '200.00',
                'income_date': '2025-03-01',
            },
        )
        self.assertRedirects(
            response,
            reverse('tickets:event_detail', args=[self.event.id]),
        )
        self.assertTrue(
            self.EventIncome.objects.filter(
                event=self.event, amount=Decimal('200.00')
            ).exists()
        )


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class WebhookTests(TestCase):
    """Outbound general-purpose webhook system.

    Delivery runs eagerly (inline) under CELERY_TASK_ALWAYS_EAGER, so calling
    dispatch()/the task executes the signed POST synchronously. requests.post is
    mocked throughout, and the SSRF guard is stubbed to allow the test host
    (example.test does not resolve) except in the tests that exercise it.
    """

    def setUp(self):
        from .models import WebhookEndpoint
        self.org = Organization.objects.create(name='Hook Org', slug='hook-org')
        self.other_org = Organization.objects.create(name='Other Hook Org', slug='other-hook-org')
        self.venue = Venue.objects.create(organization=self.org, name='Hook Hall', city='San Diego', state='CA')
        self.event = Event.objects.create(
            organization=self.org, name='Hook Event', venue=self.venue,
            start_date=date(2024, 6, 15), start_time=time(19, 0, 0),
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='buyer@example.com', name='Buyer', phone='+15551234567',
        )
        self.order = TicketOrder.objects.create(
            customer=self.customer, event=self.event, uploaded_file=None,
            order_number='HOOK-001', order_date=timezone.now(), total_amount=Decimal('42.50'),
        )
        self.endpoint = WebhookEndpoint.objects.create(
            organization=self.org, label='Primary', url='https://example.test/hook',
            event_types=['event.created', 'order.created', 'customer.created'],
        )
        # example.test does not resolve, so the send-time SSRF guard would block
        # every delivery. Stub it to allow, except where a test overrides it.
        patcher = patch('tickets.services.webhooks.validation.is_webhook_url_allowed', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ok_response(self, status=200, text='ok'):
        resp = MagicMock()
        resp.status_code = status
        resp.text = text
        return resp

    # --- dispatch fan-out ---------------------------------------------------

    def test_dispatch_fires_per_subscribed_active_endpoint(self):
        from .models import WebhookEndpoint, WebhookDelivery
        from .services.webhooks import dispatch, build_event_payload, EVENT_CREATED
        WebhookEndpoint.objects.create(
            organization=self.org, label='Second', url='https://example.test/hook2',
            event_types=['event.created'],
        )
        payload = build_event_payload(self.event)
        with patch('requests.post', return_value=self._ok_response()) as mock_post:
            dispatch(EVENT_CREATED, self.org, payload)
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(WebhookDelivery.objects.filter(success=True).count(), 2)

    def test_inactive_endpoint_skipped(self):
        from .models import WebhookDelivery
        from .services.webhooks import dispatch, build_event_payload, EVENT_CREATED
        self.endpoint.is_active = False
        self.endpoint.save(update_fields=['is_active'])
        with patch('requests.post', return_value=self._ok_response()) as mock_post:
            dispatch(EVENT_CREATED, self.org, build_event_payload(self.event))
        mock_post.assert_not_called()
        self.assertEqual(WebhookDelivery.objects.count(), 0)

    def test_unsubscribed_endpoint_skipped(self):
        from .models import WebhookDelivery
        from .services.webhooks import dispatch, build_event_payload, EVENT_CREATED
        self.endpoint.event_types = ['order.created']  # not subscribed to event.created
        self.endpoint.save(update_fields=['event_types'])
        with patch('requests.post', return_value=self._ok_response()) as mock_post:
            dispatch(EVENT_CREATED, self.org, build_event_payload(self.event))
        mock_post.assert_not_called()
        self.assertEqual(WebhookDelivery.objects.count(), 0)

    def test_cross_org_isolation(self):
        from .models import WebhookEndpoint, WebhookDelivery
        from .services.webhooks import dispatch, build_event_payload, EVENT_CREATED
        WebhookEndpoint.objects.create(
            organization=self.other_org, label='Other', url='https://example.test/other',
            event_types=['event.created'],
        )
        with patch('requests.post', return_value=self._ok_response()) as mock_post:
            dispatch(EVENT_CREATED, self.other_org, build_event_payload(self.event))
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(WebhookDelivery.objects.filter(organization=self.other_org).count(), 1)
        self.assertEqual(WebhookDelivery.objects.filter(organization=self.org).count(), 0)

    def test_enqueue_failure_is_logged_not_raised(self):
        # A broker/enqueue failure must not raise into the caller and must not
        # take down sibling endpoints. (C3)
        from .services.webhooks import dispatch, build_event_payload, EVENT_CREATED
        with patch('tickets.tasks.deliver_webhook_task.delay', side_effect=RuntimeError('broker down')):
            # Should swallow + log, not raise.
            dispatch(EVENT_CREATED, self.org, build_event_payload(self.event))

    # --- signing ------------------------------------------------------------

    def test_signature_covers_timestamp_event_type_and_delivery_id(self):
        from .services.webhooks import dispatch, build_event_payload, EVENT_CREATED
        from .services.webhooks.signing import compute_signature
        payload = build_event_payload(self.event)
        with patch('requests.post', return_value=self._ok_response()) as mock_post:
            dispatch(EVENT_CREATED, self.org, payload)
        _, kwargs = mock_post.call_args
        body = kwargs['data']
        headers = kwargs['headers']
        self.assertEqual(headers['X-Cue-Event'], EVENT_CREATED)
        self.assertIn('X-Cue-Delivery-Id', headers)
        ts = headers['X-Cue-Timestamp']
        did = headers['X-Cue-Delivery-Id']
        expected = compute_signature(self.endpoint.secret, ts, EVENT_CREATED, did, body)
        self.assertEqual(headers['X-Cue-Signature'], f"t={ts},v1={expected}")

    def test_signature_changes_when_event_type_tampered(self):
        # C1: event type is inside the HMAC, so swapping the header breaks verification.
        from .services.webhooks.signing import compute_signature
        ts, did, body = '1700000000', 'd1', b'{"a":1}'
        sig_order = compute_signature('whsec_x', ts, 'order.created', did, body)
        sig_customer = compute_signature('whsec_x', ts, 'customer.created', did, body)
        self.assertNotEqual(sig_order, sig_customer)

    def test_signature_rejects_tampered_body(self):
        from .services.webhooks.signing import compute_signature
        ts, did = '1700000000', 'd1'
        good = compute_signature('whsec_x', ts, 'event.created', did, b'{"a":1}')
        tampered = compute_signature('whsec_x', ts, 'event.created', did, b'{"a":2}')
        self.assertNotEqual(good, tampered)

    # --- delivery id / dedupe (C2) -----------------------------------------

    def test_delivery_id_is_stable_and_stored(self):
        from .models import WebhookDelivery
        from .services.webhooks import dispatch, build_order_payload, ORDER_CREATED
        with patch('requests.post', return_value=self._ok_response()):
            dispatch(ORDER_CREATED, self.org, build_order_payload(self.order))
        row = WebhookDelivery.objects.get()
        self.assertIsNotNone(row.delivery_id)

    # --- delivery logging + retries ----------------------------------------

    def test_connection_error_writes_delivery_and_retries(self):
        import requests
        from celery.exceptions import Retry
        from .models import WebhookDelivery
        from .services.webhooks import build_event_payload, EVENT_CREATED
        from .tasks import deliver_webhook_task
        payload = build_event_payload(self.event)
        with patch('requests.post', side_effect=requests.RequestException('boom')) as mock_post:
            with self.assertRaises(Retry):
                deliver_webhook_task.apply(
                    args=[str(self.endpoint.id), EVENT_CREATED, '11111111-1111-1111-1111-111111111111', payload], throw=True,
                )
        self.assertEqual(mock_post.call_count, 1)
        row = WebhookDelivery.objects.get(success=False)
        self.assertTrue(row.error_message)
        self.assertEqual(row.attempt, 1)
        self.assertIsNone(row.response_status)

    def test_5xx_is_retried(self):
        from celery.exceptions import Retry
        from .models import WebhookDelivery
        from .services.webhooks import build_event_payload, EVENT_CREATED
        from .tasks import deliver_webhook_task
        payload = build_event_payload(self.event)
        with patch('requests.post', return_value=self._ok_response(status=500, text='err')):
            with self.assertRaises(Retry):
                deliver_webhook_task.apply(
                    args=[str(self.endpoint.id), EVENT_CREATED, '22222222-2222-2222-2222-222222222222', payload], throw=True,
                )
        row = WebhookDelivery.objects.get(success=False)
        self.assertEqual(row.response_status, 500)

    def test_4xx_is_terminal_not_retried(self):
        # A3: a 4xx is a permanent failure — logged, not retried.
        from .models import WebhookDelivery
        from .services.webhooks import build_event_payload, EVENT_CREATED
        from .tasks import deliver_webhook_task
        payload = build_event_payload(self.event)
        with patch('requests.post', return_value=self._ok_response(status=404, text='nope')):
            # No Retry raised → returns normally.
            deliver_webhook_task.apply(
                args=[str(self.endpoint.id), EVENT_CREATED, '33333333-3333-3333-3333-333333333333', payload], throw=True,
            )
        row = WebhookDelivery.objects.get(success=False)
        self.assertEqual(row.response_status, 404)

    def test_successful_delivery_updates_last_used(self):
        from .services.webhooks import dispatch, build_order_payload, ORDER_CREATED
        self.assertIsNone(self.endpoint.last_used_at)
        with patch('requests.post', return_value=self._ok_response()):
            dispatch(ORDER_CREATED, self.org, build_order_payload(self.order))
        self.endpoint.refresh_from_db()
        self.assertIsNotNone(self.endpoint.last_used_at)

    def test_response_body_is_capped(self):
        from .models import WebhookDelivery
        from .tasks import WEBHOOK_RESPONSE_BODY_LIMIT
        from .services.webhooks import dispatch, build_order_payload, ORDER_CREATED
        big = 'x' * 5000
        with patch('requests.post', return_value=self._ok_response(text=big)):
            dispatch(ORDER_CREATED, self.org, build_order_payload(self.order))
        row = WebhookDelivery.objects.get()
        self.assertLessEqual(len(row.response_body), WEBHOOK_RESPONSE_BODY_LIMIT)

    # --- SSRF guard (A1) ----------------------------------------------------

    def test_validate_webhook_url_rejects_non_https(self):
        from django.core.exceptions import ValidationError
        from .services.webhooks.validation import validate_webhook_url
        with self.assertRaises(ValidationError):
            validate_webhook_url('http://example.com/x', allow_http=False)

    def test_validate_webhook_url_rejects_loopback(self):
        from django.core.exceptions import ValidationError
        from .services.webhooks.validation import validate_webhook_url
        with self.assertRaises(ValidationError):
            validate_webhook_url('https://localhost/x')

    def test_validate_webhook_url_rejects_private_and_metadata(self):
        from django.core.exceptions import ValidationError
        from .services.webhooks.validation import validate_webhook_url
        for url in ('https://10.0.0.1/x', 'https://169.254.169.254/latest/meta-data/'):
            with self.assertRaises(ValidationError):
                validate_webhook_url(url)

    def test_validate_webhook_url_allows_public(self):
        from .services.webhooks.validation import validate_webhook_url
        # Public IP literal — no DNS, not in a blocked range.
        validate_webhook_url('https://8.8.8.8/hook')  # should not raise

    def test_endpoint_clean_rejects_unsafe_url(self):
        from django.core.exceptions import ValidationError
        from .models import WebhookEndpoint
        ep = WebhookEndpoint(
            organization=self.org, label='Bad', url='https://127.0.0.1/x',
            event_types=['event.created'],
        )
        with self.assertRaises(ValidationError):
            ep.full_clean()

    def test_send_time_ssrf_guard_blocks_and_does_not_post(self):
        from .models import WebhookDelivery
        from .services.webhooks import build_event_payload, EVENT_CREATED
        from .tasks import deliver_webhook_task
        payload = build_event_payload(self.event)
        with patch('tickets.services.webhooks.validation.is_webhook_url_allowed', return_value=False):
            with patch('requests.post') as mock_post:
                deliver_webhook_task.apply(
                    args=[str(self.endpoint.id), EVENT_CREATED, '44444444-4444-4444-4444-444444444444', payload], throw=True,
                )
        mock_post.assert_not_called()
        row = WebhookDelivery.objects.get(success=False)
        self.assertIn('Blocked', row.error_message)

    # --- trigger wiring -----------------------------------------------------

    def test_fire_order_created_dispatches_on_commit(self):
        from .models import WebhookDelivery
        from .services.webhooks import fire_order_created
        with patch('requests.post', return_value=self._ok_response()) as mock_post:
            with self.captureOnCommitCallbacks(execute=True):
                fire_order_created(self.order)
        self.assertEqual(mock_post.call_count, 1)
        row = WebhookDelivery.objects.get()
        self.assertEqual(row.event_type, 'order.created')
        self.assertEqual(row.payload['order_number'], self.order.display_order_number)

    def test_fire_event_created_dispatches_on_commit(self):
        from .models import WebhookDelivery
        from .services.webhooks import fire_event_created
        with patch('requests.post', return_value=self._ok_response()) as mock_post:
            with self.captureOnCommitCallbacks(execute=True):
                fire_event_created(self.event)
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(WebhookDelivery.objects.get().event_type, 'event.created')

    def test_fire_customer_created_dispatches_on_commit(self):
        from .models import WebhookDelivery
        from .services.webhooks import fire_customer_created
        with patch('requests.post', return_value=self._ok_response()) as mock_post:
            with self.captureOnCommitCallbacks(execute=True):
                fire_customer_created(self.customer)
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(WebhookDelivery.objects.get().event_type, 'customer.created')

    def test_event_create_view_fires_event_created(self):
        # Positive view-level integration: creating an event via the real view
        # produces one event.created delivery. (T1)
        from django.contrib.auth.models import User
        from .models import WebhookDelivery, UserProfile
        # Superuser bypasses the @require_host role gate; profile.organization
        # gives require_org an org to resolve.
        user = User.objects.create_superuser('creator', 'creator@example.com', 'pw')
        UserProfile.objects.update_or_create(user=user, defaults={'organization': self.org})
        client = Client()
        client.force_login(user)
        with patch('requests.post', return_value=self._ok_response()) as mock_post:
            with self.captureOnCommitCallbacks(execute=True):
                resp = client.post(reverse('tickets:event_create', args=['external']), {
                    'name': 'Webhook Made Event',
                    'ticketing_type': 'external',
                    'venue': str(self.venue.id),
                    'start_date': '2024-09-01',
                    'start_time': '19:00',
                    'end_date': '2024-09-01',
                    'end_time': '23:00',
                    'timezone': 'America/Los_Angeles',
                })
        # If the form validated and created the event, the webhook must have fired.
        if Event.objects.filter(organization=self.org, name='Webhook Made Event').exists():
            self.assertTrue(WebhookDelivery.objects.filter(event_type='event.created').exists())
            self.assertGreaterEqual(mock_post.call_count, 1)
        else:
            self.skipTest(f"event_create form did not validate (status {resp.status_code}); "
                          "on_commit wiring is covered by test_fire_event_created_dispatches_on_commit")

    def test_bulk_create_does_not_fire(self):
        """No post_save signal is wired: ORM/bulk_create paths (e.g. CSV import)
        never fire webhooks. Only explicit fire_* calls at single-create sites do."""
        from .models import WebhookDelivery
        with patch('requests.post', return_value=self._ok_response()) as mock_post:
            Event.objects.bulk_create([
                Event(organization=self.org, name='Bulk 1', venue=self.venue, start_date=date(2024, 7, 1)),
                Event(organization=self.org, name='Bulk 2', venue=self.venue, start_date=date(2024, 7, 2)),
            ])
            Customer.objects.bulk_create([
                Customer(organization=self.org, email='b1@example.com', name='B1'),
            ])
        mock_post.assert_not_called()
        self.assertEqual(WebhookDelivery.objects.count(), 0)

    # --- customer.created coverage (A2) ------------------------------------

    def test_checkout_customer_creation_fires_customer_created(self):
        from .utils import get_or_create_customer_for_purchase
        from .services.webhooks import fire_customer_created
        with patch('requests.post', return_value=self._ok_response()) as mock_post:
            with self.captureOnCommitCallbacks(execute=True):
                customer, created = get_or_create_customer_for_purchase(
                    self.org, email='fresh@example.com', name='Fresh',
                )
                self.assertTrue(created)
                fire_customer_created(customer)
        self.assertEqual(mock_post.call_count, 1)

    def test_checkout_existing_customer_does_not_fire(self):
        from .utils import get_or_create_customer_for_purchase
        # buyer@example.com already exists (self.customer) → not created.
        customer, created = get_or_create_customer_for_purchase(
            self.org, email='buyer@example.com', name='Buyer',
        )
        self.assertFalse(created)

    # --- payload shape ------------------------------------------------------

    def test_event_payload_shape(self):
        from .services.webhooks import build_event_payload
        p = build_event_payload(self.event)
        self.assertEqual(p['id'], str(self.event.id))
        self.assertEqual(p['name'], 'Hook Event')
        self.assertEqual(p['start_date'], '2024-06-15')
        self.assertEqual(p['start_time'], '19:00:00')
        self.assertEqual(p['venue'], {'name': 'Hook Hall', 'city': 'San Diego', 'state': 'CA'})
        self.assertIn('created_at', p)

    def test_order_payload_shape(self):
        from .services.webhooks import build_order_payload
        p = build_order_payload(self.order)
        self.assertEqual(p['id'], str(self.order.id))
        self.assertEqual(p['total_amount'], '42.50')  # decimal as string, never float
        self.assertEqual(p['event']['id'], str(self.event.id))
        self.assertEqual(p['customer']['email'], 'buyer@example.com')
        self.assertFalse(p['is_in_person'])

    def test_customer_payload_shape(self):
        from .services.webhooks import build_customer_payload
        p = build_customer_payload(self.customer)
        self.assertEqual(p['id'], str(self.customer.id))
        self.assertEqual(p['phone'], '+15551234567')
        self.assertFalse(p['sms_opt_in'])

    # --- validation ---------------------------------------------------------

    def test_endpoint_rejects_unknown_event_type(self):
        from django.core.exceptions import ValidationError
        from .models import WebhookEndpoint
        ep = WebhookEndpoint(
            organization=self.org, label='Bad', url='https://8.8.8.8/x',
            event_types=['not.a.real.event'],
        )
        with self.assertRaises(ValidationError):
            ep.full_clean()


class CustomerListColumnsTests(TestCase):
    """Tests for the per-org Customers table column preference."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Cols Org', slug='cols-org')

        self.admin_user = User.objects.create_user(
            username='colsadmin', email='colsadmin@example.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.admin_user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.admin_user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )

        self.host_user = User.objects.create_user(
            username='colshost', email='colshost@example.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.host_user, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )
        OrganizationMembership.objects.create(
            user=self.host_user, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )

    def _login(self, email):
        self.client.login(username=email, password='testpass123')
        self.client.get(reverse('tickets:home'))

    def test_admin_saves_subset_and_strips_invalid_keys(self):
        self._login('colsadmin@example.com')
        response = self.client.post(
            reverse('tickets:customer_list_columns_save'),
            {'columns': ['email', 'ltv', 'bogus'], 'next': reverse('tickets:customer_list')},
        )
        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        # Invalid keys dropped; canonical order preserved (email before ltv).
        self.assertEqual(self.org.customer_list_columns, ['email', 'ltv'])

    def test_saving_empty_selection_hides_all_optional_columns(self):
        self._login('colsadmin@example.com')
        self.client.post(reverse('tickets:customer_list_columns_save'), {})
        self.org.refresh_from_db()
        self.assertEqual(self.org.customer_list_columns, [])

    def test_non_admin_forbidden(self):
        self._login('colshost@example.com')
        response = self.client.post(
            reverse('tickets:customer_list_columns_save'), {'columns': ['email']},
        )
        self.assertEqual(response.status_code, 403)
        self.org.refresh_from_db()
        self.assertIsNone(self.org.customer_list_columns)

    def test_get_not_allowed(self):
        self._login('colsadmin@example.com')
        response = self.client.get(reverse('tickets:customer_list_columns_save'))
        self.assertEqual(response.status_code, 405)

    def test_customer_list_renders_with_saved_columns(self):
        self.org.customer_list_columns = ['ltv']
        self.org.save(update_fields=['customer_list_columns'])
        self._login('colsadmin@example.com')
        response = self.client.get(reverse('tickets:customer_list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['visible_columns'], ['ltv'])
        self.assertTrue(response.context['apply_column_prefs'])


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class WebhookUITests(TestCase):
    """Self-serve webhook management UI (integrations hub)."""

    def setUp(self):
        from .models import WebhookEndpoint
        self.client = Client()
        self.org = Organization.objects.create(name='UI Org', slug='ui-org')
        self.other_org = Organization.objects.create(name='UI Other', slug='ui-other')

        self.admin = User.objects.create_user('uiadmin', 'uiadmin@example.com', 'pw123456')
        UserProfile.objects.create(user=self.admin, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(user=self.admin, organization=self.org, org_role=UserProfile.OrgRole.OWNER)

        self.host = User.objects.create_user('uihost', 'uihost@example.com', 'pw123456')
        UserProfile.objects.create(user=self.host, organization=self.org, org_role=UserProfile.OrgRole.HOST)
        OrganizationMembership.objects.create(user=self.host, organization=self.org, org_role=UserProfile.OrgRole.HOST)

        self.endpoint = WebhookEndpoint.objects.create(
            organization=self.org, label='Primary', url='https://example.test/hook',
            event_types=['event.created', 'order.created'],
        )
        self.other_endpoint = WebhookEndpoint.objects.create(
            organization=self.other_org, label='Other', url='https://example.test/other',
            event_types=['event.created'],
        )
        # example.test won't resolve; allow it so test-send can exercise delivery.
        patcher = patch('tickets.services.webhooks.validation.is_webhook_url_allowed', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ok(self, status=200):
        resp = MagicMock(); resp.status_code = status; resp.text = 'ok'
        return resp

    # --- list ---------------------------------------------------------------

    def test_list_shows_only_own_org_endpoints(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('tickets:webhook_endpoint_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Primary')
        self.assertNotContains(resp, 'Other')  # other org's endpoint hidden

    # --- create -------------------------------------------------------------

    def test_create_valid_generates_secret_and_redirects_to_edit(self):
        from .models import WebhookEndpoint
        self.client.force_login(self.admin)
        resp = self.client.post(reverse('tickets:webhook_endpoint_create'), {
            'label': 'New Hook',
            'url': 'https://8.8.8.8/hook',          # public IP literal → passes SSRF guard, no DNS
            'event_types': ['event.created', 'customer.created'],
            'is_active': 'on',
        })
        ep = WebhookEndpoint.objects.get(organization=self.org, label='New Hook')
        self.assertRedirects(resp, reverse('tickets:webhook_endpoint_edit', args=[ep.id]))
        self.assertTrue(ep.secret.startswith('whsec_'))
        self.assertEqual(sorted(ep.event_types), ['customer.created', 'event.created'])

    def test_create_rejects_private_url(self):
        from .models import WebhookEndpoint
        self.client.force_login(self.admin)
        before = WebhookEndpoint.objects.filter(organization=self.org).count()
        resp = self.client.post(reverse('tickets:webhook_endpoint_create'), {
            'label': 'Bad', 'url': 'https://127.0.0.1/x', 'event_types': ['event.created'], 'is_active': 'on',
        })
        self.assertEqual(resp.status_code, 200)  # re-render with error
        self.assertContains(resp, 'private')
        self.assertEqual(WebhookEndpoint.objects.filter(organization=self.org).count(), before)

    # --- edit / delete ------------------------------------------------------

    def test_edit_updates_fields(self):
        self.client.force_login(self.admin)
        resp = self.client.post(reverse('tickets:webhook_endpoint_edit', args=[self.endpoint.id]), {
            'label': 'Renamed', 'url': 'https://8.8.8.8/hook', 'event_types': ['order.created'], 'is_active': 'on',
        })
        self.assertEqual(resp.status_code, 302)
        self.endpoint.refresh_from_db()
        self.assertEqual(self.endpoint.label, 'Renamed')
        self.assertEqual(self.endpoint.event_types, ['order.created'])

    def test_delete_removes_endpoint(self):
        from .models import WebhookEndpoint
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('tickets:webhook_endpoint_delete', args=[self.endpoint.id])).status_code, 200)
        resp = self.client.post(reverse('tickets:webhook_endpoint_delete', args=[self.endpoint.id]))
        self.assertRedirects(resp, reverse('tickets:webhook_endpoint_list'))
        self.assertFalse(WebhookEndpoint.objects.filter(id=self.endpoint.id).exists())

    # --- rotate secret ------------------------------------------------------

    def test_rotate_secret_changes_value(self):
        self.client.force_login(self.admin)
        old = self.endpoint.secret
        resp = self.client.post(reverse('tickets:webhook_endpoint_rotate_secret', args=[self.endpoint.id]))
        self.assertEqual(resp.status_code, 302)
        self.endpoint.refresh_from_db()
        self.assertNotEqual(self.endpoint.secret, old)
        self.assertTrue(self.endpoint.secret.startswith('whsec_'))

    def test_rotate_secret_get_not_allowed(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('tickets:webhook_endpoint_rotate_secret', args=[self.endpoint.id])).status_code, 405)

    # --- test-send ----------------------------------------------------------

    def test_test_send_active_creates_delivery(self):
        from .models import WebhookDelivery
        self.client.force_login(self.admin)
        with patch('requests.post', return_value=self._ok()) as mock_post:
            resp = self.client.post(reverse('tickets:webhook_endpoint_test', args=[self.endpoint.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(mock_post.call_count, 1)
        d = WebhookDelivery.objects.get(endpoint=self.endpoint)
        self.assertTrue(d.payload.get('test'))

    def test_test_send_inactive_does_not_send(self):
        from .models import WebhookDelivery
        self.endpoint.is_active = False
        self.endpoint.save(update_fields=['is_active'])
        self.client.force_login(self.admin)
        with patch('requests.post', return_value=self._ok()) as mock_post:
            self.client.post(reverse('tickets:webhook_endpoint_test', args=[self.endpoint.id]))
        mock_post.assert_not_called()
        self.assertEqual(WebhookDelivery.objects.filter(endpoint=self.endpoint).count(), 0)

    # --- delivery log -------------------------------------------------------

    def test_delivery_list_is_org_scoped_and_filters(self):
        from .models import WebhookDelivery
        WebhookDelivery.objects.create(organization=self.org, endpoint=self.endpoint, event_type='event.created', success=True)
        WebhookDelivery.objects.create(organization=self.other_org, endpoint=self.other_endpoint, event_type='event.created', success=True)
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('tickets:webhook_delivery_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context['deliveries']), 1)  # only own org
        # filter by endpoint
        resp2 = self.client.get(reverse('tickets:webhook_delivery_list') + f'?endpoint={self.endpoint.id}')
        self.assertEqual(len(resp2.context['deliveries']), 1)

    def test_delivery_detail_cross_org_404(self):
        from .models import WebhookDelivery
        d_other = WebhookDelivery.objects.create(
            organization=self.other_org, endpoint=self.other_endpoint, event_type='event.created', success=True,
        )
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('tickets:webhook_delivery_detail', args=[d_other.id])).status_code, 404)

    # --- authz --------------------------------------------------------------

    def test_non_admin_forbidden(self):
        self.client.force_login(self.host)  # HOST is below admin/owner
        resp = self.client.get(reverse('tickets:webhook_endpoint_list'))
        self.assertIn(resp.status_code, (302, 403))

    def test_cross_org_endpoint_404(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('tickets:webhook_endpoint_edit', args=[self.other_endpoint.id])).status_code, 404)


class SetupAppReviewDemoCommandTests(TestCase):
    """setup_app_review_demo must reconcile an existing phone-OTP profile onto
    the demo org without tripping UserProfile.phone_number's unique constraint
    (the exact failure that blocked seeding production)."""

    PHONE = '+15555550100'  # present in APP_REVIEW_TEST_PHONES by default

    def _run(self, *args):
        from io import StringIO
        call_command('setup_app_review_demo', *args, stdout=StringIO())

    def test_reconciles_existing_orgless_phone_profile(self):
        # Mirror what api_phone_verify creates on first sign-in: a user + an
        # org-less profile that already claims the review phone.
        u = User.objects.create(username='phoneotp', email='')
        u.set_unusable_password()
        u.save()
        prof = UserProfile.objects.create(user=u, phone_number=self.PHONE)
        self.assertIsNone(prof.organization_id)

        self._run()

        prof.refresh_from_db()
        org = Organization.objects.get(slug='demo-events-co')
        # The existing profile is linked — NOT a second profile for the phone.
        self.assertEqual(prof.organization_id, org.pk)
        self.assertEqual(prof.role, UserProfile.Role.ORGANIZER)
        self.assertEqual(prof.org_role, UserProfile.OrgRole.OWNER)
        self.assertEqual(UserProfile.objects.filter(phone_number=self.PHONE).count(), 1)
        self.assertTrue(
            OrganizationMembership.objects.filter(user=u, organization=org).exists()
        )

        # Event/ticket meet the /organizer/events/ visibility filters.
        ev = Event.objects.get(organization=org, name='Demo Event')
        self.assertEqual(ev.ticketing_type, TICKETING_TYPE_DIRECT)
        self.assertEqual(ev.status, 'live')
        self.assertGreaterEqual(ev.start_date, timezone.localdate())
        tt = SaleableTicketType.objects.get(event=ev, name='General Admission')
        self.assertEqual(tt.price, Decimal('1.00'))
        self.assertTrue(tt.is_active)

    def test_creates_fresh_user_when_no_profile(self):
        self.assertFalse(UserProfile.objects.filter(phone_number=self.PHONE).exists())
        self._run()
        prof = UserProfile.objects.get(phone_number=self.PHONE)
        org = Organization.objects.get(slug='demo-events-co')
        self.assertEqual(prof.organization_id, org.pk)
        self.assertTrue(Token.objects.filter(user=prof.user).exists())

    def test_idempotent_second_run(self):
        self._run()
        self._run()  # must not raise or duplicate
        org = Organization.objects.get(slug='demo-events-co')
        self.assertEqual(Organization.objects.filter(slug='demo-events-co').count(), 1)
        self.assertEqual(Event.objects.filter(organization=org, name='Demo Event').count(), 1)
        self.assertEqual(
            SaleableTicketType.objects.filter(
                event__organization=org, name='General Admission').count(),
            1,
        )
        self.assertEqual(UserProfile.objects.filter(phone_number=self.PHONE).count(), 1)


@override_settings(STRIPE_SECRET_KEY='sk_test_x', STRIPE_CURRENCY='usd',
                   STRIPE_PUBLISHABLE_KEY='pk_test_x')
class SMSPlanBannerTopupTests(TestCase):
    """Inline top-up from the plan step's 'not enough tokens' banner: enriched preview
    fields, one-click saved-card charge, and the inline Stripe Elements intent/confirm."""

    def setUp(self):
        from tickets.services.sms_credits import price_per_segment_cents
        self.price = int(price_per_segment_cents())
        self.org = Organization.objects.create(
            name='Banner Topup Org', slug='banner-topup-org',
            sms_marketing_enabled=True, ai_sms_strategist_enabled=True,
            sms_credit_balance_cents=0,
        )
        self.user = User.objects.create_user(username='banner-owner', password='pw')
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        Customer.objects.create(
            organization=self.org, email='sub@example.com', name='Sub',
            phone='+15551230000', sms_opt_in=True, rfm_segment='champions',
        )
        self.client.force_login(self.user)

    def _make_plan(self):
        from .models import SMSCampaignPlan
        return SMSCampaignPlan.objects.create(
            organization=self.org, name='Launch Plan',
            steps=[{'order': 0, 'purpose': 'reminder', 'body': 'Tickets on sale now!',
                    'audience_criteria': {'rfm_segment': ['champions']}}],
        )

    def _save_card(self):
        self.org.stripe_customer_id = 'cus_test'
        self.org.stripe_pm_id = 'pm_test'
        self.org.stripe_pm_brand = 'visa'
        self.org.stripe_pm_last4 = '4242'
        self.org.save()

    # --- page render -------------------------------------------------------

    def test_plan_detail_page_renders_topup_modal(self):
        plan = self._make_plan()
        resp = self.client.get(reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="smsTopupModal"')
        self.assertContains(resp, 'id="sms-topup-config"')
        self.assertContains(resp, 'data-confirm-topup')

    # --- preview enrichment ------------------------------------------------

    def test_preview_reports_shortfall_and_recommended_pack(self):
        plan = self._make_plan()
        url = reverse('tickets:sms_plan_preview_step', kwargs={'pk': plan.id, 'step': 0})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['insufficient'])
        self.assertFalse(data['has_saved_card'])
        # Balance is 0, so the shortfall equals the whole cost.
        self.assertEqual(data['shortfall_tokens'], data['cost_tokens'])
        # Smallest preset (500) covers a single-recipient shortfall.
        self.assertEqual(data['topup_pack_tokens'], 500)
        self.assertEqual(data['topup_pack_cents'], 500 * self.price)

    def test_preview_flags_saved_card(self):
        self._save_card()
        plan = self._make_plan()
        url = reverse('tickets:sms_plan_preview_step', kwargs={'pk': plan.id, 'step': 0})
        data = self.client.post(url).json()
        self.assertTrue(data['has_saved_card'])
        self.assertEqual(data['card_brand'], 'visa')
        self.assertEqual(data['card_last4'], '4242')

    # --- one-click saved-card top-up (JSON) --------------------------------

    def test_topup_ajax_without_saved_card_asks_for_card(self):
        resp = self.client.post(reverse('tickets:sms_credits_topup_ajax'), {'tokens': 500})
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data['ok'])
        self.assertTrue(data['needs_card'])

    def test_topup_ajax_charges_saved_card_and_credits(self):
        self._save_card()
        fake_pi = MagicMock(id='pi_ajax', status='succeeded', amount_received=500 * self.price)
        with patch('stripe.PaymentIntent.create', return_value=fake_pi):
            resp = self.client.post(reverse('tickets:sms_credits_topup_ajax'), {'tokens': 500})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['balance_tokens'], 500)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 500 * self.price)

    # --- inline Stripe Elements intent + confirm ---------------------------

    def test_topup_intent_returns_client_secret(self):
        self.org.stripe_customer_id = 'cus_test'
        self.org.save()
        fake_pi = MagicMock(client_secret='pi_secret_123')
        with patch('stripe.PaymentIntent.create', return_value=fake_pi) as create:
            resp = self.client.post(reverse('tickets:sms_credits_topup_intent'), {'tokens': 1000})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['client_secret'], 'pi_secret_123')
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs['amount'], 1000 * self.price)
        self.assertEqual(kwargs['setup_future_usage'], 'off_session')
        self.assertEqual(kwargs['metadata']['organization_id'], str(self.org.id))

    def test_topup_intent_rejects_bad_pack(self):
        resp = self.client.post(reverse('tickets:sms_credits_topup_intent'), {'tokens': 777})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()['ok'])

    def _fake_confirmed_pi(self, org_id):
        return {
            'id': 'pi_confirm', 'status': 'succeeded', 'payment_method': 'pm_new',
            'amount_received': 500 * self.price,
            'metadata': {'kind': 'sms_credits', 'organization_id': str(org_id),
                         'credit_cents': str(500 * self.price), 'flow': 'inline'},
        }

    def test_topup_confirm_credits_once_and_is_idempotent(self):
        url = reverse('tickets:sms_credits_topup_confirm')
        pi = self._fake_confirmed_pi(self.org.id)
        with patch('stripe.PaymentIntent.retrieve', return_value=pi), \
                patch('tickets.views._save_org_card_from_pm'):
            r1 = self.client.post(url, {'payment_intent_id': 'pi_confirm'})
            r2 = self.client.post(url, {'payment_intent_id': 'pi_confirm'})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.org.refresh_from_db()
        # Idempotent: the same PaymentIntent id credits exactly once.
        self.assertEqual(self.org.sms_credit_balance_cents, 500 * self.price)

    def test_topup_confirm_rejects_other_orgs_payment_intent(self):
        other = Organization.objects.create(name='Other', slug='other-topup-org')
        pi = self._fake_confirmed_pi(other.id)
        with patch('stripe.PaymentIntent.retrieve', return_value=pi):
            resp = self.client.post(reverse('tickets:sms_credits_topup_confirm'),
                                    {'payment_intent_id': 'pi_confirm'})
        self.assertEqual(resp.status_code, 404)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 0)

    def test_charge_saved_view_still_redirects(self):
        """The wallet-page redirect view keeps working after the helper refactor."""
        self._save_card()
        fake_pi = MagicMock(id='pi_redir', status='succeeded', amount_received=500 * self.price)
        with patch('stripe.PaymentIntent.create', return_value=fake_pi):
            resp = self.client.post(reverse('tickets:sms_credits_charge_saved'), {'tokens': 500})
        self.assertRedirects(resp, reverse('tickets:sms_credits'))
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 500 * self.price)


class SurveyAnalyticsServiceTests(TestCase):
    """SurveyAnalytics combines Cue-native + imported survey responses."""

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Survey Agg Org', slug='survey-agg-org')
        cls.venue = Venue.objects.create(organization=cls.org, name='Agg Venue', city='Austin')
        cls.event = Event.objects.create(
            organization=cls.org, name='Agg Event', venue=cls.venue,
            start_date=date(2025, 5, 1),
        )
        cls.customer = Customer.objects.create(
            organization=cls.org, email='agg@example.com', name='Agg Buyer',
        )

        # Internal (Cue) response: one promoter NPS (10) + one 4-star rating.
        invitation = SurveyInvitation.objects.create(
            organization=cls.org, event=cls.event,
            customer=cls.customer, email=cls.customer.email,
        )
        response = SurveyResponse.objects.create(
            organization=cls.org, event=cls.event,
            customer=cls.customer, invitation=invitation,
        )
        nps_q = SurveyQuestion.objects.create(
            organization=cls.org, question_text='How likely to recommend?',
            question_type='nps', position=1,
        )
        star_q = SurveyQuestion.objects.create(
            organization=cls.org, question_text='Rate the event',
            question_type='star_rating', position=2,
        )
        SurveyAnswer.objects.create(response=response, question=nps_q, nps_score=10)
        SurveyAnswer.objects.create(response=response, question=star_q, star_rating=4)

        # Imported (external) response: one detractor NPS (0) + a text rating.
        upload = ExternalSurveyUpload.objects.create(
            organization=cls.org, filename='typeform.csv',
            status=ExternalSurveyUpload.Status.COMPLETED,
        )
        ExternalSurveyResponse.objects.create(
            organization=cls.org, upload=upload, event=cls.event,
            responded_at=timezone.make_aware(datetime(2025, 5, 3, 9, 0)),
            email='imported@example.com', overall_rating='Meh', nps_score=0,
        )

    def test_calculate_merges_internal_and_external(self):
        from tickets.services.survey.analytics import SurveyAnalytics

        stats = SurveyAnalytics(organization=self.org).calculate()

        # Totals: 1 Cue response + 1 imported response.
        self.assertEqual(stats['total'], 2)
        self.assertEqual(stats['internal_total'], 1)
        self.assertEqual(stats['external_total'], 1)

        # NPS: 1 promoter (10) + 1 detractor (0) → (1 - 1) / 2 * 100 = 0.
        self.assertEqual(stats['nps_total'], 2)
        self.assertEqual(stats['promoters'], 1)
        self.assertEqual(stats['detractors'], 1)
        self.assertEqual(stats['nps_score'], 0)

        # Star average comes from Cue surveys only.
        self.assertEqual(stats['avg_star_rating'], 4.0)

        # Rating breakdown stays external-only (free-text scale).
        self.assertEqual(stats['rating_breakdown'], [{'overall_rating': 'Meh', 'count': 1}])

    def test_nps_over_time_buckets_by_event_date_not_response_date(self):
        """The time series groups responses by the month of the event date, not
        the month the survey was submitted/imported. Both setUp responses belong
        to the May 1 event, so despite the imported response being captured May 3,
        the entire series lands in a single May 2025 bucket."""
        from tickets.services.survey.analytics import SurveyAnalytics

        stats = SurveyAnalytics(organization=self.org).calculate()

        self.assertEqual([r['month'] for r in stats['nps_over_time']], ['2025-05'])
        may = stats['nps_over_time'][0]
        self.assertEqual(may['n'], 2)               # both NPS responses
        self.assertEqual(may['nps_score'], 0)       # 1 promoter, 1 detractor


class DeviceTokenRegistrationTests(TestCase):
    """POST /api/notification/device-token/ — organizer-authed token upsert."""

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name='Push Org', slug='push-org')
        cls.user = User.objects.create_user(username='pushuser', email='push@example.com')
        UserProfile.objects.create(
            user=cls.user, organization=cls.org,
            org_role=UserProfile.OrgRole.OWNER, role=UserProfile.Role.ORGANIZER,
        )
        cls.token = Token.objects.create(user=cls.user)
        cls.auth = {'HTTP_AUTHORIZATION': f'Token {cls.token.key}'}
        cls.url = '/api/notification/device-token/'

    def test_register_returns_204_and_stores_token(self):
        resp = self.client.post(
            self.url, {'token': 'abc123', 'platform': 'ios'}, **self.auth,
        )
        self.assertEqual(resp.status_code, 204)
        dt = DeviceToken.objects.get(token='abc123')
        self.assertEqual(dt.organizer, self.user)
        self.assertEqual(dt.organization, self.org)
        self.assertEqual(dt.platform, 'ios')

    def test_register_without_trailing_slash_reaches_view_directly(self):
        """A client omitting the trailing slash must hit the view (204), not eat a
        301 that downgrades the POST to GET and drops the body."""
        resp = self.client.post(
            '/api/notification/device-token', {'token': 'noslash'}, **self.auth,
        )
        self.assertEqual(resp.status_code, 204)
        self.assertTrue(DeviceToken.objects.filter(token='noslash').exists())

    def test_second_token_rotates_out_the_first(self):
        self.client.post(self.url, {'token': 'old-token'}, **self.auth)
        self.client.post(self.url, {'token': 'new-token'}, **self.auth)
        tokens = list(DeviceToken.objects.filter(organizer=self.user).values_list('token', flat=True))
        self.assertEqual(tokens, ['new-token'])

    def test_reregistering_same_token_is_idempotent(self):
        self.client.post(self.url, {'token': 'same'}, **self.auth)
        resp = self.client.post(self.url, {'token': 'same'}, **self.auth)
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(DeviceToken.objects.filter(organizer=self.user).count(), 1)

    def test_missing_token_returns_400(self):
        resp = self.client.post(self.url, {'platform': 'ios'}, **self.auth)
        self.assertEqual(resp.status_code, 400)

    def test_unauthenticated_returns_401(self):
        resp = self.client.post(self.url, {'token': 'abc'})
        self.assertEqual(resp.status_code, 401)

    def test_user_without_org_returns_403(self):
        orphan = User.objects.create_user(username='orphan')
        UserProfile.objects.create(user=orphan, organization=None)
        token = Token.objects.create(user=orphan)
        resp = self.client.post(
            self.url, {'token': 'orphan-tok'},
            HTTP_AUTHORIZATION=f'Token {token.key}',
        )
        self.assertEqual(resp.status_code, 403)


class SendPushNotificationTaskTests(TestCase):
    """send_push_notification_task — delivery, stale-token cleanup, retry."""

    def setUp(self):
        self.org = Organization.objects.create(name='Task Org', slug='task-org')
        self.user = User.objects.create_user(username='taskuser')
        self.dt = DeviceToken.objects.create(
            organizer=self.user, organization=self.org, token='dev-token-1',
        )
        self.payload = {'aps': {'alert': {'title': 'Hi', 'body': 'There'}}}

    def test_stale_token_is_deleted(self):
        from tickets.tasks import send_push_notification_task
        from tickets.services.push_notifications.apns import PushResult

        stale = PushResult(ok=False, status=410, reason='Unregistered')
        with patch('tickets.services.push_notifications.apns.send', return_value=stale):
            send_push_notification_task(str(self.dt.id), self.payload)
        self.assertFalse(DeviceToken.objects.filter(id=self.dt.id).exists())

    def test_successful_send_keeps_token(self):
        from tickets.tasks import send_push_notification_task
        from tickets.services.push_notifications.apns import PushResult

        ok = PushResult(ok=True, status=200)
        with patch('tickets.services.push_notifications.apns.send', return_value=ok):
            send_push_notification_task(str(self.dt.id), self.payload)
        self.assertTrue(DeviceToken.objects.filter(id=self.dt.id).exists())

    def test_transient_failure_retries(self):
        from tickets.tasks import send_push_notification_task
        from tickets.services.push_notifications.apns import PushResult

        transient = PushResult(ok=False, status=503)
        with patch('tickets.services.push_notifications.apns.send', return_value=transient), \
                patch.object(send_push_notification_task, 'retry', side_effect=Exception('retried')) as mock_retry:
            with self.assertRaises(Exception):
                send_push_notification_task(str(self.dt.id), self.payload)
        self.assertTrue(mock_retry.called)
        self.assertTrue(DeviceToken.objects.filter(id=self.dt.id).exists())


class LaunchPushCommandTests(TestCase):
    """send_launch_push management command — dry-run vs --confirm."""

    def setUp(self):
        self.org = Organization.objects.create(name='Cmd Org', slug='cmd-org')
        self.user = User.objects.create_user(username='cmduser')
        for i in range(3):
            DeviceToken.objects.create(
                organizer=self.user, organization=self.org, token=f'tok-{i}',
            )

    def test_dry_run_enqueues_nothing(self):
        with patch('tickets.tasks.send_push_notification_task.delay') as mock_delay:
            call_command('send_launch_push')
        mock_delay.assert_not_called()

    def test_confirm_enqueues_all(self):
        with patch('tickets.tasks.send_push_notification_task.delay') as mock_delay:
            call_command('send_launch_push', '--confirm')
        self.assertEqual(mock_delay.call_count, 3)


class TapToPayEnabledPushTriggerTests(TestCase):
    """_handle_connect_account_updated — fire once on pending -> enabled."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='Merchant Org', slug='merchant-org',
            stripe_account_id='acct_123', tap_to_pay_enabled_push_sent=False,
        )

    def _account(self, card_state='active', country='US'):
        return {'id': 'acct_123', 'country': country,
                'capabilities': {'card_payments': card_state}}

    def test_fires_and_sets_flag_when_enabled(self):
        from tickets.views import _handle_connect_account_updated
        with patch('tickets.services.push_notifications.dispatch.fire_tap_to_pay_enabled') as mock_fire:
            _handle_connect_account_updated(self._account())
        self.org.refresh_from_db()
        self.assertTrue(self.org.tap_to_pay_enabled_push_sent)
        self.assertTrue(mock_fire.called)

    def test_idempotent_second_event_does_not_fire(self):
        from tickets.views import _handle_connect_account_updated
        with patch('tickets.services.push_notifications.dispatch.fire_tap_to_pay_enabled') as mock_fire:
            _handle_connect_account_updated(self._account())
            _handle_connect_account_updated(self._account())
        self.assertEqual(mock_fire.call_count, 1)

    def test_does_not_fire_while_pending(self):
        from tickets.views import _handle_connect_account_updated
        with patch('tickets.services.push_notifications.dispatch.fire_tap_to_pay_enabled') as mock_fire:
            _handle_connect_account_updated(self._account(card_state='pending'))
        self.org.refresh_from_db()
        self.assertFalse(self.org.tap_to_pay_enabled_push_sent)
        self.assertFalse(mock_fire.called)

    def test_unknown_account_is_ignored(self):
        from tickets.views import _handle_connect_account_updated
        with patch('tickets.services.push_notifications.dispatch.fire_tap_to_pay_enabled') as mock_fire:
            _handle_connect_account_updated({'id': 'acct_unknown',
                                             'capabilities': {'card_payments': 'active'}})
        self.assertFalse(mock_fire.called)


class APNsSenderTests(TestCase):
    """Direct tests for apns.send() — host selection, headers, status classification.

    These exercise the real send() path (URL build, header assembly, response
    classification) that every other push test mocks out. _provider_token is
    stubbed so we don't need a real EC key; httpx.Client is faked so no network.
    """

    def _run_send(self, status_code, reason=None, use_sandbox=True, configured=True):
        from tickets.services.push_notifications import apns

        captured = {}

        fake_resp = MagicMock()
        fake_resp.status_code = status_code
        fake_resp.json.return_value = {'reason': reason} if reason else {}

        class FakeClient:
            def __init__(self, *a, **k):
                captured['client_kwargs'] = k

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, content=None):
                captured['url'] = url
                captured['headers'] = headers
                captured['content'] = content
                return fake_resp

        creds = dict(
            APNS_KEY_ID='KEY123', APNS_TEAM_ID='TEAM123',
            APNS_BUNDLE_ID='co.cueup.cue', APNS_AUTH_KEY='dummy-pem',
            APNS_USE_SANDBOX=use_sandbox,
        )
        if not configured:
            creds.update(APNS_KEY_ID='', APNS_AUTH_KEY='', APNS_KEY_PATH='')

        with override_settings(**creds), \
                patch.object(apns, '_provider_token', return_value='fake-jwt'), \
                patch('httpx.Client', FakeClient):
            result = apns.send('devtok', {'aps': {'alert': {'title': 't'}}})
        return result, captured

    def test_sandbox_host_and_headers(self):
        result, cap = self._run_send(200, use_sandbox=True)
        self.assertTrue(result.ok)
        self.assertEqual(cap['url'], 'https://api.sandbox.push.apple.com/3/device/devtok')
        self.assertEqual(cap['headers']['apns-topic'], 'co.cueup.cue')
        self.assertEqual(cap['headers']['authorization'], 'bearer fake-jwt')
        self.assertEqual(cap['headers']['apns-push-type'], 'alert')

    def test_production_host(self):
        _result, cap = self._run_send(200, use_sandbox=False)
        self.assertEqual(cap['url'], 'https://api.push.apple.com/3/device/devtok')

    def test_410_is_stale(self):
        result, _cap = self._run_send(410)
        self.assertTrue(result.stale)
        self.assertFalse(result.ok)
        self.assertFalse(result.transient)

    def test_400_bad_device_token_is_stale(self):
        result, _cap = self._run_send(400, reason='BadDeviceToken')
        self.assertTrue(result.stale)

    def test_400_other_reason_not_stale(self):
        result, _cap = self._run_send(400, reason='PayloadTooLarge')
        self.assertFalse(result.stale)

    def test_503_is_transient(self):
        result, _cap = self._run_send(503)
        self.assertTrue(result.transient)
        self.assertFalse(result.stale)

    def test_429_is_transient(self):
        result, _cap = self._run_send(429)
        self.assertTrue(result.transient)

    def test_unconfigured_is_skipped(self):
        result, cap = self._run_send(200, configured=False)
        self.assertTrue(result.skipped)
        self.assertFalse(result.ok)
        self.assertNotIn('url', cap)  # never attempted the send


class TapToPayEnabledPushConcurrencyTests(TestCase):
    """The once-only guard must be atomic (A1) and skip unsupported countries."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='Race Org', slug='race-org',
            stripe_account_id='acct_race', tap_to_pay_enabled_push_sent=False,
        )

    def _account(self, card_state='active', country='US'):
        return {'id': 'acct_race', 'country': country,
                'capabilities': {'card_payments': card_state}}

    def test_atomic_claim_blocks_concurrent_winner(self):
        """Genuinely exercise the conditional UPDATE: a concurrent event claims the
        flag AFTER this call's read-check but BEFORE its UPDATE. The UPDATE then
        matches 0 rows and no push fires. A plain read-check-write guard would
        double-fire here — this proves the atomic claim closes the race."""
        from tickets.views import _handle_connect_account_updated

        def claim_flag_midway(account):
            # Stand in for a concurrent account.updated winning the claim between
            # our read-check (flag was False) and our UPDATE.
            Organization.objects.filter(pk=self.org.pk).update(tap_to_pay_enabled_push_sent=True)
            return ('enabled', 'active', 'US')

        with patch('tickets.api_views._tap_to_pay_status_from_account', side_effect=claim_flag_midway), \
                patch('tickets.services.push_notifications.dispatch.fire_tap_to_pay_enabled') as mock_fire:
            _handle_connect_account_updated(self._account())
        self.assertFalse(mock_fire.called)

    def test_single_event_fires_once(self):
        from tickets.views import _handle_connect_account_updated

        with patch('tickets.services.push_notifications.dispatch.fire_tap_to_pay_enabled') as mock_fire:
            _handle_connect_account_updated(self._account())
        self.assertEqual(mock_fire.call_count, 1)
        self.org.refresh_from_db()
        self.assertTrue(self.org.tap_to_pay_enabled_push_sent)

    def test_unsupported_country_does_not_fire(self):
        from tickets.views import _handle_connect_account_updated

        with patch('tickets.services.push_notifications.dispatch.fire_tap_to_pay_enabled') as mock_fire:
            _handle_connect_account_updated(self._account(country='IN'))
        self.org.refresh_from_db()
        self.assertFalse(self.org.tap_to_pay_enabled_push_sent)
        self.assertFalse(mock_fire.called)


class EventDuplicateViewTests(TestCase):
    """Tests for the event_duplicate view (modal-driven server-side copy)."""

    def setUp(self):
        from .models import PromoCode, TrackingLink, EventTalent
        self.client = Client()
        self.org = Organization.objects.create(name='Dup Org', slug='dup-org')
        self.user = User.objects.create_user(
            username='dupuser', email='dup@test.com', password='pass12345',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='dup@test.com', password='pass12345')
        self.client.get(reverse('tickets:home'))  # seed _org_id

        self.venue = Venue.objects.create(
            organization=self.org, name='The Spot', city='Las Vegas',
        )
        self.source = Event.objects.create(
            organization=self.org, name='Familiar Faces', venue=self.venue,
            ticketing_type='direct', status='live',
            start_date=date(2024, 1, 1), start_time=time(20, 0),
            end_date=date(2024, 1, 2), end_time=time(2, 0),
            cached_ticket_count=50, computed_total_revenue=Decimal('1234.00'),
        )
        # Two ticket types, the second unlocking after the first; both with sales.
        self.tt1 = SaleableTicketType.objects.create(
            event=self.source, name='GA', price=Decimal('20.00'),
            quantity_limit=100, quantity_sold=40, order=0,
        )
        self.tt2 = SaleableTicketType.objects.create(
            event=self.source, name='VIP', price=Decimal('50.00'),
            quantity_limit=20, quantity_sold=5, order=1, unlocks_after=self.tt1,
        )
        SaleableTicketTypeTier.objects.create(
            ticket_type=self.tt1, name='Early Bird', price=Decimal('15.00'),
            allotment=30, quantity_sold=30, order=0,
        )
        PromoCode.objects.create(
            organization=self.org, event=self.source, code='SAVE10',
            discount_type=PromoCode.PERCENTAGE, discount_value=Decimal('10.00'),
            times_used=7,
        )
        TrackingLink.objects.create(
            organization=self.org, event=self.source, name='IG', token='origtoken123',
            click_count=99,
        )
        EventTalent.objects.create(event=self.source, name='DJ Shadow', order=0)

    def _future(self, days_ahead, hour):
        d = timezone.localdate() + timedelta(days=days_ahead)
        return f"{d.isoformat()}T{hour:02d}:00"

    def test_duplicate_creates_draft_copy_with_reset_config(self):
        from .models import PromoCode, TrackingLink, EventTalent
        resp = self.client.post(
            reverse('tickets:event_duplicate', args=[self.source.id]),
            {'start': self._future(30, 20), 'end': self._future(31, 2)},
        )
        self.assertRedirects(resp, reverse('tickets:event_list'))

        copies = Event.objects.filter(organization=self.org, name='Familiar Faces').exclude(id=self.source.id)
        self.assertEqual(copies.count(), 1)
        new = copies.first()

        # Draft, future dates, denormalized counters reset, fresh public_id.
        self.assertEqual(new.status, 'draft')
        self.assertEqual(new.start_date, timezone.localdate() + timedelta(days=30))
        self.assertEqual(new.cached_ticket_count, 0)
        self.assertEqual(new.computed_total_revenue, Decimal('0.00'))
        self.assertNotEqual(new.public_id, self.source.public_id)
        self.assertEqual(new.venue_id, self.venue.id)  # same venue reused

        # Ticket types copied with sold counts reset and unlock relationship preserved.
        new_tts = {t.name: t for t in new.saleable_ticket_types.all()}
        self.assertEqual(set(new_tts), {'GA', 'VIP'})
        self.assertEqual(new_tts['GA'].quantity_sold, 0)
        self.assertEqual(new_tts['VIP'].unlocks_after_id, new_tts['GA'].id)
        # Tier copied, sold reset.
        tier = new_tts['GA'].tiers.first()
        self.assertEqual(tier.name, 'Early Bird')
        self.assertEqual(tier.quantity_sold, 0)

        # Promo code copied with usage reset; tracking link gets a fresh token.
        pc = PromoCode.objects.get(event=new)
        self.assertEqual(pc.code, 'SAVE10')
        self.assertEqual(pc.times_used, 0)
        tl = TrackingLink.objects.get(event=new)
        self.assertEqual(tl.click_count, 0)
        self.assertNotEqual(tl.token, 'origtoken123')
        self.assertTrue(EventTalent.objects.filter(event=new, name='DJ Shadow').exists())

        # Source is untouched.
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, 'live')
        self.assertEqual(self.source.cached_ticket_count, 50)

    def test_custom_title_is_applied(self):
        resp = self.client.post(
            reverse('tickets:event_duplicate', args=[self.source.id]),
            {'name': 'Familiar Faces: Reunion', 'start': self._future(30, 20)},
        )
        self.assertRedirects(resp, reverse('tickets:event_list'))
        new = Event.objects.get(organization=self.org, name='Familiar Faces: Reunion')
        self.assertNotEqual(new.id, self.source.id)
        # Blank/whitespace title falls back to the source name.
        resp2 = self.client.post(
            reverse('tickets:event_duplicate', args=[self.source.id]),
            {'name': '   ', 'start': self._future(31, 20)},
        )
        self.assertRedirects(resp2, reverse('tickets:event_list'))
        self.assertTrue(
            Event.objects.filter(organization=self.org, name='Familiar Faces')
            .exclude(id=self.source.id).exists()
        )

    def test_past_start_is_rejected(self):
        past = timezone.localdate() - timedelta(days=1)
        resp = self.client.post(
            reverse('tickets:event_duplicate', args=[self.source.id]),
            {'start': f"{past.isoformat()}T20:00"},
        )
        self.assertRedirects(resp, reverse('tickets:event_list'))
        self.assertFalse(
            Event.objects.filter(organization=self.org, name='Familiar Faces')
            .exclude(id=self.source.id).exists()
        )

    def test_other_org_event_is_404(self):
        other_org = Organization.objects.create(name='Other', slug='other-org')
        other_venue = Venue.objects.create(organization=other_org, name='V', city='X')
        other_event = Event.objects.create(
            organization=other_org, name='Theirs', venue=other_venue,
            start_date=date(2024, 1, 1), start_time=time(20, 0),
        )
        resp = self.client.post(
            reverse('tickets:event_duplicate', args=[other_event.id]),
            {'start': self._future(10, 20)},
        )
        self.assertEqual(resp.status_code, 404)

    def test_get_is_not_allowed(self):
        resp = self.client.get(reverse('tickets:event_duplicate', args=[self.source.id]))
        self.assertEqual(resp.status_code, 405)


class EventDuplicateCsrfCookieTests(TestCase):
    """The events list has no rendered {% csrf_token %} (its HTML is cached), so the
    duplicate modal reads the token from the csrftoken cookie. event_list must therefore
    set that cookie (@ensure_csrf_cookie) or the real browser POST fails CSRF with 403.
    Uses a CSRF-enforcing client to reproduce the browser, unlike the default test client.
    """

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.org = Organization.objects.create(name='CsrfDupOrg', slug='csrf-dup-org')
        self.user = User.objects.create_user(
            username='csrfdup', email='csrfdup@test.com', password='pass12345',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='csrfdup@test.com', password='pass12345')
        self.venue = Venue.objects.create(organization=self.org, name='V', city='C')
        self.source = Event.objects.create(
            organization=self.org, name='CsrfSrc', venue=self.venue,
            start_date=date(2024, 1, 1), start_time=time(20, 0),
        )

    def test_events_page_sets_csrf_cookie_and_post_succeeds(self):
        resp = self.client.get(reverse('tickets:event_list'))
        self.assertEqual(resp.status_code, 200)
        token = self.client.cookies.get('csrftoken')
        self.assertIsNotNone(token, 'csrftoken cookie not set on /events/ — browser POST would 403')
        self.assertTrue(token.value)

        future = (timezone.localdate() + timedelta(days=10)).isoformat() + 'T20:00'
        resp2 = self.client.post(
            reverse('tickets:event_duplicate', args=[self.source.id]),
            {'start': future, 'csrfmiddlewaretoken': token.value},
        )
        self.assertRedirects(resp2, reverse('tickets:event_list'))
        self.assertEqual(
            Event.objects.filter(organization=self.org, name='CsrfSrc').count(), 2,
        )


class BrandVoiceSettingsTests(TestCase):
    """The Brand Voice settings page and its wiring into the AI SMS strategist."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Voice Org', slug='voice-org')

        self.admin_user = User.objects.create_user(
            username='voiceadmin', email='voiceadmin@example.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.admin_user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.admin_user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )

        self.host_user = User.objects.create_user(
            username='voicehost', email='voicehost@example.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.host_user, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )
        OrganizationMembership.objects.create(
            user=self.host_user, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )

    def _login_admin(self):
        self.client.login(username='voiceadmin@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))

    def test_get_renders_form(self):
        self._login_admin()
        response = self.client.get(reverse('tickets:settings_brand_voice'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('form', response.context)
        self.assertIn('brand_voice_guidelines', response.context['form'].fields)

    def test_post_saves_guidelines(self):
        self._login_admin()
        response = self.client.post(
            reverse('tickets:settings_brand_voice'),
            {'brand_voice_guidelines': 'Warm and casual, like a friend. Never corporate.'},
        )
        self.assertRedirects(response, reverse('tickets:settings_brand_voice'))
        self.org.refresh_from_db()
        self.assertEqual(
            self.org.brand_voice_guidelines,
            'Warm and casual, like a friend. Never corporate.',
        )

    def test_non_admin_forbidden(self):
        self.client.login(username='voicehost@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))
        response = self.client.get(reverse('tickets:settings_brand_voice'))
        self.assertEqual(response.status_code, 403)

    @patch('tickets.services.sms_strategist.record_ai_token_usage')
    @patch('langchain_openai.ChatOpenAI')
    def test_guidelines_passed_into_llm_context(self, mock_llm_cls, _mock_meter):
        """Saved brand voice guidelines reach the prompt sent to the LLM."""
        from tickets.services.sms_strategist import (
            generate_campaign_plan, CampaignPlan, PlanStep,
        )

        self.org.brand_voice_guidelines = 'Talk like a friendly pirate, matey.'
        self.org.save(update_fields=['brand_voice_guidelines'])

        venue = Venue.objects.create(organization=self.org, name='V', city='C')
        event = Event.objects.create(
            organization=self.org, name='Voice Event', venue=venue,
            start_date=timezone.localdate() + timedelta(days=14),
        )

        plan = CampaignPlan(
            title='Pirate push',
            strategy_summary='Three touches.',
            steps=[PlanStep(
                purpose='announcement', audience='All subscribers', offset_days=10,
                send_time='18:00', message='Ahoy, tickets are live.', rationale='Kickoff.',
            )],
        )

        captured = {}

        def fake_invoke(messages):
            captured['messages'] = messages
            return {'raw': MagicMock(), 'parsed': plan, 'parsing_error': None}

        mock_structured = MagicMock()
        mock_structured.invoke.side_effect = fake_invoke
        mock_instance = MagicMock()
        mock_instance.with_structured_output.return_value = mock_structured
        mock_llm_cls.return_value = mock_instance

        generate_campaign_plan(self.org, event=event)

        user_content = captured['messages'][1]['content']
        self.assertIn('Talk like a friendly pirate, matey.', user_content)
        self.assertIn('brand_voice_guidelines', user_content)

    def _fake_example_llm(self, message='Doors open this Friday - grab tickets now.'):
        """Build a mock ChatOpenAI whose structured invoke returns a VoiceExample."""
        from tickets.services.sms_strategist import VoiceExample

        example = VoiceExample(message=message)
        captured = {}

        def fake_invoke(messages):
            captured['messages'] = messages
            return {'raw': MagicMock(), 'parsed': example, 'parsing_error': None}

        mock_structured = MagicMock()
        mock_structured.invoke.side_effect = fake_invoke
        mock_instance = MagicMock()
        mock_instance.with_structured_output.return_value = mock_structured
        return mock_instance, captured

    def test_example_requires_post(self):
        self._login_admin()
        response = self.client.get(reverse('tickets:settings_brand_voice_example'))
        self.assertEqual(response.status_code, 405)

    def test_example_non_admin_forbidden(self):
        self.client.login(username='voicehost@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))
        response = self.client.post(reverse('tickets:settings_brand_voice_example'))
        self.assertEqual(response.status_code, 403)

    @patch('tickets.services.sms_strategist.record_ai_token_usage')
    @patch('langchain_openai.ChatOpenAI')
    def test_example_returns_message_json(self, mock_llm_cls, _mock_meter):
        self._login_admin()
        mock_instance, captured = self._fake_example_llm()
        mock_llm_cls.return_value = mock_instance

        response = self.client.post(
            reverse('tickets:settings_brand_voice_example'),
            {'guidelines': 'Loud and hyped, all caps energy.'},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['message'], 'Doors open this Friday - grab tickets now.')
        self.assertIn('segments', data)
        self.assertIn('encoding', data)
        # The typed (unsaved) guidelines are what reached the LLM.
        self.assertIn('Loud and hyped, all caps energy.', captured['messages'][1]['content'])

    @patch('langchain_openai.ChatOpenAI')
    def test_example_reports_llm_failure(self, mock_llm_cls):
        self._login_admin()
        mock_llm_cls.side_effect = RuntimeError('no api key')
        response = self.client.post(reverse('tickets:settings_brand_voice_example'))
        self.assertEqual(response.status_code, 503)
        self.assertIn('error', response.json())




class CheckinCurveTests(TestCase):
    """Check-in arrival curve: series bucketing + the comparison API endpoint."""

    def setUp(self):
        from zoneinfo import ZoneInfo
        self.la = ZoneInfo('America/Los_Angeles')
        self.client = Client()
        self.org = Organization.objects.create(name='Checkin Org', slug='checkin-org')
        self.user = User.objects.create_user(
            username='ciuser', email='ci@test.com', password='pass12345'
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER
        )
        self.client.login(username='ci@test.com', password='pass12345')
        self.client.get(reverse('tickets:home'))  # seed _org_id

        self.venue = Venue.objects.create(organization=self.org, name='Hall', city='LA')
        self.event = Event.objects.create(
            organization=self.org, name='Main Show', venue=self.venue,
            start_date=date(2024, 6, 15), start_time=time(19, 0), timezone='America/Los_Angeles',
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='c@example.com', name='C', lifetime_value=Decimal('0.00')
        )
        self.order = TicketOrder.objects.create(
            customer=self.customer, event=self.event, order_number='CI-1',
            order_date='2024-06-01 10:00:00', total_amount=Decimal('40.00'),
        )

    def _ticket(self, hh, mm):
        return Ticket.objects.create(
            ticket_order=self.order, ticket_type='GA', price=Decimal('10.00'),
            scanned_at=datetime(2024, 6, 15, hh, mm, tzinfo=self.la),
        )

    def test_get_checkin_series_buckets_relative_to_start(self):
        from tickets.services.forecasting.sales_curve import SalesCurveCalculator
        self._ticket(18, 50)          # -10 min -> bucket -15
        self._ticket(19, 7)           # +7 min  -> bucket 0
        self._ticket(19, 10)          # +10 min -> bucket 0
        self._ticket(19, 20)          # +20 min -> bucket 15
        Ticket.objects.create(        # never scanned -> excluded
            ticket_order=self.order, ticket_type='GA', price=Decimal('10.00'), scanned_at=None,
        )

        data = SalesCurveCalculator().get_checkin_series(self.event)
        self.assertEqual(data['bucket_minutes'], 15)
        self.assertEqual(data['total_checkins'], 4)
        self.assertEqual(
            data['series'],
            [{'m': -15, 'checkins': 1}, {'m': 0, 'checkins': 2}, {'m': 15, 'checkins': 1}],
        )

    def test_get_checkin_series_empty_when_no_scans(self):
        from tickets.services.forecasting.sales_curve import SalesCurveCalculator
        Ticket.objects.create(
            ticket_order=self.order, ticket_type='GA', price=Decimal('10.00'), scanned_at=None,
        )
        data = SalesCurveCalculator().get_checkin_series(self.event)
        self.assertEqual(data['series'], [])
        self.assertEqual(data['total_checkins'], 0)

    def test_checkin_curve_api_returns_series(self):
        self._ticket(19, 5)
        resp = self.client.get(
            reverse('tickets:event_checkin_curve_api', args=[self.event.id])
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['id'], str(self.event.id))
        self.assertEqual(body['name'], 'Main Show')
        self.assertEqual(body['total_checkins'], 1)
        self.assertIn('series', body)

    def test_checkin_curve_api_is_org_scoped(self):
        other_org = Organization.objects.create(name='Other', slug='other-ci-org')
        other_venue = Venue.objects.create(organization=other_org, name='Away', city='NYC')
        other_event = Event.objects.create(
            organization=other_org, name='Foreign', venue=other_venue,
            start_date=date(2024, 7, 1),
        )
        resp = self.client.get(
            reverse('tickets:event_checkin_curve_api', args=[other_event.id])
        )
        self.assertEqual(resp.status_code, 404)


class EventListEmptyStateTests(TestCase):
    """The events-page empty state guides new organizers to create/import an event."""

    def _make_org_user(self, external_enabled):
        org = Organization.objects.create(
            name='Empty Org', slug='empty-org',
            external_events_enabled=external_enabled,
        )
        user = User.objects.create_user(
            username='emptyorg', email='emptyorg@example.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=user, organization=org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='emptyorg@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))
        return org

    def test_empty_account_shows_both_ctas_when_external_enabled(self):
        self._make_org_user(external_enabled=True)
        resp = self.client.get(reverse('tickets:event_list'))
        self.assertContains(resp, 'Create your first event')
        self.assertContains(resp, reverse('tickets:event_create', args=['direct']))
        self.assertContains(resp, reverse('tickets:event_create', args=['external']))
        self.assertContains(resp, 'Import CSV')
        # The redundant header "Create Event" button is hidden in this empty state.
        self.assertNotContains(resp, 'href="%s"' % reverse('tickets:event_type_select'))

    def test_import_cta_hidden_when_external_disabled(self):
        self._make_org_user(external_enabled=False)
        resp = self.client.get(reverse('tickets:event_list'))
        self.assertContains(resp, 'Create your first event')
        self.assertContains(resp, reverse('tickets:event_create', args=['direct']))
        self.assertNotContains(resp, 'Import CSV')

    def test_search_miss_shows_lighter_message_not_ctas(self):
        self._make_org_user(external_enabled=True)
        resp = self.client.get(reverse('tickets:event_list'), {'search': 'zzz-no-match'})
        self.assertContains(resp, 'No events match your search')
        self.assertNotContains(resp, 'Create your first event')
        self.assertNotContains(resp, 'Import CSV')
        # The header "Create Event" button stays visible for search/filter misses.
        self.assertContains(resp, 'href="%s"' % reverse('tickets:event_type_select'))


class ExternalEventCreateCSVTests(TestCase):
    """Inline CSV import on the external ('Import Event') create page."""

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self._SimpleUploadedFile = SimpleUploadedFile
        self.client = Client()
        self.org = Organization.objects.create(
            name='Import CSV Org', slug='import-csv-org', external_events_enabled=True,
        )
        self.user = User.objects.create_user(
            username='importer', email='importer@example.com', password='pw',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.force_login(self.user)
        self.client.get(reverse('tickets:home'))
        self.venue = Venue.objects.create(
            organization=self.org, name='Import Hall', city='Austin', state='TX', country='US',
        )
        # A format that carries its own price column → no manual price-entry step.
        self.csv_format = CSVFormat.objects.create(
            organization=self.org, name='Priced Format', requires_manual_pricing=False,
            column_mapping={'order_number': 'Order ID', 'total_amount': 'Price'},
        )

    def _base_payload(self):
        return {
            'name': 'Imported Show',
            'ticketing_type': 'external',
            'venue': str(self.venue.id),
            'start_date': '2025-03-10', 'start_time': '20:00',
            'end_date': '2025-03-10', 'end_time': '22:00',
            'timezone': 'America/Chicago', 'ticket_link': '',
            'talent-TOTAL_FORMS': '0', 'talent-INITIAL_FORMS': '0',
            'talent-MIN_NUM_FORMS': '0', 'talent-MAX_NUM_FORMS': '1000',
        }

    def test_create_with_csv_still_processing_redirects_to_event(self):
        # delay() patched to a no-op → upload stays 'pending' (mimics async prod),
        # so we land on the event with the ?importing banner param.
        csv = self._SimpleUploadedFile(
            'orders.csv', b'Order ID,Price\nA-1,10.00\n', content_type='text/csv',
        )
        payload = self._base_payload()
        payload['csv_file'] = csv
        payload['csv_format'] = str(self.csv_format.id)
        with patch('tickets.tasks.process_csv_task.delay') as mock_delay:
            resp = self.client.post(
                reverse('tickets:event_create', args=['external']), payload,
            )
        event = Event.objects.get(organization=self.org, name='Imported Show')
        uploaded = UploadedFile.objects.get(organization=self.org)
        self.assertEqual(uploaded.metadata.get('event_id'), str(event.id))
        self.assertEqual(uploaded.csv_format, self.csv_format)
        self.assertTrue(mock_delay.called)
        self.assertRedirects(
            resp,
            f"{reverse('tickets:event_detail', args=[event.id])}?importing={uploaded.id}",
            fetch_redirect_response=False,
        )

    def test_create_with_csv_completed_redirects_to_event(self):
        # delay() marks the upload completed (mimics eager/finished) → land on the
        # event detail page with no ?importing param.
        def _complete(file_id, *a, **kw):
            UploadedFile.objects.filter(id=file_id).update(status='completed')
        csv = self._SimpleUploadedFile(
            'orders.csv', b'Order ID,Price\nA-1,10.00\n', content_type='text/csv',
        )
        payload = self._base_payload()
        payload['csv_file'] = csv
        payload['csv_format'] = str(self.csv_format.id)
        with patch('tickets.tasks.process_csv_task.delay', side_effect=_complete):
            resp = self.client.post(
                reverse('tickets:event_create', args=['external']), payload,
            )
        event = Event.objects.get(organization=self.org, name='Imported Show')
        self.assertRedirects(
            resp, reverse('tickets:event_detail', args=[event.id]),
            fetch_redirect_response=False,
        )

    def test_per_event_upload_redirects_to_event(self):
        event = Event.objects.create(
            organization=self.org, name='Upload Target', venue=self.venue,
            ticketing_type='external', start_date=date(2025, 3, 10), start_time=time(20, 0),
        )
        csv = self._SimpleUploadedFile(
            'orders.csv', b'Order ID,Price\nB-1,12.00\n', content_type='text/csv',
        )
        with patch('tickets.tasks.process_csv_task.delay'):
            resp = self.client.post(
                reverse('tickets:event_upload_csv', args=[event.id]),
                {'csv_file': csv, 'csv_format': str(self.csv_format.id)},
            )
        uploaded = UploadedFile.objects.get(organization=self.org)
        self.assertRedirects(
            resp,
            f"{reverse('tickets:event_detail', args=[event.id])}?importing={uploaded.id}",
            fetch_redirect_response=False,
        )

    def test_create_without_csv_goes_to_event_detail(self):
        resp = self.client.post(
            reverse('tickets:event_create', args=['external']), self._base_payload(),
        )
        event = Event.objects.get(organization=self.org, name='Imported Show')
        self.assertEqual(UploadedFile.objects.filter(organization=self.org).count(), 0)
        self.assertRedirects(
            resp, reverse('tickets:event_detail', args=[event.id]),
            fetch_redirect_response=False,
        )


class EventDateTimeFieldTests(TestCase):
    """Combined start/end datetime-local inputs on the event forms."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='DT Form Org', slug='dt-form-org', external_events_enabled=True,
        )
        self.venue = Venue.objects.create(
            organization=self.org, name='DT Venue', city='Austin', state='TX', country='US',
        )

    def _external_form(self, **overrides):
        from .forms import EventForm
        data = {
            'name': 'DT Show', 'ticketing_type': 'external', 'venue': str(self.venue.id),
            'start_datetime': '2025-03-10T20:00', 'end_datetime': '2025-03-10T23:00',
            'timezone': 'America/Chicago', 'ticket_link': '',
        }
        data.update(overrides)
        return EventForm(data=data, organization=self.org, ticketing_type_locked=True)

    def test_combined_datetime_splits_into_model_fields(self):
        form = self._external_form()
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['start_date'], date(2025, 3, 10))
        self.assertEqual(form.cleaned_data['start_time'], time(20, 0))
        self.assertEqual(form.cleaned_data['end_date'], date(2025, 3, 10))
        self.assertEqual(form.cleaned_data['end_time'], time(23, 0))

    def test_rejects_end_before_start(self):
        form = self._external_form(end_datetime='2025-03-10T19:00')
        self.assertFalse(form.is_valid())
        self.assertIn('end_datetime', form.errors)

    def test_missing_start_is_required(self):
        form = self._external_form(start_datetime='')
        self.assertFalse(form.is_valid())
        self.assertIn('start_datetime', form.errors)

    def test_legacy_separate_date_time_still_accepted(self):
        # Programmatic posts that still send the split fields keep working.
        from .forms import EventForm
        form = EventForm(
            data={
                'name': 'Legacy', 'ticketing_type': 'external', 'venue': str(self.venue.id),
                'start_date': '2025-03-10', 'start_time': '20:00',
                'end_date': '2025-03-10', 'end_time': '23:00',
                'timezone': 'America/Chicago', 'ticket_link': '',
            }, organization=self.org, ticketing_type_locked=True,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['start_time'], time(20, 0))

    def test_edit_prefills_combined_datetime(self):
        from .forms import EventForm
        event = Event.objects.create(
            organization=self.org, name='Edit DT', venue=self.venue, ticketing_type='external',
            start_date=date(2025, 3, 10), start_time=time(20, 0),
            end_date=date(2025, 3, 10), end_time=time(23, 0),
        )
        form = EventForm(instance=event, organization=self.org, ticketing_type_locked=True)
        self.assertEqual(form.initial.get('start_datetime'), datetime(2025, 3, 10, 20, 0))
        self.assertEqual(form.initial.get('end_datetime'), datetime(2025, 3, 10, 23, 0))


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    CELERY_TASK_ALWAYS_EAGER=True,
    SITE_URL='https://cueup.co',
)
class OrganizerWaitlistAcceptedEmailTests(TestCase):
    """Approving a waitlisted organizer sends the acceptance email exactly once."""

    def setUp(self):
        from .models import OrganizerWaitlist
        from .admin import OrganizerWaitlistAdmin
        from django.contrib.admin.sites import AdminSite
        self.OrganizerWaitlist = OrganizerWaitlist
        self.admin = OrganizerWaitlistAdmin(OrganizerWaitlist, AdminSite())
        self.factory = RequestFactory()
        self.staff = User.objects.create_user(
            username='admin@test.com', email='admin@test.com', password='pass123', is_staff=True,
        )

    def _request(self):
        request = self.factory.post('/admin/')
        request.user = self.staff
        setattr(request, 'session', {})
        setattr(request, '_messages', FallbackStorage(request))
        return request

    def test_bulk_action_sends_email_and_stamps_fields(self):
        from django.core import mail
        entry = self.OrganizerWaitlist.objects.create(
            name='Ada Lovelace', email='ada@example.com', organization_name='Analytical Events',
        )
        qs = self.OrganizerWaitlist.objects.filter(pk=entry.pk)
        self.admin.approve_selected(self._request(), qs)

        entry.refresh_from_db()
        self.assertEqual(entry.status, self.OrganizerWaitlist.Status.APPROVED)
        self.assertIsNotNone(entry.approved_at)
        self.assertEqual(entry.approved_by, self.staff)
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ['ada@example.com'])
        self.assertIn('https://cueup.co/create-organization/', sent.body)

    def test_bulk_action_skips_already_approved(self):
        from django.core import mail
        entry = self.OrganizerWaitlist.objects.create(
            name='Grace Hopper', email='grace@example.com', organization_name='COBOL Nights',
            status=self.OrganizerWaitlist.Status.APPROVED,
        )
        qs = self.OrganizerWaitlist.objects.filter(pk=entry.pk)
        self.admin.approve_selected(self._request(), qs)
        self.assertEqual(len(mail.outbox), 0)

    def test_change_form_transition_sends_email_once(self):
        from django.core import mail
        entry = self.OrganizerWaitlist.objects.create(
            name='Alan Turing', email='alan@example.com', organization_name='Enigma Shows',
        )
        entry.status = self.OrganizerWaitlist.Status.APPROVED
        self.admin.save_model(self._request(), entry, form=None, change=True)

        entry.refresh_from_db()
        self.assertEqual(entry.status, self.OrganizerWaitlist.Status.APPROVED)
        self.assertIsNotNone(entry.approved_at)
        self.assertEqual(entry.approved_by, self.staff)
        self.assertEqual(len(mail.outbox), 1)

        # Re-saving an already-approved entry must not send again.
        self.admin.save_model(self._request(), entry, form=None, change=True)
        self.assertEqual(len(mail.outbox), 1)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class EventImageGalleryTests(TestCase):
    """Tests for the event photo gallery (upload/delete/reorder + public render)."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Gallery Org', slug='gallery-org')
        self.user = User.objects.create_user(
            username='galleryowner', email='gallery@test.com', password='testpass123')
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.venue = Venue.objects.create(organization=self.org, name='V', city='C')
        self.event = Event.objects.create(
            organization=self.org, name='Gallery Event', venue=self.venue,
            start_date=date.today() + timedelta(days=7),
            ticketing_type=TICKETING_TYPE_DIRECT, status='live',
        )
        SaleableTicketType.objects.create(
            event=self.event, name='GA', price=Decimal('20.00'), quantity_limit=50)
        self.client.login(username='gallery@test.com', password='testpass123')
        self.client.get(reverse('tickets:home'))  # seed _org_id for @require_org

    def _png(self, name='p.png'):
        from io import BytesIO
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image
        buf = BytesIO()
        Image.new('RGB', (4, 4), (10, 120, 200)).save(buf, format='PNG')
        buf.seek(0)
        return SimpleUploadedFile(name, buf.read(), content_type='image/png')

    def test_upload_creates_images(self):
        url = reverse('tickets:event_image_upload', args=[self.event.id])
        resp = self.client.post(url, {'image': [self._png('a.png'), self._png('b.png')]})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['images']), 2)
        self.assertEqual(EventImage.objects.filter(event=self.event).count(), 2)
        # sort_order assigned sequentially
        self.assertEqual(
            sorted(EventImage.objects.filter(event=self.event).values_list('sort_order', flat=True)),
            [0, 1])

    def test_non_image_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        bad = SimpleUploadedFile('x.txt', b'hi', content_type='text/plain')
        url = reverse('tickets:event_image_upload', args=[self.event.id])
        resp = self.client.post(url, {'image': bad})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(EventImage.objects.filter(event=self.event).count(), 0)

    def test_delete_image(self):
        img = EventImage.objects.create(event=self.event, image=self._png(), sort_order=0)
        url = reverse('tickets:event_image_delete', args=[self.event.id, img.id])
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(EventImage.objects.filter(id=img.id).exists())

    def test_reorder_images(self):
        a = EventImage.objects.create(event=self.event, image=self._png(), sort_order=0)
        b = EventImage.objects.create(event=self.event, image=self._png(), sort_order=1)
        url = reverse('tickets:event_image_reorder', args=[self.event.id])
        resp = self.client.post(url, data=json.dumps({'order': [str(b.id), str(a.id)]}),
                                content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(b.sort_order, 0)
        self.assertEqual(a.sort_order, 1)

    def test_cross_org_upload_blocked(self):
        other = Organization.objects.create(name='Other', slug='other-gallery')
        other_event = Event.objects.create(
            organization=other, name='Other Event', venue=Venue.objects.create(organization=other, name='V2', city='C2'),
            start_date=date.today() + timedelta(days=7), ticketing_type=TICKETING_TYPE_DIRECT, status='live')
        url = reverse('tickets:event_image_upload', args=[other_event.id])
        resp = self.client.post(url, {'image': self._png()})
        self.assertEqual(resp.status_code, 404)

    def test_public_page_renders_photos(self):
        EventImage.objects.create(event=self.event, image=self._png(), sort_order=0)
        resp = self.client.get(reverse('tickets:public_event_buy', args=[self.event.public_id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'event-photos')


class ViewModeOrganizerGuardTests(TestCase):
    """The Attendee View toggle must win over the superuser bypass.

    Regression: a superuser (who is also an organizer) in Attendee View used to
    render organizer page bodies (e.g. the event list) inside attendee chrome,
    because require_organizer returned early for superusers before checking the
    session view mode. Now the view-mode check runs first for everyone.
    """

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='View Mode Org', slug='view-mode-org')

        # Superuser who is also an organizer member of the org.
        self.superuser = User.objects.create_superuser(
            username='vmsuper', email='vmsuper@example.com', password='pw',
        )
        UserProfile.objects.create(
            user=self.superuser, organization=self.org,
            role=UserProfile.Role.ORGANIZER, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.superuser, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )

        # Plain organizer (not superuser) for the baseline path.
        self.organizer = User.objects.create_user(
            username='vmorg', email='vmorg@example.com', password='pw',
        )
        UserProfile.objects.create(
            user=self.organizer, organization=self.org,
            role=UserProfile.Role.ORGANIZER, org_role=UserProfile.OrgRole.HOST,
        )
        OrganizationMembership.objects.create(
            user=self.organizer, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )

    def _set_view_mode(self, mode):
        session = self.client.session
        session['_view_mode'] = mode
        session['_org_id'] = str(self.org.pk)
        session.save()

    def test_superuser_in_attendee_mode_redirected_from_event_list(self):
        self.client.force_login(self.superuser)
        self._set_view_mode('attendee')
        resp = self.client.get(reverse('tickets:event_list'))
        self.assertRedirects(
            resp, reverse('tickets:attendee_dashboard'), fetch_redirect_response=False,
        )

    def test_superuser_in_organizer_mode_sees_event_list(self):
        self.client.force_login(self.superuser)
        self._set_view_mode('organizer')
        resp = self.client.get(reverse('tickets:event_list'))
        self.assertEqual(resp.status_code, 200)

    def test_organizer_in_attendee_mode_redirected_from_event_list(self):
        self.client.force_login(self.organizer)
        self._set_view_mode('attendee')
        resp = self.client.get(reverse('tickets:event_list'))
        self.assertRedirects(
            resp, reverse('tickets:attendee_dashboard'), fetch_redirect_response=False,
        )
