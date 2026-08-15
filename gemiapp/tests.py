from datetime import date
from unittest.mock import patch
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from .models import ActivityCode, Company, CompanyActivity, DigestDelivery, DigestPreference
from .services import import_for_date, send_daily_digests


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
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)
        self.client.login(username="member@example.com", password="StrongPass123")
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

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
    def test_digest_sent_once(self, _fetch):
        import_for_date(date(2026, 8, 1))
        self.assertEqual(send_daily_digests(date(2026, 8, 1)), (1, 0))
        self.assertEqual(send_daily_digests(date(2026, 8, 1)), (0, 1))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(DigestDelivery.objects.count(), 1)

    def test_csv_export(self):
        Company.objects.create(gemi_number="1", name="CSV TEST", incorporation_date=date(2026, 8, 1))
        self.client.login(username="member@example.com", password="StrongPass123")
        response = self.client.get(reverse("export_csv"))
        self.assertEqual(response.status_code, 200)
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

    def test_settings_save_selected_kads(self):
        self.client.login(username="member@example.com", password="StrongPass123")
        response = self.client.post(reverse("settings"), {
            "frequency": "daily", "only_active": "on", "activity_codes": "62010000",
        })
        self.assertRedirects(response, reverse("settings"))
        self.assertEqual(DigestPreference.objects.get(user=self.user).activity_codes, ["62010000"])

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    @patch("gemiapp.services.fetch_companies", return_value=[SAMPLE])
    def test_digest_filters_by_kad(self, _fetch):
        preference = self.user.digest_preference
        preference.activity_codes = ["99999999"]
        preference.save()
        import_for_date(date(2026, 8, 1))
        self.assertEqual(send_daily_digests(date(2026, 8, 1)), (0, 1))
