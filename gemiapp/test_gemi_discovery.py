"""Tests for Discovery v2 (A10, gemiapp.ingestion.discovery).

Fixture GEMI pages only -- no live call (NoNetworkMixin guards urlopen).
"""

import copy
import urllib.parse
from datetime import date, datetime, time, timedelta
from io import StringIO
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import AddConstraint, AddIndex, CreateModel
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import apps as gemi_apps
from .ingestion.discovery import (
    BOOTSTRAP,
    DUPLICATE_RECORD,
    INGEST,
    INVALID_DATE,
    INVALID_IDENTIFIER,
    KNOWN,
    LATE_PUBLICATION,
    NEW_INCORPORATION,
    ORDERING_VIOLATION,
    PAGE_BOUNDARY_VIOLATION,
    SHADOW,
    STOP_END_OF_RESULTS,
    STOP_NO_CURSOR,
    STOP_OVERLAP_SATISFIED,
    STOP_PAGE_LIMIT,
    DiscoveryPolicy,
    bootstrap_discovery_cursor,
    compare_with_legacy,
    get_cursor,
    normalize_gemi_number,
    run_discovery,
)
from .ingestion.rate_budget import GemiLane
from .models import (
    Company, CompanyActivity, CompanyMonitoring, CustomerRadar, DigestDelivery, GemiDiscoveryCursor,
    GemiDiscoveryObservation, GemiDiscoveryRun, ImportRun, RadarMatch, UserCompanyLead, UserSubscription,
)
from .services import company_matches_radar, import_for_date, match_imported_companies, send_digests
from .tasks import run_gemi_discovery_v2_shadow_task
from .test_gemi_client import NoNetworkMixin, make_client, response
from .test_gemi_company_activities import TARGET, entitled_user, radar_for
from .test_gemi_validation import CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL, full_item, page

AS_OF = date(2026, 9, 16)
POLICY = DiscoveryPolicy(page_size=3, max_pages=3, overlap_known_records=2, min_overlap_pages=1,
                         bootstrap_known_confirmations=2, bootstrap_max_pages=3)


def item(number, day=None, **overrides):
    return full_item(str(number), (day or AS_OF).isoformat() if not isinstance(day, str) else day, **overrides)


def known_company(number, day=None):
    return Company.objects.create(
        gemi_number=str(number), name=f"ΕΤΑΙΡΕΙΑ {number}", incorporation_date=day or AS_OF, prefecture="ΑΤΤΙΚΗΣ",
        legal_type="ΙΚΕ",
    )


def paged_client(pages, *, total=None, failure_after=None):
    """A client whose /companies search returns ``pages`` (lists of records) by resultsOffset."""
    total_count = total if total is not None else sum(len(records) for records in pages)
    calls = {"count": 0}

    def route(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        offset = int(query.get("resultsOffset", 0))
        calls["count"] += 1
        if failure_after is not None and calls["count"] > failure_after:
            return response(503, body=b"")
        position = 0
        for records in pages:
            if position == offset:
                return response(200, page(*records, total=total_count))
            position += len(records)
        return response(200, page(total=total_count))

    client, transport, _, _ = make_client(route, max_attempts=1)
    return client, transport


def ready_cursor(high_water_mark):
    cursor = get_cursor()
    cursor.status = "ready"
    cursor.high_water_mark = str(high_water_mark)
    cursor.high_water_mark_value = int(high_water_mark)
    cursor.bootstrap_method = "verified_scan"
    cursor.bootstrapped_at = timezone.now()
    cursor.save()
    return cursor


def observations(run_id=None):
    rows = GemiDiscoveryObservation.objects.all()
    if run_id is not None:
        rows = rows.filter(run_id=run_id)
    return {row.gemi_number: row.classification for row in rows}


def discovered(run_id=None):
    """Only the newly discovered records; the scan also records the known ones it paged through."""
    rows = GemiDiscoveryObservation.objects.exclude(classification=KNOWN)
    if run_id is not None:
        rows = rows.filter(run_id=run_id)
    return {row.gemi_number: row.classification for row in rows}


def discovered_rows():
    """Every discovery row across runs: in shadow mode the same company is rediscovered each run."""
    return GemiDiscoveryObservation.objects.exclude(classification=KNOWN).count()


class SchemaTests(TestCase):
    def test_discovery_tables_hold_identifiers_counts_and_dates_only(self):
        self.assertEqual(
            {field.name for field in GemiDiscoveryObservation._meta.concrete_fields},
            {"id", "run", "gemi_number", "classification", "incorporation_date", "incorporation_date_quality",
             "company_existed", "page_index", "created_at"},
        )
        forbidden = ("name", "email", "phone", "afm", "vat", "person", "address", "payload", "raw")
        for model in (GemiDiscoveryCursor, GemiDiscoveryRun, GemiDiscoveryObservation):
            for field in model._meta.concrete_fields:
                self.assertFalse(
                    any(word in field.name for word in forbidden), f"{model.__name__}.{field.name}",
                )

    def test_the_cursor_is_pipeline_state_not_customer_state(self):
        self.assertEqual(GemiDiscoveryCursor._meta.get_field("stream").unique, True)
        related = {field.related_model for field in GemiDiscoveryCursor._meta.get_fields() if field.is_relation}
        self.assertEqual(related, {GemiDiscoveryRun})
        self.assertFalse(any(field.name.startswith("discovery") for field in UserSubscription._meta.concrete_fields))
        self.assertEqual(  # ImportRun is untouched: it still means one legacy import of one day
            [field.name for field in ImportRun._meta.concrete_fields],
            ["id", "target_date", "status", "fetched_count", "created_count", "updated_count", "error_message",
             "started_at", "finished_at"],
        )

    def test_the_migration_only_creates_the_discovery_tables(self):
        migration = MigrationLoader(None, ignore_no_migrations=True).get_migration("gemiapp", "0038_gemi_discovery")
        self.assertEqual(migration.dependencies, [("gemiapp", "0037_company_monitoring")])
        self.assertTrue(all(isinstance(op, (CreateModel, AddIndex, AddConstraint)) for op in migration.operations))
        self.assertEqual(
            {op.name for op in migration.operations if isinstance(op, CreateModel)},
            {"GemiDiscoveryRun", "GemiDiscoveryCursor", "GemiDiscoveryObservation"},
        )


class IdentifierTests(TestCase):
    def test_only_positive_integer_identifiers_are_accepted(self):
        self.assertEqual(normalize_gemi_number("118717203000"), ("118717203000", 118717203000))
        self.assertEqual(normalize_gemi_number(" 1001 "), ("1001", 1001))
        for value in (None, "", "abc", "12A4", "-5", "0", 12.5, True):
            self.assertIsNone(normalize_gemi_number(value), value)


class BootstrapTests(NoNetworkMixin, TestCase):
    def test_a_verified_scan_sets_the_frontier_to_the_highest_known_identifier(self):
        for number in (1000, 999):
            known_company(number)
        client, _ = paged_client([[item(1002), item(1001), item(1000)], [item(999)]])

        result = bootstrap_discovery_cursor(client=client, policy=POLICY, as_of=AS_OF)

        cursor = get_cursor()
        self.assertEqual((result.status, result.resulting_high_water_mark), ("success", "1000"))
        self.assertEqual((cursor.status, cursor.high_water_mark, cursor.high_water_mark_value), ("ready", "1000", 1000))
        self.assertEqual((cursor.bootstrap_method, result.new_records), ("verified_scan", 2))  # 1002, 1001 backlog
        self.assertEqual(Company.objects.count(), 2)  # bootstrap never creates companies
        self.assertEqual(GemiDiscoveryRun.objects.get().mode, BOOTSTRAP)

    def test_too_few_confirmations_refuses_to_establish_a_frontier(self):
        known_company(1000)
        client, _ = paged_client([[item(1002), item(1001), item(1000)]])
        result = bootstrap_discovery_cursor(client=client, policy=POLICY, as_of=AS_OF)
        cursor = get_cursor()
        self.assertEqual(result.status, "incomplete")
        self.assertEqual((cursor.status, cursor.high_water_mark_value), ("uninitialised", None))

    def test_no_known_record_at_all_refuses_to_establish_a_frontier(self):
        client, _ = paged_client([[item(1002), item(1001)]])
        result = bootstrap_discovery_cursor(client=client, policy=POLICY, as_of=AS_OF)
        self.assertEqual(result.status, "incomplete")
        self.assertIsNone(get_cursor().high_water_mark_value)

    def test_re_bootstrapping_needs_an_explicit_force_and_a_dry_run_writes_nothing(self):
        for number in (1000, 999):
            known_company(number)
        pages = [[item(1002), item(1001), item(1000)], [item(999)]]
        client, _ = paged_client(copy.deepcopy(pages))
        preview = bootstrap_discovery_cursor(client=client, policy=POLICY, as_of=AS_OF, dry_run=True)
        self.assertEqual(preview.status, "success")
        self.assertEqual((GemiDiscoveryRun.objects.count(), get_cursor().high_water_mark), (0, ""))

        client, _ = paged_client(copy.deepcopy(pages))
        bootstrap_discovery_cursor(client=client, policy=POLICY, as_of=AS_OF)
        client, _ = paged_client(copy.deepcopy(pages))
        with self.assertRaises(ValueError):
            bootstrap_discovery_cursor(client=client, policy=POLICY, as_of=AS_OF)
        client, _ = paged_client(copy.deepcopy(pages))
        forced = bootstrap_discovery_cursor(client=client, policy=POLICY, as_of=AS_OF, force=True)
        self.assertEqual(forced.status, "success")


class DiscoveryRunTests(NoNetworkMixin, TestCase):
    def setUp(self):
        for number in (1000, 999, 998):
            known_company(number)

    def test_without_a_cursor_the_run_refuses_to_guess(self):
        client, transport = paged_client([[item(1002)]])
        result = run_discovery(client=client, policy=POLICY, as_of=AS_OF)
        self.assertEqual((result.status, result.stop_reason), ("failed", STOP_NO_CURSOR))
        self.assertEqual(transport.calls, [])
        self.assertEqual(GemiDiscoveryObservation.objects.count(), 0)

    def test_new_records_beyond_the_frontier_are_discovered_and_the_cursor_advances(self):
        ready_cursor(1000)
        client, transport = paged_client([[item(1003), item(1002), item(1001)], [item(1000), item(999), item(998)]])

        result = run_discovery(client=client, policy=POLICY, as_of=AS_OF)

        self.assertEqual((result.status, result.stop_reason, result.pages_fetched), ("success", STOP_OVERLAP_SATISFIED, 2))
        self.assertEqual((result.new_records, result.known_records), (3, 3))
        self.assertEqual(set(discovered()), {"1001", "1002", "1003"})
        self.assertEqual(set(observations()) - set(discovered()), {"1000", "999", "998"})  # seen and already known
        cursor = get_cursor()
        self.assertEqual((cursor.high_water_mark, result.cursor_advanced), ("1003", True))
        self.assertEqual((Company.objects.count(), CompanyActivity.objects.count()), (3, 0))  # shadow writes no company
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(transport.calls[0]["url"]).query))
        self.assertEqual(query["resultsSortBy"], "-arGemi")

    def test_the_overlap_policy_decides_when_it_is_safe_to_stop(self):
        ready_cursor(1000)
        pages = [[item(1003), item(1002), item(1001)], [item(1000), item(999), item(998)], [item(997)]]
        client, _ = paged_client(copy.deepcopy(pages))
        self.assertEqual(run_discovery(client=client, policy=POLICY, as_of=AS_OF).pages_fetched, 2)

        GemiDiscoveryRun.objects.all().delete()
        ready_cursor(1000)
        known_company(997)
        strict = DiscoveryPolicy(page_size=3, max_pages=3, overlap_known_records=4, min_overlap_pages=1)
        client, _ = paged_client(copy.deepcopy(pages))
        result = run_discovery(client=client, policy=strict, as_of=AS_OF)
        # Page 2 already held three known records; the stricter overlap kept it going to page 3.
        self.assertEqual((result.pages_fetched, result.stop_reason), (3, STOP_OVERLAP_SATISFIED))

    def test_a_repeated_run_discovers_nothing_new(self):
        ready_cursor(1000)
        pages = [[item(1003), item(1002), item(1001)], [item(1000), item(999), item(998)]]
        client, _ = paged_client(copy.deepcopy(pages))
        run_discovery(client=client, policy=POLICY, as_of=AS_OF)
        for number in ("1001", "1002", "1003"):  # the legacy importer stored them meanwhile
            known_company(number)

        client, _ = paged_client(copy.deepcopy(pages))
        second = run_discovery(client=client, policy=POLICY, as_of=AS_OF)

        self.assertEqual((second.new_records, second.status), (0, "success"))
        self.assertEqual(discovered(second.run_id), {})  # nothing new the second time
        self.assertEqual(len(discovered()), 3)  # only the first run's discoveries
        self.assertEqual(get_cursor().high_water_mark, "1003")


class LatePublicationTests(NoNetworkMixin, TestCase):
    def setUp(self):
        for number in (1000, 999, 998):
            known_company(number)
        ready_cursor(1000)

    def run_with(self, *records):
        client, _ = paged_client([list(records), [item(1000), item(999), item(998)]])
        return run_discovery(client=client, policy=POLICY, as_of=AS_OF)

    def test_an_old_incorporation_date_is_discovered_and_classified(self):
        result = self.run_with(item(1003, "2025-12-15"), item(1002, AS_OF.isoformat()), item(1001, (AS_OF - timedelta(days=1)).isoformat()))
        self.assertEqual(discovered(), {"1003": LATE_PUBLICATION, "1002": NEW_INCORPORATION, "1001": LATE_PUBLICATION})
        self.assertEqual((result.late_publication_records, result.new_records), (2, 3))
        row = GemiDiscoveryObservation.objects.get(gemi_number="1003")
        self.assertEqual((row.incorporation_date, row.incorporation_date_quality), (date(2025, 12, 15), "valid"))

    def test_an_unusable_date_is_still_discovered_and_never_clamped(self):
        # Values the A2 contract accepts but A3 refuses to turn into a date.
        result = self.run_with(item(1003, "9011-12-09"), item(1002, ""), item(1001, incorporationDate=None))
        self.assertEqual(set(discovered().values()), {INVALID_DATE})
        self.assertEqual(result.invalid_date_records, 3)
        new_rows = GemiDiscoveryObservation.objects.exclude(classification=KNOWN)
        self.assertEqual(
            dict(new_rows.values_list("gemi_number", "incorporation_date_quality")),
            {"1003": "out_of_range", "1002": "missing", "1001": "missing"},
        )
        self.assertFalse(new_rows.exclude(incorporation_date__isnull=True).exists())
        self.assertEqual(get_cursor().high_water_mark, "1003")  # discovered, not skipped


class OrderingGuardrailTests(NoNetworkMixin, TestCase):
    def setUp(self):
        for number in (1000, 999, 998):
            known_company(number)
        self.cursor = ready_cursor(1000)

    def assertCursorFrozen(self, result, kind):
        cursor = get_cursor()
        self.assertEqual(result.status, "anomaly")
        self.assertEqual([entry["kind"] for entry in result.blocking_anomalies], [kind])
        self.assertEqual((cursor.high_water_mark, cursor.status, result.cursor_advanced), ("1000", "anomaly", False))
        self.assertEqual(cursor.anomaly_reason, kind)

    def test_an_out_of_order_record_inside_a_page_blocks_the_cursor(self):
        client, _ = paged_client([[item(1003), item(1001), item(1002)], [item(1000), item(999), item(998)]])
        self.assertCursorFrozen(run_discovery(client=client, policy=POLICY, as_of=AS_OF), ORDERING_VIOLATION)

    def test_an_out_of_order_page_boundary_blocks_the_cursor(self):
        client, _ = paged_client([[item(1003), item(1002), item(1001)], [item(1004), item(1000), item(999)]])
        self.assertCursorFrozen(run_discovery(client=client, policy=POLICY, as_of=AS_OF), PAGE_BOUNDARY_VIOLATION)

    def test_a_duplicated_page_is_recorded_but_does_not_block(self):
        first = [item(1003), item(1002), item(1001)]
        client, _ = paged_client([first, copy.deepcopy(first), [item(1000), item(999), item(998)]])
        result = run_discovery(client=client, policy=POLICY, as_of=AS_OF)
        self.assertEqual((result.status, result.duplicate_records, result.new_records), ("success", 3, 3))
        self.assertEqual([entry["kind"] for entry in result.anomalies], [DUPLICATE_RECORD] * 3)
        self.assertEqual(get_cursor().high_water_mark, "1003")

    def test_a_page_the_response_contract_rejects_fails_the_run_and_keeps_the_frontier(self):
        broken = item(1002)
        broken["arGemi"] = "abc"  # A2 refuses the page: an unusable identifier never reaches discovery
        client, _ = paged_client([[item(1003), broken, item(1001)], [item(1000), item(999), item(998)]])

        result = run_discovery(client=client, policy=POLICY, as_of=AS_OF)

        self.assertEqual((result.status, result.records_examined, result.cursor_advanced), ("failed", 0, False))
        self.assertIn("GemiResponseValidationError", result.error_message)
        self.assertEqual((get_cursor().high_water_mark, get_cursor().consecutive_failures), ("1000", 1))
        self.assertEqual(GemiDiscoveryObservation.objects.count(), 0)


class CursorSafetyTests(NoNetworkMixin, TestCase):
    def setUp(self):
        for number in (1000, 999, 998):
            known_company(number)
        ready_cursor(1000)

    def test_a_failed_run_keeps_the_previous_frontier(self):
        client, _ = paged_client([[item(1003), item(1002), item(1001)], [item(1000)]], failure_after=1)
        result = run_discovery(client=client, policy=POLICY, as_of=AS_OF)
        cursor = get_cursor()
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.error_message)
        self.assertEqual((cursor.high_water_mark, cursor.consecutive_failures, result.cursor_advanced), ("1000", 1, False))
        self.assertEqual(GemiDiscoveryRun.objects.get().status, "failed")

    def test_a_page_limited_run_is_incomplete_and_does_not_advance(self):
        pages = [[item(1000 + index * 3 + 3), item(1000 + index * 3 + 2), item(1000 + index * 3 + 1)] for index in reversed(range(4))]
        client, _ = paged_client(pages)
        result = run_discovery(client=client, policy=POLICY, as_of=AS_OF)
        self.assertEqual((result.status, result.stop_reason, result.pages_fetched), ("incomplete", STOP_PAGE_LIMIT, 3))
        self.assertEqual((get_cursor().high_water_mark, result.cursor_advanced), ("1000", False))

    def test_a_dry_run_writes_nothing(self):
        client, _ = paged_client([[item(1003), item(1002), item(1001)], [item(1000), item(999), item(998)]])
        result = run_discovery(client=client, policy=POLICY, as_of=AS_OF, dry_run=True)
        self.assertEqual((result.status, result.new_records), ("success", 3))
        self.assertEqual((GemiDiscoveryRun.objects.count(), GemiDiscoveryObservation.objects.count()), (0, 0))
        self.assertEqual((get_cursor().high_water_mark, result.cursor_advanced), ("1000", False))


class IngestModeTests(NoNetworkMixin, TestCase):
    def setUp(self):
        for number in (1000, 999, 998):
            known_company(number)
        ready_cursor(1000)
        self.pages = [[item(1003), item(1002), item(1001)], [item(1000), item(999), item(998)]]

    def test_ingest_is_refused_while_the_flag_is_off(self):
        client, _ = paged_client(copy.deepcopy(self.pages))
        with self.assertRaises(ValueError):
            run_discovery(mode=INGEST, client=client, policy=POLICY, as_of=AS_OF)
        self.assertEqual(Company.objects.count(), 3)

    @override_settings(GEMI_DISCOVERY_V2_ENABLED=True)
    def test_ingest_persists_through_the_existing_importer_path_without_duplicates(self):
        client, _ = paged_client(copy.deepcopy(self.pages))
        result = run_discovery(mode=INGEST, client=client, policy=POLICY, as_of=AS_OF)
        self.assertEqual((result.ingested_records, Company.objects.count()), (3, 6))
        self.assertTrue(CompanyActivity.objects.filter(company__gemi_number="1003").exists())
        self.assertEqual(CompanyMonitoring.objects.count(), 0)  # monitoring is a separate, explicit recompute

        client, _ = paged_client(copy.deepcopy(self.pages))
        again = run_discovery(mode=INGEST, client=client, policy=POLICY, as_of=AS_OF)
        self.assertEqual((again.ingested_records, Company.objects.count()), (0, 6))


class ComparisonTests(NoNetworkMixin, TestCase):
    def test_legacy_and_v2_sets_are_classified(self):
        known_company(1000, AS_OF)
        legacy_only = known_company(1004, AS_OF)  # imported by the legacy pipeline, below the frontier
        older = AS_OF - timedelta(days=5)
        ready_cursor(1000)
        client, _ = paged_client([
            [item(1003, "2025-12-15"), item(1002, AS_OF.isoformat()), item(1001, "9011-12-09")],
            [item(1000), item(999), item(998)],
        ])
        known_company(999, older)
        known_company(998, older)
        run_discovery(client=client, policy=POLICY, as_of=AS_OF)
        # The comparison selects runs by their start date; pin it to AS_OF instead of the wall clock.
        GemiDiscoveryRun.objects.update(started_at=timezone.make_aware(datetime.combine(AS_OF, time(12, 0))))
        Company.objects.filter(gemi_number="1002").delete()  # v2 saw it; legacy has not imported it

        report = compare_with_legacy(AS_OF)

        self.assertEqual((report.legacy, report.v2, report.runs), (2, 3, 1))
        # 1000 was seen during the overlap, so it is BOTH; 1004 was never reached, which is the miss signal.
        self.assertEqual((report.both, report.legacy_only), (1, [legacy_only.gemi_number]))
        self.assertEqual(report.v2_only, ["1001", "1002", "1003"])
        self.assertEqual(
            report.v2_only_reasons,
            {"late_publication": 1, "invalid_incorporation_date": 1, "legacy_filter_miss": 1},
        )
        self.assertIn("legacy=2 v2_discovered=3 v2_seen=", report.lines()[0])


class RateBudgetAndPrivacyTests(NoNetworkMixin, TestCase):
    def setUp(self):
        for number in (1000, 999, 998):
            known_company(number)
        ready_cursor(1000)

    def test_every_request_goes_through_the_shared_client_in_the_discovery_lane(self):
        client, transport = paged_client([[item(1003), item(1002), item(1001)], [item(1000), item(999), item(998)]])
        lanes = []
        original = client.search_companies
        client.search_companies = lambda params, lane: (lanes.append(lane), original(params, lane=lane))[1]

        with patch("gemiapp.ingestion.discovery.get_gemi_client", side_effect=AssertionError("must use the injected client")):
            run_discovery(client=client, policy=POLICY, as_of=AS_OF)

        self.assertEqual(set(lanes), {GemiLane.DISCOVERY})
        self.assertEqual(len(transport.calls), 2)
        for call in transport.calls:
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(call["url"]).query))
            self.assertEqual(query["resultsSize"], str(POLICY.page_size))

    def test_no_person_or_contact_data_reaches_the_discovery_tables_logs_or_output(self):
        client, _ = paged_client([[item(1003), item(1002), item(1001)], [item(1000), item(999), item(998)]])
        out = StringIO()
        with self.assertLogs("gemiapp", level="INFO") as logs:
            result = run_discovery(client=client, policy=POLICY, as_of=AS_OF)
            call_command("run_gemi_discovery_v2", "--compare", AS_OF.isoformat(), stdout=out)
        rendered = "\n".join([
            str(list(GemiDiscoveryObservation.objects.values())), str(list(GemiDiscoveryRun.objects.values())),
            str(list(GemiDiscoveryCursor.objects.values())), out.getvalue(), "\n".join(logs.output), str(result.summary()),
        ])
        for sentinel in (PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, "ΔΟΚΙΜΑΣΤΙΚΗ", "ΕΤΑΙΡΕΙΑ 1000"):
            self.assertNotIn(sentinel, rendered)


class CommandAndTaskTests(NoNetworkMixin, TestCase):
    def setUp(self):
        for number in (1000, 999, 998):
            known_company(number)
        self.pages = [[item(1003), item(1002), item(1001)], [item(1000), item(999), item(998)]]

    def fake_client(self):
        client, _ = paged_client(copy.deepcopy(self.pages))
        return patch("gemiapp.ingestion.discovery.get_gemi_client", return_value=client)

    @override_settings(GEMI_DISCOVERY_PAGE_SIZE=3, GEMI_DISCOVERY_MAX_PAGES=3, GEMI_DISCOVERY_OVERLAP_KNOWN_RECORDS=2,
                       GEMI_DISCOVERY_BOOTSTRAP_CONFIRMATIONS=2, GEMI_DISCOVERY_BOOTSTRAP_MAX_PAGES=3)
    def test_bootstrap_then_shadow_run_through_the_commands_and_the_task(self):
        out = StringIO()
        with self.fake_client():
            call_command("bootstrap_gemi_discovery_v2", stdout=out)
        self.assertIn("frontier previous=none", out.getvalue())
        self.assertEqual(get_cursor().high_water_mark, "1000")
        # The verified scan also records the backlog it found above the frontier.
        after_bootstrap = GemiDiscoveryObservation.objects.count()
        self.assertEqual(len(discovered()), 3)

        out = StringIO()
        with self.fake_client():
            call_command("run_gemi_discovery_v2", "--dry-run", stdout=out)
        self.assertIn("[dry-run] mode=shadow status=success", out.getvalue())
        self.assertEqual(GemiDiscoveryObservation.objects.count(), after_bootstrap)

        with self.fake_client():
            summary = run_gemi_discovery_v2_shadow_task()
        self.assertEqual((summary["mode"], summary["status"], summary["new_records"]), (SHADOW, "success", 3))
        self.assertEqual(len(discovered(summary["run_id"])), 3)
        # Shadow never stores the companies, so the run rediscovers the backlog the bootstrap recorded.
        self.assertEqual(discovered_rows(), 6)
        self.assertNotIn(
            "gemiapp.tasks.run_gemi_discovery_v2_shadow_task", [entry["func"] for entry in gemi_apps.SCHEDULES],
        )

    def test_command_validation(self):
        with self.fake_client(), self.assertRaises(CommandError):
            call_command("run_gemi_discovery_v2", "--max-pages", "0", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("run_gemi_discovery_v2", "--compare", "not-a-date", stdout=StringIO())
        with self.fake_client(), self.assertRaises(CommandError):
            call_command("run_gemi_discovery_v2", "--mode", INGEST, stdout=StringIO())  # flag is off
        with self.fake_client():
            call_command("bootstrap_gemi_discovery_v2", "--dry-run", stdout=StringIO())
        self.assertIsNone(get_cursor().high_water_mark_value)


class MultiDaySimulationTests(NoNetworkMixin, TestCase):
    """Several days of fixture pages, including a late publication, an out-of-order item, a duplicated page
    and a mid-run failure."""

    def setUp(self):
        for number in (1000, 999, 998):
            known_company(number)
        ready_cursor(1000)

    def ingest_locally(self, numbers):
        for number in numbers:  # what the legacy importer would have stored by the next day
            if not Company.objects.filter(gemi_number=str(number)).exists():
                known_company(number)

    def test_a_week_of_runs_keeps_the_cursor_truthful(self):
        day_one = [[item(1003), item(1002), item(1001)], [item(1000), item(999), item(998)]]
        client, _ = paged_client(copy.deepcopy(day_one))
        first = run_discovery(client=client, policy=POLICY, as_of=AS_OF)
        self.assertEqual((first.status, get_cursor().high_water_mark), ("success", "1003"))
        self.ingest_locally([1001, 1002, 1003])

        # Day 2: two new registrations and one late publication, with a duplicated page in between.
        day_two = AS_OF + timedelta(days=1)
        day_two_first = [item(1005, day_two.isoformat()), item(1004, "2025-11-02"), item(1003)]
        day_two_pages = [day_two_first, copy.deepcopy(day_two_first), [item(1002), item(1001), item(1000)]]
        client, _ = paged_client(day_two_pages)
        second = run_discovery(client=client, policy=POLICY, as_of=day_two)
        self.assertEqual((second.status, second.new_records, second.duplicate_records), ("success", 2, 3))
        self.assertEqual(discovered(second.run_id), {"1005": NEW_INCORPORATION, "1004": LATE_PUBLICATION})
        self.assertEqual(get_cursor().high_water_mark, "1005")
        self.ingest_locally([1004, 1005])

        # Day 3: the API returns an out-of-order record -- the cursor must freeze.
        client, _ = paged_client([[item(1007), item(1006), item(1008)], [item(1005), item(1004), item(1003)]])
        third = run_discovery(client=client, policy=POLICY, as_of=AS_OF + timedelta(days=2))
        self.assertEqual((third.status, get_cursor().high_water_mark), ("anomaly", "1005"))

        # Day 4: the same window, now well ordered, after the anomaly was reviewed.
        client, _ = paged_client([[item(1008), item(1007), item(1006)], [item(1005), item(1004), item(1003)]])
        fourth = run_discovery(client=client, policy=POLICY, as_of=AS_OF + timedelta(days=3))
        self.assertEqual((fourth.status, fourth.new_records, get_cursor().high_water_mark), ("success", 3, "1008"))
        self.assertEqual(GemiDiscoveryRun.objects.count(), 4)
        self.assertEqual(Company.objects.count(), 8)  # only what the legacy path stored; shadow added none

        # Day 5: the transport fails midway -- the frontier survives.
        client, _ = paged_client([[item(1010), item(1009), item(1008)], [item(1005)]], failure_after=1)
        fifth = run_discovery(client=client, policy=POLICY, as_of=AS_OF + timedelta(days=4))
        self.assertEqual((fifth.status, get_cursor().high_water_mark), ("failed", "1008"))


class CustomerParityTests(NoNetworkMixin, TestCase):
    """A shadow run changes nothing customer-visible."""

    def setUp(self):
        self.user = entitled_user()
        self.client.login(username="member@example.com", password="StrongPass123")
        items = [full_item("118717203000", TARGET.isoformat()), full_item("118717204000", TARGET.isoformat())]
        with patch("gemiapp.services.fetch_companies", side_effect=lambda _: copy.deepcopy(items)):
            import_for_date(TARGET)
        radar_for(self.user, "Αττική", prefectures=["ΑΤΤΙΚΗΣ"])
        ready_cursor(118717204000)

    def snapshot(self):
        companies = list(Company.objects.prefetch_related("activity_records").order_by("pk"))
        radars = list(CustomerRadar.objects.prefetch_related("activity_codes").order_by("pk"))
        state = {
            "companies": list(Company.objects.order_by("pk").values()),
            "activities": list(CompanyActivity.objects.order_by("pk").values()),
            "matches": list(RadarMatch.objects.order_by("pk").values_list("radar_id", "company_id", "matched_on")),
            "leads": list(UserCompanyLead.objects.order_by("pk").values_list("user_id", "company_id", "status")),
            "subscriptions": list(UserSubscription.objects.order_by("pk").values()),
            "monitoring": list(CompanyMonitoring.objects.values()),
            "import_runs": list(ImportRun.objects.order_by("pk").values()),
            "predicate": [(radar.pk, company.pk, company_matches_radar(company, radar)) for radar in radars for company in companies],
        }
        with patch("django.core.signing.time.time", return_value=1789000000.0):
            mail.outbox = []
            DigestDelivery.objects.all().delete()
            state["digest"] = (send_digests(TARGET), [(message.subject, message.body) for message in mail.outbox])
            state["export"] = self.client.get(reverse("export_csv")).content
        return state

    def test_a_shadow_run_leaves_every_customer_facing_result_identical(self):
        match_imported_companies(ImportRun.objects.create(target_date=TARGET, status="success"))
        before = self.snapshot()
        self.assertTrue(before["matches"])

        client, _ = paged_client([
            [item(118717204002), item(118717204001), item(118717204000)],
            [item(118717203000)],
        ])
        result = run_discovery(client=client, policy=POLICY, as_of=AS_OF)

        self.assertEqual((result.status, result.new_records), ("success", 2))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(CompanyMonitoring.objects.count(), 0)
