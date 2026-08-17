from django.apps import AppConfig


class GemiappConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'gemiapp'

    def ready(self):
        try:
            from django_q.models import Schedule
            from datetime import time
            Schedule.objects.get_or_create(
                func="gemiapp.tasks.run_daily_pipeline_task",
                defaults={
                    "schedule_type": Schedule.DAILY,
                    "repeats": -1,
                }
            )
        except Exception:
            pass
