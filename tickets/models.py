import uuid
import secrets
import string
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.db.models import Sum, Max
from django.utils import timezone
from django.core.exceptions import ValidationError


def _get_media_storage():
    """Resolve media storage at runtime so S3 is used when DEFAULT_FILE_STORAGE is set (avoids boot-time resolution using FileSystemStorage)."""
    if not hasattr(_get_media_storage, '_cached'):
        import importlib
        backend = getattr(settings, 'DEFAULT_FILE_STORAGE', 'django.core.files.storage.FileSystemStorage')
        module_path, _, class_name = backend.rpartition('.')
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        _get_media_storage._cached = cls()
    return _get_media_storage._cached


def _event_flyer_upload_to(instance, filename):
    return f"orgs/{instance.organization.slug}/event_flyers/{filename}"


def _csv_upload_to(instance, filename):
    org_slug = instance.organization.slug if instance.organization_id else 'unknown'
    return f"orgs/{org_slug}/csv_uploads/{instance.id}_{filename}"


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
    stripe_account_id = models.CharField(
        max_length=255,
        blank=True,
        help_text='Stripe Connect Express account ID (acct_xxx).',
    )
    stripe_onboarding_complete = models.BooleanField(
        default=False,
        help_text='True after Stripe confirms details_submitted, charges_enabled, and payouts_enabled.',
    )
    meta_capi_access_token = models.CharField(
        max_length=255, blank=True, default='',
        help_text='Meta Conversions API access token for server-side event reporting.',
    )
    waitlist_feature_enabled = models.BooleanField(
        default=False,
        help_text='Enable the waitlist feature for this organization.',
    )
    photo = models.ImageField(
        upload_to='org_photos/',
        storage=_get_media_storage,
        blank=True,
        null=True,
    )
    description = models.TextField(
        max_length=500,
        blank=True,
        default='',
    )
    website = models.URLField(
        max_length=255,
        blank=True,
        default='',
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class OrderCounter(models.Model):
    """Global atomic counter for sequential order numbers."""
    last_number = models.PositiveIntegerField(default=0)

    @classmethod
    def next(cls):
        """Atomically increment and return the next order number. Call inside a transaction."""
        counter, _ = cls.objects.select_for_update().get_or_create(pk=1)
        counter.last_number += 1
        counter.save(update_fields=['last_number'])
        return counter.last_number


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


def _generate_api_key():
    return f"cue_live_{secrets.token_hex(16)}"


class OrganizationAPIKey(BaseModel):
    """Per-organization API key for external AI agent access."""
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='api_keys',
    )
    name = models.CharField(max_length=100, help_text="Label to identify this key, e.g. 'Instagram DM Agent'")
    key = models.CharField(max_length=100, unique=True, default=_generate_api_key, db_index=True)
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['key', 'is_active']),
        ]
        verbose_name = 'Organization API key'
        verbose_name_plural = 'Organization API keys'

    def __str__(self):
        return f"{self.organization.name} — {self.name}"

    @property
    def masked_key(self):
        return f"{self.key[:14]}...{self.key[-4:]}"


class FeatureFlagSettings(models.Model):
    """Singleton model for global feature flags managed from Django admin."""

    singleton_enforcer = models.BooleanField(default=True, unique=True, editable=False)
    direct_ticketing_enabled = models.BooleanField(
        default=True,
        help_text='Enable direct ticket selling flows for allowed users.',
    )
    browse_events_enabled = models.BooleanField(
        default=False,
        help_text='Expose the public Browse Events experience.',
    )
    smart_pricing_recommendations_enabled = models.BooleanField(
        default=False,
        help_text='Enable Smart Pricing Recommendations on direct-ticketing events.',
    )

    class Meta:
        verbose_name = 'Feature Flag Settings'
        verbose_name_plural = 'Feature Flag Settings'

    def __str__(self):
        return 'Feature Flag Settings'

    @classmethod
    def get_solo(cls):
        return cls.objects.get_or_create(singleton_enforcer=True)[0]


class UserProfile(models.Model):
    """OneToOne profile linking a user to an organization."""

    class Role(models.TextChoices):
        ORGANIZER = 'organizer', 'Organizer'
        ATTENDEE  = 'attendee',  'Attendee'

    class OrgRole(models.TextChoices):
        OWNER    = 'owner',    'Owner'
        ADMIN    = 'admin',    'Admin'
        HOST     = 'host',     'Host'
        DOORMAN  = 'doorman',  'Doorman'

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
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.ORGANIZER,
        db_index=True,
    )
    org_role = models.CharField(
        max_length=20,
        choices=OrgRole.choices,
        null=True,
        blank=True,
        db_index=True,
    )
    phone_number = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        unique=True,
        db_index=True,
    )

    class Gender(models.TextChoices):
        MALE   = 'male',   'Male'
        FEMALE = 'female', 'Female'
        OTHER  = 'other',  'Other'

    gender = models.CharField(
        max_length=10,
        choices=Gender.choices,
        blank=True,
        null=True,
    )
    marketing_opt_in = models.BooleanField(default=False)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)

    stripe_customer_id = models.CharField(
        max_length=255, blank=True, null=True, db_index=True,
        help_text="Stripe Customer ID (cus_xxx).",
    )
    stripe_pm_id = models.CharField(
        max_length=255, blank=True, null=True,
        help_text="Saved Stripe PaymentMethod ID (pm_xxx).",
    )
    stripe_pm_brand = models.CharField(
        max_length=50, blank=True,
        help_text="Card brand, e.g. 'visa'.",
    )
    stripe_pm_last4 = models.CharField(
        max_length=4, blank=True,
        help_text="Last 4 digits of saved card.",
    )

    class Meta:
        verbose_name = "User profile"
        verbose_name_plural = "User profiles"

    def __str__(self):
        return f"{self.user.get_username()} ({self.organization or 'no org'})"

    @property
    def is_organizer(self):
        return self.role == self.Role.ORGANIZER

    @property
    def is_attendee(self):
        return True  # all users have attendee abilities; organizers are a superset

    @property
    def is_org_owner(self):
        return self.org_role == self.OrgRole.OWNER

    @property
    def is_org_admin(self):
        return self.org_role in (self.OrgRole.OWNER, self.OrgRole.ADMIN)

    @property
    def is_org_host(self):
        return self.org_role in (self.OrgRole.OWNER, self.OrgRole.ADMIN, self.OrgRole.HOST)


class OrganizationMembership(BaseModel):
    """Per-organization role for a user. Supports multi-org: one row per user+org pair."""
    user = models.ForeignKey(
        'auth.User',
        on_delete=models.CASCADE,
        related_name='org_memberships',
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='memberships',
    )
    org_role = models.CharField(
        max_length=20,
        choices=UserProfile.OrgRole.choices,
        null=True,
        blank=True,
        db_index=True,
    )

    class Meta:
        unique_together = ('user', 'organization')
        verbose_name = 'Organization membership'
        verbose_name_plural = 'Organization memberships'

    def __str__(self):
        return f"{self.user.get_username()} @ {self.organization.name} ({self.org_role})"


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
    role = models.CharField(
        max_length=20,
        choices=UserProfile.Role.choices,
        default=UserProfile.Role.ORGANIZER,
    )
    org_role = models.CharField(
        max_length=20,
        choices=UserProfile.OrgRole.choices,
        default=UserProfile.OrgRole.HOST,
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


class PhoneOTP(BaseModel):
    """One-time password for phone number verification (attendee signup/login)."""
    class Purpose(models.TextChoices):
        SIGNUP = 'signup', 'Signup'
        LOGIN  = 'login',  'Login'

    phone_number = models.CharField(max_length=20, db_index=True)
    otp_code     = models.CharField(max_length=6)
    purpose      = models.CharField(max_length=20, choices=Purpose.choices)
    is_verified  = models.BooleanField(default=False)
    attempts     = models.PositiveSmallIntegerField(default=0)
    signup_data  = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['phone_number', 'purpose', '-created_at'])]

    def __str__(self):
        return f"PhoneOTP {self.phone_number} ({self.purpose}) - {'verified' if self.is_verified else 'pending'}"

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
    csv_file = models.FileField(
        upload_to=_csv_upload_to,
        storage=_get_media_storage,
        blank=True,
        null=True,
    )
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
    behavior_profile = models.CharField(max_length=40, blank=True, db_index=True)
    behavior_profile_reason = models.CharField(max_length=255, blank=True)
    days_since_last_order = models.IntegerField(null=True, blank=True)
    avg_days_between_orders = models.IntegerField(null=True, blank=True)
    days_to_second_order = models.IntegerField(null=True, blank=True)
    tags = models.ManyToManyField('CustomerTag', blank=True, related_name='customers')
    sms_opt_in = models.BooleanField(default=False)
    sms_opt_in_date = models.DateTimeField(null=True, blank=True)

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
        """Calculate LTV from all associated ticket orders (excludes refunded)."""
        result = self.ticket_orders.filter(refunded_at__isnull=True).aggregate(
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


class CustomerTag(BaseModel):
    """Organization-scoped label for customers (e.g. 'VIP', 'Press', 'Comp List')."""
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='customer_tags',
    )
    name = models.CharField(max_length=50)
    color = models.CharField(
        max_length=20,
        default='blue',
        choices=[
            ('blue', 'Blue'),
            ('green', 'Green'),
            ('red', 'Red'),
            ('yellow', 'Yellow'),
            ('purple', 'Purple'),
            ('orange', 'Orange'),
        ],
    )

    class Meta:
        unique_together = [('organization', 'name')]
        ordering = ['name']

    def __str__(self):
        return self.name


class Venue(BaseModel):
    """Venue information for events."""
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='venues',
    )
    name = models.CharField(max_length=200, db_index=True)
    city = models.CharField(max_length=100, blank=True, db_index=True)
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

    _ADDRESS_FIELDS = ('street_address', 'city', 'state', 'postal_code', 'country')

    def save(self, *args, **kwargs):
        from .address_utils import normalize_venue_address_fields, geocode_venue_address
        normalize_venue_address_fields(self)

        from django.conf import settings
        api_key = getattr(settings, 'GOOGLE_MAPS_API_KEY', '')
        if api_key and self.street_address and not self._address_fields_unchanged():
            geocode_venue_address(self, api_key)

        super().save(*args, **kwargs)

    def _address_fields_unchanged(self):
        """Return True if this is an update and no address field has changed vs DB."""
        if self._state.adding or not self.pk:
            return False
        try:
            old = Venue.objects.filter(pk=self.pk).values(*self._ADDRESS_FIELDS).first()
            return bool(old and all(getattr(self, f) == old.get(f, '') for f in self._ADDRESS_FIELDS))
        except Exception:
            return False

    def __str__(self):
        return f"{self.name}, {self.city}"


TICKETING_TYPE_DIRECT = 'direct'
TICKETING_TYPE_EXTERNAL = 'external'
TICKETING_TYPE_CHOICES = [
    (TICKETING_TYPE_DIRECT, 'Direct (sell tickets on this platform)'),
    (TICKETING_TYPE_EXTERNAL, 'External (upload CSV ticket data)'),
]

EVENT_STATUS_DRAFT = 'draft'
EVENT_STATUS_LIVE = 'live'
EVENT_STATUS_ENDED = 'ended'
EVENT_STATUS_CANCELLED = 'cancelled'
EVENT_STATUS_CHOICES = [
    (EVENT_STATUS_DRAFT, 'Draft'),
    (EVENT_STATUS_LIVE, 'Live'),
    (EVENT_STATUS_ENDED, 'Ended'),
    (EVENT_STATUS_CANCELLED, 'Cancelled'),
]


def generate_event_public_id():
    """Generate a random 10-char alphanumeric ID for public-facing event URLs."""
    return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))


class Event(AuditBaseModel):
    """Event information."""
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='events',
    )
    name = models.CharField(max_length=200, db_index=True)
    summary = models.CharField(max_length=500, blank=True)
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
    flyer = models.ImageField(upload_to=_event_flyer_upload_to, blank=True, null=True, storage=_get_media_storage)
    capacity = models.IntegerField(
        null=True,
        blank=True,
        help_text="Total ticket capacity for the event (optional)"
    )
    ticket_link = models.URLField(max_length=500, blank=True)
    ticketing_type = models.CharField(
        max_length=20,
        choices=TICKETING_TYPE_CHOICES,
        default=TICKETING_TYPE_EXTERNAL,
    )
    status = models.CharField(
        max_length=20,
        choices=EVENT_STATUS_CHOICES,
        default=EVENT_STATUS_DRAFT,
        db_index=True,
        help_text="Publication state; only meaningful for direct ticketing events.",
    )
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
    facebook_pixel_id = models.CharField(
        max_length=20,
        blank=True,
        default='',
        help_text="Facebook Pixel ID for conversion tracking on the public ticketing flow.",
    )
    public_buy_page_views = models.PositiveIntegerField(
        default=0,
        help_text="Number of times the public ticket page (/e/<id>/) was loaded.",
    )
    scanner_pin = models.CharField(
        max_length=8, null=True, blank=True, unique=True, db_index=True,
        help_text="6-digit PIN for guest scanner access (no Cue account required).",
    )
    public_id = models.CharField(
        max_length=10,
        unique=True,
        db_index=True,
        default=generate_event_public_id,
        editable=False,
        help_text="Short public-facing ID used in shareable URLs (/e/<public_id>/).",
    )
    ai_summary = models.TextField(blank=True, default='')
    ai_summary_generated_at = models.DateTimeField(null=True, blank=True)
    computed_total_revenue = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="Denormalized sum of ticket revenue + additional income; maintained by signals.",
    )
    cached_ticket_count = models.IntegerField(
        default=0,
        help_text="Denormalized total ticket count; maintained by signals and CSV import.",
    )
    cached_paid_ticket_count = models.IntegerField(
        default=0,
        help_text="Denormalized count of non-refunded paid tickets; maintained by signals and CSV import.",
    )
    cached_paid_ticket_sum = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="Denormalized sum of non-refunded paid ticket prices; maintained by signals and CSV import.",
    )

    class Meta:
        ordering = ['-start_date', '-start_time', 'name']
        indexes = [
            models.Index(fields=['name', 'start_date']),
            models.Index(fields=['organization', '-start_date']),
            models.Index(fields=['ticketing_type', 'status', 'start_date']),
        ]

    def __str__(self):
        return f"{self.name} - {self.venue.name}, {self.venue.city} ({self.start_date})"

    @property
    def effective_status(self):
        if self.ticketing_type != TICKETING_TYPE_DIRECT or self.status in (
            EVENT_STATUS_DRAFT, EVENT_STATUS_ENDED, EVENT_STATUS_CANCELLED
        ):
            return self.status
        # status == 'live' - check if event date has passed
        today = timezone.now().date()
        end = self.end_date or self.start_date
        if end < today:
            return EVENT_STATUS_ENDED
        return EVENT_STATUS_LIVE

    def get_associated_uploads(self):
        """Get all distinct uploads associated with this event via ticket orders."""
        return UploadedFile.objects.filter(
            ticket_orders__event=self
        ).distinct()

    def get_upload_count(self):
        """Get count of distinct uploads associated with this event."""
        return self.get_associated_uploads().count()


def generate_unique_scanner_pin():
    """Return a unique 6-digit numeric PIN not already used by any Event."""
    import random
    import string
    while True:
        pin = ''.join(random.choices(string.digits, k=6))
        if not Event.objects.filter(scanner_pin=pin).exists():
            return pin


class ScannerSession(BaseModel):
    """Issued when a guest logs in with an event's scanner PIN."""
    event = models.ForeignKey('Event', on_delete=models.CASCADE, related_name='scanner_sessions')
    token = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [models.Index(fields=['token', 'is_active'])]

    def __str__(self):
        return f"ScannerSession {self.token} ({'active' if self.is_active else 'inactive'})"


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
            models.Index(fields=['event', 'deleted_at']),
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
            models.Index(fields=['event', 'deleted_at']),
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
    external_order_number = models.CharField(max_length=100, blank=True, db_index=True)
    order_date = models.DateTimeField(db_index=True)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    refunded_at = models.DateTimeField(null=True, blank=True)
    promo_code = models.ForeignKey(
        'PromoCode',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='ticket_orders',
    )
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    is_in_person = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Order was processed in person (no customer identity)."
    )
    checked_in_at = models.DateTimeField(null=True, blank=True, db_index=True)
    checked_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='checkins',
    )

    class Meta:
        ordering = ['-order_date']
        indexes = [
            models.Index(fields=['order_date']),
            models.Index(fields=['customer', 'order_date']),
            models.Index(fields=['customer', 'refunded_at']),
            models.Index(fields=['event', 'total_amount']),
            models.Index(fields=['event', 'is_in_person']),
            models.Index(fields=['event', 'refunded_at'], name='tktorder_event_refund_idx'),
            # Covers the has_uploads EXISTS check: only indexes non-NULL uploaded_file_id rows
            models.Index(fields=['event', 'uploaded_file'], name='tktorder_event_upload_idx'),
            # Covers paginated orders query: filter by event + sort by order_date
            models.Index(fields=['event', 'order_date'], name='tktorder_event_orderdate_idx'),
        ]

    @property
    def display_order_number(self):
        """Returns external_order_number for CSV orders, order_number for direct orders."""
        return self.external_order_number if self.external_order_number else self.order_number

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
            models.Index(fields=['ticket_order', 'price'], name='tkt_ticketorder_price_idx'),
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


class SaleableTicketType(BaseModel):
    """Organizer-configured, per-event product catalog for direct ticket selling."""
    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name='saleable_ticket_types',
    )
    name = models.CharField(max_length=200, help_text="Buyer-facing name, e.g. 'General Admission'")
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="USD price; 0.00 = free",
    )
    quantity_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Max tickets available; null = unlimited",
    )
    quantity_sold = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True, help_text="Hidden from public page when False")
    is_password_protected = models.BooleanField(
        default=False,
        help_text="If checked, this ticket type is hidden until a customer enters the password below.",
    )
    sale_start = models.DateTimeField(null=True, blank=True)
    sale_end = models.DateTimeField(null=True, blank=True)
    description = models.TextField(blank=True)
    order = models.PositiveSmallIntegerField(default=0, help_text="Display order on public page")
    password = models.CharField(
        max_length=100,
        blank=True,
        help_text="If set, this ticket type is hidden on the public page until a customer enters this password.",
    )
    unlocks_after = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='unlocked_by',
        help_text='This ticket type will be shown but disabled until the selected ticket type sells out.',
    )
    waitlist_enabled = models.BooleanField(
        default=False,
        help_text='Allow buyers to join a waitlist when this ticket type is sold out.',
    )
    quantity_held = models.PositiveIntegerField(
        default=0,
        help_text='Spots temporarily held for waitlist notifications. Counted as sold for availability purposes.',
    )

    class Meta:
        ordering = ['order', 'name']
        indexes = [
            models.Index(fields=['event', 'is_active']),
            models.Index(fields=['event', 'order']),
        ]

    def __str__(self):
        return f"{self.name} (${self.price})"

    def is_on_sale(self):
        """True if currently within the optional sale window (or no window set)."""
        now = timezone.now()
        if self.sale_start and now < self.sale_start:
            return False
        if self.sale_end and now > self.sale_end:
            return False
        return True

    def remaining_quantity(self):
        """Remaining tickets; None means unlimited."""
        if self.quantity_limit is None:
            return None
        return max(0, self.quantity_limit - self.quantity_sold)

    def get_active_tier(self):
        """First available tier by order. Uses prefetch cache - no extra query."""
        for tier in self.tiers.all():
            if tier.is_available():
                return tier
        return None

    @property
    def effective_price(self):
        active = self.get_active_tier()
        return active.price if active else self.price

    @property
    def gross_price(self):
        """Estimated organizer net for a single-ticket order (Display Price minus extracted fee)."""
        from tickets.utils import extract_fee_from_display_cents
        price = self.effective_price
        if price == 0:
            return Decimal('0.00')
        display_cents = int(price * 100)
        fee_cents = extract_fee_from_display_cents(display_cents)
        return (Decimal(display_cents - fee_cents) / 100).quantize(Decimal('0.01'))

    def is_sold_out(self):
        """True only when a limit is set and fully exhausted (including held spots)."""
        tiers = list(self.tiers.all())  # prefetch-safe
        if tiers:
            return not any(t.is_available() for t in tiers)
        if self.quantity_limit is None:
            return False
        return (self.quantity_sold + self.quantity_held) >= self.quantity_limit

    def is_unlocked(self):
        """True if no prerequisite, or the prerequisite ticket type is sold out."""
        if self.unlocks_after_id is None:
            return True
        return self.unlocks_after.is_sold_out()


class SaleableTicketTypeTier(BaseModel):
    """Tiered pricing for a SaleableTicketType (Early Bird, Regular, etc.)."""
    ticket_type = models.ForeignKey(
        SaleableTicketType, on_delete=models.CASCADE, related_name='tiers'
    )
    name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    allotment = models.PositiveIntegerField()
    quantity_sold = models.PositiveIntegerField(default=0)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['order']
        unique_together = [['ticket_type', 'order']]
        indexes = [models.Index(fields=['ticket_type', 'order'])]

    def __str__(self):
        return f"{self.name} (${self.price})"

    def is_available(self):
        return self.quantity_sold < self.allotment

    def remaining_capacity(self):
        return max(0, self.allotment - self.quantity_sold)


class WaitlistEntry(BaseModel):
    """A single entry in the per-ticket-type waitlist queue."""
    ticket_type = models.ForeignKey(
        SaleableTicketType, on_delete=models.CASCADE, related_name='waitlist_entries'
    )
    email = models.EmailField(db_index=True)
    name = models.CharField(max_length=200, blank=True)
    position = models.PositiveIntegerField()
    notified_at = models.DateTimeField(null=True, blank=True)
    hold_expires_at = models.DateTimeField(null=True, blank=True)
    purchased_at = models.DateTimeField(null=True, blank=True)
    expired = models.BooleanField(default=False)
    hold_token = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)

    class Meta:
        ordering = ['position']
        indexes = [
            models.Index(fields=['ticket_type', 'position']),
            models.Index(fields=['ticket_type', 'email', 'expired', 'purchased_at']),
            models.Index(fields=['hold_token']),
        ]

    def __str__(self):
        return f"Waitlist #{self.position} - {self.email}"


class PromoCode(BaseModel):
    """Organizer-created discount codes scoped to a single event."""
    PERCENTAGE = 'percentage'
    FIXED = 'fixed'
    DISCOUNT_TYPE_CHOICES = [
        (PERCENTAGE, 'Percentage off'),
        (FIXED, 'Fixed amount off'),
    ]

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='promo_codes',
    )
    event = models.ForeignKey(
        'Event',
        on_delete=models.CASCADE,
        related_name='promo_codes',
    )
    code = models.CharField(max_length=50)  # stored uppercase
    discount_type = models.CharField(max_length=20, choices=DISCOUNT_TYPE_CHOICES)
    discount_value = models.DecimalField(max_digits=10, decimal_places=2)  # % or $ amount
    max_uses = models.PositiveIntegerField(null=True, blank=True)  # None = unlimited
    times_used = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [['event', 'code']]

    def __str__(self):
        return f"{self.code} ({self.get_discount_type_display()})"

    def is_valid(self):
        if not self.is_active:
            return False
        if self.expires_at and timezone.now() > self.expires_at:
            return False
        if self.max_uses is not None and self.times_used >= self.max_uses:
            return False
        return True

    def calculate_discount_cents(self, subtotal_cents):
        if self.discount_type == self.PERCENTAGE:
            return round(subtotal_cents * (self.discount_value / 100))
        else:  # FIXED
            return min(subtotal_cents, int(self.discount_value * 100))


def _generate_tracking_token():
    """Generate a unique 12-character alphanumeric token for a TrackingLink."""
    alphabet = string.ascii_letters + string.digits
    while True:
        token = ''.join(secrets.choice(alphabet) for _ in range(12))
        if not TrackingLink.objects.filter(token=token).exists():
            return token


class TrackingLink(BaseModel):
    """Named short link attached to a direct-ticketing event for click and purchase attribution."""
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='tracking_links',
    )
    event = models.ForeignKey(
        'Event',
        on_delete=models.CASCADE,
        related_name='tracking_links',
    )
    name = models.CharField(max_length=100)
    token = models.CharField(max_length=12, unique=True, db_index=True)
    click_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.token})"


class StripeCheckoutSession(BaseModel):
    """One row per Stripe Checkout Session - idempotency anchor for webhook processing."""

    class Status(models.TextChoices):
        PENDING   = 'pending',   'Pending'
        COMPLETED = 'completed', 'Completed'
        EXPIRED   = 'expired',   'Expired'
        CANCELED  = 'canceled',  'Canceled'
        REFUNDED  = 'refunded',  'Refunded'

    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        related_name='stripe_checkout_sessions',
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='stripe_checkout_sessions',
    )
    stripe_session_id = models.CharField(max_length=255, unique=True)
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True)
    buyer_email = models.EmailField()
    buyer_name = models.CharField(max_length=200, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    # Written at session creation; webhook reads this to avoid extra Stripe API call.
    # Schema: list of {saleable_ticket_type_id, name, price (str), quantity}
    line_items_snapshot = models.JSONField(default=list)
    amount_total_cents = models.PositiveIntegerField(default=0)
    platform_fee_cents = models.PositiveIntegerField(default=0)
    promo_code = models.ForeignKey(
        PromoCode,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='stripe_sessions',
    )
    discount_cents = models.PositiveIntegerField(default=0)
    ticket_order = models.OneToOneField(
        TicketOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stripe_checkout_session',
    )
    tracking_link = models.ForeignKey(
        TrackingLink,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='checkout_sessions',
    )
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    # Populated from the charge's balance_transaction.available_on at webhook time.
    # Null means the payment pre-dates this field — treat as already settled.
    available_on = models.DateTimeField(
        null=True, blank=True,
        help_text='When this payment settles into the Stripe platform balance (from balance_transaction.available_on).',
    )
    fb_browser_data = models.JSONField(
        default=dict, blank=True,
        help_text='Stores _fbp, _fbc, client IP, user agent for CAPI Purchase call on webhook.',
    )

    class Meta:
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['event', 'status']),
            models.Index(fields=['organization', 'status', 'available_on']),
        ]

    def __str__(self):
        return f"Stripe session {self.stripe_session_id} ({self.status})"


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


# ---------------------------------------------------------------------------
# External Survey (CSV upload from Typeform/similar)
# ---------------------------------------------------------------------------

class ExternalSurveyUpload(AuditBaseModel):
    """Tracks a CSV survey export uploaded by an organizer."""

    class Status(models.TextChoices):
        PROCESSING = 'processing', 'Processing'
        COMPLETED  = 'completed',  'Completed'
        FAILED     = 'failed',     'Failed'

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='external_survey_uploads',
    )
    filename = models.CharField(max_length=255)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    row_count = models.IntegerField(default=0)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PROCESSING, db_index=True,
    )
    error_log = models.TextField(blank=True, help_text='JSON list of per-row parse errors')

    class Meta:
        ordering = ['-uploaded_at']
        indexes = [
            models.Index(fields=['organization', '-uploaded_at']),
        ]

    def __str__(self):
        return f"{self.filename} ({self.organization})"


class ExternalSurveyResponse(BaseModel):
    """A single parsed row from an external survey CSV export."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='external_survey_responses', db_index=True,
    )
    upload = models.ForeignKey(
        ExternalSurveyUpload, on_delete=models.CASCADE, related_name='responses',
    )
    event = models.ForeignKey(
        Event, on_delete=models.SET_NULL, null=True, blank=True, related_name='external_survey_responses',
    )
    responded_at = models.DateTimeField(db_index=True)
    email = models.EmailField(blank=True, db_index=True)
    overall_rating = models.CharField(max_length=30, blank=True, db_index=True)
    nps_score = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    city = models.CharField(max_length=100, blank=True, db_index=True)
    enjoyed = models.JSONField(default=list)
    genres = models.JSONField(default=list)
    improvements = models.JSONField(default=list)
    crowd_vibe = models.CharField(max_length=80, blank=True)
    venue_feel = models.CharField(max_length=80, blank=True)
    pre_event_info = models.CharField(max_length=80, blank=True)
    found_out_how = models.CharField(max_length=200, blank=True)
    text_feedback = models.TextField(blank=True)
    raffle_email = models.EmailField(blank=True)

    class Meta:
        ordering = ['-responded_at']
        indexes = [
            models.Index(fields=['organization', '-responded_at']),
            models.Index(fields=['organization', 'city']),
            models.Index(fields=['organization', 'nps_score']),
            models.Index(fields=['upload', 'city']),
            models.Index(fields=['event', '-responded_at']),
        ]

    def __str__(self):
        return f"Survey response ({self.city or 'no city'}) at {self.responded_at:%Y-%m-%d}"


class Payout(BaseModel):
    """Platform-to-organizer Stripe Transfer record."""

    class Status(models.TextChoices):
        PENDING    = 'pending',    'Pending'
        IN_TRANSIT = 'in_transit', 'In Transit'
        COMPLETED  = 'completed',  'Completed'
        FAILED     = 'failed',     'Failed'

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='payouts', db_index=True,
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    stripe_transfer_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True,
        help_text='Stripe Transfer ID (tr_xxx) - set after successful Transfer call.',
    )
    stripe_payout_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True,
        help_text='Stripe Payout ID (po_xxx) - set via payout.created webhook on the connected account.',
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True,
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='initiated_payouts',
    )
    notes = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'status'],    name='tickets_payout_org_stat_idx'),
            models.Index(fields=['organization', '-created_at'], name='tickets_payout_org_date_idx'),
        ]

    def __str__(self):
        return f"Payout ${self.amount} \u2192 {self.organization.name} ({self.status})"


class OrganizerWaitlist(BaseModel):
    """Beta-gate waitlist for prospective organizers."""

    class Status(models.TextChoices):
        PENDING  = 'pending',  'Pending'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    name              = models.CharField(max_length=200)
    email             = models.EmailField(unique=True, db_index=True)
    organization_name = models.CharField(max_length=200)
    instagram_handle  = models.CharField(max_length=100, blank=True)
    status            = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        'auth.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='approved_waitlist_entries',
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Organizer Waitlist Entry'
        verbose_name_plural = 'Organizer Waitlist'

    def __str__(self):
        return f"{self.name} <{self.email}> ({self.status})"
