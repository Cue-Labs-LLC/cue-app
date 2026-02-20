from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.hashers import make_password

User = get_user_model()


class EmailBackend(ModelBackend):
    """Authenticate by email (case-insensitive) only — no username fallback."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None

        try:
            user = User.objects.get(email__iexact=username)
        except User.DoesNotExist:
            # Run dummy hasher to prevent timing attacks
            make_password(password)
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None


class PhoneBackend:
    """Authenticate attendees by phone number (stored as username on auth.User)."""

    def authenticate(self, request, phone_number=None, **kwargs):
        if not phone_number:
            return None
        try:
            return User.objects.get(username=phone_number)
        except User.DoesNotExist:
            return None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
