from django.db import migrations
from django.utils import timezone


def split_event_date(apps, schema_editor):
    """Split event_date into start_date and start_time."""
    Event = apps.get_model('tickets', 'Event')
    for event in Event.objects.all():
        if event.event_date:
            local_dt = timezone.localtime(event.event_date)
            event.start_date = local_dt.date()
            event.start_time = local_dt.time()
            event.save(update_fields=['start_date', 'start_time'])


def merge_event_date(apps, schema_editor):
    """Reverse: merge start_date and start_time back into event_date."""
    import datetime as dt
    Event = apps.get_model('tickets', 'Event')
    for event in Event.objects.all():
        if event.start_date:
            time_part = event.start_time or dt.time(0, 0, 0)
            naive_dt = dt.datetime.combine(event.start_date, time_part)
            event.event_date = timezone.make_aware(naive_dt)
            event.save(update_fields=['event_date'])


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0007_add_start_end_date_time_fields'),
    ]

    operations = [
        migrations.RunPython(split_event_date, merge_event_date),
    ]
