"""Organization Radar foundation (C3, blueprint §25).

A Radar is «stored search + opportunity rules»: a specific, organization-owned market watch. An organization has
many Radars. It is distinct from its neighbours:

* ``OrganizationICP`` (C2) -- broadly, who could be our customer;
* ``OrganizationRadar`` (C3) -- which specific segment and events we want watched.

The ICP is never copied into a Radar, and never narrows one.

Dormant, and coexisting with CustomerRadar
------------------------------------------
``CustomerRadar`` is the production Radar and stays authoritative: its forms, matching, RadarMatch rows, digests,
subscription Radar limits, A9 ``ACTIVE_RADAR_MATCH`` monitoring and the B4 refresh planner read only the legacy,
user-owned model with its description-string criteria. ``OrganizationRadar`` is the Phase-C architecture built in
parallel with canonical GEMI identities. The two are not assumed equivalent: there is no synchronisation, no
backfill from CustomerRadar and no organization created for their users. A later, explicit cutover decides any
mapping.

Nothing consumes an OrganizationRadar yet. ``active`` is stored configuration only: activating one runs no
matching, creates no match, lead, opportunity, signal or monitoring reason, schedules no task and calls nothing.

What §25 defines, and what it does not
--------------------------------------
Defined: ``radars`` with ``organization_id``, ``name``, ``active``, ``score_threshold``, ``created_at``, and the
supporting ``radar_kads``, ``radar_regions``, ``radar_legal_forms``, ``radar_signal_types``, ``radar_exclusions``.
§30-§31 put the opportunity score on a 0-100 scale and say the customer may later change thresholds.

Not defined, and therefore decided here conservatively:

* **Default activity** -- new Radars are inactive until the matching layer exists.
* **Name uniqueness** -- names are labels, duplicates within an organization are allowed. Length follows the
  legacy Radar name (80).
* **Score threshold** -- an optional integer 0-100 on the §30 scale, null meaning "not set". It is stored and
  range-checked only: nothing calculates a score or compares against it, and activation does not require it.
* **Regions** -- §25 names no level; prefecture and municipality are the canonical GEMI levels available.
* **Exclusions** -- no schema is given. v1 exclusions are structured negatives on the dimensions the Radar already
  supports as canonical identities: an exact KAD, a prefecture, a municipality or a legal type. Signal types are
  not excludable: a Radar simply does not list the events it does not watch. No free text, names, expressions or
  personal data.
* **What an active Radar needs** -- see Activation.

Criteria
--------
* KAD -- exact ``GemiKad`` rows, so code *and* version are the identity (KAD 2008 and 2026 stay distinct). No
  prefix or hierarchy semantics; legacy 4-digit values are not GEMI ids.
* Regions -- explicit level with a separate foreign key per level, so equal source ids never collide.
* Legal forms -- ``GemiLegalType``.
* Signal types -- the ``CompanySignal`` taxonomy, only types with an implemented detector.

Like the ICP, new criteria may not reference reference rows retired from GEMI; an existing criterion keeps its
reference if it retires later.

Contradictions
--------------
Within one table the database refuses duplicates. The same exact identity may not appear both as a positive
criterion and as an exclusion; that spans two tables, so the service enforces it before writing. Different
levels are not compared: including a prefecture while excluding one of its municipalities is legitimate.

Activation
----------
A Radar may exist as an empty or partial draft while inactive. An **active** Radar must have at least one
positive criterion -- a KAD, a region, a legal form or a signal type. A signal-type-only Radar ("every
NEW_COMPANY") is valid: §28 matching starts from a signal and filters by the Radar, and the production product
already has criteria-less Radars. An exclusions-only Radar is not configured: exclusions narrow a definition,
they never stand for "everything else in the registry". ``is_radar_configured`` derives this; nothing persists it.
Deactivating only changes configuration.

Services
--------
``create_organization_radar`` and ``replace_organization_radar`` validate a complete ``RadarDefinition`` and write
root and criteria inside one transaction; a rejected or failing call writes nothing and leaves an existing Radar
as it was. ``set_organization_radar_active`` toggles activity under the same activation rule. Every call takes
the Organization explicitly and refuses a Radar belonging to another organization -- never the logged-in user,
a session or a current-organization context. Multi-member access stays blocked by G5; there is no URL, view,
form, API, middleware, task or schedule, and the admin is read-only.

Nothing here calls GEMI, the web, Stripe or email.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.apps import apps
from django.db import transaction

from .company_signals import SIGNAL_TYPES, rule_for

NAME_MAX_LENGTH = 80
SCORE_MIN, SCORE_MAX = 0, 100


class RadarError(ValueError):
    """An Organization Radar request that violates the C3 rules. Nothing has been written when it is raised."""


@dataclass(frozen=True)
class RadarDefinition:
    """A complete Radar configuration. Replacing a Radar always supplies the whole value.

    ``regions`` and ``exclusions`` hold canonical rows; their model decides the level or subject
    (GemiKad, GemiPrefecture, GemiMunicipality, GemiLegalType).
    """

    name: str
    active: bool = False
    score_threshold: int | None = None
    kads: tuple = ()
    regions: tuple = ()
    legal_forms: tuple = ()
    signal_types: tuple = ()
    exclusions: tuple = ()

    @property
    def has_positive_criteria(self) -> bool:
        return bool(self.kads or self.regions or self.legal_forms or self.signal_types)


def _model(name):
    return apps.get_model("gemiapp", name)


def implemented_signal_types() -> tuple:
    return tuple(t for t in SIGNAL_TYPES if (rule := rule_for(t)) is not None and rule.implemented)


def _canonical(value, allowed: tuple, what: str):
    """The canonical identity of a saved, present reference row of one of the allowed models."""
    for model_name, kind in allowed:
        model = _model(model_name)
        if isinstance(value, model):
            if value.pk is None:
                raise RadarError(f"{what} must be a saved {model_name}")
            if not value.is_present:
                raise RadarError(f"{what} {value.source_id} is retired from the GEMI reference data")
            identity = (kind, value.source_id, value.kad_version) if kind == "kad" else (kind, value.source_id)
            return identity
    raise RadarError(f"{what} must be one of: {', '.join(name for name, _ in allowed)}")


def _identities(values, allowed, what):
    identities = [_canonical(value, allowed, what) for value in values]
    if len(set(identities)) != len(identities):
        raise RadarError(f"duplicate {what}")
    return identities


KAD = (("GemiKad", "kad"),)
REGION = (("GemiPrefecture", "prefecture"), ("GemiMunicipality", "municipality"))
LEGAL_FORM = (("GemiLegalType", "legal_form"),)
EXCLUDABLE = KAD + REGION + LEGAL_FORM


def validate_radar_definition(definition: RadarDefinition) -> RadarDefinition:
    """Every rule a stored Radar must satisfy, checked before any write. Returns the definition with the
    name trimmed."""
    if not isinstance(definition, RadarDefinition):
        raise RadarError("definition must be a RadarDefinition value")
    name = definition.name.strip() if isinstance(definition.name, str) else ""
    if not name:
        raise RadarError("a Radar needs a name")
    if len(name) > NAME_MAX_LENGTH:
        raise RadarError(f"a Radar name may not exceed {NAME_MAX_LENGTH} characters")
    if not isinstance(definition.active, bool):
        raise RadarError("active must be True or False")
    threshold = definition.score_threshold
    if threshold is not None and (isinstance(threshold, bool) or not isinstance(threshold, int)
                                  or not SCORE_MIN <= threshold <= SCORE_MAX):
        raise RadarError(f"score_threshold must be a whole number from {SCORE_MIN} to {SCORE_MAX}")

    positive = set()
    positive.update(_identities(definition.kads, KAD, "KAD"))
    positive.update(_identities(definition.regions, REGION, "region"))
    positive.update(_identities(definition.legal_forms, LEGAL_FORM, "legal form"))
    excluded = _identities(definition.exclusions, EXCLUDABLE, "exclusion")
    contradictions = sorted(positive.intersection(excluded))
    if contradictions:
        raise RadarError(f"the same criterion cannot be both targeted and excluded: {contradictions}")

    allowed = implemented_signal_types()
    for signal_type in definition.signal_types:
        if signal_type not in SIGNAL_TYPES:
            raise RadarError(f"unknown signal type: {signal_type!r}")
        if signal_type not in allowed:
            raise RadarError(f"{signal_type} has no implemented detector and cannot be watched yet")
    if len(set(definition.signal_types)) != len(definition.signal_types):
        raise RadarError("duplicate signal type")

    if definition.active and not definition.has_positive_criteria:
        raise RadarError("an active Radar needs at least one KAD, region, legal form or signal type")
    return RadarDefinition(
        name=name, active=definition.active, score_threshold=threshold, kads=tuple(definition.kads),
        regions=tuple(definition.regions), legal_forms=tuple(definition.legal_forms),
        signal_types=tuple(definition.signal_types), exclusions=tuple(definition.exclusions),
    )


def _saved_organization(organization):
    if not isinstance(organization, _model("Organization")) or organization.pk is None:
        raise RadarError("an explicit, saved Organization is required")
    return organization


def _owned_radar(organization, radar, *, for_update=False):
    """The Radar row, re-read and locked, only if it belongs to this organization."""
    OrganizationRadar = _model("OrganizationRadar")
    if not isinstance(radar, OrganizationRadar) or radar.pk is None:
        raise RadarError("an explicit, saved OrganizationRadar is required")
    queryset = OrganizationRadar.objects.filter(pk=radar.pk, organization=organization)
    if for_update:
        queryset = queryset.select_for_update()
    row = queryset.first()
    if row is None:
        raise RadarError("this Radar does not belong to the given organization")
    return row


def _write_criteria(radar, definition: RadarDefinition) -> None:
    Region = _model("OrganizationRadarRegion")
    Exclusion = _model("OrganizationRadarExclusion")
    Prefecture, Municipality, Kad = _model("GemiPrefecture"), _model("GemiMunicipality"), _model("GemiKad")
    _model("OrganizationRadarKad").objects.bulk_create(
        [_model("OrganizationRadarKad")(radar=radar, kad=kad) for kad in definition.kads]
    )
    Region.objects.bulk_create([
        Region(radar=radar, level=Region.PREFECTURE, prefecture=area) if isinstance(area, Prefecture)
        else Region(radar=radar, level=Region.MUNICIPALITY, municipality=area)
        for area in definition.regions
    ])
    _model("OrganizationRadarLegalForm").objects.bulk_create(
        [_model("OrganizationRadarLegalForm")(radar=radar, legal_type=legal_type) for legal_type in definition.legal_forms]
    )
    _model("OrganizationRadarSignalType").objects.bulk_create(
        [_model("OrganizationRadarSignalType")(radar=radar, signal_type=t) for t in definition.signal_types]
    )
    exclusions = []
    for value in definition.exclusions:
        if isinstance(value, Kad):
            exclusions.append(Exclusion(radar=radar, subject=Exclusion.KAD, kad=value))
        elif isinstance(value, Prefecture):
            exclusions.append(Exclusion(radar=radar, subject=Exclusion.PREFECTURE, prefecture=value))
        elif isinstance(value, Municipality):
            exclusions.append(Exclusion(radar=radar, subject=Exclusion.MUNICIPALITY, municipality=value))
        else:
            exclusions.append(Exclusion(radar=radar, subject=Exclusion.LEGAL_FORM, legal_type=value))
    Exclusion.objects.bulk_create(exclusions)


def create_organization_radar(organization, definition: RadarDefinition):
    """Create one Radar of this organization with its whole configuration, all-or-nothing."""
    _saved_organization(organization)
    definition = validate_radar_definition(definition)
    with transaction.atomic():
        radar = _model("OrganizationRadar").objects.create(
            organization=organization, name=definition.name, active=definition.active,
            score_threshold=definition.score_threshold,
        )
        _write_criteria(radar, definition)
    return radar


def replace_organization_radar(organization, radar, definition: RadarDefinition):
    """Replace the whole configuration of one of this organization's Radars, all-or-nothing."""
    _saved_organization(organization)
    definition = validate_radar_definition(definition)
    with transaction.atomic():
        row = _owned_radar(organization, radar, for_update=True)
        for related in (row.kads, row.regions, row.legal_forms, row.signal_types, row.exclusions):
            related.all().delete()
        row.name, row.active, row.score_threshold = definition.name, definition.active, definition.score_threshold
        row.save(update_fields=["name", "active", "score_threshold", "updated_at"])
        _write_criteria(row, definition)
    return row


def get_organization_radar_definition(organization, radar) -> RadarDefinition:
    """The stored configuration of one of this organization's Radars, as an immutable value."""
    _saved_organization(organization)
    row = _owned_radar(organization, radar)
    by_pk = lambda rows: sorted(rows, key=lambda r: r.pk)  # noqa: E731 -- insertion order
    exclusions = []
    for item in by_pk(row.exclusions.select_related("kad", "prefecture", "municipality", "legal_type")):
        exclusions.append(item.kad or item.prefecture or item.municipality or item.legal_type)
    return RadarDefinition(
        name=row.name, active=row.active, score_threshold=row.score_threshold,
        kads=tuple(item.kad for item in by_pk(row.kads.select_related("kad"))),
        regions=tuple(item.prefecture or item.municipality
                      for item in by_pk(row.regions.select_related("prefecture", "municipality"))),
        legal_forms=tuple(item.legal_type for item in by_pk(row.legal_forms.select_related("legal_type"))),
        signal_types=tuple(item.signal_type for item in by_pk(row.signal_types.all())),
        exclusions=tuple(exclusions),
    )


def set_organization_radar_active(organization, radar, active: bool):
    """Activate or deactivate one of this organization's Radars. Configuration only: nothing runs."""
    _saved_organization(organization)
    if not isinstance(active, bool):
        raise RadarError("active must be True or False")
    with transaction.atomic():
        row = _owned_radar(organization, radar, for_update=True)
        if active and not is_radar_configured(row):
            raise RadarError("an active Radar needs at least one KAD, region, legal form or signal type")
        row.active = active
        row.save(update_fields=["active", "updated_at"])
    return row


def is_radar_configured(radar) -> bool:
    """Whether a Radar has at least one positive criterion. Exclusions alone never configure a Radar."""
    return any(related.exists() for related in (radar.kads, radar.regions, radar.legal_forms, radar.signal_types))
