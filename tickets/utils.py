"""Organization helpers for scoping data by current user's organization."""

import base64
import io
import logging
from functools import wraps

from django.shortcuts import redirect


logger = logging.getLogger(__name__)


def generate_qr_b64(data: str) -> str:
    """Return a base64-encoded PNG QR code for the given data string."""
    png_bytes = generate_qr_png_bytes(data)
    return base64.b64encode(png_bytes).decode() if png_bytes else ''


def generate_qr_png_bytes(data: str) -> bytes | None:
    """Return PNG bytes for a QR code of the given data, or None if qrcode is unavailable."""
    try:
        import qrcode
        img = qrcode.make(data)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    except ImportError:
        return None


# Prefix marking a per-ticket QR payload, distinguishing it from a legacy
# order-number payload (e.g. "ORD-...") so the scanner can route the two paths.
TICKET_QR_PREFIX = 'TKT-'


def ticket_qr_payload(ticket) -> str:
    """Return the scannable payload encoded into an individual ticket's QR code."""
    return f"{TICKET_QR_PREFIX}{ticket.id}"


def build_ticket_qr_codes(tickets):
    """Return per-ticket QR data for emails/templates.

    Produces a list of dicts ``[{'ticket', 'cid', 'png_bytes'}]`` — one entry per
    ticket — where ``cid`` is the inline-image Content-ID stem (e.g. ``qrcode-0``).
    Returns ``[]`` if qrcode is unavailable (mirrors generate_qr_png_bytes' ImportError
    path), so callers can treat an empty list as "no QR codes to show".
    """
    out = []
    for i, ticket in enumerate(tickets):
        png = generate_qr_png_bytes(ticket_qr_payload(ticket))
        if not png:
            return []
        out.append({'ticket': ticket, 'cid': f'qrcode-{i}', 'png_bytes': png})
    return out


def generate_username(first_name, last_name):
    """Generate a unique slugified username from first + last name."""
    from django.utils.text import slugify
    from django.contrib.auth.models import User
    base = slugify(f"{first_name}{last_name}") or 'user'
    username = base
    counter = 1
    while User.objects.filter(username=username).exists():
        username = f"{base}{counter}"
        counter += 1
    return username


def get_organization(request):
    """
    Return the current user's organization, or None if not authenticated or no org.

    Caches the organization object on the request so that multiple calls in the
    same request (decorator + view body + context processor) cost zero DB queries
    after the first. The PK is also stored in the session for cross-request caching.
    """
    if not request.user.is_authenticated:
        return None

    # Per-request object cache - avoids repeated DB hits within the same request
    _attr = '_cached_org'
    _sentinel = '_cached_org_set'
    if getattr(request, _sentinel, False):
        return getattr(request, _attr, None)

    from .models import Organization, OrganizationMembership, UserProfile

    org = None
    org_id = request.session.get('_org_id')
    if org_id is not None:
        # Fast path: org id cached in session
        if org_id == '':
            request._cached_org = None
            request._cached_org_set = True
            return None  # user has no org
        try:
            org = Organization.objects.get(pk=org_id)
        except Organization.DoesNotExist:
            # Stale session value - fall through to DB lookup
            org = None

        # Validate that the user is still a member of this org. Superusers
        # bypass the check so they can impersonate any org via session.
        if org is not None and not request.user.is_superuser:
            is_member = OrganizationMembership.objects.filter(
                user=request.user, organization_id=org_id
            ).exists()
            if not is_member:
                logger.warning(
                    "Stale _org_id in session for user %s: org %s has no membership; falling back.",
                    request.user.pk,
                    org_id,
                )
                org = None
                request.session.pop('_org_id', None)

    if org is None and org_id != '':
        # Slow path: check membership table first (multi-org), fall back to legacy profile.organization
        profile, _ = UserProfile.objects.select_related('organization').get_or_create(
            user=request.user,
            defaults={'organization_id': None},
        )
        first_membership = (
            OrganizationMembership.objects
            .filter(user=request.user)
            .select_related('organization')
            .order_by('created_at')
            .first()
        )
        org = first_membership.organization if first_membership else profile.organization
        request.session['_org_id'] = str(org.pk) if org else ''

    request._cached_org = org
    request._cached_org_set = True
    return org


def clear_org_cache(request):
    """Remove the cached organization from the session and request.

    Call this when a user's org assignment changes (e.g. admin reassignment,
    org creation) so the next get_organization() re-fetches from the DB.
    """
    request.session.pop('_org_id', None)
    request._cached_org = None
    request._cached_org_set = False


def require_org(view_func):
    """
    Decorator that requires the user to have an organization.
    Redirects to org_required page if profile.organization is None.
    Superusers bypass (they can have no org and still access admin).
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())
        org = get_organization(request)
        if org is None and not request.user.is_superuser:
            return redirect('tickets:org_required')
        return view_func(request, *args, **kwargs)
    return _wrapped


def require_organizer(view_func):
    """
    Decorator that requires the user to have the organizer role.
    Pure attendees are redirected to their dashboard. Superusers bypass.
    Stack after @login_required and @require_org.
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if request.user.is_superuser:
            return view_func(request, *args, **kwargs)
        from .models import UserProfile
        try:
            profile = request.user.profile
        except UserProfile.DoesNotExist:
            return redirect('tickets:attendee_dashboard')
        if not profile.is_organizer:
            return redirect('tickets:attendee_dashboard')
        # Respect session view mode
        if request.session.get('_view_mode') == 'attendee':
            return redirect('tickets:attendee_dashboard')
        return view_func(request, *args, **kwargs)
    return _wrapped


def _org_role_required(min_check):
    """Factory for org-role decorators. min_check is a lambda(profile) -> bool."""
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)
            from .models import UserProfile, OrganizationMembership
            from django.http import HttpResponseForbidden
            try:
                profile = request.user.profile
            except UserProfile.DoesNotExist:
                return HttpResponseForbidden('Access denied.')
            # Respect session view mode
            if request.session.get('_view_mode') == 'attendee':
                return HttpResponseForbidden('Access denied.')
            # Resolve role from active org's membership (in-memory only, no .save())
            active_org = get_organization(request)
            if active_org is not None:
                try:
                    membership = OrganizationMembership.objects.get(
                        user=request.user, organization=active_org
                    )
                    profile.org_role = membership.org_role  # in-memory only, no .save()
                except OrganizationMembership.DoesNotExist:
                    pass  # fall through to legacy profile.org_role
            if not min_check(profile):
                return HttpResponseForbidden('Access denied.')
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator


def require_host(view_func):
    """Require org_role of Host, Admin, or Owner (or superuser)."""
    return _org_role_required(lambda p: p.is_org_host)(view_func)


def require_admin(view_func):
    """Require org_role of Admin or Owner (or superuser)."""
    return _org_role_required(lambda p: p.is_org_admin)(view_func)


def require_owner(view_func):
    """Require org_role of Owner (or superuser)."""
    return _org_role_required(lambda p: p.is_org_owner)(view_func)


def next_order_number():
    """Return the next globally-unique sequential order number string. Call inside a transaction."""
    from .models import OrderCounter
    seq = OrderCounter.next()
    return f"#{seq:05d}"


def link_customer_to_buyer(customer, buyer_email):
    """Link Customer to its auth.User account and copy verified phone if blank.

    Direct-ticketing buyers authenticate before checkout, so we can resolve
    the buyer's User account by email. Sets Customer.user (if NULL) and copies
    UserProfile.phone_number onto Customer.phone (if blank). Never overwrites
    existing values.
    """
    if customer.user_id and customer.phone:
        return
    from django.contrib.auth.models import User
    user = (
        User.objects
        .filter(email__iexact=buyer_email)
        .select_related('profile')
        .first()
    )
    if user is None:
        return
    fields_to_update = []
    if not customer.user_id:
        customer.user = user
        fields_to_update.append('user')
    if not customer.phone:
        phone = getattr(getattr(user, 'profile', None), 'phone_number', None)
        if phone:
            customer.phone = phone
            fields_to_update.append('phone')
    if fields_to_update:
        customer.save(update_fields=fields_to_update)


def calculate_platform_fee_cents(subtotal_cents: int) -> int:
    """Platform service fee: 8% of subtotal + $0.99, in cents."""
    return round(subtotal_cents * 0.08) + 99


def extract_fee_from_display_cents(display_total_cents: int) -> int:
    """Extract platform fee from a fee-inclusive display price (in cents).

    Reverses: display_total = subtotal * 1.08 + 99
    So:       subtotal = (display_total - 99) / 1.08
              fee = display_total - subtotal
    """
    if display_total_cents <= 0:
        return 0
    subtotal_cents = round((display_total_cents - 99) / 1.08)
    return display_total_cents - subtotal_cents
