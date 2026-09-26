from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError
from django.db.models.deletion import CASCADE, SET_NULL
from django.test import TestCase
from django.utils import timezone

from .legacy_radar_migration import MAPPING_VERSION, migrate_legacy_radars
from .models import (
    ActivityCode, CompanySignal, CustomerRadar, GemiKad, GemiLegalType, GemiPrefecture,
    LegacyRadarMigrationMap, Opportunity, OrganizationMember, OrganizationRadar,
)
from .organizations import add_organization_member, create_organization


class LegacyRadarMigrationTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.user = User.objects.create_user("legacy@example.com", "legacy@example.com", "password")
        self.org = create_organization(owner=self.user, name="One").organization
        self.kad_2008 = self.ref(GemiKad, "62010000", kad_version="kad_2008", description="Programming")
        self.kad_2026 = self.ref(GemiKad, "62010000", kad_version="kad_2026", description="Programming")
        self.attica = self.ref(GemiPrefecture, "52", description="ΑΤΤΙΚΗΣ")
        self.ike = self.ref(GemiLegalType, "10", description="ΙΚΕ")

    def ref(self, model, source_id, **values):
        return model.objects.create(source_id=source_id, first_seen_at=self.now, last_seen_at=self.now, **values)

    def radar(self, user=None, **values):
        fields = dict(name="Legacy", only_active=False, monitor_from=self.now - timedelta(days=1))
        fields.update(values)
        return CustomerRadar.objects.create(user=user or self.user, **fields)

    def migrate(self, **options):
        return migrate_legacy_radars(limit=100, **options)

    def test_provenance_schema_has_one_source_and_a_nullable_tombstone_destination(self):
        legacy = LegacyRadarMigrationMap._meta.get_field("legacy_radar")
        organization = LegacyRadarMigrationMap._meta.get_field("organization")
        destination = LegacyRadarMigrationMap._meta.get_field("organization_radar")
        self.assertTrue(legacy.one_to_one)
        self.assertIs(legacy.remote_field.on_delete, CASCADE)
        self.assertIs(organization.remote_field.on_delete, CASCADE)
        self.assertTrue(destination.one_to_one)
        self.assertTrue(destination.null)
        self.assertIs(destination.remote_field.on_delete, SET_NULL)

    def test_one_owner_membership_is_the_destination(self):
        radar = self.radar()
        report = self.migrate()
        self.assertEqual(report.counters["migrated"], 1)
        self.assertEqual(LegacyRadarMigrationMap.objects.get(legacy_radar=radar).organization, self.org)

    def test_one_membership_only_organization_is_the_destination(self):
        member = User.objects.create_user("member@example.com", password="password")
        add_organization_member(self.org, user=member, role=OrganizationMember.VIEWER)
        radar = self.radar(user=member)
        self.migrate()
        self.assertEqual(LegacyRadarMigrationMap.objects.get(legacy_radar=radar).organization, self.org)

    def test_owner_membership_is_one_deduplicated_relationship(self):
        self.radar()
        self.assertEqual(OrganizationMember.objects.filter(user=self.user, organization=self.org).count(), 1)
        self.assertEqual(self.migrate().counters["migrated"], 1)

    def test_owner_of_one_and_member_of_another_is_ambiguous(self):
        other = create_organization(owner=User.objects.create_user("other@example.com", password="password"),
                                    name="Other").organization
        add_organization_member(other, user=self.user, role=OrganizationMember.VIEWER)
        self.radar()
        self.assertEqual(self.migrate().counters["ambiguous_organization"], 1)
        self.assertFalse(OrganizationRadar.objects.exists())

    def test_member_of_two_is_ambiguous(self):
        member = User.objects.create_user("member@example.com", password="password")
        add_organization_member(self.org, user=member, role=OrganizationMember.VIEWER)
        other = create_organization(owner=User.objects.create_user("other@example.com", password="password"),
                                    name="Other").organization
        add_organization_member(other, user=member, role=OrganizationMember.VIEWER)
        self.radar(user=member)
        self.assertEqual(self.migrate().counters["ambiguous_organization"], 1)

    def test_no_organization_is_missing(self):
        outsider = User.objects.create_user("none@example.com", password="password")
        self.radar(user=outsider)
        self.assertEqual(self.migrate().counters["missing_organization"], 1)

    def test_dry_run_classifies_but_writes_nothing(self):
        self.radar(prefectures=["ΑΤΤΙΚΗΣ"])
        report = self.migrate(dry_run=True)
        self.assertEqual((report.counters["eligible"], report.counters["prefectures"]), (1, 1))
        self.assertFalse(OrganizationRadar.objects.exists())
        self.assertFalse(LegacyRadarMigrationMap.objects.exists())
        self.assertEqual(self.migrate().counters["eligible"], report.counters["eligible"])

    def test_idempotency_duplicate_names_and_tombstone(self):
        second_user = User.objects.create_user("second@example.com", password="password")
        add_organization_member(self.org, user=second_user, role=OrganizationMember.VIEWER)
        first, second = self.radar(name="Same"), self.radar(user=second_user, name="Same")
        self.assertEqual(self.migrate().counters["migrated"], 2)
        self.assertEqual(self.migrate().counters["already_migrated"], 2)
        mapping = LegacyRadarMigrationMap.objects.get(legacy_radar=first)
        mapping.organization_radar.delete()
        mapping.refresh_from_db()
        self.assertIsNone(mapping.organization_radar_id)
        report = self.migrate()
        self.assertEqual(report.counters["already_migrated_deleted"], 1)
        self.assertEqual(OrganizationRadar.objects.count(), 1)

    def test_provenance_conflict_skips(self):
        radar = self.radar()
        migrated = OrganizationRadar.objects.create(organization=self.org, name="x")
        other = create_organization(owner=User.objects.create_user("other@example.com", password="password"),
                                    name="Other").organization
        LegacyRadarMigrationMap.objects.create(legacy_radar=radar, organization=other,
                                                organization_radar=migrated, mapping_version=MAPPING_VERSION)
        self.assertEqual(self.migrate().counters["provenance_conflict"], 1)

    def test_active_inactive_and_off_mapping(self):
        active = self.radar(name="active")
        inactive = self.radar(name="inactive", is_active=False)
        off = self.radar(name="off", frequency="off")
        self.migrate()
        migrated = {row.legacy_radar_id: row.organization_radar
                    for row in LegacyRadarMigrationMap.objects.select_related("organization_radar")}
        self.assertTrue(migrated[active.pk].active)
        self.assertFalse(migrated[inactive.pk].active)
        self.assertFalse(migrated[off.pk].active)

    def test_exact_filters_all_kad_versions_and_new_company_only(self):
        code = ActivityCode.objects.create(code="62.01.00.00", normalized_code="62010000",
                                           description="Programming", search_text="62010000 PROGRAMMING")
        radar = self.radar(prefectures=["αττικής"], legal_types=["ΙΚΕ"])
        radar.activity_codes.add(code)
        self.migrate()
        migrated = LegacyRadarMigrationMap.objects.get(legacy_radar=radar).organization_radar
        self.assertEqual(set(migrated.kads.values_list("kad_id", flat=True)), {self.kad_2008.pk, self.kad_2026.pk})
        self.assertEqual(list(migrated.regions.values_list("prefecture_id", flat=True)), [self.attica.pk])
        self.assertEqual(list(migrated.legal_forms.values_list("legal_type_id", flat=True)), [self.ike.pk])
        self.assertEqual(list(migrated.signal_types.values_list("signal_type", flat=True)), ["new_company"])
        self.assertFalse(migrated.regions.filter(level="municipality").exists())
        self.assertFalse(migrated.exclusions.exists())
        self.assertIsNone(migrated.score_threshold)

    def test_unsupported_fields_and_soft_delete_never_partially_migrate(self):
        self.radar(name="name", name_query="ACME")
        self.radar(name="active-only", only_active=True)
        self.radar(name="future", monitor_from=self.now + timedelta(days=1))
        self.radar(name="deleted", deleted_at=self.now)
        report = self.migrate()
        self.assertEqual(report.counters["unsupported_mapping"], 2)
        self.assertEqual(report.counters["only_active_currently_inert"], 1)
        self.assertEqual(report.counters["soft_deleted"], 1)
        self.assertEqual(OrganizationRadar.objects.count(), 1)

    def test_invalid_reference_data_never_creates_a_partial_radar(self):
        bad_kad = ActivityCode.objects.create(code="99", normalized_code="99", description="bad", search_text="99")
        cases = [
            self.radar(name="kad"), self.radar(name="pref", prefectures=["UNKNOWN"]),
            self.radar(name="legal", legal_types=["UNKNOWN"]),
        ]
        cases[0].activity_codes.add(bad_kad)
        report = self.migrate()
        self.assertEqual(report.counters["invalid_reference_data"], 3)
        self.assertFalse(OrganizationRadar.objects.exists())

    def test_failure_during_criteria_or_provenance_rolls_back_everything(self):
        self.radar()
        with patch("gemiapp.organization_radars._write_criteria", side_effect=RuntimeError("boom")):
            self.assertEqual(self.migrate().counters["errors"], 1)
        self.assertFalse(OrganizationRadar.objects.exists())
        self.assertFalse(LegacyRadarMigrationMap.objects.exists())
        with patch.object(LegacyRadarMigrationMap.objects, "create", side_effect=IntegrityError("boom")):
            self.assertEqual(self.migrate().counters["errors"], 1)
        self.assertFalse(OrganizationRadar.objects.exists())

    def test_legacy_and_unrelated_2_0_data_are_untouched(self):
        radar = self.radar()
        before = tuple(CustomerRadar.objects.filter(pk=radar.pk).values().get().items())
        subscription_before = tuple(type(self.user.subscription).objects.filter(pk=self.user.subscription.pk)
                                    .values().get().items())
        memberships_before = OrganizationMember.objects.count()
        manual = OrganizationRadar.objects.create(organization=self.org, name="manual")
        signals, opportunities = CompanySignal.objects.count(), Opportunity.objects.count()
        self.migrate()
        self.assertEqual(tuple(CustomerRadar.objects.filter(pk=radar.pk).values().get().items()), before)
        self.assertTrue(OrganizationRadar.objects.filter(pk=manual.pk, name="manual").exists())
        self.assertEqual((CompanySignal.objects.count(), Opportunity.objects.count()), (signals, opportunities))
        subscription_after = tuple(type(self.user.subscription).objects.filter(pk=self.user.subscription.pk)
                                   .values().get().items())
        self.assertEqual(subscription_after, subscription_before)
        self.assertEqual(OrganizationMember.objects.count(), memberships_before)

    def test_targeting_options_and_command_output(self):
        selected = self.radar(name="selected")
        other_user = User.objects.create_user("other@example.com", password="password")
        other_org = create_organization(owner=other_user, name="Other").organization
        other = self.radar(user=other_user, name="other")
        self.assertEqual(self.migrate(dry_run=True, legacy_radar_id=selected.pk).counters["legacy_radars_examined"], 1)
        self.assertEqual(self.migrate(dry_run=True, user_id=other_user.pk).counters["legacy_radars_examined"], 1)
        self.assertEqual(self.migrate(dry_run=True, organization_id=other_org.pk).counters["legacy_radars_examined"], 1)
        out = StringIO()
        call_command("migrate_legacy_radars_to_organizations", "--dry-run", "--legacy-radar-id", str(other.pk),
                     stdout=out)
        self.assertIn("legacy_radars_examined=1", out.getvalue())
        self.assertIn("[dry-run] zero database writes", out.getvalue())
