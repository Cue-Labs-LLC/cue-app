"""
Mobile API views for the Eventflow app.
All views are plain Django function-based views decorated with DRF's @api_view.
No DRF ViewSets are used, in keeping with the project's FBV-only rule.
"""
import logging
from decimal import Decimal, InvalidOperation

from django.contrib.auth import authenticate
from django.core.cache import cache as django_cache
from django.db import transaction
from django.db.models import Count, F, IntegerField, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import (
    Customer,
    Event,
    SaleableTicketType,
    Ticket,
    TicketOrder,
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

    events = (
        Event.objects
        .filter(organization=org, start_date__gte=today)
        .annotate(
            checked_in_count=Coalesce(Subquery(checked_in_sq, output_field=IntegerField()), 0),
            total_tickets=Coalesce(Subquery(total_tickets_sq, output_field=IntegerField()), 0),
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
            return Response({
                'status': 'already_checked_in',
                'order_number': order.order_number,
                'customer_name': order.customer.name,
                'checked_in_at': order.checked_in_at.isoformat(),
                'ticket_types': [t.ticket_type for t in order.tickets.all()],
            }, status=200)

        order.checked_in_at = timezone.now()
        order.checked_in_by = request.user
        order.save(update_fields=['checked_in_at', 'checked_in_by'])

    return Response({
        'status': 'checked_in',
        'order_number': order.order_number,
        'customer_name': order.customer.name,
        'checked_in_at': order.checked_in_at.isoformat(),
        'ticket_types': [t.ticket_type for t in order.tickets.all()],
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

    amount_cents = 0
    resolved_items = []
    for item in line_items:
        tt_id = item.get('ticket_type_id', '')
        qty = int(item.get('quantity', 1))
        if qty < 1:
            return Response({'error': 'quantity must be >= 1'}, status=400)

        try:
            tt = SaleableTicketType.objects.get(id=tt_id, event=event, is_active=True)
        except SaleableTicketType.DoesNotExist:
            return Response({'error': f'Ticket type {tt_id} not found for this event'}, status=400)

        remaining = tt.remaining_quantity()
        if remaining is not None and qty > remaining:
            return Response({'error': f'Only {remaining} tickets remaining for {tt.name}'}, status=400)

        item_cents = int((tt.price * qty * 100).to_integral_value())
        amount_cents += item_cents
        resolved_items.append({'tt': tt, 'qty': qty})

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
        return Response({'error': str(exc)}, status=502)

    return Response({
        'client_secret': pi.client_secret,
        'payment_intent_id': pi.id,
        'amount_cents': amount_cents,
        'currency': django_settings.STRIPE_CURRENCY,
    }, status=201)


# ---------------------------------------------------------------------------
# Organizer — Sell (post-terminal payment fulfillment)
# ---------------------------------------------------------------------------

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

    import stripe as stripe_lib
    from django.conf import settings as django_settings

    stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY

    # Verify payment succeeded
    try:
        pi = stripe_lib.PaymentIntent.retrieve(payment_intent_id)
    except stripe_lib.error.StripeError as exc:
        logger.error("Stripe PaymentIntent retrieve error: %s", exc)
        return Response({'error': str(exc)}, status=502)

    if pi.status != 'succeeded':
        return Response({'error': f'PaymentIntent status is {pi.status!r}, expected succeeded'}, status=400)

    # Validate line items and compute total
    total_amount = Decimal('0.00')
    resolved = []
    for item in line_items:
        tt_id = item.get('ticket_type_id', '')
        qty = int(item.get('quantity', 1))
        item_name = item.get('name', '')
        try:
            item_price = Decimal(str(item.get('price', '0')))
        except InvalidOperation:
            return Response({'error': f'Invalid price for item {item_name}'}, status=400)

        try:
            tt = SaleableTicketType.objects.get(id=tt_id, event=event)
        except SaleableTicketType.DoesNotExist:
            return Response({'error': f'Ticket type {tt_id} not found'}, status=400)

        total_amount += item_price * qty
        resolved.append({'tt': tt, 'tt_id': tt_id, 'qty': qty, 'name': item_name or tt.name, 'price': item_price})

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
            checked_in_by=request.user,
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

    return Response({
        'order_number': order.order_number,
        'order_id': str(order.pk),
        'total_amount': str(order.total_amount),
        'ticket_count': ticket_count,
    }, status=201)
