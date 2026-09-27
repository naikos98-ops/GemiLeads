"""G4 transition: the legacy CustomerRadar is the customer's canonical editor and every mutation of it is mirrored
into its OrganizationRadar (the object the SHADOW pipeline evaluates), through the provenance mapping only."""

import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import legacy_radar_sync as sync
from .admin import CustomerRadarAdmin
from .company_signals import LIVE, SHADOW
from .legacy_radar_migration import migrate_legacy_radars
from .models import (
    ActivityCode, CustomerRadar, GemiKad, GemiPrefecture, LegacyRadarMigrationMap, Opportunity, OrganizationMember,
    OrganizationRadar, UserSubscription,
)
from .opportunity_pipeline import process_company_signal
from .organization_radar_matching import find_matching_organization_radars
from .organizations import add_organization_member, create_organization
from .services import company_matches_radar
from .test_organization_radar_matching import T0, make_company, new_company_signal, snapshot
from .test_organization_radars import entitle

TRANSITION = override_settings(GEMI_TRANSITION_LEGACY_ACCESS=True)
AFTER_CUTOVER = override_settings(GEMI_TRANSITION_LEGACY_ACCESS=False)


@TRANSITION
class SyncTestCase(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.user = entitle(User.objects.create_user("sync@example.com", "sync@example.com", "x"))
        self.org = create_organization(owner=self.user, name="Sync Org").organization
        self.kad_2008 = self.ref(GemiKad, "62010000", kad_version="kad_2008", description="Programming")
        self.kad_2026 = self.ref(GemiKad, "62010000", kad_version="kad_2026", description="Programming")
        self.retail = self.ref(GemiKad, "47110000", kad_version="kad_2026", description="Retail")
        self.attica = self.ref(GemiPrefecture, "5", description="ΑΤΤΙΚΗΣ")
        self.chios = self.ref(GemiPrefecture, "88", description="ΧΙΟΥ")
        for code, normalized in (("62.01.00.00", "62010000"), ("47.11.00.00", "47110000")):  # may be seeded
            ActivityCode.objects.get_or_create(normalized_code=normalized, defaults={
                "code": code, "description": normalized, "search_text": normalized})
        self.company = make_company("400100")                        # prefecture ΑΤΤΙΚΗΣ (a form choice)
        chios_company = make_company("400200")                          # so ΧΙΟΥ is a form choice too
        chios_company.prefecture = "ΧΙΟΥ"
        chios_company.save()
        self.client.force_login(self.user)

    def ref(self, model, source_id, **values):
        return model.objects.create(source_id=source_id, first_seen_at=self.now, last_seen_at=self.now, **values)

    def form(self, **overrides):
        data = {"name": "Προγραμματιστές Αττικής", "name_query": "", "prefectures": ["ΑΤΤΙΚΗΣ"],
                "frequency": "daily", "is_active": "on", "activity_codes": ["62010000"]}
        data.update(overrides)
        return {key: value for key, value in data.items() if value is not None}

    def create_via_ui(self, **overrides):
        self.client.post(reverse("radar_create"), self.form(**overrides))
        return CustomerRadar.objects.latest("pk")                      # the logged-in user's new Radar

    def edit_via_ui(self, radar, **overrides):
        return self.client.post(reverse("radar_edit", args=[radar.pk]), self.form(**overrides))

    def mirror_of(self, radar):
        return LegacyRadarMigrationMap.objects.get(legacy_radar=radar).organization_radar

    def criteria(self, organization_radar):
        organization_radar.refresh_from_db()
        return {
            "name": organization_radar.name, "active": organization_radar.active,
            "kads": set(organization_radar.kads.values_list("kad_id", flat=True)),
            "regions": set(organization_radar.regions.values_list("prefecture_id", flat=True)),
            "signal_types": list(organization_radar.signal_types.values_list("signal_type", flat=True)),
        }

    def counts(self):
        return OrganizationRadar.objects.count(), LegacyRadarMigrationMap.objects.count()


class MirrorTests(SyncTestCase):
    def test_f_a_new_radar_gets_exactly_one_organization_radar_and_one_mapping(self):
        radar = self.create_via_ui()
        self.assertEqual(self.counts(), (1, 1))
        mirror = self.mirror_of(radar)
        self.assertEqual(mirror.organization_id, self.org.pk)
        self.assertEqual(self.criteria(mirror), {"name": "Προγραμματιστές Αττικής", "active": True,
                                                 "kads": {self.kad_2008.pk, self.kad_2026.pk},
                                                 "regions": {self.attica.pk}, "signal_types": ["new_company"]})

    def test_c_an_edit_replaces_the_criteria_exactly_in_place(self):
        radar = self.create_via_ui()
        mirror_pk = self.mirror_of(radar).pk
        self.edit_via_ui(radar, name="Λιανική Χίου", prefectures=["ΧΙΟΥ"], activity_codes=["47110000"])
        self.assertEqual(self.counts(), (1, 1))
        mirror = self.mirror_of(radar)
        self.assertEqual(mirror.pk, mirror_pk)                               # the same object, never a new one
        self.assertEqual(self.criteria(mirror), {"name": "Λιανική Χίου", "active": True,
                                                 "kads": {self.retail.pk}, "regions": {self.chios.pk},
                                                 "signal_types": ["new_company"]})

    def test_d_pause_and_resume_mirror_and_frequency_off_is_inactive(self):
        radar = self.create_via_ui()
        mirror = self.mirror_of(radar)
        self.client.post(reverse("radar_toggle", args=[radar.pk]))
        self.assertFalse(self.criteria(mirror)["active"])
        self.client.post(reverse("radar_toggle", args=[radar.pk]))
        self.assertTrue(self.criteria(mirror)["active"])
        self.edit_via_ui(radar, frequency="off")                             # muted from legacy matching too
        self.assertFalse(self.criteria(mirror)["active"])

    def test_e_a_soft_delete_deactivates_and_keeps_the_provenance(self):
        radar = self.create_via_ui()
        mirror = self.mirror_of(radar)
        self.client.post(reverse("radar_delete", args=[radar.pk]))
        radar.refresh_from_db()
        self.assertIsNotNone(radar.deleted_at)
        self.assertFalse(self.criteria(mirror)["active"])
        self.assertEqual(self.mirror_of(radar).pk, mirror.pk)                # mapping and row kept
        self.assertEqual(self.counts(), (1, 1))

    def test_g_retrying_the_synchronization_never_duplicates(self):
        radar = self.create_via_ui()
        before = self.criteria(self.mirror_of(radar))
        for _ in range(3):
            self.assertEqual(sync.mirror_legacy_radar(radar), sync.UPDATED)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.criteria(self.mirror_of(radar)), before)
        legacy_only = CustomerRadar.objects.create(user=self.user, name="Χειροκίνητο", only_active=False,
                                                   monitor_from=self.now - timedelta(days=1))
        self.assertEqual(sync.mirror_legacy_radar(legacy_only), sync.CREATED)
        self.assertEqual(sync.mirror_legacy_radar(legacy_only), sync.UPDATED)
        self.assertEqual(self.counts(), (2, 2))

    def test_a_deleted_copy_is_a_tombstone_that_is_never_recreated(self):
        radar = self.create_via_ui()
        self.mirror_of(radar).delete()
        self.edit_via_ui(radar, name="Μετά")
        self.assertEqual(self.counts(), (0, 1))
        self.assertEqual(sync.mirror_legacy_radar(radar), sync.TOMBSTONE)

    def test_the_admin_mirrors_staff_edits_and_refuses_hard_deletes(self):
        radar = self.create_via_ui()
        CustomerRadar.objects.filter(pk=radar.pk).update(is_active=False)
        model_admin = CustomerRadarAdmin(CustomerRadar, admin.site)

        class SavedForm:
            instance = CustomerRadar.objects.get(pk=radar.pk)

            def save_m2m(self):
                pass

        model_admin.save_related(None, SavedForm(), [], True)
        self.assertFalse(self.criteria(self.mirror_of(radar))["active"])
        self.assertFalse(model_admin.has_delete_permission(None, radar))      # would cascade the provenance away


class FailureContractTests(SyncTestCase):
    def test_an_unrepresentable_radar_keeps_the_legacy_change_and_quarantines_its_copy(self):
        radar = self.create_via_ui()
        with self.assertLogs("gemiapp.legacy_radar_sync", level="ERROR"):
            self.edit_via_ui(radar, name_query="ACME")                       # no OrganizationRadar equivalent
        radar.refresh_from_db()
        self.assertEqual(radar.name_query, "ACME")                          # the customer's change is saved
        self.assertFalse(self.criteria(self.mirror_of(radar))["active"])     # SHADOW never runs stale criteria

    def test_an_unrepresentable_new_radar_is_saved_and_not_copied(self):
        with self.assertLogs("gemiapp.legacy_radar_sync", level="ERROR"):
            radar = self.create_via_ui(name_query="ACME")
        self.assertEqual((radar.name_query, self.counts()), ("ACME", (0, 0)))
        self.assertEqual(migrate_legacy_radars(dry_run=True).counters["unsupported_mapping"], 1)  # still reported

    def test_an_unexpected_failure_rolls_the_legacy_change_back(self):
        radar = self.create_via_ui()
        before = self.criteria(self.mirror_of(radar))
        with patch.object(sync, "replace_organization_radar", side_effect=RuntimeError("boom")), \
                self.assertLogs("gemiapp.legacy_radar_sync", level="ERROR"):
            response = self.edit_via_ui(radar, name="Δεν αποθηκεύεται")
        self.assertRedirects(response, reverse("radar_list"), fetch_redirect_response=False)
        radar.refresh_from_db()
        self.assertEqual(radar.name, "Προγραμματιστές Αττικής")
        self.assertEqual(self.criteria(self.mirror_of(radar)), before)
        with patch.object(sync, "replace_organization_radar", side_effect=RuntimeError("boom")), \
                self.assertLogs("gemiapp.legacy_radar_sync", level="ERROR"):
            self.client.post(reverse("radar_toggle", args=[radar.pk]))      # a pause is mirrored as a replace
        radar.refresh_from_db()
        self.assertTrue(radar.is_active)                                     # the pause did not happen either

    def test_after_the_cutover_nothing_is_mirrored(self):
        radar = self.create_via_ui()
        before = self.criteria(self.mirror_of(radar))
        with AFTER_CUTOVER:
            self.edit_via_ui(radar, name="Μετά το LIVE", activity_codes=["47110000"])
            self.assertEqual(sync.mirror_legacy_radar(radar), sync.DISABLED)
        radar.refresh_from_db()
        self.assertEqual(radar.name, "Μετά το LIVE")                        # the legacy URL still works (rollback)
        self.assertEqual(self.criteria(self.mirror_of(radar)), before)       # and never overwrites the new editor


class TenantTests(SyncTestCase):
    def test_h_each_owner_mirrors_into_their_own_organization_only(self):
        other = entitle(User.objects.create_user("other@example.com", "other@example.com", "x"))
        other_org = create_organization(owner=other, name="Other").organization
        mine = self.create_via_ui()
        self.client.force_login(other)
        theirs = self.create_via_ui(name="Άλλο Radar")
        self.assertEqual(self.mirror_of(mine).organization_id, self.org.pk)
        self.assertEqual(self.mirror_of(theirs).organization_id, other_org.pk)
        self.assertEqual(OrganizationRadar.objects.filter(organization=other_org).count(), 1)

    def test_a_mapping_to_another_tenant_is_never_written(self):
        radar = self.create_via_ui()
        other_org = create_organization(owner=entitle(User.objects.create_user("o2@example.com", "o2@example.com",
                                                                                 "x")), name="O2").organization
        LegacyRadarMigrationMap.objects.filter(legacy_radar=radar).update(organization=other_org)
        before = self.criteria(self.mirror_of(radar))
        with self.assertLogs("gemiapp.legacy_radar_sync", level="ERROR"):
            self.edit_via_ui(radar, name="Αλλαγή")
        self.assertEqual(self.criteria(self.mirror_of(radar)), before)
        self.assertEqual(OrganizationRadar.objects.filter(organization=other_org).count(), 0)

    def test_an_owner_who_moved_to_another_organization_changes_neither_tenant(self):
        radar = self.create_via_ui()
        before = self.criteria(self.mirror_of(radar))
        OrganizationMember.objects.filter(organization=self.org, user=self.user).delete()
        new_org = create_organization(owner=self.user, name="Νέος οργανισμός").organization
        with self.assertLogs("gemiapp.legacy_radar_sync", level="ERROR"):
            self.edit_via_ui(radar, name="Μετακόμιση")
        radar.refresh_from_db()
        self.assertEqual(radar.name, "Μετακόμιση")                          # the customer's own change is saved
        self.assertEqual(self.criteria(self.mirror_of(radar)), before)       # the old tenant's copy is untouched
        self.assertFalse(OrganizationRadar.objects.filter(organization=new_org).exists())

    def test_owners_without_a_single_organization_keep_the_legacy_editor_only(self):
        loner = entitle(User.objects.create_user("loner@example.com", "loner@example.com", "x"))
        self.client.force_login(loner)
        radar = self.create_via_ui()
        self.assertEqual(sync.mirror_legacy_radar(radar), sync.NOT_MIRRORED)
        add_organization_member(self.org, loner, "viewer")
        add_organization_member(create_organization(owner=entitle(User.objects.create_user(
            "x3@example.com", "x3@example.com", "x")), name="Third").organization, loner, "viewer")
        self.assertEqual(sync.mirror_legacy_radar(radar), sync.NOT_MIRRORED)  # ambiguous: never guessed
        self.assertEqual(LegacyRadarMigrationMap.objects.filter(legacy_radar=radar).count(), 0)


class DownstreamTests(SyncTestCase):
    def test_i_the_shadow_pipeline_sees_the_mirrored_state(self):
        snapshot(self.company, T0)                                           # 62010000 kad_2026, prefecture 5
        signal = new_company_signal(self.company, T0 + timedelta(hours=1), mode=SHADOW)
        radar = self.create_via_ui()
        mirror = self.mirror_of(radar)
        self.assertEqual([m.radar_id for m in find_matching_organization_radars(signal)], [mirror.pk])
        self.client.post(reverse("radar_toggle", args=[radar.pk]))            # paused by the customer
        self.assertEqual(find_matching_organization_radars(signal), ())
        self.client.post(reverse("radar_toggle", args=[radar.pk]))
        self.edit_via_ui(radar, activity_codes=["47110000"])                  # criteria no longer fit
        self.assertEqual(find_matching_organization_radars(signal), ())

    def test_j_legacy_leads_keep_following_the_customers_radar(self):
        radar = self.create_via_ui(activity_codes=[])
        self.assertTrue(company_matches_radar(self.company, CustomerRadar.objects.get(pk=radar.pk))[0])
        self.edit_via_ui(radar, prefectures=["ΧΙΟΥ"], activity_codes=[])
        self.assertFalse(company_matches_radar(self.company, CustomerRadar.objects.get(pk=radar.pk))[0])

    def test_l_shadow_opportunities_stay_invisible(self):
        snapshot(self.company, T0)
        signal = new_company_signal(self.company, T0 + timedelta(hours=1), mode=SHADOW)
        self.create_via_ui()
        process_company_signal(signal)
        self.assertTrue(Opportunity.objects.filter(organization=self.org, latest_signal__mode=SHADOW).exists())
        self.assertFalse(Opportunity.objects.filter(latest_signal__mode=LIVE).exists())
        for url in (reverse("organization_opportunities", args=[self.org.pk]),
                    reverse("organization_dashboard", args=[self.org.pk])):
            self.assertNotIn(f"/company/{self.company.pk}/", self.client.get(url).content.decode(), url)

    def test_m_the_migration_command_sees_mirrored_radars_as_migrated(self):
        mirrored = self.create_via_ui()
        with AFTER_CUTOVER:
            unmirrored = self.create_via_ui(name="Πριν τη μετάβαση")          # e.g. created before this release
        self.assertFalse(LegacyRadarMigrationMap.objects.filter(legacy_radar=unmirrored).exists())
        counters = migrate_legacy_radars(limit=100).counters
        self.assertEqual((counters["already_migrated"], counters["migrated"]), (1, 1))
        self.assertEqual(self.mirror_of(mirrored).organization_id, self.mirror_of(unmirrored).organization_id)


class FreeAndNavigationTests(SyncTestCase):
    def test_k_free_radar_limits_are_unchanged(self):
        free = User.objects.create_user("free-sync@example.com", "free-sync@example.com", "x")
        create_organization(owner=free, name="Free Sync")
        self.assertFalse(UserSubscription.objects.get(user=free).has_entitlement)
        self.client.force_login(free)
        self.client.post(reverse("radar_create"), self.form())
        self.assertFalse(CustomerRadar.objects.filter(user=free).exists())
        self.assertEqual(OrganizationRadar.objects.count(), 0)

    def nav_radars(self, url):
        html = self.client.get(url).content.decode()
        rail = html.split('<nav class="product-rail', 1)[1].split("</nav>", 1)[0]
        mobile = html.split('<nav class="product-mobile-nav', 1)[1].split("</nav>", 1)[0]
        return (re.findall(r'href="([^"]+)" data-nav="radars"', rail),
                re.findall(r'href="([^"]+)" data-nav="radars"', mobile))

    def test_a_during_the_transition_the_one_radars_entry_is_the_legacy_editor(self):
        self.assertEqual(self.nav_radars(reverse("organization_dashboard", args=[self.org.pk])),
                         (["/radars/"], ["/radars/"]))
        html = self.client.get(reverse("organization_dashboard", args=[self.org.pk])).content.decode()
        self.assertNotIn(f"/organizations/{self.org.pk}/radars/", html)

    def test_b_after_the_cutover_the_same_entry_is_the_organizations_radars(self):
        with AFTER_CUTOVER:
            organization_radars = reverse("organization_radars", args=[self.org.pk])
            self.assertEqual(self.nav_radars(reverse("organization_dashboard", args=[self.org.pk])),
                             ([organization_radars], [organization_radars]))
            self.assertEqual(self.client.get(reverse("radar_list")).status_code, 200)  # rollback URL still works

    def test_the_organization_radar_editor_refuses_edits_during_the_transition(self):
        radar = self.create_via_ui()
        mirror = self.mirror_of(radar)
        before = self.criteria(mirror)
        for url in (reverse("organization_radar_create", args=[self.org.pk]),
                    reverse("organization_radar_edit", args=[self.org.pk, mirror.pk])):
            self.assertRedirects(self.client.get(url), reverse("radar_list"), fetch_redirect_response=False)
            self.assertRedirects(self.client.post(url, {"name": "x"}), reverse("radar_list"),
                                 fetch_redirect_response=False)
        self.assertRedirects(self.client.post(reverse("organization_radar_active", args=[self.org.pk, mirror.pk]),
                                              {"active": "0"}), reverse("radar_list"), fetch_redirect_response=False)
        self.assertEqual(self.criteria(mirror), before)
        self.assertEqual(self.counts(), (1, 1))
        page = self.client.get(reverse("organization_radars", args=[self.org.pk])).content.decode()
        self.assertIn("data-transition-radars", page)
        self.assertNotIn("data-new-radar", page)
        self.assertNotIn("data-edit-radar", page)
        # a non-member still gets the single 404, not a hint about the transition
        stranger = User.objects.create_user("stranger@example.com", "stranger@example.com", "x")
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(reverse("organization_radar_create", args=[self.org.pk])).status_code, 404)
