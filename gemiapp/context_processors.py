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
    }
