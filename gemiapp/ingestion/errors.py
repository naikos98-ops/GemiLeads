"""Exceptions raised by the shared GEMI client.

Every class subclasses RuntimeError, which is what ``gemiapp.services._get`` raised before the
client existed, so callers that catch RuntimeError or Exception -- and ``ImportRun.error_message``,
which stores ``str(exc)`` -- keep working unchanged. No message ever contains the API key.
"""

from __future__ import annotations


class GemiApiError(RuntimeError):
    """Base class for every failure of a GEMI Open Data API call."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str = "",
        request_id: str = "",
        attempts: int = 0,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.attempts = attempts


class GemiConfigurationError(GemiApiError):
    """GEMI_API_KEY is not configured. Nothing was sent."""


class GemiAuthenticationError(GemiApiError):
    """HTTP 401/403: the gateway rejected the key. Not retried."""


class GemiBadRequestError(GemiApiError):
    """HTTP 400: request validation failed (e.g. resultsSize above 200). Not retried."""


class GemiNotFoundError(GemiApiError):
    """HTTP 404 outside a search: the requested company or resource does not exist."""


class GemiResponseFormatError(GemiApiError):
    """A successful response whose body is not valid JSON."""


class GemiRetryExhaustedError(GemiApiError):
    """Every attempt ended in a retryable failure: 429, a transient 5xx or a network error."""


class GemiBudgetTimeoutError(GemiApiError):
    """No request slot became available within the caller's maximum wait."""


class GemiBudgetUnavailableError(GemiApiError):
    """The shared budget store could not be read or written. Requests are refused (fail closed)."""
