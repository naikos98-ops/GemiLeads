"""Tests for D33 Notes (blueprint §41 «opportunity_notes», §62 tenant security, Phase D item 33).

A note is user-authored CRM text on one explicit C8 opportunity, owned by its organization and attributed to the
exact membership that wrote it. Append-only in v1. OWNER, SALES_MANAGER and the assigned SALES_USER may add one;
everyone who can read the opportunity reads its notes. Adding a note changes nothing else: no status, assignment,
score, timeline, feed, audit entry or notification.
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

from . import organization_access as g5
from . import organization_views
from .company_contact import extract_company_contact_phones
from .company_signals import SHADOW
from .ingestion.monitoring import radar_match_evidence
from .ingestion.refresh import RefreshPolicy, build_company_refresh_plan
from .models import (
    OPPORTUNITY_NOTE_MAX_LENGTH, Company, CompanyMonitoring, CompanySignal, CompanySnapshot, CustomerRadar,
    DigestDelivery, Opportunity, OpportunityNote, OrganizationMember, RadarMatch, UserCompanyLead, UserSubscription,
)
from .opportunity_feed import get_opportunity_feed
from .organization_access import (
    FORMER_MEMBER_LABEL, NOTE_MAX_LENGTH, NOTES_PAGE_LIMIT, TERMINAL_STATUSES, NoteRefused, OrganizationAccessDenied,
    add_authorized_opportunity_note, get_authorized_opportunity_score_breakdown, normalize_note_body,
)
from .organizations import add_organization_member
from .services import company_matches_radar, eligible_radars, send_digests
from .test_gemi_company_activities import entitled_user, radar_for
from .test_opportunity_assignment import MARIA_EMAIL
from .test_opportunity_status import StatusTestCase
from .test_organization_radar_matching import T0, snapshot


class NoteTestCase(StatusTestCase):
    def note(self, body="Κλήση, καμία απάντηση", row=None, actor=None, org=None):
        return add_authorized_opportunity_note(actor or self.owner, (org or self.org).pk, (row or self.row).pk, body)

    def note_url(self, row=None, org=None):
        return reverse("organization_add_opportunity_note",
                       kwargs={"organization_id": (org or self.org).pk, "opportunity_id": (row or self.row).pk})

    def post_note(self, who, body="Κλήση, καμία απάντηση", row=None, org=None, **extra):
        self.client.force_login(who)
        data = {} if body is None else {"body": body}
        return self.client.post(self.note_url(row, org), data, **extra)

    def membership(self, user, org=None):
        return OrganizationMember.objects.get(organization=org or self.org, user=user)

    def row_block(self, html, row):
        """The HTML of one opportunity row on D29, from its marker to the next row's."""
        start = html.index(f'data-opportunity="{row.pk}"')
        following = html.find('data-opportunity="', start + 1)
        return html[start:following if following != -1 else len(html)]


# --- schema --------------------------------------------------------------------------------------------

class SchemaTests(NoteTestCase):
    def test_the_model_its_fk_policies_index_and_constraints(self):
        fields = {f.name for f in OpportunityNote._meta.get_fields()}
        self.assertEqual(fields, {"id", "organization", "opportunity", "author", "body", "created_at"})
        policies = {name: OpportunityNote._meta.get_field(name).remote_field.on_delete.__name__
                    for name in ("organization", "opportunity", "author")}
        self.assertEqual(policies, {"organization": "CASCADE", "opportunity": "CASCADE", "author": "SET_NULL"})
        self.assertIs(OpportunityNote._meta.get_field("author").related_model, OrganizationMember)
        self.assertTrue(OpportunityNote._meta.get_field("author").null)
        self.assertEqual(OpportunityNote._meta.ordering, ["-created_at", "-id"])
        self.assertEqual([index.fields for index in OpportunityNote._meta.indexes],
                         [["organization", "opportunity", "-created_at"]])
        self.assertEqual((OPPORTUNITY_NOTE_MAX_LENGTH, NOTE_MAX_LENGTH, NOTES_PAGE_LIMIT), (4000, 4000, 50))
        self.assertEqual(OpportunityNote.objects.count(), 0)

    def test_the_migration_is_one_additive_create_model(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        # D33 owns 0050; D34's 0051 (tasks) follows it.
        self.assertEqual(loader.disk_migrations[("gemiapp", "0051_opportunity_task")].dependencies,
                         [("gemiapp", "0050_opportunity_note")])
        migration = loader.disk_migrations[("gemiapp", "0050_opportunity_note")]
        self.assertEqual(migration.dependencies, [("gemiapp", "0049_opportunity_assignment")])
        self.assertEqual([(type(op).__name__, op.name) for op in migration.operations],
                         [("CreateModel", "OpportunityNote")])

    def test_the_database_itself_refuses_an_empty_or_oversized_body(self):
        membership = self.membership(self.owner)
        for body in ("", "α" * (OPPORTUNITY_NOTE_MAX_LENGTH + 1)):
            with self.assertRaises(IntegrityError), transaction.atomic():
                OpportunityNote.objects.create(organization=self.org, opportunity=self.row, author=membership, body=body)
        OpportunityNote.objects.create(organization=self.org, opportunity=self.row, author=membership,
                                       body="α" * OPPORTUNITY_NOTE_MAX_LENGTH)
        self.assertEqual(OpportunityNote.objects.count(), 1)


# --- who may write ---------------------------------------------------------------------------------

class AuthorizationTests(NoteTestCase):
    def test_owner_and_sales_manager_write_on_any_visible_live_row(self):
        for role in ("owner", "sales_manager"):
            response = self.post_note(self.members[role], f"Σημείωση {role}")
            self.assertRedirects(response, self.url(), fetch_redirect_response=False)
            note = OpportunityNote.objects.get(body=f"Σημείωση {role}")
            self.assertEqual((note.organization_id, note.opportunity_id, note.author_id),
                             (self.org.pk, self.row.pk, self.membership(self.members[role]).pk))
        self.assertIsNone(self.stored()["assigned_to_id"])  # no assignment required for managers

    def test_a_sales_user_writes_only_on_their_own_assignment(self):
        theirs = self.live_opportunity(self.full_radar("Radar Νίκου", legal_forms=()))
        unassigned = self.live_opportunity(self.full_radar("Radar κανενός", signal_types=()))
        self.put_assigned(self.maria)
        self.put_assigned(self.nikos, row=theirs)
        result = self.note("Μίλησα με τον υπεύθυνο", actor=self.maria_user)
        self.assertEqual((result.author_membership_id, result.opportunity_id), (self.maria.pk, self.row.pk))
        for row in (theirs, unassigned):
            for body in ("κείμενο", ""):  # an invalid body changes nothing about the answer: access comes first
                self.assertEqual(self.post_note(self.maria_user, body, row=row).status_code, 404)
            self.assertRaises(OrganizationAccessDenied, self.note, "x", row, self.maria_user)
        self.assertEqual(self.post_note(self.members["sales_user"], "Νίκος εδώ").status_code, 404)  # Maria's row
        self.assertEqual(list(OpportunityNote.objects.values_list("body", flat=True)), ["Μίλησα με τον υπεύθυνο"])

    def test_admin_and_viewer_read_notes_but_cannot_write(self):
        self.note("Ορατή σε όλους τους αναγνώστες")
        for role in ("admin", "viewer"):
            html = self.get(self.members[role]).content.decode()
            self.assertIn("Ορατή σε όλους τους αναγνώστες", html, role)
            self.assertNotIn("data-note-form-for", html, role)
            self.assertNotIn("/notes/", html, role)
            self.assertEqual(self.post_note(self.members[role], "όχι").status_code, 404, role)
            self.assertRaises(OrganizationAccessDenied, self.note, "όχι", None, self.members[role])
        self.assertEqual(OpportunityNote.objects.count(), 1)

    def test_non_member_foreign_missing_and_shadow_are_the_same_404(self):
        shadow_company = Company.objects.create(gemi_number="900600", name="Σκιώδης ΑΕ", incorporation_date=date(2026, 9, 1))
        snapshot(shadow_company, T0)
        shadow = self.live_opportunity(self.full_radar("Shadow"), company=shadow_company, mode=SHADOW)
        foreign = self.live_opportunity(self.radar_b_foreign, company=Company.objects.create(
            gemi_number="900700", name="Ξένη ΑΕ", incorporation_date=date(2026, 9, 1)))
        missing = Opportunity.objects.order_by("-pk").first().pk + 1
        outsider = User.objects.create_user("outsider-note@example.com", "outsider-note@example.com", "x")
        responses = [self.post_note(self.owner, row=shadow), self.post_note(self.owner, row=foreign),
                     self.post_note(self.owner, row=foreign, org=self.org_b), self.post_note(self.b_owner),
                     self.post_note(outsider)]
        self.client.force_login(self.owner)
        responses.append(self.client.post(f"/organizations/{self.org.pk}/opportunities/{missing}/notes/", {"body": "x"}))
        responses.append(self.client.post(f"/organizations/999999/opportunities/{self.row.pk}/notes/", {"body": "x"}))
        self.assertEqual({response.status_code for response in responses}, {404})
        self.assertEqual({response.content for response in responses[1:]}, {responses[0].content})
        self.assertEqual(OpportunityNote.objects.count(), 0)

    def test_terminal_opportunities_still_take_notes_and_keep_their_status(self):
        for terminal in sorted(TERMINAL_STATUSES):
            self.set_status(terminal)
            self.assertEqual(self.post_note(self.owner, f"Κλείσιμο: {terminal}").status_code, 302)
            self.assertEqual(self.status(), terminal)
        self.assertEqual(OpportunityNote.objects.filter(opportunity=self.row).count(), len(TERMINAL_STATUSES))

    def test_one_user_in_two_organizations_writes_as_each_exact_membership(self):
        add_organization_member(self.org_b, self.maria_user, "sales_manager")
        b_company = Company.objects.create(gemi_number="900800", name="Εταιρεία Β", incorporation_date=date(2026, 9, 1))
        b_row = self.live_opportunity(self.radar_b_foreign, company=b_company)
        self.put_assigned(self.maria)
        in_a = self.note("Στον Α", actor=self.maria_user)
        in_b = self.note("Στον Β", row=b_row, actor=self.maria_user, org=self.org_b)
        self.assertEqual((in_a.author_membership_id, in_b.author_membership_id),
                         (self.maria.pk, self.membership(self.maria_user, self.org_b).pk))
        self.assertEqual(OpportunityNote.objects.get(body="Στον Β").organization_id, self.org_b.pk)
        # a route of one organization never reaches the other's row, whatever her role there
        self.assertRaises(OrganizationAccessDenied, self.note, "x", b_row, self.maria_user)
        self.assertRaises(OrganizationAccessDenied, self.note, "x", self.row, self.maria_user, self.org_b)
        self.assertNotIn("Στον Β", self.get(self.maria_user).content.decode())

    def test_organization_opportunity_and_author_never_come_from_the_request(self):
        self.assertEqual(list(inspect.signature(add_authorized_opportunity_note).parameters),
                         ["user", "organization_id", "opportunity_id", "body"])
        other = self.membership(self.members["viewer"])
        self.client.force_login(self.owner)
        self.client.post(self.note_url(), {"body": "Κανονική", "author": other.pk, "author_id": other.pk,
                                           "organization": self.org_b.pk, "organization_id": self.org_b.pk,
                                           "opportunity": 1, "company": 1})
        note = OpportunityNote.objects.get()
        self.assertEqual((note.organization_id, note.opportunity_id, note.author_id),
                         (self.org.pk, self.row.pk, self.membership(self.owner).pk))


# --- the body contract -------------------------------------------------------------------------------

class BodyTests(NoteTestCase):
    def test_ends_are_trimmed_and_inner_lines_are_kept(self):
        result = self.note("  Κλήση στις 10:00\r\n\r\n   Ξανά  την Παρασκευή\t \n ")
        self.assertEqual(OpportunityNote.objects.get(pk=result.note_id).body, "Κλήση στις 10:00\n\n   Ξανά  την Παρασκευή")

    def test_blank_whitespace_missing_and_non_text_bodies_are_refused_without_a_row(self):
        for body in ("", "   ", "\n\t \r\n", None, ["Κλήση"], 42):
            with self.assertRaises(NoteRefused) as refused:
                self.note(body)
            self.assertEqual(refused.exception.reason, "blank", repr(body))
        for body in ("", "   ", None):
            response = self.post_note(self.owner, body, follow=True)
            self.assertIn(NoteRefused.MESSAGES["blank"], response.content.decode())
        self.assertEqual(OpportunityNote.objects.count(), 0)

    def test_the_length_limit_is_4000_characters_after_trimming(self):
        self.assertEqual(normalize_note_body("α" * 4000), ("α" * 4000, None))
        self.assertEqual(normalize_note_body("  " + "α" * 4000 + "\n"), ("α" * 4000, None))
        self.assertEqual(normalize_note_body("α" * 4001), (None, "too_long"))
        self.assertTrue(self.note("α" * 4000).note_id)
        with self.assertRaises(NoteRefused) as refused:
            self.note("β" * 4001)
        self.assertEqual(refused.exception.reason, "too_long")
        response = self.post_note(self.owner, "β" * 4001, follow=True)
        self.assertIn(NoteRefused.MESSAGES["too_long"], response.content.decode())
        self.assertEqual(OpportunityNote.objects.count(), 1)

    def test_a_nul_character_is_refused_rather_than_breaking_postgresql(self):
        with self.assertRaises(NoteRefused) as refused:
            self.note("Κλήση\x00")
        self.assertEqual(refused.exception.reason, "invalid")
        self.assertEqual(OpportunityNote.objects.count(), 0)

    def test_the_same_text_twice_is_two_notes(self):
        for _ in range(2):
            self.assertEqual(self.post_note(self.owner, "Κλήση, καμία απάντηση").status_code, 302)
        self.assertEqual(OpportunityNote.objects.filter(body="Κλήση, καμία απάντηση").count(), 2)
        self.assertFalse([c for c in OpportunityNote._meta.constraints if "unique" in type(c).__name__.lower()])


# --- the page ------------------------------------------------------------------------------------------

class PageTests(NoteTestCase):
    def test_notes_sit_under_their_own_opportunity_newest_first(self):
        row_b = self.live_opportunity(self.full_radar("Radar Β", legal_forms=()))
        moments = [datetime(2026, 9, 18, hour, tzinfo=dt_timezone.utc) for hour in (8, 9, 9, 11)]
        for body, row, moment in (("Α-πρώτη", self.row, moments[0]), ("Α-δεύτερη", self.row, moments[1]),
                                  ("Α-τρίτη", self.row, moments[2]), ("Β-μόνη", row_b, moments[3])):
            with patch("django.utils.timezone.now", return_value=moment):
                self.note(body, row=row)
        html = self.get(self.owner).content.decode()
        block_a, block_b = self.row_block(html, self.row), self.row_block(html, row_b)
        self.assertEqual(re.findall(r"Α-\w+", block_a), ["Α-τρίτη", "Α-δεύτερη", "Α-πρώτη"])  # same time: -id
        self.assertNotIn("Β-μόνη", block_a)
        self.assertIn("Β-μόνη", block_b)
        self.assertNotIn("Α-", block_b)
        self.assertIn(f'data-note-form-for="{self.row.pk}"', block_a)
        self.assertIn(f'data-note-form-for="{row_b.pk}"', block_b)
        self.assertEqual(list(OpportunityNote.objects.filter(opportunity=self.row).values_list("body", flat=True)),
                         ["Α-τρίτη", "Α-δεύτερη", "Α-πρώτη"])

    def test_authors_are_shown_by_name_by_membership_number_or_as_former_member(self):
        self.put_assigned(self.maria)
        self.note("από τη Μαρία", actor=self.maria_user)
        self.note("από τον ιδιοκτήτη")
        html = self.get(self.owner).content.decode()
        self.assertIn("Μαρία Παππά", html)
        self.assertIn(f"Πωλητής #{self.membership(self.owner).pk}", html)  # the owner has no name set
        for address in (MARIA_EMAIL, "sales_user@a.example.com"):
            self.assertNotIn(address, html)
        self.maria.delete()
        self.assertIn(FORMER_MEMBER_LABEL, self.get(self.owner).content.decode())

    def test_the_page_shows_at_most_fifty_notes_with_a_neutral_notice(self):
        rows = [self.row] + [self.live_opportunity(self.full_radar(f"Radar {i}", legal_forms=())) for i in range(2)]
        authors = [self.membership(self.owner), self.membership(self.members["sales_manager"]), self.maria]
        OpportunityNote.objects.bulk_create([
            OpportunityNote(organization=self.org, opportunity=rows[i % 3], author=authors[i % 3], body=f"n{i:02d}")
            for i in range(50)])
        html = self.get(self.owner).content.decode()
        self.assertEqual(html.count("data-note="), 50)
        self.assertNotIn("data-notes-truncated", html)
        OpportunityNote.objects.create(organization=self.org, opportunity=self.row, author=authors[0], body="n50")
        html = self.get(self.owner).content.decode()
        self.assertEqual(html.count("data-note="), 50)
        self.assertIn("data-notes-truncated", html)
        self.assertIn("n50", html)
        self.assertNotIn(">n00<", html)  # the oldest is the one left out (same timestamps order by -id)
        self.assertEqual(OpportunityNote.objects.count(), 51)  # the database keeps them all

    def test_notes_are_read_in_one_bounded_query(self):
        rows = [self.row] + [self.live_opportunity(self.full_radar(f"Radar {i}", legal_forms=())) for i in range(2)]
        with CaptureQueriesContext(connection) as empty:
            self.page()
        authors = [self.membership(self.owner), self.membership(self.members["sales_manager"]), self.maria, None]
        OpportunityNote.objects.bulk_create([
            OpportunityNote(organization=self.org, opportunity=rows[i % 3], author=authors[i % 4], body=f"n{i}")
            for i in range(60)])
        with CaptureQueriesContext(connection) as full:
            page = self.page()
        self.assertEqual(len(full), len(empty))  # the note count never changes the query count
        # 3 opportunities, 60 notes, 4 authors (one former): measured on SQLite; D34 added two constant task queries
        self.assertEqual(len(full), 18)  # D35 added one constant suppression read
        note_queries = [q["sql"] for q in full.captured_queries if "opportunitynote" in q["sql"]]
        self.assertEqual(len(note_queries), 1)
        self.assertIn("LIMIT 51", note_queries[0])
        self.assertNotIn('"email"', note_queries[0])
        self.assertEqual(sum(len(o.notes) for o in page.opportunities), 50)
        self.assertTrue(page.notes_truncated)

    def test_a_sales_user_sees_and_writes_only_their_own_opportunity_notes(self):
        sibling = self.live_opportunity(self.full_radar("Radar Κρυφό Νίκου", legal_forms=()))
        self.put_assigned(self.maria)
        self.put_assigned(self.nikos, row=sibling)
        self.note("Σημείωση Μαρίας", actor=self.maria_user)
        self.note("Μυστική σημείωση Νίκου", row=sibling, actor=self.members["sales_user"])
        self.note("Διευθυντής για Νίκο", row=sibling, actor=self.members["sales_manager"])
        html = self.get(self.maria_user).content.decode()
        self.assertIn("Σημείωση Μαρίας", html)
        for hidden in ("Μυστική σημείωση Νίκου", "Διευθυντής για Νίκο", "Radar Κρυφό Νίκου",
                       f'data-opportunity="{sibling.pk}"', f'data-notes-for="{sibling.pk}"'):
            self.assertNotIn(hidden, html, hidden)
        self.assertEqual(html.count("data-note="), 1)
        self.assertEqual(html.count("data-note-form-for="), 1)
        self.assertEqual(self.post_note(self.maria_user, "εισβολή", row=sibling).status_code, 404)
        self.assertEqual(OpportunityNote.objects.filter(opportunity=sibling).count(), 2)

    def test_note_text_is_escaped_plain_text(self):
        self.note("<script>alert(1)</script>\n<b>έντονα</b>")
        html = self.get(self.owner).content.decode()
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)", html)
        self.assertNotIn("<b>έντονα</b>", html)
        template = open("templates/organizations/company_opportunity.html", encoding="utf-8").read()
        self.assertIn("white-space: pre-wrap", template)
        self.assertNotIn("|safe", template)
        self.assertNotIn("autoescape off", template)

    def test_reading_the_page_writes_nothing(self):
        self.note("υπάρχουσα")
        before = (list(OpportunityNote.objects.values()), self.stored())
        for who in (self.owner, self.members["viewer"], self.members["admin"]):
            self.client.force_login(who)
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(self.url()).status_code, 200)
            self.assertFalse([sql for sql in self.writes(queries) if "gemiapp_opportunity" in sql], who.email)
        self.assertEqual((list(OpportunityNote.objects.values()), self.stored()), before)


# --- membership lifecycle ------------------------------------------------------------------------------

class MembershipTests(NoteTestCase):
    def test_a_removed_member_leaves_the_note_and_regains_nothing(self):
        self.put_assigned(self.maria)
        result = self.note("Γράφτηκε από τη Μαρία", actor=self.maria_user)
        self.maria.delete()
        note = OpportunityNote.objects.get(pk=result.note_id)
        self.assertEqual((note.author_id, note.body, note.opportunity_id), (None, "Γράφτηκε από τη Μαρία", self.row.pk))
        self.assertTrue(Opportunity.objects.filter(pk=self.row.pk).exists())
        self.assertEqual(self.get(self.maria_user).status_code, 404)
        self.assertEqual(self.post_note(self.maria_user, "ακόμα εδώ;").status_code, 404)
        again = add_organization_member(self.org, self.maria_user, "sales_user")
        self.assertNotEqual(again.pk, self.maria.pk)
        note.refresh_from_db()
        self.assertIsNone(note.author_id)
        self.assertEqual(self.get(self.maria_user).status_code, 404)  # not the assignee either
        html = self.get(self.owner).content.decode()
        self.assertIn(FORMER_MEMBER_LABEL, html)
        self.assertNotIn("Μαρία Παππά", self.row_block(html, self.row).split("data-notes-for", 1)[1])


class MembershipRaceTests(NoteTestCase):
    """The acting membership is re-read and locked inside the note transaction. SQLite serialises writers, so these
    tests reproduce each interleaving deterministically; they prove the behaviour, not PostgreSQL's row locks."""

    def resolve_then(self, action):
        """Resolve the access context normally, then run ``action`` before the locked mutation begins -- the
        window a concurrent membership deletion would hit."""
        real = g5.get_organization_access_context

        def resolving(user, organization):
            context = real(user, organization)
            action()
            return context

        return patch.object(g5, "get_organization_access_context", side_effect=resolving)

    def test_a_membership_deleted_before_the_locked_write_is_a_safe_denial_without_a_note(self):
        self.put_assigned(self.maria)
        with self.resolve_then(lambda: OrganizationMember.objects.filter(pk=self.maria.pk).delete()):
            self.assertRaises(OrganizationAccessDenied, self.note, "αργά", None, self.maria_user)
        self.assertEqual(OpportunityNote.objects.count(), 0)
        again = add_organization_member(self.org, self.maria_user, "sales_manager")
        self.client.force_login(self.maria_user)
        with self.resolve_then(lambda: OrganizationMember.objects.filter(pk=again.pk).delete()):
            response = self.client.post(self.note_url(), {"body": "αργά"})
        self.assertEqual(response.status_code, 404)  # the same denial as every inaccessible target, never a 500
        self.assertEqual(OpportunityNote.objects.count(), 0)
        self.assertFalse(OpportunityNote.objects.filter(author__isnull=True).exists())  # never author=NULL on create

    def test_a_membership_whose_role_changed_before_the_locked_write_is_denied(self):
        manager = self.membership(self.members["sales_manager"])
        with self.resolve_then(lambda: OrganizationMember.objects.filter(pk=manager.pk).update(role="viewer")):
            self.assertRaises(OrganizationAccessDenied, self.note, "x", None, self.members["sales_manager"])
        self.assertEqual(OpportunityNote.objects.count(), 0)

    def test_an_existing_membership_writes_with_its_own_author_and_is_locked_after_the_opportunity(self):
        self.put_assigned(self.maria)
        result = self.note("εντάξει", actor=self.maria_user)
        self.assertEqual(OpportunityNote.objects.get(pk=result.note_id).author_id, self.maria.pk)
        source = inspect.getsource(add_authorized_opportunity_note)
        opportunity_lock = source.index('select_for_update(of=("self",))')
        membership_lock = source.index('_model("OrganizationMember").objects.select_for_update()')
        self.assertLess(opportunity_lock, membership_lock)  # one lock order: opportunity, then membership
        self.assertLess(membership_lock, source.index("OpportunityNote\").objects.create("))
        self.assertIn("author_id=author", source)  # the locked row's id, not the stale context value

    def test_a_membership_deleted_after_the_note_leaves_it_with_a_cleared_author(self):
        result = self.note("πριν τη διαγραφή", actor=self.members["sales_manager"])
        self.membership(self.members["sales_manager"]).delete()
        self.assertEqual(OpportunityNote.objects.filter(pk=result.note_id).values_list("author_id", "body").get(),
                         (None, "πριν τη διαγραφή"))

    def test_a_foreign_key_failure_at_commit_is_the_same_denial_not_a_server_error(self):
        with patch.object(OpportunityNote.objects, "create", side_effect=IntegrityError("author fk")):
            self.assertRaises(OrganizationAccessDenied, self.note, "x")
            self.client.force_login(self.owner)
            self.assertEqual(self.client.post(self.note_url(), {"body": "x"}).status_code, 404)
        self.assertEqual(OpportunityNote.objects.count(), 0)


# --- nothing else changes ------------------------------------------------------------------------------

class InvarianceTests(NoteTestCase):
    def test_status_assignment_score_breakdown_timeline_and_feed_are_untouched(self):
        self.put_assigned(self.maria)
        sibling = self.live_opportunity(self.full_radar("Radar Β", legal_forms=()))
        before = (self.stored(), self.stored(sibling), self.frozen(), self.frozen(sibling),
                  get_authorized_opportunity_score_breakdown(self.owner, self.org, self.row.pk),
                  list(CompanySignal.objects.values()), list(CompanySnapshot.objects.values()),
                  self.page().timeline, get_opportunity_feed(self.org).cards)
        self.note("Κάλεσα τον πελάτη", actor=self.maria_user)  # «called» -- and still no automatic CONTACTED
        self.note("Follow up την Παρασκευή")
        after = (self.stored(), self.stored(sibling), self.frozen(), self.frozen(sibling),
                 get_authorized_opportunity_score_breakdown(self.owner, self.org, self.row.pk),
                 list(CompanySignal.objects.values()), list(CompanySnapshot.objects.values()),
                 self.page().timeline, get_opportunity_feed(self.org).cards)
        self.assertEqual(after, before)  # status, assigned_to/at, updated_at and the whole capture included
        self.assertEqual(self.stored()["status"], "assigned")

    def test_the_endpoint_is_post_only_login_only_csrf_protected_and_redirects_to_the_page(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.note_url()).status_code, 405)
        anonymous = Client().post(self.note_url(), {"body": "x"})
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn(reverse("login"), anonymous["Location"])
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.owner)
        self.assertEqual(strict.post(self.note_url(), {"body": "x"}).status_code, 403)
        self.assertEqual(OpportunityNote.objects.count(), 0)
        response = self.client.post(self.note_url() + "?next=https://evil.example/",
                                    {"body": "Κανονική", "next": "https://evil.example/"})
        self.assertRedirects(response, self.url(), fetch_redirect_response=False)
        self.assertIn("Η σημείωση προστέθηκε.", self.client.get(self.url()).content.decode())

    def test_append_only_there_is_no_edit_or_delete_anywhere(self):
        def flatten(patterns, prefix=""):
            for pattern in patterns:
                if hasattr(pattern, "url_patterns"):
                    yield from flatten(pattern.url_patterns, prefix + str(pattern.pattern))
                else:
                    yield prefix + str(pattern.pattern)

        # the legacy «leads/<pk>/notes/» route (UserCompanyLead) is a separate, untouched product
        routes = [route for route in flatten(get_resolver().url_patterns) if "note" in route and "organization" in route]
        self.assertEqual(routes, ["organizations/<int:organization_id>/opportunities/<int:opportunity_id>/notes/"])
        code = inspect.getsource(g5) + inspect.getsource(organization_views)
        for forbidden in ("def edit_", "def delete_", "def update_note", "def remove_", "note.delete", "note.save(",
                          ".notes.all().delete", "require_http_methods"):
            self.assertNotIn(forbidden, code, forbidden)
        self.note("μένει")
        self.client.force_login(self.owner)
        note = OpportunityNote.objects.get()
        for suffix in (f"{note.pk}/edit/", f"{note.pk}/delete/", f"{note.pk}/"):
            self.assertEqual(self.client.post(self.note_url() + suffix).status_code, 404, suffix)
        self.assertEqual(self.client.delete(self.note_url()).status_code, 405)
        model_admin = admin.site._registry[OpportunityNote]
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser("staff-note@example.com", "staff-note@example.com", "x")
        self.assertEqual((model_admin.has_add_permission(request), model_admin.has_change_permission(request, note),
                          model_admin.has_delete_permission(request, note)), (False, False, False))
        self.assertEqual(OpportunityNote.objects.get().body, "μένει")

    def test_nothing_is_injected_and_nobody_is_notified(self):
        Company.objects.filter(pk=self.company.pk).update(raw_data={"phone": "2109990001", "email": "info@x.gr",
                                                                    "persons": [{"name": "Γιώργος"}]})
        mail.outbox = []
        result = self.note("Κλήση")
        self.assertEqual(OpportunityNote.objects.get(pk=result.note_id).body, "Κλήση")  # exactly as typed, nothing added
        self.assertEqual(len(mail.outbox), 0)
        source = inspect.getsource(add_authorized_opportunity_note) + inspect.getsource(normalize_note_body)
        for forbidden in ("raw_data", "gemi_phones", "email", "persons", "send_mail", "notify", "openai", "anthropic",
                          "AuditLog", "timeline"):
            self.assertNotIn(forbidden, source, forbidden)
        from django.apps import apps

        names = {model.__name__ for model in apps.get_app_config("gemiapp").get_models()}
        self.assertEqual({n for n in names if "Audit" in n}, {"AdminAuditLog", "OrganizationAuditEvent"})  # D36
        self.assertFalse([n for n in names if "Notification" in n or "Timeline" in n])
        self.assertEqual({n for n in names if "Task" in n}, {"OpportunityTask"})  # D34's own table, not a note

    def test_the_legacy_product_billing_and_phone_are_untouched(self):
        legacy_user = entitled_user("legacy-d33@example.com")
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
            self.assertEqual(self.post_note(self.maria_user, "Σημείωση").status_code, 302)
            self.assertEqual(world(), before)
        client.assert_not_called()
        self.assertEqual([p.tel_uri for p in Company(gemi_number="1", raw_data={"phone": "+30 2109990001"}).gemi_phones],
                         ["tel:+302109990001"])
        self.assertEqual([p.display for p in extract_company_contact_phones({"phone": "2109990001"})], ["2109990001"])
