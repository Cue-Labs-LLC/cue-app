from django.db import transaction
from django.db.models import Count

from tickets.models import (
    Event,
    Market,
    MARKET_GEOGRAPHY_CITY,
    MARKET_GEOGRAPHY_COUNTRY,
    MARKET_GEOGRAPHY_STATE,
)

NO_MARKET_LABEL = 'No market'


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
        self._market_index = None

    @classmethod
    def normalize_level(cls, level):
        level = (level or MARKET_GEOGRAPHY_CITY).strip().lower()
        if level not in cls.LEVEL_FIELDS:
            return MARKET_GEOGRAPHY_CITY
        return level

    @staticmethod
    def normalize_value(value):
        return (value or '').strip()

    @staticmethod
    def label_for_market(market):
        return market.name if market else NO_MARKET_LABEL

    def market_index(self):
        """This org's markets loaded once as ``{level: {geography_value: Market}}``.

        Cached on the instance so a single builder resolves many events with
        one query instead of a query per level per event. Call
        ``invalidate_market_index()`` after creating markets. Reuse a single
        builder across a loop (e.g. CSV import) to share the cache.

            Market rows (1 query)                     resolve_for_venue(venue)
            ┌───────────────────────────┐             city?  → index[city][v]
            │ city  → {'Austin': <M1>}  │  ── build ─▶ state? → index[state][v]
            │ state → {'TX': <M2>}      │             country?→ index[country][v]
            │ ...                       │             first hit wins (specificity)
            └───────────────────────────┘
        """
        if self._market_index is None:
            index = {}
            for market in Market.objects.filter(organization=self.organization):
                index.setdefault(market.geography_level, {})[market.geography_value] = market
            self._market_index = index
        return self._market_index

    def invalidate_market_index(self):
        """Drop the cached index so the next resolve re-reads markets from the DB."""
        self._market_index = None

    def resolve_for_venue(self, venue):
        """Return the best matching existing market for a venue, or None.

        Specificity order: city > state > country. Backed by the in-memory
        market index (one query per builder), not a query per level.
        """
        if not venue:
            return None

        index = self.market_index()
        candidates = (
            (MARKET_GEOGRAPHY_CITY, getattr(venue, 'city', '')),
            (MARKET_GEOGRAPHY_STATE, getattr(venue, 'state', '')),
            (MARKET_GEOGRAPHY_COUNTRY, getattr(venue, 'country', '')),
        )
        for level, raw_value in candidates:
            value = self.normalize_value(raw_value)
            if not value:
                continue
            market = index.get(level, {}).get(value)
            if market:
                return market
        return None

    def assign_event(self, event, save=True):
        """Assign an event's market from existing rules. Does not create markets."""
        market = self.resolve_for_venue(getattr(event, 'venue', None))
        event.market = market
        if save and event.pk:
            event.save(update_fields=['market'])
        return market

    def assign_events_for_venue(self, venue):
        """Reassign all events at a venue after its geography changes."""
        market = self.resolve_for_venue(venue)
        return Event.objects.filter(
            organization=self.organization,
            venue=venue,
        ).update(market=market)

    def assign_all_events(self):
        """Backfill all organization events from existing market rules.

        One query for the market index, one pull of events, one bulk_update
        for the changed rows — no per-event queries or saves. Mirrors the
        batched pattern in migration 0170_backfill_event_markets.
        """
        events = (
            Event.objects.filter(organization=self.organization)
            .select_related('venue')
            .only('id', 'market_id', 'venue__city', 'venue__state', 'venue__country')
        )
        changed = []
        for event in events:
            market = self.resolve_for_venue(event.venue)
            new_market_id = market.id if market else None
            if event.market_id != new_market_id:
                event.market_id = new_market_id
                changed.append(event)
        if changed:
            Event.objects.bulk_update(changed, ['market'], batch_size=500)
        return len(changed)

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
        raw_values = values or []
        values = []
        seen = set()
        for value in raw_values:
            value = self.normalize_value(value)
            if value and value not in seen:
                values.append(value)
                seen.add(value)

        created_count = 0
        markets = []
        for value in values:
            market, created = self._get_or_create_market(level, value)
            if created:
                created_count += 1
            markets.append(market)
        # New markets were just created, so drop the cached index before the
        # backfill resolves events against the full, current market set.
        self.invalidate_market_index()
        updated_count = self.assign_all_events()

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
