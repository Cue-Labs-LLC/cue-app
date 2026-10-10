"""Run a raw market-competition web search for manual inspection (P2).

Thin wrapper over ``tickets.services.market_competition.search_client`` — no
business logic, no LLM. Prints the title / url / snippet of each result so a
developer can eyeball what the search layer returns before the scanner (P3)
turns it into structured competitors.

    python manage.py market_search "hip-hop events in Los Angeles June 2026"

With ``TAVILY_API_KEY`` unset it prints a clear notice and exits 0 (the same
graceful empty the feature uses in production).
"""
import json

from django.conf import settings
from django.core.management.base import BaseCommand

from tickets.services.market_competition import search_client


class Command(BaseCommand):
    help = "Run a raw market-competition web search and print the results."

    def add_arguments(self, parser):
        parser.add_argument('query', help='The search query to run.')
        parser.add_argument(
            '--max-results', type=int, default=None,
            help='Override MARKET_COMPETITION_MAX_RESULTS for this query.',
        )
        parser.add_argument(
            '--json', action='store_true',
            help='Print raw result dicts as JSON instead of a formatted list.',
        )
        parser.add_argument(
            '--include-domains', default=None,
            help='Comma-separated domains to restrict the search to '
                 '(e.g. "eventbrite.com,dice.fm,seetickets.com").',
        )
        parser.add_argument(
            '--exclude-domains', default=None,
            help='Comma-separated domains to drop from results '
                 '(e.g. "ticketmaster.com,livenation.com,axs.com").',
        )

    def _split(self, raw):
        if not raw:
            return None
        return [d.strip() for d in raw.split(',') if d.strip()] or None

    def handle(self, *args, **options):
        query = options['query']

        if not getattr(settings, 'TAVILY_API_KEY', ''):
            self.stdout.write(self.style.WARNING(
                'TAVILY_API_KEY is not set — returning no results. '
                'Set it to exercise the live search.'
            ))

        results = search_client.web_search(
            query,
            max_results=options.get('max_results'),
            include_domains=self._split(options.get('include_domains')),
            exclude_domains=self._split(options.get('exclude_domains')),
        )

        if options.get('json'):
            self.stdout.write(json.dumps(results, indent=2))
            return

        self.stdout.write(self.style.SUCCESS(
            f'{len(results)} result(s) for {query!r}:'
        ))
        for i, r in enumerate(results, 1):
            title = r.get('title') or '(no title)'
            url = r.get('url') or ''
            content = (r.get('content') or '').strip().replace('\n', ' ')
            if len(content) > 200:
                content = content[:197] + '...'
            self.stdout.write(f'\n{i}. {title}')
            if url:
                self.stdout.write(f'   {url}')
            if content:
                self.stdout.write(f'   {content}')
