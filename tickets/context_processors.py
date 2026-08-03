from .utils import get_organization
from .feature_flags import (
    smart_pricing_recommendations_enabled as smart_pricing_recommendations_enabled_flag,
    waitlist_enabled as wl_enabled,
    browse_events_enabled as browse_events_enabled_flag,
    loyalty_enabled as loyalty_enabled_flag,
    external_events_enabled as external_events_enabled_flag,
)


def organization_context(request):
    """Inject org_name, is_organizer, is_attendee, user_role, view_mode, and org role flags into every template context.

    Uses the session-cached get_organization() so this adds zero DB queries
    after the first request in a session.
    """
    org = get_organization(request)
    ctx = {
        'org_name': org.name if org else 'Cue',
        'org': org,
        'user_role': 'organizer',
        'is_organizer': True,
        'is_attendee': False,
        'view_mode': 'organizer',
        'org_role': None,
        'is_org_owner': False,
        'is_org_admin': False,
        'is_org_host': False,
        'user_orgs': [],
        # True when an authenticated attendee has been approved off the organizer
        # waitlist but hasn't created their org yet — the nav points them straight
        # to org creation instead of the (misleading) "Become an Organizer" form.
        'organizer_waitlist_approved': False,
    }
    if request.user.is_authenticated:
        try:
            from .models import OrganizationMembership, UserProfile
            profile = request.user.profile
            ctx['user_role'] = profile.role          # actual DB role — never overridden

            # Fetch all memberships in one query; derive active org role and switcher list
            user_memberships = list(
                OrganizationMembership.objects
                .filter(user=request.user)
                .select_related('organization')
                .order_by('organization__name')
            )
            ctx['user_orgs'] = user_memberships
            active_org_role = next(
                (m.org_role for m in user_memberships if org and m.organization_id == org.pk),
                profile.org_role,  # legacy fallback for users not yet in membership table
            )
            ctx['org_role'] = active_org_role
            ctx['is_org_owner'] = active_org_role == UserProfile.OrgRole.OWNER
            ctx['is_org_admin'] = active_org_role in (UserProfile.OrgRole.OWNER, UserProfile.OrgRole.ADMIN)
            ctx['is_org_host'] = active_org_role in (
                UserProfile.OrgRole.OWNER, UserProfile.OrgRole.ADMIN, UserProfile.OrgRole.HOST
            )
            if profile.is_organizer:
                view_mode = request.session.get('_view_mode', 'organizer')
                ctx['view_mode'] = view_mode
                if view_mode == 'attendee':
                    ctx['is_organizer'] = False
                    ctx['is_attendee'] = True
                else:
                    ctx['is_organizer'] = True
                    ctx['is_attendee'] = profile.is_attendee
            else:
                ctx['is_organizer'] = False
                ctx['is_attendee'] = profile.is_attendee
                ctx['view_mode'] = 'attendee'

            # Approved-but-org-less attendees get a direct "Create your organization"
            # nav link. One indexed .exists() only for non-organizers.
            if not profile.is_organizer:
                from .models import OrganizerWaitlist
                ctx['organizer_waitlist_approved'] = OrganizerWaitlist.objects.filter(
                    email=request.user.email,
                    status=OrganizerWaitlist.Status.APPROVED,
                ).exists()
        except Exception:
            pass

    # SMS section visibility: the Marketing SMS tab carries native sends (gated
    # by sms_marketing_enabled) plus read-only SlickText linked results. It's
    # always shown to org hosts so linked results stay reachable even when
    # native SMS is off; the page itself hides native-only UI in that case.
    ctx['sms_section_visible'] = ctx['is_org_host']

    ctx['outstanding_actions_count'] = 0
    if org and ctx['is_organizer']:
        try:
            from .models import AIRecommendation
            ctx['outstanding_actions_count'] = AIRecommendation.outstanding_for_org(org).count()
        except Exception:
            pass

    # True when an internal admin is currently logged in as this user via the
    # "Log in as" flow — drives the impersonation banner in base.html.
    ctx['is_impersonating'] = bool(request.session.get('_impersonator_id'))
    return ctx


def feature_flags_context(request):
    """Inject feature flag values into every template context."""
    org = get_organization(request)
    return {
        'smart_pricing_recommendations_enabled': smart_pricing_recommendations_enabled_flag(request.user),
        'waitlist_feature_enabled': wl_enabled(org),
        'browse_events_enabled': browse_events_enabled_flag(),
        'loyalty_feature_enabled': loyalty_enabled_flag(org),
        'external_events_enabled': external_events_enabled_flag(org),
    }
