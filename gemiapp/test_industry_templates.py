"""Tests for the industry template foundation (C4, gemiapp.industry_templates).

Exact KAD identities only: no hierarchy, prefix, description or cross-version inference. Canonical references are
fixtures; no template is seeded anywhere.
"""

import dataclasses
import inspect
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from . import industry_templates as c4
from .company_contact import extract_company_contact_phones
from .industry_templates import (
    IndustryTemplateError,
    KadIdentity,
    create_industry_template,
    get_industry_template_definition,
    replace_industry_template_kads,
    set_industry_template_active,
    template_kad_proposal,
)
from .ingestion.monitoring import radar_match_evidence
from .ingestion.refresh import RefreshPolicy, build_company_refresh_plan
from .models import (
    Company, CompanyMonitoring, CompanySignal, CustomerRadar, GemiKad, IndustryTemplate, IndustryTemplateKad,
    OrganizationICP, OrganizationICPKad, OrganizationRadar, OrganizationRadarKad, RadarMatch, UserCompanyLead,
    UserSubscription,
)
from .organization_icp import ICPCriteria, KadCriterion, create_organization_icp, get_organization_icp_criteria
from .organization_radars import RadarDefinition, create_organization_radar, get_organization_radar_definition
from .organizations import create_organization
from .services import company_matches_radar, eligible_radars
from .test_gemi_company_activities import entitled_user, radar_for
from .test_organization_icp import ref



class Kads:
    def __init__(self):
        self.restaurant_2026 = ref(GemiKad, "56101100", kad_version="kad_2026", description="ΕΣΤΙΑΤΟΡΙΑ")
        self.restaurant_2008 = ref(GemiKad, "56101100", kad_version="kad_2008", description="ΕΣΤΙΑΤΟΡΙΑ (2008)")
        self.cafe_2026 = ref(GemiKad, "56301000", kad_version="kad_2026", description="ΚΑΦΕΤΕΡΙΕΣ")
        self.prefix_sibling = ref(GemiKad, "56101200", kad_version="kad_2026", description="ΕΣΤΙΑΤΟΡΙΑ ΤΑΧΕΙΑΣ")


class TemplateTestCase(TestCase):
    def setUp(self):
        self.k = Kads()

    def hospitality(self, **overrides):
        values = dict(slug="hospitality", name="Εστίαση", description="Εστιατόρια και καφέ",
                      kads=(self.k.restaurant_2026, self.k.restaurant_2008, self.k.cafe_2026))
        values.update(overrides)
        return create_industry_template(**values)


# --- audit, schema -------------------------------------------------------------------------------

class CapabilityAuditTests(TestCase):
    def test_no_code_infers_hierarchy_prefixes_or_description_similarity(self):
        code = inspect.getsource(c4).split('"""', 2)[2]
        for forbidden in ("startswith", "[:2]", "[:4]", "[:6]", "parent", "level", "difflib", "similar",
                          "kad.description", "kad__description", "crosswalk", "normalize_kad_search", "icontains"):
            self.assertNotIn(forbidden, code)

    def test_the_gemi_kad_reference_has_no_hierarchy_fields(self):
        names = {f.name for f in GemiKad._meta.get_fields()}
        for missing in ("parent", "parent_code", "level", "group", "section", "division"):
            self.assertNotIn(missing, names)

    def test_no_template_is_seeded(self):
        self.assertEqual((IndustryTemplate.objects.count(), IndustryTemplateKad.objects.count()), (0, 0))
        loader = MigrationLoader(None, ignore_no_migrations=True)
        operations = loader.disk_migrations[("gemiapp", "0047_industry_template")].operations
        self.assertEqual({type(op).__name__ for op in operations} - {"CreateModel", "AddConstraint"}, set())


class SchemaTests(TestCase):
    def test_templates_are_platform_owned_and_carry_no_scoring_or_personal_fields(self):
        for model in (IndustryTemplate, IndustryTemplateKad):
            names = [f.name for f in model._meta.get_fields() if f.concrete]
            for field in model._meta.get_fields():
                if field.concrete and field.is_relation:
                    self.assertIn(field.related_model, (IndustryTemplate, GemiKad), f"{model.__name__}.{field.name}")
            for forbidden in ("organization", "user", "subscription", "radar", "score", "weight", "priority", "phone",
                              "email", "person", "group", "parent", "level"):
                self.assertFalse([n for n in names if forbidden in n], f"{model.__name__}: {forbidden}")
        self.assertEqual({f.name for f in IndustryTemplate._meta.get_fields() if f.concrete},
                         {"id", "slug", "name", "description", "active", "created_at", "updated_at"})

    def test_no_dynamic_link_from_radar_icp_or_legacy_models(self):
        for model in (OrganizationRadar, OrganizationICP, CustomerRadar, UserSubscription, Company):
            self.assertFalse([f for f in model._meta.get_fields()
                              if getattr(f, "related_model", None) in (IndustryTemplate, IndustryTemplateKad)],
                             model.__name__)


# --- root ------------------------------------------------------------------------------------------

class RootTests(TemplateTestCase):
    def test_new_templates_are_inactive_and_may_start_empty(self):
        template = create_industry_template(slug="draft", name="Πρόχειρο")
        self.assertFalse(template.active)
        self.assertEqual(template.kads.count(), 0)

    def test_slug_is_validated_unique_and_stable(self):
        self.hospitality()
        for bad in ("", "Hospitality", "hospitality!", "-hospitality", "hosp--itality", "hosp itality", "x" * 65, None):
            with self.subTest(slug=bad), self.assertRaises(IndustryTemplateError):
                create_industry_template(slug=bad, name="x")
        with self.assertRaisesMessage(IndustryTemplateError, "already exists"):
            create_industry_template(slug="hospitality", name="Άλλο")
        with self.assertRaises(IntegrityError), transaction.atomic():
            IndustryTemplate.objects.create(slug="hospitality", name="dup")
        template = IndustryTemplate.objects.get(slug="hospitality")
        replace_industry_template_kads(template, (self.k.cafe_2026,))
        self.assertEqual(IndustryTemplate.objects.get(pk=template.pk).slug, "hospitality")

    def test_name_is_required_and_bounded(self):
        for bad in ("", "   ", "x" * 121, None):
            with self.subTest(name=bad), self.assertRaises(IndustryTemplateError):
                create_industry_template(slug="t", name=bad)
        with self.assertRaises(IndustryTemplateError):
            create_industry_template(slug="t", name="ok", description="x" * 501)
        with self.assertRaises(IntegrityError), transaction.atomic():
            IndustryTemplate.objects.create(slug="t", name="")

    def test_an_active_template_needs_a_kad(self):
        with self.assertRaises(IndustryTemplateError):
            create_industry_template(slug="t", name="t", active=True)
        draft = create_industry_template(slug="draft", name="d")
        with self.assertRaises(IndustryTemplateError):
            set_industry_template_active(draft, True)
        active = self.hospitality(active=True)
        with self.assertRaises(IndustryTemplateError):
            replace_industry_template_kads(active, ())
        self.assertEqual(active.kads.count(), 3)
        set_industry_template_active(active, False)
        replace_industry_template_kads(active, ())
        self.assertEqual(active.kads.count(), 0)


# --- KAD mapping -----------------------------------------------------------------------------------

class KadMappingTests(TemplateTestCase):
    def test_exact_code_and_version_with_2008_and_2026_distinct(self):
        template = self.hospitality()
        self.assertEqual(template_kad_proposal(template), (
            KadIdentity("56101100", "kad_2008"), KadIdentity("56101100", "kad_2026"), KadIdentity("56301000", "kad_2026"),
        ))
        only_2026 = create_industry_template(slug="only-2026", name="x", kads=(self.k.restaurant_2026,))
        self.assertEqual(template_kad_proposal(only_2026), (KadIdentity("56101100", "kad_2026"),))  # no 2008 added

    def test_prefix_siblings_are_never_implied(self):
        template = create_industry_template(slug="t", name="t", kads=(self.k.restaurant_2026,))
        self.assertNotIn(KadIdentity("56101200", "kad_2026"), template_kad_proposal(template))

    def test_only_saved_canonical_kads_without_duplicates(self):
        for bad in (("56101100",), (GemiKad(source_id="1", kad_version="kad_2026"),),
                    (self.k.cafe_2026, self.k.cafe_2026), (object(),)):
            with self.subTest(bad=bad), self.assertRaises(IndustryTemplateError):
                create_industry_template(slug="t", name="t", kads=bad)
        template = create_industry_template(slug="t", name="t", kads=(self.k.cafe_2026,))
        with self.assertRaises(IntegrityError), transaction.atomic():
            IndustryTemplateKad.objects.create(template=template, kad=self.k.cafe_2026)

    def test_retired_kads_cannot_be_added_but_existing_mappings_survive(self):
        retired = ref(GemiKad, "11111111", kad_version="kad_2008", present=False)
        with self.assertRaisesMessage(IndustryTemplateError, "retired"):
            create_industry_template(slug="t", name="t", kads=(retired,))
        template = self.hospitality()
        with self.assertRaisesMessage(IndustryTemplateError, "retired"):
            replace_industry_template_kads(template, (self.k.cafe_2026, retired))
        GemiKad.objects.filter(pk=self.k.restaurant_2008.pk).update(is_present=False)  # GEMI retires it later
        self.k.restaurant_2008.refresh_from_db()
        self.assertIn(KadIdentity("56101100", "kad_2008"), template_kad_proposal(template))
        replace_industry_template_kads(template, (self.k.restaurant_2008, self.k.cafe_2026))  # kept, not re-added
        self.assertEqual(len(template_kad_proposal(template)), 2)

    def test_a_referenced_kad_cannot_be_deleted(self):
        from django.db.models.deletion import ProtectedError

        self.hospitality()
        with self.assertRaises(ProtectedError):
            self.k.cafe_2026.delete()


# --- services --------------------------------------------------------------------------------------

class ServiceTests(TemplateTestCase):
    def test_a_failing_create_writes_nothing(self):
        with patch.object(IndustryTemplateKad.objects, "bulk_create", side_effect=IntegrityError("forced")):
            with self.assertRaises(IntegrityError):
                self.hospitality()
        self.assertEqual((IndustryTemplate.objects.count(), IndustryTemplateKad.objects.count()), (0, 0))

    def test_replace_succeeds_and_a_failed_replace_preserves_the_original(self):
        template = self.hospitality(active=True)
        original = get_industry_template_definition(template)
        replace_industry_template_kads(template, (self.k.cafe_2026,))
        self.assertEqual(template_kad_proposal(template), (KadIdentity("56301000", "kad_2026"),))
        replace_industry_template_kads(template, (self.k.restaurant_2026, self.k.restaurant_2008, self.k.cafe_2026))
        self.assertEqual(get_industry_template_definition(template).kads, original.kads)
        for bad in ((self.k.cafe_2026, self.k.cafe_2026), ("x",), ()):
            with self.subTest(bad=bad), self.assertRaises(IndustryTemplateError):
                replace_industry_template_kads(template, bad)
        with patch.object(IndustryTemplateKad.objects, "bulk_create", side_effect=IntegrityError("forced")):
            with self.assertRaises(IntegrityError):
                replace_industry_template_kads(template, (self.k.cafe_2026,))
        self.assertEqual(get_industry_template_definition(template), original)

    def test_activate_and_deactivate(self):
        template = self.hospitality()
        set_industry_template_active(template, True)
        self.assertTrue(get_industry_template_definition(template).active)
        set_industry_template_active(template, False)
        self.assertFalse(get_industry_template_definition(template).active)
        with self.assertRaises(IndustryTemplateError):
            set_industry_template_active(template, "yes")
        for bad in (None, IndustryTemplate(slug="unsaved", name="x"), GemiKad()):
            with self.subTest(bad=bad), self.assertRaises(IndustryTemplateError):
                set_industry_template_active(bad, False)


class DefinitionReaderTests(TemplateTestCase):
    def test_the_definition_is_deterministic_immutable_and_uses_exact_identity(self):
        template = self.hospitality()
        first = get_industry_template_definition(template)
        self.assertEqual(first, get_industry_template_definition(template))
        self.assertEqual((first.template_id, first.slug, first.name, first.active), (template.pk, "hospitality", "Εστίαση", False))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.slug = "other"
        self.assertEqual(first.kads, tuple(sorted(first.kads)))

    def test_description_wording_never_changes_identity(self):
        template = self.hospitality()
        before = template_kad_proposal(template)
        GemiKad.objects.filter(pk=self.k.restaurant_2026.pk).update(description="ΕΝΤΕΛΩΣ ΑΛΛΗ ΔΙΑΤΥΠΩΣΗ")
        self.assertEqual(template_kad_proposal(template), before)

    def test_reading_performs_no_write(self):
        template = self.hospitality()
        with CaptureQueriesContext(connection) as queries:
            get_industry_template_definition(template)
            template_kad_proposal(template)
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries.captured_queries))


# --- side effects, parity, regressions --------------------------------------------------------------

class NoSideEffectTests(TemplateTestCase):
    def test_templates_never_touch_icp_radars_matching_monitoring_or_billing(self):
        owner = User.objects.create_user("owner@example.com", "owner@example.com", "StrongPass123")
        org = create_organization(owner=owner, name="Org").organization
        create_organization_icp(org, ICPCriteria(kads=(KadCriterion(self.k.cafe_2026),)))
        radar = create_organization_radar(org, RadarDefinition(name="r", kads=(self.k.cafe_2026,)))
        member = entitled_user("legacy@example.com")
        legacy = radar_for(member, "legacy", prefectures=["ΑΤΤΙΚΗΣ"])
        company = Company.objects.create(gemi_number="300", name="Χ", incorporation_date=date(2026, 9, 5), prefecture="ΑΤΤΙΚΗΣ")
        run_at = datetime(2026, 9, 16, 9, tzinfo=dt_timezone.utc)
        CompanyMonitoring.objects.create(company=company, state="active", priority="high", primary_reason="active_radar_match",
                                         policy_version=1, monitored_since=run_at - timedelta(days=3),
                                         next_check_at=run_at - timedelta(hours=1))
        policy = RefreshPolicy(max_requests_per_run=5, max_pages_per_query=2, max_direct_details_per_run=2, page_size=2)

        def world():
            return (
                get_organization_icp_criteria(org), get_organization_radar_definition(org, radar),
                list(OrganizationICPKad.objects.values()), list(OrganizationRadarKad.objects.values()),
                list(CustomerRadar.objects.values()), [r.pk for r in eligible_radars()],
                company_matches_radar(Company.objects.get(pk=company.pk), CustomerRadar.objects.get(pk=legacy.pk)),
                {pk: e.source_ids for pk, e in radar_match_evidence().items()},
                build_company_refresh_plan(run_at=run_at, policy=policy).summary(),
                list(CompanyMonitoring.objects.values()), RadarMatch.objects.count(), UserCompanyLead.objects.count(),
                CompanySignal.objects.count(), list(UserSubscription.objects.values()),
                UserSubscription.objects.get(user=member).has_entitlement,
            )

        before = world()
        with patch("gemiapp.ingestion.refresh.get_gemi_client") as client:
            template = self.hospitality()
            set_industry_template_active(template, True)
            replace_industry_template_kads(template, (self.k.restaurant_2026,))
            template_kad_proposal(template)
        client.assert_not_called()
        self.assertEqual(world(), before)

    def test_nothing_in_the_product_uses_templates_and_the_module_calls_nothing(self):
        from django.conf import settings

        for path in ("gemiapp/urls.py", "gemiapp/views.py", "config/urls.py", "gemiapp/tasks.py", "gemiapp/apps.py",
                     "gemiapp/services.py", "gemiapp/billing.py", "gemiapp/forms.py", "gemiapp/organization_icp.py",
                     "gemiapp/organization_radars.py", "gemiapp/ingestion/monitoring.py", "gemiapp/ingestion/refresh.py",
                     "gemiapp/company_contact.py"):
            source = open(path, encoding="utf-8").read()
            self.assertNotIn("industry_template", source.lower(), path)
            self.assertNotIn("IndustryTemplate", source, path)
        self.assertFalse([m for m in settings.MIDDLEWARE if "template" in m.lower() and "industry" in m.lower()])
        code = inspect.getsource(c4).split('"""', 2)[2]
        for forbidden in ("request.user", "session", "get_gemi_client", "stripe", "send_mail", "urllib", "requests",
                          "Company.objects", "OrganizationRadar", "OrganizationICP", "CustomerRadar", "RadarMatch"):
            self.assertNotIn(forbidden, code)

    def test_the_admin_is_read_only(self):
        from django.contrib import admin
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser("root", "root@example.com", "StrongPass123")
        for model in (IndustryTemplate, IndustryTemplateKad):
            self.assertFalse(admin.site._registry[model].has_add_permission(request))
            self.assertFalse(admin.site._registry[model].has_change_permission(request))
        self.hospitality()
        template_admin = admin.site._registry[IndustryTemplate]
        self.assertEqual(template_admin.kad_count(template_admin.get_queryset(request).get()), 3)


class PhoneRegressionTests(TestCase):
    def test_the_deployed_phone_bridge_is_unchanged_by_c4(self):
        self.assertEqual([p.display for p in extract_company_contact_phones({"phone": "2109990001", "persons": [
            {"phone": "6999990002"}]})], ["2109990001"])
        company = Company(gemi_number="1", raw_data={"phone": "+30 2109990001"})
        self.assertEqual([p.tel_uri for p in company.gemi_phones], ["tel:+302109990001"])
