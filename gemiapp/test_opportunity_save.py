"""Tests for D30 Save (gemiapp.organization_access.save_authorized_opportunity, gemiapp.organization_views).

Save is one narrow lifecycle action on one explicit C8 opportunity: NEW or VIEWED -> SAVED; SAVED -> SAVED with no
write; every later §39 state refused and untouched. OWNER and SALES_MANAGER only (manage_opportunity_workflow),
LIVE-backed opportunities only, every refusal of access the same 404. No unsave, no general status workflow.
"""

import inspect
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.core import mail
from django.core.signing import TimestampSigner
from django.db import connection
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
    OpportunityScoreComponent, OpportunityScoreEvidence, OpportunitySignal, OrganizationMember, OrganizationRadar,
    RadarMatch, UserCompanyLead, UserSubscription,
)
from .opportunity_feed import FeedFilters, get_opportunity_feed
from .organization_access import (
    SAVE_ALLOWED_FROM, OpportunityTransitionRefused, OrganizationAccessDenied, get_authorized_opportunity_score_breakdown,
    save_authorized_opportunity,
)
from .services import company_matches_radar, eligible_radars, send_digests
from .test_company_opportunity_page import PageTestCase
from .test_gemi_company_activities import entitled_user, radar_for
from .test_organization_radar_matching import T0, snapshot

LATER_STATES = ("assigned", "contacted", "interested", "follow_up", "won", "lost", "not_relevant", "do_not_contact")
FROZEN_FIELDS = ("score", "score_class", "score_rule_version", "match_rule_version", "scored_as_of",
                 "primary_reason_code", "first_signal_id", "latest_signal_id", "organization_id", "radar_id",
                 "company_id", "created_at", "expires_at")


class SaveTestCase(PageTestCase):
    def setUp(self):
        super().setUp()
        snapshot(self.company, T0)
        self.row = self.live_opportunity(self.full_radar("Radar Α"))

    def save_url(self, row=None, org=None):
        return reverse("organization_save_opportunity",
                       kwargs={"organization_id": (org or self.org).pk, "opportunity_id": (row or self.row).pk})

    def post(self, who, row=None, org=None):
        self.client.force_login(who)
        return self.client.post(self.save_url(row, org))

    def status(self, row=None):
        return Opportunity.objects.get(pk=(row or self.row).pk).status

    def set_status(self, status, row=None):
        Opportunity.objects.filter(pk=(row or self.row).pk).update(status=status)

    def frozen(self, row=None):
        pk = (row or self.row).pk
        return (Opportunity.objects.filter(pk=pk).values(*FROZEN_FIELDS).get(),
                list(OpportunityScoreComponent.objects.filter(opportunity_id=pk).values()),
                list(OpportunityScoreEvidence.objects.filter(component__opportunity_id=pk).values()),
                list(OpportunitySignal.objects.filter(opportunity_id=pk).values()))


# --- the transition contract -----------------------------------------------------------------------

class TransitionTests(SaveTestCase):
    def test_new_and_viewed_become_saved_with_exactly_one_status_write(self):
        self.assertEqual(SAVE_ALLOWED_FROM, frozenset({"new", "viewed"}))
        for start in ("new", "viewed"):
            self.set_status(start)
            before = Opportunity.objects.get(pk=self.row.pk).updated_at
            with CaptureQueriesContext(connection) as queries:
                result = save_authorized_opportunity(self.owner, self.org.pk, self.row.pk)
            self.assertEqual((result.status, result.changed, self.status()), ("saved", True, "saved"), start)
            updates = [q["sql"] for q in queries.captured_queries if q["sql"].lstrip().upper().startswith("UPDATE")]
            self.assertEqual(len(updates), 1, updates)
            self.assertIn('"status"', updates[0])
            self.assertGreaterEqual(Opportunity.objects.get(pk=self.row.pk).updated_at, before)

    def test_saved_stays_saved_without_any_write(self):
        self.set_status("saved")
        stored = Opportunity.objects.filter(pk=self.row.pk).values().get()
        with CaptureQueriesContext(connection) as queries:
            result = save_authorized_opportunity(self.owner, self.org.pk, self.row.pk)
        self.assertEqual((result.status, result.changed), ("saved", False))
        writes = [q["sql"] for q in queries.captured_queries
                  if not q["sql"].lstrip().upper().startswith(("SELECT", "SAVEPOINT", "RELEASE"))]
        self.assertEqual(writes, [])
        self.assertEqual(Opportunity.objects.filter(pk=self.row.pk).values().get(), stored)  # updated_at included

    def test_every_later_state_is_refused_and_untouched(self):
        for state in LATER_STATES:
            self.set_status(state)
            stored = Opportunity.objects.filter(pk=self.row.pk).values().get()
            with self.assertRaises(OpportunityTransitionRefused) as refused:
                save_authorized_opportunity(self.owner, self.org.pk, self.row.pk)
            self.assertEqual((refused.exception.result.status, refused.exception.result.changed), (state, False))
            self.assertEqual(Opportunity.objects.filter(pk=self.row.pk).values().get(), stored, state)
            response = self.post(self.owner)  # the endpoint refuses the same way, back to the page
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.status(), state, state)

    def test_save_is_idempotent_across_requests(self):
        first, second = self.post(self.owner), self.post(self.owner)
        self.assertEqual((first.status_code, second.status_code), (302, 302))
        self.assertEqual(self.status(), "saved")
        self.assertEqual(Opportunity.objects.filter(company=self.company).count(), 1)

    def test_a_racing_second_save_observes_saved_and_takes_the_no_write_path(self):
        # Two requests from NEW: the first transitions; the second re-reads the locked row, sees SAVED, writes
        # nothing. SQLite proves the behaviour; the row lock itself is PostgreSQL's.
        first = save_authorized_opportunity(self.owner, self.org.pk, self.row.pk)
        second = save_authorized_opportunity(self.members["sales_manager"], self.org.pk, self.row.pk)
        self.assertEqual(((first.changed, second.changed)), (True, False))
        source = inspect.getsource(save_authorized_opportunity)
        self.assertIn("with _mutation():", source)  # D36: transaction.atomic() plus the safe FK denial
        self.assertIn("transaction.atomic()", inspect.getsource(g5._mutation))
        self.assertIn('select_for_update(of=("self",))', source)

    def test_there_is_no_unsave_and_no_general_status_endpoint(self):
        from django.urls import get_resolver

        names = [name for name in get_resolver().reverse_dict.keys() if isinstance(name, str)]
        self.assertFalse([n for n in names if "unsave" in n])
        # D32 owns the one status endpoint, which never targets SAVED (pinned in test_opportunity_status).
        self.assertEqual([n for n in names if "status" in n and "organization" in n], ["organization_opportunity_status"])
        code = inspect.getsource(g5)
        self.assertNotIn("set_opportunity_status", code)  # the unscoped C8 setter never reaches customer code
        import re

        # One opportunity status setter (D32); the other setter is a Radar's own active flag (customer Radar editing).
        self.assertEqual(sorted(re.findall(r"def (set_\w+)\(", code)),
                         ["set_authorized_opportunity_status", "set_authorized_organization_radar_active"])
        for definition in ("def unsave", "def restore", "def toggle", "def change_status"):
            self.assertNotIn(definition, code, definition)


# --- target, authorization, LIVE -------------------------------------------------------------------

class TargetAndAccessTests(SaveTestCase):
    def test_only_the_explicit_opportunity_changes_even_when_it_is_not_the_primary(self):
        radar_b = self.full_radar("Radar Β", legal_forms=())
        row_b = self.live_opportunity(radar_b)  # 90, while Radar Α is 100 and stays primary
        frozen_a, frozen_b = self.frozen(), self.frozen(row_b)
        stored_a = Opportunity.objects.filter(pk=self.row.pk).values().get()
        self.post(self.owner, row=row_b)
        self.assertEqual((self.status(row_b), self.status()), ("saved", "new"))
        self.assertEqual(Opportunity.objects.filter(pk=self.row.pk).values().get(), stored_a)
        self.assertEqual((self.frozen(), self.frozen(row_b)), (frozen_a, frozen_b))
        page = self.page()
        self.assertEqual(page.primary.opportunity_id, self.row.pk)  # primary selection untouched
        self.assertEqual({o.opportunity_id: o.status for o in page.opportunities},
                         {self.row.pk: "new", row_b.pk: "saved"})

    def test_role_matrix(self):
        for role, allowed in (("owner", True), ("sales_manager", True), ("admin", False), ("viewer", False),
                              ("sales_user", False)):
            self.set_status("new")
            response = self.post(self.members[role])
            self.assertEqual(response.status_code, 302 if allowed else 404, role)
            self.assertEqual(self.status(), "saved" if allowed else "new", role)

    def test_every_access_refusal_is_the_same_404_and_changes_nothing(self):
        foreign_company = Company.objects.create(gemi_number="800100", name="Ξένη ΑΕ", incorporation_date=date(2026, 9, 1))
        foreign = self.live_opportunity(self.radar_b_foreign, company=foreign_company)
        shadow_company = Company.objects.create(gemi_number="800200", name="Σκιώδης ΑΕ",
                                                incorporation_date=date(2026, 9, 1))
        snapshot(shadow_company, T0)
        shadow = self.live_opportunity(self.full_radar("Shadow Radar"), company=shadow_company, mode=SHADOW)
        missing = Opportunity.objects.order_by("-pk").first().pk + 1
        cases = [
            self.post(self.b_owner),                                   # non-member of A
            self.post(self.owner, row=foreign),                        # B's opportunity through A
            self.post(self.owner, row=foreign, org=self.org_b),        # A's owner at B's door
            self.post(self.owner, row=shadow),                         # SHADOW-backed
        ]
        self.client.force_login(self.owner)
        cases.append(self.client.post(f"/organizations/{self.org.pk}/opportunities/{missing}/save/"))
        cases.append(self.client.post(f"/organizations/{self.org_b.pk + 50}/opportunities/{self.row.pk}/save/"))
        self.assertEqual({response.status_code for response in cases}, {404})
        for response in cases:
            for secret in ("Ξένη ΑΕ", "Σκιώδης ΑΕ", "Shadow Radar", "B secret radar", "Tenant B"):
                self.assertNotIn(secret, response.content.decode())
        self.assertEqual({self.status(foreign), self.status(shadow)}, {"new"})

    def test_a_shadow_row_of_a_mixed_company_stays_untouchable_while_the_live_row_saves(self):
        shadow_radar = self.full_radar("Shadow on same company")
        shadow = self.live_opportunity(shadow_radar, mode=SHADOW)
        self.assertEqual(self.post(self.owner, row=shadow).status_code, 404)
        self.assertEqual(self.post(self.owner).status_code, 302)
        self.assertEqual((self.status(), self.status(shadow)), ("saved", "new"))

    def test_the_endpoint_is_post_only_login_only_and_csrf_protected(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.save_url()).status_code, 405)
        self.assertEqual(self.status(), "new")
        anonymous = Client().post(self.save_url())
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn(reverse("login"), anonymous["Location"])
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.owner)
        self.assertEqual(strict.post(self.save_url()).status_code, 403)
        self.assertEqual(self.status(), "new")

    def test_success_redirects_to_the_authorized_company_page_derived_from_the_opportunity(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.save_url() + "?next=https://evil.example/", {"next": "https://evil.example/"})
        self.assertRedirects(response, self.url(), fetch_redirect_response=False)
        page = self.client.get(response["Location"])
        self.assertContains(page, f'data-saved-for="{self.row.pk}"')


# --- invariants: score, signals, feed, page ---------------------------------------------------------

class InvariantTests(SaveTestCase):
    def test_save_leaves_the_frozen_capture_signals_snapshots_and_timeline_untouched(self):
        frozen = self.frozen()
        breakdown = get_authorized_opportunity_score_breakdown(self.owner, self.org, self.row.pk)
        signals, snapshots = list(CompanySignal.objects.values()), list(CompanySnapshot.objects.values())
        timeline = self.page().timeline
        save_authorized_opportunity(self.owner, self.org.pk, self.row.pk)
        self.assertEqual(self.frozen(), frozen)
        self.assertEqual(get_authorized_opportunity_score_breakdown(self.owner, self.org, self.row.pk), breakdown)
        self.assertEqual((list(CompanySignal.objects.values()), list(CompanySnapshot.objects.values())),
                         (signals, snapshots))
        self.assertEqual(self.page().timeline, timeline)

    def test_the_existing_feed_status_filter_finds_the_saved_opportunity(self):
        self.assertEqual(get_opportunity_feed(self.org, FeedFilters(statuses=("saved",))).cards, ())
        save_authorized_opportunity(self.owner, self.org.pk, self.row.pk)
        card, = get_opportunity_feed(self.org, FeedFilters(statuses=("saved",))).cards
        self.assertEqual((card.primary_opportunity_id, card.score, card.status), (self.row.pk, 100, "saved"))

    def test_the_page_offers_save_per_row_only_to_workflow_roles(self):
        row_b = self.live_opportunity(self.full_radar("Radar Β", legal_forms=()))
        self.set_status("saved", row_b)
        html = self.get(self.owner).content.decode()
        self.assertEqual(html.count("data-save-for="), 1)
        self.assertIn(f'data-save-for="{self.row.pk}"', html)
        self.assertIn(reverse("organization_save_opportunity", args=[self.org.pk, self.row.pk]), html)
        self.assertIn(f'data-saved-for="{row_b.pk}"', html)
        self.assertNotIn("unsave", html.lower())
        for role in ("admin", "viewer"):
            other = self.get(self.members[role]).content.decode()
            self.assertNotIn("data-save-for=", other, role)
            self.assertNotIn("/save/", other, role)
        for state in LATER_STATES:
            self.set_status(state)
            self.assertNotIn(f'data-save-for="{self.row.pk}"', self.get(self.owner).content.decode(), state)

    def test_the_page_never_auto_marks_viewed(self):
        self.get(self.owner)
        self.get(self.members["viewer"])
        self.assertEqual(self.status(), "new")

    def test_save_uses_a_bounded_number_of_queries(self):
        for index in range(4):
            self.live_opportunity(self.full_radar(f"Sibling {index}"))
        with CaptureQueriesContext(connection) as transition:
            save_authorized_opportunity(self.owner, self.org.pk, self.row.pk)
        with CaptureQueriesContext(connection) as noop:
            save_authorized_opportunity(self.owner, self.org.pk, self.row.pk)
        # organization, membership, locked row, re-validated actor (+ savepoint bookkeeping), the one UPDATE and the
        # one D36 audit INSERT on a real transition
        self.assertLessEqual(len(transition), 8)
        self.assertEqual(len([q for q in transition.captured_queries if q["sql"].upper().startswith("INSERT")]), 1)
        self.assertEqual(len([q for q in transition.captured_queries if q["sql"].upper().startswith("UPDATE")]), 1)
        self.assertEqual(len([q for q in noop.captured_queries if q["sql"].lstrip().upper().startswith("SELECT")]), 3)

    def test_the_legacy_product_billing_and_phone_are_untouched(self):
        legacy_user = entitled_user("legacy-d30@example.com")
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
                list(OrganizationMember.objects.values()), list(OrganizationRadar.objects.values()),
                list(Company.objects.values()),
            )

        with patch("django.core.signing.TimestampSigner.timestamp", return_value=TimestampSigner().timestamp()), \
                patch("gemiapp.ingestion.refresh.get_gemi_client") as client:
            before = world()
            self.assertEqual(self.post(self.owner).status_code, 302)
            self.assertEqual(world(), before)
        client.assert_not_called()
        self.assertEqual(self.status(), "saved")
        self.assertEqual([p.tel_uri for p in Company(gemi_number="1", raw_data={"phone": "+30 2109990001"}).gemi_phones],
                         ["tel:+302109990001"])
        self.assertEqual([p.display for p in extract_company_contact_phones({"phone": "2109990001"})], ["2109990001"])

    def test_the_service_and_view_touch_no_contact_or_network_code(self):
        from . import organization_views

        for module in (g5, organization_views):
            code = inspect.getsource(module).split('"""', 2)[2]
            for forbidden in ("raw_data", "gemi_phones", ".email", "persons", "get_gemi_client", "stripe", "send_mail",
                              "requests", "urllib", "request.session", "HTTP_REFERER", 'request.GET.get("next")',
                              'request.POST.get("next")'):
                self.assertNotIn(forbidden, code, (module.__name__, forbidden))
