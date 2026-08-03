"""Seed event-linked AI recommendations so the event-list action badges show up.

Creates a handful of unresolved (NEW/REVIEWED) `AIRecommendation` rows attached to
the organization's most recent events, with varied counts per event so you can see
different badge numbers. Idempotent: re-running updates the same seeded rows (they
share a `seed-badge-` dedupe-key prefix). Use --clear to remove them.

Usage::

    python manage.py seed_event_action_badges
    python manage.py seed_event_action_badges --organization-slug my-org
    python manage.py seed_event_action_badges --organization-id <uuid> --events 5
    python manage.py seed_event_action_badges --clear
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tickets.models import AIRecommendation, Event, Organization

DEDUPE_PREFIX = 'seed-badge-'

# (kind, priority, title, summary) templates — cycled per recommendation on an event.
TEMPLATES = [
    (
        AIRecommendation.Kind.SALES_PACING,
        AIRecommendation.Priority.HIGH,
        'Sales are pacing behind similar events',
        'This event is tracking ~30% behind where comparable past events were at '
        'the same days-out. Consider a targeted SMS blast to lapsed buyers.',
    ),
    (
        AIRecommendation.Kind.POST_EVENT_WRAPUP,
        AIRecommendation.Priority.MEDIUM,
        'Post-event wrap-up is ready to review',
        'The event has ended. Review attendance vs. revenue and capture learnings '
        'for your next on-sale.',
    ),
    (
        AIRecommendation.Kind.MARKETING_ATTRIBUTION,
        AIRecommendation.Priority.LOW,
        'Link your marketing campaigns for attribution',
        'We found ad spend and email sends around this event that are not yet '
        'linked. Connect them to see channel ROI.',
    ),
]


class Command(BaseCommand):
    help = "Seed event-linked AI recommendations to test the event-list action badges."

    def add_arguments(self, parser):
        parser.add_argument('--organization-id', dest='organization_id')
        parser.add_argument('--organization-slug', dest='organization_slug')
        parser.add_argument(
            '--events',
            dest='events',
            type=int,
            default=4,
            help='Number of most-recent events to attach recommendations to (default 4).',
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Delete all seeded (seed-badge-) recommendations for the org and exit.',
        )

    def handle(self, *args, **options):
        org = self._resolve_org(options)

        if options.get('clear'):
            deleted, _ = AIRecommendation.objects.filter(
                organization=org,
                dedupe_key__startswith=DEDUPE_PREFIX,
            ).delete()
            self.stdout.write(self.style.SUCCESS(
                f'Cleared {deleted} seeded recommendation(s) for {org.name}.'
            ))
            return

        num_events = max(1, int(options.get('events') or 4))
        events = list(
            Event.objects.filter(organization=org, deleted_at__isnull=True)
            .order_by('-start_date')[:num_events]
        )
        if not events:
            raise CommandError(
                f'No events found for org "{org.name}". Create/import an event first.'
            )

        now = timezone.now()
        created = 0
        updated = 0
        # Give successive events a different number of actions (1, 2, 3, 1, 2, ...)
        # so the badges show a range of counts.
        for idx, event in enumerate(events):
            count = (idx % 3) + 1
            for i in range(count):
                kind, priority, title, summary = TEMPLATES[i % len(TEMPLATES)]
                # Every other event's first action is left as NEW; others REVIEWED —
                # both count as "unresolved" and render a badge.
                status = (
                    AIRecommendation.Status.NEW
                    if (idx + i) % 2 == 0
                    else AIRecommendation.Status.REVIEWED
                )
                obj, was_created = AIRecommendation.objects.update_or_create(
                    organization=org,
                    dedupe_key=f'{DEDUPE_PREFIX}{event.id}-{i}',
                    defaults={
                        'event': event,
                        'customer': None,
                        'kind': kind,
                        'status': status,
                        'priority': priority,
                        'confidence': Decimal('0.850'),
                        'title': title,
                        'summary': summary,
                        'evidence_json': {'seeded': True, 'event': event.name},
                        'recommended_action_json': {'label': 'Review', 'seeded': True},
                        'reviewed_at': now if status == AIRecommendation.Status.REVIEWED else None,
                        'dismissed_at': None,
                        'resolved_at': None,
                    },
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'Seeded {created} new + {updated} updated recommendation(s) across '
            f'{len(events)} event(s) for {org.name}.'
        ))
        self.stdout.write(
            'Visit /events/ — the flagged events now show a red "N action(s)" badge. '
            'Click one to open the Action Center filtered to that event. '
            'Run with --clear to remove the seeded rows.'
        )

    def _resolve_org(self, options):
        org_id = options.get('organization_id')
        org_slug = options.get('organization_slug')
        qs = Organization.objects.all()
        if org_id:
            qs = qs.filter(id=org_id)
        if org_slug:
            qs = qs.filter(slug=org_slug)

        if org_id or org_slug:
            org = qs.first()
            if not org:
                raise CommandError('Organization not found.')
            return org

        # No org specified: use the only org if unambiguous, otherwise list choices.
        orgs = list(Organization.objects.all()[:20])
        if len(orgs) == 1:
            return orgs[0]
        if not orgs:
            raise CommandError('No organizations exist. Create one first.')
        listing = '\n'.join(f'  - {o.name} (slug={o.slug}, id={o.id})' for o in orgs)
        raise CommandError(
            'Multiple organizations found — pass --organization-slug or '
            f'--organization-id:\n{listing}'
        )
