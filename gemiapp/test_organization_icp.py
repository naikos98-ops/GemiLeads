"""Tests for the organization ICP (C2, gemiapp.organization_icp).

Canonical reference rows are created as fixtures; nothing depends on a live A5 sync. The ICP is configuration
only: no matching, scoring, lead, opportunity, monitoring or signal comes out of it.
"""

import inspect
from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase

from . import organization_icp as c2
from .company_signals import SIGNAL_TYPES
from .models import (
    Company, CompanyMonitoring, CompanySignal, CustomerRadar, GemiCompanyStatus, GemiKad, GemiLegalType,
    GemiMunicipality, GemiPrefecture, Organization, OrganizationICP, OrganizationICPKad, OrganizationICPLegalForm,
    OrganizationICPRegion, OrganizationICPSignalType, OrganizationICPStatus, OrganizationProfile, RadarMatch,
    UserCompanyLead, UserSubscription,
)
from .organization_icp import (
    EXCLUDE,
    INCLUDE,
    ICPCriteria,
    ICPError,
    KadCriterion,
    LegalFormCriterion,
    RegionCriterion,
    StatusCriterion,
    create_organization_icp,
    get_organization_icp_criteria,
    implemented_signal_types,
    is_icp_configured,
    replace_organization_icp,
)
from .organizations import create_organization
from .services import eligible_radars
from .test_gemi_company_activities import entitled_user, radar_for

NOW = datetime(2026, 9, 16, tzinfo=dt_timezone.utc)
ICP_TABLES = (OrganizationICP, OrganizationICPKad, OrganizationICPRegion, OrganizationICPLegalForm,
              OrganizationICPStatus, OrganizationICPSignalType)


def ref(model, source_id, description="x", present=True, **extra):
    return model.objects.create(source_id=source_id, description=description, first_seen_at=NOW, last_seen_at=NOW,
                                is_present=present, **extra)


class Refs:
    def __init__(self):
        self.kad_2026 = ref(GemiKad, "62010000", kad_version="kad_2026")
        self.kad_2008 = ref(GemiKad, "62010000", kad_version="kad_2008")
        self.kad_other = ref(GemiKad, "47191002", kad_version="kad_2026")
        self.attica = ref(GemiPrefecture, "5")
        self.thessaloniki = ref(GemiPrefecture, "61190")
        self.kifisia = ref(GemiMunicipality, "61190", source_prefecture_id="5")  # same source id as a prefecture
        self.ike = ref(GemiLegalType, "19")
        self.oe = ref(GemiLegalType, "3")
        self.active = ref(GemiCompanyStatus, "3", source_is_active=True)
        self.deleted = ref(GemiCompanyStatus, "8", source_is_active=False)


def organization(name="Org", email="owner@example.com"):
    owner = User.objects.create_user(email, email, "StrongPass123")
    return create_organization(owner=owner, name=name).organization


def fixture_criteria(r):
    return ICPCriteria(
        kads=(KadCriterion(r.kad_2026, INCLUDE), KadCriterion(r.kad_other, EXCLUDE)),
        regions=(RegionCriterion(r.kifisia, INCLUDE), RegionCriterion(r.thessaloniki, EXCLUDE)),
        legal_forms=(LegalFormCriterion(r.ike, INCLUDE),),
        statuses=(StatusCriterion(r.active, INCLUDE),),
        signal_types=("new_company", "kad_added"),
        minimum_age_months=0, maximum_age_months=24,
    )


def counts():
    return tuple(model.objects.count() for model in ICP_TABLES)


class ICPTestCase(TestCase):
    def setUp(self):
        self.refs = Refs()
        self.org = organization()


# --- schema and ownership ------------------------------------------------------------------------

class SchemaTests(TestCase):
    def test_every_icp_row_hangs_off_one_organization_and_never_a_user(self):
        icp_fields = {f.name: f for f in OrganizationICP._meta.get_fields() if f.concrete}
        self.assertTrue(icp_fields["organization"].one_to_one)
        self.assertEqual(icp_fields["organization"].remote_field.on_delete.__name__, "CASCADE")
        for model in ICP_TABLES:
            for field in model._meta.get_fields():
                if field.concrete and field.is_relation:
                    self.assertIsNot(field.related_model, User, f"{model.__name__}.{field.name}")
            if model is not OrganizationICP:
                icp = model._meta.get_field("icp")
                self.assertEqual((icp.related_model, icp.remote_field.on_delete.__name__), (OrganizationICP, "CASCADE"))

    def test_no_free_json_personal_or_scoring_fields(self):
        from django.db.models import JSONField

        for model in ICP_TABLES:
            names = [f.name for f in model._meta.get_fields() if f.concrete]
            self.assertFalse([f for f in model._meta.get_fields() if isinstance(f, JSONField)], model.__name__)
            for forbidden in ("name", "email", "phone", "person", "note", "score", "weight", "priority", "industry",
                              "description", "user", "radar"):
                self.assertFalse([n for n in names if forbidden in n], f"{model.__name__}: {forbidden}")

    def test_the_migration_is_additive_only(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        operations = loader.disk_migrations[("gemiapp", "0045_organization_icp")].operations
        self.assertEqual({type(op).__name__ for op in operations}, {"CreateModel", "AddConstraint"})

    def test_legacy_customer_models_are_not_linked(self):
        for model in (CustomerRadar, UserCompanyLead, UserSubscription, RadarMatch, Company, OrganizationProfile):
            self.assertFalse([f for f in model._meta.get_fields() if "icp" in f.name], model.__name__)


class OwnershipTests(ICPTestCase):
    def test_one_icp_per_organization_created_explicitly_and_once(self):
        self.assertIsNone(get_organization_icp_criteria(self.org))
        self.assertEqual(OrganizationICP.objects.count(), 0)  # C1 creation never invents an ICP
        icp = create_organization_icp(self.org)
        self.assertEqual(icp.organization, self.org)
        with self.assertRaises(ICPError):
            create_organization_icp(self.org)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationICP.objects.create(organization=self.org)

    def test_deleting_the_organization_removes_its_icp_and_criteria(self):
        create_organization_icp(self.org, fixture_criteria(self.refs))
        self.org.delete()
        self.assertEqual(counts(), (0, 0, 0, 0, 0, 0))
        self.assertTrue(GemiKad.objects.filter(pk=self.refs.kad_2026.pk).exists())

    def test_deleting_the_owner_user_does_not_delete_the_icp(self):
        create_organization_icp(self.org, fixture_criteria(self.refs))
        self.org.members.get().user.delete()
        self.assertTrue(Organization.objects.filter(pk=self.org.pk).exists())
        self.assertEqual(counts(), (1, 2, 2, 1, 1, 2))

    def test_services_require_an_explicit_saved_organization(self):
        for bad in (None, Organization(name="unsaved"), self.org.members.get().user, "1"):
            with self.subTest(bad=bad):
                for call in (lambda: create_organization_icp(bad), lambda: replace_organization_icp(bad, ICPCriteria()),
                             lambda: get_organization_icp_criteria(bad)):
                    with self.assertRaises(ICPError):
                        call()

    def test_replacing_one_organizations_icp_never_touches_another(self):
        other = organization("Other", "other@example.com")
        create_organization_icp(self.org, fixture_criteria(self.refs))
        create_organization_icp(other, ICPCriteria(kads=(KadCriterion(self.refs.kad_2026),)))
        replace_organization_icp(self.org, ICPCriteria())
        self.assertEqual(len(get_organization_icp_criteria(other).kads), 1)
        self.assertTrue(get_organization_icp_criteria(self.org).is_empty)
        with self.assertRaises(ICPError):
            replace_organization_icp(organization("No ICP", "none@example.com"), ICPCriteria())


# --- dimensions ----------------------------------------------------------------------------------

class FixtureTests(ICPTestCase):
    def test_the_representative_fixture_round_trips_exactly(self):
        criteria = fixture_criteria(self.refs)
        create_organization_icp(self.org, criteria)
        self.assertEqual(get_organization_icp_criteria(self.org), criteria)
        self.assertEqual(counts(), (1, 2, 2, 1, 1, 2))
        levels = dict(OrganizationICPRegion.objects.values_list("level", "polarity"))
        self.assertEqual(levels, {"municipality": INCLUDE, "prefecture": EXCLUDE})


class KadTests(ICPTestCase):
    def test_identity_is_code_and_version(self):
        criteria = ICPCriteria(kads=(KadCriterion(self.refs.kad_2026, INCLUDE), KadCriterion(self.refs.kad_2008, EXCLUDE)))
        create_organization_icp(self.org, criteria)
        stored = {(row.kad.source_id, row.kad.kad_version, row.polarity) for row in OrganizationICPKad.objects.all()}
        self.assertEqual(stored, {("62010000", "kad_2026", INCLUDE), ("62010000", "kad_2008", EXCLUDE)})

    def test_duplicates_and_contradictions_are_rejected(self):
        for kads, message in (
            ((KadCriterion(self.refs.kad_2026), KadCriterion(self.refs.kad_2026)), "duplicate"),
            ((KadCriterion(self.refs.kad_2026, INCLUDE), KadCriterion(self.refs.kad_2026, EXCLUDE)), "both included and excluded"),
        ):
            with self.subTest(message=message), self.assertRaisesMessage(ICPError, message):
                create_organization_icp(self.org, ICPCriteria(kads=kads))
        icp = create_organization_icp(self.org)
        OrganizationICPKad.objects.create(icp=icp, kad=self.refs.kad_2026, polarity=INCLUDE)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationICPKad.objects.create(icp=icp, kad=self.refs.kad_2026, polarity=EXCLUDE)

    def test_only_canonical_present_kads_are_accepted(self):
        retired = ref(GemiKad, "11111111", kad_version="kad_2008", present=False)
        for bad in (KadCriterion(retired), KadCriterion("62010000"), KadCriterion(GemiKad(source_id="1")),
                    KadCriterion(self.refs.kad_2026, "maybe")):
            with self.subTest(bad=bad), self.assertRaises(ICPError):
                create_organization_icp(self.org, ICPCriteria(kads=(bad,)))
        self.assertEqual(OrganizationICP.objects.count(), 0)


class RegionTests(ICPTestCase):
    def test_levels_never_collide_even_with_equal_source_ids(self):
        criteria = ICPCriteria(regions=(RegionCriterion(self.refs.thessaloniki, INCLUDE), RegionCriterion(self.refs.kifisia, EXCLUDE)))
        create_organization_icp(self.org, criteria)
        self.assertEqual(self.refs.thessaloniki.source_id, self.refs.kifisia.source_id)
        self.assertEqual(OrganizationICPRegion.objects.count(), 2)

    def test_a_prefecture_include_with_a_municipality_exclude_inside_it_is_legitimate(self):
        create_organization_icp(self.org, ICPCriteria(regions=(
            RegionCriterion(self.refs.attica, INCLUDE), RegionCriterion(self.refs.kifisia, EXCLUDE))))
        self.assertEqual(OrganizationICPRegion.objects.count(), 2)

    def test_duplicates_contradictions_and_wrong_types_are_rejected(self):
        for regions in (
            (RegionCriterion(self.refs.kifisia), RegionCriterion(self.refs.kifisia)),
            (RegionCriterion(self.refs.attica, INCLUDE), RegionCriterion(self.refs.attica, EXCLUDE)),
            (RegionCriterion(self.refs.ike),), (RegionCriterion("ΑΤΤΙΚΗΣ"),),
        ):
            with self.subTest(regions=regions), self.assertRaises(ICPError):
                create_organization_icp(self.org, ICPCriteria(regions=regions))

    def test_the_database_enforces_level_consistency_and_uniqueness(self):
        icp = create_organization_icp(self.org)
        for bad in ({"level": "prefecture", "municipality": self.refs.kifisia},
                    {"level": "municipality", "prefecture": self.refs.attica, "municipality": self.refs.kifisia},
                    {"level": "country", "prefecture": self.refs.attica}):
            with self.subTest(bad=bad), self.assertRaises(IntegrityError), transaction.atomic():
                OrganizationICPRegion.objects.create(icp=icp, polarity=INCLUDE, **bad)
        OrganizationICPRegion.objects.create(icp=icp, level="prefecture", prefecture=self.refs.attica, polarity=INCLUDE)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationICPRegion.objects.create(icp=icp, level="prefecture", prefecture=self.refs.attica, polarity=EXCLUDE)


class LegalFormAndStatusTests(ICPTestCase):
    def test_canonical_identity_and_contradiction_prevention(self):
        create_organization_icp(self.org, ICPCriteria(
            legal_forms=(LegalFormCriterion(self.refs.ike, INCLUDE), LegalFormCriterion(self.refs.oe, EXCLUDE)),
            statuses=(StatusCriterion(self.refs.active, INCLUDE), StatusCriterion(self.refs.deleted, EXCLUDE)),
        ))
        self.assertEqual(OrganizationICPLegalForm.objects.count(), 2)
        self.assertEqual(set(OrganizationICPStatus.objects.values_list("status__source_id", "polarity")),
                         {("3", INCLUDE), ("8", EXCLUDE)})
        for criteria in (
            ICPCriteria(legal_forms=(LegalFormCriterion(self.refs.ike, INCLUDE), LegalFormCriterion(self.refs.ike, EXCLUDE))),
            ICPCriteria(statuses=(StatusCriterion(self.refs.active, INCLUDE), StatusCriterion(self.refs.active, EXCLUDE))),
            ICPCriteria(legal_forms=(LegalFormCriterion("ΙΚΕ"),)),
            ICPCriteria(statuses=(StatusCriterion(True),)),
        ):
            with self.subTest(criteria=criteria), self.assertRaises(ICPError):
                replace_organization_icp(self.org, criteria)

    def test_status_never_uses_company_is_active(self):
        source = inspect.getsource(c2).split('"""', 2)[2]
        self.assertNotIn("is_active", source)
        self.assertFalse([f.name for f in OrganizationICPStatus._meta.get_fields() if "active" in f.name])


class AgeTests(ICPTestCase):
    def test_open_and_closed_ranges_including_zero(self):
        icp = create_organization_icp(self.org)
        for minimum, maximum in ((None, None), (None, 12), (6, None), (0, 0), (0, 36), (12, 12)):
            with self.subTest(minimum=minimum, maximum=maximum):
                replace_organization_icp(self.org, ICPCriteria(minimum_age_months=minimum, maximum_age_months=maximum))
                icp.refresh_from_db()
                self.assertEqual((icp.minimum_age_months, icp.maximum_age_months), (minimum, maximum))

    def test_negative_non_integer_and_inverted_ranges_are_rejected(self):
        create_organization_icp(self.org)
        for minimum, maximum in ((-1, None), (None, -3), (13, 12), (1.5, None), (True, None), ("6", None)):
            with self.subTest(minimum=minimum, maximum=maximum), self.assertRaises(ICPError):
                replace_organization_icp(self.org, ICPCriteria(minimum_age_months=minimum, maximum_age_months=maximum))
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationICP.objects.filter(organization=self.org).update(minimum_age_months=13, maximum_age_months=12)


class SignalTypeTests(ICPTestCase):
    def test_only_implemented_types_are_selectable(self):
        self.assertEqual(set(implemented_signal_types()), {
            "new_company", "status_changed", "kad_added", "kad_removed", "legal_form_changed", "location_changed",
        })
        create_organization_icp(self.org, ICPCriteria(signal_types=implemented_signal_types()))
        self.assertEqual(OrganizationICPSignalType.objects.count(), 6)
        for bad in (("capital_increase",), ("invented",), ("new_company", "new_company")):
            with self.subTest(bad=bad), self.assertRaises(ICPError):
                replace_organization_icp(self.org, ICPCriteria(signal_types=bad))
        self.assertIn("capital_increase", SIGNAL_TYPES)  # taxonomy exists, detector does not

    def test_the_database_rejects_duplicates_and_unknown_types(self):
        icp = create_organization_icp(self.org)
        OrganizationICPSignalType.objects.create(icp=icp, signal_type="kad_added")
        for bad in ("kad_added", "invented"):
            with self.subTest(bad=bad), self.assertRaises(IntegrityError), transaction.atomic():
                OrganizationICPSignalType.objects.create(icp=icp, signal_type=bad)


class DeferredDimensionTests(TestCase):
    def test_industry_groups_and_priorities_are_deliberately_not_stored(self):
        self.assertEqual(
            {f.name for f in ICPCriteria.__dataclass_fields__.values()},
            {"kads", "regions", "legal_forms", "statuses", "signal_types", "minimum_age_months", "maximum_age_months"},
        )
        docs = inspect.getdoc(c2)
        self.assertIn("Industry groups", docs)
        self.assertIn("Priorities", docs)


class ConfiguredStateTests(ICPTestCase):
    def test_an_empty_icp_is_unconfigured_never_universal(self):
        self.assertFalse(is_icp_configured(self.org))
        create_organization_icp(self.org)
        self.assertFalse(is_icp_configured(self.org))
        replace_organization_icp(self.org, ICPCriteria(maximum_age_months=12))
        self.assertTrue(is_icp_configured(self.org))
        replace_organization_icp(self.org, ICPCriteria(regions=(RegionCriterion(self.refs.attica, EXCLUDE),)))
        self.assertTrue(is_icp_configured(self.org))
        replace_organization_icp(self.org, ICPCriteria())
        self.assertFalse(is_icp_configured(self.org))


class AtomicUpdateTests(ICPTestCase):
    def test_a_multi_criterion_replacement_succeeds_as_a_whole(self):
        create_organization_icp(self.org, ICPCriteria(kads=(KadCriterion(self.refs.kad_2008),)))
        replace_organization_icp(self.org, fixture_criteria(self.refs))
        self.assertEqual(get_organization_icp_criteria(self.org), fixture_criteria(self.refs))

    def test_an_invalid_criterion_rejects_the_whole_update_before_writing(self):
        original = fixture_criteria(self.refs)
        create_organization_icp(self.org, original)
        broken = ICPCriteria(kads=(KadCriterion(self.refs.kad_2008),), statuses=(StatusCriterion(self.refs.active, INCLUDE),
                             StatusCriterion(self.refs.active, EXCLUDE)))
        with self.assertRaises(ICPError):
            replace_organization_icp(self.org, broken)
        self.assertEqual(get_organization_icp_criteria(self.org), original)

    def test_a_failure_during_the_write_rolls_everything_back(self):
        original = fixture_criteria(self.refs)
        create_organization_icp(self.org, original)
        with patch.object(OrganizationICPStatus.objects, "bulk_create", side_effect=IntegrityError("forced")):
            with self.assertRaises(IntegrityError):
                replace_organization_icp(self.org, ICPCriteria(kads=(KadCriterion(self.refs.kad_2008),)))
        self.assertEqual(get_organization_icp_criteria(self.org), original)
        with patch.object(OrganizationICPSignalType.objects, "bulk_create", side_effect=IntegrityError("forced")):
            with self.assertRaises(IntegrityError):
                create_organization_icp(organization("New", "new@example.com"), original)
        self.assertEqual(OrganizationICP.objects.count(), 1)


class NoMatchingTests(ICPTestCase):
    def test_creating_an_icp_produces_no_business_output(self):
        user = entitled_user("radar@example.com")
        radar = radar_for(user, "legacy", prefectures=["ΑΤΤΙΚΗΣ"])
        Company.objects.create(gemi_number="1", name="Χ", incorporation_date=NOW.date(), prefecture="ΑΤΤΙΚΗΣ")
        before = (RadarMatch.objects.count(), UserCompanyLead.objects.count(), CompanyMonitoring.objects.count(),
                  CompanySignal.objects.count(), [r.pk for r in eligible_radars()],
                  list(CustomerRadar.objects.values()), list(UserSubscription.objects.values()),
                  user.subscription.has_entitlement)
        create_organization_icp(self.org, fixture_criteria(self.refs))
        replace_organization_icp(self.org, ICPCriteria(signal_types=("new_company",)))
        user.refresh_from_db()
        after = (RadarMatch.objects.count(), UserCompanyLead.objects.count(), CompanyMonitoring.objects.count(),
                 CompanySignal.objects.count(), [r.pk for r in eligible_radars()],
                 list(CustomerRadar.objects.values()), list(UserSubscription.objects.values()),
                 user.subscription.has_entitlement)
        self.assertEqual(after, before)
        self.assertEqual(CustomerRadar.objects.get(pk=radar.pk).user, user)

    def test_no_url_view_task_middleware_matching_or_network_uses_the_icp(self):
        from django.conf import settings

        for path in ("gemiapp/urls.py", "gemiapp/views.py", "config/urls.py", "gemiapp/tasks.py", "gemiapp/apps.py",
                     "gemiapp/services.py", "gemiapp/billing.py", "gemiapp/ingestion/monitoring.py",
                     "gemiapp/ingestion/refresh.py", "gemiapp/company_timeline.py"):
            self.assertNotIn("icp", open(path, encoding="utf-8").read().lower(), path)
        self.assertFalse([m for m in settings.MIDDLEWARE if "organization" in m.lower() or "icp" in m.lower()])
        source = inspect.getsource(c2).split('"""', 2)[2]
        for forbidden in ("request.user", "session", "get_gemi_client", "stripe", "send_mail", "urllib",
                          "Company.objects", "OrganizationProfile", ".business", "target_customers"):
            self.assertNotIn(forbidden, source)

    def test_the_admin_is_read_only(self):
        from django.contrib import admin
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser("root", "root@example.com", "StrongPass123")
        for model in ICP_TABLES:
            self.assertFalse(admin.site._registry[model].has_add_permission(request))
            self.assertFalse(admin.site._registry[model].has_change_permission(request))
