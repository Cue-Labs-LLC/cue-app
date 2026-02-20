import calendar
import os
import json
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
from django.db.models.functions import Coalesce
from django.db import models
from django.core.paginator import Paginator
from django.http import JsonResponse, Http404, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.db import connection, transaction
from django.utils import timezone as django_tz
import pandas as pd

from .models import (
    Organization, UserProfile, OrganizationInvitation,
    CSVFormat, UploadedFile, Customer, Event, EventExpense, EventTalent, TicketOrder, Ticket, Venue,
    CustomField, EventCustomFieldValue, IncomeSource, EventIncome,
    SurveyQuestion, SurveyInvitation, SurveyResponse, SurveyAnswer,
    PipedreamCalendarConnection,
    SaleableTicketType, StripeCheckoutSession,
)
from .forms import (
    EventCSVUploadForm, EventExpenseForm, TicketPriceEntryForm, CSVFormatForm,
    VenueForm, EventForm, EventTalentFormSet, LoginForm,
    IncomeSourceForm, EventIncomeForm,
    SignUpForm, OTPVerificationForm, MemberInviteForm,
    SaleableTicketTypeForm, PublicTicketPurchaseForm,
    DirectEventForm, DirectTicketTypeFormSet,
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
from .utils import get_organization, require_org, clear_org_cache
from .feature_flags import direct_ticketing_enabled

from django.core.cache import cache as django_cache

import logging
logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Sign-Up with Email OTP Verification
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "POST"])
def signup_view(request):
    """Step 1: Collect email, name, and password; send OTP."""
    import random
    from django.contrib.auth.hashers import make_password
    from .models import EmailOTP
    from .tasks import send_otp_email_task

    if request.user.is_authenticated:
        return redirect('tickets:home')

    # Preserve next (e.g. invite accept URL) through signup flow for redirect after verify
    next_url = request.GET.get('next')
    if next_url:
        from django.utils.http import url_has_allowed_host_and_scheme
        from django.conf import settings
        if url_has_allowed_host_and_scheme(next_url, allowed_hosts=settings.ALLOWED_HOSTS):
            request.session['invite_accept_next'] = next_url

    if request.method == 'POST':
        form = SignUpForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email'].lower()

            # Rate limit: max 3 OTP sends per email per 30 minutes
            thirty_min_ago = django_tz.now() - timedelta(minutes=30)
            recent_count = EmailOTP.objects.filter(
                email__iexact=email,
                purpose=EmailOTP.Purpose.SIGNUP,
                created_at__gte=thirty_min_ago,
            ).count()
            if recent_count >= 3:
                messages.error(
                    request,
                    'Too many verification attempts. Please try again later.'
                )
                return redirect('tickets:signup')

            otp_code = f'{random.randint(0, 999999):06d}'
            otp = EmailOTP.objects.create(
                email=email,
                otp_code=otp_code,
                purpose=EmailOTP.Purpose.SIGNUP,
                signup_data={
                    'email': email,
                    'first_name': form.cleaned_data['first_name'],
                    'last_name': form.cleaned_data['last_name'],
                    'password': make_password(form.cleaned_data['password1']),
                },
            )

            send_otp_email_task.delay(str(otp.id))

            request.session['otp_id'] = str(otp.id)
            # Always redirect to verify — same behaviour whether email exists or not
            return redirect('tickets:verify_otp')
    else:
        form = SignUpForm()

    return render(request, 'tickets/auth/signup.html', {'form': form})


@require_http_methods(["GET", "POST"])
def verify_otp_view(request):
    """Step 2: Verify the OTP code and create the user account."""
    from django.contrib.auth import login
    from django.contrib.auth.models import User
    from .models import EmailOTP

    if request.user.is_authenticated:
        return redirect('tickets:home')

    otp_id = request.session.get('otp_id')
    if not otp_id:
        messages.error(request, 'No pending verification. Please sign up first.')
        return redirect('tickets:signup')

    try:
        otp = EmailOTP.objects.get(id=otp_id)
    except EmailOTP.DoesNotExist:
        messages.error(request, 'Verification session expired. Please sign up again.')
        return redirect('tickets:signup')

    if not otp.is_usable():
        msg = 'Code expired.' if otp.is_expired() else 'Too many attempts.'
        messages.error(request, f'{msg} Please sign up again.')
        request.session.pop('otp_id', None)
        return redirect('tickets:signup')

    # Mask email for display (e.g. t***@example.com)
    email = otp.email
    local, domain = email.split('@', 1)
    masked_email = local[0] + '***@' + domain if len(local) > 1 else email

    if request.method == 'POST':
        form = OTPVerificationForm(request.POST)
        if form.is_valid():
            otp.attempts += 1
            otp.save(update_fields=['attempts'])

            if form.cleaned_data['otp_code'] != otp.otp_code:
                remaining = 5 - otp.attempts
                if remaining <= 0:
                    messages.error(request, 'Too many failed attempts. Please sign up again.')
                    request.session.pop('otp_id', None)
                    return redirect('tickets:signup')
                messages.error(request, f'Invalid code. {remaining} attempt(s) remaining.')
                return render(request, 'tickets/auth/verify_otp.html', {
                    'form': form, 'masked_email': masked_email,
                })

            # Code matches — mark verified and create user
            otp.is_verified = True
            otp.save(update_fields=['is_verified'])

            data = otp.signup_data

            # Final uniqueness check (race condition guard)
            if User.objects.filter(email__iexact=data['email']).exists():
                messages.info(request, 'An account with this email already exists. Please log in.')
                request.session.pop('otp_id', None)
                return redirect('tickets:login')

            user = User.objects.create(
                username=data['email'],
                email=data['email'],
                first_name=data['first_name'],
                last_name=data['last_name'],
                password=data['password'],  # already hashed via make_password
            )
            UserProfile.objects.create(user=user)

            login(request, user, backend='tickets.backends.EmailBackend')
            request.session.pop('otp_id', None)
            next_url = request.session.pop('invite_accept_next', None)
            if next_url:
                from django.utils.http import url_has_allowed_host_and_scheme
                from django.conf import settings
                if url_has_allowed_host_and_scheme(next_url, allowed_hosts=settings.ALLOWED_HOSTS):
                    messages.success(request, 'Account created! Use the link below to join the organization.')
                    return redirect(next_url)
            messages.success(request, 'Account created! Please create or join an organization to get started.')
            return redirect('tickets:create_organization')
    else:
        form = OTPVerificationForm()

    return render(request, 'tickets/auth/verify_otp.html', {
        'form': form, 'masked_email': masked_email,
    })


@require_http_methods(["POST"])
def resend_otp_view(request):
    """Resend a new OTP code for the current signup session."""
    import random
    from .models import EmailOTP
    from .tasks import send_otp_email_task

    if request.user.is_authenticated:
        return redirect('tickets:home')

    otp_id = request.session.get('otp_id')
    if not otp_id:
        return redirect('tickets:signup')

    try:
        old_otp = EmailOTP.objects.get(id=otp_id)
    except EmailOTP.DoesNotExist:
        return redirect('tickets:signup')

    # Rate limit: max 3 per email per 30 min
    thirty_min_ago = django_tz.now() - timedelta(minutes=30)
    recent_count = EmailOTP.objects.filter(
        email__iexact=old_otp.email,
        purpose=EmailOTP.Purpose.SIGNUP,
        created_at__gte=thirty_min_ago,
    ).count()
    if recent_count >= 3:
        messages.error(request, 'Too many verification attempts. Please try again later.')
        return redirect('tickets:verify_otp')

    new_code = f'{random.randint(0, 999999):06d}'
    new_otp = EmailOTP.objects.create(
        email=old_otp.email,
        otp_code=new_code,
        purpose=EmailOTP.Purpose.SIGNUP,
        signup_data=old_otp.signup_data,
    )

    send_otp_email_task.delay(str(new_otp.id))
    request.session['otp_id'] = str(new_otp.id)
    messages.success(request, 'A new verification code has been sent.')
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
        return redirect('tickets:home')
    return render(request, 'tickets/landing.html')


def explore(request):
    """Public page: list upcoming events with direct ticketing (no login required)."""
    from .models import TICKETING_TYPE_DIRECT
    today = django_tz.now().date()
    events_qs = (
        Event.objects.filter(
            deleted_at__isnull=True,
            start_date__gte=today,
            ticketing_type=TICKETING_TYPE_DIRECT,
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
            org = form.save()
            profile.organization = org
            profile.save()
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
    }
    return render(request, 'tickets/member_list.html', context)


@login_required
@require_org
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
        signup_url = reverse('tickets:signup')
        invite_url = request.build_absolute_uri()
        return redirect(f'{signup_url}?{urlencode({"next": invite_url})}')

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
    profile.save()
    invitation.status = OrganizationInvitation.Status.ACCEPTED
    invitation.accepted_at = django_tz.now()
    invitation.accepted_by = request.user
    invitation.save(update_fields=['status', 'accepted_at', 'accepted_by'])
    clear_org_cache(request)
    messages.success(request, f"You've joined {invitation.organization.name}. Welcome!")
    return redirect('tickets:home')


@login_required
@require_org
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
    for ev in page_obj:
        if ev.upload_count != 0:
            continue
        end_date = ev.end_date or ev.start_date
        end_time = ev.end_time or ev.start_time or time(23, 59, 59)
        event_end = datetime.combine(end_date, end_time)
        if now_local > event_end:
            event_ids_show_warning.add(ev.id)

    # Summary statistics (org-scoped via Event/Customer/UploadedFile)
    total_customers = Customer.objects.filter(organization=org).count()
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
            _invalidate_event_list_cache(uploaded_file.organization)

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
    # Chart: left to right earliest → most recent (calculator order)
    chart_events = result['events']
    chart_data = json.dumps(chart_events, default=str)
    # Table: top to bottom most recent → earliest
    table_events = list(reversed(chart_events))
    return render(request, 'tickets/repeat_customers.html', {
        'events': table_events,
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
    for ev in page_obj:
        if ev.upload_count != 0:
            continue
        end_date = ev.end_date or ev.start_date
        end_time = ev.end_time or ev.start_time or time(23, 59, 59)
        event_end = datetime.combine(end_date, end_time)
        if now_local > event_end:
            event_ids_show_warning.add(ev.id)

    context = {
        'page_obj': page_obj,
        'search_query': search_query,
        'sort_by': sort_by,
        'event_ids_show_warning': event_ids_show_warning,
    }
    response = render(request, 'tickets/event_list.html', context)
    try:
        django_cache.set(cache_key, response.content, 300)
    except Exception:
        pass
    return response


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
    }
    if event.ticketing_type == 'direct':
        context['dashboard_sessions'] = (
            StripeCheckoutSession.objects.filter(event=event)
            .select_related('ticket_order')
            .order_by('-created_at')[:50]
        )
        context['direct_total_revenue'] = sum(
            tt.quantity_sold * tt.price for tt in saleable_ticket_types_list
        )
        context['public_buy_url'] = request.build_absolute_uri(f'/buy/{event.id}/')
        views = getattr(event, 'public_buy_page_views', 0) or 0
        context['conversion_rate_pct'] = (
            round(total_orders / views * 100, 1) if views > 0 else None
        )
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
    from django.conf import settings as django_settings
    context = {
        'form': form,
        'google_maps_api_key': django_settings.GOOGLE_MAPS_API_KEY,
    }
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
    from django.conf import settings as django_settings
    context = {
        'form': form,
        'venue': venue,
        'google_maps_api_key': django_settings.GOOGLE_MAPS_API_KEY,
    }
    return render(request, 'tickets/venue_edit.html', context)


@login_required
@require_org
def event_type_select(request):
    """Landing page to choose Direct or External ticketing before creating an event."""
    return render(request, 'tickets/event_type_select.html', {})


@login_required
@require_org
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
            form = DirectEventForm(request.POST, request.FILES)
            ticket_formset = DirectTicketTypeFormSet(
                request.POST,
                queryset=SaleableTicketType.objects.none(),
                prefix='ticket_type',
            )
            if form.is_valid() and ticket_formset.is_valid():
                venue_name = form.cleaned_data['venue_name']
                venue_address = form.cleaned_data.get('venue_address', '')
                venue, _ = Venue.objects.get_or_create(
                    organization=org,
                    name=venue_name,
                    city='',
                    defaults={'street_address': venue_address},
                )
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
                if event.start_date >= date.today():
                    _sync_event_to_google_calendar(event)
                return redirect('tickets:event_detail', event_id=event.id)
        else:
            form = DirectEventForm()
            ticket_formset = DirectTicketTypeFormSet(
                queryset=SaleableTicketType.objects.none(),
                prefix='ticket_type',
            )
        context = {
            'form': form,
            'ticket_formset': ticket_formset,
            'ticketing_type': ticketing_type,
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
def event_edit(request, event_id):
    """Edit an existing event."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
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


@login_required
@require_org
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


# ---------------------------------------------------------------------------
# Event Expense Views
# ---------------------------------------------------------------------------

@login_required
@require_org
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
def income_source_list(request):
    """List all income source types for the organization."""
    org = get_organization(request)
    sources = IncomeSource.objects.filter(organization=org).order_by('order', 'name')
    context = {'sources': sources}
    return render(request, 'tickets/income_source_list.html', context)


@login_required
@require_org
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
def profitability_overview(request):
    """Analytics page: org-wide P&L stats, chart, and sortable table."""
    org = get_organization(request)

    events = (
        Event.objects.filter(organization=org)
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

    # Chart data — only events with revenue or expenses
    chart_events = [r for r in event_rows if r['revenue'] > 0 or r['expenses'] > 0]
    chart_data = {
        'labels': [r['event'].name for r in chart_events],
        'revenue': [float(r['revenue']) for r in chart_events],
        'expenses': [float(r['expenses']) for r in chart_events],
        'profit': [float(r['profit']) for r in chart_events],
    }

    context = {
        'event_rows': event_rows,
        'summary_revenue': summary_revenue,
        'summary_expenses': summary_expenses,
        'summary_profit': summary_profit,
        'summary_margin': summary_margin,
        'chart_data_json': json.dumps(chart_data),
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
@require_http_methods(["GET", "POST"])
def saleable_ticket_type_edit(request, event_id, ticket_type_id):
    """Edit an existing SaleableTicketType."""
    if not direct_ticketing_enabled(request.user):
        raise Http404()
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    tt = get_object_or_404(SaleableTicketType.objects.filter(event=event), id=ticket_type_id)
    price_locked = tt.quantity_sold > 0

    if request.method == 'POST':
        form = SaleableTicketTypeForm(request.POST, instance=tt, price_locked=price_locked)
        if form.is_valid():
            if price_locked:
                # Prevent price changes after tickets have been sold
                form.instance.price = tt.price
            updated = form.save()
            _invalidate_event_list_cache(org)
            messages.success(request, f'Ticket type "{updated.name}" updated.')
            return redirect('tickets:event_detail', event_id=event.id)
    else:
        form = SaleableTicketTypeForm(instance=tt, price_locked=price_locked)

    return render(request, 'tickets/saleable_ticket_type_form.html', {
        'form': form,
        'event': event,
        'ticket_type': tt,
        'editing': True,
        'price_locked': price_locked,
    })


@login_required
@require_org
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
@require_http_methods(["POST"])
def event_flyer_upload(request, event_id):
    """Upload or replace event flyer (direct ticketing only). Returns JSON."""
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
    try:
        event.flyer = file
        event.save(update_fields=['flyer'])
    except Exception as e:
        logger.warning("Flyer upload validation failed: %s", e)
        return JsonResponse({'success': False, 'error': 'Invalid or unsupported image.'}, status=400)
    return JsonResponse({'success': True, 'url': event.flyer.url})


# ---------------------------------------------------------------------------
# Direct Ticket Selling — Public Views
# ---------------------------------------------------------------------------

def public_event_buy(request, event_id):
    """Public ticket selector page. POST creates a Stripe Checkout session."""
    from django.conf import settings as django_settings
    import stripe as stripe_lib

    event = get_object_or_404(
        Event.objects.select_related('venue', 'organization'),
        id=event_id,
    )
    now = django_tz.now()
    ticket_types = SaleableTicketType.objects.filter(
        event=event,
        is_active=True,
    ).order_by('order', 'name')
    # Filter to only those currently on sale
    available_types = [tt for tt in ticket_types if tt.is_on_sale() and not tt.is_sold_out()]

    if request.method == 'POST':
        form = PublicTicketPurchaseForm(available_types, request.POST)
        if form.is_valid():
            line_items = form.get_line_items()

            stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
            site_url = django_settings.SITE_URL

            stripe_line_items = []
            snapshot = []
            for tt, qty in line_items:
                stripe_line_items.append({
                    'price_data': {
                        'currency': django_settings.STRIPE_CURRENCY,
                        'unit_amount': int(tt.price * 100),
                        'product_data': {
                            'name': f"{event.name} — {tt.name}",
                        },
                    },
                    'quantity': qty,
                })
                snapshot.append({
                    'saleable_ticket_type_id': str(tt.id),
                    'name': tt.name,
                    'price': str(tt.price),
                    'quantity': qty,
                })

            try:
                session = stripe_lib.checkout.Session.create(
                    payment_method_types=['card'],
                    line_items=stripe_line_items,
                    mode='payment',
                    success_url=f"{site_url}/checkout/success/?session_id={{CHECKOUT_SESSION_ID}}",
                    cancel_url=f"{site_url}/checkout/cancel/?session_id={{CHECKOUT_SESSION_ID}}",
                    metadata={'event_id': str(event.id), 'org_id': str(event.organization_id)},
                )
            except Exception as e:
                logger.error("Stripe session creation failed: %s", e)
                messages.error(request, 'Could not create checkout session. Please try again.')
                return render(request, 'tickets/buy/public_event_buy.html', {
                    'event': event,
                    'form': form,
                    'available_types': available_types,
                })

            amount_total_cents = sum(
                int(tt.price * 100) * qty for tt, qty in line_items
            )
            StripeCheckoutSession.objects.create(
                event=event,
                organization=event.organization,
                stripe_session_id=session.id,
                status=StripeCheckoutSession.Status.PENDING,
                line_items_snapshot=snapshot,
                amount_total_cents=amount_total_cents,
            )
            return redirect(session.url)
    else:
        Event.objects.filter(pk=event.pk).update(
            public_buy_page_views=F('public_buy_page_views') + 1
        )
        form = PublicTicketPurchaseForm(available_types)

    # Pair each ticket type with its BoundField for clean template iteration
    ticket_form_pairs = list(zip(available_types, form))

    return render(request, 'tickets/buy/public_event_buy.html', {
        'event': event,
        'form': form,
        'available_types': available_types,
        'ticket_form_pairs': ticket_form_pairs,
    })


def checkout_success(request):
    """Post-payment landing page shown after Stripe redirects back."""
    session_id = request.GET.get('session_id', '')
    session_obj = None
    if session_id:
        session_obj = StripeCheckoutSession.objects.filter(
            stripe_session_id=session_id
        ).select_related('ticket_order', 'event').first()
    return render(request, 'tickets/buy/checkout_success.html', {
        'session': session_obj,
    })


def checkout_cancel(request):
    """Buyer abandoned — mark session canceled if still pending."""
    session_id = request.GET.get('session_id', '')
    if session_id:
        StripeCheckoutSession.objects.filter(
            stripe_session_id=session_id,
            status=StripeCheckoutSession.Status.PENDING,
        ).update(status=StripeCheckoutSession.Status.CANCELED)
    return render(request, 'tickets/buy/checkout_cancel.html', {})


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
    if event_type == 'checkout.session.completed':
        _fulfill_checkout(event['data']['object'])
    elif event_type in ('checkout.session.expired',):
        _expire_checkout(event['data']['object'])

    return HttpResponse(status=200)


def _fulfill_checkout(stripe_session):
    """
    Idempotently fulfill a completed Stripe Checkout session.
    Creates Customer, TicketOrder, and Ticket records; updates LTV.
    """
    session_id = stripe_session['id']
    amount_total_cents = stripe_session.get('amount_total', 0) or 0
    payment_intent_id = stripe_session.get('payment_intent', '') or ''
    buyer_email = (stripe_session.get('customer_details', {}) or {}).get('email', '') or ''
    buyer_name = (stripe_session.get('customer_details', {}) or {}).get('name', '') or ''

    # Idempotency check #1: outside lock
    session_obj = StripeCheckoutSession.objects.filter(stripe_session_id=session_id).first()
    if session_obj and session_obj.status == StripeCheckoutSession.Status.COMPLETED:
        logger.info("Stripe webhook: session %s already fulfilled", session_id)
        return

    if not session_obj:
        logger.warning("Stripe webhook: no StripeCheckoutSession for %s — skipping", session_id)
        return

    with transaction.atomic():
        # Lock the row; re-check inside the lock (idempotency check #2)
        session_obj = StripeCheckoutSession.objects.select_for_update().get(pk=session_obj.pk)
        if session_obj.status == StripeCheckoutSession.Status.COMPLETED:
            return

        org = session_obj.organization
        event = session_obj.event

        # Update buyer info from Stripe if not already set
        if buyer_email and not session_obj.buyer_email:
            session_obj.buyer_email = buyer_email.lower()
        if buyer_name and not session_obj.buyer_name:
            session_obj.buyer_name = buyer_name
        if payment_intent_id:
            session_obj.stripe_payment_intent_id = payment_intent_id
        if amount_total_cents:
            session_obj.amount_total_cents = amount_total_cents

        email = session_obj.buyer_email or buyer_email.lower()
        name = session_obj.buyer_name or buyer_name or email

        customer, _ = Customer.objects.get_or_create(
            email=email.lower(),
            defaults={'organization': org, 'name': name},
        )

        order_number = f"STRIPE-{session_id}"[:100]
        order = TicketOrder.objects.create(
            customer=customer,
            event=event,
            uploaded_file=None,
            order_number=order_number,
            order_date=django_tz.now(),
            total_amount=Decimal(str(amount_total_cents)) / 100,
        )

        for item in session_obj.line_items_snapshot:
            tt_id = item.get('saleable_ticket_type_id')
            qty = item.get('quantity', 1)
            item_name = item.get('name', '')
            item_price = Decimal(str(item.get('price', '0')))

            # Atomic increment quantity_sold
            SaleableTicketType.objects.filter(id=tt_id).update(
                quantity_sold=F('quantity_sold') + qty
            )

            tickets_to_create = [
                Ticket(
                    ticket_order=order,
                    ticket_type=item_name,
                    price=item_price,
                    tier=None,
                )
                for _ in range(qty)
            ]
            Ticket.objects.bulk_create(tickets_to_create)

        customer.update_lifetime_value()

        session_obj.status = StripeCheckoutSession.Status.COMPLETED
        session_obj.ticket_order = order
        session_obj.fulfilled_at = django_tz.now()
        session_obj.save()

        _invalidate_event_list_cache(org)

    logger.info("Fulfilled Stripe session %s — order %s", session_id, order.order_number)


def _expire_checkout(stripe_session):
    """Mark an expired Stripe session."""
    session_id = stripe_session.get('id', '')
    if session_id:
        StripeCheckoutSession.objects.filter(
            stripe_session_id=session_id,
            status=StripeCheckoutSession.Status.PENDING,
        ).update(status=StripeCheckoutSession.Status.EXPIRED)
        logger.info("Stripe session %s marked expired", session_id)
