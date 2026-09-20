"""G6: tests for the observability of real outbound ΓΕΜΗ attempts (gemiapp.ingestion.request_metrics).

The measurement has to be trustworthy in exactly the ways the G6 decision depends on: one row per attempt
that really reached the transport, retries counted separately, nothing counted when no request was sent, the
lane and outcome recorded faithfully, and the request itself never harmed by the measurement. The client
harness (fake clock, fake transport, in-memory budget store) is the one test_gemi_client already uses, so
nothing here reaches the network either.

Metrics are off for the suite by default (config/fast_test_runner), so every test here turns them on
explicitly -- which also documents the setting that production runs with.
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from io import StringIO

from .ingestion import GemiBudgetTimeoutError, GemiBudgetUnavailableError, GemiLane, GemiNotFoundError
from .ingestion.client import GemiTransportError
from .ingestion import request_metrics
from .ingestion.request_metrics import (
    BUDGET_TIMEOUT, BUDGET_UNAVAILABLE, CLIENT_ERROR, RATE_LIMITED, SERVER_ERROR, SUCCESS, TRANSPORT_ERROR,
    build_report, classify_status, normalise_endpoint,
)
from .models import GemiRequestAttempt
from .test_gemi_client import BrokenStore, NoNetworkMixin, SECRET, make_client, response

METRICS_ON = override_settings(GEMI_REQUEST_METRICS_ENABLED=True)


@METRICS_ON
class AttemptRecordingTests(NoNetworkMixin, TestCase):
    """One real outbound attempt, one row. Nothing that never reached the transport is counted as one."""

    def rows(self):
        return list(GemiRequestAttempt.objects.order_by("attempt", "id"))

    def test_one_successful_request_records_exactly_one_attempt(self):
        client, transport, _, _ = make_client(response(200, {"searchResults": []}))

        client.get("/companies", {"resultsSize": 200}, lane=GemiLane.DIGEST_IMPORT)

        row = self.rows()[0]
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(transport.calls), 1)               # one HTTP call, one observation
        self.assertEqual((row.attempt, row.outcome, row.http_status), (1, SUCCESS, 200))
        self.assertEqual(row.lane, int(GemiLane.DIGEST_IMPORT))
        self.assertFalse(row.is_retry)
        self.assertTrue(row.consumed_slot)

    def test_a_retrying_call_records_every_actual_attempt(self):
        """One logical call, three slots spent: the count that matters is attempts, not calls."""
        client, transport, _, _ = make_client(
            response(503), response(503), response(200, {"searchResults": []}), max_attempts=4
        )

        client.get("/companies", {"resultsSize": 200}, lane=GemiLane.DISCOVERY)

        rows = self.rows()
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual([(row.attempt, row.outcome) for row in rows],
                         [(1, SERVER_ERROR), (2, SERVER_ERROR), (3, SUCCESS)])
        self.assertEqual(sum(row.is_retry for row in rows), 2)

    def test_a_429_is_recorded_for_every_attempt_it_costs(self):
        client, _, _, _ = make_client(response(429, headers={"Retry-After": "1"}), max_attempts=2)

        with self.assertRaises(Exception):
            client.get("/companies", {}, lane=GemiLane.DIGEST_IMPORT)

        self.assertEqual([(row.attempt, row.outcome, row.http_status) for row in self.rows()],
                         [(1, RATE_LIMITED, 429), (2, RATE_LIMITED, 429)])

    def test_a_transport_failure_is_recorded_without_a_status(self):
        client, _, _, _ = make_client(GemiTransportError("TimeoutError", timed_out=True), max_attempts=2)

        with self.assertRaises(Exception):
            client.get("/companies", {}, lane=GemiLane.MONITORED_REFRESH)

        rows = self.rows()
        self.assertEqual([row.outcome for row in rows], [TRANSPORT_ERROR, TRANSPORT_ERROR])
        self.assertEqual([row.http_status for row in rows], [None, None])
        self.assertTrue(all(row.consumed_slot for row in rows))   # the slot was spent even with no response

    def test_a_final_4xx_is_still_one_real_request(self):
        client, _, _, _ = make_client(response(404, {"message": "not found"}))

        with self.assertRaises(GemiNotFoundError):
            client.get("/companies/123456789000", {}, lane=GemiLane.DOCUMENTS)

        row = self.rows()[0]
        self.assertEqual((row.outcome, row.http_status, row.attempt), (CLIENT_ERROR, 404, 1))

    def test_every_lane_is_recorded_as_itself(self):
        for lane in GemiLane:
            client, _, _, _ = make_client(response(200, {"searchResults": []}))
            client.get("/companies", {}, lane=lane)

        self.assertEqual(sorted(GemiRequestAttempt.objects.values_list("lane", flat=True)),
                         sorted(int(lane) for lane in GemiLane))


@METRICS_ON
class NothingSentNothingCountedTests(NoNetworkMixin, TestCase):
    """A failure *before* the transport must never look like an HTTP request."""

    def test_a_failure_before_the_transport_is_not_counted_as_a_request(self):
        """The collector switched off, and a missing key: nothing is sent and nothing is recorded."""
        with override_settings(GEMI_COLLECTOR_ENABLED=False):
            client, transport, _, _ = make_client(response(200, {}))
            with self.assertRaises(Exception):
                client.get("/companies", {}, lane=GemiLane.DIGEST_IMPORT)

        client, transport, _, _ = make_client(response(200, {}), api_key="")
        with self.assertRaises(Exception):
            client.get("/companies", {}, lane=GemiLane.DIGEST_IMPORT)

        self.assertEqual(transport.calls, [])
        self.assertEqual(GemiRequestAttempt.objects.count(), 0)

    def test_a_budget_timeout_is_recorded_but_consumed_no_slot(self):
        client, transport, clock, _ = make_client(response(200, {}))
        with patch.object(client._budget, "acquire", side_effect=GemiBudgetTimeoutError("no slot")):
            with self.assertRaises(GemiBudgetTimeoutError):
                client.get("/companies", {}, lane=GemiLane.DISCOVERY)

        row = GemiRequestAttempt.objects.get()
        self.assertEqual((row.outcome, row.http_status), (BUDGET_TIMEOUT, None))
        self.assertFalse(row.consumed_slot)
        self.assertEqual(transport.calls, [])                    # nothing was sent
        self.assertEqual(build_report(hours=1).sent_attempts, 0)  # and nothing counts against the ceiling

    def test_a_broken_budget_store_is_recorded_and_still_refuses_the_request(self):
        client, transport, _, _ = make_client(response(200, {}), store=BrokenStore())

        with self.assertRaises(GemiBudgetUnavailableError):
            client.get("/companies", {}, lane=GemiLane.DIGEST_IMPORT)

        row = GemiRequestAttempt.objects.get()
        self.assertEqual(row.outcome, BUDGET_UNAVAILABLE)
        self.assertFalse(row.consumed_slot)
        self.assertEqual(transport.calls, [])                    # the budget still fails closed

    def test_the_budget_wait_is_measured(self):
        client, _, clock, _ = make_client(response(200, {}))
        real_acquire = client._budget.acquire

        def slow_acquire(lane, *, max_wait):
            clock.now += 12.5                                    # the caller waited for a slot
            return real_acquire(lane, max_wait=max_wait)

        with patch.object(client._budget, "acquire", slow_acquire):
            client.get("/companies", {}, lane=GemiLane.DOCUMENTS)

        self.assertEqual(GemiRequestAttempt.objects.get().budget_wait_ms, 12_500)


@METRICS_ON
class ObservabilityIsHarmlessTests(NoNetworkMixin, TestCase):
    """Measurement must never cost a request, delay one, or break one."""

    def test_a_metrics_failure_does_not_break_a_valid_request(self):
        client, transport, _, _ = make_client(response(200, {"searchResults": [{"arGemi": "1"}]}))

        with patch("gemiapp.ingestion.request_metrics.apps.get_model", side_effect=RuntimeError("no table")):
            with self.assertLogs("gemiapp.ingestion.request_metrics", level="ERROR"):
                payload = client.get("/companies", {}, lane=GemiLane.DIGEST_IMPORT)

        self.assertEqual(payload["searchResults"], [{"arGemi": "1"}])   # the caller is unaffected
        self.assertEqual(len(transport.calls), 1)                        # and no request was repeated
        self.assertEqual(GemiRequestAttempt.objects.count(), 0)

    def test_recording_makes_no_extra_gemi_request(self):
        client, transport, _, _ = make_client(response(200, {"searchResults": []}))

        client.get("/companies", {"resultsSize": 200}, lane=GemiLane.DIGEST_IMPORT)

        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(GemiRequestAttempt.objects.count(), 1)

    def test_metrics_can_be_switched_off_without_touching_the_request_path(self):
        with override_settings(GEMI_REQUEST_METRICS_ENABLED=False):
            client, transport, _, _ = make_client(response(200, {"searchResults": []}))
            client.get("/companies", {}, lane=GemiLane.DIGEST_IMPORT)

        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(GemiRequestAttempt.objects.count(), 0)


@METRICS_ON
class NoSecretsOrCustomerDataTests(NoNetworkMixin, TestCase):
    """Operational metadata only: no key, no parameters, no payload, no identifiers."""

    def test_nothing_persisted_contains_the_key_parameters_or_payload(self):
        client, _, _, _ = make_client(response(200, {"searchResults": [{"arGemi": "123456789000",
                                                                       "companyName": "ΔΟΚΙΜΗ ΑΕ"}]}))

        client.get("/companies", {"api_key": SECRET, "companyName": "ΔΟΚΙΜΗ", "vatNumber": "999999999"},
                   lane=GemiLane.DIGEST_IMPORT)

        row = GemiRequestAttempt.objects.get()
        stored = " ".join(str(value) for value in row.__dict__.values())
        for forbidden in (SECRET, "companyName", "ΔΟΚΙΜΗ", "999999999", "123456789000", "vatNumber"):
            self.assertNotIn(forbidden, stored, forbidden)
        self.assertEqual(row.endpoint, "/companies")
        self.assertEqual(
            sorted(f.name for f in GemiRequestAttempt._meta.get_fields()),
            ["attempt", "budget_wait_ms", "endpoint", "http_status", "id", "lane", "occurred_at", "outcome"],
        )

    def test_an_identifier_in_the_path_is_normalised_away(self):
        client, _, _, _ = make_client(response(200, {}))

        client.get("/companies/123456789000", {}, lane=GemiLane.DOCUMENTS)

        self.assertEqual(GemiRequestAttempt.objects.get().endpoint, "/companies/{id}")

    def test_normalise_endpoint_drops_queries_and_identifiers(self):
        self.assertEqual(normalise_endpoint("/companies?companyName=X&api_key=k"), "/companies")
        self.assertEqual(normalise_endpoint("/companies/123456789000/documents"), "/companies/{id}/documents")
        self.assertEqual(normalise_endpoint("/metadata/prefectures"), "/metadata/prefectures")
        self.assertEqual(normalise_endpoint(""), "")


class ClassificationTests(TestCase):
    def test_statuses_map_to_outcome_classes(self):
        for status, expected in ((200, SUCCESS), (204, SUCCESS), (299, SUCCESS), (400, CLIENT_ERROR),
                                 (401, CLIENT_ERROR), (404, CLIENT_ERROR), (429, RATE_LIMITED),
                                 (500, SERVER_ERROR), (503, SERVER_ERROR), (599, SERVER_ERROR)):
            self.assertEqual(classify_status(status), expected, status)

    def test_the_outcomes_that_consumed_a_slot_are_exactly_the_transport_ones(self):
        self.assertEqual(set(request_metrics.SENT_OUTCOMES),
                         {SUCCESS, RATE_LIMITED, SERVER_ERROR, CLIENT_ERROR, TRANSPORT_ERROR})
        self.assertNotIn(BUDGET_TIMEOUT, request_metrics.SENT_OUTCOMES)
        self.assertNotIn(BUDGET_UNAVAILABLE, request_metrics.SENT_OUTCOMES)
        self.assertEqual(set(GemiRequestAttempt.SENT_OUTCOMES), set(request_metrics.SENT_OUTCOMES))


class ReportArithmeticTests(TestCase):
    """The figures the G6 decision is made from, on rows placed at known times."""

    BASE = datetime(2026, 9, 21, 9, 0, 0, tzinfo=dt_timezone.utc)

    def attempt(self, *, offset=0.0, lane=GemiLane.DIGEST_IMPORT, attempt=1, outcome=SUCCESS,
                status=200, wait_ms=0, endpoint="/companies"):
        return GemiRequestAttempt.objects.create(
            occurred_at=self.BASE + timedelta(seconds=offset), lane=int(lane), attempt=attempt,
            outcome=outcome, http_status=status, budget_wait_ms=wait_ms, endpoint=endpoint,
        )

    def report(self, hours=24.0, ceiling=7):
        return build_report(hours=hours, now=self.BASE + timedelta(hours=1), ceiling=ceiling)

    def test_counts_separate_attempts_calls_retries_and_outcomes(self):
        self.attempt()
        self.attempt(offset=1, attempt=2, outcome=SERVER_ERROR, status=503)
        self.attempt(offset=2, attempt=3, outcome=RATE_LIMITED, status=429)
        self.attempt(offset=3, lane=GemiLane.DISCOVERY, outcome=TRANSPORT_ERROR, status=None)
        self.attempt(offset=4, lane=GemiLane.DOCUMENTS, outcome=CLIENT_ERROR, status=404)
        self.attempt(offset=5, lane=GemiLane.DISCOVERY, outcome=BUDGET_TIMEOUT, status=None)

        report = self.report()

        self.assertEqual(report.recorded_attempts, 6)
        self.assertEqual(report.sent_attempts, 5)        # the budget timeout consumed no slot
        self.assertEqual(report.logical_calls, 4)
        self.assertEqual(report.retries, 2)
        self.assertEqual((report.successes, report.rate_limited, report.server_errors), (1, 1, 1))
        self.assertEqual((report.client_errors, report.transport_errors), (1, 1))
        self.assertEqual((report.budget_timeouts, report.budget_unavailable), (1, 0))

    def test_lane_totals_add_up(self):
        self.attempt(lane=GemiLane.DISCOVERY)
        self.attempt(offset=1, lane=GemiLane.DISCOVERY, attempt=2, outcome=SERVER_ERROR, status=500)
        self.attempt(offset=2, lane=GemiLane.MONITORED_REFRESH, outcome=BUDGET_TIMEOUT, status=None)

        by_lane = {lane.lane: lane for lane in self.report().by_lane}

        self.assertEqual((by_lane["DISCOVERY"].attempts, by_lane["DISCOVERY"].successes), (2, 1))
        self.assertEqual((by_lane["DISCOVERY"].retries, by_lane["DISCOVERY"].failures), (1, 1))
        self.assertEqual((by_lane["MONITORED_REFRESH"].attempts, by_lane["MONITORED_REFRESH"].not_sent), (0, 1))
        self.assertEqual(by_lane["DOCUMENTS"].attempts, 0)
        self.assertEqual(sum(lane.attempts for lane in self.report().by_lane), self.report().sent_attempts)

    def test_the_peak_is_a_true_rolling_minute_not_a_clock_minute(self):
        """Four attempts at 09:00:50..09:01:20 are 4 in a rolling minute but 2+2 in clock minutes."""
        for offset in (50, 55, 70, 80):
            self.attempt(offset=offset)

        report = self.report()

        self.assertEqual(report.peak_rolling_60s, 4)
        self.assertEqual(report.peak_window_start, self.BASE + timedelta(seconds=50))
        self.assertEqual(report.headroom_per_minute, 3)
        self.assertAlmostEqual(report.utilisation_pct, 400 / 7, places=6)

    def test_attempts_exactly_sixty_seconds_apart_are_not_in_the_same_window(self):
        self.attempt(offset=0)
        self.attempt(offset=60)

        self.assertEqual(self.report().peak_rolling_60s, 1)

    def test_saturated_and_high_utilisation_windows_are_counted(self):
        for offset in range(7):                     # seven attempts inside one rolling minute
            self.attempt(offset=offset)

        report = self.report(ceiling=7)

        self.assertEqual(report.peak_rolling_60s, 7)
        self.assertEqual(report.headroom_per_minute, 0)
        self.assertEqual(report.utilisation_pct, 100.0)
        self.assertEqual(report.saturated_windows, 1)          # only the window holding all seven
        self.assertEqual(report.high_utilisation_windows, 2)   # the windows holding six and seven

    def test_waiting_time_is_totalled_averaged_and_maxed(self):
        self.attempt(wait_ms=1_000)
        self.attempt(offset=1, wait_ms=3_000)
        self.attempt(offset=2, wait_ms=11_000)

        report = self.report()

        self.assertEqual(report.total_wait_seconds, 15.0)
        self.assertEqual(report.average_wait_seconds, 5.0)
        self.assertEqual(report.max_wait_seconds, 11.0)

    def test_only_the_window_is_reported(self):
        self.attempt(offset=-7200)                  # two hours before the window opens
        self.attempt(offset=1)

        self.assertEqual(build_report(hours=1, now=self.BASE + timedelta(hours=1)).sent_attempts, 1)

    def test_an_empty_window_reports_full_headroom_and_no_peak(self):
        report = self.report()

        self.assertEqual((report.recorded_attempts, report.peak_rolling_60s), (0, 0))
        self.assertIsNone(report.peak_window_start)
        self.assertEqual(report.headroom_per_minute, 7)
        self.assertEqual(report.average_per_minute, 0.0)

    def test_a_row_stamped_in_the_future_is_excluded_and_reported(self):
        """A worker whose clock runs ahead must not silently disappear from the measurement."""
        self.attempt(offset=1)
        self.attempt(offset=7200)                   # an hour past the end of the window

        report = self.report()

        self.assertEqual(report.sent_attempts, 1)
        self.assertEqual(report.future_attempts, 1)

    def test_endpoints_are_counted_by_normalised_name(self):
        self.attempt(endpoint="/companies")
        self.attempt(offset=1, endpoint="/companies")
        self.attempt(offset=2, endpoint="/companies/{id}")

        self.assertEqual(self.report().endpoints, (("/companies", 2), ("/companies/{id}", 1)))


class ReportCommandTests(TestCase):
    def run_command(self, **options):
        out = StringIO()
        call_command("report_gemi_request_budget", stdout=out, **options)
        return out.getvalue()

    def test_the_command_is_read_only_and_prints_every_required_figure(self):
        now = timezone.now()
        GemiRequestAttempt.objects.create(occurred_at=now - timedelta(minutes=5), lane=int(GemiLane.DISCOVERY),
                                          attempt=1, outcome=SUCCESS, http_status=200, budget_wait_ms=2_500,
                                          endpoint="/companies")
        GemiRequestAttempt.objects.create(occurred_at=now - timedelta(minutes=5), lane=int(GemiLane.DISCOVERY),
                                          attempt=2, outcome=RATE_LIMITED, http_status=429, budget_wait_ms=0,
                                          endpoint="/companies")

        output = self.run_command(hours=24)

        for expected in ("OUTBOUND ATTEMPTS", "consumed a slot       : 2", "retries               : 1",
                         "429 rate limited      : 1", "DISCOVERY", "DIGEST_IMPORT", "MONITORED_REFRESH",
                         "DOCUMENTS", "BUDGET WAIT", "peak requests/window  : 2", "headroom              : 5",
                         "utilisation vs ceiling", "saturated windows", "transport/network",
                         "budget timeout", "budget unavailable", "/companies"):
            self.assertIn(expected, output, expected)
        self.assertEqual(GemiRequestAttempt.objects.count(), 2)     # nothing was written or deleted

    def test_the_command_never_claims_g6_passed(self):
        output = self.run_command(hours=1)

        self.assertIn("G6_STATUS is not decided here", output)
        self.assertNotIn("G6 PASS", output)
        self.assertIn("G4 remains NOT STARTED / NOT PASSED", output)

    def test_an_empty_window_says_so_instead_of_implying_headroom(self):
        output = self.run_command(hours=1)

        self.assertIn("No outbound attempt was recorded", output)

    def test_a_clock_ahead_of_the_window_is_called_out(self):
        GemiRequestAttempt.objects.create(occurred_at=timezone.now() + timedelta(minutes=30),
                                          lane=int(GemiLane.DIGEST_IMPORT), attempt=1, outcome=SUCCESS,
                                          http_status=200, endpoint="/companies")

        output = self.run_command(hours=1)

        self.assertIn("stamped after the end of this window", output)
        self.assertIn("consumed a slot       : 0", output)

    def test_the_window_length_is_honoured(self):
        now = timezone.now()
        GemiRequestAttempt.objects.create(occurred_at=now - timedelta(hours=5), lane=int(GemiLane.DOCUMENTS),
                                          attempt=1, outcome=SUCCESS, http_status=200, endpoint="/metadata")

        self.assertIn("consumed a slot       : 0", self.run_command(hours=1))
        self.assertIn("consumed a slot       : 1", self.run_command(hours=24))
