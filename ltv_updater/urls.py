"""
URL configuration for ltv_updater project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from tickets import views as tickets_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('tickets.api_urls')),
    path('.well-known/apple-developer-merchantid-domain-association',
         tickets_views.apple_pay_domain_association,
         name='apple_pay_domain_association'),
    path('', include(('tickets.urls', 'tickets'))),
]

if settings.DEBUG:
    import debug_toolbar
    urlpatterns = [path('__debug__/', include(debug_toolbar.urls))] + urlpatterns

# Serve media files in development only
# In production, static files are served by WhiteNoise
# Media files are served from S3 (if configured) or not served directly
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    # Static files in development
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
