"""Organic Instagram content linked to an event (event detail Marketing tab).

Organizers link posts to the events they promoted or recapped (``InstagramPostEvent``,
many-to-many). ``EventInstagramContentCalculator`` sums those posts' lifetime organic
metrics and suggests unlinked posts that look like they belong to the event: published
in the run-up to it (or just after, for recaps) and naming its city or venue in the
caption. Suggestions are only ever offered, never auto-linked.
"""

import re
import statistics
from datetime import timedelta

from django.utils import timezone as django_tz

from ...models import InstagramPostEvent
from .content_insights import FORMAT_LABELS, FORMAT_REEL, InstagramContentInsightsCalculator

# Promo for an event typically runs up to ~2 months out; recaps land within a week.
SUGGEST_DAYS_BEFORE = 60
SUGGEST_DAYS_AFTER = 7
# "Find more posts" browser: a wider window than suggestions.
BROWSE_DAYS_BEFORE = 90
BROWSE_DAYS_AFTER = 14
MAX_SUGGESTIONS = 8

# Common shorthand in promoter captions. Keys are lowercase city names as stored on
# Venue.city / Market.name; values are extra spellings to look for (word-bounded).
CITY_ALIASES = {
    'new york': ['nyc', 'new york city'],
    'brooklyn': ['bk', 'nyc', 'new york'],
    'los angeles': ['la', 'l.a.'],
    'las vegas': ['vegas', 'lv'],
    'san francisco': ['sf', 'bay area'],
    'washington': ['dc', 'd.c.'],
    'atlanta': ['atl'],
    'chicago': ['chi', 'chi-town'],
    'houston': ['htx'],
    'phoenix': ['phx'],
    'san diego': ['sd'],
    'dallas': ['dfw'],
    'philadelphia': ['philly'],
    'new orleans': ['nola'],
    'miami': ['mia'],
}


def _mentions(text, phrase):
    return bool(phrase) and re.search(
        r'(?<![\w])' + re.escape(phrase.lower()) + r'(?![\w])', text,
    ) is not None


def event_place_terms(event):
    """``[(label, [phrases])]`` naming where the event is, for caption matching."""
    terms = []
    cities = {
        (event.venue.city if event.venue else '') or '',
        (event.market.name if event.market else '') or '',
    }
    for city in sorted(c.strip() for c in cities if c and c.strip()):
        terms.append((city, [city.lower(), *CITY_ALIASES.get(city.lower(), [])]))
    if event.venue and event.venue.name:
        terms.append((event.venue.name, [event.venue.name.lower()]))
    return terms


def timing_label(post_date, event_date):
    days = (event_date - post_date).days
    if days == 0:
        return 'Day of event'
    if days > 0:
        return f"{days} day{'s' if days != 1 else ''} before"
    return f"{-days} day{'s' if days != -1 else ''} after"


class EventInstagramContentCalculator:
    """Linked-post totals + link suggestions for one event."""

    def __init__(self, organization, event, client=None):
        self.organization = organization
        self.event = event
        self.content = InstagramContentInsightsCalculator(organization, window='365d', client=client)

    def calculate(self):
        links = list(
            InstagramPostEvent.objects.filter(organization=self.organization, event=self.event)
            .values_list('ig_media_id', flat=True)
        )
        rows, missing = self.content.rows_for_media_ids(links)
        tz = django_tz.get_current_timezone()
        for row in rows:
            row['timing'] = timing_label(
                row['published_at'].astimezone(tz).date(), self.event.start_date,
            )
        rows.sort(key=lambda r: r['published_at'])

        linked_ids = set(links)
        pull = self.content.raw_posts()
        return {
            'posts': rows,
            'missing_count': len(missing),
            'totals': self._totals(rows),
            'suggestions': self._suggestions(pull, linked_ids, tz),
            'browse': self._browse(pull, linked_ids, tz),
        }

    def totals(self):
        """Linked-post totals only (no suggestions/browse), for the Overview compare."""
        links = list(
            InstagramPostEvent.objects.filter(organization=self.organization, event=self.event)
            .values_list('ig_media_id', flat=True)
        )
        rows, _missing = self.content.rows_for_media_ids(links)
        return self._totals(rows)

    def _totals(self, rows):
        measured = [r for r in rows if r['has_insights'] and r['reach'] is not None]
        feed = [r for r in measured if r['format'] != FORMAT_REEL]

        def total(field, source=measured):
            values = [r[field] for r in source if r[field] is not None]
            return sum(values) if values else None

        reach = total('reach')
        interactions = total('interactions')
        indexes = sorted(r['reach_index'] for r in measured if r['reach_index'] is not None)
        return {
            'posts': len(rows),
            'formats': [
                (label, sum(1 for r in rows if r['format'] == fmt))
                for fmt, label in FORMAT_LABELS.items()
                if any(r['format'] == fmt for r in rows)
            ],
            'reach': reach,
            'views': total('views'),
            'interactions': interactions,
            'engagement_rate': round(interactions / reach * 100, 1) if reach and interactions is not None else None,
            'likes': total('likes', rows),
            'comments': total('comments', rows),
            'shares': total('shares'),
            'saves': total('saves'),
            'shares_saves': (
                (total('shares') or 0) + (total('saves') or 0)
                if total('shares') is not None or total('saves') is not None else None
            ),
            'follows': total('follows', feed),
            'profile_visits': total('profile_visits', feed),
            'has_feed_posts': bool(feed),
            'median_reach_index': round(statistics.median(indexes), 1) if indexes else None,
        }

    def _window(self, pull, linked_ids, tz, days_before, days_after):
        start = self.event.start_date - timedelta(days=days_before)
        end = self.event.start_date + timedelta(days=days_after)
        for item in pull:
            day = item['published_at'].astimezone(tz).date()
            if item['id'] not in linked_ids and start <= day <= end:
                yield item, day

    def _suggestions(self, pull, linked_ids, tz):
        terms = event_place_terms(self.event)
        if not terms:
            return []
        scored = []
        for item, day in self._window(pull, linked_ids, tz, SUGGEST_DAYS_BEFORE, SUGGEST_DAYS_AFTER):
            caption = (item.get('caption') or '').lower()
            matched = [label for label, phrases in terms if any(_mentions(caption, p) for p in phrases)]
            if not matched:
                continue
            distance = abs((self.event.start_date - day).days)
            scored.append((len(matched), -distance, item, day, matched))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return [
            self._candidate(item, day, reason=f"Mentions {', '.join(matched)}")
            for _n, _d, item, day, matched in scored[:MAX_SUGGESTIONS]
        ]

    def _browse(self, pull, linked_ids, tz):
        return [
            self._candidate(item, day)
            for item, day in self._window(pull, linked_ids, tz, BROWSE_DAYS_BEFORE, BROWSE_DAYS_AFTER)
        ]

    def _candidate(self, item, day, reason=''):
        insights = item.get('insights') or {}
        return {
            'id': item['id'],
            'caption': (item.get('caption') or '').strip(),
            'thumbnail': item.get('thumbnail_url') or (
                item.get('media_url') if item['format'] != FORMAT_REEL else ''
            ) or '',
            'format': item['format'],
            'published_at': item['published_at'],
            'timing': timing_label(day, self.event.start_date),
            'reach': insights.get('reach'),
            'reason': reason,
        }
