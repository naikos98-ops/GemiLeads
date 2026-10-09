"""Tests for the dormant same-day discovery lane (gemiapp.ingestion.sameday_discovery), its place in the
ingestion cycle, the two-lane parity report, the per-stream alerts and the Athens-date rule of the shared writer.

The shape being fixed is production's 2026-10-08 miss: companies with a GEMI number far below the -arGemi
frontier and an incorporation date of today. No test reaches the network: every client is a fake transport.
"""

import urllib.parse
from datetime import date, datetime, timedelta
from datetime import timezone as datetime_timezone
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from . import apps as gemi_apps
from . import ingestion_cycle
from .company_signals import NEW_COMPANY, SHADOW
from .ingestion import company_writer
from .ingestion.discovery import (
    INGEST,
    INGEST_ALREADY_LOCAL,
    INGEST_CREATED,
    KNOWN,
    LATE_PUBLICATION,
    NEW_INCORPORATION,
    STOP_END_OF_RESULTS,
    STOP_INGEST_WRITE_FAILED,
    STOP_PAGE_LIMIT,
    STREAM_COMPANIES,
    run_discovery,
)
from .ingestion.sameday_discovery import (
    DATE_ORDER_VIOLATION,
    STOP_DATE_ORDER_ANOMALY,
    STOP_OLDER_BOUNDARY,
    STREAM_INCORPORATION_DATE,
    TRIGGER_BACKFILL,
    TRIGGER_CYCLE,
    TRIGGER_OPERATOR,
    SameDayLaneRefused,
    SameDayPolicy,
    run_sameday_discovery,
)
from .ingestion_alerts import (
    SAMEDAY_DATE_ORDER_ANOMALY, SAMEDAY_NO_PROGRESS, SAMEDAY_REPEATED_FAILURES, ingestion_health, open_alerts,
)
from .ingestion_cycle import FAILED_CYCLE, OK, SAMEDAY_PHASE, run_ingestion_cycle
from .models import (
    Company, CompanyActivity, CompanySignal, GemiDiscoveryCursor, GemiDiscoveryObservation, GemiDiscoveryRun,
    Opportunity,
)
from .services import company_defaults
from .tasks import run_gemi_ingestion_cycle_task
from .test_discovery_frontier import imported
from .test_g4_shadow_cycle import NEW
from .test_gemi_client import NoNetworkMixin, make_client, response
from .test_gemi_discovery import item, known_company, ready_cursor
from .test_gemi_validation import page
from .test_ingestion_cycle import ENABLED, POLICY, TODAY, IngestionCycleTestCase, lookups
from .test_ingestion_ops import AlertTestCase, ParityTestCase, alert_mails, recovery_mails

BOTH_FLAGS = override_settings(GEMI_DISCOVERY_V2_ENABLED=True, GEMI_DISCOVERY_SAMEDAY_LANE_ENABLED=True)
LANE_POLICY = SameDayPolicy(page_size=4, max_pages=5, lookback_days=1, older_boundary_records=2)
WIDE_PAGE = SameDayPolicy(page_size=50, max_pages=5, lookback_days=1, older_boundary_records=2)
YESTERDAY = TODAY - timedelta(days=1)
OLDER = TODAY - timedelta(days=5)
# The production shape: a GEMI number far below any frontier these tests use, dated today.
LOW = 57043309000
LOW_2 = 67552603000


def lanes_client(frontier=(), active=(), inactive=(), *, fail=()):
    """One client for both lanes. ``frontier`` / ``active`` / ``inactive`` are lists of pages (lists of records);
    a lane named in ``fail`` answers 503."""
    def route(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if query.get("resultsSortBy") == "-incorporationDate":
            name = {"true": "active", "false": "inactive"}[query["isActive"]]
            pages = active if name == "active" else inactive
        else:
            name, pages = "frontier", frontier
        if name in fail:
            return response(503, body=b"")
        total = sum(len(records) for records in pages)
        offset, position = int(query.get("resultsOffset", 0)), 0
        for records in pages:
            if position == offset:
                return response(200, page(*records, total=total))
            position += len(records)
        return response(200, page(total=total))

    client, transport, _, _ = make_client(route, max_attempts=1)
    return client, transport


def queries(transport):
    return [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(call["url"]).query)) for call in transport.calls]


def lane_rows():
    return {row.gemi_number: (row.classification, row.ingest_outcome, row.company_existed)
            for row in GemiDiscoveryObservation.objects.filter(run__stream=STREAM_INCORPORATION_DATE).order_by("id")}


def old(number, days=5):
    return item(number, TODAY - timedelta(days=days))


class LaneTestCase(NoNetworkMixin, TestCase):
    def lane(self, active=(), inactive=(), *, fail=(), **options):
        client, transport = lanes_client(active=list(active), inactive=list(inactive), fail=fail)
        options.setdefault("policy", LANE_POLICY)
        with BOTH_FLAGS:
            return run_sameday_discovery(client=client, **options), transport


# --- the shared writer: the Athens business date ----------------------------------------------------------

class AthensMidnightTests(NoNetworkMixin, TestCase):
    # 00:30 on 2026-10-09 in Athens (EEST, UTC+3) is 21:30 on 2026-10-08 in UTC, the container's clock.
    MOMENT = datetime(2026, 10, 8, 21, 30, tzinfo=datetime_timezone.utc)
    ATHENS_TODAY = date(2026, 10, 9)
    UTC_TODAY = date(2026, 10, 8)

    def at_half_past_midnight(self):
        utc_today = self.UTC_TODAY

        class UtcDate(date):
            @classmethod
            def today(cls):
                return cls(utc_today.year, utc_today.month, utc_today.day)

        # The container clock (date.today()) says UTC-yesterday everywhere a module could ask it.
        stack = [patch("django.utils.timezone.now", return_value=self.MOMENT), patch("gemiapp.services.date", UtcDate),
                 patch("gemiapp.ingestion.company_writer.date", UtcDate)]
        for item_ in stack:
            item_.start()
            self.addCleanup(item_.stop)

    def test_a_company_dated_athens_today_is_created_immediately_with_exactly_its_source_date(self):
        self.at_half_past_midnight()
        self.assertEqual(timezone.localdate(), self.ATHENS_TODAY)
        record = item(3001, self.ATHENS_TODAY)
        # The premise, and the proof nothing global changed: the legacy defaults still clamp it to UTC-today.
        self.assertEqual(company_defaults(record)["incorporation_date"], self.UTC_TODAY)

        outcome = company_writer.create_company_from_search_item("3001", record)

        self.assertEqual((outcome.status, outcome.date_problem, outcome.stored_as_today),
                         (company_writer.CREATED, "", True))
        self.assertEqual(Company.objects.get(gemi_number="3001").incorporation_date, self.ATHENS_TODAY)
        self.assertEqual(company_defaults(record)["incorporation_date"], self.UTC_TODAY)

    def test_future_is_judged_against_the_athens_date_not_the_container_date(self):
        self.at_half_past_midnight()
        tomorrow = self.ATHENS_TODAY + timedelta(days=1)
        self.assertEqual(company_writer.source_date_problem(item(3001, self.ATHENS_TODAY)), "")
        outcome = company_writer.create_company_from_search_item("3002", item(3002, tomorrow))
        self.assertEqual((outcome.status, outcome.date_problem),
                         (company_writer.REFUSED_DATE, company_writer.DATE_FUTURE))
        self.assertFalse(Company.objects.exists())

    def test_the_same_day_lane_does_not_quarantine_it(self):
        self.at_half_past_midnight()
        client, _ = lanes_client(active=[[item(LOW, self.ATHENS_TODAY)]])
        with BOTH_FLAGS:
            result = run_sameday_discovery(client=client, policy=LANE_POLICY)
        self.assertEqual((result.as_of, result.ingested_records, result.quarantined_date_records),
                         (self.ATHENS_TODAY, 1, 0))
        self.assertEqual(lane_rows(), {str(LOW): (NEW_INCORPORATION, INGEST_CREATED, False)})
        self.assertEqual(Company.objects.get(gemi_number=str(LOW)).incorporation_date, self.ATHENS_TODAY)

    def test_the_frontier_lane_does_not_quarantine_it_either(self):
        self.at_half_past_midnight()
        known_company(1000)
        ready_cursor(1000)
        client, _ = lanes_client(frontier=[[item(1001, self.ATHENS_TODAY), item(1000)]])
        with ENABLED:
            result = run_discovery(mode=INGEST, client=client, policy=POLICY)
        self.assertEqual((result.ingested_records, result.quarantined_date_records), (1, 0))
        self.assertEqual(Company.objects.get(gemi_number="1001").incorporation_date, self.ATHENS_TODAY)


# --- the lane on its own ----------------------------------------------------------------------------------

class DormantLaneTests(LaneTestCase):
    def test_the_flag_is_off_by_default(self):
        self.assertFalse(settings.GEMI_DISCOVERY_SAMEDAY_LANE_ENABLED)

    def test_the_lane_refuses_before_any_request_or_write_without_either_flag(self):
        for flags in ({"GEMI_DISCOVERY_V2_ENABLED": True}, {"GEMI_DISCOVERY_SAMEDAY_LANE_ENABLED": True}, {}):
            for dry_run in (False, True):
                client, transport = lanes_client(active=[[item(LOW, TODAY)]])
                with override_settings(**flags), self.assertRaises(SameDayLaneRefused):
                    run_sameday_discovery(client=client, policy=LANE_POLICY, dry_run=dry_run)
                self.assertEqual(transport.calls, [])
        self.assertEqual((Company.objects.count(), GemiDiscoveryRun.objects.count()), (0, 0))

    def test_the_command_refuses_the_same_way(self):
        with patch("gemiapp.ingestion.sameday_discovery.get_gemi_client") as get_client, \
                self.assertRaises(CommandError):
            call_command("run_gemi_sameday_discovery", "--dry-run", stdout=StringIO())
        get_client.assert_not_called()

    def test_nothing_schedules_the_lane_and_the_existing_schedules_are_what_they_were(self):
        crons = {entry["func"]: entry["cron"] for entry in gemi_apps.SCHEDULES}
        self.assertEqual(crons["gemiapp.tasks.run_gemi_ingestion_cycle_task"], "13,43 * * * *")
        self.assertEqual(crons["gemiapp.tasks.run_intraday_pipeline_task"], "0 8,11,14,17,20,23 * * *")
        self.assertEqual(crons["gemiapp.tasks.run_daily_pipeline_task"], "0 9 * * *")
        self.assertFalse([func for func in crons if "sameday" in func])


class QueryTests(LaneTestCase):
    def test_both_passes_send_exactly_the_legacy_query_and_no_per_company_lookup(self):
        result, transport = self.lane(active=[[item(LOW, TODAY)]], inactive=[[item(LOW_2, TODAY)]])
        self.assertEqual(queries(transport), [
            {"isActive": "true", "resultsSortBy": "-incorporationDate", "resultsOffset": "0", "resultsSize": "4"},
            {"isActive": "false", "resultsSortBy": "-incorporationDate", "resultsOffset": "0", "resultsSize": "4"},
        ])
        self.assertEqual(lookups(transport), [])
        self.assertEqual([entry["pass"] for entry in result.passes], ["active", "inactive"])
        self.assertEqual((result.pages_fetched, result.ingested_records), (2, 2))
        self.assertTrue(Company.objects.filter(gemi_number=str(LOW_2)).exists())      # the inactive pass ran

    def test_paging_advances_the_offset_by_what_each_page_returned(self):
        today = [item(number, TODAY) for number in range(5001, 5007)]
        _, transport = self.lane(active=[today[:4], today[4:]])
        self.assertEqual([query["resultsOffset"] for query in queries(transport) if query["isActive"] == "true"],
                         ["0", "4"])
        self.assertEqual(Company.objects.count(), 6)

    def test_the_command_reports_a_dry_run(self):
        client, transport = lanes_client(active=[[item(LOW, TODAY)]])
        out = StringIO()
        with BOTH_FLAGS, patch("gemiapp.ingestion.sameday_discovery.get_gemi_client", return_value=client):
            call_command("run_gemi_sameday_discovery", "--dry-run", stdout=out)
        self.assertIn("would_create=1", out.getvalue())
        self.assertIn("ΖΗΤΗΘΗΚΑΝ", out.getvalue())
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual((Company.objects.count(), GemiDiscoveryRun.objects.count()), (0, 0))


class TriageAndStoppingTests(LaneTestCase):
    def test_future_and_unusable_dates_at_the_top_never_stop_the_scan_and_leave_no_trace(self):
        tomorrow = (TODAY + timedelta(days=1)).isoformat()
        junk = [item(9001, incorporationDate="9011-12-09"), item(9002, incorporationDate="3006-03-03"),
                item(9003, incorporationDate=tomorrow), item(9004, incorporationDate=None)]
        more_junk = [item(9005, incorporationDate="2026-13-45"), item(9006, incorporationDate="1821-01-01"),
                     item(9007, incorporationDate="5015-02-05"), item(9008, incorporationDate="")]
        result, transport = self.lane(active=[junk, more_junk, [item(LOW, TODAY), old(8001), old(8002), old(8003)]])

        self.assertEqual((result.status, result.passes[0]["stop_reason"]), ("success", STOP_OLDER_BOUNDARY))
        self.assertEqual((result.future_date_records, result.unusable_date_records), (4, 4))
        self.assertEqual(lane_rows(), {str(LOW): (NEW_INCORPORATION, INGEST_CREATED, False)})   # no junk rows
        self.assertEqual(list(Company.objects.values_list("gemi_number", flat=True)), [str(LOW)])
        self.assertEqual(result.quarantined_date_records, 0)
        self.assertEqual(len([query for query in queries(transport) if query["isActive"] == "true"]), 3)

    def test_the_boundary_page_s_older_companies_are_never_written_or_recorded(self):
        result, _ = self.lane(active=[[item(LOW, TODAY), old(8001), old(8002), old(8003)]])
        self.assertEqual((result.older_boundary_records, result.records_examined), (3, 1))
        self.assertEqual(list(Company.objects.values_list("gemi_number", flat=True)), [str(LOW)])
        self.assertEqual(set(lane_rows()), {str(LOW)})

    def test_the_scan_goes_on_until_enough_older_records_confirm_the_boundary(self):
        # One older record ends the page: below the threshold of two, so the next page is fetched.
        today = [item(number, TODAY) for number in (5001, 5002, 5003)]
        result, transport = self.lane(active=[today + [old(8001)], [old(8002), old(8003), old(8004), old(8005)]])
        self.assertEqual((result.passes[0]["pages"], result.passes[0]["stop_reason"]), (2, STOP_OLDER_BOUNDARY))
        self.assertEqual((result.status, Company.objects.count()), ("success", 3))

    def test_the_window_includes_yesterday_and_nothing_older(self):
        result, _ = self.lane(active=[[item(LOW, TODAY), item(LOW_2, YESTERDAY), old(8001, days=2), old(8002)]])
        self.assertEqual(lane_rows(), {str(LOW): (NEW_INCORPORATION, INGEST_CREATED, False),
                                       str(LOW_2): (LATE_PUBLICATION, INGEST_CREATED, False)})
        self.assertEqual(Company.objects.get(gemi_number=str(LOW_2)).incorporation_date, YESTERDAY)
        self.assertEqual((result.window_start, result.as_of, result.older_boundary_records), (YESTERDAY, TODAY, 2))

    def test_a_stray_older_record_at_the_top_is_tolerated(self):
        result, _ = self.lane(active=[[old(8001), item(5001, TODAY), item(5002, TODAY), item(5003, TODAY)],
                                      [old(8002), old(8003)]])
        self.assertEqual((result.status, result.tolerated_order_irregularities, result.date_order_anomalies),
                         ("success", 3, 0))
        self.assertEqual(Company.objects.count(), 3)

    def test_an_in_window_record_after_the_established_boundary_is_a_date_order_anomaly(self):
        result, transport = self.lane(active=[[item(5001, TODAY), old(8001), old(8002), item(5002, TODAY)],
                                              [item(5003, TODAY)]])
        self.assertEqual((result.status, result.stop_reason, result.date_order_anomalies),
                         ("anomaly", STOP_DATE_ORDER_ANOMALY, 1))
        self.assertEqual([entry["kind"] for entry in result.blocking_anomalies], [DATE_ORDER_VIOLATION])
        self.assertEqual(result.blocking_anomalies[0]["gemi_number"], "5002")
        # The record is still processed (create-only, idempotent); paging stops after that page.
        self.assertEqual(set(Company.objects.values_list("gemi_number", flat=True)), {"5001", "5002"})
        self.assertEqual(len(transport.calls), 1)
        run = GemiDiscoveryRun.objects.get()
        self.assertEqual((run.status, run.policy["statistics"]["date_order_anomalies"]), ("anomaly", 1))

    def test_the_page_limit_is_a_hard_stop_and_the_run_is_incomplete(self):
        full = [[item(number, TODAY) for number in range(start, start + 4)] for start in (5001, 5005, 5009)]
        result, transport = self.lane(active=full, max_pages=2)
        self.assertEqual((result.status, result.stop_reason, result.passes[0]["pages"]),
                         ("incomplete", STOP_PAGE_LIMIT, 2))
        self.assertEqual(Company.objects.count(), 8)             # what was fetched is kept
        self.assertEqual(result.passes[1]["pass"], "inactive")   # the other pass still ran

    def test_the_end_of_results_is_success(self):
        result, _ = self.lane(active=[[item(LOW, TODAY)]])
        self.assertEqual((result.status, result.stop_reason), ("success", STOP_END_OF_RESULTS))

    def test_a_record_seen_twice_is_examined_once(self):
        # Paging drift: a record pushed onto the next page; and the same number in the other pass.
        first = [item(number, TODAY) for number in (5001, 5002, 5003, 5004)]
        result, _ = self.lane(active=[first, [item(5004, TODAY)]], inactive=[[item(5001, TODAY)]])
        self.assertEqual((result.duplicate_records, result.records_examined, Company.objects.count()), (2, 4, 4))
        self.assertEqual(result.status, "success")


class NewnessTests(LaneTestCase):
    def test_a_stored_company_without_discovery_evidence_gets_genuine_already_local_evidence(self):
        stored = known_company(LOW, TODAY)
        before = list(Company.objects.values())
        result, _ = self.lane(active=[[item(LOW, TODAY)]])
        self.assertEqual(lane_rows(), {str(LOW): (NEW_INCORPORATION, INGEST_ALREADY_LOCAL, True)})
        self.assertEqual((result.new_records, result.rediscovered_local_records, result.ingested_records), (1, 1, 0))
        self.assertEqual(result.discovered_gemi_numbers, [str(LOW)])           # eligible for materialisation
        self.assertEqual(list(Company.objects.values()), before)               # never written over
        self.assertFalse(CompanyActivity.objects.filter(company=stored).exists())

    def test_once_it_has_evidence_it_is_known_and_the_repeat_is_not_stored_again(self):
        known_company(LOW, TODAY)
        self.lane(active=[[item(LOW, TODAY)]])
        second, _ = self.lane(active=[[item(LOW, TODAY)]])
        self.assertEqual((second.known_records, second.new_records, second.discovered_gemi_numbers), (1, 0, []))
        self.assertEqual(GemiDiscoveryObservation.objects.filter(classification=KNOWN).count(), 1)
        third, _ = self.lane(active=[[item(LOW, TODAY)]])
        self.assertEqual((third.observations_stored, third.observations_suppressed), (0, 1))
        self.assertEqual(GemiDiscoveryObservation.objects.count(), 2)

    def test_a_created_company_is_known_on_the_next_scan_and_never_created_twice(self):
        self.lane(active=[[item(LOW, TODAY)]])
        row = list(Company.objects.values())
        again, _ = self.lane(active=[[item(LOW, TODAY)]])
        self.assertEqual((again.known_records, again.ingested_records), (1, 0))
        self.assertEqual(list(Company.objects.values()), row)

    def test_a_dry_run_requests_the_pages_and_writes_nothing(self):
        result, transport = self.lane(active=[[item(LOW, TODAY)]], dry_run=True)
        self.assertEqual((result.would_ingest_records, len(transport.calls)), (1, 2))
        self.assertEqual((Company.objects.count(), GemiDiscoveryRun.objects.count(),
                          GemiDiscoveryObservation.objects.count()), (0, 0, 0))


class PersistenceTests(LaneTestCase):
    def test_the_run_is_recorded_under_its_own_stream_with_its_window_and_no_cursor(self):
        result, _ = self.lane(active=[[item(9001, incorporationDate="9011-12-09"), item(LOW, TODAY)]])
        run = GemiDiscoveryRun.objects.get()
        self.assertEqual((run.stream, run.mode, run.status, run.pk), (STREAM_INCORPORATION_DATE, INGEST, "success",
                                                                    result.run_id))
        self.assertEqual((run.policy["trigger"], run.policy["as_of"], run.policy["window_start"]),
                         (TRIGGER_OPERATOR, TODAY.isoformat(), YESTERDAY.isoformat()))
        self.assertEqual(run.policy["statistics"]["future_date_records"], 1)
        self.assertEqual([entry["pass"] for entry in run.policy["statistics"]["passes"]], ["active", "inactive"])
        self.assertFalse(GemiDiscoveryCursor.objects.exists())               # stateless: nothing to advance
        self.assertEqual((run.pages_fetched, run.ingested_records, run.new_records), (2, 1, 1))

    def test_a_failed_request_is_a_failed_run_of_this_stream_and_the_next_run_simply_rescans(self):
        failed, _ = self.lane(active=[[item(LOW, TODAY)]], fail=("active",))
        self.assertEqual((failed.status, GemiDiscoveryRun.objects.get().status), ("failed", "failed"))
        self.assertFalse(Company.objects.exists())
        retry, _ = self.lane(active=[[item(LOW, TODAY)]])
        self.assertEqual((retry.status, retry.ingested_records), ("success", 1))

    def test_an_unexpected_write_failure_fails_the_run_and_is_retried_by_the_next_scan(self):
        real = company_writer.create_company_from_search_item

        def write(number, record, **kwargs):
            if number == "5002":
                raise RuntimeError("database hiccup")
            return real(number, record, **kwargs)

        records = [item(5001, TODAY), item(5002, TODAY), item(5003, TODAY)]
        with patch.object(company_writer, "create_company_from_search_item", side_effect=write):
            failed, _ = self.lane(active=[records])
        self.assertEqual((failed.status, failed.stop_reason), ("failed", STOP_INGEST_WRITE_FAILED))
        self.assertEqual(set(Company.objects.values_list("gemi_number", flat=True)), {"5001", "5003"})
        retry, _ = self.lane(active=[records])
        self.assertEqual((retry.status, retry.ingested_records, Company.objects.count()), ("success", 1, 3))

    def test_compaction_is_per_stream_in_both_directions(self):
        # The same-day lane records first evidence for a stored company above the frontier ...
        for number in (1000, 999):
            known_company(number)
        ready_cursor(1000)
        known_company(1001, TODAY)
        self.lane(active=[[item(1001, TODAY)]])
        self.assertEqual(lane_rows()["1001"], (NEW_INCORPORATION, INGEST_ALREADY_LOCAL, True))
        # ... and the frontier lane's own first sighting of it has the very same fingerprint. It is stored.
        client, _ = lanes_client(frontier=[[item(1001, TODAY), item(1000), item(999)]])
        with ENABLED:
            frontier = run_discovery(mode=INGEST, client=client, policy=POLICY, as_of=TODAY, compact_observations=True)
        self.assertEqual(frontier.observations_suppressed, 0)
        stored = GemiDiscoveryObservation.objects.get(run__stream=STREAM_COMPANIES, gemi_number="1001")
        self.assertEqual((stored.classification, stored.ingest_outcome), (NEW_INCORPORATION, INGEST_ALREADY_LOCAL))
        # And the other way: frontier evidence of today does not suppress the same-day lane's first row.
        client, _ = lanes_client(frontier=[[item(1002, TODAY), item(1001, TODAY), item(1000)]])
        with ENABLED:
            run_discovery(mode=INGEST, client=client, policy=POLICY, as_of=TODAY, compact_observations=True)
        result, _ = self.lane(active=[[item(1002, TODAY)]])
        self.assertEqual((result.observations_stored, lane_rows()["1002"]), (1, (KNOWN, "", True)))


# --- the cycle: two lanes, one materialisation -------------------------------------------------------------

class TwoLaneCycleTestCase(IngestionCycleTestCase):
    def two_lanes(self, *frontier, active=(), inactive=(), lane=True, fail=(), **options):
        client, transport = lanes_client(frontier=[self.page(*frontier)], active=[list(active)] if active else [],
                                         inactive=[list(inactive)] if inactive else [], fail=fail)
        with override_settings(GEMI_DISCOVERY_V2_ENABLED=True, GEMI_DISCOVERY_SAMEDAY_LANE_ENABLED=lane):
            return run_ingestion_cycle(client=client, sameday_policy=WIDE_PAGE, **options), transport

    def signals(self, number):
        return CompanySignal.objects.filter(company__gemi_number=str(number), signal_type=NEW_COMPANY)


class SevenMissShapeTests(TwoLaneCycleTestCase):
    def test_the_frontier_lane_alone_never_reaches_a_below_frontier_company_dated_today(self):
        report, transport = self.two_lanes(item(NEW, self.today), active=[item(LOW, self.today)], lane=False)
        self.assertEqual(report.status, OK)
        self.assertTrue(Company.objects.filter(gemi_number=str(NEW)).exists())
        self.assertFalse(Company.objects.filter(gemi_number=str(LOW)).exists())           # the production miss
        self.assertFalse(GemiDiscoveryObservation.objects.filter(gemi_number=str(LOW)).exists())
        self.assertEqual(len(transport.calls), 1)

    def test_the_same_day_lane_finds_it_and_it_becomes_one_company_signal_and_opportunity(self):
        report, transport = self.two_lanes(item(NEW, self.today), active=[item(LOW, self.today)])

        self.assertEqual((report.status, report.failed_phases), (OK, []))
        company = Company.objects.get(gemi_number=str(LOW))
        self.assertEqual(company.incorporation_date, self.today)
        self.assertEqual((report.discovery.ingested_records, report.sameday.ingested_records), (1, 1))
        self.assertEqual(report.sameday.trigger, TRIGGER_CYCLE)
        self.assertEqual((self.signals(LOW).count(), self.signals(NEW).count()), (1, 1))
        self.assertEqual(set(CompanySignal.objects.values_list("mode", flat=True)), {SHADOW})
        self.assertEqual(Opportunity.objects.filter(company=company, radar=self.radar).count(), 1)
        self.assertEqual(set(Opportunity.objects.values_list("latest_signal__mode", flat=True)), {SHADOW})
        self.assertEqual(report.materialisation.signals_created, 2)         # one pass over the union
        # Search pages only: the frontier page and the two same-day passes.
        self.assertEqual((len(transport.calls), lookups(transport), report.gemi_requests), (3, [], 3))
        summary = report.summary()
        self.assertEqual((summary["sameday_companies_created"], summary["sameday_requests"],
                          summary["companies_created"]), (1, 2, 1))
        self.assertIn("SAME-DAY DISCOVERY", "\n".join(report.lines()))

    def test_the_one_materialisation_covers_the_union_of_both_lanes_without_the_catch_up(self):
        report, _ = self.two_lanes(item(NEW, self.today), active=[item(LOW, self.today)], catch_up_hours=0)
        self.assertEqual((report.discovered_numbers, report.catch_up_numbers), (2, 0))
        self.assertEqual((self.signals(LOW).count(), self.signals(NEW).count()), (1, 1))

    def test_one_business_date_is_fixed_at_the_start_and_shared_by_both_lanes(self):
        seen = {}
        real_frontier, real_lane = ingestion_cycle.run_discovery, ingestion_cycle.run_sameday_discovery

        def frontier(**kwargs):
            seen["frontier"] = kwargs["as_of"]
            return real_frontier(**kwargs)

        def lane(**kwargs):
            seen["lane"] = kwargs["as_of"]
            return real_lane(**kwargs)

        with patch.object(ingestion_cycle, "run_discovery", side_effect=frontier), \
                patch.object(ingestion_cycle, "run_sameday_discovery", side_effect=lane):
            self.two_lanes(active=[item(LOW, self.today)])
        self.assertEqual(seen, {"frontier": self.today, "lane": self.today})


class NoDuplicateTests(TwoLaneCycleTestCase):
    def test_a_company_both_lanes_see_is_one_company_one_signal_one_opportunity(self):
        report, _ = self.two_lanes(item(NEW, self.today), active=[item(NEW, self.today)])

        self.assertEqual(Company.objects.filter(gemi_number=str(NEW)).count(), 1)
        self.assertEqual((report.discovery.ingested_records, report.sameday.ingested_records,
                          report.sameday.known_records), (1, 0, 1))
        self.assertEqual(self.signals(NEW).count(), 1)
        self.assertEqual(Opportunity.objects.filter(company__gemi_number=str(NEW)).count(), 1)
        self.assertEqual(len(report.pipeline_runs), 1)
        opportunities = list(Opportunity.objects.order_by("pk").values())

        again, _ = self.two_lanes(item(NEW, self.today), active=[item(NEW, self.today)])
        self.assertEqual((again.status, again.pipeline_runs), (OK, []))
        self.assertEqual((self.signals(NEW).count(), list(Opportunity.objects.order_by("pk").values())),
                         (1, opportunities))

    def test_a_legacy_first_race_yields_already_local_evidence_and_the_signal_exactly_once(self):
        company = imported(LOW, self.today)          # the legacy importer stored it; discovery never saw it
        before = Company.objects.filter(pk=company.pk).values().get()

        report, _ = self.two_lanes(active=[item(LOW, self.today)])

        self.assertEqual(lane_rows(), {str(LOW): (NEW_INCORPORATION, INGEST_ALREADY_LOCAL, True)})
        self.assertEqual(Company.objects.filter(pk=company.pk).values().get(), before)     # never written over
        self.assertEqual((report.sameday.ingested_records, self.signals(LOW).count()), (0, 1))
        self.assertEqual(Opportunity.objects.filter(company=company).count(), 1)

        again, _ = self.two_lanes(active=[item(LOW, self.today)])
        self.assertEqual((again.sameday.known_records, self.signals(LOW).count(),
                          Opportunity.objects.filter(company=company).count()), (1, 1, 1))
        self.assertEqual(again.pipeline_runs, [])

    def test_a_second_cycle_changes_nothing(self):
        self.two_lanes(item(NEW, self.today), active=[item(LOW, self.today)])
        world = self.world(), list(Company.objects.order_by("pk").values())
        again, _ = self.two_lanes(item(NEW, self.today), active=[item(LOW, self.today)])
        self.assertEqual((self.world(), list(Company.objects.order_by("pk").values())), world)
        self.assertEqual((again.sameday.ingested_records, again.sameday.observations_stored), (0, 1))

    def test_a_dry_run_of_both_lanes_writes_nothing(self):
        before = self.everything()
        report, transport = self.two_lanes(item(NEW, self.today), active=[item(LOW, self.today)], dry_run=True)
        self.assertEqual((len(transport.calls), report.sameday.would_ingest_records), (3, 1))
        self.assertEqual(self.everything(), before)


class FlagOffEquivalenceTests(TwoLaneCycleTestCase):
    SUMMARY_KEYS = {
        "status", "dry_run", "gemi_requests", "failed_phases", "discovery_status", "stop_reason", "run_id",
        "examined", "newly_discovered", "companies_created", "would_create", "quarantined_date", "write_failed",
        "observations_stored", "observations_suppressed", "cursor_advanced", "materialisation_scope",
        "signals_created", "signals_existing", "pending_no_company", "pipeline_runs", "opportunities_created",
    }

    def test_with_the_flag_off_the_cycle_is_the_frontier_lane_alone(self):
        calls = {}
        real = ingestion_cycle.run_discovery

        def frontier(**kwargs):
            calls.update(kwargs)
            return real(**kwargs)

        with patch.object(ingestion_cycle, "run_sameday_discovery",
                          side_effect=AssertionError("dormant")) as lane, \
                patch.object(ingestion_cycle, "run_discovery", side_effect=frontier):
            report, transport = self.two_lanes(item(NEW, self.today), active=[item(LOW, self.today)], lane=False)

        lane.assert_not_called()
        # The frontier lane is called exactly as before the second lane existed: it resolves its own date.
        self.assertIsNone(calls.pop("as_of"))
        self.assertEqual(set(calls), {"mode", "dry_run", "max_pages", "policy", "client", "compact_observations",
                                      "max_wait"})
        self.assertEqual((calls["mode"], calls["compact_observations"], calls["dry_run"]), (INGEST, True, False))
        self.assertEqual((report.status, report.sameday, report.sameday_enabled), (OK, None, False))
        self.assertEqual(set(report.summary()), self.SUMMARY_KEYS)
        self.assertEqual([query.get("resultsSortBy") for query in queries(transport)], ["-arGemi"])
        self.assertEqual(set(GemiDiscoveryRun.objects.values_list("stream", flat=True)), {STREAM_COMPANIES})
        self.assertNotIn("SAME-DAY", "\n".join(report.lines()))
        self.assertNotIn("same-day", "\n".join(ingestion_health()))


class LaneIndependenceTests(TwoLaneCycleTestCase):
    def test_a_same_day_failure_is_that_stream_s_failed_phase_and_the_frontier_lane_is_unaffected(self):
        report, _ = self.two_lanes(item(NEW, self.today), active=[item(LOW, self.today)], fail=("active",))

        self.assertEqual(report.status, FAILED_CYCLE)
        self.assertEqual([name for name, _ in report.failed_phases], [SAMEDAY_PHASE])
        self.assertEqual((report.discovery.status, report.discovery.cursor_advanced), ("success", True))
        self.assertEqual(self.signals(NEW).count(), 1)                    # the frontier lane's work is complete
        runs = dict(GemiDiscoveryRun.objects.values_list("stream", "status"))
        self.assertEqual(runs, {STREAM_COMPANIES: "success", STREAM_INCORPORATION_DATE: "failed"})

        retry, _ = self.two_lanes(item(NEW, self.today), active=[item(LOW, self.today)])
        self.assertEqual((retry.status, retry.sameday.ingested_records, self.signals(LOW).count()), (OK, 1, 1))

    def test_a_frontier_failure_does_not_stop_the_same_day_lane(self):
        report, _ = self.two_lanes(item(NEW, self.today), active=[item(LOW, self.today)], fail=("frontier",))
        self.assertEqual([name for name, _ in report.failed_phases], ["discovery"])
        self.assertEqual((report.sameday.status, report.sameday.ingested_records), ("success", 1))
        self.assertEqual(self.signals(LOW).count(), 1)


# --- alerts, per stream -----------------------------------------------------------------------------------

class SameDayAlertTests(AlertTestCase):
    def lane_task(self, *frontier, active=(), fail=()):
        client, _ = lanes_client(frontier=[self.page(*frontier)], active=[list(active)] if active else [], fail=fail)
        with BOTH_FLAGS, patch("gemiapp.ingestion.discovery.get_gemi_client", return_value=client), \
                patch("gemiapp.ingestion.sameday_discovery.get_gemi_client", return_value=client), \
                override_settings(GEMI_DISCOVERY_SAMEDAY_OLDER_BOUNDARY_RECORDS=2):
            return run_gemi_ingestion_cycle_task()

    def test_one_isolated_same_day_failure_alerts_nobody(self):
        summary = self.lane_task(active=[item(LOW, self.today)], fail=("active",))
        self.assertEqual((summary["status"], summary["sameday_status"], summary["alerts"]["alerted"]),
                         ("failed", "failed", []))
        self.assertEqual(mail.outbox, [])

    def test_a_persistent_same_day_failure_alerts_once_as_its_own_kind_and_recovers_once(self):
        for _ in range(2):
            self.lane_task(active=[item(LOW, self.today)], fail=("active",))
        self.assertEqual(alert_mails(), [])
        third = self.lane_task(active=[item(LOW, self.today)], fail=("active",))
        self.assertEqual(third["alerts"]["alerted"], [SAMEDAY_REPEATED_FAILURES])
        self.assertEqual(len(alert_mails()), 1)
        self.assertIn("same-day", alert_mails()[0].body)
        fourth = self.lane_task(active=[item(LOW, self.today)], fail=("active",))      # no spam
        self.assertEqual((fourth["alerts"]["alerted"], len(alert_mails())), ([], 1))
        self.assertEqual(open_alerts(), [SAMEDAY_REPEATED_FAILURES])                    # never the frontier's kinds

        recovered = self.lane_task(active=[item(LOW, self.today)])
        self.assertEqual((recovered["status"], recovered["alerts"]["recovered"]), ("ok", [SAMEDAY_REPEATED_FAILURES]))
        self.assertEqual((len(recovery_mails()), open_alerts()), (1, []))

    def test_a_date_order_anomaly_alerts_on_the_first_occurrence(self):
        disorder = [item(5001, self.today), item(8001, OLDER), item(8002, OLDER), item(5002, self.today)]
        summary = self.lane_task(active=disorder)
        self.assertEqual((summary["sameday_status"], summary["alerts"]["alerted"]),
                         ("anomaly", [SAMEDAY_DATE_ORDER_ANOMALY]))
        self.assertEqual(len(alert_mails()), 1)
        self.assertNotIn("ΔΟΚΙΜΑΣΤΙΚΗ", alert_mails()[0].body)
        again = self.lane_task(active=disorder)
        self.assertEqual((again["alerts"]["alerted"], len(alert_mails())), ([], 1))

    def test_a_same_day_lane_without_a_success_for_hours_is_reported_as_no_progress(self):
        self.lane_task(active=[item(LOW, self.today)])
        GemiDiscoveryRun.objects.filter(stream=STREAM_INCORPORATION_DATE).update(
            started_at=timezone.now() - timedelta(hours=3))
        summary = self.lane_task(active=[item(LOW, self.today)], fail=("active",))
        self.assertEqual(summary["alerts"]["alerted"], [SAMEDAY_NO_PROGRESS])

    def test_a_same_day_failure_is_never_reported_as_a_frontier_or_phase_failure(self):
        for _ in range(3):
            summary = self.lane_task(active=[item(LOW, self.today)], fail=("active",))
        self.assertEqual(summary["alerts"]["open"], [SAMEDAY_REPEATED_FAILURES])


# --- parity: two lanes, the union, and evidence that has to be timely --------------------------------------

class TwoLaneParityTests(ParityTestCase):
    def lane(self, *records, **options):
        client, _ = lanes_client(active=[list(records)])
        options.setdefault("policy", WIDE_PAGE)
        with BOTH_FLAGS:
            return run_sameday_discovery(client=client, as_of=TODAY, **options)

    def test_the_production_miss_without_the_lane_and_no_miss_with_it(self):
        self.legacy_import(item(500, TODAY))                    # legacy stored a below-frontier company of today
        self.ingest(self.page(item(1001, TODAY)))               # the frontier lane succeeds and never sees it
        without = self.report()
        self.assertEqual((without.verdict, without.legacy_only, without.sameday_runs), ("MISS", ["500"], 0))

        self.lane(item(500, TODAY), item(1001, TODAY))
        report = self.report()

        self.assertEqual((report.verdict, report.legacy_only), ("OK", []))
        self.assertEqual(report.legacy_first_confirmed, 1)                       # a race the lane confirmed
        self.assertEqual(report.frontier_gaps_recovered, ["500"])
        self.assertEqual((report.found_by_frontier_only, report.found_by_sameday_only, report.found_by_both),
                         (0, 1, 1))
        self.assertEqual((report.sameday_runs, report.sameday_successful_runs, report.sameday_pages), (1, 1, 2))
        self.assertEqual((report.sameday_created, report.sameday_already_local), (0, 1))
        self.assertEqual((report.ingest_runs, report.unified_created, report.frontier_pages), (1, 1, 1))
        text = "\n".join(report.lines())
        self.assertIn("FRONTIER GAPS RECOVERED BY THE SAME-DAY LANE=1: ['500']", text)
        self.assertIn("SAME-DAY LANE (-incorporationDate): runs=1", text)

    def test_a_company_only_the_same_day_lane_could_create_is_measured_as_a_recovered_gap(self):
        self.ingest(self.page(item(1001, TODAY)))
        self.lane(item(500, TODAY), item(9001, incorporationDate="9011-12-09"))
        report = self.report()
        self.assertEqual((report.verdict, report.sameday_created, report.unified_first), ("OK", 1, 2))
        self.assertEqual((report.frontier_gaps_recovered, report.sameday_future_skipped), (["500"], 1))

    def test_late_evidence_never_turns_an_old_miss_into_ok(self):
        day = TODAY - timedelta(days=5)                         # the historical day: 2026-10-08 in production
        self.legacy_import(item(500, day), target=day)
        self.ingest(self.page(item(1001, TODAY)))
        # That day had its own frontier run; and one has succeeded since legacy stored the company.
        GemiDiscoveryRun.objects.update(started_at=timezone.now() - timedelta(days=5))
        self.ingest(self.page(item(1002, TODAY), item(1001, TODAY)))
        self.assertEqual((self.report(day).verdict, self.report(day).legacy_only), ("MISS", ["500"]))

        # Days later something does record it (here: a scan with a wide window). It is genuine evidence of now.
        self.lane(item(500, day), policy=SameDayPolicy(page_size=50, lookback_days=10, older_boundary_records=2))
        self.assertTrue(GemiDiscoveryObservation.objects.filter(gemi_number="500").exists())
        report = self.report(day)

        self.assertEqual((report.verdict, report.legacy_only), ("MISS", ["500"]))
        self.assertEqual((report.late_evidence, report.backfill_evidence, report.frontier_gaps_recovered),
                         (["500"], [], []))
        self.assertIn("recovered late", "\n".join(report.lines()))

    def test_operator_backfill_evidence_is_reported_separately_and_never_counted(self):
        self.legacy_import(item(500, TODAY))
        self.ingest(self.page(item(1001, TODAY)))
        self.lane(item(500, TODAY), trigger=TRIGGER_BACKFILL)    # even a backfill run inside the day's horizon
        report = self.report()
        self.assertEqual((report.verdict, report.legacy_only), ("MISS", ["500"]))
        self.assertEqual((report.backfill_evidence, report.late_evidence), (["500"], []))
        self.assertIn("operator backfill evidence", "\n".join(report.lines()))

    def test_same_day_write_failures_and_date_order_anomalies_are_reported(self):
        self.ingest(self.page(item(1001, TODAY)))
        with patch.object(company_writer, "create_company_from_search_item", side_effect=RuntimeError("boom")):
            self.lane(item(500, TODAY))
        client, _ = lanes_client(active=[[item(501, TODAY), item(8001, OLDER), item(8002, OLDER), item(502, TODAY)]])
        with BOTH_FLAGS:
            run_sameday_discovery(client=client, as_of=TODAY, policy=WIDE_PAGE)
        report = self.report()
        self.assertEqual((report.sameday_runs, report.sameday_failed_runs, report.sameday_anomaly_runs), (2, 1, 1))
        self.assertEqual((report.sameday_write_failed, report.sameday_date_order_anomalies), (1, 1))

    def test_a_day_with_only_same_day_runs_is_measured(self):
        self.lane(item(500, TODAY))
        self.assertEqual(self.report().verdict, "OK")
