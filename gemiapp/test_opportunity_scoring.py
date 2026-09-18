"""Tests for the opportunity scoring engine (C6, gemiapp.opportunity_scoring).

Fixtures only; scoring is pure and persists nothing. The v1 contract -- KAD fit 30, signal relevance 25,
geographic fit 15, legal form fit 10, freshness 20, total 100 -- is the authoritative one; §30/§32 are blueprint
examples. Contact availability and "company characteristics" are not part of v1 at all.
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

from . import opportunity_scoring as c6
from .company_contact import extract_company_contact_phones
from .company_signals import LIVE, SHADOW
from .industry_templates import create_industry_template
from .ingestion.monitoring import radar_match_evidence
from .ingestion.refresh import RefreshPolicy, build_company_refresh_plan
from .models import (
    Company, CompanyMonitoring, CompanySignal, CompanySnapshot, CustomerRadar, DigestDelivery, OrganizationRadar,
    RadarMatch, UserCompanyLead, UserSubscription,
)
from .opportunity_scoring import (
    FRESHNESS_BANDS, FRESHNESS_MAX_POINTS, GEOGRAPHIC_FIT_POINTS, HIGH, INDUSTRY_FIT_POINTS, LEGAL_FORM_FIT_POINTS,
    LOW, MEDIUM, OPPORTUNITY_SCORE_RULE_VERSION, PRIORITY, SIGNAL_RELEVANCE_POINTS, TOTAL_POINTS, OpportunityScore,
    OpportunityScoringContext, ScoringError, build_opportunity_scoring_context, calculate_opportunity_score,
    freshness_points_for, score_class_for, score_matching_organization_radars, score_organization_radar_match,
)
from .organization_radar_matching import (
    ORGANIZATION_RADAR_MATCH_RULE_VERSION, explain_organization_radar_matches, find_matching_organization_radars,
)
from .services import company_matches_radar, eligible_radars, send_digests
from .test_gemi_company_activities import entitled_user, radar_for
from .test_organization_radar_matching import (
    ADDRESS_SENTINEL, PRIVATE, T0, MatchingTestCase, diff_signal, make_company, new_company_signal, snapshot,
)

STALE = T0 + timedelta(days=40)  # beyond every freshness band
FRESH = T0 + timedelta(hours=2)
SECOND = timedelta(seconds=1)


class ScoringTestCase(MatchingTestCase):
    def scores(self, signal, *, as_of=STALE):
        return score_matching_organization_radars(signal, as_of=as_of)

    def one(self, signal, *, as_of=STALE):
        scores = self.scores(signal, as_of=as_of)
        self.assertEqual(len(scores), 1, scores)
        return scores[0]

    def match_parts(self, signal, radar_id=None):
        report = explain_organization_radar_matches(signal)
        match = next(m for m in report.matches if radar_id in (None, m.radar_id))
        evaluation = next(e.evaluation for e in report.evaluations if e.radar_id == match.radar_id)
        return dict(match=match, evaluation=evaluation, context=report.context)

    def everything(self, **overrides):
        values = dict(kads=(self.r.kad_2026,), regions=(self.r.attica,), legal_forms=(self.r.ike,),
                      signal_types=("new_company",))
        values.update(overrides)
        return values


# --- the v1 contract ------------------------------------------------------------------------------

class ContractTests(TestCase):
    def test_the_v1_weights_total_exactly_one_hundred(self):
        self.assertEqual(
            [INDUSTRY_FIT_POINTS, SIGNAL_RELEVANCE_POINTS, GEOGRAPHIC_FIT_POINTS, LEGAL_FORM_FIT_POINTS,
             FRESHNESS_MAX_POINTS], [30, 25, 15, 10, 20])
        self.assertEqual(TOTAL_POINTS, 100)

    def test_contact_and_company_characteristics_are_not_components_at_all(self):
        names = [f.name for f in dataclasses.fields(OpportunityScore) if f.name.endswith("_points")]
        self.assertEqual(names, ["industry_fit_points", "signal_relevance_points", "geographic_fit_points",
                                 "legal_form_fit_points", "freshness_points"])
        source = inspect.getsource(c6)
        for gone in ("available_contact", "company_characteristics", "contactability", "DEFERRED_COMPONENTS",
                     "MAX_ACHIEVABLE_SCORE"):
            self.assertNotIn(gone, source, gone)

    def test_the_freshness_schedule_is_monotonic_with_inclusive_upper_bounds(self):
        self.assertEqual(FRESHNESS_BANDS, (
            (timedelta(hours=24), 20), (timedelta(hours=72), 18), (timedelta(days=7), 15),
            (timedelta(days=14), 10), (timedelta(days=30), 5)))
        expected = [
            (timedelta(0), 20), (timedelta(hours=24), 20), (timedelta(hours=24) + SECOND, 18),
            (timedelta(hours=72), 18), (timedelta(hours=72) + SECOND, 15), (timedelta(days=7), 15),
            (timedelta(days=7) + SECOND, 10), (timedelta(days=14), 10), (timedelta(days=14) + SECOND, 5),
            (timedelta(days=30), 5), (timedelta(days=30) + SECOND, 0), (timedelta(days=365), 0),
        ]
        self.assertEqual([freshness_points_for(age) for age, _ in expected], [points for _, points in expected])
        previous = FRESHNESS_MAX_POINTS
        for age, points in expected:
            self.assertLessEqual(points, previous)
            previous = points
        for invalid in (-SECOND, 3600, None, "1d"):
            with self.assertRaises(ScoringError):
                freshness_points_for(invalid)

    def test_section_31_classes_and_boundaries(self):
        self.assertEqual([score_class_for(s) for s in (100, 90, 89, 75, 74, 55, 54, 0)],
                         [PRIORITY, PRIORITY, HIGH, HIGH, MEDIUM, MEDIUM, LOW, LOW])
        for floor, name, below in ((90, PRIORITY, HIGH), (75, HIGH, MEDIUM), (55, MEDIUM, LOW)):
            self.assertEqual(score_class_for(floor - 1), below, floor)
            self.assertEqual(score_class_for(floor), name, floor)
            self.assertEqual(score_class_for(floor + 1), name, floor)
        for invalid in (-1, 101, 55.0, True, "55", None):
            with self.assertRaises(ScoringError):
                score_class_for(invalid)

    def test_rule_version_is_pinned_and_results_are_immutable(self):
        self.assertEqual(OPPORTUNITY_SCORE_RULE_VERSION, "opportunity_score:v1")
        for cls in (OpportunityScore, OpportunityScoringContext):
            self.assertTrue(dataclasses.is_dataclass(cls) and cls.__dataclass_params__.frozen, cls)
        names = {f.name for f in dataclasses.fields(OpportunityScoringContext)} | {
            f.name for f in dataclasses.fields(OpportunityScore)}
        for forbidden in ("name", "phone", "email", "raw", "person", "address", "city", "postal", "vat", "user",
                          "subscription", "tier", "threshold", "reason", "explanation", "age", "capital"):
            self.assertFalse([n for n in names if forbidden in n], forbidden)

    def test_no_score_or_opportunity_table_and_no_migration(self):
        from django.apps import apps

        model_names = {model.__name__ for model in apps.get_app_config("gemiapp").get_models()}
        self.assertFalse([n for n in model_names if "Score" in n or "Opportunit" in n])
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(max(name for app, name in loader.disk_migrations if app == "gemiapp"),
                         "0047_industry_template")

    def test_the_engine_writes_nothing_and_reads_no_forbidden_source(self):
        code = inspect.getsource(c6).split('"""', 2)[2]
        for forbidden in (".save(", ".create(", ".update(", ".delete(", "bulk_", "timezone.now(", "random",
                          "CustomerRadar", "RadarMatch", "UserCompanyLead", "CompanyMonitoring", "CompanyActivity",
                          "raw_data", "gemi_phones", "phone", "email", "person", "IndustryTemplate",
                          "industry_template", "OrganizationICP", "organization_icp", "score_threshold",
                          "incorporation", "capital", "is_active", "get_gemi_client", "stripe", "send_mail",
                          "urllib", "requests", "request.user", "session"):
            self.assertNotIn(forbidden, code, forbidden)

    def test_nothing_in_the_product_consumes_scoring(self):
        for path in ("gemiapp/urls.py", "gemiapp/views.py", "config/urls.py", "gemiapp/tasks.py", "gemiapp/apps.py",
                     "gemiapp/services.py", "gemiapp/billing.py", "gemiapp/forms.py", "gemiapp/ingestion/monitoring.py",
                     "gemiapp/ingestion/refresh.py", "gemiapp/company_timeline.py", "gemiapp/organization_radars.py",
                     "gemiapp/organization_radar_matching.py", "gemiapp/industry_templates.py", "config/settings.py",
                     "render.yaml"):
            self.assertNotIn("opportunity_scoring", open(path, encoding="utf-8").read(), path)


# --- fixture matrix -------------------------------------------------------------------------------

class FixtureMatrixTests(ScoringTestCase):
    def test_a_fully_targeted_fresh_match_scores_one_hundred(self):
        radar = self.radar(**self.everything())
        snapshot(self.company, T0)
        score = self.one(new_company_signal(self.company, T0), as_of=FRESH)
        self.assertEqual((score.score, score.score_class), (100, PRIORITY))
        self.assertEqual(dict(score.component_points), {
            "industry_fit": 30, "signal_relevance": 25, "geographic_fit": 15, "legal_form_fit": 10, "freshness": 20})
        self.assertEqual((score.radar_id, score.organization_id, score.company_id, score.signal_id),
                         (radar.pk, self.org.pk, self.company.pk, CompanySignal.objects.get().pk))
        self.assertEqual(score.match_rule_version, ORGANIZATION_RADAR_MATCH_RULE_VERSION)

    def test_each_targeting_component_scores_its_weight_alone_when_stale(self):
        snapshot(self.company, T0)
        cases = {
            "industry": (dict(kads=(self.r.kad_2026,)), INDUSTRY_FIT_POINTS),
            "signal": (dict(signal_types=("new_company",)), SIGNAL_RELEVANCE_POINTS),
            "region": (dict(regions=(self.r.attica,)), GEOGRAPHIC_FIT_POINTS),
            "legal": (dict(legal_forms=(self.r.ike,)), LEGAL_FORM_FIT_POINTS),
        }
        radars = {name: self.radar(name=name, **criteria) for name, (criteria, _) in cases.items()}
        by_radar = {s.radar_id: s for s in self.scores(new_company_signal(self.company, T0))}
        for name, (_, expected) in cases.items():
            score = by_radar[radars[name].pk]
            self.assertEqual((score.score, score.freshness_points), (expected, 0), name)
            self.assertEqual(sum(points for _, points in score.component_points), score.score)

    def test_every_freshness_band_contributes_exactly_its_points(self):
        radar = self.radar(signal_types=("new_company",))  # broad match: only relevance is targeted
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        ages = (timedelta(hours=1), timedelta(hours=24), timedelta(hours=24) + SECOND, timedelta(hours=72),
                timedelta(hours=72) + SECOND, timedelta(days=7), timedelta(days=7) + SECOND, timedelta(days=14),
                timedelta(days=14) + SECOND, timedelta(days=30), timedelta(days=30) + SECOND)
        expected = (20, 20, 18, 18, 15, 15, 10, 10, 5, 5, 0)
        got = []
        for age in ages:
            score = self.one(signal, as_of=T0 + age)
            self.assertEqual(score.radar_id, radar.pk)
            self.assertEqual(score.score, SIGNAL_RELEVANCE_POINTS + score.freshness_points)
            got.append(score.freshness_points)
        self.assertEqual(tuple(got), expected)

    def test_components_are_additive(self):
        self.radar(kads=(self.r.kad_2026,), regions=(self.r.attica,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        self.assertEqual(self.one(signal).score, INDUSTRY_FIT_POINTS + GEOGRAPHIC_FIT_POINTS)
        composite = self.one(signal, as_of=FRESH)
        self.assertEqual((composite.score, composite.score_class), (65, MEDIUM))

    def test_all_four_score_classes_are_reachable_from_real_matches(self):
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        everything = self.radar(name="all", **self.everything())
        no_legal = self.radar(name="no-legal", **self.everything(legal_forms=()))
        kad_region = self.radar(name="kad-region", kads=(self.r.kad_2026,), regions=(self.r.attica,))
        kad_only = self.radar(name="kad", kads=(self.r.kad_2026,))
        cases = [
            (everything.pk, FRESH, 100, PRIORITY),
            (everything.pk, T0 + timedelta(days=10), 90, PRIORITY),          # freshness 10
            (no_legal.pk, T0 + timedelta(days=20), 75, HIGH),                # 30+25+15 + freshness 5
            (kad_region.pk, FRESH, 65, MEDIUM),
            (kad_only.pk, STALE, 30, LOW),
        ]
        for radar_id, as_of, expected, expected_class in cases:
            score = next(s for s in self.scores(signal, as_of=as_of) if s.radar_id == radar_id)
            self.assertEqual((score.score, score.score_class), (expected, expected_class), (radar_id, as_of))
        self.assertEqual({PRIORITY, HIGH, MEDIUM, LOW}, {c for *_, c in cases})

    def test_score_threshold_never_changes_the_score(self):
        unset = self.radar(name="a", kads=(self.r.kad_2026,), score_threshold=None)
        strict = self.radar(name="b", kads=(self.r.kad_2026,), score_threshold=90)
        snapshot(self.company, T0)
        scores = {s.radar_id: s for s in self.scores(new_company_signal(self.company, T0), as_of=FRESH)}
        self.assertEqual(scores[unset.pk].score, scores[strict.pk].score)
        self.assertEqual(scores[unset.pk].score_class, scores[strict.pk].score_class)
        self.assertNotIn("threshold", {f.name for f in dataclasses.fields(OpportunityScore)})
        self.assertFalse([n for n in dir(scores[unset.pk]) if "meets" in n])

    def test_a_broad_radar_matches_but_earns_no_signal_relevance(self):
        previous, current = snapshot(self.company, T0), snapshot(self.company, T0 + timedelta(days=1))
        declared = self.radar(name="declared", kads=(self.r.kad_2026,),
                              signal_types=("status_changed", "kad_added", "new_company"))
        broad = self.radar(name="any", kads=(self.r.kad_2026,))
        seen = {}
        for signal_type in ("status_changed", "kad_added"):
            signal = diff_signal(self.company, previous, current, T0 + timedelta(days=2), signal_type=signal_type)
            seen[signal_type] = {s.radar_id: s.score for s in self.scores(signal)}
        self.assertEqual(seen["status_changed"], seen["kad_added"])  # no per-type ranking
        self.assertEqual(seen["status_changed"][declared.pk], INDUSTRY_FIT_POINTS + SIGNAL_RELEVANCE_POINTS)
        self.assertEqual(seen["status_changed"][broad.pk], INDUSTRY_FIT_POINTS)
        self.assertEqual(len(seen["status_changed"]), 2)  # both still matched

    def test_freshness_uses_detected_at_and_an_explicit_as_of(self):
        self.radar(legal_forms=(self.r.ike,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        with self.assertRaises(ScoringError):
            self.scores(signal, as_of=T0 - SECOND)
        with self.assertRaises(ScoringError):
            self.scores(signal, as_of=datetime(2026, 9, 18, 9))  # naive
        parts = self.match_parts(signal)
        self.assertNotIn("effective", str(build_opportunity_scoring_context(**parts)))

    def test_company_age_and_invalid_incorporation_dates_never_score(self):
        self.radar(kads=(self.r.kad_2026,))
        snapshot(self.company, T0)
        baseline = self.one(new_company_signal(self.company, T0), as_of=FRESH).score
        CompanySnapshot.objects.update(incorporation_date=date(1800, 1, 1), incorporation_date_quality="out_of_range")
        young = make_company("400900")
        snapshot(young, T0)
        CompanySnapshot.objects.filter(company=young).update(incorporation_date=date(2026, 9, 9),
                                                             incorporation_date_quality="valid")
        missing = make_company("400901")
        snapshot(missing, T0)
        CompanySnapshot.objects.filter(company=missing).update(incorporation_date=None,
                                                               incorporation_date_quality="missing")
        for company in (self.company, young, missing):
            score = self.one(new_company_signal(company, T0 + timedelta(minutes=1)), as_of=FRESH)
            self.assertEqual(score.score, baseline, company.gemi_number)

    def test_no_industry_points_across_kad_versions(self):
        self.radar(name="2008", kads=(self.r.kad_2008,), legal_forms=(self.r.ike,))
        matching_radar = self.radar(name="2026", kads=(self.r.kad_2026,))
        snapshot(self.company, T0)
        scores = self.scores(new_company_signal(self.company, T0))
        self.assertEqual([(s.radar_id, s.score) for s in scores], [(matching_radar.pk, INDUSTRY_FIT_POINTS)])

    def test_industry_templates_create_no_industry_fit(self):
        self.radar(regions=(self.r.attica,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        before = self.one(signal)
        create_industry_template(slug="tech", name="Τεχνολογία", kads=(self.r.kad_2026,), active=True)
        after = self.one(signal)
        self.assertEqual(after, before)
        self.assertEqual((after.industry_fit_points, after.score), (0, GEOGRAPHIC_FIT_POINTS))

    def test_contact_information_has_no_effect_on_the_score(self):
        self.radar(kads=(self.r.kad_2026,))
        without = make_company("400500")
        without.raw_data = {"activities": []}
        without.email = ""
        without.save()
        snapshot(self.company, T0)
        snapshot(without, T0)
        with_phone = self.one(new_company_signal(self.company, T0), as_of=FRESH)
        no_phone = self.one(new_company_signal(without, T0), as_of=FRESH)
        self.assertEqual(with_phone.score, no_phone.score)
        self.assertEqual(dict(with_phone.component_points), dict(no_phone.component_points))
        self.assertTrue(self.company.gemi_phones and not without.gemi_phones)

    def test_shadow_and_live_signals_score_identically(self):
        self.radar(kads=(self.r.kad_2026,), signal_types=("new_company",))
        live_company = make_company("400600")
        snapshot(self.company, T0)
        snapshot(live_company, T0)
        shadow = self.one(new_company_signal(self.company, T0, mode=SHADOW), as_of=FRESH)
        live = self.one(new_company_signal(live_company, T0, mode=LIVE), as_of=FRESH)
        self.assertEqual((shadow.score, shadow.score_class), (live.score, live.score_class))

    def test_scoring_is_deterministic_and_repeatable(self):
        self.radar(**self.everything())
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        first, second = self.scores(signal, as_of=FRESH), self.scores(signal, as_of=FRESH)
        self.assertEqual(first, second)
        self.assertEqual(repr(first), repr(second))
        parts = self.match_parts(signal)
        self.assertEqual(score_organization_radar_match(**parts, as_of=FRESH), first[0])


# --- invariants and input contract ----------------------------------------------------------------

class InvariantTests(ScoringTestCase):
    def test_scores_are_whole_numbers_inside_the_scale_and_agree_with_their_class(self):
        combinations = (
            dict(kads=(self.r.kad_2026,)), dict(regions=(self.r.attica,)), dict(legal_forms=(self.r.ike,)),
            dict(signal_types=("new_company",)), dict(kads=(self.r.kad_2026,), legal_forms=(self.r.ike,)),
            self.everything(),
        )
        for index, criteria in enumerate(combinations):
            self.radar(name=f"r{index}", **criteria)
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        seen = set()
        for as_of in (T0, T0 + timedelta(hours=25), T0 + timedelta(days=10), STALE):
            for score in self.scores(signal, as_of=as_of):
                self.assertIsInstance(score.score, int)
                self.assertNotIsInstance(score.score, bool)
                self.assertTrue(0 <= score.score <= 100)
                self.assertEqual(sum(points for _, points in score.component_points), score.score)
                self.assertEqual(score.score_class, score_class_for(score.score))
                self.assertEqual(score.scoring_rule_version, OPPORTUNITY_SCORE_RULE_VERSION)
                seen.add(score.score_class)
        self.assertEqual(seen, {PRIORITY, HIGH, MEDIUM, LOW})

    def test_only_a_confirmed_match_of_the_same_event_is_scored(self):
        radar = self.radar(kads=(self.r.kad_2026,))
        needs_state = self.radar(name="needs", kads=(self.r.kad_2026,), legal_forms=(self.r.oe,))
        snapshot(self.company, T0, legal=None)
        signal = new_company_signal(self.company, T0)
        report = explain_organization_radar_matches(signal)
        insufficient = next(e.evaluation for e in report.evaluations if e.radar_id == needs_state.pk)
        match = next(m for m in report.matches if m.radar_id == radar.pk)
        with self.assertRaises(ScoringError):  # INSUFFICIENT_STATE is not a low score
            build_opportunity_scoring_context(match=match, evaluation=insufficient, context=report.context)
        other = explain_organization_radar_matches(new_company_signal(self.company, T0 + timedelta(minutes=5)))
        with self.assertRaises(ScoringError):  # a match from a different event
            build_opportunity_scoring_context(match=match, evaluation=insufficient, context=other.context)
        for bad in (None, {"radar_id": 1}, radar, signal):
            with self.assertRaises(ScoringError):
                build_opportunity_scoring_context(match=bad, evaluation=insufficient, context=report.context)
            with self.assertRaises(ScoringError):
                calculate_opportunity_score(bad, as_of=STALE)
        stale_rule = dataclasses.replace(match, match_rule_version="organization_radar_match:v0")
        evaluation = next(e.evaluation for e in report.evaluations if e.radar_id == radar.pk)
        with self.assertRaises(ScoringError):
            build_opportunity_scoring_context(match=stale_rule, evaluation=evaluation, context=report.context)

    def test_the_calculator_and_builder_run_without_any_query(self):
        self.radar(kads=(self.r.kad_2026,), regions=(self.r.attica,))
        snapshot(self.company, T0)
        parts = self.match_parts(new_company_signal(self.company, T0))
        with self.assertNumQueries(0):
            context = build_opportunity_scoring_context(**parts)
            score = calculate_opportunity_score(context, as_of=FRESH)
        self.assertEqual(score.score, INDUSTRY_FIT_POINTS + GEOGRAPHIC_FIT_POINTS + FRESHNESS_MAX_POINTS)

    def test_batch_scoring_reuses_one_matching_pass(self):
        criteria = self.everything()
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        for index in range(3):
            self.radar(name=f"few{index}", **criteria)
        with CaptureQueriesContext(connection) as few:
            self.assertEqual(len(self.scores(signal, as_of=FRESH)), 3)
        for index in range(27):
            self.radar(name=f"many{index}", **criteria)
        with CaptureQueriesContext(connection) as many:
            scores = self.scores(signal, as_of=FRESH)
        self.assertEqual(len(scores), 30)
        self.assertEqual(len(few), 9)  # C5's bounded pass: signal, snapshot, candidates, radars, criterion tables
        self.assertEqual(len(many), len(few))
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT")
                            for q in few.captured_queries + many.captured_queries))
        self.assertEqual({score.score for score in scores}, {100})


# --- parity, privacy, command ----------------------------------------------------------------------

class ParityTests(ScoringTestCase):
    def test_scoring_changes_nothing_in_the_legacy_product_monitoring_b4_or_billing(self):
        user = entitled_user("legacy-score@example.com")
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
                self.assertEqual(self.one(signal, as_of=FRESH).score, 100)
            self.assertEqual(world(), before)
        client.assert_not_called()
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries.captured_queries))
        self.assertEqual(self.company.monitoring.reasons.count(), 0)
        self.assertEqual(find_matching_organization_radars(signal)[0].match_rule_version,
                         ORGANIZATION_RADAR_MATCH_RULE_VERSION)

    def test_the_deployed_phone_bridge_is_intact(self):
        self.assertEqual([p.display for p in extract_company_contact_phones(
            {"phone": "2109990001", "persons": [{"phone": "6999990002"}]})], ["2109990001"])
        self.assertEqual([p.tel_uri for p in Company(gemi_number="1", raw_data={"phone": "+30 2109990001"}).gemi_phones],
                         ["tel:+302109990001"])


class PrivacyAndCommandTests(ScoringTestCase):
    def test_no_private_fact_reaches_the_context_score_repr_or_command(self):
        self.radar(kads=(self.r.kad_2026,), regions=(self.r.attica,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        parts = self.match_parts(signal)
        context = build_opportunity_scoring_context(**parts)
        score = calculate_opportunity_score(context, as_of=FRESH)
        out = StringIO()
        call_command("show_organization_radar_score", signal_id=signal.pk, as_of=FRESH.isoformat(), stdout=out)
        text = "\n".join([repr(context), repr(score), out.getvalue()])
        for secret in PRIVATE + ("ΑΤΤΙΚΗΣ", self.company.name, ADDRESS_SENTINEL, "14561"):
            self.assertNotIn(secret, text)
        self.assertIn("score=65/100 class=medium", out.getvalue())
        self.assertIn("industry_fit=30 signal_relevance=0 geographic_fit=15 legal_form_fit=0 freshness=20",
                      out.getvalue())

    def test_the_command_is_read_only_and_needs_an_explicit_as_of(self):
        self.radar(kads=(self.r.kad_2026,))
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0)
        for kwargs in ({"signal_id": signal.pk, "as_of": "2026-09-18T09:00:00"},  # naive
                       {"signal_id": signal.pk, "as_of": "yesterday"},
                       {"signal_id": 999999, "as_of": STALE.isoformat()},
                       {"signal_id": signal.pk, "as_of": (T0 - timedelta(days=1)).isoformat()}):
            with self.assertRaises(CommandError):
                call_command("show_organization_radar_score", stdout=StringIO(), **kwargs)
        out = StringIO()
        with CaptureQueriesContext(connection) as queries:
            call_command("show_organization_radar_score", signal_id=signal.pk, as_of=STALE.isoformat(),
                         radar_id=999, stdout=out)
        self.assertIn("scored=0", out.getvalue())
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries.captured_queries))
