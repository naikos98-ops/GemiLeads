from pathlib import Path
import os
import dj_database_url
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SENTRY_DSN = os.environ.get("SENTRY_DSN")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        traces_sample_rate=1.0,
        send_default_pii=True
    )

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-change-me-gemi-leads")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h.strip()]
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "DJANGO_CSRF_TRUSTED_ORIGINS",
        ",".join(f"https://{host}" for host in ALLOWED_HOSTS if host not in ("127.0.0.1", "localhost")),
    ).split(",")
    if origin.strip()
]

# Serving over HTTPS is assumed in production; everything here is a no-op while DEBUG is on.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "1") == "1"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", 60 * 60 * 24 * 365))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_HTTPONLY = True
    X_FRAME_OPTIONS = "DENY"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sitemaps",
    "django_q",
    "gemiapp",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "gemiapp.context_processors.global_stats",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# The default LocMemCache is per-process. Under gunicorn that silently multiplies every
# rate limit by the worker count, because django-ratelimit counts attempts in the cache:
# with N workers an attacker gets N times the allowance. The database cache is consistent
# across workers, needs no extra service, and the volume here is a handful of counters and
# three cached aggregates. Swap in Redis if write contention ever shows up; it will not at
# this scale. Requires: python manage.py createcachetable (run by the deploy).
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "gemi_cache",
        "TIMEOUT": 300,
        "OPTIONS": {"MAX_ENTRIES": 5000, "CULL_FREQUENCY": 3},
    }
}

# The only accounts allowed to hold superuser rights. Enforced on every superadmin
# request, so a row edited directly in the database does not grant access, and kept in
# settings rather than the database so it cannot be changed through the web interface.
SUPERADMIN_EMAILS = [
    email.strip().lower()
    for email in os.environ.get(
        "SUPERADMIN_EMAILS",
        "info@gemileads.gr,naikos98@gmail.com,iotellis@taxville.gr",
    ).split(",")
    if email.strip()
]

# Fail closed: if the cache is unreachable the limiter must refuse, never wave traffic through.
RATELIMIT_USE_CACHE = "default"
RATELIMIT_FAIL_OPEN = False

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "el"
TIME_ZONE = "Europe/Athens"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# Hashed filenames make a long immutable cache safe: a changed file gets a new URL, so
# Cloudflare can edge-cache instead of revalidating against the origin every 60 seconds.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
WHITENOISE_MAX_AGE = 31536000
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "home"
LOGIN_URL = "login"

# Email settings. Production (DJANGO_DEBUG=0) sends real mail through the configured SMTP
# relay (Brevo). Non-production defaults to the console backend so a local run or a stray
# script can never send real mail just because valid SMTP credentials sit in .env; set
# EMAIL_BACKEND explicitly to opt back into a real backend (e.g. to test SMTP locally).
def resolve_email_backend(debug: bool, override: str | None) -> str:
    if override:
        return override
    return "django.core.mail.backends.console.EmailBackend" if debug else "django.core.mail.backends.smtp.EmailBackend"


EMAIL_BACKEND = resolve_email_backend(DEBUG, os.getenv("EMAIL_BACKEND"))
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp-relay.brevo.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() == "true"
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "Gemi Leads <notifications@send.gemileads.gr>")
EMAIL_REPLY_TO = os.getenv("EMAIL_REPLY_TO", "info@gemileads.gr")

# Default recipient for the "Δοκιμαστική αποστολή" button on the Εύρεση Πελατών page.
OUTREACH_TEST_EMAIL = os.getenv("OUTREACH_TEST_EMAIL", "naikos98@gmail.com")

# Max cold-outreach emails per rolling 24h. Past the Brevo plan's daily quota the SMTP relay
# accepts the message (250 OK) and silently drops it, so send() succeeds and the row would be
# wrongly marked "sent". process_pending_outreach stops here and leaves the rest "pending"
# for a follow-up run 24h later. Kept below the plan quota with headroom for digest emails,
# which draw on the same quota.
OUTREACH_DAILY_SEND_CAP = int(os.getenv("OUTREACH_DAILY_SEND_CAP", "250"))

# Shared secret Brevo sends back with every outbound webhook call (Brevo's own "Token-based
# authentication" option, not an HMAC signature) -- checked with a constant-time comparison in
# gemiapp.email_tracking.brevo_webhook. Empty means the endpoint refuses every delivery, the
# same fail-closed default as STRIPE_WEBHOOK_SECRET.
BREVO_WEBHOOK_TOKEN = os.getenv("BREVO_WEBHOOK_TOKEN", "")

# Stripe settings
STRIPE_PUBLIC_KEY = os.getenv("STRIPE_PUBLIC_KEY")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_PRO = os.getenv("STRIPE_PRICE_PRO")
STRIPE_PRICE_BUSINESS = os.getenv("STRIPE_PRICE_BUSINESS")
STRIPE_PRICE_ENTERPRISE = os.getenv("STRIPE_PRICE_ENTERPRISE")

# Legal / imprint details for the privacy policy and terms. Empty values keep both pages in
# an explicit draft state (banner shown, noindex) rather than publishing invented information.
LEGAL_CONTROLLER_NAME = os.getenv("LEGAL_CONTROLLER_NAME", "")
LEGAL_VAT = os.getenv("LEGAL_VAT", "")
LEGAL_GEMI = os.getenv("LEGAL_GEMI", "")
LEGAL_ADDRESS = os.getenv("LEGAL_ADDRESS", "")
LEGAL_CONTACT_EMAIL = os.getenv("LEGAL_CONTACT_EMAIL", EMAIL_REPLY_TO)
LEGAL_LAST_UPDATED = os.getenv("LEGAL_LAST_UPDATED", "22 Αυγούστου 2026")
LEGAL_REFUND_POLICY = os.getenv(
    "LEGAL_REFUND_POLICY",
    "Δεν προβλέπεται επιστροφή για το τρέχον διάστημα συνδρομής, εκτός αν ορίζει διαφορετικά ο νόμος.",
)
# Flip to "1" once real payments go live so the billing clauses replace the pre-launch notice.
# This is the single switch that governs whether the product can charge anyone: while it is off,
# checkout is refused server-side, not merely hidden. Keep it independent of BETA_MODE, because
# payments may go live before the beta label comes off, or the reverse.
LEGAL_BILLING_ACTIVE = os.getenv("LEGAL_BILLING_ACTIVE", "0") == "1"

# The product is in beta: the pipeline, the app and the emails are real, but the service is still
# being shaped and access is granted by hand. Shown as a badge in the navigation and a line in the
# footer so nobody mistakes it for a finished commercial launch. Set to "0" to drop the label.
BETA_MODE = os.getenv("BETA_MODE", "1") == "1"

# Google Analytics 4 measurement id (G-XXXXXXXXXX). Unset disables analytics entirely.
GA_MEASUREMENT_ID = os.getenv("GA_MEASUREMENT_ID", "")

# Google Search Console site verification. Set to the token value only (not the full tag).
# Leave unset to omit the meta tag entirely; DNS TXT verification needs nothing here.
GOOGLE_SITE_VERIFICATION = os.getenv("GOOGLE_SITE_VERIFICATION", "")

GEMI_API_KEY = os.environ.get("GEMI_API_KEY", "")
GEMI_API_BASE = "https://opendata-api.businessportal.gr/api/opendata/v1"

Q_CLUSTER = {
    "name": "gemi_leads_cluster",
    "workers": 2,
    "recycle": 100,
    # retry must stay comfortably above timeout, otherwise a slow task is re-queued while it is
    # still running and the queue grows without bound.
    "timeout": 1800,
    "retry": 2400,
    "compress": True,
    "save_limit": 50,
    "queue_limit": 50,
    "cpu_affinity": 1,
    "label": "Django Q",
    "orm": "default",
    # Skip slots missed during downtime instead of replaying them as a burst of emails.
    "catch_up": False,
}

AUTHENTICATION_BACKENDS = [
    "gemiapp.backends.EmailOrUsernameBackend",
]
