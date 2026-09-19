"""Tests for D37 in-app notifications (blueprint §48: notifications, five types, «Unread counter»).

Only ASSIGNMENT (from a real D31 assignment, to the new assignee, in the same transaction) and TASK_DUE (from the
daily 08:00 Europe/Athens generator, to the task's assignee, once per task and due date) are emitted. Everything is
recipient-scoped; reads never write; marking read is idempotent; memberships CASCADE their notifications.
"""

import inspect
import pathlib
import re
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.conf import settings
from django.contrib import admin
from django.contrib.auth.models import User
from django.core import mail
from django.core.signing import TimestampSigner
from django.db import IntegrityError, connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import notifications as d37
from . import organization_access as g5
from .company_signals import SHADOW
from .ingestion.monitoring import radar_match_evidence
from .ingestion.refresh import RefreshPolicy, build_company_refresh_plan
from .models import (
    Company, CompanyMonitoring, CustomerRadar, DigestDelivery, Opportunity, OpportunityTask, OrganizationAuditEvent,
    OrganizationMember, OrganizationNotification, RadarMatch, UserCompanyLead, UserSubscription,
)
from .organization_access import (
    NOTIFICATIONS_PAGE_LIMIT, OrganizationAccessDenied, get_authorized_notifications,
    get_authorized_unread_notification_count, mark_all_authorized_notifications_read,
    mark_authorized_notification_read, save_authorized_opportunity,
)
from .organizations import add_organization_member
from .services import company_matches_radar, eligible_radars, send_digests
from .test_audit_log import AuditTestCase
from .test_gemi_company_activities import entitled_user, radar_for
from .test_organization_radar_matching import T0, snapshot

Notification = OrganizationNotification


class NotificationTestCase(AuditTestCase):
    def assign(self, member, row=None, actor=None):
        return g5.assign_authorized_opportunity(actor or self.owner, self.org.pk, (row or self.row).pk,
                                                member if isinstance(member, int) else member.pk)

    def generate(self, today=None):
        return d37.generate_due_task_notifications(today=today or timezone.localdate())

    def inbox(self, who, org=None):
        return get_authorized_notifications(who, (org or self.org).pk)

    def unread(self, who, org=None):
        return get_authorized_unread_notification_count(who, (org or self.org).pk)

    def of(self, member, **filters):
        return list(Notification.objects.filter(recipient=member, **filters).order_by("pk"))

    def due_task(self, due_delta=0, actor=None, assignee=None, row=None):
        """An open task assigned to ``assignee`` (default: the opportunity's salesperson), due today + delta."""
        result = self.task("Εργασία", actor=actor or self.owner, row=row, assignee=assignee)
        OpportunityTask.objects.filter(pk=result.task_id).update(due_on=timezone.localdate() + timedelta(days=due_delta))
        return result.task_id

    def list_url(self, org=None):
        return reverse("organization_notifications", kwargs={"organization_id": (org or self.org).pk})

    def read_url(self, notification_id, org=None):
        return reverse("organization_notification_read",
                       kwargs={"organization_id": (org or self.org).pk, "notification_id": notification_id})

    def read_all_url(self, org=None):
        return reverse("organization_notifications_read_all", kwargs={"organization_id": (org or self.org).pk})


# --- schema --------------------------------------------------------------------------------------------

class SchemaTests(NotificationTestCase):
    def test_the_model_types_and_fk_policies(self):
        fields = {f.name for f in Notification._meta.get_fields()}
        self.assertEqual(fields, {"id", "organization", "recipient", "notification_type", "opportunity", "task",
                                  "source_audit_event", "due_on", "created_at", "read_at"})
        policies = {f.name: f.remote_field.on_delete.__name__ for f in Notification._meta.get_fields()
                    if f.is_relation and f.concrete}
        self.assertEqual(policies, {"organization": "CASCADE", "recipient": "CASCADE", "opportunity": "CASCADE",
                                    "task": "CASCADE", "source_audit_event": "SET_NULL"})
        self.assertIs(Notification._meta.get_field("recipient").related_model, OrganizationMember)
        self.assertEqual([t for t, _ in Notification.TYPES],
                         ["new_opportunity", "priority_signal", "radar_match", "task_due", "assignment"])
        self.assertFalse([f for f in Notification._meta.get_fields() if type(f).__name__ in ("JSONField", "TextField")])
        self.assertFalse([f for f in Notification._meta.get_fields() if f.name in ("is_read", "body", "message", "title")])

    def test_the_migration_is_one_additive_create_model_without_backfill(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(max(name for app, name in loader.disk_migrations if app == "gemiapp"),
                         "0054_organization_notification")
        migration = loader.disk_migrations[("gemiapp", "0054_organization_notification")]
        self.assertEqual(migration.dependencies, [("gemiapp", "0053_organization_audit_event")])
        self.assertEqual([(type(op).__name__, op.name) for op in migration.operations],
                         [("CreateModel", "OrganizationNotification")])

    def test_the_database_refuses_nonsense_and_duplicates(self):
        maria = self.maria
        task = OpportunityTask.objects.create(organization=self.org, opportunity=self.row, title="t",
                                              due_on=timezone.localdate(), assigned_to=maria)
        base = dict(organization=self.org, recipient=maria)
        for bad in ({"notification_type": "invented"}, {"notification_type": "task_due", "opportunity": self.row},
                    {"notification_type": "task_due", "task": task, "opportunity": self.row},
                    {"notification_type": "assignment"}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Notification.objects.create(**base, **bad)
        due = dict(base, notification_type="task_due", task=task, opportunity=self.row, due_on=task.due_on)
        Notification.objects.create(**due)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(**due)
        Notification.objects.create(**{**due, "due_on": task.due_on + timedelta(days=1)})  # another due date
        Notification.objects.create(**base, notification_type="new_opportunity")          # reserved type: storable
        self.assertEqual(Notification.objects.count(), 3)


# --- ASSIGNMENT ----------------------------------------------------------------------------------------

class AssignmentTests(NotificationTestCase):
    def test_a_real_assignment_notifies_only_the_new_assignee_in_the_same_transaction(self):
        self.assign(self.maria, actor=self.members["sales_manager"])
        note, = self.of(self.maria)
        event = OrganizationAuditEvent.objects.get()
        self.assertEqual((note.notification_type, note.organization_id, note.opportunity_id, note.source_audit_event_id,
                          note.read_at), ("assignment", self.org.pk, self.row.pk, event.pk, None))
        self.assign(self.nikos)  # reassignment: the new assignee only
        self.assertEqual((len(self.of(self.maria)), len(self.of(self.nikos))), (1, 1))
        self.assign(self.maria)  # back to Maria: a new event, a new notification
        self.assertEqual(len(self.of(self.maria)), 2)
        self.assertEqual(Notification.objects.count(), 3)  # no manager, owner or organization-wide copies

    def test_no_notification_for_a_no_op_a_refusal_or_a_denial(self):
        self.assign(self.maria)
        self.assign(self.maria)  # same assignee
        self.assertRaises(g5.AssignmentRefused, self.assign, self.m("owner").pk)  # not a sales user
        for who in (self.members["admin"], self.members["viewer"], self.maria_user):
            self.assertRaises(OrganizationAccessDenied, self.assign, self.nikos, None, who)
        self.set_status("contacted")
        self.assertRaises(g5.AssignmentRefused, self.assign, self.nikos)  # a later state
        self.assertEqual(Notification.objects.count(), 1)

    def test_assigning_to_oneself_notifies_nobody(self):
        sales = g5.ROLE_CAPABILITIES["sales_user"] | {g5.Capability.ASSIGN_OPPORTUNITIES,
                                                        g5.Capability.VIEW_ALL_OPPORTUNITIES}
        with patch.dict(g5.ROLE_CAPABILITIES, {"sales_user": sales}):
            self.assign(self.maria, actor=self.maria_user)
        self.assertEqual((self.stored()["assigned_to_id"], OrganizationAuditEvent.objects.count(),
                          Notification.objects.count()), (self.maria.pk, 1, 0))

    def test_a_failing_notification_rolls_back_the_assignment_and_its_audit_event(self):
        with patch.object(Notification.objects, "create", side_effect=RuntimeError("notification unavailable")):
            self.assertRaises(RuntimeError, self.assign, self.maria)
        self.assertEqual((self.stored()["status"], self.stored()["assigned_to_id"], OrganizationAuditEvent.objects.count(),
                          Notification.objects.count()), ("new", None, 0, 0))

    def test_an_assignee_removed_before_the_write_is_refused_without_anything(self):
        with self.resolve_then(lambda: OrganizationMember.objects.filter(pk=self.maria.pk).delete()):
            self.assertRaises(g5.AssignmentRefused, self.assign, self.maria.pk)
        self.assertEqual((self.stored()["assigned_to_id"], OrganizationAuditEvent.objects.count(),
                          Notification.objects.count()), (None, 0, 0))


# --- TASK_DUE ------------------------------------------------------------------------------------------

class TaskDueTests(NotificationTestCase):
    def setUp(self):
        super().setUp()
        self.put_assigned(self.maria)

    def test_due_today_and_overdue_open_tasks_notify_their_assignee_once(self):
        today_task = self.due_task(0, actor=self.maria_user)
        overdue_task = self.due_task(-3, actor=self.maria_user)
        future_task = self.due_task(1, actor=self.maria_user)          # future: nothing today
        done = self.due_task(0, actor=self.maria_user)
        self.complete(done, actor=self.maria_user)                     # completed: nothing
        unassigned = self.due_task(0)
        OpportunityTask.objects.filter(pk=unassigned).update(assigned_to=None)  # no assignee: nobody to notify
        first = self.generate()
        self.assertEqual((first.candidates, first.created), (2, 2))
        self.assertEqual(sorted(n.task_id for n in self.of(self.maria, notification_type="task_due")),
                         sorted([today_task, overdue_task]))
        again = self.generate()
        self.assertEqual((again.candidates, again.created), (0, 0))
        self.assertEqual(Notification.objects.filter(notification_type="task_due").count(), 2)
        # tomorrow: only the task that becomes due then is new; the two still-open ones are never notified again
        tomorrow = self.generate(timezone.localdate() + timedelta(days=1))
        self.assertEqual((tomorrow.candidates, tomorrow.created), (1, 1))
        self.assertEqual(sorted(Notification.objects.filter(notification_type="task_due").values_list("task_id", flat=True)),
                         sorted([today_task, overdue_task, future_task]))

    def test_the_recipient_is_the_task_assignee_and_never_a_fallback(self):
        manager_task = self.due_task(0, assignee=self.m("sales_manager").pk)
        self.generate()
        note, = Notification.objects.all()
        self.assertEqual((note.recipient_id, note.task_id, note.due_on, note.opportunity_id),
                         (self.m("sales_manager").pk, manager_task, timezone.localdate(), self.row.pk))

    def test_visibility_is_checked_at_generation(self):
        mine = self.due_task(0, actor=self.maria_user)
        self.put_assigned(self.nikos)                                  # Maria no longer sees the opportunity
        self.assertEqual(self.generate().created, 0)
        self.put_assigned(self.maria)
        User.objects.filter(pk=self.maria_user.pk).update(is_active=False)
        self.assertEqual(self.generate().created, 0)                   # inactive user
        User.objects.filter(pk=self.maria_user.pk).update(is_active=True)
        shadow = self.live_opportunity(self.full_radar("Shadow", legal_forms=()), mode=SHADOW)
        OpportunityTask.objects.create(organization=self.org, opportunity=shadow, title="s", assigned_to=self.m("owner"),
                                       due_on=timezone.localdate())
        run = self.generate()
        self.assertEqual((run.created, [n.task_id for n in Notification.objects.all()]), (1, [mine]))  # no SHADOW

    def test_terminal_opportunities_still_notify_open_tasks_and_completion_keeps_the_notification(self):
        task = self.due_task(0, actor=self.maria_user)
        self.dnc(actor=self.members["sales_manager"])
        self.assertEqual(self.generate().created, 1)
        self.complete(task, actor=self.maria_user)
        self.assertEqual(Notification.objects.filter(task_id=task).count(), 1)  # history stays

    def test_a_concurrent_duplicate_is_a_no_op(self):
        self.due_task(0, actor=self.maria_user)
        self.generate()
        unfiltered = OpportunityTask.objects.filter(completed_at__isnull=True).order_by("pk")
        with patch.object(d37, "due_task_candidates", return_value=unfiltered):  # a racing run that missed the row
            run = self.generate()
        self.assertEqual((run.candidates, run.created), (1, 0))
        self.assertEqual(Notification.objects.count(), 1)

    def test_a_hundred_due_tasks_in_a_bounded_number_of_queries(self):
        owner = self.m("owner")
        OpportunityTask.objects.bulk_create([
            OpportunityTask(organization=self.org, opportunity=self.row, title=f"t{i}", due_on=timezone.localdate(),
                            created_by=owner, assigned_to=owner) for i in range(100)])
        with CaptureQueriesContext(connection) as queries:
            run = self.generate()
        self.assertEqual(run.created, 100)
        self.assertLessEqual(len(queries), 8)  # candidates, before/after counts, one bulk insert, the empty last page

    def test_the_one_scheduled_job_runs_daily_at_eight_athens_time(self):
        from gemiapp import tasks
        from gemiapp.apps import SCHEDULES

        entries = [e for e in SCHEDULES if "notification" in e["func"]]
        self.assertEqual(entries, [{"func": "gemiapp.tasks.generate_task_due_notifications_task",
                                    "name": "Task Due Notifications", "cron": "0 8 * * *",
                                    "requires_schema": "gemiapp.notifications.notification_schema_ready"}])
        # Only the D37 entry is schema-aware; the legacy schedules register exactly as before.
        self.assertEqual([e["func"] for e in SCHEDULES if "requires_schema" in e], [entries[0]["func"]])
        self.assertEqual(settings.TIME_ZONE, "Europe/Athens")
        self.due_task(0, actor=self.maria_user)
        self.assertEqual(tasks.generate_task_due_notifications_task()["created"], 1)


# --- read, unread, mark read -----------------------------------------------------------------------------

class ReadTests(NotificationTestCase):
    def test_the_inbox_is_the_recipients_own_newest_first_and_bounded(self):
        self.assign(self.maria)
        self.put_assigned(self.maria)
        entries = self.inbox(self.maria_user)
        self.assertEqual([(e.notification_type, e.available, e.company_id) for e in entries],
                         [("assignment", True, self.company.pk)])
        self.assertEqual(self.inbox(self.owner), ())  # the manager who acted is not a recipient
        Notification.objects.bulk_create([Notification(organization=self.org, recipient=self.maria,
                                                       notification_type="new_opportunity") for _ in range(60)])
        with CaptureQueriesContext(connection) as queries:
            entries = self.inbox(self.maria_user)
        self.assertEqual(len(entries), NOTIFICATIONS_PAGE_LIMIT)
        self.assertLessEqual(len(queries), 4)  # organization, membership, the bounded list, still-visible opportunities
        self.assertFalse([q for q in queries.captured_queries if not q["sql"].upper().startswith("SELECT")])
        for bad in (0, NOTIFICATIONS_PAGE_LIMIT + 1, True):
            self.assertRaises(OrganizationAccessDenied, get_authorized_notifications, self.maria_user, self.org.pk,
                              limit=bad)

    def test_unread_count_mark_one_and_mark_all_are_exact_and_idempotent(self):
        self.assign(self.maria)
        self.assign(self.nikos)
        self.assign(self.maria)
        mine = self.of(self.maria)
        self.assertEqual(self.unread(self.maria_user), 2)
        self.assertEqual(mark_authorized_notification_read(self.maria_user, self.org.pk, mine[0].pk).changed, 1)
        stamp = Notification.objects.get(pk=mine[0].pk).read_at
        with CaptureQueriesContext(connection) as queries:
            again = mark_authorized_notification_read(self.maria_user, self.org.pk, mine[0].pk)
        self.assertEqual(again.changed, 0)
        self.assertEqual(Notification.objects.get(pk=mine[0].pk).read_at, stamp)
        self.assertFalse([q for q in queries.captured_queries if not q["sql"].upper().startswith("SELECT")])  # no write
        self.assertEqual(self.unread(self.maria_user), 1)
        self.assertEqual(mark_all_authorized_notifications_read(self.maria_user, self.org.pk).changed, 1)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(mark_all_authorized_notifications_read(self.maria_user, self.org.pk).changed, 0)
        self.assertFalse([q for q in queries.captured_queries if not q["sql"].upper().startswith("SELECT")])
        self.assertEqual((self.unread(self.maria_user), self.unread(self.members["sales_user"])), (0, 1))  # Nikos untouched
        self.assertFalse(OrganizationAuditEvent.objects.filter(action__icontains="read").exists())  # no audit for reads

    def test_foreign_and_missing_ids_are_denied(self):
        self.assign(self.nikos)
        nikos_note, = self.of(self.nikos)
        missing = nikos_note.pk + 1000
        for notification_id in (nikos_note.pk, missing):
            self.assertRaises(OrganizationAccessDenied, mark_authorized_notification_read, self.maria_user, self.org.pk,
                              notification_id)
        self.assertRaises(OrganizationAccessDenied, mark_authorized_notification_read, self.members["sales_user"],
                          self.org_b.pk, nikos_note.pk)  # right id, wrong organization
        outsider = User.objects.create_user("outsider-notify@example.com", "outsider-notify@example.com", "x")
        for call in (lambda: self.inbox(outsider), lambda: self.unread(outsider),
                     lambda: mark_all_authorized_notifications_read(outsider, self.org.pk)):
            self.assertRaises(OrganizationAccessDenied, call)
        self.assertIsNone(Notification.objects.get(pk=nikos_note.pk).read_at)

    def test_the_endpoints_are_login_only_post_only_csrf_protected_and_self_scoped(self):
        self.assign(self.maria)
        note, = self.of(self.maria)
        self.client.force_login(self.maria_user)
        self.assertEqual(self.client.get(self.list_url()).status_code, 200)
        self.assertEqual(self.client.get(self.read_url(note.pk)).status_code, 405)
        self.assertEqual(self.client.get(self.read_all_url()).status_code, 405)
        anonymous = Client().get(self.list_url())
        self.assertIn(reverse("login"), anonymous["Location"])
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.maria_user)
        self.assertEqual(strict.post(self.read_url(note.pk)).status_code, 403)
        response = self.client.post(self.read_url(note.pk) + "?next=https://evil.example/", {"next": "https://evil.example/"})
        self.assertRedirects(response, self.list_url(), fetch_redirect_response=False)
        self.assertIsNotNone(Notification.objects.get(pk=note.pk).read_at)
        self.client.force_login(self.members["sales_user"])
        self.assertEqual(self.client.post(self.read_url(note.pk)).status_code, 404)  # Nikos cannot touch Maria's
        self.assertEqual(self.client.get(self.list_url(org=self.org_b)).status_code, 404)

    def test_reading_writes_nothing_and_never_generates_task_due(self):
        self.put_assigned(self.maria)
        self.due_task(0, actor=self.maria_user)  # due now, but only the scheduled job may create the notification
        self.client.force_login(self.maria_user)
        for url in (self.list_url(), self.url()):
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(url).status_code, 200)
            self.assertFalse([q["sql"] for q in queries.captured_queries if "gemiapp_" in q["sql"]
                              and not q["sql"].upper().startswith(("SELECT", "SAVEPOINT", "RELEASE"))])
        self.assertEqual(Notification.objects.count(), 0)


# --- tenancy, isolation, membership ----------------------------------------------------------------------

class IsolationTests(NotificationTestCase):
    def test_a_user_in_two_organizations_has_two_separate_inboxes(self):
        maria_b = add_organization_member(self.org_b, self.maria_user, "sales_user")
        b_row = self.live_opportunity(self.radar_b_foreign)
        g5.assign_authorized_opportunity(self.b_owner, self.org_b.pk, b_row.pk, maria_b.pk)
        self.assign(self.maria)
        self.assertEqual((self.unread(self.maria_user), self.unread(self.maria_user, self.org_b)), (1, 1))
        mark_all_authorized_notifications_read(self.maria_user, self.org.pk)
        self.assertEqual((self.unread(self.maria_user), self.unread(self.maria_user, self.org_b)), (0, 1))
        self.assertEqual({e.notification_id for e in self.inbox(self.maria_user, self.org_b)},
                         {n.pk for n in self.of(maria_b)})

    def test_a_sales_user_never_learns_about_a_siblings_opportunity(self):
        sibling = self.live_opportunity(self.full_radar("Radar Κρυφό Νίκου", legal_forms=()))
        self.assign(self.maria)
        self.assign(self.nikos, row=sibling)
        self.put_assigned(self.nikos, row=sibling)
        self.due_task(0, actor=self.members["sales_user"], row=sibling)
        self.generate()
        maria = self.inbox(self.maria_user)
        self.assertEqual([e.notification_type for e in maria], ["assignment"])
        self.assertNotIn("Radar Κρυφό Νίκου", str(maria))
        self.assertEqual(self.unread(self.maria_user), 1)
        nikos_ids = [n.pk for n in self.of(self.nikos)]
        self.assertEqual(len(nikos_ids), 2)
        for notification_id in nikos_ids:
            self.assertRaises(OrganizationAccessDenied, mark_authorized_notification_read, self.maria_user,
                              self.org.pk, notification_id)
        self.client.force_login(self.maria_user)
        self.assertNotIn("Radar Κρυφό Νίκου", self.client.get(self.list_url()).content.decode())

    def test_a_notification_for_an_opportunity_no_longer_visible_shows_no_details_or_link(self):
        self.assign(self.maria)
        self.put_assigned(self.nikos)  # reassigned away (set directly: no new notification for Maria)
        entry, = self.inbox(self.maria_user)
        self.assertEqual((entry.available, entry.company_id, entry.company_name, entry.radar_name),
                         (False, None, "", ""))
        self.client.force_login(self.maria_user)
        html = self.client.get(self.list_url()).content.decode()
        self.assertIn("δεν είναι πλέον διαθέσιμη", html)
        self.assertNotIn(self.url(), html)

    def test_membership_deletion_cascades_and_a_re_added_user_starts_clean(self):
        self.assign(self.maria)
        self.assertEqual(len(self.of(self.maria)), 1)
        self.maria.delete()
        self.assertEqual(Notification.objects.count(), 0)
        add_organization_member(self.org, self.maria_user, "sales_user")
        self.assertEqual((self.unread(self.maria_user), self.inbox(self.maria_user)), (0, ()))


# --- page, boundaries, privacy, parity -----------------------------------------------------------------

class BoundaryTests(NotificationTestCase):
    def test_the_d29_page_shows_the_members_own_unread_count_and_the_legacy_nav_does_not(self):
        self.assign(self.maria)
        self.put_assigned(self.maria)
        html = self.get(self.maria_user).content.decode()
        self.assertIn(self.list_url(), html)
        self.assertIn('data-unread-notifications>1<', html)
        self.assertNotIn("data-unread-notifications", self.get(self.owner).content.decode())  # the owner has none
        base = pathlib.Path("templates/base.html").read_text(encoding="utf-8")
        self.assertNotIn("notification", base.lower())

    def test_no_other_crm_action_notifies(self):
        self.put_assigned(self.maria)
        save_authorized_opportunity(self.owner, self.org.pk, self.live_opportunity(self.full_radar("Radar Β",
                                                                                                legal_forms=())).pk)
        self.change("contacted", actor=self.maria_user)
        self.note("σημείωση", actor=self.maria_user)
        task = self.task("εργασία", actor=self.owner, assignee=self.maria.pk).task_id
        self.complete(task, actor=self.maria_user)
        self.dnc(actor=self.members["sales_manager"])
        self.assertEqual(Notification.objects.count(), 0)

    def test_the_three_pipeline_types_have_no_emitter(self):
        product = [p for p in pathlib.Path("gemiapp").rglob("*.py")
                   if "test" not in p.name and "migrations" not in p.parts]
        creators = {str(p) for p in product
                    if re.search(r"NOTIFY\.(NEW_OPPORTUNITY|PRIORITY_SIGNAL|RADAR_MATCH)|\"(new_opportunity|priority_signal|radar_match)\"",
                                 p.read_text(encoding="utf-8"))}
        # only the model's choices and the G5 labels mention them; no module creates them
        self.assertEqual({pathlib.Path(p).name for p in creators}, {"models.py", "organization_access.py"})
        for module in ("opportunities.py", "opportunity_feed.py", "organization_radar_matching.py",
                       "opportunity_scoring.py", "company_signals.py", "contact_suppressions.py"):
            self.assertNotIn("Notification", (pathlib.Path("gemiapp") / module).read_text(encoding="utf-8"), module)
        creation = inspect.getsource(g5)
        self.assertEqual(creation.count('_model("OrganizationNotification").objects.create('), 1)  # the D31 emitter

    def test_titles_and_names_are_escaped_and_nothing_is_copied_or_sent(self):
        self.put_assigned(self.maria)
        Company.objects.filter(pk=self.company.pk).update(raw_data={"phone": "2109990001", "email": "info@x.gr"})
        task = self.task("<script>alert(1)</script>", actor=self.maria_user)
        OpportunityTask.objects.filter(pk=task.task_id).update(due_on=timezone.localdate())
        mail.outbox = []
        self.generate()
        self.client.force_login(self.maria_user)
        html = self.client.get(self.list_url()).content.decode()
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)", html)
        body = html.split("</header>", 1)[-1]  # the site header shows the viewer's own account, as everywhere
        for secret in ("2109990001", "info@x.gr", self.maria_user.email, "owner@example.com"):
            self.assertNotIn(secret, body)
        stored = str(list(Notification.objects.values()))
        for copied in ("script", self.company.name, "2109990001"):
            self.assertNotIn(copied, stored)
        self.assertEqual(len(mail.outbox), 0)
        template = pathlib.Path("templates/organizations/notifications.html").read_text(encoding="utf-8")
        self.assertNotIn("|safe", template)

    def test_the_legacy_digest_billing_and_admin_are_untouched(self):
        legacy_user = entitled_user("legacy-d37@example.com")
        legacy = radar_for(legacy_user, "legacy", prefectures=["ΑΤΤΙΚΗΣ"])
        run_at = datetime(2026, 9, 16, 9, tzinfo=dt_timezone.utc)
        CompanyMonitoring.objects.create(
            company=self.company, state="active", priority="high", primary_reason="active_radar_match",
            policy_version=1, monitored_since=run_at - timedelta(days=3), next_check_at=run_at - timedelta(hours=1))
        policy = RefreshPolicy(max_requests_per_run=5, max_pages_per_query=2, max_direct_details_per_run=2, page_size=2)
        self.put_assigned(self.maria)
        self.due_task(0, actor=self.maria_user)

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
            self.assign(self.nikos, row=self.live_opportunity(self.full_radar("Radar Γ", legal_forms=())))
            self.generate()
            self.assertEqual(world(), before)
        client.assert_not_called()
        self.assertEqual(Notification.objects.count(), 2)
        model_admin = admin.site._registry[Notification]
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser("staff-notify@example.com", "staff-notify@example.com", "x")
        note = Notification.objects.first()
        self.assertEqual((model_admin.has_add_permission(request), model_admin.has_change_permission(request, note),
                          model_admin.has_delete_permission(request, note)), (False, False, False))
