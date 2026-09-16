"""Tests for minimised company snapshots (B3, gemiapp.company_snapshots).

Fixture observations only: B3 never calls GEMI and never reads Company.raw_data.
"""

import copy
import json
import unicodedata
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.db import IntegrityError, models, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import AddConstraint, AddIndex, CreateModel
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .company_snapshots import (
    ACTIVITY_STATE_KEYS,
    BASELINE_CREATED,
    CHANGED_CREATED,
    COMPANY_SNAPSHOT_SCHEMA_VERSION,
    STATE_KEYS,
    UNCHANGED,
    SnapshotChronologyError,
    build_company_snapshot_state,
    canonical_state_json,
    company_state_hash,
    latest_company_snapshot,
    record_company_snapshot,
)
from .ingestion.normalizer import GEMI_NORMALIZER_VERSION, normalize_company
from .models import (
    Company, CompanyActivity, CompanyMonitoring, CompanySignal, CompanySnapshot, CustomerRadar, DigestDelivery,
    GemiDiscoveryObservation, GemiSourceRecord, ImportRun, RadarMatch, UserCompanyLead, UserSubscription,
)
from .services import company_matches_radar, import_for_date, match_imported_companies, send_digests
from .test_gemi_company_activities import TARGET, entitled_user, radar_for
from .test_gemi_validation import CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL, full_item

AS_OF = date(2026, 9, 16)
OBSERVED = datetime(2026, 9, 16, 9, 0, tzinfo=dt_timezone.utc)
GEMI_NUMBER = "118717203000"


def activity(code="62010000", type_="Κύρια", version="kad_2026", dt_from="2026-09-14", dt_to=None, descr=None):
    return {
        "activity": {"id": code, "descr": descr if descr is not None else f"ΔΡΑΣΤΗΡΙΟΤΗΤΑ {code}", "kadVersion": version},
        "type": type_, "dtFrom": dt_from, "dtTo": dt_to,
    }


def normalized(gemi_number=GEMI_NUMBER, *, as_of=AS_OF, activities=None, **overrides):
    record = full_item(gemi_number, AS_OF.isoformat(), **overrides)
    if activities is not None:
        record["activities"] = activities
    return normalize_company(record, as_of=as_of)


def make_company(gemi_number=GEMI_NUMBER):
    return Company.objects.create(
        gemi_number=gemi_number, name="ΕΤΑΙΡΕΙΑ ΙΚΕ", incorporation_date=AS_OF, prefecture="ΑΤΤΙΚΗΣ", legal_type="ΙΚΕ",
    )


def source_record(family="company_detail"):
    return GemiSourceRecord.objects.create(
        family=family, response_family=family, endpoint="/companies/118717203000", request_params={},
        request_fingerprint="f" * 64, observation_key=f"k{family}", fetched_at=OBSERVED, http_status=200,
        payload_hash="p" * 64, response_schema_version=1, record_format_version=1, retention_class="standard",
        expires_at=OBSERVED + timedelta(days=30),
    )


class SchemaTests(TestCase):
    def test_fields_carry_state_and_no_identity_or_contact_data(self):
        self.assertEqual(
            {field.name for field in CompanySnapshot._meta.concrete_fields},
            {"id", "company", "schema_version", "normalizer_version", "state_hash", "observed_at", "last_observed_at",
             "is_baseline", "source_record", "status_source_id", "last_status_change", "last_status_change_quality",
             "legal_type_source_id", "gemi_office_source_id", "prefecture_source_id", "municipality_source_id",
             "city", "postal_code", "incorporation_date", "incorporation_date_quality", "activities_state",
             "unknown_current_activity_count", "created_at"},
        )
        for forbidden in ("name", "afm", "vat", "email", "phone", "fax", "url", "street", "address", "person", "capital", "raw"):
            self.assertFalse(
                any(forbidden in field.name for field in CompanySnapshot._meta.concrete_fields), forbidden,
            )

    def test_relations_deletion_rules_and_indexes(self):
        forward = {field.related_model for field in CompanySnapshot._meta.concrete_fields if field.is_relation}
        self.assertEqual(forward, {Company, GemiSourceRecord})
        for tenant in (User, UserSubscription, CustomerRadar, UserCompanyLead):
            self.assertNotIn(tenant, forward)
        self.assertIs(CompanySnapshot._meta.get_field("company").remote_field.on_delete, models.PROTECT)
        self.assertIs(CompanySnapshot._meta.get_field("source_record").remote_field.on_delete, models.PROTECT)
        self.assertTrue(CompanySnapshot._meta.get_field("source_record").null)
        self.assertEqual({index.name for index in CompanySnapshot._meta.indexes}, {"snapshot_company_observed_idx"})

    def test_state_recurrence_is_not_blocked_by_a_unique_constraint(self):
        constraint_fields = [
            tuple(getattr(constraint, "fields", ())) for constraint in CompanySnapshot._meta.constraints
        ]
        self.assertNotIn(("company", "state_hash"), constraint_fields)
        self.assertEqual(
            [constraint.name for constraint in CompanySnapshot._meta.constraints], ["snapshot_observation_span_ordered"],
        )

    def test_the_database_refuses_a_span_that_ends_before_it_starts(self):
        company = make_company()
        values = {
            "company": company, "schema_version": 1, "normalizer_version": 1, "state_hash": "h" * 64,
            "is_baseline": True, "last_status_change_quality": "missing", "incorporation_date_quality": "valid",
            "unknown_current_activity_count": 0,
        }
        with self.assertRaises(IntegrityError), transaction.atomic():
            CompanySnapshot.objects.create(observed_at=OBSERVED, last_observed_at=OBSERVED - timedelta(days=1), **values)

    def test_snapshots_protect_history_from_company_and_source_deletion(self):
        company = make_company()
        record = source_record()
        record_company_snapshot(company, normalized(), OBSERVED, source_record=record)
        with self.assertRaises(ProtectedError), transaction.atomic():
            company.delete()
        with self.assertRaises(ProtectedError), transaction.atomic():
            record.delete()

    def test_the_migration_only_creates_the_snapshot_table(self):
        migration = MigrationLoader(None, ignore_no_migrations=True).get_migration("gemiapp", "0041_company_snapshot")
        self.assertEqual(migration.dependencies, [("gemiapp", "0040_company_signal_discovery_evidence")])
        self.assertTrue(all(isinstance(op, (CreateModel, AddIndex, AddConstraint)) for op in migration.operations))
        self.assertEqual({op.name for op in migration.operations if isinstance(op, CreateModel)}, {"CompanySnapshot"})


class BuilderPurityTests(TestCase):
    def test_the_builder_touches_no_database_and_does_not_mutate_its_input(self):
        record = normalized()
        before = copy.deepcopy(record)
        with self.assertNumQueries(0):
            state = build_company_snapshot_state(record)
        self.assertEqual(record, before)
        self.assertEqual(set(state), set(STATE_KEYS))
        self.assertEqual(state, build_company_snapshot_state(record))
        with self.assertRaises(TypeError):
            build_company_snapshot_state({"ar_gemi": GEMI_NUMBER})

    def test_the_state_serialises_deterministically_without_floats(self):
        state = build_company_snapshot_state(normalized())
        text = canonical_state_json(state)
        self.assertEqual(text, json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        self.assertNotIn(" ", text.split('"current_activities"')[0])  # compact separators
        self.assertEqual(len(company_state_hash(state)), 64)

    def test_every_activity_entry_has_the_pinned_shape(self):
        state = build_company_snapshot_state(normalized(activities=[activity()]))
        self.assertEqual(set(state["current_activities"][0]), set(ACTIVITY_STATE_KEYS))
        self.assertEqual(
            state["current_activities"][0],
            {"code": "62010000", "kad_version": "kad_2026", "activity_type": "primary",
             "source_activity_type": "Κύρια", "date_from": "2026-09-14", "date_from_quality": "valid",
             "date_to": None, "date_to_quality": "missing"},
        )


class StateHashTests(TestCase):
    def hash_of(self, **kwargs):
        return company_state_hash(build_company_snapshot_state(normalized(**kwargs)))

    def test_the_same_business_state_hashes_the_same_regardless_of_time_or_ordering(self):
        activities = [activity("62010000"), activity("47110000", "Δευτερεύουσα", dt_from="2020-01-01")]
        base = self.hash_of(activities=activities)
        self.assertEqual(base, self.hash_of(activities=list(reversed(activities))))
        self.assertEqual(base, self.hash_of(activities=activities, as_of=AS_OF + timedelta(days=5)))
        described = copy.deepcopy(activities)
        described[0]["activity"]["descr"] = "ΕΝΤΕΛΩΣ ΑΛΛΗ ΠΕΡΙΓΡΑΦΗ"
        self.assertEqual(base, self.hash_of(activities=described))
        self.assertEqual(base, self.hash_of(activities=activities, coNameEl="ΑΛΛΗ ΕΠΩΝΥΜΙΑ", afm="123456789"))

    def test_every_canonical_fact_changes_the_hash(self):
        base = self.hash_of()
        cases = {
            "status": {"status": {"id": 17, "descr": "Διαγραφή"}},
            "legal type": {"legalType": {"id": 20, "descr": "ΑΕ"}},
            "gemi office": {"gemiOffice": {"id": 9, "descr": "ΑΛΛΟ"}},
            "prefecture": {"prefecture": {"id": 6, "descr": "ΑΧΑΪΑΣ"}},
            "municipality": {"municipality": {"id": 61191, "descr": "ΑΛΛΟΣ"}},
            "city": {"city": "ΠΑΤΡΑ"},
            "postal code": {"zipCode": "26221"},
            "incorporation date": {"incorporationDate": "2026-09-13"},
            "status change date": {"lastStatusChange": "2026-09-15"},
        }
        for label, override in cases.items():
            with self.subTest(change=label):
                self.assertNotEqual(self.hash_of(**override), base)

    def test_activity_changes_and_kad_versions_change_the_hash(self):
        base = self.hash_of(activities=[activity("62010000")])
        for label, activities in {
            "added": [activity("62010000"), activity("47110000")],
            "removed": [],
            "kad version": [activity("62010000", version="kad_2008")],
            "type": [activity("62010000", "Δευτερεύουσα")],
            "period start": [activity("62010000", dt_from="2020-01-01")],
            "ended": [activity("62010000", dt_to="2026-01-01")],
        }.items():
            with self.subTest(change=label):
                self.assertNotEqual(self.hash_of(activities=activities), base)

    def test_a_description_only_status_change_does_not_change_the_hash(self):
        self.assertEqual(self.hash_of(status={"id": 3, "descr": "ΕΝΕΡΓΗ"}), self.hash_of(status={"id": 3, "descr": "Ενεργή"}))


class ActivityStateTests(TestCase):
    def state(self, activities):
        return build_company_snapshot_state(normalized(activities=activities))

    def test_only_verified_current_activities_are_included(self):
        state = self.state([
            activity("62010000"),                                            # current
            activity("47110000", dt_to="2026-01-01"),                        # ended
            activity("43210000", dt_to="31/12/2026"),                        # unreadable end -> unknown
            activity("56100000", dt_to="2027-06-30"),                        # future end -> current
        ])
        self.assertEqual([entry["code"] for entry in state["current_activities"]], ["56100000", "62010000"])
        self.assertEqual(state["unknown_current_activity_count"], 1)

    def test_unknown_currentness_participates_in_the_hash_as_observation_quality(self):
        without = company_state_hash(self.state([activity("62010000")]))
        with_unknown = company_state_hash(self.state([activity("62010000"), activity("47110000", dt_to="31/12/2026")]))
        self.assertNotEqual(without, with_unknown)

    def test_a_current_kad_2008_activity_is_kept_and_stays_distinct_from_kad_2026(self):
        state = self.state([activity("62010000", version="kad_2008"), activity("62010000", version="kad_2026")])
        self.assertEqual(
            [(entry["code"], entry["kad_version"]) for entry in state["current_activities"]],
            [("62010000", "kad_2008"), ("62010000", "kad_2026")],
        )

    def test_ordering_is_stable_and_identical_entries_collapse(self):
        entries = [
            activity("62010000", "Δευτερεύουσα"), activity("62010000", "Κύρια"),
            activity("47110000", version="kad_2008"), activity("47110000", version="kad_2026"),
            activity("62010000", "Κύρια"),  # exact duplicate of an earlier entry
            activity("56100000", dt_from=None),
        ]
        state = self.state(entries)
        self.assertEqual(
            [(entry["code"], entry["kad_version"], entry["activity_type"], entry["date_from"]) for entry in state["current_activities"]],
            [
                ("47110000", "kad_2008", "primary", "2026-09-14"),
                ("47110000", "kad_2026", "primary", "2026-09-14"),
                ("56100000", "kad_2026", "primary", None),
                ("62010000", "kad_2026", "primary", "2026-09-14"),
                ("62010000", "kad_2026", "secondary", "2026-09-14"),
            ],
        )
        self.assertEqual(state, self.state(list(reversed(entries))))

    def test_source_activity_type_keeps_the_case_and_accents_a3_preserves(self):
        # A3 normalize_text applies NFC and collapses whitespace but preserves case and accents, while the
        # A7 canonical activity_type goes through normalize_kad_search (accent-stripped, uppercased). Pin
        # both: cosmetic spacing/Unicode-form variance is absorbed, case and accent variance is not.
        padded = " \tΚύρια\n "  # cosmetic whitespace only
        spaced = self.state([activity("62010000", "Κύρια"), activity("62010000", padded)])
        self.assertEqual([entry["source_activity_type"] for entry in spaced["current_activities"]], ["Κύρια"])

        decomposed = unicodedata.normalize("NFD", "Κύρια")
        self.assertNotEqual(decomposed, "Κύρια")
        self.assertEqual(
            company_state_hash(self.state([activity("62010000", decomposed)])),
            company_state_hash(self.state([activity("62010000", "Κύρια")])),
        )

        cased = self.state([activity("62010000", "Κύρια"), activity("62010000", "ΚΥΡΙΑ")])
        self.assertEqual(
            [(entry["activity_type"], entry["source_activity_type"]) for entry in cased["current_activities"]],
            [("primary", "ΚΥΡΙΑ"), ("primary", "Κύρια")],
        )
        self.assertNotEqual(
            company_state_hash(cased), company_state_hash(self.state([activity("62010000", "Κύρια")])),
        )

        unaccented = self.state([activity("62010000", "ΚΥΡΙΑ")])
        accented = self.state([activity("62010000", "ΚΎΡΙΑ")])
        self.assertEqual(
            [entry["activity_type"] for entry in unaccented["current_activities"]],
            [entry["activity_type"] for entry in accented["current_activities"]],
        )
        self.assertNotEqual(company_state_hash(unaccented), company_state_hash(accented))

    def test_distinct_periods_of_one_code_stay_separate(self):
        state = self.state([activity("62010000", dt_from="2020-01-01"), activity("62010000", dt_from="2026-09-14")])
        self.assertEqual([entry["date_from"] for entry in state["current_activities"]], ["2020-01-01", "2026-09-14"])


class DateQualityTests(TestCase):
    def test_dates_keep_their_quality_and_are_never_clamped_or_stamped(self):
        cases = {
            "2026-09-14": ("2026-09-14", "valid"),
            "": (None, "missing"),
            "14/09/2026": (None, "invalid"),
            "9011-12-09": (None, "out_of_range"),
        }
        for value, (expected_date, expected_quality) in cases.items():
            with self.subTest(value=value):
                state = build_company_snapshot_state(normalized(incorporationDate=value, lastStatusChange=value))
                self.assertEqual((state["incorporation_date"], state["incorporation_date_quality"]), (expected_date, expected_quality))
                self.assertEqual((state["last_status_change"], state["last_status_change_quality"]), (expected_date, expected_quality))

    def test_activity_dates_keep_their_quality(self):
        state = build_company_snapshot_state(normalized(activities=[activity(dt_from="9011-12-09", dt_to="2027-01-01")]))
        entry = state["current_activities"][0]
        self.assertEqual((entry["date_from"], entry["date_from_quality"]), (None, "out_of_range"))
        self.assertEqual((entry["date_to"], entry["date_to_quality"]), ("2027-01-01", "valid"))

    def test_stored_dates_are_dates_not_midnight_timestamps(self):
        company = make_company()
        record_company_snapshot(company, normalized(), OBSERVED)
        snapshot = latest_company_snapshot(company)
        self.assertIsInstance(snapshot.incorporation_date, date)
        self.assertNotIsInstance(snapshot.incorporation_date, datetime)


class WriterTests(TestCase):
    def setUp(self):
        self.company = make_company()

    def test_the_first_observation_creates_a_baseline_and_no_signal(self):
        result = record_company_snapshot(self.company, normalized(), OBSERVED)
        snapshot = result.snapshot
        self.assertEqual((result.status, snapshot.is_baseline), (BASELINE_CREATED, True))
        self.assertEqual((snapshot.observed_at, snapshot.last_observed_at), (OBSERVED, OBSERVED))
        self.assertEqual((snapshot.schema_version, snapshot.normalizer_version), (COMPANY_SNAPSHOT_SCHEMA_VERSION, GEMI_NORMALIZER_VERSION))
        self.assertEqual(CompanySignal.objects.count(), 0)
        self.assertEqual(snapshot.state_hash, company_state_hash(build_company_snapshot_state(normalized())))

    def test_an_identical_observation_extends_the_span_without_a_new_row(self):
        first = record_company_snapshot(self.company, normalized(), OBSERVED).snapshot
        later = OBSERVED + timedelta(days=1)
        result = record_company_snapshot(self.company, normalized(), later)
        self.assertEqual((result.status, result.span_extended), (UNCHANGED, True))
        first.refresh_from_db()
        self.assertEqual((first.observed_at, first.last_observed_at), (OBSERVED, later))
        self.assertEqual(CompanySnapshot.objects.count(), 1)

        same_again = record_company_snapshot(self.company, normalized(), later)
        self.assertEqual((same_again.status, same_again.span_extended), (UNCHANGED, False))
        self.assertEqual(CompanySnapshot.objects.count(), 1)

    def test_a_changed_state_creates_a_non_baseline_snapshot(self):
        record_company_snapshot(self.company, normalized(), OBSERVED)
        changed = normalized(status={"id": 17, "descr": "Διαγραφή"})
        result = record_company_snapshot(self.company, changed, OBSERVED + timedelta(days=2))
        self.assertEqual((result.status, result.snapshot.is_baseline), (CHANGED_CREATED, False))
        self.assertEqual(CompanySnapshot.objects.count(), 2)
        self.assertEqual(latest_company_snapshot(self.company).status_source_id, "17")

    def test_state_reversion_keeps_every_period_visible(self):
        state_a, state_b = normalized(), normalized(municipality={"id": 61191, "descr": "ΑΛΛΟΣ"})
        moments = [OBSERVED + timedelta(days=index) for index in range(5)]
        statuses = [
            record_company_snapshot(self.company, record, moment).status
            for record, moment in zip([state_a, state_a, state_b, state_b, state_a], moments)
        ]
        self.assertEqual(statuses, [BASELINE_CREATED, UNCHANGED, CHANGED_CREATED, UNCHANGED, CHANGED_CREATED])
        rows = list(CompanySnapshot.objects.order_by("observed_at"))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].state_hash, rows[2].state_hash)
        self.assertNotEqual(rows[0].pk, rows[2].pk)
        self.assertEqual([row.is_baseline for row in rows], [True, False, False])

    def test_an_out_of_order_observation_never_rewrites_chronology(self):
        record_company_snapshot(self.company, normalized(), OBSERVED)
        record_company_snapshot(self.company, normalized(), OBSERVED + timedelta(days=2))
        for record in (normalized(), normalized(city="ΠΑΤΡΑ")):
            with self.subTest(record=record.city), self.assertRaises(SnapshotChronologyError):
                record_company_snapshot(self.company, record, OBSERVED + timedelta(days=1))
        snapshot = latest_company_snapshot(self.company)
        self.assertEqual((CompanySnapshot.objects.count(), snapshot.last_observed_at), (1, OBSERVED + timedelta(days=2)))

    def test_the_writer_refuses_a_foreign_record_a_naive_time_and_a_wrong_source_family(self):
        with self.assertRaises(ValueError):
            record_company_snapshot(self.company, normalized("999999999999"), OBSERVED)
        with self.assertRaises(ValueError):
            record_company_snapshot(self.company, normalized(), datetime(2026, 9, 16, 9, 0))
        with self.assertRaises(TypeError):
            record_company_snapshot(self.company, normalized(), AS_OF)
        with self.assertRaises(ValueError):
            record_company_snapshot(self.company, normalized(), OBSERVED, source_record=source_record("reference_data"))
        self.assertEqual(CompanySnapshot.objects.count(), 0)

    def test_the_writer_re_reads_the_latest_snapshot_inside_the_transaction(self):
        record_company_snapshot(self.company, normalized(), OBSERVED)
        with patch("gemiapp.company_snapshots.latest_company_snapshot", wraps=latest_company_snapshot) as lookup:
            record_company_snapshot(self.company, normalized(), OBSERVED + timedelta(days=1))
        self.assertTrue(lookup.call_args.kwargs["for_update"])  # PostgreSQL row lock; SQLite cannot prove it


class ProvenanceTests(TestCase):
    def test_a_snapshot_works_without_a_source_record_and_keeps_one_when_given(self):
        company = make_company()
        without = record_company_snapshot(company, normalized(), OBSERVED).snapshot
        self.assertIsNone(without.source_record)
        self.assertEqual(GemiSourceRecord.objects.count(), 0)  # nothing fabricated

        record = source_record("company_search")
        changed = record_company_snapshot(company, normalized(city="ΠΑΤΡΑ"), OBSERVED + timedelta(days=1), source_record=record)
        self.assertEqual(changed.snapshot.source_record_id, record.pk)


class PrivacyTests(TestCase):
    def test_no_identity_contact_or_payload_data_can_reach_a_snapshot(self):
        company = make_company()
        record = full_item(GEMI_NUMBER, AS_OF.isoformat(), fax="SENTINEL-FAX-4821", url="https://sentinel.invalid")
        record["street"], record["streetNumber"] = "ΟΔΟΣ ΜΥΣΤΙΚΗ", "13"
        with self.assertLogs("gemiapp.company_snapshots", level="INFO") as logs:
            result = record_company_snapshot(company, normalize_company(record, as_of=AS_OF), OBSERVED)
        rendered = "\n".join([
            str(list(CompanySnapshot.objects.values())), repr(result.snapshot), str(result.snapshot),
            canonical_state_json(build_company_snapshot_state(normalize_company(record, as_of=AS_OF))),
            "\n".join(logs.output),
        ])
        sentinels = (
            PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, "SENTINEL-FAX-4821", "sentinel.invalid",
            "ΔΟΚΙΜΑΣΤΙΚΗ", "ΟΔΟΣ ΜΥΣΤΙΚΗ", "099999999", "ΕΜΠΟΡΙΟ",
        )
        for sentinel in sentinels:
            self.assertNotIn(sentinel, rendered)

    def test_branch_state_is_deferred_not_scraped_from_the_payload(self):
        state = build_company_snapshot_state(normalized())
        self.assertNotIn("is_branch", state)
        self.assertNotIn("isBranch", canonical_state_json(state))


class CustomerParityTests(TestCase):
    """Writing snapshots changes nothing customer-visible, and no signal or monitoring row moves."""

    def setUp(self):
        self.user = entitled_user()
        self.client.login(username="member@example.com", password="StrongPass123")
        items = [full_item("118717203000", TARGET.isoformat()), full_item("118717204000", TARGET.isoformat())]
        with patch("gemiapp.services.fetch_companies", side_effect=lambda _: copy.deepcopy(items)):
            import_for_date(TARGET)
        radar_for(self.user, "Αττική", prefectures=["ΑΤΤΙΚΗΣ"])
        match_imported_companies(ImportRun.objects.create(target_date=TARGET, status="success"))

    def snapshot_state(self):
        companies = list(Company.objects.prefetch_related("activity_records").order_by("pk"))
        radars = list(CustomerRadar.objects.prefetch_related("activity_codes").order_by("pk"))
        state = {
            "companies": list(Company.objects.order_by("pk").values()),
            "activities": list(CompanyActivity.objects.order_by("pk").values()),
            "signals": list(CompanySignal.objects.values()),
            "monitoring": list(CompanyMonitoring.objects.values()),
            "discovery": list(GemiDiscoveryObservation.objects.values()),
            "matches": list(RadarMatch.objects.order_by("pk").values_list("radar_id", "company_id", "matched_on")),
            "leads": list(UserCompanyLead.objects.order_by("pk").values_list("user_id", "company_id", "status")),
            "subscriptions": list(UserSubscription.objects.order_by("pk").values()),
            "predicate": [(radar.pk, company.pk, company_matches_radar(company, radar)) for radar in radars for company in companies],
        }
        with patch("django.core.signing.time.time", return_value=1789000000.0):
            mail.outbox = []
            DigestDelivery.objects.all().delete()
            state["digest"] = (send_digests(TARGET), [(message.subject, message.body) for message in mail.outbox])
            state["export"] = self.client.get(reverse("export_csv")).content
            state["picker"] = self.client.get(reverse("kad_search"), {"q": "62"}).json()
        return state

    def test_snapshots_leave_every_customer_facing_result_identical(self):
        before = self.snapshot_state()
        self.assertTrue(before["matches"])
        for company in Company.objects.all():
            record_company_snapshot(company, normalized(company.gemi_number), OBSERVED)
        self.assertEqual(CompanySnapshot.objects.count(), 2)
        self.assertEqual(self.snapshot_state(), before)
