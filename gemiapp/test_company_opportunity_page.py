"""Tests for the D29 company opportunity page (blueprint §36, gemiapp.organization_views).

The first customer-facing Organization route: explicit organization in the URL, authorized only through
gemiapp.organization_access, every refusal a 404, LIVE signals only, the frozen C8 capture for «why», and the latest
canonical snapshot for «current information». GET only; nothing is written.
"""

import inspect
import pathlib
from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.core import mail
from django.core.signing import TimestampSigner
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test.utils import CaptureQueriesContext
from django.urls import get_resolver, reverse

from . import company_opportunity_page as d29
from . import organization_views
from .company_contact import extract_company_contact_phones
from .company_signals import LIVE, SHADOW, SNAPSHOT_DIFF, record_company_signal
from .ingestion.monitoring import radar_match_evidence
from .ingestion.refresh import RefreshPolicy, build_company_refresh_plan
from .models import (
    Company, CompanyMonitoring, CompanySignal, CompanySignalSnapshotEvidence, CompanySnapshot, CustomerRadar,
    DigestDelivery, GemiKad, GemiPrefecture, Opportunity, OrganizationMember, OrganizationRadar, RadarMatch,
    UserCompanyLead, UserSubscription,
)
from .organization_access import OrganizationAccessDenied, get_authorized_company_opportunity_page
from .organization_radars import RadarDefinition, replace_organization_radar
from .services import company_matches_radar, eligible_radars, send_digests
from .test_gemi_company_activities import entitled_user, radar_for
from .test_gemi_validation import CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL
from .test_opportunity_scoring import FRESH
from .test_organization_access import AccessTestCase
from .test_organization_radar_matching import T0, new_company_signal, snapshot

STREET = "ΟΔΟΣ ΜΥΣΤΙΚΗ 77"
VAT = "099977777"
_keys = iter(range(1, 1_000_000))


class PageTestCase(AccessTestCase):
    def url(self, org=None, company=None):
        return reverse("organization_company_opportunity",
                       kwargs={"organization_id": (org or self.org).pk,
                               "company_id": company.pk if hasattr(company, "pk") else (company or self.company.pk)})

    def get(self, who, org=None, company=None):
        self.client.force_login(who)
        return self.client.get(self.url(org, company))

    def live_opportunity(self, radar, *, company=None, at=T0, as_of=FRESH, mode=LIVE):
        company = company or self.company
        signal = new_company_signal(company, at, mode=mode)
        result = self.materialize(signal, radar, as_of=as_of)
        self.assertTrue(result.eligible)
        return result.opportunity

    def full_radar(self, name="Πλήρες Radar", **overrides):
        return self.radar(name=name, **self.everything(**overrides))

    def page(self, who=None, org=None, company=None):
        return get_authorized_company_opportunity_page(who or self.owner, (org or self.org).pk,
                                                       (company or self.company).pk)


# --- route, roles, 404 -----------------------------------------------------------------------------

class RouteTests(PageTestCase):
    def setUp(self):
        super().setUp()
        snapshot(self.company, T0)
        self.opportunity_row = self.live_opportunity(self.full_radar())

    def test_the_route_is_organization_scoped_and_explicit(self):
        path = self.url()
        self.assertEqual(path, f"/organizations/{self.org.pk}/opportunities/company/{self.company.pk}/")
        match = get_resolver().resolve(path)
        self.assertIs(match.func.__wrapped__.__wrapped__, organization_views.company_opportunity_page.__wrapped__.__wrapped__)

    def test_role_matrix(self):
        for role, expected in (("owner", 200), ("admin", 200), ("sales_manager", 200), ("viewer", 200),
                               ("sales_user", 404)):
            self.assertEqual(self.get(self.members[role]).status_code, expected, role)

    def test_anonymous_users_are_sent_to_login(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_every_refusal_is_the_same_404(self):
        outsider = self.b_owner
        foreign_company_row = self.live_opportunity(self.radar_b_foreign, company=Company.objects.create(
            gemi_number="700100", name="Ξένη ΑΕ", incorporation_date=date(2026, 9, 1)))
        nonexistent_org = self.org_b.pk + 50
        cases = [
            self.get(outsider),                                                     # non-member of A
            self.get(self.owner, org=self.org_b, company=foreign_company_row.company),  # A's owner, B's company
            self.get(self.owner, company=foreign_company_row.company),              # B's company through A
            self.get(self.owner, company=self.company.pk + 999),                    # nonexistent company
            self.get(self.members["sales_user"]),                                   # unassigned sales user
        ]
        self.client.force_login(self.owner)
        cases.append(self.client.get(f"/organizations/{nonexistent_org}/opportunities/company/{self.company.pk}/"))
        self.assertEqual({response.status_code for response in cases}, {404})
        for response in cases:
            text = response.content.decode()
            for secret in ("Ξένη ΑΕ", "B secret radar", "Tenant B", self.company.name):
                self.assertNotIn(secret, text)

    def test_adjacent_ids_reveal_nothing(self):
        other = Company.objects.create(gemi_number="700200", name="Διπλανή ΑΕ", incorporation_date=date(2026, 9, 1))
        self.live_opportunity(self.radar_b_foreign, company=other)
        self.assertEqual(self.get(self.owner, company=other).status_code, 404)
        self.assertEqual(self.get(self.owner, company=self.company).status_code, 200)

    def test_only_get_is_accepted(self):
        self.client.force_login(self.owner)
        for method in ("post", "put", "delete"):
            self.assertEqual(getattr(self.client, method)(self.url()).status_code, 405, method)


# --- LIVE only -------------------------------------------------------------------------------------

class LiveOnlyTests(PageTestCase):
    def test_a_shadow_only_company_has_no_page(self):
        snapshot(self.company, T0)
        self.live_opportunity(self.full_radar(), mode=SHADOW)
        self.assertEqual(Opportunity.objects.count(), 1)  # the row exists; it is simply not customer-visible
        self.assertEqual(self.get(self.owner).status_code, 404)
        with self.assertRaises(OrganizationAccessDenied):
            self.page()

    def test_only_live_backed_opportunities_contribute_and_shadow_never_reaches_the_html(self):
        snapshot(self.company, T0)
        live = self.live_opportunity(self.full_radar("Live Radar"))
        shadow_radar = self.radar(name="Shadow Radar", kads=(self.r.kad_2026,), signal_types=("status_changed",))
        previous, current = snapshot(self.company, T0 + timedelta(hours=1)), snapshot(self.company, T0 + timedelta(hours=2))
        shadow_signal, _ = record_company_signal(company=self.company, signal_type="status_changed",
                                                 source_type=SNAPSHOT_DIFF, event_key={"d29": next(_keys)},
                                                 detected_at=T0 + timedelta(hours=3), mode=SHADOW)
        CompanySignalSnapshotEvidence.objects.create(signal=shadow_signal, previous_snapshot=previous,
                                                     current_snapshot=current, subject_kind="status",
                                                     before_source_id="3", after_source_id="8")
        shadow = self.materialize(shadow_signal, shadow_radar, as_of=T0 + timedelta(hours=4)).opportunity
        page = self.page()
        self.assertEqual([o.opportunity_id for o in page.opportunities], [live.pk])
        self.assertEqual(page.primary.opportunity_id, live.pk)
        self.assertNotIn(shadow_signal.pk, [item.signal_id for item in page.timeline])
        html = self.get(self.owner).content.decode()
        self.assertNotIn("Shadow Radar", html)
        self.assertNotIn(f'data-opportunity="{shadow.pk}"', html)
        self.assertNotIn("Αλλαγή κατάστασης", html)  # the SHADOW signal's type label
        self.assertIn("Live Radar", html)

    def test_the_timeline_reads_live_signals_only(self):
        snapshot(self.company, T0)
        self.live_opportunity(self.full_radar())
        shadow_signal, _ = record_company_signal(company=self.company, signal_type="kad_added", source_type=SNAPSHOT_DIFF,
                                                 event_key={"d29": next(_keys)}, detected_at=T0 + timedelta(days=1),
                                                 mode=SHADOW)
        page = self.page()
        self.assertTrue(page.timeline)
        self.assertEqual({CompanySignal.objects.get(pk=item.signal_id).mode for item in page.timeline}, {LIVE})
        self.assertNotIn(shadow_signal.pk, [item.signal_id for item in page.timeline])
        self.assertNotIn("Προσθήκη ΚΑΔ", self.get(self.owner).content.decode())

    def test_the_timeline_is_bounded(self):
        snapshot(self.company, T0)
        self.live_opportunity(self.full_radar())
        for index in range(30):
            record_company_signal(company=self.company, signal_type="new_company", source_type="discovery",
                                  event_key={"d29": next(_keys)}, detected_at=T0 - timedelta(hours=index + 1),
                                  mode=LIVE)
        page = self.page()
        self.assertEqual(len(page.timeline), d29.TIMELINE_LIMIT)
        self.assertTrue(page.timeline_truncated)
        self.assertEqual([i.detected_at for i in page.timeline], sorted((i.detected_at for i in page.timeline),
                                                                        reverse=True))


# --- frozen explanation vs current information -----------------------------------------------------

class FrozenAndCurrentTests(PageTestCase):
    def test_why_this_lead_is_frozen_while_current_information_follows_the_latest_snapshot(self):
        snapshot(self.company, T0)
        radar = self.full_radar()
        self.live_opportunity(radar)
        before = self.page()
        self.assertEqual((before.score, before.score_class), (100, "priority"))
        self.assertEqual([line.awarded_points for line in before.breakdown], [30, 25, 15, 10, 20])
        # Edit the Radar, move the wall clock, and observe a newer company state.
        replace_organization_radar(self.org, radar, RadarDefinition(name="edited", active=True,
                                                                    kads=(self.r.kad_other,)))
        snapshot(self.company, T0 + timedelta(days=5), kads=(("47191002", "kad_2026"),), prefecture="61190",
                 municipality=None, legal="3")
        with patch("django.utils.timezone.now", return_value=T0 + timedelta(days=400)):
            after = self.page()
        self.assertEqual((after.score, after.score_class, after.scored_as_of), (100, "priority", FRESH))
        self.assertEqual(after.breakdown, before.breakdown)
        self.assertIn("ΚΑΔ 62010000 · kad_2026", after.breakdown[0].evidence[0])
        self.assertEqual([(a.code, a.kad_version) for a in after.current.activities], [("47191002", "kad_2026")])
        self.assertEqual((after.current.prefecture.source_id, after.current.legal_form.source_id), ("61190", "3"))
        self.assertIsNone(after.current.municipality)

    def test_current_information_comes_from_the_canonical_snapshot_with_descriptions(self):
        GemiPrefecture.objects.filter(source_id="5").update(description="ΑΤΤΙΚΗΣ (ref)")
        self.company.is_active = False
        self.company.status = "ΛΑΘΟΣ LEGACY ΚΑΤΑΣΤΑΣΗ"
        self.company.save()
        snapshot(self.company, T0)
        self.live_opportunity(self.full_radar())
        page = self.page()
        self.assertTrue(page.current.available)
        self.assertEqual(str(page.current.prefecture), "ΑΤΤΙΚΗΣ (ref) (5)")
        self.assertEqual(page.current.status.source_id, "3")
        self.assertEqual([(a.code, a.kad_version) for a in page.current.activities], [("62010000", "kad_2026")])
        html = self.get(self.owner).content.decode()
        self.assertNotIn("ΛΑΘΟΣ LEGACY ΚΑΤΑΣΤΑΣΗ", html)
        self.assertIn("ΑΤΤΙΚΗΣ (ref) (5)", html)

    def test_without_a_snapshot_the_page_says_so_and_never_falls_back_to_legacy_fields(self):
        radar = self.radar(name="Signal only", signal_types=("new_company",))
        self.live_opportunity(radar)  # a signal-only Radar matches without any snapshot
        self.company.legal_type = "ΛΑΘΟΣ LEGACY ΜΟΡΦΗ"
        self.company.save()
        page = self.page()
        self.assertFalse(page.current.available)
        html = self.get(self.owner).content.decode()
        self.assertIn('id="current-unavailable"', html)
        self.assertNotIn("ΛΑΘΟΣ LEGACY ΜΟΡΦΗ", html)

    def test_only_verified_current_activities_are_listed_never_legacy_rows(self):
        from .models import CompanyActivity

        snapshot(self.company, T0)
        self.live_opportunity(self.full_radar())
        CompanyActivity.objects.create(company=self.company, code="99999999", description="Λήξασα δραστηριότητα")
        html = self.get(self.owner).content.decode()
        self.assertNotIn("99999999", html)
        self.assertIn("62010000", html)
        self.assertIn("kad_2026", html)


# --- several Radars --------------------------------------------------------------------------------

class MultiRadarTests(PageTestCase):
    def test_one_page_lists_every_visible_radar_opportunity_and_the_highest_is_primary(self):
        snapshot(self.company, T0)
        radar_a = self.full_radar("Radar Α", legal_forms=())          # 30+25+15+20 = 90
        radar_b = self.full_radar("Radar Β", legal_forms=())
        high = self.live_opportunity(radar_a)
        low_signal = new_company_signal(self.company, T0 + timedelta(minutes=1), mode=LIVE)
        low = self.materialize(low_signal, radar_b, as_of=T0 + timedelta(days=20)).opportunity  # freshness 5 -> 75
        self.assertEqual((high.score, low.score), (90, 75))
        page = self.page()
        self.assertEqual([o.opportunity_id for o in page.opportunities], [high.pk, low.pk])
        self.assertEqual((page.primary.opportunity_id, page.score), (high.pk, 90))
        self.assertEqual([o.is_primary for o in page.opportunities], [True, False])
        html = self.get(self.owner).content.decode()
        self.assertEqual(html.count("data-opportunity="), 2)
        self.assertIn("Radar Α", html)
        self.assertIn("Radar Β", html)


# --- privacy, XSS, efficiency, safety --------------------------------------------------------------

class SafetyTests(PageTestCase):
    def test_no_contact_person_street_vat_or_payload_is_rendered(self):
        company = Company.objects.create(
            gemi_number="700300", name="Καθαρή Επωνυμία ΑΕ", incorporation_date=date(2026, 9, 1), address=STREET,
            email=CONTACT_SENTINEL, vat_number=VAT, website="https://sentinel.example.invalid",
            raw_data={"phone": PHONE_SENTINEL, "email": CONTACT_SENTINEL,
                      "persons": [{"personName": PERSON_SENTINEL}], "street": STREET,
                      "html": "<b>raw-payload-marker</b>"},
        )
        snapshot(company, T0)
        self.live_opportunity(self.full_radar(), company=company)
        html = self.get(self.owner, company=company).content.decode()
        self.assertIn("Καθαρή Επωνυμία ΑΕ", html)
        for secret in (PHONE_SENTINEL, CONTACT_SENTINEL, PERSON_SENTINEL, STREET, VAT, "sentinel.example.invalid",
                       "raw-payload-marker", "tel:", "mailto:", "14561"):
            self.assertNotIn(secret, html, secret)
        self.assertTrue(company.gemi_phones)  # the phone exists -- and stays Dossier-only

    def test_source_text_is_escaped(self):
        company = Company.objects.create(gemi_number="700400", name="<script>alert('x')</script> ΑΕ",
                                         trade_names="<img src=x onerror=alert(1)>", incorporation_date=date(2026, 9, 1))
        snapshot(company, T0)
        GemiKad.objects.filter(source_id="62010000", kad_version="kad_2026").update(description="<i>kad-html</i>")
        self.live_opportunity(self.full_radar(name="<b>radar-html</b>"), company=company)
        html = self.get(self.owner, company=company).content.decode()
        for raw in ("<script>alert", "<img src=x", "<b>radar-html</b>", "<i>kad-html</i>"):
            self.assertNotIn(raw, html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;b&gt;radar-html&lt;/b&gt;", html)

    def test_the_page_is_read_with_a_bounded_number_of_queries(self):
        snapshot(self.company, T0)
        radars = [self.full_radar(f"R{index}") for index in range(3)]
        self.live_opportunity(radars[0])
        with CaptureQueriesContext(connection) as one:
            self.page()
        for radar in radars[1:]:
            self.live_opportunity(radar)
        for index in range(20):
            record_company_signal(company=self.company, signal_type="new_company", source_type="discovery",
                                  event_key={"d29": next(_keys)}, detected_at=T0 - timedelta(hours=index + 1),
                                  mode=LIVE)
        with CaptureQueriesContext(connection) as three:
            page = self.page()
        self.assertEqual((len(page.opportunities), len(page.timeline)), (3, 20))
        self.assertEqual(len(three), len(one))
        self.assertLessEqual(len(three), 17)  # D33: one bounded notes query; D34: tasks + task-assignee options
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in three.captured_queries))

    def test_opening_the_page_writes_nothing(self):
        snapshot(self.company, T0)
        row = self.live_opportunity(self.full_radar())
        self.client.force_login(self.owner)
        before = (list(Opportunity.objects.values()), list(OrganizationMember.objects.values()),
                  list(OrganizationRadar.objects.values()), list(CompanySignal.objects.values()),
                  list(CompanySnapshot.objects.values()), list(Company.objects.values()))
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get(self.url()).status_code, 200)
        writes = [q["sql"] for q in queries.captured_queries
                  if not q["sql"].lstrip().upper().startswith(("SELECT", "SAVEPOINT", "RELEASE"))]
        self.assertEqual(writes, [])
        self.assertEqual((list(Opportunity.objects.values()), list(OrganizationMember.objects.values()),
                          list(OrganizationRadar.objects.values()), list(CompanySignal.objects.values()),
                          list(CompanySnapshot.objects.values()), list(Company.objects.values())), before)
        self.assertEqual(Opportunity.objects.get(pk=row.pk).status, "new")  # no auto «viewed»

    def test_the_route_reaches_tenant_data_only_through_the_authorization_layer(self):
        source = inspect.getsource(organization_views).split('"""', 2)[2]
        # The only project import is the authorization layer (D30 added its Save entry point to the same import).
        self.assertEqual([line for line in source.splitlines() if line.startswith(("from .", "import ."))],
                         ["from .organization_access import ("])
        imported = source.split("from .organization_access import (", 1)[1].split(")", 1)[0]
        self.assertEqual({name.strip() for name in imported.split(",") if name.strip()},
                         {"AssignmentRefused", "NoteRefused", "OpportunityTransitionRefused",
                          "OrganizationAccessDenied", "StatusChangeRefused", "TaskRefused",
                          "add_authorized_opportunity_note", "assign_authorized_opportunity",
                          "complete_authorized_opportunity_task", "create_authorized_opportunity_task",
                          "get_authorized_company_opportunity_page", "save_authorized_opportunity",
                          "set_authorized_opportunity_status"})
        for forbidden in (".objects", ".save(", ".create(", ".update(", ".delete(", "organization_radar_matching",
                          "opportunity_scoring", "opportunity_score_breakdown", "opportunity_feed",
                          "get_opportunity_score_breakdown", "set_opportunity_status", "request.organization",
                          "session"):
            self.assertNotIn(forbidden, source, forbidden)
        page_code = inspect.getsource(d29).split('"""', 2)[2]
        import re

        self.assertFalse(re.findall(r"objects[^\n]*\.(?:update|delete|create|get_or_create)\(", page_code))
        for forbidden in (".save(", "bulk_", "SHADOW", "ALL_MODES",
                          "raw_data", "gemi_phones", "email", "address", "vat_number", "persons", "is_active",
                          "organization_radar_matching", "opportunity_scoring", "build_opportunity_score_breakdown",
                          "get_gemi_client", "requests", "urllib"):
            self.assertNotIn(forbidden, page_code, forbidden)

    def test_every_organization_route_is_served_by_the_guarded_module(self):
        def flatten(patterns, prefix=""):
            for pattern in patterns:
                if hasattr(pattern, "url_patterns"):
                    yield from flatten(pattern.url_patterns, prefix + str(pattern.pattern))
                else:
                    yield prefix + str(pattern.pattern), pattern.callback

        # The Django admin site (staff-only, read-only for these models) is a separate concern from customer routes.
        organization_routes = [(route, callback) for route, callback in flatten(get_resolver().url_patterns)
                               if "organization" in route and not getattr(callback, "__module__", "").startswith(
                                   "django.contrib.admin") and not route.startswith("admin/")]
        self.assertEqual([route for route, _ in organization_routes],
                         ["organizations/<int:organization_id>/opportunities/company/<int:company_id>/",
                          "organizations/<int:organization_id>/opportunities/<int:opportunity_id>/save/",
                          "organizations/<int:organization_id>/opportunities/<int:opportunity_id>/assign/",
                          "organizations/<int:organization_id>/opportunities/<int:opportunity_id>/status/",
                          "organizations/<int:organization_id>/opportunities/<int:opportunity_id>/notes/",
                          "organizations/<int:organization_id>/opportunities/<int:opportunity_id>/tasks/",
                          "organizations/<int:organization_id>/opportunities/<int:opportunity_id>/tasks/"
                          "<int:task_id>/complete/"])
        for _, callback in organization_routes:
            self.assertEqual(callback.__module__, "gemiapp.organization_views")
        product_modules = [path for path in pathlib.Path("gemiapp").glob("*.py")
                           if not path.name.startswith("test")
                           and "from .organization_access import" in path.read_text(encoding="utf-8")]
        self.assertEqual(sorted(p.name for p in product_modules), ["organization_views.py"])

    def test_no_mutation_endpoint_model_or_migration(self):
        from django.apps import apps

        # Since D34 the page has exactly six kinds of form, all per opportunity: Save (D30), Assign (D31),
        # status (D32), a note (D33), and a task's create and complete (D34).
        html_template = pathlib.Path("templates/organizations/company_opportunity.html").read_text(encoding="utf-8")
        self.assertEqual(html_template.count("<form"), 6)
        self.assertEqual(html_template.count("<button"), 6)
        for route in ("organization_save_opportunity", "organization_assign_opportunity", "organization_opportunity_status",
                      "organization_add_opportunity_note", "organization_create_opportunity_task"):
            self.assertIn("{% url '" + route + "' page.organization_id opportunity.opportunity_id %}", html_template)
        self.assertFalse([m for m in apps.get_app_config("gemiapp").get_models() if "Assign" in m.__name__
                          or "Note" in m.__name__ and m.__name__ != "OpportunityNote"
                          or "Task" in m.__name__ and m.__name__ != "OpportunityTask"])
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertEqual(max(name for app, name in loader.disk_migrations if app == "gemiapp"),
                         "0051_opportunity_task")  # D34 owns 0051 (tasks); any newer migration must update this pin deliberately

    def test_the_legacy_product_billing_and_phone_are_untouched(self):
        legacy_user = entitled_user("legacy-d29@example.com")
        legacy = radar_for(legacy_user, "legacy", prefectures=["ΑΤΤΙΚΗΣ"])
        run_at = datetime(2026, 9, 16, 9, tzinfo=dt_timezone.utc)
        CompanyMonitoring.objects.create(
            company=self.company, state="active", priority="high", primary_reason="active_radar_match",
            policy_version=1, monitored_since=run_at - timedelta(days=3), next_check_at=run_at - timedelta(hours=1))
        policy = RefreshPolicy(max_requests_per_run=5, max_pages_per_query=2, max_direct_details_per_run=2, page_size=2)
        snapshot(self.company, T0)
        self.live_opportunity(self.full_radar())

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
            self.assertEqual(self.get(self.owner).status_code, 200)
            self.assertEqual(world(), before)
        client.assert_not_called()
        self.assertEqual(UserCompanyLead.objects.count(), 0)  # no legacy lead is created by viewing
        self.assertEqual([p.tel_uri for p in Company(gemi_number="1", raw_data={"phone": "+30 2109990001"}).gemi_phones],
                         ["tel:+302109990001"])
        self.assertEqual([p.display for p in extract_company_contact_phones({"phone": "2109990001"})], ["2109990001"])
