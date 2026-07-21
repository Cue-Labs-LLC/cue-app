from django.db import migrations


def backfill_event_markets(apps, schema_editor):
    Event = apps.get_model('tickets', 'Event')
    Market = apps.get_model('tickets', 'Market')

    market_rows = list(
        Market.objects.values('id', 'organization_id', 'geography_level', 'geography_value')
    )
    markets_by_org = {}
    for market in market_rows:
        markets_by_org.setdefault(market['organization_id'], {}).setdefault(
            market['geography_level'], {}
        )[market['geography_value']] = market['id']

    events = Event.objects.select_related('venue').only(
        'id', 'organization_id', 'market_id',
        'venue__city', 'venue__state', 'venue__country',
    )
    for event in events.iterator():
        org_markets = markets_by_org.get(event.organization_id) or {}
        venue = event.venue
        market_id = None
        for level, value in (
            ('city', venue.city),
            ('state', venue.state),
            ('country', venue.country),
        ):
            value = (value or '').strip()
            if value and value in org_markets.get(level, {}):
                market_id = org_markets[level][value]
                break
        if event.market_id != market_id:
            Event.objects.filter(pk=event.pk).update(market_id=market_id)


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0169_market_event_market'),
    ]

    operations = [
        migrations.RunPython(backfill_event_markets, migrations.RunPython.noop),
    ]
