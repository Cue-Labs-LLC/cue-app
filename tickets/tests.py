from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth.models import User
from decimal import Decimal
from .models import (
    UploadedFile, TicketOrder, Customer, Event,
    Venue, CSVFormat, Ticket, TicketTier
)


class UploadDeleteViewTests(TestCase):
    """Test cases for the upload_delete view."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@test.com',
            password='testpass123'
        )
        self.client.login(username='testuser', password='testpass123')

        # Create required related objects
        self.csv_format = CSVFormat.objects.create(
            name='Test Format',
            column_mapping={'order_number': 'Order ID'}
        )
        self.venue = Venue.objects.create(
            name='Test Venue',
            city='Test City'
        )
        self.event = Event.objects.create(
            name='Test Event',
            venue=self.venue,
            event_date='2024-06-15 19:00:00'
        )
        self.upload = UploadedFile.objects.create(
            csv_format=self.csv_format,
            filename='test_upload.csv',
            status='completed',
            total_rows=10,
            processed_rows=10
        )
        self.customer = Customer.objects.create(
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

    def test_delete_recalculates_customer_ltv(self):
        """Test that customer LTV is recalculated after deletion."""
        # Customer starts with LTV matching the order
        self.assertEqual(self.customer.lifetime_value, Decimal('150.00'))

        response = self.client.post(
            reverse('tickets:upload_delete', args=[self.upload.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        self.assertEqual(response.status_code, 200)

        # Refresh customer and check LTV is recalculated
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.lifetime_value, Decimal('0.00'))

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
