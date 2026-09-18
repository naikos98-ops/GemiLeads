"""Tests for the opportunity score breakdown (C7, gemiapp.opportunity_score_breakdown).

Real C5 + C6 fixture flows; the breakdown explains the authoritative score and may never drift from it. Exactly
five v1 components: no contactability, company characteristics, company age or template fit.
"""

import dataclasses
import inspect
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

from . import opportunity_score_breakdown as c7
from .company_contact import extract_company_contact_phones
from .company_signals import LIVE, SHADOW
from .industry_templates import create_industry_template
from .ingestion.monitoring import radar_match_evidence
from .ingestion.refresh import RefreshPolicy, build_company_refresh_plan
from .models import (
    Company, CompanyMonitoring, CompanySignal, CompanySnapshot, CustomerRadar, DigestDelivery, OrganizationRadar,
    RadarMatch, UserCompanyLead, UserSubscription,
)
from .opportunity_score_breakdown import (
    AWARDED, COMPONENT_LABELS, COMPONENT_MAX_POINTS, COMPONENT_ORDER, EXACT_KAD_MATCH, EXPLICIT_LEGAL_FORM_MATCH,
    EXPLICIT_REGION_MATCH, EXPLICIT_SIGNAL_TYPE_MATCH, FRESH_WITHIN_7D, FRESH_WITHIN_14D, FRESH_WITHIN_24H,
    FRESH_WITHIN_30D, FRESH_WITHIN_72H, FRESHNESS, GEOGRAPHIC_FIT, INDUSTRY_FIT, KadEvidence, LEGAL_FORM_FIT,
    LegalFormEvidence, NOT_AWARDED, OpportunityScoreBreakdown, RADAR_ACCEPTS_ANY_SIGNAL_TYPE,
    RADAR_HAS_NO_KAD_TARGET, RADAR_HAS_NO_LEGAL_FORM_TARGET, RADAR_HAS_NO_REGION_TARGET, RegionEvidence,
    SIGNAL_RELEVANCE, STALE_OVER_30D, ScoreBreakdownComponent, ScoreBreakdownError, SignalTypeEvidence,
    build_opportunity_score_breakdown, explain_opportunity_scores,
)
from .opportunity_scoring import (
    HIGH, LOW, MEDIUM, OPPORTUNITY_SCORE_RULE_VERSION, PRIORITY, TOTAL_POINTS, build_opportunity_scoring_context,
    calculate_opportunity_score, score_class_for,
)
from .organization_radar_matching import (
    ORGANIZATION_RADAR_MATCH_RULE_VERSION, confirmed_criteria, evaluate_organization_radar,
    explain_organization_radar_matches, load_radar_definitions,
)
from .organization_radars import get_organization_radar_definition
from .services import company_matches_radar, eligible_radars, send_digests
from .test_gemi_company_activities import entitled_user, radar_for
from .test_opportunity_scoring import FRESH, SECOND, STALE, ScoringTestCase
from .test_organization_radar_matching import (
    ADDRESS_SENTINEL, PRIVATE, T0, diff_signal, make_company, new_company_signal, snapshot,
)


class BreakdownTestCase(ScoringTestCase):
    def breakdowns(self, signal, *, as_of=STALE):
        return explain_opportunity_scores(signal, as_of=as_of)

    def only(self, signal, *, as_of=STALE):
        result = self.breakdowns(signal, as_of=as_of)
        self.assertEqual(len(result), 1, result)
        return result[0]

    def lines(self, breakdown):
        return {c.code: (c.awarded_points, c.max_points, c.status, c.reason_code) for c in breakdown.components}

    def parts(self, signal, radar):
        report = explain_organization_radar_matches(signal)
        match = next(m for m in report.matches if m.radar_id == radar.pk)
        evaluation = next(e.evaluation for e in report.evaluations if e.radar_id == match.radar_id)
        scoring_context = build_opportunity_scoring_context(match=match, evaluation=evaluation, context=report.context)
        return dict(scoring_context=scoring_context, match_context=report.context,
                    radar_definition=get_organization_radar_definition(self.org, radar))


# --- contract -------------------------------------------------------------------------------------

class ContractTests(TestCase):
    def test_exactly_five_components_in_a_fixed_order_with_the_v1_maximums(self):
        self.assertEqual(COMPONENT_ORDER,
                         ("industry_fit", "signal_relevance", "geographic_fit", "legal_form_fit", "freshness"))
        self.assertEqual([COMPONENT_MAX_POINTS[code] for code in COMPONENT_ORDER], [30, 25, 15, 10, 20])
        self.assertEqual(sum(COMPONENT_MAX_POINTS.values()), TOTAL_POINTS)
        self.assertEqual(set(COMPONENT_LABELS), set(COMPONENT_ORDER))

    def test_removed_dimensions_appear_nowhere(self):
        source = inspect.getsource(c7)
        for gone in ("contactability", "available_contact", "company_characteristics", "company_age",
                     "template_fit", "IndustryTemplate", "industry_template", "template_id", "meets_threshold",
                     "score_threshold", "eligib"):
            self.assertNotIn(gone, source.split("Nothing is persisted", 1)[1], gone)

    def test_the_layer_never_writes_scores_or_touches_forbidden_sources(self):
        code = inspect.getsource(c7).split('"""', 2)[2]
        for forbidden in (".save(", ".create(", ".update(", ".delete(", "bulk_", "timezone.now(", "objects.",
                          "CustomerRadar", "RadarMatch", "UserCompanyLead", "CompanyMonitoring", "CompanyActivity",
                          "raw_data", "gemi_phones", "phone", "email", "person", "is_active", "get_gemi_client",
                          "stripe", "send_mail", "urllib", "requests", "request.user", "session"):
            self.assertNotIn(forbidden, code, forbidden)

    def test_no_breakdown_model_and_no_migration(self):
        from django.apps import apps

        names = {model.__name__ for model in apps.get_app_config("gemiapp").get_models()}
        self.assertFalse([n for n in names if "Breakdown" in n or "Score" in n or "Opportunit" in n])
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(max(name for app, name in loader.disk_migrations if app == "gemiapp"),
                         "0047_industry_template")

    def test_results_are_immutable_and_carry_no_private_field(self):
        for cls in (OpportunityScoreBreakdown, ScoreBreakdownComponent, KadEvidence, RegionEvidence,
                    LegalFormEvidence, SignalTypeEvidence, c7.FreshnessEvidence):
            self.assertTrue(dataclasses.is_dataclass(cls) and cls.__dataclass_params__.frozen, cls)
        names = {f.name for cls in (OpportunityScoreBreakdown, ScoreBreakdownComponent, KadEvidence, RegionEvidence,
                                    LegalFormEvidence, SignalTypeEvidence, c7.FreshnessEvidence)
                 for f in dataclasses.fields(cls)}
        for forbidden in ("name", "phone", "email", "raw", "person", "address", "city", "postal", "vat", "user",
                          "subscription", "description", "threshold", "age_months"):
            self.assertFalse([n for n in names if forbidden in n and n != "label"], forbidden)

    def test_nothing_in_the_product_consumes_the_breakdown(self):
        for path in ("gemiapp/urls.py", "gemiapp/views.py", "config/urls.py", "gemiapp/tasks.py", "gemiapp/apps.py",
                     "gemiapp/services.py", "gemiapp/billing.py", "gemiapp/forms.py", "gemiapp/ingestion/monitoring.py",
                     "gemiapp/ingestion/refresh.py", "gemiapp/company_timeline.py", "gemiapp/organization_radars.py",
                     "gemiapp/opportunity_scoring.py", "config/settings.py", "render.yaml"):
            self.assertNotIn("score_breakdown", open(path, encoding="utf-8").read(), path)


# --- fixture matrix -------------------------------------------------------------------------------

class FixtureMatrixTests(BreakdownTestCase):
    def test_a_hundred_point_breakdown_explains_every_component(self):
        radar = self.radar(**self.everything())
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        breakdown = self.only(signal, as_of=FRESH)
        self.assertEqual((breakdown.score, breakdown.score_class), (100, PRIORITY))
        self.assertEqual(self.lines(breakdown), {
            INDUSTRY_FIT: (30, 30, AWARDED, EXACT_KAD_MATCH),
            SIGNAL_RELEVANCE: (25, 25, AWARDED, EXPLICIT_SIGNAL_TYPE_MATCH),
            GEOGRAPHIC_FIT: (15, 15, AWARDED, EXPLICIT_REGION_MATCH),
            LEGAL_FORM_FIT: (10, 10, AWARDED, EXPLICIT_LEGAL_FORM_MATCH),
            FRESHNESS: (20, 20, AWARDED, FRESH_WITHIN_24H),
        })
        self.assertEqual(breakdown.component(INDUSTRY_FIT).evidence,
                         (KadEvidence(code="62010000", kad_version="kad_2026"),))
        self.assertEqual(breakdown.component(GEOGRAPHIC_FIT).evidence,
                         (RegionEvidence(level="prefecture", source_id="5"),))
        self.assertEqual(breakdown.component(LEGAL_FORM_FIT).evidence, (LegalFormEvidence(source_id="19"),))
        self.assertEqual(breakdown.component(SIGNAL_RELEVANCE).evidence,
                         (SignalTypeEvidence(signal_type="new_company"),))
        self.assertEqual((breakdown.radar_id, breakdown.organization_id, breakdown.signal_id, breakdown.company_id),
                         (radar.pk, self.org.pk, signal.pk, self.company.pk))
        self.assertEqual(sum(c.awarded_points for c in breakdown.components), 100)

    def test_ninety_point_breakdown_shows_only_freshness_reduced(self):
        self.radar(**self.everything())
        snapshot(self.company, T0)
        breakdown = self.only(new_company_signal(self.company, T0), as_of=T0 + timedelta(days=10))
        self.assertEqual((breakdown.score, breakdown.score_class), (90, PRIORITY))
        self.assertEqual(breakdown.component(FRESHNESS).awarded_points, 10)
        self.assertEqual(breakdown.component(FRESHNESS).reason_code, FRESH_WITHIN_14D)
        self.assertEqual([c.awarded_points for c in breakdown.components], [30, 25, 15, 10, 10])

    def test_seventy_five_point_breakdown(self):
        self.radar(**self.everything(legal_forms=()))
        snapshot(self.company, T0)
        breakdown = self.only(new_company_signal(self.company, T0), as_of=T0 + timedelta(days=20))
        self.assertEqual((breakdown.score, breakdown.score_class), (75, HIGH))
        self.assertEqual(self.lines(breakdown)[LEGAL_FORM_FIT], (0, 10, NOT_AWARDED, RADAR_HAS_NO_LEGAL_FORM_TARGET))
        self.assertEqual(breakdown.component(LEGAL_FORM_FIT).evidence, ())
        self.assertEqual(breakdown.component(FRESHNESS).reason_code, FRESH_WITHIN_30D)

    def test_sixty_five_point_breakdown(self):
        self.radar(kads=(self.r.kad_2026,), regions=(self.r.attica,))
        snapshot(self.company, T0)
        breakdown = self.only(new_company_signal(self.company, T0), as_of=FRESH)
        self.assertEqual((breakdown.score, breakdown.score_class), (65, MEDIUM))
        self.assertEqual([c.awarded_points for c in breakdown.components], [30, 0, 15, 0, 20])
        self.assertEqual(self.lines(breakdown)[SIGNAL_RELEVANCE], (0, 25, NOT_AWARDED, RADAR_ACCEPTS_ANY_SIGNAL_TYPE))
        self.assertEqual(breakdown.component(SIGNAL_RELEVANCE).evidence, ())

    def test_thirty_point_breakdown(self):
        self.radar(kads=(self.r.kad_2026,))
        snapshot(self.company, T0)
        breakdown = self.only(new_company_signal(self.company, T0))
        self.assertEqual((breakdown.score, breakdown.score_class), (30, LOW))
        self.assertEqual([c.awarded_points for c in breakdown.components], [30, 0, 0, 0, 0])
        self.assertEqual(self.lines(breakdown)[GEOGRAPHIC_FIT], (0, 15, NOT_AWARDED, RADAR_HAS_NO_REGION_TARGET))
        self.assertEqual(self.lines(breakdown)[FRESHNESS], (0, 20, NOT_AWARDED, STALE_OVER_30D))

    def test_a_radar_without_kad_targeting_says_so(self):
        self.radar(regions=(self.r.attica,))
        snapshot(self.company, T0)
        breakdown = self.only(new_company_signal(self.company, T0))
        self.assertEqual(self.lines(breakdown)[INDUSTRY_FIT], (0, 30, NOT_AWARDED, RADAR_HAS_NO_KAD_TARGET))
        self.assertEqual(breakdown.component(INDUSTRY_FIT).evidence, ())

    def test_every_freshness_band_has_its_own_reason_code(self):
        self.radar(signal_types=("new_company",))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        expected = (
            (timedelta(hours=1), 20, FRESH_WITHIN_24H), (timedelta(hours=24), 20, FRESH_WITHIN_24H),
            (timedelta(hours=24) + SECOND, 18, FRESH_WITHIN_72H), (timedelta(hours=72), 18, FRESH_WITHIN_72H),
            (timedelta(hours=72) + SECOND, 15, FRESH_WITHIN_7D), (timedelta(days=7), 15, FRESH_WITHIN_7D),
            (timedelta(days=7) + SECOND, 10, FRESH_WITHIN_14D), (timedelta(days=14), 10, FRESH_WITHIN_14D),
            (timedelta(days=14) + SECOND, 5, FRESH_WITHIN_30D), (timedelta(days=30), 5, FRESH_WITHIN_30D),
            (timedelta(days=30) + SECOND, 0, STALE_OVER_30D),
        )
        for age, points, reason in expected:
            component = self.only(signal, as_of=T0 + age).component(FRESHNESS)
            evidence = component.evidence[0]
            self.assertEqual((component.awarded_points, component.reason_code), (points, reason), age)
            self.assertEqual((evidence.detected_at, evidence.as_of, evidence.age_seconds, evidence.band_points),
                             (T0, T0 + age, int(age.total_seconds()), points))
            self.assertEqual(component.status, AWARDED if points else NOT_AWARDED)

    def test_kad_evidence_is_exact_and_never_crosses_versions(self):
        both = self.radar(name="both", kads=(self.r.kad_2008, self.r.kad_2026, self.r.kad_other))
        snapshot(self.company, T0, kads=(("62010000", "kad_2026"),))
        breakdown = self.only(new_company_signal(self.company, T0))
        self.assertEqual(breakdown.component(INDUSTRY_FIT).evidence,
                         (KadEvidence(code="62010000", kad_version="kad_2026"),))
        self.assertEqual(breakdown.radar_id, both.pk)

    def test_municipality_evidence_keeps_its_level(self):
        self.radar(regions=(self.r.kifisia,))
        snapshot(self.company, T0)
        breakdown = self.only(new_company_signal(self.company, T0))
        self.assertEqual(breakdown.component(GEOGRAPHIC_FIT).evidence,
                         (RegionEvidence(level="municipality", source_id="61190"),))

    def test_industry_templates_leave_no_trace_in_the_breakdown(self):
        self.radar(kads=(self.r.kad_2026,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        before = self.only(signal, as_of=FRESH)
        create_industry_template(slug="tech", name="Τεχνολογία", kads=(self.r.kad_other,), active=True)
        self.assertEqual(self.only(signal, as_of=FRESH), before)
        self.assertNotIn("template", repr(before).lower())

    def test_contact_information_changes_neither_score_nor_breakdown(self):
        self.radar(kads=(self.r.kad_2026,), regions=(self.r.attica,))
        without = make_company("400700")
        without.raw_data, without.email = {"activities": []}, ""
        without.save()
        snapshot(self.company, T0)
        snapshot(without, T0)
        with_phone = self.only(new_company_signal(self.company, T0), as_of=FRESH)
        no_phone = self.only(new_company_signal(without, T0), as_of=FRESH)
        self.assertEqual(with_phone.score, no_phone.score)
        self.assertEqual([dataclasses.replace(c) for c in with_phone.components],
                         [dataclasses.replace(c) for c in no_phone.components])
        self.assertTrue(self.company.gemi_phones and not without.gemi_phones)

    def test_breakdowns_are_deterministic(self):
        self.radar(**self.everything())
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        first, second = self.breakdowns(signal, as_of=FRESH), self.breakdowns(signal, as_of=FRESH)
        self.assertEqual(first, second)
        self.assertEqual(repr(first), repr(second))

    def test_shadow_and_live_explain_identically(self):
        self.radar(kads=(self.r.kad_2026,), signal_types=("new_company",))
        live_company = make_company("400800")
        snapshot(self.company, T0)
        snapshot(live_company, T0)
        shadow = self.only(new_company_signal(self.company, T0, mode=SHADOW), as_of=FRESH)
        live = self.only(new_company_signal(live_company, T0, mode=LIVE), as_of=FRESH)
        self.assertEqual([self.lines(shadow)], [self.lines(live)])


# --- invariants -----------------------------------------------------------------------------------

class InvariantTests(BreakdownTestCase):
    def test_the_invariant_sweep_over_radar_shapes_and_freshness_bands(self):
        combinations = (
            dict(kads=(self.r.kad_2026,)), dict(regions=(self.r.attica,)), dict(legal_forms=(self.r.ike,)),
            dict(signal_types=("new_company",)), dict(kads=(self.r.kad_2026,), legal_forms=(self.r.ike,)),
            self.everything(), self.everything(legal_forms=()),
        )
        for index, criteria in enumerate(combinations):
            self.radar(name=f"r{index}", **criteria)
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        classes, checked = set(), 0
        for as_of in (T0, T0 + timedelta(hours=25), T0 + timedelta(days=10), T0 + timedelta(days=20), STALE):
            scores = {s.radar_id: s for s in self.scores(signal, as_of=as_of)}
            for breakdown in self.breakdowns(signal, as_of=as_of):
                score = scores[breakdown.radar_id]
                self.assertEqual(len(breakdown.components), 5)
                self.assertEqual(tuple(c.code for c in breakdown.components), COMPONENT_ORDER)
                self.assertEqual([c.max_points for c in breakdown.components], [30, 25, 15, 10, 20])
                self.assertEqual(breakdown.max_points, 100)
                self.assertEqual(sum(c.awarded_points for c in breakdown.components), score.score)
                self.assertEqual(dict(score.component_points),
                                 {c.code: c.awarded_points for c in breakdown.components})
                self.assertEqual(breakdown.score_class, score_class_for(score.score))
                self.assertEqual((breakdown.scoring_rule_version, breakdown.match_rule_version),
                                 (OPPORTUNITY_SCORE_RULE_VERSION, ORGANIZATION_RADAR_MATCH_RULE_VERSION))
                self.assertEqual((breakdown.signal_id, breakdown.company_id, breakdown.organization_id),
                                 (score.signal_id, score.company_id, score.organization_id))
                self.assertEqual(breakdown.as_of, as_of)
                self.assertEqual(breakdown.as_of, score.as_of)
                for component in breakdown.components:
                    self.assertTrue(0 <= component.awarded_points <= component.max_points)
                    self.assertIn(component.awarded_points, (0, component.max_points)
                                  if component.code != FRESHNESS else (0, 5, 10, 15, 18, 20))
                    self.assertEqual(component.label, COMPONENT_LABELS[component.code])
                classes.add(breakdown.score_class)
                checked += 1
        self.assertGreaterEqual(checked, 25)
        self.assertEqual(classes, {PRIORITY, HIGH, MEDIUM, LOW})

    def test_a_breakdown_that_would_disagree_with_the_score_is_refused(self):
        radar = self.radar(**self.everything())
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        parts = self.parts(signal, radar)
        score = calculate_opportunity_score(parts["scoring_context"], as_of=FRESH)
        self.assertEqual(build_opportunity_score_breakdown(score=score, **parts).score, 100)
        for tampered in (
            dataclasses.replace(score, score=99),                                  # sum no longer matches
            dataclasses.replace(score, industry_fit_points=0),                     # component disagrees
            dataclasses.replace(score, score_class=LOW),                           # class does not follow
            dataclasses.replace(score, freshness_points=5),                        # not the band of this age
            dataclasses.replace(score, scoring_rule_version="opportunity_score:v2"),
            dataclasses.replace(score, match_rule_version="organization_radar_match:v2"),
            dataclasses.replace(score, radar_id=score.radar_id + 1),               # different match
            dataclasses.replace(score, as_of=STALE),                               # freshness would be stale
        ):
            with self.assertRaises(ScoreBreakdownError):
                build_opportunity_score_breakdown(score=tampered, **parts)
        for bad in (None, 100, score.component_points):
            with self.assertRaises(ScoreBreakdownError):
                build_opportunity_score_breakdown(score=bad, **parts)
            with self.assertRaises(ScoreBreakdownError):
                build_opportunity_score_breakdown(score=score, **{**parts, "radar_definition": bad})

    def test_awarded_points_require_a_criterion_that_c5_confirmed(self):
        radar = self.radar(kads=(self.r.kad_2026,))
        other = self.radar(name="other", kads=(self.r.kad_other,), regions=(self.r.attica,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        parts = self.parts(signal, radar)
        score = calculate_opportunity_score(parts["scoring_context"], as_of=FRESH)
        foreign = get_organization_radar_definition(self.org, other)
        with self.assertRaises(ScoreBreakdownError):  # 30 industry points but this Radar's KAD was not confirmed
            build_opportunity_score_breakdown(score=score, **{**parts, "radar_definition": foreign})

    def test_the_c5_helper_agrees_with_the_matched_dimensions(self):
        radar = self.radar(**self.everything())
        snapshot(self.company, T0)
        report = explain_organization_radar_matches(new_company_signal(self.company, T0))
        definition = load_radar_definitions([radar.pk])[radar.pk][1]
        evaluation = evaluate_organization_radar(definition, report.context)
        confirmed = confirmed_criteria(definition, report.context)
        self.assertEqual({dimension for dimension, values in confirmed.items() if values},
                         set(evaluation.matched_dimensions) - {"signal_type"})
        with self.assertNumQueries(0):
            confirmed_criteria(definition, report.context)

    def test_the_builder_runs_without_any_query(self):
        radar = self.radar(**self.everything())
        snapshot(self.company, T0)
        parts = self.parts(new_company_signal(self.company, T0), radar)
        score = calculate_opportunity_score(parts["scoring_context"], as_of=FRESH)
        with self.assertNumQueries(0):
            breakdown = build_opportunity_score_breakdown(score=score, **parts)
        self.assertEqual(breakdown.score, 100)

    def test_the_end_to_end_helper_stays_bounded(self):
        criteria = self.everything()
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        for index in range(3):
            self.radar(name=f"few{index}", **criteria)
        with CaptureQueriesContext(connection) as few:
            self.assertEqual(len(self.breakdowns(signal, as_of=FRESH)), 3)
        for index in range(27):
            self.radar(name=f"many{index}", **criteria)
        with CaptureQueriesContext(connection) as many:
            result = self.breakdowns(signal, as_of=FRESH)
        self.assertEqual(len(result), 30)
        self.assertEqual(len(few), 15)  # C5's 9 plus one bounded definition load (6 criterion queries)
        self.assertEqual(len(many), len(few))
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT")
                            for q in few.captured_queries + many.captured_queries))
        self.assertEqual({b.score for b in result}, {100})

    def test_a_signal_without_matches_explains_nothing(self):
        self.radar(kads=(self.r.kad_other,))
        snapshot(self.company, T0)
        self.assertEqual(self.breakdowns(new_company_signal(self.company, T0)), ())


# --- parity, privacy, command -----------------------------------------------------------------------

class ParityTests(BreakdownTestCase):
    def test_explaining_scores_changes_nothing_in_the_legacy_product_or_billing(self):
        user = entitled_user("legacy-breakdown@example.com")
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

        self.radar(**self.everything())
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        with patch("django.core.signing.TimestampSigner.timestamp", return_value=TimestampSigner().timestamp()), \
                patch("gemiapp.ingestion.refresh.get_gemi_client") as client:
            before = world()
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.only(signal, as_of=FRESH).score, 100)
            self.assertEqual(world(), before)
        client.assert_not_called()
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries.captured_queries))
        self.assertEqual(self.company.monitoring.reasons.count(), 0)

    def test_the_deployed_phone_bridge_is_intact(self):
        self.assertEqual([p.display for p in extract_company_contact_phones(
            {"phone": "2109990001", "persons": [{"phone": "6999990002"}]})], ["2109990001"])
        self.assertEqual([p.tel_uri for p in Company(gemi_number="1", raw_data={"phone": "+30 2109990001"}).gemi_phones],
                         ["tel:+302109990001"])


class PrivacyAndCommandTests(BreakdownTestCase):
    def test_no_private_fact_reaches_the_breakdown_repr_or_command_output(self):
        self.radar(**self.everything())
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        breakdown = self.only(signal, as_of=FRESH)
        out = StringIO()
        call_command("show_organization_radar_score", signal_id=signal.pk, as_of=FRESH.isoformat(), breakdown=True,
                     stdout=out)
        text = "\n".join([repr(breakdown), str(breakdown.components), out.getvalue()])
        for secret in PRIVATE + ("ΑΤΤΙΚΗΣ", self.company.name, ADDRESS_SENTINEL, "14561"):
            self.assertNotIn(secret, text)
        self.assertIn("score=100/100 class=priority", out.getvalue())
        self.assertIn("industry_fit: 30/30 awarded exact_kad_match [62010000/kad_2026]", out.getvalue())
        self.assertIn("geographic_fit: 15/15 awarded explicit_region_match [prefecture:5]", out.getvalue())
        self.assertIn("legal_form_fit: 10/10 awarded explicit_legal_form_match [legal_type:19]", out.getvalue())
        self.assertIn("freshness: 20/20 awarded fresh_within_24h [age_seconds=7200", out.getvalue())

    def test_the_command_still_works_without_the_breakdown_flag(self):
        self.radar(kads=(self.r.kad_2026,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        out = StringIO()
        call_command("show_organization_radar_score", signal_id=signal.pk, as_of=FRESH.isoformat(), stdout=out)
        self.assertIn("score=50/100 class=low", out.getvalue())
        self.assertIn("industry_fit=30", out.getvalue())
        self.assertNotIn("exact_kad_match", out.getvalue())
        with self.assertRaises(CommandError):
            call_command("show_organization_radar_score", signal_id=signal.pk, as_of="not-a-time", breakdown=True,
                         stdout=StringIO())
