"""Campaign landing pages (/for/horeca/, /for/insurance/) and consent-safe conversion events."""

import json
import re
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.http import QueryDict
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .campaigns import (
    SESSION_KEY, analytics_event, attribution_query, clean_attribution, search_event_params,
)
from .models import UserSubscription


def _events(response, node_id):
    match = re.search(rf'<script id="{node_id}" type="application/json">(.*?)</script>', response.content.decode())
    return json.loads(match.group(1)) if match else []


class AttributionCleaningTests(TestCase):
    def test_keeps_plain_utm_tokens_only(self):
        cleaned = clean_attribution({
            "utm_source": "facebook", "utm_medium": "paid_social", "utm_campaign": "gl_horeca",
            "utm_content": "gl004", "utm_term": "a@b.gr", "gclid": "abc", "ref": "x",
        })
        self.assertEqual(cleaned, {
            "utm_source": "facebook", "utm_medium": "paid_social", "utm_campaign": "gl_horeca",
            "utm_content": "gl004",
        })

    def test_drops_free_text_urls_and_greek(self):
        for bad in ("two words", "https://x.gr", "καφές", "<script>", "x" * 65, ""):
            self.assertEqual(clean_attribution({"utm_campaign": bad}), {}, bad)

    def test_query_suffix(self):
        self.assertEqual(attribution_query({}), "")
        self.assertEqual(attribution_query({"utm_campaign": "gl_insurance"}), "?utm_campaign=gl_insurance")

    def test_unknown_event_names_are_refused(self):
        with self.assertRaises(ValueError):
            analytics_event("purchase_everything")

    def test_event_params_never_carry_free_text(self):
        event = analytics_event("sign_up", {"method": "email", "email": "someone@example.com", "note": "δύο λέξεις"})
        self.assertEqual(event["params"], {"method": "email"})


class LandingPageTests(TestCase):
    def test_both_pages_render_for_anonymous_visitors(self):
        for name, headline in (
            ("landing_horeca", "Βρες νέες επιχειρήσεις που μπορεί να γίνουν πελάτες σου."),
            ("landing_insurance", "Ανακάλυψε νέες επιχειρήσεις για πιθανές ασφαλιστικές συνεργασίες."),
        ):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, headline)
            self.assertContains(response, "ΕΙΚΟΝΙΚΟ ΔΕΙΓΜΑ")  # sample rows are labelled fictional
            self.assertContains(response, "public-radar-sample")

    def test_routes(self):
        self.assertEqual(reverse("landing_horeca"), "/for/horeca/")
        self.assertEqual(reverse("landing_insurance"), "/for/insurance/")

    def test_pages_are_not_reskins_of_each_other(self):
        horeca = self.client.get(reverse("landing_horeca")).content.decode()
        insurance = self.client.get(reverse("landing_insurance")).content.decode()
        self.assertIn("ΑΠΟ ΤΗ ΓΡΑΜΜΗ ΠΡΟΪΟΝΤΩΝ ΣΤΟΥΣ ΚΑΔ", horeca)
        self.assertNotIn("ΑΠΟ ΤΗ ΓΡΑΜΜΗ ΠΡΟΪΟΝΤΩΝ ΣΤΟΥΣ ΚΑΔ", insurance)
        self.assertIn("Χωρίς υποσχέσεις", insurance)
        self.assertIn("Επικοινώνησα", insurance)  # the real lead statuses

    def test_insurance_page_promises_no_need_or_sale(self):
        content = self.client.get(reverse("landing_insurance")).content.decode()
        self.assertIn("όχι ένδειξη ασφαλιστικής ανάγκης", content)
        for overclaim in ("εγγυημέν", "σίγουρ", "χρειάζονται ασφάλιση"):
            self.assertNotIn(overclaim, content)

    def test_signup_links_carry_only_sanitised_utms(self):
        response = self.client.get(reverse("landing_horeca"), {
            "utm_source": "facebook", "utm_campaign": "gl_horeca", "utm_content": "gl004",
            "utm_term": "me@example.com",
        })
        content = response.content.decode()
        self.assertIn("/signup/?utm_source=facebook&amp;utm_campaign=gl_horeca&amp;utm_content=gl004", content)
        self.assertNotIn("example.com", content)
        # Every signup link on the page -- hero, final CTA and the topbar/menu -- keeps the campaign.
        self.assertNotIn('href="/signup/"', content)

    def test_without_utms_signup_links_are_plain(self):
        response = self.client.get(reverse("landing_insurance"))
        self.assertContains(response, 'href="/signup/"')

    def test_ctas_are_tagged_for_analytics(self):
        response = self.client.get(reverse("landing_insurance"))
        self.assertContains(response, 'data-analytics="landing_cta_click" data-analytics-audience="insurance"')

    def test_no_session_is_created_for_an_anonymous_visit(self):
        response = self.client.get(reverse("landing_horeca"), {"utm_campaign": "gl_horeca"})
        self.assertNotIn("sessionid", response.cookies)

    def test_listed_in_sitemap_and_allowed_in_robots(self):
        sitemap = self.client.get("/sitemap.xml").content.decode()
        self.assertIn("/for/horeca/", sitemap)
        self.assertIn("/for/insurance/", sitemap)
        self.assertIn("Allow: /for/", self.client.get("/robots.txt").content.decode())


@override_settings(GA_MEASUREMENT_ID="G-TEST123")
class ConversionEventTests(TestCase):
    def _signup(self, query=""):
        with mock.patch("gemiapp.views.send_verification_email"):
            return self.client.post(reverse("signup") + query, {
                "first_name": "Νίκος", "email": "new@example.com",
                "password1": "Abcdef12345!", "password2": "Abcdef12345!",
            })

    def test_signup_emits_sign_up_with_campaign_but_no_pii(self):
        response = self._signup("?utm_campaign=gl_horeca&utm_content=gl005&utm_source=meta")
        self.assertEqual(response.status_code, 200)
        events = _events(response, "gemiPageEvents")
        self.assertEqual(events, [{"name": "sign_up", "params": {
            "method": "email", "utm_campaign": "gl_horeca", "utm_content": "gl005",
        }}])
        self.assertNotIn("new@example.com", json.dumps(events))

    def test_signup_form_is_tagged_for_registration_start(self):
        self.assertContains(self.client.get(reverse("signup")), 'data-analytics-form-start="sign_up_start"')

    @override_settings(GA_MEASUREMENT_ID="")
    def test_no_event_markup_without_analytics_configured(self):
        response = self._signup()
        self.assertNotContains(response, "gemiPageEvents")

    def test_email_verification_queues_one_shot_event(self):
        user = User.objects.create_user("v@example.com", "v@example.com", "Abcdef12345!", is_active=False)
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        self.client.get(reverse("verify_email", args=[uid, token]))
        first = self.client.get(reverse("settings"))
        self.assertEqual(_events(first, "gemiQueuedEvents"), [{"name": "email_verified", "params": {}}])
        second = self.client.get(reverse("settings"))
        self.assertEqual(_events(second, "gemiQueuedEvents"), [])  # emitted once, then gone
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_filtered_dashboard_reports_filter_kinds_not_values(self):
        user = User.objects.create_user("d@example.com", "d@example.com", "Abcdef12345!")
        self.client.force_login(user)
        response = self.client.get(reverse("dashboard"), {"q": "ΑΦΜ 123456789", "prefecture": "ΑΤΤΙΚΗΣ"})
        events = _events(response, "gemiPageEvents")
        self.assertEqual(events[0]["name"], "product_search")
        self.assertEqual(events[0]["params"], {
            "has_kad": False, "has_region": True, "has_legal_form": False, "has_date": False, "has_name": True,
        })
        self.assertNotIn("123456789", json.dumps(events))

    def test_unfiltered_dashboard_reports_nothing(self):
        user = User.objects.create_user("u@example.com", "u@example.com", "Abcdef12345!")
        self.client.force_login(user)
        self.assertEqual(_events(self.client.get(reverse("dashboard")), "gemiPageEvents"), [])

    def test_creating_a_radar_queues_radar_created(self):
        user = User.objects.create_user("r@example.com", "r@example.com", "Abcdef12345!")
        UserSubscription.objects.filter(user=user).update(tier="pro", status="active")
        self.client.force_login(user)
        response = self.client.post(reverse("radar_create"), {
            "name": "Καφέ Αττική", "frequency": "daily", "only_active": "on",
        }, follow=True)
        self.assertEqual(_events(response, "gemiQueuedEvents"), [{"name": "radar_created", "params": {"kad_count": 0}}])

    def test_search_params_helper_reads_presence_only(self):
        request = mock.Mock()
        request.GET = QueryDict("kad=56.30&date_from=2026-10-01")
        self.assertEqual(search_event_params(request), {
            "has_kad": True, "has_region": False, "has_legal_form": False, "has_date": True, "has_name": False,
        })


class ConsentWithdrawalTests(TestCase):
    @override_settings(GA_MEASUREMENT_ID="G-TEST123")
    def test_consent_can_be_reopened_from_footer_and_privacy_page(self):
        self.assertContains(self.client.get(reverse("landing_horeca")), "data-cookie-settings")
        self.assertContains(self.client.get(reverse("privacy")), "data-cookie-settings")

    @override_settings(GA_MEASUREMENT_ID="")
    def test_no_consent_control_when_analytics_is_off(self):
        self.assertNotContains(self.client.get(reverse("privacy")), "data-cookie-settings")


class AccuracyGuardTests(TestCase):
    def test_free_vs_paid_is_stated_next_to_the_cta_and_claims_stay_accurate(self):
        for name in ("landing_horeca", "landing_insurance"):
            content = self.client.get(reverse(name)).content.decode()
            self.assertIn('class="public-cta-note"', content)
            self.assertIn("Με συνδρομή:", content)
            for wrong in ("ανοίγουν", "και υποκωδικοί", "Κάθε νέα εταιρεία", "Κάθε νέο εστιατόριο"):
                self.assertNotIn(wrong, content, (name, wrong))
        self.assertIn("δεν περιλαμβάνει αυτόματα τους υποκωδικούς", self.client.get(reverse("landing_horeca")).content.decode())

    def test_every_internal_link_on_both_pages_resolves(self):
        for name in ("landing_horeca", "landing_insurance"):
            content = self.client.get(reverse(name)).content.decode()
            for href in set(re.findall(r'href="(/[^"#]*)', content)):
                if href.startswith("/static/"):
                    continue
                status = self.client.get(href.replace("&amp;", "&")).status_code
                self.assertIn(status, (200, 302), (name, href, status))

    @override_settings(GA_MEASUREMENT_ID="G-TEST123")
    def test_ga_is_never_loaded_by_markup_only_by_consent_script(self):
        content = self.client.get(reverse("landing_horeca")).content.decode()
        self.assertNotIn('<script async src="https://www.googletagmanager.com', content)
        self.assertIn("if (value === 'granted') { loadAnalytics(); sendPageEvents(); }", content)


class HomepageAccuracyTests(TestCase):
    def test_home_states_exact_kad_matching_and_implies_no_insurance_need(self):
        content = self.client.get(reverse("home")).content.decode()
        self.assertNotIn("ολόκληρο κλάδο", content)
        self.assertIn("δεν περιλαμβάνει αυτόματα τους υποκωδικούς", content)
        self.assertNotIn("που χρειάζονται κάλυψη", content)


class GeographyClaimTests(TestCase):
    def test_public_pages_claim_prefecture_level_filtering_only(self):
        # The customer Radar form and the dashboard filter by νομός only (CustomerRadarForm.prefectures).
        for name in ("home", "pricing", "landing_horeca", "landing_insurance"):
            content = self.client.get(reverse(name)).content.decode()
            for claim in ("πόλεις", "πόλη", "δήμο", "Δήμο"):
                self.assertNotIn(claim, content, (name, claim))
