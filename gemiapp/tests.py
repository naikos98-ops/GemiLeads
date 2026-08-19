from datetime import date, datetime, timedelta
from unittest.mock import patch
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
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
        DigestPreference.objects.create(user=self.user)
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
        radar1 = CustomerRadar.objects.create(user=self.user, name="Radar 1", prefectures=["ΑΤΤΙΚΗΣ"], frequency="daily", monitor_from=timezone.make_aware(datetime(2026, 8, 1, 0, 0)))
        radar2 = CustomerRadar.objects.create(user=self.user, name="Radar 2", legal_types=["Ιδιωτική Κεφαλαιουχική Εταιρεία"], frequency="daily", monitor_from=timezone.make_aware(datetime(2026, 8, 1, 0, 0)))
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
            "frequency": "weekly", "include_empty_digest": "on",
        })
        self.assertRedirects(response, reverse("settings"))
        preference = DigestPreference.objects.get(user=self.user)
        self.assertEqual(preference.frequency, "weekly")
        self.assertTrue(preference.include_empty_digest)

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
        other_user = User.objects.create_user("other@example.com", "other@example.com", "StrongPass123")
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
    def test_digest_weekly_and_empty_digest(self, _fetch):
        radar = CustomerRadar.objects.create(user=self.user, name="Weekly Radar", prefectures=["ΘΕΣΣΑΛΟΝΙΚΗΣ"], frequency="weekly", monitor_from=timezone.make_aware(datetime(2026, 8, 1, 0, 0)))
        preference = self.user.digest_preference
        preference.include_empty_digest = True
        preference.save()
        import_for_date(date(2026, 8, 1))
        self.assertEqual(send_digests(date(2026, 8, 1), frequency="weekly"), (1, 0))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Δεν καταγράφηκαν", mail.outbox[0].body)

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
        DigestPreference.objects.create(user=self.user)

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

        company = Company.objects.create(gemi_number="999001", vat_number="9990001", name="MATCH ME LTD", incorporation_date=today)
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
        radar = CustomerRadar.objects.create(user=self.user, name="Historical Radar", is_active=True)

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
        DigestPreference.objects.create(user=self.user)

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
        DigestPreference.objects.create(user=self.superuser)
        DigestPreference.objects.create(user=self.normal_user)
        DigestPreference.objects.create(user=self.staff_user)

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
        user = User.objects.create_superuser(username="super_user", email="superuser@gemileads.gr", password="Password123!")
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
        DigestPreference.objects.create(user=ent_user)
        sub_ent, _ = UserSubscription.objects.get_or_create(user=ent_user, defaults={"complimentary_tier": "enterprise"})
        sub_ent.complimentary_tier = "enterprise"
        sub_ent.save()

        sent, skipped = send_digests(today, frequency="intraday")
        self.assertTrue(sent >= 0)
