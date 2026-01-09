"""
Management command to create an initial superuser from environment variables.

Usage:
    python manage.py create_initial_superuser

This command creates a superuser if:
1. No superusers exist in the database
2. Environment variables are set: INITIAL_SUPERUSER_USERNAME and INITIAL_SUPERUSER_PASSWORD

Set these environment variables in Render:
- INITIAL_SUPERUSER_USERNAME: The username for the initial superuser
- INITIAL_SUPERUSER_PASSWORD: The password for the initial superuser
- INITIAL_SUPERUSER_EMAIL: (Optional) Email address for the superuser

After creating the superuser, you can remove these environment variables for security.
"""

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
import os

User = get_user_model()


class Command(BaseCommand):
    help = 'Create an initial superuser from environment variables'

    def handle(self, *args, **options):
        # Check if any superusers already exist
        if User.objects.filter(is_superuser=True).exists():
            self.stdout.write(
                self.style.WARNING('Superuser already exists. Skipping creation.')
            )
            return

        # Get credentials from environment variables
        username = os.environ.get('INITIAL_SUPERUSER_USERNAME')
        password = os.environ.get('INITIAL_SUPERUSER_PASSWORD')
        email = os.environ.get('INITIAL_SUPERUSER_EMAIL', '')

        if not username or not password:
            self.stdout.write(
                self.style.ERROR(
                    'Error: INITIAL_SUPERUSER_USERNAME and INITIAL_SUPERUSER_PASSWORD '
                    'environment variables must be set.'
                )
            )
            self.stdout.write(
                self.style.WARNING(
                    'Set these in Render dashboard → Environment → Environment Variables'
                )
            )
            return

        # Create superuser
        try:
            User.objects.create_superuser(
                username=username,
                email=email,
                password=password
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f'Successfully created superuser: {username}'
                )
            )
            self.stdout.write(
                self.style.WARNING(
                    'IMPORTANT: Remove INITIAL_SUPERUSER_* environment variables '
                    'from Render dashboard after first login for security!'
                )
            )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Error creating superuser: {str(e)}')
            )
