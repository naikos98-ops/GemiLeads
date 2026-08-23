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
        self.user = User.objects.create_user("member@example.com", "member@example.com", "StrongPass123")
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
        self.assertEqual(self.client.get(reverse("company_detail", args=[other_company.gemi_number])).status_code, 404)
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
        
        # Delete the default radar created in setUp
        CustomerRadar.objects.filter(user=self.user).delete()
        
        # Test Free limit (1)
        response = self.client.post(reverse("radar_create"), {"name": "First Radar", "is_active": True, "frequency": "daily"})
        self.assertEqual(response.status_code, 302)
        
        response2 = self.client.post(reverse("radar_create"), {"name": "Second Radar", "is_active": True, "frequency": "daily"})
        self.assertEqual(response2.status_code, 200)
        self.assertContains(response2, "Έχεις φτάσει το όριο των 1 Ραντάρ")

        # Upgrade to Pro
        from .models import UserSubscription
        sub, _ = UserSubscription.objects.update_or_create(user=self.user, defaults={"tier": "pro"})
        
        response3 = self.client.post(reverse("radar_create"), {"name": "Second Radar", "is_active": True, "frequency": "daily"})
        self.assertEqual(response3.status_code, 302)

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
