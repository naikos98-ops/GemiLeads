"""Tests for KAD catalogue reconciliation (A8, gemiapp.ingestion.kad_catalogue).

Local fixtures only -- no GEMI call.
"""

import copy
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import CreateModel
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .ingestion.kad_catalogue import (
    BOTH_VERSIONS,
    KAD_2008_ONLY,
    KAD_2026,
    NO_REFERENCE_DATA,
    NOT_IN_REFERENCE,
    OFFICIAL_KAD_CROSSWALK,
    RETIRED_IN_REFERENCE,
    UNRESOLVED,
    activity_codes_in_version,
    gemi_kad_for_activity,
    kad_picker_queryset,
    radar_criteria_compatibility,
    reconcile_kad_catalogue,
)
from .kad import display_kad_code
from .models import (
    ActivityCode, ActivityCodeKadLink, Company, CompanyActivity, CustomerRadar, DigestDelivery, GemiKad, ImportRun,
    RadarMatch, UserCompanyLead, UserSubscription,
)
from .services import (
    company_matches_radar, filter_companies_for_radar, import_for_date, match_imported_companies, send_digests,
    sync_company_activities,
)
from .test_gemi_company_activities import AS_OF, MIXED, TARGET, entitled_user, entry, radar_for
from .test_gemi_reference_data import reference_payloads, run_sync
from .test_gemi_validation import full_item

AADE_SOURCE = "https://www.aade.gr/sites/default/files/2026-03/Economic-activity-codes%202025-KAD%202025.pdf"


def catalogue_entry(code, description="ΠΕΡΙΓΡΑΦΗ", source=AADE_SOURCE):
    return ActivityCode.objects.create(
        code=display_kad_code(code), normalized_code=code, description=description, source=source, search_text=code,
    )


def reference_kad(code, version, description="ΠΕΡΙΓΡΑΦΗ", present=True):
    now = timezone.now()
    return GemiKad.objects.create(
        source_id=code, kad_version=version, description=description, is_present=present,
        first_seen_at=now, last_seen_at=now, retired_at=None if present else now,
    )


def link_state():
    return sorted(ActivityCodeKadLink.objects.values_list(
        "pk", "activity_code__normalized_code", "gemi_kad__kad_version", "description_matches", "updated_at",
    ))


def classes():
    return {code: compatibility.classification for _, _, code, compatibility in radar_criteria_compatibility()}


class CatalogueFixture:
    """A small catalogue: current, historical fallback, both versions, unmatched, description difference."""

    def build(self):
        ActivityCode.objects.all().delete()
        self.current = catalogue_entry("62010000", "Δραστηριότητες προγραμματισμού")
        self.historical = catalogue_entry("35111000", "Παραγωγή ηλεκτρικής ενέργειας", source="")
        self.both = catalogue_entry("01110000", "Καλλιέργεια σιτηρών")
        self.unmatched = catalogue_entry("99999999", "Άγνωστος κωδικός")
        self.different = catalogue_entry("47110000", "Λιανικό εμπόριο")
        reference_kad("62010000", "kad_2026", "ΔΡΑΣΤΗΡΙΟΤΗΤΕΣ  ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΥ")  # case, accents and spacing only
        reference_kad("35111000", "kad_2008", "Παραγωγή ηλεκτρικής ενέργειας")
        reference_kad("01110000", "kad_2026", "Καλλιέργεια σιτηρών")
        reference_kad("01110000", "kad_2008", "Καλλιέργεια δημητριακών")
        reference_kad("47110000", "kad_2026", "Λιανικό εμπόριο σε μη εξειδικευμένα καταστήματα")


class SchemaTests(TestCase):
    def test_the_migration_only_creates_the_link_table(self):
        migration = MigrationLoader(None, ignore_no_migrations=True).get_migration("gemiapp", "0036_activitycode_kad_links")
        self.assertEqual(migration.dependencies, [("gemiapp", "0035_companyactivity_canonical_metadata")])
        self.assertEqual([(type(op), op.name) for op in migration.operations], [(CreateModel, "ActivityCodeKadLink")])

    def test_activity_code_and_company_activity_are_unchanged(self):
        self.assertEqual(
            [field.name for field in ActivityCode._meta.concrete_fields],
            ["id", "code", "normalized_code", "description", "source", "search_text"],
        )
        self.assertFalse(any(field.related_model is GemiKad for field in CompanyActivity._meta.get_fields() if field.is_relation))

    def test_one_link_per_entry_and_reference_row(self):
        entry_row = catalogue_entry("99990001")
        kad = reference_kad("99990001", "kad_2026")
        ActivityCodeKadLink.objects.create(activity_code=entry_row, gemi_kad=kad, match_basis="code", description_matches=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ActivityCodeKadLink.objects.create(activity_code=entry_row, gemi_kad=kad, match_basis="code", description_matches=True)


class MappingTests(CatalogueFixture, TestCase):
    def setUp(self):
        self.build()

    def test_exact_code_and_version_classification(self):
        report = reconcile_kad_catalogue()
        self.assertEqual(dict(report.classifications), {KAD_2026: 2, KAD_2008_ONLY: 1, BOTH_VERSIONS: 1, UNRESOLVED: 1})
        self.assertEqual(dict(report.unresolved_reasons), {NOT_IN_REFERENCE: 1})
        self.assertEqual(report.links_created, 5)
        self.assertEqual(dict(report.description_differences), {"kad_2026": 1, "kad_2008": 1})
        self.assertEqual((report.origins["aade_catalogue"], report.origins["gemi_fallback"]), (4, 1))
        self.assertEqual(report.classifications_by_origin[("gemi_fallback", KAD_2008_ONLY)], 1)
        self.assertEqual(report.reference_codes_in_both_versions, 1)
        self.assertFalse(self.unmatched.kad_links.exists())

    def test_a_description_difference_is_evidence_not_identity(self):
        reconcile_kad_catalogue()
        link = ActivityCodeKadLink.objects.get(activity_code=self.different)
        self.assertEqual((link.gemi_kad.kad_version, link.description_matches), ("kad_2026", False))
        self.assertTrue(ActivityCodeKadLink.objects.get(activity_code=self.current).description_matches)

    def test_the_same_code_in_both_versions_keeps_two_upstream_identities(self):
        reconcile_kad_catalogue()
        links = ActivityCodeKadLink.objects.filter(activity_code=self.both).select_related("gemi_kad")
        self.assertEqual(sorted(link.gemi_kad.kad_version for link in links), ["kad_2008", "kad_2026"])
        self.assertEqual(len({link.gemi_kad_id for link in links}), 2)

    def test_without_reference_data_everything_is_unresolved_and_nothing_is_linked(self):
        GemiKad.objects.all().delete()
        report = reconcile_kad_catalogue()
        self.assertFalse(report.reference_available)
        self.assertEqual((report.classifications[UNRESOLVED], report.unresolved_reasons[NO_REFERENCE_DATA]), (5, 5))
        self.assertEqual(ActivityCodeKadLink.objects.count(), 0)
        self.assertIn("status=not_synchronised", report.lines()[0])

    def test_an_unknown_company_code_is_accepted_and_classified_later(self):
        company = Company.objects.create(gemi_number="118717203000", name="ΕΤΑΙΡΕΙΑ", incorporation_date=TARGET)
        sync_company_activities(company, [entry("88880000")], as_of=AS_OF)
        fallback = ActivityCode.objects.get(normalized_code="88880000")
        self.assertEqual(fallback.source, "")
        reconcile_kad_catalogue()
        self.assertFalse(fallback.kad_links.exists())

        reference_kad("88880000", "kad_2026", "ΔΡΑΣΤΗΡΙΟΤΗΤΑ 88880000")
        report = reconcile_kad_catalogue()
        self.assertEqual(report.links_created, 1)
        self.assertTrue(fallback.kad_links.filter(gemi_kad__kad_version="kad_2026").exists())

    def test_no_crosswalk_is_stored_or_inferred(self):
        self.assertIsNone(OFFICIAL_KAD_CROSSWALK)
        reference_kad("62019999", "kad_2008", "Δραστηριότητες προγραμματισμού")  # similar description, other code
        reconcile_kad_catalogue()
        self.assertEqual(
            set(ActivityCodeKadLink.objects.filter(activity_code=self.current).values_list("gemi_kad__source_id", flat=True)),
            {"62010000"},
        )
        self.assertIn("crosswalk: unresolved", reconcile_kad_catalogue(dry_run=True).lines()[-1])


class ReconciliationTests(CatalogueFixture, TestCase):
    def setUp(self):
        self.build()

    def test_an_identical_second_run_changes_nothing(self):
        reconcile_kad_catalogue()
        before = link_state()
        again = reconcile_kad_catalogue()
        self.assertEqual((again.links_created, again.links_updated, again.links_removed, again.links_unchanged), (0, 0, 0, 5))
        self.assertEqual(link_state(), before)

    def test_a_dry_run_reports_the_same_and_writes_nothing(self):
        preview = reconcile_kad_catalogue(dry_run=True)
        self.assertEqual(ActivityCodeKadLink.objects.count(), 0)
        real = reconcile_kad_catalogue()
        self.assertEqual([line.replace("[dry-run] ", "") for line in preview.lines()[1:6]], real.lines()[1:6])
        self.assertEqual((preview.links_created, real.links_created), (5, 5))

    def test_batches_give_the_same_result(self):
        small = reconcile_kad_catalogue(dry_run=True, batch_size=2)
        large = reconcile_kad_catalogue(dry_run=True)
        self.assertEqual((small.batches, large.batches), (3, 1))
        self.assertEqual(small.lines(), large.lines())
        with self.assertRaises(ValueError):
            reconcile_kad_catalogue(batch_size=0)

    def test_a_reference_description_update_updates_the_evidence(self):
        reconcile_kad_catalogue()
        GemiKad.objects.filter(source_id="47110000").update(description="ΛΙΑΝΙΚΟ ΕΜΠΟΡΙΟ")
        report = reconcile_kad_catalogue()
        self.assertEqual((report.links_created, report.links_updated), (0, 1))
        self.assertTrue(ActivityCodeKadLink.objects.get(activity_code=self.different).description_matches)

    def test_retired_and_reappearing_reference_rows_through_the_a5_sync(self):
        ActivityCode.objects.all().delete()
        GemiKad.objects.all().delete()
        programming = catalogue_entry("62010000", "Δραστηριότητες προγραμματισμού")
        energy = catalogue_entry("35111000", "Παραγωγή ηλεκτρικής ενέργειας")
        run_sync(reference_payloads())
        reconcile_kad_catalogue()
        self.assertEqual(
            (_classify("62010000").classification, _classify("35111000").classification), (BOTH_VERSIONS, KAD_2008_ONLY),
        )
        links = link_state()

        without = reference_payloads()
        without["/metadata/activities"] = [item for item in without["/metadata/activities"] if item["id"] != "35111000"]
        run_sync(without)
        retired = reconcile_kad_catalogue()
        self.assertEqual(_classify("35111000").unresolved_reason, RETIRED_IN_REFERENCE)
        self.assertEqual((retired.links_removed, retired.codes_with_retired_reference_rows), (0, 1))
        self.assertTrue(energy.kad_links.exists())

        run_sync(reference_payloads())
        back = reconcile_kad_catalogue()
        self.assertEqual(_classify("35111000").classification, KAD_2008_ONLY)
        self.assertEqual(back.links_created, 0)
        self.assertEqual([row[:4] for row in link_state()], [row[:4] for row in links])
        self.assertTrue(programming.kad_links.exists())

    def test_a_link_whose_code_no_longer_matches_is_removed(self):
        reconcile_kad_catalogue()
        self.different.normalized_code = "47119999"
        self.different.save()
        report = reconcile_kad_catalogue()
        self.assertEqual(report.links_removed, 1)
        self.assertFalse(self.different.kad_links.exists())

    def test_command_output_validation_and_privacy(self):
        user = entitled_user("private.person@example.com")
        radar_for(user, "ΙΔΙΩΤΙΚΟ ΟΝΟΜΑ RADAR", ["35111000"])
        out = StringIO()
        with self.assertLogs("gemiapp", level="INFO") as logs:
            call_command("reconcile_gemi_kad_catalogue", "--dry-run", "--list-radar-criteria", stdout=out)
        text = out.getvalue()
        self.assertIn("[dry-run] classification kad_2026=2 kad_2008_only=1 both_versions=1", text)
        self.assertIn("code=35111000 class=kad_2008_only", text)
        rendered = text + "\n".join(logs.output)
        for private in ("ΙΔΙΩΤΙΚΟ ΟΝΟΜΑ RADAR", "private.person@example.com"):
            self.assertNotIn(private, rendered)
        with self.assertRaises(CommandError):
            call_command("reconcile_gemi_kad_catalogue", "--batch-size", "0", stdout=StringIO())


def _classify(code):
    from .ingestion.kad_catalogue import classify_code, load_reference

    return classify_code(code, load_reference())


class RadarCompatibilityTests(CatalogueFixture, TestCase):
    def setUp(self):
        self.build()
        self.user = entitled_user()
        self.current_radar = radar_for(self.user, "Τρέχων", ["62010000"])
        self.historical_radar = radar_for(self.user, "Ιστορικός", ["35111000"])
        self.unresolved_radar = radar_for(self.user, "Άγνωστος", ["99999999"])
        self.deleted_radar = radar_for(self.user, "Διαγραμμένος", ["01110000"], deleted_at=timezone.now())

    def criteria(self):
        through = CustomerRadar.activity_codes.through
        return sorted(through.objects.values_list("customerradar_id", "activitycode_id"))

    def test_saved_criteria_are_classified_but_never_changed(self):
        before = (self.criteria(), list(CustomerRadar.objects.order_by("pk").values()))
        report = reconcile_kad_catalogue()
        self.assertEqual((self.criteria(), list(CustomerRadar.objects.order_by("pk").values())), before)
        self.assertEqual(classes(), {"62010000": KAD_2026, "35111000": KAD_2008_ONLY, "99999999": UNRESOLVED})
        self.assertEqual(dict(report.radar_criteria), {KAD_2026: 1, KAD_2008_ONLY: 1, UNRESOLVED: 1})
        self.assertEqual(report.radars_with_criteria_outside_current_taxonomy, 2)
        self.assertTrue(ActivityCode.objects.filter(pk=self.historical.pk).exists())

    def test_historical_criteria_keep_matching(self):
        company = Company.objects.create(gemi_number="118717203000", name="ΕΝΕΡΓΕΙΑ", incorporation_date=TARGET, prefecture="ΑΤΤΙΚΗΣ")
        sync_company_activities(company, [entry("35111000", version="kad_2008")], as_of=AS_OF)
        reconcile_kad_catalogue()
        company = Company.objects.prefetch_related("activity_records").get(pk=company.pk)
        radar = CustomerRadar.objects.prefetch_related("activity_codes").get(pk=self.historical_radar.pk)
        self.assertTrue(company_matches_radar(company, radar)[0])


class PickerTests(TestCase):
    QUERIES = ("62", "6201", "προγραμματ", "01.1", "ΚΑΛΛΙΕΡΓΕΙΑ", "ab", "4711")

    def setUp(self):
        entitled_user()
        self.client.login(username="member@example.com", password="StrongPass123")
        codes = list(ActivityCode.objects.order_by("pk").values_list("normalized_code", flat=True)[:4])
        self.current, self.historical, self.both, self.retired = codes
        reference_kad(self.current, "kad_2026")
        reference_kad(self.historical, "kad_2008")
        reference_kad(self.both, "kad_2026")
        reference_kad(self.both, "kad_2008")
        reference_kad(self.retired, "kad_2026", present=False)

    def search(self, query):
        return self.client.get(reverse("kad_search"), {"q": query}).json()

    def test_the_default_picker_returns_exactly_the_pre_a8_results(self):
        before = [self.search(query) for query in self.QUERIES]
        catalogue = set(ActivityCode.objects.values_list("pk", flat=True))
        reconcile_kad_catalogue()
        self.assertEqual([self.search(query) for query in self.QUERIES], before)
        self.assertEqual(set(kad_picker_queryset().values_list("pk", flat=True)), catalogue)
        self.assertEqual(self.search(self.historical)["results"][0]["normalized_code"], self.historical)

    @override_settings(GEMI_KAD_PICKER_CURRENT_TAXONOMY_ONLY=True)
    def test_the_future_picker_offers_only_present_kad_2026_entries(self):
        reconcile_kad_catalogue()
        offered = set(kad_picker_queryset().values_list("normalized_code", flat=True))
        self.assertEqual(offered, {self.current, self.both})
        for code in (self.historical, self.retired):
            self.assertNotIn(code, [item["normalized_code"] for item in self.search(code)["results"]])
        self.assertIn(self.current, [item["normalized_code"] for item in self.search(self.current)["results"]])


class VersionHelperTests(TestCase):
    def test_codes_by_version(self):
        ActivityCode.objects.all().delete()
        for code in ("62010000", "35111000", "43210000"):
            catalogue_entry(code)
        reference_kad("62010000", "kad_2026")
        reference_kad("62010000", "kad_2008")
        reference_kad("35111000", "kad_2008")
        reference_kad("43210000", "kad_2026", present=False)
        reconcile_kad_catalogue()
        current = set(activity_codes_in_version("kad_2026").values_list("normalized_code", flat=True))
        historical = set(activity_codes_in_version("kad_2008").values_list("normalized_code", flat=True))
        self.assertEqual((current, historical), ({"62010000"}, {"62010000", "35111000"}))
        self.assertEqual(set(activity_codes_in_version("kad_2026", present_only=False).values_list("normalized_code", flat=True)), {"62010000", "43210000"})

    def test_company_activities_resolve_to_their_own_kad_version_and_are_not_changed(self):
        company = Company.objects.create(gemi_number="118717203000", name="ΕΤΑΙΡΕΙΑ", incorporation_date=TARGET)
        payload = [
            entry("62010000", "Κύρια", version="kad_2008", dt_from="2010-01-01", dt_to="2026-03-01"),
            entry("62010000", "Κύρια", version="kad_2026", dt_from="2026-03-01"),
            entry("62010000", "Δευτερεύουσα", version=None),
        ]
        sync_company_activities(company, payload, as_of=AS_OF)
        old = reference_kad("62010000", "kad_2008")
        new = reference_kad("62010000", "kad_2026")
        unpublished = reference_kad("62010000", "")
        before = list(CompanyActivity.objects.order_by("pk").values())
        reconcile_kad_catalogue()
        self.assertEqual(list(CompanyActivity.objects.order_by("pk").values()), before)
        resolved = {row.kad_version: gemi_kad_for_activity(row) for row in CompanyActivity.objects.filter(company=company)}
        self.assertEqual(resolved, {"kad_2008": old, "kad_2026": new, None: unpublished})


class CustomerParityTests(TestCase):
    """With the A8 picker flag and the A7 matching flag off, everything customer-visible is identical before and
    after the catalogue is reconciled."""

    QUERIES = ("62", "4711", "προγραμματ", "35111")

    def setUp(self):
        self.user = entitled_user()
        self.client.login(username="member@example.com", password="StrongPass123")
        self.items = [
            full_item("118717203000", TARGET.isoformat(), activities=copy.deepcopy(MIXED)),
            full_item("118717204000", TARGET.isoformat(), activities=[entry("35111000", version="kad_2008", dt_from="2001-01-01")]),
        ]
        with patch("gemiapp.services.fetch_companies", side_effect=lambda _: copy.deepcopy(self.items)):
            import_for_date(TARGET)
        radar_for(self.user, "Λογισμικό", ["62010000"])
        radar_for(self.user, "Ενέργεια", ["35111000"])
        radar_for(self.user, "Αττική", prefectures=["ΑΤΤΙΚΗΣ"])
        reference_kad("62010000", "kad_2026")
        reference_kad("62010000", "kad_2008")
        reference_kad("35111000", "kad_2008")
        reference_kad("47110000", "kad_2026", "ΑΛΛΗ ΠΕΡΙΓΡΑΦΗ")
        reference_kad("43210000", "kad_2026", present=False)

    def rematch(self):
        UserCompanyLead.objects.all().delete()
        DigestDelivery.objects.all().delete()
        match_imported_companies(ImportRun.objects.create(target_date=TARGET, status="success"))

    def snapshot(self):
        from .views import _filtered_companies

        factory = RequestFactory()
        companies = list(Company.objects.prefetch_related("activity_records").order_by("pk"))
        radars = list(CustomerRadar.objects.prefetch_related("activity_codes").order_by("pk"))
        codes = ("62010000", "35111000", "47110000")
        state = {
            "catalogue": list(ActivityCode.objects.order_by("pk").values_list()),
            "criteria": sorted(CustomerRadar.activity_codes.through.objects.values_list("customerradar_id", "activitycode_id")),
            "radars": list(CustomerRadar.objects.order_by("pk").values()),
            "activities": list(CompanyActivity.objects.order_by("pk").values()),
            "matches": list(RadarMatch.objects.order_by("radar_id", "company_id").values_list("radar_id", "company_id", "matched_on", "matched_activity_codes", "match_reason")),
            "leads": list(UserCompanyLead.objects.order_by("company_id").values_list("user_id", "company_id", "status")),
            "subscriptions": list(UserSubscription.objects.order_by("pk").values()),
            "predicate": [(r.pk, c.pk, company_matches_radar(c, r)) for r in radars for c in companies],
            "preview": {code: sorted(filter_companies_for_radar(Company.objects.all(), activity_codes=[code]).values_list("pk", flat=True)) for code in codes},
            "dashboard": {code: sorted(_filtered_companies(factory.get("/", {"kad": code})).values_list("pk", flat=True)) for code in codes},
            "picker": [self.client.get(reverse("kad_search"), {"q": query}).json() for query in self.QUERIES],
        }
        with patch("django.core.signing.time.time", return_value=1789000000.0):
            mail.outbox = []
            state["digest"] = (send_digests(TARGET), [(m.subject, m.body) for m in mail.outbox])
            state["export"] = {code: self.client.get(reverse("export_csv"), {"kad": code}).content for code in codes}
            state["lead_export"] = self.client.get(reverse("lead_export_csv")).content
            state["radar_exports"] = [self.client.get(reverse("radar_export_csv", args=[r.pk])).content for r in radars]
        state["detail"] = [
            self.client.get(reverse("company_detail", args=[c.gemi_number])).content.decode()
            .split("ΔΡΑΣΤΗΡΙΟΤΗΤΕΣ ΚΑΔ", 1)[1].split("<!-- Lead Work Column", 1)[0]
            for c in companies
        ]
        return state

    def test_reconciliation_changes_nothing_customer_visible(self):
        from django.conf import settings

        self.assertFalse(settings.GEMI_KAD_PICKER_CURRENT_TAXONOMY_ONLY)
        self.assertFalse(settings.GEMI_MATCH_CURRENT_ACTIVITIES_ONLY)
        self.rematch()
        before = self.snapshot()
        self.assertTrue(before["matches"])

        report = reconcile_kad_catalogue()
        self.assertGreater(report.links_created, 0)
        self.rematch()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(User.objects.count(), 1)
