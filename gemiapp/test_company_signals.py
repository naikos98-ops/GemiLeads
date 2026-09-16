"""Tests for the company signals foundation (B1, gemiapp.company_signals).

No detector exists yet: these tests call the service directly.
"""

import copy
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import User
from django.core import mail
from django.db import DataError, IntegrityError, models, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import AddConstraint, AddIndex, CreateModel
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import company_signals
from .company_signals import (
    DISCOVERY,
    DOCUMENT_ANALYSIS,
    FUTURE_SIGNAL_TYPES,
    KAD_ADDED,
    LIVE,
    MODES,
    NEW_COMPANY,
    PRECISION_DATE,
    PRECISION_DATETIME,
    PRECISION_NONE,
    SHADOW,
    SIGNAL_RULES,
    SIGNAL_TYPES,
    SNAPSHOT_DIFF,
    SOURCE_TYPES,
    STATUS_CHANGED,
    SignalConflictError,
    build_dedupe_key,
    promote_company_signal,
    record_company_signal,
    rule_for,
)
from .models import (
    Company, CompanyActivity, CompanyMonitoring, CompanySignal, CustomerRadar, DigestDelivery, GemiDiscoveryRun,
    ImportRun, RadarMatch, UserCompanyLead, UserSubscription,
)
from .services import company_matches_radar, import_for_date, match_imported_companies, send_digests
from .test_gemi_company_activities import TARGET, entitled_user, radar_for
from .test_gemi_validation import CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL, full_item

NOW = datetime(2026, 9, 16, 9, 0, tzinfo=dt_timezone.utc)
EVENT = {"observation": "first_seen", "incorporation_date": "2026-09-14"}


def make_company(gemi_number="118717203000", **fields):
    return Company.objects.create(
        gemi_number=gemi_number, name="ΕΤΑΙΡΕΙΑ ΙΚΕ", incorporation_date=date(2026, 9, 14),
        prefecture="ΑΤΤΙΚΗΣ", legal_type="ΙΚΕ", **fields,
    )


def record(company, **overrides):
    values = {
        "company": company, "signal_type": NEW_COMPANY, "source_type": DISCOVERY, "event_key": dict(EVENT),
        "confidence": 1, "mode": SHADOW, "detected_at": NOW,
    }
    values.update(overrides)
    return record_company_signal(**values)


class SchemaTests(TestCase):
    def test_fields_hold_no_person_or_payload_data(self):
        self.assertEqual(
            {field.name for field in CompanySignal._meta.concrete_fields},
            {"id", "company", "signal_type", "source_type", "rule_version", "dedupe_key", "confidence", "mode",
             "effective_date", "effective_at", "effective_precision", "detected_at", "created_at", "updated_at"},
        )
        for field in CompanySignal._meta.concrete_fields:
            self.assertNotIsInstance(field, models.JSONField, field.name)

    def test_a_signal_belongs_to_a_company_and_to_no_customer(self):
        # The signal's own relations: only the company. Producers attach their provenance through their own
        # tables (a reverse relation), which is why reverse relations are excluded here.
        forward = {field.related_model for field in CompanySignal._meta.concrete_fields if field.is_relation}
        self.assertEqual(forward, {Company})
        self.assertIs(CompanySignal._meta.get_field("company").remote_field.on_delete, models.PROTECT)
        everything = {field.related_model for field in CompanySignal._meta.get_fields() if field.is_relation}
        for tenant_model in (User, UserSubscription, CustomerRadar, UserCompanyLead):
            self.assertNotIn(tenant_model, everything)

    def test_choices_indexes_and_the_unique_event_identity(self):
        self.assertEqual({value for value, _ in CompanySignal._meta.get_field("signal_type").choices}, set(SIGNAL_TYPES))
        self.assertEqual({value for value, _ in CompanySignal._meta.get_field("source_type").choices}, set(SOURCE_TYPES))
        self.assertEqual({value for value, _ in CompanySignal._meta.get_field("mode").choices}, set(MODES))
        self.assertTrue(CompanySignal._meta.get_field("dedupe_key").unique)
        self.assertEqual(
            {index.name for index in CompanySignal._meta.indexes},
            {"signal_company_detected_idx", "signal_type_detected_idx", "signal_mode_detected_idx"},
        )

    def test_the_database_rejects_confidence_outside_zero_to_one(self):
        company = make_company()
        base = {
            "company": company, "signal_type": NEW_COMPANY, "source_type": DISCOVERY, "rule_version": "new_company:v1",
            "mode": SHADOW, "effective_precision": PRECISION_NONE, "detected_at": NOW,
        }
        for index, confidence in enumerate((Decimal("-0.0001"), Decimal("1.0001"))):
            with self.subTest(confidence=confidence), self.assertRaises(IntegrityError), transaction.atomic():
                CompanySignal.objects.create(dedupe_key=f"bad{index}", confidence=confidence, **base)
        CompanySignal.objects.create(dedupe_key="zero", confidence=Decimal("0"), **base)
        CompanySignal.objects.create(dedupe_key="one", confidence=Decimal("1"), **base)

    def test_the_database_rejects_an_invented_time_of_day(self):
        company = make_company()
        base = {
            "company": company, "signal_type": NEW_COMPANY, "source_type": DISCOVERY, "rule_version": "new_company:v1",
            "mode": SHADOW, "confidence": Decimal("1"), "detected_at": NOW,
        }
        inconsistent = (
            {"effective_precision": PRECISION_NONE, "effective_date": date(2026, 9, 14)},
            {"effective_precision": PRECISION_DATE, "effective_date": None},
            {"effective_precision": PRECISION_DATE, "effective_date": date(2026, 9, 14), "effective_at": NOW},
            {"effective_precision": PRECISION_DATETIME, "effective_at": None},
            {"effective_precision": PRECISION_DATETIME, "effective_at": NOW, "effective_date": date(2026, 9, 14)},
        )
        for index, values in enumerate(inconsistent):
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                CompanySignal.objects.create(dedupe_key=f"x{index}", **base, **values)

    def test_signal_history_is_protected_from_company_deletion(self):
        company = make_company()
        record(company)
        with self.assertRaises(ProtectedError), transaction.atomic():
            company.delete()

    def test_the_migration_only_creates_the_signal_table(self):
        migration = MigrationLoader(None, ignore_no_migrations=True).get_migration("gemiapp", "0039_company_signal")
        self.assertEqual(migration.dependencies, [("gemiapp", "0038_gemi_discovery")])
        self.assertTrue(all(isinstance(op, (CreateModel, AddIndex, AddConstraint)) for op in migration.operations))
        self.assertEqual({op.name for op in migration.operations if isinstance(op, CreateModel)}, {"CompanySignal"})


class TaxonomyAndRegistryTests(TestCase):
    def test_the_roadmap_types_exist(self):
        for signal_type in (NEW_COMPANY, STATUS_CHANGED, KAD_ADDED, "kad_removed", "legal_form_changed", "location_changed"):
            self.assertIn(signal_type, SIGNAL_TYPES)
        for later in ("capital_increase", "merger", "dissolution", "other_corporate_event"):
            self.assertIn(later, SIGNAL_TYPES)
        self.assertEqual(set(SOURCE_TYPES), {DISCOVERY, SNAPSHOT_DIFF, "document_metadata", DOCUMENT_ANALYSIS})

    def test_every_type_either_has_a_rule_or_is_explicitly_future(self):
        with_rules = {rule.signal_type for rule in SIGNAL_RULES}
        self.assertEqual(with_rules | FUTURE_SIGNAL_TYPES, set(SIGNAL_TYPES))
        self.assertFalse(with_rules & FUTURE_SIGNAL_TYPES)
        # B2 implements NEW_COMPANY; B5 implements the five Tier-1 snapshot-diff types. Everything else, the
        # later corporate events, stays taxonomy only.
        self.assertEqual(with_rules, {
            NEW_COMPANY, STATUS_CHANGED, KAD_ADDED, "kad_removed", "legal_form_changed", "location_changed",
        })
        rule = rule_for(NEW_COMPANY)
        self.assertEqual((rule.rule_version, rule.source_types, rule.implemented), ("new_company:v1", frozenset({DISCOVERY}), True))
        rule = rule_for(STATUS_CHANGED)
        self.assertEqual((rule.rule_version, rule.source_types, rule.implemented), ("status_changed:v1", frozenset({SNAPSHOT_DIFF}), True))
        self.assertIsNone(rule_for("capital_increase"))

    def test_the_registry_rejects_duplicates_and_malformed_versions(self):
        from .company_signals import SignalRule, _validate_registry

        for broken in (
            (SignalRule(NEW_COMPANY, "new_company:v1", frozenset({DISCOVERY})), SignalRule(KAD_ADDED, "new_company:v1", frozenset({DISCOVERY}))),
            (SignalRule(NEW_COMPANY, "v1", frozenset({DISCOVERY})),),
            (SignalRule(NEW_COMPANY, "new_company:v1", frozenset({"telepathy"})),),
            (SignalRule("not_a_type", "x:v1", frozenset({DISCOVERY})),),
        ):
            with self.subTest(broken=broken), patch.object(company_signals, "SIGNAL_RULES", broken), \
                 patch.object(company_signals, "_RULES_BY_TYPE", {rule.signal_type: rule for rule in broken}), \
                 self.assertRaises(ValueError):
                _validate_registry()

    def test_a_type_without_a_rule_cannot_be_recorded(self):
        company = make_company()
        with self.assertRaises(ValueError):
            record(company, signal_type="capital_increase")  # a later corporate event: taxonomy only
        with self.assertRaises(ValueError):
            record(company, source_type=SNAPSHOT_DIFF)  # not an allowed source for this rule
        for bad in ({"signal_type": "invented"}, {"source_type": "invented"}, {"mode": "maybe"}):
            with self.assertRaises(ValueError):
                record(company, **bad)
        self.assertEqual(CompanySignal.objects.count(), 0)


class EventIdentityTests(TestCase):
    def setUp(self):
        self.company = make_company()

    def test_the_same_event_is_recorded_once(self):
        first, created = record(self.company)
        again, created_again = record(self.company)
        self.assertEqual((created, created_again), (True, False))
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(CompanySignal.objects.count(), 1)

    def test_mode_and_rule_version_are_not_part_of_the_identity(self):
        first, _ = record(self.company, mode=SHADOW)
        as_live, created = record(self.company, mode=LIVE)
        self.assertEqual((created, as_live.pk, as_live.mode), (False, first.pk, SHADOW))  # no automatic promotion

        newer, created = record(self.company, rule_version="new_company:v2")
        self.assertEqual((created, newer.pk, newer.rule_version), (False, first.pk, "new_company:v1"))
        self.assertEqual(CompanySignal.objects.count(), 1)

    def test_a_different_event_is_a_different_signal(self):
        record(self.company)
        other_company = make_company("118717204000")
        record(other_company)
        record(self.company, event_key={"observation": "first_seen", "incorporation_date": "2025-12-15"})
        self.assertEqual(CompanySignal.objects.count(), 3)

    def test_the_key_is_built_from_documented_material_only(self):
        key = build_dedupe_key(signal_type=NEW_COMPANY, gemi_number="118717203000", event_key=dict(EVENT))
        self.assertEqual(key, build_dedupe_key(signal_type=NEW_COMPANY, gemi_number="118717203000", event_key={"incorporation_date": "2026-09-14", "observation": "first_seen"}))
        self.assertEqual(len(key), 64)
        for different in (
            {"signal_type": KAD_ADDED, "gemi_number": "118717203000", "event_key": dict(EVENT)},
            {"signal_type": NEW_COMPANY, "gemi_number": "118717204000", "event_key": dict(EVENT)},
            {"signal_type": NEW_COMPANY, "gemi_number": "118717203000", "event_key": {"observation": "first_seen"}},
        ):
            self.assertNotEqual(build_dedupe_key(**different), key)
        for invalid in ({"event_key": {}}, {"gemi_number": ""}, {"signal_type": "invented"}):
            with self.assertRaises(ValueError):
                build_dedupe_key(**{"signal_type": NEW_COMPANY, "gemi_number": "1", "event_key": dict(EVENT), **invalid})
        with self.assertRaises(TypeError):
            build_dedupe_key(signal_type=NEW_COMPANY, gemi_number="1", event_key={"company": object()})

    def test_conflicting_immutable_facts_fail_visibly(self):
        first, _ = record(self.company, effective=date(2026, 9, 14))
        with self.assertRaises(SignalConflictError):
            record(self.company, effective=date(2026, 9, 15))
        first.refresh_from_db()
        self.assertEqual((first.effective_date, CompanySignal.objects.count()), (date(2026, 9, 14), 1))

    def test_a_concurrent_writer_does_not_duplicate_the_event(self):
        first, _ = record(self.company)
        real_filter = CompanySignal.objects.filter

        def pretend_missing(*args, **kwargs):
            queryset = real_filter(*args, **kwargs)
            return queryset.none() if "dedupe_key" in kwargs else queryset

        with patch.object(CompanySignal.objects, "filter", side_effect=pretend_missing):
            with self.assertRaises(IntegrityError), transaction.atomic():
                record(self.company)  # the unique key is what actually protects the event
        self.assertEqual(CompanySignal.objects.count(), 1)


class DetectedTimeTests(TestCase):
    def test_the_first_detection_time_is_never_rewritten(self):
        company = make_company()
        first, _ = record(company, detected_at=NOW)
        later, created = record(company, detected_at=NOW + timedelta(days=3))
        self.assertEqual((created, later.detected_at), (False, NOW))
        later.refresh_from_db()
        self.assertEqual(later.detected_at, NOW)

    def test_detected_at_defaults_to_now_and_must_be_aware(self):
        company = make_company()
        signal, _ = record(company, detected_at=None)
        self.assertIsNotNone(signal.detected_at.tzinfo)
        with self.assertRaises(ValueError):
            record(make_company("118717204000"), detected_at=datetime(2026, 9, 16, 9, 0))


class ConfidenceTests(TestCase):
    def test_bounds_and_precision(self):
        signal, _ = record(make_company("118717203000"), confidence=0)
        self.assertEqual(signal.confidence, Decimal("0.0000"))
        exact, _ = record(make_company("118717204000"), confidence="0.9999")
        self.assertEqual(exact.confidence, Decimal("0.9999"))
        one, _ = record(make_company("118717205000"), confidence=1)
        self.assertEqual(one.confidence, Decimal("1.0000"))
        rejected = make_company("118717206000")
        for invalid in (-0.1, 1.1, "abc", None):
            with self.subTest(confidence=invalid), self.assertRaises(ValueError):
                record(rejected, confidence=invalid)
        self.assertEqual(CompanySignal.objects.count(), 3)


class EffectiveTimeTests(TestCase):
    def test_source_precision_is_preserved_and_never_invented(self):
        without, _ = record(make_company("1"))
        self.assertEqual((without.effective_precision, without.effective_date, without.effective_at), (PRECISION_NONE, None, None))

        dated, _ = record(make_company("2"), effective=date(2026, 9, 14))
        self.assertEqual((dated.effective_precision, dated.effective_date, dated.effective_at), (PRECISION_DATE, date(2026, 9, 14), None))

        stamped, _ = record(make_company("3"), effective=NOW)
        self.assertEqual((stamped.effective_precision, stamped.effective_at, stamped.effective_date), (PRECISION_DATETIME, NOW, None))

    def test_a_naive_or_nonsense_effective_time_is_refused(self):
        with self.assertRaises(ValueError):
            record(make_company("4"), effective=datetime(2026, 9, 14, 0, 0))
        with self.assertRaises(TypeError):
            record(make_company("5"), effective="2026-09-14")


class ModeTests(TestCase):
    def test_shadow_and_live_creation_and_explicit_promotion(self):
        company = make_company()
        shadow, _ = record(company)
        self.assertEqual(shadow.mode, SHADOW)

        promoted = promote_company_signal(shadow)
        self.assertEqual((promoted.pk, promoted.mode, CompanySignal.objects.count()), (shadow.pk, LIVE, 1))
        self.assertEqual(promote_company_signal(promoted).mode, LIVE)  # idempotent

        again, created = record(company)
        self.assertEqual((created, again.mode), (False, LIVE))  # promotion survives re-detection

        live_from_the_start, created = record(make_company("118717204000"), mode=LIVE)
        self.assertEqual((created, live_from_the_start.mode), (True, LIVE))

    def test_nothing_promotes_by_itself(self):
        company = make_company()
        record(company)
        for _ in range(3):
            record(company, mode=LIVE)
        self.assertEqual(CompanySignal.objects.get().mode, SHADOW)


class AdminTests(TestCase):
    def test_the_admin_is_read_only_and_shows_no_sensitive_columns(self):
        model_admin = admin.site._registry[CompanySignal]
        request = None
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertEqual(set(model_admin.readonly_fields), {field.name for field in CompanySignal._meta.concrete_fields} - {"id"})
        self.assertNotIn("raw_data", str(model_admin.list_display))


class PrivacyTests(TestCase):
    def test_no_person_or_contact_data_can_reach_a_signal(self):
        company = make_company()
        Company.objects.filter(pk=company.pk).update(raw_data=full_item(company.gemi_number), email=CONTACT_SENTINEL)
        with self.assertLogs("gemiapp.company_signals", level="INFO") as logs:
            signal, _ = record(company)
        rendered = "\n".join([str(list(CompanySignal.objects.values())), repr(signal), str(signal), "\n".join(logs.output)])
        for sentinel in (PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, "ΔΟΚΙΜΑΣΤΙΚΗ", "ΕΤΑΙΡΕΙΑ ΙΚΕ"):
            self.assertNotIn(sentinel, rendered)
        self.assertNotIn("099999999", rendered)  # the VAT number of the fixture


class CustomerParityTests(TestCase):
    """Recording signals changes nothing customer-visible: no producer, no workflow, no notification."""

    def setUp(self):
        self.user = entitled_user()
        self.client.login(username="member@example.com", password="StrongPass123")
        items = [full_item("118717203000", TARGET.isoformat()), full_item("118717204000", TARGET.isoformat())]
        with patch("gemiapp.services.fetch_companies", side_effect=lambda _: copy.deepcopy(items)):
            import_for_date(TARGET)
        radar_for(self.user, "Αττική", prefectures=["ΑΤΤΙΚΗΣ"])
        match_imported_companies(ImportRun.objects.create(target_date=TARGET, status="success"))

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
            "discovery_runs": GemiDiscoveryRun.objects.count(),
            "predicate": [(radar.pk, company.pk, company_matches_radar(company, radar)) for radar in radars for company in companies],
        }
        with patch("django.core.signing.time.time", return_value=1789000000.0):
            mail.outbox = []
            DigestDelivery.objects.all().delete()
            state["digest"] = (send_digests(TARGET), [(message.subject, message.body) for message in mail.outbox])
            state["export"] = self.client.get(reverse("export_csv")).content
        return state

    def test_signals_do_not_touch_any_customer_facing_result(self):
        before = self.snapshot()
        self.assertTrue(before["matches"])
        for company in Company.objects.all():
            record(company, event_key={"observation": "first_seen", "company": company.gemi_number})
        self.assertEqual(CompanySignal.objects.count(), 2)
        self.assertEqual(self.snapshot(), before)
