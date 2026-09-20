"""Customer form for creating and editing an Organization Radar.

Presentation and parsing only. It reads the **platform** GEMI reference data (present KADs, prefectures,
municipalities and legal types, never tenant data) and turns a posted form into a ``RadarDefinition`` checked by the
C3 domain rule ``validate_radar_definition`` -- the same rule ``create_organization_radar`` /
``replace_organization_radar`` apply again when writing. Nothing here writes, authorizes or reads an organization:
the customer views pass the value to ``organization_access``, which decides who may write it.

Chips, not raw controls
-----------------------
``RadarForm.selections`` carries, for every criteria field, the values currently chosen **with the label the
catalogue gives them**, so the form can show "47191002 — ΛΙΑΝΙΚΟ ΕΜΠΟΡΙΟ" instead of a bare code or a primary
key. It is presentation only: the posted representation is unchanged -- a KAD field still posts one
``"<code> <version>"`` line per selection and every other field still posts reference primary keys -- so
``parse_radar_form`` and the stored definition are exactly what they were before the pickers existed.

Only criteria the matcher supports are exposed: name, active, minimum score (0-100), exact KADs (code + version),
prefectures and municipalities, legal forms, the implemented signal types, and exclusions on KAD, prefecture,
municipality and legal form. Every reference is resolved to a present row; an unknown, malformed or retired
reference is a form error, never a guess. Duplicates, contradictions (the same criterion both targeted and excluded),
an impossible score and "active without a positive criterion" are rejected by the domain rule, with Greek messages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.apps import apps

from .company_signals import SIGNAL_TYPE_CHOICES
from .organization_radars import (
    NAME_MAX_LENGTH, SCORE_MAX, SCORE_MIN, RadarDefinition, RadarError, implemented_signal_types,
    validate_radar_definition,
)

SIGNAL_LABELS = dict(SIGNAL_TYPE_CHOICES)
KAD_VERSION_ALIASES = {"2008": "kad_2008", "kad_2008": "kad_2008", "2026": "kad_2026", "kad_2026": "kad_2026"}
LIST_FIELDS = ("prefectures", "municipalities", "legal_forms", "signal_types", "excluded_prefectures",
               "excluded_municipalities", "excluded_legal_forms")
TEXT_FIELDS = ("name", "score_threshold", "kads", "excluded_kads")

# The C3 domain messages, in the customer's language. Matched on stable fragments of RadarError's text.
DOMAIN_MESSAGES = (
    ("needs a name", "Το Radar χρειάζεται όνομα."),
    ("may not exceed", f"Το όνομα του Radar μπορεί να έχει έως {NAME_MAX_LENGTH} χαρακτήρες."),
    ("score_threshold", f"Το ελάχιστο score πρέπει να είναι ακέραιος από {SCORE_MIN} έως {SCORE_MAX}."),
    ("retired", "Ένα κριτήριο αναφέρεται σε στοιχείο που έχει αποσυρθεί από το ΓΕΜΗ."),
    ("both targeted and excluded", "Το ίδιο κριτήριο δεν μπορεί να είναι ταυτόχρονα στόχος και αποκλεισμός."),
    ("duplicate", "Το ίδιο κριτήριο δηλώθηκε δύο φορές."),
    ("unknown signal type", "Άγνωστος τύπος γεγονότος."),
    ("no implemented detector", "Αυτός ο τύπος γεγονότος δεν παρακολουθείται ακόμη."),
    ("active Radar needs", "Ένα ενεργό Radar χρειάζεται τουλάχιστον έναν ΚΑΔ, μία περιοχή, μία νομική μορφή ή έναν "
                           "τύπο γεγονότος."),
)
GENERIC_MESSAGE = "Ο ορισμός του Radar δεν είναι έγκυρος."


def radar_error_message(error) -> str:
    """The Greek, customer-facing message of a C3 ``RadarError``."""
    text = str(error)
    return next((greek for fragment, greek in DOMAIN_MESSAGES if fragment in text), GENERIC_MESSAGE)


def _model(name):
    return apps.get_model("gemiapp", name)


@dataclass(frozen=True)
class RadarFormChoices:
    prefectures: tuple        # (pk, label)
    municipalities: tuple
    legal_forms: tuple
    signal_types: tuple       # (value, label)
    reference_available: bool  # False until the GEMI reference data has been synced


def radar_form_choices() -> RadarFormChoices:
    """What the form may offer: present reference rows only (new criteria never reference retired ones)."""
    prefectures = list(_model("GemiPrefecture").objects.filter(is_present=True).order_by("description", "source_id")
                       .values_list("pk", "source_id", "description"))
    names = {source_id: description for _, source_id, description in prefectures}
    municipalities = tuple(
        (pk, f"{description or source_id}" + (f" ({names[parent]})" if names.get(parent) else ""))
        for pk, source_id, description, parent in _model("GemiMunicipality").objects.filter(is_present=True)
        .order_by("description", "source_id").values_list("pk", "source_id", "description", "source_prefecture_id"))
    legal_forms = tuple((pk, description or source_id) for pk, source_id, description in
                        _model("GemiLegalType").objects.filter(is_present=True).order_by("description", "source_id")
                        .values_list("pk", "source_id", "description"))
    has_kads = _model("GemiKad").objects.filter(is_present=True).exists()
    return RadarFormChoices(
        prefectures=tuple((pk, description or source_id) for pk, source_id, description in prefectures),
        municipalities=municipalities, legal_forms=legal_forms,
        signal_types=tuple((value, SIGNAL_LABELS.get(value, value)) for value in implemented_signal_types()),
        reference_available=bool(prefectures or municipalities or legal_forms or has_kads))


@dataclass
class RadarForm:
    """The form's values (as strings/lists, for re-rendering), its errors and, when valid, the definition.

    ``selections`` mirrors ``values`` for the criteria fields, resolved to catalogue labels for display. It is
    filled on both paths -- a stored definition and a rejected post -- so the chips a customer picked survive a
    validation error instead of silently emptying the form.
    """

    values: dict
    errors: list = field(default_factory=list)
    definition: RadarDefinition | None = None
    selections: dict = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return self.definition is not None and not self.errors


def _kad_line(kad) -> str:
    return f"{kad.source_id} {kad.kad_version}".strip()


def _version_label(version: str) -> str:
    return version.replace("kad_", "ΚΑΔ ") if version else ""


def _kad_selection(text: str) -> tuple:
    """The chips for a KAD text field: every stored line, labelled from the catalogue. One query."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ()
    parsed = [(line, line.split()) for line in lines]
    codes = {parts[0] for _, parts in parsed if parts}
    described = {}
    for source_id, version, description in _model("GemiKad").objects.filter(
            source_id__in=codes, is_present=True).values_list("source_id", "kad_version", "description"):
        described[(source_id, version)] = (description or "").strip()
        described.setdefault(source_id, (description or "").strip())
    chips = []
    for line, parts in parsed:
        code = parts[0] if parts else line
        version = parts[1] if len(parts) > 1 else ""
        description = described.get((code, version), described.get(code, ""))
        chips.append({"value": line, "label": f"{code} — {description}" if description else code,
                      "detail": _version_label(version)})
    return tuple(chips)


def _reference_selection(model_name: str, ids: list, *, parents: dict | None = None) -> tuple:
    """The chips for a reference field: the posted primary keys, labelled, in the posted order. One query."""
    wanted = [value for value in (ids or []) if isinstance(value, str) and value.isascii() and value.isdigit()]
    if not wanted:
        return ()
    rows = {}
    fields = ["pk", "source_id", "description"] + (["source_prefecture_id"] if parents is not None else [])
    for row in _model(model_name).objects.filter(pk__in={int(v) for v in wanted}).values(*fields):
        label = (row["description"] or "").strip() or row["source_id"]
        detail = (parents or {}).get(row.get("source_prefecture_id"), "") if parents is not None else ""
        rows[str(row["pk"])] = {"value": str(row["pk"]), "label": label, "detail": detail}
    return tuple(rows[value] for value in wanted if value in rows)


def _prefecture_names() -> dict:
    return {source_id: (description or "").strip() for source_id, description
            in _model("GemiPrefecture").objects.values_list("source_id", "description")}


def radar_form_selections(values: dict) -> dict:
    """Every criteria field's current selection, labelled for display. Reads reference data only."""
    parents = _prefecture_names()
    selections = {"kads": _kad_selection(values.get("kads", "")),
                  "excluded_kads": _kad_selection(values.get("excluded_kads", ""))}
    for name, model_name in (("prefectures", "GemiPrefecture"), ("municipalities", "GemiMunicipality"),
                             ("legal_forms", "GemiLegalType"), ("excluded_prefectures", "GemiPrefecture"),
                             ("excluded_municipalities", "GemiMunicipality"),
                             ("excluded_legal_forms", "GemiLegalType")):
        selections[name] = _reference_selection(
            model_name, values.get(name) or [],
            parents=parents if model_name == "GemiMunicipality" else None)
    return selections


def initial_radar_form(definition: RadarDefinition | None) -> RadarForm:
    """The form pre-filled with a stored definition (edit) or empty (create; new Radars start inactive)."""
    if definition is None:
        empty = {**{name: "" for name in TEXT_FIELDS}, **{name: [] for name in LIST_FIELDS}, "active": False}
        return RadarForm(values=empty, selections=radar_form_selections(empty))
    Prefecture, Municipality, Kad = _model("GemiPrefecture"), _model("GemiMunicipality"), _model("GemiKad")
    Legal = _model("GemiLegalType")
    excluded = definition.exclusions
    values = {
        "name": definition.name, "active": definition.active,
        "score_threshold": "" if definition.score_threshold is None else str(definition.score_threshold),
        "kads": "\n".join(_kad_line(kad) for kad in definition.kads),
        "prefectures": [str(r.pk) for r in definition.regions if isinstance(r, Prefecture)],
        "municipalities": [str(r.pk) for r in definition.regions if isinstance(r, Municipality)],
        "legal_forms": [str(r.pk) for r in definition.legal_forms],
        "signal_types": list(definition.signal_types),
        "excluded_kads": "\n".join(_kad_line(r) for r in excluded if isinstance(r, Kad)),
        "excluded_prefectures": [str(r.pk) for r in excluded if isinstance(r, Prefecture)],
        "excluded_municipalities": [str(r.pk) for r in excluded if isinstance(r, Municipality)],
        "excluded_legal_forms": [str(r.pk) for r in excluded if isinstance(r, Legal)],
    }
    return RadarForm(values=values, selections=radar_form_selections(values))


def _kads(text: str, errors: list, what: str) -> list:
    """One KAD per line: a code, optionally followed by its version (2008 / 2026). Resolved to present rows."""
    Kad = _model("GemiKad")
    rows = []
    for line in (text or "").splitlines():
        parts = line.replace(",", " ").split()
        if not parts:
            continue
        code = parts[0].replace(".", "")
        version = KAD_VERSION_ALIASES.get(parts[1].lower()) if len(parts) > 1 else None
        if not re.fullmatch(r"\d{1,16}", code) or len(parts) > 2 or (len(parts) == 2 and version is None):
            errors.append(f"{what}: «{line.strip()}» δεν είναι έγκυρος ΚΑΔ (π.χ. 62010000 ή 62010000 2026).")
            continue
        matches = Kad.objects.filter(source_id=code, is_present=True)
        if version:
            matches = matches.filter(kad_version=version)
        found = list(matches.order_by("kad_version")[:3])
        if not found:
            errors.append(f"{what}: ο ΚΑΔ {code} δεν υπάρχει στα τρέχοντα στοιχεία ΓΕΜΗ.")
        elif len(found) > 1:
            versions = ", ".join(kad.kad_version.replace("kad_", "") for kad in found)
            errors.append(f"{what}: ο ΚΑΔ {code} υπάρχει σε περισσότερες εκδόσεις ({versions}) — πρόσθεσε την έκδοση, "
                          f"π.χ. «{code} 2026».")
        else:
            rows.append(found[0])
    return rows


def _references(model_name: str, ids: list, errors: list, what: str) -> list:
    """Posted ids of present reference rows, in the posted order (a repeated id stays repeated, so the domain rule
    rejects the duplicate). Anything else is a form error."""
    if not ids:
        return []
    if not all(isinstance(value, str) and value.isascii() and value.isdigit() for value in ids):
        errors.append(f"{what}: μη έγκυρη επιλογή.")
        return []
    present = {row.pk: row for row in _model(model_name).objects.filter(pk__in={int(v) for v in ids}, is_present=True)}
    if len(present) != len({int(v) for v in ids}):
        errors.append(f"{what}: μία επιλογή δεν υπάρχει ή έχει αποσυρθεί από το ΓΕΜΗ.")
        return []
    return [present[int(value)] for value in ids]


def parse_radar_form(post) -> RadarForm:
    """A posted form -> a validated ``RadarDefinition`` (or the errors). Reads reference data; writes nothing."""
    values = {name: (post.get(name) or "") for name in TEXT_FIELDS}
    values.update({name: post.getlist(name) for name in LIST_FIELDS})
    values["active"] = post.get("active") == "1"
    form = RadarForm(values=values, selections=radar_form_selections(values))
    errors = form.errors

    threshold_text = values["score_threshold"].strip()
    threshold = None
    if threshold_text:
        if threshold_text.isascii() and threshold_text.lstrip("-").isdigit():
            threshold = int(threshold_text)  # the range is the domain rule's
        else:
            errors.append(f"Το ελάχιστο score πρέπει να είναι ακέραιος από {SCORE_MIN} έως {SCORE_MAX}.")
    kads = _kads(values["kads"], errors, "ΚΑΔ")
    regions = (_references("GemiPrefecture", values["prefectures"], errors, "Περιφερειακές ενότητες")
               + _references("GemiMunicipality", values["municipalities"], errors, "Δήμοι"))
    legal_forms = _references("GemiLegalType", values["legal_forms"], errors, "Νομικές μορφές")
    exclusions = (_kads(values["excluded_kads"], errors, "Αποκλεισμός ΚΑΔ")
                  + _references("GemiPrefecture", values["excluded_prefectures"], errors,
                                "Αποκλεισμός περιφερειακών ενοτήτων")
                  + _references("GemiMunicipality", values["excluded_municipalities"], errors, "Αποκλεισμός δήμων")
                  + _references("GemiLegalType", values["excluded_legal_forms"], errors, "Αποκλεισμός νομικών μορφών"))
    if errors:
        return form
    try:
        form.definition = validate_radar_definition(RadarDefinition(
            name=values["name"], active=values["active"], score_threshold=threshold, kads=tuple(kads),
            regions=tuple(regions), legal_forms=tuple(legal_forms), signal_types=tuple(values["signal_types"]),
            exclusions=tuple(exclusions)))
    except RadarError as error:
        errors.append(radar_error_message(error))
    return form
