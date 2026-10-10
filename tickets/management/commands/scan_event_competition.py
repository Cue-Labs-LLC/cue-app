"""Run a full market-competition scan for one event synchronously (P3 demo).

Drives the whole Phase 3 pipeline — derive genre hints → fixed query plan →
Tavily search → LLM extraction → deterministic scoring → narrative — against a
real seeded event and prints the result. This is the Phase 3 demo surface; the
in-app panel and Celery task arrive in P4/P5.

    python manage.py scan_event_competition <event_id>
    python manage.py scan_event_competition <event_id> --json

Needs ``TAVILY_API_KEY`` + ``OPENAI_API_KEY`` to produce a real result; without
them it prints a clear notice and an ``unavailable`` result (the same graceful
degradation the feature uses in production — never a silent 0, D2).

NOTE: the numeric score is printed here for developer inspection. In the UI it
stays gated until the P6 eval meets the precision bar (DO1).
"""
import json

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from tickets.models import Event
from tickets.services.ai_tracing import flush_traces
from tickets.services.market_competition import (
    calculate_event_competition,
    derive_genre_hints,
)


class Command(BaseCommand):
    help = "Run a full market-competition scan for one event and print the result."

    def add_arguments(self, parser):
        parser.add_argument('event_id', help='UUID of the event to scan.')
        parser.add_argument(
            '--genres', default=None,
            help='Comma-separated genre hints to use instead of the auto-derived ones '
                 '(e.g. "techno,house"). Overrides the keyword-map derivation; pass '
                 'an empty value to force the city+date density fallback.',
        )
        parser.add_argument(
            '--include-domains', default=None,
            help='Comma-separated hostnames to restrict every search to for this scan '
                 '(allow-list, e.g. "eventbrite.com,dice.fm,seetickets.com"). '
                 'Per-run only — does not touch global settings.',
        )
        parser.add_argument(
            '--exclude-domains', default=None,
            help='Comma-separated hostnames to drop from every search for this scan '
                 '(deny-list, e.g. "ticketmaster.com,livenation.com,axs.com"). '
                 'Per-run only — does not touch global settings.',
        )
        parser.add_argument(
            '--json', action='store_true',
            help='Print the full CompetitionResult as JSON instead of a formatted report.',
        )

    @staticmethod
    def _split(raw):
        """Comma-separated values -> list, or None when the flag was not given."""
        if raw is None:
            return None
        return [v.strip() for v in raw.split(',') if v.strip()]

    def handle(self, *args, **options):
        event_id = options['event_id']
        try:
            event = Event.objects.select_related('venue').get(id=event_id)
        except (Event.DoesNotExist, ValidationError, ValueError, TypeError):
            raise CommandError(f'No event found with id {event_id!r}.')

        if not getattr(settings, 'TAVILY_API_KEY', ''):
            self.stdout.write(self.style.WARNING(
                'TAVILY_API_KEY is not set — the scan will report "unavailable". '
                'Set it (and OPENAI_API_KEY) to exercise a live scan.'
            ))
        if not getattr(settings, 'OPENAI_API_KEY', ''):
            self.stdout.write(self.style.WARNING(
                'OPENAI_API_KEY is not set — extraction/narrative will fail.'
            ))

        genres = self._split(options.get('genres'))
        include_domains = self._split(options.get('include_domains'))
        exclude_domains = self._split(options.get('exclude_domains'))
        try:
            result = calculate_event_competition(
                event.organization, event, genre_hints=genres,
                include_domains=include_domains, exclude_domains=exclude_domains,
            )
        finally:
            # One-shot CLI process: flush buffered Langfuse spans before exit, or the
            # background sender may not deliver this run's trace (no-op if tracing off).
            flush_traces()

        if options.get('json'):
            self.stdout.write(json.dumps(result.to_dict(), indent=2))
            return

        # Show which genre hints actually drove the scan (explicit override, or derived).
        hints_used = genres if genres is not None else derive_genre_hints(event)
        self._print_report(event, result, hints_used, include_domains, exclude_domains)

    def _print_report(self, event, result, hints_used, include_domains, exclude_domains):
        style = self.style
        self.stdout.write(style.SUCCESS(
            f'\nMarket competition — {event.name} '
            f'({getattr(event.venue, "city", "") or "no city"})'
        ))
        if hints_used:
            self.stdout.write(f'  genres:   {", ".join(hints_used)}')
        else:
            self.stdout.write('  genres:   (none — city+date density fallback, DO5-f)')
        if include_domains:
            self.stdout.write(f'  include:  {", ".join(include_domains)}')
        if exclude_domains:
            self.stdout.write(f'  exclude:  {", ".join(exclude_domains)}')
        self.stdout.write(f'  status:   {result.status}')
        self.stdout.write(f'  label:    {result.label}')
        self.stdout.write(f'  score:    {result.score}  (ungated — CLI only; UI gated by P6/DO1)')

        counts = result.counts or {}
        if counts:
            self.stdout.write(
                '  counts:   '
                f'total={counts.get("total", 0)} '
                f'same_metro={counts.get("same_metro", 0)} '
                f'in_window={counts.get("in_window", 0)} '
                f'genre_matched={counts.get("genre_matched", 0)} '
                f'undated={counts.get("undated", 0)}'
            )
        coverage = result.coverage or {}
        if coverage:
            self.stdout.write(
                '  coverage: '
                f'{coverage.get("queries_with_results", 0)}/{coverage.get("queries_total", 0)} '
                f'queries returned results (ratio={coverage.get("ratio", 0):.2f})'
            )

        self.stdout.write('\n  Competing events (scored):')
        if result.competitors:
            for c in result.competitors:
                when = c.date.isoformat() if c.date else 'date unknown'
                self.stdout.write(
                    f'   - {c.name} | {c.genre or "genre n/a"} | '
                    f'{c.venue_name or "venue n/a"} | {c.metro_normalized or "metro n/a"} | '
                    f'{when} | {c.platform or "platform n/a"}'
                )
                if c.source_url:
                    self.stdout.write(f'       {c.source_url}')
        else:
            self.stdout.write('   (none)')

        if result.undated:
            self.stdout.write('\n  Found but undated (excluded from score — DO5-d):')
            for c in result.undated:
                self.stdout.write(
                    f'   - {c.name} | {c.venue_name or "venue n/a"} | '
                    f'{c.platform or "platform n/a"}'
                )

        self.stdout.write('\n  Narrative:')
        self.stdout.write(f'   {result.summary or "(none)"}\n')
