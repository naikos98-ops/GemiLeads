"""Tests for the monitored company refresh collector (B4, gemiapp.ingestion.refresh).

Every execution test drives a mocked client. B4 never calls the live GEMI API here or anywhere else.
"""

import json
from datetime import date, datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import CreateModel
from django.test import TestCase
from django.utils import timezone

from .company_snapshots import record_company_snapshot
from .ingestion.errors import (
    GemiAuthenticationError,
    GemiBudgetTimeoutError,
    GemiResponseValidationError,
    GemiRetryExhaustedError,
)
from .ingestion.monitoring import (
    ACTIVE_OPPORTUNITY,
    ACTIVE_RADAR_MATCH,
    DEFAULT_POLICY as MONITORING_POLICY,
    MANUAL,
    NEW_COMPANY,
    RECENT_SIGNAL,
    compute_next_check_at,
)
from .ingestion.normalizer import normalize_company
from .ingestion.rate_budget import GemiLane
from .ingestion.refresh import (
    CAP_DETAILS,
    CAP_REQUESTS,
    DEFERRED_NO_STRATEGY,
    DIRECT_DETAIL_REASONS,
    FAILED,
    PARTIAL,
    REFRESH_LANE,
    SUCCESS,
    RefreshPlanError,
    RefreshPolicy,
    build_company_refresh_plan,
    due_monitoring_targets,
    policy_from_settings,
    radar_search_params,
    run_company_refresh,
)
from .ingestion.schemas import ResponseFamily
from .models import (
    Company, CompanyActivity, CompanyMonitoring, CompanyMonitoringReason, CompanySignal, CompanySnapshot,
    CustomerRadar, GemiLegalType, GemiPrefecture, GemiRefreshRun, RadarMatch, UserCompanyLead,
    UserSubscription,
)
from .services import company_matches_radar, import_for_date, match_imported_companies, send_digests
from .test_gemi_company_activities import entitled_user, radar_for
from .test_gemi_validation import CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL, full_item, page

RUN_AT = datetime(2026, 9, 16, 9, 0, tzinfo=dt_timezone.utc)
OBSERVED = datetime(2026, 9, 16, 9, 0, 30, tzinfo=dt_timezone.utc)
PREFECTURE = "ΑΤΤΙΚΗΣ"
LEGAL_TYPE = "ΙΚΕ"
CODE_A = "47191002"
CODE_B = "35111000"

POLICY = RefreshPolicy(max_requests_per_run=10, max_pages_per_query=3, max_direct_details_per_run=3, page_size=2)


def make_company(gemi_number, **fields):
    values = {
        "name": "ΕΤΑΙΡΕΙΑ ΙΚΕ", "incorporation_date": date(2026, 9, 14), "prefecture": PREFECTURE,
        "legal_type": LEGAL_TYPE, "is_active": True,
    }
    values.update(fields)
    return Company.objects.create(gemi_number=gemi_number, **values)


def monitor(company, *, reasons=(NEW_COMPANY,), priority="normal", next_check_at=None, radar_ids=(),
            state="active", monitored_since=None):
    monitored_since = monitored_since or RUN_AT - timedelta(days=10)
    row = CompanyMonitoring.objects.create(
        company=company, state=state, priority=priority, primary_reason=reasons[0] if reasons else None,
        policy_version=MONITORING_POLICY.version, monitored_since=monitored_since,
        next_check_at=RUN_AT - timedelta(hours=1) if next_check_at is None else next_check_at,
    )
    for reason in reasons:
        CompanyMonitoringReason.objects.create(
            monitoring=row, reason=reason, active=True, first_active_at=monitored_since,
            activated_at=monitored_since, activation_count=1,
            source_ids=list(radar_ids) if reason == ACTIVE_RADAR_MATCH else [],
        )
    return row


def reference_entry(model, source_id, description):
    return model.objects.create(
        source_id=source_id, description=description, first_seen_at=RUN_AT - timedelta(days=30),
        last_seen_at=RUN_AT,
    )


def reference_data():
    reference_entry(GemiPrefecture, "5", PREFECTURE)
    reference_entry(GemiLegalType, "19", LEGAL_TYPE)


class FakeClient:
    """Stands in for GemiClient. Records every call so the tests can prove the lane and the endpoint."""

    def __init__(self, *, searches=None, details=None, clock_start=OBSERVED, step=timedelta(minutes=1)):
        self.searches = list(searches or [])
        self.details = dict(details or {})
        self.calls = []
        self._now = clock_start
        self._step = step

    def search_companies(self, params, *, lane, max_wait=None):
        self.calls.append(("search", dict(params), lane))
        result = self.searches.pop(0) if self.searches else page()
        if isinstance(result, Exception):
            raise result
        return result

    def get(self, path, params=None, *, lane, max_wait=None, family=None):
        self.calls.append((path, params, lane, family))
        result = self.details.get(path, page())
        if isinstance(result, Exception):
            raise result
        return result

    def clock(self):
        value = self._now
        self._now = self._now + self._step
        return value


class PolicyTests(TestCase):
    def test_every_cap_comes_from_settings_and_is_validated(self):
        policy = policy_from_settings()
        self.assertEqual(policy.max_requests_per_run, 20)
        self.assertEqual(policy.max_pages_per_query, 5)
        self.assertEqual(policy.max_direct_details_per_run, 5)
        self.assertEqual(policy.page_size, 200)
        for values in ({"max_requests_per_run": 0}, {"max_pages_per_query": 0},
                       {"max_direct_details_per_run": 0}, {"page_size": 0}, {"page_size": 201}):
            with self.assertRaises(RefreshPlanError):
                RefreshPolicy(**values)

    def test_the_collector_uses_the_monitored_refresh_lane_never_discovery(self):
        self.assertEqual(REFRESH_LANE, GemiLane.MONITORED_REFRESH)
        self.assertNotEqual(REFRESH_LANE, GemiLane.DISCOVERY)

    def test_manual_never_qualifies_for_a_direct_request(self):
        self.assertEqual(DIRECT_DETAIL_REASONS, (ACTIVE_OPPORTUNITY, RECENT_SIGNAL))
        self.assertNotIn(MANUAL, DIRECT_DETAIL_REASONS)
        self.assertNotIn(NEW_COMPANY, DIRECT_DETAIL_REASONS)


class DueSelectionTests(TestCase):
    def test_no_due_companies_is_an_empty_plan_not_an_error(self):
        monitor(make_company("1"), next_check_at=RUN_AT + timedelta(days=1))
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        self.assertEqual(plan.due_companies, 0)
        self.assertEqual((plan.search_groups, plan.detail_targets, plan.deferred), ((), (), ()))
        self.assertEqual(plan.estimated_requests, 0)

    def test_spare_capacity_never_pulls_in_a_company_that_is_not_due(self):
        monitor(make_company("1"), next_check_at=RUN_AT - timedelta(minutes=1))
        monitor(make_company("2"), next_check_at=RUN_AT + timedelta(seconds=1))
        self.assertEqual([t.gemi_number for t in due_monitoring_targets(run_at=RUN_AT)], ["1"])

    def test_inactive_monitoring_is_never_due(self):
        company = make_company("1")
        CompanyMonitoring.objects.create(
            company=company, state="inactive", policy_version=1, monitored_since=RUN_AT - timedelta(days=90),
            inactive_since=RUN_AT - timedelta(days=1),
        )
        self.assertEqual(due_monitoring_targets(run_at=RUN_AT), [])

    def test_ordering_is_priority_then_oldest_due_then_stable_identity(self):
        monitor(make_company("300"), priority="normal", next_check_at=RUN_AT - timedelta(days=9))
        monitor(make_company("200"), priority="high", next_check_at=RUN_AT - timedelta(hours=1))
        monitor(make_company("100"), priority="critical", next_check_at=RUN_AT - timedelta(minutes=1))
        monitor(make_company("050"), priority="high", next_check_at=RUN_AT - timedelta(days=2))
        monitor(make_company("040"), priority="high", next_check_at=RUN_AT - timedelta(days=2))
        order = [t.gemi_number for t in due_monitoring_targets(run_at=RUN_AT)]
        self.assertEqual(order, ["100", "040", "050", "200", "300"])
        self.assertEqual(order, [t.gemi_number for t in due_monitoring_targets(run_at=RUN_AT)])


class QueryTranslationTests(TestCase):
    def setUp(self):
        self.user = entitled_user()

    def test_only_exactly_translatable_criteria_are_sent(self):
        reference_data()
        radar = radar_for(self.user, "r", codes=[CODE_A], prefectures=[PREFECTURE], legal_types=[LEGAL_TYPE])
        self.assertEqual(dict(radar_search_params(radar)), {
            "activities": CODE_A, "prefectures": "5", "legalTypes": "19", "isActive": "true",
        })

    def test_an_unresolvable_description_drops_its_criterion_and_never_guesses_an_id(self):
        radar = radar_for(self.user, "r", codes=[CODE_A], prefectures=[PREFECTURE])
        params = dict(radar_search_params(radar))
        self.assertEqual(params, {"activities": CODE_A, "isActive": "true"})
        self.assertNotIn("prefectures", params)

    def test_kad_prefixes_that_are_not_gemi_ids_are_never_sent(self):
        reference_data()
        radar = radar_for(self.user, "r", codes=["5229"], prefectures=[PREFECTURE])
        params = dict(radar_search_params(radar))
        self.assertNotIn("activities", params)
        self.assertEqual(params["prefectures"], "5")

    def test_a_partially_translatable_kad_set_is_dropped_whole(self):
        radar = radar_for(self.user, "r", codes=[CODE_A, "5229"], prefectures=[PREFECTURE])
        reference_data()
        self.assertNotIn("activities", dict(radar_search_params(radar)))

    def test_the_name_criterion_is_never_sent_upstream(self):
        reference_data()
        radar = radar_for(self.user, "r", prefectures=[PREFECTURE], name_query="ΔΟΚΙΜΗ")
        self.assertNotIn("name", dict(radar_search_params(radar)))

    def test_a_radar_with_nothing_selective_is_untranslatable(self):
        self.assertIsNone(radar_search_params(radar_for(self.user, "r")))

    def test_a_group_query_pages_deterministically_by_gemi_number(self):
        reference_data()
        radar = radar_for(self.user, "r", codes=[CODE_A])
        for index in range(4):
            company = make_company(f"e{index}")
            monitor(company, reasons=(ACTIVE_RADAR_MATCH,), priority="high", radar_ids=[radar.pk])
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        query = plan.search_groups[0].query(page_size=2, offset=4)
        self.assertEqual(query["resultsSortBy"], "+arGemi")
        self.assertEqual((query["resultsSize"], query["resultsOffset"]), (2, 4))


class PlannerTests(TestCase):
    def setUp(self):
        self.user = entitled_user()
        reference_data()

    def radar_target(self, gemi_number, radar, **fields):
        company = make_company(gemi_number, **fields)
        monitor(company, reasons=(ACTIVE_RADAR_MATCH,), priority="high", radar_ids=[radar.pk])
        return company

    def test_identical_criteria_produce_one_group_and_one_upstream_search(self):
        first = radar_for(self.user, "a", codes=[CODE_A], prefectures=[PREFECTURE])
        second = radar_for(self.user, "b", codes=[CODE_A], prefectures=[PREFECTURE])
        for index in range(4):
            company = make_company(f"1{index}")
            monitor(company, reasons=(ACTIVE_RADAR_MATCH,), priority="high", radar_ids=[first.pk, second.pk])
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        self.assertEqual(len(plan.search_groups), 1)
        self.assertEqual(plan.search_groups[0].radar_ids, tuple(sorted((first.pk, second.pk))))
        self.assertEqual(len(plan.search_groups[0].targets), 4)

    def test_groups_differing_only_in_kad_are_merged_into_one_or_query(self):
        first = radar_for(self.user, "a", codes=[CODE_A], prefectures=[PREFECTURE])
        second = radar_for(self.user, "b", codes=[CODE_B], prefectures=[PREFECTURE])
        for index, radar in enumerate([first, second] * 2):
            company = make_company(f"2{index}")
            monitor(company, reasons=(ACTIVE_RADAR_MATCH,), priority="high", radar_ids=[radar.pk])
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        self.assertEqual(len(plan.search_groups), 1)
        self.assertEqual(dict(plan.search_groups[0].params)["activities"], ",".join(sorted([CODE_A, CODE_B])))
        self.assertEqual(dict(plan.search_groups[0].params)["prefectures"], "5")

    def test_criteria_differing_in_two_dimensions_are_never_crossed(self):
        reference_entry(GemiPrefecture, "6", "ΘΕΣΣΑΛΟΝΙΚΗΣ")
        first = radar_for(self.user, "a", codes=[CODE_A], prefectures=[PREFECTURE])
        second = radar_for(self.user, "b", codes=[CODE_B], prefectures=["ΘΕΣΣΑΛΟΝΙΚΗΣ"])
        for index, radar in enumerate([first, second] * 2):
            company = make_company(f"3{index}", prefecture=PREFECTURE if radar is first else "ΘΕΣΣΑΛΟΝΙΚΗΣ")
            monitor(company, reasons=(ACTIVE_RADAR_MATCH,), priority="high", radar_ids=[radar.pk])
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        self.assertEqual(len(plan.search_groups), 2)
        for group in plan.search_groups:
            params = dict(group.params)
            self.assertNotIn(",", params["prefectures"])
            self.assertNotIn(",", params["activities"])

    def test_a_company_matched_by_two_radars_is_one_target_not_two(self):
        reference_entry(GemiPrefecture, "6", "ΘΕΣΣΑΛΟΝΙΚΗΣ")
        first = radar_for(self.user, "a", codes=[CODE_A], prefectures=[PREFECTURE])
        second = radar_for(self.user, "b", codes=[CODE_B], prefectures=["ΘΕΣΣΑΛΟΝΙΚΗΣ"])
        for index in range(4):
            company = make_company(f"4{index}")
            monitor(company, reasons=(ACTIVE_RADAR_MATCH,), priority="high", radar_ids=[first.pk, second.pk])
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        self.assertEqual(plan.planned_companies, 4)
        self.assertEqual(plan.companies_in_multiple_groups, 4)

    def test_new_company_only_targets_are_deferred_with_no_request(self):
        for index in range(3):
            monitor(make_company(f"5{index}"), reasons=(NEW_COMPANY,))
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        self.assertEqual((len(plan.search_groups), len(plan.detail_targets)), (0, 0))
        self.assertEqual(len(plan.deferred), 3)
        self.assertEqual({item.classification for item in plan.deferred}, {DEFERRED_NO_STRATEGY})
        self.assertEqual(plan.estimated_requests, 0)

    def test_a_high_value_reason_without_search_coverage_gets_a_direct_detail(self):
        for reason in DIRECT_DETAIL_REASONS:
            company = make_company(f"6{reason}")
            monitor(company, reasons=(reason,), priority="critical")
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        self.assertEqual({target.reason for target in plan.detail_targets}, set(DIRECT_DETAIL_REASONS))
        self.assertEqual(len(plan.deferred), 0)

    def test_manual_monitoring_never_spends_a_direct_request(self):
        monitor(make_company("7"), reasons=(MANUAL,))
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        self.assertEqual(len(plan.detail_targets), 0)
        self.assertEqual(len(plan.deferred), 1)

    def test_search_coverage_beats_a_detail_request_for_a_multi_reason_company(self):
        radar = radar_for(self.user, "a", codes=[CODE_A], prefectures=[PREFECTURE])
        for index in range(4):
            company = make_company(f"8{index}")
            monitor(company, reasons=(ACTIVE_OPPORTUNITY, ACTIVE_RADAR_MATCH), priority="critical",
                    radar_ids=[radar.pk])
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        self.assertEqual(len(plan.detail_targets), 0)
        self.assertEqual(len(plan.search_groups[0].targets), 4)

    def test_a_group_costing_more_requests_than_its_targets_is_rejected(self):
        radar = radar_for(self.user, "a", prefectures=[PREFECTURE])
        self.radar_target("90", radar)
        for index in range(6):  # local candidates inflate the estimate beyond the single target
            make_company(f"pad{index}")
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        self.assertEqual(len(plan.search_groups), 0)
        self.assertEqual(plan.rejected_groups, 1)
        self.assertEqual(len(plan.deferred), 1)

    def test_an_untranslatable_radar_is_counted_and_its_targets_deferred(self):
        radar = radar_for(self.user, "a")
        self.radar_target("91", radar)
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        self.assertEqual(plan.untranslatable_radars, 1)
        self.assertEqual((len(plan.search_groups), len(plan.deferred)), (0, 1))

    def test_the_direct_detail_cap_truncates_in_plan_order(self):
        for index in range(5):
            company = make_company(f"a{index}")
            monitor(company, reasons=(ACTIVE_OPPORTUNITY,), priority="critical",
                    next_check_at=RUN_AT - timedelta(days=index + 1))
        policy = RefreshPolicy(max_requests_per_run=10, max_pages_per_query=3, max_direct_details_per_run=2,
                               page_size=2)
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=policy)
        self.assertEqual(len(plan.detail_targets), 2)
        self.assertEqual(plan.truncated_by, CAP_DETAILS)
        self.assertEqual([t.gemi_number for t in plan.detail_targets], ["a4", "a3"])

    def test_the_request_cap_truncates_the_plan(self):
        for index in range(4):
            company = make_company(f"b{index}")
            monitor(company, reasons=(RECENT_SIGNAL,), priority="high",
                    next_check_at=RUN_AT - timedelta(days=index + 1))
        policy = RefreshPolicy(max_requests_per_run=2, max_pages_per_query=3, max_direct_details_per_run=9,
                               page_size=2)
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=policy)
        self.assertEqual(len(plan.detail_targets), 2)
        self.assertEqual(plan.truncated_by, CAP_REQUESTS)
        self.assertLessEqual(plan.estimated_requests, 2)

    def test_planning_asks_gemi_nothing(self):
        radar = radar_for(self.user, "a", codes=[CODE_A], prefectures=[PREFECTURE])
        self.radar_target("c0", radar)
        with patch("gemiapp.ingestion.refresh.get_gemi_client") as client:
            build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        client.assert_not_called()

    def test_the_plan_is_reproducible(self):
        radar = radar_for(self.user, "a", codes=[CODE_A], prefectures=[PREFECTURE])
        for index in range(4):
            self.radar_target(f"d{index}", radar)
        first = build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        second = build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        self.assertEqual(first.summary(), second.summary())
        self.assertEqual([g.targets for g in first.search_groups], [g.targets for g in second.search_groups])

    def test_run_at_must_be_timezone_aware(self):
        for value in (datetime(2026, 9, 16, 9, 0), date(2026, 9, 16), None):
            with self.assertRaises(RefreshPlanError):
                build_company_refresh_plan(run_at=value, policy=POLICY)


class ExecutionTestCase(TestCase):
    """Shared fixture: four companies covered by one translatable Radar search group."""

    def setUp(self):
        self.user = entitled_user()
        reference_data()
        self.radar = radar_for(self.user, "a", codes=[CODE_A], prefectures=[PREFECTURE])
        self.companies = {}
        for number in ("100000000001", "100000000002"):
            company = make_company(number)
            monitor(company, reasons=(ACTIVE_RADAR_MATCH,), priority="high", radar_ids=[self.radar.pk])
            self.companies[number] = company

    def plan(self, policy=POLICY):
        return build_company_refresh_plan(run_at=RUN_AT, policy=policy)

    def make_due_again(self):
        """A successful refresh legitimately pushes next_check_at forward, so a second observation in a
        test has to wait for the company to be due again."""
        CompanyMonitoring.objects.update(next_check_at=RUN_AT - timedelta(hours=1))

    def execute(self, client, policy=POLICY, plan=None):
        return run_company_refresh(
            run_at=RUN_AT, policy=policy, client=client, clock=client.clock,
            plan=plan if plan is not None else self.plan(policy),
        )


class Scenario1BaselineTests(ExecutionTestCase):
    def test_one_search_creates_a_baseline_for_each_due_company_and_no_signal(self):
        client = FakeClient(searches=[page(full_item("100000000001"), full_item("100000000002"), total=2)])
        result = self.execute(client)
        self.assertEqual(result.status, SUCCESS)
        self.assertEqual((result.request_count, result.search_requests, result.detail_requests), (1, 1, 0))
        self.assertEqual(result.target_records_observed, 2)
        self.assertEqual((result.baselines_created, result.changed_snapshots_created), (2, 0))
        self.assertEqual(CompanySnapshot.objects.filter(is_baseline=True).count(), 2)
        self.assertEqual(CompanySignal.objects.count(), 0)
        for row in CompanyMonitoring.objects.all():
            self.assertIsNotNone(row.last_success_at)
            self.assertEqual(row.consecutive_failures, 0)
            self.assertGreater(row.next_check_at, RUN_AT)

    def test_the_search_uses_the_monitored_refresh_lane_and_the_search_endpoint(self):
        client = FakeClient(searches=[page(full_item("100000000001"), full_item("100000000002"), total=2)])
        self.execute(client)
        self.assertEqual(len(client.calls), 1)
        kind, params, lane = client.calls[0]
        self.assertEqual((kind, lane), ("search", GemiLane.MONITORED_REFRESH))
        self.assertEqual(params["activities"], CODE_A)

    def test_companies_returned_that_are_not_targets_are_ignored_entirely(self):
        client = FakeClient(searches=[page(
            full_item("999999999999"), full_item("100000000001"), full_item("100000000002"), total=3,
        )])
        result = self.execute(client)
        self.assertEqual(result.records_examined, 3)
        self.assertEqual(result.target_records_observed, 2)
        self.assertEqual(Company.objects.count(), 2)
        self.assertFalse(Company.objects.filter(gemi_number="999999999999").exists())
        self.assertEqual(CompanyMonitoring.objects.count(), 2)

    def test_two_responses_receive_different_observation_times(self):
        client = FakeClient(
            searches=[page(full_item("100000000001"), total=3), page(full_item("100000000002"), total=3)],
            step=timedelta(minutes=5),
        )
        policy = RefreshPolicy(max_requests_per_run=10, max_pages_per_query=3, max_direct_details_per_run=3, page_size=1)
        self.execute(client, policy=policy)
        times = sorted(CompanySnapshot.objects.values_list("observed_at", flat=True))
        self.assertEqual(len(set(times)), 2)
        self.assertEqual(times[1] - times[0], timedelta(minutes=5))

    def test_a3_runs_with_the_observation_date_not_the_run_date(self):
        observed = datetime(2026, 9, 20, 10, 0, tzinfo=dt_timezone.utc)
        client = FakeClient(searches=[page(full_item("100000000001"), total=1)], clock_start=observed)
        self.execute(client)
        snapshot = CompanySnapshot.objects.get()
        self.assertEqual(snapshot.observed_at, observed)
        self.assertEqual(
            snapshot.state_hash,
            __import__("gemiapp.company_snapshots", fromlist=["x"]).company_state_hash(
                __import__("gemiapp.company_snapshots", fromlist=["x"]).build_company_snapshot_state(
                    normalize_company(full_item("100000000001"), as_of=timezone.localdate(observed))
                )
            ),
        )


class Scenario2ChangeTests(ExecutionTestCase):
    def observe(self, *items, clock_start=OBSERVED):
        self.make_due_again()
        client = FakeClient(searches=[page(*items, total=len(items))], clock_start=clock_start)
        return self.execute(client)

    def test_an_unchanged_state_extends_the_span_and_a_changed_one_adds_a_row(self):
        self.observe(full_item("100000000001"), full_item("100000000002"))
        later = OBSERVED + timedelta(days=1)
        result = self.observe(
            full_item("100000000001"),
            full_item("100000000002", status={"id": 8, "descr": "Διαγραφή"}),
            clock_start=later,
        )
        self.assertEqual(result.unchanged_snapshots, 1)
        self.assertEqual(result.changed_snapshots_created, 1)
        self.assertEqual(result.baselines_created, 0)
        first = CompanySnapshot.objects.filter(company=self.companies["100000000001"])
        self.assertEqual(first.count(), 1)
        self.assertEqual(first.get().last_observed_at, later)
        self.assertEqual(CompanySnapshot.objects.filter(company=self.companies["100000000002"]).count(), 2)

    def test_a_changed_snapshot_creates_zero_signals(self):
        self.observe(full_item("100000000001"), full_item("100000000002"))
        before = CompanySignal.objects.count()
        result = self.observe(
            full_item("100000000001", municipality={"id": 61191, "descr": "ΑΛΛΟΣ ΔΗΜΟΣ"}),
            full_item("100000000002", legalType={"id": 20, "descr": "ΕΠΕ"}),
            clock_start=OBSERVED + timedelta(days=1),
        )
        self.assertEqual(result.changed_snapshots_created, 2)
        self.assertEqual(CompanySignal.objects.count(), before)
        self.assertEqual(CompanySignal.objects.count(), 0)

    def test_the_collector_never_touches_legacy_company_state(self):
        company = self.companies["100000000001"]
        before = Company.objects.filter(pk=company.pk).values(
            "is_active", "status", "legal_type", "prefecture", "municipality", "city", "name", "search_name",
            "raw_data", "activities", "updated_at", "last_synced_at",
        ).get()
        self.observe(full_item("100000000001", status={"id": 8, "descr": "Διαγραφή"}, isActive=False))
        after = Company.objects.filter(pk=company.pk).values(*before.keys()).get()
        self.assertEqual(before, after)
        self.assertEqual(CompanyActivity.objects.count(), 0)


class Scenario3SearchMissTests(ExecutionTestCase):
    def test_a_complete_search_that_omits_a_target_records_a_miss_and_leaves_it_due(self):
        row = CompanyMonitoring.objects.get(company=self.companies["100000000002"])
        before = (row.next_check_at, row.last_checked_at, row.last_success_at)
        client = FakeClient(searches=[page(full_item("100000000001"), total=1)])
        result = self.execute(client)
        self.assertEqual(result.search_misses, 1)
        self.assertEqual(result.status, PARTIAL)
        self.assertEqual(result.incomplete_groups, 0)
        self.assertEqual(CompanySnapshot.objects.filter(company=self.companies["100000000002"]).count(), 0)
        row.refresh_from_db()
        self.assertEqual((row.next_check_at, row.last_checked_at, row.last_success_at), before)
        self.assertLessEqual(row.next_check_at, RUN_AT)

    def test_a_miss_never_escalates_to_a_detail_request(self):
        client = FakeClient(searches=[page(full_item("100000000001"), total=1)])
        self.execute(client)
        self.assertEqual([call[0] for call in client.calls], ["search"])


class Scenario4IncompleteSearchTests(ExecutionTestCase):
    def test_the_page_cap_marks_the_group_incomplete_and_absence_is_not_source_truth(self):
        policy = RefreshPolicy(max_requests_per_run=10, max_pages_per_query=2, max_direct_details_per_run=3, page_size=1)
        client = FakeClient(searches=[
            page(full_item("100000000001"), total=9),
            page(full_item("777777777777"), total=9),
            page(full_item("100000000002"), total=9),
        ])
        result = self.execute(client, policy=policy)
        self.assertEqual(result.incomplete_groups, 1)
        self.assertEqual(result.search_misses, 0)  # never counted as absent from GEMI
        self.assertEqual(result.status, PARTIAL)
        self.assertEqual(result.search_requests, 2)
        self.assertEqual(CompanySnapshot.objects.count(), 1)
        row = CompanyMonitoring.objects.get(company=self.companies["100000000002"])
        self.assertIsNone(row.last_success_at)
        self.assertLessEqual(row.next_check_at, RUN_AT)

    def test_a_company_observed_inside_an_incomplete_group_still_succeeds(self):
        policy = RefreshPolicy(max_requests_per_run=10, max_pages_per_query=1, max_direct_details_per_run=3, page_size=1)
        client = FakeClient(searches=[page(full_item("100000000001"), total=9)])
        self.execute(client, policy=policy)
        row = CompanyMonitoring.objects.get(company=self.companies["100000000001"])
        self.assertEqual(row.last_success_at, OBSERVED)
        self.assertEqual(CompanySnapshot.objects.filter(company=self.companies["100000000001"]).count(), 1)


class Scenario5DuplicateCoverageTests(TestCase):
    def test_the_same_company_in_two_groups_is_processed_once(self):
        user = entitled_user()
        reference_data()
        reference_entry(GemiPrefecture, "6", "ΘΕΣΣΑΛΟΝΙΚΗΣ")
        first = radar_for(user, "a", codes=[CODE_A], prefectures=[PREFECTURE])
        second = radar_for(user, "b", codes=[CODE_B], prefectures=["ΘΕΣΣΑΛΟΝΙΚΗΣ"])
        companies = []
        for index in range(4):
            company = make_company(f"11000000000{index}")
            monitor(company, reasons=(ACTIVE_RADAR_MATCH,), priority="high", radar_ids=[first.pk, second.pk])
            companies.append(company)
        items = [full_item(company.gemi_number) for company in companies]
        client = FakeClient(searches=[page(*items, total=4), page(*items, total=4)])
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=RefreshPolicy(
            max_requests_per_run=10, max_pages_per_query=3, max_direct_details_per_run=3, page_size=4))
        self.assertEqual(len(plan.search_groups), 2)
        result = run_company_refresh(run_at=RUN_AT, policy=plan.policy, client=client, clock=client.clock, plan=plan)
        self.assertEqual(result.target_records_observed, 4)
        self.assertEqual(result.duplicate_observations_avoided, 4)
        self.assertEqual(CompanySnapshot.objects.count(), 4)
        self.assertEqual(result.baselines_created, 4)


class Scenario6DirectDetailTests(TestCase):
    def setUp(self):
        self.company = make_company("120000000001")
        monitor(self.company, reasons=(ACTIVE_OPPORTUNITY,), priority="critical")

    def test_a_high_value_company_is_refreshed_through_the_detail_endpoint(self):
        client = FakeClient(details={"/companies/120000000001": full_item("120000000001")})
        result = run_company_refresh(run_at=RUN_AT, policy=POLICY, client=client, clock=client.clock)
        self.assertEqual((result.detail_requests, result.search_requests), (1, 0))
        self.assertEqual(result.baselines_created, 1)
        path, params, lane, family = client.calls[0]
        self.assertEqual(path, "/companies/120000000001")
        self.assertEqual(lane, GemiLane.MONITORED_REFRESH)
        self.assertEqual(family, ResponseFamily.COMPANY_DETAIL)
        self.assertIsNone(params)

    def test_a_recent_signal_reason_also_qualifies(self):
        company = make_company("120000000002")
        monitor(company, reasons=(RECENT_SIGNAL,), priority="high")
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        self.assertEqual({t.gemi_number for t in plan.detail_targets}, {"120000000001", "120000000002"})

    def test_a_detail_that_returns_another_company_is_rejected(self):
        client = FakeClient(details={"/companies/120000000001": full_item("999999999999")})
        result = run_company_refresh(run_at=RUN_AT, policy=POLICY, client=client, clock=client.clock)
        self.assertEqual(result.company_failures, 1)
        self.assertEqual(CompanySnapshot.objects.count(), 0)
        self.assertEqual(result.status, PARTIAL)
        row = CompanyMonitoring.objects.get(company=self.company)
        self.assertIsNone(row.last_success_at)

    def test_the_detail_cap_bounds_the_requests(self):
        for index in range(4):
            company = make_company(f"12100000000{index}")
            monitor(company, reasons=(ACTIVE_OPPORTUNITY,), priority="critical")
        policy = RefreshPolicy(max_requests_per_run=10, max_pages_per_query=3, max_direct_details_per_run=2, page_size=2)
        client = FakeClient(details={})
        result = run_company_refresh(run_at=RUN_AT, policy=policy, client=client, clock=client.clock)
        self.assertEqual(result.detail_requests, 2)


class Scenario7DeferralTests(TestCase):
    def test_a_new_company_only_target_costs_nothing_and_is_neither_marked_nor_failed(self):
        company = make_company("130000000001")
        row = monitor(company, reasons=(NEW_COMPANY,))
        before = (row.next_check_at, row.last_checked_at, row.last_success_at, row.last_failure_at,
                  row.consecutive_failures)
        client = FakeClient()
        result = run_company_refresh(run_at=RUN_AT, policy=POLICY, client=client, clock=client.clock)
        self.assertEqual(result.deferred_no_strategy, 1)
        self.assertEqual(result.request_count, 0)
        self.assertEqual(client.calls, [])
        self.assertEqual(CompanySnapshot.objects.count(), 0)
        self.assertEqual(result.status, SUCCESS)  # deferral is a planning outcome, not a failure
        row.refresh_from_db()
        self.assertEqual(
            (row.next_check_at, row.last_checked_at, row.last_success_at, row.last_failure_at,
             row.consecutive_failures), before)

    def test_a_new_company_also_covered_by_a_radar_is_refreshed_by_the_search(self):
        user = entitled_user()
        reference_data()
        radar = radar_for(user, "a", codes=[CODE_A], prefectures=[PREFECTURE])
        company = make_company("130000000002")
        monitor(company, reasons=(NEW_COMPANY, ACTIVE_RADAR_MATCH), priority="high", radar_ids=[radar.pk])
        make_company("130000000003")
        monitor(Company.objects.get(gemi_number="130000000003"), reasons=(NEW_COMPANY,))
        plan = build_company_refresh_plan(run_at=RUN_AT, policy=POLICY)
        self.assertEqual(len(plan.search_groups), 0)  # one target cannot beat one detail request
        self.assertEqual(len(plan.deferred), 2)


class Scenario8RequestCapTests(ExecutionTestCase):
    def test_execution_stops_at_the_cap_and_the_rest_stays_due(self):
        policy = RefreshPolicy(max_requests_per_run=1, max_pages_per_query=3, max_direct_details_per_run=3, page_size=1)
        plan = self.plan(policy)
        client = FakeClient(searches=[page(full_item("100000000001"), total=9), page(full_item("100000000002"), total=9)])
        result = self.execute(client, policy=policy, plan=plan)
        self.assertEqual(result.request_count, 1)
        self.assertEqual(result.status, PARTIAL)
        self.assertEqual(result.group_stats.get("cap"), CAP_REQUESTS)
        row = CompanyMonitoring.objects.get(company=self.companies["100000000002"])
        self.assertIsNone(row.last_success_at)
        self.assertLessEqual(row.next_check_at, RUN_AT)


class Scenario9SystemicFailureTests(ExecutionTestCase):
    def test_a_contract_break_stops_the_collector_and_fails_the_run(self):
        error = GemiResponseValidationError(
            "schema", family=ResponseFamily.COMPANY_SEARCH, schema_version=1, kind="type",
            location="searchResults[0].arGemi", request_id="",
        ) if _validation_error_takes_kwargs() else GemiResponseValidationError("schema")
        client = FakeClient(searches=[error, page(full_item("100000000001"), total=1)])
        result = self.execute(client)
        self.assertEqual(result.status, FAILED)
        self.assertEqual(result.search_requests, 1)
        self.assertEqual(CompanySnapshot.objects.count(), 0)
        self.assertIn("GemiResponseValidationError", result.error_message)
        for row in CompanyMonitoring.objects.all():
            self.assertIsNone(row.last_success_at)
            self.assertLessEqual(row.next_check_at, RUN_AT)

    def test_authentication_budget_and_retry_failures_all_stop_the_run(self):
        for error in (GemiAuthenticationError("auth"), GemiBudgetTimeoutError("budget"),
                      GemiRetryExhaustedError("network")):
            with self.subTest(error=type(error).__name__):
                CompanySnapshot.objects.all().delete()
                GemiRefreshRun.objects.all().delete()
                client = FakeClient(searches=[error])
                result = self.execute(client)
                self.assertEqual(result.status, FAILED)
                self.assertEqual(CompanySnapshot.objects.count(), 0)
                self.assertEqual(GemiRefreshRun.objects.get().status, FAILED)

    def test_a_failure_after_successful_work_is_partial_and_keeps_what_succeeded(self):
        policy = RefreshPolicy(max_requests_per_run=10, max_pages_per_query=3, max_direct_details_per_run=3, page_size=1)
        client = FakeClient(searches=[page(full_item("100000000001"), total=9), GemiRetryExhaustedError("network")])
        result = self.execute(client, policy=policy)
        self.assertEqual(result.status, PARTIAL)
        self.assertEqual(result.baselines_created, 1)
        self.assertEqual(CompanySnapshot.objects.count(), 1)
        self.assertIsNone(CompanyMonitoring.objects.get(company=self.companies["100000000002"]).last_success_at)


def _validation_error_takes_kwargs():
    import inspect

    return "family" in inspect.signature(GemiResponseValidationError.__init__).parameters


class Scenario10CompanyFailureTests(ExecutionTestCase):
    def test_one_bad_company_does_not_destroy_a_valid_page(self):
        broken = full_item("100000000001")
        broken["incorporationDate"] = "2026-09-14"
        with patch("gemiapp.ingestion.refresh.normalize_company", side_effect=_fail_for("100000000001")):
            client = FakeClient(searches=[page(broken, full_item("100000000002"), total=2)])
            result = self.execute(client)
        self.assertEqual(result.company_failures, 1)
        self.assertEqual(result.target_records_observed, 1)
        self.assertEqual(result.status, PARTIAL)
        self.assertEqual(CompanySnapshot.objects.filter(company=self.companies["100000000002"]).count(), 1)
        self.assertIsNone(CompanyMonitoring.objects.get(company=self.companies["100000000001"]).last_success_at)

    def test_an_observation_older_than_the_snapshot_span_is_a_company_failure(self):
        company = self.companies["100000000001"]
        record_company_snapshot(
            company, normalize_company(full_item("100000000001"), as_of=date(2026, 9, 20)),
            datetime(2026, 9, 20, 12, 0, tzinfo=dt_timezone.utc),
        )
        client = FakeClient(searches=[page(full_item("100000000001"), full_item("100000000002"), total=2)])
        result = self.execute(client)
        self.assertEqual(result.company_failures, 1)
        self.assertEqual(CompanySnapshot.objects.filter(company=company).count(), 1)
        self.assertIsNone(CompanyMonitoring.objects.get(company=company).last_success_at)
        self.assertEqual(result.baselines_created, 1)  # the other company still succeeded

    def test_no_payload_is_ever_logged(self):
        with patch("gemiapp.ingestion.refresh.normalize_company", side_effect=_fail_for("100000000001")):
            client = FakeClient(searches=[page(full_item("100000000001"), total=1)])
            with self.assertLogs("gemiapp.ingestion.refresh", level="WARNING") as logs:
                self.execute(client)
        blob = "\n".join(logs.output)
        for sentinel in (PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, "ΔΟΚΙΜΑΣΤΙΚΗ", "ΟΔΟΣ ΔΟΚΙΜΗΣ"):
            self.assertNotIn(sentinel, blob)


def _fail_for(gemi_number):
    def side_effect(record, **kwargs):
        if str(record.get("arGemi")) == gemi_number:
            raise ValueError("normalisation failed")
        return normalize_company(record, **kwargs)

    return side_effect


class LifecycleTests(ExecutionTestCase):
    def test_first_seen_at_is_set_once_and_never_moved_later(self):
        company = self.companies["100000000001"]
        self.assertIsNone(company.first_seen_at)
        client = FakeClient(searches=[page(full_item("100000000001"), total=1)])
        self.execute(client)
        company.refresh_from_db()
        self.assertEqual(company.first_seen_at, OBSERVED)

        later = OBSERVED + timedelta(days=2)
        self.make_due_again()
        client = FakeClient(searches=[page(
            full_item("100000000001", status={"id": 8, "descr": "Διαγραφή"}), total=1)], clock_start=later)
        self.execute(client)
        company.refresh_from_db()
        self.assertEqual(company.first_seen_at, OBSERVED)
        self.assertEqual(company.last_seen_at, later)

    def test_an_earlier_proven_observation_may_move_first_seen_at_back(self):
        company = self.companies["100000000001"]
        Company.objects.filter(pk=company.pk).update(first_seen_at=OBSERVED + timedelta(days=5))
        client = FakeClient(searches=[page(full_item("100000000001"), total=1)])
        self.execute(client)
        company.refresh_from_db()
        self.assertEqual(company.first_seen_at, OBSERVED)

    def test_last_synced_at_and_updated_at_are_untouched(self):
        company = self.companies["100000000001"]
        before = Company.objects.filter(pk=company.pk).values("updated_at", "last_synced_at").get()
        client = FakeClient(searches=[page(full_item("100000000001"), total=1)])
        self.execute(client)
        after = Company.objects.filter(pk=company.pk).values("updated_at", "last_synced_at").get()
        self.assertIsNone(after["last_synced_at"])
        self.assertEqual(before, after)


class MonitoringUpdateTests(ExecutionTestCase):
    def test_the_next_check_comes_from_the_a9_helper_not_a_local_constant(self):
        company = self.companies["100000000001"]
        client = FakeClient(searches=[page(full_item("100000000001"), total=1)])
        self.execute(client)
        row = CompanyMonitoring.objects.get(company=company)
        self.assertEqual(row.last_checked_at, OBSERVED)
        self.assertEqual(row.next_check_at, compute_next_check_at(
            MONITORING_POLICY, priority=row.priority, company_id=company.pk,
            monitored_since=row.monitored_since, last_checked_at=OBSERVED,
        ))

    def test_a_previous_failure_streak_is_reset_by_a_success(self):
        company = self.companies["100000000001"]
        CompanyMonitoring.objects.filter(company=company).update(
            consecutive_failures=3, last_failure_at=RUN_AT - timedelta(days=1))
        client = FakeClient(searches=[page(full_item("100000000001"), total=1)])
        self.execute(client)
        row = CompanyMonitoring.objects.get(company=company)
        self.assertEqual(row.consecutive_failures, 0)
        self.assertEqual(row.last_success_at, OBSERVED)

    def test_monitoring_reasons_and_radar_matches_are_never_written(self):
        before_reasons = set(CompanyMonitoringReason.objects.values_list("id", "reason", "active"))
        client = FakeClient(searches=[page(full_item("100000000001"), full_item("100000000002"), total=2)])
        self.execute(client)
        self.assertEqual(set(CompanyMonitoringReason.objects.values_list("id", "reason", "active")), before_reasons)
        self.assertEqual(RadarMatch.objects.count(), 0)
        self.assertEqual(UserCompanyLead.objects.count(), 0)
        self.assertEqual(CustomerRadar.objects.get(pk=self.radar.pk).prefectures, [PREFECTURE])


class RunModelTests(ExecutionTestCase):
    def test_the_run_records_aggregate_counts_only(self):
        client = FakeClient(searches=[page(full_item("100000000001"), full_item("100000000002"), total=2)])
        result = self.execute(client)
        run = GemiRefreshRun.objects.get()
        self.assertEqual(run.pk, result.run_id)
        self.assertEqual(run.status, SUCCESS)
        self.assertEqual(run.run_at, RUN_AT)
        self.assertEqual((run.due_companies, run.planned_companies), (2, 2))
        self.assertEqual((run.request_count, run.search_requests, run.detail_requests), (1, 1, 0))
        self.assertEqual((run.baselines_created, run.changed_snapshots_created, run.unchanged_snapshots), (2, 0, 0))
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.error_message, "")

    def test_group_stats_hold_counts_and_opaque_keys_only(self):
        client = FakeClient(searches=[page(full_item("100000000001"), full_item("100000000002"), total=2)])
        self.execute(client)
        stats = GemiRefreshRun.objects.get().group_stats
        self.assertEqual(set(stats), {"groups"})
        for group in stats["groups"]:
            self.assertEqual(set(group), {"key", "pages", "targets", "found", "complete"})
        blob = json.dumps(stats, ensure_ascii=False)
        for sentinel in (PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, "ΔΟΚΙΜΑΣΤΙΚΗ"):
            self.assertNotIn(sentinel, blob)

    def test_the_run_model_has_no_field_that_could_hold_a_payload_or_a_person(self):
        names = {field.name for field in GemiRefreshRun._meta.fields}
        for forbidden in ("payload", "raw_data", "company", "name", "email", "phone", "afm", "persons", "user"):
            self.assertNotIn(forbidden, names)
        self.assertEqual([f.name for f in GemiRefreshRun._meta.fields if f.is_relation], [])

    def test_the_error_message_is_capped_and_carries_no_payload(self):
        client = FakeClient(searches=[GemiRetryExhaustedError("x" * 900)])
        result = self.execute(client)
        self.assertLessEqual(len(result.error_message), 300)
        self.assertLessEqual(len(GemiRefreshRun.objects.get().error_message), 300)

    def test_the_migration_only_creates_the_refresh_run_table(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        operations = loader.disk_migrations[("gemiapp", "0042_gemi_refresh_run")].operations
        self.assertEqual([type(op) for op in operations], [CreateModel])
        self.assertEqual(operations[0].name, "GemiRefreshRun")


class PlanOnlyCommandTests(ExecutionTestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("run_gemi_company_refresh", *args, stdout=out)
        return out.getvalue()

    def test_plan_only_makes_no_request_and_writes_nothing(self):
        with patch("gemiapp.ingestion.refresh.get_gemi_client") as client:
            output = self.run_command("--plan-only", "--run-at", RUN_AT.isoformat())
        client.assert_not_called()
        self.assertEqual(GemiRefreshRun.objects.count(), 0)
        self.assertEqual(CompanySnapshot.objects.count(), 0)
        self.assertEqual(CompanySignal.objects.count(), 0)
        self.assertIsNone(CompanyMonitoring.objects.first().last_checked_at)
        self.assertIn("due=2", output)
        self.assertIn("plan-only", output)

    def test_plan_only_leaves_every_monitoring_row_byte_identical(self):
        before = list(CompanyMonitoring.objects.values())
        self.run_command("--plan-only", "--run-at", RUN_AT.isoformat())
        self.assertEqual(list(CompanyMonitoring.objects.values()), before)

    def test_overrides_are_validated_and_may_only_shrink_a_run(self):
        for argument in ("--max-requests", "--max-pages", "--max-direct-details"):
            with self.assertRaises(CommandError):
                self.run_command("--plan-only", argument, "0")
        with self.assertRaises(CommandError):
            self.run_command("--plan-only", "--run-at", "2026-09-16T09:00:00")
        with self.assertRaises(CommandError):
            self.run_command("--plan-only", "--run-at", "not-a-date")
        output = self.run_command("--plan-only", "--max-requests", "99", "--run-at", RUN_AT.isoformat())
        self.assertIn("cap=20", output)  # the setting still bounds the override

    def test_the_command_exposes_no_query_cursor_or_signal_switch(self):
        from gemiapp.management.commands.run_gemi_company_refresh import Command

        parser = Command().create_parser("manage.py", "run_gemi_company_refresh")
        options = {action.dest for action in parser._actions}
        self.assertEqual(
            options - {"help", "version", "verbosity", "settings", "pythonpath", "traceback", "no_color",
                       "force_color", "skip_checks"},
            {"plan_only", "run_at", "max_requests", "max_pages", "max_direct_details"},
        )


class TaskTests(TestCase):
    def test_the_task_is_callable_and_deliberately_unscheduled(self):
        from gemiapp import tasks
        from gemiapp.apps import SCHEDULES

        self.assertTrue(callable(tasks.run_gemi_company_refresh_task))
        for entry in SCHEDULES:
            self.assertNotIn("refresh", entry["func"])
        self.assertNotIn(
            "gemiapp.tasks.run_gemi_company_refresh_task", [entry["func"] for entry in SCHEDULES],
        )

    def test_importing_the_collector_performs_no_request(self):
        with patch("gemiapp.ingestion.refresh.get_gemi_client") as client:
            import importlib

            importlib.reload(importlib.import_module("gemiapp.ingestion.refresh"))
        client.assert_not_called()


class PrivacyTests(ExecutionTestCase):
    def test_no_person_or_contact_data_survives_a_refresh(self):
        client = FakeClient(searches=[page(full_item("100000000001"), full_item("100000000002"), total=2)])
        self.execute(client)
        blob = json.dumps(list(GemiRefreshRun.objects.values()), ensure_ascii=False, default=str)
        blob += json.dumps(list(CompanySnapshot.objects.values()), ensure_ascii=False, default=str)
        blob += json.dumps(list(CompanyMonitoring.objects.values()), ensure_ascii=False, default=str)
        for sentinel in (PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, "ΔΟΚΙΜΑΣΤΙΚΗ", "ΟΔΟΣ ΔΟΚΙΜΗΣ",
                         "099999999", "ΕΜΠΟΡΙΟ"):
            self.assertNotIn(sentinel, blob)

    def test_the_command_output_carries_no_person_or_contact_data(self):
        out = StringIO()
        client = FakeClient(searches=[page(full_item("100000000001"), total=1)])
        self.execute(client)
        call_command("run_gemi_company_refresh", "--plan-only", "--run-at", RUN_AT.isoformat(), stdout=out)
        for sentinel in (PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, "ΔΟΚΙΜΑΣΤΙΚΗ"):
            self.assertNotIn(sentinel, out.getvalue())

    def test_a_successful_refresh_logs_no_person_or_contact_data(self):
        client = FakeClient(searches=[page(full_item("100000000001"), total=1)])
        with self.assertLogs("gemiapp", level="INFO") as logs:
            self.execute(client)
        blob = "\n".join(logs.output)
        for sentinel in (PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, "ΔΟΚΙΜΑΣΤΙΚΗ"):
            self.assertNotIn(sentinel, blob)


class ProvenanceTests(ExecutionTestCase):
    def test_snapshots_cite_no_source_record_because_the_client_exposes_none(self):
        client = FakeClient(searches=[page(full_item("100000000001"), total=1)])
        self.execute(client)
        self.assertIsNone(CompanySnapshot.objects.get().source_record)

    def test_the_collector_never_guesses_a_source_record(self):
        source = open("gemiapp/ingestion/refresh.py", encoding="utf-8").read()
        for guess in ('GemiSourceRecord"', "GemiSourceRecord.objects", ".latest(", "order_by(\"-fetched_at\")"):
            self.assertNotIn(guess, source)
        self.assertIn("source_record=None", source)


class CustomerParityTests(TestCase):
    """The presence of B4 changes nothing a customer can see."""

    def setUp(self):
        self.user = entitled_user("parity@example.com")
        self.radar = radar_for(self.user, "parity", codes=[CODE_A], prefectures=[PREFECTURE])
        reference_data()

    def snapshot_of_world(self):
        return {
            "companies": list(Company.objects.values(
                "gemi_number", "name", "is_active", "status", "legal_type", "prefecture", "municipality",
                "city", "activities", "search_name", "last_synced_at")),
            "activities": list(CompanyActivity.objects.values("company_id", "code", "activity_type")),
            "matches": list(RadarMatch.objects.values("radar_id", "company_id")),
            "leads": list(UserCompanyLead.objects.values("user_id", "company_id", "status")),
            "subscriptions": list(UserSubscription.objects.values("user_id", "tier", "status")),
            "radars": list(CustomerRadar.objects.values("id", "prefectures", "legal_types", "only_active")),
        }

    def test_a_refresh_leaves_every_customer_facing_result_identical(self):
        with patch("gemiapp.services._get", return_value=page(full_item("140000000001", day="2026-09-14"))):
            run = import_for_date(date(2026, 9, 14))
        company = Company.objects.get(gemi_number="140000000001")
        monitor(company, reasons=(ACTIVE_OPPORTUNITY,), priority="critical")
        match_imported_companies(run)
        before = self.snapshot_of_world()
        before_matches = company_matches_radar(company, self.radar)

        client = FakeClient(details={"/companies/140000000001": full_item("140000000001")})
        result = run_company_refresh(run_at=RUN_AT, policy=POLICY, client=client, clock=client.clock)
        self.assertEqual(result.baselines_created, 1)

        self.assertEqual(self.snapshot_of_world(), before)
        self.assertEqual(company_matches_radar(Company.objects.get(pk=company.pk), self.radar), before_matches)
        mail.outbox.clear()
        send_digests(date(2026, 9, 14))
        self.assertEqual(CompanySignal.objects.count(), 0)
