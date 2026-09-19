"""Tests for the Organization Radar foundation (C3, gemiapp.organization_radars).

Canonical reference rows are fixtures; nothing depends on a live A5 sync. Organization Radars are dormant
configuration: CustomerRadar stays the production Radar and nothing consumes the new model.
"""

import inspect
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase

from . import organization_radars as c3
from .ingestion.monitoring import radar_match_evidence
from .ingestion.refresh import RefreshPolicy, build_company_refresh_plan
from .models import (
    Company, CompanyMonitoring, CompanySignal, CompanySnapshot, CustomerRadar, GemiKad, Organization, OrganizationICP,
    OrganizationRadar, OrganizationRadarExclusion, OrganizationRadarKad, OrganizationRadarLegalForm,
    OrganizationRadarRegion, OrganizationRadarSignalType, RadarMatch, UserCompanyLead, UserSubscription,
)
from .organization_icp import create_organization_icp
from .organization_radars import (
    RadarDefinition,
    RadarError,
    create_organization_radar,
    get_organization_radar_definition,
    is_radar_configured,
    replace_organization_radar,
    set_organization_radar_active,
)
from .organizations import create_organization
from .services import company_matches_radar, eligible_radars
from .test_gemi_company_activities import entitled_user, radar_for
from .test_organization_icp import Refs, ref

RADAR_TABLES = (OrganizationRadar, OrganizationRadarKad, OrganizationRadarRegion, OrganizationRadarLegalForm,
                OrganizationRadarSignalType, OrganizationRadarExclusion)


def organization(name="Fixture Supplier", email="owner@example.com"):
    owner = User.objects.create_user(email, email, "StrongPass123")
    return create_organization(owner=owner, name=name).organization


def counts():
    return tuple(model.objects.count() for model in RADAR_TABLES)


def hospitality(r, **overrides):
    values = dict(
        name="New hospitality businesses in Attica", active=True, score_threshold=75,
        kads=(r.kad_2026,), regions=(r.attica,), legal_forms=(r.ike,), signal_types=("new_company",),
        exclusions=(r.kifisia,),
    )
    values.update(overrides)
    return RadarDefinition(**values)


class RadarTestCase(TestCase):
    def setUp(self):
        self.refs = Refs()
        self.org = organization()


# --- schema ----------------------------------------------------------------------------------------

class SchemaTests(TestCase):
    def test_root_fields_follow_section_25_and_belong_to_an_organization(self):
        fields = {f.name: f for f in OrganizationRadar._meta.get_fields() if f.concrete}
        self.assertEqual(set(fields), {"id", "organization", "name", "active", "score_threshold", "created_at", "updated_at"})
        self.assertFalse(fields["organization"].one_to_one)  # many Radars per organization
        self.assertEqual(fields["organization"].remote_field.on_delete.__name__, "CASCADE")
        self.assertIs(fields["active"].default, False)
        for model in RADAR_TABLES:
            for field in model._meta.get_fields():
                if field.concrete and field.is_relation:
                    self.assertNotIn(field.related_model, (User, UserSubscription, CustomerRadar), f"{model.__name__}.{field.name}")

    def test_no_json_personal_or_free_text_criteria(self):
        from django.db.models import JSONField, TextField

        for model in RADAR_TABLES:
            names = [f.name for f in model._meta.get_fields() if f.concrete]
            self.assertFalse([f for f in model._meta.get_fields() if isinstance(f, (JSONField, TextField))], model.__name__)
            for forbidden in ("email", "phone", "person", "note", "regex", "expression", "description", "user"):
                self.assertFalse([n for n in names if forbidden in n], f"{model.__name__}: {forbidden}")

    def test_the_migration_is_additive_only(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        operations = loader.disk_migrations[("gemiapp", "0046_organization_radar")].operations
        self.assertLessEqual({type(op).__name__ for op in operations}, {"CreateModel", "AddConstraint", "AddIndex"})
        self.assertEqual({op.name for op in operations if type(op).__name__ == "CreateModel"},
                         {model.__name__ for model in RADAR_TABLES})

    def test_legacy_models_are_not_linked(self):
        for model in (CustomerRadar, RadarMatch, UserCompanyLead, UserSubscription, CompanyMonitoring, OrganizationICP):
            self.assertFalse([f for f in model._meta.get_fields() if "organization_radar" in f.name or
                              getattr(f, "related_model", None) is OrganizationRadar], model.__name__)


# --- root ------------------------------------------------------------------------------------------

class RootTests(RadarTestCase):
    def test_the_fixture_radar_round_trips_and_an_organization_holds_many(self):
        first = create_organization_radar(self.org, hospitality(self.refs))
        second = create_organization_radar(self.org, RadarDefinition(name="New hospitality businesses in Attica"))
        self.assertEqual(self.org.radars.count(), 2)  # duplicate names are labels, not identity
        self.assertEqual(get_organization_radar_definition(self.org, first), hospitality(self.refs))
        self.assertEqual(counts(), (2, 1, 1, 1, 1, 1))
        self.assertEqual((second.active, second.score_threshold), (False, None))
        exclusion = OrganizationRadarExclusion.objects.get()
        self.assertEqual((exclusion.subject, exclusion.municipality_id), ("municipality", self.refs.kifisia.pk))

    def test_names_are_required_trimmed_and_bounded(self):
        radar = create_organization_radar(self.org, RadarDefinition(name="  Attica  "))
        self.assertEqual(radar.name, "Attica")
        for bad in ("", "   ", "x" * 81, None, 5):
            with self.subTest(name=bad), self.assertRaises(RadarError):
                create_organization_radar(self.org, RadarDefinition(name=bad))
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationRadar.objects.create(organization=self.org, name="")

    def test_score_threshold_is_an_optional_whole_number_from_0_to_100(self):
        for value in (None, 0, 55, 100):
            with self.subTest(value=value):
                radar = create_organization_radar(self.org, RadarDefinition(name="r", score_threshold=value))
                self.assertEqual(radar.score_threshold, value)
        for bad in (-1, 101, 75.5, "80", True):
            with self.subTest(bad=bad), self.assertRaises(RadarError):
                create_organization_radar(self.org, RadarDefinition(name="r", score_threshold=bad))
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationRadar.objects.create(organization=self.org, name="r", score_threshold=101)

    def test_nothing_evaluates_the_threshold(self):
        source = inspect.getsource(c3).split('"""', 2)[2]
        for forbidden in ("score >", "score <", ">= radar.score_threshold", "calculate", "Opportunity"):
            self.assertNotIn(forbidden, source)


# --- criteria --------------------------------------------------------------------------------------

class CriteriaTests(RadarTestCase):
    def test_kad_identity_is_code_and_version(self):
        radar = create_organization_radar(self.org, RadarDefinition(name="r", kads=(self.refs.kad_2026, self.refs.kad_2008)))
        self.assertEqual(set(radar.kads.values_list("kad__source_id", "kad__kad_version")),
                         {("62010000", "kad_2026"), ("62010000", "kad_2008")})

    def test_duplicates_retired_and_non_canonical_references_are_rejected(self):
        retired = ref(GemiKad, "11111111", kad_version="kad_2008", present=False)
        for definition in (
            RadarDefinition(name="r", kads=(self.refs.kad_2026, self.refs.kad_2026)),
            RadarDefinition(name="r", kads=(retired,)),
            RadarDefinition(name="r", kads=("5229",)),
            RadarDefinition(name="r", kads=(GemiKad(source_id="1"),)),
            RadarDefinition(name="r", regions=(self.refs.attica, self.refs.attica)),
            RadarDefinition(name="r", regions=("ΑΤΤΙΚΗΣ",)),
            RadarDefinition(name="r", regions=(self.refs.ike,)),
            RadarDefinition(name="r", legal_forms=(self.refs.ike, self.refs.ike)),
            RadarDefinition(name="r", legal_forms=(self.refs.attica,)),
            RadarDefinition(name="r", exclusions=(self.refs.active,)),
            RadarDefinition(name="r", exclusions=("free text",)),
        ):
            with self.subTest(definition=definition), self.assertRaises(RadarError):
                create_organization_radar(self.org, definition)
        self.assertEqual(counts(), (0, 0, 0, 0, 0, 0))

    def test_region_levels_never_collide(self):
        radar = create_organization_radar(self.org, RadarDefinition(name="r", regions=(self.refs.thessaloniki, self.refs.kifisia)))
        self.assertEqual(self.refs.thessaloniki.source_id, self.refs.kifisia.source_id)
        self.assertEqual(sorted(radar.regions.values_list("level", flat=True)), ["municipality", "prefecture"])
        for bad in ({"level": "prefecture", "municipality": self.refs.kifisia},
                    {"level": "municipality", "prefecture": self.refs.attica, "municipality": self.refs.kifisia}):
            with self.subTest(bad=bad), self.assertRaises(IntegrityError), transaction.atomic():
                OrganizationRadarRegion.objects.create(radar=radar, **bad)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationRadarRegion.objects.create(radar=radar, level="prefecture", prefecture=self.refs.thessaloniki)

    def test_signal_types_are_implemented_only_and_unique(self):
        radar = create_organization_radar(self.org, RadarDefinition(name="r", signal_types=c3.implemented_signal_types()))
        self.assertEqual(radar.signal_types.count(), 6)
        for bad in (("capital_increase",), ("invented",), ("kad_added", "kad_added")):
            with self.subTest(bad=bad), self.assertRaises(RadarError):
                create_organization_radar(self.org, RadarDefinition(name="r", signal_types=bad))
        for bad in ("new_company", "invented"):
            with self.subTest(db=bad), self.assertRaises(IntegrityError), transaction.atomic():
                OrganizationRadarSignalType.objects.create(radar=radar, signal_type=bad)


class ExclusionTests(RadarTestCase):
    def test_structured_exclusions_on_every_supported_subject(self):
        radar = create_organization_radar(self.org, RadarDefinition(
            name="r", signal_types=("new_company",),
            exclusions=(self.refs.kad_other, self.refs.thessaloniki, self.refs.kifisia, self.refs.oe)))
        self.assertEqual(sorted(radar.exclusions.values_list("subject", flat=True)),
                         ["kad", "legal_form", "municipality", "prefecture"])

    def test_the_same_identity_cannot_be_targeted_and_excluded(self):
        for field, value in (("kads", self.refs.kad_2026), ("regions", self.refs.attica),
                             ("regions", self.refs.kifisia), ("legal_forms", self.refs.ike)):
            with self.subTest(field=field), self.assertRaisesMessage(RadarError, "both targeted and excluded"):
                create_organization_radar(self.org, RadarDefinition(name="r", exclusions=(value,), **{field: (value,)}))

    def test_different_levels_and_versions_are_not_contradictions(self):
        create_organization_radar(self.org, RadarDefinition(
            name="r", regions=(self.refs.attica,), kads=(self.refs.kad_2026,),
            exclusions=(self.refs.kifisia, self.refs.kad_2008)))
        self.assertEqual(OrganizationRadarExclusion.objects.count(), 2)

    def test_the_database_enforces_subject_consistency_and_uniqueness(self):
        radar = create_organization_radar(self.org, RadarDefinition(name="r"))
        for bad in ({"subject": "kad"}, {"subject": "kad", "prefecture": self.refs.attica},
                    {"subject": "legal_form", "legal_type": self.refs.ike, "kad": self.refs.kad_2026},
                    {"subject": "company_name", "kad": self.refs.kad_2026}):
            with self.subTest(bad=bad), self.assertRaises(IntegrityError), transaction.atomic():
                OrganizationRadarExclusion.objects.create(radar=radar, **bad)
        OrganizationRadarExclusion.objects.create(radar=radar, subject="kad", kad=self.refs.kad_2026)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationRadarExclusion.objects.create(radar=radar, subject="kad", kad=self.refs.kad_2026)


# --- activation -----------------------------------------------------------------------------------

class ActivationTests(RadarTestCase):
    def test_inactive_drafts_may_be_empty_but_active_radars_need_a_positive_criterion(self):
        draft = create_organization_radar(self.org, RadarDefinition(name="draft"))
        self.assertFalse(is_radar_configured(draft))
        for definition in (RadarDefinition(name="a", active=True),
                           RadarDefinition(name="a", active=True, exclusions=(self.refs.kifisia,)),
                           RadarDefinition(name="a", active=True, score_threshold=90)):
            with self.subTest(definition=definition), self.assertRaises(RadarError):
                create_organization_radar(self.org, definition)
        with self.assertRaises(RadarError):
            set_organization_radar_active(self.org, draft, True)
        for field, value in (("kads", (self.refs.kad_2026,)), ("regions", (self.refs.attica,)),
                             ("legal_forms", (self.refs.ike,)), ("signal_types", ("new_company",))):
            with self.subTest(field=field):
                radar = create_organization_radar(self.org, RadarDefinition(name=field, active=True, **{field: value}))
                self.assertTrue(radar.active and is_radar_configured(radar))

    def test_activation_and_deactivation_only_change_configuration(self):
        radar = create_organization_radar(self.org, RadarDefinition(name="r", signal_types=("new_company",)))
        set_organization_radar_active(self.org, radar, True)
        radar.refresh_from_db()
        self.assertTrue(radar.active)
        set_organization_radar_active(self.org, radar, False)
        radar.refresh_from_db()
        self.assertFalse(radar.active)
        self.assertEqual(radar.signal_types.count(), 1)
        with self.assertRaises(RadarError):
            set_organization_radar_active(self.org, radar, "yes")

    def test_an_active_radar_has_zero_business_side_effects(self):
        user = entitled_user("legacy@example.com")
        radar_for(user, "legacy", prefectures=["ΑΤΤΙΚΗΣ"])
        company = Company.objects.create(gemi_number="100", name="Χ", incorporation_date=date(2026, 9, 1), prefecture="ΑΤΤΙΚΗΣ")
        tables = (RadarMatch, UserCompanyLead, CompanyMonitoring, CompanySignal, CompanySnapshot)
        before = [model.objects.count() for model in tables]
        with patch("gemiapp.ingestion.refresh.get_gemi_client") as client, \
             patch("gemiapp.services.get_gemi_client") as legacy_client:
            radar = create_organization_radar(self.org, hospitality(self.refs))
            set_organization_radar_active(self.org, radar, False)
            set_organization_radar_active(self.org, radar, True)
        client.assert_not_called()
        legacy_client.assert_not_called()
        self.assertEqual([model.objects.count() for model in tables], before)
        self.assertFalse(Company.objects.get(pk=company.pk).radar_matches.exists())


# --- services -------------------------------------------------------------------------------------

class ServiceTests(RadarTestCase):
    def test_a_failing_create_writes_nothing(self):
        with patch.object(OrganizationRadarSignalType.objects, "bulk_create", side_effect=IntegrityError("forced")):
            with self.assertRaises(IntegrityError):
                create_organization_radar(self.org, hospitality(self.refs))
        self.assertEqual(counts(), (0, 0, 0, 0, 0, 0))

    def test_replace_succeeds_as_a_whole(self):
        radar = create_organization_radar(self.org, RadarDefinition(name="draft", kads=(self.refs.kad_2008,)))
        replace_organization_radar(self.org, radar, hospitality(self.refs))
        self.assertEqual(get_organization_radar_definition(self.org, radar), hospitality(self.refs))
        self.assertEqual(counts(), (1, 1, 1, 1, 1, 1))

    def test_an_invalid_or_failing_replace_leaves_the_radar_unchanged(self):
        radar = create_organization_radar(self.org, hospitality(self.refs))
        original = get_organization_radar_definition(self.org, radar)
        for bad in (hospitality(self.refs, exclusions=(self.refs.attica,)), hospitality(self.refs, name=""),
                    RadarDefinition(name="empty but active", active=True)):
            with self.subTest(bad=bad), self.assertRaises(RadarError):
                replace_organization_radar(self.org, radar, bad)
        with patch.object(OrganizationRadarExclusion.objects, "bulk_create", side_effect=IntegrityError("forced")):
            with self.assertRaises(IntegrityError):
                replace_organization_radar(self.org, radar, RadarDefinition(name="new", kads=(self.refs.kad_2008,)))
        self.assertEqual(get_organization_radar_definition(self.org, radar), original)


# --- tenancy --------------------------------------------------------------------------------------

class TenancyTests(RadarTestCase):
    def setUp(self):
        super().setUp()
        self.other = organization("Other", "other@example.com")
        self.radar_a = create_organization_radar(self.org, hospitality(self.refs))
        self.radar_b = create_organization_radar(self.other, RadarDefinition(name="B", kads=(self.refs.kad_other,)))

    def test_one_organization_can_never_read_or_mutate_anothers_radar(self):
        for call in (
            lambda: replace_organization_radar(self.org, self.radar_b, RadarDefinition(name="hijack")),
            lambda: set_organization_radar_active(self.org, self.radar_b, True),
            lambda: get_organization_radar_definition(self.org, self.radar_b),
        ):
            with self.assertRaisesMessage(RadarError, "does not belong"):
                call()
        self.assertEqual(get_organization_radar_definition(self.other, self.radar_b),
                         RadarDefinition(name="B", kads=(self.refs.kad_other,)))

    def test_services_require_explicit_saved_organizations_and_radars(self):
        user = self.org.members.get().user
        for bad_org in (None, user, Organization(name="unsaved"), "1"):
            with self.subTest(org=bad_org), self.assertRaises(RadarError):
                create_organization_radar(bad_org, RadarDefinition(name="r"))
        for bad_radar in (None, OrganizationRadar(name="unsaved"), self.org):
            with self.subTest(radar=bad_radar), self.assertRaises(RadarError):
                replace_organization_radar(self.org, bad_radar, RadarDefinition(name="r"))

    def test_deleting_one_organization_leaves_the_other_untouched(self):
        self.org.delete()
        self.assertEqual(counts(), (1, 1, 0, 0, 0, 0))
        self.assertEqual(OrganizationRadar.objects.get().organization, self.other)

    def test_deleting_the_owner_user_keeps_organization_radars(self):
        self.org.members.get().user.delete()
        self.assertTrue(OrganizationRadar.objects.filter(pk=self.radar_a.pk).exists())

    def test_the_icp_is_never_copied_into_or_narrowing_a_radar(self):
        from .organization_icp import ICPCriteria, KadCriterion

        create_organization_icp(self.org, ICPCriteria(kads=(KadCriterion(self.refs.kad_2008),)))
        radar = create_organization_radar(self.org, RadarDefinition(name="independent"))
        self.assertEqual(get_organization_radar_definition(self.org, radar), RadarDefinition(name="independent"))
        source = inspect.getsource(c3).split('"""', 2)[2]
        self.assertNotIn("ICP", source)


# --- legacy parity, safety ------------------------------------------------------------------------

class LegacyParityTests(RadarTestCase):
    def test_customer_radar_matching_monitoring_b4_and_billing_are_unchanged(self):
        user = entitled_user("legacy@example.com")
        legacy = radar_for(user, "legacy", prefectures=["ΑΤΤΙΚΗΣ"])
        company = Company.objects.create(gemi_number="200", name="Χ", incorporation_date=date(2026, 9, 5), prefecture="ΑΤΤΙΚΗΣ")
        run_at = datetime(2026, 9, 16, 9, tzinfo=dt_timezone.utc)
        monitoring = CompanyMonitoring.objects.create(
            company=company, state="active", priority="high", primary_reason="active_radar_match", policy_version=1,
            monitored_since=run_at - timedelta(days=3), next_check_at=run_at - timedelta(hours=1))
        policy = RefreshPolicy(max_requests_per_run=5, max_pages_per_query=2, max_direct_details_per_run=2, page_size=2)

        def world():
            return (
                list(CustomerRadar.objects.values()), [r.pk for r in eligible_radars()],
                company_matches_radar(Company.objects.get(pk=company.pk), CustomerRadar.objects.get(pk=legacy.pk)),
                {pk: e.source_ids for pk, e in radar_match_evidence().items()},
                build_company_refresh_plan(run_at=run_at, policy=policy).summary(),
                list(UserSubscription.objects.values()), UserSubscription.objects.get(user=user).has_entitlement,
                list(CompanyMonitoring.objects.values()), RadarMatch.objects.count(),
            )

        before = world()
        radar = create_organization_radar(self.org, hospitality(self.refs))
        set_organization_radar_active(self.org, radar, False)
        replace_organization_radar(self.org, radar, hospitality(self.refs, name="changed"))
        self.assertEqual(world(), before)
        self.assertEqual(monitoring.reasons.count(), 0)

    def test_nothing_in_the_product_reads_organization_radars(self):
        from django.conf import settings

        for path in ("gemiapp/urls.py", "gemiapp/views.py", "config/urls.py", "gemiapp/tasks.py", "gemiapp/apps.py",
                     "gemiapp/services.py", "gemiapp/billing.py", "gemiapp/forms.py", "gemiapp/adapters.py",
                     "gemiapp/ingestion/monitoring.py", "gemiapp/ingestion/refresh.py", "gemiapp/company_timeline.py",
                     "gemiapp/snapshot_change_signals.py", "gemiapp/organization_icp.py"):
            source = open(path, encoding="utf-8").read()
            if path == "gemiapp/urls.py":
                # Since the customer workspace the URL conf routes one read-only Radars page -- to
                # organization_views, which reads Radars only through organization_access. No other mention.
                source = source.replace('organization_views.workspace_radars, name="organization_radars"', "")
            self.assertNotIn("OrganizationRadar", source, path)
            self.assertNotIn("organization_radar", source, path)
        self.assertFalse([m for m in settings.MIDDLEWARE if "organization" in m.lower() or "radar" in m.lower()])
        code = inspect.getsource(c3).split('"""', 2)[2]
        for forbidden in ("request.user", "session", "get_gemi_client", "stripe", "send_mail", "urllib", "CustomerRadar",
                          "RadarMatch", "CompanySignal.objects", "Company.objects", "CompanyMonitoring"):
            self.assertNotIn(forbidden, code)

    def test_the_admin_is_read_only(self):
        from django.contrib import admin
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser("root", "root@example.com", "StrongPass123")
        for model in RADAR_TABLES:
            self.assertFalse(admin.site._registry[model].has_add_permission(request))
            self.assertFalse(admin.site._registry[model].has_change_permission(request))
