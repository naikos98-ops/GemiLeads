from django.conf import settings
from django.core.cache import cache
from django.db.utils import OperationalError
from .models import Company


def global_stats(request):
    # Runs on every request site-wide. An exact count is not needed for a marketing
    # figure, so it is cached for an hour to keep the query off the critical path.
    try:
        count = cache.get_or_set("global_company_count", lambda: Company.objects.count(), 3600)
    except OperationalError:
        count = 0
    return {
        "global_company_count": count,
        "google_site_verification": settings.GOOGLE_SITE_VERIFICATION,
        "ga_measurement_id": settings.GA_MEASUREMENT_ID,
        # Site-wide because both facts have to be visible on every page, including the ones a
        # visitor lands on directly. Read from settings on each request so flipping the env var
        # takes effect on redeploy without touching a template.
        "beta_mode": settings.BETA_MODE,
        "billing_active": settings.LEGAL_BILLING_ACTIVE,
    }


def social_login_providers(request):
    """Which social buttons to render.

    allauth's {% get_providers %} lists every provider in SOCIALACCOUNT_PROVIDERS whether or
    not it has credentials, so on an environment where only Google is set up it would still
    draw an Apple button that dead-ends on Apple's error page. Keying on the client_id keeps
    a half-finished setup invisible instead.
    """
    configured = {
        provider
        for provider, config in settings.SOCIALACCOUNT_PROVIDERS.items()
        if config.get("APP", {}).get("client_id")
    }
    return {
        "google_login_enabled": "google" in configured,
        "apple_login_enabled": "apple" in configured,
    }
