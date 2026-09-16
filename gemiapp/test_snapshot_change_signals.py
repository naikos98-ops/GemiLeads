"""Tests for snapshot change detection and Tier-1 change signals (B5, gemiapp.snapshot_change_signals).

Deterministic snapshot fixtures only. B5 never calls GEMI and never reads Company.raw_data.
"""

import inspect
import json
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import CreateModel
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from . import snapshot_change_signals as b5
from .company_signals import (
    KAD_ADDED,
    KAD_REMOVED,
    LEGAL_FORM_CHANGED,
    LOCATION_CHANGED,
    PRECISION_DATE,
    PRECISION_NONE,
    SHADOW,
    SNAPSHOT_DIFF,
    STATUS_CHANGED,
    record_company_signal,
    rule_for,
)
from .company_snapshots import COMPANY_SNAPSHOT_SCHEMA_VERSION, company_state_hash, record_company_snapshot
from .ingestion.normalizer import normalize_company
from .models import (
    Company, CompanyActivity, CompanyMonitoring, CompanySignal, CompanySignalSnapshotEvidence, CompanySnapshot,
    CustomerRadar, RadarMatch, UserCompanyLead, UserSubscription,
)
from .services import company_matches_radar
from .snapshot_change_signals import (
    BASELINE,
    COMPARED,
    INCOMPATIBLE_SNAPSHOT_SCHEMA,
    KAD_TAXONOMY_TRANSITION_DATE,
    SnapshotEvidenceConflictError,
    SnapshotPairError,
    detect_snapshot_changes,
    immediate_predecessor,
    materialize_all_snapshot_change_signals,
    materialize_snapshot_change_signals,
    transition_anchor,
)
from .test_gemi_company_activities import entitled_user, radar_for
from .test_gemi_validation import CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL, full_item

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=dt_timezone.utc)
GEMI = "118717203000"
BOUNDARY = "2026-03-01"


def act(code, version="kad_2026", type_="primary", source="Κύρια", dt_from="2020-01-01", from_q="valid",
        dt_to=None, to_q="missing"):
    return {
        "code": code, "kad_version": version, "activity_type": type_, "source_activity_type": source,
        "date_from": dt_from if from_q == "valid" else None, "date_from_quality": from_q,
        "date_to": dt_to if to_q == "valid" else None, "date_to_quality": to_q,
    }


BASE_STATE = {
    "status_source_id": "3", "last_status_change": "2026-01-10", "last_status_change_quality": "valid",
    "legal_type_source_id": "19", "gemi_office_source_id": "3", "prefecture_source_id": "5",
    "municipality_source_id": "61190", "city": "ΚΗΦΙΣΙΑ", "postal_code": "14561",
    "incorporation_date": "2020-01-01", "incorporation_date_quality": "valid",
    "current_activities": [act("47191002")], "unknown_current_activity_count": 0,
}


def make_company(gemi_number=GEMI):
    return Company.objects.create(
        gemi_number=gemi_number, name="ΕΤΑΙΡΕΙΑ ΙΚΕ", incorporation_date=date(2020, 1, 1), prefecture="ΑΤΤΙΚΗΣ",
    )


class History:
    """Builds consecutive snapshots of one company exactly as B3 would store them."""

    def __init__(self, company):
        self.company = company
        self.count = 0

    def add(self, *, schema_version=COMPANY_SNAPSHOT_SCHEMA_VERSION, observed_at=None, **changes):
        state = {**BASE_STATE, **changes}
        state["current_activities"] = sorted(
            state["current_activities"], key=lambda e: (e["code"], e["kad_version"] or "", e["activity_type"]),
        )
        observed_at = observed_at or T0 + timedelta(days=self.count)
        latest = CompanySnapshot.objects.filter(company=self.company).order_by("-observed_at", "-id").first()
        self.count += 1
        return CompanySnapshot.objects.create(
            company=self.company, schema_version=schema_version, normalizer_version=1,
            state_hash=company_state_hash(state), observed_at=observed_at, last_observed_at=observed_at,
            is_baseline=latest is None,
            status_source_id=state["status_source_id"],
            last_status_change=date.fromisoformat(state["last_status_change"]) if state["last_status_change"] else None,
            last_status_change_quality=state["last_status_change_quality"],
            legal_type_source_id=state["legal_type_source_id"], gemi_office_source_id=state["gemi_office_source_id"],
            prefecture_source_id=state["prefecture_source_id"], municipality_source_id=state["municipality_source_id"],
            city=state["city"], postal_code=state["postal_code"],
            incorporation_date=date.fromisoformat(state["incorporation_date"]),
            incorporation_date_quality=state["incorporation_date_quality"],
            activities_state=state["current_activities"],
            unknown_current_activity_count=state["unknown_current_activity_count"],
        )


def run(snapshot, **kwargs):
    return materialize_snapshot_change_signals(snapshot, **kwargs)


def types(signals=None):
    signals = CompanySignal.objects.all() if signals is None else signals
    return sorted(signal.signal_type for signal in signals)


class B5TestCase(TestCase):
    def setUp(self):
        self.company = make_company()
        self.history = History(self.company)

    def detect(self, previous, current):
        return detect_snapshot_changes(previous, current)


# --- registry, schema, safety ------------------------------------------------------------------

class RuleRegistryTests(TestCase):
    def test_exact_v1_rule_versions_all_implemented_from_snapshot_diff_only(self):
        expected = {
            STATUS_CHANGED: "status_changed:v1", KAD_ADDED: "kad_added:v1", KAD_REMOVED: "kad_removed:v1",
            LEGAL_FORM_CHANGED: "legal_form_changed:v1", LOCATION_CHANGED: "location_changed:v1",
        }
        for signal_type, version in expected.items():
            rule = rule_for(signal_type)
            self.assertEqual((rule.rule_version, rule.source_types, rule.implemented),
                             (version, frozenset({SNAPSHOT_DIFF}), True))

    def test_later_corporate_events_remain_taxonomy_only(self):
        for later in ("capital_increase", "new_branch", "merger", "dissolution", "management_change"):
            self.assertIsNone(rule_for(later))

    def test_the_taxonomy_boundary_is_one_central_constant(self):
        self.assertEqual(KAD_TAXONOMY_TRANSITION_DATE, date(2026, 3, 1))


class SchemaTests(TestCase):
    def test_evidence_relations_protect_snapshots_and_follow_the_signal(self):
        fields = {f.name: f for f in CompanySignalSnapshotEvidence._meta.get_fields() if f.concrete}
        self.assertEqual(fields["signal"].remote_field.on_delete.__name__, "CASCADE")
        self.assertTrue(fields["signal"].one_to_one)
        for name in ("previous_snapshot", "current_snapshot"):
            self.assertEqual(fields[name].remote_field.on_delete.__name__, "PROTECT")
        self.assertEqual(
            set(fields) - {"id", "created_at"},
            {"signal", "previous_snapshot", "current_snapshot", "subject_kind", "before_source_id",
             "after_source_id", "activity_code", "kad_version"},
        )
        self.assertFalse(any(isinstance(f, __import__("django").db.models.JSONField) for f in fields.values()))

    def test_the_migration_only_creates_the_evidence_table(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        operations = loader.disk_migrations[("gemiapp", "0043_company_signal_snapshot_evidence")].operations
        self.assertEqual([type(op) for op in operations], [CreateModel])
        self.assertEqual(operations[0].name, "CompanySignalSnapshotEvidence")


class ShadowSafetyTests(B5TestCase):
    def test_there_is_no_mode_parameter_and_no_live_option(self):
        for fn in (materialize_snapshot_change_signals, materialize_all_snapshot_change_signals, detect_snapshot_changes):
            self.assertNotIn("mode", inspect.signature(fn).parameters)
        from gemiapp.management.commands.materialize_snapshot_change_signals import Command

        parser = Command().create_parser("manage.py", "materialize_snapshot_change_signals")
        options = {action.dest for action in parser._actions}
        self.assertNotIn("live", options)
        self.assertNotIn("mode", options)
        source = inspect.getsource(b5)
        self.assertNotIn("LIVE", source)
        self.assertNotIn("promote_company_signal", source)

    def test_every_b5_signal_is_shadow(self):
        a = self.history.add()
        b = self.history.add(status_source_id="8", legal_type_source_id="20", municipality_source_id="61191",
                             current_activities=[act("47191002"), act("62010000")])
        run(b)
        self.assertEqual(CompanySignal.objects.count(), 4)
        self.assertEqual(set(CompanySignal.objects.values_list("mode", flat=True)), {SHADOW})
        self.assertEqual(set(CompanySignal.objects.values_list("source_type", flat=True)), {SNAPSHOT_DIFF})
        self.assertEqual(set(CompanySignal.objects.values_list("confidence", flat=True)), {Decimal("1.0000")})

    def test_b5_never_calls_gemi(self):
        self.assertNotIn("get_gemi_client", inspect.getsource(b5))
        self.assertNotIn("GemiClient", inspect.getsource(b5))

    def test_b4_is_not_hooked_to_the_detector(self):
        source = open("gemiapp/ingestion/refresh.py", encoding="utf-8").read()
        self.assertNotIn("snapshot_change_signals", source)

    def test_nothing_is_scheduled(self):
        from gemiapp.apps import SCHEDULES

        for entry in SCHEDULES:
            self.assertNotIn("snapshot", entry["func"])
            self.assertNotIn("signal", entry["func"])


# --- pair validation ---------------------------------------------------------------------------

class PairValidationTests(B5TestCase):
    def test_scenario_1_a_baseline_alone_emits_nothing(self):
        a = self.history.add()
        self.assertEqual(detect_snapshot_changes(None, a).status, BASELINE)
        report = run(a)
        self.assertEqual((report.baselines_skipped, report.transitions_inspected), (1, 0))
        self.assertEqual(CompanySignal.objects.count(), 0)

    def test_snapshots_of_different_companies_are_refused(self):
        a = self.history.add()
        other = History(make_company("999999999999"))
        other.add()
        b = other.add(status_source_id="8")
        with self.assertRaises(SnapshotPairError):
            detect_snapshot_changes(a, b)

    def test_reversed_chronology_is_refused(self):
        a = self.history.add()
        b = self.history.add(status_source_id="8")
        with self.assertRaises(SnapshotPairError):
            detect_snapshot_changes(b, a)

    def test_the_predecessor_is_the_immediate_one_never_the_baseline(self):
        a = self.history.add()
        b = self.history.add(status_source_id="8")
        c = self.history.add(status_source_id="9")
        self.assertEqual(immediate_predecessor(c), b)
        self.assertEqual(immediate_predecessor(b), a)
        self.assertIsNone(immediate_predecessor(a))
        run(c)
        evidence = CompanySignalSnapshotEvidence.objects.get()
        self.assertEqual((evidence.previous_snapshot, evidence.current_snapshot), (b, c))
        self.assertEqual((evidence.before_source_id, evidence.after_source_id), ("8", "9"))

    def test_same_observation_time_breaks_the_tie_by_id(self):
        a = self.history.add(observed_at=T0)
        b = self.history.add(observed_at=T0, status_source_id="8")
        self.assertEqual(immediate_predecessor(b), a)
        self.assertEqual(detect_snapshot_changes(a, b).status, COMPARED)

    def test_incompatible_schema_versions_are_reported_not_guessed(self):
        a = self.history.add()
        b = self.history.add(schema_version=2, status_source_id="8")
        result = detect_snapshot_changes(a, b)
        self.assertEqual((result.status, result.candidates), (INCOMPATIBLE_SNAPSHOT_SCHEMA, ()))
        report = run(b)
        self.assertEqual(report.incompatible_schema_pairs, 1)
        self.assertEqual(CompanySignal.objects.count(), 0)

    def test_an_unsupported_schema_on_both_sides_is_also_incompatible(self):
        a = self.history.add(schema_version=2)
        b = self.history.add(schema_version=2, status_source_id="8")
        self.assertEqual(detect_snapshot_changes(a, b).status, INCOMPATIBLE_SNAPSHOT_SCHEMA)

    def test_the_detector_performs_no_query(self):
        a = self.history.add()
        b = self.history.add(status_source_id="8", current_activities=[act("62010000")])
        with self.assertNumQueries(0):
            result = detect_snapshot_changes(a, b)
        self.assertEqual(len(result.candidates), 3)

    def test_a_changed_hash_alone_is_never_a_signal(self):
        a = self.history.add()
        b = self.history.add(unknown_current_activity_count=2, gemi_office_source_id="7",
                             incorporation_date="2019-05-05")
        result = detect_snapshot_changes(a, b)
        self.assertTrue(result.state_changed)
        self.assertEqual(result.candidates, ())


# --- status / legal form / location ------------------------------------------------------------

class StatusTests(B5TestCase):
    def test_scenario_2_known_to_different_known_with_a_valid_status_date(self):
        self.history.add()
        b = self.history.add(status_source_id="8", last_status_change="2026-08-20")
        run(b)
        signal = CompanySignal.objects.get()
        self.assertEqual(signal.signal_type, STATUS_CHANGED)
        self.assertEqual(signal.rule_version, "status_changed:v1")
        self.assertEqual((signal.effective_precision, signal.effective_date, signal.effective_at),
                         (PRECISION_DATE, date(2026, 8, 20), None))
        self.assertEqual(signal.detected_at, b.observed_at)

    def test_an_unusable_status_date_leaves_no_source_time(self):
        for quality in ("missing", "invalid", "out_of_range"):
            with self.subTest(quality=quality):
                CompanySignalSnapshotEvidence.objects.all().delete()
                CompanySignal.objects.all().delete()
                CompanySnapshot.objects.all().delete()
                self.history = History(self.company)
                self.history.add()
                b = self.history.add(status_source_id="8", last_status_change=None, last_status_change_quality=quality)
                run(b)
                signal = CompanySignal.objects.get()
                self.assertEqual((signal.effective_precision, signal.effective_date), (PRECISION_NONE, None))

    def test_scenario_3_null_and_same_status_transitions_are_silent(self):
        for before, after in ((None, "8"), ("3", None), (None, None), ("3", "3")):
            with self.subTest(before=before, after=after):
                a = self.history.add(status_source_id=before)
                b = self.history.add(status_source_id=after, city=f"ΠΟΛΗ {self.history.count}")
                self.assertEqual([c.signal_type for c in detect_snapshot_changes(a, b).candidates], [])


class LegalFormTests(B5TestCase):
    def test_scenario_4_known_to_different_known_has_no_source_time(self):
        self.history.add()
        b = self.history.add(legal_type_source_id="20")
        run(b)
        signal = CompanySignal.objects.get()
        self.assertEqual((signal.signal_type, signal.rule_version), (LEGAL_FORM_CHANGED, "legal_form_changed:v1"))
        self.assertEqual(signal.effective_precision, PRECISION_NONE)
        evidence = signal.snapshot_evidence
        self.assertEqual((evidence.subject_kind, evidence.before_source_id, evidence.after_source_id),
                         ("legal_form", "19", "20"))

    def test_null_legal_form_transitions_are_silent(self):
        for before, after in ((None, "20"), ("19", None)):
            with self.subTest(before=before, after=after):
                a = self.history.add(legal_type_source_id=before)
                b = self.history.add(legal_type_source_id=after, city=f"ΠΟΛΗ {self.history.count}")
                self.assertEqual(detect_snapshot_changes(a, b).candidates, ())


class LocationTests(B5TestCase):
    def test_scenario_5_municipality_change_with_postal_change_is_one_signal(self):
        self.history.add()
        b = self.history.add(municipality_source_id="61191", postal_code="15125", city="ΜΑΡΟΥΣΙ")
        run(b)
        self.assertEqual(types(), [LOCATION_CHANGED])
        signal = CompanySignal.objects.get()
        self.assertEqual((signal.rule_version, signal.effective_precision), ("location_changed:v1", PRECISION_NONE))
        self.assertEqual((signal.snapshot_evidence.subject_kind, signal.snapshot_evidence.after_source_id),
                         ("municipality", "61191"))

    def test_scenario_6_postal_city_or_prefecture_only_change_is_a_changed_snapshot_without_signal(self):
        self.history.add()
        b = self.history.add(postal_code="14562", city="ΝΕΑ ΚΗΦΙΣΙΑ", prefecture_source_id="6")
        report = run(b)
        self.assertEqual(CompanySignal.objects.count(), 0)
        self.assertEqual(report.changed_snapshot_no_tier1_signal, 1)

    def test_null_municipality_transitions_are_silent(self):
        for before, after in ((None, "61191"), ("61190", None)):
            with self.subTest(before=before, after=after):
                a = self.history.add(municipality_source_id=before)
                b = self.history.add(municipality_source_id=after, prefecture_source_id=f"{self.history.count}")
                self.assertEqual(detect_snapshot_changes(a, b).candidates, ())


# --- KAD -----------------------------------------------------------------------------------------

class KadTests(B5TestCase):
    def test_scenario_7_an_added_kad_uses_its_valid_dt_from(self):
        self.history.add()
        b = self.history.add(current_activities=[act("47191002"), act("62010000", dt_from="2026-08-01")])
        run(b)
        signal = CompanySignal.objects.get()
        self.assertEqual((signal.signal_type, signal.rule_version), (KAD_ADDED, "kad_added:v1"))
        self.assertEqual((signal.effective_precision, signal.effective_date), (PRECISION_DATE, date(2026, 8, 1)))
        evidence = signal.snapshot_evidence
        self.assertEqual((evidence.subject_kind, evidence.activity_code, evidence.kad_version,
                          evidence.before_source_id, evidence.after_source_id),
                         ("kad", "62010000", "kad_2026", None, None))

    def test_scenario_8_a_removed_kad_uses_the_prior_valid_dt_to(self):
        self.history.add(current_activities=[
            act("47191002"), act("56100000", dt_to="2026-12-31", to_q="valid"),
        ])
        b = self.history.add(current_activities=[act("47191002")])
        run(b)
        signal = CompanySignal.objects.get()
        self.assertEqual((signal.signal_type, signal.rule_version), (KAD_REMOVED, "kad_removed:v1"))
        self.assertEqual((signal.effective_precision, signal.effective_date), (PRECISION_DATE, date(2026, 12, 31)))

    def test_a_removal_without_a_dated_end_has_no_source_time(self):
        self.history.add(current_activities=[act("47191002"), act("56100000")])
        b = self.history.add(current_activities=[act("47191002")])
        run(b)
        self.assertEqual(CompanySignal.objects.get().effective_precision, PRECISION_NONE)

    def test_scenario_9_type_wording_or_period_changes_of_a_present_kad_are_not_presence_changes(self):
        a = self.history.add(current_activities=[act("47191002", type_="primary", source="Κύρια", dt_from="2020-01-01")])
        b = self.history.add(current_activities=[
            act("47191002", type_="secondary", source="ΔΕΥΤΕΡΕΥΟΥΣΑ", dt_from="2024-02-02"),
        ])
        result = detect_snapshot_changes(a, b)
        self.assertTrue(result.state_changed)
        self.assertEqual(result.candidates, ())
        self.assertEqual(run(b).changed_snapshot_no_tier1_signal, 1)

    def test_scenario_10_two_additions_and_three_removals_are_five_signals(self):
        self.history.add(current_activities=[act("11111111"), act("22222222"), act("33333333"), act("44444444")])
        b = self.history.add(current_activities=[act("44444444"), act("55555555"), act("66666666")])
        report = run(b)
        self.assertEqual(types(), [KAD_ADDED] * 2 + [KAD_REMOVED] * 3)
        self.assertEqual((report.kad_additions, report.kad_removals, report.signals_created), (2, 3, 5))
        self.assertEqual(CompanySignalSnapshotEvidence.objects.count(), 5)

    def test_several_entries_of_one_identity_are_present_once(self):
        self.history.add()
        b = self.history.add(current_activities=[
            act("47191002"), act("62010000", type_="primary"), act("62010000", type_="secondary"),
        ])
        run(b)
        self.assertEqual(types(), [KAD_ADDED])

    def test_the_same_code_in_two_versions_is_two_identities(self):
        self.history.add(current_activities=[act("47191002", version="kad_2008", dt_to="2026-12-31", to_q="valid")])
        b = self.history.add(current_activities=[act("47191002", version="kad_2026", dt_from="2026-09-01")])
        run(b)
        self.assertEqual(types(), [KAD_ADDED, KAD_REMOVED])
        self.assertEqual(
            sorted(CompanySignalSnapshotEvidence.objects.values_list("kad_version", flat=True)), ["kad_2008", "kad_2026"],
        )

    def test_agreeing_dates_are_used_and_conflicting_dates_are_not(self):
        self.history.add()
        b = self.history.add(current_activities=[
            act("47191002"),
            act("62010000", type_="primary", dt_from="2026-08-01"),
            act("62010000", type_="secondary", dt_from="2026-08-01"),
            act("62010000", type_="auxiliary", from_q="missing"),  # an undated entry does not vote
            act("70220000", type_="primary", dt_from="2026-08-01"),
            act("70220000", type_="secondary", dt_from="2026-08-15"),
        ])
        run(b)
        by_code = {s.snapshot_evidence.activity_code: s for s in CompanySignal.objects.all()}
        self.assertEqual(by_code["62010000"].effective_date, date(2026, 8, 1))
        self.assertEqual((by_code["70220000"].effective_precision, by_code["70220000"].effective_date),
                         (PRECISION_NONE, None))

    def test_scenario_13_unknown_to_known_version_is_enrichment_not_remove_plus_add(self):
        a = self.history.add(current_activities=[act("47191002", version=None)])
        b = self.history.add(current_activities=[act("47191002", version="kad_2026")])
        result = detect_snapshot_changes(a, b)
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.version_quality_suppressed, 2)
        c = self.history.add(current_activities=[act("47191002", version=None)])
        self.assertEqual(detect_snapshot_changes(b, c).candidates, ())

    def test_a_genuinely_new_code_with_no_version_is_emitted_with_a_null_version(self):
        self.history.add()
        b = self.history.add(current_activities=[act("47191002"), act("62010000", version=None)])
        run(b)
        evidence = CompanySignalSnapshotEvidence.objects.get()
        self.assertEqual((evidence.activity_code, evidence.kad_version), ("62010000", None))


class TaxonomyMigrationTests(B5TestCase):
    def test_scenario_11_a_boundary_dated_2008_to_2026_migration_is_suppressed(self):
        a = self.history.add(current_activities=[
            act("47191002", version="kad_2008", dt_to=BOUNDARY, to_q="valid"),
            act("56101000", version="kad_2008", dt_to=BOUNDARY, to_q="valid"),
            act("62010000", version="kad_2008", dt_to=BOUNDARY, to_q="valid"),
        ])
        b = self.history.add(current_activities=[
            act("47191003", version="kad_2026", dt_from=BOUNDARY),
            act("56101100", version="kad_2026", dt_from=BOUNDARY),
        ])
        result = detect_snapshot_changes(a, b)
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.taxonomy_suppressed, 5)
        report = run(b)
        self.assertEqual((report.taxonomy_suppressed, report.changed_snapshot_no_tier1_signal), (5, 1))
        self.assertEqual(CompanySignal.objects.count(), 0)

    def test_scenario_12_migration_is_suppressed_while_a_genuine_later_addition_is_signalled(self):
        a = self.history.add(current_activities=[act("47191002", version="kad_2008", dt_to=BOUNDARY, to_q="valid")])
        b = self.history.add(current_activities=[
            act("47191003", version="kad_2026", dt_from=BOUNDARY),
            act("70220000", version="kad_2026", dt_from="2026-07-10"),
        ])
        result = detect_snapshot_changes(a, b)
        self.assertEqual(result.taxonomy_suppressed, 2)
        run(b)
        signal = CompanySignal.objects.get()
        self.assertEqual((signal.signal_type, signal.effective_date), (KAD_ADDED, date(2026, 7, 10)))
        self.assertEqual(signal.snapshot_evidence.activity_code, "70220000")

    def test_removals_after_the_boundary_are_suppressed_when_2026_boundary_codes_already_exist(self):
        # Observed before 2026-03-01 both taxonomies were current; afterwards only KAD 2026 remains.
        a = self.history.add(current_activities=[
            act("47191002", version="kad_2008", dt_to=BOUNDARY, to_q="valid"),
            act("47191003", version="kad_2026", dt_from=BOUNDARY),
        ])
        b = self.history.add(current_activities=[act("47191003", version="kad_2026", dt_from=BOUNDARY)])
        result = detect_snapshot_changes(a, b)
        self.assertEqual((result.candidates, result.taxonomy_suppressed), ((), 1))

    def test_a_boundary_dated_change_without_opposite_version_evidence_is_not_suppressed(self):
        a = self.history.add(current_activities=[
            act("47191002", version="kad_2008", dt_to=BOUNDARY, to_q="valid"), act("62010000", version="kad_2008"),
        ])
        b = self.history.add(current_activities=[act("62010000", version="kad_2008")])
        result = detect_snapshot_changes(a, b)
        self.assertEqual(result.taxonomy_suppressed, 0)
        self.assertEqual([c.signal_type for c in result.candidates], [KAD_REMOVED])

        c = self.history.add(current_activities=[act("62010000", version="kad_2008"),
                                                 act("47191003", version="kad_2026", dt_from=BOUNDARY)])
        result = detect_snapshot_changes(b, c)
        self.assertEqual(result.taxonomy_suppressed, 0)
        self.assertEqual([c.signal_type for c in result.candidates], [KAD_ADDED])

    def test_other_dates_are_never_suppressed_however_close_to_the_boundary(self):
        a = self.history.add(current_activities=[
            act("47191002", version="kad_2008", dt_to="2026-06-01", to_q="valid"),
            act("11111111", version="kad_2008", dt_to=BOUNDARY, to_q="valid"),
        ], observed_at=datetime(2026, 2, 27, 9, 0, tzinfo=dt_timezone.utc))
        b = self.history.add(current_activities=[
            act("70220000", version="kad_2026", dt_from="2026-07-10"),
            act("22222222", version="kad_2026", dt_from=BOUNDARY),
        ], observed_at=datetime(2026, 3, 2, 9, 0, tzinfo=dt_timezone.utc))
        result = detect_snapshot_changes(a, b)
        emitted = sorted((c.signal_type, c.activity_code) for c in result.candidates)
        self.assertEqual(emitted, [(KAD_ADDED, "70220000"), (KAD_REMOVED, "47191002")])
        self.assertEqual(result.taxonomy_suppressed, 2)

    def test_a_2026_addition_dated_on_the_boundary_needs_valid_quality(self):
        a = self.history.add(current_activities=[act("47191002", version="kad_2008", dt_to=BOUNDARY, to_q="valid")])
        b = self.history.add(current_activities=[
            act("47191003", version="kad_2026", from_q="invalid"),
            act("47191004", version="kad_2026", dt_from=BOUNDARY),
        ])
        emitted = [c.activity_code for c in detect_snapshot_changes(a, b).candidates]
        self.assertEqual(emitted, ["47191003"])

    def test_no_description_or_crosswalk_takes_part(self):
        source = inspect.getsource(b5)
        for forbidden in ("description", "difflib", "similar", "crosswalk_map", "fuzz"):
            self.assertNotIn(forbidden, source.split('"""', 2)[2])


# --- identity -----------------------------------------------------------------------------------

class EventIdentityTests(B5TestCase):
    def test_the_v1_event_key_structure_is_pinned(self):
        a = self.history.add()
        b = self.history.add(status_source_id="8", current_activities=[act("47191002"), act("62010000")])
        by_type = {c.signal_type: c for c in detect_snapshot_changes(a, b).candidates}
        transition = {"from_state": a.state_hash, "to_state": b.state_hash,
                      "observed_at": b.observed_at.astimezone(dt_timezone.utc).isoformat()}
        self.assertEqual(by_type[STATUS_CHANGED].event_key,
                         {"transition": transition, "subject": {"kind": "status", "before": "3", "after": "8"}})
        self.assertEqual(by_type[KAD_ADDED].event_key,
                         {"transition": transition, "subject": {"kind": "kad", "code": "62010000", "kad_version": "kad_2026"}})
        self.assertEqual(transition_anchor(a, b), transition)
        self.assertEqual(
            CompanySignal.objects.count(), 0,
        )

    def test_the_anchor_is_timezone_independent(self):
        a = self.history.add()
        b = self.history.add(status_source_id="8")
        athens = dt_timezone(timedelta(hours=3))
        shifted = CompanySnapshot(company=self.company, state_hash=b.state_hash, observed_at=b.observed_at.astimezone(athens))
        self.assertEqual(transition_anchor(a, shifted), transition_anchor(a, b))

    def test_rerunning_a_transition_reuses_its_signals_and_keeps_detected_time(self):
        self.history.add()
        b = self.history.add(status_source_id="8", current_activities=[act("47191002"), act("62010000")])
        first = run(b)
        before = list(CompanySignal.objects.values("id", "detected_at", "effective_date", "mode", "dedupe_key"))
        second = run(b)
        third = materialize_all_snapshot_change_signals()
        self.assertEqual((first.signals_created, second.signals_created, third.signals_created), (2, 0, 0))
        self.assertEqual((second.signals_reused, third.signals_reused), (2, 2))
        self.assertEqual(list(CompanySignal.objects.values("id", "detected_at", "effective_date", "mode", "dedupe_key")), before)
        self.assertEqual(CompanySignalSnapshotEvidence.objects.count(), 2)

    def test_scenario_14_a_repeated_transition_is_a_new_occurrence(self):
        a1 = self.history.add()
        b1 = self.history.add(status_source_id="8")
        a2 = self.history.add()
        b2 = self.history.add(status_source_id="8")
        self.assertEqual((a1.state_hash, b1.state_hash), (a2.state_hash, b2.state_hash))
        materialize_all_snapshot_change_signals()
        materialize_all_snapshot_change_signals()
        signals = CompanySignal.objects.order_by("detected_at")
        self.assertEqual(types(signals), [STATUS_CHANGED] * 3)  # A->B, B->A, A->B
        self.assertEqual([s.detected_at for s in signals], [b1.observed_at, a2.observed_at, b2.observed_at])
        self.assertEqual(len({s.dedupe_key for s in signals}), 3)
        pairs = [(e.previous_snapshot_id, e.current_snapshot_id)
                 for e in CompanySignalSnapshotEvidence.objects.order_by("current_snapshot__observed_at")]
        self.assertEqual(pairs, [(a1.pk, b1.pk), (b1.pk, a2.pk), (a2.pk, b2.pk)])

    def test_scenario_15_four_event_types_from_one_transition(self):
        self.history.add()
        b = self.history.add(status_source_id="8", legal_type_source_id="20", municipality_source_id="61191",
                             current_activities=[act("47191002"), act("62010000")])
        report = run(b)
        self.assertEqual(types(), sorted([STATUS_CHANGED, LEGAL_FORM_CHANGED, LOCATION_CHANGED, KAD_ADDED]))
        self.assertEqual(report.signals_created, 4)
        self.assertEqual(set(CompanySignalSnapshotEvidence.objects.values_list("current_snapshot_id", flat=True)), {b.pk})

    def test_detected_time_is_the_current_observation_never_now(self):
        self.history.add()
        b = self.history.add(legal_type_source_id="20")
        with patch("django.utils.timezone.now", return_value=datetime(2030, 1, 1, tzinfo=dt_timezone.utc)):
            run(b)
        self.assertEqual(CompanySignal.objects.get().detected_at, b.observed_at)


# --- evidence and conflicts ---------------------------------------------------------------------

class EvidenceTests(B5TestCase):
    def test_cited_snapshots_cannot_be_deleted_and_evidence_follows_its_signal(self):
        a = self.history.add()
        b = self.history.add(status_source_id="8")
        run(b)
        for snapshot in (a, b):
            with self.assertRaises(ProtectedError):
                snapshot.delete()
        CompanySignal.objects.get().delete()
        self.assertEqual(CompanySignalSnapshotEvidence.objects.count(), 0)

    def test_the_database_refuses_an_inconsistent_subject(self):
        a = self.history.add()
        b = self.history.add(status_source_id="8")
        run(b)
        signal = CompanySignal.objects.get()
        signal.snapshot_evidence.delete()
        for bad in ({"subject_kind": "kad", "before_source_id": None, "after_source_id": None},
                    {"subject_kind": "status", "before_source_id": "3", "after_source_id": None},
                    {"subject_kind": "status", "before_source_id": "3", "after_source_id": "8", "activity_code": "1"}):
            with self.subTest(bad=bad), self.assertRaises(IntegrityError), transaction.atomic():
                CompanySignalSnapshotEvidence.objects.create(signal=signal, previous_snapshot=a, current_snapshot=b, **bad)

    def test_evidence_pointing_at_another_pair_is_a_conflict_and_is_never_replaced(self):
        a = self.history.add()
        b = self.history.add(status_source_id="8")
        run(b)
        evidence = CompanySignalSnapshotEvidence.objects.get()
        c = self.history.add(status_source_id="8", city="ΑΛΛΗ")
        CompanySignalSnapshotEvidence.objects.filter(pk=evidence.pk).update(current_snapshot=c)
        report = run(b)
        self.assertEqual((report.conflicts, report.conflicting_gemi_numbers), (1, [GEMI]))
        evidence.refresh_from_db()
        self.assertEqual(evidence.current_snapshot_id, c.pk)
        with self.assertRaises(SnapshotEvidenceConflictError):
            b5._record_candidate(self.company, a, b, detect_snapshot_changes(a, b).candidates[0])

    def test_a_signal_identity_conflict_is_counted_and_other_companies_continue(self):
        self.history.add()
        b = self.history.add(status_source_id="8", last_status_change="2026-08-20")
        candidate = detect_snapshot_changes(immediate_predecessor(b), b).candidates[0]
        record_company_signal(company=self.company, signal_type=STATUS_CHANGED, source_type=SNAPSHOT_DIFF,
                              event_key=candidate.event_key, effective=date(2020, 1, 1), detected_at=b.observed_at)
        other = History(make_company("100000000009"))
        other.add()
        other.add(legal_type_source_id="20")
        report = materialize_all_snapshot_change_signals()
        self.assertEqual(report.conflicts, 1)
        self.assertEqual(CompanySignal.objects.get(signal_type=STATUS_CHANGED).effective_date, date(2020, 1, 1))
        self.assertTrue(CompanySignal.objects.filter(signal_type=LEGAL_FORM_CHANGED).exists())

    def test_systemic_failures_are_not_swallowed(self):
        self.history.add()
        b = self.history.add(status_source_id="8")
        with patch("gemiapp.snapshot_change_signals.record_company_signal", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                run(b)


# --- batch command, dry run ---------------------------------------------------------------------

class BatchTests(TestCase):
    def build(self):
        for index in range(3):
            history = History(make_company(f"20000000000{index}"))
            history.add()
            history.add(status_source_id="8", current_activities=[act("47191002"), act("62010000")])
            history.add(status_source_id="8", current_activities=[act("47191002"), act("62010000")], postal_code="1")

    def test_dry_run_detects_everything_and_writes_nothing(self):
        self.build()
        before = list(CompanySnapshot.objects.values())
        report = materialize_all_snapshot_change_signals(dry_run=True, batch_size=2)
        self.assertEqual((report.snapshots_inspected, report.transitions_inspected, report.baselines_skipped), (9, 6, 3))
        self.assertEqual((report.status_changes, report.kad_additions, report.signals_created), (3, 3, 6))
        self.assertEqual(report.changed_snapshot_no_tier1_signal, 3)
        self.assertEqual((CompanySignal.objects.count(), CompanySignalSnapshotEvidence.objects.count()), (0, 0))
        self.assertEqual(list(CompanySnapshot.objects.values()), before)
        again = materialize_all_snapshot_change_signals(dry_run=True)
        self.assertEqual(again.summary() | {"batches": 0, "last_snapshot_id": None},
                         report.summary() | {"batches": 0, "last_snapshot_id": None})

    def test_a_real_run_then_a_second_run_creates_nothing_more(self):
        self.build()
        first = materialize_all_snapshot_change_signals(batch_size=4)
        second = materialize_all_snapshot_change_signals(batch_size=4)
        self.assertEqual((first.signals_created, second.signals_created, second.signals_reused), (6, 0, 6))
        self.assertEqual(CompanySignal.objects.count(), 6)
        dry = materialize_all_snapshot_change_signals(dry_run=True)
        self.assertEqual((dry.signals_created, dry.signals_reused), (0, 6))

    def test_the_run_is_resumable_from_a_snapshot_id(self):
        self.build()
        ids = list(CompanySnapshot.objects.order_by("pk").values_list("pk", flat=True))
        report = materialize_all_snapshot_change_signals(start_snapshot_id=ids[4])
        self.assertEqual(report.snapshots_inspected, 4)
        self.assertEqual(report.last_snapshot_id, ids[-1])

    def test_the_command_reports_counts_and_no_names(self):
        self.build()
        out = StringIO()
        call_command("materialize_snapshot_change_signals", "--dry-run", stdout=out)
        text = out.getvalue()
        self.assertIn("transitions=6", text)
        self.assertIn("mode=shadow (always)", text)
        self.assertNotIn("ΕΤΑΙΡΕΙΑ", text)
        self.assertEqual(CompanySignal.objects.count(), 0)


# --- privacy, integration and parity ------------------------------------------------------------

class RealSnapshotIntegrationTests(TestCase):
    """End to end through A3 and the B3 writer, with sentinel personal data in the source records."""

    def test_signals_come_from_real_b3_snapshots_and_carry_no_personal_data(self):
        company = make_company()
        first = full_item(GEMI, "2026-09-14")
        second = full_item(GEMI, "2026-09-14", status={"id": 8, "descr": "Διαγραφή"},
                           municipality={"id": 61191, "descr": "ΑΜΑΡΟΥΣΙΟΥ"})
        record_company_snapshot(company, normalize_company(first, as_of=date(2026, 9, 15)), T0)
        later = T0 + timedelta(days=3)
        result = record_company_snapshot(company, normalize_company(second, as_of=date(2026, 9, 18)), later)
        report = run(result.snapshot)
        self.assertEqual(types(), [LOCATION_CHANGED, STATUS_CHANGED])
        self.assertEqual(report.signals_created, 2)
        blob = json.dumps(list(CompanySignalSnapshotEvidence.objects.values()), default=str, ensure_ascii=False)
        blob += json.dumps(list(CompanySignal.objects.values()), default=str, ensure_ascii=False)
        for sentinel in (PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, "099999999", "ΟΔΟΣ ΔΟΚΙΜΗΣ",
                         "ΔΟΚΙΜΑΣΤΙΚΗ", "ΕΜΠΟΡΙΟ", "ΛΙΑΝΙΚΟ ΕΜΠΟΡΙΟ", "Διαγραφή", "ΑΜΑΡΟΥΣΙΟΥ", "ΕΤΑΙΡΕΙΑ ΙΚΕ"):
            self.assertNotIn(sentinel, blob)

    def test_logs_carry_no_personal_data(self):
        company = make_company()
        record_company_snapshot(company, normalize_company(full_item(GEMI, "2026-09-14"), as_of=date(2026, 9, 15)), T0)
        result = record_company_snapshot(
            company, normalize_company(full_item(GEMI, "2026-09-14", legalType={"id": 20, "descr": "ΕΠΕ"}),
                                      as_of=date(2026, 9, 18)), T0 + timedelta(days=3))
        with self.assertLogs("gemiapp", level="INFO") as logs:
            run(result.snapshot)
        blob = "\n".join(logs.output)
        for sentinel in (PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, "ΔΟΚΙΜΑΣΤΙΚΗ", "ΕΠΕ"):
            self.assertNotIn(sentinel, blob)


class CustomerParityTests(TestCase):
    def world(self):
        return {
            "companies": list(Company.objects.values()),
            "activities": list(CompanyActivity.objects.values()),
            "radars": list(CustomerRadar.objects.values()),
            "matches": list(RadarMatch.objects.values()),
            "leads": list(UserCompanyLead.objects.values()),
            "subscriptions": list(UserSubscription.objects.values()),
            "monitoring": list(CompanyMonitoring.objects.values()),
            "snapshots": list(CompanySnapshot.objects.values()),
        }

    def test_materialising_change_signals_changes_nothing_a_customer_can_see(self):
        user = entitled_user()
        radar = radar_for(user, "parity", prefectures=["ΑΤΤΙΚΗΣ"])
        history = History(make_company())
        history.add()
        b = history.add(status_source_id="8", legal_type_source_id="20", municipality_source_id="61191",
                        current_activities=[act("47191002"), act("62010000")])
        company = Company.objects.get()
        before, matched = self.world(), company_matches_radar(company, radar)
        materialize_all_snapshot_change_signals()
        self.assertEqual(CompanySignal.objects.count(), 4)
        self.assertEqual(self.world(), before)
        self.assertEqual(company_matches_radar(Company.objects.get(), radar), matched)
