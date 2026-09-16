"""Tests for the NEW_COMPANY signal producer (B2, gemiapp.new_company_signals).

Discovery evidence is built as fixtures; no GEMI call and no importer run.
"""

import copy
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import models
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import CreateModel
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .company_signals import DISCOVERY, LIVE, NEW_COMPANY, SHADOW, build_dedupe_key, record_company_signal, rule_for
from .ingestion.discovery import INVALID_DATE, KNOWN, LATE_PUBLICATION, NEW_INCORPORATION, STREAM_COMPANIES
from .models import (
    Company, CompanyActivity, CompanyMonitoring, CompanySignal, CompanySignalDiscoveryEvidence, CustomerRadar,
    DigestDelivery, GemiDiscoveryObservation, GemiDiscoveryRun, ImportRun, RadarMatch, UserCompanyLead,
    UserSubscription,
)
from .new_company_signals import (
    CONFLICT,
    CREATED,
    ELIGIBLE_CLASSIFICATIONS,
    EVENT_KEY,
    EXISTING,
    NOT_ELIGIBLE,
    PENDING_NO_COMPANY,
    materialize_new_company_signals,
    produce_new_company_signal,
)
from .services import company_matches_radar, import_for_date, match_imported_companies, send_digests
from .test_gemi_company_activities import TARGET, entitled_user, radar_for
from .test_gemi_validation import CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL, full_item

FIRST_RUN_AT = datetime(2026, 9, 10, 8, 0, tzinfo=dt_timezone.utc)
LATER_RUN_AT = datetime(2026, 9, 14, 8, 0, tzinfo=dt_timezone.utc)


def discovery_run(started_at=FIRST_RUN_AT, *, mode="shadow", status="success"):
    run = GemiDiscoveryRun.objects.create(stream=STREAM_COMPANIES, mode=mode, status=status, finished_at=started_at)
    GemiDiscoveryRun.objects.filter(pk=run.pk).update(started_at=started_at)  # started_at is auto_now_add
    return GemiDiscoveryRun.objects.get(pk=run.pk)


def observation(run, gemi_number="1001", classification=NEW_INCORPORATION, incorporation_date=date(2026, 9, 9), quality="valid", page_index=0):
    return GemiDiscoveryObservation.objects.create(
        run=run, gemi_number=gemi_number, classification=classification, incorporation_date=incorporation_date,
        incorporation_date_quality=quality, company_existed=(classification == KNOWN), page_index=page_index,
    )


def make_company(gemi_number="1001", incorporated=date(2026, 9, 9)):
    return Company.objects.create(
        gemi_number=gemi_number, name="ΕΤΑΙΡΕΙΑ ΙΚΕ", incorporation_date=incorporated, prefecture="ΑΤΤΙΚΗΣ",
        legal_type="ΙΚΕ",
    )


def signal_for(gemi_number):
    return CompanySignal.objects.filter(company__gemi_number=gemi_number).first()


class SchemaAndRuleTests(TestCase):
    def test_the_rule_is_implemented_and_only_from_discovery(self):
        rule = rule_for(NEW_COMPANY)
        self.assertTrue(rule.implemented)
        self.assertEqual((rule.rule_version, rule.source_types), ("new_company:v1", frozenset({DISCOVERY})))

    def test_the_provenance_model_links_a_signal_to_its_evidence(self):
        self.assertEqual(
            {field.name for field in CompanySignalDiscoveryEvidence._meta.concrete_fields},
            {"id", "signal", "discovery_observation", "created_at"},
        )
        self.assertTrue(CompanySignalDiscoveryEvidence._meta.get_field("signal").one_to_one)
        self.assertIs(
            CompanySignalDiscoveryEvidence._meta.get_field("discovery_observation").remote_field.on_delete,
            models.PROTECT,
        )

    def test_discovery_evidence_cannot_be_deleted_under_a_signal(self):
        run = discovery_run()
        evidence = observation(run)
        make_company()
        produce_new_company_signal(evidence)
        with self.assertRaises(ProtectedError):
            evidence.delete()

    def test_the_migration_only_creates_the_evidence_table(self):
        migration = MigrationLoader(None, ignore_no_migrations=True).get_migration("gemiapp", "0040_company_signal_discovery_evidence")
        self.assertEqual(migration.dependencies, [("gemiapp", "0039_company_signal")])
        self.assertEqual([(type(op), op.name) for op in migration.operations], [(CreateModel, "CompanySignalDiscoveryEvidence")])


class EligibilityTests(TestCase):
    def setUp(self):
        self.run = discovery_run()

    def test_every_newly_discovered_class_produces_a_candidate(self):
        self.assertEqual(set(ELIGIBLE_CLASSIFICATIONS), {NEW_INCORPORATION, LATE_PUBLICATION, INVALID_DATE})
        cases = {
            "1001": (NEW_INCORPORATION, date(2026, 9, 9), "valid"),
            "1002": (LATE_PUBLICATION, date(2025, 12, 15), "valid"),
            "1003": (INVALID_DATE, None, "out_of_range"),
        }
        for gemi_number, (classification, incorporated, quality) in cases.items():
            make_company(gemi_number)
            result = produce_new_company_signal(observation(self.run, gemi_number, classification, incorporated, quality))
            self.assertEqual(result.status, CREATED, gemi_number)
        self.assertEqual(CompanySignal.objects.count(), 3)

    def test_a_known_observation_is_never_a_candidate(self):
        make_company("1004")
        result = produce_new_company_signal(observation(self.run, "1004", KNOWN))
        self.assertEqual(result.status, NOT_ELIGIBLE)
        self.assertEqual(CompanySignal.objects.count(), 0)


class EventIdentityTests(TestCase):
    def setUp(self):
        self.company = make_company()

    def test_the_event_key_is_fixed_and_documented(self):
        self.assertEqual(EVENT_KEY, {"event": "first_observed"})
        produce_new_company_signal(observation(discovery_run()))
        signal = signal_for("1001")
        self.assertEqual(
            signal.dedupe_key,
            build_dedupe_key(signal_type=NEW_COMPANY, gemi_number="1001", event_key={"event": "first_observed"}),
        )

    def test_one_event_per_company_across_runs_and_classifications(self):
        first = observation(discovery_run(), "1001", NEW_INCORPORATION)
        produce_new_company_signal(first)
        later_run = discovery_run(LATER_RUN_AT)
        produce_new_company_signal(observation(later_run, "1001", LATE_PUBLICATION, date(2025, 12, 15)))
        self.assertEqual(CompanySignal.objects.count(), 1)
        signal = signal_for("1001")
        self.assertEqual((signal.detected_at, signal.effective_date), (FIRST_RUN_AT, date(2026, 9, 9)))
        self.assertEqual(signal.discovery_evidence.discovery_observation_id, first.pk)


class DetectedTimeTests(TestCase):
    def test_the_earliest_eligible_observation_decides_and_a_rerun_keeps_it(self):
        make_company()
        late_run = discovery_run(LATER_RUN_AT)
        early_run = discovery_run(FIRST_RUN_AT)
        observation(late_run, "1001", NEW_INCORPORATION)
        early = observation(early_run, "1001", NEW_INCORPORATION)

        materialize_new_company_signals()
        signal = signal_for("1001")
        self.assertEqual(signal.detected_at, FIRST_RUN_AT)
        self.assertEqual(signal.discovery_evidence.discovery_observation_id, early.pk)

        materialize_new_company_signals()
        signal.refresh_from_db()
        self.assertEqual((signal.detected_at, CompanySignal.objects.count()), (FIRST_RUN_AT, 1))

    def test_detection_time_is_not_the_command_time_or_the_company_import_time(self):
        company = make_company()
        produce_new_company_signal(observation(discovery_run()))
        signal = signal_for("1001")
        self.assertEqual(signal.detected_at, FIRST_RUN_AT)
        self.assertLess(signal.detected_at, company.imported_at)
        self.assertLess(signal.detected_at, timezone.now() - timedelta(days=1))


class EffectiveDateTests(TestCase):
    def setUp(self):
        self.run = discovery_run()

    def effective(self, gemi_number, classification, incorporated, quality):
        make_company(gemi_number)
        produce_new_company_signal(observation(self.run, gemi_number, classification, incorporated, quality))
        signal = signal_for(gemi_number)
        return signal.effective_date, signal.effective_at, signal.effective_precision

    def test_a_valid_date_is_kept_as_a_date_for_new_and_late_publications(self):
        self.assertEqual(self.effective("1001", NEW_INCORPORATION, date(2026, 9, 9), "valid"), (date(2026, 9, 9), None, "date"))
        self.assertEqual(self.effective("1002", LATE_PUBLICATION, date(2025, 12, 15), "valid"), (date(2025, 12, 15), None, "date"))

    def test_an_unusable_date_leaves_no_source_time_and_is_never_replaced_by_detection(self):
        for gemi_number, quality in (("1003", "missing"), ("1004", "invalid"), ("1005", "out_of_range")):
            with self.subTest(quality=quality):
                self.assertEqual(self.effective(gemi_number, INVALID_DATE, None, quality), (None, None, "none"))
        self.assertFalse(CompanySignal.objects.filter(effective_at__isnull=False).exists())


class CompanyMaterialisationTests(TestCase):
    def test_a_missing_company_leaves_the_candidate_pending_until_it_arrives(self):
        run = discovery_run()
        evidence = observation(run, "1001", LATE_PUBLICATION, date(2025, 12, 15))

        first = materialize_new_company_signals()
        self.assertEqual((first.unmaterialised_no_company, first.signals_created), (1, 0))
        self.assertEqual((CompanySignal.objects.count(), Company.objects.count()), (0, 0))
        self.assertTrue(GemiDiscoveryObservation.objects.filter(pk=evidence.pk).exists())

        make_company("1001")  # the approved importer stores it later
        second = materialize_new_company_signals()

        self.assertEqual((second.signals_created, CompanySignal.objects.count()), (1, 1))
        signal = signal_for("1001")
        self.assertEqual((signal.detected_at, signal.mode), (FIRST_RUN_AT, SHADOW))
        self.assertEqual(signal.effective_date, date(2025, 12, 15))
        self.assertEqual(signal.discovery_evidence.discovery_observation_id, evidence.pk)

        third = materialize_new_company_signals()
        self.assertEqual((third.signals_created, third.signals_existing, CompanySignal.objects.count()), (0, 1, 1))


class ShadowSafetyTests(TestCase):
    def test_every_path_produces_shadow_only(self):
        make_company()
        produce_new_company_signal(observation(discovery_run()))
        materialize_new_company_signals()
        self.assertEqual(set(CompanySignal.objects.values_list("mode", flat=True)), {SHADOW})
        self.assertEqual(CompanySignal.objects.filter(mode=LIVE).count(), 0)

    def test_no_option_can_ask_for_live(self):
        import inspect

        from . import new_company_signals

        for function in (new_company_signals.produce_new_company_signal, new_company_signals.materialize_new_company_signals):
            self.assertNotIn("mode", inspect.signature(function).parameters, function.__name__)
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command("materialize_new_company_signals", "--live", stdout=out)

    def test_an_ingest_mode_run_still_produces_shadow(self):
        make_company()
        produce_new_company_signal(observation(discovery_run(mode="ingest")))
        self.assertEqual(signal_for("1001").mode, SHADOW)


class ProvenanceAndConflictTests(TestCase):
    def test_the_first_evidence_is_never_replaced(self):
        make_company()
        first = observation(discovery_run(), "1001")
        produce_new_company_signal(first)
        later = observation(discovery_run(LATER_RUN_AT), "1001")
        produce_new_company_signal(later)
        self.assertEqual(CompanySignalDiscoveryEvidence.objects.count(), 1)
        self.assertEqual(signal_for("1001").discovery_evidence.discovery_observation_id, first.pk)

    def test_a_conflicting_event_is_reported_and_never_overwritten(self):
        company = make_company()
        record_company_signal(
            company=company, signal_type=NEW_COMPANY, source_type=DISCOVERY, event_key=dict(EVENT_KEY),
            effective=date(2020, 1, 1), confidence=1, mode=SHADOW, detected_at=FIRST_RUN_AT,
        )
        result = produce_new_company_signal(observation(discovery_run(), "1001", NEW_INCORPORATION, date(2026, 9, 9)))
        self.assertEqual(result.status, CONFLICT)
        self.assertEqual(signal_for("1001").effective_date, date(2020, 1, 1))

    def test_a_batch_reports_conflicts_and_keeps_going(self):
        for number in ("1001", "1002"):
            make_company(number)
        record_company_signal(
            company=Company.objects.get(gemi_number="1001"), signal_type=NEW_COMPANY, source_type=DISCOVERY,
            event_key=dict(EVENT_KEY), effective=date(2020, 1, 1), confidence=1, mode=SHADOW, detected_at=FIRST_RUN_AT,
        )
        run = discovery_run()
        observation(run, "1001", NEW_INCORPORATION, date(2026, 9, 9))
        observation(run, "1002", NEW_INCORPORATION, date(2026, 9, 9), page_index=1)

        with self.assertLogs("gemiapp.new_company_signals", level="WARNING"):
            report = materialize_new_company_signals()

        self.assertEqual((report.conflicts, report.signals_created, report.conflicting_gemi_numbers), (1, 1, ["1001"]))
        self.assertEqual(signal_for("1001").effective_date, date(2020, 1, 1))


class BatchTests(TestCase):
    def setUp(self):
        self.run = discovery_run()
        for index in range(5):
            number = f"100{index}"
            make_company(number)
            observation(self.run, number, NEW_INCORPORATION, date(2026, 9, 9), page_index=index)

    def test_counts_batching_and_idempotency(self):
        preview = materialize_new_company_signals(dry_run=True, batch_size=2)
        self.assertEqual((preview.candidate_companies, preview.signals_created, preview.batches), (5, 5, 3))
        self.assertEqual(CompanySignal.objects.count(), 0)
        self.assertEqual(CompanySignalDiscoveryEvidence.objects.count(), 0)

        first = materialize_new_company_signals(batch_size=2)
        self.assertEqual((first.signals_created, first.observations_inspected), (5, 5))
        self.assertEqual(CompanySignal.objects.count(), 5)

        second = materialize_new_company_signals()
        self.assertEqual((second.signals_created, second.signals_existing), (0, 5))
        self.assertEqual(CompanySignal.objects.count(), 5)

        resumed = materialize_new_company_signals(start_gemi_number="1002")
        self.assertEqual(resumed.candidate_companies, 2)
        with self.assertRaises(ValueError):
            materialize_new_company_signals(batch_size=0)

    def test_the_command_reports_counts_only(self):
        out = StringIO()
        call_command("materialize_new_company_signals", "--dry-run", "--batch-size", "2", stdout=out)
        text = out.getvalue()
        self.assertIn("[dry-run] signals would create=5", text)
        self.assertIn("mode=shadow (always)", text)
        self.assertNotIn("ΕΤΑΙΡΕΙΑ", text)
        call_command("materialize_new_company_signals", stdout=StringIO())
        self.assertEqual(CompanySignal.objects.count(), 5)
        with self.assertRaises(CommandError):
            call_command("materialize_new_company_signals", "--batch-size", "0", stdout=StringIO())


class PrivacyTests(TestCase):
    def test_no_person_or_contact_data_reaches_a_signal_or_the_output(self):
        company = make_company()
        Company.objects.filter(pk=company.pk).update(raw_data=full_item("1001"), email=CONTACT_SENTINEL)
        out = StringIO()
        with self.assertLogs("gemiapp", level="INFO") as logs:
            materialize_new_company_signals()
            call_command("materialize_new_company_signals", "--dry-run", stdout=out)
        self.assertEqual(CompanySignal.objects.count(), 0)  # no discovery evidence in this test
        rendered = "\n".join([
            str(list(CompanySignal.objects.values())), str(list(CompanySignalDiscoveryEvidence.objects.values())),
            out.getvalue(), "\n".join(logs.output),
        ])
        for sentinel in (PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, "ΔΟΚΙΜΑΣΤΙΚΗ", "ΕΤΑΙΡΕΙΑ ΙΚΕ"):
            self.assertNotIn(sentinel, rendered)


class CustomerParityTests(TestCase):
    """Producing signals changes nothing customer-visible and nothing in A9 monitoring."""

    def setUp(self):
        self.user = entitled_user()
        self.client.login(username="member@example.com", password="StrongPass123")
        items = [full_item("118717203000", TARGET.isoformat()), full_item("118717204000", TARGET.isoformat())]
        with patch("gemiapp.services.fetch_companies", side_effect=lambda _: copy.deepcopy(items)):
            import_for_date(TARGET)
        radar_for(self.user, "Αττική", prefectures=["ΑΤΤΙΚΗΣ"])
        match_imported_companies(ImportRun.objects.create(target_date=TARGET, status="success"))
        run = discovery_run()
        for index, number in enumerate(("118717203000", "118717204000")):
            observation(run, number, NEW_INCORPORATION, TARGET, "valid", page_index=index)

    def snapshot(self):
        companies = list(Company.objects.prefetch_related("activity_records").order_by("pk"))
        radars = list(CustomerRadar.objects.prefetch_related("activity_codes").order_by("pk"))
        state = {
            "companies": list(Company.objects.order_by("pk").values()),
            "activities": list(CompanyActivity.objects.order_by("pk").values()),
            "matches": list(RadarMatch.objects.order_by("pk").values_list("radar_id", "company_id", "matched_on")),
            "leads": list(UserCompanyLead.objects.order_by("pk").values_list("user_id", "company_id", "status")),
            "subscriptions": list(UserSubscription.objects.order_by("pk").values()),
            "monitoring": list(CompanyMonitoring.objects.values()),
            "discovery": list(GemiDiscoveryObservation.objects.order_by("pk").values()),
            "predicate": [(radar.pk, company.pk, company_matches_radar(company, radar)) for radar in radars for company in companies],
        }
        with patch("django.core.signing.time.time", return_value=1789000000.0):
            mail.outbox = []
            DigestDelivery.objects.all().delete()
            state["digest"] = (send_digests(TARGET), [(message.subject, message.body) for message in mail.outbox])
            state["export"] = self.client.get(reverse("export_csv")).content
        return state

    def test_materialisation_touches_nothing_customer_facing(self):
        before = self.snapshot()
        self.assertTrue(before["matches"])
        report = materialize_new_company_signals()
        self.assertEqual(report.signals_created, 2)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(CompanyMonitoring.objects.count(), 0)
