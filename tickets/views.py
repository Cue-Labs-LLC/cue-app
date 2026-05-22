import calendar
import csv
import io
import os
import json
import random
import secrets
import uuid as _uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from django import forms as django_forms
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.urls import reverse, reverse_lazy
from django.conf import settings
from django.db.models import (
    Sum, Count, Avg, Max, Min, Q, Subquery, OuterRef, Prefetch,
    Case, When, Value, F, CharField, Exists, ExpressionWrapper, DecimalField,
)
from django.db.models.functions import Coalesce, Greatest, TruncDate, Cast, TruncMonth, TruncQuarter
from django.db import models
from django.core.paginator import Paginator
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.http import JsonResponse, Http404, HttpResponse, HttpResponseBadRequest
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.db import connection, IntegrityError, transaction
from django.utils import timezone as django_tz
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify

from .models import (
    Organization, UserProfile, OrganizationMembership, OrganizationInvitation,
    AIRecommendation,
    CSVFormat, UploadedFile, Customer, CustomerTag, Event, EventExpense, EventEmailCampaign, EventSMSCampaign, EventTalent, TicketOrder, Ticket, Venue,
    CustomField, CustomFieldOption, EventCustomFieldValue, IncomeSource, EventIncome,
    SurveyQuestion, SurveyInvitation, SurveyResponse, SurveyAnswer,
    PipedreamCalendarConnection, OrganizationAPIKey,
    SaleableTicketType, SaleableTicketTypeTier, StripeCheckoutSession, Payout, PromoCode,
    ExternalSurveyUpload, ExternalSurveyResponse, EventDailyPageView,
    WaitlistEntry, OrganizerWaitlist,
    ScannerSession, generate_unique_scanner_pin, TrackingLink, _generate_tracking_token,
    EVENT_STATUS_DRAFT, EVENT_STATUS_LIVE, EVENT_STATUS_ENDED, EVENT_STATUS_CANCELLED,
)
from .forms import (
    EventCSVUploadForm, EventExpenseForm, TicketPriceEntryForm, CSVFormatForm,
    VenueForm, EventForm, EventTalentFormSet, LoginForm,
    CustomFieldForm, CustomFieldOptionFormSet,
    IncomeSourceForm, EventIncomeForm,
    OTPVerificationForm, MemberInviteForm, AttendeePhoneForm,
    ProfileCompletionForm, EmailLoginForm, EmailProfileCompletionForm,
    SaleableTicketTypeForm, SaleableTicketTypeTierFormSet, PublicTicketPurchaseForm,
    DirectEventForm, DirectTicketTypeFormSet,
    PromoCodeForm, SurveyUploadForm, UserProfileForm, OrgProfileForm,
    WaitlistJoinForm, OrganizerWaitlistForm,
)
from .csv_processor import CSVProcessor
from .services.forecasting.preview import generate_forecast_preview
from .services.pricing import SmartPricingRecommender
from .services.churn_detection.churn_calculator import ChurnDetectionService, THRESHOLD_OPTIONS
from .services.segmentation import (
    BEHAVIOR_PROFILE_BADGE_COLORS,
    BEHAVIOR_PROFILE_DESCRIPTIONS,
    BEHAVIOR_PROFILE_ORDER,
)
from .services.segmentation.segment_definitions import (
    SEGMENT_BADGE_COLORS,
    SEGMENT_DESCRIPTIONS,
    SEGMENT_RULES,
)
RFM_RECENCY_LABELS = {
    5: "Bought very recently",
    4: "Bought recently",
    3: "Bought a few months ago",
    2: "Hasn't bought in a while",
    1: "Hasn't bought in a long time",
}
RFM_FREQUENCY_LABELS = {
    5: "Very frequent buyer",
    4: "Frequent buyer",
    3: "Repeat buyer",
    2: "Occasional buyer",
    1: "One-time buyer",
}
RFM_MONETARY_LABELS = {
    5: "Top spender",
    4: "High spender",
    3: "Moderate spender",
    2: "Low spender",
    1: "Minimal spend",
}
BEHAVIOR_METRIC_LABELS = {
    "days_since_last_order": "Days since last order",
    "avg_days_between_orders": "Average days between orders",
    "days_to_second_order": "Days to second order",
}

from .services.cohort_analysis.repeat_customer_calculator import RepeatCustomerCalculator
from .services.cohort_analysis.cohort_retention_calculator import CohortRetentionCalculator
from .services.meta_ads import (
    MetaAdsAPIError,
    MetaAdsClient,
    exchange_code_for_token as meta_exchange_code_for_token,
    exchange_for_long_lived_token,
)
from .services.meta_campaign_matcher import MetaCampaignMatcher
from .services.mailchimp import (
    MailchimpAPIError,
    MailchimpClient,
    build_authorize_url,
    exchange_code_for_token,
    get_oauth_metadata,
    normalize_campaign_report,
)
from .services.mailchimp_campaign_matcher import MailchimpCampaignMatcher
from .services.slicktext import (
    SlickTextAPIError,
    SlickTextClient,
    build_campaign_report as build_slicktext_campaign_report,
    normalize_campaign_report as normalize_slicktext_campaign_report,
)
from .services.slicktext_campaign_matcher import SlickTextCampaignMatcher
from .services.marketing import (
    MarketingAnalyticsService,
    WINDOW_CHOICES as MARKETING_WINDOW_CHOICES,
    generate_marketing_narrative,
)
from .services.marketing.analytics import DEFAULT_WINDOW, resolve_window
from .utils import get_organization, require_org, require_organizer, require_host, require_admin, require_owner, clear_org_cache, next_order_number, generate_qr_b64
from .feature_flags import (
    smart_pricing_recommendations_enabled,
    browse_events_enabled,
)

from django.core.cache import cache as django_cache

from .cache_utils import safe_cache_delete, safe_cache_get, safe_cache_set

import logging
logger = logging.getLogger(__name__)


WINDOW_CHOICES = [
    ('This Month',   'this_month'),
    ('Last Month',   'last_month'),
    ('This Quarter', 'this_quarter'),
    ('Last Quarter', 'last_quarter'),
    ('This Year',    'this_year'),
    ('Last Year',    'last_year'),
    ('All',          'all'),
    ('Custom',       'custom'),
]


def _is_e2e_test_mode():
    from django.conf import settings as django_settings
    return getattr(django_settings, 'E2E_TEST_MODE', False)


def _format_meta_ads_datetime(value):
    """Format Meta Ads timestamps for campaign matching UI."""
    if not value:
        return ''

    parsed = parse_datetime(str(value))
    if parsed is None:
        parsed_date = parse_date(str(value)[:10])
        if not parsed_date:
            return str(value)
        parsed = datetime.combine(parsed_date, time.min)

    if django_tz.is_naive(parsed):
        parsed = django_tz.make_aware(parsed, django_tz.get_current_timezone())
    parsed = django_tz.localtime(parsed)

    day = parsed.day
    if 10 <= day % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')

    hour = parsed.strftime('%I').lstrip('0') or '12'
    return f"{parsed.strftime('%A, %B')} {day}{suffix} {parsed.year} at {hour}:{parsed.strftime('%M %p')} PST"


def _configure_direct_create_unlock_fields(ticket_formset):
    """Use draft row indexes for unlock relationships before SaleableTicketTypes exist."""
    draft_choices = [('', '- None -')]
    active_rows = []
    for idx, form_item in enumerate(ticket_formset.forms):
        if form_item.is_bound:
            raw_name = form_item.data.get(f'{form_item.prefix}-name', '')
            raw_delete = form_item.data.get(f'{form_item.prefix}-DELETE')
            is_deleted = str(raw_delete).lower() in ('on', 'true', '1')
        else:
            raw_name = form_item.initial.get('name', '') or getattr(form_item.instance, 'name', '')
            is_deleted = False
        if raw_name and str(raw_name).strip() and not is_deleted:
            active_rows.append((str(idx), str(raw_name).strip()))

    draft_choices.extend(active_rows)

    for idx, form_item in enumerate(ticket_formset.forms):
        current_choices = [choice for choice in draft_choices if choice[0] != str(idx)]
        selected = (
            form_item.data.get(f'{form_item.prefix}-unlocks_after')
            if form_item.is_bound
            else form_item.initial.get('unlocks_after', '')
        ) or ''
        if selected and not any(value == selected for value, _ in current_choices):
            current_choices.append((selected, selected))
        form_item.fields['unlocks_after'] = django_forms.ChoiceField(
            required=False,
            choices=current_choices,
            widget=django_forms.Select(attrs={'class': 'form-select form-select-sm'}),
        )

    ticket_formset.empty_form.fields['unlocks_after'] = django_forms.ChoiceField(
        required=False,
        choices=[('', '- None -')],
        widget=django_forms.Select(attrs={'class': 'form-select form-select-sm'}),
    )


def _quarter_bounds(year, q):
    """Return (first_day, last_day) for the given quarter (1–4)."""
    start_month = (q - 1) * 3 + 1
    start = date(year, start_month, 1)
    end_month = start_month + 2
    if end_month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, end_month + 1, 1) - timedelta(days=1)
    return start, end


def _parse_window(request):
    """Return (start_date, end_date, active_window) from ?window= query params."""
    today = date.today()
    window = request.GET.get('window', 'this_year')
    if window == 'this_month':
        return date(today.year, today.month, 1), today, window
    if window == 'last_month':
        first_this = date(today.year, today.month, 1)
        last_prev = first_this - timedelta(days=1)
        return date(last_prev.year, last_prev.month, 1), last_prev, window
    if window == 'this_year':
        return date(today.year, 1, 1), today, window
    if window == 'last_year':
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), window
    if window == 'this_quarter':
        q = (today.month - 1) // 3 + 1
        start, _ = _quarter_bounds(today.year, q)
        return start, today, window
    if window == 'last_quarter':
        q = (today.month - 1) // 3 + 1
        prev_q = q - 1 if q > 1 else 4
        prev_year = today.year if q > 1 else today.year - 1
        start, end = _quarter_bounds(prev_year, prev_q)
        return start, end, window
    if window == 'custom':
        try:
            start = date.fromisoformat(request.GET.get('start', ''))
        except ValueError:
            start = None
        try:
            end = date.fromisoformat(request.GET.get('end', ''))
        except ValueError:
            end = today
        return start, end, 'custom'
    return None, None, 'all'


def _event_list_cache_key(org_id, search, sort, page, status_filter, actions_count=0):
    """Build a versioned, org-scoped cache key for the event_list response."""
    try:
        version = django_cache.get(f'event_list_ver:{org_id}', 0)
    except Exception:
        version = 0
    return f'event_list:{version}:{org_id}:{search}:{sort}:{page}:{status_filter}:a{actions_count}'


def _invalidate_event_list_cache(org):
    """Bump the version counter so all existing event_list cache entries expire naturally."""
    key = f'event_list_ver:{org.pk}'
    try:
        django_cache.incr(key)
    except ValueError:
        try:
            django_cache.set(key, 1, timeout=None)
        except Exception:
            pass
    except Exception:
        pass


EVENT_STATS_CACHE_VERSION = 4

EVENT_STATS_REQUIRED_KEYS = frozenset({
    'total_orders',
    'ticket_revenue',
    'ticket_fees',
    'net_ticket_revenue',
    'total_tickets',
    'total_customers',
    'new_customers_count',
    'returning_customers_count',
    'total_additional_income',
    'additional_income_lines',
    'total_revenue',
    'total_expenses',
    'expenses',
    'expenses_by_category',
    'profit',
    'margin_pct',
    'ticket_type_breakdown',
    'ticket_type_allocation_charts',
    'saleable_ticket_types_list',
    'sales_over_time',
    'page_views_over_time',
    'survey_invitations_count',
    'survey_responses_count',
    'external_survey_responses_count',
    'survey_total_response_count',
    'survey_results',
    'attendee_segments',
})


def _event_stats_cache_key(event_id):
    """Cache key for _compute_event_stats() results. Invalidated via django_cache.delete()."""
    return f'event_stats:v{EVENT_STATS_CACHE_VERSION}:{event_id}'


def _event_upload_stats_cache_key(event_id):
    """Cache key for cached upload stats shown on the event detail uploads card."""
    return f'event_upload_stats:{event_id}'


def _invalidate_event_upload_stats_cache(event_id):
    """Clear cached upload stats for an event."""
    try:
        django_cache.delete(_event_upload_stats_cache_key(event_id))
    except Exception:
        pass


def _compute_event_upload_stats(event):
    """Return cached per-upload stats for an event using grouped queries only.

    Results are cached for 300s. Aggregations remain isolated to avoid join
    inflation while also avoiding correlated subqueries per upload.
    """
    cache_key = _event_upload_stats_cache_key(event.pk)
    cached = django_cache.get(cache_key)
    if cached is not None:
        return cached

    associated_uploads = list(
        event.get_associated_uploads().select_related('csv_format')
    )
    if not associated_uploads:
        try:
            django_cache.set(cache_key, [], 300)
        except Exception:
            pass
        return []

    order_stats_map = {
        row['uploaded_file']: row
        for row in TicketOrder.objects.filter(
            event=event,
            uploaded_file__isnull=False,
        )
        .values('uploaded_file')
        .annotate(
            orders_count=Count('id'),
            revenue=Coalesce(Sum('total_amount'), Decimal('0.00')),
        )
    }
    ticket_counts_map = {
        row['ticket_order__uploaded_file']: row['tickets_count']
        for row in Ticket.objects.filter(
            ticket_order__event=event,
            ticket_order__uploaded_file__isnull=False,
        )
        .values('ticket_order__uploaded_file')
        .annotate(tickets_count=Count('id'))
    }

    upload_stats = []
    for upload in associated_uploads:
        order_stats = order_stats_map.get(upload.id, {})
        upload_stats.append({
            'upload': upload,
            'orders_count': order_stats.get('orders_count', 0),
            'revenue': order_stats.get('revenue', Decimal('0.00')),
            'tickets_count': ticket_counts_map.get(upload.id, 0),
        })

    try:
        django_cache.set(cache_key, upload_stats, 300)
    except Exception:
        pass
    return upload_stats


def _reconcile_customers_after_order_deletion(organization, affected_customer_ids):
    """Reconcile surviving customers after order deletion in a deterministic lock order."""
    customer_ids = sorted({cid for cid in affected_customer_ids if cid})
    if not customer_ids:
        return 0

    customers = list(
        Customer.objects.filter(
            organization=organization,
            id__in=customer_ids,
        )
        .select_for_update()
        .order_by('id')
    )
    if not customers:
        return 0

    remaining_order_stats = {
        row['customer_id']: row
        for row in TicketOrder.objects.filter(
            customer_id__in=[customer.id for customer in customers],
            customer__organization=organization,
        )
        .values('customer_id')
        .annotate(
            order_count=Count('id'),
            lifetime_value=Coalesce(
                Sum('total_amount', filter=Q(refunded_at__isnull=True)),
                Decimal('0.00'),
            ),
            last_order_at=Max('order_date'),
        )
    }

    customers_deleted = 0
    for customer in customers:
        stats = remaining_order_stats.get(customer.id)
        if not stats or stats['order_count'] == 0:
            customer.delete()
            customers_deleted += 1
            continue

        customer.lifetime_value = stats['lifetime_value']
        customer.last_order_date = (
            stats['last_order_at'].date() if stats['last_order_at'] else None
        )
        customer.save(update_fields=['lifetime_value', 'last_order_date'])

    return customers_deleted


def _clear_waitlist_hold(request, event_id):
    """Mark a waitlist hold as purchased and release the hold counter."""
    wl_hold = request.session.pop(f'waitlist_hold_{event_id}', None)
    if not wl_hold:
        return
    from django.utils import timezone as tz
    WaitlistEntry.objects.filter(
        id=wl_hold['entry_id'], purchased_at__isnull=True
    ).update(purchased_at=tz.now())
    SaleableTicketType.objects.filter(id=wl_hold['ticket_type_id']).update(
        quantity_held=Greatest(F('quantity_held') - 1, Value(0))
    )




def _annotate_events(queryset):
    """Annotate an Event queryset with stats needed for the event list page.

    Uses denormalized cached_* fields for all ticket stats (avoids expensive
    Ticket→TicketOrder joins on large datasets). Only two subqueries remain:
    has_uploads (EXISTS — needed for the past-event warning logic) and total_expenses
    (EventExpense table is small and now has a covering index on (event, deleted_at)).
    """
    return queryset.annotate(
        # EXISTS stops on the first match — avoids COUNT(DISTINCT) table scans on large
        # events. The (event_id, uploaded_file_id) index makes this an index point-lookup.
        has_uploads=Exists(
            TicketOrder.objects.filter(
                event=OuterRef('pk'),
                uploaded_file__isnull=False,
            )
        ),
        total_expenses=Coalesce(
            Subquery(
                EventExpense.objects.visible().filter(event=OuterRef('pk'))
                .values('event')
                .annotate(total=Sum('amount'))
                .values('total')[:1],
                output_field=models.DecimalField(max_digits=10, decimal_places=2),
            ),
            Decimal('0.00'),
        ),
        # Denormalized fields — maintained by signals.refresh_event_stats and CSV import
        total_revenue=F('computed_total_revenue'),
        total_tickets=F('cached_ticket_count'),
        paid_ticket_sum=F('cached_paid_ticket_sum'),
        paid_ticket_count=F('cached_paid_ticket_count'),
        # Actual platform fees from completed Stripe sessions (direct ticketing only)
        platform_fees_cents=Coalesce(
            Subquery(
                StripeCheckoutSession.objects.filter(
                    ticket_order__event=OuterRef('pk')
                )
                .values('ticket_order__event')
                .annotate(total=Sum('platform_fee_cents'))
                .values('total')[:1],
                output_field=models.IntegerField(),
            ),
            0,
        ),
    )



def _sync_event_to_google_calendar(event):
    """Send event to Pipedream webhook for Google Calendar sync. Fails silently if not configured."""
    from .services.google_calendar.sync import send_event_to_pipedream
    try:
        connection = PipedreamCalendarConnection.objects.filter(
            organization=event.organization,
        ).first()
        if not connection or not connection.webhook_url:
            return
        success, google_event_id = send_event_to_pipedream(
            connection.webhook_url, event
        )
        if success and google_event_id:
            event.google_calendar_event_id = google_event_id
            event.save(update_fields=['google_calendar_event_id'])
    except Exception:
        logger.exception("Failed to sync event to Google Calendar via Pipedream")


# Authentication Views

@never_cache
def unified_login_view(request):
    """Step 1: enter phone number - handles both login and new signup."""
    from .sms import start_phone_verification
    if request.user.is_authenticated:
        try:
            if request.user.profile.is_organizer:
                return redirect('tickets:home')
        except UserProfile.DoesNotExist:
            pass
        return redirect('tickets:attendee_dashboard')
    if request.method == 'POST':
        form = AttendeePhoneForm(request.POST)
        if form.is_valid():
            phone = form.cleaned_data['phone_number']
            is_new = not UserProfile.objects.filter(phone_number=phone).exists()
            if not start_phone_verification(phone):
                messages.error(request, 'Could not send a verification code. Please check the number and try again.')
            else:
                request.session['verify_unified'] = {'phone': phone, 'is_new': is_new}
                return redirect('tickets:unified_verify')
    else:
        form = AttendeePhoneForm()
        next_url = request.GET.get('next', '')
        if next_url:
            request.session['auth_next'] = next_url
    return render(request, 'tickets/auth/login.html', {'form': form})


@never_cache
@require_http_methods(["GET", "POST"])
def unified_verify_view(request):
    """Step 2: verify OTP - log in existing user or send new user to profile completion."""
    from django.contrib.auth import login as auth_login
    from django.contrib.auth.models import User
    from .sms import check_phone_verification
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    session_data = request.session.get('verify_unified')
    if not session_data:
        messages.info(request, 'This verification step has already completed or expired. Please start again.')
        return redirect('tickets:login')
    phone = session_data['phone']
    is_new = session_data.get('is_new', False)
    if request.method == 'POST':
        form = OTPVerificationForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data['otp_code']
            if not check_phone_verification(phone, code):
                messages.error(request, 'Incorrect or expired code. Please try again.')
            else:
                del request.session['verify_unified']
                if not is_new:
                    try:
                        profile = UserProfile.objects.select_related('user').get(phone_number=phone)
                        user = profile.user
                    except UserProfile.DoesNotExist:
                        messages.error(request, 'Account not found. Please sign up.')
                        return redirect('tickets:login')
                    auth_login(request, user, backend='tickets.backends.PhoneBackend')
                    invitation = _maybe_accept_pending_invite(request)
                    if invitation is not None:
                        request.session.pop('auth_next', None)
                        is_organizer = invitation.role == UserProfile.Role.ORGANIZER
                        return redirect('tickets:home' if is_organizer else 'tickets:attendee_dashboard')
                    try:
                        if user.profile.is_organizer:
                            request.session.pop('auth_next', None)
                            return redirect('tickets:home')
                    except UserProfile.DoesNotExist:
                        pass
                    next_url = request.session.pop('auth_next', None)
                    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                        return redirect(next_url)
                    return redirect('tickets:attendee_dashboard')
                else:
                    request.session['pending_signup_phone'] = phone
                    return redirect('tickets:complete_profile')
    else:
        form = OTPVerificationForm()
    return render(request, 'tickets/auth/login_verify.html', {
        'form': form,
        'masked_phone': f"***{phone[-4:]}",
        'is_new': is_new,
    })


@require_http_methods(["POST"])
def unified_resend_view(request):
    """Resend OTP for the unified login/signup flow."""
    from .sms import start_phone_verification
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    session_data = request.session.get('verify_unified')
    if not session_data:
        return redirect('tickets:login')
    if not start_phone_verification(session_data['phone']):
        messages.error(request, 'Could not resend the code. Please try again.')
    else:
        messages.success(request, 'A new code has been sent.')
    return redirect('tickets:unified_verify')


@never_cache
@require_http_methods(["GET", "POST"])
def complete_profile_view(request):
    """Step 3 (new users only): collect name, email, gender, marketing opt-in."""
    from .sms import start_email_verification
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    phone = request.session.get('pending_signup_phone')
    if not phone:
        return redirect('tickets:login')
    if request.method == 'POST':
        form = ProfileCompletionForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            if UserProfile.objects.filter(phone_number=phone).exists():
                messages.info(request, 'An account with this phone already exists. Please log in.')
                del request.session['pending_signup_phone']
                return redirect('tickets:login')
            email = cd['email']
            if not start_email_verification(email):
                messages.error(request, 'Could not send a verification code to that email. Please check the address and try again.')
                return render(request, 'tickets/auth/complete_profile.html', {'form': form})
            del request.session['pending_signup_phone']
            request.session['pending_profile_data'] = {
                'phone': phone,
                'first_name': cd['first_name'],
                'last_name': cd['last_name'],
                'email': email,
                'gender': cd['gender'],
                'marketing_opt_in': cd['marketing_opt_in'],
            }
            return redirect('tickets:verify_email_after_profile')
    else:
        form = ProfileCompletionForm()
    return render(request, 'tickets/auth/complete_profile.html', {'form': form})


@require_http_methods(["GET", "POST"])
def verify_email_after_profile_view(request):
    """Step 4 (phone signup, new users): verify email OTP then create account."""
    from django.contrib.auth import login as auth_login
    from django.contrib.auth.models import User
    from .sms import check_email_verification
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    profile_data = request.session.get('pending_profile_data')
    if not profile_data:
        return redirect('tickets:login')
    email = profile_data['email']
    at_index = email.index('@')
    masked_email = email[:2] + '***' + email[at_index:]
    if request.method == 'POST':
        form = OTPVerificationForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data['otp_code']
            if not check_email_verification(email, code):
                messages.error(request, 'Incorrect or expired code. Please try again.')
            else:
                from .utils import generate_username
                from django.db import transaction, IntegrityError
                from django.utils import timezone as tz
                try:
                    with transaction.atomic():
                        user = User.objects.create(
                            username=generate_username(profile_data['first_name'], profile_data['last_name']),
                            email=email,
                            first_name=profile_data['first_name'],
                            last_name=profile_data['last_name'],
                        )
                        user.set_unusable_password()
                        user.save()
                        UserProfile.objects.create(
                            user=user,
                            role=UserProfile.Role.ATTENDEE,
                            phone_number=profile_data['phone'],
                            gender=profile_data['gender'],
                            marketing_opt_in=profile_data['marketing_opt_in'],
                            terms_accepted_at=tz.now(),
                        )
                except IntegrityError:
                    messages.info(request, 'An account with this phone or email already exists. Please log in.')
                    del request.session['pending_profile_data']
                    return redirect('tickets:login')
                del request.session['pending_profile_data']
                auth_login(request, user, backend='tickets.backends.PhoneBackend')
                messages.success(request, 'Welcome to Cue!')
                invitation = _maybe_accept_pending_invite(request)
                if invitation is not None:
                    is_organizer = invitation.role == UserProfile.Role.ORGANIZER
                    return redirect('tickets:home' if is_organizer else 'tickets:attendee_dashboard')
                next_url = request.session.pop('auth_next', None)
                if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                    return redirect(next_url)
                return redirect('tickets:attendee_dashboard')
    else:
        form = OTPVerificationForm()
    return render(request, 'tickets/auth/verify_email_after_profile.html', {
        'form': form,
        'masked_email': masked_email,
    })


@require_http_methods(["POST"])
def resend_email_after_profile_view(request):
    """Resend email OTP during phone-signup email verification step."""
    from .sms import start_email_verification
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    profile_data = request.session.get('pending_profile_data')
    if not profile_data:
        return redirect('tickets:login')
    if not start_email_verification(profile_data['email']):
        messages.error(request, 'Could not resend the code. Please try again.')
    else:
        messages.success(request, 'A new code has been sent.')
    return redirect('tickets:verify_email_after_profile')


@never_cache
@require_http_methods(["GET", "POST"])
def email_login_view(request):
    """Step 1 (email path): enter email address - handles both login and new signup."""
    from .sms import start_email_verification
    if request.user.is_authenticated:
        try:
            if request.user.profile.is_organizer:
                return redirect('tickets:home')
        except UserProfile.DoesNotExist:
            pass
        return redirect('tickets:attendee_dashboard')
    if request.method == 'POST':
        form = EmailLoginForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email']
            from django.contrib.auth.models import User
            is_new = not User.objects.filter(email__iexact=email).exists()
            if not start_email_verification(email):
                messages.error(request, 'Could not send a verification code. Please check the address and try again.')
            else:
                request.session['verify_email'] = {'email': email, 'is_new': is_new}
                return redirect('tickets:email_verify')
    else:
        initial = {}
        prefill_email = request.GET.get('email', '').strip()
        if prefill_email:
            initial['email'] = prefill_email
        form = EmailLoginForm(initial=initial)
        next_url = request.GET.get('next', '')
        if next_url:
            request.session['auth_next'] = next_url
    return render(request, 'tickets/auth/login_email.html', {'form': form})


@never_cache
@require_http_methods(["GET", "POST"])
def email_verify_view(request):
    """Step 2 (email path): verify OTP - log in existing user or send new user to profile completion."""
    from django.contrib.auth import login as auth_login
    from django.contrib.auth.models import User
    from .sms import check_email_verification
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    session_data = request.session.get('verify_email')
    if not session_data:
        return redirect('tickets:email_login')
    email = session_data['email']
    is_new = session_data.get('is_new', False)
    # Mask the email: show first 2 chars then *** @ domain
    at_index = email.index('@')
    masked_email = email[:2] + '***' + email[at_index:]
    if request.method == 'POST':
        form = OTPVerificationForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data['otp_code']
            if not check_email_verification(email, code):
                messages.error(request, 'Incorrect or expired code. Please try again.')
            else:
                del request.session['verify_email']
                if not is_new:
                    try:
                        user = User.objects.get(email__iexact=email)
                    except User.DoesNotExist:
                        messages.error(request, 'Account not found. Please sign up.')
                        return redirect('tickets:email_login')
                    auth_login(request, user, backend='tickets.backends.EmailOTPBackend')
                    invitation = _maybe_accept_pending_invite(request)
                    if invitation is not None:
                        request.session.pop('auth_next', None)
                        is_organizer = invitation.role == UserProfile.Role.ORGANIZER
                        return redirect('tickets:home' if is_organizer else 'tickets:attendee_dashboard')
                    try:
                        if user.profile.is_organizer:
                            request.session.pop('auth_next', None)
                            return redirect('tickets:home')
                    except UserProfile.DoesNotExist:
                        pass
                    next_url = request.session.pop('auth_next', None)
                    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                        return redirect(next_url)
                    return redirect('tickets:attendee_dashboard')
                else:
                    request.session['pending_signup_email'] = email
                    return redirect('tickets:email_complete_profile')
    else:
        form = OTPVerificationForm()
    return render(request, 'tickets/auth/login_email_verify.html', {
        'form': form,
        'masked_email': masked_email,
        'is_new': is_new,
    })


@require_http_methods(["POST"])
def email_resend_view(request):
    """Resend OTP for the email login/signup flow."""
    from .sms import start_email_verification
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    session_data = request.session.get('verify_email')
    if not session_data:
        return redirect('tickets:email_login')
    if not start_email_verification(session_data['email']):
        messages.error(request, 'Could not resend the code. Please try again.')
    else:
        messages.success(request, 'A new code has been sent.')
    return redirect('tickets:email_verify')


@never_cache
@require_http_methods(["GET", "POST"])
def email_complete_profile_view(request):
    """Step 3 (email path, new users only): collect name, phone, gender, marketing opt-in."""
    from .sms import start_phone_verification
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    email = request.session.get('pending_signup_email')
    if not email:
        return redirect('tickets:email_login')
    if request.method == 'POST':
        form = EmailProfileCompletionForm(request.POST, initial={'email_display': email})
        if form.is_valid():
            cd = form.cleaned_data
            from django.contrib.auth.models import User
            if User.objects.filter(email__iexact=email).exists():
                messages.info(request, 'An account with this email already exists. Please log in.')
                del request.session['pending_signup_email']
                return redirect('tickets:email_login')
            phone = cd['phone_number']
            if not start_phone_verification(phone):
                messages.error(request, 'Could not send a verification code to that number. Please check it and try again.')
                return render(request, 'tickets/auth/complete_profile.html', {'form': form})
            del request.session['pending_signup_email']
            request.session['pending_email_profile_data'] = {
                'email': email,
                'first_name': cd['first_name'],
                'last_name': cd['last_name'],
                'phone_number': phone,
                'gender': cd['gender'],
                'marketing_opt_in': cd['marketing_opt_in'],
            }
            return redirect('tickets:verify_phone_after_profile')
    else:
        form = EmailProfileCompletionForm(initial={'email_display': email})
    return render(request, 'tickets/auth/complete_profile.html', {'form': form})


@require_http_methods(["GET", "POST"])
def verify_phone_after_profile_view(request):
    """Step 4 (email signup, new users): verify phone OTP then create account."""
    from django.contrib.auth import login as auth_login
    from django.contrib.auth.models import User
    from .sms import check_phone_verification
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    profile_data = request.session.get('pending_email_profile_data')
    if not profile_data:
        return redirect('tickets:email_login')
    phone = profile_data['phone_number']
    if request.method == 'POST':
        form = OTPVerificationForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data['otp_code']
            if not check_phone_verification(phone, code):
                messages.error(request, 'Incorrect or expired code. Please try again.')
            else:
                from .utils import generate_username
                from django.db import transaction, IntegrityError
                from django.utils import timezone as tz
                try:
                    with transaction.atomic():
                        user = User.objects.create(
                            username=generate_username(profile_data['first_name'], profile_data['last_name']),
                            email=profile_data['email'],
                            first_name=profile_data['first_name'],
                            last_name=profile_data['last_name'],
                        )
                        user.set_unusable_password()
                        user.save()
                        UserProfile.objects.create(
                            user=user,
                            role=UserProfile.Role.ATTENDEE,
                            phone_number=phone,
                            gender=profile_data['gender'],
                            marketing_opt_in=profile_data['marketing_opt_in'],
                            terms_accepted_at=tz.now(),
                        )
                except IntegrityError:
                    messages.info(request, 'An account with this phone or email already exists. Please log in.')
                    del request.session['pending_email_profile_data']
                    return redirect('tickets:email_login')
                del request.session['pending_email_profile_data']
                auth_login(request, user, backend='tickets.backends.EmailOTPBackend')
                messages.success(request, 'Welcome to Cue!')
                invitation = _maybe_accept_pending_invite(request)
                if invitation is not None:
                    is_organizer = invitation.role == UserProfile.Role.ORGANIZER
                    return redirect('tickets:home' if is_organizer else 'tickets:attendee_dashboard')
                next_url = request.session.pop('auth_next', None)
                if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                    return redirect(next_url)
                return redirect('tickets:attendee_dashboard')
    else:
        form = OTPVerificationForm()
    return render(request, 'tickets/auth/verify_phone_after_profile.html', {
        'form': form,
        'masked_phone': f"***{phone[-4:]}",
    })


@require_http_methods(["POST"])
def resend_phone_after_profile_view(request):
    """Resend phone OTP during email-signup phone verification step."""
    from .sms import start_phone_verification
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    profile_data = request.session.get('pending_email_profile_data')
    if not profile_data:
        return redirect('tickets:email_login')
    if not start_phone_verification(profile_data['phone_number']):
        messages.error(request, 'Could not resend the code. Please try again.')
    else:
        messages.success(request, 'A new code has been sent.')
    return redirect('tickets:verify_phone_after_profile')


@require_http_methods(["POST"])
def modal_auth_start(request):
    """JSON endpoint: send OTP to phone for inline modal auth flow."""
    import json as _json
    from .sms import start_phone_verification
    try:
        body = _json.loads(request.body)
    except (ValueError, KeyError):
        return JsonResponse({'error': 'Invalid request.'}, status=400)
    phone = (body.get('phone') or '').strip()
    if not phone:
        return JsonResponse({'error': 'Phone number is required.'}, status=400)
    # Normalize to E.164: strip spaces/dashes/parens, then prepend +1 if no country code
    digits = ''.join(c for c in phone if c.isdigit())
    if phone.startswith('+'):
        phone = '+' + digits
    elif len(digits) == 10:
        phone = '+1' + digits
    elif len(digits) == 11 and digits.startswith('1'):
        phone = '+' + digits
    else:
        phone = '+' + digits
    is_new = not UserProfile.objects.filter(phone_number=phone).exists()
    if not start_phone_verification(phone):
        return JsonResponse({'error': 'Could not send a verification code. Please check the number and try again.'}, status=400)
    request.session['modal_auth'] = {'phone': phone, 'is_new': is_new}
    return JsonResponse({'ok': True, 'is_new': is_new})


@require_http_methods(["POST"])
def modal_auth_verify(request):
    """JSON endpoint: verify OTP for inline modal auth flow."""
    import json as _json
    from django.contrib.auth import login as auth_login
    from django.contrib.auth.models import User
    from .sms import check_phone_verification
    try:
        body = _json.loads(request.body)
    except (ValueError, KeyError):
        return JsonResponse({'error': 'Invalid request.'}, status=400)
    code = (body.get('code') or '').strip()
    session_data = request.session.get('modal_auth')
    if not session_data or not code:
        return JsonResponse({'error': 'Session expired. Please start over.'}, status=400)
    phone = session_data['phone']
    is_new = session_data.get('is_new', False)
    if not check_phone_verification(phone, code):
        return JsonResponse({'error': 'Incorrect or expired code. Please try again.'}, status=400)
    if not is_new:
        try:
            profile = UserProfile.objects.select_related('user').get(phone_number=phone)
            user = profile.user
        except UserProfile.DoesNotExist:
            return JsonResponse({'error': 'Account not found. Please try signing up.'}, status=400)
        del request.session['modal_auth']
        auth_login(request, user, backend='tickets.backends.PhoneBackend')
        return JsonResponse({'status': 'logged_in'})
    # New user - keep session data, signal profile step needed
    request.session['modal_auth']['verified'] = True
    return JsonResponse({'status': 'new_user'})


@require_http_methods(["POST"])
def modal_auth_complete(request):
    """JSON endpoint: create account for new users in inline modal auth flow."""
    import json as _json
    from django.contrib.auth import login as auth_login
    from django.contrib.auth.models import User
    try:
        body = _json.loads(request.body)
    except (ValueError, KeyError):
        return JsonResponse({'error': 'Invalid request.'}, status=400)
    session_data = request.session.get('modal_auth')
    if not session_data or not session_data.get('verified'):
        return JsonResponse({'error': 'Session expired. Please start over.'}, status=400)
    phone = session_data['phone']
    first_name = (body.get('first_name') or '').strip()
    last_name = (body.get('last_name') or '').strip()
    email = (body.get('email') or '').strip()
    if not first_name or not email:
        return JsonResponse({'error': 'First name and email are required.'}, status=400)
    if UserProfile.objects.filter(phone_number=phone).exists():
        return JsonResponse({'error': 'An account with this phone already exists. Please log in.'}, status=400)
    from .utils import generate_username
    user = User.objects.create(
        username=generate_username(first_name, last_name),
        email=email,
        first_name=first_name,
        last_name=last_name,
    )
    user.set_unusable_password()
    user.save()
    marketing_opt_in = bool(body.get('marketing_opt_in', False))
    UserProfile.objects.create(
        user=user,
        role=UserProfile.Role.ATTENDEE,
        phone_number=phone,
        marketing_opt_in=marketing_opt_in,
    )
    del request.session['modal_auth']
    from django.contrib.auth import login as auth_login
    auth_login(request, user, backend='tickets.backends.PhoneBackend')
    return JsonResponse({'status': 'logged_in'})


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


def become_organizer_view(request):
    """Beta waitlist for prospective organizers."""
    if request.user.is_authenticated:
        try:
            if request.user.profile.is_organizer:
                return redirect('tickets:home')
        except UserProfile.DoesNotExist:
            pass
        # Approved users skip the form and go straight to org creation
        if OrganizerWaitlist.objects.filter(
            email=request.user.email,
            status=OrganizerWaitlist.Status.APPROVED,
        ).exists():
            return redirect('tickets:create_organization')

    if request.method == 'POST':
        form = OrganizerWaitlistForm(request.POST)
        # Treat duplicate email as idempotent - redirect to success rather than error
        submitted_email = request.POST.get('email', '').strip()
        if submitted_email and OrganizerWaitlist.objects.filter(email=submitted_email).exists():
            return redirect('tickets:waitlist_success')
        if form.is_valid():
            OrganizerWaitlist.objects.create(
                name=form.cleaned_data['name'],
                email=form.cleaned_data['email'],
                organization_name=form.cleaned_data['organization_name'],
                instagram_handle=form.cleaned_data.get('instagram_handle', ''),
            )
            return redirect('tickets:waitlist_success')
    else:
        initial = {}
        if request.user.is_authenticated:
            initial['email'] = request.user.email
            initial['name'] = request.user.get_full_name() or ''
        form = OrganizerWaitlistForm(initial=initial)
    return render(request, 'tickets/auth/become_organizer.html', {'form': form})


def waitlist_success_view(request):
    """Confirmation page after joining the organizer waitlist."""
    return render(request, 'tickets/auth/waitlist_success.html')


def signup_view(request):
    """Step 1: Attendee enters phone; Twilio Verify sends OTP. No org assigned."""
    from .sms import start_phone_verification
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    if request.method == "POST":
        form = AttendeePhoneForm(request.POST)
        if form.is_valid():
            phone = form.cleaned_data["phone_number"]
            if UserProfile.objects.filter(phone_number=phone).exists():
                messages.error(request, 'An account with this phone number already exists. Please log in.')
                return redirect('tickets:phone_login')
            if not start_phone_verification(phone):
                messages.error(request, 'Could not send a verification code. Please check the number and try again.')
            else:
                request.session["verify_signup"] = {"phone": phone}
                return redirect('tickets:verify_otp')
    else:
        form = AttendeePhoneForm()
    return render(request, 'tickets/auth/signup.html', {'form': form})


@require_http_methods(["GET", "POST"])
def verify_otp_view(request):
    """Step 2: Verify Twilio code and create attendee account (no org)."""
    from django.contrib.auth import login
    from django.contrib.auth.models import User
    from .sms import check_phone_verification
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    session_data = request.session.get("verify_signup")
    if not session_data:
        messages.info(request, 'This verification step has already completed or expired. Please start again.')
        return redirect('tickets:signup')
    phone = session_data["phone"]
    if request.method == "POST":
        form = OTPVerificationForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data["otp_code"]
            if not check_phone_verification(phone, code):
                messages.error(request, 'Incorrect or expired code. Please try again.')
            else:
                if UserProfile.objects.filter(phone_number=phone).exists():
                    messages.info(request, 'An account with this phone already exists. Please log in.')
                    del request.session["verify_signup"]
                    return redirect('tickets:phone_login')
                from .utils import generate_username
                user = User.objects.create(username=generate_username('user', phone[-4:]), email='', first_name='', last_name='')
                user.set_unusable_password()
                user.save()
                UserProfile.objects.create(user=user, role=UserProfile.Role.ATTENDEE, phone_number=phone)
                del request.session["verify_signup"]
                login(request, user, backend='tickets.backends.PhoneBackend')
                messages.success(request, 'Account created! Welcome to Cue.')
                return redirect('tickets:attendee_dashboard')
    else:
        form = OTPVerificationForm()
    return render(request, 'tickets/auth/verify_otp.html', {'form': form, 'masked_phone': f"***{phone[-4:]}"})


@require_http_methods(["POST"])
def resend_otp_view(request):
    """Resend Twilio Verify code for the /signup/ phone flow."""
    from .sms import start_phone_verification
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    session_data = request.session.get("verify_signup")
    if not session_data:
        messages.info(request, 'This verification step has already completed or expired. Please start again.')
        return redirect('tickets:signup')
    if not start_phone_verification(session_data["phone"]):
        messages.error(request, 'Could not resend the code. Please try again.')
    else:
        messages.success(request, 'A new code has been sent.')
    return redirect('tickets:verify_otp')


def health_check(request):
    """Health check endpoint for Render monitoring.

    DB failure → 503 (liveness signal, triggers Render restart).
    Redis failure → 200 with error in body (informational — Redis flakiness
    should not restart the service).

    Add ?fmt=json to get machine-readable output.
    """
    import time
    import uuid
    from django.core.cache import cache as django_cache
    from django.conf import settings

    status = {}

    # DB check — sole liveness signal
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        status['db'] = 'ok'
    except Exception as e:
        status['db'] = f'ERROR: {e}'

    # Redis / cache check — informational only
    cache_url = getattr(settings, 'CACHES', {}).get('default', {}).get('LOCATION', 'n/a')
    status['cache_url'] = cache_url
    t0 = time.monotonic()
    try:
        probe_key = f'_health_probe_{uuid.uuid4().hex}'
        probe_val = probe_key
        django_cache.set(probe_key, probe_val, timeout=10)
        val = django_cache.get(probe_key)
        django_cache.delete(probe_key)
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        status['cache'] = 'ok' if val == probe_val else 'MISS'
        status['cache_ms'] = elapsed_ms
    except Exception as e:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        status['cache'] = f'ERROR: {e}'
        status['cache_ms'] = elapsed_ms

    http_status = 200 if status['db'] == 'ok' else 503

    if request.GET.get('fmt') == 'json':
        return JsonResponse(status, status=http_status)
    lines = [f'{k}: {v}' for k, v in status.items()]
    return HttpResponse('\n'.join(lines), status=http_status, content_type='text/plain')


def support(request):
    """Public support / FAQ page."""
    return render(request, 'tickets/support.html')


def privacy(request):
    """Public privacy policy page."""
    return render(request, 'tickets/privacy.html')


def terms(request):
    """Public terms and conditions page."""
    return render(request, 'tickets/terms.html')


def landing(request):
    """Public landing page (no login required). Logged-in users are redirected to the dashboard."""
    if request.user.is_authenticated:
        org = get_organization(request)
        if org is None:
            return redirect('tickets:attendee_dashboard')
        return redirect('tickets:home')
    return render(request, 'tickets/landing.html')


def explore(request):
    """Public page: list upcoming events with direct ticketing (no login required)."""
    if not browse_events_enabled():
        raise Http404
    from .models import TICKETING_TYPE_DIRECT
    today = django_tz.now().date()
    events_qs = (
        Event.objects.filter(
            deleted_at__isnull=True,
            ticketing_type=TICKETING_TYPE_DIRECT,
            status=EVENT_STATUS_LIVE,
        )
        .filter(
            Q(end_date__isnull=False, end_date__gte=today) |
            Q(end_date__isnull=True, start_date__gte=today)
        )
        .select_related('venue')
        .order_by('start_date', 'start_time', 'name')
    )
    paginator = Paginator(events_qs, 24)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    return render(request, 'tickets/explore.html', {'page_obj': page_obj})


def public_org_profile(request, slug):
    """Public organizer profile page — no login required."""
    from .models import TICKETING_TYPE_DIRECT
    org = get_object_or_404(Organization, slug=slug)
    today = django_tz.now().date()
    events = (
        Event.objects.filter(
            organization=org,
            status=EVENT_STATUS_LIVE,
            ticketing_type=TICKETING_TYPE_DIRECT,
            deleted_at__isnull=True,
        )
        .filter(
            Q(end_date__isnull=False, end_date__gte=today) |
            Q(end_date__isnull=True, start_date__gte=today)
        )
        .select_related('venue')
        .order_by('start_date', 'start_time', 'name')
    )
    return render(request, 'tickets/public_org_profile.html', {'org': org, 'events': events})


@login_required
def org_required(request):
    """Shown when user has no organization; prompt to create or join one."""
    return render(request, 'tickets/org_required.html')


@login_required
@require_http_methods(["GET", "POST"])
def create_organization(request):
    """Create a new organization and assign the current user to it."""
    from .forms import OrganizationForm
    if not request.user.is_superuser:
        approved = OrganizerWaitlist.objects.filter(
            email=request.user.email,
            status=OrganizerWaitlist.Status.APPROVED,
        ).exists()
        if not approved:
            messages.info(request, 'Organization creation is currently by invite only. Join the waitlist for early access.')
            return redirect('tickets:become_organizer')

    profile, _ = UserProfile.objects.get_or_create(
        user=request.user,
        defaults={'organization_id': None},
    )
    if request.method == 'POST':
        form = OrganizationForm(request.POST)
        if form.is_valid():
            org = form.save(commit=False)
            base_slug = slugify(org.name)
            slug = base_slug
            counter = 1
            while Organization.objects.filter(slug=slug).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            org.slug = slug
            with transaction.atomic():
                org.save()
                OrganizationMembership.objects.create(
                    user=request.user,
                    organization=org,
                    org_role=UserProfile.OrgRole.OWNER,
                )
                profile.role = UserProfile.Role.ORGANIZER
                profile.org_role = UserProfile.OrgRole.OWNER
                if profile.organization_id is None:
                    profile.organization = org
                profile.save(update_fields=['organization', 'role', 'org_role'])
            clear_org_cache(request)
            request.session['_org_id'] = str(org.pk)
            messages.success(request, f"Organization '{org.name}' created. You can now use the app.")
            return redirect('tickets:home')
    else:
        form = OrganizationForm()
    return render(request, 'tickets/create_organization.html', {'form': form})


@login_required
@require_org
def member_list(request):
    """List organization members and pending invites; show invite form."""
    org = get_organization(request)
    members = (
        OrganizationMembership.objects.filter(organization=org)
        .select_related('user', 'user__profile')
        .order_by('user__email')
    )
    now = django_tz.now()
    pending_invites = (
        OrganizationInvitation.objects.filter(
            organization=org,
            status=OrganizationInvitation.Status.PENDING,
            expires_at__gt=now,
        )
        .select_related('invited_by')
        .order_by('-created_at')
    )
    form = MemberInviteForm()
    context = {
        'members': members,
        'pending_invites': pending_invites,
        'invite_form': form,
        'org_role_choices': UserProfile.OrgRole.choices,
    }
    return render(request, 'tickets/member_list.html', context)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def member_invite(request):
    """Create an organization invitation and send email."""
    from django.contrib.auth.models import User
    org = get_organization(request)
    form = MemberInviteForm(request.POST)
    if not form.is_valid():
        for field, errors in form.errors.items():
            for err in errors:
                messages.error(request, err)
        return redirect('tickets:member_list')

    method = form.cleaned_data.get('invite_method', 'email')
    role = UserProfile.Role.ORGANIZER
    org_role = form.cleaned_data['org_role']

    resolved_email = ''
    resolved_phone = ''

    if method == 'email':
        entered_email = form.cleaned_data['email'].strip().lower()
        matched_user = User.objects.filter(email__iexact=entered_email).first()
        resolved_email = entered_email
        if matched_user is not None:
            profile = UserProfile.objects.filter(user=matched_user).first()
            if profile and profile.phone_number:
                resolved_phone = profile.phone_number
    else:
        entered_phone = form.cleaned_data['phone_number']
        profile = (
            UserProfile.objects
            .select_related('user')
            .filter(phone_number=entered_phone)
            .first()
        )
        if profile is None or not profile.user.email:
            messages.error(
                request,
                'No account found for that phone number. Invite them by email instead.',
            )
            return redirect('tickets:member_list')
        resolved_email = profile.user.email.lower()
        resolved_phone = entered_phone

    if OrganizationMembership.objects.filter(
        organization=org,
        user__email__iexact=resolved_email,
    ).exists():
        messages.error(request, f'{resolved_email} is already a member of this organization.')
        return redirect('tickets:member_list')

    pending_qs = OrganizationInvitation.objects.filter(
        organization=org,
        status=OrganizationInvitation.Status.PENDING,
        expires_at__gt=django_tz.now(),
    )
    if pending_qs.filter(email__iexact=resolved_email).exists():
        messages.error(request, f'An invitation for {resolved_email} is already pending.')
        return redirect('tickets:member_list')
    if resolved_phone and pending_qs.filter(phone_number=resolved_phone).exists():
        messages.error(request, f'An invitation for {resolved_phone} is already pending.')
        return redirect('tickets:member_list')

    expires_at = django_tz.now() + timedelta(days=7)
    invitation = OrganizationInvitation(
        organization=org,
        email=resolved_email,
        phone_number=resolved_phone,
        invited_via=(
            OrganizationInvitation.InvitedVia.PHONE
            if method == 'phone'
            else OrganizationInvitation.InvitedVia.EMAIL
        ),
        invited_by=request.user,
        status=OrganizationInvitation.Status.PENDING,
        expires_at=expires_at,
        role=role,
        org_role=org_role,
    )
    invitation.full_clean()
    invitation.save()

    from .tasks import send_org_invite_email_task
    send_org_invite_email_task.delay(str(invitation.id))
    messages.success(
        request,
        f'Invitation sent to {resolved_email}. They can use the link in the email to join.',
    )
    return redirect('tickets:member_list')


def _maybe_accept_pending_invite(request):
    """If a `pending_invite_token` is in the session, attach the user to that org.

    Called right after a brand-new account is created via the signup flow so an
    invitee who clicked an invite link without an existing account auto-joins.
    Returns the invitation if accepted, else None.
    """
    token = request.session.pop('pending_invite_token', None)
    if not token:
        return None
    try:
        invitation = OrganizationInvitation.objects.get(token=token)
    except (OrganizationInvitation.DoesNotExist, ValueError):
        return None
    if not invitation.is_usable():
        return None
    if not request.user.is_authenticated:
        return None
    if (request.user.email or '').lower() != invitation.email.lower():
        return None
    profile, _ = UserProfile.objects.get_or_create(
        user=request.user,
        defaults={'organization_id': None},
    )
    with transaction.atomic():
        OrganizationMembership.objects.update_or_create(
            user=request.user,
            organization=invitation.organization,
            defaults={'org_role': invitation.org_role},
        )
        if invitation.role == UserProfile.Role.ORGANIZER:
            profile.role = UserProfile.Role.ORGANIZER
        if profile.organization_id is None:
            profile.organization = invitation.organization
            profile.org_role = invitation.org_role
        profile.save(update_fields=['organization', 'role', 'org_role'])
        invitation.status = OrganizationInvitation.Status.ACCEPTED
        invitation.accepted_at = django_tz.now()
        invitation.accepted_by = request.user
        invitation.save(update_fields=['status', 'accepted_at', 'accepted_by'])
    clear_org_cache(request)
    request.session['_org_id'] = str(invitation.organization.pk)
    messages.success(request, f"You've joined {invitation.organization.name}. Welcome!")
    return invitation


@require_http_methods(["GET", "POST"])
def invite_accept(request, token):
    """Accept an organization invitation by token (requires login; redirects if not)."""
    invitation = get_object_or_404(OrganizationInvitation, token=token)
    if not invitation.is_usable():
        return render(request, 'tickets/invite_accept.html', {
            'invitation': invitation,
            'expired_or_used': True,
        })

    if not request.user.is_authenticated:
        from django.contrib.auth.models import User
        from django.urls import reverse
        from urllib.parse import urlencode
        invite_url = request.build_absolute_uri()
        # Stash the token so post-login completion can auto-accept even if the
        # login flow short-circuits past `next=` (e.g. organizer dashboard redirect).
        request.session['pending_invite_token'] = str(invitation.token)
        user_exists = User.objects.filter(email__iexact=invitation.email).exists()
        if user_exists:
            login_url = reverse('tickets:login')
            return redirect(f'{login_url}?{urlencode({"next": invite_url})}')
        # Clicking the tokenized link we emailed to invitation.email already
        # proves the recipient controls that inbox, so skip the email OTP step
        # and drop them straight into profile creation.
        request.session['pending_signup_email'] = invitation.email
        return redirect('tickets:email_complete_profile')

    if request.user.email.lower() != invitation.email.lower():
        return render(request, 'tickets/invite_accept.html', {
            'invitation': invitation,
            'expired_or_used': False,
            'email_mismatch': True,
        })

    profile, _ = UserProfile.objects.get_or_create(
        user=request.user,
        defaults={'organization_id': None},
    )
    with transaction.atomic():
        OrganizationMembership.objects.update_or_create(
            user=request.user,
            organization=invitation.organization,
            defaults={'org_role': invitation.org_role},
        )
        if invitation.role == UserProfile.Role.ORGANIZER:
            profile.role = UserProfile.Role.ORGANIZER
        if profile.organization_id is None:
            profile.organization = invitation.organization
            profile.org_role = invitation.org_role
        profile.save(update_fields=['organization', 'role', 'org_role'])
        invitation.status = OrganizationInvitation.Status.ACCEPTED
        invitation.accepted_at = django_tz.now()
        invitation.accepted_by = request.user
        invitation.save(update_fields=['status', 'accepted_at', 'accepted_by'])
    clear_org_cache(request)
    request.session['_org_id'] = str(invitation.organization.pk)
    messages.success(request, f"You've joined {invitation.organization.name}. Welcome!")
    if profile.is_organizer:
        return redirect('tickets:home')
    return redirect('tickets:attendee_dashboard')


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def invite_revoke(request, token):
    """Revoke a pending organization invitation."""
    org = get_organization(request)
    invitation = get_object_or_404(
        OrganizationInvitation.objects.filter(organization=org),
        token=token,
    )
    if invitation.status != OrganizationInvitation.Status.PENDING:
        messages.info(request, 'That invitation is no longer pending.')
        return redirect('tickets:member_list')
    invitation.status = OrganizationInvitation.Status.REVOKED
    invitation.save(update_fields=['status'])
    messages.success(request, f'Invitation for {invitation.email} has been revoked.')
    return redirect('tickets:member_list')


@login_required
@require_org
@require_organizer
def home(request):
    """Home/dashboard page with overview statistics."""
    org = get_organization(request)

    # Paginate lightweight queryset first, then annotate only the page
    recent_events = (
        Event.objects.filter(organization=org)
        .select_related('venue')
        .order_by('-start_date')
    )
    page_number = request.GET.get('page', 1)
    paginator = Paginator(recent_events, 10)
    page_obj = paginator.get_page(page_number)

    page_pks = [e.pk for e in page_obj.object_list]
    annotated_map = {
        e.pk: e
        for e in _annotate_events(
            Event.objects.filter(pk__in=page_pks)
        ).select_related('venue')
    }
    page_obj.object_list = [annotated_map[pk] for pk in page_pks]

    # Compute net_revenue (after fees for direct events, gross for external)
    for ev in page_obj.object_list:
        if ev.ticketing_type == 'direct':
            fees = Decimal(ev.platform_fees_cents) / Decimal('100')
        else:
            fees = Decimal('0.00')
        # computed_total_revenue = ticket_revenue + additional_income (signal-maintained)
        ev.net_revenue = ev.total_revenue - fees

    # Show warning when current time is past the event's end date+time and upload_count is 0 (current page only)
    now_local = django_tz.localtime(django_tz.now()).replace(tzinfo=None)
    event_ids_show_warning = set()
    event_ids_show_placeholder = set()
    for ev in page_obj:
        if ev.has_uploads:
            continue
        end_date = ev.end_date or ev.start_date
        end_time = ev.end_time or ev.start_time or time(23, 59, 59)
        event_end = datetime.combine(end_date, end_time)
        if now_local > event_end:
            event_ids_show_warning.add(ev.id)
        elif ev.ticketing_type == 'external':
            event_ids_show_placeholder.add(ev.id)

    # Summary statistics (org-scoped via Event/Customer/UploadedFile)
    total_customers = Customer.objects.filter(organization=org).exclude(email__endswith='@placeholder.local').count()
    order_agg = TicketOrder.objects.filter(event__organization=org).aggregate(
        total_orders=Count('id'),
        total_revenue=Coalesce(Sum('total_amount'), Decimal('0.00')),
    )
    additional_agg = EventIncome.objects.filter(
        event__organization=org, deleted_at__isnull=True
    ).aggregate(total=Coalesce(Sum('amount'), Decimal('0.00')))
    direct_fees_agg = StripeCheckoutSession.objects.filter(
        event__organization=org,
        ticket_order__isnull=False,
    ).aggregate(total_fees=Coalesce(Sum('platform_fee_cents'), 0))
    direct_fees = Decimal(direct_fees_agg['total_fees']) / Decimal('100')
    total_orders = order_agg['total_orders']
    total_revenue = order_agg['total_revenue'] + (additional_agg['total'] or Decimal('0.00')) - direct_fees
    total_tickets = Ticket.objects.filter(ticket_order__event__organization=org).count()
    ai_recommendations = (
        AIRecommendation.objects
        .filter(
            organization=org,
            status__in=[AIRecommendation.Status.NEW, AIRecommendation.Status.REVIEWED],
        )
        .select_related('event', 'customer')
        .order_by(
            Case(
                When(priority=AIRecommendation.Priority.HIGH, then=Value(0)),
                When(priority=AIRecommendation.Priority.MEDIUM, then=Value(1)),
                default=Value(2),
                output_field=models.IntegerField(),
            ),
            '-confidence',
            '-created_at',
        )[:3]
    )
    
    context = {
        'page_obj': page_obj,
        'event_ids_show_warning': event_ids_show_warning,
        'event_ids_show_placeholder': event_ids_show_placeholder,
        'today': date.today(),
        'total_customers': total_customers,
        'total_orders': total_orders,
        'total_revenue': total_revenue,
        'total_tickets': total_tickets,
        'ai_recommendations': ai_recommendations,
    }
    return render(request, 'tickets/home.html', context)


@login_required
@require_org
@require_organizer
def action_center(request):
    """Reviewed AI recommendations for the active organization."""
    org = get_organization(request)
    status_filter = request.GET.get('status', '')
    priority_filter = request.GET.get('priority', '')
    kind_filter = request.GET.get('kind', '')

    recommendations = (
        AIRecommendation.objects
        .filter(organization=org)
        .select_related('event', 'customer')
    )
    if status_filter:
        recommendations = recommendations.filter(status=status_filter)
    else:
        recommendations = recommendations.filter(
            status__in=[AIRecommendation.Status.NEW, AIRecommendation.Status.REVIEWED],
        )
    if priority_filter:
        recommendations = recommendations.filter(priority=priority_filter)
    if kind_filter:
        recommendations = recommendations.filter(kind=kind_filter)

    recommendations = recommendations.order_by(
        Case(
            When(priority=AIRecommendation.Priority.HIGH, then=Value(0)),
            When(priority=AIRecommendation.Priority.MEDIUM, then=Value(1)),
            default=Value(2),
            output_field=models.IntegerField(),
        ),
        Case(
            When(status=AIRecommendation.Status.NEW, then=Value(0)),
            When(status=AIRecommendation.Status.REVIEWED, then=Value(1)),
            default=Value(2),
            output_field=models.IntegerField(),
        ),
        '-confidence',
        '-created_at',
    )

    paginator = Paginator(recommendations, 20)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    context = {
        'page_obj': page_obj,
        'status_filter': status_filter,
        'priority_filter': priority_filter,
        'kind_filter': kind_filter,
        'status_choices': AIRecommendation.Status.choices,
        'priority_choices': AIRecommendation.Priority.choices,
        'kind_choices': AIRecommendation.Kind.choices,
    }
    return render(request, 'tickets/action_center.html', context)


def _ai_recommendation_redirect(request):
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('tickets:action_center')
    if url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect('tickets:action_center')


@login_required
@require_org
@require_organizer
@require_http_methods(["POST"])
def ai_recommendation_review(request, recommendation_id):
    org = get_organization(request)
    recommendation = get_object_or_404(
        AIRecommendation.objects.filter(organization=org),
        id=recommendation_id,
    )
    if recommendation.status == AIRecommendation.Status.NEW:
        recommendation.mark_reviewed()
    action = recommendation.recommended_action_json or {}
    url = action.get('url') or reverse('tickets:action_center')
    if url_has_allowed_host_and_scheme(
        url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(url)
    return redirect('tickets:action_center')


@login_required
@require_org
@require_organizer
@require_http_methods(["POST"])
def ai_recommendation_dismiss(request, recommendation_id):
    org = get_organization(request)
    recommendation = get_object_or_404(
        AIRecommendation.objects.filter(organization=org),
        id=recommendation_id,
    )
    recommendation.dismiss()
    messages.success(request, 'Recommendation dismissed.')
    return _ai_recommendation_redirect(request)


@login_required
@require_org
@require_organizer
@require_http_methods(["POST"])
def ai_recommendation_resolve(request, recommendation_id):
    org = get_organization(request)
    recommendation = get_object_or_404(
        AIRecommendation.objects.filter(organization=org),
        id=recommendation_id,
    )
    recommendation.resolve()
    messages.success(request, 'Recommendation marked resolved.')
    return _ai_recommendation_redirect(request)


@login_required
@require_org
@require_host
@require_http_methods(["GET", "POST"])
def price_entry(request, file_id):
    """Display form for manually entering ticket prices or tiers."""
    org = get_organization(request)
    uploaded_file = get_object_or_404(UploadedFile.objects.filter(organization=org), id=file_id)
    uses_tiers = uploaded_file.csv_format.uses_tiers
    
    if request.method == 'POST':
        # Extract unique ticket types from CSV
        if uploaded_file.csv_file:
            _post_file_handle = uploaded_file.csv_file.open('rb')
        else:
            _legacy_path = os.path.join('media', uploaded_file.metadata.get('file_path', ''))
            if not os.path.exists(_legacy_path):
                messages.error(request, "CSV file not found.")
                return redirect('tickets:event_list')
            _post_file_handle = open(_legacy_path, 'rb')

        import pandas as pd
        df = pd.read_csv(_post_file_handle, dtype=str, keep_default_na=False)
        _post_file_handle.close()
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
        if uploaded_file.csv_file:
            _get_file_handle = uploaded_file.csv_file.open('rb')
        else:
            _legacy_path = os.path.join('media', uploaded_file.metadata.get('file_path', ''))
            if not os.path.exists(_legacy_path):
                messages.error(request, "CSV file not found.")
                return redirect('tickets:event_list')
            _get_file_handle = open(_legacy_path, 'rb')

        import pandas as pd
        df = pd.read_csv(_get_file_handle, dtype=str, keep_default_na=False)
        _get_file_handle.close()
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
    """Queue CSV processing as a Celery task and redirect to results page."""
    from tickets.tasks import process_csv_task

    # Serialize Decimal values to strings for JSON-safe task transport
    serialized_prices = (
        {k: str(v) for k, v in manual_prices.items()} if manual_prices else None
    )
    serialized_tiers = None
    if tier_definitions:
        serialized_tiers = {}
        for ticket_type, tiers in tier_definitions.items():
            serialized_tiers[ticket_type] = [
                {**t, 'price': str(t['price']) if t.get('price') is not None else None}
                for t in tiers
            ]

    process_csv_task.delay(
        str(uploaded_file.id),
        manual_prices=serialized_prices,
        tier_definitions=serialized_tiers,
    )
    return redirect('tickets:upload_results', file_id=uploaded_file.id)


@login_required
@require_org
@require_host
@require_http_methods(["GET", "POST"])
def reprocess_csv_file(request, file_id):
    """Delete all orders from an upload and re-run the CSV processor with current format settings."""
    org = get_organization(request)
    uploaded_file = get_object_or_404(UploadedFile.objects.filter(organization=org), id=file_id)

    if not uploaded_file.csv_file:
        messages.error(request, "No stored file available to re-process. Re-processing requires the original CSV file.")
        return redirect('tickets:upload_results', file_id=uploaded_file.id)

    if request.method == 'POST':
        event_ids = list(
            TicketOrder.objects.filter(uploaded_file=uploaded_file)
            .values_list('event_id', flat=True)
            .distinct()
        )
        TicketOrder.objects.filter(uploaded_file=uploaded_file).delete()
        uploaded_file.status = 'pending'
        uploaded_file.metadata.pop('processing_results', None)
        uploaded_file.save(update_fields=['status', 'metadata'])
        for event_id in event_ids:
            _invalidate_event_upload_stats_cache(event_id)
        return process_csv_file(request, uploaded_file)

    order_count = TicketOrder.objects.filter(uploaded_file=uploaded_file).count()
    return render(request, 'tickets/reprocess_confirm.html', {
        'uploaded_file': uploaded_file,
        'order_count': order_count,
    })


@login_required
@require_org
def upload_status_api(request, file_id):
    """JSON endpoint returning current processing status for a given upload."""
    org = get_organization(request)
    uploaded_file = get_object_or_404(UploadedFile.objects.filter(organization=org), id=file_id)
    return JsonResponse({
        'status': uploaded_file.status,
        'processed_rows': uploaded_file.processed_rows,
        'total_rows': uploaded_file.total_rows,
        'processing_results': uploaded_file.metadata.get('processing_results'),
    })


@login_required
@require_org
@require_host
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
@require_admin
@require_http_methods(["POST"])
def upload_delete(request, file_id):
    """Delete an upload and all associated order data."""
    org = get_organization(request)
    uploaded_file = get_object_or_404(UploadedFile.objects.filter(organization=org), id=file_id)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    # Block deletion if status is 'processing'
    if uploaded_file.status == 'processing':
        error_msg = "Cannot delete upload while it is processing. Please wait for processing to complete."
        if is_ajax:
            return JsonResponse({'success': False, 'error': error_msg}, status=400)
        messages.error(request, error_msg)
        return redirect('tickets:home')

    try:
        with transaction.atomic():
            # Get all orders associated with this upload
            orders = uploaded_file.ticket_orders.all()
            orders_count = orders.count()
            affected_event_ids = list(orders.values_list('event_id', flat=True).distinct())

            # Collect affected customers before deletion
            affected_customer_ids = list(
                orders.values_list('customer_id', flat=True).distinct()
            )

            # Delete orders first (Tickets will cascade delete)
            orders.delete()

            # Delete the upload file (TicketTiers will cascade delete)
            filename = uploaded_file.filename
            uploaded_file.hard_delete()

            for event_id in affected_event_ids:
                _invalidate_event_upload_stats_cache(event_id)

            customers_deleted = _reconcile_customers_after_order_deletion(
                org,
                affected_customer_ids,
            )

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
@require_host
def customer_list(request):
    """Display list of all customers with LTV and optional segment/tag filter."""
    org = get_organization(request)
    customers = Customer.objects.filter(organization=org).exclude(email__endswith='@placeholder.local')

    # Segment filter
    segment_filter = request.GET.get('segment', '').strip()
    if segment_filter:
        customers = customers.filter(rfm_segment=segment_filter)

    # Tag filter — validate UUID to avoid ValueError on bad input
    tag_filter = request.GET.get('tag', '').strip()
    if tag_filter:
        try:
            _uuid.UUID(tag_filter)
            customers = customers.filter(tags__id=tag_filter)
        except ValueError:
            tag_filter = ''

    # Search
    search_query = request.GET.get('search', '')
    if search_query:
        customers = customers.filter(
            name__icontains=search_query
        ) | customers.filter(
            email__icontains=search_query
        )

    customers = customers.annotate(order_count=Count('ticket_orders'))

    # Sorting
    sort_by = request.GET.get('sort', '-lifetime_value')
    if sort_by in ['name', 'email', 'lifetime_value', 'last_order_date']:
        customers = customers.order_by(sort_by)
    elif sort_by == '-lifetime_value':
        customers = customers.order_by('-lifetime_value')

    # prefetch_related must go AFTER the OR search chain to avoid Django dropping it
    customers = customers.prefetch_related('tags')

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

    org_tags = CustomerTag.objects.filter(organization=org)
    context = {
        'page_obj': page_obj,
        'search_query': search_query,
        'sort_by': sort_by,
        'segment_filter': segment_filter,
        'tag_filter': tag_filter,
        'segment_choices': segment_choices,
        'segment_badge_colors': SEGMENT_BADGE_COLORS,
        'current_segment_definition': current_segment_definition,
        'org_tags': org_tags,
    }
    return render(request, 'tickets/customer_list.html', context)


@login_required
@require_org
@require_host
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
        order_count = row['order_count'] or 0
        avg_orders = round(order_count / customer_count, 1) if customer_count else 0
        market_stats.append({
            'city': city.strip() or '-',
            'total_ltv': total_ltv,
            'order_count': order_count,
            'customer_count': customer_count,
            'avg_ltv': avg_ltv,
            'avg_orders': avg_orders,
        })

    chart_data = [
        {
            'city': row['city'],
            'total_ltv': float(row['total_ltv']),
            'avg_ltv': float(row['avg_ltv']),
            'avg_orders': row['avg_orders'],
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


def _parse_churn_days(request):
    """Return a validated churn threshold from the query string."""
    raw_days = request.GET.get('days', '').strip()
    try:
        days = int(raw_days or 90)
    except (TypeError, ValueError):
        days = 90
    if days not in THRESHOLD_OPTIONS:
        days = 90
    return days


def _normalized_customer_group_stats(org, field_name, ordered_labels, badge_colors):
    value_expr = f'{field_name}'
    customer_rows = (
        Customer.objects.filter(organization=org)
        .exclude(email__endswith='@placeholder.local')
        .annotate(
            group_name=Case(
                When(**{f'{value_expr}': ''}, then=Value('Dormant')),
                When(**{f'{value_expr}__isnull': True}, then=Value('Dormant')),
                default=F(value_expr),
                output_field=CharField(),
            )
        )
        .values('group_name')
        .annotate(
            count=Count('id'),
            total_ltv=Sum('lifetime_value'),
            avg_gap=Avg('avg_days_between_orders'),
        )
    )
    order_rows = (
        TicketOrder.objects.filter(customer__organization=org, is_in_person=False)
        .annotate(
            group_name=Case(
                When(**{f'customer__{value_expr}': ''}, then=Value('Dormant')),
                When(**{f'customer__{value_expr}__isnull': True}, then=Value('Dormant')),
                default=F(f'customer__{value_expr}'),
                output_field=CharField(),
            )
        )
        .values('group_name')
        .annotate(total_orders=Count('id'))
    )

    customer_map = {row['group_name'] or 'Dormant': row for row in customer_rows}
    order_map = {row['group_name'] or 'Dormant': row['total_orders'] for row in order_rows}
    total_customers = sum(row['count'] for row in customer_map.values())
    stats = []
    for name in ordered_labels:
        row = customer_map.get(name, {'count': 0, 'total_ltv': Decimal('0'), 'avg_gap': None})
        count = row['count']
        total_ltv = row.get('total_ltv') or Decimal('0')
        total_orders = order_map.get(name, 0)
        avg_gap = row.get('avg_gap')
        stats.append({
            'segment': name,
            'count': count,
            'pct': round((100.0 * count / total_customers), 1) if total_customers else 0,
            'avg_ltv': (total_ltv / count) if count else Decimal('0'),
            'avg_orders': round((total_orders / count), 1) if count else 0,
            'avg_gap': round(avg_gap, 1) if avg_gap is not None else None,
            'badge_color': badge_colors.get(name, 'secondary'),
        })
    return stats, total_customers


@login_required
@require_org
@require_host
def analytics_overview(request):
    """Hub for organizer analytics destinations."""
    get_organization(request)
    return render(request, 'tickets/analytics_overview.html')


def _marketing_cache_key(org_id, window):
    try:
        version = django_cache.get(f'marketing_overview_ver:{org_id}', 0)
    except Exception:
        version = 0
    return f'marketing_overview:{version}:{org_id}:{window}'


def _invalidate_marketing_cache(org):
    if org is None:
        return
    key = f'marketing_overview_ver:{org.pk}'
    try:
        django_cache.incr(key)
    except ValueError:
        try:
            django_cache.set(key, 1, timeout=None)
        except Exception:
            pass
    except Exception:
        pass
    safe_cache_delete(f'marketing_ai:{org.pk}:30')
    safe_cache_delete(f'marketing_ai:{org.pk}:90')
    safe_cache_delete(f'marketing_ai:{org.pk}:365')
    safe_cache_delete(f'marketing_ai:{org.pk}:all')


@login_required
@require_org
@require_host
def marketing_overview(request):
    """Org-wide marketing performance dashboard with AI recommendations."""
    org = get_organization(request)
    window_key, window_days, window_label = resolve_window(request.GET.get('window', DEFAULT_WINDOW))
    allowed_tabs = {'overview', 'email', 'sms', 'ads'}
    active_tab = request.GET.get('tab', 'overview').lower()
    if active_tab not in allowed_tabs:
        active_tab = 'overview'

    cache_key = _marketing_cache_key(org.pk, window_key)
    metrics = safe_cache_get(cache_key)
    if metrics is None:
        metrics = MarketingAnalyticsService(org, window_days).calculate()
        safe_cache_set(cache_key, metrics, timeout=600)

    recommendations = (
        AIRecommendation.objects
        .filter(
            organization=org,
            kind=AIRecommendation.Kind.MARKETING_ATTRIBUTION,
            status__in=[AIRecommendation.Status.NEW, AIRecommendation.Status.REVIEWED],
        )
        .select_related('event')
        .order_by(
            Case(
                When(priority=AIRecommendation.Priority.HIGH, then=Value(0)),
                When(priority=AIRecommendation.Priority.MEDIUM, then=Value(1)),
                default=Value(2),
                output_field=models.IntegerField(),
            ),
            '-confidence',
            '-created_at',
        )[:20]
    )

    trend_chart = {
        'labels': [row['month'] for row in metrics['trends']],
        'email_revenue': [float(row['email_revenue']) for row in metrics['trends']],
        'sms_revenue': [float(row['sms_revenue']) for row in metrics['trends']],
        'ads_spend': [float(row['ads_spend']) for row in metrics['trends']],
    }
    engagement_chart = {
        'labels': [row['month'] for row in metrics['engagement_trends']],
        'email_opens': [row['email_opens'] for row in metrics['engagement_trends']],
        'email_clicks': [row['email_clicks'] for row in metrics['engagement_trends']],
        'sms_clicks': [row['sms_clicks'] for row in metrics['engagement_trends']],
    }

    context = {
        'metrics': metrics,
        'recommendations': recommendations,
        'window_choices': MARKETING_WINDOW_CHOICES,
        'window_key': window_key,
        'window_label': window_label,
        'active_tab': active_tab,
        'trend_chart_json': json.dumps(trend_chart),
        'engagement_chart_json': json.dumps(engagement_chart),
    }
    return render(request, 'tickets/marketing_overview.html', context)


@login_required
@require_org
@require_host
@require_http_methods(["POST"])
def marketing_ai_analyze(request):
    """On-demand: ask the configured LLM for narrative insights about the current window."""
    org = get_organization(request)
    window_key, window_days, window_label = resolve_window(request.POST.get('window', DEFAULT_WINDOW))

    ai_cache_key = f'marketing_ai:{org.pk}:{window_key}'
    cached = safe_cache_get(ai_cache_key)
    if cached is not None:
        return JsonResponse({**cached, 'cached': True})

    metrics_key = _marketing_cache_key(org.pk, window_key)
    metrics = safe_cache_get(metrics_key)
    if metrics is None:
        metrics = MarketingAnalyticsService(org, window_days).calculate()
        safe_cache_set(metrics_key, metrics, timeout=600)

    try:
        result = generate_marketing_narrative(org, metrics, window_label)
    except Exception:
        logger.exception('Marketing narrative generation failed for org %s', org.pk)
        return JsonResponse(
            {'error': 'Could not generate insights right now. Please try again in a moment.'},
            status=502,
        )

    safe_cache_set(ai_cache_key, result, timeout=600)
    return JsonResponse({**result, 'cached': False})


@login_required
@require_org
@require_host
def customer_segments(request):
    """Analytics page for RFM segments and purchase-pattern behavior profiles."""
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

    segment_stats, total_customers = _normalized_customer_group_stats(
        org,
        'rfm_segment',
        segment_order,
        SEGMENT_BADGE_COLORS,
    )
    segment_stats_json = json.dumps([
        {'segment': s['segment'], 'count': s['count'], 'avg_ltv': float(s['avg_ltv'])}
        for s in segment_stats
    ])
    behavior_stats, _ = _normalized_customer_group_stats(
        org,
        'behavior_profile',
        BEHAVIOR_PROFILE_ORDER,
        BEHAVIOR_PROFILE_BADGE_COLORS,
    )
    behavior_stats_json = json.dumps([
        {
            'segment': s['segment'],
            'count': s['count'],
            'avg_ltv': float(s['avg_ltv']),
            'avg_gap': s['avg_gap'],
        }
        for s in behavior_stats
    ])
    behavior_definitions = [
        {
            'segment': name,
            'description': BEHAVIOR_PROFILE_DESCRIPTIONS.get(name, ''),
            'badge_color': BEHAVIOR_PROFILE_BADGE_COLORS.get(name, 'secondary'),
        }
        for name in BEHAVIOR_PROFILE_ORDER
    ]
    context = {
        'segment_stats': segment_stats,
        'segment_stats_json': segment_stats_json,
        'segment_definitions': segment_definitions,
        'behavior_stats': behavior_stats,
        'behavior_stats_json': behavior_stats_json,
        'behavior_definitions': behavior_definitions,
        'total_customers': total_customers,
        'rfm_recalc_in_progress': org.rfm_recalc_in_progress,
    }
    return render(request, 'tickets/customer_segments.html', context)


@login_required
@require_org
@require_host
def churn_overview(request):
    """Analytics page for churned customers and win-back tagging."""
    org = get_organization(request)
    days = _parse_churn_days(request)
    result = ChurnDetectionService(org).calculate(days_threshold=days)

    paginator = Paginator(result['customers'], 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    segment_breakdown = []
    for row in result['stats']['segment_breakdown']:
        segment = row['seg'] or 'Dormant'
        segment_breakdown.append({
            'segment': segment,
            'count': row['count'],
            'badge_color': SEGMENT_BADGE_COLORS.get(segment, 'secondary'),
            'description': SEGMENT_DESCRIPTIONS.get(segment, ''),
        })

    context = {
        'page_obj': page_obj,
        'stats': result['stats'],
        'days': days,
        'threshold_options': THRESHOLD_OPTIONS,
        'org_tags': CustomerTag.objects.filter(organization=org),
        'segment_breakdown': segment_breakdown,
        'segment_breakdown_json': json.dumps([
            {'segment': row['segment'], 'count': row['count']}
            for row in segment_breakdown
        ]),
        'segment_badge_colors': SEGMENT_BADGE_COLORS,
    }
    return render(request, 'tickets/churn_overview.html', context)


@login_required
@require_org
@require_host
@require_http_methods(['POST'])
def churn_bulk_tag(request):
    """Apply an existing org tag to the selected churned customers."""
    org = get_organization(request)
    tag_id = request.POST.get('tag_id', '').strip()
    days = request.POST.get('days', '').strip()
    customer_ids = request.POST.getlist('customer_ids')

    try:
        _uuid.UUID(tag_id)
    except ValueError:
        messages.error(request, 'Select a valid tag.')
        return redirect(f"{reverse('tickets:churn_overview')}?days={_parse_churn_days(request)}")

    try:
        redirect_days = int(days or 90)
    except (TypeError, ValueError):
        redirect_days = 90
    if redirect_days not in THRESHOLD_OPTIONS:
        redirect_days = 90

    tag = get_object_or_404(CustomerTag.objects.filter(organization=org), id=tag_id)
    customers = Customer.objects.filter(organization=org, id__in=customer_ids)

    tagged_count = customers.count()
    for customer in customers:
        customer.tags.add(tag)

    messages.success(request, f'Tagged {tagged_count} customers as "{tag.name}".')
    return redirect(f"{reverse('tickets:churn_overview')}?days={redirect_days}")


@login_required
@require_org
@require_host
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
@require_host
def repeat_customers(request):
    """Analytics page: new vs returning customers per event."""
    org = get_organization(request)
    start_date, end_date, active_window = _parse_window(request)
    calculator = RepeatCustomerCalculator(org)
    result = calculator.calculate()
    # Chart: left to right earliest → most recent (calculator order)
    chart_events = result['events']

    if start_date:
        end = end_date or date.today()
        chart_events = [
            e for e in chart_events
            if start_date <= date.fromisoformat(str(e['event_date'])[:10]) <= end
        ]
        total = sum(e['total'] for e in chart_events)
        new_count = sum(e['new_count'] for e in chart_events)
        ret_count = sum(e['returning_count'] for e in chart_events)
        summary = {
            'total_attendees': total,
            'new_count': new_count,
            'returning_count': ret_count,
            'new_pct': round(new_count / total * 100, 1) if total else 0,
            'returning_pct': round(ret_count / total * 100, 1) if total else 0,
        }
    else:
        summary = result['summary']

    # Build market (venue city) aggregation from already-filtered events
    markets = {}
    for e in chart_events:
        city = e.get('venue_city') or 'Unknown'
        m = markets.setdefault(city, {'city': city, 'total': 0, 'new_count': 0, 'returning_count': 0})
        m['total'] += e['total']
        m['new_count'] += e['new_count']
        m['returning_count'] += e['returning_count']
    for m in markets.values():
        m['returning_pct'] = round(m['returning_count'] / m['total'] * 100, 1) if m['total'] else 0
    market_data = sorted(markets.values(), key=lambda x: x['total'], reverse=True)

    # Monthly aggregation for chart (bucket events by calendar month)
    month_buckets = {}
    for e in chart_events:
        key = str(e['event_date'])[:7]   # "YYYY-MM"
        m = month_buckets.setdefault(key, {'month': key, 'new_count': 0, 'returning_count': 0, 'total': 0})
        m['new_count'] += e['new_count']
        m['returning_count'] += e['returning_count']
        m['total'] += e['total']
    for m in month_buckets.values():
        m['returning_pct'] = round(m['returning_count'] / m['total'] * 100, 1) if m['total'] else 0
    monthly_chart = sorted(month_buckets.values(), key=lambda x: x['month'])

    monthly_chart_data_json = json.dumps(monthly_chart, default=str)
    market_chart_data = json.dumps(market_data, default=str)

    # Per-event chart data for By Event toggle
    event_chart_data = []
    for e in chart_events:
        event_date_str = str(e['event_date'])[:10]
        try:
            ed = date.fromisoformat(event_date_str)
            date_label = ed.strftime('%b %d')
        except Exception:
            date_label = event_date_str
        total = e['total']
        event_chart_data.append({
            'label': '{} ({})'.format(e['event_name'], date_label),
            'new_count': e['new_count'],
            'returning_count': e['returning_count'],
            'total': total,
            'returning_pct': round(e['returning_count'] / total * 100, 1) if total else 0,
        })
    event_chart_data_json = json.dumps(event_chart_data, default=str)

    # Table: top to bottom most recent → earliest
    table_events = list(reversed(chart_events))
    return render(request, 'tickets/repeat_customers.html', {
        'events': table_events,
        'summary': summary,
        'monthly_chart_data_json': monthly_chart_data_json,
        'event_chart_data_json': event_chart_data_json,
        'market_chart_data_json': market_chart_data,
        'active_window': active_window,
        'window_start': start_date or '',
        'window_end': end_date or '',
        'window_choices': WINDOW_CHOICES,
    })


@login_required
@require_org
@require_host
def cohort_retention(request):
    """Analytics page: monthly cohort retention heatmap and line chart."""
    org = get_organization(request)
    start_date, end_date, active_window = _parse_window(request)
    calculator = CohortRetentionCalculator(org)
    result = calculator.calculate()
    cohorts = result['cohorts']

    if start_date:
        start_m = start_date.strftime('%Y-%m')
        end_m = (end_date or date.today()).strftime('%Y-%m')
        cohorts = [c for c in cohorts if start_m <= c['cohort'] <= end_m]

    if cohorts:
        m1_vals = [c['periods'][1]['retention_pct'] for c in cohorts if len(c['periods']) > 1]
        m3_vals = [c['periods'][3]['retention_pct'] for c in cohorts if len(c['periods']) > 3]
        summary = {
            'total_cohorts': len(cohorts),
            'avg_m1_retention': round(sum(m1_vals) / len(m1_vals), 1) if m1_vals else 0,
            'avg_m3_retention': round(sum(m3_vals) / len(m3_vals), 1) if m3_vals else 0,
        }
    else:
        summary = {'total_cohorts': 0, 'avg_m1_retention': 0, 'avg_m3_retention': 0}

    chart_data = json.dumps(cohorts, default=str)
    max_periods = max(len(c['periods']) for c in cohorts) if cohorts else 0
    return render(request, 'tickets/cohort_retention.html', {
        'cohorts': cohorts,
        'summary': summary,
        'max_periods': range(max_periods),
        'chart_data_json': chart_data,
        'active_window': active_window,
        'window_start': start_date or '',
        'window_end': end_date or '',
        'window_choices': WINDOW_CHOICES,
    })


@login_required
@require_org
@require_host
def customer_detail(request, customer_id):
    """Display detailed customer information with LTV and order history."""
    org = get_organization(request)
    customer = get_object_or_404(Customer.objects.filter(organization=org), id=customer_id)
    
    # Subquery for platform fee — zero for non-direct (CSV) orders
    _fee_subq = Subquery(
        StripeCheckoutSession.objects.filter(ticket_order=OuterRef('pk'))
        .values('platform_fee_cents')[:1],
        output_field=DecimalField(max_digits=10, decimal_places=2),
    )
    _net_amount = ExpressionWrapper(
        F('total_amount') - Cast(
            Coalesce(_fee_subq, 0),
            output_field=DecimalField(max_digits=10, decimal_places=2),
        ) * Decimal('0.01'),
        output_field=DecimalField(max_digits=10, decimal_places=2),
    )

    # Order statistics — avg uses net amount (after platform fees) to match LTV
    order_stats = customer.ticket_orders.annotate(net_amount=_net_amount).aggregate(
        total_orders=Count('id'),
        avg_order_value=Coalesce(Avg('net_amount'), Decimal('0.00')),
        last_order_date=Max('order_date'),
    )
    total_orders = order_stats['total_orders']
    avg_order_value = order_stats['avg_order_value']
    last_order_date = order_stats['last_order_date']
    total_tickets = Ticket.objects.filter(ticket_order__customer=customer).count()

    # Event attendance - select_related to avoid N+1 on venue in template
    events_attended = Event.objects.filter(
        ticket_orders__customer=customer
    ).select_related('venue').distinct()

    # Paginate orders — annotate net_amount so the template shows post-fee totals
    orders = customer.ticket_orders.select_related('event').annotate(
        tickets_count=Count('tickets'),
        net_amount=_net_amount,
    ).order_by('-order_date')
    paginator = Paginator(orders, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    segment_badge_color = SEGMENT_BADGE_COLORS.get(
        (customer.rfm_segment or '').strip(), 'secondary'
    )
    behavior_profile_badge_color = BEHAVIOR_PROFILE_BADGE_COLORS.get(
        (customer.behavior_profile or '').strip(), 'secondary'
    )
    rfm_recency_label = RFM_RECENCY_LABELS.get(customer.rfm_recency_score)
    rfm_frequency_label = RFM_FREQUENCY_LABELS.get(customer.rfm_frequency_score)
    rfm_monetary_label = RFM_MONETARY_LABELS.get(customer.rfm_monetary_score)

    # Tags: current tags + available org tags not yet assigned
    assigned_tags = customer.tags.all()
    assigned_tag_ids = set(assigned_tags.values_list('id', flat=True))
    org_tags = CustomerTag.objects.filter(organization=org)
    available_tags = [t for t in org_tags if t.id not in assigned_tag_ids]

    context = {
        'customer': customer,
        'total_orders': total_orders,
        'total_tickets': total_tickets,
        'avg_order_value': avg_order_value,
        'last_order_date': last_order_date,
        'events_attended': events_attended,
        'page_obj': page_obj,
        'segment_badge_color': segment_badge_color,
        'behavior_profile_badge_color': behavior_profile_badge_color,
        'rfm_recency_label': rfm_recency_label,
        'rfm_frequency_label': rfm_frequency_label,
        'rfm_monetary_label': rfm_monetary_label,
        'behavior_metric_labels': BEHAVIOR_METRIC_LABELS,
        'assigned_tags': assigned_tags,
        'available_tags': available_tags,
        'org_tags': org_tags,
    }
    return render(request, 'tickets/customer_detail.html', context)


# Customer Tag Views

TAG_COLOR_CLASSES = {
    'blue': 'bg-primary',
    'green': 'bg-success',
    'red': 'bg-danger',
    'yellow': 'bg-warning text-dark',
    'orange': 'bg-warning text-dark',
    'purple': 'badge-purple',
}


@login_required
@require_org
@require_host
def customer_tag_list(request):
    """List all org tags with customer counts."""
    org = get_organization(request)
    tags = CustomerTag.objects.filter(organization=org).annotate(
        customer_count=Count('customers')
    )
    context = {
        'tags': tags,
        'tag_color_classes': TAG_COLOR_CLASSES,
    }
    return render(request, 'tickets/customer_tags.html', context)


@login_required
@require_org
@require_host
@require_http_methods(['POST'])
def customer_tag_create(request):
    """Create a new tag for the org."""
    org = get_organization(request)
    name = request.POST.get('name', '').strip()
    color = request.POST.get('color', 'blue')

    valid_colors = [c[0] for c in CustomerTag._meta.get_field('color').choices]
    if color not in valid_colors:
        color = 'blue'

    if not name:
        messages.error(request, 'Tag name is required.')
        return redirect('tickets:customer_tag_list')

    if len(name) > 50:
        messages.error(request, 'Tag name must be 50 characters or fewer.')
        return redirect('tickets:customer_tag_list')

    if CustomerTag.objects.filter(organization=org, name__iexact=name).exists():
        messages.error(request, f'A tag named "{name}" already exists.')
        return redirect('tickets:customer_tag_list')

    CustomerTag.objects.create(organization=org, name=name, color=color)
    messages.success(request, f'Tag "{name}" created.')
    return redirect('tickets:customer_tag_list')


@login_required
@require_org
@require_host
@require_http_methods(['POST'])
def customer_tag_delete(request, tag_id):
    """Delete a tag (removes it from all customers via M2M cascade)."""
    org = get_organization(request)
    tag = get_object_or_404(CustomerTag.objects.filter(organization=org), id=tag_id)
    name = tag.name
    tag.delete()
    messages.success(request, f'Tag "{name}" deleted.')
    return redirect('tickets:customer_tag_list')


@login_required
@require_org
@require_host
@require_http_methods(['POST'])
def customer_tag_add(request, customer_id):
    """Add a tag to a customer. Validates org ownership. Idempotent."""
    org = get_organization(request)
    customer = get_object_or_404(Customer.objects.filter(organization=org), id=customer_id)
    tag_id = request.POST.get('tag_id', '').strip()

    try:
        _uuid.UUID(tag_id)
    except ValueError:
        return redirect('tickets:customer_detail', customer_id=customer_id)

    tag = get_object_or_404(CustomerTag.objects.filter(organization=org), id=tag_id)
    customer.tags.add(tag)
    return redirect('tickets:customer_detail', customer_id=customer_id)


@login_required
@require_org
@require_host
@require_http_methods(['POST'])
def customer_tag_remove(request, customer_id, tag_id):
    """Remove a tag from a customer. Validates org ownership."""
    org = get_organization(request)
    customer = get_object_or_404(Customer.objects.filter(organization=org), id=customer_id)
    tag = get_object_or_404(CustomerTag.objects.filter(organization=org), id=tag_id)
    customer.tags.remove(tag)
    return redirect('tickets:customer_detail', customer_id=customer_id)


# Event Management Views

@login_required
@require_org
@require_organizer
def event_list(request):
    """Display list of all events ordered by most recent start date."""
    org = get_organization(request)

    search_query = request.GET.get('search', '')
    page_number = request.GET.get('page', '1')
    status_filter = request.GET.get('status', 'all')
    if status_filter not in ('all', 'live', 'ended', 'upcoming'):
        status_filter = 'all'

    # Include outstanding actions count in the cache key so the sidebar badge
    # stays in sync (the rendered HTML embeds the count via the context processor).
    try:
        actions_count = AIRecommendation.objects.filter(
            organization=org,
            status__in=[AIRecommendation.Status.NEW, AIRecommendation.Status.REVIEWED],
        ).count()
    except Exception:
        actions_count = 0

    # Check cache first (skip gracefully when Redis is unavailable)
    cache_key = _event_list_cache_key(org.pk, search_query, '', page_number, status_filter, actions_count)
    try:
        cached = django_cache.get(cache_key)
    except Exception:
        cached = None
    if cached is not None:
        return HttpResponse(cached, content_type='text/html')

    base_qs = Event.objects.filter(organization=org).select_related('venue')

    # Search functionality
    if search_query:
        base_qs = base_qs.filter(
            Q(name__icontains=search_query) |
            Q(venue__name__icontains=search_query) |
            Q(venue__city__icontains=search_query)
        )

    # Status filter: All / Live / Ended (matches effective_status logic)
    today = django_tz.now().date()
    if status_filter == 'live':
        base_qs = base_qs.annotate(
            effective_end_date=Coalesce(F('end_date'), F('start_date'))
        ).filter(
            Q(ticketing_type='direct', status=EVENT_STATUS_LIVE) & Q(effective_end_date__gte=today)
        )
    elif status_filter == 'ended':
        base_qs = base_qs.annotate(
            effective_end_date=Coalesce(F('end_date'), F('start_date'))
        ).filter(
            Q(status__in=[EVENT_STATUS_ENDED, EVENT_STATUS_CANCELLED]) |
            (Q(ticketing_type='direct', status=EVENT_STATUS_LIVE) & Q(effective_end_date__lt=today)) |
            (Q(ticketing_type='external') & Q(effective_end_date__lt=today))
        )
    elif status_filter == 'upcoming':
        base_qs = base_qs.filter(start_date__gte=today)

    # Always sort by most recent start date. Paginate first, then annotate only the page.
    events = base_qs.order_by('-start_date')
    paginator = Paginator(events, 25)
    page_obj = paginator.get_page(page_number)

    page_pks = [e.pk for e in page_obj.object_list]
    annotated_map = {
        e.pk: e
        for e in _annotate_events(
            Event.objects.filter(pk__in=page_pks)
        ).select_related('venue')
    }
    page_obj.object_list = [annotated_map[pk] for pk in page_pks]

    # Compute net_revenue for each event on this page.
    # Fees apply to direct ticketing events only; external/CSV events are shown at gross.
    for ev in page_obj.object_list:
        if ev.ticketing_type == 'direct':
            fees = Decimal(ev.platform_fees_cents) / Decimal('100')
        else:
            fees = Decimal('0.00')
        ev.net_revenue = ev.total_revenue - fees

    # Show warning when current time is past the event's end date+time and upload_count is 0
    now_local = django_tz.localtime(django_tz.now()).replace(tzinfo=None)
    event_ids_show_warning = set()
    event_ids_show_placeholder = set()
    for ev in page_obj:
        if ev.has_uploads:
            continue
        end_date = ev.end_date or ev.start_date
        end_time = ev.end_time or ev.start_time or time(23, 59, 59)
        event_end = datetime.combine(end_date, end_time)
        if now_local > event_end:
            event_ids_show_warning.add(ev.id)
        elif ev.ticketing_type == 'external':
            event_ids_show_placeholder.add(ev.id)

    context = {
        'page_obj': page_obj,
        'search_query': search_query,
        'status_filter': status_filter,
        'event_ids_show_warning': event_ids_show_warning,
        'event_ids_show_placeholder': event_ids_show_placeholder,
    }
    response = render(request, 'tickets/event_list.html', context)
    try:
        django_cache.set(cache_key, response.content, 300)
    except Exception:
        pass
    return response


@login_required
@require_org
@require_organizer
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


def _compute_event_stats(event):
    """Compute financial, attendance, and survey stats for an event.

    Results are cached under _event_stats_cache_key(event.pk) for 300s.
    Invalidated by signals in signals.py and call-site invalidation in
    send_survey() and survey_event_link() (which use bulk_create/queryset.update
    that bypass signals).
    """
    cache_key = _event_stats_cache_key(event.pk)
    cached = safe_cache_get(cache_key)
    if isinstance(cached, dict) and EVENT_STATS_REQUIRED_KEYS.issubset(cached):
        return cached

    # Core order stats — total_customers excludes in-person placeholder customers
    # to match the customers list page which excludes @placeholder.local emails.
    event_stats = event.ticket_orders.aggregate(
        total_orders=Count('id'),
        total_revenue=Coalesce(Sum('total_amount'), Decimal('0.00')),
        total_customers=Count('customer', filter=Q(is_in_person=False), distinct=True),
    )
    total_orders = event_stats['total_orders']
    ticket_revenue = event_stats['total_revenue']
    total_customers = event_stats['total_customers']

    # New vs returning customers (online orders only).
    # A customer is "returning" if they have any online order at a *different* event
    # in this org. Using event exclusion rather than date comparison avoids fragile
    # datetime equality and guarantees returning=0 for an org's first event.
    if total_customers > 0:
        online_customer_ids = set(
            TicketOrder.objects.filter(event=event, is_in_person=False)
            .values_list('customer_id', flat=True)
            .distinct()
        )
        returning_customer_ids = set(
            TicketOrder.objects.filter(
                customer_id__in=online_customer_ids,
                customer__organization=event.organization,
                is_in_person=False,
            )
            .exclude(event=event)
            .values_list('customer_id', flat=True)
            .distinct()
        )
        returning_customers_count = len(returning_customer_ids)
        new_customers_count = len(online_customer_ids) - returning_customers_count
    else:
        new_customers_count = 0
        returning_customers_count = 0

    # Use cached counts from Event — maintained by refresh_event_stats() in signals.py.
    # Avoids 2 queries to the Ticket table on every page load.
    total_tickets = event.cached_ticket_count

    # Platform fee — use actual Stripe fees recorded per order (direct ticketing only)
    if event.ticketing_type == 'direct':
        fee_agg = StripeCheckoutSession.objects.filter(
            ticket_order__event=event
        ).aggregate(total_fees=Coalesce(Sum('platform_fee_cents'), 0))
        ticket_fees = Decimal(fee_agg['total_fees']) / Decimal('100')
    else:
        ticket_fees = Decimal('0.00')
    net_ticket_revenue = ticket_revenue - ticket_fees

    # Additional income
    additional_income_lines = list(event.additional_income.all())
    total_additional_income = sum(line.amount for line in additional_income_lines)
    total_revenue = net_ticket_revenue + total_additional_income

    # Expenses — evaluate queryset to list so it can be pickled for cache.
    # Unconfirmed meta_ads (campaign-matched) expenses are excluded from event
    # expense totals/listings; they only appear in the Marketing tab review UI.
    expenses_qs = event.expenses.visible()
    total_expenses = expenses_qs.aggregate(
        total=Coalesce(Sum('amount'), Decimal('0.00'))
    )['total']
    profit = total_revenue - total_expenses
    margin_pct = (profit / total_revenue * 100) if total_revenue > 0 else None
    expenses = list(expenses_qs)
    expenses_by_category = list(
        expenses_qs.values('category')
        .annotate(total=Sum('amount'))
        .order_by('-total')
    )

    # Ticket type breakdown
    saleable_ticket_types_list = list(event.saleable_ticket_types.all())
    ticket_type_allocation_charts = []
    if event.ticketing_type == 'direct':
        for tt in saleable_ticket_types_list:
            sold = tt.quantity_sold or 0
            allocated = tt.quantity_limit
            is_unlimited = allocated is None
            remaining = None if is_unlimited else max(allocated - sold, 0)
            percent_sold = None if is_unlimited or allocated == 0 else round(min(sold / allocated * 100, 100))
            ticket_type_allocation_charts.append({
                'label': tt.name,
                'sold': sold,
                'allocated': allocated,
                'remaining': remaining,
                'percent_sold': percent_sold,
                'is_unlimited': is_unlimited,
            })
        ticket_type_breakdown = [
            {'label': tt.name, 'count': tt.quantity_sold}
            for tt in saleable_ticket_types_list
            if tt.quantity_sold > 0
        ]
    else:
        _breakdown_qs = (
            Ticket.objects.filter(ticket_order__event=event)
            .values('ticket_type')
            .annotate(count=Count('id'))
            .order_by('-count')
        )
        ticket_type_breakdown = [
            {'label': row['ticket_type'], 'count': row['count']}
            for row in _breakdown_qs
            if row['ticket_type']
        ]

    # Sales over time — for the chart; cached here so it's not re-run on every page load
    sales_over_time = list(
        event.ticket_orders
        .annotate(date=TruncDate('order_date'))
        .values('date')
        .annotate(count=Count('id'), revenue=Sum('total_amount'))
        .order_by('date')
    )
    page_views_over_time = list(
        EventDailyPageView.objects.filter(event=event)
        .order_by('date')
        .values('date', 'view_count')
    )

    # Survey results — internal (SurveyResponse/SurveyAnswer)
    survey_invitations_count = SurveyInvitation.objects.filter(event=event).count()
    survey_responses_count = SurveyResponse.objects.filter(event=event).count()

    star_avg = None
    int_nps_total = int_promoters = int_detractors = 0
    internal_comments = []

    if survey_responses_count > 0:
        star_avg = SurveyAnswer.objects.filter(
            response__event=event, star_rating__isnull=False
        ).aggregate(avg=Avg('star_rating'))['avg']

        # Single aggregate instead of 3 separate .count() calls
        nps_agg = SurveyAnswer.objects.filter(
            response__event=event, nps_score__isnull=False
        ).aggregate(
            total=Count('id'),
            promoters=Count('id', filter=Q(nps_score__gte=9)),
            detractors=Count('id', filter=Q(nps_score__lte=6)),
        )
        int_nps_total = nps_agg['total']
        int_promoters = nps_agg['promoters']
        int_detractors = nps_agg['detractors']

        internal_comments = [
            {
                'text': c['text_answer'],
                'author': c['response__customer__name'] or c['response__customer__email'] or 'Anonymous',
                'source': 'Cue survey',
                'submitted_at': c['response__submitted_at'],
            }
            for c in SurveyAnswer.objects.filter(
                response__event=event
            ).exclude(text_answer='').order_by('-response__submitted_at').values(
                'text_answer',
                'response__customer__name',
                'response__customer__email',
                'response__submitted_at',
            )[:5]
        ]

    # Survey results — external (ExternalSurveyResponse from CSV uploads)
    ext_qs = ExternalSurveyResponse.objects.filter(event=event)
    ext_count = ext_qs.count()
    ext_nps_total = ext_promoters = ext_detractors = 0
    ext_comments = []
    ext_rating_breakdown = []

    if ext_count > 0:
        # Single aggregate instead of 3 separate .count() calls
        nps_agg = ext_qs.filter(nps_score__isnull=False).aggregate(
            total=Count('id'),
            promoters=Count('id', filter=Q(nps_score__gte=9)),
            detractors=Count('id', filter=Q(nps_score__lte=6)),
        )
        ext_nps_total = nps_agg['total']
        ext_promoters = nps_agg['promoters']
        ext_detractors = nps_agg['detractors']

        ext_comments = [
            {
                'text': c['text_feedback'],
                'author': c['email'] or 'Anonymous',
                'source': 'External upload',
                'submitted_at': c['responded_at'],
            }
            for c in ext_qs.exclude(text_feedback='').order_by('-responded_at').values(
                'text_feedback',
                'email',
                'responded_at',
            )[:5]
        ]

        ext_rating_breakdown = list(
            ext_qs.exclude(overall_rating='')
            .values('overall_rating')
            .annotate(count=Count('id'))
            .order_by('-count')
        )

    # Merge both sources
    survey_results = None
    survey_total_response_count = survey_responses_count + ext_count
    if survey_responses_count > 0 or ext_count > 0:
        combined_nps_total = int_nps_total + ext_nps_total
        if combined_nps_total > 0:
            combined_promoters = int_promoters + ext_promoters
            combined_detractors = int_detractors + ext_detractors
            nps_score = round((combined_promoters - combined_detractors) / combined_nps_total * 100)
        else:
            nps_score = None

        all_comments = sorted(
            internal_comments + ext_comments,
            key=lambda comment: comment['submitted_at'],
            reverse=True,
        )[:5]

        survey_results = {
            'avg_star_rating': round(star_avg, 1) if star_avg else None,
            'nps_score': nps_score,
            'nps_total': combined_nps_total,
            'recent_comments': all_comments,
            'internal_response_count': survey_responses_count,
            'ext_response_count': ext_count,
            'overall_rating_breakdown': ext_rating_breakdown,
        }

    # Customer segment breakdown for attendees of this event.
    # NOTE: attendee_segments reflects Customer.rfm_segment at cache-write time.
    # Refreshes within 300s after RFM recalculation runs — TTL is the safety net.
    segment_rows = list(
        Customer.objects.filter(
            ticket_orders__event=event,
        ).exclude(
            rfm_segment='',
        ).values('rfm_segment').annotate(count=Count('id', distinct=True)).order_by('-count')
    )
    attendee_total_with_segment = sum(r['count'] for r in segment_rows)
    attendee_segments = [
        {
            'segment': r['rfm_segment'],
            'count': r['count'],
            'pct': round(r['count'] / attendee_total_with_segment * 100) if attendee_total_with_segment else 0,
        }
        for r in segment_rows
    ]

    result = {
        'total_orders': total_orders,
        'ticket_revenue': ticket_revenue,
        'ticket_fees': ticket_fees,
        'net_ticket_revenue': net_ticket_revenue,
        'total_tickets': total_tickets,
        'total_customers': total_customers,
        'new_customers_count': new_customers_count,
        'returning_customers_count': returning_customers_count,
        'total_additional_income': total_additional_income,
        'additional_income_lines': additional_income_lines,
        'total_revenue': total_revenue,
        'total_expenses': total_expenses,
        'expenses': expenses,
        'expenses_by_category': expenses_by_category,
        'profit': profit,
        'margin_pct': margin_pct,
        'ticket_type_breakdown': ticket_type_breakdown,
        'ticket_type_allocation_charts': ticket_type_allocation_charts,
        'saleable_ticket_types_list': saleable_ticket_types_list,
        'sales_over_time': sales_over_time,
        'page_views_over_time': page_views_over_time,
        'survey_invitations_count': survey_invitations_count,
        'survey_responses_count': survey_responses_count,
        'external_survey_responses_count': ext_count,
        'survey_total_response_count': survey_total_response_count,
        'survey_results': survey_results,
        'attendee_segments': attendee_segments,
    }
    safe_cache_set(cache_key, result, timeout=300)
    return result


def _get_adjacent_event(org, event, direction):
    """Return the previous or next event in newest-first event-list order."""
    current_start_time = event.start_time or time.min
    base_qs = (
        Event.objects.filter(organization=org)
        .annotate(
            sort_start_time=Coalesce(
                'start_time',
                Value(time.min, output_field=models.TimeField()),
            )
        )
        .only('id', 'name', 'start_date', 'start_time')
    )

    if direction == 'prev':
        return base_qs.filter(
            Q(start_date__gt=event.start_date)
            | Q(start_date=event.start_date, sort_start_time__gt=current_start_time)
            | Q(start_date=event.start_date, sort_start_time=current_start_time, name__lt=event.name)
        ).order_by('start_date', 'sort_start_time', '-name').first()

    return base_qs.filter(
        Q(start_date__lt=event.start_date)
        | Q(start_date=event.start_date, sort_start_time__lt=current_start_time)
        | Q(start_date=event.start_date, sort_start_time=current_start_time, name__gt=event.name)
    ).order_by('-start_date', '-sort_start_time', 'name').first()


def _refresh_meta_ads_expenses_for_event(org, event, user=None):
    """Best-effort refresh of linked Meta Ads campaign spend before event stats render."""
    if not org.meta_ads_access_token or not org.meta_ads_account_id:
        return False

    meta_expenses = list(
        EventExpense.objects.filter(
            event=event,
            source='meta_ads',
            deleted_at__isnull=True,
        )
        .exclude(external_id='')
        .order_by('external_id')
    )
    if not meta_expenses:
        return False

    client = MetaAdsClient(org.meta_ads_access_token)
    had_error = False
    changed = False
    synced = False
    sync_time = django_tz.now()

    for expense in meta_expenses:
        try:
            spend = client.get_campaign_spend(expense.external_id)
        except MetaAdsAPIError as exc:
            had_error = True
            logger.warning(
                "Meta Ads spend refresh failed for org=%s event=%s campaign=%s: %s",
                org.id,
                event.id,
                expense.external_id,
                exc,
            )
            continue

        metadata = dict(expense.external_metadata or {})
        metadata['last_synced_at'] = sync_time.isoformat()
        update_fields = ['external_metadata', 'updated_at', 'version']
        expense.external_metadata = metadata
        expense.version += 1
        synced = True

        if expense.amount != spend:
            expense.amount = spend
            update_fields.append('amount')
            changed = True

        if user and user.is_authenticated:
            expense.updated_by = user
            update_fields.append('updated_by')

        expense.save(update_fields=update_fields)

    if synced:
        django_cache.delete(_event_stats_cache_key(event.pk))
    if changed:
        _invalidate_event_list_cache(org)
        _invalidate_marketing_cache(org)

    return had_error


def _marketing_tab_redirect(event):
    _invalidate_marketing_cache(getattr(event, 'organization', None))
    return redirect(f"{reverse('tickets:event_detail', kwargs={'event_id': event.id})}?tab=marketing")


def _get_active_meta_ads_expense_or_404(org, event_id, expense_id):
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    expense = get_object_or_404(
        EventExpense.objects.filter(
            event=event,
            source='meta_ads',
            deleted_at__isnull=True,
        ).exclude(external_id=''),
        id=expense_id,
    )
    return event, expense


def _update_meta_ads_expense_spend(expense, spend, user=None):
    metadata = dict(expense.external_metadata or {})
    metadata['last_synced_at'] = django_tz.now().isoformat()

    api_changed = expense.amount != spend
    expense.amount = spend
    expense.external_metadata = metadata
    expense.version += 1
    update_fields = ['amount', 'external_metadata', 'version', 'updated_at']

    if user and user.is_authenticated:
        expense.updated_by = user
        update_fields.append('updated_by')

    if api_changed and expense.confirmed_at:
        expense.api_data_changed_at = django_tz.now()
        update_fields.append('api_data_changed_at')

    expense.save(update_fields=update_fields)


def _get_active_mailchimp_campaign_or_404(org, event_id, email_campaign_id):
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    email_campaign = get_object_or_404(
        EventEmailCampaign.objects.filter(
            event=event,
            source='mailchimp',
            deleted_at__isnull=True,
        ).exclude(external_id=''),
        id=email_campaign_id,
    )
    return event, email_campaign


def _get_mailchimp_connection(org):
    if org.mailchimp_access_token and org.mailchimp_dc:
        return org
    return None


MAILCHIMP_TRACKED_API_FIELDS = (
    'emails_sent', 'opens', 'unique_opens', 'clicks', 'unique_clicks',
    'bounces', 'unsubscribes', 'ecommerce_orders', 'ecommerce_revenue',
)


def _save_mailchimp_campaign_from_report(event, report, user=None, match_confidence=None, match_reasoning=''):
    normalized = normalize_campaign_report(report)
    external_id = normalized['external_id']
    if not external_id:
        raise MailchimpAPIError('Mailchimp report did not include a campaign ID.')

    defaults = {
        'campaign_title': normalized['campaign_title'][:300],
        'subject_line': normalized['subject_line'][:500],
        'send_time': normalized['send_time'],
        'archive_url': normalized['archive_url'][:500],
        'emails_sent': normalized['emails_sent'],
        'opens': normalized['opens'],
        'unique_opens': normalized['unique_opens'],
        'open_rate': normalized['open_rate'],
        'clicks': normalized['clicks'],
        'unique_clicks': normalized['unique_clicks'],
        'click_rate': normalized['click_rate'],
        'bounces': normalized['bounces'],
        'unsubscribes': normalized['unsubscribes'],
        'abuse_reports': normalized['abuse_reports'],
        'ecommerce_orders': normalized['ecommerce_orders'],
        'ecommerce_revenue': normalized['ecommerce_revenue'],
        'last_synced_at': django_tz.now(),
        'external_metadata': normalized['external_metadata'],
    }
    if match_confidence is not None:
        defaults['match_confidence'] = Decimal(str(match_confidence)).quantize(Decimal('0.001'))
    if match_reasoning:
        defaults['match_reasoning'] = match_reasoning

    existing = EventEmailCampaign.objects.filter(
        event=event,
        source='mailchimp',
        external_id=external_id,
        deleted_at__isnull=True,
    ).first()

    # Snapshot pre-save values so we can detect API changes after a previous confirmation.
    pre_snapshot = (
        {field: getattr(existing, field) for field in MAILCHIMP_TRACKED_API_FIELDS}
        if existing else None
    )
    was_confirmed = bool(existing and existing.confirmed_at)

    if existing:
        for field, value in defaults.items():
            setattr(existing, field, value)
        existing.version += 1
        if user and user.is_authenticated:
            existing.updated_by = user
        email_campaign = existing
        created = False
    else:
        email_campaign = EventEmailCampaign(
            event=event,
            source='mailchimp',
            external_id=external_id,
            created_by=user if user and user.is_authenticated else None,
            **defaults,
        )
        created = True

    if was_confirmed and pre_snapshot is not None and any(
        pre_snapshot[field] != getattr(email_campaign, field) for field in MAILCHIMP_TRACKED_API_FIELDS
    ):
        email_campaign.api_data_changed_at = django_tz.now()

    email_campaign.save()
    return email_campaign, created


def _serialize_mailchimp_campaign(email_campaign, event):
    return {
        'id': str(email_campaign.id),
        'external_id': email_campaign.external_id,
        'campaign_title': email_campaign.campaign_title,
        'subject_line': email_campaign.subject_line,
        'send_time_display': _format_meta_ads_datetime(email_campaign.send_time.isoformat() if email_campaign.send_time else '') or 'Unknown',
        'emails_sent': email_campaign.emails_sent,
        'unique_opens': email_campaign.unique_opens,
        'unique_clicks': email_campaign.unique_clicks,
        'ecommerce_revenue': f"{email_campaign.ecommerce_revenue:.2f}",
        'refresh_url': reverse('tickets:event_mailchimp_refresh', kwargs={
            'event_id': event.id,
            'email_campaign_id': email_campaign.id,
        }),
        'remove_url': reverse('tickets:event_mailchimp_remove', kwargs={
            'event_id': event.id,
            'email_campaign_id': email_campaign.id,
        }),
    }


def _serialize_meta_ads_expense(expense, event):
    metadata = expense.external_metadata or {}
    return {
        'id': str(expense.id),
        'external_id': expense.external_id,
        'campaign_name': metadata.get('campaign_name') or expense.description,
        'amount': f"{expense.amount:.2f}",
        'last_synced_display': _format_meta_ads_datetime(metadata.get('last_synced_at')) or '',
        'refresh_url': reverse('tickets:event_meta_ads_refresh', kwargs={
            'event_id': event.id,
            'expense_id': expense.id,
        }),
        'remove_url': reverse('tickets:event_meta_ads_remove', kwargs={
            'event_id': event.id,
            'expense_id': expense.id,
        }),
    }


def _get_active_slicktext_campaign_or_404(org, event_id, sms_campaign_id):
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    sms_campaign = get_object_or_404(
        EventSMSCampaign.objects.filter(
            event=event,
            source='slicktext',
            deleted_at__isnull=True,
        ).exclude(external_id=''),
        id=sms_campaign_id,
    )
    return event, sms_campaign


def _get_slicktext_connection(org):
    if org.slicktext_api_key and org.slicktext_brand_id:
        return org
    return None


SLICKTEXT_TRACKED_API_FIELDS = (
    'audience_size', 'clicks', 'unique_clicks',
    'unsubscribes', 'orders', 'revenue',
)


def _save_slicktext_campaign_from_report(event, report, user=None, match_confidence=None, match_reasoning=''):
    normalized = normalize_slicktext_campaign_report(report)
    external_id = normalized['external_id']
    if not external_id:
        raise SlickTextAPIError('SlickText campaign did not include a campaign ID.')

    defaults = {
        'name': normalized['name'][:300],
        'message': normalized['message'][:1600],
        'media_url': normalized['media_url'][:500],
        'send_time': normalized['send_time'],
        'audience_size': normalized['audience_size'],
        'clicks': normalized['clicks'],
        'unique_clicks': normalized['unique_clicks'],
        'click_rate': normalized['click_rate'],
        'unsubscribes': normalized['unsubscribes'],
        'unsubscribe_rate': normalized['unsubscribe_rate'],
        'orders': normalized['orders'],
        'revenue': normalized['revenue'],
        'last_synced_at': django_tz.now(),
        'external_metadata': normalized['external_metadata'],
    }
    if match_confidence is not None:
        defaults['match_confidence'] = Decimal(str(match_confidence)).quantize(Decimal('0.001'))
    if match_reasoning:
        defaults['match_reasoning'] = match_reasoning

    existing = EventSMSCampaign.objects.filter(
        event=event,
        source='slicktext',
        external_id=external_id,
        deleted_at__isnull=True,
    ).first()

    pre_snapshot = (
        {field: getattr(existing, field) for field in SLICKTEXT_TRACKED_API_FIELDS}
        if existing else None
    )
    was_confirmed = bool(existing and existing.confirmed_at)

    if existing:
        for field, value in defaults.items():
            setattr(existing, field, value)
        existing.version += 1
        if user and user.is_authenticated:
            existing.updated_by = user
        sms_campaign = existing
        created = False
    else:
        sms_campaign = EventSMSCampaign(
            event=event,
            source='slicktext',
            external_id=external_id,
            created_by=user if user and user.is_authenticated else None,
            **defaults,
        )
        created = True

    if was_confirmed and pre_snapshot is not None and any(
        pre_snapshot[field] != getattr(sms_campaign, field) for field in SLICKTEXT_TRACKED_API_FIELDS
    ):
        sms_campaign.api_data_changed_at = django_tz.now()

    sms_campaign.save()
    return sms_campaign, created


def _serialize_slicktext_campaign(sms_campaign, event):
    return {
        'id': str(sms_campaign.id),
        'external_id': sms_campaign.external_id,
        'name': sms_campaign.name,
        'message': sms_campaign.message,
        'send_time_display': _format_meta_ads_datetime(sms_campaign.send_time.isoformat() if sms_campaign.send_time else '') or 'Unknown',
        'audience_size': sms_campaign.audience_size,
        'clicks': sms_campaign.clicks,
        'unique_clicks': sms_campaign.unique_clicks,
        'unsubscribes': sms_campaign.unsubscribes,
        'orders': sms_campaign.orders,
        'revenue': f"{sms_campaign.revenue:.2f}",
        'refresh_url': reverse('tickets:event_slicktext_refresh', kwargs={
            'event_id': event.id,
            'sms_campaign_id': sms_campaign.id,
        }),
        'remove_url': reverse('tickets:event_slicktext_remove', kwargs={
            'event_id': event.id,
            'sms_campaign_id': sms_campaign.id,
        }),
    }


def _slicktext_fetch_campaign_with_analytics(client, campaign_id):
    """Fetch a SlickText campaign plus its analytics and bundle them as a report."""
    campaign = client.get_campaign(campaign_id)
    try:
        analytics = client.get_campaign_analytics(campaign_id)
    except SlickTextAPIError:
        analytics = {}
    return build_slicktext_campaign_report(campaign, analytics)


@login_required
@require_org
@require_organizer
def event_detail(request, event_id):
    """Display detailed event information with associated uploads."""
    org = get_organization(request)
    event = get_object_or_404(
        Event.objects.filter(organization=org).select_related('venue').prefetch_related(
            'talent_lineup',
            Prefetch(
                'custom_field_values',
                EventCustomFieldValue.objects.select_related('custom_field', 'custom_field_option'),
            ),
            Prefetch(
                'additional_income',
                EventIncome.objects.filter(deleted_at__isnull=True).select_related('income_source'),
            ),
            Prefetch(
                'saleable_ticket_types',
                SaleableTicketType.objects.prefetch_related('tiers').annotate(
                    waitlist_count=Count('waitlist_entries', filter=Q(
                        waitlist_entries__purchased_at__isnull=True,
                        waitlist_entries__expired=False,
                    ))
                ).order_by('order', 'name'),
            ),
        ),
        id=event_id,
    )

    if _refresh_meta_ads_expenses_for_event(org, event, request.user):
        messages.warning(request, 'Could not refresh one or more Meta Ads campaign spends.')
    mailchimp_connection = _get_mailchimp_connection(org)

    # Compute event stats via shared helper
    stats = _compute_event_stats(event)
    total_orders = stats['total_orders']
    ticket_revenue = stats['ticket_revenue']
    ticket_fees = stats['ticket_fees']
    net_ticket_revenue = stats['net_ticket_revenue']
    total_tickets = stats['total_tickets']
    total_customers = stats['total_customers']
    new_customers_count = stats['new_customers_count']
    returning_customers_count = stats['returning_customers_count']
    total_additional_income = stats['total_additional_income']
    additional_income_lines = stats['additional_income_lines']
    total_revenue = stats['total_revenue']
    total_expenses = stats['total_expenses']
    expenses = stats['expenses']
    expenses_by_category = stats['expenses_by_category']
    profit = stats['profit']
    margin_pct = stats['margin_pct']
    ticket_type_breakdown = stats['ticket_type_breakdown']
    ticket_type_allocation_charts = stats.get('ticket_type_allocation_charts', [])
    saleable_ticket_types_list = stats['saleable_ticket_types_list']
    survey_invitations_count = stats['survey_invitations_count']
    survey_responses_count = stats['survey_responses_count']
    external_survey_responses_count = stats['external_survey_responses_count']
    survey_total_response_count = stats['survey_total_response_count']
    survey_results = stats['survey_results']

    # Paginate orders - select_related + annotate to avoid N+1 in template
    _platform_fee_subq = Subquery(
        StripeCheckoutSession.objects.filter(ticket_order=OuterRef('pk')).values('platform_fee_cents')[:1],
        output_field=DecimalField(max_digits=10, decimal_places=2),
    )
    orders_qs = event.ticket_orders.select_related(
        'customer', 'uploaded_file'
    ).annotate(
        tickets_count=Count('tickets'),
        gross_total=ExpressionWrapper(
            F('total_amount') - Cast(
                Coalesce(_platform_fee_subq, 0),
                output_field=DecimalField(max_digits=10, decimal_places=2),
            ) * Decimal('0.01'),
            output_field=DecimalField(max_digits=10, decimal_places=2),
        ),
    ).order_by('-order_date')
    paginator = Paginator(orders_qs, 100)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Custom field values: org-scoped rows with a selected option (template only renders those)
    custom_field_values_display = [
        v for v in event.custom_field_values.all()
        if v.custom_field.organization_id == org.id and v.custom_field_option_id
    ]

    # All org custom fields + existing event values for the modal
    org_custom_fields = list(
        CustomField.objects.filter(organization=org, field_type='dropdown')
        .prefetch_related('options')
    )
    existing_values = {
        v.custom_field_id: v.custom_field_option_id
        for v in EventCustomFieldValue.objects.filter(event=event)
    }

    # Map category keys to display labels
    category_labels = dict(EventExpense.CATEGORY_CHOICES)
    # The Marketing tab review UI needs ALL meta_ads expenses including
    # unconfirmed ones (so the user can confirm/unconfirm them), so we query
    # them separately rather than deriving from `expenses` (which is filtered
    # to visible / confirmed meta_ads only).
    meta_ads_expenses = list(
        event.expenses.filter(deleted_at__isnull=True, source='meta_ads')
        .order_by('-expense_date', '-created_at')
    )
    for expense in meta_ads_expenses:
        metadata = expense.external_metadata or {}
        expense.meta_ads_last_synced_display = _format_meta_ads_datetime(metadata.get('last_synced_at'))
    mailchimp_campaigns = list(
        EventEmailCampaign.objects.filter(
            event=event,
            source='mailchimp',
            deleted_at__isnull=True,
        ).order_by('-send_time', '-created_at')
    )
    for campaign in mailchimp_campaigns:
        campaign.send_time_display = _format_meta_ads_datetime(campaign.send_time.isoformat() if campaign.send_time else '')
        campaign.last_synced_display = _format_meta_ads_datetime(campaign.last_synced_at.isoformat() if campaign.last_synced_at else '')

    slicktext_connection = _get_slicktext_connection(org)
    slicktext_campaigns = list(
        EventSMSCampaign.objects.filter(
            event=event,
            source='slicktext',
            deleted_at__isnull=True,
        ).order_by('-send_time', '-created_at')
    )
    for sms_campaign in slicktext_campaigns:
        sms_campaign.send_time_display = _format_meta_ads_datetime(sms_campaign.send_time.isoformat() if sms_campaign.send_time else '')
        sms_campaign.last_synced_display = _format_meta_ads_datetime(sms_campaign.last_synced_at.isoformat() if sms_campaign.last_synced_at else '')

    mailchimp_pending_count = sum(1 for c in mailchimp_campaigns if not c.is_confirmed)
    slicktext_pending_count = sum(1 for c in slicktext_campaigns if not c.is_confirmed)
    meta_ads_pending_count = sum(1 for e in meta_ads_expenses if not e.is_confirmed)

    ticket_type_breakdown_json = json.dumps(ticket_type_breakdown)
    ticket_type_allocation_charts_json = json.dumps(ticket_type_allocation_charts)

    sales_over_time_json = json.dumps([
        {
            'date': row['date'].isoformat(),
            'count': row['count'],
            'revenue': float(row['revenue'] or 0),
        }
        for row in stats['sales_over_time']
    ])
    page_views_over_time_json = json.dumps([
        {
            'date': row['date'].isoformat(),
            'views': row['view_count'],
        }
        for row in stats['page_views_over_time']
    ])

    active_scanner_sessions = ScannerSession.objects.filter(event=event, is_active=True).count()

    prev_event = _get_adjacent_event(org, event, 'prev')
    next_event = _get_adjacent_event(org, event, 'next')

    context = {
        'event': event,
        'active_scanner_sessions': active_scanner_sessions,
        'total_orders': total_orders,
        'ticket_revenue': ticket_revenue,
        'ticket_fees': ticket_fees,
        'net_ticket_revenue': net_ticket_revenue,
        'total_additional_income': total_additional_income,
        'total_revenue': total_revenue,
        'total_tickets': total_tickets,
        'total_customers': total_customers,
        'new_customers_count': new_customers_count,
        'returning_customers_count': returning_customers_count,
        'total_expenses': total_expenses,
        'profit': profit,
        'margin_pct': margin_pct,
        'expenses_by_category': expenses_by_category,
        'expenses': expenses,
        'meta_ads_expenses': meta_ads_expenses,
        'mailchimp_connection': mailchimp_connection,
        'mailchimp_campaigns': mailchimp_campaigns,
        'slicktext_connection': slicktext_connection,
        'slicktext_campaigns': slicktext_campaigns,
        'mailchimp_pending_count': mailchimp_pending_count,
        'slicktext_pending_count': slicktext_pending_count,
        'meta_ads_pending_count': meta_ads_pending_count,
        'category_labels': category_labels,
        'additional_income_lines': additional_income_lines,
        'income_sources': IncomeSource.objects.filter(organization=org).order_by('order', 'name'),
        'page_obj': page_obj,
        'custom_field_values_display': custom_field_values_display,
        'org_custom_fields': org_custom_fields,
        'existing_values': existing_values,
        'org_has_custom_fields': bool(org_custom_fields),
        'survey_invitations_count': survey_invitations_count,
        'survey_responses_count': survey_responses_count,
        'external_survey_responses_count': external_survey_responses_count,
        'survey_total_response_count': survey_total_response_count,
        'survey_results': survey_results,
        'ticket_type_breakdown': ticket_type_breakdown,
        'ticket_type_breakdown_json': ticket_type_breakdown_json,
        'ticket_type_allocation_charts': ticket_type_allocation_charts,
        'ticket_type_allocation_charts_json': ticket_type_allocation_charts_json,
        'sales_over_time_json': sales_over_time_json,
        'page_views_over_time_json': page_views_over_time_json,
        'has_page_view_data': bool(stats['page_views_over_time']),
        'show_page_views_chart': event.ticketing_type == 'direct',
        'prev_event_id': prev_event.id if prev_event else None,
        'next_event_id': next_event.id if next_event else None,
        'prev_event_name': prev_event.name if prev_event else None,
        'next_event_name': next_event.name if next_event else None,
    }
    if event.ticketing_type != 'direct':
        context['upload_form'] = EventCSVUploadForm(organization=org)
    if event.ticketing_type == 'direct':
        _mrp_total = Decimal('0.00')
        _mrp_has_limit = False
        for _tt in saleable_ticket_types_list:
            if _tt.is_active and _tt.quantity_limit is not None:
                _mrp_has_limit = True
                _mrp_total += _tt.price * _tt.quantity_limit
        context['max_revenue_potential'] = _mrp_total if _mrp_has_limit else None

        sessions = list(
            StripeCheckoutSession.objects.filter(event=event)
            .select_related('ticket_order')
            .order_by('-created_at')[:50]
        )
        for s in sessions:
            s.amount_dollars = Decimal(str(s.amount_total_cents)) / 100
        context['dashboard_sessions'] = sessions
        context['public_buy_url'] = request.build_absolute_uri(f'/e/{event.public_id}/')
        views = getattr(event, 'public_buy_page_views', 0) or 0
        context['conversion_rate_pct'] = (
            round(total_orders / views * 100, 1) if views > 0 else None
        )
        tracking_links = list(
            TrackingLink.objects
            .filter(event=event)
            .annotate(
                purchase_count=Coalesce(Subquery(
                    StripeCheckoutSession.objects
                    .filter(tracking_link=OuterRef('pk'), status=StripeCheckoutSession.Status.COMPLETED)
                    .values('tracking_link')
                    .annotate(c=Count('id'))
                    .values('c')
                ), 0),
                purchase_revenue_cents=Coalesce(Subquery(
                    StripeCheckoutSession.objects
                    .filter(tracking_link=OuterRef('pk'), status=StripeCheckoutSession.Status.COMPLETED)
                    .values('tracking_link')
                    .annotate(s=Sum('amount_total_cents'))
                    .values('s')
                ), 0),
            )
            .order_by('-created_at')
        )
        for tl in tracking_links:
            tl.purchase_revenue = Decimal(str(tl.purchase_revenue_cents)) / 100
            tl.full_url = request.build_absolute_uri(f'/track/{tl.token}/')
        context['tracking_links'] = tracking_links
    context['today'] = date.today()
    return render(request, 'tickets/event_detail.html', context)


@login_required
@require_org
@require_organizer
def event_uploads_summary(request, event_id):
    """Render the uploads card separately so the main detail page can load sooner."""
    org = get_organization(request)
    event = get_object_or_404(
        Event.objects.filter(organization=org).only('id', 'ticketing_type', 'name'),
        id=event_id,
    )
    upload_stats = _compute_event_upload_stats(event)
    return render(request, 'tickets/includes/event_uploads_card.html', {
        'event': event,
        'upload_stats': upload_stats,
    })


@login_required
@require_org
@require_organizer
@require_http_methods(["POST"])
def generate_scanner_pin(request, event_id):
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    if event.scanner_pin:
        ScannerSession.objects.filter(event=event, is_active=True).update(is_active=False)
    event.scanner_pin = generate_unique_scanner_pin()
    event.save(update_fields=['scanner_pin', 'updated_at'])
    messages.success(request, 'Scanner PIN generated.')
    return redirect('tickets:event_detail', event_id=event.id)


@login_required
@require_org
@require_organizer
@require_http_methods(["POST"])
def revoke_scanner_pin(request, event_id):
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    ScannerSession.objects.filter(event=event, is_active=True).update(is_active=False)
    event.scanner_pin = None
    event.save(update_fields=['scanner_pin', 'updated_at'])
    messages.success(request, 'Scanner PIN revoked.')
    return redirect('tickets:event_detail', event_id=event.id)


@login_required
@require_org
def event_export_csv(request, event_id):
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    orders = (
        event.ticket_orders
        .select_related('customer', 'promo_code')
        .annotate(ticket_count=Count('tickets'))
        .order_by('order_date')
    )

    filename = f"{event.name} - Orders.csv".replace('/', '-')
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow([
        'Order Number', 'Order Date', 'Customer Name', 'Customer Email',
        'Customer Phone', 'Tickets', 'Total Amount', 'Promo Code',
        'Discount Amount', 'Refunded', 'Checked In',
    ])
    for order in orders:
        writer.writerow([
            order.order_number,
            order.order_date.strftime('%Y-%m-%d %H:%M'),
            order.customer.name,
            order.customer.email,
            order.customer.phone,
            order.ticket_count,
            order.total_amount,
            order.promo_code.code if order.promo_code else '',
            order.discount_amount or '',
            'Yes' if order.refunded_at else 'No',
            'Yes' if order.checked_in_at else 'No',
        ])
    return response


@login_required
@require_org
@require_admin
@require_http_methods(["GET", "POST"])
def event_delete(request, event_id):
    """Permanently delete an event and all its orders and tickets."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    from .models import TICKETING_TYPE_DIRECT
    if event.ticketing_type == TICKETING_TYPE_DIRECT and event.status != EVENT_STATUS_DRAFT:
        messages.error(request, 'Direct-ticketing events cannot be deleted. Use "Cancel Event" to stop sales and refund buyers.')
        return redirect('tickets:event_detail', event_id=event.id)

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

                customers_deleted = _reconcile_customers_after_order_deletion(
                    org,
                    affected_customer_ids,
                )

            _invalidate_event_list_cache(org)

            _invalidate_marketing_cache(org)
            success_msg = f"Event '{event_name}' and {orders_count} associated order(s) have been permanently deleted."
            if customers_deleted > 0:
                success_msg += f" Removed {customers_deleted} customer(s) with no remaining orders."
            messages.success(request, success_msg)
            return redirect('tickets:event_list')
        except Exception as e:
            messages.error(request, f"Error deleting event: {str(e)}")
            return redirect('tickets:event_detail', event_id=event_id)

    context = {'event': event}
    return render(request, 'tickets/event_delete.html', context)


@login_required
@require_org
@require_host
def order_detail(request, order_id):
    """Display detailed order information with all tickets."""
    org = get_organization(request)
    order = get_object_or_404(
        TicketOrder.objects.filter(event__organization=org).select_related(
            'customer', 'event', 'event__venue', 'uploaded_file',
            'stripe_checkout_session',
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

    stripe_session = getattr(order, 'stripe_checkout_session', None)
    can_refund = (
        stripe_session is not None
        and stripe_session.status == StripeCheckoutSession.Status.COMPLETED
        and order.refunded_at is None
    )

    context = {
        'order': order,
        'tickets': tickets,
        'total_tickets': total_tickets,
        'ticket_types': ticket_types,
        'stripe_session': stripe_session,
        'can_refund': can_refund,
    }
    return render(request, 'tickets/order_detail.html', context)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def refund_order(request, order_id):
    """Issue a full Stripe refund for a completed order."""
    from django.conf import settings as django_settings
    import stripe as stripe_lib

    org = get_organization(request)
    order = get_object_or_404(
        TicketOrder.objects.filter(event__organization=org).select_related(
            'customer', 'stripe_checkout_session',
        ),
        id=order_id
    )

    session = getattr(order, 'stripe_checkout_session', None)
    if (
        session is None
        or session.status != StripeCheckoutSession.Status.COMPLETED
        or order.refunded_at is not None
    ):
        messages.error(request, 'This order cannot be refunded.')
        return redirect('tickets:order_detail', order_id=order_id)

    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
    try:
        stripe_lib.Refund.create(payment_intent=session.stripe_session_id)
    except stripe_lib.error.StripeError as e:
        logger.error("Stripe refund failed for order %s: %s", order_id, e)
        messages.error(request, f'Refund failed: {e.user_message or str(e)}')
        return redirect('tickets:order_detail', order_id=order_id)

    with transaction.atomic():
        order.refunded_at = django_tz.now()
        order.save(update_fields=['refunded_at'])

        session.status = StripeCheckoutSession.Status.REFUNDED
        session.save(update_fields=['status'])

        for item in session.line_items_snapshot:
            tt_id = item.get('saleable_ticket_type_id')
            qty = item.get('quantity', 0)
            if tt_id and qty:
                SaleableTicketType.objects.filter(id=tt_id).update(
                    quantity_sold=Greatest(F('quantity_sold') - qty, Value(0))
                )

        order.customer.update_lifetime_value()
        _invalidate_event_list_cache(org)
        _invalidate_marketing_cache(org)

    # Trigger waitlist notifications for any ticket types that opened up
    from tickets.tasks import notify_next_waitlist_entry
    for item in session.line_items_snapshot:
        tt_id = item.get('saleable_ticket_type_id')
        qty = item.get('quantity', 0)
        if tt_id and qty:
            tt = SaleableTicketType.objects.filter(id=tt_id, waitlist_enabled=True).first()
            if tt:
                notify_next_waitlist_entry.delay(tt_id)

    messages.success(request, f'Order {order.display_order_number} has been refunded.')
    return redirect('tickets:order_detail', order_id=order_id)


# Format Management Views

@login_required
@require_org
@require_host
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
@require_host
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
        'existing_mapping': None,
    }
    return render(request, 'tickets/format_form.html', context)


@login_required
@require_org
@require_host
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
        'existing_mapping': format_obj.column_mapping,
    }
    return render(request, 'tickets/format_form.html', context)


@login_required
@require_org
@require_host
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
@require_host
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
@require_host
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
@require_host
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
    from django.conf import settings as django_settings
    context = {
        'form': form,
        'google_maps_api_key': django_settings.GOOGLE_MAPS_API_KEY,
    }
    return render(request, 'tickets/venue_create.html', context)


@login_required
@require_org
@require_host
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
    from django.conf import settings as django_settings
    context = {
        'form': form,
        'venue': venue,
        'google_maps_api_key': django_settings.GOOGLE_MAPS_API_KEY,
    }
    return render(request, 'tickets/venue_edit.html', context)


@login_required
@require_org
@require_host
def event_type_select(request):
    """Landing page to choose Direct or External ticketing before creating an event."""
    return render(request, 'tickets/event_type_select.html', {})


@login_required
@require_org
@require_host
def event_create(request, ticketing_type):
    """Create new event (ticketing_type comes from URL, chosen on type-select page)."""
    from .models import TICKETING_TYPE_DIRECT, TICKETING_TYPE_EXTERNAL
    if ticketing_type not in (TICKETING_TYPE_DIRECT, TICKETING_TYPE_EXTERNAL):
        return redirect('tickets:event_type_select')
    org = get_organization(request)

    if ticketing_type == TICKETING_TYPE_DIRECT:
        if request.method == 'POST':
            form = DirectEventForm(request.POST, request.FILES, organization=org)
            ticket_formset = DirectTicketTypeFormSet(
                request.POST,
                queryset=SaleableTicketType.objects.none(),
                prefix='ticket_type',
            )
            _configure_direct_create_unlock_fields(ticket_formset)
            if form.is_valid() and ticket_formset.is_valid():
                valid_tts = [
                    f for f in ticket_formset.forms
                    if f.cleaned_data.get('name', '').strip()
                    and not f.cleaned_data.get('DELETE', False)
                ]
                if not valid_tts:
                    ticket_formset._non_form_errors = ticket_formset.error_class(
                        ['At least one ticket type is required.']
                    )
                else:
                    venue = form.cleaned_data['venue']
                    event = form.save(commit=False)
                    event.organization = org
                    event.created_by = request.user
                    event.venue = venue
                    event.ticketing_type = TICKETING_TYPE_DIRECT
                    event.save()
                    created_by_index = {}
                    for idx, tt_form in enumerate(ticket_formset.forms):
                        if tt_form not in valid_tts:
                            continue
                        tt = tt_form.save(commit=False)
                        tt.event = event
                        tt.unlocks_after = None
                        tt.save()
                        created_by_index[str(idx)] = tt
                    for idx, tt_form in enumerate(ticket_formset.forms):
                        if tt_form not in valid_tts:
                            continue
                        unlock_index = (tt_form.cleaned_data.get('unlocks_after') or '').strip()
                        if not unlock_index:
                            continue
                        unlock_target = created_by_index.get(unlock_index)
                        current_tt = created_by_index.get(str(idx))
                        if unlock_target and current_tt and unlock_target.pk != current_tt.pk:
                            current_tt.unlocks_after = unlock_target
                            current_tt.save(update_fields=['unlocks_after'])
                    _invalidate_event_list_cache(org)
                    _invalidate_marketing_cache(org)
                    messages.success(request, f"Event '{event.name}' created successfully.")
                    _sync_event_to_google_calendar(event)
                    return redirect('tickets:event_detail', event_id=event.id)
        else:
            form = DirectEventForm(organization=org)
            ticket_formset = DirectTicketTypeFormSet(
                queryset=SaleableTicketType.objects.none(),
                prefix='ticket_type',
            )
            _configure_direct_create_unlock_fields(ticket_formset)
        no_venues = not Venue.objects.filter(organization=org).exists()
        venue_capacities = {
            str(v.id): v.capacity
            for v in Venue.objects.filter(organization=org)
            if v.capacity
        }
        context = {
            'form': form,
            'ticket_formset': ticket_formset,
            'ticketing_type': ticketing_type,
            'no_venues': no_venues,
            'venue_capacities_json': json.dumps(venue_capacities),
        }
        return render(request, 'tickets/event_create.html', context)

    # External ticketing path (unchanged)
    if request.method == 'POST':
        form = EventForm(
            request.POST, organization=org,
            ticketing_type_locked=True,
            hide_ticket_link=False,
        )
        talent_formset = EventTalentFormSet(request.POST, prefix='talent')
        if form.is_valid() and talent_formset.is_valid():
            event = form.save(commit=False)
            event.organization = org
            event.created_by = request.user
            event.status = EVENT_STATUS_LIVE
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
            _invalidate_event_list_cache(org)
            _invalidate_marketing_cache(org)
            messages.success(request, f"Event '{event.name}' created successfully.")
            _sync_event_to_google_calendar(event)
            return redirect('tickets:event_detail', event_id=event.id)
    else:
        form = EventForm(
            organization=org,
            ticketing_type_locked=True,
            hide_ticket_link=False,
            initial={'ticketing_type': ticketing_type},
        )
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
        'ticketing_type': ticketing_type,
    }
    return render(request, 'tickets/event_create.html', context)


@login_required
@require_org
@require_host
def event_edit(request, event_id):
    """Edit an existing event."""
    from .models import TICKETING_TYPE_DIRECT
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    if event.ticketing_type == TICKETING_TYPE_DIRECT:
        if request.method == 'POST':
            form = DirectEventForm(request.POST, request.FILES, instance=event, organization=org)
            if form.is_valid():
                if not SaleableTicketType.objects.filter(event=event).exists():
                    messages.error(request, 'At least one ticket type is required before saving.')
                else:
                    venue = form.cleaned_data['venue']
                    event = form.save(commit=False)
                    event.updated_by = request.user
                    event.venue = venue
                    event.save()
                    _invalidate_event_list_cache(org)
                    _invalidate_marketing_cache(org)
                    messages.success(request, f"Event '{event.name}' updated successfully.")
                    return redirect('tickets:event_detail', event_id=event.id)
        else:
            form = DirectEventForm(instance=event, organization=org)
        saleable_tts = list(
            SaleableTicketType.objects.filter(event=event)
            .prefetch_related('tiers')
            .annotate(
                waitlist_count=Count('waitlist_entries', filter=Q(
                    waitlist_entries__purchased_at__isnull=True,
                    waitlist_entries__expired=False,
                ))
            )
            .order_by('order', 'name')
        )
        context = {
            'form': form,
            'event': event,
            'ticketing_type': event.ticketing_type,
            'saleable_ticket_types': saleable_tts,
            'direct_total_revenue': sum(tt.quantity_sold * tt.price for tt in saleable_tts),
            'promo_codes': list(PromoCode.objects.filter(event=event, organization=org).order_by('code')),
            'venue_capacities_json': json.dumps({
                str(v.id): v.capacity
                for v in Venue.objects.filter(organization=org)
                if v.capacity
            }),
        }
        return render(request, 'tickets/event_edit.html', context)

    # External ticketing path
    if request.method == 'POST':
        form = EventForm(request.POST, instance=event, organization=org, ticketing_type_locked=True)
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
            _invalidate_event_list_cache(org)
            _invalidate_marketing_cache(org)
            messages.success(request, f"Event '{event.name}' updated successfully.")
            return redirect('tickets:event_detail', event_id=event.id)
    else:
        form = EventForm(instance=event, organization=org, ticketing_type_locked=True)
        talent_formset = EventTalentFormSet(
            queryset=EventTalent.objects.filter(event=event).order_by('order', 'name'),
            prefix='talent',
        )

    context = {
        'form': form,
        'talent_formset': talent_formset,
        'event': event,
        'ticketing_type': event.ticketing_type,
    }
    return render(request, 'tickets/event_edit.html', context)


@login_required
@require_org
@require_host
@require_http_methods(["GET", "POST"])
def event_upload_csv(request, event_id):
    """Upload a CSV directly for an existing event."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org).select_related('venue'), id=event_id)

    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

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

            uploaded_file.csv_file.save(csv_file.name, csv_file, save=True)

            if csv_format.requires_manual_pricing:
                if is_ajax:
                    from django.urls import reverse
                    return JsonResponse({'redirect': reverse('tickets:price_entry', kwargs={'file_id': uploaded_file.id})})
                return redirect('tickets:price_entry', file_id=uploaded_file.id)
            else:
                if is_ajax:
                    from tickets.tasks import process_csv_task
                    process_csv_task.delay(str(uploaded_file.id))
                    return JsonResponse({'file_id': str(uploaded_file.id)})
                return process_csv_file(request, uploaded_file)
        elif is_ajax:
            errors = {field: [str(e) for e in errs] for field, errs in form.errors.items()}
            return JsonResponse({'errors': errors}, status=400)
    else:
        form = EventCSVUploadForm(organization=org)

    context = {
        'form': form,
        'event': event,
    }
    return render(request, 'tickets/event_upload.html', context)



@login_required
@require_org
@require_host
def settings_overview(request):
    """Hub for organization setup and integrations."""
    get_organization(request)
    return render(request, 'tickets/settings_overview.html')


@login_required
@require_org
@require_admin
def settings_google_calendar(request):
    """Google Calendar (Pipedream) settings: set or edit webhook URL, or disconnect."""
    from django.core.validators import URLValidator
    from django.core.exceptions import ValidationError

    org = get_organization(request)
    connection = PipedreamCalendarConnection.objects.filter(organization=org).first()

    if request.method == 'POST':
        webhook_url = (request.POST.get('webhook_url') or '').strip()
        if not webhook_url:
            messages.error(request, 'Please enter a webhook URL.')
            return redirect('tickets:settings_google_calendar')
        try:
            URLValidator()(webhook_url)
        except ValidationError:
            messages.error(request, 'Please enter a valid URL.')
            return redirect('tickets:settings_google_calendar')
        connection, created = PipedreamCalendarConnection.objects.get_or_create(
            organization=org,
            defaults={'webhook_url': webhook_url},
        )
        if not created:
            connection.webhook_url = webhook_url
            connection.save()
        messages.success(request, 'Google Calendar (Pipedream) webhook saved. New events will be sent to this URL.')
        return redirect('tickets:settings_google_calendar')

    context = {
        'connection': connection,
    }
    return render(request, 'tickets/settings_google_calendar.html', context)


@login_required
@require_org
@require_admin
def settings_google_calendar_disconnect(request):
    """Remove Pipedream calendar connection for the current org."""
    if request.method != 'POST':
        return redirect('tickets:settings_google_calendar')
    org = get_organization(request)
    PipedreamCalendarConnection.objects.filter(organization=org).delete()
    messages.success(request, 'Google Calendar (Pipedream) disconnected.')
    return redirect('tickets:settings_google_calendar')


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def meta_ads_settings(request):
    """Show Meta Ads connection state for the current org."""
    org = get_organization(request)
    accounts = None
    if org.meta_ads_access_token and not org.meta_ads_account_id:
        try:
            accounts = MetaAdsClient(org.meta_ads_access_token).list_ad_accounts()
        except MetaAdsAPIError as exc:
            messages.error(request, f'Could not load Meta ad accounts: {exc}')

    return render(request, 'tickets/settings_meta_ads.html', {
        'accounts': accounts,
        'callback_url': request.build_absolute_uri(reverse('tickets:meta_ads_callback')),
        'facebook_configured': bool(settings.FACEBOOK_APP_ID and settings.FACEBOOK_APP_SECRET),
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def meta_ads_connect(request):
    """Start Facebook OAuth for Meta Ads Insights access."""
    if not settings.FACEBOOK_APP_ID or not settings.FACEBOOK_APP_SECRET:
        messages.error(request, 'Meta Ads is not configured. Add FACEBOOK_APP_ID and FACEBOOK_APP_SECRET.')
        return redirect('tickets:meta_ads_settings')

    state = secrets.token_urlsafe(32)
    request.session['meta_oauth_state'] = state
    callback_url = request.build_absolute_uri(reverse('tickets:meta_ads_callback'))
    params = urlencode({
        'client_id': settings.FACEBOOK_APP_ID,
        'redirect_uri': callback_url,
        'state': state,
        'scope': 'ads_read,business_management',
        'response_type': 'code',
    })
    return redirect(f'https://www.facebook.com/{settings.FACEBOOK_GRAPH_API_VERSION}/dialog/oauth?{params}')


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def meta_ads_callback(request):
    """Handle Facebook OAuth callback and persist the long-lived user token."""
    org = get_organization(request)
    expected_state = request.session.pop('meta_oauth_state', None)
    if not expected_state or request.GET.get('state') != expected_state:
        messages.error(request, 'Meta Ads connection could not be verified. Please try again.')
        return redirect('tickets:meta_ads_settings')

    if request.GET.get('error'):
        messages.error(request, request.GET.get('error_description') or 'Meta Ads authorization was cancelled.')
        return redirect('tickets:meta_ads_settings')

    code = request.GET.get('code')
    if not code:
        messages.error(request, 'Meta did not return an authorization code.')
        return redirect('tickets:meta_ads_settings')

    callback_url = request.build_absolute_uri(reverse('tickets:meta_ads_callback'))
    try:
        short_token = meta_exchange_code_for_token(code, callback_url)
        long_token = exchange_for_long_lived_token(short_token['access_token'])
        access_token = long_token['access_token']
        profile = MetaAdsClient(access_token).get_user_profile()
    except (KeyError, MetaAdsAPIError) as exc:
        messages.error(request, f'Could not connect Meta Ads: {exc}')
        return redirect('tickets:meta_ads_settings')

    expires_in = long_token.get('expires_in')
    org.meta_ads_access_token = access_token
    org.meta_ads_user_id = profile.get('id', '')
    org.meta_ads_account_id = ''
    org.meta_ads_account_name = ''
    org.meta_ads_token_expires_at = (
        django_tz.now() + timedelta(seconds=int(expires_in))
        if expires_in else None
    )
    org.save(update_fields=[
        'meta_ads_access_token',
        'meta_ads_user_id',
        'meta_ads_account_id',
        'meta_ads_account_name',
        'meta_ads_token_expires_at',
    ])
    messages.success(request, 'Meta Ads connected. Choose an ad account to finish setup.')
    return redirect('tickets:meta_ads_select_account')


@login_required
@require_org
@require_admin
@require_http_methods(["GET", "POST"])
def meta_ads_select_account(request):
    """Pick the Meta ad account to use for campaign spend."""
    org = get_organization(request)
    if not org.meta_ads_access_token:
        messages.error(request, 'Connect Meta Ads before choosing an ad account.')
        return redirect('tickets:meta_ads_settings')

    try:
        accounts = MetaAdsClient(org.meta_ads_access_token).list_ad_accounts()
    except MetaAdsAPIError as exc:
        messages.error(request, f'Could not load Meta ad accounts: {exc}')
        return redirect('tickets:meta_ads_settings')

    if request.method == 'POST':
        selected_account_id = request.POST.get('account_id', '')
        selected = next((account for account in accounts if account.get('id') == selected_account_id), None)
        if not selected:
            messages.error(request, 'Please choose a valid Meta ad account.')
            return redirect('tickets:meta_ads_select_account')

        org.meta_ads_account_id = selected.get('id', '')
        org.meta_ads_account_name = selected.get('name', '')
        org.save(update_fields=['meta_ads_account_id', 'meta_ads_account_name'])
        messages.success(request, f'Meta Ads account "{org.meta_ads_account_name}" connected.')
        return redirect('tickets:meta_ads_settings')

    return render(request, 'tickets/settings_meta_ads.html', {
        'accounts': accounts,
        'callback_url': request.build_absolute_uri(reverse('tickets:meta_ads_callback')),
        'facebook_configured': bool(settings.FACEBOOK_APP_ID and settings.FACEBOOK_APP_SECRET),
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def meta_ads_disconnect(request):
    """Clear Meta Ads tokens and account selection for the current org."""
    org = get_organization(request)
    org.meta_ads_access_token = ''
    org.meta_ads_user_id = ''
    org.meta_ads_account_id = ''
    org.meta_ads_account_name = ''
    org.meta_ads_token_expires_at = None
    org.save(update_fields=[
        'meta_ads_access_token',
        'meta_ads_user_id',
        'meta_ads_account_id',
        'meta_ads_account_name',
        'meta_ads_token_expires_at',
    ])
    messages.success(request, 'Meta Ads disconnected.')
    return redirect('tickets:meta_ads_settings')


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def mailchimp_settings(request):
    """Show Mailchimp connection state for the current org."""
    org = get_organization(request)
    return render(request, 'tickets/settings_mailchimp.html', {
        'org': org,
        'mailchimp_connection': _get_mailchimp_connection(org),
        'mailchimp_configured': bool(settings.MAILCHIMP_CLIENT_ID and settings.MAILCHIMP_CLIENT_SECRET),
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def mailchimp_connect(request):
    """Start direct Mailchimp OAuth for report access."""
    org = get_organization(request)
    if not settings.MAILCHIMP_CLIENT_ID or not settings.MAILCHIMP_CLIENT_SECRET:
        messages.error(request, 'Mailchimp OAuth is not configured. Add MAILCHIMP_CLIENT_ID and MAILCHIMP_CLIENT_SECRET.')
        return redirect('tickets:mailchimp_settings')

    callback_url = request.build_absolute_uri(reverse('tickets:mailchimp_callback'))
    state = secrets.token_urlsafe(32)
    request.session['mailchimp_oauth_state'] = state
    return redirect(build_authorize_url(callback_url, state))


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def mailchimp_callback(request):
    """Persist direct Mailchimp OAuth credentials after authorization."""
    org = get_organization(request)
    if request.GET.get('error'):
        messages.error(request, request.GET.get('error_description') or request.GET.get('error') or 'Mailchimp authorization was cancelled.')
        return redirect('tickets:mailchimp_settings')

    expected_state = request.session.get('mailchimp_oauth_state')
    state = request.GET.get('state')
    if not expected_state or not state or state != expected_state:
        messages.error(request, 'Mailchimp authorization could not be verified. Please try connecting again.')
        return redirect('tickets:mailchimp_settings')
    request.session.pop('mailchimp_oauth_state', None)

    code = request.GET.get('code')
    if not code:
        messages.error(request, 'Mailchimp did not return an authorization code. Please try connecting again.')
        return redirect('tickets:mailchimp_settings')

    callback_url = request.build_absolute_uri(reverse('tickets:mailchimp_callback'))
    try:
        access_token = exchange_code_for_token(code, callback_url)
        metadata = get_oauth_metadata(access_token)
        dc = metadata.get('dc')
        if not dc:
            raise MailchimpAPIError('Mailchimp did not return an account data center.')
        account_root = MailchimpClient(access_token, dc).get_account_root()
    except MailchimpAPIError as exc:
        messages.error(request, f'Could not verify Mailchimp connection: {exc}')
        return redirect('tickets:mailchimp_settings')

    login = metadata.get('login') or {}
    org.mailchimp_access_token = access_token
    org.mailchimp_dc = dc
    org.mailchimp_account_id = str(
        account_root.get('account_id')
        or metadata.get('account_id')
        or metadata.get('user_id')
        or ''
    )
    org.mailchimp_account_name = str(
        account_root.get('account_name')
        or metadata.get('accountname')
        or metadata.get('account_name')
        or ''
    )
    org.mailchimp_login_email = str(login.get('email') or metadata.get('login_email') or account_root.get('email') or '')
    org.save(update_fields=[
        'mailchimp_access_token',
        'mailchimp_dc',
        'mailchimp_account_id',
        'mailchimp_account_name',
        'mailchimp_login_email',
    ])

    messages.success(request, 'Mailchimp connected.')
    return redirect('tickets:mailchimp_settings')


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def mailchimp_disconnect(request):
    """Disconnect Mailchimp from the current org."""
    org = get_organization(request)
    if _get_mailchimp_connection(org):
        org.mailchimp_access_token = ''
        org.mailchimp_dc = ''
        org.mailchimp_account_id = ''
        org.mailchimp_account_name = ''
        org.mailchimp_login_email = ''
        org.save(update_fields=[
            'mailchimp_access_token',
            'mailchimp_dc',
            'mailchimp_account_id',
            'mailchimp_account_name',
            'mailchimp_login_email',
        ])
        messages.success(request, 'Mailchimp disconnected.')
    else:
        messages.info(request, 'Mailchimp was not connected.')
    return redirect('tickets:mailchimp_settings')


@login_required
@require_org
@require_admin
def slicktext_settings(request):
    """Show SlickText connection status and the credential form."""
    org = get_organization(request)
    return render(request, 'tickets/settings_slicktext.html', {
        'org': org,
        'slicktext_connection': _get_slicktext_connection(org),
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def slicktext_save(request):
    """Validate posted SlickText API key, fetch the brand, and persist credentials."""
    org = get_organization(request)
    api_key = (request.POST.get('api_key') or '').strip()
    if not api_key:
        messages.error(request, 'Enter your SlickText API key.')
        return redirect('tickets:slicktext_settings')

    try:
        brand = SlickTextClient(api_key).get_brand()
    except SlickTextAPIError as exc:
        messages.error(request, f'Could not verify SlickText credentials: {exc}')
        return redirect('tickets:slicktext_settings')

    brand_id = str(brand.get('brand_id') or brand.get('id') or '')
    if not brand_id:
        messages.error(request, 'SlickText did not return a brand ID for this API key.')
        return redirect('tickets:slicktext_settings')

    org.slicktext_api_key = api_key
    org.slicktext_brand_id = brand_id
    org.slicktext_brand_name = str(brand.get('name') or brand.get('legal_name') or '')
    org.slicktext_validated_at = django_tz.now()
    org.save(update_fields=[
        'slicktext_api_key',
        'slicktext_brand_id',
        'slicktext_brand_name',
        'slicktext_validated_at',
    ])
    messages.success(request, 'SlickText connected.')
    return redirect('tickets:slicktext_settings')


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def slicktext_disconnect(request):
    """Disconnect SlickText from the current org."""
    org = get_organization(request)
    if _get_slicktext_connection(org):
        org.slicktext_api_key = ''
        org.slicktext_brand_id = ''
        org.slicktext_brand_name = ''
        org.slicktext_validated_at = None
        org.save(update_fields=[
            'slicktext_api_key',
            'slicktext_brand_id',
            'slicktext_brand_name',
            'slicktext_validated_at',
        ])
        messages.success(request, 'SlickText disconnected.')
    else:
        messages.info(request, 'SlickText was not connected.')
    return redirect('tickets:slicktext_settings')


@login_required
@require_org
@require_admin
def settings_api_keys(request):
    """List and create API keys for the current org."""
    org = get_organization(request)
    new_key_plain = None

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        if not name:
            messages.error(request, 'Key name is required.')
        else:
            api_key = OrganizationAPIKey.objects.create(organization=org, name=name)
            new_key_plain = api_key.key
            messages.success(request, f'API key "{name}" created. Copy it now — it will not be shown again.')

    api_keys = OrganizationAPIKey.objects.filter(organization=org)
    return render(request, 'tickets/settings_api_keys.html', {
        'api_keys': api_keys,
        'new_key_plain': new_key_plain,
    })


@login_required
@require_org
@require_admin
def settings_api_key_revoke(request, key_id):
    """Revoke an API key (POST only)."""
    if request.method != 'POST':
        return redirect('tickets:settings_api_keys')
    org = get_organization(request)
    api_key = get_object_or_404(OrganizationAPIKey.objects.filter(organization=org), id=key_id)
    api_key.is_active = False
    api_key.save(update_fields=['is_active'])
    messages.success(request, f'API key "{api_key.name}" has been revoked.')
    return redirect('tickets:settings_api_keys')


@login_required
@require_org
@require_admin
def custom_field_list(request):
    """List all org-level custom field definitions."""
    org = get_organization(request)
    fields = CustomField.objects.filter(organization=org).prefetch_related('options')
    return render(request, 'tickets/custom_field_list.html', {'fields': fields})


@login_required
@require_org
@require_admin
def custom_field_create(request):
    """Create a new custom field with inline options."""
    org = get_organization(request)
    if request.method == 'POST':
        form = CustomFieldForm(request.POST)
        formset = CustomFieldOptionFormSet(request.POST)
        if form.is_valid() and formset.is_valid():
            field = form.save(commit=False)
            field.organization = org
            field.field_type = 'dropdown'
            # Append new fields at the end of the current order
            field.order = CustomField.objects.filter(organization=org).count()
            field.save()
            formset.instance = field
            formset.save()
            messages.success(request, f'Custom field "{field.name}" created.')
            return redirect('tickets:custom_field_list')
    else:
        form = CustomFieldForm()
        formset = CustomFieldOptionFormSet()
    return render(request, 'tickets/custom_field_form.html', {
        'form': form,
        'formset': formset,
        'action': 'Create',
    })


@login_required
@require_org
@require_admin
def custom_field_edit(request, field_id):
    """Edit a custom field and manage its options."""
    org = get_organization(request)
    field = get_object_or_404(CustomField.objects.filter(organization=org), id=field_id)
    if request.method == 'POST':
        form = CustomFieldForm(request.POST, instance=field)
        formset = CustomFieldOptionFormSet(request.POST, instance=field)
        if form.is_valid() and formset.is_valid():
            form.save()
            formset.save()
            # Handle default_option after formset.save() so newly-added options are available
            raw_default = request.POST.get('default_option', '').strip()
            if raw_default:
                try:
                    opt = CustomFieldOption.objects.get(id=int(raw_default), custom_field=field)
                    field.default_option = opt
                except (ValueError, CustomFieldOption.DoesNotExist):
                    field.default_option = None
            else:
                field.default_option = None
            field.save(update_fields=['default_option'])
            messages.success(request, f'Custom field "{field.name}" updated.')
            return redirect('tickets:custom_field_list')
    else:
        form = CustomFieldForm(instance=field)
        formset = CustomFieldOptionFormSet(instance=field)
    return render(request, 'tickets/custom_field_form.html', {
        'form': form,
        'formset': formset,
        'action': 'Edit',
        'field': field,
    })


@login_required
@require_org
@require_admin
def custom_field_delete(request, field_id):
    """Delete a custom field (cascades to options and event values)."""
    if request.method != 'POST':
        return redirect('tickets:custom_field_list')
    org = get_organization(request)
    field = get_object_or_404(CustomField.objects.filter(organization=org), id=field_id)
    name = field.name
    field.delete()
    messages.success(request, f'Custom field "{name}" deleted.')
    return redirect('tickets:custom_field_list')


@login_required
@require_org
@require_admin
def custom_field_reorder(request):
    """AJAX: save new order for custom fields after drag-and-drop."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    org = get_organization(request)
    try:
        data = json.loads(request.body)
        ids = [int(i) for i in data.get('order', [])]
    except (json.JSONDecodeError, ValueError, TypeError):
        return JsonResponse({'error': 'Invalid payload'}, status=400)

    fields = {cf.id: cf for cf in CustomField.objects.filter(organization=org, id__in=ids)}
    to_update = []
    for position, field_id in enumerate(ids):
        if field_id in fields:
            fields[field_id].order = position
            to_update.append(fields[field_id])
    CustomField.objects.bulk_update(to_update, ['order'])
    return JsonResponse({'ok': True})


@login_required
@require_org
@require_host
def event_custom_fields(request, event_id):
    """POST-only: save custom field values for a specific event."""
    if request.method != 'POST':
        return redirect('tickets:event_detail', event_id=event_id)
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    dropdown_fields = CustomField.objects.filter(
        organization=org, field_type='dropdown'
    ).prefetch_related('options')

    for cf in dropdown_fields:
        raw = request.POST.get(f'custom_field_{cf.id}', '').strip()
        if raw:
            try:
                option = CustomFieldOption.objects.get(id=int(raw), custom_field=cf)
                EventCustomFieldValue.objects.update_or_create(
                    event=event,
                    custom_field=cf,
                    defaults={'custom_field_option': option},
                )
            except (ValueError, CustomFieldOption.DoesNotExist):
                pass
        else:
            EventCustomFieldValue.objects.filter(event=event, custom_field=cf).delete()

    messages.success(request, 'Custom field values saved.')
    return redirect('tickets:event_detail', event_id=event.id)


@login_required
@require_org
@require_admin
def org_profile(request):
    """View and edit the organization's public profile (photo, description, website)."""
    org = get_organization(request)
    if request.method == 'POST':
        form = OrgProfileForm(request.POST, request.FILES, instance=org)
        if form.is_valid():
            form.save()
            messages.success(request, 'Organization profile updated.')
            return redirect('tickets:org_profile')
    else:
        form = OrgProfileForm(instance=org)
    return render(request, 'tickets/settings_org_profile.html', {'form': form, 'org': org})


# Forecast Tool Views

@login_required
@require_org
@require_host
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
@require_host
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


@login_required
@require_org
@require_host
@require_http_methods(["GET", "POST"])
def event_pricing_recommendation(request, event_id):
    """Get or apply smart pricing recommendations for a direct-ticketing event."""
    if not smart_pricing_recommendations_enabled(request.user):
        raise Http404()
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    recommender = SmartPricingRecommender(org)

    if request.method == 'GET':
        preview_event = event
        venue_id = request.GET.get('venue_id', '').strip()
        capacity_str = request.GET.get('capacity', '').strip()
        start_date_str = request.GET.get('start_date', '').strip()

        if venue_id:
            preview_event.venue = get_object_or_404(Venue.objects.filter(organization=org), id=venue_id)
            preview_event.venue_id = preview_event.venue.id
        if capacity_str:
            try:
                preview_event.capacity = int(capacity_str)
            except ValueError:
                pass
        if start_date_str:
            try:
                preview_event.start_date = date.fromisoformat(start_date_str)
            except ValueError:
                pass

        recommendation = recommender.recommend(preview_event)
        response = JsonResponse(recommendation)
        response['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        response['Pragma'] = 'no-cache'
        return response

    recommendation = recommender.recommend(event)
    if not recommendation.get('ready'):
        return JsonResponse({'success': False, 'error': recommendation.get('blocking_reason', 'Unable to apply recommendation.')}, status=400)
    if event.saleable_ticket_types.exists():
        return JsonResponse({'success': False, 'error': 'This event already has ticket types. Smart Pricing can only auto-create pricing before ticket types are configured.'}, status=400)

    ticket_type_data = recommendation.get('recommended_ticket_type', {})
    recommended_tiers = recommendation.get('recommended_tiers', [])
    if not recommended_tiers:
        return JsonResponse({'success': False, 'error': 'No recommended tiers were generated.'}, status=400)

    with transaction.atomic():
        ticket_type = SaleableTicketType.objects.create(
            event=event,
            name=ticket_type_data.get('name') or 'General Admission',
            description='',
            price=Decimal(ticket_type_data.get('price') or recommended_tiers[-1]['price']),
            quantity_limit=event.capacity,
            order=0,
            is_active=True,
        )
        SaleableTicketTypeTier.objects.bulk_create([
            SaleableTicketTypeTier(
                ticket_type=ticket_type,
                name=tier['name'],
                price=Decimal(tier['price']),
                allotment=int(tier['allotment']),
                order=int(tier['order']),
            )
            for tier in recommended_tiers
        ])

    _invalidate_event_list_cache(org)

    _invalidate_marketing_cache(org)
    return JsonResponse({
        'success': True,
        'message': 'Ticket types created from recommendation.',
        'redirect': reverse('tickets:event_edit', kwargs={'event_id': event.id}),
    })


# ---------------------------------------------------------------------------
# Event Expense Views
# ---------------------------------------------------------------------------

@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def event_meta_ads_match(request, event_id):
    """Rank Meta campaigns that likely correspond to this event."""
    org = get_organization(request)
    wants_json = request.GET.get('format') == 'json'
    event = get_object_or_404(
        Event.objects.filter(organization=org).select_related('venue'),
        id=event_id,
    )
    if not org.meta_ads_access_token or not org.meta_ads_account_id:
        if wants_json:
            return JsonResponse({
                'success': False,
                'error': 'Connect Meta Ads and choose an ad account before matching campaigns.',
            }, status=400)
        messages.error(request, 'Connect Meta Ads and choose an ad account before matching campaigns.')
        return redirect('tickets:meta_ads_settings')

    try:
        client = MetaAdsClient(org.meta_ads_access_token)
        campaigns = client.list_campaigns(org.meta_ads_account_id)
        match_result = MetaCampaignMatcher(org).rank(event, campaigns)
    except MetaAdsAPIError as exc:
        if wants_json:
            return JsonResponse({'success': False, 'error': f'Could not load Meta campaigns: {exc}'}, status=502)
        messages.error(request, f'Could not load Meta campaigns: {exc}')
        return redirect('tickets:event_detail', event_id=event.id)
    except Exception as exc:
        logger.exception("Meta campaign matching failed for event %s: %s", event.id, exc)
        if wants_json:
            return JsonResponse({
                'success': False,
                'error': 'Could not rank Meta campaigns. Please check your OpenAI configuration and try again.',
            }, status=500)
        messages.error(request, 'Could not rank Meta campaigns. Please check your OpenAI configuration and try again.')
        return redirect('tickets:event_detail', event_id=event.id)

    campaigns_by_id = {str(campaign.get('id')): campaign for campaign in campaigns}
    candidates = []
    for candidate in match_result.candidates:
        campaign = campaigns_by_id.get(candidate.campaign_id)
        if not campaign:
            continue
        confidence_pct = int(round(candidate.confidence * 100))
        if candidate.confidence >= 0.7:
            confidence_class = 'bg-success'
        elif candidate.confidence >= 0.3:
            confidence_class = 'bg-warning'
        else:
            confidence_class = 'bg-secondary'
        candidates.append({
            'campaign': campaign,
            'confidence': candidate.confidence,
            'confidence_pct': confidence_pct,
            'confidence_class': confidence_class,
            'reasoning': candidate.reasoning,
        })

    if wants_json:
        return JsonResponse({
            'success': True,
            'account_name': org.meta_ads_account_name,
            'candidates': [
                {
                    'campaign_id': item['campaign'].get('id'),
                    'campaign_name': item['campaign'].get('name') or item['campaign'].get('id'),
                    'objective': item['campaign'].get('objective') or 'No objective',
                    'start_time': _format_meta_ads_datetime(
                        item['campaign'].get('start_time') or item['campaign'].get('created_time')
                    ) or 'Unknown',
                    'stop_time': _format_meta_ads_datetime(item['campaign'].get('stop_time')) or 'Not set',
                    'confidence_pct': item['confidence_pct'],
                    'confidence_class': item['confidence_class'],
                    'reasoning': item['reasoning'],
                }
                for item in candidates
            ],
        })

    return render(request, 'tickets/event_meta_ads_match.html', {
        'event': event,
        'candidates': candidates,
        'account_name': org.meta_ads_account_name,
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_meta_ads_apply(request, event_id):
    """Pull campaign lifetime spend and upsert it as this event's Meta Ads expense."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    wants_json = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    def _json_error(msg, status):
        return JsonResponse({'success': False, 'error': msg}, status=status)

    if not org.meta_ads_access_token or not org.meta_ads_account_id:
        if wants_json:
            return _json_error('Connect Meta Ads and choose an ad account before applying campaign spend.', 400)
        messages.error(request, 'Connect Meta Ads and choose an ad account before applying campaign spend.')
        return redirect('tickets:meta_ads_settings')

    campaign_id = request.POST.get('campaign_id', '').strip()
    if not campaign_id:
        if wants_json:
            return _json_error('Choose a Meta campaign to apply.', 400)
        messages.error(request, 'Choose a Meta campaign to apply.')
        return redirect('tickets:event_meta_ads_match', event_id=event.id)

    try:
        client = MetaAdsClient(org.meta_ads_access_token)
        campaigns = client.list_campaigns(org.meta_ads_account_id)
        campaign = next((item for item in campaigns if str(item.get('id')) == campaign_id), None)
        if not campaign:
            if wants_json:
                return _json_error('The selected Meta campaign was not found in this ad account.', 404)
            messages.error(request, 'The selected Meta campaign was not found in this ad account.')
            return redirect('tickets:event_meta_ads_match', event_id=event.id)
        spend = client.get_campaign_spend(campaign_id)
    except MetaAdsAPIError as exc:
        if wants_json:
            return _json_error(f'Could not pull campaign spend from Meta: {exc}', 502)
        messages.error(request, f'Could not pull campaign spend from Meta: {exc}')
        return redirect('tickets:event_meta_ads_match', event_id=event.id)

    campaign_name = campaign.get('name') or campaign_id
    expense, created = EventExpense.objects.get_or_create(
        event=event,
        source='meta_ads',
        external_id=campaign_id,
        deleted_at__isnull=True,
        defaults={
            'category': 'marketing',
            'description': f'Meta Ads: {campaign_name}'[:300],
            'amount': spend,
            'expense_date': event.start_date,
            'external_metadata': {},
            'created_by': request.user,
        },
    )
    if not created:
        old_amount = expense.amount
        was_confirmed = bool(expense.confirmed_at)
        expense.category = 'marketing'
        expense.description = f'Meta Ads: {campaign_name}'[:300]
        expense.amount = spend
        expense.expense_date = expense.expense_date or event.start_date
        expense.external_id = campaign_id
        expense.updated_by = request.user
        expense.version += 1
        if was_confirmed and old_amount != spend:
            expense.api_data_changed_at = django_tz.now()

    expense.external_metadata = {
        'campaign_name': campaign_name,
        'ad_account_id': org.meta_ads_account_id,
        'ad_account_name': org.meta_ads_account_name,
        'last_synced_at': django_tz.now().isoformat(),
    }
    expense.save()
    _invalidate_event_list_cache(org)
    _invalidate_marketing_cache(org)

    action = 'updated' if not created else 'added'
    success_msg = f'Meta Ads campaign spend ${spend:,.2f} {action} as a linked marketing expense.'

    if wants_json:
        linked = list(
            EventExpense.objects.filter(
                event=event,
                source='meta_ads',
                deleted_at__isnull=True,
            ).order_by('-created_at')
        )
        return JsonResponse({
            'success': True,
            'message': success_msg,
            'expenses': [_serialize_meta_ads_expense(item, event) for item in linked],
        })

    messages.success(request, success_msg)
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_meta_ads_refresh(request, event_id, expense_id):
    """Refresh a single linked Meta Ads campaign expense."""
    org = get_organization(request)
    event, expense = _get_active_meta_ads_expense_or_404(org, event_id, expense_id)
    ajax = _wants_marketing_json(request)

    if not org.meta_ads_access_token or not org.meta_ads_account_id:
        msg = 'Connect Meta Ads and choose an ad account before refreshing campaign spend.'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=400)
        messages.error(request, msg)
        return redirect('tickets:meta_ads_settings')

    try:
        spend = MetaAdsClient(org.meta_ads_access_token).get_campaign_spend(expense.external_id)
    except MetaAdsAPIError as exc:
        logger.warning(
            "Meta Ads row refresh failed for org=%s event=%s expense=%s campaign=%s: %s",
            org.id,
            event.id,
            expense.id,
            expense.external_id,
            exc,
        )
        msg = 'Could not refresh this Meta Ads campaign spend.'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=502)
        messages.warning(request, msg)
        return _marketing_tab_redirect(event)

    old_amount = expense.amount
    _update_meta_ads_expense_spend(expense, spend, request.user)
    django_cache.delete(_event_stats_cache_key(event.pk))
    if old_amount != spend:
        _invalidate_event_list_cache(org)
        _invalidate_marketing_cache(org)

    if ajax:
        expense.refresh_from_db()
        return JsonResponse({'ok': True, 'row': _serialize_ads_row(expense)})
    messages.success(request, f'Meta Ads campaign spend refreshed to ${spend:,.2f}.')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_meta_ads_remove(request, event_id, expense_id):
    """Unlink a Meta Ads campaign from an event by soft-deleting its expense."""
    org = get_organization(request)
    event, expense = _get_active_meta_ads_expense_or_404(org, event_id, expense_id)

    expense.updated_by = request.user
    expense.save(update_fields=['updated_by'])
    expense.delete()
    django_cache.delete(_event_stats_cache_key(event.pk))
    _invalidate_event_list_cache(org)
    _invalidate_marketing_cache(org)

    if _wants_marketing_json(request):
        return JsonResponse({'ok': True, 'removed_id': str(expense.id)})
    messages.success(request, 'Meta Ads campaign removed from this event.')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def event_mailchimp_match(request, event_id):
    """Rank Mailchimp campaigns that likely correspond to this event."""
    org = get_organization(request)
    wants_json = request.GET.get('format') == 'json'
    event = get_object_or_404(
        Event.objects.filter(organization=org).select_related('venue'),
        id=event_id,
    )
    connection = _get_mailchimp_connection(org)
    if not connection:
        if wants_json:
            return JsonResponse({
                'success': False,
                'error': 'Connect Mailchimp before matching campaigns.',
            }, status=400)
        messages.error(request, 'Connect Mailchimp before matching campaigns.')
        return redirect('tickets:mailchimp_settings')

    try:
        client = MailchimpClient(connection.mailchimp_access_token, connection.mailchimp_dc)
        reports = client.list_campaign_reports()
        match_result = MailchimpCampaignMatcher(org).rank(event, reports)
    except MailchimpAPIError as exc:
        if wants_json:
            return JsonResponse({'success': False, 'error': f'Could not load Mailchimp campaigns: {exc}'}, status=502)
        messages.error(request, f'Could not load Mailchimp campaigns: {exc}')
        return redirect('tickets:event_detail', event_id=event.id)
    except Exception as exc:
        logger.exception("Mailchimp campaign matching failed for event %s: %s", event.id, exc)
        if wants_json:
            return JsonResponse({
                'success': False,
                'error': 'Could not rank Mailchimp campaigns. Please check your OpenAI configuration and try again.',
            }, status=500)
        messages.error(request, 'Could not rank Mailchimp campaigns. Please check your OpenAI configuration and try again.')
        return redirect('tickets:event_detail', event_id=event.id)

    linked_ids = set(
        EventEmailCampaign.objects.filter(
            event=event,
            source='mailchimp',
            deleted_at__isnull=True,
        ).exclude(external_id='').values_list('external_id', flat=True)
    )

    reports_by_id = {str(report.get('id')): report for report in reports}
    candidates = []
    for candidate in match_result.candidates:
        report = reports_by_id.get(candidate.campaign_id)
        if not report:
            continue
        confidence_pct = int(round(candidate.confidence * 100))
        if candidate.confidence >= 0.7:
            confidence_class = 'bg-success'
        elif candidate.confidence >= 0.3:
            confidence_class = 'bg-warning'
        else:
            confidence_class = 'bg-secondary'
        candidates.append({
            'report': report,
            'confidence': candidate.confidence,
            'confidence_pct': confidence_pct,
            'confidence_class': confidence_class,
            'reasoning': candidate.reasoning,
            'is_linked': str(report.get('id')) in linked_ids,
        })

    if wants_json:
        return JsonResponse({
            'success': True,
            'account_name': connection.mailchimp_account_name or connection.mailchimp_login_email,
            'candidates': [
                {
                    'campaign_id': item['report'].get('id'),
                    'campaign_title': item['report'].get('campaign_title') or item['report'].get('id'),
                    'subject_line': item['report'].get('subject_line') or '',
                    'send_time': _format_meta_ads_datetime(item['report'].get('send_time')) or 'Unknown',
                    'emails_sent': item['report'].get('emails_sent') or 0,
                    'confidence': item['confidence'],
                    'confidence_pct': item['confidence_pct'],
                    'confidence_class': item['confidence_class'],
                    'reasoning': item['reasoning'],
                    'is_linked': item['is_linked'],
                }
                for item in candidates
            ],
        })

    return render(request, 'tickets/event_mailchimp_match.html', {
        'event': event,
        'candidates': candidates,
        'account_name': connection.mailchimp_account_name or connection.mailchimp_login_email,
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_mailchimp_apply(request, event_id):
    """Pull campaign report details and upsert them as this event's Mailchimp campaign results."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    wants_json = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    connection = _get_mailchimp_connection(org)
    if not connection:
        if wants_json:
            return JsonResponse({'success': False, 'error': 'Connect Mailchimp before applying campaign results.'}, status=400)
        messages.error(request, 'Connect Mailchimp before applying campaign results.')
        return redirect('tickets:mailchimp_settings')

    campaign_ids = [cid.strip() for cid in request.POST.getlist('campaign_id') if cid.strip()]
    if not campaign_ids:
        if wants_json:
            return JsonResponse({'success': False, 'error': 'Choose at least one Mailchimp campaign to apply.'}, status=400)
        messages.error(request, 'Choose at least one Mailchimp campaign to apply.')
        return redirect('tickets:event_mailchimp_match', event_id=event.id)

    confidences = request.POST.getlist('confidence')
    reasonings = request.POST.getlist('reasoning')

    try:
        client = MailchimpClient(connection.mailchimp_access_token, connection.mailchimp_dc)
        reports = client.list_campaign_reports()
    except MailchimpAPIError as exc:
        if wants_json:
            return JsonResponse({'success': False, 'error': f'Could not load Mailchimp campaigns: {exc}'}, status=502)
        messages.error(request, f'Could not load Mailchimp campaigns: {exc}')
        return redirect('tickets:event_mailchimp_match', event_id=event.id)

    available_ids = {str(item.get('id')) for item in reports}
    unknown_ids = [cid for cid in campaign_ids if cid not in available_ids]
    valid_ids = [cid for cid in campaign_ids if cid in available_ids]
    if unknown_ids and not wants_json:
        messages.error(
            request,
            'These Mailchimp campaigns were not found in this account: ' + ', '.join(unknown_ids),
        )

    added_titles = []
    updated_titles = []
    failed_ids = []
    for index, campaign_id in enumerate(valid_ids):
        confidence = confidences[index] if index < len(confidences) else None
        reasoning = (reasonings[index] if index < len(reasonings) else '').strip()
        try:
            report = client.get_campaign_report(campaign_id)
            email_campaign, created = _save_mailchimp_campaign_from_report(
                event,
                report,
                user=request.user,
                match_confidence=confidence or None,
                match_reasoning=reasoning,
            )
        except MailchimpAPIError as exc:
            logger.warning(
                "Mailchimp apply failed for org=%s event=%s campaign=%s: %s",
                org.id, event.id, campaign_id, exc,
            )
            failed_ids.append(campaign_id)
            continue
        if created:
            added_titles.append(email_campaign.campaign_title)
        else:
            updated_titles.append(email_campaign.campaign_title)

    succeeded = len(added_titles) + len(updated_titles)
    if succeeded == 1:
        only_title = (added_titles + updated_titles)[0]
        verb = 'added' if added_titles else 'updated'
        success_msg = f'Mailchimp campaign "{only_title}" {verb} for this event.'
    elif succeeded:
        parts = []
        if added_titles:
            parts.append(f'added {len(added_titles)}')
        if updated_titles:
            parts.append(f'updated {len(updated_titles)}')
        success_msg = f'Linked {succeeded} Mailchimp campaigns to this event ({", ".join(parts)}).'
    else:
        success_msg = ''

    if wants_json:
        all_linked = list(
            EventEmailCampaign.objects.filter(
                event=event,
                source='mailchimp',
                deleted_at__isnull=True,
            ).order_by('-send_time', '-created_at')
        )
        return JsonResponse({
            'success': succeeded > 0,
            'message': success_msg,
            'failed_ids': failed_ids,
            'unknown_ids': unknown_ids,
            'campaigns': [_serialize_mailchimp_campaign(c, event) for c in all_linked],
        })

    if succeeded:
        messages.success(request, success_msg)

    if failed_ids:
        messages.error(
            request,
            'Could not pull results from Mailchimp for: ' + ', '.join(failed_ids),
        )

    if not succeeded and not unknown_ids and not failed_ids:
        messages.error(request, 'No Mailchimp campaigns were applied.')

    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_mailchimp_refresh_all(request, event_id):
    """Refresh all linked Mailchimp campaign reports for an event."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    connection = _get_mailchimp_connection(org)
    if not connection:
        return JsonResponse({
            'success': False,
            'error': 'Connect Mailchimp before refreshing campaign results.',
        }, status=400)

    email_campaigns = list(
        EventEmailCampaign.objects.filter(
            event=event,
            source='mailchimp',
            deleted_at__isnull=True,
        ).exclude(external_id='')
    )
    client = MailchimpClient(connection.mailchimp_access_token, connection.mailchimp_dc)
    had_error = False
    refreshed_campaigns = []

    for email_campaign in email_campaigns:
        try:
            report = client.get_campaign_report(email_campaign.external_id)
            refreshed, _created = _save_mailchimp_campaign_from_report(event, report, user=request.user)
            refreshed_campaigns.append(refreshed)
        except MailchimpAPIError as exc:
            had_error = True
            logger.warning(
                "Mailchimp bulk refresh failed for org=%s event=%s email_campaign=%s campaign=%s: %s",
                org.id,
                event.id,
                email_campaign.id,
                email_campaign.external_id,
                exc,
            )
            refreshed_campaigns.append(email_campaign)

    refreshed_campaigns.sort(key=lambda item: (item.send_time or django_tz.datetime.min.replace(tzinfo=django_tz.utc), item.created_at), reverse=True)
    return JsonResponse({
        'success': True,
        'had_error': had_error,
        'campaigns': [
            _serialize_mailchimp_campaign(email_campaign, event)
            for email_campaign in refreshed_campaigns
        ],
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_mailchimp_refresh(request, event_id, email_campaign_id):
    """Refresh a single linked Mailchimp campaign report."""
    org = get_organization(request)
    event, email_campaign = _get_active_mailchimp_campaign_or_404(org, event_id, email_campaign_id)
    ajax = _wants_marketing_json(request)
    connection = _get_mailchimp_connection(org)
    if not connection:
        msg = 'Connect Mailchimp before refreshing campaign results.'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=400)
        messages.error(request, msg)
        return redirect('tickets:mailchimp_settings')

    try:
        report = MailchimpClient(connection.mailchimp_access_token, connection.mailchimp_dc).get_campaign_report(email_campaign.external_id)
        refreshed, _created = _save_mailchimp_campaign_from_report(event, report, user=request.user)
    except MailchimpAPIError as exc:
        logger.warning(
            "Mailchimp row refresh failed for org=%s event=%s email_campaign=%s campaign=%s: %s",
            org.id,
            event.id,
            email_campaign.id,
            email_campaign.external_id,
            exc,
        )
        msg = 'Could not refresh this Mailchimp campaign.'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=502)
        messages.warning(request, msg)
        return _marketing_tab_redirect(event)

    if ajax:
        return JsonResponse({'ok': True, 'row': _serialize_email_row(refreshed)})
    messages.success(request, f'Mailchimp campaign "{refreshed.campaign_title}" refreshed.')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_mailchimp_remove(request, event_id, email_campaign_id):
    """Unlink a Mailchimp campaign from an event by soft-deleting its stored results."""
    org = get_organization(request)
    event, email_campaign = _get_active_mailchimp_campaign_or_404(org, event_id, email_campaign_id)

    email_campaign.updated_by = request.user
    email_campaign.save(update_fields=['updated_by'])
    email_campaign.delete()

    if _wants_marketing_json(request):
        return JsonResponse({'ok': True, 'removed_id': str(email_campaign.id)})
    messages.success(request, 'Mailchimp campaign removed from this event.')
    return _marketing_tab_redirect(event)


def _parse_optional_int(raw):
    if raw is None:
        return None
    raw = raw.strip()
    if raw == '':
        return None
    value = int(raw)
    if value < 0:
        raise ValueError('Negative not allowed')
    return value


def _parse_optional_decimal(raw):
    if raw is None:
        return None
    raw = raw.strip()
    if raw == '':
        return None
    value = Decimal(raw)
    if value < 0:
        raise ValueError('Negative not allowed')
    return value.quantize(Decimal('0.01'))


def _wants_marketing_json(request):
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _status_label(obj):
    if obj.needs_review:
        return 'Re-review'
    if obj.is_confirmed:
        return 'Confirmed'
    return 'Pending'


def _status_badge_class(obj):
    if obj.needs_review:
        return 'bg-warning text-dark'
    if obj.is_confirmed:
        return 'bg-success'
    return 'bg-secondary'


def _serialize_email_row(c):
    return {
        'id': str(c.id),
        # Effective (manual override falls back to API) — used for all table cells.
        'effective_emails_sent': c.effective_emails_sent or 0,
        'effective_unique_opens': c.effective_unique_opens or 0,
        'effective_clicks': c.effective_clicks or 0,
        'effective_unsubscribes': c.effective_unsubscribes or 0,
        'effective_orders': c.effective_orders or 0,
        'effective_revenue': f'{(c.effective_revenue or Decimal("0.00")):.2f}',
        # Raw manual values for form prefill / data-numeric-value sync.
        'manual_emails_sent': c.manual_emails_sent,
        'manual_unique_opens': c.manual_unique_opens,
        'manual_clicks': c.manual_clicks,
        'manual_unsubscribes': c.manual_unsubscribes,
        'manual_orders': c.manual_orders,
        'manual_revenue': f'{c.manual_revenue:.2f}' if c.manual_revenue is not None else '',
        'is_confirmed': c.is_confirmed,
        'needs_review': c.needs_review,
        'status_label': _status_label(c),
        'status_badge_class': _status_badge_class(c),
    }


def _serialize_sms_row(c):
    return {
        'id': str(c.id),
        'effective_audience': c.effective_audience or 0,
        'effective_clicks': c.effective_clicks or 0,
        'effective_unsubscribes': c.effective_unsubscribes or 0,
        'effective_orders': c.effective_orders or 0,
        'effective_revenue': f'{(c.effective_revenue or Decimal("0.00")):.2f}',
        'manual_audience': c.manual_audience,
        'manual_clicks': c.manual_clicks,
        'manual_unsubscribes': c.manual_unsubscribes,
        'manual_orders': c.manual_orders,
        'manual_revenue': f'{c.manual_revenue:.2f}' if c.manual_revenue is not None else '',
        'is_confirmed': c.is_confirmed,
        'needs_review': c.needs_review,
        'status_label': _status_label(c),
        'status_badge_class': _status_badge_class(c),
    }


def _serialize_ads_row(e):
    return {
        'id': str(e.id),
        'amount': f'{(e.amount or Decimal("0.00")):.2f}',
        'effective_attributed_orders': e.effective_attributed_orders or 0,
        'effective_attributed_revenue': f'{(e.effective_attributed_revenue or Decimal("0.00")):.2f}',
        'manual_attributed_orders': e.manual_attributed_orders,
        'manual_attributed_revenue': f'{e.manual_attributed_revenue:.2f}' if e.manual_attributed_revenue is not None else '',
        'is_confirmed': e.is_confirmed,
        'needs_review': e.needs_review,
        'status_label': _status_label(e),
        'status_badge_class': _status_badge_class(e),
    }


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_mailchimp_metrics_edit(request, event_id, email_campaign_id):
    """Set or clear manual_clicks/manual_orders/manual_revenue on a Mailchimp campaign.

    Partial update: only fields present in POST are written. Empty string clears
    (sets to NULL → fall back to API). Missing key leaves the field unchanged.
    """
    org = get_organization(request)
    event, email_campaign = _get_active_mailchimp_campaign_or_404(org, event_id, email_campaign_id)
    ajax = _wants_marketing_json(request)
    update_fields = []
    int_fields = ['manual_emails_sent', 'manual_unique_opens', 'manual_clicks', 'manual_unsubscribes', 'manual_orders']
    try:
        for field in int_fields:
            if field in request.POST:
                setattr(email_campaign, field, _parse_optional_int(request.POST.get(field)))
                update_fields.append(field)
        if 'manual_revenue' in request.POST:
            email_campaign.manual_revenue = _parse_optional_decimal(request.POST.get('manual_revenue'))
            update_fields.append('manual_revenue')
    except (ValueError, InvalidOperation):
        msg = 'Enter non-negative numbers (or leave a field blank to use the API value).'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=400)
        messages.error(request, msg)
        return _marketing_tab_redirect(event)
    if not update_fields:
        if ajax:
            return JsonResponse({'ok': True, 'row': _serialize_email_row(email_campaign)})
        return _marketing_tab_redirect(event)
    email_campaign.updated_by = request.user
    update_fields += ['updated_by', 'updated_at']
    email_campaign.save(update_fields=update_fields)
    _invalidate_marketing_cache(org)
    if ajax:
        return JsonResponse({'ok': True, 'row': _serialize_email_row(email_campaign)})
    messages.success(request, f'Updated metrics for "{email_campaign.campaign_title}".')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_mailchimp_confirm(request, event_id, email_campaign_id):
    org = get_organization(request)
    event, email_campaign = _get_active_mailchimp_campaign_or_404(org, event_id, email_campaign_id)
    email_campaign.confirmed_at = django_tz.now()
    email_campaign.confirmed_by = request.user
    email_campaign.updated_by = request.user
    email_campaign.save(update_fields=['confirmed_at', 'confirmed_by', 'updated_by', 'updated_at'])
    _invalidate_marketing_cache(org)
    if _wants_marketing_json(request):
        return JsonResponse({'ok': True, 'row': _serialize_email_row(email_campaign)})
    messages.success(request, f'Confirmed "{email_campaign.campaign_title}".')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_mailchimp_unconfirm(request, event_id, email_campaign_id):
    org = get_organization(request)
    event, email_campaign = _get_active_mailchimp_campaign_or_404(org, event_id, email_campaign_id)
    email_campaign.confirmed_at = None
    email_campaign.confirmed_by = None
    email_campaign.updated_by = request.user
    email_campaign.save(update_fields=['confirmed_at', 'confirmed_by', 'updated_by', 'updated_at'])
    _invalidate_marketing_cache(org)
    if _wants_marketing_json(request):
        return JsonResponse({'ok': True, 'row': _serialize_email_row(email_campaign)})
    messages.success(request, f'Removed "{email_campaign.campaign_title}" from reports.')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_slicktext_metrics_edit(request, event_id, sms_campaign_id):
    """Partial update of manual_clicks/manual_orders/manual_revenue."""
    org = get_organization(request)
    event, sms_campaign = _get_active_slicktext_campaign_or_404(org, event_id, sms_campaign_id)
    ajax = _wants_marketing_json(request)
    update_fields = []
    int_fields = ['manual_audience', 'manual_clicks', 'manual_unsubscribes', 'manual_orders']
    try:
        for field in int_fields:
            if field in request.POST:
                setattr(sms_campaign, field, _parse_optional_int(request.POST.get(field)))
                update_fields.append(field)
        if 'manual_revenue' in request.POST:
            sms_campaign.manual_revenue = _parse_optional_decimal(request.POST.get('manual_revenue'))
            update_fields.append('manual_revenue')
    except (ValueError, InvalidOperation):
        msg = 'Enter non-negative numbers (or leave a field blank to use the API value).'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=400)
        messages.error(request, msg)
        return _marketing_tab_redirect(event)
    if not update_fields:
        if ajax:
            return JsonResponse({'ok': True, 'row': _serialize_sms_row(sms_campaign)})
        return _marketing_tab_redirect(event)
    sms_campaign.updated_by = request.user
    update_fields += ['updated_by', 'updated_at']
    sms_campaign.save(update_fields=update_fields)
    _invalidate_marketing_cache(org)
    if ajax:
        return JsonResponse({'ok': True, 'row': _serialize_sms_row(sms_campaign)})
    messages.success(request, f'Updated metrics for "{sms_campaign.name}".')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_slicktext_confirm(request, event_id, sms_campaign_id):
    org = get_organization(request)
    event, sms_campaign = _get_active_slicktext_campaign_or_404(org, event_id, sms_campaign_id)
    sms_campaign.confirmed_at = django_tz.now()
    sms_campaign.confirmed_by = request.user
    sms_campaign.updated_by = request.user
    sms_campaign.save(update_fields=['confirmed_at', 'confirmed_by', 'updated_by', 'updated_at'])
    _invalidate_marketing_cache(org)
    if _wants_marketing_json(request):
        return JsonResponse({'ok': True, 'row': _serialize_sms_row(sms_campaign)})
    messages.success(request, f'Confirmed "{sms_campaign.name}".')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_slicktext_unconfirm(request, event_id, sms_campaign_id):
    org = get_organization(request)
    event, sms_campaign = _get_active_slicktext_campaign_or_404(org, event_id, sms_campaign_id)
    sms_campaign.confirmed_at = None
    sms_campaign.confirmed_by = None
    sms_campaign.updated_by = request.user
    sms_campaign.save(update_fields=['confirmed_at', 'confirmed_by', 'updated_by', 'updated_at'])
    _invalidate_marketing_cache(org)
    if _wants_marketing_json(request):
        return JsonResponse({'ok': True, 'row': _serialize_sms_row(sms_campaign)})
    messages.success(request, f'Removed "{sms_campaign.name}" from reports.')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_meta_ads_metrics_edit(request, event_id, expense_id):
    """Partial update of manual_attributed_orders / manual_attributed_revenue."""
    org = get_organization(request)
    event, expense = _get_active_meta_ads_expense_or_404(org, event_id, expense_id)
    ajax = _wants_marketing_json(request)
    update_fields = []
    try:
        if 'manual_attributed_orders' in request.POST:
            expense.manual_attributed_orders = _parse_optional_int(request.POST.get('manual_attributed_orders'))
            update_fields.append('manual_attributed_orders')
        if 'manual_attributed_revenue' in request.POST:
            expense.manual_attributed_revenue = _parse_optional_decimal(request.POST.get('manual_attributed_revenue'))
            update_fields.append('manual_attributed_revenue')
    except (ValueError, InvalidOperation):
        msg = 'Enter non-negative numbers for attributed orders and revenue.'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=400)
        messages.error(request, msg)
        return _marketing_tab_redirect(event)
    if not update_fields:
        if ajax:
            return JsonResponse({'ok': True, 'row': _serialize_ads_row(expense)})
        return _marketing_tab_redirect(event)
    expense.updated_by = request.user
    update_fields += ['updated_by', 'updated_at']
    expense.save(update_fields=update_fields)
    _invalidate_marketing_cache(org)
    if ajax:
        return JsonResponse({'ok': True, 'row': _serialize_ads_row(expense)})
    messages.success(request, 'Updated ad attribution.')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_meta_ads_confirm(request, event_id, expense_id):
    org = get_organization(request)
    event, expense = _get_active_meta_ads_expense_or_404(org, event_id, expense_id)
    expense.confirmed_at = django_tz.now()
    expense.confirmed_by = request.user
    expense.updated_by = request.user
    expense.save(update_fields=['confirmed_at', 'confirmed_by', 'updated_by', 'updated_at'])
    _invalidate_marketing_cache(org)
    if _wants_marketing_json(request):
        return JsonResponse({'ok': True, 'row': _serialize_ads_row(expense)})
    messages.success(request, 'Confirmed ad spend.')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_meta_ads_unconfirm(request, event_id, expense_id):
    org = get_organization(request)
    event, expense = _get_active_meta_ads_expense_or_404(org, event_id, expense_id)
    expense.confirmed_at = None
    expense.confirmed_by = None
    expense.updated_by = request.user
    expense.save(update_fields=['confirmed_at', 'confirmed_by', 'updated_by', 'updated_at'])
    _invalidate_marketing_cache(org)
    if _wants_marketing_json(request):
        return JsonResponse({'ok': True, 'row': _serialize_ads_row(expense)})
    messages.success(request, 'Removed ad spend from reports.')
    return _marketing_tab_redirect(event)


def _confirm_all_channel(request, event_id, model_cls, extra_filter, serialize_row, label):
    """Shared helper: bulk-confirm all unconfirmed rows of one channel on an event."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    now = django_tz.now()

    pending_qs = model_cls.objects.filter(
        event=event, deleted_at__isnull=True, confirmed_at__isnull=True,
        **extra_filter,
    )
    pending_ids = list(pending_qs.values_list('pk', flat=True))
    if pending_ids:
        model_cls.objects.filter(pk__in=pending_ids).update(
            confirmed_at=now, confirmed_by=request.user, updated_by=request.user, updated_at=now,
        )
        _invalidate_marketing_cache(org)

    if _wants_marketing_json(request):
        # Re-fetch so the serializers reflect the new confirmed_at.
        rows = [serialize_row(obj) for obj in model_cls.objects.filter(pk__in=pending_ids)]
        return JsonResponse({'ok': True, 'rows': rows, 'count': len(rows)})

    if pending_ids:
        messages.success(request, f'Confirmed {len(pending_ids)} {label}.')
    else:
        messages.info(request, f'Nothing to confirm — every {label} is already confirmed.')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_mailchimp_confirm_all(request, event_id):
    """Confirm every unconfirmed Mailchimp campaign on the event."""
    return _confirm_all_channel(
        request, event_id, EventEmailCampaign,
        extra_filter={'source': 'mailchimp'},
        serialize_row=_serialize_email_row,
        label='Mailchimp campaign(s)',
    )


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_slicktext_confirm_all(request, event_id):
    """Confirm every unconfirmed SlickText broadcast on the event."""
    return _confirm_all_channel(
        request, event_id, EventSMSCampaign,
        extra_filter={'source': 'slicktext'},
        serialize_row=_serialize_sms_row,
        label='SlickText broadcast(s)',
    )


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_meta_ads_confirm_all(request, event_id):
    """Confirm every unconfirmed Meta Ads expense on the event."""
    return _confirm_all_channel(
        request, event_id, EventExpense,
        extra_filter={'source': 'meta_ads'},
        serialize_row=_serialize_ads_row,
        label='Meta Ads expense(s)',
    )


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def event_slicktext_match(request, event_id):
    """Rank SlickText broadcasts that likely correspond to this event."""
    org = get_organization(request)
    wants_json = request.GET.get('format') == 'json'
    event = get_object_or_404(
        Event.objects.filter(organization=org).select_related('venue'),
        id=event_id,
    )
    connection = _get_slicktext_connection(org)
    if not connection:
        if wants_json:
            return JsonResponse({
                'success': False,
                'error': 'Connect SlickText before matching campaigns.',
            }, status=400)
        messages.error(request, 'Connect SlickText before matching campaigns.')
        return redirect('tickets:slicktext_settings')

    try:
        client = SlickTextClient(connection.slicktext_api_key, connection.slicktext_brand_id)
        campaigns = client.list_campaigns()
        match_result = SlickTextCampaignMatcher(org).rank(event, campaigns)
    except SlickTextAPIError as exc:
        if wants_json:
            return JsonResponse({'success': False, 'error': f'Could not load SlickText campaigns: {exc}'}, status=502)
        messages.error(request, f'Could not load SlickText campaigns: {exc}')
        return redirect('tickets:event_detail', event_id=event.id)
    except Exception as exc:
        logger.exception("SlickText campaign matching failed for event %s: %s", event.id, exc)
        if wants_json:
            return JsonResponse({
                'success': False,
                'error': 'Could not rank SlickText campaigns. Please check your OpenAI configuration and try again.',
            }, status=500)
        messages.error(request, 'Could not rank SlickText campaigns. Please check your OpenAI configuration and try again.')
        return redirect('tickets:event_detail', event_id=event.id)

    linked_ids = set(
        EventSMSCampaign.objects.filter(
            event=event,
            source='slicktext',
            deleted_at__isnull=True,
        ).exclude(external_id='').values_list('external_id', flat=True)
    )

    campaigns_by_id = {str(campaign.get('campaign_id') or campaign.get('id')): campaign for campaign in campaigns}
    candidates = []
    for candidate in match_result.candidates:
        campaign = campaigns_by_id.get(candidate.campaign_id)
        if not campaign:
            continue
        confidence_pct = int(round(candidate.confidence * 100))
        if candidate.confidence >= 0.7:
            confidence_class = 'bg-success'
        elif candidate.confidence >= 0.3:
            confidence_class = 'bg-warning'
        else:
            confidence_class = 'bg-secondary'
        external_id = str(campaign.get('campaign_id') or campaign.get('id'))
        candidates.append({
            'campaign': campaign,
            'confidence': candidate.confidence,
            'confidence_pct': confidence_pct,
            'confidence_class': confidence_class,
            'reasoning': candidate.reasoning,
            'is_linked': external_id in linked_ids,
        })

    if wants_json:
        return JsonResponse({
            'success': True,
            'brand_name': connection.slicktext_brand_name or connection.slicktext_brand_id,
            'candidates': [
                {
                    'campaign_id': str(item['campaign'].get('campaign_id') or item['campaign'].get('id')),
                    'name': item['campaign'].get('name') or '',
                    'message': item['campaign'].get('body') or item['campaign'].get('message') or '',
                    'send_time': _format_meta_ads_datetime(
                        item['campaign'].get('finished')
                        or item['campaign'].get('started')
                        or item['campaign'].get('scheduled')
                    ) or 'Unknown',
                    'audience_size': item['campaign'].get('audience_size') or 0,
                    'status': item['campaign'].get('status') or '',
                    'confidence': item['confidence'],
                    'confidence_pct': item['confidence_pct'],
                    'confidence_class': item['confidence_class'],
                    'reasoning': item['reasoning'],
                    'is_linked': item['is_linked'],
                }
                for item in candidates
            ],
        })

    return render(request, 'tickets/event_slicktext_match.html', {
        'event': event,
        'candidates': candidates,
        'brand_name': connection.slicktext_brand_name or connection.slicktext_brand_id,
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_slicktext_apply(request, event_id):
    """Pull SlickText campaign + analytics for each chosen ID and upsert as EventSMSCampaign rows."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    wants_json = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    connection = _get_slicktext_connection(org)
    if not connection:
        if wants_json:
            return JsonResponse({'success': False, 'error': 'Connect SlickText before applying campaign results.'}, status=400)
        messages.error(request, 'Connect SlickText before applying campaign results.')
        return redirect('tickets:slicktext_settings')

    campaign_ids = [cid.strip() for cid in request.POST.getlist('campaign_id') if cid.strip()]
    if not campaign_ids:
        if wants_json:
            return JsonResponse({'success': False, 'error': 'Choose at least one SlickText broadcast to apply.'}, status=400)
        messages.error(request, 'Choose at least one SlickText broadcast to apply.')
        return redirect('tickets:event_slicktext_match', event_id=event.id)

    confidences = request.POST.getlist('confidence')
    reasonings = request.POST.getlist('reasoning')

    client = SlickTextClient(connection.slicktext_api_key, connection.slicktext_brand_id)
    added_titles = []
    updated_titles = []
    failed_ids = []
    for index, campaign_id in enumerate(campaign_ids):
        confidence = confidences[index] if index < len(confidences) else None
        reasoning = (reasonings[index] if index < len(reasonings) else '').strip()
        try:
            report = _slicktext_fetch_campaign_with_analytics(client, campaign_id)
            sms_campaign, created = _save_slicktext_campaign_from_report(
                event,
                report,
                user=request.user,
                match_confidence=confidence or None,
                match_reasoning=reasoning,
            )
        except SlickTextAPIError as exc:
            logger.warning(
                "SlickText apply failed for org=%s event=%s campaign=%s: %s",
                org.id, event.id, campaign_id, exc,
            )
            failed_ids.append(campaign_id)
            continue
        if created:
            added_titles.append(sms_campaign.name)
        else:
            updated_titles.append(sms_campaign.name)

    succeeded = len(added_titles) + len(updated_titles)
    if succeeded == 1:
        only_title = (added_titles + updated_titles)[0]
        verb = 'added' if added_titles else 'updated'
        success_msg = f'SlickText broadcast "{only_title}" {verb} for this event.'
    elif succeeded:
        parts = []
        if added_titles:
            parts.append(f'added {len(added_titles)}')
        if updated_titles:
            parts.append(f'updated {len(updated_titles)}')
        success_msg = f'Linked {succeeded} SlickText broadcasts to this event ({", ".join(parts)}).'
    else:
        success_msg = ''

    if wants_json:
        all_linked = list(
            EventSMSCampaign.objects.filter(
                event=event,
                source='slicktext',
                deleted_at__isnull=True,
            ).order_by('-send_time', '-created_at')
        )
        return JsonResponse({
            'success': succeeded > 0,
            'message': success_msg,
            'added_count': len(added_titles),
            'updated_count': len(updated_titles),
            'failed_ids': failed_ids,
            'campaigns': [_serialize_slicktext_campaign(item, event) for item in all_linked],
        })

    if succeeded:
        messages.success(request, success_msg)

    if failed_ids:
        messages.error(
            request,
            'Could not pull results from SlickText for: ' + ', '.join(failed_ids),
        )

    if not succeeded and not failed_ids:
        messages.error(request, 'No SlickText broadcasts were applied.')

    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_slicktext_refresh_all(request, event_id):
    """Refresh all linked SlickText campaign analytics for an event."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    connection = _get_slicktext_connection(org)
    if not connection:
        return JsonResponse({
            'success': False,
            'error': 'Connect SlickText before refreshing campaign results.',
        }, status=400)

    sms_campaigns = list(
        EventSMSCampaign.objects.filter(
            event=event,
            source='slicktext',
            deleted_at__isnull=True,
        ).exclude(external_id='')
    )
    client = SlickTextClient(connection.slicktext_api_key, connection.slicktext_brand_id)
    had_error = False
    refreshed = []

    for sms_campaign in sms_campaigns:
        try:
            report = _slicktext_fetch_campaign_with_analytics(client, sms_campaign.external_id)
            refreshed_row, _created = _save_slicktext_campaign_from_report(event, report, user=request.user)
            refreshed.append(refreshed_row)
        except SlickTextAPIError as exc:
            had_error = True
            logger.warning(
                "SlickText bulk refresh failed for org=%s event=%s sms_campaign=%s campaign=%s: %s",
                org.id, event.id, sms_campaign.id, sms_campaign.external_id, exc,
            )
            refreshed.append(sms_campaign)

    refreshed.sort(
        key=lambda item: (item.send_time or django_tz.datetime.min.replace(tzinfo=django_tz.utc), item.created_at),
        reverse=True,
    )
    return JsonResponse({
        'success': True,
        'had_error': had_error,
        'campaigns': [_serialize_slicktext_campaign(item, event) for item in refreshed],
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_slicktext_refresh(request, event_id, sms_campaign_id):
    """Refresh a single linked SlickText campaign."""
    org = get_organization(request)
    event, sms_campaign = _get_active_slicktext_campaign_or_404(org, event_id, sms_campaign_id)
    ajax = _wants_marketing_json(request)
    connection = _get_slicktext_connection(org)
    if not connection:
        msg = 'Connect SlickText before refreshing campaign results.'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=400)
        messages.error(request, msg)
        return redirect('tickets:slicktext_settings')

    try:
        client = SlickTextClient(connection.slicktext_api_key, connection.slicktext_brand_id)
        report = _slicktext_fetch_campaign_with_analytics(client, sms_campaign.external_id)
        refreshed, _created = _save_slicktext_campaign_from_report(event, report, user=request.user)
    except SlickTextAPIError as exc:
        logger.warning(
            "SlickText row refresh failed for org=%s event=%s sms_campaign=%s campaign=%s: %s",
            org.id, event.id, sms_campaign.id, sms_campaign.external_id, exc,
        )
        msg = 'Could not refresh this SlickText broadcast.'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=502)
        messages.warning(request, msg)
        return _marketing_tab_redirect(event)

    if ajax:
        return JsonResponse({'ok': True, 'row': _serialize_sms_row(refreshed)})
    messages.success(request, f'SlickText broadcast "{refreshed.name}" refreshed.')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_slicktext_remove(request, event_id, sms_campaign_id):
    """Unlink a SlickText broadcast from an event by soft-deleting its stored results."""
    org = get_organization(request)
    event, sms_campaign = _get_active_slicktext_campaign_or_404(org, event_id, sms_campaign_id)

    sms_campaign.updated_by = request.user
    sms_campaign.save(update_fields=['updated_by'])
    sms_campaign.delete()

    if _wants_marketing_json(request):
        return JsonResponse({'ok': True, 'removed_id': str(sms_campaign.id)})
    messages.success(request, 'SlickText broadcast removed from this event.')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["GET", "POST"])
def expense_create(request, event_id):
    """Add a new expense to an event."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    if request.method == 'POST':
        form = EventExpenseForm(request.POST)
        if form.is_valid():
            expense = form.save(commit=False)
            expense.event = event
            expense.created_by = request.user
            expense.save()
            _invalidate_event_list_cache(org)
            _invalidate_marketing_cache(org)
            messages.success(request, f'Expense "${expense.description}" added.')
            return redirect('tickets:event_detail', event_id=event.id)
    else:
        form = EventExpenseForm(initial={'expense_date': event.start_date})

    return render(request, 'tickets/expense_form.html', {
        'form': form,
        'event': event,
        'editing': False,
    })


@login_required
@require_org
@require_admin
@require_http_methods(["GET", "POST"])
def expense_edit(request, event_id, expense_id):
    """Edit an existing expense."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    expense = get_object_or_404(
        EventExpense.objects.filter(event=event, deleted_at__isnull=True),
        id=expense_id,
    )

    if request.method == 'POST':
        form = EventExpenseForm(request.POST, instance=expense)
        if form.is_valid():
            expense = form.save(commit=False)
            expense.updated_by = request.user
            expense.save()
            _invalidate_event_list_cache(org)
            _invalidate_marketing_cache(org)
            messages.success(request, f'Expense "{expense.description}" updated.')
            return redirect('tickets:event_detail', event_id=event.id)
    else:
        form = EventExpenseForm(instance=expense)

    return render(request, 'tickets/expense_form.html', {
        'form': form,
        'event': event,
        'expense': expense,
        'editing': True,
    })


@login_required
@require_org
@require_admin
@require_http_methods(["GET", "POST"])
def expense_delete(request, event_id, expense_id):
    """Soft-delete an expense."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    expense = get_object_or_404(
        EventExpense.objects.filter(event=event, deleted_at__isnull=True),
        id=expense_id,
    )

    if request.method == 'POST':
        expense.delete()  # soft delete
        _invalidate_event_list_cache(org)
        _invalidate_marketing_cache(org)
        messages.success(request, f'Expense "{expense.description}" deleted.')
        return redirect('tickets:event_detail', event_id=event.id)

    return render(request, 'tickets/expense_delete.html', {
        'event': event,
        'expense': expense,
    })


# ---------------------------------------------------------------------------
# Income Source Management (org-level)
# ---------------------------------------------------------------------------

@login_required
@require_org
@require_admin
def income_source_list(request):
    """List all income source types for the organization."""
    org = get_organization(request)
    sources = IncomeSource.objects.filter(organization=org).order_by('order', 'name')
    context = {'sources': sources}
    return render(request, 'tickets/income_source_list.html', context)


@login_required
@require_org
@require_admin
@require_http_methods(["GET", "POST"])
def income_source_create(request):
    """Create a new income source type."""
    org = get_organization(request)
    if request.method == 'POST':
        form = IncomeSourceForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.organization = org
            obj.save()
            messages.success(request, f"Income source '{obj.name}' created.")
            return redirect('tickets:income_source_list')
    else:
        form = IncomeSourceForm()
    return render(request, 'tickets/income_source_form.html', {
        'form': form,
        'action': 'Create',
    })


@login_required
@require_org
@require_admin
@require_http_methods(["GET", "POST"])
def income_source_edit(request, source_id):
    """Edit an income source type."""
    org = get_organization(request)
    obj = get_object_or_404(IncomeSource.objects.filter(organization=org), id=source_id)
    if request.method == 'POST':
        form = IncomeSourceForm(request.POST, instance=obj)
        if form.is_valid():
            obj = form.save()
            messages.success(request, f"Income source '{obj.name}' updated.")
            return redirect('tickets:income_source_list')
    else:
        form = IncomeSourceForm(instance=obj)
    return render(request, 'tickets/income_source_form.html', {
        'form': form,
        'source': obj,
        'action': 'Edit',
    })


@login_required
@require_org
@require_admin
@require_http_methods(["GET", "POST"])
def income_source_delete(request, source_id):
    """Delete an income source type (only if not used by any event income)."""
    org = get_organization(request)
    obj = get_object_or_404(IncomeSource.objects.filter(organization=org), id=source_id)
    if request.method == 'POST':
        if obj.event_income_lines.filter(deleted_at__isnull=True).exists():
            messages.error(
                request,
                f"Cannot delete '{obj.name}' because it is used by event income entries.",
            )
            return redirect('tickets:income_source_list')
        name = obj.name
        obj.delete()
        messages.success(request, f"Income source '{name}' deleted.")
        return redirect('tickets:income_source_list')
    return render(request, 'tickets/income_source_delete.html', {'source': obj})


# ---------------------------------------------------------------------------
# Event Additional Income Views
# ---------------------------------------------------------------------------

@login_required
@require_org
@require_admin
@require_http_methods(["GET", "POST"])
def event_income_create(request, event_id):
    """Add additional income to an event."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    if request.method == 'POST':
        form = EventIncomeForm(request.POST, organization=org)
        if form.is_valid():
            income = form.save(commit=False)
            income.event = event
            income.created_by = request.user
            income.save()
            _invalidate_event_list_cache(org)
            _invalidate_marketing_cache(org)
            messages.success(request, f"Income '{income.income_source.name}' added.")
            return redirect('tickets:event_detail', event_id=event.id)
    else:
        form = EventIncomeForm(organization=org)
    return render(request, 'tickets/event_income_form.html', {
        'form': form,
        'event': event,
        'editing': False,
    })


@login_required
@require_org
@require_admin
@require_http_methods(["GET", "POST"])
def event_income_edit(request, event_id, income_id):
    """Edit an event additional income entry."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    income = get_object_or_404(
        EventIncome.objects.filter(event=event, deleted_at__isnull=True).select_related('income_source'),
        id=income_id,
    )
    if request.method == 'POST':
        form = EventIncomeForm(request.POST, instance=income, organization=org)
        if form.is_valid():
            income = form.save(commit=False)
            income.updated_by = request.user
            income.save()
            _invalidate_event_list_cache(org)
            _invalidate_marketing_cache(org)
            messages.success(request, f"Income '{income.income_source.name}' updated.")
            return redirect('tickets:event_detail', event_id=event.id)
    else:
        form = EventIncomeForm(instance=income, organization=org)
    return render(request, 'tickets/event_income_form.html', {
        'form': form,
        'event': event,
        'income': income,
        'editing': True,
    })


@login_required
@require_org
@require_admin
@require_http_methods(["GET", "POST"])
def event_income_delete(request, event_id, income_id):
    """Soft-delete an event additional income entry."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    income = get_object_or_404(
        EventIncome.objects.filter(event=event, deleted_at__isnull=True).select_related('income_source'),
        id=income_id,
    )
    if request.method == 'POST':
        source_name = income.income_source.name
        income.delete()
        _invalidate_event_list_cache(org)
        _invalidate_marketing_cache(org)
        messages.success(request, f"Income '{source_name}' deleted.")
        return redirect('tickets:event_detail', event_id=event.id)
    return render(request, 'tickets/event_income_delete.html', {
        'event': event,
        'income': income,
    })


@login_required
@require_org
@require_host
def profitability_overview(request):
    """Analytics page: org-wide P&L stats, chart, and sortable table."""
    org = get_organization(request)
    start_date, end_date, active_window = _parse_window(request)

    events_qs = Event.objects.filter(organization=org)
    if start_date:
        events_qs = events_qs.filter(start_date__gte=start_date)
        if end_date:
            events_qs = events_qs.filter(start_date__lte=end_date)

    events = (
        events_qs
        .annotate(
            total_expenses=Coalesce(
                Subquery(
                    EventExpense.objects.visible().filter(event=OuterRef('pk'))
                    .values('event')
                    .annotate(total=Sum('amount'))
                    .values('total')[:1],
                    output_field=models.DecimalField(max_digits=10, decimal_places=2),
                ),
                Decimal('0.00'),
            ),
            paid_ticket_sum=F('cached_paid_ticket_sum'),
            paid_ticket_count=F('cached_paid_ticket_count'),
        )
        .select_related('venue')
        .order_by('-start_date')
    )

    # Pre-fetch actual Stripe platform fees per event in one query (direct ticketing only)
    stripe_fees_qs = (
        StripeCheckoutSession.objects
        .filter(ticket_order__event__in=events_qs, ticket_order__event__ticketing_type='direct')
        .values('ticket_order__event_id')
        .annotate(total_cents=Coalesce(Sum('platform_fee_cents'), 0))
    )
    stripe_fees_by_event = {
        row['ticket_order__event_id']: Decimal(row['total_cents']) / Decimal('100')
        for row in stripe_fees_qs
    }

    # Summary stats (computed_total_revenue = ticket_revenue + additional_income, signal-maintained)
    summary_revenue = Decimal('0.00')
    summary_expenses = Decimal('0.00')
    summary_fees = Decimal('0.00')
    summary_paid_ticket_sum = Decimal('0.00')
    summary_paid_ticket_count = 0
    event_rows = []
    for e in events:
        total_revenue = e.computed_total_revenue
        fees = stripe_fees_by_event.get(e.pk, Decimal('0.00'))
        net_revenue = total_revenue - fees
        profit = net_revenue - e.total_expenses
        margin = (profit / net_revenue * 100) if net_revenue > 0 else None
        event_rows.append({
            'event': e,
            'revenue': total_revenue,
            'expenses': e.total_expenses,
            'net_revenue': net_revenue,
            'profit': profit,
            'margin': margin,
            'fees': fees,
        })
        summary_revenue += total_revenue
        summary_expenses += e.total_expenses
        summary_fees += fees
        summary_paid_ticket_sum += e.paid_ticket_sum
        summary_paid_ticket_count += e.paid_ticket_count

    summary_net_revenue = summary_revenue - summary_fees
    summary_profit = summary_net_revenue - summary_expenses
    summary_margin = (summary_profit / summary_net_revenue * 100) if summary_net_revenue > 0 else None

    # Market rollup by venue city (sorted high → low for chart)
    markets: dict = {}
    for row in event_rows:
        city = (row['event'].venue.city if row['event'].venue else None) or 'Unknown'
        m = markets.setdefault(city, {
            'city': city, 'revenue': Decimal('0.00'),
            'expenses': Decimal('0.00'), 'profit': Decimal('0.00'),
            'event_count': 0,
        })
        m['revenue'] += row['revenue']
        m['expenses'] += row['expenses']
        m['profit'] += row['profit']
        m['event_count'] += 1
    market_rows = sorted(markets.values(), key=lambda m: m['profit'], reverse=True)

    # Market chart data - sorted high → low by profit
    market_chart_data = {
        'labels': [m['city'] for m in market_rows],
        'profit': [float(m['profit']) for m in market_rows],
    }

    # Monthly aggregation for chart - bucket events by calendar month, ordered earliest → most recent
    chart_events = [r for r in reversed(event_rows) if r['revenue'] > 0 or r['expenses'] > 0]
    month_buckets_profit = {}
    for r in chart_events:
        key = r['event'].start_date.strftime('%Y-%m')
        m = month_buckets_profit.setdefault(key, {'month': key, 'revenue': 0.0, 'expenses': 0.0, 'profit': 0.0})
        m['revenue'] += float(r['revenue'])
        m['expenses'] += float(r['expenses'])
        m['profit'] += float(r['profit'])
    monthly_profit_chart = sorted(month_buckets_profit.values(), key=lambda x: x['month'])
    chart_data = {
        'labels': [m['month'] for m in monthly_profit_chart],
        'revenue': [m['revenue'] for m in monthly_profit_chart],
        'expenses': [m['expenses'] for m in monthly_profit_chart],
        'profit': [m['profit'] for m in monthly_profit_chart],
    }

    # Quarterly aggregation for chart - bucket events by calendar quarter
    quarter_buckets_profit = {}
    for r in chart_events:
        d = r['event'].start_date
        q = (d.month - 1) // 3 + 1
        sort_key = f'{d.year}-Q{q}'
        label = f'Q{q} {d.year}'
        m = quarter_buckets_profit.setdefault(sort_key, {
            'label': label, 'sort_key': sort_key,
            'revenue': 0.0, 'expenses': 0.0, 'profit': 0.0,
        })
        m['revenue'] += float(r['revenue'])
        m['expenses'] += float(r['expenses'])
        m['profit'] += float(r['profit'])
    quarterly_profit_chart = sorted(quarter_buckets_profit.values(), key=lambda x: x['sort_key'])
    quarter_chart_data = {
        'labels': [m['label'] for m in quarterly_profit_chart],
        'revenue': [m['revenue'] for m in quarterly_profit_chart],
        'expenses': [m['expenses'] for m in quarterly_profit_chart],
        'profit': [m['profit'] for m in quarterly_profit_chart],
    }

    # Per-event chart data - ordered earliest → most recent
    event_chart_events = [r for r in reversed(event_rows) if r['revenue'] > 0 or r['expenses'] > 0]
    event_chart_data = {
        'labels': [
            '{} ({})'.format(r['event'].name, r['event'].start_date.strftime('%b %d'))
            for r in event_chart_events
        ],
        'revenue': [float(r['revenue']) for r in event_chart_events],
        'expenses': [float(r['expenses']) for r in event_chart_events],
        'profit': [float(r['profit']) for r in event_chart_events],
    }

    context = {
        'event_rows': event_rows,
        'summary_revenue': summary_revenue,
        'summary_expenses': summary_expenses,
        'summary_fees': summary_fees,
        'summary_net_revenue': summary_net_revenue,
        'summary_profit': summary_profit,
        'summary_margin': summary_margin,
        'chart_data_json': json.dumps(chart_data),
        'event_chart_data_json': json.dumps(event_chart_data),
        'quarter_chart_data_json': json.dumps(quarter_chart_data),
        'market_chart_data_json': json.dumps(market_chart_data),
        'summary_paid_ticket_sum': float(summary_paid_ticket_sum),
        'summary_paid_ticket_count': summary_paid_ticket_count,
        'show_fee_simulator': request.user.is_superuser,
        'active_window': active_window,
        'window_start': start_date or '',
        'window_end': end_date or '',
        'window_choices': WINDOW_CHOICES,
    }
    return render(request, 'tickets/profitability_overview.html', context)


# ---------------------------------------------------------------------------
# Expense Analytics
# ---------------------------------------------------------------------------

@login_required
@require_host
@require_org
def expense_analytics(request):
    """Configurable chart page: org-wide expense breakdown by time, event, and category."""
    from decimal import Decimal

    org = get_organization(request)
    start_date, end_date, active_window = _parse_window(request)

    selected_cats = request.GET.getlist('cat')
    selected_event_id = request.GET.get('event', '')

    # Base queryset — org-scoped, soft-delete safe, excludes unconfirmed meta_ads
    qs = EventExpense.objects.visible().filter(
        event__organization=org,
    ).select_related('event')

    # Time-series views: use expense_date when set, fall back to event start_date
    qs_dated = qs.annotate(effective_date=Coalesce(F('expense_date'), F('event__start_date')))
    if start_date:
        qs_dated = qs_dated.filter(effective_date__gte=start_date)
    if end_date:
        qs_dated = qs_dated.filter(effective_date__lte=end_date)

    # Event/category views: scope by event start_date (include null-date expenses)
    qs_event = qs
    if start_date:
        qs_event = qs_event.filter(event__start_date__gte=start_date)
    if end_date:
        qs_event = qs_event.filter(event__start_date__lte=end_date)

    if selected_cats:
        qs_dated = qs_dated.filter(category__in=selected_cats)
        qs_event = qs_event.filter(category__in=selected_cats)
    if selected_event_id:
        try:
            import uuid
            uuid.UUID(selected_event_id)
            qs_dated = qs_dated.filter(event__id=selected_event_id)
            qs_event = qs_event.filter(event__id=selected_event_id)
        except (ValueError, AttributeError):
            selected_event_id = ''

    cat_label_map = dict(EventExpense.CATEGORY_CHOICES)

    def _pivot_to_chartjs(rows, bucket_key, bucket_fmt):
        """Pivot {bucket, category, total} rows into Chart.js stacked dataset format."""
        buckets = []
        seen = set()
        for r in rows:
            b = bucket_fmt(r[bucket_key])
            if b not in seen:
                seen.add(b)
                buckets.append(b)

        totals = {}  # {bucket: {cat: amount}}
        for r in rows:
            b = bucket_fmt(r[bucket_key])
            totals.setdefault(b, {})[r['category']] = float(r['total'])

        datasets = []
        for cat, label in EventExpense.CATEGORY_CHOICES:
            if any(cat in totals.get(b, {}) for b in buckets):
                datasets.append({
                    'key': cat,
                    'label': label,
                    'data': [totals.get(b, {}).get(cat, 0) for b in buckets],
                })
        return {'labels': buckets, 'datasets': datasets}

    # --- Monthly data ---
    monthly_rows = list(
        qs_dated
        .annotate(month=TruncMonth('effective_date'))
        .values('month', 'category')
        .annotate(total=Sum('amount'))
        .order_by('month', 'category')
    )
    monthly_data = _pivot_to_chartjs(
        monthly_rows, 'month', lambda d: d.strftime('%Y-%m')
    )

    # --- Quarterly data ---
    quarterly_rows = list(
        qs_dated
        .annotate(quarter=TruncQuarter('effective_date'))
        .values('quarter', 'category')
        .annotate(total=Sum('amount'))
        .order_by('quarter', 'category')
    )

    def _quarter_label(d):
        return f"Q{((d.month - 1) // 3) + 1} {d.year}"

    def _quarter_sort_key(label):
        # "Q2 2025" → "2025-Q2" for correct sort
        parts = label.split()
        return f"{parts[1]}-{parts[0]}"

    quarterly_data = _pivot_to_chartjs(
        quarterly_rows, 'quarter', _quarter_label
    )
    # Re-sort labels chronologically (pivot preserves insertion order which is already sorted)
    quarterly_data['labels'] = sorted(quarterly_data['labels'], key=_quarter_sort_key)
    # Re-align dataset data to sorted labels
    if quarterly_data['labels']:
        raw_totals = {}
        for r in quarterly_rows:
            label = _quarter_label(r['quarter'])
            raw_totals.setdefault(label, {})[r['category']] = float(r['total'])
        for ds in quarterly_data['datasets']:
            ds['data'] = [raw_totals.get(lbl, {}).get(ds['key'], 0) for lbl in quarterly_data['labels']]

    # --- Event data ---
    event_rows_raw = list(
        qs_event
        .values('event__id', 'event__name', 'event__start_date', 'category')
        .annotate(total=Sum('amount'))
        .order_by('event__start_date', 'event__name', 'category')
    )

    seen_events = {}
    for r in event_rows_raw:
        eid = str(r['event__id'])
        if eid not in seen_events:
            seen_events[eid] = {
                'label': '{} ({})'.format(r['event__name'], r['event__start_date'].strftime('%b %d')) if r['event__start_date'] else r['event__name'],
                'start_date': r['event__start_date'] or '',
            }
    event_id_order = sorted(seen_events.keys(), key=lambda k: (seen_events[k]['start_date'] or '', seen_events[k]['label']))

    event_totals = {}
    for r in event_rows_raw:
        eid = str(r['event__id'])
        event_totals.setdefault(eid, {})[r['category']] = float(r['total'])

    event_data = {
        'labels': [seen_events[eid]['label'] for eid in event_id_order],
        'datasets': [
            {
                'key': cat,
                'label': label,
                'data': [event_totals.get(eid, {}).get(cat, 0) for eid in event_id_order],
            }
            for cat, label in EventExpense.CATEGORY_CHOICES
            if any(cat in event_totals.get(eid, {}) for eid in event_id_order)
        ],
    }

    # --- Category totals ---
    cat_rows = list(
        qs_event
        .values('category')
        .annotate(total=Sum('amount'))
        .order_by('-total')
    )
    category_totals_data = {
        'labels': [cat_label_map.get(r['category'], r['category']) for r in cat_rows],
        'keys': [r['category'] for r in cat_rows],
        'data': [float(r['total']) for r in cat_rows],
    }

    # --- Summary stats ---
    agg = qs_event.aggregate(
        total=Coalesce(Sum('amount'), Decimal('0.00')),
        count=Count('id'),
    )
    summary_total = agg['total']
    summary_count = agg['count']

    largest_cat = cat_rows[0] if cat_rows else None
    largest_cat_label = cat_label_map.get(largest_cat['category']) if largest_cat else None
    largest_cat_amount = largest_cat['total'] if largest_cat else Decimal('0.00')

    most_expensive_event = (
        qs_event
        .values('event__id', 'event__name', 'event__start_date')
        .annotate(total=Sum('amount'))
        .order_by('-total')
        .first()
    )

    # --- Event filter dropdown ---
    filter_events_qs = Event.objects.filter(organization=org)
    if start_date:
        filter_events_qs = filter_events_qs.filter(start_date__gte=start_date)
    if end_date:
        filter_events_qs = filter_events_qs.filter(start_date__lte=end_date)
    filter_events = list(filter_events_qs.order_by('-start_date').values('id', 'name', 'start_date'))

    has_data = bool(cat_rows)

    context = {
        'monthly_data_json': json.dumps(monthly_data, default=str),
        'quarterly_data_json': json.dumps(quarterly_data, default=str),
        'event_data_json': json.dumps(event_data, default=str),
        'category_totals_json': json.dumps(category_totals_data, default=str),
        'summary_total': summary_total,
        'summary_count': summary_count,
        'largest_cat_label': largest_cat_label,
        'largest_cat_amount': largest_cat_amount,
        'most_expensive_event_name': most_expensive_event['event__name'] if most_expensive_event else None,
        'most_expensive_event_date': most_expensive_event['event__start_date'] if most_expensive_event else None,
        'most_expensive_event_total': most_expensive_event['total'] if most_expensive_event else Decimal('0.00'),
        'active_window': active_window,
        'window_start': start_date or '',
        'window_end': end_date or '',
        'window_choices': WINDOW_CHOICES,
        'category_choices': EventExpense.CATEGORY_CHOICES,
        'selected_cats': selected_cats,
        'filter_events': filter_events,
        'selected_event_id': selected_event_id,
        'has_data': has_data,
    }
    return render(request, 'tickets/expense_analytics.html', context)


# ---------------------------------------------------------------------------
# Survey views
# ---------------------------------------------------------------------------

def _get_survey_questions_for_event(event):
    """Return active survey questions for an event.

    Priority: event-specific > org defaults > system defaults.
    """
    # 1. Event-specific questions
    event_questions = SurveyQuestion.objects.filter(
        event=event, is_active=True
    ).order_by('position')
    if event_questions.exists():
        return event_questions

    # 2. Organization defaults
    org_questions = SurveyQuestion.objects.filter(
        organization=event.organization, event__isnull=True, is_active=True
    ).order_by('position')
    if org_questions.exists():
        return org_questions

    # 3. System defaults (no event, no org)
    return SurveyQuestion.objects.filter(
        event__isnull=True, organization__isnull=True, is_active=True
    ).order_by('position')


@login_required
@require_org
@require_host
def send_survey(request, event_id):
    """Create survey invitations and dispatch email task. POST only."""
    if request.method != 'POST':
        return redirect('tickets:event_detail', event_id=event_id)

    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    # Get attendees who don't already have an invitation for this event
    existing_customer_ids = SurveyInvitation.objects.filter(
        event=event
    ).values_list('customer_id', flat=True)

    attendees = Customer.objects.filter(
        ticket_orders__event=event, organization=org
    ).distinct().exclude(id__in=existing_customer_ids)

    if not attendees.exists():
        messages.info(request, "All attendees have already been sent a survey for this event.")
        return redirect('tickets:event_detail', event_id=event_id)

    # Create invitations
    invitations = []
    for customer in attendees:
        invitations.append(SurveyInvitation(
            event=event,
            customer=customer,
            organization=org,
            email=customer.email,
        ))
    SurveyInvitation.objects.bulk_create(invitations)
    # bulk_create bypasses post_save signals — invalidate manually
    django_cache.delete(_event_stats_cache_key(event.id))
    _invalidate_event_upload_stats_cache(event.id)

    # Dispatch Celery task
    from .tasks import send_survey_emails_task
    send_survey_emails_task.delay(str(event_id), str(org.id))

    messages.success(
        request,
        f"Survey invitations created for {len(invitations)} attendee(s). Emails are being sent."
    )
    return redirect('tickets:event_detail', event_id=event_id)


def survey_form(request, token):
    """Public survey form - no login required."""
    invitation = get_object_or_404(
        SurveyInvitation.objects.select_related('event', 'event__venue', 'customer'),
        token=token,
    )

    if invitation.completed_at:
        return redirect('tickets:survey_thank_you')

    questions = _get_survey_questions_for_event(invitation.event)

    if request.method == 'POST':
        errors = {}

        # Validate answers
        answers_data = []
        for question in questions:
            field_name = f"question_{question.id}"
            value = request.POST.get(field_name, '').strip()

            if question.is_required and not value:
                errors[field_name] = "This field is required."
                continue

            if not value:
                answers_data.append({'question': question, 'star_rating': None, 'nps_score': None, 'text_answer': ''})
                continue

            if question.question_type == 'star_rating':
                try:
                    rating = int(value)
                    if not (1 <= rating <= 5):
                        raise ValueError
                    answers_data.append({'question': question, 'star_rating': rating, 'nps_score': None, 'text_answer': ''})
                except (ValueError, TypeError):
                    errors[field_name] = "Please select a rating between 1 and 5."
            elif question.question_type == 'nps':
                try:
                    score = int(value)
                    if not (0 <= score <= 10):
                        raise ValueError
                    answers_data.append({'question': question, 'star_rating': None, 'nps_score': score, 'text_answer': ''})
                except (ValueError, TypeError):
                    errors[field_name] = "Please select a score between 0 and 10."
            else:
                answers_data.append({'question': question, 'star_rating': None, 'nps_score': None, 'text_answer': value})

        if not errors:
            with transaction.atomic():
                response = SurveyResponse.objects.create(
                    invitation=invitation,
                    event=invitation.event,
                    customer=invitation.customer,
                    organization=invitation.organization,
                )
                answer_objects = []
                for data in answers_data:
                    answer_objects.append(SurveyAnswer(
                        response=response,
                        question=data['question'],
                        star_rating=data['star_rating'],
                        nps_score=data['nps_score'],
                        text_answer=data['text_answer'],
                    ))
                SurveyAnswer.objects.bulk_create(answer_objects)

                invitation.completed_at = django_tz.now()
                invitation.save(update_fields=['completed_at'])

            return redirect('tickets:survey_thank_you')

        return render(request, 'tickets/survey/survey_form.html', {
            'invitation': invitation,
            'questions': questions,
            'errors': errors,
        })

    return render(request, 'tickets/survey/survey_form.html', {
        'invitation': invitation,
        'questions': questions,
        'errors': {},
    })


def survey_thank_you(request):
    """Public thank-you page after survey submission."""
    return render(request, 'tickets/survey/survey_thank_you.html')


# ---------------------------------------------------------------------------
# Chat Agent Views
# ---------------------------------------------------------------------------

@login_required
@require_org
@require_host
@require_http_methods(["POST"])
def chat_stream(request):
    """SSE endpoint - streams LLM tokens for the chat agent."""
    import uuid as uuid_mod
    from django.http import StreamingHttpResponse
    from .services.chat.agent import ChatAgentService

    org = get_organization(request)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    user_message = body.get('message', '').strip()
    if not user_message:
        return JsonResponse({'error': 'Message is required'}, status=400)

    conversation_id_str = body.get('conversation_id', '')
    try:
        conversation_id = uuid_mod.UUID(conversation_id_str)
    except (ValueError, AttributeError):
        conversation_id = uuid_mod.uuid4()

    agent_service = ChatAgentService(org, request.user)
    response = StreamingHttpResponse(
        agent_service.stream_response(user_message, conversation_id),
        content_type='text/event-stream',
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


@login_required
@require_org
@require_host
def chat_history(request):
    """Return messages for a conversation as JSON."""
    import uuid as uuid_mod
    from .models import ChatMessage

    org = get_organization(request)
    conversation_id_str = request.GET.get('conversation_id', '')

    try:
        conversation_id = uuid_mod.UUID(conversation_id_str)
    except (ValueError, AttributeError):
        return JsonResponse({'messages': []})

    messages_qs = ChatMessage.objects.filter(
        organization=org,
        user=request.user,
        conversation_id=conversation_id,
    ).order_by('created_at').values('role', 'content', 'created_at')

    result = []
    for msg in messages_qs:
        if msg['role'] in ('user', 'assistant'):
            result.append({
                'role': msg['role'],
                'content': msg['content'],
                'created_at': msg['created_at'].isoformat(),
            })

    return JsonResponse({'messages': result})


@login_required
@require_org
@require_host
def chat_conversations(request):
    """List user's recent conversations with their first message."""
    from django.db.models import Min, Max
    from .models import ChatMessage

    org = get_organization(request)

    conversations = ChatMessage.objects.filter(
        organization=org,
        user=request.user,
        role='user',
    ).values('conversation_id').annotate(
        first_message_at=Min('created_at'),
        last_message_at=Max('created_at'),
    ).order_by('-last_message_at')[:20]

    result = []
    for conv in conversations:
        first_msg = ChatMessage.objects.filter(
            conversation_id=conv['conversation_id'],
            role='user',
        ).order_by('created_at').values_list('content', flat=True).first()

        result.append({
            'conversation_id': str(conv['conversation_id']),
            'preview': (first_msg[:80] + '...') if first_msg and len(first_msg) > 80 else (first_msg or ''),
            'last_message_at': conv['last_message_at'].isoformat(),
        })

    return JsonResponse({'conversations': result})


# ---------------------------------------------------------------------------
# Direct Ticket Selling - Organizer Views
# ---------------------------------------------------------------------------

@login_required
@require_org
@require_host
@require_http_methods(["GET", "POST"])
def saleable_ticket_type_create(request, event_id):
    """Create a new SaleableTicketType for an event."""

    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    if request.method == 'POST':
        form = SaleableTicketTypeForm(request.POST)
        form.fields['unlocks_after'].queryset = SaleableTicketType.objects.filter(event=event)
        form.fields['unlocks_after'].empty_label = '- None -'
        tier_formset = SaleableTicketTypeTierFormSet(request.POST)
        if form.is_valid() and tier_formset.is_valid():
            tt = form.save(commit=False)
            tt.event = event
            tt.save()
            tier_formset.instance = tt
            tier_formset.save()
            _invalidate_event_list_cache(org)
            _invalidate_marketing_cache(org)
            messages.success(request, f'Ticket type "{tt.name}" created.')
            from django.urls import reverse
            redirect_url = reverse('tickets:event_edit', kwargs={'event_id': event.id})
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': True, 'redirect': redirect_url})
            return redirect(redirect_url)
        else:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                errors = {field: [str(e) for e in errs] for field, errs in form.errors.items()}
                return JsonResponse({'success': False, 'errors': errors}, status=400)
    else:
        form = SaleableTicketTypeForm()
        form.fields['unlocks_after'].queryset = SaleableTicketType.objects.filter(event=event)
        form.fields['unlocks_after'].empty_label = '- None -'
        tier_formset = SaleableTicketTypeTierFormSet()

    return render(request, 'tickets/saleable_ticket_type_form.html', {
        'form': form,
        'tier_formset': tier_formset,
        'event': event,
        'editing': False,
    })


@login_required
@require_org
@require_host
@require_http_methods(["GET", "POST"])
def saleable_ticket_type_edit(request, event_id, ticket_type_id):
    """Edit an existing SaleableTicketType."""

    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    tt = get_object_or_404(SaleableTicketType.objects.filter(event=event), id=ticket_type_id)
    if request.method == 'POST':
        old_quantity_limit = tt.quantity_limit
        form = SaleableTicketTypeForm(request.POST, instance=tt)
        form.fields['unlocks_after'].queryset = SaleableTicketType.objects.filter(event=event).exclude(pk=tt.pk)
        form.fields['unlocks_after'].empty_label = '- None -'
        tier_formset = SaleableTicketTypeTierFormSet(request.POST, instance=tt)
        if form.is_valid() and tier_formset.is_valid():
            updated = form.save()
            tier_formset.save()
            _invalidate_event_list_cache(org)
            _invalidate_marketing_cache(org)
            # If quantity limit increased (or unlimited added) and waitlist is enabled, notify next person
            new_quantity_limit = updated.quantity_limit
            if updated.waitlist_enabled and (
                new_quantity_limit is not None and (
                    old_quantity_limit is None or new_quantity_limit > old_quantity_limit
                )
            ) and WaitlistEntry.objects.filter(
                ticket_type=updated, notified_at__isnull=True, expired=False, purchased_at__isnull=True
            ).exists():
                from tickets.tasks import notify_next_waitlist_entry
                notify_next_waitlist_entry.delay(str(updated.id))
            messages.success(request, f'Ticket type "{updated.name}" updated.')
            from django.urls import reverse
            redirect_url = reverse('tickets:event_edit', kwargs={'event_id': event.id})
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': True, 'redirect': redirect_url})
            return redirect(redirect_url)
        else:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                errors = {field: [str(e) for e in errs] for field, errs in form.errors.items()}
                return JsonResponse({'success': False, 'errors': errors}, status=400)
    else:
        form = SaleableTicketTypeForm(instance=tt)
        form.fields['unlocks_after'].queryset = SaleableTicketType.objects.filter(event=event).exclude(pk=tt.pk)
        form.fields['unlocks_after'].empty_label = '- None -'
        tier_formset = SaleableTicketTypeTierFormSet(instance=tt)

    return render(request, 'tickets/saleable_ticket_type_form.html', {
        'form': form,
        'tier_formset': tier_formset,
        'event': event,
        'ticket_type': tt,
        'editing': True,
    })


@login_required
@require_org
@require_host
@require_http_methods(["GET"])
def saleable_ticket_type_data(request, event_id, ticket_type_id):
    """Return JSON field values for a SaleableTicketType (used to populate the edit modal)."""

    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    tt = get_object_or_404(SaleableTicketType.objects.filter(event=event), id=ticket_type_id)

    def fmt_dt(dt):
        if dt is None:
            return ''
        # datetime-local inputs expect YYYY-MM-DDTHH:MM
        return dt.strftime('%Y-%m-%dT%H:%M')

    return JsonResponse({
        'name': tt.name,
        'description': tt.description or '',
        'price': str(tt.price),
        'quantity_limit': tt.quantity_limit if tt.quantity_limit is not None else '',
        'max_per_customer': tt.max_per_customer if tt.max_per_customer is not None else '',
        'order': tt.order,
        'sale_start': fmt_dt(tt.sale_start),
        'sale_end': fmt_dt(tt.sale_end),
        'is_active': tt.is_active,
        'is_password_protected': tt.is_password_protected,
        'password': tt.password or '',
        'unlocks_after_id': str(tt.unlocks_after_id) if tt.unlocks_after_id else '',
        'waitlist_enabled': tt.waitlist_enabled,
    })


@login_required
@require_org
@require_host
@require_http_methods(["POST"])
def saleable_ticket_type_toggle(request, event_id, ticket_type_id):
    """Toggle is_active on a SaleableTicketType."""

    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    tt = get_object_or_404(SaleableTicketType.objects.filter(event=event), id=ticket_type_id)
    tt.is_active = not tt.is_active
    tt.save(update_fields=['is_active'])
    status = 'activated' if tt.is_active else 'deactivated'
    messages.success(request, f'"{tt.name}" {status}.')
    return redirect('tickets:event_edit', event_id=event.id)


@login_required
@require_org
@require_host
@require_http_methods(["GET", "POST"])
def saleable_ticket_type_delete(request, event_id, ticket_type_id):
    """Delete a SaleableTicketType (only if no tickets sold)."""

    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    tt = get_object_or_404(SaleableTicketType.objects.filter(event=event), id=ticket_type_id)

    if request.method == 'POST':
        if tt.quantity_sold > 0:
            messages.error(request, f'Cannot delete "{tt.name}" - {tt.quantity_sold} tickets already sold.')
            return redirect('tickets:event_edit', event_id=event.id)
        name = tt.name
        tt.delete()
        _invalidate_event_list_cache(org)
        _invalidate_marketing_cache(org)
        messages.success(request, f'Ticket type "{name}" deleted.')
        return redirect('tickets:event_edit', event_id=event.id)

    return render(request, 'tickets/saleable_ticket_type_confirm_delete.html', {
        'event': event,
        'ticket_type': tt,
    })


@login_required
@require_org
@require_host
@require_http_methods(["POST"])
def event_publish(request, event_id):
    """Transition a direct event from Draft → Live."""

    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org, deleted_at__isnull=True), id=event_id)
    if event.status != EVENT_STATUS_DRAFT:
        messages.error(request, 'Event is not in Draft state.')
        return redirect('tickets:event_detail', event_id=event.id)
    event.status = EVENT_STATUS_LIVE
    event.save(update_fields=['status'])
    _invalidate_event_list_cache(org)
    _invalidate_marketing_cache(org)
    messages.success(request, f'"{event.name}" is now live. The public ticket page is active.')
    return redirect('tickets:event_detail', event_id=event.id)


@login_required
@require_org
@require_host
@require_http_methods(["POST"])
def event_end_sales(request, event_id):
    """Transition a direct event from Live → Ended (terminal, irreversible)."""

    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org, deleted_at__isnull=True), id=event_id)
    if event.status != EVENT_STATUS_LIVE:
        messages.error(request, 'Event is not in Live state.')
        return redirect('tickets:event_detail', event_id=event.id)
    event.status = EVENT_STATUS_ENDED
    event.save(update_fields=['status'])
    _invalidate_event_list_cache(org)
    _invalidate_marketing_cache(org)
    messages.success(request, f'Sales for "{event.name}" have ended.')
    return redirect('tickets:event_detail', event_id=event.id)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_cancel(request, event_id):
    """Cancel a live event and issue Stripe refunds for all completed orders."""
    from django.conf import settings as django_settings
    import stripe as stripe_lib



    org = get_organization(request)
    event = get_object_or_404(
        Event.objects.filter(organization=org, deleted_at__isnull=True),
        id=event_id,
    )

    if event.status != EVENT_STATUS_LIVE:
        messages.error(request, 'Only live events can be cancelled.')
        return redirect('tickets:event_detail', event_id=event.id)

    # Immediately mark cancelled so no new purchases can complete
    with transaction.atomic():
        event.status = EVENT_STATUS_CANCELLED
        event.save(update_fields=['status'])

    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY

    orders = list(
        TicketOrder.objects.filter(event=event, refunded_at__isnull=True)
        .select_related('customer', 'stripe_checkout_session')
    )

    refunded_count = 0
    failed_count = 0
    affected_customer_ids = set()

    for order in orders:
        session = getattr(order, 'stripe_checkout_session', None)
        if session is None or session.status != StripeCheckoutSession.Status.COMPLETED:
            continue

        try:
            stripe_lib.Refund.create(payment_intent=session.stripe_session_id)
        except stripe_lib.error.StripeError as e:
            logger.error('Stripe refund failed for order %s during event cancel: %s', order.id, e)
            failed_count += 1
            continue

        with transaction.atomic():
            order.refunded_at = django_tz.now()
            order.save(update_fields=['refunded_at'])
            session.status = StripeCheckoutSession.Status.REFUNDED
            session.save(update_fields=['status'])
            for item in session.line_items_snapshot:
                tt_id = item.get('saleable_ticket_type_id')
                qty = item.get('quantity', 0)
                if tt_id and qty:
                    SaleableTicketType.objects.filter(id=tt_id).update(
                        quantity_sold=Greatest(F('quantity_sold') - qty, Value(0))
                    )
            affected_customer_ids.add(order.customer_id)
            refunded_count += 1

    for customer_id in affected_customer_ids:
        try:
            Customer.objects.get(id=customer_id).update_lifetime_value()
        except Customer.DoesNotExist:
            pass

    _invalidate_event_list_cache(org)

    _invalidate_marketing_cache(org)

    if failed_count:
        messages.warning(
            request,
            f'"{event.name}" cancelled. {refunded_count} order(s) refunded. '
            f'{failed_count} refund(s) failed - please refund those orders manually.',
        )
    else:
        messages.success(request, f'"{event.name}" cancelled. {refunded_count} order(s) refunded.')
    return redirect('tickets:event_detail', event_id=event.id)


_HEIC_CONTENT_TYPES = {'image/heic', 'image/heif', 'image/heic-sequence', 'image/heif-sequence'}


def _convert_heic_to_jpeg(upload_file):
    """Convert a HEIC/HEIF InMemoryUploadedFile to JPEG. Returns a new InMemoryUploadedFile."""
    from pillow_heif import register_heif_opener
    register_heif_opener()
    from PIL import Image
    img = Image.open(upload_file)
    img = img.convert('RGB')
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=90)
    buf.seek(0)
    name = upload_file.name.rsplit('.', 1)[0] + '.jpg'
    return InMemoryUploadedFile(buf, 'flyer', name, 'image/jpeg', buf.getbuffer().nbytes, None)


@login_required
@require_org
@require_host
@require_http_methods(["POST"])
def event_flyer_upload(request, event_id):
    """Upload or replace event flyer (direct ticketing only). Returns JSON."""
    from django.conf import settings
    # #region agent log
    import json as _json
    _log_path = getattr(settings, 'BASE_DIR', None) and str(settings.BASE_DIR / 'debug-5764fb.log') or 'debug-5764fb.log'
    def _dlog(hid, msg, **data):
        try:
            payload = {"sessionId": "5764fb", "hypothesisId": hid, "location": "views.py:event_flyer_upload", "message": msg, "data": data, "timestamp": int(__import__('time').time() * 1000)}
            line = _json.dumps(payload)
            with open(_log_path, 'a') as _f:
                _f.write(line + '\n')
            logger.warning("[DEBUG-5764fb] %s", line)
        except Exception:
            pass
    # #endregion
    from .models import TICKETING_TYPE_DIRECT

    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    if event.ticketing_type != TICKETING_TYPE_DIRECT:
        return JsonResponse({'success': False, 'error': 'Not a direct ticketing event.'}, status=400)
    file = request.FILES.get('flyer')
    if not file:
        return JsonResponse({'success': False, 'error': 'No file provided.'}, status=400)
    # Validate image (ImageField will reject invalid images on save)
    if not file.content_type.startswith('image/'):
        return JsonResponse({'success': False, 'error': 'File must be an image.'}, status=400)
    # Convert HEIC/HEIF → JPEG for browser compatibility
    if (file.content_type in _HEIC_CONTENT_TYPES
            or file.name.lower().endswith(('.heic', '.heif'))):
        try:
            file = _convert_heic_to_jpeg(file)
        except Exception as e:
            logger.warning("HEIC conversion failed: %s", e)
            return JsonResponse({'success': False, 'error': 'Could not process HEIC image.'}, status=400)
    # #region agent log
    _dlog("H1", "storage config at upload", default_file_storage=getattr(settings, 'DEFAULT_FILE_STORAGE', None), aws_bucket_env=bool(__import__('os').environ.get('AWS_STORAGE_BUCKET_NAME')), aws_bucket_value=__import__('os').environ.get('AWS_STORAGE_BUCKET_NAME', '')[:20] if __import__('os').environ.get('AWS_STORAGE_BUCKET_NAME') else None)
    # #endregion
    try:
        event.flyer = file
        # #region agent log
        _st = getattr(event.flyer, 'storage', None)
        _dlog("H4", "before save", storage_class=_st.__class__.__name__ if _st else None, bucket_name=getattr(_st, 'bucket_name', None), file_name=getattr(file, 'name', None))
        # #endregion
        event.save(update_fields=['flyer'])
        # #region agent log
        _st2 = getattr(event.flyer, 'storage', None)
        _dlog("H3", "after save success", saved_name=event.flyer.name, saved_url=event.flyer.url[:80] if event.flyer.url else None, storage_class=_st2.__class__.__name__ if _st2 else None, bucket_name=getattr(_st2, 'bucket_name', None))
        # #endregion
    except Exception as e:
        # #region agent log
        _dlog("H2", "save exception", exc_type=type(e).__name__, exc_msg=str(e)[:200])
        # #endregion
        logger.warning("Flyer upload failed: %s", e, exc_info=True)
        try:
            from botocore.exceptions import ClientError
            if isinstance(e, ClientError):
                return JsonResponse({
                    'success': False,
                    'error': 'Storage upload failed. Check AWS credentials and S3 permissions (s3:PutObject, s3:PutObjectAcl).',
                }, status=503)
        except ImportError:
            pass
        return JsonResponse({'success': False, 'error': 'Invalid or unsupported image.'}, status=400)
    return JsonResponse({'success': True, 'url': event.flyer.url})


# ---------------------------------------------------------------------------
# Direct Ticket Selling - Public Views
# ---------------------------------------------------------------------------

def buy_redirect(request, event_id):
    """Redirect old /buy/<uuid>/ links to the new short /e/<public_id>/ URL."""
    event = get_object_or_404(Event, id=event_id, deleted_at__isnull=True)
    return redirect('tickets:public_event_buy', public_id=event.public_id, permanent=True)


def _customer_ticket_type_ticket_count(ticket_type, customer_email):
    """Count non-refunded tickets already owned for this ticket type."""
    if not customer_email:
        return 0
    return Ticket.objects.filter(
        ticket_order__event=ticket_type.event,
        ticket_order__refunded_at__isnull=True,
        ticket_order__customer__email=customer_email.strip().lower(),
        ticket_type=ticket_type.name,
    ).count()


def _pending_customer_ticket_type_ticket_count(ticket_type, customer_email, *, exclude_session_id=None):
    """Count tickets held in other pending checkout sessions for this ticket type/email."""
    if not customer_email:
        return 0
    pending_sessions = StripeCheckoutSession.objects.filter(
        event=ticket_type.event,
        buyer_email=customer_email.strip().lower(),
        status=StripeCheckoutSession.Status.PENDING,
    )
    if exclude_session_id is not None:
        pending_sessions = pending_sessions.exclude(id=exclude_session_id)

    total = 0
    for session in pending_sessions.only('line_items_snapshot'):
        total += sum(
            int(item.get('quantity', 0) or 0)
            for item in session.line_items_snapshot
            if str(item.get('saleable_ticket_type_id')) == str(ticket_type.id)
        )
    return total


def _customer_ticket_type_limit_error(ticket_type, customer_email, requested_qty, *, exclude_session_id=None):
    """Return a buyer-facing cap error message, or None when within the ticket-type limit."""
    limit = ticket_type.max_per_customer
    if not limit or not customer_email:
        return None

    owned = _customer_ticket_type_ticket_count(ticket_type, customer_email)
    pending = _pending_customer_ticket_type_ticket_count(
        ticket_type,
        customer_email,
        exclude_session_id=exclude_session_id,
    )
    if owned + pending + requested_qty <= limit:
        return None

    return (
        f'You can only purchase up to {limit} {ticket_type.name} ticket'
        f'{"s" if limit != 1 else ""} for this event. '
        f'You already have {owned} confirmed and {pending} pending.'
    )


def _ticket_type_remaining_by_customer(ticket_types, customer_email):
    """Return remaining per-customer quantity by saleable ticket type id.

    Only counts confirmed (non-refunded) tickets, not pending checkout sessions.
    Pending sessions are intentionally excluded here: stale abandoned sessions would
    otherwise permanently set remaining=0 and break the buy-page quantity selector.
    Checkout-time validation (_customer_ticket_type_limit_error) still checks pending
    sessions to prevent simultaneous double-payment.
    """
    remaining = {}
    if not customer_email:
        return remaining
    for ticket_type in ticket_types:
        if not ticket_type.max_per_customer:
            continue
        owned = _customer_ticket_type_ticket_count(ticket_type, customer_email)
        remaining[str(ticket_type.id)] = max(0, ticket_type.max_per_customer - owned)
    return remaining


def _cart_ticket_type_limit_error(event, customer_email, cart, *, exclude_session_id=None):
    """Return the first ticket-type customer-cap error for the cart, if any."""
    if not customer_email:
        return None

    ticket_type_ids = [
        str(item.get('saleable_ticket_type_id'))
        for item in cart
        if item.get('saleable_ticket_type_id')
    ]
    ticket_types = {
        str(ticket_type.id): ticket_type
        for ticket_type in SaleableTicketType.objects.filter(event=event, id__in=ticket_type_ids)
    }

    for item in cart:
        ticket_type = ticket_types.get(str(item.get('saleable_ticket_type_id')))
        if not ticket_type:
            continue
        requested_qty = int(item.get('quantity', 0) or 0)
        limit_error = _customer_ticket_type_limit_error(
            ticket_type,
            customer_email,
            requested_qty,
            exclude_session_id=exclude_session_id,
        )
        if limit_error:
            return limit_error
    return None


def _build_public_event_preview_context(event, *, suffix):
    """Build consistent title/description metadata for public event link previews."""
    preview_parts = [event.start_date.strftime('%a, %b %-d, %Y')]

    if event.start_time:
        preview_parts.append(event.start_time.strftime('%-I:%M %p'))

    if event.venue:
        venue_label = event.venue.name
        locality_parts = [part for part in [event.venue.city, event.venue.state] if part]
        if locality_parts:
            venue_label = f"{venue_label}, {', '.join(locality_parts)}"
        preview_parts.append(venue_label)

    preview_description = ' · '.join(preview_parts)
    social_preview_title = f"{event.name} · {preview_description}" if preview_description else event.name
    return {
        'preview_description': preview_description,
        'social_preview_title': social_preview_title,
        'preview_title': f"{social_preview_title} · {suffix}" if suffix else social_preview_title,
    }


def public_event_buy(request, public_id):
    """Public ticket selector page. POST stores cart in session and redirects to checkout."""
    event = get_object_or_404(
        Event.objects.select_related('venue', 'organization'),
        public_id=public_id,
        deleted_at__isnull=True,
    )
    eff = event.effective_status
    if eff == EVENT_STATUS_DRAFT:
        raise Http404()
    if eff == EVENT_STATUS_ENDED:
        return render(
            request,
            'tickets/buy/sales_ended.html',
            {
                'event': event,
                **_build_public_event_preview_context(event, suffix='Ticket Sales Ended'),
            },
        )
    if eff == EVENT_STATUS_CANCELLED:
        return render(
            request,
            'tickets/buy/event_cancelled.html',
            {
                'event': event,
                **_build_public_event_preview_context(event, suffix='Event Cancelled'),
            },
        )

    wl_feature_on = event.organization.waitlist_feature_enabled
    if wl_feature_on:
        ticket_types = SaleableTicketType.objects.filter(
            event=event, is_active=True,
        ).select_related('unlocks_after').prefetch_related('tiers', 'unlocks_after__tiers').annotate(
            waitlist_count=Count('waitlist_entries', filter=Q(
                waitlist_entries__purchased_at__isnull=True,
                waitlist_entries__expired=False,
            ))
        ).order_by('order', 'name')
        wl_hold = request.session.get(f'waitlist_hold_{event.id}')
        wl_held_tt_id = wl_hold.get('ticket_type_id') if wl_hold else None
    else:
        ticket_types = SaleableTicketType.objects.filter(
            event=event, is_active=True,
        ).select_related('unlocks_after').prefetch_related('tiers', 'unlocks_after__tiers').order_by('order', 'name')
        wl_held_tt_id = None

    # Purchasable: on sale, not pw-protected, prerequisite sold out (or none); sold-out types included so buyers see them
    available_types   = [tt for tt in ticket_types
                         if tt.is_on_sale() and not tt.is_password_protected and tt.is_unlocked()]

    # Coming soon: on sale, not sold out, not pw-protected, but prerequisite NOT yet sold out
    coming_soon_types = [tt for tt in ticket_types
                         if tt.is_on_sale() and not tt.is_sold_out()
                         and not tt.is_password_protected and not tt.is_unlocked()]

    # Password-protected and unlocked (existing behavior); sold-out types included so buyers see them
    locked_types      = [tt for tt in ticket_types
                         if tt.is_on_sale() and tt.is_password_protected and tt.is_unlocked()]

    # Build waitlist join forms for sold-out + waitlist-enabled types (excluding held)
    if wl_feature_on:
        waitlist_join_forms = {
            str(tt.id): WaitlistJoinForm(prefix=f'wl_{tt.id.hex}')
            for tt in ticket_types
            if tt.is_sold_out() and tt.waitlist_enabled and str(tt.id) != wl_held_tt_id
        }
    else:
        waitlist_join_forms = {}

    all_types = available_types + locked_types
    buyer_email = request.user.email.strip().lower() if request.user.is_authenticated and request.user.email else ''
    per_ticket_remaining = _ticket_type_remaining_by_customer(all_types, buyer_email)

    if request.method == 'POST':
        form = PublicTicketPurchaseForm(
            all_types,
            request.POST,
            per_ticket_remaining=per_ticket_remaining,
        )
        if form.is_valid():
            line_items = form.get_line_items()
            snapshot = []
            for tt, qty in line_items:
                active_tier = tt.get_active_tier()
                snapshot.append({
                    'saleable_ticket_type_id': str(tt.id),
                    'name': tt.name,
                    'price': str(active_tier.price if active_tier else tt.price),
                    'quantity': qty,
                    'tier_id': str(active_tier.id) if active_tier else None,
                    'tier_name': active_tier.name if active_tier else None,
                })
            limit_error = _cart_ticket_type_limit_error(event, buyer_email, snapshot)
            if limit_error:
                form.add_error(None, limit_error)
            else:
                request.session[f'cart_{event.id}'] = snapshot
                return redirect('tickets:checkout_payment', public_id=public_id)
    else:
        Event.objects.filter(pk=event.pk).update(
            public_buy_page_views=F('public_buy_page_views') + 1
        )
        today = django_tz.localdate()
        rows = EventDailyPageView.objects.filter(event=event, date=today).update(
            view_count=F('view_count') + 1
        )
        if rows == 0:
            try:
                EventDailyPageView.objects.create(event=event, date=today, view_count=1)
            except IntegrityError:
                EventDailyPageView.objects.filter(event=event, date=today).update(
                    view_count=F('view_count') + 1
                )
        safe_cache_delete(_event_stats_cache_key(event.pk))
        ref = request.GET.get('ref')
        if ref:
            request.session[f'tracking_ref_{event.id}'] = ref
        form = PublicTicketPurchaseForm(all_types, per_ticket_remaining=per_ticket_remaining)

    all_bound_fields = list(form)
    all_caps         = [per_ticket_remaining.get(str(tt.id)) for tt in all_types]
    all_pairs        = list(zip(all_types, all_bound_fields, all_caps))
    available_pairs  = all_pairs[:len(available_types)]
    locked_pairs     = all_pairs[len(available_types):]

    view_event_id = str(_uuid.uuid4())
    pixel_id = event.facebook_pixel_id
    capi_token = event.organization.meta_capi_access_token
    if pixel_id and capi_token:
        from tickets.services.facebook_capi import send_capi_event
        _client_ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()
        send_capi_event(
            pixel_id, capi_token, 'ViewContent',
            content_ids=[str(tt.id) for tt in all_types],
            client_ip=_client_ip,
            client_user_agent=request.META.get('HTTP_USER_AGENT', ''),
            fbp=request.COOKIES.get('_fbp'),
            fbc=request.COOKIES.get('_fbc'),
            event_id=view_event_id,
            event_source_url=request.build_absolute_uri(),
        )

    all_sold_out = (ticket_types.exists()
                    and not any(not tt.is_sold_out() for tt in available_types + locked_types)
                    and not coming_soon_types)
    min_ticket_price = min((tt.effective_price for tt in all_types), default=None)

    # Social proof: up to 6 distinct confirmed attendees + total ticket count
    _preview_orders = (
        TicketOrder.objects
        .filter(event=event, refunded_at__isnull=True, is_in_person=False)
        .select_related('customer')
        .order_by('-order_date')
    )
    _seen = set()
    attendee_preview = []
    for _order in _preview_orders[:50]:
        if _order.customer_id not in _seen:
            _seen.add(_order.customer_id)
            _name = _order.customer.name or ''
            _parts = _name.split()
            if len(_parts) >= 2:
                _initials = (_parts[0][0] + _parts[-1][0]).upper()
            elif _parts:
                _initials = _parts[0][:2].upper()
            else:
                _initials = '?'
            attendee_preview.append({
                'initials': _initials,
                'first_name': _parts[0] if _parts else '',
            })
            if len(attendee_preview) >= 6:
                break

    attendee_count = Ticket.objects.filter(
        ticket_order__event=event,
        ticket_order__refunded_at__isnull=True,
        ticket_order__is_in_person=False,
    ).count()

    # Sold-out ticket types that have waitlist enabled (for the "sold out" section)
    if wl_feature_on:
        waitlisted_sold_out_types = [
            tt for tt in ticket_types
            if tt.is_sold_out() and tt.waitlist_enabled and str(tt.id) != wl_held_tt_id
        ]
        # Set of ticket type IDs the current user has already joined the waitlist for
        already_on_waitlist = set()
        if request.user.is_authenticated and waitlisted_sold_out_types:
            already_on_waitlist = set(
                WaitlistEntry.objects.filter(
                    ticket_type__in=waitlisted_sold_out_types,
                    email=request.user.email.lower(),
                    purchased_at__isnull=True,
                    expired=False,
                ).values_list('ticket_type_id', flat=True)
            )
    else:
        waitlisted_sold_out_types = []
        already_on_waitlist = set()

    return render(request, 'tickets/buy/public_event_buy.html', {
        'event': event,
        'form': form,
        'available_pairs': available_pairs,
        'locked_pairs': locked_pairs,
        'coming_soon_types': coming_soon_types,
        'waitlisted_sold_out_types': waitlisted_sold_out_types,
        'waitlist_join_forms': waitlist_join_forms,
        'already_on_waitlist': already_on_waitlist,
        'all_sold_out': all_sold_out,
        'min_ticket_price': min_ticket_price,
        'view_event_id': view_event_id,
        'attendee_preview': attendee_preview,
        'attendee_count': attendee_count,
        'wl_held_tt_id': wl_held_tt_id,
        'user_is_authenticated': request.user.is_authenticated,
        **_build_public_event_preview_context(event, suffix='Buy Tickets'),
    })


def unlock_ticket_type(request, public_id, ticket_type_id):
    """AJAX POST: validate password for a password-protected SaleableTicketType."""
    if request.method != 'POST':
        return JsonResponse({'success': False}, status=405)
    event = get_object_or_404(
        Event.objects.filter(deleted_at__isnull=True),
        public_id=public_id,
    )
    tt = get_object_or_404(SaleableTicketType, id=ticket_type_id, event=event, is_active=True)
    if not tt.is_password_protected or not tt.password:
        return JsonResponse({'success': False, 'error': 'No password set.'}, status=400)
    submitted = request.POST.get('password', '')
    if submitted == tt.password:
        return JsonResponse({'success': True})
    return JsonResponse({'success': False, 'error': 'Incorrect password.'})


@require_http_methods(["POST"])
def join_waitlist(request, public_id, ticket_type_id):
    """Public AJAX endpoint - adds the buyer to the waitlist for a sold-out ticket type."""
    if not request.user.is_authenticated:
        return JsonResponse({'success': False, 'error': 'Login required.'}, status=401)

    event = get_object_or_404(Event.objects.select_related('organization'), public_id=public_id, deleted_at__isnull=True)
    if not event.organization.waitlist_feature_enabled:
        raise Http404()
    tt = get_object_or_404(
        SaleableTicketType,
        id=ticket_type_id, event=event, is_active=True, waitlist_enabled=True,
    )

    email = request.user.email.strip().lower()
    name = request.user.get_full_name() or ''

    if WaitlistEntry.objects.filter(
        ticket_type=tt, email=email, purchased_at__isnull=True, expired=False
    ).exists():
        return JsonResponse(
            {'success': False, 'error': 'You are already on the waitlist for this ticket.'}, status=400
        )
    from django.db.models import Max as _Max
    position = (WaitlistEntry.objects.filter(ticket_type=tt).aggregate(
        max_pos=_Max('position')
    )['max_pos'] or 0) + 1
    WaitlistEntry.objects.create(
        ticket_type=tt,
        email=email,
        name=name,
        position=position,
    )
    return JsonResponse({'success': True})


def activate_waitlist_hold(request, public_id, hold_token):
    """Public endpoint - validates a waitlist hold token and sets a session flag."""
    from django.utils import timezone as tz
    entry = get_object_or_404(
        WaitlistEntry, hold_token=hold_token, purchased_at__isnull=True, expired=False
    )
    tt = entry.ticket_type
    event = get_object_or_404(Event.objects.select_related('organization'), public_id=public_id)
    if not event.organization.waitlist_feature_enabled:
        raise Http404()
    if str(tt.event_id) != str(event.id):
        raise Http404()
    if entry.hold_expires_at and tz.now() > entry.hold_expires_at:
        return render(request, 'tickets/buy/waitlist_expired.html', {'event': event})
    request.session[f'waitlist_hold_{event.id}'] = {
        'ticket_type_id': str(tt.id),
        'entry_id': str(entry.id),
    }
    return redirect('tickets:public_event_buy', public_id=public_id)


@require_http_methods(["POST"])
def validate_promo_code(request, public_id):
    """Public AJAX endpoint - validates a promo code and stores it in the session."""
    event = get_object_or_404(Event, public_id=public_id)

    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid request.'}, status=400)

    code = (data.get('code') or '').strip().upper()
    if not code:
        return JsonResponse({'error': 'Please enter a promo code.'}, status=400)

    try:
        promo = PromoCode.objects.get(event=event, code=code, organization=event.organization)
    except PromoCode.DoesNotExist:
        return JsonResponse({'error': 'Promo code not found.'}, status=400)

    if not promo.is_valid():
        if not promo.is_active:
            return JsonResponse({'error': 'This promo code is no longer active.'}, status=400)
        from django.utils import timezone as tz
        if promo.expires_at and tz.now() > promo.expires_at:
            return JsonResponse({'error': 'This promo code has expired.'}, status=400)
        return JsonResponse({'error': 'This promo code has reached its usage limit.'}, status=400)

    cart = request.session.get(f'cart_{event.id}', [])
    subtotal_cents = sum(
        int(Decimal(item['price']) * 100) * item['quantity']
        for item in cart
    )
    discount_cents = promo.calculate_discount_cents(subtotal_cents)

    request.session[f'promo_{event.id}'] = {
        'promo_code_id': str(promo.id),
        'code': promo.code,
        'discount_cents': discount_cents,
    }
    request.session.modified = True

    if promo.discount_type == PromoCode.PERCENTAGE:
        discount_label = f'{promo.discount_value.normalize()}% off'
    else:
        discount_label = f'${(discount_cents / 100):.2f} off'

    return JsonResponse({
        'success': True,
        'code': promo.code,
        'discount_cents': discount_cents,
        'discount_label': discount_label,
    })


@login_required
@require_org
@require_organizer
def promo_code_create(request, event_id):
    """Organizer view to create a promo code for an event."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    if request.method == 'POST':
        form = PromoCodeForm(request.POST)
        if form.is_valid():
            promo = form.save(commit=False)
            promo.organization = org
            promo.event = event
            promo.save()
            messages.success(request, f'Promo code "{promo.code}" created.')
            return redirect('tickets:event_edit', event_id=event_id)
    else:
        form = PromoCodeForm()

    return render(request, 'tickets/promo_code_form.html', {
        'form': form,
        'event': event,
    })


@login_required
@require_org
@require_organizer
@require_http_methods(["POST"])
def promo_code_delete(request, event_id, promo_code_id):
    """Organizer view to delete a promo code."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    promo = get_object_or_404(PromoCode.objects.filter(organization=org, event=event), id=promo_code_id)
    code = promo.code
    promo.delete()
    messages.success(request, f'Promo code "{code}" deleted.')
    return redirect('tickets:event_edit', event_id=event_id)


# ---------------------------------------------------------------------------
# Tracking Links
# ---------------------------------------------------------------------------

def track_link_redirect(request, token):
    """Public redirect that records a click and forwards to the event buy page."""
    link = get_object_or_404(TrackingLink.objects.select_related('event'), token=token)
    TrackingLink.objects.filter(pk=link.pk).update(click_count=models.F('click_count') + 1)
    request.session[f'tracking_ref_{link.event_id}'] = token
    return redirect(f"/e/{link.event.public_id}/?ref={token}")


@login_required
@require_org
@require_organizer
@require_http_methods(["POST"])
def tracking_link_create(request, event_id):
    """Create a new tracking link for a direct-ticketing event."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    if event.ticketing_type != 'direct':
        return HttpResponseBadRequest("Tracking links are only available for direct-ticketing events.")
    name = request.POST.get('name', '').strip()
    if not name or len(name) > 100:
        messages.error(request, 'Link name must be between 1 and 100 characters.')
        return redirect('tickets:event_detail', event_id=event_id)
    token = _generate_tracking_token()
    TrackingLink.objects.create(organization=org, event=event, name=name, token=token)
    messages.success(request, f'Tracking link "{name}" created.')
    return redirect('tickets:event_detail', event_id=event_id)


@login_required
@require_org
@require_organizer
@require_http_methods(["POST"])
def tracking_link_delete(request, event_id, link_id):
    """Delete a tracking link."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    link = get_object_or_404(TrackingLink.objects.filter(organization=org, event=event), id=link_id)
    name = link.name
    link.delete()
    messages.success(request, f'Tracking link "{name}" deleted.')
    return redirect('tickets:event_detail', event_id=event_id)


def checkout_payment(request, public_id):
    """Custom checkout page - collects buyer info and processes payment via Stripe Elements."""
    from django.conf import settings as django_settings

    event = get_object_or_404(
        Event.objects.select_related('venue', 'organization'),
        public_id=public_id,
    )

    if event.effective_status != EVENT_STATUS_LIVE:
        request.session.pop(f'cart_{event.id}', None)
        return redirect('tickets:public_event_buy', public_id=public_id)

    cart = request.session.get(f'cart_{event.id}')
    if not cart:
        return redirect('tickets:public_event_buy', public_id=public_id)

    total_cents = sum(
        int(Decimal(item['price']) * 100) * item['quantity']
        for item in cart
    )
    requested_qty = sum(int(item['quantity']) for item in cart)

    promo_session = request.session.get(f'promo_{event.id}')
    discount_cents = promo_session['discount_cents'] if promo_session else 0
    discounted_subtotal_cents = total_cents - discount_cents
    is_free = discounted_subtotal_cents == 0

    total_dollars = Decimal(total_cents) / 100

    if request.method == 'POST' and is_free:
        buyer_name = request.user.get_full_name() or request.user.email
        buyer_email = request.user.email.strip().lower()
        if not buyer_name or not buyer_email:
            return render(request, 'tickets/buy/checkout_payment.html', {
                'event': event,
                'cart': cart,
                'total_cents': total_cents,
                'total_dollars': total_dollars,
                'is_free': is_free,
                'stripe_publishable_key': django_settings.STRIPE_PUBLISHABLE_KEY,
                'error': 'Could not retrieve your account details. Please log in and try again.',
            })
        limit_error = _cart_ticket_type_limit_error(event, buyer_email, cart)
        if limit_error:
            return render(request, 'tickets/buy/checkout_payment.html', {
                'event': event,
                'cart': cart,
                'total_cents': total_cents,
                'total_dollars': total_dollars,
                'is_free': is_free,
                'stripe_publishable_key': django_settings.STRIPE_PUBLISHABLE_KEY,
                'saved_pm': None,
                'user_is_authenticated': request.user.is_authenticated,
                'subtotal': Decimal(total_cents) / 100,
                'service_fee': Decimal('0.00'),
                'grand_total': Decimal('0.00'),
                'discount_cents': discount_cents,
                'discount_dollars': Decimal(discount_cents) / 100,
                'promo_applied': promo_session,
                'initcheckout_event_id': '',
                'grand_total_cents': 0,
                'stripe_currency': django_settings.STRIPE_CURRENCY,
                'error': limit_error,
            })

        with transaction.atomic():
            org = event.organization
            customer, _ = Customer.objects.get_or_create(
                email=buyer_email,
                organization=org,
                defaults={'name': buyer_name},
            )
            sms_opt_in = request.POST.get('sms_opt_in') == '1'
            if sms_opt_in and not customer.sms_opt_in:
                customer.sms_opt_in = True
                customer.sms_opt_in_date = django_tz.now()
                customer.save(update_fields=['sms_opt_in', 'sms_opt_in_date'])
            # Resolve promo code for free-after-discount orders
            promo_code_obj = None
            discount_amount_val = None
            if promo_session:
                try:
                    promo_code_obj = PromoCode.objects.get(id=promo_session['promo_code_id'])
                    discount_amount_val = Decimal(str(promo_session['discount_cents'])) / 100
                except PromoCode.DoesNotExist:
                    pass

            order = TicketOrder.objects.create(
                customer=customer,
                event=event,
                uploaded_file=None,
                order_number=next_order_number(),
                order_date=django_tz.now(),
                total_amount=Decimal('0.00'),
                promo_code=promo_code_obj,
                discount_amount=discount_amount_val,
            )
            if promo_code_obj:
                PromoCode.objects.filter(pk=promo_code_obj.pk).update(times_used=F('times_used') + 1)

            for item in cart:
                tt_id = item['saleable_ticket_type_id']
                qty = item['quantity']
                item_name = item['name']
                item_tier_id = item.get('tier_id')
                item_tier_name = item.get('tier_name') or ''
                SaleableTicketType.objects.filter(id=tt_id).update(
                    quantity_sold=F('quantity_sold') + qty
                )
                if item_tier_id:
                    try:
                        SaleableTicketTypeTier.objects.filter(id=item_tier_id).update(
                            quantity_sold=F('quantity_sold') + qty
                        )
                    except Exception:
                        logger.warning("checkout_payment: failed to update tier %s", item_tier_id)
                Ticket.objects.bulk_create([
                    Ticket(
                        ticket_order=order,
                        ticket_type=item_name,
                        price=Decimal('0.00'),
                        tier=None,
                        tier_name=item_tier_name or None,
                    )
                    for _ in range(qty)
                ])
            customer.update_lifetime_value()
            _invalidate_event_list_cache(org)
            _invalidate_marketing_cache(org)

        from tickets.tasks import send_order_confirmation_email_task
        send_order_confirmation_email_task.delay(str(order.id))

        _clear_waitlist_hold(request, event.id)
        del request.session[f'cart_{event.id}']
        request.session.pop(f'promo_{event.id}', None)
        return redirect(f"{reverse_lazy('tickets:checkout_success')}?order_id={order.id}")

    saved_pm = None

    if request.user.is_authenticated:
        user = request.user
        try:
            profile = user.profile
            if profile.stripe_pm_id and profile.stripe_pm_last4:
                saved_pm = {
                    'id': profile.stripe_pm_id,
                    'brand': profile.stripe_pm_brand,
                    'last4': profile.stripe_pm_last4,
                }
        except UserProfile.DoesNotExist:
            pass

    from tickets.utils import extract_fee_from_display_cents
    fee_cents = extract_fee_from_display_cents(discounted_subtotal_cents) if not is_free else 0
    grand_total_cents = discounted_subtotal_cents  # Display price is already fee-inclusive

    initcheckout_event_id = str(_uuid.uuid4())
    request.session[f'initcheckout_eid_{event.id}'] = initcheckout_event_id
    pixel_id = event.facebook_pixel_id
    capi_token = event.organization.meta_capi_access_token
    if pixel_id and capi_token:
        from tickets.services.facebook_capi import send_capi_event
        _client_ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()
        send_capi_event(
            pixel_id, capi_token, 'InitiateCheckout',
            value=total_dollars,
            content_ids=[item['saleable_ticket_type_id'] for item in cart],
            client_ip=_client_ip,
            client_user_agent=request.META.get('HTTP_USER_AGENT', ''),
            fbp=request.COOKIES.get('_fbp'),
            fbc=request.COOKIES.get('_fbc'),
            event_id=initcheckout_event_id,
            event_source_url=request.build_absolute_uri(),
        )

    return render(request, 'tickets/buy/checkout_payment.html', {
        'event': event,
        'cart': cart,
        'total_cents': total_cents,
        'total_dollars': total_dollars,
        'is_free': is_free,
        'stripe_publishable_key': django_settings.STRIPE_PUBLISHABLE_KEY,
        'saved_pm': saved_pm,
        'user_is_authenticated': request.user.is_authenticated,
        'subtotal': Decimal(total_cents) / 100,
        'service_fee': Decimal(fee_cents) / 100,
        'grand_total': Decimal(grand_total_cents) / 100,
        'discount_cents': discount_cents,
        'discount_dollars': Decimal(discount_cents) / 100,
        'promo_applied': promo_session,
        'initcheckout_event_id': initcheckout_event_id,
        'grand_total_cents': grand_total_cents,
        'stripe_currency': django_settings.STRIPE_CURRENCY,
        'e2e_test_mode': _is_e2e_test_mode(),
    })


@require_http_methods(["POST"])
def create_payment_intent(request, public_id):
    """JSON endpoint - creates a Stripe PaymentIntent and a StripeCheckoutSession record."""
    from django.conf import settings as django_settings

    event = get_object_or_404(
        Event.objects.select_related('organization'),
        public_id=public_id,
    )

    if event.effective_status != EVENT_STATUS_LIVE:
        return JsonResponse({'error': 'Ticket sales for this event have ended.'}, status=400)

    cart = request.session.get(f'cart_{event.id}')
    if not cart:
        return JsonResponse({'error': 'No cart found. Please start over.'}, status=400)

    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid request.'}, status=400)

    buyer_name = request.user.get_full_name() or request.user.email
    buyer_email = request.user.email.strip().lower()
    if not buyer_name or not buyer_email:
        return JsonResponse({'error': 'Could not retrieve your account details. Please log in and try again.'}, status=400)
    limit_error = _cart_ticket_type_limit_error(event, buyer_email, cart)
    if limit_error:
        return JsonResponse({'error': limit_error}, status=400)

    save_card    = bool(data.get('save_card', False))
    use_saved_pm = (data.get('use_saved_pm') or '').strip()
    sms_opt_in   = bool(data.get('sms_opt_in', False))
    _capi_client_ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()
    fb_browser_data = {
        'fbp': data.get('fbp', ''),
        'fbc': data.get('fbc', ''),
        'client_ip': _capi_client_ip,
        'client_user_agent': request.META.get('HTTP_USER_AGENT', ''),
        'event_source_url': request.build_absolute_uri(
            f'/e/{event.public_id}/success/'
        ),
    }

    # Re-validate availability
    for item in cart:
        tt_id = item['saleable_ticket_type_id']
        qty = item['quantity']
        try:
            tt = SaleableTicketType.objects.get(id=tt_id, event=event, is_active=True)
        except SaleableTicketType.DoesNotExist:
            return JsonResponse({'error': f'Ticket type is no longer available.'}, status=400)
        if tt.quantity_limit is not None and (tt.quantity_sold + qty) > tt.quantity_limit:
            return JsonResponse({'error': f'Not enough tickets available for {tt.name}.'}, status=400)
        tier_id = item.get('tier_id')
        if tier_id:
            try:
                tier = SaleableTicketTypeTier.objects.get(id=tier_id)
            except SaleableTicketTypeTier.DoesNotExist:
                return JsonResponse({'error': f'Selected tier for {tt.name} is no longer available.'}, status=400)
            if (tier.quantity_sold + qty) > tier.allotment:
                return JsonResponse({'error': f'Not enough tickets in the selected tier for {tt.name}.'}, status=400)

    total_cents = sum(
        int(Decimal(item['price']) * 100) * item['quantity']
        for item in cart
    )
    if total_cents == 0:
        return JsonResponse({'error': 'Use the free ticket flow for $0 orders.'}, status=400)

    # Re-validate and apply any promo code stored in the session
    promo_session = request.session.get(f'promo_{event.id}')
    discount_cents = 0
    promo_code_id = None
    if promo_session:
        try:
            promo_obj = PromoCode.objects.get(id=promo_session['promo_code_id'], event=event)
            if promo_obj.is_valid():
                discount_cents = promo_obj.calculate_discount_cents(total_cents)
                promo_code_id = promo_obj.id
        except PromoCode.DoesNotExist:
            pass

    discounted_subtotal_cents = total_cents - discount_cents

    from tickets.utils import extract_fee_from_display_cents
    fee_cents = extract_fee_from_display_cents(discounted_subtotal_cents)
    charge_cents = discounted_subtotal_cents  # Display price is fee-inclusive; buyer pays display total

    profile_pk = None
    stripe_customer_id = None
    payment_method_id = use_saved_pm or None
    if _is_e2e_test_mode():
        pi_id = f"pi_e2e_{_uuid.uuid4().hex}"
        client_secret = f"{pi_id}_secret_e2e"
    else:
        import stripe as stripe_lib

        stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY

        # Resolve or create a Stripe Customer for authenticated users
        if request.user.is_authenticated:
            try:
                profile = request.user.profile
                profile_pk = profile.pk
                if profile.stripe_customer_id:
                    stripe_customer_id = profile.stripe_customer_id
                else:
                    cus = stripe_lib.Customer.create(email=buyer_email, name=buyer_name)
                    stripe_customer_id = cus.id
                    UserProfile.objects.filter(pk=profile_pk).update(stripe_customer_id=stripe_customer_id)
            except Exception as e:
                logger.error("Stripe Customer.create failed: %s", e)
                # non-fatal - card saving skipped, payment proceeds

        def _payment_intent_create_kwargs():
            md = {
                'event_id': str(event.id),
                'org_id': str(event.organization_id),
            }
            if save_card and stripe_customer_id:
                md['user_id'] = str(request.user.pk)
            kw = {
                'amount': charge_cents,
                'currency': django_settings.STRIPE_CURRENCY,
                'metadata': md,
            }
            if stripe_customer_id:
                kw['customer'] = stripe_customer_id
            if save_card and stripe_customer_id:
                kw['setup_future_usage'] = 'off_session'
            if payment_method_id:
                kw['payment_method'] = payment_method_id
            return kw

        pi = None
        for attempt in range(2):
            try:
                pi = stripe_lib.PaymentIntent.create(**_payment_intent_create_kwargs())
                break
            except stripe_lib.error.InvalidRequestError as e:
                code = getattr(e, 'code', None)
                param = getattr(e, 'param', None)
                if attempt == 0 and code == 'resource_missing' and param == 'customer' and profile_pk:
                    logger.warning(
                        "Stripe customer not found for PaymentIntent; resetting profile Stripe IDs and retrying: %s",
                        e,
                    )
                    UserProfile.objects.filter(pk=profile_pk).update(
                        stripe_customer_id=None,
                        stripe_pm_id=None,
                        stripe_pm_brand='',
                        stripe_pm_last4='',
                    )
                    payment_method_id = None
                    try:
                        cus = stripe_lib.Customer.create(email=buyer_email, name=buyer_name)
                        stripe_customer_id = cus.id
                        UserProfile.objects.filter(pk=profile_pk).update(stripe_customer_id=stripe_customer_id)
                    except Exception as cus_exc:
                        logger.error("Stripe Customer.create after stale ID clear failed: %s", cus_exc)
                        stripe_customer_id = None
                    continue
                if attempt == 0 and code == 'resource_missing' and param == 'payment_method':
                    if profile_pk:
                        UserProfile.objects.filter(pk=profile_pk).update(
                            stripe_pm_id=None,
                            stripe_pm_brand='',
                            stripe_pm_last4='',
                        )
                    payment_method_id = None
                    continue
                logger.error("PaymentIntent creation failed: %s", e)
                return JsonResponse({'error': 'Could not initiate payment. Please try again.'}, status=500)
            except Exception as e:
                logger.error("PaymentIntent creation failed: %s", e)
                return JsonResponse({'error': 'Could not initiate payment. Please try again.'}, status=500)

        if pi is None:
            return JsonResponse({'error': 'Could not initiate payment. Please try again.'}, status=500)
        pi_id = pi.id
        client_secret = pi.client_secret

    tracking_ref = request.session.get(f'tracking_ref_{event.id}')
    tracking_link_obj = None
    if tracking_ref:
        tracking_link_obj = TrackingLink.objects.filter(token=tracking_ref, event=event).first()

    session_record = StripeCheckoutSession.objects.create(
        event=event,
        organization=event.organization,
        stripe_session_id=pi_id,
        stripe_payment_intent_id=pi_id,
        buyer_email=buyer_email,
        buyer_name=buyer_name,
        status=StripeCheckoutSession.Status.PENDING,
        line_items_snapshot=cart,
        amount_total_cents=charge_cents,
        platform_fee_cents=fee_cents,
        promo_code_id=promo_code_id,
        discount_cents=discount_cents,
        fb_browser_data=fb_browser_data,
        tracking_link=tracking_link_obj,
        sms_opt_in=sms_opt_in,
    )

    return JsonResponse({
        'client_secret': client_secret,
        'session_id': str(session_record.id),
    })


@require_http_methods(["POST"])
def e2e_complete_payment(request, public_id):
    """Test-only endpoint that fulfills a pending checkout session without Stripe."""
    if not _is_e2e_test_mode():
        raise Http404()

    event = get_object_or_404(
        Event.objects.select_related('organization'),
        public_id=public_id,
    )

    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid request.'}, status=400)

    session_id = (data.get('session_id') or '').strip()
    if not session_id:
        return JsonResponse({'error': 'Missing session id.'}, status=400)

    session_obj = get_object_or_404(
        StripeCheckoutSession.objects.filter(event=event),
        id=session_id,
    )

    _fulfill_payment_intent({
        'id': session_obj.stripe_session_id,
        'amount_received': session_obj.amount_total_cents,
    })

    session_obj.refresh_from_db()
    if session_obj.status != StripeCheckoutSession.Status.COMPLETED:
        return JsonResponse({'error': 'Could not complete checkout.'}, status=500)

    return JsonResponse({'ok': True, 'session_id': str(session_obj.id)})


def checkout_success(request):
    """Post-payment landing page - supports ?session_id=<uuid> (paid) and ?order_id=<uuid> (free)."""
    session_obj = None
    order_obj = None

    session_id = request.GET.get('session_id', '')
    order_id = request.GET.get('order_id', '')

    if session_id:
        # Paid flow: session_id is our DB record UUID
        session_obj = StripeCheckoutSession.objects.filter(
            id=session_id
        ).select_related('ticket_order', 'event').first()
        # Clear waitlist hold if this was a waitlist-hold purchase
        if session_obj and session_obj.status == StripeCheckoutSession.Status.COMPLETED:
            _clear_waitlist_hold(request, str(session_obj.event_id))
    elif order_id:
        # Free flow: look up TicketOrder directly
        order_obj = TicketOrder.objects.filter(
            id=order_id
        ).select_related('event', 'customer').first()

    qr_code = ''
    if order_obj and not order_obj.refunded_at:
        qr_code = generate_qr_b64(order_obj.order_number)
    elif session_obj and session_obj.ticket_order and not session_obj.ticket_order.refunded_at:
        qr_code = generate_qr_b64(session_obj.ticket_order.order_number)

    _event_for_pixel = order_obj.event if order_obj else (session_obj.event if session_obj else None)

    # When no Meta Pixel is configured, there's no client-side event to fire — keep the
    # original UX of bouncing authenticated buyers straight to /my_tickets/.
    _has_pixel = bool(_event_for_pixel and _event_for_pixel.facebook_pixel_id)
    if request.user.is_authenticated and not _has_pixel:
        if order_obj:
            messages.success(request, "Your tickets are confirmed!")
            return redirect('tickets:my_tickets')
        if session_obj and session_obj.status == StripeCheckoutSession.Status.COMPLETED:
            messages.success(request, "Your tickets are confirmed!")
            return redirect('tickets:my_tickets')

    pixel_content_ids = []
    if order_obj:
        ticket_names = list(order_obj.tickets.values_list('ticket_type', flat=True).distinct())
        pixel_content_ids = [
            str(i) for i in SaleableTicketType.objects.filter(
                event=order_obj.event, name__in=ticket_names
            ).values_list('id', flat=True)
        ]
    elif session_obj and session_obj.line_items_snapshot:
        pixel_content_ids = [item['saleable_ticket_type_id'] for item in session_obj.line_items_snapshot]

    purchase_event_id = ''
    if order_obj:
        purchase_event_id = f'purchase_{order_obj.order_number}'
        pixel_id_for_capi = _event_for_pixel.facebook_pixel_id if _event_for_pixel else ''
        capi_token = _event_for_pixel.organization.meta_capi_access_token if _event_for_pixel else ''
        if pixel_id_for_capi and capi_token:
            from tickets.services.facebook_capi import send_capi_event
            _client_ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()
            name_parts = (order_obj.customer.name or '').split()
            send_capi_event(
                pixel_id_for_capi, capi_token, 'Purchase',
                value=order_obj.total_amount,
                content_ids=pixel_content_ids,
                email=order_obj.customer.email,
                first_name=name_parts[0] if name_parts else '',
                last_name=' '.join(name_parts[1:]) if len(name_parts) > 1 else '',
                client_ip=_client_ip,
                client_user_agent=request.META.get('HTTP_USER_AGENT', ''),
                fbp=request.COOKIES.get('_fbp'),
                fbc=request.COOKIES.get('_fbc'),
                event_id=purchase_event_id,
                event_source_url=request.build_absolute_uri(),
            )
    elif session_obj and session_obj.ticket_order:
        purchase_event_id = f'purchase_{session_obj.ticket_order.order_number}'

    return render(request, 'tickets/buy/checkout_success.html', {
        'session': session_obj,
        'order': order_obj,
        'qr_code': qr_code,
        'pixel_id': _event_for_pixel.facebook_pixel_id if _event_for_pixel else '',
        'pixel_content_ids': pixel_content_ids,
        'purchase_event_id': purchase_event_id,
    })


def checkout_session_status(request, session_id):
    """JSON endpoint - returns fulfillment status + Purchase pixel payload once a paid session completes."""
    session_obj = StripeCheckoutSession.objects.filter(
        id=session_id
    ).select_related('ticket_order').first()
    if not session_obj:
        return JsonResponse({'status': 'unknown'}, status=404)

    if session_obj.status != StripeCheckoutSession.Status.COMPLETED or not session_obj.ticket_order:
        return JsonResponse({'status': 'pending'})

    order = session_obj.ticket_order
    content_ids = [item['saleable_ticket_type_id'] for item in (session_obj.line_items_snapshot or [])]
    redirect_url = reverse('tickets:my_tickets') if request.user.is_authenticated else ''

    return JsonResponse({
        'status': 'completed',
        'purchase': {
            'value': str(order.total_amount),
            'currency': 'USD',
            'content_ids': content_ids,
            'event_id': f'purchase_{order.order_number}',
            'order_number': order.display_order_number,
            'redirect_url': redirect_url,
        },
    })


# ---------------------------------------------------------------------------
# Stripe Webhook
# ---------------------------------------------------------------------------

@csrf_exempt
@require_http_methods(["POST"])
def stripe_webhook(request):
    """Receive and process Stripe webhook events."""
    from django.conf import settings as django_settings
    import stripe as stripe_lib

    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE', '')
    try:
        event = stripe_lib.Webhook.construct_event(
            payload, sig_header, django_settings.STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        logger.warning("Stripe webhook: invalid payload")
        return HttpResponse(status=400)
    except stripe_lib.error.SignatureVerificationError:
        logger.warning("Stripe webhook: invalid signature")
        return HttpResponse(status=400)

    event_type = event['type']
    if event_type == 'payment_intent.succeeded':
        _fulfill_payment_intent(event['data']['object'])
    elif event_type == 'payment_intent.payment_failed':
        _fail_payment_intent(event['data']['object'])

    return HttpResponse(status=200)


@csrf_exempt
@require_http_methods(["POST"])
def stripe_connect_webhook(request):
    """Receive payout lifecycle events from Stripe connected accounts."""
    from django.conf import settings as django_settings
    import stripe as stripe_lib

    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE', '')
    try:
        event = stripe_lib.Webhook.construct_event(
            payload, sig_header, django_settings.STRIPE_CONNECTED_ACCOUNT_WEBHOOK_SECRET
        )
    except ValueError:
        logger.warning("Stripe connect webhook: invalid payload")
        return HttpResponse(status=400)
    except stripe_lib.error.SignatureVerificationError:
        logger.warning("Stripe connect webhook: invalid signature")
        return HttpResponse(status=400)

    event_type = event['type']
    if event_type in ('payout.created', 'payout.updated', 'payout.paid', 'payout.failed'):
        _handle_stripe_payout_event(event)

    return HttpResponse(status=200)


def _handle_stripe_payout_event(event):
    """
    Handle Stripe payout lifecycle webhooks from connected accounts.

    Stripe fires these on the connected account, so the event includes an
    'account' field with the Express account ID. We use that to find the org
    and then reconcile by Stripe payout ID first, with best-effort fallback
    to metadata or the oldest open payout of the same amount.

    payout.created  → confirm/store stripe_payout_id
    payout.updated  → advance to IN_TRANSIT when Stripe dispatches to bank
    payout.paid     → advance to COMPLETED (funds arrived at bank)
    payout.failed   → advance to FAILED
    """
    def _stripe_value(obj, key, default=None):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    stripe_payout_obj = event['data']['object']
    connected_account_id = _stripe_value(event, 'account')
    stripe_payout_id = _stripe_value(stripe_payout_obj, 'id')
    stripe_payout_status = _stripe_value(stripe_payout_obj, 'status')  # pending, in_transit, paid, failed, canceled
    stripe_payout_amount = _stripe_value(stripe_payout_obj, 'amount')
    payout_metadata = _stripe_value(stripe_payout_obj, 'metadata', {}) or {}
    local_payout_id = _stripe_value(payout_metadata, 'payout_id')

    if not connected_account_id:
        logger.warning("Stripe payout webhook missing 'account' field: %s", _stripe_value(event, 'id'))
        return

    try:
        org = Organization.objects.get(stripe_account_id=connected_account_id)
    except Organization.DoesNotExist:
        logger.warning("Stripe payout webhook for unknown account: %s", connected_account_id)
        return

    payout = None
    if stripe_payout_id:
        payout = (
            Payout.objects
            .filter(organization=org, stripe_payout_id=stripe_payout_id)
            .first()
        )

    if not payout and local_payout_id:
        payout = (
            Payout.objects
            .filter(organization=org, id=local_payout_id)
            .first()
        )

    if not payout and stripe_payout_amount is not None:
        payout_amount = Decimal(str(stripe_payout_amount)) / 100
        payout = (
            Payout.objects
            .filter(
                organization=org,
                amount=payout_amount,
                stripe_payout_id__isnull=True,
                status__in=[Payout.Status.PENDING, Payout.Status.IN_TRANSIT],
            )
            .order_by('created_at')
            .first()
        )

    if not payout:
        logger.info("No matching Payout record for Stripe payout %s (org %s)", stripe_payout_id, org.id)
        return

    update_fields = []

    if not payout.stripe_payout_id and stripe_payout_id:
        payout.stripe_payout_id = stripe_payout_id
        update_fields.append('stripe_payout_id')

    status_map = {
        'in_transit': Payout.Status.IN_TRANSIT,
        'paid':       Payout.Status.COMPLETED,
        'failed':     Payout.Status.FAILED,
        'canceled':   Payout.Status.FAILED,
    }
    new_status = status_map.get(stripe_payout_status)
    if new_status and payout.status != new_status:
        payout.status = new_status
        update_fields.append('status')

    if update_fields:
        payout.save(update_fields=update_fields)
        logger.info(
            "Payout %s advanced to %s via Stripe payout %s",
            payout.id, payout.status, stripe_payout_id,
        )


def apple_pay_domain_association(request):
    """Serve Apple Pay domain verification file for Stripe."""
    from django.conf import settings as django_settings
    content = getattr(django_settings, 'APPLE_PAY_DOMAIN_ASSOCIATION', '')
    if not content:
        raise Http404
    return HttpResponse(content, content_type='text/plain')


def _fulfill_payment_intent(payment_intent):
    """
    Idempotently fulfill a succeeded PaymentIntent.
    Buyer info is pre-populated in our DB record at PI creation time.
    """
    pi_id = payment_intent['id']
    amount_total_cents = getattr(payment_intent, 'amount_received', 0) or 0

    # Idempotency check #1: outside lock
    session_obj = StripeCheckoutSession.objects.filter(stripe_session_id=pi_id).first()
    if session_obj and session_obj.status == StripeCheckoutSession.Status.COMPLETED:
        logger.info("Stripe webhook: PaymentIntent %s already fulfilled", pi_id)
        return

    if not session_obj:
        logger.warning("Stripe webhook: no StripeCheckoutSession for PaymentIntent %s - skipping", pi_id)
        return

    with transaction.atomic():
        # Lock the row; re-check inside the lock (idempotency check #2)
        session_obj = StripeCheckoutSession.objects.select_for_update().get(pk=session_obj.pk)
        if session_obj.status == StripeCheckoutSession.Status.COMPLETED:
            return

        org = session_obj.organization
        event = session_obj.event

        if amount_total_cents:
            session_obj.amount_total_cents = amount_total_cents

        email = session_obj.buyer_email
        name = session_obj.buyer_name or email

        customer, _ = Customer.objects.get_or_create(
            email=email.lower(),
            organization=org,
            defaults={'name': name},
        )
        if session_obj.sms_opt_in and not customer.sms_opt_in:
            customer.sms_opt_in = True
            customer.sms_opt_in_date = django_tz.now()
            customer.save(update_fields=['sms_opt_in', 'sms_opt_in_date'])
        limit_error = _cart_ticket_type_limit_error(
            event,
            email,
            session_obj.line_items_snapshot,
            exclude_session_id=session_obj.id,
        )
        if limit_error:
            logger.warning(
                "Stripe webhook: customer ticket-type cap exceeded for session %s event %s email %s; refunding payment",
                session_obj.id, event.id, email,
            )
            try:
                import stripe as stripe_lib_inner
                from django.conf import settings as django_settings_inner
                stripe_lib_inner.api_key = django_settings_inner.STRIPE_SECRET_KEY
                stripe_lib_inner.Refund.create(payment_intent=session_obj.stripe_session_id)
                session_obj.status = StripeCheckoutSession.Status.REFUNDED
            except Exception:
                logger.exception("Stripe webhook: automatic refund failed for capped session %s", session_obj.id)
                session_obj.status = StripeCheckoutSession.Status.CANCELED
            session_obj.save(update_fields=['status', 'amount_total_cents'])
            return

        order = TicketOrder.objects.create(
            customer=customer,
            event=event,
            uploaded_file=None,
            order_number=next_order_number(),
            order_date=django_tz.now(),
            total_amount=Decimal(str(session_obj.amount_total_cents)) / 100,
            promo_code_id=session_obj.promo_code_id,
            discount_amount=Decimal(str(session_obj.discount_cents)) / 100 if session_obj.discount_cents else None,
        )
        if session_obj.promo_code_id:
            PromoCode.objects.filter(pk=session_obj.promo_code_id).update(times_used=F('times_used') + 1)

        for item in session_obj.line_items_snapshot:
            tt_id = item.get('saleable_ticket_type_id')
            qty = item.get('quantity', 1)
            item_name = item.get('name', '')
            item_price = Decimal(str(item.get('price', '0')))
            tier_id = item.get('tier_id')
            tier_name = item.get('tier_name') or ''

            try:
                tt_locked = SaleableTicketType.objects.select_for_update().get(id=tt_id)
            except SaleableTicketType.DoesNotExist:
                logger.warning("Stripe webhook: SaleableTicketType %s not found, skipping inventory update", tt_id)
            else:
                if tt_locked.quantity_limit is not None and (tt_locked.quantity_sold + qty) > tt_locked.quantity_limit:
                    logger.error(
                        "Stripe webhook: oversell detected for SaleableTicketType %s "
                        "(limit=%s, sold=%s, adding=%s) - fulfilling anyway",
                        tt_id, tt_locked.quantity_limit, tt_locked.quantity_sold, qty,
                    )
                SaleableTicketType.objects.filter(id=tt_id).update(
                    quantity_sold=F('quantity_sold') + qty
                )

            if tier_id:
                try:
                    tier_locked = SaleableTicketTypeTier.objects.select_for_update().get(id=tier_id)
                except SaleableTicketTypeTier.DoesNotExist:
                    logger.warning("Stripe webhook: SaleableTicketTypeTier %s not found", tier_id)
                else:
                    if (tier_locked.quantity_sold + qty) > tier_locked.allotment:
                        logger.error("Stripe webhook: tier oversell for %s - fulfilling anyway", tier_id)
                    SaleableTicketTypeTier.objects.filter(id=tier_id).update(
                        quantity_sold=F('quantity_sold') + qty
                    )

            Ticket.objects.bulk_create([
                Ticket(
                    ticket_order=order,
                    ticket_type=item_name,
                    price=item_price,
                    tier=None,
                    tier_name=tier_name or None,
                )
                for _ in range(qty)
            ])

        customer.update_lifetime_value()

        session_obj.status = StripeCheckoutSession.Status.COMPLETED
        session_obj.ticket_order = order
        session_obj.fulfilled_at = django_tz.now()
        session_obj.save()

        # Clear any active waitlist holds for this buyer (no request object available in webhook).
        for item in session_obj.line_items_snapshot:
            tt_id = item.get('saleable_ticket_type_id')
            if not tt_id:
                continue
            updated = WaitlistEntry.objects.filter(
                ticket_type_id=tt_id,
                email=email.lower(),
                purchased_at__isnull=True,
                expired=False,
            ).update(purchased_at=django_tz.now())
            if updated:
                SaleableTicketType.objects.filter(id=tt_id).update(
                    quantity_held=Greatest(F('quantity_held') - 1, Value(0))
                )

        _invalidate_event_list_cache(org)

        _invalidate_marketing_cache(org)

        from tickets.tasks import send_order_confirmation_email_task
        send_order_confirmation_email_task.delay(str(order.id))

        pixel_id = event.facebook_pixel_id
        capi_token = getattr(org, 'meta_capi_access_token', '')
        fb = session_obj.fb_browser_data or {}
        if pixel_id and capi_token:
            from tickets.services.facebook_capi import send_capi_event
            content_ids = [item['saleable_ticket_type_id'] for item in session_obj.line_items_snapshot]
            name_parts = (customer.name or '').split()
            send_capi_event(
                pixel_id, capi_token, 'Purchase',
                value=order.total_amount,
                content_ids=content_ids,
                email=customer.email,
                first_name=name_parts[0] if name_parts else '',
                last_name=' '.join(name_parts[1:]) if len(name_parts) > 1 else '',
                client_ip=fb.get('client_ip', ''),
                client_user_agent=fb.get('client_user_agent', ''),
                fbp=fb.get('fbp', ''),
                fbc=fb.get('fbc', ''),
                event_id=f'purchase_{order.order_number}',
                event_source_url=fb.get('event_source_url', ''),
            )

        if getattr(payment_intent, 'setup_future_usage', None) == 'off_session':
            metadata = getattr(payment_intent, 'metadata', {}) or {}
            user_id = metadata.get('user_id', '') if isinstance(metadata, dict) else getattr(metadata, 'user_id', '')
            pm_id   = getattr(payment_intent, 'payment_method', '') or ''
            if user_id and pm_id:
                import stripe as stripe_lib_inner
                from django.conf import settings as django_settings_inner
                stripe_lib_inner.api_key = django_settings_inner.STRIPE_SECRET_KEY
                try:
                    pm   = stripe_lib_inner.PaymentMethod.retrieve(pm_id)
                    card = getattr(pm, 'card', None) or {}
                    UserProfile.objects.filter(user_id=user_id).update(
                        stripe_pm_id=pm_id,
                        stripe_pm_brand=card.get('brand', '') if isinstance(card, dict) else getattr(card, 'brand', ''),
                        stripe_pm_last4=card.get('last4', '') if isinstance(card, dict) else getattr(card, 'last4', ''),
                    )
                    logger.info("Saved PaymentMethod %s for user %s", pm_id, user_id)
                except Exception as e:
                    logger.error("Failed to save PaymentMethod %s for user %s: %s", pm_id, user_id, e)

    logger.info("Fulfilled PaymentIntent %s - order %s", pi_id, order.order_number)

    # Store the settlement date so we can gate payouts to settled funds only.
    # latest_charge is a string ID in the webhook payload; fetch the charge to get
    # its balance_transaction.available_on.
    latest_charge_id = (
        payment_intent.get('latest_charge')
        if isinstance(payment_intent, dict)
        else getattr(payment_intent, 'latest_charge', None)
    )
    if latest_charge_id and isinstance(latest_charge_id, str):
        try:
            import stripe as stripe_lib_bt
            from django.conf import settings as django_settings_bt
            stripe_lib_bt.api_key = django_settings_bt.STRIPE_SECRET_KEY
            charge = stripe_lib_bt.Charge.retrieve(
                latest_charge_id,
                expand=['balance_transaction'],
            )
            bt = charge.balance_transaction
            if bt and getattr(bt, 'available_on', None):
                import datetime as _dt
                available_on_dt = _dt.datetime.fromtimestamp(bt.available_on, tz=_dt.timezone.utc)
                StripeCheckoutSession.objects.filter(stripe_session_id=pi_id).update(
                    available_on=available_on_dt,
                )
                logger.info("Set available_on=%s for PaymentIntent %s", available_on_dt, pi_id)
        except Exception:
            logger.exception("Could not fetch balance_transaction for charge %s (PI %s)", latest_charge_id, pi_id)


def _fail_payment_intent(payment_intent):
    """Mark a failed PaymentIntent session as canceled."""
    pi_id = payment_intent['id'] if 'id' in payment_intent else getattr(payment_intent, 'id', '')
    if pi_id:
        StripeCheckoutSession.objects.filter(
            stripe_session_id=pi_id,
            status=StripeCheckoutSession.Status.PENDING,
        ).update(status=StripeCheckoutSession.Status.CANCELED)
        logger.info("PaymentIntent %s marked canceled (payment failed)", pi_id)

# ---------------------------------------------------------------------------
# Attendee Auth Views (public - no login required)
# ---------------------------------------------------------------------------

def attendee_signup_view(request, org_slug):
    """Public. Attendee enters phone number; Twilio Verify sends the OTP."""
    from .sms import start_phone_verification
    org = get_object_or_404(Organization, slug=org_slug)
    if request.method == "POST":
        form = AttendeePhoneForm(request.POST)
        if form.is_valid():
            phone = form.cleaned_data["phone_number"]
            if UserProfile.objects.filter(phone_number=phone).exists():
                messages.error(request, 'An account with this phone number already exists. Please log in.')
                return redirect('tickets:phone_login')
            if not start_phone_verification(phone):
                messages.error(request, 'Could not send a verification code. Please check the number and try again.')
            else:
                request.session["verify_org_signup"] = {"phone": phone, "org_id": str(org.id)}
                return redirect('tickets:attendee_verify_otp', org_slug=org_slug)
    else:
        form = AttendeePhoneForm()
    return render(request, 'tickets/auth/attendee_signup.html', {'form': form, 'org': org})


def attendee_verify_otp_view(request, org_slug):
    """Verifies Twilio Verify code, creates User+UserProfile with attendee role."""
    from django.contrib.auth import login as auth_login
    from django.contrib.auth import get_user_model
    from .sms import check_phone_verification
    AuthUser = get_user_model()

    org = get_object_or_404(Organization, slug=org_slug)
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    session_data = request.session.get("verify_org_signup")
    if not session_data:
        messages.info(request, 'This verification step has already completed or expired. Please start again.')
        return redirect('tickets:attendee_signup', org_slug=org_slug)
    phone = session_data["phone"]

    if request.method == "POST":
        form = OTPVerificationForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data["otp_code"]
            if not check_phone_verification(phone, code):
                messages.error(request, 'Incorrect or expired code. Please try again.')
            else:
                from .utils import generate_username
                user = AuthUser.objects.create(
                    username=generate_username('user', phone[-4:]),
                    email='',
                    first_name='',
                    last_name='',
                )
                user.set_unusable_password()
                user.save()
                UserProfile.objects.create(
                    user=user,
                    organization=org,
                    role=UserProfile.Role.ATTENDEE,
                    phone_number=phone,
                )
                del request.session["verify_org_signup"]
                auth_login(request, user, backend='tickets.backends.PhoneBackend')
                messages.success(request, 'Welcome! You are now registered.')
                return redirect('tickets:attendee_dashboard')
    else:
        form = OTPVerificationForm()
    return render(request, 'tickets/auth/attendee_verify_otp.html', {
        'form': form,
        'org': org,
        "masked_phone": f"***{phone[-4:]}",
    })


def phone_login_view(request):
    """Public. Existing attendee enters phone; Twilio Verify sends the OTP."""
    from .sms import start_phone_verification
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    if request.method == "POST":
        form = AttendeePhoneForm(request.POST)
        if form.is_valid():
            phone = form.cleaned_data["phone_number"]
            if not UserProfile.objects.filter(phone_number=phone).exists():
                messages.error(request, 'No account found with this phone number.')
            elif not start_phone_verification(phone):
                messages.error(request, 'Could not send a verification code. Please try again.')
            else:
                request.session["verify_login"] = {"phone": phone}
                return redirect('tickets:phone_login_verify')
    else:
        form = AttendeePhoneForm()
    return render(request, 'tickets/auth/phone_login.html', {'form': form})


def phone_login_verify_view(request):
    """Verify Twilio Verify code for returning attendees; log in on success."""
    from django.contrib.auth import login as auth_login
    from django.contrib.auth import get_user_model
    from .sms import check_phone_verification
    AuthUser = get_user_model()

    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    session_data = request.session.get("verify_login")
    if not session_data:
        messages.info(request, 'This verification step has already completed or expired. Please start again.')
        return redirect('tickets:phone_login')
    phone = session_data["phone"]

    if request.method == "POST":
        form = OTPVerificationForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data["otp_code"]
            if not check_phone_verification(phone, code):
                messages.error(request, 'Incorrect or expired code. Please try again.')
            else:
                try:
                    profile = UserProfile.objects.select_related('user').get(phone_number=phone)
                    user = profile.user
                except UserProfile.DoesNotExist:
                    messages.error(request, 'Account not found. Please sign up first.')
                    del request.session["verify_login"]
                    return redirect('tickets:phone_login')
                del request.session["verify_login"]
                auth_login(request, user, backend='tickets.backends.PhoneBackend')
                try:
                    profile = user.profile
                    if profile.is_organizer:
                        return redirect('tickets:home')
                except UserProfile.DoesNotExist:
                    pass
                return redirect('tickets:attendee_dashboard')
    else:
        form = OTPVerificationForm()
    return render(request, 'tickets/auth/phone_login_verify.html', {
        'form': form,
        "masked_phone": f"***{phone[-4:]}",
    })


@require_http_methods(["POST"])
def phone_login_resend_view(request):
    """Resend Twilio Verify code for the /login/phone/ flow."""
    from .sms import start_phone_verification
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    session_data = request.session.get("verify_login")
    if not session_data:
        return redirect('tickets:phone_login')
    if not start_phone_verification(session_data["phone"]):
        messages.error(request, 'Could not resend the code. Please try again.')
    else:
        messages.success(request, 'A new code has been sent.')
    return redirect('tickets:phone_login_verify')


# ---------------------------------------------------------------------------
# View Mode Toggle (organizer ↔ attendee)
# ---------------------------------------------------------------------------

@login_required
@require_http_methods(["POST"])
def switch_view_mode(request):
    """Let organizers toggle between organizer and attendee view."""
    try:
        profile = request.user.profile
    except UserProfile.DoesNotExist:
        return redirect('tickets:attendee_dashboard')
    if not profile.is_organizer:
        return redirect('tickets:attendee_dashboard')
    mode = request.POST.get('mode', 'organizer')
    if mode == 'attendee':
        request.session['_view_mode'] = 'attendee'
        return redirect('tickets:attendee_dashboard')
    else:
        request.session['_view_mode'] = 'organizer'
        return redirect('tickets:home')


# ---------------------------------------------------------------------------
# Attendee Dashboard
# ---------------------------------------------------------------------------

@login_required
def attendee_dashboard(request):
    """Redirects legacy /attendee/dashboard/ to My Tickets (now the attendee home)."""
    return redirect('tickets:my_tickets')


@login_required
def my_tickets(request):
    """Attendee order history - shows ticket orders for direct ticketing events only."""
    from .models import TICKETING_TYPE_DIRECT
    today = django_tz.localdate()  # Use app timezone so Upcoming/Past match where events are held
    base_qs = (
        TicketOrder.objects
        .filter(
            customer__email=request.user.email,
            event__ticketing_type=TICKETING_TYPE_DIRECT,
        )
        .select_related('event', 'event__venue', 'customer')
        .prefetch_related('tickets')
        .order_by('-order_date')
    )
    tab = (request.GET.get('tab') or 'upcoming').lower()
    if tab == 'past':
        # Event has ended: end_date < today, or no end_date and start_date < today
        orders = base_qs.filter(
            Q(event__end_date__lt=today)
            | (Q(event__end_date__isnull=True) & Q(event__start_date__lt=today))
        )
    else:
        # Upcoming: default; event end date (or start if no end) >= today
        tab = 'upcoming'
        orders = base_qs.filter(
            Q(event__end_date__gte=today)
            | (Q(event__end_date__isnull=True) & Q(event__start_date__gte=today))
        )
    paginator = Paginator(orders, 12)
    page_obj = paginator.get_page(request.GET.get('page'))
    for order in page_obj:
        order.qr_code = generate_qr_b64(order.order_number) if not order.refunded_at else ''
    return render(request, 'tickets/my_tickets.html', {'page_obj': page_obj, 'active_tab': tab})


@login_required
def my_ticket_detail(request, order_id):
    """Ticket detail page for a single order - shows QR code and full order info."""
    order = get_object_or_404(
        TicketOrder.objects
        .select_related('event', 'event__venue', 'customer')
        .prefetch_related('tickets'),
        id=order_id,
        customer__email=request.user.email,
    )
    qr_code = generate_qr_b64(order.order_number) if not order.refunded_at else ''
    return render(request, 'tickets/ticket_detail.html', {
        'order': order,
        'qr_code': qr_code,
    })


@login_required
def user_profile(request):
    """View and edit the logged-in user's profile information."""
    profile = request.user.profile
    if request.method == 'POST':
        form = UserProfileForm(request.POST, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Profile updated.')
            return redirect('tickets:user_profile')
    else:
        form = UserProfileForm(user=request.user, initial={
            'first_name':       request.user.first_name,
            'last_name':        request.user.last_name,
            'phone_number':     profile.phone_number or '',
            'gender':           profile.gender or '',
            'marketing_opt_in': profile.marketing_opt_in,
        })
    return render(request, 'tickets/account_profile.html', {
        'form':    form,
        'profile': profile,
    })


# ---------------------------------------------------------------------------
# Member Role Update
# ---------------------------------------------------------------------------

@login_required
@require_org
@require_owner
@require_http_methods(["POST"])
def member_role_update(request, membership_id):
    """Owner updates the org role of an org member."""
    org = get_organization(request)
    membership = get_object_or_404(
        OrganizationMembership.objects.select_related('user', 'user__profile').filter(organization=org),
        id=membership_id,
    )
    if membership.user == request.user:
        messages.error(request, 'You cannot change your own role.')
        return redirect('tickets:member_list')
    new_org_role = request.POST.get('org_role', '').strip()
    valid_org_roles = [r[0] for r in UserProfile.OrgRole.choices]
    if new_org_role and new_org_role not in valid_org_roles:
        messages.error(request, 'Invalid org role.')
        return redirect('tickets:member_list')
    if new_org_role:
        membership.org_role = new_org_role
        membership.save(update_fields=['org_role'])
        # Legacy sync: keep profile.org_role in sync if this is the user's primary org
        try:
            profile = membership.user.profile
            if profile.organization_id == org.pk:
                profile.org_role = new_org_role
                profile.save(update_fields=['org_role'])
        except UserProfile.DoesNotExist:
            pass
        messages.success(request, 'Member role updated.')
    return redirect('tickets:member_list')


@login_required
@require_http_methods(["POST"])
def org_switch(request):
    """Switch the active organization for the current user."""
    org_id = request.POST.get('org_id', '').strip()
    membership = get_object_or_404(
        OrganizationMembership.objects.select_related('organization'),
        user=request.user,
        organization_id=org_id,
    )
    clear_org_cache(request)
    request.session['_org_id'] = str(membership.organization_id)
    # Seed the per-request cache so any subsequent get_organization() call in
    # this same request sees the new org without re-querying.
    request._cached_org = membership.organization
    request._cached_org_set = True
    messages.success(request, f"Switched to {membership.organization.name}.")
    next_url = request.POST.get('next', '')
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect('tickets:home')


# ---------------------------------------------------------------------------
# Finance / Stripe Connect
# ---------------------------------------------------------------------------

_MIN_PAYOUT = Decimal('1.00')


def _compute_available_balance(org):
    """Return (stripe_revenue, platform_fees, paid_out, available_balance) for the given org."""
    completed_sessions = StripeCheckoutSession.objects.filter(
        organization=org, status=StripeCheckoutSession.Status.COMPLETED,
    )
    agg = completed_sessions.aggregate(
        total_charged=Coalesce(Sum('amount_total_cents'), 0),
        total_fees=Coalesce(Sum('platform_fee_cents'), 0),
    )
    stripe_revenue = Decimal(str(agg['total_charged'])) / 100
    platform_fees = Decimal(str(agg['total_fees'])) / 100

    # Deduct all non-failed payouts — PENDING and IN_TRANSIT are already committed
    # to the connected account, so they must not be available for re-request.
    paid_out = Payout.objects.filter(
        organization=org,
    ).exclude(status=Payout.Status.FAILED).aggregate(
        total=Coalesce(Sum('amount'), Decimal('0.00'))
    )['total']

    organizer_revenue = stripe_revenue - platform_fees
    return stripe_revenue, platform_fees, paid_out, organizer_revenue - paid_out


_STRIPE_PLATFORM_AVAILABLE_CACHE_KEY = 'stripe_platform_available_cents'
_STRIPE_PLATFORM_AVAILABLE_CACHE_TTL = 60


def _get_stripe_platform_available_cents(use_cache=False):
    """
    Query the Stripe platform account's available balance in cents.
    Used both for the finance display (cached) and as a safety check before
    initiating a Transfer (uncached). Returns None on error.
    """
    if use_cache:
        cached = django_cache.get(_STRIPE_PLATFORM_AVAILABLE_CACHE_KEY)
        if cached is not None:
            return cached

    import stripe as stripe_lib
    from django.conf import settings as django_settings
    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
    try:
        balance = stripe_lib.Balance.retrieve()
        amount = 0
        for entry in balance.available:
            if entry.currency.lower() == django_settings.STRIPE_CURRENCY.lower():
                amount = entry.amount
                break
        if use_cache:
            django_cache.set(
                _STRIPE_PLATFORM_AVAILABLE_CACHE_KEY,
                amount,
                _STRIPE_PLATFORM_AVAILABLE_CACHE_TTL,
            )
        return amount
    except Exception:
        logger.exception("Could not retrieve Stripe platform balance")
        return None


def _compute_settled_payout_balance(org):
    """
    Return the amount available for payout for this org, in dollars.

    Counts sessions whose funds have an explicit available_on <= now, plus
    sessions with available_on=NULL (payments that pre-date that field, treated
    as already settled per the model's documented intent).
    Subtracts completed payouts.
    """
    from django.utils import timezone as django_tz
    now = django_tz.now()
    settled_sessions = StripeCheckoutSession.objects.filter(
        organization=org,
        status=StripeCheckoutSession.Status.COMPLETED,
    ).filter(
        Q(available_on__lte=now) | Q(available_on__isnull=True)
    )

    agg = settled_sessions.aggregate(
        total_charged=Coalesce(Sum('amount_total_cents'), 0),
        total_fees=Coalesce(Sum('platform_fee_cents'), 0),
    )
    settled_organizer_cents = agg['total_charged'] - agg['total_fees']

    paid_out = Payout.objects.filter(
        organization=org,
    ).exclude(status=Payout.Status.FAILED).aggregate(
        total=Coalesce(Sum('amount'), Decimal('0.00'))
    )['total']

    settled = Decimal(str(settled_organizer_cents)) / 100 - paid_out
    return max(Decimal('0.00'), settled)


def _extract_bank_account(acct):
    ext_accounts = []
    if isinstance(acct, dict):
        ext_accounts = (acct.get('external_accounts') or {}).get('data', [])
    else:
        external_accounts = getattr(acct, 'external_accounts', None)
        if external_accounts is not None:
            if isinstance(external_accounts, dict):
                ext_accounts = external_accounts.get('data', [])
            else:
                ext_accounts = getattr(external_accounts, 'data', []) or []
    if not ext_accounts:
        return None
    ba = ext_accounts[0]
    if not isinstance(ba, dict):
        ba = {
            'bank_name': getattr(ba, 'bank_name', None),
            'last4': getattr(ba, 'last4', ''),
            'currency': getattr(ba, 'currency', 'usd'),
        }
    return {
        'bank_name': ba.get('bank_name') or 'Bank',
        'last4': ba.get('last4', ''),
        'currency': (ba.get('currency') or 'usd').upper(),
    }


def _get_connected_account_state(stripe_lib, connected_account_id):
    acct = stripe_lib.Account.retrieve(
        connected_account_id,
        expand=['external_accounts'],
    )
    bank_account = _extract_bank_account(acct)
    payouts_ready = bool(
        getattr(acct, 'details_submitted', False)
        and getattr(acct, 'charges_enabled', False)
        and getattr(acct, 'payouts_enabled', False)
    )
    return {
        'account': acct,
        'bank_account': bank_account,
        'payouts_ready': payouts_ready,
    }


def _sync_org_payout_readiness(org, payouts_ready):
    if org.stripe_onboarding_complete != payouts_ready:
        org.stripe_onboarding_complete = payouts_ready
        org.save(update_fields=['stripe_onboarding_complete'])


def _ensure_manual_payout_schedule(stripe_lib, connected_account_id):
    """Use manual payouts so organizer requests map to explicit bank payouts."""
    stripe_lib.Account.modify(
        connected_account_id,
        settings={
            'payouts': {
                'schedule': {
                    'interval': 'manual',
                }
            }
        },
    )


def _read_stripe_capability(capabilities, name):
    """Read one capability state from a Stripe Account.capabilities object.

    The Stripe Python SDK exposes capabilities as a `Capabilities`
    StripeObject which supports `__getitem__` and attribute access but
    NOT `.get()`. Plain dicts (used in tests) support `.get()`. This
    helper handles both so we can't silently miss an active capability
    because of the type mismatch.
    """
    if capabilities is None:
        return None
    if hasattr(capabilities, 'to_dict'):
        return capabilities.to_dict().get(name)
    if isinstance(capabilities, dict):
        return capabilities.get(name)
    return None


def _request_card_payments_capability(stripe_lib, connected_account_id):
    """Idempotently request the `card_payments` capability on a Connect account.

    Stripe Express onboarding sometimes leaves capabilities in the
    `unrequested` state if they weren't asked for at create-time. This is
    the documented unstick: re-requesting an already-active capability is
    a no-op for Stripe. Returns the refreshed Account on success, None on
    Stripe error (logged at WARN — non-fatal for the caller).
    """
    try:
        return stripe_lib.Account.modify(
            connected_account_id,
            capabilities={'card_payments': {'requested': True}},
        )
    except stripe_lib.error.StripeError as exc:
        logger.warning(
            "Could not request card_payments capability for %s: %s",
            connected_account_id, exc,
        )
        return None


def _derive_tap_to_pay_ui_state(account):
    """Map a Stripe Account object to the state the finance template renders.

    Tap to Pay on iPhone runs on the `card_payments` capability — Stripe
    Connect does not expose a separate `tap_to_pay_payments` capability.
    Apple's entitlement + T&C acceptance are handled on the iOS client.

    Returns {'status', 'country'} where status is one of:
    'pending', 'enabled', 'unsupported'.
    """
    from django.conf import settings as django_settings

    country = (getattr(account, 'country', '') or '').upper()
    card_cap = _read_stripe_capability(getattr(account, 'capabilities', None), 'card_payments')

    if country and country not in django_settings.TAP_TO_PAY_SUPPORTED_COUNTRIES:
        status = 'unsupported'
    elif card_cap == 'active':
        status = 'enabled'
    else:
        status = 'pending'

    return {
        'status': status,
        'country': country,
    }

@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def finance_overview(request):
    """Finance overview: revenue stats, bank account status, payout history."""
    org = get_organization(request)

    stripe_revenue, platform_fees, paid_out, available_balance = _compute_available_balance(org)

    payout_history = (
        Payout.objects.filter(organization=org)
        .select_related('initiated_by')
    )

    net_sales = stripe_revenue - platform_fees

    stripe_available = None
    settling_balance = Decimal('0.00')
    bank_account = None
    tap_to_pay_ui = None
    if org.stripe_account_id:
        try:
            import stripe as stripe_lib
            from django.conf import settings as django_settings
            stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
            stripe_state = _get_connected_account_state(stripe_lib, org.stripe_account_id)
            bank_account = stripe_state['bank_account']
            tap_to_pay_ui = _derive_tap_to_pay_ui_state(stripe_state['account'])
            _sync_org_payout_readiness(org, stripe_state['payouts_ready'])
        except Exception:
            logger.exception("Could not retrieve Stripe account state for org %s", org.id)

    if org.stripe_onboarding_complete and org.stripe_account_id:
        db_settled = _compute_settled_payout_balance(org)
        stripe_actual_cents = _get_stripe_platform_available_cents(use_cache=True)
        if stripe_actual_cents is not None:
            stripe_actual = Decimal(str(stripe_actual_cents)) / 100
            if stripe_actual < db_settled:
                logger.warning(
                    "Stripe-available below DB-settled for org %s: db=%s stripe=%s gap=%s",
                    org.id, db_settled, stripe_actual, db_settled - stripe_actual,
                )
            stripe_available = min(db_settled, stripe_actual)
        else:
            stripe_available = db_settled
        settling_balance = max(Decimal('0.00'), available_balance - stripe_available)

    legacy_pending_payouts = Payout.objects.filter(
        organization=org,
        status=Payout.Status.PENDING,
        stripe_transfer_id__isnull=False,
        stripe_payout_id__isnull=True,
    ).count()

    context = {
        'net_sales': net_sales,
        'paid_out': paid_out,
        'available_balance': available_balance,
        'stripe_available': stripe_available,
        'settling_balance': settling_balance,
        'payout_history': payout_history,
        'onboarding_complete': org.stripe_onboarding_complete,
        'has_stripe_account': bool(org.stripe_account_id),
        'min_payout': _MIN_PAYOUT,
        'bank_account': bank_account,
        'tap_to_pay_ui': tap_to_pay_ui,
        'legacy_pending_payouts': legacy_pending_payouts,
    }
    return render(request, 'tickets/finance/overview.html', context)


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def ai_token_usage_dashboard(request):
    """Monthly AI token usage breakdown for billing review."""
    from .services.ai_metering import monthly_ai_token_usage_breakdown, to_cue_tokens

    org = get_organization(request)
    today = django_tz.localdate()

    def _parse_month_key(value):
        try:
            year_str, month_str = value.split('-', 1)
            year = int(year_str)
            month = int(month_str)
        except (ValueError, AttributeError):
            return None
        if not (1 <= month <= 12) or not (2000 <= year <= 2100):
            return None
        return year, month

    selected = _parse_month_key(request.GET.get('month_key', ''))
    if selected is None:
        selected = (today.year, today.month)
    year, month = selected

    breakdown = monthly_ai_token_usage_breakdown(org, year, month)

    month_options = []
    cursor_year, cursor_month = today.year, today.month
    for _ in range(12):
        key = f"{cursor_year:04d}-{cursor_month:02d}"
        label = date(cursor_year, cursor_month, 1).strftime('%B %Y')
        month_options.append({
            'key': key,
            'label': label,
            'selected': (cursor_year == year and cursor_month == month),
        })
        cursor_month -= 1
        if cursor_month == 0:
            cursor_month = 12
            cursor_year -= 1

    totals_cue = {
        'prompt_tokens': to_cue_tokens(breakdown['totals']['prompt_tokens']),
        'completion_tokens': to_cue_tokens(breakdown['totals']['completion_tokens']),
        'total_tokens': to_cue_tokens(breakdown['totals']['total_tokens']),
    }
    by_feature_cue = [
        {
            'feature': row['feature'],
            'feature_label': row['feature_label'],
            'prompt_tokens': to_cue_tokens(row['prompt_tokens']),
            'completion_tokens': to_cue_tokens(row['completion_tokens']),
            'total_tokens': to_cue_tokens(row['total_tokens']),
        }
        for row in breakdown['by_feature']
    ]
    daily_json = json.dumps([
        {'date': row['date'].isoformat(), 'total_tokens': to_cue_tokens(row['total_tokens'])}
        for row in breakdown['daily']
    ])

    selected_label = date(year, month, 1).strftime('%B %Y')

    context = {
        'totals': totals_cue,
        'by_feature': by_feature_cue,
        'daily_json': daily_json,
        'month_options': month_options,
        'selected_month_key': f"{year:04d}-{month:02d}",
        'selected_month_label': selected_label,
    }
    return render(request, 'tickets/ai_token_usage.html', context)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def stripe_connect_onboard(request):
    """Create (or reuse) a Stripe Express account and redirect to onboarding."""
    import stripe as stripe_lib
    from django.urls import reverse
    from django.conf import settings as django_settings
    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
    org = get_organization(request)

    try:
        if not org.stripe_account_id:
            account = stripe_lib.Account.create(
                type='express',
                metadata={'org_id': str(org.id)},
                capabilities={
                    'card_payments': {'requested': True},
                    'transfers': {'requested': True},
                },
            )
            org.stripe_account_id = account.id
            org.save(update_fields=['stripe_account_id'])

        account_link = stripe_lib.AccountLink.create(
            account=org.stripe_account_id,
            refresh_url=request.build_absolute_uri(reverse('tickets:stripe_connect_refresh')),
            return_url=request.build_absolute_uri(reverse('tickets:stripe_connect_return')),
            type='account_onboarding',
        )
        return redirect(account_link.url)
    except stripe_lib.error.StripeError as e:
        messages.error(request, f'Could not start Stripe onboarding: {getattr(e, "user_message", None) or str(e)}')
        return redirect('tickets:finance_overview')


def _redirect_to_custom_scheme(target):
    # Django's HttpResponseRedirect blocks non-http(s)/ftp schemes for safety,
    # so we hand-roll the 302 to let iOS Safari follow the cueup:// link.
    from django.http import HttpResponse
    resp = HttpResponse(status=302)
    resp['Location'] = target
    return resp


def mobile_stripe_connect_return(request):
    """HTTPS bridge that Stripe Connect redirects to after KYC completes.

    Stripe's AccountLink API rejects custom URI schemes on return_url /
    refresh_url, so we hand Stripe an https://.../m/stripe-connect-return/
    URL and 302 to the iOS app's cueup:// deep link from here. iOS Safari
    follows the 302 and hands control back to the app's Linking listener.
    """
    return _redirect_to_custom_scheme('cueup://stripe-connect-return')


def mobile_stripe_connect_refresh(request):
    """HTTPS bridge for the Stripe AccountLink refresh_url (link expired)."""
    return _redirect_to_custom_scheme('cueup://stripe-connect-refresh')


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def enable_tap_to_pay(request):
    """Request the `card_payments` capability on the org's Connect account.

    Tap to Pay on iPhone (and every other in-person payment) rides on the
    `card_payments` capability. Express accounts sometimes leave it in the
    `unrequested` state — this endpoint is the manual unstick. If Stripe
    responds with outstanding requirements, the merchant is redirected
    through a fresh Account Link to fill them in.
    """
    import stripe as stripe_lib
    from django.core.cache import cache as django_cache
    from django.urls import reverse
    from django.conf import settings as django_settings

    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
    org = get_organization(request)

    if not org.stripe_account_id:
        messages.error(
            request,
            'Connect your Stripe account first, then come back to enable in-person payments.',
        )
        return redirect('tickets:finance_overview')

    try:
        pre_state = _get_connected_account_state(stripe_lib, org.stripe_account_id)
        pre_ui = _derive_tap_to_pay_ui_state(pre_state['account'])
    except stripe_lib.error.StripeError as exc:
        messages.error(
            request,
            f'Could not check your Stripe account: {getattr(exc, "user_message", None) or str(exc)}',
        )
        return redirect('tickets:finance_overview')

    if pre_ui['status'] == 'unsupported':
        messages.error(request, "Tap to Pay on iPhone isn't supported in your country yet.")
        return redirect('tickets:finance_overview')

    if pre_ui['status'] == 'enabled':
        django_cache.delete(f'tap_to_pay_status:{org.pk}')
        messages.info(request, 'In-person payments are already enabled on your account.')
        return redirect('tickets:finance_overview')

    refreshed = _request_card_payments_capability(stripe_lib, org.stripe_account_id)
    django_cache.delete(f'tap_to_pay_status:{org.pk}')

    if refreshed is None:
        messages.error(
            request,
            'We couldn\'t reach Stripe to request in-person payments. Please try again in a moment.',
        )
        return redirect('tickets:finance_overview')

    requirements = getattr(refreshed, 'requirements', None)
    currently_due = list(getattr(requirements, 'currently_due', None) or []) if requirements else []
    if currently_due:
        try:
            account_link = stripe_lib.AccountLink.create(
                account=org.stripe_account_id,
                refresh_url=request.build_absolute_uri(reverse('tickets:stripe_connect_refresh')),
                return_url=request.build_absolute_uri(reverse('tickets:stripe_connect_return')),
                type='account_onboarding',
                collect='currently_due',
            )
            return redirect(account_link.url)
        except stripe_lib.error.StripeError as exc:
            messages.error(
                request,
                f'Could not open Stripe onboarding: {getattr(exc, "user_message", None) or str(exc)}',
            )
            return redirect('tickets:finance_overview')

    messages.success(
        request,
        'In-person payments requested. Stripe usually activates them within a few minutes — '
        'pull to refresh in the iOS app to check.',
    )
    return redirect('tickets:finance_overview')


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def stripe_connect_return(request):
    """Stripe redirects here after the organizer completes (or abandons) onboarding."""
    import stripe as stripe_lib
    from django.core.cache import cache as django_cache
    from django.conf import settings as django_settings
    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
    org = get_organization(request)

    if org.stripe_account_id:
        # Idempotently re-request card_payments — Express accounts created
        # before we asked for it at create-time can be left in 'unrequested'
        # and this is the surest way to unstick them.
        _request_card_payments_capability(stripe_lib, org.stripe_account_id)
        django_cache.delete(f'tap_to_pay_status:{org.pk}')

        try:
            stripe_state = _get_connected_account_state(stripe_lib, org.stripe_account_id)
            _sync_org_payout_readiness(org, stripe_state['payouts_ready'])
            if stripe_state['payouts_ready']:
                messages.success(request, 'Bank account connected successfully. You can now request payouts.')
            else:
                messages.warning(
                    request,
                    'Bank account connection is incomplete for payouts. Please finish Stripe onboarding to enable bank payouts.',
                )
        except stripe_lib.error.StripeError as e:
            messages.error(request, f'Could not verify Stripe account: {getattr(e, "user_message", None) or str(e)}')

    return redirect('tickets:finance_overview')


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def stripe_connect_refresh(request):
    """Re-create an expired onboarding link and redirect the organizer."""
    import stripe as stripe_lib
    from django.urls import reverse
    from django.conf import settings as django_settings
    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
    org = get_organization(request)

    if not org.stripe_account_id:
        messages.error(request, 'No Stripe account found. Please start onboarding again.')
        return redirect('tickets:finance_overview')

    try:
        account_link = stripe_lib.AccountLink.create(
            account=org.stripe_account_id,
            refresh_url=request.build_absolute_uri(reverse('tickets:stripe_connect_refresh')),
            return_url=request.build_absolute_uri(reverse('tickets:stripe_connect_return')),
            type='account_onboarding',
        )
        return redirect(account_link.url)
    except stripe_lib.error.StripeError as e:
        messages.error(request, f'Could not refresh onboarding link: {getattr(e, "user_message", None) or str(e)}')
        return redirect('tickets:finance_overview')


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def stripe_account_login(request):
    """Generate a Stripe Express dashboard login link and redirect the organizer."""
    import stripe as stripe_lib
    from django.conf import settings as django_settings
    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
    if not stripe_lib.api_key:
        logger.error("STRIPE_SECRET_KEY is not configured")
        messages.error(request, "Stripe is not configured. Contact support.")
        return redirect("tickets:finance_overview")
    org = get_organization(request)
    if not org.stripe_account_id:
        messages.error(request, "No connected bank account found.")
        return redirect("tickets:finance_overview")
    try:
        stripe_state = _get_connected_account_state(stripe_lib, org.stripe_account_id)
        _sync_org_payout_readiness(org, stripe_state['payouts_ready'])
        if not stripe_state['payouts_ready']:
            messages.error(request, "Stripe onboarding is not complete for payouts yet.")
            return redirect("tickets:finance_overview")
        login_link = stripe_lib.Account.create_login_link(org.stripe_account_id)
        return redirect(login_link.url)
    except stripe_lib.error.StripeError as e:
        logger.exception("Stripe login link error for account %s: %s", org.stripe_account_id, e)
        messages.error(request, "Could not open Stripe dashboard. Please try again.")
        return redirect("tickets:finance_overview")


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def stripe_disconnect(request):
    """Unlink the Stripe Connect account from this organization."""
    org = get_organization(request)
    if not org.stripe_account_id:
        messages.error(request, "No Stripe account is connected.")
        return redirect("tickets:finance_overview")
    old_account_id = org.stripe_account_id
    org.stripe_account_id = ""
    org.stripe_onboarding_complete = False
    org.save(update_fields=["stripe_account_id", "stripe_onboarding_complete"])
    logger.info("Stripe account %s disconnected from org %s by user %s", old_account_id, org.id, request.user.id)
    messages.success(request, "Bank account disconnected. You can reconnect a new account at any time.")
    return redirect("tickets:finance_overview")


@login_required
@require_org
@require_owner
@require_http_methods(["POST"])
def initiate_payout(request):
    """Initiate a Stripe Transfer and connected-account bank payout."""
    import stripe as stripe_lib
    from django.conf import settings as django_settings
    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
    org = get_organization(request)

    if not org.stripe_account_id:
        messages.error(request, 'Please connect your bank account before requesting a payout.')
        return redirect('tickets:finance_overview')

    try:
        stripe_state = _get_connected_account_state(stripe_lib, org.stripe_account_id)
        _sync_org_payout_readiness(org, stripe_state['payouts_ready'])
    except stripe_lib.error.StripeError as e:
        messages.error(request, f'Could not verify your Stripe payout settings: {getattr(e, "user_message", None) or str(e)}')
        return redirect('tickets:finance_overview')

    if not stripe_state['payouts_ready']:
        messages.error(request, 'Your bank account is not yet enabled for payouts. Please finish Stripe onboarding.')
        return redirect('tickets:finance_overview')

    raw_amount = request.POST.get('amount', '').strip()
    try:
        amount = Decimal(raw_amount)
    except Exception:
        messages.error(request, 'Invalid payout amount.')
        return redirect('tickets:finance_overview')

    if amount < _MIN_PAYOUT:
        messages.error(request, f'Minimum payout is ${_MIN_PAYOUT:.2f}.')
        return redirect('tickets:finance_overview')

    _, _, _, available_balance = _compute_available_balance(org)
    if amount > available_balance:
        messages.error(request, f'Payout amount exceeds available balance (${available_balance:.2f}).')
        return redirect('tickets:finance_overview')

    stripe_available = _compute_settled_payout_balance(org)
    if amount > stripe_available:
        messages.error(
            request,
            f'Only ${stripe_available:.2f} has settled and is available to pay out. '
            f'Funds from recent sales typically settle within 2\u20137 business days.',
        )
        return redirect('tickets:finance_overview')

    stripe_actual_cents = _get_stripe_platform_available_cents()
    if stripe_actual_cents is not None:
        stripe_actual = Decimal(str(stripe_actual_cents)) / 100
        if amount > stripe_actual:
            messages.error(
                request,
                f'Your Stripe balance has ${stripe_actual:.2f} available right now. '
                f'Funds from recent sales typically settle within 2\u20137 business days.',
            )
            return redirect('tickets:finance_overview')

    notes = request.POST.get('notes', '').strip()[:500]
    payout = Payout.objects.create(
        organization=org,
        amount=amount,
        status=Payout.Status.PENDING,
        initiated_by=request.user,
        notes=notes,
    )

    try:
        _ensure_manual_payout_schedule(stripe_lib, org.stripe_account_id)
        transfer = stripe_lib.Transfer.create(
            amount=int(amount * 100),
            currency=django_settings.STRIPE_CURRENCY,
            destination=org.stripe_account_id,
            description=f'Payout to {org.name}',
            metadata={'org_id': str(org.id), 'payout_id': str(payout.id)},
        )
        payout.stripe_transfer_id = transfer.id
        payout.save(update_fields=['stripe_transfer_id'])

        stripe_payout = stripe_lib.Payout.create(
            amount=int(amount * 100),
            currency=django_settings.STRIPE_CURRENCY,
            metadata={'org_id': str(org.id), 'payout_id': str(payout.id)},
            stripe_account=org.stripe_account_id,
        )
        payout.stripe_payout_id = stripe_payout.id
        payout_status_map = {
            'in_transit': Payout.Status.IN_TRANSIT,
            'paid': Payout.Status.COMPLETED,
            'failed': Payout.Status.FAILED,
            'canceled': Payout.Status.FAILED,
        }
        update_fields = ['stripe_payout_id']
        new_status = payout_status_map.get(getattr(stripe_payout, 'status', None))
        if new_status and payout.status != new_status:
            payout.status = new_status
            update_fields.append('status')
        payout.save(update_fields=update_fields)
        messages.success(request, f'Payout of ${amount:.2f} processing. Funds will arrive in 1–5 business days.')
    except stripe_lib.error.StripeError as e:
        transfer = locals().get('transfer')
        if transfer is not None:
            try:
                stripe_lib.Transfer.create_reversal(
                    transfer.id,
                    metadata={'org_id': str(org.id), 'payout_id': str(payout.id), 'reason': 'payout_create_failed'},
                )
            except stripe_lib.error.StripeError:
                logger.exception("Could not reverse transfer %s after payout failure for payout %s", transfer.id, payout.id)
        payout.status = Payout.Status.FAILED
        error_note = f' [Stripe error: {str(e)[:400]}]'
        payout.notes = (payout.notes + error_note)[:500]
        update_fields = ['status', 'notes']
        if transfer is not None and payout.stripe_transfer_id != transfer.id:
            payout.stripe_transfer_id = transfer.id
            update_fields.append('stripe_transfer_id')
        payout.save(update_fields=update_fields)
        messages.error(request, f'Payout failed: {getattr(e, "user_message", None) or str(e)}')

    return redirect('tickets:finance_overview')


@login_required
@require_org
@require_owner
@require_http_methods(["POST"])
def recover_pending_payouts(request):
    """Create missing Stripe payouts for legacy transfer-only pending payouts."""
    import stripe as stripe_lib
    from django.conf import settings as django_settings

    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
    org = get_organization(request)

    if not org.stripe_account_id:
        messages.error(request, 'Please connect your bank account before recovering pending payouts.')
        return redirect('tickets:finance_overview')

    try:
        stripe_state = _get_connected_account_state(stripe_lib, org.stripe_account_id)
        _sync_org_payout_readiness(org, stripe_state['payouts_ready'])
        if not stripe_state['payouts_ready']:
            messages.error(request, 'Your bank account is not yet enabled for payouts. Please finish Stripe onboarding.')
            return redirect('tickets:finance_overview')
        _ensure_manual_payout_schedule(stripe_lib, org.stripe_account_id)
    except stripe_lib.error.StripeError as e:
        messages.error(request, f'Could not prepare Stripe payouts: {getattr(e, "user_message", None) or str(e)}')
        return redirect('tickets:finance_overview')

    pending_payouts = list(
        Payout.objects.filter(
            organization=org,
            status=Payout.Status.PENDING,
            stripe_transfer_id__isnull=False,
            stripe_payout_id__isnull=True,
        ).order_by('created_at')
    )
    if not pending_payouts:
        messages.warning(request, 'There are no pending payouts that need bank payout recovery.')
        return redirect('tickets:finance_overview')

    recovered = 0
    failed = 0
    for payout in pending_payouts:
        try:
            stripe_payout = stripe_lib.Payout.create(
                amount=int(payout.amount * 100),
                currency=django_settings.STRIPE_CURRENCY,
                metadata={'org_id': str(org.id), 'payout_id': str(payout.id)},
                stripe_account=org.stripe_account_id,
            )
            payout.stripe_payout_id = stripe_payout.id
            payout_status_map = {
                'in_transit': Payout.Status.IN_TRANSIT,
                'paid': Payout.Status.COMPLETED,
                'failed': Payout.Status.FAILED,
                'canceled': Payout.Status.FAILED,
            }
            update_fields = ['stripe_payout_id']
            new_status = payout_status_map.get(getattr(stripe_payout, 'status', None))
            if new_status and payout.status != new_status:
                payout.status = new_status
                update_fields.append('status')
            payout.save(update_fields=update_fields)
            recovered += 1
        except stripe_lib.error.StripeError as e:
            error_note = f' [Recovery failed: {str(e)[:400]}]'
            payout.notes = (payout.notes + error_note)[:500]
            payout.save(update_fields=['notes'])
            failed += 1

    if recovered and not failed:
        messages.success(request, f'Sent {recovered} pending payout(s) to the organizer bank account for processing.')
    elif recovered and failed:
        messages.warning(request, f'Sent {recovered} pending payout(s) to the bank, but {failed} still need attention.')
    else:
        messages.error(request, 'Could not recover any pending payouts. Check Stripe account status and try again.')

    return redirect('tickets:finance_overview')


# ---------------------------------------------------------------------------
# External Survey views
# ---------------------------------------------------------------------------

@login_required
@require_org
def survey_upload_list(request):
    org = get_organization(request)
    uploads = ExternalSurveyUpload.objects.filter(organization=org).order_by('-uploaded_at')
    return render(request, 'tickets/survey_upload_list.html', {'uploads': uploads})


@login_required
@require_org
def survey_upload_create(request):
    org = get_organization(request)
    if request.method == 'POST':
        form = SurveyUploadForm(request.POST, request.FILES)
        if form.is_valid():
            csv_file = form.cleaned_data['csv_file']
            upload = ExternalSurveyUpload.objects.create(
                organization=org,
                filename=csv_file.name,
                status=ExternalSurveyUpload.Status.PROCESSING,
            )
            try:
                from .services.external_survey.parser import ExternalSurveyParser
                parser = ExternalSurveyParser(organization=org, upload=upload)
                result = parser.parse(csv_file)
                upload.row_count = result['rows_inserted']
                upload.status = ExternalSurveyUpload.Status.COMPLETED
                upload.save(update_fields=['row_count', 'status', 'error_log'])
                messages.success(
                    request,
                    f'Uploaded {result["rows_inserted"]} responses'
                    + (f' ({result["rows_skipped"]} skipped).' if result['rows_skipped'] else '.'),
                )
                return redirect('tickets:survey_event_link', upload_id=upload.id)
            except Exception as exc:
                upload.status = ExternalSurveyUpload.Status.FAILED
                upload.error_log = json.dumps([str(exc)])
                upload.save(update_fields=['status', 'error_log'])
                messages.error(request, f'Parse failed: {exc}')
    else:
        form = SurveyUploadForm()
    return render(request, 'tickets/survey_upload_form.html', {'form': form})


@login_required
@require_org
def survey_upload_detail(request, upload_id):
    org = get_organization(request)
    upload = get_object_or_404(ExternalSurveyUpload.objects.filter(organization=org), id=upload_id)
    cities = (
        ExternalSurveyResponse.objects.filter(upload=upload)
        .values_list('city', flat=True)
        .distinct()
        .order_by('city')
    )
    return render(request, 'tickets/survey_upload_detail.html', {
        'upload': upload,
        'cities': cities,
    })


@login_required
@require_org
def survey_upload_delete(request, upload_id):
    if request.method != 'POST':
        return redirect('tickets:survey_upload_list')
    org = get_organization(request)
    upload = get_object_or_404(ExternalSurveyUpload.objects.filter(organization=org), id=upload_id)
    upload.hard_delete()
    messages.success(request, 'Survey upload deleted.')
    return redirect('tickets:survey_upload_list')


@login_required
@require_org
def survey_event_link(request, upload_id):
    from collections import defaultdict
    org = get_organization(request)
    upload = get_object_or_404(ExternalSurveyUpload.objects.filter(organization=org), id=upload_id)

    responses = (
        ExternalSurveyResponse.objects.filter(upload=upload)
        .select_related('event')
        .order_by('-responded_at')
    )
    events = Event.objects.filter(organization=org).order_by('-start_date')

    if request.method == 'POST':
        mapping = {}
        for resp in responses:
            mapping[str(resp.id)] = request.POST.get(f'event_{resp.id}', '').strip()

        valid_event_ids = set(
            str(eid) for eid in Event.objects.filter(organization=org).values_list('id', flat=True)
        )
        by_event = defaultdict(list)
        for resp_id, event_id in mapping.items():
            by_event[event_id].append(resp_id)

        # Collect event IDs affected before and after the update for cache invalidation.
        # queryset.update() bypasses post_save signals — must invalidate manually.
        old_event_ids = set(
            str(eid) for eid in
            ExternalSurveyResponse.objects.filter(upload=upload, event__isnull=False)
            .values_list('event_id', flat=True)
        )
        for event_id, resp_ids in by_event.items():
            if event_id and event_id in valid_event_ids:
                ExternalSurveyResponse.objects.filter(id__in=resp_ids).update(event_id=event_id)
            else:
                ExternalSurveyResponse.objects.filter(id__in=resp_ids).update(event=None)

        # Invalidate cache for all affected events (old assignments + new assignments)
        new_event_ids = set(eid for eid in by_event if eid and eid in valid_event_ids)
        for eid in old_event_ids | new_event_ids:
            django_cache.delete(_event_stats_cache_key(eid))
            _invalidate_event_upload_stats_cache(eid)

        messages.success(request, 'Event links saved.')
        return redirect(f"{reverse_lazy('tickets:survey_analytics')}?upload={upload.id}")

    return render(request, 'tickets/survey_event_link.html', {
        'upload': upload,
        'responses': responses,
        'events': events,
    })


@login_required
@require_org
def survey_analytics(request):
    org = get_organization(request)
    city_filter = request.GET.get('city', '').strip() or None

    from .services.external_survey.analytics import ExternalSurveyAnalytics
    stats = ExternalSurveyAnalytics(organization=org).calculate(city=city_filter)

    feedback_qs = ExternalSurveyResponse.objects.filter(organization=org)
    if city_filter:
        feedback_qs = feedback_qs.filter(event__venue__city=city_filter)
    feedback_qs = feedback_qs.select_related('event', 'event__venue').order_by('-responded_at')
    paginator = Paginator(feedback_qs, 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    distinct_cities = sorted(set(
        c for c in ExternalSurveyResponse.objects.filter(organization=org)
        .filter(event__isnull=False, event__venue__isnull=False)
        .exclude(event__venue__city='')
        .values_list('event__venue__city', flat=True)
        .distinct()
        if c
    ))

    return render(request, 'tickets/survey_analytics.html', {
        'stats': stats,
        'city_filter': city_filter,
        'distinct_cities': distinct_cities,
        'page_obj': page_obj,
        'rating_labels': [r['overall_rating'] for r in stats['rating_breakdown']],
        'rating_counts': [r['count'] for r in stats['rating_breakdown']],
    })


# ── Error handlers ──────────────────────────────────────────────────────────

def csrf_failure(request, reason=''):
    """Custom CSRF failure view - renders the branded 403 template."""
    logger.warning("CSRF failure: %s", reason)
    return render(request, '403.html', {'reason': reason, 'csrf_token_missing': True}, status=403)
