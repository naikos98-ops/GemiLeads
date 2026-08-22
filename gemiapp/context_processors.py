from django.core.cache import cache
from django.db.utils import OperationalError
from .models import Company


def global_stats(request):
    # Runs on every request site-wide. An exact count is not needed for a marketing
    # figure, so it is cached for an hour to keep the query off the critical path.
    try:
        return {
            "global_company_count": cache.get_or_set(
                "global_company_count", lambda: Company.objects.count(), 3600
            )
        }
    except OperationalError:
        return {"global_company_count": 0}
