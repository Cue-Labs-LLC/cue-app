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
    """Authenticate attendees by phone number stored in UserProfile."""

    def authenticate(self, request, phone_number=None, **kwargs):
        if not phone_number:
            return None
        from .models import UserProfile
        try:
            profile = UserProfile.objects.select_related('user').get(phone_number=phone_number)
            return profile.user
        except UserProfile.DoesNotExist:
            return None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None


class EmailOTPBackend:
    """Authenticate users by email address after Twilio has already verified the OTP."""

    def authenticate(self, request, email=None, **kwargs):
        if not email:
            return None
        try:
            return User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            return None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
