"""Release readiness (G0): deployment safety of config/environment.py, the GEMI collector switch and the D37
schedule's schema guard. No network: the GEMI transport is a stub that fails the test if it is ever called."""

from unittest.mock import Mock, patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings

from config.environment import STAGING_DEFAULT_EMAIL_BACKEND, deployment_safety
from gemiapp.ingestion.client import COLLECTOR_DISABLED_MESSAGE, GemiClient
from gemiapp.ingestion.errors import GemiConfigurationError

PROD_VARS = {"GEMI_API_KEY": "prod-gemi-key", "BREVO_API_KEY": "prod-brevo-key",
             "STRIPE_SECRET_KEY": "sk_live_xxx", "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend"}


class DeploymentSafetyTests(SimpleTestCase):
    def test_unset_means_production_and_nothing_changes(self):
        for environ in (PROD_VARS, {**PROD_VARS, "GEMI_LEADS_ENVIRONMENT": "production"},
                        {**PROD_VARS, "GEMI_LEADS_ENVIRONMENT": " Production "}):
            safety = deployment_safety(environ)
            self.assertEqual((safety.environment, safety.gemi_api_key, safety.gemi_collector_enabled,
                              safety.email_backend, safety.email_provider_api_key, safety.outreach_forced_off),
                             ("production", "prod-gemi-key", True, None, "prod-brevo-key", False))
        self.assertEqual(deployment_safety({"GEMI_API_KEY": "dev"}).gemi_api_key, "dev")
        self.assertEqual(deployment_safety({"GEMI_LEADS_ENVIRONMENT": "development"}).environment, "development")

    def test_an_unknown_environment_refuses_to_start(self):
        for bad in ("prod", "stage", "live", "test"):
            self.assertRaises(ImproperlyConfigured, deployment_safety, {"GEMI_LEADS_ENVIRONMENT": bad})

    def test_staging_never_inherits_the_production_gemi_key(self):
        copied = {**PROD_VARS, "STRIPE_SECRET_KEY": "sk_test_xxx", "GEMI_LEADS_ENVIRONMENT": "staging"}
        safety = deployment_safety(copied)
        self.assertEqual((safety.gemi_api_key, safety.gemi_collector_enabled), ("", False))
        with_own = deployment_safety({**copied, "GEMI_STAGING_API_KEY": "staging-gemi-key"})
        self.assertEqual((with_own.gemi_api_key, with_own.gemi_collector_enabled), ("staging-gemi-key", True))

    def test_staging_never_inherits_email_or_outreach(self):
        copied = {**PROD_VARS, "STRIPE_SECRET_KEY": "sk_test_xxx", "GEMI_LEADS_ENVIRONMENT": "staging"}
        safety = deployment_safety(copied)
        self.assertEqual((safety.email_backend, safety.email_provider_api_key, safety.outreach_forced_off),
                         (STAGING_DEFAULT_EMAIL_BACKEND, "", True))
        explicit = deployment_safety({**copied, "STAGING_EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
                                      "STAGING_BREVO_API_KEY": "staging-brevo"})
        self.assertEqual((explicit.email_backend, explicit.email_provider_api_key),
                         ("django.core.mail.backends.locmem.EmailBackend", "staging-brevo"))

    def test_staging_refuses_a_live_stripe_key(self):
        for live in ("sk_live_abc", "rk_live_abc"):
            self.assertRaises(ImproperlyConfigured, deployment_safety,
                              {"GEMI_LEADS_ENVIRONMENT": "staging", "STRIPE_SECRET_KEY": live})
        self.assertEqual(deployment_safety({"GEMI_LEADS_ENVIRONMENT": "staging",
                                            "STRIPE_SECRET_KEY": "sk_test_abc"}).environment, "staging")
        self.assertEqual(deployment_safety({"GEMI_LEADS_ENVIRONMENT": "staging"}).environment, "staging")

    def test_the_test_run_itself_is_production_shaped(self):
        self.assertEqual(settings.GEMI_LEADS_ENVIRONMENT, "production")
        self.assertTrue(settings.GEMI_COLLECTOR_ENABLED)


class CollectorSwitchTests(SimpleTestCase):
    def test_a_disabled_collector_sends_nothing_even_with_a_key(self):
        transport = Mock(side_effect=AssertionError("no request may be sent"))
        client = GemiClient(api_key="some-key", transport=transport, budget=Mock())
        with override_settings(GEMI_COLLECTOR_ENABLED=False):
            with self.assertRaises(GemiConfigurationError) as refused:
                client.get("/companies", {"resultsSize": 1}, lane="interactive")
        self.assertEqual(str(refused.exception), COLLECTOR_DISABLED_MESSAGE)
        transport.assert_not_called()


class TaskDueScheduleGuardTests(TestCase):
    def test_a_missing_notification_table_is_a_logged_skip_not_a_failing_task(self):
        from gemiapp import tasks

        with patch("gemiapp.notifications.notification_schema_ready", return_value=False), \
                patch("gemiapp.notifications.generate_due_task_notifications") as generator, \
                self.assertLogs("gemiapp.tasks", level="WARNING"):
            self.assertEqual(tasks.generate_task_due_notifications_task(), {"skipped": "notification_schema_missing"})
        generator.assert_not_called()
        from gemiapp.notifications import notification_schema_ready

        self.assertTrue(notification_schema_ready())
        self.assertEqual(tasks.generate_task_due_notifications_task()["created"], 0)


D37_FUNC = "gemiapp.tasks.generate_task_due_notifications_task"
LEGACY_FUNCS = {"gemiapp.tasks.run_daily_pipeline_task", "gemiapp.tasks.run_intraday_pipeline_task",
                "gemiapp.tasks.drain_pending_outreach_task"}


class TaskDueScheduleLifecycleTests(TestCase):
    """The D37 schedule row exists exactly while its table does: one row at 0054+, none below it (after a rollback,
    post_migrate removes it). The three legacy schedules are registered exactly as before in every state."""

    def setUp(self):
        from django_q.models import Schedule

        Schedule.objects.all().delete()

    def register(self, *, table_exists):
        from django.db import connection

        from gemiapp.apps import setup_daily_pipeline_schedule
        from gemiapp.models import OrganizationNotification

        tables = [name for name in connection.introspection.table_names()
                  if table_exists or name != OrganizationNotification._meta.db_table]
        with patch.object(connection.introspection, "table_names", return_value=tables):
            setup_daily_pipeline_schedule(None)

    def d37_count(self):
        from django_q.models import Schedule

        return Schedule.objects.filter(func=D37_FUNC).count()

    def legacy_rows(self):
        from django_q.models import Schedule

        return {row["func"]: row for row in Schedule.objects.filter(func__in=LEGACY_FUNCS).values(
            "id", "func", "name", "schedule_type", "cron", "repeats", "next_run")}

    def test_pre_0054_schema_registers_no_task_due_schedule_and_every_legacy_one(self):
        self.register(table_exists=False)
        self.assertEqual(self.d37_count(), 0)
        self.assertEqual(set(self.legacy_rows()), LEGACY_FUNCS)

    def test_0054_schema_registers_exactly_one_and_repeated_registration_keeps_one(self):
        self.register(table_exists=True)
        self.assertEqual(self.d37_count(), 1)
        for _ in range(3):
            self.register(table_exists=True)
        self.assertEqual(self.d37_count(), 1)

    def test_a_duplicate_task_due_row_is_repaired_to_one(self):
        from django_q.models import Schedule

        self.register(table_exists=True)
        Schedule.objects.create(func=D37_FUNC, schedule_type=Schedule.CRON, cron="0 8 * * *", repeats=-1)
        self.register(table_exists=True)
        self.assertEqual(self.d37_count(), 1)

    def test_rollback_below_0054_removes_the_row_and_reapply_restores_exactly_one(self):
        from django_q.models import Schedule

        self.register(table_exists=True)
        Schedule.objects.create(func=D37_FUNC, schedule_type=Schedule.CRON, cron="0 8 * * *", repeats=-1)
        legacy = self.legacy_rows()
        self.register(table_exists=False)  # post_migrate after migrate gemiapp 0053 / 0031
        self.assertEqual(self.d37_count(), 0)
        self.register(table_exists=False)  # repeated post_migrate below 0054
        self.assertEqual(self.d37_count(), 0)
        self.assertEqual(self.legacy_rows(), legacy, "legacy schedule rows must be untouched by the rollback")
        self.register(table_exists=True)  # reapply 0054
        self.assertEqual(self.d37_count(), 1)
        self.assertEqual(self.legacy_rows(), legacy)
        self.assertEqual(Schedule.objects.count(), 4)

    def test_the_real_schema_at_head_registers_it(self):
        from gemiapp.apps import setup_daily_pipeline_schedule

        setup_daily_pipeline_schedule(None)
        self.assertEqual(self.d37_count(), 1)
