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
incorporated today, so this writer refuses it before anything is written (``REFUSED_DATE``):

1. the source date is judged directly -- missing, unreadable, before 1900 or later than today is refused,
   whatever ``company_defaults`` would do with it;
2. and the date ``company_defaults`` returns must equal the source date exactly (the clamp can never slip
   through even if its rule changes).

A genuine source date of today is not clamped and is written. The clock is the one ``company_defaults`` uses
(``date.today()``), and a date later than the configured local date is refused as well.

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
DATE_CLAMPED = "clamped"           # company_defaults would store something other than the source date


@dataclass(frozen=True)
class WriteOutcome:
    status: str
    activities_created: int = 0
    stored_as_today: bool = False   # created (or would be) with a genuine source date of today
    date_problem: str = ""          # for REFUSED_DATE: which rule refused it


def _gemi_number_of(item: Any) -> str:
    text = str(item.get("arGemi") if isinstance(item, dict) and item.get("arGemi") is not None else "").strip()
    return text if text.isdigit() and int(text) > 0 else ""


def source_date_problem(item: dict) -> str:
    """Why the record's own ``incorporationDate`` may not be stored as a legacy-visible date, or ""."""
    raw = str(item.get("incorporationDate") or "")[:10]
    if not raw:
        return DATE_MISSING
    try:
        value = date.fromisoformat(raw)
    except ValueError:
        return DATE_UNREADABLE
    if value.year < 1900:
        return DATE_BEFORE_1900
    if value > date.today() or value > timezone.localdate():
        return DATE_FUTURE
    return ""


def create_company_from_search_item(gemi_number: str, item: dict, *, dry_run: bool = False) -> WriteOutcome:
    """Create the canonical Company (and its activities) for one already-received search record, or refuse.

    See the module docstring. ``dry_run`` judges the record and reports what would happen without writing.
    Unexpected database errors propagate after the savepoint has rolled back: no partial row remains.
    """
    from ..services import company_defaults, sync_company_activities

    if not isinstance(item, dict) or _gemi_number_of(item) != str(gemi_number):
        raise ValueError("the search record is not the requested GEMI number")
    problem = source_date_problem(item)
    if problem:
        return WriteOutcome(REFUSED_DATE, date_problem=problem)
    defaults = company_defaults(item)
    stored = defaults["incorporation_date"]
    if stored != date.fromisoformat(str(item.get("incorporationDate"))[:10]):
        return WriteOutcome(REFUSED_DATE, date_problem=DATE_CLAMPED)
    as_today = stored == date.today()

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
