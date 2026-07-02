from django.db import transaction
from django.db.models import Count

from tickets.models import (
    Event,
    Market,
    MARKET_GEOGRAPHY_CITY,
    MARKET_GEOGRAPHY_COUNTRY,
    MARKET_GEOGRAPHY_STATE,
)


class MarketBuilder:
    """Create org markets from event venue geography and assign matching events."""

    LEVEL_FIELDS = {
        MARKET_GEOGRAPHY_CITY: 'city',
        MARKET_GEOGRAPHY_STATE: 'state',
        MARKET_GEOGRAPHY_COUNTRY: 'country',
    }

    LEVEL_LABELS = {
        MARKET_GEOGRAPHY_CITY: 'City',
        MARKET_GEOGRAPHY_STATE: 'State',
        MARKET_GEOGRAPHY_COUNTRY: 'Country',
    }

    def __init__(self, organization):
        self.organization = organization

    @classmethod
    def normalize_level(cls, level):
        level = (level or MARKET_GEOGRAPHY_CITY).strip().lower()
        if level not in cls.LEVEL_FIELDS:
            return MARKET_GEOGRAPHY_CITY
        return level

    @staticmethod
    def normalize_value(value):
        return (value or '').strip()

    def preview(self, level):
        level = self.normalize_level(level)
        venue_field = self.LEVEL_FIELDS[level]
        value_key = f'venue__{venue_field}'
        existing_values = set(
            Market.objects.filter(
                organization=self.organization,
                geography_level=level,
            ).values_list('geography_value', flat=True)
        )

        rows = (
            Event.objects.filter(organization=self.organization)
            .exclude(**{value_key: ''})
            .values(value_key)
            .annotate(
                event_count=Count('id'),
                venue_count=Count('venue', distinct=True),
            )
            .order_by(value_key)
        )

        preview_rows = []
        for row in rows:
            value = self.normalize_value(row[value_key])
            if not value:
                continue
            preview_rows.append({
                'name': value,
                'value': value,
                'event_count': row['event_count'],
                'venue_count': row['venue_count'],
                'market_exists': value in existing_values,
            })
        return preview_rows

    @transaction.atomic
    def build(self, level, values):
        level = self.normalize_level(level)
        venue_field = self.LEVEL_FIELDS[level]
        raw_values = values or []
        values = []
        seen = set()
        for value in raw_values:
            value = self.normalize_value(value)
            if value and value not in seen:
                values.append(value)
                seen.add(value)

        created_count = 0
        updated_count = 0
        markets = []
        for value in values:
            market, created = self._get_or_create_market(level, value)
            if created:
                created_count += 1
            markets.append(market)
            updated_count += Event.objects.filter(
                organization=self.organization,
                **{f'venue__{venue_field}': value},
            ).update(market=market)

        return {
            'created_count': created_count,
            'updated_count': updated_count,
            'markets': markets,
        }

    def _get_or_create_market(self, level, value):
        market = Market.objects.filter(
            organization=self.organization,
            geography_level=level,
            geography_value=value,
        ).first()
        if market:
            return market, False

        return Market.objects.create(
            organization=self.organization,
            name=self._unique_name(value, level),
            geography_level=level,
            geography_value=value,
        ), True

    def _unique_name(self, base_name, level):
        if not Market.objects.filter(organization=self.organization, name=base_name).exists():
            return base_name

        level_label = self.LEVEL_LABELS[level]
        candidate = f'{base_name} ({level_label})'
        if not Market.objects.filter(organization=self.organization, name=candidate).exists():
            return candidate

        suffix = 2
        while True:
            candidate = f'{base_name} ({level_label} {suffix})'
            if not Market.objects.filter(organization=self.organization, name=candidate).exists():
                return candidate
            suffix += 1
