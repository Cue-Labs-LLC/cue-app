# Data migration: set Parking Info custom field options to match built-in choices
# and backfill event.parking_info into EventCustomFieldValue; then remove parking_info.

from django.db import migrations


# Labels for the Parking Info custom field (match former PARKING_CHOICES display).
PARKING_OPTION_LABELS = [
    '—',
    'Street parking',
    'Lot available',
    'No parking',
    'See venue / contact venue',
]

# Map old Event.parking_info value to new option label (for backfill).
PARKING_VALUE_TO_LABEL = {
    'street': 'Street parking',
    'lot': 'Lot available',
    'none': 'No parking',
    'venue': 'See venue / contact venue',
}


def update_parking_custom_field_and_backfill(apps, schema_editor):
    Event = apps.get_model('tickets', 'Event')
    CustomField = apps.get_model('tickets', 'CustomField')
    CustomFieldOption = apps.get_model('tickets', 'CustomFieldOption')
    EventCustomFieldValue = apps.get_model('tickets', 'EventCustomFieldValue')

    for cf in CustomField.objects.filter(name='Parking Info').select_related('organization'):
        # Create new options (match non-custom field choices).
        new_options = []
        for i, label in enumerate(PARKING_OPTION_LABELS):
            opt = CustomFieldOption.objects.create(
                custom_field=cf, label=label, order=i
            )
            new_options.append(opt)

        label_to_option = {opt.label: opt for opt in new_options}

        # Backfill: for each event in this org with parking_info set, set custom value.
        events = Event.objects.filter(
            organization=cf.organization
        ).exclude(parking_info='').exclude(parking_info__isnull=True)
        for event in events:
            label = PARKING_VALUE_TO_LABEL.get(event.parking_info)
            if not label:
                continue
            option = label_to_option.get(label)
            if not option:
                continue
            EventCustomFieldValue.objects.update_or_create(
                event=event,
                custom_field=cf,
                defaults={'custom_field_option': option},
            )

        # Map existing EventCustomFieldValues from old option labels to new options.
        old_to_new_label = {
            'Street parking available': 'Street parking',
            'Limited Parking available, rideshare': 'Lot available',
        }
        for ev_val in EventCustomFieldValue.objects.filter(
            custom_field=cf,
            custom_field_option__label__in=list(old_to_new_label),
        ).select_related('custom_field_option'):
            new_label = old_to_new_label.get(ev_val.custom_field_option.label)
            if new_label and new_label in label_to_option:
                ev_val.custom_field_option = label_to_option[new_label]
                ev_val.save()

        # Remove old options (from original seed).
        CustomFieldOption.objects.filter(
            custom_field=cf,
            label__in=[
                'Limited Parking available, rideshare',
                'Street parking available',
            ],
        ).delete()


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0025_add_customfield_required_and_default_option'),
    ]

    operations = [
        migrations.RunPython(update_parking_custom_field_and_backfill, noop),
        migrations.RemoveField(
            model_name='event',
            name='parking_info',
        ),
    ]
