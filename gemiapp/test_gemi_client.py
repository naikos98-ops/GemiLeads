"""Tests for the shared GEMI client and its rate budget (gemiapp.ingestion).

No test reaches the network. Every client gets a FakeTransport driven by a fake clock, and
urllib.request.urlopen is patched to fail loudly except where the urllib transport itself is tested.
"""

import json
import socket
import time
import traceback
import urllib.error
import urllib.parse
from datetime import date, timedelta
from io import BytesIO
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from .ingestion import (
    BudgetConfig,
    CacheBudgetStore,
    GemiApiError,
    GemiAuthenticationError,
    GemiBadRequestError,
    GemiBudgetTimeoutError,
    GemiBudgetUnavailableError,
    GemiClient,
    GemiConfigurationError,
    GemiLane,
    GemiNotFoundError,
    GemiRateBudget,
    GemiResponseFormatError,
    GemiRetryExhaustedError,
    current_gemi_lane,
    get_gemi_client,
    parse_error_body,
)
from .ingestion.client import GemiTransportError, RawResponse, RequestHeaders, urllib_transport
from .models import Company, CompanyActivity, ImportRun

SECRET = "gemi-test-SECRET-7Qz9"
BASE_URL = "https://gemi.invalid/api/opendata/v1"
RATE_HEADERS = {
    "RateLimit-Limit": "8", "RateLimit-Remaining": "5", "RateLimit-Reset": "30",
    "X-RateLimit-Limit-Minute": "8", "X-RateLimit-Remaining-Minute": "5",
}
# Verbatim shape of a validation error observed from the live API (docs/GEMI_API_CAPABILITY_REPORT.md).
HTML_400 = (
    '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<title>Error</title>\n</head>\n'
    "<body>\n<pre>Error: Parameter (resultsSize) is greater than the configured maximum (200): 201<br>"
    " &nbsp; &nbsp;at throwErrorWithCode (/opt/app/opendata/node_modules/swagger-tools/lib/validators.js:116:13)"
    "<br> &nbsp; &nbsp;at validateNumber</pre>\n</body>\n</html>\n"
)


class FakeClock:
    def __init__(self, start=1_800_000_000.0):
        self.now = start
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class MemoryStore:
    """BudgetStore with the same claim/expiry semantics as CacheBudgetStore, on a fake clock."""

    def __init__(self, clock):
        self.clock = clock
        self.data = {}

    def _live(self, key):
        item = self.data.get(key)
        if item is not None and item[1] <= self.clock.time():
            del self.data[key]
            return None
        return item

    def claim(self, key, value, ttl):
        if self._live(key):
            return False
        self.data[key] = (value, self.clock.time() + ttl)
        return True

    def get_float(self, key):
        item = self._live(key)
        return None if item is None else float(item[0])

    def set_float(self, key, value, ttl):
        self.data[key] = (value, self.clock.time() + ttl)

    def exists_any(self, keys):
        return any(self._live(key) for key in keys)


class BrokenStore:
    def claim(self, *args):
        raise RuntimeError("database is gone")

    get_float = set_float = exists_any = claim


def response(status=200, payload=None, *, body=None, headers=None):
    if body is None:
        body = b"" if payload is None else json.dumps(payload).encode()
    return RawResponse(status, {**RATE_HEADERS, **(headers or {})}, body)


class FakeTransport:
    """Returns the given outcomes in order, repeating the last one. An outcome may be a RawResponse,
    an exception instance to raise, or a callable taking the URL."""

    def __init__(self, *outcomes, clock=None):
        self.outcomes = list(outcomes)
        self.calls = []
        self.clock = clock

    def __call__(self, url, headers, timeout):
        self.calls.append({"url": url, "headers": headers.to_dict(), "at": self.clock.time() if self.clock else None})
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if callable(outcome):
            outcome = outcome(url)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def queries(self):
        return [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(call["url"]).query)) for call in self.calls]


class RecordingBudget(GemiRateBudget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lanes = []

    def acquire(self, lane, *, max_wait):
        self.lanes.append(GemiLane(lane))
        return super().acquire(lane, max_wait=max_wait)


def make_client(*outcomes, clock=None, store=None, max_attempts=4, api_key=SECRET):
    clock = clock or FakeClock()
    store = store if store is not None else MemoryStore(clock)
    budget = RecordingBudget(store, BudgetConfig(), clock=clock.time, sleep=clock.sleep)
    transport = FakeTransport(*outcomes, clock=clock)
    client = GemiClient(
        api_key=api_key, base_url=BASE_URL, budget=budget, transport=transport,
        timeout=60, max_attempts=max_attempts, sleep=clock.sleep, clock=clock.time,
    )
    return client, transport, clock, budget


class NoNetworkMixin:
    def setUp(self):
        super().setUp()
        guard = patch("urllib.request.urlopen", side_effect=AssertionError("tests must not reach the GEMI API"))
        guard.start()
        self.addCleanup(guard.stop)


class GemiClientRequestTests(NoNetworkMixin, SimpleTestCase):
    def test_successful_request_returns_decoded_json_and_sends_the_key_only_as_a_header(self):
        payload = {"searchMetadata": {"totalCount": 1}, "searchResults": [{"arGemi": "1"}]}
        client, transport, _, budget = make_client(response(200, payload))

        result = client.search_companies({"isActive": "true", "resultsSize": 200}, lane=GemiLane.DIGEST_IMPORT)

        self.assertEqual(result, payload)
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["url"], f"{BASE_URL}/companies?isActive=true&resultsSize=200")
        self.assertEqual(call["headers"]["api_key"], SECRET)
        self.assertEqual(call["headers"]["Accept"], "application/json")
        self.assertNotIn(SECRET, call["url"])
        self.assertEqual(budget.lanes, [GemiLane.DIGEST_IMPORT])

    def test_empty_search_404_is_an_empty_result_page_and_not_retried(self):
        client, transport, _, _ = make_client(response(404, body=b""))

        page = client.search_companies({"activities": "35111000", "resultsOffset": 400}, lane=GemiLane.DISCOVERY)

        self.assertEqual(page["searchResults"], [])
        self.assertEqual(page["searchMetadata"]["totalCount"], 0)
        self.assertEqual(page["searchMetadata"]["resultsOffset"], 400)
        self.assertEqual(len(transport.calls), 1)

    def test_404_outside_a_search_raises_not_found(self):
        client, transport, _, _ = make_client(response(404, body=b""))

        with self.assertRaises(GemiNotFoundError) as caught:
            client.get("/companies/1", lane=GemiLane.MONITORED_REFRESH)

        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(len(transport.calls), 1)

    def test_429_waits_for_retry_after_before_trying_again(self):
        limited = response(
            429, {"message": "API rate limit exceeded", "request_id": "6850120e"},
            headers={"Retry-After": "13", "RateLimit-Remaining": "0", "RateLimit-Reset": "13"},
        )
        client, transport, _, budget = make_client(limited, response(200, {"ok": True}))

        self.assertEqual(client.get("/health", lane=GemiLane.DISCOVERY), {"ok": True})

        self.assertEqual(len(transport.calls), 2)
        self.assertGreaterEqual(transport.calls[1]["at"] - transport.calls[0]["at"], 13)
        self.assertGreaterEqual(budget.store.get_float(budget.COOLDOWN_KEY) or 0, transport.calls[0]["at"] + 13)

    def test_a_429_pauses_every_client_sharing_the_budget(self):
        clock = FakeClock()
        store = MemoryStore(clock)
        first, first_transport, _, _ = make_client(
            response(429, headers={"Retry-After": "20"}), clock=clock, store=store, max_attempts=1,
        )
        with self.assertRaises(GemiRetryExhaustedError):
            first.get("/health", lane=GemiLane.DOCUMENTS)

        second, second_transport, _, _ = make_client(response(200, {"ok": True}), clock=clock, store=store)
        second.get("/health", lane=GemiLane.DISCOVERY)

        self.assertGreaterEqual(second_transport.calls[0]["at"] - first_transport.calls[0]["at"], 20)

    def test_an_exhausted_rate_limit_window_defers_the_next_request_until_reset(self):
        exhausted = response(
            200, {"n": 1}, headers={"RateLimit-Remaining": "0", "X-RateLimit-Remaining-Minute": "0", "RateLimit-Reset": "25"},
        )
        client, transport, _, _ = make_client(exhausted, response(200, {"n": 2}))

        client.get("/health", lane=GemiLane.DIGEST_IMPORT)
        client.get("/health", lane=GemiLane.DIGEST_IMPORT)

        self.assertGreaterEqual(transport.calls[1]["at"] - transport.calls[0]["at"], 25)

    def test_transient_5xx_is_retried_with_exponential_backoff(self):
        upstream = json.dumps([{"id": "systemError", "descr": "Error: Connection terminated due to connection timeout"}])
        client, transport, clock, _ = make_client(
            response(500, body=upstream.encode()), response(503, body=b""), response(200, {"ok": True}),
        )

        self.assertEqual(client.get("/companies/10708653000", lane=GemiLane.MONITORED_REFRESH), {"ok": True})

        self.assertEqual(len(transport.calls), 3)
        self.assertIn(5.0, clock.sleeps)
        self.assertIn(15.0, clock.sleeps)
        self.assertGreaterEqual(transport.calls[1]["at"] - transport.calls[0]["at"], 5)
        self.assertGreaterEqual(transport.calls[2]["at"] - transport.calls[1]["at"], 15)

    def test_network_timeout_is_retried(self):
        client, transport, _, _ = make_client(
            GemiTransportError("TimeoutError", timed_out=True), response(200, {"ok": True}),
        )

        self.assertEqual(client.get("/health", lane=GemiLane.DIGEST_IMPORT), {"ok": True})
        self.assertEqual(len(transport.calls), 2)

    def test_retry_exhaustion_is_bounded_and_reports_the_last_failure(self):
        client, transport, _, _ = make_client(
            response(502, {"message": "An invalid response was received from the upstream server"}),
        )

        with self.assertRaises(GemiRetryExhaustedError) as caught:
            client.get("/companies", {"isActive": "true"}, lane=GemiLane.DIGEST_IMPORT)

        self.assertEqual(len(transport.calls), 4)
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(caught.exception.attempts, 4)
        self.assertEqual(str(caught.exception), "GEMI API HTTP 502: An invalid response was received from the upstream server")
        self.assertIsInstance(caught.exception, RuntimeError)

    def test_network_retry_exhaustion_keeps_the_legacy_message(self):
        client, transport, _, _ = make_client(GemiTransportError("ConnectionRefusedError", timed_out=False), max_attempts=3)

        with self.assertRaises(GemiRetryExhaustedError) as caught:
            client.get("/health", lane=GemiLane.DIGEST_IMPORT)

        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(str(caught.exception), "Το GEMI API δεν απάντησε.")

    def test_non_retryable_statuses_fail_on_the_first_attempt(self):
        client, transport, _, _ = make_client(response(400, body=HTML_400.encode()))
        with self.assertRaises(GemiBadRequestError) as caught:
            client.search_companies({"resultsSize": 201}, lane=GemiLane.DIGEST_IMPORT)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(
            str(caught.exception),
            "GEMI API HTTP 400: Error: Parameter (resultsSize) is greater than the configured maximum (200): 201",
        )

        client, transport, _, _ = make_client(response(401, {"message": "Invalid authentication credentials"}))
        with self.assertRaises(GemiAuthenticationError) as caught:
            client.get("/health", lane=GemiLane.DIGEST_IMPORT)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(str(caught.exception), "Το GEMI_API_KEY δεν είναι έγκυρο.")

        client, transport, _, _ = make_client(response(405, body=b""))
        with self.assertRaises(GemiApiError):
            client.get("/downloadFile", lane=GemiLane.DOCUMENTS)
        self.assertEqual(len(transport.calls), 1)

    def test_missing_key_fails_before_any_request(self):
        client, transport, _, _ = make_client(response(200, {}), api_key="")

        with self.assertRaises(GemiConfigurationError) as caught:
            client.get("/health", lane=GemiLane.DIGEST_IMPORT)

        self.assertEqual(str(caught.exception), "Λείπει το GEMI_API_KEY από το περιβάλλον.")
        self.assertEqual(transport.calls, [])

    def test_invalid_json_in_a_successful_response_is_a_format_error(self):
        client, transport, _, _ = make_client(response(200, body=b"<html>maintenance</html>"))

        with self.assertRaises(GemiResponseFormatError):
            client.get("/health", lane=GemiLane.DIGEST_IMPORT)
        self.assertEqual(len(transport.calls), 1)

    def test_budget_store_failure_sends_nothing(self):
        clock = FakeClock()
        client, transport, _, _ = make_client(response(200, {}), clock=clock, store=BrokenStore())

        with self.assertRaises(GemiBudgetUnavailableError):
            client.get("/health", lane=GemiLane.DIGEST_IMPORT)
        self.assertEqual(transport.calls, [])


class GemiErrorParsingTests(SimpleTestCase):
    def test_gateway_json_object(self):
        detail = parse_error_body(b'{"message": "API rate limit exceeded", "request_id": "6850120e"}')
        self.assertEqual((detail.message, detail.request_id), ("API rate limit exceeded", "6850120e"))

    def test_upstream_json_array(self):
        detail = parse_error_body(b'[{"id": "systemError", "descr": "Error: connect ECONNREFUSED 10.241.78.229:5432"}]')
        self.assertEqual(detail.code, "systemError")
        self.assertEqual(detail.message, "Error: connect ECONNREFUSED 10.241.78.229:5432")

    def test_spec_error_entry(self):
        detail = parse_error_body(b'[{"code": "E42", "message": "invalid arGemi"}]')
        self.assertEqual((detail.code, detail.message), ("E42", "invalid arGemi"))

    def test_html_validation_error_keeps_only_the_first_line(self):
        detail = parse_error_body(HTML_400.encode())
        self.assertEqual(detail.message, "Error: Parameter (resultsSize) is greater than the configured maximum (200): 201")
        self.assertNotIn("throwErrorWithCode", detail.message)

    def test_empty_body_uses_the_gateway_request_id_header(self):
        detail = parse_error_body(b"", {"X-Kong-Request-Id": "09cb3a69"})
        self.assertEqual((detail.message, detail.request_id), ("", "09cb3a69"))

    def test_plain_text_is_collapsed_and_truncated(self):
        detail = parse_error_body(("upstream   down\n" * 100).encode())
        self.assertTrue(detail.message.startswith("upstream down upstream down"))
        self.assertLessEqual(len(detail.message), 300)

    def test_the_key_is_redacted_from_every_variant(self):
        for body in (
            json.dumps({"message": f"bad key {SECRET}"}).encode(),
            json.dumps([{"id": "x", "descr": f"bad key {SECRET}"}]).encode(),
            f"<pre>Error: bad key {SECRET}<br> at x</pre>".encode(),
            f"bad key {SECRET}".encode(),
        ):
            with self.subTest(body=body[:20]):
                self.assertNotIn(SECRET, parse_error_body(body, secret=SECRET).message)


class GemiRateBudgetTests(SimpleTestCase):
    def budget(self, clock, store=None, sleep=None):
        return GemiRateBudget(store or MemoryStore(clock), BudgetConfig(), clock=clock.time, sleep=sleep or clock.sleep)

    def test_never_more_than_seven_requests_in_any_rolling_minute(self):
        clock = FakeClock()
        budget = self.budget(clock)
        times = []
        for _ in range(40):
            budget.acquire(GemiLane.DIGEST_IMPORT, max_wait=900)
            times.append(clock.time())

        for start in times:
            self.assertLessEqual(sum(1 for t in times if start <= t < start + 60), 7)
            # The same holds with the full clock-skew margin added to the window.
            self.assertLessEqual(sum(1 for t in times if start <= t < start + 62), 7)
        # ...and in every fixed gateway minute, whatever its alignment.
        for offset in range(0, 60, 5):
            per_minute = {}
            for t in times:
                per_minute[(t + offset) // 60] = per_minute.get((t + offset) // 60, 0) + 1
            self.assertLessEqual(max(per_minute.values()), 7)
        # Sustained pacing is one request per 10.33 s slot, not slower.
        self.assertLess(times[-1] - times[0], 39 * 10.34 + 1)

    def test_two_processes_sharing_the_store_share_one_allowance(self):
        clock = FakeClock()
        store = MemoryStore(clock)
        daily, intraday = self.budget(clock, store), self.budget(clock, store)
        times = []
        for index in range(30):
            (daily if index % 2 else intraday).acquire(GemiLane.DIGEST_IMPORT, max_wait=900)
            times.append(clock.time())

        for start in times:
            self.assertLessEqual(sum(1 for t in times if start <= t < start + 60), 7)

    def test_capacity_is_clamped_to_seven(self):
        with override_settings(GEMI_RATE_LIMIT_PER_MINUTE=12):
            self.assertEqual(BudgetConfig.from_settings().capacity, 7)
        with override_settings(GEMI_RATE_LIMIT_PER_MINUTE=5):
            self.assertEqual(BudgetConfig.from_settings().capacity, 5)
        with override_settings(GEMI_RATE_LIMIT_PER_MINUTE=1):
            self.assertEqual(BudgetConfig.from_settings().capacity, 2)
        with self.assertRaises(ValueError):
            BudgetConfig(capacity=8)

    def test_a_waiting_higher_priority_lane_holds_back_lower_lanes(self):
        clock = FakeClock()
        store = MemoryStore(clock)
        budget = self.budget(clock, store)
        store.set_float("waiting:1", clock.time(), 100)  # a discovery caller elsewhere is waiting

        with self.assertRaises(GemiBudgetTimeoutError):
            budget.acquire(GemiLane.DOCUMENTS, max_wait=30)
        with self.assertRaises(GemiBudgetTimeoutError):
            budget.acquire(GemiLane.DIGEST_IMPORT, max_wait=30)
        budget.acquire(GemiLane.DISCOVERY, max_wait=1)  # never blocked by its own lane

    def test_a_lower_lane_proceeds_once_the_higher_lane_marker_expires(self):
        clock = FakeClock()
        store = MemoryStore(clock)
        budget = self.budget(clock, store)
        store.set_float("waiting:2", clock.time(), 5)
        started = clock.time()

        budget.acquire(GemiLane.DOCUMENTS, max_wait=60)

        self.assertGreaterEqual(clock.time() - started, 5)

    def test_a_caller_that_has_to_wait_announces_its_lane(self):
        clock = FakeClock()
        store = MemoryStore(clock)
        observed = []

        def sleep(seconds):
            observed.append(store.exists_any(["waiting:1"]))
            clock.sleep(seconds)

        budget = self.budget(clock, store, sleep=sleep)
        budget.acquire(GemiLane.DIGEST_IMPORT, max_wait=60)  # takes the current slot
        budget.acquire(GemiLane.DISCOVERY, max_wait=60)  # has to wait for the next one

        self.assertTrue(observed and all(observed))

    def test_times_out_when_no_slot_frees_up_in_time(self):
        clock = FakeClock()
        budget = self.budget(clock)
        budget.acquire(GemiLane.DIGEST_IMPORT, max_wait=1)

        with self.assertRaises(GemiBudgetTimeoutError):
            budget.acquire(GemiLane.DIGEST_IMPORT, max_wait=2)

    def test_a_cooldown_holds_every_lane_until_it_passes(self):
        clock = FakeClock()
        budget = self.budget(clock)
        started = clock.time()
        budget.defer_until(started + 30, reason="test")
        budget.defer_until(started + 10, reason="a shorter pause never shortens the longer one")

        budget.acquire(GemiLane.DISCOVERY, max_wait=60)

        self.assertGreaterEqual(clock.time() - started, 30)

    def test_store_failure_fails_closed(self):
        clock = FakeClock()
        with self.assertRaises(GemiBudgetUnavailableError):
            self.budget(clock, BrokenStore()).acquire(GemiLane.DISCOVERY, max_wait=60)


class CacheBudgetStoreTests(TestCase):
    """The production store, on the real "shared" database cache."""

    def test_claims_are_exclusive_and_expire(self):
        store = CacheBudgetStore(prefix="gemi-budget-test")

        self.assertTrue(store.claim("slot:1", 1.0, ttl=1))
        self.assertFalse(store.claim("slot:1", 2.0, ttl=1))
        self.assertTrue(store.claim("slot:2", 3.0, ttl=1))
        store.set_float("cooldown-until", 42.5, ttl=30)
        self.assertEqual(store.get_float("cooldown-until"), 42.5)
        self.assertIsNone(store.get_float("absent"))
        self.assertTrue(store.exists_any(["absent", "slot:2"]))
        self.assertFalse(store.exists_any(["absent", "also-absent"]))

        time.sleep(2.1)
        self.assertTrue(store.claim("slot:1", 4.0, ttl=1))

    def test_two_budgets_on_the_database_cache_cannot_claim_the_same_slot(self):
        clock = FakeClock()
        first = GemiRateBudget(CacheBudgetStore(prefix="gemi-budget-test"), BudgetConfig(), clock=clock.time, sleep=clock.sleep)
        second = GemiRateBudget(CacheBudgetStore(prefix="gemi-budget-test"), BudgetConfig(), clock=clock.time, sleep=clock.sleep)

        first.acquire(GemiLane.DIGEST_IMPORT, max_wait=1)
        with self.assertRaises(GemiBudgetTimeoutError):
            second.acquire(GemiLane.DIGEST_IMPORT, max_wait=0.1)

    def test_the_default_client_uses_the_shared_database_cache_and_settings(self):
        client = get_gemi_client()
        self.assertIsInstance(client._budget.store, CacheBudgetStore)
        self.assertEqual(client._budget.store.alias, "shared")
        self.assertEqual(client._budget.config.capacity, 7)
        self.assertEqual(client._max_attempts, 4)
        self.assertEqual(client._timeout, 60)


class GemiSecrecyTests(NoNetworkMixin, TestCase):
    def test_the_key_never_appears_in_logs_errors_or_reprs(self):
        scenarios = [
            (response(500, {"message": f"upstream echoed {SECRET}"}),),
            (response(400, body=f"<pre>Error: bad key {SECRET}<br> at x</pre>".encode()),),
            (GemiTransportError("TimeoutError", timed_out=True),),
            (response(429, {"message": SECRET}, headers={"Retry-After": "5"}),),
            (response(401, {"message": SECRET}),),
        ]
        errors = []
        with self.assertLogs("gemiapp.ingestion", level="DEBUG") as logs:
            for outcomes in scenarios:
                client, _, _, _ = make_client(*outcomes, max_attempts=2)
                with self.assertRaises(GemiApiError) as caught:
                    client.get("/companies", {"isActive": "true"}, lane=GemiLane.DIGEST_IMPORT)
                errors.append(caught.exception)

        for error in errors:
            rendered = "".join(traceback.format_exception(error))
            self.assertNotIn(SECRET, str(error))
            self.assertNotIn(SECRET, repr(error))
            self.assertNotIn(SECRET, rendered)
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertNotIn(SECRET, repr(client))
        self.assertNotIn(SECRET, repr(RequestHeaders(SECRET)))

    def test_urllib_transport_maps_failures_without_carrying_the_request(self):
        headers = RequestHeaders(SECRET)
        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            with self.assertRaises(GemiTransportError) as caught:
                urllib_transport(f"{BASE_URL}/health", headers, 1)
        self.assertTrue(caught.exception.timed_out)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertIsNone(caught.exception.__cause__)

        refused = urllib.error.URLError(ConnectionRefusedError("refused"))
        with patch("urllib.request.urlopen", side_effect=refused):
            with self.assertRaises(GemiTransportError) as caught:
                urllib_transport(f"{BASE_URL}/health", headers, 1)
        self.assertFalse(caught.exception.timed_out)
        self.assertEqual(caught.exception.kind, "ConnectionRefusedError")

        not_found = urllib.error.HTTPError(f"{BASE_URL}/companies", 404, "Not Found", {"RateLimit-Remaining": "4"}, BytesIO(b""))
        with patch("urllib.request.urlopen", side_effect=not_found):
            raw = urllib_transport(f"{BASE_URL}/companies", headers, 1)
        self.assertEqual((raw.status, raw.body), (404, b""))
        self.assertEqual(raw.headers.get("RateLimit-Remaining"), "4")

    def test_a_failed_import_records_no_key(self):
        from .services import import_for_date

        client, _, _, _ = make_client(response(500, {"message": f"upstream echoed {SECRET}"}), max_attempts=2)
        with patch("gemiapp.services.get_gemi_client", return_value=client):
            with self.assertRaises(GemiRetryExhaustedError):
                import_for_date(date(2026, 9, 14))

        run = ImportRun.objects.get()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_message, "GEMI API HTTP 500: upstream echoed [redacted]")


def gemi_item(ar_gemi, day, *, name=None, activity="62010000"):
    return {
        "arGemi": str(ar_gemi), "afm": "099999999", "coNameEl": name or f"ΕΤΑΙΡΕΙΑ {ar_gemi} ΙΚΕ",
        "coTitlesEl": [], "incorporationDate": day.isoformat(),
        "legalType": {"id": 19, "descr": "ΙΚΕ"}, "status": {"id": 3, "descr": "Ενεργή"},
        "prefecture": {"id": 5, "descr": "ΑΤΤΙΚΗΣ"}, "municipality": {"id": 61190, "descr": "ΚΗΦΙΣΙΑΣ"},
        "gemiOffice": {"id": 3, "descr": "ΕΠΙΜΕΛΗΤΗΡΙΟ ΑΘΗΝΑΣ"}, "city": "ΑΘΗΝΑ", "street": "ΟΔΟΣ", "zipCode": "10682",
        "activities": [{"activity": {"id": activity, "descr": "ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΣ", "kadVersion": "kad_2026"}, "type": "Κύρια"}],
    }


def routed(pages):
    """Route a search URL to a page by (isActive, resultsOffset); anything unrouted is an empty 404."""
    def route(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        page = pages.get((query.get("isActive"), query.get("resultsOffset")))
        return response(404, body=b"") if page is None else response(200, page)
    return route


class ImporterCompatibilityTests(NoNetworkMixin, TestCase):
    """The importers behave as before the shared client, now paced by the shared budget."""

    target = date(2026, 9, 14)

    def test_fetch_companies_sends_the_legacy_query_and_matches_the_legacy_seam(self):
        from .services import fetch_companies

        older = self.target - timedelta(days=1)
        active = {"searchMetadata": {"totalCount": 4}, "searchResults": [
            gemi_item(900000001000, self.target), gemi_item(900000002000, self.target),
            gemi_item(900000003000, self.target), gemi_item(900000004000, older),
        ]}
        inactive = {"searchMetadata": {"totalCount": 1}, "searchResults": [gemi_item(900000005000, older)]}
        pages = {("true", "0"): active, ("false", "0"): inactive}

        client, transport, _, budget = make_client(routed(pages))
        with patch("gemiapp.services.get_gemi_client", return_value=client):
            via_client = fetch_companies(self.target)

        # The pre-client importer, fed the same payloads through its original _get seam.
        legacy_payload = {("true", 0): active, ("false", 0): inactive}
        with patch("gemiapp.services._get", side_effect=lambda path, params: legacy_payload[(params["isActive"], params["resultsOffset"])]):
            via_legacy_seam = fetch_companies(self.target)

        self.assertEqual(via_client, via_legacy_seam)
        self.assertEqual(sorted(item["arGemi"] for item in via_client), ["900000001000", "900000002000", "900000003000"])
        self.assertEqual(transport.queries(), [
            {"isActive": "true", "resultsSortBy": "-incorporationDate", "resultsOffset": "0", "resultsSize": "200"},
            {"isActive": "false", "resultsSortBy": "-incorporationDate", "resultsOffset": "0", "resultsSize": "200"},
        ])
        self.assertTrue(all(call["url"].startswith(f"{BASE_URL}/companies?") for call in transport.calls))
        self.assertEqual(set(budget.lanes), {GemiLane.DIGEST_IMPORT})

    def test_pagination_follows_the_legacy_offsets(self):
        from .services import fetch_companies

        first = {"searchMetadata": {"totalCount": 251}, "searchResults": [gemi_item(800000000000 + i, self.target) for i in range(200)]}
        second = {"searchMetadata": {"totalCount": 251}, "searchResults": (
            [gemi_item(810000000000 + i, self.target) for i in range(50)] + [gemi_item(820000000000, self.target - timedelta(days=1))]
        )}
        client, transport, _, _ = make_client(routed({("true", "0"): first, ("true", "200"): second}))
        with patch("gemiapp.services.get_gemi_client", return_value=client):
            found = fetch_companies(self.target)

        self.assertEqual(len(found), 250)
        self.assertEqual(
            [(query["isActive"], query["resultsOffset"]) for query in transport.queries()],
            [("true", "0"), ("true", "200"), ("false", "0")],
        )

    def test_import_for_date_stores_the_same_rows_as_the_legacy_seam(self):
        from .services import import_for_date

        items = [gemi_item(900000001000, self.target), gemi_item(900000002000, self.target, activity="56101000")]
        client, _, _, _ = make_client(routed({("true", "0"): {"searchMetadata": {"totalCount": 2}, "searchResults": items}}))

        def snapshot():
            return (
                list(Company.objects.order_by("gemi_number").values(
                    "gemi_number", "vat_number", "name", "legal_type", "status", "is_active", "incorporation_date",
                    "gemi_office", "prefecture", "municipality", "city", "address", "postal_code", "activities", "raw_data",
                )),
                list(CompanyActivity.objects.order_by("company__gemi_number", "code").values_list("company__gemi_number", "code", "activity_type")),
            )

        with patch("gemiapp.services.get_gemi_client", return_value=client):
            run = import_for_date(self.target)  # the inactive search is an empty 404 and must not fail the run
        via_client = snapshot()
        self.assertEqual((run.status, run.fetched_count, run.created_count), ("success", 2, 2))

        Company.objects.all().delete()
        ImportRun.objects.all().delete()
        with patch("gemiapp.services.fetch_companies", return_value=items):
            import_for_date(self.target)

        self.assertEqual(via_client, snapshot())

    def test_bulk_import_runs_in_the_refresh_lane_and_restores_the_caller_lane(self):
        from .services import import_companies_since_date

        start = self.target - timedelta(days=2)
        page = {"searchMetadata": {"totalCount": 2}, "searchResults": [
            gemi_item(900000001000, self.target), gemi_item(900000009000, start - timedelta(days=1)),
        ]}
        client, _, _, budget = make_client(routed({("true", "0"): page}))
        with patch("gemiapp.services.get_gemi_client", return_value=client):
            created, updated = import_companies_since_date(start)

        self.assertEqual((created, updated), (1, 0))
        self.assertEqual(set(budget.lanes), {GemiLane.MONITORED_REFRESH})
        self.assertEqual(current_gemi_lane(default=GemiLane.DIGEST_IMPORT), GemiLane.DIGEST_IMPORT)

    def test_bulk_import_stops_after_bounded_retries_instead_of_looping_forever(self):
        from .services import import_companies_since_date

        client, transport, _, _ = make_client(response(503, body=b""), max_attempts=2)
        with patch("gemiapp.services.get_gemi_client", return_value=client):
            with self.assertRaises(GemiRetryExhaustedError):
                import_companies_since_date(self.target)

        self.assertEqual(len(transport.calls), 2)
