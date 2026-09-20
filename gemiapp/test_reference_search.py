"""Tests for the GEMI reference search behind the Organization Radar criteria pickers.

The catalogue is canonical platform metadata, so the endpoint needs a signed-in user and nothing more; who may
*write* a Radar stays the decision of organization_access. Every row used here is created by the test, never
read from production data.

The matching rules that matter to a Greek customer: case-insensitive, accent-insensitive, by code and by
description, bounded, and returning exactly the value the Radar form posts for that row.
"""

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .models import GemiKad, GemiLegalType, GemiMunicipality, GemiPrefecture
from .reference_search import (
    DEFAULT_LIMIT, KAD, KINDS, LEGAL_FORM, MAX_LIMIT, MUNICIPALITY, PREFECTURE, ReferenceHit, browse_reference,
    reference_options, search_reference,
)
from .test_customer_workspace import WorkspaceTestCase
from .test_organization_icp import ref


def kad(code, description, version="kad_2026"):
    return ref(GemiKad, code, description=description, kad_version=version)


class CatalogueFixture:
    """A small, self-made catalogue. Nothing here reads production reference data."""

    def build_catalogue(self):
        # Codes and ids no shared fixture claims, so the catalogue is entirely this test's own.
        self.food = kad("56101000", "ΥΠΗΡΕΣΙΕΣ ΕΣΤΙΑΣΗΣ")
        self.retail = kad("47191003", "ΛΙΑΝΙΚΟ ΕΜΠΟΡΙΟ ΕΙΔΩΝ ΔΩΡΩΝ")
        self.old_food = kad("56101000", "ΥΠΗΡΕΣΙΕΣ ΕΣΤΙΑΣΗΣ (ΠΑΛΑΙΟ)", version="kad_2008")
        self.chios = ref(GemiPrefecture, "81", description="ΧΙΟΥ")
        self.attica = ref(GemiPrefecture, "5001", description="ΑΤΤΙΚΗΣ ΔΟΚΙΜΗΣ")
        self.kifisia = ref(GemiMunicipality, "611901", description="ΚΗΦΙΣΙΑΣ", source_prefecture_id="5001")
        self.ike = ref(GemiLegalType, "1901", description="ΙΔΙΩΤΙΚΗ ΚΕΦΑΛΑΙΟΥΧΙΚΗ ΕΤΑΙΡΕΙΑ (ΙΚΕ)")


class SearchTestCase(CatalogueFixture, TestCase):
    def setUp(self):
        self.build_catalogue()

    def values(self, kind, query):
        return [hit.value for hit in search_reference(kind, query)]

    def labels(self, kind, query):
        return [hit.label for hit in search_reference(kind, query)]


class KadSearchTests(SearchTestCase):
    def test_a_code_prefix_finds_the_kad(self):
        self.assertIn("47191003 kad_2026", self.values(KAD, "4719"))
        self.assertIn("47191003 kad_2026", self.values(KAD, "47191003"))

    def test_a_greek_description_finds_the_kad(self):
        self.assertEqual(set(self.values(KAD, "ΕΣΤΙΑΣΗ")), {"56101000 kad_2026", "56101000 kad_2008"})

    def test_search_is_case_and_accent_insensitive(self):
        expected = set(self.values(KAD, "ΕΣΤΙΑΣΗΣ"))
        self.assertTrue(expected)
        for spelling in ("εστιασησ", "Εστίασης", "ΕΣΤΊΑΣΗΣ", "  εστίασης  "):
            self.assertEqual(set(self.values(KAD, spelling)), expected, spelling)

    def test_every_token_must_match(self):
        self.assertEqual(self.values(KAD, "ΛΙΑΝΙΚΟ ΔΩΡΩΝ"), ["47191003 kad_2026"])
        self.assertEqual(self.values(KAD, "ΛΙΑΝΙΚΟ ΕΣΤΙΑΣΗΣ"), [])

    def test_the_label_is_code_then_description(self):
        self.assertEqual(self.labels(KAD, "47191003"), ["47191003 — ΛΙΑΝΙΚΟ ΕΜΠΟΡΙΟ ΕΙΔΩΝ ΔΩΡΩΝ"])

    def test_the_kad_version_is_kept_in_the_value_and_shown_as_detail(self):
        hits = {hit.value: hit.detail for hit in search_reference(KAD, "ΕΣΤΙΑΣΗ")}
        self.assertEqual(hits, {"56101000 kad_2026": "ΚΑΔ 2026", "56101000 kad_2008": "ΚΑΔ 2008"})

    def test_a_retired_kad_is_never_offered(self):
        GemiKad.objects.filter(pk=self.retail.pk).update(is_present=False)
        self.assertEqual(self.values(KAD, "47191003"), [])

    def test_a_short_query_returns_nothing_rather_than_the_catalogue(self):
        for query in ("", " ", "4", "Ε"):
            self.assertEqual(search_reference(KAD, query), [], query)

    def test_results_are_bounded(self):
        for index in range(MAX_LIMIT + 20):
            kad(f"9900{index:04d}", "ΔΟΚΙΜΑΣΤΙΚΗ ΔΡΑΣΤΗΡΙΟΤΗΤΑ")
        self.assertEqual(len(search_reference(KAD, "ΔΟΚΙΜΑΣΤΙΚΗ")), DEFAULT_LIMIT)
        self.assertEqual(len(search_reference(KAD, "ΔΟΚΙΜΑΣΤΙΚΗ", limit=10_000)), MAX_LIMIT)
        self.assertEqual(len(search_reference(KAD, "ΔΟΚΙΜΑΣΤΙΚΗ", limit=5)), 5)


class RegionAndLegalFormSearchTests(SearchTestCase):
    def test_a_prefecture_is_found_by_a_prefix_of_its_name(self):
        hits = search_reference(PREFECTURE, "ΧΙΟ")
        self.assertEqual([(hit.value, hit.label) for hit in hits], [(str(self.chios.pk), "ΧΙΟΥ")])

    def test_a_municipality_is_found_by_name_and_carries_its_prefecture(self):
        hits = search_reference(MUNICIPALITY, "κηφισιας")
        self.assertEqual([(hit.value, hit.label, hit.detail) for hit in hits],
                         [(str(self.kifisia.pk), "ΚΗΦΙΣΙΑΣ", "ΑΤΤΙΚΗΣ ΔΟΚΙΜΗΣ")])

    def test_a_municipality_is_also_findable_through_its_prefecture(self):
        self.assertEqual(self.values(MUNICIPALITY, "ΚΗΦΙΣΙΑΣ ΑΤΤΙΚΗΣ"), [str(self.kifisia.pk)])

    def test_a_legal_form_is_found_by_description(self):
        self.assertEqual(self.values(LEGAL_FORM, "ΙΚΕ"), [str(self.ike.pk)])
        self.assertEqual(self.values(LEGAL_FORM, "κεφαλαιουχικη"), [str(self.ike.pk)])

    def test_the_posted_value_of_a_reference_row_is_its_primary_key(self):
        for kind, row in ((PREFECTURE, self.attica), (MUNICIPALITY, self.kifisia), (LEGAL_FORM, self.ike)):
            hit = search_reference(kind, (row.description or "")[:6])[0]
            self.assertEqual(hit.value, str(row.pk), kind)

    def test_a_retired_row_is_never_offered(self):
        GemiPrefecture.objects.filter(pk=self.chios.pk).update(is_present=False)
        self.assertEqual(search_reference(PREFECTURE, "ΧΙΟ"), [])


class BrowseTests(SearchTestCase):
    """What the picker offers the moment it is opened, before anyone has typed."""

    def test_opening_a_picker_offers_rows_instead_of_an_empty_panel(self):
        self.assertIn("56101000 kad_2026", [hit.value for hit in browse_reference(KAD)])

    def test_the_opening_list_is_bounded_and_never_the_catalogue(self):
        for index in range(MAX_LIMIT + 20):
            kad(f"9901{index:04d}", "ΔΟΚΙΜΑΣΤΙΚΗ ΔΡΑΣΤΗΡΙΟΤΗΤΑ")
        self.assertEqual(len(browse_reference(KAD)), DEFAULT_LIMIT)
        self.assertEqual(len(browse_reference(KAD, limit=10_000)), MAX_LIMIT)
        self.assertEqual(len(browse_reference(KAD, limit=5)), 5)

    def test_an_opened_row_posts_the_same_value_a_searched_one_does(self):
        searched = {hit.value: hit for hit in search_reference(KAD, "ΕΣΤΙΑΣΗ")}
        browsed = {hit.value: hit for hit in browse_reference(KAD)}
        self.assertTrue(searched)
        for value, hit in searched.items():
            self.assertEqual(browsed.get(value), hit, value)

    def test_a_retired_row_is_never_offered(self):
        GemiPrefecture.objects.filter(pk=self.chios.pk).update(is_present=False)
        self.assertNotIn(str(self.chios.pk), [hit.value for hit in browse_reference(PREFECTURE)])

    def test_every_kind_opens_with_something(self):
        for kind in KINDS:
            self.assertTrue(browse_reference(kind), kind)

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            browse_reference("companies")


class ReferenceOptionsTests(SearchTestCase):
    """Opening the dropdown and typing into it are the same question, so they are one entry point."""

    def test_a_query_too_short_to_search_opens_the_list(self):
        opening = [hit.value for hit in browse_reference(PREFECTURE)]
        self.assertTrue(opening)
        for query in ("", "   ", "Χ"):
            self.assertEqual([hit.value for hit in reference_options(PREFECTURE, query)], opening, repr(query))

    def test_typing_filters_that_same_list(self):
        self.assertEqual([hit.value for hit in reference_options(PREFECTURE, "ΧΙΟ")], [str(self.chios.pk)])
        self.assertLess(len(reference_options(PREFECTURE, "ΧΙΟ")), len(reference_options(PREFECTURE, "")))

    def test_filtering_is_exactly_the_search(self):
        for kind, query in ((KAD, "ΕΣΤΙΑΣΗ"), (MUNICIPALITY, "κηφισιας"), (LEGAL_FORM, "ΙΚΕ")):
            self.assertEqual(reference_options(kind, query), search_reference(kind, query), kind)

    def test_the_result_is_bounded_whatever_the_query(self):
        for index in range(MAX_LIMIT + 20):
            kad(f"9902{index:04d}", "ΔΟΚΙΜΑΣΤΙΚΗ ΔΡΑΣΤΗΡΙΟΤΗΤΑ")
        for query in ("", "ΔΟΚΙΜΑΣΤΙΚΗ"):
            self.assertEqual(len(reference_options(KAD, query, limit=10_000)), MAX_LIMIT, repr(query))


class ContractTests(SimpleTestCase):
    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            search_reference("companies", "ΧΙΟ")

    def test_the_kinds_are_exactly_the_four_reference_tables(self):
        self.assertEqual(KINDS, (KAD, PREFECTURE, MUNICIPALITY, LEGAL_FORM))

    def test_a_hit_serialises_to_value_label_detail(self):
        self.assertEqual(ReferenceHit("1", "ΧΙΟΥ", "").as_dict(), {"value": "1", "label": "ΧΙΟΥ", "detail": ""})


class EndpointTests(CatalogueFixture, WorkspaceTestCase):
    """The HTTP surface: signed in, bounded, JSON, and no tenant data anywhere in it."""

    def setUp(self):
        super().setUp()
        self.build_catalogue()
        self.url = reverse("reference_search")

    def get(self, who=None, **params):
        if who is not None:
            self.client.force_login(who)
        return self.client.get(self.url, params)

    def test_an_anonymous_visitor_is_sent_to_the_login_page(self):
        self.client.logout()
        response = self.get(kind=KAD, q="ΕΣΤΙΑΣΗ")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_a_signed_in_user_searches_the_catalogue(self):
        response = self.get(self.owner, kind=KAD, q="ΕΣΤΙΑΣΗ")
        self.assertEqual(response.status_code, 200)
        values = [item["value"] for item in response.json()["results"]]
        self.assertEqual(set(values), {"56101000 kad_2026", "56101000 kad_2008"})

    def test_every_kind_is_reachable(self):
        for kind, query in ((KAD, "4719"), (PREFECTURE, "ΧΙΟ"), (MUNICIPALITY, "ΚΗΦΙΣΙΑΣ"), (LEGAL_FORM, "ΙΚΕ")):
            response = self.get(self.owner, kind=kind, q=query)
            self.assertEqual(response.status_code, 200, kind)
            self.assertTrue(response.json()["results"], kind)

    def test_opening_a_picker_asks_for_a_bounded_list(self):
        """No query at all is the dropdown opening: it answers with rows, not with nothing."""
        response = self.get(self.owner, kind=KAD)
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertTrue(results)
        self.assertLessEqual(len(results), MAX_LIMIT)
        self.assertEqual(sorted(results[0]), ["detail", "label", "value"])

    def test_the_opening_list_can_never_be_the_whole_catalogue(self):
        for index in range(MAX_LIMIT + 20):
            kad(f"9903{index:04d}", "ΔΟΚΙΜΑΣΤΙΚΗ ΔΡΑΣΤΗΡΙΟΤΗΤΑ")
        self.assertEqual(len(self.get(self.owner, kind=KAD, q="").json()["results"]), DEFAULT_LIMIT)

    def test_typing_filters_the_list_the_dropdown_opened_with(self):
        opened = self.get(self.owner, kind=PREFECTURE, q="").json()["results"]
        typed = self.get(self.owner, kind=PREFECTURE, q="ΧΙΟ").json()["results"]
        self.assertEqual([item["value"] for item in typed], [str(self.chios.pk)])
        self.assertGreater(len(opened), len(typed))

    def test_every_kind_opens(self):
        for kind in KINDS:
            self.assertTrue(self.get(self.owner, kind=kind).json()["results"], kind)

    def test_an_unknown_kind_is_a_bad_request(self):
        response = self.get(self.owner, kind="companies", q="ΧΙΟ")
        self.assertEqual((response.status_code, response.json()["results"]), (400, []))

    def test_a_member_of_another_tenant_may_still_search_the_platform_catalogue(self):
        # The rows are canonical GEMI metadata, identical for everyone; only Radar writes are tenant-scoped.
        response = self.get(self.b_owner, kind=PREFECTURE, q="ΧΙΟ")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["results"])

    def test_other_methods_are_refused(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(self.url, {"kind": KAD, "q": "4719"}).status_code, 405)
