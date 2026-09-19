"""Tests for D36 Audit Log (blueprint §52: actor, organization, action, entity, timestamp, metadata).

Every Phase D mutation that actually changes state writes exactly one OrganizationAuditEvent in the same
transaction; refused and no-op calls write none. Events are organization-owned, append-only and typed (no note or task
text, no contact data). The read model follows D29 visibility: a sales user never sees a sibling opportunity's events.
"""

import inspect
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import User
from django.core import mail
from django.db import IntegrityError, connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext

from . import organization_access as g5
from .company_signals import SHADOW
from .models import Company, Opportunity, OpportunityNote, OpportunityTask, OrganizationAuditEvent, OrganizationMember
from .organization_access import (
    AUDIT_PAGE_LIMIT, FORMER_MEMBER_LABEL, AssignmentRefused, DoNotContactRefused, NoteRefused,
    OpportunityTransitionRefused, OrganizationAccessDenied, StatusChangeRefused, TaskRefused,
    get_authorized_company_audit_events, save_authorized_opportunity,
)
from .organizations import add_organization_member
from .test_contact_suppression import DncTestCase
from .test_organization_radar_matching import T0, snapshot

MOMENT = datetime(2026, 9, 18, 10, tzinfo=dt_timezone.utc)
Event = OrganizationAuditEvent


class AuditTestCase(DncTestCase):
    def events(self, **filters):
        return list(Event.objects.filter(**filters).order_by("pk").values())

    def read(self, who=None, company=None, org=None, **kwargs):
        return get_authorized_company_audit_events(who or self.owner, (org or self.org).pk,
                                                   (company or self.company).pk, **kwargs)

    def one(self):
        event, = Event.objects.all()
        return event

    def resolve_then(self, action):
        real = g5.get_organization_access_context

        def resolving(user, organization):
            context = real(user, organization)
            action()
            return context

        return patch.object(g5, "get_organization_access_context", side_effect=resolving)


# --- schema --------------------------------------------------------------------------------------------

class SchemaTests(AuditTestCase):
    def test_the_model_is_typed_and_organization_owned(self):
        fields = {f.name for f in Event._meta.get_fields()}
        self.assertEqual(fields, {"id", "organization", "actor", "action", "company", "opportunity", "note", "task",
                                  "suppression", "previous_status", "new_status", "previous_assignee", "new_assignee",
                                  "reason", "created_at"})
        policies = {f.name: f.remote_field.on_delete.__name__ for f in Event._meta.get_fields()
                    if f.is_relation and f.concrete}
        self.assertEqual(policies.pop("organization"), "CASCADE")
        self.assertEqual(set(policies.values()), {"SET_NULL"})  # history outlives everything it refers to
        self.assertFalse([f for f in Event._meta.get_fields() if type(f).__name__ in ("JSONField", "TextField")])
        self.assertEqual({a for a, _ in Event.ACTIONS}, {
            "opportunity_saved", "opportunity_assigned", "opportunity_reassigned", "opportunity_status_changed",
            "note_added", "task_created", "task_completed", "suppression_added", "suppression_reapplied"})
        self.assertEqual(Event.objects.count(), 0)

    def test_the_migration_is_one_additive_create_model_without_backfill(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        # D36 owns 0053; D37's 0054 (notifications) follows it.
        self.assertEqual(loader.disk_migrations[("gemiapp", "0054_organization_notification")].dependencies,
                         [("gemiapp", "0053_organization_audit_event")])
        migration = loader.disk_migrations[("gemiapp", "0053_organization_audit_event")]
        self.assertEqual(migration.dependencies, [("gemiapp", "0052_organization_contact_suppression")])
        self.assertEqual([(type(op).__name__, op.name) for op in migration.operations],
                         [("CreateModel", "OrganizationAuditEvent")])

    def test_events_are_append_only(self):
        save_authorized_opportunity(self.owner, self.org.pk, self.row.pk)
        event = self.one()
        event.reason = "rewritten"
        self.assertRaises(ValueError, event.save)
        self.assertRaises(ValueError, event.delete)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Event.objects.create(organization=self.org, action="invented")
        model_admin = admin.site._registry[Event]
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser("staff-audit@example.com", "staff-audit@example.com", "x")
        self.assertEqual((model_admin.has_add_permission(request), model_admin.has_change_permission(request, event),
                          model_admin.has_delete_permission(request, event)), (False, False, False))
        self.assertEqual(Event.objects.get().reason, "")


# --- every action, exactly once ----------------------------------------------------------------------

class TaxonomyTests(AuditTestCase):
    def test_save_assign_reassign_and_status_change(self):
        owner, manager = self.m("owner"), self.m("sales_manager")
        save_authorized_opportunity(self.owner, self.org.pk, self.row.pk)
        g5.assign_authorized_opportunity(self.members["sales_manager"], self.org.pk, self.row.pk, self.maria.pk)
        g5.assign_authorized_opportunity(self.owner, self.org.pk, self.row.pk, self.nikos.pk)
        self.change("contacted", actor=self.members["sales_user"])
        rows = [(e["action"], e["actor_id"], e["opportunity_id"], e["company_id"], e["previous_status"],
                 e["new_status"], e["previous_assignee_id"], e["new_assignee_id"]) for e in self.events()]
        self.assertEqual(rows, [
            ("opportunity_saved", owner.pk, self.row.pk, self.company.pk, "new", "saved", None, None),
            ("opportunity_assigned", manager.pk, self.row.pk, self.company.pk, "saved", "assigned", None, self.maria.pk),
            ("opportunity_reassigned", owner.pk, self.row.pk, self.company.pk, "assigned", "assigned", self.maria.pk,
             self.nikos.pk),
            ("opportunity_status_changed", self.nikos.pk, self.row.pk, self.company.pk, "assigned", "contacted", None,
             None),
        ])
        self.assertEqual({e["organization_id"] for e in self.events()}, {self.org.pk})

    def test_notes_tasks_and_suppression(self):
        self.put_assigned(self.maria)
        note = self.note("Κείμενο που δεν πρέπει να αντιγραφεί", actor=self.maria_user)
        task = self.task("Τίτλος που δεν πρέπει να αντιγραφεί", actor=self.maria_user)
        self.complete(task.task_id, actor=self.maria_user)
        self.dnc(actor=self.members["sales_manager"], reason="call_objection")
        rows = [(e["action"], e["actor_id"], e["opportunity_id"], e["note_id"], e["task_id"], e["suppression_id"],
                 e["reason"], e["new_assignee_id"], e["new_status"]) for e in self.events()]
        suppression_id = self.dnc_record_id()
        self.assertEqual(rows, [
            ("note_added", self.maria.pk, self.row.pk, note.note_id, None, None, "", None, ""),
            ("task_created", self.maria.pk, self.row.pk, None, task.task_id, None, "", self.maria.pk, ""),
            ("task_completed", self.maria.pk, self.row.pk, None, task.task_id, None, "", None, ""),
            ("suppression_added", self.m("sales_manager").pk, None, None, None, suppression_id, "call_objection", None,
             "do_not_contact"),
        ])
        flat = str(self.events())
        for copied in ("Κείμενο που", "Τίτλος που", "@", self.company.name):
            self.assertNotIn(copied, flat)  # typed references only: no note/task text, no contact, no name

    def dnc_record_id(self):
        from .models import OrganizationContactSuppression

        return OrganizationContactSuppression.objects.get().pk

    def test_a_dnc_repeat_that_settles_a_stray_row_is_its_own_event(self):
        self.dnc()
        Opportunity.objects.filter(pk=self.row.pk).update(status="new")
        self.dnc()
        self.assertEqual([e["action"] for e in self.events()], ["suppression_added", "suppression_reapplied"])
        self.assertEqual(self.events()[1]["reason"], "")  # the original record's reason is not re-asserted


# --- no phantom events ---------------------------------------------------------------------------------

class NoPhantomTests(AuditTestCase):
    def test_no_op_calls_write_no_event(self):
        save_authorized_opportunity(self.owner, self.org.pk, self.row.pk)
        save_authorized_opportunity(self.owner, self.org.pk, self.row.pk)                      # already SAVED
        g5.assign_authorized_opportunity(self.owner, self.org.pk, self.row.pk, self.maria.pk)
        g5.assign_authorized_opportunity(self.owner, self.org.pk, self.row.pk, self.maria.pk)  # same assignee
        self.change("contacted")
        self.change("contacted")                                                               # same status
        task = self.task().task_id
        self.complete(task)
        self.complete(task)                                                                    # already completed
        self.dnc()
        self.dnc()                                                                             # already suppressed
        self.assertEqual([e["action"] for e in self.events()], [
            "opportunity_saved", "opportunity_assigned", "opportunity_status_changed", "task_created",
            "task_completed", "suppression_added"])

    def test_refused_and_denied_calls_write_no_event(self):
        self.set_status("won")
        self.assertRaises(OpportunityTransitionRefused, save_authorized_opportunity, self.owner, self.org.pk, self.row.pk)
        self.assertRaises(AssignmentRefused, g5.assign_authorized_opportunity, self.owner, self.org.pk, self.row.pk,
                          self.maria.pk)
        self.assertRaises(StatusChangeRefused, self.change, "lost")
        self.assertRaises(NoteRefused, self.note, "   ")
        self.assertRaises(TaskRefused, self.task, "x", "yesterday")
        self.assertRaises(DoNotContactRefused, self.dnc, None, "manual", False)
        for who in (self.members["admin"], self.members["viewer"], self.members["sales_user"]):
            self.assertRaises(OrganizationAccessDenied, self.change, "contacted", None, who)
            self.assertRaises(OrganizationAccessDenied, self.dnc, who)
        self.assertEqual(Event.objects.count(), 0)


# --- one transaction -----------------------------------------------------------------------------------

class TransactionTests(AuditTestCase):
    def test_a_failing_audit_write_rolls_the_mutation_back(self):
        self.put_assigned(self.maria)
        before = (self.stored(), list(OpportunityNote.objects.values()), list(OpportunityTask.objects.values()))
        with patch.object(g5, "_audit", side_effect=RuntimeError("audit unavailable")):
            self.assertRaises(RuntimeError, save_authorized_opportunity, self.owner, self.org.pk,
                              self.live_opportunity(self.full_radar("Radar Β", legal_forms=())).pk)
            self.assertRaises(RuntimeError, self.change, "contacted")
            self.assertRaises(RuntimeError, g5.assign_authorized_opportunity, self.owner, self.org.pk, self.row.pk,
                              self.nikos.pk)
            self.assertRaises(RuntimeError, self.note, "x")
            self.assertRaises(RuntimeError, self.task, "x")
            self.assertRaises(RuntimeError, self.dnc)
        self.assertEqual((self.stored(), list(OpportunityNote.objects.values()), list(OpportunityTask.objects.values())),
                         before)
        from .models import OrganizationContactSuppression

        self.assertEqual((Event.objects.count(), OrganizationContactSuppression.objects.count()), (0, 0))
        self.assertEqual(Opportunity.objects.filter(status__in=("saved", "do_not_contact")).count(), 0)

    def test_an_actor_removed_before_the_locked_write_is_denied_without_a_change_or_event(self):
        manager = self.m("sales_manager")
        for call in (lambda: save_authorized_opportunity(self.members["sales_manager"], self.org.pk, self.row.pk),
                     lambda: self.change("contacted", actor=self.members["sales_manager"]),
                     lambda: g5.assign_authorized_opportunity(self.members["sales_manager"], self.org.pk, self.row.pk,
                                                              self.maria.pk)):
            again = OrganizationMember.objects.filter(organization=self.org, user=self.members["sales_manager"]).first() \
                or add_organization_member(self.org, self.members["sales_manager"], "sales_manager")
            with self.resolve_then(lambda: OrganizationMember.objects.filter(pk=again.pk).delete()):
                self.assertRaises(OrganizationAccessDenied, call)
        self.assertEqual((self.status(), Event.objects.count()), ("new", 0))
        self.assertTrue(manager.pk)

    def test_an_fk_failure_at_commit_is_the_same_denial(self):
        with patch.object(g5, "_audit", side_effect=IntegrityError("actor fk")):
            self.assertRaises(OrganizationAccessDenied, self.change, "contacted")
            self.assertEqual(self.post_status(self.owner, "contacted").status_code, 404)
        self.assertEqual((self.status(), Event.objects.count()), ("new", 0))


# --- actor and tenancy ---------------------------------------------------------------------------------

class ActorAndTenancyTests(AuditTestCase):
    def test_the_event_outlives_its_actor_who_regains_nothing(self):
        self.change("contacted", actor=self.members["sales_manager"])
        self.m("sales_manager").delete()
        event = self.one()
        self.assertEqual((event.actor_id, event.action, event.new_status), (None, "opportunity_status_changed", "contacted"))
        add_organization_member(self.org, self.members["sales_manager"], "sales_manager")
        event.refresh_from_db()
        self.assertIsNone(event.actor_id)
        self.assertEqual(self.read()[0].actor_display_name, FORMER_MEMBER_LABEL)

    def test_organizations_never_see_each_others_history(self):
        foreign_row = self.live_opportunity(self.radar_b_foreign)  # organization B, the same company
        save_authorized_opportunity(self.b_owner, self.org_b.pk, foreign_row.pk)
        self.change("contacted")
        self.assertEqual([e.action for e in self.read()], ["opportunity_status_changed"])
        self.assertEqual([e.action for e in self.read(self.b_owner, org=self.org_b)], ["opportunity_saved"])
        self.assertRaises(OrganizationAccessDenied, self.read, self.owner, None, self.org_b)
        outsider = User.objects.create_user("outsider-audit@example.com", "outsider-audit@example.com", "x")
        self.assertRaises(OrganizationAccessDenied, self.read, outsider)


# --- the read model ------------------------------------------------------------------------------------

class ReadModelTests(AuditTestCase):
    def test_newest_first_with_id_tie_break_and_a_bound(self):
        with patch("django.utils.timezone.now", return_value=MOMENT):
            save_authorized_opportunity(self.owner, self.org.pk, self.row.pk)
            self.change("contacted")
        with patch("django.utils.timezone.now", return_value=MOMENT + timedelta(hours=1)):
            self.change("interested")
        entries = self.read()
        self.assertEqual([e.action for e in entries],
                         ["opportunity_status_changed", "opportunity_status_changed", "opportunity_saved"])
        self.assertEqual([e.new_status for e in entries], ["interested", "contacted", "saved"])  # same moment: -id
        for bad in (0, AUDIT_PAGE_LIMIT + 1, "5", True):
            self.assertRaises(OrganizationAccessDenied, self.read, limit=bad)
        Event.objects.bulk_create([Event(organization=self.org, company=self.company, opportunity=self.row,
                                         action="note_added") for _ in range(60)])
        with CaptureQueriesContext(connection) as queries:
            entries = self.read()
        self.assertEqual(len(entries), AUDIT_PAGE_LIMIT)
        self.assertLessEqual(len(queries), 5)  # organization, membership, visibility check, one bounded event query
        self.assertIn(f"LIMIT {AUDIT_PAGE_LIMIT}", [q["sql"] for q in queries.captured_queries][-1])

    def test_a_sales_user_sees_only_their_opportunitys_history_and_company_level_events(self):
        sibling = self.live_opportunity(self.full_radar("Radar Κρυφό Νίκου", legal_forms=()))
        self.put_assigned(self.maria)
        self.put_assigned(self.nikos, row=sibling)
        self.note("της Μαρίας", actor=self.maria_user)
        self.note("του Νίκου", row=sibling, actor=self.members["sales_user"])
        self.change("contacted", row=sibling, actor=self.members["sales_manager"])
        self.dnc(actor=self.members["sales_manager"])
        maria = self.read(self.maria_user)
        self.assertEqual([(e.action, e.opportunity_id) for e in maria],
                         [("suppression_added", None), ("note_added", self.row.pk)])
        self.assertNotIn("Radar Κρυφό Νίκου", str(maria))
        nikos = self.read(self.members["sales_user"])
        self.assertEqual([(e.action, e.opportunity_id) for e in nikos],
                         [("suppression_added", None), ("opportunity_status_changed", sibling.pk),
                          ("note_added", sibling.pk)])
        manager = self.read(self.members["sales_manager"])
        self.assertEqual(len(manager), 4)  # managers see the whole company
        for who in (self.members["admin"], self.members["viewer"]):
            self.assertEqual(len(self.read(who)), 4)

    def test_shadow_rows_and_unreadable_companies_stay_hidden(self):
        shadow = self.live_opportunity(self.full_radar("Shadow", legal_forms=()), mode=SHADOW)
        Event.objects.create(organization=self.org, company=self.company, opportunity=shadow, action="note_added")
        self.assertEqual(self.read(), ())
        other = Company.objects.create(gemi_number="903100", name="Κρυφή ΑΕ", incorporation_date=date(2026, 9, 1))
        snapshot(other, T0)
        self.assertRaises(OrganizationAccessDenied, self.read, self.owner, other)  # no visible opportunity
        self.assertRaises(OrganizationAccessDenied, self.read, self.maria_user)   # unassigned sales user

    def test_the_read_is_not_the_timeline_and_nobody_is_notified(self):
        timeline = self.page().timeline
        mail.outbox = []
        self.change("contacted")
        self.note("σημείωση")
        self.assertEqual(self.page().timeline, timeline)  # B6 describes business events, never CRM actions
        self.assertEqual(len(mail.outbox), 0)
        source = inspect.getsource(g5._audit) + inspect.getsource(get_authorized_company_audit_events)
        for forbidden in ("send_mail", "notify", "webhook", "raw_data", "body", "title", "email"):
            self.assertNotIn(forbidden, source, forbidden)
        self.assertNotIn("audit", open("templates/organizations/company_opportunity.html", encoding="utf-8").read().lower())
