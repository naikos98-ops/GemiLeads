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


class GemiResponseValidationError(GemiApiError):
    """A successful response whose JSON does not match the contract the application depends on.

    Raised at the ingestion boundary (gemiapp.ingestion.schemas) before any of the payload reaches
    the application, so nothing from the response has been stored. Not retried: a malformed payload
    is not a transient transport failure, and the next scheduled run fetches the data again.

    The message and attributes name the endpoint, response family, schema version and the location
    that failed, plus the expected and actual *types* -- never payload values, which can include
    natural persons and contact data.
    """

    INVALID_STRUCTURE = "invalid_structure"  # the top-level payload is not the expected object/array
    MISSING_FIELD = "missing_field"  # a field the application depends on is absent
    WRONG_TYPE = "wrong_type"  # a field is present with an incompatible type or format
    INVALID_CONTAINER = "invalid_container"  # a collection is not an array, or holds incompatible items

    def __init__(
        self,
        message: str,
        *,
        family: str,
        schema_version: int,
        kind: str,
        location: str,
        path: str = "",
        request_id: str = "",
    ):
        super().__init__(message, status=200, code=kind, request_id=request_id)
        self.family = family
        self.schema_version = schema_version
        self.kind = kind
        self.location = location
        self.path = path


class GemiRetryExhaustedError(GemiApiError):
    """Every attempt ended in a retryable failure: 429, a transient 5xx or a network error."""


class GemiBudgetTimeoutError(GemiApiError):
    """No request slot became available within the caller's maximum wait."""


class GemiBudgetUnavailableError(GemiApiError):
    """The shared budget store could not be read or written. Requests are refused (fail closed)."""
