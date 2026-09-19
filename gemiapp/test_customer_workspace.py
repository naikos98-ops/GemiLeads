"""Tests for the Gemi Leads 2.0 customer workspace: navigation, dashboard, opportunity list (with the Saved view),
open tasks and Radars -- the already-implemented organization features made reachable from the normal UI.

Every page is a read-only GET over ``organization_access``: explicit organization from the route, membership and
capability, the same SQL visibility scope (a sales user sees only their assignments) and LIVE-backed opportunities
only. Every refusal is the same 404; nothing crosses tenants. Users without a membership see no workspace at all.
"""

import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse

from .models import Opportunity, OpportunityTask, OrganizationRadar
from .opportunity_feed import FeedFilters, get_opportunity_feed
from .organization_access import (
    OrganizationAccessDenied, get_authorized_workspace_dashboard, get_authorized_workspace_opportunities,
    get_authorized_workspace_radars, get_authorized_workspace_tasks, get_workspace_navigation,
    mark_all_authorized_notifications_read,
)
from .company_signals import LIVE, SHADOW
from .organizations import add_organization_member, create_organization
from .test_gemi_company_activities import entitled_user
from .test_opportunity_tasks import TaskTestCase
from .test_organization_radar_matching import T0, make_company, snapshot

_numbers = iter(range(500_000, 900_000))


class WorkspaceTestCase(TaskTestCase):
    """Tenant A (self.org) with a member of every role, a LIVE opportunity (self.row) and Maria (sales user);
    tenant B (self.org_b) with its own LIVE opportunity for the same company."""

    def setUp(self):
        super().setUp()
        self.radar_b_full = self.radar(org=self.org_b, name="Radar Β του B", **self.everything())
        self.b_row = self.live_opportunity(self.radar_b_full)

    def new_company(self, *, org_radar=None, mode=LIVE):
        company = make_company(str(next(_numbers)))
        snapshot(company, T0)
        return company, self.live_opportunity(org_radar or self.row.radar, company=company, mode=mode)

    def ws_url(self, name, org=None, **query):
        path = reverse(name, args=[(org or self.org).pk])
        return path + ("?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else "")

    def html(self, who, name, org=None, **query):
        self.client.force_login(who)
        response = self.client.get(self.ws_url(name, org, **query))
        self.assertEqual(response.status_code, 200, (name, query))
        return response.content.decode()

    def status_of(self, who, name, org=None, **query):
        self.client.force_login(who)
        return self.client.get(self.ws_url(name, org, **query)).status_code

    def rail(self, html):
        match = re.search(r"<div class=\"product-rail-workspace\" data-rail-workspace>(.*?)</div>", html, re.S)
        return match.group(1) if match else ""


# --- navigation -------------------------------------------------------------------------------------------

class NavigationTests(WorkspaceTestCase):
    def test_a_user_without_a_membership_sees_no_workspace_at_all(self):
        legacy = entitled_user("legacy-workspace@example.com")
        self.client.force_login(legacy)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn("data-rail-workspace", html)
        self.assertNotIn("data-mobile-workspace", html)
        self.assertNotIn("/organizations/", html)
        self.assertEqual(get_workspace_navigation(legacy).workspaces, ())

    def test_staff_and_superusers_get_no_workspace_without_a_membership(self):
        root = User.objects.create_superuser("root-ws", "root-ws@example.com", "StrongPass123")
        self.assertEqual(get_workspace_navigation(root).workspaces, ())
        self.assertEqual(self.status_of(root, "organization_dashboard"), 404)

    def test_signed_out_visitors_run_no_membership_query(self):
        self.client.logout()
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("home"))
        self.assertFalse([q for q in queries.captured_queries if "organizationmember" in q["sql"].lower()])
        self.assertEqual(get_workspace_navigation(None).workspaces, ())

    def test_every_workspace_link_of_a_member_resolves_to_a_page_they_can_open(self):
        for role, user in self.members.items():
            self.client.force_login(user)
            rail = self.rail(self.client.get(reverse("dashboard")).content.decode())
            links = re.findall(r'href="([^"]+)"', rail)
            self.assertTrue(links, role)
            for link in links:
                self.assertTrue(link.startswith(f"/organizations/{self.org.pk}/"), (role, link))
                self.assertEqual(resolve(link.split("?")[0]).func.__module__, "gemiapp.organization_views")
                self.assertEqual(self.client.get(link).status_code, 200, (role, link))

    def test_the_links_follow_the_role(self):
        for role, user in self.members.items():
            self.client.force_login(user)
            rail = self.rail(self.client.get(reverse("dashboard")).content.decode())
            self.assertEqual(self.ws_url("organization_radars") in rail, role != "sales_user", role)
            self.assertIn(self.ws_url("organization_notifications"), rail, role)
        # the hidden link is also a refused page, not merely a hidden one
        self.assertEqual(self.status_of(self.members["sales_user"], "organization_radars"), 404)

    def test_the_active_section_is_marked_on_the_organizations_own_pages_only(self):
        cases = {("organization_dashboard", ()): "Πίνακας", ("organization_opportunities", ()): "Ευκαιρίες",
                 ("organization_opportunities", (("status", "saved"),)): "Αποθηκευμένες",
                 ("organization_tasks", ()): "Εργασίες", ("organization_radars", ()): "Radars",
                 ("organization_notifications", ()): "Ειδοποιήσεις"}
        for (name, query), label in cases.items():
            html = self.html(self.owner, name, **dict(query))
            current = re.findall(r'aria-current="page">([^<]+)</a>', html)
            self.assertIn(label, current, name)
            self.assertNotIn("Signals", current)
            self.assertNotRegex(html, r'href="/radars/" aria-current')  # the legacy Radars entry stays unselected
        self.client.force_login(self.owner)
        html = self.client.get(reverse("organization_company_opportunity",
                                       args=[self.org.pk, self.company.pk])).content.decode()
        self.assertIn('aria-current="page">Ευκαιρίες</a>', html)  # D29 belongs to Opportunities
        legacy = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn('aria-current="page">Πίνακας', legacy)  # nothing of the workspace is "current" there

    def test_the_organization_context_is_shown_by_name_and_role(self):
        html = self.html(self.members["sales_manager"], "organization_dashboard")
        bar = html.split('class="product-workspace-bar"', 1)[1].split("</div>", 1)[0]
        self.assertIn(self.org.name, bar)
        self.assertIn("Διευθυντής πωλήσεων", bar)
        self.assertNotIn(self.org_b.name, html)

    def test_several_memberships_are_listed_not_guessed(self):
        add_organization_member(self.org_b, self.owner, "viewer")
        navigation = get_workspace_navigation(self.owner)
        self.assertEqual({w.organization_id for w in navigation.workspaces}, {self.org.pk, self.org_b.pk})
        self.assertIsNone(navigation.current)
        self.client.force_login(self.owner)
        legacy_rail = self.rail(self.client.get(reverse("dashboard")).content.decode())
        self.assertEqual(sorted(map(int, re.findall(r'data-workspace-switch="(\d+)"', legacy_rail))),
                         sorted([self.org.pk, self.org_b.pk]))
        on_a = get_workspace_navigation(self.owner, self.org.pk)
        self.assertEqual((on_a.current.organization_id, on_a.current_is_route), (self.org.pk, True))
        self.assertEqual(get_workspace_navigation(self.owner, self.org_b.pk).current.role_label, "Μόνο ανάγνωση")
        # a route naming an organization the user is not in never becomes "current"
        self.assertFalse(get_workspace_navigation(self.members["viewer"], self.org_b.pk).current_is_route)

    def test_the_mobile_navigation_offers_the_workspace_to_members(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("dashboard")).content.decode()
        mobile = html.split('class="product-mobile-nav"', 1)[1]
        self.assertIn(f'href="{self.ws_url("organization_dashboard")}" data-mobile-workspace', mobile)
        on_page = self.html(self.owner, "organization_tasks").split('class="product-mobile-nav"', 1)[1]
        self.assertRegex(on_page, r'data-mobile-workspace aria-current="page"')


# --- dashboard ----------------------------------------------------------------------------------------------

class DashboardTests(WorkspaceTestCase):
    def test_every_member_can_open_it_and_everyone_else_gets_the_same_404(self):
        for role, user in self.members.items():
            self.assertEqual(self.status_of(user, "organization_dashboard"), 200, role)
        outsider = User.objects.create_user("out-ws@example.com", "out-ws@example.com", "x")
        for who, org in ((outsider, self.org), (self.owner, self.org_b), (self.b_owner, self.org)):
            self.assertEqual(self.status_of(who, "organization_dashboard", org), 404)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/organizations/999999/").status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.ws_url("organization_dashboard")).status_code, 302)

    def test_the_counts_are_this_organizations_live_data_only(self):
        _, saved_row = self.new_company()
        Opportunity.objects.filter(pk=saved_row.pk).update(status="saved")
        _, won_row = self.new_company()
        Opportunity.objects.filter(pk=won_row.pk).update(status="won")
        self.new_company(mode=SHADOW)                        # never reaches a customer
        b_company = make_company("777000")
        snapshot(b_company, T0)
        self.live_opportunity(self.radar_b_full, company=b_company)  # tenant B
        open_task = self.task("Ανοιχτή").task_id
        OpportunityTask.objects.filter(pk=self.task("Εκπρόθεσμη").task_id).update(due_on=self.today() - timedelta(days=2))
        self.complete(self.task("Ολοκληρωμένη").task_id)
        self.task("Άλλου οργανισμού", row=self.b_row, actor=self.b_owner, org=self.org_b)
        dashboard = get_authorized_workspace_dashboard(self.owner, self.org.pk)
        self.assertEqual((dashboard.active_opportunities, dashboard.new_opportunities, dashboard.saved_opportunities),
                         (2, 1, 1))                      # self.row (new) + saved; the WON company is not active
        self.assertEqual((dashboard.open_tasks, dashboard.overdue_tasks), (2, 1))
        self.assertEqual([t.title for t in dashboard.attention_tasks], ["Εκπρόθεσμη", "Ανοιχτή"])
        self.assertTrue(dashboard.attention_tasks[0].overdue)
        self.assertEqual({row.company_id for row in dashboard.top_opportunities}, {self.company.pk, saved_row.company_id})
        radars = OrganizationRadar.objects.filter(organization=self.org)
        self.assertEqual((dashboard.radars, dashboard.active_radars), (radars.count(), radars.filter(active=True).count()))
        html = self.html(self.owner, "organization_dashboard")
        self.assertIn(f'data-task="{open_task}"', html)
        self.assertIn('data-count="active"><b><a href="%s" style="color: inherit;">2<' % self.ws_url("organization_opportunities"), html)
        self.assertNotIn("Άλλου οργανισμού", html)
        self.assertNotIn("777000", html)

    def test_a_sales_user_counts_only_their_assignments(self):
        _, other_row = self.new_company()
        self.task("Μόνο του διευθυντή", row=other_row)
        self.assertEqual(get_authorized_workspace_dashboard(self.maria_user, self.org.pk).active_opportunities, 0)
        self.put_assigned(self.maria)
        dashboard = get_authorized_workspace_dashboard(self.maria_user, self.org.pk)
        self.assertEqual((dashboard.active_opportunities, dashboard.open_tasks, dashboard.radars), (1, 0, None))
        self.assertEqual([row.company_id for row in dashboard.top_opportunities], [self.company.pk])
        self.assertNotIn("data-radar-summary", self.html(self.maria_user, "organization_dashboard"))

    def test_the_unread_count_and_recent_notifications_are_the_members_own(self):
        self.assign(self.maria)                          # D37: notifies Maria only
        self.assertEqual(get_authorized_workspace_dashboard(self.maria_user, self.org.pk).unread_notifications, 1)
        self.assertEqual(get_authorized_workspace_dashboard(self.owner, self.org.pk).unread_notifications, 0)
        html = self.html(self.maria_user, "organization_dashboard")
        self.assertIn('data-notification-state="unread"', html)
        mark_all_authorized_notifications_read(self.maria_user, self.org.pk)
        self.assertEqual(get_authorized_workspace_dashboard(self.maria_user, self.org.pk).unread_notifications, 0)

    def test_an_empty_organization_gets_empty_states_not_errors(self):
        founder = User.objects.create_user("founder@c.example.com", "founder@c.example.com", "x")
        empty = create_organization(owner=founder, name="Νέος οργανισμός").organization
        dashboard = get_authorized_workspace_dashboard(founder, empty.pk)
        self.assertEqual((dashboard.active_opportunities, dashboard.saved_opportunities, dashboard.open_tasks,
                          dashboard.unread_notifications, dashboard.radars), (0, 0, 0, 0, 0))
        html = self.html(founder, "organization_dashboard", empty)
        for section in ("opportunities", "tasks", "notifications", "radars"):
            self.assertIn(f'data-empty="{section}"', html, section)
        for name, marker in (("organization_opportunities", "opportunities"), ("organization_tasks", "tasks"),
                             ("organization_radars", "radars"), ("organization_notifications", "data-no-notifications")):
            page = self.html(founder, name, empty)
            self.assertIn(marker if marker.startswith("data-") else f'data-empty="{marker}"', page, name)
        self.assertIn('data-empty="saved"', self.html(founder, "organization_opportunities", empty, status="saved"))


# --- opportunities ------------------------------------------------------------------------------------------

class OpportunityListTests(WorkspaceTestCase):
    def test_only_this_organizations_live_cards_are_listed_and_link_to_their_page(self):
        live_company, _ = self.new_company()
        shadow_company, _ = self.new_company(mode=SHADOW)
        b_only = make_company("778000")
        snapshot(b_only, T0)
        self.live_opportunity(self.radar_b_full, company=b_only)
        listing = get_authorized_workspace_opportunities(self.owner, self.org.pk)
        self.assertEqual({row.company_id for row in listing.rows}, {self.company.pk, live_company.pk})
        html = self.html(self.owner, "organization_opportunities")
        for company in (self.company, live_company):
            link = reverse("organization_company_opportunity", args=[self.org.pk, company.pk])
            self.assertIn(f'href="{link}"', html)
            self.assertEqual(self.client.get(link).status_code, 200)
        for hidden in (shadow_company.gemi_number, b_only.gemi_number, "Radar Β του B"):
            self.assertNotIn(hidden, html)

    def test_a_card_shows_score_class_reason_status_assignee_and_freshness(self):
        self.put_assigned(self.maria)
        row = get_authorized_workspace_opportunities(self.owner, self.org.pk).rows[0]
        stored = Opportunity.objects.select_related("latest_signal").get(pk=self.row.pk)
        self.assertEqual((row.company_name, row.score, row.status, row.status_label, row.assignee_name),
                         (self.company.name, stored.score, "assigned", "Ανατεθειμένη", "Μαρία Παππά"))
        self.assertEqual(row.latest_signal_detected_at, stored.latest_signal.detected_at)
        self.assertNotEqual(row.reason_label, "—")
        html = self.html(self.owner, "organization_opportunities")
        self.assertIn(f"{stored.score}/100 · {row.score_class_label}", html)
        self.assertIn("Υπεύθυνος: Μαρία Παππά", html)

    def test_the_primary_is_chosen_among_live_rows_only(self):
        # A SHADOW-backed higher score on another Radar must neither become the card nor leak its score.
        shadow_radar = self.full_radar("Radar σκιάς")
        self.live_opportunity(shadow_radar, mode=SHADOW)
        unrestricted = get_opportunity_feed(self.org, FeedFilters())  # C9 without the mode filter: unchanged
        self.assertEqual(unrestricted.cards[0].opportunity_count, 2)
        live_only = get_opportunity_feed(self.org, FeedFilters(), latest_signal_mode=LIVE)
        self.assertEqual([c.primary_opportunity_id for c in live_only.cards], [self.row.pk])
        rows = get_authorized_workspace_opportunities(self.owner, self.org.pk).rows
        self.assertEqual([(r.company_id, r.radar_name, r.other_radar_count) for r in rows],
                         [(self.company.pk, self.row.radar.name, 0)])

    def test_the_saved_view_and_the_status_filter(self):
        _, saved_row = self.new_company()
        Opportunity.objects.filter(pk=saved_row.pk).update(status="saved")
        saved = get_authorized_workspace_opportunities(self.owner, self.org.pk, view="saved")
        self.assertEqual(([r.company_id for r in saved.rows], saved.view_label), ([saved_row.company_id], "Αποθηκευμένη"))
        html = self.html(self.owner, "organization_opportunities", status="saved")
        self.assertIn("Αποθηκευμένες ευκαιρίες", html)
        self.assertNotIn(f'data-opportunity-company="{self.company.pk}"', html)
        Opportunity.objects.filter(pk=self.row.pk).update(status="lost")
        self.assertEqual([r.company_id for r in get_authorized_workspace_opportunities(self.owner, self.org.pk).rows],
                         [saved_row.company_id])       # default: active only
        self.assertEqual(len(get_authorized_workspace_opportunities(self.owner, self.org.pk, view="all").rows), 2)
        self.assertEqual(get_authorized_workspace_opportunities(self.owner, self.org.pk, view="nonsense").view, "active")

    def test_a_sales_user_lists_only_their_assignments(self):
        self.new_company()
        self.assertEqual(get_authorized_workspace_opportunities(self.maria_user, self.org.pk).rows, ())
        self.put_assigned(self.maria)
        listing = get_authorized_workspace_opportunities(self.maria_user, self.org.pk)
        self.assertEqual(([r.company_id for r in listing.rows], listing.radar_options), ([self.company.pk], ()))

    def test_the_radar_filter_accepts_only_this_organizations_radars(self):
        other_radar = self.full_radar("Radar Γ")
        other_company, _ = self.new_company(org_radar=other_radar)
        listing = get_authorized_workspace_opportunities(self.owner, self.org.pk, radar_id=other_radar.pk)
        self.assertEqual([r.company_id for r in listing.rows], [other_company.pk])
        self.assertIn((other_radar.pk, "Radar Γ"), listing.radar_options)
        self.denied(get_authorized_workspace_opportunities, self.owner, self.org.pk, radar_id=self.radar_b_full.pk)
        self.denied(get_authorized_workspace_opportunities, self.maria_user, self.org.pk, radar_id=other_radar.pk)
        for bad in (str(self.radar_b_full.pk), "abc", "-1", "²"):
            self.assertEqual(self.status_of(self.owner, "organization_opportunities", radar=bad), 404, bad)
        self.assertEqual(self.status_of(self.owner, "organization_opportunities", cursor="not-a-cursor"), 404)

    def test_other_tenants_cannot_open_the_list(self):
        self.assertEqual(self.status_of(self.b_owner, "organization_opportunities"), 404)
        self.assertEqual(self.status_of(self.owner, "organization_opportunities", self.org_b), 404)
        self.denied(get_authorized_workspace_opportunities, self.b_owner, self.org.pk)


# --- tasks and radars ------------------------------------------------------------------------------------------

class TaskListTests(WorkspaceTestCase):
    def test_open_tasks_of_visible_live_opportunities_only(self):
        mine = self.task("Κλήση").task_id
        self.complete(self.task("Έγινε").task_id)
        _, shadow_row = self.new_company(mode=SHADOW)
        OpportunityTask.objects.create(organization=self.org, opportunity=shadow_row, title="Σκιά",
                                       due_on=self.today(), created_by=self.membership(self.owner))
        self.task("Του B", row=self.b_row, actor=self.b_owner, org=self.org_b)
        listing = get_authorized_workspace_tasks(self.owner, self.org.pk)
        self.assertEqual([t.task_id for t in listing.tasks], [mine])
        html = self.html(self.owner, "organization_tasks")
        self.assertIn(f'data-task="{mine}"', html)
        for hidden in ("Έγινε", "Σκιά", "Του B"):
            self.assertNotIn(hidden, html)
        self.assertIn(reverse("organization_company_opportunity", args=[self.org.pk, self.company.pk]), html)

    def test_overdue_and_today_are_marked(self):
        overdue = self.task("Παλιά").task_id
        OpportunityTask.objects.filter(pk=overdue).update(due_on=self.today() - timedelta(days=1))
        today = self.task("Σημερινή", due=self.today().isoformat()).task_id
        tasks = {t.task_id: t for t in get_authorized_workspace_tasks(self.owner, self.org.pk).tasks}
        self.assertEqual((tasks[overdue].overdue, tasks[today].overdue, tasks[today].due_today), (True, False, True))
        html = self.html(self.owner, "organization_tasks")
        self.assertIn(f'data-task="{overdue}" data-task-overdue', html)
        self.assertIn("ΕΚΠΡΟΘΕΣΜΗ", html)

    def test_a_sales_user_sees_the_tasks_of_their_own_opportunities(self):
        _, other_row = self.new_company()
        self.task("Σε ξένη ευκαιρία", row=other_row)
        self.put_assigned(self.maria)
        managers = self.task("Του διευθυντή στη δική της").task_id
        self.assertEqual([t.task_id for t in get_authorized_workspace_tasks(self.maria_user, self.org.pk).tasks],
                         [managers])


class RadarListTests(WorkspaceTestCase):
    def test_the_organizations_radars_with_their_criteria_and_found_companies(self):
        idle = self.radar(name="Αδρανές", active=False, signal_types=("new_company",))
        listing = get_authorized_workspace_radars(self.members["viewer"], self.org.pk)
        by_id = {r.radar_id: r for r in listing.radars}
        self.assertNotIn(self.radar_b_full.pk, by_id)
        full = by_id[self.row.radar_id]
        self.assertEqual((full.kads, full.regions, full.legal_forms, full.signal_types, full.opportunities),
                         (1, 1, 1, 1, 1))
        self.assertEqual((by_id[idle.pk].active, by_id[idle.pk].opportunities), (False, 0))
        html = self.html(self.members["viewer"], "organization_radars")
        self.assertIn("Αδρανές", html)
        self.assertNotIn("Radar Β του B", html)
        self.assertIn(self.ws_url("organization_opportunities") + f"?status=all&amp;radar={full.radar_id}", html)
        self.assertNotIn("<form", html.split('id="radars"', 1)[1])  # read-only: no editing is offered

    def test_roles_without_radar_access_and_other_tenants_get_404(self):
        self.denied(get_authorized_workspace_radars, self.maria_user, self.org.pk)
        self.assertEqual(self.status_of(self.b_owner, "organization_radars"), 404)


# --- safety ---------------------------------------------------------------------------------------------------

class SafetyTests(WorkspaceTestCase):
    NAMES = ("organization_dashboard", "organization_opportunities", "organization_tasks", "organization_radars")

    def test_the_pages_are_get_only_and_write_nothing(self):
        self.task("Κλήση")
        self.assign(self.maria)
        self.client.force_login(self.owner)
        for name in self.NAMES:
            self.assertEqual(self.client.post(self.ws_url(name)).status_code, 405, name)
        before = (list(Opportunity.objects.values()), list(OpportunityTask.objects.values()))
        with CaptureQueriesContext(connection) as queries:
            for name in self.NAMES:
                self.client.get(self.ws_url(name))
        writes = [q["sql"] for q in queries.captured_queries
                  if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                  and "django_session" not in q["sql"]]
        self.assertEqual(writes, [])
        self.assertEqual((list(Opportunity.objects.values()), list(OpportunityTask.objects.values())), before)

    def test_the_dashboard_is_read_with_a_bounded_number_of_queries(self):
        with CaptureQueriesContext(connection) as one:
            get_authorized_workspace_dashboard(self.owner, self.org.pk)
        for _ in range(6):
            _, row = self.new_company()
            self.task("Κλήση", row=row)
        with CaptureQueriesContext(connection) as many:
            dashboard = get_authorized_workspace_dashboard(self.owner, self.org.pk)
        self.assertEqual(len(dashboard.top_opportunities), 5)
        self.assertEqual(len(many), len(one))
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in many.captured_queries))

    def test_every_refusal_is_the_single_denial(self):
        for call in (get_authorized_workspace_dashboard, get_authorized_workspace_opportunities,
                     get_authorized_workspace_tasks, get_authorized_workspace_radars):
            for who, org in ((self.b_owner, self.org.pk), (self.owner, self.org_b.pk), (self.owner, 999_999),
                             (self.owner, str(self.org.pk)), (None, self.org.pk)):
                self.denied(call, who, org)

    def test_no_contact_person_street_or_vat_reaches_the_workspace_pages(self):
        from .test_organization_radar_matching import ADDRESS_SENTINEL
        from .test_gemi_validation import CONTACT_SENTINEL, PHONE_SENTINEL

        self.task("Κλήση")
        for name in self.NAMES:
            html = self.html(self.owner, name)
            for secret in (CONTACT_SENTINEL, PHONE_SENTINEL, ADDRESS_SENTINEL, self.company.vat_number):
                self.assertNotIn(secret, html, (name, secret))

    def test_the_legacy_navigation_is_unchanged_for_legacy_users(self):
        legacy = entitled_user("legacy-nav@example.com")
        self.client.force_login(legacy)
        html = self.client.get(reverse("radar_list")).content.decode()
        self.assertEqual(re.findall(r'<a href="(/[a-z]*/)" aria-current="page"><span>', html), ["/radars/"])
        mobile = html.split('class="product-mobile-nav"', 1)[1].split("</nav>", 1)[0]
        self.assertEqual(re.findall(r'href="([^"]+)"', mobile), ["/dashboard/", "/radars/", "/leads/", "/settings/"])
