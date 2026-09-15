"""Minimised GEMI source records: auditable provenance for validated GEMI responses.

Switch
------
``GEMI_SOURCE_RECORDS_ENABLED`` (default off). While off, ``get_gemi_client()`` builds a client
without a recorder: nothing is written and the importer behaves exactly as before. While on, every
successful response that ``GemiClient.get()`` validated against an A2 response family is recorded
after validation and before the payload is returned to the importer. Error responses, the synthesised
empty page for a 404 search, and calls made without a response family are not recorded.

Failure semantics
-----------------
With the switch on, provenance is mandatory: if the record cannot be built or written the call raises
GemiSourceRecordError and the importer stops before writing any company row of that run
(fetch_companies collects every page before persisting). Records already written for earlier pages
stay -- they describe responses that really were received. This is the Gemi Leads 2.0 cutover
behaviour; the legacy production importer is protected by the switch being off.

What is stored (gemiapp.models.GemiSourceRecord)
------------------------------------------------
Source and family, the A2 response family, endpoint path, canonical request parameters, a request
fingerprint, an observation key, fetch time, HTTP status, gateway request id, payload hash, result
count, the response-schema / normaliser / record-format versions, an optional sanitised payload,
retention class and expiry. Never: the raw response, the API key, request headers.

What is hashed
--------------
``payload_hash`` is the SHA-256 of the *decoded JSON body* of the successful, validated response,
serialised canonically in memory: object keys sorted, no insignificant whitespace, UTF-8, non-ASCII
kept as is. Array order is preserved because at this level it is part of what the source returned.
Headers, the URL and the API key are not part of the body and never enter the hash. The body is
hashed whole -- including persons and contact fields -- so a change anywhere in the source is
detectable, but that body is never written. The hash is for provenance and change detection, not for
authentication.

Sanitised payload
-----------------
Built only when ``GEMI_SOURCE_RECORDS_STORE_PAYLOAD`` is explicitly turned on. The default is off:
metadata and the payload hash are the minimum safe provenance until storage has been sized on staging.
When on:

* company search -> ``{"as_of", "search_metadata": {"total_count", "results_offset"}, "companies": [...]}``
* company detail -> ``{"as_of", "company": {...}}``

where each company is the A3 ``NormalizedCompany`` primitive minus the fields not approved for
history (``name``, ``street``, ``street_number``) and minus the per-company ``as_of`` and
``normalizer_version`` (stored once, on the record). ``normalizer_version`` is therefore set on the
record only when the normaliser produced the stored payload. Persons, phone, fax, email, url, afm and
every other field A3 excludes never reach the database.

* reference data -> ``{"items": [...]}`` keeping only ``id``, ``descr``, ``descrEn``, ``lastUpdated``,
  ``kadVersion``, ``prefectureId``, ``isActive``. No normaliser runs, so ``normalizer_version`` is null.
  GEMI office contact fields are dropped.

The document-metadata family is reserved in the model; nothing records it until document ingestion
exists.

Request parameters
------------------
Canonicalised and allow-listed. Filter, paging and sort parameters are kept; list filters become
sorted id lists (so ``"54,5"`` and ``["5", "54"]`` are the same request). ``afm`` and ``name`` can
identify a natural person, so only their presence is kept (``"[redacted]"``). Every other key --
including anything like ``api_key``, a token or an authorization value -- is dropped.

Observations versus retries
---------------------------
``request_fingerprint`` identifies the request (endpoint + canonical parameters). ``observation_key``
adds the response family, the payload hash and the observation window
(``GEMI_SOURCE_RECORD_OBSERVATION_WINDOW_SECONDS``, default one hour) and is unique:

* a retried task that gets the same response to the same request in the same window finds the existing
  record and inserts nothing;
* the same response observed in a later window is a new record -- "unchanged at that time" is a fact;
* a different response, or a different request, is always a new record.

A retry that happens to straddle a window boundary yields two records; both are genuine observations.

Retention
---------
One mapping decides the class per family (``RETENTION_BY_FAMILY``) and one setting the days per class
(``GEMI_SOURCE_RECORD_RETENTION_DAYS``, defaults short 7, standard 30, audit 365). ``expires_at`` is
fixed when the record is written. These are technical defaults; final periods follow the approved
legal/DPO retention policy. ``purge_expired_source_records`` deletes records whose ``expires_at`` is
at or before now, in batches, logging counts only; it touches no other table.

Legacy note
-----------
``Company.raw_data`` still stores the full search item, persons included. A4 neither reads nor extends
it; its treatment is a separate compliance migration.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Mapping

from django.conf import settings
from django.utils import timezone

from .errors import GemiSourceRecordError
from .normalizer import GEMI_NORMALIZER_VERSION, fields_not_approved_for_history, normalize_company
from .schemas import GEMI_RESPONSE_SCHEMA_VERSION, ResponseFamily

logger = logging.getLogger(__name__)

GEMI_SOURCE_RECORD_FORMAT_VERSION = 1
SOURCE_GEMI_OPENDATA = "gemi_opendata"

SOURCE_FAMILY_BY_RESPONSE: dict[ResponseFamily, str] = {
    ResponseFamily.COMPANY_SEARCH: "company_search",
    ResponseFamily.COMPANY_DETAIL: "company_detail",
    ResponseFamily.ACTIVITIES: "reference_data",
    ResponseFamily.PREFECTURES: "reference_data",
    ResponseFamily.MUNICIPALITIES: "reference_data",
    ResponseFamily.COMPANY_STATUSES: "reference_data",
    ResponseFamily.LEGAL_TYPES: "reference_data",
    ResponseFamily.GEMI_OFFICES: "reference_data",
    ResponseFamily.DECISION_SUBJECTS: "reference_data",
}

RETENTION_BY_FAMILY: dict[str, str] = {
    "company_search": "standard",
    "company_detail": "standard",
    "reference_data": "audit",
    "document_metadata": "short",
}

_LIST_PARAMS = frozenset({"activities", "gemiOffices", "legalTypes", "municipalities", "prefectures", "statuses"})
_SCALAR_PARAMS = frozenset({"arGemi", "isActive", "resultsOffset", "resultsSize", "resultsSortBy"})
_REDACTED_PARAMS = frozenset({"afm", "name"})
REDACTED = "[redacted]"

_REFERENCE_KEYS = ("id", "descr", "descrEn", "lastUpdated", "kadVersion", "prefectureId", "isActive")
_EXCLUDED_COMPANY_KEYS = fields_not_approved_for_history() | {"as_of", "normalizer_version"}


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def canonical_request_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    items = {str(key): value for key, value in (params or {}).items()}
    canonical: dict[str, Any] = {}
    for key in sorted(items):
        value = items[key]
        if key in _REDACTED_PARAMS:
            canonical[key] = REDACTED
        elif key in _LIST_PARAMS:
            canonical[key] = _id_list(value)
        elif key in _SCALAR_PARAMS:
            canonical[key] = _scalar(value)
        # Any other key -- api_key, tokens, authorization values, unknown parameters -- is not kept.
    return canonical


def _id_list(value: Any) -> list[str]:
    parts = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
    ids = {str(part).strip() for part in parts if str(part).strip()}
    return sorted(ids, key=lambda item: (len(item), item))


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def request_fingerprint(endpoint: str, canonical_params: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json({"endpoint": endpoint, "params": canonical_params})).hexdigest()


def observation_key(
    *, source: str, response_family: ResponseFamily, fingerprint: str, digest: str, fetched_at: datetime, window_seconds: int
) -> str:
    window = int(fetched_at.timestamp() // max(1, window_seconds))
    return hashlib.sha256(canonical_json({
        "source": source, "response_family": ResponseFamily(response_family).value,
        "request": fingerprint, "payload": digest, "window": window,
    })).hexdigest()


def result_count(response_family: ResponseFamily, payload: Any) -> int | None:
    if response_family is ResponseFamily.COMPANY_SEARCH:
        return len(payload.get("searchResults") or [])
    if response_family is ResponseFamily.COMPANY_DETAIL:
        return 1
    return len(payload) if isinstance(payload, list) else None


def sanitise_payload(response_family: ResponseFamily, payload: Any, *, as_of) -> tuple[dict[str, Any], int | None]:
    """Return (sanitised payload, normaliser version that produced it or None)."""
    response_family = ResponseFamily(response_family)
    if response_family is ResponseFamily.COMPANY_SEARCH:
        metadata = payload.get("searchMetadata") or {}
        return {
            "as_of": as_of.isoformat(),
            "search_metadata": {
                "total_count": _count(metadata.get("totalCount")),
                "results_offset": _count(metadata.get("resultsOffset")),
            },
            "companies": [_company(item, as_of) for item in payload.get("searchResults") or []],
        }, GEMI_NORMALIZER_VERSION
    if response_family is ResponseFamily.COMPANY_DETAIL:
        return {"as_of": as_of.isoformat(), "company": _company(payload, as_of)}, GEMI_NORMALIZER_VERSION
    return {
        "items": [{key: item[key] for key in _REFERENCE_KEYS if key in item} for item in payload],
    }, None


def _company(record: Mapping[str, Any], as_of) -> dict[str, Any]:
    primitive = normalize_company(record, as_of=as_of).as_primitive()
    return {key: value for key, value in primitive.items() if key not in _EXCLUDED_COMPANY_KEYS}


def _count(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def retention_expires_at(retention_class: str, fetched_at: datetime, *, retention_days: Mapping[str, int] | None = None) -> datetime:
    days = (retention_days if retention_days is not None else settings.GEMI_SOURCE_RECORD_RETENTION_DAYS)[retention_class]
    return fetched_at + timedelta(days=max(0, int(days)))


class SourceRecorder:
    """Writes one GemiSourceRecord per observed response. get_gemi_client() creates one only while
    GEMI_SOURCE_RECORDS_ENABLED is on."""

    def __init__(
        self,
        *,
        store_payload: bool | None = None,
        retention_days: Mapping[str, int] | None = None,
        window_seconds: int | None = None,
    ):
        self.store_payload = settings.GEMI_SOURCE_RECORDS_STORE_PAYLOAD if store_payload is None else store_payload
        self.retention_days = retention_days
        self.window_seconds = (
            settings.GEMI_SOURCE_RECORD_OBSERVATION_WINDOW_SECONDS if window_seconds is None else window_seconds
        )

    def record(
        self,
        *,
        response_family: ResponseFamily,
        endpoint: str,
        params: Mapping[str, Any] | None,
        http_status: int,
        payload: Any,
        fetched_at: datetime,
        request_id: str = "",
    ):
        """Record one validated response. Returns (record, created)."""
        from ..models import GemiSourceRecord

        try:
            response_family = ResponseFamily(response_family)
            family = SOURCE_FAMILY_BY_RESPONSE[response_family]
            retention_class = RETENTION_BY_FAMILY[family]
            params_canonical = canonical_request_params(params)
            fingerprint = request_fingerprint(endpoint, params_canonical)
            digest = payload_hash(payload)
            sanitised, normalizer_version = (None, None)
            if self.store_payload:
                sanitised, normalizer_version = sanitise_payload(
                    response_family, payload, as_of=timezone.localdate(fetched_at)
                )
            record, created = GemiSourceRecord.objects.get_or_create(
                observation_key=observation_key(
                    source=SOURCE_GEMI_OPENDATA, response_family=response_family, fingerprint=fingerprint,
                    digest=digest, fetched_at=fetched_at, window_seconds=self.window_seconds,
                ),
                defaults={
                    "source": SOURCE_GEMI_OPENDATA,
                    "family": family,
                    "response_family": response_family.value,
                    "endpoint": endpoint[:255],
                    "request_params": params_canonical,
                    "request_fingerprint": fingerprint,
                    "fetched_at": fetched_at,
                    "http_status": http_status,
                    "gateway_request_id": (request_id or "")[:80],
                    "payload_hash": digest,
                    "result_count": result_count(response_family, payload),
                    "response_schema_version": GEMI_RESPONSE_SCHEMA_VERSION,
                    "normalizer_version": normalizer_version,
                    "record_format_version": GEMI_SOURCE_RECORD_FORMAT_VERSION,
                    "sanitised_payload": sanitised,
                    "retention_class": retention_class,
                    "expires_at": retention_expires_at(retention_class, fetched_at, retention_days=self.retention_days),
                },
            )
        except Exception as exc:
            logger.error(
                "GEMI source record for %s (%s) could not be written (%s); the call fails because source "
                "records are enabled.",
                endpoint, getattr(response_family, "value", response_family), type(exc).__name__,
            )
            raise GemiSourceRecordError(
                "Η καταγραφή προέλευσης (GemiSourceRecord) για την απάντηση του GEMI API απέτυχε· η κλήση "
                "σταμάτησε επειδή οι source records είναι ενεργές."
            ) from None
        logger.debug(
            "GEMI source record %s for %s (%s): %s.",
            record.pk, endpoint, response_family.value, "created" if created else "same observation already recorded",
        )
        return record, created


def purge_expired_source_records(*, now: datetime | None = None, batch_size: int = 1000, dry_run: bool = False) -> int:
    """Delete GemiSourceRecord rows whose expires_at is at or before ``now``. Returns the number
    deleted (or, with ``dry_run``, the number that would be). Safe to run repeatedly."""
    from ..models import GemiSourceRecord

    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    now = now or timezone.now()
    expired = GemiSourceRecord.objects.filter(expires_at__lte=now)
    if dry_run:
        count = expired.count()
        logger.info("GEMI source records purge (dry run): %s expired record(s) would be deleted.", count)
        return count
    deleted = batches = 0
    while True:
        ids = list(expired.order_by("pk").values_list("pk", flat=True)[:batch_size])
        if not ids:
            break
        deleted += GemiSourceRecord.objects.filter(pk__in=ids).delete()[0]
        batches += 1
    logger.info("GEMI source records purge: %s expired record(s) deleted in %s batch(es).", deleted, batches)
    return deleted
