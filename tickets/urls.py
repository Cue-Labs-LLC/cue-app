from django.urls import path
from . import views

app_name = 'tickets'

urlpatterns = [
    # Health check endpoint
    path('health/', views.health_check, name='health_check'),
    
    # Home/Dashboard
    path('', views.home, name='home'),
    
    # CSV Upload
    path('upload/', views.upload_csv, name='upload_csv'),
    path('upload/price-entry/<uuid:file_id>/', views.price_entry, name='price_entry'),
    path('upload/results/<uuid:file_id>/', views.upload_results, name='upload_results'),
    
    # Customers
    path('customers/', views.customer_list, name='customer_list'),
    path('customers/<uuid:customer_id>/', views.customer_detail, name='customer_detail'),
    
    # CSV Formats
    path('formats/', views.format_list, name='format_list'),
    path('formats/create/', views.format_create, name='format_create'),
    path('formats/<uuid:format_id>/edit/', views.format_edit, name='format_edit'),
    path('formats/<uuid:format_id>/delete/', views.format_delete, name='format_delete'),
    path('formats/<uuid:format_id>/set-default/', views.format_set_default, name='format_set_default'),
    
    # Venues
    path('venues/create/', views.venue_create, name='venue_create'),
]
