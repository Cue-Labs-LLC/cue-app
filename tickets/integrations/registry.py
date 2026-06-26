"""Single source of truth for the third-party integrations Cue supports.

The hub page, the Settings overview, and the connection-aware CTAs scattered
through the app all read from this registry so that an integration is described
in exactly one place. `is_connected` predicates are deliberately inlined here
(rather than importing the private helpers in tickets.views) to keep this module
a dependency-free leaf.
"""


def _gcal_connected(org):
    """True when this org has a saved calendar webhook connection."""
    from ..models import PipedreamCalendarConnection
    return PipedreamCalendarConnection.objects.filter(organization=org).exists()


# Each entry describes one integration. `category` groups integrations on the
# hub; `settings_url` is the URL name of its connection page; `is_connected`
# takes an Organization and returns a bool.
INTEGRATIONS = [
    {
        'key': 'mailchimp',
        'label': 'Mailchimp',
        'icon': 'bi-envelope-paper',
        'category': 'Marketing',
        'settings_url': 'tickets:mailchimp_settings',
        'description': 'Attach email campaign reporting to events for marketing analysis.',
        'is_connected': lambda org: bool(org.mailchimp_access_token and org.mailchimp_dc),
        'superuser_only': False,
    },
    {
        'key': 'slicktext',
        'label': 'SlickText',
        'icon': 'bi-chat-dots',
        'category': 'Marketing',
        'settings_url': 'tickets:slicktext_settings',
        'description': 'Link SMS broadcasts to events and track audience response.',
        'is_connected': lambda org: bool(org.slicktext_api_key and org.slicktext_brand_id),
        'superuser_only': False,
    },
    {
        'key': 'meta_ads',
        'label': 'Meta Ads',
        'icon': 'bi-megaphone',
        'category': 'Marketing',
        'settings_url': 'tickets:meta_ads_settings',
        'description': 'Connect ad spend for event-level marketing performance.',
        'is_connected': lambda org: bool(org.meta_ads_access_token and org.meta_ads_account_id),
        'superuser_only': False,
    },
    {
        'key': 'typeform',
        'label': 'Typeform',
        'icon': 'bi-clipboard2-data',
        'category': 'Surveys',
        'settings_url': 'tickets:typeform_settings',
        'description': 'Stream Typeform survey responses into Cue and auto-suggest events.',
        'is_connected': lambda org: bool(org.typeform_access_token),
        'superuser_only': False,
    },
    {
        'key': 'google_calendar',
        'label': 'Google Calendar',
        'icon': 'bi-calendar-check',
        'category': 'Calendar',
        'settings_url': 'tickets:settings_google_calendar',
        'description': 'Configure the calendar webhook used to ingest new events.',
        'is_connected': _gcal_connected,
        'superuser_only': True,
    },
]


def integration_statuses(org, *, include_superuser_only=False):
    """Return the registry annotated with a live `connected` flag for this org.

    `include_superuser_only` gates entries (e.g. Google Calendar) that should only
    be shown to superusers.
    """
    rows = []
    for entry in INTEGRATIONS:
        if entry['superuser_only'] and not include_superuser_only:
            continue
        rows.append({**entry, 'connected': bool(entry['is_connected'](org))})
    return rows
