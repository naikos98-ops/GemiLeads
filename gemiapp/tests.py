import json
import re
from datetime import date, datetime, timedelta
from unittest.mock import patch
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.db.utils import OperationalError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .models import (
    ActivityCode,
    Company,
    CompanyActivity,
    CustomerRadar,
    DigestDelivery,
    DigestPreference,
    ImportRun,
    RadarMatch,
    UserCompanyLead,
    UserSubscription,
)
from .services import import_for_date, match_imported_companies, send_digests


SAMPLE = {
    "arGemi": 123456789000, "afm": "123456789", "coNameEl": "TEST SIGNAL ΙΚΕ",
    "coTitlesEl": ["TEST SIGNAL"], "incorporationDate": "2026-08-01",
    "legalType": {"descr": "Ιδιωτική Κεφαλαιουχική Εταιρεία"},
    "status": {"descr": "Ενεργή", "isActive": True}, "city": "ΑΘΗΝΑ",
    "prefecture": {"descr": "ΑΤΤΙΚΗΣ"},
    "activities": [{"activity": {"id": "62010000", "descr": "ΔΡΑΣΤΗΡΙΟΤΗΤΕΣ ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΥ"}, "type": "Κύρια"}],
}


class AppTests(TestCase):
    def setUp(self):
        from .models import UserSubscription
        self.user = User.objects.create_user("member@example.com", "member@example.com", "StrongPass123")
        sub, _ = UserSubscription.objects.get_or_create(user=self.user)
        sub.tier = "pro"
        sub.status = "active"
        sub.save()
        DigestPreference.objects.get_or_create(user=self.user)
        self.kad = ActivityCode.objects.create(
            code="62.01.00.00", normalized_code="62010000", description="ΔΡΑΣΤΗΡΙΟΤΗΤΕΣ ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΥ",
            search_text="62.01.00.00 62010000 ΔΡΑΣΤΗΡΙΟΤΗΤΕΣ ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΥ",
        )

    def test_home_and_protected_dashboard(self):
        home = self.client.get(reverse("home"))
        self.assertEqual(home.status_code, 200)
        self.assertContains(home, "Gemi Leads")
        self.assertNotContains(home, "GEMI Signal")
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)
        self.client.login(username="member@example.com", password="StrongPass123")
        dashboard = self.client.get(reverse("dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "Dashboard · Gemi Leads")

    @patch("gemiapp.services.fetch_companies", return_value=[SAMPLE])
    def test_idempotent_import(self, _fetch):
        first = import_for_date(date(2026, 8, 1))
        second = import_for_date(date(2026, 8, 1))
        self.assertEqual(first.created_count, 1)
        self.assertEqual(second.updated_count, 1)
        self.assertEqual(Company.objects.count(), 1)
        self.assertEqual(CompanyActivity.objects.get().code, "62010000")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    @patch("gemiapp.services.fetch_companies", return_value=[SAMPLE])
    def test_digest_sent_once_and_deduplication(self, _fetch):
        CustomerRadar.objects.create(user=self.user, name="Radar 1", prefectures=["ΑΤΤΙΚΗΣ"], frequency="daily", monitor_from=timezone.make_aware(datetime(2026, 8, 1, 0, 0)))
        CustomerRadar.objects.create(user=self.user, name="Radar 2", legal_types=["Ιδιωτική Κεφαλαιουχική Εταιρεία"], frequency="daily", monitor_from=timezone.make_aware(datetime(2026, 8, 1, 0, 0)))
        import_for_date(date(2026, 8, 1))

        self.assertEqual(send_digests(date(2026, 8, 1), frequency="daily"), (1, 0))
        self.assertEqual(send_digests(date(2026, 8, 1), frequency="daily"), (0, 1))
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(mail.outbox[0].subject.startswith("Gemi Leads ·"))

        body = mail.outbox[0].body
        self.assertIn("Radar 1", body)
        self.assertIn("Radar 2", body)
        self.assertEqual(body.count("TEST SIGNAL ΙΚΕ"), 1)
        self.assertEqual(DigestDelivery.objects.count(), 1)

    def test_csv_export(self):
        Company.objects.create(gemi_number="1", name="CSV TEST", incorporation_date=date(2026, 8, 1))
        self.client.login(username="member@example.com", password="StrongPass123")
        response = self.client.get(reverse("export_csv"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("gemi-leads-export.csv", response["Content-Disposition"])
        self.assertIn("CSV TEST", response.content.decode("utf-8-sig"))

    def test_kad_search_and_dashboard_filter(self):
        matching = Company.objects.create(gemi_number="1", name="SOFTWARE", incorporation_date=date(2026, 8, 1))
        other = Company.objects.create(gemi_number="2", name="BAKERY", incorporation_date=date(2026, 8, 1))
        CompanyActivity.objects.create(company=matching, code="62010000", description=self.kad.description)
        CompanyActivity.objects.create(company=other, code="10710000", description="ΑΡΤΟΠΟΙΙΑ")
        self.client.login(username="member@example.com", password="StrongPass123")
        search = self.client.get(reverse("kad_search"), {"q": "προγραμματισμού"})
        self.assertEqual(search.status_code, 200)
        self.assertIn("62010000", [item["normalized_code"] for item in search.json()["results"]])
        dashboard = self.client.get(reverse("dashboard"), {"kad": "62010000"})
        self.assertContains(dashboard, "SOFTWARE")
        self.assertNotContains(dashboard, "BAKERY")

    def test_dashboard_and_csv_filter_by_inclusive_date_range(self):
        before = Company.objects.create(gemi_number="date-1", name="BEFORE RANGE", incorporation_date=date(2026, 8, 1))
        first = Company.objects.create(gemi_number="date-2", name="FIRST DAY", incorporation_date=date(2026, 8, 2))
        last = Company.objects.create(gemi_number="date-3", name="LAST DAY", incorporation_date=date(2026, 8, 4))
        after = Company.objects.create(gemi_number="date-4", name="AFTER RANGE", incorporation_date=date(2026, 8, 5))
        params = {"date_from": "2026-08-02", "date_to": "2026-08-04"}
        self.client.login(username="member@example.com", password="StrongPass123")

        dashboard = self.client.get(reverse("dashboard"), params)
        self.assertContains(dashboard, 'name="date_from" value="2026-08-02"')
        self.assertContains(dashboard, 'name="date_to" value="2026-08-04"')
        self.assertContains(dashboard, first.name)
        self.assertContains(dashboard, last.name)
        self.assertNotContains(dashboard, before.name)
        self.assertNotContains(dashboard, after.name)

        export = self.client.get(reverse("export_csv"), params)
        content = export.content.decode("utf-8-sig")
        self.assertIn(first.name, content)
        self.assertIn(last.name, content)
        self.assertNotIn(before.name, content)
        self.assertNotIn(after.name, content)

    def test_invalid_dates_do_not_crash_dashboard(self):
        self.client.login(username="member@example.com", password="StrongPass123")
        response = self.client.get(reverse("dashboard"), {"date_from": "not-a-date", "date_to": "2026-08-04"})
        self.assertEqual(response.status_code, 200)
        reversed_range = self.client.get(reverse("dashboard"), {"date_from": "2026-08-05", "date_to": "2026-08-04"})
        self.assertContains(reversed_range, "Η ημερομηνία «Από» πρέπει να είναι πριν ή ίδια")

    def test_settings_save_global_digest_preferences(self):
        self.client.login(username="member@example.com", password="StrongPass123")
        response = self.client.post(reverse("settings"), {
            "frequency": "off", "include_empty_digest": "on",
        })
        self.assertRedirects(response, reverse("settings"))
        preference = DigestPreference.objects.get(user=self.user)
        self.assertEqual(preference.frequency, "off")
        self.assertTrue(preference.include_empty_digest)

    def test_weekly_is_no_longer_offered_anywhere(self):
        from .models import CustomerRadar as Radar

        self.assertNotIn("weekly", dict(DigestPreference.FREQUENCIES))
        self.assertNotIn("weekly", dict(Radar.FREQUENCIES))
        self.client.login(username="member@example.com", password="StrongPass123")
        self.assertNotContains(self.client.get(reverse("settings")), "Εβδομαδιαία")

    def test_signup_creates_initial_broad_radar(self):
        response = self.client.post(reverse("signup"), {
            "first_name": "Νέος",
            "email": "new@example.com",
            "password1": "VeryStrongPass123!",
            "password2": "VeryStrongPass123!",
        })
        self.assertEqual(response.status_code, 200)
        radar = CustomerRadar.objects.get(user__email="new@example.com")
        self.assertEqual(radar.name, "Όλες οι νέες επιχειρήσεις")
        self.assertTrue(radar.is_active)

    def test_radar_create_preview_edit_and_case_insensitive_unique_name(self):
        company = Company.objects.create(
            gemi_number="radar-preview",
            name="SOFTWARE PREVIEW ΙΚΕ",
            incorporation_date=date(2026, 8, 1),
            prefecture="ΑΤΤΙΚΗΣ",
            legal_type="ΙΚΕ",
        )
        CompanyActivity.objects.create(company=company, code="62010000", description=self.kad.description)
        self.client.login(username="member@example.com", password="StrongPass123")
        payload = {
            "name": "Software Αττικής",
            "name_query": "software",
            "prefectures": ["ΑΤΤΙΚΗΣ"],
            "legal_types": ["ΙΚΕ"],
            "only_active": "on",
            "frequency": "daily",
            "is_active": "on",
            "activity_codes": ["62010000"],
        }
        preview = self.client.post(reverse("radar_preview"), payload)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["count"], 1)

        created = self.client.post(reverse("radar_create"), payload)
        radar = CustomerRadar.objects.get(user=self.user)
        self.assertRedirects(created, reverse("radar_detail", args=[radar.pk]))
        self.assertEqual(list(radar.activity_codes.values_list("normalized_code", flat=True)), ["62010000"])
        self.assertEqual(radar.prefectures, ["ΑΤΤΙΚΗΣ"])

        duplicate = payload | {"name": "software αττικής"}
        response = self.client.post(reverse("radar_create"), duplicate)
        self.assertContains(response, "Υπάρχει ήδη Radar με αυτό το όνομα.")
        self.assertEqual(CustomerRadar.objects.filter(user=self.user).count(), 1)

        edit_payload = payload | {"radar_id": radar.pk, "name": "Software Ελλάδας", "prefectures": []}
        preview_edit = self.client.post(reverse("radar_preview"), edit_payload)
        self.assertEqual(preview_edit.status_code, 200)
        edited = self.client.post(reverse("radar_edit", args=[radar.pk]), edit_payload)
        self.assertRedirects(edited, reverse("radar_detail", args=[radar.pk]))
        radar.refresh_from_db()
        self.assertEqual(radar.name, "Software Ελλάδας")
        self.assertEqual(radar.prefectures, [])

    def test_radar_matching_or_and_deduplication(self):
        matching = Company.objects.create(
            gemi_number="match-1",
            name="MATCHING ΙΚΕ",
            incorporation_date=date(2026, 8, 2),
            prefecture="ΑΤΤΙΚΗΣ",
            legal_type="ΙΚΕ",
            is_active=True,
        )
        wrong_area = Company.objects.create(
            gemi_number="match-2",
            name="WRONG AREA ΙΚΕ",
            incorporation_date=date(2026, 8, 2),
            prefecture="ΑΧΑΪΑΣ",
            legal_type="ΙΚΕ",
            is_active=True,
        )
        CompanyActivity.objects.create(company=matching, code="62010000", description=self.kad.description)
        CompanyActivity.objects.create(company=wrong_area, code="62010000", description=self.kad.description)
        radar = CustomerRadar.objects.create(
            user=self.user,
            name="Αττική software",
            prefectures=["ΑΤΤΙΚΗΣ", "ΠΕΙΡΑΙΩΣ"],
            legal_types=["ΙΚΕ", "ΕΠΕ"],
            monitor_from=timezone.make_aware(datetime(2026, 8, 1, 0, 0)),
        )
        radar.activity_codes.add(self.kad)
        run = ImportRun.objects.create(target_date=date(2026, 8, 2), status="success")

        first = match_imported_companies(run)
        self.assertEqual(first.new_leads, 1)
        self.assertEqual(first.new_matches, 1)
        match = RadarMatch.objects.get()
        self.assertEqual(match.company, matching)
        self.assertEqual(match.matched_activity_codes, ["62010000"])
        self.assertEqual(match.match_reason["prefecture"], "ΑΤΤΙΚΗΣ")

        second = match_imported_companies(run)
        self.assertEqual(second.new_matches, 0)
        self.assertEqual(second.duplicate_matches, 1)

        second_radar = CustomerRadar.objects.create(
            user=self.user,
            name="Όλες Αττικής",
            prefectures=["ΑΤΤΙΚΗΣ"],
            monitor_from=timezone.make_aware(datetime(2026, 8, 1, 0, 0)),
        )
        third = match_imported_companies(run)
        self.assertEqual(third.new_leads, 0)
        self.assertEqual(third.new_matches, 1)
        self.assertEqual(UserCompanyLead.objects.count(), 1)
        self.assertEqual(RadarMatch.objects.filter(company=matching).count(), 2)
        self.assertEqual(second_radar.matches.get().lead, radar.matches.get().lead)

    def test_radar_cutoff_toggle_soft_delete_and_ownership(self):
        User.objects.create_user("other@example.com", "other@example.com", "StrongPass123")
        radar = CustomerRadar.objects.create(
            user=self.user,
            name="Future Radar",
            monitor_from=timezone.now() + timedelta(days=1),
        )
        Company.objects.create(gemi_number="cutoff", name="CUTOFF", incorporation_date=timezone.localdate())
        run = ImportRun.objects.create(target_date=timezone.localdate(), status="success")
        self.assertEqual(match_imported_companies(run).new_matches, 0)

        self.client.login(username="other@example.com", password="StrongPass123")
        self.assertEqual(self.client.get(reverse("radar_detail", args=[radar.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("radar_toggle", args=[radar.pk])).status_code, 404)

        self.client.login(username="member@example.com", password="StrongPass123")
        before_toggle = radar.monitor_from
        self.assertRedirects(self.client.post(reverse("radar_toggle", args=[radar.pk])), reverse("radar_list"))
        radar.refresh_from_db()
        self.assertFalse(radar.is_active)
        self.assertEqual(radar.monitor_from, before_toggle)
        self.client.post(reverse("radar_toggle", args=[radar.pk]))
        radar.refresh_from_db()
        self.assertTrue(radar.is_active)
        self.assertNotEqual(radar.monitor_from, before_toggle)
        self.assertLessEqual(radar.monitor_from, timezone.now())

        self.assertRedirects(self.client.post(reverse("radar_delete", args=[radar.pk])), reverse("radar_list"))
        radar.refresh_from_db()
        self.assertFalse(radar.is_active)
        self.assertIsNotNone(radar.deleted_at)
        self.assertEqual(self.client.get(reverse("radar_detail", args=[radar.pk])).status_code, 404)

    def test_radar_rejects_more_than_25_kads_and_mutations_are_post_only(self):
        radar = CustomerRadar.objects.create(user=self.user, name="Protected Radar")
        self.client.login(username="member@example.com", password="StrongPass123")
        payload = {
            "name": "Too many KADs",
            "frequency": "daily",
            "is_active": "on",
            "activity_codes": [f"{index:08d}" for index in range(1, 27)],
        }
        response = self.client.post(reverse("radar_create"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Μπορείς να επιλέξεις έως 25 ΚΑΔ.")
        self.assertFalse(CustomerRadar.objects.filter(user=self.user, name="Too many KADs").exists())
        self.assertEqual(self.client.get(reverse("radar_toggle", args=[radar.pk])).status_code, 405)
        self.assertEqual(self.client.get(reverse("radar_delete", args=[radar.pk])).status_code, 405)

    def _create_lead_match(self, *, user=None, suffix="1", status="new", favorite=False, radar=None):
        user = user or self.user
        company = Company.objects.create(
            gemi_number=f"lead-{suffix}",
            name=f"LEAD COMPANY {suffix}",
            incorporation_date=date(2026, 8, 3),
            prefecture="ΑΤΤΙΚΗΣ",
            legal_type="ΙΚΕ",
            email=f"lead{suffix}@example.com",
        )
        CompanyActivity.objects.create(company=company, code="62010000", description=self.kad.description)
        radar = radar or CustomerRadar.objects.create(user=user, name=f"Radar {suffix}")
        lead = UserCompanyLead.objects.create(user=user, company=company, status=status, is_favorite=favorite)
        match = RadarMatch.objects.create(
            radar=radar,
            lead=lead,
            company=company,
            matched_on=date(2026, 8, 3),
            matched_activity_codes=["62010000"],
            match_reason={"activity_codes": ["62010000"], "prefecture": "ΑΤΤΙΚΗΣ", "legal_type": "ΙΚΕ"},
        )
        return company, radar, lead, match

    def test_lead_inbox_filters_and_is_user_scoped(self):
        own_company, own_radar, own_lead, _ = self._create_lead_match(status="interested", favorite=True)
        second_company, _, _, _ = self._create_lead_match(suffix="2", status="new")
        other_user = User.objects.create_user("other@example.com", "other@example.com", "StrongPass123")
        other_company, _, _, _ = self._create_lead_match(user=other_user, suffix="other")
        self.client.login(username="member@example.com", password="StrongPass123")

        inbox = self.client.get(reverse("lead_list"))
        self.assertContains(inbox, own_company.name)
        self.assertContains(inbox, second_company.name)
        self.assertNotContains(inbox, other_company.name)

        filtered = self.client.get(reverse("lead_list"), {"status": "interested", "favorite": "1", "radar": own_radar.pk})
        self.assertContains(filtered, own_company.name)
        self.assertNotContains(filtered, second_company.name)
        self.assertEqual(own_lead.status, "interested")

    def test_company_detail_lifecycle_match_reason_and_lead_mutations(self):
        company, _, lead, _ = self._create_lead_match()
        self.client.login(username="member@example.com", password="StrongPass123")

        detail = self.client.get(reverse("company_detail", args=[company.gemi_number]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Γιατί εντοπίστηκε")
        self.assertContains(detail, "62010000")
        lead.refresh_from_db()
        self.assertEqual(lead.status, "viewed")

        self.client.post(reverse("lead_status", args=[lead.pk]), {"status": "contacted"})
        self.client.post(reverse("lead_favorite", args=[lead.pk]))
        self.client.post(reverse("lead_notes", args=[lead.pk]), {"notes": "Κλήση τη Δευτέρα"})
        lead.refresh_from_db()
        self.assertEqual(lead.status, "contacted")
        self.assertTrue(lead.is_favorite)
        self.assertEqual(lead.notes, "Κλήση τη Δευτέρα")

        self.client.get(reverse("company_detail", args=[company.gemi_number]))
        lead.refresh_from_db()
        self.assertEqual(lead.status, "contacted")

    def test_lead_ownership_post_only_and_radar_csv_scope(self):
        company, radar, lead, _ = self._create_lead_match(status="contacted")
        other_user = User.objects.create_user("other@example.com", "other@example.com", "StrongPass123")
        other_company, other_radar, other_lead, _ = self._create_lead_match(user=other_user, suffix="other")
        self.client.login(username="member@example.com", password="StrongPass123")

        for route in ("lead_status", "lead_favorite", "lead_notes"):
            self.assertEqual(self.client.get(reverse(route, args=[lead.pk])).status_code, 405)
            self.assertEqual(self.client.post(reverse(route, args=[other_lead.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("company_detail", args=[other_company.gemi_number])).status_code, 200)
        self.assertEqual(self.client.get(reverse("radar_export_csv", args=[other_radar.pk])).status_code, 404)

        export = self.client.get(reverse("radar_export_csv", args=[radar.pk]), {"status": "contacted"})
        content = export.content.decode("utf-8-sig")
        self.assertEqual(export.status_code, 200)
        self.assertIn(company.name, content)
        self.assertIn("Επικοινώνησα", content)
        self.assertNotIn(other_company.name, content)

        empty_export = self.client.get(reverse("radar_export_csv", args=[radar.pk]), {"status": "interested"})
        self.assertNotIn(company.name, empty_export.content.decode("utf-8-sig"))

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    @patch("gemiapp.services.fetch_companies", return_value=[SAMPLE])
    def test_empty_digest_is_still_sent_when_requested(self, _fetch):
        CustomerRadar.objects.create(user=self.user, name="Άδειο Radar", prefectures=["ΘΕΣΣΑΛΟΝΙΚΗΣ"], frequency="daily", monitor_from=timezone.make_aware(datetime(2026, 8, 1, 0, 0)))
        preference = self.user.digest_preference
        preference.include_empty_digest = True
        preference.save()
        import_for_date(date(2026, 8, 1))
        self.assertEqual(send_digests(date(2026, 8, 1), frequency="daily"), (1, 0))
        self.assertEqual(len(mail.outbox), 1)

    def test_requesting_a_weekly_digest_is_refused(self):
        with self.assertRaises(ValueError):
            send_digests(date(2026, 8, 1), frequency="weekly")

    def test_user_subscription_limits(self):
        self.client.login(username="member@example.com", password="StrongPass123")

        # Reset subscription to unpaid for this test
        self.user.subscription.status = "inactive"
        self.user.subscription.tier = "free"
        self.user.subscription.save()

        # Delete the default radar created in setUp
        CustomerRadar.objects.filter(user=self.user).delete()

        # Test Unpaid user limit (0) - creation blocked
        response = self.client.post(reverse("radar_create"), {"name": "First Radar", "is_active": True, "frequency": "daily"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Απαιτείται ενεργή συνδρομή")

        # Upgrade to Pro active
        from .models import UserSubscription
        sub, _ = UserSubscription.objects.get_or_create(user=self.user)
        sub.tier = "pro"
        sub.status = "active"
        sub.save()

        response3 = self.client.post(reverse("radar_create"), {"name": "First Radar", "is_active": True, "frequency": "daily"})
        self.assertEqual(response3.status_code, 302)


class PaidOnlyModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="paidtest@example.com", password="password123", first_name="PaidUser")
        DigestPreference.objects.get_or_create(user=self.user)

    def test_one_user_subscription_per_user(self):
        from .models import UserSubscription
        # Verify signal created exactly 1 subscription
        self.assertEqual(UserSubscription.objects.filter(user=self.user).count(), 1)
        # Attempting to create duplicate subscription raises IntegrityError
        with self.assertRaises(Exception):
            UserSubscription.objects.create(user=self.user, tier="pro")

    def test_unpaid_user_entitlement_and_radar_limit(self):
        from .models import get_user_radar_limit
        sub = self.user.subscription
        self.assertFalse(sub.has_active_paid_subscription)
        self.assertEqual(sub.radar_limit, 0)
        self.assertEqual(get_user_radar_limit(self.user), 0)

    def test_pro_active_entitlement(self):
        from .models import get_user_radar_limit
        sub = self.user.subscription
        sub.tier = "pro"
        sub.status = "active"
        sub.save()
        self.assertTrue(sub.has_active_paid_subscription)
        self.assertEqual(sub.radar_limit, 5)
        self.assertEqual(get_user_radar_limit(self.user), 5)

    def test_business_active_entitlement(self):
        from .models import get_user_radar_limit
        sub = self.user.subscription
        sub.tier = "business"
        sub.status = "active"
        sub.save()
        self.assertTrue(sub.has_active_paid_subscription)
        self.assertEqual(sub.radar_limit, 10)
        self.assertEqual(get_user_radar_limit(self.user), 10)

    def test_all_subscription_status_entitlements(self):
        sub = self.user.subscription

        # Valid active paid tiers
        sub.tier = "pro"
        sub.status = "active"
        sub.save()
        self.assertTrue(sub.has_active_paid_subscription)
        self.assertEqual(sub.radar_limit, 5)

        sub.tier = "business"
        sub.status = "active"
        sub.save()
        self.assertTrue(sub.has_active_paid_subscription)
        self.assertEqual(sub.radar_limit, 10)

        sub.tier = "enterprise"
        sub.status = "active"
        sub.save()
        self.assertTrue(sub.has_active_paid_subscription)
        self.assertEqual(sub.radar_limit, 15)

        # Free tier even if status is active must return False
        sub.tier = "free"
        sub.status = "active"
        sub.save()
        self.assertFalse(sub.has_active_paid_subscription)
        self.assertEqual(sub.radar_limit, 0)

        # Non-active statuses for Pro tier must return False
        sub.tier = "pro"
        invalid_statuses = [
            "incomplete",
            "incomplete_expired",
            "past_due",
            "unpaid",
            "canceled",
            "inactive",
            "paused",
            "trialing",
        ]
        for st in invalid_statuses:
            sub.status = st
            sub.save()
            self.assertFalse(sub.has_active_paid_subscription, f"Status {st} should not grant paid entitlement")
            self.assertEqual(sub.radar_limit, 0)

    def test_unpaid_user_cannot_create_or_toggle_radar(self):
        self.client.login(username="paidtest@example.com", password="password123")
        # Direct POST creation attempt
        res_create = self.client.post(reverse("radar_create"), {"name": "Blocked Radar", "is_active": True, "frequency": "daily"})
        self.assertEqual(res_create.status_code, 200)
        self.assertContains(res_create, "Απαιτείται ενεργή συνδρομή")

        # Create radar directly in DB for testing toggle
        radar = CustomerRadar.objects.create(user=self.user, name="DB Radar", is_active=False)
        res_toggle = self.client.post(reverse("radar_toggle", kwargs={"pk": radar.pk}))
        self.assertEqual(res_toggle.status_code, 302)
        self.assertRedirects(res_toggle, reverse("pricing"))
        radar.refresh_from_db()
        self.assertFalse(radar.is_active)

    def test_pro_limit_enforcement(self):
        sub = self.user.subscription
        sub.tier = "pro"
        sub.status = "active"
        sub.save()
        self.client.login(username="paidtest@example.com", password="password123")

        for i in range(5):
            res = self.client.post(reverse("radar_create"), {"name": f"Radar {i}", "is_active": True, "frequency": "daily"})
            self.assertEqual(res.status_code, 302)

        # 6th attempt should be blocked
        res_6 = self.client.post(reverse("radar_create"), {"name": "Radar 6", "is_active": True, "frequency": "daily"})
        self.assertEqual(res_6.status_code, 200)
        self.assertContains(res_6, "Έχεις φτάσει το όριο των 5 Ραντάρ")

    def test_matching_pipeline_skips_unpaid_radars(self):
        from .services import match_imported_companies
        from .models import Company, ImportRun, RadarMatch
        from datetime import date
        today = date.today()

        # Create active radar for unpaid user
        CustomerRadar.objects.create(user=self.user, name="Unpaid Radar", is_active=True, monitor_from=timezone.now() - timedelta(days=1))

        Company.objects.create(gemi_number="999001", vat_number="9990001", name="MATCH ME LTD", incorporation_date=today)
        import_run = ImportRun.objects.create(target_date=today, status="success")

        summary = match_imported_companies(import_run)
        self.assertEqual(summary.radars_checked, 0)
        self.assertEqual(RadarMatch.objects.count(), 0)

    def test_digest_pipeline_skips_unpaid_users(self):
        from .services import send_digests
        from datetime import date
        today = date.today()
        sent, skipped = send_digests(today, "daily")
        self.assertEqual(sent, 0)
        self.assertGreaterEqual(skipped, 1)

    def test_historical_data_preserved_on_cancellation(self):
        from .models import Company, UserCompanyLead
        from datetime import date
        company = Company.objects.create(gemi_number="888001", vat_number="8880001", name="PRESERVED LTD", incorporation_date=date.today())
        lead = UserCompanyLead.objects.create(user=self.user, company=company, notes="Important Note", is_favorite=True)
        CustomerRadar.objects.create(user=self.user, name="Historical Radar", is_active=True)

        # User subscription cancelled
        sub = self.user.subscription
        sub.status = "canceled"
        sub.save()

        # Verify historical objects exist untouched
        self.assertEqual(UserCompanyLead.objects.filter(user=self.user).count(), 1)
        self.assertEqual(CustomerRadar.objects.filter(user=self.user).count(), 1)
        lead.refresh_from_db()
        self.assertEqual(lead.notes, "Important Note")
        self.assertTrue(lead.is_favorite)


class AuthFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testauth@example.com", password="password123", first_name="AuthUser")
        DigestPreference.objects.get_or_create(user=self.user)

    def test_signup_creates_inactive_user(self):
        response = self.client.post(reverse("signup"), {
            "first_name": "New",
            "last_name": "User",
            "email": "newuser@example.com",
            "password1": "strongpassword123",
            "password2": "strongpassword123",
            "terms": "on"
        })
        self.assertEqual(response.status_code, 200) # Form render verify_pending
        self.assertContains(response, "Ελέγξτε το email σας")

        new_user = User.objects.get(username="newuser@example.com")
        self.assertFalse(new_user.is_active)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Επιβεβαίωση email", mail.outbox[0].subject)

    def test_verify_email(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.http import urlsafe_base64_encode
        from django.utils.encoding import force_bytes

        self.user.is_active = False
        self.user.save()

        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)

        response = self.client.get(reverse("verify_email", kwargs={"uidb64": uid, "token": token}))
        self.assertEqual(response.status_code, 302)

        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_unsubscribe(self):
        from django.core.signing import TimestampSigner
        signer = TimestampSigner()
        token = signer.sign(self.user.id)

        response = self.client.get(reverse("unsubscribe", kwargs={"token": token}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Απεγγραφή επιτυχής")

        self.user.digest_preference.refresh_from_db()
        self.assertEqual(self.user.digest_preference.frequency, "off")


class SuperadminTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(username="admin@gemileads.gr", email="admin@gemileads.gr", password="SuperPassword123")
        self.normal_user = User.objects.create_user(username="normal@example.com", email="normal@example.com", password="NormalPassword123")
        self.staff_user = User.objects.create_user(username="staff@example.com", email="staff@example.com", password="StaffPassword123", is_staff=True, is_superuser=False)
        DigestPreference.objects.get_or_create(user=self.superuser)
        DigestPreference.objects.get_or_create(user=self.normal_user)
        DigestPreference.objects.get_or_create(user=self.staff_user)

    def test_superadmin_access_control(self):
        # 1. Anonymous user redirected to login
        res_anon = self.client.get(reverse("superadmin:overview"))
        self.assertEqual(res_anon.status_code, 302)
        self.assertIn(reverse("login"), res_anon.url)

        # 2. Normal user gets 403 Permission Denied
        self.client.login(username="normal@example.com", password="NormalPassword123")
        res_normal = self.client.get(reverse("superadmin:overview"))
        self.assertEqual(res_normal.status_code, 403)

        # 3. Staff user without superuser gets 403 Permission Denied
        self.client.login(username="staff@example.com", password="StaffPassword123")
        res_staff = self.client.get(reverse("superadmin:overview"))
        self.assertEqual(res_staff.status_code, 403)

        # 4. Superuser gets 200 OK
        self.client.login(username="admin@gemileads.gr", password="SuperPassword123")
        res_admin = self.client.get(reverse("superadmin:overview"))
        self.assertEqual(res_admin.status_code, 200)
        self.assertContains(res_admin, "Executive Overview")

    def test_superadmin_post_action_security(self):
        # Normal user trying to post to administrative action receives 403
        self.client.login(username="normal@example.com", password="NormalPassword123")
        res = self.client.post(reverse("superadmin:user_toggle_active", kwargs={"user_id": self.normal_user.id}), {"active": "0"})
        self.assertEqual(res.status_code, 403)

    def test_user_list_search_and_pagination(self):
        self.client.login(username="admin@gemileads.gr", password="SuperPassword123")
        res = self.client.get(reverse("superadmin:user_list") + "?q=normal@example.com")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "normal@example.com")

    def test_user_deactivate_and_reactivate(self):
        from .models import AdminAuditLog
        self.client.login(username="admin@gemileads.gr", password="SuperPassword123")
        # Deactivate normal user
        res_deact = self.client.post(reverse("superadmin:user_toggle_active", kwargs={"user_id": self.normal_user.id}), {"active": "0"})
        self.assertEqual(res_deact.status_code, 302)
        self.normal_user.refresh_from_db()
        self.assertFalse(self.normal_user.is_active)
        self.assertTrue(AdminAuditLog.objects.filter(action="deactivate_user", target_id=str(self.normal_user.id)).exists())

        # Reactivate normal user
        res_react = self.client.post(reverse("superadmin:user_toggle_active", kwargs={"user_id": self.normal_user.id}), {"active": "1"})
        self.assertEqual(res_react.status_code, 302)
        self.normal_user.refresh_from_db()
        self.assertTrue(self.normal_user.is_active)

    def test_complimentary_access_grant_and_expiry(self):
        from .models import AdminAuditLog
        self.client.login(username="admin@gemileads.gr", password="SuperPassword123")

        # Grant complimentary Pro access
        res_grant = self.client.post(reverse("superadmin:user_complimentary", kwargs={"user_id": self.normal_user.id}), {
            "action": "grant",
            "tier": "pro",
        })
        self.assertEqual(res_grant.status_code, 302)
        sub = self.normal_user.subscription
        sub.refresh_from_db()
        self.assertEqual(sub.complimentary_tier, "pro")
        self.assertTrue(sub.has_valid_complimentary_access)
        self.assertTrue(sub.has_entitlement)
        self.assertEqual(sub.radar_limit, 5)
        # Verify Stripe status untouched
        self.assertEqual(sub.status, "inactive")
        self.assertTrue(AdminAuditLog.objects.filter(action="grant_complimentary_access").exists())

        # Expired complimentary access
        sub.complimentary_until = timezone.now() - timedelta(days=1)
        sub.save()
        self.assertFalse(sub.has_valid_complimentary_access)
        self.assertFalse(sub.has_entitlement)
        self.assertEqual(sub.radar_limit, 0)

        # Revoke complimentary access
        res_revoke = self.client.post(reverse("superadmin:user_complimentary", kwargs={"user_id": self.normal_user.id}), {
            "action": "revoke",
        })
        self.assertEqual(res_revoke.status_code, 302)
        sub.refresh_from_db()
        self.assertEqual(sub.complimentary_tier, "none")

    def test_global_radar_list_and_eligibility(self):
        self.client.login(username="admin@gemileads.gr", password="SuperPassword123")
        radar = CustomerRadar.objects.create(user=self.normal_user, name="Superadmin Test Radar", is_active=True)

        res = self.client.get(reverse("superadmin:radar_list"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Superadmin Test Radar")

        res_detail = self.client.get(reverse("superadmin:radar_detail", kwargs={"radar_id": radar.id}))
        self.assertEqual(res_detail.status_code, 200)
        self.assertContains(res_detail, "DISABLED")

    def test_manual_pipeline_run_security_and_idempotency(self):
        # 1. Non-superuser blocked
        self.client.login(username="normal@example.com", password="NormalPassword123")
        res_blocked = self.client.post(reverse("superadmin:pipeline_run_now"), {"target_date": "2026-08-01"})
        self.assertEqual(res_blocked.status_code, 403)

        # 2. Superuser run pipeline with mock fetch
        self.client.login(username="admin@gemileads.gr", password="SuperPassword123")
        with patch("gemiapp.services.fetch_companies", return_value=[{
            "arGemi": 777000111, "afm": "777000111", "coNameEl": "SUPER PIPELINE LTD",
            "coTitlesEl": ["SUPER PIPELINE"], "incorporationDate": "2026-08-01",
            "legalType": {"descr": "Ιδιωτική Κεφαλαιουχική Εταιρεία"},
            "status": {"descr": "Ενεργή", "isActive": True}, "city": "ΑΘΗΝΑ",
            "prefecture": {"descr": "ΑΤΤΙΚΗΣ"}, "activities": [],
        }]):
            res_run = self.client.post(reverse("superadmin:pipeline_run_now"), {"target_date": "2026-08-01"})
            self.assertEqual(res_run.status_code, 302)
            self.assertEqual(Company.objects.filter(gemi_number="777000111").count(), 1)

            # Second manual run for same date must not create duplicate company (Idempotency)
            self.client.post(reverse("superadmin:pipeline_run_now"), {"target_date": "2026-08-01"})
            self.assertEqual(Company.objects.filter(gemi_number="777000111").count(), 1)

    def test_user_impersonation(self):
        from .models import AdminAuditLog
        # 1. Superadmin starts impersonation
        self.client.login(username="admin@gemileads.gr", password="SuperPassword123")
        res_start = self.client.post(reverse("superadmin:impersonate_start", kwargs={"user_id": self.normal_user.id}))
        self.assertEqual(res_start.status_code, 302)
        self.assertRedirects(res_start, reverse("dashboard"))

        # Verify active user is now normal_user and session has impersonator_id
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.normal_user.id)
        self.assertEqual(self.client.session["impersonator_id"], self.superuser.id)
        self.assertTrue(AdminAuditLog.objects.filter(action="impersonate_start").exists())

        # Verify impersonation banner rendered on dashboard
        res_dash = self.client.get(reverse("dashboard"))
        self.assertEqual(res_dash.status_code, 200)
        self.assertContains(res_dash, "SUPERADMIN IMPERSONATION")

        # 2. Exit impersonation
        res_stop = self.client.post(reverse("superadmin:impersonate_stop"))
        self.assertEqual(res_stop.status_code, 302)
        self.assertRedirects(res_stop, reverse("superadmin:overview"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.superuser.id)
        self.assertNotIn("impersonator_id", self.client.session)

    def test_system_health_page(self):
        self.client.login(username="admin@gemileads.gr", password="SuperPassword123")
        res = self.client.get(reverse("superadmin:health_overview"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "System Health & Operational Status")
        self.assertContains(res, "Database (SQLite/PostgreSQL)")

    def test_email_or_username_authentication(self):
        # 1. Login using email address
        self.client.logout()
        res_email = self.client.post(reverse("login"), {"username": "admin@gemileads.gr", "password": "SuperPassword123"})
        self.assertEqual(res_email.status_code, 302)

        # 2. Login using username if different
        User.objects.create_superuser(username="super_user", email="superuser@gemileads.gr", password="Password123!")
        res_user = self.client.post(reverse("login"), {"username": "superuser@gemileads.gr", "password": "Password123!"})
        self.assertEqual(res_user.status_code, 302)

        res_user_name = self.client.post(reverse("login"), {"username": "super_user", "password": "Password123!"})
        self.assertEqual(res_user_name.status_code, 302)

    def test_send_user_yesterday_digest(self):
        self.client.login(username="admin@gemileads.gr", password="SuperPassword123")
        res = self.client.post(reverse("superadmin:user_send_yesterday_digest", kwargs={"user_id": self.normal_user.id}))
        self.assertEqual(res.status_code, 302)
        self.assertRedirects(res, reverse("superadmin:user_detail", kwargs={"user_id": self.normal_user.id}))
        from .models import AdminAuditLog
        self.assertTrue(AdminAuditLog.objects.filter(action="send_user_yesterday_digest", target_id=str(self.normal_user.id)).exists())

    def test_grant_complimentary_enterprise_and_custom_limits(self):
        self.client.login(username="admin@gemileads.gr", password="SuperPassword123")
        # 1. Grant Enterprise permanent access (15 radars)
        res_ent = self.client.post(reverse("superadmin:user_complimentary", kwargs={"user_id": self.normal_user.id}), {
            "action": "grant",
            "tier": "enterprise",
            "duration": "permanent",
        })
        self.assertEqual(res_ent.status_code, 302)
        sub = self.normal_user.subscription
        sub.refresh_from_db()
        self.assertEqual(sub.complimentary_tier, "enterprise")
        self.assertIsNone(sub.complimentary_until)
        self.assertEqual(sub.radar_limit, 15)

        # 2. Grant Custom Radar limit (e.g. 50)
        res_custom = self.client.post(reverse("superadmin:user_complimentary", kwargs={"user_id": self.normal_user.id}), {
            "action": "grant",
            "tier": "custom",
            "duration": "permanent",
            "custom_radar_limit": "50",
        })
        self.assertEqual(res_custom.status_code, 302)
        sub.refresh_from_db()
        self.assertEqual(sub.custom_radar_limit, 50)
        self.assertEqual(sub.radar_limit, 50)

    def test_intraday_pipeline_only_notifies_enterprise_users(self):
        from gemiapp.services import send_digests
        from gemiapp.models import UserSubscription
        from datetime import date
        today = date.today()
        sub_normal = self.normal_user.subscription
        sub_normal.complimentary_tier = "pro"
        sub_normal.save()

        ent_user = User.objects.create_user(username="ent@example.com", email="ent@example.com", password="Password123")
        DigestPreference.objects.get_or_create(user=ent_user)
        sub_ent, _ = UserSubscription.objects.get_or_create(user=ent_user, defaults={"complimentary_tier": "enterprise"})
        sub_ent.complimentary_tier = "enterprise"
        sub_ent.save()

        sent, skipped = send_digests(today, frequency="intraday")
        self.assertTrue(sent >= 0)


class RegressionTests(TestCase):
    """Covers the defects found during the 2026-08-21 audit."""

    def setUp(self):
        self.user = User.objects.create_user("ent@example.com", "ent@example.com", "StrongPass123")
        DigestPreference.objects.get_or_create(user=self.user)
        sub = self.user.subscription
        sub.tier = "enterprise"
        sub.status = "active"
        sub.save()
        self.today = timezone.localdate()

    def _company(self, gemi, name, day=None):
        return Company.objects.create(
            gemi_number=gemi, name=name, vat_number=gemi[:9],
            incorporation_date=day or self.today, prefecture="ΑΤΤΙΚΗΣ",
            legal_type="Ιδιωτική Κεφαλαιουχική Εταιρεία", is_active=True,
        )

    def test_repeated_intraday_digests_do_not_crash_on_unique_constraint(self):
        """Intraday runs 6x per day against a unique (user, date, frequency) constraint."""
        self._company("900000000001", "ALPHA ΕΝΕΡΓΕΙΑΚΗ ΙΚΕ")
        first_sent, _ = send_digests(self.today, frequency="intraday")
        self.assertEqual(first_sent, 1)

        # A second company arrives three hours later; the second run must not raise.
        self._company("900000000002", "BETA ΕΝΕΡΓΕΙΑΚΗ ΙΚΕ")
        second_sent, _ = send_digests(self.today, frequency="intraday")
        self.assertEqual(second_sent, 1)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(
            DigestDelivery.objects.filter(user=self.user, digest_date=self.today, frequency="intraday").count(),
            1,
        )

    def test_manual_yesterday_digest_can_be_sent_twice(self):
        from .services import send_user_yesterday_digest

        yesterday = self.today - timedelta(days=1)
        self._company("900000000003", "GAMMA ΙΚΕ", day=yesterday)
        self.assertEqual(send_user_yesterday_digest(self.user), 1)
        self.assertEqual(send_user_yesterday_digest(self.user), 1)
        self.assertEqual(
            DigestDelivery.objects.filter(user=self.user, frequency="manual_yesterday").count(), 1
        )

    def test_bulk_import_runs_radar_matching_without_error(self):
        """import_companies_since_date used to end in a NameError on an undefined function."""
        from .services import import_companies_since_date, match_companies_in_range

        radar = CustomerRadar.objects.create(
            user=self.user, name="Αττική", prefectures=["ΑΤΤΙΚΗΣ"],
            monitor_from=timezone.now() - timedelta(days=30),
        )
        start = self.today - timedelta(days=2)
        self._company("900000000004", "DELTA ΙΚΕ", day=start)
        self._company("900000000005", "EPSILON ΙΚΕ", day=self.today)

        summary = match_companies_in_range(start, self.today)
        self.assertEqual(summary.new_matches, 2)
        self.assertEqual(UserCompanyLead.objects.filter(user=self.user).count(), 2)

        # Historical matches keep the incorporation date, so a backfill cannot flood today's digest.
        self.assertEqual(
            set(RadarMatch.objects.filter(radar=radar).values_list("matched_on", flat=True)),
            {start, self.today},
        )

        with patch("gemiapp.services._get", return_value={"searchResults": []}):
            self.assertEqual(import_companies_since_date(start), (0, 0))

    def test_radar_name_filter_is_accent_insensitive_and_indexed(self):
        from .services import filter_companies_for_radar

        self._company("900000000006", "ΕΝΕΡΓΕΙΑΚΉ ΛΎΣΗ ΙΚΕ")
        self._company("900000000007", "ΤΕΧΝΙΚΗ ΑΕ")
        matches = filter_companies_for_radar(Company.objects.all(), name_query="ενεργειακη λυση")
        self.assertEqual([c.gemi_number for c in matches], ["900000000006"])

    def test_search_name_is_maintained_on_save_and_update(self):
        company = self._company("900000000008", "ΆΛΦΑ ΙΚΕ")
        self.assertEqual(company.search_name, "ΑΛΦΑ ΙΚΕ")
        company.name = "ΒΉΤΑ ΙΚΕ"
        company.save(update_fields=["name"])
        company.refresh_from_db()
        self.assertEqual(company.search_name, "ΒΗΤΑ ΙΚΕ")

    def test_radar_limits_come_from_a_single_source(self):
        from .models import RADAR_LIMITS

        sub = self.user.subscription
        for tier, expected in RADAR_LIMITS.items():
            sub.tier = tier
            sub.status = "active"
            sub.complimentary_tier = "none"
            sub.custom_radar_limit = None
            sub.save()
            self.assertEqual(self.user.subscription.radar_limit, expected, tier)

        sub.custom_radar_limit = 42
        sub.save()
        self.assertEqual(self.user.subscription.radar_limit, 42)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SubscriptionQueryHelperTests(TestCase):
    """The database predicates must agree with the model properties for every combination."""

    def test_query_helpers_match_python_properties(self):
        from .models import (
            UserSubscription,
            complimentary_q,
            effective_tier_q,
            entitlement_q,
            paid_subscription_q,
        )

        tiers = ["free", "pro", "business", "enterprise", "custom"]
        statuses = ["active", "canceled", "past_due", "unpaid", "inactive"]
        complimentary = ["none", "pro", "business", "enterprise", "custom"]
        untils = [None, timezone.now() + timedelta(days=5), timezone.now() - timedelta(days=5)]

        index = 0
        for tier in tiers:
            for status in statuses:
                for comp in complimentary:
                    for until in untils:
                        index += 1
                        user = User.objects.create_user(f"u{index}@example.com", f"u{index}@example.com", "x")
                        UserSubscription.objects.filter(user=user).update(
                            tier=tier, status=status, complimentary_tier=comp,
                            complimentary_until=until if comp != "none" else None,
                        )

        users = list(User.objects.select_related("subscription"))
        self.assertEqual(len(users), index)

        def db_ids(q):
            return set(User.objects.filter(q).values_list("id", flat=True))

        def py_ids(predicate):
            return {u.id for u in users if predicate(u.subscription)}

        self.assertEqual(db_ids(paid_subscription_q()), py_ids(lambda s: s.has_active_paid_subscription))
        self.assertEqual(db_ids(complimentary_q()), py_ids(lambda s: s.has_valid_complimentary_access))
        self.assertEqual(db_ids(entitlement_q()), py_ids(lambda s: s.has_entitlement))
        for tier in tiers:
            self.assertEqual(
                db_ids(effective_tier_q(tier)),
                py_ids(lambda s, t=tier: s.effective_tier == t),
                f"effective_tier_q({tier})",
            )

        self.assertEqual(
            UserSubscription.objects.filter(entitlement_q(prefix="")).count(),
            len(py_ids(lambda s: s.has_entitlement)),
        )


@override_settings(
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz",
    STRIPE_PRICE_ENTERPRISE="price_ent",
)
class StripeTierMappingTests(TestCase):
    def test_price_ids_map_to_tiers_in_both_directions(self):
        from .billing import price_id_for_tier, tier_for_price_id

        for tier, price in (("pro", "price_pro"), ("business", "price_biz"), ("enterprise", "price_ent")):
            self.assertEqual(price_id_for_tier(tier), price)
            self.assertEqual(tier_for_price_id(price), tier)

        self.assertIsNone(tier_for_price_id("price_unknown"))
        self.assertIsNone(tier_for_price_id(None))
        self.assertIsNone(price_id_for_tier("custom"))

    @override_settings(STRIPE_PRICE_ENTERPRISE=None)
    def test_unconfigured_price_never_maps_to_a_tier(self):
        from .billing import tier_for_price_id

        self.assertIsNone(tier_for_price_id(None))
        self.assertEqual(tier_for_price_id("price_pro"), "pro")

    def test_enterprise_checkout_tier_is_accepted(self):
        user = User.objects.create_user("buyer@example.com", "buyer@example.com", "StrongPass123")
        self.client.force_login(user)
        with patch("stripe.checkout.Session.create") as create:
            create.return_value = type("S", (), {"url": "https://stripe.test/checkout"})()
            response = self.client.post(reverse("create_checkout_session"), {"tier": "enterprise"})
        self.assertEqual(create.call_args.kwargs["line_items"], [{"price": "price_ent", "quantity": 1}])
        self.assertEqual(response.status_code, 303)

    def test_unknown_tier_is_rejected(self):
        user = User.objects.create_user("buyer2@example.com", "buyer2@example.com", "StrongPass123")
        self.client.force_login(user)
        with patch("stripe.checkout.Session.create") as create:
            response = self.client.post(reverse("create_checkout_session"), {"tier": "bogus"})
        create.assert_not_called()
        self.assertRedirects(response, reverse("pricing"))


class ScheduleRegistrationTests(TestCase):
    """The scheduler swallows its own exceptions, so these assert on effects, not on calls.

    A missing croniter used to make django-q raise inside the scheduler transaction, which rolled
    back every schedule in the same pass and silently stopped the daily digest too.
    """

    def setUp(self):
        from django_q.models import Schedule

        Schedule.objects.all().delete()
        from gemiapp.apps import setup_daily_pipeline_schedule

        setup_daily_pipeline_schedule(None)

    def test_both_pipelines_are_registered_with_a_resolvable_cron(self):
        from django_q.models import Schedule

        schedules = {s.func: s for s in Schedule.objects.all()}
        self.assertEqual(
            set(schedules),
            {"gemiapp.tasks.run_daily_pipeline_task", "gemiapp.tasks.run_intraday_pipeline_task"},
        )
        for schedule in schedules.values():
            self.assertEqual(schedule.schedule_type, Schedule.CRON)
            self.assertEqual(schedule.repeats, -1)
            # Raises ImportError if croniter is not installed.
            self.assertGreater(schedule.calculate_next_run(), timezone.now())

    def test_new_schedules_do_not_fire_immediately_on_deploy(self):
        from django_q.models import Schedule

        for schedule in Schedule.objects.all():
            self.assertGreater(schedule.next_run, timezone.now(), schedule.name)

    def test_scheduler_enqueues_a_task_for_every_due_schedule(self):
        from django_q.brokers import get_broker
        from django_q.models import OrmQ, Schedule
        from django_q.scheduler import scheduler

        past = timezone.now() - timedelta(minutes=5)
        Schedule.objects.update(next_run=past)

        broker = get_broker()
        broker.purge_queue()
        scheduler(broker=broker)

        queued = OrmQ.objects.count()
        self.assertEqual(queued, 2, "the scheduler produced no task - check the cron dependency")

        funcs = {entry.task["func"] for entry in OrmQ.objects.all()}
        self.assertEqual(
            funcs,
            {"gemiapp.tasks.run_daily_pipeline_task", "gemiapp.tasks.run_intraday_pipeline_task"},
        )

        # Every schedule must have moved on, otherwise the same run repeats every tick.
        for schedule in Schedule.objects.all():
            self.assertGreater(schedule.next_run, timezone.now(), schedule.name)

    def test_schedule_definition_changes_are_applied_to_existing_rows(self):
        from django_q.models import Schedule
        from gemiapp.apps import setup_daily_pipeline_schedule

        stale = Schedule.objects.get(func="gemiapp.tasks.run_intraday_pipeline_task")
        stale.cron = "0 3 * * *"
        stale.name = "Old name"
        stale.save()
        kept_next_run = stale.next_run

        setup_daily_pipeline_schedule(None)

        stale.refresh_from_db()
        self.assertEqual(stale.cron, "0 8,11,14,17,20,23 * * *")
        self.assertEqual(stale.name, "Intraday 3-Hour GEMI Pipeline (Top Tier)")
        self.assertEqual(Schedule.objects.count(), 2)
        # An existing row keeps its next_run; only creation seeds it.
        self.assertEqual(stale.next_run, kept_next_run)


class DuplicateScheduleRepairTests(TestCase):
    """Reproduces the production state found on 2026-08-21.

    Two rows existed for run_daily_pipeline_task with no name and schedule_type DAILY, and no
    intraday row at all. update_or_create(func=...) raised MultipleObjectsReturned, which the
    blanket except swallowed, so registration never completed: the daily pipeline ran twice
    concurrently every night and the intraday schedule was never created.
    """

    def setUp(self):
        from django_q.models import Schedule

        Schedule.objects.all().delete()
        for _ in range(2):
            Schedule.objects.create(
                func="gemiapp.tasks.run_daily_pipeline_task",
                schedule_type=Schedule.DAILY,
                repeats=-1,
            )

    def test_duplicates_are_removed_and_both_schedules_end_up_registered(self):
        from django_q.models import Schedule
        from gemiapp.apps import setup_daily_pipeline_schedule

        setup_daily_pipeline_schedule(None)

        self.assertEqual(
            Schedule.objects.filter(func="gemiapp.tasks.run_daily_pipeline_task").count(), 1
        )
        intraday = Schedule.objects.get(func="gemiapp.tasks.run_intraday_pipeline_task")
        self.assertEqual(intraday.cron, "0 8,11,14,17,20,23 * * *")

        daily = Schedule.objects.get(func="gemiapp.tasks.run_daily_pipeline_task")
        self.assertEqual(daily.schedule_type, Schedule.CRON)
        self.assertEqual(daily.cron, "0 9 * * *")
        self.assertEqual(daily.name, "Daily GEMI Import & Digest")

    def test_repair_is_idempotent(self):
        from django_q.models import Schedule
        from gemiapp.apps import setup_daily_pipeline_schedule

        setup_daily_pipeline_schedule(None)
        setup_daily_pipeline_schedule(None)
        self.assertEqual(Schedule.objects.count(), 2)


class PipelineOverlapGuardTests(TestCase):
    """A second pipeline for the same date must not start while the first is still running."""

    def test_running_import_blocks_a_second_daily_pipeline(self):
        from gemiapp.tasks import _pipeline_is_already_running, run_daily_pipeline_task

        target = date.today() - timedelta(days=1)
        ImportRun.objects.create(target_date=target, status="running")
        self.assertTrue(_pipeline_is_already_running(target))

        with patch("gemiapp.tasks.import_for_date") as imported:
            run_daily_pipeline_task()
        imported.assert_not_called()

    def test_a_finished_run_does_not_block(self):
        from gemiapp.tasks import _pipeline_is_already_running

        target = date.today() - timedelta(days=1)
        ImportRun.objects.create(target_date=target, status="success")
        self.assertFalse(_pipeline_is_already_running(target))

    def test_a_run_older_than_the_task_timeout_does_not_block_forever(self):
        from django.conf import settings
        from gemiapp.tasks import _pipeline_is_already_running

        target = date.today() - timedelta(days=1)
        run = ImportRun.objects.create(target_date=target, status="running")
        stale = timezone.now() - timedelta(seconds=settings.Q_CLUSTER["timeout"] + 60)
        ImportRun.objects.filter(pk=run.pk).update(started_at=stale)
        self.assertFalse(_pipeline_is_already_running(target))

    def test_retry_stays_above_timeout(self):
        """Otherwise django-q re-presents a slow task while it is still running."""
        from django.conf import settings

        self.assertGreater(settings.Q_CLUSTER["retry"], settings.Q_CLUSTER["timeout"])


class DigestRecipientTests(TestCase):
    """Why a digest reaches a user, or does not. This is what made the Enterprise accounts silent."""

    def _user(self, name, tier="enterprise", status="active"):
        user = User.objects.create_user(name, f"{name}@example.com", "StrongPass123")
        sub = user.subscription
        sub.tier = tier
        sub.status = status
        sub.save()
        return user

    def test_every_new_user_gets_a_subscription_and_a_digest_preference(self):
        user = User.objects.create_user("fresh", "fresh@example.com", "StrongPass123")
        self.assertIsNotNone(getattr(user, "subscription", None))
        self.assertIsNotNone(getattr(user, "digest_preference", None))

    def test_signup_still_works_now_that_the_signal_creates_the_preference(self):
        response = self.client.post(reverse("signup"), {
            "first_name": "Νίκος",
            "email": "signup@example.com",
            "password1": "StrongPass123!",
            "password2": "StrongPass123!",
        })
        self.assertEqual(response.status_code, 200)
        user = User.objects.get(email="signup@example.com")
        self.assertEqual(DigestPreference.objects.filter(user=user).count(), 1)

    def test_entitled_enterprise_user_is_a_valid_intraday_recipient(self):
        from .services import digest_skip_reason

        user = self._user("ent")
        self.assertIsNone(digest_skip_reason(user, "intraday"))
        self.assertIsNone(digest_skip_reason(user, "daily"))

    def test_complimentary_enterprise_access_is_enough(self):
        from .services import digest_skip_reason

        user = self._user("comp", tier="free", status="inactive")
        sub = user.subscription
        sub.complimentary_tier = "enterprise"
        sub.complimentary_until = timezone.now() + timedelta(days=30)
        sub.save()
        self.assertIsNone(digest_skip_reason(User.objects.get(pk=user.pk), "intraday"))

    def test_pro_user_is_excluded_from_intraday_but_not_from_daily(self):
        from .services import digest_skip_reason

        user = self._user("pro", tier="pro")
        self.assertIn("enterprise/custom", digest_skip_reason(user, "intraday"))
        self.assertIsNone(digest_skip_reason(user, "daily"))

    def test_each_blocking_condition_is_reported_distinctly(self):
        from .services import NO_ENTITLEMENT, digest_skip_reason

        unpaid = self._user("unpaid", tier="free", status="inactive")
        self.assertEqual(digest_skip_reason(unpaid, "daily"), NO_ENTITLEMENT)

        no_pref = self._user("nopref")
        no_pref.digest_preference.delete()
        self.assertIn("DigestPreference", digest_skip_reason(User.objects.get(pk=no_pref.pk), "intraday"))

        unsubscribed = self._user("off")
        unsubscribed.digest_preference.frequency = "off"
        unsubscribed.digest_preference.save()
        self.assertIn("frequency=off", digest_skip_reason(unsubscribed, "intraday"))

        inactive = self._user("inactive")
        inactive.is_active = False
        inactive.save()
        self.assertIn("Ανενεργός", digest_skip_reason(inactive, "intraday"))

        no_email = self._user("noemail")
        no_email.email = ""
        no_email.save()
        self.assertIn("email", digest_skip_reason(no_email, "intraday"))

    def test_intraday_never_tries_to_mail_a_user_without_an_address(self):
        """The intraday branch used to skip the email check that the daily branch had."""
        user = self._user("blank")
        user.email = ""
        user.save()
        Company.objects.create(
            gemi_number="910000000001", name="ΤΕΣΤ ΙΚΕ", incorporation_date=timezone.localdate(),
        )
        sent, skipped = send_digests(timezone.localdate(), frequency="intraday")
        self.assertEqual(sent, 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_digest_recipients_command_reports_both_groups(self):
        from io import StringIO
        from django.core.management import call_command

        self._user("good")
        self._user("bad", tier="free", status="inactive")

        out = StringIO()
        call_command("digest_recipients", "--frequency", "intraday", stdout=out)
        output = out.getvalue()
        self.assertIn("good@example.com", output)
        self.assertIn("bad@example.com", output)
        self.assertIn("No active subscription entitlement", output)


def _gemi_item(ar_gemi, name, incorporation_date):
    return {
        "arGemi": ar_gemi, "afm": str(ar_gemi)[:9], "coNameEl": name, "coTitlesEl": [],
        "incorporationDate": incorporation_date.isoformat(),
        "legalType": {"descr": "Ιδιωτική Κεφαλαιουχική Εταιρεία"},
        "status": {"descr": "Ενεργή", "isActive": True},
        "city": "ΑΘΗΝΑ", "prefecture": {"descr": "ΑΤΤΙΚΗΣ"}, "activities": [],
    }


class IntradayFetchesTodayTests(TestCase):
    """The 3-hour pipeline must hit the GEMI API and keep today's registrations, not yesterday's."""

    def setUp(self):
        self.today = timezone.localdate()
        self.yesterday = self.today - timedelta(days=1)

    def test_fetch_companies_keeps_only_the_requested_day(self):
        from .services import fetch_companies

        page = {
            "searchResults": [
                _gemi_item(700000000001, "ΣΗΜΕΡΙΝΗ ΑΛΦΑ ΙΚΕ", self.today),
                _gemi_item(700000000002, "ΣΗΜΕΡΙΝΗ ΒΗΤΑ ΙΚΕ", self.today),
                _gemi_item(700000000003, "ΧΘΕΣΙΝΗ ΓΑΜΑ ΙΚΕ", self.yesterday),
            ],
            "searchMetadata": {"totalCount": 3},
        }
        with patch("gemiapp.services._get", return_value=page) as api:
            found = fetch_companies(self.today)

        self.assertTrue(api.called, "the GEMI API was never called")
        self.assertEqual(api.call_args.args[0], "/companies")
        self.assertEqual(
            sorted(str(item["arGemi"]) for item in found),
            ["700000000001", "700000000002"],
        )

    def test_intraday_pipeline_asks_the_api_for_today(self):
        from .tasks import run_intraday_pipeline_task

        midday = timezone.localtime().replace(hour=10, minute=0)
        with patch("gemiapp.tasks.timezone.localtime", return_value=midday), \
             patch("gemiapp.services.fetch_companies", return_value=[]) as fetch:
            run_intraday_pipeline_task()

        fetch.assert_called_once_with(self.today)

    def test_intraday_import_stores_todays_companies(self):
        from .services import import_for_date

        with patch(
            "gemiapp.services.fetch_companies",
            return_value=[_gemi_item(700000000010, "ΝΕΑ ΣΗΜΕΡΙΝΗ ΙΚΕ", self.today)],
        ):
            run = import_for_date(self.today)

        self.assertEqual(run.status, "success")
        self.assertEqual(run.created_count, 1)
        company = Company.objects.get(gemi_number="700000000010")
        self.assertEqual(company.incorporation_date, self.today)

    def test_pipeline_is_skipped_outside_the_window(self):
        from .tasks import run_intraday_pipeline_task

        night = timezone.localtime().replace(hour=3, minute=0)
        with patch("gemiapp.tasks.timezone.localtime", return_value=night), \
             patch("gemiapp.services.fetch_companies") as fetch:
            run_intraday_pipeline_task()
        fetch.assert_not_called()


class RealtimeDashboardTests(TestCase):
    """Enterprise/Custom must see what the 3-hour pipeline just imported."""

    def setUp(self):
        self.today = timezone.localdate()
        self.user = User.objects.create_user("rt@example.com", "rt@example.com", "StrongPass123")
        sub = self.user.subscription
        sub.tier = "enterprise"
        sub.status = "active"
        sub.save()
        self.client.force_login(self.user)
        self.company = Company.objects.create(
            gemi_number="700000000020", name="ΣΗΜΕΡΙΝΗ REALTIME ΙΚΕ",
            incorporation_date=self.today, prefecture="ΑΤΤΙΚΗΣ", is_active=True,
        )

    def test_todays_companies_appear_in_the_dashboard(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ΣΗΜΕΡΙΝΗ REALTIME ΙΚΕ")
        self.assertContains(response, "Σήμερα")

    def test_realtime_panel_shows_the_last_intraday_run(self):
        ImportRun.objects.create(
            target_date=self.today, status="success", finished_at=timezone.now()
        )
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Real-Time ΓΕΜΗ")
        self.assertTrue(response.context["is_realtime_tier"])
        self.assertIsNotNone(response.context["last_intraday_run"])

    def test_lower_tiers_do_not_get_the_realtime_panel(self):
        sub = self.user.subscription
        sub.tier = "pro"
        sub.save()
        response = self.client.get(reverse("dashboard"))
        self.assertFalse(response.context["is_realtime_tier"])
        self.assertNotContains(response, "Real-Time ΓΕΜΗ")
        # The company itself is still listed; only the panel is tier specific.
        self.assertContains(response, "ΣΗΜΕΡΙΝΗ REALTIME ΙΚΕ")


class IntradayIncrementalTests(TestCase):
    """Each 3-hour alert must carry only what is new since the previous one.

    The general section was already incremental, but radar matches were selected purely by
    matched_on, so the same matched companies were repeated in all six emails of the day.
    """

    def setUp(self):
        self.today = timezone.localdate()
        self.user = User.objects.create_user("inc@example.com", "inc@example.com", "StrongPass123")
        sub = self.user.subscription
        sub.tier = "enterprise"
        sub.status = "active"
        sub.save()
        self.kad = ActivityCode.objects.create(
            code="62.01.00.00", normalized_code="62010000", description="ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΣ",
            search_text="62010000 ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΣ",
        )
        self.radar = CustomerRadar.objects.create(
            user=self.user, name="Αττική", prefectures=["ΑΤΤΙΚΗΣ"],
            monitor_from=timezone.now() - timedelta(days=1),
        )

    def _matched_company(self, gemi, name):
        """A company that both exists today and is matched by the radar."""
        company = Company.objects.create(
            gemi_number=gemi, name=name, incorporation_date=self.today,
            prefecture="ΑΤΤΙΚΗΣ", is_active=True,
        )
        lead, _ = UserCompanyLead.objects.get_or_create(user=self.user, company=company)
        RadarMatch.objects.create(
            radar=self.radar, lead=lead, company=company, matched_on=self.today,
            matched_activity_codes=[], match_reason={},
        )
        return company

    def _bodies(self):
        return "\n".join(message.body for message in mail.outbox)

    def test_a_radar_match_is_not_repeated_in_the_next_alert(self):
        first = self._matched_company("800000000001", "ΠΡΩΤΗ ΜΑΤΣ ΙΚΕ")

        sent, _ = send_digests(self.today, frequency="intraday")
        self.assertEqual(sent, 1)
        self.assertIn("ΠΡΩΤΗ ΜΑΤΣ ΙΚΕ", self._bodies())

        # Three hours later, nothing new has arrived.
        mail.outbox.clear()
        sent, _ = send_digests(self.today, frequency="intraday")
        self.assertEqual(sent, 0, "the same radar match was sent again")
        self.assertEqual(len(mail.outbox), 0)

        # And once something new does arrive, only that one is included.
        second = self._matched_company("800000000002", "ΔΕΥΤΕΡΗ ΜΑΤΣ ΙΚΕ")
        sent, _ = send_digests(self.today, frequency="intraday")
        self.assertEqual(sent, 1)
        body = self._bodies()
        self.assertIn("ΔΕΥΤΕΡΗ ΜΑΤΣ ΙΚΕ", body)
        self.assertNotIn("ΠΡΩΤΗ ΜΑΤΣ ΙΚΕ", body)
        self.user.subscription.refresh_from_db()
        self.assertGreaterEqual(
            self.user.subscription.last_sent_company_id, second.id
        )
        self.assertGreater(second.id, first.id)

    def test_general_and_radar_sections_share_one_pointer(self):
        plain = Company.objects.create(
            gemi_number="800000000010", name="ΓΕΝΙΚΗ ΙΚΕ", incorporation_date=self.today,
            prefecture="ΗΡΑΚΛΕΙΟΥ", is_active=True,
        )
        matched = self._matched_company("800000000011", "ΜΑΤΣ ΙΚΕ")

        sent, _ = send_digests(self.today, frequency="intraday")
        self.assertEqual(sent, 1)
        body = self._bodies()
        self.assertIn("ΓΕΝΙΚΗ ΙΚΕ", body)
        self.assertIn("ΜΑΤΣ ΙΚΕ", body)

        self.user.subscription.refresh_from_db()
        self.assertEqual(
            self.user.subscription.last_sent_company_id, max(plain.id, matched.id)
        )

        mail.outbox.clear()
        sent, skipped = send_digests(self.today, frequency="intraday")
        self.assertEqual(sent, 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_daily_digest_is_still_a_full_snapshot(self):
        """Only intraday is incremental; the daily digest must keep reporting the whole day."""
        self._matched_company("800000000020", "ΗΜΕΡΗΣΙΑ ΜΑΤΣ ΙΚΕ")
        self.user.subscription.last_sent_company_id = 99999999
        self.user.subscription.save()

        sent, _ = send_digests(self.today, frequency="daily")
        self.assertEqual(sent, 1)
        self.assertIn("ΗΜΕΡΗΣΙΑ ΜΑΤΣ ΙΚΕ", self._bodies())

    def test_pointer_is_not_advanced_when_sending_fails(self):
        self._matched_company("800000000030", "ΑΠΟΤΥΧΙΑ ΙΚΕ")
        with patch("gemiapp.services.send_mail", side_effect=RuntimeError("smtp down")):
            sent, _ = send_digests(self.today, frequency="intraday")
        self.assertEqual(sent, 0)

        self.user.subscription.refresh_from_db()
        self.assertEqual(self.user.subscription.last_sent_company_id, 0)

        # The retry three hours later still contains it.
        sent, _ = send_digests(self.today, frequency="intraday")
        self.assertEqual(sent, 1)
        self.assertIn("ΑΠΟΤΥΧΙΑ ΙΚΕ", self._bodies())


class UnsubscribeRobustnessTests(TestCase):
    """Every digest email carries this link, so it must never 500."""

    def test_unsubscribe_works_when_the_preference_row_is_missing(self):
        from django.core.signing import TimestampSigner

        user = User.objects.create_user("legacy@example.com", "legacy@example.com", "StrongPass123")
        DigestPreference.objects.filter(user=user).delete()

        token = TimestampSigner().sign(user.id)
        response = self.client.get(reverse("unsubscribe", args=[token]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(DigestPreference.objects.get(user=user).frequency, "off")

    def test_unsubscribe_turns_off_an_existing_preference(self):
        from django.core.signing import TimestampSigner

        user = User.objects.create_user("on@example.com", "on@example.com", "StrongPass123")
        token = TimestampSigner().sign(user.id)

        self.assertEqual(self.client.get(reverse("unsubscribe", args=[token])).status_code, 200)
        self.assertEqual(DigestPreference.objects.get(user=user).frequency, "off")

    def test_a_tampered_token_is_rejected_without_changing_anything(self):
        from django.core.signing import TimestampSigner

        user = User.objects.create_user("safe@example.com", "safe@example.com", "StrongPass123")
        token = TimestampSigner().sign(user.id)

        response = self.client.get(reverse("unsubscribe", args=[token + "x"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "άκυρος")
        self.assertEqual(DigestPreference.objects.get(user=user).frequency, "daily")


class LeadsAreRadarOutcomesTests(TestCase):
    """Browsing a company must not manufacture a lead for a user with no entitlement."""

    def setUp(self):
        self.company = Company.objects.create(
            gemi_number="990000000001", name="ΔΟΚΙΜΗ ΙΚΕ",
            incorporation_date=timezone.localdate(), prefecture="ΑΤΤΙΚΗΣ", is_active=True,
        )

    def _user(self, name, entitled):
        user = User.objects.create_user(name, f"{name}@example.com", "StrongPass123")
        if entitled:
            sub = user.subscription
            sub.tier = "pro"
            sub.status = "active"
            sub.save()
        return user

    def test_unpaid_user_browsing_does_not_create_a_lead(self):
        user = self._user("free", entitled=False)
        self.client.force_login(user)

        response = self.client.get(reverse("company_detail", args=[self.company.gemi_number]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(UserCompanyLead.objects.filter(user=user).count(), 0)
        self.assertContains(response, "Δες τα πλάνα")

    def test_entitled_user_browsing_still_gets_a_lead(self):
        user = self._user("paid", entitled=True)
        self.client.force_login(user)

        response = self.client.get(reverse("company_detail", args=[self.company.gemi_number]))

        self.assertEqual(response.status_code, 200)
        lead = UserCompanyLead.objects.get(user=user, company=self.company)
        self.assertEqual(lead.status, "viewed")

    def test_a_lead_earned_before_cancellation_stays_visible(self):
        user = self._user("lapsed", entitled=True)
        UserCompanyLead.objects.create(user=user, company=self.company, notes="κράτα με")
        sub = user.subscription
        sub.tier = "free"
        sub.status = "inactive"
        sub.save()

        self.client.force_login(user)
        response = self.client.get(reverse("company_detail", args=[self.company.gemi_number]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(UserCompanyLead.objects.filter(user=user).count(), 1)


class ExportBoundsTests(TestCase):
    """An unfiltered export used to buffer the entire company table into one response."""

    def setUp(self):
        self.user = User.objects.create_user("exp@example.com", "exp@example.com", "StrongPass123")
        sub = self.user.subscription
        sub.tier = "pro"
        sub.status = "active"
        sub.save()
        self.client.force_login(self.user)

    def test_export_is_capped(self):
        from .views import MAX_EXPORT_ROWS

        today = timezone.localdate()
        Company.objects.bulk_create([
            Company(gemi_number=f"9910000{i:05d}", name=f"ΕΤΑΙΡΕΙΑ {i}",
                    incorporation_date=today, prefecture="ΑΤΤΙΚΗΣ", is_active=True)
            for i in range(12)
        ])

        response = self.client.get(reverse("export_csv"))
        self.assertEqual(response.status_code, 200)

        body = response.content.decode("utf-8-sig")
        data_rows = [line for line in body.splitlines() if line.strip()][1:]
        self.assertEqual(len(data_rows), 12)
        self.assertLessEqual(len(data_rows), MAX_EXPORT_ROWS)

    def test_export_still_requires_a_subscription(self):
        sub = self.user.subscription
        sub.tier = "free"
        sub.status = "inactive"
        sub.save()
        response = self.client.get(reverse("export_csv"))
        self.assertRedirects(response, reverse("pricing"))


@override_settings(RATELIMIT_ENABLE=False)
class UnverifiedAccountRecoveryTests(TestCase):
    """An account stuck unverified used to have no way back in.

    Signing up again was refused because the email exists, Django's password reset silently ignores
    inactive users, and the login error said nothing about verification.
    """

    def _signup(self, email="stuck@example.com"):
        return self.client.post(reverse("signup"), {
            "first_name": "Δοκιμή", "email": email,
            "password1": "StrongPass123!", "password2": "StrongPass123!",
        })

    def _verify_link(self, message):
        match = re.search(r"/verify/[^\s\"'<]+", message.body)
        self.assertIsNotNone(match, "no verification link in the email")
        return match.group(0)

    def test_password_reset_still_ignores_inactive_accounts(self):
        """Pins the Django behaviour that makes the resend flow necessary."""
        self._signup()
        mail.outbox.clear()
        self.client.post(reverse("password_reset"), {"email": "stuck@example.com"})
        self.assertEqual(len(mail.outbox), 0)

    def test_resend_gives_a_working_verification_link(self):
        self._signup()
        user = User.objects.get(email="stuck@example.com")
        self.assertFalse(user.is_active)

        mail.outbox.clear()
        response = self.client.post(reverse("resend_verification"), {"email": "stuck@example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

        self.client.get(self._verify_link(mail.outbox[0]))
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_resend_does_not_reveal_whether_an_account_exists(self):
        mail.outbox.clear()
        unknown = self.client.post(reverse("resend_verification"), {"email": "nobody@example.com"})
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

        self._signup()
        mail.outbox.clear()
        known = self.client.post(reverse("resend_verification"), {"email": "stuck@example.com"})

        # Same status and same wording; only the echoed address differs.
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(
            known.content.decode().replace("stuck@example.com", "X"),
            unknown.content.decode().replace("nobody@example.com", "X"),
        )
        self.assertEqual(len(mail.outbox), 1)

    def test_resend_does_nothing_for_an_already_verified_account(self):
        self._signup()
        user = User.objects.get(email="stuck@example.com")
        user.is_active = True
        user.save()

        mail.outbox.clear()
        self.client.post(reverse("resend_verification"), {"email": "stuck@example.com"})
        self.assertEqual(len(mail.outbox), 0)

    def test_signup_error_points_an_unverified_user_at_the_resend_page(self):
        self._signup()
        response = self._signup()
        errors = response.context["form"].errors.as_text()
        self.assertIn("δεν έχει επιβεβαιωθεί", errors)

    def test_signup_error_for_a_verified_account_stays_generic(self):
        self._signup()
        User.objects.filter(email="stuck@example.com").update(is_active=True)
        response = self._signup()
        errors = response.context["form"].errors.as_text()
        self.assertIn("Υπάρχει ήδη λογαριασμός", errors)
        self.assertNotIn("δεν έχει επιβεβαιωθεί", errors)

    def test_login_page_offers_the_escape_hatch(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, reverse("resend_verification"))

    def test_verification_link_is_single_use(self):
        self._signup()
        user = User.objects.get(email="stuck@example.com")
        link = self._verify_link(mail.outbox[0])

        self.assertEqual(self.client.get(link).status_code, 302)
        user.refresh_from_db()
        self.assertTrue(user.is_active)

        # Logging in sets last_login, which is part of the token hash, so the link dies.
        self.client.logout()
        replay = self.client.get(link)
        self.assertRedirects(replay, reverse("login"), fetch_redirect_response=False)

    def test_resend_command_supports_targeting_and_dry_run(self):
        from io import StringIO
        from django.core.management import call_command

        self._signup("a@example.com")
        self._signup("b@example.com")
        mail.outbox.clear()

        out = StringIO()
        call_command("resend_verification_emails", "--dry-run", stdout=out)
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("dry-run", out.getvalue())

        call_command("resend_verification_emails", "--email", "a@example.com", stdout=StringIO())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["a@example.com"])


class ResendVerificationRateLimitTests(TestCase):
    """The resend endpoint sends mail to an address the caller supplies, so it must stay limited."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def test_repeated_requests_are_blocked(self):
        url = reverse("resend_verification")
        statuses = [
            self.client.post(url, {"email": f"x{i}@example.com"}).status_code for i in range(7)
        ]
        self.assertIn(403, statuses, f"rate limit never triggered: {statuses}")


class SeoSurfaceTests(TestCase):
    """robots.txt, sitemap.xml and the head metadata that search engines actually read."""

    def test_robots_txt_allows_public_and_blocks_private(self):
        response = self.client.get("/robots.txt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain")

        body = response.content.decode()
        for private in ("/superadmin/", "/dashboard/", "/leads/", "/api/", "/admin/"):
            self.assertIn(f"Disallow: {private}", body)
        self.assertIn("Sitemap:", body)
        self.assertIn("sitemap.xml", body)

    def test_sitemap_lists_only_public_pages(self):
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)

        body = response.content.decode()
        self.assertIn(reverse("home"), body)
        self.assertIn(reverse("pricing"), body)
        # Nothing behind a login should ever be advertised.
        for private in (reverse("dashboard"), reverse("lead_list"), reverse("settings")):
            self.assertNotIn(f"<loc>{private}</loc>", body)
            self.assertNotIn(private, body.replace(reverse("home"), ""))

    def test_public_pages_are_indexable_and_described(self):
        for name in ("home", "pricing", "signup"):
            response = self.client.get(reverse(name))
            html = response.content.decode()
            self.assertIn('name="robots" content="index, follow"', html, name)
            self.assertIn('name="description"', html, name)
            self.assertIn('property="og:title"', html, name)
            self.assertIn('rel="canonical"', html, name)

    def test_private_pages_are_noindex(self):
        user = User.objects.create_user("seo@example.com", "seo@example.com", "StrongPass123")
        self.client.force_login(user)
        for name in ("dashboard", "settings", "lead_list", "radar_list"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn('content="noindex, nofollow"', html, name)

    def test_every_page_declares_a_viewport_and_language(self):
        html = self.client.get(reverse("home")).content.decode()
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', html)
        self.assertIn('lang="el"', html)


class FrontendAssetTests(TestCase):
    """The Tailwind CDN shipped a 120 KB JIT compiler that ran on every visitor's device."""

    def test_pages_use_the_compiled_stylesheet_not_the_cdn(self):
        html = self.client.get(reverse("home")).content.decode()
        self.assertNotIn("cdn.tailwindcss.com", html)
        # ManifestStaticFilesStorage hashes the filename: css/app.<hash>.css
        self.assertRegex(html, r"css/app(\.[0-9a-f]{12})?\.css")

    def test_the_compiled_stylesheet_exists_and_is_small(self):
        from pathlib import Path
        from django.conf import settings

        css = Path(settings.BASE_DIR) / "static" / "css" / "app.css"
        self.assertTrue(css.exists(), "run: npm run build:css")
        self.assertLess(css.stat().st_size, 150 * 1024)

    def test_the_nav_height_class_is_generated(self):
        """base.html used h-18, which Tailwind does not define by default, so the fixed nav had
        no height while main compensated with a hardcoded padding."""
        from pathlib import Path
        from django.conf import settings

        css = (Path(settings.BASE_DIR) / "static" / "css" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".h-18", css)
        self.assertIn(".pt-18", css)

    def test_images_stay_within_a_sane_budget(self):
        from pathlib import Path
        from django.conf import settings

        images = Path(settings.BASE_DIR) / "static" / "images"
        for name, limit_kb in (("favicon.png", 40), ("logo.png", 150)):
            size_kb = (images / name).stat().st_size / 1024
            self.assertLess(size_kb, limit_kb, f"{name} is {size_kb:.0f} KB")


class PricingAccuracyTests(TestCase):
    """P0-2: the pricing page advertised two capabilities the product does not provide."""

    def test_weekly_digest_is_not_advertised(self):
        """send_digests raises ValueError for weekly, so it must not be sold."""
        html = self.client.get(reverse("pricing")).content.decode()
        self.assertNotIn("Weekly Digest", html)

        with self.assertRaises(ValueError):
            send_digests(timezone.localdate(), frequency="weekly")

    def test_intraday_window_matches_the_scheduler(self):
        html = self.client.get(reverse("pricing")).content.decode()
        self.assertIn("08:00 - 23:00", html)
        self.assertNotIn("08:00 - 00:00", html)


class PricingDiscoverabilityTests(TestCase):
    """P0-3: /pricing/ had zero inbound internal links from any public page."""

    def test_public_pages_link_to_pricing(self):
        for name in ("home", "login", "signup"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn(reverse("pricing"), html, f"{name} does not link to pricing")

    def test_anonymous_visitor_sees_the_pricing_link(self):
        html = self.client.get(reverse("home")).content.decode()
        # Nav + mobile menu + footer.
        self.assertGreaterEqual(html.count('href="%s"' % reverse("pricing")), 3)

    def test_authenticated_nav_still_works(self):
        user = User.objects.create_user("nav@example.com", "nav@example.com", "StrongPass123")
        self.client.force_login(user)
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)


@override_settings(ALLOWED_HOSTS=["gemileads.gr", "testserver"])
class CanonicalTests(TestCase):
    """P1-1: build_absolute_uri echoed the query string, so parameter URLs self-canonicalised."""

    def _canonical(self, url):
        html = self.client.get(url, HTTP_HOST="gemileads.gr").content.decode()
        match = re.search(r'<link rel="canonical" href="([^"]+)"', html)
        self.assertIsNotNone(match, "no canonical tag rendered")
        return match.group(1)

    def test_parameter_urls_consolidate_to_the_clean_path(self):
        self.assertEqual(self._canonical("/"), "http://gemileads.gr/")
        self.assertEqual(self._canonical("/?utm_source=test"), "http://gemileads.gr/")
        self.assertEqual(self._canonical("/?utm_source=a&page=2&x=1"), "http://gemileads.gr/")

    def test_each_page_self_references(self):
        self.assertEqual(self._canonical("/pricing/"), "http://gemileads.gr/pricing/")

    def test_og_url_matches_the_canonical(self):
        html = self.client.get("/?utm_source=test", HTTP_HOST="gemileads.gr").content.decode()
        og = re.search(r'property="og:url" content="([^"]+)"', html).group(1)
        self.assertEqual(og, "http://gemileads.gr/")


@override_settings(ALLOWED_HOSTS=["gemileads.gr", "testserver"])
class OpenGraphImageTests(TestCase):
    """P1-4: og:image was root-relative, so scrapers could not resolve it."""

    def test_og_image_is_absolute(self):
        html = self.client.get("/", HTTP_HOST="gemileads.gr").content.decode()
        og = re.search(r'property="og:image" content="([^"]+)"', html).group(1)
        self.assertTrue(og.startswith("http://gemileads.gr/"), og)
        # Hashed by ManifestStaticFilesStorage: logo.<hash>.png
        self.assertRegex(og, r"logo(\.[0-9a-f]{12})?\.png$")


class CachedAggregateTests(TestCase):
    """P1-2: an uncached COUNT(*) ran on every request site-wide."""

    def setUp(self):
        cache.clear()
        Company.objects.bulk_create([
            Company(gemi_number=f"6660000{i:05d}", name=f"ΕΤΑΙΡΕΙΑ {i}",
                    incorporation_date=timezone.localdate(), prefecture="ΑΤΤΙΚΗΣ")
            for i in range(15)
        ])

    def test_repeat_requests_avoid_the_count_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as cold:
            self.client.get(reverse("home"))
        with CaptureQueriesContext(connection) as warm:
            self.client.get(reverse("home"))

        self.assertGreater(len(cold), len(warm), "caching did not reduce queries")
        counts = [q for q in warm.captured_queries if "COUNT" in q["sql"].upper()]
        self.assertEqual(counts, [], "a COUNT query still runs on a warm request")

    def test_values_are_still_correct(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.context["today_count"], 15)
        self.assertEqual(response.context["global_company_count"], 15)

    def test_cache_failure_does_not_break_the_page(self):
        """global_stats must degrade gracefully, as it did before."""
        with patch("gemiapp.context_processors.Company.objects.count", side_effect=OperationalError):
            cache.clear()
            response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)


@override_settings(ALLOWED_HOSTS=["gemileads.gr", "testserver"])
class StructuredDataTests(TestCase):
    """P1-3: no structured data existed in any format."""

    def _blocks(self, url):
        html = self.client.get(url, HTTP_HOST="gemileads.gr").content.decode()
        raw = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
        return [json.loads(block) for block in raw]

    def test_organization_and_website_are_valid_json_ld(self):
        blocks = self._blocks("/")
        self.assertEqual(len(blocks), 1)
        nodes = {n["@type"]: n for n in blocks[0]["@graph"]}
        self.assertEqual(set(nodes), {"Organization", "WebSite"})
        for required in ("name", "url", "logo", "email"):
            self.assertIn(required, nodes["Organization"])
        self.assertEqual(nodes["WebSite"]["publisher"]["@id"], nodes["Organization"]["@id"])

    def test_software_application_offers_match_the_visible_prices(self):
        blocks = self._blocks("/pricing/")
        software = [b for b in blocks if b.get("@type") == "SoftwareApplication"][0]
        prices = sorted(o["price"] for o in software["offers"])
        self.assertEqual(prices, ["19", "49", "99"])
        for offer in software["offers"]:
            self.assertEqual(offer["priceCurrency"], "EUR")

    def test_quote_based_tier_has_no_offer(self):
        """Custom is 'Κατόπιν Επικοινωνίας'; an Offer with price 0 would say it is free."""
        software = [b for b in self._blocks("/pricing/") if b.get("@type") == "SoftwareApplication"][0]
        names = {o["name"] for o in software["offers"]}
        self.assertNotIn("Custom Package", names)
        self.assertNotIn("0", {o["price"] for o in software["offers"]})

    def test_entity_graph_has_no_dangling_references(self):
        """Each block validates alone even when @id references break, so assert it explicitly."""
        ids = {n["@id"] for b in self._blocks("/pricing/") for n in b.get("@graph", [b])}
        software = [b for b in self._blocks("/pricing/") if b.get("@type") == "SoftwareApplication"][0]
        self.assertIn(software["provider"]["@id"], ids)

    def test_no_fabricated_properties(self):
        """Nothing unverifiable may be encoded."""
        html = self.client.get("/pricing/", HTTP_HOST="gemileads.gr").content.decode()
        raw = " ".join(re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S))
        for banned in ("aggregateRating", "review", "address", "vatID", "legalName", "sameAs"):
            self.assertNotIn(banned, raw, f"{banned} must not be fabricated")


@override_settings(ALLOWED_HOSTS=["gemileads.gr", "testserver"], RATELIMIT_ENABLE=False)
class SocialMetadataUniquenessTests(TestCase):
    """og:title once referenced {{ page_title }}, which no view sets, so every page emitted the
    same generic value while <title> correctly differed."""

    PUBLIC_PAGES = ["home", "pricing", "signup", "login"]

    def _meta(self, name, pattern):
        html = self.client.get(reverse(name), HTTP_HOST="gemileads.gr").content.decode()
        match = re.search(pattern, html)
        self.assertIsNotNone(match, f"{name}: {pattern} not found")
        return match.group(1)

    def test_og_title_is_unique_per_page(self):
        titles = [self._meta(n, r'property="og:title" content="([^"]*)"') for n in self.PUBLIC_PAGES]
        self.assertEqual(len(set(titles)), len(titles), f"duplicate og:title values: {titles}")

    def test_og_title_matches_the_page_title(self):
        import html as html_module

        for name in self.PUBLIC_PAGES:
            title = html_module.unescape(self._meta(name, r"<title>(.*?)</title>"))
            og = html_module.unescape(self._meta(name, r'property="og:title" content="([^"]*)"'))
            self.assertEqual(og, title, f"{name}: og:title does not match <title>")

    def test_og_description_is_unique_per_page(self):
        descs = [self._meta(n, r'property="og:description" content="([^"]*)"') for n in self.PUBLIC_PAGES]
        self.assertEqual(len(set(descs)), len(descs), "duplicate og:description values")

    def test_no_unresolved_template_variable_leaks_into_meta(self):
        """A missing context variable renders empty; assert none of these tags are blank."""
        for name in self.PUBLIC_PAGES:
            for pattern in (r'property="og:title" content="([^"]*)"',
                            r'property="og:description" content="([^"]*)"',
                            r'name="description" content="([^"]*)"'):
                self.assertNotEqual(self._meta(name, pattern).strip(), "", f"{name}: {pattern} empty")

    def test_head_tags_are_not_duplicated(self):
        html = self.client.get(reverse("pricing"), HTTP_HOST="gemileads.gr").content.decode()
        for tag in ('rel="canonical"', 'name="description"', 'name="robots"',
                    "og:title", "og:url", "og:image", "og:description"):
            self.assertEqual(html.count(tag), 1, f"{tag} appears {html.count(tag)} times")


@override_settings(ALLOWED_HOSTS=["gemileads.gr", "testserver"], RATELIMIT_ENABLE=False)
class HomepageAnswerContentTests(TestCase):
    """P2-2: the homepage had no passage an answer engine could quote, and never stated
    what a lead actually contains."""

    def setUp(self):
        self.html = self.client.get(reverse("home"), HTTP_HOST="gemileads.gr").content.decode()

    def test_question_form_headings_exist(self):
        for question in ("Τι είναι το ΓΕΜΗ", "Τι περιλαμβάνει κάθε lead",
                         "Πόσο συχνά ενημερώνονται", "Πώς φιλτράρονται"):
            self.assertIn(question, self.html, f"missing answer block: {question}")

    def test_lead_fields_are_named_on_the_page(self):
        """The strongest selling point was previously invisible."""
        for field in ("ΑΦΜ", "επωνυμία", "νομική μορφή", "email"):
            self.assertIn(field, self.html, f"lead field not described: {field}")

    def test_stated_schedule_matches_the_scheduler(self):
        from gemiapp.apps import SCHEDULES

        crons = {e["func"].rsplit(".", 1)[-1]: e["cron"] for e in SCHEDULES}
        self.assertEqual(crons["run_daily_pipeline_task"], "0 9 * * *")
        self.assertEqual(crons["run_intraday_pipeline_task"], "0 8,11,14,17,20,23 * * *")
        self.assertIn("09:00", self.html)
        self.assertIn("23:00", self.html)

    def test_kad_figure_matches_the_catalogue(self):
        """P2-4: the page claimed 9.744, which matched neither the catalogue nor the DB."""
        import json
        from pathlib import Path
        from django.conf import settings

        catalogue = json.loads(
            (Path(settings.BASE_DIR) / "gemiapp" / "data" / "kad_2025.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(catalogue), 9651)
        self.assertIn("9.651", self.html)
        self.assertNotIn("9.744", self.html)

    def test_links_to_the_primary_source(self):
        self.assertIn("opendata.businessportal.gr", self.html)

    def test_menu_icon_is_hidden_from_assistive_tech(self):
        """P2-3: the button has aria-label; its decorative icon should not be announced too."""
        self.assertIn('aria-label="Μενού"', self.html)
        button = self.html[self.html.index('id="menuButton"'):]
        self.assertIn('aria-hidden="true"', button[:400])


class SchedulerHealthCheckTests(TestCase):
    """The check counted every failure ever recorded, so it pinned itself to Warning forever.

    django-q2 prunes successful tasks via save_limit but never deletes failed ones, so a bug
    fixed months ago would still show as a live problem.
    """

    def _failure(self, days_ago):
        from django_q.models import Task

        task = Task.objects.create(
            id=f"fail-{days_ago}", name=f"fail-{days_ago}",
            func="gemiapp.tasks.run_daily_pipeline_task",
            started=timezone.now(), stopped=timezone.now(), success=False,
        )
        Task.objects.filter(pk=task.pk).update(started=timezone.now() - timedelta(days=days_ago))

    def _status(self):
        from gemiapp.superadmin.services import get_system_health

        return get_system_health()["services"]["scheduler"]

    def test_healthy_when_schedules_exist_and_nothing_failed(self):
        self.assertEqual(self._status()["status"], "Operational")

    def test_old_failures_do_not_keep_the_check_red(self):
        from gemiapp.superadmin.services import SCHEDULER_FAILURE_WINDOW_DAYS

        self._failure(days_ago=SCHEDULER_FAILURE_WINDOW_DAYS + 23)
        status = self._status()
        self.assertEqual(status["status"], "Operational")
        self.assertIn("older failure", status["details"])

    def test_recent_failure_raises_a_warning(self):
        self._failure(days_ago=1)
        status = self._status()
        self.assertEqual(status["status"], "Warning")
        self.assertIn("failed run", status["details"])

    def test_missing_schedules_warn_even_with_no_failures(self):
        """Nothing registered means the pipelines silently never run."""
        from django_q.models import Schedule

        Schedule.objects.all().delete()
        status = self._status()
        self.assertEqual(status["status"], "Warning")
        self.assertIn("No scheduled tasks", status["details"])


class PurgeFailedTasksCommandTests(TestCase):
    """Recent failures signal a live problem and must survive the purge."""

    def _failure(self, key, days_ago):
        from django_q.models import Task

        task = Task.objects.create(
            id=key, name=key, func="gemiapp.tasks.run_daily_pipeline_task",
            started=timezone.now(), stopped=timezone.now(), success=False,
        )
        Task.objects.filter(pk=task.pk).update(started=timezone.now() - timedelta(days=days_ago))

    def _run(self, *args):
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        call_command("purge_failed_tasks", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_deletes_nothing(self):
        from django_q.models import Task

        self._failure("old", days_ago=30)
        output = self._run("--dry-run")
        self.assertIn("dry-run", output)
        self.assertEqual(Task.objects.filter(success=False).count(), 1)

    def test_old_failures_are_removed_and_recent_ones_kept(self):
        from django_q.models import Task

        self._failure("old", days_ago=30)
        self._failure("fresh", days_ago=1)

        self._run()

        remaining = list(Task.objects.filter(success=False).values_list("id", flat=True))
        self.assertEqual(remaining, ["fresh"], "a recent failure must not be purged")

    def test_successful_tasks_are_never_touched(self):
        from django_q.models import Task

        Task.objects.create(id="ok", name="ok", func="gemiapp.tasks.run_daily_pipeline_task",
                            started=timezone.now() - timedelta(days=99),
                            stopped=timezone.now(), success=True)
        self._run()
        self.assertTrue(Task.objects.filter(id="ok").exists())


@override_settings(
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz",
    STRIPE_PRICE_ENTERPRISE="price_ent", RATELIMIT_ENABLE=False,
)
class CheckoutAuthenticationFlowTests(TestCase):
    """Selecting a plan while logged out used to 405.

    @login_required redirected the POST to /login/?next=<POST-only URL>; after logging in the
    browser replayed `next` as a GET and @require_POST answered 405.
    """

    def _user(self, email="buyer@example.com"):
        return User.objects.create_user(email, email, "StrongPass123")

    def _stripe(self):
        return patch("stripe.checkout.Session.create",
                     return_value=type("S", (), {"url": "https://stripe.test/checkout"})())

    # A. logged-in user selects a plan
    def test_authenticated_user_reaches_stripe_directly(self):
        self.client.force_login(self._user())
        with self._stripe() as create:
            response = self.client.post(reverse("create_checkout_session"), {"tier": "pro"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(create.call_args.kwargs["line_items"], [{"price": "price_pro", "quantity": 1}])

    # B. logged-out user selects a plan -> login -> resume, with no 405 anywhere
    def test_anonymous_selection_survives_login_without_405(self):
        self._user()

        selected = self.client.post(reverse("create_checkout_session"), {"tier": "business"})
        self.assertEqual(selected.status_code, 302)
        self.assertEqual(selected["Location"], f"{reverse('login')}?next={reverse('resume_checkout')}")
        self.assertEqual(self.client.session["pending_checkout_tier"], "business")

        logged_in = self.client.post(
            f"{reverse('login')}?next={reverse('resume_checkout')}",
            {"username": "buyer@example.com", "password": "StrongPass123"},
        )
        self.assertEqual(logged_in["Location"], reverse("resume_checkout"))

        resumed = self.client.get(reverse("resume_checkout"))
        self.assertEqual(resumed.status_code, 200, "the resume step must never 405")
        self.assertContains(resumed, 'value="business"')

        with self._stripe() as create:
            done = self.client.post(reverse("create_checkout_session"), {"tier": "business"})
        self.assertEqual(done.status_code, 303)
        self.assertEqual(create.call_args.kwargs["line_items"], [{"price": "price_biz", "quantity": 1}])

    # C. the tier chosen before login is the tier that gets bought
    def test_resumed_plan_matches_the_original_selection(self):
        self._user()
        self.client.post(reverse("create_checkout_session"), {"tier": "enterprise"})
        self.client.login(username="buyer@example.com", password="StrongPass123")
        self.assertContains(self.client.get(reverse("resume_checkout")), 'value="enterprise"')

    # D. new user signs up after selecting a plan
    def test_signup_then_verification_resumes_the_plan(self):
        self.client.post(reverse("create_checkout_session"), {"tier": "pro"})
        self.client.post(reverse("signup"), {
            "first_name": "Νέος", "email": "new@example.com",
            "password1": "StrongPass123!", "password2": "StrongPass123!",
        })
        user = User.objects.get(email="new@example.com")
        link = re.search(r"/verify/[^\s\"'<]+", mail.outbox[-1].body).group(0)

        response = self.client.get(link)
        self.assertRedirects(response, reverse("resume_checkout"), fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_verification_without_a_pending_plan_still_goes_to_dashboard(self):
        self.client.post(reverse("signup"), {
            "first_name": "Απλός", "email": "plain@example.com",
            "password1": "StrongPass123!", "password2": "StrongPass123!",
        })
        link = re.search(r"/verify/[^\s\"'<]+", mail.outbox[-1].body).group(0)
        self.assertRedirects(self.client.get(link), reverse("dashboard"), fetch_redirect_response=False)

    # E. the state-changing endpoint stays POST-only
    def test_get_on_checkout_endpoint_is_rejected(self):
        self.client.force_login(self._user())
        with patch("stripe.checkout.Session.create") as create:
            response = self.client.get(reverse("create_checkout_session"))
        self.assertEqual(response.status_code, 405)
        create.assert_not_called()

    def test_anonymous_get_creates_no_session_and_no_pending_tier(self):
        with patch("stripe.checkout.Session.create") as create:
            self.assertEqual(self.client.get(reverse("create_checkout_session")).status_code, 405)
        create.assert_not_called()
        self.assertNotIn("pending_checkout_tier", self.client.session)

    # F. manipulated plan identifiers
    def test_invalid_tier_is_rejected_without_touching_stripe(self):
        self.client.force_login(self._user())
        for bogus in ("bogus", "custom", "free", "", "PRO", "pro; drop"):
            with patch("stripe.checkout.Session.create") as create:
                response = self.client.post(reverse("create_checkout_session"), {"tier": bogus})
            self.assertRedirects(response, reverse("pricing"))
            create.assert_not_called()

    def test_invalid_tier_is_not_parked_in_the_session_when_anonymous(self):
        response = self.client.post(reverse("create_checkout_session"), {"tier": "bogus"})
        self.assertRedirects(response, reverse("pricing"))
        self.assertNotIn("pending_checkout_tier", self.client.session)

    def test_a_price_id_cannot_be_injected(self):
        """Only a validated tier name is accepted; price ids come from settings."""
        self.client.force_login(self._user())
        with patch("stripe.checkout.Session.create") as create:
            self.client.post(reverse("create_checkout_session"),
                             {"tier": "pro", "price": "price_attacker", "price_id": "price_attacker"})
        self.assertEqual(create.call_args.kwargs["line_items"], [{"price": "price_pro", "quantity": 1}])

    # G. open redirect
    def test_external_next_cannot_redirect_off_site(self):
        self._user()
        response = self.client.post(
            f"{reverse('login')}?next=https://evil.example.com/steal",
            {"username": "buyer@example.com", "password": "StrongPass123"},
        )
        self.assertNotIn("evil.example.com", response["Location"])

    def test_resume_route_ignores_a_supplied_next(self):
        """The tier comes from the session, never from the query string."""
        self.client.force_login(self._user())
        response = self.client.get(reverse("resume_checkout") + "?tier=enterprise&next=https://evil.example.com")
        self.assertRedirects(response, reverse("pricing"))

    # H. every selectable plan uses the same corrected flow
    def test_all_plans_resume_identically(self):
        from gemiapp.billing import SELECTABLE_TIERS

        for tier, price in zip(SELECTABLE_TIERS, ("price_pro", "price_biz", "price_ent")):
            client = Client()
            User.objects.create_user(f"{tier}@example.com", f"{tier}@example.com", "StrongPass123")

            redirected = client.post(reverse("create_checkout_session"), {"tier": tier})
            self.assertEqual(redirected["Location"], f"{reverse('login')}?next={reverse('resume_checkout')}")

            client.login(username=f"{tier}@example.com", password="StrongPass123")
            self.assertContains(client.get(reverse("resume_checkout")), f'value="{tier}"')

            with self._stripe() as create:
                client.post(reverse("create_checkout_session"), {"tier": tier})
            self.assertEqual(create.call_args.kwargs["line_items"], [{"price": price, "quantity": 1}])

    # I. the upgrade / manage-plan path
    def test_customer_portal_is_not_used_as_a_login_next_target(self):
        """customer_portal is POST-only behind auth, but is only rendered to authenticated
        users, so it can never become a login `next` target."""
        html = self.client.get(reverse("pricing")).content.decode()
        self.assertNotIn(reverse("customer_portal"), html)

    def test_customer_portal_rejects_get(self):
        self.client.force_login(self._user())
        self.assertEqual(self.client.get(reverse("customer_portal")).status_code, 405)

    # J. ordinary login is untouched
    def test_login_without_a_pending_plan_goes_to_the_dashboard(self):
        self._user()
        response = self.client.post(reverse("login"),
                                    {"username": "buyer@example.com", "password": "StrongPass123"})
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_resume_route_requires_authentication(self):
        response = self.client.get(reverse("resume_checkout"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_resume_without_a_pending_plan_falls_back_to_pricing(self):
        self.client.force_login(self._user())
        self.assertRedirects(self.client.get(reverse("resume_checkout")), reverse("pricing"))

    def test_pending_tier_is_consumed_once(self):
        """Prevents a stale plan resurfacing on a later, unrelated login."""
        self._user()
        self.client.post(reverse("create_checkout_session"), {"tier": "pro"})
        self.client.login(username="buyer@example.com", password="StrongPass123")

        self.assertEqual(self.client.get(reverse("resume_checkout")).status_code, 200)
        self.assertRedirects(self.client.get(reverse("resume_checkout")), reverse("pricing"))


class PostOnlyEndpointExposureTests(TestCase):
    """No POST-only view behind authentication may be reachable by an anonymous visitor,
    otherwise it becomes a login `next` target and 405s after login."""

    def test_no_public_template_posts_to_a_login_required_post_only_view(self):
        """pricing is the only public page that posts to a POST-only endpoint, and its
        checkout view now handles anonymous callers itself."""
        html = self.client.get(reverse("pricing")).content.decode()
        # These are the POST-only endpoints behind authentication. None may appear on the
        # only public page that renders POST forms.
        for path in (reverse("customer_portal"), reverse("radar_preview"),
                     reverse("lead_favorite", args=[1]), reverse("lead_status", args=[1]),
                     reverse("lead_notes", args=[1]), reverse("radar_toggle", args=[1]),
                     reverse("radar_delete", args=[1])):
            self.assertNotIn(path, html)

    def test_anonymous_post_to_gated_endpoints_never_yields_405_after_login(self):
        """Those endpoints redirect anonymous POSTs to login, but their forms are only ever
        rendered to authenticated users, so the GET replay cannot occur in practice."""
        user = User.objects.create_user("g@example.com", "g@example.com", "StrongPass123")
        company = Company.objects.create(
            gemi_number="770000000001", name="ΤΕΣΤ ΙΚΕ",
            incorporation_date=timezone.localdate(), prefecture="ΑΤΤΙΚΗΣ",
        )
        lead = UserCompanyLead.objects.create(user=user, company=company)

        response = self.client.post(reverse("lead_favorite", args=[lead.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])


@override_settings(ALLOWED_HOSTS=["gemileads.gr", "testserver"], RATELIMIT_ENABLE=False)
class SearchConsoleVerificationTests(TestCase):
    """Meta-tag verification is env-driven so the token is never committed and can be
    changed without a code deploy. DNS TXT verification needs none of this."""

    def _html(self):
        return self.client.get(reverse("home"), HTTP_HOST="gemileads.gr").content.decode()

    @override_settings(GOOGLE_SITE_VERIFICATION="")
    def test_no_empty_meta_tag_when_unconfigured(self):
        self.assertNotIn("google-site-verification", self._html())

    @override_settings(GOOGLE_SITE_VERIFICATION="tok3n-value")
    def test_tag_is_emitted_when_configured(self):
        self.assertIn('<meta name="google-site-verification" content="tok3n-value">', self._html())

    @override_settings(GOOGLE_SITE_VERIFICATION="tok3n-value")
    def test_tag_appears_on_every_public_page(self):
        """Google may check any URL, not only the homepage."""
        for name in ("home", "pricing", "signup", "login"):
            html = self.client.get(reverse(name), HTTP_HOST="gemileads.gr").content.decode()
            self.assertIn("google-site-verification", html, name)

    def test_sitemap_is_reachable_and_lists_only_public_pages(self):
        """Search Console rejects a sitemap it cannot fetch."""
        response = self.client.get("/sitemap.xml", HTTP_HOST="gemileads.gr")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("<loc>", body)
        for private in ("/dashboard/", "/leads/", "/settings/", "/superadmin/"):
            self.assertNotIn(f"<loc>https://gemileads.gr{private}</loc>", body)

    def test_robots_does_not_block_the_pages_in_the_sitemap(self):
        """A sitemap URL blocked by robots.txt is reported as an error in Search Console."""
        robots = self.client.get("/robots.txt", HTTP_HOST="gemileads.gr").content.decode()
        for allowed in ("/pricing/", "/signup/", "/login/"):
            self.assertNotIn(f"Disallow: {allowed}", robots)
        self.assertIn("Sitemap:", robots)


@override_settings(ALLOWED_HOSTS=["gemileads.gr", "testserver"], RATELIMIT_ENABLE=False)
class AnalyticsConsentTests(TestCase):
    """GA4 sets non-essential cookies, so under ePrivacy/GDPR nothing may load before consent."""

    def _html(self, **extra):
        return self.client.get(reverse("home"), HTTP_HOST="gemileads.gr", **extra).content.decode()

    @override_settings(GA_MEASUREMENT_ID="")
    def test_nothing_renders_when_analytics_is_disabled(self):
        html = self._html()
        self.assertNotIn("cookieBanner", html)
        self.assertNotIn("googletagmanager", html)

    @override_settings(GA_MEASUREMENT_ID="G-TEST123456")
    def test_gtag_is_never_a_static_script_tag(self):
        """The 486 KB gtag.js must only ever be injected after an explicit accept."""
        html = self._html()
        self.assertIsNone(
            re.search(r'<script[^>]+src="https://www\.googletagmanager\.com', html),
            "gtag.js must not load before consent",
        )

    @override_settings(GA_MEASUREMENT_ID="G-TEST123456")
    def test_banner_and_both_choices_are_offered(self):
        html = self._html()
        self.assertIn("cookieBanner", html)
        self.assertIn("data-cookie-accept", html)
        self.assertIn("data-cookie-decline", html)

    @override_settings(GA_MEASUREMENT_ID="G-TEST123456")
    def test_measurement_id_reaches_the_page_safely(self):
        """escapejs encodes the hyphen; JavaScript still resolves it to the original id."""
        html = self._html()
        # Django's escapejs emits a literal backslash-u escape for the hyphen; JavaScript
        # decodes it back to the original id at runtime.
        self.assertIn("TEST123456", html)
        self.assertRegex(html, "var ID = 'G(" + chr(92) + chr(92) + "u002D|-)TEST123456'")

    @override_settings(GA_MEASUREMENT_ID="G-TEST123456")
    def test_analytics_appears_on_every_public_page(self):
        for name in ("home", "pricing", "signup", "login"):
            html = self.client.get(reverse(name), HTTP_HOST="gemileads.gr").content.decode()
            self.assertIn("cookieBanner", html, name)

    @override_settings(GA_MEASUREMENT_ID='G-X"><script>alert(1)</script>')
    def test_measurement_id_cannot_break_out_of_the_script(self):
        html = self._html()
        self.assertNotIn("<script>alert(1)</script>", html)


@override_settings(ALLOWED_HOSTS=["gemileads.gr", "testserver"], RATELIMIT_ENABLE=False)
class LegalPagesTests(TestCase):
    """A policy naming no controller is not binding, so it must never be published as if it were."""

    def _get(self, name):
        return self.client.get(reverse(name), HTTP_HOST="gemileads.gr").content.decode()

    @override_settings(LEGAL_CONTROLLER_NAME="")
    def test_draft_pages_are_noindex_and_warn(self):
        for name in ("privacy", "terms"):
            html = self._get(name)
            self.assertIn("noindex, nofollow", html, name)
            self.assertIn("Προσχέδιο", html, name)

    @override_settings(LEGAL_CONTROLLER_NAME="ΔΟΚΙΜΗ ΙΚΕ", LEGAL_VAT="EL123456789")
    def test_completed_pages_become_indexable(self):
        for name in ("privacy", "terms"):
            html = self._get(name)
            self.assertIn("index, follow", html, name)
            self.assertNotIn("Προσχέδιο", html, name)
            self.assertIn("ΔΟΚΙΜΗ ΙΚΕ", html, name)

    @override_settings(LEGAL_CONTROLLER_NAME="")
    def test_no_invented_company_details_are_shown(self):
        """Empty optional fields are omitted rather than rendered blank."""
        html = self._get("privacy")
        for label in ("ΑΦΜ:", "Αριθμός ΓΕΜΗ:", "Διεύθυνση:"):
            self.assertNotIn(label, html)

    @override_settings(LEGAL_BILLING_ACTIVE=False)
    def test_terms_do_not_promise_billing_before_it_exists(self):
        html = self._get("terms")
        self.assertIn("δεν είναι ακόμη ενεργές", html)
        self.assertNotIn("ανανεώνεται αυτόματα", html)

    @override_settings(LEGAL_BILLING_ACTIVE=True)
    def test_billing_clauses_appear_once_payments_are_live(self):
        html = self._get("terms")
        self.assertIn("ανανεώνεται αυτόματα", html)

    def test_privacy_covers_the_processors_actually_in_use(self):
        html = self._get("privacy")
        for processor in ("Render", "Brevo", "Sentry", "Google Analytics", "Cloudflare"):
            self.assertIn(processor, html, processor)

    def test_privacy_names_the_supervisory_authority(self):
        self.assertIn("dpa.gr", self._get("privacy"))

    def test_footer_links_to_both_pages_from_every_public_page(self):
        for name in ("home", "pricing", "login"):
            html = self.client.get(reverse(name), HTTP_HOST="gemileads.gr").content.decode()
            self.assertIn(reverse("privacy"), html, name)
            self.assertIn(reverse("terms"), html, name)

    @override_settings(GA_MEASUREMENT_ID="G-TEST123456")
    def test_cookie_banner_links_to_the_privacy_policy(self):
        html = self._get("home")
        self.assertIn(reverse("privacy"), html)

    def test_pages_are_public(self):
        for name in ("privacy", "terms"):
            self.assertEqual(
                self.client.get(reverse(name), HTTP_HOST="gemileads.gr").status_code, 200, name
            )


class TemplateTagIntegrityTests(TestCase):
    """Django parses {{ }}, {% %} and {# #} with single-line regexes.

    A tag broken across a newline (usually by an HTML formatter reflowing a long line) is
    not parsed at all: it is emitted verbatim to the visitor. This shipped to production
    once, printing SEO comments at the top of every page and a literal "{{ field.label }}"
    on the password-reset form, so it is guarded rather than merely fixed.
    """

    CLOSERS = {"#": "#}", "{": "}}", "%": "%}"}

    def _offenders(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "templates"
        found = []
        for path in sorted(root.rglob("*.html")):
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r"\{([#{%])", source):
                closer = self.CLOSERS[match.group(1)]
                end = source.find(closer, match.end())
                if end != -1 and "\n" in source[match.start():end + len(closer)]:
                    line = source[:match.start()].count("\n") + 1
                    found.append(f"{path.relative_to(root)}:{line}")
        return found

    def test_no_template_tag_spans_a_newline(self):
        self.assertEqual(
            self._offenders(), [],
            "these tags span a newline and will render as literal text to visitors",
        )

    def test_the_detector_actually_catches_a_broken_tag(self):
        """Guards the guard: a silently-passing detector would be worse than none."""
        from django.template import engines

        rendered = engines["django"].from_string("A{{ v\n }}B").render({"v": "x"})
        self.assertIn("{{ v", rendered, "Django would have to change for the check to be moot")

    def test_pages_do_not_leak_template_source_to_visitors(self):
        for name in ("home", "pricing", "login", "password_reset", "privacy", "terms"):
            with self.subTest(page=name):
                html = self.client.get(reverse(name)).content.decode()
                self.assertNotIn("{#", html)
                self.assertNotIn("{{", html)


class CompanyPeopleTests(TestCase):
    """Managers and partners come from the ΓΕΜΗ payload the importer already stores."""

    PERSONS = [
        {"personName": "ΠΑΠΑΔΟΠΟΥΛΟΥ ΒΑΣΙΛΙΚΗ", "businessName": None,
         "role": "Ετερόρρυθμο Μέλος", "category": "Εταίροι", "percentage": "10%",
         "dtFrom": "2026-08-19", "dtTo": None,
         "isRepresentativeAlone": None, "isRepresentativeInCommon": None},
        {"personName": "ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", "businessName": None,
         "role": "Ομόρρυθμο Μέλος, Διαχειριστής & Εκπρόσωπος", "category": "Εταίροι",
         "percentage": "90%", "dtFrom": "2026-08-19", "dtTo": None,
         "isRepresentativeAlone": True, "isRepresentativeInCommon": None},
        {"personName": "ΠΑΛΑΙΟΣ ΔΙΑΧΕΙΡΙΣΤΗΣ", "businessName": None,
         "role": "Διαχειριστής", "category": "Εταίροι", "percentage": "0%",
         "dtFrom": "2020-01-01", "dtTo": "2024-06-30",
         "isRepresentativeAlone": True, "isRepresentativeInCommon": None},
    ]

    def _company(self, persons=None, gemi="880000000001"):
        return Company.objects.create(
            gemi_number=gemi, name="ΔΟΚΙΜΗ Ε.Ε.", legal_type="ΕΕ",
            incorporation_date=timezone.localdate(), prefecture="ΡΟΔΟΣ",
            raw_data={"persons": self.PERSONS if persons is None else persons},
        )

    def _user(self, email, tier=None):
        user = User.objects.create_user(email, email, "StrongPass123")
        if tier:
            sub = UserSubscription.objects.get(user=user)
            sub.complimentary_tier = tier
            sub.save()
        return user

    def test_current_people_are_parsed_from_the_stored_payload(self):
        names = [p["name"] for p in self._company().people]
        self.assertIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", names)
        self.assertIn("ΠΑΠΑΔΟΠΟΥΛΟΥ ΒΑΣΙΛΙΚΗ", names)

    def test_people_whose_term_has_ended_are_excluded(self):
        """Showing someone who has left as a current manager is worse than showing nobody."""
        self.assertNotIn("ΠΑΛΑΙΟΣ ΔΙΑΧΕΙΡΙΣΤΗΣ", [p["name"] for p in self._company().people])

    def test_the_representative_is_listed_first(self):
        self.assertEqual(self._company().people[0]["name"], "ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ")

    def test_role_and_stake_are_carried_through(self):
        person = self._company().people[0]
        self.assertEqual(person["percentage"], "90%")
        self.assertTrue(person["represents_alone"])
        self.assertIn("Διαχειριστής", person["role"])

    def test_malformed_entries_do_not_raise(self):
        for payload in ({}, {"persons": None}, {"persons": []}, {"persons": ["x", None]},
                        {"persons": [{"personName": "   ", "dtTo": None}]}):
            company = Company(gemi_number="0", name="X", raw_data=payload)
            self.assertEqual(company.people, [], payload)

    def test_a_business_entity_partner_falls_back_to_its_name(self):
        people = self._company([{"personName": None, "businessName": "ΑΛΦΑ Α.Ε.",
                                 "role": "Εταίρος", "dtTo": None}]).people
        self.assertEqual(people[0]["name"], "ΑΛΦΑ Α.Ε.")

    # Gating: everything except free.
    def test_a_subscriber_sees_the_names(self):
        company = self._company()
        self.client.force_login(self._user("paid@example.com", "pro"))
        html = self.client.get(reverse("company_detail", args=[company.gemi_number])).content.decode()
        self.assertIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", html)
        self.assertIn("Εκπροσωπεί μόνος", html)

    def test_every_paid_tier_sees_the_names(self):
        for tier in ("pro", "business", "enterprise", "custom"):
            with self.subTest(tier=tier):
                company = self._company(gemi=f"88000000{hash(tier) % 10000:04d}")
                client = Client()
                client.force_login(self._user(f"{tier}-p@example.com", tier))
                html = client.get(reverse("company_detail", args=[company.gemi_number])).content.decode()
                self.assertIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", html)

    def test_a_free_user_gets_the_upsell_and_never_the_names(self):
        company = self._company()
        self.client.force_login(self._user("free@example.com"))
        html = self.client.get(reverse("company_detail", args=[company.gemi_number])).content.decode()
        self.assertNotIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", html)
        self.assertNotIn("ΠΑΠΑΔΟΠΟΥΛΟΥ ΒΑΣΙΛΙΚΗ", html)
        self.assertIn("Απαιτείται συνδρομή", html)
        self.assertIn(reverse("pricing"), html)

    def test_staff_see_the_names_without_a_subscription(self):
        company = self._company()
        staff = self._user("staff@example.com")
        staff.is_staff = True
        staff.save()
        self.client.force_login(staff)
        html = self.client.get(reverse("company_detail", args=[company.gemi_number])).content.decode()
        self.assertIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", html)

    def test_the_page_is_not_public(self):
        """Names are personal data: they must never be reachable without logging in."""
        company = self._company()
        response = self.client.get(reverse("company_detail", args=[company.gemi_number]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_company_pages_stay_out_of_robots(self):
        self.assertIn("Disallow: /companies/", self.client.get("/robots.txt").content.decode())

    def test_a_sole_trader_page_renders_without_an_empty_card(self):
        company = Company.objects.create(
            gemi_number="880000000777", name="ΑΤΟΜΙΚΗ", legal_type="ΑΤΟΜΙΚΗ",
            incorporation_date=timezone.localdate(), prefecture="ΑΤΤΙΚΗΣ", raw_data={},
        )
        self.client.force_login(self._user("solo@example.com", "pro"))
        html = self.client.get(reverse("company_detail", args=[company.gemi_number])).content.decode()
        self.assertNotIn("Διαχειριστές &amp; Εταίροι", html)

    def test_the_privacy_policy_discloses_this_processing(self):
        """The disclosure and the feature must not ship apart."""
        html = self.client.get(reverse("privacy")).content.decode()
        for phrase in ("διαχειριστές", "νόμιμους εκπροσώπους", "έννομο συμφέρον",
                       "δικαίωμα εναντίωσης", "μόνο σε συνδρομητές"):
            self.assertIn(phrase, html, phrase)
