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


class Organization(BaseModel):
    """Organization that owns venues, events, uploads, customers, and custom fields."""
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=100, unique=True)
    rfm_recalc_in_progress = models.BooleanField(default=False)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class PipedreamCalendarConnection(BaseModel):
    """Per-organization Pipedream webhook URL for Google Calendar event sync."""
    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name='pipedream_calendar_connection',
    )
    webhook_url = models.URLField(max_length=500)

    class Meta:
        verbose_name = 'Pipedream calendar connection'
        verbose_name_plural = 'Pipedream calendar connections'

    def __str__(self):
        return f"Pipedream calendar: {self.organization.name}"


class UserProfile(models.Model):
    """OneToOne profile linking a user to an organization."""
    user = models.OneToOneField(
        'auth.User',
        on_delete=models.CASCADE,
        related_name='profile',
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='members',
    )

    class Meta:
        verbose_name = "User profile"
        verbose_name_plural = "User profiles"

    def __str__(self):
        return f"{self.user.get_username()} ({self.organization or 'no org'})"


class OrganizationInvitation(BaseModel):
    """Invitation for a user to join an organization by email."""
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACCEPTED = 'accepted', 'Accepted'
        EXPIRED = 'expired', 'Expired'
        REVOKED = 'revoked', 'Revoked'

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='organization_invitations',
    )
    email = models.EmailField(db_index=True)
    invited_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sent_organization_invitations',
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='accepted_organization_invitations',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'status']),
        ]

    def __str__(self):
        return f"Invite {self.email} -> {self.organization.name} ({self.status})"

    def is_expired(self):
        return timezone.now() > self.expires_at

    def is_usable(self):
        return self.status == self.Status.PENDING and not self.is_expired()

    def clean(self):
        if self.status != self.Status.PENDING or not self.organization_id:
            return
        qs = OrganizationInvitation.objects.filter(
            organization_id=self.organization_id,
            email__iexact=self.email,
            status=self.Status.PENDING,
            expires_at__gt=timezone.now(),
        )
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        if qs.exists():
            raise ValidationError(
                f"An invitation for {self.email} is already pending for this organization."
            )


class EmailOTP(BaseModel):
    """One-time password for email verification (signup, etc.)."""
    class Purpose(models.TextChoices):
        SIGNUP = 'signup', 'Sign Up'

    email = models.EmailField(db_index=True)
    otp_code = models.CharField(max_length=6)
    purpose = models.CharField(max_length=20, choices=Purpose.choices, default=Purpose.SIGNUP)
    is_verified = models.BooleanField(default=False)
    attempts = models.PositiveSmallIntegerField(default=0)
    signup_data = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['email', 'purpose', '-created_at'])]

    def __str__(self):
        return f"OTP {self.email} ({self.purpose}) - {'verified' if self.is_verified else 'pending'}"

    def is_expired(self):
        return timezone.now() > self.created_at + timezone.timedelta(minutes=10)

    def is_usable(self):
        return not self.is_expired() and not self.is_verified and self.attempts < 5


class CSVFormat(AuditBaseModel):
    """Defines CSV file format configurations with column mappings."""
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='csv_formats',
    )
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
        if self.is_default and self.organization_id:
            # Check if another format is already default in this organization
            existing_default = CSVFormat.objects.filter(
                organization=self.organization_id, is_default=True
            ).exclude(id=self.id).first()
            if existing_default:
                raise ValidationError(
                    f"'{existing_default.name}' is already set as default. "
                    "Only one format can be default per organization."
                )
        
        # Tiers can only be used when manual pricing is required
        if self.uses_tiers and not self.requires_manual_pricing:
            raise ValidationError(
                "Tier-based pricing can only be enabled when manual pricing is required."
            )

    def save(self, *args, **kwargs):
        """Override save to ensure only one default format."""
        self.full_clean()
        # If setting this as default, unset others in the same organization
        if self.is_default and self.organization_id:
            CSVFormat.objects.filter(
                organization=self.organization_id, is_default=True
            ).exclude(id=self.id).update(is_default=False)
        super().save(*args, **kwargs)


class UploadedFile(AuditBaseModel):
    """Tracks uploaded CSV files and metadata."""
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='uploaded_files',
    )
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
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='customers',
    )
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
    # RFM segmentation (Recency, Frequency, Monetary)
    rfm_recency_score = models.IntegerField(null=True, blank=True, db_index=True)
    rfm_frequency_score = models.IntegerField(null=True, blank=True, db_index=True)
    rfm_monetary_score = models.IntegerField(null=True, blank=True, db_index=True)
    rfm_segment = models.CharField(max_length=30, blank=True, db_index=True)
    rfm_updated_at = models.DateTimeField(null=True, blank=True)

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
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='venues',
    )
    name = models.CharField(max_length=200, db_index=True)
    city = models.CharField(max_length=100, db_index=True)
    street_address = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=100, blank=True)
    capacity = models.IntegerField(
        null=True,
        blank=True,
        help_text="Total capacity for the venue (optional)",
    )

    class Meta:
        unique_together = [['organization', 'name', 'city']]
        ordering = ['name', 'city']
        indexes = [
            models.Index(fields=['name', 'city']),
        ]

    def get_display_address(self):
        """Return a single formatted line from street, state, postal_code, country.
        Empty when none of those are set, so templates can hide the address block.
        """
        parts = []
        if self.street_address:
            parts.append(self.street_address)
        line2 = ', '.join(
            p for p in [self.state, self.postal_code] if p
        )
        if line2:
            parts.append(line2)
        if self.country:
            parts.append(self.country)
        return ' | '.join(parts) if parts else ''

    def __str__(self):
        return f"{self.name}, {self.city}"


class Event(AuditBaseModel):
    """Event information."""
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='events',
    )
    name = models.CharField(max_length=200, db_index=True)
    venue = models.ForeignKey(
        'Venue',
        on_delete=models.PROTECT,
        related_name='events'
    )
    start_date = models.DateField(db_index=True)
    start_time = models.TimeField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    description = models.TextField(blank=True)
    capacity = models.IntegerField(
        null=True,
        blank=True,
        help_text="Total ticket capacity for the event (optional)"
    )
    ticket_link = models.URLField(max_length=500, blank=True)
    timezone = models.CharField(
        max_length=50,
        default='America/Los_Angeles',
        choices=[
            ('America/Los_Angeles', 'Pacific'),
            ('America/Denver', 'Mountain'),
            ('America/Chicago', 'Central'),
            ('America/New_York', 'Eastern'),
            ('America/Anchorage', 'Alaska'),
            ('Pacific/Honolulu', 'Hawaii'),
        ],
        help_text="Timezone where the event takes place",
    )
    google_calendar_event_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Google Calendar event ID after sync (create-only).",
    )

    class Meta:
        unique_together = [['organization', 'name', 'start_date']]
        ordering = ['-start_date', '-start_time', 'name']
        indexes = [
            models.Index(fields=['name', 'start_date']),
            models.Index(fields=['organization', '-start_date']),
        ]

    def __str__(self):
        return f"{self.name} - {self.venue.name}, {self.venue.city} ({self.start_date})"

    def get_associated_uploads(self):
        """Get all distinct uploads associated with this event via ticket orders."""
        return UploadedFile.objects.filter(
            ticket_orders__event=self
        ).distinct()

    def get_upload_count(self):
        """Get count of distinct uploads associated with this event."""
        return self.get_associated_uploads().count()


class EventExpense(AuditBaseModel):
    """Expense line item for an event."""
    CATEGORY_CHOICES = [
        ('talent', 'Talent / Artist Fees'),
        ('venue', 'Venue Rental'),
        ('production', 'Production / AV / Sound'),
        ('marketing', 'Marketing / Promotion'),
        ('staffing', 'Staffing / Security'),
        ('catering', 'Catering / Hospitality'),
        ('insurance', 'Insurance / Permits'),
        ('travel', 'Travel / Accommodation'),
        ('other', 'Other'),
    ]

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='expenses')
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, db_index=True)
    description = models.CharField(max_length=300)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    expense_date = models.DateField(null=True, blank=True, db_index=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-expense_date', '-created_at']
        indexes = [
            models.Index(fields=['event', 'category']),
            models.Index(fields=['event', '-expense_date']),
        ]

    def __str__(self):
        return f"{self.get_category_display()} - {self.description} (${self.amount})"


class IncomeSource(BaseModel):
    """User-defined income source type (e.g. Bar Splits, Merch) scoped per organization."""
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='income_sources',
    )
    name = models.CharField(max_length=100)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['order', 'name']
        unique_together = [['organization', 'name']]
        indexes = [
            models.Index(fields=['organization', 'order']),
        ]

    def __str__(self):
        return self.name


class EventIncome(AuditBaseModel):
    """Additional income line item for an event (e.g. Bar Splits $500, Merch $200)."""
    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name='additional_income',
    )
    income_source = models.ForeignKey(
        IncomeSource,
        on_delete=models.PROTECT,
        related_name='event_income_lines',
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    income_date = models.DateField(null=True, blank=True, db_index=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['income_source__order', 'income_source__name']
        indexes = [
            models.Index(fields=['event', 'income_source']),
            models.Index(fields=['event', '-income_date']),
        ]

    def __str__(self):
        return f"{self.income_source.name} - ${self.amount}"


class EventTalent(models.Model):
    """One talent entry in an event's lineup."""
    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name='talent_lineup',
    )
    name = models.CharField(max_length=200)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['order', 'name']

    def __str__(self):
        return self.name


class CustomField(models.Model):
    """Definition of a custom field (e.g. Event Type) shown as dropdown; scoped per organization."""
    FIELD_TYPE_CHOICES = [
        ('dropdown', 'Dropdown'),
    ]
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='custom_fields',
    )
    name = models.CharField(max_length=100)
    field_type = models.CharField(max_length=20, choices=FIELD_TYPE_CHOICES, default='dropdown')
    order = models.PositiveSmallIntegerField(default=0)
    required = models.BooleanField(
        default=False,
        help_text="When set, event form requires a value for this field.",
    )
    default_option = models.ForeignKey(
        'CustomFieldOption',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='default_for_custom_fields',
        help_text="Option to pre-select when creating a new event.",
    )

    class Meta:
        ordering = ['order', 'name']

    def __str__(self):
        return self.name


class CustomFieldOption(models.Model):
    """One option for a dropdown-type custom field."""
    custom_field = models.ForeignKey(
        CustomField,
        on_delete=models.CASCADE,
        related_name='options',
    )
    label = models.CharField(max_length=200)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['order', 'label']

    def __str__(self):
        return self.label


class EventCustomFieldValue(models.Model):
    """Stores the selected custom field value per event (e.g. Event Type = Day Party)."""
    event = models.ForeignKey(
        'Event',
        on_delete=models.CASCADE,
        related_name='custom_field_values',
    )
    custom_field = models.ForeignKey(
        CustomField,
        on_delete=models.CASCADE,
    )
    custom_field_option = models.ForeignKey(
        CustomFieldOption,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )

    class Meta:
        unique_together = [['event', 'custom_field']]

    def __str__(self):
        return f"{self.event} - {self.custom_field}: {self.custom_field_option}"


class TicketOrder(AuditBaseModel):
    """Ticket order records."""
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='ticket_orders'
    )
    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
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
            models.Index(fields=['order_date']),
            models.Index(fields=['customer', 'order_date']),
            models.Index(fields=['event', 'total_amount']),
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
            models.Index(fields=['ticket_order']),
            models.Index(fields=['tier']),
        ]

    def __str__(self):
        tier_info = f" [{self.tier_name}]" if self.tier_name else ""
        return f"{self.ticket_type}{tier_info} - ${self.price} (Order: {self.ticket_order.order_number})"


class ChatMessage(BaseModel):
    """Persisted chat message for the AI chat agent."""
    ROLE_CHOICES = [
        ('user', 'User'),
        ('assistant', 'Assistant'),
        ('system', 'System'),
        ('tool', 'Tool'),
    ]

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='chat_messages',
    )
    user = models.ForeignKey(
        'auth.User',
        on_delete=models.CASCADE,
        related_name='chat_messages',
    )
    conversation_id = models.UUIDField(db_index=True)
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField(blank=True)
    tool_name = models.CharField(max_length=100, blank=True)
    tool_call_id = models.CharField(max_length=100, blank=True)
    token_count = models.IntegerField(default=0)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['organization', 'user', 'conversation_id', 'created_at']),
        ]

    def __str__(self):
        return f"[{self.role}] {self.content[:60]}"


class SurveyQuestion(BaseModel):
    """A question in a post-event survey."""
    QUESTION_TYPE_CHOICES = [
        ('star_rating', 'Star Rating (1-5)'),
        ('nps', 'NPS Score (0-10)'),
        ('text', 'Free Text'),
    ]

    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='survey_questions',
        help_text="Null = not event-specific (org default or system default)",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='survey_questions',
        help_text="Null = system default question",
    )
    question_text = models.CharField(max_length=500)
    question_type = models.CharField(max_length=20, choices=QUESTION_TYPE_CHOICES)
    position = models.PositiveSmallIntegerField(default=0)
    is_required = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['position', 'created_at']
        indexes = [
            models.Index(fields=['event', 'position']),
            models.Index(fields=['organization', 'position']),
        ]

    def __str__(self):
        scope = "System"
        if self.event_id:
            scope = f"Event: {self.event_id}"
        elif self.organization_id:
            scope = f"Org: {self.organization_id}"
        return f"[{scope}] {self.question_text[:60]}"


class SurveyInvitation(BaseModel):
    """Tracks a survey invitation sent to a customer for an event."""
    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name='survey_invitations',
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='survey_invitations',
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='survey_invitations',
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    email = models.EmailField(help_text="Denormalized from customer at send time")
    sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [['event', 'customer']]
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['event', 'sent_at']),
        ]

    def __str__(self):
        status = "completed" if self.completed_at else ("sent" if self.sent_at else "pending")
        return f"Survey invite: {self.customer} for {self.event} ({status})"


class SurveyResponse(BaseModel):
    """A completed survey response from a customer."""
    invitation = models.OneToOneField(
        SurveyInvitation,
        on_delete=models.CASCADE,
        related_name='response',
    )
    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name='survey_responses',
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='survey_responses',
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='survey_responses',
    )
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-submitted_at']
        indexes = [
            models.Index(fields=['event', '-submitted_at']),
        ]

    def __str__(self):
        return f"Response from {self.customer} for {self.event}"


class SurveyAnswer(BaseModel):
    """An individual answer to a survey question within a response."""
    response = models.ForeignKey(
        SurveyResponse,
        on_delete=models.CASCADE,
        related_name='answers',
    )
    question = models.ForeignKey(
        SurveyQuestion,
        on_delete=models.CASCADE,
        related_name='answers',
    )
    star_rating = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="1-5 star rating",
    )
    nps_score = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="0-10 NPS score",
    )
    text_answer = models.TextField(blank=True)

    class Meta:
        unique_together = [['response', 'question']]
        ordering = ['question__position']

    def __str__(self):
        return f"Answer to '{self.question.question_text[:40]}' by {self.response.customer}"
