"""Company signals foundation (B1): taxonomy, event identity, rule registry and the writer service.

A signal is a concrete business event detected for one company from GEMI-derived evidence -- a
company-level fact, not a customer's. The same event may later matter to many Radars and customers, so
nothing here is tenant-owned and no signal references a user.

B1 is the foundation only. No detector runs, nothing is generated, and the table stays empty after the
migration. The producers begin in B2.

Taxonomy
--------
``SIGNAL_TYPE_CHOICES`` holds the approved roadmap types. The six the roadmap implements first --
NEW_COMPANY, STATUS_CHANGED, KAD_ADDED, KAD_REMOVED, LEGAL_FORM_CHANGED, LOCATION_CHANGED -- and the
approved later corporate events. Listing a type is taxonomy only: without a registered rule the service
refuses to record it, so a type cannot be produced by accident.

``SOURCE_TYPE_CHOICES`` names the evidence pipeline: DISCOVERY exists today (A10); SNAPSHOT_DIFF,
DOCUMENT_METADATA and DOCUMENT_ANALYSIS are declared but no producer exists. A source type outside the
list is refused, so a new pipeline needs a deliberate code change rather than an arbitrary string.

Event identity (``dedupe_key``)
-------------------------------
``build_dedupe_key`` hashes exactly this canonical material, and nothing else:

* the key version (so the scheme itself can evolve deliberately);
* the signal type;
* the company's GEMI number (the stable business identifier, not a database primary key);
* the producer's ``event_key``: the deterministic facts that make this event *this* event, e.g. for a
  KAD change the code and version, for a status change the new status id and its effective date.

Deliberately excluded, because they describe the processing attempt rather than the event: the primary
key, ``detected_at``, any job or run id, random values, the shadow/live mode and the rule version. A
retry, a reprocessed source, a re-detection, a promotion from shadow to live and a newer rule version all
map to the same key, so the event exists once. The key is unique at database level.

Rule versions
-------------
Every signal records the detector semantics that produced it, as ``name:vN`` (e.g. ``new_company:v1``) --
never a deployment SHA, which says nothing about how an event was interpreted. ``SIGNAL_RULES`` is the one
place these live: a signal type without a registered rule cannot be recorded, and a producer may only use
a source type its rule allows. A detector changing its semantics registers a new version here; that does
not change event identity, so history is not duplicated.

Confidence
----------
A decimal in [0, 1] with four decimal places, validated by the service and by a database constraint.
B1 never calculates it: producers pass the value their evidence justifies (a structured source event at
or near 1.0, an ambiguous document lower).

Effective time versus detected time
-----------------------------------
``detected_at`` is when Gemi Leads first detected the event: our own timestamp, timezone-aware, set once
and never reset by a later detection.

The source's own time is kept at the precision the source actually provides, never invented:

* no source time at all -> ``effective_precision="none"``, both fields null;
* a date (an incorporation date, a status-change date) -> ``effective_date`` with precision ``date`` and
  ``effective_at`` null -- a date is never turned into midnight to look like a timestamp;
* a genuine timestamp -> ``effective_at`` (timezone-aware) with precision ``datetime``.

A database constraint enforces that the precision and the two fields agree.

Shadow and live
---------------
Every signal is explicitly SHADOW (for validation and detector precision measurement, never customer
workflows) or LIVE (an approved production fact). The mode is not part of the event identity, so a
shadow signal can be promoted without creating a second event. ``promote_company_signal`` is the explicit,
tested way to do that; nothing promotes automatically, and B1 attaches no workflow to either mode.

Provenance
----------
A signal records its source type and rule version. Deliberately no foreign key to
``GemiDiscoveryObservation`` or ``GemiSourceRecord`` yet: only a discovery producer would set the first,
snapshots do not exist before B3, and a GenericForeignKey to cover unknown future sources would be a
brittle guess. The producer that needs a provenance link adds its own nullable field then; the model stays
extensible either way.

Privacy
-------
A signal holds identifiers, an event type, dates, a rule version and a confidence. There is no payload,
evidence blob or free JSON: no persons, contact details, VAT number or ``raw_data`` can enter it. The
company relation and its GEMI number are identity enough.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from django.apps import apps
from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

DEDUPE_KEY_VERSION = 1
CONFIDENCE_PLACES = Decimal("0.0001")

# --- signal types (taxonomy only; a type needs a registered rule before it can be recorded) ------
NEW_COMPANY = "new_company"
STATUS_CHANGED = "status_changed"
KAD_ADDED = "kad_added"
KAD_REMOVED = "kad_removed"
LEGAL_FORM_CHANGED = "legal_form_changed"
LOCATION_CHANGED = "location_changed"
CAPITAL_INCREASE = "capital_increase"
CAPITAL_DECREASE = "capital_decrease"
NEW_BRANCH = "new_branch"
BRANCH_CLOSED = "branch_closed"
MANAGEMENT_CHANGE = "management_change"
MERGER = "merger"
DISSOLUTION = "dissolution"
COMPANY_TRANSFORMATION = "company_transformation"
OTHER_CORPORATE_EVENT = "other_corporate_event"

SIGNAL_TYPE_CHOICES = (
    (NEW_COMPANY, "Νέα επιχείρηση"),
    (STATUS_CHANGED, "Αλλαγή κατάστασης"),
    (KAD_ADDED, "Προσθήκη ΚΑΔ"),
    (KAD_REMOVED, "Αφαίρεση ΚΑΔ"),
    (LEGAL_FORM_CHANGED, "Αλλαγή νομικής μορφής"),
    (LOCATION_CHANGED, "Αλλαγή έδρας"),
    (CAPITAL_INCREASE, "Αύξηση κεφαλαίου"),
    (CAPITAL_DECREASE, "Μείωση κεφαλαίου"),
    (NEW_BRANCH, "Νέο υποκατάστημα"),
    (BRANCH_CLOSED, "Κλείσιμο υποκαταστήματος"),
    (MANAGEMENT_CHANGE, "Αλλαγή διοίκησης"),
    (MERGER, "Συγχώνευση"),
    (DISSOLUTION, "Λύση"),
    (COMPANY_TRANSFORMATION, "Μετατροπή"),
    (OTHER_CORPORATE_EVENT, "Άλλο εταιρικό γεγονός"),
)

# --- evidence pipelines --------------------------------------------------------------------------
DISCOVERY = "discovery"
SNAPSHOT_DIFF = "snapshot_diff"
DOCUMENT_METADATA = "document_metadata"
DOCUMENT_ANALYSIS = "document_analysis"

SOURCE_TYPE_CHOICES = (
    (DISCOVERY, "Discovery"),
    (SNAPSHOT_DIFF, "Snapshot diff"),
    (DOCUMENT_METADATA, "Document metadata"),
    (DOCUMENT_ANALYSIS, "Document analysis"),
)

SHADOW = "shadow"
LIVE = "live"
MODE_CHOICES = ((SHADOW, "Shadow"), (LIVE, "Live"))

PRECISION_NONE = "none"
PRECISION_DATE = "date"
PRECISION_DATETIME = "datetime"
PRECISION_CHOICES = (
    (PRECISION_NONE, "Χωρίς χρόνο πηγής"), (PRECISION_DATE, "Ημερομηνία"), (PRECISION_DATETIME, "Χρονοσήμανση"),
)

RULE_VERSION_PATTERN = re.compile(r"^[a-z][a-z0-9_]*:v[0-9]+$")


class SignalConflictError(RuntimeError):
    """The same event identity was recorded with different immutable facts: a rule or data bug."""


@dataclass(frozen=True)
class SignalRule:
    """How one signal type is detected: its current version and the evidence it may come from."""

    signal_type: str
    rule_version: str
    source_types: frozenset
    implemented: bool = False


# The one place detector semantics are named. A type missing here is taxonomy only and cannot be recorded.
SIGNAL_RULES: tuple[SignalRule, ...] = (
    # Implemented by gemiapp.new_company_signals (B2): Discovery v2 evidence that Gemi Leads observed a
    # company for the first time. Not "incorporated today" -- a late publication is just as much a first
    # observation.
    SignalRule(NEW_COMPANY, "new_company:v1", frozenset({DISCOVERY}), implemented=True),
)

SIGNAL_TYPES = tuple(value for value, _ in SIGNAL_TYPE_CHOICES)
SOURCE_TYPES = tuple(value for value, _ in SOURCE_TYPE_CHOICES)
MODES = tuple(value for value, _ in MODE_CHOICES)
_RULES_BY_TYPE = {rule.signal_type: rule for rule in SIGNAL_RULES}
FUTURE_SIGNAL_TYPES = frozenset(SIGNAL_TYPES) - set(_RULES_BY_TYPE)


def _validate_registry() -> None:
    versions = [rule.rule_version for rule in SIGNAL_RULES]
    if len(versions) != len(set(versions)):
        raise ValueError("duplicate rule version in SIGNAL_RULES")
    if len(_RULES_BY_TYPE) != len(SIGNAL_RULES):
        raise ValueError("duplicate signal type in SIGNAL_RULES")
    for rule in SIGNAL_RULES:
        if rule.signal_type not in SIGNAL_TYPES:
            raise ValueError(f"unknown signal type in SIGNAL_RULES: {rule.signal_type}")
        if not RULE_VERSION_PATTERN.match(rule.rule_version):
            raise ValueError(f"rule version must look like name:v1, got {rule.rule_version}")
        unknown = rule.source_types - set(SOURCE_TYPES)
        if unknown:
            raise ValueError(f"unknown source types for {rule.signal_type}: {sorted(unknown)}")


_validate_registry()


def rule_for(signal_type: str) -> SignalRule | None:
    return _RULES_BY_TYPE.get(signal_type)


# --- event identity -------------------------------------------------------------------------------

def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"event_key values must be JSON-safe, got {type(value).__name__}")


def build_dedupe_key(*, signal_type: str, gemi_number: str, event_key: Mapping[str, Any]) -> str:
    """The event's identity. See the module docstring for exactly what enters it."""
    if signal_type not in SIGNAL_TYPES:
        raise ValueError(f"unknown signal type: {signal_type}")
    if not gemi_number:
        raise ValueError("gemi_number is required for a signal identity")
    if not isinstance(event_key, Mapping) or not event_key:
        raise ValueError("event_key must be a non-empty mapping of the facts that identify the event")
    material = json.dumps(
        [DEDUPE_KEY_VERSION, signal_type, str(gemi_number), _canonical(event_key)],
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# --- writer service -------------------------------------------------------------------------------

def _effective_fields(effective: date | datetime | None) -> dict[str, Any]:
    if effective is None:
        return {"effective_date": None, "effective_at": None, "effective_precision": PRECISION_NONE}
    if isinstance(effective, datetime):
        if timezone.is_naive(effective):
            raise ValueError("an effective timestamp must be timezone-aware")
        return {"effective_date": None, "effective_at": effective, "effective_precision": PRECISION_DATETIME}
    if isinstance(effective, date):
        # A date stays a date: no midnight is invented to make it look like a timestamp.
        return {"effective_date": effective, "effective_at": None, "effective_precision": PRECISION_DATE}
    raise TypeError("effective must be a date, an aware datetime or None")


def _confidence(value: Any) -> Decimal:
    try:
        confidence = Decimal(str(value)).quantize(CONFIDENCE_PLACES)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"confidence must be a number between 0 and 1, got {value!r}") from exc
    if not Decimal("0") <= confidence <= Decimal("1"):
        raise ValueError(f"confidence must be between 0 and 1, got {value!r}")
    return confidence


def record_company_signal(
    *, company, signal_type: str, source_type: str, event_key: Mapping[str, Any],
    effective: date | datetime | None = None, confidence: Any = 1, mode: str = SHADOW,
    rule_version: str | None = None, detected_at: datetime | None = None,
) -> tuple[Any, bool]:
    """Record one detected event once. Returns (signal, created).

    Takes facts a producer has already derived; it never infers a business event itself. Recording the
    same event again returns the existing row with its original ``detected_at`` and mode. Recording the
    same identity with different immutable facts raises SignalConflictError instead of rewriting history.
    """
    CompanySignal = apps.get_model("gemiapp", "CompanySignal")
    if signal_type not in SIGNAL_TYPES:
        raise ValueError(f"unknown signal type: {signal_type}")
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"unknown source type: {source_type}")
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    rule = rule_for(signal_type)
    if rule is None:
        raise ValueError(f"{signal_type} has no registered rule: it is taxonomy only until a detector exists")
    if source_type not in rule.source_types:
        raise ValueError(f"{signal_type} may not be detected from {source_type}")
    if rule_version is None:
        rule_version = rule.rule_version
    elif not RULE_VERSION_PATTERN.match(rule_version) or rule_version.split(":")[0] != rule.rule_version.split(":")[0]:
        raise ValueError(f"rule version must be a version of {rule.rule_version.split(':')[0]}, got {rule_version}")
    if detected_at is not None and timezone.is_naive(detected_at):
        raise ValueError("detected_at must be timezone-aware")

    values = {
        "signal_type": signal_type, "source_type": source_type, "rule_version": rule_version,
        "confidence": _confidence(confidence), "mode": mode, **_effective_fields(effective),
    }
    dedupe_key = build_dedupe_key(signal_type=signal_type, gemi_number=company.gemi_number, event_key=event_key)
    immutable = ("signal_type", "effective_date", "effective_at", "effective_precision")

    def _existing():
        found = CompanySignal.objects.filter(dedupe_key=dedupe_key).first()
        if found is None:
            return None
        conflicts = {
            name: (getattr(found, name), values[name]) for name in immutable if getattr(found, name) != values[name]
        }
        if found.company_id != company.pk:
            conflicts["company_id"] = (found.company_id, company.pk)
        if conflicts:
            raise SignalConflictError(
                f"signal {dedupe_key[:12]} already exists with different immutable facts: {sorted(conflicts)}"
            )
        return found

    found = _existing()
    if found is not None:
        return found, False
    try:
        with transaction.atomic():
            signal = CompanySignal.objects.create(
                company=company, dedupe_key=dedupe_key, detected_at=detected_at or timezone.now(), **values,
            )
    except IntegrityError:  # another worker recorded the same event first
        found = _existing()
        if found is None:
            raise
        return found, False
    logger.info(
        "Company signal recorded: type=%s mode=%s company=%s rule=%s", signal_type, mode, company.pk, rule_version,
    )
    return signal, True


def promote_company_signal(signal, *, at: datetime | None = None):
    """Explicitly mark a shadow signal as a live production fact. Never creates a second event, and
    nothing calls this automatically; the promotion policy itself belongs to a later task."""
    if signal.mode == LIVE:
        return signal
    if at is not None and timezone.is_naive(at):
        raise ValueError("at must be timezone-aware")
    signal.mode = LIVE
    signal.save(update_fields=["mode", "updated_at"])
    logger.info("Company signal promoted to live: id=%s type=%s", signal.pk, signal.signal_type)
    return signal
