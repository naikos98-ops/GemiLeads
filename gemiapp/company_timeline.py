"""Company timeline read model (B6): a company as an evolving entity.

The timeline answers: *what verified business events have we observed for this company, in what order, from
what evidence, and with what source-effective date where available?*

A read model, not another event table
-------------------------------------
``CompanySignal`` is the one authoritative event record. B1 owns its identity and dedupe, B2 and B5 own its
production, and each producer keeps its own structured provenance beside it. A ``CompanyTimelineEvent`` table
would be a second copy of the same facts that must be kept in sync with every producer, rerun and future
promotion, and would drift the first time one of them changed. So B6 stores nothing: ``get_company_timeline``
reads ``CompanySignal`` with ``CompanySignalDiscoveryEvidence`` and ``CompanySignalSnapshotEvidence`` and
returns frozen ``CompanyTimelineEntry`` values. Editing an entry cannot change the database, and there is no
migration, task, schedule, URL, view or template.

One entry per signal
--------------------
Every ``CompanySignal`` becomes exactly one entry, and B6 never deduplicates or merges events itself. Signals
stay the atomic business events: filtering by type stays trivial, pagination needs no invented aggregate
identity, and a future presentation layer can still group entries.

Grouping
--------
Several B5 signals can come from one fresh observation. They share ``group_key`` without being collapsed:

* snapshot-diff signals: ``snapshot:<current snapshot id>`` -- one transition, one key; a later transition of
  the same company gets a new key;
* NEW_COMPANY: ``discovery:<signal id>`` -- a first observation is its own moment;
* any entry whose provenance is not COMPLETE: ``signal:<signal id>``, so unverified evidence never groups
  events together.

The key is derived on read; nothing is persisted for it.

Ordering and time
-----------------
Canonical chronology is ``detected_at`` descending, then signal id descending. ``detected_at`` is the exact
moment Gemi Leads first observed the event and is always present. The source-effective time
(``effective_date`` / ``effective_at`` with ``effective_precision``) is exposed separately and never used for
ordering: it may be a date without a time, or missing, and it is never turned into a midnight timestamp. A
late publication's NEW_COMPANY entry therefore sits where Gemi Leads discovered it, not at its older
incorporation date.

Mode is explicit
----------------
Current signals are SHADOW -- validation data, not approved customer facts. A request reads exactly one mode
(SHADOW by default, or LIVE); the two are never mixed by accident. Reading every mode together requires the
deliberate ``ALL_MODES`` option, meant for internal debugging only. There is no customer endpoint.

Provenance adapters
-------------------
Each supported (signal type, source type) pair has an adapter that exposes the structured facts its producer
recorded, never re-deriving them:

* NEW_COMPANY from DISCOVERY: the discovery classification and observation id from B2's evidence. The event
  dates stay the signal's own; the raw discovery payload is never read.
* STATUS_CHANGED, LEGAL_FORM_CHANGED, LOCATION_CHANGED, KAD_ADDED, KAD_REMOVED from SNAPSHOT_DIFF: the subject
  kind with before/after source ids or the KAD code and version from B5's evidence. ``activities_state`` is not
  parsed again.

Only stable identifiers are exposed. Status, legal-form, municipality and KAD descriptions are not resolved:
the timeline must not depend on A5 reference data having been synced, and its identity must not change when
wording does. A presentation layer may resolve names later, outside this service.

Integrity policy
----------------
``provenance_status`` never lets a timeline pretend its evidence is complete:

* COMPLETE -- the expected evidence exists and agrees with the signal;
* MISSING -- B6 understands the signal but its evidence row is absent;
* INVALID -- the evidence exists but contradicts the signal (a snapshot of another company, a subject kind
  that does not belong to the signal type, a discovery observation for another GEMI number, an ineligible
  classification, an incomplete subject);
* UNSUPPORTED -- a signal type/source B6 has no adapter for yet (a future producer).

A signal whose provenance is not COMPLETE is still returned, with its own base metadata and no subject facts,
so one corrupt historical event is an auditable warning rather than a broken or silently shortened timeline.

Privacy
-------
Only already-minimised data is read: the signal row, the two evidence tables, the discovery observation and
the company ids of cited snapshots. ``Company.raw_data``, source payloads, names, addresses, persons and
contacts are never loaded. The company's GEMI number is its only external identifier here.

Queries
-------
One query reads a page of signals with both evidence relations and the discovery observation joined; at most
one more reads the company ids of the cited snapshots. Page size does not change the query count.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from typing import Iterable

from django.apps import apps
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django.utils import timezone

from .company_signals import (
    DISCOVERY,
    KAD_ADDED,
    KAD_REMOVED,
    LEGAL_FORM_CHANGED,
    LIVE,
    LOCATION_CHANGED,
    NEW_COMPANY,
    SHADOW,
    SIGNAL_TYPES,
    SNAPSHOT_DIFF,
    STATUS_CHANGED,
)
from .new_company_signals import ELIGIBLE_CLASSIFICATIONS

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# The deliberate, internal-only way to read SHADOW and LIVE together. Never a default.
ALL_MODES = "all_modes"

COMPLETE = "complete"
MISSING = "missing"
INVALID = "invalid"
UNSUPPORTED = "unsupported"

SUBJECT_STATUS = "status"
SUBJECT_KAD = "kad"
SUBJECT_LEGAL_FORM = "legal_form"
SUBJECT_MUNICIPALITY = "municipality"

# The subject kind each snapshot-diff signal type must carry.
SNAPSHOT_SUBJECTS = {
    STATUS_CHANGED: SUBJECT_STATUS,
    LEGAL_FORM_CHANGED: SUBJECT_LEGAL_FORM,
    LOCATION_CHANGED: SUBJECT_MUNICIPALITY,
    KAD_ADDED: SUBJECT_KAD,
    KAD_REMOVED: SUBJECT_KAD,
}
SUPPORTED_SOURCES = {NEW_COMPANY: DISCOVERY, **{signal_type: SNAPSHOT_DIFF for signal_type in SNAPSHOT_SUBJECTS}}


class TimelineError(ValueError):
    """A timeline request that cannot be answered as asked (bad limit, mode, filter or cursor)."""


@dataclass(frozen=True)
class TimelineCursor:
    """The position after the last returned entry: ``(detected_at, signal id)``. Carries no tenant state."""

    detected_at: datetime
    signal_id: int

    def encode(self) -> str:
        if timezone.is_naive(self.detected_at):
            raise TimelineError("a cursor needs a timezone-aware detected_at")
        raw = f"{self.detected_at.astimezone(dt_timezone.utc).isoformat()}|{self.signal_id}"
        return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii")

    @classmethod
    def decode(cls, token: str) -> "TimelineCursor":
        try:
            detected, signal_id = base64.urlsafe_b64decode(token.encode("ascii")).decode("ascii").split("|")
            value = datetime.fromisoformat(detected)
            identifier = int(signal_id)
        except (ValueError, UnicodeError, binascii.Error, AttributeError) as exc:
            raise TimelineError("invalid timeline cursor") from exc
        if timezone.is_naive(value) or identifier < 1:
            raise TimelineError("invalid timeline cursor")
        return cls(detected_at=value, signal_id=identifier)


@dataclass(frozen=True)
class CompanyTimelineEntry:
    """One CompanySignal as a timeline event. Identifiers and dates only; frozen."""

    signal_id: int
    company_id: int
    gemi_number: str
    signal_type: str
    source_type: str
    mode: str
    confidence: Decimal
    rule_version: str
    detected_at: datetime
    effective_precision: str
    effective_date: date | None
    effective_at: datetime | None
    group_key: str
    provenance_status: str
    # Snapshot-diff subject (B5)
    subject_kind: str | None = None
    before_source_id: str | None = None
    after_source_id: str | None = None
    activity_code: str | None = None
    kad_version: str | None = None
    # Discovery provenance (B2)
    discovery_classification: str | None = None
    discovery_observation_id: int | None = None

    @property
    def cursor(self) -> TimelineCursor:
        return TimelineCursor(detected_at=self.detected_at, signal_id=self.signal_id)


@dataclass(frozen=True)
class CompanyTimelinePage:
    entries: tuple
    next_cursor: TimelineCursor | None


def _base(signal, gemi_number: str) -> dict:
    return {
        "signal_id": signal.pk, "company_id": signal.company_id, "gemi_number": gemi_number,
        "signal_type": signal.signal_type, "source_type": signal.source_type, "mode": signal.mode,
        "confidence": signal.confidence, "rule_version": signal.rule_version, "detected_at": signal.detected_at,
        "effective_precision": signal.effective_precision, "effective_date": signal.effective_date,
        "effective_at": signal.effective_at,
    }


def _plain(signal, gemi_number: str, status: str) -> CompanyTimelineEntry:
    return CompanyTimelineEntry(**_base(signal, gemi_number), group_key=f"signal:{signal.pk}", provenance_status=status)


def _evidence(signal, name):
    # Both relations were joined, so an absent row raises without a query.
    try:
        return getattr(signal, name)
    except ObjectDoesNotExist:
        return None


def _discovery_entry(signal, gemi_number: str) -> CompanyTimelineEntry:
    evidence = _evidence(signal, "discovery_evidence")
    if evidence is None:
        return _plain(signal, gemi_number, MISSING)
    observation = evidence.discovery_observation
    if observation.gemi_number != gemi_number or observation.classification not in ELIGIBLE_CLASSIFICATIONS:
        return _plain(signal, gemi_number, INVALID)
    return CompanyTimelineEntry(
        **_base(signal, gemi_number), group_key=f"discovery:{signal.pk}", provenance_status=COMPLETE,
        discovery_classification=observation.classification, discovery_observation_id=observation.pk,
    )


def _snapshot_entry(signal, gemi_number: str, snapshot_companies: dict) -> CompanyTimelineEntry:
    evidence = _evidence(signal, "snapshot_evidence")
    if evidence is None:
        return _plain(signal, gemi_number, MISSING)
    kind = SNAPSHOT_SUBJECTS[signal.signal_type]
    cited = (snapshot_companies.get(evidence.previous_snapshot_id), snapshot_companies.get(evidence.current_snapshot_id))
    if kind == SUBJECT_KAD:
        subject_complete = evidence.activity_code is not None
    else:
        subject_complete = evidence.before_source_id is not None and evidence.after_source_id is not None
    if evidence.subject_kind != kind or cited != (signal.company_id, signal.company_id) or not subject_complete:
        return _plain(signal, gemi_number, INVALID)
    subject = {"subject_kind": kind}
    if kind == SUBJECT_KAD:
        subject.update(activity_code=evidence.activity_code, kad_version=evidence.kad_version)
    else:
        subject.update(before_source_id=evidence.before_source_id, after_source_id=evidence.after_source_id)
    return CompanyTimelineEntry(
        **_base(signal, gemi_number), group_key=f"snapshot:{evidence.current_snapshot_id}",
        provenance_status=COMPLETE, **subject,
    )


def _limit(limit) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TimelineError("limit must be an integer")
    if not 1 <= limit <= MAX_LIMIT:
        raise TimelineError(f"limit must be between 1 and {MAX_LIMIT}")
    return limit


def get_company_timeline(
    company, *, mode: str = SHADOW, limit: int = DEFAULT_LIMIT, before: TimelineCursor | str | None = None,
    signal_types: Iterable[str] | None = None,
) -> CompanyTimelinePage:
    """One page of the company's timeline, newest first. Read-only; see the module docstring."""
    CompanySignal = apps.get_model("gemiapp", "CompanySignal")
    CompanySnapshot = apps.get_model("gemiapp", "CompanySnapshot")
    if company is None or company.pk is None:
        raise TimelineError("a saved company is required")
    if mode not in (SHADOW, LIVE, ALL_MODES):
        raise TimelineError(f"mode must be {SHADOW}, {LIVE} or the explicit {ALL_MODES}")
    limit = _limit(limit)
    if isinstance(before, str):
        before = TimelineCursor.decode(before)
    elif before is not None and not isinstance(before, TimelineCursor):
        raise TimelineError("before must be a TimelineCursor or its encoded token")

    signals = CompanySignal.objects.filter(company_id=company.pk)
    if mode != ALL_MODES:
        signals = signals.filter(mode=mode)
    if signal_types is not None:
        wanted = set(signal_types)
        unknown = wanted - set(SIGNAL_TYPES)
        if not wanted or unknown:
            raise TimelineError(f"unknown or empty signal types: {sorted(unknown)}")
        signals = signals.filter(signal_type__in=sorted(wanted))
    if before is not None:
        signals = signals.filter(
            Q(detected_at__lt=before.detected_at) | Q(detected_at=before.detected_at, id__lt=before.signal_id)
        )
    rows = list(
        # The company is deliberately not joined: its payload and contact columns are never loaded.
        signals.select_related("snapshot_evidence", "discovery_evidence__discovery_observation")
        .order_by("-detected_at", "-id")[:limit + 1]
    )
    has_more = len(rows) > limit
    rows = rows[:limit]

    cited_ids = set()
    for signal in rows:
        evidence = _evidence(signal, "snapshot_evidence") if signal.source_type == SNAPSHOT_DIFF else None
        if evidence is not None:
            cited_ids.update((evidence.previous_snapshot_id, evidence.current_snapshot_id))
    snapshot_companies = (
        dict(CompanySnapshot.objects.filter(pk__in=cited_ids).values_list("pk", "company_id")) if cited_ids else {}
    )

    entries = []
    gemi_number = company.gemi_number
    for signal in rows:
        if SUPPORTED_SOURCES.get(signal.signal_type) != signal.source_type:
            entries.append(_plain(signal, gemi_number, UNSUPPORTED))
        elif signal.source_type == DISCOVERY:
            entries.append(_discovery_entry(signal, gemi_number))
        else:
            entries.append(_snapshot_entry(signal, gemi_number, snapshot_companies))
    return CompanyTimelinePage(
        entries=tuple(entries), next_cursor=entries[-1].cursor if has_more and entries else None,
    )
