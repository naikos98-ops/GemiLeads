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
from .organization_radars import RadarError, replace_organization_radar, set_organization_radar_active

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
