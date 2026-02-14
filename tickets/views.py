import os
import json
from datetime import date
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.urls import reverse_lazy
from django.db.models import Sum, Count, Avg, Q, Subquery, OuterRef
from django.db.models.functions import Coalesce
from django.db import models
from django.core.paginator import Paginator
from django.http import JsonResponse, Http404, HttpResponse
from django.views.decorators.http import require_http_methods
from django.db import connection, transaction
import pandas as pd

from .models import (
    CSVFormat, UploadedFile, Customer, Event, TicketOrder, Ticket, Venue
)
from .forms import CSVUploadForm, TicketPriceEntryForm, CSVFormatForm, VenueForm, EventForm, LoginForm
from .csv_processor import CSVProcessor
from .services.forecasting.preview import generate_forecast_preview


# Authentication Views
class LoginView(auth_views.LoginView):
    """Custom login view."""
    template_name = 'tickets/auth/login.html'
    form_class = LoginForm
    redirect_authenticated_user = True


def login_view(request):
    """Login view wrapper."""
    return LoginView.as_view()(request)


class LogoutView(auth_views.LogoutView):
    """Custom logout view."""
    template_name = 'tickets/auth/logged_out.html'


def logout_view(request):
    """Logout view wrapper."""
    return LogoutView.as_view()(request)


class PasswordResetView(auth_views.PasswordResetView):
    """Password reset request view."""
    template_name = 'tickets/auth/password_reset.html'
    email_template_name = 'tickets/auth/password_reset_email.html'
    subject_template_name = 'tickets/auth/password_reset_subject.txt'
    success_url = reverse_lazy('tickets:password_reset_done')


def password_reset_request(request):
    """Password reset request view wrapper."""
    return PasswordResetView.as_view()(request)


class PasswordResetDoneView(auth_views.PasswordResetDoneView):
    """Password reset done view."""
    template_name = 'tickets/auth/password_reset_done.html'


def password_reset_done(request):
    """Password reset done view wrapper."""
    return PasswordResetDoneView.as_view()(request)


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    """Password reset confirm view."""
    template_name = 'tickets/auth/password_reset_confirm.html'
    success_url = reverse_lazy('tickets:password_reset_complete')


def password_reset_confirm(request, uidb64, token):
    """Password reset confirm view wrapper."""
    return PasswordResetConfirmView.as_view()(request, uidb64=uidb64, token=token)


class PasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    """Password reset complete view."""
    template_name = 'tickets/auth/password_reset_complete.html'


def password_reset_complete(request):
    """Password reset complete view wrapper."""
    return PasswordResetCompleteView.as_view()(request)


def health_check(request):
    """Health check endpoint for Render monitoring."""
    try:
        # Check database connection
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return HttpResponse("OK", status=200)
    except Exception as e:
        return HttpResponse(f"Database connection failed: {str(e)}", status=503)


@login_required
def home(request):
    """Home/dashboard page with overview statistics."""
    # Recent events
    recent_events = Event.objects.annotate(
        total_orders=Count('ticket_orders', distinct=True),
        upload_count=Count('ticket_orders__uploaded_file', distinct=True),
        total_revenue=Coalesce(
            Subquery(
                TicketOrder.objects.filter(
                    event=OuterRef('pk')
                ).values('event').annotate(
                    total=Sum('total_amount')
                ).values('total')[:1],
                output_field=models.DecimalField(max_digits=10, decimal_places=2)
            ),
            Decimal('0.00')
        ),
    ).select_related('venue').order_by('-start_date')[:10]

    # Summary statistics
    total_customers = Customer.objects.count()
    total_orders = TicketOrder.objects.count()
    total_revenue = TicketOrder.objects.aggregate(
        total=Sum('total_amount')
    )['total'] or Decimal('0.00')
    total_tickets = Ticket.objects.count()
    
    context = {
        'recent_events': recent_events,
        'today': date.today(),
        'total_customers': total_customers,
        'total_orders': total_orders,
        'total_revenue': total_revenue,
        'total_tickets': total_tickets,
    }
    return render(request, 'tickets/home.html', context)


@login_required
@require_http_methods(["GET", "POST"])
def upload_csv(request):
    """Handle CSV file upload and processing."""
    if request.method == 'POST':
        form = CSVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            csv_file = form.cleaned_data['csv_file']
            csv_format = form.cleaned_data['csv_format']
            venue = form.cleaned_data['venue']
            
            # Save uploaded file
            uploaded_file = UploadedFile.objects.create(
                csv_format=csv_format,
                filename=csv_file.name,
                description='',
                source='',
                metadata={
                    'notes': form.cleaned_data.get('notes', ''),
                    'event_name': form.cleaned_data.get('event_name', ''),
                    'event_start_date': form.cleaned_data.get('event_start_date').isoformat() if form.cleaned_data.get('event_start_date') else '',
                    'event_start_time': form.cleaned_data.get('event_start_time').isoformat() if form.cleaned_data.get('event_start_time') else '',
                    'venue_id': str(venue.id),
                    'venue_name': venue.name,
                    'venue_city': venue.city,
                }
            )
            
            # Save file to media directory
            media_path = os.path.join('uploads', f"{uploaded_file.id}_{csv_file.name}")
            os.makedirs(os.path.dirname(os.path.join('media', media_path)), exist_ok=True)
            with open(os.path.join('media', media_path), 'wb+') as destination:
                for chunk in csv_file.chunks():
                    destination.write(chunk)
            
            uploaded_file.metadata['file_path'] = media_path
            uploaded_file.save(update_fields=['metadata'])
            
            # Check if manual pricing is required
            if csv_format.requires_manual_pricing:
                # Redirect to price entry
                return redirect('tickets:price_entry', file_id=uploaded_file.id)
            else:
                # Process CSV directly
                return process_csv_file(request, uploaded_file)
    else:
        form = CSVUploadForm()
    
    return render(request, 'tickets/upload.html', {'form': form})


@login_required
@require_http_methods(["GET", "POST"])
def price_entry(request, file_id):
    """Display form for manually entering ticket prices or tiers."""
    uploaded_file = get_object_or_404(UploadedFile, id=file_id)
    uses_tiers = uploaded_file.csv_format.uses_tiers
    
    if request.method == 'POST':
        # Extract unique ticket types from CSV
        file_path = os.path.join('media', uploaded_file.metadata.get('file_path', ''))
        if not os.path.exists(file_path):
            messages.error(request, "CSV file not found.")
            return redirect('tickets:upload_csv')
        
        # Parse CSV to get ticket types
        df = pd.read_csv(file_path, dtype=str, keep_default_na=False)
        processor = CSVProcessor(uploaded_file, uploaded_file.csv_format)
        
        # Use dict to preserve order (same as GET request)
        ticket_type_counts = {}
        for _, row in df.iterrows():
            mapped_row = processor.map_columns(row.to_dict())
            ticket_type = mapped_row.get('ticket_type')
            quantity = int(mapped_row.get('quantity', 1) or 1)
            if ticket_type:
                ticket_type_counts[ticket_type] = ticket_type_counts.get(ticket_type, 0) + quantity
        
        # Use ordered list from dict keys (same order as GET request)
        ticket_types_list = list(ticket_type_counts.keys())
        
        # Create form with ticket types and uses_tiers flag
        form = TicketPriceEntryForm(ticket_types_list, uses_tiers=uses_tiers, data=request.POST)
        
        if uses_tiers:
            # For tier mode, we need to extract data directly from POST since fields are dynamic
            # Validate form first (it should accept dynamic tier fields we added in __init__)
            if not form.is_valid():
                # Log form errors for debugging
                for field, errors in form.errors.items():
                    for error in errors:
                        messages.error(request, f"Form validation error: {field} - {error}")
                # Still try to process tier data even if form has some validation issues
                # (some fields might be optional)
            
            tier_definitions = {}
            
            # First, build a mapping of ticket type indices to actual ticket types
            ticket_type_map = {}
            for key, value in request.POST.items():
                if key.startswith('ticket_type_'):
                    try:
                        idx = int(key.replace('ticket_type_', ''))
                        if idx < len(ticket_types_list):
                            ticket_type_map[str(idx)] = ticket_types_list[idx]
                    except (ValueError, TypeError):
                        continue
            
            # Parse tier data from POST directly
            # New format: tier_{ticket_type_index}_{tier_index}_{field}
            # e.g., tier_0_0_name, tier_0_1_price, etc.
            for key, value in request.POST.items():
                if key.startswith('tier_') and not key.startswith('tier_count_'):
                    # Parse: tier_{ticket_type_index}_{tier_index}_{field}
                    parts = key.split('_')
                    if len(parts) >= 4:  # tier, ticket_type_idx, tier_idx, field
                        try:
                            ticket_type_idx = parts[1]
                            tier_index = int(parts[2])
                            field = parts[3]
                            
                            # Get the actual ticket type from the map
                            ticket_type = ticket_type_map.get(ticket_type_idx)
                            
                            if not ticket_type:
                                # Fallback: try to match by index in ticket_types_list
                                try:
                                    idx = int(ticket_type_idx)
                                    if idx < len(ticket_types_list):
                                        ticket_type = ticket_types_list[idx]
                                except (ValueError, TypeError):
                                    continue
                            
                            if not ticket_type:
                                continue
                            
                            if ticket_type not in tier_definitions:
                                tier_definitions[ticket_type] = []
                            
                            # Ensure we have enough entries
                            while len(tier_definitions[ticket_type]) <= tier_index:
                                tier_definitions[ticket_type].append({
                                    'name': '',
                                    'price': None,
                                    'allotment': None,
                                    'order': None
                                })
                            
                            # Set the field value
                            if field == 'name':
                                tier_definitions[ticket_type][tier_index]['name'] = value
                            elif field == 'price':
                                try:
                                    tier_definitions[ticket_type][tier_index]['price'] = float(value)
                                except (ValueError, TypeError):
                                    pass
                            elif field == 'allotment':
                                try:
                                    tier_definitions[ticket_type][tier_index]['allotment'] = int(value)
                                except (ValueError, TypeError):
                                    pass
                            elif field == 'order':
                                try:
                                    tier_definitions[ticket_type][tier_index]['order'] = int(value)
                                except (ValueError, TypeError):
                                    pass
                        except (ValueError, TypeError, IndexError):
                            continue
            
            # Clean up and validate tier definitions
            cleaned_definitions = {}
            for ticket_type, tiers in tier_definitions.items():
                # Filter out incomplete tiers and sort by order
                valid_tiers = [
                    tier for tier in tiers
                    if tier['name'] and tier['price'] is not None and 
                       tier['allotment'] is not None and tier['order'] is not None
                ]
                if valid_tiers:
                    # Sort by order
                    valid_tiers.sort(key=lambda x: int(x['order']))
                    cleaned_definitions[ticket_type] = valid_tiers
            
            # Validate that we have tiers for all ticket types
            ticket_types_set = set(ticket_types_list)
            cleaned_types_set = set(cleaned_definitions.keys())
            
            if not cleaned_definitions or len(cleaned_definitions) < len(ticket_types_set):
                missing_types = ticket_types_set - cleaned_types_set
                found_types = cleaned_types_set
                
                # Debug info
                debug_msg = f"Found tiers for: {', '.join(found_types) if found_types else 'none'}. "
                debug_msg += f"Expected: {', '.join(ticket_types_set)}. "
                debug_msg += f"Missing: {', '.join(missing_types) if missing_types else 'none'}."
                
                messages.error(
                    request, 
                    f"Please define at least one tier for each ticket type. Missing tiers for: {', '.join(missing_types)}. {debug_msg}"
                )
                return redirect('tickets:price_entry', file_id=file_id)
            
            # Process CSV with tier definitions
            return process_csv_file(request, uploaded_file, tier_definitions=cleaned_definitions)
        
        elif form.is_valid():
            # Get prices dictionary
            manual_prices = form.get_prices_dict()
            
            # Process CSV with manual prices
            return process_csv_file(request, uploaded_file, manual_prices=manual_prices)
    else:
        # GET: Display form with ticket types
        file_path = os.path.join('media', uploaded_file.metadata.get('file_path', ''))
        if not os.path.exists(file_path):
            messages.error(request, "CSV file not found.")
            return redirect('tickets:upload_csv')
        
        # Parse CSV to extract unique ticket types and quantities
        df = pd.read_csv(file_path, dtype=str, keep_default_na=False)
        processor = CSVProcessor(uploaded_file, uploaded_file.csv_format)
        
        ticket_type_counts = {}
        for _, row in df.iterrows():
            mapped_row = processor.map_columns(row.to_dict())
            ticket_type = mapped_row.get('ticket_type')
            quantity = int(mapped_row.get('quantity', 1) or 1)
            if ticket_type:
                ticket_type_counts[ticket_type] = ticket_type_counts.get(ticket_type, 0) + quantity
        
        ticket_types_list = list(ticket_type_counts.keys())
        
        form = TicketPriceEntryForm(ticket_types_list, uses_tiers=uses_tiers)
        
        context = {
            'uploaded_file': uploaded_file,
            'form': form,
            'ticket_type_counts': ticket_type_counts,
            'uses_tiers': uses_tiers,
        }
        return render(request, 'tickets/price_entry.html', context)


@login_required
def process_csv_file(request, uploaded_file, manual_prices=None, tier_definitions=None):
    """Process CSV file and redirect to results."""
    try:
        file_path = os.path.join('media', uploaded_file.metadata.get('file_path', ''))
        if not os.path.exists(file_path):
            messages.error(request, "CSV file not found.")
            return redirect('tickets:upload_csv')
        
        # Initialize processor
        processor = CSVProcessor(uploaded_file, uploaded_file.csv_format)
        
        # Check if format uses tiers
        uses_tiers = uploaded_file.csv_format.uses_tiers
        
        # Validate CSV
        with open(file_path, 'rb') as f:
            is_valid, error_msg = processor.validate_csv(f)
            if not is_valid:
                uploaded_file.status = 'failed'
                uploaded_file.save(update_fields=['status'])
                messages.error(request, f"CSV validation failed: {error_msg}")
                return redirect('tickets:upload_results', file_id=uploaded_file.id)
        
        # Parse CSV
        with open(file_path, 'r', encoding='utf-8') as f:
            csv_data = processor.parse_csv(f)
        
        # Process and save based on format type
        if uses_tiers and tier_definitions:
            results = processor.process_and_save(csv_data, tier_definitions=tier_definitions)
        else:
            results = processor.process_and_save(csv_data, manual_prices=manual_prices)
        
        # Store results in session or metadata
        uploaded_file.metadata['processing_results'] = {
            'success_count': results['success_count'],
            'error_count': results['error_count'],
            'skipped_duplicates': results['skipped_duplicates'],
            'errors': results['errors'][:50],  # Limit to first 50 errors
            'skipped_order_numbers': results['skipped_order_numbers'][:50],  # Limit to first 50
            'rejected_orders': results.get('rejected_orders', [])[:50],  # Limit to first 50
        }
        uploaded_file.save(update_fields=['metadata'])
        
        if results['success_count'] > 0:
            messages.success(
                request,
                f"Successfully processed {results['success_count']} orders."
            )
        if results['error_count'] > 0:
            messages.warning(
                request,
                f"{results['error_count']} rows had errors. Check results for details."
            )
        if results['skipped_duplicates'] > 0:
            messages.info(
                request,
                f"{results['skipped_duplicates']} duplicate orders were skipped."
            )
        if results.get('rejected_orders'):
            messages.warning(
                request,
                f"{len(results['rejected_orders'])} orders were rejected due to tier capacity limits."
            )
        
        return redirect('tickets:upload_results', file_id=uploaded_file.id)
        
    except Exception as e:
        uploaded_file.status = 'failed'
        uploaded_file.save(update_fields=['status'])
        messages.error(request, f"Error processing CSV: {str(e)}")
        return redirect('tickets:upload_results', file_id=uploaded_file.id)


@login_required
def upload_results(request, file_id):
    """Display processing results."""
    uploaded_file = get_object_or_404(UploadedFile, id=file_id)

    results = uploaded_file.metadata.get('processing_results', {})

    context = {
        'uploaded_file': uploaded_file,
        'results': results,
    }
    return render(request, 'tickets/results.html', context)


@login_required
@require_http_methods(["POST"])
def upload_delete(request, file_id):
    """Delete an upload and all associated order data."""
    uploaded_file = get_object_or_404(UploadedFile, id=file_id)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    # TODO: Re-enable this check after temporary bypass
    # Block deletion if status is 'processing'
    # if uploaded_file.status == 'processing':
    #     error_msg = "Cannot delete upload while it is processing. Please wait for processing to complete."
    #     if is_ajax:
    #         return JsonResponse({'success': False, 'error': error_msg}, status=400)
    #     messages.error(request, error_msg)
    #     return redirect('tickets:home')

    try:
        with transaction.atomic():
            # Get all orders associated with this upload
            orders = uploaded_file.ticket_orders.all()
            orders_count = orders.count()

            # Collect affected customers before deletion
            affected_customer_ids = list(
                orders.values_list('customer_id', flat=True).distinct()
            )

            # Delete orders first (Tickets will cascade delete)
            orders.delete()

            # Delete the upload file (TicketTiers will cascade delete)
            filename = uploaded_file.filename
            uploaded_file.hard_delete()

            # Delete orphaned customers (no remaining orders) or recalculate LTV
            customers_deleted = 0
            for customer_id in affected_customer_ids:
                try:
                    customer = Customer.objects.get(id=customer_id)
                    if not customer.ticket_orders.exists():
                        customer.delete()
                        customers_deleted += 1
                    else:
                        customer.update_lifetime_value()
                except Customer.DoesNotExist:
                    pass

        success_msg = f"Successfully deleted '{filename}' and {orders_count} associated order(s)."
        if customers_deleted > 0:
            success_msg += f" Removed {customers_deleted} customer(s) with no remaining orders."
        if is_ajax:
            return JsonResponse({'success': True, 'message': success_msg})
        messages.success(request, success_msg)
        return redirect('tickets:home')

    except Exception as e:
        error_msg = f"Error deleting upload: {str(e)}"
        if is_ajax:
            return JsonResponse({'success': False, 'error': error_msg}, status=500)
        messages.error(request, error_msg)
        return redirect('tickets:home')


@login_required
def customer_list(request):
    """Display list of all customers with LTV."""
    customers = Customer.objects.all()
    
    # Search
    search_query = request.GET.get('search', '')
    if search_query:
        customers = customers.filter(
            name__icontains=search_query
        ) | customers.filter(
            email__icontains=search_query
        )
    
    # Sorting
    sort_by = request.GET.get('sort', '-lifetime_value')
    if sort_by in ['name', 'email', 'lifetime_value', 'last_order_date']:
        customers = customers.order_by(sort_by)
    elif sort_by == '-lifetime_value':
        customers = customers.order_by('-lifetime_value')
    
    # Pagination
    paginator = Paginator(customers, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'page_obj': page_obj,
        'search_query': search_query,
        'sort_by': sort_by,
    }
    return render(request, 'tickets/customer_list.html', context)


@login_required
def customer_detail(request, customer_id):
    """Display detailed customer information with LTV and order history."""
    customer = get_object_or_404(Customer, id=customer_id)
    
    # Order statistics
    orders = customer.ticket_orders.all()
    total_orders = orders.count()
    total_tickets = Ticket.objects.filter(ticket_order__customer=customer).count()
    avg_order_value = orders.aggregate(avg=Avg('total_amount'))['avg'] or Decimal('0.00')
    
    first_order = orders.order_by('order_date').first()
    last_order = orders.order_by('-order_date').first()
    
    # Event attendance
    events_attended = Event.objects.filter(
        ticket_orders__customer=customer
    ).distinct()
    
    # Paginate orders
    paginator = Paginator(orders, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'customer': customer,
        'total_orders': total_orders,
        'total_tickets': total_tickets,
        'avg_order_value': avg_order_value,
        'first_order': first_order,
        'last_order': last_order,
        'events_attended': events_attended,
        'page_obj': page_obj,
    }
    return render(request, 'tickets/customer_detail.html', context)


# Event Management Views

@login_required
def event_list(request):
    """Display list of all events with associated uploads."""
    # Use Subquery to calculate revenue correctly (avoids double-counting when orders have multiple tickets)
    events = Event.objects.annotate(
        upload_count=Count('ticket_orders__uploaded_file', distinct=True),
        total_orders=Count('ticket_orders', distinct=True),
        total_revenue=Coalesce(
            Subquery(
                TicketOrder.objects.filter(
                    event=OuterRef('pk')
                ).values('event').annotate(
                    total=Sum('total_amount')
                ).values('total')[:1],
                output_field=models.DecimalField(max_digits=10, decimal_places=2)
            ),
            Decimal('0.00')
        ),
        total_tickets=Count('ticket_orders__tickets', distinct=True),
    ).select_related('venue')
    
    # Search functionality
    search_query = request.GET.get('search', '')
    if search_query:
        events = events.filter(
            Q(name__icontains=search_query) |
            Q(venue__name__icontains=search_query) |
            Q(venue__city__icontains=search_query)
        )
    
    # Sorting
    sort_by = request.GET.get('sort', '-start_date')
    if sort_by in ['name', 'start_date', 'upload_count', 'total_revenue']:
        events = events.order_by(sort_by)
    elif sort_by == '-start_date':
        events = events.order_by('-start_date')
    elif sort_by == '-upload_count':
        events = events.order_by('-upload_count')
    elif sort_by == '-total_revenue':
        events = events.order_by('-total_revenue')
    else:
        events = events.order_by('-start_date')
    
    # Pagination
    paginator = Paginator(events, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'page_obj': page_obj,
        'search_query': search_query,
        'sort_by': sort_by,
    }
    return render(request, 'tickets/event_list.html', context)


@login_required
def event_detail(request, event_id):
    """Display detailed event information with associated uploads."""
    event = get_object_or_404(
        Event.objects.select_related('venue'),
        id=event_id
    )
    
    # Get all distinct uploads associated with this event
    associated_uploads = event.get_associated_uploads().select_related('csv_format')
    
    # Calculate statistics per upload
    upload_stats = []
    for upload in associated_uploads:
        orders = TicketOrder.objects.filter(event=event, uploaded_file=upload)
        orders_count = orders.count()
        revenue = orders.aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')
        tickets_count = Ticket.objects.filter(ticket_order__event=event, ticket_order__uploaded_file=upload).count()
        
        upload_stats.append({
            'upload': upload,
            'orders_count': orders_count,
            'revenue': revenue,
            'tickets_count': tickets_count,
        })
    
    # Event statistics
    orders = event.ticket_orders.all()
    total_orders = orders.count()
    total_revenue = orders.aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')
    total_tickets = Ticket.objects.filter(ticket_order__event=event).count()
    total_customers = Customer.objects.filter(ticket_orders__event=event).distinct().count()
    
    # Paginate orders
    paginator = Paginator(orders.order_by('-order_date'), 100)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'event': event,
        'upload_stats': upload_stats,
        'total_orders': total_orders,
        'total_revenue': total_revenue,
        'total_tickets': total_tickets,
        'total_customers': total_customers,
        'page_obj': page_obj,
    }
    return render(request, 'tickets/event_detail.html', context)


@login_required
def order_detail(request, order_id):
    """Display detailed order information with all tickets."""
    order = get_object_or_404(
        TicketOrder.objects.select_related('customer', 'event', 'event__venue', 'uploaded_file'),
        id=order_id
    )
    
    # Get all tickets for this order with tier information
    tickets = order.tickets.select_related('tier').all()
    
    # Calculate ticket statistics
    total_tickets = tickets.count()
    ticket_types = {}
    for ticket in tickets:
        ticket_type = ticket.ticket_type
        if ticket_type not in ticket_types:
            ticket_types[ticket_type] = {
                'count': 0,
                'total_price': Decimal('0.00'),
                'tier_name': ticket.tier_name,
                'unit_price': ticket.price
            }
        ticket_types[ticket_type]['count'] += 1
        ticket_types[ticket_type]['total_price'] += ticket.price
    
    context = {
        'order': order,
        'tickets': tickets,
        'total_tickets': total_tickets,
        'ticket_types': ticket_types,
    }
    return render(request, 'tickets/order_detail.html', context)


# Format Management Views

@login_required
def format_list(request):
    """List all CSV formats."""
    formats = CSVFormat.objects.all()
    context = {
        'formats': formats,
    }
    return render(request, 'tickets/format_list.html', context)


@login_required
def format_create(request):
    """Create new CSV format."""
    if request.method == 'POST':
        form = CSVFormatForm(request.POST)
        if form.is_valid():
            format_obj = form.save()
            messages.success(request, f"CSV format '{format_obj.name}' created successfully.")
            return redirect('tickets:format_list')
    else:
        form = CSVFormatForm()
    
    context = {
        'form': form,
        'action': 'Create',
    }
    return render(request, 'tickets/format_form.html', context)


@login_required
def format_edit(request, format_id):
    """Edit existing CSV format."""
    format_obj = get_object_or_404(CSVFormat, id=format_id)
    
    if request.method == 'POST':
        form = CSVFormatForm(request.POST, instance=format_obj)
        if form.is_valid():
            format_obj = form.save()
            messages.success(request, f"CSV format '{format_obj.name}' updated successfully.")
            return redirect('tickets:format_list')
    else:
        # Form will handle JSON formatting in __init__ method
        form = CSVFormatForm(instance=format_obj)
    
    context = {
        'form': form,
        'format': format_obj,
        'action': 'Edit',
    }
    return render(request, 'tickets/format_form.html', context)


@login_required
def format_delete(request, format_id):
    """Delete CSV format."""
    format_obj = get_object_or_404(CSVFormat, id=format_id)
    
    if request.method == 'POST':
        # Check if format is in use
        if format_obj.uploaded_files.exists():
            messages.error(
                request,
                f"Cannot delete format '{format_obj.name}' because it is in use by uploaded files."
            )
            return redirect('tickets:format_list')
        
        format_name = format_obj.name
        format_obj.delete()
        messages.success(request, f"CSV format '{format_name}' deleted successfully.")
        return redirect('tickets:format_list')
    
    context = {
        'format': format_obj,
    }
    return render(request, 'tickets/format_delete.html', context)


@login_required
def format_set_default(request, format_id):
    """Set CSV format as default."""
    format_obj = get_object_or_404(CSVFormat, id=format_id)
    
    # Unset other defaults
    CSVFormat.objects.filter(is_default=True).exclude(id=format_id).update(is_default=False)
    
    # Set this as default
    format_obj.is_default = True
    format_obj.save(update_fields=['is_default'])
    
    messages.success(request, f"'{format_obj.name}' is now the default CSV format.")
    return redirect('tickets:format_list')


# Venue Management Views

@login_required
def venue_create(request):
    """Create new venue."""
    if request.method == 'POST':
        form = VenueForm(request.POST)
        if form.is_valid():
            venue = form.save()
            messages.success(request, f"Venue '{venue.name}, {venue.city}' created successfully.")
            # Stay on the page to allow creating multiple venues
            form = VenueForm()
    else:
        form = VenueForm()
    
    context = {
        'form': form,
    }
    return render(request, 'tickets/venue_create.html', context)


@login_required
def event_create(request):
    """Create new event."""
    if request.method == 'POST':
        form = EventForm(request.POST)
        if form.is_valid():
            event = form.save(commit=False)
            event.created_by = request.user
            event.save()
            messages.success(request, f"Event '{event.name}' created successfully.")
            return redirect('tickets:event_detail', event_id=event.id)
    else:
        form = EventForm()

    context = {
        'form': form,
    }
    return render(request, 'tickets/event_create.html', context)


# Forecast Tool Views

@login_required
def forecast_tool(request):
    """Display the standalone forecast tool page."""
    venues = Venue.objects.all().order_by('city', 'name')
    context = {
        'venues': venues,
    }
    return render(request, 'tickets/forecast_tool.html', context)


@login_required
def forecast_api(request):
    """Return forecast data as JSON for the chart."""
    from datetime import datetime

    venue_id = request.GET.get('venue_id', '').strip()
    event_date_str = request.GET.get('event_date', '').strip()
    capacity_str = request.GET.get('capacity', '').strip()
    starting_tickets_str = request.GET.get('starting_tickets', '').strip()

    # Validate inputs
    errors = []
    if not event_date_str:
        errors.append('Event date is required')
    if not capacity_str:
        errors.append('Capacity is required')

    try:
        capacity = int(capacity_str) if capacity_str else 0
        if capacity <= 0:
            errors.append('Capacity must be a positive number')
    except ValueError:
        errors.append('Capacity must be a valid number')
        capacity = 0

    try:
        event_date = datetime.strptime(event_date_str, '%Y-%m-%d').date() if event_date_str else None
    except ValueError:
        errors.append('Invalid date format')
        event_date = None

    starting_tickets = None
    if starting_tickets_str:
        try:
            starting_tickets = int(starting_tickets_str)
            if starting_tickets < 0:
                errors.append('Tickets sold to date must be non-negative')
        except ValueError:
            errors.append('Tickets sold to date must be a valid number')

    if errors:
        return JsonResponse({'error': '; '.join(errors)}, status=400)

    # Generate forecast preview
    result = generate_forecast_preview(
        venue_id=venue_id if venue_id else None,
        event_date=event_date,
        capacity=capacity,
        starting_tickets=starting_tickets,
    )

    response = JsonResponse(result)
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    response['Pragma'] = 'no-cache'
    return response
