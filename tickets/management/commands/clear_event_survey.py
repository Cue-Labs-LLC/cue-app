"""Management command to remove all survey invitations/responses for an event.

Resets an event so you can re-test the survey send flow. Deleting the
SurveyInvitation rows is what matters: the recipient list (and the
unique (event, customer) constraint) excludes any customer who already has an
invitation, regardless of whether it was sent. Invitation deletes cascade to
SurveyResponse -> SurveyAnswer -> SurveyAnswerOption.

The event's SurveyQuestion definitions are NOT touched.
"""

from django.core.management.base import BaseCommand, CommandError

from tickets.models import (
    Event,
    SurveyAnswer,
    SurveyInvitation,
    SurveyResponse,
)


class Command(BaseCommand):
    help = (
        'Delete all SurveyInvitations (and their cascaded SurveyResponses / '
        'SurveyAnswers) for a given event, so survey sends can be re-tested. '
        'Survey questions are left intact.'
    )

    def add_arguments(self, parser):
        parser.add_argument('event_id', help='UUID of the event to clear.')
        parser.add_argument(
            '--org-slug',
            dest='org_slug',
            default=None,
            help='Optional org slug to confirm the event belongs to the expected org.',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            default=False,
            help='Write changes to the database (default: dry-run).',
        )

    def handle(self, *args, **options):
        event_id = options['event_id']
        org_slug = options['org_slug']
        apply = options['apply']

        try:
            qs = Event.objects.select_related('organization')
            if org_slug:
                qs = qs.filter(organization__slug=org_slug)
            event = qs.get(pk=event_id)
        except Event.DoesNotExist:
            raise CommandError(
                f'Event "{event_id}" not found'
                + (f' under org "{org_slug}"' if org_slug else '') + '.'
            )

        invitation_count = SurveyInvitation.objects.filter(event=event).count()
        response_count = SurveyResponse.objects.filter(event=event).count()
        answer_count = SurveyAnswer.objects.filter(response__event=event).count()

        self.stdout.write(f'Event      : {event.name} ({event.pk})')
        self.stdout.write(f'Org        : {event.organization.name} ({event.organization.slug})')
        self.stdout.write(f'Invitations: {invitation_count}')
        self.stdout.write(f'Responses  : {response_count} (Answers cascade-deleted automatically)')
        self.stdout.write(f'Answers    : {answer_count}')

        if not apply:
            self.stdout.write(self.style.WARNING(
                '\nDRY-RUN — no changes saved. Re-run with --apply to delete.'
            ))
            return

        # Deleting invitations cascades to responses, answers, and answer options.
        SurveyInvitation.objects.filter(event=event).delete()

        self.stdout.write(self.style.SUCCESS(
            f'\nDeleted {invitation_count} invitation(s) and {response_count} response(s) '
            f'for event "{event.name}". You can now re-send the survey.'
        ))
