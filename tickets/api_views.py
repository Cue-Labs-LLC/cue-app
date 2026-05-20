"""
Mobile API views for the Cue app.
All views are plain Django function-based views decorated with DRF's @api_view.
No DRF ViewSets are used, in keeping with the project's FBV-only rule.
"""
import logging
from decimal import Decimal, InvalidOperation

from django.contrib.auth import authenticate
from django.core.cache import cache as django_cache
from django.db import transaction
from django.db.models import Count, DecimalField, F, IntegerField, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

from .models import (
    Customer,
    Event,
    EventCustomFieldValue,
    EventExpense,
    EventIncome,
    OrganizationAPIKey,
    ReceiptSend,
    SaleableTicketType,
    ScannerSession,
    TapToPayTermsAcceptance,
    Ticket,
    TicketOrder,
    TICKETING_TYPE_DIRECT,
    UserProfile,
)
from .utils import next_order_number

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_org_from_user(user):
    """For token-authenticated requests — bypass session cache, use UserProfile directly."""
    profile = getattr(user, 'profile', None)
    if profile is None:
        return None
    return profile.organization


def _invalidate_event_list_cache(org):
    """Bump the event list cache version so existing entries expire naturally."""
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


# ---------------------------------------------------------------------------
# Scanner auth classes
# ---------------------------------------------------------------------------

class ScannerSessionAuthentication(BaseAuthentication):
    """Authenticate requests carrying 'Authorization: Scanner <token>'."""

    def authenticate(self, request):
        auth = get_authorization_header(request).split()
        if len(auth) != 2 or auth[0].lower() != b'scanner':
            return None
        try:
            session = ScannerSession.objects.select_related('event__organization').get(
                token=auth[1].decode(), is_active=True
            )
        except (ScannerSession.DoesNotExist, Exception):
            raise AuthenticationFailed('Invalid or expired scanner token.')
        return (None, session)  # user=None; session is request.auth


class IsScannerAuthenticated(BasePermission):
    def has_permission(self, request, view):
        return isinstance(request.auth, ScannerSession)


# ---------------------------------------------------------------------------
# Organization API key auth
# ---------------------------------------------------------------------------

class OrganizationAPIKeyAuthentication(BaseAuthentication):
    """Authenticate requests carrying 'Authorization: Bearer cue_live_...'."""

    def authenticate(self, request):
        auth = get_authorization_header(request).split()
        if len(auth) != 2 or auth[0].lower() != b'bearer':
            return None
        raw_key = auth[1].decode('utf-8', errors='ignore')
        if not raw_key.startswith('cue_live_'):
            return None
        try:
            api_key = (
                OrganizationAPIKey.objects
                .select_related('organization')
                .get(key=raw_key, is_active=True)
            )
        except OrganizationAPIKey.DoesNotExist:
            raise AuthenticationFailed('Invalid or revoked API key.')
        OrganizationAPIKey.objects.filter(pk=api_key.pk).update(last_used_at=timezone.now())
        return (None, api_key)  # user=None; org via request.auth.organization


class IsOrganizationAPIKeyAuthenticated(BasePermission):
    def has_permission(self, request, view):
        return isinstance(request.auth, OrganizationAPIKey)


# ---------------------------------------------------------------------------
# Agent API — upcoming events
# ---------------------------------------------------------------------------

@api_view(['GET'])
@authentication_classes([OrganizationAPIKeyAuthentication])
@permission_classes([IsOrganizationAPIKeyAuthenticated])
def agent_upcoming_events(request):
    """
    GET /api/v1/events/upcoming/
    Returns events starting within the next 31 days for the authenticated org.
    Query params:
        limit   int  (default 10, max 50)
    """
    from datetime import timedelta
    from django.db.models import Prefetch

    org = request.auth.organization

    try:
        limit = min(int(request.query_params.get('limit', 10)), 50)
    except (ValueError, TypeError):
        limit = 10

    today = timezone.localdate()
    cutoff = today + timedelta(days=31)

    events = (
        Event.objects
        .filter(
            organization=org,
            start_date__gte=today,
            start_date__lte=cutoff,
            deleted_at__isnull=True,
        )
        .select_related('venue')
        .prefetch_related(
            'talent_lineup',
            Prefetch(
                'custom_field_values',
                queryset=EventCustomFieldValue.objects.select_related(
                    'custom_field', 'custom_field_option'
                ),
            ),
            Prefetch(
                'saleable_ticket_types',
                queryset=SaleableTicketType.objects.filter(is_active=True).order_by('order', 'name'),
            ),
        )
        .order_by('start_date', 'start_time', 'name')[:limit]
    )

    data = []
    for event in events:
        venue = event.venue
        custom_fields = {}
        for cfv in event.custom_field_values.all():
            if cfv.custom_field_option:
                custom_fields[cfv.custom_field.name] = cfv.custom_field_option.label

        talent = [t.name for t in event.talent_lineup.all()]

        ticket_types = [
            {
                'name': tt.name,
                'price': str(tt.price),
                'description': tt.description,
                'sold_out': tt.remaining_quantity() == 0,
            }
            for tt in event.saleable_ticket_types.all()
        ]

        data.append({
            'name': event.name,
            'summary': event.summary,
            'description': event.description,
            'start_date': event.start_date.isoformat(),
            'start_time': event.start_time.isoformat() if event.start_time else None,
            'end_date': event.end_date.isoformat() if event.end_date else None,
            'end_time': event.end_time.isoformat() if event.end_time else None,
            'timezone': event.timezone,
            'ticket_link': event.ticket_link or None,
            'venue': {
                'name': venue.name,
                'city': venue.city,
                'street_address': venue.street_address,
                'state': venue.state,
                'postal_code': venue.postal_code,
                'country': venue.country,
            },
            'talent_lineup': talent,
            'additional_details': custom_fields,
            'ticket_types': ticket_types,
        })

    response = Response({
        'organization': org.name,
        'generated_at': timezone.now().isoformat(),
        'event_count': len(data),
        'events': data,
    })
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response


@api_view(['GET'])
@authentication_classes([OrganizationAPIKeyAuthentication])
@permission_classes([IsOrganizationAPIKeyAuthenticated])
def agent_events(request):
    """
    GET /api/v1/events/
    Returns all events for the org with per-event ticket count and revenue summary.
    Query params:
        status  str  upcoming|past|all (default: all)
        limit   int  (default 20, max 100)
    """
    org = request.auth.organization
    today = timezone.localdate()

    status_filter = request.query_params.get('status', 'all')
    try:
        limit = min(int(request.query_params.get('limit', 20)), 100)
    except (ValueError, TypeError):
        limit = 20

    qs = Event.objects.filter(organization=org, deleted_at__isnull=True)
    if status_filter == 'upcoming':
        qs = qs.filter(start_date__gte=today)
    elif status_filter == 'past':
        qs = qs.filter(start_date__lt=today)

    orders_sq = (
        TicketOrder.objects
        .filter(event=OuterRef('pk'), refunded_at__isnull=True)
        .values('event')
        .annotate(c=Count('id'))
        .values('c')
    )
    revenue_sq = (
        TicketOrder.objects
        .filter(event=OuterRef('pk'), refunded_at__isnull=True)
        .values('event')
        .annotate(s=Sum('total_amount'))
        .values('s')
    )
    expense_sq = (
        EventExpense.objects.visible()
        .filter(event=OuterRef('pk'))
        .values('event')
        .annotate(s=Sum('amount'))
        .values('s')
    )
    income_sq = (
        EventIncome.objects
        .filter(event=OuterRef('pk'), deleted_at__isnull=True)
        .values('event')
        .annotate(s=Sum('amount'))
        .values('s')
    )

    events = (
        qs
        .select_related('venue')
        .annotate(
            ticket_count=Coalesce(Subquery(orders_sq, output_field=IntegerField()), 0),
            ticket_revenue=Coalesce(
                Subquery(revenue_sq, output_field=DecimalField(max_digits=12, decimal_places=2)),
                Decimal('0.00'),
            ),
            total_expenses=Coalesce(
                Subquery(expense_sq, output_field=DecimalField(max_digits=12, decimal_places=2)),
                Decimal('0.00'),
            ),
            additional_income_total=Coalesce(
                Subquery(income_sq, output_field=DecimalField(max_digits=12, decimal_places=2)),
                Decimal('0.00'),
            ),
        )
        .order_by('-start_date', '-start_time')[:limit]
    )

    data = []
    for event in events:
        venue = event.venue
        total_revenue = event.ticket_revenue + event.additional_income_total
        data.append({
            'id': str(event.id),
            'name': event.name,
            'summary': event.summary,
            'start_date': event.start_date.isoformat(),
            'start_time': event.start_time.isoformat() if event.start_time else None,
            'end_date': event.end_date.isoformat() if event.end_date else None,
            'status': 'upcoming' if event.start_date >= today else 'past',
            'venue': {
                'name': venue.name,
                'city': venue.city,
                'state': venue.state,
            },
            'tickets_sold': event.ticket_count,
            'ticket_revenue': str(event.ticket_revenue),
            'additional_income': str(event.additional_income_total),
            'total_revenue': str(total_revenue),
            'total_expenses': str(event.total_expenses),
            'net_profit': str(total_revenue - event.total_expenses),
        })

    response = Response({
        'organization': org.name,
        'generated_at': timezone.now().isoformat(),
        'event_count': len(data),
        'events': data,
    })
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response


@api_view(['GET'])
@authentication_classes([OrganizationAPIKeyAuthentication])
@permission_classes([IsOrganizationAPIKeyAuthenticated])
def agent_event_detail(request, event_id):
    """
    GET /api/v1/events/<uuid:event_id>/
    Full event detail with attendance breakdown, financials, and ticket types.
    """
    org = request.auth.organization
    today = timezone.localdate()
    event = get_object_or_404(
        Event.objects.filter(organization=org, deleted_at__isnull=True).select_related('venue'),
        id=event_id,
    )
    venue = event.venue

    orders_qs = TicketOrder.objects.filter(event=event, refunded_at__isnull=True)
    agg = orders_qs.aggregate(
        total_orders=Count('id'),
        checked_in=Count('id', filter=Q(checked_in_at__isnull=False)),
        ticket_revenue=Coalesce(Sum('total_amount'), Decimal('0.00')),
    )

    attended_customer_ids = set(orders_qs.values_list('customer_id', flat=True))
    prior_customer_ids = set(
        TicketOrder.objects
        .filter(
            customer_id__in=attended_customer_ids,
            event__organization=org,
            refunded_at__isnull=True,
        )
        .exclude(event=event)
        .values_list('customer_id', flat=True)
        .distinct()
    )
    returning_count = len(prior_customer_ids & attended_customer_ids)
    new_count = len(attended_customer_ids - prior_customer_ids)

    expenses = list(
        EventExpense.objects.visible()
        .filter(event=event)
        .values('category', 'description', 'amount', 'expense_date')
        .order_by('-expense_date')
    )
    total_expenses = sum(e['amount'] for e in expenses)

    income_lines = list(
        EventIncome.objects
        .filter(event=event, deleted_at__isnull=True)
        .select_related('income_source')
        .order_by('income_source__order')
    )
    total_additional_income = sum(i.amount for i in income_lines)

    ticket_types = list(
        event.saleable_ticket_types.filter(is_active=True).order_by('order', 'name')
    )

    ticket_revenue = agg['ticket_revenue']
    total_revenue = ticket_revenue + total_additional_income

    response = Response({
        'id': str(event.id),
        'name': event.name,
        'summary': event.summary,
        'description': event.description,
        'start_date': event.start_date.isoformat(),
        'start_time': event.start_time.isoformat() if event.start_time else None,
        'end_date': event.end_date.isoformat() if event.end_date else None,
        'end_time': event.end_time.isoformat() if event.end_time else None,
        'timezone': event.timezone,
        'status': 'upcoming' if event.start_date >= today else 'past',
        'venue': {
            'name': venue.name,
            'city': venue.city,
            'state': venue.state,
            'street_address': venue.street_address,
            'postal_code': venue.postal_code,
            'country': venue.country,
            'capacity': venue.capacity,
        },
        'attendance': {
            'total_orders': agg['total_orders'],
            'checked_in': agg['checked_in'],
            'new_customers': new_count,
            'returning_customers': returning_count,
        },
        'financials': {
            'ticket_revenue': str(ticket_revenue),
            'additional_income': str(total_additional_income),
            'total_revenue': str(total_revenue),
            'total_expenses': str(total_expenses),
            'net_profit': str(total_revenue - total_expenses),
            'income': [
                {
                    'source': i.income_source.name,
                    'amount': str(i.amount),
                    'date': i.income_date.isoformat() if i.income_date else None,
                }
                for i in income_lines
            ],
            'expenses': [
                {
                    'category': e['category'],
                    'description': e['description'],
                    'amount': str(e['amount']),
                    'date': e['expense_date'].isoformat() if e['expense_date'] else None,
                }
                for e in expenses
            ],
        },
        'ticket_types': [
            {
                'name': tt.name,
                'price': str(tt.price),
                'quantity_limit': tt.quantity_limit,
                'quantity_sold': tt.quantity_sold,
                'remaining': tt.remaining_quantity(),
            }
            for tt in ticket_types
        ],
    })
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response


@api_view(['GET'])
@authentication_classes([OrganizationAPIKeyAuthentication])
@permission_classes([IsOrganizationAPIKeyAuthenticated])
def agent_customers(request):
    """
    GET /api/v1/customers/
    Returns paginated customers with LTV and RFM segment.
    Query params:
        segment  str   RFM segment name (e.g. Champions, At Risk)
        limit    int   (default 50, max 200)
        page     int   (default 1)
    """
    org = request.auth.organization

    segment = request.query_params.get('segment', '').strip()
    try:
        limit = min(int(request.query_params.get('limit', 50)), 200)
    except (ValueError, TypeError):
        limit = 50
    try:
        page = max(int(request.query_params.get('page', 1)), 1)
    except (ValueError, TypeError):
        page = 1

    order_count_sq = (
        TicketOrder.objects
        .filter(customer=OuterRef('pk'), refunded_at__isnull=True)
        .values('customer')
        .annotate(c=Count('id'))
        .values('c')
    )

    qs = Customer.objects.filter(organization=org)
    if segment:
        qs = qs.filter(rfm_segment=segment)

    qs = qs.annotate(
        order_count=Coalesce(Subquery(order_count_sq, output_field=IntegerField()), 0),
    ).order_by('-lifetime_value')

    total = qs.count()
    offset = (page - 1) * limit
    customers = qs[offset: offset + limit]

    data = [
        {
            'id': str(c.id),
            'email': c.email,
            'name': c.name,
            'lifetime_value': str(c.lifetime_value),
            'rfm_segment': c.rfm_segment or None,
            'behavior_profile': c.behavior_profile or None,
            'order_count': c.order_count,
            'last_order_date': c.last_order_date.isoformat() if c.last_order_date else None,
        }
        for c in customers
    ]

    response = Response({
        'organization': org.name,
        'generated_at': timezone.now().isoformat(),
        'total': total,
        'page': page,
        'limit': limit,
        'customers': data,
    })
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response


@api_view(['GET'])
@authentication_classes([OrganizationAPIKeyAuthentication])
@permission_classes([IsOrganizationAPIKeyAuthenticated])
def agent_customer_detail(request, customer_id):
    """
    GET /api/v1/customers/<uuid:customer_id>/
    Customer profile with recent order history.
    """
    org = request.auth.organization
    customer = get_object_or_404(Customer.objects.filter(organization=org), id=customer_id)

    orders = (
        TicketOrder.objects
        .filter(customer=customer)
        .select_related('event', 'event__venue')
        .order_by('-order_date')[:20]
    )

    response = Response({
        'id': str(customer.id),
        'email': customer.email,
        'name': customer.name,
        'phone': customer.phone or None,
        'lifetime_value': str(customer.lifetime_value),
        'rfm_segment': customer.rfm_segment or None,
        'rfm_recency_score': customer.rfm_recency_score,
        'rfm_frequency_score': customer.rfm_frequency_score,
        'rfm_monetary_score': customer.rfm_monetary_score,
        'behavior_profile': customer.behavior_profile or None,
        'days_since_last_order': customer.days_since_last_order,
        'avg_days_between_orders': customer.avg_days_between_orders,
        'last_order_date': customer.last_order_date.isoformat() if customer.last_order_date else None,
        'sms_opt_in': customer.sms_opt_in,
        'recent_orders': [
            {
                'id': str(o.id),
                'order_number': o.display_order_number,
                'event': o.event.name,
                'event_date': o.event.start_date.isoformat(),
                'order_date': o.order_date.isoformat(),
                'total_amount': str(o.total_amount),
                'refunded': o.refunded_at is not None,
                'checked_in': o.checked_in_at is not None,
            }
            for o in orders
        ],
    })
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response


@api_view(['GET'])
@authentication_classes([OrganizationAPIKeyAuthentication])
@permission_classes([IsOrganizationAPIKeyAuthenticated])
def agent_analytics_segments(request):
    """
    GET /api/v1/analytics/segments/
    RFM segment distribution for the org's customer base.
    """
    org = request.auth.organization

    all_customers = Customer.objects.filter(organization=org)
    total = all_customers.count()

    segments = list(
        all_customers
        .exclude(rfm_segment='')
        .values('rfm_segment')
        .annotate(count=Count('id'))
        .order_by('-count')
    )

    unscored = all_customers.filter(rfm_segment='').count()

    data = [
        {
            'segment': s['rfm_segment'],
            'count': s['count'],
            'pct': round(s['count'] / total * 100, 1) if total else 0,
        }
        for s in segments
    ]

    response = Response({
        'organization': org.name,
        'generated_at': timezone.now().isoformat(),
        'total_customers': total,
        'unscored_customers': unscored,
        'segments': data,
    })
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response


@api_view(['GET'])
@authentication_classes([OrganizationAPIKeyAuthentication])
@permission_classes([IsOrganizationAPIKeyAuthenticated])
def agent_analytics_revenue(request):
    """
    GET /api/v1/analytics/revenue/
    Revenue summary for the org across different time windows.
    """
    from datetime import timedelta

    org = request.auth.organization
    now = timezone.now()

    base_qs = TicketOrder.objects.filter(
        customer__organization=org,
        refunded_at__isnull=True,
    )

    def _revenue(days=None):
        qs = base_qs
        if days:
            qs = qs.filter(order_date__gte=now - timedelta(days=days))
        return qs.aggregate(s=Coalesce(Sum('total_amount'), Decimal('0.00')))['s']

    event_count = Event.objects.filter(organization=org, deleted_at__isnull=True).count()

    # Additional income (non-ticket)
    additional_income_sq = (
        EventIncome.objects
        .filter(event__organization=org, deleted_at__isnull=True)
        .aggregate(s=Coalesce(Sum('amount'), Decimal('0.00')))['s']
    )

    ticket_revenue_all = _revenue()
    total_revenue_all = ticket_revenue_all + additional_income_sq

    response = Response({
        'organization': org.name,
        'generated_at': timezone.now().isoformat(),
        'event_count': event_count,
        'ticket_revenue': {
            'last_30_days': str(_revenue(30)),
            'last_90_days': str(_revenue(90)),
            'last_365_days': str(_revenue(365)),
            'all_time': str(ticket_revenue_all),
        },
        'total_revenue_all_time': str(total_revenue_all),
    })
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response


@api_view(['GET'])
@authentication_classes([OrganizationAPIKeyAuthentication])
@permission_classes([IsOrganizationAPIKeyAuthenticated])
def agent_orders(request):
    """
    GET /api/v1/orders/
    Returns recent ticket orders for the org, optionally filtered by event.
    Query params:
        event_id  uuid  filter to a specific event
        limit     int   (default 50, max 200)
        page      int   (default 1)
    """
    org = request.auth.organization

    event_id = request.query_params.get('event_id')
    try:
        limit = min(int(request.query_params.get('limit', 50)), 200)
    except (ValueError, TypeError):
        limit = 50
    try:
        page = max(int(request.query_params.get('page', 1)), 1)
    except (ValueError, TypeError):
        page = 1

    qs = (
        TicketOrder.objects
        .filter(customer__organization=org)
        .select_related('customer', 'event', 'event__venue')
        .order_by('-order_date')
    )

    if event_id:
        qs = qs.filter(event_id=event_id)

    total = qs.count()
    offset = (page - 1) * limit
    orders = qs[offset: offset + limit]

    data = [
        {
            'id': str(o.id),
            'order_number': o.display_order_number,
            'order_date': o.order_date.isoformat(),
            'customer': {
                'id': str(o.customer.id),
                'name': o.customer.name,
                'email': o.customer.email,
            },
            'event': {
                'id': str(o.event.id),
                'name': o.event.name,
                'start_date': o.event.start_date.isoformat(),
                'venue_city': o.event.venue.city,
            },
            'total_amount': str(o.total_amount),
            'refunded': o.refunded_at is not None,
            'checked_in': o.checked_in_at is not None,
        }
        for o in orders
    ]

    response = Response({
        'organization': org.name,
        'generated_at': timezone.now().isoformat(),
        'total': total,
        'page': page,
        'limit': limit,
        'orders': data,
    })
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def api_login(request):
    """
    POST /api/auth/login/
    Body: {email, password}
    Returns: {token, user_type, user_name, org_name, org_id}
    """
    email = request.data.get('email', '').strip()
    password = request.data.get('password', '')

    if not email or not password:
        return Response({'error': 'email and password are required'}, status=400)

    user = authenticate(request, username=email, password=password)
    if user is None:
        return Response({'error': 'Invalid credentials'}, status=400)

    token, _ = Token.objects.get_or_create(user=user)

    org = _get_org_from_user(user)
    profile = getattr(user, 'profile', None)
    if profile is not None:
        user_type = profile.role
    else:
        user_type = UserProfile.Role.ATTENDEE
    org_name = org.name if org else ''
    org_id = str(org.pk) if org else None

    return Response({
        'token': token.key,
        'user_type': user_type,
        'user_name': user.get_full_name() or user.username,
        'org_name': org_name,
        'org_id': org_id,
    })


# ---------------------------------------------------------------------------
# Organizer — Events
# ---------------------------------------------------------------------------

@api_view(['GET'])
def organizer_events(request):
    """
    GET /api/organizer/events/
    Returns upcoming events for the authenticated organizer's org.
    """
    org = _get_org_from_user(request.user)
    if org is None:
        return Response({'error': 'No organization found for this user'}, status=403)

    today = timezone.localdate()

    checked_in_sq = (
        TicketOrder.objects
        .filter(event=OuterRef('pk'), checked_in_at__isnull=False)
        .values('event')
        .annotate(c=Count('id'))
        .values('c')
    )

    total_tickets_sq = (
        Ticket.objects
        .filter(ticket_order__event=OuterRef('pk'))
        .values('ticket_order__event')
        .annotate(c=Count('id'))
        .values('c')
    )

    revenue_sq = (
        TicketOrder.objects
        .filter(event=OuterRef('pk'), refunded_at__isnull=True)
        .values('event')
        .annotate(r=Sum('total_amount'))
        .values('r')
    )

    events = (
        Event.objects
        .filter(organization=org, start_date__gte=today, ticketing_type=TICKETING_TYPE_DIRECT)
        .annotate(
            checked_in_count=Coalesce(Subquery(checked_in_sq, output_field=IntegerField()), 0),
            total_tickets=Coalesce(Subquery(total_tickets_sq, output_field=IntegerField()), 0),
            total_revenue=Coalesce(Subquery(revenue_sq, output_field=DecimalField()), Decimal('0.00')),
        )
        .order_by('start_date')
    )

    data = [
        {
            'id': str(event.pk),
            'name': event.name,
            'start_date': event.start_date.isoformat() if event.start_date else None,
            'start_time': event.start_time.isoformat() if event.start_time else None,
            'venue': event.venue.name if event.venue else None,
            'city': event.venue.city if event.venue else None,
            'checked_in_count': event.checked_in_count,
            'total_tickets': event.total_tickets,
            'total_revenue': str(event.total_revenue),
        }
        for event in events
    ]
    return Response(data)


# ---------------------------------------------------------------------------
# Organizer — Ticket Types
# ---------------------------------------------------------------------------

@api_view(['GET'])
def organizer_ticket_types(request, event_id):
    """
    GET /api/organizer/events/<uuid:event_id>/ticket-types/
    Returns active, non-password-protected, on-sale ticket types for the event.
    """
    org = _get_org_from_user(request.user)
    if org is None:
        return Response({'error': 'No organization found for this user'}, status=403)

    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    ticket_types = SaleableTicketType.objects.filter(
        event=event,
        is_active=True,
        is_password_protected=False,
    )
    on_sale = [t for t in ticket_types if t.is_on_sale()]

    data = [
        {
            'id': str(tt.pk),
            'name': tt.name,
            'price': str(tt.price),
            'description': tt.description,
            'remaining': tt.remaining_quantity(),  # None = unlimited
        }
        for tt in on_sale
    ]
    return Response(data)


# ---------------------------------------------------------------------------
# Organizer — Check-in Stats
# ---------------------------------------------------------------------------

@api_view(['GET'])
def organizer_checkin_stats(request, event_id):
    """
    GET /api/organizer/events/<uuid:event_id>/checkin-stats/
    Returns per-ticket-type check-in counts for the event.
    """
    org = _get_org_from_user(request.user)
    if org is None:
        return Response({'error': 'No organization found for this user'}, status=403)

    stats = (
        Ticket.objects
        .filter(
            ticket_order__event_id=event_id,
            ticket_order__event__organization=org,
        )
        .values('ticket_type')
        .annotate(
            total=Count('id'),
            checked_in=Count('id', filter=Q(ticket_order__checked_in_at__isnull=False)),
        )
        .order_by('ticket_type')
    )

    return Response(
        [{'ticket_type_name': s['ticket_type'], 'total': s['total'], 'checked_in': s['checked_in']}
         for s in stats],
    )


# ---------------------------------------------------------------------------
# Organizer — Event Orders
# ---------------------------------------------------------------------------

@api_view(['GET'])
def organizer_event_orders(request, event_id):
    """
    GET /api/organizer/events/<uuid:event_id>/orders/
    Returns all ticket orders for the event, newest first.
    """
    org = _get_org_from_user(request.user)
    if org is None:
        return Response({'error': 'No organization found for this user'}, status=403)

    orders = (
        TicketOrder.objects
        .filter(event_id=event_id, event__organization=org)
        .select_related('customer')
        .prefetch_related('tickets')
        .order_by('-order_date')
    )

    data = [
        {
            'order_number': o.display_order_number,
            'customer_name': o.customer.name,
            'customer_email': o.customer.email,
            'total_amount': str(o.total_amount),
            'order_date': o.order_date.isoformat(),
            'checked_in_at': o.checked_in_at.isoformat() if o.checked_in_at else None,
            'refunded_at': o.refunded_at.isoformat() if o.refunded_at else None,
            'ticket_types': [t.ticket_type for t in o.tickets.all()],
        }
        for o in orders
    ]
    return Response(data)


# ---------------------------------------------------------------------------
# Organizer — Check-in
# ---------------------------------------------------------------------------

@api_view(['POST'])
def organizer_checkin(request):
    """
    POST /api/organizer/checkin/
    Body: {order_number, event_id}
    Returns: {status, order_number, customer_name, ticket_types}
    """
    org = _get_org_from_user(request.user)
    if org is None:
        return Response({'error': 'No organization found for this user'}, status=403)

    order_number = request.data.get('order_number', '').strip()
    event_id = request.data.get('event_id', '').strip()

    if not order_number or not event_id:
        return Response({'error': 'order_number and event_id are required'}, status=400)

    with transaction.atomic():
        order = (
            TicketOrder.objects
            .select_for_update()
            .filter(
                customer__organization=org,
                event_id=event_id,
                order_number=order_number,
            )
            .select_related('customer')
            .prefetch_related('tickets')
            .first()
        )

        if order is None:
            return Response({'error': 'Order not found'}, status=404)

        if order.refunded_at is not None:
            return Response({
                'status': 'refunded',
                'order_number': order.order_number,
                'customer_name': order.customer.name,
            }, status=200)

        if order.checked_in_at is not None:
            checked_in_count = TicketOrder.objects.filter(
                event_id=event_id,
                customer__organization=org,
                checked_in_at__isnull=False,
            ).count()
            return Response({
                'status': 'already_checked_in',
                'order_number': order.order_number,
                'customer_name': order.customer.name,
                'checked_in_at': order.checked_in_at.isoformat(),
                'ticket_types': [t.ticket_type for t in order.tickets.all()],
                'checked_in_count': checked_in_count,
            }, status=200)

        order.checked_in_at = timezone.now()
        order.checked_in_by = request.user
        order.save(update_fields=['checked_in_at', 'checked_in_by'])

    checked_in_count = TicketOrder.objects.filter(
        event_id=event_id,
        customer__organization=org,
        checked_in_at__isnull=False,
    ).count()

    return Response({
        'status': 'checked_in',
        'order_number': order.order_number,
        'customer_name': order.customer.name,
        'checked_in_at': order.checked_in_at.isoformat(),
        'ticket_types': [t.ticket_type for t in order.tickets.all()],
        'checked_in_count': checked_in_count,
    }, status=200)


# ---------------------------------------------------------------------------
# Stripe Terminal — Connection Token
# ---------------------------------------------------------------------------

@api_view(['POST'])
def stripe_connection_token(request):
    """
    POST /api/stripe/connection-token/
    Returns a Stripe Terminal connection token for the org's location.
    """
    org = _get_org_from_user(request.user)
    if org is None:
        return Response({'error': 'No organization found for this user'}, status=403)

    import stripe as stripe_lib
    from django.conf import settings as django_settings

    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY

    try:
        kwargs = {}
        location_id = django_settings.STRIPE_TERMINAL_LOCATION_ID
        if location_id:
            kwargs['location'] = location_id
        conn_token = stripe_lib.terminal.ConnectionToken.create(**kwargs)
    except stripe_lib.error.StripeError as exc:
        logger.error("Stripe connection token error: %s", exc)
        return Response({'error': str(exc)}, status=502)

    return Response({'secret': conn_token.secret})


# ---------------------------------------------------------------------------
# Stripe Terminal — Payment Intent
# ---------------------------------------------------------------------------

def _create_terminal_payment_intent(event, line_items):
    """Validate ticket-type inventory and create a card-present PaymentIntent.

    Returns (payload, status). On success, payload is the response body and
    status is 201. On failure, payload is an {'error': ...} dict and status
    is 400 (validation) or 502 (Stripe).
    """
    if not line_items:
        return {'error': 'event_id and line_items are required'}, 400

    amount_cents = 0
    for item in line_items:
        tt_id = item.get('ticket_type_id', '')
        qty = int(item.get('quantity', 1))
        if qty < 1:
            return {'error': 'quantity must be >= 1'}, 400

        try:
            tt = SaleableTicketType.objects.get(id=tt_id, event=event, is_active=True)
        except SaleableTicketType.DoesNotExist:
            return {'error': f'Ticket type {tt_id} not found for this event'}, 400

        remaining = tt.remaining_quantity()
        if remaining is not None and qty > remaining:
            return {'error': f'Only {remaining} tickets remaining for {tt.name}'}, 400

        amount_cents += int((tt.price * qty * 100).to_integral_value())

    import stripe as stripe_lib
    from django.conf import settings as django_settings

    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY

    try:
        pi = stripe_lib.PaymentIntent.create(
            amount=amount_cents,
            currency=django_settings.STRIPE_CURRENCY,
            payment_method_types=['card_present'],
            capture_method='automatic',
        )
    except stripe_lib.error.StripeError as exc:
        logger.error("Stripe terminal PaymentIntent error: %s", exc)
        return {'error': str(exc)}, 502

    return {
        'client_secret': pi.client_secret,
        'payment_intent_id': pi.id,
        'amount_cents': amount_cents,
        'currency': django_settings.STRIPE_CURRENCY,
    }, 201


@api_view(['POST'])
def stripe_terminal_payment_intent(request):
    """
    POST /api/stripe/terminal-payment-intent/
    Body: {event_id, line_items: [{ticket_type_id, quantity}]}
    Creates a PaymentIntent for a card-present terminal transaction.
    """
    org = _get_org_from_user(request.user)
    if org is None:
        return Response({'error': 'No organization found for this user'}, status=403)

    event_id = request.data.get('event_id', '').strip()
    line_items = request.data.get('line_items', [])

    if not event_id or not line_items:
        return Response({'error': 'event_id and line_items are required'}, status=400)

    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    payload, status = _create_terminal_payment_intent(event, line_items)
    return Response(payload, status=status)


# ---------------------------------------------------------------------------
# Organizer — Sell (post-terminal payment fulfillment)
# ---------------------------------------------------------------------------

def _finalize_in_person_sale(event, payment_intent_id, buyer_name, buyer_email, line_items, checked_in_by):
    """Verify Stripe PaymentIntent succeeded and create the in-person TicketOrder.

    Returns (payload, status). On success, payload is the response body and
    status is 201. On failure, payload is an {'error': ...} dict and status
    is 400 (validation) or 502 (Stripe).

    checked_in_by may be None for scanner-PIN sessions (no Cue account).
    """
    import stripe as stripe_lib
    from django.conf import settings as django_settings

    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY

    try:
        pi = stripe_lib.PaymentIntent.retrieve(payment_intent_id)
    except stripe_lib.error.StripeError as exc:
        logger.error("Stripe PaymentIntent retrieve error: %s", exc)
        return {'error': str(exc)}, 502

    if pi.status != 'succeeded':
        return {'error': f'PaymentIntent status is {pi.status!r}, expected succeeded'}, 400

    total_amount = Decimal('0.00')
    resolved = []
    for item in line_items:
        tt_id = item.get('ticket_type_id', '')
        qty = int(item.get('quantity', 1))
        item_name = item.get('name', '')
        try:
            item_price = Decimal(str(item.get('price', '0')))
        except InvalidOperation:
            return {'error': f'Invalid price for item {item_name}'}, 400

        try:
            tt = SaleableTicketType.objects.get(id=tt_id, event=event)
        except SaleableTicketType.DoesNotExist:
            return {'error': f'Ticket type {tt_id} not found'}, 400

        total_amount += item_price * qty
        resolved.append({'tt': tt, 'tt_id': tt_id, 'qty': qty, 'name': item_name or tt.name, 'price': item_price})

    org = event.organization
    now = timezone.now()
    customer, _ = Customer.objects.get_or_create(
        email=buyer_email,
        organization=org,
        defaults={'name': buyer_name or buyer_email},
    )

    with transaction.atomic():
        order = TicketOrder.objects.create(
            customer=customer,
            event=event,
            uploaded_file=None,
            order_number=next_order_number(),
            order_date=now,
            total_amount=total_amount,
            is_in_person=True,
            checked_in_at=now,
            checked_in_by=checked_in_by,
        )

        ticket_count = 0
        for item in resolved:
            Ticket.objects.bulk_create([
                Ticket(
                    ticket_order=order,
                    ticket_type=item['name'],
                    price=item['price'],
                    tier=None,
                )
                for _ in range(item['qty'])
            ])
            SaleableTicketType.objects.filter(id=item['tt_id']).update(
                quantity_sold=F('quantity_sold') + item['qty']
            )
            ticket_count += item['qty']

    customer.update_lifetime_value()
    _invalidate_event_list_cache(org)

    return {
        'order_number': order.order_number,
        'order_id': str(order.pk),
        'total_amount': str(order.total_amount),
        'ticket_count': ticket_count,
    }, 201


@api_view(['POST'])
def organizer_sell(request):
    """
    POST /api/organizer/sell/
    Body: {event_id, payment_intent_id, buyer_name, buyer_email,
           line_items: [{ticket_type_id, quantity, name, price}]}
    Verifies the PaymentIntent succeeded, then creates the order.
    """
    org = _get_org_from_user(request.user)
    if org is None:
        return Response({'error': 'No organization found for this user'}, status=403)

    event_id = request.data.get('event_id', '').strip()
    payment_intent_id = request.data.get('payment_intent_id', '').strip()
    buyer_name = request.data.get('buyer_name', '').strip()
    buyer_email = request.data.get('buyer_email', '').strip().lower()
    line_items = request.data.get('line_items', [])

    if not event_id or not payment_intent_id or not buyer_email or not line_items:
        return Response(
            {'error': 'event_id, payment_intent_id, buyer_email, and line_items are required'},
            status=400,
        )

    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)

    payload, status = _finalize_in_person_sale(
        event=event,
        payment_intent_id=payment_intent_id,
        buyer_name=buyer_name,
        buyer_email=buyer_email,
        line_items=line_items,
        checked_in_by=request.user,
    )
    return Response(payload, status=status)


# ---------------------------------------------------------------------------
# Scanner — guest (PIN-based, no Cue account)
# ---------------------------------------------------------------------------

@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def scanner_login(request):
    """
    POST /api/auth/scanner-login/
    Body: {pin}
    Returns: {scanner_token, event_id, event_name, org_name, venue, start_date}
    """
    pin = request.data.get('pin', '').strip()
    if not pin:
        return Response({'error': 'pin is required'}, status=400)
    try:
        event = Event.objects.select_related('venue', 'organization').get(scanner_pin=pin)
    except Event.DoesNotExist:
        return Response({'error': 'Invalid PIN'}, status=400)
    session = ScannerSession.objects.create(event=event)
    return Response({
        'scanner_token': str(session.token),
        'event_id': str(event.pk),
        'event_name': event.name,
        'org_name': event.organization.name,
        'venue': event.venue.name if event.venue else '',
        'start_date': event.start_date.isoformat() if event.start_date else None,
    })


@api_view(['GET'])
@authentication_classes([ScannerSessionAuthentication])
@permission_classes([IsScannerAuthenticated])
def scanner_event(request):
    """
    GET /api/scanner/event/
    Returns event info and check-in stats for the scanner's session.
    """
    session = request.auth
    event = session.event
    org = event.organization
    total = TicketOrder.objects.filter(event=event, customer__organization=org).count()
    checked_in = TicketOrder.objects.filter(
        event=event, customer__organization=org, checked_in_at__isnull=False
    ).count()
    return Response({
        'event_id': str(event.pk),
        'event_name': event.name,
        'org_name': org.name,
        'venue': event.venue.name if event.venue else '',
        'start_date': event.start_date.isoformat() if event.start_date else None,
        'start_time': event.start_time.isoformat() if event.start_time else None,
        'total_orders': total,
        'checked_in_count': checked_in,
    })


@api_view(['POST'])
@authentication_classes([ScannerSessionAuthentication])
@permission_classes([IsScannerAuthenticated])
def scanner_checkin(request):
    """
    POST /api/scanner/checkin/
    Body: {order_number, event_id?}
    Authorization: Scanner <token>

    event_id is optional. The scanner session is already bound to a single
    event, which is authoritative. If event_id is supplied, it must match.
    """
    session = request.auth
    event = session.event
    org = event.organization

    order_number = request.data.get('order_number', '').strip()
    event_id = request.data.get('event_id', '').strip()

    if not order_number:
        try:
            received_keys = sorted(request.data.keys())
        except Exception:
            received_keys = '<unreadable>'
        logger.warning(
            "scanner_checkin 400: missing order_number — content_type=%r, received_keys=%r",
            request.content_type, received_keys,
        )
        return Response({'error': 'order_number is required'}, status=400)
    if event_id and str(event.pk) != event_id:
        return Response({'error': 'Event mismatch'}, status=403)

    with transaction.atomic():
        order = (
            TicketOrder.objects
            .select_for_update()
            .filter(customer__organization=org, event=event, order_number=order_number)
            .select_related('customer')
            .prefetch_related('tickets')
            .first()
        )
        if order is None:
            return Response({'error': 'Order not found'}, status=404)
        if order.refunded_at is not None:
            return Response({
                'status': 'refunded',
                'order_number': order.order_number,
                'customer_name': order.customer.name,
            })
        if order.checked_in_at is not None:
            checked_in_count = TicketOrder.objects.filter(
                event=event, customer__organization=org, checked_in_at__isnull=False
            ).count()
            return Response({
                'status': 'already_checked_in',
                'order_number': order.order_number,
                'customer_name': order.customer.name,
                'checked_in_at': order.checked_in_at.isoformat(),
                'ticket_types': [t.ticket_type for t in order.tickets.all()],
                'checked_in_count': checked_in_count,
            })
        order.checked_in_at = timezone.now()
        order.checked_in_by = None  # no Cue account for scanner guests
        order.save(update_fields=['checked_in_at', 'checked_in_by'])

    checked_in_count = TicketOrder.objects.filter(
        event=event, customer__organization=org, checked_in_at__isnull=False
    ).count()
    return Response({
        'status': 'checked_in',
        'order_number': order.order_number,
        'customer_name': order.customer.name,
        'checked_in_at': order.checked_in_at.isoformat(),
        'ticket_types': [t.ticket_type for t in order.tickets.all()],
        'checked_in_count': checked_in_count,
    })


# ---------------------------------------------------------------------------
# Scanner — Check-in Stats
# ---------------------------------------------------------------------------

@api_view(['GET'])
@authentication_classes([ScannerSessionAuthentication])
@permission_classes([IsScannerAuthenticated])
def scanner_checkin_stats(request):
    """
    GET /api/scanner/checkin-stats/
    Returns per-ticket-type check-in counts for the scanner's event.
    """
    event = request.auth.event
    stats = (
        Ticket.objects
        .filter(ticket_order__event=event)
        .values('ticket_type')
        .annotate(
            total=Count('id'),
            checked_in=Count('id', filter=Q(ticket_order__checked_in_at__isnull=False)),
        )
        .order_by('ticket_type')
    )
    return Response(
        [{'ticket_type_name': s['ticket_type'], 'total': s['total'], 'checked_in': s['checked_in']}
         for s in stats],
    )


# ---------------------------------------------------------------------------
# Scanner — Orders
# ---------------------------------------------------------------------------

@api_view(['GET'])
@authentication_classes([ScannerSessionAuthentication])
@permission_classes([IsScannerAuthenticated])
def scanner_orders(request):
    """
    GET /api/scanner/orders/
    Returns all ticket orders for the scanner's event, newest first.
    """
    event = request.auth.event
    orders = (
        TicketOrder.objects
        .filter(event=event)
        .select_related('customer')
        .prefetch_related('tickets')
        .order_by('-order_date')
    )
    data = [
        {
            'order_number': o.display_order_number,
            'customer_name': o.customer.name,
            'customer_email': o.customer.email,
            'total_amount': str(o.total_amount),
            'order_date': o.order_date.isoformat(),
            'checked_in_at': o.checked_in_at.isoformat() if o.checked_in_at else None,
            'refunded_at': o.refunded_at.isoformat() if o.refunded_at else None,
            'ticket_types': [t.ticket_type for t in o.tickets.all()],
        }
        for o in orders
    ]
    return Response(data)


# ---------------------------------------------------------------------------
# Scanner — In-person sell flow
#
# Mirrors the /api/organizer/* + /api/stripe/* endpoints below but scoped to a
# guest scanner-PIN session (no Cue account). The iOS scanner app's in-person
# sell flow targets these paths; the legacy /api/organizer/* paths still serve
# session-authenticated organizers from the web UI.
# ---------------------------------------------------------------------------

def _require_matching_scanner_event(request, requested_event_id):
    """If a client passes ?event_id=<uuid> or {event_id: ...}, it must match
    the scanner session's bound event. Returns an error Response on mismatch,
    or None when the request is OK to proceed.
    """
    if not requested_event_id:
        return None
    if str(request.auth.event_id) != str(requested_event_id):
        return Response(status=404)
    return None


@api_view(['GET'])
@authentication_classes([ScannerSessionAuthentication])
@permission_classes([IsScannerAuthenticated])
def scanner_ticket_types(request):
    """
    GET /api/scanner/ticket-types/?event_id=<uuid>
    Returns active, non-password-protected, on-sale ticket types for the
    scanner session's bound event. The event_id query param is optional but
    must match the bound event when supplied.
    """
    requested = request.query_params.get('event_id', '').strip()
    mismatch = _require_matching_scanner_event(request, requested)
    if mismatch is not None:
        return mismatch

    event = request.auth.event
    ticket_types = SaleableTicketType.objects.filter(
        event=event,
        is_active=True,
        is_password_protected=False,
    )
    on_sale = [t for t in ticket_types if t.is_on_sale()]

    data = [
        {
            'id': str(tt.pk),
            'name': tt.name,
            'price': str(tt.price),
            'description': tt.description,
            'remaining': tt.remaining_quantity(),
        }
        for tt in on_sale
    ]
    return Response(data)


@api_view(['POST'])
@authentication_classes([ScannerSessionAuthentication])
@permission_classes([IsScannerAuthenticated])
def scanner_stripe_connection_token(request):
    """
    POST /api/scanner/stripe/connection-token/
    Returns a Stripe Terminal connection token for the merchant tied to the
    scanner session.
    """
    import stripe as stripe_lib
    from django.conf import settings as django_settings

    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY

    try:
        kwargs = {}
        location_id = django_settings.STRIPE_TERMINAL_LOCATION_ID
        if location_id:
            kwargs['location'] = location_id
        conn_token = stripe_lib.terminal.ConnectionToken.create(**kwargs)
    except stripe_lib.error.StripeError as exc:
        logger.error("Stripe connection token error: %s", exc)
        return Response({'error': str(exc)}, status=502)

    return Response({'secret': conn_token.secret})


@api_view(['POST'])
@authentication_classes([ScannerSessionAuthentication])
@permission_classes([IsScannerAuthenticated])
def scanner_stripe_terminal_payment_intent(request):
    """
    POST /api/scanner/stripe/terminal-payment-intent/
    Body: {event_id, line_items: [{ticket_type_id, quantity}]}
    Creates a card-present PaymentIntent for the scanner session's event.
    """
    event_id = request.data.get('event_id', '').strip()
    line_items = request.data.get('line_items', [])

    mismatch = _require_matching_scanner_event(request, event_id)
    if mismatch is not None:
        return mismatch

    if not line_items:
        return Response({'error': 'line_items are required'}, status=400)

    payload, status = _create_terminal_payment_intent(request.auth.event, line_items)
    return Response(payload, status=status)


@api_view(['POST'])
@authentication_classes([ScannerSessionAuthentication])
@permission_classes([IsScannerAuthenticated])
def scanner_sell(request):
    """
    POST /api/scanner/sell/
    Body: {event_id, payment_intent_id, buyer_name, buyer_email,
           line_items: [{ticket_type_id, quantity, name, price}]}
    Verifies the PaymentIntent succeeded, then creates an in-person ticket
    order. checked_in_by is None because the seller is a scanner-PIN guest.
    """
    event_id = request.data.get('event_id', '').strip()
    payment_intent_id = request.data.get('payment_intent_id', '').strip()
    buyer_name = request.data.get('buyer_name', '').strip()
    buyer_email = request.data.get('buyer_email', '').strip().lower()
    line_items = request.data.get('line_items', [])

    mismatch = _require_matching_scanner_event(request, event_id)
    if mismatch is not None:
        return mismatch

    if not payment_intent_id or not buyer_email or not line_items:
        return Response(
            {'error': 'payment_intent_id, buyer_email, and line_items are required'},
            status=400,
        )

    payload, status = _finalize_in_person_sale(
        event=request.auth.event,
        payment_intent_id=payment_intent_id,
        buyer_name=buyer_name,
        buyer_email=buyer_email,
        line_items=line_items,
        checked_in_by=None,
    )
    return Response(payload, status=status)


@api_view(['GET'])
@authentication_classes([ScannerSessionAuthentication])
@permission_classes([IsScannerAuthenticated])
def scanner_sell_eligibility(request):
    """
    GET /api/scanner/sell-eligibility/?event_id=<uuid>
    Returns whether the scanner session may sell in person right now, based
    on the merchant's Tap to Pay enablement state.
    """
    requested = request.query_params.get('event_id', '').strip()
    mismatch = _require_matching_scanner_event(request, requested)
    if mismatch is not None:
        return mismatch

    status = _resolve_tap_to_pay_status(request.auth.event.organization)
    if status == 'enabled':
        return Response({'eligible': True})
    reason = 'tap_to_pay_unsupported' if status == 'unsupported' else 'tap_to_pay_pending'
    return Response({'eligible': False, 'reason': reason})


# ---------------------------------------------------------------------------
# Tap to Pay on iPhone (Apple entitlement compliance)
# ---------------------------------------------------------------------------

def _client_ip(request):
    """Best-effort client IP, honoring X-Forwarded-For when present."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip() or None
    return request.META.get('REMOTE_ADDR') or None


def _send_receipt_email_for_order(order, to_email):
    """Send the standard order-confirmation email synchronously to an arbitrary address.

    Mirrors send_order_confirmation_email_task in tickets/tasks.py but runs inline
    (this is a manual scanner action — no need to queue Celery) and lets the caller
    override the recipient.
    Returns (status, error_message): ('sent', '') or ('failed', '<reason>').
    """
    from email.mime.image import MIMEImage
    from decimal import Decimal as _Decimal

    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.conf import settings as django_settings
    from tickets.utils import generate_qr_png_bytes

    qr_png_bytes = generate_qr_png_bytes(order.order_number)
    try:
        service_fee = _Decimal(order.stripe_checkout_session.platform_fee_cents) / 100
    except Exception:
        service_fee = _Decimal('0.00')

    context = {
        'order': order,
        'customer': order.customer,
        'event': order.event,
        'tickets': list(order.tickets.all()),
        'show_qr_code': bool(qr_png_bytes),
        'service_fee': service_fee,
    }
    html_body = render_to_string('tickets/buy/order_confirmation_email.html', context)
    text_body = render_to_string('tickets/buy/order_confirmation_email.txt', context)

    msg = EmailMultiAlternatives(
        subject=f"Your receipt - {order.event.name}",
        body=text_body,
        from_email=django_settings.DEFAULT_FROM_EMAIL,
        to=[to_email],
    )
    msg.attach_alternative(html_body, 'text/html')
    if qr_png_bytes:
        qr_attachment = MIMEImage(qr_png_bytes)
        qr_attachment.add_header('Content-ID', '<qrcode>')
        qr_attachment.add_header('Content-Disposition', 'inline', filename='qrcode.png')
        msg.attach(qr_attachment)

    try:
        msg.send()
        return 'sent', ''
    except Exception as exc:
        logger.exception("Failed to send order receipt email for order %s", order.pk)
        return 'failed', str(exc)[:1000]


def _send_receipt_email_for_intent(summary, to_email):
    """Send the attempt-receipt email (declined/cancelled/etc.) for a PaymentIntent.

    summary is the dict built in scanner_receipt's payment_intent_id branch.
    Returns (status, error_message).
    """
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.conf import settings as django_settings

    html_body = render_to_string('tickets/buy/tap_to_pay_attempt_receipt.html', summary)
    text_body = render_to_string('tickets/buy/tap_to_pay_attempt_receipt.txt', summary)

    subject_event = summary.get('event_name') or summary.get('merchant_business_name') or 'your purchase'
    msg = EmailMultiAlternatives(
        subject=f"Transaction receipt - {subject_event}",
        body=text_body,
        from_email=django_settings.DEFAULT_FROM_EMAIL,
        to=[to_email],
    )
    msg.attach_alternative(html_body, 'text/html')
    try:
        msg.send()
        return 'sent', ''
    except Exception as exc:
        logger.exception("Failed to send Tap-to-Pay attempt receipt email")
        return 'failed', str(exc)[:1000]


def _resolve_tap_to_pay_status(org):
    """Inspect the org's Stripe Connect account and return one of
    'pending' / 'enabled' / 'unsupported'.

    Cached per-org for TAP_TO_PAY_STATUS_CACHE_TTL seconds so the iOS app
    can poll on every foreground transition without burning Stripe quota.
    """
    from django.conf import settings as django_settings

    cache_key = f'tap_to_pay_status:{org.pk}'
    cached = django_cache.get(cache_key)
    if cached is not None:
        return cached

    status = 'pending'
    account_id = (org.stripe_account_id or '').strip()
    if account_id:
        import stripe as stripe_lib
        stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
        try:
            account = stripe_lib.Account.retrieve(account_id)
        except stripe_lib.error.StripeError as exc:
            logger.warning("Stripe Account retrieve failed for org %s: %s", org.pk, exc)
            account = None

        if account is not None:
            capabilities = getattr(account, 'capabilities', None) or {}
            if hasattr(capabilities, 'get'):
                ttp_cap = capabilities.get('tap_to_pay_payments')
            else:
                ttp_cap = None
            country = (getattr(account, 'country', '') or '').upper()

            if ttp_cap == 'active':
                status = 'enabled'
            elif country and country not in django_settings.TAP_TO_PAY_SUPPORTED_COUNTRIES:
                status = 'unsupported'

    try:
        django_cache.set(cache_key, status, timeout=django_settings.TAP_TO_PAY_STATUS_CACHE_TTL)
    except Exception:
        pass
    return status


@api_view(['GET'])
@authentication_classes([ScannerSessionAuthentication])
@permission_classes([IsScannerAuthenticated])
def merchant_status(request):
    """
    GET /api/merchant/status/
    Returns the current Tap to Pay enablement state for the merchant tied
    to the scanner session. Designed to be polled on every iOS foreground
    transition; the first 'enabled' result fires the Apple §3.3 awareness
    splash on the client.

    Response shape is composable — future phases will add sibling keys
    (kyc, payouts, country) alongside tap_to_pay rather than nesting them.
    """
    org = request.auth.event.organization
    status = _resolve_tap_to_pay_status(org)
    return Response({'tap_to_pay': {'status': status}})


@api_view(['GET'])
@authentication_classes([ScannerSessionAuthentication])
@permission_classes([IsScannerAuthenticated])
def tap_to_pay_terms_version(request):
    """
    GET /api/tap-to-pay/terms-version/
    Returns the opaque version identifier of Apple's currently-published
    Tap to Pay on iPhone Terms & Conditions. The client treats it as an
    equality check (no semver / date math).
    """
    from django.conf import settings as django_settings
    return Response({'version': django_settings.TAP_TO_PAY_TERMS_VERSION})


@api_view(['POST'])
@authentication_classes([ScannerSessionAuthentication])
@permission_classes([IsScannerAuthenticated])
def tap_to_pay_terms_acceptance(request):
    """
    POST /api/tap-to-pay/terms-acceptance/
    Body: {version}
    Records a merchant acceptance of Apple's Tap to Pay T&Cs for audit.
    Append-only — never dedupes.
    """
    version = (request.data.get('version') or '').strip()
    if not version:
        return Response({'error': 'version is required'}, status=400)

    session = request.auth
    user_agent = request.META.get('HTTP_USER_AGENT', '')[:512]

    TapToPayTermsAcceptance.objects.create(
        scanner_session=session,
        organization=session.event.organization,
        version=version[:64],
        client_ip=_client_ip(request),
        user_agent=user_agent,
    )
    return Response({'ok': True}, status=201)


# ---------------------------------------------------------------------------
# Scanner — Receipt (success by order_id, or declined/cancelled by payment_intent_id)
# ---------------------------------------------------------------------------

@api_view(['POST'])
@authentication_classes([ScannerSessionAuthentication])
@permission_classes([IsScannerAuthenticated])
def scanner_receipt(request):
    """
    POST /api/scanner/receipt/
    Body: {order_id?, payment_intent_id?, channel, contact}

    Exactly one of order_id or payment_intent_id must be supplied. When
    order_id is present we send the standard order confirmation. When
    payment_intent_id is present we look up the PaymentIntent in Stripe and
    send a transaction-attempt summary (Apple §5.10: receipts must be
    available even for declined/cancelled transactions, which have no order).
    """
    session = request.auth
    event = session.event
    org = event.organization

    order_id = (request.data.get('order_id') or '').strip()
    payment_intent_id = (request.data.get('payment_intent_id') or '').strip()
    channel = (request.data.get('channel') or '').strip().lower()
    contact = (request.data.get('contact') or '').strip()

    if bool(order_id) == bool(payment_intent_id):
        return Response(
            {'error': 'exactly one of order_id and payment_intent_id is required'},
            status=400,
        )
    if channel not in ('email', 'sms'):
        return Response({'error': "channel must be 'email' or 'sms'"}, status=400)
    if not contact:
        return Response({'error': 'contact is required'}, status=400)
    if channel == 'sms':
        # No transactional SMS sender wired up; iOS treats 422 as the
        # "Receipts unavailable" fallback per the entitlement spec.
        return Response({'error': 'channel not supported'}, status=422)

    if order_id:
        order = (
            TicketOrder.objects
            .select_related('customer', 'event', 'event__venue', 'stripe_checkout_session')
            .prefetch_related('tickets')
            .filter(customer__organization=org, event=event, order_number=order_id)
            .first()
        )
        if order is None:
            return Response({'error': 'order not found'}, status=404)

        send_status, error_message = _send_receipt_email_for_order(order, contact)
        ReceiptSend.objects.create(
            organization=org,
            ticket_order=order,
            payment_intent_id='',
            channel=channel,
            contact=contact,
            status=send_status,
            error_message=error_message,
        )
        if send_status != 'sent':
            return Response({'error': 'failed to send receipt'}, status=502)
        return Response({'ok': True})

    # payment_intent_id branch
    import stripe as stripe_lib
    from django.conf import settings as django_settings
    from datetime import datetime, timezone as _tz

    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY

    try:
        pi = stripe_lib.PaymentIntent.retrieve(payment_intent_id)
    except stripe_lib.error.InvalidRequestError:
        return Response({'error': 'payment intent not found'}, status=404)
    except stripe_lib.error.StripeError as exc:
        logger.error("Stripe PaymentIntent retrieve error: %s", exc)
        return Response({'error': str(exc)}, status=502)

    # Resolve event from PI metadata if our /organizer/sell/ flow stamped it;
    # otherwise fall back to the scanner session's event (best effort).
    pi_event = None
    try:
        metadata = getattr(pi, 'metadata', None)
        if metadata is not None and hasattr(metadata, 'get'):
            pi_event_id = metadata.get('event_id')
        else:
            pi_event_id = None
    except Exception:
        pi_event_id = None
    if pi_event_id:
        pi_event = Event.objects.filter(organization=org, id=pi_event_id).first()
    if pi_event is None:
        pi_event = event

    status_raw = (getattr(pi, 'status', '') or '').lower()
    status_labels = {
        'succeeded': 'Approved',
        'canceled': 'Cancelled',
        'cancelled': 'Cancelled',
        'requires_payment_method': 'Declined',
        'requires_confirmation': 'Incomplete',
        'requires_action': 'Incomplete',
        'requires_capture': 'Authorized',
        'processing': 'Processing',
    }
    status_label = status_labels.get(status_raw, status_raw.replace('_', ' ').title() or 'Unknown')

    decline_message = ''
    try:
        lpe = getattr(pi, 'last_payment_error', None)
        if lpe is not None:
            decline_message = (
                getattr(lpe, 'message', None)
                or (lpe.get('message', '') if isinstance(lpe, dict) else '')
                or ''
            )
    except Exception:
        decline_message = ''

    succeeded = status_raw == 'succeeded'
    summary = {
        'merchant_business_name': org.name,
        'event_name': pi_event.name if pi_event else '',
        'amount': (pi.amount / 100.0) if getattr(pi, 'amount', None) is not None else None,
        'currency': (getattr(pi, 'currency', '') or '').upper(),
        'created': datetime.fromtimestamp(pi.created, tz=_tz.utc) if getattr(pi, 'created', None) else None,
        'status_label': status_label,
        'decline_message': decline_message,
        'succeeded': succeeded,
        'note': '' if succeeded else 'No charge was made to your card.',
        'payment_intent_id': getattr(pi, 'id', payment_intent_id),
    }

    send_status, error_message = _send_receipt_email_for_intent(summary, contact)
    ReceiptSend.objects.create(
        organization=org,
        ticket_order=None,
        payment_intent_id=payment_intent_id[:255],
        channel=channel,
        contact=contact,
        status=send_status,
        error_message=error_message,
    )
    if send_status != 'sent':
        return Response({'error': 'failed to send receipt'}, status=502)
    return Response({'ok': True})
