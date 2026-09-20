"""Tests for Discovery v2 newness by run-start frontier (A10 revision).

Newness used to be decided by whether a ``Company`` row existed at the moment a page was scanned, so the same
GEMI record was recorded as ``known`` or as newly discovered depending on whether the legacy importer had
already run -- and a ``known`` record is not eligible B2 evidence, so the sighting was lost for good. Newness
is now a fact about Discovery's own frontier: above it a record is newly discovered whatever ``Company``
holds, at or below it the local-existence rule is unchanged. ``company_existed`` keeps recording the local
fact and becomes the diagnostic for exactly the sightings the old rule dropped.

Fixture GEMI pages only -- no live call (NoNetworkMixin guards urlopen).
"""

from datetime import datetime, time, timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from .company_signals import NEW_COMPANY, SHADOW
from .ingestion.discovery import (
    INGEST,
    KNOWN,
    LATE_PUBLICATION,
    NEW_INCORPORATION,
    ORDERING_VIOLATION,
    STOP_OVERLAP_SATISFIED,
    DiscoveryPolicy,
    compare_with_legacy,
    get_cursor,
    run_discovery,
)
from .models import (
    Company, CompanyActivity, CompanySignal, CompanySnapshot, CustomerRadar, DigestDelivery,
    GemiDiscoveryObservation, GemiDiscoveryRun, Opportunity, OrganizationNotification, OpportunityTask,
    RadarMatch, UserCompanyLead, UserSubscription,
)
from .new_company_signals import materialize_new_company_signals
from .organization_access import (
    OrganizationAccessDenied, get_authorized_company_opportunity_page, get_authorized_workspace_dashboard,
    get_authorized_workspace_opportunities,
)
from .organization_radars import RadarDefinition, create_organization_radar
from .test_gemi_client import NoNetworkMixin
from .test_gemi_discovery import AS_OF, item, known_company, paged_client, ready_cursor
from .test_organization_icp import Refs
from .test_organization_radars import organization

# Four records per page: one scan can hold both sides of the frontier and both sides of local existence.
POLICY = DiscoveryPolicy(page_size=4, max_pages=3, overlap_known_records=2, min_overlap_pages=1)
OLD = "2025-11-02"


def rows():
    return {row.gemi_number: (row.classification, row.company_existed) for row in GemiDiscoveryObservation.objects.all()}


def imported(number, day=None):
    """A company the legacy importer stored, keeping the record it received -- what B2's baseline needs."""
    record = item(number, day)
    company = Company.objects.create(
        gemi_number=str(number), name=record["coNameEl"], incorporation_date=day or AS_OF,
        prefecture="ΑΤΤΙΚΗΣ", legal_type="ΙΚΕ", raw_data=record,
    )
    company.refresh_from_db()
    return company


class ClassificationTests(NoNetworkMixin, TestCase):
    """The four cases of (above / at-or-below the frontier) x (stored locally / not)."""

    def setUp(self):
        # Above the frontier: 1003 already stored by the legacy importer, 1002 not stored.
        # At or below it: 1000, 998, 997 stored; 999 never stored, published late.
        for number in (1003, 1000, 998, 997):
            known_company(number)
        ready_cursor(1000)
        self.client_, self.transport = paged_client([
            [item(1003), item(1002), item(1001), item(1000)],
            [item(999, OLD), item(998), item(997)],
        ])
        self.result = run_discovery(client=self.client_, policy=POLICY, as_of=AS_OF)

    def test_above_the_frontier_without_a_local_company_is_discovered_as_before(self):
        self.assertEqual(rows()["1002"], (NEW_INCORPORATION, False))
        self.assertEqual(rows()["1001"], (NEW_INCORPORATION, False))

    def test_above_the_frontier_a_stored_company_is_still_newly_discovered(self):
        # The defect: the legacy importer had already stored 1003, which used to make it `known` and lost the
        # sighting. It is now classified on its own identifier, and the local fact is kept as evidence.
        classification, company_existed = rows()["1003"]
        self.assertEqual((classification, company_existed), (NEW_INCORPORATION, True))
        self.assertNotEqual(classification, KNOWN)
        self.assertEqual(self.result.rediscovered_local_records, 1)
        self.assertTrue(Company.objects.filter(gemi_number="1003").exists())

    def test_at_or_below_the_frontier_a_stored_company_stays_known(self):
        for number in ("1000", "998", "997"):
            self.assertEqual(rows()[number], (KNOWN, True), number)

    def test_at_or_below_the_frontier_an_unstored_company_is_still_discovered(self):
        self.assertEqual(rows()["999"], (LATE_PUBLICATION, False))
        self.assertEqual(self.result.late_publication_records, 1)

    def test_the_counters_and_the_request_count_stay_truthful(self):
        result = self.result
        self.assertEqual((result.status, result.stop_reason, result.pages_fetched), ("success", STOP_OVERLAP_SATISFIED, 2))
        # examined = known + new still holds; the rediscovered ones are a subset of new, never double counted.
        self.assertEqual((result.records_examined, result.known_records, result.new_records), (7, 3, 4))
        self.assertEqual(len(self.transport.calls), result.pages_fetched)   # no request added by the new rule
        self.assertIn("known=3 new=4 (already local=1)", "\n".join(result.lines()))

    def test_the_frontier_advances_and_the_overlap_is_measured_on_local_rows_only(self):
        # 1003 is above the frontier, so it is not overlap evidence even though it is stored locally.
        self.assertEqual(self.result.overlap_known_records, 3)
        self.assertEqual((get_cursor().high_water_mark, self.result.cursor_advanced), ("1003", True))

    def test_the_discovery_tables_gained_no_column(self):   # a classification change needs no migration
        self.assertEqual(
            {field.name for field in GemiDiscoveryObservation._meta.concrete_fields},
            {"id", "run", "gemi_number", "classification", "incorporation_date", "incorporation_date_quality",
             "company_existed", "page_index", "created_at"},
        )


class OrderingWobbleTests(NoNetworkMixin, TestCase):
    def test_a_record_above_the_frontier_after_one_below_it_is_judged_on_its_own_identifier(self):
        for number in (1003, 1002, 1001):
            known_company(number)
        ready_cursor(1001)
        # 1002 arrives after 1001, which is at the frontier: a sticky "past the frontier now" flag would make
        # it known. Its own identifier is above the frontier, so it is newly discovered.
        client, _ = paged_client([[item(1003), item(1001), item(1002)]])

        result = run_discovery(client=client, policy=POLICY, as_of=AS_OF)

        self.assertEqual(rows()["1002"], (NEW_INCORPORATION, True))
        self.assertEqual(rows()["1003"], (NEW_INCORPORATION, True))
        self.assertEqual(rows()["1001"], (KNOWN, True))
        # The disorder itself is still measured and still freezes the cursor.
        self.assertEqual(result.status, "anomaly")
        self.assertEqual([entry["kind"] for entry in result.blocking_anomalies], [ORDERING_VIOLATION])
        self.assertEqual((get_cursor().high_water_mark, result.cursor_advanced), ("1001", False))


class IngestSafetyTests(NoNetworkMixin, TestCase):
    @override_settings(GEMI_DISCOVERY_V2_ENABLED=True)
    def test_a_rediscovered_company_is_never_written_over(self):
        stored = known_company(1003)
        Company.objects.filter(pk=stored.pk).update(name="ΟΠΩΣ ΤΗΝ ΕΓΡΑΨΕ Ο IMPORTER", raw_data={"kept": True})
        for number in (1000, 999):
            known_company(number)
        ready_cursor(1000)
        before = list(Company.objects.filter(gemi_number="1003").values())
        client, _ = paged_client([[item(1003), item(1002), item(1001), item(1000)], [item(999)]])

        result = run_discovery(mode=INGEST, client=client, policy=POLICY, as_of=AS_OF)

        # 1003 is newly discovered, so its observation stands, but the stored row is left exactly as it was.
        self.assertEqual(rows()["1003"], (NEW_INCORPORATION, True))
        self.assertEqual(list(Company.objects.filter(gemi_number="1003").values()), before)
        self.assertFalse(CompanyActivity.objects.filter(company__gemi_number="1003").exists())
        # The genuinely absent ones are still ingested.
        self.assertEqual((result.ingested_records, Company.objects.count()), (2, 5))
        self.assertTrue(CompanyActivity.objects.filter(company__gemi_number="1002").exists())


class ComparisonTests(NoNetworkMixin, TestCase):
    def test_a_rediscovered_company_is_both_and_late_publications_are_unchanged(self):
        older = AS_OF - timedelta(days=5)
        for number in (1000, 999):
            known_company(number, older)
        rediscovered = known_company(1003, AS_OF)          # the legacy importer stored it for this very day
        ready_cursor(1000)
        client, _ = paged_client([[item(1003), item(1002, OLD), item(1001), item(1000)], [item(999)]])
        run_discovery(client=client, policy=POLICY, as_of=AS_OF)
        # The comparison selects runs by their start date; pin it to AS_OF instead of the wall clock.
        GemiDiscoveryRun.objects.update(started_at=timezone.make_aware(datetime.combine(AS_OF, time(12, 0))))

        report = compare_with_legacy(AS_OF)

        # Discovered above the frontier *and* stored by the legacy importer: BOTH, never V2_ONLY.
        self.assertEqual((report.legacy, report.both, report.legacy_only), (1, 1, []))
        self.assertNotIn(rediscovered.gemi_number, report.v2_only)
        self.assertEqual(report.v2_only, ["1001", "1002"])
        self.assertEqual(report.v2_only_reasons, {"late_publication": 1, "legacy_filter_miss": 1})


class PipelineTestCase(NoNetworkMixin, TestCase):
    """A real scan feeding the real B2 producer, so the ce0cd4f baseline rule is exercised end to end."""

    def setUp(self):
        self.r = Refs()
        self.org = organization()
        self.owner = self.org.members.get(role="owner").user
        self.radar = create_organization_radar(self.org, RadarDefinition(
            name="ΚΑΔ Αττικής", active=True, kads=(self.r.kad_other,), regions=(self.r.attica,),
            legal_forms=(self.r.ike,), signal_types=("new_company",)))
        for number in (118717202000, 118717201000):
            known_company(number)
        ready_cursor(118717202000)

    def scan(self, *records):
        client, _ = paged_client([list(records) + [item(118717202000), item(118717201000)]])
        result = run_discovery(client=client, policy=POLICY, as_of=AS_OF)
        self.assertEqual(result.status, "success")
        return GemiDiscoveryRun.objects.get(pk=result.run_id).started_at

    def materialize(self):
        with self.captureOnCommitCallbacks(execute=True):
            return materialize_new_company_signals()

    def assertDiscoveryDrivenOpportunity(self, company, run_started_at):
        signal = CompanySignal.objects.get(company=company)
        snapshot = CompanySnapshot.objects.get(company=company)
        evidence = signal.discovery_evidence.discovery_observation
        self.assertEqual((signal.signal_type, signal.mode), (NEW_COMPANY, SHADOW))
        self.assertEqual(signal.detected_at, max(run_started_at, snapshot.observed_at))   # ce0cd4f, unchanged
        self.assertEqual(snapshot.observed_at, company.updated_at)
        self.assertTrue(snapshot.is_baseline)
        self.assertEqual(evidence.run.started_at, run_started_at)         # the discovery time is preserved
        self.assertNotEqual(evidence.classification, KNOWN)
        opportunity = Opportunity.objects.get(company=company, radar=self.radar)
        self.assertEqual(opportunity.latest_signal.mode, SHADOW)
        return opportunity


class ImportOrderEquivalenceTests(PipelineTestCase):
    def test_import_first_and_discovery_first_reach_the_same_result(self):
        import_first = imported(118717203000)                  # stored before the scan: the race case
        started_at = self.scan(item(118717204000), item(118717203000))
        self.assertEqual(rows()["118717203000"][1], True)      # company_existed, yet newly discovered
        self.assertEqual(rows()["118717204000"][1], False)

        first_report = self.materialize()
        self.assertEqual((first_report.signals_created, first_report.unmaterialised_no_company), (1, 1))

        discovery_first = imported(118717204000)               # stored only now, after the scan
        second_report = self.materialize()
        self.assertEqual((second_report.signals_created, second_report.signals_existing), (1, 1))

        one = self.assertDiscoveryDrivenOpportunity(import_first, started_at)
        two = self.assertDiscoveryDrivenOpportunity(discovery_first, started_at)
        self.assertEqual((one.score, two.score, one.score), (two.score, one.score, 100))
        self.assertEqual(first_report.baselines_created + second_report.baselines_created, 2)
        # Same rule, applied to each company's own evidence: whichever of the two moments came second decides.
        self.assertEqual(CompanySignal.objects.get(company=import_first).detected_at, started_at)
        self.assertEqual(CompanySignal.objects.get(company=discovery_first).detected_at, discovery_first.updated_at)
        self.assertGreater(discovery_first.updated_at, started_at)
        self.assertLess(import_first.updated_at, started_at)


class EndToEndTests(PipelineTestCase):
    def legacy_world(self):
        return tuple(list(model.objects.order_by("pk").values()) for model in (
            CustomerRadar, UserCompanyLead, RadarMatch, DigestDelivery, UserSubscription))

    def test_a_stored_company_reaches_a_hidden_shadow_opportunity(self):
        company = imported(118717203000)
        before = self.legacy_world()

        started_at = self.scan(item(118717203000))
        observation = GemiDiscoveryObservation.objects.get(gemi_number="118717203000")
        self.assertEqual((observation.classification, observation.company_existed), (NEW_INCORPORATION, True))

        report = self.materialize()

        self.assertEqual((report.signals_created, report.baselines_created, report.state_unavailable), (1, 1, 0))
        self.assertDiscoveryDrivenOpportunity(company, started_at)
        # Invisible to every customer surface, and nothing was sent or assigned.
        self.assertEqual(get_authorized_workspace_opportunities(self.owner, self.org.pk, view="all").rows, ())
        dashboard = get_authorized_workspace_dashboard(self.owner, self.org.pk)
        self.assertEqual((dashboard.active_opportunities, dashboard.unread_notifications), (0, 0))
        with self.assertRaises(OrganizationAccessDenied):
            get_authorized_company_opportunity_page(self.owner, self.org.pk, company.pk)
        self.assertEqual((OrganizationNotification.objects.count(), OpportunityTask.objects.count()), (0, 0))
        self.assertFalse(CompanySignal.objects.exclude(mode=SHADOW).exists())
        self.assertEqual(self.legacy_world(), before)

    def test_a_late_publication_stays_pending_and_writes_nothing(self):
        self.scan(item(118717204000, OLD))
        report = self.materialize()
        self.assertEqual((report.signals_created, report.unmaterialised_no_company, report.late_publications), (0, 1, 1))
        self.assertEqual((CompanySignal.objects.count(), CompanySnapshot.objects.count()), (0, 0))
        self.assertFalse(Company.objects.filter(gemi_number="118717204000").exists())
        self.assertEqual(
            GemiDiscoveryObservation.objects.get(gemi_number="118717204000").classification, LATE_PUBLICATION)
