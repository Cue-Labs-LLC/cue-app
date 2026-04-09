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

    def test_post_creates_user_and_sets_phone(self):
        """Valid POST creates User + UserProfile with phone_number set."""
        session = self.client.session
        session['pending_signup_email'] = 'newuser@example.com'
        session.save()
        response = self.client.post(reverse('tickets:email_complete_profile'), {
            'first_name': 'Jane',
            'last_name': 'Doe',
            'phone_number': '+12125551234',
            'email_display': 'newuser@example.com',
            'gender': 'female',
            'terms_accepted': True,
        })
        self.assertRedirects(response, reverse('tickets:attendee_dashboard'), fetch_redirect_response=False)
        user = User.objects.get(email='newuser@example.com')
        self.assertEqual(user.first_name, 'Jane')
        profile = UserProfile.objects.get(user=user)
        self.assertEqual(profile.phone_number, '+12125551234')

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
