import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ltv_updater.settings')
app = Celery('ltv_updater')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
