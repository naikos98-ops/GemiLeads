"""Tests for the dormant unified ingestion path: the shared date-safe Company writer
(gemiapp.ingestion.company_writer), Discovery's safe ingest and compact observations
(gemiapp.ingestion.discovery) and the lean ingestion cycle (gemiapp.ingestion_cycle).

No test reaches the network: every client is a fake transport on a fake clock.
"""

import math
from datetime import date, timedelta
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.core.cache import caches
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone

from . import apps as gemi_apps
from . import ingestion_cycle
from .company_signals import SHADOW
from .ingestion import company_writer
from .ingestion import discovery as discovery_module
from .ingestion.discovery import (
    INGEST,
    INGEST_ALREADY_LOCAL,
    INGEST_CREATED,
    INGEST_QUARANTINED_DATE,
    INGEST_WRITE_FAILED,
    INVALID_DATE,
    KNOWN,
    LATE_PUBLICATION,
    NEW_INCORPORATION,
    ORDERING_VIOLATION,
    STOP_INGEST_WRITE_FAILED,
    DiscoveryPolicy,
    get_cursor,
    run_discovery,
)
from .ingestion.rate_budget import MAX_REQUESTS_PER_MINUTE, BudgetConfig, GemiLane
from .ingestion_cycle import (
    FAILED_CYCLE,
    LOCK_KEY,
    MATERIALISATION_PHASE,
    OK,
    SKIPPED_LOCKED,
    IngestionCycleRefused,
    claim_cycle_lock,
    run_ingestion_cycle,
)
from .models import (
    Company, CompanyActivity, CompanySignal, CompanySnapshot, DigestDelivery, GemiDiscoveryObservation,
    GemiDiscoveryRun, Opportunity, OpportunityTask, OrganizationNotification, RadarMatch, UserCompanyLead,
)
from .organization_access import (
    OrganizationAccessDenied, get_authorized_company_opportunity_page, get_authorized_workspace_dashboard,
    get_authorized_workspace_opportunities,
)
from .services import company_defaults, import_for_date
from .tasks import run_gemi_ingestion_cycle_task
from .test_g4_shadow_cycle import BELOW, FRONTIER, LATE, NEW, OLD, CycleTestCase
from .test_gemi_client import NoNetworkMixin, make_client
from .test_gemi_discovery import AS_OF, item, known_company, paged_client, ready_cursor
from .test_new_company_signals import discovery_run, observation

ENABLED = override_settings(GEMI_DISCOVERY_V2_ENABLED=True)
POLICY = DiscoveryPolicy(page_size=4, max_pages=5, overlap_known_records=2, min_overlap_pages=1)
TODAY = date.today()


def lookups(transport):
    """The per-company requests a transport saw: every URL that asks for one arGemi."""
    return [call["url"] for call in transport.calls if "arGemi=" in call["url"]]


def outcomes():
    return {row.gemi_number: row.ingest_outcome for row in GemiDiscoveryObservation.objects.order_by("id")}


# --- phase 1: the shared writer --------------------------------------------------------------------------

class SharedWriterTests(NoNetworkMixin, TestCase):
    def write(self, record, number="3001", **options):
        return company_writer.create_company_from_search_item(number, record, **options)

    def test_a_valid_historical_date_is_stored_exactly_with_the_activities(self):
        outcome = self.write(item(3001, "2019-03-07"))
        company = Company.objects.get(gemi_number="3001")
        self.assertEqual((outcome.status, outcome.stored_as_today), (company_writer.CREATED, False))
        self.assertEqual(company.incorporation_date, date(2019, 3, 7))
        self.assertEqual(company.raw_data["arGemi"], "3001")
        self.assertEqual(outcome.activities_created, CompanyActivity.objects.filter(company=company).count())
        self.assertGreater(outcome.activities_created, 0)

    def test_a_genuine_date_of_today_is_stored_as_today(self):
        outcome = self.write(item(3001, TODAY))
        self.assertEqual((outcome.status, outcome.stored_as_today), (company_writer.CREATED, True))
        self.assertEqual(Company.objects.get(gemi_number="3001").incorporation_date, TODAY)

    def test_every_date_company_defaults_would_clamp_is_refused_and_nothing_is_written(self):
        unsafe = {
            company_writer.DATE_MISSING: [None, ""],
            company_writer.DATE_UNREADABLE: ["not-a-date", "2026-13-45", "09/09/2026"],
            company_writer.DATE_BEFORE_1900: ["1899-12-31", "1821-01-01"],
            company_writer.DATE_FUTURE: [(TODAY + timedelta(days=1)).isoformat(), "3006-03-03", "9011-12-09"],
        }
        for problem, values in unsafe.items():
            for value in values:
                with self.subTest(value=value):
                    record = item(3001, incorporationDate=value)
                    # The premise: the legacy defaults would have stored today for this record.
                    self.assertEqual(company_defaults(record)["incorporation_date"], TODAY)
                    outcome = self.write(record)
                    self.assertEqual((outcome.status, outcome.date_problem), (company_writer.REFUSED_DATE, problem))
        self.assertEqual((Company.objects.count(), CompanyActivity.objects.count()), (0, 0))

    def test_the_writer_not_company_defaults_decides_the_stored_date(self):
        # Whatever the legacy defaults compute for the date -- here a clamp to another day -- the row is stored
        # with the validated source date. (Athens midnight: gemiapp.test_sameday_discovery.)
        real = company_defaults

        def clamping(record):
            return {**real(record), "incorporation_date": date(2001, 1, 1)}

        with patch("gemiapp.services.company_defaults", side_effect=clamping):
            outcome = self.write(item(3001, "2019-03-07"))
        self.assertEqual(outcome.status, company_writer.CREATED)
        self.assertEqual(Company.objects.get(gemi_number="3001").incorporation_date, date(2019, 3, 7))

    def test_an_existing_company_is_never_updated(self):
        stored = known_company(3001)
        Company.objects.filter(pk=stored.pk).update(name="ΟΠΩΣ ΗΤΑΝ", raw_data={"kept": True})
        before = list(Company.objects.values())
        with patch.object(Company.objects, "update_or_create", side_effect=AssertionError("never an upsert")):
            outcome = self.write(item(3001, "2019-03-07"))
        self.assertEqual(outcome.status, company_writer.EXISTS)
        self.assertEqual(list(Company.objects.values()), before)
        self.assertFalse(CompanyActivity.objects.exists())

    def test_losing_the_insert_race_leaves_the_winner_untouched(self):
        # Another writer's row is committed, but this writer's existence check does not see it yet -- as under
        # PostgreSQL before the other transaction commits. Only the unique gemi_number stops the insert.
        Company.objects.bulk_create([Company(gemi_number="3001", name="Ο ΑΛΛΟΣ WRITER",
                                             incorporation_date=date(2026, 9, 1))])
        real_filter = Company.objects.filter
        state = {"blind": True}

        def blind_once(*args, **kwargs):
            if kwargs.get("gemi_number") == "3001" and state["blind"]:
                state["blind"] = False
                return Company.objects.none()
            return real_filter(*args, **kwargs)

        with patch.object(Company.objects, "filter", side_effect=blind_once):
            outcome = self.write(item(3001, "2019-03-07"))
        self.assertEqual(outcome.status, company_writer.EXISTS)
        self.assertEqual(Company.objects.get(gemi_number="3001").name, "Ο ΑΛΛΟΣ WRITER")
        self.assertFalse(CompanyActivity.objects.exists())

    def test_an_unrelated_integrity_error_is_not_swallowed(self):
        with patch.object(Company.objects, "create", side_effect=IntegrityError("something else")):
            with self.assertRaises(IntegrityError):
                self.write(item(3001, "2019-03-07"))

    def test_a_failing_activity_sync_rolls_the_company_back(self):
        with patch("gemiapp.services.sync_canonical_company_activities", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.write(item(3001, "2019-03-07"))
        self.assertEqual((Company.objects.count(), CompanyActivity.objects.count()), (0, 0))

    def test_a_record_of_another_company_is_never_written_under_this_number(self):
        with self.assertRaises(ValueError):
            self.write(item(3002, "2019-03-07"), number="3001")
        self.assertFalse(Company.objects.exists())

    def test_a_dry_run_reports_and_writes_nothing(self):
        self.assertEqual(self.write(item(3001, "2019-03-07"), dry_run=True).status, company_writer.WOULD_CREATE)
        known_company(3002)
        self.assertEqual(self.write(item(3002, "2019-03-07"), number="3002", dry_run=True).status,
                         company_writer.EXISTS)
        self.assertEqual(Company.objects.count(), 1)

    def test_hydration_uses_this_writer(self):
        from . import pending_company_hydration

        self.assertIs(pending_company_hydration.company_writer, company_writer)
        self.assertFalse(hasattr(pending_company_hydration, "_create_only"))


# --- phase 2: discovery ingest ---------------------------------------------------------------------------

class DiscoveryIngestTestCase(NoNetworkMixin, TestCase):
    def setUp(self):
        super().setUp()
        for number in (1000, 999):
            known_company(number)
        ready_cursor(1000)

    def ingest(self, pages, **options):
        client, transport = paged_client(pages, failure_after=options.pop("failure_after", None))
        options.setdefault("policy", POLICY)
        with ENABLED:
            return run_discovery(mode=INGEST, client=client, as_of=TODAY, **options), transport


class RequestEconomyTests(DiscoveryIngestTestCase):
    def test_one_search_page_creates_every_new_company_with_zero_per_company_requests(self):
        result, transport = self.ingest([[item(1003, TODAY), item(1002, TODAY), item(1001, "2024-02-02"),
                                          item(1000)], [item(999)]], policy=DiscoveryPolicy(
            page_size=4, max_pages=5, overlap_known_records=1, min_overlap_pages=1))

        self.assertEqual(len(transport.calls), 1)               # the one page...
        self.assertEqual(lookups(transport), [])                # ...and not a single arGemi lookup
        self.assertEqual((result.status, result.ingested_records, result.pages_fetched), ("success", 3, 1))
        for number in ("1003", "1002", "1001"):
            company = Company.objects.get(gemi_number=number)
            self.assertEqual(company.raw_data["arGemi"], number)     # the search payload itself
            self.assertTrue(CompanyActivity.objects.filter(company=company).exists())
        self.assertEqual(Company.objects.get(gemi_number="1001").incorporation_date, date(2024, 2, 2))
        self.assertEqual(Company.objects.get(gemi_number="1003").incorporation_date, TODAY)

    def test_hydration_is_never_reached_from_discovery_ingest(self):
        with patch("gemiapp.pending_company_hydration.hydrate_pending_companies",
                   side_effect=AssertionError("ingest must not hydrate")), \
                patch("gemiapp.ingestion.discovery.get_gemi_client",
                      side_effect=AssertionError("only the injected client")):
            result, transport = self.ingest([[item(1002, TODAY), item(1001, TODAY), item(1000), item(999)]])
        self.assertEqual((result.ingested_records, len(transport.calls), lookups(transport)), (2, 1, []))


class IngestOutcomeTests(DiscoveryIngestTestCase):
    def test_each_record_gets_its_outcome_and_keeps_its_classification(self):
        stored = known_company(1004)                              # above the frontier, already stored
        Company.objects.filter(pk=stored.pk).update(name="ΟΠΩΣ ΤΗΝ ΕΓΡΑΨΕ Ο IMPORTER", raw_data={"kept": True})
        before = list(Company.objects.filter(gemi_number="1004").values())
        tomorrow = (TODAY + timedelta(days=1)).isoformat()        # valid for A3, clamped by company_defaults
        result, _ = self.ingest([
            [item(1005, incorporationDate=None), item(1004, TODAY), item(1003, tomorrow), item(1002, "2021-05-05")],
            [item(1001, TODAY), item(1000), item(999)],
        ])

        self.assertEqual(outcomes(), {
            "1005": INGEST_QUARANTINED_DATE, "1004": INGEST_ALREADY_LOCAL, "1003": INGEST_QUARANTINED_DATE,
            "1002": INGEST_CREATED, "1001": INGEST_CREATED, "1000": "", "999": "",
        })
        classes = {row.gemi_number: row.classification for row in GemiDiscoveryObservation.objects.all()}
        self.assertEqual((classes["1005"], classes["1003"], classes["1002"], classes["1000"]),
                         (INVALID_DATE, NEW_INCORPORATION, LATE_PUBLICATION, KNOWN))
        # Quarantined: no Company row at all, and never a clamped "today".
        self.assertFalse(Company.objects.filter(gemi_number__in=["1005", "1003"]).exists())
        self.assertEqual(list(Company.objects.filter(gemi_number="1004").values()), before)
        self.assertEqual((result.ingested_records, result.quarantined_date_records, result.ingest_failed_records),
                         (2, 2, 0))
        # Quarantine is evidence, not an anomaly: the frontier moves exactly as in a shadow run.
        self.assertEqual((result.status, get_cursor().high_water_mark), ("success", "1005"))

    def test_an_invalid_date_or_already_stored_record_is_never_handed_to_the_writer(self):
        known_company(1003)
        with patch.object(company_writer, "create_company_from_search_item",
                          wraps=company_writer.create_company_from_search_item) as writer:
            self.ingest([[item(1004, incorporationDate=None), item(1003, TODAY), item(1002, TODAY), item(1001, TODAY)],
                         [item(1000), item(999)]])
        self.assertEqual([call.args[0] for call in writer.call_args_list], ["1002", "1001"])

    def test_a_quarantined_record_is_created_once_its_payload_becomes_valid(self):
        self.ingest([[item(1001, incorporationDate=None), item(1000), item(999)]])
        self.assertFalse(Company.objects.filter(gemi_number="1001").exists())
        result, _ = self.ingest([[item(1001, "2022-02-02"), item(1000), item(999)]])
        self.assertEqual((result.ingested_records, Company.objects.get(gemi_number="1001").incorporation_date),
                         (1, date(2022, 2, 2)))

    def test_a_dry_run_requests_the_pages_and_writes_nothing(self):
        before = (Company.objects.count(), GemiDiscoveryRun.objects.count(), get_cursor().high_water_mark)
        result, transport = self.ingest([[item(1002, TODAY), item(1001, TODAY), item(1000), item(999)]],
                                        dry_run=True)
        self.assertEqual((result.would_ingest_records, result.ingested_records, len(transport.calls)), (2, 0, 1))
        self.assertEqual((Company.objects.count(), GemiDiscoveryRun.objects.count(), get_cursor().high_water_mark),
                         before)

    def test_ingest_stays_refused_while_the_flag_is_off(self):
        client, transport = paged_client([[item(1001, TODAY), item(1000), item(999)]])
        with self.assertRaises(ValueError):
            run_discovery(mode=INGEST, client=client, policy=POLICY, as_of=TODAY)
        self.assertEqual((transport.calls, Company.objects.count()), ([], 2))


class PagingTests(DiscoveryIngestTestCase):
    def test_more_than_two_hundred_new_companies_are_created_across_pages(self):
        policy = DiscoveryPolicy(page_size=200, max_pages=10, overlap_known_records=2, min_overlap_pages=1)
        records = [item(number, "2026-09-01") for number in range(1450, 1000, -1)] + [item(1000), item(999)]
        pages = [records[start:start + 200] for start in range(0, len(records), 200)]

        result, transport = self.ingest(pages, policy=policy)

        self.assertEqual((result.status, result.pages_fetched, result.ingested_records), ("success", 3, 450))
        self.assertEqual((len(transport.calls), lookups(transport)), (3, []))
        self.assertEqual(Company.objects.count(), 452)
        self.assertEqual(get_cursor().high_water_mark, "1450")

    def test_a_failure_on_a_later_page_keeps_the_first_page_and_the_frontier(self):
        pages = [[item(1006, TODAY), item(1005, TODAY), item(1004, TODAY), item(1003, TODAY)],
                 [item(1002, TODAY), item(1001, TODAY), item(1000), item(999)]]
        failed, _ = self.ingest(pages, failure_after=1)

        self.assertEqual((failed.status, failed.ingested_records, failed.cursor_advanced), ("failed", 4, False))
        self.assertEqual((get_cursor().high_water_mark, get_cursor().consecutive_failures), ("1000", 1))
        self.assertEqual(Company.objects.count(), 6)
        # The evidence of the page that did arrive is stored with the failed run.
        self.assertEqual({number: outcomes()[number] for number in ("1006", "1003")},
                         {"1006": INGEST_CREATED, "1003": INGEST_CREATED})

        again, _ = self.ingest(pages)                              # the next run repeats the same window
        self.assertEqual((again.status, again.ingested_records, again.rediscovered_local_records),
                         ("success", 2, 4))
        self.assertEqual((Company.objects.count(), get_cursor().high_water_mark), (8, "1006"))

    def test_an_ordering_anomaly_freezes_the_cursor(self):
        result, _ = self.ingest([[item(1001, TODAY), item(1002, TODAY), item(1000), item(999)]])
        self.assertEqual((result.status, [entry["kind"] for entry in result.blocking_anomalies]),
                         ("anomaly", [ORDERING_VIOLATION]))
        cursor = get_cursor()
        self.assertEqual((cursor.high_water_mark, cursor.status, result.cursor_advanced), ("1000", "anomaly", False))
        self.assertEqual(Company.objects.filter(gemi_number__in=["1001", "1002"]).count(), 2)   # no duplicates

    def test_a_page_repeated_by_offset_drift_creates_nothing_twice(self):
        first = [item(1004, TODAY), item(1003, TODAY), item(1002, TODAY), item(1001, TODAY)]
        result, _ = self.ingest([first, [item(1002, TODAY), item(1001, TODAY), item(1000), item(999)]])
        self.assertEqual((result.status, result.duplicate_records, result.ingested_records), ("success", 2, 4))
        self.assertEqual(Company.objects.count(), 6)
        self.assertEqual(GemiDiscoveryObservation.objects.filter(gemi_number="1002").count(), 1)

    def test_a_rerun_creates_nothing_and_changes_no_company(self):
        pages = [[item(1002, TODAY), item(1001, "2023-03-03"), item(1000), item(999)]]
        self.ingest(pages)
        before = list(Company.objects.order_by("pk").values())
        activities = CompanyActivity.objects.count()
        again, _ = self.ingest(pages)
        self.assertEqual((again.ingested_records, again.new_records), (0, 0))
        self.assertEqual(list(Company.objects.order_by("pk").values()), before)
        self.assertEqual(CompanyActivity.objects.count(), activities)


class UnexpectedWriteFailureTests(DiscoveryIngestTestCase):
    """An unexpected Company/activity persistence error is a failure of the run, never a fact about the record:
    the frontier does not move, and the next run retries from it."""

    # Two pages above the frontier (1000): the failure is on the second one.
    PAGES = [[item(1008, TODAY), item(1007, TODAY), item(1006, "2023-03-03"), item(1005, TODAY)],
             [item(1004, TODAY), item(1003, TODAY), item(1002, TODAY), item(1001, TODAY)],
             [item(1000), item(999)]]

    def failing(self, *numbers, error=RuntimeError("database hiccup")):
        real = company_writer.create_company_from_search_item

        def write(number, record, **kwargs):
            if number in numbers:
                raise error
            return real(number, record, **kwargs)

        return patch.object(company_writer, "create_company_from_search_item", side_effect=write)

    def test_a_the_cursor_does_not_advance_and_no_further_page_is_fetched(self):
        with self.failing("1003"):
            result, transport = self.ingest(self.PAGES)

        self.assertEqual((result.status, result.stop_reason, result.cursor_advanced),
                         ("failed", STOP_INGEST_WRITE_FAILED, False))
        cursor = get_cursor()
        self.assertEqual((cursor.high_water_mark, cursor.high_water_mark_value, cursor.consecutive_failures),
                         ("1000", 1000, 1))
        self.assertEqual(cursor.status, "ready")                    # a failure, not an ordering anomaly
        self.assertEqual(len(transport.calls), 2)                   # page 3 was never requested
        self.assertIn("frontier was not advanced", result.error_message)
        stored = GemiDiscoveryRun.objects.get()
        self.assertEqual((stored.status, stored.cursor_advanced, stored.resulting_high_water_mark),
                         ("failed", False, "1000"))
        # The evidence is kept: the failure itself, and the rest of that page, which was still written.
        self.assertEqual({number: outcomes()[number] for number in ("1004", "1003", "1002", "1001")}, {
            "1004": INGEST_CREATED, "1003": INGEST_WRITE_FAILED, "1002": INGEST_CREATED, "1001": INGEST_CREATED})
        self.assertFalse(Company.objects.filter(gemi_number="1003").exists())
        self.assertEqual((result.ingested_records, result.ingest_failed_records), (7, 1))

    def test_a_failing_activity_sync_is_such_a_failure_and_leaves_no_partial_company(self):
        from .services import sync_canonical_company_activities as real

        def sync(company, activities, **kwargs):
            if company.gemi_number == "1007":
                raise RuntimeError("activity write failed")
            return real(company, activities, **kwargs)

        with patch("gemiapp.services.sync_canonical_company_activities", side_effect=sync):
            result, _ = self.ingest(self.PAGES)
        self.assertEqual((result.status, get_cursor().high_water_mark), ("failed", "1000"))
        self.assertEqual(outcomes()["1007"], INGEST_WRITE_FAILED)
        self.assertFalse(Company.objects.filter(gemi_number="1007").exists())            # rolled back with it
        self.assertFalse(CompanyActivity.objects.filter(company__gemi_number="1007").exists())

    def test_b_the_next_run_retries_the_affected_record_from_the_unchanged_frontier(self):
        with self.failing("1003"):
            self.ingest(self.PAGES)
        with patch.object(company_writer, "create_company_from_search_item",
                          wraps=company_writer.create_company_from_search_item) as writer:
            result, _ = self.ingest(self.PAGES)

        self.assertEqual([call.args[0] for call in writer.call_args_list], ["1003"])   # retried, and only it
        self.assertEqual((result.status, result.ingested_records, result.ingest_failed_records), ("success", 1, 0))
        self.assertTrue(Company.objects.filter(gemi_number="1003", incorporation_date=TODAY).exists())
        self.assertEqual((get_cursor().high_water_mark, get_cursor().consecutive_failures), ("1008", 0))
        latest = GemiDiscoveryObservation.objects.filter(gemi_number="1003").order_by("id").last()
        self.assertEqual((latest.classification, latest.ingest_outcome), (NEW_INCORPORATION, INGEST_CREATED))

    def test_b_a_record_that_keeps_failing_keeps_the_frontier_where_it_is(self):
        for expected_failures in (1, 2, 3):
            with self.failing("1003"):
                result, _ = self.ingest(self.PAGES)
            self.assertEqual((result.status, get_cursor().high_water_mark, get_cursor().consecutive_failures),
                             ("failed", "1000", expected_failures))
        self.assertEqual(Company.objects.count(), 9)                 # the seven others once, plus the two known

    def test_c_companies_written_before_the_failure_stay_and_are_never_written_twice(self):
        with self.failing("1003"):
            self.ingest(self.PAGES)
        written = list(Company.objects.exclude(gemi_number="1003").order_by("pk").values())
        activities = CompanyActivity.objects.count()
        self.assertEqual(len(written), 9)

        again, _ = self.ingest(self.PAGES)

        self.assertEqual(list(Company.objects.exclude(gemi_number="1003").order_by("pk").values()), written)
        self.assertEqual(CompanyActivity.objects.exclude(company__gemi_number="1003").count(), activities)
        self.assertEqual((again.rediscovered_local_records, again.ingested_records), (7, 1))
        self.assertEqual({number: outcomes()[number] for number in ("1008", "1006", "1001")},   # latest evidence
                         {"1008": INGEST_ALREADY_LOCAL, "1006": INGEST_ALREADY_LOCAL, "1001": INGEST_ALREADY_LOCAL})
        self.assertEqual(Company.objects.get(gemi_number="1006").incorporation_date, date(2023, 3, 3))

    def test_d_a_quarantined_date_still_does_not_freeze_the_cursor(self):
        future = (TODAY + timedelta(days=1)).isoformat()
        result, transport = self.ingest([
            [item(1004, incorporationDate=None), item(1003, future), item(1002, "1850-01-01"), item(1001, TODAY)],
            [item(1000), item(999)]])
        self.assertEqual({number: outcomes()[number] for number in ("1004", "1003", "1002", "1001")}, {
            "1004": INGEST_QUARANTINED_DATE, "1003": INGEST_QUARANTINED_DATE, "1002": INGEST_QUARANTINED_DATE,
            "1001": INGEST_CREATED})
        self.assertEqual((result.status, result.quarantined_date_records, result.ingest_failed_records),
                         ("success", 3, 0))
        self.assertEqual((get_cursor().high_water_mark, result.cursor_advanced, len(transport.calls)),
                         ("1004", True, 2))

    def test_e_an_insert_race_still_does_not_freeze_the_cursor(self):
        # Another writer's row exists, but neither Discovery's page lookup nor the writer's own check sees it:
        # only the unique gemi_number stops the insert, and the shared writer turns that into a safe skip.
        Company.objects.bulk_create([Company(gemi_number="1002", name="Ο ΑΛΛΟΣ WRITER",
                                             incorporation_date=date(2026, 9, 1))])
        real_known, real_filter = discovery_module._known_numbers, Company.objects.filter
        state = {"blind": True}

        def blind_once(*args, **kwargs):
            if kwargs.get("gemi_number") == "1002" and state["blind"]:
                state["blind"] = False
                return Company.objects.none()
            if "1002" in (kwargs.get("gemi_number__in") or ()):
                return real_filter(*args, **kwargs).exclude(gemi_number="1002")
            return real_filter(*args, **kwargs)

        with patch.object(discovery_module, "_known_numbers", side_effect=lambda numbers: real_known(numbers) - {"1002"}), \
                patch.object(Company.objects, "filter", side_effect=blind_once):
            result, _ = self.ingest([[item(1002, TODAY), item(1001, TODAY), item(1000), item(999)]])

        self.assertFalse(state["blind"])                             # the insert really was attempted
        self.assertEqual((outcomes()["1002"], outcomes()["1001"]), (INGEST_ALREADY_LOCAL, INGEST_CREATED))
        self.assertEqual((result.status, result.ingest_failed_records, result.cursor_advanced), ("success", 0, True))
        self.assertEqual(get_cursor().high_water_mark, "1002")
        self.assertEqual(Company.objects.get(gemi_number="1002").name, "Ο ΑΛΛΟΣ WRITER")

    def test_f_neither_the_failure_nor_the_retry_issues_a_per_company_lookup(self):
        with patch("gemiapp.pending_company_hydration.hydrate_pending_companies",
                   side_effect=AssertionError("recovery must not need hydration")):
            with self.failing("1003"):
                _, failed_transport = self.ingest(self.PAGES)
            _, retry_transport = self.ingest(self.PAGES)
        self.assertEqual((lookups(failed_transport), lookups(retry_transport)), ([], []))
        self.assertEqual((len(failed_transport.calls), len(retry_transport.calls)), (2, 3))   # search pages only
        for call in failed_transport.calls + retry_transport.calls:
            self.assertIn("resultsSortBy=-arGemi", call["url"])


class PersistenceTests(DiscoveryIngestTestCase):
    PAGES = [[item(1002, TODAY), item(1001, TODAY), item(1000), item(999)]]

    def test_the_run_its_evidence_and_the_cursor_are_one_transaction(self):
        with patch.object(GemiDiscoveryObservation.objects, "bulk_create", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.ingest(self.PAGES)
        # No run row without its observations, and no frontier without its evidence.
        self.assertEqual((GemiDiscoveryRun.objects.count(), GemiDiscoveryObservation.objects.count()), (0, 0))
        cursor = get_cursor()
        self.assertEqual((cursor.high_water_mark, cursor.last_run_id, cursor.last_success_at), ("1000", None, None))

    def test_a_crash_between_the_company_write_and_the_evidence_heals_on_the_next_run(self):
        with patch.object(discovery_module, "_save_run", side_effect=RuntimeError("worker killed")):
            with self.assertRaises(RuntimeError):
                self.ingest(self.PAGES)
        self.assertEqual((Company.objects.count(), GemiDiscoveryObservation.objects.count()), (4, 0))

        result, _ = self.ingest(self.PAGES)
        # Still above the unmoved frontier: seen again, already local, recorded, never written twice.
        self.assertEqual((result.new_records, result.rediscovered_local_records, result.ingested_records), (2, 2, 0))
        self.assertEqual({number: outcomes()[number] for number in ("1002", "1001")},
                         {"1002": INGEST_ALREADY_LOCAL, "1001": INGEST_ALREADY_LOCAL})
        self.assertEqual((Company.objects.count(), get_cursor().high_water_mark), (4, "1002"))


# --- phase 3: compact observations -----------------------------------------------------------------------

class CompactObservationTests(DiscoveryIngestTestCase):
    PAGES = [[item(1002, TODAY), item(1001, incorporationDate=None), item(1000), item(999)]]

    def run_compact(self, pages=None):
        return self.ingest(pages or self.PAGES, compact_observations=True)[0]

    def test_one_hundred_unchanged_runs_add_no_observation(self):
        first = self.run_compact()
        self.assertEqual((first.observations_stored, first.observations_suppressed), (4, 0))
        second = self.run_compact()          # 1002 is now known: a change, stored once
        settled = GemiDiscoveryObservation.objects.count()
        self.assertEqual((second.observations_stored, settled), (1, 5))

        for _ in range(100):
            result = self.run_compact()
            self.assertEqual((result.status, result.observations_stored, result.observations_suppressed),
                             ("success", 0, 4))
        self.assertEqual(GemiDiscoveryObservation.objects.count(), settled)
        self.assertEqual(GemiDiscoveryRun.objects.count(), 102)        # every run is still on record
        self.assertEqual(result.records_examined, 4)                   # and still counts what it examined

    def test_the_first_sighting_and_the_quarantine_evidence_are_kept(self):
        self.run_compact()
        for _ in range(3):
            self.run_compact()
        rows = list(GemiDiscoveryObservation.objects.order_by("id").values_list(
            "gemi_number", "classification", "ingest_outcome", "company_existed"))
        self.assertIn(("1002", NEW_INCORPORATION, INGEST_CREATED, False), rows)     # the first eligible sighting
        self.assertIn(("1001", INVALID_DATE, INGEST_QUARANTINED_DATE, False), rows)  # the quarantine
        self.assertEqual(GemiDiscoveryObservation.objects.filter(gemi_number="1001").count(), 1)
        first = GemiDiscoveryObservation.objects.filter(gemi_number="1002").order_by("id").first()
        self.assertEqual(first.run_id, GemiDiscoveryRun.objects.order_by("id").first().pk)

    def test_a_classification_or_outcome_change_is_always_stored(self):
        self.run_compact()
        self.run_compact()
        before = GemiDiscoveryObservation.objects.filter(gemi_number="1001").count()
        changed = self.run_compact([[item(1002, TODAY), item(1001, "2022-02-02"), item(1000), item(999)]])
        latest = GemiDiscoveryObservation.objects.filter(gemi_number="1001").order_by("id").last()
        self.assertEqual(GemiDiscoveryObservation.objects.filter(gemi_number="1001").count(), before + 1)
        self.assertEqual((latest.classification, latest.ingest_outcome), (LATE_PUBLICATION, INGEST_CREATED))
        self.assertEqual(changed.observations_stored, 1)

    def test_a_known_record_is_stored_at_most_once_per_local_day(self):
        self.run_compact()
        self.run_compact()
        self.run_compact()
        self.assertEqual(GemiDiscoveryObservation.objects.filter(gemi_number="1000", classification=KNOWN).count(), 1)
        # A new local day: yesterday's evidence no longer suppresses today's first sighting.
        for run in GemiDiscoveryRun.objects.all():
            GemiDiscoveryRun.objects.filter(pk=run.pk).update(started_at=run.started_at - timedelta(days=1))
        result = self.run_compact()
        self.assertEqual((result.observations_stored, result.observations_suppressed), (4, 0))
        self.run_compact()
        self.assertEqual(GemiDiscoveryObservation.objects.filter(gemi_number="1000", classification=KNOWN).count(), 2)

    def test_compaction_never_touches_anomalies_or_the_cursor(self):
        self.run_compact()
        result = self.run_compact([[item(1003, TODAY), item(1004, TODAY), item(1002), item(1001)]])
        self.assertEqual((result.status, [entry["kind"] for entry in result.blocking_anomalies]),
                         ("anomaly", [ORDERING_VIOLATION]))
        self.assertEqual(GemiDiscoveryRun.objects.order_by("id").last().anomalies, result.anomalies)
        self.assertEqual((get_cursor().high_water_mark, get_cursor().status), ("1002", "anomaly"))

    def test_the_default_run_still_stores_every_observation(self):
        self.ingest(self.PAGES)
        self.ingest(self.PAGES)
        self.ingest(self.PAGES)
        self.assertEqual(GemiDiscoveryObservation.objects.count(), 12)


# --- phase 4: the lean cycle -----------------------------------------------------------------------------

class IngestionCycleTestCase(CycleTestCase):
    def run_cycle(self, *records, pages=None, enabled=True, **options):
        client, transport = paged_client(pages or [self.page(*records)],
                                         failure_after=options.pop("failure_after", None))
        with override_settings(GEMI_DISCOVERY_V2_ENABLED=enabled):
            return run_ingestion_cycle(client=client, **options), transport

    def everything(self):
        return tuple(list(model.objects.order_by("pk").values()) for model in (
            Company, CompanyActivity, CompanySignal, CompanySnapshot, Opportunity, GemiDiscoveryRun,
            GemiDiscoveryObservation))


class DormantTests(IngestionCycleTestCase):
    def test_ingest_is_off_by_default_and_the_schedule_entry_is_gated_on_it(self):
        self.assertFalse(settings.GEMI_DISCOVERY_V2_ENABLED)
        entry = {item["func"]: item for item in gemi_apps.SCHEDULES}["gemiapp.tasks.run_gemi_ingestion_cycle_task"]
        self.assertEqual(entry["requires"], "gemiapp.ingestion.discovery.ingest_enabled")
        from django_q.models import Schedule

        self.assertFalse(Schedule.objects.filter(func=entry["func"]).exists())   # no row while the flag is off
        self.assertEqual({item["func"]: item["cron"] for item in gemi_apps.SCHEDULES}[
            "gemiapp.tasks.run_intraday_pipeline_task"], "0 8,11,14,17,20,23 * * *")

    def test_the_cycle_refuses_before_any_lock_request_or_write_while_the_flag_is_off(self):
        before = self.everything()
        for dry_run in (False, True):
            client, transport = paged_client([self.page(item(NEW, self.today))])
            with self.assertRaises(IngestionCycleRefused) as refused:
                run_ingestion_cycle(client=client, dry_run=dry_run)
            self.assertIn("GEMI_DISCOVERY_V2_ENABLED", str(refused.exception))
            self.assertEqual(transport.calls, [])
        self.assertEqual(self.everything(), before)
        self.assertIsNone(caches["shared"].get(LOCK_KEY))

    def test_the_task_and_the_command_refuse_the_same_way(self):
        with patch("gemiapp.ingestion.discovery.get_gemi_client") as get_client:
            self.assertEqual(run_gemi_ingestion_cycle_task()["status"], "refused")
            with self.assertRaises(CommandError):
                call_command("run_gemi_ingestion_cycle", "--dry-run", stdout=StringIO())
        get_client.assert_not_called()
        self.assertEqual(GemiDiscoveryRun.objects.count(), 0)

    @override_settings(GEMI_DISCOVERY_V2_SHADOW=False)
    def test_the_shadow_flag_being_off_still_refuses(self):
        with self.assertRaises(IngestionCycleRefused):
            self.run_cycle(item(NEW, self.today))

    def test_a_live_signal_still_refuses(self):
        from .company_signals import DISCOVERY, LIVE, record_company_signal

        record_company_signal(company=Company.objects.get(gemi_number=str(BELOW)), signal_type="new_company",
                              source_type=DISCOVERY, event_key={"event": "first_observed"}, mode=LIVE,
                              detected_at=timezone.now())
        with self.assertRaises(IngestionCycleRefused) as refused:
            self.run_cycle(item(NEW, self.today))
        self.assertIn("no LIVE signal exists", str(refused.exception))


class EndToEndTests(IngestionCycleTestCase):
    def test_a_page_becomes_companies_shadow_signals_and_shadow_opportunities_without_a_lookup(self):
        report, transport = self.run_cycle(item(LATE, OLD), item(NEW, self.today))

        self.assertEqual((report.status, report.failed_phases), (OK, []))
        self.assertEqual((len(transport.calls), lookups(transport), report.gemi_requests), (1, [], 1))
        new, late = Company.objects.get(gemi_number=str(NEW)), Company.objects.get(gemi_number=str(LATE))
        self.assertEqual((new.incorporation_date, late.incorporation_date), (self.today, date(2025, 11, 2)))
        self.assertEqual(report.discovery.ingested_records, 2)
        self.assertEqual(report.materialisation.signals_created, 2)
        self.assertEqual(set(CompanySignal.objects.values_list("mode", flat=True)), {SHADOW})
        self.assertEqual(len(report.pipeline_runs), 2)                       # the after-commit hook, once each
        self.assertEqual(Opportunity.objects.filter(company=new, radar=self.radar).count(), 1)
        self.assertEqual(set(Opportunity.objects.values_list("latest_signal__mode", flat=True)), {SHADOW})
        summary = report.summary()
        self.assertEqual((summary["companies_created"], summary["signals_created"], summary["gemi_requests"]),
                         (2, 2, 1))
        self.assertIn("zero per-company lookups", "\n".join(report.lines()))

    def test_nothing_reaches_a_customer_an_inbox_or_the_legacy_product(self):
        self.run_cycle(item(NEW, self.today))
        company = Company.objects.get(gemi_number=str(NEW))

        self.assertEqual(mail.outbox, [])
        self.assertEqual((OrganizationNotification.objects.count(), OpportunityTask.objects.count(),
                          DigestDelivery.objects.count()), (0, 0, 0))
        self.assertEqual((RadarMatch.objects.count(), UserCompanyLead.objects.count()), (0, 0))
        self.assertEqual(get_authorized_workspace_opportunities(self.owner, self.org.pk, view="all").rows, ())
        dashboard = get_authorized_workspace_dashboard(self.owner, self.org.pk)
        self.assertEqual((dashboard.active_opportunities, dashboard.unread_notifications), (0, 0))
        with self.assertRaises(OrganizationAccessDenied):
            get_authorized_company_opportunity_page(self.owner, self.org.pk, company.pk)
        self.assertIsNone(Opportunity.objects.get().assigned_to_id)

    def test_a_quarantined_date_creates_no_company_and_no_signal(self):
        future = (self.today + timedelta(days=1)).isoformat()
        report, _ = self.run_cycle(item(LATE, future), item(NEW, incorporationDate="9011-12-09"))
        self.assertFalse(Company.objects.filter(gemi_number__in=[str(NEW), str(LATE)]).exists())
        self.assertEqual((report.discovery.quarantined_date_records, CompanySignal.objects.count()), (2, 0))
        self.assertEqual(report.materialisation.unmaterialised_no_company, 2)
        self.assertFalse(Company.objects.filter(incorporation_date=self.today)
                         .exclude(gemi_number__in=[str(FRONTIER), str(BELOW)]).exists())

    def test_running_the_cycle_again_changes_nothing(self):
        self.run_cycle(item(NEW, self.today))
        world = tuple(list(model.objects.order_by("pk").values()) for model in (
            Company, CompanyActivity, CompanySignal, CompanySnapshot, Opportunity))
        again, _ = self.run_cycle(item(NEW, self.today))
        self.assertEqual(tuple(list(model.objects.order_by("pk").values()) for model in (
            Company, CompanyActivity, CompanySignal, CompanySnapshot, Opportunity)), world)
        self.assertEqual((again.status, again.pipeline_runs, again.discovery.ingested_records), (OK, [], 0))

    def test_a_dry_run_requests_the_page_and_writes_nothing(self):
        before = self.everything()
        report, transport = self.run_cycle(item(NEW, self.today), dry_run=True)
        self.assertEqual((len(transport.calls), report.discovery.would_ingest_records), (1, 1))
        self.assertEqual(self.everything(), before)
        self.assertIn("[dry-run] nothing was written", "\n".join(report.lines()))

    def test_the_command_prints_the_report(self):
        client, _ = paged_client([self.page(item(NEW, self.today))])
        out = StringIO()
        with ENABLED, patch("gemiapp.ingestion.discovery.get_gemi_client", return_value=client):
            call_command("run_gemi_ingestion_cycle", stdout=out)
        self.assertIn("GEMI INGESTION CYCLE", out.getvalue())
        self.assertTrue(Company.objects.filter(gemi_number=str(NEW)).exists())


class ScopedMaterialisationTests(IngestionCycleTestCase):
    def test_only_this_runs_numbers_and_the_bounded_catch_up_are_materialised(self):
        # Old evidence with a stored company and no signal, outside the catch-up window: not this cycle's work.
        old = known_company(118717100000, self.today)
        observation(discovery_run(timezone.now() - timedelta(days=3)), old.gemi_number)
        # The same situation one hour ago: a cycle that died before materialising. The catch-up repairs it.
        recent = known_company(118717100500, self.today)
        observation(discovery_run(timezone.now() - timedelta(hours=1)), recent.gemi_number)

        with patch.object(ingestion_cycle, "materialize_new_company_signals",
                          wraps=ingestion_cycle.materialize_new_company_signals) as spy:
            report, _ = self.run_cycle(item(NEW, self.today))

        self.assertEqual(spy.call_args.kwargs["gemi_numbers"], sorted([str(NEW), recent.gemi_number]))
        self.assertEqual((report.discovered_numbers, report.catch_up_numbers), (1, 1))
        self.assertEqual(set(CompanySignal.objects.values_list("company__gemi_number", flat=True)),
                         {str(NEW), recent.gemi_number})
        self.assertFalse(CompanySignal.objects.filter(company=old).exists())

    def test_a_cycle_with_nothing_new_materialises_nothing(self):
        with patch.object(ingestion_cycle, "materialize_new_company_signals") as spy:
            report, _ = self.run_cycle()
        spy.assert_not_called()
        self.assertEqual((report.status, report.materialisation), (OK, None))

    def test_a_cycle_that_died_before_materialising_is_repaired_by_the_next_one(self):
        with patch.object(ingestion_cycle, "materialize_new_company_signals", side_effect=RuntimeError("killed")):
            crashed, _ = self.run_cycle(item(NEW, self.today))
        self.assertEqual((crashed.status, [name for name, _ in crashed.failed_phases]),
                         (FAILED_CYCLE, [MATERIALISATION_PHASE]))
        # Evidence and frontier were committed; the company exists; there is no signal yet.
        self.assertEqual((get_cursor().high_water_mark, CompanySignal.objects.count()), (str(NEW), 0))
        self.assertIsNone(caches["shared"].get(LOCK_KEY))            # the lock did not outlive the failure

        repaired, _ = self.run_cycle(item(NEW, self.today))          # NEW is now below the frontier: "known"
        self.assertEqual((repaired.discovered_numbers, repaired.catch_up_numbers), (0, 1))
        self.assertEqual(CompanySignal.objects.filter(company__gemi_number=str(NEW), mode=SHADOW).count(), 1)
        self.assertEqual(Opportunity.objects.count(), 1)


class CycleWriteFailureTests(IngestionCycleTestCase):
    def test_the_cycle_fails_keeps_the_frontier_materialises_what_was_written_and_heals_next_time(self):
        real = company_writer.create_company_from_search_item

        def fail_late(number, record, **kwargs):
            if number == str(LATE):
                raise RuntimeError("database hiccup")
            return real(number, record, **kwargs)

        with patch.object(company_writer, "create_company_from_search_item", side_effect=fail_late):
            report, transport = self.run_cycle(item(LATE, OLD), item(NEW, self.today))

        self.assertEqual((report.status, report.discovery.status, report.discovery.stop_reason),
                         (FAILED_CYCLE, "failed", STOP_INGEST_WRITE_FAILED))
        self.assertEqual(get_cursor().high_water_mark, str(FRONTIER))                 # not advanced
        # The company that was written is kept and its SHADOW signal is still materialised.
        self.assertEqual(set(CompanySignal.objects.values_list("company__gemi_number", "mode")), {(str(NEW), SHADOW)})
        self.assertFalse(Company.objects.filter(gemi_number=str(LATE)).exists())
        self.assertEqual(lookups(transport), [])
        self.assertIsNone(caches["shared"].get(LOCK_KEY))

        healed, transport = self.run_cycle(item(LATE, OLD), item(NEW, self.today))    # nothing but the next run
        self.assertEqual((healed.status, healed.discovery.ingested_records), (OK, 1))
        self.assertEqual(get_cursor().high_water_mark, str(LATE))
        self.assertEqual(CompanySignal.objects.filter(mode=SHADOW).count(), 2)
        self.assertEqual(Company.objects.filter(gemi_number=str(NEW)).count(), 1)
        self.assertEqual(lookups(transport), [])


class FailureTests(IngestionCycleTestCase):
    def test_a_failed_page_is_a_failed_cycle_and_the_lock_is_released(self):
        report, _ = self.run_cycle(item(NEW, self.today), failure_after=0)
        self.assertEqual((report.status, report.discovery.status), (FAILED_CYCLE, "failed"))
        self.assertEqual(get_cursor().high_water_mark, str(FRONTIER))
        self.assertIsNone(caches["shared"].get(LOCK_KEY))
        client, _ = paged_client([self.page(item(NEW, self.today))], failure_after=0)
        with ENABLED, patch("gemiapp.ingestion.discovery.get_gemi_client", return_value=client):
            with self.assertRaises(CommandError):
                call_command("run_gemi_ingestion_cycle", stdout=StringIO())


class MutexTests(IngestionCycleTestCase):
    def test_a_second_cycle_does_nothing_while_one_is_running(self):
        release = claim_cycle_lock()
        self.assertIsNotNone(release)
        before = self.everything()
        report, transport = self.run_cycle(item(NEW, self.today))
        self.assertEqual((report.status, transport.calls, report.discovery), (SKIPPED_LOCKED, [], None))
        self.assertEqual(self.everything(), before)
        self.assertIn("another cycle holds the lock", "\n".join(report.lines()))
        self.assertIsNone(claim_cycle_lock())                        # still held by the first cycle
        release()
        report, _ = self.run_cycle(item(NEW, self.today))
        self.assertEqual(report.status, OK)

    def test_a_cycle_running_inside_another_is_shut_out(self):
        inner = {}

        def nested(*args, **kwargs):
            inner["report"] = run_ingestion_cycle(client=paged_client([self.page()])[0])
            raise RuntimeError("stop here")

        with patch.object(ingestion_cycle, "run_discovery", side_effect=nested):
            outer, _ = self.run_cycle(item(NEW, self.today))
        self.assertEqual(inner["report"].status, SKIPPED_LOCKED)
        self.assertEqual(outer.status, FAILED_CYCLE)
        self.assertIsNone(caches["shared"].get(LOCK_KEY))

    def test_the_lock_is_not_bucketed_by_the_hour_and_only_its_holder_releases_it(self):
        self.assertNotIn(timezone.now().strftime("%Y%m%d%H"), LOCK_KEY)
        first = claim_cycle_lock()
        caches["shared"].delete(LOCK_KEY)                            # the first holder's TTL ran out
        second = claim_cycle_lock()
        first()                                                      # a late release must not free the successor
        self.assertIsNotNone(caches["shared"].get(LOCK_KEY))
        second()
        self.assertIsNone(caches["shared"].get(LOCK_KEY))


class RateBudgetTests(IngestionCycleTestCase):
    def test_the_shared_limiter_is_unchanged(self):
        self.assertEqual((MAX_REQUESTS_PER_MINUTE, BudgetConfig().capacity, settings.GEMI_RATE_LIMIT_PER_MINUTE),
                         (7, 7, 7))
        self.assertAlmostEqual(BudgetConfig().slot_seconds, 62 / 6)
        self.assertEqual([lane.name for lane in sorted(GemiLane)],
                         ["DISCOVERY", "DIGEST_IMPORT", "MONITORED_REFRESH", "DOCUMENTS"])

    def test_every_page_takes_one_slot_of_the_shared_budget_in_the_discovery_lane_with_a_short_wait(self):
        records = [item(number, "2026-09-01") for number in range(FRONTIER + 450_000, FRONTIER, -1000)]
        records += [item(FRONTIER, self.today), item(BELOW, self.today)]
        pages = [records[start:start + 200] for start in range(0, len(records), 200)]
        waits = []

        def route(url):
            import urllib.parse

            from .test_gemi_client import response
            from .test_gemi_validation import page

            offset = int(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get("resultsOffset", 0))
            position = 0
            for chunk in pages:
                if position == offset:
                    return response(200, page(*chunk, total=len(records)))
                position += len(chunk)
            return response(200, page(total=len(records)))

        client, transport, clock, budget = make_client(route, max_attempts=1)
        acquire = budget.acquire
        budget.acquire = lambda lane, *, max_wait: (waits.append(max_wait), acquire(lane, max_wait=max_wait))[1]
        with ENABLED:
            report = run_ingestion_cycle(client=client)

        self.assertEqual((report.status, report.discovery.ingested_records), (OK, 450))
        self.assertEqual((len(transport.calls), lookups(transport)), (3, []))
        self.assertEqual(budget.lanes, [GemiLane.DISCOVERY] * 3)     # one slot per page, nothing else
        self.assertEqual(waits, [120.0] * 3)                         # never the lane's 900 s
        # Each request owns its own budget slot: the guarantee behind "never more than 7 in a rolling minute".
        slots = {math.floor(call["at"] / BudgetConfig().slot_seconds) for call in transport.calls}
        self.assertEqual(len(slots), 3)

    @override_settings(GEMI_INGESTION_MAX_WAIT_SECONDS=45)
    def test_the_wait_is_configurable(self):
        seen = []

        def capture(**kwargs):
            seen.append(kwargs)
            raise RuntimeError("captured")

        with patch.object(ingestion_cycle, "run_discovery", side_effect=capture):
            self.run_cycle()
        self.assertEqual((seen[0]["max_wait"], seen[0]["mode"], seen[0]["compact_observations"]), (45.0, INGEST, True))


class LegacyImporterUnchangedTests(IngestionCycleTestCase):
    def test_the_legacy_import_still_upserts_and_matches_over_a_company_the_cycle_created(self):
        self.run_cycle(item(NEW, self.today))
        created = Company.objects.get(gemi_number=str(NEW))
        refreshed = item(NEW, self.today, coNameEl="ΝΕΑ ΕΠΩΝΥΜΙΑ ΙΚΕ")

        with patch("gemiapp.services.fetch_companies", return_value=[refreshed]):
            run = import_for_date(self.today)

        # Same-day refresh is still the legacy importer's: it updates the row in place.
        self.assertEqual((run.status, run.created_count, run.updated_count), ("success", 0, 1))
        created.refresh_from_db()
        self.assertEqual(created.name, "ΝΕΑ ΕΠΩΝΥΜΙΑ ΙΚΕ")
        self.assertEqual(Company.objects.filter(gemi_number=str(NEW)).count(), 1)

    def test_company_defaults_still_clamps_for_the_legacy_importer(self):
        self.assertEqual(company_defaults(item(NEW, incorporationDate="9011-12-09"))["incorporation_date"], TODAY)
        self.assertEqual(company_defaults(item(NEW, incorporationDate=None))["incorporation_date"], TODAY)
        self.assertEqual(company_defaults(item(NEW, AS_OF))["incorporation_date"], AS_OF)

    def test_the_cycle_never_updates_a_company_the_legacy_importer_stored(self):
        stored = self.new_company()
        Company.objects.filter(pk=stored.pk).update(name="ΟΠΩΣ ΤΗΝ ΕΓΡΑΨΕ Ο IMPORTER")
        before = list(Company.objects.filter(pk=stored.pk).values())
        report, _ = self.run_cycle(item(NEW, self.today, coNameEl="ΑΛΛΗ ΕΠΩΝΥΜΙΑ"))
        self.assertEqual(list(Company.objects.filter(pk=stored.pk).values()), before)
        self.assertEqual((report.discovery.ingested_records, outcomes()[str(NEW)]), (0, INGEST_ALREADY_LOCAL))
        self.assertEqual(CompanySignal.objects.filter(company=stored, mode=SHADOW).count(), 1)
