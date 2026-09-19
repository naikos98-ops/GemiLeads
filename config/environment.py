"""Release readiness (G0): which deployment this is, and what it may touch.

``GEMI_LEADS_ENVIRONMENT`` is ``production`` (the default when unset, so production behaviour never changes),
``staging`` or ``development``. A staging deployment is fail-safe by construction:

* **GEMI** -- its key is read only from ``GEMI_STAGING_API_KEY``, never from ``GEMI_API_KEY``. Without a staging key
  the collector is disabled and every GEMI request is refused before anything is sent, so staging can never spend the
  production key's budget (8 requests/minute upstream, 7 used by production) even if the production variables were
  copied over.
* **Email** -- the backend is ``STAGING_EMAIL_BACKEND`` or the console backend; the SMTP relay and the email
  provider's API key are never inherited, so no customer can be emailed from staging.
* **Outreach** -- forced off.
* **Stripe** -- a live secret key refuses to start; only test-mode keys may be configured.

Nothing here reads the network or prints a secret.
"""

from dataclasses import dataclass

from django.core.exceptions import ImproperlyConfigured

PRODUCTION, STAGING, DEVELOPMENT = "production", "staging", "development"
ENVIRONMENTS = frozenset({PRODUCTION, STAGING, DEVELOPMENT})
STAGING_DEFAULT_EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
LIVE_STRIPE_KEY_PREFIXES = ("sk_live_", "rk_live_")


@dataclass(frozen=True)
class DeploymentSafety:
    environment: str
    gemi_api_key: str
    gemi_collector_enabled: bool
    email_backend: str | None       # a forced backend (staging), or None to keep the normal resolution
    email_provider_api_key: str
    outreach_forced_off: bool


def deployment_safety(environ) -> DeploymentSafety:
    """The settings a deployment may use, derived from its environment variables only."""
    environment = (environ.get("GEMI_LEADS_ENVIRONMENT") or PRODUCTION).strip().lower()
    if environment not in ENVIRONMENTS:
        raise ImproperlyConfigured(f"GEMI_LEADS_ENVIRONMENT must be one of {sorted(ENVIRONMENTS)}")
    if environment != STAGING:
        return DeploymentSafety(
            environment=environment, gemi_api_key=environ.get("GEMI_API_KEY", ""), gemi_collector_enabled=True,
            email_backend=None, email_provider_api_key=environ.get("BREVO_API_KEY", ""), outreach_forced_off=False)
    if (environ.get("STRIPE_SECRET_KEY") or "").startswith(LIVE_STRIPE_KEY_PREFIXES):
        raise ImproperlyConfigured("A staging deployment must not be configured with a live Stripe secret key.")
    staging_key = environ.get("GEMI_STAGING_API_KEY", "")
    return DeploymentSafety(
        environment=STAGING, gemi_api_key=staging_key, gemi_collector_enabled=bool(staging_key),
        email_backend=environ.get("STAGING_EMAIL_BACKEND") or STAGING_DEFAULT_EMAIL_BACKEND,
        email_provider_api_key=environ.get("STAGING_BREVO_API_KEY", ""), outreach_forced_off=True)
