"""The single entry point for every request to the ΓΕΜΗ Open Data API.

Every GEMI call goes through ``GemiClient.get()`` or ``GemiClient.search_companies()``, which:

* waits for a slot in the shared GemiRateBudget, in the caller's priority lane;
* retries 429, transient 5xx and network failures a bounded number of times -- a 429 through the
  gateway's Retry-After, recorded as a cooldown every process honours; the rest with exponential
  backoff;
* treats a 404 from the search endpoint as an empty result (the gateway answers a search with no
  matches with 404 and an empty body) and a 404 anywhere else as GemiNotFoundError;
* reduces the three error formats the API returns -- gateway JSON objects, upstream JSON arrays and
  HTML stack traces from request validation -- to one short message;
* keeps the API key out of every message, log line and exception chain.

Observed API behaviour is documented in docs/GEMI_API_CAPABILITY_REPORT.md.
"""

from __future__ import annotations

import email.utils
import html
import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping

from django.conf import settings

from .errors import (
    GemiApiError,
    GemiAuthenticationError,
    GemiBadRequestError,
    GemiConfigurationError,
    GemiNotFoundError,
    GemiResponseFormatError,
    GemiRetryExhaustedError,
)
from .rate_budget import GemiLane, GemiRateBudget

logger = logging.getLogger(__name__)

SEARCH_PATH = "/companies"
USER_AGENT = "Gemi-Leads/1.0"
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
BACKOFF_BASE_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 60.0
RETRY_AFTER_FALLBACK_SECONDS = 60.0
RETRY_AFTER_MAX_SECONDS = 300.0
MAX_ERROR_MESSAGE_LENGTH = 300

# How long a caller waits for a request slot before giving up. The two lanes that feed customers
# wait longest; both stay well inside the django-q task timeout (Q_CLUSTER["timeout"] = 1800 s).
DEFAULT_MAX_WAIT_SECONDS = {
    GemiLane.DISCOVERY: 900.0,
    GemiLane.DIGEST_IMPORT: 900.0,
    GemiLane.MONITORED_REFRESH: 600.0,
    GemiLane.DOCUMENTS: 300.0,
}

# The messages gemiapp.services._get raised before this client existed, kept verbatim because they
# surface in ImportRun.error_message and the Superadmin pipeline view.
MISSING_KEY_MESSAGE = "Λείπει το GEMI_API_KEY από το περιβάλλον."
INVALID_KEY_MESSAGE = "Το GEMI_API_KEY δεν είναι έγκυρο."
UNREACHABLE_MESSAGE = "Το GEMI API δεν απάντησε."

_current_lane: ContextVar[GemiLane | None] = ContextVar("gemi_lane", default=None)


@contextmanager
def gemi_lane(lane: GemiLane) -> Iterator[None]:
    """Run a block of GEMI calls in ``lane`` without passing the lane through every function."""
    token = _current_lane.set(GemiLane(lane))
    try:
        yield
    finally:
        _current_lane.reset(token)


def current_gemi_lane(default: GemiLane) -> GemiLane:
    return _current_lane.get() or default


class RequestHeaders:
    """The headers of one GEMI request.

    Deliberately not a Mapping: debuggers, error reporters and log formatters render it through
    __repr__, which never shows the key.
    """

    __slots__ = ("_api_key",)

    def __init__(self, api_key: str):
        self._api_key = api_key

    def to_dict(self) -> dict[str, str]:
        return {"api_key": self._api_key, "Accept": "application/json", "User-Agent": USER_AGENT}

    def __repr__(self) -> str:
        return "<RequestHeaders api_key=[redacted]>"


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class GemiTransportError(Exception):
    """A request that produced no HTTP response: timeout, refused connection, DNS failure.

    Carries only the underlying exception's type name, never the request object.
    """

    def __init__(self, kind: str, *, timed_out: bool):
        super().__init__(kind)
        self.kind = kind
        self.timed_out = timed_out


Transport = Callable[[str, RequestHeaders, float], RawResponse]


def urllib_transport(url: str, headers: RequestHeaders, timeout: float) -> RawResponse:
    """Send one GET. Every HTTP status is returned as a RawResponse; anything else is re-raised as
    GemiTransportError with its context suppressed, so the original exception cannot carry the
    request into a traceback or error report."""
    request = urllib.request.Request(url, headers=headers.to_dict())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return RawResponse(response.status, dict(response.headers.items()), response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        return RawResponse(exc.code, dict(exc.headers.items()) if exc.headers else {}, body or b"")
    except TimeoutError as exc:
        raise GemiTransportError(type(exc).__name__, timed_out=True) from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        kind = type(reason).__name__ if isinstance(reason, BaseException) else "URLError"
        raise GemiTransportError(kind, timed_out=isinstance(reason, TimeoutError)) from None
    except (OSError, http.client.HTTPException) as exc:
        raise GemiTransportError(type(exc).__name__, timed_out=False) from None


@dataclass(frozen=True)
class GemiErrorDetail:
    message: str = ""
    code: str = ""
    request_id: str = ""


_HTML_PRE = re.compile(r"<pre[^>]*>(.*?)(?:<br\s*/?>|</pre>)", re.IGNORECASE | re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")


def redact(text: str, secret: str | None) -> str:
    if secret and text:
        return text.replace(secret, "[redacted]")
    return text


def parse_error_body(
    body: bytes, headers: Mapping[str, str] | None = None, *, secret: str | None = None
) -> GemiErrorDetail:
    """Reduce any GEMI error body to a short, key-free message.

    Observed variants: gateway objects ``{"message", "request_id"}``, upstream arrays
    ``[{"id", "descr"}]`` (the spec's ErrorEntry is ``{"code", "message"}``), HTML stack traces from
    request validation (only the first line of the ``<pre>`` block is kept), and empty bodies.
    """
    text = (body or b"").decode("utf-8", "replace").strip()
    lowered = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
    message: Any = ""
    code: Any = ""
    request_id: Any = ""
    data: Any = None
    if text and text[0] in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if isinstance(data, dict):
        message = data.get("message") or data.get("descr") or data.get("error") or ""
        code = data.get("code") or data.get("id") or ""
        request_id = data.get("request_id") or ""
    elif isinstance(data, list):
        entry = next((item for item in data if isinstance(item, dict)), {})
        message = entry.get("message") or entry.get("descr") or ""
        code = entry.get("code") or entry.get("id") or ""
    elif "<" in text:
        match = _HTML_PRE.search(text)
        fragment = match.group(1) if match else text
        message = html.unescape(_HTML_TAG.sub(" ", fragment))
    else:
        message = text
    message = " ".join(str(message).split())[:MAX_ERROR_MESSAGE_LENGTH]
    return GemiErrorDetail(
        message=redact(message, secret),
        code=redact(str(code), secret)[:80],
        request_id=redact(str(request_id or lowered.get("x-kong-request-id", "")), secret)[:80],
    )


def _retry_after_seconds(headers: Mapping[str, str], now: float) -> float:
    """Seconds to pause after a 429: Retry-After (seconds or HTTP date), else RateLimit-Reset."""
    for name in ("retry-after", "ratelimit-reset"):
        raw = headers.get(name)
        if not raw:
            continue
        try:
            seconds = float(raw)
        except ValueError:
            try:
                seconds = email.utils.parsedate_to_datetime(raw).timestamp() - now
            except (TypeError, ValueError, IndexError):
                continue
        return min(max(seconds, 1.0), RETRY_AFTER_MAX_SECONDS)
    return RETRY_AFTER_FALLBACK_SECONDS


def _exhausted_window_seconds(headers: Mapping[str, str]) -> float | None:
    """Seconds until the gateway window resets, when a response reports no remaining requests."""
    exhausted = False
    for name in ("ratelimit-remaining", "x-ratelimit-remaining-minute"):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            exhausted = exhausted or int(float(raw)) <= 0
        except ValueError:
            continue
    if not exhausted:
        return None
    try:
        return min(max(float(headers.get("ratelimit-reset", "")), 1.0), RETRY_AFTER_MAX_SECONDS)
    except ValueError:
        return RETRY_AFTER_FALLBACK_SECONDS


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class GemiClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        budget: GemiRateBudget | None = None,
        transport: Transport | None = None,
        timeout: float | None = None,
        max_attempts: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self._api_key = settings.GEMI_API_KEY if api_key is None else api_key
        self._base_url = (base_url or settings.GEMI_API_BASE).rstrip("/")
        self._budget = budget if budget is not None else GemiRateBudget(clock=clock, sleep=sleep)
        self._transport = transport or urllib_transport
        self._timeout = float(timeout if timeout is not None else getattr(settings, "GEMI_REQUEST_TIMEOUT_SECONDS", 60))
        attempts = max_attempts if max_attempts is not None else getattr(settings, "GEMI_MAX_ATTEMPTS", 4)
        self._max_attempts = max(1, int(attempts))
        self._sleep = sleep
        self._clock = clock

    def __repr__(self) -> str:
        return f"<GemiClient {self._base_url} api_key={'set' if self._api_key else 'missing'}>"

    def search_companies(
        self, params: Mapping[str, Any], *, lane: GemiLane, max_wait: float | None = None
    ) -> dict[str, Any]:
        """``GET /companies``. A search with no matches is returned as an empty result page."""
        try:
            payload = self.get(SEARCH_PATH, params, lane=lane, max_wait=max_wait)
        except GemiNotFoundError:
            return {
                "searchMetadata": {"totalCount": 0, "resultsOffset": _int(params.get("resultsOffset"), 0), "resultsSize": "0"},
                "searchResults": [],
            }
        if not isinstance(payload, dict):
            raise GemiResponseFormatError("Μη αναμενόμενη μορφή απάντησης αναζήτησης από το GEMI API.", status=200)
        return payload

    def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        lane: GemiLane,
        max_wait: float | None = None,
    ) -> Any:
        """GET ``path`` and return the decoded JSON body (None for an empty body)."""
        if not self._api_key:
            raise GemiConfigurationError(MISSING_KEY_MESSAGE)
        lane = GemiLane(lane)
        wait_limit = DEFAULT_MAX_WAIT_SECONDS[lane] if max_wait is None else float(max_wait)
        url = f"{self._base_url}{path}" + (f"?{urllib.parse.urlencode(params, doseq=True)}" if params else "")
        headers = RequestHeaders(self._api_key)
        failure, status = UNREACHABLE_MESSAGE, None

        for attempt in range(1, self._max_attempts + 1):
            self._budget.acquire(lane, max_wait=wait_limit)
            try:
                response = self._transport(url, headers, self._timeout)
            except GemiTransportError as exc:
                failure, status = UNREACHABLE_MESSAGE, None
                delay = self._backoff(attempt)
                logger.warning(
                    "GEMI %s: %s (%s) on attempt %s/%s.",
                    path, "timeout" if exc.timed_out else "network error", exc.kind, attempt, self._max_attempts,
                )
            else:
                response_headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
                reset_in = _exhausted_window_seconds(response_headers)
                if reset_in is not None:
                    self._budget.defer_until(self._clock() + reset_in, reason=f"rate-limit window exhausted after {path}")
                if 200 <= response.status < 300:
                    logger.debug("GEMI %s -> %s (lane %s, attempt %s).", path, response.status, lane.name, attempt)
                    return self._decode(response)

                status = response.status
                detail = parse_error_body(response.body, response_headers, secret=self._api_key)
                self._raise_if_final(path, status, detail, attempt)
                failure = f"GEMI API HTTP {status}: {detail.message}"
                if status == 429:
                    delay = 0.0  # the shared cooldown below makes the next acquire() wait
                    pause = _retry_after_seconds(response_headers, self._clock())
                    self._budget.defer_until(self._clock() + pause, reason=f"HTTP 429 on {path}")
                else:
                    delay = self._backoff(attempt)
                logger.warning(
                    "GEMI %s: HTTP %s on attempt %s/%s%s.",
                    path, status, attempt, self._max_attempts, f" ({detail.message})" if detail.message else "",
                )
            if attempt < self._max_attempts and delay > 0:
                self._sleep(delay)

        logger.error("GEMI %s: giving up after %s attempts (%s).", path, self._max_attempts, failure)
        raise GemiRetryExhaustedError(failure, status=status, attempts=self._max_attempts)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(BACKOFF_BASE_SECONDS * 3 ** (attempt - 1), BACKOFF_MAX_SECONDS)

    @staticmethod
    def _decode(response: RawResponse) -> Any:
        if not response.body:
            return None
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise GemiResponseFormatError("Μη έγκυρη απάντηση JSON από το GEMI API.", status=response.status) from None

    @staticmethod
    def _raise_if_final(path: str, status: int, detail: GemiErrorDetail, attempt: int) -> None:
        context = {"status": status, "code": detail.code, "request_id": detail.request_id, "attempts": attempt}
        message = f"GEMI API HTTP {status}: {detail.message}"
        if status == 404:
            logger.debug("GEMI %s -> 404.", path)
            raise GemiNotFoundError(message, **context)
        if status == 401:
            raise GemiAuthenticationError(INVALID_KEY_MESSAGE, **context)
        if status == 403:
            raise GemiAuthenticationError(message, **context)
        if status == 400:
            raise GemiBadRequestError(message, **context)
        if status not in RETRYABLE_STATUSES:
            raise GemiApiError(message, **context)


def get_gemi_client() -> GemiClient:
    """A client configured from settings. Cheap to create: all shared state lives in the budget store."""
    return GemiClient()
