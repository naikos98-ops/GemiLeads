"""Tests for opportunity persistence (C8, gemiapp.opportunities).

Real C5 -> C6 -> C7 -> C8 fixture flows. An opportunity is one row per (organization, radar, company); its scoring
capture is frozen, so a later Radar edit can never rewrite what the customer was told.
"""

import dataclasses
import inspect
from datetime import date, datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.core import mail
from django.db import IntegrityError, connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.core.signing import TimestampSigner

from . import opportunities as c8
from .company_contact import extract_company_contact_phones
from .company_signals import SHADOW
from .industry_templates import create_industry_template
from .ingestion.monitoring import radar_match_evidence
from .ingestion.refresh import RefreshPolicy, build_company_refresh_plan
from .models import (
    Company, CompanyMonitoring, CompanySignal, CompanySnapshot, CustomerRadar, DigestDelivery, Opportunity,
    OpportunityScoreComponent, OpportunityScoreEvidence, OpportunitySignal, OrganizationRadar, RadarMatch,
    UserCompanyLead, UserSubscription,
)
from .opportunities import (
    BELOW_THRESHOLD, OpportunityError, effective_threshold, get_opportunity_score_breakdown,
    materialize_opportunities_for_signal, materialize_opportunity, qualifies, set_opportunity_status,
)
from .opportunity_score_breakdown import (
    AWARDED, COMPONENT_ORDER, EXACT_KAD_MATCH, FRESH_WITHIN_24H, KadEvidence, LegalFormEvidence, RegionEvidence,
    SignalTypeEvidence, explain_opportunity_scores,
)
from .opportunity_scoring import (
    HIGH, LOW, MEDIUM, OPPORTUNITY_SCORE_RULE_VERSION, PRIORITY, score_matching_organization_radars,
)
from .organization_radar_matching import ORGANIZATION_RADAR_MATCH_RULE_VERSION, explain_organization_radar_matches
from .organization_radars import RadarDefinition, replace_organization_radar
from .services import company_matches_radar, eligible_radars, send_digests
from .test_gemi_company_activities import entitled_user, radar_for
from .test_opportunity_scoring import FRESH, STALE
from .test_organization_radar_matching import (
    ADDRESS_SENTINEL, PRIVATE, T0, MatchingTestCase, diff_signal, make_company, new_company_signal, organization,
    snapshot,
)

C8_TABLES = (Opportunity, OpportunitySignal, OpportunityScoreComponent, OpportunityScoreEvidence)


def counts():
    return tuple(model.objects.count() for model in C8_TABLES)


class OpportunityTestCase(MatchingTestCase):
    def everything(self, **overrides):
        values = dict(kads=(self.r.kad_2026,), regions=(self.r.attica,), legal_forms=(self.r.ike,),
                      signal_types=("new_company",))
        values.update(overrides)
        return values

    def parts(self, signal, radar, *, as_of=FRESH):
        breakdown = next(b for b in explain_opportunity_scores(signal, as_of=as_of) if b.radar_id == radar.pk)
        score = next(s for s in score_matching_organization_radars(signal, as_of=as_of) if s.radar_id == radar.pk)
        return dict(signal=signal, radar=radar, score=score, breakdown=breakdown)

    def materialize(self, signal, radar, *, as_of=FRESH):
        return materialize_opportunity(**self.parts(signal, radar, as_of=as_of))


# --- schema and boundaries -------------------------------------------------------------------------

class SchemaTests(TestCase):
    def test_the_migration_is_additive_and_is_the_first_opportunity_migration(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        names = sorted(name for app, name in loader.disk_migrations if app == "gemiapp")
        # 0048 created the opportunity tables; D31's 0049 adds the assignment pair and D33's 0050 the notes table
        # (each pinned in its own tests).
        self.assertEqual(names[names.index("0048_opportunity"):],
                         ["0048_opportunity", "0049_opportunity_assignment", "0050_opportunity_note"])
        migration = loader.disk_migrations[("gemiapp", "0048_opportunity")]
        allowed = {"CreateModel", "AddIndex", "AddConstraint", "AddField"}
        self.assertTrue({type(op).__name__ for op in migration.operations} <= allowed)
        self.assertFalse([op for op in migration.operations if type(op).__name__ == "RunPython"])

    def test_opportunity_fields_follow_section_29_and_belong_to_an_organization(self):
        names = {f.name for f in Opportunity._meta.get_fields()}
        for required in ("organization", "radar", "company", "score", "status", "created_at", "expires_at"):
            self.assertIn(required, names)
        for forbidden in ("user", "subscription", "phone", "email", "address", "vat_number", "raw_data", "persons",
                          "name", "note"):
            self.assertNotIn(forbidden, names, forbidden)
        # D31 (§40) added exactly the assignment pair, pointing at a membership -- never a bare user.
        self.assertIn("assigned_to", names)
        self.assertIn("assigned_at", names)
        self.assertEqual(Opportunity._meta.get_field("assigned_to").related_model.__name__, "OrganizationMember")
        self.assertEqual([value for value, _ in Opportunity.STATUSES],
                         ["new", "viewed", "saved", "assigned", "contacted", "interested", "follow_up", "won",
                          "lost", "not_relevant", "do_not_contact"])
        self.assertEqual(Opportunity._meta.get_field("status").default, "new")
        self.assertTrue(Opportunity._meta.get_field("expires_at").null)

    def test_history_is_protected_and_organizations_cascade(self):
        policies = {
            (Opportunity, "organization"): "CASCADE", (Opportunity, "radar"): "PROTECT",
            (Opportunity, "company"): "PROTECT", (Opportunity, "first_signal"): "PROTECT",
            (Opportunity, "latest_signal"): "PROTECT", (OpportunitySignal, "signal"): "PROTECT",
            (OpportunitySignal, "opportunity"): "CASCADE", (OpportunityScoreComponent, "opportunity"): "CASCADE",
            (OpportunityScoreEvidence, "component"): "CASCADE",
        }
        for (model, field), expected in policies.items():
            self.assertEqual(model._meta.get_field(field).remote_field.on_delete.__name__, expected, (model, field))

    def test_no_evidence_blob_and_no_free_text(self):
        for model in (Opportunity, OpportunityScoreComponent, OpportunityScoreEvidence):
            for field in model._meta.get_fields():
                self.assertNotEqual(type(field).__name__, "JSONField", (model, field))
                self.assertNotEqual(type(field).__name__, "TextField", (model, field))

    def test_the_service_writes_no_legacy_or_billing_state(self):
        code = inspect.getsource(c8).split('"""', 2)[2]
        for forbidden in ("CustomerRadar", "RadarMatch", "UserCompanyLead", "CompanyMonitoring", "UserSubscription",
                          "stripe", "billing", "send_mail", "urllib", "requests", "request.user", "session",
                          "raw_data", "gemi_phones", "phone", "email", "person", "timezone.now("):
            self.assertNotIn(forbidden, code, forbidden)

    def test_nothing_in_the_product_reads_opportunities(self):
        from django.conf import settings

        for path in ("gemiapp/urls.py", "gemiapp/views.py", "config/urls.py", "gemiapp/tasks.py", "gemiapp/apps.py",
                     "gemiapp/services.py", "gemiapp/billing.py", "gemiapp/forms.py",
                     "gemiapp/ingestion/monitoring.py", "gemiapp/ingestion/refresh.py", "config/settings.py",
                     "render.yaml"):
            source = open(path, encoding="utf-8").read()
            for usage in ("from .opportunities", "from gemiapp.opportunities", "import opportunities",
                          "Opportunity.objects", "materialize_opportunity", "opportunities.", "OpportunitySignal",
                          "OpportunityScore"):
                self.assertNotIn(usage, source, (path, usage))
        # A9 only *reserves* an ACTIVE_OPPORTUNITY reason in prose; it must still populate nothing.
        monitoring = open("gemiapp/ingestion/monitoring.py", encoding="utf-8").read()
        self.assertIn("ACTIVE_OPPORTUNITY (reserved, not populated)", monitoring)
        self.assertIn("RESERVED_REASONS = (ACTIVE_OPPORTUNITY, RECENT_SIGNAL)", monitoring)
        self.assertFalse([m for m in settings.MIDDLEWARE if "opportunit" in m.lower()])


# --- creation, threshold, aggregation ---------------------------------------------------------------

class MaterializationTests(OpportunityTestCase):
    def test_a_qualifying_match_creates_one_opportunity_with_its_frozen_capture(self):
        radar = self.radar(**self.everything())
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        result = self.materialize(signal, radar)
        self.assertEqual((result.eligible, result.created, result.signal_attached, result.rescored),
                         (True, True, True, False))
        opportunity = result.opportunity
        self.assertEqual((opportunity.organization_id, opportunity.radar_id, opportunity.company_id),
                         (self.org.pk, radar.pk, self.company.pk))
        self.assertEqual((opportunity.score, opportunity.score_class, opportunity.status), (100, PRIORITY, "new"))
        self.assertEqual((opportunity.score_rule_version, opportunity.match_rule_version),
                         (OPPORTUNITY_SCORE_RULE_VERSION, ORGANIZATION_RADAR_MATCH_RULE_VERSION))
        self.assertEqual((opportunity.scored_as_of, opportunity.first_signal_id, opportunity.latest_signal_id),
                         (FRESH, signal.pk, signal.pk))
        self.assertEqual(opportunity.primary_reason_code, EXACT_KAD_MATCH)
        self.assertIsNone(opportunity.expires_at)
        self.assertEqual(counts(), (1, 1, 5, 5))

    def test_the_stored_capture_holds_exactly_the_five_components_and_sums_to_the_score(self):
        radar = self.radar(**self.everything())
        snapshot(self.company, T0)
        opportunity = self.materialize(new_company_signal(self.company, T0), radar).opportunity
        components = list(opportunity.score_components.order_by("position"))
        self.assertEqual([c.code for c in components], list(COMPONENT_ORDER))
        self.assertEqual([c.awarded_points for c in components], [30, 25, 15, 10, 20])
        self.assertEqual([c.max_points for c in components], [30, 25, 15, 10, 20])
        self.assertEqual(sum(c.awarded_points for c in components), opportunity.score)
        frozen = get_opportunity_score_breakdown(opportunity)
        self.assertEqual(frozen.component("industry_fit").evidence,
                         (KadEvidence(code="62010000", kad_version="kad_2026"),))
        self.assertEqual(frozen.component("geographic_fit").evidence,
                         (RegionEvidence(level="prefecture", source_id="5"),))
        self.assertEqual(frozen.component("legal_form_fit").evidence, (LegalFormEvidence(source_id="19"),))
        self.assertEqual(frozen.component("signal_relevance").evidence,
                         (SignalTypeEvidence(signal_type="new_company"),))
        freshness = frozen.component("freshness")
        self.assertEqual((freshness.reason_code, freshness.status, freshness.evidence[0].age_seconds),
                         (FRESH_WITHIN_24H, AWARDED, 7200))
        self.assertEqual(frozen, next(b for b in explain_opportunity_scores(opportunity.latest_signal, as_of=FRESH)
                                      if b.radar_id == radar.pk))

    def test_the_threshold_decides_eligibility_at_its_exact_boundary(self):
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        # KAD + region + freshness = 65 for this Radar shape.
        for threshold, expected in ((64, True), (65, True), (66, False), (100, False)):
            radar = self.radar(name=f"t{threshold}", kads=(self.r.kad_2026,), regions=(self.r.attica,),
                               score_threshold=threshold)
            result = self.materialize(signal, radar)
            self.assertEqual(result.eligible, expected, threshold)
            if expected:
                self.assertEqual(result.opportunity.score, 65)
            else:
                self.assertIsNone(result.opportunity)
                self.assertEqual(result.ineligible_reason, BELOW_THRESHOLD)
        self.assertEqual(Opportunity.objects.count(), 2)

    def test_a_radar_without_a_threshold_qualifies_at_any_score(self):
        radar = self.radar(kads=(self.r.kad_2026,), score_threshold=None)
        snapshot(self.company, T0)
        self.assertIsNone(effective_threshold(radar))
        result = self.materialize(new_company_signal(self.company, T0), radar, as_of=STALE)  # freshness 0
        self.assertEqual((result.eligible, result.opportunity.score, result.opportunity.score_class),
                         (True, 30, LOW))

    def test_a_rejected_evaluation_writes_nothing_at_all(self):
        radar = self.radar(kads=(self.r.kad_2026,), score_threshold=90)
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        before = counts()
        result = self.materialize(signal, radar, as_of=STALE)
        self.assertFalse(result.eligible)
        self.assertEqual(counts(), before)
        self.assertEqual(counts(), (0, 0, 0, 0))

    def test_processing_the_same_signal_again_is_idempotent(self):
        radar = self.radar(**self.everything())
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        first = self.materialize(signal, radar)
        stored = Opportunity.objects.values().get()
        second = self.materialize(signal, radar)
        self.assertEqual((second.created, second.signal_attached, second.rescored), (False, False, False))
        self.assertEqual(second.opportunity.pk, first.opportunity.pk)
        self.assertEqual(Opportunity.objects.values().get(), stored)  # not even updated_at moved
        self.assertEqual(counts(), (1, 1, 5, 5))

    def test_a_second_qualifying_signal_aggregates_into_the_same_opportunity(self):
        radar = self.radar(kads=(self.r.kad_2026,), regions=(self.r.attica,))
        previous, current = snapshot(self.company, T0), snapshot(self.company, T0 + timedelta(hours=1))
        first_signal = new_company_signal(self.company, T0)
        opportunity = self.materialize(first_signal, radar).opportunity
        created_at, opened_by = opportunity.created_at, opportunity.first_signal_id
        later = diff_signal(self.company, previous, current, T0 + timedelta(days=2))
        result = self.materialize(later, radar, as_of=T0 + timedelta(days=2, hours=1))
        self.assertEqual((result.created, result.signal_attached, result.rescored), (False, True, True))
        opportunity.refresh_from_db()
        self.assertEqual(Opportunity.objects.count(), 1)
        self.assertEqual(sorted(opportunity.signals.values_list("signal_id", flat=True)),
                         sorted([first_signal.pk, later.pk]))
        self.assertEqual((opportunity.first_signal_id, opportunity.latest_signal_id), (opened_by, later.pk))
        self.assertEqual(opportunity.created_at, created_at)
        self.assertEqual(opportunity.scored_as_of, T0 + timedelta(days=2, hours=1))
        self.assertEqual(opportunity.score_components.count(), 5)

    def test_the_newest_qualifying_evaluation_wins_even_when_it_scores_lower(self):
        radar = self.radar(kads=(self.r.kad_2026,), regions=(self.r.attica,))
        previous, current = snapshot(self.company, T0), snapshot(self.company, T0 + timedelta(hours=1))
        opportunity = self.materialize(new_company_signal(self.company, T0), radar).opportunity
        self.assertEqual(opportunity.score, 65)
        later = diff_signal(self.company, previous, current, T0 + timedelta(days=2))
        self.materialize(later, radar, as_of=T0 + timedelta(days=40))  # stale: freshness 0
        opportunity.refresh_from_db()
        self.assertEqual(opportunity.score, 45)  # not max(65, 45)
        self.assertEqual(opportunity.score_components.get(code="freshness").awarded_points, 0)
        self.assertEqual([row.score for row in opportunity.signals.order_by("id")], [65, 45])

    def test_a_later_below_threshold_signal_leaves_the_opportunity_untouched(self):
        radar = self.radar(kads=(self.r.kad_2026,), regions=(self.r.attica,), score_threshold=60)
        previous, current = snapshot(self.company, T0), snapshot(self.company, T0 + timedelta(hours=1))
        opportunity = self.materialize(new_company_signal(self.company, T0), radar).opportunity
        stored = Opportunity.objects.values().get()
        later = diff_signal(self.company, previous, current, T0 + timedelta(days=2))
        result = self.materialize(later, radar, as_of=T0 + timedelta(days=40))  # 45 < 60
        self.assertEqual((result.eligible, result.ineligible_reason), (False, BELOW_THRESHOLD))
        self.assertEqual(Opportunity.objects.values().get(), stored)
        self.assertEqual(opportunity.signals.count(), 1)

    def test_each_radar_and_each_organization_keeps_its_own_opportunity(self):
        other_org = organization("Other", "other-c8@example.com")
        mine = self.radar(name="mine", kads=(self.r.kad_2026,))
        second = self.radar(name="second", regions=(self.r.attica,))
        theirs = self.radar(other_org, name="theirs", kads=(self.r.kad_2026,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        for radar in (mine, second, theirs):
            self.assertTrue(self.materialize(signal, radar).eligible)
        self.assertEqual(Opportunity.objects.count(), 3)
        self.assertEqual(Opportunity.objects.filter(organization=self.org).count(), 2)
        self.assertEqual(Opportunity.objects.filter(organization=other_org).count(), 1)
        self.assertEqual(set(Opportunity.objects.values_list("company_id", flat=True)), {self.company.pk})

    def test_the_identity_is_enforced_by_the_database(self):
        radar = self.radar(kads=(self.r.kad_2026,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        opportunity = self.materialize(signal, radar).opportunity
        with self.assertRaises(IntegrityError), transaction.atomic():
            Opportunity.objects.create(
                organization=self.org, radar=radar, company=self.company, first_signal=signal, latest_signal=signal,
                score=10, score_class=LOW, score_rule_version="x", match_rule_version="y", scored_as_of=FRESH)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OpportunitySignal.objects.create(opportunity=opportunity, signal=signal, score=1, scored_as_of=FRESH)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OpportunityScoreComponent.objects.create(opportunity=opportunity, code="industry_fit", awarded_points=1,
                                                     max_points=30, reason_code="x", position=9)

    def test_the_batch_helper_runs_the_whole_pipeline_without_duplicates(self):
        qualifying = self.radar(name="q", **self.everything())
        low = self.radar(name="low", kads=(self.r.kad_2026,), score_threshold=95)
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        results = materialize_opportunities_for_signal(signal, as_of=FRESH)
        self.assertEqual(sorted(r.eligible for r in results), [False, True])
        self.assertEqual(Opportunity.objects.count(), 1)
        self.assertEqual(Opportunity.objects.get().radar_id, qualifying.pk)
        again = materialize_opportunities_for_signal(signal, as_of=FRESH)
        self.assertEqual([r.created for r in again], [False, False])
        self.assertEqual(counts(), (1, 1, 5, 5))
        self.assertFalse(Opportunity.objects.filter(radar=low).exists())


# --- historical stability, validation, status --------------------------------------------------------

class FrozenCaptureTests(OpportunityTestCase):
    def test_editing_the_radar_afterwards_never_changes_the_stored_explanation(self):
        radar = self.radar(**self.everything())
        snapshot(self.company, T0)
        opportunity = self.materialize(new_company_signal(self.company, T0), radar).opportunity
        before = dataclasses.replace(get_opportunity_score_breakdown(opportunity))
        stored = Opportunity.objects.values().get()
        replace_organization_radar(self.org, radar, RadarDefinition(
            name="changed", active=True, kads=(self.r.kad_other,), regions=(self.r.thessaloniki,)))
        create_industry_template(slug="x", name="X", kads=(self.r.kad_2026,), active=True)
        opportunity.refresh_from_db()
        self.assertEqual(Opportunity.objects.values().get(), stored)
        self.assertEqual(get_opportunity_score_breakdown(opportunity), before)
        self.assertEqual(opportunity.score_components.get(code="industry_fit").evidence.get().kad_code, "62010000")

    def test_the_frozen_as_of_is_exactly_the_capture_time(self):
        radar = self.radar(kads=(self.r.kad_2026,))
        snapshot(self.company, T0)
        opportunity = self.materialize(new_company_signal(self.company, T0), radar, as_of=FRESH).opportunity
        self.assertEqual(opportunity.scored_as_of, FRESH)
        self.assertEqual(get_opportunity_score_breakdown(opportunity).as_of, FRESH)
        self.assertEqual(opportunity.score_components.get(code="freshness").evidence.get().scored_as_of, FRESH)

    def test_the_reader_never_recomputes(self):
        radar = self.radar(**self.everything())
        snapshot(self.company, T0)
        opportunity = self.materialize(new_company_signal(self.company, T0), radar).opportunity
        CompanySnapshot.objects.all().delete()
        OrganizationRadar.objects.filter(pk=radar.pk).update(active=False)
        stored = Opportunity.objects.get(pk=opportunity.pk)
        with CaptureQueriesContext(connection) as queries:
            frozen = get_opportunity_score_breakdown(stored)
        self.assertEqual(frozen.score, 100)
        self.assertEqual(len(queries), 2)  # components plus their evidence
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries.captured_queries))


class ValidationTests(OpportunityTestCase):
    def setUp(self):
        super().setUp()
        self.radar = self.radar(**self.everything())
        snapshot(self.company, T0)
        self.signal = new_company_signal(self.company, T0)

    def test_only_confirmed_scored_explained_matches_are_accepted(self):
        parts = self.parts(self.signal, self.radar)
        other_company = make_company("401000")
        snapshot(other_company, T0)
        other_signal = new_company_signal(other_company, T0)
        other_org = organization("Other", "other-valid@example.com")
        foreign_radar = self.radar_for(other_org)
        bad = [
            {"signal": other_signal},                                   # the score describes another signal
            {"radar": foreign_radar},                                   # cross-tenant Radar
            {"score": dataclasses.replace(parts["score"], score=99)},   # score/breakdown disagree
            {"score": dataclasses.replace(parts["score"], organization_id=other_org.pk)},
            {"score": dataclasses.replace(parts["score"], company_id=other_company.pk)},
            {"score": dataclasses.replace(parts["score"], scoring_rule_version="opportunity_score:v2")},
            {"score": dataclasses.replace(parts["score"], match_rule_version="organization_radar_match:v2")},
            {"breakdown": dataclasses.replace(parts["breakdown"], score=99)},
            {"breakdown": dataclasses.replace(parts["breakdown"], components=parts["breakdown"].components[:4])},
            {"signal": None}, {"radar": None}, {"score": None}, {"breakdown": None}, {"score": 100},
        ]
        for override in bad:
            with self.assertRaises(OpportunityError, msg=override):
                materialize_opportunity(**{**parts, **override})
        self.assertEqual(counts(), (0, 0, 0, 0))

    def radar_for(self, org):
        from .organization_radars import create_organization_radar

        return create_organization_radar(org, RadarDefinition(name="foreign", active=True, kads=(self.r.kad_2026,)))

    def test_a_deactivated_radar_cannot_materialize(self):
        parts = self.parts(self.signal, self.radar)
        OrganizationRadar.objects.filter(pk=self.radar.pk).update(active=False)
        parts["radar"].refresh_from_db()
        with self.assertRaises(OpportunityError):
            materialize_opportunity(**parts)
        self.assertEqual(counts(), (0, 0, 0, 0))

    def test_non_matching_evaluations_never_reach_persistence(self):
        other = make_company("401100")
        snapshot(other, T0, kads=(("47191002", "kad_2026"),))
        signal = new_company_signal(other, T0)
        report = explain_organization_radar_matches(signal)
        self.assertEqual(report.matches, ())  # NO_MATCH: nothing to score, nothing to persist
        self.assertEqual(materialize_opportunities_for_signal(signal, as_of=FRESH), ())
        insufficient = make_company("401200")
        signal_without_state = new_company_signal(insufficient, T0)  # no snapshot -> INSUFFICIENT_STATE
        self.assertEqual(materialize_opportunities_for_signal(signal_without_state, as_of=FRESH), ())
        self.assertEqual(counts(), (0, 0, 0, 0))

    def test_status_is_validated_and_triggers_nothing(self):
        opportunity = self.materialize(self.signal, self.radar).opportunity
        self.assertEqual(opportunity.status, "new")
        for status in ("viewed", "won", "do_not_contact"):
            self.assertEqual(set_opportunity_status(opportunity, status).status, status)
        for invalid in ("archived", "NEW", "", None, 5):
            with self.assertRaises(OpportunityError):
                set_opportunity_status(opportunity, invalid)
        opportunity.refresh_from_db()
        self.assertEqual((opportunity.status, opportunity.score, RadarMatch.objects.count(),
                          UserCompanyLead.objects.count()), ("do_not_contact", 100, 0, 0))

    def test_reading_an_opportunity_with_its_history_is_bounded(self):
        opportunity = self.materialize(self.signal, self.radar).opportunity
        with CaptureQueriesContext(connection) as queries:
            row = (Opportunity.objects.select_related("organization", "radar", "company", "first_signal",
                                                      "latest_signal")
                   .prefetch_related("signals", "score_components__evidence").get(pk=opportunity.pk))
            list(row.signals.all())
            [list(component.evidence.all()) for component in row.score_components.all()]
        self.assertEqual(len(queries), 4)  # root with its relations, signals, components, evidence


# --- parity, privacy ---------------------------------------------------------------------------------

class ParityTests(OpportunityTestCase):
    def test_materializing_changes_nothing_in_the_legacy_product_or_billing(self):
        user = entitled_user("legacy-opportunity@example.com")
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
                list(CompanySignal.objects.values()), list(CompanySnapshot.objects.values()),
                list(Company.objects.values()), list(OrganizationRadar.objects.values()),
            )

        radar = self.radar(**self.everything())
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        with patch("django.core.signing.TimestampSigner.timestamp", return_value=TimestampSigner().timestamp()), \
                patch("gemiapp.ingestion.refresh.get_gemi_client") as client:
            before = world()
            self.assertTrue(self.materialize(signal, radar).eligible)
            self.assertEqual(world(), before)
        client.assert_not_called()
        self.assertEqual(self.company.monitoring.reasons.count(), 0)
        self.assertEqual(Opportunity.objects.count(), 1)

    def test_the_deployed_phone_bridge_is_intact_and_contact_data_is_never_stored(self):
        self.assertEqual([p.display for p in extract_company_contact_phones(
            {"phone": "2109990001", "persons": [{"phone": "6999990002"}]})], ["2109990001"])
        self.assertEqual([p.tel_uri for p in Company(gemi_number="1", raw_data={"phone": "+30 2109990001"}).gemi_phones],
                         ["tel:+302109990001"])
        radar = self.radar(**self.everything())
        snapshot(self.company, T0)
        opportunity = self.materialize(new_company_signal(self.company, T0), radar).opportunity
        rows = [*Opportunity.objects.values(), *OpportunitySignal.objects.values(),
                *OpportunityScoreComponent.objects.values(), *OpportunityScoreEvidence.objects.values()]
        text = "\n".join(repr(row) for row in rows) + repr(get_opportunity_score_breakdown(opportunity))
        for secret in PRIVATE + ("ΑΤΤΙΚΗΣ", self.company.name, ADDRESS_SENTINEL, "14561"):
            self.assertNotIn(secret, text)

    def test_the_admin_is_read_only(self):
        from django.contrib import admin
        from django.contrib.auth.models import User
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser("root-c8", "root-c8@example.com", "StrongPass123")
        for model in C8_TABLES:
            self.assertFalse(admin.site._registry[model].has_add_permission(request))
            self.assertFalse(admin.site._registry[model].has_change_permission(request))
