"""Tests for tenant isolation and organization authorization (release gate G5, gemiapp.organization_access).

Authorization always starts from an explicit (user, organization) pair and the membership joining them; every
refusal is the same error; every read is scoped in SQL. §82: «Organization A requests opportunity B -> 404/403».
"""

import dataclasses
import inspect
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.signing import TimestampSigner
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from . import organization_access as g5
from .company_contact import extract_company_contact_phones
from .ingestion.monitoring import radar_match_evidence
from .ingestion.refresh import RefreshPolicy, build_company_refresh_plan
from .models import (
    Company, CompanyMonitoring, CustomerRadar, DigestDelivery, GemiKad, IndustryTemplate, Opportunity,
    OpportunitySignal, Organization, OrganizationMember, OrganizationRadar, RadarMatch, UserCompanyLead,
    UserSubscription,
)
from .opportunity_feed import FeedFilters, OpportunityFeedPage
from .organization_access import (
    ASSIGNMENT_DEPENDENT, ROLE_CAPABILITIES, Capability, OrganizationAccessContext, OrganizationAccessDenied, can,
    get_authorized_opportunity, get_authorized_opportunity_feed, get_authorized_opportunity_score_breakdown,
    get_authorized_radar, get_organization_access_context, organization_opportunities_for, organization_radars_for,
    require,
)
from .organization_radars import RadarDefinition, create_organization_radar
from .organizations import add_organization_member, create_organization
from .services import company_matches_radar, eligible_radars, send_digests
from .test_gemi_company_activities import entitled_user, radar_for
from .test_opportunity_feed import FeedTestCase
from .test_organization_radars import entitle
from .test_organization_radar_matching import PRIVATE, T0, make_company, new_company_signal, snapshot

ROLES = ("owner", "admin", "sales_manager", "sales_user", "viewer")
C = Capability
EXPECTED = {
    #                          owner  admin  mgr    user   viewer
    C.VIEW_ORGANIZATION:          (1,    1,     1,     1,     1),
    C.VIEW_ORGANIZATION_SETTINGS: (1,    1,     1,     0,     1),
    C.MANAGE_ORGANIZATION:        (1,    1,     0,     0,     0),
    C.MANAGE_MEMBERS:             (1,    1,     0,     0,     0),
    C.VIEW_RADARS:                (1,    1,     1,     0,     1),
    C.MANAGE_RADARS:              (1,    1,     0,     0,     0),
    C.VIEW_ALL_OPPORTUNITIES:     (1,    1,     1,     0,     1),
    C.VIEW_ASSIGNED_OPPORTUNITIES:(1,    1,     1,     1,     1),
    C.MANAGE_OPPORTUNITY_WORKFLOW:(1,    0,     1,     0,     0),
    C.ASSIGN_OPPORTUNITIES:       (1,    0,     1,     0,     0),
    # D32: sales users too, but only ever within their assignment scope.
    C.UPDATE_OPPORTUNITY_STATUS:  (1,    0,     1,     1,     0),
    # D33: writing notes follows the sales workflow; reading them follows the opportunity.
    C.ADD_OPPORTUNITY_NOTE:       (1,    0,     1,     1,     0),
    # D34: tasks follow the sales workflow; a sales user completes only their own (checked per task).
    C.MANAGE_OPPORTUNITY_TASKS:   (1,    0,     1,     1,     0),
    # D35: company-level Do Not Contact touches hidden sibling rows, so never a sales user's.
    C.MANAGE_CONTACT_SUPPRESSIONS:(1,    0,     1,     0,     0),
}


def user(email):
    return User.objects.create_user(email, email, "StrongPass123")


class AccessTestCase(FeedTestCase):
    """Two tenants: A (self.org, the feed fixtures) and B, with a member of every role in A."""

    def setUp(self):
        super().setUp()
        self.owner = self.org.members.get(role="owner").user
        self.members = {"owner": self.owner}
        for role in ROLES[1:]:
            self.members[role] = user(f"{role}@a.example.com")
            add_organization_member(self.org, self.members[role], role)
        self.b_owner = entitle(user("owner@b.example.com"))
        self.org_b = create_organization(owner=self.b_owner, name="Tenant B").organization
        self.radar_b_foreign = create_organization_radar(self.org_b, RadarDefinition(
            name="B secret radar", active=True, signal_types=("new_company",)))

    def denied(self, call, *args, **kwargs):
        with self.assertRaises(OrganizationAccessDenied) as caught:
            call(*args, **kwargs)
        self.assertEqual(str(caught.exception), OrganizationAccessDenied.MESSAGE)
        return caught.exception


# --- the pair, the membership, the context ---------------------------------------------------------

class ContextTests(AccessTestCase):
    def test_a_member_gets_an_immutable_context_from_their_membership(self):
        context = get_organization_access_context(self.members["viewer"], self.org)
        membership = OrganizationMember.objects.get(organization=self.org, user=self.members["viewer"])
        self.assertEqual((context.organization_id, context.user_id, context.membership_id, context.role),
                         (self.org.pk, self.members["viewer"].pk, membership.pk, "viewer"))
        self.assertTrue(dataclasses.is_dataclass(context) and OrganizationAccessContext.__dataclass_params__.frozen)
        self.assertNotIn("_issuer", repr(context))

    def test_non_members_unsaved_inactive_and_foreign_inputs_are_denied(self):
        outsider = user("outsider@example.com")
        inactive = user("inactive@a.example.com")
        add_organization_member(self.org, inactive, "viewer")
        inactive.is_active = False
        inactive.save()
        for who, org in ((outsider, self.org), (self.members["owner"], self.org_b), (User(username="x"), self.org),
                         (self.owner, Organization(name="unsaved")), (None, self.org), (self.owner, None),
                         (inactive, self.org), (self.owner, self.org.pk), (self.owner.pk, self.org)):
            self.denied(get_organization_access_context, who, org)

    def test_staff_and_superusers_get_no_customer_bypass(self):
        root = User.objects.create_superuser("root-g5", "root-g5@example.com", "StrongPass123")
        staff = user("staff-g5@example.com")
        staff.is_staff = True
        staff.save()
        for who in (root, staff):
            self.denied(get_organization_access_context, who, self.org)
            self.denied(get_authorized_opportunity_feed, who, self.org)

    def test_a_hand_built_context_is_refused(self):
        forged = OrganizationAccessContext(organization_id=self.org_b.pk, user_id=self.owner.pk, membership_id=1,
                                           role="owner")
        for call in (lambda: can(forged, C.VIEW_RADARS), lambda: organization_radars_for(forged),
                     lambda: organization_opportunities_for(forged)):
            self.denied(call)

    def test_deleting_the_membership_revokes_access_on_the_next_call(self):
        row = self.opportunity(self.radar_a, self.company, score=80)
        viewer = self.members["viewer"]
        self.assertEqual(get_authorized_opportunity(viewer, self.org, row.pk).pk, row.pk)
        OrganizationMember.objects.filter(organization=self.org, user=viewer).delete()
        self.denied(get_authorized_opportunity, viewer, self.org, row.pk)
        self.denied(get_authorized_opportunity_feed, viewer, self.org)

    def test_a_role_change_applies_on_the_next_call(self):
        viewer = self.members["viewer"]
        self.assertFalse(can(get_organization_access_context(viewer, self.org), C.MANAGE_RADARS))
        OrganizationMember.objects.filter(organization=self.org, user=viewer).update(role="admin")
        self.assertTrue(can(get_organization_access_context(viewer, self.org), C.MANAGE_RADARS))
        OrganizationMember.objects.filter(organization=self.org, user=viewer).update(role="sales_user")
        self.assertFalse(can(get_organization_access_context(viewer, self.org), C.VIEW_ALL_OPPORTUNITIES))


# --- the capability matrix -------------------------------------------------------------------------

class CapabilityMatrixTests(AccessTestCase):
    def test_the_matrix_is_exactly_section_64_and_covers_every_c1_role(self):
        self.assertEqual(set(ROLE_CAPABILITIES), {value for value, _ in OrganizationMember.ROLES})
        self.assertEqual(set(EXPECTED), set(Capability.ALL))
        for capability, grants in EXPECTED.items():
            for role, granted in zip(ROLES, grants):
                context = get_organization_access_context(self.members[role], self.org)
                self.assertEqual(can(context, capability), bool(granted), (role, capability))
        self.assertEqual(ROLE_CAPABILITIES["owner"], frozenset(Capability.ALL))  # owner: «όλα», same table
        self.assertEqual(ASSIGNMENT_DEPENDENT, frozenset({C.VIEW_ASSIGNED_OPPORTUNITIES}))

    def test_viewer_is_read_only(self):
        viewer = get_organization_access_context(self.members["viewer"], self.org)
        writes = {C.MANAGE_ORGANIZATION, C.MANAGE_MEMBERS, C.MANAGE_RADARS, C.MANAGE_OPPORTUNITY_WORKFLOW,
                  C.ASSIGN_OPPORTUNITIES, C.UPDATE_OPPORTUNITY_STATUS, C.ADD_OPPORTUNITY_NOTE,
                  C.MANAGE_OPPORTUNITY_TASKS, C.MANAGE_CONTACT_SUPPRESSIONS}
        self.assertFalse([c for c in writes if can(viewer, c)])
        for capability in writes:
            self.denied(require, viewer, capability)

    def test_role_strings_are_compared_only_in_the_matrix(self):
        code = inspect.getsource(g5).split('"""', 2)[2]
        body = code.split("ROLE_CAPABILITIES = {", 1)[1].split("\n}\n", 1)[1]
        for role in ('"owner"', '"admin"', '"sales_manager"', '"sales_user"', '"viewer"', "role ==", "is_staff",
                     "is_superuser"):
            self.assertNotIn(role, body, role)

    def test_one_user_in_two_organizations_keeps_two_separate_roles(self):
        # The owner of A becomes a mere viewer of B: nothing of A's ownership bleeds into B.
        add_organization_member(self.org_b, self.owner, "viewer")
        in_a = get_organization_access_context(self.owner, self.org)
        in_b = get_organization_access_context(self.owner, self.org_b)
        self.assertEqual((in_a.role, in_b.role), ("owner", "viewer"))
        self.assertTrue(can(in_a, C.MANAGE_RADARS))
        self.assertFalse(can(in_b, C.MANAGE_RADARS))
        self.assertFalse(can(in_b, C.MANAGE_OPPORTUNITY_WORKFLOW))
        a_row = self.opportunity(self.radar_a, self.company, score=80)
        b_row = self.opportunity(self.radar_b_foreign, self.company, score=90)
        self.assertEqual(list(organization_opportunities_for(in_a)), [a_row])
        self.assertEqual(list(organization_opportunities_for(in_b)), [b_row])
        self.denied(get_authorized_opportunity, self.owner, self.org_b, a_row.pk)  # A's row through B's door
        self.denied(get_authorized_opportunity, self.owner, self.org, b_row.pk)    # and the other way round


# --- opportunities, feed, radars -------------------------------------------------------------------

class OpportunityAccessTests(AccessTestCase):
    def test_org_wide_roles_read_their_own_opportunities_with_the_frozen_breakdown(self):
        radar = self.radar(name="real", **self.everything())
        snapshot(self.company, T0)
        result = self.materialize(new_company_signal(self.company, T0), radar)
        for role in ("owner", "admin", "sales_manager", "viewer"):
            row = get_authorized_opportunity(self.members[role], self.org, result.opportunity.pk)
            self.assertEqual(row.pk, result.opportunity.pk, role)
            breakdown = get_authorized_opportunity_score_breakdown(self.members[role], self.org, row.pk)
            self.assertEqual((breakdown.score, breakdown.organization_id), (100, self.org.pk))

    def test_a_sales_user_sees_no_opportunity_until_assignment_exists(self):
        row = self.opportunity(self.radar_a, self.company, score=95)
        sales_user = self.members["sales_user"]
        context = get_organization_access_context(sales_user, self.org)
        self.assertTrue(can(context, C.VIEW_ASSIGNED_OPPORTUNITIES))
        self.assertFalse(can(context, C.VIEW_ALL_OPPORTUNITIES))
        self.assertEqual(list(organization_opportunities_for(context)), [])
        self.denied(get_authorized_opportunity, sales_user, self.org, row.pk)
        self.denied(get_authorized_opportunity_score_breakdown, sales_user, self.org, row.pk)
        page = get_authorized_opportunity_feed(sales_user, self.org)
        self.assertEqual((page.cards, page.next_cursor, page.organization_id), ((), None, self.org.pk))
        self.assertIsInstance(page, OpportunityFeedPage)
        self.denied(get_authorized_radar, sales_user, self.org, self.radar_a.pk)
        self.denied(get_authorized_opportunity_feed, sales_user, self.org, FeedFilters(radar_ids=(self.radar_a.pk,)))

    def test_organization_a_requesting_opportunity_b_is_refused_like_a_missing_id(self):
        a_rows = [self.opportunity(self.radar_a, make_company(f"6000{i}"), score=70) for i in range(2)]
        b_row = self.opportunity(self.radar_b_foreign, make_company("600099"), score=99)
        missing = max(Opportunity.objects.values_list("pk", flat=True)) + 1
        self.assertEqual(b_row.pk, a_rows[-1].pk + 1)  # adjacent ids: the classic enumeration target
        foreign = self.denied(get_authorized_opportunity, self.owner, self.org, b_row.pk)
        absent = self.denied(get_authorized_opportunity, self.owner, self.org, missing)
        self.assertEqual((type(foreign), str(foreign)), (type(absent), str(absent)))
        for bad in ("1", None, True, 1.0):
            self.denied(get_authorized_opportunity, self.owner, self.org, bad)

    def test_the_authorized_feed_shows_only_the_members_organization(self):
        mine = self.opportunity(self.radar_a, self.company, score=70)
        theirs = self.opportunity(self.radar_b_foreign, self.company, score=99)  # same company, tenant B
        for role in ("owner", "admin", "sales_manager", "viewer"):
            page = get_authorized_opportunity_feed(self.members[role], self.org)
            self.assertEqual([c.primary_opportunity_id for c in page.cards], [mine.pk], role)
        page_b = get_authorized_opportunity_feed(self.b_owner, self.org_b)
        self.assertEqual([c.primary_opportunity_id for c in page_b.cards], [theirs.pk])
        self.denied(get_authorized_opportunity_feed, self.owner, self.org_b)  # not a member of B

    def test_a_foreign_radar_filter_reveals_nothing(self):
        self.opportunity(self.radar_b_foreign, self.company, score=80)
        missing = max(OrganizationRadar.objects.values_list("pk", flat=True)) + 1
        foreign = self.denied(get_authorized_opportunity_feed, self.owner, self.org,
                              FeedFilters(radar_ids=(self.radar_b_foreign.pk,)))
        absent = self.denied(get_authorized_opportunity_feed, self.owner, self.org, FeedFilters(radar_ids=(missing,)))
        self.assertEqual(str(foreign), str(absent))
        own = get_authorized_opportunity_feed(self.owner, self.org, FeedFilters(radar_ids=(self.radar_a.pk,)))
        self.assertEqual(own.cards, ())

    def test_radars_are_reachable_only_inside_their_organization(self):
        for role in ("owner", "admin", "sales_manager", "viewer"):
            self.assertEqual(get_authorized_radar(self.members[role], self.org, self.radar_a.pk), self.radar_a)
        self.denied(get_authorized_radar, self.owner, self.org, self.radar_b_foreign.pk)
        self.denied(get_authorized_radar, self.b_owner, self.org_b, self.radar_a.pk)
        context = get_organization_access_context(self.owner, self.org)
        self.assertEqual(set(organization_radars_for(context)), {self.radar_a, self.radar_b})
        self.assertNotIn(self.radar_b_foreign, set(organization_radars_for(context)))


class SettingsAndMembersAccessTests(AccessTestCase):
    def test_profile_icp_and_members_are_reachable_only_through_the_membership(self):
        from .organization_icp import ICPCriteria, create_organization_icp

        create_organization_icp(self.org, ICPCriteria(signal_types=("new_company",)))
        create_organization_icp(self.org_b, ICPCriteria(signal_types=("kad_added",)))
        for role in ("owner", "admin", "sales_manager", "viewer"):
            self.assertEqual(g5.get_authorized_organization_profile(self.members[role], self.org).organization_id,
                             self.org.pk)
            self.assertEqual(g5.get_authorized_icp_criteria(self.members[role], self.org).signal_types,
                             ("new_company",))
        self.denied(g5.get_authorized_organization_profile, self.members["sales_user"], self.org)
        self.denied(g5.get_authorized_icp_criteria, self.members["sales_user"], self.org)
        self.denied(g5.get_authorized_icp_criteria, self.owner, self.org_b)       # not a member of B
        self.denied(g5.get_authorized_organization_profile, self.b_owner, self.org)

    def test_members_are_listed_to_administrators_and_team_leads_only_without_personal_fields(self):
        for role, allowed in (("owner", True), ("admin", True), ("sales_manager", True), ("sales_user", False),
                              ("viewer", False)):
            context = get_organization_access_context(self.members[role], self.org)
            if allowed:
                rows = list(g5.organization_members_for(context))
                self.assertEqual({row.organization_id for row in rows}, {self.org.pk}, role)
                self.assertEqual(len(rows), 5)
            else:
                self.denied(g5.organization_members_for, context)
        with CaptureQueriesContext(connection) as queries:
            list(g5.organization_members_for(get_organization_access_context(self.owner, self.org)))
        sql = queries.captured_queries[-1]["sql"]
        self.assertIn('"organization_id"', sql)
        for personal in ("email", "username", "first_name", "last_name", "password"):
            self.assertNotIn(personal, sql)


# --- scoping, efficiency, safety -------------------------------------------------------------------

class SafetyTests(AccessTestCase):
    def test_every_read_is_scoped_in_sql_and_bounded(self):
        for index in range(20):
            self.opportunity(self.radar_a if index % 2 else self.radar_b, make_company(f"61{index:04d}"), score=50)
        viewer = self.members["viewer"]
        with CaptureQueriesContext(connection) as context_queries:
            get_organization_access_context(viewer, self.org)
        self.assertEqual(len(context_queries), 1)
        membership_sql = context_queries.captured_queries[0]["sql"]
        self.assertIn('"organization_id"', membership_sql)
        self.assertIn('"user_id"', membership_sql)
        with CaptureQueriesContext(connection) as feed_queries:
            self.assertEqual(len(get_authorized_opportunity_feed(viewer, self.org).cards), 20)
        self.assertEqual(len(feed_queries), 4)  # membership + C9's three
        target = Opportunity.objects.filter(organization=self.org).first().pk
        with CaptureQueriesContext(connection) as detail_queries:
            get_authorized_opportunity(viewer, self.org, target)
        self.assertEqual(len(detail_queries), 2)  # membership + one scoped fetch
        self.assertIn('"organization_id"', detail_queries.captured_queries[1]["sql"])

    def test_reads_write_nothing(self):
        row = self.opportunity(self.radar_a, self.company, score=80)
        before = (list(Opportunity.objects.values()), list(OrganizationMember.objects.values()),
                  list(OrganizationRadar.objects.values()), list(OpportunitySignal.objects.values()))
        with CaptureQueriesContext(connection) as queries:
            for role in ROLES:
                try:
                    get_authorized_opportunity_feed(self.members[role], self.org)
                    get_authorized_opportunity(self.members[role], self.org, row.pk)
                    get_authorized_radar(self.members[role], self.org, self.radar_a.pk)
                except OrganizationAccessDenied:
                    pass
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries.captured_queries))
        self.assertEqual((list(Opportunity.objects.values()), list(OrganizationMember.objects.values()),
                          list(OrganizationRadar.objects.values()), list(OpportunitySignal.objects.values())), before)

    def test_refusals_carry_no_foreign_detail(self):
        b_row = self.opportunity(self.radar_b_foreign, self.company, score=99)
        texts = []
        for call, args in ((get_authorized_opportunity, (self.owner, self.org, b_row.pk)),
                           (get_authorized_radar, (self.owner, self.org, self.radar_b_foreign.pk)),
                           (get_authorized_opportunity_feed, (self.owner, self.org_b))):
            error = self.denied(call, *args)
            texts.append(f"{error!s} {error!r} {error.args}")
        text = " ".join(texts)
        for secret in (str(b_row.pk), "B secret radar", "Tenant B", str(self.radar_b_foreign.pk), self.company.name,
                       *PRIVATE):
            self.assertNotIn(secret, text)

    def test_platform_data_is_not_tenant_scoped(self):
        for model in (IndustryTemplate, GemiKad):
            self.assertFalse([f for f in model._meta.get_fields() if f.name == "organization"], model)
        code = inspect.getsource(g5).split('"""', 2)[2]
        for platform in ("IndustryTemplate", "GemiKad", "GemiPrefecture", "GemiLegalType"):
            self.assertNotIn(platform, code)

    def test_customer_reads_never_touch_the_cross_tenant_engines(self):
        code = inspect.getsource(g5).split('"""', 2)[2]
        # The frozen C8 reader is the only explanation source; the live C5-C7 engines are never imported.
        for engine in ("organization_radar_matching", "opportunity_scoring", "from .opportunity_score_breakdown",
                       "build_opportunity_score_breakdown", "explain_", "materialize", "request.user", "session",
                       "user.organization"):
            self.assertNotIn(engine, code, engine)

    def test_routes_use_the_layer_only_through_organization_views_and_no_middleware_model_or_migration(self):
        from django.apps import apps
        from django.conf import settings

        # D29 added the first organization route; it lives in organization_views, the one view module that imports
        # this layer. Legacy views, the URL conf itself, tasks and the project URLs never import it.
        for path in ("gemiapp/urls.py", "gemiapp/views.py", "config/urls.py", "gemiapp/tasks.py"):
            self.assertNotIn("from .organization_access", open(path, encoding="utf-8").read(), path)
            self.assertNotIn("import organization_access", open(path, encoding="utf-8").read(), path)
        self.assertIn("from .organization_access import", open("gemiapp/organization_views.py", encoding="utf-8").read())
        self.assertFalse([m for m in settings.MIDDLEWARE if "organization" in m.lower() or "tenant" in m.lower()])
        names = {model.__name__ for model in apps.get_app_config("gemiapp").get_models()}
        self.assertFalse([n for n in names if "Access" in n or "Permission" in n or "Assign" in n])
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(max(name for app, name in loader.disk_migrations if app == "gemiapp"),
                         "0055_gemi_request_attempt")  # G6 owns 0055 (GemiRequestAttempt); any newer migration must update this pin deliberately

    def test_the_legacy_product_and_billing_are_untouched(self):
        legacy_user = entitled_user("legacy-g5@example.com")
        legacy = radar_for(legacy_user, "legacy", prefectures=["ΑΤΤΙΚΗΣ"])
        run_at = datetime(2026, 9, 16, 9, tzinfo=dt_timezone.utc)
        CompanyMonitoring.objects.create(
            company=self.company, state="active", priority="high", primary_reason="active_radar_match",
            policy_version=1, monitored_since=run_at - timedelta(days=3), next_check_at=run_at - timedelta(hours=1))
        policy = RefreshPolicy(max_requests_per_run=5, max_pages_per_query=2, max_direct_details_per_run=2, page_size=2)
        row = self.opportunity(self.radar_a, self.company, score=80)

        def world():
            mail.outbox = []
            DigestDelivery.objects.all().delete()
            sent = send_digests(date(2026, 9, 1))
            return (
                list(CustomerRadar.objects.values()), [r.pk for r in eligible_radars()],
                company_matches_radar(Company.objects.get(pk=self.company.pk), CustomerRadar.objects.get(pk=legacy.pk)),
                {pk: e.source_ids for pk, e in radar_match_evidence().items()},
                build_company_refresh_plan(run_at=run_at, policy=policy).summary(),
                list(UserSubscription.objects.values()), UserSubscription.objects.get(user=legacy_user).has_entitlement,
                list(CompanyMonitoring.objects.values()), list(RadarMatch.objects.values()),
                list(UserCompanyLead.objects.values()), (sent, [(m.subject, m.body) for m in mail.outbox]),
            )

        with patch("django.core.signing.TimestampSigner.timestamp", return_value=TimestampSigner().timestamp()), \
                patch("gemiapp.ingestion.refresh.get_gemi_client") as client:
            before = world()
            get_authorized_opportunity_feed(self.owner, self.org)
            get_authorized_opportunity(self.owner, self.org, row.pk)
            self.assertEqual(world(), before)
        client.assert_not_called()
        # A legacy entitled user with no membership reaches nothing in the Organization architecture.
        self.denied(get_authorized_opportunity_feed, legacy_user, self.org)

    def test_the_phone_bridge_is_intact(self):
        self.assertEqual([p.display for p in extract_company_contact_phones(
            {"phone": "2109990001", "persons": [{"phone": "6999990002"}]})], ["2109990001"])
        self.assertEqual([p.tel_uri for p in Company(gemi_number="1", raw_data={"phone": "+30 2109990001"}).gemi_phones],
                         ["tel:+302109990001"])
