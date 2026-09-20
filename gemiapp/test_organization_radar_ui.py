"""Tests for customer-facing Organization Radar create / edit / activate.

Owners and admins of an entitled organization (``manage_radars``) create and edit Radars through the C3 domain
service; everyone else -- other roles, other tenants, unpaid organizations -- is refused and nothing is written. The
form exposes only matcher-supported criteria, resolves every GEMI reference to a present row and reports the C3 rules
in Greek. Saving a Radar is configuration only: no signal, opportunity, notification, audit event or schedule.
"""

import re
from pathlib import Path

from django.contrib.messages import get_messages
from django.test import SimpleTestCase
from django_q.models import Schedule
from django.urls import reverse

from .models import (
    CompanySignal, CustomerRadar, GemiKad, GemiLegalType, GemiMunicipality, GemiPrefecture, Opportunity,
    OrganizationAuditEvent, OrganizationNotification, OrganizationRadar, OrganizationRadarKad, UserSubscription,
)
from .organization_radar_form import radar_error_message
from .organization_radars import RadarDefinition, RadarError, get_organization_radar_definition
from .test_customer_workspace import WorkspaceTestCase
from .test_gemi_company_activities import entitled_user, radar_for
from .test_organization_icp import ref


class RadarUiTestCase(WorkspaceTestCase):
    def setUp(self):
        super().setUp()
        r = self.r
        GemiPrefecture.objects.filter(pk=r.attica.pk).update(description="Αττικής")
        GemiMunicipality.objects.filter(pk=r.kifisia.pk).update(description="Κηφισιάς")
        GemiLegalType.objects.filter(pk=r.ike.pk).update(description="ΙΚΕ")
        GemiLegalType.objects.filter(pk=r.oe.pk).update(description="ΟΕ")

    def create_url(self, org=None):
        return reverse("organization_radar_create", args=[(org or self.org).pk])

    def edit_url(self, radar, org=None):
        return reverse("organization_radar_edit", args=[(org or self.org).pk, radar.pk])

    def active_url(self, radar, org=None):
        return reverse("organization_radar_active", args=[(org or self.org).pk, radar.pk])

    def good_form(self, **overrides):
        r = self.r
        data = {"name": "Νέα λογισμικού στην Αττική", "active": "1", "score_threshold": "70",
                "kads": "47191002", "prefectures": [str(r.attica.pk)], "legal_forms": [str(r.ike.pk)],
                "signal_types": ["new_company", "kad_added"], "excluded_municipalities": [str(r.kifisia.pk)],
                "excluded_legal_forms": [str(r.oe.pk)]}
        data.update(overrides)
        return {key: value for key, value in data.items() if value is not None}

    def post(self, who, url, data=None):
        self.client.force_login(who)
        return self.client.post(url, data or {})

    def world(self):
        return (list(OrganizationRadar.objects.order_by("pk").values()),
                list(OrganizationRadarKad.objects.order_by("pk").values()))

    def pipeline_world(self):
        return (list(Opportunity.objects.values()), list(CompanySignal.objects.values()),
                OrganizationNotification.objects.count(), OrganizationAuditEvent.objects.count(),
                sorted(Schedule.objects.values_list("func", flat=True)), list(CustomerRadar.objects.values()),
                list(UserSubscription.objects.values()))


# --- create -------------------------------------------------------------------------------------------------

class CreateTests(RadarUiTestCase):
    def test_an_entitled_owner_creates_a_radar_with_exactly_the_posted_criteria(self):
        r = self.r
        before = OrganizationRadar.objects.filter(organization=self.org).count()
        response = self.post(self.owner, self.create_url(), self.good_form())
        self.assertRedirects(response, self.ws_url("organization_radars"), fetch_redirect_response=False)
        self.assertIn("Το Radar δημιουργήθηκε.", [str(m) for m in get_messages(response.wsgi_request)])
        radar = OrganizationRadar.objects.filter(organization=self.org).latest("pk")
        self.assertEqual(OrganizationRadar.objects.filter(organization=self.org).count(), before + 1)
        self.assertEqual(get_organization_radar_definition(self.org, radar), RadarDefinition(
            name="Νέα λογισμικού στην Αττική", active=True, score_threshold=70, kads=(r.kad_other,),
            regions=(r.attica,), legal_forms=(r.ike,), signal_types=("new_company", "kad_added"),
            exclusions=(r.kifisia, r.oe)))

    def test_an_admin_may_create_and_edit_other_roles_may_not(self):
        self.assertEqual(self.post(self.members["admin"], self.create_url(), self.good_form()).status_code, 302)
        radar = OrganizationRadar.objects.filter(organization=self.org).latest("pk")
        self.assertEqual(self.post(self.members["admin"], self.edit_url(radar),
                                   self.good_form(name="Μετονομασμένο")).status_code, 302)
        self.assertEqual(OrganizationRadar.objects.get(pk=radar.pk).name, "Μετονομασμένο")
        world = self.world()
        for role in ("sales_manager", "sales_user", "viewer"):
            who = self.members[role]
            self.client.force_login(who)
            for url in (self.create_url(), self.edit_url(radar)):
                self.assertEqual(self.client.get(url).status_code, 404, (role, url))
                self.assertEqual(self.client.post(url, self.good_form(name=role)).status_code, 404, (role, url))
            self.assertEqual(self.client.post(self.active_url(radar), {"active": "0"}).status_code, 404, role)
        self.assertEqual(self.world(), world)

    def test_an_unpaid_organization_cannot_create_edit_or_toggle(self):
        UserSubscription.objects.filter(user=self.owner).update(tier="free", status="inactive")
        radar = self.row.radar
        world = self.world()
        self.client.force_login(self.owner)
        for response in (self.client.get(self.create_url()), self.client.post(self.create_url(), self.good_form()),
                         self.client.get(self.edit_url(radar)), self.client.post(self.edit_url(radar), self.good_form()),
                         self.client.post(self.active_url(radar), {"active": "0"})):
            self.assertEqual((response.status_code, response["Location"]), (302, reverse("pricing")))
        self.assertEqual(self.world(), world)

    def test_other_tenants_can_neither_see_nor_touch_a_radar(self):
        b_radar = self.radar_b_full
        world = self.world()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.edit_url(b_radar)).status_code, 404)              # A's door, B's radar
        self.assertEqual(self.client.get(self.edit_url(b_radar, self.org_b)).status_code, 404)  # B's door
        self.assertEqual(self.client.post(self.edit_url(b_radar), self.good_form()).status_code, 404)
        self.assertEqual(self.client.post(self.active_url(b_radar), {"active": "0"}).status_code, 404)
        self.assertEqual(self.client.get(self.create_url(self.org_b)).status_code, 404)
        self.client.force_login(self.b_owner)
        self.assertEqual(self.client.post(self.edit_url(self.row.radar, self.org_b), self.good_form()).status_code, 404)
        self.assertEqual(self.world(), world)

    def test_other_methods_are_refused(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.put(self.create_url()).status_code, 405)
        self.assertEqual(self.client.get(self.active_url(self.row.radar)).status_code, 405)


# --- edit ----------------------------------------------------------------------------------------------------

class EditTests(RadarUiTestCase):
    def test_the_form_is_prefilled_with_the_stored_definition(self):
        radar = self.row.radar
        self.client.force_login(self.owner)
        html = self.client.get(self.edit_url(radar)).content.decode()
        self.assertIn(f'value="{radar.name}"', html)
        self.assertIn("62010000 kad_2026", html)
        self.assertRegex(html, rf'<option value="{self.r.attica.pk}" selected>Αττικής</option>')
        self.assertRegex(html, r'value="new_company" checked')
        self.assertIn("Επεξεργασία Radar", html)

    def test_edit_replaces_the_whole_configuration_atomically(self):
        r, radar = self.r, self.row.radar
        response = self.post(self.owner, self.edit_url(radar), self.good_form(
            name="Μόνο γεγονότα", active="1", score_threshold="", kads="", prefectures=[], legal_forms=[],
            signal_types=["status_changed"], excluded_municipalities=[], excluded_legal_forms=[]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(get_organization_radar_definition(self.org, OrganizationRadar.objects.get(pk=radar.pk)),
                         RadarDefinition(name="Μόνο γεγονότα", active=True, signal_types=("status_changed",)))
        # a rejected edit leaves the stored configuration exactly as it was
        before = get_organization_radar_definition(self.org, radar)
        response = self.post(self.owner, self.edit_url(radar), self.good_form(kads="47191002\n47191002"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_organization_radar_definition(self.org, radar), before)

    def test_the_kad_version_is_explicit_when_a_code_has_two(self):
        response = self.post(self.owner, self.create_url(), self.good_form(kads="62010000"))
        self.assertContains(response, "υπάρχει σε περισσότερες εκδόσεις (2008, 2026)")
        self.post(self.owner, self.create_url(), self.good_form(name="2008", kads="62.01.00.00 2008"))
        radar = OrganizationRadar.objects.get(organization=self.org, name="2008")
        self.assertEqual(get_organization_radar_definition(self.org, radar).kads, (self.r.kad_2008,))


# --- validation ------------------------------------------------------------------------------------------------

class ValidationTests(RadarUiTestCase):
    def test_invalid_criteria_are_rejected_with_a_greek_message_and_nothing_is_written(self):
        r = self.r
        retired = ref(GemiPrefecture, "999", description="Αποσυρμένη", present=False)
        cases = {
            "unknown KAD": ({"kads": "99999999"}, "δεν υπάρχει στα τρέχοντα στοιχεία ΓΕΜΗ"),
            "malformed KAD": ({"kads": "abc"}, "δεν είναι έγκυρος ΚΑΔ"),
            "bad KAD version": ({"kads": "47191002 2019"}, "δεν είναι έγκυρος ΚΑΔ"),
            "unknown region": ({"prefectures": ["987654"]}, "δεν υπάρχει ή έχει αποσυρθεί"),
            "retired region": ({"prefectures": [str(retired.pk)]}, "δεν υπάρχει ή έχει αποσυρθεί"),
            "non-numeric id": ({"legal_forms": ["ike"]}, "μη έγκυρη επιλογή"),
            "score above 100": ({"score_threshold": "150"}, "από 0 έως 100"),
            "negative score": ({"score_threshold": "-1"}, "από 0 έως 100"),
            "fractional score": ({"score_threshold": "7.5"}, "από 0 έως 100"),
            "unimplemented signal": ({"signal_types": ["capital_increase"]}, "δεν παρακολουθείται ακόμη"),
            "unknown signal": ({"signal_types": ["bogus"]}, "Άγνωστος τύπος γεγονότος"),
            "duplicate KAD": ({"kads": "47191002\n47191002"}, "δηλώθηκε δύο φορές"),
            "duplicate region": ({"prefectures": [str(r.attica.pk), str(r.attica.pk)]}, "δηλώθηκε δύο φορές"),
            "targeted and excluded": ({"excluded_legal_forms": [str(r.ike.pk)]}, "ταυτόχρονα στόχος και αποκλεισμός"),
            "active with no criterion": ({"kads": "", "prefectures": [], "legal_forms": [], "signal_types": []},
                                         "χρειάζεται τουλάχιστον"),
            "no name": ({"name": "   "}, "χρειάζεται όνομα"),
            "long name": ({"name": "α" * 81}, "έως 80 χαρακτήρες"),
        }
        world, pipeline = self.world(), self.pipeline_world()
        for label, (overrides, message) in cases.items():
            response = self.post(self.owner, self.create_url(), self.good_form(**overrides))
            self.assertEqual(response.status_code, 200, label)
            self.assertContains(response, "data-form-errors", msg_prefix=label)
            self.assertContains(response, message, msg_prefix=label)
        self.assertEqual((self.world(), self.pipeline_world()), (world, pipeline))

    def test_an_inactive_draft_may_have_no_criteria(self):
        response = self.post(self.owner, self.create_url(), {"name": "Πρόχειρο"})
        self.assertEqual(response.status_code, 302)
        radar = OrganizationRadar.objects.get(organization=self.org, name="Πρόχειρο")
        self.assertEqual((radar.active, get_organization_radar_definition(self.org, radar).has_positive_criteria),
                         (False, False))

    def test_every_domain_rule_has_a_greek_message(self):
        for text in ("a Radar needs a name", "a Radar name may not exceed 80 characters",
                     "score_threshold must be a whole number from 0 to 100", "KAD 1 is retired from the GEMI reference data",
                     "the same criterion cannot be both targeted and excluded: []", "duplicate KAD",
                     "unknown signal type: 'x'", "x has no implemented detector and cannot be watched yet",
                     "an active Radar needs at least one KAD, region, legal form or signal type"):
            self.assertNotEqual(radar_error_message(RadarError(text)), "Ο ορισμός του Radar δεν είναι έγκυρος.", text)


# --- activation, list, isolation of the pipeline ------------------------------------------------------------

class ActivationAndListTests(RadarUiTestCase):
    def test_deactivate_and_reactivate(self):
        radar = self.row.radar
        response = self.post(self.owner, self.active_url(radar), {"active": "0"})
        self.assertRedirects(response, self.ws_url("organization_radars"), fetch_redirect_response=False)
        self.assertFalse(OrganizationRadar.objects.get(pk=radar.pk).active)
        self.post(self.members["admin"], self.active_url(radar), {"active": "1"})
        self.assertTrue(OrganizationRadar.objects.get(pk=radar.pk).active)

    def test_an_unconfigured_radar_cannot_be_activated(self):
        self.post(self.owner, self.create_url(), {"name": "Κενό"})
        empty = OrganizationRadar.objects.get(organization=self.org, name="Κενό")
        response = self.post(self.owner, self.active_url(empty), {"active": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(radar_error_message(RadarError("an active Radar needs")),
                      [str(m) for m in get_messages(response.wsgi_request)])
        self.assertFalse(OrganizationRadar.objects.get(pk=empty.pk).active)

    def test_the_list_is_actionable_for_managers_and_read_only_for_others(self):
        radar = self.row.radar
        owner_html = self.html(self.owner, "organization_radars")
        self.assertIn(f'href="{self.create_url()}"', owner_html)
        self.assertIn(f'href="{self.edit_url(radar)}"', owner_html)
        self.assertIn(f'action="{self.active_url(radar)}"', owner_html)
        self.assertIn("Αττικής", owner_html)            # readable criteria, not ids
        self.assertIn('data-criteria="ΚΑΔ"', owner_html)
        self.assertIn('data-radar-state="active"', owner_html)
        viewer_html = self.html(self.members["viewer"], "organization_radars")
        for control in ("data-new-radar", "data-edit-radar", "data-toggle-radar", "<form"):
            self.assertNotIn(control, viewer_html.split('id="radars"', 1)[1], control)
        self.assertIn("Αττικής", viewer_html)

    def test_saving_or_toggling_a_radar_changes_no_pipeline_legacy_or_billing_state(self):
        legacy_user = entitled_user("legacy-radar-ui@example.com")
        radar_for(legacy_user, "Παλιό Radar", prefectures=["ΑΤΤΙΚΗΣ"])
        pipeline = self.pipeline_world()
        opportunity_rows = list(Opportunity.objects.values())
        self.post(self.owner, self.create_url(), self.good_form())
        radar = OrganizationRadar.objects.filter(organization=self.org).latest("pk")
        self.post(self.owner, self.edit_url(radar), self.good_form(name="Άλλο"))
        self.post(self.owner, self.active_url(radar), {"active": "0"})
        self.post(self.owner, self.active_url(self.row.radar), {"active": "0"})
        self.assertEqual(self.pipeline_world(), pipeline)   # no opportunity, signal, notification, audit, schedule
        self.assertEqual(list(Opportunity.objects.values()), opportunity_rows)

    def test_without_reference_data_only_signal_types_are_offered(self):
        for model in (GemiKad, GemiPrefecture, GemiMunicipality, GemiLegalType):
            model.objects.update(is_present=False)
        self.client.force_login(self.owner)
        html = self.client.get(self.create_url()).content.decode()
        self.assertIn("data-reference-unavailable", html)
        self.assertNotIn('name="kads"', html)
        self.assertIn('value="new_company"', html)
        response = self.client.post(self.create_url(), {"name": "Μόνο νέες", "active": "1",
                                                        "signal_types": ["new_company"]})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(OrganizationRadar.objects.get(organization=self.org, name="Μόνο νέες").active)


# --- criteria pickers -----------------------------------------------------------------------------------------

class PickerTests(RadarUiTestCase):
    """The criteria are searchable multi-selects over the canonical reference data. The posted
    representation is unchanged, so the stored definition and the matcher see exactly what they did."""

    def test_every_reference_criterion_is_a_searchable_picker(self):
        """The criteria are pickers over the canonical reference data, not raw code entry."""
        self.client.force_login(self.owner)
        html = self.client.get(self.create_url()).content.decode()

        for name in ("kads", "prefectures", "municipalities", "legal_forms", "excluded_kads",
                     "excluded_prefectures", "excluded_municipalities", "excluded_legal_forms"):
            self.assertIn(f'data-input-name="{name}"', html, name)
        self.assertEqual(html.count("data-reference-picker"), 8)
        self.assertEqual(sorted(set(re.findall(r'data-kind="(\w+)"', html))),
                         ["kad", "legal_form", "municipality", "prefecture"])
        self.assertIn(reverse("reference_search"), html)
        # The ~19.000 KAD rows are searched, never rendered: no KAD option or description reaches the page.
        self.assertNotIn(self.r.kad_other.source_id, html)

    def test_the_page_keeps_working_without_javascript(self):
        """The control that posts without JavaScript is still there, with the same names and values."""
        self.client.force_login(self.owner)
        html = self.client.get(self.create_url()).content.decode()
        self.assertIn('name="kads"', html)                       # the textarea the server already parses
        self.assertIn('data-picker-lines', html)
        self.assertIn('name="prefectures" data-picker-select multiple', html)
        self.assertIn("data-picker-fallback", html)

    def test_stored_selections_come_back_as_labelled_chips(self):
        r = self.r
        GemiKad.objects.filter(pk=r.kad_other.pk).update(description="ΛΙΑΝΙΚΟ ΕΜΠΟΡΙΟ")
        self.post(self.owner, self.create_url(), self.good_form(
            name="Με chips", kads=f"{r.kad_other.source_id} {r.kad_other.kad_version}"))
        radar = OrganizationRadar.objects.get(organization=self.org, name="Με chips")

        html = self.client.get(self.edit_url(radar)).content.decode()

        self.assertIn(f'data-value="{r.kad_other.source_id} {r.kad_other.kad_version}"', html)
        self.assertIn(f"{r.kad_other.source_id} — ΛΙΑΝΙΚΟ ΕΜΠΟΡΙΟ", html)     # code — description
        self.assertIn("ΚΑΔ 2026", html)                                        # the version stays visible
        for row, label in ((r.attica, "Αττικής"), (r.ike, "ΙΚΕ"), (r.kifisia, "Κηφισιάς")):
            self.assertIn(f'data-value="{row.pk}"', html, label)
            self.assertIn(label, html)
        # kad, prefecture, legal form, excluded municipality, excluded legal form
        self.assertEqual(html.count("data-picker-chip"), 5)

    def test_a_rejected_post_keeps_the_chips_the_customer_chose(self):
        r = self.r
        response = self.post(self.owner, self.create_url(), self.good_form(name="", kads="47191002"))
        html = response.content.decode()
        self.assertIn("data-form-errors", html)
        self.assertIn('data-value="47191002"', html)             # the KAD survives the error
        self.assertIn(f'data-value="{r.attica.pk}"', html)
        self.assertFalse(OrganizationRadar.objects.filter(organization=self.org, name="").exists())

    def test_a_picker_selection_round_trips_into_the_stored_definition(self):
        """What the picker posts is what the matcher stores: the representation is unchanged."""
        r = self.r
        posted = {"name": "Από picker", "active": "1", "kads": f"{r.kad_other.source_id} {r.kad_other.kad_version}",
                  "prefectures": [str(r.attica.pk)], "municipalities": [str(r.kifisia.pk)],
                  "legal_forms": [str(r.ike.pk)], "signal_types": ["new_company"],
                  "excluded_legal_forms": [str(r.oe.pk)]}
        self.assertEqual(self.post(self.owner, self.create_url(), posted).status_code, 302)

        radar = OrganizationRadar.objects.get(organization=self.org, name="Από picker")
        definition = get_organization_radar_definition(self.org, radar)
        self.assertEqual([(k.source_id, k.kad_version) for k in definition.kads],
                         [(r.kad_other.source_id, r.kad_other.kad_version)])
        self.assertEqual({row.pk for row in definition.regions}, {r.attica.pk, r.kifisia.pk})
        self.assertEqual([row.pk for row in definition.legal_forms], [r.ike.pk])
        self.assertEqual([row.pk for row in definition.exclusions], [r.oe.pk])

    def test_each_picker_is_a_dropdown_the_customer_can_open(self):
        """A chevron and a combobox: the field says "there is a list here", it is not a blank search box."""
        self.client.force_login(self.owner)
        html = self.client.get(self.create_url()).content.decode()

        self.assertEqual(html.count("data-picker-toggle"), 8)
        self.assertEqual(html.count("data-picker-field"), 8)
        self.assertEqual(html.count('aria-haspopup="listbox"'), 8)
        self.assertEqual(html.count('role="listbox"'), 8)
        # Opening a picker asks the endpoint; it still costs nothing in the page itself.
        self.assertEqual(html.count(reverse("reference_search")), 8)
        self.assertNotIn(self.r.kad_other.source_id, html)

    def test_a_chosen_value_is_offered_once_and_held_once(self):
        """One chip, one posted value: opening the dropdown again cannot duplicate a selection."""
        r = self.r
        self.post(self.owner, self.create_url(), self.good_form(
            name="Μία φορά", kads=f"{r.kad_other.source_id} {r.kad_other.kad_version}"))
        radar = OrganizationRadar.objects.get(organization=self.org, name="Μία φορά")

        html = self.client.get(self.edit_url(radar)).content.decode()

        self.assertEqual(html.count(f'data-value="{r.kad_other.source_id} {r.kad_other.kad_version}"'), 1)
        self.assertEqual(html.count(f'name="prefectures" value="{r.attica.pk}"'), 1)
        # Within the prefectures picker itself: one chip, so the chosen row is held exactly once.
        prefectures = html.split('data-input-name="prefectures"')[1].split("data-input-name=")[0]
        self.assertEqual(prefectures.count("data-picker-chip"), 1)
        self.assertEqual(prefectures.count(f'data-value="{r.attica.pk}"'), 1)
        self.assertEqual(OrganizationRadarKad.objects.filter(radar=radar).count(), 1)

class PickerScriptTests(SimpleTestCase):
    """The dropdown behaviour lives in static/js/app.js, which no Python test can execute. These assert the
    wiring exists, so the affordance the template promises cannot silently disappear."""

    SCRIPT = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js").read_text(encoding="utf-8")

    def test_the_dropdown_opens_on_focus_click_and_the_chevron(self):
        for binding in ("input.addEventListener('focus', openResults)",
                        "field?.addEventListener('click', openResults)",
                        "toggle?.addEventListener('click'"):
            self.assertIn(binding, self.SCRIPT, binding)

    def test_the_dropdown_closes_on_escape_and_on_an_outside_click(self):
        self.assertIn("event.key === 'Escape'", self.SCRIPT)
        self.assertIn("if (!root.contains(event.target)) closeResults();", self.SCRIPT)

    def test_the_keyboard_can_walk_the_list(self):
        for key in ("'ArrowDown'", "'ArrowUp'", "'Enter'"):
            self.assertIn(f"event.key === {key}", self.SCRIPT, key)

    def test_an_already_chosen_row_is_not_offered_again(self):
        self.assertIn("shown.filter(item => !taken.has(item.value))", self.SCRIPT)

    def test_choosing_an_option_is_not_mistaken_for_a_click_outside(self):
        """Found in the browser: picking an option replaces the list, so by the time a bubble-phase document
        listener runs the clicked node is detached and `contains` reads it as a click outside -- which closed
        the dropdown on every selection. The listener has to be on the capture phase."""
        self.assertIn("closeResults(); }, true);", self.SCRIPT)

    def test_an_empty_result_says_so_in_greek(self):
        self.assertIn("Δεν βρέθηκαν αποτελέσματα", self.SCRIPT)
