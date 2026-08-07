from django.db.utils import OperationalError
from .models import Company


def global_stats(request):
    try:
        return {"global_company_count": Company.objects.count()}
    except OperationalError:
        return {"global_company_count": 0}
