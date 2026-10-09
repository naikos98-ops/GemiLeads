"""The shared, date-safe, create-only writer for a canonical ``Company`` from one GEMI search record.

One writer for every path that creates a company from a payload it already holds: Discovery's ingest (the
record came in the ``/companies`` page the scan just fetched) and pending-company hydration (the record came
from one ``GET /companies?arGemi=<n>``). It never fetches anything: the caller hands over the full record.

Create-only
-----------
Inside one savepoint: if a ``Company`` with that GEMI number exists, nothing is written (``EXISTS``); otherwise
``Company.objects.create(gemi_number=..., **company_defaults(item))`` and ``sync_company_activities`` -- the
legacy importer's own two primitives, so normalisation, legacy-visible activity rows and the ActivityCode
catalogue are identical. ``gemi_number`` is unique, so a writer that wins the race between the check and the
insert makes this insert fail with ``IntegrityError``; the savepoint rolls back and the result is ``EXISTS``,
with the other writer's row untouched. Company and activities commit together or not at all. Never
``update_or_create``: an existing row is never rewritten here.

Date safety
-----------
``company_defaults`` (unchanged, the legacy importer depends on it) stores a missing, unreadable, pre-1900 or
future source ``incorporationDate`` as *today*. Such a row would enter today's legacy digest and matching as if
incorporated today, so this writer never lets that value through. **The writer, not ``company_defaults``, is the
authority for the stored incorporation date:**

1. the source date is parsed and judged here -- missing, unreadable, before 1900 or later than the
   application's local business date (``timezone.localdate()``, Europe/Athens) is refused before anything is
   written (``REFUSED_DATE``);
2. the row is stored with **exactly that validated source date**. Whatever ``company_defaults`` computed for
   ``incorporation_date`` is replaced by it, so the legacy clamp can neither slip through nor reject a record.

Why the override and not a comparison: ``company_defaults`` clamps against ``date.today()``, the container's
clock, which is UTC in production. Between 00:00 and about 03:00 Athens the UTC date is still yesterday, so a
company incorporated *today in Athens* looks "future" to it and comes back clamped. Comparing with that value
would quarantine a perfectly valid record for hours; the business date is the Athens one, so it is accepted
and stored as it is.

Identity
--------
Only a record whose own ``arGemi`` normalises to the requested number is written -- never company Y under
company X. A mismatch is a caller bug and raises ``ValueError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from django.apps import apps
from django.db import IntegrityError, transaction
from django.utils import timezone

CREATED = "created"
WOULD_CREATE = "would_create"
EXISTS = "exists"                  # already stored (found before, or it won the insert race): never rewritten
REFUSED_DATE = "refused_date"      # the stored date would not be the source date: nothing written

DATE_MISSING = "missing"
DATE_UNREADABLE = "unreadable"
DATE_BEFORE_1900 = "before_1900"
DATE_FUTURE = "future"
DATE_CLAMPED = "clamped"           # kept for callers' vocabulary; the writer overrides the clamp instead


@dataclass(frozen=True)
class WriteOutcome:
    status: str
    activities_created: int = 0
    stored_as_today: bool = False   # created (or would be) with a genuine source date of today
    date_problem: str = ""          # for REFUSED_DATE: which rule refused it


def _gemi_number_of(item: Any) -> str:
    text = str(item.get("arGemi") if isinstance(item, dict) and item.get("arGemi") is not None else "").strip()
    return text if text.isdigit() and int(text) > 0 else ""


def validated_source_date(item: dict) -> tuple[date | None, str]:
    """``(the record's own incorporation date, "")`` when it may be stored as it is, else ``(None, problem)``.

    "Future" is judged against the application's local business date, never the container's ``date.today()``.
    """
    raw = str(item.get("incorporationDate") or "")[:10]
    if not raw:
        return None, DATE_MISSING
    try:
        value = date.fromisoformat(raw)
    except ValueError:
        return None, DATE_UNREADABLE
    if value.year < 1900:
        return None, DATE_BEFORE_1900
    if value > timezone.localdate():
        return None, DATE_FUTURE
    return value, ""


def source_date_problem(item: dict) -> str:
    """Why the record's own ``incorporationDate`` may not be stored as a legacy-visible date, or ""."""
    return validated_source_date(item)[1]


def create_company_from_search_item(gemi_number: str, item: dict, *, dry_run: bool = False) -> WriteOutcome:
    """Create the canonical Company (and its activities) for one already-received search record, or refuse.

    See the module docstring. ``dry_run`` judges the record and reports what would happen without writing.
    Unexpected database errors propagate after the savepoint has rolled back: no partial row remains.
    """
    from ..services import company_defaults, sync_company_activities

    if not isinstance(item, dict) or _gemi_number_of(item) != str(gemi_number):
        raise ValueError("the search record is not the requested GEMI number")
    source_date, problem = validated_source_date(item)
    if problem:
        return WriteOutcome(REFUSED_DATE, date_problem=problem)
    defaults = company_defaults(item)
    # Authoritative: the validated source date, whatever the legacy clamp made of it (see "Date safety").
    defaults["incorporation_date"] = source_date
    as_today = source_date == timezone.localdate()

    Company = apps.get_model("gemiapp", "Company")
    if dry_run:
        exists = Company.objects.filter(gemi_number=gemi_number).exists()
        return WriteOutcome(EXISTS if exists else WOULD_CREATE, stored_as_today=as_today and not exists)
    try:
        with transaction.atomic():
            if Company.objects.filter(gemi_number=gemi_number).exists():
                return WriteOutcome(EXISTS)
            company = Company.objects.create(gemi_number=gemi_number, **defaults)
            counts = sync_company_activities(company, item.get("activities"))
    except IntegrityError:
        if Company.objects.filter(gemi_number=gemi_number).exists():
            return WriteOutcome(EXISTS)      # another writer won the insert; its row is left exactly as it is
        raise
    return WriteOutcome(CREATED, activities_created=int(getattr(counts, "created", 0) or 0), stored_as_today=as_today)
