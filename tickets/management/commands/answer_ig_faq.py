"""Demo shim: run the Instagram support agent's FAQ answerer over a question.

No messages are sent — this just calls answer_faq() and prints the decision,
useful before the inbound transport (webhook/inbox) exists.

    python manage.py answer_ig_faq --org familiar-faces --question "how do I reach a human?"
"""

from django.core.management.base import BaseCommand, CommandError

from tickets.models import Organization
from tickets.services.instagram import InstagramAgentError, answer_faq


class Command(BaseCommand):
    help = "Ask the Instagram support agent a question and print its FAQ answer."

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True, help='Organization slug.')
        parser.add_argument('--question', required=True, help='The customer DM text.')

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(slug=options['org'])
        except Organization.DoesNotExist:
            raise CommandError(f"No organization with slug '{options['org']}'.")

        try:
            result = answer_faq(org, question=options['question'])
        except InstagramAgentError as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.MIGRATE_HEADING(f"Q: {options['question']}"))
        self.stdout.write(f"answered:       {result.answered}")
        self.stdout.write(f"confidence:     {result.confidence}")
        self.stdout.write(f"matched_faq_id: {result.matched_faq_id or '-'}")
        self.stdout.write(self.style.SUCCESS(f"answer:         {result.answer}"))
