from django.urls import path
from . import views

app_name = 'tickets'

urlpatterns = [
    # Authentication
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('password-reset/', views.password_reset_request, name='password_reset'),
    path('password-reset/done/', views.password_reset_done, name='password_reset_done'),
    path('password-reset-confirm/<uidb64>/<token>/', views.password_reset_confirm, name='password_reset_confirm'),
    path('password-reset-complete/', views.password_reset_complete, name='password_reset_complete'),
    
    # Health check endpoint
    path('health/', views.health_check, name='health_check'),
    
    # Organization (no-org flow)
    path('org-required/', views.org_required, name='org_required'),
    path('create-organization/', views.create_organization, name='create_organization'),
    
    # Home/Dashboard
    path('', views.home, name='home'),
    
    # CSV Upload
    path('upload/', views.upload_csv, name='upload_csv'),
    path('upload/price-entry/<uuid:file_id>/', views.price_entry, name='price_entry'),
    path('upload/results/<uuid:file_id>/', views.upload_results, name='upload_results'),
    path('upload/<uuid:file_id>/delete/', views.upload_delete, name='upload_delete'),

    # Customers
    path('customers/', views.customer_list, name='customer_list'),
    path('customers/<uuid:customer_id>/', views.customer_detail, name='customer_detail'),
    
    # Events
    path('events/', views.event_list, name='event_list'),
    path('events/create/', views.event_create, name='event_create'),
    path('events/<uuid:event_id>/', views.event_detail, name='event_detail'),
    path('events/<uuid:event_id>/edit/', views.event_edit, name='event_edit'),
    path('events/<uuid:event_id>/upload/', views.event_upload_csv, name='event_upload_csv'),
    path('events/<uuid:event_id>/delete/', views.event_delete, name='event_delete'),

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

    # Venues
    path('venues/', views.venue_list, name='venue_list'),
    path('venues/create/', views.venue_create, name='venue_create'),
    path('venues/<uuid:venue_id>/edit/', views.venue_edit, name='venue_edit'),
]
