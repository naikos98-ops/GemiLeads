"""KAD catalogue reconciliation: the live ActivityCode catalogue and A5 GemiKad reference data (A8).

Two catalogues
--------------
* ``ActivityCode`` is the live product catalogue. The KAD picker searches it, Radars store their criteria as
  links to it, and the importer adds a fallback entry for any unknown company code. Its identity is the KAD
  code alone (``normalized_code``, unique) and it has no version. On the copied development database it holds
  10,467 entries: 9,647 from the AADE KAD 2025 list (``kad_2025.json``), 4 from a later AADE circular, and 816
  fallback entries created from company records (blank ``source``).
* ``GemiKad`` is the A5 copy of GET /metadata/activities, with identity (code, ``kad_version``). The live
  capability probe counted 9,652 ``kad_2026`` and 9,716 ``kad_2008`` entries, 3,636 codes present in both
  versions, and 9,648 of the 9,651 ``kad_2025.json`` codes present in ``kad_2026`` (none in ``kad_2008`` only).

Bridge
------
``ActivityCodeKadLink`` links an ActivityCode to every GemiKad row with exactly the same code digits. That is
at most one row per KAD version, so a code published in both versions links to both rows and the upstream
identities never collapse. The code is the only match criterion. Descriptions are compared case-, accent- and
whitespace-insensitively and the result is stored as evidence in ``description_matches``; it never creates or
refuses a link. ActivityCode itself is not changed, and neither are saved Radar criteria.

Classification
--------------
Each ActivityCode is classified from the reference rows present in the latest successful sync:

* ``kad_2026``: current taxonomy only;
* ``kad_2008_only``: historical only;
* ``both_versions``: the code exists in both versions;
* ``other_version_only``: only a version other than 2008/2026, or an unpublished version;
* ``unresolved``: no present reference row. ``unresolved_reason`` says which: ``no_reference_data`` (GemiKad
  has never been synchronised), ``not_in_reference`` (the code is absent) or ``retired_in_reference`` (its
  rows were retired).

Retired reference rows keep their links and are reported, but are never counted as present.

KAD 2008 -> KAD 2026
--------------------
GEMI publishes no crosswalk between the versions: the metadata items carry only id, descr, descrEn,
kadVersion and lastUpdated. So none is stored or inferred, and a KAD 2008 entry and a KAD 2026 entry with
similar descriptions are never treated as equivalent. ``OFFICIAL_KAD_CROSSWALK = None`` records that this
capability is unresolved.

What later logic can rely on:

* every CompanyActivity row keeps its published ``kad_version`` and ``dtFrom`` / ``dtTo`` (A7);
* ``gemi_kad_for_activity`` resolves a row to the reference entry of that same version only.

The observed mass transition (KAD 2008 entries ending around 2026-03-01, KAD 2026 reference rows last updated
2026-02-25) is evidence for the future signal detector's suppression strategy, not a rule encoded here.

Hierarchy
---------
Not derived, because no rule is proven:

* the catalogue mixes 8-digit codes with a 4-digit code (00.04) and 7-digit fallback codes;
* the reference publishes 556 7-digit ids;
* trailing zeros do not reliably denote a parent: 01.00.00.00 is "ΑΓΡΟΤΗΣ ΕΙΔΙΚΟΥ ΚΑΘΕΣΤΩΤΟΣ", not division 01.

The primitive a later, proven rule would need -- the code digits -- is already stored in
``ActivityCode.normalized_code`` and ``GemiKad.source_id``.

Fallback entries
----------------
The importer's fallback (gemiapp.ingestion.activities) still creates an ActivityCode with a blank source for an
unknown code, so no import depends on reference data. Nothing more is needed to classify it later: its code
digits link it to reference rows at the next reconciliation, and the company's CompanyActivity rows keep the
version it was published under.

Picker
------
``kad_picker_queryset`` is what the KAD picker searches. With GEMI_KAD_PICKER_CURRENT_TAXONOMY_ONLY off (the
default) it is the whole ActivityCode catalogue, exactly as before. With it on, it is only the entries linked
to a present KAD 2026 reference row.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..kad import normalize_kad_code, normalize_kad_search

logger = logging.getLogger(__name__)

CURRENT_KAD_VERSION = "kad_2026"
HISTORICAL_KAD_VERSION = "kad_2008"
# GEMI publishes no mapping between KAD 2008 and KAD 2026 entries; none is stored or inferred.
OFFICIAL_KAD_CROSSWALK = None
MATCH_BASIS_CODE = "code"

KAD_2026 = "kad_2026"
KAD_2008_ONLY = "kad_2008_only"
BOTH_VERSIONS = "both_versions"
OTHER_VERSION_ONLY = "other_version_only"
UNRESOLVED = "unresolved"
CLASSIFICATIONS = (KAD_2026, KAD_2008_ONLY, BOTH_VERSIONS, OTHER_VERSION_ONLY, UNRESOLVED)
NO_REFERENCE_DATA = "no_reference_data"
NOT_IN_REFERENCE = "not_in_reference"
RETIRED_IN_REFERENCE = "retired_in_reference"
AADE_CATALOGUE = "aade_catalogue"
GEMI_FALLBACK = "gemi_fallback"


@dataclass(frozen=True)
class ReferenceKad:
    pk: int
    kad_version: str
    is_present: bool
    description: str | None


@dataclass(frozen=True)
class KadCompatibility:
    classification: str
    present_versions: frozenset
    retired_versions: frozenset
    unresolved_reason: str | None = None

    @property
    def in_current_taxonomy(self) -> bool:
        return CURRENT_KAD_VERSION in self.present_versions


def descriptions_match(first, second) -> bool:
    return normalize_kad_search(first) == normalize_kad_search(second)


def catalogue_origin(source: str) -> str:
    return AADE_CATALOGUE if source else GEMI_FALLBACK


def load_reference() -> dict[str, list[ReferenceKad]]:
    """Every GemiKad row, present or retired, by code digits."""
    GemiKad = apps.get_model("gemiapp", "GemiKad")
    reference: dict[str, list[ReferenceKad]] = defaultdict(list)
    rows = GemiKad.objects.order_by("pk").values_list("pk", "source_id", "kad_version", "is_present", "description")
    for pk, source_id, kad_version, is_present, description in rows.iterator(chunk_size=5000):
        code = normalize_kad_code(source_id)
        if code:
            reference[code].append(ReferenceKad(pk, kad_version, is_present, description))
    return dict(reference)


def reference_available(reference: dict[str, list[ReferenceKad]]) -> bool:
    return any(row.is_present for rows in reference.values() for row in rows)


def classify_code(code: str, reference: dict[str, list[ReferenceKad]], *, available: bool | None = None) -> KadCompatibility:
    rows = reference.get(code, [])
    present = frozenset(row.kad_version for row in rows if row.is_present)
    retired = frozenset(row.kad_version for row in rows if not row.is_present) - present
    if CURRENT_KAD_VERSION in present and HISTORICAL_KAD_VERSION in present:
        return KadCompatibility(BOTH_VERSIONS, present, retired)
    if CURRENT_KAD_VERSION in present:
        return KadCompatibility(KAD_2026, present, retired)
    if HISTORICAL_KAD_VERSION in present:
        return KadCompatibility(KAD_2008_ONLY, present, retired)
    if present:
        return KadCompatibility(OTHER_VERSION_ONLY, present, retired)
    available = reference_available(reference) if available is None else available
    reason = NO_REFERENCE_DATA if not available else (RETIRED_IN_REFERENCE if rows else NOT_IN_REFERENCE)
    return KadCompatibility(UNRESOLVED, present, retired, reason)


# --- version-aware helpers ----------------------------------------------------------------------

def current_taxonomy_picker_enabled(value: bool | None = None) -> bool:
    if value is None:
        return bool(getattr(settings, "GEMI_KAD_PICKER_CURRENT_TAXONOMY_ONLY", False))
    return bool(value)


def activity_codes_in_version(kad_version: str, *, present_only: bool = True):
    """ActivityCode entries linked to a reference row of ``kad_version``."""
    ActivityCode = apps.get_model("gemiapp", "ActivityCode")
    lookups = {"kad_links__gemi_kad__kad_version": kad_version}
    if present_only:
        lookups["kad_links__gemi_kad__is_present"] = True
    return ActivityCode.objects.filter(**lookups).distinct()


def kad_picker_queryset(*, current_taxonomy_only: bool | None = None):
    """What the KAD picker searches: the whole catalogue unless the current-taxonomy picker is enabled."""
    if current_taxonomy_picker_enabled(current_taxonomy_only):
        return activity_codes_in_version(CURRENT_KAD_VERSION)
    return apps.get_model("gemiapp", "ActivityCode").objects.all()


def gemi_kad_for_activity(activity):
    """The reference row of the activity's own code and published version; never another version's row."""
    GemiKad = apps.get_model("gemiapp", "GemiKad")
    return GemiKad.objects.filter(source_id=activity.code, kad_version=activity.kad_version or "").first()


# --- reconciliation -----------------------------------------------------------------------------

@dataclass
class KadReconciliationReport:
    dry_run: bool
    reference_available: bool = False
    reference_rows: Counter = field(default_factory=Counter)
    reference_codes_in_both_versions: int = 0
    current_codes_missing_from_catalogue: int = 0
    activity_codes: int = 0
    origins: Counter = field(default_factory=Counter)
    classifications: Counter = field(default_factory=Counter)
    classifications_by_origin: Counter = field(default_factory=Counter)
    unresolved_reasons: Counter = field(default_factory=Counter)
    codes_with_retired_reference_rows: int = 0
    description_differences: Counter = field(default_factory=Counter)
    links_created: int = 0
    links_updated: int = 0
    links_unchanged: int = 0
    links_removed: int = 0
    observed_in_company_records: Counter = field(default_factory=Counter)
    company_activity_rows_not_in_catalogue: int = 0
    radar_criteria: Counter = field(default_factory=Counter)
    radars_with_criteria_outside_current_taxonomy: int = 0
    batches: int = 0

    def lines(self) -> list[str]:
        p = "[dry-run] " if self.dry_run else ""
        verb = "would be " if self.dry_run else ""
        versions = sorted({version for version, _ in self.reference_rows})

        def counts(counter, keys):
            return " ".join(f"{key}={counter[key]}" for key in keys)

        observed_keys = ("kad_2026_only", "kad_2008_only", "both", "no_version", "never")
        return [
            f"{p}reference GemiKad status={'available' if self.reference_available else 'not_synchronised'} "
            + " ".join(f"{version or 'unpublished'}: present={self.reference_rows[(version, True)]} retired={self.reference_rows[(version, False)]}" for version in versions)
            + f" codes_in_both_versions={self.reference_codes_in_both_versions}",
            f"{p}ActivityCode entries={self.activity_codes} {counts(self.origins, (AADE_CATALOGUE, GEMI_FALLBACK))}",
            f"{p}classification {counts(self.classifications, CLASSIFICATIONS)} "
            f"(unresolved: {counts(self.unresolved_reasons, (NO_REFERENCE_DATA, NOT_IN_REFERENCE, RETIRED_IN_REFERENCE))}) "
            f"codes_with_retired_reference_rows={self.codes_with_retired_reference_rows}",
            *(f"{p}  {origin}: " + " ".join(f"{name}={self.classifications_by_origin[(origin, name)]}" for name in CLASSIFICATIONS)
              for origin in (AADE_CATALOGUE, GEMI_FALLBACK)),
            f"{p}description differences for the same code and version (evidence only) "
            + (" ".join(f"{version}={count}" for version, count in sorted(self.description_differences.items())) or "none"),
            f"{p}current {CURRENT_KAD_VERSION} codes missing from ActivityCode={self.current_codes_missing_from_catalogue} (not added)",
            f"{p}links {verb}created={self.links_created} {verb}updated={self.links_updated} unchanged={self.links_unchanged} "
            f"{verb}removed={self.links_removed}",
            *(f"{p}catalogue codes seen in company records, {origin}: " + " ".join(f"{key}={self.observed_in_company_records[(origin, key)]}" for key in observed_keys)
              for origin in (AADE_CATALOGUE, GEMI_FALLBACK)),
            f"{p}CompanyActivity rows whose code is not in ActivityCode={self.company_activity_rows_not_in_catalogue}",
            f"{p}saved Radar KAD criteria={sum(self.radar_criteria.values())} {counts(self.radar_criteria, CLASSIFICATIONS)} "
            f"radars_with_criteria_outside_{CURRENT_KAD_VERSION}={self.radars_with_criteria_outside_current_taxonomy}",
            f"{p}KAD 2008 -> 2026 crosswalk: unresolved (GEMI publishes none; nothing inferred)",
        ]


def _observed_versions() -> dict[str, set]:
    CompanyActivity = apps.get_model("gemiapp", "CompanyActivity")
    observed: dict[str, set] = defaultdict(set)
    for code, kad_version in CompanyActivity.objects.values_list("code", "kad_version").distinct().iterator(chunk_size=5000):
        observed[code].add(kad_version)
    return observed


def _observed_class(versions: set) -> str:
    if not versions:
        return "never"
    if {CURRENT_KAD_VERSION, HISTORICAL_KAD_VERSION} <= versions:
        return "both"
    if CURRENT_KAD_VERSION in versions:
        return "kad_2026_only"
    if HISTORICAL_KAD_VERSION in versions:
        return "kad_2008_only"
    return "no_version"


def radar_criteria_compatibility(reference: dict[str, list[ReferenceKad]] | None = None) -> list[tuple[int, bool, str, KadCompatibility]]:
    """Every saved KAD criterion of every non-deleted Radar, classified. Read-only."""
    CustomerRadar = apps.get_model("gemiapp", "CustomerRadar")
    reference = load_reference() if reference is None else reference
    available = reference_available(reference)
    result = []
    radars = CustomerRadar.objects.filter(deleted_at__isnull=True).prefetch_related("activity_codes").order_by("pk")
    for radar in radars:
        for activity_code in sorted(radar.activity_codes.all(), key=lambda item: item.normalized_code):
            code = activity_code.normalized_code
            result.append((radar.pk, radar.is_active, code, classify_code(code, reference, available=available)))
    return result


def reconcile_kad_catalogue(*, dry_run: bool = False, batch_size: int = 1000) -> KadReconciliationReport:
    """Link ActivityCode entries to GemiKad rows by code and classify them. Local data only; idempotent."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    ActivityCode = apps.get_model("gemiapp", "ActivityCode")
    ActivityCodeKadLink = apps.get_model("gemiapp", "ActivityCodeKadLink")
    CompanyActivity = apps.get_model("gemiapp", "CompanyActivity")

    report = KadReconciliationReport(dry_run=dry_run)
    reference = load_reference()
    available = report.reference_available = reference_available(reference)
    present_codes: dict[str, set] = defaultdict(set)
    for code, rows in reference.items():
        for row in rows:
            report.reference_rows[(row.kad_version, row.is_present)] += 1
            if row.is_present:
                present_codes[row.kad_version].add(code)
    report.reference_codes_in_both_versions = len(present_codes[CURRENT_KAD_VERSION] & present_codes[HISTORICAL_KAD_VERSION])
    catalogue_codes = set(ActivityCode.objects.values_list("normalized_code", flat=True))
    report.current_codes_missing_from_catalogue = len(present_codes[CURRENT_KAD_VERSION] - catalogue_codes)
    observed = _observed_versions()

    last_pk = 0
    while True:
        batch = list(ActivityCode.objects.filter(pk__gt=last_pk).order_by("pk").values("pk", "normalized_code", "description", "source")[:batch_size])
        if not batch:
            break
        existing: dict[int, dict[int, object]] = defaultdict(dict)
        for link in ActivityCodeKadLink.objects.filter(activity_code_id__in=[item["pk"] for item in batch]):
            existing[link.activity_code_id][link.gemi_kad_id] = link
        creates, updates, removals = [], [], []
        now = timezone.now()
        for item in batch:
            code = item["normalized_code"]
            origin = catalogue_origin(item["source"])
            compatibility = classify_code(code, reference, available=available)
            report.activity_codes += 1
            report.origins[origin] += 1
            report.classifications[compatibility.classification] += 1
            report.classifications_by_origin[(origin, compatibility.classification)] += 1
            if compatibility.unresolved_reason:
                report.unresolved_reasons[compatibility.unresolved_reason] += 1
            report.codes_with_retired_reference_rows += int(bool(compatibility.retired_versions))
            report.observed_in_company_records[(origin, _observed_class(observed.get(code, set())))] += 1

            matches = {row.pk: row for row in reference.get(code, [])}
            links = existing.get(item["pk"], {})
            for kad_pk, row in matches.items():
                same = descriptions_match(item["description"], row.description)
                if not same and row.is_present:
                    report.description_differences[row.kad_version] += 1
                link = links.get(kad_pk)
                if link is None:
                    creates.append(ActivityCodeKadLink(
                        activity_code_id=item["pk"], gemi_kad_id=kad_pk, match_basis=MATCH_BASIS_CODE, description_matches=same,
                    ))
                    report.links_created += 1
                elif link.description_matches != same:
                    link.description_matches, link.updated_at = same, now
                    updates.append(link)
                    report.links_updated += 1
                else:
                    report.links_unchanged += 1
            for kad_pk, link in links.items():
                if kad_pk not in matches:  # the ActivityCode's code was edited since the link was made
                    removals.append(link.pk)
                    report.links_removed += 1
        if not dry_run:
            with transaction.atomic():
                if removals:
                    ActivityCodeKadLink.objects.filter(pk__in=removals).delete()
                if updates:
                    ActivityCodeKadLink.objects.bulk_update(updates, ["description_matches", "updated_at"], batch_size=1000)
                if creates:
                    ActivityCodeKadLink.objects.bulk_create(creates, batch_size=1000)
        report.batches += 1
        last_pk = batch[-1]["pk"]

    report.company_activity_rows_not_in_catalogue = CompanyActivity.objects.exclude(
        code__in=ActivityCode.objects.values("normalized_code"),
    ).count()
    outside = set()
    for radar_id, _, _, compatibility in radar_criteria_compatibility(reference):
        report.radar_criteria[compatibility.classification] += 1
        if not compatibility.in_current_taxonomy:
            outside.add(radar_id)
    report.radars_with_criteria_outside_current_taxonomy = len(outside)
    logger.info(
        "KAD catalogue reconciliation%s: %s entries, links created=%s updated=%s removed=%s.",
        " (dry run)" if dry_run else "", report.activity_codes, report.links_created, report.links_updated, report.links_removed,
    )
    return report
