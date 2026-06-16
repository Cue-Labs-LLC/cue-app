import uuid
import json
from datetime import date, time, datetime, timedelta
from unittest.mock import patch, MagicMock
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from decimal import Decimal
from rest_framework.authtoken.models import Token
from .models import (
    UploadedFile, TicketOrder, Customer, CustomerTag, Event,
    Venue, CSVFormat, Ticket, TicketTier,
    Organization, UserProfile, OrganizationMembership, OrganizationInvitation, ChatMessage,
    AITokenUsage, AIRecommendation,
    EventExpense,
    SaleableTicketType, SaleableTicketTypeTier, StripeCheckoutSession, FeatureFlagSettings,
    SurveyInvitation, SurveyResponse, SurveyAnswer, SurveyQuestion, Payout,
    ExternalSurveyUpload, ExternalSurveyResponse, EventDailyPageView,
    LoyaltyProgram, LoyaltyTier, LoyaltyPointsTransaction,
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


class ChatTestMixin:
    """Shared setup for chat tests: creates org, user, profile, venue, event, customer, order."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Test Org', slug='test-org')
        self.user = User.objects.create_user(
            username='chatuser', email='chat@test.com', password='testpass123'
        )
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='chat@test.com', password='testpass123')
        # Hit any org-required view to seed the session with _org_id
        self.client.get(reverse('tickets:home'))

        self.venue = Venue.objects.create(
            organization=self.org, name='Test Venue', city='Test City'
        )
        self.event = Event.objects.create(
            organization=self.org, name='Summer Fest',
            venue=self.venue, start_date=date(2025, 7, 15)
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='alice@example.com',
            name='Alice Smith', lifetime_value=Decimal('500.00'),
            rfm_segment='VIP',
        )
        self.csv_format = CSVFormat.objects.create(
            organization=self.org, name='Chat Test Format',
            column_mapping={'order_number': 'Order ID'},
        )
        self.upload = UploadedFile.objects.create(
            organization=self.org, csv_format=self.csv_format,
            filename='test.csv', status='completed',
        )
        self.order = TicketOrder.objects.create(
            customer=self.customer, event=self.event,
            uploaded_file=self.upload,
            order_number='CHAT-001',
            order_date='2025-07-10 10:00:00',
            total_amount=Decimal('500.00'),
        )
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

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(
            name='API Test Org', slug='api-test-org',
            stripe_account_id='acct_test_api',
            stripe_onboarding_complete=True,
        )
        self.user = User.objects.create_user(
            username='apiuser',
            email='api@example.com',
            password='apipass123',
        )
        UserProfile.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
            role=UserProfile.Role.ORGANIZER,
        )
        self.token = Token.objects.create(user=self.user)
        self.auth_header = {'HTTP_AUTHORIZATION': f'Token {self.token.key}'}

        self.venue = Venue.objects.create(
            organization=self.org, name='Test Venue', city='Portland'
        )
        self.event = Event.objects.create(
            organization=self.org,
            name='Test Event',
            venue=self.venue,
            start_date=date.today() + timedelta(days=7),
        )
        self.customer = Customer.objects.create(
            organization=self.org,
            email='buyer@example.com',
            name='Test Buyer',
        )
        self.order = TicketOrder.objects.create(
            customer=self.customer,
            event=self.event,
            order_number='TEST-001',
            order_date=timezone.now(),
            total_amount=Decimal('25.00'),
        )
        Ticket.objects.create(
            ticket_order=self.order,
            ticket_type='General Admission',
            price=Decimal('25.00'),
        )

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

    def test_token_auth_required(self):
        response = self.client.get('/api/organizer/events/')
        self.assertEqual(response.status_code, 401)


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

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(
            name='Finance Test Org',
            slug='finance-test-org',
            stripe_account_id='acct_123',
            stripe_onboarding_complete=True,
        )
        self.user = User.objects.create_user(
            username='finance-owner',
            email='owner@example.com',
            password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
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

    def setUp(self):
        self.org = Organization.objects.create(name='Chunk Test Org', slug='chunk-test-org')
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org,
            name='Chunk Test Event',
            venue=self.venue,
            start_date=date(2024, 6, 15),
        )
        self.csv_format = CSVFormat.objects.create(
            organization=self.org,
            name='Chunk Test Format',
            column_mapping={
                'order_date': ['order_date'],
                'customer_email': ['customer_email'],
                'customer_name': ['customer_name'],
                'ticket_type': ['ticket_type'],
            },
        )
        self.upload = UploadedFile.objects.create(
            organization=self.org,
            csv_format=self.csv_format,
            filename='chunk_test.csv',
            status='pending',
            metadata={'event_id': str(self.event.id), 'event_name': self.event.name,
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

    def setUp(self):
        from datetime import date
        self.org = Organization.objects.create(name='RFM Test Org', slug='rfm-test-org')
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Event', venue=self.venue,
            start_date=date(2025, 1, 1),
        )
        self.csv_format = CSVFormat.objects.create(
            organization=self.org, name='Fmt', column_mapping={'order_number': 'Order ID'},
        )
        self.upload = UploadedFile.objects.create(
            organization=self.org, csv_format=self.csv_format,
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


class TestCustomerBehaviorProfiler(TestCase):
    """Tests for layered customer behavior profiling."""

    def setUp(self):
        self.org = Organization.objects.create(name='Behavior Org', slug='behavior-org')
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

    def test_customer_segments_includes_behavior_stats(self):
        customer = Customer.objects.create(
            organization=self.org,
            email='profiled@example.com',
            name='Profiled Customer',
            lifetime_value=Decimal('180.00'),
            last_order_date=date.today() - timedelta(days=12),
            rfm_segment='Loyal',
            behavior_profile='Fast Repeat',
            behavior_profile_reason='Returns quickly after each purchase and is currently active.',
            days_since_last_order=12,
            avg_days_between_orders=14,
            days_to_second_order=16,
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
        self.assertIn('behavior_stats', response.context)
        self.assertTrue(any(row['segment'] == 'Fast Repeat' for row in response.context['behavior_stats']))
        self.assertContains(response, 'Behavior profiles')
        self.assertContains(response, 'Fast Repeat')

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


class CustomerDetailMarketingTabTests(TestCase):
    """The Marketing Activity tab surfaces per-customer native-SMS delivery state."""

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

    def test_marketing_tab_lists_sms_activity(self):
        self._make_message(first_clicked_at=timezone.now() - timedelta(hours=1), click_count=2)

        response = self.client.get(reverse('tickets:customer_detail', args=[self.customer.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['sms_stats']['total'], 1)
        self.assertEqual(response.context['sms_stats']['delivered'], 1)
        self.assertEqual(response.context['sms_stats']['clicked'], 1)
        self.assertContains(response, 'Marketing Activity')
        self.assertContains(response, 'Summer Promo')
        self.assertContains(response, 'Delivered')

    def test_marketing_tab_empty_state(self):
        response = self.client.get(reverse('tickets:customer_detail', args=[self.customer.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['sms_stats']['total'], 0)
        self.assertContains(response, 'No marketing messages sent to this customer yet.')

    def test_marketing_tab_hidden_when_feature_disabled(self):
        self.org.sms_marketing_enabled = False
        self.org.save(update_fields=['sms_marketing_enabled'])

        response = self.client.get(reverse('tickets:customer_detail', args=[self.customer.id]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Marketing Activity')


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

    def setUp(self):
        from django.core.cache import cache as django_cache
        self.org = Organization.objects.create(name='Cache Test Org', slug='cache-test-org')
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='City')
        self.event = Event.objects.create(
            organization=self.org, name='Cache Test Event', venue=self.venue,
            start_date=date(2025, 8, 1),
        )
        self.customer = Customer.objects.create(
            organization=self.org, email='cachebuyer@example.com', name='Cache Buyer',
        )
        self.csv_format = CSVFormat.objects.create(
            organization=self.org,
            name='Cache Format',
            column_mapping={'order_number': 'Order ID'},
        )
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
        self.assertContains(response, 'Talent Lineup (optional)')


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


class ChurnDetectionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Churn Org', slug='churn-org')
        self.user = User.objects.create_user(
            username='churnuser',
            email='churn@test.com',
            password='testpass123',
        )
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='churn@test.com', password='testpass123')
        self.client.get(reverse('tickets:home'))

        self.csv_format = CSVFormat.objects.create(
            organization=self.org,
            name='Churn Format',
            column_mapping={'order_number': 'Order ID'},
        )
        self.upload = UploadedFile.objects.create(
            organization=self.org,
            csv_format=self.csv_format,
            filename='churn.csv',
            status='completed',
        )
        self.venue = Venue.objects.create(organization=self.org, name='Churn Venue', city='Los Angeles')
        self.event = Event.objects.create(
            organization=self.org,
            name='Churn Event',
            venue=self.venue,
            start_date=date.today() - timedelta(days=120),
            start_time=time(20, 0),
        )

    def _create_customer_with_orders(
        self,
        email,
        name,
        order_days_ago,
        lifetime_value,
        order_count=2,
        is_in_person=False,
        rfm_segment='At-Risk',
        organization=None,
    ):
        organization = organization or self.org
        customer = Customer.objects.create(
            organization=organization,
            email=email,
            name=name,
            lifetime_value=lifetime_value,
            last_order_date=date.today() - timedelta(days=order_days_ago),
            rfm_segment=rfm_segment,
        )
        upload = self.upload
        event = self.event
        if organization != self.org:
            csv_format = CSVFormat.objects.create(
                organization=organization,
                name=f'Format {name}',
                column_mapping={'order_number': 'Order ID'},
            )
            upload = UploadedFile.objects.create(
                organization=organization,
                csv_format=csv_format,
                filename=f'{name}.csv',
                status='completed',
            )
            venue = Venue.objects.create(organization=organization, name=f'Venue {name}', city='Oakland')
            event = Event.objects.create(
                organization=organization,
                name=f'Event {name}',
                venue=venue,
                start_date=date.today() - timedelta(days=order_days_ago),
                start_time=time(20, 0),
            )

        for idx in range(order_count):
            TicketOrder.objects.create(
                customer=customer,
                event=event,
                uploaded_file=upload,
                order_number=f'{name}-{idx}-{uuid.uuid4().hex[:6]}',
                order_date=timezone.now() - timedelta(days=order_days_ago + idx),
                total_amount=lifetime_value / order_count,
                is_in_person=is_in_person,
            )
        return customer

    def test_churn_overview_filters_to_multi_order_online_customers_past_threshold(self):
        qualifying = self._create_customer_with_orders(
            email='qualifying@example.com',
            name='Qualifying',
            order_days_ago=120,
            lifetime_value=Decimal('240.00'),
            order_count=3,
            rfm_segment='Loyal',
        )
        self._create_customer_with_orders(
            email='recent@example.com',
            name='Recent',
            order_days_ago=20,
            lifetime_value=Decimal('120.00'),
            order_count=3,
        )
        self._create_customer_with_orders(
            email='oneorder@example.com',
            name='One Order',
            order_days_ago=120,
            lifetime_value=Decimal('90.00'),
            order_count=1,
        )
        self._create_customer_with_orders(
            email='inperson@example.com',
            name='In Person',
            order_days_ago=120,
            lifetime_value=Decimal('180.00'),
            order_count=3,
            is_in_person=True,
        )

        response = self.client.get(reverse('tickets:churn_overview'))

        self.assertEqual(response.status_code, 200)
        page_customers = list(response.context['page_obj'])
        self.assertEqual(page_customers, [qualifying])
        self.assertEqual(response.context['stats']['total_count'], 1)
        self.assertEqual(response.context['stats']['total_ltv_at_risk'], Decimal('240.00'))

    def test_churn_overview_honors_valid_days_parameter_and_defaults_invalid(self):
        customer = self._create_customer_with_orders(
            email='threshold@example.com',
            name='Threshold Customer',
            order_days_ago=75,
            lifetime_value=Decimal('150.00'),
            order_count=2,
        )

        response_60 = self.client.get(reverse('tickets:churn_overview'), {'days': 60})
        response_invalid = self.client.get(reverse('tickets:churn_overview'), {'days': '999'})

        self.assertIn(customer, list(response_60.context['page_obj']))
        self.assertEqual(response_60.context['days'], 60)
        self.assertNotIn(customer, list(response_invalid.context['page_obj']))
        self.assertEqual(response_invalid.context['days'], 90)

    def test_churn_overview_is_org_scoped(self):
        other_org = Organization.objects.create(name='Other Churn Org', slug='other-churn-org')
        self._create_customer_with_orders(
            email='other@example.com',
            name='Other Org Customer',
            order_days_ago=120,
            lifetime_value=Decimal('300.00'),
            order_count=3,
            organization=other_org,
        )

        response = self.client.get(reverse('tickets:churn_overview'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['page_obj']), [])

    def test_churn_bulk_tag_adds_tag_to_selected_customers_only(self):
        first = self._create_customer_with_orders(
            email='first@example.com',
            name='First',
            order_days_ago=120,
            lifetime_value=Decimal('200.00'),
            order_count=2,
        )
        second = self._create_customer_with_orders(
            email='second@example.com',
            name='Second',
            order_days_ago=120,
            lifetime_value=Decimal('300.00'),
            order_count=2,
        )
        unselected = self._create_customer_with_orders(
            email='unselected@example.com',
            name='Unselected',
            order_days_ago=120,
            lifetime_value=Decimal('110.00'),
            order_count=2,
        )
        tag = CustomerTag.objects.create(organization=self.org, name='Win-Back', color='green')

        response = self.client.post(reverse('tickets:churn_bulk_tag'), {
            'tag_id': str(tag.id),
            'days': '60',
            'customer_ids': [str(first.id), str(second.id)],
        })

        self.assertRedirects(response, f"{reverse('tickets:churn_overview')}?days=60")
        self.assertIn(tag, first.tags.all())
        self.assertIn(tag, second.tags.all())
        self.assertNotIn(tag, unselected.tags.all())

    def test_churn_bulk_tag_ignores_cross_org_customer_ids(self):
        local_customer = self._create_customer_with_orders(
            email='local@example.com',
            name='Local',
            order_days_ago=120,
            lifetime_value=Decimal('140.00'),
            order_count=2,
        )
        other_org = Organization.objects.create(name='Cross Org', slug='cross-org')
        other_customer = self._create_customer_with_orders(
            email='cross@example.com',
            name='Cross',
            order_days_ago=120,
            lifetime_value=Decimal('180.00'),
            order_count=2,
            organization=other_org,
        )
        tag = CustomerTag.objects.create(organization=self.org, name='Re-engage', color='blue')

        response = self.client.post(reverse('tickets:churn_bulk_tag'), {
            'tag_id': str(tag.id),
            'days': '90',
            'customer_ids': [str(local_customer.id), str(other_customer.id)],
        })

        self.assertRedirects(response, f"{reverse('tickets:churn_overview')}?days=90")
        self.assertIn(tag, local_customer.tags.all())


class CustomerBulkSMSStatusTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='SMS Org', slug='sms-org')
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
        )
        self.bob = Customer.objects.create(
            organization=self.org, email='bob@example.com', name='Bob',
            phone='+13105550002', sms_opt_in=False,
        )
        self.url = reverse('tickets:customers_bulk_sms_status')

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


class AIRecommendationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='AI Org', slug='ai-org')
        self.other_org = Organization.objects.create(name='Other AI Org', slug='other-ai-org')
        self.user = User.objects.create_user(
            username='aiuser',
            email='ai@example.com',
            password='testpass123',
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
        self.client.login(username='ai@example.com', password='testpass123')
        session = self.client.session
        session['_org_id'] = str(self.org.pk)
        session.save()
        self.venue = Venue.objects.create(
            organization=self.org,
            name='AI Venue',
            city='Atlanta',
            capacity=500,
        )
        self.event = Event.objects.create(
            organization=self.org,
            name='AI Event',
            venue=self.venue,
            ticketing_type='direct',
            start_date=date.today() + timedelta(days=10),
            start_time=time(20, 0),
            end_date=date.today() + timedelta(days=10),
            capacity=400,
        )

    def _recommendation(self, **overrides):
        defaults = {
            'organization': self.org,
            'event': self.event,
            'kind': AIRecommendation.Kind.SALES_PACING,
            'priority': AIRecommendation.Priority.HIGH,
            'confidence': Decimal('0.800'),
            'title': 'Review pacing',
            'summary': 'Sales are behind pace.',
            'evidence_json': {'gap': 12},
            'recommended_action_json': {
                'type': 'open_url',
                'label': 'Open event',
                'url': reverse('tickets:event_detail', args=[self.event.id]),
                'payload': {'event_id': str(self.event.id)},
            },
            'dedupe_key': 'sales_pacing:test',
        }
        defaults.update(overrides)
        return AIRecommendation.objects.create(**defaults)

    def test_recommendation_dedupe_is_scoped_to_organization(self):
        self._recommendation()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._recommendation(title='Duplicate')
        other_event = Event.objects.create(
            organization=self.other_org,
            name='Other Event',
            venue=Venue.objects.create(organization=self.other_org, name='Other Venue', city='Savannah'),
            start_date=date.today() + timedelta(days=10),
        )
        rec = self._recommendation(
            organization=self.other_org,
            event=other_event,
            dedupe_key='sales_pacing:test',
        )
        self.assertEqual(rec.organization, self.other_org)

    def test_status_transition_helpers_set_timestamps(self):
        rec = self._recommendation()
        rec.mark_reviewed()
        rec.refresh_from_db()
        self.assertEqual(rec.status, AIRecommendation.Status.REVIEWED)
        self.assertIsNotNone(rec.reviewed_at)
        rec.dismiss()
        rec.refresh_from_db()
        self.assertEqual(rec.status, AIRecommendation.Status.DISMISSED)
        self.assertIsNotNone(rec.dismissed_at)
        rec.resolve()
        rec.refresh_from_db()
        self.assertEqual(rec.status, AIRecommendation.Status.RESOLVED)
        self.assertIsNotNone(rec.resolved_at)

    @patch('tickets.services.ai_recommendations.generator.generate_forecast_preview')
    def test_sales_pacing_detector_creates_recommendation(self, mock_preview):
        from tickets.services.ai_recommendations import AIRecommendationGenerator

        self.event.cached_paid_ticket_count = 2
        self.event.save(update_fields=['cached_paid_ticket_count'])
        mock_preview.return_value = {
            'has_sufficient_data': True,
            'curve_points': [{'days_before': 10, 'expected_tickets': 25}],
        }
        recommendations = AIRecommendationGenerator(self.org).detect_sales_pacing_risks()
        self.assertEqual(len(recommendations), 1)
        self.assertEqual(recommendations[0].kind, AIRecommendation.Kind.SALES_PACING)
        self.assertEqual(recommendations[0].evidence_json['ticket_gap'], 23)

    def test_post_event_detector_creates_review_recommendation_and_dedupes(self):
        from tickets.services.ai_recommendations import AIRecommendationGenerator

        self.event.start_date = date.today() - timedelta(days=2)
        self.event.end_date = date.today() - timedelta(days=2)
        self.event.save(update_fields=['start_date', 'end_date'])
        generator = AIRecommendationGenerator(self.org)
        first = generator.detect_post_event_wrapups()
        second = generator.detect_post_event_wrapups()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(
            AIRecommendation.objects.filter(organization=self.org, kind=AIRecommendation.Kind.POST_EVENT_WRAPUP).count(),
            1,
        )

    def test_dismissed_recommendation_is_not_reopened_by_detector(self):
        from tickets.services.ai_recommendations import AIRecommendationGenerator

        self.event.start_date = date.today() - timedelta(days=2)
        self.event.end_date = date.today() - timedelta(days=2)
        self.event.save(update_fields=['start_date', 'end_date'])
        rec = AIRecommendationGenerator(self.org).detect_post_event_wrapups()[0]
        rec.dismiss()
        AIRecommendationGenerator(self.org).detect_post_event_wrapups()
        rec.refresh_from_db()
        self.assertEqual(rec.status, AIRecommendation.Status.DISMISSED)

    def test_winback_detector_creates_recommendation(self):
        from tickets.services.ai_recommendations import AIRecommendationGenerator

        past_event = Event.objects.create(
            organization=self.org,
            name='Past Buyer Event',
            venue=self.venue,
            start_date=date.today() - timedelta(days=120),
        )
        customer = Customer.objects.create(
            organization=self.org,
            email='winback@example.com',
            name='Win Back',
            lifetime_value=Decimal('500.00'),
            last_order_date=date.today() - timedelta(days=120),
        )
        for idx in range(2):
            TicketOrder.objects.create(
                customer=customer,
                event=past_event,
                order_number=f'WB-{idx}-{uuid.uuid4().hex[:6]}',
                order_date=timezone.now() - timedelta(days=120 + idx),
                total_amount=Decimal('250.00'),
            )
        recommendations = AIRecommendationGenerator(self.org).detect_winback_audience()
        self.assertEqual(len(recommendations), 1)
        self.assertEqual(recommendations[0].kind, AIRecommendation.Kind.WINBACK_AUDIENCE)
        self.assertEqual(recommendations[0].evidence_json['customer_count'], 1)

    def test_marketing_attribution_detector_creates_recommendation(self):
        from tickets.services.ai_recommendations import AIRecommendationGenerator

        self.org.meta_ads_account_id = 'act_123'
        self.org.mailchimp_access_token = 'token'
        self.org.mailchimp_dc = 'us1'
        self.org.save(update_fields=['meta_ads_account_id', 'mailchimp_access_token', 'mailchimp_dc'])
        recommendations = AIRecommendationGenerator(self.org).detect_marketing_attribution_gaps()
        self.assertEqual(len(recommendations), 1)
        self.assertEqual(recommendations[0].kind, AIRecommendation.Kind.MARKETING_ATTRIBUTION)
        self.assertIn('Meta Ads spend', recommendations[0].evidence_json['missing_items'])

    def test_marketing_attribution_detector_skips_meta_gap_for_manual_facebook_marketing_expense(self):
        from tickets.services.ai_recommendations import AIRecommendationGenerator

        self.org.meta_ads_account_id = 'act_123'
        self.org.save(update_fields=['meta_ads_account_id'])
        EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='manual',
            description='Facebook ads campaign',
            amount=Decimal('150.00'),
        )

        recommendations = AIRecommendationGenerator(self.org).detect_marketing_attribution_gaps()

        self.assertEqual(recommendations, [])
        self.assertFalse(
            AIRecommendation.objects.filter(
                organization=self.org,
                dedupe_key=f'marketing_attribution:{self.event.id}',
            ).exists()
        )

    def test_marketing_attribution_detector_requires_manual_meta_expense_to_be_marketing(self):
        from tickets.services.ai_recommendations import AIRecommendationGenerator

        self.org.meta_ads_account_id = 'act_123'
        self.org.save(update_fields=['meta_ads_account_id'])
        EventExpense.objects.create(
            event=self.event,
            category='venue',
            source='manual',
            description='Facebook ads watch party rental',
            amount=Decimal('150.00'),
        )

        recommendations = AIRecommendationGenerator(self.org).detect_marketing_attribution_gaps()

        self.assertEqual(len(recommendations), 1)
        self.assertIn('Meta Ads spend', recommendations[0].evidence_json['missing_items'])

    def test_marketing_attribution_detector_resolves_open_recommendation_when_manual_meta_expense_exists(self):
        from tickets.services.ai_recommendations import AIRecommendationGenerator

        self.org.meta_ads_account_id = 'act_123'
        self.org.save(update_fields=['meta_ads_account_id'])
        generator = AIRecommendationGenerator(self.org)
        recommendation = generator.detect_marketing_attribution_gaps()[0]

        EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='manual',
            description='Launch marketing',
            notes='IG and FB ad spend',
            amount=Decimal('150.00'),
        )

        recommendations = generator.detect_marketing_attribution_gaps()
        recommendation.refresh_from_db()

        self.assertEqual(recommendations, [])
        self.assertEqual(recommendation.status, AIRecommendation.Status.RESOLVED)
        self.assertIsNotNone(recommendation.resolved_at)

    def test_action_center_is_org_scoped_and_dashboard_excludes_closed_items(self):
        visible = self._recommendation(title='Visible recommendation')
        hidden = self._recommendation(
            title='Hidden recommendation',
            dedupe_key='sales_pacing:hidden',
            status=AIRecommendation.Status.DISMISSED,
        )
        other_event = Event.objects.create(
            organization=self.other_org,
            name='Other Event',
            venue=Venue.objects.create(organization=self.other_org, name='Other Venue', city='Savannah'),
            start_date=date.today() + timedelta(days=10),
        )
        self._recommendation(
            organization=self.other_org,
            event=other_event,
            title='Other org recommendation',
            dedupe_key='other-org-rec',
        )

        response = self.client.get(reverse('tickets:action_center'))
        self.assertContains(response, visible.title)
        self.assertNotContains(response, 'Other org recommendation')
        self.assertNotContains(response, hidden.title)

        response = self.client.get(reverse('tickets:home'))
        self.assertContains(response, visible.title)
        self.assertNotContains(response, hidden.title)

    def test_action_center_renders_review_link_for_marketing_unconfirmed(self):
        rec = self._recommendation(
            kind=AIRecommendation.Kind.MARKETING_UNCONFIRMED,
            dedupe_key=f'marketing_unconfirmed:{self.event.id}',
            title='AI Event has 1 marketing match to confirm',
            recommended_action_json={
                'type': 'open_url',
                'label': 'Review and confirm',
                'url': reverse('tickets:event_detail', args=[self.event.id]) + '#marketing',
            },
        )
        response = self.client.get(reverse('tickets:action_center'))
        self.assertContains(response, rec.title)
        self.assertContains(response, 'Review and confirm')
        content = response.content.decode()
        # Admin sees the unconfirmed-matches modal trigger with a marketing-tab fallback URL.
        self.assertIn('data-bs-target="#unconfirmedMatchesModal"', content)
        self.assertIn(f'data-recommendation-id="{rec.id}"', content)
        self.assertIn(
            reverse('tickets:event_detail', args=[self.event.id]) + '#marketing',
            content,
        )
        # The empty-modal-bug shouldn't recur: this kind must not attach the link-campaigns modal.
        self.assertNotIn(
            f'data-bs-target="#linkCampaignsModal" data-recommendation-id="{rec.id}"',
            content,
        )

    def test_recommendation_review_dismiss_and_resolve_views(self):
        rec = self._recommendation()
        response = self.client.post(reverse('tickets:ai_recommendation_review', args=[rec.id]))
        self.assertEqual(response.status_code, 302)
        rec.refresh_from_db()
        self.assertEqual(rec.status, AIRecommendation.Status.REVIEWED)

        response = self.client.post(reverse('tickets:ai_recommendation_dismiss', args=[rec.id]))
        self.assertEqual(response.status_code, 302)
        rec.refresh_from_db()
        self.assertEqual(rec.status, AIRecommendation.Status.DISMISSED)

        response = self.client.post(reverse('tickets:ai_recommendation_resolve', args=[rec.id]))
        self.assertEqual(response.status_code, 302)
        rec.refresh_from_db()
        self.assertEqual(rec.status, AIRecommendation.Status.RESOLVED)

    def test_task_handles_detector_failure(self):
        from tickets.tasks import generate_org_ai_opportunities_task

        with patch(
            'tickets.services.ai_recommendations.generator.AIRecommendationGenerator.detect_sales_pacing_risks',
            side_effect=RuntimeError('boom'),
        ):
            generated = generate_org_ai_opportunities_task.run(str(self.org.id))
        self.assertGreaterEqual(generated, 0)

    def test_management_command_enqueues_one_task_per_org(self):
        with patch(
            'tickets.management.commands.generate_ai_opportunities.generate_org_ai_opportunities_task.delay'
        ) as mock_delay:
            call_command('generate_ai_opportunities')
        self.assertGreaterEqual(mock_delay.call_count, 2)


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

    def test_unconfirmed_campaigns_excluded_from_all_metrics(self):
        """Unconfirmed campaigns must not flow into channel totals or top tables."""
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
            event=self.event, source='mailchimp', external_id='mc-pending',
            campaign_title='Pending', send_time=now - timedelta(days=3),
            emails_sent=2000, unique_opens=400, unique_clicks=100,
            ecommerce_orders=12, ecommerce_revenue=Decimal('9999.00'),
        )

        result = MarketingAnalyticsService(self.org, window_days=90).calculate()
        self.assertEqual(result['channels']['email']['revenue'], Decimal('250.00'))
        self.assertEqual(result['channels']['email']['campaigns'], 1)
        self.assertEqual(len(result['top_email_campaigns']), 1)
        self.assertEqual(result['top_email_campaigns'][0]['name'], 'Confirmed')

    def test_unconfirmed_meta_expense_excluded(self):
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
            event=self.event, category='marketing', description='Pending ad',
            amount=Decimal('500.00'), expense_date=date.today(), source='meta_ads',
            external_id='ad-p', manual_attributed_revenue=Decimal('9999.00'),
        )

        result = MarketingAnalyticsService(self.org, window_days=90).calculate()
        self.assertEqual(result['channels']['ads']['spend'], Decimal('100.00'))
        self.assertEqual(result['channels']['ads']['revenue'], Decimal('300.00'))

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
    """View-level tests for the marketing overview page."""

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

    def test_overview_renders_with_default_window(self):
        response = self.client.get(reverse('tickets:marketing_overview'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('metrics', response.context)
        self.assertEqual(response.context['window_key'], '90')

    def test_window_querystring_overrides_default(self):
        response = self.client.get(reverse('tickets:marketing_overview') + '?window=all')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['window_key'], 'all')

    def test_invalid_window_falls_back_to_default(self):
        response = self.client.get(reverse('tickets:marketing_overview') + '?window=banana')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['window_key'], '90')


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
    """View-level tests for the review/edit/confirm/unconfirm endpoints."""

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

    def test_confirm_sets_confirmed_at_and_by(self):
        url = reverse('tickets:event_mailchimp_confirm', args=[self.event.id, self.email.id])
        self.client.post(url)
        self.email.refresh_from_db()
        self.assertIsNotNone(self.email.confirmed_at)
        self.assertEqual(self.email.confirmed_by, self.admin)

    def test_unconfirm_clears_confirmed_at(self):
        self.email.confirmed_at = timezone.now()
        self.email.confirmed_by = self.admin
        self.email.save()
        url = reverse('tickets:event_mailchimp_unconfirm', args=[self.event.id, self.email.id])
        self.client.post(url)
        self.email.refresh_from_db()
        self.assertIsNone(self.email.confirmed_at)
        self.assertIsNone(self.email.confirmed_by)

    def test_confirm_meta_ads_expense(self):
        url = reverse('tickets:event_meta_ads_confirm', args=[self.event.id, self.ad.id])
        self.client.post(url)
        self.ad.refresh_from_db()
        self.assertIsNotNone(self.ad.confirmed_at)

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

    def test_mailchimp_confirm_all_confirms_only_emails(self):
        url = reverse('tickets:event_mailchimp_confirm_all', args=[self.event.id])
        self.client.post(url)
        self.email.refresh_from_db()
        self.sms.refresh_from_db()
        self.ad.refresh_from_db()
        self.assertIsNotNone(self.email.confirmed_at)
        self.assertIsNone(self.sms.confirmed_at)
        self.assertIsNone(self.ad.confirmed_at)

    def test_slicktext_confirm_all_confirms_only_sms(self):
        url = reverse('tickets:event_slicktext_confirm_all', args=[self.event.id])
        self.client.post(url)
        self.email.refresh_from_db()
        self.sms.refresh_from_db()
        self.ad.refresh_from_db()
        self.assertIsNone(self.email.confirmed_at)
        self.assertIsNotNone(self.sms.confirmed_at)
        self.assertIsNone(self.ad.confirmed_at)

    def test_meta_ads_confirm_all_confirms_only_ads(self):
        url = reverse('tickets:event_meta_ads_confirm_all', args=[self.event.id])
        self.client.post(url)
        self.email.refresh_from_db()
        self.sms.refresh_from_db()
        self.ad.refresh_from_db()
        self.assertIsNone(self.email.confirmed_at)
        self.assertIsNone(self.sms.confirmed_at)
        self.assertIsNotNone(self.ad.confirmed_at)

    def test_mailchimp_confirm_all_skips_already_confirmed(self):
        original_time = timezone.now() - timedelta(days=1)
        self.email.confirmed_at = original_time
        self.email.save()
        url = reverse('tickets:event_mailchimp_confirm_all', args=[self.event.id])
        self.client.post(url)
        self.email.refresh_from_db()
        # Already-confirmed row keeps its original timestamp.
        self.assertEqual(self.email.confirmed_at, original_time)

    def test_ajax_mailchimp_confirm_all_returns_rows_json(self):
        url = reverse('tickets:event_mailchimp_confirm_all', args=[self.event.id])
        resp = self.client.post(url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['count'], 1)
        self.assertEqual(len(body['rows']), 1)
        self.assertTrue(body['rows'][0]['is_confirmed'])
        self.assertEqual(body['rows'][0]['status_label'], 'Confirmed')

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
        self.assertEqual(row['is_confirmed'], False)
        self.assertEqual(row['status_label'], 'Pending')

    def test_ajax_confirm_returns_json_with_status(self):
        url = reverse('tickets:event_mailchimp_confirm', args=[self.event.id, self.email.id])
        resp = self.client.post(url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body['ok'])
        self.assertTrue(body['row']['is_confirmed'])
        self.assertEqual(body['row']['status_label'], 'Confirmed')

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


class MarketingDetectorTests(TestCase):
    """Tests for the new marketing detectors in AIRecommendationGenerator."""

    def setUp(self):
        from .models import EventEmailCampaign, EventSMSCampaign
        self.org = Organization.objects.create(name='Detector Org', slug='detector-org')
        venue = Venue.objects.create(organization=self.org, name='V', city='SF')
        self.event = Event.objects.create(
            organization=self.org, name='Event A', venue=venue,
            start_date=date.today() - timedelta(days=10), start_time=time(20, 0, 0),
            computed_total_revenue=Decimal('100.00'),
        )

        self.high_unsub_email = EventEmailCampaign.objects.create(
            event=self.event, source='mailchimp', external_id='em-bad',
            campaign_title='Bad blast', send_time=timezone.now() - timedelta(days=5),
            emails_sent=1000, unsubscribes=25, unique_opens=100, unique_clicks=20,
        )
        EventExpense.objects.create(
            event=self.event, category='marketing', description='Meta Ads',
            amount=Decimal('500.00'), expense_date=date.today() - timedelta(days=5),
            source='meta_ads', external_id='ad-bad',
            confirmed_at=timezone.now(),
        )

    def test_high_unsubscribe_rate_detector_creates_one_rec(self):
        from tickets.services.ai_recommendations.generator import AIRecommendationGenerator

        gen = AIRecommendationGenerator(self.org)
        recs = gen.detect_high_unsubscribe_rate()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].kind, AIRecommendation.Kind.MARKETING_ATTRIBUTION)
        self.assertEqual(recs[0].dedupe_key, f'marketing_unsub:email:{self.high_unsub_email.id}')

        # Idempotent on re-run
        recs2 = gen.detect_high_unsubscribe_rate()
        self.assertEqual(len(recs2), 1)
        self.assertEqual(AIRecommendation.objects.filter(organization=self.org).count(), 1)

    def test_low_channel_roi_detector_fires_when_roas_below_one(self):
        from tickets.services.ai_recommendations.generator import AIRecommendationGenerator

        gen = AIRecommendationGenerator(self.org)
        recs = gen.detect_low_channel_roi()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].dedupe_key, 'marketing_low_roi:ads:90')
        self.assertEqual(recs[0].priority, AIRecommendation.Priority.HIGH)

    def test_channel_imbalance_detector_flags_ads_without_email(self):
        from tickets.services.ai_recommendations.generator import AIRecommendationGenerator

        self.high_unsub_email.hard_delete()
        gen = AIRecommendationGenerator(self.org)
        recs = gen.detect_channel_imbalance()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].dedupe_key, f'marketing_imbalance:{self.event.id}')

    def test_dismissed_recommendation_is_not_reopened(self):
        from tickets.services.ai_recommendations.generator import AIRecommendationGenerator

        gen = AIRecommendationGenerator(self.org)
        recs = gen.detect_high_unsubscribe_rate()
        recs[0].dismiss()

        # Re-run; the dismissed record stays dismissed.
        gen.detect_high_unsubscribe_rate()
        kept = AIRecommendation.objects.get(id=recs[0].id)
        self.assertEqual(kept.status, AIRecommendation.Status.DISMISSED)

    def test_channel_imbalance_detector_resolves_when_email_linked(self):
        from tickets.services.ai_recommendations.generator import AIRecommendationGenerator
        from .models import EventEmailCampaign

        # Start with no email campaigns and a confirmed Meta Ads expense already in setUp.
        self.high_unsub_email.hard_delete()
        gen = AIRecommendationGenerator(self.org)
        recs = gen.detect_channel_imbalance()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.status, AIRecommendation.Status.NEW)

        # User links a Mailchimp campaign; rerunning the detector should resolve the stale rec.
        EventEmailCampaign.objects.create(
            event=self.event, source='mailchimp', external_id='em-new',
            campaign_title='Linked blast', send_time=timezone.now() - timedelta(days=3),
            emails_sent=500,
        )
        gen.detect_channel_imbalance()
        rec.refresh_from_db()
        self.assertEqual(rec.status, AIRecommendation.Status.RESOLVED)

    def test_channel_imbalance_detector_emits_modal_evidence_keys(self):
        from tickets.services.ai_recommendations.generator import AIRecommendationGenerator

        self.org.mailchimp_access_token = 'fake-token'
        self.org.mailchimp_dc = 'us1'
        self.org.meta_ads_account_id = '12345'
        self.org.save(update_fields=['mailchimp_access_token', 'mailchimp_dc', 'meta_ads_account_id'])

        self.high_unsub_email.hard_delete()
        recs = AIRecommendationGenerator(self.org).detect_channel_imbalance()
        self.assertEqual(len(recs), 1)
        evidence = recs[0].evidence_json
        self.assertEqual(evidence.get('missing_items'), ['Mailchimp campaign report'])
        self.assertTrue(evidence.get('mailchimp_connected'))
        self.assertTrue(evidence.get('meta_connected'))

    def test_unconfirmed_matches_detector_fires_for_unconfirmed_email(self):
        from tickets.services.ai_recommendations.generator import AIRecommendationGenerator

        # setUp already created an unconfirmed Mailchimp email campaign for self.event.
        recs = AIRecommendationGenerator(self.org).detect_unconfirmed_marketing_matches()
        matching = [r for r in recs if r.dedupe_key == f'marketing_unconfirmed:{self.event.id}']
        self.assertEqual(len(matching), 1)
        rec = matching[0]
        self.assertEqual(rec.kind, AIRecommendation.Kind.MARKETING_UNCONFIRMED)
        self.assertEqual(rec.evidence_json['channels']['mailchimp'], 1)
        self.assertEqual(rec.evidence_json['channels']['slicktext'], 0)
        self.assertEqual(rec.evidence_json['channels']['meta_ads'], 0)
        self.assertEqual(rec.evidence_json['total'], 1)
        self.assertTrue(rec.recommended_action_json['url'].endswith('#marketing'))

    def test_unconfirmed_matches_detector_resolves_when_confirmed(self):
        from tickets.services.ai_recommendations.generator import AIRecommendationGenerator

        gen = AIRecommendationGenerator(self.org)
        recs = gen.detect_unconfirmed_marketing_matches()
        rec = next(r for r in recs if r.dedupe_key == f'marketing_unconfirmed:{self.event.id}')
        self.assertEqual(rec.status, AIRecommendation.Status.NEW)

        self.high_unsub_email.confirmed_at = timezone.now()
        self.high_unsub_email.save(update_fields=['confirmed_at'])

        gen.detect_unconfirmed_marketing_matches()
        rec.refresh_from_db()
        self.assertEqual(rec.status, AIRecommendation.Status.RESOLVED)

    def test_unconfirmed_matches_detector_counts_across_channels(self):
        from tickets.services.ai_recommendations.generator import AIRecommendationGenerator
        from .models import EventSMSCampaign

        EventSMSCampaign.objects.create(
            event=self.event, source='slicktext', external_id='sms-1',
            name='SMS blast', send_time=timezone.now() - timedelta(days=2),
        )
        EventExpense.objects.create(
            event=self.event, category='marketing', description='Another Meta Ads campaign',
            amount=Decimal('100.00'), expense_date=date.today() - timedelta(days=2),
            source='meta_ads', external_id='ad-unconfirmed',
        )

        recs = AIRecommendationGenerator(self.org).detect_unconfirmed_marketing_matches()
        rec = next(r for r in recs if r.dedupe_key == f'marketing_unconfirmed:{self.event.id}')
        self.assertEqual(rec.evidence_json['channels']['mailchimp'], 1)
        self.assertEqual(rec.evidence_json['channels']['slicktext'], 1)
        self.assertEqual(rec.evidence_json['channels']['meta_ads'], 1)
        self.assertEqual(rec.evidence_json['total'], 3)


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


class UnconfirmedMatchesEndpointTests(TestCase):
    """JSON endpoint that powers the Action Center 'Review and confirm' modal."""

    def setUp(self):
        from .models import EventEmailCampaign, EventSMSCampaign

        self.client = Client()
        self.org = Organization.objects.create(name='Modal Org', slug='modal-org')
        self.user = User.objects.create_user(
            username='modaladmin', email='modaladmin@example.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        venue = Venue.objects.create(organization=self.org, name='V', city='SF')
        self.event = Event.objects.create(
            organization=self.org, name='Match Modal Show', venue=venue,
            start_date=date.today() - timedelta(days=5), start_time=time(20, 0, 0),
        )

        # One unconfirmed row per channel + one confirmed Mailchimp row that must be excluded.
        EventExpense.objects.create(
            event=self.event, category='marketing', description='ad1',
            amount=Decimal('250.00'), source='meta_ads', external_id='ad-1',
            external_metadata={'campaign_name': 'Spring Push'},
        )
        EventEmailCampaign.objects.create(
            event=self.event, source='mailchimp', external_id='mc-1',
            campaign_title='Newsletter', subject_line='Hello',
            send_time=timezone.now() - timedelta(days=3),
        )
        EventEmailCampaign.objects.create(
            event=self.event, source='mailchimp', external_id='mc-2',
            campaign_title='Already confirmed', send_time=timezone.now() - timedelta(days=4),
            confirmed_at=timezone.now(), confirmed_by=self.user,
        )
        EventSMSCampaign.objects.create(
            event=self.event, source='slicktext', external_id='st-1',
            name='SMS blast', message='Doors at 8',
            send_time=timezone.now() - timedelta(days=2),
        )

        self.unconfirmed_rec = AIRecommendation.objects.create(
            organization=self.org,
            event=self.event,
            kind=AIRecommendation.Kind.MARKETING_UNCONFIRMED,
            priority=AIRecommendation.Priority.HIGH,
            confidence=Decimal('0.900'),
            title='3 matches to confirm',
            summary='Review and confirm',
            dedupe_key=f'marketing_unconfirmed:{self.event.id}',
            recommended_action_json={'type': 'open_url', 'label': 'Review and confirm', 'url': '/x/'},
        )
        self.other_kind_rec = AIRecommendation.objects.create(
            organization=self.org,
            event=self.event,
            kind=AIRecommendation.Kind.MARKETING_ATTRIBUTION,
            priority=AIRecommendation.Priority.MEDIUM,
            confidence=Decimal('0.500'),
            title='Link campaigns',
            summary='Link',
            dedupe_key=f'marketing_attribution:{self.event.id}',
        )

    def _login(self):
        self.client.login(username='modaladmin@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))

    def test_requires_login(self):
        url = reverse('tickets:ai_recommendation_unconfirmed_matches', args=[self.unconfirmed_rec.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    def test_returns_unconfirmed_rows_grouped_by_channel(self):
        self._login()
        url = reverse('tickets:ai_recommendation_unconfirmed_matches', args=[self.unconfirmed_rec.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['event_id'], str(self.event.id))
        self.assertEqual(body['event_name'], 'Match Modal Show')
        self.assertEqual(len(body['meta_ads']), 1)
        self.assertEqual(len(body['mailchimp']), 1)  # confirmed row excluded
        self.assertEqual(len(body['slicktext']), 1)
        self.assertEqual(body['total'], 3)
        self.assertEqual(body['meta_ads'][0]['label'], 'Spring Push')
        self.assertEqual(body['meta_ads'][0]['amount'], '250.00')
        self.assertEqual(body['mailchimp'][0]['label'], 'Newsletter')
        self.assertEqual(body['slicktext'][0]['label'], 'SMS blast')
        self.assertIn('confirm_url', body['meta_ads'][0])
        self.assertIn('remove_url', body['meta_ads'][0])
        self.assertIn('meta_ads', body['confirm_all_urls'])

    def test_rejects_non_marketing_unconfirmed_kind(self):
        self._login()
        url = reverse('tickets:ai_recommendation_unconfirmed_matches', args=[self.other_kind_rec.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()['ok'])

    def test_does_not_leak_across_orgs(self):
        other_org = Organization.objects.create(name='Outsider', slug='outsider')
        other_venue = Venue.objects.create(organization=other_org, name='V2', city='LA')
        other_event = Event.objects.create(
            organization=other_org, name='Other', venue=other_venue,
            start_date=date.today(), start_time=time(20, 0, 0),
        )
        outside_rec = AIRecommendation.objects.create(
            organization=other_org,
            event=other_event,
            kind=AIRecommendation.Kind.MARKETING_UNCONFIRMED,
            priority=AIRecommendation.Priority.HIGH,
            confidence=Decimal('0.900'),
            title='not yours',
            summary='nope',
            dedupe_key=f'marketing_unconfirmed:{other_event.id}',
        )
        self._login()
        url = reverse('tickets:ai_recommendation_unconfirmed_matches', args=[outside_rec.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


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
