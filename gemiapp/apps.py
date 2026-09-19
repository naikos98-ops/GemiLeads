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
    # Outreach left "pending" by the daily cap used to be drained by a ONCE schedule the
    # sending task created for its own batch. That stranded rows two ways: the follow-up only
    # ever retried the ids of the batch that scheduled it, and it was only created when that
    # batch itself hit the cap -- so a backlog sat untouched until some *later* batch
    # overflowed and dragged part of it along. This sweeps every pending row, no matter which
    # batch it came from. Runs before the outreach working day so a full cap is available.
    {
        "func": "gemiapp.tasks.drain_pending_outreach_task",
        "name": "Drain Pending Outreach",
        "cron": "37 7 * * *",
    },
    # D37 (the one approved exception to the scheduling freeze): in-app TASK_DUE notifications, every day at 08:00
    # in the configured time zone (Europe/Athens). Only creates notification rows; sends nothing.
    # "requires_schema": registered only while that check passes (its table exists, 0054+). post_migrate also runs
    # after a rollback below 0054, and then removes the row instead of leaving a daily job for a missing table. The
    # task keeps its own check too.
    {
        "func": "gemiapp.tasks.generate_task_due_notifications_task",
        "name": "Task Due Notifications",
        "cron": "0 8 * * *",
        "requires_schema": "gemiapp.notifications.notification_schema_ready",
    },
]


def setup_daily_pipeline_schedule(sender, **kwargs):
    """
    Registers the daily & intraday GEMI pipeline tasks in the django-q Schedule table after
    migrations complete, and keeps an existing row in sync when the definition above changes.

    Duplicate rows for the same func are removed first. A duplicate makes update_or_create raise
    MultipleObjectsReturned, which previously aborted the whole registration: production ended up
    running the daily pipeline twice concurrently while the intraday schedule was never created.

    An entry with "requires_schema" is schema-aware: exactly one row while that check (database
    introspection, never a query on the model) passes, none otherwise, so this holds at any migration
    state. Entries without it are registered exactly as before.
    """
    try:
        from django.utils.module_loading import import_string
        from django_q.models import Schedule

        for entry in SCHEDULES:
            if "requires_schema" in entry:
                if not import_string(entry["requires_schema"])():
                    removed = Schedule.objects.filter(func=entry["func"]).delete()[0]
                    if removed:
                        logger.info("Removed the %s schedule: its table does not exist", entry["func"])
                    continue

            duplicates = list(
                Schedule.objects.filter(func=entry["func"]).order_by("id").values_list("id", flat=True)
            )
            if len(duplicates) > 1:
                Schedule.objects.filter(id__in=duplicates[1:]).delete()
                logger.warning(
                    "Removed %s duplicate schedule row(s) for %s", len(duplicates) - 1, entry["func"]
                )

            defaults = {
                "name": entry["name"],
                "schedule_type": Schedule.CRON,
                "cron": entry["cron"],
                "repeats": -1,
            }
            unsaved = Schedule(schedule_type=Schedule.CRON, cron=entry["cron"])
            Schedule.objects.update_or_create(
                func=entry["func"],
                defaults=defaults,
                # Only on creation: start at the next real cron slot rather than firing
                # immediately, so a deploy never triggers an unexpected send.
                create_defaults={**defaults, "next_run": unsaved.calculate_next_run()},
            )
    except Exception:
        logger.exception("Could not register the GEMI pipeline schedules")


class GemiappConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "gemiapp"

    def ready(self):
        post_migrate.connect(setup_daily_pipeline_schedule, sender=self)
