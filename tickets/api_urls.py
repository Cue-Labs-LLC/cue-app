from django.urls import path
from tickets import api_views

urlpatterns = [
    # Auth
    path('auth/login/', api_views.api_login, name='api_login'),
    path('auth/scanner-login/', api_views.scanner_login, name='api_scanner_login'),

    # Scanner (PIN-based guest check-in)
    path('scanner/event/', api_views.scanner_event, name='api_scanner_event'),
    path('scanner/checkin/', api_views.scanner_checkin, name='api_scanner_checkin'),
    path('scanner/checkin-stats/', api_views.scanner_checkin_stats, name='api_scanner_checkin_stats'),
    path('scanner/orders/', api_views.scanner_orders, name='api_scanner_orders'),

    # Organizer
    path('organizer/events/', api_views.organizer_events, name='api_organizer_events'),
    path('organizer/events/<uuid:event_id>/ticket-types/', api_views.organizer_ticket_types, name='api_organizer_ticket_types'),
    path('organizer/events/<uuid:event_id>/checkin-stats/', api_views.organizer_checkin_stats, name='api_organizer_checkin_stats'),
    path('organizer/events/<uuid:event_id>/orders/', api_views.organizer_event_orders, name='api_organizer_event_orders'),
    path('organizer/checkin/', api_views.organizer_checkin, name='api_organizer_checkin'),
    path('organizer/sell/', api_views.organizer_sell, name='api_organizer_sell'),

    # Agent / External API (API key auth, no user session)
    path('v1/events/upcoming/', api_views.agent_upcoming_events, name='api_agent_upcoming_events'),

    # Stripe Terminal
    path('stripe/connection-token/', api_views.stripe_connection_token, name='api_stripe_connection_token'),
    path('stripe/terminal-payment-intent/', api_views.stripe_terminal_payment_intent, name='api_stripe_terminal_pi'),
]
