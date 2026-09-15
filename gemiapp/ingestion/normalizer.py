"""Normaliser v1: the canonical meaning of one validated ΓΕΜΗ company record.

A2 (gemiapp.ingestion.schemas) answers "is this payload structurally safe to process?". This module
answers "what does this valid record mean?". Its input is a company record that has already passed
``validate_response`` as a ``COMPANY_SEARCH`` item or a ``COMPANY_DETAIL`` payload; structural checks
are not repeated here.

Purity
------
Standard library only: no Django, no database, no GEMI call, no environment variables, no clock, and
the input is never mutated. The one time-dependent fact -- whether an activity is still current -- is
evaluated against an explicit ``as_of`` date supplied by the caller (for Gemi Leads,
``timezone.localdate()`` in Europe/Athens), and ``as_of`` is recorded in the result.

No change to current behaviour
------------------------------
Nothing in the application calls this module yet. The importer still stores
``gemiapp.services.company_defaults()``, Radars still match on stored ``CompanyActivity`` rows, and
digests are unchanged. Moving persistence and matching onto this representation is A6/A7, behind
parity reports.

Version
-------
``GEMI_NORMALIZER_VERSION`` identifies these semantics and is carried on every NormalizedCompany so
future source records, snapshots and debugging reports can name the normaliser that produced them.
Bump it whenever any rule below changes.

Canonical company (NormalizedCompany)
-------------------------------------
* identity: ``ar_gemi``; ``name`` (see the warning below)
* status: ``status`` (reference), ``is_active``
* references: ``legal_type``, ``gemi_office``, ``prefecture``, ``municipality``
* location: ``city``, ``postal_code``, ``street``, ``street_number``
* dates: ``incorporation_date``, ``last_status_change``
* activities: ``activities`` plus ``activities_without_code``
* provenance: ``as_of``, ``normalizer_version``

Text
----
Unicode NFC (which also folds the Greek question mark and ano teleia into their canonical
characters), whitespace collapsed to single spaces and trimmed. Case is preserved. Empty text is
``None`` -- a null, absent or blank source value is always ``None``, never the string "None".

References
----------
Every reference object (status, legal type, GEMI office, prefecture, municipality) becomes
``NormalizedReference(id, description)`` with the two kept apart: ids are identifiers, descriptions are
labels and are never used as keys. Ids become strings (``5`` and ``"5"`` are the same id; leading zeros
in a string id are kept). An absent or null object is ``NormalizedReference(None, None)``. Ids unknown
to any local catalogue are kept as they are.

``is_active``
-------------
``status.isActive`` when the source states it, else a top-level ``isActive``, else ``None``. Live
records carry neither (docs/GEMI_API_CAPABILITY_REPORT.md), so ``None`` -- "not stated by the source" --
is the normal case and must not be read as active. The companyStatuses reference data is what will
resolve it (A5/A6). The current importer defaults it to True, which is why every stored company is
active today.

Dates and DateQuality
---------------------
Each date is a ``NormalizedDate(value, quality, source)``:

* ``VALID`` -- a real calendar date inside the plausible range; ``value`` is set.
* ``MISSING`` -- absent, null or blank.
* ``INVALID`` -- present but not a real ``YYYY-MM-DD`` calendar date (e.g. ``14/09/2026``,
  ``2026-02-30``). An optional time suffix after the date is ignored.
* ``OUT_OF_RANGE`` -- a real calendar date outside the plausible range for its field.

``value`` is set only for ``VALID``. For ``INVALID`` and ``OUT_OF_RANGE`` the raw source text is kept in
``source`` for diagnosis. A date is never replaced by today or any other value.

Plausible range (``DatePolicy``, one place to adjust):

* no date before 1830-01-01, the founding of the modern Greek state; the registry uses 1821-01-01 as a
  placeholder for unknown old dates, which this flags;
* past-event dates (``incorporation_date``, ``last_status_change``) no later than ``as_of`` plus one day
  of tolerance for the gap between the registry's day and ours; the registry also publishes
  impossible values such as 9011-12-09;
* activity period bounds (``dtFrom``, ``dtTo``) may lie in the future, up to 2100-12-31.

Activities and KAD
------------------
Each activity entry with a code becomes a ``NormalizedActivity``:
``code``, ``description``, ``activity_type`` (as published: Κύρια, Δευτερεύουσα, Βοηθητική, Λοιπή...),
``kad_version`` (as published: ``kad_2008``, ``kad_2026`` or ``None``), ``date_from``, ``date_to`` and
``is_current``.

``is_current``, evaluated against ``as_of``:

* ``dtTo`` missing -> ``True``;
* ``dtTo`` a calendar date later than ``as_of`` -> ``True``; on or before ``as_of`` -> ``False``
  (the registry ends a KAD on the same day its successor starts). This also applies when the date is
  ``OUT_OF_RANGE``: ``date_to.quality`` then tells the caller the answer rests on an implausible date;
* ``dtTo`` ``INVALID`` -> ``None`` (cannot be determined).

``dtFrom`` does not affect ``is_current``.

KAD-2008 and KAD-2026 entries are never merged: the same code in both versions is two activities.
Detecting additions and removals, and telling a real change from the 2008 -> 2026 reclassification,
is the signal detector's job, not this module's.

Activity order carries no meaning in the source, so activities are de-duplicated (identical entries
collapse) and sorted by code, KAD version, type, dates and description, which makes equivalent records
compare -- and later serialise and hash -- identically. Entries with no activity code cannot be
represented and are counted in ``activities_without_code`` rather than silently dropped.

Data minimisation
-----------------
Only the fields above are produced. Excluded from the source record: ``persons`` (partners,
shareholders, administrators), ``phone``, ``fax``, ``email``, ``url``, ``poBox``, ``afm``, ``objective``,
``capital``, ``stocks``, ``branch``, ``isBranch``, ``autoRegistered``, ``coNamesEn``, ``coTitlesEl`` and
``coTitlesEn``. Contact data belongs to a later, explicit contact model.

Company name and street address: not approved for history
----------------------------------------------------------
For a sole trader (ΑΤΟΜΙΚΗ) the company name is a natural person's name, and the business address
may be a home address. ``name``, ``street`` and ``street_number`` are normalised because the current
application uses them, but their field metadata marks them ``not_approved`` for historical structures:
snapshot, hashing and diff design must leave them out by default. ``fields_not_approved_for_history()``
lists them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Mapping

GEMI_NORMALIZER_VERSION = 1

HISTORY = "historical_snapshot"
HISTORY_APPROVED = "approved"
HISTORY_NOT_APPROVED = "not_approved"

_MAX_SOURCE_LENGTH = 40


class DateQuality(str, Enum):
    VALID = "valid"
    MISSING = "missing"
    INVALID = "invalid"
    OUT_OF_RANGE = "out_of_range"


@dataclass(frozen=True)
class DatePolicy:
    """The plausible range for GEMI company dates. See the module docstring for the reasoning."""

    earliest: date = date(1830, 1, 1)
    event_future_tolerance: timedelta = timedelta(days=1)
    latest_period_bound: date = date(2100, 12, 31)


DEFAULT_DATE_POLICY = DatePolicy()


@dataclass(frozen=True)
class NormalizedDate:
    value: date | None
    quality: DateQuality
    source: str | None = None  # raw source text, kept only for INVALID and OUT_OF_RANGE

    @property
    def is_reliable(self) -> bool:
        return self.quality is DateQuality.VALID


@dataclass(frozen=True)
class NormalizedReference:
    id: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class NormalizedActivity:
    code: str
    description: str | None
    activity_type: str | None
    kad_version: str | None
    date_from: NormalizedDate
    date_to: NormalizedDate
    is_current: bool | None


def _history(approved: bool) -> dict[str, str]:
    return {HISTORY: HISTORY_APPROVED if approved else HISTORY_NOT_APPROVED}


@dataclass(frozen=True)
class NormalizedCompany:
    ar_gemi: str = field(metadata=_history(True))
    # Not approved for history: for a sole trader this is a natural person's name.
    name: str | None = field(metadata=_history(False))
    status: NormalizedReference = field(metadata=_history(True))
    is_active: bool | None = field(metadata=_history(True))
    legal_type: NormalizedReference = field(metadata=_history(True))
    gemi_office: NormalizedReference = field(metadata=_history(True))
    prefecture: NormalizedReference = field(metadata=_history(True))
    municipality: NormalizedReference = field(metadata=_history(True))
    city: str | None = field(metadata=_history(True))
    postal_code: str | None = field(metadata=_history(True))
    # Not approved for history: a sole trader's business address may be a home address.
    street: str | None = field(metadata=_history(False))
    street_number: str | None = field(metadata=_history(False))
    incorporation_date: NormalizedDate = field(metadata=_history(True))
    last_status_change: NormalizedDate = field(metadata=_history(True))
    activities: tuple[NormalizedActivity, ...] = field(metadata=_history(True))
    activities_without_code: int = field(metadata=_history(True))
    as_of: date = field(metadata=_history(True))
    normalizer_version: int = field(default=GEMI_NORMALIZER_VERSION, metadata=_history(True))

    def as_primitive(self) -> dict[str, Any]:
        """Plain dicts, lists, strings, numbers, booleans and None, in field order: deterministic and
        JSON-serialisable (dates as ISO strings, enums as their values)."""
        return _primitive(self)


def fields_not_approved_for_history() -> frozenset[str]:
    return frozenset(f.name for f in fields(NormalizedCompany) if f.metadata.get(HISTORY) != HISTORY_APPROVED)


def normalize_company(
    record: Mapping[str, Any], *, as_of: date, date_policy: DatePolicy = DEFAULT_DATE_POLICY
) -> NormalizedCompany:
    """Return the canonical representation of one A2-validated GEMI company record."""
    if isinstance(as_of, datetime) or not isinstance(as_of, date):
        raise TypeError("as_of must be a date (not a datetime).")
    ar_gemi = _identifier(record.get("arGemi"))
    if ar_gemi is None:
        raise ValueError("A GEMI company record needs arGemi; validate it with gemiapp.ingestion.schemas first.")
    latest_event = as_of + date_policy.event_future_tolerance
    activities, without_code = _activities(record.get("activities"), as_of=as_of, policy=date_policy)
    return NormalizedCompany(
        ar_gemi=ar_gemi,
        name=_text(record.get("coNameEl")),
        status=_reference(record.get("status")),
        is_active=_is_active(record),
        legal_type=_reference(record.get("legalType")),
        gemi_office=_reference(record.get("gemiOffice")),
        prefecture=_reference(record.get("prefecture")),
        municipality=_reference(record.get("municipality")),
        city=_text(record.get("city")),
        postal_code=_text(record.get("zipCode")),
        street=_text(record.get("street")),
        street_number=_text(record.get("streetNumber")),
        incorporation_date=_date(record.get("incorporationDate"), latest=latest_event, policy=date_policy),
        last_status_change=_date(record.get("lastStatusChange"), latest=latest_event, policy=date_policy),
        activities=activities,
        activities_without_code=without_code,
        as_of=as_of,
    )


# --- helpers ------------------------------------------------------------------------------------

def _text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = " ".join(unicodedata.normalize("NFC", str(value)).split())
    return text or None


def _identifier(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _reference(value: Any) -> NormalizedReference:
    if not isinstance(value, Mapping):
        return NormalizedReference()
    return NormalizedReference(id=_identifier(value.get("id")), description=_text(value.get("descr")))


def _is_active(record: Mapping[str, Any]) -> bool | None:
    status = record.get("status")
    if isinstance(status, Mapping) and isinstance(status.get("isActive"), bool):
        return status["isActive"]
    if isinstance(record.get("isActive"), bool):
        return record["isActive"]
    return None


_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?")


def _calendar_date(value: Any) -> date | None:
    """The calendar date a source value names, regardless of plausibility; None if it names none."""
    if not isinstance(value, str):
        return None
    match = _ISO_DATE.fullmatch(value.strip())
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _date(value: Any, *, latest: date, policy: DatePolicy) -> NormalizedDate:
    if value is None or (isinstance(value, str) and not value.strip()):
        return NormalizedDate(None, DateQuality.MISSING)
    source = str(value).strip()[:_MAX_SOURCE_LENGTH]
    parsed = _calendar_date(value)
    if parsed is None:
        return NormalizedDate(None, DateQuality.INVALID, source)
    if parsed < policy.earliest or parsed > latest:
        return NormalizedDate(None, DateQuality.OUT_OF_RANGE, source)
    return NormalizedDate(parsed, DateQuality.VALID)


def _is_current(raw_date_to: Any, date_to: NormalizedDate, as_of: date) -> bool | None:
    if date_to.quality is DateQuality.MISSING:
        return True
    ends = _calendar_date(raw_date_to)
    if ends is None:
        return None
    return ends > as_of


def _activities(entries: Any, *, as_of: date, policy: DatePolicy) -> tuple[tuple[NormalizedActivity, ...], int]:
    if not isinstance(entries, list):
        return (), 0
    normalized: set[NormalizedActivity] = set()
    without_code = 0
    for entry in entries:
        activity = entry.get("activity") if isinstance(entry, Mapping) else None
        code = _identifier(activity.get("id")) if isinstance(activity, Mapping) else None
        if code is None:
            without_code += 1
            continue
        date_to = _date(entry.get("dtTo"), latest=policy.latest_period_bound, policy=policy)
        normalized.add(NormalizedActivity(
            code=code,
            description=_text(activity.get("descr")),
            activity_type=_text(entry.get("type")),
            kad_version=_text(activity.get("kadVersion")),
            date_from=_date(entry.get("dtFrom"), latest=policy.latest_period_bound, policy=policy),
            date_to=date_to,
            is_current=_is_current(entry.get("dtTo"), date_to, as_of),
        ))
    return tuple(sorted(normalized, key=_activity_sort_key)), without_code


def _date_sort_key(value: NormalizedDate) -> str:
    return f"{value.quality.value}:{value.value.isoformat() if value.value else value.source or ''}"


def _activity_sort_key(activity: NormalizedActivity) -> tuple[str, ...]:
    return (
        activity.code,
        activity.kad_version or "",
        activity.activity_type or "",
        _date_sort_key(activity.date_from),
        _date_sort_key(activity.date_to),
        activity.description or "",
    )


def _primitive(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _primitive(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    return value
