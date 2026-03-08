import json
from django.contrib import admin
from django.utils.html import format_html
from django.db import models
from django.db.models import Sum, Count
from django import forms
from .models import (
    Organization, UserProfile, OrganizationInvitation, EmailOTP, PhoneOTP,
    CSVFormat, UploadedFile, Customer, Event, EventExpense, EventTalent, TicketOrder, Ticket, TicketTier, Venue,
    CustomField, CustomFieldOption, EventCustomFieldValue,
    IncomeSource, EventIncome,
    SurveyQuestion, SurveyInvitation, SurveyResponse, SurveyAnswer,
    ChatMessage, Payout, StripeCheckoutSession,
    ExternalSurveyUpload, ExternalSurveyResponse,
)


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
    readonly_fields = ['id', 'lifetime_value', 'last_order_date', 'created_at', 'updated_at']
    date_hierarchy = 'created_at'
    
    fieldsets = (
        ('Customer Information', {
            'fields': ('name', 'email', 'phone')
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


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = ['name', 'city', 'state', 'country', 'capacity', 'event_count', 'organization', 'created_at']
    list_filter = ['organization', 'city', 'state', 'country', 'created_at']
    search_fields = [
        'name', 'city', 'street_address', 'state', 'postal_code', 'country'
    ]
    readonly_fields = ['id', 'created_at', 'updated_at']

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
        """Optimize queryset with event count."""
        qs = super().get_queryset(request)
        return qs.annotate(event_count=Count('events'))


class EventTalentInline(admin.TabularInline):
    model = EventTalent
    extra = 2


class EventExpenseInline(admin.TabularInline):
    model = EventExpense
    extra = 1
    fields = ['category', 'description', 'amount', 'expense_date', 'notes']


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ['name', 'venue', 'organization', 'start_date', 'start_time', 'end_date', 'end_time', 'capacity', 'order_count', 'created_at']
    list_filter = ['organization', 'start_date', 'created_at', 'venue__city']
    search_fields = ['name', 'venue__name', 'venue__city', 'description', 'ticket_link']
    readonly_fields = ['id', 'created_at', 'updated_at']
    date_hierarchy = 'start_date'
    inlines = [EventTalentInline, EventExpenseInline]

    fieldsets = (
        ('Event Information', {
            'fields': (
                'name', 'venue', 'start_date', 'start_time', 'end_date', 'end_time',
                'description', 'capacity', 'ticket_link',
            )
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


@admin.register(EventExpense)
class EventExpenseAdmin(admin.ModelAdmin):
    list_display = ['description', 'category', 'amount', 'expense_date', 'get_event', 'get_organization', 'created_at']
    list_filter = ['event__organization', 'category', 'expense_date', 'created_at']
    search_fields = ['description', 'notes', 'event__name']
    readonly_fields = ['id', 'created_at', 'updated_at']
    date_hierarchy = 'expense_date'

    fieldsets = (
        ('Expense Information', {
            'fields': ('event', 'category', 'description', 'amount', 'expense_date', 'notes')
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


@admin.register(IncomeSource)
class IncomeSourceAdmin(admin.ModelAdmin):
    list_display = ['name', 'order', 'organization', 'created_at']
    list_filter = ['organization', 'created_at']
    search_fields = ['name']
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
    ordering = ['order', 'name']
    inlines = [CustomFieldOptionInline]

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

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        profile = getattr(request.user, 'profile', None)
        if profile and profile.organization_id:
            return qs.filter(custom_field__organization=profile.organization)
        return qs.none()


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'stripe_onboarding_complete', 'created_at']
    search_fields = ['name', 'slug']
    readonly_fields = ['id', 'created_at', 'updated_at']
    prepopulated_fields = {'slug': ('name',)}
    fieldsets = (
        ('Basic', {'fields': ('name', 'slug', 'rfm_recalc_in_progress')}),
        ('Stripe Connect', {'fields': ('stripe_account_id', 'stripe_onboarding_complete')}),
        ('Metadata', {'fields': ('id', 'created_at', 'updated_at'), 'classes': ('collapse',)}),
    )


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = ['organization', 'amount', 'status', 'stripe_transfer_id', 'initiated_by', 'created_at']
    list_filter = ['status', 'organization']
    search_fields = ['organization__name', 'stripe_transfer_id']
    readonly_fields = ['stripe_transfer_id', 'created_at', 'updated_at']
    ordering = ['-created_at']


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'organization', 'role', 'phone_number', 'user_full_name']
    list_filter = ['role', 'organization']
    search_fields = ['user__username', 'user__first_name', 'user__last_name', 'user__email', 'phone_number']
    raw_id_fields = ['user']

    def user_full_name(self, obj):
        return obj.user.get_full_name() if obj.user_id else ''
    user_full_name.short_description = 'Full Name'


@admin.register(OrganizationInvitation)
class OrganizationInvitationAdmin(admin.ModelAdmin):
    list_display = ['email', 'organization', 'role', 'status', 'invited_by', 'expires_at', 'created_at']
    list_filter = ['organization', 'role', 'status', 'created_at']
    search_fields = ['email', 'organization__name']
    readonly_fields = ['id', 'token', 'created_at', 'updated_at', 'accepted_at', 'accepted_by']
    raw_id_fields = ['invited_by', 'accepted_by']

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
    ordering = ['position']

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


@admin.register(ExternalSurveyUpload)
class ExternalSurveyUploadAdmin(admin.ModelAdmin):
    list_display = ['filename', 'organization', 'uploaded_at', 'row_count', 'status']
    list_filter = ['organization', 'status', 'uploaded_at']
    readonly_fields = ['id', 'uploaded_at', 'created_at', 'updated_at']
    search_fields = ['filename', 'organization__name']


@admin.register(ExternalSurveyResponse)
class ExternalSurveyResponseAdmin(admin.ModelAdmin):
    list_display = ['responded_at', 'organization', 'city', 'overall_rating', 'nps_score', 'event']
    list_filter = ['organization', 'city', 'responded_at']
    raw_id_fields = ['upload', 'event']
    readonly_fields = ['id', 'created_at', 'updated_at']
    search_fields = ['email', 'city', 'text_feedback']
