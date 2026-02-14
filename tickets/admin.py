import json
from django.contrib import admin
from django.utils.html import format_html
from django.db.models import Sum, Count
from django import forms
from .models import (
    CSVFormat, UploadedFile, Customer, Event, TicketOrder, Ticket, TicketTier, Venue
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
    list_display = ['name', 'is_default', 'requires_manual_pricing', 'uses_tiers', 'created_at']
    list_filter = ['is_default', 'requires_manual_pricing', 'uses_tiers', 'created_at']
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


@admin.register(UploadedFile)
class UploadedFileAdmin(admin.ModelAdmin):
    list_display = ['filename', 'csv_format', 'status', 'total_rows', 'processed_rows', 'uploaded_at']
    list_filter = ['status', 'csv_format', 'uploaded_at']
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


class TicketInline(admin.TabularInline):
    model = Ticket
    extra = 0
    readonly_fields = ['id', 'created_at']
    fields = ['ticket_type', 'price', 'tier_name', 'created_at']


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ['name', 'email', 'phone', 'lifetime_value_display', 'last_order_date', 'order_count', 'created_at']
    list_filter = ['last_order_date', 'created_at']
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
        return format_html(
            '<strong style="color: green;">${:,.2f}</strong>',
            float(obj.lifetime_value)
        )
    lifetime_value_display.short_description = 'Lifetime Value'
    lifetime_value_display.admin_order_field = 'lifetime_value'
    
    def order_count(self, obj):
        """Display number of orders."""
        return obj.ticket_orders.count()
    order_count.short_description = 'Orders'
    
    def get_queryset(self, request):
        """Optimize queryset with order count."""
        qs = super().get_queryset(request)
        return qs.annotate(order_count=Count('ticket_orders'))


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = ['name', 'city', 'event_count', 'created_at']
    list_filter = ['city', 'created_at']
    search_fields = ['name', 'city']
    readonly_fields = ['id', 'created_at', 'updated_at']
    
    fieldsets = (
        ('Venue Information', {
            'fields': ('name', 'city')
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


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ['name', 'venue', 'start_date', 'start_time', 'end_date', 'end_time', 'capacity', 'order_count', 'created_at']
    list_filter = ['start_date', 'created_at', 'venue__city']
    search_fields = ['name', 'venue__name', 'venue__city', 'description']
    readonly_fields = ['id', 'created_at', 'updated_at']
    date_hierarchy = 'start_date'

    fieldsets = (
        ('Event Information', {
            'fields': ('name', 'venue', 'start_date', 'start_time', 'end_date', 'end_time', 'description', 'capacity')
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
        """Optimize queryset with order count."""
        qs = super().get_queryset(request)
        return qs.select_related('venue').annotate(order_count=Count('ticket_orders'))


@admin.register(TicketOrder)
class TicketOrderAdmin(admin.ModelAdmin):
    list_display = ['order_number', 'customer', 'event', 'order_date', 'total_amount', 'ticket_count', 'uploaded_file']
    list_filter = ['order_date', 'event', 'uploaded_file', 'created_at']
    search_fields = ['order_number', 'customer__name', 'customer__email', 'event__name']
    readonly_fields = ['id', 'created_at', 'updated_at']
    date_hierarchy = 'order_date'
    inlines = [TicketInline]
    
    fieldsets = (
        ('Order Information', {
            'fields': ('order_number', 'order_date', 'total_amount')
        }),
        ('Relationships', {
            'fields': ('customer', 'event', 'uploaded_file')
        }),
        ('Metadata', {
            'fields': ('id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def ticket_count(self, obj):
        """Display number of tickets in this order."""
        return obj.tickets.count()
    ticket_count.short_description = 'Tickets'
    
    def get_queryset(self, request):
        """Optimize queryset with ticket count."""
        qs = super().get_queryset(request)
        return qs.select_related('customer', 'event', 'uploaded_file').prefetch_related('tickets')


@admin.register(TicketTier)
class TicketTierAdmin(admin.ModelAdmin):
    list_display = ['ticket_type', 'name', 'price', 'allotment', 'tickets_assigned', 'remaining_capacity', 'order', 'uploaded_file']
    list_filter = ['ticket_type', 'uploaded_file', 'created_at']
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


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ['ticket_order', 'ticket_type', 'tier_name', 'price', 'created_at']
    list_filter = ['ticket_type', 'tier_name', 'created_at']
    search_fields = ['ticket_order__order_number', 'ticket_type', 'tier_name']
    readonly_fields = ['id', 'created_at', 'updated_at']
    
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
        """Optimize queryset."""
        qs = super().get_queryset(request)
        return qs.select_related('ticket_order', 'ticket_order__customer', 'ticket_order__event')
