from django.core.management.base import BaseCommand

from tickets.models import Organization
from tickets.tasks import generate_org_ai_opportunities_task


class Command(BaseCommand):
    help = "Generate reviewed AI recommendations for organizations."

    def add_arguments(self, parser):
        parser.add_argument(
            '--organization-id',
            dest='organization_id',
            help='Generate recommendations for a single organization UUID.',
        )
        parser.add_argument(
            '--sync',
            action='store_true',
            help='Run inline instead of enqueueing Celery tasks.',
        )

    def handle(self, *args, **options):
        qs = Organization.objects.all().order_by('name')
        if options.get('organization_id'):
            qs = qs.filter(id=options['organization_id'])

        count = 0
        for org in qs.iterator():
            count += 1
            if options['sync']:
                generated = generate_org_ai_opportunities_task(str(org.id))
                self.stdout.write(f'{org.name}: generated/updated {generated}')
            else:
                generate_org_ai_opportunities_task.delay(str(org.id))
                self.stdout.write(f'{org.name}: queued')

        self.stdout.write(self.style.SUCCESS(f'Processed {count} organization(s).'))
