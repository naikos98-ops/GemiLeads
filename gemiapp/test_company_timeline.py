"""Tests for the company timeline read model (B6, gemiapp.company_timeline).

Fixture history is produced through the real B2 and B5 producers wherever possible; malformed and future
rows are written directly only to prove the integrity policy. B6 itself never writes.
"""

import dataclasses
import inspect
import json
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from . import company_timeline
from .company_signals import (
    DOCUMENT_METADATA,
    KAD_ADDED,
    LEGAL_FORM_CHANGED,
    LIVE,
    LOCATION_CHANGED,
    NEW_COMPANY,
    PRECISION_DATE,
    PRECISION_NONE,
    SHADOW,
    STATUS_CHANGED,
    promote_company_signal,
)
from .company_timeline import (
    ALL_MODES,
    COMPLETE,
    DEFAULT_LIMIT,
    INVALID,
    MAX_LIMIT,
    MISSING,
    UNSUPPORTED,
    CompanyTimelineEntry,
    TimelineCursor,
    TimelineError,
    get_company_timeline,
)
from .ingestion.discovery import LATE_PUBLICATION
from .models import (
    Company, CompanyActivity, CompanyMonitoring, CompanySignal, CompanySignalDiscoveryEvidence,
    CompanySignalSnapshotEvidence, CompanySnapshot, CustomerRadar, RadarMatch, UserCompanyLead, UserSubscription,
)
from .new_company_signals import produce_new_company_signal
from .snapshot_change_signals import materialize_snapshot_change_signals
from .test_new_company_signals import discovery_run, observation
from .test_snapshot_change_signals import GEMI, History, act
from .test_gemi_validation import CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL

DISCOVERED = datetime(2026, 9, 2, 8, 0, tzinfo=dt_timezone.utc)


def make_company(gemi_number=GEMI):
    return Company.objects.create(
        gemi_number=gemi_number, name=f"{PERSON_SENTINEL} ΙΚΕ", incorporation_date=date(2026, 6, 1),
        prefecture="ΑΤΤΙΚΗΣ", email=CONTACT_SENTINEL, address="ΟΔΟΣ ΔΟΚΙΜΗΣ 6", vat_number="099999999",
        raw_data={"persons": [{"personName": PERSON_SENTINEL}], "phone": PHONE_SENTINEL, "email": CONTACT_SENTINEL},
    )


def new_company_signal(company, *, discovered=DISCOVERED, classification=LATE_PUBLICATION,
                       incorporated=date(2026, 6, 1)):
    evidence = observation(discovery_run(discovered), gemi_number=company.gemi_number,
                           classification=classification, incorporation_date=incorporated)
    result = produce_new_company_signal(evidence)
    return CompanySignal.objects.get(pk=result.signal_id)


def raw_signal(company, *, signal_type, source_type, detected_at, key, mode=SHADOW):
    """A row written without a producer: only for malformed-history and future-source fixtures."""
    return CompanySignal.objects.create(
        company=company, signal_type=signal_type, source_type=source_type, rule_version="future:v1",
        dedupe_key=key.ljust(64, "0"), confidence=Decimal("0.5000"), mode=mode, effective_precision="none",
        detected_at=detected_at,
    )


def timeline(company, **kwargs):
    return get_company_timeline(company, **kwargs)


class TimelineTestCase(TestCase):
    def setUp(self):
        self.company = make_company()
        self.history = History(self.company)

    def ids(self, page):
        return [entry.signal_id for entry in page.entries]


# --- architecture and schema ---------------------------------------------------------------------

class ArchitectureTests(TestCase):
    def test_no_timeline_table_and_no_migration_exist(self):
        from django.apps import apps

        names = {model.__name__ for model in apps.get_app_config("gemiapp").get_models()}
        self.assertFalse({name for name in names if "Timeline" in name})

    def test_no_url_view_template_task_or_schedule(self):
        for path in ("gemiapp/urls.py", "gemiapp/views.py", "gemiapp/tasks.py", "gemiapp/apps.py", "config/urls.py"):
            self.assertNotIn("timeline", open(path, encoding="utf-8").read().lower())

    def test_the_service_never_calls_gemi_or_writes_raw_data(self):
        source = inspect.getsource(company_timeline)
        code = source.split('"""', 2)[2]
        for forbidden in ("get_gemi_client", "raw_data", "GemiSourceRecord", "select_related(\"company", ".save(",
                          "objects.create", "objects.update", ".delete(", "bulk_", "get_or_create",
                          "promote_company_signal", "record_company_signal"):
            self.assertNotIn(forbidden, code)

    def test_limits_are_central(self):
        self.assertEqual((DEFAULT_LIMIT, MAX_LIMIT), (50, 200))


class EntrySchemaTests(TimelineTestCase):
    def test_exact_fields_and_no_tenant_fields(self):
        names = [f.name for f in dataclasses.fields(CompanyTimelineEntry)]
        self.assertEqual(names, [
            "signal_id", "company_id", "gemi_number", "signal_type", "source_type", "mode", "confidence",
            "rule_version", "detected_at", "effective_precision", "effective_date", "effective_at", "group_key",
            "provenance_status", "subject_kind", "before_source_id", "after_source_id", "activity_code",
            "kad_version", "discovery_classification", "discovery_observation_id",
        ])
        for forbidden in ("user", "organization", "subscription", "radar", "customer", "opportunity", "name", "email"):
            self.assertFalse([n for n in names if forbidden in n], forbidden)

    def test_entries_are_frozen_and_editing_one_changes_nothing(self):
        signal = new_company_signal(self.company)
        entry = timeline(self.company).entries[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            entry.mode = LIVE
        with self.assertRaises(dataclasses.FrozenInstanceError):
            entry.signal_type = STATUS_CHANGED
        signal.refresh_from_db()
        self.assertEqual(signal.mode, SHADOW)


# --- scenarios -------------------------------------------------------------------------------------

class ScenarioTests(TimelineTestCase):
    def test_scenario_1_new_company_only(self):
        signal = new_company_signal(self.company)
        page = timeline(self.company)
        self.assertEqual(len(page.entries), 1)
        entry = page.entries[0]
        self.assertEqual((entry.signal_id, entry.signal_type, entry.source_type, entry.mode),
                         (signal.pk, NEW_COMPANY, "discovery", SHADOW))
        self.assertEqual((entry.provenance_status, entry.discovery_classification, entry.group_key),
                         (COMPLETE, LATE_PUBLICATION, f"discovery:{signal.pk}"))
        self.assertEqual(entry.discovery_observation_id, signal.discovery_evidence.discovery_observation_id)
        self.assertEqual((entry.subject_kind, entry.before_source_id, entry.activity_code), (None, None, None))
        self.assertEqual((entry.gemi_number, entry.company_id), (GEMI, self.company.pk))
        self.assertIsNone(page.next_cursor)

    def test_scenario_2_a_later_status_change_comes_first(self):
        new_company = new_company_signal(self.company)
        self.history.add()
        b = self.history.add(status_source_id="8", last_status_change="2026-09-01")
        materialize_snapshot_change_signals(b)
        entries = timeline(self.company).entries
        self.assertEqual([e.signal_type for e in entries], [STATUS_CHANGED, NEW_COMPANY])
        status = entries[0]
        self.assertEqual((status.subject_kind, status.before_source_id, status.after_source_id),
                         ("status", "3", "8"))
        self.assertEqual((status.effective_precision, status.effective_date), (PRECISION_DATE, date(2026, 9, 1)))
        self.assertEqual(status.detected_at, b.observed_at)
        self.assertEqual(entries[1].signal_id, new_company.pk)

    def test_scenario_3_one_transition_is_three_entries_with_one_group_key(self):
        self.history.add()
        b = self.history.add(status_source_id="8", legal_type_source_id="20",
                             current_activities=[act("47191002"), act("62010000", dt_from="2026-08-01")])
        materialize_snapshot_change_signals(b)
        entries = timeline(self.company).entries
        self.assertEqual(sorted(e.signal_type for e in entries), sorted([STATUS_CHANGED, LEGAL_FORM_CHANGED, KAD_ADDED]))
        self.assertEqual({e.group_key for e in entries}, {f"snapshot:{b.pk}"})
        self.assertEqual({e.provenance_status for e in entries}, {COMPLETE})
        by_type = {e.signal_type: e for e in entries}
        self.assertEqual((by_type[KAD_ADDED].subject_kind, by_type[KAD_ADDED].activity_code, by_type[KAD_ADDED].kad_version,
                          by_type[KAD_ADDED].before_source_id), ("kad", "62010000", "kad_2026", None))
        self.assertEqual((by_type[LEGAL_FORM_CHANGED].subject_kind, by_type[LEGAL_FORM_CHANGED].after_source_id),
                         ("legal_form", "20"))

    def test_scenario_4_a_later_transition_is_a_new_group_and_a_newer_moment(self):
        self.history.add()
        b = self.history.add(status_source_id="8", legal_type_source_id="20")
        materialize_snapshot_change_signals(b)
        c = self.history.add(status_source_id="8", legal_type_source_id="20", municipality_source_id="61191")
        materialize_snapshot_change_signals(c)
        entries = timeline(self.company).entries
        self.assertEqual(entries[0].signal_type, LOCATION_CHANGED)
        self.assertEqual((entries[0].subject_kind, entries[0].after_source_id), ("municipality", "61191"))
        self.assertEqual(entries[0].group_key, f"snapshot:{c.pk}")
        self.assertEqual({e.group_key for e in entries[1:]}, {f"snapshot:{b.pk}"})
        self.assertGreater(entries[0].detected_at, entries[1].detected_at)

    def test_kad_removal_is_exposed_with_its_identity(self):
        self.history.add(current_activities=[act("47191002"), act("56100000", version=None, dt_to="2026-12-31", to_q="valid")])
        b = self.history.add(current_activities=[act("47191002")])
        materialize_snapshot_change_signals(b)
        entry = timeline(self.company).entries[0]
        self.assertEqual((entry.signal_type, entry.subject_kind, entry.activity_code, entry.kad_version),
                         ("kad_removed", "kad", "56100000", None))

    def test_scenario_5_an_older_effective_date_never_moves_an_entry_back(self):
        # Discovered after the status change was detected, but incorporated months before it.
        self.history.add(observed_at=datetime(2026, 9, 1, 9, tzinfo=dt_timezone.utc))
        b = self.history.add(status_source_id="8", last_status_change="2026-08-31",
                             observed_at=datetime(2026, 9, 3, 9, tzinfo=dt_timezone.utc))
        materialize_snapshot_change_signals(b)
        new_company_signal(self.company, discovered=datetime(2026, 9, 5, 9, tzinfo=dt_timezone.utc),
                           incorporated=date(2026, 3, 15))
        entries = timeline(self.company).entries
        self.assertEqual([e.signal_type for e in entries], [NEW_COMPANY, STATUS_CHANGED])
        self.assertEqual((entries[0].effective_precision, entries[0].effective_date), (PRECISION_DATE, date(2026, 3, 15)))
        self.assertIsNone(entries[0].effective_at)  # a date never becomes a midnight timestamp
        self.assertLess(entries[0].effective_date, entries[1].effective_date)

    def test_scenario_6_tied_detection_times_break_by_id_descending(self):
        self.history.add()
        b = self.history.add(status_source_id="8", legal_type_source_id="20", municipality_source_id="61191",
                             current_activities=[act("47191002"), act("62010000")])
        materialize_snapshot_change_signals(b)
        entries = timeline(self.company).entries
        self.assertEqual(len({e.detected_at for e in entries}), 1)
        self.assertEqual([e.signal_id for e in entries], sorted((e.signal_id for e in entries), reverse=True))
        self.assertEqual([e.signal_id for e in entries],
                         list(CompanySignal.objects.order_by("-detected_at", "-id").values_list("id", flat=True)))

    def test_unusable_effective_times_stay_empty(self):
        self.history.add()
        b = self.history.add(legal_type_source_id="20")
        materialize_snapshot_change_signals(b)
        entry = timeline(self.company).entries[0]
        self.assertEqual((entry.effective_precision, entry.effective_date, entry.effective_at), (PRECISION_NONE, None, None))


# --- provenance integrity ---------------------------------------------------------------------------

class ProvenanceTests(TimelineTestCase):
    def test_scenario_8a_new_company_without_discovery_evidence_is_visible_as_missing(self):
        signal = new_company_signal(self.company)
        CompanySignalDiscoveryEvidence.objects.filter(signal=signal).delete()
        entry = timeline(self.company).entries[0]
        self.assertEqual((entry.signal_id, entry.provenance_status, entry.group_key),
                         (signal.pk, MISSING, f"signal:{signal.pk}"))
        self.assertIsNone(entry.discovery_classification)

    def test_scenario_8b_status_change_without_snapshot_evidence_is_visible_as_missing(self):
        self.history.add()
        b = self.history.add(status_source_id="8")
        materialize_snapshot_change_signals(b)
        CompanySignalSnapshotEvidence.objects.all().delete()
        entry = timeline(self.company).entries[0]
        self.assertEqual((entry.signal_type, entry.provenance_status), (STATUS_CHANGED, MISSING))
        self.assertEqual((entry.subject_kind, entry.before_source_id, entry.after_source_id), (None, None, None))

    def test_evidence_that_contradicts_its_signal_is_invalid_and_exposes_no_subject(self):
        self.history.add()
        b = self.history.add(status_source_id="8")
        materialize_snapshot_change_signals(b)
        evidence = CompanySignalSnapshotEvidence.objects.get()
        # Subject of the wrong kind for the signal type.
        CompanySignalSnapshotEvidence.objects.filter(pk=evidence.pk).update(subject_kind="legal_form")
        entry = timeline(self.company).entries[0]
        self.assertEqual((entry.provenance_status, entry.subject_kind, entry.group_key),
                         (INVALID, None, f"signal:{entry.signal_id}"))
        # Snapshots belonging to another company.
        CompanySignalSnapshotEvidence.objects.filter(pk=evidence.pk).update(subject_kind="status")
        other = History(make_company("300000000001"))
        x, y = other.add(), other.add(city="ΑΛΛΗ")
        CompanySignalSnapshotEvidence.objects.filter(pk=evidence.pk).update(previous_snapshot=x, current_snapshot=y)
        self.assertEqual(timeline(self.company).entries[0].provenance_status, INVALID)

    def test_discovery_evidence_for_another_company_is_invalid(self):
        signal = new_company_signal(self.company)
        CompanySignalDiscoveryEvidence.objects.filter(signal=signal).update(
            discovery_observation=observation(discovery_run(DISCOVERED + timedelta(days=1)), gemi_number="777"),
        )
        entry = timeline(self.company).entries[0]
        self.assertEqual((entry.provenance_status, entry.discovery_classification), (INVALID, None))

    def test_scenario_9_an_unsupported_future_signal_is_a_generic_entry(self):
        future = raw_signal(self.company, signal_type="capital_increase", source_type=DOCUMENT_METADATA,
                            detected_at=DISCOVERED, key="future")
        mismatched = raw_signal(self.company, signal_type=STATUS_CHANGED, source_type=DOCUMENT_METADATA,
                                detected_at=DISCOVERED - timedelta(days=1), key="mismatch")
        entries = timeline(self.company).entries
        self.assertEqual([(e.signal_id, e.provenance_status) for e in entries],
                         [(future.pk, UNSUPPORTED), (mismatched.pk, UNSUPPORTED)])
        self.assertEqual((entries[0].group_key, entries[0].subject_kind, entries[0].confidence),
                         (f"signal:{future.pk}", None, Decimal("0.5000")))


# --- mode ------------------------------------------------------------------------------------------

class ModeTests(TimelineTestCase):
    def setUp(self):
        super().setUp()
        self.shadow = new_company_signal(self.company)
        self.history.add()
        b = self.history.add(status_source_id="8")
        materialize_snapshot_change_signals(b)
        self.live = CompanySignal.objects.get(signal_type=STATUS_CHANGED)
        promote_company_signal(self.live)  # a test fixture only: no production promotion exists

    def test_the_default_reads_shadow_only(self):
        self.assertEqual(self.ids(timeline(self.company)), [self.shadow.pk])

    def test_live_reads_live_only(self):
        self.assertEqual(self.ids(timeline(self.company, mode=LIVE)), [self.live.pk])

    def test_all_modes_must_be_asked_for_explicitly(self):
        self.assertEqual(self.ids(timeline(self.company, mode=ALL_MODES)), [self.live.pk, self.shadow.pk])
        for bad in (None, "", "both", "SHADOW"):
            with self.assertRaises(TimelineError):
                timeline(self.company, mode=bad)

    def test_signal_type_filtering(self):
        page = timeline(self.company, mode=ALL_MODES, signal_types=[STATUS_CHANGED])
        self.assertEqual(self.ids(page), [self.live.pk])
        for bad in ([], ["invented"]):
            with self.assertRaises(TimelineError):
                timeline(self.company, signal_types=bad)


# --- pagination ------------------------------------------------------------------------------------

class HistoryMixin:
    def build(self):
        new_company_signal(self.company)
        self.history.add()
        # Three transitions producing 4, 2 and 3 signals: several share an exact detected_at.
        b = self.history.add(status_source_id="8", legal_type_source_id="20", municipality_source_id="61191",
                             current_activities=[act("47191002"), act("62010000")])
        c = self.history.add(status_source_id="9", legal_type_source_id="21", municipality_source_id="61191",
                             current_activities=[act("47191002"), act("62010000")])
        d = self.history.add(status_source_id="9", legal_type_source_id="21", municipality_source_id="61192",
                             current_activities=[act("62010000"), act("70220000")])
        for snapshot in (b, c, d):
            materialize_snapshot_change_signals(snapshot)
        return list(CompanySignal.objects.order_by("-detected_at", "-id").values_list("id", flat=True))


class PaginationTests(HistoryMixin, TimelineTestCase):

    def walk(self, limit, token=False):
        seen, cursor = [], None
        while True:
            before = cursor.encode() if (token and cursor) else cursor
            page = timeline(self.company, limit=limit, before=before)
            seen.extend(self.ids(page))
            cursor = page.next_cursor
            if cursor is None:
                return seen

    def test_scenario_7_pages_concatenate_to_the_unpaginated_order_without_duplicates_or_gaps(self):
        expected = self.build()
        self.assertEqual(len(expected), 10)
        self.assertEqual(self.ids(timeline(self.company, limit=MAX_LIMIT)), expected)
        for limit in (1, 2, 3, 4, 7, 9, 10):
            with self.subTest(limit=limit):
                self.assertEqual(self.walk(limit), expected)
                self.assertEqual(self.walk(limit, token=True), expected)

    def test_a_cursor_splitting_a_tie_neither_skips_nor_repeats(self):
        expected = self.build()
        first = timeline(self.company, limit=2)
        self.assertEqual(first.entries[0].detected_at, first.entries[1].detected_at)
        second = timeline(self.company, limit=2, before=first.next_cursor)
        self.assertEqual(self.ids(first) + self.ids(second), expected[:4])
        self.assertEqual(second.entries[0].detected_at, first.entries[1].detected_at)

    def test_a_newer_signal_after_page_one_does_not_disturb_older_pages(self):
        expected = self.build()
        first = timeline(self.company, limit=4)
        e = self.history.add(status_source_id="1", legal_type_source_id="21", municipality_source_id="61192",
                             current_activities=[act("62010000"), act("70220000")])
        materialize_snapshot_change_signals(e)
        rest = self.walk_from(first.next_cursor, limit=3)
        self.assertEqual(self.ids(first) + rest, expected)
        self.assertEqual(timeline(self.company, limit=1).entries[0].detected_at, e.observed_at)

    def walk_from(self, cursor, *, limit):
        seen = []
        while cursor is not None:
            page = timeline(self.company, limit=limit, before=cursor)
            seen.extend(self.ids(page))
            cursor = page.next_cursor
        return seen

    def test_a_page_ending_exactly_at_the_last_entry_has_no_next_cursor(self):
        expected = self.build()
        self.assertIsNone(timeline(self.company, limit=len(expected)).next_cursor)
        self.assertIsNotNone(timeline(self.company, limit=len(expected) - 1).next_cursor)

    def test_invalid_limits_and_cursors_are_refused(self):
        for bad in (0, -1, MAX_LIMIT + 1, "10", 2.5, True, None):
            with self.subTest(limit=bad), self.assertRaises(TimelineError):
                timeline(self.company, limit=bad)
        import base64

        naive = base64.urlsafe_b64encode(b"2026-09-01T00:00:00|1").decode()
        zero_id = base64.urlsafe_b64encode(b"2026-09-01T00:00:00+00:00|0").decode()
        with self.assertRaises(TimelineError):
            TimelineCursor(datetime(2026, 9, 1), 1).encode()
        for bad in ("not-a-cursor", "", naive, zero_id, 42):
            with self.subTest(cursor=bad), self.assertRaises(TimelineError):
                timeline(self.company, before=bad)
        with self.assertRaises(TimelineError):
            timeline(Company(gemi_number="1"))

    def test_the_cursor_round_trips_and_carries_no_tenant_state(self):
        cursor = TimelineCursor(datetime(2026, 9, 3, 9, 0, 0, 123456, tzinfo=dt_timezone(timedelta(hours=3))), 17)
        decoded = TimelineCursor.decode(cursor.encode())
        self.assertEqual((decoded.detected_at, decoded.signal_id), (cursor.detected_at, 17))
        self.assertEqual([f.name for f in dataclasses.fields(TimelineCursor)], ["detected_at", "signal_id"])


# --- efficiency, read-only, privacy, parity -------------------------------------------------------

class QueryEfficiencyTests(HistoryMixin, TimelineTestCase):
    def test_a_mixed_timeline_costs_two_queries_whatever_its_size(self):
        self.build()
        with self.assertNumQueries(2):
            page = timeline(self.company, limit=MAX_LIMIT)
        self.assertEqual(len(page.entries), 10)
        with self.assertNumQueries(2):
            timeline(self.company, limit=3)
        new_only = make_company("400000000001")
        new_company_signal(new_only)
        with self.assertNumQueries(1):  # no snapshot evidence, no snapshot lookup
            timeline(new_only)

    def test_reading_performs_no_write_and_never_selects_company_or_raw_data(self):
        self.build()
        with CaptureQueriesContext(connection) as queries:
            timeline(self.company, limit=MAX_LIMIT, mode=ALL_MODES)
        for query in queries.captured_queries:
            sql = query["sql"].upper()
            self.assertTrue(sql.lstrip().startswith("SELECT"), sql)
            self.assertNotIn("RAW_DATA", sql)
            self.assertNotIn('"GEMIAPP_COMPANY"', sql)


class PrivacyTests(TimelineTestCase):
    def test_entries_their_repr_and_command_output_carry_no_personal_data(self):
        new_company_signal(self.company)
        self.history.add()
        b = self.history.add(status_source_id="8", current_activities=[act("47191002"), act("62010000")])
        materialize_snapshot_change_signals(b)
        entries = timeline(self.company).entries
        blob = repr(entries) + json.dumps([dataclasses.asdict(e) for e in entries], default=str, ensure_ascii=False)
        out = StringIO()
        call_command("show_company_timeline", GEMI, stdout=out)
        blob += out.getvalue()
        for sentinel in (PERSON_SENTINEL, CONTACT_SENTINEL, PHONE_SENTINEL, "099999999", "ΟΔΟΣ ΔΟΚΙΜΗΣ", "ΙΚΕ",
                         "ΚΗΦΙΣΙΑ", "14561", "Κύρια"):
            self.assertNotIn(sentinel, blob)
        self.assertIn("status=3->8", out.getvalue())
        self.assertIn("kad=62010000/kad_2026", out.getvalue())


class CommandTests(TimelineTestCase):
    def test_the_command_is_read_only_paginates_and_validates(self):
        self.history.add()
        b = self.history.add(status_source_id="8", legal_type_source_id="20")
        materialize_snapshot_change_signals(b)
        before = list(CompanySignal.objects.values())
        out = StringIO()
        call_command("show_company_timeline", GEMI, "--limit", "1", stdout=out)
        text = out.getvalue()
        self.assertIn("entries=1", text)
        token = text.strip().splitlines()[-1].split("next=")[1]
        out2 = StringIO()
        call_command("show_company_timeline", GEMI, "--limit", "1", "--before", token, stdout=out2)
        self.assertIn("entries=1", out2.getvalue())
        self.assertEqual(list(CompanySignal.objects.values()), before)
        for args in (("999",), (GEMI, "--limit", "0"), (GEMI, "--before", "junk")):
            with self.assertRaises(CommandError):
                call_command("show_company_timeline", *args, stdout=StringIO())


class CustomerParityTests(TimelineTestCase):
    def world(self):
        return {name: list(model.objects.values()) for name, model in (
            ("companies", Company), ("activities", CompanyActivity), ("radars", CustomerRadar),
            ("matches", RadarMatch), ("leads", UserCompanyLead), ("subscriptions", UserSubscription),
            ("monitoring", CompanyMonitoring), ("snapshots", CompanySnapshot), ("signals", CompanySignal),
            ("discovery_evidence", CompanySignalDiscoveryEvidence), ("snapshot_evidence", CompanySignalSnapshotEvidence),
        )}

    def test_reading_timelines_changes_nothing(self):
        new_company_signal(self.company)
        self.history.add()
        b = self.history.add(status_source_id="8")
        materialize_snapshot_change_signals(b)
        before = self.world()
        for mode in (SHADOW, LIVE, ALL_MODES):
            timeline(self.company, mode=mode)
        call_command("show_company_timeline", GEMI, stdout=StringIO())
        self.assertEqual(self.world(), before)
