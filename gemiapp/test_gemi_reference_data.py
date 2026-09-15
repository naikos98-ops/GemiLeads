"""Tests for the GEMI reference tables and sync (gemiapp.ingestion.reference_data).

Mocked transports only -- no GEMI call.
"""

import copy
import inspect
import urllib.parse
from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import caches
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import CreateModel
from django.test import TestCase, override_settings
from django.utils import timezone

from .apps import SCHEDULES
from .ingestion import (
    GemiClient,
    GemiLane,
    GemiResponseValidationError,
    GemiRetryExhaustedError,
    SourceRecorder,
    sync_reference_data,
)
from .ingestion import reference_data as reference_module
from .ingestion.client import RawResponse
from .models import (
    ActivityCode, Company, CompanyActivity, CustomerRadar, DigestDelivery, GemiCompanyStatus, GemiDecisionSubject,
    GemiKad, GemiLegalType, GemiMunicipality, GemiOffice, GemiPrefecture, GemiReferenceSyncRun, GemiSourceRecord,
    RadarMatch, UserCompanyLead, UserSubscription,
)
from .tasks import sync_gemi_reference_data_task
from .test_gemi_client import BASE_URL, SECRET, NoNetworkMixin, make_client, response

REFERENCE_MODELS = (GemiKad, GemiPrefecture, GemiMunicipality, GemiCompanyStatus, GemiLegalType, GemiOffice, GemiDecisionSubject)
OFFICE_PHONE_SENTINEL = "2109990004821"
OFFICE_EMAIL_SENTINEL = "sentinel.office@example.invalid"
OFFICE_ADDRESS_SENTINEL = "SENTINEL-OFFICE-ADDRESS"


def reference_payloads():
    return {
        "/metadata/activities": [
            {"id": "62010000", "descr": "Δραστηριότητες προγραμματισμού", "descrEn": "Computer programming",
             "lastUpdated": "2026-02-25 15:56:01", "kadVersion": "kad_2026"},
            {"id": "62010000", "descr": "Δραστηριότητες προγραμματισμού ηλεκτρονικών υπολογιστών", "descrEn": None,
             "lastUpdated": "2015-12-11 18:35:52", "kadVersion": "kad_2008"},
            {"id": "35111000", "descr": "Παραγωγή ηλεκτρικής ενέργειας", "descrEn": None,
             "lastUpdated": "2015-12-11 18:35:52", "kadVersion": "kad_2008"},
        ],
        "/metadata/prefectures": [
            {"id": "5", "descr": "ΑΤΤΙΚΗΣ", "descrEn": "ATTICA", "lastUpdated": "2012-06-13 00:00:00"},
            {"id": "54", "descr": "ΑΘΗΝΩΝ", "descrEn": None, "lastUpdated": "2012-06-13 00:00:00"},
            {"id": "0", "descr": "Inadequate Info", "descrEn": None, "lastUpdated": "2011-02-21 14:17:08"},
        ],
        "/metadata/municipalities": [
            {"id": "61190", "prefectureId": "5", "descr": "ΚΗΦΙΣΙΑΣ / ΒΟΡΕΙΟΥ ΤΟΜΕΑ ΑΘΗΝΩΝ", "descrEn": None, "lastUpdated": "2025-03-26 16:01:46"},
            {"id": "61034", "prefectureId": "37", "descr": "ΑΒΔΗΡΩΝ / ΞΑΝΘΗΣ", "descrEn": None, "lastUpdated": "2025-03-26 16:01:46"},
        ],
        "/metadata/companyStatuses": [
            {"id": "3", "descr": "Ενεργή", "descrEn": "Active", "isActive": True, "lastUpdated": "2015-12-11 18:35:51"},
            {"id": "17", "descr": "Διαγραφή", "descrEn": "Deleted", "isActive": False, "lastUpdated": "2015-12-11 18:35:51"},
        ],
        "/metadata/legalTypes": [
            {"id": "19", "descr": "ΙΚΕ", "descrEn": "PC", "lastUpdated": "2024-11-29 17:46:10"},
            {"id": "16", "descr": "ΑΤΟΜΙΚΗ", "descrEn": None, "lastUpdated": "2007-09-26 18:03:59"},
        ],
        "/metadata/gemiOffices": [
            {"id": "3", "descr": "ΕΠΑΓΓΕΛΜΑΤΙΚΟ ΕΠΙΜΕΛΗΤΗΡΙΟ ΑΘΗΝΑΣ", "descrEn": None, "lastUpdated": "2026-04-03 12:26:45",
             "address": OFFICE_ADDRESS_SENTINEL, "city": "ΑΘΗΝΑ", "zipCode": "10564", "phone": OFFICE_PHONE_SENTINEL,
             "fax": None, "email": OFFICE_EMAIL_SENTINEL, "url": None},
        ],
        "/metadata/assemblySubjects": [
            {"id": "31", "descr": "Ανακοίνωση αύξησης μετοχικού κεφαλαίου", "descrEn": None, "lastUpdated": "2025-12-08 17:08:17"},
            {"id": "1", "descr": "Αλλαγή δ/νσης γραφείων-έδρας", "descrEn": None, "lastUpdated": "2015-12-11 17:50:42"},
        ],
    }


EXPECTED_COUNTS = {"activities": 3, "prefectures": 3, "municipalities": 2, "company_statuses": 2, "legal_types": 2, "gemi_offices": 1, "decision_subjects": 2}


def route_by_path(payloads):
    def route(url):
        path = urllib.parse.urlsplit(url).path.removeprefix("/api/opendata/v1")
        value = payloads[path]
        return value if isinstance(value, RawResponse) else response(200, value)
    return route


def reference_client(payloads, *, recorder=None, max_attempts=4):
    _, transport, clock, budget = make_client(route_by_path(payloads))
    client = GemiClient(
        api_key=SECRET, base_url=BASE_URL, budget=budget, transport=transport, timeout=60,
        max_attempts=max_attempts, sleep=clock.sleep, clock=clock.time, source_recorder=recorder,
    )
    return client, transport, budget


def run_sync(payloads, **kwargs):
    client, _, _ = reference_client(payloads, max_attempts=kwargs.pop("max_attempts", 4))
    return sync_reference_data(client=client, **kwargs)


def reference_snapshot():
    return {model.__name__: list(model.objects.order_by("pk").values()) for model in REFERENCE_MODELS}


def created_counts(result):
    return {key: counts.created for key, counts in result.families.items()}


class FullSyncTests(NoNetworkMixin, TestCase):
    def test_a_full_sync_imports_all_seven_families(self):
        result = run_sync(reference_payloads())

        self.assertFalse(result.dry_run)
        self.assertEqual(created_counts(result), EXPECTED_COUNTS)
        for model, expected in zip(REFERENCE_MODELS, EXPECTED_COUNTS.values()):
            self.assertEqual(model.objects.filter(is_present=True).count(), expected, model.__name__)

        status = GemiCompanyStatus.objects.get(source_id="17")
        self.assertEqual((status.description, status.description_en, status.source_is_active), ("Διαγραφή", "Deleted", False))
        self.assertIsNone(GemiLegalType.objects.get(source_id="19").source_is_active)
        self.assertEqual(GemiPrefecture.objects.get(source_id="5").source_last_updated, "2012-06-13 00:00:00")
        self.assertEqual(GemiDecisionSubject.objects.get(source_id="31").description, "Ανακοίνωση αύξησης μετοχικού κεφαλαίου")

        run = GemiReferenceSyncRun.objects.get(pk=result.run_id)
        self.assertEqual(run.status, "success")
        self.assertEqual(run.families, list(EXPECTED_COUNTS))
        self.assertEqual({key: value["created"] for key, value in run.counts.items()}, EXPECTED_COUNTS)
        self.assertGreaterEqual(run.duration_seconds, 0)

    def test_gemi_office_contact_fields_are_not_stored_or_logged(self):
        with self.assertLogs("gemiapp", level="DEBUG") as logs:
            run_sync(reference_payloads())
        office = GemiOffice.objects.get()
        stored = str(list(GemiOffice.objects.values()))
        for sentinel in (OFFICE_PHONE_SENTINEL, OFFICE_EMAIL_SENTINEL, OFFICE_ADDRESS_SENTINEL, SECRET):
            self.assertNotIn(sentinel, stored)
            self.assertNotIn(sentinel, "\n".join(logs.output))
        self.assertEqual(office.description, "ΕΠΑΓΓΕΛΜΑΤΙΚΟ ΕΠΙΜΕΛΗΤΗΡΙΟ ΑΘΗΝΑΣ")
        self.assertNotIn("Δραστηριότητες προγραμματισμού", "\n".join(logs.output))

    def test_requests_go_through_the_shared_client_in_the_lowest_priority_lane(self):
        client, transport, budget = reference_client(reference_payloads())
        sync_reference_data(client=client)

        self.assertEqual(len(transport.calls), 7)
        self.assertEqual(budget.lanes, [GemiLane.DOCUMENTS] * 7)
        self.assertEqual(
            sorted(urllib.parse.urlsplit(call["url"]).path.rsplit("/", 1)[-1] for call in transport.calls),
            sorted(["activities", "prefectures", "municipalities", "companyStatuses", "legalTypes", "gemiOffices", "assemblySubjects"]),
        )
        source = inspect.getsource(reference_module)
        self.assertNotIn("urllib", source)
        self.assertNotIn("urlopen", source)

    def test_selected_families_only(self):
        client, transport, _ = reference_client(reference_payloads())
        result = sync_reference_data(["prefectures", "legal_types"], client=client)

        self.assertEqual(list(result.families), ["prefectures", "legal_types"])
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(GemiKad.objects.count(), 0)
        with self.assertRaises(ValueError):
            sync_reference_data(["regions"], client=client)


class ChangeTrackingTests(NoNetworkMixin, TestCase):
    def setUp(self):
        super().setUp()
        run_sync(reference_payloads())

    def test_an_identical_second_sync_is_idempotent(self):
        before = {model.__name__: {row.pk: (row.first_seen_at, row.last_seen_at, row.updated_at) for row in model.objects.all()} for model in REFERENCE_MODELS}
        result = run_sync(reference_payloads())

        for key, counts in result.families.items():
            self.assertEqual((counts.created, counts.updated, counts.reappeared, counts.retired), (0, 0, 0, 0), key)
            self.assertEqual(counts.unchanged, EXPECTED_COUNTS[key], key)
        for model in REFERENCE_MODELS:
            rows = {row.pk: row for row in model.objects.all()}
            self.assertEqual(set(rows), set(before[model.__name__]), model.__name__)
            for pk, (first_seen, last_seen, updated) in before[model.__name__].items():
                self.assertEqual(rows[pk].first_seen_at, first_seen)
                self.assertGreaterEqual(rows[pk].last_seen_at, last_seen)
                self.assertEqual(rows[pk].updated_at, updated)

    def test_a_changed_description_updates_in_place(self):
        payloads = reference_payloads()
        payloads["/metadata/legalTypes"][0]["descr"] = "Ιδιωτική Κεφαλαιουχική Εταιρεία"
        original = GemiLegalType.objects.get(source_id="19")

        result = run_sync(payloads)

        updated = GemiLegalType.objects.get(source_id="19")
        self.assertEqual((updated.pk, updated.first_seen_at), (original.pk, original.first_seen_at))
        self.assertEqual(updated.description, "Ιδιωτική Κεφαλαιουχική Εταιρεία")
        self.assertEqual((result.families["legal_types"].updated, result.families["legal_types"].unchanged), (1, 1))

    def test_a_new_source_item_is_created(self):
        payloads = reference_payloads()
        payloads["/metadata/companyStatuses"].append({"id": "7", "descr": "Λύση - Εκκαθάριση", "isActive": False})

        result = run_sync(payloads)

        self.assertEqual(result.families["company_statuses"].created, 1)
        self.assertFalse(GemiCompanyStatus.objects.get(source_id="7").source_is_active)

    def test_a_disappeared_item_is_retired_not_deleted_and_revived_when_it_returns(self):
        payloads = reference_payloads()
        removed = payloads["/metadata/assemblySubjects"].pop(1)
        original = GemiDecisionSubject.objects.get(source_id="1")

        retired_result = run_sync(payloads)
        retired = GemiDecisionSubject.objects.get(source_id="1")
        self.assertEqual(retired_result.families["decision_subjects"].retired, 1)
        self.assertEqual(retired.pk, original.pk)
        self.assertFalse(retired.is_present)
        self.assertIsNotNone(retired.retired_at)
        self.assertEqual(retired.description, "Αλλαγή δ/νσης γραφείων-έδρας")

        still_absent = run_sync(payloads)
        self.assertEqual(still_absent.families["decision_subjects"].retired, 0)

        payloads["/metadata/assemblySubjects"].append(removed)
        revived_result = run_sync(payloads)
        revived = GemiDecisionSubject.objects.get(source_id="1")
        self.assertEqual(revived_result.families["decision_subjects"].reappeared, 1)
        self.assertEqual((revived.pk, revived.is_present, revived.retired_at), (original.pk, True, None))
        self.assertEqual(GemiDecisionSubject.objects.filter(source_id="1").count(), 1)

    def test_kad_2008_and_kad_2026_are_separate_reference_entries(self):
        rows = {row.kad_version: row for row in GemiKad.objects.filter(source_id="62010000")}
        self.assertEqual(set(rows), {"kad_2008", "kad_2026"})

        payloads = reference_payloads()
        payloads["/metadata/activities"] = [entry for entry in payloads["/metadata/activities"] if entry["kadVersion"] == "kad_2026"] + [
            {**payloads["/metadata/activities"][2], "descr": "Παραγωγή ενέργειας (νέα περιγραφή)"},
        ]
        result = run_sync(payloads)

        self.assertEqual(result.families["activities"].retired, 1)  # only the 2008 62010000 entry
        self.assertFalse(GemiKad.objects.get(source_id="62010000", kad_version="kad_2008").is_present)
        current = GemiKad.objects.get(source_id="62010000", kad_version="kad_2026")
        self.assertTrue(current.is_present)
        self.assertEqual(current.pk, rows["kad_2026"].pk)

    def test_municipality_prefecture_ids_are_kept_without_a_foreign_key(self):
        field = GemiMunicipality._meta.get_field("source_prefecture_id")
        self.assertFalse(field.is_relation)
        abdera = GemiMunicipality.objects.get(source_id="61034")
        self.assertEqual(abdera.source_prefecture_id, "37")
        self.assertFalse(GemiPrefecture.objects.filter(source_id="37").exists())

    def test_null_and_blank_descriptions_stay_null(self):
        payloads = reference_payloads()
        payloads["/metadata/prefectures"][1]["descr"] = "   "
        payloads["/metadata/prefectures"][1]["lastUpdated"] = None
        run_sync(payloads)

        athens = GemiPrefecture.objects.get(source_id="54")
        self.assertIsNone(athens.description)
        self.assertIsNone(athens.description_en)
        self.assertIsNone(athens.source_last_updated)
        for model in REFERENCE_MODELS:
            self.assertFalse(model.objects.filter(description="None").exists())
            self.assertFalse(model.objects.filter(description_en="None").exists())


class TransactionSafetyTests(NoNetworkMixin, TestCase):
    def setUp(self):
        super().setUp()
        run_sync(reference_payloads())
        self.changed = reference_payloads()
        self.changed["/metadata/activities"][0]["descr"] = "ΑΛΛΑΓΜΕΝΗ ΠΕΡΙΓΡΑΦΗ"
        self.changed["/metadata/prefectures"].pop()
        self.changed["/metadata/legalTypes"].append({"id": "1", "descr": "ΑΕ"})

    def assertAttemptChangedNothing(self, before, error_type, **kwargs):
        with self.assertRaises(error_type):
            run_sync(self.changed, **kwargs)
        self.assertEqual(reference_snapshot(), before)
        run = GemiReferenceSyncRun.objects.order_by("-pk").first()
        self.assertEqual(run.status, "failed")
        self.assertTrue(run.error_message)

    def test_a_validation_failure_in_the_last_family_commits_nothing(self):
        before = reference_snapshot()
        self.changed["/metadata/assemblySubjects"] = {"unexpected": "object"}
        self.assertAttemptChangedNothing(before, GemiResponseValidationError)

    def test_an_http_failure_in_the_last_family_commits_nothing(self):
        before = reference_snapshot()
        self.changed["/metadata/assemblySubjects"] = response(503, body=b"")
        self.assertAttemptChangedNothing(before, GemiRetryExhaustedError, max_attempts=1)

    def test_a_malformed_payload_on_an_empty_database_writes_no_reference_row(self):
        for model in REFERENCE_MODELS:
            model.objects.all().delete()
        payloads = reference_payloads()
        payloads["/metadata/municipalities"][0]["prefectureId"] = "ΑΤΤ"
        with self.assertRaises(GemiResponseValidationError):
            run_sync(payloads)
        self.assertEqual(sum(model.objects.count() for model in REFERENCE_MODELS), 0)

    def test_a_database_failure_while_applying_rolls_back_every_family(self):
        before = reference_snapshot()
        self.changed["/metadata/assemblySubjects"][0]["descr"] = "ΑΛΛΑΓΜΕΝΟ ΘΕΜΑ"
        with patch.object(GemiDecisionSubject.objects, "bulk_update", side_effect=DatabaseError("disk full")):
            self.assertAttemptChangedNothing(before, DatabaseError)

    def test_a_concurrent_sync_is_refused(self):
        caches["shared"].add(reference_module.REFERENCE_SYNC_LOCK_KEY, "1", 60)
        runs_before = GemiReferenceSyncRun.objects.count()
        with self.assertRaises(RuntimeError):
            run_sync(reference_payloads())
        self.assertEqual(GemiReferenceSyncRun.objects.count(), runs_before)
        caches["shared"].delete(reference_module.REFERENCE_SYNC_LOCK_KEY)


class AnomalyTests(NoNetworkMixin, TestCase):
    def setUp(self):
        super().setUp()
        run_sync(reference_payloads())

    def test_an_empty_response_for_a_populated_family_retires_nothing_and_warns(self):
        payloads = reference_payloads()
        payloads["/metadata/prefectures"] = []
        with self.assertLogs("gemiapp.ingestion.reference_data", level="WARNING") as logs:
            result = run_sync(payloads)

        self.assertTrue(result.families["prefectures"].retirement_skipped)
        self.assertEqual(result.families["prefectures"].retired, 0)
        self.assertEqual(GemiPrefecture.objects.filter(is_present=True).count(), 3)
        self.assertIn("prefectures: the source returned 0 item(s) while 3 are present locally", "\n".join(logs.output))
        self.assertEqual(GemiReferenceSyncRun.objects.get(pk=result.run_id).anomalies, result.anomalies)

        forced = run_sync(payloads, force_retire=True)
        self.assertEqual(forced.families["prefectures"].retired, 3)
        self.assertEqual(GemiPrefecture.objects.filter(is_present=True).count(), 0)
        self.assertEqual(GemiPrefecture.objects.count(), 3)

    def test_a_count_collapse_warns_but_an_ordinary_change_does_not(self):
        payloads = reference_payloads()
        payloads["/metadata/activities"] = payloads["/metadata/activities"][:1]
        collapsed = run_sync(payloads)
        self.assertTrue(collapsed.families["activities"].retirement_skipped)

        ordinary = reference_payloads()
        ordinary["/metadata/activities"].pop()
        result = run_sync(ordinary)
        self.assertFalse(result.families["activities"].retirement_skipped)
        self.assertEqual(result.families["activities"].retired, 1)
        self.assertEqual(result.anomalies, [])

    def test_conflicting_duplicates_are_resolved_deterministically(self):
        payloads = reference_payloads()
        payloads["/metadata/legalTypes"].append({"id": "19", "descr": "ΙΚΕ (διπλή)"})
        payloads["/metadata/legalTypes"].append(copy.deepcopy(payloads["/metadata/legalTypes"][1]))
        first = run_sync(payloads)
        chosen = GemiLegalType.objects.get(source_id="19").description
        reversed_payloads = reference_payloads()
        reversed_payloads["/metadata/legalTypes"] = list(reversed(payloads["/metadata/legalTypes"]))
        second = run_sync(reversed_payloads)

        self.assertEqual(
            (first.families["legal_types"].conflicting_duplicates, second.families["legal_types"].conflicting_duplicates), (1, 1),
        )
        self.assertEqual(GemiLegalType.objects.filter(source_id="19").count(), 1)
        # The kept item is the first by canonical JSON order, whatever order the response used.
        self.assertEqual(chosen, "ΙΚΕ (διπλή)")
        self.assertEqual(GemiLegalType.objects.get(source_id="19").description, chosen)
        self.assertEqual(second.families["legal_types"].updated, 0)
        self.assertIn("legal_types: 1 conflicting item(s)", " ".join(first.anomalies))


class DryRunAndSourceRecordTests(NoNetworkMixin, TestCase):
    def test_a_dry_run_reports_changes_and_writes_nothing(self):
        result = run_sync(reference_payloads(), dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertIsNone(result.run_id)
        self.assertEqual(created_counts(result), EXPECTED_COUNTS)
        self.assertEqual(sum(model.objects.count() for model in REFERENCE_MODELS), 0)
        self.assertEqual(GemiReferenceSyncRun.objects.count(), 0)

        run_sync(reference_payloads())
        payloads = reference_payloads()
        payloads["/metadata/legalTypes"][0]["descr"] = "ΝΕΑ ΠΕΡΙΓΡΑΦΗ"
        payloads["/metadata/assemblySubjects"].pop()
        before = reference_snapshot()
        preview = run_sync(payloads, dry_run=True)
        self.assertEqual((preview.families["legal_types"].updated, preview.families["decision_subjects"].retired), (1, 1))
        self.assertEqual(reference_snapshot(), before)

    @override_settings(GEMI_SOURCE_RECORDS_ENABLED=True)
    def test_a_dry_run_never_gets_a_source_recorder(self):
        fake_client = MagicMock()
        with patch.object(reference_module, "GemiClient", return_value=fake_client) as client_class, \
             patch.object(reference_module, "get_gemi_client") as factory:
            fake_client.get.side_effect = lambda path, **kwargs: reference_payloads()[path]
            sync_reference_data(dry_run=True)
        client_class.assert_called_once_with()
        factory.assert_not_called()
        self.assertEqual(GemiSourceRecord.objects.count(), 0)

    def test_the_sync_works_with_source_records_off_and_records_provenance_when_on(self):
        run_sync(reference_payloads())
        self.assertEqual(GemiSourceRecord.objects.count(), 0)
        without_records = {model.__name__: sorted(model.objects.values_list("source_id", "description")) for model in REFERENCE_MODELS}

        for model in REFERENCE_MODELS:
            model.objects.all().delete()
        client, _, _ = reference_client(reference_payloads(), recorder=SourceRecorder(store_payload=True))
        sync_reference_data(client=client)

        with_records = {model.__name__: sorted(model.objects.values_list("source_id", "description")) for model in REFERENCE_MODELS}
        self.assertEqual(without_records, with_records)
        self.assertEqual(GemiSourceRecord.objects.count(), 7)
        self.assertEqual(set(GemiSourceRecord.objects.values_list("family", "retention_class")), {("reference_data", "audit")})
        office_record = GemiSourceRecord.objects.get(endpoint="/metadata/gemiOffices")
        self.assertNotIn(OFFICE_PHONE_SENTINEL, str(office_record.sanitised_payload))


class CommandTaskAndProductionSafetyTests(NoNetworkMixin, TestCase):
    def fake_default_client(self, payloads=None):
        client, _, _ = reference_client(payloads or reference_payloads())
        return patch.object(reference_module, "_default_client", return_value=client)

    def test_the_command_prints_counts_only(self):
        out = StringIO()
        with self.fake_default_client():
            call_command("sync_gemi_reference_data", "--dry-run", stdout=out)
        output = out.getvalue()
        self.assertIn("[dry-run] activities: fetched=3 created=3", output)
        self.assertIn("decision_subjects", output)
        for text in (SECRET, "Δραστηριότητες προγραμματισμού", OFFICE_PHONE_SENTINEL):
            self.assertNotIn(text, output)
        self.assertEqual(sum(model.objects.count() for model in REFERENCE_MODELS), 0)

        out = StringIO()
        with self.fake_default_client():
            call_command("sync_gemi_reference_data", "--family", "prefectures", stdout=out)
        self.assertEqual(GemiPrefecture.objects.count(), 3)
        self.assertEqual(GemiKad.objects.count(), 0)
        with self.assertRaises(CommandError):
            call_command("sync_gemi_reference_data", "--family", "regions", stdout=StringIO())

    def test_the_task_returns_counts_and_is_not_scheduled(self):
        with self.fake_default_client():
            counts = sync_gemi_reference_data_task()
        self.assertEqual({key: value["created"] for key, value in counts.items()}, EXPECTED_COUNTS)
        self.assertNotIn("gemiapp.tasks.sync_gemi_reference_data_task", [entry["func"] for entry in SCHEDULES])

    def test_the_sync_does_not_touch_live_catalogue_company_radar_or_billing_data(self):
        user = User.objects.create_user("member@example.com", "member@example.com", "StrongPass123")
        subscription = user.subscription
        subscription.tier, subscription.status = "pro", "active"
        subscription.save()
        company = Company.objects.create(gemi_number="118717203000", name="ΕΤΑΙΡΕΙΑ ΙΚΕ", incorporation_date=timezone.localdate(), prefecture="ΑΤΤΙΚΗΣ", legal_type="ΙΚΕ")
        CompanyActivity.objects.create(company=company, code="62010000", activity_type="Κύρια")
        radar = CustomerRadar.objects.create(user=user, name="Αττική", prefectures=["ΑΤΤΙΚΗΣ"], monitor_from=timezone.now() - timedelta(days=5))
        lead = UserCompanyLead.objects.create(user=user, company=company)
        RadarMatch.objects.create(radar=radar, lead=lead, company=company, matched_on=timezone.localdate())
        DigestDelivery.objects.create(user=user, digest_date=timezone.localdate(), status="sent")

        def live_state():
            return {
                model.__name__: list(model.objects.order_by("pk").values())
                for model in (Company, CompanyActivity, CustomerRadar, UserCompanyLead, RadarMatch, DigestDelivery, UserSubscription)
            } | {"ActivityCode": (ActivityCode.objects.count(), list(ActivityCode.objects.order_by("pk").values_list("pk", "normalized_code", "description")[:50]))}

        before = live_state()
        run_sync(reference_payloads())
        self.assertEqual(live_state(), before)

    def test_the_migration_only_creates_the_reference_tables_with_their_identity_constraints(self):
        migration = MigrationLoader(None, ignore_no_migrations=True).get_migration("gemiapp", "0033_gemi_reference_data")

        self.assertEqual(migration.dependencies, [("gemiapp", "0032_gemi_source_records")])
        self.assertTrue(all(isinstance(operation, CreateModel) for operation in migration.operations))
        self.assertEqual(sorted(operation.name for operation in migration.operations), [
            "GemiCompanyStatus", "GemiDecisionSubject", "GemiKad", "GemiLegalType", "GemiMunicipality",
            "GemiOffice", "GemiPrefecture", "GemiReferenceSyncRun",
        ])
        self.assertEqual({c.name: tuple(c.fields) for c in GemiKad._meta.constraints}, {"unique_gemi_kad_code_version": ("source_id", "kad_version")})
        for model in REFERENCE_MODELS[1:]:
            self.assertEqual([tuple(c.fields) for c in model._meta.constraints], [("source_id",)], model.__name__)
