"""The lean GEMI ingestion cycle: one fetch, the companies it returned, their SHADOW signals.

    mutex -> Discovery v2 INGEST run (companies created from the search page itself)
          -> run, evidence and cursor persisted in one transaction
          -> NEW_COMPANY materialisation, scoped to this run's numbers (always SHADOW)
          -> the after-commit SHADOW opportunity pipeline (observed, never repeated)
          -> counters -> mutex released

This is the unit the schedule runs (first cadence: every 30 minutes, ``apps.SCHEDULES``). **It is dormant**:
that schedule entry is registered only while ``GEMI_DISCOVERY_V2_ENABLED`` is on, the flag is off by default, and
the cycle refuses without it -- dry runs included, because a dry run still spends GEMI requests. The legacy
importer (``services.import_for_date``) remains the canonical production fetch and is not touched from here.
The scheduled task evaluates the operator-alert policy after each real cycle (``gemiapp.ingestion_alerts``).

One fetch path, no second importer
----------------------------------
Everything is the existing machinery, in order: ``run_discovery`` (the shared GemiClient, the DISCOVERY lane,
the shared rate budget, the frontier, the ordering guardrails), ``company_writer`` (the same date-safe
create-only writer hydration uses), ``materialize_new_company_signals`` and the pipeline hook. A page of N new
valid companies creates N companies from that page: no ``GET /companies?arGemi=<n>`` follows. Pending-company
hydration stays what it was -- an unscheduled, default-off recovery tool for evidence this cycle cannot reach.

Create-only
-----------
A ``Company`` row that already exists is never updated here. Same-day refresh of a stored company is still the
legacy importer's job (``update_or_create`` on every intraday run) until that is decided separately.

Mutex
-----
One cycle at a time across every worker, cluster and instance: a key in the shared (database) cache, claimed
with the atomic ``cache.add``. It is **not** bucketed by the hour like ``tasks._claim_pipeline_slot`` -- the
point is "one at a time", and it is released as soon as the cycle ends. The TTL only bounds a worker that died
mid-cycle. A cycle that finds the lock held does nothing and says so (``skipped_locked``): no request, no write.
Only the holder releases it (the stored token is checked), so a cycle that outlived its TTL cannot release a
successor's lock.

Request budget
--------------
Each page request waits at most ``GEMI_INGESTION_MAX_WAIT_SECONDS`` (default 120 s) for a slot instead of the
DISCOVERY lane's 900 s, so a starved run fails fast -- the cursor does not move and the next run repeats the
window -- rather than outliving the interval it is meant to run in. The budget itself, its 7/minute ceiling and
the lane order are untouched.

Scoped materialisation
----------------------
``materialize_new_company_signals`` without a scope reads every eligible observation ever stored. This cycle
passes only (a) the identifiers this run classified as newly discovered and (b) a bounded catch-up: companies
with eligible evidence from the last ``catch_up_hours`` that are stored but have no NEW_COMPANY signal. (b) is
what repairs a cycle that died after its evidence and cursor were committed but before it materialised -- on
the next run those records are below the frontier and ``known``, so (a) alone would never see them again.
Evidence a run recorded before failing is materialised too: B2's rule is that a record GEMI returned and A2
validated is authentic whatever happened to the run afterwards.

SHADOW
------
Signals are created by a producer that has no live option, the pipeline accepts SHADOW only, and every customer
surface reads LIVE-backed opportunities only. Nothing here sends an email or a notification, promotes a signal
or changes a schedule. The G4 preflight still refuses when a LIVE signal exists.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.apps import apps
from django.conf import settings
from django.core.cache import caches
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .company_signals import NEW_COMPANY
from .g4_shadow_cycle import Precheck, preflight
from .ingestion.discovery import INGEST, DiscoveryPolicy, DiscoveryResult, ingest_enabled, run_discovery
from .new_company_signals import ELIGIBLE_CLASSIFICATIONS, MaterialisationReport, materialize_new_company_signals
from .opportunity_pipeline import FAILED, collect_pipeline_runs

logger = logging.getLogger(__name__)

LOCK_KEY = "gemi-ingestion-cycle-lock"
# Bounds a worker that died mid-cycle; equals the django-q task timeout, after which the task is gone anyway.
LOCK_TTL_SECONDS = 1800
DEFAULT_MAX_WAIT_SECONDS = 120.0
DEFAULT_CATCH_UP_HOURS = 24

OK = "ok"
SKIPPED_LOCKED = "skipped_locked"
FAILED_CYCLE = "failed"

DISCOVERY_PHASE = "discovery"
MATERIALISATION_PHASE = "materialisation"
PIPELINE_PHASE = "opportunity_pipeline"

INGEST_FLAG_OFF = ("GEMI_DISCOVERY_V2_ENABLED is off: the ingestion cycle creates Company rows and only runs "
                   "when it is explicitly enabled")


class IngestionCycleRefused(RuntimeError):
    """The precheck refused the cycle. Nothing was written, no lock was taken and no GEMI request was made."""


def max_wait_from_settings() -> float:
    return float(getattr(settings, "GEMI_INGESTION_MAX_WAIT_SECONDS", DEFAULT_MAX_WAIT_SECONDS))


def claim_cycle_lock(ttl_seconds: int = LOCK_TTL_SECONDS):
    """Claim the one-cycle-at-a-time lock. Returns a release callable, or None when another cycle holds it."""
    cache = caches["shared"]
    token = secrets.token_hex(16)
    if not cache.add(LOCK_KEY, token, ttl_seconds):
        return None

    def release():
        try:
            if cache.get(LOCK_KEY) == token:   # never a successor's lock
                cache.delete(LOCK_KEY)
        except Exception:  # the TTL still frees it; a failed release must not mask the cycle's own result
            logger.exception("GEMI ingestion cycle: releasing the lock failed; it expires on its own")

    return release


def unmaterialised_recent_numbers(*, hours: int, now: datetime) -> list[str]:
    """Stored companies with eligible discovery evidence from the last ``hours`` and no NEW_COMPANY signal.

    Bounded by the evidence of that window, not by the tables. Reads only.
    """
    Company = apps.get_model("gemiapp", "Company")
    CompanySignal = apps.get_model("gemiapp", "CompanySignal")
    GemiDiscoveryObservation = apps.get_model("gemiapp", "GemiDiscoveryObservation")
    recent = (GemiDiscoveryObservation.objects
              .filter(classification__in=ELIGIBLE_CLASSIFICATIONS, run__started_at__gte=now - timedelta(hours=hours))
              .values("gemi_number"))
    return list(
        Company.objects.filter(gemi_number__in=recent)
        .exclude(Exists(CompanySignal.objects.filter(company=OuterRef("pk"), signal_type=NEW_COMPANY)))
        .order_by("gemi_number").values_list("gemi_number", flat=True)
    )


@dataclass
class IngestionCycleReport:
    dry_run: bool
    started_at: datetime
    precheck: Precheck
    status: str = OK
    failed_phases: list = field(default_factory=list)   # (phase, detail)
    discovery: DiscoveryResult | None = None
    materialisation: MaterialisationReport | None = None
    pipeline_runs: list = field(default_factory=list)    # observed after commit, never repeated
    discovered_numbers: int = 0      # newly discovered identifiers of this run (scope a)
    catch_up_numbers: int = 0        # stored, evidenced in the window, still without a signal (scope b)
    finished_at: datetime | None = None

    @property
    def gemi_requests(self) -> int:
        """Pages fetched by the discovery run: the only GEMI requests this cycle makes."""
        return self.discovery.pages_fetched if self.discovery is not None else 0

    def summary(self) -> dict:
        discovery, materialised = self.discovery, self.materialisation
        return {
            "status": self.status, "dry_run": self.dry_run, "gemi_requests": self.gemi_requests,
            "failed_phases": [name for name, _ in self.failed_phases],
            "discovery_status": discovery.status if discovery else "",
            "stop_reason": discovery.stop_reason if discovery else "",
            "run_id": discovery.run_id if discovery else None,
            "examined": discovery.records_examined if discovery else 0,
            "newly_discovered": discovery.new_records if discovery else 0,
            "companies_created": discovery.ingested_records if discovery else 0,
            "would_create": discovery.would_ingest_records if discovery else 0,
            "quarantined_date": discovery.quarantined_date_records if discovery else 0,
            "write_failed": discovery.ingest_failed_records if discovery else 0,
            "observations_stored": discovery.observations_stored if discovery else 0,
            "observations_suppressed": discovery.observations_suppressed if discovery else 0,
            "cursor_advanced": discovery.cursor_advanced if discovery else False,
            "materialisation_scope": self.discovered_numbers + self.catch_up_numbers,
            "signals_created": materialised.signals_created if materialised else 0,
            "signals_existing": materialised.signals_existing if materialised else 0,
            "pending_no_company": materialised.unmaterialised_no_company if materialised else 0,
            "pipeline_runs": len(self.pipeline_runs),
            "opportunities_created": sum(run.created for run in self.pipeline_runs),
        }

    def lines(self) -> list[str]:
        prefix = "[dry-run] " if self.dry_run else ""
        now = self.finished_at or timezone.now()
        out = [f"{prefix}GEMI INGESTION CYCLE {self.started_at.isoformat()} -> {now.isoformat()} status={self.status}"]
        if self.status == SKIPPED_LOCKED:
            return out + [f"{prefix}another cycle holds the lock: nothing requested, nothing written."]
        out += ["PRECHECK", *self.precheck.lines(), "DISCOVERY (ingest: companies are created from the fetched page)"]
        if self.discovery is None:
            out.append("  not run")
        else:
            out += [f"  {line}" for line in self.discovery.lines()]
            out.append(f"  GEMI requests={self.gemi_requests} (search pages only; zero per-company lookups)")
        out.append("NEW_COMPANY MATERIALISATION (scoped to this run)")
        out.append(f"  scope: newly discovered={self.discovered_numbers} catch-up (stored, no signal yet)="
                   f"{self.catch_up_numbers}")
        if self.materialisation is None:
            out.append("  not run" if self.discovered_numbers + self.catch_up_numbers else "  nothing in scope")
        else:
            out += [f"  {line}" for line in self.materialisation.lines()]
        broken = [run for run in self.pipeline_runs if run.skipped_reason == FAILED or run.errors]
        out.append(f"OPPORTUNITY PIPELINE (observed, after commit): runs={len(self.pipeline_runs)} "
                   f"opportunities created={sum(run.created for run in self.pipeline_runs)} failed={len(broken)}")
        for name, detail in self.failed_phases:
            out.append(f"FAILED {name}{f' -- {detail}' if detail else ''}")
        out.append("SHADOW only: no signal mode changed, no email or notification, nothing visible to customers. "
                   "Existing companies are never updated.")
        if self.dry_run:
            out.append("[dry-run] nothing was written; the search pages WERE requested from GEMI.")
        return out


def cycle_preflight() -> Precheck:
    """The G4 preflight (SHADOW signals and pipeline, no LIVE signal, tables, cursor, collector) plus the one
    thing this cycle needs on top: Company ingest explicitly enabled."""
    check = preflight()
    enabled = ingest_enabled()
    check.checks.append(("company ingest is explicitly enabled", enabled, "" if enabled else INGEST_FLAG_OFF))
    return check


def run_ingestion_cycle(*, dry_run: bool = False, max_pages: int | None = None, max_wait: float | None = None,
                        policy: DiscoveryPolicy | None = None, client=None,
                        catch_up_hours: int = DEFAULT_CATCH_UP_HOURS) -> IngestionCycleReport:
    """One lean ingestion cycle. See the module docstring.

    Raises ``IngestionCycleRefused`` before any lock, request or write when the precheck refuses. A phase that
    fails afterwards is recorded in the report (``status == "failed"``), never hidden and never undone.
    """
    if catch_up_hours < 0:
        raise ValueError("catch_up_hours must not be negative")
    started_at = timezone.now()
    precheck = cycle_preflight()
    report = IngestionCycleReport(dry_run=dry_run, started_at=started_at, precheck=precheck)
    if precheck.refusals:
        raise IngestionCycleRefused("; ".join(f"{name}: {detail}" for name, detail in precheck.refusals))

    release = claim_cycle_lock()
    if release is None:
        report.status, report.finished_at = SKIPPED_LOCKED, timezone.now()
        logger.info("GEMI ingestion cycle: another cycle holds the lock; skipped.")
        return report
    try:
        _run_locked(report, dry_run=dry_run, max_pages=max_pages, policy=policy, client=client,
                    max_wait=max_wait_from_settings() if max_wait is None else max_wait,
                    catch_up_hours=catch_up_hours)
    finally:
        release()
    report.status = FAILED_CYCLE if report.failed_phases else OK
    report.finished_at = timezone.now()
    summary = report.summary()
    logger.info(
        "GEMI ingestion cycle%s: status=%s gemi_requests=%s created=%s quarantined=%s signals=%s "
        "pipeline_runs=%s failed_phases=%s",
        " (dry run)" if dry_run else "", report.status, report.gemi_requests, summary["companies_created"],
        summary["quarantined_date"], summary["signals_created"], len(report.pipeline_runs),
        summary["failed_phases"] or "none",
    )
    return report


def _run_locked(report: IngestionCycleReport, *, dry_run, max_pages, policy, client, max_wait, catch_up_hours) -> None:
    # The only phase that talks to GEMI: search pages, in the DISCOVERY lane, under the shared budget.
    try:
        report.discovery = run_discovery(mode=INGEST, dry_run=dry_run, max_pages=max_pages, policy=policy,
                                         client=client, compact_observations=True, max_wait=max_wait)
    except Exception as error:
        report.failed_phases.append((DISCOVERY_PHASE, f"{type(error).__name__}: {error}"[:200]))
        logger.exception("GEMI ingestion cycle: discovery raised")
    else:
        if report.discovery.status in ("failed", "anomaly"):
            report.failed_phases.append((
                DISCOVERY_PHASE, f"status={report.discovery.status} stop_reason={report.discovery.stop_reason}"))

    discovered = set(report.discovery.discovered_gemi_numbers) if report.discovery is not None else set()
    try:
        catch_up = set(unmaterialised_recent_numbers(hours=catch_up_hours, now=timezone.now())) - discovered \
            if catch_up_hours else set()
        report.discovered_numbers, report.catch_up_numbers = len(discovered), len(catch_up)
        scope = sorted(discovered | catch_up)
        if not scope:
            return
        with collect_pipeline_runs() as observed:
            report.materialisation = materialize_new_company_signals(dry_run=dry_run, gemi_numbers=scope)
        report.pipeline_runs = list(observed)
    except Exception as error:
        report.failed_phases.append((MATERIALISATION_PHASE, f"{type(error).__name__}: {error}"[:200]))
        logger.exception("GEMI ingestion cycle: materialisation raised")
        return
    broken = [run for run in report.pipeline_runs if run.skipped_reason == FAILED or run.errors]
    if broken:
        report.failed_phases.append((PIPELINE_PHASE, f"{len(broken)} of {len(report.pipeline_runs)} run(s) failed"))
