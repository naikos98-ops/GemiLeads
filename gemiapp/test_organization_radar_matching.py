"""Tests for the Organization Radar matching engine (C5, gemiapp.organization_radar_matching).

Fixtures only: reference rows, snapshots and signals are created locally, nothing calls GEMI. Matching is read-only
and persists nothing; CustomerRadar stays the production matcher.
"""

import dataclasses
import inspect
import itertools
from datetime import date, datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.signing import TimestampSigner
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from . import organization_radar_matching as c5
from .company_contact import extract_company_contact_phones
from .company_signals import (
    DISCOVERY, LIVE, SHADOW, SNAPSHOT_DIFF, record_company_signal,
)
from .industry_templates import create_industry_template
from .ingestion.monitoring import radar_match_evidence
from .ingestion.refresh import RefreshPolicy, build_company_refresh_plan
from .models import (
    Company, CompanyMonitoring, CompanySignal, CompanySignalSnapshotEvidence, CompanySnapshot, CustomerRadar,
    DigestDelivery, GemiKad, OrganizationRadar, RadarMatch, UserCompanyLead, UserSubscription,
)
from .organization_icp import EXCLUDE, INCLUDE, ICPCriteria, KadCriterion, RegionCriterion, create_organization_icp
from .organization_radar_matching import (
    INSUFFICIENT_STATE, MATCH, MISSING_EVIDENCE, NO_MATCH, NO_SNAPSHOT, ORGANIZATION_RADAR_MATCH_RULE_VERSION,
    SNAPSHOT_AT_DETECTION, SNAPSHOT_EVIDENCE, UNSUPPORTED_SNAPSHOT_SCHEMA, UNSUPPORTED_SOURCE, MatchContext,
    MatchedOrganizationRadar, MatchingError, build_match_context, candidate_radar_ids, evaluate_organization_radar,
    explain_organization_radar_matches, find_matching_organization_radars, load_radar_definitions,
    summarize_organization_radar_matching,
)
from .organization_radars import (
    RadarDefinition, RadarError, create_organization_radar, get_organization_radar_definition,
)
from .services import company_matches_radar, eligible_radars, send_digests
from .test_gemi_company_activities import entitled_user, radar_for
from .test_gemi_validation import CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL
from .test_organization_icp import Refs, ref
from .test_organization_radars import organization

T0 = datetime(2026, 9, 10, 9, tzinfo=dt_timezone.utc)
ADDRESS_SENTINEL = "ΟΔΟΣ ΦΡΟΥΡΟΣ 4821"
PRIVATE = (PHONE_SENTINEL, CONTACT_SENTINEL, PERSON_SENTINEL, ADDRESS_SENTINEL, "099994821")
_keys = itertools.count(1)
C8_PERSISTENCE = ("Opportunity", "OpportunitySignal", "OpportunityScoreComponent",
                  "OpportunityScoreEvidence",  # C8 owns these; earlier packages must add none
                  "OpportunityNote",  # D33's user-authored notes: workflow data, never written by this engine
                  "OpportunityTask")  # D34's tasks: workflow data, never written by this engine


def make_company(gemi_number="400100"):
    return Company.objects.create(
        gemi_number=gemi_number, name=f"{PERSON_SENTINEL} ΙΚΕ", incorporation_date=date(2026, 9, 1),
        prefecture="ΑΤΤΙΚΗΣ", email=CONTACT_SENTINEL, address=ADDRESS_SENTINEL, vat_number="099994821",
        raw_data={"phone": PHONE_SENTINEL, "email": CONTACT_SENTINEL, "persons": [{"personName": PERSON_SENTINEL}],
                  "street": ADDRESS_SENTINEL},
    )


def snapshot(company, at, *, kads=(("62010000", "kad_2026"),), unknown=0, prefecture="5", municipality="61190",
             legal="19", schema=1):
    return CompanySnapshot.objects.create(
        company=company, schema_version=schema, normalizer_version=1, state_hash=f"{next(_keys):064d}",
        observed_at=at, last_observed_at=at, is_baseline=not company.snapshots.exists(), status_source_id="3",
        last_status_change_quality="missing", legal_type_source_id=legal, prefecture_source_id=prefecture,
        municipality_source_id=municipality, city=ADDRESS_SENTINEL, postal_code="14561",
        incorporation_date_quality="valid", incorporation_date=date(2026, 9, 1),
        activities_state=[{"code": code, "kad_version": version, "activity_type": "primary"} for code, version in kads],
        unknown_current_activity_count=unknown,
    )


def new_company_signal(company, at, mode=SHADOW):
    signal, _ = record_company_signal(company=company, signal_type="new_company", source_type=DISCOVERY,
                                      event_key={"n": next(_keys)}, detected_at=at, mode=mode)
    return signal


def diff_signal(company, previous, current, at, signal_type="status_changed", mode=SHADOW):
    signal, _ = record_company_signal(company=company, signal_type=signal_type, source_type=SNAPSHOT_DIFF,
                                      event_key={"n": next(_keys)}, detected_at=at, mode=mode)
    CompanySignalSnapshotEvidence.objects.create(signal=signal, previous_snapshot=previous, current_snapshot=current,
                                                 subject_kind="status", before_source_id="3", after_source_id="8")
    return signal


def context(signal_type="new_company", *, status=SNAPSHOT_AT_DETECTION, kads=frozenset({("62010000", "kad_2026")}),
            unknown=0, prefecture="5", municipality="61190", legal="19", source=DISCOVERY):
    stateful = status in (SNAPSHOT_AT_DETECTION, SNAPSHOT_EVIDENCE)
    return MatchContext(
        signal_id=1, company_id=1, signal_type=signal_type, source_type=source, detected_at=T0, context_status=status,
        state_snapshot_id=1 if stateful else None, kads=kads if stateful else frozenset(),
        indeterminate_activity_count=unknown if stateful else 0,
        prefecture_source_id=prefecture if stateful else None, municipality_source_id=municipality if stateful else None,
        legal_type_source_id=legal if stateful else None,
    )


class MatchingTestCase(TestCase):
    def setUp(self):
        self.r = Refs()
        self.org = organization()
        self.company = make_company()

    def radar(self, org=None, **values):
        values.setdefault("name", "Radar")
        values.setdefault("active", True)
        return create_organization_radar(org or self.org, RadarDefinition(**values))

    def matched(self, signal):
        return [m.radar_id for m in find_matching_organization_radars(signal)]

    def status_of(self, signal, radar):
        definition = get_organization_radar_definition(radar.organization, radar)
        return evaluate_organization_radar(definition, build_match_context(signal))


# --- architecture ----------------------------------------------------------------------------------

class ArchitectureTests(TestCase):
    def test_matching_persists_nothing_of_its_own(self):
        from django.apps import apps

        names = {model.__name__ for model in apps.get_app_config("gemiapp").get_models()}
        # No match table was ever created: matching stays a read/evaluation engine.
        self.assertFalse({n for n in names if "Match" in n} - {"RadarMatch"})
        # Opportunity persistence belongs to C8 alone, and C5 neither defines nor writes it.
        self.assertEqual({n for n in names if "Opportunit" in n}, set(C8_PERSISTENCE))
        code = inspect.getsource(c5).split('"""', 2)[2]  # the docstring may discuss §29; the code may not touch it
        self.assertNotIn("models.Model", code)
        self.assertNotIn("Opportunit", code)
        loader = MigrationLoader(None, ignore_no_migrations=True)
        for (app, name), migration in loader.disk_migrations.items():
            if app == "gemiapp":
                self.assertNotIn("organization_radar_matching", str(migration.operations), name)

    def test_rule_version_is_pinned_and_results_are_immutable(self):
        self.assertEqual(ORGANIZATION_RADAR_MATCH_RULE_VERSION, "organization_radar_match:v1")
        for cls in (MatchContext, c5.RadarEvaluation, MatchedOrganizationRadar, c5.SignalMatchReport):
            self.assertTrue(dataclasses.is_dataclass(cls) and cls.__dataclass_params__.frozen, cls)
        self.assertEqual(
            [f.name for f in dataclasses.fields(MatchedOrganizationRadar)],
            ["organization_id", "radar_id", "signal_id", "company_id", "signal_type", "match_rule_version",
             "state_snapshot_id"],
        )
        context_fields = {f.name for f in dataclasses.fields(MatchContext)}
        for forbidden in ("name", "phone", "email", "raw_data", "persons", "address", "city", "postal_code", "vat",
                          "user", "subscription", "score"):
            self.assertFalse([f for f in context_fields if forbidden in f], forbidden)

    def test_the_module_reads_no_legacy_icp_template_contact_or_network_code(self):
        code = inspect.getsource(c5).split('"""', 2)[2]
        for forbidden in ("CustomerRadar", "RadarMatch", "UserCompanyLead", "CompanyMonitoring", "CompanyActivity",
                          "is_active", "raw_data", "gemi_phones", "phone", "email", "persons", "OrganizationICP",
                          "organization_icp", "IndustryTemplate", "industry_template", "score_threshold >",
                          "get_gemi_client", "stripe", "send_mail", "urllib", "requests", "request.user", "session",
                          ".save(", ".create(", ".delete(", "bulk_", "update(", "select_for_update"):
            self.assertNotIn(forbidden, code, forbidden)

    def test_nothing_in_the_product_calls_the_matcher(self):
        for path in ("gemiapp/urls.py", "gemiapp/views.py", "config/urls.py", "gemiapp/tasks.py", "gemiapp/apps.py",
                     "gemiapp/services.py", "gemiapp/billing.py", "gemiapp/forms.py", "gemiapp/ingestion/monitoring.py",
                     "gemiapp/ingestion/refresh.py", "gemiapp/company_timeline.py", "gemiapp/snapshot_change_signals.py",
                     "gemiapp/organization_radars.py", "gemiapp/organization_icp.py", "gemiapp/industry_templates.py",
                     "config/settings.py", "render.yaml"):
            self.assertNotIn("radar_matching", open(path, encoding="utf-8").read(), path)


# --- fixture matrix ------------------------------------------------------------------------------

class FixtureMatrixTests(MatchingTestCase):
    def test_01_signal_type_only_radar_matches_new_company_without_snapshot(self):
        radar = self.radar(signal_types=("new_company",))
        signal = new_company_signal(self.company, T0)
        result = find_matching_organization_radars(signal)
        self.assertEqual(result, (MatchedOrganizationRadar(
            organization_id=self.org.pk, radar_id=radar.pk, signal_id=signal.pk, company_id=self.company.pk,
            signal_type="new_company", match_rule_version=ORGANIZATION_RADAR_MATCH_RULE_VERSION, state_snapshot_id=None,
        ),))
        self.assertEqual(build_match_context(signal).context_status, NO_SNAPSHOT)

    def test_02_inactive_radar_is_no_candidate_and_no_match_and_is_not_mutated(self):
        radar = self.radar(signal_types=("new_company",), active=False)
        before = OrganizationRadar.objects.filter(pk=radar.pk).values().get()
        signal = new_company_signal(self.company, T0)
        self.assertNotIn(radar.pk, candidate_radar_ids(build_match_context(signal)))
        self.assertEqual(self.matched(signal), [])
        self.assertEqual(self.status_of(signal, radar).status, NO_MATCH)
        self.assertEqual(OrganizationRadar.objects.filter(pk=radar.pk).values().get(), before)

    def test_03_exact_kad_identity_matches(self):
        radar = self.radar(kads=(self.r.kad_2026,))
        snap = snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0 + timedelta(hours=1))
        self.assertEqual(self.matched(signal), [radar.pk])
        self.assertEqual(find_matching_organization_radars(signal)[0].state_snapshot_id, snap.pk)

    def test_04_kad_version_mismatch_is_no_match(self):
        radar = self.radar(kads=(self.r.kad_2008,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        self.assertEqual(self.matched(signal), [])
        evaluation = self.status_of(signal, radar)
        self.assertEqual((evaluation.status, evaluation.failed_dimensions), (NO_MATCH, ("kad",)))

    def test_05_kad_criteria_are_or(self):
        radar = self.radar(kads=(self.r.kad_other, self.r.kad_2026))
        snapshot(self.company, T0)
        self.assertEqual(self.matched(new_company_signal(self.company, T0)), [radar.pk])

    def test_06_dimensions_are_and(self):
        radar = self.radar(kads=(self.r.kad_2026,), regions=(self.r.attica,), legal_forms=(self.r.ike,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        self.assertEqual(self.matched(signal), [radar.pk])
        self.assertEqual(self.status_of(signal, radar).matched_dimensions, ("kad", "region", "legal_form"))

    def test_07_one_failed_dimension_is_no_match(self):
        radar = self.radar(kads=(self.r.kad_2026,), regions=(self.r.attica,), legal_forms=(self.r.oe,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        self.assertEqual(self.matched(signal), [])
        self.assertEqual(self.status_of(signal, radar).failed_dimensions, ("legal_form",))

    def test_08_unrestricted_dimension_accepts_unknown_facts_but_a_required_one_does_not(self):
        kad_only = self.radar(kads=(self.r.kad_2026,))
        needs_form = self.radar(kads=(self.r.kad_2026,), legal_forms=(self.r.ike,))
        snapshot(self.company, T0, legal=None)
        signal = new_company_signal(self.company, T0)
        self.assertEqual(self.matched(signal), [kad_only.pk])
        evaluation = self.status_of(signal, needs_form)
        self.assertEqual((evaluation.status, evaluation.unknown_dimensions), (INSUFFICIENT_STATE, ("legal_form",)))
        self.assertIn(needs_form.pk, candidate_radar_ids(build_match_context(signal)))

    def test_09_state_criteria_without_snapshot_are_insufficient_not_no_match(self):
        radar = self.radar(kads=(self.r.kad_2026,))
        signal = new_company_signal(self.company, T0)
        report = explain_organization_radar_matches(signal)
        self.assertEqual(report.matches, ())
        self.assertEqual([(e.radar_id, e.evaluation.status) for e in report.evaluations],
                         [(radar.pk, INSUFFICIENT_STATE)])
        self.assertEqual(report.count(INSUFFICIENT_STATE), 1)

    def test_10_snapshot_diff_uses_the_evidence_snapshot_not_the_latest(self):
        previous = snapshot(self.company, T0, legal="3")
        current = snapshot(self.company, T0 + timedelta(days=1))
        snapshot(self.company, T0 + timedelta(days=2), kads=(("47191002", "kad_2026"),), prefecture="61190",
                 municipality=None)
        signal = diff_signal(self.company, previous, current, T0 + timedelta(days=3))
        kad_2026 = self.radar(kads=(self.r.kad_2026,), regions=(self.r.attica,))
        latest_only = self.radar(kads=(self.r.kad_other,))
        ctx = build_match_context(signal)
        self.assertEqual((ctx.context_status, ctx.state_snapshot_id), (SNAPSHOT_EVIDENCE, current.pk))
        self.assertEqual(self.matched(signal), [kad_2026.pk])
        self.assertEqual(self.status_of(signal, latest_only).status, NO_MATCH)

    def test_10b_new_company_uses_the_latest_snapshot_at_detection_never_a_later_one(self):
        earlier = snapshot(self.company, T0, kads=(("47191002", "kad_2026"),))
        snapshot(self.company, T0 + timedelta(days=2))
        signal = new_company_signal(self.company, T0 + timedelta(days=1))
        self.radar(kads=(self.r.kad_2026,))
        other = self.radar(kads=(self.r.kad_other,))
        self.assertEqual(build_match_context(signal).state_snapshot_id, earlier.pk)
        self.assertEqual(self.matched(signal), [other.pk])

    def test_11_municipality_exclusion_vetoes_an_included_prefecture(self):
        radar = self.radar(regions=(self.r.attica,), exclusions=(self.r.kifisia,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        self.assertEqual(self.matched(signal), [])
        self.assertNotIn(radar.pk, candidate_radar_ids(build_match_context(signal)))
        evaluation = self.status_of(signal, radar)
        self.assertEqual((evaluation.status, evaluation.matched_dimensions, evaluation.exclusion_hit),
                         (NO_MATCH, ("region",), "municipality"))

    def test_12_kad_exclusion_vetoes_and_version_exclusions_do_not_collide(self):
        vetoed = self.radar(regions=(self.r.attica,), exclusions=(self.r.kad_2026,))
        other_version = self.radar(kads=(self.r.kad_2026,), exclusions=(self.r.kad_2008,))
        legal_vetoed = self.radar(kads=(self.r.kad_2026,), exclusions=(self.r.ike,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        self.assertEqual(self.matched(signal), [other_version.pk])
        self.assertEqual(self.status_of(signal, vetoed).exclusion_hit, "kad")
        self.assertEqual(self.status_of(signal, legal_vetoed).exclusion_hit, "legal_form")

    def test_13_no_signal_criteria_means_any_implemented_signal_type(self):
        radar = self.radar(kads=(self.r.kad_2026,))
        previous = snapshot(self.company, T0, legal="3")
        current = snapshot(self.company, T0 + timedelta(days=1))
        for signal_type in ("status_changed", "kad_added", "legal_form_changed", "location_changed"):
            signal = diff_signal(self.company, previous, current, T0 + timedelta(days=2), signal_type=signal_type)
            self.assertEqual(self.matched(signal), [radar.pk], signal_type)
        definition = get_organization_radar_definition(self.org, radar)
        taxonomy_only = evaluate_organization_radar(definition, context("capital_increase", source="document_metadata"))
        self.assertEqual((taxonomy_only.status, taxonomy_only.failed_dimensions), (NO_MATCH, ("signal_type",)))
        listed = self.radar(kads=(self.r.kad_2026,), signal_types=("kad_added",))
        self.assertEqual(evaluate_organization_radar(get_organization_radar_definition(self.org, listed),
                                                     context("status_changed")).status, NO_MATCH)

    def test_14_score_threshold_is_ignored(self):
        unset = self.radar(kads=(self.r.kad_2026,), score_threshold=None)
        strict = self.radar(kads=(self.r.kad_2026,), score_threshold=90)
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        self.assertEqual(self.matched(signal), sorted([unset.pk, strict.pk]))
        self.assertEqual(self.status_of(signal, unset), self.status_of(signal, strict))

    def test_15_the_icp_never_narrows_a_radar(self):
        radar = self.radar(kads=(self.r.kad_2026,), regions=(self.r.attica,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        before = find_matching_organization_radars(signal)
        create_organization_icp(self.org, ICPCriteria(
            kads=(KadCriterion(self.r.kad_2026, EXCLUDE), KadCriterion(self.r.kad_other, INCLUDE)),
            regions=(RegionCriterion(self.r.thessaloniki, INCLUDE), RegionCriterion(self.r.attica, EXCLUDE)),
            signal_types=("kad_removed",),
        ))
        self.assertEqual(find_matching_organization_radars(signal), before)
        self.assertEqual([m.radar_id for m in before], [radar.pk])

    def test_16_industry_templates_are_ignored(self):
        radar = self.radar(kads=(self.r.kad_2026,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        before = explain_organization_radar_matches(signal)
        create_industry_template(slug="retail", name="Λιανική", kads=(self.r.kad_other,), active=True)
        self.assertEqual(explain_organization_radar_matches(signal), before)
        self.assertEqual([m.radar_id for m in before.matches], [radar.pk])

    def test_17_organizations_are_matched_only_by_their_own_radars(self):
        other_org = organization("Other Supplier", "other@example.com")
        mine = self.radar(kads=(self.r.kad_2026,))
        theirs = self.radar(other_org, kads=(self.r.kad_other,))
        shared_type = self.radar(other_org, signal_types=("new_company",))
        snapshot(self.company, T0)
        result = find_matching_organization_radars(new_company_signal(self.company, T0))
        self.assertEqual(sorted((m.organization_id, m.radar_id) for m in result),
                         sorted([(self.org.pk, mine.pk), (other_org.pk, shared_type.pk)]))
        self.assertNotIn(theirs.pk, [m.radar_id for m in result])


# --- state rules ---------------------------------------------------------------------------------

class CompanyStateTests(MatchingTestCase):
    def test_missing_or_foreign_evidence_yields_no_state(self):
        other = make_company("400200")
        previous, current = snapshot(other, T0), snapshot(other, T0 + timedelta(days=1))
        foreign = diff_signal(self.company, previous, current, T0 + timedelta(days=2))
        orphan, _ = record_company_signal(company=self.company, signal_type="kad_added", source_type=SNAPSHOT_DIFF,
                                          event_key={"orphan": 1}, detected_at=T0)
        signal_only = self.radar(signal_types=("status_changed", "kad_added"))
        needs_kad = self.radar(kads=(self.r.kad_2026,))
        for signal in (foreign, orphan):
            ctx = build_match_context(signal)
            self.assertEqual((ctx.context_status, ctx.state_snapshot_id, ctx.kads), (MISSING_EVIDENCE, None, frozenset()))
            self.assertEqual(self.matched(signal), [signal_only.pk])
            self.assertEqual(self.status_of(signal, needs_kad).status, INSUFFICIENT_STATE)

    def test_a_future_source_type_does_not_guess_state_or_crash_a_batch(self):
        snapshot(self.company, T0)
        signal_only = self.radar(signal_types=("new_company",))
        needs_kad = self.radar(kads=(self.r.kad_2026,))
        future = CompanySignal.objects.create(
            company=self.company, signal_type="new_company", source_type="document_metadata", rule_version="future:v1",
            dedupe_key="f" * 64, confidence="0.5", mode=SHADOW, effective_precision="none", detected_at=T0)
        self.assertEqual(build_match_context(future).context_status, UNSUPPORTED_SOURCE)
        self.assertEqual(self.matched(future), [signal_only.pk])
        self.assertEqual(self.status_of(future, needs_kad).status, INSUFFICIENT_STATE)
        new_company_signal(self.company, T0 + timedelta(hours=1))
        summary = summarize_organization_radar_matching(mode=SHADOW)
        self.assertEqual((summary.signals, summary.matches, summary.insufficient_state), (2, 3, 1))
        self.assertEqual(dict(summary.context_statuses), {SNAPSHOT_AT_DETECTION: 1, UNSUPPORTED_SOURCE: 1})

    def test_an_unknown_snapshot_schema_is_not_interpreted(self):
        snapshot(self.company, T0, schema=2)
        needs_kad = self.radar(kads=(self.r.kad_2026,))
        signal = new_company_signal(self.company, T0)
        self.assertEqual(build_match_context(signal).context_status, UNSUPPORTED_SNAPSHOT_SCHEMA)
        self.assertEqual(self.status_of(signal, needs_kad).status, INSUFFICIENT_STATE)

    def test_legacy_company_fields_never_supply_state(self):
        self.company.is_active = False
        self.company.prefecture = "ΘΕΣΣΑΛΟΝΙΚΗΣ"
        self.company.save()
        snapshot(self.company, T0)
        radar = self.radar(regions=(self.r.attica,))
        self.assertEqual(self.matched(new_company_signal(self.company, T0)), [radar.pk])

    def test_indeterminate_activities_and_unpublished_versions_are_unknown(self):
        radar = self.radar(kads=(self.r.kad_2026,))
        definition = get_organization_radar_definition(self.org, radar)
        self.assertEqual(evaluate_organization_radar(definition, context(kads=frozenset(), unknown=1)).status,
                         INSUFFICIENT_STATE)
        self.assertEqual(evaluate_organization_radar(definition, context(kads=frozenset())).status, NO_MATCH)
        self.assertEqual(evaluate_organization_radar(definition, context(kads=frozenset({("62010000", None)}))).status,
                         INSUFFICIENT_STATE)
        blank = ref(GemiKad, "62010000", kad_version="")
        unpublished = self.radar(kads=(blank,))
        self.assertEqual(evaluate_organization_radar(get_organization_radar_definition(self.org, unpublished),
                                                     context()).status, INSUFFICIENT_STATE)
        # A confirmed identity still matches while other activities are indeterminate.
        self.assertEqual(evaluate_organization_radar(definition, context(unknown=3)).status, MATCH)

    def test_regions_compare_exact_source_ids_per_level_without_inference(self):
        prefecture = self.radar(regions=(self.r.thessaloniki,))  # source id 61190, like the municipality
        municipality = self.radar(regions=(self.r.kifisia,))
        snapshot(self.company, T0, prefecture="5", municipality="61190")
        signal = new_company_signal(self.company, T0)
        self.assertEqual(self.matched(signal), [municipality.pk])
        self.assertEqual(self.status_of(signal, prefecture).status, NO_MATCH)
        unknown_municipality = get_organization_radar_definition(self.org, municipality)
        self.assertEqual(evaluate_organization_radar(unknown_municipality, context(municipality=None)).status,
                         INSUFFICIENT_STATE)

    def test_an_exclusion_that_cannot_be_evaluated_is_not_a_veto_and_not_a_match(self):
        radar = self.radar(signal_types=("new_company",), exclusions=(self.r.kifisia,))
        definition = get_organization_radar_definition(self.org, radar)
        evaluation = evaluate_organization_radar(definition, context(status=NO_SNAPSHOT))
        self.assertEqual((evaluation.status, evaluation.exclusion_hit, evaluation.unknown_exclusions),
                         (INSUFFICIENT_STATE, None, ("municipality",)))
        self.assertEqual(evaluate_organization_radar(definition, context(municipality="1")).status, MATCH)

    def test_input_must_be_a_persisted_signal(self):
        for bad in (None, {"company_id": 1, "signal_type": "new_company"}, CompanySignal(company=self.company),
                    CustomerRadar()):
            with self.assertRaises(MatchingError):
                find_matching_organization_radars(bad)
        signal = new_company_signal(self.company, T0)
        stale = CompanySignal(pk=signal.pk + 999)
        with self.assertRaises(MatchingError):
            find_matching_organization_radars(stale)
        with self.assertRaises(MatchingError):
            summarize_organization_radar_matching(mode="all")

    def test_shadow_and_live_signals_match_identically(self):
        radar = self.radar(signal_types=("new_company",))
        shadow = new_company_signal(self.company, T0)
        live = new_company_signal(make_company("400300"), T0, mode=LIVE)
        self.assertEqual(self.matched(shadow), self.matched(live))
        self.assertEqual(self.matched(live), [radar.pk])
        self.assertEqual(summarize_organization_radar_matching(mode=LIVE).signals, 1)


# --- candidate selection -------------------------------------------------------------------------

class CandidateSelectionTests(MatchingTestCase):
    def test_candidate_selection_is_never_narrower_than_the_evaluator(self):
        r = self.r
        blank = ref(GemiKad, "62010000", kad_version="")
        grid = itertools.product(
            [(), (r.kad_2026,), (r.kad_2008,), (r.kad_other,), (r.kad_2026, r.kad_other), (blank,)],
            [(), (r.attica,), (r.thessaloniki,), (r.kifisia,), (r.thessaloniki, r.kifisia)],
            [(), (r.ike,), (r.oe,)],
            [(), ("new_company",), ("kad_added",)],
            [(), (r.kifisia,), (r.kad_2026,), (r.ike,)],
        )
        for index, (kads, regions, forms, types, exclusions) in enumerate(grid):
            try:
                self.radar(name=f"g{index}", kads=kads, regions=regions, legal_forms=forms, signal_types=types,
                           exclusions=exclusions)
            except RadarError:
                continue  # inactive-invalid or contradictory combinations cannot exist
        all_ids = list(OrganizationRadar.objects.values_list("pk", flat=True))
        definitions = load_radar_definitions(all_ids)
        contexts = [
            context(), context("kad_added"),
            context(kads=frozenset({("47191002", "kad_2026")}), prefecture="61190", municipality=None, legal=None),
            context(kads=frozenset({("62010000", None)}), unknown=2, prefecture=None, legal="3"),
            context(kads=frozenset({("47191002", "kad_2026")}), unknown=1),
            context(kads=frozenset(), prefecture="5", municipality="1"),
            context(status=NO_SNAPSHOT), context("capital_increase", status=UNSUPPORTED_SOURCE, source="document_metadata"),
        ]
        self.assertGreater(len(all_ids), 500)
        for ctx in contexts:
            candidates = set(candidate_radar_ids(ctx))
            reachable = {pk for pk, (_, d) in definitions.items()
                         if evaluate_organization_radar(d, ctx).status in (MATCH, INSUFFICIENT_STATE)}
            self.assertTrue(reachable <= candidates, (ctx, sorted(reachable - candidates)[:5]))
        full = contexts[0]
        self.assertLess(len(candidate_radar_ids(full)), len(all_ids) / 2)

    def test_many_irrelevant_radars_are_not_materialized_or_evaluated(self):
        r = self.r
        others = [organization(f"Org {i}", f"o{i}@example.com") for i in range(4)]
        for i in range(60):
            org = others[i % 4]
            kind = i % 4
            if kind == 0:
                self.radar(org, kads=(r.kad_other,))
            elif kind == 1:
                self.radar(org, kads=(r.kad_2008,), regions=(r.attica,))
            elif kind == 2:
                self.radar(org, signal_types=("kad_removed",))
            else:
                self.radar(org, regions=(r.thessaloniki,), legal_forms=(r.oe,))
        matching = [self.radar(kads=(r.kad_2026,)), self.radar(regions=(r.attica,)), self.radar(legal_forms=(r.ike,))]
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        with patch.object(c5, "evaluate_organization_radar", wraps=evaluate_organization_radar) as evaluator:
            report = explain_organization_radar_matches(signal)
        self.assertEqual(OrganizationRadar.objects.filter(active=True).count(), 63)
        self.assertEqual(report.candidate_count, 3)
        self.assertEqual(evaluator.call_count, 3)
        self.assertEqual(sorted(m.radar_id for m in report.matches), sorted(x.pk for x in matching))

    def test_query_count_does_not_grow_with_candidates(self):
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        full = dict(kads=(self.r.kad_2026,), regions=(self.r.attica,), legal_forms=(self.r.ike,),
                    signal_types=("new_company",), exclusions=(self.r.thessaloniki,))
        for _ in range(3):
            self.radar(**full)
        with CaptureQueriesContext(connection) as few:
            self.assertEqual(len(find_matching_organization_radars(signal)), 3)
        for _ in range(27):
            self.radar(**full)
        with CaptureQueriesContext(connection) as many:
            self.assertEqual(len(find_matching_organization_radars(signal)), 30)
        # signal, snapshot, candidates, radars, and one query per criterion table
        self.assertEqual(len(few), 9)
        self.assertEqual(len(many), len(few))
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in few.captured_queries + many.captured_queries))

    def test_batch_definitions_equal_the_c3_reader(self):
        radar = self.radar(kads=(self.r.kad_other, self.r.kad_2026), regions=(self.r.kifisia, self.r.attica),
                           legal_forms=(self.r.oe, self.r.ike), signal_types=("kad_added", "new_company"),
                           exclusions=(self.r.thessaloniki, self.r.kad_2008), score_threshold=40)
        loaded = load_radar_definitions([radar.pk])
        self.assertEqual(loaded, {radar.pk: (self.org.pk, get_organization_radar_definition(self.org, radar))})
        self.assertEqual(load_radar_definitions([]), {})


# --- side effects, parity, privacy ---------------------------------------------------------------

class ParityTests(MatchingTestCase):
    def test_matching_changes_nothing_in_the_legacy_product_monitoring_b4_or_billing(self):
        user = entitled_user("legacy@example.com")
        legacy = radar_for(user, "legacy", prefectures=["ΑΤΤΙΚΗΣ"])
        run_at = datetime(2026, 9, 16, 9, tzinfo=dt_timezone.utc)
        CompanyMonitoring.objects.create(
            company=self.company, state="active", priority="high", primary_reason="active_radar_match",
            policy_version=1, monitored_since=run_at - timedelta(days=3), next_check_at=run_at - timedelta(hours=1))
        policy = RefreshPolicy(max_requests_per_run=5, max_pages_per_query=2, max_direct_details_per_run=2, page_size=2)

        def world():
            mail.outbox = []
            DigestDelivery.objects.all().delete()
            sent = send_digests(date(2026, 9, 1))
            return (
                list(CustomerRadar.objects.values()), [r.pk for r in eligible_radars()],
                company_matches_radar(Company.objects.get(pk=self.company.pk), CustomerRadar.objects.get(pk=legacy.pk)),
                {pk: e.source_ids for pk, e in radar_match_evidence().items()},
                build_company_refresh_plan(run_at=run_at, policy=policy).summary(),
                list(UserSubscription.objects.values()), UserSubscription.objects.get(user=user).has_entitlement,
                list(CompanyMonitoring.objects.values()), list(RadarMatch.objects.values()),
                list(UserCompanyLead.objects.values()), (sent, [(m.subject, m.body) for m in mail.outbox]),
                list(OrganizationRadar.objects.values()), list(CompanySignal.objects.values()),
                list(CompanySnapshot.objects.values()), list(Company.objects.values()),
            )

        snapshot(self.company, T0)
        self.radar(kads=(self.r.kad_2026,), regions=(self.r.attica,))
        signal = new_company_signal(self.company, T0)
        with patch("django.core.signing.TimestampSigner.timestamp", return_value=TimestampSigner().timestamp()), \
                patch("gemiapp.ingestion.refresh.get_gemi_client") as client:
            before = world()
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(len(find_matching_organization_radars(signal)), 1)
                summarize_organization_radar_matching(mode=SHADOW)
            self.assertEqual(world(), before)
        client.assert_not_called()
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries.captured_queries))
        self.assertEqual(self.company.monitoring.reasons.count(), 0)

    def test_the_deployed_phone_bridge_is_intact(self):
        self.assertEqual([p.display for p in extract_company_contact_phones(
            {"phone": "2109990001", "persons": [{"phone": "6999990002"}]})], ["2109990001"])
        self.assertEqual([p.tel_uri for p in Company(gemi_number="1", raw_data={"phone": "+30 2109990001"}).gemi_phones],
                         ["tel:+302109990001"])


class PrivacyAndCommandTests(MatchingTestCase):
    def test_no_private_fact_reaches_context_results_logs_or_command_output(self):
        snapshot(self.company, T0, legal=None)
        self.radar(kads=(self.r.kad_2026,))
        self.radar(legal_forms=(self.r.ike,))
        signal = new_company_signal(self.company, T0)
        with self.assertLogs("gemiapp.organization_radar_matching", level="INFO") as logs:
            report = explain_organization_radar_matches(signal)
        out = StringIO()
        call_command("show_organization_radar_matches", signal_id=signal.pk, stdout=out)
        call_command("show_organization_radar_matches", mode=SHADOW, stdout=out)
        text = "\n".join([repr(report), repr(report.matches), "\n".join(logs.output), out.getvalue()])
        for secret in PRIVATE + ("ΑΤΤΙΚΗΣ", self.company.name, "14561"):
            self.assertNotIn(secret, text)
        self.assertIn(f"signal=#{signal.pk} type=new_company", out.getvalue())
        self.assertIn("result=match", out.getvalue())
        self.assertIn("result=insufficient_state matched=- failed=- unknown=legal_form", out.getvalue())

    def test_command_requires_an_explicit_target_and_writes_nothing(self):
        with self.assertRaises(CommandError):
            call_command("show_organization_radar_matches", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("show_organization_radar_matches", signal_id=999999, stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("show_organization_radar_matches", mode=SHADOW, limit=0, stdout=StringIO())
