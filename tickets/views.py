import calendar
import os
import json
from datetime import date, timedelta
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.urls import reverse_lazy
from django.db.models import (
    Sum, Count, Avg, Q, Subquery, OuterRef, Prefetch,
    Case, When, Value, F, CharField,
)
from django.db.models.functions import Coalesce
from django.db import models
from django.core.paginator import Paginator
from django.http import JsonResponse, Http404, HttpResponse
from django.views.decorators.http import require_http_methods
from django.db import connection, transaction
import pandas as pd

from .models import (
    Organization, UserProfile,
    CSVFormat, UploadedFile, Customer, Event, EventTalent, TicketOrder, Ticket, Venue,
    CustomField, EventCustomFieldValue,
)
from .forms import (
    EventCSVUploadForm, TicketPriceEntryForm, CSVFormatForm,
    VenueForm, EventForm, EventTalentFormSet, LoginForm,
)
from .csv_processor import CSVProcessor
from .services.forecasting.preview import generate_forecast_preview
from .services.segmentation.rfm_calculator import RFMCalculator
from .services.segmentation.segment_definitions import (
    SEGMENT_BADGE_COLORS,
    SEGMENT_DESCRIPTIONS,
    SEGMENT_RULES,
)
from .services.cohort_analysis.repeat_customer_calculator import RepeatCustomerCalculator
from .services.cohort_analysis.cohort_retention_calculator import CohortRetentionCalculator
from .utils import get_organization, require_org

import logging
logger = logging.getLogger(__name__)


def _regenerate_event_doc_background(org):
    """Regenerate the Upcoming Events Google Doc. Fails silently if not configured."""
    from django.conf import settings
    if not settings.GOOGLE_DOC_ID or not settings.GOOGLE_SERVICE_ACCOUNT_JSON:
        return
    try:
        from .services.google_docs import EventDocFormatter, GoogleDocWriter
        formatter = EventDocFormatter(org)
        content = formatter.generate_full_document()
        writer = GoogleDocWriter(settings.GOOGLE_DOC_ID)
        writer.update_document(content)
    except Exception:
        logger.exception("Failed to regenerate event doc")


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
def org_required(request):
    """Shown when user has no organization; prompt to create or join one."""
    return render(request, 'tickets/org_required.html')


@login_required
@require_http_methods(["GET", "POST"])
def create_organization(request):
    """Create a new organization and assign the current user to it."""
    from .forms import OrganizationForm
    profile, _ = UserProfile.objects.get_or_create(
        user=request.user,
        defaults={'organization_id': None},
    )
    if profile.organization_id and not request.user.is_superuser:
        # Already has an org, redirect home
        return redirect('tickets:home')
    if request.method == 'POST':
        form = OrganizationForm(request.POST)
        if form.is_valid():
            org = form.save()
            profile.organization = org
            profile.save()
            messages.success(request, f"Organization '{org.name}' created. You can now use the app.")
            return redirect('tickets:home')
    else:
        form = OrganizationForm()
    return render(request, 'tickets/create_organization.html', {'form': form})


@login_required
@require_org
def home(request):
    """Home/dashboard page with overview statistics."""
    org = get_organization(request)
    # Recent events (org-scoped)
    recent_events = Event.objects.filter(organization=org).annotate(
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

    # Summary statistics (org-scoped via Event/Customer/UploadedFile)
    total_customers = Customer.objects.filter(organization=org).count()
    total_orders = TicketOrder.objects.filter(event__organization=org).count()
    total_revenue = TicketOrder.objects.filter(event__organization=org).aggregate(
        total=Sum('total_amount')
    )['total'] or Decimal('0.00')
    total_tickets = Ticket.objects.filter(ticket_order__event__organization=org).count()
    
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
@require_org
@require_http_methods(["GET", "POST"])
def price_entry(request, file_id):
    """Display form for manually entering ticket prices or tiers."""
    org = get_organization(request)
    uploaded_file = get_object_or_404(UploadedFile.objects.filter(organization=org), id=file_id)
    uses_tiers = uploaded_file.csv_format.uses_tiers
    
    if request.method == 'POST':
        # Extract unique ticket types from CSV
        file_path = os.path.join('media', uploaded_file.metadata.get('file_path', ''))
        if not os.path.exists(file_path):
            messages.error(request, "CSV file not found.")
            return redirect('tickets:event_list')
        
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
            return redirect('tickets:event_list')
        
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
            return redirect('tickets:event_list')
        
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
            'skipped_rows_count': results.get('skipped_rows_count', 0),
            'skipped_rows_by_reason': results.get('skipped_rows_by_reason', {}),
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
        if results.get('skipped_rows_count', 0) > 0:
            messages.info(
                request,
                f"{results['skipped_rows_count']} row(s) were skipped (section headers, blank rows, etc.). See results for details."
            )

        if results['success_count'] > 0:
            try:
                RFMCalculator(uploaded_file.organization).calculate_all()
            except Exception:
                logger.exception("RFM recalc after CSV import failed")

        return redirect('tickets:upload_results', file_id=uploaded_file.id)

    except Exception as e:
        uploaded_file.status = 'failed'
        uploaded_file.save(update_fields=['status'])
        messages.error(request, f"Error processing CSV: {str(e)}")
        return redirect('tickets:upload_results', file_id=uploaded_file.id)


@login_required
@require_org
def upload_results(request, file_id):
    """Display processing results."""
    org = get_organization(request)
    uploaded_file = get_object_or_404(UploadedFile.objects.filter(organization=org), id=file_id)

    results = uploaded_file.metadata.get('processing_results', {})

    context = {
        'uploaded_file': uploaded_file,
        'results': results,
    }
    return render(request, 'tickets/results.html', context)


@login_required
@require_org
@require_http_methods(["POST"])
def upload_delete(request, file_id):
    """Delete an upload and all associated order data."""
    org = get_organization(request)
    uploaded_file = get_object_or_404(UploadedFile.objects.filter(organization=org), id=file_id)
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
                    customer = Customer.objects.filter(organization=org).get(id=customer_id)
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
@require_org
def customer_list(request):
    """Display list of all customers with LTV and optional segment filter."""
    org = get_organization(request)
    customers = Customer.objects.filter(organization=org)

    # Segment filter
    segment_filter = request.GET.get('segment', '').strip()
    if segment_filter:
        customers = customers.filter(rfm_segment=segment_filter)

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

    segment_choices = list(SEGMENT_BADGE_COLORS.keys())
    current_segment_definition = None
    if segment_filter:
        desc = SEGMENT_DESCRIPTIONS.get(segment_filter, '')
        if desc:
            current_segment_definition = {
                'segment': segment_filter,
                'description': desc,
                'badge_color': SEGMENT_BADGE_COLORS.get(segment_filter, 'secondary'),
            }
    context = {
        'page_obj': page_obj,
        'search_query': search_query,
        'sort_by': sort_by,
        'segment_filter': segment_filter,
        'segment_choices': segment_choices,
        'segment_badge_colors': SEGMENT_BADGE_COLORS,
        'current_segment_definition': current_segment_definition,
    }
    return render(request, 'tickets/customer_list.html', context)


@login_required
@require_org
def customer_ltv_by_market(request):
    """Display customer LTV metrics aggregated by city (event venue city)."""
    org = get_organization(request)
    qs = (
        TicketOrder.objects.filter(event__organization=org)
        .values('event__venue__city')
        .annotate(
            total_ltv=Sum('total_amount'),
            order_count=Count('id'),
            customer_count=Count('customer', distinct=True),
        )
    )
    sort_by = request.GET.get('sort', '-total_ltv')
    if sort_by == 'city':
        qs = qs.order_by('event__venue__city')
    elif sort_by == '-city':
        qs = qs.order_by('-event__venue__city')
    elif sort_by == 'total_ltv':
        qs = qs.order_by('total_ltv')
    elif sort_by == '-total_ltv':
        qs = qs.order_by('-total_ltv')
    elif sort_by == 'customer_count':
        qs = qs.order_by('customer_count')
    elif sort_by == '-customer_count':
        qs = qs.order_by('-customer_count')
    elif sort_by == 'order_count':
        qs = qs.order_by('order_count')
    elif sort_by == '-order_count':
        qs = qs.order_by('-order_count')
    else:
        qs = qs.order_by('-total_ltv')

    market_stats = []
    for row in qs:
        city = row['event__venue__city'] or ''
        customer_count = row['customer_count'] or 0
        total_ltv = row['total_ltv'] or Decimal('0.00')
        avg_ltv = (total_ltv / customer_count) if customer_count else Decimal('0.00')
        market_stats.append({
            'city': city.strip() or '—',
            'total_ltv': total_ltv,
            'order_count': row['order_count'] or 0,
            'customer_count': customer_count,
            'avg_ltv': avg_ltv,
        })

    chart_data = [
        {
            'city': row['city'],
            'total_ltv': float(row['total_ltv']),
            'avg_ltv': float(row['avg_ltv']),
        }
        for row in market_stats
    ]
    market_stats_json = json.dumps(chart_data)

    context = {
        'market_stats': market_stats,
        'market_stats_json': market_stats_json,
        'sort_by': sort_by,
    }
    return render(request, 'tickets/ltv_by_market.html', context)


def _format_range(min_max):
    """Format (lo, hi) as 'lo-hi'; None as 'any'."""
    if min_max is None:
        return "any"
    lo, hi = min_max
    return f"{lo}-{hi}"


@login_required
@require_org
def customer_segments(request):
    """Analytics page: segment distribution (donut), avg LTV per segment (bar), table with links."""
    org = get_organization(request)
    segment_order = list(SEGMENT_BADGE_COLORS.keys())

    # Build segment definitions for "What the segments mean" card (simple language + optional R/F/M).
    segment_definitions = []
    for name, r_range, f_range, m_range in SEGMENT_RULES:
        segment_definitions.append({
            "segment": name,
            "description": SEGMENT_DESCRIPTIONS.get(name, ""),
            "badge_color": SEGMENT_BADGE_COLORS.get(name, "secondary"),
            "r_range": _format_range(r_range),
            "f_range": _format_range(f_range),
            "m_range": _format_range(m_range),
        })

    # Query 1: count + total_ltv per segment (normalize null/blank to Dormant)
    seg_data = (
        Customer.objects.filter(organization=org)
        .annotate(
            seg=Case(
                When(rfm_segment='', then=Value('Dormant')),
                When(rfm_segment__isnull=True, then=Value('Dormant')),
                default=F('rfm_segment'),
                output_field=CharField(),
            )
        )
        .values('seg')
        .annotate(count=Count('id'), total_ltv=Sum('lifetime_value'))
    )
    seg_map = {r['seg'] or 'Dormant': r for r in seg_data}

    # Query 2: total orders per segment
    order_data = (
        TicketOrder.objects.filter(customer__organization=org)
        .annotate(
            seg=Case(
                When(customer__rfm_segment='', then=Value('Dormant')),
                When(customer__rfm_segment__isnull=True, then=Value('Dormant')),
                default=F('customer__rfm_segment'),
                output_field=CharField(),
            )
        )
        .values('seg')
        .annotate(total_orders=Count('id'))
    )
    order_map = {r['seg'] or 'Dormant': r['total_orders'] for r in order_data}

    total_customers = sum(r['count'] for r in seg_map.values())
    segment_stats = []
    for seg in segment_order:
        d = seg_map.get(seg, {'count': 0, 'total_ltv': Decimal('0')})
        count = d['count']
        total_ltv = d.get('total_ltv') or Decimal('0')
        total_orders = order_map.get(seg, 0)
        pct = (100.0 * count / total_customers) if total_customers else 0
        avg_ltv = (total_ltv / count) if count else Decimal('0')
        avg_orders = (total_orders / count) if count else 0
        segment_stats.append({
            'segment': seg,
            'count': count,
            'pct': round(pct, 1),
            'avg_ltv': avg_ltv,
            'avg_orders': round(avg_orders, 1),
            'badge_color': SEGMENT_BADGE_COLORS.get(seg, 'secondary'),
        })
    segment_stats_json = json.dumps([
        {'segment': s['segment'], 'count': s['count'], 'avg_ltv': float(s['avg_ltv'])}
        for s in segment_stats
    ])
    context = {
        'segment_stats': segment_stats,
        'segment_stats_json': segment_stats_json,
        'segment_definitions': segment_definitions,
        'total_customers': total_customers,
        'rfm_recalc_in_progress': org.rfm_recalc_in_progress,
    }
    return render(request, 'tickets/customer_segments.html', context)


@login_required
@require_org
@require_http_methods(["POST"])
def recalculate_segments(request):
    """Enqueue Celery task to recalculate RFM segments; redirect with message."""
    from .tasks import recalculate_rfm_task

    org = get_organization(request)
    if org.rfm_recalc_in_progress:
        messages.info(request, 'Recalculation already in progress.')
    else:
        recalculate_rfm_task.delay(str(org.id))
        messages.success(request, 'Segment recalculation started. Results will appear shortly.')
    return redirect('tickets:customer_segments')


@login_required
@require_org
def repeat_customers(request):
    """Analytics page: new vs returning customers per event."""
    org = get_organization(request)
    calculator = RepeatCustomerCalculator(org)
    result = calculator.calculate()
    chart_data = json.dumps(result['events'], default=str)
    return render(request, 'tickets/repeat_customers.html', {
        'events': result['events'],
        'summary': result['summary'],
        'chart_data_json': chart_data,
    })


@login_required
@require_org
def cohort_retention(request):
    """Analytics page: monthly cohort retention heatmap and line chart."""
    org = get_organization(request)
    calculator = CohortRetentionCalculator(org)
    result = calculator.calculate()
    chart_data = json.dumps(result['cohorts'], default=str)
    max_periods = max(len(c['periods']) for c in result['cohorts']) if result['cohorts'] else 0
    return render(request, 'tickets/cohort_retention.html', {
        'cohorts': result['cohorts'],
        'summary': result['summary'],
        'max_periods': range(max_periods),
        'chart_data_json': chart_data,
    })


@login_required
@require_org
def customer_detail(request, customer_id):
    """Display detailed customer information with LTV and order history."""
    org = get_organization(request)
    customer = get_object_or_404(Customer.objects.filter(organization=org), id=customer_id)
    
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
    
    segment_badge_color = SEGMENT_BADGE_COLORS.get(
        (customer.rfm_segment or '').strip(), 'secondary'
    )
    context = {
        'customer': customer,
        'total_orders': total_orders,
        'total_tickets': total_tickets,
        'avg_order_value': avg_order_value,
        'first_order': first_order,
        'last_order': last_order,
        'events_attended': events_attended,
        'page_obj': page_obj,
        'segment_badge_color': segment_badge_color,
    }
    return render(request, 'tickets/customer_detail.html', context)


# Event Management Views

@login_required
@require_org
def event_list(request):
    """Display list of all events with associated uploads."""
    org = get_organization(request)
    # Use Subquery to calculate revenue correctly (avoids double-counting when orders have multiple tickets)
    events = Event.objects.filter(organization=org).annotate(
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
@require_org
def event_calendar(request):
    """Display events in a month calendar grid."""
    org = get_organization(request)
    today = date.today()

    try:
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))
    except (TypeError, ValueError):
        year, month = today.year, today.month
    if not (1 <= month <= 12) or not (2000 <= year <= 2100):
        year, month = today.year, today.month

    first_day = date(year, month, 1)
    _, last_day_num = calendar.monthrange(year, month)
    last_day = date(year, month, last_day_num)

    events = (
        Event.objects.filter(
            organization=org,
            deleted_at__isnull=True,
            start_date__gte=first_day,
            start_date__lte=last_day,
        )
        .select_related('venue')
        .order_by('start_date', 'start_time', 'name')
    )

    events_by_date = {}
    for event in events:
        events_by_date.setdefault(event.start_date, []).append(event)

    cal = calendar.Calendar(firstweekday=6)  # Sunday = 6
    raw_weeks = cal.monthdatescalendar(year, month)
    weeks = []
    for week in raw_weeks:
        week_cells = []
        for d in week:
            week_cells.append({
                'date': d,
                'in_month': d.month == month,
                'events': events_by_date.get(d, []),
            })
        weeks.append(week_cells)
    month_name = calendar.month_name[month]

    if month == 1:
        prev_month, prev_year = 12, year - 1
    else:
        prev_month, prev_year = month - 1, year
    if month == 12:
        next_month, next_year = 1, year + 1
    else:
        next_month, next_year = month + 1, year

    context = {
        'month_name': month_name,
        'year': year,
        'month': month,
        'weeks': weeks,
        'events_by_date': events_by_date,
        'prev_month': prev_month,
        'prev_year': prev_year,
        'next_month': next_month,
        'next_year': next_year,
    }
    return render(request, 'tickets/event_calendar.html', context)


@login_required
@require_org
def event_detail(request, event_id):
    """Display detailed event information with associated uploads."""
    org = get_organization(request)
    event = get_object_or_404(
        Event.objects.filter(organization=org).select_related('venue').prefetch_related(
            Prefetch(
                'custom_field_values',
                EventCustomFieldValue.objects.select_related('custom_field', 'custom_field_option'),
            ),
        ),
        id=event_id,
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

    # Custom field values: only show those for the current org's custom fields
    custom_field_values_display = [
        v for v in event.custom_field_values.all()
        if v.custom_field.organization_id == org.id
    ]

    context = {
        'event': event,
        'upload_stats': upload_stats,
        'total_orders': total_orders,
        'total_revenue': total_revenue,
        'total_tickets': total_tickets,
        'total_customers': total_customers,
        'page_obj': page_obj,
        'custom_field_values_display': custom_field_values_display,
    }
    return render(request, 'tickets/event_detail.html', context)


@login_required
@require_org
@require_http_methods(["GET", "POST"])
def event_delete(request, event_id):
    """Permanently delete an event and all its orders and tickets."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    if request.method == 'POST':
        try:
            was_future = event.start_date >= date.today()
            with transaction.atomic():
                orders_count = event.ticket_orders.count()
                affected_customer_ids = list(
                    event.ticket_orders.values_list('customer_id', flat=True).distinct()
                )
                event_name = event.name
                event.hard_delete()

                customers_deleted = 0
                for customer_id in affected_customer_ids:
                    try:
                        customer = Customer.objects.filter(organization=org).get(id=customer_id)
                        if not customer.ticket_orders.exists():
                            customer.delete()
                            customers_deleted += 1
                        else:
                            customer.update_lifetime_value()
                    except Customer.DoesNotExist:
                        pass

            success_msg = f"Event '{event_name}' and {orders_count} associated order(s) have been permanently deleted."
            if customers_deleted > 0:
                success_msg += f" Removed {customers_deleted} customer(s) with no remaining orders."
            if was_future:
                _regenerate_event_doc_background(org)
            messages.success(request, success_msg)
            return redirect('tickets:event_list')
        except Exception as e:
            messages.error(request, f"Error deleting event: {str(e)}")
            return redirect('tickets:event_detail', event_id=event_id)

    context = {'event': event}
    return render(request, 'tickets/event_delete.html', context)


@login_required
@require_org
def order_detail(request, order_id):
    """Display detailed order information with all tickets."""
    org = get_organization(request)
    order = get_object_or_404(
        TicketOrder.objects.filter(event__organization=org).select_related(
            'customer', 'event', 'event__venue', 'uploaded_file'
        ),
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
@require_org
def format_list(request):
    """List all CSV formats."""
    org = get_organization(request)
    formats = CSVFormat.objects.filter(organization=org)
    context = {
        'formats': formats,
    }
    return render(request, 'tickets/format_list.html', context)


@login_required
@require_org
def format_create(request):
    """Create new CSV format."""
    org = get_organization(request)
    if request.method == 'POST':
        form = CSVFormatForm(request.POST)
        if form.is_valid():
            format_obj = form.save(commit=False)
            format_obj.organization = org
            format_obj.save()
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
@require_org
def format_edit(request, format_id):
    """Edit existing CSV format."""
    org = get_organization(request)
    format_obj = get_object_or_404(CSVFormat.objects.filter(organization=org), id=format_id)
    
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
@require_org
def format_delete(request, format_id):
    """Delete CSV format."""
    org = get_organization(request)
    format_obj = get_object_or_404(CSVFormat.objects.filter(organization=org), id=format_id)
    
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
@require_org
def format_set_default(request, format_id):
    """Set CSV format as default."""
    org = get_organization(request)
    format_obj = get_object_or_404(CSVFormat.objects.filter(organization=org), id=format_id)
    
    # Unset other defaults in this org
    CSVFormat.objects.filter(organization=org, is_default=True).exclude(id=format_id).update(is_default=False)
    
    # Set this as default
    format_obj.is_default = True
    format_obj.save(update_fields=['is_default'])
    
    messages.success(request, f"'{format_obj.name}' is now the default CSV format.")
    return redirect('tickets:format_list')


# Venue Management Views

@login_required
@require_org
def venue_list(request):
    """List all venues with optional search and pagination."""
    org = get_organization(request)
    venues = Venue.objects.filter(organization=org).annotate(event_count=Count('events')).order_by('name', 'city')
    search_query = request.GET.get('search', '')
    if search_query:
        venues = venues.filter(
            Q(name__icontains=search_query) |
            Q(city__icontains=search_query) |
            Q(state__icontains=search_query) |
            Q(country__icontains=search_query)
        )
    paginator = Paginator(venues, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    context = {
        'page_obj': page_obj,
        'search_query': search_query,
    }
    return render(request, 'tickets/venue_list.html', context)


@login_required
@require_org
def venue_create(request):
    """Create new venue."""
    org = get_organization(request)
    if request.method == 'POST':
        form = VenueForm(request.POST)
        if form.is_valid():
            venue = form.save(commit=False)
            venue.organization = org
            venue.save()
            messages.success(request, f"Venue '{venue.name}, {venue.city}' created successfully.")
            return redirect('tickets:venue_list')
    else:
        form = VenueForm()
    context = {'form': form}
    return render(request, 'tickets/venue_create.html', context)


@login_required
@require_org
def venue_edit(request, venue_id):
    """Edit an existing venue."""
    org = get_organization(request)
    venue = get_object_or_404(Venue.objects.filter(organization=org), id=venue_id)
    if request.method == 'POST':
        form = VenueForm(request.POST, instance=venue)
        if form.is_valid():
            form.save()
            messages.success(request, f"Venue '{venue.name}, {venue.city}' updated successfully.")
            return redirect('tickets:venue_list')
    else:
        form = VenueForm(instance=venue)
    context = {'form': form, 'venue': venue}
    return render(request, 'tickets/venue_edit.html', context)


@login_required
@require_org
def event_create(request):
    """Create new event."""
    org = get_organization(request)
    if request.method == 'POST':
        form = EventForm(request.POST, organization=org)
        talent_formset = EventTalentFormSet(request.POST, prefix='talent')
        if form.is_valid() and talent_formset.is_valid():
            event = form.save(commit=False)
            event.organization = org
            event.created_by = request.user
            event.save()
            instances = talent_formset.save(commit=False)
            for obj in instances:
                if obj.name and obj.name.strip():
                    obj.event = event
                    obj.save()
            for obj in talent_formset.deleted_objects:
                obj.delete()
            # Save custom field values for current org's dropdown custom fields only
            for cf in CustomField.objects.filter(field_type='dropdown', organization=org):
                field_name = f'custom_field_{cf.id}'
                option_id = form.cleaned_data.get(field_name)
                value, _ = EventCustomFieldValue.objects.get_or_create(
                    event=event, custom_field=cf,
                    defaults={'custom_field_option_id': None},
                )
                if option_id:
                    value.custom_field_option_id = int(option_id)
                else:
                    value.custom_field_option_id = None
                value.save()
            messages.success(request, f"Event '{event.name}' created successfully.")
            if event.start_date >= date.today():
                _regenerate_event_doc_background(org)
            return redirect('tickets:event_detail', event_id=event.id)
    else:
        form = EventForm(organization=org)
        talent_formset = EventTalentFormSet(queryset=EventTalent.objects.none(), prefix='talent')

    venue_capacities = {
        str(v.id): v.capacity
        for v in Venue.objects.filter(organization=org)
        if v.capacity
    }
    context = {
        'form': form,
        'talent_formset': talent_formset,
        'venue_capacities_json': json.dumps(venue_capacities),
    }
    return render(request, 'tickets/event_create.html', context)


@login_required
@require_org
def event_edit(request, event_id):
    """Edit an existing event."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    if request.method == 'POST':
        form = EventForm(request.POST, instance=event, organization=org)
        talent_formset = EventTalentFormSet(request.POST, prefix='talent')
        if form.is_valid() and talent_formset.is_valid():
            was_future = event.start_date >= date.today()
            event = form.save(commit=False)
            event.updated_by = request.user
            event.save()
            instances = talent_formset.save(commit=False)
            for obj in instances:
                if obj.name and obj.name.strip():
                    obj.event = event
                    obj.save()
            for obj in talent_formset.deleted_objects:
                obj.delete()
            # Save custom field values
            for cf in CustomField.objects.filter(field_type='dropdown', organization=org):
                field_name = f'custom_field_{cf.id}'
                option_id = form.cleaned_data.get(field_name)
                value, _ = EventCustomFieldValue.objects.get_or_create(
                    event=event, custom_field=cf,
                    defaults={'custom_field_option_id': None},
                )
                if option_id:
                    value.custom_field_option_id = int(option_id)
                else:
                    value.custom_field_option_id = None
                value.save()
            messages.success(request, f"Event '{event.name}' updated successfully.")
            if was_future or event.start_date >= date.today():
                _regenerate_event_doc_background(org)
            return redirect('tickets:event_detail', event_id=event.id)
    else:
        form = EventForm(instance=event, organization=org)
        talent_formset = EventTalentFormSet(
            queryset=EventTalent.objects.filter(event=event).order_by('order', 'name'),
            prefix='talent',
        )

    context = {
        'form': form,
        'talent_formset': talent_formset,
        'event': event,
    }
    return render(request, 'tickets/event_edit.html', context)


@login_required
@require_org
@require_http_methods(["GET", "POST"])
def event_upload_csv(request, event_id):
    """Upload a CSV directly for an existing event."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org).select_related('venue'), id=event_id)

    if request.method == 'POST':
        form = EventCSVUploadForm(request.POST, request.FILES, organization=org)
        if form.is_valid():
            csv_file = form.cleaned_data['csv_file']
            csv_format = form.cleaned_data['csv_format']

            uploaded_file = UploadedFile.objects.create(
                organization=org,
                csv_format=csv_format,
                filename=csv_file.name,
                description='',
                source='',
                metadata={
                    'notes': form.cleaned_data.get('notes', ''),
                    'event_id': str(event.id),
                    'event_name': event.name,
                    'event_start_date': event.start_date.isoformat() if event.start_date else '',
                    'event_start_time': event.start_time.isoformat() if event.start_time else '',
                    'venue_id': str(event.venue.id) if event.venue else '',
                    'venue_name': event.venue.name if event.venue else '',
                    'venue_city': event.venue.city if event.venue else '',
                }
            )

            media_path = os.path.join('uploads', f"{uploaded_file.id}_{csv_file.name}")
            os.makedirs(os.path.dirname(os.path.join('media', media_path)), exist_ok=True)
            with open(os.path.join('media', media_path), 'wb+') as destination:
                for chunk in csv_file.chunks():
                    destination.write(chunk)

            uploaded_file.metadata['file_path'] = media_path
            uploaded_file.save(update_fields=['metadata'])

            if csv_format.requires_manual_pricing:
                return redirect('tickets:price_entry', file_id=uploaded_file.id)
            else:
                return process_csv_file(request, uploaded_file)
    else:
        form = EventCSVUploadForm(organization=org)

    context = {
        'form': form,
        'event': event,
    }
    return render(request, 'tickets/event_upload.html', context)


@login_required
@require_org
@require_http_methods(["POST"])
def regenerate_event_doc(request):
    """Trigger regeneration of the Upcoming Events Google Doc."""
    from django.conf import settings
    from .services.google_docs import EventDocFormatter, GoogleDocWriter

    org = get_organization(request)
    doc_id = settings.GOOGLE_DOC_ID

    if not doc_id:
        messages.error(request, "Google Doc ID is not configured.")
        return redirect('tickets:home')

    if not settings.GOOGLE_SERVICE_ACCOUNT_JSON:
        messages.error(request, "Google service account credentials are not configured.")
        return redirect('tickets:home')

    formatter = EventDocFormatter(org)
    events = formatter.get_upcoming_events()
    content = formatter.generate_full_document()

    try:
        writer = GoogleDocWriter(doc_id)
        result = writer.update_document(content)
        if result['success']:
            messages.success(
                request,
                f"Updated Google Doc with {len(events)} upcoming event(s) "
                f"({result['characters_written']} characters)."
            )
        else:
            messages.error(request, f"Failed to update Google Doc: {result['error']}")
    except Exception as e:
        messages.error(request, f"Error updating Google Doc: {e}")

    return redirect('tickets:home')


# Forecast Tool Views

@login_required
@require_org
def forecast_tool(request):
    """Display the standalone forecast tool page."""
    org = get_organization(request)
    venues = Venue.objects.filter(organization=org).order_by('city', 'name')
    context = {
        'venues': venues,
    }
    return render(request, 'tickets/forecast_tool.html', context)


@login_required
@require_org
def forecast_api(request):
    """Return forecast data as JSON for the chart."""
    from datetime import datetime

    org = get_organization(request)
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
        organization=org,
    )

    response = JsonResponse(result)
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    response['Pragma'] = 'no-cache'
    return response
