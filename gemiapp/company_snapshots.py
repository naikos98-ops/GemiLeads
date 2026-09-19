"""Minimised company snapshots (B3): the canonical historical state layer.

A snapshot is what Gemi Leads knew about one company's relevant **business state** at one observation. It
is not the GEMI payload, not ``Company.raw_data``, not a copy of every field, not customer data and not a
signal. B3 stores state and nothing else: no detector runs here and no signal is ever emitted.

Built from A3
-------------
The builder consumes the A3 ``NormalizedCompany`` produced by ``normalize_company`` -- the one normalisation
system -- and never re-interprets GEMI JSON itself. A2 has already validated the response, A3 has already
decided date quality and currentness, and A6/A7 already use the same semantics.

Canonical state (schema version 1)
----------------------------------
Exactly these facts take part, and nothing else:

* ``status_source_id``, ``last_status_change``, ``last_status_change_quality``
* ``legal_type_source_id``
* ``gemi_office_source_id``
* ``prefecture_source_id``, ``municipality_source_id``, ``city``, ``postal_code``
* ``incorporation_date``, ``incorporation_date_quality``
* ``current_activities`` (below)
* ``unknown_current_activity_count``

Identity is carried by source **ids**, never by descriptions: a re-worded status, legal form or KAD
description is reference metadata, not a change of company state, and must not look like one. The company
name, VAT number, street, address, contact details, persons, capital and stocks are never part of a
snapshot at all.

``unknown_current_activity_count`` does take part in the hash: a source that stops saying whether an
activity is current has changed the quality of what we can observe, and that deserves a new state row even
though no individual activity moved.

Current activities
------------------
The A3/A7 canonical currentness decides membership, evaluated on the fresh observation:

* ``is_current is True``  -> in the current set;
* ``is_current is False`` -> excluded (ended);
* ``is_current is None``  -> **not** verified current, so excluded, and counted in
  ``unknown_current_activity_count`` instead of silently joining the set.

The A7 customer-facing KAD-2026-only restriction is deliberately **not** applied here. Snapshots are source
truth: a KAD 2008 activity the source still reports as current stays current, and the KAD version is part of
an activity's identity. The same code in KAD 2008 and KAD 2026 is two distinct entries, never collapsed,
because B5 needs that evidence to tell a genuine KAD change from the 2008 -> 2026 taxonomy transition. B3
itself implements no transition suppression.

Each entry holds only deterministic source facts: code, KAD version, normalised activity type, the published
type (kept so two genuinely different published types cannot collapse), and ``dtFrom`` / ``dtTo`` with their
A3 qualities -- value only when VALID, never clamped.

The two type fields absorb different amounts of variance, and the difference is deliberate.
``activity_type`` is the A7 canonical vocabulary value (primary / secondary / auxiliary / other / unknown),
reached through ``normalize_kad_search``, which strips Greek accents and uppercases, so it is stable across
case and accent variance. ``source_activity_type`` is the A3 ``normalize_text`` value, which applies NFC and
collapses whitespace but **preserves case and accents**. A whitespace-only or Unicode-form-only difference in
the published type therefore does not change the state, while a case-only or accent-only difference does
produce a new state row even though the canonical type is unchanged. B3 records that the source's own wording
of the type changed and judges nothing about it; distinguishing cosmetic re-wording from a real change is
B5's work, with the canonical ``activity_type`` available beside it for exactly that comparison. Descriptions, database ids, ``current_as_of`` and the
legacy ``legacy_listed`` flag are all excluded. Membership already means verified-current, so ``is_current``
is not repeated inside entries.

Entries are sorted by (code, KAD version, normalised type, published type, ``dtFrom``, ``dtTo``) with nulls
last, so source ordering can never affect the hash, and exactly identical canonical entries collapse.

State hash
----------
SHA-256 over canonical JSON of the state above: UTF-8, sorted keys, compact separators, ``ensure_ascii``
false, ``allow_nan`` false, dates as ISO-8601 strings, no floats. Observation and persistence metadata are
**excluded by construction** -- the hash is computed from the state mapping alone, which never contains the
row id, ``observed_at``, ``last_observed_at``, ``created_at``, the source record, the normalizer version,
the schema version, the baseline flag or any related row's id. Two observations of identical business state
at different times therefore hash identically.

This is a different question from A4's ``payload_hash``: that asks "what exact JSON did GEMI return?", this
asks "what canonical business state did it represent?".

Branch state
------------
``isBranch`` is not part of the A2-validated, A3-normalised contract, so B3 does not read it from raw data
just because the upstream response carries it. Branch state is deferred to the future branch/corporate-event
work, which will normalise it first.

Writer
------
``record_company_snapshot`` verifies the normalised record belongs to the company, builds the state, hashes
it, and then, inside one transaction holding a lock on the latest snapshot:

* no prior snapshot -> create the **baseline** (``is_baseline=True``);
* same hash as the latest -> create nothing and move ``last_observed_at`` forward only;
* different hash -> create a new non-baseline snapshot.

A baseline never means "the company changed"; it only establishes comparison history. Nothing here infers
what changed, and no signal is emitted: that is B5's work.

Two callers observe companies: the B4 refresh collector (fresh GEMI observations) and the B2 NEW_COMPANY
producer, which records a newly discovered company's **detection-time baseline** from the importer's stored
record -- only when that record passes the A2 ``company_search`` contract, belongs to the company, was not
edited through the admin and has an A6 observation time -- so the signal can be detected with canonical state
(``gemiapp.new_company_signals``). Both go through this writer; no baseline is ever built from any other stored
``raw_data``.

``observed_at`` must be supplied and timezone-aware -- there is no hidden ``timezone.now()`` fallback, and it
is never the incorporation date, the status-change date or the row's creation time. An observation older
than the current state's span is rejected rather than allowed to rewrite chronology.

State may legitimately return to an earlier value (A -> B -> A), so there is no unique constraint on
(company, state_hash): only *consecutive* identical observations collapse, and a recurrence is its own row.

Concurrency
-----------
The writer re-reads the latest snapshot under ``select_for_update`` inside the transaction, so two workers
observing the same state concurrently produce one row on PostgreSQL. SQLite does not reproduce those row
locks faithfully, so the tests exercise the re-check path structurally and this invariant is documented
rather than claimed to be proven by SQLite.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

from django.apps import apps
from django.db import transaction
from django.utils import timezone

from .ingestion.activities import normalize_activity_type
from .ingestion.normalizer import GEMI_NORMALIZER_VERSION, DateQuality, NormalizedCompany

logger = logging.getLogger(__name__)

COMPANY_SNAPSHOT_SCHEMA_VERSION = 1

# The A4 families a company snapshot may cite as provenance.
COMPANY_SOURCE_FAMILIES = frozenset({"company_search", "company_detail"})

BASELINE_CREATED = "baseline_created"
CHANGED_CREATED = "changed_created"
UNCHANGED = "unchanged"

STATE_KEYS = (
    "status_source_id", "last_status_change", "last_status_change_quality", "legal_type_source_id",
    "gemi_office_source_id", "prefecture_source_id", "municipality_source_id", "city", "postal_code",
    "incorporation_date", "incorporation_date_quality", "current_activities", "unknown_current_activity_count",
)
ACTIVITY_STATE_KEYS = (
    "code", "kad_version", "activity_type", "source_activity_type", "date_from", "date_from_quality",
    "date_to", "date_to_quality",
)
# The scalar state columns of CompanySnapshot, i.e. every state key except the activity collection.
SCALAR_STATE_KEYS = tuple(key for key in STATE_KEYS if key != "current_activities")


class SnapshotChronologyError(RuntimeError):
    """An observation older than the current state's span: accepting it would rewrite history."""


@dataclass(frozen=True)
class SnapshotResult:
    status: str
    snapshot: Any
    state_hash: str
    span_extended: bool = False

    @property
    def created(self) -> bool:
        return self.status in (BASELINE_CREATED, CHANGED_CREATED)


def _date_value(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _sort_key(entry: Mapping[str, Any]) -> tuple:
    # (value is None, value) puts nulls last deterministically for every component.
    return tuple(
        item
        for key in ("code", "kad_version", "activity_type", "source_activity_type", "date_from", "date_to")
        for item in ((entry[key] is None), entry[key] or "")
    )


def _activity_state(activity) -> dict[str, Any]:
    return {
        "code": activity.code,
        # Part of identity: the same code in KAD 2008 and KAD 2026 is two activities.
        "kad_version": activity.kad_version,
        # A7 canonical vocabulary: accent- and case-insensitive, so cosmetic variance cannot move it.
        "activity_type": normalize_activity_type(activity.activity_type),
        # The A3 text: NFC and collapsed whitespace, but case and accents preserved. Kept so two genuinely
        # different published types cannot collapse; a case-only change is therefore a state change here.
        "source_activity_type": activity.activity_type,
        "date_from": _date_value(activity.date_from.value),
        "date_from_quality": activity.date_from.quality.value,
        "date_to": _date_value(activity.date_to.value),
        "date_to_quality": activity.date_to.quality.value,
    }


def build_company_snapshot_state(normalized: NormalizedCompany) -> dict[str, Any]:
    """The canonical minimised business state of one normalised observation. Pure: no database access, and
    the input is never mutated."""
    if not isinstance(normalized, NormalizedCompany):
        raise TypeError("build_company_snapshot_state expects an A3 NormalizedCompany")
    current: list[dict[str, Any]] = []
    unknown = 0
    for activity in normalized.activities:
        if activity.is_current is True:
            current.append(_activity_state(activity))
        elif activity.is_current is None:
            unknown += 1
    unique = {json.dumps(entry, sort_keys=True): entry for entry in current}
    return {
        "status_source_id": normalized.status.id,
        "last_status_change": _date_value(normalized.last_status_change.value),
        "last_status_change_quality": normalized.last_status_change.quality.value,
        "legal_type_source_id": normalized.legal_type.id,
        "gemi_office_source_id": normalized.gemi_office.id,
        "prefecture_source_id": normalized.prefecture.id,
        "municipality_source_id": normalized.municipality.id,
        "city": normalized.city,
        "postal_code": normalized.postal_code,
        "incorporation_date": _date_value(normalized.incorporation_date.value),
        "incorporation_date_quality": normalized.incorporation_date.quality.value,
        "current_activities": sorted(unique.values(), key=_sort_key),
        "unknown_current_activity_count": unknown,
    }


def canonical_state_json(state: Mapping[str, Any]) -> str:
    return json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def company_state_hash(state: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical state JSON. Only business facts enter; no metadata is present to exclude."""
    return hashlib.sha256(canonical_state_json(state).encode("utf-8")).hexdigest()


def latest_company_snapshot(company, *, for_update: bool = False):
    """The company's most recent snapshot: the one canonical predecessor selection, reused by B5."""
    CompanySnapshot = apps.get_model("gemiapp", "CompanySnapshot")
    queryset = CompanySnapshot.objects.filter(company=company).order_by("-observed_at", "-id")
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.first()


def _row_values(state: Mapping[str, Any]) -> dict[str, Any]:
    values = {key: state[key] for key in SCALAR_STATE_KEYS}
    values["activities_state"] = state["current_activities"]
    return values


def record_company_snapshot(company, normalized: NormalizedCompany, observed_at: datetime, source_record=None) -> SnapshotResult:
    """Persist the canonical state of one fresh observation. See the module docstring."""
    CompanySnapshot = apps.get_model("gemiapp", "CompanySnapshot")
    if not isinstance(observed_at, datetime):
        raise TypeError("observed_at must be a timezone-aware datetime")
    if timezone.is_naive(observed_at):
        raise ValueError("observed_at must be timezone-aware")
    if normalized.ar_gemi != company.gemi_number:
        raise ValueError(
            f"normalised record {normalized.ar_gemi} does not belong to company {company.gemi_number}"
        )
    if source_record is not None and source_record.family not in COMPANY_SOURCE_FAMILIES:
        # A4 does not persist an identifier that proves which company a response was about, so this checks
        # the family only and claims nothing more.
        raise ValueError(f"a company snapshot cannot cite a {source_record.family} source record")

    state = build_company_snapshot_state(normalized)
    state_hash = company_state_hash(state)
    with transaction.atomic():
        latest = latest_company_snapshot(company, for_update=True)
        if latest is not None and observed_at < latest.observed_at:
            raise SnapshotChronologyError(
                f"observation at {observed_at.isoformat()} precedes the current state's span; refusing to rewrite history"
            )
        if latest is not None and latest.state_hash == state_hash:
            if observed_at < latest.last_observed_at:
                raise SnapshotChronologyError(
                    f"observation at {observed_at.isoformat()} precedes last_observed_at; refusing to rewrite history"
                )
            if observed_at == latest.last_observed_at:
                return SnapshotResult(UNCHANGED, latest, state_hash)
            latest.last_observed_at = observed_at
            latest.save(update_fields=["last_observed_at"])
            return SnapshotResult(UNCHANGED, latest, state_hash, span_extended=True)
        if latest is not None and observed_at <= latest.last_observed_at:
            raise SnapshotChronologyError(
                f"observation at {observed_at.isoformat()} is not newer than the current state's span"
            )
        snapshot = CompanySnapshot.objects.create(
            company=company, schema_version=COMPANY_SNAPSHOT_SCHEMA_VERSION,
            normalizer_version=normalized.normalizer_version or GEMI_NORMALIZER_VERSION, state_hash=state_hash,
            observed_at=observed_at, last_observed_at=observed_at, is_baseline=latest is None,
            source_record=source_record, **_row_values(state),
        )
    status = BASELINE_CREATED if snapshot.is_baseline else CHANGED_CREATED
    logger.info(
        "Company snapshot %s: company=%s hash=%s activities=%s",
        status, company.pk, state_hash[:12], len(state["current_activities"]),
    )
    return SnapshotResult(status, snapshot, state_hash)
