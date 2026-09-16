"""Organization ICP -- Ideal Customer Profile (C2, blueprint §24).

The ICP answers: *which kinds of businesses could be good customers for this organization?* It is structured,
organization-owned customer intelligence, distinct from its neighbours:

* ``OrganizationProfile`` (C1) -- who the organization is and what it sells, in its own free text;
* ``OrganizationICP`` (C2) -- the structured definition of a promising customer;
* Radar (§25, later) -- a specific operational watch with its own rules.

C2 only stores the ICP. Nothing matches companies against it, scores anything, creates leads, opportunities,
monitoring reasons or signals, or reads it in the legacy Radar pipeline. The C1 profile text is never parsed
into criteria, and no ICP is derived from existing Radars or users -- those semantics are not equivalent.

One ICP per organization
------------------------
§24 says «Κάθε customer δημιουργεί ICP» -- one ICP per customer. Specific searches are Radars. The ICP is
created explicitly (an organization may exist without one) and exactly once.

Dimensions (§24)
----------------
Each implemented dimension uses canonical GEMI reference identity, never a description:

* **KAD** -- exact ``GemiKad`` rows, so the code *and* its version are the identity: the same code in KAD 2008
  and KAD 2026 are two different criteria. No prefix, 2/4/6-digit or parent semantics are assumed: §9 plans a
  ``kad_dictionary`` with ``parent_code``/``level`` for category targeting, but no such hierarchy exists yet,
  and legacy 4-digit Radar values are not GEMI ids.
* **Geographical areas** -- prefecture or municipality, with an explicit level and a separate foreign key per
  level (``GemiPrefecture`` / ``GemiMunicipality``), so equal source ids at different levels never collide.
* **Legal forms** -- ``GemiLegalType``.
* **Business status** -- ``GemiCompanyStatus``. ``Company.is_active`` and status descriptions play no part.
* **Company age** -- a range in whole months (``minimum_age_months`` / ``maximum_age_months``, either end open,
  zero allowed, minimum <= maximum). §24 gives no unit, so months is chosen deliberately: fine enough for young
  companies, and never a moving "incorporated after <date>" that silently changes meaning. A later matcher
  evaluates it against an explicit ``as_of``.
* **Signal types** -- the ``CompanySignal`` taxonomy, include-only, and only types whose detector is implemented
  (``SIGNAL_RULES``): the ICP must not promise events nothing produces.
* **Exclusions** -- not free text and not a separate store: KAD, region, legal-form and status criteria each
  carry a polarity, INCLUDE or EXCLUDE. A criterion's identity ignores polarity, so the same KAD, area, legal
  form or status can neither repeat nor be both included and excluded (enforced by database uniqueness and
  reported clearly by the service). Different levels are not compared: including a prefecture while excluding
  one of its municipalities is legitimate. There are no company-name or single-company blacklists.

Deliberately not implemented
----------------------------
* **Industry groups** -- no canonical industry-group taxonomy exists in the blueprint or the repository. §27
  describes industry templates mapped to «ΚΑΔ groups», which in turn need the §9 KAD hierarchy. Inventing a
  classification here would become a permanent, unowned taxonomy, and ``OrganizationProfile.business`` is not
  one. The industry-templates package must first provide the taxonomy and its KAD mapping.
* **Priorities** -- §24 names them but defines no scale, ranking or weight. Scoring weights appear only in §30
  for opportunity scoring, which is a later package. Nothing is stored for priorities and no weight is invented.

Configured vs empty
-------------------
An ICP with no criteria and no age bound is *not configured yet*. It never means "every company matches".
``is_icp_configured`` derives the state; nothing is persisted for it.

Updates are all-or-nothing
--------------------------
``replace_organization_icp`` validates a complete ``ICPCriteria`` value, then replaces every criterion and the
age range inside one transaction holding a lock on the ICP row. A rejected or failing update leaves the
previous ICP exactly as it was.

Tenancy and G5
--------------
Every row hangs off exactly one Organization; nothing references a User. Services take the organization
explicitly -- never the logged-in user, a session or a "current organization" -- and no view, URL, form,
middleware or task exists. Organization-owned intelligence now exists internally, but customer-facing tenant
context and multi-member use remain blocked by G5. The admin is read-only.

Nothing here calls GEMI, the web, Stripe or email.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.apps import apps
from django.db import transaction

from .company_signals import SIGNAL_TYPES, rule_for

INCLUDE = "include"
EXCLUDE = "exclude"
POLARITIES = (INCLUDE, EXCLUDE)


class ICPError(ValueError):
    """An ICP request that violates the C2 rules. Nothing has been written when it is raised."""


@dataclass(frozen=True)
class KadCriterion:
    kad: object  # GemiKad
    polarity: str = INCLUDE


@dataclass(frozen=True)
class RegionCriterion:
    area: object  # GemiPrefecture or GemiMunicipality; the model decides the level
    polarity: str = INCLUDE


@dataclass(frozen=True)
class LegalFormCriterion:
    legal_type: object  # GemiLegalType
    polarity: str = INCLUDE


@dataclass(frozen=True)
class StatusCriterion:
    status: object  # GemiCompanyStatus
    polarity: str = INCLUDE


@dataclass(frozen=True)
class ICPCriteria:
    """A complete ICP definition. Replacing an ICP always supplies the whole value."""

    kads: tuple = ()
    regions: tuple = ()
    legal_forms: tuple = ()
    statuses: tuple = ()
    signal_types: tuple = ()
    minimum_age_months: int | None = None
    maximum_age_months: int | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.kads or self.regions or self.legal_forms or self.statuses or self.signal_types
                    or self.minimum_age_months is not None or self.maximum_age_months is not None)


def implemented_signal_types() -> tuple:
    return tuple(t for t in SIGNAL_TYPES if (rule := rule_for(t)) is not None and rule.implemented)


def _model(name):
    return apps.get_model("gemiapp", name)


def _reference(value, model_name, what):
    model = _model(model_name)
    if not isinstance(value, model) or value.pk is None:
        raise ICPError(f"{what} must be a saved {model_name}")
    if not value.is_present:
        raise ICPError(f"{what} {value.source_id} is retired from the GEMI reference data")
    return value


def _polarity(value):
    if value not in POLARITIES:
        raise ICPError(f"polarity must be {INCLUDE} or {EXCLUDE}, got {value!r}")
    return value


def _unique(identities, what):
    seen = {}
    for identity, polarity in identities:
        if identity in seen:
            if seen[identity] != polarity:
                raise ICPError(f"the same {what} cannot be both included and excluded: {identity}")
            raise ICPError(f"duplicate {what}: {identity}")
        seen[identity] = polarity


def _age(value, name):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ICPError(f"{name} must be a non-negative whole number of months")
    return value


def validate_icp_criteria(criteria: ICPCriteria) -> None:
    """Every rule a stored ICP must satisfy, checked before any write."""
    if not isinstance(criteria, ICPCriteria):
        raise ICPError("criteria must be an ICPCriteria value")
    kads = []
    for item in criteria.kads:
        if not isinstance(item, KadCriterion):
            raise ICPError("kads must contain KadCriterion values")
        kad = _reference(item.kad, "GemiKad", "KAD")
        kads.append(((kad.source_id, kad.kad_version), _polarity(item.polarity)))
    _unique(kads, "KAD")

    regions = []
    for item in criteria.regions:
        if not isinstance(item, RegionCriterion):
            raise ICPError("regions must contain RegionCriterion values")
        if isinstance(item.area, _model("GemiPrefecture")):
            area = _reference(item.area, "GemiPrefecture", "prefecture")
            regions.append((("prefecture", area.source_id), _polarity(item.polarity)))
        elif isinstance(item.area, _model("GemiMunicipality")):
            area = _reference(item.area, "GemiMunicipality", "municipality")
            regions.append((("municipality", area.source_id), _polarity(item.polarity)))
        else:
            raise ICPError("a region must be a GemiPrefecture or a GemiMunicipality")
    _unique(regions, "region")

    for items, cls, attr, model_name, what in (
        (criteria.legal_forms, LegalFormCriterion, "legal_type", "GemiLegalType", "legal form"),
        (criteria.statuses, StatusCriterion, "status", "GemiCompanyStatus", "status"),
    ):
        identities = []
        for item in items:
            if not isinstance(item, cls):
                raise ICPError(f"{what} criteria must be {cls.__name__} values")
            reference = _reference(getattr(item, attr), model_name, what)
            identities.append((reference.source_id, _polarity(item.polarity)))
        _unique(identities, what)

    allowed = implemented_signal_types()
    for signal_type in criteria.signal_types:
        if signal_type not in SIGNAL_TYPES:
            raise ICPError(f"unknown signal type: {signal_type!r}")
        if signal_type not in allowed:
            raise ICPError(f"{signal_type} has no implemented detector and cannot be targeted yet")
    if len(set(criteria.signal_types)) != len(criteria.signal_types):
        raise ICPError("duplicate signal type")

    minimum = _age(criteria.minimum_age_months, "minimum_age_months")
    maximum = _age(criteria.maximum_age_months, "maximum_age_months")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ICPError("minimum_age_months cannot exceed maximum_age_months")


def _saved_organization(organization):
    if not isinstance(organization, _model("Organization")) or organization.pk is None:
        raise ICPError("an explicit, saved Organization is required")
    return organization


def create_organization_icp(organization, criteria: ICPCriteria | None = None):
    """Create the organization's one ICP, optionally with its first criteria. Refuses a second ICP."""
    OrganizationICP = _model("OrganizationICP")
    _saved_organization(organization)
    criteria = criteria or ICPCriteria()
    validate_icp_criteria(criteria)
    with transaction.atomic():
        if OrganizationICP.objects.filter(organization=organization).exists():
            raise ICPError("this organization already has an ICP; replace its criteria instead")
        icp = OrganizationICP.objects.create(organization=organization)
        _write_criteria(icp, criteria)
    return icp


def replace_organization_icp(organization, criteria: ICPCriteria):
    """Replace the whole ICP definition of this organization, all-or-nothing."""
    OrganizationICP = _model("OrganizationICP")
    _saved_organization(organization)
    validate_icp_criteria(criteria)
    with transaction.atomic():
        icp = OrganizationICP.objects.select_for_update().filter(organization=organization).first()
        if icp is None:
            raise ICPError("this organization has no ICP yet; create it first")
        for related in (icp.kads, icp.regions, icp.legal_forms, icp.statuses, icp.signal_types):
            related.all().delete()
        _write_criteria(icp, criteria)
    return icp


def _write_criteria(icp, criteria: ICPCriteria) -> None:
    Region = _model("OrganizationICPRegion")
    _model("OrganizationICPKad").objects.bulk_create(
        [_model("OrganizationICPKad")(icp=icp, kad=item.kad, polarity=item.polarity) for item in criteria.kads]
    )
    Region.objects.bulk_create([
        Region(icp=icp, level=Region.PREFECTURE, prefecture=item.area, polarity=item.polarity)
        if isinstance(item.area, _model("GemiPrefecture"))
        else Region(icp=icp, level=Region.MUNICIPALITY, municipality=item.area, polarity=item.polarity)
        for item in criteria.regions
    ])
    _model("OrganizationICPLegalForm").objects.bulk_create([
        _model("OrganizationICPLegalForm")(icp=icp, legal_type=item.legal_type, polarity=item.polarity)
        for item in criteria.legal_forms
    ])
    _model("OrganizationICPStatus").objects.bulk_create([
        _model("OrganizationICPStatus")(icp=icp, status=item.status, polarity=item.polarity)
        for item in criteria.statuses
    ])
    _model("OrganizationICPSignalType").objects.bulk_create([
        _model("OrganizationICPSignalType")(icp=icp, signal_type=signal_type) for signal_type in criteria.signal_types
    ])
    icp.minimum_age_months = criteria.minimum_age_months
    icp.maximum_age_months = criteria.maximum_age_months
    icp.save(update_fields=["minimum_age_months", "maximum_age_months", "updated_at"])


def get_organization_icp_criteria(organization) -> ICPCriteria | None:
    """The stored ICP as an immutable ICPCriteria value, or None when the organization has none."""
    OrganizationICP = _model("OrganizationICP")
    _saved_organization(organization)
    icp = (
        OrganizationICP.objects.filter(organization=organization)
        .prefetch_related("kads__kad", "regions__prefecture", "regions__municipality", "legal_forms__legal_type",
                          "statuses__status", "signal_types")
        .first()
    )
    if icp is None:
        return None
    return ICPCriteria(
        kads=tuple(KadCriterion(row.kad, row.polarity) for row in sorted(icp.kads.all(), key=lambda r: r.pk)),
        regions=tuple(RegionCriterion(row.prefecture or row.municipality, row.polarity)
                      for row in sorted(icp.regions.all(), key=lambda r: r.pk)),
        legal_forms=tuple(LegalFormCriterion(row.legal_type, row.polarity)
                          for row in sorted(icp.legal_forms.all(), key=lambda r: r.pk)),
        statuses=tuple(StatusCriterion(row.status, row.polarity) for row in sorted(icp.statuses.all(), key=lambda r: r.pk)),
        signal_types=tuple(row.signal_type for row in sorted(icp.signal_types.all(), key=lambda r: r.pk)),
        minimum_age_months=icp.minimum_age_months, maximum_age_months=icp.maximum_age_months,
    )


def is_icp_configured(organization) -> bool:
    """Whether the organization has stated any ICP criterion. An empty ICP is unconfigured, never universal."""
    criteria = get_organization_icp_criteria(organization)
    return criteria is not None and not criteria.is_empty
