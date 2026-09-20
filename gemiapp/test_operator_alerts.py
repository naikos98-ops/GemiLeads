"""Tests for active operator alerting on GEMI ingestion failures (config/operator_alerts.py).

The requirement is not "run Sentry": it is that a production ERROR from the ingestion path actively notifies an
operator. Everything else that records such a failure -- Render's stderr, ``ImportRun.status``, django-q's
failure row -- is passive. These tests pin the routing, the three ways it must stay quiet (not production, not
the right logger, no recipients), and the rule that a failing alert can never change what the ingestion code was
doing.

No real email is sent: the suite uses the locmem backend, and the test runner empties ``ADMINS`` for the whole
run, so a test that wants the routing has to ask for it with ``override_settings``.
"""

import contextlib
import copy
import io
import json
import logging
from datetime import date, timedelta
from unittest.mock import patch

from django.core import mail
from django.test import SimpleTestCase, TestCase, override_settings

from config import fast_test_runner, settings as settings_module
from config.operator_alerts import OperatorEmailHandler

from .ingestion.errors import GemiResponseValidationError
from .models import ImportRun
from .test_gemi_client import make_client
from .test_gemi_validation import full_item, page, routed

OPERATORS = [("Gemi Leads operator", "operator@example.com")]
CLIENT_LOGGER = "gemiapp.ingestion.client"
SERVICES_LOGGER = "gemiapp.services"


def console_handlers():
    """The configured stderr handlers of the two alerting loggers. They hold a direct reference to the stream
    they were built with, so redirecting ``sys.stderr`` alone would not capture them."""
    return [handler
            for name in settings_module.OPERATOR_ALERT_LOGGERS
            for handler in logging.getLogger(name).handlers
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, OperatorEmailHandler)]


@contextlib.contextmanager
def captured_stderr():
    """Everything that would reach the log stream: the configured console handlers, ``logging.lastResort``
    (which reads ``sys.stderr`` when a logger has none) and ``Handler.handleError``."""
    buffer = io.StringIO()
    handlers = console_handlers()
    originals = [handler.stream for handler in handlers]
    for handler in handlers:
        handler.setStream(buffer)
    try:
        with contextlib.redirect_stderr(buffer):
            yield buffer
    finally:
        for handler, stream in zip(handlers, originals):
            handler.setStream(stream)


def emit(logger_name, level=logging.ERROR, message="ingestion failure marker"):
    """Log one record the way the ingestion path does, returning what reached the log stream."""
    with captured_stderr() as buffer:
        logging.getLogger(logger_name).log(level, message)
    return buffer.getvalue()


class RecipientTests(SimpleTestCase):
    """ADMINS comes from the configured operator addresses, and from nothing else."""

    def test_admins_are_derived_from_superadmin_emails(self):
        derived = settings_module.operator_admins(settings_module.SUPERADMIN_EMAILS)
        self.assertTrue(settings_module.SUPERADMIN_EMAILS)              # the deployment configures operators
        self.assertEqual([email for _, email in derived], settings_module.SUPERADMIN_EMAILS)
        self.assertEqual({name for name, _ in derived}, {"Gemi Leads operator"})

    def test_the_settings_module_uses_that_derivation(self):
        self.assertEqual(settings_module.ADMINS,
                         settings_module.operator_admins(settings_module.SUPERADMIN_EMAILS))

    def test_no_address_is_written_into_the_logging_configuration(self):
        # Recipients reach the handler only through ADMINS; nothing addressable is spelled out in LOGGING.
        self.assertNotIn("@", json.dumps(settings_module.LOGGING, default=str))
        self.assertIn("ADMINS = operator_admins(SUPERADMIN_EMAILS)",
                      open("config/settings.py", encoding="utf-8").read())

    def test_without_configured_operators_there_are_no_recipients(self):
        self.assertEqual(settings_module.operator_admins([]), [])


class ConfigurationTests(SimpleTestCase):
    def test_exactly_the_two_ingestion_loggers_carry_the_email_handler(self):
        self.assertEqual(tuple(settings_module.OPERATOR_ALERT_LOGGERS), (CLIENT_LOGGER, SERVICES_LOGGER))
        for name in settings_module.OPERATOR_ALERT_LOGGERS:
            logger = logging.getLogger(name)
            self.assertEqual(sum(isinstance(h, OperatorEmailHandler) for h in logger.handlers), 1, name)
            self.assertTrue(logger.propagate, name)                     # assertLogs and any root handler still see it
        # No ancestor carries it, so one record can never produce two messages.
        for name in ("gemiapp", "gemiapp.ingestion", ""):
            self.assertFalse(any(isinstance(h, OperatorEmailHandler) for h in logging.getLogger(name).handlers), name)

    def test_stderr_visibility_is_preserved_and_unchanged(self):
        for name in settings_module.OPERATOR_ALERT_LOGGERS:
            logger = logging.getLogger(name)
            self.assertTrue(any(type(h) is logging.StreamHandler for h in logger.handlers), name)
            self.assertEqual(logger.level, logging.WARNING, name)       # exactly what lastResort gave before
            self.assertIn("WARNING-marker", emit(name, logging.WARNING, "WARNING-marker"))
            self.assertNotIn("INFO-marker", emit(name, logging.INFO, "INFO-marker"))

    def test_the_email_handler_only_fires_outside_debug(self):
        handler = next(h for h in logging.getLogger(CLIENT_LOGGER).handlers if isinstance(h, OperatorEmailHandler))
        self.assertEqual(handler.level, logging.ERROR)
        self.assertEqual([type(f).__name__ for f in handler.filters], ["RequireDebugFalse"])

    def test_the_test_runner_silences_operator_alerts(self):
        self.assertTrue(hasattr(fast_test_runner, "silence_operator_alerts"))
        source = open("config/fast_test_runner.py", encoding="utf-8").read()
        # Called twice -- main process and every parallel worker -- beside the definition itself.
        self.assertEqual(source.count("silence_operator_alerts()\n"), 2)


@override_settings(ADMINS=OPERATORS, DEBUG=False)
class RoutingTests(SimpleTestCase):
    def setUp(self):
        mail.outbox = []

    def assertAlerted(self, logger_name):
        stderr = emit(logger_name)
        self.assertEqual(len(mail.outbox), 1, logger_name)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["operator@example.com"])
        self.assertIn("ERROR", message.subject)
        self.assertIn("ingestion failure marker", message.subject)
        self.assertIn("ingestion failure marker", stderr)               # and still in the log stream
        return message

    def test_an_ingestion_client_error_notifies_the_operator(self):
        self.assertAlerted(CLIENT_LOGGER)

    def test_a_services_error_notifies_the_operator(self):
        self.assertAlerted(SERVICES_LOGGER)

    def test_one_record_produces_exactly_one_message(self):
        emit(CLIENT_LOGGER)
        self.assertEqual(len(mail.outbox), 1)

    def test_info_and_warning_do_not_notify_anyone(self):
        for name in (CLIENT_LOGGER, SERVICES_LOGGER):
            for level in (logging.INFO, logging.WARNING):
                emit(name, level)
        self.assertEqual(mail.outbox, [])

    def test_an_unrelated_logger_does_not_notify_anyone(self):
        for name in ("gemiapp", "gemiapp.opportunity_pipeline", "gemiapp.ingestion.discovery", "gemiapp.tasks"):
            emit(name)
        self.assertEqual(mail.outbox, [])


class QuietWhenItMustBeTests(SimpleTestCase):
    def setUp(self):
        mail.outbox = []

    @override_settings(ADMINS=OPERATORS, DEBUG=True)
    def test_debug_never_notifies_anyone(self):
        stderr = emit(CLIENT_LOGGER)
        self.assertEqual(mail.outbox, [])
        self.assertIn("ingestion failure marker", stderr)               # still visible to the developer

    @override_settings(ADMINS=[], DEBUG=False)
    def test_no_configured_operator_is_not_an_error(self):
        stderr = emit(SERVICES_LOGGER)
        self.assertEqual(mail.outbox, [])
        self.assertIn("ingestion failure marker", stderr)

    def test_the_suite_itself_never_queues_an_operator_email(self):
        from django.conf import settings

        self.assertEqual(settings.ADMINS, [])                           # the runner emptied it for this run
        emit(CLIENT_LOGGER)
        self.assertEqual(mail.outbox, [])


@override_settings(ADMINS=OPERATORS, DEBUG=False)
class AlertFailureIsContainedTests(SimpleTestCase):
    """A failing alert may never change what the caller was doing."""

    def setUp(self):
        mail.outbox = []

    def test_a_backend_that_raises_does_not_reach_the_caller(self):
        with patch("django.core.mail.backends.locmem.EmailBackend.send_messages",
                   side_effect=RuntimeError("relay exploded")):
            stderr = emit(SERVICES_LOGGER)                              # must not raise
        self.assertIn("ingestion failure marker", stderr)               # the original error still reaches stderr
        self.assertEqual(mail.outbox, [])

    def test_the_handler_routes_its_own_failure_to_handle_error(self):
        handler = OperatorEmailHandler()
        with patch.object(OperatorEmailHandler, "handleError") as handled, \
                patch("django.utils.log.AdminEmailHandler.emit", side_effect=RuntimeError("boom")):
            handler.emit(logging.LogRecord(SERVICES_LOGGER, logging.ERROR, __file__, 1, "x", None, None))
        handled.assert_called_once()


class IngestionSemanticsTests(TestCase):
    """The import fails exactly as it did before: same exception, same ImportRun, nothing written."""

    def setUp(self):
        self.target = date(2026, 9, 14)
        mail.outbox = []

    def broken_client(self):
        day = self.target.isoformat()
        valid = page(full_item("118717203000", day))
        broken = copy.deepcopy(full_item("118717206000", (self.target - timedelta(days=1)).isoformat()))
        broken["activities"][0] = "47191002"
        client, _, _, _ = make_client(routed({("true", "0"): valid, ("false", "0"): page(broken)}))
        return client

    def run_failing_import(self):
        """The real import against a page the A2 contract refuses. Returns (exception, stderr)."""
        from .services import import_for_date

        with captured_stderr() as buffer:
            with patch("gemiapp.services.get_gemi_client", return_value=self.broken_client()):
                with self.assertRaises(GemiResponseValidationError) as caught:
                    import_for_date(self.target)
        return caught.exception, buffer.getvalue()

    @override_settings(ADMINS=OPERATORS, DEBUG=False)
    def test_the_failure_still_raises_and_still_marks_the_run_failed(self):
        error, _ = self.run_failing_import()

        run = ImportRun.objects.get()
        self.assertEqual(run.status, "failed")
        self.assertIn("company_search v1", run.error_message)
        self.assertIn("company_search", str(error))
        self.assertTrue(mail.outbox)                                    # and the operator was told
        self.assertEqual(mail.outbox[0].to, ["operator@example.com"])
        self.assertIn(f"ImportRun {run.pk}", "\n".join(m.subject + m.body for m in mail.outbox))

    @override_settings(ADMINS=OPERATORS, DEBUG=False)
    def test_a_failing_alert_does_not_change_the_failure(self):
        with patch("django.core.mail.backends.locmem.EmailBackend.send_messages",
                   side_effect=RuntimeError("relay exploded")):
            error, stderr = self.run_failing_import()                   # the same exception, not the relay's

        self.assertIsInstance(error, GemiResponseValidationError)
        self.assertEqual(ImportRun.objects.get().status, "failed")
        self.assertEqual(mail.outbox, [])
        # The failure it could not send is still in the log stream, and so is the alert's own problem.
        self.assertIn("company_search", stderr)
        self.assertIn("relay exploded", stderr)

    @override_settings(ADMINS=[], DEBUG=False)
    def test_without_operators_the_import_behaves_identically(self):
        error, stderr = self.run_failing_import()

        self.assertIn("company_search", stderr)
        self.assertIsInstance(error, GemiResponseValidationError)
        self.assertEqual(ImportRun.objects.get().status, "failed")
        self.assertEqual(mail.outbox, [])
