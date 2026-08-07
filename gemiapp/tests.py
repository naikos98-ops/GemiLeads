from datetime import date
from unittest.mock import patch
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from .models import Company, DigestDelivery, DigestPreference
from .services import import_for_date, send_daily_digests


SAMPLE = {
    "arGemi": 123456789000, "afm": "123456789", "coNameEl": "TEST SIGNAL ΙΚΕ",
    "coTitlesEl": ["TEST SIGNAL"], "incorporationDate": "2026-08-01",
    "legalType": {"descr": "Ιδιωτική Κεφαλαιουχική Εταιρεία"},
    "status": {"descr": "Ενεργή", "isActive": True}, "city": "ΑΘΗΝΑ",
    "prefecture": {"descr": "ΑΤΤΙΚΗΣ"}, "activities": [],
}


class AppTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("member@example.com", "member@example.com", "StrongPass123")
        DigestPreference.objects.create(user=self.user)

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
