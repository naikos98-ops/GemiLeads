"""Tests for the GEMI company phone in the Company Dossier (gemiapp.company_contact).

Fake sentinel numbers only. The phone is read from the stored record's top-level ``phone`` field and nowhere else.
"""

import copy
import logging
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .company_contact import CompanyPhone, extract_company_contact_phones
from .models import Company, CustomerRadar, UserCompanyLead, UserSubscription

COMPANY_PHONE = "2109990001"
PERSON_PHONE = "6999990002"
ADMIN_PHONE = "6999990003"
TEXT_PHONE = "2109990004"
FAX = "2109990005"

RECORD = {
    "arGemi": "990000000777", "coNameEl": "ΔΟΚΙΜΗ ΙΚΕ", "phone": COMPANY_PHONE, "fax": FAX, "email": "x@example.invalid",
    "objective": f"Εμπόριο. Τηλ. επικοινωνίας {TEXT_PHONE}",
    "persons": [
        {"personName": "ΠΡΟΣΩΠΟ Α", "role": "Διαχειριστής", "phone": PERSON_PHONE, "mobile": PERSON_PHONE},
        {"personName": "ΠΡΟΣΩΠΟ Β", "role": "Εταίρος", "contact": {"phone": ADMIN_PHONE}},
    ],
    "gemiOffice": {"id": 3, "descr": "ΕΠΙΜΕΛΗΤΗΡΙΟ", "phone": ADMIN_PHONE},
}


class ExtractionTests(TestCase):
    def phones(self, value):
        return extract_company_contact_phones({"phone": value})

    def test_the_official_company_phone_is_returned_with_a_tel_uri(self):
        self.assertEqual(self.phones(COMPANY_PHONE), (CompanyPhone(COMPANY_PHONE, f"tel:{COMPANY_PHONE}"),))

    def test_missing_null_empty_and_placeholder_values_give_no_phone(self):
        for record in ({}, {"phone": None}, {"phone": ""}, {"phone": "   "}, {"phone": "-"}, {"phone": "ΔΕΝ ΥΠΑΡΧΕΙ"},
                       {"phone": True}, {"phone": {"number": COMPANY_PHONE}}, None, "not a record", []):
            with self.subTest(record=record):
                self.assertEqual(extract_company_contact_phones(record), ())

    def test_whitespace_is_collapsed_and_the_display_is_otherwise_preserved(self):
        (phone,) = self.phones("  210  999\t0001 ")
        self.assertEqual((phone.display, phone.tel_uri), ("210 999 0001", "tel:2109990001"))
        (phone,) = self.phones("210-999.0001")
        self.assertEqual((phone.display, phone.tel_uri), ("210-999.0001", "tel:2109990001"))

    def test_a_country_prefix_is_preserved_and_never_guessed(self):
        (phone,) = self.phones("+30 210 999 0001")
        self.assertEqual((phone.display, phone.tel_uri), ("+30 210 999 0001", "tel:+302109990001"))
        (phone,) = self.phones("2109990001")
        self.assertEqual(phone.tel_uri, "tel:2109990001")  # no +30 invented

    def test_multiple_phones_and_duplicates(self):
        phones = self.phones([COMPANY_PHONE, " 2109990001 ", "+30 2109990009", None, "", 2109990010, False])
        self.assertEqual([p.display for p in phones], [COMPANY_PHONE, "+30 2109990009", "2109990010"])

    def test_the_record_is_never_mutated(self):
        record = copy.deepcopy(RECORD)
        extract_company_contact_phones(record)
        self.assertEqual(record, RECORD)

    def test_only_the_company_contact_field_is_read(self):
        phones = extract_company_contact_phones(RECORD)
        self.assertEqual([p.display for p in phones], [COMPANY_PHONE])
        blob = repr(phones)
        for other in (PERSON_PHONE, ADMIN_PHONE, TEXT_PHONE, FAX):
            self.assertNotIn(other, blob)
        without_company_phone = {**RECORD, "phone": None}
        self.assertEqual(extract_company_contact_phones(without_company_phone), ())


class DossierTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            gemi_number="990000000777", name="ΔΟΚΙΜΗ ΙΚΕ", incorporation_date=timezone.localdate(),
            prefecture="ΑΤΤΙΚΗΣ", raw_data=copy.deepcopy(RECORD),
        )

    def login(self, entitled=True, name="member"):
        user = User.objects.create_user(name, f"{name}@example.com", "StrongPass123")
        if entitled:
            subscription = user.subscription
            subscription.tier, subscription.status = "pro", "active"
            subscription.save()
        self.client.force_login(user)
        return user

    def dossier(self):
        return self.client.get(reverse("company_detail", args=[self.company.gemi_number]))

    def test_the_phone_is_shown_as_a_tel_link(self):
        self.login()
        response = self.dossier()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ΤΗΛΕΦΩΝΟ ΓΕΜΗ")
        self.assertContains(response, f'href="tel:{COMPANY_PHONE}"')
        self.assertContains(response, f"☎ {COMPANY_PHONE}")
        self.assertNotContains(response, "Δεν υπάρχει διαθέσιμο τηλέφωνο στο ΓΕΜΗ")

    def test_only_the_company_phone_ever_reaches_the_page(self):
        self.login()
        content = self.dossier().content.decode()
        for other in (PERSON_PHONE, ADMIN_PHONE, TEXT_PHONE, FAX):
            self.assertNotIn(other, content)
        self.assertNotIn('"persons"', content)
        self.assertNotIn("'phone'", content)
        self.assertNotIn('"phone"', content)

    def test_missing_phone_shows_the_fallback(self):
        for value in (None, "", "-"):
            with self.subTest(value=value):
                Company.objects.filter(pk=self.company.pk).update(raw_data={**RECORD, "phone": value})
                self.client.logout()
                self.login(name=f"member{len(str(value))}{value is None}")
                response = self.dossier()
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Δεν υπάρχει διαθέσιμο τηλέφωνο στο ΓΕΜΗ")
                self.assertNotContains(response, 'href="tel:')
                self.assertNotContains(response, "None")

    def test_multiple_phones_each_get_their_own_link(self):
        Company.objects.filter(pk=self.company.pk).update(raw_data={**RECORD, "phone": [COMPANY_PHONE, "+30 2109990009"]})
        self.login()
        response = self.dossier()
        self.assertContains(response, f'href="tel:{COMPANY_PHONE}"')
        self.assertContains(response, 'href="tel:+302109990009"')

    def test_source_markup_is_escaped(self):
        Company.objects.filter(pk=self.company.pk).update(
            raw_data={**RECORD, "phone": '210 999 0001"><script>alert(1)</script>'})
        self.login()
        response = self.dossier()
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertContains(response, 'href="tel:21099900011"')  # the URI carries digits only, never markup

    def test_the_phone_follows_the_same_subscription_gate_as_the_email(self):
        self.login(entitled=False, name="free")
        response = self.dossier()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ΤΗΛΕΦΩΝΟ ΓΕΜΗ")
        self.assertNotContains(response, COMPANY_PHONE)
        self.assertContains(response, "Απαιτείται συνδρομή")

    def test_existing_dossier_fields_remain(self):
        Company.objects.filter(pk=self.company.pk).update(email="info@example.invalid", website="https://example.invalid")
        self.login()
        response = self.dossier()
        for label in ("ΣΥΣΤΑΣΗ", "ΝΟΜΙΚΗ ΜΟΡΦΗ", "ΠΕΡΙΟΧΗ", "ΔΙΕΥΘΥΝΣΗ", "EMAIL ΕΠΙΚΟΙΝΩΝΙΑΣ", "WEBSITE",
                      "ΔΡΑΣΤΗΡΙΟΤΗΤΕΣ ΚΑΔ", "mailto:info@example.invalid", "ΠΡΟΣΩΠΟ Α"):
            self.assertContains(response, label)

    def test_opening_the_dossier_makes_no_gemi_request_writes_nothing_and_logs_no_phone(self):
        self.login()
        company_before = list(Company.objects.values())
        with patch("gemiapp.services._get") as gemi, self.assertLogs(level="DEBUG") as logs:
            logging.getLogger("gemiapp").debug("dossier-probe")
            response = self.dossier()
        gemi.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(Company.objects.values()), company_before)
        self.assertNotIn(COMPANY_PHONE, "\n".join(logs.output))

    def test_the_phone_adds_no_query(self):
        user = self.login()
        UserCompanyLead.objects.create(user=user, company=self.company, status="viewed")
        self.dossier()  # warm caches
        with patch.object(Company, "gemi_phones", property(lambda company: ())):
            with CaptureQueriesContext(connection) as without_phone:
                self.dossier()
        with CaptureQueriesContext(connection) as with_phone:
            self.dossier()
        self.assertEqual(len(with_phone.captured_queries), len(without_phone.captured_queries))

    def test_nothing_else_changes(self):
        user = self.login()
        before = (list(CustomerRadar.objects.values()), list(UserSubscription.objects.values()),
                  list(UserCompanyLead.objects.exclude(company=self.company).values()), user.subscription.has_entitlement)
        self.dossier()
        user.refresh_from_db()
        after = (list(CustomerRadar.objects.values()), list(UserSubscription.objects.values()),
                 list(UserCompanyLead.objects.exclude(company=self.company).values()), user.subscription.has_entitlement)
        self.assertEqual(after, before)
