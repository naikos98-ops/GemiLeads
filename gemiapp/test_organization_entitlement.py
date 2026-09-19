"""Tests for the organization compatibility layer: user-owned billing -> organization entitlement.

An organization's paid access is derived, read-only, from its single owner's existing ``UserSubscription``
(``has_entitlement``: an active paid tier or valid complimentary access). Membership is necessary but not sufficient.
It fails closed (no owner, several owners, inactive owner, free/inactive/expired plan). Billing, Stripe and the legacy
product are untouched; organizations are created only by the operator-run provisioning command.
"""

import io
import pathlib
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import organization_entitlement as oe
from .models import (
    CustomerRadar, Opportunity, Organization, OrganizationMember, OrganizationProfile, OrganizationRadar, RadarMatch,
    UserCompanyLead, UserSubscription,
)
from .organization_access import (
    OrganizationAccessDenied, OrganizationNotEntitled, get_authorized_workspace_dashboard,
    get_organization_access_context, get_workspace_navigation,
)
from .organization_views import ENTITLEMENT_REQUIRED_MESSAGE
from .organizations import add_organization_member, create_organization
from .test_customer_workspace import WorkspaceTestCase
from .test_gemi_company_activities import entitled_user, radar_for
from .test_organization_radar_matching import make_company
from .test_organization_radars import entitle


def person(email, **fields):
    return User.objects.create_user(email, email, "StrongPass123", **fields)


def set_plan(user, **values):
    UserSubscription.objects.filter(user=user).update(**values)


def both_forms(organization):
    """The Python resolver and the SQL form must always agree."""
    python = oe.resolve_organization_entitlement(organization)
    sql = oe.entitled_organizations().filter(pk=organization.pk).exists()
    return python, sql


# --- the resolver -------------------------------------------------------------------------------------------

class ResolverTests(TestCase):
    def setUp(self):
        self.owner = person("owner-ent@example.com")
        self.org = create_organization(owner=self.owner, name="Πελάτης").organization

    def test_every_paid_tier_with_an_active_subscription_is_entitled_with_its_tier(self):
        for tier in ("pro", "business", "enterprise", "custom"):
            set_plan(self.owner, tier=tier, status="active")
            python, sql = both_forms(self.org)
            self.assertEqual((python.entitled, python.reason, python.effective_tier, python.owner_user_id, sql),
                             (True, oe.ENTITLED, tier, self.owner.pk, True), tier)

    def test_valid_complimentary_access_counts_and_expired_does_not(self):
        set_plan(self.owner, complimentary_tier="business", complimentary_until=timezone.now() + timedelta(days=3))
        python, sql = both_forms(self.org)
        self.assertEqual((python.entitled, python.effective_tier, sql), (True, "business", True))
        set_plan(self.owner, complimentary_until=timezone.now() - timedelta(days=1))
        python, sql = both_forms(self.org)
        self.assertEqual((python.entitled, python.reason, sql), (False, oe.OWNER_NOT_ENTITLED, False))

    def test_free_default_and_inactive_or_lapsed_plans_are_not_entitled(self):
        for values in ({}, {"tier": "pro", "status": "canceled"}, {"tier": "pro", "status": "past_due"},
                       {"tier": "business", "status": "inactive"}, {"tier": "free", "status": "active"}):
            set_plan(self.owner, tier="free", status="inactive")
            set_plan(self.owner, **values)
            python, sql = both_forms(self.org)
            self.assertEqual((python.entitled, python.reason, python.effective_tier, sql),
                             (False, oe.OWNER_NOT_ENTITLED, "free", False), values)

    def test_a_missing_subscription_row_fails_closed(self):
        set_plan(self.owner, tier="pro", status="active")
        UserSubscription.objects.filter(user=self.owner).delete()
        python, sql = both_forms(Organization.objects.get(pk=self.org.pk))
        self.assertEqual((python.entitled, python.reason, sql), (False, oe.OWNER_NOT_ENTITLED, False))

    def test_an_inactive_owner_account_fails_closed_even_when_paid(self):
        set_plan(self.owner, tier="pro", status="active")
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        python, sql = both_forms(self.org)
        self.assertEqual((python.entitled, python.reason, sql), (False, oe.OWNER_INACTIVE, False))

    def test_no_owner_fails_closed(self):
        entitle(self.owner)
        OrganizationMember.objects.filter(organization=self.org).update(role="admin")
        python, sql = both_forms(self.org)
        self.assertEqual((python.entitled, python.reason, python.owner_user_id, sql), (False, oe.NO_OWNER, None, False))

    def test_several_owners_fail_closed_even_when_all_are_paid(self):
        entitle(self.owner)
        second = entitle(person("second-owner@example.com"), "business")
        add_organization_member(self.org, second, "owner")
        python, sql = both_forms(self.org)
        self.assertEqual((python.entitled, python.reason, python.owner_user_id, sql),
                         (False, oe.MULTIPLE_OWNERS, None, False))

    def test_a_non_owner_member_never_grants_entitlement(self):
        for role in ("admin", "sales_manager", "sales_user", "viewer"):
            member = entitle(person(f"{role}-paid@example.com"), "enterprise")
            add_organization_member(self.org, member, role)
        python, sql = both_forms(self.org)
        self.assertEqual((python.entitled, python.reason, sql), (False, oe.OWNER_NOT_ENTITLED, False))

    def test_no_organization_is_not_entitled_and_the_resolver_writes_nothing(self):
        self.assertEqual(oe.resolve_organization_entitlement(None).reason, oe.NO_ORGANIZATION)
        self.assertEqual(oe.resolve_organization_entitlement(Organization(name="unsaved")).reason, oe.NO_ORGANIZATION)
        entitle(self.owner)
        before = list(UserSubscription.objects.values())
        with CaptureQueriesContext(connection) as queries:
            oe.resolve_organization_entitlement(self.org)
        self.assertEqual(len(queries), 1)
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries.captured_queries))
        self.assertEqual(list(UserSubscription.objects.values()), before)

    def test_the_rule_reuses_the_legacy_entitlement_and_adds_no_plan_logic(self):
        source = pathlib.Path("gemiapp/organization_entitlement.py").read_text(encoding="utf-8")
        self.assertIn("has_entitlement", source)
        self.assertIn("entitlement_q", source)
        for forbidden in ("stripe", "RADAR_LIMITS", '"pro"', '"business"', ".save(", ".update(", ".create("):
            self.assertNotIn(forbidden, source.split('"""', 2)[2], forbidden)


# --- the access rule ------------------------------------------------------------------------------------------

class AccessRuleTests(WorkspaceTestCase):
    """Tenant A (self.org) is a paying customer in the shared fixtures; tenant B too."""

    def lapse(self, org=None):
        owner = (org or self.org).members.get(role="owner").user
        set_plan(owner, tier="free", status="inactive", complimentary_tier="none")

    def test_membership_and_entitlement_are_both_required(self):
        self.assertEqual(self.status_of(self.owner, "organization_dashboard"), 200)
        self.lapse()
        for role, user in self.members.items():
            self.client.force_login(user)
            response = self.client.get(self.ws_url("organization_dashboard"))
            self.assertRedirects(response, reverse("pricing"), fetch_redirect_response=False, msg_prefix=role)
            self.assertIn(ENTITLEMENT_REQUIRED_MESSAGE, [str(m) for m in get_messages(response.wsgi_request)])
            with self.assertRaises(OrganizationNotEntitled):
                get_organization_access_context(user, self.org)

    def test_every_organization_page_and_action_is_gated_and_nothing_changes(self):
        self.lapse()
        company_page = reverse("organization_company_opportunity", args=[self.org.pk, self.company.pk])
        self.client.force_login(self.owner)
        for url in (self.ws_url("organization_dashboard"), self.ws_url("organization_opportunities"),
                    self.ws_url("organization_tasks"), self.ws_url("organization_radars"),
                    self.ws_url("organization_notifications"), company_page):
            response = self.client.get(url)
            self.assertEqual((response.status_code, response["Location"]), (302, reverse("pricing")), url)
        before = list(Opportunity.objects.values())
        for url, data in ((self.save_url(), {}), (self.status_url(), {"status": "won"}),
                          (self.note_url(), {"body": "x"}), (self.task_url(), {"title": "x", "due_on": self.tomorrow()})):
            self.assertEqual(self.client.post(url, data).status_code, 302, url)
        self.assertEqual(list(Opportunity.objects.values()), before)
        self.assertEqual((self.row.notes.count(), self.row.tasks.count()), (0, 0))

    def test_the_paywall_never_renders_tenant_data(self):
        self.lapse()
        self.client.force_login(self.owner)
        final = self.client.get(self.ws_url("organization_dashboard"), follow=True)
        html = final.content.decode()
        self.assertEqual(final.request["PATH_INFO"], reverse("pricing"))
        for secret in (self.company.name, self.company.gemi_number, self.row.radar.name):
            self.assertNotIn(secret, html)

    def test_non_members_of_an_unpaid_organization_still_get_the_same_404(self):
        self.lapse()
        outsider = person("outsider-ent@example.com")
        for who in (outsider, self.b_owner):
            self.assertEqual(self.status_of(who, "organization_dashboard"), 404)
            with self.assertRaises(OrganizationAccessDenied) as caught:
                get_organization_access_context(who, self.org)
            self.assertNotIsInstance(caught.exception, OrganizationNotEntitled)

    def test_a_non_owner_members_own_subscription_grants_nothing(self):
        self.lapse()
        entitle(self.members["admin"], "enterprise")
        entitle(self.maria_user, "business")
        for who in (self.members["admin"], self.maria_user, self.owner):
            self.assertEqual(self.status_of(who, "organization_dashboard"), 302)

    def test_a_lapse_or_renewal_applies_on_the_next_request(self):
        self.assertEqual(self.status_of(self.owner, "organization_tasks"), 200)
        self.lapse()
        self.assertEqual(self.status_of(self.owner, "organization_tasks"), 302)
        entitle(self.owner, "business")
        self.assertEqual(self.status_of(self.owner, "organization_tasks"), 200)

    def test_staff_and_superusers_get_no_bypass(self):
        self.lapse()
        root = User.objects.create_superuser("root-ent", "root-ent@example.com", "StrongPass123")
        entitle(root, "enterprise")
        self.assertEqual(self.status_of(root, "organization_dashboard"), 404)   # not a member
        add_organization_member(self.org, root, "viewer")
        self.assertEqual(self.status_of(root, "organization_dashboard"), 302)   # member of an unpaid organization

    def test_the_navigation_offers_only_entitled_organizations(self):
        self.assertEqual([w.organization_id for w in get_workspace_navigation(self.owner).workspaces], [self.org.pk])
        self.lapse()
        self.assertEqual(get_workspace_navigation(self.owner).workspaces, ())
        self.client.force_login(self.owner)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn("data-rail-workspace", html)
        self.assertNotIn("data-mobile-workspace", html)

    def test_cross_tenant_protection_is_unchanged(self):
        self.assertEqual(self.status_of(self.owner, "organization_dashboard", self.org_b), 404)
        self.assertEqual(self.status_of(self.b_owner, "organization_dashboard"), 404)
        with self.assertRaises(OrganizationAccessDenied) as caught:
            get_authorized_workspace_dashboard(self.owner, self.org_b.pk)
        self.assertNotIsInstance(caught.exception, OrganizationNotEntitled)

    def test_the_gate_costs_no_extra_query(self):
        with CaptureQueriesContext(connection) as queries:
            get_organization_access_context(self.owner, self.org)
        self.assertEqual(len(queries), 1)
        self.assertIn("subscription", queries.captured_queries[0]["sql"].lower())


# --- provisioning -----------------------------------------------------------------------------------------------

class ProvisioningTests(TestCase):
    def provision(self, *args):
        out = io.StringIO()
        call_command("provision_organization_for_user", *args, stdout=out)
        return out.getvalue()

    def legacy_world(self):
        return (list(UserSubscription.objects.values()), list(CustomerRadar.objects.values()),
                list(UserCompanyLead.objects.values()), list(RadarMatch.objects.values()))

    def setUp(self):
        self.customer = entitled_user("customer@example.com")
        User.objects.filter(pk=self.customer.pk).update(first_name="Ελένη", last_name="Παππά")
        self.customer.refresh_from_db()
        radar = radar_for(self.customer, "Legacy radar", prefectures=["ΑΤΤΙΚΗΣ"])
        company = make_company("880001")
        lead = UserCompanyLead.objects.create(user=self.customer, company=company)
        RadarMatch.objects.create(radar=radar, lead=lead, company=company, matched_on=date(2026, 9, 1),
                                  matched_activity_codes=[], match_reason={})

    def test_it_creates_one_organization_with_its_profile_and_owner_membership(self):
        before = self.legacy_world()
        output = self.provision(str(self.customer.pk))
        organization = Organization.objects.get()
        self.assertEqual(organization.name, "Ελένη Παππά")
        self.assertEqual(list(OrganizationMember.objects.values_list("organization_id", "user_id", "role")),
                         [(organization.pk, self.customer.pk, "owner")])
        self.assertTrue(OrganizationProfile.objects.filter(organization=organization).exists())
        self.assertIn(f"created organization #{organization.pk}", output)
        self.assertIn("2.0 access: entitled (pro)", output)
        self.assertEqual(self.legacy_world(), before)          # subscription, Radars, leads, matches untouched
        self.assertEqual((OrganizationRadar.objects.count(), Opportunity.objects.count()), (0, 0))
        self.assertTrue(oe.resolve_organization_entitlement(organization).entitled)

    def test_it_is_idempotent(self):
        self.provision("customer@example.com")
        world = (list(Organization.objects.values()), list(OrganizationMember.objects.values()), self.legacy_world())
        output = self.provision("CUSTOMER@example.com", "--name", "Άλλο όνομα")
        self.assertIn("unchanged: already owns", output)
        self.assertEqual((list(Organization.objects.values()), list(OrganizationMember.objects.values()),
                          self.legacy_world()), world)

    def test_identifiers_name_and_dry_run(self):
        output = self.provision(self.customer.username, "--name", "  Πελάτης ΑΕ  ", "--dry-run")
        self.assertIn("dry run", output)
        self.assertEqual(Organization.objects.count(), 0)
        self.provision(self.customer.username, "--name", "  Πελάτης ΑΕ  ")
        self.assertEqual(Organization.objects.get().name, "Πελάτης ΑΕ")

    def test_an_unpaid_customer_is_provisioned_but_reported_as_not_entitled(self):
        free = person("free@example.com")
        output = self.provision("free@example.com")
        self.assertIn("NOT entitled (owner_not_entitled)", output)
        self.assertEqual(UserSubscription.objects.get(user=free).tier, "free")

    def test_unsafe_or_ambiguous_states_are_refused_and_nothing_is_written(self):
        inactive = person("inactive@example.com", is_active=False)
        member = person("member@example.com")
        add_organization_member(create_organization(owner=person("boss@example.com"), name="Άλλος").organization,
                                member, "viewer")
        many = person("many@example.com")
        first = create_organization(owner=many, name="Ένας").organization
        add_organization_member(create_organization(owner=person("x@example.com"), name="Δύο").organization,
                                person("y@example.com"), "viewer")
        OrganizationMember.objects.create(organization=Organization.objects.get(name="Δύο"), user=many, role="owner")
        person("same@example.com")
        User.objects.create_user("another-login", "SAME@example.com", "StrongPass123")
        world = (Organization.objects.count(), OrganizationMember.objects.count(), self.legacy_world())
        for args, fragment in ((("nobody@example.com",), "No user matches"), (("",), "required"),
                               ((str(inactive.pk),), "inactive"), (("member@example.com",), "non-owner member"),
                               (("many@example.com",), "already owns 2"), (("same@example.com",), "matches 2 users")):
            with self.assertRaises(CommandError) as caught:
                self.provision(*args)
            self.assertIn(fragment, str(caught.exception), args)
        self.assertEqual((Organization.objects.count(), OrganizationMember.objects.count(), self.legacy_world()), world)
        self.assertEqual(Organization.objects.filter(pk=first.pk).count(), 1)


# --- nothing is automatic; the legacy product is unchanged ----------------------------------------------------

class NoAutomaticOrganizationTests(TestCase):
    def test_signup_login_and_page_visits_create_no_organization(self):
        response = self.client.post(reverse("signup"), {
            "email": "new-customer@example.com", "password1": "StrongPass123!x", "password2": "StrongPass123!x"})
        self.assertIn(response.status_code, (200, 302))
        customer = entitled_user("visitor@example.com")
        self.client.force_login(customer)
        for name in ("dashboard", "radar_list", "lead_list", "settings"):
            self.client.get(reverse(name))
        self.assertEqual((Organization.objects.count(), OrganizationMember.objects.count()), (0, 0))

    def test_no_migration_or_signal_calls_the_provisioning_path(self):
        callers = [str(path) for path in pathlib.Path("gemiapp").rglob("*.py")
                   if "create_organization(" in path.read_text(encoding="utf-8")
                   and not path.name.startswith("test") and path.name != "organizations.py"]
        self.assertEqual(callers, [str(pathlib.Path("gemiapp/management/commands/provision_organization_for_user.py"))])

    def test_legacy_access_follows_the_users_own_subscription_as_before(self):
        paid = entitled_user("paid-legacy@example.com")
        free = person("free-legacy@example.com")
        self.client.force_login(paid)
        self.assertEqual(self.client.get(reverse("lead_export_csv")).status_code, 200)
        self.client.force_login(free)
        self.assertRedirects(self.client.get(reverse("lead_export_csv")), reverse("pricing"),
                             fetch_redirect_response=False)
        # Owning an organization changes nothing for the legacy product, either way round.
        create_organization(owner=free, name="Δωρεάν")
        self.assertRedirects(self.client.get(reverse("lead_export_csv")), reverse("pricing"),
                             fetch_redirect_response=False)
