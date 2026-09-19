"""Tests for D34 Tasks (blueprint §42 «tasks», §48 TASK_DUE reserved for item 37, Phase D item 34).

A task is a title and a due date on one explicit C8 opportunity: create and complete only. Creator, assignee and
completer are memberships; a sales user may only hold a task on the opportunity assigned to them. Reading follows
the opportunity; OWNER and SALES_MANAGER manage every task, a SALES_USER creates on their own opportunity and
completes only their own tasks. Nothing else changes: no status, assignment, score, note, timeline, feed, audit or
notification.
"""

import inspect
import re
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import User
from django.core import mail
from django.core.signing import TimestampSigner
from django.db import IntegrityError, connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import get_resolver, reverse
from django.utils import timezone

from . import organization_access as g5
from . import organization_views
from .company_contact import extract_company_contact_phones
from .company_signals import SHADOW
from .ingestion.monitoring import radar_match_evidence
from .ingestion.refresh import RefreshPolicy, build_company_refresh_plan
from .models import (
    OPPORTUNITY_TASK_TITLE_MAX_LENGTH, Company, CompanyMonitoring, CompanySignal, CompanySnapshot, CustomerRadar,
    DigestDelivery, Opportunity, OpportunityNote, OpportunityTask, OrganizationMember, RadarMatch, UserCompanyLead,
    UserSubscription,
)
from .opportunity_feed import get_opportunity_feed
from .organization_access import (
    FORMER_MEMBER_LABEL, TASK_TITLE_MAX_LENGTH, TASK_UNASSIGNED_LABEL, TASKS_PAGE_LIMIT, TERMINAL_STATUSES,
    OrganizationAccessDenied, TaskRefused, complete_authorized_opportunity_task, create_authorized_opportunity_task,
    get_authorized_opportunity_score_breakdown, normalize_task_title, parse_task_due_on, task_is_overdue,
)
from .organizations import add_organization_member
from .services import company_matches_radar, eligible_radars, send_digests
from .test_gemi_company_activities import entitled_user, radar_for
from .test_opportunity_notes import NoteTestCase
from .test_organization_radar_matching import T0, snapshot

MOMENT = datetime(2026, 9, 18, 10, tzinfo=dt_timezone.utc)


class TaskTestCase(NoteTestCase):
    def today(self):
        return timezone.localdate()

    def tomorrow(self):
        return (self.today() + timedelta(days=1)).isoformat()

    def task(self, title="Κλήση αύριο", due=None, row=None, actor=None, org=None, assignee=None):
        return create_authorized_opportunity_task(actor or self.owner, (org or self.org).pk, (row or self.row).pk,
                                                  title, due if due is not None else self.tomorrow(), assignee)

    def complete(self, task_id, row=None, actor=None, org=None):
        return complete_authorized_opportunity_task(actor or self.owner, (org or self.org).pk, (row or self.row).pk,
                                                    task_id)

    def task_url(self, row=None, org=None):
        return reverse("organization_create_opportunity_task",
                       kwargs={"organization_id": (org or self.org).pk, "opportunity_id": (row or self.row).pk})

    def complete_url(self, task_id, row=None, org=None):
        return reverse("organization_complete_opportunity_task",
                       kwargs={"organization_id": (org or self.org).pk, "opportunity_id": (row or self.row).pk,
                               "task_id": task_id})

    def post_task(self, who, title="Κλήση αύριο", due=None, row=None, org=None, assignee=None, **extra):
        self.client.force_login(who)
        data = {"title": title, "due_on": due if due is not None else self.tomorrow()}
        if assignee is not None:
            data["assignee_membership_id"] = assignee
        return self.client.post(self.task_url(row, org), {k: v for k, v in data.items() if v is not None}, **extra)

    def post_complete(self, who, task_id, row=None, org=None, **extra):
        self.client.force_login(who)
        return self.client.post(self.complete_url(task_id, row, org), **extra)

    def stored_task(self, task_id):
        return OpportunityTask.objects.filter(pk=task_id).values().get()

    def m(self, role):
        return self.membership(self.members[role])

    def task_block(self, html, row=None):
        block = self.row_block(html, row or self.row)
        return block.split("data-tasks-for", 1)[1] if "data-tasks-for" in block else ""


# --- schema --------------------------------------------------------------------------------------------

class SchemaTests(TaskTestCase):
    def test_the_model_its_fk_policies_index_and_constraints(self):
        fields = {f.name for f in OpportunityTask._meta.get_fields()}
        self.assertEqual(fields, {"id", "organization", "opportunity", "title", "due_on", "created_by", "assigned_to",
                                  "completed_at", "completed_by", "created_at"})
        policies = {name: OpportunityTask._meta.get_field(name).remote_field.on_delete.__name__
                    for name in ("organization", "opportunity", "created_by", "assigned_to", "completed_by")}
        self.assertEqual(policies, {"organization": "CASCADE", "opportunity": "CASCADE", "created_by": "SET_NULL",
                                    "assigned_to": "SET_NULL", "completed_by": "SET_NULL"})
        for name in ("created_by", "assigned_to", "completed_by"):
            self.assertIs(OpportunityTask._meta.get_field(name).related_model, OrganizationMember)
        self.assertEqual(OpportunityTask._meta.get_field("due_on").get_internal_type(), "DateField")
        self.assertEqual([index.fields for index in OpportunityTask._meta.indexes],
                         [["organization", "opportunity", "due_on"]])
        self.assertEqual((OPPORTUNITY_TASK_TITLE_MAX_LENGTH, TASK_TITLE_MAX_LENGTH, TASKS_PAGE_LIMIT), (200, 200, 50))
        self.assertEqual(OpportunityTask.objects.count(), 0)

    def test_the_migration_is_one_additive_create_model(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(max(name for app, name in loader.disk_migrations if app == "gemiapp"), "0051_opportunity_task")
        migration = loader.disk_migrations[("gemiapp", "0051_opportunity_task")]
        self.assertEqual(migration.dependencies, [("gemiapp", "0050_opportunity_note")])
        self.assertEqual([(type(op).__name__, op.name) for op in migration.operations],
                         [("CreateModel", "OpportunityTask")])

    def test_the_database_refuses_empty_or_oversized_titles_and_a_completer_without_completion(self):
        owner = self.membership(self.owner)
        base = dict(organization=self.org, opportunity=self.row, due_on=self.today(), created_by=owner)
        for extra in ({"title": ""}, {"title": "α" * 201}, {"title": "x", "completed_by": owner}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                OpportunityTask.objects.create(**{**base, **extra})
        OpportunityTask.objects.create(**base, title="α" * 200)
        self.assertEqual(OpportunityTask.objects.count(), 1)


# --- create: authorization and assignee safety -----------------------------------------------------

class CreateTests(TaskTestCase):
    def test_owner_and_sales_manager_create_on_any_visible_live_row(self):
        for role in ("owner", "sales_manager"):
            response = self.post_task(self.members[role], f"Εργασία {role}")
            self.assertRedirects(response, self.url(), fetch_redirect_response=False)
            task = OpportunityTask.objects.get(title=f"Εργασία {role}")
            self.assertEqual((task.organization_id, task.opportunity_id, task.created_by_id, task.assigned_to_id,
                              task.completed_at, task.completed_by_id),
                             (self.org.pk, self.row.pk, self.m(role).pk, self.m(role).pk, None, None))

    def test_an_assigned_sales_user_creates_a_task_for_themselves(self):
        self.put_assigned(self.maria)
        self.assertEqual(self.post_task(self.maria_user, "Δική μου").status_code, 302)
        task = OpportunityTask.objects.get()
        self.assertEqual((task.created_by_id, task.assigned_to_id), (self.maria.pk, self.maria.pk))
        self.assertTrue(self.task("Ρητά σε εμένα", actor=self.maria_user, assignee=self.maria.pk).changed)

    def test_a_sales_user_cannot_create_on_an_unassigned_or_sibling_opportunity(self):
        sibling = self.live_opportunity(self.full_radar("Radar Νίκου", legal_forms=()))
        self.put_assigned(self.nikos, row=sibling)
        for row in (self.row, sibling):
            for title in ("κανονικό", ""):  # an invalid title changes nothing about the answer: access comes first
                self.assertEqual(self.post_task(self.maria_user, title, row=row).status_code, 404)
        self.assertEqual(OpportunityTask.objects.count(), 0)

    def test_a_sales_user_may_not_name_anyone_else(self):
        self.put_assigned(self.maria)
        for other in (self.m("owner").pk, self.m("sales_manager").pk, self.nikos.pk, "abc", "0"):
            with self.assertRaises(TaskRefused) as refused:
                self.task(actor=self.maria_user, assignee=other)
            self.assertEqual(refused.exception.reason, "assignee", other)
        self.assertEqual(OpportunityTask.objects.count(), 0)  # refused, never silently replaced with herself

    def test_managers_assign_only_to_managers_or_the_opportunitys_own_sales_user(self):
        self.put_assigned(self.maria)
        allowed = (self.m("owner"), self.m("sales_manager"), self.maria)
        for member in allowed:
            self.assertEqual(self.task(f"για {member.pk}", assignee=member.pk).assigned_membership_id, member.pk)
        b_member = add_organization_member(self.org_b, User.objects.create_user("b-task@example.com",
                                                                                "b-task@example.com", "x"), "owner")
        missing = OrganizationMember.objects.order_by("-pk").first().pk + 1
        messages = set()
        for member in (self.nikos.pk, self.m("admin").pk, self.m("viewer").pk, b_member.pk, missing, "1.5", "-3"):
            with self.assertRaises(TaskRefused) as refused:
                self.task("όχι", assignee=member)
            self.assertEqual(refused.exception.reason, "assignee", member)
            messages.add(str(refused.exception))
        self.assertEqual(len(messages), 1)  # an unrelated, foreign or missing member all read the same
        self.assertEqual(OpportunityTask.objects.count(), len(allowed))

    def test_an_inactive_manager_is_not_an_assignee(self):
        manager = self.m("sales_manager")
        User.objects.filter(pk=manager.user_id).update(is_active=False)
        with self.assertRaises(TaskRefused):
            self.task(assignee=manager.pk)

    def test_the_default_assignee(self):
        # unassigned opportunity: the acting member
        self.assertEqual(self.task("1").assigned_membership_id, self.m("owner").pk)
        # the opportunity's valid sales user
        self.put_assigned(self.maria)
        self.assertEqual(self.task("2", actor=self.members["sales_manager"]).assigned_membership_id, self.maria.pk)
        # a salesperson who is no longer a valid sales user: back to the acting member
        User.objects.filter(pk=self.maria_user.pk).update(is_active=False)
        self.assertEqual(self.task("3").assigned_membership_id, self.m("owner").pk)
        User.objects.filter(pk=self.maria_user.pk).update(is_active=True)
        OrganizationMember.objects.filter(pk=self.maria.pk).update(role="viewer")
        self.assertEqual(self.task("4").assigned_membership_id, self.m("owner").pk)
        self.assertEqual(self.task("5", assignee="").assigned_membership_id, self.m("owner").pk)  # blank = omitted

    def test_admin_viewer_non_member_foreign_missing_and_shadow_are_the_same_404(self):
        shadow_company = Company.objects.create(gemi_number="901600", name="Σκιώδης ΑΕ", incorporation_date=date(2026, 9, 1))
        snapshot(shadow_company, T0)
        shadow = self.live_opportunity(self.full_radar("Shadow"), company=shadow_company, mode=SHADOW)
        foreign = self.live_opportunity(self.radar_b_foreign, company=Company.objects.create(
            gemi_number="901700", name="Ξένη ΑΕ", incorporation_date=date(2026, 9, 1)))
        missing = Opportunity.objects.order_by("-pk").first().pk + 1
        outsider = User.objects.create_user("outsider-task@example.com", "outsider-task@example.com", "x")
        responses = [self.post_task(self.members["admin"]), self.post_task(self.members["viewer"]),
                     self.post_task(outsider), self.post_task(self.b_owner), self.post_task(self.owner, row=shadow),
                     self.post_task(self.owner, row=foreign), self.post_task(self.owner, row=foreign, org=self.org_b)]
        self.client.force_login(self.owner)
        responses.append(self.client.post(f"/organizations/{self.org.pk}/opportunities/{missing}/tasks/",
                                          {"title": "x", "due_on": self.tomorrow()}))
        self.assertEqual({response.status_code for response in responses}, {404})
        self.assertEqual({response.content for response in responses[1:]}, {responses[0].content})
        self.assertEqual(OpportunityTask.objects.count(), 0)

    def test_the_task_carries_its_opportunitys_organization_never_the_requests(self):
        self.assertEqual(list(inspect.signature(create_authorized_opportunity_task).parameters),
                         ["user", "organization_id", "opportunity_id", "title", "due_on", "assignee_membership_id"])
        self.client.force_login(self.owner)
        self.client.post(self.task_url(), {"title": "Κανονική", "due_on": self.tomorrow(), "organization": self.org_b.pk,
                                           "created_by": self.m("viewer").pk, "completed_at": "2026-01-01"})
        task = OpportunityTask.objects.get()
        self.assertEqual((task.organization_id, task.created_by_id, task.completed_at),
                         (self.org.pk, self.m("owner").pk, None))

    def test_terminal_opportunities_still_take_and_complete_tasks(self):
        for terminal in sorted(TERMINAL_STATUSES):
            self.set_status(terminal)
            result = self.task(f"Κλείσιμο {terminal}")
            self.assertTrue(self.complete(result.task_id).changed)
            self.assertEqual(self.status(), terminal)


# --- title and due date ---------------------------------------------------------------------------------

class ValidationTests(TaskTestCase):
    def test_title_rules(self):
        self.assertEqual(normalize_task_title("  Κλήση   αύριο \t"), ("Κλήση   αύριο", None))
        self.assertEqual(normalize_task_title("α"), ("α", None))
        self.assertEqual(normalize_task_title("α" * 200), ("α" * 200, None))
        for value, reason in (("α" * 201, "title_too_long"), ("", "title_blank"), ("   \n", "title_blank"),
                              (None, "title_blank"), (["x"], "title_blank"), ("Κλήση\x00", "title_invalid")):
            self.assertEqual(normalize_task_title(value), (None, reason), repr(value))
            with self.assertRaises(TaskRefused) as refused:
                self.task(value)
            self.assertEqual(refused.exception.reason, reason)
        self.assertEqual(OpportunityTask.objects.get(pk=self.task("  με κενά  ").task_id).title, "με κενά")

    def test_due_date_rules(self):
        today = self.today()
        self.assertEqual(parse_task_due_on(today.isoformat(), today), (today, None))
        self.assertEqual(parse_task_due_on((today + timedelta(days=400)).isoformat(), today),
                         (today + timedelta(days=400), None))
        self.assertEqual(parse_task_due_on((today - timedelta(days=1)).isoformat(), today), (None, "due_past"))
        for bad in ("2026-02-30", "19/09/2026", "20260919", "tomorrow", "Παρασκευή", "αύριο", "", None, "2026-9-1"):
            self.assertEqual(parse_task_due_on(bad, today), (None, "due_invalid"), bad)
        self.assertTrue(self.task(due=today.isoformat()).changed)
        with self.assertRaises(TaskRefused) as refused:
            self.task(due=(today - timedelta(days=1)).isoformat())
        self.assertEqual(refused.exception.reason, "due_past")
        response = self.post_task(self.owner, due="Friday", follow=True)
        self.assertIn(TaskRefused.MESSAGES["due_invalid"], response.content.decode())
        self.assertEqual(OpportunityTask.objects.count(), 1)

    def test_today_is_the_configured_local_date_not_utc(self):
        # 22:30 UTC on 18 September is already 19 September in Europe/Athens.
        late = datetime(2026, 9, 18, 22, 30, tzinfo=dt_timezone.utc)
        with patch("django.utils.timezone.now", return_value=late):
            with self.assertRaises(TaskRefused) as refused:
                self.task(due="2026-09-18")
            self.assertEqual(refused.exception.reason, "due_past")
            self.assertTrue(self.task(due="2026-09-19").changed)
        self.assertFalse(task_is_overdue(None, date(2026, 9, 19), date(2026, 9, 19)))  # due today: not overdue
        self.assertTrue(task_is_overdue(None, date(2026, 9, 18), date(2026, 9, 19)))
        self.assertFalse(task_is_overdue(late, date(2026, 9, 1), date(2026, 9, 19)))  # completed: never overdue


# --- completion ------------------------------------------------------------------------------------------

class CompletionTests(TaskTestCase):
    def test_completing_an_open_task_sets_time_and_completer_once(self):
        task_id = self.task().task_id
        with patch("django.utils.timezone.now", return_value=MOMENT), CaptureQueriesContext(connection) as queries:
            result = self.complete(task_id, actor=self.members["sales_manager"])
        self.assertEqual((result.completed, result.changed), (True, True))
        stored = self.stored_task(task_id)
        self.assertEqual((stored["completed_at"], stored["completed_by_id"]), (MOMENT, self.m("sales_manager").pk))
        updates = self.writes(queries)
        self.assertEqual(len(updates), 1)
        self.assertIn('"completed_at"', updates[0])
        before = self.stored_task(task_id)
        with CaptureQueriesContext(connection) as again:
            second = self.complete(task_id)  # another manager, later: still the first completion
        self.assertEqual((second.completed, second.changed), (True, False))
        self.assertEqual(self.writes(again), [])
        self.assertEqual(self.stored_task(task_id), before)
        response = self.post_complete(self.owner, task_id, follow=True)
        self.assertNotIn("Η εργασία ολοκληρώθηκε.", response.content.decode())

    def test_a_sales_user_completes_only_their_own_tasks_and_managers_complete_any(self):
        self.put_assigned(self.maria)
        hers = self.task("της Μαρίας", actor=self.maria_user).task_id
        managers = self.task("του διευθυντή", assignee=self.m("sales_manager").pk).task_id
        self.assertEqual(self.post_complete(self.maria_user, managers).status_code, 404)
        self.assertIsNone(self.stored_task(managers)["completed_at"])
        self.assertEqual(self.post_complete(self.maria_user, hers).status_code, 302)
        self.assertEqual(self.stored_task(hers)["completed_by_id"], self.maria.pk)
        another = self.task("για τη Μαρία", assignee=self.maria.pk).task_id
        self.assertTrue(self.complete(another, actor=self.members["sales_manager"]).changed)

    def test_admin_viewer_and_foreign_or_mismatched_tasks_are_the_same_404(self):
        task_id = self.task().task_id
        sibling = self.live_opportunity(self.full_radar("Radar Β", legal_forms=()))
        foreign_row = self.live_opportunity(self.radar_b_foreign, company=Company.objects.create(
            gemi_number="901800", name="Ξένη ΑΕ", incorporation_date=date(2026, 9, 1)))
        foreign_task = OpportunityTask.objects.create(organization=self.org_b, opportunity=foreign_row, title="ξένη",
                                                      due_on=self.today())
        missing = foreign_task.pk + 1
        responses = [self.post_complete(self.members["admin"], task_id),
                     self.post_complete(self.members["viewer"], task_id),
                     self.post_complete(self.owner, task_id, row=sibling),        # the task is not this row's
                     self.post_complete(self.owner, foreign_task.pk),              # another tenant's task
                     self.post_complete(self.owner, foreign_task.pk, row=foreign_row, org=self.org_b),
                     self.post_complete(self.b_owner, task_id),
                     self.post_complete(self.owner, missing)]
        self.assertEqual({response.status_code for response in responses}, {404})
        self.assertEqual((self.stored_task(task_id)["completed_at"], self.stored_task(foreign_task.pk)["completed_at"]),
                         (None, None))


# --- memberships -----------------------------------------------------------------------------------------

class MembershipTests(TaskTestCase):
    def test_creator_assignee_and_completer_deletion(self):
        self.put_assigned(self.maria)
        created = self.task("από τη Μαρία", actor=self.maria_user).task_id
        done = self.task("ολοκληρωμένη", actor=self.members["sales_manager"]).task_id
        with patch("django.utils.timezone.now", return_value=MOMENT):
            self.complete(done, actor=self.members["sales_manager"])
        self.m("sales_manager").delete()
        stored = self.stored_task(done)
        self.assertEqual((stored["created_by_id"], stored["completed_by_id"], stored["completed_at"]), (None, None, MOMENT))
        self.maria.delete()
        stored = self.stored_task(created)
        self.assertEqual((stored["created_by_id"], stored["assigned_to_id"], stored["completed_at"]), (None, None, None))
        html = self.get(self.owner).content.decode()
        self.assertIn(TASK_UNASSIGNED_LABEL, html)
        self.assertIn(FORMER_MEMBER_LABEL, html)
        # an unassigned open task: managers can still complete it; the re-added user regains nothing
        again = add_organization_member(self.org, self.maria_user, "sales_user")
        self.assertIsNone(self.stored_task(created)["assigned_to_id"])
        self.assertEqual(self.post_complete(self.maria_user, created).status_code, 404)
        self.put_assigned(again)  # even with the opportunity again: the old task was not hers any more
        self.assertEqual(self.post_complete(self.maria_user, created).status_code, 404)
        self.assertTrue(self.complete(created).changed)


class MembershipRaceTests(TaskTestCase):
    """The acting (and named) memberships are re-read and locked inside the task transaction. SQLite serialises
    writers, so these reproduce each interleaving deterministically; they prove behaviour, not PostgreSQL locks."""

    def resolve_then(self, action):
        real = g5.get_organization_access_context

        def resolving(user, organization):
            context = real(user, organization)
            action()
            return context

        return patch.object(g5, "get_organization_access_context", side_effect=resolving)

    def test_a_membership_removed_or_changed_before_the_locked_write_is_denied(self):
        task_id = self.task().task_id
        manager = self.m("sales_manager")
        with self.resolve_then(lambda: OrganizationMember.objects.filter(pk=manager.pk).delete()):
            self.assertRaises(OrganizationAccessDenied, self.task, "x", None, None, self.members["sales_manager"])
        again = add_organization_member(self.org, self.members["sales_manager"], "sales_manager")
        with self.resolve_then(lambda: OrganizationMember.objects.filter(pk=again.pk).update(role="viewer")):
            self.assertRaises(OrganizationAccessDenied, self.complete, task_id, None, self.members["sales_manager"])
        OrganizationMember.objects.filter(pk=again.pk).update(role="sales_manager")
        self.client.force_login(self.members["sales_manager"])
        with self.resolve_then(lambda: OrganizationMember.objects.filter(pk=again.pk).delete()):
            self.assertEqual(self.client.post(self.complete_url(task_id)).status_code, 404)  # never a 500
        self.assertEqual(OpportunityTask.objects.count(), 1)
        self.assertIsNone(self.stored_task(task_id)["completed_at"])

    def test_a_named_assignee_removed_before_the_locked_write_is_refused(self):
        manager = self.m("sales_manager")
        with self.resolve_then(lambda: OrganizationMember.objects.filter(pk=manager.pk).delete()):
            with self.assertRaises(TaskRefused) as refused:
                self.task(assignee=manager.pk)
        self.assertEqual(refused.exception.reason, "assignee")
        self.assertEqual(OpportunityTask.objects.count(), 0)

    def test_one_lock_order_and_foreign_key_failures_become_the_same_denial(self):
        complete_source = inspect.getsource(complete_authorized_opportunity_task)
        order = [complete_source.index(marker) for marker in
                 ("_task_opportunity(", 'OpportunityTask").objects.select_for_update()', "_lock_memberships(")]
        self.assertEqual(order, sorted(order))  # opportunity -> task -> memberships
        create_source = inspect.getsource(create_authorized_opportunity_task)
        order = [create_source.index(marker) for marker in
                 ("_task_opportunity(", "_lock_memberships(", 'OpportunityTask").objects.create(')]
        self.assertEqual(order, sorted(order))
        self.assertIn('.order_by("pk")', inspect.getsource(g5._lock_memberships))  # several members: ascending ids
        with patch.object(OpportunityTask.objects, "create", side_effect=IntegrityError("fk")):
            self.assertRaises(OrganizationAccessDenied, self.task)
            self.assertEqual(self.post_task(self.owner).status_code, 404)
        self.assertEqual(OpportunityTask.objects.count(), 0)


# --- the page ------------------------------------------------------------------------------------------

class PageTests(TaskTestCase):
    def test_tasks_sit_under_their_own_opportunity_open_first_then_completed(self):
        row_b = self.live_opportunity(self.full_radar("Radar Β", legal_forms=()))
        today = self.today()
        titles = {}
        for title, due, hour in (("Τ-μακρινή", 9, 8), ("Τ-κοντινή-β", 2, 9), ("Τ-κοντινή-α", 2, 8), ("Τ-σήμερα", 0, 7),
                                 ("Τ-νωρίς-ολοκλ", 1, 7), ("Τ-αργά-ολοκλ", 5, 7)):
            with patch("django.utils.timezone.now", return_value=MOMENT.replace(hour=hour)):
                titles[title] = self.task(title, due=(today + timedelta(days=due)).isoformat()).task_id
        self.task("Β-μόνη", row=row_b)
        with patch("django.utils.timezone.now", return_value=MOMENT.replace(hour=11)):
            self.complete(titles["Τ-νωρίς-ολοκλ"])
        with patch("django.utils.timezone.now", return_value=MOMENT.replace(hour=12)):
            self.complete(titles["Τ-αργά-ολοκλ"])
        html = self.get(self.owner).content.decode()
        block = self.task_block(html)
        self.assertEqual(re.findall(r"Τ-[\w-]+", block),
                         ["Τ-σήμερα", "Τ-κοντινή-α", "Τ-κοντινή-β", "Τ-μακρινή", "Τ-αργά-ολοκλ", "Τ-νωρίς-ολοκλ"])
        self.assertNotIn("Β-μόνη", block)
        self.assertIn("Β-μόνη", self.task_block(html, row_b))
        self.assertIn(f'data-task-form-for="{self.row.pk}"', block)
        self.assertIn(f'data-task-form-for="{row_b.pk}"', self.task_block(html, row_b))

    def test_overdue_is_derived_on_read_and_today_is_not_overdue(self):
        today = self.today()
        late = self.task("καθυστερημένη").task_id
        due_today = self.task("σήμερα", due=today.isoformat()).task_id
        done_late = self.task("ολοκληρωμένη καθυστερημένη").task_id
        OpportunityTask.objects.filter(pk__in=[late, done_late]).update(due_on=today - timedelta(days=3))
        self.complete(done_late)
        html = self.get(self.owner).content.decode()
        self.assertIn(f'data-task="{late}" data-task-state="overdue"', html)
        self.assertIn(f'data-task="{due_today}" data-task-state="open"', html)
        self.assertIn(f'data-task="{done_late}" data-task-state="completed"', html)
        self.assertEqual(html.count("Εκπρόθεσμη"), 1)
        self.assertFalse([f.name for f in OpportunityTask._meta.get_fields() if "overdue" in f.name])

    def test_controls_follow_the_role(self):
        self.put_assigned(self.maria)
        managers_task = self.task("του διευθυντή", assignee=self.m("sales_manager").pk).task_id
        hers = self.task("της Μαρίας", assignee=self.maria.pk).task_id
        owner_html = self.task_block(self.get(self.owner).content.decode())
        options = re.findall(r'<option value="(\d+)"( selected)?', owner_html.split("data-task-form-for", 1)[1])
        self.assertEqual({int(value) for value, _ in options},
                         {self.m("owner").pk, self.m("sales_manager").pk, self.maria.pk})  # never Nikos/admin/viewer
        self.assertEqual([int(value) for value, selected in options if selected], [self.maria.pk])  # the default
        self.assertEqual(owner_html.count("data-task-complete-for"), 2)
        maria_html = self.task_block(self.get(self.maria_user).content.decode())
        self.assertIn("του διευθυντή", maria_html)  # she reads the manager's task on her opportunity ...
        self.assertNotIn(f'data-task-complete-for="{managers_task}"', maria_html)  # ... but cannot complete it
        self.assertIn(f'data-task-complete-for="{hers}"', maria_html)
        self.assertIn("data-task-form-for", maria_html)
        self.assertNotIn('name="assignee_membership_id"', maria_html.split("data-task-form-for", 1)[1])
        self.assertIn("Υπεύθυνος: Μαρία Παππά", maria_html)
        for role in ("admin", "viewer"):
            html = self.get(self.members[role]).content.decode()
            self.assertIn("της Μαρίας", html, role)
            for control in ("data-task-form-for", "data-task-complete-for", "/tasks/"):
                self.assertNotIn(control, html, (role, control))

    def test_a_sales_user_learns_nothing_about_sibling_tasks(self):
        sibling = self.live_opportunity(self.full_radar("Radar Κρυφό Νίκου", legal_forms=()))
        self.put_assigned(self.maria)
        self.put_assigned(self.nikos, row=sibling)
        self.task("Εργασία Μαρίας", actor=self.maria_user)
        secret = self.task("Μυστική εργασία Νίκου", row=sibling, actor=self.members["sales_user"],
                           due=(self.today() + timedelta(days=77)).isoformat()).task_id
        self.complete(self.task("Ολοκληρωμένη Νίκου", row=sibling).task_id, row=sibling)
        html = self.get(self.maria_user).content.decode()
        for hidden in ("Μυστική εργασία Νίκου", "Ολοκληρωμένη Νίκου", "Radar Κρυφό Νίκου",
                       (self.today() + timedelta(days=77)).strftime("%d/%m/%Y"), f'data-task="{secret}"',
                       f'data-tasks-for="{sibling.pk}"', f"Πωλητής #{self.nikos.pk}"):
            self.assertNotIn(hidden, html, hidden)
        self.assertEqual(html.count("data-task="), 1)
        self.assertEqual(self.post_complete(self.maria_user, secret, row=sibling).status_code, 404)
        self.assertEqual(self.post_complete(self.maria_user, secret).status_code, 404)

    def test_the_page_shows_at_most_fifty_tasks_open_first_with_a_notice(self):
        owner = self.m("owner")
        OpportunityTask.objects.bulk_create(
            [OpportunityTask(organization=self.org, opportunity=self.row, title=f"done{i}", due_on=self.today(),
                             created_by=owner, completed_at=MOMENT, completed_by=owner) for i in range(30)]
            + [OpportunityTask(organization=self.org, opportunity=self.row, title=f"open{i}", due_on=self.today(),
                               created_by=owner, assigned_to=owner) for i in range(25)])
        html = self.get(self.owner).content.decode()
        self.assertEqual(html.count("data-task="), 50)
        self.assertEqual(len(re.findall(r'data-task-state="open"', html)), 25)  # every open task is shown
        self.assertIn("data-tasks-truncated", html)
        self.assertEqual(OpportunityTask.objects.count(), 55)

    def test_tasks_are_read_in_one_bounded_query(self):
        rows = [self.row] + [self.live_opportunity(self.full_radar(f"Radar {i}", legal_forms=())) for i in range(2)]
        self.put_assigned(self.maria)
        for i in range(6):
            self.note(f"σημείωση {i}", row=rows[i % 3])
        with CaptureQueriesContext(connection) as empty:
            self.page()
        members = [self.m("owner"), self.m("sales_manager"), self.maria, None]
        OpportunityTask.objects.bulk_create([
            OpportunityTask(organization=self.org, opportunity=rows[i % 3], title=f"t{i}", due_on=self.today(),
                            created_by=members[i % 4], assigned_to=members[(i + 1) % 4],
                            completed_at=MOMENT if i % 5 == 0 else None,
                            completed_by=members[i % 3] if i % 5 == 0 else None) for i in range(60)])
        with CaptureQueriesContext(connection) as full:
            page = self.page()
        self.assertEqual(len(full), len(empty))  # the task count never changes the query count
        self.assertEqual(len(full), 17)  # 3 opportunities, notes, 60 tasks, 4 member states: measured on SQLite
        task_queries = [q["sql"] for q in full.captured_queries if "opportunitytask" in q["sql"]]
        self.assertEqual(len(task_queries), 1)
        self.assertIn("LIMIT 51", task_queries[0])
        self.assertNotIn('"email"', task_queries[0])
        self.assertEqual(sum(len(o.tasks) for o in page.opportunities), 50)
        self.assertTrue(page.tasks_truncated)

    def test_titles_are_escaped_plain_text_and_reading_writes_nothing(self):
        self.task("<script>alert(1)</script><b>x</b>")
        before = (list(OpportunityTask.objects.values()), self.stored())
        for who in (self.owner, self.members["viewer"]):
            self.client.force_login(who)
            with CaptureQueriesContext(connection) as queries:
                html = self.client.get(self.url()).content.decode()
            self.assertFalse([sql for sql in self.writes(queries) if "gemiapp_opportunity" in sql])
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;&lt;b&gt;x&lt;/b&gt;", html)
            self.assertNotIn("<script>alert(1)", html)
        self.assertEqual((list(OpportunityTask.objects.values()), self.stored()), before)
        template = open("templates/organizations/company_opportunity.html", encoding="utf-8").read()
        self.assertNotIn("|safe", template)


# --- nothing else changes ------------------------------------------------------------------------------

class InvarianceTests(TaskTestCase):
    def test_status_assignment_score_notes_timeline_and_feed_are_untouched(self):
        self.put_assigned(self.maria)
        self.set_status("contacted")
        sibling = self.live_opportunity(self.full_radar("Radar Β", legal_forms=()))
        self.note("υπάρχουσα σημείωση")

        def world():
            return (self.stored(), self.stored(sibling), self.frozen(), self.frozen(sibling),
                    get_authorized_opportunity_score_breakdown(self.owner, self.org, self.row.pk),
                    list(OpportunityNote.objects.values()), list(CompanySignal.objects.values()),
                    list(CompanySnapshot.objects.values()), self.page().timeline, get_opportunity_feed(self.org).cards)

        before = world()
        follow_up = self.task("Follow up Friday", actor=self.maria_user)  # and still no automatic FOLLOW_UP
        self.complete(follow_up.task_id, actor=self.maria_user)          # nor CONTACTED/WON on completion
        self.complete(self.task("Call tomorrow", assignee=self.m("sales_manager").pk).task_id)
        self.assertEqual(world(), before)  # status, assigned_to/at, updated_at, capture, notes, timeline, feed
        self.assertEqual(self.stored()["status"], "contacted")

    def test_the_endpoints_are_post_only_login_only_csrf_protected_and_redirect_to_the_page(self):
        task_id = self.task().task_id
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.task_url()).status_code, 405)
        self.assertEqual(self.client.get(self.complete_url(task_id)).status_code, 405)
        for url, data in ((self.task_url(), {"title": "x", "due_on": self.tomorrow()}), (self.complete_url(task_id), {})):
            anonymous = Client().post(url, data)
            self.assertEqual(anonymous.status_code, 302)
            self.assertIn(reverse("login"), anonymous["Location"])
            strict = Client(enforce_csrf_checks=True)
            strict.force_login(self.owner)
            self.assertEqual(strict.post(url, data).status_code, 403)
        self.assertEqual((OpportunityTask.objects.count(), self.stored_task(task_id)["completed_at"]), (1, None))
        for url, data in ((self.task_url(), {"title": "Κανονική", "due_on": self.tomorrow()}), (self.complete_url(task_id), {})):
            response = self.client.post(url + "?next=https://evil.example/", {**data, "next": "https://evil.example/"})
            self.assertRedirects(response, self.url(), fetch_redirect_response=False)
        html = self.client.get(self.url()).content.decode()
        self.assertIn("Η εργασία ολοκληρώθηκε.", html)

    def test_create_and_complete_are_the_only_task_mutations(self):
        def flatten(patterns, prefix=""):
            for pattern in patterns:
                if hasattr(pattern, "url_patterns"):
                    yield from flatten(pattern.url_patterns, prefix + str(pattern.pattern))
                else:
                    yield prefix + str(pattern.pattern)

        routes = [route for route in flatten(get_resolver().url_patterns) if "task" in route and "organization" in route]
        self.assertEqual(routes, ["organizations/<int:organization_id>/opportunities/<int:opportunity_id>/tasks/",
                                  "organizations/<int:organization_id>/opportunities/<int:opportunity_id>/tasks/"
                                  "<int:task_id>/complete/"])
        code = inspect.getsource(g5) + inspect.getsource(organization_views)
        for forbidden in ("def edit_", "def delete_", "def reopen", "def update_task", "def reassign_task",
                          "completed_at = None", "task.delete", "require_http_methods"):
            self.assertNotIn(forbidden, code, forbidden)
        task_id = self.task().task_id
        self.complete(task_id)
        self.client.force_login(self.owner)
        for suffix in ("edit/", "delete/", "reopen/", "assign/"):
            self.assertEqual(self.client.post(self.task_url() + f"{task_id}/{suffix}").status_code, 404, suffix)
        self.assertEqual(self.client.delete(self.complete_url(task_id)).status_code, 405)
        model_admin = admin.site._registry[OpportunityTask]
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser("staff-task@example.com", "staff-task@example.com", "x")
        task = OpportunityTask.objects.get()
        self.assertEqual((model_admin.has_add_permission(request), model_admin.has_change_permission(request, task),
                          model_admin.has_delete_permission(request, task)), (False, False, False))
        self.assertIsNotNone(self.stored_task(task_id)["completed_at"])  # no reopen anywhere

    def test_nothing_is_injected_nobody_is_notified_and_nothing_is_scheduled(self):
        Company.objects.filter(pk=self.company.pk).update(raw_data={"phone": "2109990001", "email": "info@x.gr",
                                                                    "persons": [{"name": "Γιώργος"}]})
        mail.outbox = []
        result = self.task("Κλήση")
        self.complete(result.task_id)
        self.assertEqual(OpportunityTask.objects.get(pk=result.task_id).title, "Κλήση")
        self.assertEqual(len(mail.outbox), 0)
        source = "".join(inspect.getsource(f) for f in (
            create_authorized_opportunity_task, complete_authorized_opportunity_task, normalize_task_title,
            parse_task_due_on, g5._page_tasks))
        for forbidden in ("raw_data", "gemi_phones", "email", "persons", "send_mail", "notify", "schedule", "celery",
                          "AuditLog", "timeline", "OpportunityNote"):
            self.assertNotIn(forbidden, source, forbidden)
        from django.apps import apps

        names = {model.__name__ for model in apps.get_app_config("gemiapp").get_models()}
        self.assertEqual({n for n in names if "Audit" in n}, {"AdminAuditLog"})
        self.assertFalse([n for n in names if "Notification" in n or "Reminder" in n or "History" in n])
        for path in ("gemiapp/tasks.py", "gemiapp/apps.py"):
            self.assertNotIn("OpportunityTask", open(path, encoding="utf-8").read(), path)

    def test_the_legacy_product_billing_and_phone_are_untouched(self):
        legacy_user = entitled_user("legacy-d34@example.com")
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
            self.assertEqual(self.post_task(self.maria_user, "Εργασία").status_code, 302)
            self.assertEqual(self.post_complete(self.maria_user, OpportunityTask.objects.get().pk).status_code, 302)
            self.assertEqual(world(), before)
        client.assert_not_called()
        self.assertEqual([p.tel_uri for p in Company(gemi_number="1", raw_data={"phone": "+30 2109990001"}).gemi_phones],
                         ["tel:+302109990001"])
        self.assertEqual([p.display for p in extract_company_contact_phones({"phone": "2109990001"})], ["2109990001"])
