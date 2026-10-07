"""Demo shim: run the Instagram support agent's answer pipeline over a question.

No messages are sent — this drafts a reply, classifies escalation, and prints the
auto-send-vs-queue decision. Useful before the inbound transport (webhook/inbox) exists.

    python manage.py answer_ig_faq --org familiar-faces --question "when do doors open?"
"""

from django.core.management.base import BaseCommand, CommandError

from tickets.models import Organization
from tickets.services.instagram import (
    InstagramAgentError,
    InstagramSupportAgentService,
    classify_escalation,
    decide_autosend,
)


class Command(BaseCommand):
    help = "Run the Instagram support agent over a question and print its decision."

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True, help='Organization slug.')
        parser.add_argument('--question', required=True, help='The customer DM text.')

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(slug=options['org'])
        except Organization.DoesNotExist:
            raise CommandError(f"No organization with slug '{options['org']}'.")

        question = options['question']
        try:
            service = InstagramSupportAgentService(org)
            result = service.answer(None, question)
            decision = classify_escalation(org, question, result.text)
        except InstagramAgentError as exc:
            raise CommandError(str(exc))

        auto_send = decide_autosend(decision, result)

        self.stdout.write(self.style.MIGRATE_HEADING(f"Q: {question}"))
        self.stdout.write(f"tools fired:  {', '.join(result.tool_calls) or '(none)'}")
        self.stdout.write(f"grounded:     {result.grounded}")
        self.stdout.write(f"escalate:     {decision.should_escalate} "
                          f"({decision.category}, confidence {decision.confidence})")
        self.stdout.write(f"reason:       {decision.reason or '-'}")
        verdict = 'AUTO-SEND' if auto_send else 'QUEUE FOR REVIEW'
        self.stdout.write(self.style.SUCCESS(f"decision:     {verdict}"))
        self.stdout.write(f"answer:       {result.text}")
