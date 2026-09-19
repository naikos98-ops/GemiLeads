"""Tests for D32 Statuses (blueprint §39, Phase D item 32).

D32 owns only the general sales statuses CONTACTED, INTERESTED, FOLLOW_UP, WON, LOST and NOT_RELEVANT. SAVED stays
D30's action, ASSIGNED D31's, DO_NOT_CONTACT is reserved for item 35, and NEW/VIEWED are never targets. §39 defines
no graph, so any non-terminal opportunity may move to any D32 target; WON, LOST, NOT_RELEVANT and DO_NOT_CONTACT are
final here. OWNER and SALES_MANAGER may change any visible LIVE opportunity; a SALES_USER only one assigned to their
own membership. The same status again is a no-write no-op, and assignment survives every change.
"""

import inspect
import re
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
    Company, CompanyMonitoring, CompanySignal, CompanySnapshot, CustomerRadar, DigestDelivery, Opportunity,
    OrganizationMember, OutreachSuppression, PersonSuppression, RadarMatch, UserCompanyLead, UserSubscription,
)
from .opportunity_feed import FeedFilters, get_opportunity_feed
from .organization_access import (
    STATUS_SOURCES, STATUS_TARGETS, TERMINAL_STATUSES, AssignmentRefused, OpportunityTransitionRefused,
    OrganizationAccessDenied, StatusChangeRefused, get_authorized_opportunity_feed,
    get_authorized_opportunity_score_breakdown, save_authorized_opportunity, set_authorized_opportunity_status,
)
from .organizations import add_organization_member
from .services import company_matches_radar, eligible_radars, send_digests
from .test_gemi_company_activities import entitled_user, radar_for
from .test_opportunity_assignment import MARIA_EMAIL, AssignTestCase
from .test_organization_radar_matching import T0, snapshot

MOMENT = datetime(2026, 9, 18, 10, tzinfo=dt_timezone.utc)
SPECIALISED = ("new", "viewed", "saved", "assigned", "do_not_contact")
ACTIVE_TARGETS = ("contacted", "interested", "follow_up")


class StatusTestCase(AssignTestCase):
    def change(self, target, row=None, actor=None):
        return set_authorized_opportunity_status(actor or self.owner, self.org.pk, (row or self.row).pk, target)

    def status_url(self, row=None, org=None):
        return reverse("organization_opportunity_status",
                       kwargs={"organization_id": (org or self.org).pk, "opportunity_id": (row or self.row).pk})

    def post_status(self, who, target, row=None, org=None, **extra):
        self.client.force_login(who)
        data = {} if target is None else {"status": target}
        return self.client.post(self.status_url(row, org), data, **extra)

    def put_assigned(self, member, row=None):
        """ASSIGNED to ``member`` at a fixed moment, set directly: D31's own tests own the assign path."""
        Opportunity.objects.filter(pk=(row or self.row).pk).update(status="assigned", assigned_to=member,
                                                                     assigned_at=MOMENT)

    def writes(self, queries):
        return [q["sql"] for q in queries.captured_queries
                # savepoint bookkeeping (a refusal rolls its savepoint back) is not a data write
                if not q["sql"].lstrip().upper().startswith(("SELECT", "SAVEPOINT", "RELEASE", "ROLLBACK TO SAVEPOINT"))]

    def status_form(self, html, row=None):
        match = re.search(r'data-status-for="%d".*?</form>' % (row or self.row).pk, html, re.S)
        return match.group(0) if match else None


# --- the product contract ----------------------------------------------------------------------------

class ContractTests(StatusTestCase):
    def test_the_targets_sources_and_terminals_are_exactly_the_v1_contract(self):
        self.assertEqual(STATUS_TARGETS, ("contacted", "interested", "follow_up", "won", "lost", "not_relevant"))
        self.assertEqual(STATUS_SOURCES, frozenset({"new", "viewed", "saved", "assigned", "contacted", "interested",
                                                    "follow_up"}))
        self.assertEqual(TERMINAL_STATUSES, frozenset({"won", "lost", "not_relevant", "do_not_contact"}))
        vocabulary = {value for value, _ in Opportunity.STATUSES}
        self.assertEqual(STATUS_SOURCES | TERMINAL_STATUSES, vocabulary)  # every §39 state is one or the other
        self.assertFalse(set(STATUS_TARGETS) & set(SPECIALISED))
        self.assertEqual(set(STATUS_TARGETS) | set(SPECIALISED), vocabulary)

    def test_every_target_from_assigned_individually_with_one_status_write(self):
        for target in STATUS_TARGETS:
            self.put_assigned(self.maria)
            before = self.stored()
            with CaptureQueriesContext(connection) as queries:
                result = self.change(target)
            after = self.stored()
            self.assertEqual((result.previous_status, result.current_status, result.changed, after["status"]),
                             ("assigned", target, True, target), target)
            self.assertEqual((result.opportunity_id, result.company_id, result.organization_id),
                             (self.row.pk, self.company.pk, self.org.pk))
            updates = self.writes(queries)
            self.assertEqual(len(updates), 1, target)
            self.assertIn('"status"', updates[0])
            self.assertNotIn('"assigned_to_id"', updates[0])
            self.assertGreaterEqual(after["updated_at"], before["updated_at"])
            self.assertEqual({k: v for k, v in after.items() if k not in ("status", "updated_at")},
                             {k: v for k, v in before.items() if k not in ("status", "updated_at")}, target)

    def test_there_is_no_linear_funnel(self):
        for start, target in (("contacted", "follow_up"), ("follow_up", "contacted"), ("interested", "won"),
                              ("contacted", "interested"), ("interested", "contacted"), ("follow_up", "lost"),
                              ("contacted", "not_relevant")):
            self.set_status(start)
            self.assertTrue(self.change(target).changed, (start, target))
            self.assertEqual(self.status(), target, (start, target))

    def test_a_manager_may_settle_an_unassigned_new_opportunity(self):
        for actor, target in ((self.members["sales_manager"], "not_relevant"), (self.owner, "won")):
            self.set_status("new")
            result = self.change(target, actor=actor)
            self.assertEqual((result.previous_status, result.current_status), ("new", target))
            self.assertEqual((self.stored()["assigned_to_id"], self.stored()["assigned_at"]), (None, None))

    def test_saved_and_viewed_rows_move_through_d32(self):
        for start, target in (("saved", "contacted"), ("saved", "not_relevant"), ("viewed", "lost"),
                              ("viewed", "interested")):
            self.set_status(start)
            self.assertEqual(self.change(target).current_status, target, (start, target))

    def test_the_same_status_again_is_a_no_op_without_any_write(self):
        for target in ACTIVE_TARGETS:
            self.set_status(target)
            before = self.stored()
            with CaptureQueriesContext(connection) as queries:
                result = self.change(target)
            self.assertEqual((result.previous_status, result.current_status, result.changed), (target, target, False))
            self.assertEqual(self.writes(queries), [], target)
            self.assertEqual(self.stored(), before, target)  # updated_at included
            response = self.post_status(self.owner, target, follow=True)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("ενημερώθηκε", response.content.decode())  # nothing changed, nothing announced
            self.assertEqual(self.stored(), before, target)

    def test_terminal_states_refuse_every_change_including_the_same_status(self):
        for terminal in sorted(TERMINAL_STATUSES):
            self.set_status(terminal)
            before = self.stored()
            for target in STATUS_TARGETS:
                with CaptureQueriesContext(connection) as queries, self.assertRaises(StatusChangeRefused) as refused:
                    self.change(target)
                self.assertEqual(refused.exception.reason, "terminal", (terminal, target))
                self.assertEqual((refused.exception.result.current_status, refused.exception.result.changed),
                                 (terminal, False))
                self.assertEqual(self.writes(queries), [])
            response = self.post_status(self.owner, "contacted", follow=True)
            self.assertIn(StatusChangeRefused.MESSAGES["terminal"], response.content.decode())
            self.assertEqual(self.stored(), before, terminal)

    def test_specialised_and_junk_targets_are_refused_without_a_write(self):
        self.put_assigned(self.maria)
        before = self.stored()
        for target in (*SPECIALISED, "", None, "CONTACTED", " contacted", "contacted ", "reopen", "1", True, 1,
                       ["contacted"], {"status": "won"}):
            with CaptureQueriesContext(connection) as queries, self.assertRaises(StatusChangeRefused) as refused:
                self.change(target)
            self.assertEqual(refused.exception.reason, "target", repr(target))
            self.assertEqual(self.writes(queries), [], repr(target))
        for target in (*SPECIALISED, "", "unknown", None):
            response = self.post_status(self.owner, target)
            self.assertEqual(response.status_code, 302, target)  # a form error on an authorized row, not a 404
        self.assertEqual(self.stored(), before)
        self.assertEqual(len(set(StatusChangeRefused.MESSAGES.values())), 2)


# --- who may change what ----------------------------------------------------------------------------

class AccessTests(StatusTestCase):
    def test_the_role_matrix_on_an_unassigned_row(self):
        for role, allowed in (("owner", True), ("sales_manager", True), ("admin", False), ("viewer", False),
                              ("sales_user", False)):
            self.set_status("new")
            response = self.post_status(self.members[role], "contacted")
            self.assertEqual(response.status_code, 302 if allowed else 404, role)
            self.assertEqual(self.status(), "contacted" if allowed else "new", role)

    def test_a_sales_user_changes_only_their_own_assignment(self):
        theirs = self.live_opportunity(self.full_radar("Radar Νίκου", legal_forms=()))
        unassigned = self.live_opportunity(self.full_radar("Radar κανενός", signal_types=()))
        self.put_assigned(self.maria)
        self.put_assigned(self.nikos, row=theirs)
        before_theirs, before_unassigned = self.stored(theirs), self.stored(unassigned)
        self.assertEqual(self.post_status(self.maria_user, "contacted").status_code, 302)
        self.assertEqual(self.status(), "contacted")
        for row in (theirs, unassigned):
            # an invalid target changes nothing about the answer: access is decided first
            for target in ("won", "saved", ""):
                self.assertEqual(self.post_status(self.maria_user, target, row=row).status_code, 404)
            self.assertRaises(OrganizationAccessDenied, self.change, "won", row, self.maria_user)
        self.assertEqual((self.stored(theirs), self.stored(unassigned)), (before_theirs, before_unassigned))
        self.assertEqual(self.post_status(self.members["sales_user"], "lost").status_code, 404)  # Nikos on Maria's
        self.assertEqual(self.status(), "contacted")

    def test_the_scope_is_the_exact_membership_never_the_user(self):
        self.put_assigned(self.maria)
        # Maria also owns another organization: nothing of it reaches this row, through either route.
        add_organization_member(self.org_b, self.maria_user, "owner")
        self.assertEqual(self.post_status(self.maria_user, "won", org=self.org_b).status_code, 404)
        # Demoted to viewer here, the old assignment grants her nothing.
        OrganizationMember.objects.filter(pk=self.maria.pk).update(role="viewer")
        self.assertEqual(self.post_status(self.maria_user, "won").status_code, 404)
        # Membership gone and re-added: a new membership, not the assignee.
        OrganizationMember.objects.filter(pk=self.maria.pk).delete()
        add_organization_member(self.org, self.maria_user, "sales_user")
        self.assertEqual(self.post_status(self.maria_user, "won").status_code, 404)
        self.assertEqual(self.status(), "assigned")

    def test_shadow_foreign_missing_and_non_member_are_the_same_404(self):
        shadow_company = Company.objects.create(gemi_number="900400", name="Σκιώδης ΑΕ", incorporation_date=date(2026, 9, 1))
        snapshot(shadow_company, T0)
        shadow = self.live_opportunity(self.full_radar("Shadow"), company=shadow_company, mode=SHADOW)
        foreign = self.live_opportunity(self.radar_b_foreign, company=Company.objects.create(
            gemi_number="900500", name="Ξένη ΑΕ", incorporation_date=date(2026, 9, 1)))
        missing = Opportunity.objects.order_by("-pk").first().pk + 1
        outsider = User.objects.create_user("outsider@example.com", "outsider@example.com", "x")
        responses = [self.post_status(self.owner, "won", row=shadow),
                     self.post_status(self.owner, "won", row=foreign),
                     self.post_status(self.owner, "won", row=foreign, org=self.org_b),
                     self.post_status(self.b_owner, "won"),
                     self.post_status(outsider, "won")]
        self.client.force_login(self.owner)
        responses.append(self.client.post(f"/organizations/{self.org.pk}/opportunities/{missing}/status/",
                                          {"status": "won"}))
        responses.append(self.client.post(f"/organizations/999999/opportunities/{self.row.pk}/status/",
                                          {"status": "won"}))
        self.assertEqual({response.status_code for response in responses}, {404})
        self.assertEqual({response.content for response in responses[1:]}, {responses[0].content})  # no tell
        self.assertEqual((self.stored(shadow)["status"], self.stored(foreign)["status"], self.status()),
                         ("new", "new", "new"))

    def test_the_endpoint_is_post_only_login_only_csrf_protected_and_redirects_to_the_page(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.status_url()).status_code, 405)
        anonymous = Client().post(self.status_url(), {"status": "won"})
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn(reverse("login"), anonymous["Location"])
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.owner)
        self.assertEqual(strict.post(self.status_url(), {"status": "won"}).status_code, 403)
        self.assertEqual(self.status(), "new")
        response = self.client.post(self.status_url() + "?next=https://evil.example/",
                                    {"status": "contacted", "next": "https://evil.example/"})
        self.assertRedirects(response, self.url(), fetch_redirect_response=False)
        self.assertEqual(self.status(), "contacted")
        page = self.client.get(self.url())
        self.assertIn("Η κατάσταση της ευκαιρίας ενημερώθηκε.", page.content.decode())


# --- boundaries with D30, D31, D35 and the frozen capture ------------------------------------------

class BoundaryTests(StatusTestCase):
    def test_the_assignment_survives_the_whole_path(self):
        self.put_assigned(self.maria)
        for actor, target in ((self.maria_user, "contacted"), (self.maria_user, "interested"),
                              (self.members["sales_manager"], "follow_up"), (self.maria_user, "won")):
            self.change(target, actor=actor)
            stored = self.stored()
            self.assertEqual((stored["status"], stored["assigned_to_id"], stored["assigned_at"]),
                             (target, self.maria.pk, MOMENT), target)

    def test_save_and_assign_stay_specialised_after_a_status_change(self):
        self.put_assigned(self.maria)
        self.change("contacted")
        with self.assertRaises(AssignmentRefused) as refused:  # D31 still refuses every later state
            g5.assign_authorized_opportunity(self.owner, self.org.pk, self.row.pk, self.nikos.pk)
        self.assertEqual(refused.exception.reason, "state")
        with self.assertRaises(OpportunityTransitionRefused):   # D30 never moves back to SAVED
            save_authorized_opportunity(self.owner, self.org.pk, self.row.pk)
        self.assertEqual((self.status(), self.stored()["assigned_to_id"]), ("contacted", self.maria.pk))
        # and D32 cannot produce either state
        for target in ("saved", "assigned"):
            self.assertRaises(StatusChangeRefused, self.change, target)

    def test_do_not_contact_is_not_implemented(self):
        suppressions = (list(OutreachSuppression.objects.values()), list(PersonSuppression.objects.values()))
        for start in ("new", "assigned", "contacted"):
            self.set_status(start)
            self.assertEqual(self.post_status(self.owner, "do_not_contact").status_code, 302)
            self.assertEqual(self.status(), start)
        self.assertEqual((list(OutreachSuppression.objects.values()), list(PersonSuppression.objects.values())),
                         suppressions)
        html = self.get(self.owner).content.decode()
        self.assertNotIn('value="do_not_contact"', html)
        from django.apps import apps

        names = {model.__name__ for model in apps.get_app_config("gemiapp").get_models()}
        self.assertEqual({n for n in names if "Suppression" in n}, {"OutreachSuppression", "PersonSuppression"})
        self.assertFalse([n for n in names if "History" in n or "Notification" in n or "StatusChange" in n])
        # only the pre-existing KAD catalogue / company-activity models: no activity log (item 36)
        self.assertEqual({n for n in names if "Activity" in n}, {"ActivityCode", "ActivityCodeKadLink", "CompanyActivity"})
        self.assertEqual({n for n in names if "Audit" in n}, {"AdminAuditLog"})
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(max(name for app, name in loader.disk_migrations if app == "gemiapp"),
                         "0051_opportunity_task")  # D32 has no migration; 0050/0051 are D33 notes, D34 tasks

    def test_the_frozen_capture_signals_snapshots_timeline_and_ranking_are_untouched(self):
        self.put_assigned(self.maria)
        sibling = self.live_opportunity(self.full_radar("Radar Β", legal_forms=()))
        frozen, frozen_sibling = self.frozen(), self.frozen(sibling)
        sibling_row = self.stored(sibling)
        breakdown = get_authorized_opportunity_score_breakdown(self.owner, self.org, self.row.pk)
        signals, snapshots = list(CompanySignal.objects.values()), list(CompanySnapshot.objects.values())
        timeline = self.page().timeline
        order = [card.primary_opportunity_id for card in get_opportunity_feed(self.org).cards]
        for target in ("contacted", "won"):
            self.change(target)
        self.assertEqual((self.frozen(), self.frozen(sibling)), (frozen, frozen_sibling))
        self.assertEqual(self.stored(sibling), sibling_row)  # the sibling row, status included, is untouched
        self.assertEqual(get_authorized_opportunity_score_breakdown(self.owner, self.org, self.row.pk), breakdown)
        self.assertEqual((list(CompanySignal.objects.values()), list(CompanySnapshot.objects.values())),
                         (signals, snapshots))
        self.assertEqual(self.page().timeline, timeline)
        self.assertEqual([card.primary_opportunity_id for card in get_opportunity_feed(self.org).cards], order)

    def test_a_multi_radar_sales_user_changes_only_their_row(self):
        sibling = self.live_opportunity(self.full_radar("Radar Κρυφό", legal_forms=()))
        self.put_assigned(self.maria)
        self.put_assigned(self.nikos, row=sibling)
        before = self.stored(sibling)
        self.change("interested", actor=self.maria_user)
        self.assertEqual(self.stored(sibling), before)
        card, = get_authorized_opportunity_feed(self.maria_user, self.org).cards
        self.assertEqual(([c.opportunity_id for c in card.opportunities], card.status), ([self.row.pk], "interested"))

    def test_the_existing_feed_status_filter_sees_the_change_and_visibility_stays_the_assignment(self):
        self.put_assigned(self.maria)
        contacted = FeedFilters(statuses=("contacted",))
        self.assertEqual(get_opportunity_feed(self.org, contacted).cards, ())
        maria_before = get_authorized_opportunity_feed(self.maria_user, self.org).cards
        self.change("contacted", actor=self.maria_user)
        card, = get_opportunity_feed(self.org, contacted).cards
        self.assertEqual((card.primary_opportunity_id, card.status, card.score), (self.row.pk, "contacted", 100))
        mine, = get_authorized_opportunity_feed(self.maria_user, self.org, contacted).cards
        self.assertEqual(mine.primary_opportunity_id, self.row.pk)
        self.assertEqual([c.primary_opportunity_id for c in get_authorized_opportunity_feed(self.maria_user, self.org).cards],
                         [c.primary_opportunity_id for c in maria_before])
        self.assertEqual(get_authorized_opportunity_feed(self.members["sales_user"], self.org).cards, ())  # still Maria's


# --- the page, reads, efficiency, privacy, parity ---------------------------------------------------

class PageTests(StatusTestCase):
    def test_managers_get_a_per_row_control_with_only_d32_targets(self):
        sibling = self.live_opportunity(self.full_radar("Radar Β", legal_forms=()))
        self.set_status("contacted", sibling)
        for role in ("owner", "sales_manager"):
            html = self.get(self.members[role]).content.decode()
            self.assertIn(reverse("organization_opportunity_status", args=[self.org.pk, self.row.pk]), html)
            form = self.status_form(html)
            self.assertEqual(re.findall(r'<option value="([a-z_]+)"', form), list(STATUS_TARGETS), role)
            self.assertEqual(re.findall(r'<option value="([a-z_]+)"', self.status_form(html, sibling)),
                             [t for t in STATUS_TARGETS if t != "contacted"], role)  # rows are independent
            self.assertIn("Κερδήθηκε", form)  # Greek labels; the value stays the enum
        for role in ("admin", "viewer"):
            html = self.get(self.members[role]).content.decode()
            self.assertNotIn("data-status-for", html, role)
            self.assertNotIn("/status/", html, role)

    def test_an_assigned_sales_user_gets_the_status_control_and_nothing_else(self):
        self.put_assigned(self.maria)
        html = self.get(self.maria_user).content.decode()
        self.assertIsNotNone(self.status_form(html))
        for other in ("data-save-for", "data-assign-for", "/save/", "/assign/"):
            self.assertNotIn(other, html, other)
        self.assertEqual(self.get(self.members["sales_user"]).status_code, 404)  # Nikos: not his

    def test_terminal_rows_show_the_settled_status_only(self):
        labels = {"won": "Κερδήθηκε", "lost": "Χάθηκε", "not_relevant": "Μη σχετική",
                  "do_not_contact": "Χωρίς επικοινωνία"}
        for terminal, label in labels.items():
            self.set_status(terminal)
            html = self.get(self.owner).content.decode()
            self.assertIsNone(self.status_form(html), terminal)
            self.assertIn(label, html, terminal)

    def test_no_form_ever_offers_a_specialised_target(self):
        for start in sorted(STATUS_SOURCES):
            self.set_status(start)
            form = self.status_form(self.get(self.owner).content.decode())
            values = set(re.findall(r'<option value="([a-z_]+)"', form))
            self.assertFalse(values & set(SPECIALISED), start)
            self.assertNotIn(start, values)

    def test_reading_the_page_writes_nothing_and_never_marks_viewed(self):
        self.put_assigned(self.maria)
        self.set_status("new")
        for who in (self.owner, self.maria_user, self.members["viewer"]):
            self.client.force_login(who)
            with CaptureQueriesContext(connection) as queries:
                self.assertIn(self.client.get(self.url()).status_code, (200, 404))
            self.assertFalse([sql for sql in self.writes(queries) if "gemiapp_opportunity" in sql], who.email)
        self.assertEqual(self.status(), "new")

    def test_status_changes_use_a_bounded_number_of_queries(self):
        for index in range(4):
            self.live_opportunity(self.full_radar(f"Sibling {index}"))
        counts = {}
        for label, target in (("change", "contacted"), ("noop", "contacted"), ("terminal", None)):
            if target is None:
                self.set_status("lost")
            with CaptureQueriesContext(connection) as queries:
                try:
                    self.change(target or "won")
                except StatusChangeRefused:
                    pass
            counts[label] = [q["sql"].split()[0].upper() for q in queries.captured_queries]
        self.assertEqual([c.count("UPDATE") for c in counts.values()], [1, 0, 0])
        self.assertEqual([c.count("SELECT") for c in counts.values()], [3, 3, 3])  # organization, membership, row

    def test_refusals_and_the_page_leak_nothing(self):
        self.put_assigned(self.maria)
        self.set_status("won")
        response = self.post_status(self.maria_user, "lost", follow=True)
        html = response.content.decode()
        self.assertIn(StatusChangeRefused.MESSAGES["terminal"], html)
        for secret in ("owner@example.com", "sales_user@a.example.com", "viewer@a.example.com", "b-owner",
                       "raw_data", "2109990001"):
            self.assertNotIn(secret, html, secret)
        for message in StatusChangeRefused.MESSAGES.values():
            self.assertNotRegex(message, r"@|\d")
        source = inspect.getsource(set_authorized_opportunity_status)
        for forbidden in ("raw_data", "gemi_phones", "email", "persons", "send_mail", "set_opportunity_status",
                          "notify", "History", "AuditLog"):
            self.assertNotIn(forbidden, source, forbidden)
        self.assertIn("transaction.atomic()", source)
        self.assertIn('select_for_update(of=("self",))', source)

    def test_the_legacy_product_billing_and_phone_are_untouched(self):
        legacy_user = entitled_user("legacy-d32@example.com")
        legacy = radar_for(legacy_user, "legacy", prefectures=["ΑΤΤΙΚΗΣ"])
        run_at = datetime(2026, 9, 16, 9, tzinfo=dt_timezone.utc)
        CompanyMonitoring.objects.create(
            company=self.company, state="active", priority="high", primary_reason="active_radar_match",
            policy_version=1, monitored_since=run_at - timedelta(days=3), next_check_at=run_at - timedelta(hours=1))
        policy = RefreshPolicy(max_requests_per_run=5, max_pages_per_query=2, max_direct_details_per_run=2, page_size=2)
        self.put_assigned(self.maria)

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
            self.assertEqual(self.post_status(self.maria_user, "contacted").status_code, 302)
            self.assertEqual(world(), before)
        client.assert_not_called()
        mail.outbox = []
        self.assertEqual(self.post_status(self.owner, "won").status_code, 302)
        self.assertEqual((self.status(), len(mail.outbox)), ("won", 0))  # no notification (D37)
        self.assertNotIn(MARIA_EMAIL, str(Opportunity.objects.filter(pk=self.row.pk).values().get()))
        self.assertEqual([p.tel_uri for p in Company(gemi_number="1", raw_data={"phone": "+30 2109990001"}).gemi_phones],
                         ["tel:+302109990001"])
        self.assertEqual([p.display for p in extract_company_contact_phones({"phone": "2109990001"})], ["2109990001"])
