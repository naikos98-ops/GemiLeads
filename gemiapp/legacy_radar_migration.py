"""Explicit, create-only migration of legacy CustomerRadar definitions to OrganizationRadar.

This module is called only by ``migrate_legacy_radars_to_organizations``.  It never creates organizations,
signals, opportunities or historical matches, and it never mutates a legacy Radar.  Rule version 1 preserves
only semantics that the current OrganizationRadar matcher can represent exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from .company_signals import NEW_COMPANY
from .kad import normalize_kad_search
from .models import (
    CustomerRadar, GemiKad, GemiLegalType, GemiPrefecture, LegacyRadarMigrationMap, Organization,
    OrganizationMember,
)
from .organization_radars import RadarDefinition, create_organization_radar

MAPPING_VERSION = 1
DEFAULT_LIMIT = 100
MAX_LIMIT = 10_000


class MappingProblem(ValueError):
    def __init__(self, classification: str, reason: str):
        super().__init__(reason)
        self.classification = classification
        self.reason = reason


@dataclass(frozen=True)
class PreparedMigration:
    organization: object
    definition: RadarDefinition
    kads: int
    prefectures: int
    legal_types: int


@dataclass
class MigrationReport:
    dry_run: bool
    counters: dict[str, int] = field(default_factory=lambda: {
        "legacy_radars_examined": 0, "eligible": 0, "migrated": 0, "already_migrated": 0,
        "already_migrated_deleted": 0, "missing_organization": 0, "ambiguous_organization": 0,
        "unsupported_mapping": 0, "invalid_reference_data": 0, "provenance_conflict": 0,
        "soft_deleted": 0, "active_radars": 0, "inactive_radars": 0, "errors": 0,
        "kads": 0, "prefectures": 0, "legal_types": 0, "only_active_currently_inert": 0,
    })

    def increment(self, name: str, amount: int = 1):
        self.counters[name] += amount

    def lines(self):
        yield f"mode={'dry-run' if self.dry_run else 'write'} mapping_version={MAPPING_VERSION}"
        for name, value in self.counters.items():
            yield f"{name}={value}"


def _organizations_for(user_id: int):
    """All tenants this user belongs to. Ownership is an OWNER membership in the current schema."""
    ids = OrganizationMember.objects.filter(user_id=user_id).values_list("organization_id", flat=True).distinct()
    return list(Organization.objects.filter(pk__in=ids).order_by("pk")[:2])


def _destination(radar):
    organizations = _organizations_for(radar.user_id)
    if not organizations:
        raise MappingProblem("missing_organization", "the owner has no Organization membership")
    if len(organizations) != 1:
        raise MappingProblem("ambiguous_organization", "the owner belongs to more than one Organization")
    return organizations[0]


def _exact_reference(model, legacy_value: str, what: str):
    value = str(legacy_value or "").strip()
    if not value:
        raise MappingProblem("invalid_reference_data", f"blank legacy {what}")
    by_id = list(model.objects.filter(source_id=value)[:2])
    candidates = by_id if len(by_id) == 1 else [
        row for row in model.objects.all().only("pk", "source_id", "description", "is_present")
        if normalize_kad_search(row.description) == normalize_kad_search(value)
    ]
    if len(candidates) != 1:
        raise MappingProblem("invalid_reference_data", f"legacy {what} is missing or ambiguous")
    if not candidates[0].is_present:
        raise MappingProblem("invalid_reference_data", f"legacy {what} resolves only to a retired reference")
    return candidates[0]


def _definition(radar, organization) -> PreparedMigration:
    if radar.name_query:
        raise MappingProblem("unsupported_mapping", "name_query has no OrganizationRadar equivalent")
    # Current-behaviour exception: the GEMI company search rows do not publish isActive in either accepted
    # location, so company_defaults falls back to True and the local production-shaped data is uniformly active.
    # This preserves today's behaviour only; it must be revisited before status-reference ingestion changes it.
    if radar.monitor_from > timezone.now():
        raise MappingProblem("unsupported_mapping", "future monitor_from cannot be represented prospectively")

    kads = []
    for activity in radar.activity_codes.all().order_by("pk"):
        rows = list(GemiKad.objects.filter(source_id=activity.normalized_code).order_by("source_id", "kad_version"))
        if not rows or any(not row.is_present for row in rows):
            raise MappingProblem("invalid_reference_data", "legacy KAD has no complete present canonical mapping")
        kads.extend(rows)  # all versions preserve the legacy code-only match semantics
    prefectures = tuple(_exact_reference(GemiPrefecture, value, "prefecture") for value in radar.prefectures)
    legal_types = tuple(_exact_reference(GemiLegalType, value, "legal type") for value in radar.legal_types)
    definition = RadarDefinition(
        name=radar.name,
        active=bool(radar.is_active and radar.frequency != "off"),
        score_threshold=None,
        kads=tuple(kads),
        regions=prefectures,
        legal_forms=legal_types,
        signal_types=(NEW_COMPANY,),
        exclusions=(),
    )
    return PreparedMigration(organization, definition, len(kads), len(prefectures), len(legal_types))


def _provenance_state(radar, destination):
    mapping = (LegacyRadarMigrationMap.objects.filter(legacy_radar_id=radar.pk)
               .select_related("organization_radar").first())
    if mapping is None:
        return None
    if (mapping.organization_id != destination.pk or mapping.mapping_version != MAPPING_VERSION
            or (mapping.organization_radar_id is not None
                and mapping.organization_radar.organization_id != mapping.organization_id)):
        return "provenance_conflict"
    return "already_migrated" if mapping.organization_radar_id is not None else "already_migrated_deleted"


def _query(*, user_id=None, organization_id=None, legacy_radar_id=None):
    queryset = CustomerRadar.objects.all().prefetch_related("activity_codes").order_by("pk")
    if user_id is not None:
        queryset = queryset.filter(user_id=user_id)
    if legacy_radar_id is not None:
        queryset = queryset.filter(pk=legacy_radar_id)
    if organization_id is not None:
        queryset = queryset.filter(user__organization_memberships__organization_id=organization_id).distinct()
    return queryset


def migrate_legacy_radars(*, dry_run=False, limit=DEFAULT_LIMIT, user_id=None, organization_id=None,
                          legacy_radar_id=None) -> MigrationReport:
    report = MigrationReport(dry_run=dry_run)
    for radar in _query(user_id=user_id, organization_id=organization_id,
                        legacy_radar_id=legacy_radar_id)[:limit]:
        report.increment("legacy_radars_examined")
        if radar.deleted_at is not None:
            report.increment("soft_deleted")
            continue
        active = bool(radar.is_active and radar.frequency != "off")
        report.increment("active_radars" if active else "inactive_radars")
        try:
            destination = _destination(radar)
            state = _provenance_state(radar, destination)
            if state:
                report.increment(state)
                continue
            prepared = _definition(radar, destination)
            report.increment("eligible")
            if radar.only_active:
                report.increment("only_active_currently_inert")
            if dry_run:
                report.increment("kads", prepared.kads)
                report.increment("prefectures", prepared.prefectures)
                report.increment("legal_types", prepared.legal_types)
                continue
            with transaction.atomic():
                locked = CustomerRadar.objects.select_for_update().get(pk=radar.pk)
                # Re-resolve and revalidate under the per-Radar transaction. This prevents a stale classification
                # from producing a partial or wrongly-owned copy during an operator run.
                destination = _destination(locked)
                state = _provenance_state(locked, destination)
                if state:
                    report.increment(state)
                    continue
                prepared = _definition(locked, destination)
                organization_radar = create_organization_radar(destination, prepared.definition)
                if organization_radar.name != locked.name:
                    # The domain service normally trims labels. Migration is archival configuration copying, so
                    # retain the exact legacy label (including harmless surrounding whitespace) as requested.
                    organization_radar.name = locked.name
                    organization_radar.save(update_fields=["name", "updated_at"])
                LegacyRadarMigrationMap.objects.create(
                    legacy_radar=locked, organization=destination, organization_radar=organization_radar,
                    mapping_version=MAPPING_VERSION,
                )
            report.increment("migrated")
            report.increment("kads", prepared.kads)
            report.increment("prefectures", prepared.prefectures)
            report.increment("legal_types", prepared.legal_types)
        except MappingProblem as exc:
            report.increment(exc.classification)
        except Exception:
            # A per-Radar failure is isolated by the transaction. The command deliberately prints counts, not
            # exception messages that could contain customer criteria or other data.
            report.increment("errors")
    return report
