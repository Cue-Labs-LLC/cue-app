from django.urls import path
from tickets import api_views

urlpatterns = [
    # Auth
    path('auth/login/', api_views.api_login, name='api_login'),
    path('auth/scanner-login/', api_views.scanner_login, name='api_scanner_login'),
    path('auth/phone/start/', api_views.api_phone_start, name='api_phone_start'),
    path('auth/phone/verify/', api_views.api_phone_verify, name='api_phone_verify'),
    path('auth/org/select/', api_views.api_org_select, name='api_org_select'),

    # Scanner (PIN-based guest check-in)
    path('scanner/event/', api_views.scanner_event, name='api_scanner_event'),
    path('scanner/checkin/', api_views.scanner_checkin, name='api_scanner_checkin'),
    path('scanner/checkin-stats/', api_views.scanner_checkin_stats, name='api_scanner_checkin_stats'),
    path('scanner/orders/', api_views.scanner_orders, name='api_scanner_orders'),
    path('scanner/receipt/', api_views.scanner_receipt, name='api_scanner_receipt'),
    path('scanner/ticket-types/', api_views.scanner_ticket_types, name='api_scanner_ticket_types'),
    path('scanner/stripe/connection-token/', api_views.scanner_stripe_connection_token, name='api_scanner_stripe_connection_token'),
    path('scanner/stripe/terminal-payment-intent/', api_views.scanner_stripe_terminal_payment_intent, name='api_scanner_stripe_terminal_pi'),
    path('scanner/sell/', api_views.scanner_sell, name='api_scanner_sell'),
    path('scanner/sell-eligibility/', api_views.scanner_sell_eligibility, name='api_scanner_sell_eligibility'),

    # Tap to Pay on iPhone (Apple entitlement compliance)
    path('tap-to-pay/terms-version/', api_views.tap_to_pay_terms_version, name='api_ttp_terms_version'),
    path('tap-to-pay/terms-acceptance/', api_views.tap_to_pay_terms_acceptance, name='api_ttp_terms_acceptance'),
    path('merchant/status/', api_views.merchant_status, name='api_merchant_status'),

    # Push notifications (APNs device-token registration)
    path('notification/device-token/', api_views.register_device_token, name='api_register_device_token'),

    # Organizer
    path('organizer/events/', api_views.organizer_events, name='api_organizer_events'),
    path('organizer/events/<uuid:event_id>/ticket-types/', api_views.organizer_ticket_types, name='api_organizer_ticket_types'),
    path('organizer/events/<uuid:event_id>/checkin/', api_views.organizer_event_checkin, name='api_organizer_event_checkin'),
    path('organizer/events/<uuid:event_id>/checkin-stats/', api_views.organizer_checkin_stats, name='api_organizer_checkin_stats'),
    path('organizer/events/<uuid:event_id>/orders/', api_views.organizer_event_orders, name='api_organizer_event_orders'),
    path('organizer/checkin/', api_views.organizer_checkin, name='api_organizer_checkin'),
    path('organizer/sell/', api_views.organizer_sell, name='api_organizer_sell'),
    # Same dual-auth view as scanner/receipt/ — the organizer app (Token auth)
    # posts here; the scanner app (Scanner PIN auth) posts to scanner/receipt/.
    path('organizer/receipt/', api_views.scanner_receipt, name='api_organizer_receipt'),

    # Agent / External API (API key auth, no user session)
    path('v1/events/upcoming/', api_views.agent_upcoming_events, name='api_agent_upcoming_events'),
    path('v1/events/', api_views.agent_events, name='api_agent_events'),
    path('v1/events/<uuid:event_id>/', api_views.agent_event_detail, name='api_agent_event_detail'),
    path('v1/customers/', api_views.agent_customers, name='api_agent_customers'),
    path('v1/customers/<uuid:customer_id>/', api_views.agent_customer_detail, name='api_agent_customer_detail'),
    path('v1/analytics/segments/', api_views.agent_analytics_segments, name='api_agent_analytics_segments'),
    path('v1/analytics/revenue/', api_views.agent_analytics_revenue, name='api_agent_analytics_revenue'),
    path('v1/orders/', api_views.agent_orders, name='api_agent_orders'),

    # Stripe Terminal
    path('stripe/connection-token/', api_views.stripe_connection_token, name='api_stripe_connection_token'),
    path('stripe/terminal-payment-intent/', api_views.stripe_terminal_payment_intent, name='api_stripe_terminal_pi'),

    # Stripe Connect — mobile onboarding (organizer Token auth, cueup:// deep-links)
    path('stripe/connect/onboarding-url/', api_views.stripe_connect_onboarding_url, name='api_stripe_connect_onboarding_url'),
]
