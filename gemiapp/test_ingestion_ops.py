"""Tests for the operational safety layer of the scheduled GEMI ingestion: operator alerts
(gemiapp.ingestion_alerts), provenance-based parity with the legacy importer (gemiapp.ingestion_parity) and
the flag-gated schedule entry (gemiapp.apps.SCHEDULES).

No test reaches the network, and no test notifies a real operator: ADMINS is a fixture address and the email
backend is the test outbox.
"""

from datetime import date, timedelta
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.core.cache import caches
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from . import apps as gemi_apps
from . import ingestion_alerts
from .company_signals import DISCOVERY, LIVE, SHADOW, record_company_signal
from .ingestion import company_writer
from .ingestion.discovery import INGEST_QUARANTINED_DATE, compare_with_legacy, get_cursor
from .ingestion_alerts import (
    NO_PROGRESS, ORDERING_ANOMALY, PHASE_FAILED, REFUSED, REPEATED_FAILURES, WRITE_FAILED, open_alerts,
)
from .ingestion_cycle import LOCK_KEY, claim_cycle_lock
from .ingestion_parity import compare_unified_with_legacy
from .models import Company, CompanySignal, GemiDiscoveryRun, ImportRun, Opportunity
from .opportunity_pipeline import PIPELINE_MODES
from .services import import_for_date
from .tasks import run_gemi_ingestion_cycle_task
from .test_g4_shadow_cycle import BELOW, FRONTIER, LATE, NEW, OLD
from .test_gemi_discovery import item, known_company, paged_client
from .test_ingestion_cycle import ENABLED, TODAY, DiscoveryIngestTestCase, IngestionCycleTestCase, outcomes
from .test_operator_alerts import OPERATORS

TASK = "gemiapp.tasks.run_gemi_ingestion_cycle_task"


def alert_mails():
    return [message for message in mail.outbox if "GEMI ingestion ALERT" in message.subject]


def recovery_mails():
    return [message for message in mail.outbox if "GEMI ingestion recovered" in message.subject]


# --- 1. operator alerts ----------------------------------------------------------------------------------

@override_settings(ADMINS=OPERATORS, DEBUG=False)
class AlertTestCase(IngestionCycleTestCase):
    def task(self, *records, enabled=True, failure_after=None):
        """One scheduled invocation against a fixture page. Returns the task's summary."""
        client, _ = paged_client([self.page(*records)], failure_after=failure_after)
        with override_settings(GEMI_DISCOVERY_V2_ENABLED=enabled), \
                patch("gemiapp.ingestion.discovery.get_gemi_client", return_value=client):
            return run_gemi_ingestion_cycle_task()

    def setUp(self):
        super().setUp()
        # The shared cache is a plain table, not a model: a test-database flush does not empty it.
        caches["shared"].delete_many([ingestion_alerts._key(kind) for kind in ingestion_alerts.KINDS])

    def failing_write(self, number):
        real = company_writer.create_company_from_search_item

        def write(gemi_number, record, **kwargs):
            if gemi_number == str(number):
                raise RuntimeError("database hiccup")
            return real(gemi_number, record, **kwargs)

        return patch.object(company_writer, "create_company_from_search_item", side_effect=write)


class TransientFailureTests(AlertTestCase):
    def test_one_isolated_failure_is_recorded_and_alerts_nobody(self):
        with self.assertLogs("gemiapp.ingestion_alerts", level="WARNING") as logs:
            summary = self.task(item(NEW, self.today), failure_after=0)

        self.assertEqual((summary["status"], summary["alerts"]["alerted"]), ("failed", []))
        self.assertEqual(mail.outbox, [])
        self.assertEqual(open_alerts(), [])
        # Recorded, three ways: the run row, the cursor's count and a server-log line.
        self.assertEqual(GemiDiscoveryRun.objects.get().status, "failed")
        self.assertEqual(get_cursor().consecutive_failures, 1)
        self.assertEqual([record.levelname for record in logs.records], ["WARNING"])
        self.assertIn("recorded, no alert yet", logs.output[0])

    def test_a_transient_failure_followed_by_success_sends_no_recovery_either(self):
        self.task(item(NEW, self.today), failure_after=0)
        summary = self.task(item(NEW, self.today))
        self.assertEqual((summary["status"], summary["alerts"]), ("ok", {"alerted": [], "open": [], "recovered": []}))
        self.assertEqual(mail.outbox, [])

    def test_one_isolated_write_failure_alerts_nobody(self):
        with self.failing_write(NEW):
            summary = self.task(item(NEW, self.today))
        self.assertEqual((summary["stop_reason"], summary["alerts"]["alerted"]), ("ingest_write_failed", []))
        self.assertEqual(mail.outbox, [])
        self.assertEqual(self.task(item(NEW, self.today))["status"], "ok")      # the retry stored it
        self.assertEqual(mail.outbox, [])


class OneVoiceTests(AlertTestCase):
    def test_a_failing_scheduled_run_does_not_also_email_through_the_client_logger(self):
        with self.assertLogs("gemiapp.ingestion.client", level="ERROR") as logs:
            self.task(item(NEW, self.today), failure_after=0)
        self.assertIn("giving up after", logs.output[0])     # still logged for the server log...
        self.assertEqual(mail.outbox, [])                    # ...but not one email per failed run

    def test_the_legacy_importer_still_alerts_on_its_own_client_error(self):
        import logging

        logging.getLogger("gemiapp.ingestion.client").error("legacy import failure marker")
        logging.getLogger("gemiapp.services").error("legacy import failure marker")
        self.assertEqual(len(mail.outbox), 2)

    def test_the_suppression_ends_with_the_cycle_even_when_it_raises(self):
        import logging

        from config.operator_alerts import managed_alerting

        with self.assertRaises(RuntimeError):
            with managed_alerting():
                logging.getLogger("gemiapp.ingestion.client").error("inside")
                raise RuntimeError("boom")
        self.assertEqual(mail.outbox, [])
        logging.getLogger("gemiapp.ingestion.client").error("outside")
        self.assertEqual(len(mail.outbox), 1)


class PersistentFailureTests(AlertTestCase):
    def test_a_write_failure_that_repeats_on_the_retry_alerts_the_operator_once(self):
        with self.failing_write(NEW):
            self.task(item(NEW, self.today))
            self.assertEqual(alert_mails(), [])
            second = self.task(item(NEW, self.today))
            self.assertEqual(second["alerts"]["alerted"], [WRITE_FAILED])
            self.assertEqual(len(alert_mails()), 1)
            for _ in range(4):                                # it keeps failing: recorded, never re-sent
                again = self.task(item(NEW, self.today))
                self.assertEqual((again["alerts"]["alerted"], again["alerts"]["open"]), ([], [WRITE_FAILED]))
        message = alert_mails()[0]
        self.assertEqual((len(alert_mails()), message.to), (1, ["operator@example.com"]))
        self.assertIn("write failure", message.body)
        self.assertIn(str(FRONTIER), message.body)                               # where the frontier is held
        self.assertEqual(get_cursor().high_water_mark, str(FRONTIER))
        self.assertFalse(Company.objects.filter(gemi_number=str(NEW)).exists())

    def test_three_consecutive_failed_runs_alert_once_and_then_stay_quiet(self):
        for expected in ([], [], [REPEATED_FAILURES], [], []):
            summary = self.task(item(NEW, self.today), failure_after=0)
            self.assertEqual(summary["alerts"]["alerted"], expected)
        self.assertEqual(len(alert_mails()), 1)
        self.assertIn("consecutive ingestion runs ended without success", alert_mails()[0].body)
        self.assertEqual(get_cursor().consecutive_failures, 5)

    @override_settings(GEMI_INGESTION_ALERT_REMINDER_SECONDS=3600)
    def test_a_condition_that_lasts_is_reminded_after_the_reminder_interval(self):
        for _ in range(3):
            self.task(item(NEW, self.today), failure_after=0)
        self.assertEqual(len(alert_mails()), 1)
        caches["shared"].delete(ingestion_alerts._key(REPEATED_FAILURES))     # the reminder interval elapsed
        self.task(item(NEW, self.today), failure_after=0)
        self.assertEqual(len(alert_mails()), 2)

    def test_recovery_is_reported_once_and_the_next_failure_alerts_afresh(self):
        for _ in range(3):
            self.task(item(NEW, self.today), failure_after=0)
        self.assertEqual((len(alert_mails()), open_alerts()), (1, [REPEATED_FAILURES]))

        recovered = self.task(item(NEW, self.today))
        self.assertEqual((recovered["status"], recovered["alerts"]["recovered"]), ("ok", [REPEATED_FAILURES]))
        self.assertEqual((len(recovery_mails()), open_alerts()), (1, []))
        self.assertIn(REPEATED_FAILURES, recovery_mails()[0].body)
        self.assertEqual(get_cursor().high_water_mark, str(NEW))                 # and it really did recover

        self.task()                                                              # healthy: nothing more is sent
        self.assertEqual((len(alert_mails()), len(recovery_mails())), (1, 1))
        for _ in range(3):
            self.task(item(LATE, OLD), failure_after=0)
        self.assertEqual(len(alert_mails()), 2)                                  # a new episode, a new alert


class FrozenCursorTests(AlertTestCase):
    def test_a_blocking_ordering_anomaly_alerts_on_the_first_occurrence(self):
        summary = self.task(item(NEW, self.today), item(LATE, self.today))      # ascending: out of order
        self.assertEqual(summary["discovery_status"], "anomaly")
        self.assertEqual(summary["alerts"]["alerted"], [ORDERING_ANOMALY])
        cursor = get_cursor()
        self.assertEqual((cursor.status, cursor.high_water_mark), ("anomaly", str(FRONTIER)))
        body = alert_mails()[0].body
        self.assertIn("ordering anomaly", body)
        self.assertIn("bootstrap_gemi_discovery_v2 --force", body)

        again = self.task(item(NEW, self.today), item(LATE, self.today))
        self.assertEqual((again["alerts"]["alerted"], len(alert_mails())), ([], 1))   # not once per run


class NoProgressTests(AlertTestCase):
    def stale(self, hours=3):
        cursor = get_cursor()
        cursor.last_success_at = timezone.now() - timedelta(hours=hours)
        cursor.save()

    def test_a_cycle_that_cannot_run_for_hours_alerts_even_though_it_leaves_no_run_row(self):
        self.stale()
        release = claim_cycle_lock()                    # a lock nobody releases: every cycle is skipped
        summary = self.task(item(NEW, self.today))
        self.assertEqual((summary["status"], summary["alerts"]["alerted"]), ("skipped_locked", [NO_PROGRESS]))
        self.assertEqual(GemiDiscoveryRun.objects.count(), 0)
        self.assertIn("no successful ingestion run for 3h", alert_mails()[0].body)
        self.task(item(NEW, self.today))
        self.assertEqual(len(alert_mails()), 1)

        release()
        recovered = self.task(item(NEW, self.today))
        self.assertEqual((recovered["status"], recovered["alerts"]["recovered"]), ("ok", [NO_PROGRESS]))
        self.assertEqual(len(recovery_mails()), 1)

    def test_a_skipped_cycle_with_recent_progress_alerts_nobody(self):
        cursor = get_cursor()
        cursor.last_success_at = timezone.now() - timedelta(minutes=40)
        cursor.save()
        release = claim_cycle_lock()
        summary = self.task(item(NEW, self.today))
        release()
        self.assertEqual((summary["status"], summary["alerts"]["alerted"], mail.outbox), ("skipped_locked", [], []))

    def test_an_enabled_cycle_that_is_refused_alerts(self):
        record_company_signal(company=Company.objects.get(gemi_number=str(BELOW)), signal_type="new_company",
                              source_type=DISCOVERY, event_key={"event": "first_observed"}, mode=LIVE,
                              detected_at=timezone.now())
        summary = self.task(item(NEW, self.today))
        self.assertEqual(summary["status"], "refused")
        self.assertEqual((len(alert_mails()), open_alerts()), (1, [REFUSED]))
        self.assertIn("no LIVE signal exists", alert_mails()[0].body)
        self.task(item(NEW, self.today))
        self.assertEqual(len(alert_mails()), 1)


class AlertScopeTests(AlertTestCase):
    def test_a_disabled_task_is_dormant_not_a_failure(self):
        cursor = get_cursor()
        cursor.last_success_at = timezone.now() - timedelta(days=30)             # as stale as it gets
        cursor.save()
        for _ in range(5):
            with patch("gemiapp.ingestion.discovery.get_gemi_client") as get_client:
                summary = self.task(item(NEW, self.today), enabled=False)
                get_client.assert_not_called()
            self.assertEqual(summary["status"], "refused")
        self.assertEqual((mail.outbox, open_alerts(), GemiDiscoveryRun.objects.count()), ([], [], 0))

    def test_a_failed_phase_after_a_successful_discovery_alerts(self):
        from . import ingestion_cycle

        with patch.object(ingestion_cycle, "materialize_new_company_signals", side_effect=RuntimeError("killed")):
            summary = self.task(item(NEW, self.today))
        self.assertEqual((summary["discovery_status"], summary["alerts"]["alerted"]), ("success", [PHASE_FAILED]))
        self.assertIn("materialisation", alert_mails()[0].body)

    def test_a_broken_alert_evaluation_never_fails_the_cycle(self):
        with patch.object(ingestion_alerts, "evaluate_cycle", side_effect=RuntimeError("alerting is broken")):
            summary = self.task(item(NEW, self.today))
        self.assertEqual(summary["status"], "ok")
        self.assertNotIn("alerts", summary)
        self.assertTrue(Company.objects.filter(gemi_number=str(NEW)).exists())

    def test_an_unreachable_alert_store_sends_the_alert_rather_than_dropping_it(self):
        for _ in range(2):
            self.task(item(NEW, self.today), failure_after=0)
        class Down:
            def __getattr__(self, name):
                raise RuntimeError("cache down")

        with patch.object(ingestion_alerts, "caches", {"shared": Down()}):
            summary = self.task(item(NEW, self.today), failure_after=0)
        self.assertEqual((summary["alerts"]["alerted"], len(alert_mails())), ([REPEATED_FAILURES], 1))

    def test_a_manual_run_and_a_dry_run_alert_nobody(self):
        for argv in ((), ("--dry-run",)):
            for _ in range(4):
                client, _ = paged_client([self.page(item(NEW, self.today))], failure_after=0)
                with ENABLED, patch("gemiapp.ingestion.discovery.get_gemi_client", return_value=client):
                    with self.assertRaises(CommandError):
                        call_command("run_gemi_ingestion_cycle", *argv, stdout=StringIO())
        self.assertEqual((alert_mails(), open_alerts()), ([], []))   # the policy belongs to the scheduled task

    def test_alerts_carry_no_payload_or_personal_data(self):
        from .test_gemi_validation import CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL

        with self.failing_write(NEW):
            for _ in range(2):
                self.task(item(NEW, self.today))
        text = alert_mails()[0].subject + alert_mails()[0].body
        for sentinel in (CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL, "ΔΟΚΙΜΑΣΤΙΚΗ"):
            self.assertNotIn(sentinel, text)

    def test_the_cycle_stays_shadow_whatever_the_alerts_do(self):
        for _ in range(3):
            self.task(item(NEW, self.today), failure_after=0)
        self.task(item(LATE, OLD), item(NEW, self.today))
        self.assertEqual(PIPELINE_MODES, (SHADOW,))
        self.assertEqual(set(CompanySignal.objects.values_list("mode", flat=True)), {SHADOW})
        self.assertEqual(set(Opportunity.objects.values_list("latest_signal__mode", flat=True)), {SHADOW})
        self.assertIsNone(caches["shared"].get(LOCK_KEY))


# --- 2. parity with the legacy importer ------------------------------------------------------------------

class ParityTestCase(DiscoveryIngestTestCase):
    def legacy_import(self, *records, target=TODAY):
        """One legacy ImportRun for ``target`` that fetched exactly ``records``."""
        with patch("gemiapp.services.fetch_companies", return_value=list(records)):
            return import_for_date(target)

    def report(self, target=TODAY):
        return compare_unified_with_legacy(target)

    def page(self, *records):
        return [list(records) + [item(1000), item(999)]]


class ParityTests(ParityTestCase):
    def test_a_company_the_ingestion_created_is_unified_first_never_a_legacy_only_miss(self):
        self.ingest(self.page(item(1002, TODAY), item(1001, TODAY)))
        report = self.report()
        self.assertEqual((report.companies_dated, report.unified_first, report.legacy_first), (2, 2, 0))
        self.assertEqual((report.legacy_only, report.unified_failed_to_store, report.verdict), ([], [], "OK"))
        self.assertEqual((report.unified_created, report.unified_created_same_day), (2, 2))
        # The contaminated comparison would have called both of them legacy finds.
        self.assertEqual(compare_with_legacy(TODAY).legacy, 2)

    def test_unified_first_then_the_later_legacy_import_is_counted_as_such(self):
        self.ingest(self.page(item(1002, TODAY), item(1001, TODAY)))
        run = self.legacy_import(item(1001, TODAY))
        self.assertEqual((run.created_count, run.updated_count), (0, 1))          # the importer's own evidence
        report = self.report()
        self.assertEqual((report.unified_first, report.unified_first_then_legacy, report.legacy_first), (2, 1, 0))
        self.assertEqual((report.legacy_import_runs, report.legacy_reported_created, report.verdict), (1, 0, "OK"))

    def test_a_company_legacy_stored_that_no_later_unified_run_recorded_is_a_real_miss(self):
        self.legacy_import(item(500, TODAY))                 # far below the window the scan pages through
        self.ingest(self.page(item(1001, TODAY)))            # a successful unified run after it: never saw 500
        report = self.report()
        self.assertTrue(Company.objects.filter(gemi_number="500").exists())      # the row exists...
        self.assertEqual(report.legacy_only, ["500"])                             # ...and that is not parity
        self.assertEqual((report.verdict, report.unified_first, report.legacy_first), ("MISS", 1, 1))
        self.assertEqual(report.legacy_reported_created, 1)
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command("report_gemi_ingestion_parity", "--date", TODAY.isoformat(), stdout=out)
        self.assertIn("LEGACY-ONLY", out.getvalue())
        self.assertIn("['500']", out.getvalue())
        self.assertIn("INGESTION HEALTH", out.getvalue())

    def test_a_legacy_find_is_not_judged_before_a_unified_run_had_the_chance(self):
        self.ingest(self.page(item(1001, TODAY)))
        self.legacy_import(item(500, TODAY))                 # stored after the last unified run
        report = self.report()
        self.assertEqual((report.awaiting_unified_run, report.legacy_only, report.verdict), (1, [], "PENDING"))
        self.ingest(self.page(item(1001, TODAY)))            # the next run still does not reach it
        self.assertEqual((self.report().legacy_only, self.report().verdict), (["500"], "MISS"))

    def test_legacy_first_then_unified_discovers_it_is_a_race_not_a_miss(self):
        self.legacy_import(item(1001, TODAY))
        self.ingest(self.page(item(1002, TODAY), item(1001, TODAY)))
        report = self.report()
        self.assertEqual(outcomes()["1001"], "already_local")
        self.assertEqual((report.legacy_first_confirmed, report.legacy_only, report.verdict), (1, [], "OK"))
        self.assertEqual((report.unified_first, report.legacy_first), (1, 1))
        self.assertEqual(Company.objects.filter(gemi_number="1001").count(), 1)

    def test_legacy_first_seen_only_as_an_already_known_record(self):
        self.legacy_import(item(998, TODAY))                  # below the frontier and stored: "known" to the scan
        self.ingest([[item(1001, TODAY), item(1000), item(999), item(998, TODAY)]])
        report = self.report()
        self.assertEqual((report.legacy_first_seen_known, report.legacy_only, report.verdict), (1, [], "OK"))

    def test_a_company_unified_discovered_but_failed_to_store_is_flagged_when_legacy_stores_it(self):
        real = company_writer.create_company_from_search_item

        def write(number, record, **kwargs):
            if number == "1001":
                raise RuntimeError("database hiccup")
            return real(number, record, **kwargs)

        with patch.object(company_writer, "create_company_from_search_item", side_effect=write):
            self.ingest(self.page(item(1001, TODAY)))
        self.legacy_import(item(1001, TODAY))
        report = self.report()
        self.assertEqual((report.unified_failed_to_store, report.write_failed, report.verdict), (["1001"], 1, "MISS"))
        self.assertEqual((report.failed_runs, report.legacy_only), (1, []))
        self.ingest(self.page(item(1001, TODAY)))             # seeing it later does not rewrite what happened
        self.assertEqual(self.report().unified_failed_to_store, ["1001"])

    def test_quarantined_dates_are_reported_on_their_own_and_are_never_a_miss(self):
        future = (TODAY + timedelta(days=1)).isoformat()
        self.ingest(self.page(item(1003, incorporationDate=None), item(1002, future), item(1001, TODAY)))
        report = self.report()
        self.assertEqual(outcomes()["1003"], INGEST_QUARANTINED_DATE)
        self.assertEqual((report.quarantined_date, report.unified_created, report.companies_dated), (2, 1, 1))
        self.assertEqual((report.legacy_only, report.unified_failed_to_store, report.verdict), ([], [], "OK"))
        self.assertFalse(Company.objects.filter(gemi_number__in=["1003", "1002"]).exists())

    def test_late_publications_are_unified_only_by_design(self):
        self.ingest(self.page(item(1002, "2021-05-05"), item(1001, TODAY)))
        report = self.report()
        self.assertEqual((report.unified_created, report.unified_only_late_publications,
                          report.unified_created_same_day), (2, 1, 1))
        self.assertEqual((report.companies_dated, report.verdict), (1, "OK"))     # the late one is dated 2021
        old = self.report(date(2021, 5, 5))
        self.assertEqual((old.companies_dated, old.unified_first, old.verdict), (1, 1, "NOT_MEASURED"))

    def test_a_row_created_by_neither_pipeline_is_never_attributed_to_legacy(self):
        self.ingest(self.page(item(1001, TODAY)))
        known_company(1500, TODAY)                            # hydration or the admin: no ImportRun window
        self.ingest(self.page(item(1001, TODAY)))
        report = self.report()
        self.assertEqual((report.other_writer, report.legacy_first, report.legacy_only), (1, 0, []))
        self.assertEqual((report.other_writer_unseen, report.verdict), (["1500"], "OK"))

    def test_duplicates_anomalies_and_failed_runs_are_counted_separately(self):
        first = [item(1004, TODAY), item(1003, TODAY), item(1002, TODAY), item(1001, TODAY)]
        self.ingest([first, [item(1002, TODAY), item(1001, TODAY), item(1000), item(999)]])     # offset drift
        self.ingest(self.page(item(1005, TODAY)), failure_after=0)                             # a failed run
        self.ingest([[item(1006, TODAY), item(1007, TODAY), item(1000), item(999)]])            # out of order
        report = self.report()
        self.assertEqual((report.ingest_runs, report.successful_runs, report.failed_runs, report.anomaly_runs),
                         (3, 1, 1, 1))
        self.assertEqual((report.duplicate_records, report.legacy_only), (2, []))

    def test_a_day_without_any_unified_run_is_not_measured_rather_than_ok(self):
        self.legacy_import(item(500, TODAY))
        report = self.report()
        self.assertEqual((report.ingest_runs, report.verdict, report.legacy_only), (0, "NOT_MEASURED", []))
        self.assertEqual(report.awaiting_unified_run, 1)

    def test_a_failed_legacy_import_is_visible(self):
        with patch("gemiapp.services.fetch_companies", side_effect=RuntimeError("gemi down")):
            with self.assertRaises(RuntimeError):
                import_for_date(TODAY)
        self.ingest(self.page(item(1001, TODAY)))
        report = self.report()
        self.assertEqual((report.legacy_import_runs, report.legacy_failed_runs, report.verdict), (1, 1, "OK"))

    def test_the_report_is_read_only_and_holds_identifiers_only(self):
        self.legacy_import(item(500, TODAY))
        self.ingest(self.page(item(1001, TODAY)))
        before = (Company.objects.count(), GemiDiscoveryRun.objects.count(), ImportRun.objects.count())
        with self.assertNumQueries(6):      # both lanes in the same six reads
            text = "\n".join(self.report().lines())
        self.assertEqual((Company.objects.count(), GemiDiscoveryRun.objects.count(), ImportRun.objects.count()),
                         before)
        self.assertNotIn("ΔΟΚΙΜΑΣΤΙΚΗ", text)


# --- 3. the schedule: prepared, gated, not enabled ---------------------------------------------------------

class ScheduleTests(TestCase):
    def rows(self):
        from django_q.models import Schedule

        return Schedule.objects.filter(func=TASK)

    def register(self):
        gemi_apps.setup_daily_pipeline_schedule(None)

    def others(self):
        from django_q.models import Schedule

        return list(Schedule.objects.exclude(func=TASK).order_by("id").values("id", "func", "cron", "name"))

    def test_the_entry_is_defined_once_for_every_thirty_minutes_off_the_hour(self):
        entries = [entry for entry in gemi_apps.SCHEDULES if entry["func"] == TASK]
        self.assertEqual(entries, [{
            "func": TASK, "name": "GEMI Unified Ingestion (SHADOW)", "cron": "13,43 * * * *",
            "requires": "gemiapp.ingestion.discovery.ingest_enabled",
        }])
        minutes = [int(value) for value in entries[0]["cron"].split()[0].split(",")]
        self.assertEqual((minutes, minutes[1] - minutes[0]), ([13, 43], 30))      # every 30 minutes, not 10
        # Never on a minute another job fires on: :00 imports and digests, 07:37 drain, 08:00 notifications.
        taken = {int(entry["cron"].split()[0]) for entry in gemi_apps.SCHEDULES if entry["func"] != TASK}
        self.assertEqual(taken & set(minutes), set())

    def test_the_other_schedules_are_exactly_what_they_were(self):
        self.assertEqual({entry["func"]: entry["cron"] for entry in gemi_apps.SCHEDULES if entry["func"] != TASK}, {
            "gemiapp.tasks.run_daily_pipeline_task": "0 9 * * *",
            "gemiapp.tasks.run_intraday_pipeline_task": "0 8,11,14,17,20,23 * * *",
            "gemiapp.tasks.drain_pending_outreach_task": "37 7 * * *",
            "gemiapp.tasks.generate_task_due_notifications_task": "0 8 * * *",
        })
        scheduled = " ".join(entry["func"] for entry in gemi_apps.SCHEDULES)
        for unscheduled in ("hydrat", "discovery_v2", "g4_shadow", "company_refresh", "reference_data"):
            self.assertNotIn(unscheduled, scheduled)

    def test_the_flag_is_off_by_default_and_no_schedule_row_exists(self):
        self.assertFalse(settings.GEMI_DISCOVERY_V2_ENABLED)
        self.assertEqual(self.rows().count(), 0)              # the registration at test-database setup made none
        before = self.others()
        self.register()
        self.register()
        self.assertEqual((self.rows().count(), self.others()), (0, before))

    def test_enabling_the_flag_registers_exactly_one_row_and_disabling_removes_it(self):
        from django_q.models import Schedule

        before = self.others()
        with ENABLED:
            self.register()
            self.register()
            self.register()
            row = self.rows().get()                           # exactly one, however often migrate runs
            self.assertEqual((row.cron, row.schedule_type, row.repeats), ("13,43 * * * *", Schedule.CRON, -1))
            self.assertGreater(row.next_run, timezone.now())  # never fires at the moment it is registered
            self.assertIn(timezone.localtime(row.next_run).minute, (13, 43))
            self.assertEqual(self.others(), before)
        self.register()                                       # the flag is off again
        self.assertEqual((self.rows().count(), self.others()), (0, before))

    def test_a_duplicated_row_is_repaired_to_one(self):
        from django_q.models import Schedule

        with ENABLED:
            self.register()
            Schedule.objects.create(func=TASK, schedule_type=Schedule.CRON, cron="*/10 * * * *", repeats=-1)
            self.register()
            self.assertEqual(self.rows().get().cron, "13,43 * * * *")

    def test_the_task_is_dormant_while_the_flag_is_off(self):
        with patch("gemiapp.ingestion.discovery.get_gemi_client") as get_client, \
                patch.object(ingestion_alerts, "evaluate_refusal") as alert:
            summary = run_gemi_ingestion_cycle_task()
        self.assertEqual(summary["status"], "refused")
        get_client.assert_not_called()
        alert.assert_not_called()
        self.assertEqual((GemiDiscoveryRun.objects.count(), mail.outbox), (0, []))
