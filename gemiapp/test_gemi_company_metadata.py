"""Tests for Company reference ids and lifecycle metadata (A6, gemiapp.ingestion.company_metadata).

Fixtures only -- no GEMI call.
"""

import copy
from datetime import date, datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.signing import TimestampSigner
from django.db import DatabaseError, models
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import AddField
from django.test import TestCase
from django.utils import timezone

from .ingestion import DateQuality
from .ingestion import company_metadata as metadata_module
from .ingestion.company_metadata import A6_FIELDS, BACKFILL_FIELDS, backfill_company_metadata, derive_company_metadata, reconcile
from .models import (
    ActivityCode, Company, CompanyActivity, CustomerRadar, DigestDelivery, RadarMatch, UserCompanyLead, UserSubscription,
)
from .services import company_matches_radar
from .test_gemi_validation import CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL, full_item

IMPORTED_AT = datetime(2026, 9, 1, 6, 0, tzinfo=dt_timezone.utc)
UPDATED_AT = datetime(2026, 9, 14, 6, 0, tzinfo=dt_timezone.utc)
LEGACY_FIELDS = [
    f.attname for f in Company._meta.concrete_fields if f.name not in A6_FIELDS
]
FAX_SENTINEL = "SENTINEL-FAX-4821"


def create_company(gemi_number="118717203000", *, raw_data=None, incorporation_date=date(2026, 9, 14), imported_at=IMPORTED_AT, updated_at=UPDATED_AT, **fields):
    company = Company.objects.create(
        gemi_number=gemi_number, name=fields.pop("name", "ΕΤΑΙΡΕΙΑ ΙΚΕ"), incorporation_date=incorporation_date,
        raw_data=full_item(gemi_number) if raw_data is None else raw_data, **fields,
    )
    Company.objects.filter(pk=company.pk).update(imported_at=imported_at, updated_at=updated_at)
    return Company.objects.get(pk=company.pk)


def derive(raw_data, *, gemi_number="118717203000", admin_touched=False, updated_at=UPDATED_AT):
    return derive_company_metadata(
        gemi_number=gemi_number, raw_data=raw_data, imported_at=IMPORTED_AT, updated_at=updated_at, admin_touched=admin_touched,
    )


def a6_values(company):
    company.refresh_from_db()
    return {name: getattr(company, name) for name in A6_FIELDS}


class SchemaTests(TestCase):
    def test_every_new_field_is_nullable_not_editable_and_not_a_relation(self):
        types = {
            "status_source_id": models.CharField, "legal_type_source_id": models.CharField, "gemi_office_source_id": models.CharField,
            "prefecture_source_id": models.CharField, "municipality_source_id": models.CharField,
            "incorporation_date_quality": models.CharField, "first_seen_at": models.DateTimeField,
            "last_seen_at": models.DateTimeField, "last_synced_at": models.DateTimeField,
        }
        self.assertEqual(set(types), set(A6_FIELDS))
        for name, field_type in types.items():
            field = Company._meta.get_field(name)
            with self.subTest(field=name):
                self.assertIs(type(field), field_type)
                self.assertTrue(field.null)
                self.assertFalse(field.editable)
                self.assertFalse(field.is_relation)
                self.assertFalse(field.db_index)
                self.assertFalse(field.unique)
                self.assertIsNone(field.get_default())

    def test_no_new_index_and_quality_choices_are_the_a3_date_qualities(self):
        self.assertEqual([index.name for index in Company._meta.indexes], ["company_recent_order_idx"])
        self.assertEqual(
            {value for value, _ in Company._meta.get_field("incorporation_date_quality").choices},
            {quality.value for quality in DateQuality},
        )

    def test_the_migration_only_adds_the_nine_company_fields(self):
        migration = MigrationLoader(None, ignore_no_migrations=True).get_migration("gemiapp", "0034_company_gemi_metadata")
        self.assertEqual(migration.dependencies, [("gemiapp", "0033_gemi_reference_data")])
        self.assertTrue(all(isinstance(operation, AddField) and operation.model_name == "company" for operation in migration.operations))
        self.assertEqual(sorted(operation.name for operation in migration.operations), sorted(A6_FIELDS))
        self.assertTrue(all(operation.field.null for operation in migration.operations))


class ReferenceIdExtractionTests(TestCase):
    def test_ids_are_extracted_for_every_reference_object(self):
        values = derive(full_item()).values
        self.assertEqual(
            {name: values[name] for name in ("status_source_id", "legal_type_source_id", "gemi_office_source_id", "prefecture_source_id", "municipality_source_id")},
            {"status_source_id": "3", "legal_type_source_id": "19", "gemi_office_source_id": "3", "prefecture_source_id": "5", "municipality_source_id": "61190"},
        )

    def test_integer_and_digit_string_ids_are_the_same(self):
        self.assertEqual(derive(full_item(prefecture={"id": 5, "descr": "ΑΤΤΙΚΗΣ"})).values["prefecture_source_id"], "5")
        self.assertEqual(derive(full_item(prefecture={"id": "5", "descr": "ΑΤΤΙΚΗΣ"})).values["prefecture_source_id"], "5")

    def test_missing_null_and_description_only_objects_give_no_id(self):
        absent = full_item()
        del absent["legalType"]
        self.assertIsNone(derive(absent).values["legal_type_source_id"])
        self.assertIsNone(derive(full_item(legalType=None)).values["legal_type_source_id"])
        self.assertIsNone(derive(full_item(prefecture={"descr": "ΑΤΤΙΚΗΣ"})).values["prefecture_source_id"])
        self.assertIsNone(derive(full_item(municipality={"id": None, "descr": "ΚΗΦΙΣΙΑΣ"})).values["municipality_source_id"])

    def test_an_unknown_id_is_kept(self):
        self.assertEqual(derive(full_item(status={"id": 999999, "descr": "ΝΕΑ ΚΑΤΑΣΤΑΣΗ"})).values["status_source_id"], "999999")

    def test_unusable_ids_and_shapes_are_counted_as_malformed(self):
        derived = derive(full_item(prefecture={"id": "ΑΤΤ", "descr": "ΑΤΤΙΚΗΣ"}, status="Ενεργή", gemiOffice={"id": {"x": 1}}))
        self.assertIsNone(derived.values["prefecture_source_id"])
        self.assertIsNone(derived.values["status_source_id"])
        self.assertIsNone(derived.values["gemi_office_source_id"])
        self.assertEqual(set(derived.malformed_references), {"prefecture", "status", "gemiOffice"})
        self.assertEqual(derived.values["legal_type_source_id"], "19")

    def test_records_that_are_not_this_companys_gemi_record_give_nothing(self):
        for raw_data, state in (
            ({}, "missing"), ("legacy text", "malformed"), ([full_item()], "malformed"),
            ({"persons": [{"personName": PERSON_SENTINEL}]}, "not_gemi_record"), (full_item("999999999999"), "not_gemi_record"),
        ):
            with self.subTest(state=state):
                derived = derive(raw_data)
                self.assertEqual(derived.raw_state, state)
                self.assertEqual(set(derived.values.values()), {None})


class DateQualityTests(TestCase):
    def quality(self, value, updated_at=UPDATED_AT):
        record = full_item()
        if value is ...:
            del record["incorporationDate"]
        else:
            record["incorporationDate"] = value
        return derive(record, updated_at=updated_at).values["incorporation_date_quality"]

    def test_quality_uses_the_a3_semantics(self):
        self.assertEqual(self.quality("2026-09-14"), DateQuality.VALID.value)
        self.assertEqual(self.quality(...), DateQuality.MISSING.value)
        self.assertEqual(self.quality(None), DateQuality.MISSING.value)
        self.assertEqual(self.quality("14/09/2026"), DateQuality.INVALID.value)
        self.assertEqual(self.quality("9011-12-09"), DateQuality.OUT_OF_RANGE.value)
        self.assertEqual(self.quality("1821-01-01"), DateQuality.OUT_OF_RANGE.value)

    def test_it_is_evaluated_against_the_day_the_record_was_last_written(self):
        self.assertEqual(self.quality("2026-09-15"), DateQuality.VALID.value)  # one day of tolerance
        self.assertEqual(self.quality("2026-09-16"), DateQuality.OUT_OF_RANGE.value)
        self.assertEqual(self.quality("2026-09-16", updated_at=UPDATED_AT + timedelta(days=2)), DateQuality.VALID.value)

    def test_the_legacy_stored_date_is_never_rewritten(self):
        clamped = create_company(raw_data=full_item(day="9011-12-09"), incorporation_date=date(2026, 8, 19))
        backfill_company_metadata()
        clamped.refresh_from_db()
        self.assertEqual(clamped.incorporation_date, date(2026, 8, 19))
        self.assertEqual(clamped.incorporation_date_quality, "out_of_range")


class LifecycleTests(TestCase):
    def test_gemi_records_take_first_and_last_seen_from_imported_and_updated_at(self):
        company = create_company()
        backfill_company_metadata()
        values = a6_values(company)
        self.assertEqual((values["first_seen_at"], values["last_seen_at"]), (IMPORTED_AT, UPDATED_AT))
        self.assertIsNone(values["last_synced_at"])

    def test_non_gemi_admin_touched_and_seed_rows_get_no_lifecycle(self):
        seed = create_company("186420501000", raw_data={})
        marketing = create_company("999000101000", raw_data={"persons": [{"personName": "ΔΟΚΙΜΗ"}]})
        edited = create_company("118717204000")
        created_in_admin = create_company("118717205000")
        deleted_log = create_company("118717206000")
        admin_user = User.objects.create_superuser("admin@example.com", "admin@example.com", "StrongPass123")
        content_type = ContentType.objects.get_for_model(Company)
        for company, flag in ((edited, CHANGE), (created_in_admin, ADDITION), (deleted_log, DELETION)):
            LogEntry.objects.create(user=admin_user, content_type=content_type, object_id=str(company.pk), object_repr="x", action_flag=flag, change_message="[]")

        report = backfill_company_metadata()

        for company in (seed, marketing, edited, created_in_admin):
            values = a6_values(company)
            self.assertIsNone(values["first_seen_at"], company.gemi_number)
            self.assertIsNone(values["last_seen_at"], company.gemi_number)
        self.assertEqual(a6_values(edited)["status_source_id"], "3")  # ids still come from the record
        self.assertEqual(a6_values(deleted_log)["first_seen_at"], IMPORTED_AT)  # a deletion log is not an edit
        self.assertEqual(report.admin_touched, 2)

    def test_the_backfill_never_uses_the_current_time_and_never_touches_existing_timestamps(self):
        company = create_company()
        future = datetime(2099, 1, 1, tzinfo=dt_timezone.utc)
        with patch.object(metadata_module.timezone, "now", return_value=future):
            backfill_company_metadata()
        company.refresh_from_db()
        self.assertEqual((company.imported_at, company.updated_at), (IMPORTED_AT, UPDATED_AT))
        self.assertNotIn(future, a6_values(company).values())

    def test_reconciliation_never_erases_or_regresses(self):
        current = {name: None for name in BACKFILL_FIELDS}
        current.update(status_source_id="3", first_seen_at=IMPORTED_AT - timedelta(days=5), last_seen_at=UPDATED_AT + timedelta(days=5))
        derived = derive(full_item(status=None)).values

        changes = reconcile(current, derived)

        self.assertNotIn("status_source_id", changes)
        self.assertNotIn("first_seen_at", changes)
        self.assertNotIn("last_seen_at", changes)
        self.assertEqual(changes["prefecture_source_id"], "5")

        company = create_company()
        Company.objects.filter(pk=company.pk).update(last_synced_at=UPDATED_AT)
        backfill_company_metadata()
        self.assertEqual(a6_values(company)["last_synced_at"], UPDATED_AT)


class BackfillRunTests(TestCase):
    def test_bad_legacy_rows_do_not_stop_the_batch(self):
        good = create_company("118717203000")
        create_company("118717204000", raw_data="legacy text")
        create_company("118717205000", raw_data=[1, 2])
        partial = create_company("118717206000", raw_data={"arGemi": "118717206000", "status": "Ενεργή", "prefecture": {"id": {"x": 1}}})

        report = backfill_company_metadata(batch_size=2)

        self.assertEqual(report.inspected, 4)
        self.assertEqual(report.raw_states["malformed"], 2)
        self.assertEqual(dict(report.malformed_references), {"status": 1, "prefecture": 1})
        self.assertEqual(a6_values(good)["status_source_id"], "3")
        self.assertEqual(a6_values(partial)["incorporation_date_quality"], "missing")
        self.assertEqual(a6_values(partial)["first_seen_at"], IMPORTED_AT)

    def test_a_row_level_derivation_error_is_counted_and_the_rest_continue(self):
        broken = create_company("118717203000")
        fine = create_company("118717204000")
        original = metadata_module.derive_company_metadata

        def flaky(**kwargs):
            if kwargs["gemi_number"] == broken.gemi_number:
                raise TypeError("unexpected legacy shape")
            return original(**kwargs)

        with patch.object(metadata_module, "derive_company_metadata", side_effect=flaky), \
             self.assertLogs("gemiapp.ingestion.company_metadata", level="WARNING") as logs:
            report = backfill_company_metadata()
        self.assertEqual(report.row_errors, 1)
        self.assertIsNone(a6_values(broken)["status_source_id"])
        self.assertEqual(a6_values(fine)["status_source_id"], "3")
        self.assertIn(f"Company {broken.pk}", "\n".join(logs.output))

    def test_operational_errors_are_not_swallowed(self):
        create_company()
        with patch.object(Company.objects, "bulk_update", side_effect=DatabaseError("disk full")):
            with self.assertRaises(DatabaseError):
                backfill_company_metadata()

    def test_a_second_run_changes_nothing(self):
        companies = [create_company(f"11871720{index}000") for index in range(3)]
        first = backfill_company_metadata()
        after_first = [a6_values(company) for company in companies]
        second = backfill_company_metadata()

        self.assertEqual((first.changed, second.changed), (3, 0))
        self.assertEqual([a6_values(company) for company in companies], after_first)

    def test_batches_boundaries_and_resume(self):
        companies = [create_company(f"1187172{index:02d}000") for index in range(7)]
        report = backfill_company_metadata(batch_size=3)
        self.assertEqual((report.inspected, report.changed, report.batches, report.last_id), (7, 7, 3, companies[-1].pk))

        Company.objects.update(status_source_id=None)
        resumed = backfill_company_metadata(batch_size=3, start_id=companies[3].pk)
        self.assertEqual(resumed.inspected, 3)
        self.assertEqual([a6_values(company)["status_source_id"] for company in companies], [None] * 4 + ["3"] * 3)
        with self.assertRaises(ValueError):
            backfill_company_metadata(batch_size=0)

    def test_a_dry_run_reports_the_changes_and_writes_nothing(self):
        companies = [create_company(f"11871720{index}000") for index in range(3)]
        create_company("186420501000", raw_data={})
        preview = backfill_company_metadata(dry_run=True)

        self.assertTrue(preview.dry_run)
        self.assertEqual((preview.inspected, preview.changed), (4, 3))
        self.assertEqual(dict(preview.date_quality), {"valid": 3})
        self.assertEqual(preview.reference_ids_found["status_source_id"], 3)
        for company in companies:
            self.assertEqual(set(a6_values(company).values()), {None})
        self.assertEqual(backfill_company_metadata().changed, preview.changed)

    def test_command_output_and_validation(self):
        create_company()
        out = StringIO()
        call_command("backfill_gemi_company_metadata", "--dry-run", "--batch-size", "10", stdout=out)
        self.assertIn("[dry-run] inspected=1 would change=1", out.getvalue())
        with self.assertRaises(CommandError):
            call_command("backfill_gemi_company_metadata", "--batch-size", "0", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("backfill_gemi_company_metadata", "--start-id", "-1", stdout=StringIO())


class PrivacyTests(TestCase):
    def test_person_and_contact_data_never_reach_fields_logs_or_output(self):
        record = full_item(fax=FAX_SENTINEL)
        record["persons"].append({"personName": "SENTINEL-NESTED-4821", "contact": {"email": "sentinel.person@example.invalid"}})
        company = create_company(raw_data=record)
        create_company("118717204000", raw_data={"arGemi": "118717204000", "persons": [{"personName": PERSON_SENTINEL}], "status": PERSON_SENTINEL})

        out = StringIO()
        with self.assertLogs("gemiapp", level="DEBUG") as logs:
            call_command("backfill_gemi_company_metadata", stdout=out)
            logger = metadata_module.logger
            logger.debug("probe")  # keep assertLogs satisfied even if nothing else is logged
        rendered = "\n".join([str(a6_values(company)), out.getvalue(), "\n".join(logs.output)])
        for sentinel in (PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, FAX_SENTINEL, "SENTINEL-NESTED-4821", "sentinel.person@example.invalid", "ΔΟΚΙΜΑΣΤΙΚΗ"):
            self.assertNotIn(sentinel, rendered)


class CurrentBehaviourTests(TestCase):
    target = date(2026, 9, 14)

    def test_backfill_leaves_legacy_company_data_matching_leads_digests_and_billing_unchanged(self):
        from .services import import_for_date, send_digests

        user = User.objects.create_user("member@example.com", "member@example.com", "StrongPass123")
        subscription = user.subscription
        subscription.tier, subscription.status = "pro", "active"
        subscription.save()
        radar = CustomerRadar.objects.create(user=user, name="Αττική", prefectures=["ΑΤΤΙΚΗΣ"], monitor_from=timezone.now() - timedelta(days=60))
        inactive_radar = CustomerRadar.objects.create(user=user, name="Μόνο ενεργές", prefectures=["ΑΤΤΙΚΗΣ"], only_active=True, is_active=False, monitor_from=timezone.now() - timedelta(days=60))
        items = [full_item("118717203000", self.target.isoformat()), full_item("118717204000", "9011-12-09", status={"id": 17, "descr": "Διαγραφή"})]
        with patch("gemiapp.services.fetch_companies", return_value=copy.deepcopy(items)):
            import_for_date(self.target)

        def state():
            mail.outbox = []
            DigestDelivery.objects.all().delete()
            sent = send_digests(self.target)
            companies = list(Company.objects.prefetch_related("activity_records").order_by("pk"))
            return {
                "legacy_company_fields": list(Company.objects.order_by("pk").values_list(*LEGACY_FIELDS)),
                "activities": list(CompanyActivity.objects.order_by("pk").values()),
                "matches": list(RadarMatch.objects.order_by("pk").values()),
                "leads": list(UserCompanyLead.objects.order_by("pk").values()),
                "subscriptions": list(UserSubscription.objects.order_by("pk").values()),
                "radars": list(CustomerRadar.objects.order_by("pk").values()),
                "activity_codes": ActivityCode.objects.count(),
                "is_active": list(Company.objects.order_by("pk").values_list("gemi_number", "is_active")),
                "radar_predicate": [(r.pk, c.pk, company_matches_radar(c, r)[0]) for r in (radar, inactive_radar) for c in companies],
                "digest": (sent, [(m.subject, m.body) for m in mail.outbox]),
            }

        # Digest links carry TimestampSigner tokens (unsubscribe, CSV export) that embed the signing second, so two
        # renders straddling a second boundary differ although nothing changed. Pin only the signer's clock: the
        # tokens are still produced and compared in full.
        with patch("django.core.signing.TimestampSigner.timestamp", return_value=TimestampSigner().timestamp()):
            before = state()
            backfill_company_metadata()
            after = state()

        self.assertEqual(before, after)
        deleted = Company.objects.get(gemi_number="118717204000")
        self.assertEqual((deleted.status_source_id, deleted.is_active), ("17", True))  # the is_active bug is left as it is
        self.assertEqual(deleted.incorporation_date_quality, "out_of_range")
