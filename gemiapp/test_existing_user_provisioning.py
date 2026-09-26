import ast
import inspect
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import existing_user_provisioning as provisioning
from .existing_user_provisioning import provision_existing_user_organizations
from .legacy_radar_migration import migrate_legacy_radars
from .models import (
    CompanySignal, CustomerRadar, LegacyRadarMigrationMap, Opportunity, Organization, OrganizationMember,
    OrganizationProfile, OrganizationRadar, UserCompanyLead, UserSubscription,
)
from .organization_entitlement import resolve_organization_entitlement
from .organizations import add_organization_member, create_organization

WRITE_SQL = ("INSERT", "UPDATE", "DELETE")


def tenancy_counts():
    return (Organization.objects.count(), OrganizationMember.objects.count(), OrganizationProfile.objects.count())


class ProvisioningTestCase(TestCase):
    def setUp(self):
        self.now = timezone.now()

    def user(self, email, **fields):
        user = User.objects.create_user(email, email, "password")
        for name, value in fields.items():
            setattr(user, name, value)
        if fields:
            user.save()
        return user

    def radar(self, user, **fields):
        values = dict(name="Legacy", only_active=False, monitor_from=self.now - timedelta(days=1))
        values.update(fields)
        return CustomerRadar.objects.create(user=user, **values)

    def paid(self, user, tier="pro"):
        UserSubscription.objects.filter(user=user).update(
            tier=tier, status="active", stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
        )
        return user

    def provision(self, **options):
        options.setdefault("limit", 100)
        return provision_existing_user_organizations(**options)

    def memberships(self, user):
        return list(OrganizationMember.objects.filter(user=user).select_related("organization"))


class ResolutionTests(ProvisioningTestCase):
    def test_zero_organizations_creates_one_organization_with_an_owner_membership(self):
        owner = self.user("anna@example.com", first_name="Anna", last_name="Papa")
        self.radar(owner)
        report = self.provision()
        self.assertEqual((report.counters["eligible"], report.counters["provisioned"]), (1, 1))
        [membership] = self.memberships(owner)
        self.assertEqual(membership.role, OrganizationMember.OWNER)
        self.assertEqual(membership.organization.name, "Anna Papa")  # the existing operator naming rule
        self.assertTrue(OrganizationProfile.objects.filter(organization=membership.organization).exists())
        self.assertEqual(tenancy_counts(), (1, 1, 1))

    def test_naming_falls_back_to_email_like_the_existing_provisioning_command(self):
        owner = self.user("nameless@example.com")
        self.radar(owner)
        self.provision()
        self.assertEqual(self.memberships(owner)[0].organization.name, "nameless@example.com")

    def test_exactly_one_owned_organization_is_already_provisioned_including_norva(self):
        owner = self.user("norva@example.com")
        norva = create_organization(owner=owner, name="NORVA").organization
        self.radar(owner)
        report = self.provision()
        self.assertEqual((report.counters["already_provisioned"], report.counters["provisioned"]), (1, 0))
        self.assertEqual(Organization.objects.get(), norva)
        self.assertEqual(tenancy_counts(), (1, 1, 1))

    def test_a_membership_only_organization_of_any_role_is_already_provisioned(self):
        host = self.user("host@example.com")
        organization = create_organization(owner=host, name="Host").organization
        member = self.user("viewer@example.com")
        add_organization_member(organization, member, OrganizationMember.VIEWER)
        self.radar(member)
        report = self.provision()
        self.assertEqual(report.counters["already_provisioned"], 1)
        self.assertEqual([m.organization for m in self.memberships(member)], [organization])
        self.assertEqual(Organization.objects.count(), 1)

    def test_several_organizations_are_ambiguous_and_untouched(self):
        user = self.user("two@example.com")
        create_organization(owner=user, name="First")
        other = create_organization(owner=self.user("other@example.com"), name="Second").organization
        add_organization_member(other, user, OrganizationMember.VIEWER)
        self.radar(user)
        before = tenancy_counts()
        report = self.provision()
        self.assertEqual((report.counters["ambiguous_organization"], report.counters["legacy_radars_still_blocked"]),
                         (1, 1))
        self.assertEqual(tenancy_counts(), before)

    def test_a_second_run_is_idempotent(self):
        owner = self.user("again@example.com")
        self.radar(owner)
        self.assertEqual(self.provision().counters["provisioned"], 1)
        second = self.provision()
        self.assertEqual((second.counters["provisioned"], second.counters["already_provisioned"]), (0, 1))
        self.assertEqual(tenancy_counts(), (1, 1, 1))

    def test_two_owners_get_separate_organizations(self):
        first, second = self.user("a@example.com"), self.user("b@example.com")
        self.radar(first)
        self.radar(second)
        report = self.provision()
        self.assertEqual(report.counters["provisioned"], 2)
        self.assertNotEqual(self.memberships(first)[0].organization_id, self.memberships(second)[0].organization_id)


class ScopeTests(ProvisioningTestCase):
    def test_only_live_legacy_radar_owners_are_in_scope(self):
        owner = self.user("owner@example.com")
        self.radar(owner)
        self.user("free-without-radar@example.com")
        self.paid(self.user("paid-without-radar@example.com"))
        deleted_only = self.user("deleted-only@example.com")
        self.radar(deleted_only, deleted_at=self.now)
        report = self.provision()
        self.assertEqual((report.counters["users_examined"], report.counters["provisioned"]), (1, 1))
        self.assertEqual(report.counters["out_of_scope_active_users_without_organization"], 3)
        self.assertEqual(OrganizationMember.objects.get().user, owner)

    def test_user_id_scopes_the_run(self):
        first, second = self.user("a@example.com"), self.user("b@example.com")
        self.radar(first)
        self.radar(second)
        report = self.provision(user_id=second.pk)
        self.assertEqual((report.counters["users_examined"], report.counters["provisioned"]), (1, 1))
        self.assertEqual(self.memberships(first), [])
        self.assertEqual(len(self.memberships(second)), 1)

    def test_user_id_never_widens_the_scope(self):
        outsider = self.user("no-radar@example.com")
        report = self.provision(user_id=outsider.pk)
        self.assertEqual((report.counters["skipped_out_of_scope"], report.counters["provisioned"]), (1, 0))
        self.assertEqual(tenancy_counts(), (0, 0, 0))

    def test_limit_bounds_the_users_examined(self):
        for index in range(3):
            self.radar(self.user(f"u{index}@example.com"))
        self.assertEqual(self.provision(limit=2).counters["users_examined"], 2)

    def test_several_radars_of_one_owner_share_one_organization_and_are_all_unblocked(self):
        owner = self.user("many@example.com")
        for index in range(3):
            self.radar(owner, name=f"Radar {index}")
        self.radar(owner, name="Gone", deleted_at=self.now)
        before = migrate_legacy_radars(dry_run=True, limit=100).counters
        self.assertEqual(before["missing_organization"], 3)
        report = self.provision()
        self.assertEqual((report.counters["provisioned"], report.counters["legacy_radars_unblocked"]), (1, 3))
        self.assertEqual(Organization.objects.count(), 1)
        after = migrate_legacy_radars(dry_run=True, limit=100).counters
        self.assertEqual((after["missing_organization"], after["eligible"]), (0, 3))
        self.assertEqual(OrganizationRadar.objects.count(), 0)  # still only a dry-run of the Radar migration

    def test_inactive_and_unverified_accounts_are_skipped(self):
        inactive = self.user("inactive@example.com", is_active=False)  # unverified signups are inactive too
        self.radar(inactive)
        report = self.provision()
        self.assertEqual((report.counters["skipped_inactive"], report.counters["legacy_radars_still_blocked"]),
                         (1, 1))
        self.assertEqual(tenancy_counts(), (0, 0, 0))

    def test_staff_and_superusers_are_skipped_unless_explicitly_included(self):
        staff = self.user("staff@example.com", is_staff=True)
        superuser = self.user("root@example.com", is_superuser=True)
        self.radar(staff)
        self.radar(superuser)
        report = self.provision()
        self.assertEqual((report.counters["skipped_staff"], report.counters["provisioned"]), (2, 0))
        report = self.provision(include_staff=True, user_id=staff.pk)
        self.assertEqual(report.counters["provisioned"], 1)
        self.assertEqual(self.memberships(superuser), [])

    def test_the_demo_seed_account_is_never_provisioned(self):
        demo = self.user("demo@gemileads.gr")
        self.radar(demo)
        report = self.provision(include_staff=True)
        self.assertEqual((report.counters["skipped_demo"], report.counters["provisioned"]), (1, 0))


class DryRunTests(ProvisioningTestCase):
    def test_dry_run_classifies_everything_and_writes_nothing(self):
        owner = self.paid(self.user("paid@example.com"))
        self.radar(owner)
        self.radar(owner, name="Second")
        UserCompanyLead.objects.create(user=owner, company=self._company())
        self.radar(self.user("free@example.com"))
        with CaptureQueriesContext(connection) as queries:
            report = self.provision(dry_run=True)
        self.assertFalse([q["sql"] for q in queries.captured_queries if q["sql"].lstrip().upper().startswith(WRITE_SQL)])
        counters = report.counters
        self.assertEqual((counters["eligible"], counters["provisioned"], counters["legacy_radars_unblocked"]),
                         (2, 0, 3))
        self.assertEqual((counters["eligible_entitled"], counters["eligible_not_entitled"],
                          counters["eligible_with_legacy_leads"]), (1, 1, 1))
        self.assertEqual(tenancy_counts(), (0, 0, 0))

    def _company(self):
        from .models import Company
        return Company.objects.create(gemi_number="123456789000", name="Co", incorporation_date=self.now.date())


class RaceAndFailureTests(ProvisioningTestCase):
    def test_a_membership_that_appears_after_selection_is_respected(self):
        user = self.user("race@example.com")
        self.radar(user)
        host = create_organization(owner=self.user("host@example.com"), name="Host").organization
        original = provisioning._owner_entitled
        raced = []

        def racing(candidate):
            if not raced:
                raced.append(True)
                add_organization_member(host, candidate, OrganizationMember.VIEWER)
            return original(candidate)

        with patch.object(provisioning, "_owner_entitled", side_effect=racing):
            report = self.provision()
        self.assertEqual((report.counters["race_skipped"], report.counters["provisioned"]), (1, 0))
        self.assertEqual(Organization.objects.count(), 1)
        self.assertEqual([m.organization for m in self.memberships(user)], [host])

    def test_organization_creation_failure_leaves_nothing(self):
        self.radar(self.user("fail@example.com"))
        with patch.object(Organization, "save", side_effect=IntegrityError("boom")):
            report = self.provision()
        self.assertEqual((report.counters["errors"], report.counters["provisioned"]), (1, 0))
        self.assertEqual(tenancy_counts(), (0, 0, 0))

    def test_owner_membership_failure_rolls_the_organization_back(self):
        self.radar(self.user("fail@example.com"))
        with patch.object(OrganizationMember.objects, "create", side_effect=IntegrityError("boom")):
            report = self.provision()
        self.assertEqual(report.counters["errors"], 1)
        self.assertEqual(tenancy_counts(), (0, 0, 0))

    def test_a_failed_verification_rolls_everything_back_and_the_next_user_continues(self):
        first, second = self.user("a@example.com"), self.user("b@example.com")
        self.radar(first)
        self.radar(second)
        real = provisioning.resolve_organization_entitlement
        calls = []

        def mismatch_once(organization):
            result = real(organization)
            calls.append(organization.pk)
            if len(calls) == 1:
                return result.__class__(result.organization_id, not result.entitled, result.reason,
                                        result.owner_user_id, result.effective_tier)
            return result

        with patch.object(provisioning, "resolve_organization_entitlement", side_effect=mismatch_once):
            report = self.provision()
        self.assertEqual((report.counters["errors"], report.counters["provisioned"]), (1, 1))
        self.assertEqual(self.memberships(first), [])
        self.assertEqual(tenancy_counts(), (1, 1, 1))


class BillingAndBoundaryTests(ProvisioningTestCase):
    def subscriptions(self):
        return list(UserSubscription.objects.order_by("pk").values())

    def test_subscriptions_billing_and_entitlement_are_unchanged_and_equivalent(self):
        free = self.user("free@example.com")
        paid = self.paid(self.user("paid@example.com"), tier="business")
        comp = self.user("comp@example.com")
        UserSubscription.objects.filter(user=comp).update(complimentary_tier="pro")
        for user in (free, paid, comp):
            self.radar(user)
        before = self.subscriptions()
        expected = {u.pk: UserSubscription.objects.get(user=u) for u in (free, paid, comp)}
        self.assertEqual(self.provision().counters["provisioned"], 3)
        self.assertEqual(self.subscriptions(), before)  # tier, status, Stripe ids, complimentary: every column
        for user_pk, subscription in expected.items():
            organization = OrganizationMember.objects.get(user_id=user_pk).organization
            entitlement = resolve_organization_entitlement(organization)
            self.assertEqual(entitlement.entitled, subscription.has_entitlement)
            if entitlement.entitled:
                self.assertEqual(entitlement.effective_tier, subscription.effective_tier)
        self.assertFalse(resolve_organization_entitlement(OrganizationMember.objects.get(user=free).organization)
                         .entitled)  # Free stays Free
        self.assertEqual(resolve_organization_entitlement(OrganizationMember.objects.get(user=paid).organization)
                         .effective_tier, "business")  # paid stays paid

    def test_no_radar_signal_opportunity_or_legacy_data_is_created_or_changed(self):
        owner = self.user("owner@example.com")
        self.radar(owner)
        legacy = list(CustomerRadar.objects.order_by("pk").values())
        self.provision()
        self.assertEqual(list(CustomerRadar.objects.order_by("pk").values()), legacy)
        self.assertEqual((OrganizationRadar.objects.count(), LegacyRadarMigrationMap.objects.count(),
                          CompanySignal.objects.count(), Opportunity.objects.count()), (0, 0, 0, 0))

    def test_the_module_touches_no_billing_radar_migration_g4_or_hydration_code(self):
        tree = ast.parse(inspect.getsource(provisioning))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        self.assertEqual(imported, {
            "__future__", "annotations", "dataclasses", "dataclass", "field", "django.contrib.auth.models", "User",
            "django.db", "transaction", "django.db.models", "Count", "Exists", "OuterRef", "Q", "models",
            "CustomerRadar", "OrganizationMember", "UserCompanyLead", "organization_entitlement",
            "resolve_organization_entitlement", "organizations", "create_organization", "default_organization_name",
        })
        self.assertFalse(settings.GEMI_DISCOVERY_PENDING_COMPANY_HYDRATION_ENABLED)

    def test_nothing_runs_it_automatically(self):
        root = Path(__file__).resolve().parent
        for name in ("apps.py", "tasks.py", "signals.py", "models.py", "views.py", "urls.py"):
            path = root / name
            if path.exists():
                self.assertNotIn("existing_user_provisioning", path.read_text(encoding="utf-8"), name)


class CommandTests(ProvisioningTestCase):
    def test_output_is_counters_only_and_dry_run_is_announced(self):
        owner = self.user("secret-person@example.com", first_name="Secret", last_name="Person")
        self.radar(owner, name="Secret criteria")
        out = StringIO()
        call_command("provision_existing_user_organizations", "--dry-run", "--limit", "10000", stdout=out)
        text = out.getvalue()
        self.assertIn("mode=dry-run scope=legacy_radar_owners", text)
        self.assertIn("legacy_radars_unblocked=1", text)
        self.assertIn("zero database writes", text)
        for secret in ("secret-person", "Secret", "Person", "criteria"):
            self.assertNotIn(secret, text)
        self.assertEqual(tenancy_counts(), (0, 0, 0))

    def test_invalid_options_are_refused(self):
        for args in (["--limit", "0"], ["--limit", "10001"], ["--user-id", "0"]):
            with self.assertRaises(CommandError):
                call_command("provision_existing_user_organizations", *args, stdout=StringIO())

    def test_the_single_user_command_keeps_the_same_default_name(self):
        user = self.user("single@example.com", first_name="Maria", last_name="K")
        call_command("provision_organization_for_user", str(user.pk), stdout=StringIO())
        self.assertEqual(self.memberships(user)[0].organization.name, "Maria K")
