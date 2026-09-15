"""Tests for GEMI response-shape validation (gemiapp.ingestion.schemas) and its failure behaviour.

No test reaches the network: payloads are handed to the validator directly or served by the
FakeTransport from test_gemi_client.
"""

import copy
from datetime import date, timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from .ingestion import (
    GEMI_RESPONSE_SCHEMA_VERSION,
    GemiApiError,
    GemiAuthenticationError,
    GemiBadRequestError,
    GemiBudgetTimeoutError,
    GemiBudgetUnavailableError,
    GemiConfigurationError,
    GemiLane,
    GemiNotFoundError,
    GemiResponseFormatError,
    GemiResponseValidationError,
    GemiRetryExhaustedError,
    ResponseFamily,
    validate_response,
)
from .models import ActivityCode, Company, CompanyActivity, ImportRun, RadarMatch, UserCompanyLead
from .test_gemi_client import SECRET, NoNetworkMixin, gemi_item, make_client, response, routed
from .tests import SAMPLE

PERSON_SENTINEL = "SENTINEL-ΠΡΟΣΩΠΟ-4821"
CONTACT_SENTINEL = "sentinel.contact@example.invalid"
PHONE_SENTINEL = "2109990004821"

INVALID_STRUCTURE = GemiResponseValidationError.INVALID_STRUCTURE
MISSING_FIELD = GemiResponseValidationError.MISSING_FIELD
WRONG_TYPE = GemiResponseValidationError.WRONG_TYPE
INVALID_CONTAINER = GemiResponseValidationError.INVALID_CONTAINER


def full_item(ar_gemi="118717203000", day="2026-09-14", **overrides):
    """A company record in the full shape observed from the live API, with sentinel personal data."""
    item = {
        "arGemi": ar_gemi, "afm": "099999999", "coNameEl": "ΔΟΚΙΜΑΣΤΙΚΗ ΙΚΕ", "coNamesEn": ["DOKIMASTIKI IKE"],
        "coTitlesEl": [], "coTitlesEn": [],
        "legalType": {"id": 19, "descr": "ΙΚΕ"}, "status": {"id": 3, "descr": "Ενεργή"},
        "prefecture": {"id": 5, "descr": "ΑΤΤΙΚΗΣ"}, "municipality": {"id": 61190, "descr": "ΚΗΦΙΣΙΑΣ / ΒΟΡΕΙΟΥ ΤΟΜΕΑ ΑΘΗΝΩΝ"},
        "gemiOffice": {"id": 3, "descr": "ΕΠΑΓΓΕΛΜΑΤΙΚΟ ΕΠΙΜΕΛΗΤΗΡΙΟ ΑΘΗΝΑΣ"},
        "city": "ΚΗΦΙΣΙΑ", "street": "ΟΔΟΣ ΔΟΚΙΜΗΣ", "streetNumber": "6", "zipCode": "14561", "poBox": None,
        "email": CONTACT_SENTINEL, "url": None, "phone": PHONE_SENTINEL, "fax": None, "objective": "ΕΜΠΟΡΙΟ",
        "isBranch": False, "branch": [], "autoRegistered": False,
        "incorporationDate": day, "lastStatusChange": day,
        "capital": [{"capitalStock": 1000, "currency": "Euro", "ecsokefalaiikes": 0, "eggiitikes": 0}],
        "stocks": [{"stockTypeId": 1, "amount": 100, "nominalPrice": 10, "stockType": "ΚΟΙΝΕΣ"}],
        "persons": [{
            "personName": PERSON_SENTINEL, "businessName": None, "role": "Διαχειριστής", "dtFrom": day, "dtTo": None,
            "isRepresentativeAlone": True, "isRepresentativeInCommon": False, "percentage": "100", "category": "Εταίρος",
        }],
        "activities": [
            {"activity": {"id": "47191002", "descr": "ΛΙΑΝΙΚΟ ΕΜΠΟΡΙΟ", "kadVersion": "kad_2026"},
             "type": "Κύρια", "dtFrom": day, "dtTo": None},
            {"activity": {"id": "35111000", "descr": "ΠΑΡΑΓΩΓΗ ΗΛΕΚΤΡΙΚΗΣ ΕΝΕΡΓΕΙΑΣ", "kadVersion": "kad_2008"},
             "type": "Δευτερεύουσα", "dtFrom": "2007-06-28", "dtTo": "2026-03-01"},
        ],
    }
    item.update(overrides)
    return item


def page(*items, total=None):
    return {
        "searchMetadata": {"totalCount": len(items) if total is None else total, "resultsOffset": 0, "resultsSize": str(len(items))},
        "searchResults": list(items),
    }


class ValidationAssertions:
    def assertInvalid(self, payload, kind, location, family=ResponseFamily.COMPANY_SEARCH):
        with self.assertRaises(GemiResponseValidationError) as caught:
            validate_response(family, payload, path="/test")
        error = caught.exception
        self.assertEqual((error.kind, error.location), (kind, location), str(error))
        self.assertEqual(error.family, family.value)
        self.assertEqual(error.schema_version, GEMI_RESPONSE_SCHEMA_VERSION)
        self.assertEqual(error.path, "/test")
        return error


class CompanySearchContractTests(ValidationAssertions, SimpleTestCase):
    family = ResponseFamily.COMPANY_SEARCH

    # --- valid ---------------------------------------------------------------------------------

    def test_a_normal_full_page_is_valid(self):
        validate_response(self.family, page(full_item(), full_item("118717204000")))

    def test_optional_and_null_fields_are_tolerated(self):
        minimal = {"arGemi": "118717203000"}
        nulls = full_item(
            afm=None, coNameEl=None, coTitlesEl=None, legalType=None, status=None, prefecture=None, municipality=None,
            gemiOffice=None, incorporationDate=None, lastStatusChange=None, email=None, url=None, city=None,
            activities=[
                {"activity": {"id": "47191002", "descr": None, "kadVersion": None}, "type": None, "dtFrom": None, "dtTo": None},
                {"activity": None},
                {},
            ],
        )
        partial_reference = full_item(legalType={"descr": "ΙΚΕ"}, status={"descr": "Ενεργή", "isActive": True})
        validate_response(self.family, page(minimal, nulls, partial_reference))
        validate_response(self.family, {"searchResults": [minimal], "searchMetadata": None})
        validate_response(self.family, {"searchResults": [minimal]})

    def test_unknown_extra_fields_are_ignored(self):
        item = full_item(newTopLevelField={"anything": [1, 2]})
        item["status"]["newStatusField"] = "x"
        item["activities"][0]["newActivityField"] = True
        item["activities"][0]["activity"]["newKadField"] = 7
        payload = page(item)
        payload["searchMetadata"]["newMetadataField"] = "x"
        payload["newContainerField"] = []
        validate_response(self.family, payload)

    def test_an_empty_result_set_is_valid(self):
        validate_response(self.family, {"searchMetadata": {"totalCount": 0, "resultsOffset": 400, "resultsSize": "0"}, "searchResults": []})

    def test_existing_importer_fixtures_are_valid(self):
        validate_response(self.family, page(SAMPLE, gemi_item(900000001000, date(2026, 9, 14))))

    def test_observed_data_quality_problems_are_not_shape_problems(self):
        validate_response(self.family, page(full_item(day="9011-12-09"), full_item("1", day="1821-01-01"), full_item(incorporationDate="")))

    # --- invalid -------------------------------------------------------------------------------

    def test_top_level_wrong_type(self):
        self.assertInvalid([full_item()], INVALID_STRUCTURE, "$")
        self.assertInvalid(None, INVALID_STRUCTURE, "$")
        self.assertInvalid("maintenance", INVALID_STRUCTURE, "$")

    def test_missing_result_container(self):
        self.assertInvalid({"searchMetadata": {"totalCount": 3}}, MISSING_FIELD, "searchResults")

    def test_result_container_wrong_shape(self):
        self.assertInvalid({"searchResults": {"0": full_item()}}, INVALID_CONTAINER, "searchResults")
        self.assertInvalid({"searchResults": None}, INVALID_CONTAINER, "searchResults")

    def test_result_item_wrong_type(self):
        self.assertInvalid(page(full_item(), "118717204000"), INVALID_CONTAINER, "searchResults[1]")
        self.assertInvalid(page(None), INVALID_CONTAINER, "searchResults[0]")

    def test_missing_or_unusable_company_identifier(self):
        item = full_item()
        del item["arGemi"]
        self.assertInvalid(page(item), MISSING_FIELD, "searchResults[0].arGemi")
        self.assertInvalid(page(full_item(ar_gemi=None)), WRONG_TYPE, "searchResults[0].arGemi")
        self.assertInvalid(page(full_item(ar_gemi="1187A7203000")), WRONG_TYPE, "searchResults[0].arGemi")
        self.assertInvalid(page(full_item(ar_gemi=True)), WRONG_TYPE, "searchResults[0].arGemi")

    def test_malformed_activities_collection(self):
        self.assertInvalid(page(full_item(activities={"activity": {"id": "47191002"}})), INVALID_CONTAINER, "searchResults[0].activities")
        self.assertInvalid(page(full_item(activities="47191002")), INVALID_CONTAINER, "searchResults[0].activities")

    def test_activity_item_wrong_type(self):
        self.assertInvalid(page(full_item(activities=["47191002"])), INVALID_CONTAINER, "searchResults[0].activities[0]")
        item = full_item()
        item["activities"][1]["activity"] = "47191002"
        self.assertInvalid(page(item), WRONG_TYPE, "searchResults[0].activities[1].activity")

    def test_activity_code_missing_or_unusable(self):
        item = full_item()
        del item["activities"][0]["activity"]["id"]
        self.assertInvalid(page(item), MISSING_FIELD, "searchResults[0].activities[0].activity.id")
        item = full_item()
        item["activities"][0]["activity"]["id"] = {"code": "47191002"}
        self.assertInvalid(page(item), WRONG_TYPE, "searchResults[0].activities[0].activity.id")
        item = full_item()
        item["activities"][0]["activity"]["id"] = None
        self.assertInvalid(page(item), WRONG_TYPE, "searchResults[0].activities[0].activity.id")

    def test_incompatible_dates(self):
        self.assertInvalid(page(full_item(incorporationDate=20260914)), WRONG_TYPE, "searchResults[0].incorporationDate")
        self.assertInvalid(page(full_item(incorporationDate="14/09/2026")), WRONG_TYPE, "searchResults[0].incorporationDate")
        self.assertInvalid(page(full_item(lastStatusChange={"date": "2026-09-14"})), WRONG_TYPE, "searchResults[0].lastStatusChange")
        item = full_item()
        item["activities"][1]["dtTo"] = 1772323200
        self.assertInvalid(page(item), WRONG_TYPE, "searchResults[0].activities[1].dtTo")

    def test_incompatible_reference_data(self):
        self.assertInvalid(page(full_item(status="Ενεργή")), WRONG_TYPE, "searchResults[0].status")
        self.assertInvalid(page(full_item(prefecture={"id": "ΑΤΤ", "descr": "ΑΤΤΙΚΗΣ"})), WRONG_TYPE, "searchResults[0].prefecture.id")
        self.assertInvalid(page(full_item(legalType={"id": 19, "descr": ["ΙΚΕ"]})), WRONG_TYPE, "searchResults[0].legalType.descr")
        self.assertInvalid(page(full_item(status={"id": 3, "isActive": "yes"})), WRONG_TYPE, "searchResults[0].status.isActive")

    def test_incompatible_scalars_and_metadata(self):
        self.assertInvalid(page(full_item(coTitlesEl="ΔΟΚΙΜΗ")), INVALID_CONTAINER, "searchResults[0].coTitlesEl")
        self.assertInvalid(page(full_item(city={"descr": "ΚΗΦΙΣΙΑ"})), WRONG_TYPE, "searchResults[0].city")
        self.assertInvalid(page(full_item(afm=True)), WRONG_TYPE, "searchResults[0].afm")
        payload = page(full_item())
        payload["searchMetadata"]["totalCount"] = "many"
        self.assertInvalid(payload, WRONG_TYPE, "searchMetadata.totalCount")

    def test_errors_name_the_company_and_types_but_never_values(self):
        item = full_item(activities={"broken": True})
        error = self.assertInvalid(page(full_item("118717204000"), item), INVALID_CONTAINER, "searchResults[1].activities")
        message = str(error)
        self.assertIn("arGemi=118717203000", message)
        self.assertIn("company_search v1", message)
        self.assertIn("αναμενόταν array, βρέθηκε object", message)
        for sentinel in (PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, "ΔΟΚΙΜΑΣΤΙΚΗ"):
            self.assertNotIn(sentinel, message)
            self.assertNotIn(sentinel, repr(error))


class CompanyDetailContractTests(ValidationAssertions, SimpleTestCase):
    family = ResponseFamily.COMPANY_DETAIL

    def test_detail_uses_the_company_record_contract(self):
        validate_response(self.family, full_item())
        self.assertInvalid([full_item()], INVALID_STRUCTURE, "$", family=self.family)
        item = full_item()
        del item["arGemi"]
        self.assertInvalid(item, MISSING_FIELD, "arGemi", family=self.family)
        error = self.assertInvalid(full_item(activities={}), INVALID_CONTAINER, "activities", family=self.family)
        self.assertIn("arGemi=118717203000", str(error))


REFERENCE_SAMPLES = {
    ResponseFamily.ACTIVITIES: {"id": "00010000", "descr": "ΕΛΛΕΙΨΗ ΔΡΑΣΤΗΡΙΟΤΗΤΑΣ", "descrEn": None, "lastUpdated": "2026-02-25 15:56:01", "kadVersion": "kad_2026"},
    ResponseFamily.PREFECTURES: {"id": "5", "descr": "ΑΤΤΙΚΗΣ", "descrEn": "ATTICA", "lastUpdated": "2012-06-13 00:00:00"},
    ResponseFamily.MUNICIPALITIES: {"id": "61190", "prefectureId": "5", "descr": "ΚΗΦΙΣΙΑΣ / ΒΟΡΕΙΟΥ ΤΟΜΕΑ ΑΘΗΝΩΝ", "descrEn": None, "lastUpdated": "2025-03-26 16:01:46"},
    ResponseFamily.COMPANY_STATUSES: {"id": "3", "descr": "Ενεργή", "descrEn": "Active", "isActive": True, "lastUpdated": "2015-12-11 18:35:51"},
    ResponseFamily.LEGAL_TYPES: {"id": "19", "descr": "ΙΚΕ", "descrEn": "PC", "lastUpdated": "2024-11-29 17:46:10"},
    ResponseFamily.GEMI_OFFICES: {
        "id": "3", "descr": "ΕΠΑΓΓΕΛΜΑΤΙΚΟ ΕΠΙΜΕΛΗΤΗΡΙΟ ΑΘΗΝΑΣ", "descrEn": None, "lastUpdated": "2026-04-03 12:26:45",
        "address": "ΑΚΑΔΗΜΙΑΣ", "city": "ΑΘΗΝΑ", "zipCode": "10671", "phone": None, "fax": None, "email": None, "url": None,
    },
    ResponseFamily.DECISION_SUBJECTS: {"id": "31", "descr": "Ανακοίνωση αύξησης μετοχικού κεφαλαίου", "descrEn": None, "lastUpdated": "2025-12-08 17:08:17"},
}


class ReferenceDataContractTests(ValidationAssertions, SimpleTestCase):
    def test_observed_shapes_extra_fields_and_integer_ids_are_valid(self):
        for family, sample in REFERENCE_SAMPLES.items():
            with self.subTest(family=family.value):
                validate_response(family, [sample])
                validate_response(family, [{**sample, "id": int(sample["id"]), "newField": {"x": 1}}])
                validate_response(family, [])

    def test_every_family_rejects_incompatible_shapes(self):
        for family, sample in REFERENCE_SAMPLES.items():
            with self.subTest(family=family.value):
                self.assertInvalid({"results": [sample]}, INVALID_STRUCTURE, "$", family=family)
                self.assertInvalid([sample, "3"], INVALID_CONTAINER, "$[1]", family=family)
                without_id = {key: value for key, value in sample.items() if key != "id"}
                self.assertInvalid([without_id], MISSING_FIELD, "$[0].id", family=family)
                self.assertInvalid([{**sample, "id": "ab"}], WRONG_TYPE, "$[0].id", family=family)
                self.assertInvalid([{**sample, "descr": None}], WRONG_TYPE, "$[0].descr", family=family)

    def test_family_specific_fields(self):
        self.assertInvalid(
            [{**REFERENCE_SAMPLES[ResponseFamily.MUNICIPALITIES], "prefectureId": "ΑΒ"}],
            WRONG_TYPE, "$[0].prefectureId", family=ResponseFamily.MUNICIPALITIES,
        )
        self.assertInvalid(
            [{**REFERENCE_SAMPLES[ResponseFamily.COMPANY_STATUSES], "isActive": "yes"}],
            WRONG_TYPE, "$[0].isActive", family=ResponseFamily.COMPANY_STATUSES,
        )
        self.assertInvalid(
            [{**REFERENCE_SAMPLES[ResponseFamily.ACTIVITIES], "kadVersion": 2026}],
            WRONG_TYPE, "$[0].kadVersion", family=ResponseFamily.ACTIVITIES,
        )


class ClientValidationTests(NoNetworkMixin, SimpleTestCase):
    def test_an_invalid_search_page_raises_a_distinct_error_and_is_not_retried(self):
        broken = page(full_item(activities={"broken": True}))
        client, transport, _, _ = make_client(response(200, broken, headers={"X-Kong-Request-Id": "req-4821"}))

        with self.assertLogs("gemiapp.ingestion", level="ERROR") as logs:
            with self.assertRaises(GemiResponseValidationError) as caught:
                client.search_companies({"isActive": "true"}, lane=GemiLane.DIGEST_IMPORT)

        error = caught.exception
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual((error.family, error.kind, error.location), ("company_search", INVALID_CONTAINER, "searchResults[0].activities"))
        self.assertEqual((error.path, error.request_id, error.schema_version), ("/companies", "req-4821", GEMI_RESPONSE_SCHEMA_VERSION))
        self.assertIsInstance(error, GemiApiError)
        self.assertIsInstance(error, RuntimeError)
        for other in (GemiRetryExhaustedError, GemiAuthenticationError, GemiBadRequestError, GemiNotFoundError,
                      GemiConfigurationError, GemiBudgetTimeoutError, GemiBudgetUnavailableError, GemiResponseFormatError):
            self.assertNotIsInstance(error, other)

        output = "\n".join(logs.output)
        for expected in ("/companies", "company_search", "schema v1", "invalid_container", "searchResults[0].activities", "req-4821", "DIGEST_IMPORT"):
            self.assertIn(expected, output)
        for secret in (SECRET, PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL):
            self.assertNotIn(secret, output)
            self.assertNotIn(secret, str(error))

    def test_get_validates_when_a_family_is_given_and_not_otherwise(self):
        statuses = [REFERENCE_SAMPLES[ResponseFamily.COMPANY_STATUSES]]
        client, _, _, _ = make_client(response(200, statuses))
        self.assertEqual(client.get("/metadata/companyStatuses", lane=GemiLane.MONITORED_REFRESH, family=ResponseFamily.COMPANY_STATUSES), statuses)

        client, _, _, _ = make_client(response(200, {"not": "a list"}))
        with self.assertRaises(GemiResponseValidationError):
            client.get("/metadata/companyStatuses", lane=GemiLane.MONITORED_REFRESH, family=ResponseFamily.COMPANY_STATUSES)

        client, _, _, _ = make_client(response(200, {"not": "a list"}))
        self.assertEqual(client.get("/health", lane=GemiLane.DIGEST_IMPORT), {"not": "a list"})

    def test_empty_search_404_and_invalid_json_keep_their_own_behaviour(self):
        client, _, _, _ = make_client(response(404, body=b""))
        self.assertEqual(client.search_companies({"isActive": "false"}, lane=GemiLane.DIGEST_IMPORT)["searchResults"], [])

        client, _, _, _ = make_client(response(200, body=b"<html>maintenance</html>"))
        with self.assertRaises(GemiResponseFormatError):
            client.search_companies({"isActive": "true"}, lane=GemiLane.DIGEST_IMPORT)


def snapshot_rows():
    return (
        list(Company.objects.order_by("gemi_number").values(
            "gemi_number", "vat_number", "name", "trade_names", "legal_type", "status", "is_active", "incorporation_date",
            "gemi_office", "prefecture", "municipality", "city", "address", "postal_code", "email", "website",
            "activities", "raw_data",
        )),
        list(CompanyActivity.objects.order_by("company__gemi_number", "code", "activity_type").values_list(
            "company__gemi_number", "code", "description", "activity_type",
        )),
    )


class ImportSafetyTests(NoNetworkMixin, TestCase):
    target = date(2026, 9, 14)

    def assertNothingWritten(self, activity_codes_before):
        self.assertEqual(Company.objects.count(), 0)
        self.assertEqual(CompanyActivity.objects.count(), 0)
        self.assertEqual(ActivityCode.objects.count(), activity_codes_before)
        self.assertEqual(UserCompanyLead.objects.count(), 0)
        self.assertEqual(RadarMatch.objects.count(), 0)

    def test_an_invalid_later_page_leaves_no_company_rows_and_fails_the_run(self):
        from .services import import_for_date

        day = self.target.isoformat()
        older = (self.target - timedelta(days=1)).isoformat()
        valid_active = page(full_item("118717203000", day), full_item("118717204000", day), full_item("118717205000", older))
        broken = copy.deepcopy(full_item("118717206000", older))
        broken["activities"][0] = "47191002"
        invalid_inactive = page(broken)
        client, _, _, _ = make_client(routed({("true", "0"): valid_active, ("false", "0"): invalid_inactive}))
        activity_codes_before = ActivityCode.objects.count()

        with patch("gemiapp.services.get_gemi_client", return_value=client):
            with self.assertLogs("gemiapp", level="ERROR") as logs:
                with self.assertRaises(GemiResponseValidationError):
                    import_for_date(self.target)

        self.assertNothingWritten(activity_codes_before)
        run = ImportRun.objects.get()
        self.assertEqual(run.status, "failed")
        self.assertIn("company_search v1", run.error_message)
        self.assertIn("searchResults[0].activities[0]", run.error_message)
        self.assertIn("arGemi=118717206000", run.error_message)
        output = "\n".join(logs.output)
        self.assertIn(f"ImportRun {run.pk}", output)
        for secret in (SECRET, PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL):
            self.assertNotIn(secret, output)
            self.assertNotIn(secret, run.error_message)

    def test_an_invalid_first_page_leaves_no_company_rows(self):
        from .services import import_for_date

        client, _, _, _ = make_client(routed({("true", "0"): {"searchMetadata": {"totalCount": 2}}}))
        activity_codes_before = ActivityCode.objects.count()

        with patch("gemiapp.services.get_gemi_client", return_value=client):
            with self.assertRaises(GemiResponseValidationError) as caught:
                import_for_date(self.target)

        self.assertEqual((caught.exception.kind, caught.exception.location), (MISSING_FIELD, "searchResults"))
        self.assertNothingWritten(activity_codes_before)
        self.assertEqual(ImportRun.objects.get().status, "failed")

    def test_backfill_keeps_earlier_valid_pages_and_writes_nothing_from_the_invalid_page(self):
        from .services import import_companies_since_date

        start = self.target - timedelta(days=5)
        first = {"searchMetadata": {"totalCount": 400}, "searchResults": [gemi_item(800000000000 + i, self.target) for i in range(200)]}
        second = {"searchMetadata": {"totalCount": 400}, "searchResults": (
            [gemi_item(810000000000 + i, self.target) for i in range(199)] + [{"coNameEl": "ΧΩΡΙΣ ΑΡΙΘΜΟ ΓΕΜΗ"}]
        )}
        client, _, _, _ = make_client(routed({("true", "0"): first, ("true", "200"): second}))

        with patch("gemiapp.services.get_gemi_client", return_value=client):
            with self.assertRaises(GemiResponseValidationError) as caught:
                import_companies_since_date(start)

        self.assertEqual(caught.exception.location, "searchResults[199].arGemi")
        self.assertEqual(Company.objects.count(), 200)
        self.assertFalse(Company.objects.filter(gemi_number__startswith="81").exists())

    def test_valid_full_payloads_store_the_same_rows_as_the_unvalidated_legacy_seam(self):
        from .services import import_for_date

        day = self.target.isoformat()
        items = [full_item("118717203000", day), full_item("118717204000", day, legalType={"descr": "ΙΚΕ"}, activities=None)]
        client, _, _, _ = make_client(routed({("true", "0"): page(*items)}))

        with patch("gemiapp.services.get_gemi_client", return_value=client):
            run = import_for_date(self.target)
        via_validated_client = snapshot_rows()
        self.assertEqual((run.status, run.created_count), ("success", 2))

        Company.objects.all().delete()
        ImportRun.objects.all().delete()
        with patch("gemiapp.services.fetch_companies", return_value=copy.deepcopy(items)):
            import_for_date(self.target)

        self.assertEqual(via_validated_client, snapshot_rows())
