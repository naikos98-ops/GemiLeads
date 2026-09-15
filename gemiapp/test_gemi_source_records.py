"""Tests for minimised GEMI source records (gemiapp.ingestion.source_records, GemiSourceRecord).

Fixtures and mocked transports only -- no GEMI call.
"""

import copy
import json
from datetime import date, datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, IntegrityError, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import CreateModel
from django.forms.models import model_to_dict
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .apps import SCHEDULES
from .ingestion import (
    GEMI_NORMALIZER_VERSION,
    GEMI_RESPONSE_SCHEMA_VERSION,
    GEMI_SOURCE_RECORD_FORMAT_VERSION,
    GemiClient,
    GemiLane,
    GemiSourceRecordError,
    ResponseFamily,
    SourceRecorder,
    canonical_request_params,
    get_gemi_client,
    payload_hash,
    purge_expired_source_records,
    sanitise_payload,
)
from .ingestion.source_records import RETENTION_BY_FAMILY, SOURCE_FAMILY_BY_RESPONSE, canonical_json
from .models import (
    Company, CompanyActivity, CustomerRadar, DigestDelivery, GemiSourceRecord, ImportRun, RadarMatch,
    UserCompanyLead, UserSubscription,
)
from .tasks import purge_gemi_source_records_task
from .test_gemi_client import BASE_URL, SECRET, FakeClock, NoNetworkMixin, make_client, response, routed
from .test_gemi_validation import (
    CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL, REFERENCE_SAMPLES, full_item, page, snapshot_rows,
)

FETCHED_AT = datetime(2026, 9, 15, 7, 30, tzinfo=dt_timezone.utc)
SEARCH_PARAMS = {"isActive": "true", "resultsSortBy": "-incorporationDate", "resultsOffset": 0, "resultsSize": 200}
TARGET = date(2026, 9, 14)

SENTINELS = (
    PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, "SENTINEL-FAX-4821", "SENTINEL-BUSINESS-4821",
    "sentinel.person@example.invalid", "SENTINEL-NESTED-4821", "SENTINEL-COMPANY-NAME", "SENTINEL-STREET",
    "990004821", "https://sentinel.example.invalid",
)


def sentinel_item(ar_gemi="118717203000", day="2026-09-14"):
    item = full_item(
        ar_gemi, day, coNameEl="SENTINEL-COMPANY-NAME ΙΚΕ", street="SENTINEL-STREET", fax="SENTINEL-FAX-4821",
        afm="990004821", url="https://sentinel.example.invalid",
    )
    item["persons"][0]["businessName"] = "SENTINEL-BUSINESS-4821"
    item["persons"].append({
        "personName": "SENTINEL-NESTED-4821", "role": "Μέτοχος",
        "contact": {"email": "sentinel.person@example.invalid", "phone": PHONE_SENTINEL},
    })
    return item


def record(
    payload, *, family=ResponseFamily.COMPANY_SEARCH, endpoint="/companies", params=None, fetched_at=FETCHED_AT,
    store_payload=True, **kwargs,
):
    """Record through a SourceRecorder that opts in to payload storage unless told otherwise, so the
    sanitisation path is exercised. The settings default (off) is tested on its own."""
    return SourceRecorder(store_payload=store_payload, **kwargs).record(
        response_family=family, endpoint=endpoint, params=SEARCH_PARAMS if params is None else params,
        http_status=200, payload=payload, fetched_at=fetched_at, request_id="req-4821",
    )


def recording_client(*outcomes, recorder=None, clock=None, max_attempts=4):
    _, transport, clock, budget = make_client(*outcomes, clock=clock)
    client = GemiClient(
        api_key=SECRET, base_url=BASE_URL, budget=budget, transport=transport, timeout=60,
        max_attempts=max_attempts, sleep=clock.sleep, clock=clock.time, source_recorder=recorder,
    )
    return client, transport, clock


class SourceRecordModelTests(TestCase):
    def test_a_record_carries_its_metadata_versions_and_retention(self):
        payload = page(full_item(), full_item("118717204000"))
        source_record, created = record(payload)

        self.assertTrue(created)
        source_record.refresh_from_db()
        self.assertEqual(source_record.source, "gemi_opendata")
        self.assertEqual((source_record.family, source_record.response_family), ("company_search", "company_search"))
        self.assertEqual(source_record.endpoint, "/companies")
        self.assertEqual(source_record.request_params, {
            "isActive": "true", "resultsOffset": "0", "resultsSize": "200", "resultsSortBy": "-incorporationDate",
        })
        self.assertEqual(len(source_record.request_fingerprint), 64)
        self.assertEqual(len(source_record.observation_key), 64)
        self.assertEqual((source_record.fetched_at, source_record.http_status), (FETCHED_AT, 200))
        self.assertEqual(source_record.gateway_request_id, "req-4821")
        self.assertEqual(source_record.payload_hash, payload_hash(payload))
        self.assertEqual(source_record.result_count, 2)
        self.assertEqual(source_record.response_schema_version, GEMI_RESPONSE_SCHEMA_VERSION)
        self.assertEqual(source_record.normalizer_version, GEMI_NORMALIZER_VERSION)
        self.assertEqual(source_record.record_format_version, GEMI_SOURCE_RECORD_FORMAT_VERSION)
        self.assertEqual(source_record.retention_class, "standard")
        self.assertEqual(source_record.expires_at, FETCHED_AT + timedelta(days=30))
        self.assertIsNotNone(source_record.created_at)
        self.assertIn(source_record.payload_hash[:12], str(source_record))

    def test_indexes_and_the_unique_observation_key(self):
        index_names = {index.name for index in GemiSourceRecord._meta.indexes}
        self.assertEqual(index_names, {"gemisrc_request_fetched_idx", "gemisrc_family_fetched_idx", "gemisrc_expires_idx"})
        self.assertTrue(GemiSourceRecord._meta.get_field("observation_key").unique)

        source_record, _ = record(page(full_item()))
        duplicate = model_to_dict(source_record, exclude=["id"])
        with self.assertRaises(IntegrityError), transaction.atomic():
            GemiSourceRecord.objects.create(**duplicate)

    def test_family_and_retention_mappings_match_the_model_choices(self):
        self.assertEqual(set(SOURCE_FAMILY_BY_RESPONSE), set(ResponseFamily))
        self.assertLessEqual(set(SOURCE_FAMILY_BY_RESPONSE.values()), set(GemiSourceRecord.Family.values))
        self.assertEqual(set(RETENTION_BY_FAMILY), set(GemiSourceRecord.Family.values))
        self.assertLessEqual(set(RETENTION_BY_FAMILY.values()), set(GemiSourceRecord.Retention.values))

    def test_records_from_different_versions_coexist(self):
        current, _ = record(page(full_item()))
        future = model_to_dict(current, exclude=["id"])
        future.update(observation_key="f" * 64, response_schema_version=2, normalizer_version=None, record_format_version=2)
        GemiSourceRecord.objects.create(**future)

        self.assertEqual(GemiSourceRecord.objects.filter(response_schema_version=1).count(), 1)
        self.assertEqual(GemiSourceRecord.objects.filter(response_schema_version=2, normalizer_version__isnull=True).count(), 1)

    def test_the_migration_only_creates_the_source_record_table(self):
        migration = MigrationLoader(None, ignore_no_migrations=True).get_migration("gemiapp", "0032_gemi_source_records")

        self.assertEqual(migration.dependencies, [("gemiapp", "0031_cancel_pending_outreach")])
        self.assertEqual(len(migration.operations), 1)
        self.assertIsInstance(migration.operations[0], CreateModel)
        self.assertEqual(migration.operations[0].name, "GemiSourceRecord")


class PayloadHashTests(TestCase):
    def test_key_order_does_not_change_the_hash(self):
        first = {"searchMetadata": {"totalCount": 1, "resultsOffset": 0}, "searchResults": [{"arGemi": "1", "coNameEl": "Α"}]}
        second = {"searchResults": [{"coNameEl": "Α", "arGemi": "1"}], "searchMetadata": {"resultsOffset": 0, "totalCount": 1}}
        self.assertEqual(payload_hash(first), payload_hash(second))
        self.assertEqual(canonical_json({"b": 1, "a": "ά"}), '{"a":"ά","b":1}'.encode("utf-8"))

    def test_a_value_change_changes_the_hash(self):
        payload = page(full_item())
        changed = copy.deepcopy(payload)
        changed["searchResults"][0]["status"]["id"] = 17
        self.assertNotEqual(payload_hash(payload), payload_hash(changed))

    def test_array_order_is_part_of_the_source_hash(self):
        self.assertNotEqual(payload_hash(page(full_item("1"), full_item("2"))), payload_hash(page(full_item("2"), full_item("1"))))

    def test_hashing_does_not_mutate_the_payload(self):
        payload = page(sentinel_item())
        before = copy.deepcopy(payload)
        payload_hash(payload)
        self.assertEqual(payload, before)

    def test_personal_fields_influence_the_hash_but_are_never_stored(self):
        payload = page(sentinel_item())
        changed_person = copy.deepcopy(payload)
        changed_person["searchResults"][0]["persons"][0]["percentage"] = "50"
        self.assertNotEqual(payload_hash(payload), payload_hash(changed_person))

        source_record, _ = record(payload)
        self.assertEqual(source_record.payload_hash, payload_hash(payload))
        stored = json.dumps(source_record.sanitised_payload, ensure_ascii=False)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, stored)


class SanitisationTests(NoNetworkMixin, TestCase):
    def test_sentinels_never_reach_the_database_reprs_logs_or_admin(self):
        client, _, _ = recording_client(response(200, page(sentinel_item())), recorder=SourceRecorder(store_payload=True))
        with self.assertLogs("gemiapp", level="DEBUG") as logs:
            client.search_companies(SEARCH_PARAMS, lane=GemiLane.DIGEST_IMPORT)

        source_record = GemiSourceRecord.objects.get()
        admin_user = User.objects.create_superuser("admin@example.com", "admin@example.com", "StrongPass123")
        self.client.force_login(admin_user)
        change_page = self.client.get(reverse("admin:gemiapp_gemisourcerecord_change", args=[source_record.pk]))
        list_page = self.client.get(reverse("admin:gemiapp_gemisourcerecord_changelist"))
        self.assertEqual((change_page.status_code, list_page.status_code), (200, 200))

        renderings = [
            json.dumps(model_to_dict(source_record), ensure_ascii=False, default=str),
            str(source_record), repr(source_record), "\n".join(logs.output),
            change_page.content.decode("utf-8"), list_page.content.decode("utf-8"),
        ]
        for rendering in renderings:
            for sentinel in SENTINELS + (SECRET,):
                self.assertNotIn(sentinel, rendering)

    def test_the_stored_company_payload_is_the_history_approved_normalised_record(self):
        source_record, _ = record(page(sentinel_item()))
        payload = source_record.sanitised_payload

        self.assertEqual(payload["as_of"], "2026-09-15")
        self.assertEqual(payload["search_metadata"], {"total_count": 1, "results_offset": 0})
        (company,) = payload["companies"]
        self.assertEqual(list(company), [
            "ar_gemi", "status", "is_active", "legal_type", "gemi_office", "prefecture", "municipality",
            "city", "postal_code", "incorporation_date", "last_status_change", "activities", "activities_without_code",
        ])
        self.assertEqual(company["ar_gemi"], "118717203000")
        self.assertEqual(company["prefecture"], {"id": "5", "description": "ΑΤΤΙΚΗΣ"})
        self.assertEqual(company["activities"][0]["kad_version"], "kad_2008")

    def test_company_detail_and_reference_data_payloads(self):
        detail, _ = record(sentinel_item(), family=ResponseFamily.COMPANY_DETAIL, endpoint="/companies/118717203000", params={})
        self.assertEqual(set(detail.sanitised_payload), {"as_of", "company"})
        self.assertEqual((detail.family, detail.retention_class, detail.result_count), ("company_detail", "standard", 1))

        offices = [REFERENCE_SAMPLES[ResponseFamily.GEMI_OFFICES] | {"phone": PHONE_SENTINEL, "email": CONTACT_SENTINEL}]
        reference, _ = record(offices, family=ResponseFamily.GEMI_OFFICES, endpoint="/metadata/gemiOffices", params={})
        self.assertEqual(reference.sanitised_payload, {"items": [{
            "id": "3", "descr": "ΕΠΑΓΓΕΛΜΑΤΙΚΟ ΕΠΙΜΕΛΗΤΗΡΙΟ ΑΘΗΝΑΣ", "descrEn": None, "lastUpdated": "2026-04-03 12:26:45",
        }]})
        self.assertEqual((reference.family, reference.retention_class), ("reference_data", "audit"))
        self.assertIsNone(reference.normalizer_version)

        payload, version = sanitise_payload(ResponseFamily.MUNICIPALITIES, [REFERENCE_SAMPLES[ResponseFamily.MUNICIPALITIES]], as_of=date(2026, 9, 15))
        self.assertEqual(payload["items"][0]["prefectureId"], "5")
        self.assertIsNone(version)

    def test_payload_storage_is_off_by_default_and_explicitly_opt_in(self):
        self.assertFalse(settings.GEMI_SOURCE_RECORDS_STORE_PAYLOAD)
        payload = page(full_item())

        default_record, _ = SourceRecorder().record(
            response_family=ResponseFamily.COMPANY_SEARCH, endpoint="/companies", params=SEARCH_PARAMS,
            http_status=200, payload=payload, fetched_at=FETCHED_AT,
        )
        self.assertIsNone(default_record.sanitised_payload)
        self.assertIsNone(default_record.normalizer_version)
        self.assertEqual(default_record.payload_hash, payload_hash(payload))
        self.assertEqual((default_record.result_count, default_record.retention_class), (1, "standard"))

        with override_settings(GEMI_SOURCE_RECORDS_STORE_PAYLOAD=True):
            opted_in, created = SourceRecorder().record(
                response_family=ResponseFamily.COMPANY_SEARCH, endpoint="/companies", params=SEARCH_PARAMS,
                http_status=200, payload=payload, fetched_at=FETCHED_AT + timedelta(hours=2),
            )
        self.assertTrue(created)
        self.assertEqual(opted_in.normalizer_version, GEMI_NORMALIZER_VERSION)
        self.assertEqual(opted_in.sanitised_payload["companies"][0]["ar_gemi"], "118717203000")

        explicitly_off, _ = record(payload, store_payload=False, fetched_at=FETCHED_AT + timedelta(hours=4))
        self.assertIsNone(explicitly_off.sanitised_payload)


class RequestParameterTests(TestCase):
    def test_parameters_are_canonical(self):
        messy = {"resultsSize": 200, "prefectures": "54, 5", "isActive": True, "activities": ["62010000", "47191002", "62010000"]}
        tidy = {"activities": "47191002,62010000", "isActive": "true", "prefectures": ["5", "54"], "resultsSize": "200"}

        self.assertEqual(canonical_request_params(messy), {
            "activities": ["47191002", "62010000"], "isActive": "true", "prefectures": ["5", "54"], "resultsSize": "200",
        })
        self.assertEqual(list(canonical_request_params(messy)), ["activities", "isActive", "prefectures", "resultsSize"])
        self.assertEqual(canonical_request_params(messy), canonical_request_params(tidy))
        first, _ = record(page(full_item()), params=messy)
        second, created = record(page(full_item()), params=tidy)
        self.assertEqual(first.request_fingerprint, second.request_fingerprint)
        self.assertFalse(created)

    def test_secrets_and_unknown_parameters_are_dropped_and_personal_filters_redacted(self):
        params = {
            "api_key": SECRET, "apiKey": SECRET, "token": SECRET, "Authorization": f"Bearer {SECRET}", "unexpected": "x",
            "afm": "990004821", "name": "SENTINEL-COMPANY-NAME", "resultsOffset": 400, "resultsSize": 200,
            "resultsSortBy": "+arGemi", "statuses": "3", "arGemi": "118717203000",
        }
        self.assertEqual(canonical_request_params(params), {
            "afm": "[redacted]", "arGemi": "118717203000", "name": "[redacted]", "resultsOffset": "400",
            "resultsSize": "200", "resultsSortBy": "+arGemi", "statuses": ["3"],
        })
        source_record, _ = record(page(full_item()), params=params)
        stored = json.dumps(model_to_dict(source_record), ensure_ascii=False, default=str)
        for secret in (SECRET, "990004821", "SENTINEL-COMPANY-NAME"):
            self.assertNotIn(secret, stored)


class IdempotencyTests(TestCase):
    def test_a_retry_of_the_same_request_in_the_same_window_records_once(self):
        payload = page(full_item())
        first, first_created = record(payload, fetched_at=FETCHED_AT)
        second, second_created = record(copy.deepcopy(payload), fetched_at=FETCHED_AT + timedelta(minutes=20))

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(GemiSourceRecord.objects.count(), 1)

    def test_genuinely_separate_observations_are_recorded(self):
        payload = page(full_item())
        record(payload, fetched_at=FETCHED_AT)
        record(payload, fetched_at=FETCHED_AT + timedelta(hours=3))  # the next intraday slot
        changed = copy.deepcopy(payload)
        changed["searchResults"][0]["status"] = {"id": 17, "descr": "Διαγραφή"}
        record(changed, fetched_at=FETCHED_AT)
        record(payload, params={**SEARCH_PARAMS, "resultsOffset": 200}, fetched_at=FETCHED_AT)

        self.assertEqual(GemiSourceRecord.objects.count(), 4)
        self.assertEqual(GemiSourceRecord.objects.values("request_fingerprint").distinct().count(), 2)


def expired_record(key, *, expires_at, retention="standard"):
    return GemiSourceRecord.objects.create(
        family="company_search", response_family="company_search", endpoint="/companies", request_params={},
        request_fingerprint="0" * 64, observation_key=key, fetched_at=expires_at - timedelta(days=1), http_status=200,
        payload_hash="1" * 64, response_schema_version=1, record_format_version=1, retention_class=retention,
        expires_at=expires_at,
    )


class RetentionAndPurgeTests(TestCase):
    now = datetime(2026, 9, 15, 12, 0, tzinfo=dt_timezone.utc)

    @override_settings(GEMI_SOURCE_RECORD_RETENTION_DAYS={"short": 2, "standard": 10, "audit": 100})
    def test_each_class_uses_its_configured_days(self):
        search, _ = record(page(full_item()))
        reference, _ = record([REFERENCE_SAMPLES[ResponseFamily.LEGAL_TYPES]], family=ResponseFamily.LEGAL_TYPES, endpoint="/metadata/legalTypes", params={})
        self.assertEqual(search.expires_at, FETCHED_AT + timedelta(days=10))
        self.assertEqual(reference.expires_at, FETCHED_AT + timedelta(days=100))

    def test_expired_records_are_purged_unexpired_kept_and_the_boundary_is_inclusive(self):
        expired_record("a" * 64, expires_at=self.now - timedelta(days=3), retention="short")
        expired_record("b" * 64, expires_at=self.now, retention="standard")
        expired_record("c" * 64, expires_at=self.now + timedelta(microseconds=1), retention="standard")
        expired_record("d" * 64, expires_at=self.now + timedelta(days=200), retention="audit")

        self.assertEqual(purge_expired_source_records(now=self.now), 2)
        self.assertEqual(set(GemiSourceRecord.objects.values_list("observation_key", flat=True)), {"c" * 64, "d" * 64})

    def test_purge_is_idempotent_handles_zero_rows_and_batches(self):
        self.assertEqual(purge_expired_source_records(now=self.now), 0)
        for index in range(5):
            expired_record(f"{index:064d}", expires_at=self.now - timedelta(hours=index + 1))
        expired_record("e" * 64, expires_at=self.now + timedelta(days=1))

        with self.assertLogs("gemiapp.ingestion.source_records", level="INFO") as logs:
            self.assertEqual(purge_expired_source_records(now=self.now, batch_size=2), 5)
        self.assertEqual(purge_expired_source_records(now=self.now, batch_size=2), 0)
        self.assertEqual(GemiSourceRecord.objects.count(), 1)
        self.assertIn("5 expired record(s) deleted in 3 batch(es)", logs.output[0])
        with self.assertRaises(ValueError):
            purge_expired_source_records(now=self.now, batch_size=0)

    def test_purge_logs_counts_only_and_touches_no_other_table(self):
        source_record, _ = record(page(sentinel_item()))
        Company.objects.create(gemi_number="118717203000", name="ΕΤΑΙΡΕΙΑ", incorporation_date=TARGET)
        GemiSourceRecord.objects.filter(pk=source_record.pk).update(expires_at=self.now - timedelta(days=1))

        with self.assertLogs("gemiapp", level="DEBUG") as logs:
            self.assertEqual(purge_expired_source_records(now=self.now), 1)
        output = "\n".join(logs.output)
        self.assertNotIn(source_record.payload_hash, output)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, output)
        self.assertEqual(Company.objects.count(), 1)

    def test_dry_run_command_and_task(self):
        expired_record("a" * 64, expires_at=timezone.now() - timedelta(days=1))

        out = StringIO()
        call_command("purge_gemi_source_records", "--dry-run", stdout=out)
        self.assertIn("1", out.getvalue())
        self.assertEqual(GemiSourceRecord.objects.count(), 1)

        call_command("purge_gemi_source_records", "--batch-size", "10", stdout=StringIO())
        self.assertEqual(GemiSourceRecord.objects.count(), 0)
        with self.assertRaises(CommandError):
            call_command("purge_gemi_source_records", "--batch-size", "0", stdout=StringIO())

        expired_record("b" * 64, expires_at=timezone.now() - timedelta(days=1))
        self.assertEqual(purge_gemi_source_records_task(), 1)
        self.assertNotIn("gemiapp.tasks.purge_gemi_source_records_task", [entry["func"] for entry in SCHEDULES])


class FeatureFlagAndImporterTests(NoNetworkMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("member@example.com", "member@example.com", "StrongPass123")
        subscription = self.user.subscription
        subscription.tier, subscription.status = "pro", "active"
        subscription.save()
        CustomerRadar.objects.create(
            user=self.user, name="Αττική", prefectures=["ΑΤΤΙΚΗΣ"], monitor_from=timezone.now() - timedelta(days=60),
        )

    def pages(self):
        day, older = TARGET.isoformat(), (TARGET - timedelta(days=1)).isoformat()
        return routed({("true", "0"): page(sentinel_item("118717203000", day), full_item("118717204000", day), full_item("118717205000", older))})

    def outcome(self):
        from .services import import_for_date, send_digests

        run = import_for_date(TARGET)
        mail.outbox = []
        sent, skipped = send_digests(TARGET)
        return {
            "rows": snapshot_rows(),
            "matches": sorted(RadarMatch.objects.values_list("company__gemi_number", "matched_on", "match_reason")),
            "leads": sorted(UserCompanyLead.objects.values_list("company__gemi_number", "status")),
            "subscription": UserSubscription.objects.filter(user=self.user).values("tier", "status", "last_sent_company_id").get(),
            "run": (run.status, run.fetched_count, run.created_count),
            "digest": (sent, skipped, len(mail.outbox), [message.subject for message in mail.outbox]),
        }

    def reset(self):
        Company.objects.all().delete()
        ImportRun.objects.all().delete()
        DigestDelivery.objects.all().delete()

    def test_the_factory_attaches_a_recorder_only_when_enabled(self):
        self.assertIsNone(get_gemi_client()._source_recorder)
        with override_settings(GEMI_SOURCE_RECORDS_ENABLED=True):
            self.assertIsInstance(get_gemi_client()._source_recorder, SourceRecorder)
            self.assertFalse(get_gemi_client()._source_recorder.store_payload)  # payloads stay opt-in

    def test_enabled_records_provenance_without_changing_any_stored_or_customer_facing_result(self):
        disabled_client, _, _ = recording_client(self.pages(), recorder=None)
        with patch("gemiapp.services.get_gemi_client", return_value=disabled_client):
            disabled = self.outcome()
        self.assertEqual(GemiSourceRecord.objects.count(), 0)

        self.reset()
        enabled_client, _, _ = recording_client(self.pages(), recorder=SourceRecorder(store_payload=True))
        with patch("gemiapp.services.get_gemi_client", return_value=enabled_client):
            enabled = self.outcome()

        self.assertEqual(disabled, enabled)
        self.assertEqual(enabled["run"], ("success", 2, 2))
        self.assertEqual(len(enabled["matches"]), 2)
        # The active page is recorded; the inactive search is an empty 404 and is not.
        source_record = GemiSourceRecord.objects.get()
        self.assertEqual(source_record.request_params["isActive"], "true")
        self.assertEqual(source_record.result_count, 3)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, json.dumps(source_record.sanitised_payload, ensure_ascii=False))

    def test_a_retried_import_does_not_duplicate_source_records(self):
        from .services import import_for_date

        clock = FakeClock()
        for _ in range(2):
            client, _, _ = recording_client(self.pages(), recorder=SourceRecorder(), clock=clock)
            with patch("gemiapp.services.get_gemi_client", return_value=client):
                import_for_date(TARGET)
        self.assertEqual(GemiSourceRecord.objects.count(), 1)
        self.assertEqual(Company.objects.count(), 2)

    def test_a_source_record_failure_stops_the_import_before_any_company_is_written(self):
        from .services import import_for_date

        client, _, _ = recording_client(self.pages(), recorder=SourceRecorder())
        with patch("gemiapp.services.get_gemi_client", return_value=client), \
             patch.object(GemiSourceRecord.objects, "get_or_create", side_effect=DatabaseError("disk full")), \
             self.assertLogs("gemiapp", level="ERROR") as logs:
            with self.assertRaises(GemiSourceRecordError):
                import_for_date(TARGET)

        self.assertEqual(Company.objects.count(), 0)
        self.assertEqual(CompanyActivity.objects.count(), 0)
        self.assertEqual(ImportRun.objects.get().status, "failed")
        self.assertIn("could not be written (DatabaseError)", "\n".join(logs.output))
        for sentinel in SENTINELS + (SECRET,):
            self.assertNotIn(sentinel, "\n".join(logs.output))
