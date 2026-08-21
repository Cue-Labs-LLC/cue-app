import uuid
import secrets
import string
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.db.models import Sum, Max, F
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator


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


def _event_image_upload_to(instance, filename):
    return f"orgs/{instance.event.organization.slug}/event_images/{filename}"


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


# Offset types for the default post-event survey send schedule. An empty
# offset_type means "send immediately" (no scheduled offset configured).
SURVEY_SEND_OFFSET_CHOICES = [('hours', 'hours'), ('days', 'days')]

# Whether the survey send offset is measured from the event start or end.
SURVEY_SEND_ANCHOR_CHOICES = [('end', 'end'), ('start', 'start')]


class Organization(BaseModel):
    """Organization that owns venues, events, uploads, customers, and custom fields."""
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=100, unique=True)
    rfm_recalc_in_progress = models.BooleanField(default=False)
    # How rfm_segment is assigned. "percentile" = population-relative quintiles
    # (default, legacy). "absolute" = behavior-anchored fixed cut-offs from
    # segment_bands (see tickets/services/segmentation/segment_definitions.py).
    SEGMENT_MODE_CHOICES = [
        ('percentile', 'Percentile (relative)'),
        ('absolute', 'Absolute (fixed cut-offs)'),
    ]
    segment_mode = models.CharField(
        max_length=12, choices=SEGMENT_MODE_CHOICES, default='percentile', db_index=True,
    )
    # Absolute-mode cut-offs. Empty {} means "use module defaults + auto-seeded
    # monetary". Keys: recency_active_days, recency_cooling_days, freq_few,
    # freq_many, monetary_mid, monetary_high.
    segment_bands = models.JSONField(default=dict, blank=True)
    # Which optional columns appear on the Customers list (list of keys, see
    # CUSTOMER_LIST_COLUMNS in views). Null = no preference saved, show all defaults.
    customer_list_columns = models.JSONField(
        null=True, blank=True, default=None,
        help_text="Visible optional columns on the Customers table (list of keys). "
                  "Null = show all default columns.",
    )
    survey_email_subject = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Org-wide default subject for survey invitation emails. "
                  "Use {event} for the event name. Blank = built-in default.",
    )
    survey_reply_to_email = models.EmailField(
        blank=True, default='',
        help_text="Replies to survey invitations go here, and the org name appears "
                  "on the From line. Blank = send as Cue with no reply-to.",
    )
    # Org-wide default schedule for sending the post-event survey, expressed as
    # an offset from the event start or end. Blank offset_type = send immediately.
    survey_send_offset_type = models.CharField(
        max_length=10, blank=True, default='', choices=SURVEY_SEND_OFFSET_CHOICES,
        help_text="Default survey send timing: 'hours' or 'days' relative to the "
                  "event. Blank = send immediately.",
    )
    survey_send_offset_value = models.PositiveSmallIntegerField(null=True, blank=True)
    survey_send_time_of_day = models.TimeField(
        null=True, blank=True,
        help_text="Time of day to send when offset_type is 'days' (event timezone).",
    )
    survey_send_anchor = models.CharField(
        max_length=10, blank=True, default='', choices=SURVEY_SEND_ANCHOR_CHOICES,
        help_text="Whether the offset is measured from the event 'start' or 'end'. "
                  "Blank = end.",
    )
    sms_marketing_enabled = models.BooleanField(
        default=True,
        help_text=(
            'Gates the native marketing SMS feature for this org. On by default for '
            'new orgs (existing orgs unchanged by the default flip). Actual sending is '
            'still gated by per-customer consent and platform A2P readiness. '
            'FeatureFlagSettings is a global singleton and cannot scope to individual '
            'orgs, so the SMS gate lives here.'
        ),
    )
    sms_subscribe_title = models.CharField(
        max_length=80,
        blank=True,
        default='',
        help_text=(
            "Optional header shown above the mobile number field on the public "
            "subscribe page (e.g. 'Join the Tempo Global text list'). Blank shows "
            "no header (the org name still appears as the page heading)."
        ),
    )
    sms_subscribe_segment_by_market = models.BooleanField(
        default=False,
        help_text=(
            "When on (and the org has >1 market), the public subscribe page asks each "
            "new subscriber which market they're in and stores it on Customer.home_market, "
            "so market-scoped SMS campaigns can reach them before any purchase."
        ),
    )
    sms_subscribe_market_label = models.CharField(
        max_length=60,
        blank=True,
        default='',
        help_text=(
            "Custom label for the market picker on the public subscribe page "
            "(e.g. 'Which city?'). Blank falls back to the default 'Your area'."
        ),
    )
    loyalty_feature_enabled = models.BooleanField(
        default=False,
        help_text=(
            'Gates the loyalty program feature (tier builder + points engine) for '
            'this org. Off by default; enable per-org for pilot rollout. When off: '
            'loyalty pages 404, the sidebar link and customer-detail tier badge are '
            'hidden, orders earn no points, and recalc/backfill tasks no-op. '
            'Existing data is preserved; revokes of past earns still apply.'
        ),
    )
    # Prepaid SMS credit wallet (cents). Topped up via Stripe Checkout, debited per
    # segment when a marketing-SMS campaign sends. See SMSCreditTransaction for the
    # audit ledger. Never mutate directly outside the wallet service (atomic F()).
    sms_credit_balance_cents = models.PositiveIntegerField(default=0)
    # Loyalty points: when the org-wide historical backfill last completed.
    # Lives on the org (not LoyaltyProgram) because points balances are
    # (customer, organization) state that survives program replacement.
    loyalty_points_backfilled_at = models.DateTimeField(null=True, blank=True)
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
    tap_to_pay_enabled_push_sent = models.BooleanField(
        default=False,
        help_text=(
            "True once the one-time 'Tap to Pay is ready' push has been sent to "
            "this org's devices. Guards against duplicate sends across the many "
            "account.updated webhooks Stripe fires."
        ),
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
    ai_event_summary_enabled = models.BooleanField(
        default=True,
        help_text='Show the AI Event Summary card on event detail pages.',
    )
    ai_event_summary_auto_regenerate = models.BooleanField(
        default=True,
        help_text=(
            'Automatically regenerate an event\'s AI summary once a day when its '
            'underlying data changes after the event ends. When off, summaries are '
            'only (re)generated when someone clicks Generate.'
        ),
    )
    ai_sms_strategist_enabled = models.BooleanField(
        default=True,
        help_text='Show the AI SMS Campaign Strategist (plan recommendations) entry points.',
    )
    disabled_action_kinds = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'List of AIRecommendation.Kind values this org has turned off in the '
            'Action Center. Disabled kinds are hidden at read time everywhere (the '
            'Action Center page, per-event badges on the events list, and the sidebar '
            'count); they keep being generated, so re-enabling a kind restores its '
            'suggestions immediately. Empty list = all kinds enabled.'
        ),
    )
    brand_voice_guidelines = models.TextField(
        blank=True,
        default='',
        help_text=(
            'How AI-written marketing messages should sound (tone, formality, '
            'phrases to use or avoid). Takes precedence over the voice auto-detected '
            'from your past messages.'
        ),
    )
    timezone = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text=(
            'IANA timezone (e.g. America/New_York) used when showing scheduled send '
            'times. Blank falls back to the site default.'
        ),
    )
    external_events_enabled = models.BooleanField(
        default=True,
        help_text=(
            'Allow this org to create external (CSV-imported) events. On by default '
            '(external-first onboarding); a data migration backfilled existing orgs.'
        ),
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
    instagram_url = models.URLField(
        max_length=255,
        blank=True,
        default='',
    )
    youtube_url = models.URLField(
        max_length=255,
        blank=True,
        default='',
    )
    tiktok_url = models.URLField(
        max_length=255,
        blank=True,
        default='',
    )
    # Set when the organizer dismisses the dashboard "Getting started" checklist.
    # The checklist's step completion is derived from existing data (events, Stripe
    # onboarding); this only records that the card should stay hidden.
    onboarding_dismissed_at = models.DateTimeField(null=True, blank=True)
    # Same idea for the value-gated "sell through Cue" direct-ticketing upsell.
    directticketing_upsell_dismissed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def get_timezone(self):
        """Return the org's tzinfo, falling back to the site default TIME_ZONE."""
        import zoneinfo
        from django.conf import settings
        for name in (self.timezone, getattr(settings, 'TIME_ZONE', 'UTC')):
            if name:
                try:
                    return zoneinfo.ZoneInfo(name)
                except Exception:
                    continue
        return zoneinfo.ZoneInfo('UTC')


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


def _generate_webhook_secret():
    return f"whsec_{secrets.token_hex(32)}"


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


class WebhookEndpoint(BaseModel):
    """Per-organization outbound webhook endpoint.

    An org registers a URL and subscribes it to one or more domain event types
    (e.g. 'event.created'). When a subscribed event fires, a signed HTTP POST is
    delivered to `url` asynchronously via `deliver_webhook_task`. Each attempt is
    recorded as a WebhookDelivery row.
    """
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='webhook_endpoints',
    )
    label = models.CharField(max_length=100, help_text="Label to identify this endpoint, e.g. 'Zapier — new orders'")
    url = models.URLField(max_length=500)
    secret = models.CharField(
        max_length=100,
        default=_generate_webhook_secret,
        help_text="Used to HMAC-sign each delivery. Verify with the X-Cue-Signature header. "
                  "Deliveries sign with the CURRENT secret at send time, so rotating it can "
                  "cause in-flight/retrying deliveries to fail verification with the old secret.",
    )
    event_types = models.JSONField(
        default=list,
        blank=True,
        help_text="List of subscribed event-type strings, e.g. ['event.created', 'order.created'].",
    )
    is_active = models.BooleanField(default=True)
    description = models.CharField(max_length=255, blank=True, default='')
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'is_active']),
        ]
        verbose_name = 'Webhook endpoint'
        verbose_name_plural = 'Webhook endpoints'

    def __str__(self):
        return f"{self.organization.name} — {self.label}"

    def clean(self):
        # Validate subscribed event types against the canonical set.
        from tickets.services.webhooks.constants import WEBHOOK_EVENT_TYPES
        types = self.event_types or []
        if not isinstance(types, list):
            raise ValidationError({'event_types': 'Must be a list of event-type strings.'})
        invalid = [t for t in types if t not in WEBHOOK_EVENT_TYPES]
        if invalid:
            raise ValidationError({
                'event_types': f"Unknown event type(s): {', '.join(map(str, invalid))}. "
                               f"Valid: {', '.join(WEBHOOK_EVENT_TYPES)}.",
            })
        # SSRF guard: reject non-https / private / loopback / reserved destinations.
        from tickets.services.webhooks.validation import validate_webhook_url
        if self.url:
            try:
                validate_webhook_url(self.url)
            except ValidationError as exc:
                raise ValidationError({'url': exc.messages})

    @property
    def masked_secret(self):
        return f"{self.secret[:11]}...{self.secret[-4:]}"


class WebhookDelivery(BaseModel):
    """Append-only log of a single outbound webhook delivery attempt.

    One row per attempt: a Celery retry produces a new row with an incremented
    `attempt`. `payload` is the exact dict that was signed and POSTed, enabling
    debugging and replay. `created_at` (from BaseModel) is the attempt timestamp.
    """
    endpoint = models.ForeignKey(
        WebhookEndpoint,
        on_delete=models.CASCADE,
        related_name='deliveries',
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='webhook_deliveries',
    )
    event_type = models.CharField(max_length=50, db_index=True)
    # Stable across all retry attempts of one delivery; sent as X-Cue-Delivery-Id
    # so consumers can dedupe at-least-once redeliveries. Null only for legacy rows.
    delivery_id = models.UUIDField(null=True, blank=True, db_index=True)
    payload = models.JSONField(default=dict)
    response_status = models.IntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True, default='')
    attempt = models.PositiveIntegerField(default=1)
    success = models.BooleanField(default=False, db_index=True)
    error_message = models.CharField(max_length=1000, blank=True, default='')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['endpoint', 'created_at']),
            models.Index(fields=['organization', 'event_type', 'created_at']),
            models.Index(fields=['success', 'created_at']),
        ]
        verbose_name = 'Webhook delivery'
        verbose_name_plural = 'Webhook deliveries'

    def __str__(self):
        status = 'ok' if self.success else 'fail'
        return f"{self.event_type} → {self.endpoint_id} [{status}]"


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


class SMSMessagingService(BaseModel):
    """A selectable Twilio Messaging Service for marketing SMS.

    Lets an admin switch the active sender (e.g. Toll-Free 10k/day vs A2P 2k/day)
    live from Django admin without editing env vars and redeploying. Exactly one
    row is active at a time; the active row's SID drives sends (``send_sms``) and
    its ``daily_segment_cap`` drives the daily carrier guard (``daily_segment_cap``
    in ``tickets/services/sms_limits.py``). When no row is active, both fall back
    to the ``TWILIO_MESSAGING_SERVICE_SID`` / ``SMS_DAILY_SEGMENT_CAP`` settings.
    """

    label = models.CharField(
        max_length=100,
        help_text='Human-readable name, e.g. "Toll-Free" or "A2P 10DLC".',
    )
    messaging_service_sid = models.CharField(
        max_length=64,
        help_text='Twilio Messaging Service SID (starts with "MG").',
    )
    sms_from = models.CharField(
        max_length=20,
        blank=True,
        help_text='Optional fallback sender number (E.164) used when no Messaging '
                  'Service SID is set on this row.',
    )
    daily_segment_cap = models.PositiveIntegerField(
        default=2000,
        help_text='Daily segment ceiling for this service (e.g. 10000 toll-free, '
                  '2000 A2P). 0 disables the daily-cap guard.',
    )
    is_active = models.BooleanField(
        default=False,
        db_index=True,
        help_text='Only one service is active at a time; the active one is used '
                  'for all marketing sends.',
    )

    class Meta:
        verbose_name = 'SMS Messaging Service'
        verbose_name_plural = 'SMS Messaging Services'
        ordering = ['-is_active', 'label']

    def __str__(self):
        return f"{self.label} ({'active' if self.is_active else 'inactive'})"

    def save(self, *args, **kwargs):
        from django.db import transaction

        with transaction.atomic():
            super().save(*args, **kwargs)
            if self.is_active:
                # Enforce a single active service.
                SMSMessagingService.objects.exclude(pk=self.pk).filter(
                    is_active=True
                ).update(is_active=False)

    @classmethod
    def get_active(cls):
        """The active messaging service, or None when none is selected."""
        return cls.objects.filter(is_active=True).first()


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
    instagram_handle = models.CharField(max_length=30, blank=True, default='')
    profile_picture = models.ImageField(
        upload_to='user_avatars/',
        storage=_get_media_storage,
        blank=True,
        null=True,
    )
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
        null=True,
        blank=True,
        help_text="Owning organization, or NULL for a global built-in format.",
    )
    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    is_default = models.BooleanField(default=False)
    is_system = models.BooleanField(
        default=False,
        help_text="Built-in format maintained by Cue; read-only for organizations.",
    )
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

    @classmethod
    def available_for(cls, organization):
        """Formats an organization can select: its own plus global built-ins."""
        return cls.objects.filter(
            models.Q(organization=organization)
            | models.Q(organization__isnull=True, is_system=True)
        ).order_by('-is_default', 'name')


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

    class AcquisitionSource(models.TextChoices):
        SUBSCRIBE_FORM  = 'subscribe_form',  'Opt-in form'
        TICKET_PURCHASE = 'ticket_purchase', 'Ticket purchase'
        IMPORT          = 'import',          'CSV Import'
        MANUAL          = 'manual',          'Manual'

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
    # Blank email is allowed for phone-only subscribers (public subscribe page captures
    # phone + consent only). Uniqueness is enforced only when email is present — see the
    # partial UniqueConstraint in Meta. Identity for these rows is keyed on phone.
    email = models.EmailField(db_index=True, blank=True)
    name = models.CharField(max_length=200, blank=True)
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
    # Market this customer self-declared by joining through a market-tagged subscribe
    # link (/subscribe/<org>/?m=<market_id>). Unions with purchase-derived market
    # membership in customer_filters so a market-scoped SMS campaign reaches direct
    # subscribers who have not (yet) bought a ticket. Null = untagged / org-wide.
    home_market = models.ForeignKey(
        'Market',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='home_subscribers',
        db_index=True,
    )
    # Loyalty program tier (denormalized; assigned by LoyaltyTierAssigner, mirrors RFM fields)
    loyalty_tier = models.ForeignKey(
        'LoyaltyTier',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='members',
        db_index=True,
    )
    loyalty_tier_updated_at = models.DateTimeField(null=True, blank=True)
    # Loyalty points (denormalized from LoyaltyPointsTransaction; never mutate
    # outside tickets/services/loyalty/points.py). points_balance is spendable
    # (Phase 2 redemption); lifetime_points only ever grows back to 0 floor and
    # drives the min_lifetime_points tier rule.
    points_balance = models.PositiveIntegerField(default=0)
    lifetime_points = models.PositiveIntegerField(default=0)
    # How this customer first entered the org. Set once at creation and treated as
    # immutable — creation sites stamp it, nothing overwrites it. Blank = Unknown
    # (unattributed legacy rows the backfill couldn't classify).
    acquisition_source = models.CharField(
        max_length=20,
        blank=True,
        db_index=True,
        choices=AcquisitionSource.choices,
        help_text="How this customer first entered the org. Set once at creation; immutable.",
    )

    class Meta:
        ordering = ['-lifetime_value', 'name']
        indexes = [
            models.Index(fields=['email']),
            models.Index(fields=['lifetime_value']),
            models.Index(fields=['last_order_date']),
            # Phone-keyed identity: subscribe merge + import/checkout reconciliation.
            models.Index(fields=['organization', 'phone']),
        ]
        constraints = [
            # Email is unique per org only when present; blank emails (phone-only
            # subscribers) are exempt so many can coexist.
            models.UniqueConstraint(
                fields=['organization', 'email'],
                condition=~models.Q(email=''),
                name='customer_org_email_unique',
            ),
            models.UniqueConstraint(
                fields=['organization', 'phone'],
                condition=models.Q(email='') & ~models.Q(phone=''),
                name='customer_org_phone_phone_only_unique',
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


MARKET_GEOGRAPHY_CITY = 'city'
MARKET_GEOGRAPHY_STATE = 'state'
MARKET_GEOGRAPHY_COUNTRY = 'country'
MARKET_GEOGRAPHY_CHOICES = [
    (MARKET_GEOGRAPHY_CITY, 'City'),
    (MARKET_GEOGRAPHY_STATE, 'State'),
    (MARKET_GEOGRAPHY_COUNTRY, 'Country'),
]


class Market(BaseModel):
    """Organization-scoped geographic market for event grouping."""
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='markets',
    )
    name = models.CharField(max_length=120, db_index=True)
    geography_level = models.CharField(
        max_length=20,
        choices=MARKET_GEOGRAPHY_CHOICES,
        db_index=True,
    )
    geography_value = models.CharField(max_length=120, db_index=True)

    class Meta:
        unique_together = [
            ['organization', 'name'],
            ['organization', 'geography_level', 'geography_value'],
        ]
        ordering = ['geography_level', 'name']
        indexes = [
            models.Index(fields=['organization', 'geography_level', 'geography_value']),
        ]

    def clean(self):
        super().clean()
        valid_levels = {choice[0] for choice in MARKET_GEOGRAPHY_CHOICES}
        if self.geography_level not in valid_levels:
            raise ValidationError({'geography_level': 'Choose a valid geography level.'})
        if not (self.name or '').strip():
            raise ValidationError({'name': 'Market name is required.'})
        if not (self.geography_value or '').strip():
            raise ValidationError({'geography_value': 'Geography value is required.'})

    def save(self, *args, **kwargs):
        self.name = (self.name or '').strip()
        self.geography_value = (self.geography_value or '').strip()
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


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
    ai_summary = models.TextField(blank=True, default='')
    ai_summary_generated_at = models.DateTimeField(blank=True, null=True)
    # Fingerprint (hex digest) of the data that produced the current ai_summary.
    # The daily auto-regeneration job compares a freshly computed fingerprint
    # against this value to decide whether the underlying data changed and the
    # summary needs regenerating. Set on every generation (manual or automatic).
    ai_summary_input_hash = models.CharField(max_length=64, blank=True, default='')
    venue = models.ForeignKey(
        'Venue',
        on_delete=models.PROTECT,
        related_name='events'
    )
    market = models.ForeignKey(
        'Market',
        on_delete=models.SET_NULL,
        related_name='events',
        null=True,
        blank=True,
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
    survey_email_subject = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Per-event override for the survey email subject. "
                  "Use {event} for the event name. Blank = org default.",
    )
    # Per-event override for the survey send schedule. Blank offset_type =
    # inherit the org default (see Event.resolved_survey_schedule()).
    survey_send_offset_type = models.CharField(
        max_length=10, blank=True, default='', choices=SURVEY_SEND_OFFSET_CHOICES,
        help_text="Per-event override for survey send timing. Blank = org default.",
    )
    survey_send_offset_value = models.PositiveSmallIntegerField(null=True, blank=True)
    survey_send_time_of_day = models.TimeField(null=True, blank=True)
    survey_send_anchor = models.CharField(
        max_length=10, blank=True, default='', choices=SURVEY_SEND_ANCHOR_CHOICES,
        help_text="Per-event override: measure the offset from the event 'start' "
                  "or 'end'. Blank = end.",
    )
    survey_auto_send_opted_out = models.BooleanField(
        default=False,
        help_text="Set when a host cancels a scheduled survey send. Stops the "
                  "auto-send scheduler from re-arming this event's survey.",
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
            models.Index(fields=['organization', 'market', '-start_date']),
        ]

    def __str__(self):
        return f"{self.name} - {self.venue.name}, {self.venue.city} ({self.start_date})"

    @property
    def effective_status(self):
        if self.ticketing_type != TICKETING_TYPE_DIRECT or self.status in (
            EVENT_STATUS_DRAFT, EVENT_STATUS_ENDED, EVENT_STATUS_CANCELLED
        ):
            return self.status
        # status == 'live' - check if the event end time has passed
        if self.end_datetime() < timezone.now():
            return EVENT_STATUS_ENDED
        return EVENT_STATUS_LIVE

    @property
    def spans_extra_days(self):
        """True when the event ends two or more calendar days after it starts.

        A same-day event or an overnight one that finishes the next morning
        (end_date == start_date + 1) is not "extra days" — the closing time
        alone communicates the end, so the end date can be hidden. Only an
        event that runs into a third day needs its end date shown.
        """
        if not self.end_date:
            return False
        return (self.end_date - self.start_date).days >= 2

    def get_associated_uploads(self):
        """Get all distinct uploads associated with this event via ticket orders."""
        return UploadedFile.objects.filter(
            ticket_orders__event=self
        ).distinct()

    def get_upload_count(self):
        """Get count of distinct uploads associated with this event."""
        return self.get_associated_uploads().count()

    def resolved_survey_subject(self):
        """Effective survey email subject: event override → org default →
        built-in default, with {event} expanded to the event name."""
        template = (
            (self.survey_email_subject or '').strip()
            or (self.organization.survey_email_subject or '').strip()
            or DEFAULT_SURVEY_SUBJECT
        )
        return template.replace('{event}', self.name)

    def resolved_survey_schedule(self):
        """Effective survey send schedule: event override → org default → None.

        Returns a dict {'offset_type', 'offset_value', 'time_of_day'} or None when
        neither the event nor the org configures a schedule (= send immediately).
        """
        if (self.survey_send_offset_type or '').strip():
            src = self
        elif (self.organization.survey_send_offset_type or '').strip():
            src = self.organization
        else:
            return None
        return {
            'offset_type': src.survey_send_offset_type,
            'offset_value': src.survey_send_offset_value,
            'time_of_day': src.survey_send_time_of_day,
            'anchor': src.survey_send_anchor or 'end',
        }

    def start_datetime(self):
        """Timezone-aware datetime for when the event starts, in the event's own
        timezone. Used to anchor survey-send scheduling to the event start.

        Falls back to midnight when start_time is unset. Returns an aware datetime.
        """
        from datetime import datetime, time as dt_time
        from zoneinfo import ZoneInfo

        start_time = self.start_time or dt_time(0, 0)
        try:
            tz = ZoneInfo(self.timezone)
        except Exception:
            tz = ZoneInfo('America/Los_Angeles')
        return datetime.combine(self.start_date, start_time, tzinfo=tz)

    def end_datetime(self):
        """Timezone-aware datetime for when the event ends, in the event's own
        timezone. Used to anchor relative survey-send scheduling.

        Falls back gracefully: end_date → start_date; end_time → start_time →
        end of day (23:59). Returns an aware datetime.
        """
        from datetime import datetime, time as dt_time
        from zoneinfo import ZoneInfo

        end_date = self.end_date or self.start_date
        end_time = self.end_time or self.start_time or dt_time(23, 59)
        try:
            tz = ZoneInfo(self.timezone)
        except Exception:
            tz = ZoneInfo('America/Los_Angeles')
        return datetime.combine(end_date, end_time, tzinfo=tz)


class EventImage(BaseModel):
    """A photo attached to an event, shown on the public buy page below the description."""
    event = models.ForeignKey('Event', on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to=_event_image_upload_to, storage=_get_media_storage)
    sort_order = models.PositiveIntegerField(default=0, db_index=True)

    class Meta:
        ordering = ['sort_order', 'created_at']

    def __str__(self):
        return f"Image for event {self.event_id}"


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
    api_attributed_orders = models.PositiveIntegerField(null=True, blank=True)
    api_attributed_revenue = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cue_attributed_orders = models.PositiveIntegerField(null=True, blank=True)
    cue_attributed_revenue = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
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
    def attribution_source(self):
        """Which input populates effective_* (manual → cue → api → none)."""
        if self.manual_attributed_orders is not None or self.manual_attributed_revenue is not None:
            return 'manual'
        if self.cue_attributed_orders is not None or self.cue_attributed_revenue is not None:
            return 'cue'
        if self.api_attributed_orders is not None or self.api_attributed_revenue is not None:
            return 'api'
        return 'none'

    @property
    def effective_attributed_orders(self):
        if self.manual_attributed_orders is not None:
            return self.manual_attributed_orders
        if self.cue_attributed_orders is not None:
            return self.cue_attributed_orders
        return self.api_attributed_orders if self.api_attributed_orders is not None else 0

    @property
    def effective_attributed_revenue(self):
        if self.manual_attributed_revenue is not None:
            return self.manual_attributed_revenue
        if self.cue_attributed_revenue is not None:
            return self.cue_attributed_revenue
        return self.api_attributed_revenue if self.api_attributed_revenue is not None else Decimal('0.00')

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
    # Cue-tracked attribution computed from first-party UTMs captured on ticket
    # orders (see services/marketing/sms_attribution.py). None = not computed, so
    # effective_* falls through to SlickText's own (usually empty) numbers.
    cue_attributed_orders = models.PositiveIntegerField(null=True, blank=True)
    cue_attributed_revenue = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
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
    def attribution_source(self):
        """Which input populates effective_orders/revenue (manual → cue → slicktext → none)."""
        if self.manual_orders is not None or self.manual_revenue is not None:
            return 'manual'
        if self.cue_attributed_orders is not None or self.cue_attributed_revenue is not None:
            return 'cue'
        if self.orders or self.revenue:
            return 'slicktext'
        return 'none'

    @property
    def effective_orders(self):
        if self.manual_orders is not None:
            return self.manual_orders
        if self.cue_attributed_orders is not None:
            return self.cue_attributed_orders
        return self.orders

    @property
    def effective_revenue(self):
        if self.manual_revenue is not None:
            return self.manual_revenue
        if self.cue_attributed_revenue is not None:
            return self.cue_attributed_revenue
        return self.revenue

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


class SMSConsentRecord(AuditBaseModel):
    """Provable record of marketing-SMS consent captured at a self-serve
    origination surface (the public /subscribe/ page).

    Cue historically only *imported* already-consented data (CSV, SlickText), so
    consent lived as booleans on Customer. This is the first surface where Cue
    *originates* consent directly, so it needs a defensible audit trail: the exact
    disclosure text shown, the IP and user-agent, and the timestamp. The proof
    fields are frozen once written (see save()); only lifecycle fields
    (verified_at, pending_start, opted_out_at, customer) may change afterward.

    A record only counts as consent when ``verified_at`` is set (OTP passed).
    Unverified rows are pending/abandoned signups and must be excluded everywhere.
    """
    class Source(models.TextChoices):
        SUBSCRIBE_PAGE = 'subscribe_page', 'Subscribe page'
        TEXT_JOIN = 'text_join', 'Text to join'  # forward-compat (Phase 2)

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='sms_consent_records',
    )
    customer = models.ForeignKey(
        'Customer', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='sms_consent_records',
        help_text='Linked after the Customer upsert on verification.',
    )
    # Contact info is denormalized (copied, not read through customer) so the
    # ledger freezes exactly what was disclosed at consent time.
    phone = models.CharField(max_length=20, db_index=True)  # E.164
    email = models.EmailField(blank=True)
    name = models.CharField(max_length=200, blank=True)
    consent_given = models.BooleanField(default=False)
    consent_text = models.TextField(help_text='The exact disclosure the subscriber agreed to.')
    consent_url = models.CharField(max_length=200, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    source = models.CharField(
        max_length=20, choices=Source.choices, default=Source.SUBSCRIBE_PAGE, db_index=True,
    )
    verified_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Set on OTP success; only verified records count as consent.',
    )
    pending_start = models.BooleanField(
        default=False,
        help_text='Phone was globally Twilio-suppressed at consent time; consented '
                  'but unreachable until they text START. Cleared by the inbound webhook.',
    )
    opted_out_at = models.DateTimeField(null=True, blank=True)

    # Frozen once written — mutating any of these raises. Lifecycle fields
    # (verified_at, pending_start, opted_out_at, customer, version) stay mutable.
    _FROZEN_FIELDS = (
        'phone', 'email', 'name', 'consent_given', 'consent_text',
        'consent_url', 'ip_address', 'user_agent', 'source',
    )

    class Meta:
        indexes = [
            models.Index(fields=['organization', 'phone']),
            models.Index(fields=['organization', '-created_at']),
        ]

    def __str__(self):
        state = 'verified' if self.verified_at else 'pending'
        return f"consent {self.phone} ({self.source}, {state})"

    def save(self, *args, **kwargs):
        """Freeze proof fields after the row exists (append-only ledger)."""
        if not self._state.adding:
            old = type(self).objects.filter(pk=self.pk).only(*self._FROZEN_FIELDS).first()
            if old is not None:
                for field in self._FROZEN_FIELDS:
                    if getattr(old, field) != getattr(self, field):
                        raise ValidationError(
                            f"SMSConsentRecord.{field} is immutable once written."
                        )
        super().save(*args, **kwargs)


class SMSCampaignQuerySet(models.QuerySet):
    def by_urgency(self):
        """Order soonest-linked-event first (event-less campaigns last, then oldest
        scheduled). Under the shared daily carrier-cap budget, time-sensitive blasts
        (day-of / day-before an event) should consume today's allowance ahead of
        evergreen ones — so the dispatcher sends and the recovery pass resumes in this
        order. See tickets/services/sms_limits.py."""
        return self.order_by(
            F('event__start_date').asc(nulls_last=True),
            'scheduled_at',
        )

    def due(self, now=None):
        """Scheduled campaigns whose send time has arrived — the scheduler's
        dispatch set, ordered by urgency. Source of truth shared by the dispatcher
        and read-only status commands so the two can never disagree about what's pending."""
        now = now or timezone.now()
        return self.filter(
            status=SMSCampaign.Status.SCHEDULED,
            scheduled_at__lte=now,
        ).by_urgency()

    def upcoming(self, now=None):
        """Scheduled campaigns whose send time is still in the future — frozen,
        charged, and waiting. Not yet dispatchable (that's due()); surfaced for
        operational visibility so a queued-but-not-due campaign isn't mistaken
        for a stuck or missing one. Exact complement of due() over SCHEDULED."""
        now = now or timezone.now()
        return self.filter(
            status=SMSCampaign.Status.SCHEDULED,
            scheduled_at__gt=now,
        )

    def stuck(self, now=None, minutes=None):
        """Campaigns wedged in 'sending' past the recovery threshold (worker
        died mid-send). Re-dispatch is safe — the orchestrator resends only
        still-queued recipients."""
        now = now or timezone.now()
        if minutes is None:
            minutes = SMSCampaign.STUCK_SENDING_MINUTES
        return self.filter(
            status=SMSCampaign.Status.SENDING,
            started_at__lte=now - timezone.timedelta(minutes=minutes),
        ).by_urgency()


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
    # A campaign stuck in 'sending' longer than this (worker died mid-send) is
    # re-dispatched by the scheduler; the orchestrator resends only still-queued
    # recipients. Set comfortably above the worst-case wall-clock of a healthy
    # max-size send (SMS_CAMPAIGN_MAX_RECIPIENTS under Twilio throttling) so
    # recovery never fires alongside a slow-but-live send and double-dispatches.
    STUCK_SENDING_MINUTES = 30

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        SCHEDULED = 'scheduled', 'Scheduled'
        SENDING = 'sending', 'Sending'
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Failed'
        CANCELED = 'canceled', 'Canceled'

    objects = SMSCampaignQuerySet.as_manager()

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
    # Set when this campaign was launched from an AI campaign plan step. Lets the send
    # pipeline hold the campaign while its plan is disabled (paused); null for standalone
    # composer campaigns, which are never gated by a plan.
    plan = models.ForeignKey(
        'SMSCampaignPlan',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='campaigns',
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
    # Human-readable reason a campaign ended in FAILED (e.g. the daily carrier cap was
    # reached at send time). Surfaced on the campaign detail page so the organizer knows
    # to shrink + reschedule. Blank for non-failed campaigns.
    failure_reason = models.CharField(max_length=255, blank=True, default='')
    # Post-send conversion attribution, computed by NativeSMSAttributionCalculator:
    # recipients who bought this campaign's linked event within SMS_ATTRIBUTION_WINDOW_DAYS
    # of their send (last-touch across overlapping sends; refunds excluded). NULL = not yet
    # computed (renders "—" in the list); a concrete value (including 0) once recomputed.
    attributed_orders = models.PositiveIntegerField(null=True, blank=True)
    attributed_revenue = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
    )

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
        from tickets.services.customer_filters import filter_customers, _valid_uuids
        org = organization or self.organization
        criteria = self.filter_criteria or {}
        include_ids = _valid_uuids(self.manual_include_ids or [])
        exclude_ids = _valid_uuids(self.manual_exclude_ids or [])

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
        # Deterministic order so materialize() slicing is stable — the two-batch split
        # (finalize_campaign_split) relies on "first N fit today" resolving the same set
        # on every call, including idempotent replays.
        return qs.distinct().order_by('created_at', 'id')

    def materialize(self, organization=None, cap=None):
        """Return deduped, non-suppressed recipients as a list of
        {'customer_id', 'phone'} dicts (E.164). Dedupe, suppression, and country
        eligibility are done in Python so they work identically on SQLite (dev) and
        Postgres (prod). Numbers outside SMS_ALLOWED_COUNTRY_PREFIXES are excluded here
        — before scheduling/charging — since Twilio Geo Permissions would block them
        (Error 21408) and they aren't billable."""
        from tickets.sms import normalize_phone, sms_country_allowed, is_plausible_e164
        org = organization or self.organization
        suppressed = PhoneSuppression.suppressed_phones(org)
        seen = set()
        out = []
        for customer in self.candidate_customers(org).only('id', 'phone'):
            phone = normalize_phone(customer.phone)
            if not phone or phone in seen or phone in suppressed:
                continue
            if not is_plausible_e164(phone):
                # Malformed number (e.g. a double country code → '+111…'); Twilio would
                # reject it (21211). Drop before scheduling/charging.
                continue
            if not sms_country_allowed(phone):
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
        # T11: use _as_list/_valid_uuids to guard against malformed persisted criteria
        from tickets.services.customer_filters import NO_MARKET_VALUE, _as_list, _valid_uuids
        raw_market_ids = _as_list(criteria.get('market_ids')) + _as_list(criteria.get('market_id'))
        no_market = any(str(v) == NO_MARKET_VALUE for v in raw_market_ids)
        valid_market_ids = _valid_uuids(v for v in raw_market_ids if str(v) != NO_MARKET_VALUE)
        if raw_market_ids:
            market_names = list(
                Market.objects.filter(organization=org, id__in=valid_market_ids)
                .values_list('name', flat=True)
            )
            if no_market:
                market_names.append("No market")
            if market_names:
                parts.append("Markets: " + ", ".join(market_names))
        if self.manual_include_ids:
            parts.append("Custom selection")
        return " · ".join(parts) if parts else "No audience"


class SMSCampaignPlan(BaseModel):
    """An AI-generated multi-touch SMS campaign strategy for an event or segment.

    The plan is advisory: it recommends a sequence of timed touches and writes each
    message, but sends nothing itself. Each step is launched individually into the
    existing composer (via the session prefill handoff), where the organizer reviews,
    confirms cost, and sends through the normal SMSCampaign flow. ``steps`` is a JSON
    list; per-step ``launched_campaign_id`` is filled in when a step is launched.

    ``status`` is a derived label only, rolled up from the live campaign status of each step:
    Draft (nothing launched) → In progress (some steps launched, some still in draft) →
    Scheduled (every step launched, at least one still queued) → Sent (every step delivered).
    It's recomputed from the steps + their campaigns (see ``_plan_progress`` /
    ``_save_plan_steps``) and never gates sending — steps launch regardless. Because a step's
    campaign can flip scheduled → sent asynchronously (outside any plan mutation), the display
    always recomputes live and the stored value is self-healed on render.
    """
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        IN_PROGRESS = 'in_progress', 'In progress'
        SCHEDULED = 'scheduled', 'Scheduled'
        SENT = 'sent', 'Sent'

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='sms_campaign_plans',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sms_campaign_plans',
    )
    # Set for event-based plans; null for pure segment/audience plans.
    event = models.ForeignKey(
        Event,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sms_campaign_plans',
    )
    # The segment/audience the plan targets (same schema as SMSCampaign.filter_criteria
    # / filter_customers). Empty for a pure event plan.
    filter_criteria = models.JSONField(default=dict, blank=True)
    name = models.CharField(max_length=200)
    objective = models.CharField(max_length=300, blank=True, default='')
    strategy_summary = models.TextField(blank=True, default='')
    model_name = models.CharField(max_length=100, blank=True, default='')
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    # Master on/off. A disabled plan is paused: its scheduled sends are held (not sent by
    # the send pipeline) and its steps can't be confirmed, until it's re-enabled. Toggled
    # from the plan detail page and the plans list; does not cancel or refund anything.
    enabled = models.BooleanField(default=True, db_index=True)
    generated_at = models.DateTimeField(default=timezone.now)
    # Ordered sequence. Each entry:
    #   {order, purpose, audience_label, audience_criteria, timing_label, body,
    #    rationale, segments, encoding, launched_campaign_id|null}
    steps = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', '-created_at']),
        ]

    def __str__(self):
        return f"SMS plan: {self.name}"


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
    # Did an opt-out disclosure reach this recipient — via the appended "Reply STOP"
    # footer OR explicit opt-out copy already in the body? Decided + persisted at
    # schedule time (charge), honored at send. Drives the disclosure-cadence lookup
    # (recently_disclosed_phones). default=True: historical rows all carried the
    # footer under the old always-append behavior, so they count as disclosures and
    # the cadence works immediately on deploy.
    stop_disclosed = models.BooleanField(default=True)
    # Charged segment count for this recipient (schedule-time, computed on campaign.body).
    # Auditability; the actual sent count can differ for tracked-link bodies.
    segments = models.PositiveSmallIntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=['campaign', 'status']),
            models.Index(fields=['twilio_sid']),
            # Serves recently_disclosed_phones (phone__in + sent_at range) and the
            # inbound-webhook per-phone most-recent-send lookup.
            models.Index(fields=['phone', 'sent_at']),
        ]

    def __str__(self):
        return f"{self.phone} [{self.status}]"

    @classmethod
    def recently_disclosed_phones(cls, organization, phones, as_of):
        """Subset of ``phones`` that received a STOP disclosure within
        SMS_FOOTER_DISCLOSURE_DAYS before ``as_of``, scoped to this org.

        The org filter is mandatory, not an optimization: two different orgs can
        text the same phone number, and one org's disclosure must never suppress
        another's footer. Only SENT/DELIVERED count — an UNDELIVERED/FAILED message
        never reached the handset, so it disclosed nothing.

        When SMS_ALWAYS_DISCLOSE_STOP is on (the compliant default), no phone counts as
        recently disclosed, so every message carries the footer regardless of cadence.
        """
        if not phones or getattr(settings, 'SMS_ALWAYS_DISCLOSE_STOP', True):
            return set()
        from datetime import timedelta
        cutoff = as_of - timedelta(
            days=getattr(settings, 'SMS_FOOTER_DISCLOSURE_DAYS', 30)
        )
        return set(
            cls.objects.filter(
                campaign__organization=organization,
                phone__in=phones,
                stop_disclosed=True,
                sent_at__gte=cutoff,
                status__in=[cls.Status.SENT, cls.Status.DELIVERED],
            ).values_list('phone', flat=True)
        )


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
    attribution = models.JSONField(
        default=dict, blank=True,
        help_text='First-party UTM/fbclid/referrer captured at checkout for campaign attribution.',
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
    scanned_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="When this individual ticket was scanned in (import or live check-in).",
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

    @classmethod
    def outstanding_for_org(cls, org):
        """Unresolved (NEW/REVIEWED) recommendations for an org, excluding the kinds
        the org has turned off in the Action Center settings.

        Single source of truth for the Action Center count/badges so the sidebar,
        the events-list badges, and the Action Center page stay in sync.
        """
        qs = cls.objects.filter(
            organization=org,
            status__in=[cls.Status.NEW, cls.Status.REVIEWED],
        )
        disabled = getattr(org, 'disabled_action_kinds', None) or []
        if disabled:
            qs = qs.exclude(kind__in=disabled)
        return qs

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
    FEATURE_EVENT_SUMMARY = 'event_summary'
    FEATURE_SMS_PLAN = 'sms_plan'
    FEATURE_BRAND_VOICE_EXAMPLE = 'brand_voice_example'

    FEATURE_CHOICES = [
        (FEATURE_CHAT_AGENT, 'Chat agent'),
        (FEATURE_META_CAMPAIGN_MATCH, 'Meta campaign match'),
        (FEATURE_MAILCHIMP_CAMPAIGN_MATCH, 'Mailchimp campaign match'),
        (FEATURE_SLICKTEXT_CAMPAIGN_MATCH, 'SlickText campaign match'),
        (FEATURE_MARKETING_NARRATIVE, 'Marketing narrative'),
        (FEATURE_TYPEFORM_EVENT_MATCH, 'Typeform event match'),
        (FEATURE_EVENT_SUMMARY, 'Event summary'),
        (FEATURE_SMS_PLAN, 'SMS campaign plan'),
        (FEATURE_BRAND_VOICE_EXAMPLE, 'Brand voice example'),
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
    """Named short link attached to an event for click and purchase attribution.

    Blank ``target_url`` → redirect to the event's Cue buy page (direct ticketing).
    Set ``target_url`` → redirect off-site to a third-party ticket page (imported/
    external events), while still counting clicks. Purchase/revenue attribution only
    works for the buy-page case; external clicks are counted but not attributed to orders.
    """
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
    # Off-site redirect target for external ticket links; blank = the Cue buy page.
    target_url = models.URLField(max_length=500, blank=True, default='')

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
    """One row per Stripe Checkout Session - idempotency anchor for webhook processing.

    Money flow per ``charge_flow`` (see Payout for the payout-side pools):

        platform     buyer ──► PLATFORM acct ──(payout-time Transfer)──► connected acct
                     Legacy flow; rows swept to the connected account by the
                     migrate_legacy_balances command after cutover.
        destination  buyer ──► PLATFORM acct ──(transfer_data at charge time)──► connected acct
                     Organizer net (amount - platform fee) lands in the connected
                     balance at sale time; refunds claw it back via transfer reversal.
        direct       buyer ──► CONNECTED acct (card_present / Tap to Pay)
                     Platform fee collected via application_fee_amount; the
                     connected account pays Stripe processing fees.
    """

    class Status(models.TextChoices):
        PENDING            = 'pending',            'Pending'
        COMPLETED          = 'completed',          'Completed'
        EXPIRED            = 'expired',            'Expired'
        CANCELED           = 'canceled',           'Canceled'
        PARTIALLY_REFUNDED = 'partially_refunded', 'Partially refunded'
        REFUNDED           = 'refunded',           'Refunded'

    class ChargeFlow(models.TextChoices):
        PLATFORM    = 'platform',    'Platform charge (legacy)'
        DESTINATION = 'destination', 'Destination charge'
        DIRECT      = 'direct',      'Direct charge (connected account)'

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
    charge_flow = models.CharField(
        max_length=20,
        choices=ChargeFlow.choices,
        default=ChargeFlow.PLATFORM,
        db_index=True,
        help_text='How the money moved: platform (legacy), destination, or direct (in-person).',
    )
    stripe_transfer_id = models.CharField(
        max_length=255, blank=True, default='',
        help_text='Transfer (tr_xxx) created by the destination charge; empty for platform/direct flows.',
    )
    transfer_cents = models.PositiveIntegerField(
        default=0,
        help_text='Organizer net sent to the connected account by the destination charge.',
    )
    # Display/ledger cache only. Stripe Transfer.amount_reversed is the
    # authority for reversal math — never compute a reversal delta from this.
    transfer_reversed_cents = models.PositiveIntegerField(
        default=0,
        help_text='Cumulative cents reversed back to the platform after refunds (cache of Stripe state).',
    )
    fb_browser_data = models.JSONField(
        default=dict, blank=True,
        help_text='Stores _fbp, _fbc, client IP, user agent for CAPI Purchase call on webhook.',
    )
    attribution = models.JSONField(
        default=dict, blank=True,
        help_text='First-party UTM/fbclid/referrer captured at checkout; copied to the order on fulfillment.',
    )

    class Meta:
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['event', 'status']),
            models.Index(fields=['organization', 'status', 'available_on']),
        ]

    def __str__(self):
        return f"Stripe session {self.stripe_session_id} ({self.status})"


# Built-in fallback subject for survey invitation emails. Use {event} for the
# event name. Overridden per-org (Organization.survey_email_subject) and
# per-event (Event.survey_email_subject); see Event.resolved_survey_subject().
DEFAULT_SURVEY_SUBJECT = "How was {event}? Share your feedback"


class SurveyQuestion(BaseModel):
    """A question in a post-event survey."""
    QUESTION_TYPE_CHOICES = [
        ('star_rating', 'Star Rating (1-5)'),
        ('nps', 'NPS Score (0-10)'),
        ('text', 'Free Text'),
        ('single_select', 'Single Choice'),
        ('multi_select', 'Multiple Choice'),
    ]
    # Question types whose answers are stored as selected options rather than
    # a scalar column on SurveyAnswer.
    CHOICE_TYPES = ('single_select', 'multi_select')

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


class SurveyQuestionOption(BaseModel):
    """A selectable option for a single_select / multi_select SurveyQuestion."""
    question = models.ForeignKey(
        SurveyQuestion,
        on_delete=models.CASCADE,
        related_name='options',
    )
    label = models.CharField(max_length=255)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['position', 'created_at']
        indexes = [
            models.Index(fields=['question', 'position']),
        ]
        constraints = [
            # Composite-FK target: lets SurveyAnswerOption enforce that a chosen
            # option belongs to the same question the answer is for.
            models.UniqueConstraint(
                fields=['id', 'question'],
                name='surveyoption_id_question_uniq',
            ),
        ]

    def __str__(self):
        return self.label


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
    scheduled_send_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text="Absolute UTC time this invitation may be sent. NULL = send "
                  "immediately. The bulk send task only dispatches rows whose "
                  "scheduled time is in the past (or NULL).",
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    send_failed_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Set when a send permanently fails (invalid/refused recipient). "
                  "Excludes the row from future send attempts.",
    )
    send_error = models.CharField(max_length=200, blank=True, default='')

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
    selected_options = models.ManyToManyField(
        SurveyQuestionOption,
        through='SurveyAnswerOption',
        blank=True,
        related_name='answers',
        help_text="Chosen option(s) for single_select / multi_select questions",
    )

    class Meta:
        unique_together = [['response', 'question']]
        ordering = ['question__position']
        constraints = [
            # Composite-FK target so SurveyAnswerOption can pin a selection to
            # the same question this answer is for (see migration 0155 RunSQL).
            models.UniqueConstraint(
                fields=['id', 'question'],
                name='surveyanswer_id_question_uniq',
            ),
        ]

    def clean(self):
        """Enforce that only the field family matching question_type is populated.

        Defends every app code path (admin, fixtures, scripts), not just the
        public form parser. Option-membership is additionally enforced at the
        SurveyAnswerOption level + a DB composite FK.
        """
        super().clean()
        from django.core.exceptions import ValidationError
        qt = self.question.question_type
        if qt == 'star_rating':
            if self.nps_score is not None or self.text_answer:
                raise ValidationError("A star_rating answer must only set star_rating.")
            if self.star_rating is not None and not (1 <= self.star_rating <= 5):
                raise ValidationError("star_rating must be between 1 and 5.")
        elif qt == 'nps':
            if self.star_rating is not None or self.text_answer:
                raise ValidationError("An nps answer must only set nps_score.")
            if self.nps_score is not None and not (0 <= self.nps_score <= 10):
                raise ValidationError("nps_score must be between 0 and 10.")
        elif qt == 'text':
            if self.star_rating is not None or self.nps_score is not None:
                raise ValidationError("A text answer must only set text_answer.")
        elif qt in SurveyQuestion.CHOICE_TYPES:
            if self.star_rating is not None or self.nps_score is not None or self.text_answer:
                raise ValidationError("A choice answer must not set scalar fields.")

    def __str__(self):
        return f"Answer to '{self.question.question_text[:40]}' by {self.response.customer}"


class SurveyAnswerOption(BaseModel):
    """Through-model linking a SurveyAnswer to a chosen SurveyQuestionOption.

    `question` is denormalized so the migration can add composite foreign keys
    ((answer_id, question_id) -> SurveyAnswer(id, question); (option_id,
    question_id) -> SurveyQuestionOption(id, question)). The shared question_id
    forces the chosen option to belong to the answer's question at the DB layer
    (Postgres/prod); SurveyAnswerOption.clean() mirrors it for dev/SQLite.
    """
    answer = models.ForeignKey(
        SurveyAnswer,
        on_delete=models.CASCADE,
        related_name='answer_options',
    )
    option = models.ForeignKey(
        SurveyQuestionOption,
        on_delete=models.PROTECT,
        related_name='answer_options',
    )
    question = models.ForeignKey(
        SurveyQuestion,
        on_delete=models.CASCADE,
        related_name='+',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['answer', 'option'],
                name='surveyansweroption_answer_option_uniq',
            ),
        ]

    def clean(self):
        super().clean()
        from django.core.exceptions import ValidationError
        if self.option.question_id != self.answer.question_id:
            raise ValidationError("Selected option does not belong to the answer's question.")
        if self.question_id != self.answer.question_id:
            raise ValidationError("SurveyAnswerOption.question must match the answer's question.")

    def __str__(self):
        return f"{self.answer_id} -> {self.option_id}"


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
    """One organizer withdrawal (or platform-funds movement) to track in history.

    ``origin`` states which money pool the payout drew from — never infer it
    from field nullness:

        legacy_transfer  pre-cutover flow: platform Transfer + bank Payout
        migration        migrate_legacy_balances true-up Transfer (platform pool)
        cue              in-app Request Payout against the connected balance
        stripe_dashboard organizer-initiated payout discovered via webhook
    """

    class Status(models.TextChoices):
        PENDING    = 'pending',    'Pending'
        IN_TRANSIT = 'in_transit', 'In Transit'
        COMPLETED  = 'completed',  'Completed'
        FAILED     = 'failed',     'Failed'

    class Origin(models.TextChoices):
        LEGACY_TRANSFER  = 'legacy_transfer',  'Legacy transfer + payout'
        MIGRATION        = 'migration',        'Balance migration (true-up)'
        CUE              = 'cue',              'Requested in Cue'
        STRIPE_DASHBOARD = 'stripe_dashboard', 'Initiated via Stripe'

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='payouts', db_index=True,
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    origin = models.CharField(
        max_length=20,
        choices=Origin.choices,
        default=Origin.CUE,
        db_index=True,
        help_text='Which money pool this payout drew from; set explicitly at every creation site.',
    )
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


class DeviceToken(BaseModel):
    """APNs device token for a Cue organizer's iOS device.

    Tokens rotate: on each registration we keep the newest token for an
    organizer and drop the rest. Scoped to the organizer's Organization so
    pushes (launch announcement, 'Tap to Pay is ready') can be fanned out
    per-org. Tokens are environment-specific — a sandbox (dev-build) token
    will not deliver against the production APNs host and vice versa.
    """
    organizer = models.ForeignKey(
        'auth.User',
        on_delete=models.CASCADE,
        related_name='device_tokens',
    )
    organization = models.ForeignKey(
        'Organization',
        on_delete=models.CASCADE,
        related_name='device_tokens',
        db_index=True,
    )
    token = models.CharField(max_length=200, unique=True, db_index=True)
    platform = models.CharField(max_length=16, default='ios', choices=[('ios', 'iOS')])

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['organization'])]

    def __str__(self):
        return f"{self.organizer.get_username()} — {self.token[:16]}… ({self.platform})"


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


# Badge color choices shared with CustomerTag; maps to Bootstrap classes in templates.
LOYALTY_TIER_COLOR_CHOICES = [
    ('blue', 'Blue'),
    ('green', 'Green'),
    ('red', 'Red'),
    ('yellow', 'Yellow'),
    ('purple', 'Purple'),
    ('orange', 'Orange'),
]


class LoyaltyProgram(AuditBaseModel):
    """Organizer-defined, brand loyalty program.

    A program owns a ladder of ``LoyaltyTier`` rows. A background job
    (``LoyaltyTierAssigner``) evaluates every customer against the tiers' rules
    and assigns each to the best tier they qualify for — mirroring how RFM
    segments are computed. Only one program per org is active at a time, so the
    denormalized ``Customer.loyalty_tier`` FK fully represents membership.
    """
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='loyalty_programs',
    )
    name = models.CharField(max_length=120)
    description = models.TextField(
        blank=True,
        help_text="Branding / intro copy describing the program to your team.",
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Only one program per organization can be active at a time.",
    )
    recalc_in_progress = models.BooleanField(default=False)
    last_recalculated_at = models.DateTimeField(null=True, blank=True)
    # Points config. The program supplies HOW points are earned; the balances
    # themselves are (customer, organization) state and survive program
    # replacement (see Customer.points_balance / LoyaltyPointsTransaction).
    points_enabled = models.BooleanField(
        default=False,
        help_text="Award loyalty points for ticket purchases.",
    )

    class PointsBasis(models.TextChoices):
        PER_TICKET = 'per_ticket', 'Per ticket'
        PER_DOLLAR = 'per_dollar', 'Per dollar spent'

    points_basis = models.CharField(
        max_length=12,
        choices=PointsBasis.choices,
        default=PointsBasis.PER_TICKET,
    )
    points_rate = models.DecimalField(
        max_digits=6, decimal_places=2,
        default=Decimal('1'),
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text="Points per ticket (or per dollar). Note: with 'per dollar', free orders earn 0 points.",
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', 'is_active']),
        ]
        constraints = [
            # At most one live, non-deleted active program per organization.
            # Enforced at the DB so concurrent saves can't leave two actives.
            models.UniqueConstraint(
                fields=['organization'],
                condition=models.Q(is_active=True, deleted_at__isnull=True),
                name='one_active_loyalty_program_per_org',
            ),
        ]

    def __str__(self):
        return self.name


class LoyaltyTier(BaseModel):
    """One tier within a ``LoyaltyProgram`` with qualifying rules and perks.

    All rule fields are optional and AND-combined: a customer qualifies when
    they meet *every* threshold that is set. A tier with no rules acts as a
    base/"member" tier that everyone qualifies for. When a customer satisfies
    multiple tiers, the highest ``rank`` wins.
    """
    program = models.ForeignKey(
        LoyaltyProgram,
        on_delete=models.CASCADE,
        related_name='tiers',
    )
    name = models.CharField(max_length=60)
    rank = models.PositiveIntegerField(
        default=0,
        help_text="Higher rank = better tier. Used to break ties when a customer qualifies for several tiers.",
    )
    color = models.CharField(max_length=20, default='blue', choices=LOYALTY_TIER_COLOR_CHOICES)
    perks = models.TextField(
        blank=True,
        help_text="Describe the rewards and perks members of this tier earn.",
    )
    # Qualifying rules (all optional; AND-combined).
    min_lifetime_value = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Minimum total spend (lifetime value) to qualify.",
    )
    min_order_count = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Minimum number of orders placed to qualify.",
    )
    min_events_purchased = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Minimum number of distinct events purchased to qualify.",
    )
    min_tickets_purchased = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Minimum number of tickets purchased to qualify.",
    )
    max_days_since_last_order = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Must have ordered within this many days to qualify (recency).",
    )
    min_lifetime_points = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Minimum lifetime points earned to qualify.",
    )
    # Attendance rules count only tickets actually scanned in at the door
    # (Ticket.scanned_at), so free-RSVP no-shows never qualify — unlike the
    # *_purchased rules above, which count every order regardless of attendance.
    min_events_attended = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Minimum number of distinct events actually attended (checked in) to qualify.",
    )
    attended_within_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Only count events attended within this many days (attendance window). "
                  "Leave blank to count attendance over all time.",
    )
    # "Paid events in the last N days": a paired count + window rule. Counts the
    # distinct events for which the customer has a non-refunded order where money
    # was actually paid (total_amount > 0), so free-RSVP orders never qualify and
    # several paid orders to the same event count once. Mirrors the attendance
    # pair above in shape.
    min_paid_events_recent = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Minimum number of unique events with a paid order (total > $0) "
                  "within the window below to qualify.",
    )
    paid_events_within_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Only count paid events placed within this many days. "
                  "Leave blank to count paid events over all time.",
    )

    class Meta:
        ordering = ['-rank', 'name']
        constraints = [
            models.UniqueConstraint(fields=['program', 'name'], name='loyaltytier_program_name_unique'),
            models.UniqueConstraint(fields=['program', 'rank'], name='loyaltytier_program_rank_unique'),
        ]

    def __str__(self):
        return f"{self.name} ({self.program.name})"

    def qualifies(self, *, lifetime_value, order_count, events_purchased, tickets_purchased, days_since_last_order, lifetime_points=0, events_attended=0, events_attended_in_window=0, paid_event_count=0, paid_event_count_in_window=0):
        """Return True if the given per-customer metrics meet every set rule.

        ``events_attended`` is the all-time distinct-events-attended count;
        ``events_attended_in_window`` is that count restricted to
        ``attended_within_days`` (the caller computes it against this tier's
        window). The attendance rule is active when either ``min_events_attended``
        or ``attended_within_days`` is set: the customer must have attended at
        least ``min_events_attended`` (or 1 if only a window is set) distinct
        events, counted within the window when one is set and over all time
        otherwise.

        ``paid_event_count`` / ``paid_event_count_in_window`` mirror the
        attendance pair for the "paid events in the last N days" rule (active
        when either ``min_paid_events_recent`` or ``paid_events_within_days`` is
        set); it counts the distinct events for which the customer has an order
        with money paid (total_amount > 0).
        """
        if self.min_lifetime_value is not None and (lifetime_value or Decimal('0')) < self.min_lifetime_value:
            return False
        if self.min_order_count is not None and (order_count or 0) < self.min_order_count:
            return False
        if self.min_events_purchased is not None and (events_purchased or 0) < self.min_events_purchased:
            return False
        if self.min_tickets_purchased is not None and (tickets_purchased or 0) < self.min_tickets_purchased:
            return False
        if self.max_days_since_last_order is not None:
            if days_since_last_order is None or days_since_last_order > self.max_days_since_last_order:
                return False
        if self.min_lifetime_points is not None and (lifetime_points or 0) < self.min_lifetime_points:
            return False
        if self.min_events_attended is not None or self.attended_within_days is not None:
            required = self.min_events_attended or 1
            count = events_attended_in_window if self.attended_within_days is not None else events_attended
            if (count or 0) < required:
                return False
        if self.min_paid_events_recent is not None or self.paid_events_within_days is not None:
            required = self.min_paid_events_recent or 1
            count = paid_event_count_in_window if self.paid_events_within_days is not None else paid_event_count
            if (count or 0) < required:
                return False
        return True

    def has_no_rules(self):
        """True when this tier has no qualifying rules set (a catch-all/base tier)."""
        return all(
            getattr(self, f) is None
            for f in (
                'min_lifetime_value', 'min_order_count', 'min_events_purchased',
                'min_tickets_purchased', 'max_days_since_last_order',
                'min_lifetime_points', 'min_events_attended',
                'attended_within_days', 'min_paid_events_recent',
                'paid_events_within_days',
            )
        )

    def qualifying_rules(self):
        """Human-readable summary of each qualifying rule that is set.

        Returns a list of short strings (one per active rule) in the same order
        ``qualifies()`` evaluates them, for display on the tier members page.
        An empty list means the tier is a base/catch-all tier (see
        ``has_no_rules()``).
        """
        rules = []
        if self.min_lifetime_value is not None:
            rules.append(f"Lifetime spend ≥ ${self.min_lifetime_value:,.2f}")
        if self.min_order_count is not None:
            rules.append(f"Orders ≥ {self.min_order_count}")
        if self.min_events_purchased is not None:
            rules.append(f"Events purchased ≥ {self.min_events_purchased}")
        if self.min_tickets_purchased is not None:
            rules.append(f"Tickets purchased ≥ {self.min_tickets_purchased}")
        if self.max_days_since_last_order is not None:
            rules.append(f"Ordered within {self.max_days_since_last_order} days")
        if self.min_lifetime_points is not None:
            rules.append(f"Lifetime points ≥ {self.min_lifetime_points}")
        if self.min_events_attended is not None or self.attended_within_days is not None:
            # Mirror qualifies(): a bare window still requires at least 1 event.
            required = self.min_events_attended or 1
            rule = f"Attended ≥ {required} event{'' if required == 1 else 's'}"
            if self.attended_within_days is not None:
                rule += f" within {self.attended_within_days} days"
            rules.append(rule)
        if self.min_paid_events_recent is not None or self.paid_events_within_days is not None:
            required = self.min_paid_events_recent or 1
            rule = f"Paid events ≥ {required}"
            if self.paid_events_within_days is not None:
                rule += f" within {self.paid_events_within_days} days"
            rules.append(rule)
        return rules


class LoyaltyPointsTransaction(BaseModel):
    """Immutable ledger for customer loyalty points.

    Mirrors ``SMSCreditTransaction``: every balance mutation writes exactly one
    signed row with post-mutation snapshots. Never mutate balances outside
    ``tickets/services/loyalty/points.py``.

    Invariants:
      - At most one EARN and one REVOKE row per ticket_order (partial unique).
      - REVOKE.amount is the APPLIED balance delta (-min(earn, balance)), so
        SUM(amount) per customer == Customer.points_balance — the ledger stays
        sum-auditable even after Phase-2 spending introduces clamping. When
        clamped, the original earn amount is noted in ``description``.
      - customer is CASCADE: the only customer hard-delete path
        (_reconcile_customers_after_order_deletion) runs after revoke-before-
        delete hooks, so any cascaded trail nets to zero. Phase-2 note: once
        ADJUST rows exist, reconcile must skip customers with points_balance>0.
    """

    class Kind(models.TextChoices):
        EARN = 'earn', 'Earned'
        REVOKE = 'revoke', 'Revoked'
        ADJUST = 'adjust', 'Manual adjustment'

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='points_transactions',
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='points_transactions',
    )
    ticket_order = models.ForeignKey(
        'TicketOrder',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='points_transactions',
    )
    kind = models.CharField(max_length=12, choices=Kind.choices, db_index=True)
    amount = models.IntegerField(help_text='Signed: + earn, - revoke (applied delta).')
    balance_after = models.PositiveIntegerField()
    lifetime_after = models.PositiveIntegerField()
    description = models.CharField(max_length=255, blank=True, default='')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer', '-created_at']),
            models.Index(fields=['organization', '-created_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['ticket_order', 'kind'],
                condition=models.Q(ticket_order__isnull=False),
                name='loyaltypoints_one_per_order_kind',
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.amount} pts (customer={self.customer_id})"


class LoyaltyTierTransition(BaseModel):
    """Immutable record of a customer moving between loyalty tiers.

    Written by ``LoyaltyTierAssigner`` whenever a customer's assigned tier
    changes, so the customer timeline can show tier progression over time.
    Forward-only: no history exists prior to this model being introduced, and
    ``LoyaltyTierAssigner`` only records changes it observes on subsequent runs.

    ``from_tier`` / ``to_tier`` are nullable — a customer can enter from "no
    tier" or drop back to none — and use ``SET_NULL`` so deleting a
    ``LoyaltyTier`` definition never erases the transition history.
    """

    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='tier_transitions',
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='tier_transitions',
    )
    from_tier = models.ForeignKey(
        'LoyaltyTier',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )
    to_tier = models.ForeignKey(
        'LoyaltyTier',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )
    changed_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ['-changed_at']
        indexes = [
            models.Index(fields=['customer', '-changed_at']),
        ]

    def __str__(self):
        return (
            f"{self.from_tier_id or 'none'} -> {self.to_tier_id or 'none'} "
            f"(customer={self.customer_id})"
        )
