"""G4: closing the last Radar consistency gaps -- existing drift (``sync_legacy_radars_to_organizations``) and legacy
Radars of users whose organization is provisioned later (the provisioning paths copy them once)."""

from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import legacy_radar_sync as sync
from .company_signals import SHADOW
from .existing_user_provisioning import provision_existing_user_organizations
from .models import (
    CustomerRadar, LegacyRadarMigrationMap, Opportunity, OrganizationMember, OrganizationRadar,
)
from .opportunity_pipeline import process_company_signal
from .organizations import add_organization_member, create_organization
from .test_legacy_radar_sync import AFTER_CUTOVER, SyncTestCase
from .test_organization_radar_matching import T0, new_company_signal, snapshot
from .test_organization_radars import entitle

WRITES = ("INSERT", "UPDATE", "DELETE")


def writes_in(queries):
    return [q["sql"] for q in queries.captured_queries if q["sql"].lstrip().upper().startswith(WRITES)]


class ResyncTests(SyncTestCase):
    def stale(self, **legacy_changes):
        """A mapped Radar changed without the mirror (as before the mirror was deployed)."""
        radar = self.create_via_ui()
        CustomerRadar.objects.filter(pk=radar.pk).update(**legacy_changes)
        return CustomerRadar.objects.get(pk=radar.pk)

    def resync(self, **options):
        return sync.resync_legacy_radars(**options)

    def test_a_b_the_dry_run_detects_drift_and_writes_nothing(self):
        radar = self.stale(name="Άλλαξε πριν τη γέφυρα", is_active=False)
        with CaptureQueriesContext(connection) as queries:
            counters = self.resync()
        self.assertEqual(writes_in(queries), [])
        self.assertEqual((counters["examined"], counters["mapped"], counters["would_update"]), (1, 1, 1))
        self.assertEqual(self.criteria(self.mirror_of(radar))["name"], "Προγραμματιστές Αττικής")  # untouched

    def test_c_d_apply_updates_exactly_and_a_second_apply_writes_nothing(self):
        radar = self.stale(name="Χίος", prefectures=["ΧΙΟΥ"])
        radar.activity_codes.set([])
        counters = self.resync(apply=True)
        self.assertEqual((counters["would_update"], counters["applied_updated"]), (1, 1))
        self.assertEqual(self.criteria(self.mirror_of(radar)), {"name": "Χίος", "active": True, "kads": set(),
                                                                "regions": {self.chios.pk},
                                                                "signal_types": ["new_company"]})
        with CaptureQueriesContext(connection) as queries:
            again = self.resync(apply=True)
        self.assertEqual(writes_in(queries), [])
        self.assertEqual((again["unchanged"], again["would_update"], again["applied_updated"]), (1, 0, 0))
        self.assertEqual(self.counts(), (1, 1))

    def test_e_an_unmapped_radar_of_a_single_organization_owner_is_created_once(self):
        with AFTER_CUTOVER:
            radar = self.create_via_ui(name="Πριν τη γέφυρα")                # nothing mirrored then
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.resync()["would_create"], 1)
        self.assertEqual(self.resync(apply=True)["applied_created"], 1)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.mirror_of(radar).organization_id, self.org.pk)
        self.assertEqual(self.resync(apply=True)["unchanged"], 1)
        self.assertEqual(self.counts(), (1, 1))

    def test_f_owners_without_one_organization_are_skipped(self):
        loner = entitle(User.objects.create_user("loner-r@example.com", "loner-r@example.com", "x"))
        CustomerRadar.objects.create(user=loner, name="Χωρίς οργανισμό", only_active=False,
                                     monitor_from=self.now - timedelta(days=1))
        many = entitle(User.objects.create_user("many-r@example.com", "many-r@example.com", "x"))
        add_organization_member(self.org, many, "viewer")
        other = create_organization(owner=entitle(User.objects.create_user("o-r@example.com", "o-r@example.com",
                                                                            "x")), name="Άλλος").organization
        add_organization_member(other, many, "viewer")
        CustomerRadar.objects.create(user=many, name="Πολλοί", only_active=False,
                                     monitor_from=self.now - timedelta(days=1))
        counters = self.resync(apply=True)
        self.assertEqual((counters["skipped_no_org"], counters["skipped_ambiguous_org"]), (1, 1))
        self.assertEqual(self.counts(), (0, 0))

    def test_g_a_tombstone_is_never_resurrected(self):
        radar = self.stale(name="Άλλαξε")
        self.mirror_of(radar).delete()
        counters = self.resync(apply=True)
        self.assertEqual((counters["tombstones"], counters["applied_created"]), (1, 0))
        self.assertEqual(self.counts(), (0, 1))

    def test_h_an_unsupported_radar_never_leaves_an_active_stale_copy(self):
        mapped = self.stale(name_query="ACME")
        self.assertEqual(self.resync()["would_deactivate"], 1)
        counters = self.resync(apply=True)
        self.assertEqual(counters["applied_deactivated"], 1)
        self.assertFalse(self.criteria(self.mirror_of(mapped))["active"])
        self.assertEqual((self.resync()["unsupported"], self.resync()["would_deactivate"]), (1, 0))
        with AFTER_CUTOVER:
            unmapped = self.create_via_ui(name="Με όνομα", name_query="ACME")
        self.assertEqual(self.resync(apply=True, user_id=self.user.pk)["unsupported"], 2)
        self.assertFalse(LegacyRadarMigrationMap.objects.filter(legacy_radar=unmapped).exists())

    def test_a_soft_deleted_radar_with_an_active_copy_is_deactivated(self):
        radar = self.stale(deleted_at=timezone.now(), is_active=False)
        self.assertEqual(self.resync()["would_deactivate"], 1)
        self.assertEqual(self.resync(apply=True)["applied_deactivated"], 1)
        self.assertFalse(self.criteria(self.mirror_of(radar))["active"])
        self.assertEqual(self.mirror_of(radar).pk, LegacyRadarMigrationMap.objects.get(legacy_radar=radar)
                         .organization_radar_id)                         # provenance kept

    def test_i_a_cross_tenant_mapping_is_refused(self):
        radar = self.stale(name="Άλλαξε")
        other = create_organization(owner=entitle(User.objects.create_user("x-t@example.com", "x-t@example.com",
                                                                            "x")), name="Ξένος").organization
        LegacyRadarMigrationMap.objects.filter(legacy_radar=radar).update(organization=other)
        before = self.criteria(self.mirror_of(radar))
        with CaptureQueriesContext(connection) as queries:
            counters = self.resync(apply=True)
        self.assertEqual(writes_in(queries), [])
        self.assertEqual((counters["unsafe"], counters["applied_updated"]), (1, 0))
        self.assertEqual(self.criteria(self.mirror_of(radar)), before)

    def test_the_command_reports_every_counter_and_refuses_after_the_cutover(self):
        self.stale(name="Άλλαξε")
        out = StringIO()
        call_command("sync_legacy_radars_to_organizations", stdout=out)
        text = out.getvalue()
        for name in ("examined", "mapped", "unmapped", "unchanged", "would_create", "would_update",
                     "would_deactivate", "tombstones", "skipped_no_org", "skipped_ambiguous_org", "unsupported",
                     "errors"):
            self.assertRegex(text, rf"(?m)^{name}=\d+$")
        self.assertIn("mode=dry-run", text)
        self.assertIn("would_update=1", text)
        self.assertNotIn("sync@example.com", text)
        with AFTER_CUTOVER, self.assertRaises(CommandError):
            call_command("sync_legacy_radars_to_organizations", "--apply", stdout=StringIO())

    def test_n_resynced_radars_never_make_shadow_opportunities_visible(self):
        self.stale(name="Άλλαξε")
        self.resync(apply=True)
        snapshot(self.company, T0)
        process_company_signal(new_company_signal(self.company, T0 + timedelta(hours=1), mode=SHADOW))
        self.assertTrue(Opportunity.objects.filter(organization=self.org, latest_signal__mode=SHADOW).exists())
        page = self.client.get(reverse("organization_opportunities", args=[self.org.pk])).content.decode()
        self.assertNotIn(f"/company/{self.company.pk}/", page)


class LateProvisioningTests(SyncTestCase):
    def starter_owner(self, email, **radar_fields):
        user = entitle(User.objects.create_user(email, email, "x"))
        fields = dict(name="Όλες οι νέες επιχειρήσεις", only_active=False, monitor_from=self.now - timedelta(days=1))
        fields.update(radar_fields)
        return user, CustomerRadar.objects.create(user=user, **fields)

    def test_j_k_provisioning_copies_the_starter_radar_exactly_once(self):
        user, starter = self.starter_owner("starter@example.com")
        report = provision_existing_user_organizations(limit=100, user_id=user.pk)
        self.assertEqual((report.counters["provisioned"], report.counters["legacy_radars_copied"]), (1, 1))
        organization = OrganizationMember.objects.get(user=user).organization
        self.assertEqual(self.mirror_of(starter).organization_id, organization.pk)
        again = provision_existing_user_organizations(limit=100, user_id=user.pk)
        self.assertEqual((again.counters["already_provisioned"], again.counters["legacy_radars_copied"]), (1, 0))
        self.assertEqual(sync.copy_unmapped_legacy_radars(user, organization), {"copied": 0, "unsupported": 0})
        self.assertEqual(OrganizationRadar.objects.filter(organization=organization).count(), 1)
        self.assertEqual(LegacyRadarMigrationMap.objects.filter(legacy_radar__user=user).count(), 1)

    def test_l_an_unsupported_or_deleted_starter_radar_is_not_copied(self):
        user, starter = self.starter_owner("unsupported-starter@example.com", name_query="ACME")
        deleted = CustomerRadar.objects.create(user=user, name="Διαγραμμένο", only_active=False,
                                               monitor_from=self.now - timedelta(days=1), deleted_at=self.now)
        report = provision_existing_user_organizations(limit=100, user_id=user.pk)
        self.assertEqual((report.counters["provisioned"], report.counters["legacy_radars_copied"],
                          report.counters["legacy_radars_not_copied_unsupported"]), (1, 0, 1))
        self.assertFalse(LegacyRadarMigrationMap.objects.filter(legacy_radar__in=[starter, deleted]).exists())
        self.assertEqual(OrganizationRadar.objects.filter(organization__members__user=user).count(), 0)

    def test_the_single_user_command_copies_the_starter_radar_once(self):
        user, starter = self.starter_owner("single-starter@example.com")
        call_command("provision_organization_for_user", str(user.pk), stdout=StringIO())
        call_command("provision_organization_for_user", str(user.pk), stdout=StringIO())   # idempotent
        self.assertEqual(LegacyRadarMigrationMap.objects.filter(legacy_radar__user=user).count(), 1)
        self.assertEqual(self.mirror_of(starter).organization_id,
                         OrganizationMember.objects.get(user=user).organization_id)

    def test_users_who_stay_without_an_organization_get_nothing(self):
        user, starter = self.starter_owner("stays-alone@example.com", is_active=True)
        User.objects.filter(pk=user.pk).update(is_active=False)                        # skipped by provisioning
        provision_existing_user_organizations(limit=100, user_id=user.pk)
        self.assertFalse(LegacyRadarMigrationMap.objects.filter(legacy_radar=starter).exists())
        self.assertEqual(OrganizationRadar.objects.count(), 0)
        self.assertEqual(sync.plan_legacy_radar(starter), sync.SKIPPED_NO_ORG)
