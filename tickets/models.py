import uuid
import secrets
import string
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.db.models import Sum, Max, F
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
    sms_marketing_enabled = models.BooleanField(
        default=False,
        help_text=(
            'Gates the native marketing SMS feature for this org. Off by default; '
            'enable per-org for pilot rollout. FeatureFlagSettings is a global '
            'singleton and cannot scope to individual orgs, so the SMS gate lives here.'
        ),
    )
    # Prepaid SMS credit wallet (cents). Topped up via Stripe Checkout, debited per
    # segment when a marketing-SMS campaign sends. See SMSCreditTransaction for the
    # audit ledger. Never mutate directly outside the wallet service (atomic F()).
    sms_credit_balance_cents = models.PositiveIntegerField(default=0)
    # Saved card on file for one-click wallet top-ups. This is a PLATFORM billing
    # Customer (the org pays Cue) — NOT the Connect account in stripe_account_id,
    # which is for paying organizers out. Created lazily on first "save card".
    stripe_customer_id = models.CharField(
        max_length=255, blank=True, null=True, db_index=True,
        help_text='Platform billing Stripe Customer (cus_xxx) for charging the org. '
                  'Distinct from stripe_account_id (Connect payouts).',
    )
    stripe_pm_id = models.CharField(
        max_length=255, blank=True, null=True,
        help_text='Saved Stripe PaymentMethod (pm_xxx) for one-click SMS top-ups.',
    )
    stripe_pm_brand = models.CharField(max_length=50, blank=True, help_text="Card brand, e.g. 'visa'.")
    stripe_pm_last4 = models.CharField(max_length=4, blank=True, help_text='Last 4 digits of the saved card.')
    stripe_pm_exp_month = models.PositiveSmallIntegerField(null=True, blank=True)
    stripe_pm_exp_year = models.PositiveSmallIntegerField(null=True, blank=True)
    stripe_account_id = models.CharField(
        max_length=255,
        blank=True,
        help_text='Stripe Connect Express account ID (acct_xxx).',
    )
    stripe_onboarding_complete = models.BooleanField(
        default=False,
        help_text='True after Stripe confirms details_submitted, charges_enabled, and payouts_enabled.',
    )
    stripe_terminal_location_id = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text=(
            "Stripe Terminal Location ID (tml_xxx) scoped to this merchant's "
            "Connect account. Created lazily on first in-person sale and cached "
            "forever — Locations don't expire and aren't per-sale."
        ),
    )
    meta_capi_access_token = models.CharField(
        max_length=255, blank=True, default='',
        help_text='Meta Conversions API access token for server-side event reporting.',
    )
    meta_ads_access_token = models.CharField(max_length=512, blank=True, default='')
    meta_ads_user_id = models.CharField(max_length=64, blank=True, default='')
    meta_ads_account_id = models.CharField(max_length=64, blank=True, default='')
    meta_ads_account_name = models.CharField(max_length=255, blank=True, default='')
    meta_ads_token_expires_at = models.DateTimeField(null=True, blank=True)
    mailchimp_access_token = models.CharField(max_length=512, blank=True, default='')
    mailchimp_dc = models.CharField(max_length=20, blank=True, default='')
    mailchimp_account_id = models.CharField(max_length=100, blank=True, default='')
    mailchimp_account_name = models.CharField(max_length=255, blank=True, default='')
    mailchimp_login_email = models.EmailField(blank=True, default='')
    mailchimp_campaign_title_hints = models.TextField(
        blank=True,
        default='',
        help_text=(
            "Optional guidance for the AI campaign matcher about your "
            "campaign naming conventions. Example: 'Campaigns prefixed "
            "lv- are for Las Vegas events. Format is lv-MMDDYYYY-email-NN "
            "where MMDDYYYY is the event date.'"
        ),
    )
    slicktext_api_key = models.CharField(max_length=255, blank=True, default='')
    slicktext_brand_id = models.CharField(max_length=100, blank=True, default='')
    slicktext_brand_name = models.CharField(max_length=255, blank=True, default='')
    slicktext_validated_at = models.DateTimeField(null=True, blank=True)
    typeform_access_token = models.CharField(max_length=255, blank=True, default='')
    typeform_account_email = models.EmailField(blank=True, default='')
    typeform_validated_at = models.DateTimeField(null=True, blank=True)
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


def _generate_client_id():
    return f"cue_client_{secrets.token_hex(16)}"


def _generate_auth_code():
    return secrets.token_urlsafe(32)


def _generate_access_token():
    return f"cue_at_{secrets.token_urlsafe(32)}"


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


class OAuthClient(BaseModel):
    """OAuth 2.0 public client registered by an MCP client (e.g. Claude Desktop)."""
    client_id = models.CharField(max_length=100, unique=True, default=_generate_client_id, db_index=True)
    client_name = models.CharField(max_length=200)
    redirect_uris = models.JSONField(default=list, help_text="Allowed redirect URIs — exact match enforced.")
    is_public = models.BooleanField(default=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'OAuth client'
        verbose_name_plural = 'OAuth clients'

    def __str__(self):
        return f"{self.client_name} ({self.client_id})"


class OAuthAuthorizationCode(BaseModel):
    """Short-lived (10-minute) authorization code issued after user consent. One-time use."""
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE, related_name='auth_codes')
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='oauth_auth_codes')
    code = models.CharField(max_length=100, unique=True, default=_generate_auth_code, db_index=True)
    code_challenge = models.CharField(max_length=200)
    redirect_uri = models.URLField(max_length=500)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['code', 'used'])]
        verbose_name = 'OAuth authorization code'
        verbose_name_plural = 'OAuth authorization codes'


class OAuthAccessToken(BaseModel):
    """Long-lived (30-day) bearer token issued after PKCE code exchange. Accepted at /mcp."""
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE, related_name='access_tokens')
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='oauth_access_tokens')
    token = models.CharField(max_length=200, unique=True, default=_generate_access_token, db_index=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['token', 'expires_at'])]
        verbose_name = 'OAuth access token'
        verbose_name_plural = 'OAuth access tokens'

    def __str__(self):
        return f"{self.organization.name} — {self.client.client_name}"


class FeatureFlagSettings(models.Model):
    """Singleton model for global feature flags managed from Django admin."""

    singleton_enforcer = models.BooleanField(default=True, unique=True, editable=False)
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
    """Invitation for a user to join an organization by email or phone."""
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACCEPTED = 'accepted', 'Accepted'
        EXPIRED = 'expired', 'Expired'
        REVOKED = 'revoked', 'Revoked'

    class InvitedVia(models.TextChoices):
        EMAIL = 'email', 'Email'
        PHONE = 'phone', 'Phone'

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='organization_invitations',
    )
    email = models.EmailField(db_index=True)
    phone_number = models.CharField(max_length=20, blank=True, default='', db_index=True)
    invited_via = models.CharField(
        max_length=10,
        choices=InvitedVia.choices,
        default=InvitedVia.EMAIL,
    )
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
        base = OrganizationInvitation.objects.filter(
            organization_id=self.organization_id,
            status=self.Status.PENDING,
            expires_at__gt=timezone.now(),
        )
        if self.pk:
            base = base.exclude(pk=self.pk)
        if base.filter(email__iexact=self.email).exists():
            raise ValidationError(
                f"An invitation for {self.email} is already pending for this organization."
            )
        if self.phone_number and base.filter(phone_number=self.phone_number).exists():
            raise ValidationError(
                f"An invitation for {self.phone_number} is already pending for this organization."
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
    user = models.ForeignKey(
        'auth.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='customer_profiles',
        help_text="Linked auth.User account, if the buyer has one. Null for CSV-imported customers without a backing account.",
    )
    email = models.EmailField(db_index=True)
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
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'email'],
                name='customer_org_email_unique',
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.email})"

    def clean_email(self):
        """Normalize email format (lowercase, trim)."""
        if self.email:
            return self.email.lower().strip()
        return self.email

    def calculate_lifetime_value(self):
        """Calculate LTV from all associated ticket orders (excludes refunded).

        For direct ticketing orders, subtracts platform fees so LTV reflects
        what the organizer actually receives, not the gross amount the customer paid.
        """
        orders = self.ticket_orders.filter(refunded_at__isnull=True)
        total = orders.aggregate(
            total=Sum(F('total_amount') - F('refunded_amount'))
        )['total'] or Decimal('0.00')
        fees_cents = (
            StripeCheckoutSession.objects.filter(ticket_order__in=orders)
            .aggregate(total=Sum('platform_fee_cents'))['total'] or 0
        )
        return total - Decimal(fees_cents) / 100

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
    max_tickets_per_customer = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Optional cumulative cap on how many tickets one customer can buy for this event.",
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
    show_social_proof = models.BooleanField(
        default=True,
        help_text="Display attendee avatars and count on the public buy page.",
    )
    show_attendee_count = models.BooleanField(
        default=True,
        help_text="Show the exact number of others (e.g. '+ 5 others'). When off, shows '+ others' without a count.",
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


class EventExpenseQuerySet(models.QuerySet):
    def visible(self):
        return self.filter(deleted_at__isnull=True).exclude(
            source='meta_ads', confirmed_at__isnull=True
        )


class EventExpense(AuditBaseModel):
    """Expense line item for an event."""
    SOURCE_CHOICES = [
        ('manual', 'Manual'),
        ('meta_ads', 'Meta Ads'),
    ]

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
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='manual', db_index=True)
    external_id = models.CharField(max_length=100, blank=True, default='')
    external_metadata = models.JSONField(default=dict, blank=True)
    manual_attributed_orders = models.PositiveIntegerField(null=True, blank=True)
    manual_attributed_revenue = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )
    api_data_changed_at = models.DateTimeField(null=True, blank=True)

    objects = EventExpenseQuerySet.as_manager()

    class Meta:
        ordering = ['-expense_date', '-created_at']
        indexes = [
            models.Index(fields=['event', 'category']),
            models.Index(fields=['event', '-expense_date']),
            models.Index(fields=['event', 'deleted_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['event', 'source', 'external_id'],
                condition=models.Q(source='meta_ads', deleted_at__isnull=True),
                name='uniq_active_meta_expense_per_campaign',
            ),
        ]

    def __str__(self):
        return f"{self.get_category_display()} - {self.description} (${self.amount})"

    @property
    def effective_attributed_orders(self):
        return self.manual_attributed_orders if self.manual_attributed_orders is not None else 0

    @property
    def effective_attributed_revenue(self):
        return self.manual_attributed_revenue if self.manual_attributed_revenue is not None else Decimal('0.00')

    @property
    def is_confirmed(self):
        return self.confirmed_at is not None

    @property
    def needs_review(self):
        return bool(
            self.api_data_changed_at
            and (not self.confirmed_at or self.api_data_changed_at > self.confirmed_at)
        )


class EventEmailCampaign(AuditBaseModel):
    """Email marketing campaign results linked to an event."""
    SOURCE_CHOICES = [
        ('mailchimp', 'Mailchimp'),
    ]

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='email_campaigns')
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='mailchimp', db_index=True)
    external_id = models.CharField(max_length=100)
    campaign_title = models.CharField(max_length=300)
    subject_line = models.CharField(max_length=500, blank=True, default='')
    send_time = models.DateTimeField(null=True, blank=True, db_index=True)
    archive_url = models.URLField(max_length=500, blank=True, default='')
    emails_sent = models.PositiveIntegerField(default=0)
    opens = models.PositiveIntegerField(default=0)
    unique_opens = models.PositiveIntegerField(default=0)
    open_rate = models.DecimalField(max_digits=7, decimal_places=4, default=Decimal('0.0000'))
    clicks = models.PositiveIntegerField(default=0)
    unique_clicks = models.PositiveIntegerField(default=0)
    click_rate = models.DecimalField(max_digits=7, decimal_places=4, default=Decimal('0.0000'))
    bounces = models.PositiveIntegerField(default=0)
    unsubscribes = models.PositiveIntegerField(default=0)
    abuse_reports = models.PositiveIntegerField(default=0)
    ecommerce_orders = models.PositiveIntegerField(default=0)
    ecommerce_revenue = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    match_confidence = models.DecimalField(max_digits=4, decimal_places=3, default=Decimal('0.000'))
    match_reasoning = models.TextField(blank=True, default='')
    last_synced_at = models.DateTimeField(null=True, blank=True)
    external_metadata = models.JSONField(default=dict, blank=True)
    manual_emails_sent = models.PositiveIntegerField(null=True, blank=True)
    manual_unique_opens = models.PositiveIntegerField(null=True, blank=True)
    manual_clicks = models.PositiveIntegerField(null=True, blank=True)
    manual_unsubscribes = models.PositiveIntegerField(null=True, blank=True)
    manual_orders = models.PositiveIntegerField(null=True, blank=True)
    manual_revenue = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )
    api_data_changed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-send_time', '-created_at']
        indexes = [
            models.Index(fields=['event', 'source']),
            models.Index(fields=['source', 'external_id']),
            models.Index(fields=['event', 'deleted_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['event', 'source', 'external_id'],
                condition=models.Q(source='mailchimp', deleted_at__isnull=True),
                name='uniq_active_email_campaign',
            ),
        ]

    def __str__(self):
        return f"{self.get_source_display()} - {self.campaign_title}"

    @property
    def effective_emails_sent(self):
        return self.manual_emails_sent if self.manual_emails_sent is not None else self.emails_sent

    @property
    def effective_unique_opens(self):
        return self.manual_unique_opens if self.manual_unique_opens is not None else self.unique_opens

    @property
    def effective_clicks(self):
        return self.manual_clicks if self.manual_clicks is not None else self.unique_clicks

    @property
    def effective_unsubscribes(self):
        return self.manual_unsubscribes if self.manual_unsubscribes is not None else self.unsubscribes

    @property
    def effective_orders(self):
        return self.manual_orders if self.manual_orders is not None else self.ecommerce_orders

    @property
    def effective_revenue(self):
        return self.manual_revenue if self.manual_revenue is not None else self.ecommerce_revenue

    @property
    def is_confirmed(self):
        return self.confirmed_at is not None

    @property
    def needs_review(self):
        return bool(
            self.api_data_changed_at
            and (not self.confirmed_at or self.api_data_changed_at > self.confirmed_at)
        )


class EventSMSCampaign(AuditBaseModel):
    """SMS marketing broadcast results linked to an event."""
    SOURCE_CHOICES = [
        ('slicktext', 'SlickText'),
    ]

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='sms_campaigns')
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='slicktext', db_index=True)
    external_id = models.CharField(max_length=100)
    name = models.CharField(max_length=300)
    message = models.CharField(max_length=1600, blank=True, default='')
    media_url = models.URLField(max_length=500, blank=True, default='')
    send_time = models.DateTimeField(null=True, blank=True, db_index=True)
    audience_size = models.PositiveIntegerField(default=0)
    clicks = models.PositiveIntegerField(default=0)
    unique_clicks = models.PositiveIntegerField(default=0)
    click_rate = models.DecimalField(max_digits=7, decimal_places=4, default=Decimal('0.0000'))
    unsubscribes = models.PositiveIntegerField(default=0)
    unsubscribe_rate = models.DecimalField(max_digits=7, decimal_places=4, default=Decimal('0.0000'))
    orders = models.PositiveIntegerField(default=0)
    revenue = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    match_confidence = models.DecimalField(max_digits=4, decimal_places=3, default=Decimal('0.000'))
    match_reasoning = models.TextField(blank=True, default='')
    last_synced_at = models.DateTimeField(null=True, blank=True)
    external_metadata = models.JSONField(default=dict, blank=True)
    manual_audience = models.PositiveIntegerField(null=True, blank=True)
    manual_clicks = models.PositiveIntegerField(null=True, blank=True)
    manual_unsubscribes = models.PositiveIntegerField(null=True, blank=True)
    manual_orders = models.PositiveIntegerField(null=True, blank=True)
    manual_revenue = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )
    api_data_changed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-send_time', '-created_at']
        indexes = [
            models.Index(fields=['event', 'source']),
            models.Index(fields=['source', 'external_id']),
            models.Index(fields=['event', 'deleted_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['event', 'source', 'external_id'],
                condition=models.Q(source='slicktext', deleted_at__isnull=True),
                name='uniq_active_sms_campaign',
            ),
        ]

    def __str__(self):
        return f"{self.get_source_display()} - {self.name}"

    @property
    def effective_audience(self):
        return self.manual_audience if self.manual_audience is not None else self.audience_size

    @property
    def effective_clicks(self):
        return self.manual_clicks if self.manual_clicks is not None else self.unique_clicks

    @property
    def effective_unsubscribes(self):
        return self.manual_unsubscribes if self.manual_unsubscribes is not None else self.unsubscribes

    @property
    def effective_orders(self):
        return self.manual_orders if self.manual_orders is not None else self.orders

    @property
    def effective_revenue(self):
        return self.manual_revenue if self.manual_revenue is not None else self.revenue

    @property
    def is_confirmed(self):
        return self.confirmed_at is not None

    @property
    def needs_review(self):
        return bool(
            self.api_data_changed_at
            and (not self.confirmed_at or self.api_data_changed_at > self.confirmed_at)
        )


# ---------------------------------------------------------------------------
# Native marketing SMS (send texts via Twilio)
#
# Distinct from EventSMSCampaign above, which only *tracks* external SlickText
# metrics post-hoc. These models *send* texts natively.
#
#   SMSCampaign.materialize() ──► candidate Customers (inline filter_criteria
#         │                        + manual include/exclude) ∩ {phone set,
#         │                        sms_opt_in} ─ dedupe(phone) ─ suppressed
#         ▼
#   SMSCampaign  draft ─► scheduled ─► sending ─► sent
#         │                  └─► canceled        └─► failed
#         ▼
#   SMSMessageRecipient (per-recipient delivery; SOURCE OF TRUTH for counts)
#
# Opt-out is keyed by phone number (PhoneSuppression), NOT by Customer row,
# because one phone can map to many Customer rows across orgs and Twilio
# enforces opt-out per number.
# ---------------------------------------------------------------------------


class PhoneSuppression(BaseModel):
    """A phone number that must not receive marketing SMS (opt-out / suppression).

    Keyed by E.164 phone, not Customer, because the same number can appear on
    many Customer rows. `organization=None` means a GLOBAL suppression (mirrored
    from Twilio's STOP/OptOutType — the shared sender enforces it for everyone);
    a set `organization` means a per-org unsubscribe (forward-compatible with
    per-org sender numbers).
    """
    class Reason(models.TextChoices):
        TWILIO_STOP = 'twilio_stop', 'Twilio STOP'
        MANUAL = 'manual', 'Manual'
        BOUNCE = 'bounce', 'Bounce'

    phone = models.CharField(max_length=20, db_index=True)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='phone_suppressions',
        help_text='Null = global suppression (all orgs). Set = per-org unsubscribe.',
    )
    reason = models.CharField(max_length=20, choices=Reason.choices, default=Reason.TWILIO_STOP)

    class Meta:
        indexes = [
            models.Index(fields=['phone', 'organization']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['phone', 'organization'],
                name='uniq_phone_suppression_phone_org',
            ),
        ]

    def __str__(self):
        scope = 'global' if self.organization_id is None else f'org={self.organization_id}'
        return f"{self.phone} suppressed ({scope})"

    @classmethod
    def suppressed_phones(cls, organization):
        """Return the set of phones suppressed for this org (org-specific OR global)."""
        from django.db.models import Q
        return set(
            cls.objects.filter(Q(organization=organization) | Q(organization__isnull=True))
            .values_list('phone', flat=True)
        )

    @classmethod
    def is_suppressed(cls, phone, organization):
        from django.db.models import Q
        return cls.objects.filter(
            Q(organization=organization) | Q(organization__isnull=True),
            phone=phone,
        ).exists()


class SMSCampaign(AuditBaseModel):
    """A native marketing-SMS broadcast.

    State machine (see status field):
        draft ──► scheduled ──► sending ──► sent
                      │            └──────► failed
                      └──► canceled
    Per-recipient delivery lives in SMSMessageRecipient, which is the source of
    truth for sent/delivered/failed counts (derived, never incremented here, so
    retried Twilio callbacks can't cause drift).
    """
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        SCHEDULED = 'scheduled', 'Scheduled'
        SENDING = 'sending', 'Sending'
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Failed'
        CANCELED = 'canceled', 'Canceled'

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='sms_campaigns',
    )
    event = models.ForeignKey(
        Event,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='native_sms_campaigns',
    )
    # Audience lives inline on the campaign (no separate recipient-list model).
    # filter_criteria is a JSON spec consumed by filter_customers: tag_ids /
    # rfm_segment / event_id / min_ltv / last_order_after. manual include/exclude
    # are customer UUID lists. materialize() resolves them to opted-in,
    # contactable, non-suppressed, deduped recipients at send time.
    filter_criteria = models.JSONField(
        default=dict,
        blank=True,
        help_text="e.g. {'rfm_segment': ['VIP'], 'tag_ids': [...], 'event_id': '...'}",
    )
    manual_include_ids = models.JSONField(default=list, blank=True)
    manual_exclude_ids = models.JSONField(default=list, blank=True)
    name = models.CharField(max_length=200)
    body = models.CharField(max_length=1600)
    link_url = models.URLField(max_length=500, blank=True, default='')
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    scheduled_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    audience_size = models.PositiveIntegerField(default=0)
    # Per-submit token from the confirm panel. A duplicate confirm (double-click /
    # browser retry) reuses the same key, so the unique constraint stops a second
    # campaign from being created, charged, and sent.
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', '-created_at']),
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['status', 'scheduled_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'idempotency_key'],
                condition=models.Q(idempotency_key__isnull=False),
                name='uniq_sms_campaign_idempotency_key',
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.status})"

    def candidate_customers(self, organization=None):
        """Org-scoped candidate Customer queryset: criteria ∪ manual includes − excludes,
        restricted to opted-in customers with a phone. Dedupe + suppression happen in
        materialize(). Fail-safe: empty criteria AND no manual includes → none."""
        from tickets.services.customer_filters import filter_customers
        org = organization or self.organization
        criteria = self.filter_criteria or {}
        include_ids = self.manual_include_ids or []
        exclude_ids = self.manual_exclude_ids or []

        if not criteria and not include_ids:
            return Customer.objects.none()

        if criteria:
            qs = filter_customers(org, criteria)
        else:
            qs = Customer.objects.none()

        if include_ids:
            qs = qs | Customer.objects.filter(organization=org, id__in=include_ids)

        qs = qs.filter(organization=org, sms_opt_in=True).exclude(phone='')
        if exclude_ids:
            qs = qs.exclude(id__in=exclude_ids)
        return qs.distinct()

    def materialize(self, organization=None, cap=None):
        """Return deduped, non-suppressed recipients as a list of
        {'customer_id', 'phone'} dicts (E.164). Dedupe + suppression are done in
        Python so they work identically on SQLite (dev) and Postgres (prod)."""
        from tickets.sms import normalize_phone
        org = organization or self.organization
        suppressed = PhoneSuppression.suppressed_phones(org)
        seen = set()
        out = []
        for customer in self.candidate_customers(org).only('id', 'phone'):
            phone = normalize_phone(customer.phone)
            if not phone or phone in seen or phone in suppressed:
                continue
            seen.add(phone)
            out.append({'customer_id': str(customer.id), 'phone': phone})
            if cap and len(out) >= cap:
                break
        return out

    def audience_summary(self, organization=None):
        """Human label for the campaign's audience. Resolves tag UUIDs with a single
        query. Call on the detail page only — NOT per-row in a list (N+1)."""
        org = organization or self.organization
        criteria = self.filter_criteria or {}
        parts = []
        event_id = criteria.get('event_id')
        if event_id:
            ev = Event.objects.filter(organization=org, id=event_id).first()
            parts.append(f"Attendees of {ev.name}" if ev else "Event attendees")
        segments = criteria.get('rfm_segment') or []
        if isinstance(segments, str):
            segments = [segments]
        if segments:
            parts.append("Segments: " + ", ".join(segments))
        tag_ids = criteria.get('tag_ids') or []
        if isinstance(tag_ids, str):
            tag_ids = [tag_ids]
        if tag_ids:
            names = list(
                CustomerTag.objects.filter(organization=org, id__in=tag_ids)
                .values_list('name', flat=True)
            )
            if names:
                parts.append("Tags: " + ", ".join(names))
        if self.manual_include_ids:
            parts.append("Custom selection")
        return " · ".join(parts) if parts else "No audience"


class SMSMessageRecipient(BaseModel):
    """One outbound marketing text. Source of truth for delivery state.

    Twilio posts multiple/retried status callbacks per message, so transitions
    here must be idempotent and campaign counts are derived from these rows
    (never incremented), preventing double-counting.
    """
    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        SENT = 'sent', 'Sent'
        DELIVERED = 'delivered', 'Delivered'
        UNDELIVERED = 'undelivered', 'Undelivered'
        FAILED = 'failed', 'Failed'

    campaign = models.ForeignKey(
        SMSCampaign,
        on_delete=models.CASCADE,
        related_name='recipients',
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sms_message_recipients',
    )
    phone = models.CharField(max_length=20)
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    twilio_sid = models.CharField(max_length=64, blank=True, default='', db_index=True)
    error_code = models.CharField(max_length=20, blank=True, default='')
    error_message = models.CharField(max_length=255, blank=True, default='')
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    # Link-click tracking. click_token is set per recipient at send time only when
    # the campaign has a tracked link (NULL otherwise). Counts are derived at the
    # campaign level from these rows: total = Sum(click_count), unique = rows with
    # first_clicked_at set.
    click_token = models.CharField(
        max_length=24, null=True, blank=True, unique=True, db_index=True,
    )
    click_count = models.PositiveIntegerField(default=0)
    first_clicked_at = models.DateTimeField(null=True, blank=True)
    # Set when an inbound STOP is attributed to this recipient's campaign.
    opted_out_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['campaign', 'status']),
            models.Index(fields=['twilio_sid']),
        ]

    def __str__(self):
        return f"{self.phone} [{self.status}]"


class SMSCreditTransaction(BaseModel):
    """Immutable ledger for the prepaid SMS credit wallet.

    Every change to Organization.sms_credit_balance_cents writes one row here, so
    the balance is always reconstructable and Stripe top-ups are idempotent
    (stripe_checkout_session_id is unique). amount_cents is signed: positive for
    top-ups/refunds (credits), negative for campaign charges (debits).
    """
    class Kind(models.TextChoices):
        TOPUP = 'topup', 'Top-up'
        CHARGE = 'charge', 'Campaign charge'
        REFUND = 'refund', 'Refund'
        ADJUSTMENT = 'adjustment', 'Manual adjustment'

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='sms_credit_transactions',
    )
    kind = models.CharField(max_length=12, choices=Kind.choices, db_index=True)
    amount_cents = models.IntegerField(help_text='Signed: + credit, - debit.')
    balance_after_cents = models.PositiveIntegerField()
    campaign = models.ForeignKey(
        SMSCampaign, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='credit_transactions',
    )
    stripe_checkout_session_id = models.CharField(
        max_length=255, null=True, blank=True, unique=True,
        help_text='Set on TOPUP rows; unique so webhook retries cannot double-credit.',
    )
    description = models.CharField(max_length=255, blank=True, default='')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', '-created_at']),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.amount_cents}¢ (org={self.organization_id})"


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
    refunded_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Cumulative dollars refunded (partial + full). Equals total_amount when fully refunded.",
    )
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


class AIRecommendation(BaseModel):
    """Reviewed AI-generated opportunity surfaced in the organizer Action Center."""

    class Kind(models.TextChoices):
        SALES_PACING = 'sales_pacing', 'Sales pacing'
        POST_EVENT_WRAPUP = 'post_event_wrapup', 'Post-event wrap-up'
        WINBACK_AUDIENCE = 'winback_audience', 'Win-back audience'
        MARKETING_ATTRIBUTION = 'marketing_attribution', 'Marketing attribution'
        MARKETING_UNCONFIRMED = 'marketing_unconfirmed', 'Marketing match unconfirmed'

    class Status(models.TextChoices):
        NEW = 'new', 'New'
        REVIEWED = 'reviewed', 'Reviewed'
        DISMISSED = 'dismissed', 'Dismissed'
        RESOLVED = 'resolved', 'Resolved'

    class Priority(models.TextChoices):
        HIGH = 'high', 'High'
        MEDIUM = 'medium', 'Medium'
        LOW = 'low', 'Low'

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='ai_recommendations',
    )
    event = models.ForeignKey(
        Event,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='ai_recommendations',
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='ai_recommendations',
    )
    kind = models.CharField(max_length=40, choices=Kind.choices, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.NEW,
        db_index=True,
    )
    priority = models.CharField(max_length=20, choices=Priority.choices, db_index=True)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, default=Decimal('0.000'))
    title = models.CharField(max_length=200)
    summary = models.TextField()
    evidence_json = models.JSONField(default=dict, blank=True)
    recommended_action_json = models.JSONField(default=dict, blank=True)
    dedupe_key = models.CharField(max_length=160)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['status', 'priority', '-confidence', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'dedupe_key'],
                name='ai_recommendation_org_dedupe_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['organization', 'status', 'priority']),
            models.Index(fields=['organization', 'kind', 'status']),
            models.Index(fields=['event', 'status']),
            models.Index(fields=['customer', 'status']),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} - {self.title}"

    @property
    def is_unresolved(self):
        return self.status in {self.Status.NEW, self.Status.REVIEWED}

    def mark_reviewed(self):
        self.status = self.Status.REVIEWED
        self.reviewed_at = timezone.now()
        self.save(update_fields=['status', 'reviewed_at', 'updated_at'])

    def dismiss(self):
        self.status = self.Status.DISMISSED
        self.dismissed_at = timezone.now()
        self.save(update_fields=['status', 'dismissed_at', 'updated_at'])

    def resolve(self):
        self.status = self.Status.RESOLVED
        self.resolved_at = timezone.now()
        self.save(update_fields=['status', 'resolved_at', 'updated_at'])


class AITokenUsage(BaseModel):
    """Immutable token usage record for billable AI features."""

    FEATURE_CHAT_AGENT = 'chat_agent'
    FEATURE_META_CAMPAIGN_MATCH = 'meta_campaign_match'
    FEATURE_MAILCHIMP_CAMPAIGN_MATCH = 'mailchimp_campaign_match'
    FEATURE_SLICKTEXT_CAMPAIGN_MATCH = 'slicktext_campaign_match'
    FEATURE_MARKETING_NARRATIVE = 'marketing_narrative'
    FEATURE_TYPEFORM_EVENT_MATCH = 'typeform_event_match'

    FEATURE_CHOICES = [
        (FEATURE_CHAT_AGENT, 'Chat agent'),
        (FEATURE_META_CAMPAIGN_MATCH, 'Meta campaign match'),
        (FEATURE_MAILCHIMP_CAMPAIGN_MATCH, 'Mailchimp campaign match'),
        (FEATURE_SLICKTEXT_CAMPAIGN_MATCH, 'SlickText campaign match'),
        (FEATURE_MARKETING_NARRATIVE, 'Marketing narrative'),
        (FEATURE_TYPEFORM_EVENT_MATCH, 'Typeform event match'),
    ]

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='ai_token_usage',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ai_token_usage',
    )
    feature = models.CharField(max_length=50, choices=FEATURE_CHOICES, db_index=True)
    model_name = models.CharField(max_length=100, blank=True, db_index=True)
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)
    request_id = models.CharField(max_length=100, blank=True, db_index=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-occurred_at', '-created_at']
        indexes = [
            models.Index(fields=['organization', 'occurred_at'], name='aiusage_org_time_idx'),
            models.Index(fields=['organization', 'feature', 'occurred_at'], name='aiusage_org_feature_idx'),
            models.Index(fields=['organization', 'model_name', 'occurred_at'], name='aiusage_org_model_idx'),
        ]
        verbose_name = 'AI token usage'
        verbose_name_plural = 'AI token usage'

    def __str__(self):
        return f"{self.organization} {self.feature}: {self.total_tokens} tokens"


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
    max_per_customer = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Max tickets a single customer can purchase for this ticket type; null = unlimited",
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
    low_stock_threshold = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Show an 'Only X left' warning once remaining tickets drop to this "
                  "number or fewer. Leave blank to disable the warning.",
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

    def low_stock_remaining(self):
        """Remaining count to show in a low-stock warning, or None when the warning
        should not appear (no threshold, unlimited, sold out, or above threshold)."""
        if self.low_stock_threshold is None or self.is_sold_out():
            return None
        active = self.get_active_tier()
        if active is not None:
            remaining = active.remaining_capacity()
        elif self.quantity_limit is not None:
            remaining = self.remaining_quantity()
        else:
            return None  # unlimited, no tier -> nothing to warn about
        if remaining is not None and remaining <= self.low_stock_threshold:
            return remaining
        return None


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


class EventDailyPageView(BaseModel):
    """Daily public buy-page view counts for an event."""
    event = models.ForeignKey(
        'Event',
        on_delete=models.CASCADE,
        related_name='daily_page_views',
    )
    date = models.DateField(db_index=True)
    view_count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [('event', 'date')]
        ordering = ['date']
        indexes = [
            models.Index(fields=['event', 'date']),
        ]

    def __str__(self):
        return f"{self.event.name} - {self.date}: {self.view_count}"


class StripeCheckoutSession(BaseModel):
    """One row per Stripe Checkout Session - idempotency anchor for webhook processing."""

    class Status(models.TextChoices):
        PENDING            = 'pending',            'Pending'
        COMPLETED          = 'completed',          'Completed'
        EXPIRED            = 'expired',            'Expired'
        CANCELED           = 'canceled',           'Canceled'
        PARTIALLY_REFUNDED = 'partially_refunded', 'Partially refunded'
        REFUNDED           = 'refunded',           'Refunded'

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
    sms_opt_in = models.BooleanField(default=False)
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
    typeform_response_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    raw_answers = models.JSONField(
        default=list, blank=True,
        help_text='Full list of {id, ref, type, title, value} dicts from Typeform.',
    )
    suggested_event = models.ForeignKey(
        'Event', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='suggested_survey_responses',
    )
    match_confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    match_reasoning = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-responded_at']
        indexes = [
            models.Index(fields=['organization', '-responded_at']),
            models.Index(fields=['organization', 'city']),
            models.Index(fields=['organization', 'nps_score']),
            models.Index(fields=['upload', 'city']),
            models.Index(fields=['event', '-responded_at']),
            models.Index(fields=['organization', 'typeform_response_id']),
        ]

    def __str__(self):
        return f"Survey response ({self.city or 'no city'}) at {self.responded_at:%Y-%m-%d}"


def _generate_typeform_webhook_secret():
    return secrets.token_urlsafe(32)


class TypeformFormSubscription(AuditBaseModel):
    """A Typeform form an organization has wired up for response sync."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='typeform_subscriptions',
    )
    form_id = models.CharField(max_length=64)
    form_title = models.CharField(max_length=255)
    webhook_id = models.CharField(max_length=64, blank=True, default='')
    webhook_secret = models.CharField(max_length=64, default=_generate_typeform_webhook_secret)
    field_map = models.JSONField(
        null=True, blank=True, default=None,
        help_text=(
            'Map of Typeform field ref/id → ExternalSurveyResponse column name '
            '(e.g. {"q1_ref": "overall_rating", "email_id": "email"}). '
            'null = never saved through the editor (auto-suggest on first visit); '
            '{} = saved with everything explicitly Ignored; '
            '{...} = saved with at least one mapped field.'
        ),
    )
    questions = models.JSONField(
        default=list, blank=True,
        help_text=(
            "Cached snapshot of the form's leaf questions: "
            "[{id, ref, type, title, group_title}, ...]. Refreshed whenever we "
            "call TypeformClient.get_form() — used as the source of truth for "
            "question titles when rendering responses (raw_answers may have empty "
            "titles for older rows ingested before group-walking was fixed)."
        ),
    )
    upload = models.ForeignKey(
        ExternalSurveyUpload, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='typeform_subscriptions',
        help_text='Synthetic upload row so the existing surveys UI groups responses by form.',
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_sync_error = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = [('organization', 'form_id')]
        indexes = [
            models.Index(fields=['organization', 'is_active']),
        ]
        verbose_name = 'Typeform form subscription'
        verbose_name_plural = 'Typeform form subscriptions'

    def __str__(self):
        return f"{self.form_title} ({self.organization.name})"

    @property
    def field_map_is_unconfigured(self) -> bool:
        """True only when ``field_map`` was never saved (``None``). An explicit
        empty dict means "ignore everything" and must NOT trigger a warning.
        """
        return self.field_map is None


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


class TapToPayTermsAcceptance(BaseModel):
    """Audit log of merchant acceptances of Apple's Tap to Pay on iPhone T&Cs.

    Append-only — one row per acceptance event, never deduped. Apple may ask
    for the audit trail showing the merchant re-accepted on each version bump.
    """
    scanner_session = models.ForeignKey(
        'ScannerSession',
        on_delete=models.CASCADE,
        related_name='tap_to_pay_acceptances',
    )
    organization = models.ForeignKey(
        'Organization',
        on_delete=models.CASCADE,
        related_name='tap_to_pay_acceptances',
    )
    version = models.CharField(max_length=64)
    accepted_at = models.DateTimeField(auto_now_add=True, db_index=True)
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True, default='')

    class Meta:
        indexes = [models.Index(fields=['organization', '-accepted_at'])]
        verbose_name = 'Tap to Pay Terms Acceptance'
        verbose_name_plural = 'Tap to Pay Terms Acceptances'

    def __str__(self):
        return f"{self.organization.name} accepted {self.version} at {self.accepted_at:%Y-%m-%d %H:%M}"


class ReceiptSend(BaseModel):
    """Log of receipt sends initiated by the scanner app (successful sales and declined attempts)."""
    organization = models.ForeignKey(
        'Organization',
        on_delete=models.CASCADE,
        related_name='receipt_sends',
    )
    ticket_order = models.ForeignKey(
        'TicketOrder',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='receipt_sends',
    )
    payment_intent_id = models.CharField(max_length=255, blank=True, default='', db_index=True)
    channel = models.CharField(max_length=10)
    contact = models.CharField(max_length=255)
    status = models.CharField(max_length=20)
    error_message = models.TextField(blank=True, default='')

    class Meta:
        indexes = [models.Index(fields=['organization', '-created_at'])]

    def __str__(self):
        return f"{self.channel} to {self.contact} ({self.status})"
