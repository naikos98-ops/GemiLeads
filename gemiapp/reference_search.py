"""Searchable lookup over the canonical GEMI reference tables, for the Radar criteria pickers.

Why this exists
---------------
The Organization Radar form used to ask the customer to *type* KAD codes and to pick regions out of a
``<select multiple>``. There are about 19,000 KAD rows, so the catalogue can never be rendered into the page;
and typing "62010000" from memory is not a product. This module answers "which reference rows match what the
customer is typing", bounded and cheap, so the form can offer a searchable multi-select instead.

Platform data only
------------------
The four tables here -- KAD, prefecture, municipality, legal type -- are canonical GEMI metadata, identical for
every customer. Nothing in this module reads, writes or is scoped to an organization: authorization belongs to
the view (a signed-in user) and to ``organization_access`` (who may write a Radar). Only rows still present in
the latest sync are offered, so a new criterion can never reference a retired one.

Matching
--------
Greek text, so neither SQLite's ASCII-only ``UPPER`` nor ``LIKE`` can be trusted: both would make ΕΣΤΙΑΣΗ,
Εστίαση and εστιαση three different things, and the tests run on SQLite while production runs PostgreSQL. The
comparison is therefore done in Python over a normalised haystack built with the same ``normalize_kad_search``
the legacy picker already uses -- uppercased, accents stripped -- so case and accents are irrelevant and both
databases behave identically. A query matches when **every** whitespace-separated token appears in the
haystack of ``source_id + description``; a purely numeric query is additionally treated as a code prefix, which
is what someone typing "4719" means.

Cost
----
The normalised haystack is built once and cached, keyed on a stamp of the table (row count and the newest
``updated_at``), so a reference sync invalidates it by itself and tests never see another test's rows. Building
it reads only four small columns; searching is a scan over tuples in memory, which for 19,000 KAD rows is
immaterial and, unlike a SQL ``LIKE``, is accent-insensitive.

Results are hard-limited (``MAX_LIMIT``), so no query can return the catalogue.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from django.apps import apps
from django.core.cache import caches
from django.db.models import Count, Max

from .kad import normalize_kad_search

MIN_QUERY_LENGTH = 2
DEFAULT_LIMIT = 40
MAX_LIMIT = 50
CACHE_ALIAS = "default"
CACHE_SECONDS = 900

KAD = "kad"
PREFECTURE = "prefecture"
MUNICIPALITY = "municipality"
LEGAL_FORM = "legal_form"
KINDS = (KAD, PREFECTURE, MUNICIPALITY, LEGAL_FORM)

_MODELS = {KAD: "GemiKad", PREFECTURE: "GemiPrefecture", MUNICIPALITY: "GemiMunicipality",
           LEGAL_FORM: "GemiLegalType"}


@dataclass(frozen=True)
class ReferenceHit:
    """One offered row. ``value`` is exactly what the form posts for it, so the picker never invents a
    representation: a KAD posts ``"<code> <version>"`` (the line format ``parse_radar_form`` reads) and every
    other kind posts its primary key, the value the ``<select multiple>`` posted before."""

    value: str
    label: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"value": self.value, "label": self.label, "detail": self.detail}


def _model(name):
    return apps.get_model("gemiapp", name)


def _stamp(model) -> str:
    """Cheap fingerprint of the table's present rows: a sync changes it, so the cache needs no invalidation.

    Hashed, because the raw value carries a timestamp and a cache key may not contain whitespace."""
    row = model.objects.filter(is_present=True).aggregate(rows=Count("pk"), newest=Max("pk"),
                                                          changed=Max("updated_at"))
    return hashlib.md5(f"{row['rows']}:{row['newest']}:{row['changed']}".encode()).hexdigest()[:16]


def _kad_entries() -> tuple:
    rows = _model("GemiKad").objects.filter(is_present=True).order_by("source_id", "kad_version").values_list(
        "source_id", "kad_version", "description")
    entries = []
    for source_id, version, description in rows:
        text = (description or "").strip()
        label = f"{source_id} — {text}" if text else source_id
        detail = version.replace("kad_", "ΚΑΔ ") if version else ""
        value = f"{source_id} {version}".strip() if version else source_id
        entries.append((value, label, detail, normalize_kad_search(f"{source_id} {text}"), source_id))
    return tuple(entries)


def _simple_entries(model_name: str) -> tuple:
    rows = _model(model_name).objects.filter(is_present=True).order_by("description", "source_id").values_list(
        "pk", "source_id", "description")
    return tuple((str(pk), (description or "").strip() or source_id, "",
                  normalize_kad_search(f"{source_id} {description or ''}"), source_id)
                 for pk, source_id, description in rows)


def _municipality_entries() -> tuple:
    prefectures = dict(_model("GemiPrefecture").objects.filter(is_present=True).values_list("source_id",
                                                                                            "description"))
    rows = _model("GemiMunicipality").objects.filter(is_present=True).order_by("description", "source_id").values_list(
        "pk", "source_id", "description", "source_prefecture_id")
    entries = []
    for pk, source_id, description, parent in rows:
        label = (description or "").strip() or source_id
        parent_name = (prefectures.get(parent) or "").strip()
        # The prefecture is searchable too: "ΚΗΦΙΣΙΑΣ ΑΤΤΙΚΗΣ" should find the municipality.
        entries.append((str(pk), label, parent_name, normalize_kad_search(f"{source_id} {description or ''} "
                                                                         f"{parent_name}"), source_id))
    return tuple(entries)


_BUILDERS = {KAD: _kad_entries, PREFECTURE: lambda: _simple_entries("GemiPrefecture"),
             MUNICIPALITY: _municipality_entries, LEGAL_FORM: lambda: _simple_entries("GemiLegalType")}


def reference_entries(kind: str) -> tuple:
    """(value, label, detail, haystack, source_id) for every present row of ``kind``. Cached per table stamp."""
    if kind not in KINDS:
        raise ValueError(f"unknown reference kind: {kind}")
    cache = caches[CACHE_ALIAS]
    key = f"gemi-reference-index:{kind}:{_stamp(_model(_MODELS[kind]))}"
    entries = cache.get(key)
    if entries is None:
        entries = _BUILDERS[kind]()
        cache.set(key, entries, CACHE_SECONDS)
    return entries


def search_reference(kind: str, query: str, limit: int = DEFAULT_LIMIT) -> list[ReferenceHit]:
    """The rows matching ``query``, case- and accent-insensitively, at most ``limit`` of them.

    A short query returns nothing rather than the head of the catalogue: the picker asks the customer to type.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    text = (query or "").strip()
    if len(text) < MIN_QUERY_LENGTH:
        return []
    tokens = normalize_kad_search(text).split()
    if not tokens:
        return []
    digits = text.replace(".", "").replace(" ", "")
    code_prefix = digits if digits.isascii() and digits.isdigit() else ""

    prefix_hits, contains_hits = [], []
    for value, label, detail, haystack, source_id in reference_entries(kind):
        if code_prefix and source_id.startswith(code_prefix):
            prefix_hits.append(ReferenceHit(value, label, detail))
        elif all(token in haystack for token in tokens):
            contains_hits.append(ReferenceHit(value, label, detail))
        if len(prefix_hits) + len(contains_hits) >= limit:
            break
    return (prefix_hits + contains_hits)[:limit]
