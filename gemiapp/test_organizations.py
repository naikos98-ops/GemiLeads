"""Tests for the organization foundation (C1, gemiapp.organizations).

Foundation only: no existing user-owned data changes owner, and nothing in the product reads these tables.
"""

import inspect
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import CreateModel
from django.test import RequestFactory, TestCase
from django.urls import get_resolver

from . import organizations as c1
from .models import (
    Company, CompanySignal, CustomerRadar, DigestDelivery, DigestPreference, Organization, OrganizationMember,
    OrganizationProfile, RadarMatch, UserCompanyLead, UserSubscription,
)
from .organizations import OrganizationError, add_organization_member, create_organization
from .services import eligible_radars
from .test_gemi_company_activities import entitled_user, radar_for

ROLES = ["owner", "admin", "sales_manager", "sales_user", "viewer"]
PROFILE = {
    "business": "Insurance broker", "products": "Vehicle insurance\nBusiness insurance",
    "target_customers": "Transport, delivery, restaurants", "location": "Attica",
}


def user(email="owner@example.com", **fields):
    return User.objects.create_user(email, email, "StrongPass123", **fields)


class SchemaTests(TestCase):
    def fields(self, model):
        return {field.name: field for field in model._meta.get_fields() if field.concrete}

    def test_organization_fields_carry_no_billing_company_or_tenant_data(self):
        self.assertEqual(set(self.fields(Organization)), {"id", "name", "created_at", "updated_at"})
        # Reverse relations only from organization-owned tables: members, profile, (C2) the ICP, (C3) Radars.
        from .models import OrganizationICP, OrganizationRadar

        for field in Organization._meta.get_fields():
            if field.is_relation:
                self.assertIn(field.related_model,
                              (OrganizationMember, OrganizationProfile, OrganizationICP, OrganizationRadar))
        self.assertFalse([f for f in Organization._meta.get_fields() if f.is_relation and f.related_model is Company])

    def test_membership_roles_constraints_and_deletion(self):
        fields = self.fields(OrganizationMember)
        self.assertEqual(set(fields), {"id", "organization", "user", "role", "created_at"})
        self.assertEqual([value for value, _ in OrganizationMember.ROLES], ROLES)
        self.assertEqual(fields["organization"].remote_field.on_delete.__name__, "CASCADE")
        self.assertEqual(fields["user"].remote_field.on_delete.__name__, "CASCADE")
        names = {c.name for c in OrganizationMember._meta.constraints}
        self.assertEqual(names, {"unique_organization_member", "organization_member_role_valid"})

    def test_profile_is_one_to_one_declared_context_without_contact_or_targeting_fields(self):
        fields = self.fields(OrganizationProfile)
        self.assertEqual(set(fields), {"id", "organization", "business", "products", "target_customers", "location",
                                       "created_at", "updated_at"})
        self.assertTrue(fields["organization"].one_to_one)
        self.assertEqual(fields["organization"].remote_field.on_delete.__name__, "CASCADE")
        for forbidden in ("phone", "email", "address", "birth", "note", "kad", "activity", "prefecture", "radar",
                          "score", "signal", "legal_type", "gemi", "website", "vat", "afm"):
            self.assertFalse([name for name in fields if forbidden in name], forbidden)
        for name in ("business", "products", "target_customers", "location"):
            self.assertTrue(fields[name].max_length)

    def test_existing_customer_models_have_no_organization_relation(self):
        for model in (CustomerRadar, UserCompanyLead, UserSubscription, DigestPreference, DigestDelivery, RadarMatch,
                      Company, CompanySignal):
            self.assertFalse([f for f in model._meta.get_fields() if "organization" in f.name], model.__name__)

    def test_the_migration_only_creates_the_three_tables(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        operations = loader.disk_migrations[("gemiapp", "0044_organization_foundation")].operations
        self.assertEqual({type(op) for op in operations}, {CreateModel})
        self.assertEqual({op.name for op in operations}, {"Organization", "OrganizationMember", "OrganizationProfile"})


class CreationTests(TestCase):
    def test_creation_makes_organization_owner_membership_and_profile_together(self):
        owner = user()
        created = create_organization(owner=owner, name="  Ασφαλιστική Αττικής  ", **PROFILE)
        self.assertEqual(created.organization.name, "Ασφαλιστική Αττικής")
        self.assertIsNotNone(created.organization.created_at)
        self.assertEqual((created.owner_membership.user, created.owner_membership.role), (owner, "owner"))
        self.assertEqual(created.profile.organization, created.organization)
        self.assertEqual({k: getattr(created.profile, k) for k in PROFILE}, PROFILE)
        self.assertEqual((Organization.objects.count(), OrganizationMember.objects.count(),
                          OrganizationProfile.objects.count()), (1, 1, 1))
        self.assertTrue(created.organization.members.filter(role="owner").exists())

    def test_a_profile_is_created_even_when_nothing_is_declared(self):
        created = create_organization(owner=user(), name="Org")
        self.assertEqual([getattr(created.profile, k) for k in PROFILE], ["", "", "", ""])

    def test_a_failure_after_the_first_write_leaves_nothing_behind(self):
        owner = user()
        with patch.object(OrganizationProfile.objects, "create", side_effect=IntegrityError("boom")):
            with self.assertRaises(IntegrityError):
                create_organization(owner=owner, name="Org", **PROFILE)
        self.assertEqual((Organization.objects.count(), OrganizationMember.objects.count(),
                          OrganizationProfile.objects.count()), (0, 0, 0))

    def test_invalid_requests_are_refused_before_any_write(self):
        owner = user()
        inactive = user("inactive@example.com", is_active=False)
        for kwargs in (
            {"owner": owner, "name": ""}, {"owner": owner, "name": "   "}, {"owner": owner, "name": "x" * 201},
            {"owner": None, "name": "Org"}, {"owner": User(username="unsaved"), "name": "Org"},
            {"owner": inactive, "name": "Org"}, {"owner": owner, "name": "Org", "business": "x" * 201},
            {"owner": owner, "name": "Org", "products": "x" * 1001}, {"owner": owner, "name": "Org", "website": "x"},
            {"owner": owner, "name": "Org", "gemi_number": "123"},
        ):
            with self.subTest(kwargs={k: (v[:20] if isinstance(v, str) else v) for k, v in kwargs.items()}):
                with self.assertRaises(OrganizationError):
                    create_organization(**kwargs)
        self.assertEqual(Organization.objects.count(), 0)

    def test_the_database_refuses_a_blank_name_and_a_second_profile(self):
        created = create_organization(owner=user(), name="Org")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Organization.objects.create(name="")
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationProfile.objects.create(organization=created.organization)

    def test_deleting_an_organization_removes_only_its_memberships_and_profile(self):
        owner = entitled_user()
        radar = radar_for(owner, "r")
        created = create_organization(owner=owner, name="Org")
        created.organization.delete()
        self.assertEqual((OrganizationMember.objects.count(), OrganizationProfile.objects.count()), (0, 0))
        self.assertTrue(User.objects.filter(pk=owner.pk).exists())
        self.assertTrue(CustomerRadar.objects.filter(pk=radar.pk).exists())
        self.assertTrue(UserSubscription.objects.filter(user=owner).exists())


class MembershipTests(TestCase):
    def setUp(self):
        self.owner = user()
        self.organization = create_organization(owner=self.owner, name="Org").organization

    def test_one_membership_per_organization_and_user(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationMember.objects.create(organization=self.organization, user=self.owner, role="admin")
        with self.assertRaises(OrganizationError):
            add_organization_member(self.organization, self.owner, "viewer")

    def test_role_values_are_enforced_by_the_database(self):
        other = user("other@example.com")
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrganizationMember.objects.create(organization=self.organization, user=other, role="superuser")
        with self.assertRaises(ValidationError):
            OrganizationMember(organization=self.organization, user=other, role="god").full_clean()

    def test_a_user_may_belong_to_several_organizations_and_an_organization_may_hold_several_users(self):
        second = create_organization(owner=self.owner, name="Second").organization
        for index, role in enumerate(ROLES[1:]):
            add_organization_member(self.organization, user(f"m{index}@example.com"), role)
        self.assertEqual(OrganizationMember.objects.filter(user=self.owner).count(), 2)
        self.assertEqual(self.organization.members.count(), 5)
        self.assertEqual(second.members.get().role, "owner")

    def test_the_internal_primitive_validates(self):
        for args in ((self.organization, user("a@example.com"), "boss"), (Organization(name="x"), user("b@example.com"), "admin"),
                     (self.organization, user("c@example.com", is_active=False), "admin")):
            with self.assertRaises(OrganizationError):
                add_organization_member(*args)


class G5SafetyTests(TestCase):
    """Multi-member organizations are blocked by G5: nothing in the product can add or use members."""

    def test_no_url_view_middleware_template_or_task_uses_organizations(self):
        from django.conf import settings

        for path in ("gemiapp/urls.py", "gemiapp/views.py", "config/urls.py", "gemiapp/tasks.py", "gemiapp/apps.py",
                     "gemiapp/billing.py", "gemiapp/services.py", "gemiapp/context_processors.py"):
            try:
                source = open(path, encoding="utf-8").read()
            except FileNotFoundError:
                continue
            self.assertNotIn("organization", source.lower().replace("\"organization\"", ""), path)
        self.assertFalse([m for m in settings.MIDDLEWARE if "organization" in m.lower()])
        for pattern in get_resolver().reverse_dict:
            self.assertNotIn("organization", str(pattern).lower())

    def test_the_member_primitive_is_wired_to_nothing(self):
        import pathlib

        callers = [
            str(path) for path in pathlib.Path("gemiapp").rglob("*.py")
            if "add_organization_member" in path.read_text(encoding="utf-8")
            and path.name not in ("organizations.py", "test_organizations.py")
        ]
        self.assertEqual(callers, [])
        self.assertIn("INTERNAL ONLY", inspect.getdoc(add_organization_member))

    def test_the_admin_can_neither_add_organizations_nor_members(self):
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser("root", "root@example.com", "StrongPass123")
        for model in (Organization, OrganizationMember, OrganizationProfile):
            model_admin = admin.site._registry[model]
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_change_permission(request))

    def test_no_organization_is_created_for_existing_users(self):
        entitled_user()
        user("plain@example.com")
        self.assertEqual((Organization.objects.count(), OrganizationMember.objects.count()), (0, 0))


class CompatibilityTests(TestCase):
    """Existing user-owned flows are untouched by the presence of organizations."""

    def test_user_owned_radars_entitlements_and_subscriptions_behave_identically(self):
        member = entitled_user()
        radar = radar_for(member, "r", prefectures=["ΑΤΤΙΚΗΣ"])
        before = (
            [r.pk for r in eligible_radars()], member.subscription.has_entitlement,
            list(UserSubscription.objects.values()), list(CustomerRadar.objects.values()),
            DigestPreference.objects.filter(user=member).exists(),
        )
        create_organization(owner=member, name="Org", **PROFILE)
        member.refresh_from_db()
        after = (
            [r.pk for r in eligible_radars()], member.subscription.has_entitlement,
            list(UserSubscription.objects.values()), list(CustomerRadar.objects.values()),
            DigestPreference.objects.filter(user=member).exists(),
        )
        self.assertEqual(after, before)
        self.assertEqual(CustomerRadar.objects.get(pk=radar.pk).user, member)

    def test_the_service_calls_no_network_billing_or_email(self):
        source = inspect.getsource(c1).split('"""', 2)[2]
        for forbidden in ("stripe", "requests", "urllib", "send_mail", "get_gemi_client", "billing", "Company\""):
            self.assertNotIn(forbidden, source)
