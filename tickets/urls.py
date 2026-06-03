from django.urls import path, re_path
from django.views.generic import RedirectView
from . import views
from . import oauth_views
from . import sms_views

app_name = 'tickets'

urlpatterns = [
    # OAuth 2.0 endpoints (for MCP / Claude Desktop Connectors)
    path('oauth/authorize/', oauth_views.oauth_authorize, name='oauth_authorize'),
    path('oauth/token/', oauth_views.oauth_token, name='oauth_token'),
    path('oauth/clients/register/', oauth_views.oauth_register_client, name='oauth_register_client'),
    path('oauth/revoke/', oauth_views.oauth_revoke, name='oauth_revoke'),

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
    # org/switch/ must come before org/<slug:slug>/ to avoid slug matching 'switch'
    path('org/switch/', views.org_switch, name='org_switch'),
    path('org/<slug:slug>/', views.public_org_profile, name='public_org_profile'),

    # Organization (no-org flow)
    path('org-required/', views.org_required, name='org_required'),
    path('create-organization/', views.create_organization, name='create_organization'),
    path('members/', views.member_list, name='member_list'),
    path('members/invite/', views.member_invite, name='member_invite'),
    path('members/revoke/<uuid:token>/', views.invite_revoke, name='invite_revoke'),
    path('members/<uuid:membership_id>/role/', views.member_role_update, name='member_role_update'),
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
    path('sms-consent/', views.sms_consent, name='sms_consent'),
    path('', views.landing),
    path('dashboard/', views.home, name='home'),
    path('actions/', views.action_center, name='action_center'),
    path('actions/<uuid:recommendation_id>/review/', views.ai_recommendation_review, name='ai_recommendation_review'),
    path('actions/<uuid:recommendation_id>/dismiss/', views.ai_recommendation_dismiss, name='ai_recommendation_dismiss'),
    path('actions/<uuid:recommendation_id>/resolve/', views.ai_recommendation_resolve, name='ai_recommendation_resolve'),
    path('actions/<uuid:recommendation_id>/unconfirmed-matches/', views.ai_recommendation_unconfirmed_matches, name='ai_recommendation_unconfirmed_matches'),

    # CSV Upload (price entry and results used when uploading from an event)
    path('upload/price-entry/<uuid:file_id>/', views.price_entry, name='price_entry'),
    path('upload/results/<uuid:file_id>/', views.upload_results, name='upload_results'),
    path('upload/<uuid:file_id>/delete/', views.upload_delete, name='upload_delete'),
    path('upload/<uuid:file_id>/reprocess/', views.reprocess_csv_file, name='reprocess_csv_file'),
    path('upload/<uuid:file_id>/status/', views.upload_status_api, name='upload_status_api'),

    # Customers
    path('customers/', views.customer_list, name='customer_list'),
    path('customers/ltv-by-market/', views.customer_ltv_by_market, name='customer_ltv_by_market'),
    path('customers/tags/', views.customer_tag_list, name='customer_tag_list'),
    path('customers/tags/create/', views.customer_tag_create, name='customer_tag_create'),
    path('customers/tags/<uuid:tag_id>/delete/', views.customer_tag_delete, name='customer_tag_delete'),
    path('customers/<uuid:customer_id>/tags/add/', views.customer_tag_add, name='customer_tag_add'),
    path('customers/<uuid:customer_id>/tags/<uuid:tag_id>/remove/', views.customer_tag_remove, name='customer_tag_remove'),
    path('customers/<uuid:customer_id>/', views.customer_detail, name='customer_detail'),
    # Analytics - Segments
    path('analytics/', views.analytics_overview, name='analytics_overview'),
    path('analytics/segments/', views.customer_segments, name='customer_segments'),
    path('analytics/segments/recalculate/', views.recalculate_segments, name='recalculate_segments'),
    path('analytics/churn/', views.churn_overview, name='churn_overview'),
    path('analytics/churn/bulk-tag/', views.churn_bulk_tag, name='churn_bulk_tag'),
    path('analytics/repeat-customers/', views.repeat_customers, name='repeat_customers'),
    path('analytics/cohort-retention/', views.cohort_retention, name='cohort_retention'),
    path('analytics/profitability/', views.profitability_overview, name='profitability_overview'),
    path('analytics/expenses/', views.expense_analytics, name='expense_analytics'),
    # External Survey
    path('analytics/surveys/', views.survey_upload_list, name='survey_upload_list'),
    path('analytics/surveys/upload/', views.survey_upload_create, name='survey_upload_create'),
    path('analytics/surveys/analytics/', views.survey_analytics, name='survey_analytics'),
    path('analytics/surveys/<uuid:upload_id>/', views.survey_upload_detail, name='survey_upload_detail'),
    path('analytics/surveys/<uuid:upload_id>/delete/', views.survey_upload_delete, name='survey_upload_delete'),
    path('analytics/surveys/<uuid:upload_id>/link-events/', views.survey_event_link, name='survey_event_link'),

    # Typeform integration (organizer settings)
    path('settings/typeform/', views.typeform_settings, name='typeform_settings'),
    path('settings/typeform/connect/', views.typeform_connect, name='typeform_connect'),
    path('settings/typeform/disconnect/', views.typeform_disconnect, name='typeform_disconnect'),
    path('settings/typeform/forms/', views.typeform_form_picker, name='typeform_form_picker'),
    path('settings/typeform/forms/<uuid:sub_id>/mapping/', views.typeform_form_mapping, name='typeform_form_mapping'),
    path('settings/typeform/forms/<uuid:sub_id>/sync/', views.typeform_form_sync, name='typeform_form_sync'),
    path('settings/typeform/forms/<uuid:sub_id>/delete/', views.typeform_form_unsubscribe, name='typeform_form_unsubscribe'),
    path('webhooks/typeform/<uuid:sub_id>/', views.typeform_webhook, name='typeform_webhook'),

    # Surveys (public - no login required)
    path('survey/<uuid:token>/', views.survey_form, name='survey_form'),
    path('survey/thank-you/', views.survey_thank_you, name='survey_thank_you'),

    # Events
    path('events/', views.event_list, name='event_list'),
    path('events/calendar/', views.event_calendar, name='event_calendar'),
    path('events/create/', views.event_type_select, name='event_type_select'),
    path('events/create/<str:ticketing_type>/', views.event_create, name='event_create'),
    path('events/<uuid:event_id>/', views.event_detail, name='event_detail'),
    path('events/<uuid:event_id>/weather/hourly/', views.event_weather_hourly, name='event_weather_hourly'),
    path('events/<uuid:event_id>/surveys/match/',  views.event_survey_match,  name='event_survey_match'),
    path('events/<uuid:event_id>/surveys/apply/',  views.event_survey_apply,  name='event_survey_apply'),
    path('events/<uuid:event_id>/surveys/unlink/', views.event_survey_unlink, name='event_survey_unlink'),
    path('events/<uuid:event_id>/uploads-summary/', views.event_uploads_summary, name='event_uploads_summary'),
    path('events/<uuid:event_id>/scanner-pin/generate/', views.generate_scanner_pin, name='generate_scanner_pin'),
    path('events/<uuid:event_id>/scanner-pin/revoke/', views.revoke_scanner_pin, name='revoke_scanner_pin'),
    path('events/<uuid:event_id>/edit/', views.event_edit, name='event_edit'),
    path('events/<uuid:event_id>/pricing-recommendation/', views.event_pricing_recommendation, name='event_pricing_recommendation'),
    path('events/<uuid:event_id>/flyer/', views.event_flyer_upload, name='event_flyer_upload'),
    path('events/<uuid:event_id>/upload/', views.event_upload_csv, name='event_upload_csv'),
    path('events/<uuid:event_id>/export-csv/', views.event_export_csv, name='event_export_csv'),
    path('events/<uuid:event_id>/delete/', views.event_delete, name='event_delete'),
    path('events/<uuid:event_id>/custom-fields/', views.event_custom_fields, name='event_custom_fields'),
    path('events/<uuid:event_id>/publish/', views.event_publish, name='event_publish'),
    path('events/<uuid:event_id>/end-sales/', views.event_end_sales, name='event_end_sales'),
    path('events/<uuid:event_id>/cancel/', views.event_cancel, name='event_cancel'),
    path('events/<uuid:event_id>/send-survey/', views.send_survey, name='send_survey'),
    path('events/<uuid:event_id>/meta-ads/match/', views.event_meta_ads_match, name='event_meta_ads_match'),
    path('events/<uuid:event_id>/meta-ads/apply/', views.event_meta_ads_apply, name='event_meta_ads_apply'),
    path('events/<uuid:event_id>/meta-ads/<uuid:expense_id>/refresh/', views.event_meta_ads_refresh, name='event_meta_ads_refresh'),
    path('events/<uuid:event_id>/meta-ads/<uuid:expense_id>/remove/', views.event_meta_ads_remove, name='event_meta_ads_remove'),
    path('events/<uuid:event_id>/meta-ads/<uuid:expense_id>/metrics/', views.event_meta_ads_metrics_edit, name='event_meta_ads_metrics_edit'),
    path('events/<uuid:event_id>/meta-ads/<uuid:expense_id>/confirm/', views.event_meta_ads_confirm, name='event_meta_ads_confirm'),
    path('events/<uuid:event_id>/meta-ads/<uuid:expense_id>/unconfirm/', views.event_meta_ads_unconfirm, name='event_meta_ads_unconfirm'),
    path('events/<uuid:event_id>/mailchimp/confirm-all/', views.event_mailchimp_confirm_all, name='event_mailchimp_confirm_all'),
    path('events/<uuid:event_id>/slicktext/confirm-all/', views.event_slicktext_confirm_all, name='event_slicktext_confirm_all'),
    path('events/<uuid:event_id>/meta-ads/confirm-all/', views.event_meta_ads_confirm_all, name='event_meta_ads_confirm_all'),
    path('events/<uuid:event_id>/mailchimp/match/', views.event_mailchimp_match, name='event_mailchimp_match'),
    path('events/<uuid:event_id>/mailchimp/apply/', views.event_mailchimp_apply, name='event_mailchimp_apply'),
    path('events/<uuid:event_id>/mailchimp/refresh/', views.event_mailchimp_refresh_all, name='event_mailchimp_refresh_all'),
    path('events/<uuid:event_id>/mailchimp/<uuid:email_campaign_id>/refresh/', views.event_mailchimp_refresh, name='event_mailchimp_refresh'),
    path('events/<uuid:event_id>/mailchimp/<uuid:email_campaign_id>/remove/', views.event_mailchimp_remove, name='event_mailchimp_remove'),
    path('events/<uuid:event_id>/mailchimp/<uuid:email_campaign_id>/metrics/', views.event_mailchimp_metrics_edit, name='event_mailchimp_metrics_edit'),
    path('events/<uuid:event_id>/mailchimp/<uuid:email_campaign_id>/confirm/', views.event_mailchimp_confirm, name='event_mailchimp_confirm'),
    path('events/<uuid:event_id>/mailchimp/<uuid:email_campaign_id>/unconfirm/', views.event_mailchimp_unconfirm, name='event_mailchimp_unconfirm'),
    path('events/<uuid:event_id>/slicktext/match/', views.event_slicktext_match, name='event_slicktext_match'),
    path('events/<uuid:event_id>/slicktext/apply/', views.event_slicktext_apply, name='event_slicktext_apply'),
    path('events/<uuid:event_id>/slicktext/refresh/', views.event_slicktext_refresh_all, name='event_slicktext_refresh_all'),
    path('events/<uuid:event_id>/slicktext/<uuid:sms_campaign_id>/refresh/', views.event_slicktext_refresh, name='event_slicktext_refresh'),
    path('events/<uuid:event_id>/slicktext/<uuid:sms_campaign_id>/remove/', views.event_slicktext_remove, name='event_slicktext_remove'),
    path('events/<uuid:event_id>/slicktext/<uuid:sms_campaign_id>/metrics/', views.event_slicktext_metrics_edit, name='event_slicktext_metrics_edit'),
    path('events/<uuid:event_id>/slicktext/<uuid:sms_campaign_id>/confirm/', views.event_slicktext_confirm, name='event_slicktext_confirm'),
    path('events/<uuid:event_id>/slicktext/<uuid:sms_campaign_id>/unconfirm/', views.event_slicktext_unconfirm, name='event_slicktext_unconfirm'),
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

    # Marketing
    path('marketing/', views.marketing_overview, name='marketing_overview'),
    path('marketing/analyze/', views.marketing_ai_analyze, name='marketing_ai_analyze'),

    # Marketing SMS — campaigns
    path('marketing/sms/', sms_views.sms_campaign_list, name='sms_campaign_list'),
    path('marketing/sms/new/', sms_views.sms_campaign_create, name='sms_campaign_create'),
    path('marketing/sms/<uuid:pk>/', sms_views.sms_campaign_detail, name='sms_campaign_detail'),
    path('marketing/sms/<uuid:pk>/cancel/', sms_views.sms_campaign_cancel, name='sms_campaign_cancel'),
    # Marketing SMS — prepaid credit wallet
    path('marketing/sms/credits/', sms_views.sms_credits, name='sms_credits'),
    path('marketing/sms/credits/checkout/', sms_views.sms_credits_checkout, name='sms_credits_checkout'),
    path('marketing/sms/credits/success/', sms_views.sms_credits_success, name='sms_credits_success'),
    # Marketing SMS — recipient lists
    path('marketing/sms/lists/', sms_views.sms_recipient_list_list, name='sms_recipient_list_list'),
    path('marketing/sms/lists/new/', sms_views.sms_recipient_list_create, name='sms_recipient_list_create'),
    path('marketing/sms/lists/preview/', sms_views.sms_recipient_list_preview, name='sms_recipient_list_preview'),
    path('marketing/sms/lists/<uuid:pk>/', sms_views.sms_recipient_list_detail, name='sms_recipient_list_detail'),

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
    path('settings/', views.settings_overview, name='settings_overview'),
    path('settings/google-calendar/disconnect/', views.settings_google_calendar_disconnect, name='settings_google_calendar_disconnect'),
    path('settings/meta-ads/', views.meta_ads_settings, name='meta_ads_settings'),
    path('settings/meta-ads/connect/', views.meta_ads_connect, name='meta_ads_connect'),
    path('settings/meta-ads/callback/', views.meta_ads_callback, name='meta_ads_callback'),
    path('settings/meta-ads/select-account/', views.meta_ads_select_account, name='meta_ads_select_account'),
    path('settings/meta-ads/disconnect/', views.meta_ads_disconnect, name='meta_ads_disconnect'),
    path('settings/mailchimp/', views.mailchimp_settings, name='mailchimp_settings'),
    path('settings/mailchimp/connect/', views.mailchimp_connect, name='mailchimp_connect'),
    path('settings/mailchimp/callback/', views.mailchimp_callback, name='mailchimp_callback'),
    path('settings/mailchimp/disconnect/', views.mailchimp_disconnect, name='mailchimp_disconnect'),
    path('settings/mailchimp/hints/', views.mailchimp_save_hints, name='mailchimp_save_hints'),
    path('settings/slicktext/', views.slicktext_settings, name='slicktext_settings'),
    path('settings/slicktext/save/', views.slicktext_save, name='slicktext_save'),
    path('settings/slicktext/disconnect/', views.slicktext_disconnect, name='slicktext_disconnect'),
    path('settings/profile/', views.org_profile, name='org_profile'),
    path('settings/api-keys/', views.settings_api_keys, name='settings_api_keys'),
    path('settings/ai-token-usage/', views.ai_token_usage_dashboard, name='ai_token_usage'),
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
    path('events/<uuid:event_id>/ticket-types/reorder/', views.saleable_ticket_type_reorder, name='saleable_ticket_type_reorder'),
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
    re_path(r'^e/(?P<public_id>[A-Za-z0-9]{10})/test-fulfill/$', views.e2e_complete_payment, name='e2e_complete_payment'),
    re_path(r'^e/(?P<public_id>[A-Za-z0-9]{10})/apply-promo/$', views.validate_promo_code, name='validate_promo_code'),
    # Redirect old /buy/<uuid>/ links to new short /e/<public_id>/
    path('buy/<uuid:event_id>/', views.buy_redirect, name='buy_redirect'),
    path('checkout/success/', views.checkout_success, name='checkout_success'),
    path('checkout/session-status/<uuid:session_id>/', views.checkout_session_status, name='checkout_session_status'),

    # Promo Codes - Organizer
    path('events/<uuid:event_id>/promo-codes/create/', views.promo_code_create, name='promo_code_create'),
    path('events/<uuid:event_id>/promo-codes/<uuid:promo_code_id>/delete/', views.promo_code_delete, name='promo_code_delete'),

    # Tracking Links
    path('track/<str:token>/', views.track_link_redirect, name='track_link_redirect'),
    path('events/<uuid:event_id>/tracking-links/create/', views.tracking_link_create, name='tracking_link_create'),
    path('events/<uuid:event_id>/tracking-links/<uuid:link_id>/delete/', views.tracking_link_delete, name='tracking_link_delete'),

    # Twilio marketing-SMS webhooks
    path('webhooks/twilio/sms-status/', sms_views.twilio_sms_status_webhook, name='twilio_sms_status_webhook'),
    path('webhooks/twilio/sms-inbound/', sms_views.twilio_sms_inbound_webhook, name='twilio_sms_inbound_webhook'),
    # Short public redirect for tracked marketing-SMS links (kept short for SMS).
    path('c/<str:token>/', sms_views.sms_click_redirect, name='sms_click_redirect'),

    # Stripe Webhooks
    path('webhooks/stripe/', views.stripe_webhook, name='stripe_webhook'),
    path('webhooks/stripe/connect/', views.stripe_connect_webhook, name='stripe_connect_webhook'),

    # Mobile Stripe Connect deep-link bridges. Stripe's AccountLink validator
    # rejects custom URI schemes (cueup://), so we hand it HTTPS URLs and 302
    # to the cueup:// deep link from these views.
    path('m/stripe-connect-return/', views.mobile_stripe_connect_return, name='mobile_stripe_connect_return'),
    path('m/stripe-connect-refresh/', views.mobile_stripe_connect_refresh, name='mobile_stripe_connect_refresh'),

    # Finance / Stripe Connect
    path('finance/', views.finance_overview, name='finance_overview'),
    path('finance/stripe/onboard/', views.stripe_connect_onboard, name='stripe_connect_onboard'),
    path('finance/stripe/return/', views.stripe_connect_return, name='stripe_connect_return'),
    path('finance/stripe/refresh/', views.stripe_connect_refresh, name='stripe_connect_refresh'),
    path('finance/stripe/manage/', views.stripe_account_login, name='stripe_account_login'),
    path('finance/stripe/disconnect/', views.stripe_disconnect, name='stripe_disconnect'),
    path('finance/stripe/tap-to-pay/enable/', views.enable_tap_to_pay, name='enable_tap_to_pay'),
    path('finance/payout/', views.initiate_payout, name='initiate_payout'),
    path('finance/payout/recover/', views.recover_pending_payouts, name='recover_pending_payouts'),
]
