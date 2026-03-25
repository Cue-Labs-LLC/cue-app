import uuid
from datetime import date, time, datetime, timedelta
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth.models import User
from django.utils import timezone
from decimal import Decimal
from rest_framework.authtoken.models import Token
from .models import (
    UploadedFile, TicketOrder, Customer, Event,
    Venue, CSVFormat, Ticket, TicketTier,
    Organization, UserProfile, OrganizationInvitation, ChatMessage,
    SaleableTicketType, StripeCheckoutSession,
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
