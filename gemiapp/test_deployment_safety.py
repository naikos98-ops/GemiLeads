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
