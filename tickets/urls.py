from django.urls import path, re_path
from django.views.generic import RedirectView
from . import views
from . import oauth_views
from . import sms_views
from .integrations import (
    mailchimp as mailchimp_views,
    slicktext as slicktext_views,
    meta_ads as meta_ads_views,
    typeform as typeform_views,
    google_calendar as google_calendar_views,
    hub as integrations_hub,
)

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
    # Public "subscribe to an organizer" (accountless Customer + SMS consent)
    path('subscribe/<slug:org_slug>/', views.subscribe_view, name='subscribe'),
    path('subscribe/<slug:org_slug>/verify/', views.subscribe_verify_view, name='subscribe_verify'),
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
    path('onboarding/dismiss/', views.dismiss_onboarding, name='dismiss_onboarding'),
    path('onboarding/dismiss-upsell/', views.dismiss_directticketing_upsell, name='dismiss_directticketing_upsell'),
    path('actions/', views.action_center, name='action_center'),
    path('actions/<uuid:recommendation_id>/review/', views.ai_recommendation_review, name='ai_recommendation_review'),
    path('actions/<uuid:recommendation_id>/dismiss/', views.ai_recommendation_dismiss, name='ai_recommendation_dismiss'),
    path('actions/<uuid:recommendation_id>/resolve/', views.ai_recommendation_resolve, name='ai_recommendation_resolve'),
    path('actions/<uuid:recommendation_id>/unconfirmed-matches/', views.ai_recommendation_unconfirmed_matches, name='ai_recommendation_unconfirmed_matches'),

    # CSV Upload (price entry and results used when uploading from an event)
    path('sample-import.csv', views.sample_import_csv, name='sample_import_csv'),
    path('upload/price-entry/<uuid:file_id>/', views.price_entry, name='price_entry'),
    path('upload/results/<uuid:file_id>/', views.upload_results, name='upload_results'),
    path('upload/<uuid:file_id>/delete/', views.upload_delete, name='upload_delete'),
    path('upload/<uuid:file_id>/reprocess/', views.reprocess_csv_file, name='reprocess_csv_file'),
    path('upload/<uuid:file_id>/download/', views.download_csv_file, name='download_csv_file'),
    path('upload/<uuid:file_id>/status/', views.upload_status_api, name='upload_status_api'),

    # Customers
    path('customers/', views.customer_list, name='customer_list'),
    path('customers/bulk-tag/', sms_views.customers_bulk_tag, name='customers_bulk_tag'),
    path('customers/bulk-sms-status/', sms_views.customers_bulk_sms_status, name='customers_bulk_sms_status'),
    path('customers/bulk-sms-compose/', sms_views.customers_bulk_sms_compose, name='customers_bulk_sms_compose'),
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
    path('analytics/market-trends/', views.market_trends, name='market_trends'),
    path('analytics/profitability/', views.profitability_overview, name='profitability_overview'),
    path('analytics/expenses/', views.expense_analytics, name='expense_analytics'),
    # Loyalty programs
    path('loyalty/', views.loyalty_program_list, name='loyalty_program_list'),
    path('loyalty/create/', views.loyalty_program_create, name='loyalty_program_create'),
    path('loyalty/<uuid:program_id>/', views.loyalty_program_detail, name='loyalty_program_detail'),
    path('loyalty/<uuid:program_id>/edit/', views.loyalty_program_edit, name='loyalty_program_edit'),
    path('loyalty/<uuid:program_id>/recalculate/', views.loyalty_recalculate, name='loyalty_recalculate'),
    path('loyalty/<uuid:program_id>/recompute/', views.loyalty_recompute_points, name='loyalty_recompute_points'),
    path('loyalty/<uuid:program_id>/delete/', views.loyalty_program_delete, name='loyalty_program_delete'),
    path('loyalty/<uuid:program_id>/tiers/<uuid:tier_id>/members/', views.loyalty_tier_members, name='loyalty_tier_members'),
    # External Survey
    path('analytics/surveys/', views.survey_upload_list, name='survey_upload_list'),
    path('analytics/surveys/upload/', views.survey_upload_create, name='survey_upload_create'),
    path('analytics/surveys/analytics/', views.survey_analytics, name='survey_analytics'),
    path('analytics/surveys/<uuid:upload_id>/', views.survey_upload_detail, name='survey_upload_detail'),
    path('analytics/surveys/<uuid:upload_id>/delete/', views.survey_upload_delete, name='survey_upload_delete'),
    path('analytics/surveys/<uuid:upload_id>/link-events/', views.survey_event_link, name='survey_event_link'),

    # Typeform integration (organizer settings) — see the Integrations block for the
    # full set of integration connection pages. The webhook path stays unchanged because
    # it is registered with Typeform per active subscription.
    path('settings/integrations/typeform/', typeform_views.typeform_settings, name='typeform_settings'),
    path('settings/integrations/typeform/connect/', typeform_views.typeform_connect, name='typeform_connect'),
    path('settings/integrations/typeform/disconnect/', typeform_views.typeform_disconnect, name='typeform_disconnect'),
    path('settings/integrations/typeform/forms/', typeform_views.typeform_form_picker, name='typeform_form_picker'),
    path('settings/integrations/typeform/forms/<uuid:sub_id>/mapping/', typeform_views.typeform_form_mapping, name='typeform_form_mapping'),
    path('settings/integrations/typeform/forms/<uuid:sub_id>/sync/', typeform_views.typeform_form_sync, name='typeform_form_sync'),
    path('settings/integrations/typeform/forms/<uuid:sub_id>/delete/', typeform_views.typeform_form_unsubscribe, name='typeform_form_unsubscribe'),
    path('webhooks/typeform/<uuid:sub_id>/', typeform_views.typeform_webhook, name='typeform_webhook'),

    # Surveys (public - no login required)
    path('survey/<uuid:token>/', views.survey_form, name='survey_form'),
    path('survey/thank-you/', views.survey_thank_you, name='survey_thank_you'),

    # Survey builder — hub + org-default template (singular /survey/ to avoid
    # the plural /events/<id>/surveys/ external-survey endpoints). Questions are
    # configured inline on the builder page via these JSON endpoints.
    path('surveys/', views.survey_hub, name='survey_hub'),
    path('surveys/builder/', views.survey_builder, name='survey_builder'),
    path('surveys/builder/preview/', views.survey_preview, name='survey_preview'),
    path('surveys/builder/save/', views.survey_question_save, name='survey_question_save'),
    path('surveys/builder/<uuid:question_id>/save/', views.survey_question_save, name='survey_question_save'),
    path('surveys/builder/<uuid:question_id>/delete/', views.survey_question_delete, name='survey_question_delete'),
    path('surveys/builder/reorder/', views.survey_reorder, name='survey_reorder'),
    path('surveys/builder/email-subject/', views.survey_email_subject_save, name='survey_email_subject_save'),
    path('surveys/builder/reply-to/', views.survey_reply_to_save, name='survey_reply_to_save'),
    path('surveys/builder/send-schedule/', views.survey_schedule_save, name='survey_schedule_save'),
    path('surveys/builder/send-test/', views.survey_send_test_email, name='survey_send_test_email'),
    # Survey builder — per-event (same views, event scope)
    path('events/<uuid:event_id>/survey/builder/', views.survey_builder, name='event_survey_builder'),
    path('events/<uuid:event_id>/survey/builder/preview/', views.survey_preview, name='event_survey_preview'),
    path('events/<uuid:event_id>/survey/customize/', views.event_survey_customize, name='event_survey_customize'),
    path('events/<uuid:event_id>/survey/reset/', views.event_survey_reset, name='event_survey_reset'),
    path('events/<uuid:event_id>/survey/save/', views.survey_question_save, name='event_survey_question_save'),
    path('events/<uuid:event_id>/survey/<uuid:question_id>/save/', views.survey_question_save, name='event_survey_question_save'),
    path('events/<uuid:event_id>/survey/<uuid:question_id>/delete/', views.survey_question_delete, name='event_survey_question_delete'),
    path('events/<uuid:event_id>/survey/reorder/', views.survey_reorder, name='event_survey_reorder'),
    path('events/<uuid:event_id>/survey/email-subject/', views.survey_email_subject_save, name='event_survey_email_subject_save'),
    path('events/<uuid:event_id>/survey/send-schedule/', views.survey_schedule_save, name='event_survey_schedule_save'),
    path('events/<uuid:event_id>/survey/send-test/', views.survey_send_test_email, name='event_survey_send_test_email'),

    # Events
    path('events/', views.event_list, name='event_list'),
    path('events/calendar/', views.event_calendar, name='event_calendar'),
    path('events/create/', views.event_type_select, name='event_type_select'),
    path('events/create/<str:ticketing_type>/', views.event_create, name='event_create'),
    path('events/<uuid:event_id>/', views.event_detail, name='event_detail'),
    path('events/<uuid:event_id>/summary/stream/', views.event_summary_stream, name='event_summary_stream'),
    path('events/<uuid:event_id>/weather/hourly/', views.event_weather_hourly, name='event_weather_hourly'),
    path('events/<uuid:event_id>/surveys/match/',  views.event_survey_match,  name='event_survey_match'),
    path('events/<uuid:event_id>/surveys/apply/',  views.event_survey_apply,  name='event_survey_apply'),
    path('events/<uuid:event_id>/surveys/unlink/', views.event_survey_unlink, name='event_survey_unlink'),
    path('events/<uuid:event_id>/surveys/response/<str:kind>/<uuid:response_id>/', views.event_survey_response_detail, name='event_survey_response_detail'),
    path('events/<uuid:event_id>/bulk-tag/', sms_views.event_bulk_tag, name='event_bulk_tag'),
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
    path('events/<uuid:event_id>/cancel-scheduled-survey/', views.cancel_scheduled_survey, name='cancel_scheduled_survey'),
    path('events/<uuid:event_id>/survey-recipient-count/', views.survey_recipient_count, name='survey_recipient_count'),
    path('events/<uuid:event_id>/survey-schedule-preview/', views.survey_schedule_preview, name='survey_schedule_preview'),
    # Integrations — per-event campaign actions (paths stay under the event; handlers
    # live in tickets/integrations/). See the "Integrations" block lower down for the
    # connection/settings pages.
    path('events/<uuid:event_id>/meta-ads/match/', meta_ads_views.event_meta_ads_match, name='event_meta_ads_match'),
    path('events/<uuid:event_id>/meta-ads/apply/', meta_ads_views.event_meta_ads_apply, name='event_meta_ads_apply'),
    path('events/<uuid:event_id>/meta-ads/<uuid:expense_id>/refresh/', meta_ads_views.event_meta_ads_refresh, name='event_meta_ads_refresh'),
    path('events/<uuid:event_id>/meta-ads/<uuid:expense_id>/remove/', meta_ads_views.event_meta_ads_remove, name='event_meta_ads_remove'),
    path('events/<uuid:event_id>/meta-ads/<uuid:expense_id>/metrics/', meta_ads_views.event_meta_ads_metrics_edit, name='event_meta_ads_metrics_edit'),
    path('events/<uuid:event_id>/meta-ads/<uuid:expense_id>/confirm/', meta_ads_views.event_meta_ads_confirm, name='event_meta_ads_confirm'),
    path('events/<uuid:event_id>/meta-ads/<uuid:expense_id>/unconfirm/', meta_ads_views.event_meta_ads_unconfirm, name='event_meta_ads_unconfirm'),
    path('events/<uuid:event_id>/mailchimp/confirm-all/', mailchimp_views.event_mailchimp_confirm_all, name='event_mailchimp_confirm_all'),
    path('events/<uuid:event_id>/slicktext/confirm-all/', slicktext_views.event_slicktext_confirm_all, name='event_slicktext_confirm_all'),
    path('events/<uuid:event_id>/meta-ads/confirm-all/', meta_ads_views.event_meta_ads_confirm_all, name='event_meta_ads_confirm_all'),
    path('events/<uuid:event_id>/mailchimp/match/', mailchimp_views.event_mailchimp_match, name='event_mailchimp_match'),
    path('events/<uuid:event_id>/mailchimp/apply/', mailchimp_views.event_mailchimp_apply, name='event_mailchimp_apply'),
    path('events/<uuid:event_id>/mailchimp/refresh/', mailchimp_views.event_mailchimp_refresh_all, name='event_mailchimp_refresh_all'),
    path('events/<uuid:event_id>/mailchimp/<uuid:email_campaign_id>/refresh/', mailchimp_views.event_mailchimp_refresh, name='event_mailchimp_refresh'),
    path('events/<uuid:event_id>/mailchimp/<uuid:email_campaign_id>/remove/', mailchimp_views.event_mailchimp_remove, name='event_mailchimp_remove'),
    path('events/<uuid:event_id>/mailchimp/<uuid:email_campaign_id>/metrics/', mailchimp_views.event_mailchimp_metrics_edit, name='event_mailchimp_metrics_edit'),
    path('events/<uuid:event_id>/mailchimp/<uuid:email_campaign_id>/confirm/', mailchimp_views.event_mailchimp_confirm, name='event_mailchimp_confirm'),
    path('events/<uuid:event_id>/mailchimp/<uuid:email_campaign_id>/unconfirm/', mailchimp_views.event_mailchimp_unconfirm, name='event_mailchimp_unconfirm'),
    path('events/<uuid:event_id>/slicktext/match/', slicktext_views.event_slicktext_match, name='event_slicktext_match'),
    path('events/<uuid:event_id>/slicktext/apply/', slicktext_views.event_slicktext_apply, name='event_slicktext_apply'),
    path('events/<uuid:event_id>/slicktext/refresh/', slicktext_views.event_slicktext_refresh_all, name='event_slicktext_refresh_all'),
    path('events/<uuid:event_id>/slicktext/<uuid:sms_campaign_id>/refresh/', slicktext_views.event_slicktext_refresh, name='event_slicktext_refresh'),
    path('events/<uuid:event_id>/slicktext/<uuid:sms_campaign_id>/remove/', slicktext_views.event_slicktext_remove, name='event_slicktext_remove'),
    path('events/<uuid:event_id>/slicktext/<uuid:sms_campaign_id>/metrics/', slicktext_views.event_slicktext_metrics_edit, name='event_slicktext_metrics_edit'),
    path('events/<uuid:event_id>/slicktext/<uuid:sms_campaign_id>/confirm/', slicktext_views.event_slicktext_confirm, name='event_slicktext_confirm'),
    path('events/<uuid:event_id>/slicktext/<uuid:sms_campaign_id>/unconfirm/', slicktext_views.event_slicktext_unconfirm, name='event_slicktext_unconfirm'),
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
    path('marketing/sms/<uuid:pk>/link-event/', sms_views.sms_campaign_link_event, name='sms_campaign_link_event'),
    # Marketing SMS — prepaid credit wallet
    path('marketing/sms/credits/', sms_views.sms_credits, name='sms_credits'),
    path('marketing/sms/credits/checkout/', sms_views.sms_credits_checkout, name='sms_credits_checkout'),
    path('marketing/sms/credits/success/', sms_views.sms_credits_success, name='sms_credits_success'),
    path('marketing/sms/credits/charge-saved/', sms_views.sms_credits_charge_saved, name='sms_credits_charge_saved'),
    path('marketing/sms/credits/remove-card/', sms_views.sms_credits_remove_card, name='sms_credits_remove_card'),
    # Marketing SMS — audience + ticket-link helpers
    path('marketing/sms/audience-preview/', sms_views.sms_audience_preview, name='sms_audience_preview'),
    path('marketing/sms/ticket-link/', sms_views.sms_ticket_link, name='sms_ticket_link'),

    path('marketing/sms/plans/', sms_views.sms_plan_list, name='sms_plan_list'),
    path('marketing/sms/plan/new/', sms_views.sms_plan_create, name='sms_plan_create'),
    path('marketing/sms/plan/<uuid:pk>/', sms_views.sms_plan_detail, name='sms_plan_detail'),
    path('marketing/sms/plan/<uuid:pk>/delete/', sms_views.sms_plan_delete, name='sms_plan_delete'),
    path('marketing/sms/plan/<uuid:pk>/step/<int:step>/update/', sms_views.sms_plan_update_step, name='sms_plan_update_step'),
    path('marketing/sms/plan/<uuid:pk>/step/<int:step>/schedule/', sms_views.sms_plan_update_schedule, name='sms_plan_update_schedule'),
    path('marketing/sms/plan/<uuid:pk>/step/<int:step>/audience/', sms_views.sms_plan_update_audience, name='sms_plan_update_audience'),
    path('marketing/sms/plan/<uuid:pk>/step/<int:step>/launch/', sms_views.sms_plan_launch_step, name='sms_plan_launch_step'),
    path('marketing/sms/plan/<uuid:pk>/step/<int:step>/preview/', sms_views.sms_plan_preview_step, name='sms_plan_preview_step'),
    path('marketing/sms/plan/<uuid:pk>/step/<int:step>/confirm/', sms_views.sms_plan_confirm_step, name='sms_plan_confirm_step'),
    path('marketing/sms/plan/<uuid:pk>/step/<int:step>/remove/', sms_views.sms_plan_remove_step, name='sms_plan_remove_step'),

    # Forecast Tool
    path('forecast/', views.forecast_tool, name='forecast_tool'),
    path('forecast/api/', views.forecast_api, name='forecast_api'),
    
    # Orders
    path('orders/<uuid:order_id>/', views.order_detail, name='order_detail'),
    path('orders/<uuid:order_id>/refund/', views.refund_order, name='refund_order'),
    path('orders/<uuid:order_id>/resend-confirmation/', views.resend_order_confirmation, name='resend_order_confirmation'),
    
    # CSV Formats
    path('formats/', views.format_list, name='format_list'),
    path('formats/create/', views.format_create, name='format_create'),
    path('formats/<uuid:format_id>/edit/', views.format_edit, name='format_edit'),
    path('formats/<uuid:format_id>/delete/', views.format_delete, name='format_delete'),
    path('formats/<uuid:format_id>/set-default/', views.format_set_default, name='format_set_default'),
    path('formats/<uuid:format_id>/duplicate/', views.format_duplicate, name='format_duplicate'),

    # Settings
    path('settings/', views.settings_overview, name='settings_overview'),
    path('settings/display/', views.settings_display_preferences, name='settings_display_preferences'),
    path('settings/segment-tuning/', views.settings_segment_tuning, name='settings_segment_tuning'),

    # === Integrations ===
    # One place that frames Mailchimp, Typeform, SlickText, Meta Ads, and Google
    # Calendar as optional, pluggable integrations. Connection/settings pages live
    # under settings/integrations/; handlers live in tickets/integrations/.
    # NOTE: OAuth callback paths (meta-ads/mailchimp) are intentionally left at their
    # original URLs because they are registered as redirect URIs in the provider apps.
    path('settings/integrations/', integrations_hub.integrations_overview, name='integrations_overview'),

    path('settings/integrations/meta-ads/', meta_ads_views.meta_ads_settings, name='meta_ads_settings'),
    path('settings/integrations/meta-ads/connect/', meta_ads_views.meta_ads_connect, name='meta_ads_connect'),
    path('settings/meta-ads/callback/', meta_ads_views.meta_ads_callback, name='meta_ads_callback'),
    path('settings/integrations/meta-ads/select-account/', meta_ads_views.meta_ads_select_account, name='meta_ads_select_account'),
    path('settings/integrations/meta-ads/disconnect/', meta_ads_views.meta_ads_disconnect, name='meta_ads_disconnect'),

    path('settings/integrations/mailchimp/', mailchimp_views.mailchimp_settings, name='mailchimp_settings'),
    path('settings/integrations/mailchimp/connect/', mailchimp_views.mailchimp_connect, name='mailchimp_connect'),
    path('settings/mailchimp/callback/', mailchimp_views.mailchimp_callback, name='mailchimp_callback'),
    path('settings/integrations/mailchimp/disconnect/', mailchimp_views.mailchimp_disconnect, name='mailchimp_disconnect'),
    path('settings/integrations/mailchimp/hints/', mailchimp_views.mailchimp_save_hints, name='mailchimp_save_hints'),

    path('settings/integrations/slicktext/', slicktext_views.slicktext_settings, name='slicktext_settings'),
    path('settings/integrations/slicktext/save/', slicktext_views.slicktext_save, name='slicktext_save'),
    path('settings/integrations/slicktext/disconnect/', slicktext_views.slicktext_disconnect, name='slicktext_disconnect'),

    path('settings/integrations/google-calendar/', google_calendar_views.settings_google_calendar, name='settings_google_calendar'),
    path('settings/integrations/google-calendar/disconnect/', google_calendar_views.settings_google_calendar_disconnect, name='settings_google_calendar_disconnect'),
    path('settings/profile/', views.org_profile, name='org_profile'),
    path('settings/api-keys/', views.settings_api_keys, name='settings_api_keys'),
    path('settings/ai-token-usage/', views.ai_token_usage_dashboard, name='ai_token_usage'),
    path('settings/api-keys/<uuid:key_id>/revoke/', views.settings_api_key_revoke, name='settings_api_key_revoke'),
    path('settings/custom-fields/', views.custom_field_list, name='custom_field_list'),
    path('settings/custom-fields/create/', views.custom_field_create, name='custom_field_create'),
    path('settings/custom-fields/<int:field_id>/edit/', views.custom_field_edit, name='custom_field_edit'),
    path('settings/custom-fields/<int:field_id>/delete/', views.custom_field_delete, name='custom_field_delete'),
    path('settings/custom-fields/reorder/', views.custom_field_reorder, name='custom_field_reorder'),

    # Markets
    path('markets/', views.market_list, name='market_list'),
    path('markets/builder/', views.market_builder, name='market_builder'),

    # Venues
    path('venues/', views.venue_list, name='venue_list'),
    path('venues/create/', views.venue_create, name='venue_create'),
    path('venues/create-inline/', views.venue_create_inline, name='venue_create_inline'),
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
    path('events/<uuid:event_id>/ticket-types/<uuid:ticket_type_id>/orders/', views.saleable_ticket_type_orders, name='saleable_ticket_type_orders'),
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

    # Tracking Links — short '/t/' is canonical; '/track/' kept as a legacy
    # alias so links already sent in past SMS still resolve and attribute.
    path('t/<str:token>/', views.track_link_redirect, name='track_link_redirect'),
    path('track/<str:token>/', views.track_link_redirect, name='track_link_redirect_legacy'),
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
