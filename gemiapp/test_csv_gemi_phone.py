"""The GEMI contact phone in the CSV exports (customer request, 2026-11-07).

The column reads the same, single approved source as the Dossier: the top-level ``raw_data["phone"]``.
A partner's or manager's number under ``persons`` must never reach a file, and neither does a sole trader's
phone (ΑΤΟΜΙΚΗ, GEMI legalType id 16): there it is usually the owner's own number. Unknown form = excluded.
"""

import csv
import io

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Company, CustomerRadar, RadarMatch, UserCompanyLead

HEADER = "Τηλέφωνο ΓΕΜΗ"
PERSON_PHONE = "6999999999"


SOLE_TRADER_PHONE = "6911111111"


def _raw(phone, legal_id=3, legal_descr="ΙΚΕ"):
    return {
        "phone": phone,
        "legalType": {"id": legal_id, "descr": legal_descr},
        "persons": [{"firstName": "ΓΙΑΝΝΗΣ", "phone": PERSON_PHONE}],
    }


class CsvGemiPhoneTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("phone@example.com", "phone@example.com", "StrongPass123")
        sub = self.user.subscription
        sub.tier = "pro"
        sub.status = "active"
        sub.save()
        self.client.force_login(self.user)
        today = timezone.localdate()
        self.with_phone = Company.objects.create(
            gemi_number="8100001", name="ΜΕ ΤΗΛΕΦΩΝΟ ΑΕ", incorporation_date=today,
            prefecture="ΑΤΤΙΚΗΣ", legal_type="ΙΚΕ", raw_data=_raw(["2101234567", " 2101234567 ", "2109876543"]),
        )
        self.without_phone = Company.objects.create(
            gemi_number="8100002", name="ΧΩΡΙΣ ΤΗΛΕΦΩΝΟ ΑΕ", incorporation_date=today,
            prefecture="ΑΤΤΙΚΗΣ", legal_type="ΙΚΕ", raw_data=_raw(None),
        )
        self.sole_trader = Company.objects.create(
            gemi_number="8100003", name="ΠΑΠΑΔΟΠΟΥΛΟΣ ΓΕΩΡΓΙΟΣ", incorporation_date=today,
            prefecture="ΑΤΤΙΚΗΣ", legal_type="ΑΤΟΜΙΚΗ", raw_data=_raw(SOLE_TRADER_PHONE, 16, "ΑΤΟΜΙΚΗ"),
        )
        self.sole_trader_by_id = Company.objects.create(
            gemi_number="8100004", name="ΝΙΚΟΛΑΟΥ ΜΑΡΙΑ", incorporation_date=today,
            prefecture="ΑΤΤΙΚΗΣ", legal_type="", raw_data=_raw(SOLE_TRADER_PHONE, 16, ""),
        )
        self.sole_trader_by_label = Company.objects.create(
            gemi_number="8100005", name="ΙΩΑΝΝΟΥ ΚΩΣΤΑΣ", incorporation_date=today,
            prefecture="ΑΤΤΙΚΗΣ", legal_type="Ατομική επιχείρηση", raw_data={"phone": SOLE_TRADER_PHONE},
        )
        self.unknown_form = Company.objects.create(
            gemi_number="8100006", name="ΑΓΝΩΣΤΗ ΜΟΡΦΗ", incorporation_date=today,
            prefecture="ΑΤΤΙΚΗΣ", legal_type="", raw_data={"phone": SOLE_TRADER_PHONE},
        )
        self.radar = CustomerRadar.objects.create(user=self.user, name="Όλα", prefectures=["ΑΤΤΙΚΗΣ"])
        for company in Company.objects.all():
            lead = UserCompanyLead.objects.create(user=self.user, company=company, status="new")
            RadarMatch.objects.create(radar=self.radar, lead=lead, company=company, matched_on=today)

    def _table(self, response):
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        header, body = rows[0], rows[1:]
        column = header.index(HEADER)
        return {row[header.index("Επωνυμία")]: row[column] for row in body}, response.content.decode("utf-8-sig")

    def _assert_phone_column(self, response):
        cells, content = self._table(response)
        self.assertEqual(cells["ΜΕ ΤΗΛΕΦΩΝΟ ΑΕ"], "2101234567 / 2109876543")
        self.assertEqual(cells["ΧΩΡΙΣ ΤΗΛΕΦΩΝΟ ΑΕ"], "")
        for name in ("ΠΑΠΑΔΟΠΟΥΛΟΣ ΓΕΩΡΓΙΟΣ", "ΝΙΚΟΛΑΟΥ ΜΑΡΙΑ", "ΙΩΑΝΝΟΥ ΚΩΣΤΑΣ", "ΑΓΝΩΣΤΗ ΜΟΡΦΗ"):
            self.assertEqual(cells[name], "", name)
        self.assertNotIn(PERSON_PHONE, content)
        self.assertNotIn(SOLE_TRADER_PHONE, content)

    def test_dashboard_export_has_the_gemi_phone(self):
        self._assert_phone_column(self.client.get(reverse("export_csv")))

    def test_lead_inbox_export_has_the_gemi_phone(self):
        self._assert_phone_column(self.client.get(reverse("lead_export_csv")))

    def test_radar_export_has_the_gemi_phone(self):
        self._assert_phone_column(self.client.get(reverse("radar_export_csv", args=[self.radar.pk])))

    def test_free_accounts_still_cannot_export(self):
        sub = self.user.subscription
        sub.tier = "free"
        sub.status = "inactive"
        sub.save()
        for name, args in (("export_csv", []), ("lead_export_csv", []), ("radar_export_csv", [self.radar.pk])):
            self.assertRedirects(self.client.get(reverse(name, args=args)), reverse("pricing"),
                                 fetch_redirect_response=False)


class SoleTraderPhoneRuleTests(TestCase):
    """The same rule hides a sole trader's phone on the Dossier and in the opportunity contact block."""

    def test_rule(self):
        from .company_contact import company_contact, phone_may_be_shown, phone_withheld

        company = Company(legal_type="ΙΚΕ", raw_data=_raw("2101234567"))
        self.assertTrue(phone_may_be_shown(company))
        self.assertEqual([p.display for p in company_contact(company).phones], ["2101234567"])
        for sole in (
            Company(legal_type="ΑΤΟΜΙΚΗ", raw_data=_raw(SOLE_TRADER_PHONE, 16, "ΑΤΟΜΙΚΗ")),
            Company(legal_type="", raw_data=_raw(SOLE_TRADER_PHONE, 16, "")),
            Company(legal_type="Ατομική επιχείρηση", raw_data={"phone": SOLE_TRADER_PHONE}),
            Company(legal_type="", raw_data={"phone": SOLE_TRADER_PHONE}),
        ):
            self.assertFalse(phone_may_be_shown(sole))
            self.assertEqual(sole.gemi_phones, ())
            self.assertEqual(company_contact(sole).phones, ())
            self.assertTrue(phone_withheld(sole))
        self.assertFalse(phone_withheld(Company(legal_type="ΑΤΟΜΙΚΗ", raw_data={})))

    def test_dossier_hides_a_sole_traders_phone(self):
        user = User.objects.create_user("d@example.com", "d@example.com", "StrongPass123")
        sub = user.subscription
        sub.tier, sub.status = "pro", "active"
        sub.save()
        self.client.force_login(user)
        sole = Company.objects.create(
            gemi_number="8200001", name="ΠΑΠΑΔΟΠΟΥΛΟΣ ΓΕΩΡΓΙΟΣ", incorporation_date=timezone.localdate(),
            prefecture="ΑΤΤΙΚΗΣ", legal_type="ΑΤΟΜΙΚΗ", raw_data=_raw(SOLE_TRADER_PHONE, 16, "ΑΤΟΜΙΚΗ"),
        )
        page = self.client.get(reverse("company_detail", args=[sole.gemi_number])).content.decode()
        self.assertNotIn(SOLE_TRADER_PHONE, page)
        self.assertIn("Δεν εμφανίζεται για ατομικές επιχειρήσεις", page)

        ike = Company.objects.create(
            gemi_number="8200002", name="ΔΟΚΙΜΗ ΙΚΕ", incorporation_date=timezone.localdate(),
            prefecture="ΑΤΤΙΚΗΣ", legal_type="ΙΚΕ", raw_data=_raw("2101234567"),
        )
        page = self.client.get(reverse("company_detail", args=[ike.gemi_number])).content.decode()
        self.assertIn("tel:2101234567", page)
        self.assertNotIn("Δεν εμφανίζεται για ατομικές επιχειρήσεις", page)
