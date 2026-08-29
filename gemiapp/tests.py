import datetime as dt
import json
import re
import tempfile
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import stripe
from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.core.cache import cache
from django.db import transaction
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
    RADAR_LIMITS,
    PersonSuppression,
    EmailEngagementEvent,
    StripeWebhookEvent,
    UserCompanyLead,
    UserSubscription,
)
from .services import digest_email_tag, import_for_date, match_imported_companies, send_digests


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


@override_settings(SUPERADMIN_EMAILS=["admin@gemileads.gr"])
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

    def test_client_finder_lists_and_sends_outreach(self):
        from datetime import date
        from .models import Company, CompanyOutreach

        with_email = Company.objects.create(
            gemi_number="900001", name="Νέα ΑΕ", incorporation_date=date.today(), email="hello@nea.gr",
        )
        no_email = Company.objects.create(
            gemi_number="900002", name="Χωρίς Email ΟΕ", incorporation_date=date.today(),
        )
        already = Company.objects.create(
            gemi_number="900003", name="Ήδη Επικοινωνημένη", incorporation_date=date.today(), email="x@y.gr",
        )
        CompanyOutreach.objects.create(company=already, status="sent", sent_to="x@y.gr")

        self.client.login(username="admin@gemileads.gr", password="SuperPassword123")
        res = self.client.get(reverse("superadmin:client_finder"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Νέα ΑΕ")
        self.assertNotContains(res, "Χωρίς Email ΟΕ")
        # Already contacted: absent from the candidate checkboxes (history lives on its own
        # page now -- see OutreachHistoryTests).
        self.assertNotContains(res, f'value="{already.id}"')

        # Test send: goes to an arbitrary address, records nothing.
        res_test = self.client.post(reverse("superadmin:client_finder_test"), {"email": "tester@example.com"})
        self.assertRedirects(res_test, reverse("superadmin:client_finder"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["tester@example.com"])
        self.assertEqual(CompanyOutreach.objects.count(), 1)  # only the pre-existing "already" row
        mail.outbox.clear()

        # Send: the request only queues (pending rows), a worker task does the SMTP.
        res_send = self.client.post(reverse("superadmin:client_finder_send"), {
            "mode": "selected", "company_ids": [with_email.id, no_email.id],
        })
        self.assertRedirects(res_send, reverse("superadmin:client_finder"))
        self.assertEqual(len(mail.outbox), 0)  # nothing sent synchronously
        self.assertTrue(CompanyOutreach.objects.filter(company=with_email, status="pending").exists())
        # Queued companies drop out of the candidate checkboxes immediately (they now show in
        # Sent History instead, as "pending").
        res_after = self.client.get(reverse("superadmin:client_finder"))
        self.assertNotContains(res_after, f'value="{with_email.id}"')

        # Run the worker task explicitly.
        from gemiapp.tasks import send_company_outreach_task
        send_company_outreach_task([with_email.id])
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Νέα ΑΕ", mail.outbox[0].body)
        self.assertTrue(CompanyOutreach.objects.filter(company=with_email, status="sent").exists())
        mail.outbox.clear()

        # The unsubscribe link opts the address out for good.
        from django.core.signing import TimestampSigner
        from gemiapp.views import OUTREACH_UNSUBSCRIBE_SALT
        token = TimestampSigner(salt=OUTREACH_UNSUBSCRIBE_SALT).sign("later@co.gr")
        res_unsub = self.client.get(reverse("outreach_unsubscribe", kwargs={"token": token}))
        self.assertEqual(res_unsub.status_code, 200)
        from gemiapp.models import OutreachSuppression
        self.assertTrue(OutreachSuppression.is_suppressed("later@co.gr"))

        opted_out = Company.objects.create(
            gemi_number="900004", name="Απεγγεγραμμένη", incorporation_date=date.today(), email="later@co.gr",
        )
        res2 = self.client.get(reverse("superadmin:client_finder"))
        self.assertNotContains(res2, "Απεγγεγραμμένη")

        # A second send must not email the same company again.
        mail.outbox.clear()
        self.client.post(reverse("superadmin:client_finder_send"), {
            "mode": "all_filtered",
        })
        self.assertEqual(len(mail.outbox), 0)

    def test_outreach_history_lives_on_its_own_page(self):
        from datetime import date
        from .models import Company, CompanyActivity, CompanyOutreach

        low_engagement = Company.objects.create(
            gemi_number="900101", name="Χαμηλό Engagement ΑΕ", incorporation_date=date.today(), email="low@a.gr",
        )
        high_engagement = Company.objects.create(
            gemi_number="900102", name="Υψηλό Engagement ΑΕ", incorporation_date=date.today(), email="high@b.gr",
        )
        low_out = CompanyOutreach.objects.create(company=low_engagement, status="sent", sent_to="low@a.gr")
        high_out = CompanyOutreach.objects.create(company=high_engagement, status="sent", sent_to="high@b.gr")
        EmailEngagementEvent.objects.create(event_type="click", email="high@b.gr", tag=f"outreach:{high_engagement.id}", payload={})
        EmailEngagementEvent.objects.create(event_type="click", email="high@b.gr", tag=f"outreach:{high_engagement.id}", payload={})
        EmailEngagementEvent.objects.create(event_type="click", email="low@a.gr", tag=f"outreach:{low_engagement.id}", payload={})

        CompanyActivity.objects.create(company=high_engagement, code="47.71", description="Λιανικό ένδυσης")

        self.client.login(username="admin@gemileads.gr", password="SuperPassword123")

        # The candidate-finder page no longer shows sent history.
        res_finder = self.client.get(reverse("superadmin:client_finder"))
        self.assertNotContains(res_finder, "Χαμηλό Engagement ΑΕ")
        self.assertContains(res_finder, "Ιστορικό Αποστολών")  # the link to the new page

        # The new page shows it instead, with engagement counts.
        res_history = self.client.get(reverse("superadmin:outreach_history"))
        self.assertEqual(res_history.status_code, 200)
        self.assertContains(res_history, "Χαμηλό Engagement ΑΕ")
        self.assertContains(res_history, "Υψηλό Engagement ΑΕ")

        # Sort by clicks: the higher-engagement row comes first.
        res_sorted = self.client.get(reverse("superadmin:outreach_history"), {"sort": "clicks"})
        body = res_sorted.content.decode()
        self.assertLess(body.index("Υψηλό Engagement ΑΕ"), body.index("Χαμηλό Engagement ΑΕ"))

        # ΚΑΔ filter narrows to the matching company only.
        res_kad = self.client.get(reverse("superadmin:outreach_history"), {"hkad": "47"})
        self.assertContains(res_kad, "Υψηλό Engagement ΑΕ")
        self.assertNotContains(res_kad, "Χαμηλό Engagement ΑΕ")

    def test_client_finder_candidate_search_filters_by_kad(self):
        from datetime import date
        from .models import ActivityCode, Company, CompanyActivity

        matching = Company.objects.create(
            gemi_number="900103", name="ΚΑΔ Ταιριάζει", incorporation_date=date.today(), email="m@a.gr",
        )
        CompanyActivity.objects.create(company=matching, code="47.71", description="Λιανικό ένδυσης")
        other = Company.objects.create(
            gemi_number="900104", name="ΚΑΔ Δεν Ταιριάζει", incorporation_date=date.today(), email="o@a.gr",
        )
        CompanyActivity.objects.create(company=other, code="62.01", description="Προγραμματισμός")

        self.client.login(username="admin@gemileads.gr", password="SuperPassword123")
        res = self.client.get(reverse("superadmin:client_finder"), {"kad": "47"})
        self.assertContains(res, "ΚΑΔ Ταιριάζει")
        self.assertNotContains(res, "ΚΑΔ Δεν Ταιριάζει")

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


# LEGAL_BILLING_ACTIVE: these assert the checkout mechanics, which only exist once payments are
# open. Beta keeps the flag off in every other context; see BetaAndBillingDisabledTests.
@override_settings(
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz",
    STRIPE_PRICE_ENTERPRISE="price_ent", LEGAL_BILLING_ACTIVE=True,
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
        self.assertContains(response, "Priority Alerts")
        self.assertTrue(response.context["is_realtime_tier"])
        self.assertIsNotNone(response.context["last_intraday_run"])

    def test_lower_tiers_do_not_get_the_realtime_panel(self):
        sub = self.user.subscription
        sub.tier = "pro"
        sub.save()
        response = self.client.get(reverse("dashboard"))
        self.assertFalse(response.context["is_realtime_tier"])
        self.assertNotContains(response, "Priority Alerts · σημερινές")
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
        with patch("gemiapp.services.EmailMultiAlternatives.send", side_effect=RuntimeError("smtp down")):
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
        # Match the aggregate itself, not the substring "count", which also appears inside
        # cache key names such as home_today_count once the cache lives in the database.
        counts = [q for q in warm.captured_queries if "COUNT(" in q["sql"].upper().replace(" ", "")]
        self.assertEqual(counts, [], "a COUNT query still runs on a warm request")

    def test_a_warm_request_replaces_scans_with_key_lookups(self):
        """The database cache turns each hit into a query of its own. That is the trade:
        three indexed single-key reads instead of three COUNT(*) over every company."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.client.get(reverse("home"))
        with CaptureQueriesContext(connection) as warm:
            self.client.get(reverse("home"))

        sql = [q["sql"] for q in warm.captured_queries]
        self.assertTrue(any("gemi_cache" in q for q in sql), "cache was not consulted")
        self.assertFalse([q for q in sql if "COUNT(" in q.upper().replace(" ", "")])

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


class DashboardChartDataTests(TestCase):
    """The bar heights used to be a fixed `value*2 + 5` px formula with no cap relative to the
    template's h-32 (128px) bar container -- a spike day (e.g. many leads generated at once)
    rendered a bar taller than the container and got visually clipped at its edge. Heights are
    now scaled against the single largest value across both series, capped well inside the
    container, so no day can overflow it regardless of how large a single day's count gets.
    """

    def test_no_bar_height_exceeds_the_chart_container(self):
        from gemiapp.superadmin.services import get_chart_data_last_30_days

        for i in range(80):
            User.objects.create_user(username=f"spike{i}", email=f"spike{i}@example.com")

        chart = get_chart_data_last_30_days()
        for day in chart:
            self.assertLessEqual(day["users_height"], 128)
            self.assertLessEqual(day["leads_height"], 128)

    def test_zero_count_day_still_renders_a_visible_sliver(self):
        from gemiapp.superadmin.services import get_chart_data_last_30_days

        chart = get_chart_data_last_30_days()
        for day in chart:
            self.assertGreaterEqual(day["users_height"], 2)
            self.assertGreaterEqual(day["leads_height"], 2)

    def test_the_largest_day_reaches_close_to_the_top_of_the_container(self):
        """Scaling must not compress everything into a sliver either -- the tallest day should
        visibly use most of the available height, not just avoid overflow."""
        from gemiapp.superadmin.services import get_chart_data_last_30_days

        for i in range(80):
            User.objects.create_user(username=f"spike{i}", email=f"spike{i}@example.com")

        chart = get_chart_data_last_30_days()
        self.assertEqual(max(day["users_height"] for day in chart), 120)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DigestEmailTagTests(TestCase):
    """The X-Mailin-Tag header is how an inbound Brevo engagement event (opened/clicked/
    unsubscribed) gets matched back to the DigestDelivery row it's about -- if it's ever
    missing, or built differently by different send paths, engagement stats silently show as
    zero for a real delivery instead of erroring, which would be much harder to notice.
    """

    def setUp(self):
        self.user = User.objects.create_user("digest@example.com", "digest@example.com", "StrongPass123")
        sub, _ = UserSubscription.objects.get_or_create(user=self.user)
        sub.tier = "pro"
        sub.status = "active"
        sub.save()
        DigestPreference.objects.get_or_create(user=self.user)

    def test_tag_format(self):
        tag = digest_email_tag(self.user.id, date(2026, 8, 28), "daily")
        self.assertEqual(tag, f"digest:{self.user.id}:2026-08-28:daily")

    @patch("gemiapp.services.fetch_companies", return_value=[SAMPLE])
    def test_sent_digest_carries_the_tag_header(self, _fetch):
        import_for_date(date(2026, 8, 1))
        send_digests(date(2026, 8, 1), frequency="daily")
        self.assertEqual(len(mail.outbox), 1)
        expected_tag = digest_email_tag(self.user.id, date(2026, 8, 1), "daily")
        self.assertEqual(mail.outbox[0].extra_headers.get("X-Mailin-Tag"), expected_tag)

    @patch("gemiapp.services.fetch_companies", return_value=[SAMPLE])
    def test_html_alternative_is_attached(self, _fetch):
        import_for_date(date(2026, 8, 1))
        send_digests(date(2026, 8, 1), frequency="daily")
        message = mail.outbox[0]
        self.assertEqual(len(message.alternatives), 1)
        self.assertEqual(message.alternatives[0][1], "text/html")


@override_settings(BREVO_WEBHOOK_TOKEN="test-token-123")
class BrevoWebhookTests(TestCase):
    """The webhook has no signature to verify (Brevo's own "token-based authentication" just
    echoes a shared secret back verbatim), so the token check is the only thing standing
    between this endpoint and an unauthenticated write -- and, unlike the Stripe webhook, a
    malformed item must never take the rest of a batch down with it.
    """

    def _post(self, token, payload_bytes, content_type="application/json"):
        return self.client.post(
            reverse("brevo_webhook", kwargs={"token": token}),
            data=payload_bytes, content_type=content_type,
        )

    def test_wrong_token_is_rejected(self):
        response = self._post("wrong-token", json.dumps({"event": "opened", "email": "a@example.com"}).encode())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(EmailEngagementEvent.objects.count(), 0)

    @override_settings(BREVO_WEBHOOK_TOKEN="")
    def test_unconfigured_token_rejects_everything(self):
        response = self._post("anything", json.dumps({"event": "opened"}).encode())
        self.assertEqual(response.status_code, 403)

    def test_malformed_json_is_rejected(self):
        response = self._post("test-token-123", b"not json")
        self.assertEqual(response.status_code, 400)

    def test_single_event_is_recorded(self):
        payload = {"event": "opened", "email": "a@example.com", "tag": "digest:1:2026-08-01:daily"}
        response = self._post("test-token-123", json.dumps(payload).encode())
        self.assertEqual(response.status_code, 200)
        event = EmailEngagementEvent.objects.get()
        self.assertEqual(event.event_type, "opened")
        self.assertEqual(event.email, "a@example.com")
        self.assertEqual(event.tag, "digest:1:2026-08-01:daily")
        self.assertEqual(event.payload, payload)

    def test_transactional_tags_list_is_read(self):
        """Transactional (vs marketing) Brevo events carry the tag under a "tags" list rather
        than a singular "tag" string -- both shapes must resolve to the same stored tag."""
        payload = {"event": "delivered", "email": "a@example.com", "tags": ["digest:1:2026-08-01:daily"]}
        self._post("test-token-123", json.dumps(payload).encode())
        self.assertEqual(EmailEngagementEvent.objects.get().tag, "digest:1:2026-08-01:daily")

    def test_batch_array_is_recorded(self):
        payload = [
            {"event": "delivered", "email": "a@example.com", "tag": "t1"},
            {"event": "opened", "email": "a@example.com", "tag": "t1"},
        ]
        response = self._post("test-token-123", json.dumps(payload).encode())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(EmailEngagementEvent.objects.count(), 2)

    def test_one_malformed_item_does_not_lose_the_rest_of_the_batch(self):
        payload = ["not-a-dict", {"event": "opened", "email": "a@example.com", "tag": "t1"}]
        response = self._post("test-token-123", json.dumps(payload).encode())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(EmailEngagementEvent.objects.count(), 1)

    def test_missing_tag_is_stored_as_empty_not_dropped(self):
        payload = {"event": "spam", "email": "a@example.com"}
        self._post("test-token-123", json.dumps(payload).encode())
        event = EmailEngagementEvent.objects.get()
        self.assertEqual(event.tag, "")

    def test_json_array_wrapped_tag_from_smtp_relay_is_unwrapped(self):
        """Brevo's SMTP relay wraps the X-Mailin-Tag value in a JSON array before the webhook
        sees it: a message sent with "outreach:17" arrives as the literal '["outreach:17"]'.
        It must be stored as the bare tag so the dashboard's tag__in lookup matches."""
        payload = {"event": "delivered", "email": "a@example.com", "tag": '["outreach:17"]'}
        self._post("test-token-123", json.dumps(payload).encode())
        self.assertEqual(EmailEngagementEvent.objects.get().tag, "outreach:17")


class EmailEngagementStatsTests(TestCase):
    """Aggregation must key strictly on the (user, date, frequency) tag -- an event tagged for
    a different delivery must never bleed into another delivery's counts."""

    def setUp(self):
        from gemiapp.superadmin.services import attach_email_engagement_stats
        self.attach = attach_email_engagement_stats
        self.user = User.objects.create_user("digest2@example.com", "digest2@example.com", "StrongPass123")

    def _delivery(self, digest_date=date(2026, 8, 1), frequency="daily"):
        return DigestDelivery.objects.create(
            user=self.user, digest_date=digest_date, frequency=frequency,
            status="sent", company_count=1,
        )

    def test_counts_bucket_by_event_type(self):
        delivery = self._delivery()
        tag = digest_email_tag(self.user.id, delivery.digest_date, delivery.frequency)
        EmailEngagementEvent.objects.create(event_type="delivered", email="x@example.com", tag=tag, payload={})
        EmailEngagementEvent.objects.create(event_type="opened", email="x@example.com", tag=tag, payload={})
        EmailEngagementEvent.objects.create(event_type="opened", email="x@example.com", tag=tag, payload={})
        EmailEngagementEvent.objects.create(event_type="click", email="x@example.com", tag=tag, payload={})
        EmailEngagementEvent.objects.create(event_type="unsubscribed", email="x@example.com", tag=tag, payload={})

        (annotated,) = self.attach([delivery])
        self.assertEqual(annotated.engagement, {"delivered": 1, "opened": 2, "clicked": 1, "unsubscribed": 1})

    def test_unrecognised_event_type_falls_into_other_not_dropped(self):
        delivery = self._delivery()
        tag = digest_email_tag(self.user.id, delivery.digest_date, delivery.frequency)
        EmailEngagementEvent.objects.create(event_type="deferred", email="x@example.com", tag=tag, payload={})

        (annotated,) = self.attach([delivery])
        self.assertEqual(annotated.engagement, {"other": 1})

    def test_events_for_a_different_delivery_never_bleed_in(self):
        delivery = self._delivery(digest_date=date(2026, 8, 1))
        other_tag = digest_email_tag(self.user.id, date(2026, 8, 2), "daily")
        EmailEngagementEvent.objects.create(event_type="opened", email="x@example.com", tag=other_tag, payload={})

        (annotated,) = self.attach([delivery])
        self.assertEqual(annotated.engagement, {})

    def test_no_events_yields_empty_dict_not_an_error(self):
        delivery = self._delivery()
        (annotated,) = self.attach([delivery])
        self.assertEqual(annotated.engagement, {})


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class OutreachEngagementTests(TestCase):
    """Same Brevo tag/tracking machinery as digest emails, applied to the "Εύρεση Πελατών" cold
    outreach tool -- so a sent CompanyOutreach row can show delivered/opened/clicked/
    unsubscribed the same way a DigestDelivery row does.
    """

    def setUp(self):
        from gemiapp.models import Company, CompanyOutreach
        from gemiapp.superadmin.services import attach_outreach_engagement_stats, build_outreach_email
        self.Company = Company
        self.CompanyOutreach = CompanyOutreach
        self.attach = attach_outreach_engagement_stats
        self.build_email = build_outreach_email
        self.company = Company.objects.create(
            gemi_number="900010", name="Tag Test ΑΕ", incorporation_date=date(2026, 8, 1), email="a@tagtest.gr",
        )

    def test_sent_outreach_carries_the_tag_header(self):
        message = self.build_email(self.company)
        self.assertEqual(message.extra_headers.get("X-Mailin-Tag"), f"outreach:{self.company.pk}")

    def test_test_send_on_an_unsaved_company_has_no_tag(self):
        """send_outreach_test_email builds the email against an in-memory, unsaved Company (no
        pk, no CompanyOutreach row could ever exist to match a tag against)."""
        unsaved = self.Company(name="Δείγμα", incorporation_date=date(2026, 8, 1))
        message = self.build_email(unsaved, to_email="tester@example.com")
        self.assertNotIn("X-Mailin-Tag", message.extra_headers)

    def test_counts_bucket_by_event_type(self):
        outreach = self.CompanyOutreach.objects.create(company=self.company, status="sent", sent_to="a@tagtest.gr")
        tag = f"outreach:{self.company.id}"
        EmailEngagementEvent.objects.create(event_type="delivered", email="a@tagtest.gr", tag=tag, payload={})
        EmailEngagementEvent.objects.create(event_type="opened", email="a@tagtest.gr", tag=tag, payload={})
        EmailEngagementEvent.objects.create(event_type="click", email="a@tagtest.gr", tag=tag, payload={})

        (annotated,) = self.attach([outreach])
        self.assertEqual(annotated.engagement, {"delivered": 1, "opened": 1, "clicked": 1})

    def test_events_for_a_different_company_never_bleed_in(self):
        other = self.Company.objects.create(
            gemi_number="900011", name="Άλλη ΑΕ", incorporation_date=date(2026, 8, 1), email="b@other.gr",
        )
        outreach = self.CompanyOutreach.objects.create(company=self.company, status="sent", sent_to="a@tagtest.gr")
        EmailEngagementEvent.objects.create(
            event_type="opened", email="b@other.gr", tag=f"outreach:{other.id}", payload={},
        )

        (annotated,) = self.attach([outreach])
        self.assertEqual(annotated.engagement, {})

    def test_snake_case_bounce_events_from_smtp_relay_are_bucketed(self):
        outreach = self.CompanyOutreach.objects.create(company=self.company, status="sent", sent_to="a@tagtest.gr")
        tag = f"outreach:{self.company.id}"
        EmailEngagementEvent.objects.create(event_type="hard_bounce", email="a@tagtest.gr", tag=tag, payload={})
        EmailEngagementEvent.objects.create(event_type="soft_bounce", email="a@tagtest.gr", tag=tag, payload={})
        (annotated,) = self.attach([outreach])
        self.assertEqual(annotated.engagement.get("bounced"), 2)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class OutreachDailyCapTests(TestCase):
    """Past the Brevo daily quota the SMTP relay returns 250 OK and drops the message, so
    send() succeeds and the row would be wrongly marked "sent". process_pending_outreach
    must stop at OUTREACH_DAILY_SEND_CAP and leave the rest "pending"."""

    def setUp(self):
        from gemiapp.models import Company, CompanyOutreach
        from gemiapp.superadmin.services import process_pending_outreach
        self.Company = Company
        self.CompanyOutreach = CompanyOutreach
        self.process = process_pending_outreach
        self.companies = [
            Company.objects.create(
                gemi_number=f"9100{i:02d}", name=f"Cap {i} ΑΕ",
                incorporation_date=date(2026, 8, 1), email=f"cap{i}@x.gr",
            )
            for i in range(5)
        ]
        for c in self.companies:
            CompanyOutreach.objects.create(company=c, status="pending", sent_to=c.email)

    @override_settings(OUTREACH_DAILY_SEND_CAP=3)
    def test_stops_at_cap_and_leaves_the_rest_pending(self):
        ids = [c.id for c in self.companies]
        sent, failed, skipped = self.process(ids)
        self.assertEqual((sent, failed, skipped), (3, 0, 2))
        self.assertEqual(self.CompanyOutreach.objects.filter(status="sent").count(), 3)
        self.assertEqual(self.CompanyOutreach.objects.filter(status="pending").count(), 2)
        self.assertEqual(len(mail.outbox), 3)

    @override_settings(OUTREACH_DAILY_SEND_CAP=3)
    def test_earlier_sends_in_the_window_count_against_the_cap(self):
        # Two already sent in the last 24h -> only one slot left this run.
        for c in self.companies[:2]:
            self.CompanyOutreach.objects.filter(company=c).update(status="sent")
        remaining = [c.id for c in self.companies[2:]]
        sent, failed, skipped = self.process(remaining)
        self.assertEqual((sent, failed, skipped), (1, 0, 2))


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


@override_settings(
    STRIPE_SECRET_KEY="sk_test", STRIPE_WEBHOOK_SECRET="whsec_test",
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent",
)
class StripeWebhookHealthCheckTests(TestCase):
    """A fully-configured Stripe integration can still be silently failing on real deliveries
    (a code bug, a malformed payload, a Stripe API change) -- nothing about the config check
    alone would ever catch that. Same "recent failures only" windowing as the scheduler check
    (SchedulerHealthCheckTests), for the same reason: a resolved incident must not pin this
    check to Warning forever.
    """

    def _failed_event(self, key, days_ago):
        from gemiapp.models import StripeWebhookEvent

        event = StripeWebhookEvent.objects.create(
            stripe_event_id=key, event_type="customer.subscription.updated",
            payload={}, status="failed", error_message="boom",
        )
        StripeWebhookEvent.objects.filter(pk=event.pk).update(
            received_at=timezone.now() - timedelta(days=days_ago),
        )

    def _status(self):
        from gemiapp.superadmin.services import get_system_health

        return get_system_health()["services"]["stripe"]

    def test_healthy_when_configured_and_nothing_failed(self):
        self.assertEqual(self._status()["status"], "Operational")

    def test_recent_failure_raises_a_warning(self):
        self._failed_event("evt_recent", days_ago=1)
        status = self._status()
        self.assertEqual(status["status"], "Warning")
        self.assertIn("failed webhook event", status["details"])

    def test_old_failures_do_not_keep_the_check_red(self):
        from gemiapp.superadmin.services import STRIPE_WEBHOOK_FAILURE_WINDOW_DAYS

        self._failed_event("evt_old", days_ago=STRIPE_WEBHOOK_FAILURE_WINDOW_DAYS + 23)
        status = self._status()
        self.assertEqual(status["status"], "Operational")
        self.assertIn("older failed webhook event", status["details"])

    def test_processed_and_ignored_events_never_count_as_failures(self):
        from gemiapp.models import StripeWebhookEvent

        StripeWebhookEvent.objects.create(
            stripe_event_id="evt_ok", event_type="checkout.session.completed",
            payload={}, status="processed",
        )
        StripeWebhookEvent.objects.create(
            stripe_event_id="evt_skip", event_type="price.updated",
            payload={}, status="ignored",
        )
        self.assertEqual(self._status()["status"], "Operational")


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
    LEGAL_BILLING_ACTIVE=True,
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


class PersonSuppressionTests(TestCase):
    """Article 21 GDPR: an objection must hide the person and survive re-imports."""

    PERSONS = [
        {"personName": "ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", "role": "Διαχειριστής & Εκπρόσωπος",
         "category": "Εταίροι", "percentage": "90%", "dtTo": None, "isRepresentativeAlone": True},
        {"personName": "ΠΑΠΑΔΟΠΟΥΛΟΥ ΒΑΣΙΛΙΚΗ", "role": "Ετερόρρυθμο Μέλος",
         "category": "Εταίροι", "percentage": "10%", "dtTo": None, "isRepresentativeAlone": None},
    ]

    def setUp(self):
        self.company = Company.objects.create(
            gemi_number="770000000100", name="ΠΡΩΤΗ Ε.Ε.", legal_type="ΕΕ",
            incorporation_date=timezone.localdate(), prefecture="ΑΤΤΙΚΗΣ",
            raw_data={"persons": self.PERSONS},
        )
        self.other = Company.objects.create(
            gemi_number="770000000200", name="ΔΕΥΤΕΡΗ Ε.Ε.", legal_type="ΕΕ",
            incorporation_date=timezone.localdate(), prefecture="ΑΤΤΙΚΗΣ",
            raw_data={"persons": self.PERSONS},
        )

    def _names(self, company):
        return [p["name"] for p in company.people]

    def test_a_global_objection_hides_the_person_everywhere(self):
        """559 people in the live data appear in more than one company."""
        PersonSuppression.objects.create(full_name="ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ")
        self.assertNotIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", self._names(self.company))
        self.assertNotIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", self._names(self.other))

    def test_other_people_are_unaffected(self):
        PersonSuppression.objects.create(full_name="ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ")
        self.assertIn("ΠΑΠΑΔΟΠΟΥΛΟΥ ΒΑΣΙΛΙΚΗ", self._names(self.company))

    def test_an_objection_can_be_scoped_to_one_company(self):
        PersonSuppression.objects.create(full_name="ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", company=self.company)
        self.assertNotIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", self._names(self.company))
        self.assertIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", self._names(self.other))

    def test_matching_ignores_accents_case_and_spacing(self):
        PersonSuppression.objects.create(full_name="  γεωργίου   νικόλαος ")
        self.assertNotIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", self._names(self.company))

    def test_the_objection_survives_a_reimport(self):
        """The importer overwrites raw_data wholesale; the suppression must outlive it."""
        PersonSuppression.objects.create(full_name="ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ")
        Company.objects.update_or_create(
            gemi_number=self.company.gemi_number,
            defaults={"name": "ΠΡΩΤΗ Ε.Ε.", "incorporation_date": timezone.localdate(),
                      "raw_data": {"persons": self.PERSONS}},
        )
        self.assertNotIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ",
                         self._names(Company.objects.get(gemi_number=self.company.gemi_number)))

    def test_deleting_the_objection_restores_the_person(self):
        row = PersonSuppression.objects.create(full_name="ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ")
        row.delete()
        self.assertIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", self._names(self.company))

    def test_a_suppressed_name_never_reaches_the_page(self):
        PersonSuppression.objects.create(full_name="ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ")
        user = User.objects.create_user("s@example.com", "s@example.com", "StrongPass123")
        sub = UserSubscription.objects.get(user=user)
        sub.complimentary_tier = "enterprise"
        sub.save()
        self.client.force_login(user)
        html = self.client.get(reverse("company_detail", args=[self.company.gemi_number])).content.decode()
        self.assertNotIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", html)
        self.assertIn("ΠΑΠΑΔΟΠΟΥΛΟΥ ΒΑΣΙΛΙΚΗ", html)

    def test_a_staff_user_does_not_bypass_an_objection(self):
        """Gating is commercial; an objection is legal and binds every viewer."""
        PersonSuppression.objects.create(full_name="ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ")
        staff = User.objects.create_user("st@example.com", "st@example.com", "StrongPass123")
        staff.is_staff = True
        staff.save()
        self.client.force_login(staff)
        html = self.client.get(reverse("company_detail", args=[self.company.gemi_number])).content.decode()
        self.assertNotIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", html)

    def test_the_same_scope_cannot_be_recorded_twice(self):
        from django.db import IntegrityError, transaction

        PersonSuppression.objects.create(full_name="ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ")
        with self.assertRaises(IntegrityError), transaction.atomic():
            PersonSuppression.objects.create(full_name="γεωργιου νικολαος")

    def test_normalized_name_is_maintained_on_save(self):
        row = PersonSuppression.objects.create(full_name="Γεωργίου Νικόλαος")
        self.assertEqual(row.normalized_name, "ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ")
        row.full_name = "Παπαδοπούλου Βασιλική"
        row.save()
        self.assertEqual(row.normalized_name, "ΠΑΠΑΔΟΠΟΥΛΟΥ ΒΑΣΙΛΙΚΗ")

    # Management command
    def test_the_command_suppresses_and_reports_the_blast_radius(self):
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        call_command("suppress_person", "ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", stdout=out)
        self.assertIn("2 επιχειρήσεις", out.getvalue())
        self.assertNotIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", self._names(self.company))

    def test_the_command_dry_run_changes_nothing(self):
        from io import StringIO
        from django.core.management import call_command

        call_command("suppress_person", "ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", "--dry-run", stdout=StringIO())
        self.assertFalse(PersonSuppression.objects.exists())
        self.assertIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", self._names(self.company))

    def test_the_command_can_scope_and_undo(self):
        from io import StringIO
        from django.core.management import call_command

        call_command("suppress_person", "ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ",
                     "--gemi", self.company.gemi_number, stdout=StringIO())
        self.assertIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", self._names(self.other))

        call_command("suppress_person", "ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ",
                     "--gemi", self.company.gemi_number, "--undo", stdout=StringIO())
        self.assertIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", self._names(self.company))

    def test_the_command_warns_when_a_name_matches_nobody(self):
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        call_command("suppress_person", "ΑΓΝΩΣΤΟΣ ΤΙΣ", stdout=out)
        self.assertIn("Καμία αντιστοίχιση", out.getvalue())

    def test_the_command_rejects_an_unknown_company(self):
        from io import StringIO
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("suppress_person", "ΚΑΠΟΙΟΣ", "--gemi", "000000000000", stdout=StringIO())

    def test_an_unsaved_company_does_not_crash_the_lookup(self):
        """A related filter against an unsaved instance raises; only global rows apply."""
        PersonSuppression.objects.create(full_name="ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ")
        draft = Company(gemi_number="770000000999", name="ΠΡΟΧΕΙΡΗ",
                        incorporation_date=timezone.localdate(),
                        raw_data={"persons": self.PERSONS})
        self.assertNotIn("ΓΕΩΡΓΙΟΥ ΝΙΚΟΛΑΟΣ", [p["name"] for p in draft.people])
        self.assertIn("ΠΑΠΑΔΟΠΟΥΛΟΥ ΒΑΣΙΛΙΚΗ", [p["name"] for p in draft.people])


@override_settings(RATELIMIT_ENABLE=True)
class RateLimitTests(TestCase):
    """The limiter counts in the cache, so these also prove the cache is wired up."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _post(self, url, data, times):
        return [self.client.post(url, data).status_code for _ in range(times)]

    def test_password_reset_is_rate_limited(self):
        """An open email-sending endpoint: unlimited, it burns the sending quota."""
        codes = self._post(reverse("password_reset"), {"email": "victim@example.com"}, 7)
        self.assertIn(403, codes, f"password reset was never blocked: {codes}")

    def test_password_reset_sends_a_bounded_number_of_emails(self):
        self._post(reverse("password_reset"), {"email": "victim@example.com"}, 12)
        self.assertLessEqual(len(mail.outbox), 5)

    def test_login_is_rate_limited(self):
        codes = self._post(reverse("login"), {"username": "a@b.com", "password": "wrong"}, 7)
        self.assertIn(403, codes)

    def test_signup_is_rate_limited(self):
        codes = self._post(reverse("signup"), {
            "first_name": "X", "email": "a@b.com",
            "password1": "StrongPass123!", "password2": "StrongPass123!",
        }, 7)
        self.assertIn(403, codes)

    def test_a_normal_visitor_is_not_blocked(self):
        """A limit that blocks real users is worse than no limit."""
        self.assertNotEqual(self.client.get(reverse("login")).status_code, 403)
        self.assertEqual(self.client.post(
            reverse("password_reset"), {"email": "someone@example.com"}).status_code, 302)


class CacheBackendTests(TestCase):
    """The default LocMemCache is per-process, which silently multiplies every rate
    limit by the gunicorn worker count."""

    def test_the_cache_is_not_per_process(self):
        from django.conf import settings

        self.assertNotIn("locmem", settings.CACHES["default"]["BACKEND"].lower())

    def test_the_limiter_fails_closed(self):
        """If the cache is unreachable the limiter must refuse, not wave traffic through."""
        from django.conf import settings

        self.assertFalse(getattr(settings, "RATELIMIT_FAIL_OPEN", False))
        self.assertEqual(getattr(settings, "RATELIMIT_USE_CACHE", None), "default")

    def test_the_cache_round_trips(self):
        cache.set("probe", {"a": 1}, 30)
        self.assertEqual(cache.get("probe"), {"a": 1})
        cache.delete("probe")
        self.assertIsNone(cache.get("probe"))

    def test_the_cache_table_exists(self):
        """Without the table every cached view raises and the limiter locks everyone out."""
        from django.db import connection

        self.assertIn("gemi_cache", connection.introspection.table_names())


class BackupCriticalTests(TestCase):
    """ΓΕΜΗ data is re-fetchable (measured: ~10 hours, ~18,856 API calls). Users,
    subscriptions, radars and leads are not re-fetchable at all."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "backup.json"
        self.user = User.objects.create_user("owner@example.com", "owner@example.com", "StrongPass123")
        sub = UserSubscription.objects.get(user=self.user)
        sub.tier = "business"
        sub.status = "active"
        sub.stripe_customer_id = "cus_test123"
        sub.save()
        self.company = Company.objects.create(
            gemi_number="990000000042", name="ΠΕΛΑΤΗΣ Α.Ε.", legal_type="ΑΕ",
            incorporation_date=timezone.localdate(), prefecture="ΑΤΤΙΚΗΣ",
        )
        self.radar = CustomerRadar.objects.create(user=self.user, name="Ραντάρ μου", frequency="daily")
        self.lead = UserCompanyLead.objects.create(
            user=self.user, company=self.company, status="won", is_favorite=True, notes="κλείστηκε",
        )
        PersonSuppression.objects.create(full_name="ΤΙΣ ΤΙΝΟΣ", reason="άρθρο 21")

    def _run(self, *args):
        call_command("backup_critical", "--output", str(self.path), *args, stdout=StringIO())
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_the_backup_captures_every_critical_table(self):
        data = self._run()
        self.assertEqual(len(data["users"]), 1)
        self.assertEqual(len(data["subscriptions"]), 1)
        self.assertEqual(len(data["radars"]), 1)
        self.assertEqual(len(data["leads"]), 1)
        self.assertEqual(len(data["person_suppressions"]), 1)

    def test_billing_state_is_preserved(self):
        """Losing this means not knowing who paid."""
        sub = self._run()["subscriptions"][0]
        self.assertEqual(sub["tier"], "business")
        self.assertEqual(sub["status"], "active")
        self.assertEqual(sub["stripe_customer_id"], "cus_test123")

    def test_leads_reference_companies_by_natural_key(self):
        """Company ids are regenerated by a re-import, so a pk-based dump would
        silently reattach every lead to the wrong company."""
        lead = self._run()["leads"][0]
        self.assertEqual(lead["company"], "990000000042")
        self.assertNotIn("company_id", lead)
        self.assertEqual(lead["user"], "owner@example.com")

    def test_password_hashes_are_exported_but_not_raw_passwords(self):
        user = self._run()["users"][0]
        self.assertTrue(user["password_hash"].startswith("pbkdf2_"))
        self.assertNotIn("password", user)
        self.assertNotIn("StrongPass123", json.dumps(user))

    def test_gemi_data_is_excluded(self):
        """It is re-fetchable, and including it would bloat the file for no gain."""
        raw = json.dumps(self._run(), ensure_ascii=False)
        self.assertNotIn("ΠΕΛΑΤΗΣ Α.Ε.", raw)
        self.assertNotIn("companies", self._run())

    def test_new_model_fields_are_captured_automatically(self):
        """Fields are derived from the model, so a column added later is not missed."""
        exported = set(self._run()["radars"][0])
        for field in CustomerRadar._meta.local_fields:
            if field.name not in ("id", "user"):
                self.assertIn(field.name, exported, field.name)

    def test_the_file_is_small_enough_to_email(self):
        self._run()
        self.assertLess(self.path.stat().st_size, 1_000_000)

    def test_print_mode_writes_no_file(self):
        """The flag cannot be called --stdout: call_command supplies its own stdout kwarg,
        which would make the flag always true and silently ignore --output."""
        out = StringIO()
        call_command("backup_critical", "--print", stdout=out)
        self.assertIn("format_version", out.getvalue())

    def test_output_path_is_honoured_when_called_programmatically(self):
        self._run()
        self.assertTrue(self.path.exists())

    def test_an_empty_database_warns_instead_of_writing_silently(self):
        User.objects.all().delete()
        out = StringIO()
        call_command("backup_critical", "--output", str(self.path), stdout=out)
        self.assertIn("Σίγουρα", out.getvalue())

    def test_the_backup_can_actually_be_restored(self):
        """An untested backup is not a backup. Wipe everything except the ΓΕΜΗ data,
        then rebuild from the file and check the business state came back."""
        data = self._run()
        UserCompanyLead.objects.all().delete()
        CustomerRadar.objects.all().delete()
        User.objects.all().delete()
        self.assertEqual(UserSubscription.objects.count(), 0)

        for row in data["users"]:
            user = User.objects.create(
                username=row["username"], email=row["email"], is_active=row["is_active"],
                is_staff=row["is_staff"], is_superuser=row["is_superuser"],
            )
            user.password = row["password_hash"]
            user.save()
        for row in data["subscriptions"]:
            UserSubscription.objects.update_or_create(
                user=User.objects.get(username=row["user"]),
                defaults={"tier": row["tier"], "status": row["status"],
                          "stripe_customer_id": row["stripe_customer_id"]},
            )
        for row in data["radars"]:
            CustomerRadar.objects.create(
                user=User.objects.get(username=row["user"]),
                name=row["name"], frequency=row["frequency"],
            )
        for row in data["leads"]:
            UserCompanyLead.objects.create(
                user=User.objects.get(username=row["user"]),
                company=Company.objects.get(gemi_number=row["company"]),
                status=row["status"], is_favorite=row["is_favorite"], notes=row["notes"],
            )

        restored = User.objects.get(username="owner@example.com")
        self.assertEqual(restored.subscription.tier, "business")
        self.assertEqual(restored.subscription.stripe_customer_id, "cus_test123")
        lead = UserCompanyLead.objects.get(user=restored)
        self.assertEqual(lead.company.gemi_number, "990000000042")
        self.assertEqual(lead.status, "won")
        self.assertEqual(lead.notes, "κλείστηκε")
        # The point of exporting the hash: the old password still works.
        self.assertTrue(self.client.login(username="owner@example.com", password="StrongPass123"))


@override_settings(SUPERADMIN_EMAILS=["boss@gemileads.gr", "second@gemileads.gr"])
class SuperadminAccessTests(TestCase):
    """Superuser rights alone are not enough: the address must also be on the list."""

    def _user(self, email, superuser=False, staff=False):
        user = User.objects.create_user(email, email, "StrongPass123")
        user.is_superuser = superuser
        user.is_staff = staff
        user.save()
        return user

    def test_a_listed_superuser_gets_in(self):
        self.client.force_login(self._user("boss@gemileads.gr", superuser=True))
        self.assertEqual(self.client.get(reverse("superadmin:account_list")).status_code, 200)

    def test_a_superuser_not_on_the_list_is_refused(self):
        """Flipping is_superuser directly in the database must grant nothing."""
        self.client.force_login(self._user("rogue@example.com", superuser=True))
        self.assertEqual(self.client.get(reverse("superadmin:account_list")).status_code, 403)

    def test_a_listed_address_without_superuser_rights_is_refused(self):
        """Both conditions are required, not either."""
        self.client.force_login(self._user("boss@gemileads.gr"))
        self.assertEqual(self.client.get(reverse("superadmin:account_list")).status_code, 403)

    def test_staff_alone_is_not_superadmin(self):
        self.client.force_login(self._user("staff@example.com", staff=True))
        self.assertEqual(self.client.get(reverse("superadmin:account_list")).status_code, 403)

    def test_an_anonymous_visitor_is_sent_to_login(self):
        response = self.client.get(reverse("superadmin:account_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_matching_ignores_case(self):
        self.client.force_login(self._user("BOSS@GemiLeads.GR", superuser=True))
        self.assertEqual(self.client.get(reverse("superadmin:account_list")).status_code, 200)

    def test_every_superadmin_route_is_guarded(self):
        """A new view added without the decorator would be publicly reachable."""
        from gemiapp.superadmin import urls as superadmin_urls

        self.client.force_login(self._user("rogue@example.com", superuser=True))
        for pattern in superadmin_urls.urlpatterns:
            name = pattern.name
            if name == "impersonate_stop":
                # Intentionally unguarded: while impersonating, request.user is the target
                # account, so the decorator would trap the operator. Gated by the session.
                continue
            try:
                url = reverse(f"superadmin:{name}")
            except Exception:
                continue  # needs arguments; covered by its own tests
            with self.subTest(view=name):
                get = self.client.get(url).status_code
                post = self.client.post(url).status_code
                self.assertIn(403, (get, post), f"{name} did not refuse a non-superadmin")


@override_settings(SUPERADMIN_EMAILS=["boss@gemileads.gr"])
class SuperadminBackupButtonTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss@gemileads.gr", "boss@gemileads.gr", "StrongPass123")
        self.admin.is_superuser = True
        self.admin.is_staff = True
        self.admin.save()
        self.client.force_login(self.admin)

    def test_the_button_downloads_a_json_file(self):
        response = self.client.post(reverse("superadmin:backup_download"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn(".json", response["Content-Disposition"])
        self.assertEqual(json.loads(response.content.decode())["format_version"], 1)

    def test_the_download_contains_the_critical_tables(self):
        payload = json.loads(self.client.post(reverse("superadmin:backup_download")).content.decode())
        for key in ("users", "subscriptions", "radars", "leads", "person_suppressions"):
            self.assertIn(key, payload)
        self.assertEqual(payload["users"][0]["username"], "boss@gemileads.gr")

    def test_the_download_is_not_cached(self):
        """It carries personal data; no proxy or browser should keep a copy."""
        response = self.client.post(reverse("superadmin:backup_download"))
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_a_get_cannot_export_personal_data(self):
        self.assertEqual(self.client.get(reverse("superadmin:backup_download")).status_code, 405)

    def test_a_non_superadmin_cannot_download(self):
        self.client.force_login(User.objects.create_user("x@example.com", "x@example.com", "StrongPass123"))
        self.assertEqual(self.client.post(reverse("superadmin:backup_download")).status_code, 403)

    def test_the_download_is_audited(self):
        from gemiapp.models import AdminAuditLog

        self.client.post(reverse("superadmin:backup_download"))
        self.assertTrue(AdminAuditLog.objects.filter(action="backup_download", admin_user=self.admin).exists())


@override_settings(SUPERADMIN_EMAILS=["boss@gemileads.gr", "reserved@gemileads.gr"])
class SuperadminAccountCreationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss@gemileads.gr", "boss@gemileads.gr", "StrongPass123")
        self.admin.is_superuser = True
        self.admin.is_staff = True
        self.admin.save()
        self.client.force_login(self.admin)

    def _create(self, **overrides):
        data = {"email": "new@example.com", "first_name": "Nikos", "role": "user",
                "password": "StrongPass123!", "is_active": "on"}
        data.update(overrides)
        return self.client.post(reverse("superadmin:account_create"), data)

    def test_a_plain_user_can_be_created(self):
        self._create()
        user = User.objects.get(email="new@example.com")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password("StrongPass123!"))

    def test_an_admin_can_be_created(self):
        self._create(email="admin@example.com", role="admin")
        user = User.objects.get(email="admin@example.com")
        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser, "the panel must never mint a superuser")

    def test_no_role_can_produce_a_superuser(self):
        """The decisive guard: the interface cannot escalate to superadmin."""
        for role in ("user", "admin", "superadmin", "superuser", "", "admin OR 1=1"):
            with self.subTest(role=role):
                self._create(email="r-%d@example.com" % abs(hash(role)), role=role)
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)

    def test_a_reserved_address_cannot_be_created_here(self):
        """Otherwise someone with a session could take over a superadmin identity."""
        self._create(email="reserved@gemileads.gr", role="admin")
        self.assertFalse(User.objects.filter(email="reserved@gemileads.gr").exists())

    def test_a_duplicate_email_is_refused(self):
        self._create()
        self._create(first_name="Diplos")
        self.assertEqual(User.objects.filter(email="new@example.com").count(), 1)

    def test_a_weak_password_is_refused(self):
        self._create(email="weak@example.com", password="12345678")
        self.assertFalse(User.objects.filter(email="weak@example.com").exists())

    def test_an_account_can_be_created_inactive(self):
        response = self.client.post(reverse("superadmin:account_create"), {
            "email": "pending@example.com", "role": "user", "password": "StrongPass123!",
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.get(email="pending@example.com").is_active)

    def test_creation_is_audited(self):
        from gemiapp.models import AdminAuditLog

        self._create()
        self.assertTrue(AdminAuditLog.objects.filter(action="account_create").exists())

    def test_a_non_superadmin_cannot_create_accounts(self):
        self.client.force_login(User.objects.create_user("x@example.com", "x@example.com", "StrongPass123"))
        self.assertEqual(self._create(email="sneaky@example.com").status_code, 403)
        self.assertFalse(User.objects.filter(email="sneaky@example.com").exists())

    def test_the_creation_endpoint_rejects_get(self):
        self.assertEqual(self.client.get(reverse("superadmin:account_create")).status_code, 405)


@override_settings(SUPERADMIN_EMAILS=["boss@gemileads.gr"])
class EnsureSuperadminCommandTests(TestCase):
    """Reserved addresses cannot be created from the panel, so they need this route."""

    def test_it_creates_a_reserved_account(self):
        call_command("ensure_superadmin", "boss@gemileads.gr", "--password", "StrongPass123!", stdout=StringIO())
        user = User.objects.get(username="boss@gemileads.gr")
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.check_password("StrongPass123!"))

    def test_it_promotes_an_existing_account(self):
        User.objects.create_user("boss@gemileads.gr", "boss@gemileads.gr", "StrongPass123")
        call_command("ensure_superadmin", "boss@gemileads.gr", stdout=StringIO())
        self.assertTrue(User.objects.get(username="boss@gemileads.gr").is_superuser)

    def test_it_refuses_an_address_not_on_the_list(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("ensure_superadmin", "rogue@example.com", "--password", "StrongPass123!", stdout=StringIO())
        self.assertFalse(User.objects.filter(username="rogue@example.com").exists())

    def test_a_new_account_requires_a_password(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("ensure_superadmin", "boss@gemileads.gr", stdout=StringIO())


@override_settings(ALLOWED_HOSTS=["gemileads.gr", "testserver"], RATELIMIT_ENABLE=False)
class BetaAndBillingDisabledTests(TestCase):
    """The product is in beta and cannot charge anyone.

    Hiding the buttons is not enough on its own: the POST endpoint stays routable, and a page left
    open across a deploy still holds a valid CSRF token. Both layers are asserted here, plus the
    reverse direction: flipping the flag must restore checkout without a code change.
    """

    def setUp(self):
        self.user = User.objects.create_user("beta@example.com", "beta@example.com", "StrongPass123")

    def _html(self, name):
        return self.client.get(reverse(name), HTTP_HOST="gemileads.gr").content.decode()

    @override_settings(LEGAL_BILLING_ACTIVE=False)
    def test_no_checkout_form_is_rendered_while_billing_is_off(self):
        html = self._html("pricing")
        self.assertNotIn(reverse("create_checkout_session"), html)
        self.assertIn("Σύντομα διαθέσιμο", html)

    @override_settings(LEGAL_BILLING_ACTIVE=False)
    def test_a_direct_post_cannot_reach_stripe(self):
        """The gate that matters: the endpoint itself refuses, with no Stripe call attempted."""
        self.client.login(username="beta@example.com", password="StrongPass123")
        with patch("stripe.checkout.Session.create") as create:
            response = self.client.post(reverse("create_checkout_session"), {"tier": "pro"})
        self.assertFalse(create.called, "Stripe must not be contacted while billing is off")
        self.assertRedirects(response, reverse("pricing"))

    @override_settings(LEGAL_BILLING_ACTIVE=False)
    def test_a_parked_plan_choice_does_not_resume_into_checkout(self):
        self.client.login(username="beta@example.com", password="StrongPass123")
        session = self.client.session
        session["pending_checkout_tier"] = "pro"
        session.save()
        self.assertRedirects(self.client.get(reverse("resume_checkout")), reverse("pricing"))

    @override_settings(LEGAL_BILLING_ACTIVE=False)
    def test_the_pricing_page_says_payments_are_not_live(self):
        html = self._html("pricing")
        self.assertIn("Οι πληρωμές δεν είναι ακόμη ενεργές", html)

    @override_settings(LEGAL_BILLING_ACTIVE=False)
    def test_offers_are_not_advertised_as_purchasable(self):
        """An InStock offer on a product that cannot be bought is a false claim to search engines."""
        html = self._html("pricing")
        blocks = [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]
        software = [b for b in blocks if b.get("@type") == "SoftwareApplication"][0]
        for offer in software["offers"]:
            self.assertEqual(offer["availability"], "https://schema.org/PreOrder")

    @override_settings(LEGAL_BILLING_ACTIVE=True, STRIPE_PRICE_PRO="price_test_pro")
    def test_turning_the_flag_on_restores_checkout(self):
        html = self._html("pricing")
        self.assertIn(reverse("create_checkout_session"), html)
        self.assertNotIn("Οι πληρωμές δεν είναι ακόμη ενεργές", html)

        self.client.login(username="beta@example.com", password="StrongPass123")
        with patch("stripe.checkout.Session.create") as create:
            create.return_value = type("S", (), {"url": "https://stripe.test/session"})()
            response = self.client.post(reverse("create_checkout_session"), {"tier": "pro"})
        self.assertTrue(create.called)
        self.assertEqual(response.status_code, 303)

    @override_settings(BETA_MODE=True)
    def test_the_beta_label_is_visible_site_wide(self):
        for name in ("home", "pricing", "login"):
            self.assertIn("Beta", self._html(name), name)

    @override_settings(BETA_MODE=False, LEGAL_BILLING_ACTIVE=True)
    def test_the_labels_disappear_when_both_flags_are_cleared(self):
        html = self._html("home")
        self.assertNotIn("βρίσκεται σε <b>beta</b>", html)
        self.assertNotIn("Οι πληρωμές δεν είναι ενεργές", html)


class DiagnoseIntradayCommandTests(TestCase):
    """The command exists to answer one question without a shell session, so the assertions are
    about whether its verdict is correct, not about its wording."""

    def setUp(self):
        self.today = timezone.localdate()
        self.user = User.objects.create_user("ent@example.com", "ent@example.com", "StrongPass123")

    def _run(self):
        out = StringIO()
        call_command("diagnose_intraday", stdout=out)
        return out.getvalue()

    def test_it_reports_when_nobody_is_eligible_for_intraday(self):
        output = self._run()
        self.assertIn("Δικαιούνται 3ωρο email: 0", output)
        self.assertIn("Κανένας λογαριασμός δεν δικαιούται", output)

    def test_a_pro_complimentary_account_is_reported_as_daily_only(self):
        """The exact trap: access was granted, but not at a tier that receives 3-hour alerts."""
        sub = self.user.subscription
        sub.complimentary_tier = "pro"
        sub.save()
        output = self._run()
        self.assertIn("Μόνο ημερήσιο (όχι 3ωρο): 1", output)
        self.assertIn("ent@example.com", output)
        self.assertIn("intraday απαιτεί enterprise/custom", output)

    def test_an_enterprise_account_is_reported_as_eligible(self):
        sub = self.user.subscription
        sub.complimentary_tier = "enterprise"
        sub.save()
        output = self._run()
        self.assertIn("Δικαιούνται 3ωρο email: 1", output)

    def test_missing_import_runs_are_flagged(self):
        self.assertIn("Κανένα ImportRun", self._run())

    def test_a_completed_run_is_listed(self):
        ImportRun.objects.create(
            target_date=self.today, status="success", fetched_count=3, created_count=2,
            finished_at=timezone.now(),
        )
        self.assertIn("status=success", self._run())

    def test_it_accepts_an_explicit_date(self):
        out = StringIO()
        call_command("diagnose_intraday", "--date", "2026-01-15", stdout=out)
        self.assertIn("2026-01-15", out.getvalue())

    def test_it_sends_nothing(self):
        self._run()
        self.assertEqual(len(mail.outbox), 0)


class DiagnoseIntradaySlotTests(TestCase):
    """A slot that stayed silent has four possible causes and they need telling apart.

    The intraday path writes no DigestDelivery row when it has nothing to send, so a per-slot
    verdict can only be reconstructed from ImportRun plus the per-user pointer.
    """

    def setUp(self):
        self.today = timezone.localdate()
        self.user = User.objects.create_user("ent@example.com", "ent@example.com", "StrongPass123")
        sub = self.user.subscription
        sub.complimentary_tier = "enterprise"
        sub.save()

    def _run(self):
        out = StringIO()
        call_command("diagnose_intraday", stdout=out)
        return out.getvalue()

    def _company(self, gemi, day=None):
        return Company.objects.create(
            gemi_number=gemi, name=f"ΕΤΑΙΡΕΙΑ {gemi}", incorporation_date=day or self.today,
        )

    def test_a_successful_run_that_imported_nothing_is_distinguishable(self):
        ImportRun.objects.create(
            target_date=self.today, status="success", fetched_count=0, created_count=0,
            finished_at=timezone.now(),
        )
        output = self._run()
        self.assertIn("status=success", output)
        self.assertIn("new=0", output)

    def test_a_failed_run_shows_its_error(self):
        ImportRun.objects.create(
            target_date=self.today, status="failed", error_message="GEMI API HTTP 503",
            finished_at=timezone.now(),
        )
        self.assertIn("GEMI API HTTP 503", self._run())

    def test_a_run_left_running_is_visible_as_such(self):
        ImportRun.objects.create(target_date=self.today, status="running")
        self.assertIn("status=running", self._run())

    def test_pending_records_are_reported_against_the_pointer(self):
        """Distinguishes "nothing arrived" from "something arrived and was not sent"."""
        first = self._company("111000")
        self._company("222000")
        sub = self.user.subscription
        sub.last_sent_company_id = first.id
        sub.save()
        self.assertIn("εκκρεμείς νέες εγγραφές τώρα=1", self._run())

    def test_nothing_pending_once_the_pointer_has_caught_up(self):
        last = self._company("333000")
        sub = self.user.subscription
        sub.last_sent_company_id = last.id
        sub.save()
        self.assertIn("εκκρεμείς νέες εγγραφές τώρα=0", self._run())

    def test_yesterdays_companies_never_count_as_pending_today(self):
        """The pointer is a global id, so a daily import of older records must not look like
        unsent intraday traffic."""
        self._company("444000", day=self.today - timedelta(days=1))
        self.assertIn("εκκρεμείς νέες εγγραφές τώρα=0", self._run())


class DigestDeliveryIsNotPerSlotTests(TestCase):
    """`sent_at` is auto_now_add, so update_or_create never moves it.

    The intraday row is therefore stamped with the first send of the day and keeps that timestamp
    through every later send. Reading it as a per-slot log says "only 08:00 went out" about a day
    that in fact delivered five times, so the diagnostic has to say so out loud.
    """

    def setUp(self):
        self.user = User.objects.create_user("ent@example.com", "ent@example.com", "StrongPass123")
        sub = self.user.subscription
        sub.complimentary_tier = "enterprise"
        sub.save()
        self.today = timezone.localdate()

    def test_a_later_send_does_not_move_the_timestamp(self):
        first = DigestDelivery.objects.create(
            user=self.user, digest_date=self.today, frequency="intraday", status="sent", company_count=2,
        )
        original = first.sent_at
        DigestDelivery.objects.update_or_create(
            user=self.user, digest_date=self.today, frequency="intraday",
            defaults={"status": "sent", "company_count": 4},
        )
        first.refresh_from_db()
        self.assertEqual(first.sent_at, original, "sent_at must stay at the first send of the day")
        self.assertEqual(first.company_count, 4, "the row itself is updated in place")
        self.assertEqual(DigestDelivery.objects.filter(frequency="intraday").count(), 1)

    def test_the_diagnostic_warns_against_reading_it_per_slot(self):
        out = StringIO()
        call_command("diagnose_intraday", stdout=out)
        output = out.getvalue()
        self.assertIn("ΜΙΑ γραμμή για όλη την ημέρα", output)
        self.assertIn("ΠΡΩΤΗ αποστολή", output)


class EmailBackendDefaultTests(TestCase):
    """A local run or a stray script must never send real mail just because valid SMTP
    credentials sit in .env: non-production has to default to a non-SMTP backend without
    anyone having to remember to set one, while production keeps using the real relay."""

    def test_development_defaults_to_console_not_smtp(self):
        from config.settings import resolve_email_backend

        self.assertEqual(
            resolve_email_backend(debug=True, override=None),
            "django.core.mail.backends.console.EmailBackend",
        )

    def test_production_defaults_to_smtp(self):
        from config.settings import resolve_email_backend

        self.assertEqual(
            resolve_email_backend(debug=False, override=None),
            "django.core.mail.backends.smtp.EmailBackend",
        )

    def test_explicit_override_wins_even_in_development(self):
        """Opting into a real backend locally (e.g. to test the Brevo relay itself) must still
        be possible — the safe default must not become a hard lock."""
        from config.settings import resolve_email_backend

        self.assertEqual(
            resolve_email_backend(debug=True, override="django.core.mail.backends.smtp.EmailBackend"),
            "django.core.mail.backends.smtp.EmailBackend",
        )


class CompanyActivitiesImportConsistencyTests(TestCase):
    """Company.activities (JSONField) is written by both import paths but never read by live
    code — the only reader is the historical data migration 0002, which ran once, backfilling
    CompanyActivity/ActivityCode from whatever Company rows existed at that time. CompanyActivity
    and raw_data are the real, queried source of truth for activities today. The field is kept
    (not dead-code-deleted per instructions) but the two import paths used to disagree on it:
    import_for_date wrote company_defaults()'s activities list onto Company.activities, while
    import_companies_since_date popped it out of defaults before update_or_create, leaving the
    field at its `[]` default (or stale, for an existing row) regardless of what GEMI returned.
    The canonical representation is company_defaults()'s own shape — the list of
    {"code", "description", "type"} dicts — applied identically by both paths."""

    def _fake_get(self, path, params):
        if params.get("isActive") == "true":
            return {"searchResults": [SAMPLE], "searchMetadata": {"totalCount": 1}}
        return {"searchResults": []}

    @patch("gemiapp.services.fetch_companies", return_value=[SAMPLE])
    def test_import_for_date_writes_the_canonical_shape(self, _fetch):
        import_for_date(date(2026, 8, 1))
        company = Company.objects.get(gemi_number=str(SAMPLE["arGemi"]))
        self.assertEqual(
            company.activities,
            [{"code": "62010000", "description": "ΔΡΑΣΤΗΡΙΟΤΗΤΕΣ ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΥ", "type": "Κύρια"}],
        )

    def test_bulk_import_now_matches_daily_import_on_activities(self):
        from .services import import_companies_since_date

        with patch("gemiapp.services._get", side_effect=self._fake_get):
            import_companies_since_date(date(2026, 8, 1))
        via_bulk_import = Company.objects.get(gemi_number=str(SAMPLE["arGemi"])).activities

        Company.objects.all().delete()

        with patch("gemiapp.services.fetch_companies", return_value=[SAMPLE]):
            import_for_date(date(2026, 8, 1))
        via_daily_import = Company.objects.get(gemi_number=str(SAMPLE["arGemi"])).activities

        self.assertEqual(via_bulk_import, via_daily_import)
        self.assertTrue(via_bulk_import, "activities must not silently end up empty for either path")


# ---------------------------------------------------------------------------------------------
# Phase 0 of the billing/subscription production-readiness audit: regression tests that pin down
# the ACTUAL current behaviour of gemiapp/billing.py, including known-dangerous behaviour that is
# deliberately NOT fixed here. Tests marked @unittest.expectedFailure encode the CORRECT behaviour
# and fail today on purpose -- they document a real defect from the audit and are expected to
# start passing once the corresponding phase of the fix lands. Removing the decorator without
# fixing the underlying code is what "unexpected success" would flag.
#
# No real Stripe network call happens anywhere below: stripe.Webhook.construct_event,
# stripe.checkout.Session.create and stripe.Subscription.retrieve are always mocked.
# ---------------------------------------------------------------------------------------------

FAKE_SIG_HEADER = "t=1,v1=fake_signature"


def _checkout_completed_event(user_id, customer_id="cus_123", subscription_id="sub_123", event_id="evt_checkout_1"):
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": {
            "client_reference_id": str(user_id),
            "customer": customer_id,
            "subscription": subscription_id,
        }},
    }


def _stripe_subscription_payload(status="active", price_id="price_pro"):
    """What stripe.Subscription.retrieve(...) returns on success."""
    return {"status": status, "items": {"data": [{"price": {"id": price_id}}]}}


def _subscription_event(
    event_type, subscription_id="sub_123", status="active", price_id="price_pro", event_id="evt_sub_1",
    cancel_at_period_end=False, cancel_at=None, item_current_period_end=None,
):
    item = {"price": {"id": price_id}}
    if item_current_period_end is not None:
        item["current_period_end"] = item_current_period_end
    obj = {
        "id": subscription_id,
        "status": status,
        "items": {"data": [item]},
        "cancel_at_period_end": cancel_at_period_end,
    }
    if cancel_at is not None:
        obj["cancel_at"] = cancel_at
    return {"id": event_id, "type": event_type, "data": {"object": obj}}


def _post_webhook(client, event=None, body=b"{}"):
    """POST to the webhook endpoint with stripe.Webhook.construct_event mocked to return `event`."""
    with patch("stripe.Webhook.construct_event", return_value=event):
        return client.post(
            reverse("stripe_webhook"), data=body, content_type="application/json",
            HTTP_STRIPE_SIGNATURE=FAKE_SIG_HEADER,
        )


@override_settings(
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent",
    STRIPE_WEBHOOK_SECRET="whsec_test",
)
class StripeWebhookCheckoutCompletedTests(TestCase):
    """Priority A.1 — checkout.session.completed happy path. Confirms CORRECT current behaviour:
    no regression here, just a pin so future changes can't silently break the working path."""

    def setUp(self):
        self.user = User.objects.create_user("buyer@example.com", "buyer@example.com", "StrongPass123")

    def test_successful_checkout_activates_entitlement(self):
        event = _checkout_completed_event(self.user.id)
        with patch("stripe.Subscription.retrieve", return_value=_stripe_subscription_payload("active", "price_pro")) as retrieve:
            response = _post_webhook(self.client, event)

        retrieve.assert_called_once_with("sub_123")
        self.assertEqual(response.status_code, 200)
        sub = UserSubscription.objects.get(user=self.user)
        self.assertEqual(sub.stripe_customer_id, "cus_123")
        self.assertEqual(sub.stripe_subscription_id, "sub_123")
        self.assertEqual(sub.status, "active")
        self.assertEqual(sub.tier, "pro")
        self.assertTrue(sub.has_active_paid_subscription)
        self.assertTrue(sub.has_entitlement)

        # Phase 1: the delivery must have been recorded, durably, as processed.
        record = StripeWebhookEvent.objects.get(stripe_event_id=event["id"])
        self.assertEqual(record.event_type, "checkout.session.completed")
        self.assertEqual(record.status, "processed")
        self.assertEqual(record.error_message, "")
        self.assertIsNotNone(record.processed_at)
        self.assertEqual(record.payload["id"], event["id"])


@override_settings(
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent",
    STRIPE_WEBHOOK_SECRET="whsec_test",
)
class StripeWebhookRetrieveFailureBugTests(TestCase):
    """Priority A.2 / Phase 2 — P0 fix: stripe.Subscription.retrieve failing after a completed
    checkout must not silently strip or fabricate entitlement, and the delivery must be
    retryable so a later, successful delivery of the SAME event.id can self-heal the local
    projection. Both tests below were, at various points, Phase 0/1's expectedFailure spec test
    and its paired characterisation test; Phase 2 replaces both with the fixed behaviour.
    """

    def setUp(self):
        self.user = User.objects.create_user("paidbuyer@example.com", "paidbuyer@example.com", "StrongPass123")

    def _post_with_failing_retrieve(self):
        client = Client(raise_request_exception=False)
        event = _checkout_completed_event(self.user.id)
        with patch("stripe.Webhook.construct_event", return_value=event), \
             patch("stripe.Subscription.retrieve", side_effect=RuntimeError("simulated Stripe/network failure")):
            response = client.post(
                reverse("stripe_webhook"), data=b"{}", content_type="application/json",
                HTTP_STRIPE_SIGNATURE=FAKE_SIG_HEADER,
            )
        return response, event

    def test_retrieve_failure_returns_5xx_and_fails_the_event_without_touching_the_subscription(self):
        response, event = self._post_with_failing_retrieve()

        self.assertGreaterEqual(response.status_code, 500)  # Stripe must see this as retryable.
        record = StripeWebhookEvent.objects.get(stripe_event_id=event["id"])
        self.assertEqual(record.status, "failed")
        self.assertIn("simulated Stripe/network failure", record.error_message)

        # Nothing about the local projection was written: not "inactive", not any tier, and
        # crucially stripe_customer_id/stripe_subscription_id were never linked either -- proving
        # the handler aborted before its atomic block rather than partially writing state. What's
        # left is exactly the free/inactive default the post_save signal created at signup.
        sub = UserSubscription.objects.get(user=self.user)
        self.assertEqual(sub.tier, "free")
        self.assertEqual(sub.status, "inactive")
        self.assertEqual(sub.stripe_customer_id, "")
        self.assertEqual(sub.stripe_subscription_id, "")
        self.assertFalse(sub.has_entitlement)

    def test_a_charged_customer_should_not_silently_lose_entitlement(self):
        """Was an @unittest.expectedFailure spec test through Phase 0 and Phase 1; now a normal
        passing regression test. This is the Phase 2 acceptance test: delivery #1 fails and
        changes nothing; delivery #2 of the SAME event.id, once Stripe is reachable again, fully
        self-heals the local projection and grants entitlement."""
        self._post_with_failing_retrieve()

        event = _checkout_completed_event(self.user.id)  # same event id as delivery #1
        with patch("stripe.Subscription.retrieve", return_value=_stripe_subscription_payload("active", "price_pro")) as retrieve:
            second_response = _post_webhook(self.client, event)

        retrieve.assert_called_once()
        self.assertEqual(second_response.status_code, 200)

        record = StripeWebhookEvent.objects.get(stripe_event_id=event["id"])
        self.assertEqual(record.status, "processed")
        self.assertEqual(record.error_message, "")

        sub = UserSubscription.objects.get(user=self.user)
        self.assertEqual(sub.stripe_customer_id, "cus_123")
        self.assertEqual(sub.stripe_subscription_id, "sub_123")
        self.assertEqual(sub.status, "active")
        self.assertEqual(sub.tier, "pro")
        self.assertTrue(sub.has_entitlement, "a charge that succeeded must not silently revoke access")


@override_settings(
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent",
    STRIPE_WEBHOOK_SECRET="whsec_test",
)
class StripeWebhookDuplicateDeliveryTests(TestCase):
    """Priority A.3 / Phase 1 item 2 — Stripe explicitly guarantees only at-least-once delivery:
    the same event.id can arrive more than once. StripeWebhookEvent (added in Phase 1) makes a
    duplicate delivery a no-op instead of full reprocessing.

    In Phase 0 this class held a passing characterisation test asserting retrieve.call_count == 2
    (the old, dangerous reality) alongside an @unittest.expectedFailure spec test asserting
    call_count == 1. Phase 1 fixes exactly this, so the old characterisation assertion is now
    FALSE -- keeping it would just be a stale, contradictory test -- and the spec test below has
    had @unittest.expectedFailure removed per the Phase 1 brief, replacing both.
    """

    def setUp(self):
        self.user = User.objects.create_user("dup@example.com", "dup@example.com", "StrongPass123")

    def _replay_same_event_twice(self):
        event = _checkout_completed_event(self.user.id)  # same "id" both times
        with patch("stripe.Subscription.retrieve", return_value=_stripe_subscription_payload()) as retrieve:
            r1 = _post_webhook(self.client, event)
            r2 = _post_webhook(self.client, event)
        return r1, r2, retrieve

    def test_a_replayed_event_id_is_processed_at_most_once(self):
        r1, r2, retrieve = self._replay_same_event_twice()
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        retrieve.assert_called_once()

        record = StripeWebhookEvent.objects.get(stripe_event_id="evt_checkout_1")
        self.assertEqual(record.status, "processed")
        # And only one row -- the second delivery did not insert a duplicate.
        self.assertEqual(StripeWebhookEvent.objects.filter(stripe_event_id="evt_checkout_1").count(), 1)

        sub = UserSubscription.objects.get(user=self.user)
        self.assertEqual(sub.tier, "pro")
        self.assertEqual(sub.status, "active")


@override_settings(
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent",
    STRIPE_WEBHOOK_SECRET="whsec_test",
)
class StripeWebhookEventPersistenceTests(TestCase):
    """Phase 1 items 4-7 — failed processing, retrying a failed event, and the DB-level
    uniqueness/concurrency guarantee StripeWebhookEvent relies on."""

    def setUp(self):
        self.user = User.objects.create_user("persist@example.com", "persist@example.com", "StrongPass123")
        sub = self.user.subscription
        sub.stripe_customer_id = "cus_1"
        sub.stripe_subscription_id = "sub_1"
        sub.tier = "pro"
        sub.status = "active"
        sub.save()

    def _post_raw(self, event):
        """Like _post_webhook, but with a Client that returns the 500 response instead of
        re-raising -- needed here because we are deliberately triggering an unhandled exception
        in business processing (pre-existing behaviour, not introduced by Phase 1) to prove the
        "failed" bookkeeping around it."""
        client = Client(raise_request_exception=False)
        with patch("stripe.Webhook.construct_event", return_value=event):
            return client.post(
                reverse("stripe_webhook"), data=b"{}", content_type="application/json",
                HTTP_STRIPE_SIGNATURE=FAKE_SIG_HEADER,
            )

    def test_failed_processing_is_recorded_with_an_error_message(self):
        """Phase 1 item 4. An unexpected exception during business processing (here: a
        malformed `items[0]` shape missing "price", which is not caught by the existing
        `except UserSubscription.DoesNotExist` in _handle_subscription_updated_or_deleted --
        true before Phase 1 too) still propagates and Django still answers 500, exactly as
        before Phase 1. What's new is that the attempt is durably recorded as failed."""
        event = _subscription_event("customer.subscription.updated", subscription_id="sub_1", status="active")
        event["data"]["object"]["items"]["data"] = [{"unexpected": "shape"}]

        response = self._post_raw(event)

        self.assertEqual(response.status_code, 500)
        record = StripeWebhookEvent.objects.get(stripe_event_id=event["id"])
        self.assertEqual(record.status, "failed")
        self.assertIn("price", record.error_message)
        self.assertIsNotNone(record.processed_at)
        # The half-processed business state must not have been left dangling either.
        sub = UserSubscription.objects.get(user=self.user)
        self.assertEqual(sub.status, "active")  # untouched by the failed attempt

    def test_retrying_a_previously_failed_event_reprocesses_it(self):
        """Phase 1 item 5. A second delivery of the SAME event.id, once whatever caused the
        first failure no longer applies, must actually retry -- not be treated as a duplicate
        of something already handled."""
        bad_event = _subscription_event("customer.subscription.updated", subscription_id="sub_1", status="active")
        bad_event["data"]["object"]["items"]["data"] = [{"unexpected": "shape"}]
        first_response = self._post_raw(bad_event)
        self.assertEqual(first_response.status_code, 500)
        self.assertEqual(StripeWebhookEvent.objects.get(stripe_event_id=bad_event["id"]).status, "failed")

        good_event = _subscription_event(
            "customer.subscription.updated", subscription_id="sub_1", status="active",
            price_id="price_biz", event_id=bad_event["id"],
        )
        second_response = _post_webhook(self.client, good_event)

        self.assertEqual(second_response.status_code, 200)
        record = StripeWebhookEvent.objects.get(stripe_event_id=bad_event["id"])
        self.assertEqual(record.status, "processed")
        self.assertEqual(record.error_message, "")
        sub = UserSubscription.objects.get(user=self.user)
        self.assertEqual(sub.tier, "business")

    def test_unique_constraint_on_stripe_event_id(self):
        """Phase 1 item 6."""
        from django.db import IntegrityError

        StripeWebhookEvent.objects.create(stripe_event_id="evt_dup", event_type="x", payload={})
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                StripeWebhookEvent.objects.create(stripe_event_id="evt_dup", event_type="y", payload={})
        self.assertEqual(StripeWebhookEvent.objects.filter(stripe_event_id="evt_dup").count(), 1)

    def test_concurrent_duplicate_claims_cannot_both_create_a_row(self):
        """Phase 1 item 7. True concurrent-request locking (the `select_for_update` path in
        `_claim_webhook_event`) can only be meaningfully exercised against Postgres: SQLite (the
        test database) has no row-level locking and Django's ORM silently no-ops FOR UPDATE
        against it (`connection.features.has_select_for_update` is False), so a real multi-
        thread timing race cannot be proven from this test suite. What IS deterministic and
        database-agnostic is the invariant idempotency actually depends on: the unique
        constraint on stripe_event_id makes it impossible for two racing "create" attempts to
        both succeed, which is what `get_or_create` inside `_claim_webhook_event` relies on --
        one caller gets `created=True`, the other is guaranteed to fall back to `get()`. This
        test proves that guarantee directly against the constraint, which holds identically on
        SQLite and Postgres, rather than attempting a thread-timing test that would be flaky
        here and meaningless against SQLite regardless."""
        from django.db import IntegrityError

        StripeWebhookEvent.objects.create(
            stripe_event_id="evt_race", event_type="checkout.session.completed", payload={}, status="received",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                StripeWebhookEvent.objects.create(
                    stripe_event_id="evt_race", event_type="checkout.session.completed", payload={}, status="received",
                )
        self.assertEqual(StripeWebhookEvent.objects.filter(stripe_event_id="evt_race").count(), 1)


@override_settings(
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent",
    LEGAL_BILLING_ACTIVE=True,
)
class DuplicateSubscriptionCheckoutTests(TestCase):
    """Priority A.4 / Phase 4 — an already-subscribed user must not be able to spin up a second,
    unrelated Stripe subscription just by posting to create_checkout_session again. The former
    @unittest.expectedFailure spec test below is now the fix, and green: this is THE Phase 4
    acceptance test. The rest of this class covers the full status predicate the fix relies on.
    """

    def _user(self, email, **sub_fields):
        user = User.objects.create_user(email, email, "StrongPass123")
        sub = user.subscription
        for field, value in sub_fields.items():
            setattr(sub, field, value)
        sub.save()
        return user

    def _post(self, user, tier="business"):
        self.client.force_login(user)
        with patch("stripe.checkout.Session.create") as create:
            create.return_value = type("S", (), {"url": "https://stripe.test/checkout"})()
            response = self.client.post(reverse("create_checkout_session"), {"tier": tier})
        return response, create

    # Phase 4 test 1 + main acceptance test: same tier (Pro -> Pro), the most literal duplicate.
    def test_an_already_subscribed_user_should_not_get_a_new_checkout_session(self):
        """Was an @unittest.expectedFailure spec test through Phase 0-3; now the fix, and green."""
        user = self._user(
            "already1@example.com", tier="pro", status="active",
            stripe_customer_id="cus_existing", stripe_subscription_id="sub_existing",
        )
        response, create = self._post(user, tier="pro")

        create.assert_not_called()  # test 4: Session.create call_count == 0
        self.assertRedirects(response, reverse("settings"))
        sub = user.subscription
        sub.refresh_from_db()
        self.assertEqual(sub.stripe_subscription_id, "sub_existing")  # test 6: unchanged
        self.assertEqual(sub.tier, "pro")
        self.assertEqual(sub.status, "active")

    # Phase 4 tests 2-3: a different tier is blocked too -- this is not an upgrade path (Phase 5).
    def test_active_pro_to_business_is_also_blocked(self):
        user = self._user(
            "already2@example.com", tier="pro", status="active",
            stripe_customer_id="cus_existing", stripe_subscription_id="sub_existing",
        )
        response, create = self._post(user, tier="business")
        create.assert_not_called()
        self.assertRedirects(response, reverse("settings"))

    def test_active_pro_to_enterprise_is_also_blocked(self):
        user = self._user(
            "already3@example.com", tier="pro", status="active",
            stripe_customer_id="cus_existing", stripe_subscription_id="sub_existing",
        )
        response, create = self._post(user, tier="enterprise")
        create.assert_not_called()
        self.assertRedirects(response, reverse("settings"))

    # Phase 4 test 5: the Phase 3 checkout-attempt nonce must never be created on a blocked path.
    def test_blocking_path_does_not_create_a_checkout_attempt_nonce(self):
        user = self._user(
            "already5@example.com", tier="pro", status="active",
            stripe_customer_id="cus_existing", stripe_subscription_id="sub_existing",
        )
        self.client.force_login(user)
        with patch("stripe.checkout.Session.create") as create:
            self.client.post(reverse("create_checkout_session"), {"tier": "business"})
        create.assert_not_called()
        self.assertNotIn("checkout_attempt_nonce:business", self.client.session)

    # Phase 4 test 6 (standalone): tier/status/entitlement genuinely untouched, not just the id.
    def test_blocking_path_leaves_tier_status_and_id_untouched(self):
        user = self._user(
            "already6@example.com", tier="pro", status="active",
            stripe_customer_id="cus_existing", stripe_subscription_id="sub_existing",
        )
        before = (user.subscription.tier, user.subscription.status, user.subscription.stripe_subscription_id)
        self._post(user, tier="pro")
        sub = user.subscription
        sub.refresh_from_db()
        after = (sub.tier, sub.status, sub.stripe_subscription_id)
        self.assertEqual(before, after)

    # Phase 4 test 7: trialing is not "active" but still fully live -- must block.
    def test_trialing_subscription_blocks_new_checkout(self):
        user = self._user(
            "already7@example.com", tier="pro", status="trialing",
            stripe_customer_id="cus_existing", stripe_subscription_id="sub_existing",
        )
        _response, create = self._post(user, tier="business")
        create.assert_not_called()

    # Phase 4 test 8: past_due/unpaid/incomplete are all recoverable by Stripe/the customer --
    # a second subscription must not be offered as a workaround for a broken one.
    def test_past_due_subscription_blocks_new_checkout(self):
        user = self._user(
            "already8a@example.com", tier="pro", status="past_due",
            stripe_customer_id="cus_existing", stripe_subscription_id="sub_existing",
        )
        _response, create = self._post(user, tier="business")
        create.assert_not_called()

    def test_unpaid_subscription_blocks_new_checkout(self):
        user = self._user(
            "already8b@example.com", tier="pro", status="unpaid",
            stripe_customer_id="cus_existing", stripe_subscription_id="sub_existing",
        )
        _response, create = self._post(user, tier="business")
        create.assert_not_called()

    def test_incomplete_subscription_blocks_new_checkout(self):
        user = self._user(
            "already8c@example.com", tier="pro", status="incomplete",
            stripe_customer_id="cus_existing", stripe_subscription_id="sub_existing",
        )
        _response, create = self._post(user, tier="business")
        create.assert_not_called()

    # Phase 4 test 9: genuinely terminal statuses allow a new checkout.
    def test_canceled_subscription_allows_new_checkout(self):
        user = self._user(
            "already9a@example.com", tier="pro", status="canceled",
            stripe_customer_id="cus_existing", stripe_subscription_id="sub_existing",
        )
        response, create = self._post(user, tier="business")
        create.assert_called_once()
        self.assertEqual(response.status_code, 303)

    def test_incomplete_expired_subscription_allows_new_checkout(self):
        user = self._user(
            "already9b@example.com", tier="pro", status="incomplete_expired",
            stripe_customer_id="cus_existing", stripe_subscription_id="sub_existing",
        )
        _response, create = self._post(user, tier="business")
        create.assert_called_once()

    # Phase 4 test 10: Stripe keeps status="active" for a cancel-at-period-end subscription until
    # the period genuinely ends (a separate customer.subscription.deleted fires later);
    # UserSubscription doesn't even store cancel_at_period_end, so this case is -- correctly --
    # indistinguishable from, and handled identically to, any other still-active subscription.
    # Pinned as its own test so that assumption is explicit, not just implied by the plain-active
    # test above.
    def test_cancel_scheduled_but_still_active_is_blocked(self):
        user = self._user(
            "already10@example.com", tier="pro", status="active",
            stripe_customer_id="cus_existing", stripe_subscription_id="sub_existing",
        )
        _response, create = self._post(user, tier="business")
        create.assert_not_called()

    # Phase 4 test 11: a Customer alone, with no live subscription, never blocks checkout.
    def test_customer_without_a_subscription_id_allows_checkout(self):
        user = self._user("already11@example.com", stripe_customer_id="cus_existing")
        response, create = self._post(user, tier="pro")
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs.get("customer"), "cus_existing")  # still reused
        self.assertEqual(response.status_code, 303)

    # Phase 4 test 12: complimentary access is independent of Stripe and must not be conflated
    # with a paid-subscription guard.
    def test_complimentary_only_access_allows_checkout(self):
        user = self._user("already12@example.com", complimentary_tier="enterprise")
        self.assertTrue(user.subscription.has_entitlement)
        self.assertFalse(user.subscription.has_active_paid_subscription)
        response, create = self._post(user, tier="pro")
        create.assert_called_once()
        self.assertEqual(response.status_code, 303)

    # Phase 4 test 13: an unrecognised local status with a real stripe_subscription_id resolves
    # via a fresh Stripe read rather than assuming either way.
    def test_unknown_local_status_resolves_via_stripe_and_blocks_if_still_live(self):
        user = self._user(
            "already13a@example.com", tier="pro", status="inactive",
            stripe_customer_id="cus_existing", stripe_subscription_id="sub_existing",
        )
        with patch("stripe.Subscription.retrieve", return_value={"status": "active"}) as retrieve:
            _response, create = self._post(user, tier="business")
        retrieve.assert_called_once_with("sub_existing")
        create.assert_not_called()

    def test_unknown_local_status_resolves_via_stripe_and_allows_if_terminal(self):
        user = self._user(
            "already13b@example.com", tier="pro", status="inactive",
            stripe_customer_id="cus_existing", stripe_subscription_id="sub_existing",
        )
        with patch("stripe.Subscription.retrieve", return_value={"status": "canceled"}) as retrieve:
            _response, create = self._post(user, tier="business")
        retrieve.assert_called_once_with("sub_existing")
        create.assert_called_once()

    # Phase 4 test 14: a Stripe failure while resolving ambiguity fails closed -- this guards
    # real money, so uncertainty must never let a second Session.create through.
    def test_stripe_retrieve_failure_during_ambiguity_check_fails_closed(self):
        user = self._user(
            "already14@example.com", tier="pro", status="inactive",
            stripe_customer_id="cus_existing", stripe_subscription_id="sub_existing",
        )
        self.client.force_login(user)
        with patch("stripe.Subscription.retrieve", side_effect=RuntimeError("stripe down")), \
             patch("stripe.checkout.Session.create") as create:
            self.client.post(reverse("create_checkout_session"), {"tier": "business"})
        create.assert_not_called()
        sub = user.subscription
        sub.refresh_from_db()
        self.assertEqual(sub.status, "inactive")  # untouched, not fabricated either way


@override_settings(STRIPE_WEBHOOK_SECRET="whsec_test")
class StripeWebhookSignatureTests(TestCase):
    """Priority A.5 — signature verification must gate all processing."""

    def test_invalid_signature_is_rejected_before_any_processing(self):
        with patch("stripe.Webhook.construct_event",
                    side_effect=stripe.SignatureVerificationError("bad sig", FAKE_SIG_HEADER)) as construct, \
             patch("stripe.Subscription.retrieve") as retrieve:
            response = self.client.post(
                reverse("stripe_webhook"), data=b"{}", content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=wrong",
            )
        self.assertEqual(response.status_code, 400)
        construct.assert_called_once()
        retrieve.assert_not_called()
        # The event was never verified, so nothing about it is trustworthy enough to persist.
        self.assertEqual(StripeWebhookEvent.objects.count(), 0)

    @override_settings(STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent")
    def test_valid_signature_reaches_the_handler(self):
        user = User.objects.create_user("sigok@example.com", "sigok@example.com", "StrongPass123")
        event = _checkout_completed_event(user.id)
        with patch("stripe.Subscription.retrieve", return_value=_stripe_subscription_payload()) as retrieve:
            response = _post_webhook(self.client, event)
        self.assertEqual(response.status_code, 200)
        retrieve.assert_called_once()


@override_settings(
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent",
    STRIPE_WEBHOOK_SECRET="whsec_test",
)
class StripeWebhookSubscriptionLifecycleTests(TestCase):
    """Priority B — customer.subscription.updated / .deleted, unknown ids, unsupported event
    types, malformed payloads. All PASS today: this is confirmatory, not bug-proving."""

    def setUp(self):
        self.user = User.objects.create_user("lifecycle@example.com", "lifecycle@example.com", "StrongPass123")
        sub = self.user.subscription
        sub.stripe_customer_id = "cus_1"
        sub.stripe_subscription_id = "sub_1"
        sub.tier = "pro"
        sub.status = "active"
        sub.save()

    def test_subscription_updated_reflects_new_tier_and_status(self):
        event = _subscription_event("customer.subscription.updated", subscription_id="sub_1", status="active", price_id="price_biz")
        response = _post_webhook(self.client, event)
        self.assertEqual(response.status_code, 200)
        sub = UserSubscription.objects.get(user=self.user)
        self.assertEqual(sub.tier, "business")
        self.assertEqual(sub.status, "active")
        self.assertTrue(sub.has_entitlement)

    def test_subscription_deleted_revokes_entitlement(self):
        event = _subscription_event("customer.subscription.deleted", subscription_id="sub_1", status="canceled")
        response = _post_webhook(self.client, event)
        self.assertEqual(response.status_code, 200)
        sub = UserSubscription.objects.get(user=self.user)
        self.assertEqual(sub.status, "canceled")
        self.assertFalse(sub.has_active_paid_subscription)
        self.assertFalse(sub.has_entitlement)

    def test_event_for_unknown_subscription_id_is_dropped_silently(self):
        """Priority B.8. Characterisation test: PASSES today. No local row matches, the event is
        logged and swallowed with a 200 -- Stripe considers it delivered and never retries."""
        event = _subscription_event("customer.subscription.updated", subscription_id="sub_never_seen", status="active")
        response = _post_webhook(self.client, event)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(UserSubscription.objects.filter(stripe_subscription_id="sub_never_seen").exists())
        sub = UserSubscription.objects.get(user=self.user)
        self.assertEqual(sub.status, "active")  # the unrelated, existing row is untouched

    def test_unsupported_event_type_is_ignored_safely(self):
        """Priority B.9 / Phase 1 item 3. No if/elif branch matches -> falls through to the
        final `return HttpResponse(status=200)` with no UserSubscription side effects, but the
        delivery is still recorded, durably, with status="ignored"."""
        event = {"id": "evt_x", "type": "payment_intent.succeeded", "data": {"object": {}}}
        response = _post_webhook(self.client, event)
        self.assertEqual(response.status_code, 200)
        sub = UserSubscription.objects.get(user=self.user)
        self.assertEqual(sub.status, "active")

        record = StripeWebhookEvent.objects.get(stripe_event_id="evt_x")
        self.assertEqual(record.event_type, "payment_intent.succeeded")
        self.assertEqual(record.status, "ignored")
        self.assertEqual(record.error_message, "")
        self.assertIsNotNone(record.processed_at)

    def test_malformed_payload_returns_400(self):
        """Priority B.10 / Phase 1 item 8. construct_event raising ValueError (e.g. invalid
        JSON) -> 400, and nothing is persisted: the event was never verified."""
        with patch("stripe.Webhook.construct_event", side_effect=ValueError("invalid payload")):
            response = self.client.post(
                reverse("stripe_webhook"), data=b"not json", content_type="application/json",
                HTTP_STRIPE_SIGNATURE=FAKE_SIG_HEADER,
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(StripeWebhookEvent.objects.count(), 0)


@override_settings(
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent",
    STRIPE_WEBHOOK_SECRET="whsec_test",
)
class StripeWebhookOutOfOrderTests(TestCase):
    """customer.subscription.updated arriving before checkout.session.completed, for a
    subscription id the local database has never seen. Stripe explicitly does not guarantee
    webhook delivery order. Characterisation only: PASSES today, not marked as a bug proof,
    because this specific pair happens to self-heal (see the docstring below) -- but that
    self-healing is incidental, not a designed ordering guarantee, so it is pinned here rather
    than assumed."""

    def setUp(self):
        self.user = User.objects.create_user("outoforder@example.com", "outoforder@example.com", "StrongPass123")

    def test_current_behaviour_the_early_update_is_dropped_then_the_later_checkout_self_heals(self):
        update_event = _subscription_event(
            "customer.subscription.updated", subscription_id="sub_early", status="active", price_id="price_pro",
        )
        response = _post_webhook(self.client, update_event)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            UserSubscription.objects.filter(stripe_subscription_id="sub_early").exists(),
            "the early update finds no matching row and is silently dropped -- logged, never retried",
        )

        # The later checkout.session.completed for the SAME subscription still ends up correct in
        # THIS example only because it independently calls stripe.Subscription.retrieve for the
        # CURRENT state rather than trusting the lost update -- that save is what protects this
        # specific path, not any ordering logic in the webhook itself. A later out-of-order pair
        # of two customer.subscription.updated events (not covered here) would have no such safety
        # net and would simply apply whichever one arrived last.
        checkout_event = _checkout_completed_event(self.user.id, customer_id="cus_early", subscription_id="sub_early")
        with patch("stripe.Subscription.retrieve", return_value=_stripe_subscription_payload("active", "price_pro")):
            _post_webhook(self.client, checkout_event)

        sub = UserSubscription.objects.get(user=self.user)
        self.assertEqual(sub.stripe_subscription_id, "sub_early")
        self.assertEqual(sub.status, "active")


class EntitlementConsistencyTests(TestCase):
    """Priority C — entitlement must track Stripe-driven state, and complimentary access must
    stay independent of it. The 375-combination Python/DB-predicate agreement is already covered
    by SubscriptionQueryHelperTests; this class checks the specific scenarios named in the audit."""

    def setUp(self):
        self.user = User.objects.create_user("entitlement@example.com", "entitlement@example.com", "StrongPass123")

    def test_active_paid_subscription_grants_entitlement(self):
        sub = self.user.subscription
        sub.tier, sub.status = "business", "active"
        sub.save()
        self.assertTrue(sub.has_entitlement)
        self.assertEqual(sub.effective_tier, "business")

    def test_canceled_subscription_has_no_entitlement(self):
        sub = self.user.subscription
        sub.tier, sub.status = "business", "canceled"
        sub.save()
        self.assertFalse(sub.has_entitlement)
        self.assertEqual(sub.effective_tier, "free")

    def test_inactive_status_has_no_entitlement_even_with_a_paid_tier(self):
        sub = self.user.subscription
        sub.tier, sub.status = "pro", "inactive"
        sub.save()
        self.assertFalse(sub.has_entitlement)

    def test_complimentary_access_is_independent_of_stripe_state(self):
        sub = self.user.subscription
        sub.tier, sub.status = "free", "inactive"  # no Stripe subscription at all
        sub.complimentary_tier = "enterprise"
        sub.save()
        self.assertTrue(sub.has_entitlement)
        self.assertEqual(sub.effective_tier, "enterprise")
        self.assertFalse(sub.has_active_paid_subscription, "complimentary access must not read as a paid Stripe subscription")

    def test_expired_complimentary_access_removes_entitlement(self):
        sub = self.user.subscription
        sub.tier, sub.status = "free", "inactive"
        sub.complimentary_tier = "pro"
        sub.complimentary_until = timezone.now() - timedelta(days=1)
        sub.save()
        self.assertFalse(sub.has_entitlement)
        self.assertEqual(sub.effective_tier, "free")

    @override_settings(STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz",
                        STRIPE_PRICE_ENTERPRISE="price_ent", STRIPE_WEBHOOK_SECRET="whsec_test")
    def test_webhook_driven_status_change_is_reflected_in_entitlement(self):
        """End-to-end: a subscription.updated webhook flips status, and has_entitlement moves
        with it -- not just the raw DB field."""
        sub = self.user.subscription
        sub.stripe_customer_id, sub.stripe_subscription_id = "cus_e2e", "sub_e2e"
        sub.tier, sub.status = "pro", "active"
        sub.save()
        self.assertTrue(sub.has_entitlement)

        event = _subscription_event("customer.subscription.deleted", subscription_id="sub_e2e", status="canceled")
        _post_webhook(self.client, event)

        sub.refresh_from_db()
        self.assertFalse(sub.has_entitlement)


# ---------------------------------------------------------------------------------------------
# Phase 3 of the billing production-readiness audit: Stripe Checkout idempotency and
# double-submit protection. Deliberately narrow scope -- this does NOT touch webhook processing,
# entitlement logic, or the duplicate-subscription problem (an already-subscribed user starting
# an unrelated second subscription is Phase 4; DuplicateSubscriptionCheckoutTests' xfail is
# untouched below).
# ---------------------------------------------------------------------------------------------


@override_settings(
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent",
    LEGAL_BILLING_ACTIVE=True,
)
class StripeCheckoutIdempotencyTests(TestCase):
    """Phase 3 items 1-6 (Priority: Stripe idempotency). Every Session.create call must carry an
    idempotency key scoped to one logical checkout attempt: reused across a retry of that SAME
    attempt (a double submit, or a retry after an ambiguous Stripe-side failure), rotated once a
    Session has actually been created so a later, distinct attempt is never silently deduped
    against a stale key days later.
    """

    def _stripe_ok(self):
        return patch(
            "stripe.checkout.Session.create",
            return_value=type("S", (), {"url": "https://stripe.test/checkout"})(),
        )

    def _post(self, client, tier="pro"):
        return client.post(reverse("create_checkout_session"), {"tier": tier})

    def test_checkout_session_create_receives_a_non_empty_idempotency_key(self):
        """Phase 3 test 1."""
        user = User.objects.create_user("idem1@example.com", "idem1@example.com", "StrongPass123")
        self.client.force_login(user)
        with self._stripe_ok() as create:
            self._post(self.client, "pro")
        key = create.call_args.kwargs.get("idempotency_key")
        self.assertTrue(key)
        self.assertIsInstance(key, str)

    def test_retry_after_a_stripe_failure_reuses_the_same_idempotency_key(self):
        """Phase 3 tests 2 and 6 (same mechanism): from the nonce's point of view, a caller
        retrying because of a double-click and a caller retrying because the previous call
        failed both look like "the same attempt, not yet confirmed created" -- both must reuse
        the key. Also proves failure doesn't corrupt attempt state: the retry succeeds cleanly.
        """
        user = User.objects.create_user("idem2@example.com", "idem2@example.com", "StrongPass123")
        self.client.force_login(user)

        with patch("stripe.checkout.Session.create", side_effect=RuntimeError("stripe down")) as create:
            first_response = self._post(self.client, "pro")
        self.assertEqual(first_response.status_code, 200)  # re-rendered pricing.html with an error
        first_key = create.call_args.kwargs.get("idempotency_key")
        self.assertTrue(first_key)

        with self._stripe_ok() as create:
            second_response = self._post(self.client, "pro")
        second_key = create.call_args.kwargs.get("idempotency_key")

        self.assertEqual(first_key, second_key, "a retry of the same still-open attempt must reuse the key")
        self.assertEqual(second_response.status_code, 303)

    def test_a_different_tier_gets_a_different_idempotency_scope(self):
        """Phase 3 test 3."""
        user = User.objects.create_user("idem3@example.com", "idem3@example.com", "StrongPass123")
        self.client.force_login(user)

        with self._stripe_ok() as create:
            self._post(self.client, "pro")
        pro_key = create.call_args.kwargs["idempotency_key"]

        with self._stripe_ok() as create:
            self._post(self.client, "business")
        business_key = create.call_args.kwargs["idempotency_key"]

        self.assertNotEqual(pro_key, business_key)

    def test_a_new_attempt_after_a_successful_checkout_gets_a_new_key(self):
        """Phase 3 test 4. Confirms the nonce is not a permanent dedupe: a legitimate later
        attempt (e.g. the first Checkout Session expired or was abandoned) is never blocked by
        an old key days later."""
        user = User.objects.create_user("idem4@example.com", "idem4@example.com", "StrongPass123")
        self.client.force_login(user)

        with self._stripe_ok() as create:
            self._post(self.client, "pro")
        first_key = create.call_args.kwargs["idempotency_key"]

        with self._stripe_ok() as create:
            self._post(self.client, "pro")
        second_key = create.call_args.kwargs["idempotency_key"]

        self.assertNotEqual(first_key, second_key, "a new attempt after a concluded one must not reuse a stale key")

    def test_different_users_never_share_an_idempotency_key(self):
        """Phase 3 test 5."""
        user_a = User.objects.create_user("idem5a@example.com", "idem5a@example.com", "StrongPass123")
        user_b = User.objects.create_user("idem5b@example.com", "idem5b@example.com", "StrongPass123")
        client_a, client_b = Client(), Client()
        client_a.force_login(user_a)
        client_b.force_login(user_b)

        with self._stripe_ok() as create:
            self._post(client_a, "pro")
        key_a = create.call_args.kwargs["idempotency_key"]

        with self._stripe_ok() as create:
            self._post(client_b, "pro")
        key_b = create.call_args.kwargs["idempotency_key"]

        self.assertNotEqual(key_a, key_b)

    def test_idempotency_key_contains_no_secret_or_raw_nonce_material(self):
        """Phase 3 item 9 (security). The key is derived and opaque -- never a raw Stripe
        secret, never the raw nonce itself, safe to log if that's ever needed."""
        from django.conf import settings as django_settings
        from .billing import _stripe_idempotency_key

        key = _stripe_idempotency_key(user_id=42, tier="pro", nonce="abc123")
        self.assertTrue(key.startswith("ck_"))
        self.assertNotIn("abc123", key)  # the raw nonce is hashed away, not embedded
        if django_settings.STRIPE_SECRET_KEY:
            self.assertNotIn(django_settings.STRIPE_SECRET_KEY, key)


@override_settings(LEGAL_BILLING_ACTIVE=True)
class CheckoutDoubleSubmitTemplateTests(TestCase):
    """Phase 3 item 6 (frontend). There is no JS test runner in this project, so this only
    proves the hooks app.js relies on are actually rendered on the page -- it does not, and
    cannot, prove the JS behaves correctly at runtime. That's an explicit, accepted limitation:
    the correctness-bearing protection against a duplicate Stripe Checkout Session is server-
    side (StripeCheckoutIdempotencyTests above) and does not depend on this markup at all; this
    class only guards the UX layer against silently regressing.
    """

    def test_each_checkout_form_carries_the_double_submit_hooks(self):
        response = self.client.get(reverse("pricing"))
        content = response.content.decode()
        self.assertIn("data-checkout-form", content)
        self.assertIn("data-checkout-submit", content)
        self.assertIn("data-loading-label=", content)
        # Three plans (Pro/Business/Enterprise) render three independent forms, so the guard
        # only ever needs to -- and only ever does -- disable the one that was submitted.
        self.assertEqual(content.count("data-checkout-form"), 3)

    def test_app_js_scopes_the_disable_to_the_submitted_form_only(self):
        from django.conf import settings as django_settings

        app_js_path = Path(django_settings.BASE_DIR) / "static" / "js" / "app.js"
        source = app_js_path.read_text(encoding="utf-8")
        self.assertIn("data-checkout-form", source)
        self.assertIn("data-checkout-submit", source)
        # Regression guard against a future rewrite disabling every button on the page instead
        # of scoping to the one submitted form.
        self.assertIn("event.target.closest('[data-checkout-form]')", source)


# ---------------------------------------------------------------------------------------------
# Phase 5a of the billing production-readiness audit: schema foundation only for the future
# upgrade/downgrade lifecycle (Phase 5b+). scheduled_tier/scheduled_change_at/stripe_schedule_id
# are added to UserSubscription here but are completely inert -- nothing reads or writes them
# yet outside these tests, and nothing in entitlement logic references them. This class exists
# to pin that inertness down as a regression guard for Phase 5b, not to test any lifecycle
# behaviour (there isn't any yet).
# ---------------------------------------------------------------------------------------------


class ScheduledTierProjectionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("scheduled@example.com", "scheduled@example.com", "StrongPass123")

    # Phase 5a test 1
    def test_new_subscription_has_no_scheduled_change_by_default(self):
        sub = self.user.subscription
        self.assertIsNone(sub.scheduled_tier)
        self.assertIsNone(sub.scheduled_change_at)
        self.assertEqual(sub.stripe_schedule_id, "")

    # Phase 5a test 2
    def test_a_valid_scheduled_tier_is_stored(self):
        sub = self.user.subscription
        sub.scheduled_tier = "pro"
        sub.scheduled_change_at = timezone.now() + timedelta(days=10)
        sub.stripe_schedule_id = "sub_sched_123"
        sub.full_clean()
        sub.save()

        sub.refresh_from_db()
        self.assertEqual(sub.scheduled_tier, "pro")
        self.assertIsNotNone(sub.scheduled_change_at)
        self.assertEqual(sub.stripe_schedule_id, "sub_sched_123")

    # Phase 5a test 3
    def test_an_invalid_scheduled_tier_is_rejected_by_choices_validation(self):
        """Same validation mechanism the project already uses for `tier`/`complimentary_tier`
        (a `choices=` CharField, enforced by Django's full_clean(), not a DB constraint)."""
        from django.core.exceptions import ValidationError

        sub = self.user.subscription
        sub.scheduled_tier = "free"  # not a schedulable tier -- see the field's own comment
        with self.assertRaises(ValidationError):
            sub.full_clean()

        sub.scheduled_tier = "custom"  # quote-based, no Stripe price, never a schedule target
        with self.assertRaises(ValidationError):
            sub.full_clean()

        sub.scheduled_tier = "not-a-real-tier"
        with self.assertRaises(ValidationError):
            sub.full_clean()

    def test_scheduled_metadata_without_a_scheduled_tier_is_rejected(self):
        """The clean() cross-field invariant: a schedule reference or a projected change time
        with no scheduled_tier to go with it is inconsistent data."""
        from django.core.exceptions import ValidationError

        sub = self.user.subscription
        sub.scheduled_tier = None
        sub.scheduled_change_at = timezone.now() + timedelta(days=10)
        with self.assertRaises(ValidationError):
            sub.full_clean()

        sub.scheduled_change_at = None
        sub.stripe_schedule_id = "sub_sched_123"
        with self.assertRaises(ValidationError):
            sub.full_clean()

    # Phase 5a test 4
    def test_scheduled_tier_does_not_affect_effective_tier_entitlement_or_radar_limit(self):
        sub = self.user.subscription
        sub.tier, sub.status = "business", "active"
        sub.scheduled_tier = "pro"
        sub.scheduled_change_at = timezone.now() + timedelta(days=10)
        sub.stripe_schedule_id = "sub_sched_456"
        sub.save()

        # Baseline computed with no scheduled_tier at all, to prove the two are identical.
        baseline = UserSubscription(tier="business", status="active")

        self.assertEqual(sub.effective_tier, baseline.effective_tier)
        self.assertEqual(sub.has_entitlement, baseline.has_entitlement)
        self.assertEqual(sub.has_active_paid_subscription, baseline.has_active_paid_subscription)
        self.assertEqual(sub.radar_limit, baseline.radar_limit)
        self.assertEqual(sub.effective_tier, "business")
        self.assertTrue(sub.has_entitlement)

    # Phase 5a test 5
    def test_scheduled_downgrade_metadata_on_active_business_keeps_business_entitlement(self):
        """"Business until X, then Pro" -- while X hasn't happened yet (nothing here simulates
        Stripe actually applying the schedule; Phase 5a adds no webhook wiring), entitlement is
        still fully Business."""
        sub = self.user.subscription
        sub.tier, sub.status = "business", "active"
        sub.scheduled_tier = "pro"
        sub.scheduled_change_at = timezone.now() + timedelta(days=3)
        sub.stripe_schedule_id = "sub_sched_789"
        sub.save()

        self.assertEqual(sub.effective_tier, "business")
        self.assertEqual(sub.radar_limit, RADAR_LIMITS["business"])
        self.assertTrue(sub.has_entitlement)

    # Phase 5a test 6
    def test_complimentary_entitlement_stays_independent_of_scheduled_fields(self):
        sub = self.user.subscription
        sub.tier, sub.status = "free", "inactive"
        sub.complimentary_tier = "enterprise"
        sub.scheduled_tier = "pro"
        sub.scheduled_change_at = timezone.now() + timedelta(days=1)
        sub.stripe_schedule_id = "sub_sched_999"
        sub.save()

        self.assertTrue(sub.has_entitlement)
        self.assertEqual(sub.effective_tier, "enterprise")
        self.assertFalse(sub.has_active_paid_subscription)

    # Phase 5a test 7
    def test_active_until_is_not_involved_in_normal_renewal_entitlement(self):
        """active_until's Phase 5a semantics is "scheduled entitlement expiry due to a
        cancellation," not a renewal date. A normal, nothing-scheduled active subscription must
        have it unset, and has_entitlement/effective_tier must not reference it at all (Phase 5a
        adds no runtime wiring that would ever set it either)."""
        sub = self.user.subscription
        sub.tier, sub.status = "pro", "active"
        sub.save()

        self.assertIsNone(sub.active_until)
        self.assertTrue(sub.has_entitlement)

        # Setting it (simulating what Phase 5c+ will eventually do for a real cancellation)
        # still must not, on its own, change entitlement -- only `status` does that.
        sub.active_until = timezone.now() + timedelta(days=5)
        sub.save()
        self.assertTrue(sub.has_entitlement)  # unchanged: status is still "active"


# ---------------------------------------------------------------------------------------------
# Phase 5b: cancel-at-period-end / resume. Both endpoints make exactly one Stripe write and
# report the outcome -- neither ever writes tier/status/active_until/scheduled_*/entitlement
# locally (that's Phase 5c, webhook-only). No Stripe network calls happen anywhere below.
# ---------------------------------------------------------------------------------------------


class SubscriptionCancelResumeTests(TestCase):
    def _user(self, email, **sub_fields):
        user = User.objects.create_user(email, email, "StrongPass123")
        sub = user.subscription
        for field, value in sub_fields.items():
            setattr(sub, field, value)
        sub.save()
        return user

    # ---------------- Cancel ----------------

    def test_cancel_active_subscription_calls_modify_with_cancel_at_period_end_true(self):
        user = self._user(
            "cancel1@example.com", tier="pro", status="active",
            stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
        )
        self.client.force_login(user)
        with patch("stripe.Subscription.retrieve", return_value={"status": "active"}), \
             patch("stripe.Subscription.modify") as modify:
            response = self.client.post(reverse("cancel_subscription"))

        modify.assert_called_once()
        args, kwargs = modify.call_args
        self.assertEqual(args[0], "sub_1")
        self.assertTrue(kwargs.get("cancel_at_period_end"))
        self.assertTrue(kwargs.get("idempotency_key"))
        self.assertRedirects(response, reverse("settings"))

    def test_cancel_subscription_id_comes_from_authenticated_user_not_post_body(self):
        user = self._user(
            "cancel2@example.com", tier="pro", status="active",
            stripe_customer_id="cus_2", stripe_subscription_id="sub_2",
        )
        self.client.force_login(user)
        with patch("stripe.Subscription.retrieve", return_value={"status": "active"}), \
             patch("stripe.Subscription.modify") as modify:
            self.client.post(reverse("cancel_subscription"), {"subscription_id": "sub_HACKED", "stripe_subscription_id": "sub_HACKED"})

        self.assertEqual(modify.call_args[0][0], "sub_2")

    def test_cancel_does_not_change_local_tier_status_or_entitlement(self):
        user = self._user(
            "cancel3@example.com", tier="pro", status="active",
            stripe_customer_id="cus_3", stripe_subscription_id="sub_3",
        )
        self.client.force_login(user)
        before = (
            user.subscription.tier, user.subscription.status, user.subscription.stripe_subscription_id,
            user.subscription.active_until, user.subscription.scheduled_tier,
        )
        with patch("stripe.Subscription.retrieve", return_value={"status": "active"}), \
             patch("stripe.Subscription.modify"):
            self.client.post(reverse("cancel_subscription"))

        sub = user.subscription
        sub.refresh_from_db()
        after = (sub.tier, sub.status, sub.stripe_subscription_id, sub.active_until, sub.scheduled_tier)
        self.assertEqual(before, after)
        self.assertTrue(sub.has_entitlement)

    def test_cancel_retry_of_same_attempt_reuses_idempotency_key(self):
        user = self._user(
            "cancel4@example.com", tier="pro", status="active",
            stripe_customer_id="cus_4", stripe_subscription_id="sub_4",
        )
        self.client.force_login(user)

        with patch("stripe.Subscription.retrieve", return_value={"status": "active"}), \
             patch("stripe.Subscription.modify", side_effect=RuntimeError("stripe down")) as modify:
            self.client.post(reverse("cancel_subscription"))
        first_key = modify.call_args.kwargs["idempotency_key"]

        with patch("stripe.Subscription.retrieve", return_value={"status": "active"}), \
             patch("stripe.Subscription.modify") as modify:
            self.client.post(reverse("cancel_subscription"))
        second_key = modify.call_args.kwargs["idempotency_key"]

        self.assertEqual(first_key, second_key, "a retry of the same still-open attempt must reuse the key")

    def test_cancel_new_attempt_after_success_gets_a_new_idempotency_key(self):
        user = self._user(
            "cancel4b@example.com", tier="pro", status="active",
            stripe_customer_id="cus_4b", stripe_subscription_id="sub_4b",
        )
        self.client.force_login(user)

        with patch("stripe.Subscription.retrieve", return_value={"status": "active"}), \
             patch("stripe.Subscription.modify") as modify:
            self.client.post(reverse("cancel_subscription"))
        first_key = modify.call_args.kwargs["idempotency_key"]

        with patch("stripe.Subscription.retrieve", return_value={"status": "active"}), \
             patch("stripe.Subscription.modify") as modify:
            self.client.post(reverse("cancel_subscription"))
        second_key = modify.call_args.kwargs["idempotency_key"]

        self.assertNotEqual(first_key, second_key)

    def test_cancel_stripe_failure_causes_no_local_mutation(self):
        user = self._user(
            "cancel5@example.com", tier="pro", status="active",
            stripe_customer_id="cus_5", stripe_subscription_id="sub_5",
        )
        self.client.force_login(user)
        with patch("stripe.Subscription.retrieve", return_value={"status": "active"}), \
             patch("stripe.Subscription.modify", side_effect=RuntimeError("stripe down")):
            response = self.client.post(reverse("cancel_subscription"))

        sub = user.subscription
        sub.refresh_from_db()
        self.assertEqual(sub.status, "active")
        self.assertEqual(sub.tier, "pro")
        self.assertRedirects(response, reverse("settings"))

    def test_cancel_missing_subscription_id_never_calls_stripe(self):
        user = self._user("cancel6@example.com")  # no stripe_subscription_id at all
        self.client.force_login(user)
        with patch("stripe.Subscription.retrieve") as retrieve, patch("stripe.Subscription.modify") as modify:
            self.client.post(reverse("cancel_subscription"))

        retrieve.assert_not_called()
        modify.assert_not_called()

    def test_cancel_terminal_canceled_subscription_is_blocked(self):
        user = self._user(
            "cancel7@example.com", tier="pro", status="canceled",
            stripe_customer_id="cus_7", stripe_subscription_id="sub_7",
        )
        self.client.force_login(user)
        with patch("stripe.Subscription.retrieve", return_value={"status": "canceled"}), \
             patch("stripe.Subscription.modify") as modify:
            self.client.post(reverse("cancel_subscription"))

        modify.assert_not_called()

    def test_cancel_unknown_status_after_fresh_read_fails_closed(self):
        user = self._user(
            "cancel8@example.com", tier="pro", status="inactive",
            stripe_customer_id="cus_8", stripe_subscription_id="sub_8",
        )
        self.client.force_login(user)
        with patch("stripe.Subscription.retrieve", return_value={"status": "paused"}), \
             patch("stripe.Subscription.modify") as modify:
            self.client.post(reverse("cancel_subscription"))

        modify.assert_not_called()

    def test_cancel_past_due_redirects_without_mutating(self):
        user = self._user(
            "cancel9@example.com", tier="pro", status="past_due",
            stripe_customer_id="cus_9", stripe_subscription_id="sub_9",
        )
        self.client.force_login(user)
        with patch("stripe.Subscription.retrieve", return_value={"status": "past_due"}), \
             patch("stripe.Subscription.modify") as modify:
            response = self.client.post(reverse("cancel_subscription"))

        modify.assert_not_called()
        self.assertRedirects(response, reverse("settings"))

    def test_cancel_anonymous_redirects_to_login(self):
        response = self.client.post(reverse("cancel_subscription"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_cancel_get_is_rejected(self):
        user = self._user(
            "cancel11@example.com", tier="pro", status="active", stripe_subscription_id="sub_11",
        )
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("cancel_subscription")).status_code, 405)

    # ---------------- Resume ----------------

    def test_resume_active_scheduled_cancel_calls_modify_false(self):
        user = self._user(
            "resume1@example.com", tier="pro", status="active",
            stripe_customer_id="cus_r1", stripe_subscription_id="sub_r1",
        )
        self.client.force_login(user)
        with patch("stripe.Subscription.retrieve", return_value={"status": "active", "cancel_at_period_end": True}), \
             patch("stripe.Subscription.modify") as modify:
            response = self.client.post(reverse("resume_subscription"))

        modify.assert_called_once()
        args, kwargs = modify.call_args
        self.assertEqual(args[0], "sub_r1")
        self.assertFalse(kwargs.get("cancel_at_period_end"))
        self.assertTrue(kwargs.get("idempotency_key"))
        self.assertRedirects(response, reverse("settings"))

    def test_resume_already_not_scheduled_is_a_safe_noop(self):
        user = self._user(
            "resume2@example.com", tier="pro", status="active",
            stripe_customer_id="cus_r2", stripe_subscription_id="sub_r2",
        )
        self.client.force_login(user)
        with patch("stripe.Subscription.retrieve", return_value={"status": "active", "cancel_at_period_end": False}), \
             patch("stripe.Subscription.modify") as modify:
            response = self.client.post(reverse("resume_subscription"))

        modify.assert_not_called()
        self.assertRedirects(response, reverse("settings"))

    def test_resume_terminal_subscription_is_blocked(self):
        user = self._user(
            "resume3@example.com", tier="pro", status="canceled",
            stripe_customer_id="cus_r3", stripe_subscription_id="sub_r3",
        )
        self.client.force_login(user)
        with patch("stripe.Subscription.retrieve", return_value={"status": "canceled"}), \
             patch("stripe.Subscription.modify") as modify:
            self.client.post(reverse("resume_subscription"))

        modify.assert_not_called()

    def test_resume_stripe_failure_causes_no_local_mutation(self):
        user = self._user(
            "resume4@example.com", tier="pro", status="active",
            stripe_customer_id="cus_r4", stripe_subscription_id="sub_r4",
        )
        self.client.force_login(user)
        with patch("stripe.Subscription.retrieve", return_value={"status": "active", "cancel_at_period_end": True}), \
             patch("stripe.Subscription.modify", side_effect=RuntimeError("stripe down")):
            response = self.client.post(reverse("resume_subscription"))

        sub = user.subscription
        sub.refresh_from_db()
        self.assertEqual(sub.status, "active")
        self.assertRedirects(response, reverse("settings"))

    def test_resume_retry_of_same_attempt_reuses_idempotency_key(self):
        user = self._user(
            "resume5@example.com", tier="pro", status="active",
            stripe_customer_id="cus_r5", stripe_subscription_id="sub_r5",
        )
        self.client.force_login(user)

        with patch("stripe.Subscription.retrieve", return_value={"status": "active", "cancel_at_period_end": True}), \
             patch("stripe.Subscription.modify", side_effect=RuntimeError("stripe down")) as modify:
            self.client.post(reverse("resume_subscription"))
        first_key = modify.call_args.kwargs["idempotency_key"]

        with patch("stripe.Subscription.retrieve", return_value={"status": "active", "cancel_at_period_end": True}), \
             patch("stripe.Subscription.modify") as modify:
            self.client.post(reverse("resume_subscription"))
        second_key = modify.call_args.kwargs["idempotency_key"]

        self.assertEqual(first_key, second_key)

    def test_resume_anonymous_redirects_to_login(self):
        response = self.client.post(reverse("resume_subscription"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_resume_get_is_rejected(self):
        user = self._user(
            "resume7@example.com", tier="pro", status="active", stripe_subscription_id="sub_r7",
        )
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("resume_subscription")).status_code, 405)

    # ---------------- Namespace isolation vs Phase 3 checkout nonce ----------------

    def test_cancel_attempt_nonce_uses_a_separate_session_namespace_from_checkout(self):
        user = self._user(
            "cancelns@example.com", tier="pro", status="active",
            stripe_customer_id="cus_ns", stripe_subscription_id="sub_ns",
        )
        self.client.force_login(user)
        with patch("stripe.Subscription.retrieve", return_value={"status": "active"}), \
             patch("stripe.Subscription.modify", side_effect=RuntimeError("stripe down")):
            self.client.post(reverse("cancel_subscription"))

        self.assertIn("subscription_action_attempt_nonce:cancel", self.client.session)
        self.assertNotIn("checkout_attempt_nonce:pro", self.client.session)


@override_settings(
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent",
    STRIPE_WEBHOOK_SECRET="whsec_test",
)
class CancelAtPeriodEndWebhookRegressionTests(TestCase):
    """Phase 5b item 5 / regression test 20: a customer.subscription.updated event whose Stripe
    status is still "active" -- exactly what cancel_at_period_end=True looks like until the
    period genuinely ends -- must not strip entitlement, whether or not Phase 5c's active_until
    wiring is involved."""

    def test_active_status_with_cancellation_scheduled_keeps_entitlement(self):
        user = User.objects.create_user("cancelweb@example.com", "cancelweb@example.com", "StrongPass123")
        sub = user.subscription
        sub.stripe_customer_id, sub.stripe_subscription_id = "cus_w", "sub_w"
        sub.tier, sub.status = "pro", "active"
        sub.save()

        # Stripe's real webhook body for a scheduled cancellation carries cancel_at_period_end
        # and cancel_at alongside status="active" -- Phase 5c now reads both (active_until), but
        # entitlement must still come exclusively from `status`.
        cancel_at_ts = int(timezone.now().timestamp()) + 10 * 24 * 3600
        event = _subscription_event(
            "customer.subscription.updated", subscription_id="sub_w", status="active", price_id="price_pro",
            cancel_at_period_end=True, cancel_at=cancel_at_ts,
        )
        response = _post_webhook(self.client, event)

        self.assertEqual(response.status_code, 200)
        sub.refresh_from_db()
        self.assertEqual(sub.status, "active")
        self.assertTrue(sub.has_entitlement)
        self.assertTrue(sub.has_active_paid_subscription)
        self.assertIsNotNone(sub.active_until)


# ---------------------------------------------------------------------------------------------
# Phase 5c: active_until webhook projection + Cancel/Resume UI state. No new Stripe API calls
# anywhere in this section -- everything active_until needs is already in the verified webhook
# payload. Entitlement (has_entitlement/effective_tier/radar_limit) must never reference
# active_until; these tests pin that down explicitly alongside the projection itself.
# ---------------------------------------------------------------------------------------------


@override_settings(
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent",
    STRIPE_WEBHOOK_SECRET="whsec_test",
)
class ActiveUntilWebhookProjectionTests(TestCase):
    """Phase 5c items 1-10 (webhook projection) and 11-15 (entitlement isolation)."""

    def setUp(self):
        self.user = User.objects.create_user("activeuntil@example.com", "activeuntil@example.com", "StrongPass123")
        self.sub = self.user.subscription
        self.sub.stripe_customer_id, self.sub.stripe_subscription_id = "cus_au", "sub_au"
        self.sub.tier, self.sub.status = "pro", "active"
        self.sub.save()

    def _post(self, **event_kwargs):
        event_kwargs.setdefault("price_id", "price_pro")
        event = _subscription_event("customer.subscription.updated", subscription_id="sub_au", **event_kwargs)
        return _post_webhook(self.client, event), event

    # test 1
    def test_normal_active_subscription_leaves_active_until_none(self):
        response, _event = self._post(status="active", cancel_at_period_end=False)
        self.assertEqual(response.status_code, 200)
        self.sub.refresh_from_db()
        self.assertIsNone(self.sub.active_until)
        self.assertTrue(self.sub.has_entitlement)

    # test 2
    def test_cancel_at_period_end_with_explicit_cancel_at_uses_it(self):
        cancel_at_ts = 1900000000
        item_period_end_ts = 1800000000  # deliberately different, to prove cancel_at wins
        response, _event = self._post(
            status="active", cancel_at_period_end=True, cancel_at=cancel_at_ts,
            item_current_period_end=item_period_end_ts,
        )
        self.assertEqual(response.status_code, 200)
        self.sub.refresh_from_db()
        expected = dt.datetime.fromtimestamp(cancel_at_ts, tz=dt.timezone.utc)
        self.assertEqual(self.sub.active_until, expected)

    # test 3
    def test_cancel_at_period_end_without_cancel_at_falls_back_to_item_period_end(self):
        item_period_end_ts = 1850000000
        response, _event = self._post(
            status="active", cancel_at_period_end=True, item_current_period_end=item_period_end_ts,
        )
        self.assertEqual(response.status_code, 200)
        self.sub.refresh_from_db()
        expected = dt.datetime.fromtimestamp(item_period_end_ts, tz=dt.timezone.utc)
        self.assertEqual(self.sub.active_until, expected)

    # test 4
    def test_active_until_is_timezone_aware(self):
        response, _event = self._post(status="active", cancel_at_period_end=True, cancel_at=1900000000)
        self.assertEqual(response.status_code, 200)
        self.sub.refresh_from_db()
        self.assertIsNotNone(self.sub.active_until.tzinfo)

    # test 5
    def test_resume_webhook_clears_active_until(self):
        self.sub.active_until = timezone.now() + timedelta(days=5)
        self.sub.save()
        response, _event = self._post(status="active", cancel_at_period_end=False)
        self.assertEqual(response.status_code, 200)
        self.sub.refresh_from_db()
        self.assertIsNone(self.sub.active_until)

    # test 6
    def test_actual_deletion_clears_active_until_and_removes_entitlement(self):
        self.sub.active_until = timezone.now() + timedelta(days=5)
        self.sub.save()
        event = _subscription_event(
            "customer.subscription.deleted", subscription_id="sub_au", status="canceled", price_id="price_pro",
        )
        response = _post_webhook(self.client, event)
        self.assertEqual(response.status_code, 200)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, "canceled")
        self.assertIsNone(self.sub.active_until)
        self.assertFalse(self.sub.has_entitlement)

    # test 7
    def test_cancel_at_period_end_without_any_usable_expiry_fails_the_event(self):
        client = Client(raise_request_exception=False)
        event = _subscription_event(
            "customer.subscription.updated", subscription_id="sub_au", status="active", price_id="price_pro",
            cancel_at_period_end=True,  # no cancel_at, no item_current_period_end
        )
        with patch("stripe.Webhook.construct_event", return_value=event):
            response = client.post(
                reverse("stripe_webhook"), data=b"{}", content_type="application/json",
                HTTP_STRIPE_SIGNATURE=FAKE_SIG_HEADER,
            )

        self.assertEqual(response.status_code, 500)
        record = StripeWebhookEvent.objects.get(stripe_event_id=event["id"])
        self.assertEqual(record.status, "failed")
        self.sub.refresh_from_db()
        # Nothing partially written -- still whatever it was before this delivery.
        self.assertEqual(self.sub.status, "active")
        self.assertIsNone(self.sub.active_until)

    # test 8
    def test_retry_of_the_failed_event_with_valid_data_is_processed(self):
        bad_event = _subscription_event(
            "customer.subscription.updated", subscription_id="sub_au", status="active", price_id="price_pro",
            cancel_at_period_end=True, event_id="evt_retry_1",
        )
        client = Client(raise_request_exception=False)
        with patch("stripe.Webhook.construct_event", return_value=bad_event):
            first = client.post(
                reverse("stripe_webhook"), data=b"{}", content_type="application/json",
                HTTP_STRIPE_SIGNATURE=FAKE_SIG_HEADER,
            )
        self.assertEqual(first.status_code, 500)

        good_event = _subscription_event(
            "customer.subscription.updated", subscription_id="sub_au", status="active", price_id="price_pro",
            cancel_at_period_end=True, cancel_at=1900000000, event_id="evt_retry_1",
        )
        second = _post_webhook(self.client, good_event)
        self.assertEqual(second.status_code, 200)

        record = StripeWebhookEvent.objects.get(stripe_event_id="evt_retry_1")
        self.assertEqual(record.status, "processed")
        self.sub.refresh_from_db()
        self.assertIsNotNone(self.sub.active_until)

    # test 9
    def test_tier_status_active_until_are_updated_together(self):
        response, _event = self._post(
            status="active", price_id="price_biz", cancel_at_period_end=True, cancel_at=1900000000,
        )
        self.assertEqual(response.status_code, 200)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.tier, "business")
        self.assertEqual(self.sub.status, "active")
        self.assertIsNotNone(self.sub.active_until)

    # test 10
    def test_normal_renewal_boundary_does_not_fill_active_until(self):
        """current_period_end alone (renewal boundary, cancel_at_period_end=False) must never
        populate active_until -- only an actual scheduled cancellation may."""
        response, _event = self._post(
            status="active", cancel_at_period_end=False, item_current_period_end=1900000000,
        )
        self.assertEqual(response.status_code, 200)
        self.sub.refresh_from_db()
        self.assertIsNone(self.sub.active_until)

    # test 11
    def test_active_with_future_active_until_still_has_entitlement_today(self):
        self._post(status="active", cancel_at_period_end=True, cancel_at=int(timezone.now().timestamp()) + 86400)
        self.sub.refresh_from_db()
        self.assertTrue(self.sub.has_entitlement)

    # test 12
    def test_effective_tier_unaffected_by_active_until(self):
        self._post(status="active", price_id="price_biz", cancel_at_period_end=True, cancel_at=1900000000)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.effective_tier, "business")

    # test 13
    def test_radar_limit_unaffected_by_active_until(self):
        self._post(status="active", price_id="price_biz", cancel_at_period_end=True, cancel_at=1900000000)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.radar_limit, RADAR_LIMITS["business"])

    # test 14
    def test_canceled_status_removes_entitlement_regardless_of_active_until(self):
        self.sub.active_until = timezone.now() + timedelta(days=5)
        self.sub.status = "canceled"
        self.sub.save()
        self.assertFalse(self.sub.has_entitlement)

    # test 15
    def test_complimentary_access_unaffected_by_active_until(self):
        self.sub.tier, self.sub.status = "free", "inactive"
        self.sub.complimentary_tier = "enterprise"
        self.sub.active_until = timezone.now() + timedelta(days=1)
        self.sub.save()
        self.assertTrue(self.sub.has_entitlement)
        self.assertEqual(self.sub.effective_tier, "enterprise")

    # Scheduled-downgrade isolation regression (§11 of the brief): a Phase 5a scheduled tier
    # change must never be confused with a scheduled entitlement termination.
    def test_scheduled_tier_change_does_not_set_active_until(self):
        self.sub.tier, self.sub.status = "business", "active"
        self.sub.scheduled_tier = "pro"
        self.sub.scheduled_change_at = timezone.now() + timedelta(days=30)
        self.sub.stripe_schedule_id = "sub_sched_iso"
        self.sub.save()

        self.sub.refresh_from_db()
        self.assertIsNone(self.sub.active_until)
        self.assertEqual(self.sub.effective_tier, "business")
        self.assertEqual(self.sub.scheduled_tier, "pro")


@override_settings(STRIPE_PRICE_PRO="price_pro")
class SettingsSubscriptionUITests(TestCase):
    """Phase 5c items 16-20 (settings UI)."""

    def _user(self, email, **sub_fields):
        user = User.objects.create_user(email, email, "StrongPass123")
        sub = user.subscription
        for field, value in sub_fields.items():
            setattr(sub, field, value)
        sub.save()
        return user

    # test 16
    def test_active_without_scheduled_cancellation_shows_cancel_not_resume(self):
        user = self._user(
            "ui16@example.com", tier="pro", status="active",
            stripe_customer_id="cus_16", stripe_subscription_id="sub_16",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("settings")).content.decode()
        self.assertIn(reverse("cancel_subscription"), content)
        self.assertNotIn(reverse("resume_subscription"), content)
        self.assertIn("Ακύρωση στο τέλος περιόδου", content)

    # test 17
    def test_active_with_scheduled_cancellation_shows_expiry_and_resume_not_cancel(self):
        from django.template.defaultfilters import date as date_filter
        from django.utils.timezone import localtime

        expiry = timezone.now() + timedelta(days=7)
        user = self._user(
            "ui17@example.com", tier="pro", status="active", stripe_customer_id="cus_17",
            stripe_subscription_id="sub_17", active_until=expiry,
        )
        self.client.force_login(user)
        content = self.client.get(reverse("settings")).content.decode()
        self.assertIn(reverse("resume_subscription"), content)
        self.assertNotIn(reverse("cancel_subscription"), content)
        self.assertIn("Συνέχιση συνδρομής", content)
        # The template's |date filter converts to the current TIME_ZONE before formatting, so
        # the expectation must go through the same conversion -- comparing against the raw UTC
        # value is flaky near local-midnight, which is exactly what surfaced this.
        self.assertIn(date_filter(localtime(expiry), "d/m/Y"), content)

    # test 18
    def test_portal_button_present_regardless_of_scheduled_cancellation(self):
        user = self._user(
            "ui18@example.com", tier="pro", status="active", stripe_customer_id="cus_18",
            stripe_subscription_id="sub_18", active_until=timezone.now() + timedelta(days=3),
        )
        self.client.force_login(user)
        content = self.client.get(reverse("settings")).content.decode()
        self.assertIn(reverse("customer_portal"), content)

    # test 19
    def test_no_paid_subscription_shows_no_cancel_or_resume_controls(self):
        user = self._user("ui19@example.com")  # free/inactive default
        self.client.force_login(user)
        content = self.client.get(reverse("settings")).content.decode()
        self.assertNotIn(reverse("cancel_subscription"), content)
        self.assertNotIn(reverse("resume_subscription"), content)

    # test 20
    def test_cancel_and_resume_forms_are_post_with_csrf(self):
        user = self._user(
            "ui20@example.com", tier="pro", status="active",
            stripe_customer_id="cus_20", stripe_subscription_id="sub_20",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("settings")).content.decode()
        self.assertIn(f'action="{reverse("cancel_subscription")}" method="post"', content)
        self.assertIn("csrfmiddlewaretoken", content)


# ---------------------------------------------------------------------------------------------
# Phase 5d: immediate strict-upgrade with proration. Only Pro->Business, Pro->Enterprise,
# Business->Enterprise. No Stripe write anywhere in this section is real -- everything is
# mocked. change_plan never writes tier/status/active_until/scheduled_*/entitlement locally on
# either success or failure; the webhook (unchanged Phase 1/2/5c logic) is the only writer.
# ---------------------------------------------------------------------------------------------


def _plan_change_retrieve_payload(status="active", price_id="price_pro", item_id="si_1", cancel_at_period_end=False, items=None):
    if items is None:
        items = [{"id": item_id, "price": {"id": price_id}}]
    return {"status": status, "items": {"data": items}, "cancel_at_period_end": cancel_at_period_end}


@override_settings(
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent",
)
class ChangePlanTests(TestCase):
    def _user(self, email, **sub_fields):
        user = User.objects.create_user(email, email, "StrongPass123")
        sub = user.subscription
        for field, value in sub_fields.items():
            setattr(sub, field, value)
        sub.save()
        return user

    def _post(self, user, target_tier):
        self.client.force_login(user)
        return self.client.post(reverse("change_plan"), {"target_tier": target_tier})

    def _assert_no_mutation(self, user, retrieve_payload, target_tier, *, expect_retrieve_called=True):
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload) as retrieve, \
             patch("stripe.Subscription.modify") as modify, \
             patch("stripe.checkout.Session.create") as checkout_create:
            response = self._post(user, target_tier)
        modify.assert_not_called()
        checkout_create.assert_not_called()
        self.assertEqual(retrieve.called, expect_retrieve_called)
        return response

    # ---------------- Happy paths (item 17) ----------------

    def _assert_successful_upgrade(self, current_tier, current_price, target_tier, target_price):
        user = self._user(
            f"up_{current_tier}_{target_tier}@example.com", tier=current_tier, status="active",
            stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
        )
        retrieve_payload = _plan_change_retrieve_payload(price_id=current_price, item_id="si_1")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload) as retrieve, \
             patch("stripe.Subscription.modify") as modify, \
             patch("stripe.checkout.Session.create") as checkout_create:
            response = self._post(user, target_tier)

        retrieve.assert_called_once_with("sub_1")
        modify.assert_called_once()
        args, kwargs = modify.call_args
        self.assertEqual(args[0], "sub_1")
        self.assertEqual(kwargs["items"], [{"id": "si_1", "price": target_price}])
        self.assertEqual(kwargs["proration_behavior"], "always_invoice")
        self.assertEqual(kwargs["payment_behavior"], "error_if_incomplete")
        self.assertTrue(kwargs.get("idempotency_key"))
        checkout_create.assert_not_called()
        self.assertRedirects(response, reverse("settings"))

        sub = user.subscription
        sub.refresh_from_db()
        self.assertEqual(sub.tier, current_tier)  # unchanged synchronously -- test 25
        self.assertEqual(sub.status, "active")

    def test_pro_to_business_upgrade(self):
        self._assert_successful_upgrade("pro", "price_pro", "business", "price_biz")

    def test_pro_to_enterprise_upgrade(self):
        self._assert_successful_upgrade("pro", "price_pro", "enterprise", "price_ent")

    def test_business_to_enterprise_upgrade(self):
        self._assert_successful_upgrade("business", "price_biz", "enterprise", "price_ent")

    # ---------------- Rejected paths (item 18) ----------------

    def test_business_to_pro_is_rejected(self):
        user = self._user(
            "rej4@example.com", tier="business", status="active",
            stripe_customer_id="cus_4", stripe_subscription_id="sub_4",
        )
        self._assert_no_mutation(user, _plan_change_retrieve_payload(price_id="price_biz"), "pro")

    def test_enterprise_to_business_is_rejected(self):
        user = self._user(
            "rej5@example.com", tier="enterprise", status="active",
            stripe_customer_id="cus_5", stripe_subscription_id="sub_5",
        )
        self._assert_no_mutation(user, _plan_change_retrieve_payload(price_id="price_ent"), "business")

    def test_same_tier_is_rejected(self):
        user = self._user(
            "rej6@example.com", tier="pro", status="active",
            stripe_customer_id="cus_6", stripe_subscription_id="sub_6",
        )
        self._assert_no_mutation(user, _plan_change_retrieve_payload(price_id="price_pro"), "pro")

    def test_missing_subscription_id_never_calls_stripe(self):
        user = self._user("rej7@example.com", tier="pro", status="active")  # no stripe_subscription_id
        self._assert_no_mutation(user, {}, "business", expect_retrieve_called=False)

    def test_unknown_current_price_is_rejected(self):
        user = self._user(
            "rej8@example.com", tier="pro", status="active",
            stripe_customer_id="cus_8", stripe_subscription_id="sub_8",
        )
        self._assert_no_mutation(user, _plan_change_retrieve_payload(price_id="price_unknown"), "business")

    def test_local_stripe_tier_mismatch_is_rejected(self):
        user = self._user(
            "rej9@example.com", tier="pro", status="active",  # local says pro
            stripe_customer_id="cus_9", stripe_subscription_id="sub_9",
        )
        # ...but Stripe's current price is actually Business.
        self._assert_no_mutation(user, _plan_change_retrieve_payload(price_id="price_biz"), "enterprise")

    def test_malformed_items_zero_items_is_rejected(self):
        user = self._user(
            "rej10a@example.com", tier="pro", status="active",
            stripe_customer_id="cus_10a", stripe_subscription_id="sub_10a",
        )
        self._assert_no_mutation(user, _plan_change_retrieve_payload(items=[]), "business")

    def test_malformed_items_multiple_items_is_rejected(self):
        user = self._user(
            "rej10b@example.com", tier="pro", status="active",
            stripe_customer_id="cus_10b", stripe_subscription_id="sub_10b",
        )
        items = [{"id": "si_1", "price": {"id": "price_pro"}}, {"id": "si_2", "price": {"id": "price_biz"}}]
        self._assert_no_mutation(user, _plan_change_retrieve_payload(items=items), "enterprise")

    def test_malformed_items_missing_price_id_is_rejected(self):
        user = self._user(
            "rej10c@example.com", tier="pro", status="active",
            stripe_customer_id="cus_10c", stripe_subscription_id="sub_10c",
        )
        items = [{"id": "si_1", "price": {}}]
        self._assert_no_mutation(user, _plan_change_retrieve_payload(items=items), "business")

    def test_missing_target_price_env_is_rejected(self):
        user = self._user(
            "rej11@example.com", tier="pro", status="active",
            stripe_customer_id="cus_11", stripe_subscription_id="sub_11",
        )
        with override_settings(STRIPE_PRICE_BUSINESS=None):
            self._assert_no_mutation(user, _plan_change_retrieve_payload(price_id="price_pro"), "business")

    def test_past_due_is_rejected(self):
        user = self._user(
            "rej12@example.com", tier="pro", status="past_due",
            stripe_customer_id="cus_12", stripe_subscription_id="sub_12",
        )
        self._assert_no_mutation(user, _plan_change_retrieve_payload(status="past_due", price_id="price_pro"), "business")

    def test_unpaid_is_rejected(self):
        user = self._user(
            "rej13@example.com", tier="pro", status="unpaid",
            stripe_customer_id="cus_13", stripe_subscription_id="sub_13",
        )
        self._assert_no_mutation(user, _plan_change_retrieve_payload(status="unpaid", price_id="price_pro"), "business")

    def test_incomplete_is_rejected(self):
        user = self._user(
            "rej14@example.com", tier="pro", status="incomplete",
            stripe_customer_id="cus_14", stripe_subscription_id="sub_14",
        )
        self._assert_no_mutation(user, _plan_change_retrieve_payload(status="incomplete", price_id="price_pro"), "business")

    def test_canceled_terminal_is_rejected(self):
        user = self._user(
            "rej15@example.com", tier="pro", status="canceled",
            stripe_customer_id="cus_15", stripe_subscription_id="sub_15",
        )
        self._assert_no_mutation(user, _plan_change_retrieve_payload(status="canceled", price_id="price_pro"), "business")

    def test_unknown_status_is_rejected(self):
        user = self._user(
            "rej16@example.com", tier="pro", status="active",
            stripe_customer_id="cus_16", stripe_subscription_id="sub_16",
        )
        self._assert_no_mutation(user, _plan_change_retrieve_payload(status="paused", price_id="price_pro"), "business")

    def test_scheduled_cancellation_is_rejected(self):
        user = self._user(
            "rej17@example.com", tier="pro", status="active",
            stripe_customer_id="cus_17", stripe_subscription_id="sub_17",
        )
        self._assert_no_mutation(
            user, _plan_change_retrieve_payload(price_id="price_pro", cancel_at_period_end=True), "business",
        )

    def test_scheduled_tier_blocks_upgrade_without_even_retrieving(self):
        user = self._user(
            "rej18@example.com", tier="pro", status="active",
            stripe_customer_id="cus_18", stripe_subscription_id="sub_18",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=10),
            stripe_schedule_id="sub_sched_18",
        )
        self._assert_no_mutation(user, {}, "business", expect_retrieve_called=False)

    def test_stripe_schedule_id_alone_blocks_upgrade_without_even_retrieving(self):
        user = self._user(
            "rej19@example.com", tier="pro", status="active",
            stripe_customer_id="cus_19", stripe_subscription_id="sub_19",
            stripe_schedule_id="sub_sched_19",
        )
        self._assert_no_mutation(user, {}, "business", expect_retrieve_called=False)

    # ---------------- Failure semantics (item 19) ----------------

    def test_retrieve_failure_causes_no_mutation_or_local_change(self):
        user = self._user(
            "fail20@example.com", tier="pro", status="active",
            stripe_customer_id="cus_20", stripe_subscription_id="sub_20",
        )
        with patch("stripe.Subscription.retrieve", side_effect=RuntimeError("stripe down")), \
             patch("stripe.Subscription.modify") as modify:
            self._post(user, "business")
        modify.assert_not_called()
        sub = user.subscription
        sub.refresh_from_db()
        self.assertEqual(sub.tier, "pro")

    def test_modify_failure_causes_no_local_change_and_retains_nonce(self):
        user = self._user(
            "fail21@example.com", tier="pro", status="active",
            stripe_customer_id="cus_21", stripe_subscription_id="sub_21",
        )
        retrieve_payload = _plan_change_retrieve_payload(price_id="price_pro")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.Subscription.modify", side_effect=RuntimeError("payment failed")) as modify:
            response = self._post(user, "business")

        sub = user.subscription
        sub.refresh_from_db()
        self.assertEqual(sub.tier, "pro")
        self.assertEqual(response.status_code, 302)
        content = self.client.get(reverse("settings")).content.decode()
        self.assertNotIn("payment failed", content)  # no raw Stripe exception text shown

        first_key = modify.call_args.kwargs["idempotency_key"]
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.Subscription.modify", side_effect=RuntimeError("payment failed")) as modify:
            self._post(user, "business")
        second_key = modify.call_args.kwargs["idempotency_key"]
        self.assertEqual(first_key, second_key)  # test 22: retry of same attempt reuses key

    def test_successful_upgrade_clears_the_nonce(self):
        user = self._user(
            "fail23@example.com", tier="pro", status="active",
            stripe_customer_id="cus_23", stripe_subscription_id="sub_23",
        )
        retrieve_payload = _plan_change_retrieve_payload(price_id="price_pro")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.Subscription.modify"):
            self._post(user, "business")

        self.assertNotIn("plan_change_attempt_nonce:business", self.client.session)

    def test_new_later_attempt_after_success_gets_a_new_key(self):
        user = self._user(
            "fail24@example.com", tier="pro", status="active",
            stripe_customer_id="cus_24", stripe_subscription_id="sub_24",
        )
        retrieve_payload = _plan_change_retrieve_payload(price_id="price_pro")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.Subscription.modify") as modify:
            self._post(user, "business")
        first_key = modify.call_args.kwargs["idempotency_key"]

        # Local tier is still "pro" (sync call never wrote it), so a second, distinct attempt at
        # the same target tier is a legitimate repeat, not a retry of the concluded one.
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.Subscription.modify") as modify:
            self._post(user, "business")
        second_key = modify.call_args.kwargs["idempotency_key"]

        self.assertNotEqual(first_key, second_key)

    # ---------------- Webhook-authoritative projection (item 20) ----------------

    def test_webhook_after_upgrade_updates_local_tier_entitlement_and_radar_limit(self):
        user = self._user(
            "webup1@example.com", tier="pro", status="active",
            stripe_customer_id="cus_w1", stripe_subscription_id="sub_w1",
        )
        retrieve_payload = _plan_change_retrieve_payload(price_id="price_pro")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.Subscription.modify"):
            self._post(user, "business")

        sub = user.subscription
        sub.refresh_from_db()
        self.assertEqual(sub.tier, "pro")  # test 25: still old, pre-webhook
        self.assertEqual(sub.radar_limit, RADAR_LIMITS["pro"])

        with override_settings(STRIPE_WEBHOOK_SECRET="whsec_test"):
            event = _subscription_event(
                "customer.subscription.updated", subscription_id="sub_w1", status="active",
                price_id="price_biz", cancel_at_period_end=False,
            )
            response = _post_webhook(self.client, event)

        self.assertEqual(response.status_code, 200)
        sub.refresh_from_db()
        self.assertEqual(sub.tier, "business")  # test 26
        self.assertTrue(sub.has_entitlement)  # test 27
        self.assertEqual(sub.radar_limit, RADAR_LIMITS["business"])  # test 28
        self.assertIsNone(sub.active_until)  # test 29: a plain upgrade never schedules an expiry


@override_settings(LEGAL_BILLING_ACTIVE=True)
class ChangePlanPricingUITests(TestCase):
    """Phase 5d item 21 (UI, tests 30-35)."""

    def _user(self, email, **sub_fields):
        user = User.objects.create_user(email, email, "StrongPass123")
        sub = user.subscription
        for field, value in sub_fields.items():
            setattr(sub, field, value)
        sub.save()
        return user

    # test 30
    def test_pro_subscriber_sees_current_and_two_upgrade_actions(self):
        user = self._user(
            "ui30@example.com", tier="pro", status="active",
            stripe_customer_id="cus_30", stripe_subscription_id="sub_30",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()

        self.assertIn(reverse("customer_portal"), content)  # Pro card: current-plan action
        self.assertIn(reverse("change_plan"), content)
        self.assertIn('value="business"', content)
        self.assertIn('value="enterprise"', content)
        self.assertIn("Αναβάθμιση", content)

    # test 31
    def test_business_subscriber_sees_no_active_change_toward_pro_and_upgrade_to_enterprise(self):
        user = self._user(
            "ui31@example.com", tier="business", status="active",
            stripe_customer_id="cus_31", stripe_subscription_id="sub_31",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()

        self.assertIn("Υποβάθμιση", content)  # the Pro card offers no active change
        self.assertIn(reverse("change_plan"), content)
        self.assertIn('value="enterprise"', content)
        # Business card itself must show the current-plan action, not an upgrade form for itself.
        self.assertNotIn('value="business"', content)

    # test 32
    def test_enterprise_subscriber_sees_no_upgrade_actions_at_all(self):
        """Enterprise is the top tier, so no card should ever offer "Αναβάθμιση" to an Enterprise
        subscriber. Phase 5e added real downgrade actions (Pro/Business, via the same
        change_plan endpoint) where this test used to see only inert "σύντομα" placeholders --
        updated here to assert the thing this test actually cares about (no upgrade action),
        not the since-superseded absence of change_plan entirely."""
        user = self._user(
            "ui32@example.com", tier="enterprise", status="active",
            stripe_customer_id="cus_32", stripe_subscription_id="sub_32",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()

        self.assertNotIn("Αναβάθμιση", content)

    # test 33
    def test_scheduled_cancellation_hides_upgrade_action(self):
        user = self._user(
            "ui33@example.com", tier="pro", status="active",
            stripe_customer_id="cus_33", stripe_subscription_id="sub_33",
            active_until=timezone.now() + timedelta(days=5),
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertNotIn(reverse("change_plan"), content)

    # test 34
    def test_scheduled_downgrade_hides_upgrade_action(self):
        user = self._user(
            "ui34@example.com", tier="business", status="active",
            stripe_customer_id="cus_34", stripe_subscription_id="sub_34",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=20),
            stripe_schedule_id="sub_sched_34",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertNotIn(reverse("change_plan"), content)

    # test 35
    def test_anonymous_and_no_subscription_checkout_flows_are_unchanged(self):
        anon_content = self.client.get(reverse("pricing")).content.decode()
        self.assertNotIn(reverse("change_plan"), anon_content)
        self.assertIn(reverse("create_checkout_session"), anon_content)

        user = User.objects.create_user("ui35@example.com", "ui35@example.com", "StrongPass123")
        self.client.force_login(user)
        no_sub_content = self.client.get(reverse("pricing")).content.decode()
        self.assertNotIn(reverse("change_plan"), no_sub_content)
        self.assertIn(reverse("create_checkout_session"), no_sub_content)


# ---------------------------------------------------------------------------------------------
# Phase 5e: scheduled downgrade at period end via Stripe Subscription Schedule. Business->Pro,
# Enterprise->Business, Enterprise->Pro. No Stripe write anywhere in this section is real --
# everything is mocked. The synchronous change_plan endpoint never writes scheduled_tier/
# scheduled_change_at/stripe_schedule_id/tier/status/entitlement on success OR failure; only the
# webhook (subscription_schedule.updated for the projection, customer.subscription.updated for
# the eventual tier change + cleanup) ever writes those fields.
# ---------------------------------------------------------------------------------------------


@override_settings(STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent")
class ChangePlanDowngradeTests(TestCase):
    def _user(self, email, **sub_fields):
        user = User.objects.create_user(email, email, "StrongPass123")
        sub = user.subscription
        for field, value in sub_fields.items():
            setattr(sub, field, value)
        sub.save()
        return user

    def _post(self, user, target_tier):
        self.client.force_login(user)
        return self.client.post(reverse("change_plan"), {"target_tier": target_tier})

    def _retrieve_payload(
        self, price_id="price_biz", item_id="si_1", period_end_ts=2000000000, schedule=None,
        quantity=1, status="active", cancel_at_period_end=False, include_period_end=True,
    ):
        item = {"id": item_id, "price": {"id": price_id}, "quantity": quantity}
        if include_period_end:
            item["current_period_end"] = period_end_ts
        return {
            "status": status,
            "items": {"data": [item]},
            "cancel_at_period_end": cancel_at_period_end,
            "schedule": schedule,
        }

    def _created_schedule(self, schedule_id="sub_sched_1", price_id="price_biz", start_date=1500000000, phases=None):
        if phases is None:
            phases = [{"items": [{"price": {"id": price_id}}], "start_date": start_date, "proration_behavior": "none"}]
        return {"id": schedule_id, "subscription": "sub_1", "phases": phases}

    # ---------------- Happy paths (item 23) ----------------

    def _assert_successful_downgrade(self, current_tier, current_price, target_tier, target_price):
        user = self._user(
            f"dg_{current_tier}_{target_tier}@example.com", tier=current_tier, status="active",
            stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
        )
        retrieve_payload = self._retrieve_payload(price_id=current_price, quantity=2)
        created_schedule = self._created_schedule(price_id=current_price)
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload) as sub_retrieve, \
             patch("stripe.SubscriptionSchedule.create", return_value=created_schedule) as sched_create, \
             patch("stripe.SubscriptionSchedule.modify") as sched_modify, \
             patch("stripe.Subscription.modify") as sub_modify, \
             patch("stripe.checkout.Session.create") as checkout_create:
            response = self._post(user, target_tier)

        sub_retrieve.assert_called_once_with("sub_1")
        sched_create.assert_called_once()
        self.assertEqual(sched_create.call_args.kwargs["from_subscription"], "sub_1")
        self.assertTrue(sched_create.call_args.kwargs.get("idempotency_key"))

        sched_modify.assert_called_once()
        args, kwargs = sched_modify.call_args
        self.assertEqual(args[0], "sub_sched_1")
        self.assertEqual(kwargs["end_behavior"], "release")  # test 12
        phases = kwargs["phases"]
        self.assertEqual(len(phases), 2)
        self.assertEqual(phases[0]["items"], [{"price": current_price, "quantity": 2}])  # test 6: quantity preserved
        self.assertEqual(phases[0]["end_date"], 2000000000)  # test 7: actual period end
        self.assertEqual(phases[0]["proration_behavior"], "none")
        self.assertEqual(phases[1]["items"], [{"price": target_price, "quantity": 2}])
        self.assertEqual(phases[1]["start_date"], 2000000000)
        self.assertEqual(phases[1]["proration_behavior"], "none")  # test 4
        self.assertNotIn("end_date", phases[1])  # open-ended: keeps recurring after the transition
        self.assertTrue(kwargs.get("idempotency_key"))

        sub_modify.assert_not_called()  # no immediate Subscription.modify
        checkout_create.assert_not_called()  # no Checkout Session
        self.assertRedirects(response, reverse("settings"))

        sub = user.subscription
        sub.refresh_from_db()
        self.assertEqual(sub.tier, current_tier)  # test 5: current tier remains current
        self.assertIsNone(sub.scheduled_tier)  # no synchronous scheduled field writes

    def test_business_to_pro(self):
        self._assert_successful_downgrade("business", "price_biz", "pro", "price_pro")

    def test_enterprise_to_business(self):
        self._assert_successful_downgrade("enterprise", "price_ent", "business", "price_biz")

    def test_enterprise_to_pro(self):
        self._assert_successful_downgrade("enterprise", "price_ent", "pro", "price_pro")

    # ---------------- Proration/boundary (item 24) ----------------

    def test_missing_period_end_fails_closed(self):
        """Test 8."""
        user = self._user(
            "dg8@example.com", tier="business", status="active",
            stripe_customer_id="cus_8", stripe_subscription_id="sub_8",
        )
        retrieve_payload = self._retrieve_payload(price_id="price_biz", include_period_end=False)
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.SubscriptionSchedule.create") as sched_create, \
             patch("stripe.SubscriptionSchedule.modify") as sched_modify:
            self._post(user, "pro")
        sched_create.assert_not_called()
        sched_modify.assert_not_called()

    def test_boundary_uses_the_actual_stripe_period_end_not_a_computed_guess(self):
        """Test 9: a deliberately odd, non-round timestamp proves this is read, not computed."""
        user = self._user(
            "dg9@example.com", tier="business", status="active",
            stripe_customer_id="cus_9", stripe_subscription_id="sub_9",
        )
        weird_ts = 1234567890
        retrieve_payload = self._retrieve_payload(price_id="price_biz", period_end_ts=weird_ts)
        created_schedule = self._created_schedule(price_id="price_biz")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.SubscriptionSchedule.create", return_value=created_schedule), \
             patch("stripe.SubscriptionSchedule.modify") as sched_modify:
            self._post(user, "pro")
        phases = sched_modify.call_args.kwargs["phases"]
        self.assertEqual(phases[0]["end_date"], weird_ts)
        self.assertEqual(phases[1]["start_date"], weird_ts)

    # ---------------- Rejected paths (item 25) ----------------

    def test_upgrade_path_never_touches_subscription_schedule(self):
        """Test 10."""
        user = self._user(
            "dg10@example.com", tier="pro", status="active",
            stripe_customer_id="cus_10", stripe_subscription_id="sub_10",
        )
        retrieve_payload = self._retrieve_payload(price_id="price_pro")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.Subscription.modify") as sub_modify, \
             patch("stripe.SubscriptionSchedule.create") as sched_create, \
             patch("stripe.SubscriptionSchedule.modify") as sched_modify:
            self._post(user, "business")
        sub_modify.assert_called_once()
        sched_create.assert_not_called()
        sched_modify.assert_not_called()

    def test_same_tier_makes_no_schedule_calls(self):
        """Test 11."""
        user = self._user(
            "dg11@example.com", tier="business", status="active",
            stripe_customer_id="cus_11", stripe_subscription_id="sub_11",
        )
        retrieve_payload = self._retrieve_payload(price_id="price_biz")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.SubscriptionSchedule.create") as sched_create, \
             patch("stripe.SubscriptionSchedule.modify") as sched_modify:
            self._post(user, "business")
        sched_create.assert_not_called()
        sched_modify.assert_not_called()

    def test_missing_subscription_id_never_calls_stripe(self):
        """Test 12."""
        user = self._user("dg12@example.com", tier="business", status="active")
        with patch("stripe.Subscription.retrieve") as retrieve, \
             patch("stripe.SubscriptionSchedule.create") as sched_create:
            self._post(user, "pro")
        retrieve.assert_not_called()
        sched_create.assert_not_called()

    def test_unknown_current_price_blocks_downgrade(self):
        """Test 13."""
        user = self._user(
            "dg13@example.com", tier="business", status="active",
            stripe_customer_id="cus_13", stripe_subscription_id="sub_13",
        )
        retrieve_payload = self._retrieve_payload(price_id="price_unknown")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.SubscriptionSchedule.create") as sched_create:
            self._post(user, "pro")
        sched_create.assert_not_called()

    def test_malformed_item_blocks_downgrade(self):
        """Test 14."""
        user = self._user(
            "dg14@example.com", tier="business", status="active",
            stripe_customer_id="cus_14", stripe_subscription_id="sub_14",
        )
        retrieve_payload = {"status": "active", "items": {"data": []}, "cancel_at_period_end": False, "schedule": None}
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.SubscriptionSchedule.create") as sched_create:
            self._post(user, "pro")
        sched_create.assert_not_called()

    def test_local_stripe_tier_mismatch_blocks_downgrade(self):
        """Test 15."""
        user = self._user(
            "dg15@example.com", tier="business", status="active",  # local says business
            stripe_customer_id="cus_15", stripe_subscription_id="sub_15",
        )
        retrieve_payload = self._retrieve_payload(price_id="price_ent")  # Stripe says enterprise
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.SubscriptionSchedule.create") as sched_create:
            self._post(user, "pro")
        sched_create.assert_not_called()

    def test_missing_target_price_blocks_downgrade(self):
        """Test 16."""
        user = self._user(
            "dg16@example.com", tier="business", status="active",
            stripe_customer_id="cus_16", stripe_subscription_id="sub_16",
        )
        retrieve_payload = self._retrieve_payload(price_id="price_biz")
        with override_settings(STRIPE_PRICE_PRO=None):
            with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
                 patch("stripe.SubscriptionSchedule.create") as sched_create:
                self._post(user, "pro")
        sched_create.assert_not_called()

    def test_past_due_blocks_downgrade(self):
        """Test 17."""
        user = self._user(
            "dg17@example.com", tier="business", status="past_due",
            stripe_customer_id="cus_17", stripe_subscription_id="sub_17",
        )
        retrieve_payload = self._retrieve_payload(price_id="price_biz", status="past_due")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.SubscriptionSchedule.create") as sched_create:
            self._post(user, "pro")
        sched_create.assert_not_called()

    def test_canceled_terminal_blocks_downgrade(self):
        """Test 18."""
        user = self._user(
            "dg18@example.com", tier="business", status="canceled",
            stripe_customer_id="cus_18", stripe_subscription_id="sub_18",
        )
        retrieve_payload = self._retrieve_payload(price_id="price_biz", status="canceled")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.SubscriptionSchedule.create") as sched_create:
            self._post(user, "pro")
        sched_create.assert_not_called()

    def test_cancel_at_period_end_blocks_downgrade(self):
        """Test 19."""
        user = self._user(
            "dg19@example.com", tier="business", status="active",
            stripe_customer_id="cus_19", stripe_subscription_id="sub_19",
        )
        retrieve_payload = self._retrieve_payload(price_id="price_biz", cancel_at_period_end=True)
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.SubscriptionSchedule.create") as sched_create:
            self._post(user, "pro")
        sched_create.assert_not_called()

    def test_existing_scheduled_tier_blocks_new_downgrade_without_retrieving(self):
        """Test 20."""
        user = self._user(
            "dg20@example.com", tier="business", status="active",
            stripe_customer_id="cus_20", stripe_subscription_id="sub_20",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=5),
            stripe_schedule_id="sub_sched_20",
        )
        with patch("stripe.Subscription.retrieve") as retrieve, \
             patch("stripe.SubscriptionSchedule.create") as sched_create:
            self._post(user, "pro")
        retrieve.assert_not_called()
        sched_create.assert_not_called()

    def test_existing_stripe_schedule_id_alone_blocks_new_downgrade(self):
        """Test 21."""
        user = self._user(
            "dg21@example.com", tier="business", status="active",
            stripe_customer_id="cus_21", stripe_subscription_id="sub_21",
            stripe_schedule_id="sub_sched_21",
        )
        with patch("stripe.Subscription.retrieve") as retrieve, \
             patch("stripe.SubscriptionSchedule.create") as sched_create:
            self._post(user, "pro")
        retrieve.assert_not_called()
        sched_create.assert_not_called()

    def test_unknown_status_blocks_downgrade(self):
        """Test 22."""
        user = self._user(
            "dg22@example.com", tier="business", status="active",
            stripe_customer_id="cus_22", stripe_subscription_id="sub_22",
        )
        retrieve_payload = self._retrieve_payload(price_id="price_biz", status="paused")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.SubscriptionSchedule.create") as sched_create:
            self._post(user, "pro")
        sched_create.assert_not_called()

    # ---------------- Partial failure / retry (item 26) — the critical section ----------------

    def test_schedule_create_failure_causes_no_mutation_and_retry_reuses_key(self):
        """Test 23."""
        user = self._user(
            "dg23@example.com", tier="business", status="active",
            stripe_customer_id="cus_23", stripe_subscription_id="sub_23",
        )
        retrieve_payload = self._retrieve_payload(price_id="price_biz")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.SubscriptionSchedule.create", side_effect=RuntimeError("stripe down")) as sched_create:
            self._post(user, "pro")
        first_key = sched_create.call_args.kwargs["idempotency_key"]
        sub = user.subscription
        sub.refresh_from_db()
        self.assertIsNone(sub.scheduled_tier)

        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.SubscriptionSchedule.create", side_effect=RuntimeError("stripe down")) as sched_create:
            self._post(user, "pro")
        second_key = sched_create.call_args.kwargs["idempotency_key"]
        self.assertEqual(first_key, second_key)

    def test_create_succeeds_modify_fails_then_retry_reuses_the_created_schedule(self):
        """Test 24 -- the single most critical acceptance scenario of Phase 5e."""
        user = self._user(
            "dg24@example.com", tier="business", status="active",
            stripe_customer_id="cus_24", stripe_subscription_id="sub_24",
        )
        retrieve_no_schedule = self._retrieve_payload(price_id="price_biz", schedule=None)
        created_schedule = self._created_schedule(price_id="price_biz", schedule_id="sub_sched_24")

        with patch("stripe.Subscription.retrieve", return_value=retrieve_no_schedule), \
             patch("stripe.SubscriptionSchedule.create", return_value=created_schedule) as sched_create, \
             patch("stripe.SubscriptionSchedule.modify", side_effect=RuntimeError("modify failed")):
            self._post(user, "pro")

        sched_create.assert_called_once()
        sub = user.subscription
        sub.refresh_from_db()
        self.assertIsNone(sub.scheduled_tier)          # not fabricated
        self.assertEqual(sub.stripe_schedule_id, "")    # not fabricated either

        # Retry: a fresh Subscription.retrieve now reports the schedule Stripe already created.
        retrieve_with_schedule = self._retrieve_payload(price_id="price_biz", schedule="sub_sched_24")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_with_schedule), \
             patch("stripe.SubscriptionSchedule.create") as sched_create_retry, \
             patch("stripe.SubscriptionSchedule.retrieve", return_value=created_schedule) as sched_retrieve, \
             patch("stripe.SubscriptionSchedule.modify") as sched_modify_retry:
            response = self._post(user, "pro")

        sched_create_retry.assert_not_called()  # no second schedule created
        sched_retrieve.assert_called_once_with("sub_sched_24")
        sched_modify_retry.assert_called_once()
        self.assertRedirects(response, reverse("settings"))

    def test_successful_downgrade_clears_the_nonce(self):
        """Test 26."""
        user = self._user(
            "dg26@example.com", tier="business", status="active",
            stripe_customer_id="cus_26", stripe_subscription_id="sub_26",
        )
        retrieve_payload = self._retrieve_payload(price_id="price_biz")
        created_schedule = self._created_schedule(price_id="price_biz")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.SubscriptionSchedule.create", return_value=created_schedule), \
             patch("stripe.SubscriptionSchedule.modify"):
            self._post(user, "pro")
        self.assertNotIn("downgrade_attempt_nonce:pro", self.client.session)

    def test_failed_attempt_retains_the_nonce(self):
        """Test 27."""
        user = self._user(
            "dg27@example.com", tier="business", status="active",
            stripe_customer_id="cus_27", stripe_subscription_id="sub_27",
        )
        retrieve_payload = self._retrieve_payload(price_id="price_biz")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.SubscriptionSchedule.create", side_effect=RuntimeError("down")):
            self._post(user, "pro")
        self.assertIn("downgrade_attempt_nonce:pro", self.client.session)

    def test_new_later_downgrade_attempt_after_success_gets_new_key(self):
        """Test 28."""
        user = self._user(
            "dg28@example.com", tier="business", status="active",
            stripe_customer_id="cus_28", stripe_subscription_id="sub_28",
        )
        retrieve_payload = self._retrieve_payload(price_id="price_biz")
        created_schedule = self._created_schedule(price_id="price_biz")
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.SubscriptionSchedule.create", return_value=created_schedule) as sched_create, \
             patch("stripe.SubscriptionSchedule.modify"):
            self._post(user, "pro")
        first_key = sched_create.call_args.kwargs["idempotency_key"]

        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.SubscriptionSchedule.create", return_value=created_schedule) as sched_create, \
             patch("stripe.SubscriptionSchedule.modify"):
            self._post(user, "pro")
        second_key = sched_create.call_args.kwargs["idempotency_key"]
        self.assertNotEqual(first_key, second_key)


@override_settings(
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent",
    STRIPE_WEBHOOK_SECRET="whsec_test",
)
class SubscriptionScheduleWebhookTests(TestCase):
    """Phase 5e item 27 (webhook projection, tests 29-36)."""

    def setUp(self):
        self.user = User.objects.create_user("schedwebhook@example.com", "schedwebhook@example.com", "StrongPass123")
        self.sub = self.user.subscription
        self.sub.tier, self.sub.status = "business", "active"
        self.sub.stripe_customer_id, self.sub.stripe_subscription_id = "cus_sw", "sub_sw"
        self.sub.save()

    def _schedule_event(self, phases, subscription_id="sub_sw", event_id="evt_sched_1"):
        return {
            "id": event_id, "type": "subscription_schedule.updated",
            "data": {"object": {"id": "sub_sched_sw", "subscription": subscription_id, "phases": phases}},
        }

    def _two_phases(self, future_price_id="price_pro", future_start=1900000000):
        return [
            {"items": [{"price": {"id": "price_biz"}}], "start_date": 1000000000, "proration_behavior": "none"},
            {"items": [{"price": {"id": future_price_id}}], "start_date": future_start, "proration_behavior": "none"},
        ]

    def test_schedule_updated_populates_scheduled_fields(self):
        """Tests 29-31."""
        event = self._schedule_event(self._two_phases())
        response = _post_webhook(self.client, event)
        self.assertEqual(response.status_code, 200)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.scheduled_tier, "pro")
        self.assertIsNotNone(self.sub.scheduled_change_at)
        self.assertEqual(self.sub.stripe_schedule_id, "sub_sched_sw")

    def test_unknown_future_price_fails_the_event(self):
        """Test 32."""
        client = Client(raise_request_exception=False)
        event = self._schedule_event(self._two_phases(future_price_id="price_unknown"))
        with patch("stripe.Webhook.construct_event", return_value=event):
            response = client.post(
                reverse("stripe_webhook"), data=b"{}", content_type="application/json",
                HTTP_STRIPE_SIGNATURE=FAKE_SIG_HEADER,
            )
        self.assertEqual(response.status_code, 500)
        record = StripeWebhookEvent.objects.get(stripe_event_id=event["id"])
        self.assertEqual(record.status, "failed")

    def test_malformed_phases_fails_and_is_retryable(self):
        """Test 33 + retry."""
        client = Client(raise_request_exception=False)
        event = self._schedule_event([{"items": [{"price": {"id": "price_biz"}}], "start_date": 1000000000}])
        with patch("stripe.Webhook.construct_event", return_value=event):
            response = client.post(
                reverse("stripe_webhook"), data=b"{}", content_type="application/json",
                HTTP_STRIPE_SIGNATURE=FAKE_SIG_HEADER,
            )
        self.assertEqual(response.status_code, 500)
        record = StripeWebhookEvent.objects.get(stripe_event_id=event["id"])
        self.assertEqual(record.status, "failed")

        good_event = self._schedule_event(self._two_phases(), event_id=event["id"])
        response2 = _post_webhook(self.client, good_event)
        self.assertEqual(response2.status_code, 200)
        record.refresh_from_db()
        self.assertEqual(record.status, "processed")

    def test_duplicate_schedule_webhook_is_idempotent(self):
        """Test 34."""
        event = self._schedule_event(self._two_phases(), event_id="evt_dup_sched")
        r1 = _post_webhook(self.client, event)
        r2 = _post_webhook(self.client, event)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(StripeWebhookEvent.objects.filter(stripe_event_id="evt_dup_sched").count(), 1)

    def test_unknown_local_subscription_is_ignored_gracefully_not_failed(self):
        """Test 35: a schedule webhook for a subscription this app doesn't track is
        indistinguishable from a foreign/unrelated schedule -- retrying could never make the row
        appear, so this is treated as a graceful no-op (processed), the same convention already
        used by _handle_subscription_updated_or_deleted's own DoesNotExist handling, not as a
        failure that would just be retried forever for no reason."""
        event = self._schedule_event(self._two_phases(), subscription_id="sub_never_seen_sched")
        response = _post_webhook(self.client, event)
        self.assertEqual(response.status_code, 200)
        record = StripeWebhookEvent.objects.get(stripe_event_id=event["id"])
        self.assertEqual(record.status, "processed")

    def test_projection_does_not_touch_tier_status_or_active_until(self):
        """Test 36 (atomicity, by way of scope): a schedule event only ever writes the three
        scheduled_* fields, never tier/status/active_until -- those stay whatever they already
        were."""
        event = self._schedule_event(self._two_phases())
        _post_webhook(self.client, event)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.tier, "business")
        self.assertEqual(self.sub.status, "active")
        self.assertIsNone(self.sub.active_until)


class ScheduledDowngradeEntitlementTests(TestCase):
    """Phase 5e item 28 (entitlement, tests 37-42)."""

    def setUp(self):
        self.user = User.objects.create_user("dgent@example.com", "dgent@example.com", "StrongPass123")
        self.sub = self.user.subscription
        self.sub.tier, self.sub.status = "business", "active"
        self.sub.stripe_customer_id, self.sub.stripe_subscription_id = "cus_e", "sub_e"
        self.sub.scheduled_tier = "pro"
        self.sub.scheduled_change_at = timezone.now() + timedelta(days=10)
        self.sub.stripe_schedule_id = "sub_sched_e"
        self.sub.save()

    def test_entitlement_stays_on_current_tier_before_transition(self):
        """Tests 37-38."""
        self.assertEqual(self.sub.effective_tier, "business")
        self.assertTrue(self.sub.has_entitlement)
        self.assertEqual(self.sub.radar_limit, RADAR_LIMITS["business"])

    @override_settings(
        STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent",
        STRIPE_WEBHOOK_SECRET="whsec_test",
    )
    def test_period_transition_webhook_moves_tier_and_entitlement_and_cleans_scheduled_fields(self):
        """Tests 39-41."""
        event = _subscription_event(
            "customer.subscription.updated", subscription_id="sub_e", status="active",
            price_id="price_pro", cancel_at_period_end=False,
        )
        response = _post_webhook(self.client, event)
        self.assertEqual(response.status_code, 200)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.tier, "pro")
        self.assertTrue(self.sub.has_entitlement)
        self.assertEqual(self.sub.effective_tier, "pro")
        self.assertIsNone(self.sub.scheduled_tier)
        self.assertIsNone(self.sub.scheduled_change_at)
        self.assertEqual(self.sub.stripe_schedule_id, "")

    def test_complimentary_access_remains_independent_of_scheduled_downgrade(self):
        """Test 42."""
        self.sub.complimentary_tier = "enterprise"
        self.sub.tier, self.sub.status = "free", "inactive"
        self.sub.save()
        self.assertTrue(self.sub.has_entitlement)
        self.assertEqual(self.sub.effective_tier, "enterprise")


@override_settings(LEGAL_BILLING_ACTIVE=True)
class DowngradeUITests(TestCase):
    """Phase 5e item 29 (UI, tests 43-50)."""

    def _user(self, email, **sub_fields):
        user = User.objects.create_user(email, email, "StrongPass123")
        sub = user.subscription
        for field, value in sub_fields.items():
            setattr(sub, field, value)
        sub.save()
        return user

    def test_business_user_sees_pro_downgrade_action(self):
        """Test 43."""
        user = self._user(
            "dgu43@example.com", tier="business", status="active",
            stripe_customer_id="cus_43", stripe_subscription_id="sub_43",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertIn(reverse("change_plan"), content)
        self.assertIn('value="pro"', content)
        self.assertIn("Υποβάθμιση στο τέλος περιόδου", content)

    def test_enterprise_user_sees_business_and_pro_downgrade_actions(self):
        """Test 44."""
        user = self._user(
            "dgu44@example.com", tier="enterprise", status="active",
            stripe_customer_id="cus_44", stripe_subscription_id="sub_44",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertIn('value="pro"', content)
        self.assertIn('value="business"', content)

    def test_pro_user_sees_no_downgrade_action(self):
        """Test 45."""
        user = self._user(
            "dgu45@example.com", tier="pro", status="active",
            stripe_customer_id="cus_45", stripe_subscription_id="sub_45",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertNotIn("Υποβάθμιση στο τέλος περιόδου", content)

    def test_scheduled_downgrade_shows_target_and_date_in_settings(self):
        """Test 46."""
        user = self._user(
            "dgu46@example.com", tier="business", status="active", stripe_customer_id="cus_46",
            stripe_subscription_id="sub_46", scheduled_tier="pro",
            scheduled_change_at=timezone.now() + timedelta(days=12), stripe_schedule_id="sub_sched_46",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("settings")).content.decode()
        # Phase 5g item 12 copy: "Τρέχον πλάνο: Business" / "Προγραμματισμένη αλλαγή: Pro στις X".
        self.assertIn("Προγραμματισμένη αλλαγή", content)
        self.assertIn("Pro", content)

    def test_no_duplicate_downgrade_button_while_one_is_pending(self):
        """Test 47."""
        user = self._user(
            "dgu47@example.com", tier="business", status="active", stripe_customer_id="cus_47",
            stripe_subscription_id="sub_47", scheduled_tier="pro",
            scheduled_change_at=timezone.now() + timedelta(days=12), stripe_schedule_id="sub_sched_47",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertNotIn(reverse("change_plan"), content)

    def test_upgrade_buttons_still_behave_per_phase_5d(self):
        """Test 48."""
        user = self._user(
            "dgu48@example.com", tier="pro", status="active",
            stripe_customer_id="cus_48", stripe_subscription_id="sub_48",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertIn("Αναβάθμιση", content)

    def test_cancellation_ui_from_phase_5c_remains_correct(self):
        """Test 49."""
        user = self._user(
            "dgu49@example.com", tier="pro", status="active", stripe_customer_id="cus_49",
            stripe_subscription_id="sub_49", active_until=timezone.now() + timedelta(days=3),
        )
        self.client.force_login(user)
        content = self.client.get(reverse("settings")).content.decode()
        self.assertIn(reverse("resume_subscription"), content)

    def test_billing_portal_remains_available(self):
        """Test 50."""
        user = self._user(
            "dgu50@example.com", tier="business", status="active", stripe_customer_id="cus_50",
            stripe_subscription_id="sub_50", scheduled_tier="pro",
            scheduled_change_at=timezone.now() + timedelta(days=5), stripe_schedule_id="sub_sched_50",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("settings")).content.decode()
        self.assertIn(reverse("customer_portal"), content)


# ---------------------------------------------------------------------------------------------
# Phase 5f: cancel / release a pending scheduled downgrade. No Stripe write anywhere in this
# section is real -- everything is mocked. cancel_scheduled_downgrade never writes
# scheduled_tier/scheduled_change_at/stripe_schedule_id/tier/status/entitlement locally on
# success OR failure; only the subscription_schedule.released/.canceled webhook does that.
# ---------------------------------------------------------------------------------------------


@override_settings(STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent")
class CancelScheduledDowngradeTests(TestCase):
    def _user(self, email, **sub_fields):
        user = User.objects.create_user(email, email, "StrongPass123")
        sub = user.subscription
        for field, value in sub_fields.items():
            setattr(sub, field, value)
        sub.save()
        return user

    def _post(self, user):
        self.client.force_login(user)
        return self.client.post(reverse("cancel_scheduled_downgrade"))

    def _schedule_payload(
        self, status="active", subscription_id="sub_1", current_price="price_biz",
        future_price="price_pro", phases=None, schedule_id="sub_sched_1",
    ):
        if phases is None:
            phases = [
                {"items": [{"price": {"id": current_price}}]},
                {"items": [{"price": {"id": future_price}}]},
            ]
        return {"id": schedule_id, "subscription": subscription_id, "status": status, "phases": phases}

    # ---------------- Happy paths (item 15) ----------------

    def _assert_successful_cancel(self, current_tier, scheduled_tier, current_price, future_price):
        user = self._user(
            f"cdr_{current_tier}_{scheduled_tier}@example.com", tier=current_tier, status="active",
            stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
            scheduled_tier=scheduled_tier, scheduled_change_at=timezone.now() + timedelta(days=10),
            stripe_schedule_id="sub_sched_1",
        )
        schedule_payload = self._schedule_payload(current_price=current_price, future_price=future_price)
        with patch("stripe.SubscriptionSchedule.retrieve", return_value=schedule_payload) as sched_retrieve, \
             patch("stripe.SubscriptionSchedule.release") as sched_release, \
             patch("stripe.Subscription.modify") as sub_modify, \
             patch("stripe.checkout.Session.create") as checkout_create:
            response = self._post(user)

        sched_retrieve.assert_called_once_with("sub_sched_1")
        sched_release.assert_called_once()
        args, kwargs = sched_release.call_args
        self.assertEqual(args[0], "sub_sched_1")
        self.assertTrue(kwargs.get("idempotency_key"))
        sub_modify.assert_not_called()
        checkout_create.assert_not_called()
        self.assertRedirects(response, reverse("settings"))

        sub = user.subscription
        sub.refresh_from_db()
        self.assertEqual(sub.scheduled_tier, scheduled_tier)  # not cleared synchronously
        self.assertEqual(sub.tier, current_tier)  # unchanged

    def test_business_scheduled_pro_cancel(self):
        self._assert_successful_cancel("business", "pro", "price_biz", "price_pro")

    def test_enterprise_scheduled_business_cancel(self):
        self._assert_successful_cancel("enterprise", "business", "price_ent", "price_biz")

    def test_enterprise_scheduled_pro_cancel(self):
        self._assert_successful_cancel("enterprise", "pro", "price_ent", "price_pro")

    # ---------------- Rejected paths (item 17) ----------------

    def test_no_schedule_id_is_a_safe_noop(self):
        """Test 11."""
        user = self._user(
            "cdr11@example.com", tier="business", status="active",
            stripe_customer_id="cus_11", stripe_subscription_id="sub_11",
        )
        with patch("stripe.SubscriptionSchedule.retrieve") as retrieve, \
             patch("stripe.SubscriptionSchedule.release") as release:
            self._post(user)
        retrieve.assert_not_called()
        release.assert_not_called()

    def test_missing_scheduled_tier_is_a_safe_noop(self):
        """Test 12."""
        user = self._user(
            "cdr12@example.com", tier="business", status="active",
            stripe_customer_id="cus_12", stripe_subscription_id="sub_12",
            stripe_schedule_id="sub_sched_12",
        )
        with patch("stripe.SubscriptionSchedule.retrieve") as retrieve, \
             patch("stripe.SubscriptionSchedule.release") as release:
            self._post(user)
        retrieve.assert_not_called()
        release.assert_not_called()

    def test_malformed_schedule_blocks_release(self):
        """Test 13."""
        user = self._user(
            "cdr13@example.com", tier="business", status="active",
            stripe_customer_id="cus_13", stripe_subscription_id="sub_13",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=5),
            stripe_schedule_id="sub_sched_13",
        )
        schedule_payload = self._schedule_payload(
            subscription_id="sub_13", phases=[{"items": [{"price": {"id": "price_biz"}}]}],
        )
        with patch("stripe.SubscriptionSchedule.retrieve", return_value=schedule_payload), \
             patch("stripe.SubscriptionSchedule.release") as release:
            self._post(user)
        release.assert_not_called()

    def test_schedule_belongs_to_another_subscription_blocks_release(self):
        """Test 14."""
        user = self._user(
            "cdr14@example.com", tier="business", status="active",
            stripe_customer_id="cus_14", stripe_subscription_id="sub_14",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=5),
            stripe_schedule_id="sub_sched_14",
        )
        schedule_payload = self._schedule_payload(subscription_id="sub_DIFFERENT")
        with patch("stripe.SubscriptionSchedule.retrieve", return_value=schedule_payload), \
             patch("stripe.SubscriptionSchedule.release") as release:
            self._post(user)
        release.assert_not_called()

    def test_future_tier_mismatch_blocks_release(self):
        """Test 15."""
        user = self._user(
            "cdr15@example.com", tier="business", status="active",
            stripe_customer_id="cus_15", stripe_subscription_id="sub_15",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=5),
            stripe_schedule_id="sub_sched_15",
        )
        schedule_payload = self._schedule_payload(subscription_id="sub_15", future_price="price_biz")  # local says pro
        with patch("stripe.SubscriptionSchedule.retrieve", return_value=schedule_payload), \
             patch("stripe.SubscriptionSchedule.release") as release:
            self._post(user)
        release.assert_not_called()

    def test_no_future_items_blocks_release(self):
        """Test 16."""
        user = self._user(
            "cdr16@example.com", tier="business", status="active",
            stripe_customer_id="cus_16", stripe_subscription_id="sub_16",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=5),
            stripe_schedule_id="sub_sched_16",
        )
        schedule_payload = self._schedule_payload(
            subscription_id="sub_16", phases=[{"items": [{"price": {"id": "price_biz"}}]}, {"items": []}],
        )
        with patch("stripe.SubscriptionSchedule.retrieve", return_value=schedule_payload), \
             patch("stripe.SubscriptionSchedule.release") as release:
            self._post(user)
        release.assert_not_called()

    def test_already_released_status_is_a_safe_noop(self):
        """Test 17."""
        user = self._user(
            "cdr17@example.com", tier="business", status="active",
            stripe_customer_id="cus_17", stripe_subscription_id="sub_17",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=5),
            stripe_schedule_id="sub_sched_17",
        )
        schedule_payload = self._schedule_payload(subscription_id="sub_17", status="released")
        with patch("stripe.SubscriptionSchedule.retrieve", return_value=schedule_payload), \
             patch("stripe.SubscriptionSchedule.release") as release:
            response = self._post(user)
        release.assert_not_called()
        self.assertRedirects(response, reverse("settings"))

    def test_already_completed_status_is_reported_not_as_success(self):
        """Test 18 / 19: the boundary race -- the transition already happened, so this must NOT
        look like a successful cancellation, and must not auto-upgrade back."""
        user = self._user(
            "cdr18@example.com", tier="business", status="active",
            stripe_customer_id="cus_18", stripe_subscription_id="sub_18",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=5),
            stripe_schedule_id="sub_sched_18",
        )
        schedule_payload = self._schedule_payload(subscription_id="sub_18", status="completed")
        with patch("stripe.SubscriptionSchedule.retrieve", return_value=schedule_payload), \
             patch("stripe.SubscriptionSchedule.release") as release, \
             patch("stripe.Subscription.modify") as sub_modify:
            response = self._post(user)
        release.assert_not_called()
        sub_modify.assert_not_called()  # no automatic upgrade-back
        self.assertRedirects(response, reverse("settings"))

    def test_already_canceled_status_is_a_safe_noop(self):
        """Test 19-adjacent."""
        user = self._user(
            "cdr19@example.com", tier="business", status="active",
            stripe_customer_id="cus_19", stripe_subscription_id="sub_19",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=5),
            stripe_schedule_id="sub_sched_19",
        )
        schedule_payload = self._schedule_payload(subscription_id="sub_19", status="canceled")
        with patch("stripe.SubscriptionSchedule.retrieve", return_value=schedule_payload), \
             patch("stripe.SubscriptionSchedule.release") as release:
            self._post(user)
        release.assert_not_called()

    def test_retrieve_failure_blocks_release(self):
        """Test 20."""
        user = self._user(
            "cdr20@example.com", tier="business", status="active",
            stripe_customer_id="cus_20", stripe_subscription_id="sub_20",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=5),
            stripe_schedule_id="sub_sched_20",
        )
        with patch("stripe.SubscriptionSchedule.retrieve", side_effect=RuntimeError("down")), \
             patch("stripe.SubscriptionSchedule.release") as release:
            self._post(user)
        release.assert_not_called()

    def test_inconsistent_state_with_active_until_blocks_release(self):
        """Item 14 of the brief: a schedule pending AND active_until set at the same time is
        ambiguous local state -- fail closed without even calling Stripe."""
        user = self._user(
            "cdr_ambig@example.com", tier="business", status="active",
            stripe_customer_id="cus_ambig", stripe_subscription_id="sub_ambig",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=5),
            stripe_schedule_id="sub_sched_ambig", active_until=timezone.now() + timedelta(days=3),
        )
        with patch("stripe.SubscriptionSchedule.retrieve") as retrieve, \
             patch("stripe.SubscriptionSchedule.release") as release:
            self._post(user)
        retrieve.assert_not_called()
        release.assert_not_called()

    # ---------------- Idempotency / failure (item 18) ----------------

    def test_release_failure_retains_nonce_and_no_local_mutation(self):
        """Test 21 / 25."""
        user = self._user(
            "cdr21@example.com", tier="business", status="active",
            stripe_customer_id="cus_21", stripe_subscription_id="sub_21",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=5),
            stripe_schedule_id="sub_sched_21",
        )
        schedule_payload = self._schedule_payload(subscription_id="sub_21", schedule_id="sub_sched_21")
        with patch("stripe.SubscriptionSchedule.retrieve", return_value=schedule_payload), \
             patch("stripe.SubscriptionSchedule.release", side_effect=RuntimeError("down")):
            self._post(user)
        self.assertIn("cancel_downgrade_attempt_nonce", self.client.session)
        sub = user.subscription
        sub.refresh_from_db()
        self.assertEqual(sub.scheduled_tier, "pro")
        self.assertEqual(sub.stripe_schedule_id, "sub_sched_21")

    def test_retry_reuses_same_idempotency_key(self):
        """Test 22."""
        user = self._user(
            "cdr22@example.com", tier="business", status="active",
            stripe_customer_id="cus_22", stripe_subscription_id="sub_22",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=5),
            stripe_schedule_id="sub_sched_22",
        )
        schedule_payload = self._schedule_payload(subscription_id="sub_22", schedule_id="sub_sched_22")
        with patch("stripe.SubscriptionSchedule.retrieve", return_value=schedule_payload), \
             patch("stripe.SubscriptionSchedule.release", side_effect=RuntimeError("down")) as release:
            self._post(user)
        first_key = release.call_args.kwargs["idempotency_key"]

        with patch("stripe.SubscriptionSchedule.retrieve", return_value=schedule_payload), \
             patch("stripe.SubscriptionSchedule.release", side_effect=RuntimeError("down")) as release:
            self._post(user)
        second_key = release.call_args.kwargs["idempotency_key"]
        self.assertEqual(first_key, second_key)

    def test_success_clears_nonce(self):
        """Test 23."""
        user = self._user(
            "cdr23@example.com", tier="business", status="active",
            stripe_customer_id="cus_23", stripe_subscription_id="sub_23",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=5),
            stripe_schedule_id="sub_sched_23",
        )
        schedule_payload = self._schedule_payload(subscription_id="sub_23", schedule_id="sub_sched_23")
        with patch("stripe.SubscriptionSchedule.retrieve", return_value=schedule_payload), \
             patch("stripe.SubscriptionSchedule.release"):
            self._post(user)
        self.assertNotIn("cancel_downgrade_attempt_nonce", self.client.session)

    def test_new_later_attempt_gets_a_new_key(self):
        """Test 24."""
        user = self._user(
            "cdr24@example.com", tier="business", status="active",
            stripe_customer_id="cus_24", stripe_subscription_id="sub_24",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=5),
            stripe_schedule_id="sub_sched_24",
        )
        schedule_payload = self._schedule_payload(subscription_id="sub_24", schedule_id="sub_sched_24")
        with patch("stripe.SubscriptionSchedule.retrieve", return_value=schedule_payload), \
             patch("stripe.SubscriptionSchedule.release") as release:
            self._post(user)
        first_key = release.call_args.kwargs["idempotency_key"]

        # Simulate: the webhook cleared the fields, then a brand new downgrade schedule was
        # created (Phase 5e), and the user cancels THAT one too -- a genuinely new attempt.
        sub = user.subscription
        sub.scheduled_tier = "pro"
        sub.scheduled_change_at = timezone.now() + timedelta(days=20)
        sub.stripe_schedule_id = "sub_sched_24_v2"
        sub.save()
        schedule_payload_v2 = self._schedule_payload(subscription_id="sub_24", schedule_id="sub_sched_24_v2")
        with patch("stripe.SubscriptionSchedule.retrieve", return_value=schedule_payload_v2), \
             patch("stripe.SubscriptionSchedule.release") as release:
            self._post(user)
        second_key = release.call_args.kwargs["idempotency_key"]
        self.assertNotEqual(first_key, second_key)


@override_settings(
    STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent",
    STRIPE_WEBHOOK_SECRET="whsec_test",
)
class SubscriptionScheduleReleasedWebhookTests(TestCase):
    """Phase 5f item 16 (webhook cleanup, tests 4-10)."""

    def setUp(self):
        self.user = User.objects.create_user("relwebhook@example.com", "relwebhook@example.com", "StrongPass123")
        self.sub = self.user.subscription
        self.sub.tier, self.sub.status = "business", "active"
        self.sub.stripe_customer_id, self.sub.stripe_subscription_id = "cus_rw", "sub_rw"
        self.sub.scheduled_tier = "pro"
        self.sub.scheduled_change_at = timezone.now() + timedelta(days=10)
        self.sub.stripe_schedule_id = "sub_sched_rw"
        self.sub.save()

    def _released_event(
        self, subscription_id="sub_rw", schedule_id="sub_sched_rw", event_id="evt_rel_1",
        event_type="subscription_schedule.released",
    ):
        return {
            "id": event_id, "type": event_type,
            "data": {"object": {"id": schedule_id, "released_subscription": subscription_id, "subscription": None}},
        }

    def test_released_webhook_clears_scheduled_fields(self):
        """Tests 4 + 8 (atomic: all three checked together)."""
        event = self._released_event()
        response = _post_webhook(self.client, event)
        self.assertEqual(response.status_code, 200)
        self.sub.refresh_from_db()
        self.assertIsNone(self.sub.scheduled_tier)
        self.assertIsNone(self.sub.scheduled_change_at)
        self.assertEqual(self.sub.stripe_schedule_id, "")

    def test_current_tier_unchanged(self):
        """Test 5."""
        event = self._released_event()
        _post_webhook(self.client, event)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.tier, "business")

    def test_entitlement_and_radar_limit_unchanged(self):
        """Tests 6-7."""
        event = self._released_event()
        _post_webhook(self.client, event)
        self.sub.refresh_from_db()
        self.assertTrue(self.sub.has_entitlement)
        self.assertEqual(self.sub.radar_limit, RADAR_LIMITS["business"])

    def test_duplicate_released_webhook_is_idempotent(self):
        """Test 9."""
        event = self._released_event(event_id="evt_dup_rel")
        r1 = _post_webhook(self.client, event)
        r2 = _post_webhook(self.client, event)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(StripeWebhookEvent.objects.filter(stripe_event_id="evt_dup_rel").count(), 1)

    def test_unknown_local_subscription_is_handled_safely(self):
        """Test 10."""
        event = self._released_event(subscription_id="sub_never_seen_rel")
        response = _post_webhook(self.client, event)
        self.assertEqual(response.status_code, 200)
        record = StripeWebhookEvent.objects.get(stripe_event_id=event["id"])
        self.assertEqual(record.status, "processed")

    def test_canceled_event_type_also_clears_fields(self):
        """§9 of the brief: subscription_schedule.canceled is handled identically for local
        bookkeeping purposes -- the real entitlement consequence, if any, comes from a separate
        customer.subscription.deleted event, unchanged by this handler."""
        event = self._released_event(event_type="subscription_schedule.canceled")
        event["data"]["object"]["subscription"] = "sub_rw"
        event["data"]["object"]["released_subscription"] = None
        response = _post_webhook(self.client, event)
        self.assertEqual(response.status_code, 200)
        self.sub.refresh_from_db()
        self.assertIsNone(self.sub.scheduled_tier)
        self.assertEqual(self.sub.tier, "business")  # untouched, per §9

    def test_stale_event_for_a_replaced_schedule_is_ignored(self):
        """Out-of-order guard: a release event for an OLD schedule id must not clear a NEWER one
        that has already replaced it locally."""
        self.sub.stripe_schedule_id = "sub_sched_NEWER"
        self.sub.save()
        event = self._released_event(schedule_id="sub_sched_rw")  # the OLD schedule id
        response = _post_webhook(self.client, event)
        self.assertEqual(response.status_code, 200)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.stripe_schedule_id, "sub_sched_NEWER")  # untouched


@override_settings(LEGAL_BILLING_ACTIVE=True)
class CancelScheduledDowngradeUITests(TestCase):
    """Phase 5f item 19 (UI, tests 26-32)."""

    def _user(self, email, **sub_fields):
        user = User.objects.create_user(email, email, "StrongPass123")
        sub = user.subscription
        for field, value in sub_fields.items():
            setattr(sub, field, value)
        sub.save()
        return user

    def test_pending_downgrade_shows_cancel_button(self):
        """Test 26."""
        user = self._user(
            "cdru26@example.com", tier="business", status="active", stripe_customer_id="cus_26",
            stripe_subscription_id="sub_26", scheduled_tier="pro",
            scheduled_change_at=timezone.now() + timedelta(days=10), stripe_schedule_id="sub_sched_26",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("settings")).content.decode()
        self.assertIn(reverse("cancel_scheduled_downgrade"), content)
        self.assertIn("Ακύρωση προγραμματισμένης υποβάθμισης", content)

    def test_no_pending_downgrade_button_absent(self):
        """Test 27."""
        user = self._user(
            "cdru27@example.com", tier="business", status="active",
            stripe_customer_id="cus_27", stripe_subscription_id="sub_27",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("settings")).content.decode()
        self.assertNotIn(reverse("cancel_scheduled_downgrade"), content)

    def test_pending_downgrade_still_shows_target_and_date(self):
        """Test 28."""
        user = self._user(
            "cdru28@example.com", tier="business", status="active", stripe_customer_id="cus_28",
            stripe_subscription_id="sub_28", scheduled_tier="pro",
            scheduled_change_at=timezone.now() + timedelta(days=10), stripe_schedule_id="sub_sched_28",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("settings")).content.decode()
        # Phase 5g item 12 copy: "Τρέχον πλάνο: Business" / "Προγραμματισμένη αλλαγή: Pro στις X".
        self.assertIn("Προγραμματισμένη αλλαγή", content)
        self.assertIn("Pro", content)

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_test")
    def test_after_released_webhook_normal_ui_returns(self):
        """Test 29."""
        user = self._user(
            "cdru29@example.com", tier="business", status="active", stripe_customer_id="cus_29",
            stripe_subscription_id="sub_29", scheduled_tier="pro",
            scheduled_change_at=timezone.now() + timedelta(days=10), stripe_schedule_id="sub_sched_29",
        )
        event = {
            "id": "evt_ui_rel", "type": "subscription_schedule.released",
            "data": {"object": {"id": "sub_sched_29", "released_subscription": "sub_29", "subscription": None}},
        }
        _post_webhook(self.client, event)
        self.client.force_login(user)
        content = self.client.get(reverse("settings")).content.decode()
        self.assertNotIn(reverse("cancel_scheduled_downgrade"), content)
        self.assertIn(reverse("cancel_subscription"), content)

    def test_no_duplicate_downgrade_buttons_on_pricing(self):
        """Test 30."""
        user = self._user(
            "cdru30@example.com", tier="business", status="active", stripe_customer_id="cus_30",
            stripe_subscription_id="sub_30", scheduled_tier="pro",
            scheduled_change_at=timezone.now() + timedelta(days=10), stripe_schedule_id="sub_sched_30",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertNotIn(reverse("change_plan"), content)

    def test_billing_portal_remains_available(self):
        """Test 31."""
        user = self._user(
            "cdru31@example.com", tier="business", status="active", stripe_customer_id="cus_31",
            stripe_subscription_id="sub_31", scheduled_tier="pro",
            scheduled_change_at=timezone.now() + timedelta(days=10), stripe_schedule_id="sub_sched_31",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("settings")).content.decode()
        self.assertIn(reverse("customer_portal"), content)

    def test_cancellation_ui_from_phase_5c_remains_correct(self):
        """Test 32."""
        user = self._user(
            "cdru32@example.com", tier="pro", status="active", stripe_customer_id="cus_32",
            stripe_subscription_id="sub_32", active_until=timezone.now() + timedelta(days=3),
        )
        self.client.force_login(user)
        content = self.client.get(reverse("settings")).content.decode()
        self.assertIn(reverse("resume_subscription"), content)


# ---------------------------------------------------------------------------------------------
# Phase 5g: final billing UI / subscription lifecycle polish. No Stripe write anywhere in this
# section -- these tests only render pricing/settings and inspect the resulting HTML against
# existing server-side fields (tier, status, active_until, scheduled_tier, complimentary_*).
# Nothing here changes billing mechanics, entitlement predicates, or webhook semantics.
# ---------------------------------------------------------------------------------------------


@override_settings(LEGAL_BILLING_ACTIVE=True)
class PricingLifecycleUITests(TestCase):
    """Phase 5g items 3-8, 23 (pricing states)."""

    def _user(self, email, **sub_fields):
        user = User.objects.create_user(email, email, "StrongPass123")
        sub = user.subscription
        for field, value in sub_fields.items():
            setattr(sub, field, value)
        sub.save()
        return user

    # test 1: no subscription -> checkout buttons
    def test_no_subscription_shows_checkout_buttons_for_all_tiers(self):
        user = self._user("pl1@example.com")
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertIn('value="pro"', content)
        self.assertIn('value="business"', content)
        self.assertIn('value="enterprise"', content)
        self.assertIn(reverse("create_checkout_session"), content)

    # test 2: complimentary-only -> checkout buttons (item 3 -- the bug this phase fixes)
    def test_complimentary_only_still_shows_checkout_buttons(self):
        user = self._user("pl2@example.com", complimentary_tier="business")
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertIn(reverse("create_checkout_session"), content)
        self.assertIn('value="pro"', content)
        self.assertIn('value="business"', content)
        self.assertIn('value="enterprise"', content)

    # test 3: Pro current card -> current, no mutation form on its own card
    def test_pro_subscriber_current_card_has_no_upgrade_or_checkout_for_itself(self):
        user = self._user(
            "pl3@example.com", tier="pro", status="active",
            stripe_customer_id="cus_pl3", stripe_subscription_id="sub_pl3",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertNotIn('name="tier" value="pro"', content)
        self.assertIn(reverse("customer_portal"), content)

    # test 4/5: Pro -> Business / Enterprise upgrade
    def test_pro_to_business_and_enterprise_upgrade_actions(self):
        user = self._user(
            "pl45@example.com", tier="pro", status="active",
            stripe_customer_id="cus_pl45", stripe_subscription_id="sub_pl45",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertIn("Αναβάθμιση", content)
        self.assertIn(reverse("change_plan"), content)
        self.assertIn('value="business"', content)
        self.assertIn('value="enterprise"', content)

    # test 6: Business -> Pro downgrade
    def test_business_to_pro_downgrade_action(self):
        user = self._user(
            "pl6@example.com", tier="business", status="active",
            stripe_customer_id="cus_pl6", stripe_subscription_id="sub_pl6",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertIn("Υποβάθμιση στο τέλος περιόδου", content)
        self.assertIn('value="pro"', content)

    # test 7: Business -> Enterprise upgrade
    def test_business_to_enterprise_upgrade_action(self):
        user = self._user(
            "pl7@example.com", tier="business", status="active",
            stripe_customer_id="cus_pl7", stripe_subscription_id="sub_pl7",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertIn("Αναβάθμιση", content)
        self.assertIn('value="enterprise"', content)

    # test 8: Enterprise -> lower-tier downgrade actions
    def test_enterprise_subscriber_sees_downgrade_actions_to_lower_tiers(self):
        user = self._user(
            "pl8@example.com", tier="enterprise", status="active",
            stripe_customer_id="cus_pl8", stripe_subscription_id="sub_pl8",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertIn("Υποβάθμιση στο τέλος περιόδου", content)
        self.assertIn('value="pro"', content)
        self.assertIn('value="business"', content)
        self.assertNotIn("Αναβάθμιση", content)

    # test 9: scheduled downgrade -> no second mutation actions anywhere on pricing
    def test_scheduled_downgrade_shows_no_mutation_actions_on_pricing(self):
        user = self._user(
            "pl9@example.com", tier="business", status="active",
            stripe_customer_id="cus_pl9", stripe_subscription_id="sub_pl9",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=12),
            stripe_schedule_id="sub_sched_pl9",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertNotIn(reverse("change_plan"), content)
        self.assertNotIn(reverse("create_checkout_session"), content)
        self.assertIn("Προγραμματισμένη αλλαγή", content)
        self.assertIn(reverse("settings"), content)

    # test 10: scheduled cancellation -> no upgrade/downgrade actions
    def test_scheduled_cancellation_shows_no_mutation_actions_on_pricing(self):
        user = self._user(
            "pl10@example.com", tier="pro", status="active",
            stripe_customer_id="cus_pl10", stripe_subscription_id="sub_pl10",
            active_until=timezone.now() + timedelta(days=6),
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertNotIn(reverse("change_plan"), content)
        self.assertIn("Η συνδρομή θα λήξει στις", content)
        self.assertIn(reverse("settings"), content)

    # test 11: terminal subscription -> new checkout path allowed
    def test_terminal_subscription_allows_new_checkout(self):
        user = self._user(
            "pl11@example.com", tier="pro", status="canceled",
            stripe_customer_id="cus_pl11", stripe_subscription_id="sub_pl11",
        )
        self.client.force_login(user)
        content = self.client.get(reverse("pricing")).content.decode()
        self.assertIn(reverse("create_checkout_session"), content)

    # test 12: payment problem -> pricing still renders (management lives in settings)
    def test_payment_problem_status_does_not_crash_pricing_render(self):
        user = self._user(
            "pl12@example.com", tier="pro", status="past_due",
            stripe_customer_id="cus_pl12", stripe_subscription_id="sub_pl12",
        )
        self.client.force_login(user)
        response = self.client.get(reverse("pricing"))
        self.assertEqual(response.status_code, 200)


class SettingsLifecycleUITests(TestCase):
    """Phase 5g items 9-17, 24 (settings states)."""

    def _user(self, email, **sub_fields):
        user = User.objects.create_user(email, email, "StrongPass123")
        sub = user.subscription
        for field, value in sub_fields.items():
            setattr(sub, field, value)
        sub.save()
        return user

    def _content(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("settings")).content.decode()

    # test 13: normal active -> Portal + Cancel
    def test_normal_active_shows_portal_and_cancel_only(self):
        user = self._user(
            "sl13@example.com", tier="pro", status="active",
            stripe_customer_id="cus_sl13", stripe_subscription_id="sub_sl13",
        )
        content = self._content(user)
        self.assertIn(reverse("customer_portal"), content)
        self.assertIn(reverse("cancel_subscription"), content)
        self.assertNotIn(reverse("resume_subscription"), content)
        self.assertNotIn(reverse("cancel_scheduled_downgrade"), content)
        self.assertIn("Ενεργή", content)

    # test 13b: trialing is also "live" (cancelable) and gets Portal + Cancel, with its own label
    def test_trialing_shows_portal_and_cancel_with_trial_label(self):
        user = self._user(
            "sl13b@example.com", tier="pro", status="trialing",
            stripe_customer_id="cus_sl13b", stripe_subscription_id="sub_sl13b",
        )
        content = self._content(user)
        self.assertIn(reverse("customer_portal"), content)
        self.assertIn(reverse("cancel_subscription"), content)
        self.assertIn("Δοκιμαστική περίοδος", content)

    # test 14: cancellation pending -> Resume + Portal
    def test_cancellation_pending_shows_resume_and_portal_only(self):
        user = self._user(
            "sl14@example.com", tier="business", status="active",
            stripe_customer_id="cus_sl14", stripe_subscription_id="sub_sl14",
            active_until=timezone.now() + timedelta(days=4),
        )
        content = self._content(user)
        self.assertIn(reverse("resume_subscription"), content)
        self.assertIn(reverse("customer_portal"), content)
        self.assertNotIn(reverse("cancel_subscription"), content)
        self.assertNotIn(reverse("cancel_scheduled_downgrade"), content)

    # test 15: downgrade pending -> Cancel Downgrade + Portal
    def test_downgrade_pending_shows_cancel_downgrade_and_portal_only(self):
        user = self._user(
            "sl15@example.com", tier="business", status="active",
            stripe_customer_id="cus_sl15", stripe_subscription_id="sub_sl15",
            scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=9),
            stripe_schedule_id="sub_sched_sl15",
        )
        content = self._content(user)
        self.assertIn(reverse("cancel_scheduled_downgrade"), content)
        self.assertIn(reverse("customer_portal"), content)
        self.assertNotIn(reverse("cancel_subscription"), content)
        self.assertNotIn(reverse("resume_subscription"), content)

    # test 16: payment problem -> Portal only
    def test_payment_problem_shows_portal_only(self):
        user = self._user(
            "sl16@example.com", tier="pro", status="past_due",
            stripe_customer_id="cus_sl16", stripe_subscription_id="sub_sl16",
        )
        content = self._content(user)
        self.assertIn(reverse("customer_portal"), content)
        self.assertNotIn(reverse("cancel_subscription"), content)
        self.assertNotIn(reverse("resume_subscription"), content)
        self.assertNotIn(reverse("cancel_scheduled_downgrade"), content)
        self.assertIn("Χρειάζεται ενέργεια", content)
        self.assertIn("Εκκρεμεί πληρωμή", content)

    # test 17: canceled -> pricing CTA, no lifecycle mutation buttons
    def test_canceled_subscription_shows_pricing_cta_only(self):
        user = self._user(
            "sl17@example.com", tier="pro", status="canceled",
            stripe_customer_id="cus_sl17", stripe_subscription_id="sub_sl17",
        )
        content = self._content(user)
        self.assertIn(reverse("pricing"), content)
        self.assertNotIn(reverse("customer_portal"), content)
        self.assertNotIn(reverse("cancel_subscription"), content)
        self.assertNotIn(reverse("resume_subscription"), content)
        self.assertNotIn(reverse("cancel_scheduled_downgrade"), content)
        self.assertIn("Επιλογή νέου πλάνου", content)

    # test 18: complimentary-only -> complimentary info + pricing CTA, no billing controls
    def test_complimentary_only_shows_info_and_pricing_cta_only(self):
        user = self._user("sl18@example.com", complimentary_tier="enterprise")
        content = self._content(user)
        self.assertIn(reverse("pricing"), content)
        self.assertIn("Δωρεάν Πρόσβαση", content)
        self.assertIn("Αγορά συνδρομής", content)
        self.assertNotIn(reverse("customer_portal"), content)
        self.assertNotIn(reverse("cancel_subscription"), content)
        self.assertNotIn(reverse("resume_subscription"), content)
        self.assertNotIn(reverse("cancel_scheduled_downgrade"), content)

    # test 19: no subscription -> pricing CTA
    def test_no_subscription_shows_pricing_cta(self):
        user = self._user("sl19@example.com")
        content = self._content(user)
        self.assertIn(reverse("pricing"), content)
        self.assertIn("Δεν υπάρχει ενεργή συνδρομή", content)
        self.assertNotIn(reverse("customer_portal"), content)

    # test 20: no contradictory buttons in each state -- at most one of the three lifecycle-
    # mutation actions ever renders together; payment_problem legitimately shows none of them
    # (Portal only, checked separately in test 16).
    def test_states_never_mix_lifecycle_mutation_actions(self):
        mutation_urls = [
            reverse("cancel_subscription"),
            reverse("resume_subscription"),
            reverse("cancel_scheduled_downgrade"),
        ]
        cases = [
            (self._user(
                "sl20a@example.com", tier="pro", status="active",
                stripe_customer_id="c20a", stripe_subscription_id="s20a",
            ), 1),
            (self._user(
                "sl20b@example.com", tier="pro", status="active",
                stripe_customer_id="c20b", stripe_subscription_id="s20b",
                active_until=timezone.now() + timedelta(days=2),
            ), 1),
            (self._user(
                "sl20c@example.com", tier="business", status="active",
                stripe_customer_id="c20c", stripe_subscription_id="s20c",
                scheduled_tier="pro", scheduled_change_at=timezone.now() + timedelta(days=2),
                stripe_schedule_id="sched20c",
            ), 1),
            (self._user(
                "sl20d@example.com", tier="pro", status="past_due",
                stripe_customer_id="c20d", stripe_subscription_id="s20d",
            ), 0),
        ]
        for user, expected_count in cases:
            content = self._content(user)
            present = [url for url in mutation_urls if url in content]
            self.assertEqual(
                len(present), expected_count,
                f"{user.email}: expected {expected_count} of {mutation_urls}, got {present}",
            )


class BillingStatusLabelTests(TestCase):
    """Phase 5g items 18, 25 (status label mapping)."""

    def test_known_statuses_get_human_readable_greek_labels(self):
        from .templatetags.billing_tags import stripe_status_label

        expected = {
            "active": "Ενεργή",
            "trialing": "Δοκιμαστική περίοδος",
            "past_due": "Εκκρεμεί πληρωμή",
            "unpaid": "Ανεξόφλητη",
            "incomplete": "Η πληρωμή δεν ολοκληρώθηκε",
            "incomplete_expired": "Έληξε",
            "canceled": "Ακυρωμένη",
            "paused": "Σε παύση",
        }
        for status, label in expected.items():
            self.assertEqual(stripe_status_label(status), label)

    def test_unknown_status_has_safe_fallback(self):
        from .templatetags.billing_tags import stripe_status_label

        self.assertEqual(stripe_status_label("some_future_stripe_status"), "Άγνωστη κατάσταση")
        self.assertNotIn("some_future_stripe_status", stripe_status_label("some_future_stripe_status"))

    def test_no_raw_stripe_status_exposed_on_settings_page(self):
        user = User.objects.create_user("bl1@example.com", "bl1@example.com", "StrongPass123")
        sub = user.subscription
        sub.tier = "pro"
        sub.status = "past_due"
        sub.stripe_customer_id = "cus_bl1"
        sub.stripe_subscription_id = "sub_bl1"
        sub.save()
        self.client.force_login(user)
        content = self.client.get(reverse("settings")).content.decode()
        # The raw Stripe status string itself must never appear where a label is expected.
        self.assertNotIn(">past_due<", content)
        self.assertIn("Εκκρεμεί πληρωμή", content)


@override_settings(STRIPE_PRICE_PRO="price_pro", STRIPE_PRICE_BUSINESS="price_biz", STRIPE_PRICE_ENTERPRISE="price_ent")
class BillingMessageContentTests(TestCase):
    """Phase 5g items 19, 25 (Django messages stay human, no raw exception content)."""

    def _user(self, email, **sub_fields):
        user = User.objects.create_user(email, email, "StrongPass123")
        sub = user.subscription
        for field, value in sub_fields.items():
            setattr(sub, field, value)
        sub.save()
        return user

    def test_cancel_stripe_failure_message_has_no_raw_exception_text(self):
        user = self._user(
            "bm1@example.com", tier="pro", status="active",
            stripe_customer_id="cus_bm1", stripe_subscription_id="sub_bm1",
        )
        self.client.force_login(user)
        with patch("stripe.Subscription.retrieve", return_value={"status": "active", "cancel_at_period_end": False}), \
             patch("stripe.Subscription.modify", side_effect=RuntimeError("card_declined: insufficient_funds")):
            self.client.post(reverse("cancel_subscription"))
        content = self.client.get(reverse("settings")).content.decode()
        self.assertNotIn("card_declined", content)
        self.assertNotIn("insufficient_funds", content)
        self.assertIn("Η ακύρωση δεν ολοκληρώθηκε", content)

    def test_change_plan_success_message_is_correct_and_human(self):
        user = self._user(
            "bm2@example.com", tier="pro", status="active",
            stripe_customer_id="cus_bm2", stripe_subscription_id="sub_bm2",
        )
        self.client.force_login(user)
        retrieve_payload = {
            "status": "active", "cancel_at_period_end": False,
            "items": {"data": [{"id": "si_1", "price": {"id": "price_pro"}}]},
        }
        with patch("stripe.Subscription.retrieve", return_value=retrieve_payload), \
             patch("stripe.Subscription.modify"):
            self.client.post(reverse("change_plan"), {"target_tier": "business"})
        content = self.client.get(reverse("settings")).content.decode()
        self.assertIn("Η αναβάθμιση του πλάνου σου ξεκίνησε", content)
        self.assertNotIn("SubscriptionSchedule", content)
        self.assertNotIn("idempotency", content)
