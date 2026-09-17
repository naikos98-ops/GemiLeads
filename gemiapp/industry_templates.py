"""Industry template foundation (C4, blueprint §27).

§27 wants presets so a customer can target an industry without knowing KAD codes, mapped to «ΚΑΔ groups». There
is no canonical KAD group to map to:

* GEMI's ``/metadata/activities`` publishes ``id``, ``descr``, ``descrEn``, ``lastUpdated`` and ``kadVersion`` --
  no parent, level or group identifier (docs/GEMI_API_CAPABILITY_REPORT.md §6);
* A5 therefore stores none in ``GemiKad``;
* the code structure is not a proven hierarchy: the catalogue mixes 4-, 7- and 8-digit ids and trailing zeros do
  not denote a parent (01.00.00.00 is a special farming regime, not division 01 -- see
  gemiapp.ingestion.kad_catalogue);
* the repository holds no verified industry taxonomy (the ``kad_2025.json`` catalogue has code, description and
  source only).

So C4 takes the conservative path: an ``IndustryTemplate`` maps to **exact** ``GemiKad`` identities (code and
version). It is a curated preset, *not* an industry group, and C2's industry-group ICP dimension stays deferred
until a canonical grouping exists. Nothing here derives hierarchy from prefixes, compares descriptions, guesses a
KAD 2008 -> 2026 crosswalk or classifies anything automatically.

Templates
---------
Platform-owned: no organization, user, subscription or Radar owns one. ``slug`` is the stable identity (lowercase
letters, digits and single hyphens) and never changes; ``name`` and ``description`` are presentation. A new template
is inactive. An inactive template may have no KADs; an active one needs at least one. Active only means a future
interface may offer the preset -- nothing matches, monitors, scores or notifies.

No template is seeded. The blueprint's examples (transport, restaurants, retail...) are not reviewed KAD mappings,
so the template tables stay empty until someone curates one.

KAD mappings
------------
One mapping is one exact (code, version). KAD 2008 and KAD 2026 rows are separate identities: a template covering
both must map both explicitly. Like the ICP and Radars, a retired reference row cannot be *added*; a mapping that
already exists survives the reference being retired later and may be kept when the mappings are replaced. Nothing
is ever substituted.

Applying a template
-------------------
``get_industry_template_definition`` and ``template_kad_proposal`` are read-only and return immutable values: the
exact KAD identities a future interface could copy into an ICP or Radar after review. There is deliberately no
foreign key from ``OrganizationRadar`` or ``OrganizationICP`` to a template and nothing applies one automatically:
if a template changes tomorrow, no customer's configuration silently changes with it.

Services are transactional and all-or-nothing. There is no URL, view, form, API, task, schedule or network call,
and the admin is read-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.apps import apps
from django.db import transaction

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SLUG_MAX_LENGTH = 64
NAME_MAX_LENGTH = 120
DESCRIPTION_MAX_LENGTH = 500


class IndustryTemplateError(ValueError):
    """An industry template request that violates the C4 rules. Nothing has been written when it is raised."""


@dataclass(frozen=True, order=True)
class KadIdentity:
    """An exact KAD: code and version. Descriptions are never part of identity."""

    code: str
    kad_version: str


@dataclass(frozen=True)
class IndustryTemplateDefinition:
    template_id: int
    slug: str
    name: str
    description: str
    active: bool
    kads: tuple  # KadIdentity values, sorted


def _model(name):
    return apps.get_model("gemiapp", name)


def _template(template):
    IndustryTemplate = _model("IndustryTemplate")
    if not isinstance(template, IndustryTemplate) or template.pk is None:
        raise IndustryTemplateError("a saved IndustryTemplate is required")
    return template


def _validated_kads(kads, *, already_mapped: frozenset = frozenset()) -> tuple:
    GemiKad = _model("GemiKad")
    kads = tuple(kads)
    for kad in kads:
        if not isinstance(kad, GemiKad) or kad.pk is None:
            raise IndustryTemplateError("template KADs must be saved GemiKad rows")
        if not kad.is_present and kad.pk not in already_mapped:
            raise IndustryTemplateError(f"KAD {kad.source_id} ({kad.kad_version}) is retired and cannot be added")
    if len({kad.pk for kad in kads}) != len(kads):
        raise IndustryTemplateError("duplicate KAD in template")
    return kads


def _text(value, what, max_length, *, required):
    if not isinstance(value, str):
        raise IndustryTemplateError(f"{what} must be text")
    value = value.strip()
    if required and not value:
        raise IndustryTemplateError(f"{what} is required")
    if len(value) > max_length:
        raise IndustryTemplateError(f"{what} may not exceed {max_length} characters")
    return value


def create_industry_template(*, slug: str, name: str, description: str = "", kads=(), active: bool = False):
    """Create a template with its exact KAD mappings, all-or-nothing."""
    IndustryTemplate = _model("IndustryTemplate")
    IndustryTemplateKad = _model("IndustryTemplateKad")
    if not isinstance(slug, str) or not SLUG_PATTERN.match(slug) or len(slug) > SLUG_MAX_LENGTH:
        raise IndustryTemplateError("slug must be lowercase letters, digits and single hyphens (max 64)")
    name = _text(name, "name", NAME_MAX_LENGTH, required=True)
    description = _text(description, "description", DESCRIPTION_MAX_LENGTH, required=False)
    if not isinstance(active, bool):
        raise IndustryTemplateError("active must be True or False")
    kads = _validated_kads(kads)
    if active and not kads:
        raise IndustryTemplateError("an active template needs at least one KAD")
    with transaction.atomic():
        if IndustryTemplate.objects.filter(slug=slug).exists():
            raise IndustryTemplateError(f"an industry template with slug {slug!r} already exists")
        template = IndustryTemplate.objects.create(slug=slug, name=name, description=description, active=active)
        IndustryTemplateKad.objects.bulk_create([IndustryTemplateKad(template=template, kad=kad) for kad in kads])
    return template


def replace_industry_template_kads(template, kads):
    """Replace every KAD mapping of a template, all-or-nothing. The slug never changes."""
    IndustryTemplate = _model("IndustryTemplate")
    IndustryTemplateKad = _model("IndustryTemplateKad")
    _template(template)
    with transaction.atomic():
        row = IndustryTemplate.objects.select_for_update().get(pk=template.pk)
        mapped = frozenset(row.kads.values_list("kad_id", flat=True))
        kads = _validated_kads(kads, already_mapped=mapped)
        if row.active and not kads:
            raise IndustryTemplateError("an active template needs at least one KAD; deactivate it first")
        row.kads.all().delete()
        IndustryTemplateKad.objects.bulk_create([IndustryTemplateKad(template=row, kad=kad) for kad in kads])
        row.save(update_fields=["updated_at"])
    return row


def set_industry_template_active(template, active: bool):
    """Offer or withdraw a template. Configuration only: nothing runs."""
    IndustryTemplate = _model("IndustryTemplate")
    _template(template)
    if not isinstance(active, bool):
        raise IndustryTemplateError("active must be True or False")
    with transaction.atomic():
        row = IndustryTemplate.objects.select_for_update().get(pk=template.pk)
        if active and not row.kads.exists():
            raise IndustryTemplateError("an active template needs at least one KAD")
        row.active = active
        row.save(update_fields=["active", "updated_at"])
    return row


def template_kad_proposal(template) -> tuple:
    """The exact, sorted KAD identities a reviewed template proposes. Read-only."""
    _template(template)
    rows = _model("IndustryTemplateKad").objects.filter(template_id=template.pk).values_list(
        "kad__source_id", "kad__kad_version")
    return tuple(sorted(KadIdentity(code, version) for code, version in rows))


def get_industry_template_definition(template) -> IndustryTemplateDefinition:
    """The template as an immutable value. Read-only."""
    _template(template)
    row = _model("IndustryTemplate").objects.get(pk=template.pk)
    return IndustryTemplateDefinition(
        template_id=row.pk, slug=row.slug, name=row.name, description=row.description, active=row.active,
        kads=template_kad_proposal(row),
    )
