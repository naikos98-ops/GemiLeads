import logging

from django.apps import AppConfig
from django.db.models.signals import post_migrate

logger = logging.getLogger(__name__)


# Both pipelines are declared as cron expressions so their wall-clock times are explicit and
# survive a redeploy. django-q2 needs the optional croniter package for these; it is pinned in
# requirements.txt. Without it the scheduler raises inside its transaction and silently rolls
# back every schedule in the same pass, which stops the daily digest as well.
SCHEDULES = [
    {
        "func": "gemiapp.tasks.run_daily_pipeline_task",
        "name": "Daily GEMI Import & Digest",
        "cron": "0 9 * * *",
    },
    {
        "func": "gemiapp.tasks.run_intraday_pipeline_task",
        "name": "Intraday 3-Hour GEMI Pipeline (Top Tier)",
        "cron": "0 8,11,14,17,20,23 * * *",
    },
]


def setup_daily_pipeline_schedule(sender, **kwargs):
    """
    Registers the daily & intraday GEMI pipeline tasks in the django-q Schedule table after
    migrations complete, and keeps an existing row in sync when the definition above changes.
    """
    try:
        from django_q.models import Schedule

        for entry in SCHEDULES:
            defaults = {
                "name": entry["name"],
                "schedule_type": Schedule.CRON,
                "cron": entry["cron"],
                "repeats": -1,
            }
            schedule = Schedule(schedule_type=Schedule.CRON, cron=entry["cron"])
            Schedule.objects.update_or_create(
                func=entry["func"],
                defaults=defaults,
                # Only on creation: start at the next real cron slot rather than firing
                # immediately, so a deploy never triggers an unexpected send.
                create_defaults={**defaults, "next_run": schedule.calculate_next_run()},
            )
    except Exception:
        logger.exception("Could not register the GEMI pipeline schedules")


class GemiappConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "gemiapp"

    def ready(self):
        post_migrate.connect(setup_daily_pipeline_schedule, sender=self)
