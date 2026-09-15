"""Tests for the company monitoring universe and refresh policy (A9, gemiapp.ingestion.monitoring).

Local fixtures only -- no GEMI call.
"""

import copy
from datetime import date, datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import AddConstraint, AddIndex, CreateModel
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from . import apps as gemi_apps
from .ingestion.monitoring import (
    ACTIVE,
    ACTIVE_OPPORTUNITY,
    ACTIVE_RADAR_MATCH,
    CRITICAL,
    DECAYING,
    DEFAULT_POLICY,
    HIGH,
    INACTIVE,
    LOW,
    MANUAL,
    NEW_COMPANY,
    NORMAL,
    PRIORITIES,
    REASONS,
    RECENT_SIGNAL,
    STATES,
    check_jitter_fraction,
    compute_next_check_at,
    recompute_company_monitoring,
    set_external_reason,
    set_manual_monitoring,
)
from .models import (
    ActivityCode, Company, CompanyActivity, CompanyMonitoring, CompanyMonitoringReason, CustomerRadar, DigestDelivery,
    ImportRun, RadarMatch, UserCompanyLead, UserSubscription,
)
from .services import (
    company_matches_radar, filter_companies_for_radar, import_for_date, match_imported_companies, send_digests,
    sync_company_activities,
)
from .tasks import recompute_gemi_company_monitoring_task
from .test_gemi_company_activities import MIXED, TARGET, entitled_user, entry, radar_for
from .test_gemi_validation import full_item

AS_OF = datetime(2026, 9, 16, 9, 0, tzinfo=dt_timezone.utc)
SECOND = timedelta(seconds=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
WINDOW = DEFAULT_POLICY.new_company_window
GRACE = DEFAULT_POLICY.decay_grace


def make_company(gemi_number="118717203000", *, first_seen=None, incorporated=date(2026, 9, 10), quality="valid", prefecture="ΑΤΤΙΚΗΣ"):
    company = Company.objects.create(
        gemi_number=gemi_number, name="ΕΤΑΙΡΕΙΑ ΙΚΕ", incorporation_date=incorporated, prefecture=prefecture, legal_type="ΙΚΕ",
    )
    Company.objects.filter(pk=company.pk).update(first_seen_at=first_seen, incorporation_date_quality=quality)
    return Company.objects.get(pk=company.pk)


def new_company(gemi_number="118717203000", *, days_ago=5, **kwargs):
    first_seen = AS_OF - timedelta(days=days_ago)
    return make_company(gemi_number, first_seen=first_seen, incorporated=(first_seen - 2 * DAY).date(), **kwargs)


def monitoring(company):
    return CompanyMonitoring.objects.filter(company=company).first()


def reason(company, name):
    return CompanyMonitoringReason.objects.filter(monitoring__company=company, reason=name).first()


def snapshot():
    return (
        list(CompanyMonitoring.objects.order_by("pk").values()),
        list(CompanyMonitoringReason.objects.order_by("pk").values()),
    )


def recompute(as_of=AS_OF, **kwargs):
    return recompute_company_monitoring(as_of=as_of, **kwargs)


class SchemaTests(TestCase):
    def test_fields_hold_no_personal_or_contact_data(self):
        self.assertEqual(
            {field.name for field in CompanyMonitoring._meta.concrete_fields},
            {"id", "company", "state", "priority", "primary_reason", "policy_version", "monitored_since", "decay_started_at",
             "inactive_since", "next_check_at", "last_checked_at", "last_success_at", "last_failure_at",
             "consecutive_failures", "created_at", "updated_at"},
        )
        self.assertEqual(
            {field.name for field in CompanyMonitoringReason._meta.concrete_fields},
            {"id", "monitoring", "reason", "active", "first_active_at", "activated_at", "deactivated_at", "activation_count",
             "expires_at", "source_ids", "created_at", "updated_at"},
        )

    def test_choices_are_the_policy_vocabulary(self):
        self.assertEqual({value for value, _ in CompanyMonitoring.REASONS}, set(REASONS))
        self.assertEqual({value for value, _ in CompanyMonitoring.PRIORITIES}, set(PRIORITIES))
        self.assertEqual({value for value, _ in CompanyMonitoring.STATES}, set(STATES))

    def test_one_state_per_company_and_one_row_per_reason(self):
        company = make_company()
        values = {"company": company, "state": ACTIVE, "priority": NORMAL, "policy_version": 1, "monitored_since": AS_OF, "next_check_at": AS_OF}
        row = CompanyMonitoring.objects.create(**values)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CompanyMonitoring.objects.create(**values)
        reason_values = {"monitoring": row, "reason": MANUAL, "active": True, "first_active_at": AS_OF, "activated_at": AS_OF, "activation_count": 1}
        CompanyMonitoringReason.objects.create(**reason_values)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CompanyMonitoringReason.objects.create(**reason_values)

    def test_state_consistency_constraints(self):
        company = make_company()
        for values in (
            {"state": INACTIVE, "priority": None, "next_check_at": AS_OF},
            {"state": ACTIVE, "priority": None, "next_check_at": AS_OF},
            {"state": DECAYING, "priority": LOW, "next_check_at": None},
        ):
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                CompanyMonitoring.objects.create(company=company, policy_version=1, monitored_since=AS_OF, **values)
        row = CompanyMonitoring.objects.create(company=company, state=ACTIVE, priority=NORMAL, policy_version=1, monitored_since=AS_OF, next_check_at=AS_OF)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CompanyMonitoringReason.objects.create(monitoring=row, reason=MANUAL, active=True, first_active_at=AS_OF, activated_at=AS_OF, deactivated_at=AS_OF, activation_count=1)

    def test_the_migration_only_creates_the_monitoring_tables(self):
        migration = MigrationLoader(None, ignore_no_migrations=True).get_migration("gemiapp", "0037_company_monitoring")
        self.assertEqual(migration.dependencies, [("gemiapp", "0036_activitycode_kad_links")])
        self.assertTrue(all(isinstance(op, (CreateModel, AddIndex, AddConstraint)) for op in migration.operations))
        self.assertEqual({op.name for op in migration.operations if isinstance(op, CreateModel)}, {"CompanyMonitoring", "CompanyMonitoringReason"})
        self.assertEqual(
            {op.model_name for op in migration.operations if not isinstance(op, CreateModel)},
            {"companymonitoring", "companymonitoringreason"},
        )


class NewCompanyTests(TestCase):
    def test_window_boundaries(self):
        inside = make_company("1", first_seen=AS_OF - WINDOW + SECOND, incorporated=date(2026, 8, 16))
        edge = make_company("2", first_seen=AS_OF - WINDOW, incorporated=date(2026, 8, 16))
        future = make_company("3", first_seen=AS_OF + HOUR, incorporated=date(2026, 9, 15))
        unknown = make_company("4", first_seen=None)
        unreliable = make_company("5", first_seen=AS_OF - DAY, incorporated=date(2026, 9, 14), quality="out_of_range")
        bulk_imported = make_company("6", first_seen=AS_OF - DAY, incorporated=date(2026, 5, 6))
        just_new = make_company("7", first_seen=AS_OF - DAY, incorporated=(AS_OF - DAY - WINDOW).date())
        too_old = make_company("8", first_seen=AS_OF - DAY, incorporated=(AS_OF - DAY - WINDOW - DAY).date())

        report = recompute()

        self.assertEqual(reason(inside, NEW_COMPANY).expires_at, AS_OF + SECOND)
        self.assertTrue(reason(just_new, NEW_COMPANY).active)
        for company in (edge, future, unknown, unreliable, bulk_imported, too_old):
            self.assertIsNone(monitoring(company), company.gemi_number)
        self.assertEqual(report.active_reasons[NEW_COMPANY], 2)
        self.assertEqual(monitoring(inside).priority, NORMAL)

    def test_the_reason_ends_exactly_when_the_window_ends(self):
        company = new_company(days_ago=29)
        recompute()
        expires = reason(company, NEW_COMPANY).expires_at
        recompute(as_of=expires - SECOND)
        self.assertTrue(reason(company, NEW_COMPANY).active)
        recompute(as_of=expires)
        item = reason(company, NEW_COMPANY)
        self.assertEqual((item.active, item.deactivated_at), (False, expires))
        self.assertEqual(monitoring(company).state, DECAYING)


class RadarReasonTests(TestCase):
    def setUp(self):
        self.user = entitled_user()
        self.company = make_company()

    def test_radar_changes_keep_one_monitoring_row(self):
        first = radar_for(self.user, "Αττική Α", prefectures=["ΑΤΤΙΚΗΣ"])
        recompute()
        item = reason(self.company, ACTIVE_RADAR_MATCH)
        self.assertEqual((item.active, item.source_ids, item.first_active_at), (True, [first.pk], AS_OF))
        self.assertEqual((monitoring(self.company).state, monitoring(self.company).priority), (ACTIVE, HIGH))

        first.is_active = False
        first.save()
        recompute(as_of=AS_OF + HOUR)
        item = reason(self.company, ACTIVE_RADAR_MATCH)
        self.assertEqual((item.active, item.deactivated_at), (False, AS_OF + HOUR))
        self.assertEqual(monitoring(self.company).state, DECAYING)

        first.is_active = True
        first.save()
        recompute(as_of=AS_OF + 2 * HOUR)
        item = reason(self.company, ACTIVE_RADAR_MATCH)
        self.assertEqual(
            (item.active, item.activation_count, item.first_active_at, item.activated_at, item.deactivated_at),
            (True, 2, AS_OF, AS_OF + 2 * HOUR, None),
        )
        self.assertEqual((monitoring(self.company).state, monitoring(self.company).monitored_since), (ACTIVE, AS_OF))

        second = radar_for(self.user, "Αττική Β", prefectures=["ΑΤΤΙΚΗΣ"])
        recompute(as_of=AS_OF + 3 * HOUR)
        self.assertEqual(reason(self.company, ACTIVE_RADAR_MATCH).source_ids, sorted([first.pk, second.pk]))
        self.assertEqual(CompanyMonitoring.objects.count(), 1)
        self.assertEqual(CompanyMonitoringReason.objects.count(), 1)

        first.deleted_at = timezone.now()
        first.save()
        recompute(as_of=AS_OF + 4 * HOUR)
        item = reason(self.company, ACTIVE_RADAR_MATCH)
        self.assertEqual((item.active, item.source_ids, item.activation_count), (True, [second.pk], 2))

    def test_only_radars_the_live_matcher_uses_count(self):
        other = User.objects.create_user("free@example.com", "free@example.com", "StrongPass123")
        radar_for(other, "Χωρίς συνδρομή", prefectures=["ΑΤΤΙΚΗΣ"])
        radar_for(self.user, "Σε σίγαση", prefectures=["ΑΤΤΙΚΗΣ"], frequency="off")
        radar_for(self.user, "Άλλη περιοχή", prefectures=["ΑΧΑΪΑΣ"])
        later = radar_for(self.user, "Αργότερα", prefectures=["ΑΤΤΙΚΗΣ"])
        CustomerRadar.objects.filter(pk=later.pk).update(monitor_from=timezone.make_aware(datetime(2026, 9, 11)))
        report = recompute()
        self.assertIsNone(monitoring(self.company))
        self.assertEqual(report.companies_without_reason, 1)

    def test_production_legacy_matching_an_ended_activity_still_counts(self):
        self.assertFalse(settings.GEMI_MATCH_CURRENT_ACTIVITIES_ONLY)
        sync_company_activities(self.company, [entry("47110000", dt_from="2019-01-01", dt_to="2026-02-01")], as_of=AS_OF.date())
        radar = radar_for(self.user, "Λιανικό", ["47110000"])
        recompute()
        self.assertEqual(reason(self.company, ACTIVE_RADAR_MATCH).source_ids, [radar.pk])
        company = Company.objects.prefetch_related("activity_records").get(pk=self.company.pk)
        self.assertTrue(company_matches_radar(company, CustomerRadar.objects.prefetch_related("activity_codes").get(pk=radar.pk))[0])


class PriorityTests(TestCase):
    def test_policy_is_pure_and_explainable(self):
        cases = {
            (NEW_COMPANY,): (NORMAL, NEW_COMPANY),
            (ACTIVE_RADAR_MATCH,): (HIGH, ACTIVE_RADAR_MATCH),
            (NEW_COMPANY, ACTIVE_RADAR_MATCH): (HIGH, ACTIVE_RADAR_MATCH),
            (MANUAL, ACTIVE_RADAR_MATCH): (HIGH, ACTIVE_RADAR_MATCH),
            (MANUAL,): (NORMAL, MANUAL),
            (MANUAL, NEW_COMPANY): (NORMAL, NEW_COMPANY),
            (RECENT_SIGNAL,): (HIGH, RECENT_SIGNAL),
            (RECENT_SIGNAL, ACTIVE_RADAR_MATCH): (HIGH, RECENT_SIGNAL),
            (ACTIVE_OPPORTUNITY, ACTIVE_RADAR_MATCH, NEW_COMPANY): (CRITICAL, ACTIVE_OPPORTUNITY),
            (): (None, None),
        }
        for reasons, expected in cases.items():
            with self.subTest(reasons=reasons):
                self.assertEqual((DEFAULT_POLICY.priority_for(reasons), DEFAULT_POLICY.primary_reason(reasons)), expected)

    def test_combinations_through_the_recompute(self):
        user = entitled_user()
        company = new_company()
        recompute()
        self.assertEqual((monitoring(company).priority, monitoring(company).primary_reason), (NORMAL, NEW_COMPANY))

        radar_for(user, "Αττική", prefectures=["ΑΤΤΙΚΗΣ"])
        report = recompute(as_of=AS_OF + HOUR)
        self.assertEqual((monitoring(company).priority, monitoring(company).primary_reason), (HIGH, ACTIVE_RADAR_MATCH))
        self.assertEqual((report.priority_changed, report.companies_with_multiple_reasons), (1, 1))

        set_manual_monitoring(company.pk, as_of=AS_OF + 2 * HOUR)
        self.assertEqual((monitoring(company).priority, monitoring(company).primary_reason), (HIGH, ACTIVE_RADAR_MATCH))
        self.assertTrue(reason(company, MANUAL).active)

        set_external_reason(company.pk, ACTIVE_OPPORTUNITY, as_of=AS_OF + 3 * HOUR)
        self.assertEqual((monitoring(company).priority, monitoring(company).primary_reason), (CRITICAL, ACTIVE_OPPORTUNITY))
        set_external_reason(company.pk, ACTIVE_OPPORTUNITY, active=False, as_of=AS_OF + 4 * HOUR)
        self.assertEqual(monitoring(company).priority, HIGH)
        self.assertEqual(CompanyMonitoring.objects.count(), 1)

    def test_manual_only_with_expiry(self):
        company = make_company()
        set_manual_monitoring(company.pk, as_of=AS_OF, expires_at=AS_OF + 2 * DAY)
        row = monitoring(company)
        self.assertEqual((row.state, row.priority, row.primary_reason, row.monitored_since), (ACTIVE, NORMAL, MANUAL, AS_OF))
        recompute(as_of=AS_OF + DAY)
        self.assertTrue(reason(company, MANUAL).active)
        recompute(as_of=AS_OF + 2 * DAY)
        self.assertEqual((reason(company, MANUAL).active, reason(company, MANUAL).deactivated_at), (False, AS_OF + 2 * DAY))
        self.assertEqual(monitoring(company).state, DECAYING)

    def test_reserved_reasons_are_never_derived(self):
        user = entitled_user()
        company = make_company()
        UserCompanyLead.objects.create(user=user, company=company, status="interested", is_favorite=True, notes="Σημείωση")
        report = recompute()
        self.assertEqual((report.active_reasons[ACTIVE_OPPORTUNITY], report.active_reasons[RECENT_SIGNAL]), (0, 0))
        self.assertIsNone(monitoring(company))
        for derived in (NEW_COMPANY, ACTIVE_RADAR_MATCH):
            with self.assertRaises(ValueError):
                set_external_reason(company.pk, derived, as_of=AS_OF)


class DecayTests(TestCase):
    def setUp(self):
        self.user = entitled_user()
        self.company = make_company()
        self.radar = radar_for(self.user, "Αττική", prefectures=["ΑΤΤΙΚΗΣ"])
        recompute()

    def set_radar(self, active):
        self.radar.is_active = active
        self.radar.save()

    def test_grace_period_then_unmonitored(self):
        self.set_radar(False)
        started = AS_OF + DAY
        report = recompute(as_of=started)
        row = monitoring(self.company)
        self.assertEqual((report.decayed, row.state, row.priority, row.primary_reason, row.decay_started_at), (1, DECAYING, LOW, None, started))
        self.assertEqual(row.next_check_at, compute_next_check_at(DEFAULT_POLICY, priority=LOW, company_id=self.company.pk, monitored_since=AS_OF, last_checked_at=None))

        recompute(as_of=started + GRACE - SECOND)
        self.assertEqual(monitoring(self.company).state, DECAYING)

        report = recompute(as_of=started + GRACE)
        row = monitoring(self.company)
        self.assertEqual(
            (report.unmonitored, row.state, row.priority, row.next_check_at, row.inactive_since),
            (1, INACTIVE, None, None, started + GRACE),
        )
        self.assertFalse(reason(self.company, ACTIVE_RADAR_MATCH).active)  # history kept

    def test_a_reason_returning_during_grace_resumes_the_same_period(self):
        self.set_radar(False)
        recompute(as_of=AS_OF + DAY)
        self.set_radar(True)
        report = recompute(as_of=AS_OF + 10 * DAY)
        row = monitoring(self.company)
        self.assertEqual((row.state, row.priority, row.decay_started_at, row.monitored_since), (ACTIVE, HIGH, None, AS_OF))
        self.assertEqual(report.entered_monitoring, 0)

    def test_returning_after_leaving_starts_a_new_period(self):
        self.set_radar(False)
        recompute(as_of=AS_OF + DAY)
        recompute(as_of=AS_OF + DAY + GRACE)
        self.set_radar(True)
        back = AS_OF + 40 * DAY
        report = recompute(as_of=back)
        row = monitoring(self.company)
        item = reason(self.company, ACTIVE_RADAR_MATCH)
        self.assertEqual((report.entered_monitoring, report.monitored_created), (1, 0))
        self.assertEqual((row.state, row.monitored_since, row.inactive_since), (ACTIVE, back, None))
        self.assertEqual((item.activation_count, item.first_active_at, item.activated_at), (2, AS_OF, back))


class NextCheckTests(TestCase):
    def test_cadence_by_priority_before_any_check(self):
        intervals = [DEFAULT_POLICY.interval(priority) for priority in (CRITICAL, HIGH, NORMAL, LOW)]
        self.assertEqual(intervals, sorted(intervals))
        for priority in PRIORITIES:
            value = compute_next_check_at(DEFAULT_POLICY, priority=priority, company_id=42, monitored_since=AS_OF, last_checked_at=None)
            self.assertEqual(value, AS_OF + DEFAULT_POLICY.interval(priority) * check_jitter_fraction(42))
            self.assertTrue(AS_OF <= value < AS_OF + DEFAULT_POLICY.interval(priority))

    def test_after_a_collector_check(self):
        checked = AS_OF - 2 * DAY
        interval = DEFAULT_POLICY.interval(HIGH)
        value = compute_next_check_at(DEFAULT_POLICY, priority=HIGH, company_id=7, monitored_since=AS_OF - 30 * DAY, last_checked_at=checked)
        self.assertTrue(checked + interval <= value < checked + interval + interval * DEFAULT_POLICY.checked_jitter_fraction)

    def test_jitter_is_reproducible_and_spread(self):
        fractions = [check_jitter_fraction(pk) for pk in range(1, 1001)]
        self.assertEqual(fractions, [check_jitter_fraction(pk) for pk in range(1, 1001)])
        self.assertTrue(all(0 <= value < 1 for value in fractions))
        deciles = [sum(1 for value in fractions if low / 10 <= value < (low + 1) / 10) for low in range(10)]
        self.assertTrue(all(60 <= count <= 140 for count in deciles), deciles)

    def test_recompute_never_invents_a_check_and_keeps_the_schedule_stable(self):
        user = entitled_user()
        company = make_company()
        radar_for(user, "Αττική", prefectures=["ΑΤΤΙΚΗΣ"])
        recompute()
        row = monitoring(company)
        self.assertEqual((row.last_checked_at, row.last_success_at, row.last_failure_at, row.consecutive_failures), (None, None, None, 0))
        self.assertIsNotNone(row.next_check_at.tzinfo)
        scheduled = row.next_check_at

        recompute(as_of=AS_OF + 2 * DAY)
        self.assertEqual(monitoring(company).next_check_at, scheduled)

        CompanyMonitoring.objects.filter(pk=row.pk).update(last_checked_at=AS_OF + DAY)  # what a future collector writes
        report = recompute(as_of=AS_OF + 3 * DAY)
        row = monitoring(company)
        self.assertEqual(row.last_checked_at, AS_OF + DAY)
        self.assertEqual(row.next_check_at, compute_next_check_at(DEFAULT_POLICY, priority=HIGH, company_id=company.pk, monitored_since=AS_OF, last_checked_at=AS_OF + DAY))
        self.assertEqual(report.next_check_changed, 1)

    def test_as_of_must_be_timezone_aware(self):
        with self.assertRaises(ValueError):
            recompute_company_monitoring(as_of=datetime(2026, 9, 16, 9, 0))


class RecomputeTests(TestCase):
    def setUp(self):
        self.user = entitled_user()
        radar_for(self.user, "Αττική", prefectures=["ΑΤΤΙΚΗΣ"])
        self.attica = [make_company(f"11871720{index}000") for index in range(3)]
        self.new_elsewhere = [new_company(f"11871721{index}000", prefecture="ΑΧΑΪΑΣ") for index in range(2)]
        self.both = new_company("118717230000")
        self.neither = make_company("118717240000", prefecture="ΑΧΑΪΑΣ")

    def test_counts(self):
        report = recompute()
        self.assertEqual((report.companies_in_scope, report.companies_inspected, report.monitored_created), (7, 6, 6))
        self.assertEqual(dict(report.active_reasons), {ACTIVE_RADAR_MATCH: 4, NEW_COMPANY: 3})
        self.assertEqual((report.companies_with_multiple_reasons, report.companies_without_reason), (1, 1))
        self.assertEqual(dict(report.priorities), {HIGH: 4, NORMAL: 2})
        self.assertEqual(sum(report.next_check.values()), 6)
        self.assertIsNone(monitoring(self.neither))

    def test_a_second_recompute_changes_nothing(self):
        recompute()
        before = snapshot()
        second = recompute(as_of=AS_OF + HOUR)
        self.assertEqual(
            (second.monitored_created, second.entered_monitoring, second.priority_changed, second.next_check_changed,
             second.decayed, second.unmonitored, sum(second.reasons_activated.values()), sum(second.reasons_deactivated.values())),
            (0, 0, 0, 0, 0, 0, 0, 0),
        )
        self.assertEqual(second.unchanged, 6)
        self.assertEqual(snapshot(), before)

    def test_a_dry_run_reports_and_writes_nothing(self):
        preview = recompute(dry_run=True)
        self.assertEqual(CompanyMonitoring.objects.count() + CompanyMonitoringReason.objects.count(), 0)
        real = recompute()
        self.assertEqual([line.replace("[dry-run] ", "") for line in preview.lines()], real.lines())

    def test_batches_resume_and_targeted_recompute(self):
        small = recompute(dry_run=True, batch_size=2)
        large = recompute(dry_run=True)
        self.assertEqual((small.batches, large.batches), (3, 1))
        self.assertEqual(small.lines()[1:], large.lines()[1:])

        targeted = recompute(company_ids=[self.attica[0].pk, self.neither.pk])
        self.assertEqual((targeted.scope, targeted.companies_in_scope, targeted.monitored_created), ("targeted", 2, 1))
        self.assertEqual(CompanyMonitoring.objects.count(), 1)

        resumed = recompute(start_company_id=self.attica[2].pk)
        self.assertEqual(resumed.monitored_created, 3)
        self.assertIsNone(monitoring(self.attica[1]))
        with self.assertRaises(ValueError):
            recompute(batch_size=0)

    def test_command_task_schedule_and_privacy(self):
        out = StringIO()
        with self.assertLogs("gemiapp", level="INFO") as logs:
            call_command("recompute_gemi_company_monitoring", "--dry-run", "--as-of", AS_OF.isoformat(), stdout=out)
        text = out.getvalue()
        self.assertIn("[dry-run] monitoring rows created=6", text)
        self.assertIn("active_opportunity reserved: not populated", text)
        for private in ("ΕΤΑΙΡΕΙΑ ΙΚΕ", "member@example.com", "118717200000"):
            self.assertNotIn(private, text + "\n".join(logs.output))
        for arguments in (("--as-of", "2026-09-16T09:00:00"), ("--batch-size", "0"), ("--start-company-id", "-1")):
            with self.assertRaises(CommandError):
                call_command("recompute_gemi_company_monitoring", *arguments, stdout=StringIO())
        call_command("recompute_gemi_company_monitoring", "--company-id", str(self.both.pk), "--as-of", AS_OF.isoformat(), stdout=StringIO())
        self.assertEqual(monitoring(self.both).primary_reason, ACTIVE_RADAR_MATCH)

        summary = recompute_gemi_company_monitoring_task()
        self.assertEqual(summary["scope"], "full")
        self.assertIn("monitored_created", summary)
        self.assertNotIn("gemiapp.tasks.recompute_gemi_company_monitoring_task", [item["func"] for item in gemi_apps.SCHEDULES])


class CustomerParityTests(TestCase):
    """Recomputing monitoring changes nothing customer-visible."""

    CODES = ("62010000", "47110000")

    def setUp(self):
        self.user = entitled_user()
        self.client.login(username="member@example.com", password="StrongPass123")
        self.items = [
            full_item("118717203000", TARGET.isoformat(), activities=copy.deepcopy(MIXED)),
            full_item("118717204000", TARGET.isoformat(), activities=[entry("47110000", dt_from="2001-01-01", dt_to="2020-01-01")]),
        ]
        with patch("gemiapp.services.fetch_companies", side_effect=lambda _: copy.deepcopy(self.items)):
            import_for_date(TARGET)
        radar_for(self.user, "Λογισμικό", ["62010000"])
        radar_for(self.user, "Λιανικό", ["47110000"])
        radar_for(self.user, "Αττική", prefectures=["ΑΤΤΙΚΗΣ"])

    def rematch(self):
        UserCompanyLead.objects.all().delete()
        DigestDelivery.objects.all().delete()
        match_imported_companies(ImportRun.objects.create(target_date=TARGET, status="success"))

    def snapshot(self):
        from .views import _filtered_companies

        factory = RequestFactory()
        companies = list(Company.objects.prefetch_related("activity_records").order_by("pk"))
        radars = list(CustomerRadar.objects.prefetch_related("activity_codes").order_by("pk"))
        state = {
            "companies": list(Company.objects.order_by("pk").values()),
            "activities": list(CompanyActivity.objects.order_by("pk").values()),
            "catalogue_count": ActivityCode.objects.count(),
            "radars": list(CustomerRadar.objects.order_by("pk").values()),
            "criteria": sorted(CustomerRadar.activity_codes.through.objects.values_list("customerradar_id", "activitycode_id")),
            "matches": list(RadarMatch.objects.order_by("radar_id", "company_id").values_list("radar_id", "company_id", "matched_on", "matched_activity_codes", "match_reason")),
            "leads": list(UserCompanyLead.objects.order_by("company_id").values_list("user_id", "company_id", "status", "is_favorite", "notes")),
            "subscriptions": list(UserSubscription.objects.order_by("pk").values()),
            "predicate": [(r.pk, c.pk, company_matches_radar(c, r)) for r in radars for c in companies],
            "preview": {code: sorted(filter_companies_for_radar(Company.objects.all(), activity_codes=[code]).values_list("pk", flat=True)) for code in self.CODES},
            "dashboard": {code: sorted(_filtered_companies(factory.get("/", {"kad": code})).values_list("pk", flat=True)) for code in self.CODES},
            "picker": [self.client.get(reverse("kad_search"), {"q": query}).json() for query in ("62", "4711", "προγραμματ")],
        }
        with patch("django.core.signing.time.time", return_value=1789000000.0):
            mail.outbox = []
            state["digest"] = (send_digests(TARGET), [(m.subject, m.body) for m in mail.outbox])
            state["export"] = {code: self.client.get(reverse("export_csv"), {"kad": code}).content for code in self.CODES}
            state["lead_export"] = self.client.get(reverse("lead_export_csv")).content
            state["radar_exports"] = [self.client.get(reverse("radar_export_csv", args=[r.pk])).content for r in radars]
        state["detail"] = [
            self.client.get(reverse("company_detail", args=[c.gemi_number])).content.decode()
            .split("ΔΡΑΣΤΗΡΙΟΤΗΤΕΣ ΚΑΔ", 1)[1].split("<!-- Lead Work Column", 1)[0]
            for c in companies
        ]
        return state

    def test_recompute_changes_nothing_customer_visible(self):
        self.rematch()
        before = self.snapshot()
        self.assertTrue(before["matches"])
        report = recompute()
        self.assertGreater(report.monitored_created, 0)
        self.rematch()
        self.assertEqual(self.snapshot(), before)
