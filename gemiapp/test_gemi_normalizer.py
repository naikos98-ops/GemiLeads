"""Tests for Normaliser v1 (gemiapp.ingestion.normalizer).

Fixtures only: no GEMI call, no database (SimpleTestCase refuses queries).
"""

import ast
import copy
import inspect
import json
from dataclasses import fields
from datetime import date, datetime
from pathlib import Path

from django.test import SimpleTestCase

from .ingestion import (
    GEMI_NORMALIZER_VERSION,
    DatePolicy,
    DateQuality,
    NormalizedActivity,
    NormalizedCompany,
    NormalizedDate,
    NormalizedReference,
    ResponseFamily,
    fields_not_approved_for_history,
    normalize_company,
    validate_response,
)
from .ingestion import normalizer as normalizer_module
from .test_gemi_client import gemi_item
from .test_gemi_validation import CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL, full_item
from .tests import SAMPLE

AS_OF = date(2026, 9, 15)

CANONICAL_FIELDS = [
    "ar_gemi", "name", "status", "is_active", "legal_type", "gemi_office", "prefecture", "municipality",
    "city", "postal_code", "street", "street_number", "incorporation_date", "last_status_change",
    "activities", "activities_without_code", "as_of", "normalizer_version",
]


def normalize(record, as_of=AS_OF, **kwargs):
    return normalize_company(record, as_of=as_of, **kwargs)


def activity(code, *, version="kad_2026", kind="Κύρια", date_from="2026-01-01", date_to=None, descr="ΔΡΑΣΤΗΡΙΟΤΗΤΑ"):
    return {"activity": {"id": code, "descr": descr, "kadVersion": version}, "type": kind, "dtFrom": date_from, "dtTo": date_to}


class ReferenceNormalizationTests(SimpleTestCase):
    def test_ids_and_descriptions_are_kept_separately(self):
        company = normalize(full_item())

        self.assertEqual(company.status, NormalizedReference("3", "Ενεργή"))
        self.assertEqual(company.legal_type, NormalizedReference("19", "ΙΚΕ"))
        self.assertEqual(company.gemi_office, NormalizedReference("3", "ΕΠΑΓΓΕΛΜΑΤΙΚΟ ΕΠΙΜΕΛΗΤΗΡΙΟ ΑΘΗΝΑΣ"))
        self.assertEqual(company.prefecture, NormalizedReference("5", "ΑΤΤΙΚΗΣ"))
        self.assertEqual(company.municipality, NormalizedReference("61190", "ΚΗΦΙΣΙΑΣ / ΒΟΡΕΙΟΥ ΤΟΜΕΑ ΑΘΗΝΩΝ"))

    def test_a_null_description_is_none_never_the_string_none(self):
        company = normalize(full_item(
            legalType={"id": 19, "descr": None}, status={"id": 3, "descr": None},
            prefecture={"id": 5, "descr": "   "}, city=None, zipCode=None,
        ))

        self.assertEqual(company.legal_type, NormalizedReference("19", None))
        self.assertEqual(company.status, NormalizedReference("3", None))
        self.assertEqual(company.prefecture, NormalizedReference("5", None))
        self.assertIsNone(company.city)
        self.assertNotIn('"None"', json.dumps(company.as_primitive(), ensure_ascii=False))
        self.assertNotIn("'None'", repr(company))

    def test_an_unknown_reference_id_is_kept(self):
        company = normalize(full_item(prefecture={"id": 999999, "descr": "ΝΕΑ ΠΕΡΙΦΕΡΕΙΑΚΗ ΕΝΟΤΗΤΑ"}))
        self.assertEqual(company.prefecture, NormalizedReference("999999", "ΝΕΑ ΠΕΡΙΦΕΡΕΙΑΚΗ ΕΝΟΤΗΤΑ"))

    def test_a_missing_or_null_reference_is_an_empty_reference(self):
        absent = full_item()
        del absent["legalType"], absent["municipality"]
        null = full_item(legalType=None, municipality=None)

        for record in (absent, null):
            company = normalize(record)
            self.assertEqual(company.legal_type, NormalizedReference(None, None))
            self.assertEqual(company.municipality, NormalizedReference(None, None))
        self.assertEqual(normalize(absent), normalize(null))

    def test_integer_and_digit_string_ids_are_the_same_id(self):
        self.assertEqual(
            normalize(full_item(prefecture={"id": 5, "descr": "ΑΤΤΙΚΗΣ"})).prefecture,
            normalize(full_item(prefecture={"id": "5", "descr": "ΑΤΤΙΚΗΣ"})).prefecture,
        )
        self.assertEqual(normalize(full_item(ar_gemi=118717203000)).ar_gemi, "118717203000")

    def test_descriptions_are_nfc_and_whitespace_normalised_without_changing_case(self):
        decomposed = "Ενεργή"  # η + combining acute accent
        company = normalize(full_item(status={"id": 3, "descr": f"  {decomposed}  "}, city="  Κηφισιά\n"))

        self.assertEqual(company.status.description, "Ενεργή")
        self.assertEqual(company.city, "Κηφισιά")

    def test_is_active_is_only_what_the_source_states(self):
        self.assertIsNone(normalize(full_item()).is_active)  # live records state neither
        self.assertFalse(normalize(full_item(status={"id": 17, "descr": "Διαγραφή", "isActive": False})).is_active)
        self.assertTrue(normalize(full_item(isActive=True)).is_active)
        self.assertFalse(normalize(full_item(status={"id": 7, "isActive": False}, isActive=True)).is_active)


class DateNormalizationTests(SimpleTestCase):
    def incorporation(self, value, **kwargs):
        return normalize(full_item(incorporationDate=value), **kwargs).incorporation_date

    def test_a_valid_date(self):
        self.assertEqual(self.incorporation("2026-09-14"), NormalizedDate(date(2026, 9, 14), DateQuality.VALID))
        self.assertTrue(self.incorporation("2026-09-14").is_reliable)
        self.assertEqual(self.incorporation("2026-09-14T00:00:00").value, date(2026, 9, 14))

    def test_missing_null_and_blank_dates(self):
        record = full_item()
        del record["incorporationDate"]
        for company_date in (normalize(record).incorporation_date, self.incorporation(None), self.incorporation(""), self.incorporation("   ")):
            self.assertEqual(company_date, NormalizedDate(None, DateQuality.MISSING))
            self.assertFalse(company_date.is_reliable)

    def test_malformed_dates_are_invalid_and_keep_their_source(self):
        for raw in ("14/09/2026", "2026-02-30", "2026-9-4", "yesterday"):
            with self.subTest(raw=raw):
                self.assertEqual(self.incorporation(raw), NormalizedDate(None, DateQuality.INVALID, raw))

    def test_an_impossible_future_year_is_out_of_range(self):
        self.assertEqual(self.incorporation("9011-12-09"), NormalizedDate(None, DateQuality.OUT_OF_RANGE, "9011-12-09"))
        self.assertEqual(
            normalize(full_item(lastStatusChange="5015-02-05")).last_status_change,
            NormalizedDate(None, DateQuality.OUT_OF_RANGE, "5015-02-05"),
        )

    def test_one_day_of_future_tolerance_and_no_more(self):
        self.assertEqual(self.incorporation("2026-09-16").quality, DateQuality.VALID)
        self.assertEqual(self.incorporation("2026-09-17").quality, DateQuality.OUT_OF_RANGE)

    def test_historical_dates_inside_the_range_are_valid_and_the_placeholder_is_not(self):
        self.assertEqual(self.incorporation("1841-05-30"), NormalizedDate(date(1841, 5, 30), DateQuality.VALID))
        self.assertEqual(self.incorporation("1830-01-01").quality, DateQuality.VALID)
        self.assertEqual(self.incorporation("1821-01-01"), NormalizedDate(None, DateQuality.OUT_OF_RANGE, "1821-01-01"))

    def test_no_date_is_ever_replaced_by_today(self):
        for raw in ("9011-12-09", "1821-01-01", "14/09/2026", None, ""):
            with self.subTest(raw=raw):
                company_date = self.incorporation(raw)
                self.assertIsNone(company_date.value)
                self.assertNotEqual(company_date.value, AS_OF)
                self.assertNotEqual(company_date.value, date.today())

    def test_the_range_is_adjustable_in_one_place(self):
        strict = DatePolicy(earliest=date(1900, 1, 1))
        self.assertEqual(self.incorporation("1841-05-30", date_policy=strict).quality, DateQuality.OUT_OF_RANGE)


class ActivityNormalizationTests(SimpleTestCase):
    def activities(self, *entries, as_of=AS_OF):
        return normalize(full_item(activities=list(entries)), as_of=as_of).activities

    def test_no_dt_to_or_a_null_dt_to_is_current(self):
        without_key = activity("47191002")
        del without_key["dtTo"]
        for entry in (without_key, activity("47191002", date_to=None)):
            (result,) = self.activities(entry)
            self.assertTrue(result.is_current)
            self.assertEqual(result.date_to.quality, DateQuality.MISSING)

    def test_a_future_dt_to_is_current_and_a_past_or_same_day_dt_to_is_ended(self):
        (future,) = self.activities(activity("47191002", date_to="2027-01-01"))
        (same_day,) = self.activities(activity("47191002", date_to=AS_OF.isoformat()))
        (past,) = self.activities(activity("35111000", version="kad_2008", date_to="2026-03-01"))

        self.assertTrue(future.is_current)
        self.assertFalse(same_day.is_current)
        self.assertFalse(past.is_current)

    def test_is_current_follows_as_of(self):
        entry = activity("35111000", version="kad_2008", date_to="2026-03-01")
        (before,) = self.activities(entry, as_of=date(2026, 2, 28))
        (after,) = self.activities(entry, as_of=date(2026, 3, 1))
        self.assertTrue(before.is_current)
        self.assertFalse(after.is_current)

    def test_implausible_or_unreadable_end_dates(self):
        (open_ended,) = self.activities(activity("47191002", date_to="9999-12-31"))
        (unreadable,) = self.activities(activity("47191002", date_to="31/12/2027"))

        self.assertTrue(open_ended.is_current)
        self.assertEqual(open_ended.date_to, NormalizedDate(None, DateQuality.OUT_OF_RANGE, "9999-12-31"))
        self.assertIsNone(unreadable.is_current)
        self.assertEqual(unreadable.date_to.quality, DateQuality.INVALID)

    def test_every_published_activity_attribute_is_preserved(self):
        (result,) = self.activities(activity(
            "35111000", version="kad_2008", kind="Δευτερεύουσα", date_from="2007-06-28", date_to="2026-03-01",
            descr="ΠΑΡΑΓΩΓΗ ΗΛΕΚΤΡΙΚΗΣ ΕΝΕΡΓΕΙΑΣ",
        ))

        self.assertEqual(result, NormalizedActivity(
            code="35111000", description="ΠΑΡΑΓΩΓΗ ΗΛΕΚΤΡΙΚΗΣ ΕΝΕΡΓΕΙΑΣ", activity_type="Δευτερεύουσα", kad_version="kad_2008",
            date_from=NormalizedDate(date(2007, 6, 28), DateQuality.VALID),
            date_to=NormalizedDate(date(2026, 3, 1), DateQuality.VALID), is_current=False,
        ))
        (future_start,) = self.activities(activity("47191002", date_from="2027-01-01"))
        self.assertEqual(future_start.date_from.quality, DateQuality.VALID)

    def test_kad_2008_and_kad_2026_entries_stay_distinguishable(self):
        reclassified = self.activities(
            activity("62010000", version="kad_2008", date_from="2012-01-01", date_to="2026-03-01"),
            activity("62010000", version="kad_2026", date_from="2026-03-01"),
        )

        self.assertEqual(len(reclassified), 2)
        self.assertEqual([(a.code, a.kad_version, a.is_current) for a in reclassified],
                         [("62010000", "kad_2008", False), ("62010000", "kad_2026", True)])

    def test_order_is_deterministic_and_identical_entries_collapse(self):
        entries = [
            activity("56101000"),
            activity("35111000", version="kad_2008", kind="Δευτερεύουσα", date_to="2026-03-01"),
            activity("47191002", kind="Δευτερεύουσα"),
            activity("47191002"),
        ]
        forward = self.activities(*entries)
        backward = self.activities(*reversed(entries))
        duplicated = self.activities(*entries, *copy.deepcopy(entries))

        self.assertEqual(forward, backward)
        self.assertEqual(forward, duplicated)
        self.assertEqual([a.code for a in forward], ["35111000", "47191002", "47191002", "56101000"])

    def test_entries_without_a_code_are_counted_not_silently_dropped(self):
        company = normalize(full_item(activities=[activity("47191002"), {"activity": None}, {}]))
        self.assertEqual(len(company.activities), 1)
        self.assertEqual(company.activities_without_code, 2)
        self.assertEqual(normalize(full_item(activities=None)).activities, ())

    def test_the_input_activities_are_not_mutated(self):
        entries = [activity("56101000"), activity("35111000", version="kad_2008", date_to="2026-03-01")]
        before = copy.deepcopy(entries)
        normalize(full_item(activities=entries))
        self.assertEqual(entries, before)


class DataMinimisationTests(SimpleTestCase):
    def test_person_contact_and_other_excluded_data_never_reach_the_output(self):
        sentinels = {
            PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, "SENTINEL-BUSINESS-NAME", "2109990009999",
            "https://sentinel.example.invalid", "SENTINEL-POBOX", "990004821", "SENTINEL-OBJECTIVE",
            "SENTINEL-TITLE-EL", "SENTINEL-NAME-EN", "SENTINEL-STOCK", "48214821",
        }
        record = full_item(
            fax="2109990009999", url="https://sentinel.example.invalid", poBox="SENTINEL-POBOX", afm="990004821",
            objective="SENTINEL-OBJECTIVE", coTitlesEl=["SENTINEL-TITLE-EL"], coNamesEn=["SENTINEL-NAME-EN"],
            capital=[{"capitalStock": 48214821, "currency": "Euro"}], stocks=[{"stockType": "SENTINEL-STOCK"}],
        )
        record["persons"][0]["businessName"] = "SENTINEL-BUSINESS-NAME"

        company = normalize(record)
        renderings = [repr(company), str(company.as_primitive()), json.dumps(company.as_primitive(), ensure_ascii=False)]
        for rendering in renderings:
            for sentinel in sentinels:
                self.assertNotIn(sentinel, rendering)

    def test_the_output_has_exactly_the_canonical_fields(self):
        self.assertEqual([f.name for f in fields(NormalizedCompany)], CANONICAL_FIELDS)
        self.assertEqual(list(normalize(full_item()).as_primitive()), CANONICAL_FIELDS)

    def test_name_and_street_address_are_marked_not_approved_for_history(self):
        self.assertEqual(fields_not_approved_for_history(), frozenset({"name", "street", "street_number"}))
        company = normalize(full_item())
        self.assertEqual(company.name, "ΔΟΚΙΜΑΣΤΙΚΗ ΙΚΕ")
        self.assertEqual((company.street, company.street_number, company.postal_code), ("ΟΔΟΣ ΔΟΚΙΜΗΣ", "6", "14561"))


class DeterminismAndPurityTests(SimpleTestCase):
    def test_semantically_equivalent_records_normalise_identically(self):
        tidy = full_item(activities=[activity("47191002"), activity("35111000", version="kad_2008", date_to="2026-03-01")])
        messy = full_item(
            ar_gemi=118717203000,
            coNameEl="  ΔΟΚΙΜΑΣΤΙΚΗ   ΙΚΕ ",
            prefecture={"id": "5", "descr": "ΑΤΤΙΚΗΣ "},
            legalType={"descr": "ΙΚΕ", "id": "19"},
            zipCode=14561,
            activities=[activity("35111000", version="kad_2008", date_to="2026-03-01"), activity(" 47191002 ")],
        )
        tidy["legalType"] = {"id": 19, "descr": "ΙΚΕ"}
        tidy["zipCode"] = "14561"

        self.assertEqual(normalize(tidy), normalize(messy))
        self.assertEqual(
            json.dumps(normalize(tidy).as_primitive(), ensure_ascii=False),
            json.dumps(normalize(messy).as_primitive(), ensure_ascii=False),
        )

    def test_running_twice_gives_exactly_the_same_result(self):
        record = full_item()
        self.assertEqual(normalize(record), normalize(record))
        self.assertEqual(json.dumps(normalize(record).as_primitive()), json.dumps(normalize(record).as_primitive()))

    def test_the_input_record_is_not_mutated(self):
        record = full_item()
        before = copy.deepcopy(record)
        normalize(record)
        self.assertEqual(record, before)

    def test_result_carries_its_version_and_evaluation_date(self):
        company = normalize(full_item())
        self.assertEqual(GEMI_NORMALIZER_VERSION, 1)
        self.assertEqual((company.normalizer_version, company.as_of), (1, AS_OF))
        self.assertEqual(company.as_primitive()["as_of"], "2026-09-15")
        self.assertEqual(company.as_primitive()["incorporation_date"], {"value": "2026-09-14", "quality": "valid", "source": None})

    def test_as_of_is_required_and_must_be_a_date(self):
        with self.assertRaises(TypeError):
            normalize_company(full_item())
        with self.assertRaises(TypeError):
            normalize_company(full_item(), as_of=datetime(2026, 9, 15, 12, 0))

    def test_a_record_without_a_gemi_number_is_refused(self):
        record = full_item()
        del record["arGemi"]
        with self.assertRaises(ValueError):
            normalize(record)

    def test_every_validated_fixture_normalises(self):
        for record in (full_item(), SAMPLE, gemi_item(900000001000, date(2026, 9, 14)), {"arGemi": "1"}):
            with self.subTest(ar_gemi=record["arGemi"]):
                validate_response(ResponseFamily.COMPANY_DETAIL, record)
                self.assertIsInstance(normalize(record), NormalizedCompany)

    def test_the_module_is_pure_standard_library(self):
        tree = ast.parse(Path(inspect.getfile(normalizer_module)).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0] if node.level == 0 else f"relative:{node.module}")
        self.assertLessEqual(imported, {"__future__", "re", "unicodedata", "dataclasses", "datetime", "enum", "typing"})

    def test_the_current_importer_does_not_use_the_normaliser_yet(self):
        from . import services, tasks

        for module in (services, tasks):
            self.assertNotIn("normalize_company", inspect.getsource(module))
            self.assertNotIn("normalizer", inspect.getsource(module))
