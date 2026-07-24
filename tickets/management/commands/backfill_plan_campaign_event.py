"""Relink AI-plan campaigns to their source event.

Campaigns launched from "Plan with AI" were saved with event=None when the plan's
event venue matched a Market (the step audience became market_ids, and the event
attribution logic only handled all_subscribers / event_id). The forward-path is
fixed; this command backfills existing campaigns.

Discovery: walk every SMSCampaignPlan that has an event, extract the
launched_campaign_id from each step, and set event on the SMSCampaign if it is
currently NULL. Campaigns that already have an event are left untouched.

Usage:
    python manage.py backfill_plan_campaign_event          # dry-run, show what would change
    python manage.py backfill_plan_campaign_event --apply  # write the updates
    python manage.py backfill_plan_campaign_event --org <slug>  # limit to one org
"""

import logging
import uuid

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


def _valid_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


class Command(BaseCommand):
    help = (
        'Relink AI-plan campaigns to their source event. '
        'Dry-run by default; pass --apply to write changes.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            default=False,
            help='Write updates to the database (default is dry-run).',
        )
        parser.add_argument(
            '--org',
            metavar='SLUG',
            default=None,
            help='Limit backfill to the org with this slug.',
        )

    def handle(self, *args, **options):
        from tickets.models import SMSCampaign, SMSCampaignPlan

        apply = options['apply']
        org_slug = options.get('org')

        plans_qs = SMSCampaignPlan.objects.filter(
            event__isnull=False,
        ).select_related('event', 'organization')

        if org_slug:
            plans_qs = plans_qs.filter(organization__slug=org_slug)

        total_plans = 0
        total_updated = 0
        total_already_linked = 0
        total_missing = 0

        for plan in plans_qs.iterator():
            total_plans += 1
            steps = plan.steps or []
            for step in steps:
                raw_id = step.get('launched_campaign_id')
                if not raw_id:
                    continue  # step not yet launched
                campaign_uuid = _valid_uuid(raw_id)
                if not campaign_uuid:
                    self.stdout.write(self.style.WARNING(
                        f'Plan {plan.id} step has invalid launched_campaign_id={raw_id!r} — skipping.'
                    ))
                    continue

                try:
                    campaign = SMSCampaign.objects.get(
                        id=campaign_uuid, organization=plan.organization,
                    )
                except SMSCampaign.DoesNotExist:
                    total_missing += 1
                    self.stdout.write(self.style.WARNING(
                        f'Campaign {campaign_uuid} from plan {plan.id} ({plan.organization.slug}) not found — skipping.'
                    ))
                    continue

                if campaign.event_id is not None:
                    total_already_linked += 1
                    continue  # already attributed; leave it alone

                self.stdout.write(
                    f'{"[DRY RUN] Would link" if not apply else "Linking"} '
                    f'campaign "{campaign.name}" ({campaign.id}) '
                    f'→ event "{plan.event.name}" ({plan.event.id}) '
                    f'[org: {plan.organization.slug}]'
                )
                if apply:
                    SMSCampaign.objects.filter(id=campaign.id).update(event=plan.event)
                total_updated += 1

        verb = 'Updated' if apply else 'Would update'
        self.stdout.write(self.style.SUCCESS(
            f'\n{verb} {total_updated} campaign(s) across {total_plans} plan(s). '
            f'{total_already_linked} already linked, {total_missing} campaign record(s) missing.'
        ))
        if not apply and total_updated:
            self.stdout.write('Re-run with --apply to write changes.')
