"""Backfill external_events_enabled=True for existing orgs (external-first onboarding).

Changing the model default (0171) only affects NEW rows. This flips existing orgs
so they can also use CSV-imported events. Only `external_events_enabled` is
backfilled: `sms_marketing_enabled` is a pilot/compliance (A2P) gate, so existing
orgs are left untouched and graduate to SMS on a separate, deliberate decision.
"""

import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def backfill_external_events(apps, schema_editor):
    Organization = apps.get_model('tickets', 'Organization')
    qs = Organization.objects.filter(external_events_enabled=False)
    count = qs.count()
    qs.update(external_events_enabled=True)
    logger.info(
        'Backfilled external_events_enabled=True for %s existing org(s).', count
    )


def reverse_noop(apps, schema_editor):
    # Irreversible: backfilled orgs are indistinguishable from originally-enabled
    # ones, so we do not flip anything back.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0171_alter_organization_external_events_enabled_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_external_events, reverse_noop),
    ]
