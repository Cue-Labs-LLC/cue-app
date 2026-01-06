import os
import json
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db.models import Sum, Count, Avg
from django.core.paginator import Paginator
from django.http import JsonResponse, Http404
from django.views.decorators.http import require_http_methods
import pandas as pd

from .models import (
    CSVFormat, UploadedFile, Customer, Event, TicketOrder, Ticket, Venue
)
from .forms import CSVUploadForm, TicketPriceEntryForm, CSVFormatForm, VenueForm
from .services import CSVProcessor


def home(request):
    """Home/dashboard page with overview statistics."""
    # Recent uploads
    recent_uploads = UploadedFile.objects.all()[:10]
    
    # Summary statistics
    total_customers = Customer.objects.count()
    total_orders = TicketOrder.objects.count()
    total_revenue = TicketOrder.objects.aggregate(
        total=Sum('total_amount')
    )['total'] or Decimal('0.00')
    total_tickets = Ticket.objects.count()
    
    context = {
        'recent_uploads': recent_uploads,
        'total_customers': total_customers,
        'total_orders': total_orders,
        'total_revenue': total_revenue,
        'total_tickets': total_tickets,
    }
    return render(request, 'tickets/home.html', context)


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
                    'event_date': form.cleaned_data.get('event_date').isoformat() if form.cleaned_data.get('event_date') else '',
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
        
        ticket_types = set()
        for _, row in df.iterrows():
            mapped_row = processor.map_columns(row.to_dict())
            ticket_type = mapped_row.get('ticket_type')
            if ticket_type:
                ticket_types.add(ticket_type)
        
        # Create form with ticket types and uses_tiers flag
        form = TicketPriceEntryForm(list(ticket_types), uses_tiers=uses_tiers, data=request.POST)
        
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
            ticket_types_list = list(ticket_types)
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
            ticket_types_set = set(ticket_types)
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
        
        form = TicketPriceEntryForm(list(ticket_type_counts.keys()), uses_tiers=uses_tiers)
        
        context = {
            'uploaded_file': uploaded_file,
            'form': form,
            'ticket_type_counts': ticket_type_counts,
            'uses_tiers': uses_tiers,
        }
        return render(request, 'tickets/price_entry.html', context)


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


def upload_results(request, file_id):
    """Display processing results."""
    uploaded_file = get_object_or_404(UploadedFile, id=file_id)
    
    results = uploaded_file.metadata.get('processing_results', {})
    
    context = {
        'uploaded_file': uploaded_file,
        'results': results,
    }
    return render(request, 'tickets/results.html', context)


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


# Format Management Views

def format_list(request):
    """List all CSV formats."""
    formats = CSVFormat.objects.all()
    context = {
        'formats': formats,
    }
    return render(request, 'tickets/format_list.html', context)


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
        # Convert column_mapping to JSON string for textarea
        initial_data = format_obj.__dict__.copy()
        if isinstance(initial_data.get('column_mapping'), dict):
            initial_data['column_mapping'] = json.dumps(initial_data['column_mapping'], indent=2)
        form = CSVFormatForm(instance=format_obj, initial=initial_data)
    
    context = {
        'form': form,
        'format': format_obj,
        'action': 'Edit',
    }
    return render(request, 'tickets/format_form.html', context)


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
