"""Tests for the deterministic G4 SHADOW cycle (``gemiapp.g4_shadow_cycle``).

``TransactionTestCase``, deliberately: the cycle's central claim is that the signals it creates are processed
exactly once by the existing ``transaction.on_commit`` hook and never a second time by the orchestrator. That is
only observable when commits really happen.

Fixture GEMI pages only -- no live call (NoNetworkMixin guards urlopen).
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from . import apps as gemi_apps
from .company_signals import DISCOVERY as DISCOVERY_SOURCE, LIVE, SHADOW, record_company_signal
from .g4_shadow_cycle import (
    COMPARISON,
    DISCOVERY,
    MATERIALISATION,
    PIPELINE,
    PRECHECK,
    REPLAY,
    ShadowCycleRefused,
    run_g4_shadow_cycle,
)
from .ingestion.discovery import KNOWN, LATE_PUBLICATION, NEW_INCORPORATION, compare_with_legacy, get_cursor
from .models import (
    Company, CompanyActivity, CompanySignal, CompanySnapshot, CustomerRadar, DigestDelivery,
    GemiDiscoveryObservation, GemiDiscoveryRun, Opportunity, OpportunityTask, OrganizationNotification,
    RadarMatch, UserCompanyLead, UserSubscription,
)
from . import opportunity_pipeline as g2
from .opportunity_pipeline import FAILED
from .organization_access import (
    OrganizationAccessDenied, get_authorized_company_opportunity_page, get_authorized_workspace_dashboard,
    get_authorized_workspace_opportunities,
)
from .organization_radars import RadarDefinition, create_organization_radar
from .test_discovery_frontier import imported
from .test_gemi_client import NoNetworkMixin
from .test_gemi_discovery import item, known_company, paged_client, ready_cursor
from .test_organization_icp import Refs
from .test_organization_radars import organization

FRONTIER = 118717202000
BELOW = 118717201000
NEW = 118717203000          # published after the frontier and stored by the legacy importer
LATE = 118717204000         # published after the frontier, never stored: a late publication
OLD = "2025-11-02"


class CycleTestCase(NoNetworkMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.r = Refs()
        self.org = organization()
        self.owner = self.org.members.get(role="owner").user
        self.radar = create_organization_radar(self.org, RadarDefinition(
            name="ΚΑΔ Αττικής", active=True, kads=(self.r.kad_other,), regions=(self.r.attica,),
            legal_forms=(self.r.ike,), signal_types=("new_company",)))
        for number in (FRONTIER, BELOW):
            known_company(number, self.today)
        ready_cursor(FRONTIER)

    def page(self, *records):
        return list(records) + [item(FRONTIER, self.today), item(BELOW, self.today)]

    def cycle(self, *records, **options):
        """One whole cycle against a fixture page. Returns (report, transport)."""
        client, transport = paged_client([self.page(*records)])
        with patch("gemiapp.ingestion.discovery.get_gemi_client", return_value=client):
            return run_g4_shadow_cycle(**options), transport

    def command(self, *records, argv=(), **options):
        client, transport = paged_client([self.page(*records)])
        out = StringIO()
        with patch("gemiapp.ingestion.discovery.get_gemi_client", return_value=client):
            call_command("run_g4_shadow_cycle", *argv, stdout=out, stderr=StringIO(), **options)
        return out.getvalue(), transport

    def new_company(self):
        return imported(NEW, self.today)

    def world(self):
        return tuple(list(model.objects.order_by("pk").values()) for model in (
            CompanySignal, CompanySnapshot, Opportunity))

    def legacy_world(self):
        return tuple(list(model.objects.order_by("pk").values()) for model in (
            Company, CompanyActivity, CustomerRadar, UserCompanyLead, RadarMatch, DigestDelivery, UserSubscription))


class PhaseOrderTests(CycleTestCase):
    def test_the_phases_run_in_the_documented_order(self):
        self.new_company()
        report, _ = self.cycle(item(NEW, self.today), replay_hours=24, compare_date=self.today)
        self.assertEqual([phase.name for phase in report.phases],
                          [PRECHECK, DISCOVERY, MATERIALISATION, PIPELINE, REPLAY, COMPARISON])
        self.assertEqual([phase.status for phase in report.phases], ["ok"] * 6)
        self.assertEqual(report.failed_phases, [])

    def test_replay_and_comparison_are_skipped_unless_asked_for(self):
        report, _ = self.cycle()
        statuses = {phase.name: phase.status for phase in report.phases}
        self.assertEqual((statuses[REPLAY], statuses[COMPARISON]), ("skipped", "skipped"))
        self.assertEqual(report.replay_runs, [])
        self.assertIsNone(report.comparison)

    def test_discovery_stays_shadow_and_ingest_stays_off(self):
        report, _ = self.cycle(item(NEW, self.today))
        self.assertEqual(GemiDiscoveryRun.objects.get().mode, SHADOW)
        self.assertEqual(report.discovery.mode, SHADOW)
        self.assertFalse(Company.objects.filter(gemi_number=str(NEW)).exists())   # nothing ingested
        self.assertNotIn("run_g4_shadow_cycle", [entry["func"] for entry in gemi_apps.SCHEDULES])
        self.assertNotIn("gemiapp.tasks.run_gemi_discovery_v2_shadow_task",
                         [entry["func"] for entry in gemi_apps.SCHEDULES])


class SingleProcessingTests(CycleTestCase):
    def test_a_new_signal_is_processed_exactly_once_by_the_after_commit_hook(self):
        company = self.new_company()
        with patch.object(g2, "_process_company_signal", wraps=g2._process_company_signal) as spy:
            report, _ = self.cycle(item(NEW, self.today))
        signal = CompanySignal.objects.get(company=company)
        self.assertEqual(spy.call_count, 1)                                   # never a second pass
        self.assertEqual([run.signal_id for run in report.pipeline_runs], [signal.pk])
        self.assertTrue(report.pipeline_runs[0].processed)
        self.assertEqual(Opportunity.objects.filter(company=company, radar=self.radar).count(), 1)
        self.assertEqual(report.materialisation.signals_created, 1)

    def test_a_replay_in_the_same_cycle_never_touches_what_phase_d_just_did(self):
        company = self.new_company()
        report, _ = self.cycle(item(NEW, self.today), replay_hours=24)
        signal = CompanySignal.objects.get(company=company)
        self.assertEqual([run.signal_id for run in report.pipeline_runs], [signal.pk])
        self.assertEqual(report.replay_runs, [])          # the signal it just processed is excluded
        self.assertEqual(Opportunity.objects.count(), 1)

    def test_a_replay_in_a_later_cycle_leaves_the_capture_frozen(self):
        self.new_company()
        self.cycle(item(NEW, self.today))
        captured = self.world()

        report, _ = self.cycle(item(NEW, self.today), replay_hours=24)

        self.assertEqual([run.signal_id for run in report.replay_runs],
                          [CompanySignal.objects.get().pk])
        self.assertEqual((report.replay_runs[0].unchanged, report.replay_runs[0].created), (1, 0))
        self.assertEqual(self.world(), captured)


class IdempotencyTests(CycleTestCase):
    def test_running_the_whole_cycle_again_changes_nothing_that_matters(self):
        self.new_company()
        first, _ = self.cycle(item(NEW, self.today))
        captured = self.world()
        observations = GemiDiscoveryObservation.objects.count()

        second, _ = self.cycle(item(NEW, self.today))

        self.assertEqual(self.world(), captured)   # no second signal, snapshot or opportunity
        self.assertEqual((second.materialisation.signals_created, second.materialisation.signals_existing), (0, 1))
        self.assertEqual(second.materialisation.baselines_created, 0)
        self.assertEqual(second.pipeline_runs, [])          # nothing new to process
        # A rerun does record a fresh run and its observations: that is the discovery log, not duplicated work.
        self.assertEqual(GemiDiscoveryRun.objects.count(), 2)
        self.assertGreater(GemiDiscoveryObservation.objects.count(), observations)
        self.assertEqual(first.discovery.resulting_high_water_mark, second.discovery.resulting_high_water_mark)


class CoverageTests(CycleTestCase):
    def test_a_company_stored_before_the_scan_is_discovered_measured_and_materialised(self):
        self.new_company()
        report, _ = self.cycle(item(NEW, self.today))
        observation = GemiDiscoveryObservation.objects.get(gemi_number=str(NEW))
        self.assertEqual((observation.classification, observation.company_existed), (NEW_INCORPORATION, True))
        self.assertEqual(report.discovery.rediscovered_local_records, 1)
        coverage = report.coverage
        self.assertEqual((coverage.eligible_companies, coverage.eligible_with_company), (1, 1))
        self.assertEqual((coverage.signals_materialised, coverage.coverage_ratio), (1, 1.0))
        self.assertEqual(coverage.rediscovered_local_observations, 1)

    def test_a_late_publication_stays_pending_and_is_counted_as_the_g4_gap(self):
        report, _ = self.cycle(item(LATE, OLD))
        self.assertEqual(GemiDiscoveryObservation.objects.get(gemi_number=str(LATE)).classification, LATE_PUBLICATION)
        self.assertEqual(report.materialisation.unmaterialised_no_company, 1)
        self.assertFalse(Company.objects.filter(gemi_number=str(LATE)).exists())
        self.assertEqual((CompanySignal.objects.count(), CompanySnapshot.objects.count()), (0, 0))
        coverage = report.coverage
        self.assertEqual((coverage.eligible_companies, coverage.eligible_with_company), (1, 0))
        self.assertEqual((coverage.pending_no_company, coverage.coverage_ratio), (1, None))
        self.assertEqual(coverage.pending_by_classification, {LATE_PUBLICATION: 1})
        self.assertIsNotNone(coverage.oldest_pending_at)
        self.assertIn("pending (no Company row)=1", "\n".join(report.lines()))

    def test_the_ratio_counts_only_eligible_discoveries_whose_company_is_available(self):
        self.new_company()
        report, _ = self.cycle(item(LATE, OLD), item(NEW, self.today))
        coverage = report.coverage
        self.assertEqual((coverage.eligible_companies, coverage.eligible_with_company), (2, 1))
        self.assertEqual((coverage.signals_materialised, coverage.coverage_ratio), (1, 1.0))
        self.assertEqual((coverage.pending_no_company, coverage.pending_by_classification),
                          (1, {LATE_PUBLICATION: 1}))

    def test_a_known_record_is_neither_eligible_nor_pending(self):
        report, _ = self.cycle()
        self.assertEqual(set(GemiDiscoveryObservation.objects.values_list("classification", flat=True)), {KNOWN})
        self.assertEqual((report.coverage.eligible_companies, report.coverage.pending_no_company), (0, 0))


class ComparisonTests(CycleTestCase):
    def test_the_comparison_reuses_the_existing_implementation(self):
        self.new_company()
        report, _ = self.cycle(item(NEW, self.today), compare_date=self.today)
        expected = compare_with_legacy(self.today)
        self.assertEqual(
            (report.comparison.legacy, report.comparison.v2, report.comparison.both,
             report.comparison.legacy_only, report.comparison.v2_only, report.comparison.v2_only_reasons),
            (expected.legacy, expected.v2, expected.both, expected.legacy_only, expected.v2_only,
             expected.v2_only_reasons))
        self.assertIn("LEGACY COMPARISON", "\n".join(report.lines()))


class GemiBudgetTests(CycleTestCase):
    def test_only_discovery_talks_to_gemi(self):
        self.new_company()
        report, transport = self.cycle(item(NEW, self.today), replay_hours=24, compare_date=self.today)
        # Every request the cycle made went through the injected discovery client; anything else would have hit
        # the urlopen guard. One page, one request.
        self.assertEqual((len(transport.calls), report.gemi_requests), (1, 1))
        self.assertEqual(report.discovery.pages_fetched, 1)
        self.assertEqual(CompanySnapshot.objects.count(), 1)   # the baseline cost no request


class FailureTests(CycleTestCase):
    def test_a_pipeline_failure_keeps_the_signal_and_a_later_replay_recovers_it(self):
        company = self.new_company()
        with patch("gemiapp.opportunity_pipeline.matching.explain_organization_radar_matches",
                   side_effect=RuntimeError("radar matching exploded")):
            report, _ = self.cycle(item(NEW, self.today))

        signal = CompanySignal.objects.get(company=company)          # committed, never rolled back
        self.assertEqual(report.materialisation.signals_created, 1)
        self.assertEqual(report.pipeline_runs[0].skipped_reason, FAILED)
        self.assertEqual(report.failed_phases, [PIPELINE])
        self.assertEqual(Opportunity.objects.count(), 0)

        recovered, _ = self.cycle(item(NEW, self.today), replay_hours=24)

        self.assertEqual([run.signal_id for run in recovered.replay_runs], [signal.pk])
        self.assertEqual(recovered.failed_phases, [])
        self.assertEqual(Opportunity.objects.filter(company=company, radar=self.radar).count(), 1)

    def test_the_command_exits_non_zero_and_names_the_failed_phase(self):
        self.new_company()
        with patch("gemiapp.opportunity_pipeline.matching.explain_organization_radar_matches",
                   side_effect=RuntimeError("radar matching exploded")):
            with self.assertRaises(CommandError) as raised:
                self.command(item(NEW, self.today))
        self.assertIn(PIPELINE, str(raised.exception))
        self.assertTrue(CompanySignal.objects.exists())

    def test_a_failed_discovery_run_is_a_failed_phase_and_materialisation_still_runs(self):
        self.new_company()
        client, transport = paged_client([self.page(item(NEW, self.today))], failure_after=0)
        with patch("gemiapp.ingestion.discovery.get_gemi_client", return_value=client):
            report = run_g4_shadow_cycle()
        self.assertEqual(report.discovery.status, "failed")
        self.assertEqual(report.failed_phases, [DISCOVERY])
        self.assertEqual({phase.name: phase.status for phase in report.phases}[MATERIALISATION], "ok")


class PrecheckTests(CycleTestCase):
    def assertRefused(self, reason, *, signals=0):
        client, transport = paged_client([self.page(item(NEW, self.today))])
        with patch("gemiapp.ingestion.discovery.get_gemi_client", return_value=client):
            with self.assertRaises(ShadowCycleRefused) as raised:
                run_g4_shadow_cycle()
        self.assertIn(reason, str(raised.exception))
        self.assertEqual(transport.calls, [])                       # refused before any GEMI request
        self.assertEqual(GemiDiscoveryRun.objects.count(), 0)       # and before any write
        self.assertEqual(CompanySignal.objects.count(), signals)    # only what the fixture itself created
        self.assertEqual(Opportunity.objects.count(), 0)

    @override_settings(GEMI_DISCOVERY_V2_ENABLED=True)
    def test_the_cutover_flag_refuses_the_cycle(self):
        self.assertRefused("discovery ingest is off")

    @override_settings(GEMI_DISCOVERY_V2_SHADOW=False)
    def test_the_shadow_flag_being_off_refuses_the_cycle(self):
        self.assertRefused("discovery shadow flag is on")

    @override_settings(GEMI_COLLECTOR_ENABLED=False)
    def test_a_deployment_without_a_gemi_key_refuses_the_cycle(self):
        self.assertRefused("GEMI collector is configured")

    def test_a_live_signal_refuses_the_cycle(self):
        company = self.new_company()
        record_company_signal(company=company, signal_type="new_company", source_type=DISCOVERY_SOURCE,
                              event_key={"fixture": "live"}, detected_at=timezone.now(), mode=LIVE)
        self.assertRefused("no LIVE signal exists", signals=1)

    def test_an_uninitialised_cursor_refuses_the_cycle(self):
        cursor = get_cursor()
        cursor.high_water_mark, cursor.high_water_mark_value, cursor.status = "", None, "uninitialised"
        cursor.save()
        self.assertRefused("the discovery cursor is initialised")


class ShadowIsolationTests(CycleTestCase):
    def test_the_cycle_shows_nothing_to_a_customer_and_changes_no_legacy_row(self):
        company = self.new_company()
        before = self.legacy_world()

        report, _ = self.cycle(item(LATE, OLD), item(NEW, self.today), replay_hours=24)

        self.assertEqual(Opportunity.objects.filter(company=company, radar=self.radar).count(), 1)
        self.assertEqual(get_authorized_workspace_opportunities(self.owner, self.org.pk, view="all").rows, ())
        dashboard = get_authorized_workspace_dashboard(self.owner, self.org.pk)
        self.assertEqual((dashboard.active_opportunities, dashboard.unread_notifications), (0, 0))
        with self.assertRaises(OrganizationAccessDenied):
            get_authorized_company_opportunity_page(self.owner, self.org.pk, company.pk)
        self.assertEqual((OrganizationNotification.objects.count(), OpportunityTask.objects.count()), (0, 0))
        self.assertIsNone(Opportunity.objects.get().assigned_to_id)
        self.assertFalse(CompanySignal.objects.exclude(mode=SHADOW).exists())
        self.assertEqual(self.legacy_world(), before)
        self.assertIn("SHADOW only", "\n".join(report.lines()))


class DryRunTests(CycleTestCase):
    def test_a_dry_run_writes_nothing_and_says_what_it_could_not_see(self):
        self.new_company()
        report, transport = self.cycle(item(NEW, self.today), dry_run=True)
        self.assertEqual(report.discovery.status, "success")
        self.assertEqual(len(transport.calls), 1)                   # a dry run still asks GEMI
        self.assertEqual((GemiDiscoveryRun.objects.count(), GemiDiscoveryObservation.objects.count()), (0, 0))
        self.assertEqual((CompanySignal.objects.count(), CompanySnapshot.objects.count(), Opportunity.objects.count()),
                          (0, 0, 0))
        self.assertEqual(get_cursor().high_water_mark, str(FRONTIER))
        text = "\n".join(report.lines())
        self.assertIn("[dry-run] nothing was written.", text)
        self.assertIn("could only see evidence recorded by earlier runs", text)
