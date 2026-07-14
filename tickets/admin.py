import json
from django.contrib import admin, messages
from django.utils.html import format_html
from django.db import models
from django.db.models import Sum, Count
from django import forms
from .models import (
    Organization, UserProfile, OrganizationMembership, OrganizationInvitation, EmailOTP, PhoneOTP,
    AIRecommendation,
    CSVFormat, UploadedFile, Customer, CustomerTag, Event, ScannerSession, EventExpense, EventEmailCampaign, EventSMSCampaign, EventTalent, TicketOrder, Ticket, TicketTier, Venue, Market,
    CustomField, CustomFieldOption, EventCustomFieldValue,
    IncomeSource, EventIncome,
    SurveyQuestion, SurveyInvitation, SurveyResponse, SurveyAnswer,
    ChatMessage, AITokenUsage, Payout, StripeCheckoutSession,
    ExternalSurveyUpload, ExternalSurveyResponse,
    SaleableTicketType, SaleableTicketTypeTier,
    OrganizerWaitlist,
    PipedreamCalendarConnection,
    OrganizationAPIKey,
    OAuthClient,
    OAuthAccessToken,
    FeatureFlagSettings,
    SMSCampaign, SMSCampaignPlan, SMSMessageRecipient, PhoneSuppression,
    SMSConsentRecord,
    SMSCreditTransaction,
    LoyaltyProgram, LoyaltyTier, LoyaltyPointsTransaction, LoyaltyTierTransition,
)


@admin.register(AIRecommendation)
class AIRecommendationAdmin(admin.ModelAdmin):
    list_display = ['title', 'organization', 'kind', 'priority', 'status', 'confidence', 'event', 'customer', 'created_at']
    list_filter = ['organization', 'kind', 'priority', 'status', 'created_at']
    search_fields = ['title', 'summary', 'dedupe_key', 'event__name', 'customer__email', 'customer__name']
    readonly_fields = ['id', 'created_at', 'updated_at', 'reviewed_at', 'dismissed_at', 'resolved_at']
    autocomplete_fields = ['organization', 'event', 'customer']
    date_hierarchy = 'created_at'


class JSONWidget(forms.Textarea):
    """Custom widget to format JSON properly in admin."""
    def format_value(self, value):
        if value is None:
            return ''
        if isinstance(value, dict):
            return json.dumps(value, indent=2)
        if isinstance(value, str):
            try:
                # Try to parse and reformat if it's valid JSON
                parsed = json.loads(value)
                return json.dumps(parsed, indent=2)
            except (json.JSONDecodeError, TypeError):
                return value
        return str(value)


@admin.register(CSVFormat)
class CSVFormatAdmin(admin.ModelAdmin):
    list_display = ['name', 'organization', 'is_default', 'requires_manual_pricing', 'uses_tiers', 'created_at']
    list_filter = ['organization', 'is_default', 'requires_manual_pricing', 'uses_tiers', 'created_at']
    search_fields = ['name', 'description']
    readonly_fields = ['id', 'created_at', 'updated_at']
    
    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # Use custom widget for column_mapping field
        form.base_fields['column_mapping'].widget = JSONWidget(attrs={
            'rows': 15,
            'style': 'font-family: monospace;'
        })
        return form
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'description', 'is_default', 'requires_manual_pricing', 'uses_tiers')
        }),
        ('Column Mapping', {
            'fields': ('column_mapping',),
            'description': 'JSON mapping of CSV column names to internal field names'
        }),
        ('Metadata', {
            'fields': ('id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(organization=profile.organization)
        return qs.none()

    def save_model(self, request, obj, form, change):
        if not change and not getattr(obj, 'organization_id', None):
            profile = getattr(request.user, 'profile', None)
            if profile and profile.organization_id:
                obj.organization = profile.organization
        super().save_model(request, obj, form, change)


@admin.register(UploadedFile)
class UploadedFileAdmin(admin.ModelAdmin):
    list_display = ['filename', 'csv_format', 'organization', 'status', 'total_rows', 'processed_rows', 'uploaded_at']
    list_filter = ['organization', 'status', 'csv_format', 'uploaded_at']
    search_fields = ['filename', 'description', 'source']
    readonly_fields = ['id', 'uploaded_at', 'created_at', 'updated_at']
    autocomplete_fields = ['csv_format']
    date_hierarchy = 'uploaded_at'

    fieldsets = (
        ('File Information', {
            'fields': ('csv_format', 'filename', 'description', 'source', 'status')
        }),
        ('Processing Information', {
            'fields': ('total_rows', 'processed_rows', 'metadata')
        }),
        ('Metadata', {
            'fields': ('id', 'uploaded_at', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(organization=profile.organization)
        return qs.none()

    def save_model(self, request, obj, form, change):
        if not change and not getattr(obj, 'organization_id', None):
            profile = getattr(request.user, 'profile', None)
            if profile and profile.organization_id:
                obj.organization = profile.organization
        super().save_model(request, obj, form, change)


class TicketInline(admin.TabularInline):
    model = Ticket
    extra = 0
    readonly_fields = ['id', 'created_at']
    fields = ['ticket_type', 'price', 'tier_name', 'created_at']


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ['name', 'email', 'phone', 'organization', 'lifetime_value_display', 'last_order_date', 'order_count', 'created_at']
    list_filter = ['organization', 'last_order_date', 'created_at']
    search_fields = ['name', 'email', 'phone']
    readonly_fields = ['id', 'lifetime_value', 'last_order_date', 'sms_opt_in_date', 'created_at', 'updated_at']
    date_hierarchy = 'created_at'
    
    filter_horizontal = ('tags',)
    fieldsets = (
        ('Customer Information', {
            'fields': ('name', 'email', 'phone')
        }),
        ('Tags', {
            'fields': ('tags',)
        }),
        ('SMS Marketing', {
            'fields': ('sms_opt_in', 'sms_opt_in_date')
        }),
        ('Lifetime Value', {
            'fields': ('lifetime_value', 'last_order_date')
        }),
        ('Metadata', {
            'fields': ('id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def lifetime_value_display(self, obj):
        """Display LTV as formatted currency."""
        value_str = '${:,.2f}'.format(float(obj.lifetime_value))
        return format_html('<strong style="color: green;">{}</strong>', value_str)
    lifetime_value_display.short_description = 'Lifetime Value'
    lifetime_value_display.admin_order_field = 'lifetime_value'
    
    def order_count(self, obj):
        """Display number of orders."""
        return obj.ticket_orders.count()
    order_count.short_description = 'Orders'
    
    def get_queryset(self, request):
        """Optimize queryset with order count; filter by org for non-superusers."""
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            profile = getattr(request.user, 'profile', None)
            if profile and profile.organization_id:
                qs = qs.filter(organization=profile.organization)
            else:
                qs = qs.none()
        return qs.annotate(order_count=Count('ticket_orders'))

    def save_model(self, request, obj, form, change):
        if not change and not getattr(obj, 'organization_id', None):
            profile = getattr(request.user, 'profile', None)
            if profile and profile.organization_id:
                obj.organization = profile.organization
        super().save_model(request, obj, form, change)


@admin.register(CustomerTag)
class CustomerTagAdmin(admin.ModelAdmin):
    list_display = ['name', 'color', 'organization', 'customer_count', 'created_at']
    list_filter = ['organization', 'color']
    search_fields = ['name']
    readonly_fields = ['id', 'created_at', 'updated_at']
    autocomplete_fields = ['organization']

    def customer_count(self, obj):
        return obj.customers.count()
    customer_count.short_description = 'Customers'


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = ['name', 'city', 'state', 'country', 'capacity', 'event_count', 'organization', 'created_at']
    list_filter = ['organization', 'city', 'state', 'country', 'created_at']
    search_fields = [
        'name', 'city', 'street_address', 'state', 'postal_code', 'country'
    ]
    readonly_fields = ['id', 'created_at', 'updated_at']

    def save_model(self, request, obj, form, change):
        if not change and not getattr(obj, 'organization_id', None):
            profile = getattr(request.user, 'profile', None)
            if profile and profile.organization_id:
                obj.organization = profile.organization
        super().save_model(request, obj, form, change)
    
    fieldsets = (
        ('Venue Information', {
            'fields': ('name', 'city', 'capacity')
        }),
        ('Address', {
            'fields': ('street_address', 'state', 'postal_code', 'country')
        }),
        ('Metadata', {
            'fields': ('id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def event_count(self, obj):
        """Display number of events at this venue."""
        return obj.events.count()
    event_count.short_description = 'Events'
    
    def get_queryset(self, request):
        """Optimize queryset with event count; filter by org for non-superusers."""
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            profile = getattr(request.user, 'profile', None)
            if profile and profile.organization_id:
                qs = qs.filter(organization=profile.organization)
            else:
                qs = qs.none()
        return qs.annotate(event_count=Count('events'))


@admin.register(Market)
class MarketAdmin(admin.ModelAdmin):
    list_display = ['name', 'geography_level', 'geography_value', 'event_count', 'organization', 'created_at']
    list_filter = ['organization', 'geography_level', 'created_at']
    search_fields = ['name', 'geography_value']
    readonly_fields = ['id', 'created_at', 'updated_at']
    autocomplete_fields = ['organization']

    fieldsets = (
        ('Market', {
            'fields': ('organization', 'name', 'geography_level', 'geography_value')
        }),
        ('Metadata', {
            'fields': ('id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def event_count(self, obj):
        return obj.event_count
    event_count.short_description = 'Events'
    event_count.admin_order_field = 'event_count'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            profile = getattr(request.user, 'profile', None)
            if profile and profile.organization_id:
                qs = qs.filter(organization=profile.organization)
            else:
                qs = qs.none()
        return qs.annotate(event_count=Count('events'))

    def save_model(self, request, obj, form, change):
        if not change and not getattr(obj, 'organization_id', None):
            profile = getattr(request.user, 'profile', None)
            if profile and profile.organization_id:
                obj.organization = profile.organization
        super().save_model(request, obj, form, change)


class EventTalentInline(admin.TabularInline):
    model = EventTalent
    extra = 2


class EventExpenseInline(admin.TabularInline):
    model = EventExpense
    extra = 1
    fields = ['category', 'description', 'amount', 'expense_date', 'notes']


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ['name', 'venue', 'organization', 'start_date', 'start_time', 'end_date', 'end_time', 'capacity', 'scanner_pin', 'order_count', 'created_at']
    list_filter = ['organization', 'start_date', 'created_at', 'venue__city']
    search_fields = ['name', 'venue__name', 'venue__city', 'description', 'ticket_link']
    readonly_fields = ['id', 'created_at', 'updated_at']
    autocomplete_fields = ['venue']
    date_hierarchy = 'start_date'
    inlines = [EventTalentInline, EventExpenseInline]

    fieldsets = (
        ('Event Information', {
            'fields': (
                'name', 'venue', 'start_date', 'start_time', 'end_date', 'end_time',
                'description', 'capacity', 'ticket_link',
            )
        }),
        ('Scanner Access', {
            'fields': ('scanner_pin',),
            'description': 'PIN for guest scanner login. Leave blank to disable scanner access.',
        }),
        ('Metadata', {
            'fields': ('id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def order_count(self, obj):
        """Display number of orders for this event."""
        return obj.ticket_orders.count()
    order_count.short_description = 'Orders'
    
    def get_queryset(self, request):
        """Optimize queryset with order count; filter by org for non-superusers."""
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            profile = getattr(request.user, 'profile', None)
            if profile and profile.organization_id:
                qs = qs.filter(organization=profile.organization)
            else:
                qs = qs.none()
        return qs.select_related('venue').annotate(order_count=Count('ticket_orders'))

    def save_model(self, request, obj, form, change):
        if not change and not getattr(obj, 'organization_id', None):
            profile = getattr(request.user, 'profile', None)
            if profile and profile.organization_id:
                obj.organization = profile.organization
        super().save_model(request, obj, form, change)


@admin.register(ScannerSession)
class ScannerSessionAdmin(admin.ModelAdmin):
    list_display = ['event', 'token', 'is_active', 'created_at']
    list_filter = ['is_active', 'event__organization']
    search_fields = ['event__name', 'token']
    readonly_fields = ['id', 'token', 'created_at', 'updated_at']
    list_editable = ['is_active']

    fieldsets = (
        ('Session', {
            'fields': ('event', 'token', 'is_active')
        }),
        ('Metadata', {
            'fields': ('id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    autocomplete_fields = ['event']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            profile = getattr(request.user, 'profile', None)
            if profile and profile.organization_id:
                return qs.filter(event__organization=profile.organization)
            return qs.none()
        return qs.select_related('event')


@admin.register(EventExpense)
class EventExpenseAdmin(admin.ModelAdmin):
    list_display = ['description', 'category', 'source', 'amount', 'expense_date', 'get_event', 'get_organization', 'created_at']
    list_filter = ['event__organization', 'category', 'source', 'expense_date', 'created_at']
    search_fields = ['description', 'notes', 'external_id', 'event__name']
    readonly_fields = ['id', 'created_at', 'updated_at']
    autocomplete_fields = ['event']
    date_hierarchy = 'expense_date'

    fieldsets = (
        ('Expense Information', {
            'fields': ('event', 'category', 'source', 'description', 'amount', 'expense_date', 'notes')
        }),
        ('External Source', {
            'fields': ('external_id', 'external_metadata'),
            'classes': ('collapse',)
        }),
        ('Metadata', {
            'fields': ('id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def get_event(self, obj):
        return obj.event.name if obj.event_id else ''
    get_event.short_description = 'Event'
    get_event.admin_order_field = 'event__name'

    def get_organization(self, obj):
        return obj.event.organization if obj.event_id else ''
    get_organization.short_description = 'Organization'
    get_organization.admin_order_field = 'event__organization'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs.select_related('event', 'event__organization')
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(event__organization=profile.organization).select_related('event', 'event__organization')
        return qs.none()


@admin.register(EventEmailCampaign)
class EventEmailCampaignAdmin(admin.ModelAdmin):
    list_display = ['campaign_title', 'source', 'emails_sent', 'unique_opens', 'unique_clicks', 'ecommerce_revenue', 'get_event', 'get_organization', 'send_time']
    list_filter = ['event__organization', 'source', 'send_time', 'created_at']
    search_fields = ['campaign_title', 'subject_line', 'external_id', 'event__name']
    readonly_fields = ['id', 'created_at', 'updated_at', 'last_synced_at']
    autocomplete_fields = ['event', 'confirmed_by']
    date_hierarchy = 'send_time'

    def get_event(self, obj):
        return obj.event.name if obj.event_id else ''
    get_event.short_description = 'Event'
    get_event.admin_order_field = 'event__name'

    def get_organization(self, obj):
        return obj.event.organization if obj.event_id else ''
    get_organization.short_description = 'Organization'
    get_organization.admin_order_field = 'event__organization'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs.select_related('event', 'event__organization')
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(event__organization=profile.organization).select_related('event', 'event__organization')
        return qs.none()


@admin.register(EventSMSCampaign)
class EventSMSCampaignAdmin(admin.ModelAdmin):
    list_display = ['name', 'source', 'audience_size', 'unique_clicks', 'unsubscribes', 'revenue', 'get_event', 'get_organization', 'send_time']
    list_filter = ['event__organization', 'source', 'send_time', 'created_at']
    search_fields = ['name', 'message', 'external_id', 'event__name']
    readonly_fields = ['id', 'created_at', 'updated_at', 'last_synced_at']
    autocomplete_fields = ['event', 'confirmed_by']
    date_hierarchy = 'send_time'

    def get_event(self, obj):
        return obj.event.name if obj.event_id else ''
    get_event.short_description = 'Event'
    get_event.admin_order_field = 'event__name'

    def get_organization(self, obj):
        return obj.event.organization if obj.event_id else ''
    get_organization.short_description = 'Organization'
    get_organization.admin_order_field = 'event__organization'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs.select_related('event', 'event__organization')
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(event__organization=profile.organization).select_related('event', 'event__organization')
        return qs.none()


@admin.register(IncomeSource)
class IncomeSourceAdmin(admin.ModelAdmin):
    list_display = ['name', 'order', 'organization', 'created_at']
    list_filter = ['organization', 'created_at']
    search_fields = ['name']
    autocomplete_fields = ['organization']
    ordering = ['organization', 'order', 'name']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs.select_related('organization')
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(organization=profile.organization).select_related('organization')
        return qs.none()


@admin.register(EventIncome)
class EventIncomeAdmin(admin.ModelAdmin):
    list_display = ['income_source', 'amount', 'income_date', 'get_event', 'get_organization', 'created_at']
    list_filter = ['event__organization', 'income_source', 'income_date', 'created_at']
    search_fields = ['income_source__name', 'notes', 'event__name']
    readonly_fields = ['id', 'created_at', 'updated_at']
    autocomplete_fields = ['event', 'income_source']
    date_hierarchy = 'income_date'

    fieldsets = (
        ('Income Information', {
            'fields': ('event', 'income_source', 'amount', 'income_date', 'notes')
        }),
        ('Metadata', {
            'fields': ('id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def get_event(self, obj):
        return obj.event.name if obj.event_id else ''
    get_event.short_description = 'Event'
    get_event.admin_order_field = 'event__name'

    def get_organization(self, obj):
        return obj.event.organization if obj.event_id else ''
    get_organization.short_description = 'Organization'
    get_organization.admin_order_field = 'event__organization'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs.select_related('event', 'event__organization', 'income_source')
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(event__organization=profile.organization).select_related('event', 'event__organization', 'income_source')
        return qs.none()


@admin.register(TicketOrder)
class TicketOrderAdmin(admin.ModelAdmin):
    list_display = ['order_number', 'customer', 'event', 'get_organization', 'order_date', 'total_amount', 'ticket_count', 'stripe_dashboard_link', 'uploaded_file']
    list_filter = ['event__organization', 'order_date', 'event', 'uploaded_file', 'created_at']
    search_fields = ['order_number', 'external_order_number', 'customer__name', 'customer__email', 'event__name']
    readonly_fields = ['id', 'created_at', 'updated_at', 'stripe_dashboard_link']
    autocomplete_fields = ['customer', 'event', 'uploaded_file']
    date_hierarchy = 'order_date'
    inlines = [TicketInline]

    def get_organization(self, obj):
        return obj.event.organization if obj.event_id else ''
    get_organization.short_description = 'Organization'
    get_organization.admin_order_field = 'event__organization'
    
    fieldsets = (
        ('Order Information', {
            'fields': ('order_number', 'external_order_number', 'order_date', 'total_amount')
        }),
        ('Relationships', {
            'fields': ('customer', 'event', 'uploaded_file')
        }),
        ('Stripe', {
            'fields': ('stripe_dashboard_link',),
            'classes': ('collapse',),
        }),
        ('Metadata', {
            'fields': ('id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def stripe_dashboard_link(self, obj):
        try:
            session = obj.stripe_checkout_session
        except Exception:
            return '-'
        pi_id = session.stripe_payment_intent_id
        if not pi_id:
            return '-'
        url = f"https://dashboard.stripe.com/payments/{pi_id}"
        return format_html('<a href="{}" target="_blank" rel="noopener">View in Stripe</a>', url)
    stripe_dashboard_link.short_description = 'Stripe'

    def ticket_count(self, obj):
        """Display number of tickets in this order."""
        return obj.tickets.count()
    ticket_count.short_description = 'Tickets'

    def get_queryset(self, request):
        """Optimize queryset with ticket count; filter by org for non-superusers."""
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            profile = getattr(request.user, 'profile', None)
            if profile and profile.organization_id:
                qs = qs.filter(event__organization=profile.organization)
            else:
                qs = qs.none()
        return qs.select_related(
            'customer', 'event', 'uploaded_file', 'stripe_checkout_session'
        ).prefetch_related('tickets')


@admin.register(TicketTier)
class TicketTierAdmin(admin.ModelAdmin):
    list_display = ['ticket_type', 'name', 'price', 'allotment', 'tickets_assigned', 'remaining_capacity', 'order', 'uploaded_file', 'get_organization']
    list_filter = ['uploaded_file__organization', 'ticket_type', 'uploaded_file', 'created_at']

    def get_organization(self, obj):
        return obj.uploaded_file.organization if obj.uploaded_file_id else ''
    get_organization.short_description = 'Organization'
    get_organization.admin_order_field = 'uploaded_file__organization'
    search_fields = ['ticket_type', 'name', 'uploaded_file__filename']
    readonly_fields = ['id', 'created_at', 'updated_at']
    autocomplete_fields = ['uploaded_file']

    fieldsets = (
        ('Tier Information', {
            'fields': ('ticket_type', 'name', 'price', 'allotment', 'order')
        }),
        ('Assignment Tracking', {
            'fields': ('tickets_assigned', 'uploaded_file')
        }),
        ('Metadata', {
            'fields': ('id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def remaining_capacity(self, obj):
        """Display remaining capacity."""
        return obj.remaining_capacity()
    remaining_capacity.short_description = 'Remaining'
    remaining_capacity.admin_order_field = 'tickets_assigned'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(uploaded_file__organization=profile.organization)
        return qs.none()


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ['ticket_order', 'ticket_type', 'tier_name', 'price', 'get_organization', 'created_at']
    list_filter = ['ticket_order__event__organization', 'ticket_type', 'tier_name', 'created_at']
    search_fields = ['ticket_order__order_number', 'ticket_type', 'tier_name']
    readonly_fields = ['id', 'created_at', 'updated_at']
    autocomplete_fields = ['ticket_order', 'tier']

    def get_organization(self, obj):
        return obj.ticket_order.event.organization if obj.ticket_order_id and obj.ticket_order.event_id else ''
    get_organization.short_description = 'Organization'
    get_organization.admin_order_field = 'ticket_order__event__organization'
    
    fieldsets = (
        ('Ticket Information', {
            'fields': ('ticket_order', 'ticket_type', 'price', 'tier', 'tier_name')
        }),
        ('Metadata', {
            'fields': ('id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        """Optimize queryset; filter by org for non-superusers."""
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            profile = getattr(request.user, 'profile', None)
            if profile and profile.organization_id:
                qs = qs.filter(ticket_order__event__organization=profile.organization)
            else:
                qs = qs.none()
        return qs.select_related('ticket_order', 'ticket_order__customer', 'ticket_order__event')


class CustomFieldOptionInline(admin.TabularInline):
    model = CustomFieldOption
    extra = 1


@admin.register(CustomField)
class CustomFieldAdmin(admin.ModelAdmin):
    list_display = ['name', 'field_type', 'order', 'required', 'default_option', 'organization']
    list_filter = ['organization', 'field_type', 'required']
    search_fields = ['name']
    ordering = ['order', 'name']
    inlines = [CustomFieldOptionInline]
    autocomplete_fields = ['organization']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(organization=profile.organization)
        return qs.none()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'default_option':
            obj = getattr(request, '_current_customfield_obj', None)
            if obj is not None and obj.pk:
                kwargs['queryset'] = CustomFieldOption.objects.filter(custom_field_id=obj.pk)
            else:
                kwargs['queryset'] = CustomFieldOption.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_form(self, request, obj=None, **kwargs):
        request._current_customfield_obj = obj
        return super().get_form(request, obj, **kwargs)

    def save_model(self, request, obj, form, change):
        if not change and not getattr(obj, 'organization_id', None):
            profile = getattr(request.user, 'profile', None)
            if profile and profile.organization_id:
                obj.organization = profile.organization
        super().save_model(request, obj, form, change)


@admin.register(CustomFieldOption)
class CustomFieldOptionAdmin(admin.ModelAdmin):
    list_display = ['custom_field', 'label', 'order', 'get_organization']
    list_filter = ['custom_field__organization', 'custom_field']
    search_fields = ['label', 'custom_field__name']
    autocomplete_fields = ['custom_field']

    def get_organization(self, obj):
        return obj.custom_field.organization if obj.custom_field_id else ''
    get_organization.short_description = 'Organization'
    get_organization.admin_order_field = 'custom_field__organization'
    ordering = ['custom_field', 'order', 'label']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(custom_field__organization=profile.organization)
        return qs.none()


@admin.register(EventCustomFieldValue)
class EventCustomFieldValueAdmin(admin.ModelAdmin):
    list_display = ['event', 'custom_field', 'custom_field_option', 'get_organization']
    list_filter = ['event__organization', 'custom_field']

    def get_organization(self, obj):
        return obj.event.organization if obj.event_id else ''
    get_organization.short_description = 'Organization'
    get_organization.admin_order_field = 'event__organization'
    search_fields = ['event__name']
    raw_id_fields = ['event']
    autocomplete_fields = ['custom_field', 'custom_field_option']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(custom_field__organization=profile.organization)
        return qs.none()


class OrganizationMembershipInline(admin.TabularInline):
    model = OrganizationMembership
    extra = 0
    fields = ['user', 'org_role', 'created_at']
    readonly_fields = ['created_at']
    autocomplete_fields = ['user']


@admin.register(OrganizationMembership)
class OrganizationMembershipAdmin(admin.ModelAdmin):
    list_display = ['user', 'organization', 'org_role', 'created_at']
    list_filter = ['org_role', 'organization']
    search_fields = ['user__email', 'user__first_name', 'user__last_name', 'organization__name']
    readonly_fields = ['id', 'created_at', 'updated_at']
    autocomplete_fields = ['user', 'organization']


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'external_events_enabled', 'sms_marketing_enabled', 'stripe_onboarding_complete', 'meta_ads_account_name', 'created_at']
    list_editable = ['external_events_enabled', 'sms_marketing_enabled']
    search_fields = ['name', 'slug']
    readonly_fields = [
        'id',
        'created_at',
        'updated_at',
        'meta_ads_user_id',
        'meta_ads_account_id',
        'meta_ads_account_name',
        'meta_ads_token_expires_at',
        'mailchimp_account_id',
        'mailchimp_account_name',
        'mailchimp_login_email',
        'mailchimp_dc',
    ]
    prepopulated_fields = {'slug': ('name',)}
    inlines = [OrganizationMembershipInline]
    fieldsets = (
        ('Basic', {'fields': ('name', 'slug', 'rfm_recalc_in_progress')}),
        ('Feature Flags', {'fields': ('external_events_enabled', 'waitlist_feature_enabled', 'sms_marketing_enabled', 'loyalty_feature_enabled', 'ai_event_summary_enabled')}),
        ('SMS Credits', {'fields': ('sms_credit_balance_cents',), 'description': 'Prepaid wallet balance in cents. For audited changes prefer creating an SMS credit transaction.'}),
        ('Stripe Connect', {'fields': ('stripe_account_id', 'stripe_onboarding_complete')}),
        ('Meta Ads', {
            'fields': (
                'meta_ads_user_id',
                'meta_ads_account_id',
                'meta_ads_account_name',
                'meta_ads_token_expires_at',
            ),
            'classes': ('collapse',),
        }),
        ('Mailchimp', {
            'fields': (
                'mailchimp_account_id',
                'mailchimp_account_name',
                'mailchimp_login_email',
                'mailchimp_dc',
            ),
            'classes': ('collapse',),
        }),
        ('Metadata', {'fields': ('id', 'created_at', 'updated_at'), 'classes': ('collapse',)}),
    )


@admin.register(FeatureFlagSettings)
class FeatureFlagSettingsAdmin(admin.ModelAdmin):
    list_display = [
        'browse_events_enabled',
        'smart_pricing_recommendations_enabled',
    ]
    readonly_fields = ['singleton_enforcer']
    fieldsets = (
        ('Global Feature Flags', {
            'fields': (
                'browse_events_enabled',
                'smart_pricing_recommendations_enabled',
            ),
            'description': 'Global feature toggles for the app. Waitlist remains organization-scoped on each Organization record.',
        }),
    )

    def has_add_permission(self, request):
        if FeatureFlagSettings.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        settings_obj = FeatureFlagSettings.get_solo()
        from django.shortcuts import redirect
        from django.urls import reverse
        return redirect(reverse('admin:tickets_featureflagsettings_change', args=[settings_obj.pk]))


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = ['organization', 'amount', 'status', 'stripe_transfer_id', 'initiated_by', 'created_at']
    list_filter = ['status', 'organization']
    search_fields = ['organization__name', 'stripe_transfer_id']
    readonly_fields = ['stripe_transfer_id', 'created_at', 'updated_at']
    autocomplete_fields = ['organization', 'initiated_by']
    ordering = ['-created_at']
    actions = ['sync_with_stripe']

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('organization', 'initiated_by')

    @admin.action(description='Sync status from Stripe')
    def sync_with_stripe(self, request, queryset):
        import stripe as stripe_lib

        status_map = {
            'in_transit': Payout.Status.IN_TRANSIT,
            'paid':       Payout.Status.COMPLETED,
            'failed':     Payout.Status.FAILED,
            'canceled':   Payout.Status.FAILED,
        }
        updated = 0
        skipped = 0
        errors = 0

        for payout in queryset:
            org = payout.organization
            if not org.stripe_account_id or not payout.stripe_payout_id:
                skipped += 1
                continue
            try:
                stripe_payout = stripe_lib.Payout.retrieve(
                    payout.stripe_payout_id,
                    stripe_account=org.stripe_account_id,
                )
            except stripe_lib.error.StripeError:
                errors += 1
                continue

            stripe_status = stripe_payout.get('status') if isinstance(stripe_payout, dict) else getattr(stripe_payout, 'status', None)
            new_status = status_map.get(stripe_status)
            if new_status and payout.status != new_status:
                payout.status = new_status
                payout.save(update_fields=['status'])
                updated += 1

        if updated:
            self.message_user(request, f'{updated} payout(s) updated from Stripe.')
        if skipped:
            self.message_user(request, f'{skipped} payout(s) skipped (no Stripe IDs).', level='warning')
        if errors:
            self.message_user(request, f'{errors} payout(s) failed to sync (Stripe API error).', level='error')


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'organization', 'role', 'phone_number', 'user_full_name', 'impersonate_link']
    list_filter = ['role', 'organization']
    search_fields = ['user__username', 'user__first_name', 'user__last_name', 'user__email', 'phone_number']
    raw_id_fields = ['user']
    autocomplete_fields = ['organization']

    def user_full_name(self, obj):
        return obj.user.get_full_name() if obj.user_id else ''
    user_full_name.short_description = 'Full Name'

    def impersonate_link(self, obj):
        # Mirror the view guardrail: never offer to impersonate a privileged account.
        if not obj.user_id or obj.user.is_superuser or obj.user.is_staff:
            return '—'
        from django.urls import reverse
        url = reverse('tickets:admin_impersonate_start', args=[obj.user_id])
        return format_html('<a href="{}">Log in as</a>', url)
    impersonate_link.short_description = 'Impersonate'


@admin.register(OrganizationInvitation)
class OrganizationInvitationAdmin(admin.ModelAdmin):
    list_display = ['email', 'organization', 'role', 'status', 'invited_by', 'expires_at', 'created_at']
    list_filter = ['organization', 'role', 'status', 'created_at']
    search_fields = ['email', 'organization__name']
    readonly_fields = ['id', 'token', 'created_at', 'updated_at', 'accepted_at', 'accepted_by']
    raw_id_fields = ['invited_by', 'accepted_by']
    autocomplete_fields = ['organization']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs.select_related('organization', 'invited_by', 'accepted_by')
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(organization=profile.organization).select_related('organization', 'invited_by', 'accepted_by')
        return qs.none()


@admin.register(PhoneOTP)
class PhoneOTPAdmin(admin.ModelAdmin):
    list_display = ['phone_number', 'purpose', 'is_verified', 'attempts', 'created_at']
    list_filter = ['purpose', 'is_verified', 'created_at']
    search_fields = ['phone_number']
    readonly_fields = ['id', 'otp_code', 'signup_data', 'created_at', 'updated_at']
    date_hierarchy = 'created_at'


@admin.register(EmailOTP)
class EmailOTPAdmin(admin.ModelAdmin):
    list_display = ['email', 'purpose', 'is_verified', 'attempts', 'created_at']
    list_filter = ['purpose', 'is_verified', 'created_at']
    search_fields = ['email']
    readonly_fields = ['id', 'otp_code', 'signup_data', 'created_at', 'updated_at']
    date_hierarchy = 'created_at'

    fieldsets = (
        ('OTP Information', {
            'fields': ('email', 'otp_code', 'purpose', 'is_verified', 'attempts')
        }),
        ('Signup Data', {
            'fields': ('signup_data',),
            'classes': ('collapse',),
        }),
        ('Metadata', {
            'fields': ('id', 'created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )


@admin.register(SurveyQuestion)
class SurveyQuestionAdmin(admin.ModelAdmin):
    list_display = ['question_text', 'question_type', 'position', 'is_required', 'is_active', 'event', 'organization']
    list_filter = ['question_type', 'is_required', 'is_active', 'organization']
    search_fields = ['question_text']
    readonly_fields = ['id', 'created_at', 'updated_at']
    autocomplete_fields = ['event', 'organization']
    ordering = ['position']

    @staticmethod
    def _has_answers(obj):
        return bool(obj) and obj.pk and obj.answers.exists()

    def get_readonly_fields(self, request, obj=None):
        """Lock structural fields once a question has answers (versioning is
        enforced by the builder; admin must not silently corrupt history)."""
        ro = list(super().get_readonly_fields(request, obj))
        if self._has_answers(obj):
            ro += ['question_type', 'event', 'organization']
        return ro

    def has_delete_permission(self, request, obj=None):
        if self._has_answers(obj):
            return False
        return super().has_delete_permission(request, obj)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs.select_related('event', 'organization')
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(
                models.Q(organization=profile.organization) | models.Q(organization__isnull=True)
            ).select_related('event', 'organization')
        return qs.none()


@admin.register(SurveyInvitation)
class SurveyInvitationAdmin(admin.ModelAdmin):
    list_display = ['customer', 'event', 'organization', 'email', 'sent_at', 'completed_at', 'created_at']
    list_filter = ['organization', 'sent_at', 'completed_at']
    search_fields = ['email', 'customer__name', 'customer__email', 'event__name']
    readonly_fields = ['id', 'token', 'created_at', 'updated_at']
    raw_id_fields = ['customer', 'event']
    autocomplete_fields = ['organization']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs.select_related('customer', 'event', 'organization')
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(organization=profile.organization).select_related('customer', 'event', 'organization')
        return qs.none()


@admin.register(SurveyResponse)
class SurveyResponseAdmin(admin.ModelAdmin):
    list_display = ['customer', 'event', 'organization', 'submitted_at']
    list_filter = ['organization', 'submitted_at']
    search_fields = ['customer__name', 'customer__email', 'event__name']
    readonly_fields = ['id', 'created_at', 'updated_at', 'submitted_at']
    raw_id_fields = ['customer', 'event', 'invitation']
    autocomplete_fields = ['organization']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs.select_related('customer', 'event', 'organization', 'invitation')
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(organization=profile.organization).select_related('customer', 'event', 'organization', 'invitation')
        return qs.none()


@admin.register(SurveyAnswer)
class SurveyAnswerAdmin(admin.ModelAdmin):
    list_display = ['get_question_text', 'star_rating', 'nps_score', 'text_answer_preview', 'get_event', 'created_at']
    list_filter = ['question__question_type', 'response__organization']
    search_fields = ['question__question_text', 'text_answer', 'response__customer__name']
    readonly_fields = ['id', 'created_at', 'updated_at']
    raw_id_fields = ['response', 'question']

    def get_question_text(self, obj):
        return obj.question.question_text[:60] if obj.question_id else ''
    get_question_text.short_description = 'Question'
    get_question_text.admin_order_field = 'question__question_text'

    def text_answer_preview(self, obj):
        return (obj.text_answer[:80] + '...') if len(obj.text_answer) > 80 else obj.text_answer
    text_answer_preview.short_description = 'Text Answer'

    def get_event(self, obj):
        return obj.response.event.name if obj.response_id and obj.response.event_id else ''
    get_event.short_description = 'Event'
    get_event.admin_order_field = 'response__event__name'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs.select_related('response', 'response__event', 'response__organization', 'question')
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(response__organization=profile.organization).select_related(
                'response', 'response__event', 'response__organization', 'question'
            )
        return qs.none()


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ['conversation_id', 'role', 'content_preview', 'user', 'organization', 'created_at']
    list_filter = ['organization', 'role', 'created_at']
    search_fields = ['content', 'user__username', 'user__email']
    readonly_fields = ['id', 'created_at', 'updated_at']
    autocomplete_fields = ['organization', 'user']
    date_hierarchy = 'created_at'

    def content_preview(self, obj):
        return (obj.content[:80] + '...') if len(obj.content) > 80 else obj.content
    content_preview.short_description = 'Content'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs.select_related('organization', 'user')
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(organization=profile.organization).select_related('organization', 'user')
        return qs.none()


@admin.register(AITokenUsage)
class AITokenUsageAdmin(admin.ModelAdmin):
    list_display = [
        'occurred_at', 'organization', 'feature', 'model_name',
        'prompt_tokens', 'completion_tokens', 'total_tokens', 'user',
    ]
    list_filter = ['organization', 'feature', 'model_name', 'occurred_at']
    search_fields = ['organization__name', 'user__username', 'user__email', 'request_id']
    readonly_fields = [
        'id', 'organization', 'user', 'feature', 'model_name',
        'prompt_tokens', 'completion_tokens', 'total_tokens',
        'request_id', 'occurred_at', 'metadata', 'created_at', 'updated_at',
    ]
    date_hierarchy = 'occurred_at'

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related('organization', 'user')
        if request.user.is_superuser:
            return qs
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(organization=profile.organization)
        return qs.none()

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ExternalSurveyUpload)
class ExternalSurveyUploadAdmin(admin.ModelAdmin):
    list_display = ['filename', 'organization', 'uploaded_at', 'row_count', 'status']
    list_filter = ['organization', 'status', 'uploaded_at']
    readonly_fields = ['id', 'uploaded_at', 'created_at', 'updated_at']
    search_fields = ['filename', 'organization__name']
    autocomplete_fields = ['organization']


@admin.register(ExternalSurveyResponse)
class ExternalSurveyResponseAdmin(admin.ModelAdmin):
    list_display = ['responded_at', 'organization', 'city', 'overall_rating', 'nps_score', 'event']
    list_filter = ['organization', 'city', 'responded_at']
    raw_id_fields = ['upload', 'event']
    readonly_fields = ['id', 'created_at', 'updated_at']
    search_fields = ['email', 'city', 'text_feedback']
    autocomplete_fields = ['organization']


class SaleableTicketTypeTierInline(admin.TabularInline):
    model = SaleableTicketTypeTier
    extra = 0
    readonly_fields = ['quantity_sold']
    fields = ['name', 'price', 'allotment', 'quantity_sold', 'order']


@admin.register(SaleableTicketType)
class SaleableTicketTypeAdmin(admin.ModelAdmin):
    list_display = ['name', 'event', 'price', 'quantity_sold', 'quantity_limit', 'low_stock_threshold', 'is_active', 'order']
    list_filter = ['event__organization', 'is_active']
    search_fields = ['name', 'event__name']
    readonly_fields = ['id', 'created_at', 'updated_at', 'quantity_sold']
    autocomplete_fields = ['event', 'unlocks_after']
    inlines = [SaleableTicketTypeTierInline]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs.select_related('event')
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(event__organization=profile.organization).select_related('event')
        return qs.none()


@admin.register(PipedreamCalendarConnection)
class PipedreamCalendarConnectionAdmin(admin.ModelAdmin):
    list_display = ['organization', 'webhook_url', 'created_at', 'updated_at']
    search_fields = ['organization__name', 'webhook_url']
    readonly_fields = ['id', 'created_at', 'updated_at']
    autocomplete_fields = ['organization']


@admin.register(OrganizationAPIKey)
class OrganizationAPIKeyAdmin(admin.ModelAdmin):
    list_display = ('name', 'organization', 'masked_key', 'is_active', 'last_used_at', 'created_at')
    list_filter = ('is_active', 'organization')
    search_fields = ('name', 'organization__name')
    readonly_fields = ('key', 'masked_key', 'last_used_at', 'created_at', 'updated_at')
    autocomplete_fields = ('organization',)


@admin.register(OAuthClient)
class OAuthClientAdmin(admin.ModelAdmin):
    list_display = ['client_name', 'client_id', 'created_at']
    readonly_fields = ['client_id', 'created_at', 'updated_at']
    search_fields = ['client_name', 'client_id']


@admin.register(OAuthAccessToken)
class OAuthAccessTokenAdmin(admin.ModelAdmin):
    list_display = ['client', 'organization', 'expires_at', 'created_at']
    readonly_fields = ['token', 'created_at', 'updated_at']
    list_filter = ['organization', 'client']
    autocomplete_fields = ['client', 'organization']


@admin.register(OrganizerWaitlist)
class OrganizerWaitlistAdmin(admin.ModelAdmin):
    list_display = ['name', 'email', 'organization_name', 'instagram_handle', 'status', 'created_at']
    list_filter = ['status', 'created_at']
    search_fields = ['name', 'email', 'organization_name', 'instagram_handle']
    readonly_fields = ['id', 'created_at', 'updated_at', 'approved_at', 'approved_by']
    actions = ['approve_selected']

    @admin.action(description='Approve selected entries')
    def approve_selected(self, request, queryset):
        from django.utils import timezone
        updated = queryset.update(
            status=OrganizerWaitlist.Status.APPROVED,
            approved_at=timezone.now(),
            approved_by=request.user,
        )
        self.message_user(request, f'{updated} entr{"y" if updated == 1 else "ies"} approved.')


@admin.register(SMSCampaign)
class SMSCampaignAdmin(admin.ModelAdmin):
    list_display = ['name', 'organization', 'status', 'audience_size', 'scheduled_at', 'sent_at', 'created_at']
    list_filter = ['organization', 'status', 'created_at']
    search_fields = ['name', 'body']
    readonly_fields = ['id', 'started_at', 'sent_at', 'audience_size', 'created_at', 'updated_at']


@admin.register(SMSCampaignPlan)
class SMSCampaignPlanAdmin(admin.ModelAdmin):
    list_display = ['name', 'organization', 'event', 'model_name', 'generated_at', 'created_at']
    list_filter = ['organization', 'created_at']
    search_fields = ['name', 'objective', 'strategy_summary']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(SMSMessageRecipient)
class SMSMessageRecipientAdmin(admin.ModelAdmin):
    list_display = ['phone', 'campaign', 'status', 'twilio_sid', 'sent_at', 'delivered_at']
    list_filter = ['status']
    search_fields = ['phone', 'twilio_sid']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(PhoneSuppression)
class PhoneSuppressionAdmin(admin.ModelAdmin):
    list_display = ['phone', 'organization', 'reason', 'created_at']
    list_filter = ['reason', 'organization']
    search_fields = ['phone']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(SMSConsentRecord)
class SMSConsentRecordAdmin(admin.ModelAdmin):
    """Consent ledger is view-only in admin — it is legal proof, never edited."""
    list_display = ['phone', 'email', 'organization', 'source', 'verified_at', 'pending_start', 'created_at']
    list_filter = ['source', 'pending_start', 'organization']
    search_fields = ['phone', 'email', 'name']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]


@admin.register(SMSCreditTransaction)
class SMSCreditTransactionAdmin(admin.ModelAdmin):
    list_display = ['organization', 'kind', 'amount_cents', 'balance_after_cents', 'campaign', 'created_at']
    list_filter = ['kind', 'organization']
    search_fields = ['organization__name', 'stripe_checkout_session_id', 'description']
    readonly_fields = ['id', 'balance_after_cents', 'created_at', 'updated_at']
    autocomplete_fields = ['organization', 'campaign']


class LoyaltyTierInline(admin.TabularInline):
    model = LoyaltyTier
    extra = 1


@admin.register(LoyaltyProgram)
class LoyaltyProgramAdmin(admin.ModelAdmin):
    list_display = ['name', 'organization', 'is_active', 'recalc_in_progress', 'last_recalculated_at']
    list_filter = ['is_active', 'organization']
    search_fields = ['name', 'organization__name']
    readonly_fields = ['id', 'recalc_in_progress', 'last_recalculated_at', 'created_at', 'updated_at']
    inlines = [LoyaltyTierInline]
    actions = ['reset_loyalty_points']

    @admin.action(description='Reset loyalty points to scratch (delete ledger, zero balances)')
    def reset_loyalty_points(self, request, queryset):
        """Hard-reset every selected program's org back to zero points.

        Irreversible — deletes the org's points ledger and zeroes balances — so
        it runs through an intermediate confirmation page. De-dupes by org (one
        active program per org, but selections can overlap) and refuses any org
        with a backfill/recalc in flight.
        """
        from django.contrib.admin import helpers
        from django.template.response import TemplateResponse
        from tickets.services.loyalty.points import reset_points_for_organization

        # De-dupe selected programs down to their organizations.
        orgs = {}
        for program in queryset.select_related('organization'):
            orgs.setdefault(program.organization_id, program.organization)
        orgs = list(orgs.values())

        if request.POST.get('confirm'):
            done, blocked = 0, []
            for org in orgs:
                # Refuse if a backfill/recalc is mid-flight for this org.
                if LoyaltyProgram.objects.filter(
                    organization=org, recalc_in_progress=True,
                ).exists():
                    blocked.append(org)
                    continue
                summary = reset_points_for_organization(org)
                done += 1
                self.message_user(
                    request,
                    f'Reset points for "{org.name}": deleted '
                    f'{summary["transactions_deleted"]} ledger rows, zeroed '
                    f'{summary["customers_reset"]} customers.',
                    level=messages.SUCCESS,
                )
            for org in blocked:
                self.message_user(
                    request,
                    f'Skipped "{org.name}": a backfill/recalc is in progress. '
                    f'Wait for it to finish, then retry.',
                    level=messages.WARNING,
                )
            return None

        context = {
            **self.admin_site.each_context(request),
            'title': 'Reset loyalty points',
            'organizations': orgs,
            'queryset': queryset,
            'action_checkbox_name': helpers.ACTION_CHECKBOX_NAME,
            'opts': self.model._meta,
            'media': self.media,
        }
        return TemplateResponse(
            request, 'admin/reset_loyalty_points_confirm.html', context,
        )


@admin.register(LoyaltyTier)
class LoyaltyTierAdmin(admin.ModelAdmin):
    list_display = ['name', 'program', 'rank', 'min_lifetime_value', 'min_order_count', 'min_events_purchased', 'min_lifetime_points']
    list_filter = ['program']
    search_fields = ['name', 'program__name']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(LoyaltyPointsTransaction)
class LoyaltyPointsTransactionAdmin(admin.ModelAdmin):
    """Read-only ledger view — balances are mutated only by the points service."""
    list_display = ['customer', 'kind', 'amount', 'balance_after', 'lifetime_after', 'ticket_order', 'created_at']
    list_filter = ['kind', 'organization']
    search_fields = ['customer__email', 'customer__name', 'description']
    readonly_fields = [
        'id', 'organization', 'customer', 'ticket_order', 'kind', 'amount',
        'balance_after', 'lifetime_after', 'description', 'created_by',
        'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LoyaltyTierTransition)
class LoyaltyTierTransitionAdmin(admin.ModelAdmin):
    """Read-only history — rows are written only by LoyaltyTierAssigner."""
    list_display = ['customer', 'from_tier', 'to_tier', 'changed_at']
    list_filter = ['organization']
    search_fields = ['customer__email', 'customer__name']
    readonly_fields = [
        'id', 'organization', 'customer', 'from_tier', 'to_tier',
        'changed_at', 'created_at', 'updated_at',
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
