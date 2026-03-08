import calendar
import os
import json
import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.urls import reverse_lazy
from django.db.models import (
    Sum, Count, Avg, Max, Q, Subquery, OuterRef, Prefetch,
    Case, When, Value, F, CharField,
)
from django.db.models.functions import Coalesce, Greatest
from django.db import models
from django.core.paginator import Paginator
from django.http import JsonResponse, Http404, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.db import connection, transaction
from django.utils import timezone as django_tz
from django.utils.text import slugify
import pandas as pd

from .models import (
    Organization, UserProfile, OrganizationInvitation,
    CSVFormat, UploadedFile, Customer, Event, EventExpense, EventTalent, TicketOrder, Ticket, Venue,
    CustomField, EventCustomFieldValue, IncomeSource, EventIncome,
    SurveyQuestion, SurveyInvitation, SurveyResponse, SurveyAnswer,
    PipedreamCalendarConnection,
    SaleableTicketType, StripeCheckoutSession, Payout, PromoCode,
    ExternalSurveyUpload, ExternalSurveyResponse,
    EVENT_STATUS_DRAFT, EVENT_STATUS_LIVE, EVENT_STATUS_ENDED, EVENT_STATUS_CANCELLED,
)
from .forms import (
    EventCSVUploadForm, EventExpenseForm, TicketPriceEntryForm, CSVFormatForm,
    VenueForm, EventForm, EventTalentFormSet, LoginForm,
    IncomeSourceForm, EventIncomeForm,
    OTPVerificationForm, MemberInviteForm, AttendeePhoneForm,
    ProfileCompletionForm,
    SaleableTicketTypeForm, PublicTicketPurchaseForm,
    DirectEventForm, DirectTicketTypeFormSet,
    PromoCodeForm, SurveyUploadForm,
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
from .utils import get_organization, require_org, require_organizer, require_host, require_admin, require_owner, clear_org_cache, next_order_number, generate_qr_b64
from .feature_flags import direct_ticketing_enabled

from django.core.cache import cache as django_cache

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


def _event_list_cache_key(org_id, search, sort, page):
    """Build a versioned, org-scoped cache key for the event_list response."""
    try:
        version = django_cache.get(f'event_list_ver:{org_id}', 0)
    except Exception:
        version = 0
    return f'event_list:{version}:{org_id}:{search}:{sort}:{page}'


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


_ANNOTATED_SORT_FIELDS = {
    'upload_count', '-upload_count', 'total_revenue', '-total_revenue',
}
_ALLOWED_SORTS = {
    'name', '-name', 'start_date', '-start_date',
    'upload_count', '-upload_count', 'total_revenue', '-total_revenue',
}


def _annotate_events(queryset):
    """Add subquery annotations (orders, uploads, ticket_revenue, additional_income, total_revenue, tickets, expenses) to an Event queryset."""
    return queryset.annotate(
        total_orders=Coalesce(
            Subquery(
                TicketOrder.objects.filter(event=OuterRef('pk'))
                .values('event')
                .annotate(n=Count('id'))
                .values('n')[:1]
            ),
            0,
        ),
        upload_count=Coalesce(
            Subquery(
                TicketOrder.objects.filter(event=OuterRef('pk'))
                .exclude(uploaded_file__isnull=True)
                .values('event')
                .annotate(n=Count('uploaded_file', distinct=True))
                .values('n')[:1]
            ),
            0,
        ),
        ticket_revenue=Coalesce(
            Subquery(
                TicketOrder.objects.filter(event=OuterRef('pk'))
                .values('event')
                .annotate(total=Sum('total_amount'))
                .values('total')[:1],
                output_field=models.DecimalField(max_digits=10, decimal_places=2),
            ),
            Decimal('0.00'),
        ),
        total_additional_income=Coalesce(
            Subquery(
                EventIncome.objects.filter(event=OuterRef('pk'), deleted_at__isnull=True)
                .values('event')
                .annotate(total=Sum('amount'))
                .values('total')[:1],
                output_field=models.DecimalField(max_digits=10, decimal_places=2),
            ),
            Decimal('0.00'),
        ),
        total_revenue=F('ticket_revenue') + F('total_additional_income'),
        total_tickets=Coalesce(
            Subquery(
                Ticket.objects.filter(ticket_order__event=OuterRef('pk'))
                .values('ticket_order__event')
                .annotate(n=Count('id'))
                .values('n')[:1]
            ),
            0,
        ),
        total_expenses=Coalesce(
            Subquery(
                EventExpense.objects.filter(event=OuterRef('pk'), deleted_at__isnull=True)
                .values('event')
                .annotate(total=Sum('amount'))
                .values('total')[:1],
                output_field=models.DecimalField(max_digits=10, decimal_places=2),
            ),
            Decimal('0.00'),
        ),
    )


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

def unified_login_view(request):
    """Step 1: enter phone number — handles both login and new signup."""
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
    return render(request, 'tickets/auth/login.html', {'form': form})


@require_http_methods(["GET", "POST"])
def unified_verify_view(request):
    """Step 2: verify OTP — log in existing user or send new user to profile completion."""
    from django.contrib.auth import login as auth_login
    from django.contrib.auth.models import User
    from .sms import check_phone_verification
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    session_data = request.session.get('verify_unified')
    if not session_data:
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
                        user = User.objects.get(username=phone)
                    except User.DoesNotExist:
                        messages.error(request, 'Account not found. Please sign up.')
                        return redirect('tickets:login')
                    auth_login(request, user, backend='tickets.backends.PhoneBackend')
                    try:
                        if user.profile.is_organizer:
                            return redirect('tickets:home')
                    except UserProfile.DoesNotExist:
                        pass
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


@require_http_methods(["GET", "POST"])
def complete_profile_view(request):
    """Step 3 (new users only): collect name, email, gender, marketing opt-in."""
    from django.contrib.auth import login as auth_login
    from django.contrib.auth.models import User
    if request.user.is_authenticated:
        return redirect('tickets:attendee_dashboard')
    phone = request.session.get('pending_signup_phone')
    if not phone:
        return redirect('tickets:login')
    if request.method == 'POST':
        form = ProfileCompletionForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            if User.objects.filter(username=phone).exists():
                messages.info(request, 'An account with this phone already exists. Please log in.')
                del request.session['pending_signup_phone']
                return redirect('tickets:login')
            user = User.objects.create(
                username=phone,
                email=cd['email'],
                first_name=cd['first_name'],
                last_name=cd['last_name'],
            )
            user.set_unusable_password()
            user.save()
            UserProfile.objects.create(
                user=user,
                role=UserProfile.Role.ATTENDEE,
                phone_number=phone,
                gender=cd['gender'],
                marketing_opt_in=cd['marketing_opt_in'],
            )
            del request.session['pending_signup_phone']
            auth_login(request, user, backend='tickets.backends.PhoneBackend')
            messages.success(request, 'Welcome to Eventflow!')
            return redirect('tickets:attendee_dashboard')
    else:
        form = ProfileCompletionForm()
    return render(request, 'tickets/auth/complete_profile.html', {'form': form})


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


@login_required
def become_organizer_view(request):
    """Informational page for attendees who want to create an organization."""
    try:
        if request.user.profile.is_organizer:
            return redirect('tickets:home')
    except UserProfile.DoesNotExist:
        pass
    return render(request, 'tickets/auth/become_organizer.html')


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
        messages.error(request, 'No pending verification. Please sign up first.')
        return redirect('tickets:signup')
    phone = session_data["phone"]
    if request.method == "POST":
        form = OTPVerificationForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data["otp_code"]
            if not check_phone_verification(phone, code):
                messages.error(request, 'Incorrect or expired code. Please try again.')
            else:
                if User.objects.filter(username=phone).exists():
                    messages.info(request, 'An account with this phone already exists. Please log in.')
                    del request.session["verify_signup"]
                    return redirect('tickets:phone_login')
                user = User.objects.create(username=phone, email='', first_name='', last_name='')
                user.set_unusable_password()
                user.save()
                UserProfile.objects.create(user=user, role=UserProfile.Role.ATTENDEE, phone_number=phone)
                del request.session["verify_signup"]
                login(request, user, backend='tickets.backends.PhoneBackend')
                messages.success(request, 'Account created! Welcome to Eventflow.')
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
        return redirect('tickets:signup')
    if not start_phone_verification(session_data["phone"]):
        messages.error(request, 'Could not resend the code. Please try again.')
    else:
        messages.success(request, 'A new code has been sent.')
    return redirect('tickets:verify_otp')


def health_check(request):
    """Health check endpoint for Render monitoring."""
    try:
        # Check database connection
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return HttpResponse("OK", status=200)
    except Exception as e:
        return HttpResponse(f"Database connection failed: {str(e)}", status=503)


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
            org = form.save(commit=False)
            base_slug = slugify(org.name)
            slug = base_slug
            counter = 1
            while Organization.objects.filter(slug=slug).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            org.slug = slug
            org.save()
            profile.organization = org
            profile.role = UserProfile.Role.ORGANIZER
            profile.org_role = UserProfile.OrgRole.OWNER
            profile.save(update_fields=['organization', 'role', 'org_role'])
            clear_org_cache(request)
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
        UserProfile.objects.filter(organization=org)
        .select_related('user')
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
    org = get_organization(request)
    form = MemberInviteForm(request.POST)
    if not form.is_valid():
        for field, errors in form.errors.items():
            for err in errors:
                messages.error(request, err)
        return redirect('tickets:member_list')

    email = form.cleaned_data['email'].strip().lower()
    role = UserProfile.Role.ORGANIZER
    org_role = form.cleaned_data['org_role']
    if UserProfile.objects.filter(
        organization=org,
        user__email__iexact=email,
    ).exists():
        messages.error(request, f'{email} is already a member of this organization.')
        return redirect('tickets:member_list')

    if OrganizationInvitation.objects.filter(
        organization=org,
        email__iexact=email,
        status=OrganizationInvitation.Status.PENDING,
        expires_at__gt=django_tz.now(),
    ).exists():
        messages.error(request, f'An invitation for {email} is already pending.')
        return redirect('tickets:member_list')

    expires_at = django_tz.now() + timedelta(days=7)
    invitation = OrganizationInvitation(
        organization=org,
        email=email,
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
    messages.success(request, f'Invitation sent to {email}. They can use the link in the email to join.')
    return redirect('tickets:member_list')


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
        from django.urls import reverse
        from urllib.parse import urlencode
        login_url = reverse('tickets:login')
        invite_url = request.build_absolute_uri()
        return redirect(f'{login_url}?{urlencode({"next": invite_url})}')

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
    profile.organization = invitation.organization
    profile.role = invitation.role
    profile.org_role = invitation.org_role
    profile.save(update_fields=['organization', 'role', 'org_role'])
    invitation.status = OrganizationInvitation.Status.ACCEPTED
    invitation.accepted_at = django_tz.now()
    invitation.accepted_by = request.user
    invitation.save(update_fields=['status', 'accepted_at', 'accepted_by'])
    clear_org_cache(request)
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

    # Show warning when current time is past the event's end date+time and upload_count is 0 (current page only)
    now_local = django_tz.localtime(django_tz.now()).replace(tzinfo=None)
    event_ids_show_warning = set()
    event_ids_show_placeholder = set()
    for ev in page_obj:
        if ev.upload_count != 0:
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
    total_orders = order_agg['total_orders']
    total_revenue = order_agg['total_revenue'] + (additional_agg['total'] or Decimal('0.00'))
    total_tickets = Ticket.objects.filter(ticket_order__event__organization=org).count()
    
    context = {
        'page_obj': page_obj,
        'event_ids_show_warning': event_ids_show_warning,
        'event_ids_show_placeholder': event_ids_show_placeholder,
        'today': date.today(),
        'total_customers': total_customers,
        'total_orders': total_orders,
        'total_revenue': total_revenue,
        'total_tickets': total_tickets,
    }
    return render(request, 'tickets/home.html', context)


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
            _invalidate_event_list_cache(uploaded_file.organization)

        return redirect('tickets:upload_results', file_id=uploaded_file.id)

    except Exception as e:
        uploaded_file.status = 'failed'
        uploaded_file.save(update_fields=['status'])
        messages.error(request, f"Error processing CSV: {str(e)}")
        return redirect('tickets:upload_results', file_id=uploaded_file.id)


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
@require_host
def customer_list(request):
    """Display list of all customers with LTV and optional segment filter."""
    org = get_organization(request)
    customers = Customer.objects.filter(organization=org).exclude(email__endswith='@placeholder.local')

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

    customers = customers.annotate(order_count=Count('ticket_orders'))

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
            'city': city.strip() or '—',
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


@login_required
@require_org
@require_host
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
        Customer.objects.filter(organization=org).exclude(email__endswith='@placeholder.local')
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
        TicketOrder.objects.filter(customer__organization=org, is_in_person=False)
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
    
    # Order statistics — single aggregate instead of 5 separate queries
    order_stats = customer.ticket_orders.aggregate(
        total_orders=Count('id'),
        avg_order_value=Coalesce(Avg('total_amount'), Decimal('0.00')),
        last_order_date=Max('order_date'),
    )
    total_orders = order_stats['total_orders']
    avg_order_value = order_stats['avg_order_value']
    last_order_date = order_stats['last_order_date']
    total_tickets = Ticket.objects.filter(ticket_order__customer=customer).count()

    # Event attendance — select_related to avoid N+1 on venue in template
    events_attended = Event.objects.filter(
        ticket_orders__customer=customer
    ).select_related('venue').distinct()

    # Paginate orders — select_related + annotate to avoid N+1 in template
    orders = customer.ticket_orders.select_related('event').annotate(
        tickets_count=Count('tickets')
    ).order_by('-order_date')
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
        'last_order_date': last_order_date,
        'events_attended': events_attended,
        'page_obj': page_obj,
        'segment_badge_color': segment_badge_color,
    }
    return render(request, 'tickets/customer_detail.html', context)


# Event Management Views

@login_required
@require_org
@require_organizer
def event_list(request):
    """Display list of all events with associated uploads."""
    org = get_organization(request)

    search_query = request.GET.get('search', '')
    sort_by = request.GET.get('sort', '-start_date')
    page_number = request.GET.get('page', '1')

    # Validate sort parameter
    if sort_by not in _ALLOWED_SORTS:
        sort_by = '-start_date'

    # Check cache first (skip gracefully when Redis is unavailable)
    cache_key = _event_list_cache_key(org.pk, search_query, sort_by, page_number)
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

    if sort_by in _ANNOTATED_SORT_FIELDS:
        # Slow path: must annotate all rows before sorting by a computed field.
        # Caching mitigates repeat hits.
        events = _annotate_events(base_qs).order_by(sort_by)
        paginator = Paginator(events, 25)
        page_obj = paginator.get_page(page_number)
    else:
        # Fast path: sort + paginate on native columns first, then annotate only the page.
        events = base_qs.order_by(sort_by)
        paginator = Paginator(events, 25)
        page_obj = paginator.get_page(page_number)

        # Annotate only the events on the current page
        page_pks = [e.pk for e in page_obj.object_list]
        annotated_map = {
            e.pk: e
            for e in _annotate_events(
                Event.objects.filter(pk__in=page_pks)
            ).select_related('venue')
        }
        # Replace the page's object list, preserving the paginator's sort order
        page_obj.object_list = [annotated_map[pk] for pk in page_pks]

    # Show warning when current time is past the event's end date+time and upload_count is 0 (same as home)
    now_local = django_tz.localtime(django_tz.now()).replace(tzinfo=None)
    event_ids_show_warning = set()
    event_ids_show_placeholder = set()
    for ev in page_obj:
        if ev.upload_count != 0:
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
        'sort_by': sort_by,
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
                SaleableTicketType.objects.order_by('order', 'name'),
            ),
        ),
        id=event_id,
    )

    # Get all distinct uploads associated with this event
    associated_uploads = event.get_associated_uploads().select_related('csv_format')

    # Calculate statistics per upload — use Subquery for revenue to avoid join inflation
    # (Count('tickets') joins Ticket and would duplicate rows, inflating Sum('total_amount'))
    revenue_per_upload = (
        TicketOrder.objects.filter(event=event, uploaded_file_id=OuterRef('uploaded_file'))
        .values('uploaded_file')
        .annotate(s=Sum('total_amount'))
        .values('s')[:1]
    )
    upload_agg = (
        TicketOrder.objects.filter(event=event, uploaded_file__in=associated_uploads)
        .values('uploaded_file')
        .annotate(
            orders_count=Count('id'),
            revenue=Coalesce(
                Subquery(revenue_per_upload, output_field=models.DecimalField(max_digits=10, decimal_places=2)),
                Decimal('0.00'),
            ),
            tickets_count=Count('tickets', distinct=True),
        )
    )
    upload_stats_map = {row['uploaded_file']: row for row in upload_agg}
    upload_stats = []
    for upload in associated_uploads:
        stats = upload_stats_map.get(upload.id, {})
        upload_stats.append({
            'upload': upload,
            'orders_count': stats.get('orders_count', 0),
            'revenue': stats.get('revenue', Decimal('0.00')),
            'tickets_count': stats.get('tickets_count', 0),
        })

    # Event statistics — combine into single aggregate (tickets separate to avoid join inflation)
    event_stats = event.ticket_orders.aggregate(
        total_orders=Count('id'),
        total_revenue=Coalesce(Sum('total_amount'), Decimal('0.00')),
        total_customers=Count('customer', distinct=True),
    )
    total_orders = event_stats['total_orders']
    ticket_revenue = event_stats['total_revenue']
    total_customers = event_stats['total_customers']
    total_tickets = Ticket.objects.filter(ticket_order__event=event).count()

    # Additional income (user-defined sources: Bar Splits, Merch, etc.)
    additional_income_lines = list(event.additional_income.all())
    total_additional_income = sum(line.amount for line in additional_income_lines)
    total_revenue = ticket_revenue + total_additional_income

    # Expense data
    expenses = event.expenses.filter(deleted_at__isnull=True)
    total_expenses = expenses.aggregate(
        total=Coalesce(Sum('amount'), Decimal('0.00'))
    )['total']
    profit = total_revenue - total_expenses
    margin_pct = (profit / total_revenue * 100) if total_revenue > 0 else None
    expenses_by_category = (
        expenses.values('category')
        .annotate(total=Sum('amount'))
        .order_by('-total')
    )

    # Paginate orders — select_related + annotate to avoid N+1 in template
    orders_qs = event.ticket_orders.select_related(
        'customer', 'uploaded_file'
    ).annotate(
        tickets_count=Count('tickets')
    ).order_by('-order_date')
    paginator = Paginator(orders_qs, 100)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Custom field values: only show those for the current org's custom fields
    custom_field_values_display = [
        v for v in event.custom_field_values.all()
        if v.custom_field.organization_id == org.id
    ]

    # Map category keys to display labels
    category_labels = dict(EventExpense.CATEGORY_CHOICES)

    # Survey results
    survey_invitations_count = SurveyInvitation.objects.filter(event=event).count()
    survey_responses_count = SurveyResponse.objects.filter(event=event).count()

    survey_results = None
    if survey_responses_count > 0:
        star_avg = SurveyAnswer.objects.filter(
            response__event=event, star_rating__isnull=False
        ).aggregate(avg=Avg('star_rating'))['avg']

        nps_answers = SurveyAnswer.objects.filter(
            response__event=event, nps_score__isnull=False
        )
        nps_total = nps_answers.count()
        nps_score = None
        if nps_total > 0:
            promoters = nps_answers.filter(nps_score__gte=9).count()
            detractors = nps_answers.filter(nps_score__lte=6).count()
            nps_score = round((promoters - detractors) / nps_total * 100)

        recent_comments = list(
            SurveyAnswer.objects.filter(
                response__event=event
            ).exclude(text_answer='').order_by('-response__submitted_at').values(
                'text_answer',
                'response__customer__name',
                'response__customer__email',
            )[:5]
        )

        survey_results = {
            'avg_star_rating': round(star_avg, 1) if star_avg else None,
            'nps_score': nps_score,
            'nps_total': nps_total,
            'recent_comments': recent_comments,
        }

    # Direct ticketing: sales dashboard data inlined on event detail
    saleable_ticket_types_list = list(event.saleable_ticket_types.all())


    # Ticket type breakdown for donut chart
    if event.ticketing_type == 'direct':
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
    ticket_type_breakdown_json = json.dumps(ticket_type_breakdown)

    context = {
        'event': event,
        'upload_stats': upload_stats,
        'total_orders': total_orders,
        'ticket_revenue': ticket_revenue,
        'total_additional_income': total_additional_income,
        'total_revenue': total_revenue,
        'total_tickets': total_tickets,
        'total_customers': total_customers,
        'total_expenses': total_expenses,
        'profit': profit,
        'margin_pct': margin_pct,
        'expenses_by_category': expenses_by_category,
        'expenses': expenses,
        'category_labels': category_labels,
        'additional_income_lines': additional_income_lines,
        'income_sources': IncomeSource.objects.filter(organization=org).order_by('order', 'name'),
        'page_obj': page_obj,
        'custom_field_values_display': custom_field_values_display,
        'survey_invitations_count': survey_invitations_count,
        'survey_responses_count': survey_responses_count,
        'survey_results': survey_results,
        'saleable_ticket_types': saleable_ticket_types_list,
        'ticket_type_breakdown': ticket_type_breakdown,
        'ticket_type_breakdown_json': ticket_type_breakdown_json,
    }
    if event.ticketing_type == 'direct':
        sessions = list(
            StripeCheckoutSession.objects.filter(event=event)
            .select_related('ticket_order')
            .order_by('-created_at')[:50]
        )
        for s in sessions:
            s.amount_dollars = Decimal(str(s.amount_total_cents)) / 100
        context['dashboard_sessions'] = sessions
        context['direct_total_revenue'] = sum(
            tt.quantity_sold * tt.price for tt in saleable_ticket_types_list
        )
        context['public_buy_url'] = request.build_absolute_uri(f'/buy/{event.id}/')
        views = getattr(event, 'public_buy_page_views', 0) or 0
        context['conversion_rate_pct'] = (
            round(total_orders / views * 100, 1) if views > 0 else None
        )
        context['promo_codes'] = list(
            PromoCode.objects.filter(event=event, organization=org).order_by('code')
        )
    return render(request, 'tickets/event_detail.html', context)


@login_required
@require_org
@require_admin
@require_http_methods(["GET", "POST"])
def event_delete(request, event_id):
    """Permanently delete an event and all its orders and tickets."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    from .models import TICKETING_TYPE_DIRECT
    if event.ticketing_type == TICKETING_TYPE_DIRECT:
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

            _invalidate_event_list_cache(org)
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
    if ticketing_type == TICKETING_TYPE_DIRECT and not direct_ticketing_enabled(request.user):
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
            if form.is_valid() and ticket_formset.is_valid():
                venue = form.cleaned_data['venue']
                event = form.save(commit=False)
                event.organization = org
                event.created_by = request.user
                event.venue = venue
                event.ticketing_type = TICKETING_TYPE_DIRECT
                event.save()
                instances = ticket_formset.save(commit=False)
                for tt in instances:
                    if tt.name and tt.name.strip():
                        tt.event = event
                        tt.save()
                for tt in ticket_formset.deleted_objects:
                    tt.delete()
                _invalidate_event_list_cache(org)
                messages.success(request, f"Event '{event.name}' created successfully.")
                # TODO: re-enable when calendar sync is ready
                # if event.start_date >= date.today():
                #     _sync_event_to_google_calendar(event)
                return redirect('tickets:event_detail', event_id=event.id)
        else:
            form = DirectEventForm(organization=org)
            ticket_formset = DirectTicketTypeFormSet(
                queryset=SaleableTicketType.objects.none(),
                prefix='ticket_type',
            )
        no_venues = not Venue.objects.filter(organization=org).exists()
        context = {
            'form': form,
            'ticket_formset': ticket_formset,
            'ticketing_type': ticketing_type,
            'no_venues': no_venues,
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
            messages.success(request, f"Event '{event.name}' created successfully.")
            if event.start_date >= date.today():
                _regenerate_event_doc_background(org)
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
            ticket_formset = DirectTicketTypeFormSet(
                request.POST,
                queryset=SaleableTicketType.objects.filter(event=event),
                prefix='ticket_type',
            )
            if form.is_valid() and ticket_formset.is_valid():
                venue = form.cleaned_data['venue']
                event = form.save(commit=False)
                event.updated_by = request.user
                event.venue = venue
                event.save()
                instances = ticket_formset.save(commit=False)
                for tt in instances:
                    if tt.name and tt.name.strip():
                        tt.event = event
                        tt.save()
                for tt in ticket_formset.deleted_objects:
                    tt.delete()
                _invalidate_event_list_cache(org)
                messages.success(request, f"Event '{event.name}' updated successfully.")
                _regenerate_event_doc_background(org)
                return redirect('tickets:event_detail', event_id=event.id)
        else:
            form = DirectEventForm(instance=event, organization=org)
            ticket_formset = DirectTicketTypeFormSet(
                queryset=SaleableTicketType.objects.filter(event=event).order_by('name'),
                prefix='ticket_type',
            )
        context = {
            'form': form,
            'ticket_formset': ticket_formset,
            'event': event,
            'ticketing_type': event.ticketing_type,
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
            messages.success(request, f"Event '{event.name}' updated successfully.")
            if was_future or event.start_date >= date.today():
                _regenerate_event_doc_background(org)
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
@require_host
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


# ---------------------------------------------------------------------------
# Event Expense Views
# ---------------------------------------------------------------------------

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
            messages.success(request, f'Expense "${expense.description}" added.')
            return redirect('tickets:event_detail', event_id=event.id)
    else:
        form = EventExpenseForm()

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
            ticket_revenue=Coalesce(
                Subquery(
                    TicketOrder.objects.filter(event=OuterRef('pk'))
                    .values('event')
                    .annotate(total=Sum('total_amount'))
                    .values('total')[:1],
                    output_field=models.DecimalField(max_digits=10, decimal_places=2),
                ),
                Decimal('0.00'),
            ),
            total_additional_income=Coalesce(
                Subquery(
                    EventIncome.objects.filter(event=OuterRef('pk'), deleted_at__isnull=True)
                    .values('event')
                    .annotate(total=Sum('amount'))
                    .values('total')[:1],
                    output_field=models.DecimalField(max_digits=10, decimal_places=2),
                ),
                Decimal('0.00'),
            ),
            total_expenses=Coalesce(
                Subquery(
                    EventExpense.objects.filter(event=OuterRef('pk'), deleted_at__isnull=True)
                    .values('event')
                    .annotate(total=Sum('amount'))
                    .values('total')[:1],
                    output_field=models.DecimalField(max_digits=10, decimal_places=2),
                ),
                Decimal('0.00'),
            ),
        )
        .select_related('venue')
        .order_by('-start_date')
    )

    # Summary stats (total_revenue = ticket_revenue + additional_income)
    summary_revenue = Decimal('0.00')
    summary_expenses = Decimal('0.00')
    event_rows = []
    for e in events:
        total_revenue = e.ticket_revenue + e.total_additional_income
        profit = total_revenue - e.total_expenses
        margin = (profit / total_revenue * 100) if total_revenue > 0 else None
        event_rows.append({
            'event': e,
            'revenue': total_revenue,
            'expenses': e.total_expenses,
            'profit': profit,
            'margin': margin,
        })
        summary_revenue += total_revenue
        summary_expenses += e.total_expenses

    summary_profit = summary_revenue - summary_expenses
    summary_margin = (summary_profit / summary_revenue * 100) if summary_revenue > 0 else None

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

    # Market chart data — sorted high → low by profit
    market_chart_data = {
        'labels': [m['city'] for m in market_rows],
        'profit': [float(m['profit']) for m in market_rows],
    }

    # Monthly aggregation for chart — bucket events by calendar month, ordered earliest → most recent
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

    # Per-event chart data — ordered earliest → most recent
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
        'summary_profit': summary_profit,
        'summary_margin': summary_margin,
        'chart_data_json': json.dumps(chart_data),
        'event_chart_data_json': json.dumps(event_chart_data),
        'market_chart_data_json': json.dumps(market_chart_data),
        'active_window': active_window,
        'window_start': start_date or '',
        'window_end': end_date or '',
        'window_choices': WINDOW_CHOICES,
    }
    return render(request, 'tickets/profitability_overview.html', context)


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

    # Dispatch Celery task
    from .tasks import send_survey_emails_task
    send_survey_emails_task.delay(str(event_id), str(org.id))

    messages.success(
        request,
        f"Survey invitations created for {len(invitations)} attendee(s). Emails are being sent."
    )
    return redirect('tickets:event_detail', event_id=event_id)


def survey_form(request, token):
    """Public survey form — no login required."""
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
    """SSE endpoint — streams LLM tokens for the chat agent."""
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
# Direct Ticket Selling — Organizer Views
# ---------------------------------------------------------------------------

@login_required
@require_org
@require_host
@require_http_methods(["GET", "POST"])
def saleable_ticket_type_create(request, event_id):
    """Create a new SaleableTicketType for an event."""
    if not direct_ticketing_enabled(request.user):
        raise Http404()
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    if request.method == 'POST':
        form = SaleableTicketTypeForm(request.POST)
        if form.is_valid():
            tt = form.save(commit=False)
            tt.event = event
            tt.save()
            _invalidate_event_list_cache(org)
            messages.success(request, f'Ticket type "{tt.name}" created.')
            return redirect('tickets:event_detail', event_id=event.id)
    else:
        form = SaleableTicketTypeForm()

    return render(request, 'tickets/saleable_ticket_type_form.html', {
        'form': form,
        'event': event,
        'editing': False,
    })


@login_required
@require_org
@require_host
@require_http_methods(["GET", "POST"])
def saleable_ticket_type_edit(request, event_id, ticket_type_id):
    """Edit an existing SaleableTicketType."""
    if not direct_ticketing_enabled(request.user):
        raise Http404()
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    tt = get_object_or_404(SaleableTicketType.objects.filter(event=event), id=ticket_type_id)
    if request.method == 'POST':
        form = SaleableTicketTypeForm(request.POST, instance=tt)
        if form.is_valid():
            updated = form.save()
            _invalidate_event_list_cache(org)
            messages.success(request, f'Ticket type "{updated.name}" updated.')
            return redirect('tickets:event_detail', event_id=event.id)
    else:
        form = SaleableTicketTypeForm(instance=tt)

    return render(request, 'tickets/saleable_ticket_type_form.html', {
        'form': form,
        'event': event,
        'ticket_type': tt,
        'editing': True,
    })


@login_required
@require_org
@require_host
@require_http_methods(["POST"])
def saleable_ticket_type_toggle(request, event_id, ticket_type_id):
    """Toggle is_active on a SaleableTicketType."""
    if not direct_ticketing_enabled(request.user):
        raise Http404()
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    tt = get_object_or_404(SaleableTicketType.objects.filter(event=event), id=ticket_type_id)
    tt.is_active = not tt.is_active
    tt.save(update_fields=['is_active'])
    status = 'activated' if tt.is_active else 'deactivated'
    messages.success(request, f'"{tt.name}" {status}.')
    return redirect('tickets:event_detail', event_id=event.id)


@login_required
@require_org
@require_host
@require_http_methods(["GET", "POST"])
def saleable_ticket_type_delete(request, event_id, ticket_type_id):
    """Delete a SaleableTicketType (only if no tickets sold)."""
    if not direct_ticketing_enabled(request.user):
        raise Http404()
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    tt = get_object_or_404(SaleableTicketType.objects.filter(event=event), id=ticket_type_id)

    if request.method == 'POST':
        if tt.quantity_sold > 0:
            messages.error(request, f'Cannot delete "{tt.name}" — {tt.quantity_sold} tickets already sold.')
            return redirect('tickets:event_detail', event_id=event.id)
        name = tt.name
        tt.delete()
        _invalidate_event_list_cache(org)
        messages.success(request, f'Ticket type "{name}" deleted.')
        return redirect('tickets:event_detail', event_id=event.id)

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
    if not direct_ticketing_enabled(request.user):
        raise Http404()
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org, deleted_at__isnull=True), id=event_id)
    if event.status != EVENT_STATUS_DRAFT:
        messages.error(request, 'Event is not in Draft state.')
        return redirect('tickets:event_detail', event_id=event.id)
    event.status = EVENT_STATUS_LIVE
    event.save(update_fields=['status'])
    _invalidate_event_list_cache(org)
    messages.success(request, f'"{event.name}" is now live. The public ticket page is active.')
    return redirect('tickets:event_detail', event_id=event.id)


@login_required
@require_org
@require_host
@require_http_methods(["POST"])
def event_end_sales(request, event_id):
    """Transition a direct event from Live → Ended (terminal, irreversible)."""
    if not direct_ticketing_enabled(request.user):
        raise Http404()
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org, deleted_at__isnull=True), id=event_id)
    if event.status != EVENT_STATUS_LIVE:
        messages.error(request, 'Event is not in Live state.')
        return redirect('tickets:event_detail', event_id=event.id)
    event.status = EVENT_STATUS_ENDED
    event.save(update_fields=['status'])
    _invalidate_event_list_cache(org)
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

    if not direct_ticketing_enabled(request.user):
        raise Http404()

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

    if failed_count:
        messages.warning(
            request,
            f'"{event.name}" cancelled. {refunded_count} order(s) refunded. '
            f'{failed_count} refund(s) failed — please refund those orders manually.',
        )
    else:
        messages.success(request, f'"{event.name}" cancelled. {refunded_count} order(s) refunded.')
    return redirect('tickets:event_detail', event_id=event.id)


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
    if not direct_ticketing_enabled(request.user):
        raise Http404()
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
# Direct Ticket Selling — Public Views
# ---------------------------------------------------------------------------

def public_event_buy(request, event_id):
    """Public ticket selector page. POST stores cart in session and redirects to checkout."""
    event = get_object_or_404(
        Event.objects.select_related('venue', 'organization'),
        id=event_id,
        deleted_at__isnull=True,
    )
    eff = event.effective_status
    if eff == EVENT_STATUS_DRAFT:
        raise Http404()
    if eff == EVENT_STATUS_ENDED:
        return render(request, 'tickets/buy/sales_ended.html', {'event': event})
    if eff == EVENT_STATUS_CANCELLED:
        return render(request, 'tickets/buy/event_cancelled.html', {'event': event})

    ticket_types = SaleableTicketType.objects.filter(
        event=event,
        is_active=True,
    ).order_by('order', 'name')
    all_on_sale     = [tt for tt in ticket_types if tt.is_on_sale() and not tt.is_sold_out()]
    available_types = [tt for tt in all_on_sale if not tt.is_password_protected]
    locked_types    = [tt for tt in all_on_sale if tt.is_password_protected]
    all_types       = available_types + locked_types

    if request.method == 'POST':
        form = PublicTicketPurchaseForm(all_types, request.POST)
        if form.is_valid():
            line_items = form.get_line_items()
            snapshot = [
                {
                    'saleable_ticket_type_id': str(tt.id),
                    'name': tt.name,
                    'price': str(tt.price),
                    'quantity': qty,
                }
                for tt, qty in line_items
            ]
            request.session[f'cart_{event_id}'] = snapshot
            return redirect('tickets:checkout_payment', event_id=event_id)
    else:
        Event.objects.filter(pk=event.pk).update(
            public_buy_page_views=F('public_buy_page_views') + 1
        )
        form = PublicTicketPurchaseForm(all_types)

    all_pairs       = list(zip(all_types, form))
    available_pairs = all_pairs[:len(available_types)]
    locked_pairs    = all_pairs[len(available_types):]

    return render(request, 'tickets/buy/public_event_buy.html', {
        'event': event,
        'form': form,
        'available_pairs': available_pairs,
        'locked_pairs': locked_pairs,
    })


def unlock_ticket_type(request, event_id, ticket_type_id):
    """AJAX POST: validate password for a password-protected SaleableTicketType."""
    if request.method != 'POST':
        return JsonResponse({'success': False}, status=405)
    event = get_object_or_404(
        Event.objects.filter(deleted_at__isnull=True),
        id=event_id,
    )
    tt = get_object_or_404(SaleableTicketType, id=ticket_type_id, event=event, is_active=True)
    if not tt.is_password_protected or not tt.password:
        return JsonResponse({'success': False, 'error': 'No password set.'}, status=400)
    submitted = request.POST.get('password', '')
    if submitted == tt.password:
        return JsonResponse({'success': True})
    return JsonResponse({'success': False, 'error': 'Incorrect password.'})


@require_http_methods(["POST"])
def validate_promo_code(request, event_id):
    """Public AJAX endpoint — validates a promo code and stores it in the session."""
    event = get_object_or_404(Event, id=event_id)

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

    cart = request.session.get(f'cart_{event_id}', [])
    subtotal_cents = sum(
        int(Decimal(item['price']) * 100) * item['quantity']
        for item in cart
    )
    discount_cents = promo.calculate_discount_cents(subtotal_cents)

    request.session[f'promo_{event_id}'] = {
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
            return redirect('tickets:event_detail', event_id=event_id)
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
    return redirect('tickets:event_detail', event_id=event_id)


def checkout_payment(request, event_id):
    """Custom checkout page — collects buyer info and processes payment via Stripe Elements."""
    from django.conf import settings as django_settings

    event = get_object_or_404(
        Event.objects.select_related('venue', 'organization'),
        id=event_id,
    )

    if event.effective_status != EVENT_STATUS_LIVE:
        request.session.pop(f'cart_{event_id}', None)
        return redirect('tickets:public_event_buy', event_id=event_id)

    cart = request.session.get(f'cart_{event_id}')
    if not cart:
        return redirect('tickets:public_event_buy', event_id=event_id)

    total_cents = sum(
        int(Decimal(item['price']) * 100) * item['quantity']
        for item in cart
    )

    promo_session = request.session.get(f'promo_{event_id}')
    discount_cents = promo_session['discount_cents'] if promo_session else 0
    discounted_subtotal_cents = total_cents - discount_cents
    is_free = discounted_subtotal_cents == 0

    total_dollars = Decimal(total_cents) / 100

    if request.method == 'POST' and is_free:
        buyer_name = request.POST.get('buyer_name', '').strip()
        buyer_email = request.POST.get('buyer_email', '').strip().lower()
        if not buyer_name or not buyer_email:
            return render(request, 'tickets/buy/checkout_payment.html', {
                'event': event,
                'cart': cart,
                'total_cents': total_cents,
                'total_dollars': total_dollars,
                'is_free': is_free,
                'stripe_publishable_key': django_settings.STRIPE_PUBLISHABLE_KEY,
                'error': 'Please provide your name and email.',
            })

        with transaction.atomic():
            org = event.organization
            customer, _ = Customer.objects.get_or_create(
                email=buyer_email,
                defaults={'organization': org, 'name': buyer_name},
            )
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
                SaleableTicketType.objects.filter(id=tt_id).update(
                    quantity_sold=F('quantity_sold') + qty
                )
                Ticket.objects.bulk_create([
                    Ticket(
                        ticket_order=order,
                        ticket_type=item_name,
                        price=Decimal('0.00'),
                        tier=None,
                    )
                    for _ in range(qty)
                ])
            customer.update_lifetime_value()
            _invalidate_event_list_cache(org)

        from tickets.tasks import send_order_confirmation_email_task
        send_order_confirmation_email_task.delay(str(order.id))

        del request.session[f'cart_{event_id}']
        request.session.pop(f'promo_{event_id}', None)
        return redirect(f"{reverse_lazy('tickets:checkout_success')}?order_id={order.id}")

    user_name = ''
    user_email = ''
    saved_pm = None

    if request.user.is_authenticated:
        user = request.user
        user_name = user.get_full_name() or user.email
        user_email = user.email
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

    from tickets.utils import calculate_platform_fee_cents
    fee_cents = calculate_platform_fee_cents(discounted_subtotal_cents) if not is_free else 0
    grand_total_cents = discounted_subtotal_cents + fee_cents

    return render(request, 'tickets/buy/checkout_payment.html', {
        'event': event,
        'cart': cart,
        'total_cents': total_cents,
        'total_dollars': total_dollars,
        'is_free': is_free,
        'stripe_publishable_key': django_settings.STRIPE_PUBLISHABLE_KEY,
        'user_name': user_name,
        'user_email': user_email,
        'saved_pm': saved_pm,
        'user_is_authenticated': request.user.is_authenticated,
        'subtotal': Decimal(total_cents) / 100,
        'service_fee': Decimal(fee_cents) / 100,
        'grand_total': Decimal(grand_total_cents) / 100,
        'discount_cents': discount_cents,
        'discount_dollars': Decimal(discount_cents) / 100,
        'promo_applied': promo_session,
    })


@require_http_methods(["POST"])
def create_payment_intent(request, event_id):
    """JSON endpoint — creates a Stripe PaymentIntent and a StripeCheckoutSession record."""
    from django.conf import settings as django_settings
    import stripe as stripe_lib

    event = get_object_or_404(
        Event.objects.select_related('organization'),
        id=event_id,
    )

    if event.effective_status != EVENT_STATUS_LIVE:
        return JsonResponse({'error': 'Ticket sales for this event have ended.'}, status=400)

    cart = request.session.get(f'cart_{event_id}')
    if not cart:
        return JsonResponse({'error': 'No cart found. Please start over.'}, status=400)

    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid request.'}, status=400)

    buyer_name = (data.get('buyer_name') or '').strip()
    buyer_email = (data.get('buyer_email') or '').strip().lower()
    if not buyer_name or not buyer_email:
        return JsonResponse({'error': 'Name and email are required.'}, status=400)

    save_card    = bool(data.get('save_card', False))
    use_saved_pm = (data.get('use_saved_pm') or '').strip()

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

    total_cents = sum(
        int(Decimal(item['price']) * 100) * item['quantity']
        for item in cart
    )
    if total_cents == 0:
        return JsonResponse({'error': 'Use the free ticket flow for $0 orders.'}, status=400)

    # Re-validate and apply any promo code stored in the session
    promo_session = request.session.get(f'promo_{event_id}')
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

    from tickets.utils import calculate_platform_fee_cents
    fee_cents = calculate_platform_fee_cents(discounted_subtotal_cents)
    charge_cents = discounted_subtotal_cents + fee_cents  # buyer pays discounted subtotal + platform fee

    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY

    # Resolve or create a Stripe Customer for authenticated users
    stripe_customer_id = None
    if request.user.is_authenticated:
        try:
            profile = request.user.profile
            if profile.stripe_customer_id:
                stripe_customer_id = profile.stripe_customer_id
            else:
                cus = stripe_lib.Customer.create(email=buyer_email, name=buyer_name)
                stripe_customer_id = cus.id
                UserProfile.objects.filter(pk=profile.pk).update(stripe_customer_id=stripe_customer_id)
        except Exception as e:
            logger.error("Stripe Customer.create failed: %s", e)
            # non-fatal — card saving skipped, payment proceeds

    pi_kwargs = {
        'amount': charge_cents,
        'currency': django_settings.STRIPE_CURRENCY,
        'receipt_email': buyer_email,
        'metadata': {
            'event_id': str(event.id),
            'org_id': str(event.organization_id),
        },
    }
    if stripe_customer_id:
        pi_kwargs['customer'] = stripe_customer_id
    if save_card and stripe_customer_id:
        pi_kwargs['setup_future_usage'] = 'off_session'
        pi_kwargs['metadata']['user_id'] = str(request.user.pk)
    if use_saved_pm:
        pi_kwargs['payment_method'] = use_saved_pm

    try:
        pi = stripe_lib.PaymentIntent.create(**pi_kwargs)
    except Exception as e:
        logger.error("PaymentIntent creation failed: %s", e)
        return JsonResponse({'error': 'Could not initiate payment. Please try again.'}, status=500)

    session_record = StripeCheckoutSession.objects.create(
        event=event,
        organization=event.organization,
        stripe_session_id=pi.id,
        stripe_payment_intent_id=pi.id,
        buyer_email=buyer_email,
        buyer_name=buyer_name,
        status=StripeCheckoutSession.Status.PENDING,
        line_items_snapshot=cart,
        amount_total_cents=charge_cents,
        platform_fee_cents=fee_cents,
        promo_code_id=promo_code_id,
        discount_cents=discount_cents,
    )

    return JsonResponse({
        'client_secret': pi.client_secret,
        'session_id': str(session_record.id),
    })


def checkout_success(request):
    """Post-payment landing page — supports ?session_id=<uuid> (paid) and ?order_id=<uuid> (free)."""
    session_obj = None
    order_obj = None

    session_id = request.GET.get('session_id', '')
    order_id = request.GET.get('order_id', '')

    if session_id:
        # Paid flow: session_id is our DB record UUID
        session_obj = StripeCheckoutSession.objects.filter(
            id=session_id
        ).select_related('ticket_order', 'event').first()
    elif order_id:
        # Free flow: look up TicketOrder directly
        order_obj = TicketOrder.objects.filter(
            id=order_id
        ).select_related('event', 'customer').first()

    # Redirect authenticated users to My Tickets when the order is confirmed
    if request.user.is_authenticated:
        if order_obj:
            messages.success(request, "Your tickets are confirmed!")
            return redirect('tickets:my_tickets')
        if session_obj and session_obj.status == StripeCheckoutSession.Status.COMPLETED:
            messages.success(request, "Your tickets are confirmed!")
            return redirect('tickets:my_tickets')

    qr_code = ''
    if order_obj and not order_obj.refunded_at:
        qr_code = generate_qr_b64(order_obj.order_number)
    elif session_obj and session_obj.ticket_order and not session_obj.ticket_order.refunded_at:
        qr_code = generate_qr_b64(session_obj.ticket_order.order_number)

    _event_for_pixel = order_obj.event if order_obj else (session_obj.event if session_obj else None)

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

    return render(request, 'tickets/buy/checkout_success.html', {
        'session': session_obj,
        'order': order_obj,
        'qr_code': qr_code,
        'pixel_id': _event_for_pixel.facebook_pixel_id if _event_for_pixel else '',
        'pixel_content_ids': pixel_content_ids,
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


def _fulfill_payment_intent(payment_intent):
    """
    Idempotently fulfill a succeeded PaymentIntent.
    Buyer info is pre-populated in our DB record at PI creation time.
    """
    pi_id = payment_intent['id']
    amount_total_cents = payment_intent.get('amount_received', 0) or 0

    # Idempotency check #1: outside lock
    session_obj = StripeCheckoutSession.objects.filter(stripe_session_id=pi_id).first()
    if session_obj and session_obj.status == StripeCheckoutSession.Status.COMPLETED:
        logger.info("Stripe webhook: PaymentIntent %s already fulfilled", pi_id)
        return

    if not session_obj:
        logger.warning("Stripe webhook: no StripeCheckoutSession for PaymentIntent %s — skipping", pi_id)
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
            defaults={'organization': org, 'name': name},
        )

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

            SaleableTicketType.objects.filter(id=tt_id).update(
                quantity_sold=F('quantity_sold') + qty
            )

            Ticket.objects.bulk_create([
                Ticket(
                    ticket_order=order,
                    ticket_type=item_name,
                    price=item_price,
                    tier=None,
                )
                for _ in range(qty)
            ])

        customer.update_lifetime_value()

        session_obj.status = StripeCheckoutSession.Status.COMPLETED
        session_obj.ticket_order = order
        session_obj.fulfilled_at = django_tz.now()
        session_obj.save()

        _invalidate_event_list_cache(org)

        from tickets.tasks import send_order_confirmation_email_task
        send_order_confirmation_email_task.delay(str(order.id))

        if payment_intent.get('setup_future_usage') == 'off_session':
            user_id = payment_intent.get('metadata', {}).get('user_id', '')
            pm_id   = payment_intent.get('payment_method', '')
            if user_id and pm_id:
                import stripe as stripe_lib_inner
                from django.conf import settings as django_settings_inner
                stripe_lib_inner.api_key = django_settings_inner.STRIPE_SECRET_KEY
                try:
                    pm   = stripe_lib_inner.PaymentMethod.retrieve(pm_id)
                    card = pm.get('card', {})
                    UserProfile.objects.filter(user_id=user_id).update(
                        stripe_pm_id=pm_id,
                        stripe_pm_brand=card.get('brand', ''),
                        stripe_pm_last4=card.get('last4', ''),
                    )
                    logger.info("Saved PaymentMethod %s for user %s", pm_id, user_id)
                except Exception as e:
                    logger.error("Failed to save PaymentMethod %s for user %s: %s", pm_id, user_id, e)

    logger.info("Fulfilled PaymentIntent %s — order %s", pi_id, order.order_number)


def _fail_payment_intent(payment_intent):
    """Mark a failed PaymentIntent session as canceled."""
    pi_id = payment_intent.get('id', '')
    if pi_id:
        StripeCheckoutSession.objects.filter(
            stripe_session_id=pi_id,
            status=StripeCheckoutSession.Status.PENDING,
        ).update(status=StripeCheckoutSession.Status.CANCELED)
        logger.info("PaymentIntent %s marked canceled (payment failed)", pi_id)

# ---------------------------------------------------------------------------
# Attendee Auth Views (public — no login required)
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
    session_data = request.session.get("verify_org_signup")
    if not session_data:
        return redirect('tickets:attendee_signup', org_slug=org_slug)
    phone = session_data["phone"]

    if request.method == "POST":
        form = OTPVerificationForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data["otp_code"]
            if not check_phone_verification(phone, code):
                messages.error(request, 'Incorrect or expired code. Please try again.')
            else:
                user = AuthUser.objects.create(
                    username=phone,
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

    session_data = request.session.get("verify_login")
    if not session_data:
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
                    user = AuthUser.objects.get(username=phone)
                except AuthUser.DoesNotExist:
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
    """Attendee dashboard — shows upcoming purchasable events."""
    from .models import TICKETING_TYPE_DIRECT
    today = django_tz.now().date()
    events_qs = (
        Event.objects.filter(
            deleted_at__isnull=True,
            start_date__gte=today,
            ticketing_type=TICKETING_TYPE_DIRECT,
            status=EVENT_STATUS_LIVE,
        )
        .select_related('venue')
        .order_by('start_date', 'start_time', 'name')
    )
    paginator = Paginator(events_qs, 24)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'tickets/attendee_dashboard.html', {'page_obj': page_obj})


@login_required
def my_tickets(request):
    """Attendee order history — shows all ticket orders for the logged-in user."""
    orders = (
        TicketOrder.objects
        .filter(customer__email=request.user.email)
        .select_related('event', 'event__venue', 'customer')
        .prefetch_related('tickets')
        .order_by('-order_date')
    )
    paginator = Paginator(orders, 12)
    page_obj = paginator.get_page(request.GET.get('page'))
    for order in page_obj:
        order.qr_code = generate_qr_b64(order.order_number) if not order.refunded_at else ''
    return render(request, 'tickets/my_tickets.html', {'page_obj': page_obj})


@login_required
def my_ticket_detail(request, order_id):
    """Ticket detail page for a single order — shows QR code and full order info."""
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


# ---------------------------------------------------------------------------
# Member Role Update
# ---------------------------------------------------------------------------

@login_required
@require_org
@require_owner
@require_http_methods(["POST"])
def member_role_update(request, profile_id):
    """Owner updates the system role and org role of an org member."""
    org = get_organization(request)
    profile = get_object_or_404(
        UserProfile.objects.select_related('user').filter(organization=org),
        id=profile_id,
    )
    if profile.user == request.user:
        messages.error(request, 'You cannot change your own role.')
        return redirect('tickets:member_list')
    new_role = request.POST.get('role', '').strip()
    new_org_role = request.POST.get('org_role', '').strip()
    valid_roles = [r[0] for r in UserProfile.Role.choices]
    valid_org_roles = [r[0] for r in UserProfile.OrgRole.choices]
    update_fields = []
    if new_role and new_role not in valid_roles:
        messages.error(request, 'Invalid role.')
        return redirect('tickets:member_list')
    if new_org_role and new_org_role not in valid_org_roles:
        messages.error(request, 'Invalid org role.')
        return redirect('tickets:member_list')
    if new_role:
        profile.role = new_role
        update_fields.append('role')
    if new_org_role:
        profile.org_role = new_org_role
        update_fields.append('org_role')
    if update_fields:
        profile.save(update_fields=update_fields)
        messages.success(request, 'Member roles updated.')
    return redirect('tickets:member_list')


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

    paid_out = Payout.objects.filter(
        organization=org, status=Payout.Status.COMPLETED,
    ).aggregate(total=Coalesce(Sum('amount'), Decimal('0.00')))['total']

    organizer_revenue = stripe_revenue - platform_fees
    return stripe_revenue, platform_fees, paid_out, organizer_revenue - paid_out


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def finance_overview(request):
    """Finance overview: revenue stats, bank account status, payout history."""
    org = get_organization(request)

    gross = TicketOrder.objects.filter(
        customer__organization=org,
        stripe_checkout_session__isnull=False,
    ).aggregate(total=Coalesce(Sum('total_amount'), Decimal('0.00')))['total']

    stripe_revenue, platform_fees, paid_out, available_balance = _compute_available_balance(org)

    recent_txns = list(
        StripeCheckoutSession.objects
        .filter(organization=org, status=StripeCheckoutSession.Status.COMPLETED)
        .select_related('ticket_order', 'event')
        .order_by('-fulfilled_at')[:20]
    )
    for s in recent_txns:
        s.amount_dollars = Decimal(str(s.amount_total_cents)) / 100

    payout_history = (
        Payout.objects.filter(organization=org)
        .select_related('initiated_by')
    )

    context = {
        'gross_revenue': gross,
        'stripe_revenue': stripe_revenue,
        'platform_fees': platform_fees,
        'paid_out': paid_out,
        'available_balance': available_balance,
        'recent_transactions': recent_txns,
        'payout_history': payout_history,
        'onboarding_complete': org.stripe_onboarding_complete,
        'has_stripe_account': bool(org.stripe_account_id),
        'min_payout': _MIN_PAYOUT,
    }
    return render(request, 'tickets/finance/overview.html', context)


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


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def stripe_connect_return(request):
    """Stripe redirects here after the organizer completes (or abandons) onboarding."""
    import stripe as stripe_lib
    from django.conf import settings as django_settings
    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
    org = get_organization(request)

    if org.stripe_account_id:
        try:
            account = stripe_lib.Account.retrieve(org.stripe_account_id)
            if account.details_submitted and account.charges_enabled:
                org.stripe_onboarding_complete = True
                org.save(update_fields=['stripe_onboarding_complete'])
                messages.success(request, 'Bank account connected successfully. You can now request payouts.')
            else:
                messages.warning(request, 'Onboarding incomplete. Please finish connecting your bank account.')
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
@require_owner
@require_http_methods(["POST"])
def initiate_payout(request):
    """Initiate a Stripe Transfer from the platform balance to the organizer's account."""
    import stripe as stripe_lib
    from django.conf import settings as django_settings
    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
    org = get_organization(request)

    if not org.stripe_onboarding_complete or not org.stripe_account_id:
        messages.error(request, 'Please connect your bank account before requesting a payout.')
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

    _, _, available_balance = _compute_available_balance(org)
    if amount > available_balance:
        messages.error(request, f'Payout amount exceeds available balance (${available_balance:.2f}).')
        return redirect('tickets:finance_overview')

    if Payout.objects.filter(organization=org, status=Payout.Status.PENDING).exists():
        messages.error(request, 'A payout is already pending for your organization.')
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
        transfer = stripe_lib.Transfer.create(
            amount=int(amount * 100),
            currency=django_settings.STRIPE_CURRENCY,
            destination=org.stripe_account_id,
            description=f'Payout to {org.name}',
            metadata={'org_id': str(org.id), 'payout_id': str(payout.id)},
        )
        payout.stripe_transfer_id = transfer.id
        payout.status = Payout.Status.COMPLETED
        payout.save(update_fields=['stripe_transfer_id', 'status'])
        messages.success(request, f'Payout of ${amount:.2f} initiated. Transfer: {transfer.id}')
    except stripe_lib.error.StripeError as e:
        payout.status = Payout.Status.FAILED
        error_note = f' [Stripe error: {str(e)[:400]}]'
        payout.notes = (payout.notes + error_note)[:500]
        payout.save(update_fields=['status', 'notes'])
        messages.error(request, f'Payout failed: {getattr(e, "user_message", None) or str(e)}')

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

        for event_id, resp_ids in by_event.items():
            if event_id and event_id in valid_event_ids:
                ExternalSurveyResponse.objects.filter(id__in=resp_ids).update(event_id=event_id)
            else:
                ExternalSurveyResponse.objects.filter(id__in=resp_ids).update(event=None)

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
