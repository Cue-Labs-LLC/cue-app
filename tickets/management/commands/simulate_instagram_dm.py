"""Demo shim: simulate one inbound Instagram DM through the full Phase 3 loop.

Builds a NormalizedInbound and runs process_instagram_inbound_task synchronously — the
same path a real signed webhook takes — then prints the resulting message status and
decision. With INSTAGRAM_SENDER_BACKEND='stub' (the default) nothing is sent to Meta, so
this works end-to-end before App Review.

    python manage.py simulate_instagram_dm --org familiar-faces --text "when do doors open?"
"""

from django.core.management.base import BaseCommand, CommandError

from tickets.models import InstagramConversation, InstagramMessage, Organization
from tickets.tasks import process_instagram_inbound_task


class Command(BaseCommand):
    help = "Simulate an inbound Instagram DM through the Phase 3 pipeline (no Meta send)."

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True, help='Organization slug.')
        parser.add_argument('--text', required=True, help='The customer DM text.')
        parser.add_argument('--sender-id', default='sim-user-1',
                            help='Simulated IG sender id (the customer). Default sim-user-1.')

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(slug=options['org'])
        except Organization.DoesNotExist:
            raise CommandError(f"No organization with slug '{options['org']}'.")

        if not org.instagram_support_agent_enabled:
            self.stdout.write(self.style.WARNING(
                "instagram_support_agent_enabled is False for this org — the task will "
                "no-op. Enable it to exercise the pipeline."
            ))

        sender_id = options['sender_id']
        import uuid
        normalized = {
            'ig_account_id': org.instagram_business_account_id or 'sim-account',
            'sender_id': sender_id,
            'text': options['text'],
            'provider_message_id': f"sim-{uuid.uuid4().hex[:24]}",
            'timestamp': 0,
        }

        # Run inline (eager) so we can read the result right after.
        process_instagram_inbound_task.apply(args=[str(org.id), normalized])

        conv = InstagramConversation.objects.filter(
            organization=org, ig_user_id=sender_id,
        ).first()
        if conv is None:
            self.stdout.write(self.style.ERROR("No conversation created (task no-op?)."))
            return

        outbound = (
            InstagramMessage.objects
            .filter(conversation=conv, direction=InstagramMessage.DIRECTION_OUTBOUND)
            .order_by('-created_at')
            .first()
        )
        self.stdout.write(self.style.MIGRATE_HEADING(f"Q: {options['text']}"))
        self.stdout.write(f"conversation: {conv.status}")
        if outbound is None:
            self.stdout.write(self.style.WARNING("No reply produced."))
            return
        self.stdout.write(f"reply status: {outbound.status}")
        self.stdout.write(f"category:     {outbound.escalation_category or '-'} "
                          f"(confidence {outbound.confidence})")
        self.stdout.write(f"reply:        {outbound.content}")
