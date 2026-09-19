"""Tests for NEW_COMPANY state at detection (B2 revision): a trustworthy importer record becomes the company's
canonical baseline before the signal is recorded, and ``detected_at = max(discovery time, baseline observed_at)``,
so the unchanged C5 matcher (``observed_at <= detected_at``) can evaluate KAD, region and legal-form Radars for a
genuinely new company. Untrustworthy records give no state and change nothing; no GEMI request is made.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from .company_signals import SHADOW
from .company_snapshots import COMPANY_SNAPSHOT_SCHEMA_VERSION
from .ingestion.normalizer import GEMI_NORMALIZER_VERSION
from .models import (
    Company, CompanyActivity, CompanySignal, CompanySnapshot, CustomerRadar, DigestDelivery, Opportunity,
    OrganizationNotification, OpportunityTask, RadarMatch, UserCompanyLead, UserSubscription,
)
from .new_company_signals import (
    CREATED, EXISTING, STATE_BASELINE_CREATED, STATE_BASELINE_REUSED, STATE_UNAVAILABLE,
    materialize_new_company_signals, produce_new_company_signal,
)
from .organization_access import (
    OrganizationAccessDenied, get_authorized_company_opportunity_page, get_authorized_workspace_dashboard,
    get_authorized_workspace_opportunities,
)
from .organization_radar_matching import SNAPSHOT_AT_DETECTION, explain_organization_radar_matches
from .organization_radars import RadarDefinition, create_organization_radar
from .test_gemi_validation import full_item
from .test_new_company_signals import FIRST_RUN_AT, discovery_run, observation
from .test_organization_icp import Refs
from .test_organization_radars import organization

GEMI = "118717203000"  # full_item's company: current KAD 47191002 (2026), prefecture 5, municipality 61190, type 19


class StateTestCase(TestCase):
    def setUp(self):
        self.r = Refs()
        self.org = organization()
        self.owner = self.org.members.get(role="owner").user
        self.company = Company.objects.create(gemi_number=GEMI, name="ΝΕΑ ΙΚΕ", incorporation_date=FIRST_RUN_AT.date(),
                                              raw_data=full_item(GEMI))
        self.company.refresh_from_db()
        self.observation = observation(discovery_run(FIRST_RUN_AT), GEMI)

    def radar(self, name, **criteria):
        return create_organization_radar(self.org, RadarDefinition(name=name, active=True, **criteria))

    def produce(self):
        with self.captureOnCommitCallbacks(execute=True):
            return produce_new_company_signal(self.observation)

    def signal(self):
        return CompanySignal.objects.get(company=self.company)


class BaselineTests(StateTestCase):
    def test_the_baseline_is_created_before_the_signal_and_valid_at_detection(self):
        result = self.produce()
        self.assertEqual((result.status, result.detection_state), (CREATED, STATE_BASELINE_CREATED))
        snapshot = CompanySnapshot.objects.get(company=self.company)
        signal = self.signal()
        self.assertTrue(snapshot.is_baseline)
        self.assertEqual(snapshot.observed_at, self.company.updated_at)          # the A6 observation time
        self.assertEqual(signal.detected_at, max(FIRST_RUN_AT, snapshot.observed_at))
        self.assertLessEqual(snapshot.observed_at, signal.detected_at)
        self.assertLessEqual(snapshot.created_at, signal.created_at)
        self.assertEqual(signal.mode, SHADOW)
        # the existing B3 writer and A3 semantics, nothing hand-made
        self.assertEqual((snapshot.schema_version, snapshot.normalizer_version),
                         (COMPANY_SNAPSHOT_SCHEMA_VERSION, GEMI_NORMALIZER_VERSION))
        self.assertEqual((snapshot.prefecture_source_id, snapshot.municipality_source_id,
                          snapshot.legal_type_source_id), ("5", "61190", "19"))
        self.assertEqual([(a["code"], a["kad_version"]) for a in snapshot.activities_state],
                         [("47191002", "kad_2026")])                                # the ended 2008 activity is out
        # the matcher finds it through its unchanged as-of lookup
        self.assertEqual(explain_organization_radar_matches(signal).context.context_status, SNAPSHOT_AT_DETECTION)

    def test_the_discovery_observation_time_is_preserved_and_distinct(self):
        self.produce()
        signal = self.signal()
        evidence = signal.discovery_evidence.discovery_observation
        self.assertEqual((evidence.pk, evidence.run.started_at), (self.observation.pk, FIRST_RUN_AT))
        self.assertGreater(signal.detected_at, FIRST_RUN_AT)   # the importer record arrived after discovery

    def test_replay_creates_no_duplicate_snapshot_signal_or_capture(self):
        self.radar("Όλες οι νέες", signal_types=("new_company",))
        self.produce()
        world = (list(CompanySnapshot.objects.values()), list(CompanySignal.objects.values()),
                 list(Opportunity.objects.values()))
        again = self.produce()
        report = materialize_new_company_signals()
        self.assertEqual((again.status, again.detection_state, report.signals_existing, report.baselines_created),
                         (EXISTING, "", 1, 0))
        self.assertEqual((list(CompanySnapshot.objects.values()), list(CompanySignal.objects.values()),
                          list(Opportunity.objects.values())), world)

    def test_an_existing_snapshot_is_reused_and_decides_detection_when_later(self):
        earlier = CompanySnapshot.objects.create(
            company=self.company, schema_version=1, normalizer_version=1, state_hash="e" * 64,
            observed_at=FIRST_RUN_AT - timedelta(days=1), last_observed_at=FIRST_RUN_AT - timedelta(days=1),
            is_baseline=True, last_status_change_quality="missing", incorporation_date_quality="valid",
            activities_state=[], unknown_current_activity_count=0)
        result = self.produce()
        self.assertEqual(result.detection_state, STATE_BASELINE_REUSED)
        self.assertEqual(CompanySnapshot.objects.filter(company=self.company).count(), 1)
        self.assertEqual(self.signal().detected_at, FIRST_RUN_AT)                  # state already existed then
        self.assertEqual(earlier.pk, CompanySnapshot.objects.get(company=self.company).pk)

    def test_a_conflicting_existing_signal_writes_no_snapshot(self):
        self.produce()
        CompanySnapshot.objects.all().delete()
        CompanySignal.objects.filter(company=self.company).update(effective_date=FIRST_RUN_AT.date() - timedelta(days=9))
        result = produce_new_company_signal(self.observation)
        self.assertEqual(result.status, "conflict")
        self.assertFalse(CompanySnapshot.objects.filter(company=self.company).exists())

    def test_the_report_counts_detection_state_and_the_dry_run_writes_nothing(self):
        dry = materialize_new_company_signals(dry_run=True)
        self.assertEqual((dry.signals_created, CompanySnapshot.objects.count()), (1, 0))
        report = materialize_new_company_signals()
        self.assertEqual((report.signals_created, report.baselines_created, report.state_unavailable), (1, 1, 0))
        self.assertIn("detection-time state: baselines created=1 reused=0 unavailable=0", "\n".join(report.lines()))


class CriteriaTests(StateTestCase):
    def test_a_genuinely_new_company_is_evaluated_on_every_criterion(self):
        r = self.r
        expected_match = {
            "kad": self.radar("ΚΑΔ", kads=(r.kad_other,)),
            "prefecture": self.radar("Νομός", regions=(r.attica,)),
            "municipality": self.radar("Δήμος", regions=(r.kifisia,)),
            "legal form": self.radar("Νομική μορφή", legal_forms=(r.ike,)),
            "combined": self.radar("Συνδυασμός", kads=(r.kad_other,), regions=(r.attica,), legal_forms=(r.ike,),
                                   signal_types=("new_company",)),
            "signal type": self.radar("Γεγονός", signal_types=("new_company",)),
        }
        expected_no_match = {
            "other kad": self.radar("Άλλος ΚΑΔ", kads=(r.kad_2026,)),
            "other prefecture": self.radar("Άλλος νομός", regions=(r.thessaloniki,)),
            "other legal form": self.radar("Άλλη μορφή", legal_forms=(r.oe,)),
            "excluded municipality": self.radar("Εξαίρεση", kads=(r.kad_other,), exclusions=(r.kifisia,)),
        }
        self.produce()
        with_opportunity = set(Opportunity.objects.filter(company=self.company).values_list("radar_id", flat=True))
        for label, radar in expected_match.items():
            self.assertIn(radar.pk, with_opportunity, label)
        for label, radar in expected_no_match.items():
            self.assertNotIn(radar.pk, with_opportunity, label)
        combined = Opportunity.objects.get(radar=expected_match["combined"])
        self.assertEqual(combined.score, 100)                                       # every dimension confirmed

    def test_the_shadow_opportunity_stays_invisible_and_sends_nothing(self):
        self.radar("ΚΑΔ", kads=(self.r.kad_other,))
        self.produce()
        row = Opportunity.objects.select_related("latest_signal").get(company=self.company)
        self.assertEqual((row.latest_signal.mode, row.assigned_to_id), (SHADOW, None))
        self.assertEqual(get_authorized_workspace_opportunities(self.owner, self.org.pk, view="all").rows, ())
        dashboard = get_authorized_workspace_dashboard(self.owner, self.org.pk)
        self.assertEqual((dashboard.active_opportunities, dashboard.unread_notifications), (0, 0))
        with self.assertRaises(OrganizationAccessDenied):
            get_authorized_company_opportunity_page(self.owner, self.org.pk, self.company.pk)
        self.assertEqual((OrganizationNotification.objects.count(), OpportunityTask.objects.count()), (0, 0))


class UntrustedRecordTests(StateTestCase):
    def assertNoState(self, reason):
        kad_radar = self.radar("ΚΑΔ", kads=(self.r.kad_other,))
        watch_all = self.radar("Γεγονός", signal_types=("new_company",))
        result = self.produce()
        self.assertEqual((result.detection_state, result.detail), (STATE_UNAVAILABLE, reason))
        self.assertFalse(CompanySnapshot.objects.filter(company=self.company).exists())
        self.assertEqual(self.signal().detected_at, FIRST_RUN_AT)                  # exactly the old behaviour
        radars = set(Opportunity.objects.filter(company=self.company).values_list("radar_id", flat=True))
        self.assertNotIn(kad_radar.pk, radars)                                     # insufficient, never guessed
        self.assertIn(watch_all.pk, radars)

    def set_raw(self, value):
        Company.objects.filter(pk=self.company.pk).update(raw_data=value)

    def test_missing_record(self):
        self.set_raw({})
        self.assertNoState("missing")

    def test_malformed_record(self):
        self.set_raw(["not", "a", "record"])
        self.assertNoState("malformed")

    def test_another_companys_record(self):
        self.set_raw(full_item("999999999999"))
        self.assertNoState("not_gemi_record")

    def test_a_record_failing_the_company_search_contract(self):
        self.set_raw(full_item(GEMI, activities="not a list"))
        self.assertNoState("invalid_record")

    def test_an_admin_edited_company(self):
        admin = User.objects.create_superuser("admin-ns", "admin-ns@example.com", "x")
        LogEntry.objects.create(user=admin, content_type=ContentType.objects.get_for_model(Company),
                                object_id=str(self.company.pk), object_repr="x", action_flag=CHANGE)
        self.assertNoState("admin_touched")


class SafetyTests(StateTestCase):
    def test_no_gemi_request_is_made(self):
        self.radar("ΚΑΔ", kads=(self.r.kad_other,))
        with patch("gemiapp.ingestion.client.GemiClient.get", side_effect=AssertionError("no GEMI request")) as get, \
                patch("gemiapp.ingestion.client.get_gemi_client", side_effect=AssertionError("no client")) as client:
            result = self.produce()
        self.assertEqual(result.detection_state, STATE_BASELINE_CREATED)
        get.assert_not_called()
        client.assert_not_called()
        source = open("gemiapp/new_company_signals.py", encoding="utf-8").read()
        for forbidden in ("get_gemi_client", "GemiClient", "search_companies", "requests", "urllib"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_legacy_and_company_rows_are_only_read(self):
        def world():
            return tuple(list(model.objects.order_by("pk").values()) for model in (
                Company, CompanyActivity, CustomerRadar, UserCompanyLead, RadarMatch, DigestDelivery, UserSubscription))

        before = world()
        self.radar("ΚΑΔ", kads=(self.r.kad_other,))
        self.produce()
        self.assertEqual(world(), before)
