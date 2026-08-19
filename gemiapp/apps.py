from django.apps import AppConfig
from django.db.models.signals import post_migrate


def setup_daily_pipeline_schedule(sender, **kwargs):
    """
    Ensures that the daily & intraday GEMI pipeline & email digest tasks are registered
    in the django-q Schedule table after migrations complete.
    """
    try:
        from django_q.models import Schedule
        # 1. Daily Pipeline Schedule
        Schedule.objects.get_or_create(
            func="gemiapp.tasks.run_daily_pipeline_task",
            defaults={
                "name": "Daily GEMI Import & Digest",
                "schedule_type": Schedule.DAILY,
                "repeats": -1,
            },
        )
        # 2. Intraday 3-Hour Pipeline Schedule (08:00 - 00:00) for Top Tier subscribers
        Schedule.objects.get_or_create(
            func="gemiapp.tasks.run_intraday_pipeline_task",
            defaults={
                "name": "Intraday 3-Hour GEMI Pipeline (Top Tier)",
                "schedule_type": Schedule.CRON,
                "cron": "0 8,11,14,17,20,23 * * *",
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
