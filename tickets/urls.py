from django.urls import path
from . import views

app_name = 'tickets'

urlpatterns = [
    # Authentication
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('signup/', views.signup_view, name='signup'),
    path('signup/verify/', views.verify_otp_view, name='verify_otp'),
    path('signup/resend-otp/', views.resend_otp_view, name='resend_otp'),
    path('password-reset/', views.password_reset_request, name='password_reset'),
    path('password-reset/done/', views.password_reset_done, name='password_reset_done'),
    path('password-reset-confirm/<uidb64>/<token>/', views.password_reset_confirm, name='password_reset_confirm'),
    path('password-reset-complete/', views.password_reset_complete, name='password_reset_complete'),
    
    # Health check endpoint
    path('health/', views.health_check, name='health_check'),

    # Public explore (no login)
    path('explore/', views.explore, name='explore'),

    # Organization (no-org flow)
    path('org-required/', views.org_required, name='org_required'),
    path('create-organization/', views.create_organization, name='create_organization'),
    path('members/', views.member_list, name='member_list'),
    path('members/invite/', views.member_invite, name='member_invite'),
    path('members/revoke/<uuid:token>/', views.invite_revoke, name='invite_revoke'),
    path('members/<int:profile_id>/role/', views.member_role_update, name='member_role_update'),
    path('invite/<uuid:token>/', views.invite_accept, name='invite_accept'),

    # Public attendee paths (no auth required)
    path('join/<slug:org_slug>/', views.attendee_signup_view, name='attendee_signup'),
    path('join/<slug:org_slug>/verify/', views.attendee_verify_otp_view, name='attendee_verify_otp'),
    path('login/phone/', views.phone_login_view, name='phone_login'),
    path('login/phone/verify/', views.phone_login_verify_view, name='phone_login_verify'),

    # Authenticated attendee path
    path('attendee/dashboard/', views.attendee_dashboard, name='attendee_dashboard'),

    # Landing (public) and Dashboard
    path('', views.landing),
    path('dashboard/', views.home, name='home'),
    
    # CSV Upload (price entry and results used when uploading from an event)
    path('upload/price-entry/<uuid:file_id>/', views.price_entry, name='price_entry'),
    path('upload/results/<uuid:file_id>/', views.upload_results, name='upload_results'),
    path('upload/<uuid:file_id>/delete/', views.upload_delete, name='upload_delete'),

    # Customers
    path('customers/', views.customer_list, name='customer_list'),
    path('customers/ltv-by-market/', views.customer_ltv_by_market, name='customer_ltv_by_market'),
    path('customers/<uuid:customer_id>/', views.customer_detail, name='customer_detail'),
    # Analytics - Segments
    path('analytics/segments/', views.customer_segments, name='customer_segments'),
    path('analytics/segments/recalculate/', views.recalculate_segments, name='recalculate_segments'),
    path('analytics/repeat-customers/', views.repeat_customers, name='repeat_customers'),
    path('analytics/cohort-retention/', views.cohort_retention, name='cohort_retention'),
    path('analytics/profitability/', views.profitability_overview, name='profitability_overview'),

    # Surveys (public - no login required)
    path('survey/<uuid:token>/', views.survey_form, name='survey_form'),
    path('survey/thank-you/', views.survey_thank_you, name='survey_thank_you'),

    # Events
    path('events/', views.event_list, name='event_list'),
    path('events/calendar/', views.event_calendar, name='event_calendar'),
    path('events/create/', views.event_type_select, name='event_type_select'),
    path('events/create/<str:ticketing_type>/', views.event_create, name='event_create'),
    path('events/<uuid:event_id>/', views.event_detail, name='event_detail'),
    path('events/<uuid:event_id>/edit/', views.event_edit, name='event_edit'),
    path('events/<uuid:event_id>/flyer/', views.event_flyer_upload, name='event_flyer_upload'),
    path('events/<uuid:event_id>/upload/', views.event_upload_csv, name='event_upload_csv'),
    path('events/<uuid:event_id>/delete/', views.event_delete, name='event_delete'),
    path('events/<uuid:event_id>/send-survey/', views.send_survey, name='send_survey'),
    path('events/<uuid:event_id>/expenses/add/', views.expense_create, name='expense_create'),
    path('events/<uuid:event_id>/expenses/<uuid:expense_id>/edit/', views.expense_edit, name='expense_edit'),
    path('events/<uuid:event_id>/expenses/<uuid:expense_id>/delete/', views.expense_delete, name='expense_delete'),
    path('events/<uuid:event_id>/income/add/', views.event_income_create, name='event_income_create'),
    path('events/<uuid:event_id>/income/<uuid:income_id>/edit/', views.event_income_edit, name='event_income_edit'),
    path('events/<uuid:event_id>/income/<uuid:income_id>/delete/', views.event_income_delete, name='event_income_delete'),

    # Income Sources (org-level)
    path('income-sources/', views.income_source_list, name='income_source_list'),
    path('income-sources/create/', views.income_source_create, name='income_source_create'),
    path('income-sources/<uuid:source_id>/edit/', views.income_source_edit, name='income_source_edit'),
    path('income-sources/<uuid:source_id>/delete/', views.income_source_delete, name='income_source_delete'),

    # Forecast Tool
    path('forecast/', views.forecast_tool, name='forecast_tool'),
    path('forecast/api/', views.forecast_api, name='forecast_api'),
    
    # Orders
    path('orders/<uuid:order_id>/', views.order_detail, name='order_detail'),
    
    # CSV Formats
    path('formats/', views.format_list, name='format_list'),
    path('formats/create/', views.format_create, name='format_create'),
    path('formats/<uuid:format_id>/edit/', views.format_edit, name='format_edit'),
    path('formats/<uuid:format_id>/delete/', views.format_delete, name='format_delete'),
    path('formats/<uuid:format_id>/set-default/', views.format_set_default, name='format_set_default'),
    
    # Tools
    path('tools/regenerate-event-doc/', views.regenerate_event_doc, name='regenerate_event_doc'),

    # Settings
    path('settings/google-calendar/', views.settings_google_calendar, name='settings_google_calendar'),
    path('settings/google-calendar/disconnect/', views.settings_google_calendar_disconnect, name='settings_google_calendar_disconnect'),

    # Venues
    path('venues/', views.venue_list, name='venue_list'),
    path('venues/create/', views.venue_create, name='venue_create'),
    path('venues/<uuid:venue_id>/edit/', views.venue_edit, name='venue_edit'),

    # Chat Agent
    path('chat/stream/', views.chat_stream, name='chat_stream'),
    path('chat/history/', views.chat_history, name='chat_history'),
    path('chat/conversations/', views.chat_conversations, name='chat_conversations'),

    # Direct Ticket Selling — Organizer
    path('events/<uuid:event_id>/ticket-types/create/', views.saleable_ticket_type_create, name='saleable_ticket_type_create'),
    path('events/<uuid:event_id>/ticket-types/<uuid:ticket_type_id>/edit/', views.saleable_ticket_type_edit, name='saleable_ticket_type_edit'),
    path('events/<uuid:event_id>/ticket-types/<uuid:ticket_type_id>/toggle/', views.saleable_ticket_type_toggle, name='saleable_ticket_type_toggle'),
    path('events/<uuid:event_id>/ticket-types/<uuid:ticket_type_id>/delete/', views.saleable_ticket_type_delete, name='saleable_ticket_type_delete'),

    # Direct Ticket Selling — Public (no auth)
    path('buy/<uuid:event_id>/', views.public_event_buy, name='public_event_buy'),
    path('checkout/success/', views.checkout_success, name='checkout_success'),
    path('checkout/cancel/', views.checkout_cancel, name='checkout_cancel'),

    # Stripe Webhook
    path('webhooks/stripe/', views.stripe_webhook, name='stripe_webhook'),
]
