"""Tests for D31 Assign (blueprint §40) and the sales-user visibility it switches on.

One explicit C8 opportunity is assigned to one active SALES_USER membership of the same organization, by OWNER or
SALES_MANAGER. NEW/VIEWED/SAVED -> ASSIGNED; reassignment while ASSIGNED; the same assignee is a no-write no-op;
later §39 states are refused untouched; no unassign. A sales user then sees exactly their own assignments -- in the
feed, the detail read and the D29 page -- and never a sibling opportunity of the same company.
"""

import inspect
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.signing import TimestampSigner
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from . import organization_access as g5
from .company_contact import extract_company_contact_phones
from .company_signals import SHADOW
from .ingestion.monitoring import radar_match_evidence
from .ingestion.refresh import RefreshPolicy, build_company_refresh_plan
from .models import (
    Company, CompanyMonitoring, CustomerRadar, DigestDelivery, Opportunity, OrganizationMember, RadarMatch,
    UserCompanyLead, UserSubscription,
)
from .opportunity_feed import get_opportunity_feed
from .organization_access import (
    ASSIGN_ALLOWED_FROM, AssignmentRefused, OrganizationAccessDenied, assign_authorized_opportunity,
    get_authorized_opportunity, get_authorized_opportunity_feed, get_authorized_opportunity_score_breakdown,
    save_authorized_opportunity, OpportunityTransitionRefused,
)
from .organizations import add_organization_member
from .services import company_matches_radar, eligible_radars, send_digests
from .test_gemi_company_activities import entitled_user, radar_for
from .test_opportunity_save import FROZEN_FIELDS, LATER_STATES, SaveTestCase
from .test_organization_radar_matching import T0, snapshot

LATER_THAN_ASSIGNED = tuple(state for state in LATER_STATES if state != "assigned")
MARIA_EMAIL = "maria@a.example.com"


class AssignTestCase(SaveTestCase):
    def setUp(self):
        super().setUp()
        self.maria_user = User.objects.create_user(MARIA_EMAIL, MARIA_EMAIL, "StrongPass123", first_name="Μαρία",
                                                   last_name="Παππά")
        self.maria = add_organization_member(self.org, self.maria_user, "sales_user")
        self.nikos = OrganizationMember.objects.get(organization=self.org, user=self.members["sales_user"])

    def assign(self, member, row=None, actor=None):
        return assign_authorized_opportunity(actor or self.owner, self.org.pk, (row or self.row).pk,
                                             member if isinstance(member, (int, str)) or member is None else member.pk)

    def assign_url(self, row=None, org=None):
        return reverse("organization_assign_opportunity",
                       kwargs={"organization_id": (org or self.org).pk, "opportunity_id": (row or self.row).pk})

    def post_assign(self, who, member, row=None, org=None):
        self.client.force_login(who)
        value = member if isinstance(member, (int, str)) else member.pk
        return self.client.post(self.assign_url(row, org), {"assignee_membership_id": value})

    def stored(self, row=None):
        return Opportunity.objects.filter(pk=(row or self.row).pk).values().get()


# --- schema ------------------------------------------------------------------------------------------

class SchemaTests(AssignTestCase):
    def test_two_nullable_fields_on_the_opportunity_and_nothing_else(self):
        field = Opportunity._meta.get_field("assigned_to")
        self.assertIs(field.related_model, OrganizationMember)
        self.assertTrue(field.null)
        self.assertEqual(field.remote_field.on_delete.__name__, "SET_NULL")
        self.assertTrue(Opportunity._meta.get_field("assigned_at").null)
        loader = MigrationLoader(None, ignore_no_migrations=True)
        # D31 owns 0049; D33's 0050 (notes) follows it and depends on it.
        self.assertEqual(loader.disk_migrations[("gemiapp", "0050_opportunity_note")].dependencies,
                         [("gemiapp", "0049_opportunity_assignment")])
        migration = loader.disk_migrations[("gemiapp", "0049_opportunity_assignment")]
        self.assertEqual(sorted((type(op).__name__, op.name) for op in migration.operations),
                         [("AddField", "assigned_at"), ("AddField", "assigned_to")])
        from django.apps import apps

        names = {model.__name__ for model in apps.get_app_config("gemiapp").get_models()}
        # No assignment, activity-history, audit or notification model: those belong to D36/D37.
        self.assertFalse([n for n in names if "Assign" in n])
        self.assertEqual({n for n in names if "Notification" in n}, {"OrganizationNotification"})  # D37
        self.assertEqual({n for n in names if "Audit" in n}, {"AdminAuditLog", "OrganizationAuditEvent"})  # D36  # pre-existing staff audit only
        self.assertEqual((self.row.assigned_to_id, self.row.assigned_at), (None, None))


# --- the transition contract -----------------------------------------------------------------------

class TransitionTests(AssignTestCase):
    def test_first_assignment_from_new_viewed_and_saved(self):
        self.assertEqual(ASSIGN_ALLOWED_FROM, frozenset({"new", "viewed", "saved"}))
        for start in ("new", "viewed", "saved"):
            Opportunity.objects.filter(pk=self.row.pk).update(status=start, assigned_to=None, assigned_at=None)
            moment = datetime(2026, 9, 18, 10, tzinfo=dt_timezone.utc)
            with patch("django.utils.timezone.now", return_value=moment):
                result = self.assign(self.maria)
            row = Opportunity.objects.get(pk=self.row.pk)
            self.assertEqual((row.status, row.assigned_to_id, row.assigned_at), ("assigned", self.maria.pk, moment), start)
            self.assertEqual((result.changed, result.assigned_membership_id, result.assigned_user_id,
                              result.previous_assigned_membership_id), (True, self.maria.pk, self.maria_user.pk, None))

    def test_the_same_assignee_is_a_no_op_without_any_write(self):
        self.assign(self.maria)
        before = self.stored()
        with CaptureQueriesContext(connection) as queries:
            result = self.assign(self.maria)
        self.assertFalse(result.changed)
        self.assertEqual([q["sql"] for q in queries.captured_queries
                          if not q["sql"].lstrip().upper().startswith(("SELECT", "SAVEPOINT", "RELEASE"))], [])
        self.assertEqual(self.stored(), before)  # assigned_at and updated_at included

    def test_reassignment_keeps_assigned_and_moves_the_timestamp(self):
        first, second = (datetime(2026, 9, 18, hour, tzinfo=dt_timezone.utc) for hour in (9, 11))
        with patch("django.utils.timezone.now", return_value=first):
            self.assign(self.maria)
        with patch("django.utils.timezone.now", return_value=second):
            result = self.assign(self.nikos, actor=self.members["sales_manager"])
        row = Opportunity.objects.get(pk=self.row.pk)
        self.assertEqual((row.status, row.assigned_to_id, row.assigned_at), ("assigned", self.nikos.pk, second))
        self.assertEqual((result.changed, result.previous_assigned_membership_id), (True, self.maria.pk))
        self.assertEqual(Opportunity.objects.count(), 1)

    def test_every_later_state_is_refused_and_untouched(self):
        self.assign(self.maria)
        for state in LATER_THAN_ASSIGNED:
            Opportunity.objects.filter(pk=self.row.pk).update(status=state)
            before = self.stored()
            with self.assertRaises(AssignmentRefused) as refused:
                self.assign(self.nikos)
            self.assertEqual(refused.exception.reason, "state", state)
            self.assertEqual(self.stored(), before, state)
            response = self.post_assign(self.owner, self.nikos)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.stored()["assigned_to_id"], self.maria.pk, state)

    def test_there_is_no_unassign(self):
        self.assign(self.maria)
        for empty in (None, "", "0", "-1", "abc", "1.5"):
            with self.assertRaises(AssignmentRefused) as refused:
                self.assign(empty)
            self.assertEqual(refused.exception.reason, "assignee")
        self.assertEqual(self.stored()["assigned_to_id"], self.maria.pk)
        code = inspect.getsource(g5)
        for definition in ("def unassign", "def clear_assign", "def set_opportunity", "assigned_to = None",
                           "assigned_to_id = None"):
            self.assertNotIn(definition, code, definition)

    def test_save_and_assign_share_one_lifecycle(self):
        save_authorized_opportunity(self.owner, self.org.pk, self.row.pk)
        self.assertEqual(self.stored()["status"], "saved")
        self.assign(self.maria)
        self.assertEqual(self.stored()["status"], "assigned")
        with self.assertRaises(OpportunityTransitionRefused):  # D30 never moves ASSIGNED back to SAVED
            save_authorized_opportunity(self.owner, self.org.pk, self.row.pk)
        self.assertEqual(self.stored()["status"], "assigned")


# --- assignee rule, actor rule, LIVE, targets -------------------------------------------------------

class AccessTests(AssignTestCase):
    def test_only_an_active_sales_user_of_this_organization_can_be_the_assignee(self):
        members = {role: OrganizationMember.objects.get(organization=self.org, user=self.members[role])
                   for role in ("owner", "admin", "sales_manager", "viewer")}
        b_sales = add_organization_member(self.org_b, User.objects.create_user("b-sales@example.com",
                                                                               "b-sales@example.com", "x"), "sales_user")
        inactive_user = User.objects.create_user("idle@a.example.com", "idle@a.example.com", "x")
        inactive = add_organization_member(self.org, inactive_user, "sales_user")
        inactive_user.is_active = False
        inactive_user.save()
        missing = OrganizationMember.objects.order_by("-pk").first().pk + 1
        messages = set()
        for candidate in (*members.values(), b_sales, inactive, missing):
            with self.assertRaises(AssignmentRefused) as refused:
                self.assign(candidate)
            self.assertEqual(refused.exception.reason, "assignee")
            messages.add(str(refused.exception))
        self.assertEqual(len(messages), 1)  # a foreign member and a missing id read the same
        self.assertEqual((self.stored()["status"], self.stored()["assigned_to_id"]), ("new", None))
        self.assertTrue(self.assign(self.maria).changed)

    def test_only_owner_and_sales_manager_may_assign(self):
        for role, allowed in (("owner", True), ("sales_manager", True), ("admin", False), ("viewer", False),
                              ("sales_user", False)):
            Opportunity.objects.filter(pk=self.row.pk).update(status="new", assigned_to=None, assigned_at=None)
            response = self.post_assign(self.members[role], self.maria)
            self.assertEqual(response.status_code, 302 if allowed else 404, role)
            self.assertEqual(self.stored()["assigned_to_id"], self.maria.pk if allowed else None, role)

    def test_shadow_foreign_and_missing_opportunities_are_the_same_404(self):
        shadow_company = Company.objects.create(gemi_number="900200", name="Σκιώδης ΑΕ", incorporation_date=date(2026, 9, 1))
        snapshot(shadow_company, T0)
        shadow = self.live_opportunity(self.full_radar("Shadow"), company=shadow_company, mode=SHADOW)
        foreign = self.live_opportunity(self.radar_b_foreign, company=Company.objects.create(
            gemi_number="900300", name="Ξένη ΑΕ", incorporation_date=date(2026, 9, 1)))
        missing = Opportunity.objects.order_by("-pk").first().pk + 1
        responses = [self.post_assign(self.owner, self.maria, row=shadow),
                     self.post_assign(self.owner, self.maria, row=foreign),
                     self.post_assign(self.owner, self.maria, row=foreign, org=self.org_b),
                     self.post_assign(self.b_owner, self.maria)]
        self.client.force_login(self.owner)
        responses.append(self.client.post(f"/organizations/{self.org.pk}/opportunities/{missing}/assign/",
                                          {"assignee_membership_id": self.maria.pk}))
        self.assertEqual({response.status_code for response in responses}, {404})
        self.assertEqual((self.stored(shadow)["assigned_to_id"], self.stored(foreign)["assigned_to_id"]), (None, None))

    def test_only_the_explicit_opportunity_changes(self):
        row_b = self.live_opportunity(self.full_radar("Radar Β", legal_forms=()))
        before_a, frozen_a, frozen_b = self.stored(), self.frozen(), self.frozen(row_b)
        self.assign(self.maria, row=row_b)
        self.assertEqual(self.stored(), before_a)
        self.assertEqual((self.stored(row_b)["status"], self.stored(row_b)["assigned_to_id"]), ("assigned", self.maria.pk))
        self.assertEqual((self.frozen(), self.frozen(row_b)), (frozen_a, frozen_b))  # scoring capture untouched

    def test_the_endpoint_is_post_only_login_only_csrf_protected_and_redirects_to_the_page(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.assign_url()).status_code, 405)
        anonymous = Client().post(self.assign_url(), {"assignee_membership_id": self.maria.pk})
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn(reverse("login"), anonymous["Location"])
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.owner)
        self.assertEqual(strict.post(self.assign_url(), {"assignee_membership_id": self.maria.pk}).status_code, 403)
        self.assertIsNone(self.stored()["assigned_to_id"])
        response = self.client.post(self.assign_url() + "?next=https://evil.example/",
                                    {"assignee_membership_id": self.maria.pk, "next": "https://evil.example/"})
        self.assertRedirects(response, self.url(), fetch_redirect_response=False)


# --- sales-user visibility ---------------------------------------------------------------------------

class SalesUserVisibilityTests(AssignTestCase):
    def test_detail_and_breakdown_follow_the_membership_assignment(self):
        self.assertRaises(OrganizationAccessDenied, get_authorized_opportunity, self.maria_user, self.org, self.row.pk)
        self.assign(self.maria)
        self.assertEqual(get_authorized_opportunity(self.maria_user, self.org, self.row.pk).pk, self.row.pk)
        self.assertEqual(get_authorized_opportunity_score_breakdown(self.maria_user, self.org, self.row.pk).score, 100)
        nikos_user = self.members["sales_user"]
        self.assertRaises(OrganizationAccessDenied, get_authorized_opportunity, nikos_user, self.org, self.row.pk)

    def test_a_multi_radar_company_shows_a_sales_user_only_their_own_opportunity(self):
        low_radar = self.full_radar("Radar Χαμηλό", legal_forms=(), signal_types=())  # a lower score
        high_radar_b = self.full_radar("Radar Κρυφό")                                  # 100, hidden from Maria
        mine = self.live_opportunity(low_radar)
        theirs = self.live_opportunity(high_radar_b)
        self.assign(self.maria, row=mine)
        self.assign(self.nikos, row=theirs)
        # Feed: one card, one child, primary = her own even though the hidden sibling scores higher.
        card, = get_authorized_opportunity_feed(self.maria_user, self.org).cards
        self.assertEqual((card.opportunity_count, card.primary_opportunity_id), (1, mine.pk))
        self.assertEqual([child.opportunity_id for child in card.opportunities], [mine.pk])
        self.assertEqual(card.score, mine.score)
        # Page: 200, only her row, no trace of the others.
        response = self.get(self.maria_user)
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(html.count("data-opportunity="), 1)
        for hidden in ("Radar Κρυφό", "Radar Α", f'data-opportunity="{theirs.pk}"', f'data-opportunity="{self.row.pk}"'):
            self.assertNotIn(hidden, html, hidden)
        self.assertIn("Radar Χαμηλό", html)
        self.assertNotIn("data-assign-for", html)  # a sales user never gets the Assign control
        self.assertNotIn("data-save-for", html)

    def test_the_sales_user_feed_contains_only_their_assignments(self):
        other_company = Company.objects.create(gemi_number="900400", name="Άλλη ΑΕ", incorporation_date=date(2026, 9, 1))
        snapshot(other_company, T0)
        other = self.live_opportunity(self.full_radar("Radar Άλλο"), company=other_company)
        self.assertEqual(get_authorized_opportunity_feed(self.maria_user, self.org).cards, ())
        self.assign(self.maria, row=other)
        page = get_authorized_opportunity_feed(self.maria_user, self.org)
        self.assertEqual([c.company_id for c in page.cards], [other_company.pk])
        self.assertEqual(len(get_opportunity_feed(self.org).cards), 2)  # the organization-wide feed is unchanged

    def test_an_assignment_in_one_organization_grants_nothing_in_another(self):
        add_organization_member(self.org_b, self.maria_user, "sales_user")
        b_row = self.live_opportunity(self.radar_b_foreign)
        self.assign(self.maria)
        self.assertRaises(OrganizationAccessDenied, get_authorized_opportunity, self.maria_user, self.org_b, b_row.pk)
        self.assertEqual(get_authorized_opportunity_feed(self.maria_user, self.org_b).cards, ())
        self.assertRaises(OrganizationAccessDenied, get_authorized_opportunity, self.maria_user, self.org_b, self.row.pk)

    def test_deleting_the_membership_clears_the_assignment_and_re_adding_does_not_revive_it(self):
        moment = datetime(2026, 9, 18, 10, tzinfo=dt_timezone.utc)
        with patch("django.utils.timezone.now", return_value=moment):
            self.assign(self.maria)
        self.maria.delete()
        row = Opportunity.objects.get(pk=self.row.pk)
        self.assertEqual((row.assigned_to_id, row.assigned_at, row.status), (None, moment, "assigned"))
        self.assertRaises(OrganizationAccessDenied, get_authorized_opportunity, self.maria_user, self.org, self.row.pk)
        add_organization_member(self.org, self.maria_user, "sales_user")  # a new membership row
        self.assertRaises(OrganizationAccessDenied, get_authorized_opportunity, self.maria_user, self.org, self.row.pk)
        self.assertEqual(get_authorized_opportunity_feed(self.maria_user, self.org).cards, ())
        # The opportunity is intact and can be assigned again.
        new_membership = OrganizationMember.objects.get(organization=self.org, user=self.maria_user)
        self.assertTrue(self.assign(new_membership).changed)


# --- page, privacy, efficiency, parity ---------------------------------------------------------------

class PageAndSafetyTests(AssignTestCase):
    def test_managers_get_a_per_row_assign_control_listing_only_this_organizations_sales_users(self):
        add_organization_member(self.org_b, User.objects.create_user("b-seller@example.com", "b-seller@example.com",
                                                                      "x", first_name="Ξένος"), "sales_user")
        for role in ("owner", "sales_manager"):
            html = self.get(self.members[role]).content.decode()
            self.assertIn(f'data-assign-for="{self.row.pk}"', html, role)
            self.assertIn(reverse("organization_assign_opportunity", args=[self.org.pk, self.row.pk]), html)
            self.assertIn(f'<option value="{self.maria.pk}"', html)
            self.assertIn(f'<option value="{self.nikos.pk}"', html)
            assign_form = html.split(f'data-assign-for="{self.row.pk}"', 1)[1].split("</form>", 1)[0]
            # no owner/admin/manager/viewer, no tenant B (D32's status select is a separate form)
            self.assertEqual(assign_form.count("<option value="), 2, role)
            self.assertNotIn("Ξένος", html)
        for role in ("admin", "viewer"):
            self.assertNotIn("data-assign-for", self.get(self.members[role]).content.decode(), role)

    def test_the_assignment_is_shown_by_name_never_by_email(self):
        with patch("django.utils.timezone.now", return_value=datetime(2026, 9, 18, 7, 5, tzinfo=dt_timezone.utc)):
            self.assign(self.maria)
        others = (MARIA_EMAIL, "sales_user@a.example.com", "owner@example.com", "viewer@a.example.com")
        for who in (self.owner, self.maria_user, self.members["viewer"]):
            html = self.get(who).content.decode()
            self.assertIn(f'data-assigned-for="{self.row.pk}"', html)
            self.assertIn("Μαρία Παππά", html)
            self.assertIn("18/09/2026", html)
            # The site header already shows the viewer their *own* account; nobody else's address may appear.
            for secret in (email for email in others if email != who.email):
                self.assertNotIn(secret, html, (who.email, secret))
        # A member without a name is shown by membership number, not username (usernames are emails here).
        self.assign(self.nikos)
        self.assertIn(f"Πωλητής #{self.nikos.pk}", self.get(self.owner).content.decode())

    def test_later_states_show_the_assignment_but_no_control(self):
        self.assign(self.maria)
        for state in LATER_THAN_ASSIGNED:
            Opportunity.objects.filter(pk=self.row.pk).update(status=state)
            html = self.get(self.owner).content.decode()
            self.assertIn(f'data-assigned-for="{self.row.pk}"', html, state)
            self.assertNotIn(f'data-assign-for="{self.row.pk}"', html, state)

    def test_assignment_uses_a_bounded_number_of_queries(self):
        for index in range(4):
            self.live_opportunity(self.full_radar(f"Sibling {index}"))
        counts = {}
        for label, member in (("first", self.maria), ("reassign", self.nikos), ("noop", self.nikos)):
            with CaptureQueriesContext(connection) as queries:
                self.assign(member)
            counts[label] = [q["sql"].split()[0].upper() for q in queries.captured_queries]
        self.assertEqual(counts["first"].count("UPDATE"), 1)
        self.assertEqual(counts["reassign"].count("UPDATE"), 1)
        self.assertEqual(counts["noop"].count("UPDATE"), 0)
        # org, membership, row, assignee; a real (re)assignment also re-validates the actor (D36 audit)
        self.assertEqual([c.count("SELECT") for c in counts.values()], [5, 5, 4])
        with CaptureQueriesContext(connection) as page:
            self.page()
        self.assertLessEqual(len(page), 19)  # D33 notes, D34 tasks, D35 suppression, D37 unread count

    def test_the_legacy_product_billing_and_phone_are_untouched(self):
        legacy_user = entitled_user("legacy-d31@example.com")
        legacy = radar_for(legacy_user, "legacy", prefectures=["ΑΤΤΙΚΗΣ"])
        run_at = datetime(2026, 9, 16, 9, tzinfo=dt_timezone.utc)
        CompanyMonitoring.objects.create(
            company=self.company, state="active", priority="high", primary_reason="active_radar_match",
            policy_version=1, monitored_since=run_at - timedelta(days=3), next_check_at=run_at - timedelta(hours=1))
        policy = RefreshPolicy(max_requests_per_run=5, max_pages_per_query=2, max_direct_details_per_run=2, page_size=2)

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
                list(OrganizationMember.objects.values()), list(Company.objects.values()),
            )

        with patch("django.core.signing.TimestampSigner.timestamp", return_value=TimestampSigner().timestamp()), \
                patch("gemiapp.ingestion.refresh.get_gemi_client") as client:
            before = world()
            self.assertEqual(self.post_assign(self.owner, self.maria).status_code, 302)
            self.assertEqual(world(), before)
        client.assert_not_called()
        mail.outbox = []
        self.assertEqual(self.post_assign(self.owner, self.nikos).status_code, 302)  # a real reassignment
        self.assertEqual(len(mail.outbox), 0)  # no notification e-mail (D37 owns notifications)
        self.assertEqual([p.tel_uri for p in Company(gemi_number="1", raw_data={"phone": "+30 2109990001"}).gemi_phones],
                         ["tel:+302109990001"])
        self.assertEqual([p.display for p in extract_company_contact_phones({"phone": "2109990001"})], ["2109990001"])
