from django.apps import AppConfig
from django.db.models.signals import post_migrate


def setup_daily_pipeline_schedule(sender, **kwargs):
    """
    Ensures that the daily GEMI pipeline & email digest task is registered
    in the django-q Schedule table after migrations complete.
    """
    try:
        from django_q.models import Schedule
        Schedule.objects.get_or_create(
            func="gemiapp.tasks.run_daily_pipeline_task",
            defaults={
                "name": "Daily GEMI Import & Digest",
                "schedule_type": Schedule.DAILY,
                "repeats": -1,
            },
        )
    except Exception:
        pass


class GemiappConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "gemiapp"

    def ready(self):
        post_migrate.connect(setup_daily_pipeline_schedule, sender=self)
