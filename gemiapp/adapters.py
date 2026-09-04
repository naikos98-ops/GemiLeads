"""allauth adapters.

Social login is bolted onto an app that already had its own signup, its own verification
email and its own first-run setup (a DigestPreference row and a starter radar). These
adapters keep allauth from growing a second, parallel version of any of that: it is allowed
to authenticate a Google/Apple identity and nothing else.
"""

import logging

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib.auth.models import User

logger = logging.getLogger(__name__)


class NoLocalSignupAdapter(DefaultAccountAdapter):
    """Blocks allauth's own email+password signup.

    The project's SignupForm owns that flow -- it sets username=email, leaves the account
    inactive and sends the verification email. Leaving allauth's version reachable would let
    someone create an active account that skipped all three.
    """

    def is_open_for_signup(self, request):
        return False


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    """Creates and links accounts for Google/Apple sign-ins."""

    def is_open_for_signup(self, request, sociallogin):
        # Social signup stays open -- this is the whole point of the feature. Only allauth's
        # local password signup is closed, in NoLocalSignupAdapter.
        return True

    def pre_social_login(self, request, sociallogin):
        """Attach the provider identity to the existing account with the same address.

        Without this, someone who signed up with a password and later clicks "Continue with
        Google" gets a second, empty account: same person, same address, none of their
        radars or leads. Both providers verify the address before releasing it, so matching
        on it is safe here in a way it would not be for an arbitrary OAuth provider.
        """
        if sociallogin.is_existing:
            return

        email = (sociallogin.user.email or "").strip().lower()
        if not email:
            return

        existing = User.objects.filter(email__iexact=email).first()
        if existing is None:
            return

        if not existing.is_active:
            # Signed up with a password but never clicked the verification link. The provider
            # has now vouched for the address, which is exactly what that link was for.
            existing.is_active = True
            existing.save(update_fields=["is_active"])
            logger.info("Activated unverified account %s via social login.", existing.pk)

        sociallogin.connect(request, existing)

    def populate_user(self, request, sociallogin, data):
        """Mirror SignupForm: the username is the address, not a provider handle."""
        user = super().populate_user(request, sociallogin, data)
        email = (user.email or "").strip().lower()
        if email:
            user.email = email
            user.username = email
        return user

    def save_user(self, request, sociallogin, form=None):
        """Give a brand-new social account the same first-run setup a form signup gets."""
        from .models import CustomerRadar, DigestPreference

        user = super().save_user(request, sociallogin, form)
        # The provider verified the address, so there is nothing left for the verification
        # email to prove -- unlike a form signup, this account starts active.
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["is_active"])
        DigestPreference.objects.get_or_create(user=user)
        if not CustomerRadar.objects.filter(user=user).exists():
            CustomerRadar.objects.create(user=user, name="Όλες οι νέες επιχειρήσεις")
        return user
