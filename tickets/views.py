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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from django import forms as django_forms
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.urls import reverse, reverse_lazy
from django.conf import settings
from django.db.models import (
    Sum, Count, Avg, Max, Min, Q, Subquery, OuterRef, Prefetch,
    Case, When, Value, F, CharField, Exists, ExpressionWrapper, DecimalField, IntegerField,
)
from django.db.models.functions import Coalesce, Greatest, TruncDate, Cast, TruncMonth, TruncQuarter
from django.db import models
from django.core.paginator import Paginator
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.http import JsonResponse, Http404, HttpResponse, HttpResponseBadRequest, FileResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.db import connection, IntegrityError, transaction
from django.utils import timezone as django_tz
from django.utils.dateparse import parse_date, parse_datetime, parse_time
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify

from .models import (
    Organization, UserProfile, OrganizationMembership, OrganizationInvitation,
    AIRecommendation,
    CSVFormat, UploadedFile, Customer, CustomerTag, Event, EventExpense, EventEmailCampaign, EventSMSCampaign, TicketOrder, Ticket, Venue, Market,
    CustomField, CustomFieldOption, EventCustomFieldValue, IncomeSource, EventIncome,
    SurveyQuestion, SurveyQuestionOption, SurveyInvitation, SurveyResponse, SurveyAnswer, SurveyAnswerOption,
    DEFAULT_SURVEY_SUBJECT, SURVEY_SEND_OFFSET_CHOICES,
    PipedreamCalendarConnection, OrganizationAPIKey,
    SaleableTicketType, SaleableTicketTypeTier, StripeCheckoutSession, Payout, PromoCode,
    ExternalSurveyUpload, ExternalSurveyResponse, TypeformFormSubscription, EventDailyPageView,
    WaitlistEntry, OrganizerWaitlist,
    ScannerSession, generate_unique_scanner_pin, TrackingLink, _generate_tracking_token,
    LoyaltyProgram, LoyaltyTier, PhoneSuppression, SMSConsentRecord,
    EVENT_STATUS_DRAFT, EVENT_STATUS_LIVE, EVENT_STATUS_ENDED, EVENT_STATUS_CANCELLED,
    TICKETING_TYPE_DIRECT, TICKETING_TYPE_EXTERNAL,
)
from .forms import (
    EventCSVUploadForm, EventExpenseForm, TicketPriceEntryForm, CSVFormatForm,
    VenueForm, VenueChoiceField, EventForm, LoginForm,
    CustomFieldForm, CustomFieldOptionFormSet,
    IncomeSourceForm, EventIncomeForm,
    OTPVerificationForm, MemberInviteForm, AttendeePhoneForm, SubscribeForm,
    ProfileCompletionForm, EmailLoginForm, EmailProfileCompletionForm,
    SaleableTicketTypeForm, SaleableTicketTypeTierFormSet, PublicTicketPurchaseForm,
    DirectEventForm, DirectTicketTypeFormSet,
    PromoCodeForm, SurveyUploadForm, UserProfileForm, OrgProfileForm,
    OrgDisplayPreferencesForm, SegmentTuningForm,
    WaitlistJoinForm, OrganizerWaitlistForm,
    LoyaltyProgramForm, LoyaltyTierFormSet,
)
from .csv_processor import CSVProcessor
from .services.forecasting.preview import generate_forecast_preview
from .services.pricing import SmartPricingRecommender
from .services.markets import MarketBuilder, NO_MARKET_LABEL
from .services.churn_detection.churn_calculator import ChurnDetectionService, THRESHOLD_OPTIONS
from .services.segmentation import (
    BEHAVIOR_PROFILE_BADGE_COLORS,
    BEHAVIOR_PROFILE_DESCRIPTIONS,
    BEHAVIOR_PROFILE_ORDER,
)
from .services.segmentation.segment_definitions import (
    SEGMENT_BADGE_COLORS,
    SEGMENT_DESCRIPTIONS,
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
from .services.loyalty import (
    LoyaltyProgramStats,
    award_points_for_order,
    revoke_points_for_order,
    revoke_points_for_orders,
)
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
    fetch_org_reports_cached,
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
    get_cached_marketing_metrics,
)
from .services.marketing.analytics import DEFAULT_WINDOW, resolve_window
from .services.customer_filters import filter_customers, NO_MARKET_VALUE, market_filter_options
from .services.weather import get_event_weather_forecast, get_event_hourly_forecast
from .utils import get_organization, require_org, require_organizer, require_host, require_admin, require_owner, clear_org_cache, next_order_number, generate_qr_b64, link_customer_to_buyer, ticket_qr_payload
from .feature_flags import (
    smart_pricing_recommendations_enabled,
    browse_events_enabled,
)

from django.core.cache import cache as django_cache

from .cache_utils import safe_cache_delete, safe_cache_get, safe_cache_set
from .integrations.registry import marketing_providers

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


def _invalidate_event_campaign_match_cache(event_id):
    """Drop cached Mailchimp/SlickText/Meta match rankings for this event."""
    from tickets.services import campaign_match_cache
    for source in ("mailchimp", "slicktext", "meta"):
        try:
            campaign_match_cache.invalidate(source, event_id)
        except Exception:
            pass


EVENT_STATS_CACHE_VERSION = 5

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
    'audience',
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


@login_required
def admin_impersonate_start(request, user_id):
    """Log an internal Cue admin (superuser) in as another user for debugging.

    Reversible: the admin's own id is stashed in the session as
    ``_impersonator_id`` so the banner can offer a one-click restore. Only
    superusers may start impersonation, and privileged (staff/superuser)
    accounts can never be targeted.
    """
    from django.contrib.auth import login as auth_login
    from django.contrib.auth.models import User
    from django.http import HttpResponseForbidden

    if not request.user.is_superuser:
        return HttpResponseForbidden('Access denied.')

    target = get_object_or_404(User, pk=user_id)

    if target.is_superuser or target.is_staff:
        messages.error(request, 'Cannot impersonate an admin or staff account.')
        return redirect('/admin/tickets/userprofile/')

    if target == request.user:
        return redirect('tickets:home')

    admin_id = request.user.pk
    logger.warning("Impersonation START: admin %s -> user %s", admin_id, target.pk)

    # auth_login() flushes the session when switching to a different user, so
    # set the impersonation marker *after* it. The flush also clears the stale
    # _org_id, forcing get_organization() to re-resolve for the target.
    auth_login(request, target, backend='tickets.backends.EmailBackend')
    request.session['_impersonator_id'] = admin_id
    clear_org_cache(request)

    messages.warning(
        request,
        f'You are now impersonating {target.get_full_name() or target.email}.'
    )
    return redirect('tickets:home')


@login_required
@require_http_methods(["POST"])
def admin_impersonate_stop(request):
    """End an active impersonation and restore the original admin session.

    Authorizes on the presence of the ``_impersonator_id`` session key rather
    than on ``is_superuser`` — by this point request.user is the (non-superuser)
    impersonated target, and that key can only have been set by
    admin_impersonate_start().
    """
    from django.contrib.auth import login as auth_login, logout as auth_logout
    from django.contrib.auth.models import User

    impersonator_id = request.session.get('_impersonator_id')
    if not impersonator_id:
        return redirect('tickets:home')

    try:
        admin_user = User.objects.get(pk=impersonator_id)
    except User.DoesNotExist:
        auth_logout(request)
        return redirect('tickets:login')

    logger.warning(
        "Impersonation STOP: admin %s <- user %s",
        impersonator_id, request.user.pk,
    )
    # Logging back in as a different user flushes the session, which clears
    # _impersonator_id along with it.
    auth_login(request, admin_user, backend='tickets.backends.EmailBackend')
    clear_org_cache(request)
    return redirect('/admin/tickets/userprofile/')


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


def sms_consent(request):
    """Public SMS messaging terms page — used for Twilio toll-free verification opt-in URL."""
    return render(request, 'tickets/sms_consent.html')


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
    base_events = (
        Event.objects.filter(
            organization=org,
            status__in=[EVENT_STATUS_LIVE, EVENT_STATUS_ENDED],
            ticketing_type=TICKETING_TYPE_DIRECT,
            deleted_at__isnull=True,
        )
        .select_related('venue')
    )
    upcoming_events = base_events.filter(
        Q(end_date__isnull=False, end_date__gte=today) |
        Q(end_date__isnull=True, start_date__gte=today)
    ).order_by('start_date', 'start_time', 'name')
    past_events = base_events.filter(
        Q(end_date__isnull=False, end_date__lt=today) |
        Q(end_date__isnull=True, start_date__lt=today)
    ).order_by('-start_date', '-start_time', 'name')
    return render(request, 'tickets/public_org_profile.html', {
        'org': org,
        'upcoming_events': upcoming_events,
        'past_events': past_events,
    })


@login_required
def org_required(request):
    """Shown when user has no organization; prompt to create or join one."""
    return render(request, 'tickets/org_required.html')


@login_required
@require_http_methods(["GET", "POST"])
def create_organization(request):
    """Create a new organization and assign the current user to it."""
    from .forms import OrganizationForm
    from .services.org_onboarding import initialize_new_organization
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
            # Seed trial SMS credits after the org is committed (credit() locks the
            # org row). Idempotent + non-fatal — see initialize_new_organization.
            initialize_new_organization(org)
            clear_org_cache(request)
            request.session['_org_id'] = str(org.pk)
            messages.success(
                request,
                f"Welcome to Cue! '{org.name}' is ready — follow the Getting started "
                "steps below to launch your first event.",
            )
            return redirect('tickets:home')
    else:
        # Prefill the org name from the waitlist application so approved users
        # confirm-and-create rather than re-entering info they already gave.
        initial = {}
        approved = OrganizerWaitlist.objects.filter(
            email=request.user.email,
            status=OrganizerWaitlist.Status.APPROVED,
        ).order_by('-approved_at').first()
        if approved and approved.organization_name:
            initial['name'] = approved.organization_name
        form = OrganizationForm(initial=initial)
    return render(request, 'tickets/create_organization.html', {
        'form': form,
        'prefilled_from_waitlist': bool(initial.get('name')),
    })


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


def _onboarding_state(org, has_customers):
    """Build the dashboard "Getting started" checklist for a new organizer.

    Analytics-first: the guaranteed payoff is importing data to unlock customer
    segments; SMS is a consent-gated later step (its CTA never points at a send
    screen until there's an opted-in audience). Step completion is derived from
    existing data (no per-step flags). The card hides once every step is complete
    or the org dismisses it. ``has_customers`` is passed in from the home view's
    already-computed customer count to avoid re-querying it here.
    """
    empty = {'show': False, 'steps': [], 'complete_count': 0, 'total': 0,
             'all_complete': False, 'has_sent_campaign': None}
    if org is None:
        # Only superusers can reach the dashboard without an org; nothing to onboard.
        return empty
    # Dismissed orgs skip all predicate queries on every dashboard load.
    if org.onboarding_dismissed_at:
        return empty

    from tickets.models import SMSCampaign

    real_customers = Customer.objects.filter(organization=org).exclude(
        email__endswith='@placeholder.local'
    )
    # Campaign audience is gated on consent + a phone (models.py); mirror it here.
    has_eligible_audience = has_customers and real_customers.filter(
        sms_opt_in=True).exclude(phone='').exists()
    has_sent_campaign = SMSCampaign.objects.filter(
        organization=org, status=SMSCampaign.Status.SENT
    ).exists()
    profile_set = bool(org.photo or org.description or org.website)

    # SMS step is consent-gated: with no opted-in audience, the CTA leads to the
    # customer list (where consent status lives), never a compose/blast screen.
    if has_eligible_audience:
        sms_step = {
            'key': 'send_campaign',
            'label': 'Send your first SMS campaign',
            'description': 'Reach your opted-in customers with a text.',
            'url': reverse('tickets:sms_campaign_create'),
            'cta': 'Compose campaign',
            'complete': has_sent_campaign,
        }
    else:
        sms_step = {
            'key': 'send_campaign',
            'label': 'Send your first SMS campaign',
            'description': 'Imported contacts need marketing consent before you can text them '
                           '— map a consent column on import, or collect opt-ins.',
            'url': reverse('tickets:customer_list') + '?focus=consent',
            'cta': 'Review consent',
            'complete': has_sent_campaign,
        }

    steps = [
        {
            'key': 'set_profile',
            'label': 'Set up your organization profile',
            'description': 'Add a logo, description, and website so customers recognize you.',
            'url': reverse('tickets:org_profile'),
            'cta': 'Edit profile',
            'complete': profile_set,
        },
        {
            'key': 'import_data',
            'label': 'Import an event report',
            'description': 'Upload a sales report from a past event to see who your customers are.',
            # Straight to the external (CSV) flow — skip the type chooser, which
            # leads with Direct Ticketing and misreads as "sell tickets", not import.
            'url': reverse('tickets:event_create', args=[TICKETING_TYPE_EXTERNAL]),
            'cta': 'Import data',
            'complete': has_customers,
        },
        {
            'key': 'review_segments',
            'label': 'Review your customer segments',
            'description': 'See your VIPs, regulars, and at-risk customers.',
            'url': reverse('tickets:customer_segments'),
            'cta': 'View segments',
            'complete': has_customers,
        },
        sms_step,
    ]

    complete_count = sum(1 for step in steps if step['complete'])
    all_complete = complete_count == len(steps)
    return {
        'show': not all_complete,
        'steps': steps,
        'complete_count': complete_count,
        'total': len(steps),
        'all_complete': all_complete,
        # Surfaced so the direct-ticketing upsell can reuse it without re-querying.
        'has_sent_campaign': has_sent_campaign,
    }


@login_required
@require_org
@require_organizer
@require_http_methods(["POST"])
def dismiss_onboarding(request):
    """Hide the dashboard "Getting started" checklist for the current org."""
    org = get_organization(request)
    org.onboarding_dismissed_at = django_tz.now()
    org.save(update_fields=['onboarding_dismissed_at'])
    return redirect('tickets:home')


def _direct_ticketing_upsell(org, has_customers, has_sent_campaign=None):
    """Whether to show the value-gated "sell through Cue" upsell card (D4/4A).

    Shown only AFTER the org has seen value (imported customers or sent a
    campaign), and only while direct ticketing isn't set up and the card hasn't
    been dismissed. Kept quiet and dismissible — never a banner or modal.

    ``has_sent_campaign`` may be passed in (the checklist already computes it) to
    avoid a duplicate query; it's only looked up here when needed and unknown.
    """
    if org is None or org.directticketing_upsell_dismissed_at or org.stripe_onboarding_complete:
        return False
    if has_customers:
        return True
    if has_sent_campaign is None:
        from tickets.models import SMSCampaign
        has_sent_campaign = SMSCampaign.objects.filter(
            organization=org, status=SMSCampaign.Status.SENT
        ).exists()
    return has_sent_campaign


@login_required
@require_org
@require_organizer
@require_http_methods(["POST"])
def dismiss_directticketing_upsell(request):
    """Permanently hide the direct-ticketing upsell card for the current org."""
    org = get_organization(request)
    org.directticketing_upsell_dismissed_at = django_tz.now()
    org.save(update_fields=['directticketing_upsell_dismissed_at'])
    return redirect('tickets:home')


@login_required
@require_org
@require_organizer
def sample_import_csv(request):
    """Downloadable canonical sample CSV for first-time importers.

    Shows the columns a ticket-order export should have, including the optional
    SMS consent column that maps to Customer.sms_opt_in on import.
    """
    import csv as _csv
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="cue-sample-import.csv"'
    writer = _csv.writer(response)
    writer.writerow([
        'order_date', 'customer_name', 'customer_email', 'customer_phone',
        'ticket_type', 'total_amount', 'sms_opt_in',
    ])
    writer.writerow([
        '2025-06-01', 'Jordan Rivera', 'jordan@example.com', '+15555550101',
        'General Admission', '45.00', 'yes',
    ])
    writer.writerow([
        '2025-06-01', 'Sam Chen', 'sam@example.com', '+15555550102',
        'VIP', '90.00', 'no',
    ])
    return response


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
            if ev.ticketing_type == 'external':
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

    # Reuse the already-computed customer count for the onboarding predicates so
    # the checklist and the upsell don't re-query "does this org have customers"
    # or "has it sent a campaign".
    has_customers = total_customers > 0
    onboarding = _onboarding_state(org, has_customers)

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
        'onboarding': onboarding,
        'show_directticketing_upsell': _direct_ticketing_upsell(
            org, has_customers, onboarding.get('has_sent_campaign')
        ),
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
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': True})
    messages.success(request, 'Recommendation marked resolved.')
    return _ai_recommendation_redirect(request)


@login_required
@require_org
@require_organizer
@require_http_methods(["GET"])
def ai_recommendation_unconfirmed_matches(request, recommendation_id):
    """JSON: unconfirmed Meta Ads / Mailchimp / SlickText matches for a recommendation's event.

    Powers the "Review and confirm" modal on the Action Center and home dashboard.
    Mirrors the same filters as ``detect_unconfirmed_marketing_matches`` so the totals
    line up with the recommendation summary.
    """
    org = get_organization(request)
    recommendation = get_object_or_404(
        AIRecommendation.objects.filter(organization=org).select_related('event'),
        id=recommendation_id,
    )
    if (
        recommendation.kind != AIRecommendation.Kind.MARKETING_UNCONFIRMED
        or recommendation.event is None
    ):
        return JsonResponse({'ok': False, 'error': 'Not a marketing match recommendation.'}, status=404)

    event = recommendation.event

    meta_ads = list(
        EventExpense.objects.filter(
            event=event,
            deleted_at__isnull=True,
            confirmed_at__isnull=True,
            source='meta_ads',
        ).order_by('-amount', 'description')
    )
    mailchimp = list(
        EventEmailCampaign.objects.filter(
            event=event,
            deleted_at__isnull=True,
            confirmed_at__isnull=True,
        ).order_by('-send_time', 'campaign_title')
    )
    slicktext = list(
        EventSMSCampaign.objects.filter(
            event=event,
            deleted_at__isnull=True,
            confirmed_at__isnull=True,
        ).order_by('-send_time', 'name')
    )

    def ads_payload(e):
        meta = e.external_metadata or {}
        return {
            'id': str(e.id),
            'label': meta.get('campaign_name') or e.description or 'Meta Ads campaign',
            'sublabel': e.external_id or '',
            'amount': f'{(e.amount or Decimal("0.00")):.2f}',
            'effective_attributed_orders': e.effective_attributed_orders or 0,
            'effective_attributed_revenue': f'{(e.effective_attributed_revenue or Decimal("0.00")):.2f}',
            'manual_attributed_orders': e.manual_attributed_orders,
            'manual_attributed_revenue': f'{e.manual_attributed_revenue:.2f}' if e.manual_attributed_revenue is not None else '',
            'confirm_url': reverse('tickets:event_meta_ads_confirm', args=[event.id, e.id]),
            'remove_url': reverse('tickets:event_meta_ads_remove', args=[event.id, e.id]),
            'metrics_edit_url': reverse('tickets:event_meta_ads_metrics_edit', args=[event.id, e.id]),
        }

    def fmt_send_time(value):
        if not value:
            return ''
        return _format_meta_ads_datetime(value.isoformat()) or ''

    def email_payload(c):
        return {
            'id': str(c.id),
            'label': c.campaign_title or c.external_id or 'Mailchimp campaign',
            'sublabel': c.subject_line or '',
            'send_time': fmt_send_time(c.send_time),
            'effective_emails_sent': c.effective_emails_sent or 0,
            'effective_unique_opens': c.effective_unique_opens or 0,
            'effective_clicks': c.effective_clicks or 0,
            'effective_orders': c.effective_orders or 0,
            'effective_revenue': f'{(c.effective_revenue or Decimal("0.00")):.2f}',
            'manual_emails_sent': c.manual_emails_sent,
            'manual_unique_opens': c.manual_unique_opens,
            'manual_clicks': c.manual_clicks,
            'manual_orders': c.manual_orders,
            'manual_revenue': f'{c.manual_revenue:.2f}' if c.manual_revenue is not None else '',
            'confirm_url': reverse('tickets:event_mailchimp_confirm', args=[event.id, c.id]),
            'remove_url': reverse('tickets:event_mailchimp_remove', args=[event.id, c.id]),
            'metrics_edit_url': reverse('tickets:event_mailchimp_metrics_edit', args=[event.id, c.id]),
        }

    def sms_payload(c):
        return {
            'id': str(c.id),
            'label': c.name or c.external_id or 'SlickText broadcast',
            'sublabel': (c.message or '')[:120],
            'send_time': fmt_send_time(c.send_time),
            'effective_audience': c.effective_audience or 0,
            'effective_clicks': c.effective_clicks or 0,
            'effective_orders': c.effective_orders or 0,
            'effective_revenue': f'{(c.effective_revenue or Decimal("0.00")):.2f}',
            'manual_audience': c.manual_audience,
            'manual_clicks': c.manual_clicks,
            'manual_orders': c.manual_orders,
            'manual_revenue': f'{c.manual_revenue:.2f}' if c.manual_revenue is not None else '',
            'confirm_url': reverse('tickets:event_slicktext_confirm', args=[event.id, c.id]),
            'remove_url': reverse('tickets:event_slicktext_remove', args=[event.id, c.id]),
            'metrics_edit_url': reverse('tickets:event_slicktext_metrics_edit', args=[event.id, c.id]),
        }

    payload = {
        'ok': True,
        'event_id': str(event.id),
        'event_name': event.name,
        'meta_ads': [ads_payload(e) for e in meta_ads],
        'mailchimp': [email_payload(c) for c in mailchimp],
        'slicktext': [sms_payload(c) for c in slicktext],
        'confirm_all_urls': {
            'meta_ads': reverse('tickets:event_meta_ads_confirm_all', args=[event.id]),
            'mailchimp': reverse('tickets:event_mailchimp_confirm_all', args=[event.id]),
            'slicktext': reverse('tickets:event_slicktext_confirm_all', args=[event.id]),
        },
        'event_marketing_url': reverse('tickets:event_detail', args=[event.id]) + '#marketing',
    }
    payload['total'] = len(payload['meta_ads']) + len(payload['mailchimp']) + len(payload['slicktext'])
    return JsonResponse(payload)


def require_external_events(view):
    """Gate a view behind the org's external_events_enabled flag.

    External (CSV-imported) events are off by default; orgs are direct-ticketing
    only until this per-org flag is enabled. Mirrors require_loyalty_feature.
    """
    from functools import wraps

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        org = get_organization(request)
        if not org or not org.external_events_enabled:
            raise Http404('External events are not enabled for this organization.')
        return view(request, *args, **kwargs)
    return wrapped


@login_required
@require_org
@require_host
@require_external_events
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
@require_external_events
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
        with transaction.atomic():
            # Revoke loyalty points BEFORE the hard delete — reprocessing
            # re-creates orders with new UUIDs and would double-award.
            # Failures PROPAGATE so a failed revoke aborts the delete instead
            # of stranding points (eng review D4).
            revoke_points_for_orders(
                list(TicketOrder.objects.filter(uploaded_file=uploaded_file).values_list('id', flat=True)),
                description='CSV reprocess',
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
@require_host
def download_csv_file(request, file_id):
    """Download the original CSV that was uploaded for this import."""
    org = get_organization(request)
    uploaded_file = get_object_or_404(UploadedFile.objects.filter(organization=org), id=file_id)
    if not uploaded_file.csv_file:
        messages.error(request, "No stored file available to download.")
        return redirect('tickets:upload_results', file_id=uploaded_file.id)
    return FileResponse(
        uploaded_file.csv_file.open('rb'),
        as_attachment=True,
        filename=uploaded_file.filename,
        content_type='text/csv',
    )


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

            # Revoke loyalty points BEFORE the hard delete; failures PROPAGATE
            # (abort the whole delete) so points can never strand (D4).
            revoke_points_for_orders(
                list(orders.values_list('id', flat=True)),
                description='Upload deleted',
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
    market_context = _customer_market_filter_context(org, request.GET.get('market', ''))

    # Segment filter
    segment_filter = request.GET.get('segment', '').strip()

    # Tag filter — validate UUID to avoid ValueError on bad input
    tag_filter = request.GET.get('tag', '').strip()
    if tag_filter:
        try:
            _uuid.UUID(tag_filter)
        except ValueError:
            tag_filter = ''

    # Search
    search_query = request.GET.get('search', '')
    last_order_from = _customer_filter_date(request.GET.get('last_order_from', ''))
    last_order_to = _customer_filter_date(request.GET.get('last_order_to', ''))
    phone_filter = request.GET.get('phone_filter', '').strip()
    sms_filter = request.GET.get('sms_filter', '').strip()   # '1', '0', or ''
    min_ltv = request.GET.get('min_ltv', '').strip()
    max_ltv = request.GET.get('max_ltv', '').strip()
    min_orders = request.GET.get('min_orders', '').strip()
    max_orders = request.GET.get('max_orders', '').strip()
    sms_opt_in_filter = True if sms_filter == '1' else (False if sms_filter == '0' else None)
    has_active_filters = any([
        search_query,
        segment_filter,
        tag_filter,
        market_context['market_filter'],
        last_order_from,
        last_order_to,
        phone_filter,
        sms_filter,
        min_ltv,
        max_ltv,
        min_orders,
        max_orders,
    ])

    # Build the queryset via the shared filter helper (also used by SMS
    # recipient lists) so filtering logic lives in one place.
    customers = filter_customers(org, {
        'rfm_segment': segment_filter or None,
        'tag_id': tag_filter or None,
        'search': search_query or None,
        'market_id': market_context['market_filter'] or None,
        'last_order_after': last_order_from or None,
        'last_order_before': last_order_to or None,
        'phone': phone_filter or None,
        'sms_opt_in': sms_opt_in_filter,
        'min_ltv': min_ltv or None,
        'max_ltv': max_ltv or None,
    })

    customers = customers.annotate(order_count=Count('ticket_orders', distinct=True))
    if min_orders:
        try:
            customers = customers.filter(order_count__gte=int(min_orders))
        except ValueError:
            pass
    if max_orders:
        try:
            customers = customers.filter(order_count__lte=int(max_orders))
        except ValueError:
            pass

    # T6: when a market filter is active, annotate each customer row with
    # market-scoped net LTV and last-order date via isolated Subqueries.
    active_market = market_context['market_filter']
    if active_market:
        if active_market == NO_MARKET_VALUE:
            _mkt_q = {'event__market__isnull': True}
        else:
            _mkt_q = {'event__market_id': active_market}
        _base_order_filter = dict(
            customer=OuterRef('pk'),
            event__organization=org,
            is_in_person=False,
            refunded_at__isnull=True,
            **_mkt_q,
        )
        _mkt_revenue_sq = Subquery(
            TicketOrder.objects.filter(**_base_order_filter)
            .values('customer')
            .annotate(v=Sum(ExpressionWrapper(
                F('total_amount') - F('refunded_amount'),
                output_field=DecimalField(max_digits=10, decimal_places=2),
            )))
            .values('v')[:1],
            output_field=DecimalField(max_digits=10, decimal_places=2),
        )
        _mkt_fee_filter = {
            f'ticket_order__{k}': v for k, v in _base_order_filter.items()
            if k != 'customer'
        }
        _mkt_fee_filter['ticket_order__customer'] = OuterRef('pk')
        _mkt_fee_sq = Subquery(
            StripeCheckoutSession.objects.filter(**_mkt_fee_filter)
            .values('ticket_order__customer')
            .annotate(v=Sum('platform_fee_cents'))
            .values('v')[:1],
            output_field=IntegerField(),
        )
        _mkt_last_sq = Subquery(
            TicketOrder.objects.filter(
                customer=OuterRef('pk'),
                event__organization=org,
                **_mkt_q,
            ).order_by('-order_date').values('order_date')[:1],
        )
        customers = customers.annotate(
            market_ltv=ExpressionWrapper(
                Coalesce(_mkt_revenue_sq, Value(Decimal('0.00')))
                - Cast(Coalesce(_mkt_fee_sq, Value(0)), output_field=DecimalField(max_digits=10, decimal_places=2))
                  / Value(Decimal('100')),
                output_field=DecimalField(max_digits=10, decimal_places=2),
            ),
            market_last_order=_mkt_last_sq,
        )

    # Sorting
    default_sort = '-market_ltv' if active_market else '-lifetime_value'
    allowed_sorts = {
        'name', '-name',
        'email', '-email',
        'lifetime_value', '-lifetime_value',
        'last_order_date', '-last_order_date',
        'rfm_segment', '-rfm_segment',
        'first_tag_name', '-first_tag_name',
        'points_balance', '-points_balance',
    }
    if active_market:
        allowed_sorts |= {'market_ltv', '-market_ltv', 'market_last_order', '-market_last_order'}
    sort_by = request.GET.get('sort', default_sort)
    if sort_by not in allowed_sorts:
        sort_by = default_sort

    if sort_by in ('first_tag_name', '-first_tag_name'):
        # Isolated subquery so we don't join through the M2M and inflate other annotations.
        first_tag_subquery = CustomerTag.objects.filter(
            customers=OuterRef('pk')
        ).order_by('name').values('name')[:1]
        customers = customers.annotate(first_tag_name=Subquery(first_tag_subquery))
        tag_order = F('first_tag_name')
        customers = customers.order_by(
            tag_order.desc(nulls_last=True) if sort_by.startswith('-') else tag_order.asc(nulls_last=True)
        )
    else:
        customers = customers.order_by(sort_by)

    # prefetch_related must go AFTER the OR search chain to avoid Django dropping it
    customers = customers.prefetch_related('tags')

    # Pagination
    paginator = Paginator(customers, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Flag rows whose phone replied STOP (on the suppression list). Suppression
    # overrides sms_opt_in — these can't be texted until they text START — so the
    # list shows an explicit "Opted out" state instead of a misleading green check.
    # Scoped to the current page's phones so it's one small query, not per-row.
    if org.sms_marketing_enabled:
        from .sms import normalize_phone
        page_customers = list(page_obj)
        phones = {normalize_phone(c.phone) for c in page_customers if c.phone}
        suppressed = set()
        if phones:
            suppressed = set(
                PhoneSuppression.objects.filter(
                    Q(organization=org) | Q(organization__isnull=True),
                    phone__in=phones,
                ).values_list('phone', flat=True)
            )
        for c in page_customers:
            c.sms_suppressed = bool(c.phone) and normalize_phone(c.phone) in suppressed

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
    _fp = {k: v for k, v in {
        'search': search_query,
        'segment': segment_filter,
        'tag': tag_filter,
        'market': market_context['market_filter'],
        'last_order_from': last_order_from,
        'last_order_to': last_order_to,
        'phone_filter': phone_filter,
        'sms_filter': sms_filter,
        'min_ltv': min_ltv,
        'max_ltv': max_ltv,
        'min_orders': min_orders,
        'max_orders': max_orders,
    }.items() if v}
    filter_params_qs = urlencode(_fp)
    context = {
        'page_obj': page_obj,
        'search_query': search_query,
        'sort_by': sort_by,
        'segment_filter': segment_filter,
        'tag_filter': tag_filter,
        'market_filter': market_context['market_filter'],
        'last_order_from': last_order_from,
        'last_order_to': last_order_to,
        'phone_filter': phone_filter,
        'sms_filter': sms_filter,
        'min_ltv': min_ltv,
        'max_ltv': max_ltv,
        'min_orders': min_orders,
        'max_orders': max_orders,
        'filter_params_qs': filter_params_qs,
        'has_active_filters': has_active_filters,
        'segment_choices': segment_choices,
        'segment_badge_colors': SEGMENT_BADGE_COLORS,
        'current_segment_definition': current_segment_definition,
        'org_tags': org_tags,
        # Onboarding "Review consent" step lands here with ?focus=consent to
        # explain how SMS consent works and where the controls are.
        'show_consent_help': request.GET.get('focus') == 'consent',
    }
    context.update(market_context)
    return render(request, 'tickets/customer_list.html', context)


def _customer_filter_date(raw_date):
    parsed = parse_date((raw_date or '').strip())
    return parsed.isoformat() if parsed else ''


@login_required
@require_org
@require_host
def customer_ltv_by_market(request):
    """Display customer LTV metrics aggregated by assigned market."""
    org = get_organization(request)
    qs = (
        TicketOrder.objects.filter(event__organization=org)
        .values('event__market_id', 'event__market__name')
        .annotate(
            total_ltv=Sum('total_amount'),
            order_count=Count('id'),
            customer_count=Count('customer', distinct=True),
        )
    )
    sort_by = request.GET.get('sort', '-total_ltv')
    if sort_by == 'city':
        qs = qs.order_by('event__market__name')
    elif sort_by == '-city':
        qs = qs.order_by('-event__market__name')
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
        market_name = (row['event__market__name'] or '').strip() or NO_MARKET_LABEL
        customer_count = row['customer_count'] or 0
        total_ltv = row['total_ltv'] or Decimal('0.00')
        avg_ltv = (total_ltv / customer_count) if customer_count else Decimal('0.00')
        order_count = row['order_count'] or 0
        avg_orders = round(order_count / customer_count, 1) if customer_count else 0
        market_stats.append({
            'market_id': str(row['event__market_id']) if row['event__market_id'] else '',
            'market_name': market_name,
            'market_label': market_name,
            'city': market_name,
            'total_ltv': total_ltv,
            'order_count': order_count,
            'customer_count': customer_count,
            'avg_ltv': avg_ltv,
            'avg_orders': avg_orders,
        })

    chart_data = [
        {
            'market_id': row['market_id'],
            'market_name': row['market_name'],
            'market_label': row['market_label'],
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


def _segment_mode_display(org):
    """Return display copy for how this org currently assigns RFM segments."""
    if org.segment_mode == 'absolute':
        from .services.segmentation.segment_definitions import default_segment_bands

        defaults = default_segment_bands()
        bands = {**defaults, **(org.segment_bands or {})}
        return {
            'label': 'Custom rules',
            'summary': (
                'Customers are sorted with fixed cut-offs: active within {active} days, '
                'slipping away through {cooling} days, repeat at {few} orders, '
                'frequent at {many} orders, good spender at ${mid:g}, top spender at ${high:g}.'
            ).format(
                active=int(bands['recency_active_days']),
                cooling=int(bands['recency_cooling_days']),
                few=int(bands['freq_few']),
                many=int(bands['freq_many']),
                mid=float(bands['monetary_mid']),
                high=float(bands['monetary_high']),
            ),
        }
    return {
        'label': 'Automatic',
        'summary': (
            'Cue sorts customers automatically using relative RFM scores for recency, '
            'frequency, and total spend within your organization.'
        ),
    }


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


def _segment_group_case(field_path):
    """Case/When expression mapping blank/null segment fields to 'Dormant'."""
    return Case(
        When(**{field_path: ''}, then=Value('Dormant')),
        When(**{f'{field_path}__isnull': True}, then=Value('Dormant')),
        default=F(field_path),
        output_field=CharField(),
    )


def _annotate_net_revenue(qs):
    """Filter to non-refunded orders and annotate _fee_cents per order.

    Required before using _net_revenue_sum() in an aggregation.
    """
    fee_sq = Subquery(
        StripeCheckoutSession.objects.filter(
            ticket_order=OuterRef('pk')
        ).values('platform_fee_cents')[:1],
        output_field=IntegerField(),
    )
    return qs.filter(refunded_at__isnull=True).annotate(
        _fee_cents=Coalesce(fee_sq, Value(0)),
    )


def _net_revenue_sum(**kwargs):
    """Sum expression for net per-order revenue. Requires _annotate_net_revenue() first.

    Pass filter=Q(...) to apply conditional aggregation (e.g. is_in_person=False).
    """
    return Coalesce(
        Sum(
            ExpressionWrapper(
                F('total_amount') - F('refunded_amount')
                - Cast(F('_fee_cents'), output_field=DecimalField(max_digits=10, decimal_places=2))
                  / Value(Decimal('100')),
                output_field=DecimalField(max_digits=10, decimal_places=2),
            ),
            **kwargs,
        ),
        Value(Decimal('0.00')),
    )


def _customer_market_filter_context(org, raw_market):
    """Return normalized market-filter state for customer/segment pages.

    Short-circuits with empty context when the org has no markets yet,
    avoiding the has_no_market query and all downstream UI cost.
    """
    market_choices, has_no_market = market_filter_options(org)
    _empty = {
        'market_choices': [],
        'has_no_market': False,
        'market_filter': '',
        'selected_market_label': '',
        'no_market_value': NO_MARKET_VALUE,
    }
    if not market_choices:
        return _empty
    raw_market = (raw_market or '').strip()
    selected_market = ''
    selected_market_label = ''
    if raw_market == NO_MARKET_VALUE and has_no_market:
        selected_market = NO_MARKET_VALUE
        selected_market_label = NO_MARKET_LABEL
    elif raw_market:
        try:
            market_uuid = _uuid.UUID(raw_market)
        except (ValueError, TypeError):
            market_uuid = None
        if market_uuid:
            selected = next((m for m in market_choices if str(m.id) == str(market_uuid)), None)
            if selected:
                selected_market = str(selected.id)
                selected_market_label = selected.name
    return {
        'market_choices': market_choices,
        'has_no_market': has_no_market,
        'market_filter': selected_market,
        'selected_market_label': selected_market_label,
        'no_market_value': NO_MARKET_VALUE,
    }


def _apply_order_market_filter(qs, market_filter):
    if market_filter == NO_MARKET_VALUE:
        return qs.filter(event__market__isnull=True)
    if market_filter:
        return qs.filter(event__market_id=market_filter)
    return qs


def _normalized_customer_group_stats(org, field_name, ordered_labels, badge_colors, market_filter=''):
    value_expr = f'{field_name}'
    # T1: wrap market-filtered qs in pk__in subquery to de-dup the multi-valued
    # ticket_orders join before aggregating, so Avg('avg_days_between_orders') is
    # not weighted by order count.
    if market_filter:
        customer_qs = Customer.objects.filter(
            pk__in=filter_customers(org, {'market_id': market_filter}).values('pk')
        )
    else:
        customer_qs = Customer.objects.filter(
            organization=org
        ).exclude(email__endswith='@placeholder.local')
    customer_rows = (
        customer_qs
        .annotate(group_name=_segment_group_case(value_expr))  # T12
        .values('group_name')
        .annotate(
            count=Count('id'),
            total_ltv=Sum('lifetime_value'),
            avg_gap=Avg('avg_days_between_orders'),
        )
    )
    order_qs = TicketOrder.objects.filter(
        customer__organization=org,
        event__organization=org,
        is_in_person=False,
    ).exclude(customer__email__endswith='@placeholder.local')
    order_qs = _apply_order_market_filter(order_qs, market_filter)
    order_qs = _annotate_net_revenue(order_qs)  # T4: filters refunded, annotates _fee_cents
    order_rows = (
        order_qs
        .annotate(group_name=_segment_group_case(f'customer__{value_expr}'))  # T12
        .values('group_name')
        .annotate(
            total_orders=Count('id'),
            market_ltv=_net_revenue_sum(),  # T4: net revenue
        )
    )

    customer_map = {row['group_name'] or 'Dormant': row for row in customer_rows}
    order_map = {row['group_name'] or 'Dormant': row for row in order_rows}
    total_customers = sum(row['count'] for row in customer_map.values())
    stats = []
    for name in ordered_labels:
        row = customer_map.get(name, {'count': 0, 'total_ltv': Decimal('0'), 'avg_gap': None})
        count = row['count']
        order_row = order_map.get(name, {})
        total_ltv = (order_row.get('market_ltv') if market_filter else row.get('total_ltv')) or Decimal('0')
        total_orders = order_row.get('total_orders') or 0
        avg_gap = row.get('avg_gap')
        stats.append({
            'segment': name,
            'count': count,
            'pct': round((100.0 * count / total_customers), 1) if total_customers else 0,
            'avg_ltv': (total_ltv / count) if count else Decimal('0'),
            'avg_orders': round((total_orders / count), 1) if count else 0,
            'avg_gap': round(avg_gap, 1) if avg_gap is not None else None,
            'badge_color': badge_colors.get(name, 'secondary'),
            'description': SEGMENT_DESCRIPTIONS.get(name, ''),
        })
    return stats, total_customers


def _market_segment_breakdown(org, ordered_labels, badge_colors):
    # T7: base queryset uses ALL order types for customer counts (matching
    # filter_customers membership). Revenue stats use is_in_person=False only.
    # T4: revenue is net (excluding refunds and Stripe fees) via conditional Sum.
    base_qs = (
        TicketOrder.objects.filter(
            customer__organization=org,
            event__organization=org,
        )
        .exclude(customer__email__endswith='@placeholder.local')
    )
    # Annotate _fee_cents per order (0 when no Stripe session)
    fee_sq = Subquery(
        StripeCheckoutSession.objects.filter(
            ticket_order=OuterRef('pk')
        ).values('platform_fee_cents')[:1],
        output_field=IntegerField(),
    )
    base_qs = base_qs.annotate(
        group_name=_segment_group_case('customer__rfm_segment'),  # T12
        _fee_cents=Coalesce(fee_sq, Value(0)),
    )
    order_rows = (
        base_qs
        .values('event__market_id', 'event__market__name', 'group_name')
        .annotate(
            # T7: all orders for customer membership count
            count=Count('customer', distinct=True),
            # T4: net revenue — is_in_person=False, non-refunded orders only
            total_net_revenue=_net_revenue_sum(
                filter=Q(is_in_person=False, refunded_at__isnull=True),
            ),
            total_orders=Count('id', filter=Q(is_in_person=False)),
        )
    )
    market_map = {}
    for row in order_rows:
        market_id = str(row['event__market_id']) if row['event__market_id'] else NO_MARKET_VALUE
        market_name = (row['event__market__name'] or '').strip() or NO_MARKET_LABEL
        market = market_map.setdefault(market_id, {
            'market_id': market_id,
            'market_name': market_name,
            'segments': {},
            'total_customers': 0,
        })
        segment = row['group_name'] or 'Dormant'
        count = row['count'] or 0
        market['segments'][segment] = {
            'segment': segment,
            'count': count,
            'total_ltv': row['total_net_revenue'] or Decimal('0'),
            'total_orders': row['total_orders'] or 0,
            'badge_color': badge_colors.get(segment, 'secondary'),
        }
        market['total_customers'] += count

    rows = []
    for market in market_map.values():
        segments = []
        for name in ordered_labels:
            seg = market['segments'].get(name)
            if not seg:
                continue
            count = seg['count']
            segments.append({
                **seg,
                'pct': round((100.0 * count / market['total_customers']), 1) if market['total_customers'] else 0,
                'avg_ltv': (seg['total_ltv'] / count) if count else Decimal('0'),
                'avg_orders': round((seg['total_orders'] / count), 1) if count else 0,
            })
        if segments:
            market['segments'] = segments
            rows.append(market)
    return sorted(rows, key=lambda row: (row['market_name'] == NO_MARKET_LABEL, row['market_name'].lower()))


@login_required
@require_org
@require_host
def analytics_overview(request):
    """Hub for organizer analytics destinations."""
    get_organization(request)
    return render(request, 'tickets/analytics_overview.html')


def _marketing_cache_key(org_id, window):
    # Delegates to the shared helper so the Marketing overview, AI analyze, and
    # the SMS Campaigns page all key (and invalidate) against one definition.
    from .services.marketing import marketing_cache_key
    return marketing_cache_key(org_id, window)


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
    allowed_tabs = {'overview', 'email', 'ads'}
    active_tab = request.GET.get('tab', 'overview').lower()
    if active_tab not in allowed_tabs:
        active_tab = 'overview'

    metrics = get_cached_marketing_metrics(org, window_days, window_key)

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

    # Public subscribe link the organizer shares (Linktree, flyers, socials) to
    # grow their SMS audience. Built absolute so copy/QR work off-dashboard.
    import base64
    from .utils import generate_qr_png_bytes
    subscribe_url = request.build_absolute_uri(reverse('tickets:subscribe', args=[org.slug]))
    _qr_png = generate_qr_png_bytes(subscribe_url)
    subscribe_qr = (
        'data:image/png;base64,' + base64.b64encode(_qr_png).decode() if _qr_png else ''
    )

    context = {
        'metrics': metrics,
        'recommendations': recommendations,
        'window_choices': MARKETING_WINDOW_CHOICES,
        'window_key': window_key,
        'window_label': window_label,
        'active_tab': active_tab,
        'trend_chart_json': json.dumps(trend_chart),
        'engagement_chart_json': json.dumps(engagement_chart),
        'org_sms_marketing_enabled': org.sms_marketing_enabled,
        'subscribe_url': subscribe_url,
        'subscribe_qr': subscribe_qr,
        'marketing_section': 'overview',
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
    """Minimal analytics page for RFM segment distribution."""
    org = get_organization(request)
    segment_order = list(SEGMENT_BADGE_COLORS.keys())
    market_context = _customer_market_filter_context(org, request.GET.get('market', ''))

    segment_stats, total_customers = _normalized_customer_group_stats(
        org,
        'rfm_segment',
        segment_order,
        SEGMENT_BADGE_COLORS,
        market_context['market_filter'],
    )
    segment_mode_display = _segment_mode_display(org)
    # T5: only compute the breakdown when the org has markets; zero-market orgs
    # would see a redundant "No market" table that duplicates the main table.
    breakdown = (
        _market_segment_breakdown(org, segment_order, SEGMENT_BADGE_COLORS)
        if market_context['market_choices']
        else []
    )
    context = {
        'segment_stats': segment_stats,
        'total_customers': total_customers,
        'market_segment_breakdown': breakdown,
        'rfm_recalc_in_progress': org.rfm_recalc_in_progress,
        'segment_mode_label': segment_mode_display['label'],
        'segment_mode_summary': segment_mode_display['summary'],
    }
    context.update(market_context)
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

    from tickets.services.tagging import tag_customers
    customers = Customer.objects.filter(organization=org, id__in=customer_ids)
    tag, tagged_count = tag_customers(org, customers, tag_id=tag_id)
    if tag is None:
        messages.error(request, 'Select a valid tag.')
    else:
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


def _segment_health_backtest_rows(bt):
    """Shape a backtest result (per-segment table) for the template, or None."""
    if not bt or bt.get('status') != 'ok':
        return None
    rows = sorted(
        bt['per_segment'].items(), key=lambda kv: -kv[1]['avg_future_revenue']
    )
    return [
        {
            'segment': name,
            'badge_color': SEGMENT_BADGE_COLORS.get(name, 'secondary'),
            'n': s['n'],
            'repeat_rate': round(s['repeat_rate'] * 100, 1),
            'avg_future_revenue': s['avg_future_revenue'],
        }
        for name, s in rows
    ]


def _align_backtest_rows(proposed_rows, current_rows, order):
    """Align two backtest tables to the same segments, same order, for easy compare.

    Returns (proposed, current) covering the union of segments present in either,
    ordered by `order`, zero-filling any segment absent from one side.
    """
    proposed_rows = proposed_rows or []
    current_rows = current_rows or []
    by_p = {r['segment']: r for r in proposed_rows}
    by_c = {r['segment']: r for r in current_rows}
    present = set(by_p) | set(by_c)
    names = [s for s in order if s in present]
    names += [s for s in present if s not in order]  # defensive: unranked names last

    def zero(name):
        return {
            'segment': name, 'badge_color': SEGMENT_BADGE_COLORS.get(name, 'secondary'),
            'n': 0, 'repeat_rate': 0.0, 'avg_future_revenue': 0.0,
        }

    return (
        [by_p.get(n) or zero(n) for n in names],
        [by_c.get(n) or zero(n) for n in names],
    )


# ---------------------------------------------------------------------------
# Loyalty programs
# ---------------------------------------------------------------------------

def require_loyalty_feature(view):
    """Gate a view behind the org's loyalty_feature_enabled flag (pilot rollout).

    Mirrors require_sms_feature in sms_views.py: per-org flag on Organization
    (the global FeatureFlagSettings singleton cannot scope to orgs).
    """
    from functools import wraps

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        org = get_organization(request)
        if not org or not org.loyalty_feature_enabled:
            raise Http404('Loyalty programs are not enabled for this organization.')
        return view(request, *args, **kwargs)
    return wrapped


def _active_loyalty_programs(org):
    """Org-scoped, non-deleted programs (AuditBaseModel hides soft-deleted rows manually)."""
    return LoyaltyProgram.objects.filter(organization=org, deleted_at__isnull=True)


@login_required
@require_org
@require_host
@require_loyalty_feature
def loyalty_program_list(request):
    """List the org's loyalty programs with member counts."""
    org = get_organization(request)
    programs = list(
        _active_loyalty_programs(org)
        .annotate(tier_count=Count('tiers', distinct=True))
        .order_by('-is_active', '-created_at')
    )
    # Member count per program = customers whose tier belongs to that program.
    member_counts = dict(
        Customer.objects.filter(
            organization=org, loyalty_tier__program__in=programs,
        )
        .values_list('loyalty_tier__program')
        .annotate(c=Count('id'))
    )
    for program in programs:
        program.member_count = member_counts.get(program.id, 0)
    return render(request, 'tickets/loyalty/program_list.html', {'programs': programs})


def _clear_loyalty_members(program):
    """Null out the tier of every customer assigned to this program's tiers."""
    Customer.objects.filter(loyalty_tier__program=program).update(
        loyalty_tier=None, loyalty_tier_updated_at=django_tz.now(),
    )


def _save_loyalty_program(request, program):
    """Shared create/edit handler. Returns (form, formset, saved_program_or_None)."""
    if request.method == 'POST':
        form = LoyaltyProgramForm(request.POST, instance=program)
        formset = LoyaltyTierFormSet(request.POST, instance=program)
        if form.is_valid() and formset.is_valid():
            # Cross-validation: a points-based tier rule under a program with
            # points disabled would be permanently unreachable.
            if not form.cleaned_data.get('points_enabled'):
                uses_points_rule = any(
                    f.cleaned_data and not f.cleaned_data.get('DELETE')
                    and f.cleaned_data.get('min_lifetime_points') is not None
                    for f in formset.forms if hasattr(f, 'cleaned_data')
                )
                if uses_points_rule:
                    formset._non_form_errors.append(
                        'A tier uses a minimum-points rule, but points are not '
                        'enabled on this program. Enable points or remove the rule.'
                    )
                    return form, formset, None
            with transaction.atomic():
                saved = form.save(commit=False)
                saved.organization = program.organization
                if saved.created_by_id is None:
                    saved.created_by = request.user
                saved.updated_by = request.user
                if saved.is_active:
                    # Deactivate other programs BEFORE saving this one active,
                    # so the one-active-per-org DB constraint never trips, and
                    # clear their now-orphaned member assignments.
                    others = LoyaltyProgram.objects.filter(
                        organization=saved.organization, is_active=True,
                    ).exclude(pk=saved.pk)
                    for other in others:
                        _clear_loyalty_members(other)
                    others.update(is_active=False)
                else:
                    # Program saved inactive: it should hold no members.
                    if saved.pk:
                        _clear_loyalty_members(saved)
                saved.save()
                formset.instance = saved
                formset.save()
            # Only an active program drives assignment; inactive ones are gated
            # out by the task anyway, so don't bother enqueuing.
            if saved.is_active:
                wants_backfill = (
                    saved.points_enabled and form.cleaned_data.get('backfill_past_orders')
                )
                if wants_backfill:
                    # Backfill chains the recalc itself — skipping the immediate
                    # recalc avoids assigning tiers against pre-backfill zeros.
                    from .tasks import backfill_loyalty_points_task
                    backfill_loyalty_points_task.delay(str(saved.id))
                else:
                    from .tasks import recalculate_loyalty_tiers_task
                    recalculate_loyalty_tiers_task.delay(str(saved.id))
            return form, formset, saved
    else:
        form = LoyaltyProgramForm(instance=program)
        formset = LoyaltyTierFormSet(instance=program)
    return form, formset, None


@login_required
@require_org
@require_host
@require_loyalty_feature
@require_http_methods(["GET", "POST"])
def loyalty_program_create(request):
    """Builder: create a program and its tiers on one page."""
    org = get_organization(request)
    program = LoyaltyProgram(organization=org)
    form, formset, saved = _save_loyalty_program(request, program)
    if saved is not None:
        messages.success(request, f'Loyalty program "{saved.name}" created. Members are being assigned.')
        return redirect('tickets:loyalty_program_detail', program_id=saved.id)
    return render(request, 'tickets/loyalty/program_form.html', {
        'form': form, 'formset': formset, 'editing': False,
    })


@login_required
@require_org
@require_host
@require_loyalty_feature
@require_http_methods(["GET", "POST"])
def loyalty_program_edit(request, program_id):
    """Builder: edit an existing program and its tiers."""
    org = get_organization(request)
    program = get_object_or_404(_active_loyalty_programs(org), id=program_id)
    form, formset, saved = _save_loyalty_program(request, program)
    if saved is not None:
        messages.success(request, 'Loyalty program updated. Members are being reassigned.')
        return redirect('tickets:loyalty_program_detail', program_id=saved.id)
    return render(request, 'tickets/loyalty/program_form.html', {
        'form': form, 'formset': formset, 'editing': True, 'program': program,
    })


@login_required
@require_org
@require_host
@require_loyalty_feature
def loyalty_program_detail(request, program_id):
    """Dashboard: tier distribution, member counts, perks, recalc controls."""
    org = get_organization(request)
    program = get_object_or_404(_active_loyalty_programs(org), id=program_id)
    stats = LoyaltyProgramStats(program).calculate()
    points_stats = None
    if program.points_enabled:
        # One aggregate pass over org customers (CLAUDE.md: combine aggregations).
        points_stats = Customer.objects.filter(organization=org).exclude(
            email__endswith='@placeholder.local'
        ).aggregate(
            outstanding=Coalesce(Sum('points_balance'), 0),
            issued=Coalesce(Sum('lifetime_points'), 0),
            members_with_points=Count('id', filter=Q(points_balance__gt=0)),
        )
    return render(request, 'tickets/loyalty/program_detail.html', {
        'program': program,
        'stats': stats,
        'points_stats': points_stats,
        'points_backfilled_at': org.loyalty_points_backfilled_at,
    })


@login_required
@require_org
@require_host
@require_loyalty_feature
def loyalty_tier_members(request, program_id, tier_id):
    """Paginated member list for a single tier."""
    org = get_organization(request)
    program = get_object_or_404(_active_loyalty_programs(org), id=program_id)
    tier = get_object_or_404(LoyaltyTier.objects.filter(program=program), id=tier_id)
    members = Customer.objects.filter(organization=org, loyalty_tier=tier)

    # Search by name or email (kept inline rather than via filter_customers, which
    # would drop placeholder-email CSV members from the tier list).
    search_query = request.GET.get('search', '').strip()
    if search_query:
        members = members.filter(
            Q(name__icontains=search_query) | Q(email__icontains=search_query)
        )

    # Market filter — matches a member's most-frequented market (see annotation below).
    market_filter = request.GET.get('market', '').strip()
    market_choices = list(
        Market.objects.filter(organization=org).order_by('name').values_list('name', flat=True)
    )

    # Sorting — validate against an allowlist, default to highest lifetime value.
    allowed_sorts = {
        'name', '-name', 'email', '-email',
        'lifetime_value', '-lifetime_value',
        'last_order_date', '-last_order_date',
    }
    sort_by = request.GET.get('sort', '-lifetime_value')
    if sort_by not in allowed_sorts:
        sort_by = '-lifetime_value'

    if sort_by in ('last_order_date', '-last_order_date'):
        last = F('last_order_date')
        ordering = [
            last.desc(nulls_last=True) if sort_by.startswith('-') else last.asc(nulls_last=True),
            'name',
        ]
    elif sort_by in ('lifetime_value', '-lifetime_value'):
        ordering = [sort_by, 'name']  # preserve the name tiebreak this page had
    else:
        ordering = [sort_by]
    # Annotate each member with their most-frequented market — the market where
    # they've placed the most orders — as a per-row subquery so the same value
    # drives both the column display and the market filter. Customer has no direct
    # market field; markets live on the Event a customer's orders belong to. Ties
    # are broken by market name for a deterministic winner.
    top_market_sq = (
        TicketOrder.objects.filter(
            customer=OuterRef('pk'),
            event__market__isnull=False,
        )
        .values('event__market__name')
        .annotate(order_count=Count('id'))
        .order_by('-order_count', 'event__market__name')
        .values('event__market__name')[:1]
    )
    members = members.annotate(top_market=Subquery(top_market_sq))
    if market_filter:
        members = members.filter(top_market=market_filter)

    members = members.order_by(*ordering)

    paginator = Paginator(members, 50)
    page_obj = paginator.get_page(request.GET.get('page'))

    return render(request, 'tickets/loyalty/tier_members.html', {
        'program': program,
        'tier': tier,
        'page_obj': page_obj,
        'search_query': search_query,
        'sort_by': sort_by,
        'market_filter': market_filter,
        'market_choices': market_choices,
    })


@login_required
@require_org
@require_host
@require_loyalty_feature
@require_http_methods(["POST"])
def loyalty_recalculate(request, program_id):
    """Enqueue tier reassignment for a program; redirect with message."""
    org = get_organization(request)
    program = get_object_or_404(_active_loyalty_programs(org), id=program_id)
    if not program.is_active:
        messages.warning(request, 'Activate this program before recalculating its members.')
    elif program.recalc_in_progress:
        messages.info(request, 'Recalculation already in progress.')
    else:
        from .tasks import recalculate_loyalty_tiers_task
        recalculate_loyalty_tiers_task.delay(str(program.id))
        messages.success(request, 'Member recalculation started. Results will appear shortly.')
    return redirect('tickets:loyalty_program_detail', program_id=program.id)


@login_required
@require_org
@require_host
@require_loyalty_feature
@require_http_methods(["POST"])
def loyalty_recompute_points(request, program_id):
    """Wipe + rebuild the org's points ledger at the CURRENT rate (reset + re-backfill).

    Irreversible and org-wide, so it's gated by a server-validated typed
    confirmation (confirm_name == program.name) on top of the client type-to-
    confirm. The actual reset + re-award + reconciliation run in the backfill
    task under its claim lock.
    """
    org = get_organization(request)
    program = get_object_or_404(_active_loyalty_programs(org), id=program_id)
    if not program.points_enabled:
        messages.warning(request, 'Enable points on this program before recomputing balances.')
    elif not program.is_active:
        messages.warning(request, 'Activate this program before recomputing its points.')
    elif request.POST.get('confirm_name', '').strip() != program.name:
        messages.warning(request, 'Type the program name exactly to confirm the recompute.')
    elif program.recalc_in_progress:
        messages.info(request, 'A recalculation or recompute is already in progress.')
    else:
        from .tasks import backfill_loyalty_points_task
        backfill_loyalty_points_task.delay(str(program.id), reset_first=True)
        messages.success(
            request,
            'Recomputing all balances at the current rate — this rebuilds points history for every member.',
        )
    return redirect('tickets:loyalty_program_detail', program_id=program.id)


@login_required
@require_org
@require_host
@require_loyalty_feature
@require_http_methods(["POST"])
def loyalty_program_delete(request, program_id):
    """Soft-delete a program and clear its members' tier assignment."""
    org = get_organization(request)
    program = get_object_or_404(_active_loyalty_programs(org), id=program_id)
    with transaction.atomic():
        _clear_loyalty_members(program)
        # Drop the active flag so a future program can be the org's sole active
        # one without tripping the one-active-per-org constraint.
        if program.is_active:
            program.is_active = False
            program.save(update_fields=['is_active'])
        program.delete()  # soft delete (AuditBaseModel)
    messages.success(request, f'Loyalty program "{program.name}" deleted.')
    return redirect('tickets:loyalty_program_list')


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

    # Build market aggregation from already-filtered events
    markets = {}
    for e in chart_events:
        market_label = e.get('market_label') or NO_MARKET_LABEL
        market_id = e.get('market_id') or ''
        m = markets.setdefault(market_label, {
            'market_id': market_id,
            'market_name': market_label,
            'market_label': market_label,
            'city': market_label,
            'total': 0,
            'new_count': 0,
            'returning_count': 0,
        })
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
def audience_analytics(request):
    """Analytics page: total customer count over time, filterable by market."""
    org = get_organization(request)
    markets, has_no_market = market_filter_options(org)

    selected = request.GET.get('market', '')
    market_id, no_market = None, False
    if selected == 'none' and has_no_market:
        no_market = True
    elif selected and any(str(m.id) == selected for m in markets):
        market_id = selected
    else:
        selected = ''  # ignore stale/invalid ids, fall back to all markets

    # Default to the all-time view so the full growth story shows on first load.
    if 'window' in request.GET:
        start_date, end_date, active_window = _parse_window(request)
    else:
        start_date, end_date, active_window = None, None, 'all'

    from tickets.services.audience_growth import AudienceGrowthCalculator
    result = AudienceGrowthCalculator(
        org, market_id=market_id, no_market=no_market,
        start_date=start_date, end_date=end_date,
    ).calculate()

    return render(request, 'tickets/audience_analytics.html', {
        'series_json': json.dumps(result['series'], default=str),
        'summary': result['summary'],
        'has_data': bool(result['series']),
        'markets': markets,
        'has_no_market': has_no_market,
        'selected_market': selected,
        'active_window': active_window,
        'window_start': start_date or '',
        'window_end': end_date or '',
        'window_choices': WINDOW_CHOICES,
    })


@login_required
@require_org
@require_host
def market_trends(request):
    """Analytics page: per-market turnout trend, decline diagnosis, and next-step CTA."""
    org = get_organization(request)
    period = request.GET.get('period', 'quarter')
    if period not in ('month', 'quarter'):
        period = 'quarter'
    metric = request.GET.get('metric', 'revenue')
    if metric not in ('revenue', 'tickets', 'profitability', 'nps'):
        metric = 'revenue'
    window = request.GET.get('window', '2y')
    if window not in ('1y', '2y', '3y', 'all'):
        window = '2y'

    from tickets.services.market_trends import MarketTrendCalculator
    result = MarketTrendCalculator(org, period=period, metric=metric, window=window).calculate()
    markets = result['markets']

    context = {
        'markets': markets,
        'markets_json': json.dumps(markets, default=str),
        'summary': result['summary'],
        'period': period,
        'metric': metric,
        'window': window,
        'change_unit': 'pts' if metric == 'nps' else '%',
        'sms_marketing_enabled': org.sms_marketing_enabled,
    }
    # AJAX selector switches request only the dynamic region so the page (and the
    # currently selected market) is preserved without a full reload.
    if request.GET.get('fragment'):
        return render(request, 'tickets/_market_trends_content.html', context)
    return render(request, 'tickets/market_trends.html', context)


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
    customer = get_object_or_404(
        Customer.objects.filter(organization=org).select_related('loyalty_tier', 'loyalty_tier__program'),
        id=customer_id,
    )

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
        first_order_date=Min('order_date'),
        last_order_date=Max('order_date'),
    )
    total_orders = order_stats['total_orders']
    avg_order_value = order_stats['avg_order_value']
    first_order_date = order_stats['first_order_date']
    last_order_date = order_stats['last_order_date']
    total_tickets = Ticket.objects.filter(ticket_order__customer=customer).count()

    # Orders feed the merged timeline. annotate net_amount so the template shows
    # post-fee totals; select_related event__venue so the Venue label doesn't
    # trigger an N+1. Paginated further down as part of the merged timeline list.
    orders = customer.ticket_orders.select_related('event', 'event__venue').annotate(
        tickets_count=Count('tickets'),
        net_amount=_net_amount,
    )

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

    # Defensive multi-tenancy guard: only surface a loyalty tier that belongs to
    # this org's own program. A foreign tier (from a bad admin edit/script) must
    # never leak another org's program name/perks onto this page.
    loyalty_tier = customer.loyalty_tier
    if loyalty_tier and loyalty_tier.program.organization_id != org.id:
        loyalty_tier = None

    # Points: surface the balance when the org's active program awards points.
    loyalty_points_enabled = _active_loyalty_programs(org).filter(
        is_active=True, points_enabled=True
    ).exists()

    # Marketing (native SMS) activity — one row per message sent to this customer.
    # Org-scoped already since `customer` is org-scoped. select_related avoids an
    # N+1 on campaign.name in the template. SMS has no "opened" event, so the
    # closest engagement signal is the tracked link click (first_clicked_at).
    # Only surfaced (in the header + timeline) when the org has SMS enabled.
    sms_marketing_enabled = org.sms_marketing_enabled
    sms_messages = (
        customer.sms_message_recipients
        .select_related('campaign')
    )
    # Single-pass summary counts for the timeline header's stat rail.
    sms_stats = customer.sms_message_recipients.aggregate(
        total=Count('id'),
        delivered=Count('id', filter=Q(status='delivered')),
        failed=Count('id', filter=Q(status__in=['failed', 'undelivered'])),
        clicked=Count('id', filter=Q(first_clicked_at__isnull=False)),
    )
    # Is this number on the SMS suppression list (replied STOP)? Suppression overrides
    # sms_opt_in — an organizer can't re-consent on their behalf, only the recipient
    # texting START can — so the consent badge must reflect it.
    from .sms import normalize_phone
    sms_suppressed = bool(customer.phone) and PhoneSuppression.is_suppressed(
        normalize_phone(customer.phone), org
    )

    # Survey responses this customer submitted — one timeline entry each.
    survey_responses = customer.survey_responses.select_related('event')

    # Loyalty tier transitions — only when the org runs the loyalty feature.
    loyalty_feature_enabled = org.loyalty_feature_enabled
    tier_transitions = (
        customer.tier_transitions.select_related('from_tier', 'to_tier')
        if loyalty_feature_enabled else []
    )

    # ---- Merge every interaction into one reverse-chronological timeline. ----
    # A single customer's interaction volume is small, so we assemble uniform
    # {kind, ts, obj} items in Python and paginate the combined list (Django's
    # Paginator accepts a plain list) rather than a DB-level UNION. All ts values
    # are timezone-aware datetimes, so cross-type ordering is correct.
    # Each item carries a `url` deep-linking to the underlying record so the
    # timeline entry title is clickable (order/survey -> event, sms -> campaign,
    # tier -> loyalty program). Related objects are already select_related, so
    # building these URLs adds no extra queries.
    timeline_items = []
    for order in orders:
        timeline_items.append({
            'kind': 'order', 'ts': order.order_date or order.created_at, 'obj': order,
            'url': reverse('tickets:event_detail', args=[order.event_id]),
        })
    if sms_marketing_enabled:
        for msg in sms_messages:
            timeline_items.append({
                'kind': 'sms', 'ts': msg.sent_at or msg.created_at, 'obj': msg,
                'url': reverse('tickets:sms_campaign_detail', args=[msg.campaign_id]),
            })
    for response in survey_responses:
        timeline_items.append({
            'kind': 'survey', 'ts': response.submitted_at, 'obj': response,
            'url': reverse('tickets:event_detail', args=[response.event_id]),
        })
    for transition in tier_transitions:
        tier = transition.to_tier or transition.from_tier
        timeline_items.append({
            'kind': 'tier', 'ts': transition.changed_at, 'obj': transition,
            'url': (reverse('tickets:loyalty_program_detail', args=[tier.program_id])
                    if tier else None),
        })
    timeline_items.sort(key=lambda i: i['ts'], reverse=True)
    paginator = Paginator(timeline_items, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    context = {
        'customer': customer,
        'loyalty_tier': loyalty_tier,
        'loyalty_points_enabled': loyalty_points_enabled,
        'total_orders': total_orders,
        'total_tickets': total_tickets,
        'avg_order_value': avg_order_value,
        'first_order_date': first_order_date,
        'last_order_date': last_order_date,
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
        'sms_stats': sms_stats,
        'sms_suppressed': sms_suppressed,
        'sms_marketing_enabled': sms_marketing_enabled,
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
            if ev.ticketing_type == 'external':
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
                'tt_id': str(tt.id),
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

    # Sales over time — for the chart; cached here so it's not re-run on every page load.
    # Tickets and revenue are aggregated independently to avoid join inflation of revenue
    # when grouping through the tickets relation.
    revenue_by_date = {
        row['date']: row['revenue']
        for row in (
            event.ticket_orders
            .annotate(date=TruncDate('order_date'))
            .values('date')
            .annotate(revenue=Sum('total_amount'))
        )
    }
    tickets_by_date = (
        Ticket.objects
        .filter(ticket_order__event=event)
        .annotate(date=TruncDate('ticket_order__order_date'))
        .values('date')
        .annotate(count=Count('id'))
        .order_by('date')
    )
    sales_over_time = [
        {
            'date': row['date'],
            'count': row['count'],
            'revenue': revenue_by_date.get(row['date'], 0),
        }
        for row in tickets_by_date
    ]
    # Backfill days with no sales so the chart's x-axis is continuous from the
    # first to the last day of activity (the ORM aggregation above omits empty days).
    if sales_over_time:
        by_date = {row['date']: row for row in sales_over_time}
        start_date = sales_over_time[0]['date']
        end_date = sales_over_time[-1]['date']
        filled = []
        current = start_date
        while current <= end_date:
            filled.append(
                by_date.get(current, {'date': current, 'count': 0, 'revenue': 0})
            )
            current += timedelta(days=1)
        sales_over_time = filled
    page_views_over_time = list(
        EventDailyPageView.objects.filter(event=event)
        .order_by('date')
        .values('date', 'view_count')
    )

    # Survey results — internal (SurveyResponse/SurveyAnswer)
    survey_invitations_count = SurveyInvitation.objects.filter(event=event).count()
    survey_responses_count = SurveyResponse.objects.filter(event=event).count()
    # Earliest pending scheduled send (None if nothing is scheduled / all sent).
    survey_scheduled_send_at = SurveyInvitation.objects.filter(
        event=event, sent_at__isnull=True, scheduled_send_at__isnull=False,
    ).aggregate(earliest=Min('scheduled_send_at'))['earliest']

    star_avg = None
    int_nps_total = int_promoters = int_passives = int_detractors = 0
    internal_comments = []
    choice_breakdowns = []

    if survey_responses_count > 0:
        star_avg = SurveyAnswer.objects.filter(
            response__event=event, star_rating__isnull=False
        ).aggregate(avg=Avg('star_rating'))['avg']

        # Single aggregate instead of 4 separate .count() calls
        nps_agg = SurveyAnswer.objects.filter(
            response__event=event, nps_score__isnull=False
        ).aggregate(
            total=Count('id'),
            promoters=Count('id', filter=Q(nps_score__gte=9)),
            passives=Count('id', filter=Q(nps_score__gte=7, nps_score__lte=8)),
            detractors=Count('id', filter=Q(nps_score__lte=6)),
        )
        int_nps_total = nps_agg['total']
        int_promoters = nps_agg['promoters']
        int_passives = nps_agg['passives']
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

        # Choice-question tallies — one annotated query over the option table,
        # grouped into per-question breakdowns (no N+1).
        choice_q_ids = list(
            SurveyAnswer.objects.filter(
                response__event=event,
                question__question_type__in=SurveyQuestion.CHOICE_TYPES,
            ).values_list('question_id', flat=True).distinct()
        )
        if choice_q_ids:
            option_rows = (
                SurveyQuestionOption.objects.filter(question_id__in=choice_q_ids)
                .annotate(n=Count('answers', filter=Q(answers__response__event=event)))
                .order_by('question__position', 'position')
                .values('question_id', 'question__question_text', 'label', 'n')
            )
            grouped = {}
            for row in option_rows:
                bucket = grouped.setdefault(
                    row['question_id'],
                    {'question': row['question__question_text'], 'options': []},
                )
                bucket['options'].append({'label': row['label'], 'count': row['n']})
            choice_breakdowns = list(grouped.values())

    # Survey results — external (ExternalSurveyResponse from CSV uploads)
    ext_qs = ExternalSurveyResponse.objects.filter(event=event)
    ext_count = ext_qs.count()
    ext_nps_total = ext_promoters = ext_passives = ext_detractors = 0
    ext_comments = []
    ext_rating_breakdown = []
    ext_structured = {}

    if ext_count > 0:
        # Single aggregate instead of 4 separate .count() calls
        nps_agg = ext_qs.filter(nps_score__isnull=False).aggregate(
            total=Count('id'),
            promoters=Count('id', filter=Q(nps_score__gte=9)),
            passives=Count('id', filter=Q(nps_score__gte=7, nps_score__lte=8)),
            detractors=Count('id', filter=Q(nps_score__lte=6)),
        )
        ext_nps_total = nps_agg['total']
        ext_promoters = nps_agg['promoters']
        ext_passives = nps_agg['passives']
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

        # Structured multi-select answers (JSON lists) — count value frequency.
        from collections import Counter
        enjoyed_c, genres_c, improvements_c = Counter(), Counter(), Counter()
        for r in ext_qs.values('enjoyed', 'genres', 'improvements'):
            enjoyed_c.update(v for v in (r['enjoyed'] or []) if v)
            genres_c.update(v for v in (r['genres'] or []) if v)
            improvements_c.update(v for v in (r['improvements'] or []) if v)

        def _top(counter):
            return [{'label': label, 'count': count} for label, count in counter.most_common(5)]

        # Single-select answers (CharFields) — same breakdown pattern as overall_rating.
        def _char_breakdown(field):
            return [
                {'label': row[field], 'count': row['count']}
                for row in ext_qs.exclude(**{field: ''})
                .values(field)
                .annotate(count=Count('id'))
                .order_by('-count')[:5]
            ]

        ext_structured = {
            'enjoyed': _top(enjoyed_c),
            'genres': _top(genres_c),
            'improvements': _top(improvements_c),
            'crowd_vibe': _char_breakdown('crowd_vibe'),
            'venue_feel': _char_breakdown('venue_feel'),
            'pre_event_info': _char_breakdown('pre_event_info'),
            'found_out_how': _char_breakdown('found_out_how'),
        }

    # Merge both sources
    survey_results = None
    survey_total_response_count = survey_responses_count + ext_count
    if survey_responses_count > 0 or ext_count > 0:
        combined_nps_total = int_nps_total + ext_nps_total
        combined_promoters = int_promoters + ext_promoters
        combined_passives = int_passives + ext_passives
        combined_detractors = int_detractors + ext_detractors
        if combined_nps_total > 0:
            nps_score = round((combined_promoters - combined_detractors) / combined_nps_total * 100)
            promoters_pct = round(combined_promoters / combined_nps_total * 100)
            passives_pct = round(combined_passives / combined_nps_total * 100)
            detractors_pct = round(combined_detractors / combined_nps_total * 100)
        else:
            nps_score = None
            promoters_pct = passives_pct = detractors_pct = 0

        all_comments = sorted(
            internal_comments + ext_comments,
            key=lambda comment: comment['submitted_at'],
            reverse=True,
        )[:5]

        survey_results = {
            'avg_star_rating': round(star_avg, 1) if star_avg else None,
            'nps_score': nps_score,
            'nps_total': combined_nps_total,
            'total': survey_total_response_count,
            'promoters': combined_promoters,
            'passives': combined_passives,
            'detractors': combined_detractors,
            'promoters_pct': promoters_pct,
            'passives_pct': passives_pct,
            'detractors_pct': detractors_pct,
            'recent_comments': all_comments,
            'internal_response_count': survey_responses_count,
            'ext_response_count': ext_count,
            'overall_rating_breakdown': ext_rating_breakdown,
            'ext_structured': ext_structured,
            'choice_breakdowns': choice_breakdowns,
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

    ci_show, ci_total, ci_checked, ci_by_type = _compute_event_checkin_stats(event)
    checkin = {
        'show': ci_show,
        'total_tickets': ci_total,
        'checked_in': ci_checked,
        'percent': round(ci_checked / ci_total * 100) if ci_total else 0,
        'by_type': ci_by_type,
    }

    # Audience analytics — only when the event has attendance data to show
    # (direct events past start, or external events with imported scan data).
    if ci_show:
        from tickets.services.audience import EventAudienceCalculator
        audience = EventAudienceCalculator(event).calculate()
    else:
        audience = {'show': False}

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
        'survey_scheduled_send_at': survey_scheduled_send_at,
        'external_survey_responses_count': ext_count,
        'survey_total_response_count': survey_total_response_count,
        'survey_results': survey_results,
        'attendee_segments': attendee_segments,
        'checkin': checkin,
        'audience': audience,
    }
    safe_cache_set(cache_key, result, timeout=300)
    return result


def _compute_marketing_events(event):
    """Return a flat, date-sorted list of marketing sends for this event.

    Each item is {'date': 'YYYY-MM-DD', 'type': 'sms'|'email', 'label': name}.
    Used to overlay send markers on the Activity chart. Computed fresh per request
    (NOT cached in _compute_event_stats) because the campaign models have no
    cache-invalidation signals — a send after caching would otherwise stay hidden.

    Timestamps are localized to match the chart's TruncDate-based axis labels.
    """
    from tickets.models import SMSCampaign

    def _local_date(ts):
        return django_tz.localtime(ts).date().isoformat()

    events = []

    # Mailchimp email campaigns.
    for row in event.email_campaigns.filter(
        send_time__isnull=False, deleted_at__isnull=True,
    ).values('send_time', 'campaign_title'):
        events.append({
            'date': _local_date(row['send_time']),
            'type': 'email',
            'label': row['campaign_title'],
        })

    # External SlickText SMS campaigns.
    for row in event.sms_campaigns.filter(
        send_time__isnull=False, deleted_at__isnull=True,
    ).values('send_time', 'name'):
        events.append({
            'date': _local_date(row['send_time']),
            'type': 'sms',
            'label': row['name'],
        })

    # Native Twilio SMS campaigns — only actually-sent ones.
    for row in event.native_sms_campaigns.filter(
        status=SMSCampaign.Status.SENT, sent_at__isnull=False, deleted_at__isnull=True,
    ).values('sent_at', 'name'):
        events.append({
            'date': _local_date(row['sent_at']),
            'type': 'sms',
            'label': row['name'],
        })

    events.sort(key=lambda e: e['date'])
    return events


def _compute_event_checkin_stats(event):
    """Return (should_show, total_tickets, checked_in_tickets, by_type).

    Check-ins are counted at the individual ticket level via Ticket.scanned_at,
    which is the single source of truth: live check-ins stamp every ticket in the
    admitted order, and CSV imports stamp each scanned ticket (so partially-scanned
    multi-ticket orders are counted accurately).

    should_show is True when the event's local start datetime has passed AND it is
    either a direct-ticketing event or an external event that has imported scan
    data. by_type is a list of {label, total, checked_in, percent} dicts grouped by
    Ticket.ticket_type, sorted by total desc.
    """
    tz_name = event.timezone or 'America/Los_Angeles'
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo('America/Los_Angeles')

    start_local = datetime.combine(
        event.start_date, event.start_time or time(0, 0)
    ).replace(tzinfo=tz)
    if start_local > django_tz.now():
        return False, 0, 0, []

    agg = event.ticket_orders.aggregate(
        total=Count('tickets'),
        checked_in=Count('tickets', filter=Q(tickets__scanned_at__isnull=False)),
    )

    # Direct events always show (even at 0%); external events only once they have
    # imported scan data, so untouched external events don't show an empty chart.
    if event.ticketing_type != 'direct' and not (agg['checked_in'] or 0):
        return False, 0, 0, []

    by_type_qs = (
        Ticket.objects.filter(ticket_order__event=event)
        .values('ticket_type')
        .annotate(
            total=Count('id'),
            checked_in=Count('id', filter=Q(scanned_at__isnull=False)),
        )
        .order_by('-total')
    )
    by_type = [
        {
            'label': row['ticket_type'] or 'Unspecified',
            'total': row['total'],
            'checked_in': row['checked_in'],
            'percent': round(row['checked_in'] / row['total'] * 100) if row['total'] else 0,
        }
        for row in by_type_qs
        if row['total']
    ]

    return True, agg['total'] or 0, agg['checked_in'] or 0, by_type


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


def _get_pacing_comparison_candidates(org, event, limit=100):
    """Past events available to compare against for sales pacing.

    Returns up to ``limit`` events that started before ``event`` (most recent
    first), ordered so events at the same venue come first — the same venue is
    the fairest pacing comparison. The caller uses the first item as the default
    comparison event and feeds the rest to the searchable comparison combobox.
    """
    past = list(
        Event.objects.filter(
            organization=org,
            start_date__lt=event.start_date,
        )
        .exclude(id=event.id)
        .only('id', 'name', 'start_date', 'venue_id')
        .order_by('-start_date')[:limit]
    )
    same_venue = [e for e in past if e.venue_id == event.venue_id]
    other = [e for e in past if e.venue_id != event.venue_id]
    return same_venue + other


def _recompute_utm_attribution_for_event(org, event):
    """Best-effort local recompute of Cue-tracked campaign attribution. Never raises.

    Covers both channels that attribute via first-party UTMs captured on ticket
    orders: Meta Ads (utm_id/utm_campaign -> EventExpense) and SlickText SMS
    broadcasts (utm_id/utm_campaign + utm_source=SlickText -> EventSMSCampaign).
    """
    try:
        from tickets.services.marketing.utm_attribution import UTMAttributionCalculator
        UTMAttributionCalculator(org).recompute_event(event)
    except Exception:
        logger.exception("UTM attribution recompute failed for org=%s event=%s", org.id, event.id)
    try:
        from tickets.services.marketing.sms_attribution import SMSAttributionCalculator
        SMSAttributionCalculator(org).recompute_event(event)
    except Exception:
        logger.exception("SMS attribution recompute failed for org=%s event=%s", org.id, event.id)


# How long to skip re-hitting Meta for an event's linked-campaign spend after a refresh.
_META_ADS_REFRESH_TTL_SECONDS = 30 * 60


def _refresh_meta_ads_expenses_for_event(org, event, user=None):
    """Best-effort refresh of linked Meta Ads campaign spend before event stats render.

    Lifetime insights are a heavy, rate-limited Graph API call, so we throttle to
    at most one refresh per event per window via a short-TTL cache marker instead
    of hitting Meta on every event-detail page load.
    """
    if not org.meta_ads_access_token or not org.meta_ads_account_id:
        return False

    refresh_marker_key = f"meta_ads_refresh:{event.pk}"
    if django_cache.get(refresh_marker_key):
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

    for expense in meta_expenses:
        try:
            insights = client.get_campaign_insights(expense.external_id)
        except MetaAdsAPIError as exc:
            had_error = True
            logger.warning(
                "Meta Ads insights refresh failed for org=%s event=%s campaign=%s: %s",
                org.id,
                event.id,
                expense.external_id,
                exc,
            )
            continue

        synced = True
        if _update_meta_ads_expense_from_insights(expense, insights, user):
            changed = True

    # Mark refreshed regardless of per-campaign outcome so a failing account can't
    # re-trigger the full set of API calls on every subsequent page load.
    django_cache.set(refresh_marker_key, True, _META_ADS_REFRESH_TTL_SECONDS)

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


def _update_meta_ads_expense_from_insights(expense, insights, user=None):
    """Write Meta insights (spend + attribution) onto a linked expense. Returns api_changed."""
    metadata = dict(expense.external_metadata or {})
    metadata['last_synced_at'] = django_tz.now().isoformat()

    api_changed = (
        expense.amount != insights.spend
        or expense.api_attributed_orders != insights.purchases
        or expense.api_attributed_revenue != insights.purchase_value
    )
    expense.amount = insights.spend
    expense.api_attributed_orders = insights.purchases
    expense.api_attributed_revenue = insights.purchase_value
    expense.external_metadata = metadata
    expense.version += 1
    update_fields = [
        'amount',
        'api_attributed_orders',
        'api_attributed_revenue',
        'external_metadata',
        'version',
        'updated_at',
    ]

    if user and user.is_authenticated:
        expense.updated_by = user
        update_fields.append('updated_by')

    if api_changed and expense.confirmed_at:
        expense.api_data_changed_at = django_tz.now()
        update_fields.append('api_data_changed_at')

    expense.save(update_fields=update_fields)
    return api_changed


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
    """Fetch a SlickText campaign plus best-effort analytics/link metrics."""
    campaign = client.get_campaign(campaign_id)
    try:
        analytics = client.get_campaign_analytics(campaign_id)
    except SlickTextAPIError:
        analytics = {}
    try:
        links = client.get_campaign_links(campaign_id)
    except SlickTextAPIError:
        links = []
    return build_slicktext_campaign_report(campaign, analytics, links)


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
    _recompute_utm_attribution_for_event(org, event)
    mailchimp_connection = _get_mailchimp_connection(org)

    weather_forecast = get_event_weather_forecast(event)

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
    allocation_sold_total = sum(
        c['sold'] for c in ticket_type_allocation_charts if not c['is_unlimited']
    )
    allocation_alloc_total = sum(
        c['allocated'] for c in ticket_type_allocation_charts if not c['is_unlimited']
    )
    allocation_remaining_total = max(allocation_alloc_total - allocation_sold_total, 0)
    allocation_percent_total = (
        round(min(allocation_sold_total / allocation_alloc_total * 100, 100))
        if allocation_alloc_total else 0
    )
    saleable_ticket_types_list = stats['saleable_ticket_types_list']
    survey_invitations_count = stats['survey_invitations_count']
    survey_responses_count = stats['survey_responses_count']
    survey_scheduled_send_at = stats['survey_scheduled_send_at']
    survey_scheduled_send_display = (
        _format_survey_send_time(event, survey_scheduled_send_at)
        if survey_scheduled_send_at else None
    )
    # Resolved send schedule (event override → org default) shown read-only in the
    # Send-survey dialog and as the surveys-tab "Auto-send" summary. The schedule
    # itself is configured in the survey builder, not here.
    _resolved_schedule = event.resolved_survey_schedule()
    _schedule_send_at_display = None
    _schedule_is_past = False
    if _resolved_schedule:
        try:
            _schedule_dt = _compute_survey_send_at(
                event, _resolved_schedule['offset_type'], _resolved_schedule['offset_value'],
                _resolved_schedule['time_of_day'], _resolved_schedule.get('anchor', 'end'),
            )
            _schedule_is_past = _schedule_dt <= django_tz.now()
            _schedule_send_at_display = _format_survey_send_time(event, _schedule_dt)
        except ValueError:
            pass
    survey_schedule_resolved = {
        'has_schedule': _resolved_schedule is not None,
        'description': _describe_survey_schedule(_resolved_schedule),
        'send_at_display': _schedule_send_at_display,
        'is_past': _schedule_is_past,
        # Schedule can actually be used only if it computes to a future time.
        'can_schedule': bool(_schedule_send_at_display) and not _schedule_is_past,
        # A prior cancel opted this event out of automatic arming.
        'opted_out': event.survey_auto_send_opted_out,
    }
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

    marketing_events = _compute_marketing_events(event)
    marketing_events_json = json.dumps(marketing_events)

    active_scanner_sessions = ScannerSession.objects.filter(event=event, is_active=True).count()

    show_checkin_chart, checkin_total_tickets, checkin_count, checkin_by_type = _compute_event_checkin_stats(event)
    checkin_percent = round(checkin_count / checkin_total_tickets * 100) if checkin_total_tickets else 0

    audience = stats.get('audience') or {'show': False}
    notable_attendees_page = None
    if audience.get('show'):
        from tickets.services.audience import EventAudienceCalculator
        from tickets.services.segmentation.segment_definitions import SEGMENT_BADGE_COLORS
        notable_qs = EventAudienceCalculator(event).notable_attendees_queryset()
        notable_paginator = Paginator(notable_qs, 10)
        notable_attendees_page = notable_paginator.get_page(request.GET.get('audience_page'))

    prev_event = _get_adjacent_event(org, event, 'prev')
    next_event = _get_adjacent_event(org, event, 'next')

    # Sales pacing — compare this event's cumulative sales curve against a
    # comparable past event, aligned on a days-before-event axis.
    pacing_candidates = _get_pacing_comparison_candidates(org, event)
    show_pacing_card = bool(total_orders) and bool(pacing_candidates)
    pacing_current_json = 'null'
    pacing_compare_json = 'null'
    pacing_candidate_list = []
    pacing_default_compare_id = None
    pacing_today_days_before = None
    if show_pacing_card:
        from tickets.services.forecasting.sales_curve import SalesCurveCalculator
        calc = SalesCurveCalculator()
        default_compare = pacing_candidates[0]
        pacing_default_compare_id = str(default_compare.id)
        pacing_current_json = json.dumps(calc.get_pacing_series(event))
        pacing_compare_json = json.dumps(calc.get_pacing_series(default_compare))
        pacing_candidate_list = [
            {'id': str(e.id), 'name': e.name, 'start_date': e.start_date}
            for e in pacing_candidates
        ]
        # Days-before-event for "today", so the chart can mark where the current
        # event stands on the shared pacing axis (positive = event still upcoming).
        pacing_today_days_before = (event.start_date - django_tz.localdate()).days

    # Native marketing SMS campaigns linked to this event (surfaced on the Marketing
    # tab when the org has SMS marketing enabled). Local import avoids load-order cycles.
    from tickets.sms_views import _annotate_counts
    from tickets.models import SMSCampaign
    native_sms_campaigns = list(
        _annotate_counts(
            SMSCampaign.objects.filter(
                organization=org, event=event, deleted_at__isnull=True,
            )
        ).order_by('-created_at')[:10]
    )

    # Customers tab — distinct buyers of this event, for bulk-tagging. Only built
    # for SMS-enabled orgs (the tab is hidden otherwise) to avoid extra queries.
    event_customers_page = None
    event_org_tags = []
    if org.sms_marketing_enabled:
        event_customers_qs = (
            Customer.objects.filter(organization=org, ticket_orders__event=event)
            .distinct()
            .annotate(
                event_orders=Count('ticket_orders', filter=Q(ticket_orders__event=event)),
                event_spend=Coalesce(
                    Sum('ticket_orders__total_amount', filter=Q(ticket_orders__event=event)),
                    Decimal('0.00'),
                ),
            )
            .order_by('name')
        )
        cust_paginator = Paginator(event_customers_qs, 100)
        event_customers_page = cust_paginator.get_page(request.GET.get('cust_page'))
        event_org_tags = CustomerTag.objects.filter(organization=org)

    context = {
        'event': event,
        'native_sms_campaigns': native_sms_campaigns,
        'event_customers_page': event_customers_page,
        'org_tags': event_org_tags,
        'weather_forecast': weather_forecast,
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
        'marketing_providers': marketing_providers(org),
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
        'survey_scheduled_send_at': survey_scheduled_send_at,
        'survey_scheduled_send_display': survey_scheduled_send_display,
        'survey_schedule_resolved': survey_schedule_resolved,
        'external_survey_responses_count': external_survey_responses_count,
        'survey_total_response_count': survey_total_response_count,
        'survey_results': survey_results,
        'ticket_type_breakdown': ticket_type_breakdown,
        'ticket_type_breakdown_json': ticket_type_breakdown_json,
        'ticket_type_allocation_charts': ticket_type_allocation_charts,
        'ticket_type_allocation_charts_json': ticket_type_allocation_charts_json,
        'allocation_sold_total': allocation_sold_total,
        'allocation_alloc_total': allocation_alloc_total,
        'allocation_remaining_total': allocation_remaining_total,
        'allocation_percent_total': allocation_percent_total,
        'sales_over_time_json': sales_over_time_json,
        'page_views_over_time_json': page_views_over_time_json,
        'marketing_events_json': marketing_events_json,
        'has_marketing_events': bool(marketing_events),
        'has_page_view_data': bool(stats['page_views_over_time']),
        'show_page_views_chart': event.ticketing_type == 'direct',
        'show_checkin_chart': show_checkin_chart,
        'checkin_total_tickets': checkin_total_tickets,
        'checkin_count': checkin_count,
        'checkin_percent': checkin_percent,
        'checkin_by_type': checkin_by_type,
        'audience': audience,
        'show_audience_tab': audience.get('show'),
        'notable_attendees_page': notable_attendees_page,
        'segment_badge_colors': SEGMENT_BADGE_COLORS if audience.get('show') else {},
        'prev_event_id': prev_event.id if prev_event else None,
        'next_event_id': next_event.id if next_event else None,
        'prev_event_name': prev_event.name if prev_event else None,
        'next_event_name': next_event.name if next_event else None,
        'show_pacing_card': show_pacing_card,
        'pacing_current_json': pacing_current_json,
        'pacing_compare_json': pacing_compare_json,
        'pacing_candidates': pacing_candidate_list,
        'pacing_default_compare_id': pacing_default_compare_id,
        'pacing_today_days_before': pacing_today_days_before,
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
            tl.full_url = request.build_absolute_uri(
                reverse('tickets:track_link_redirect', kwargs={'token': tl.token})
            )
        context['tracking_links'] = tracking_links
    context['today'] = date.today()

    # Surveys tab — per-form cards. One card per active TypeformFormSubscription,
    # each showing its previously-linked responses + a Match-responses modal trigger.
    typeform_subscriptions = list(
        TypeformFormSubscription.objects
        .filter(organization=org, is_active=True)
        .order_by('-created_at')
    )
    upload_to_linked: dict = {s.upload_id: [] for s in typeform_subscriptions if s.upload_id}
    if upload_to_linked:
        for resp in (
            ExternalSurveyResponse.objects
            .filter(event=event, upload_id__in=upload_to_linked.keys())
            .order_by('-responded_at')
        ):
            upload_to_linked.setdefault(resp.upload_id, []).append(resp)
    from .services.typeform.helpers import enrich_answers_with_titles, pick_preview_pairs
    for sub in typeform_subscriptions:
        sub.linked_responses = upload_to_linked.get(sub.upload_id, [])
        for resp in sub.linked_responses:
            resp.enriched_answers = enrich_answers_with_titles(resp.raw_answers, sub.questions)
            resp.preview_pairs = pick_preview_pairs(resp.enriched_answers, limit=2)
    context['typeform_subscriptions'] = typeform_subscriptions

    # Unified list of individual survey responses for the Surveys tab table.
    # Merges internal SurveyAnswer rows (one row per response with an NPS or
    # star_rating answer) with external/Typeform ExternalSurveyResponse rows.
    survey_response_rows = []
    if survey_responses_count > 0:
        # Group SurveyAnswer rows by response so we get one row per submission.
        # nps_score lives on the SurveyAnswer with question.question_type == 'nps';
        # star_rating on the question_type == 'star'; text_answer on text-type.
        internal_responses = (
            SurveyResponse.objects.filter(event=event)
            .select_related('customer')
            .prefetch_related('answers')
        )
        for resp in internal_responses:
            nps = None
            stars = None
            feedback_parts = []
            for ans in resp.answers.all():
                if ans.nps_score is not None and nps is None:
                    nps = ans.nps_score
                if ans.star_rating is not None and stars is None:
                    stars = ans.star_rating
                if ans.text_answer:
                    feedback_parts.append(ans.text_answer)
            survey_response_rows.append({
                'date': resp.submitted_at,
                'nps': nps,
                'rating': str(stars) if stars is not None else '',
                'rating_is_stars': stars is not None,
                'feedback': ' • '.join(feedback_parts),
                'source': 'Cue survey',
                'response_id': None,
                'detail_kind': 'internal',
                'detail_id': resp.id,
            })
    if external_survey_responses_count > 0:
        for resp in (
            ExternalSurveyResponse.objects.filter(event=event)
            .order_by('-responded_at')
        ):
            survey_response_rows.append({
                'date': resp.responded_at,
                'nps': resp.nps_score,
                'rating': resp.overall_rating or '',
                'rating_is_stars': False,
                'feedback': resp.text_feedback or '',
                'source': 'Typeform' if resp.typeform_response_id else 'External upload',
                'response_id': resp.id,
                'detail_kind': 'external',
                'detail_id': resp.id,
            })
    survey_response_rows.sort(key=lambda r: r['date'] or datetime.min, reverse=True)
    responses_paginator = Paginator(survey_response_rows, 25)
    responses_page_obj = responses_paginator.get_page(request.GET.get('responses_page'))
    context['survey_responses_page'] = responses_page_obj

    # Chart data for the rating breakdown bar (external-only — internal uses star_rating
    # on a different scale and is surfaced separately as avg_star_rating).
    if survey_results and survey_results.get('overall_rating_breakdown'):
        context['rating_labels'] = [r['overall_rating'] for r in survey_results['overall_rating_breakdown']]
        context['rating_counts'] = [r['count'] for r in survey_results['overall_rating_breakdown']]
    else:
        context['rating_labels'] = []
        context['rating_counts'] = []

    return render(request, 'tickets/event_detail.html', context)


@login_required
@require_org
def event_pacing_api(request, event_id):
    """Return the sales-pacing series for one event (used by the comparison dropdown).

    ``event_id`` is the comparison event; it is org-scoped so pacing can never be
    computed against another organization's event.
    """
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    from tickets.services.forecasting.sales_curve import SalesCurveCalculator
    data = SalesCurveCalculator().get_pacing_series(event)
    data.update({
        'id': str(event.id),
        'name': event.name,
        'start_date': event.start_date.isoformat(),
    })
    return JsonResponse(data)


@login_required
@require_org
@require_organizer
def event_weather_hourly(request, event_id):
    """Return JSON hourly forecast for every day the event spans."""
    org = get_organization(request)
    event = get_object_or_404(
        Event.objects.filter(organization=org).select_related('venue'),
        id=event_id,
    )
    data = get_event_hourly_forecast(event)
    if data is None:
        return JsonResponse({'days': [], 'venue_name': None})
    return JsonResponse(data)


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
def event_summary_stream(request, event_id):
    """SSE endpoint - streams an LLM-generated event summary."""
    from django.http import StreamingHttpResponse
    from django.core.cache import cache as django_cache

    from .services.event_summary import EventSummaryService

    org = get_organization(request)

    # Respect the org's display preference — endpoint is unavailable when hidden
    if not org.ai_event_summary_enabled:
        raise Http404()

    # Rate limit: 30 *successful* generations per org per hour. We only check the
    # ceiling here; the counter is incremented by the service after a summary is
    # actually produced, so failed attempts (e.g. a missing OpenAI key) don't burn
    # the budget or mask the real error behind a misleading "rate limit" message.
    rate_key = f"summary_ratelimit:{org.id}"
    try:
        if (django_cache.get(rate_key, 0) or 0) >= 30:
            return JsonResponse(
                {'error': 'Rate limit exceeded. Please try again later.'},
                status=429,
            )
    except Exception:
        # Redis unavailable — skip rate limiting rather than blocking the request
        pass

    event = get_object_or_404(
        Event.objects.filter(organization=org).select_related('venue'),
        id=event_id,
    )

    event_data = _compute_event_stats(event)

    service = EventSummaryService(org, user=request.user)
    response = StreamingHttpResponse(
        service.stream_summary(event, event_data, rate_limit_key=rate_key),
        content_type='text/event-stream',
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


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
                # Revoke loyalty points BEFORE the cascade hard-deletes the
                # event's orders; failures PROPAGATE so the delete aborts
                # rather than stranding points (eng review D6).
                revoke_points_for_orders(
                    list(event.ticket_orders.values_list('id', flat=True)),
                    description='Event deleted',
                )
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
    remaining_refundable = order.total_amount - order.refunded_amount
    can_refund = (
        order.event.ticketing_type == TICKETING_TYPE_DIRECT
        and stripe_session is not None
        and stripe_session.status in (
            StripeCheckoutSession.Status.COMPLETED,
            StripeCheckoutSession.Status.PARTIALLY_REFUNDED,
        )
        and order.refunded_at is None
        and remaining_refundable > 0
    )
    can_resend = (
        order.event.ticketing_type == TICKETING_TYPE_DIRECT
        and stripe_session is not None
        and bool(order.customer.email)
    )

    context = {
        'order': order,
        'tickets': tickets,
        'total_tickets': total_tickets,
        'ticket_types': ticket_types,
        'stripe_session': stripe_session,
        'can_refund': can_refund,
        'can_resend': can_resend,
        'remaining_refundable': remaining_refundable,
    }
    return render(request, 'tickets/order_detail.html', context)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def refund_order(request, order_id):
    """Issue a full or partial Stripe refund for a completed order."""
    from django.conf import settings as django_settings
    from decimal import InvalidOperation
    import stripe as stripe_lib

    org = get_organization(request)
    order = get_object_or_404(
        TicketOrder.objects.filter(event__organization=org).select_related(
            'customer', 'event', 'stripe_checkout_session',
        ),
        id=order_id
    )

    session = getattr(order, 'stripe_checkout_session', None)
    remaining = order.total_amount - order.refunded_amount
    if (
        order.event.ticketing_type != TICKETING_TYPE_DIRECT
        or session is None
        or session.status not in (
            StripeCheckoutSession.Status.COMPLETED,
            StripeCheckoutSession.Status.PARTIALLY_REFUNDED,
        )
        or order.refunded_at is not None
        or remaining <= 0
    ):
        messages.error(request, 'This order cannot be refunded.')
        return redirect('tickets:order_detail', order_id=order_id)

    if session.charge_flow == StripeCheckoutSession.ChargeFlow.DIRECT:
        # In-person charges live on the connected account; the platform key
        # can't refund them. Stripe keeps the platform fee on these refunds.
        messages.error(
            request,
            'In-person orders must be refunded from your Stripe dashboard for now.',
        )
        return redirect('tickets:order_detail', order_id=order_id)

    refund_type = request.POST.get('refund_type', 'full')
    if refund_type == 'partial':
        raw_amount = request.POST.get('refund_amount', '').strip()
        try:
            refund_amount = Decimal(raw_amount).quantize(Decimal('0.01'))
        except (InvalidOperation, ValueError):
            messages.error(request, 'Invalid refund amount.')
            return redirect('tickets:order_detail', order_id=order_id)
        if refund_amount <= 0 or refund_amount > remaining:
            messages.error(
                request,
                f'Refund amount must be between $0.01 and ${remaining}.',
            )
            return redirect('tickets:order_detail', order_id=order_id)
    else:
        refund_type = 'full'
        refund_amount = remaining
    refund_cents = int(refund_amount * 100)

    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
    try:
        if refund_type == 'partial':
            stripe_lib.Refund.create(
                payment_intent=session.stripe_session_id,
                amount=refund_cents,
            )
        else:
            stripe_lib.Refund.create(payment_intent=session.stripe_session_id)
    except stripe_lib.error.StripeError as e:
        logger.error("Stripe refund failed for order %s: %s", order_id, e)
        messages.error(request, f'Refund failed: {e.user_message or str(e)}')
        return redirect('tickets:order_detail', order_id=order_id)

    with transaction.atomic():
        order.refunded_amount = order.refunded_amount + refund_amount
        update_fields = ['refunded_amount']
        if refund_type == 'full':
            order.refunded_at = django_tz.now()
            update_fields.append('refunded_at')
        order.save(update_fields=update_fields)

        if refund_type == 'full':
            session.status = StripeCheckoutSession.Status.REFUNDED
        else:
            session.status = StripeCheckoutSession.Status.PARTIALLY_REFUNDED
        session.save(update_fields=['status'])

        if refund_type == 'full':
            for item in session.line_items_snapshot:
                tt_id = item.get('saleable_ticket_type_id')
                qty = item.get('quantity', 0)
                if tt_id and qty:
                    SaleableTicketType.objects.filter(id=tt_id).update(
                        quantity_sold=Greatest(F('quantity_sold') - qty, Value(0))
                    )

        order.customer.update_lifetime_value()
        if refund_type == 'full':
            # Loyalty points clawback: full refunds only (matches LTV's
            # coarse handling). Swallow — a refund must never fail on points.
            try:
                revoke_points_for_order(order, description='Order refunded')
            except Exception:
                logger.exception("Points revoke failed for refunded order %s", order.id)
        _invalidate_event_list_cache(org)
        _invalidate_marketing_cache(org)

    if refund_type == 'full':
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
    else:
        new_remaining = order.total_amount - order.refunded_amount
        messages.success(
            request,
            f'Refunded ${refund_amount} on order {order.display_order_number}. '
            f'Remaining refundable: ${new_remaining}.',
        )
    return redirect('tickets:order_detail', order_id=order_id)


@login_required
@require_org
@require_host
@require_http_methods(["POST"])
def resend_order_confirmation(request, order_id):
    """Re-send the order confirmation email for a direct-purchase order."""
    org = get_organization(request)
    order = get_object_or_404(
        TicketOrder.objects.filter(event__organization=org).select_related(
            'customer', 'event', 'stripe_checkout_session',
        ),
        id=order_id
    )
    stripe_session = getattr(order, 'stripe_checkout_session', None)
    if order.event.ticketing_type != TICKETING_TYPE_DIRECT or stripe_session is None:
        messages.error(request, 'Confirmation emails can only be resent for direct-purchase orders.')
        return redirect('tickets:order_detail', order_id=order_id)
    if not order.customer.email:
        messages.error(request, 'This order has no customer email on file.')
        return redirect('tickets:order_detail', order_id=order_id)

    from .tasks import send_order_confirmation_email_task
    send_order_confirmation_email_task.delay(str(order.id))
    messages.success(request, f'Confirmation email queued to {order.customer.email}.')
    return redirect('tickets:order_detail', order_id=order_id)


# Format Management Views

@login_required
@require_org
@require_host
def format_list(request):
    """List all CSV formats available to the org (its own + global built-ins)."""
    org = get_organization(request)
    formats = CSVFormat.available_for(org)
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


@login_required
@require_org
@require_host
def format_duplicate(request, format_id):
    """Copy a visible format (incl. a global built-in) into an editable org copy."""
    org = get_organization(request)
    # Source can be one of the org's own formats or a global built-in.
    source = get_object_or_404(CSVFormat.available_for(org), id=format_id)

    # Build a unique name within the global-unique constraint.
    base_name = f"{source.name} (Custom)"
    new_name = base_name
    suffix = 2
    while CSVFormat.objects.filter(name=new_name).exists():
        new_name = f"{base_name} {suffix}"
        suffix += 1

    copy = CSVFormat(
        organization=org,
        name=new_name,
        description=source.description,
        is_default=False,
        is_system=False,
        requires_manual_pricing=source.requires_manual_pricing,
        uses_tiers=source.uses_tiers,
        column_mapping=source.column_mapping,
    )
    copy.save()
    messages.success(
        request,
        f"Created an editable copy '{copy.name}'. Customize it below.",
    )
    return redirect('tickets:format_edit', format_id=copy.id)


# Market Management Views

@login_required
@require_org
@require_host
def market_list(request):
    """List organization markets with event counts."""
    org = get_organization(request)
    markets = (
        Market.objects.filter(organization=org)
        .annotate(event_count=Count('events'))
        .order_by('geography_level', 'name')
    )
    context = {
        'markets': markets,
    }
    return render(request, 'tickets/market_list.html', context)


@login_required
@require_org
@require_host
def market_builder(request):
    """Bulk-create markets from event venue city, state, or country."""
    org = get_organization(request)
    builder = MarketBuilder(org)
    level = builder.normalize_level(
        request.POST.get('level') if request.method == 'POST' else request.GET.get('level')
    )

    if request.method == 'POST':
        values = request.POST.getlist('values')
        if not values:
            messages.error(request, 'Select at least one region to create markets.')
            return redirect(f"{reverse('tickets:market_builder')}?{urlencode({'level': level})}")

        result = builder.build(level, values)
        messages.success(
            request,
            f"Created {result['created_count']} market(s) and assigned "
            f"{result['updated_count']} event(s)."
        )
        return redirect('tickets:market_list')

    context = {
        'level': level,
        'level_choices': [
            {'value': key, 'label': label}
            for key, label in builder.LEVEL_LABELS.items()
        ],
        'preview_rows': builder.preview(level),
    }
    return render(request, 'tickets/market_builder.html', context)


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
@require_http_methods(["POST"])
def venue_create_inline(request):
    """Create a venue via AJAX (from the create/edit event page) and return JSON.

    Returns the new venue's id, dropdown label (matching VenueChoiceField), and
    capacity so the event form's venue <select> can be updated without a reload.
    """
    org = get_organization(request)
    form = VenueForm(request.POST)
    if form.is_valid():
        venue = form.save(commit=False)
        venue.organization = org
        # unique_together includes `organization`, which isn't a form field, so the
        # form can't validate it — guard the IntegrityError and report it inline.
        # Normalize first so the check matches the city casing applied on save().
        from .address_utils import normalize_venue_address_fields
        normalize_venue_address_fields(venue)
        if Venue.objects.filter(
            organization=org, name=venue.name, city=venue.city
        ).exists():
            return JsonResponse(
                {'success': False, 'errors': {
                    'name': ['A venue with this name and city already exists.']
                }},
                status=400,
            )
        venue.save()
        label = VenueChoiceField(queryset=Venue.objects.none()).label_from_instance(venue)
        return JsonResponse({
            'success': True,
            'venue': {
                'id': str(venue.id),
                'label': label,
                'capacity': venue.capacity,
            },
        })
    return JsonResponse({'success': False, 'errors': form.errors}, status=400)


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
            MarketBuilder(org).assign_events_for_venue(venue)
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
    """Landing page to choose Direct or External ticketing before creating an event.

    When external events are disabled for the org (the default), skip the chooser
    and go straight to the direct-ticketing create form.
    """
    org = get_organization(request)
    if not (org and org.external_events_enabled):
        return redirect('tickets:event_create', ticketing_type='direct')
    return render(request, 'tickets/event_type_select.html', {})


@login_required
@require_org
@require_host
def event_create(request, ticketing_type):
    """Create new event (ticketing_type comes from URL, chosen on type-select page)."""
    from .models import TICKETING_TYPE_DIRECT, TICKETING_TYPE_EXTERNAL
    from django.conf import settings as django_settings
    if ticketing_type not in (TICKETING_TYPE_DIRECT, TICKETING_TYPE_EXTERNAL):
        return redirect('tickets:event_type_select')
    org = get_organization(request)
    if ticketing_type == TICKETING_TYPE_EXTERNAL and not org.external_events_enabled:
        return redirect('tickets:event_create', ticketing_type=TICKETING_TYPE_DIRECT)

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
                    MarketBuilder(org).assign_event(event, save=False)
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
            'google_maps_api_key': django_settings.GOOGLE_MAPS_API_KEY,
        }
        return render(request, 'tickets/event_create.html', context)

    # External ticketing path (unchanged)
    if request.method == 'POST':
        form = EventForm(
            request.POST, organization=org,
            ticketing_type_locked=True,
            hide_ticket_link=False,
        )
        if form.is_valid():
            event = form.save(commit=False)
            event.organization = org
            event.created_by = request.user
            event.status = EVENT_STATUS_LIVE
            MarketBuilder(org).assign_event(event, save=False)
            event.save()
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

    venue_capacities = {
        str(v.id): v.capacity
        for v in Venue.objects.filter(organization=org)
        if v.capacity
    }
    context = {
        'form': form,
        'venue_capacities_json': json.dumps(venue_capacities),
        'ticketing_type': ticketing_type,
        'google_maps_api_key': django_settings.GOOGLE_MAPS_API_KEY,
    }
    return render(request, 'tickets/event_create.html', context)


@login_required
@require_org
@require_host
def event_edit(request, event_id):
    """Edit an existing event."""
    from .models import TICKETING_TYPE_DIRECT
    from django.conf import settings as django_settings
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
                    MarketBuilder(org).assign_event(event, save=False)
                    event.save()
                    _invalidate_event_list_cache(org)
                    _invalidate_marketing_cache(org)
                    _invalidate_event_campaign_match_cache(event.id)
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
            'google_maps_api_key': django_settings.GOOGLE_MAPS_API_KEY,
        }
        return render(request, 'tickets/event_edit.html', context)

    # External ticketing path
    if request.method == 'POST':
        form = EventForm(request.POST, instance=event, organization=org, ticketing_type_locked=True)
        if form.is_valid():
            was_future = event.start_date >= date.today()
            event = form.save(commit=False)
            event.updated_by = request.user
            MarketBuilder(org).assign_event(event, save=False)
            event.save()
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
            _invalidate_event_campaign_match_cache(event.id)
            messages.success(request, f"Event '{event.name}' updated successfully.")
            return redirect('tickets:event_detail', event_id=event.id)
    else:
        form = EventForm(instance=event, organization=org, ticketing_type_locked=True)

    context = {
        'form': form,
        'event': event,
        'ticketing_type': event.ticketing_type,
        'google_maps_api_key': django_settings.GOOGLE_MAPS_API_KEY,
    }
    return render(request, 'tickets/event_edit.html', context)


@login_required
@require_org
@require_host
@require_external_events
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


@login_required
@require_org
@require_admin
def settings_display_preferences(request):
    """Toggle which optional cards appear on event pages (e.g. AI Event Summary)."""
    org = get_organization(request)
    if request.method == 'POST':
        form = OrgDisplayPreferencesForm(request.POST, instance=org)
        if form.is_valid():
            form.save()
            messages.success(request, 'Display preferences updated.')
            return redirect('tickets:settings_display_preferences')
    else:
        form = OrgDisplayPreferencesForm(instance=org)
    return render(request, 'tickets/settings_display_preferences.html', {'form': form, 'org': org})


def _candidate_bands_from_form(form):
    """Build (kwargs, monetary_tuple) for classify_segment_absolute from a valid form."""
    cd = form.cleaned_data
    kwargs = {
        'recency_active': cd['recency_active_days'],
        'recency_cooling': cd['recency_cooling_days'],
        'freq_few': cd['freq_few'],
        'freq_many': cd['freq_many'],
    }
    monetary = (float(cd['monetary_mid']), float(cd['monetary_high']))
    return kwargs, monetary


def _preview_size_rows(sizes):
    """Shape preview_absolute_sizes output for the template (badge color + plain blurb)."""
    if not sizes or sizes.get('status') != 'ok':
        return None
    from .services.segmentation.segment_definitions import SEGMENT_DESCRIPTIONS
    return [
        {
            'segment': name,
            'count': info['count'],
            'pct': info['pct'],
            'badge_color': SEGMENT_BADGE_COLORS.get(name, 'secondary'),
            'description': SEGMENT_DESCRIPTIONS.get(name, ''),
        }
        for name, info in sizes['by_segment'].items()
    ]


def _annotate_order_check(rows):
    """For the 'are your segments in the right order?' list.

    rows are in value order (best group first). Each non-empty group's later spend
    should be <= the group above it. Flag any that spent MORE than the last non-empty
    group above them, rescale bars to this list's own max, and count the violations.
    Returns (rows, violations).
    """
    max_rev = max((r['avg_future_revenue'] for r in rows if r['n']), default=0) or 1
    violations = 0
    prev = None  # (segment, avg) of the last non-empty group above
    for r in rows:
        r['bar_pct'] = max(0, round(100 * r['avg_future_revenue'] / max_rev)) if r['n'] else 0
        r['out_of_order'] = False
        r['above'] = None
        if r['n']:
            if prev is not None and r['avg_future_revenue'] > prev[1]:
                r['out_of_order'] = True
                r['above'] = prev[0]
                violations += 1
            prev = (r['segment'], r['avg_future_revenue'])
    return rows, violations


def _recommendations(diag, candidate, current_score=None):
    """One-click 'apply' recommendations from data-driven better cut-offs.

    Each item: label + why + apply_json (a {input_id: value} map the front-end
    fills in, then re-runs the check). Empty when the cut-offs already look good
    or when a suggested change would fail the same future-revenue check shown in
    this panel.
    """
    recs = diag.recommended_bands(candidate)  # e.g. {'freq_many': 3, 'monetary_high': 65}
    if not recs:
        return []

    from .services.segmentation.segment_definitions import classify_segment_absolute, SEGMENT_VALUE_ORDER

    def trial_candidate(keys):
        band_kwargs, monetary_bands = candidate
        trial_kwargs = dict(band_kwargs)
        mid, high = monetary_bands
        if 'freq_many' in keys:
            trial_kwargs['freq_many'] = recs['freq_many']
        if 'monetary_high' in keys:
            high = recs['monetary_high']
        return trial_kwargs, (mid, high)

    def passes_revenue_check(keys):
        if current_score is None:
            return True
        result = diag._backtest(
            absolute_fn=classify_segment_absolute,
            bands=trial_candidate(keys),
            value_order=SEGMENT_VALUE_ORDER,
        )
        score = (result.get('separation') or {}).get('spearman_future_revenue')
        if result.get('status') != 'ok' or score is None:
            return False
        # Match the visible verdict margin: a recommendation should not click
        # through into "worse than current automatic segments."
        return score >= current_score - 0.05

    out = []
    if 'freq_many' in recs and passes_revenue_check(['freq_many']):
        out.append({
            'label': 'Set “frequent buyer” to {} orders'.format(recs['freq_many']),
            'why': 'Almost nobody qualifies now, so your top segments are nearly empty.',
            'apply_json': json.dumps({'id_freq_many': recs['freq_many']}),
        })
    if 'monetary_high' in recs and passes_revenue_check(['monetary_high']):
        out.append({
            'label': 'Set “top spender” to ${:g}'.format(recs['monetary_high']),
            'why': 'Hardly anyone clears the current amount, so your best spenders don’t stand out.',
            'apply_json': json.dumps({'id_monetary_high': recs['monetary_high']}),
        })
    if len(out) > 1 and passes_revenue_check(['freq_many', 'monetary_high']):
        combined = {}
        if 'freq_many' in recs:
            combined['id_freq_many'] = recs['freq_many']
        if 'monetary_high' in recs:
            combined['id_monetary_high'] = recs['monetary_high']
        out.append({
            'label': 'Use all suggested numbers',
            'why': '',
            'apply_json': json.dumps(combined),
            'primary': True,
        })
    return out


def _spread_note(segment):
    """Plain-English advice when one preview segment dominates the customer base."""
    if not segment:
        return None
    if segment == 'Dormant':
        return (
            'Most customers are Dormant. That can be normal if many have no orders '
            'or only one old, low-spend order. If you want to treat more older '
            'customers as still reachable, widen the “Recently active” or '
            '“Slipping away” day ranges.'
        )
    if segment in ('VIP', 'Loyal', 'Big Spender'):
        return (
            'Most customers fall into one top segment ({}). Raise the frequent-buyer '
            'or top-spender thresholds so the highest-value segments stay selective.'
        ).format(segment)
    return (
        'Most customers fall into one segment ({}). Adjust the cut-offs above to '
        'spread customers only if this does not match how you would market to them.'
    ).format(segment)


@login_required
@require_org
@require_admin
def settings_segment_tuning(request):
    """Set absolute segment cut-offs, preview the result, and switch modes.

    Actions (hidden `action` field): preview (sizes), preview_backtest (sizes +
    separation), save (persist + recalc if absolute), reset (re-seed suggested).
    """
    from .services.segmentation.segment_definitions import (
        seed_segment_bands, classify_segment_absolute, SEGMENT_VALUE_ORDER,
    )
    from .services.segmentation.validation import SegmentDiagnostics
    from .tasks import recalculate_rfm_task

    org = get_organization(request)
    # Ensure monetary defaults are meaningful before the form renders.
    seed_segment_bands(org)

    context = {'org': org, 'preview_sizes': None, 'preview_current_sizes': None,
               'backtest_rows': None, 'backtest_current_rows': None,
               'backtest_separation': None, 'backtest_status': None,
               'rfm_recalc_in_progress': org.rfm_recalc_in_progress}

    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        if action == 'reset':
            seed_segment_bands(org, force=True)
            messages.info(request, 'Cut-offs reset to the suggested values for your data.')
            return redirect('tickets:settings_segment_tuning')

        form = SegmentTuningForm(request.POST, instance=org)
        if form.is_valid():
            if action == 'save':
                form.save()
                if org.segment_mode == 'absolute':
                    # Re-read the flag from DB — it may have flipped since page load.
                    org.refresh_from_db(fields=['rfm_recalc_in_progress'])
                    if not org.rfm_recalc_in_progress:
                        recalculate_rfm_task.delay(str(org.id))
                    messages.success(request, 'Saved. Recalculating segments with your cut-offs.')
                else:
                    messages.success(request, 'Cut-offs saved. Segments stay on percentile mode.')
                return redirect('tickets:settings_segment_tuning')

            elif action in ('preview', 'preview_backtest'):
                # no save — just render the requested preview
                candidate = _candidate_bands_from_form(form)
                diag = SegmentDiagnostics(org)
                context['preview_sizes'] = _preview_size_rows(diag.preview_absolute_sizes(candidate))
                if action == 'preview_backtest':
                    result = diag._backtest(
                        absolute_fn=classify_segment_absolute, bands=candidate,
                        value_order=SEGMENT_VALUE_ORDER,
                    )
                    context['backtest_status'] = result.get('status')
                    if result.get('status') == 'ok':
                        context['backtest_rows'] = _segment_health_backtest_rows(result)
                        context['backtest_separation'] = result.get('separation')
                    # current-rules backtest for side-by-side comparison
                    current = diag._backtest(holdout_days=result.get('holdout_days', 180))
                    if current.get('status') == 'ok':
                        context['backtest_current_rows'] = _segment_health_backtest_rows(current)
                        context['backtest_current_separation'] = current.get('separation')
                    # Align both to the same value-ordered groups (current only feeds
                    # the one-line verdict now), then build the single "order check".
                    if context.get('backtest_rows') and context.get('backtest_current_rows'):
                        context['backtest_rows'], context['backtest_current_rows'] = _align_backtest_rows(
                            context['backtest_rows'], context['backtest_current_rows'], SEGMENT_VALUE_ORDER,
                        )
                        order_rows, violations = _annotate_order_check(context['backtest_rows'])
                        context['order_rows'] = order_rows
                        context['order_violations'] = violations
                        cur_sep = (current.get('separation') or {}).get('spearman_future_revenue')
                        context['recommendations'] = _recommendations(diag, candidate, cur_sep)
                        # Over-concentration has no single safe value to apply.
                        _spread = next((r['segment'] for r in (context.get('preview_sizes') or [])
                                        if r['pct'] > 40), None)
                        context['spread_note'] = _spread_note(_spread)
                    # Plain-English verdict: is the proposed better/similar/worse?
                    prop_sep = (result.get('separation') or {}).get('spearman_future_revenue')
                    cur_sep = (current.get('separation') or {}).get('spearman_future_revenue')
                    if result.get('status') == 'ok' and prop_sep is not None and cur_sep is not None:
                        delta = round(prop_sep - cur_sep, 4)
                        verdict = 'better' if delta > 0.05 else 'worse' if delta < -0.05 else 'similar'
                        context['backtest_verdict'] = {
                            'proposed': prop_sep, 'current': cur_sep,
                            'delta': delta, 'label': verdict,
                            'proposed_pct': max(0, round(prop_sep * 100)),
                            'current_pct': max(0, round(cur_sep * 100)),
                        }

        # AJAX preview: return just the result partial so the page updates in
        # place (no reload). Falls back to full page for non-AJAX posts
        # (progressive enhancement). Fires for valid and invalid forms.
        if (action in ('preview', 'preview_backtest')
                and request.headers.get('x-requested-with') == 'XMLHttpRequest'):
            context['form'] = form
            return render(request, 'tickets/_segment_preview.html', context)
    else:
        form = SegmentTuningForm(instance=org)

    context['form'] = form
    return render(request, 'tickets/settings_segment_tuning.html', context)


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
        'api_attributed_orders': e.api_attributed_orders,
        'api_attributed_revenue': f'{e.api_attributed_revenue:.2f}' if e.api_attributed_revenue is not None else '',
        'cue_attributed_orders': e.cue_attributed_orders,
        'cue_attributed_revenue': f'{e.cue_attributed_revenue:.2f}' if e.cue_attributed_revenue is not None else '',
        'attribution_source': e.attribution_source,
        'is_confirmed': e.is_confirmed,
        'needs_review': e.needs_review,
        'status_label': _status_label(e),
        'status_badge_class': _status_badge_class(e),
    }




















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


def _bucket_margin(bucket):
    """Profit margin % for a chart bucket, or None when net revenue is non-positive.

    Uses net revenue (revenue - fees) as the denominator to stay consistent with
    the per-event and summary margin figures.
    """
    net = bucket['net_revenue']
    return (bucket['profit'] / net * 100) if net > 0 else None


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
        )
        .select_related('venue', 'market')
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

    summary_net_revenue = summary_revenue - summary_fees
    summary_profit = summary_net_revenue - summary_expenses
    summary_margin = (summary_profit / summary_net_revenue * 100) if summary_net_revenue > 0 else None

    # Market rollup by assigned market (sorted high → low for chart)
    markets: dict = {}
    for row in event_rows:
        event_market = row['event'].market
        market_label = event_market.name if event_market else NO_MARKET_LABEL
        m = markets.setdefault(market_label, {
            'market_id': str(event_market.id) if event_market else '',
            'market_name': market_label,
            'market_label': market_label,
            'city': market_label,
            'revenue': Decimal('0.00'),
            'expenses': Decimal('0.00'), 'profit': Decimal('0.00'),
            'net_revenue': Decimal('0.00'),
            'event_count': 0,
        })
        m['revenue'] += row['revenue']
        m['expenses'] += row['expenses']
        m['profit'] += row['profit']
        m['net_revenue'] += row['net_revenue']
        m['event_count'] += 1
    market_rows = sorted(markets.values(), key=lambda m: m['profit'], reverse=True)

    # Market chart data - same array shape as the other granularities so the chart can
    # render Revenue vs Expenses / Profit / Margin % grouped by market. Includes every
    # assigned market (plus "No market") sorted high → low by profit as the initial order;
    # the client re-sorts on demand. Cast Decimals to float/None so json.dumps can
    # serialize them.
    market_chart_data = {
        'labels': [m['market_label'] for m in market_rows],
        'revenue': [float(m['revenue']) for m in market_rows],
        'expenses': [float(m['expenses']) for m in market_rows],
        'profit': [float(m['profit']) for m in market_rows],
        'margin': [
            float(_bucket_margin(m)) if _bucket_margin(m) is not None else None
            for m in market_rows
        ],
    }

    # Monthly aggregation for chart - bucket events by calendar month, ordered earliest → most recent
    chart_events = [r for r in reversed(event_rows) if r['revenue'] > 0 or r['expenses'] > 0]
    month_buckets_profit = {}
    for r in chart_events:
        key = r['event'].start_date.strftime('%Y-%m')
        m = month_buckets_profit.setdefault(key, {'month': key, 'revenue': 0.0, 'expenses': 0.0, 'profit': 0.0, 'net_revenue': 0.0})
        m['revenue'] += float(r['revenue'])
        m['expenses'] += float(r['expenses'])
        m['profit'] += float(r['profit'])
        m['net_revenue'] += float(r['net_revenue'])
    monthly_profit_chart = sorted(month_buckets_profit.values(), key=lambda x: x['month'])
    chart_data = {
        'labels': [m['month'] for m in monthly_profit_chart],
        'revenue': [m['revenue'] for m in monthly_profit_chart],
        'expenses': [m['expenses'] for m in monthly_profit_chart],
        'profit': [m['profit'] for m in monthly_profit_chart],
        'margin': [_bucket_margin(m) for m in monthly_profit_chart],
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
            'revenue': 0.0, 'expenses': 0.0, 'profit': 0.0, 'net_revenue': 0.0,
        })
        m['revenue'] += float(r['revenue'])
        m['expenses'] += float(r['expenses'])
        m['profit'] += float(r['profit'])
        m['net_revenue'] += float(r['net_revenue'])
    quarterly_profit_chart = sorted(quarter_buckets_profit.values(), key=lambda x: x['sort_key'])
    quarter_chart_data = {
        'labels': [m['label'] for m in quarterly_profit_chart],
        'revenue': [m['revenue'] for m in quarterly_profit_chart],
        'expenses': [m['expenses'] for m in quarterly_profit_chart],
        'profit': [m['profit'] for m in quarterly_profit_chart],
        'margin': [_bucket_margin(m) for m in quarterly_profit_chart],
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
        'margin': [
            float(r['margin']) if r['margin'] is not None else None
            for r in event_chart_events
        ],
    }

    context = {
        'event_rows': event_rows,
        'summary_revenue': summary_revenue,
        'summary_expenses': summary_expenses,
        'summary_net_revenue': summary_net_revenue,
        'summary_profit': summary_profit,
        'summary_margin': summary_margin,
        'chart_data_json': json.dumps(chart_data),
        'event_chart_data_json': json.dumps(event_chart_data),
        'quarter_chart_data_json': json.dumps(quarter_chart_data),
        'market_chart_data_json': json.dumps(market_chart_data),
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
    ).order_by('position').prefetch_related('options')
    if event_questions.exists():
        return event_questions

    # 2. Organization defaults
    org_questions = SurveyQuestion.objects.filter(
        organization=event.organization, event__isnull=True, is_active=True
    ).order_by('position').prefetch_related('options')
    if org_questions.exists():
        return org_questions

    # 3. System defaults (no event, no org)
    return SurveyQuestion.objects.filter(
        event__isnull=True, organization__isnull=True, is_active=True
    ).order_by('position').prefetch_related('options')


# _resolve_effective_questions is an alias for the public resolver, named for the
# builder/freeze code paths where "effective survey" reads more clearly.
_resolve_effective_questions = _get_survey_questions_for_event


def _copy_question(src, **overrides):
    """Deep-copy a SurveyQuestion (and its options) into a new row.

    Reused by: clone-system-defaults-to-org, customize-for-event, and the
    send-time freeze. Pass overrides like event=..., organization=... to set the
    new scope.
    """
    fields = {
        'question_text': src.question_text,
        'question_type': src.question_type,
        'position': src.position,
        'is_required': src.is_required,
        'is_active': src.is_active,
        'event': src.event,
        'organization': src.organization,
    }
    fields.update(overrides)
    new_q = SurveyQuestion.objects.create(**fields)
    options = [
        SurveyQuestionOption(question=new_q, label=o.label, position=o.position)
        for o in src.options.all()
    ]
    if options:
        SurveyQuestionOption.objects.bulk_create(options)
    return new_q


def _clone_effective_to_event(event):
    """Freeze the effective survey into immutable event-scoped rows.

    Clones the FULL resolved set (event > org > system) into event scope so the
    event owns a stable snapshot. No-op if the event already has its own active
    questions. Returns True if anything was cloned.
    """
    if SurveyQuestion.objects.filter(event=event, is_active=True).exists():
        return False
    source = list(_resolve_effective_questions(event))
    with transaction.atomic():
        for src in source:
            _copy_question(src, event=event, organization=event.organization)
    return bool(source)


def _survey_locked(event):
    """An event's survey is locked once any invitation has been created for it."""
    return SurveyInvitation.objects.filter(event=event).exists()


def _parse_survey_answer(question, post_data):
    """Parse + validate one submitted answer for `question` from POST data.

    Returns (answer_data, error). answer_data is a dict with the scalar fields
    and (for choice questions) 'selected_option_ids'. error is a message string
    or None. Pure and unit-testable; the public form loop calls this per question.
    """
    field_name = f"question_{question.id}"

    if question.question_type in SurveyQuestion.CHOICE_TYPES:
        valid_ids = {str(o.id) for o in question.options.all()}
        if question.question_type == 'single_select':
            raw = (post_data.get(field_name) or '').strip()
            selected = [raw] if raw else []
        else:
            selected = [v for v in post_data.getlist(field_name) if v]
        if question.is_required and not selected:
            return None, "This field is required."
        if any(v not in valid_ids for v in selected):
            return None, "Invalid selection."
        return {
            'question': question, 'star_rating': None, 'nps_score': None,
            'text_answer': '', 'selected_option_ids': selected,
        }, None

    value = (post_data.get(field_name) or '').strip()
    if question.is_required and not value:
        return None, "This field is required."
    if not value:
        return {'question': question, 'star_rating': None, 'nps_score': None, 'text_answer': ''}, None

    if question.question_type == 'star_rating':
        try:
            rating = int(value)
            if not (1 <= rating <= 5):
                raise ValueError
        except (ValueError, TypeError):
            return None, "Please select a rating between 1 and 5."
        return {'question': question, 'star_rating': rating, 'nps_score': None, 'text_answer': ''}, None

    if question.question_type == 'nps':
        try:
            score = int(value)
            if not (0 <= score <= 10):
                raise ValueError
        except (ValueError, TypeError):
            return None, "Please select a score between 0 and 10."
        return {'question': question, 'star_rating': None, 'nps_score': score, 'text_answer': ''}, None

    return {'question': question, 'star_rating': None, 'nps_score': None, 'text_answer': value}, None


def _survey_recipients(event, org):
    """Attendees with a ticket order for `event` who have NOT yet been sent a
    survey invitation. Single source of truth for the count modal and send_survey."""
    existing_customer_ids = SurveyInvitation.objects.filter(
        event=event
    ).values_list('customer_id', flat=True)
    return Customer.objects.filter(
        ticket_orders__event=event, organization=org
    ).distinct().exclude(id__in=existing_customer_ids)


def _event_tz(event):
    """ZoneInfo for the event's timezone, falling back to Pacific on bad data."""
    try:
        return ZoneInfo(event.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo('America/Los_Angeles')


def _compute_survey_send_at(event, offset_type, offset_value, time_of_day, anchor='end'):
    """Absolute UTC datetime to send the survey, from an offset relative to the
    event's start or end. Raises ValueError with a user-facing message on bad input.

    anchor 'end' (default) -> measured from event end; 'start' -> from event start
    offset_type 'hours' -> anchor + N hours
    offset_type 'days'  -> N days after the anchor date, at time_of_day (event tz)
    """
    try:
        value = int(offset_value)
    except (TypeError, ValueError):
        raise ValueError("Enter a whole number for the offset.")
    if value < 0:
        raise ValueError("The offset can't be negative.")

    anchor_dt = (event.start_datetime() if anchor == 'start'
                 else event.end_datetime())  # aware, in the event's timezone

    if offset_type == 'hours':
        send_local = anchor_dt + timedelta(hours=value)
    elif offset_type == 'days':
        if not isinstance(time_of_day, time):
            raise ValueError("Pick a time of day to send.")
        send_local = datetime.combine(
            anchor_dt.date() + timedelta(days=value), time_of_day, tzinfo=anchor_dt.tzinfo,
        )
    else:
        raise ValueError("Choose how to schedule the survey.")

    return send_local.astimezone(ZoneInfo('UTC'))


def _format_survey_send_time(event, dt_utc):
    """Human-readable send time in the event's timezone, e.g. 'Jun 27, 2026 at 6:00 PM PDT'."""
    local = dt_utc.astimezone(_event_tz(event))
    return local.strftime('%b %d, %Y at %-I:%M %p %Z')


@login_required
@require_org
@require_host
def survey_recipient_count(request, event_id):
    """Count of attendees who would receive the survey if sent now. GET, JSON."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    return JsonResponse({'count': _survey_recipients(event, org).count()})


@login_required
@require_org
@require_host
def survey_schedule_preview(request, event_id):
    """Preview the absolute send time for a relative survey-send offset. GET, JSON.

    Powers the live "Survey will send: …" line in the send modal so the host sees
    exactly what the scheduled POST would compute.
    """
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    time_of_day = parse_time(request.GET.get('time_of_day') or '')
    try:
        send_at = _compute_survey_send_at(
            event,
            request.GET.get('offset_type'),
            request.GET.get('offset_value'),
            time_of_day,
            request.GET.get('anchor') or 'end',
        )
    except ValueError as exc:
        return JsonResponse({'valid': False, 'error': str(exc)})

    if send_at <= django_tz.now():
        return JsonResponse({
            'valid': False,
            'error': 'That works out to a time in the past — pick a larger offset.',
        })
    return JsonResponse({'valid': True, 'display': _format_survey_send_time(event, send_at)})


@login_required
@require_org
@require_host
def send_survey(request, event_id):
    """Send the survey to attendees immediately. POST only.

    This is the manual "send now" path. Scheduled delivery is handled
    automatically by the survey scheduler (see the send_due_survey_invitations
    management command) for events with an auto-send schedule, so this view only
    ever sends right away — used to override the schedule and send early, or to
    send for events with no schedule configured.
    """
    if request.method != 'POST':
        return redirect('tickets:event_detail', event_id=event_id)

    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    # Get attendees who don't already have an invitation for this event
    attendees = _survey_recipients(event, org)

    if not attendees.exists():
        messages.info(request, "All attendees have already been sent a survey for this event.")
        return redirect('tickets:event_detail', event_id=event_id)

    # Freeze the survey: clone the effective question set into immutable
    # event-scoped rows so answers reference a stable snapshot. No-op if the
    # event already owns questions (e.g. it was customized or already sent).
    _clone_effective_to_event(event)

    # Create invitations (scheduled_send_at=None → send immediately).
    invitations = [
        SurveyInvitation(
            event=event,
            customer=customer,
            organization=org,
            email=customer.email,
            scheduled_send_at=None,
        )
        for customer in attendees
    ]
    SurveyInvitation.objects.bulk_create(invitations)
    # bulk_create bypasses post_save signals — invalidate manually
    django_cache.delete(_event_stats_cache_key(event.id))
    _invalidate_event_upload_stats_cache(event.id)

    from .tasks import send_survey_emails_task
    send_survey_emails_task.delay(str(event_id), str(org.id))
    messages.success(
        request,
        f"Survey invitations created for {len(invitations)} attendee(s). Emails are being sent."
    )
    return redirect('tickets:event_detail', event_id=event_id)


@login_required
@require_org
@require_host
def cancel_scheduled_survey(request, event_id):
    """Cancel a scheduled (not-yet-sent) survey send. POST only.

    Deletes the pending scheduled invitations, which also unlocks the survey
    builder and restores the recipient pool so the host can reschedule or edit.
    """
    if request.method != 'POST':
        return redirect('tickets:event_detail', event_id=event_id)

    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    deleted, _ = SurveyInvitation.objects.filter(
        event=event,
        organization=org,
        sent_at__isnull=True,
        scheduled_send_at__isnull=False,
    ).delete()
    # Opt the event out of auto-send so the scheduler doesn't immediately re-arm
    # the invitations the host just canceled.
    if not event.survey_auto_send_opted_out:
        event.survey_auto_send_opted_out = True
        event.save(update_fields=['survey_auto_send_opted_out'])
    django_cache.delete(_event_stats_cache_key(event.id))
    _invalidate_event_upload_stats_cache(event.id)

    if deleted:
        messages.success(request, "Scheduled survey send canceled.")
    else:
        messages.info(request, "There was no scheduled survey send to cancel.")
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

        # Validate answers (per-question parse helper handles every type)
        answers_data = []
        for question in questions:
            data, error = _parse_survey_answer(question, request.POST)
            if error:
                errors[f"question_{question.id}"] = error
            else:
                answers_data.append(data)

        if not errors:
            with transaction.atomic():
                response = SurveyResponse.objects.create(
                    invitation=invitation,
                    event=invitation.event,
                    customer=invitation.customer,
                    organization=invitation.organization,
                )
                # Choice answers carry an M2M (through-model), so create each
                # answer individually + full_clean() rather than bulk_create.
                for data in answers_data:
                    answer = SurveyAnswer(
                        response=response,
                        question=data['question'],
                        star_rating=data['star_rating'],
                        nps_score=data['nps_score'],
                        text_answer=data['text_answer'],
                    )
                    answer.full_clean(exclude=['selected_options'])
                    answer.save()
                    option_ids = data.get('selected_option_ids') or []
                    if option_ids:
                        SurveyAnswerOption.objects.bulk_create([
                            SurveyAnswerOption(
                                answer=answer,
                                option_id=oid,
                                question=data['question'],
                            )
                            for oid in option_ids
                        ])

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
# Survey builder (organizer-facing)
# ---------------------------------------------------------------------------

def _survey_scope(request, event_id):
    """Resolve (org, event_or_None). Event is org-scoped → 404 cross-org."""
    org = get_organization(request)
    event = None
    if event_id:
        event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    return org, event


def _scope_base_qs(org, event):
    """Unfiltered question queryset for the scope (org template vs event)."""
    if event:
        return SurveyQuestion.objects.filter(event=event)
    return SurveyQuestion.objects.filter(organization=org, event__isnull=True)


def _builder_redirect(event):
    if event:
        return redirect('tickets:event_survey_builder', event_id=event.id)
    return redirect('tickets:survey_builder')


def _builder_url_names(event):
    """URL names + base args for the active scope (org template vs event)."""
    if event:
        return ('tickets:event_survey_question_save', 'tickets:event_survey_question_delete',
                'tickets:event_survey_reorder', [event.id])
    return ('tickets:survey_question_save', 'tickets:survey_question_delete',
            'tickets:survey_reorder', [])


def _decorate_builder_urls(questions, event):
    """Attach per-question save/delete (AJAX) URLs for the inline builder."""
    save_name, del_name, _reorder, args = _builder_url_names(event)
    for q in questions:
        q.save_url = reverse(save_name, args=args + [q.id])
        q.delete_url = reverse(del_name, args=args + [q.id])
    return questions


def _question_dict(q, event):
    """JSON representation of a saved question for the inline builder."""
    save_name, del_name, _reorder, args = _builder_url_names(event)
    return {
        'id': str(q.id),
        'text': q.question_text,
        'type': q.question_type,
        'type_display': q.get_question_type_display(),
        'required': q.is_required,
        'position': q.position,
        'options': [{'id': str(o.id), 'label': o.label} for o in q.options.all()],
        'save_url': reverse(save_name, args=args + [q.id]),
        'delete_url': reverse(del_name, args=args + [q.id]),
    }


def _validate_choice_options(qtype, option_labels):
    """Choice questions require ≥1 option. Returns error str or None."""
    if qtype in SurveyQuestion.CHOICE_TYPES and not option_labels:
        return "Choice questions need at least one option."
    return None


def _validate_single_metric(base_qs, qtype, exclude_id=None):
    """Enforce ≤ 1 active NPS and ≤ 1 active star question per survey."""
    if qtype in ('nps', 'star_rating'):
        existing = base_qs.filter(question_type=qtype, is_active=True)
        if exclude_id:
            existing = existing.exclude(id=exclude_id)
        if existing.exists():
            label = 'NPS' if qtype == 'nps' else 'star rating'
            return f"A survey can only have one {label} question."
    return None


@login_required
@require_org
@require_host
def survey_builder(request, event_id=None):
    """List + inline-manage survey questions for the org template or one event."""
    org, event = _survey_scope(request, event_id)
    locked = bool(event) and _survey_locked(event)

    questions = list(
        _scope_base_qs(org, event)
        .filter(is_active=True)
        .order_by('position')
        .prefetch_related('options')
    )
    _decorate_builder_urls(questions, event)

    save_name, _del, reorder_name, args = _builder_url_names(event)
    context = {
        'event': event,
        'questions': questions,
        'questions_json': [_question_dict(q, event) for q in questions],
        'locked': locked,
        'is_org_template': event is None,
        'question_types': SurveyQuestion.QUESTION_TYPE_CHOICES,
        'choice_types': list(SurveyQuestion.CHOICE_TYPES),
        'create_save_url': reverse(save_name, args=args),
        'reorder_url': reverse(reorder_name, args=args),
        'preview_url': reverse('tickets:event_survey_preview', args=args) if event
                       else reverse('tickets:survey_preview'),
        'survey_email_subject': event.survey_email_subject if event else org.survey_email_subject,
        'survey_subject_inherited': (
            ((org.survey_email_subject or '').strip() or DEFAULT_SURVEY_SUBJECT)
            if event else DEFAULT_SURVEY_SUBJECT
        ),
        'survey_subject_save_url': (
            reverse('tickets:event_survey_email_subject_save', args=[event.id]) if event
            else reverse('tickets:survey_email_subject_save')
        ),
        # Org-wide reply-to (sender identity). Org scope only; no per-event override.
        'survey_reply_to_email': org.survey_reply_to_email,
        'survey_send_test_url': (
            reverse('tickets:event_survey_send_test_email', args=[event.id]) if event
            else reverse('tickets:survey_send_test_email')
        ),
        # Default send schedule (org default / per-event override), mirroring subject.
        'survey_send_offset_type': (event if event else org).survey_send_offset_type,
        'survey_send_offset_value': (event if event else org).survey_send_offset_value,
        'survey_send_time_of_day': (event if event else org).survey_send_time_of_day,
        'survey_send_anchor': (event if event else org).survey_send_anchor or 'end',
        'survey_send_offset_choices': SURVEY_SEND_OFFSET_CHOICES,
        # Event scope: whether this event has its own schedule (vs inheriting the default).
        'survey_schedule_overridden': bool(event and event.survey_send_offset_type),
        'survey_schedule_inherited': _describe_survey_schedule(
            {'offset_type': org.survey_send_offset_type,
             'offset_value': org.survey_send_offset_value,
             'time_of_day': org.survey_send_time_of_day,
             'anchor': org.survey_send_anchor or 'end'} if event else None
        ),
        'survey_schedule_save_url': (
            reverse('tickets:event_survey_schedule_save', args=[event.id]) if event
            else reverse('tickets:survey_schedule_save')
        ),
    }
    if event and not questions:
        context['effective_preview'] = list(_resolve_effective_questions(event))
    if event is None and not questions:
        context['system_defaults_available'] = SurveyQuestion.objects.filter(
            event__isnull=True, organization__isnull=True, is_active=True
        ).exists()
    return render(request, 'tickets/survey/builder.html', context)


@login_required
@require_org
@require_host
def survey_question_save(request, event_id=None, question_id=None):
    """JSON create-or-update of a survey question + its options (inline builder)."""
    from django.db.models import Max
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'errors': ['POST required.']}, status=405)
    org, event = _survey_scope(request, event_id)
    if event and _survey_locked(event):
        return JsonResponse({'ok': False, 'errors': ['This survey has been sent and is locked.']}, status=403)

    try:
        data = json.loads(request.body or '{}')
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'ok': False, 'errors': ['Invalid request.']}, status=400)

    text = (data.get('question_text') or '').strip()
    qtype = data.get('question_type')
    required = bool(data.get('is_required'))
    labels = []
    for o in (data.get('options') or []):
        label = (o.get('label') if isinstance(o, dict) else o) or ''
        label = str(label).strip()
        if label:
            labels.append(label)

    base_qs = _scope_base_qs(org, event)
    question = get_object_or_404(base_qs, id=question_id) if question_id else None

    errors = []
    if not text:
        errors.append('Question text is required.')
    if qtype not in {c[0] for c in SurveyQuestion.QUESTION_TYPE_CHOICES}:
        errors.append('Choose a question type.')
    if not errors:
        err = (_validate_choice_options(qtype, labels)
               or _validate_single_metric(base_qs, qtype, exclude_id=question.id if question else None))
        if err:
            errors.append(err)
    if errors:
        return JsonResponse({'ok': False, 'errors': errors}, status=422)

    with transaction.atomic():
        if question is None:
            question = SurveyQuestion(
                event=event, organization=org,
                position=(base_qs.aggregate(m=Max('position'))['m'] or 0) + 1,
            )
        question.question_text = text
        question.question_type = qtype
        question.is_required = required
        question.save()
        # Replace options wholesale (editable questions never have answers).
        question.options.all().delete()
        if qtype in SurveyQuestion.CHOICE_TYPES:
            SurveyQuestionOption.objects.bulk_create([
                SurveyQuestionOption(question=question, label=label, position=i)
                for i, label in enumerate(labels)
            ])

    question = base_qs.prefetch_related('options').get(id=question.id)
    return JsonResponse({'ok': True, 'question': _question_dict(question, event)})


@login_required
@require_org
@require_host
def survey_question_delete(request, question_id, event_id=None):
    """JSON delete. Soft-deactivate if it somehow has answers; block if locked."""
    org, event = _survey_scope(request, event_id)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'errors': ['POST required.']}, status=405)
    if event and _survey_locked(event):
        return JsonResponse({'ok': False, 'errors': ['This survey is locked.']}, status=403)
    question = get_object_or_404(_scope_base_qs(org, event), id=question_id)
    if SurveyAnswer.objects.filter(question=question).exists():
        question.is_active = False
        question.save(update_fields=['is_active'])
    else:
        question.delete()
    return JsonResponse({'ok': True})


@login_required
@require_org
@require_host
def survey_reorder(request, event_id=None):
    """JSON up/down: swap a question's position with its neighbour."""
    org, event = _survey_scope(request, event_id)
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'errors': ['POST required.']}, status=405)
    if event and _survey_locked(event):
        return JsonResponse({'ok': False, 'errors': ['This survey is locked.']}, status=403)
    try:
        data = json.loads(request.body or '{}')
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'ok': False, 'errors': ['Invalid request.']}, status=400)

    base_qs = list(_scope_base_qs(org, event).filter(is_active=True).order_by('position'))
    qid = data.get('question_id')
    direction = data.get('direction')
    idx = next((i for i, q in enumerate(base_qs) if str(q.id) == str(qid)), None)
    if idx is not None:
        swap = idx - 1 if direction == 'up' else idx + 1 if direction == 'down' else None
        if swap is not None and 0 <= swap < len(base_qs):
            a, b = base_qs[idx], base_qs[swap]
            a.position, b.position = b.position, a.position
            SurveyQuestion.objects.bulk_update([a, b], ['position'])
    return JsonResponse({'ok': True})


class _PreviewOption:
    """Stand-in mimicking SurveyQuestionOption for rendering survey_form.html."""
    def __init__(self, q_idx, opt_idx, label):
        self.id = f"q{q_idx}o{opt_idx}"
        self.label = label


class _PreviewOptionManager:
    def __init__(self, options):
        self._options = options

    def all(self):
        return self._options


class _PreviewQuestion:
    """Stand-in mimicking SurveyQuestion for rendering the survey preview."""
    def __init__(self, idx, text, qtype, required, labels):
        self.id = f"q{idx}"
        self.question_text = text
        self.question_type = qtype
        self.is_required = required
        self.options = _PreviewOptionManager(
            [_PreviewOption(idx, j, lbl) for j, lbl in enumerate(labels)]
        )


def _preview_questions_from_drafts(drafts):
    """Build preview question stand-ins from draft dicts (live edit) — skips blanks."""
    out = []
    for i, d in enumerate(drafts or []):
        text = (d.get('question_text') or '').strip()
        if not text:
            continue
        labels = []
        for o in (d.get('options') or []):
            label = (o.get('label') if isinstance(o, dict) else o) or ''
            label = str(label).strip()
            if label:
                labels.append(label)
        out.append(_PreviewQuestion(i, text, d.get('question_type') or 'text', bool(d.get('is_required')), labels))
    return out


def _preview_questions_from_saved(questions):
    """Build preview question stand-ins from saved SurveyQuestion rows."""
    return [
        _PreviewQuestion(
            i, q.question_text, q.question_type, q.is_required,
            [o.label for o in q.options.all()],
        )
        for i, q in enumerate(questions)
    ]


def _preview_invitation(event):
    """A fake invitation object so survey_form.html renders its header."""
    from types import SimpleNamespace
    if event:
        return SimpleNamespace(event=event)
    return SimpleNamespace(event=SimpleNamespace(
        name="Your event",
        start_date=django_tz.now().date(),
        venue=SimpleNamespace(name="Your venue", city="Your city"),
    ))


@login_required
@require_org
@require_host
def survey_preview(request, event_id=None):
    """Render the exact public survey page for the builder's live preview.

    GET → from the scope's saved questions (iframe default src, also no-JS path).
    POST (JSON draft) → from the in-progress edits (iframe srcdoc).
    """
    org, event = _survey_scope(request, event_id)

    if request.method == 'POST':
        try:
            data = json.loads(request.body or '{}')
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({'error': 'Invalid request.'}, status=400)
        questions = _preview_questions_from_drafts(data.get('questions'))
    else:
        if event:
            saved = list(
                _scope_base_qs(org, event).filter(is_active=True)
                .order_by('position').prefetch_related('options')
            ) or list(_resolve_effective_questions(event))
        else:
            saved = list(
                _scope_base_qs(org, None).filter(is_active=True)
                .order_by('position').prefetch_related('options')
            )
        questions = _preview_questions_from_saved(saved)

    return render(request, 'tickets/survey/survey_form.html', {
        'invitation': _preview_invitation(event),
        'questions': questions,
        'errors': {},
        'preview': True,
    })


@login_required
@require_org
@require_host
def event_survey_customize(request, event_id):
    """Clone the effective survey into editable event-scoped rows. POST only."""
    org, event = _survey_scope(request, event_id)
    if request.method != 'POST':
        return _builder_redirect(event)
    if _survey_locked(event):
        messages.error(request, "This survey has been sent and can no longer be customized.")
        return _builder_redirect(event)
    if _clone_effective_to_event(event):
        messages.success(request, "You can now customize this event's survey.")
    return _builder_redirect(event)


@login_required
@require_org
@require_host
def survey_email_subject_save(request, event_id=None):
    """Save the survey email subject for the org template or one event. POST only."""
    org, event = _survey_scope(request, event_id)
    if request.method != 'POST':
        return _builder_redirect(event)
    if event and _survey_locked(event):
        messages.error(request, "This survey has been sent and can no longer be edited.")
        return _builder_redirect(event)
    subject = (request.POST.get('email_subject') or '').strip()[:255]
    if event:
        event.survey_email_subject = subject
        event.save(update_fields=['survey_email_subject'])
    else:
        org.survey_email_subject = subject
        org.save(update_fields=['survey_email_subject'])
    messages.success(request, "Survey email subject saved.")
    return _builder_redirect(event)


@login_required
@require_org
@require_host
def survey_reply_to_save(request):
    """Save the org-wide survey reply-to email. Org scope only, POST only.

    Blank clears the override (surveys then send as Cue with no reply-to)."""
    org = get_organization(request)
    if request.method != 'POST':
        return _builder_redirect(None)
    reply_to = (request.POST.get('reply_to_email') or '').strip()
    if reply_to:
        from django.core.validators import validate_email
        from django.core.exceptions import ValidationError
        try:
            validate_email(reply_to)
        except ValidationError:
            messages.error(request, "Enter a valid reply-to email address.")
            return _builder_redirect(None)
    org.survey_reply_to_email = reply_to
    org.save(update_fields=['survey_reply_to_email'])
    messages.success(request, "Survey reply-to address saved.")
    return _builder_redirect(None)


def _describe_survey_schedule(schedule):
    """Human-readable description of a resolved survey schedule dict (or None).

    e.g. "2 days after the event ends at 9:00 AM", "3 hours after the event ends",
    or "Send manually" when no automatic schedule is configured.
    """
    if not schedule or not (schedule.get('offset_type') or '').strip():
        return "Send manually"
    value = schedule.get('offset_value')
    if value is None:
        return "Send manually"
    anchor_word = 'starts' if schedule.get('anchor') == 'start' else 'ends'
    if schedule['offset_type'] == 'hours':
        unit = 'hour' if value == 1 else 'hours'
        return f"{value} {unit} after the event {anchor_word}"
    unit = 'day' if value == 1 else 'days'
    tod = schedule.get('time_of_day')
    when = tod.strftime('%-I:%M %p') if tod else '9:00 AM'
    return f"{value} {unit} after the event {anchor_word} at {when}"


@login_required
@require_org
@require_host
def survey_schedule_save(request, event_id=None):
    """Save the default survey send schedule for the org template or one event.

    Mirrors survey_email_subject_save: an org-level default with a per-event
    override. A blank offset_type clears the schedule (= inherit / send now).
    POST only.
    """
    org, event = _survey_scope(request, event_id)
    if request.method != 'POST':
        return _builder_redirect(event)
    if event and _survey_locked(event):
        messages.error(request, "This survey has been sent and can no longer be edited.")
        return _builder_redirect(event)

    offset_type = (request.POST.get('offset_type') or '').strip()
    target = event if event else org

    schedule_fields = [
        'survey_send_offset_type', 'survey_send_offset_value',
        'survey_send_time_of_day', 'survey_send_anchor',
    ]

    # Re-saving an event's schedule re-enables auto-send if a prior cancel had
    # opted it out (the org template has no opt-out flag).
    save_fields = list(schedule_fields)
    if event:
        event.survey_auto_send_opted_out = False
        save_fields.append('survey_auto_send_opted_out')

    if offset_type not in ('hours', 'days'):
        # Clear the schedule -> send immediately (event) / no org default.
        target.survey_send_offset_type = ''
        target.survey_send_offset_value = None
        target.survey_send_time_of_day = None
        target.survey_send_anchor = ''
        target.save(update_fields=save_fields)
        messages.success(request, "Survey send time saved.")
        return _builder_redirect(event)

    try:
        value = int(request.POST.get('offset_value'))
        if value < 0:
            raise ValueError
    except (TypeError, ValueError):
        messages.error(request, "Enter a whole number for the send-time offset.")
        return _builder_redirect(event)

    time_of_day = None
    if offset_type == 'days':
        time_of_day = parse_time(request.POST.get('time_of_day') or '')
        if time_of_day is None:
            messages.error(request, "Pick a time of day to send.")
            return _builder_redirect(event)

    anchor = 'start' if request.POST.get('anchor') == 'start' else 'end'
    target.survey_send_offset_type = offset_type
    target.survey_send_offset_value = value
    target.survey_send_time_of_day = time_of_day
    target.survey_send_anchor = anchor
    target.save(update_fields=save_fields)
    messages.success(request, "Survey send time saved.")
    return _builder_redirect(event)


@login_required
@require_org
@require_host
def survey_send_test_email(request, event_id=None):
    """Send a one-off test survey email to a chosen address. POST only."""
    org, event = _survey_scope(request, event_id)
    if request.method != 'POST':
        return _builder_redirect(event)

    recipient = (request.POST.get('test_email') or '').strip()
    from django.core.validators import validate_email
    from django.core.exceptions import ValidationError
    try:
        validate_email(recipient)
    except ValidationError:
        messages.error(request, "Enter a valid email address to send a test to.")
        return _builder_redirect(event)

    # Org-scope test has no event — render against a throwaway sample.
    target = event or Event(
        organization=org, name='Your Event', start_date=django_tz.now().date()
    )
    site_url = settings.SITE_URL.rstrip('/')
    # Test has no invitation; point the button at the organizer survey preview
    # (same page the builder's live preview uses) instead of a dead token URL.
    preview_path = (reverse('tickets:event_survey_preview', args=[event.id]) if event
                    else reverse('tickets:survey_preview'))
    survey_url = f"{site_url}{preview_path}"

    from .tasks import build_survey_email, survey_sender_fields
    from django.core.mail import EmailMultiAlternatives
    subject, text_body, html_body = build_survey_email(target, survey_url)
    from_email, reply_to = survey_sender_fields(org)
    try:
        msg = EmailMultiAlternatives(
            subject=subject, body=text_body, from_email=from_email,
            to=[recipient], reply_to=reply_to,
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send()
        messages.success(request, f"Test survey email sent to {recipient}.")
    except Exception:
        logger.exception("Failed to send test survey email to %s", recipient)
        messages.error(request, "Couldn't send the test email. Check email settings and try again.")
    return _builder_redirect(event)


@login_required
@require_org
@require_host
def event_survey_reset(request, event_id):
    """Remove event-scoped questions so the event falls back to the org default. POST only."""
    org, event = _survey_scope(request, event_id)
    if request.method != 'POST':
        return _builder_redirect(event)
    if _survey_locked(event):
        messages.error(request, "This survey has been sent and can no longer be reset.")
        return _builder_redirect(event)
    SurveyQuestion.objects.filter(event=event).delete()
    messages.success(request, "Reverted to the organization's default survey.")
    return _builder_redirect(event)


@login_required
@require_org
@require_host
def survey_hub(request):
    """Top-level Surveys landing: events with survey activity + links to builders."""
    org = get_organization(request)

    def _per_event_count(model):
        # Isolated per-row subquery (avoids join inflation across the multiple
        # related tables we count here). See CLAUDE.md / event_list pattern.
        return Coalesce(Subquery(
            model.objects.filter(event=OuterRef('pk')).values('event')
            .annotate(c=Count('id')).values('c')[:1],
            output_field=models.IntegerField(),
        ), 0)

    events = (
        Event.objects.filter(organization=org)
        .select_related('venue')
        .annotate(
            # Total responses = internal (SurveyResponse) + external (Typeform CSV).
            response_count=_per_event_count(SurveyResponse) + _per_event_count(ExternalSurveyResponse),
            invitation_count=_per_event_count(SurveyInvitation),
        )
        .order_by('-start_date', '-start_time')[:50]
    )
    org_question_count = SurveyQuestion.objects.filter(
        organization=org, event__isnull=True, is_active=True
    ).count()
    return render(request, 'tickets/survey/hub.html', {
        'events': events,
        'org_question_count': org_question_count,
    })


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
        'low_stock_threshold': tt.low_stock_threshold if tt.low_stock_threshold is not None else '',
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
@require_organizer
def saleable_ticket_type_orders(request, event_id, ticket_type_id):
    """List the orders that contain a given direct-ticketing ticket type.

    Reached from the Ticket Allocation card on event_detail. Orders are matched
    by ticket-type *name* (Ticket.ticket_type is the denormalized name string,
    not a FK), so two ticket types sharing a name on one event collapse together
    — tracked in TODOS.md for a future key-based filter.
    """
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    ticket_type = get_object_or_404(
        SaleableTicketType.objects.filter(event=event), id=ticket_type_id
    )

    # Mirror the orders annotation pattern used by event_detail so the table
    # renders identically (customer, status, gross total, ticket counts).
    _platform_fee_subq = Subquery(
        StripeCheckoutSession.objects.filter(ticket_order=OuterRef('pk')).values('platform_fee_cents')[:1],
        output_field=DecimalField(max_digits=10, decimal_places=2),
    )
    orders_qs = event.ticket_orders.filter(
        tickets__ticket_type=ticket_type.name
    ).select_related(
        'customer', 'uploaded_file'
    ).annotate(
        tickets_count=Count('tickets'),
        type_count=Count('tickets', filter=Q(tickets__ticket_type=ticket_type.name)),
        gross_total=ExpressionWrapper(
            F('total_amount') - Cast(
                Coalesce(_platform_fee_subq, 0),
                output_field=DecimalField(max_digits=10, decimal_places=2),
            ) * Decimal('0.01'),
            output_field=DecimalField(max_digits=10, decimal_places=2),
        ),
    ).distinct().order_by('-order_date')

    paginator = Paginator(orders_qs, 100)
    page_obj = paginator.get_page(request.GET.get('page'))

    return render(request, 'tickets/saleable_ticket_type_orders.html', {
        'event': event,
        'ticket_type': ticket_type,
        'page_obj': page_obj,
    })


@login_required
@require_org
@require_host
@require_http_methods(["POST"])
def saleable_ticket_type_reorder(request, event_id):
    """Persist a new display order for an event's ticket types.

    Accepts JSON {"order": ["<uuid>", "<uuid>", ...]}. Writes `order` = index
    for each matching ticket type belonging to this event.
    """
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    try:
        payload = json.loads(request.body.decode('utf-8'))
        ids = payload.get('order') or []
        if not isinstance(ids, list):
            raise ValueError('order must be a list')
    except (ValueError, json.JSONDecodeError):
        return JsonResponse({'success': False, 'error': 'Invalid payload'}, status=400)

    tts = {str(tt.id): tt for tt in SaleableTicketType.objects.filter(event=event)}
    to_update = []
    for index, raw_id in enumerate(ids):
        tt = tts.get(str(raw_id))
        if tt is None:
            continue
        if tt.order != index:
            tt.order = index
            to_update.append(tt)
    if to_update:
        SaleableTicketType.objects.bulk_update(to_update, ['order'], batch_size=100)
        _invalidate_event_list_cache(org)

    return JsonResponse({'success': True, 'updated': len(to_update)})


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

        if session.charge_flow == StripeCheckoutSession.ChargeFlow.DIRECT:
            # In-person charges live on the connected account; the platform
            # key can't refund them. Count into the manual-refund warning.
            failed_count += 1
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
            # Loyalty points clawback — only orders that actually get
            # refunded_at (Stripe-session orders), matching LTV behavior.
            try:
                revoke_points_for_order(order, description='Event cancelled')
            except Exception:
                logger.exception("Points revoke failed for cancelled order %s", order.id)

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


UTM_PARAM_KEYS = (
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_id', 'utm_content', 'utm_term',
)


def _extract_utm_params(request):
    """Pull non-empty UTM params, fbclid, and referrer from a landing request."""
    params = {}
    for key in UTM_PARAM_KEYS:
        value = (request.GET.get(key) or '').strip()
        if value:
            params[key] = value[:200]
    fbclid = (request.GET.get('fbclid') or '').strip()
    if fbclid:
        params['fbclid'] = fbclid[:255]
    referrer = (request.META.get('HTTP_REFERER') or '').strip()
    if referrer:
        params['referrer'] = referrer[:500]
    return params


def _event_social_proof(event):
    """Build the public-page social proof: up to 6 distinct confirmed attendees + total count."""
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
    return attendee_preview, attendee_count


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
        attendee_preview, attendee_count = _event_social_proof(event)
        return render(request, 'tickets/buy/public_event_buy.html', {
            'event': event,
            'sales_ended': True,
            'form': None,
            'available_pairs': [],
            'locked_pairs': [],
            'coming_soon_types': [],
            'waitlisted_sold_out_types': [],
            'waitlist_join_forms': {},
            'already_on_waitlist': set(),
            'all_sold_out': False,
            'min_ticket_price': None,
            'view_event_id': '',
            'attendee_preview': attendee_preview,
            'attendee_count': attendee_count,
            'wl_held_tt_id': None,
            'user_is_authenticated': request.user.is_authenticated,
            **_build_public_event_preview_context(event, suffix='Ticket Sales Ended'),
        })
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
        utm_params = _extract_utm_params(request)
        if utm_params:
            # Last-non-empty wins within the session (standard last-click).
            stored = dict(request.session.get(f'utm_{event.id}') or {})
            stored.update(utm_params)
            request.session[f'utm_{event.id}'] = stored
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
    attendee_preview, attendee_count = _event_social_proof(event)

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
    """Public redirect that records a click and forwards to the ticket page.

    Off-site (external ticket page) when the link has a target_url; otherwise the event's
    Cue buy page. Clicks are counted either way; the buy-page path also stashes the ref so
    a resulting checkout attributes back to this link.
    """
    link = get_object_or_404(TrackingLink.objects.select_related('event'), token=token)
    TrackingLink.objects.filter(pk=link.pk).update(click_count=models.F('click_count') + 1)
    if link.target_url:
        return redirect(link.target_url)
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
            link_customer_to_buyer(customer, buyer_email)
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
                attribution=request.session.get(f'utm_{event.id}') or {},
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
            try:
                award_points_for_order(order)
            except Exception:
                logger.exception("Points award failed for order %s", order.id)
            _invalidate_event_list_cache(org)
            _invalidate_marketing_cache(org)

        if order.attribution:
            _recompute_utm_attribution_for_event(org, event)

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

    buyer_phone = ''
    if request.user.is_authenticated:
        profile = getattr(request.user, 'profile', None)
        if profile and profile.phone_number:
            buyer_phone = profile.phone_number

    return render(request, 'tickets/buy/checkout_payment.html', {
        'event': event,
        'cart': cart,
        'total_cents': total_cents,
        'total_dollars': total_dollars,
        'is_free': is_free,
        'stripe_publishable_key': django_settings.STRIPE_PUBLISHABLE_KEY,
        'saved_pm': saved_pm,
        'user_is_authenticated': request.user.is_authenticated,
        'buyer_phone': buyer_phone,
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

    # Destination charge: organizer net rides transfer_data into the connected
    # account at sale time. Fall back to a plain platform charge when the org
    # isn't onboarded (funds join the legacy pool, swept later by the true-up
    # command) or when the fee consumes the whole charge — Stripe rejects a
    # zero/negative transfer_data.amount.
    org_for_charge = event.organization
    transfer_amount_cents = charge_cents - fee_cents
    use_destination = bool(
        org_for_charge.stripe_onboarding_complete
        and org_for_charge.stripe_account_id
        and transfer_amount_cents > 0
    )
    charge_flow = (
        StripeCheckoutSession.ChargeFlow.DESTINATION if use_destination
        else StripeCheckoutSession.ChargeFlow.PLATFORM
    )

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
            if use_destination:
                kw['transfer_data'] = {
                    'destination': org_for_charge.stripe_account_id,
                    'amount': transfer_amount_cents,
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
        charge_flow=charge_flow,
        promo_code_id=promo_code_id,
        discount_cents=discount_cents,
        fb_browser_data=fb_browser_data,
        tracking_link=tracking_link_obj,
        sms_opt_in=sms_opt_in,
        attribution=request.session.get(f'utm_{event.id}') or {},
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
        pi = event['data']['object']
        # SMS-wallet top-up PIs carry metadata.kind == 'sms_credits' and have no
        # StripeCheckoutSession row; route them to the wallet handler, not ticketing.
        pi_meta = _stripe_value(pi, 'metadata', {}) or {}
        if _stripe_value(pi_meta, 'kind') == 'sms_credits':
            _fulfill_sms_credit_payment_intent(pi)
        else:
            _fulfill_payment_intent(pi)
    elif event_type == 'payment_intent.payment_failed':
        _fail_payment_intent(event['data']['object'])
    elif event_type == 'checkout.session.completed':
        _fulfill_sms_credit_checkout(event['data']['object'])
    elif event_type == 'charge.refunded':
        _sync_charge_refund(event['data']['object'])

    return HttpResponse(status=200)


def _stripe_value(obj, key, default=None):
    """Read a field from either a live Stripe StripeObject or a plain dict.

    A StripeObject is NOT a dict and has no .get() — attribute access on a missing
    key raises (its __getattr__ turns `.get` into a key lookup). Tests pass plain
    dicts; the live webhook passes StripeObjects.
    """
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _save_org_card_from_pm(org_id, pm_id):
    """Persist a saved card's brand/last4/exp onto the Organization, best-effort.

    Wrapped by the caller so a card-save failure never blocks crediting. Mirrors the
    per-user save block in _fulfill_payment_intent, org-scoped."""
    if not (org_id and pm_id):
        return
    import stripe as stripe_lib
    from django.conf import settings as django_settings
    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
    pm = stripe_lib.PaymentMethod.retrieve(pm_id)
    card = getattr(pm, 'card', None) or {}
    Organization.objects.filter(id=org_id).update(
        stripe_pm_id=pm_id,
        stripe_pm_brand=card.get('brand', '') if isinstance(card, dict) else getattr(card, 'brand', ''),
        stripe_pm_last4=card.get('last4', '') if isinstance(card, dict) else getattr(card, 'last4', ''),
        stripe_pm_exp_month=card.get('exp_month') if isinstance(card, dict) else getattr(card, 'exp_month', None),
        stripe_pm_exp_year=card.get('exp_year') if isinstance(card, dict) else getattr(card, 'exp_year', None),
    )
    logger.info("Saved org card PaymentMethod %s for org %s", pm_id, org_id)


def _fulfill_sms_credit_checkout(session):
    """Credit an org's prepaid SMS wallet after a paid credits Checkout Session.

    Idempotent: the wallet service keys on the Checkout Session id, so webhook
    retries (or a success-page double-fire) can't double-credit. Ignores any
    Checkout Session that isn't one of ours (metadata.kind == 'sms_credits').

    Does NOT swallow exceptions — the webhook relies on a non-200 to make Stripe
    retry on transient failure (safe because crediting is idempotent). The
    success-page caller wraps this in its own try/except so the user's page
    still loads. Credits Stripe's settled amount_total as the source of truth so
    taxes/discounts can never desync cash-received from credits-granted.
    """
    from tickets.services.sms_credits import credit

    metadata = _stripe_value(session, 'metadata', {}) or {}
    if _stripe_value(metadata, 'kind') != 'sms_credits':
        return
    if _stripe_value(session, 'payment_status') != 'paid':
        return
    org_id = _stripe_value(metadata, 'organization_id')
    credit_cents = int(_stripe_value(session, 'amount_total')
                       or _stripe_value(metadata, 'credit_cents') or 0)
    if not org_id or credit_cents <= 0:
        return
    credit(org_id, credit_cents,
           stripe_checkout_session_id=_stripe_value(session, 'id'),
           description='Stripe top-up')


def _fulfill_sms_credit_payment_intent(payment_intent):
    """Handle a paid SMS-wallet top-up PaymentIntent (one-click off-session, or the
    PI behind a save-card Checkout).

    - ALWAYS save the card brand/last4 onto the org (best-effort; failures here must
      not block crediting).
    - Credit ONLY for the one-click flow. The save-card Checkout fires BOTH this event
      AND checkout.session.completed; those credit under different ids (pi.id vs
      session.id), so to avoid double-crediting, the Checkout flow is credited solely
      by _fulfill_sms_credit_checkout. The 'flow' metadata marker disambiguates.

    Fail-loud on credit() (lets Stripe retry transient DB errors — safe, idempotent).
    """
    from tickets.services.sms_credits import credit

    metadata = _stripe_value(payment_intent, 'metadata', {}) or {}
    if _stripe_value(metadata, 'kind') != 'sms_credits':
        return
    org_id = _stripe_value(metadata, 'organization_id')
    pm_id = _stripe_value(payment_intent, 'payment_method')
    try:
        _save_org_card_from_pm(org_id, pm_id)
    except Exception as e:
        logger.error("Failed to save org card for org %s: %s", org_id, e)

    if _stripe_value(metadata, 'flow') == 'checkout':
        return  # credited by _fulfill_sms_credit_checkout to avoid double-credit
    credit_cents = int(_stripe_value(payment_intent, 'amount_received')
                       or _stripe_value(metadata, 'credit_cents') or 0)
    if not org_id or credit_cents <= 0:
        return
    credit(org_id, credit_cents,
           stripe_checkout_session_id=_stripe_value(payment_intent, 'id'),
           description='Stripe top-up')


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
    elif event_type == 'charge.refunded':
        # Direct (in-person) charges live on connected accounts, so their
        # refund events arrive here, not on the platform endpoint.
        _sync_charge_refund(event['data']['object'])

    return HttpResponse(status=200)


# Stripe payout status → local Payout status. 'pending' matters for
# organizer-initiated payouts discovered via webhook before dispatch.
_STRIPE_PAYOUT_STATUS_MAP = {
    'pending':    Payout.Status.PENDING,
    'in_transit': Payout.Status.IN_TRANSIT,
    'paid':       Payout.Status.COMPLETED,
    'failed':     Payout.Status.FAILED,
    'canceled':   Payout.Status.FAILED,
}


def _apply_stripe_payout_status(payout, stripe_status, stripe_payout_id=None):
    """Apply a Stripe payout's status (and optionally its po_ id) to a local
    Payout row. The single translation point — webhook, initiate_payout, and
    recovery must never carry their own copies of this mapping."""
    update_fields = []
    if stripe_payout_id and not payout.stripe_payout_id:
        payout.stripe_payout_id = stripe_payout_id
        update_fields.append('stripe_payout_id')
    new_status = _STRIPE_PAYOUT_STATUS_MAP.get(stripe_status)
    if new_status and payout.status != new_status:
        payout.status = new_status
        update_fields.append('status')
    if update_fields:
        payout.save(update_fields=update_fields)
    return update_fields


def _handle_stripe_payout_event(event):
    """
    Handle Stripe payout lifecycle webhooks from connected accounts.

    Stripe fires these on the connected account, so the event includes an
    'account' field with the Express account ID. We use that to find the org
    and reconcile by Stripe payout ID first, then by our payout_id metadata
    (covers the race where payout.created beats initiate_payout's save).
    Anything still unmatched is an organizer-initiated payout (Express
    Dashboard) — record it as a new Payout row so history stays truthful.

    payout.created  → confirm/store stripe_payout_id (or record organizer payout)
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

    if not payout:
        # Organizer-initiated payout from the Express Dashboard: no local row
        # exists, so create one. get_or_create on the unique po_ id absorbs
        # duplicate webhook delivery.
        if not stripe_payout_id or stripe_payout_amount is None:
            logger.info(
                "Ignoring unmatched Stripe payout without id/amount (org %s): %s",
                org.id, stripe_payout_id,
            )
            return
        payout, created = Payout.objects.get_or_create(
            stripe_payout_id=stripe_payout_id,
            defaults={
                'organization': org,
                'amount': Decimal(str(stripe_payout_amount)) / 100,
                'status': _STRIPE_PAYOUT_STATUS_MAP.get(stripe_payout_status, Payout.Status.PENDING),
                'origin': Payout.Origin.STRIPE_DASHBOARD,
                'initiated_by': None,
                'notes': 'Initiated via Stripe',
            },
        )
        if created:
            logger.info(
                "Recorded organizer-initiated Stripe payout %s for org %s (%s)",
                stripe_payout_id, org.id, stripe_payout_status,
            )
            _bust_connected_balance_cache(org)
            return
        # Lost a race with a concurrent delivery — fall through to status sync.

    update_fields = _apply_stripe_payout_status(payout, stripe_payout_status, stripe_payout_id)
    if update_fields:
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
        link_customer_to_buyer(customer, email)
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
            attribution=session_obj.attribution or {},
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
        # Loyalty points: swallow failures — an order must never fail because
        # points hiccuped. The service's internal atomic() is a savepoint, and
        # misses are self-healing via the idempotent backfill sweep.
        try:
            award_points_for_order(order)
        except Exception:
            logger.exception("Points award failed for order %s", order.id)

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

        if order.attribution:
            _recompute_utm_attribution_for_event(org, event)

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
            session_updates = {}
            bt = charge.balance_transaction
            if bt and getattr(bt, 'available_on', None):
                import datetime as _dt
                session_updates['available_on'] = _dt.datetime.fromtimestamp(
                    bt.available_on, tz=_dt.timezone.utc,
                )
            # Destination charges carry a transfer (tr_xxx). Trust the charge,
            # not the session flag: a PENDING session created before the
            # destination-charge deploy fulfills here with no transfer and
            # correctly stays in the legacy platform pool.
            transfer_id = getattr(charge, 'transfer', None)
            if transfer_id and isinstance(transfer_id, str):
                transfer = stripe_lib_bt.Transfer.retrieve(transfer_id)
                session_updates.update(
                    stripe_transfer_id=transfer_id,
                    transfer_cents=int(transfer.amount),
                    charge_flow=StripeCheckoutSession.ChargeFlow.DESTINATION,
                )
            if session_updates:
                StripeCheckoutSession.objects.filter(stripe_session_id=pi_id).update(**session_updates)
                logger.info("Recorded settlement/transfer state for PaymentIntent %s: %s", pi_id, session_updates)
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


def _find_session_for_payment_intent(pi_id):
    """Locate the StripeCheckoutSession for a charge's PaymentIntent id.

    Direct lookup first: PI-flow rows store the pi_… id in both id fields.
    Legacy rows from the pre-April-2026 Stripe Checkout flow store only the
    cs_… Checkout Session id and have a blank stripe_payment_intent_id, so
    fall back to resolving the Checkout Session id from Stripe and matching
    on that. Read-only; returns None when the charge isn't ours (e.g. SMS
    top-ups, which have no session row).
    """
    session = StripeCheckoutSession.objects.filter(
        Q(stripe_session_id=pi_id) | Q(stripe_payment_intent_id=pi_id)
    ).select_related('ticket_order', 'organization').first()
    if session is not None:
        return session

    import stripe as stripe_lib
    from django.conf import settings as django_settings
    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
    try:
        listing = stripe_lib.checkout.Session.list(payment_intent=pi_id, limit=1)
    except stripe_lib.error.StripeError:
        logger.exception("Could not resolve Checkout Session for PaymentIntent %s", pi_id)
        return None
    data = listing.get('data', []) if isinstance(listing, dict) else getattr(listing, 'data', [])
    if not data:
        return None
    cs_id = _stripe_value(data[0], 'id')
    if not cs_id:
        return None
    return StripeCheckoutSession.objects.filter(
        stripe_session_id=cs_id,
    ).select_related('ticket_order', 'organization').first()


def _reverse_transfer_for_refund(charge, session=None):
    """Claw back the organizer's share of a refunded destination charge.

    Stripe is the authority on both sides of the math: the cumulative
    ``charge.amount_refunded`` sets the target, and ``Transfer.amount_reversed``
    says how much has already been clawed back — so a stale or missing local
    row can never cause an over-reversal, and webhook retries replay the same
    cumulative target through the same idempotency key as no-ops.

    Works without a session row (event hard-deletes CASCADE sessions away;
    the refund webhook may also beat fulfillment's transfer capture): the
    transfer id comes from the charge payload itself. Charges with no
    transfer (platform, direct, SMS top-ups) no-op.

    Refund semantics: a partial refund of R reverses exactly R (organizer
    bears it 1:1) until the transfer is exhausted; a full refund reverses the
    whole transfer, so the platform funds the fee portion of the buyer's
    refund — the fee-waiver-on-full-refund economics the ledger math in
    _aggregate_session_cents assumes.

    Failures log loudly and return — order/session state sync must proceed;
    the charge.refunded retry or backfill_refund_state converges later.
    """
    transfer_id = None
    if session is not None and session.stripe_transfer_id:
        transfer_id = session.stripe_transfer_id
    if not transfer_id:
        payload_transfer = _stripe_value(charge, 'transfer')
        if payload_transfer and isinstance(payload_transfer, str):
            transfer_id = payload_transfer
    if not transfer_id:
        return

    refunded_cents = int(_stripe_value(charge, 'amount_refunded') or 0)
    if refunded_cents <= 0:
        return

    import stripe as stripe_lib
    from django.conf import settings as django_settings
    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
    try:
        transfer = stripe_lib.Transfer.retrieve(transfer_id)
        target_cents = min(refunded_cents, int(transfer.amount))
        delta_cents = target_cents - int(transfer.amount_reversed or 0)
        if delta_cents > 0:
            stripe_lib.Transfer.create_reversal(
                transfer_id,
                amount=delta_cents,
                idempotency_key=f'trrev-{transfer_id}-{target_cents}',
                metadata={
                    'reason': 'refund_clawback',
                    'payment_intent': str(_stripe_value(charge, 'payment_intent') or ''),
                },
            )
            logger.info(
                "Reversed %s cents of transfer %s for refund (cumulative target %s)",
                delta_cents, transfer_id, target_cents,
            )
    except stripe_lib.error.StripeError:
        logger.exception("Transfer reversal failed for %s — state sync continues, backfill will converge", transfer_id)
        return

    if session is not None:
        session_updates = {'transfer_reversed_cents': target_cents}
        if not session.stripe_transfer_id:
            # Recovered from the charge payload before fulfillment stored it.
            session_updates.update(
                stripe_transfer_id=transfer_id,
                transfer_cents=int(transfer.amount),
                charge_flow=StripeCheckoutSession.ChargeFlow.DESTINATION,
            )
        StripeCheckoutSession.objects.filter(pk=session.pk).update(**session_updates)


def _sync_charge_refund(charge):
    """Sync a Stripe refund (any origin, incl. dashboard) into the DB.

    Without this, refunds issued from the Stripe dashboard reduce the platform
    balance while the DB keeps counting the session as COMPLETED — the Finance
    page figures drift apart.

    Idempotent: charge.amount_refunded is CUMULATIVE, so retries and the echo
    webhook after an app-initiated refund (refund_order writes the same state
    first) become no-ops. Inventory restore and waitlist notifications fire
    only on the transition into REFUNDED.

    The transfer clawback runs BEFORE the state no-op guard: the echo webhook
    after an app-initiated refund is exactly when the reversal must happen
    (refund_order only writes local state), and the reversal carries its own
    Stripe-side idempotency.
    """
    pi_id = _stripe_value(charge, 'payment_intent')
    if not pi_id:
        return
    session = _find_session_for_payment_intent(pi_id)
    if session is None:
        # SMS top-up, a charge that isn't ours, or a session lost to an event
        # hard-delete. The clawback must still happen for destination charges —
        # everything it needs lives on Stripe (D1 session-less fallback).
        _reverse_transfer_for_refund(charge, session=None)
        return
    if not session.stripe_payment_intent_id:
        # Legacy Checkout-flow row matched via the cs_… fallback: persist the
        # pi id so future lookups (webhook, refund_order guards) are direct.
        session.stripe_payment_intent_id = pi_id
        session.save(update_fields=['stripe_payment_intent_id'])
    if session.status not in (
        StripeCheckoutSession.Status.COMPLETED,
        StripeCheckoutSession.Status.PARTIALLY_REFUNDED,
        StripeCheckoutSession.Status.REFUNDED,
    ):
        return

    _reverse_transfer_for_refund(charge, session=session)

    order = session.ticket_order
    refunded_total = Decimal(int(_stripe_value(charge, 'amount_refunded') or 0)) / 100
    fully_refunded = bool(_stripe_value(charge, 'refunded'))
    target_status = (
        StripeCheckoutSession.Status.REFUNDED if fully_refunded
        else StripeCheckoutSession.Status.PARTIALLY_REFUNDED
    )

    # No-op guard: covers webhook retries and app-initiated refunds whose
    # state refund_order already wrote before this event arrived.
    if session.status == target_status and (
        order is None or order.refunded_amount >= refunded_total
    ):
        return

    became_full = fully_refunded and session.status != StripeCheckoutSession.Status.REFUNDED
    with transaction.atomic():
        if order is not None:
            order.refunded_amount = max(order.refunded_amount, refunded_total)
            update_fields = ['refunded_amount']
            if fully_refunded and order.refunded_at is None:
                order.refunded_at = django_tz.now()
                update_fields.append('refunded_at')
            order.save(update_fields=update_fields)

        session.status = target_status
        session.save(update_fields=['status'])

        if became_full:
            for item in session.line_items_snapshot:
                tt_id = item.get('saleable_ticket_type_id')
                qty = item.get('quantity', 0)
                if tt_id and qty:
                    SaleableTicketType.objects.filter(id=tt_id).update(
                        quantity_sold=Greatest(F('quantity_sold') - qty, Value(0))
                    )

        if order is not None and order.customer_id:
            try:
                order.customer.update_lifetime_value()
            except Customer.DoesNotExist:
                pass

        _invalidate_event_list_cache(session.organization)
        _invalidate_marketing_cache(session.organization)

    # Bust the cached balances so the Finance page reflects the refund (and
    # any clawback) immediately instead of after the 60s TTL.
    django_cache.delete(_STRIPE_PLATFORM_AVAILABLE_CACHE_KEY)
    _bust_connected_balance_cache(session.organization)
    logger.info(
        "Synced charge.refunded for session %s (refunded=%s, full=%s)",
        session.stripe_session_id, refunded_total, fully_refunded,
    )

    if became_full:
        from tickets.tasks import notify_next_waitlist_entry
        for item in session.line_items_snapshot:
            tt_id = item.get('saleable_ticket_type_id')
            qty = item.get('quantity', 0)
            if tt_id and qty:
                tt = SaleableTicketType.objects.filter(id=tt_id, waitlist_enabled=True).first()
                if tt:
                    notify_next_waitlist_entry.delay(tt_id)


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


# ---------------------------------------------------------------------------
# Public "subscribe to an organizer" flow (accountless Customer + SMS consent).
# Design doc: audience-subscribe-page. Email-keyed identity (merges with
# checkout/CSV), provable SMSConsentRecord ledger, suppression-aware,
# fail-closed rate limit, single minimal template across all steps.
# ---------------------------------------------------------------------------

SUBSCRIBE_OTP_PURPOSE = 'subscribe'


def _subscribe_client_ip(request):
    """Proxy-safe client IP (Render sits behind a proxy → REMOTE_ADDR is the proxy).
    Returns '' when the resolved value isn't a valid IP (prevents inet save errors)."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', ''))
    ip = (xff or '').split(',')[0].strip()
    try:
        from django.core.validators import validate_ipv46_address
        validate_ipv46_address(ip)
        return ip
    except Exception:
        return ''


def _subscribe_consent_text(org):
    """The exact SMS-consent disclosure shown on the page AND frozen on the record."""
    return (
        f"I agree to receive recurring automated marketing text messages from "
        f"{org.name} at the number provided. Consent is not a condition of purchase. "
        f"Message frequency varies. Msg & data rates may apply. Reply STOP to opt out, "
        f"HELP for help."
    )


def _subscribe_rate_ok(org, ip, phone):
    """Per-IP AND per-phone limiter for the OTP send. FAIL CLOSED (design F8): a
    public endpoint that spends real money on each send must not fail open, so a
    cache-backend outage denies rather than allows."""
    try:
        ip_key = f"subscribe_rl_ip:{org.id}:{ip}"
        ph_key = f"subscribe_rl_ph:{org.id}:{phone}"
        ip_n = django_cache.get(ip_key, 0) or 0
        ph_n = django_cache.get(ph_key, 0) or 0
        if ip_n >= 15 or ph_n >= 3:
            return False
        django_cache.set(ip_key, ip_n + 1, 3600)
        django_cache.set(ph_key, ph_n + 1, 3600)
        return True
    except Exception:
        return False  # fail closed


def _subscribe_render(request, org, step, **extra):
    ctx = {'org': org, 'step': step, 'consent_text': _subscribe_consent_text(org)}
    ctx.update(extra)
    return render(request, 'tickets/subscribe.html', ctx)


def subscribe_view(request, org_slug):
    """Public. Step 1: capture name/email/phone + SMS consent, send the OTP."""
    from .sms import otp_start
    org = get_object_or_404(Organization, slug=org_slug)
    if not org.sms_marketing_enabled:
        return _subscribe_render(request, org, step='unavailable')

    if request.method != 'POST':
        return _subscribe_render(request, org, step='form', form=SubscribeForm())

    form = SubscribeForm(request.POST)
    if not form.is_valid():
        return _subscribe_render(request, org, step='form', form=form)

    name = form.cleaned_data['name']
    email = form.cleaned_data['email']
    phone = form.cleaned_data['phone']
    ip = _subscribe_client_ip(request)

    if not _subscribe_rate_ok(org, ip, phone):
        return _subscribe_render(
            request, org, step='form', form=form,
            send_error="You've tried a few times. Please wait a bit and try again.",
        )

    if not otp_start(request, phone, purpose=SUBSCRIBE_OTP_PURPOSE):
        # start swallows all errors to False (bad number OR Twilio down) — generic retry.
        return _subscribe_render(
            request, org, step='form', form=form,
            send_error="We couldn't send a code to that number. Check it and try again.",
        )

    # OTP sent — only now write the pending record (keeps failed sends out of the ledger).
    record = SMSConsentRecord.objects.create(
        organization=org, phone=phone, email=email, name=name,
        consent_given=True, consent_text=_subscribe_consent_text(org),
        consent_url=request.path, ip_address=ip or None,
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:2000],
        source=SMSConsentRecord.Source.SUBSCRIBE_PAGE,
    )
    request.session['subscribe_flow'] = {
        'org_id': str(org.id), 'record_id': str(record.id), 'phone': phone,
    }
    return _subscribe_render(request, org, step='code', masked_phone=phone[-4:])


def subscribe_verify_view(request, org_slug):
    """Public. Step 2: verify the OTP → upsert Customer (email-keyed), mark consent
    verified, reconcile suppression, show success."""
    from .sms import otp_start, otp_check, otp_clear, resolve_sms_sender_number
    from django.db import transaction
    org = get_object_or_404(Organization, slug=org_slug)

    flow = request.session.get('subscribe_flow')
    if not flow or flow.get('org_id') != str(org.id):
        return _subscribe_render(request, org, step='form', form=SubscribeForm(),
                                 send_error='Your session expired. Please start again.')
    masked = flow['phone'][-4:]

    if request.method != 'POST':
        return _subscribe_render(request, org, step='code', masked_phone=masked)

    if request.POST.get('resend'):
        # Re-send the code — same fail-closed limiter so resend can't be abused.
        if not _subscribe_rate_ok(org, _subscribe_client_ip(request), flow['phone']):
            return _subscribe_render(request, org, step='code', masked_phone=masked,
                                     code_error='Too many code requests. Please wait a bit.')
        otp_start(request, flow['phone'], purpose=SUBSCRIBE_OTP_PURPOSE)
        return _subscribe_render(request, org, step='code', masked_phone=masked, resent=True)

    form = OTPVerificationForm(request.POST)
    if not form.is_valid():
        return _subscribe_render(request, org, step='code', masked_phone=masked,
                                 code_error='Enter the 6-digit code we texted you.')

    ok, phone = otp_check(request, form.cleaned_data['otp_code'], purpose=SUBSCRIBE_OTP_PURPOSE)
    if not ok or not phone:
        return _subscribe_render(request, org, step='code', masked_phone=masked,
                                 code_error="That code didn't match. Try again or resend.")

    record = SMSConsentRecord.objects.filter(id=flow['record_id'], organization=org).first()
    if record is None:
        return _subscribe_render(request, org, step='form', form=SubscribeForm(),
                                 send_error='Your session expired. Please start again.')

    with transaction.atomic():
        # Email-keyed identity — merges with checkout/CSV (design decision A).
        customer, _created = Customer.objects.get_or_create(
            organization=org, email=record.email,
            defaults={'name': record.name, 'phone': phone},
        )
        if not customer.phone:
            customer.phone = phone
        customer.sms_opt_in = True
        customer.sms_opt_in_date = django_tz.now()
        customer.save(update_fields=['phone', 'sms_opt_in', 'sms_opt_in_date', 'updated_at'])

        # Suppression reconciliation.
        # Per-org unsubscribe: fresh express consent overrides it → delete.
        PhoneSuppression.objects.filter(
            phone=phone, organization=org,
            reason__in=[PhoneSuppression.Reason.MANUAL, PhoneSuppression.Reason.BOUNCE],
        ).delete()
        # Global Twilio STOP: cannot clear server-side → consented but unreachable.
        pending_start = PhoneSuppression.objects.filter(
            phone=phone, organization__isnull=True,
        ).exists()

        record.customer = customer
        record.verified_at = django_tz.now()
        record.pending_start = pending_start
        record.save(update_fields=['customer', 'verified_at', 'pending_start', 'updated_at'])

    otp_clear(request, purpose=SUBSCRIBE_OTP_PURPOSE)
    request.session.pop('subscribe_flow', None)

    start_number = resolve_sms_sender_number() if pending_start else ''
    return _subscribe_render(request, org, step='done',
                             pending_start=pending_start, start_number=start_number)


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
    ticket_qrs = []
    if not order.refunded_at:
        ticket_qrs = [
            {'ticket': t, 'qr_b64': generate_qr_b64(ticket_qr_payload(t))}
            for t in order.tickets.all()
        ]
    return render(request, 'tickets/ticket_detail.html', {
        'order': order,
        'ticket_qrs': ticket_qrs,
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

# Sessions that still hold organizer revenue. Fully REFUNDED sessions are
# excluded: under the per-session clamp in _aggregate_session_cents they would
# contribute exactly 0 (refunded == amount), so exclusion is pure query
# efficiency and preserves the existing fee-waiver semantic on full refunds.
_BALANCE_SESSION_STATUSES = (
    StripeCheckoutSession.Status.COMPLETED,
    StripeCheckoutSession.Status.PARTIALLY_REFUNDED,
)


def _aggregate_session_cents(sessions):
    """Return (total_charged_cents, total_fees_cents, refund_adjustment_cents)
    over a StripeCheckoutSession queryset (caller pre-filters org/status/settlement).

    refund_adjustment is per-session min(refunded, max(0, amount - fee)), so
    (charged - fees - adjustment) == sum of max(0, amount - fee - refunded):
    a partial refund reduces the organizer net by exactly the refunded amount,
    and a session fully refunded through successive partials nets to 0 (the
    platform fee is waived, matching the full-refund semantic).

    The clamp runs in Python with exact Decimal math — refunded_amount is a
    Decimal in dollars on the related order while session amounts are integer
    cents, and a Cast(... * 100) in SQL float-truncates on SQLite.

    Direct (in-person) sessions intentionally get the same treatment with no
    fee-waiver special case: Stripe keeps the application fee when an
    in-person charge is refunded from the organizer's dashboard, so a fully
    refunded direct session displaying as 0 is the documented policy.
    """
    agg = sessions.aggregate(
        total_charged=Coalesce(Sum('amount_total_cents'), 0),
        total_fees=Coalesce(Sum('platform_fee_cents'), 0),
    )
    adjustment = 0
    partial_rows = sessions.filter(
        status=StripeCheckoutSession.Status.PARTIALLY_REFUNDED,
    ).values_list(
        'amount_total_cents', 'platform_fee_cents', 'ticket_order__refunded_amount',
    )
    for amount_cents, fee_cents, refunded in partial_rows:
        refunded_cents = int(((refunded or Decimal('0')) * 100).to_integral_value())
        adjustment += min(refunded_cents, max(0, amount_cents - fee_cents))
    return agg['total_charged'], agg['total_fees'], adjustment


def _compute_available_balance(org):
    """Return (stripe_revenue, platform_fees, paid_out, available_balance) for the given org.

    stripe_revenue is NET OF REFUNDS: partially refunded sessions count their
    remaining (unrefunded) amount, fully refunded sessions count nothing.
    """
    sessions = StripeCheckoutSession.objects.filter(
        organization=org, status__in=_BALANCE_SESSION_STATUSES,
    )
    charged_cents, fee_cents, refund_adj_cents = _aggregate_session_cents(sessions)
    stripe_revenue = Decimal(charged_cents - refund_adj_cents) / 100
    platform_fees = Decimal(fee_cents) / 100

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


def _compute_legacy_settled_balance(org, clamp=True):
    """
    Return the settled, still-platform-held organizer balance, in dollars.

    LEGACY POOL ONLY: counts platform-flow sessions (pre-destination-charge
    money that landed on the platform account) whose funds have settled —
    explicit available_on <= now, or available_on=NULL for payments that
    pre-date that field. Partially refunded sessions count net of their
    refunded amount. Subtracts the payouts that drew platform funds
    (origin legacy_transfer/migration), so the migrate_legacy_balances
    true-up can re-run safely: each run moves exactly the not-yet-moved
    remainder.

    Destination/direct sessions never enter this number — their money lives
    in the connected account, whose Stripe balance is the source of truth.
    """
    from django.utils import timezone as django_tz
    now = django_tz.now()
    settled_sessions = StripeCheckoutSession.objects.filter(
        organization=org,
        status__in=_BALANCE_SESSION_STATUSES,
        charge_flow=StripeCheckoutSession.ChargeFlow.PLATFORM,
    ).filter(
        Q(available_on__lte=now) | Q(available_on__isnull=True)
    )

    charged_cents, fee_cents, refund_adj_cents = _aggregate_session_cents(settled_sessions)
    settled_organizer_cents = charged_cents - fee_cents - refund_adj_cents

    paid_out = Payout.objects.filter(
        organization=org,
        origin__in=[Payout.Origin.LEGACY_TRANSFER, Payout.Origin.MIGRATION],
    ).exclude(status=Payout.Status.FAILED).aggregate(
        total=Coalesce(Sum('amount'), Decimal('0.00'))
    )['total']

    settled = Decimal(str(settled_organizer_cents)) / 100 - paid_out
    if not clamp:
        # Raw value for the true-up dry-run: negative means a legacy refund
        # landed after its funds were already trued-up (platform absorbed it).
        return settled
    return max(Decimal('0.00'), settled)


_CONNECTED_BALANCE_CACHE_TTL = 60


def _connected_balance_cache_key(org):
    return f'stripe_connected_balance:{org.pk}'


def _bust_connected_balance_cache(org):
    django_cache.delete(_connected_balance_cache_key(org))


def _get_connected_balance_cents(org, use_cache=True):
    """
    Return (available_cents, pending_cents) for the org's connected Stripe
    account, or (None, None) on error / no account. This is the source of
    truth for "Ready to Withdraw" (available) and "Settling" (pending).
    """
    if not org.stripe_account_id:
        return (None, None)
    cache_key = _connected_balance_cache_key(org)
    if use_cache:
        cached = django_cache.get(cache_key)
        if cached is not None:
            return tuple(cached)

    import stripe as stripe_lib
    from django.conf import settings as django_settings
    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
    try:
        balance = stripe_lib.Balance.retrieve(stripe_account=org.stripe_account_id)
        currency = django_settings.STRIPE_CURRENCY.lower()
        available = sum(
            entry.amount for entry in balance.available
            if entry.currency.lower() == currency
        )
        pending = sum(
            entry.amount for entry in balance.pending
            if entry.currency.lower() == currency
        )
        django_cache.set(cache_key, (available, pending), _CONNECTED_BALANCE_CACHE_TTL)
        return (available, pending)
    except Exception:
        logger.exception("Could not retrieve connected Stripe balance for org %s", org.id)
        return (None, None)


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
        # The connected account balance IS the organizer's money now:
        # available = withdrawable, pending = settling. Clamp at zero for
        # display — available can go negative after a refund clawback that
        # follows a withdrawal. On Stripe API failure render unknowns (None)
        # rather than misrepresenting platform-pool figures as withdrawable.
        available_cents, pending_cents = _get_connected_balance_cents(org)
        if available_cents is not None:
            stripe_available = max(Decimal('0.00'), Decimal(str(available_cents)) / 100)
            settling_balance = max(Decimal('0.00'), Decimal(str(pending_cents)) / 100)

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
                # Manual schedule from day one so the balance accumulates for
                # organizer-initiated withdrawals instead of auto-sweeping.
                try:
                    _ensure_manual_payout_schedule(stripe_lib, org.stripe_account_id)
                except stripe_lib.error.StripeError:
                    logger.exception("Could not set manual payout schedule for %s", org.stripe_account_id)
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

    # The connected account balance is the only gate that matters now: the
    # organizer's money already lives there (destination charges + true-up).
    available_cents, _pending_cents = _get_connected_balance_cents(org, use_cache=False)
    if available_cents is None:
        messages.error(request, 'Could not check your Stripe balance. Please try again.')
        return redirect('tickets:finance_overview')
    available = Decimal(str(available_cents)) / 100
    if amount > available:
        messages.error(
            request,
            f'Payout amount exceeds your available balance (${max(available, Decimal("0.00")):.2f}). '
            f'Funds from recent sales typically settle within 2\u20137 business days.',
        )
        return redirect('tickets:finance_overview')

    notes = request.POST.get('notes', '').strip()[:500]
    payout = Payout.objects.create(
        organization=org,
        amount=amount,
        status=Payout.Status.PENDING,
        origin=Payout.Origin.CUE,
        initiated_by=request.user,
        notes=notes,
    )

    try:
        _ensure_manual_payout_schedule(stripe_lib, org.stripe_account_id)
        # No Transfer step anymore — the money is already in the connected
        # account. The idempotency key makes a timeout retry return the
        # original payout instead of withdrawing twice.
        stripe_payout = stripe_lib.Payout.create(
            amount=int(amount * 100),
            currency=django_settings.STRIPE_CURRENCY,
            metadata={'org_id': str(org.id), 'payout_id': str(payout.id)},
            stripe_account=org.stripe_account_id,
            idempotency_key=f'payout-{payout.id}',
        )
        _apply_stripe_payout_status(payout, getattr(stripe_payout, 'status', None), stripe_payout.id)
        messages.success(request, f'Payout of ${amount:.2f} processing. Funds will arrive in 1–5 business days.')
    except stripe_lib.error.StripeError as e:
        payout.status = Payout.Status.FAILED
        error_note = f' [Stripe error: {str(e)[:400]}]'
        payout.notes = (payout.notes + error_note)[:500]
        payout.save(update_fields=['status', 'notes'])
        messages.error(request, f'Payout failed: {getattr(e, "user_message", None) or str(e)}')

    _bust_connected_balance_cache(org)
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
                idempotency_key=f'payout-{payout.id}',
            )
            _apply_stripe_payout_status(payout, getattr(stripe_payout, 'status', None), stripe_payout.id)
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

    external_uploads = ExternalSurveyUpload.objects.filter(
        organization=org
    ).order_by('-uploaded_at')

    # Native Cue surveys are stored per-response; group them by event so each
    # event with responses becomes a single row in the unified list.
    native_rows = (
        SurveyResponse.objects.filter(organization=org)
        .values('event_id', 'event__name')
        .annotate(response_count=Count('id'), last_response=Max('submitted_at'))
        .order_by('-last_response')
    )

    # Native and external surveys share one interface, distinguished by a source
    # label. Build a combined, newest-first list the template renders as one table.
    sources = []
    for upload in external_uploads:
        sources.append({
            'kind': 'external',
            'label': 'External upload',
            'name': upload.filename,
            'date': upload.uploaded_at,
            'response_count': upload.row_count,
            'status': upload.status,
            'upload_id': upload.id,
        })
    for row in native_rows:
        sources.append({
            'kind': 'native',
            'label': 'Cue survey',
            'name': row['event__name'] or 'Survey',
            'date': row['last_response'],
            'response_count': row['response_count'],
            'status': 'active',
            'event_id': row['event_id'],
        })
    sources.sort(key=lambda s: s['date'], reverse=True)

    has_external = any(s['kind'] == 'external' for s in sources)

    return render(request, 'tickets/survey_upload_list.html', {
        'sources': sources,
        'has_external': has_external,
    })


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
    market_filter = request.GET.get('market', '').strip() or None
    city_filter = request.GET.get('city', '').strip() or None

    def _parse_event_date(raw):
        raw = (raw or '').strip()
        try:
            return datetime.strptime(raw, '%Y-%m-%d').date() if raw else None
        except ValueError:
            return None

    event_from_str = request.GET.get('event_from', '').strip()
    event_to_str = request.GET.get('event_to', '').strip()
    event_from = _parse_event_date(event_from_str)
    event_to = _parse_event_date(event_to_str)

    from .services.external_survey.analytics import ExternalSurveyAnalytics
    from .services.external_survey.analytics import NO_MARKET_VALUE
    stats = ExternalSurveyAnalytics(organization=org).calculate(
        market=market_filter, city=city_filter, event_from=event_from, event_to=event_to,
    )

    feedback_qs = ExternalSurveyResponse.objects.filter(organization=org)
    if market_filter == NO_MARKET_VALUE:
        feedback_qs = feedback_qs.filter(event__market__isnull=True)
    elif market_filter:
        try:
            _uuid.UUID(str(market_filter))
        except (TypeError, ValueError):
            feedback_qs = feedback_qs.none()
        else:
            feedback_qs = feedback_qs.filter(event__market_id=market_filter)
    elif city_filter:
        feedback_qs = feedback_qs.filter(Q(event__market__name=city_filter) | Q(event__market__geography_value=city_filter))
    if event_from:
        feedback_qs = feedback_qs.filter(event__start_date__gte=event_from)
    if event_to:
        feedback_qs = feedback_qs.filter(event__start_date__lte=event_to)
    feedback_qs = feedback_qs.select_related('event', 'event__venue', 'event__market').order_by('-responded_at')
    paginator = Paginator(feedback_qs, 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    market_rows = (
        ExternalSurveyResponse.objects.filter(organization=org, event__isnull=False)
        .values('event__market_id', 'event__market__name')
        .annotate(total=Count('id'))
        .order_by('event__market__name')
    )
    distinct_cities = [
        {
            'value': str(row['event__market_id']) if row['event__market_id'] else NO_MARKET_VALUE,
            'label': row['event__market__name'] or NO_MARKET_LABEL,
        }
        for row in market_rows
    ]
    selected_market_label = ''
    for row in distinct_cities:
        if row['value'] == market_filter:
            selected_market_label = row['label']
            break
    if city_filter and not selected_market_label:
        selected_market_label = city_filter

    # Subscriptions that are syncing rows but have no field_map saved — those
    # rows show up in the DB but contribute nothing to the totals here because
    # nps_score / overall_rating / text_feedback stay NULL without a mapping.
    unmapped_subscriptions = list(
        TypeformFormSubscription.objects.filter(
            organization=org, is_active=True, field_map__isnull=True,
        ).annotate(
            response_count=Count(
                'upload__responses',
                filter=~Q(upload__responses__typeform_response_id=''),
            ),
        ).filter(response_count__gt=0)
    )

    return render(request, 'tickets/survey_analytics.html', {
        'stats': stats,
        'city_filter': selected_market_label,
        'market_filter': market_filter or '',
        'event_from': event_from_str,
        'event_to': event_to_str,
        'distinct_cities': distinct_cities,
        'no_market_value': NO_MARKET_VALUE,
        'page_obj': page_obj,
        'rating_labels': [r['overall_rating'] for r in stats['rating_breakdown']],
        'rating_counts': [r['count'] for r in stats['rating_breakdown']],
        'unmapped_subscriptions': unmapped_subscriptions,
    })


# ── Typeform integration ────────────────────────────────────────────────────







def _typeform_webhook_url(request, sub_id) -> str:
    base = (
        getattr(settings, 'TYPEFORM_WEBHOOK_BASE_URL', '')
        or getattr(settings, 'SITE_URL', '')
        or request.build_absolute_uri('/')
    ).rstrip('/')
    path = reverse('tickets:typeform_webhook', args=[sub_id])
    return f'{base}{path}'


def _is_publicly_reachable(url: str) -> bool:
    """Typeform rejects webhooks pointed at localhost or private IPs."""
    lowered = url.lower()
    for needle in ('localhost', '127.0.0.1', '0.0.0.0', '://10.', '://192.168.', '://172.'):
        if needle in lowered:
            return False
    return True


def _refresh_subscription_questions(subscription, definition):
    """Persist the form's leaf questions on the subscription. Cheap and idempotent;
    call whenever we already have a fresh `definition` in hand (mapping page GET,
    form-picker POST, etc.). Skips the write when nothing changed.
    """
    from .services.typeform.field_mapping import snapshot_questions

    questions = snapshot_questions(definition or {})
    if subscription.questions != questions:
        subscription.questions = questions
        subscription.save(update_fields=['questions'])












def _survey_candidate_dict(response, candidate, subscription=None):
    """Shape one (ExternalSurveyResponse, ResponseCandidate) pair for the modal JSON.

    When `subscription` is provided, titles missing from raw_answers are backfilled
    from `subscription.questions` so the UI never renders "(untitled)".
    """
    from .services.typeform.helpers import enrich_answers_with_titles

    confidence = float(candidate.confidence or 0)
    pct = int(round(confidence * 100))
    if confidence >= 0.7:
        confidence_class = 'bg-success'
    elif confidence >= 0.3:
        confidence_class = 'bg-warning'
    else:
        confidence_class = 'bg-secondary'
    raw = response.raw_answers or []
    if subscription is not None:
        raw = enrich_answers_with_titles(raw, subscription.questions)
    preview = []
    # Cap at 4 so the JS formatter has enough material to pick a primary line and
    # up to two secondaries; rendering decides which to show.
    for ans in raw[:4]:
        if not isinstance(ans, dict):
            continue
        preview.append({
            'title': str(ans.get('title') or '')[:120],
            'type': str(ans.get('type') or ''),
            'value': ans.get('value'),
        })
    # Full answer set for the expandable per-row detail panel (every Q/A pair).
    answers = []
    for ans in raw:
        if not isinstance(ans, dict):
            continue
        answers.append({
            'title': str(ans.get('title') or '')[:200],
            'type': str(ans.get('type') or ''),
            'value': ans.get('value'),
        })
    return {
        'response_id': str(response.id),
        'responded_at': response.responded_at.isoformat() if response.responded_at else None,
        'preview_answers': preview,
        'answers': answers,
        'confidence': confidence,
        'confidence_pct': pct,
        'confidence_class': confidence_class,
        'reasoning': str(candidate.reasoning or ''),
    }


@login_required
@require_org
def event_survey_match(request, event_id):
    """Fetch + LLM-rank unlinked Typeform responses for one event. Returns JSON for the modal."""
    from .services.typeform.client import TypeformAPIError, TypeformClient
    from .services.typeform.event_matcher import (
        CONFIDENCE_THRESHOLD,
        EventSurveyMatcher,
        EVENT_WINDOW_DAYS,
    )
    from .services.typeform.ingest import ingest_response

    org = get_organization(request)
    event = get_object_or_404(
        Event.objects.filter(organization=org).select_related('venue'),
        id=event_id,
    )

    subs_qs = TypeformFormSubscription.objects.filter(organization=org, is_active=True)
    sub_id = (request.GET.get('sub_id') or '').strip()
    if sub_id:
        subs_qs = subs_qs.filter(id=sub_id)
    subscriptions = list(subs_qs)

    # Refresh from Typeform first (mirror Mailchimp/SlickText fetch-fresh UX). Best-effort:
    # a provider hiccup falls back to ranking what's already in the DB.
    if org.typeform_access_token and subscriptions:
        client = TypeformClient(access_token=org.typeform_access_token)
        for sub in subscriptions:
            try:
                after_token: str | None = None
                while True:
                    payload = client.list_responses(
                        form_id=sub.form_id,
                        since=sub.last_synced_at,
                        after=after_token,
                    )
                    items = payload.get('items') or []
                    for item in items:
                        ingest_response(sub, item)
                    page_count = payload.get('page_count') or 0
                    current_page = payload.get('page') or 1
                    if current_page >= page_count or not items:
                        break
                    after_token = items[-1].get('token') or items[-1].get('response_id')
                    if not after_token:
                        break
                sub.last_synced_at = django_tz.now()
                sub.last_sync_error = ''
                sub.save(update_fields=['last_synced_at', 'last_sync_error'])
            except TypeformAPIError as exc:
                sub.last_sync_error = str(exc)
                sub.save(update_fields=['last_sync_error'])
                logger.warning('Typeform fetch failed during match for sub %s: %s', sub.id, exc)

    # Pull unlinked candidates from the local DB (within the event window).
    upload_ids = [sub.upload_id for sub in subscriptions if sub.upload_id]
    window_start = event.start_date - timedelta(days=EVENT_WINDOW_DAYS)
    window_end = event.start_date + timedelta(days=EVENT_WINDOW_DAYS)
    candidates_qs = (
        ExternalSurveyResponse.objects
        .filter(
            organization=org,
            event__isnull=True,
            responded_at__date__range=(window_start, window_end),
        )
        .exclude(typeform_response_id='')
    )
    if upload_ids:
        candidates_qs = candidates_qs.filter(upload_id__in=upload_ids)
    candidate_responses = list(candidates_qs.order_by('-responded_at')[:30])

    candidate_dicts: list[dict] = []
    if candidate_responses:
        try:
            ranking = EventSurveyMatcher(org).rank(event, candidate_responses)
        except Exception:
            logger.exception('EventSurveyMatcher failed for event %s', event.id)
            ranking = None
        if ranking and ranking.candidates:
            by_id = {str(r.id): r for r in candidate_responses}
            sub_by_upload = {sub.upload_id: sub for sub in subscriptions if sub.upload_id}
            threshold = float(CONFIDENCE_THRESHOLD)
            for cand in ranking.candidates:
                if float(cand.confidence or 0) < threshold:
                    continue
                resp = by_id.get(str(cand.response_id))
                if not resp:
                    continue
                sub = sub_by_upload.get(resp.upload_id)
                candidate_dicts.append(_survey_candidate_dict(resp, cand, subscription=sub))

    if request.GET.get('format') == 'json':
        return JsonResponse({'data': {'candidates': candidate_dicts}})
    # Non-JS fallback: render a minimal partial.
    return render(request, 'tickets/event_survey_match.html', {
        'event': event,
        'candidates': candidate_dicts,
        'apply_url': reverse('tickets:event_survey_apply', args=[event.id]),
    })


@login_required
@require_org
@require_http_methods(["POST"])
def event_survey_apply(request, event_id):
    """User-confirmed link: bulk-link selected responses to this event with match metadata."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    response_ids = request.POST.getlist('response_id')
    confidences = request.POST.getlist('confidence')
    reasonings = request.POST.getlist('reasoning')

    linked_count = 0
    for idx, rid in enumerate(response_ids):
        rid = (rid or '').strip()
        if not rid:
            continue
        try:
            confidence = Decimal(str(confidences[idx])) if idx < len(confidences) and confidences[idx] else None
        except (InvalidOperation, ValueError):
            confidence = None
        reasoning = reasonings[idx] if idx < len(reasonings) else ''

        updated = ExternalSurveyResponse.objects.filter(
            id=rid, organization=org, event__isnull=True,
        ).update(
            event=event,
            match_confidence=confidence,
            match_reasoning=reasoning or '',
        )
        linked_count += updated

    if linked_count:
        django_cache.delete(_event_stats_cache_key(str(event.id)))
        _invalidate_event_upload_stats_cache(str(event.id))

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'message': f'Linked {linked_count} response(s).'})

    if linked_count:
        messages.success(request, f'Linked {linked_count} survey response(s).')
    return redirect(reverse('tickets:event_detail', args=[event.id]) + '?tab=surveys')


@login_required
@require_org
@require_http_methods(["POST"])
def event_survey_unlink(request, event_id):
    """Per-row Unlink: clear the event FK on one linked response."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    response_id = (request.POST.get('response_id') or '').strip()
    if response_id:
        unlinked = ExternalSurveyResponse.objects.filter(
            id=response_id, organization=org, event=event,
        ).update(event=None)
        if unlinked:
            django_cache.delete(_event_stats_cache_key(str(event.id)))
            _invalidate_event_upload_stats_cache(str(event.id))
            messages.success(request, 'Survey response unlinked.')
    return redirect(reverse('tickets:event_detail', args=[event.id]) + '?tab=surveys')


@login_required
@require_org
def event_survey_response_detail(request, event_id, kind, response_id):
    """JSON: full question/answer breakdown for a single survey response.

    Powers the "Individual Responses" row-click modal on the event Surveys
    tab. ``kind`` is 'internal' (a Cue SurveyResponse) or 'external' (a
    Typeform/CSV ExternalSurveyResponse).
    """
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    meta = {'source': '', 'date': '', 'respondent': ''}
    items = []

    if kind == 'internal':
        resp = get_object_or_404(
            SurveyResponse.objects
            .filter(event=event, organization=org)
            .select_related('customer')
            .prefetch_related('answers__question', 'answers__selected_options'),
            id=response_id,
        )
        meta['source'] = 'Cue survey'
        if resp.submitted_at:
            meta['date'] = resp.submitted_at.strftime('%b %-d, %Y · %-I:%M %p')
        if resp.customer:
            meta['respondent'] = resp.customer.email or resp.customer.name or ''
        for ans in resp.answers.all():
            q = ans.question
            if q and q.question_type in SurveyQuestion.CHOICE_TYPES:
                value = ', '.join(o.label for o in ans.selected_options.all())
                atype = q.question_type
            elif ans.star_rating is not None:
                value, atype = f"{ans.star_rating} / 5 stars", 'star_rating'
            elif ans.nps_score is not None:
                value, atype = f"{ans.nps_score} / 10", 'nps'
            else:
                value, atype = (ans.text_answer or ''), 'text'
            items.append({
                'question': q.question_text if q else '',
                'answer': value,
                'type': atype,
            })
    elif kind == 'external':
        resp = get_object_or_404(
            ExternalSurveyResponse.objects.filter(event=event, organization=org),
            id=response_id,
        )
        meta['source'] = 'Typeform' if resp.typeform_response_id else 'External upload'
        if resp.responded_at:
            meta['date'] = resp.responded_at.strftime('%b %-d, %Y · %-I:%M %p')
        meta['respondent'] = resp.email or ''
        # Repopulate answer titles from the form's cached question snapshot —
        # raw_answers often carries empty titles (see TypeformFormSubscription).
        questions_snapshot = []
        if resp.upload_id:
            sub = (
                TypeformFormSubscription.objects
                .filter(organization=org, upload_id=resp.upload_id)
                .first()
            )
            if sub:
                questions_snapshot = sub.questions
        from .services.typeform.helpers import enrich_answers_with_titles
        for ans in enrich_answers_with_titles(resp.raw_answers, questions_snapshot):
            if not isinstance(ans, dict):
                continue
            value = ans.get('value')
            if isinstance(value, (list, tuple)):
                value = ', '.join(str(v) for v in value)
            elif value is None:
                value = ''
            else:
                value = str(value)
            items.append({
                'question': (ans.get('title') or '').strip() or '(untitled)',
                'answer': value,
                'type': ans.get('type') or '',
            })
        if not items:
            # Fallback for CSV-uploaded rows without raw_answers: rebuild the
            # Q&A list from the parsed structured columns.
            structured = [
                ('Overall rating', resp.overall_rating),
                ('NPS score', resp.nps_score),
                ('City', resp.city),
                ('What they enjoyed', ', '.join(resp.enjoyed) if resp.enjoyed else ''),
                ('Genres', ', '.join(resp.genres) if resp.genres else ''),
                ('Suggested improvements', ', '.join(resp.improvements) if resp.improvements else ''),
                ('Crowd vibe', resp.crowd_vibe),
                ('Venue feel', resp.venue_feel),
                ('Pre-event info', resp.pre_event_info),
                ('How they found out', resp.found_out_how),
                ('Feedback', resp.text_feedback),
            ]
            for label, val in structured:
                if val in (None, '', []):
                    continue
                items.append({'question': label, 'answer': str(val), 'type': ''})
    else:
        return JsonResponse({'error': 'Invalid response kind.'}, status=400)

    return JsonResponse({'meta': meta, 'items': items})


# ── Error handlers ──────────────────────────────────────────────────────────

def csrf_failure(request, reason=''):
    """Custom CSRF failure view - renders the branded 403 template."""
    logger.warning("CSRF failure: %s", reason)
    return render(request, '403.html', {'reason': reason, 'csrf_token_missing': True}, status=403)
