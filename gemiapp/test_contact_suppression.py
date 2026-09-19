"""Tests for D35 Do Not Contact (blueprint §51 «contact_suppressions», §39 DO_NOT_CONTACT, Phase D item 35).

D35 v1 suppresses a *company* for one organization: one OrganizationContactSuppression (contact_type "company",
contact_value = normalized GEMI number, required reason, source "manual", creator membership) and every opportunity
the organization has for that company becomes DO_NOT_CONTACT. OWNER and SALES_MANAGER only; no unsuppress; other
organizations, assignments, notes, tasks, scores and signals are untouched. ``is_contact_suppressed`` is the one
enforcement read future contact workflows must call.
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
from django.urls import reverse

from . import organization_access as g5
from . import organization_views
from .company_contact import extract_company_contact_phones
from .company_signals import LIVE, SHADOW
from .ingestion.monitoring import radar_match_evidence
from .ingestion.refresh import RefreshPolicy, build_company_refresh_plan
from .models import (
    Company, CompanyMonitoring, CompanySignal, CompanySnapshot, CustomerRadar, DigestDelivery, Opportunity,
    OpportunityNote, OpportunityTask, OrganizationContactSuppression, OrganizationMember, OutreachSuppression,
    PersonSuppression, RadarMatch, UserCompanyLead, UserSubscription,
)
from .opportunity_feed import FeedFilters, get_opportunity_feed
from .organization_access import (
    DNC_COMPANY_REASONS, AssignmentRefused, DoNotContactRefused, OpportunityTransitionRefused,
    OrganizationAccessDenied, StatusChangeRefused, apply_authorized_company_do_not_contact,
    company_suppression_value, get_authorized_opportunity_feed, get_authorized_opportunity_score_breakdown,
    is_company_suppressed, is_contact_suppressed, save_authorized_opportunity,
)
from .organizations import add_organization_member
from .services import company_matches_radar, eligible_radars, send_digests
from .test_gemi_company_activities import entitled_user, radar_for
from .test_opportunity_tasks import TaskTestCase
from . import opportunities as c8
from .test_organization_radar_matching import T0, new_company_signal, snapshot
from .test_opportunity_scoring import FRESH

MOMENT = datetime(2026, 9, 18, 10, tzinfo=dt_timezone.utc)
Suppression = OrganizationContactSuppression


class DncTestCase(TaskTestCase):
    def dnc(self, actor=None, reason="explicit_objection", confirmed=True, company=None, org=None):
        return apply_authorized_company_do_not_contact(actor or self.owner, (org or self.org).pk,
                                                       (company or self.company).pk, reason, confirmed)

    def dnc_url(self, company=None, org=None):
        return reverse("organization_company_do_not_contact",
                       kwargs={"organization_id": (org or self.org).pk, "company_id": (company or self.company).pk})

    def post_dnc(self, who, reason="explicit_objection", confirm="yes", company=None, org=None, **extra):
        self.client.force_login(who)
        data = {k: v for k, v in (("reason", reason), ("confirm", confirm)) if v is not None}
        return self.client.post(self.dnc_url(company, org), data, **extra)

    def statuses(self, *rows):
        return [Opportunity.objects.get(pk=row.pk).status for row in rows]

    def gemi(self, company=None):
        return (company or self.company).gemi_number

    def reset(self):
        Suppression.objects.all().delete()
        Opportunity.objects.update(status="new")


# --- schema --------------------------------------------------------------------------------------------

class SchemaTests(DncTestCase):
    def test_the_model_and_its_constraints(self):
        fields = {f.name for f in Suppression._meta.get_fields()}
        self.assertEqual(fields, {"id", "organization", "contact_type", "contact_value", "reason", "source",
                                  "created_by", "created_at"})
        self.assertEqual(Suppression._meta.get_field("organization").remote_field.on_delete.__name__, "CASCADE")
        self.assertEqual(Suppression._meta.get_field("created_by").remote_field.on_delete.__name__, "SET_NULL")
        self.assertIs(Suppression._meta.get_field("created_by").related_model, OrganizationMember)
        self.assertEqual([c for c, _ in Suppression.CONTACT_TYPES], ["company"])  # email / phone: Phase G
        self.assertEqual([c for c, _ in Suppression.SOURCES], ["manual"])
        self.assertEqual([c for c, _ in Suppression.REASONS], ["explicit_objection", "email_unsubscribe",
                                                                "call_objection", "compliance_registry", "manual"])
        self.assertEqual(DNC_COMPANY_REASONS, ("explicit_objection", "call_objection", "compliance_registry", "manual"))
        self.assertEqual(Suppression.objects.count(), 0)

    def test_the_migration_is_one_additive_create_model(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        # D35 owns 0052; D36's 0053 (audit log) follows it.
        self.assertEqual(loader.disk_migrations[("gemiapp", "0053_organization_audit_event")].dependencies,
                         [("gemiapp", "0052_organization_contact_suppression")])
        migration = loader.disk_migrations[("gemiapp", "0052_organization_contact_suppression")]
        self.assertEqual(migration.dependencies, [("gemiapp", "0051_opportunity_task")])
        self.assertEqual([(type(op).__name__, op.name) for op in migration.operations],
                         [("CreateModel", "OrganizationContactSuppression")])

    def test_the_database_refuses_duplicates_and_invalid_identities(self):
        base = dict(organization=self.org, contact_type="company", contact_value="400100", reason="manual",
                    source="manual")
        Suppression.objects.create(**base)
        for extra in ({}, {"contact_type": "email", "contact_value": "x@y.gr"}, {"contact_value": "", "reason": "manual"},
                      {"contact_value": "1", "reason": "email_unsubscribe"}, {"contact_value": "2", "reason": "spam"},
                      {"contact_value": "3", "source": "import"}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Suppression.objects.create(**{**base, **extra})
        Suppression.objects.create(**{**base, "organization": self.org_b})  # another organization: its own row
        self.assertEqual(Suppression.objects.count(), 2)


# --- who may apply it, and through which entry point ------------------------------------------------

class AuthorizationTests(DncTestCase):
    def test_owner_and_sales_manager_may_apply_it(self):
        for role in ("sales_manager", "owner"):
            self.reset()
            response = self.post_dnc(self.members[role])
            self.assertRedirects(response, self.url(), fetch_redirect_response=False)
            row = Suppression.objects.get()
            self.assertEqual((row.organization_id, row.contact_type, row.contact_value, row.reason, row.source,
                              row.created_by_id), (self.org.pk, "company", self.gemi(), "explicit_objection", "manual",
                                                   self.m(role).pk))
            self.assertEqual(self.status(), "do_not_contact")

    def test_admin_viewer_sales_user_and_non_members_are_the_same_404(self):
        self.put_assigned(self.maria)  # even her own assigned opportunity grants nothing here
        outsider = User.objects.create_user("outsider-dnc@example.com", "outsider-dnc@example.com", "x")
        responses = [self.post_dnc(who) for who in (self.members["admin"], self.members["viewer"], self.maria_user,
                                                    self.members["sales_user"], outsider, self.b_owner)]
        self.assertEqual({r.status_code for r in responses}, {404})
        self.assertEqual({r.content for r in responses[1:]}, {responses[0].content})
        self.assertRaises(OrganizationAccessDenied, self.dnc, self.maria_user)
        self.assertEqual((Suppression.objects.count(), self.status()), (0, "assigned"))

    def test_foreign_missing_and_shadow_only_companies_are_the_same_404(self):
        foreign = Company.objects.create(gemi_number="902100", name="Ξένη ΑΕ", incorporation_date=date(2026, 9, 1))
        self.live_opportunity(self.radar_b_foreign, company=foreign)
        shadow_only = Company.objects.create(gemi_number="902200", name="Σκιώδης ΑΕ", incorporation_date=date(2026, 9, 1))
        snapshot(shadow_only, T0)
        shadow_row = self.live_opportunity(self.full_radar("Shadow"), company=shadow_only, mode=SHADOW)
        no_opportunity = Company.objects.create(gemi_number="902300", name="Κενή ΑΕ", incorporation_date=date(2026, 9, 1))
        self.client.force_login(self.owner)
        missing = Company.objects.order_by("-pk").first().pk + 1
        responses = [self.post_dnc(self.owner, company=foreign), self.post_dnc(self.owner, company=shadow_only),
                     self.post_dnc(self.owner, company=no_opportunity),
                     self.post_dnc(self.owner, company=self.company, org=self.org_b),
                     self.client.post(f"/organizations/{self.org.pk}/opportunities/company/{missing}/do-not-contact/",
                                      {"reason": "manual", "confirm": "yes"}),
                     self.client.post(f"/organizations/999999/opportunities/company/{self.company.pk}/do-not-contact/",
                                      {"reason": "manual", "confirm": "yes"})]
        self.assertEqual({r.status_code for r in responses}, {404})
        self.assertEqual({r.content for r in responses[1:]}, {responses[0].content})
        self.assertEqual((Suppression.objects.count(), self.statuses(shadow_row)), (0, ["new"]))

    def test_a_live_entry_point_also_settles_hidden_shadow_rows_without_revealing_them(self):
        shadow_row = self.live_opportunity(self.full_radar("Μυστικό Shadow Radar", legal_forms=()), mode=SHADOW)
        response = self.post_dnc(self.owner, follow=True)
        self.assertEqual(self.statuses(self.row, shadow_row), ["do_not_contact", "do_not_contact"])
        html = response.content.decode()
        self.assertNotIn("Μυστικό Shadow Radar", html)
        self.assertNotIn(f'data-opportunity="{shadow_row.pk}"', html)
        self.assertNotRegex(html, r"\b2 ευκαιρ")  # no count that would reveal the hidden row


# --- the request itself --------------------------------------------------------------------------------

class RequestTests(DncTestCase):
    def test_every_company_reason_is_accepted_and_email_unsubscribe_is_not(self):
        for reason in DNC_COMPANY_REASONS:
            self.reset()
            self.assertEqual(self.dnc(reason=reason).created, True)
            self.assertEqual(Suppression.objects.get().reason, reason)
        self.reset()
        for reason in ("email_unsubscribe", "", None, "spam", "MANUAL", " manual", ["manual"]):
            with self.assertRaises(DoNotContactRefused) as refused:
                self.dnc(reason=reason)
            self.assertEqual(refused.exception.reason, "reason", repr(reason))
        self.assertEqual((Suppression.objects.count(), self.status()), (0, "new"))

    def test_the_confirmation_is_required_by_the_server(self):
        for confirm in (None, "", "no", "on", "true", "YES"):
            response = self.post_dnc(self.owner, confirm=confirm, follow=True)
            self.assertIn(DoNotContactRefused.MESSAGES["confirm"], response.content.decode(), confirm)
        for confirmed in (False, "yes", 1, None):
            with self.assertRaises(DoNotContactRefused) as refused:
                self.dnc(confirmed=confirmed)
            self.assertEqual(refused.exception.reason, "confirm")
        self.assertEqual((Suppression.objects.count(), self.status()), (0, "new"))

    def test_type_value_source_creator_and_status_never_come_from_the_request(self):
        self.client.force_login(self.members["sales_manager"])
        self.client.post(self.dnc_url(), {"reason": "manual", "confirm": "yes", "source": "import",
                                          "contact_type": "email", "contact_value": "x@y.gr", "created_by": 1,
                                          "status": "won", "organization": self.org_b.pk})
        row = Suppression.objects.get()
        self.assertEqual((row.organization_id, row.contact_type, row.contact_value, row.source, row.created_by_id),
                         (self.org.pk, "company", self.gemi(), "manual", self.m("sales_manager").pk))
        self.assertEqual(list(inspect.signature(apply_authorized_company_do_not_contact).parameters),
                         ["user", "organization_id", "company_id", "reason", "confirmed"])

    def test_a_company_without_a_usable_gemi_number_is_refused(self):
        odd = Company.objects.create(gemi_number="ΓΕΜΗ-ΧΩΡΙΣ", name="Χωρίς αριθμό ΑΕ", incorporation_date=date(2026, 9, 1))
        snapshot(odd, T0)
        row = self.live_opportunity(self.full_radar("Radar odd"), company=odd)
        with self.assertRaises(DoNotContactRefused) as refused:
            self.dnc(company=odd)
        self.assertEqual(refused.exception.reason, "identity")
        self.assertEqual((Suppression.objects.count(), self.statuses(row)), (0, ["new"]))
        html = self.get(self.owner, company=odd).content.decode()
        self.assertIn('data-do-not-contact="unavailable"', html)
        self.assertNotIn("data-do-not-contact-form", html)

    def test_gemi_identity_normalization(self):
        self.assertEqual(company_suppression_value(" 400100 "), "400100")
        self.assertEqual(company_suppression_value("000123"), "000123")  # the repository's rule keeps the text form
        for bad in ("", None, "0", "-5", "12a", "ΓΕΜΗ", "1.5"):
            self.assertIsNone(company_suppression_value(bad), bad)
        from .ingestion.discovery import normalize_gemi_number

        self.assertIn("normalize_gemi_number", inspect.getsource(company_suppression_value))  # one rule, reused
        self.assertEqual(company_suppression_value("400100"), normalize_gemi_number("400100")[0])


# --- the company-wide effect -----------------------------------------------------------------------------

class EffectTests(DncTestCase):
    def test_every_opportunity_of_the_company_in_this_organization_and_nothing_else(self):
        self.put_assigned(self.maria)
        radar_b = self.live_opportunity(self.full_radar("Radar Β", legal_forms=()))
        radar_c = self.live_opportunity(self.full_radar("Radar Γ", signal_types=()))
        self.set_status("contacted", radar_b)
        self.set_status("won", radar_c)
        other_company = Company.objects.create(gemi_number="902400", name="Άλλη ΑΕ", incorporation_date=date(2026, 9, 1))
        snapshot(other_company, T0)
        other_row = self.live_opportunity(self.full_radar("Radar άλλης"), company=other_company)
        foreign_same_company = self.live_opportunity(self.radar_b_foreign)  # Organization B, the same company
        foreign_before = self.stored(foreign_same_company)
        result = self.dnc(actor=self.members["sales_manager"])
        self.assertEqual((result.created, result.opportunities_changed), (True, 3))
        self.assertEqual(self.statuses(self.row, radar_b, radar_c), ["do_not_contact"] * 3)
        self.assertEqual(Suppression.objects.count(), 1)
        self.assertEqual(self.statuses(other_row), ["new"])
        self.assertEqual(self.stored(foreign_same_company), foreign_before)  # Organization B: completely unchanged
        self.assertTrue(is_company_suppressed(self.org, self.company))
        self.assertFalse(is_company_suppressed(self.org_b, self.company))

    def test_assignment_notes_tasks_score_and_signals_are_preserved(self):
        self.put_assigned(self.maria)
        self.note("πριν τη μη επικοινωνία")
        open_task = self.task("ανοιχτή εργασία").task_id
        sibling = self.live_opportunity(self.full_radar("Radar Β", legal_forms=()))

        def world():
            return ({k: v for k, v in self.stored().items() if k not in ("status", "updated_at")},
                    {k: v for k, v in self.stored(sibling).items() if k not in ("status", "updated_at")},
                    self.frozen(), self.frozen(sibling), list(OpportunityNote.objects.values()),
                    list(OpportunityTask.objects.values()), list(CompanySignal.objects.values()),
                    list(CompanySnapshot.objects.values()),
                    get_authorized_opportunity_score_breakdown(self.owner, self.org, self.row.pk))

        before = world()
        self.dnc()
        self.assertEqual(world(), before)  # only status (and its updated_at) moved
        self.assertEqual((self.stored()["assigned_to_id"], self.stored()["assigned_at"]), (self.maria.pk, MOMENT))
        self.assertIsNone(self.stored_task(open_task)["completed_at"])  # tasks are never auto-completed

    def test_the_feed_keeps_the_rows_and_its_status_filter_finds_them(self):
        order = [c.primary_opportunity_id for c in get_opportunity_feed(self.org).cards]
        self.dnc()
        self.assertEqual([c.primary_opportunity_id for c in get_opportunity_feed(self.org).cards], order)
        card, = get_opportunity_feed(self.org, FeedFilters(statuses=("do_not_contact",))).cards
        self.assertEqual((card.primary_opportunity_id, card.status, card.score), (self.row.pk, "do_not_contact", 100))

    def test_d32_still_refuses_the_status_and_nothing_else_reopens_it(self):
        self.assertEqual(self.post_status(self.owner, "do_not_contact").status_code, 302)
        self.assertEqual(self.status(), "new")
        self.assertRaises(StatusChangeRefused, self.change, "do_not_contact")
        self.dnc()
        for target in ("contacted", "won", "not_relevant"):
            with self.assertRaises(StatusChangeRefused) as refused:
                self.change(target)
            self.assertEqual(refused.exception.reason, "terminal")
        self.assertRaises(OpportunityTransitionRefused, save_authorized_opportunity, self.owner, self.org.pk, self.row.pk)
        with self.assertRaises(AssignmentRefused):
            g5.assign_authorized_opportunity(self.owner, self.org.pk, self.row.pk, self.maria.pk)
        self.assertEqual(self.status(), "do_not_contact")
        code = inspect.getsource(g5) + inspect.getsource(organization_views)
        for forbidden in ("def unsuppress", "def remove_suppression", "def delete_suppression", ".delete()",
                          "def restore_contact"):
            self.assertNotIn(forbidden, code, forbidden)


# --- idempotency, concurrency, membership ----------------------------------------------------------

class IdempotencyTests(DncTestCase):
    def test_repeating_it_keeps_the_original_record_and_writes_nothing(self):
        self.live_opportunity(self.full_radar("Radar Β", legal_forms=()))
        with patch("django.utils.timezone.now", return_value=MOMENT):
            first = self.dnc(actor=self.members["sales_manager"], reason="call_objection")
        record = list(Suppression.objects.values())
        rows = list(Opportunity.objects.values())
        with CaptureQueriesContext(connection) as queries:
            second = self.dnc(actor=self.owner, reason="compliance_registry")  # another actor, another reason
        self.assertEqual((first.created, second.created, second.opportunities_changed, second.suppression_id),
                         (True, False, 0, first.suppression_id))
        self.assertEqual(self.writes(queries), [])
        self.assertEqual((list(Suppression.objects.values()), list(Opportunity.objects.values())), (record, rows))
        response = self.post_dnc(self.owner, follow=True)
        self.assertNotIn("καταχωρίστηκε", response.content.decode())

    def test_a_row_that_somehow_missed_the_status_is_converged_by_a_repeat(self):
        # Rows born after the suppression are already DO_NOT_CONTACT (FutureOpportunityTests); a repeat still
        # settles any row that is not, with one UPDATE and without touching the record.
        self.dnc()
        record = list(Suppression.objects.values())
        late = self.live_opportunity(self.full_radar("Radar μεταγενέστερο", legal_forms=()))
        Opportunity.objects.filter(pk=late.pk).update(status="new")
        with CaptureQueriesContext(connection) as queries:
            result = self.dnc(reason="manual")
        self.assertEqual((result.created, result.opportunities_changed), (False, 1))
        self.assertEqual([q for q in self.writes(queries) if q.upper().startswith("UPDATE")].__len__(), 1)
        self.assertEqual((self.statuses(late), list(Suppression.objects.values())), (["do_not_contact"], record))

    def test_a_concurrent_identical_insert_converges_on_the_existing_row(self):
        # Another manager's transaction committed the same suppression after our existence check: reproduce that
        # window by hiding the committed row from the first lookup only. Our insert then hits the real unique
        # constraint and the service converges on the committed row instead of failing.
        Suppression.objects.create(organization=self.org, contact_type="company", contact_value=self.gemi(),
                                   reason="compliance_registry", source="manual", created_by=self.m("sales_manager"))
        real_filter = Suppression.objects.filter
        calls = []

        def first_lookup_misses(*args, **kwargs):
            calls.append(kwargs)
            return real_filter(pk=-1) if len(calls) == 1 else real_filter(*args, **kwargs)

        with patch.object(Suppression.objects, "filter", side_effect=first_lookup_misses):
            result = self.dnc()
        row = Suppression.objects.get()
        self.assertEqual((result.created, result.suppression_id, row.reason, row.created_by_id),
                         (False, row.pk, "compliance_registry", self.m("sales_manager").pk))
        self.assertEqual(self.status(), "do_not_contact")  # every row still settled, no duplicate, no 500

    def test_a_membership_removed_before_the_locked_write_is_denied(self):
        manager = self.m("sales_manager")
        real = g5.get_organization_access_context

        def resolving(user, organization):
            context = real(user, organization)
            OrganizationMember.objects.filter(pk=manager.pk).delete()
            return context

        with patch.object(g5, "get_organization_access_context", side_effect=resolving):
            self.assertRaises(OrganizationAccessDenied, self.dnc, self.members["sales_manager"])
        self.assertEqual((Suppression.objects.count(), self.status()), (0, "new"))
        source = inspect.getsource(apply_authorized_company_do_not_contact)
        order = [source.index(m) for m in ('select_for_update(of=("self",))', "_lock_memberships(", ".create(",
                                            ".update(status=DO_NOT_CONTACT")]
        self.assertEqual(order, sorted(order))  # opportunities -> membership -> suppression -> status

    def test_the_suppression_outlives_its_creator_who_regains_nothing(self):
        self.dnc(actor=self.members["sales_manager"])
        self.m("sales_manager").delete()
        row = Suppression.objects.get()
        self.assertEqual((row.created_by_id, row.contact_value), (None, self.gemi()))
        add_organization_member(self.org, self.members["sales_manager"], "sales_manager")
        row.refresh_from_db()
        self.assertIsNone(row.created_by_id)
        self.assertTrue(is_company_suppressed(self.org, self.company))

    def test_query_count_does_not_grow_with_the_companys_rows(self):
        for index in range(2):
            self.live_opportunity(self.full_radar(f"Radar {index}", legal_forms=()))
        with CaptureQueriesContext(connection) as three:
            self.dnc()
        self.reset()
        for index in range(3):
            self.live_opportunity(self.full_radar(f"Radar extra {index}", signal_types=()))
        with CaptureQueriesContext(connection) as six:
            self.dnc()
        self.assertEqual(len(three), len(six))
        kinds = [q["sql"].split()[0].upper() for q in six.captured_queries]
        # one suppression row, its one D36 audit event, one bulk status update
        self.assertEqual((kinds.count("INSERT"), kinds.count("UPDATE")), (2, 1))
        with CaptureQueriesContext(connection) as repeat:
            self.dnc()
        # organization, membership, company lock, the company's rows, membership lock, suppression lookup
        self.assertEqual([q["sql"].split()[0].upper() for q in repeat.captured_queries].count("SELECT"), 6)


# --- D35 hardening: opportunities created after the suppression ------------------------------------

class FutureOpportunityTests(DncTestCase):
    def capture(self, row):
        stored = Opportunity.objects.filter(pk=row.pk).values(
            "score", "score_class", "score_rule_version", "match_rule_version", "scored_as_of",
            "primary_reason_code").get()
        components = list(row.score_components.order_by("position").values_list(
            "code", "awarded_points", "max_points", "reason_code", "position"))
        return stored, components

    def test_a_new_opportunity_of_a_suppressed_company_is_born_do_not_contact(self):
        self.dnc()
        record = list(Suppression.objects.values())
        later = [self.live_opportunity(self.full_radar(f"Radar μετά {i}", legal_forms=())) for i in range(3)]
        self.assertEqual(self.statuses(*later), ["do_not_contact"] * 3)  # several Radars, no manual re-apply
        self.assertEqual(list(Suppression.objects.values()), record)     # one row, never touched

    def test_an_unsuppressed_company_still_gets_new(self):
        self.assertEqual(self.statuses(self.live_opportunity(self.full_radar("Radar κανονικό", legal_forms=()))),
                         ["new"])

    def test_the_organization_decides_the_same_company_is_normal_elsewhere(self):
        self.dnc()
        in_a = self.live_opportunity(self.full_radar("Radar A νέο", legal_forms=()))
        in_b = self.live_opportunity(self.radar_b_foreign)  # Organization B, the same company
        self.assertEqual(self.statuses(in_a, in_b), ["do_not_contact", "new"])

    def test_the_match_score_and_breakdown_are_those_of_an_unsuppressed_materialization(self):
        self.dnc()
        radar_a = self.full_radar("Radar ίδιο")
        radar_b = self.radar(self.org_b, name="Radar ίδιο", **self.everything())
        signal = new_company_signal(self.company, T0, mode=LIVE)
        results = {r.opportunity.organization_id: r for r in c8.materialize_opportunities_for_signal(signal, as_of=FRESH)
                   if r.eligible and r.opportunity.radar_id in (radar_a.pk, radar_b.pk)}
        a, b = results[self.org.pk].opportunity, results[self.org_b.pk].opportunity
        self.assertEqual(self.statuses(a, b), ["do_not_contact", "new"])
        self.assertEqual(self.capture(a), self.capture(b))  # suppression changes the status, nothing else

    def test_a_recapture_never_rewrites_an_existing_status(self):
        self.dnc()
        radar = self.full_radar("Radar επανάληψης", legal_forms=())
        born = self.live_opportunity(radar)
        later = new_company_signal(self.company, T0 + timedelta(minutes=30), mode=LIVE)
        result = self.materialize(later, radar)
        self.assertEqual((result.created, result.signal_attached), (False, True))
        stored = Opportunity.objects.get(pk=born.pk)
        self.assertEqual((stored.status, stored.latest_signal_id), ("do_not_contact", later.pk))
        # Existing rows in any state are never touched by C8 -- with or without a suppression.
        Suppression.objects.all().delete()
        for status in ("won", "saved", "assigned", "contacted"):
            Opportunity.objects.filter(pk=self.row.pk).update(status=status)
            self.materialize(new_company_signal(self.company, T0 + timedelta(minutes=40), mode=LIVE),
                             Opportunity.objects.get(pk=self.row.pk).radar)
            self.assertEqual(self.status(), status)
        Suppression.objects.create(organization=self.org, contact_type="company", contact_value=self.gemi(),
                                   reason="manual", source="manual")
        self.materialize(new_company_signal(self.company, T0 + timedelta(minutes=50), mode=LIVE),
                         Opportunity.objects.get(pk=self.row.pk).radar)
        self.assertEqual(self.status(), "contacted")  # only the D35 action settles existing rows

    def test_the_feed_and_every_workflow_guard_see_it_as_do_not_contact(self):
        self.dnc()
        born = self.live_opportunity(self.full_radar("Radar νέο", legal_forms=()))
        cards = get_opportunity_feed(self.org, FeedFilters(statuses=("do_not_contact",))).cards
        self.assertIn(born.pk, [child.opportunity_id for card in cards for child in card.opportunities])
        self.assertFalse(get_opportunity_feed(self.org, FeedFilters(statuses=("new",))).cards)  # never NEW first
        self.assertRaises(OpportunityTransitionRefused, save_authorized_opportunity, self.owner, self.org.pk, born.pk)
        with self.assertRaises(AssignmentRefused):
            g5.assign_authorized_opportunity(self.owner, self.org.pk, born.pk, self.maria.pk)
        with self.assertRaises(StatusChangeRefused) as refused:
            self.change("contacted", row=born)
        self.assertEqual(refused.exception.reason, "terminal")
        html = self.row_block(self.get(self.owner).content.decode(), born)
        for control in ("data-save-for", "data-assign-for", "data-status-for"):
            self.assertNotIn(control, html, control)

    def test_no_gemi_identity_means_no_suppression_lookup_and_no_fallback(self):
        odd = Company.objects.create(gemi_number="ΓΕΜΗ-ΧΩΡΙΣ", name="Χωρίς αριθμό ΑΕ", incorporation_date=date(2026, 9, 1))
        snapshot(odd, T0)
        with CaptureQueriesContext(connection) as queries:
            row = self.live_opportunity(self.full_radar("Radar odd"), company=odd)
        self.assertEqual(self.statuses(row), ["new"])
        self.assertFalse([q for q in queries.captured_queries if "organizationcontactsuppression" in q["sql"]])

    def test_legacy_suppressions_do_not_influence_materialization(self):
        PersonSuppression.objects.create(full_name="Γιώργος Παπαδόπουλος", company=self.company)
        OutreachSuppression.objects.create(email="info@x.gr")
        before = (list(PersonSuppression.objects.values()), list(OutreachSuppression.objects.values()))
        row = self.live_opportunity(self.full_radar("Radar legacy", legal_forms=()))
        self.assertEqual(self.statuses(row), ["new"])
        self.assertEqual((list(PersonSuppression.objects.values()), list(OutreachSuppression.objects.values())), before)

    def test_c8_uses_the_canonical_helper_and_no_authorization_layer(self):
        from . import contact_suppressions

        code = inspect.getsource(c8)
        self.assertIn("from .contact_suppressions import is_company_suppressed", code)
        for forbidden in ("organization_access", "OrganizationAccessContext", "request.user", "normalize_gemi_number"):
            self.assertNotIn(forbidden, code.split('"""', 2)[2], forbidden)
        helper = inspect.getsource(contact_suppressions).split('"""', 2)[2]  # code after the module docstring
        for forbidden in ("organization_access", "request.", "PersonSuppression", "OutreachSuppression"):
            self.assertNotIn(forbidden, helper, forbidden)
        # the customer layer re-exports the very same functions: one implementation
        self.assertIs(g5.is_company_suppressed, contact_suppressions.is_company_suppressed)
        self.assertIs(g5.is_contact_suppressed, contact_suppressions.is_contact_suppressed)
        self.assertIs(g5.company_suppression_value, contact_suppressions.company_suppression_value)

    def test_the_added_c8_cost_is_two_queries_per_created_row_and_none_for_a_recapture(self):
        radar = self.full_radar("Radar κόστους", legal_forms=())
        signal = new_company_signal(self.company, T0, mode=LIVE)
        parts = self.parts(signal, radar)
        with CaptureQueriesContext(connection) as created:
            c8.materialize_opportunity(**parts)
        sql = [q["sql"] for q in created.captured_queries]
        self.assertEqual(len([q for q in sql if "organizationcontactsuppression" in q]), 1)
        self.assertEqual(len([q for q in sql if 'FROM "gemiapp_company"' in q]), 1)  # the company lock
        later = self.parts(new_company_signal(self.company, T0 + timedelta(minutes=5), mode=LIVE), radar)
        with CaptureQueriesContext(connection) as recaptured:
            c8.materialize_opportunity(**later)
        self.assertFalse([q for q in recaptured.captured_queries if "organizationcontactsuppression" in q["sql"]
                          or 'FROM "gemiapp_company"' in q["sql"]])


# --- enforcement read ----------------------------------------------------------------------------------

class EnforcementTests(DncTestCase):
    def test_the_canonical_check_is_organization_scoped_and_normalized(self):
        self.assertFalse(is_contact_suppressed(self.org, "company", self.gemi()))
        self.dnc()
        self.assertTrue(is_contact_suppressed(self.org, "company", self.gemi()))
        self.assertTrue(is_contact_suppressed(self.org.pk, "company", f"  {self.gemi()} "))
        self.assertFalse(is_contact_suppressed(self.org_b, "company", self.gemi()))
        self.assertFalse(is_contact_suppressed(self.org, "email", self.gemi()))  # not a suppressible type yet
        self.assertFalse(is_contact_suppressed(self.org, "company", "ΓΕΜΗ"))
        self.assertTrue(is_company_suppressed(self.org, self.company))
        self.assertFalse(is_company_suppressed(self.org_b, self.company))
        for bad in (None, 0, "1", True):
            self.assertRaises(ValueError, is_contact_suppressed, bad, "company", self.gemi())

    def test_legacy_suppressions_are_neither_read_nor_written(self):
        person = PersonSuppression.objects.create(full_name="Γιώργος Παπαδόπουλος", company=self.company)
        outreach = OutreachSuppression.objects.create(email="info@x.gr")
        before = (list(PersonSuppression.objects.values()), list(OutreachSuppression.objects.values()))
        self.assertFalse(is_company_suppressed(self.org, self.company))  # a legacy row is not an org suppression
        self.dnc()
        self.assertEqual((list(PersonSuppression.objects.values()), list(OutreachSuppression.objects.values())), before)
        self.assertTrue(OutreachSuppression.is_suppressed("info@x.gr"))
        source = "".join(inspect.getsource(f) for f in (apply_authorized_company_do_not_contact, is_contact_suppressed,
                                                        is_company_suppressed, g5._page_do_not_contact))
        for legacy in ("PersonSuppression", "OutreachSuppression"):
            self.assertNotIn(legacy, source)
        self.assertTrue(person.pk and outreach.pk)


# --- page, endpoint, privacy, parity -----------------------------------------------------------------

class PageTests(DncTestCase):
    def test_one_company_level_control_for_managers_only(self):
        self.live_opportunity(self.full_radar("Radar Β", legal_forms=()))
        self.live_opportunity(self.full_radar("Radar Γ", signal_types=()))
        for role in ("owner", "sales_manager"):
            html = self.get(self.members[role]).content.decode()
            self.assertEqual(html.count("data-do-not-contact-form"), 1, role)  # one per company, not per Radar
            form = html.split("data-do-not-contact-form", 1)[1].split("</form>", 1)[0]
            self.assertEqual(re.findall(r'<option value="([a-z_]+)"', form), list(DNC_COMPANY_REASONS))
            self.assertNotIn("email_unsubscribe", form)
            self.assertIn('name="confirm" value="yes" required', form)
            self.assertIn("Δεν θέλω να επικοινωνούμε με αυτή την εταιρεία", form)
            self.assertIn("ολόκληρη την εταιρεία σε όλο τον οργανισμό", html)
        self.put_assigned(self.maria)
        for who in (self.members["admin"], self.members["viewer"], self.maria_user):
            html = self.get(who).content.decode()
            self.assertNotIn("data-do-not-contact", html)
            self.assertNotIn("/do-not-contact/", html)
        status_form = self.status_form(self.get(self.owner).content.decode())
        self.assertNotIn("do_not_contact", status_form)  # never an option of the D32 dropdown

    def test_the_settled_state_shows_reason_and_date_to_every_reader_and_offers_nothing_back(self):
        self.put_assigned(self.maria)
        with patch("django.utils.timezone.now", return_value=MOMENT):
            self.dnc(actor=self.members["sales_manager"], reason="call_objection")
        self.m("sales_manager").delete()
        for who in (self.owner, self.members["admin"], self.members["viewer"], self.maria_user):
            html = self.get(who).content.decode()
            self.assertIn('data-do-not-contact="suppressed"', html)
            self.assertIn("Αντίρρηση σε τηλεφωνική επικοινωνία", html)
            self.assertIn("18/09/2026", html)
            for control in ("data-do-not-contact-form", "/do-not-contact/", "unsuppress", "Αναίρεση"):
                self.assertNotIn(control, html, (who.email, control))

    def test_the_endpoint_is_post_only_login_only_csrf_protected_and_redirects_to_the_page(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.dnc_url()).status_code, 405)
        anonymous = Client().post(self.dnc_url(), {"reason": "manual", "confirm": "yes"})
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn(reverse("login"), anonymous["Location"])
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.owner)
        self.assertEqual(strict.post(self.dnc_url(), {"reason": "manual", "confirm": "yes"}).status_code, 403)
        self.assertEqual(Suppression.objects.count(), 0)
        response = self.client.post(self.dnc_url() + "?next=https://evil.example/",
                                    {"reason": "manual", "confirm": "yes", "next": "https://evil.example/"})
        self.assertRedirects(response, self.url(), fetch_redirect_response=False)
        self.assertIn("καταχωρίστηκε ως «Χωρίς επικοινωνία»", self.client.get(self.url()).content.decode())
        model_admin = admin.site._registry[Suppression]
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser("staff-dnc@example.com", "staff-dnc@example.com", "x")
        row = Suppression.objects.get()
        self.assertEqual((model_admin.has_add_permission(request), model_admin.has_change_permission(request, row),
                          model_admin.has_delete_permission(request, row)), (False, False, False))

    def test_nothing_but_the_identity_is_stored_and_nobody_is_notified(self):
        Company.objects.filter(pk=self.company.pk).update(raw_data={"phone": "2109990001", "email": "info@x.gr",
                                                                    "persons": [{"name": "Γιώργος"}]})
        mail.outbox = []
        self.dnc()
        stored = Suppression.objects.values().get()
        self.assertEqual(set(stored), {"id", "organization_id", "contact_type", "contact_value", "reason", "source",
                                       "created_by_id", "created_at"})
        flat = str(stored)
        for secret in ("2109990001", "info@x.gr", "Γιώργος", self.company.name):
            self.assertNotIn(secret, flat)
        self.assertEqual(len(mail.outbox), 0)
        source = inspect.getsource(apply_authorized_company_do_not_contact)
        for forbidden in ("raw_data", "gemi_phones", "email", "persons", "send_mail", "notify", "AuditLog", "celery"):
            self.assertNotIn(forbidden, source, forbidden)
        from django.apps import apps

        names = {model.__name__ for model in apps.get_app_config("gemiapp").get_models()}
        self.assertEqual({n for n in names if "Audit" in n}, {"AdminAuditLog", "OrganizationAuditEvent"})  # D36
        self.assertFalse([n for n in names if "Notification" in n])

    def test_the_legacy_product_billing_and_phone_are_untouched(self):
        legacy_user = entitled_user("legacy-d35@example.com")
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
                list(PersonSuppression.objects.values()), list(OutreachSuppression.objects.values()),
            )

        with patch("django.core.signing.TimestampSigner.timestamp", return_value=TimestampSigner().timestamp()), \
                patch("gemiapp.ingestion.refresh.get_gemi_client") as client:
            before = world()
            self.assertEqual(self.post_dnc(self.owner).status_code, 302)
            self.assertEqual(world(), before)
        client.assert_not_called()
        self.assertEqual([p.tel_uri for p in Company(gemi_number="1", raw_data={"phone": "+30 2109990001"}).gemi_phones],
                         ["tel:+302109990001"])
        self.assertEqual([p.display for p in extract_company_contact_phones({"phone": "2109990001"})], ["2109990001"])
