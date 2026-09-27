"""G4 transition mirror: legacy CustomerRadar -> OrganizationRadar, one direction only.

During G4 the legacy ``CustomerRadar`` is the customer's canonical edit surface (it produces the Leads they use),
while ``OrganizationRadar`` is what the SHADOW opportunity pipeline evaluates. So that the shadow comparison keeps
reflecting what the customer actually asked for, every customer mutation of a legacy Radar is mirrored into its
OrganizationRadar, inside the same transaction as the legacy write. Only while ``GEMI_TRANSITION_LEGACY_ACCESS`` is
on; after the LIVE cutover customers edit OrganizationRadar and nothing is mirrored (and never the other way).

Identity is the provenance row only (``LegacyRadarMigrationMap``), never a name. The mapping rules are the
migration command's own (``legacy_radar_migration``), so a Radar mirrored here and one copied by the command are
identical:

* no mapping yet -> the user's single organization (any role; none or several -> not mirrored, the operator
  provisioning and migration steps pick it up later) gets one OrganizationRadar and one mapping;
* mapped -> the whole definition is rebuilt from the legacy Radar and replaced (criteria, exact name, and active =
  ``is_active`` and frequency != off);
* soft-deleted -> the OrganizationRadar is deactivated; its row and the mapping (provenance) are kept;
* the OrganizationRadar was deleted (tombstone) -> never recreated.

Failure contract:

* a legacy Radar that OrganizationRadar cannot represent (a name search, reference data it cannot resolve, ...):
  the legacy operation proceeds -- the migration command's established contract for such Radars -- and, when one
  is mapped, its OrganizationRadar is **deactivated**, so SHADOW never evaluates stale criteria. Logged as ERROR on
  an operator-alert logger: not a silent divergence;
* a mapping that points at another tenant, or a user whose organization is no longer unique: nothing is written to
  any tenant; ERROR;
* anything unexpected: logged and re-raised, so the caller's transaction rolls the legacy write back too.

Logs carry ids and classifications only, never criteria or personal data.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction

from .legacy_radar_migration import (
    MAPPING_VERSION, MappingProblem, _definition, _destination, _keep_exact_name, copy_legacy_radar,
)
from .models import CustomerRadar, LegacyRadarMigrationMap
from .organization_radars import (
    RadarError, get_organization_radar_definition, replace_organization_radar, set_organization_radar_active,
    validate_radar_definition,
)

logger = logging.getLogger(__name__)

DISABLED = "disabled"
NOT_MIRRORED = "not_mirrored"          # no unique organization yet, or deleted before it was ever copied
CREATED = "created"
UPDATED = "updated"
DEACTIVATED = "deactivated"
TOMBSTONE = "tombstone"
UNSUPPORTED = "unsupported"
CONFLICT = "conflict"


def mirror_enabled() -> bool:
    return bool(getattr(settings, "GEMI_TRANSITION_LEGACY_ACCESS", True))


def mirror_legacy_radar(radar) -> str:
    """Bring the legacy Radar's OrganizationRadar in line with it. Idempotent; returns the outcome."""
    if not mirror_enabled():
        return DISABLED
    try:
        with transaction.atomic():
            return _mirror(radar.pk)
    except Exception:
        logger.exception("Legacy Radar #%s: mirroring failed; the legacy change is rolled back.", radar.pk)
        raise


def _mirror(radar_id: int) -> str:
    locked = CustomerRadar.objects.select_for_update().prefetch_related("activity_codes").get(pk=radar_id)
    mapping = (LegacyRadarMigrationMap.objects.select_for_update().select_related("organization_radar")
               .filter(legacy_radar_id=locked.pk).first())
    try:
        destination = _destination(locked)
    except MappingProblem:
        destination = None

    if mapping is not None:
        if mapping.organization_radar_id is None:
            return TOMBSTONE                                   # the copy was deleted: never resurrected
        target = mapping.organization_radar
        if (destination is None or mapping.organization_id != destination.pk
                or target.organization_id != mapping.organization_id or mapping.mapping_version != MAPPING_VERSION):
            logger.error("Legacy Radar #%s: its mapping no longer matches the owner's single organization; "
                         "nothing mirrored.", locked.pk)
            return CONFLICT
        if locked.deleted_at is not None:
            set_organization_radar_active(destination, target, False)
            return DEACTIVATED
        try:
            definition = _definition(locked, destination).definition
            _keep_exact_name(replace_organization_radar(destination, target, definition), locked.name)
        except (MappingProblem, RadarError) as problem:
            set_organization_radar_active(destination, target, False)
            logger.error("Legacy Radar #%s: not representable (%s); OrganizationRadar #%s deactivated.",
                         locked.pk, getattr(problem, "classification", type(problem).__name__), target.pk)
            return UNSUPPORTED
        return UPDATED

    if locked.deleted_at is not None or destination is None:
        return NOT_MIRRORED
    try:
        definition = _definition(locked, destination).definition
        copy_legacy_radar(locked, destination, definition)
    except (MappingProblem, RadarError) as problem:
        logger.error("Legacy Radar #%s: not representable (%s); no OrganizationRadar created.",
                     locked.pk, getattr(problem, "classification", type(problem).__name__))
        return UNSUPPORTED
    return CREATED


# --- existing drift: plan and resync ---------------------------------------------------------------------------
#
# The mirror above only follows mutations made after it was deployed. A Radar changed between the original
# migration and that deployment may already differ from its copy. ``plan_legacy_radar`` is read-only and says what
# the mirror would do for one legacy Radar now; ``resync_legacy_radars`` reports that for every legacy Radar and,
# only with ``apply=True``, performs the planned changes through the mirror itself (same locks, same rules, one
# transaction per Radar). A second apply therefore finds nothing to do.

UNCHANGED = "unchanged"
WOULD_CREATE = "would_create"
WOULD_UPDATE = "would_update"
WOULD_DEACTIVATE = "would_deactivate"
SKIPPED_NO_ORG = "skipped_no_org"
SKIPPED_AMBIGUOUS_ORG = "skipped_ambiguous_org"
UNSAFE = "unsafe"                       # mapped to a tenant the owner's single organization no longer is
SOFT_DELETED = "soft_deleted"           # deleted before it was ever copied: nothing to do
WRITING_PLANS = (WOULD_CREATE, WOULD_UPDATE, WOULD_DEACTIVATE)


class TransitionOff(RuntimeError):
    """After the LIVE cutover the OrganizationRadar is canonical; legacy Radars are never copied over it."""


def _identity(item):
    return item._meta.label_lower, item.pk


def _fingerprint(definition):
    """The definition's matching-relevant content, order-free; the name compared exactly."""
    return (definition.name, definition.active, definition.score_threshold,
            frozenset(map(_identity, definition.kads)), frozenset(map(_identity, definition.regions)),
            frozenset(map(_identity, definition.legal_forms)), frozenset(definition.signal_types),
            frozenset(map(_identity, definition.exclusions)))


def _expected_definition(radar, destination):
    """The definition the mirror would write, or MappingProblem/RadarError when it cannot represent the Radar."""
    definition = _definition(radar, destination).definition
    validate_radar_definition(definition)             # the domain rules the write would enforce
    return definition


def plan_legacy_radar(radar) -> str:
    """What the mirror would do for this legacy Radar now. Read-only: no row is written or locked."""
    mapping = (LegacyRadarMigrationMap.objects.select_related("organization_radar")
               .filter(legacy_radar_id=radar.pk).first())
    try:
        destination, refusal = _destination(radar), None
    except MappingProblem as problem:
        destination, refusal = None, problem.classification
    if mapping is not None:
        if mapping.organization_radar_id is None:
            return TOMBSTONE
        target = mapping.organization_radar
        if (destination is None or mapping.organization_id != destination.pk
                or target.organization_id != mapping.organization_id or mapping.mapping_version != MAPPING_VERSION):
            return UNSAFE
        if radar.deleted_at is not None:
            return WOULD_DEACTIVATE if target.active else UNCHANGED
        try:
            expected = _expected_definition(radar, destination)
        except (MappingProblem, RadarError):
            return WOULD_DEACTIVATE if target.active else UNSUPPORTED
        current = get_organization_radar_definition(destination, target)
        return UNCHANGED if _fingerprint(expected) == _fingerprint(current) else WOULD_UPDATE
    if radar.deleted_at is not None:
        return SOFT_DELETED
    if destination is None:
        return SKIPPED_AMBIGUOUS_ORG if refusal == "ambiguous_organization" else SKIPPED_NO_ORG
    try:
        _expected_definition(radar, destination)
    except (MappingProblem, RadarError):
        return UNSUPPORTED
    return WOULD_CREATE


def resync_legacy_radars(*, apply: bool = False, user_id: int | None = None) -> dict:
    """Plan (and with ``apply`` perform) the mirror for every legacy Radar. Counters only, no personal data."""
    if not mirror_enabled():
        raise TransitionOff("GEMI_TRANSITION_LEGACY_ACCESS is off: OrganizationRadar is canonical, nothing to sync.")
    counters = dict.fromkeys((
        "examined", "mapped", "unmapped", UNCHANGED, WOULD_CREATE, WOULD_UPDATE, WOULD_DEACTIVATE, "tombstones",
        SKIPPED_NO_ORG, SKIPPED_AMBIGUOUS_ORG, UNSUPPORTED, UNSAFE, SOFT_DELETED, "errors",
        "applied_created", "applied_updated", "applied_deactivated"), 0)
    mapped_ids = set(LegacyRadarMigrationMap.objects.values_list("legacy_radar_id", flat=True))
    radars = CustomerRadar.objects.prefetch_related("activity_codes").order_by("pk")
    if user_id is not None:
        radars = radars.filter(user_id=user_id)
    for radar in radars:
        counters["examined"] += 1
        counters["mapped" if radar.pk in mapped_ids else "unmapped"] += 1
        try:
            plan = plan_legacy_radar(radar)
        except Exception:
            logger.exception("Legacy Radar #%s: could not be planned.", radar.pk)
            counters["errors"] += 1
            continue
        counters["tombstones" if plan == TOMBSTONE else plan] += 1
        if not apply or plan not in WRITING_PLANS:
            continue
        try:
            outcome = mirror_legacy_radar(radar)          # re-derives under its own locks, one transaction
        except Exception:
            counters["errors"] += 1                        # already logged by the mirror; nothing half-written
            continue
        applied = {CREATED: "applied_created", UPDATED: "applied_updated", DEACTIVATED: "applied_deactivated",
                   UNSUPPORTED: "applied_deactivated"}.get(outcome)
        if applied:
            counters[applied] += 1
    return counters


# --- late organization provisioning ---------------------------------------------------------------------------


def copy_unmapped_legacy_radars(user, organization) -> dict:
    """Copy the user's existing, never-copied legacy Radars into the organization just provisioned for them.

    Called by the provisioning paths inside the transaction that created the organization, only after it is
    validly provisioned (the user's single membership). Exactly once per Radar: the provenance row is the guard,
    never a name. A Radar the organization cannot represent is not copied (the migration contract). Radars that
    are deleted, already mapped or tombstoned are left alone. Independent of the transition flag: this is the same
    initial copy the migration command makes, not a mirror of later edits."""
    counts = {"copied": 0, "unsupported": 0}
    radars = (CustomerRadar.objects.select_for_update().filter(user=user, deleted_at__isnull=True)
              .exclude(pk__in=LegacyRadarMigrationMap.objects.values("legacy_radar_id"))
              .prefetch_related("activity_codes").order_by("pk"))
    for radar in radars:
        try:
            if _destination(radar).pk != organization.pk:
                continue                                   # never into an organization that is not the user's one
            with transaction.atomic():
                copy_legacy_radar(radar, organization, _expected_definition(radar, organization))
        except (MappingProblem, RadarError):
            counts["unsupported"] += 1
            continue
        counts["copied"] += 1
    return counts
