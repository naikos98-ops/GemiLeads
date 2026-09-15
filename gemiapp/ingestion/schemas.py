"""Contracts for successful ΓΕΜΗ Open Data API responses.

A successful response is handed to the application only after its payload matches the contract the
application actually depends on. If the source changes shape -- a container renamed, an identifier
dropped, a date turned into a number -- the call fails with GemiResponseValidationError instead of
letting a partial or misread page reach the importer and, later, snapshots and signals.

Principles
----------
* Only fields the application reads, or that the approved roadmap reads next, are checked. Unknown
  and extra fields are ignored, so additions by the source never break an import.
* A field is *required* only when its absence would corrupt a stored row (e.g. ``arGemi``, the
  company key, or ``searchResults``, whose absence would read as "no companies"). Everything else is
  optional and nullable, as documented or observed (docs/GEMI_API_CAPABILITY_REPORT.md); when present
  it must have a compatible type.
* Values are never copied into errors. A failure reports the location and the expected/actual type.
* Data-quality problems that are not shape problems -- an incorporation date in the year 9011, an
  unknown KAD code -- are not rejected here; they belong to normalisation.

GEMI_RESPONSE_SCHEMA_VERSION identifies this set of contracts. Bump it whenever a contract changes
so stored data and debugging reports can be traced to the contract a payload was accepted under.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Union

from .errors import GemiResponseValidationError

GEMI_RESPONSE_SCHEMA_VERSION = 1


class ResponseFamily(str, Enum):
    COMPANY_SEARCH = "company_search"  # GET /companies
    COMPANY_DETAIL = "company_detail"  # GET /companies/{arGemi}
    ACTIVITIES = "metadata_activities"  # GET /metadata/activities (KAD)
    PREFECTURES = "metadata_prefectures"  # GET /metadata/prefectures
    MUNICIPALITIES = "metadata_municipalities"  # GET /metadata/municipalities
    COMPANY_STATUSES = "metadata_company_statuses"  # GET /metadata/companyStatuses
    LEGAL_TYPES = "metadata_legal_types"  # GET /metadata/legalTypes
    GEMI_OFFICES = "metadata_gemi_offices"  # GET /metadata/gemiOffices
    DECISION_SUBJECTS = "metadata_assembly_subjects"  # GET /metadata/assemblySubjects


@dataclass(frozen=True)
class Kind:
    """A scalar value check. ``expected`` is how an error describes the requirement."""

    expected: str
    accepts: Callable[[Any], bool]


@dataclass(frozen=True)
class Obj:
    fields: tuple["Field", ...]


@dataclass(frozen=True)
class ListOf:
    item: Union[Kind, Obj]


@dataclass(frozen=True)
class Field:
    name: str
    shape: Union[Kind, Obj, ListOf]
    required: bool = False
    nullable: bool = True


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_digits(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, str) and value.isascii() and value.isdigit()


_ISO_DATE_PREFIX = re.compile(r"\d{4}-\d{2}-\d{2}")

TEXT = Kind("string", lambda value: isinstance(value, str))
SCALAR = Kind("string or number", lambda value: isinstance(value, str) or _is_number(value))
BOOLEAN = Kind("boolean", lambda value: isinstance(value, bool))
# GEMI numbers and reference ids arrive as integers on company records and as digit strings in
# reference data; both are accepted, anything else (letters, booleans, objects) is not.
IDENTIFIER = Kind("identifier (integer or digit string)", _is_digits)
COUNT = Kind("non-negative integer", _is_digits)
# An empty string is how the importer already treats a missing date.
DATE = Kind(
    "date string YYYY-MM-DD",
    lambda value: isinstance(value, str) and (value == "" or _ISO_DATE_PREFIX.match(value) is not None),
)


# --- Company record (search items and company detail) -------------------------------------------

_REFERENCE = Obj((Field("id", IDENTIFIER), Field("descr", TEXT)))
_STATUS = Obj((Field("id", IDENTIFIER), Field("descr", TEXT), Field("isActive", BOOLEAN)))
_KAD = Obj((
    # The importer stores the code; an activity object without one cannot be stored meaningfully.
    Field("id", IDENTIFIER, required=True, nullable=False),
    Field("descr", TEXT),
    Field("kadVersion", TEXT),
))
_COMPANY_ACTIVITY = Obj((
    Field("activity", _KAD),
    Field("type", TEXT),
    Field("dtFrom", DATE),
    Field("dtTo", DATE),
))
COMPANY = Obj((
    Field("arGemi", IDENTIFIER, required=True, nullable=False),
    Field("afm", SCALAR),
    Field("coNameEl", TEXT),
    Field("coTitlesEl", ListOf(SCALAR)),
    Field("status", _STATUS),
    Field("isActive", BOOLEAN),
    Field("legalType", _REFERENCE),
    Field("gemiOffice", _REFERENCE),
    Field("prefecture", _REFERENCE),
    Field("municipality", _REFERENCE),
    Field("incorporationDate", DATE),
    Field("lastStatusChange", DATE),
    Field("activities", ListOf(_COMPANY_ACTIVITY)),
    Field("city", SCALAR),
    Field("street", SCALAR),
    Field("streetNumber", SCALAR),
    Field("zipCode", SCALAR),
    Field("email", SCALAR),
    Field("url", SCALAR),
))
_SEARCH_PAGE = Obj((
    # Required: a missing container would otherwise read as "no companies" and end pagination early.
    Field("searchResults", ListOf(COMPANY), required=True, nullable=False),
    Field("searchMetadata", Obj((Field("totalCount", COUNT), Field("resultsOffset", COUNT)))),
))


# --- Reference data -----------------------------------------------------------------------------

def _reference_list(*extra: Field) -> ListOf:
    return ListOf(Obj((
        Field("id", IDENTIFIER, required=True, nullable=False),
        Field("descr", TEXT, required=True, nullable=False),
        Field("descrEn", TEXT),
        Field("lastUpdated", TEXT),
        *extra,
    )))


CONTRACTS: dict[ResponseFamily, Union[Obj, ListOf]] = {
    ResponseFamily.COMPANY_SEARCH: _SEARCH_PAGE,
    ResponseFamily.COMPANY_DETAIL: COMPANY,
    ResponseFamily.ACTIVITIES: _reference_list(Field("kadVersion", TEXT)),
    ResponseFamily.PREFECTURES: _reference_list(),
    ResponseFamily.MUNICIPALITIES: _reference_list(Field("prefectureId", IDENTIFIER)),
    ResponseFamily.COMPANY_STATUSES: _reference_list(Field("isActive", BOOLEAN)),
    ResponseFamily.LEGAL_TYPES: _reference_list(),
    ResponseFamily.GEMI_OFFICES: _reference_list(),
    ResponseFamily.DECISION_SUBJECTS: _reference_list(),
}


# --- Validation ---------------------------------------------------------------------------------

class _Failure(Exception):
    def __init__(self, kind: str, location: str, expected: str, actual: str):
        super().__init__(kind)
        self.kind = kind
        self.location = location
        self.expected = expected
        self.actual = actual


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if _is_number(value):
        return "number"
    return type(value).__name__


def _expected(shape: Union[Kind, Obj, ListOf]) -> str:
    if isinstance(shape, Obj):
        return "object"
    if isinstance(shape, ListOf):
        return "array"
    return shape.expected


def _check(value: Any, shape: Union[Kind, Obj, ListOf], location: str) -> None:
    if isinstance(shape, Obj):
        if not isinstance(value, dict):
            raise _Failure(GemiResponseValidationError.WRONG_TYPE, location, "object", _type_name(value))
        for field in shape.fields:
            _check_field(value, field, location)
    elif isinstance(shape, ListOf):
        if not isinstance(value, list):
            raise _Failure(GemiResponseValidationError.INVALID_CONTAINER, location, "array", _type_name(value))
        for index, item in enumerate(value):
            item_location = f"{location}[{index}]"
            item_ok = isinstance(item, dict) if isinstance(shape.item, Obj) else shape.item.accepts(item)
            if not item_ok:
                raise _Failure(
                    GemiResponseValidationError.INVALID_CONTAINER, item_location, _expected(shape.item), _type_name(item)
                )
            if isinstance(shape.item, Obj):
                _check(item, shape.item, item_location)
    elif not shape.accepts(value):
        raise _Failure(GemiResponseValidationError.WRONG_TYPE, location, shape.expected, _type_name(value))


def _check_field(obj: dict, field: Field, location: str) -> None:
    field_location = field.name if location == "$" else f"{location}.{field.name}"
    if field.name not in obj:
        if field.required:
            raise _Failure(GemiResponseValidationError.MISSING_FIELD, field_location, _expected(field.shape), "missing")
        return
    value = obj[field.name]
    if value is None:
        if field.nullable:
            return
        kind = (
            GemiResponseValidationError.INVALID_CONTAINER
            if isinstance(field.shape, ListOf)
            else GemiResponseValidationError.WRONG_TYPE
        )
        raise _Failure(kind, field_location, _expected(field.shape), "null")
    _check(value, field.shape, field_location)


_SEARCH_ITEM_LOCATION = re.compile(r"searchResults\[(\d+)\]")


def _company_hint(family: ResponseFamily, payload: Any, location: str) -> str:
    """The GEMI number of the failing company, when the payload carries a valid one. A company
    registry number is public and is what an operator needs to reproduce the failure."""
    record = None
    if family is ResponseFamily.COMPANY_DETAIL and isinstance(payload, dict):
        record = payload
    elif family is ResponseFamily.COMPANY_SEARCH and isinstance(payload, dict):
        match = _SEARCH_ITEM_LOCATION.match(location)
        results = payload.get("searchResults")
        if match and isinstance(results, list) and int(match.group(1)) < len(results):
            record = results[int(match.group(1))]
    ar_gemi = record.get("arGemi") if isinstance(record, dict) else None
    return f" (arGemi={ar_gemi})" if _is_digits(ar_gemi) else ""


def validate_response(family: ResponseFamily, payload: Any, *, path: str = "", request_id: str = "") -> None:
    """Raise GemiResponseValidationError unless ``payload`` satisfies the contract for ``family``."""
    family = ResponseFamily(family)
    contract = CONTRACTS[family]
    try:
        top_level_ok = isinstance(payload, dict) if isinstance(contract, Obj) else isinstance(payload, list)
        if not top_level_ok:
            raise _Failure(
                GemiResponseValidationError.INVALID_STRUCTURE, "$", _expected(contract), _type_name(payload)
            )
        _check(payload, contract, "$")
    except _Failure as failure:
        raise GemiResponseValidationError(
            f"Η απάντηση του GEMI API στο {path or '?'} δεν ταιριάζει με το αναμενόμενο σχήμα "
            f"({family.value} v{GEMI_RESPONSE_SCHEMA_VERSION}): {failure.kind} στο {failure.location}"
            f"{_company_hint(family, payload, failure.location)} — αναμενόταν {failure.expected}, "
            f"βρέθηκε {failure.actual}.",
            family=family.value,
            schema_version=GEMI_RESPONSE_SCHEMA_VERSION,
            kind=failure.kind,
            location=failure.location,
            path=path,
            request_id=request_id,
        ) from None
