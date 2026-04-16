import uuid
import json
from datetime import date, time, datetime, timedelta
from unittest.mock import patch, MagicMock
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import TestCase, Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from decimal import Decimal
from rest_framework.authtoken.models import Token
from .models import (
    UploadedFile, TicketOrder, Customer, CustomerTag, Event,
    Venue, CSVFormat, Ticket, TicketTier,
    Organization, UserProfile, OrganizationMembership, OrganizationInvitation, ChatMessage,
    SaleableTicketType, SaleableTicketTypeTier, StripeCheckoutSession, FeatureFlagSettings,
    SurveyInvitation, SurveyResponse, SurveyAnswer, SurveyQuestion, Payout,
    ExternalSurveyResponse, EventDailyPageView,
)


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
            {'email': 'newuser@example.com', 'role': 'organizer', 'org_role': 'host', 'csrfmiddlewaretoken': self.client.cookies.get('csrftoken', '')},
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

    def test_member_invite_duplicate_email_existing_member_error(self):
        other = User.objects.create_user(
            username='other@example.com', email='other@example.com', password='pass123',
        )
        UserProfile.objects.create(user=other, organization=self.org)
        OrganizationMembership.objects.create(user=other, organization=self.org)
        response = self.client.post(
            reverse('tickets:member_invite'),
            {'email': 'other@example.com'},
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
            {'email': 'pending@example.com'},
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

    def test_invite_accept_unauthenticated_redirects_to_signup_with_next(self):
        inv = OrganizationInvitation.objects.create(
            organization=self.org,
            email='invitee@test.com',
            invited_by=self.user,
            status=OrganizationInvitation.Status.PENDING,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.client.logout()
        response = self.client.get(reverse('tickets:invite_accept', args=[inv.token]))
        self.assertEqual(response.status_code, 302)
        location = response.get('Location', '') or response.url
        # Unauthenticated invite acceptance redirects to /login/ so users can
        # sign in or create an account before accepting.
        self.assertIn('/login/', location)
        self.assertIn('next=', location)
        self.assertIn(str(inv.token), location)


class MobileAPITests(TestCase):
    """Test cases for the mobile API endpoints."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='API Test Org', slug='api-test-org')
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

    @patch('stripe.PaymentIntent.retrieve')
    def test_sell_creates_order(self, mock_retrieve):
        mock_pi = MagicMock()
        mock_pi.status = 'succeeded'
        mock_retrieve.return_value = mock_pi

        tt = SaleableTicketType.objects.create(
            event=self.event,
            name='VIP',
            price=Decimal('50.00'),
        )

        response = self.client.post(
            '/api/organizer/sell/',
            data={
                'event_id': str(self.event.pk),
                'payment_intent_id': 'pi_test_123',
                'buyer_email': 'newbuyer@example.com',
                'buyer_name': 'New Buyer',
                'line_items': [
                    {
                        'ticket_type_id': str(tt.pk),
                        'quantity': 2,
                        'name': 'VIP',
                        'price': '50.00',
                    }
                ],
            },
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

    @patch('tickets.views._compute_available_balance')
    @patch('tickets.views._compute_settled_payout_balance')
    @patch('tickets.views._get_stripe_platform_available_cents')
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
        mock_platform_available,
        mock_settled_balance,
        mock_available_balance,
    ):
        mock_available_balance.return_value = (
            Decimal('500.00'),
            Decimal('50.00'),
            Decimal('0.00'),
            Decimal('450.00'),
        )
        mock_settled_balance.return_value = Decimal('450.00')
        mock_platform_available.return_value = 45000
        mock_transfer_create.side_effect = [
            MagicMock(id='tr_first'),
            MagicMock(id='tr_second'),
        ]
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
        self.assertEqual([payout.stripe_transfer_id for payout in payouts], ['tr_first', 'tr_second'])
        self.assertEqual([payout.stripe_payout_id for payout in payouts], ['po_first', 'po_second'])
        self.assertEqual(mock_account_modify.call_count, 2)
        self.assertEqual(mock_transfer_create.call_count, 2)
        self.assertEqual(mock_payout_create.call_count, 2)

    @patch('stripe.Account.retrieve')
    def test_connect_return_requires_payouts_enabled(self, mock_account_retrieve):
        mock_account_retrieve.return_value = self._mock_account(payouts_enabled=False)

        response = self.client.get(reverse('tickets:stripe_connect_return'))

        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        self.assertFalse(self.org.stripe_onboarding_complete)

    @patch('tickets.views._compute_available_balance')
    @patch('tickets.views._compute_settled_payout_balance')
    @patch('tickets.views._get_stripe_platform_available_cents')
    @patch('stripe.Account.retrieve')
    def test_initiate_payout_rejected_when_payouts_disabled(
        self,
        mock_account_retrieve,
        mock_platform_available,
        mock_settled_balance,
        mock_available_balance,
    ):
        mock_available_balance.return_value = (
            Decimal('500.00'),
            Decimal('50.00'),
            Decimal('0.00'),
            Decimal('450.00'),
        )
        mock_settled_balance.return_value = Decimal('450.00')
        mock_platform_available.return_value = 45000
        mock_account_retrieve.return_value = self._mock_account(payouts_enabled=False)

        response = self.client.post(self.payout_url, {'amount': '100.00'})

        self.assertEqual(response.status_code, 302)
        payout = Payout.objects.count()
        self.assertEqual(payout, 0)

    @patch('tickets.views._compute_available_balance')
    @patch('tickets.views._compute_settled_payout_balance')
    @patch('tickets.views._get_stripe_platform_available_cents')
    @patch('stripe.Account.retrieve')
    @patch('stripe.Account.modify')
    @patch('stripe.Transfer.create_reversal')
    @patch('stripe.Payout.create')
    @patch('stripe.Transfer.create')
    def test_initiate_payout_reverses_transfer_when_bank_payout_creation_fails(
        self,
        mock_transfer_create,
        mock_payout_create,
        mock_create_reversal,
        mock_account_modify,
        mock_account_retrieve,
        mock_platform_available,
        mock_settled_balance,
        mock_available_balance,
    ):
        import stripe as stripe_lib

        mock_available_balance.return_value = (
            Decimal('500.00'),
            Decimal('50.00'),
            Decimal('0.00'),
            Decimal('450.00'),
        )
        mock_settled_balance.return_value = Decimal('450.00')
        mock_platform_available.return_value = 45000
        mock_account_retrieve.return_value = self._mock_account(payouts_enabled=True)
        mock_transfer_create.return_value = MagicMock(id='tr_fail')
        mock_payout_create.side_effect = stripe_lib.error.InvalidRequestError('bank unavailable', 'amount')

        response = self.client.post(self.payout_url, {'amount': '100.00'})

        self.assertEqual(response.status_code, 302)
        payout = Payout.objects.get(organization=self.org)
        self.assertEqual(payout.status, Payout.Status.FAILED)
        self.assertEqual(payout.stripe_transfer_id, 'tr_fail')
        self.assertIsNone(payout.stripe_payout_id)
        mock_create_reversal.assert_called_once()

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
    def test_connect_webhook_matches_oldest_open_payout_by_amount(self, mock_construct):
        first = Payout.objects.create(
            organization=self.org,
            amount=Decimal('25.00'),
            status=Payout.Status.PENDING,
        )
        second = Payout.objects.create(
            organization=self.org,
            amount=Decimal('25.00'),
            status=Payout.Status.PENDING,
        )
        mock_construct.return_value = {
            'type': 'payout.updated',
            'account': self.org.stripe_account_id,
            'data': {
                'object': {
                    'id': 'po_amount_match',
                    'status': 'in_transit',
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
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, Payout.Status.IN_TRANSIT)
        self.assertEqual(first.stripe_payout_id, 'po_amount_match')
        self.assertEqual(second.status, Payout.Status.PENDING)
        self.assertIsNone(second.stripe_payout_id)

    @patch('stripe.Account.retrieve')
    def test_finance_history_renders_processing_for_pending_payouts(self, mock_account_retrieve):
        mock_account_retrieve.return_value = self._mock_account(payouts_enabled=True)
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


class EventSummaryStreamTests(TestCase):
    """Test cases for the AI event summary streaming endpoint."""

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
        mock_chunk.content = 'Test summary'
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
        mock_chunk.content = 'Generated summary text'
        mock_instance.stream.return_value = [mock_chunk]
        mock_llm_cls.return_value = mock_instance

        response = self.client.post(self.url)
        # Consume the streaming response to trigger the generator
        list(response.streaming_content)

        self.event.refresh_from_db()
        self.assertEqual(self.event.ai_summary, 'Generated summary text')
        self.assertIsNotNone(self.event.ai_summary_generated_at)

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
        self.event.ai_summary = 'Test stored summary'
        self.event.ai_summary_generated_at = timezone.now()
        self.event.save(update_fields=['ai_summary', 'ai_summary_generated_at'])

        self.event.refresh_from_db()
        self.assertEqual(self.event.ai_summary, 'Test stored summary')
        self.assertIsNotNone(self.event.ai_summary_generated_at)

    @patch('langchain_openai.ChatOpenAI')
    def test_rate_limit_returns_429(self, mock_llm_cls):
        """After 10 requests, the endpoint returns 429."""
        mock_instance = MagicMock()
        mock_chunk = MagicMock()
        mock_chunk.content = 'Summary'
        mock_instance.stream.return_value = [mock_chunk]
        mock_llm_cls.return_value = mock_instance

        from django.core.cache import cache as django_cache
        rate_key = f"summary_ratelimit:{self.org.id}"
        django_cache.set(rate_key, 10, timeout=3600)

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 429)

        django_cache.delete(rate_key)

    def test_event_detail_still_works(self):
        """Regression: event_detail view still renders after stats extraction."""
        url = reverse('tickets:event_detail', args=[self.event.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Summary Event')

    def test_build_prompt_structure(self):
        """_build_prompt returns a string containing expected sections."""
        from tickets.services.event_summary import EventSummaryService
        from tickets.views import _compute_event_stats

        service = EventSummaryService(self.org)
        event_data = _compute_event_stats(self.event)
        prompt = service._build_prompt(self.event, event_data)

        self.assertIn('Event Results', prompt)
        self.assertIn('Attendee Feedback', prompt)
        self.assertIn('Financial Performance', prompt)
        self.assertIn('Summary Event', prompt)


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
        self.assertContains(response, 'Behavior Profile')
        self.assertContains(response, 'Steady Repeat')
        self.assertContains(response, 'Average days between orders')


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
            start_date=date(2026, 4, 18),
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
