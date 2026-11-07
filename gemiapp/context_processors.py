from django.conf import settings
from django.core.cache import cache
from django.db.utils import OperationalError
from .campaigns import pop_queued_analytics_events
from .models import Company


# Public marketing pages only. The Pixel sends Meta the full URL, so it stays off account pages and
# off the password-reset, verification and unsubscribe links, whose URLs carry tokens.
PIXEL_PAGES = frozenset({"home", "pricing", "landing_horeca", "landing_insurance", "signup"})


def _pixel_on_page(request):
    if not settings.META_PIXEL_ID:
        return False
    match = getattr(request, "resolver_match", None)
    if match is None or match.url_name not in PIXEL_PAGES:
        return False
    user = getattr(request, "user", None)
    return not (user and user.is_authenticated)


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
        "meta_pixel_id": settings.META_PIXEL_ID,
        "meta_pixel_on_page": _pixel_on_page(request),
        # Site-wide because both facts have to be visible on every page, including the ones a
        # visitor lands on directly. Read from settings on each request so flipping the env var
        # takes effect on redeploy without touching a template.
        "beta_mode": settings.BETA_MODE,
        "billing_active": settings.LEGAL_BILLING_ACTIVE,
        # The product contact address, shown to signed-in users in the rail, Settings and the «Βοήθεια» form.
        "support_email": settings.EMAIL_REPLY_TO,
        # One-shot conversion events queued by a server-side action (gemiapp.campaigns). Emitted by
        # includes/analytics.html only when gtag exists, i.e. only with analytics consent.
        "queued_analytics_events": pop_queued_analytics_events(request) if settings.GA_MEASUREMENT_ID else [],
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


def radar_nudge(request):
    """Highlight «Radars» for an account that may run Radars (paid or complimentary) but has no active Radar with
    criteria (the automatic «Όλες οι νέες επιχειρήσεις» does not count — see ``has_targeted_radar``).

    Approved exception to the Signal Ledger (Founder 2026-11-06): the purple «galaxy» item in the rail and
    the mobile nav, plus a notice on the dashboard. Disappears with the first active Radar. No queries for
    anonymous requests; two small ones otherwise.
    """
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated):
        return {"radar_nudge": False}
    from .models import UserSubscription, has_targeted_radar

    try:
        sub = UserSubscription.objects.filter(user=user).first()
        if sub is None or sub.radar_limit <= 0:
            return {"radar_nudge": False}
        has_radar = has_targeted_radar(user)
    except OperationalError:
        return {"radar_nudge": False}
    return {"radar_nudge": not has_radar}
