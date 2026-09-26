"""Tests for pending-company hydration: create-only canonical Company rows for Discovery evidence that stays
``pending_no_company``, behind a dedicated flag that is off by default, never scheduled, through the shared
GemiClient and rate budget, feeding the unchanged NEW_COMPANY materialiser (always SHADOW)."""

import urllib.parse
from datetime import date, timedelta
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from . import apps as gemi_apps
from .company_signals import LIVE, SHADOW
from .ingestion.client import GemiTransportError
from .ingestion.discovery import INVALID_DATE, KNOWN, LATE_PUBLICATION, NEW_INCORPORATION
from .ingestion.rate_budget import GemiLane
from .models import (
    ActivityCode, Company, CompanyActivity, CompanySignal, CompanySignalDiscoveryEvidence, CompanySnapshot,
    GemiDiscoveryCursor, GemiDiscoveryObservation, GemiDiscoveryRun, Opportunity, RadarMatch, UserCompanyLead,
)
from .new_company_signals import materialize_new_company_signals
from .pending_company_hydration import (
    HYDRATION_LANE, MAX_LIMIT, HydrationDisabled, hydrate_pending_companies, pending_numbers_queryset,
)
from .services import company_defaults, sync_company_activities
from .test_gemi_client import NoNetworkMixin, make_client, response
from .test_gemi_validation import CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL, full_item, page
from .test_new_company_signals import FIRST_RUN_AT, LATER_RUN_AT, discovery_run, observation

ENABLED = override_settings(GEMI_DISCOVERY_PENDING_COMPANY_HYDRATION_ENABLED=True)
A, B, C = "118717203000", "118717204000", "118717205000"


def search_client(records, *, max_attempts=1, on_request=None):
    """A GemiClient whose /companies?arGemi=<n> returns ``records[n]`` (a record, a payload dict or a
    RawResponse) and an empty page for anything else."""
    requests = []

    def route(url):
        number = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get("arGemi")
        requests.append(number)
        if on_request is not None:
            on_request(number)
        outcome = records.get(number)
        if outcome is None:
            return response(200, page())
        if isinstance(outcome, dict) and "arGemi" in outcome:
            return response(200, page(outcome))
        if isinstance(outcome, dict):
            return response(200, outcome)
        return outcome

    client, transport, clock, budget = make_client(route, max_attempts=max_attempts)
    return client, requests, budget, transport


def world():
    """Every table the hydration or anything downstream could touch."""
    return {
        model.__name__: list(model.objects.order_by("pk").values())
        for model in (Company, CompanyActivity, ActivityCode, CompanySignal, CompanySnapshot, Opportunity,
                      RadarMatch, UserCompanyLead, GemiDiscoveryObservation, GemiDiscoveryRun, GemiDiscoveryCursor)
    }


class HydrationTestCase(NoNetworkMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.run = discovery_run(FIRST_RUN_AT)

    def pend(self, number, classification=NEW_INCORPORATION, run=None, incorporation=date(2026, 9, 9), quality="valid"):
        return observation(run or self.run, number, classification=classification,
                           incorporation_date=incorporation, quality=quality)

    def hydrate(self, client, **kwargs):
        kwargs.setdefault("pace_seconds", 0)
        with ENABLED:
            return hydrate_pending_companies(client=client, **kwargs)


class FlagOffTests(HydrationTestCase):
    def test_the_flag_is_off_by_default_and_not_scheduled(self):
        self.assertFalse(settings.GEMI_DISCOVERY_PENDING_COMPANY_HYDRATION_ENABLED)
        scheduled = " ".join(str(entry.get("func", "")) for entry in gemi_apps.SCHEDULES)
        self.assertNotIn("hydrat", scheduled)

    def test_the_command_refuses_before_any_request_or_write(self):
        self.pend(A)
        before = world()
        with patch("gemiapp.pending_company_hydration.get_gemi_client") as get_client:
            for args in ((), ("--dry-run",)):
                with self.assertRaises(CommandError) as caught:
                    call_command("hydrate_pending_discovery_companies", *args, stdout=StringIO())
                self.assertIn("GEMI_DISCOVERY_PENDING_COMPANY_HYDRATION_ENABLED", str(caught.exception))
        get_client.assert_not_called()
        self.assertEqual(world(), before)

    def test_the_function_refuses_even_with_an_injected_client(self):
        self.pend(A)
        client, requests, _, _ = search_client({A: full_item(A)})
        with self.assertRaises(HydrationDisabled):
            hydrate_pending_companies(client=client, pace_seconds=0)
        self.assertEqual((requests, Company.objects.count()), ([], 0))


class CreateTests(HydrationTestCase):
    def test_one_pending_observation_becomes_one_canonical_company(self):
        self.pend(A)
        item = full_item(A)
        client, requests, budget, transport = search_client({A: item})

        report = self.hydrate(client)

        self.assertEqual((report.selected, report.gemi_requests, report.fetched, report.created, report.failed),
                         (1, 1, 1, 1, 0))
        self.assertEqual(transport.queries(), [{"arGemi": A}])
        self.assertEqual(budget.lanes, [GemiLane.MONITORED_REFRESH])
        company = Company.objects.get(gemi_number=A)
        expected = company_defaults(item)
        for name in ("name", "vat_number", "legal_type", "prefecture", "municipality", "city", "incorporation_date",
                     "is_active", "activities", "raw_data"):
            self.assertEqual(getattr(company, name), expected[name], name)
        self.assertEqual(report.pending_after, 0)
        # Nothing downstream: no signal, snapshot, opportunity, legacy match or lead, no email.
        self.assertEqual((CompanySignal.objects.count(), CompanySnapshot.objects.count(), Opportunity.objects.count(),
                          RadarMatch.objects.count(), UserCompanyLead.objects.count(), len(mail.outbox)),
                         (0, 0, 0, 0, 0, 0))

    def test_activities_are_exactly_what_the_legacy_importer_writes(self):
        self.pend(A)
        client, _, _, _ = search_client({A: full_item(A)})
        self.hydrate(client)
        # The same record through the legacy importer's own two lines, under another GEMI number.
        legacy_item = full_item(B)
        legacy, _ = Company.objects.update_or_create(gemi_number=B, defaults=company_defaults(legacy_item))
        sync_company_activities(legacy, legacy_item.get("activities"))

        fields = ("code", "description", "activity_type", "activity_type_normalized", "kad_version", "date_from",
                  "date_to", "is_current", "source_key", "in_latest_source", "legacy_listed")

        def rows(number):
            return list(CompanyActivity.objects.filter(company__gemi_number=number).order_by("source_key").values(*fields))

        self.assertTrue(rows(A))
        self.assertEqual(rows(A), rows(B))

    def test_late_publications_and_invalid_dates_are_hydrated_and_their_legacy_exposure_is_reported(self):
        self.pend(A, LATE_PUBLICATION, incorporation=date(2025, 12, 15))
        self.pend(B, INVALID_DATE, incorporation=None, quality="missing")
        client, _, _, _ = search_client({A: full_item(A, day="2025-12-15"),
                                         B: full_item(B, incorporationDate=None)})
        report = self.hydrate(client)
        self.assertEqual(report.created, 2)
        self.assertEqual(Company.objects.get(gemi_number=A).incorporation_date, date(2025, 12, 15))
        # company_defaults, reused unchanged, stores a missing source date as today: reported, not hidden.
        self.assertEqual(Company.objects.get(gemi_number=B).incorporation_date, date.today())
        self.assertEqual((report.stored_as_today, report.date_clamped), (1, 1))

    def test_no_personal_data_or_payload_is_printed(self):
        self.pend(A)
        client, _, _, _ = search_client({A: full_item(A)})
        with self.assertLogs("gemiapp", level="INFO") as logs:
            report = self.hydrate(client)
        text = "\n".join(report.lines() + logs.output)
        for sentinel in (CONTACT_SENTINEL, PERSON_SENTINEL, PHONE_SENTINEL, "ΔΟΚΙΜΑΣΤΙΚΗ", "099999999"):
            self.assertNotIn(sentinel, text)


class CreateOnlyTests(HydrationTestCase):
    def test_an_existing_company_is_never_selected_fetched_or_changed(self):
        self.pend(A)
        existing = Company.objects.create(gemi_number=A, name="LEGACY ΑΕ", incorporation_date=date(2026, 9, 1),
                                          raw_data={"legacy": True})
        before = world()
        client, requests, _, _ = search_client({A: full_item(A)})
        report = self.hydrate(client)
        self.assertEqual((report.pending_before, report.selected, requests), (0, 0, []))
        self.assertEqual(world(), before)
        existing.refresh_from_db()
        self.assertEqual((existing.name, existing.raw_data), ("LEGACY ΑΕ", {"legacy": True}))

    def test_a_company_that_appears_after_selection_is_skipped_without_a_request(self):
        self.pend(A)
        stale = list(pending_numbers_queryset())                    # selected while still pending
        Company.objects.create(gemi_number=A, name="LEGACY ΑΕ", incorporation_date=date(2026, 9, 1))
        client, requests, _, _ = search_client({A: full_item(A)})
        with patch("gemiapp.pending_company_hydration.pending_numbers_queryset",
                   side_effect=[_Rows(stale), _Rows([])]):
            report = self.hydrate(client)
        self.assertEqual((report.already_local, report.gemi_requests, requests), (1, 0, []))
        self.assertEqual(Company.objects.get(gemi_number=A).name, "LEGACY ΑΕ")

    def test_a_company_written_between_fetch_and_write_is_not_overwritten(self):
        self.pend(A)

        def legacy_importer_wins(number):
            Company.objects.create(gemi_number=number, name="LEGACY ΑΕ", incorporation_date=date(2026, 9, 1))

        client, _, _, _ = search_client({A: full_item(A)}, on_request=legacy_importer_wins)
        report = self.hydrate(client)
        self.assertEqual((report.race_skipped, report.created, report.failed), (1, 0, 0))
        self.assertEqual(Company.objects.filter(gemi_number=A).count(), 1)
        self.assertEqual(Company.objects.get(gemi_number=A).name, "LEGACY ΑΕ")
        self.assertFalse(CompanyActivity.objects.exists())

    def test_losing_the_insert_race_is_a_skip_not_a_failure_or_an_overwrite(self):
        self.pend(A)

        def another_writer_inserts(number):
            Company.objects.bulk_create([Company(gemi_number=number, name="LEGACY ΑΕ",
                                                 incorporation_date=date(2026, 9, 1))])

        client, _, _, _ = search_client({A: full_item(A)}, on_request=another_writer_inserts)
        real_filter = Company.objects.filter
        lookups = {"n": 0}

        def blind_to_the_other_writer(*args, **kwargs):
            # Both existence checks -- before the fetch and inside the savepoint -- miss the concurrent row,
            # as they would under PostgreSQL before the other transaction commits.
            if kwargs.get("gemi_number") == A:
                lookups["n"] += 1
                if lookups["n"] <= 2:
                    return Company.objects.none()
            return real_filter(*args, **kwargs)

        with patch.object(Company.objects, "filter", side_effect=blind_to_the_other_writer):
            report = self.hydrate(client)
        # The unique gemi_number rejects the insert, and the row that won stays exactly as it was.
        self.assertEqual((report.race_skipped, report.created, report.write_failures), (1, 0, 0))
        self.assertEqual(Company.objects.get(gemi_number=A).name, "LEGACY ΑΕ")
        self.assertFalse(CompanyActivity.objects.exists())


class _Rows(list):
    """Stands in for the pending queryset in the stale-selection test."""

    def count(self):
        return len(self)


class DeduplicationAndIdempotencyTests(HydrationTestCase):
    def test_many_observations_of_one_company_mean_one_request_and_one_row(self):
        self.pend(A)
        later = discovery_run(LATER_RUN_AT)
        self.pend(A, run=later)
        self.pend(A, LATE_PUBLICATION, run=discovery_run(LATER_RUN_AT + timedelta(days=1)))
        client, requests, _, _ = search_client({A: full_item(A)})
        report = self.hydrate(client)
        self.assertEqual((report.pending_observations, report.pending_before, report.selected), (3, 1, 1))
        self.assertEqual((requests, report.created), ([A], 1))
        self.assertEqual(Company.objects.filter(gemi_number=A).count(), 1)

    def test_known_observations_are_never_candidates(self):
        observation(self.run, A, classification=KNOWN)
        client, requests, _, _ = search_client({A: full_item(A)})
        self.assertEqual((self.hydrate(client).selected, requests), (0, []))

    def test_a_second_run_changes_nothing_and_asks_gemi_for_nothing(self):
        self.pend(A)
        client, requests, _, _ = search_client({A: full_item(A)})
        self.hydrate(client)
        after_first = world()
        report = self.hydrate(client)
        self.assertEqual((report.pending_before, report.selected, report.created, len(requests)), (0, 0, 0, 1))
        self.assertEqual(world(), after_first)

    def test_oldest_evidence_first_and_the_limit_bounds_the_run(self):
        self.pend(C, run=discovery_run(LATER_RUN_AT))
        self.pend(B)
        self.pend(A, run=discovery_run(FIRST_RUN_AT - timedelta(days=1)))
        client, requests, _, _ = search_client({n: full_item(n) for n in (A, B, C)})
        report = self.hydrate(client, limit=2)
        self.assertEqual(requests, [A, B])
        self.assertEqual((report.created, report.pending_after), (2, 1))
        with self.assertRaises(ValueError):
            self.hydrate(client, limit=MAX_LIMIT + 1)


class FailureTests(HydrationTestCase):
    def test_an_invalid_response_leaves_no_row_is_reported_and_the_batch_continues(self):
        self.pend(A)
        self.pend(B, run=discovery_run(LATER_RUN_AT))
        broken = full_item(A, activities="not a list")
        client, _, _, _ = search_client({A: broken, B: full_item(B)})
        report = self.hydrate(client)
        self.assertEqual((report.validation_failures, report.created, report.failed), (1, 1, 1))
        self.assertEqual(report.failed_gemi_numbers, [A])
        self.assertFalse(Company.objects.filter(gemi_number=A).exists())
        self.assertTrue(Company.objects.filter(gemi_number=B).exists())

    def test_no_match_or_another_company_is_never_stored(self):
        self.pend(A)
        self.pend(B)
        client, _, _, _ = search_client({A: page(), B: full_item(C)})   # empty page; a different company
        report = self.hydrate(client)
        self.assertEqual((report.not_found, report.created), (2, 0))
        self.assertFalse(Company.objects.exists())

    def test_a_network_failure_stops_the_batch_without_any_write(self):
        self.pend(A)
        self.pend(B)
        client, requests, _, _ = search_client({A: GemiTransportError("ConnectionError", timed_out=False)})
        before = world()
        report = self.hydrate(client)
        self.assertTrue(report.aborted)
        self.assertEqual((report.abort_reason, requests), ("GemiRetryExhaustedError", [A]))
        self.assertEqual(world(), before)

    def test_a_write_failure_rolls_back_the_company_with_its_activities(self):
        self.pend(A)
        client, _, _, _ = search_client({A: full_item(A)})
        with patch("gemiapp.services.sync_canonical_company_activities", side_effect=RuntimeError("boom")):
            report = self.hydrate(client)
        self.assertEqual((report.write_failures, report.created), (1, 0))
        self.assertFalse(Company.objects.exists())
        self.assertFalse(CompanyActivity.objects.exists())

    @ENABLED
    def test_the_command_exits_non_zero_on_failure_and_names_the_problem(self):
        self.pend(A)
        client, _, _, _ = search_client({A: full_item(A, activities="not a list")})
        out = StringIO()
        with patch("gemiapp.pending_company_hydration.get_gemi_client", return_value=client):
            with self.assertRaises(CommandError):
                call_command("hydrate_pending_discovery_companies", "--pace-seconds", "0", stdout=out)
        self.assertIn("validation=1", out.getvalue())


class DryRunTests(HydrationTestCase):
    def test_a_dry_run_fetches_but_mutates_nothing(self):
        self.pend(A)
        self.pend(B, INVALID_DATE, incorporation=None, quality="missing")
        client, requests, _, _ = search_client({A: full_item(A), B: full_item(B, incorporationDate=None)})
        before = world()
        report = self.hydrate(client, dry_run=True)
        self.assertEqual(world(), before)
        self.assertEqual((report.would_create, report.created, requests), (2, 0, [A, B]))
        self.assertEqual((report.stored_as_today, report.date_clamped, report.pending_after), (1, 1, 2))
        self.assertTrue(all(line.startswith("[dry-run]") for line in report.lines()))

    @ENABLED
    def test_the_command_dry_run(self):
        self.pend(A)
        client, _, _, _ = search_client({A: full_item(A)})
        out = StringIO()
        with patch("gemiapp.pending_company_hydration.get_gemi_client", return_value=client):
            call_command("hydrate_pending_discovery_companies", "--dry-run", "--pace-seconds", "0", stdout=out)
        self.assertIn("would create=1", out.getvalue())
        self.assertFalse(Company.objects.exists())


class BudgetTests(HydrationTestCase):
    def test_each_fetch_takes_one_slot_in_the_refresh_lane_and_runs_are_paced(self):
        self.assertEqual(HYDRATION_LANE, GemiLane.MONITORED_REFRESH)
        self.pend(A)
        self.pend(B)
        self.pend(C)
        Company.objects.create(gemi_number=B, name="LEGACY", incorporation_date=date(2026, 9, 1))
        client, requests, budget, _ = search_client({A: full_item(A), C: full_item(C)})
        sleeps = []
        with patch("gemiapp.pending_company_hydration.pending_numbers_queryset",
                   side_effect=[_Rows([{"gemi_number": n} for n in (A, B, C)]), _Rows([])]):
            report = self.hydrate(client, pace_seconds=20, sleep=sleeps.append)
        self.assertEqual(requests, [A, C])                         # no request for the already-local company
        self.assertEqual(budget.lanes, [GemiLane.MONITORED_REFRESH] * 2)
        self.assertEqual(sleeps, [20])                              # between fetches only, never before the first
        self.assertEqual((report.gemi_requests, report.already_local), (2, 1))

    def test_the_command_defaults_are_conservative(self):
        from .management.commands.hydrate_pending_discovery_companies import Command

        parser = Command().create_parser("manage.py", "hydrate_pending_discovery_companies")
        defaults = vars(parser.parse_args([]))
        self.assertEqual((defaults["limit"], defaults["pace_seconds"], defaults["dry_run"]), (20, 20.0, False))
        options = {action.dest for action in parser._actions}
        self.assertFalse({"live", "mode", "promote"} & options)


class FullChainTests(HydrationTestCase):
    def test_pending_evidence_to_a_shadow_signal_with_the_original_discovery_evidence(self):
        first = self.pend(A)
        self.pend(A, run=discovery_run(LATER_RUN_AT))
        self.assertEqual(materialize_new_company_signals().unmaterialised_no_company, 1)

        client, _, _, _ = search_client({A: full_item(A)})
        self.assertEqual(self.hydrate(client).created, 1)
        self.assertFalse(CompanySignal.objects.exists())            # hydration itself produces no signal

        with self.captureOnCommitCallbacks(execute=True):
            report = materialize_new_company_signals()
        self.assertEqual((report.signals_created, report.unmaterialised_no_company, report.baselines_created),
                         (1, 0, 1))
        signal = CompanySignal.objects.get(company__gemi_number=A)
        company = Company.objects.get(gemi_number=A)
        self.assertEqual(signal.mode, SHADOW)
        evidence = CompanySignalDiscoveryEvidence.objects.get(signal=signal).discovery_observation
        self.assertEqual(evidence.pk, first.pk)                       # the earliest evidence, untouched
        self.assertEqual(evidence.run.started_at, FIRST_RUN_AT)
        # The unchanged B2 rule: the first moment both the evidence and a canonical state existed.
        self.assertEqual(signal.detected_at, max(FIRST_RUN_AT, company.updated_at))
        self.assertFalse(CompanySignal.objects.filter(mode=LIVE).exists())
        self.assertFalse(Opportunity.objects.filter(latest_signal__mode=LIVE).exists())

    def test_hydration_never_writes_discovery_state(self):
        self.pend(A)
        cursor = GemiDiscoveryCursor.objects.create(stream="companies_by_gemi_number", status="ready",
                                                    high_water_mark="118717200000",
                                                    high_water_mark_value=118717200000)
        before = (list(GemiDiscoveryObservation.objects.values()), list(GemiDiscoveryRun.objects.values()),
                  list(GemiDiscoveryCursor.objects.values()))
        client, _, _, _ = search_client({A: full_item(A)})
        self.hydrate(client)
        self.assertEqual((list(GemiDiscoveryObservation.objects.values()), list(GemiDiscoveryRun.objects.values()),
                          list(GemiDiscoveryCursor.objects.values())), before)
        cursor.refresh_from_db()
        self.assertEqual(cursor.high_water_mark, "118717200000")
