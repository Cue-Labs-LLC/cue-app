"""
Staging environment seed command.

Same demo data as `seed_local_data`, but gated on DJANGO_ENV=='staging' so it
can never accidentally run against production. Run from the Render shell on
the staging web service after the database is first provisioned:

    python manage.py seed_staging --force

Refuses to run unless DJANGO_ENV is exactly 'staging'.
"""
from django.core.management.base import CommandError

from tickets.management.commands.seed_local_data import Command as SeedLocalCommand
from django.conf import settings


class Command(SeedLocalCommand):
    help = "Seed the staging DB with demo data. Refuses to run outside DJANGO_ENV=staging."

    def _guard(self):
        if getattr(settings, "DJANGO_ENV", None) != "staging":
            raise CommandError(
                "Refusing to seed: DJANGO_ENV is not 'staging' "
                f"(got {getattr(settings, 'DJANGO_ENV', None)!r}). "
                "This command is only safe on the staging environment."
            )
