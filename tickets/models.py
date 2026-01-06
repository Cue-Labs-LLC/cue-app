import uuid
from decimal import Decimal
from django.db import models
from django.db.models import Sum, Max
from django.utils import timezone
from django.core.exceptions import ValidationError


class BaseModel(models.Model):
    """Base model with UUID primary key and timestamps."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditBaseModel(BaseModel):
    """Base model with audit trail fields."""
    created_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='%(class)s_created',
        editable=False
    )
    updated_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='%(class)s_updated',
        editable=False
    )
    version = models.IntegerField(default=1, editable=False)
    deleted_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False):
        """Soft delete by setting deleted_at timestamp."""
        self.deleted_at = timezone.now()
        self.save(update_fields=['deleted_at'])

    def restore(self):
        """Restore soft-deleted object."""
        self.deleted_at = None
        self.save(update_fields=['deleted_at'])

    def hard_delete(self, using=None, keep_parents=False):
        """Permanent delete."""
        super().delete(using=using, keep_parents=keep_parents)


class CSVFormat(AuditBaseModel):
    """Defines CSV file format configurations with column mappings."""
    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    is_default = models.BooleanField(default=False)
    requires_manual_pricing = models.BooleanField(
        default=False,
        help_text="If True, CSV lacks price/total columns and requires manual price entry"
    )
    uses_tiers = models.BooleanField(
        default=False,
        help_text="If True, this format uses tier-based pricing with allotments. Only applicable when requires_manual_pricing is True."
    )
    column_mapping = models.JSONField(
        help_text="JSON mapping of CSV column names to internal field names"
    )

    class Meta:
        verbose_name = "CSV Format"
        verbose_name_plural = "CSV Formats"
        ordering = ['-is_default', 'name']

    def __str__(self):
        default_marker = " (Default)" if self.is_default else ""
        return f"{self.name}{default_marker}"

    def clean(self):
        """Validate format configuration."""
        if self.is_default:
            # Check if another format is already default
            existing_default = CSVFormat.objects.filter(
                is_default=True
            ).exclude(id=self.id).first()
            if existing_default:
                raise ValidationError(
                    f"'{existing_default.name}' is already set as default. "
                    "Only one format can be default at a time."
                )
        
        # Tiers can only be used when manual pricing is required
        if self.uses_tiers and not self.requires_manual_pricing:
            raise ValidationError(
                "Tier-based pricing can only be enabled when manual pricing is required."
            )

    def save(self, *args, **kwargs):
        """Override save to ensure only one default format."""
        self.full_clean()
        # If setting this as default, unset others
        if self.is_default:
            CSVFormat.objects.filter(is_default=True).exclude(id=self.id).update(is_default=False)
        super().save(*args, **kwargs)


class UploadedFile(AuditBaseModel):
    """Tracks uploaded CSV files and metadata."""
    csv_format = models.ForeignKey(
        CSVFormat,
        on_delete=models.PROTECT,
        related_name='uploaded_files'
    )
    filename = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    source = models.CharField(max_length=200, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=50,
        choices=[
            ('pending', 'Pending'),
            ('processing', 'Processing'),
            ('completed', 'Completed'),
            ('failed', 'Failed'),
        ],
        default='pending'
    )
    total_rows = models.IntegerField(default=0)
    processed_rows = models.IntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "Uploaded File"
        verbose_name_plural = "Uploaded Files"
        ordering = ['-uploaded_at']

    def __str__(self):
        return f"{self.filename} ({self.status})"


class Customer(BaseModel):
    """Customer information with lifetime value tracking."""
    email = models.EmailField(unique=True, db_index=True)
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=50, blank=True)
    lifetime_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        db_index=True,
        help_text="Total amount spent across all orders"
    )
    last_order_date = models.DateField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ['-lifetime_value', 'name']
        indexes = [
            models.Index(fields=['email']),
            models.Index(fields=['lifetime_value']),
            models.Index(fields=['last_order_date']),
        ]

    def __str__(self):
        return f"{self.name} ({self.email})"

    def clean_email(self):
        """Normalize email format (lowercase, trim)."""
        if self.email:
            return self.email.lower().strip()
        return self.email

    def calculate_lifetime_value(self):
        """Calculate LTV from all associated ticket orders."""
        result = self.ticket_orders.aggregate(
            total=Sum('total_amount')
        )
        return result['total'] or Decimal('0.00')

    def update_lifetime_value(self):
        """Recalculate and save LTV."""
        self.lifetime_value = self.calculate_lifetime_value()
        # Update last_order_date
        last_order = self.ticket_orders.order_by('-order_date').first()
        if last_order:
            self.last_order_date = last_order.order_date.date()
        self.save(update_fields=['lifetime_value', 'last_order_date'])

    def save(self, *args, **kwargs):
        """Override save to normalize email."""
        self.email = self.clean_email()
        super().save(*args, **kwargs)


class Venue(BaseModel):
    """Venue information for events."""
    name = models.CharField(max_length=200, db_index=True)
    city = models.CharField(max_length=100, db_index=True)
    
    class Meta:
        unique_together = [['name', 'city']]
        ordering = ['name', 'city']
        indexes = [
            models.Index(fields=['name', 'city']),
        ]
    
    def __str__(self):
        return f"{self.name}, {self.city}"


class Event(AuditBaseModel):
    """Event information."""
    name = models.CharField(max_length=200, db_index=True)
    venue = models.ForeignKey(
        'Venue',
        on_delete=models.PROTECT,
        related_name='events'
    )
    event_date = models.DateTimeField(db_index=True)
    description = models.TextField(blank=True)

    class Meta:
        unique_together = [['name', 'event_date']]
        ordering = ['-event_date', 'name']
        indexes = [
            models.Index(fields=['name', 'event_date']),
        ]

    def __str__(self):
        return f"{self.name} - {self.venue.name}, {self.venue.city} ({self.event_date.date()})"


class TicketOrder(AuditBaseModel):
    """Ticket order records."""
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='ticket_orders'
    )
    event = models.ForeignKey(
        Event,
        on_delete=models.PROTECT,
        related_name='ticket_orders'
    )
    uploaded_file = models.ForeignKey(
        UploadedFile,
        on_delete=models.PROTECT,
        related_name='ticket_orders',
        null=True,
        blank=True
    )
    order_number = models.CharField(max_length=100, unique=True, db_index=True)
    order_date = models.DateTimeField(db_index=True)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        ordering = ['-order_date']
        indexes = [
            models.Index(fields=['order_number']),
            models.Index(fields=['order_date']),
            models.Index(fields=['customer', 'order_date']),
        ]

    def __str__(self):
        return f"Order {self.order_number} - {self.customer.name}"


class TicketTier(BaseModel):
    """Pricing tier definitions for ticket types with allotments."""
    ticket_type = models.CharField(max_length=100, db_index=True)
    name = models.CharField(max_length=100, help_text="Tier name (e.g., 'Early Bird', 'Regular')")
    price = models.DecimalField(max_digits=10, decimal_places=2)
    allotment = models.IntegerField(help_text="Maximum number of tickets available in this tier")
    order = models.IntegerField(help_text="Priority/order (1 = first tier, 2 = second, etc.)")
    uploaded_file = models.ForeignKey(
        UploadedFile,
        on_delete=models.CASCADE,
        related_name='ticket_tiers'
    )
    tickets_assigned = models.IntegerField(
        default=0,
        help_text="Number of tickets already assigned to this tier"
    )

    class Meta:
        ordering = ['ticket_type', 'order']
        indexes = [
            models.Index(fields=['ticket_type', 'order']),
            models.Index(fields=['uploaded_file']),
        ]
        unique_together = [['uploaded_file', 'ticket_type', 'order']]

    def __str__(self):
        return f"{self.ticket_type} - {self.name} (${self.price}, {self.tickets_assigned}/{self.allotment})"

    def is_available(self):
        """Check if tier has available capacity."""
        return self.tickets_assigned < self.allotment

    def remaining_capacity(self):
        """Get remaining ticket capacity."""
        return max(0, self.allotment - self.tickets_assigned)


class Ticket(BaseModel):
    """Individual tickets within orders."""
    ticket_order = models.ForeignKey(
        TicketOrder,
        on_delete=models.CASCADE,
        related_name='tickets'
    )
    ticket_type = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    tier = models.ForeignKey(
        TicketTier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tickets',
        help_text="Assigned pricing tier (if tier-based pricing is used)"
    )
    tier_name = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Denormalized tier name for display/querying"
    )

    class Meta:
        ordering = ['ticket_order', 'ticket_type']
        indexes = [
            models.Index(fields=['tier']),
        ]

    def __str__(self):
        tier_info = f" [{self.tier_name}]" if self.tier_name else ""
        return f"{self.ticket_type}{tier_info} - ${self.price} (Order: {self.ticket_order.order_number})"
