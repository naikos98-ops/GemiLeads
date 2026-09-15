"""Tests for canonical CompanyActivity metadata, safe upsert and matching parity (A7,
gemiapp.ingestion.activities).

Fixtures only -- no GEMI call.
"""

import copy
from datetime import date, datetime, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, IntegrityError, connection, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import AddConstraint, AddField, RemoveConstraint, RunPython
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .ingestion import activities as activities_module
from .ingestion.activities import (
    CANONICAL_FIELDS,
    backfill_company_activities,
    canonicalize_activities,
    matching_parity,
    normalize_activity_type,
)
from .kad import display_kad_code, normalize_kad_code, normalize_kad_search
from .models import (
    ActivityCode, Company, CompanyActivity, CustomerRadar, DigestDelivery, ImportRun, RadarMatch, UserCompanyLead,
)
from .services import (
    company_matches_radar, filter_companies_for_radar, import_for_date, match_imported_companies, send_digests,
    sync_company_activities,
)
from .test_gemi_validation import CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL, full_item

AS_OF = date(2026, 9, 15)
UPDATED_AT = datetime(2026, 9, 15, 6, 0, tzinfo=dt_timezone.utc)  # 2026-09-15 in Europe/Athens
TARGET = date(2026, 9, 14)


def entry(code="62010000", type_="Κύρια", version="kad_2026", dt_from="2026-09-14", dt_to=None, descr=None):
    return {
        "activity": {"id": code, "descr": f"ΔΡΑΣΤΗΡΙΟΤΗΤΑ {code}" if descr is None else descr, "kadVersion": version},
        "type": type_, "dtFrom": dt_from, "dtTo": dt_to,
    }


# Shapes observed in the development data: an ended activity, the same code and type in both KAD versions,
# a future end, two periods of one code, the same code with another type.
MIXED = [
    entry("62010000", "Κύρια"),
    entry("47110000", "Δευτερεύουσα", dt_from="2019-01-01", dt_to="2026-02-01"),
    entry("47110000", "Δευτερεύουσα", version="kad_2008", dt_from="2010-01-01", dt_to="2026-02-01"),
    entry("43210000", "Βοηθητική", dt_to="2027-06-30"),
    entry("56100000", "Δευτερεύουσα", dt_from="2015-01-01", dt_to="2018-01-01"),
    entry("56100000", "Δευτερεύουσα", dt_from="2020-01-01"),
    entry("62010000", "Δευτερεύουσα", version="kad_2008", dt_from="2005-01-01", dt_to="2026-02-01", descr="ΠΑΛΙΑ ΠΕΡΙΓΡΑΦΗ"),
]


def legacy_rows(source_activities):
    """What the pre-A7 importer stored, verbatim: company_defaults() then sync_company_activities()."""
    activities = []
    for item in source_activities or []:
        activity = item.get("activity") or {}
        activities.append({"code": activity.get("id", ""), "description": activity.get("descr", ""), "type": item.get("type", "")})
    rows, seen = [], set()
    for activity in activities:
        code = normalize_kad_code(activity.get("code"))
        activity_type = str(activity.get("type") or "")
        if not code or (code, activity_type) in seen:
            continue
        seen.add((code, activity_type))
        rows.append((code, activity_type, str(activity.get("description") or "")))
    return rows


def legacy_sync(company, source_activities, *, as_of=None):
    """The pre-A7 persistence: delete every row and recreate the legacy list."""
    CompanyActivity.objects.filter(company=company).delete()
    CompanyActivity.objects.bulk_create([
        CompanyActivity(company=company, code=code, activity_type=activity_type, description=description)
        for code, activity_type, description in legacy_rows(source_activities)
    ])


def pre_a7_company_matches_radar(company, radar):
    """gemiapp.services.company_matches_radar before A7, verbatim."""
    if radar.only_active and not company.is_active:
        return False, {}
    if radar.name_query and normalize_kad_search(radar.name_query) not in normalize_kad_search(company.name):
        return False, {}
    if radar.prefectures and company.prefecture not in radar.prefectures:
        return False, {}
    if radar.legal_types and company.legal_type not in radar.legal_types:
        return False, {}
    wanted_codes = {item.normalized_code for item in radar.activity_codes.all()}
    company_codes = {item.code for item in company.activity_records.all()}
    matched_codes = sorted(wanted_codes & company_codes)
    if wanted_codes and not matched_codes:
        return False, {}
    return True, {
        "activity_codes": matched_codes,
        "prefecture": company.prefecture if radar.prefectures else "",
        "legal_type": company.legal_type if radar.legal_types else "",
        "name_query": radar.name_query if radar.name_query else "",
    }


def listed(company):
    return sorted(CompanyActivity.objects.filter(company=company, legacy_listed=True).values_list("code", "activity_type", "description"))


def make_company(gemi_number="118717203000", activities=(), *, updated_at=UPDATED_AT, raw_data=None, **fields):
    raw = full_item(gemi_number, TARGET.isoformat(), activities=list(activities) if isinstance(activities, tuple) else activities)
    company = Company.objects.create(
        gemi_number=gemi_number, name=fields.pop("name", "ΕΤΑΙΡΕΙΑ ΙΚΕ"), incorporation_date=TARGET,
        prefecture="ΑΤΤΙΚΗΣ", legal_type="ΙΚΕ", raw_data=raw if raw_data is None else raw_data, **fields,
    )
    Company.objects.filter(pk=company.pk).update(updated_at=updated_at)
    return Company.objects.get(pk=company.pk)


def kad(code):
    return ActivityCode.objects.get_or_create(
        normalized_code=code, defaults={"code": display_kad_code(code), "description": "ΔΟΚΙΜΗ", "search_text": code},
    )[0]


def radar_for(user, name, codes=(), **fields):
    radar = CustomerRadar.objects.create(
        user=user, name=name, monitor_from=timezone.make_aware(datetime(2026, 9, 1)), **fields,
    )
    radar.activity_codes.add(*[kad(code) for code in codes])
    return radar


def entitled_user(email="member@example.com"):
    user = User.objects.create_user(email, email, "StrongPass123")
    subscription = user.subscription
    subscription.tier, subscription.status = "pro", "active"
    subscription.save()
    return user


class SchemaTests(TestCase):
    def test_canonical_fields_are_nullable_not_editable_and_have_no_default(self):
        types = {
            "activity_type_normalized": models.CharField, "kad_version": models.CharField, "date_from": models.DateField,
            "date_from_quality": models.CharField, "date_to": models.DateField, "date_to_quality": models.CharField,
            "is_current": models.BooleanField, "current_as_of": models.DateField, "source_key": models.CharField,
            "in_latest_source": models.BooleanField,
        }
        self.assertEqual(set(types), set(CANONICAL_FIELDS))
        for name, field_type in types.items():
            field = CompanyActivity._meta.get_field(name)
            with self.subTest(field=name):
                self.assertIs(type(field), field_type)
                self.assertTrue(field.null)
                self.assertFalse(field.editable)
                self.assertIsNone(field.get_default())

    def test_legacy_listed_defaults_to_true_because_every_pre_a7_row_is_a_legacy_row(self):
        field = CompanyActivity._meta.get_field("legacy_listed")
        self.assertFalse(field.null)
        self.assertIs(field.get_default(), True)

    def test_no_relation_other_than_the_company(self):
        related = {field.related_model for field in CompanyActivity._meta.get_fields() if field.is_relation}
        self.assertEqual(related, {Company})

    def test_partial_unique_constraints(self):
        company = make_company()
        CompanyActivity.objects.create(company=company, code="62010000", activity_type="Κύρια")
        with self.assertRaises(IntegrityError), transaction.atomic():
            CompanyActivity.objects.create(company=company, code="62010000", activity_type="Κύρια")
        CompanyActivity.objects.create(company=company, code="62010000", activity_type="Κύρια", legacy_listed=False, source_key="a" * 64)
        CompanyActivity.objects.create(company=company, code="62010000", activity_type="Κύρια", legacy_listed=False, source_key="b" * 64)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CompanyActivity.objects.create(company=company, code="47110000", legacy_listed=False, source_key="a" * 64)
        self.assertEqual(CompanyActivity.objects.filter(company=company).count(), 3)

    def test_the_migration_is_additive_apart_from_relaxing_the_legacy_constraint(self):
        migration = MigrationLoader(None, ignore_no_migrations=True).get_migration("gemiapp", "0035_companyactivity_canonical_metadata")
        self.assertEqual(migration.dependencies, [("gemiapp", "0034_company_gemi_metadata")])
        self.assertTrue(all(isinstance(op, (AddField, RemoveConstraint, AddConstraint, RunPython)) for op in migration.operations))
        cleanup = [op for op in migration.operations if isinstance(op, RunPython)]
        self.assertEqual(len(cleanup), 1)
        self.assertIs(cleanup[0].code, RunPython.noop)  # no data migration forward
        self.assertEqual(cleanup[0].reverse_code.__name__, "remove_canonical_only_rows")
        names = [type(op).__name__ for op in migration.operations]
        # Reverse order: constraints dropped, then the cleanup, then the columns and the old constraint.
        self.assertLess(names.index("RunPython"), names.index("AddConstraint"))
        self.assertGreater(names.index("RunPython"), max(i for i, name in enumerate(names) if name == "AddField"))
        self.assertEqual([op.name for op in migration.operations if isinstance(op, RemoveConstraint)], ["unique_company_activity"])
        self.assertEqual(
            sorted(op.name for op in migration.operations if isinstance(op, AddField)),
            sorted((*CANONICAL_FIELDS, "legacy_listed")),
        )
        self.assertEqual(
            sorted(op.constraint.name for op in migration.operations if isinstance(op, AddConstraint)),
            ["unique_company_activity_listed", "unique_company_activity_source_key"],
        )
        self.assertTrue(all(op.model_name == "companyactivity" for op in migration.operations if not isinstance(op, RunPython)))


class MigrationRollbackTests(TransactionTestCase):
    """Reversing 0035 is self-contained: the migration itself removes the canonical-only rows, every
    legacy-visible row survives unchanged, the pre-A7 unique constraint returns, and reapplying plus the
    backfill reproduces the same canonical state."""

    BEFORE = [("gemiapp", "0034_company_gemi_metadata")]
    AFTER = [("gemiapp", "0035_companyactivity_canonical_metadata")]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return MigrationExecutor(connection).loader.project_state(targets).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def raw_rows(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, company_id, code, description, activity_type FROM gemiapp_companyactivity ORDER BY id")
            return cursor.fetchall()

    def columns(self):
        with connection.cursor() as cursor:
            return {column.name for column in connection.introspection.get_table_description(cursor, "gemiapp_companyactivity")}

    def canonical_state(self):
        return sorted(
            CompanyActivity.objects.exclude(in_latest_source=False)
            .values_list("company_id", "code", "activity_type", "legacy_listed", *CANONICAL_FIELDS),
            key=repr,
        )

    def test_forward_backfill_reverse_and_reapply(self):
        old = self.migrate(self.BEFORE)
        OldCompany = old.get_model("gemiapp", "Company")
        OldActivity = old.get_model("gemiapp", "CompanyActivity")
        stable = OldCompany.objects.create(gemi_number="118717203000", name="Α", incorporation_date=TARGET, raw_data=full_item("118717203000", TARGET.isoformat(), activities=copy.deepcopy(MIXED)))
        changed = OldCompany.objects.create(gemi_number="118717204000", name="Β", incorporation_date=TARGET, raw_data=full_item("118717204000", TARGET.isoformat(), activities=copy.deepcopy(MIXED)))
        demo = OldCompany.objects.create(gemi_number="999000101000", name="DEMO", incorporation_date=TARGET, raw_data={})
        for company in (stable, changed):
            for code, activity_type, description in legacy_rows(MIXED):
                OldActivity.objects.create(company_id=company.pk, code=code, activity_type=activity_type, description=description)
        OldActivity.objects.create(company_id=demo.pk, code="47110000", activity_type="Κύρια", description="DEMO")
        OldCompany.objects.update(updated_at=UPDATED_AT)
        baseline = self.raw_rows()

        # Forward: no row touched.
        self.migrate(self.AFTER)
        self.assertEqual(self.raw_rows(), baseline)
        self.assertIn("legacy_listed", self.columns())

        # Backfill, then a re-import in which one activity is no longer published.
        backfill_company_activities()
        reduced = [item for item in MIXED if item["activity"]["id"] != "43210000"]
        Company.objects.filter(pk=changed.pk).update(raw_data=full_item("118717204000", TARGET.isoformat(), activities=copy.deepcopy(reduced)))
        with patch.object(activities_module, "_ensure_catalogue_entries"):
            sync_company_activities(Company.objects.get(pk=changed.pk), copy.deepcopy(reduced), as_of=AS_OF)
        self.assertEqual(CompanyActivity.objects.filter(legacy_listed=False).count(), 5)  # 2 + 2 derived, 1 no longer published
        listed_before = sorted(CompanyActivity.objects.filter(legacy_listed=True).values_list("pk", "company_id", "code", "description", "activity_type"))
        canonical_before = self.canonical_state()

        # Reverse with canonical-only rows present: no manual cleanup.
        old = self.migrate(self.BEFORE)
        self.assertNotIn("legacy_listed", self.columns())
        removed_by_legacy_importer = [row for row in baseline if row[1] == changed.pk and row[2] == "43210000"]
        self.assertEqual(len(removed_by_legacy_importer), 1)
        self.assertEqual(self.raw_rows(), [row for row in baseline if row not in removed_by_legacy_importer])
        self.assertEqual(self.raw_rows(), [tuple(row) for row in listed_before])
        with self.assertRaises(IntegrityError), transaction.atomic():
            old.get_model("gemiapp", "CompanyActivity").objects.create(company_id=stable.pk, code="62010000", activity_type="Κύρια")

        # Reapply and backfill: the same canonical state, the same legacy-visible rows and keys.
        self.migrate(self.AFTER)
        backfill_company_activities()
        self.assertEqual(self.canonical_state(), canonical_before)
        self.assertEqual(sorted(CompanyActivity.objects.filter(legacy_listed=True).values_list("pk", "company_id", "code", "description", "activity_type")), listed_before)


class DerivationTests(TestCase):
    def one(self, **kwargs):
        return canonicalize_activities([entry(**kwargs)], as_of=AS_OF).activities[0]

    def test_period_dates_use_a3_quality(self):
        self.assertEqual((self.one().date_from.value, self.one().date_from.quality.value), (date(2026, 9, 14), "valid"))
        missing = self.one(dt_from=None)
        self.assertEqual((missing.date_from.value, missing.date_from.quality.value), (None, "missing"))
        malformed = self.one(dt_from="01/02/2020")
        self.assertEqual((malformed.date_from.value, malformed.date_from.quality.value), (None, "invalid"))

    def test_currentness(self):
        cases = {
            None: (True, None, "missing"),
            "2026-09-15": (False, date(2026, 9, 15), "valid"),  # ends on as_of -> ended
            "2026-02-01": (False, date(2026, 2, 1), "valid"),
            "2027-06-30": (True, date(2027, 6, 30), "valid"),
            "31/12/2026": (None, None, "invalid"),
            "9999-12-31": (True, None, "out_of_range"),  # observed in the source; not rewritten
        }
        for dt_to, expected in cases.items():
            with self.subTest(dt_to=dt_to):
                activity = self.one(dt_to=dt_to)
                self.assertEqual((activity.is_current, activity.date_to.value, activity.date_to.quality.value), expected)

    def test_kad_versions_are_preserved_and_never_merged(self):
        self.assertEqual(self.one(version="kad_2008").kad_version, "kad_2008")
        self.assertEqual(self.one(version="kad_2026").kad_version, "kad_2026")
        self.assertIsNone(self.one(version=None).kad_version)
        both = canonicalize_activities([entry(version="kad_2026"), entry(version="kad_2008")], as_of=AS_OF).activities
        self.assertEqual(len(both), 2)
        self.assertNotEqual(both[0].source_key, both[1].source_key)
        self.assertEqual([activity.legacy_listed for activity in both], [True, False])

    def test_activity_types(self):
        for value, expected in {
            "Κύρια": "primary", "ΚΥΡΙΑ": "primary", " κυρια ": "primary", "Δευτερεύουσα": "secondary",
            "Βοηθητική": "auxiliary", "Λοιπή": "other", "Νέος τύπος": "unknown", "": None, None: None,
        }.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_activity_type(value), expected)
        unknown = self.one(type_="Νέος τύπος")
        self.assertEqual((unknown.activity_type, unknown.activity_type_normalized), ("Νέος τύπος", "unknown"))

    def test_entries_without_code_and_repeated_identities_are_counted(self):
        result = canonicalize_activities(
            [entry(), entry(descr="ΑΛΛΗ"), {"activity": {"descr": "x"}}, "junk", {"activity": {"id": "abc"}}, {"type": "Κύρια"}],
            as_of=AS_OF,
        )
        self.assertEqual((len(result.activities), result.entries, result.without_code, result.collapsed), (1, 6, 4, 1))
        self.assertTrue(canonicalize_activities({"not": "a list"}, as_of=AS_OF).malformed_list)
        self.assertEqual(canonicalize_activities(None, as_of=AS_OF).activities, ())

    def test_identity_ignores_description_and_end_but_not_start_type_or_version(self):
        base = self.one().source_key
        self.assertEqual(self.one(descr="ΑΛΛΗ", dt_to="2026-01-01").source_key, base)
        for change in ({"dt_from": "2020-01-01"}, {"type_": "Δευτερεύουσα"}, {"version": "kad_2008"}, {"code": "62010001"}):
            self.assertNotEqual(self.one(**change).source_key, base, change)

    def test_as_of_must_be_a_date(self):
        with self.assertRaises(TypeError):
            canonicalize_activities([entry()], as_of=UPDATED_AT)


class UpsertTests(TestCase):
    def setUp(self):
        self.company = make_company(activities=MIXED)

    def snapshot(self):
        return list(CompanyActivity.objects.filter(company=self.company).order_by("pk").values())

    def test_first_sync_creates_one_row_per_identity_and_exactly_the_legacy_list(self):
        counts = sync_company_activities(self.company, MIXED, as_of=AS_OF)
        self.assertEqual((counts.created, counts.updated), (7, 0))
        self.assertEqual(CompanyActivity.objects.filter(company=self.company).count(), 7)
        self.assertEqual(listed(self.company), sorted(legacy_rows(MIXED)))
        row = CompanyActivity.objects.get(company=self.company, code="47110000", kad_version="kad_2008")
        self.assertEqual(
            (row.is_current, row.date_to, row.date_to_quality, row.activity_type_normalized, row.legacy_listed, row.current_as_of),
            (False, date(2026, 2, 1), "valid", "secondary", False, AS_OF),
        )

    def test_an_identical_second_sync_writes_nothing(self):
        sync_company_activities(self.company, MIXED, as_of=AS_OF)
        before = self.snapshot()
        counts = sync_company_activities(self.company, copy.deepcopy(MIXED), as_of=AS_OF)
        self.assertEqual((counts.created, counts.updated, counts.unchanged), (0, 0, 7))
        self.assertEqual(self.snapshot(), before)

    def test_metadata_changes_update_the_row_in_place(self):
        sync_company_activities(self.company, MIXED, as_of=AS_OF)
        row = CompanyActivity.objects.get(company=self.company, code="62010000", activity_type="Κύρια")
        changed = copy.deepcopy(MIXED)
        changed[0].update(dtTo="2026-09-10")
        changed[0]["activity"]["descr"] = "ΝΕΑ ΠΕΡΙΓΡΑΦΗ"
        counts = sync_company_activities(self.company, changed, as_of=AS_OF)
        self.assertEqual((counts.created, counts.updated), (0, 1))
        row.refresh_from_db()
        self.assertEqual((row.is_current, row.date_to, row.description, row.legacy_listed), (False, date(2026, 9, 10), "ΝΕΑ ΠΕΡΙΓΡΑΦΗ", True))
        self.assertEqual(CompanyActivity.objects.filter(company=self.company).count(), 7)

    def test_a_new_activity_creates_exactly_one_row(self):
        sync_company_activities(self.company, MIXED, as_of=AS_OF)
        pks = set(CompanyActivity.objects.values_list("pk", flat=True))
        counts = sync_company_activities(self.company, [*MIXED, entry("11110000")], as_of=AS_OF)
        self.assertEqual((counts.created, counts.updated), (1, 0))
        self.assertEqual(set(CompanyActivity.objects.exclude(pk__in=pks).values_list("code", flat=True)), {"11110000"})

    def test_a_source_duplicate_does_not_multiply_rows(self):
        for _ in range(2):
            sync_company_activities(self.company, [*MIXED, copy.deepcopy(MIXED[0]), copy.deepcopy(MIXED[0])], as_of=AS_OF)
        self.assertEqual(CompanyActivity.objects.filter(company=self.company).count(), 7)

    def test_an_activity_that_disappears_is_preserved_outside_the_legacy_list(self):
        sync_company_activities(self.company, MIXED, as_of=AS_OF)
        user = entitled_user()
        radar = radar_for(user, "Εγκαταστάσεις", ["43210000"])
        self.assertTrue(company_matches_radar(self.company, radar)[0])

        without = [item for item in MIXED if item["activity"]["id"] != "43210000"]
        counts = sync_company_activities(self.company, without, as_of=AS_OF)
        row = CompanyActivity.objects.get(company=self.company, code="43210000")
        self.assertEqual((row.in_latest_source, row.legacy_listed, row.is_current), (False, False, True))
        self.assertEqual(counts.preserved_absent, 1)
        self.assertEqual(listed(self.company), sorted(legacy_rows(without)))
        company = Company.objects.prefetch_related("activity_records").get(pk=self.company.pk)
        self.assertFalse(company_matches_radar(company, radar)[0])  # as when the legacy importer deleted it

    def test_legacy_rows_are_adopted_and_keep_their_primary_keys(self):
        legacy_sync(self.company, MIXED)
        before = dict(((row.code, row.activity_type), row.pk) for row in CompanyActivity.objects.filter(company=self.company))
        counts = sync_company_activities(self.company, MIXED, as_of=AS_OF)
        self.assertEqual((counts.adopted_legacy_rows, counts.created), (5, 2))
        for (code, activity_type), pk in before.items():
            row = CompanyActivity.objects.get(pk=pk)
            self.assertEqual((row.code, row.activity_type, row.legacy_listed), (code, activity_type, True))
            self.assertIsNotNone(row.source_key)

    def test_any_sequence_of_payloads_keeps_the_legacy_list_exact_and_never_deletes(self):
        type_change = copy.deepcopy(MIXED)
        type_change[0]["type"] = "Δευτερεύουσα"
        sequence = [MIXED, list(reversed(MIXED)), type_change, MIXED[2:], [], None, MIXED, list(reversed(MIXED))]
        previous = 0
        for payload in sequence:
            sync_company_activities(self.company, copy.deepcopy(payload), as_of=AS_OF)
            with self.subTest(payload=len(payload or [])):
                self.assertEqual(listed(self.company), sorted(legacy_rows(payload)))
                count = CompanyActivity.objects.filter(company=self.company).count()
                self.assertGreaterEqual(count, previous)
                previous = count
        self.assertEqual(previous, 8)  # 7 identities plus the primary 62010000 re-typed as secondary

    def test_the_fallback_catalogue_matches_the_legacy_importer(self):
        payload = [entry("99990001", descr="ΠΡΩΤΗ"), entry("99990001", "Δευτερεύουσα", descr="ΔΕΥΤΕΡΗ")]
        sync_company_activities(self.company, payload, as_of=AS_OF)
        catalogue = ActivityCode.objects.get(normalized_code="99990001")
        self.assertEqual((catalogue.code, catalogue.description), ("99.99.00.01", "ΠΡΩΤΗ"))


class ImporterTests(TestCase):
    def items(self, activities=MIXED):
        return [full_item("118717203000", TARGET.isoformat(), activities=copy.deepcopy(activities))]

    def rows(self):
        return list(CompanyActivity.objects.order_by("pk").values_list("pk", "code", "activity_type", "kad_version", "legacy_listed"))

    def test_repeated_imports_keep_primary_keys_and_rows(self):
        with patch("gemiapp.services.fetch_companies", side_effect=lambda _: self.items()):
            import_for_date(TARGET)
            first = self.rows()
            import_for_date(TARGET)
        self.assertEqual(self.rows(), first)
        self.assertEqual(len(first), 7)
        company = Company.objects.get()
        self.assertEqual(listed(company), sorted(legacy_rows(MIXED)))
        self.assertIsNone(company.last_synced_at)  # canonical Company fields are not synchronised by the importer

    def test_the_bulk_and_daily_import_paths_share_the_same_rows(self):
        from .services import import_companies_since_date

        def fake_get(path, params):
            if params.get("isActive") == "true":
                return {"searchResults": self.items(), "searchMetadata": {"totalCount": 1}}
            return {"searchResults": []}

        with patch("gemiapp.services._get", side_effect=fake_get):
            import_companies_since_date(TARGET)
        after_bulk = self.rows()
        with patch("gemiapp.services.fetch_companies", side_effect=lambda _: self.items()):
            import_for_date(TARGET)
        self.assertEqual(self.rows(), after_bulk)
        self.assertEqual(len(after_bulk), 7)

    def test_a_failed_import_leaves_no_partial_activities_and_a_retry_does_not_duplicate(self):
        with patch("gemiapp.services.fetch_companies", side_effect=lambda _: self.items()), \
             patch.object(activities_module, "_write", side_effect=DatabaseError("disk full")):
            with self.assertRaises(DatabaseError):
                import_for_date(TARGET)
        self.assertEqual(ImportRun.objects.get().status, "failed")
        self.assertEqual(CompanyActivity.objects.count(), 0)
        with patch("gemiapp.services.fetch_companies", side_effect=lambda _: self.items()):
            import_for_date(TARGET)
            import_for_date(TARGET)
        self.assertEqual(CompanyActivity.objects.count(), 7)


class BackfillTests(TestCase):
    def test_reliable_raw_data_fills_legacy_rows_in_place_and_adds_the_distinct_activities(self):
        company = make_company(activities=MIXED)
        legacy_sync(company, MIXED)
        columns = ("pk", "code", "activity_type", "description", "legacy_listed")
        before = list(CompanyActivity.objects.order_by("pk").values_list(*columns))

        report = backfill_company_activities()

        self.assertEqual(list(CompanyActivity.objects.filter(pk__in=[row[0] for row in before]).order_by("pk").values_list(*columns)), before)
        self.assertEqual(CompanyActivity.objects.count(), 7)
        self.assertEqual(CompanyActivity.objects.filter(legacy_listed=False).count(), 2)
        self.assertFalse(CompanyActivity.objects.filter(source_key__isnull=True).exists())
        self.assertEqual(set(CompanyActivity.objects.values_list("current_as_of", flat=True)), {AS_OF})
        self.assertEqual(listed(company), sorted(legacy_rows(MIXED)))
        c = report.counts
        self.assertEqual(
            (c.adopted_legacy_rows, c.created, c.unresolved_legacy_rows, c.listed_activities_without_legacy_row, report.rows_before, report.rows_after),
            (5, 2, 0, 0, 5, 7),
        )
        self.assertEqual(report.companies_with_both_kad_versions, 1)
        self.assertEqual(dict(c.currentness), {"current": 3, "ended": 4})

    def test_missing_or_foreign_raw_data_leaves_rows_untouched(self):
        empty = make_company("186420501000", raw_data={})
        foreign = make_company("118717204000", raw_data=full_item("999999999999", activities=MIXED))
        for company in (empty, foreign):
            CompanyActivity.objects.create(company=company, code="47110000", activity_type="Κύρια", description="ΔΟΚΙΜΗ")
        before = list(CompanyActivity.objects.order_by("pk").values())
        report = backfill_company_activities()
        self.assertEqual(list(CompanyActivity.objects.order_by("pk").values()), before)
        self.assertEqual((report.companies_without_gemi_record, report.counts.unresolved_legacy_rows), (2, 2))

    def test_malformed_activities_are_counted_and_the_batch_continues(self):
        malformed = make_company("118717203000", activities="not a list")
        CompanyActivity.objects.create(company=malformed, code="47110000", activity_type="Κύρια")
        partial = make_company("118717204000", activities=["junk", {"activity": {"descr": "x"}}, entry("11110000")])
        good = make_company("118717205000", activities=MIXED)
        legacy_sync(good, MIXED)

        report = backfill_company_activities(batch_size=2)

        c = report.counts
        self.assertEqual((c.malformed_lists, c.without_code, c.unresolved_legacy_rows, c.listed_activities_without_legacy_row), (1, 2, 1, 1))
        self.assertIsNone(CompanyActivity.objects.get(company=malformed).source_key)
        self.assertFalse(CompanyActivity.objects.get(company=partial).legacy_listed)  # created outside the legacy list
        self.assertEqual(CompanyActivity.objects.filter(company=good).count(), 7)

    def test_an_unresolved_legacy_row_is_preserved_and_stays_listed(self):
        company = make_company(activities=[entry("62010000")])
        CompanyActivity.objects.create(company=company, code="62010000", activity_type="Κύρια", description="ΔΡΑΣΤΗΡΙΟΤΗΤΑ 62010000")
        orphan = CompanyActivity.objects.create(company=company, code="99999999", activity_type="Κύρια", description="ΑΓΝΩΣΤΗ")
        report = backfill_company_activities()
        orphan.refresh_from_db()
        self.assertEqual((orphan.legacy_listed, orphan.source_key, orphan.is_current, orphan.kad_version), (True, None, None, None))
        self.assertEqual(report.counts.unresolved_legacy_rows, 1)

    def test_batches_dry_run_idempotency_and_resume(self):
        companies = [make_company(f"1187172{index:02d}000", activities=MIXED) for index in range(5)]
        for company in companies:
            legacy_sync(company, MIXED)

        preview = backfill_company_activities(batch_size=2, dry_run=True)
        self.assertEqual((preview.batches, preview.counts.created, preview.rows_after), (3, 10, 35))
        self.assertEqual(CompanyActivity.objects.count(), 25)
        self.assertFalse(CompanyActivity.objects.filter(source_key__isnull=False).exists())

        first = backfill_company_activities(batch_size=2)
        self.assertEqual((first.counts.created, first.last_company_id), (10, companies[-1].pk))
        state = list(CompanyActivity.objects.order_by("pk").values())
        second = backfill_company_activities(batch_size=3)
        self.assertEqual((second.counts.created, second.counts.updated, second.counts.unchanged), (0, 0, 35))
        self.assertEqual(list(CompanyActivity.objects.order_by("pk").values()), state)

        CompanyActivity.objects.filter(legacy_listed=False).delete()
        CompanyActivity.objects.update(**{name: None for name in CANONICAL_FIELDS})
        resumed = backfill_company_activities(start_company_id=companies[2].pk)
        self.assertEqual(resumed.companies_inspected, 2)
        reconciled = set(CompanyActivity.objects.filter(source_key__isnull=False).values_list("company_id", flat=True))
        self.assertEqual(reconciled, {companies[3].pk, companies[4].pk})

    def test_row_errors_are_counted_and_operational_errors_propagate(self):
        broken = make_company("118717203000", activities=[entry("77770000")])
        fine = make_company("118717204000", activities=[entry("62010000")])
        original = activities_module.canonicalize_activities

        def flaky(entries, *, as_of):
            if entries and entries[0]["activity"]["id"] == "77770000":
                raise TypeError("unexpected legacy shape")
            return original(entries, as_of=as_of)

        with patch.object(activities_module, "canonicalize_activities", side_effect=flaky), \
             self.assertLogs("gemiapp.ingestion.activities", level="WARNING") as logs:
            report = backfill_company_activities()
        self.assertEqual(report.row_errors, 1)
        self.assertIn(f"Company {broken.pk}", "\n".join(logs.output))
        self.assertFalse(CompanyActivity.objects.filter(company=broken).exists())
        self.assertTrue(CompanyActivity.objects.filter(company=fine).exists())

        CompanyActivity.objects.all().delete()
        with patch.object(CompanyActivity.objects, "bulk_create", side_effect=DatabaseError("disk full")):
            with self.assertRaises(DatabaseError):
                backfill_company_activities()

    def test_command_output_and_validation(self):
        make_company(activities=MIXED)
        out = StringIO()
        call_command("backfill_gemi_company_activities", "--dry-run", "--batch-size", "10", stdout=out)
        self.assertIn("[dry-run] rows would create=7", out.getvalue())
        for arguments in (("--batch-size", "0"), ("--start-company-id", "-1")):
            with self.assertRaises(CommandError):
                call_command("backfill_gemi_company_activities", *arguments, stdout=StringIO())


class PrivacyTests(TestCase):
    def test_person_and_contact_data_never_reach_rows_logs_or_output(self):
        company = make_company(activities=MIXED)
        legacy_sync(company, MIXED)
        radar_for(entitled_user(), "Λογισμικό", ["62010000"])
        out = StringIO()
        with self.assertLogs("gemiapp", level="INFO") as logs:
            call_command("backfill_gemi_company_activities", stdout=out)
            call_command("report_gemi_activity_matching_parity", "--list-company-ids", stdout=out)
        rendered = "\n".join([str(list(CompanyActivity.objects.values())), out.getvalue(), "\n".join(logs.output)])
        for sentinel in (PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, "ΔΟΚΙΜΑΣΤΙΚΗ", "ΕΤΑΙΡΕΙΑ ΙΚΕ"):
            self.assertNotIn(sentinel, rendered)


class MatchingFlagTests(TestCase):
    def setUp(self):
        self.user = entitled_user()
        self.company = make_company(activities=MIXED)
        sync_company_activities(self.company, MIXED, as_of=AS_OF)

    def matches(self, company, codes, **kwargs):
        radar = radar_for(self.user, f"Radar {'-'.join(codes)} {CustomerRadar.objects.count()}", codes)
        company = Company.objects.prefetch_related("activity_records").get(pk=company.pk)
        return company_matches_radar(company, radar, **kwargs)[0]

    def test_flag_off_by_default_an_ended_activity_still_matches(self):
        self.assertFalse(activities_module.resolve_current_activities_only())
        self.assertTrue(self.matches(self.company, ["47110000"]))

    @override_settings(GEMI_MATCH_CURRENT_ACTIVITIES_ONLY=True)
    def test_flag_on_ended_activities_no_longer_qualify_and_current_ones_do(self):
        self.assertFalse(self.matches(self.company, ["47110000"]))  # ended in both KAD versions
        self.assertTrue(self.matches(self.company, ["62010000"]))
        self.assertTrue(self.matches(self.company, ["43210000"]))  # future end
        self.assertTrue(self.matches(self.company, ["56100000"]))  # the second period is current

    @override_settings(GEMI_MATCH_CURRENT_ACTIVITIES_ONLY=True)
    def test_flag_on_kad_versions_stay_distinct(self):
        company = make_company("118717204000")
        payload = [entry("11110000", version="kad_2008"), entry("11110000", version="kad_2026", dt_to="2026-01-01")]
        sync_company_activities(company, payload, as_of=AS_OF)
        self.assertEqual(sorted(CompanyActivity.objects.filter(company=company).values_list("kad_version", flat=True)), ["kad_2008", "kad_2026"])
        self.assertFalse(self.matches(company, ["11110000"]))  # the current row is KAD 2008, the KAD 2026 row ended
        self.assertTrue(self.matches(company, ["11110000"], current_activities_only=False))

    @override_settings(GEMI_MATCH_CURRENT_ACTIVITIES_ONLY=True)
    def test_flag_on_unknown_currentness_and_unreconciled_rows_do_not_qualify(self):
        unreadable = make_company("118717204000")
        sync_company_activities(unreadable, [entry("33330000", dt_to="31/12/2026")], as_of=AS_OF)
        self.assertIsNone(CompanyActivity.objects.get(company=unreadable).is_current)
        unreconciled = make_company("118717205000")
        CompanyActivity.objects.create(company=unreconciled, code="33330000", activity_type="Κύρια")
        for company in (unreadable, unreconciled):
            self.assertFalse(self.matches(company, ["33330000"]))
            self.assertTrue(self.matches(company, ["33330000"], current_activities_only=False))

    def test_the_queryset_filter_agrees_with_the_predicate_in_both_modes(self):
        make_company("118717204000", activities=[])
        sync_company_activities(Company.objects.get(gemi_number="118717204000"), [entry("47110000", dt_to="2020-01-01")], as_of=AS_OF)
        companies = list(Company.objects.prefetch_related("activity_records"))
        for current_only in (False, True):
            for code in ("47110000", "62010000", "43210000", "56100000"):
                radar = radar_for(self.user, f"{code}-{current_only}", [code])
                expected = {c.pk for c in companies if company_matches_radar(c, radar, current_activities_only=current_only)[0]}
                found = set(filter_companies_for_radar(
                    Company.objects.all(), activity_codes=[code], current_activities_only=current_only,
                ).values_list("pk", flat=True))
                self.assertEqual(found, expected, (code, current_only))


class LegacyParityTests(TestCase):
    """With the flag off, everything customer-visible that activities feed is identical before A7 (rows as
    the legacy importer left them), after the backfill and after re-importing through the new upsert."""

    CODES = ("47110000", "62010000", "43210000", "56100000")

    def setUp(self):
        self.user = entitled_user()
        self.client.login(username="member@example.com", password="StrongPass123")
        self.items = [
            full_item("118717203000", TARGET.isoformat(), activities=copy.deepcopy(MIXED)),
            full_item("118717204000", TARGET.isoformat(), activities=[entry("47110000", dt_from="2001-01-01", dt_to="2020-01-01")]),
            full_item("118717205000", TARGET.isoformat(), activities=[entry("62010000", "Δευτερεύουσα")]),
        ]
        demo = Company.objects.create(gemi_number="999000101000", name="DEMO", incorporation_date=TARGET, prefecture="ΑΤΤΙΚΗΣ", legal_type="ΙΚΕ", raw_data={})
        CompanyActivity.objects.create(company=demo, code="47110000", activity_type="Κύρια", description="DEMO")
        radar_for(self.user, "Λιανικό", ["47110000"])
        radar_for(self.user, "Λογισμικό", ["62010000"])
        radar_for(self.user, "Όλη η Αττική", prefectures=["ΑΤΤΙΚΗΣ"])
        radar_for(self.user, "Εγκαταστάσεις και εστίαση", ["43210000", "56100000"])

    def rematch(self):
        UserCompanyLead.objects.all().delete()
        DigestDelivery.objects.all().delete()
        match_imported_companies(ImportRun.objects.create(target_date=TARGET, status="success"))

    def snapshot(self):
        from .superadmin.views import _client_finder_qs
        from .views import _filtered_companies

        factory = RequestFactory()
        companies = list(Company.objects.prefetch_related("activity_records").order_by("pk"))
        radars = list(CustomerRadar.objects.prefetch_related("activity_codes").order_by("pk"))
        state = {
            "matches": list(RadarMatch.objects.order_by("radar_id", "company_id").values_list("radar_id", "company_id", "matched_on", "matched_activity_codes", "match_reason")),
            "leads": list(UserCompanyLead.objects.order_by("company_id").values_list("user_id", "company_id", "status")),
            "predicate": [(r.pk, c.pk, company_matches_radar(c, r)) for r in radars for c in companies],
            "preview": {code: sorted(filter_companies_for_radar(Company.objects.all(), activity_codes=[code]).values_list("pk", flat=True)) for code in self.CODES},
            "dashboard": {code: sorted(_filtered_companies(factory.get("/", {"kad": code})).values_list("pk", flat=True)) for code in self.CODES},
            "client_finder": {prefix: sorted(_client_finder_qs(factory.get("/", {"kad": prefix}))[0].values_list("pk", flat=True)) for prefix in ("4711", "62", "5610")},
            "activity_count": CompanyActivity.objects.filter(legacy_listed=True).count(),
        }
        with patch("django.core.signing.time.time", return_value=1789000000.0):
            mail.outbox = []
            state["digest"] = (send_digests(TARGET), [(m.subject, m.body) for m in mail.outbox])
            state["export"] = {code: self.client.get(reverse("export_csv"), {"kad": code}).content for code in self.CODES}
            state["lead_export"] = self.client.get(reverse("lead_export_csv")).content
            state["radar_exports"] = [self.client.get(reverse("radar_export_csv", args=[r.pk])).content for r in radars]
        state["detail"] = [
            self.client.get(reverse("company_detail", args=[c.gemi_number])).content.decode()
            .split("ΔΡΑΣΤΗΡΙΟΤΗΤΕΣ ΚΑΔ", 1)[1].split("<!-- Lead Work Column", 1)[0]
            for c in companies
        ]
        return state

    def test_flag_off_results_are_identical_before_and_after_a7(self):
        with patch("gemiapp.services.fetch_companies", side_effect=lambda _: copy.deepcopy(self.items)), \
             patch("gemiapp.services.sync_company_activities", side_effect=legacy_sync):
            import_for_date(TARGET)
        self.rematch()
        companies = list(Company.objects.prefetch_related("activity_records"))
        radars = list(CustomerRadar.objects.prefetch_related("activity_codes"))
        for radar in radars:
            for company in companies:
                self.assertEqual(company_matches_radar(company, radar), pre_a7_company_matches_radar(company, radar))
        pre_a7 = self.snapshot()
        retail = CustomerRadar.objects.get(name="Λιανικό")
        # Legacy semantics really are exercised: an ended-only company and a demo row match the retail Radar.
        self.assertEqual(len([match for match in pre_a7["matches"] if match[0] == retail.pk]), 3)

        backfill_company_activities()
        self.assertEqual(CompanyActivity.objects.filter(legacy_listed=False).count(), 2)
        self.rematch()
        self.assertEqual(self.snapshot(), pre_a7)

        with patch("gemiapp.services.fetch_companies", side_effect=lambda _: copy.deepcopy(self.items)):
            import_for_date(TARGET)
        self.rematch()
        self.assertEqual(self.snapshot(), pre_a7)


class ParityToolTests(TestCase):
    def setUp(self):
        self.user = entitled_user()
        self.ended = make_company("118717203000", activities=MIXED)
        sync_company_activities(self.ended, MIXED, as_of=AS_OF)
        self.old_version = make_company("118717204000")
        sync_company_activities(self.old_version, [entry("47110000", version="kad_2008")], as_of=AS_OF)
        self.current = make_company("118717205000")
        sync_company_activities(self.current, [entry("47110000")], as_of=AS_OF)
        self.not_legacy = make_company("118717206000", activities=[entry("47110000")])  # no legacy row
        self.unreconciled = make_company("999000101000", raw_data={})
        CompanyActivity.objects.create(company=self.unreconciled, code="47110000", activity_type="Κύρια")
        backfill_company_activities()
        self.retail = radar_for(self.user, "Λιανικό", ["47110000"])
        self.everything = radar_for(self.user, "Όλη η Αττική", prefectures=["ΑΤΤΙΚΗΣ"])
        self.software = radar_for(self.user, "Λογισμικό", ["62010000"])
        lead = UserCompanyLead.objects.create(user=self.user, company=self.old_version)
        RadarMatch.objects.create(radar=self.retail, lead=lead, company=self.old_version, matched_on=TARGET)

    def test_counts_and_classifications(self):
        writes = (CompanyActivity.objects.count(), RadarMatch.objects.count(), UserCompanyLead.objects.count())
        results = {result.radar_id: result for result in matching_parity(chunk_size=2)}
        retail = results[self.retail.pk]
        self.assertEqual((retail.legacy, retail.current_only), (4, 2))
        self.assertEqual(sorted(retail.removed), sorted([self.ended.pk, self.old_version.pk, self.unreconciled.pk]))
        self.assertEqual(retail.added, [self.not_legacy.pk])
        self.assertEqual(dict(retail.removed_reasons), {"ended": 1, "kad_version_not_in_catalogue": 1, "unreconciled_legacy_row": 1})
        self.assertEqual(dict(retail.added_reasons), {"canonical_row_not_legacy_listed": 1})
        self.assertEqual((retail.change_pct, retail.existing_matches, retail.existing_matches_not_current_only), (-50.0, 1, 1))
        everything = results[self.everything.pk]
        self.assertEqual((everything.legacy, everything.current_only, everything.affected), (5, 5, False))
        software = results[self.software.pk]
        self.assertEqual((software.legacy, software.current_only, software.affected), (1, 1, False))
        self.assertEqual((CompanyActivity.objects.count(), RadarMatch.objects.count(), UserCompanyLead.objects.count()), writes)

    def test_command_prints_ids_and_counts_only(self):
        out = StringIO()
        call_command("report_gemi_activity_matching_parity", stdout=out)
        text = out.getvalue()
        self.assertIn("GEMI_MATCH_CURRENT_ACTIVITIES_ONLY=0", text)
        self.assertIn(f"radar={self.retail.pk} active=yes kad_codes=1 legacy=4 current_only=2 removed=3 added=1 change=-50.00%", text)
        self.assertIn("removed reasons: ended=1 kad_version_not_in_catalogue=1 unreconciled_legacy_row=1", text)
        self.assertIn("total radars=3 affected=1", text)
        self.assertNotIn("company ids", text)
        listed_out = StringIO()
        call_command("report_gemi_activity_matching_parity", "--list-company-ids", stdout=listed_out)
        self.assertIn(f"added company ids: [{self.not_legacy.pk}]", listed_out.getvalue())
