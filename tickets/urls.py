from django.urls import path, re_path
from django.views.generic import RedirectView
from . import views

app_name = 'tickets'

urlpatterns = [
    # Authentication - unified phone-first entry point
    path('login/', views.unified_login_view, name='login'),
    path('login/verify/', views.unified_verify_view, name='unified_verify'),
    path('login/resend/', views.unified_resend_view, name='unified_resend'),
    path('login/complete-profile/', views.complete_profile_view, name='complete_profile'),
    path('login/email/', views.email_login_view, name='email_login'),
    path('login/email/verify/', views.email_verify_view, name='email_verify'),
    path('login/email/resend/', views.email_resend_view, name='email_resend'),
    path('login/email/complete-profile/', views.email_complete_profile_view, name='email_complete_profile'),
    path('login/verify-email/', views.verify_email_after_profile_view, name='verify_email_after_profile'),
    path('login/resend-email/', views.resend_email_after_profile_view, name='resend_email_after_profile'),
    path('login/email/verify-phone/', views.verify_phone_after_profile_view, name='verify_phone_after_profile'),
    path('login/email/resend-phone/', views.resend_phone_after_profile_view, name='resend_phone_after_profile'),
    path('logout/', views.logout_view, name='logout'),
    # Inline modal auth (JSON endpoints for checkout flow)
    path('auth/modal/start/', views.modal_auth_start, name='modal_auth_start'),
    path('auth/modal/verify/', views.modal_auth_verify, name='modal_auth_verify'),
    path('auth/modal/complete/', views.modal_auth_complete, name='modal_auth_complete'),
    # Old attendee phone signup - redirect to unified login
    path('signup/', RedirectView.as_view(pattern_name='tickets:login', permanent=False), name='signup'),
    path('signup/verify/', RedirectView.as_view(pattern_name='tickets:login', permanent=False), name='verify_otp'),
    path('signup/resend-otp/', RedirectView.as_view(pattern_name='tickets:login', permanent=False), name='resend_otp'),
    path('become-organizer/', views.become_organizer_view, name='become_organizer'),
    path('become-organizer/thanks/', views.waitlist_success_view, name='waitlist_success'),
    path('password-reset/', views.password_reset_request, name='password_reset'),
    path('password-reset/done/', views.password_reset_done, name='password_reset_done'),
    path('password-reset-confirm/<uidb64>/<token>/', views.password_reset_confirm, name='password_reset_confirm'),
    path('password-reset-complete/', views.password_reset_complete, name='password_reset_complete'),
    
    # Health check endpoint
    path('health/', views.health_check, name='health_check'),

    # Public explore (no login)
    path('explore/', views.explore, name='explore'),
    path('org/<slug:slug>/', views.public_org_profile, name='public_org_profile'),

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
    # Old phone login - redirect to unified login
    path('login/phone/', RedirectView.as_view(pattern_name='tickets:login', permanent=False), name='phone_login'),
    path('login/phone/verify/', RedirectView.as_view(pattern_name='tickets:login', permanent=False), name='phone_login_verify'),
    path('login/phone/resend/', RedirectView.as_view(pattern_name='tickets:login', permanent=False), name='phone_login_resend'),

    # Authenticated attendee path
    path('attendee/dashboard/', views.attendee_dashboard, name='attendee_dashboard'),
    path('my-tickets/', views.my_tickets, name='my_tickets'),
    path('my-tickets/<uuid:order_id>/', views.my_ticket_detail, name='my_ticket_detail'),
    path('account/profile/', views.user_profile, name='user_profile'),
    path('switch-view/', views.switch_view_mode, name='switch_view_mode'),

    # Landing (public) and Dashboard
    path('support/', views.support, name='support'),
    path('privacy/', views.privacy, name='privacy'),
    path('terms/', views.terms, name='terms'),
    path('', views.landing),
    path('dashboard/', views.home, name='home'),
    
    # CSV Upload (price entry and results used when uploading from an event)
    path('upload/price-entry/<uuid:file_id>/', views.price_entry, name='price_entry'),
    path('upload/results/<uuid:file_id>/', views.upload_results, name='upload_results'),
    path('upload/<uuid:file_id>/delete/', views.upload_delete, name='upload_delete'),
    path('upload/<uuid:file_id>/reprocess/', views.reprocess_csv_file, name='reprocess_csv_file'),
    path('upload/<uuid:file_id>/status/', views.upload_status_api, name='upload_status_api'),

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
    # External Survey
    path('analytics/surveys/', views.survey_upload_list, name='survey_upload_list'),
    path('analytics/surveys/upload/', views.survey_upload_create, name='survey_upload_create'),
    path('analytics/surveys/analytics/', views.survey_analytics, name='survey_analytics'),
    path('analytics/surveys/<uuid:upload_id>/', views.survey_upload_detail, name='survey_upload_detail'),
    path('analytics/surveys/<uuid:upload_id>/delete/', views.survey_upload_delete, name='survey_upload_delete'),
    path('analytics/surveys/<uuid:upload_id>/link-events/', views.survey_event_link, name='survey_event_link'),

    # Surveys (public - no login required)
    path('survey/<uuid:token>/', views.survey_form, name='survey_form'),
    path('survey/thank-you/', views.survey_thank_you, name='survey_thank_you'),

    # Events
    path('events/', views.event_list, name='event_list'),
    path('events/calendar/', views.event_calendar, name='event_calendar'),
    path('events/create/', views.event_type_select, name='event_type_select'),
    path('events/create/<str:ticketing_type>/', views.event_create, name='event_create'),
    path('events/<uuid:event_id>/', views.event_detail, name='event_detail'),
    path('events/<uuid:event_id>/summary/stream/', views.event_summary_stream, name='event_summary_stream'),
    path('events/<uuid:event_id>/scanner-pin/generate/', views.generate_scanner_pin, name='generate_scanner_pin'),
    path('events/<uuid:event_id>/scanner-pin/revoke/', views.revoke_scanner_pin, name='revoke_scanner_pin'),
    path('events/<uuid:event_id>/edit/', views.event_edit, name='event_edit'),
    path('events/<uuid:event_id>/flyer/', views.event_flyer_upload, name='event_flyer_upload'),
    path('events/<uuid:event_id>/upload/', views.event_upload_csv, name='event_upload_csv'),
    path('events/<uuid:event_id>/export-csv/', views.event_export_csv, name='event_export_csv'),
    path('events/<uuid:event_id>/delete/', views.event_delete, name='event_delete'),
    path('events/<uuid:event_id>/custom-fields/', views.event_custom_fields, name='event_custom_fields'),
    path('events/<uuid:event_id>/publish/', views.event_publish, name='event_publish'),
    path('events/<uuid:event_id>/end-sales/', views.event_end_sales, name='event_end_sales'),
    path('events/<uuid:event_id>/cancel/', views.event_cancel, name='event_cancel'),
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
    path('orders/<uuid:order_id>/refund/', views.refund_order, name='refund_order'),
    
    # CSV Formats
    path('formats/', views.format_list, name='format_list'),
    path('formats/create/', views.format_create, name='format_create'),
    path('formats/<uuid:format_id>/edit/', views.format_edit, name='format_edit'),
    path('formats/<uuid:format_id>/delete/', views.format_delete, name='format_delete'),
    path('formats/<uuid:format_id>/set-default/', views.format_set_default, name='format_set_default'),
    
    # Settings
    path('settings/google-calendar/', views.settings_google_calendar, name='settings_google_calendar'),
    path('settings/google-calendar/disconnect/', views.settings_google_calendar_disconnect, name='settings_google_calendar_disconnect'),
    path('settings/profile/', views.org_profile, name='org_profile'),
    path('settings/api-keys/', views.settings_api_keys, name='settings_api_keys'),
    path('settings/api-keys/<uuid:key_id>/revoke/', views.settings_api_key_revoke, name='settings_api_key_revoke'),
    path('settings/custom-fields/', views.custom_field_list, name='custom_field_list'),
    path('settings/custom-fields/create/', views.custom_field_create, name='custom_field_create'),
    path('settings/custom-fields/<int:field_id>/edit/', views.custom_field_edit, name='custom_field_edit'),
    path('settings/custom-fields/<int:field_id>/delete/', views.custom_field_delete, name='custom_field_delete'),
    path('settings/custom-fields/reorder/', views.custom_field_reorder, name='custom_field_reorder'),

    # Venues
    path('venues/', views.venue_list, name='venue_list'),
    path('venues/create/', views.venue_create, name='venue_create'),
    path('venues/<uuid:venue_id>/edit/', views.venue_edit, name='venue_edit'),

    # Chat Agent
    path('chat/stream/', views.chat_stream, name='chat_stream'),
    path('chat/history/', views.chat_history, name='chat_history'),
    path('chat/conversations/', views.chat_conversations, name='chat_conversations'),

    # Direct Ticket Selling - Organizer
    path('events/<uuid:event_id>/ticket-types/create/', views.saleable_ticket_type_create, name='saleable_ticket_type_create'),
    path('events/<uuid:event_id>/ticket-types/<uuid:ticket_type_id>/edit/', views.saleable_ticket_type_edit, name='saleable_ticket_type_edit'),
    path('events/<uuid:event_id>/ticket-types/<uuid:ticket_type_id>/data/', views.saleable_ticket_type_data, name='saleable_ticket_type_data'),
    path('events/<uuid:event_id>/ticket-types/<uuid:ticket_type_id>/toggle/', views.saleable_ticket_type_toggle, name='saleable_ticket_type_toggle'),
    path('events/<uuid:event_id>/ticket-types/<uuid:ticket_type_id>/delete/', views.saleable_ticket_type_delete, name='saleable_ticket_type_delete'),

    # Direct Ticket Selling - Public (no auth) - short alphanumeric public_id
    re_path(r'^e/(?P<public_id>[A-Za-z0-9]{10})/$', views.public_event_buy, name='public_event_buy'),
    re_path(r'^e/(?P<public_id>[A-Za-z0-9]{10})/unlock/(?P<ticket_type_id>[0-9a-f-]{36})/$', views.unlock_ticket_type, name='unlock_ticket_type'),
    re_path(r'^e/(?P<public_id>[A-Za-z0-9]{10})/waitlist/(?P<ticket_type_id>[0-9a-f-]{36})/$', views.join_waitlist, name='join_waitlist'),
    re_path(r'^e/(?P<public_id>[A-Za-z0-9]{10})/waitlist/activate/(?P<hold_token>[0-9a-f-]{36})/$', views.activate_waitlist_hold, name='activate_waitlist_hold'),
    re_path(r'^e/(?P<public_id>[A-Za-z0-9]{10})/checkout/$', views.checkout_payment, name='checkout_payment'),
    re_path(r'^e/(?P<public_id>[A-Za-z0-9]{10})/payment-intent/$', views.create_payment_intent, name='create_payment_intent'),
    re_path(r'^e/(?P<public_id>[A-Za-z0-9]{10})/apply-promo/$', views.validate_promo_code, name='validate_promo_code'),
    # Redirect old /buy/<uuid>/ links to new short /e/<public_id>/
    path('buy/<uuid:event_id>/', views.buy_redirect, name='buy_redirect'),
    path('checkout/success/', views.checkout_success, name='checkout_success'),

    # Promo Codes - Organizer
    path('events/<uuid:event_id>/promo-codes/create/', views.promo_code_create, name='promo_code_create'),
    path('events/<uuid:event_id>/promo-codes/<uuid:promo_code_id>/delete/', views.promo_code_delete, name='promo_code_delete'),

    # Tracking Links
    path('track/<str:token>/', views.track_link_redirect, name='track_link_redirect'),
    path('events/<uuid:event_id>/tracking-links/create/', views.tracking_link_create, name='tracking_link_create'),
    path('events/<uuid:event_id>/tracking-links/<uuid:link_id>/delete/', views.tracking_link_delete, name='tracking_link_delete'),

    # Stripe Webhooks
    path('webhooks/stripe/', views.stripe_webhook, name='stripe_webhook'),
    path('webhooks/stripe/connect/', views.stripe_connect_webhook, name='stripe_connect_webhook'),

    # Finance / Stripe Connect
    path('finance/', views.finance_overview, name='finance_overview'),
    path('finance/stripe/onboard/', views.stripe_connect_onboard, name='stripe_connect_onboard'),
    path('finance/stripe/return/', views.stripe_connect_return, name='stripe_connect_return'),
    path('finance/stripe/refresh/', views.stripe_connect_refresh, name='stripe_connect_refresh'),
    path('finance/stripe/manage/', views.stripe_account_login, name='stripe_account_login'),
    path('finance/stripe/disconnect/', views.stripe_disconnect, name='stripe_disconnect'),
    path('finance/payout/', views.initiate_payout, name='initiate_payout'),
]
