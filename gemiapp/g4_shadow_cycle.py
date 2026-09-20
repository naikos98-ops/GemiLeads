"""The deterministic SHADOW cycle for the G4 observation period.

One operator-run sequence, in one fixed order, producing one auditable summary:

    A. precheck   -- refuse before any write or GEMI request if the configuration is not shadow-safe
    B. discovery  -- one Discovery v2 SHADOW run (A10)
    C. materialise -- NEW_COMPANY signals from eligible discovery evidence (B2, with ce0cd4f's baseline)
    D. pipeline   -- the SHADOW opportunity pipeline the on-commit hook already runs for each new signal (G2)
    E. replay     -- optional, bounded reconciliation of SHADOW work a previous cycle failed to finish
    F. report     -- discovery, materialisation, pipeline and G4 coverage metrics, and the legacy comparison

Phase D is observed, never repeated
-----------------------------------
``record_company_signal`` registers the pipeline with ``transaction.on_commit``, so every signal phase C creates
is processed **once**, synchronously, as part of phase C -- and the hook discards the ``PipelineRun`` it gets
back. This module therefore *observes* those runs through ``collect_pipeline_runs()`` instead of processing the
same signals again. Phase D is a reporting phase, not a second pass.

Phase E is off unless asked for
-------------------------------
On a healthy cycle a replay has nothing to do: every new signal was already processed in phase D, and C8 leaves a
signal already linked to an opportunity ``unchanged``. It exists to recover work a *previous* cycle lost -- a
pipeline failure, a Radar created later, an entitlement regained -- so it runs only with ``replay_hours``, is
bounded by a window and a limit, and skips the signals this cycle just processed.

Safety
------
Shadow only, and the precheck fails closed: Discovery ingest off, the discovery shadow flag on, the pipeline's
modes still SHADOW-only, no LIVE signal in the database, the 2.0 tables present, the discovery cursor
initialised, and the GEMI collector configured. Nothing here enables LIVE, changes a signal's mode, registers a
schedule, ingests a company or writes a customer-visible row. No credential, host or environment name is
hard-coded; what the precheck reports it reads from the running configuration.

GEMI requests
-------------
Only phase B talks to GEMI, one request per page, in the DISCOVERY lane and under the shared rate budget.
Materialisation, the detection-time baseline, matching, scoring, persistence and replay add **zero** requests:
they read what is already stored. Nothing here refreshes companies or syncs reference data.

Late publications
-----------------
Still not resolved here. A discovered company the legacy importer never stored has no ``Company`` row, so B2
leaves it ``pending_no_company`` and this cycle counts it -- by classification, with the age of the oldest one --
so it becomes G4 evidence instead of silence.

Idempotency
-----------
Rerunning the whole cycle relies on the existing guarantees, and adds none of its own: a discovery rerun
re-observes the same records (one observation per run, the frontier unmoved unless the run succeeded), B1's
dedupe key keeps one signal per company, ce0cd4f reuses an existing baseline instead of writing a second one,
and C8 keys an opportunity by (organization, Radar, company) and leaves an already linked signal untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.apps import apps
from django.conf import settings
from django.db import connection
from django.utils import timezone

from .company_signals import LIVE, NEW_COMPANY, SHADOW
from .ingestion.discovery import (
    INVALID_DATE,
    KNOWN,
    LATE_PUBLICATION,
    NEW_INCORPORATION,
    SHADOW as DISCOVERY_SHADOW,
    ComparisonReport,
    DiscoveryResult,
    compare_with_legacy,
    get_cursor,
    ingest_enabled,
    run_discovery,
    shadow_only,
)
from .new_company_signals import MaterialisationReport, eligible_observations, materialize_new_company_signals
from .opportunity_pipeline import FAILED, PIPELINE_MODES, collect_pipeline_runs, process_company_signal

logger = logging.getLogger(__name__)

PRECHECK = "precheck"
DISCOVERY = "discovery"
MATERIALISATION = "materialisation"
PIPELINE = "opportunity_pipeline"
REPLAY = "replay"
COMPARISON = "comparison"
# The order is the contract: evidence, then signals, then the opportunities those signals produce.
PHASES = (PRECHECK, DISCOVERY, MATERIALISATION, PIPELINE, REPLAY, COMPARISON)

OK = "ok"
FAILED_PHASE = "failed"
SKIPPED = "skipped"

DEFAULT_REPLAY_LIMIT = 100
MAX_REPLAY_LIMIT = 1000
CHUNK = 500

PIPELINE_COUNTERS = ("considered", "entitled_considered", "skipped_not_entitled", "matched", "insufficient_state",
                     "no_match", "created", "updated", "unchanged", "below_threshold", "skipped_live_backed")
# The 2.0 tables this cycle reads or writes; absent means the migrations are not applied here.
REQUIRED_MODELS = ("GemiDiscoveryCursor", "GemiDiscoveryRun", "GemiDiscoveryObservation", "CompanySignal",
                   "CompanySignalDiscoveryEvidence", "CompanySnapshot", "OrganizationRadar", "Opportunity",
                   "OpportunitySignal")


class ShadowCycleRefused(RuntimeError):
    """The precheck refused the cycle. Nothing was written and no GEMI request was made."""


def _model(name):
    return apps.get_model("gemiapp", name)


def _chunks(values, size: int = CHUNK):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


@dataclass
class PhaseOutcome:
    name: str
    status: str
    detail: str = ""

    def line(self) -> str:
        return f"  {self.name:<20} {self.status}{f' -- {self.detail}' if self.detail else ''}"


@dataclass
class Precheck:
    environment: str = ""
    database_vendor: str = ""
    checks: list = field(default_factory=list)    # (name, ok, detail)

    @property
    def refusals(self) -> list:
        return [(name, detail) for name, ok, detail in self.checks if not ok]

    def lines(self) -> list[str]:
        return [
            f"  environment={self.environment or 'unknown'} database={self.database_vendor or 'unknown'}",
            *(f"  {'OK ' if ok else 'REFUSED'} {name}{f' -- {detail}' if detail else ''}"
              for name, ok, detail in self.checks),
        ]


@dataclass
class CoverageReport:
    """G4 coverage, measured from stored evidence only."""

    eligible_companies: int = 0
    eligible_with_company: int = 0
    signals_materialised: int = 0
    pending_no_company: int = 0
    pending_by_classification: dict = field(default_factory=dict)
    oldest_pending_at: datetime | None = None
    rediscovered_local_observations: int = 0

    @property
    def coverage_ratio(self) -> float | None:
        """Materialised signals per eligible discovery whose company is available. None when nothing is eligible."""
        if not self.eligible_with_company:
            return None
        return self.signals_materialised / self.eligible_with_company

    def oldest_pending_age(self, now: datetime) -> timedelta | None:
        return None if self.oldest_pending_at is None else now - self.oldest_pending_at

    def lines(self, *, now: datetime) -> list[str]:
        ratio = self.coverage_ratio
        age = self.oldest_pending_age(now)
        pending = " ".join(f"{name}={self.pending_by_classification.get(name, 0)}"
                           for name in (LATE_PUBLICATION, INVALID_DATE, NEW_INCORPORATION))
        return [
            f"  eligible discoveries={self.eligible_companies} with a Company row={self.eligible_with_company} "
            f"signals materialised={self.signals_materialised} coverage="
            f"{'n/a' if ratio is None else f'{ratio:.1%}'}",
            f"  race-loss evidence (newly discovered although already stored locally)="
            f"{self.rediscovered_local_observations}",
            f"  pending (no Company row)={self.pending_no_company} by classification: {pending}",
            f"  oldest pending evidence: {'none' if age is None else f'{age.days}d {age.seconds // 3600}h'}"
            f"{'' if self.oldest_pending_at is None else f' ({self.oldest_pending_at.isoformat()})'}",
        ]


@dataclass
class CycleReport:
    dry_run: bool
    started_at: datetime
    precheck: Precheck
    phases: list = field(default_factory=list)
    discovery: DiscoveryResult | None = None
    materialisation: MaterialisationReport | None = None
    pipeline_runs: list = field(default_factory=list)     # phase D: observed, not repeated
    replay_runs: list = field(default_factory=list)       # phase E
    coverage: CoverageReport = field(default_factory=CoverageReport)
    comparison: ComparisonReport | None = None
    finished_at: datetime | None = None

    @property
    def failed_phases(self) -> list[str]:
        return [phase.name for phase in self.phases if phase.status == FAILED_PHASE]

    @property
    def gemi_requests(self) -> int:
        """Pages fetched by phase B. The only GEMI requests this cycle makes."""
        return self.discovery.pages_fetched if self.discovery is not None else 0

    def _totals(self, runs) -> dict:
        totals = dict.fromkeys(PIPELINE_COUNTERS, 0)
        for run in runs:
            for name in PIPELINE_COUNTERS:
                totals[name] += getattr(run, name)
        return totals

    def _pipeline_lines(self, title: str, runs) -> list[str]:
        totals = self._totals(runs)
        failed = [run for run in runs if run.skipped_reason == FAILED]
        errors = sum(len(run.errors) for run in runs)
        return [
            title,
            f"  signals processed={sum(1 for run in runs if run.processed)} of {len(runs)} "
            f"failed={len(failed)} errors={errors} "
            f"skipped (not shadow)={sum(1 for run in runs if run.skipped_reason and run.skipped_reason != FAILED)}",
            f"  radars considered={totals['considered']} entitled={totals['entitled_considered']} "
            f"skipped_not_entitled={totals['skipped_not_entitled']} matched={totals['matched']} "
            f"insufficient_state={totals['insufficient_state']} no_match={totals['no_match']}",
            f"  opportunities created={totals['created']} updated={totals['updated']} "
            f"unchanged={totals['unchanged']} below_threshold={totals['below_threshold']} "
            f"skipped_live_backed={totals['skipped_live_backed']}",
        ]

    def lines(self) -> list[str]:
        now = self.finished_at or timezone.now()
        prefix = "[dry-run] " if self.dry_run else ""
        out = [f"{prefix}G4 SHADOW CYCLE {self.started_at.isoformat()} -> {now.isoformat()}", "PHASES"]
        out += [phase.line() for phase in self.phases]
        out += ["PRECHECK", *self.precheck.lines()]

        out.append("DISCOVERY")
        if self.discovery is None:
            out.append("  not run")
        else:
            result = self.discovery
            out += [
                f"  status={result.status} stop_reason={result.stop_reason} pages/GEMI requests="
                f"{result.pages_fetched}",
                f"  examined={result.records_examined} known={result.known_records} newly discovered="
                f"{result.new_records} of which already stored locally={result.rediscovered_local_records}",
                f"  late_publication={result.late_publication_records} invalid_date={result.invalid_date_records} "
                f"duplicates={result.duplicate_records} invalid_identifiers={result.invalid_identifier_records}",
                f"  anomalies={len(result.anomalies)} blocking={len(result.blocking_anomalies)} "
                f"cursor {result.previous_high_water_mark or 'none'} -> "
                f"{result.resulting_high_water_mark or 'none'} advanced={result.cursor_advanced}",
                *([f"  error={result.error_message}"] if result.error_message else []),
            ]

        out.append("NEW_COMPANY MATERIALISATION")
        if self.materialisation is None:
            out.append("  not run")
        else:
            report = self.materialisation
            out += [
                f"  candidates={report.candidate_companies} signals created={report.signals_created} "
                f"existing={report.signals_existing} pending_no_company={report.unmaterialised_no_company}",
                f"  baselines created={report.baselines_created} reused={report.baselines_reused} "
                f"unavailable={report.state_unavailable}",
                f"  conflicts={report.conflicts} invalid evidence={report.invalid_evidence} mode=shadow (always)",
                *([f"  conflicting gemi numbers: {report.conflicting_gemi_numbers}"]
                  if report.conflicting_gemi_numbers else []),
            ]

        out += self._pipeline_lines("OPPORTUNITY PIPELINE (observed, after commit)", self.pipeline_runs)
        if self.replay_runs:
            out += self._pipeline_lines("REPLAY (bounded reconciliation)", self.replay_runs)
        out += ["G4 COVERAGE", *self.coverage.lines(now=now)]

        if self.comparison is not None:
            out += ["LEGACY COMPARISON", *(f"  {line}" for line in self.comparison.lines())]
        out.append("SHADOW only: no signal mode changed, nothing ingested, nothing visible to customers.")
        if self.dry_run:
            out.append("[dry-run] nothing was written. Discovery's findings were not stored, so materialisation "
                       "could only see evidence recorded by earlier runs.")
        return out


# --- phase A -----------------------------------------------------------------------------------------------

def preflight() -> Precheck:
    """Every reason to refuse, checked before any write and before any GEMI request. Reads configuration and
    schema only: no credential, host or environment name is hard-coded here."""
    check = Precheck(
        environment=str(getattr(settings, "GEMI_LEADS_ENVIRONMENT", "")),
        database_vendor=connection.vendor,
    )
    check.checks.append(("discovery ingest is off", not ingest_enabled(),
                         "" if not ingest_enabled() else "GEMI_DISCOVERY_V2_ENABLED is on: this cycle is shadow only"))
    check.checks.append(("discovery shadow flag is on", shadow_only(),
                         "" if shadow_only() else "GEMI_DISCOVERY_V2_SHADOW is off"))
    shadow_pipeline = tuple(PIPELINE_MODES) == (SHADOW,)
    check.checks.append(("opportunity pipeline is shadow only", shadow_pipeline,
                         "" if shadow_pipeline else f"the pipeline accepts {PIPELINE_MODES}"))
    check.checks.append(("GEMI collector is configured", bool(getattr(settings, "GEMI_COLLECTOR_ENABLED", False)),
                         "" if getattr(settings, "GEMI_COLLECTOR_ENABLED", False)
                         else "every GEMI request would be refused unsent (no key for this deployment)"))

    tables = set(connection.introspection.table_names())
    missing = sorted(name for name in REQUIRED_MODELS if _model(name)._meta.db_table not in tables)
    check.checks.append(("the 2.0 tables exist", not missing,
                         "" if not missing else f"missing: {', '.join(missing)} -- apply the migrations first"))
    if missing:
        return check   # every check below needs those tables

    live = _model("CompanySignal").objects.filter(mode=LIVE).count()
    check.checks.append(("no LIVE signal exists", not live,
                         "" if not live else f"{live} LIVE signals: the shadow cycle is for the pre-cutover period"))
    cursor = get_cursor()
    ready = cursor.high_water_mark_value is not None
    check.checks.append(("the discovery cursor is initialised", ready,
                         "" if ready else "run bootstrap_gemi_discovery_v2 first"))
    if ready and cursor.status == "anomaly":
        # Not a refusal: the frontier is frozen, which is exactly the safe state, and the evidence already
        # stored is still worth materialising. It is surfaced so the operator investigates.
        check.checks.append((f"the cursor is in anomaly ({cursor.anomaly_reason})", True,
                             "frozen frontier: investigate before trusting today's discovery numbers"))
    return check


# --- phase F (measurement) ---------------------------------------------------------------------------------

def measure_coverage() -> CoverageReport:
    """G4 coverage from stored evidence. Reads only; no GEMI request, no write."""
    Company, CompanySignal = _model("Company"), _model("CompanySignal")
    GemiDiscoveryObservation = _model("GemiDiscoveryObservation")
    report = CoverageReport()

    first_evidence: dict[str, tuple[str, datetime]] = {}
    # Oldest evidence first, exactly as B2 orders it, so "first evidence wins" means the same thing here.
    for number, classification, started_at in eligible_observations().values_list(
            "gemi_number", "classification", "run__started_at"):
        first_evidence.setdefault(number, (classification, started_at))
    report.eligible_companies = len(first_evidence)

    local: set[str] = set()
    for chunk in _chunks(first_evidence):
        local |= set(Company.objects.filter(gemi_number__in=chunk).values_list("gemi_number", flat=True))
    report.eligible_with_company = len(local)
    for chunk in _chunks(sorted(local)):
        report.signals_materialised += CompanySignal.objects.filter(
            signal_type=NEW_COMPANY, company__gemi_number__in=chunk).count()

    for number, (classification, started_at) in first_evidence.items():
        if number in local:
            continue
        report.pending_no_company += 1
        report.pending_by_classification[classification] = report.pending_by_classification.get(classification, 0) + 1
        if report.oldest_pending_at is None or started_at < report.oldest_pending_at:
            report.oldest_pending_at = started_at

    report.rediscovered_local_observations = (
        GemiDiscoveryObservation.objects.exclude(classification=KNOWN).filter(company_existed=True)
        .values("gemi_number").distinct().count())
    return report


# --- the cycle ---------------------------------------------------------------------------------------------

def run_g4_shadow_cycle(*, dry_run: bool = False, compare_date: date | None = None, replay_hours: int | None = None,
                        replay_limit: int = DEFAULT_REPLAY_LIMIT) -> CycleReport:
    """One deterministic SHADOW cycle. See the module docstring. Raises ``ShadowCycleRefused`` before doing any
    work if the precheck refuses; a phase that fails afterwards is recorded, never hidden."""
    if replay_hours is not None and replay_hours < 1:
        raise ValueError("replay_hours must be at least 1")
    if not 1 <= replay_limit <= MAX_REPLAY_LIMIT:
        raise ValueError(f"replay_limit must be from 1 to {MAX_REPLAY_LIMIT}")

    started_at = timezone.now()
    precheck = preflight()
    report = CycleReport(dry_run=dry_run, started_at=started_at, precheck=precheck)
    if precheck.refusals:
        report.phases.append(PhaseOutcome(PRECHECK, FAILED_PHASE,
                                          "; ".join(name for name, _ in precheck.refusals)))
        raise ShadowCycleRefused("; ".join(f"{name}: {detail}" for name, detail in precheck.refusals))
    report.phases.append(PhaseOutcome(PRECHECK, OK))

    # B -- the only phase that talks to GEMI.
    try:
        report.discovery = run_discovery(mode=DISCOVERY_SHADOW, dry_run=dry_run)
    except Exception as error:
        report.phases.append(PhaseOutcome(DISCOVERY, FAILED_PHASE, f"{type(error).__name__}: {error}"[:200]))
        logger.exception("G4 shadow cycle: discovery raised")
    else:
        status = report.discovery.status
        report.phases.append(PhaseOutcome(
            DISCOVERY, FAILED_PHASE if status in ("failed", "anomaly") else OK,
            f"status={status} stop_reason={report.discovery.stop_reason}"))

    # C -- materialisation, with D observed as it happens: the on-commit hook processes each new signal once.
    try:
        with collect_pipeline_runs() as observed:
            report.materialisation = materialize_new_company_signals(dry_run=dry_run)
        report.pipeline_runs = list(observed)
    except Exception as error:
        report.phases.append(PhaseOutcome(MATERIALISATION, FAILED_PHASE, f"{type(error).__name__}: {error}"[:200]))
        logger.exception("G4 shadow cycle: materialisation raised")
        report.phases.append(PhaseOutcome(PIPELINE, SKIPPED, "materialisation did not finish"))
    else:
        materialised = report.materialisation
        report.phases.append(PhaseOutcome(
            MATERIALISATION, OK,
            f"created={materialised.signals_created} existing={materialised.signals_existing} "
            f"pending={materialised.unmaterialised_no_company} conflicts={materialised.conflicts}"))
        broken = [run for run in report.pipeline_runs if run.skipped_reason == FAILED or run.errors]
        report.phases.append(PhaseOutcome(
            PIPELINE, FAILED_PHASE if broken else OK,
            f"observed {len(report.pipeline_runs)} run(s) of the after-commit hook"
            + (f"; {len(broken)} failed -- rerun with --replay-hours to reconcile" if broken else "")))

    # E -- bounded reconciliation, only when asked for, and never the signals phase D just processed.
    if replay_hours is None:
        report.phases.append(PhaseOutcome(REPLAY, SKIPPED, "not requested (phase D already processed new signals)"))
    else:
        just_processed = {run.signal_id for run in report.pipeline_runs}
        try:
            report.replay_runs = replay_missing_shadow_work(
                hours=replay_hours, limit=replay_limit, exclude_ids=just_processed, dry_run=dry_run)
        except Exception as error:
            report.phases.append(PhaseOutcome(REPLAY, FAILED_PHASE, f"{type(error).__name__}: {error}"[:200]))
            logger.exception("G4 shadow cycle: replay raised")
        else:
            broken = [run for run in report.replay_runs if run.skipped_reason == FAILED or run.errors]
            report.phases.append(PhaseOutcome(
                REPLAY, FAILED_PHASE if broken else OK,
                f"{len(report.replay_runs)} signal(s) in the last {replay_hours}h, limit {replay_limit}"
                + (f"; {len(broken)} failed" if broken else "")))

    # F -- measurement and, on request, the existing legacy comparison.
    if compare_date is None:
        report.phases.append(PhaseOutcome(COMPARISON, SKIPPED, "no --compare-date"))
    else:
        try:
            report.comparison = compare_with_legacy(compare_date)
        except Exception as error:
            report.phases.append(PhaseOutcome(COMPARISON, FAILED_PHASE, f"{type(error).__name__}: {error}"[:200]))
            logger.exception("G4 shadow cycle: comparison raised")
        else:
            report.phases.append(PhaseOutcome(COMPARISON, OK, compare_date.isoformat()))

    report.coverage = measure_coverage()
    report.finished_at = timezone.now()
    logger.info(
        "G4 shadow cycle%s: gemi_requests=%s signals_created=%s pipeline_runs=%s pending=%s failed_phases=%s",
        " (dry run)" if dry_run else "", report.gemi_requests,
        report.materialisation.signals_created if report.materialisation else 0,
        len(report.pipeline_runs), report.coverage.pending_no_company, report.failed_phases or "none",
    )
    return report


def replay_missing_shadow_work(*, hours: int, limit: int, exclude_ids=(), dry_run: bool = False) -> list:
    """Reprocess SHADOW signals detected in the window, in id order, except the ones already processed in this
    cycle. Bounded and idempotent: C8 leaves an already linked signal unchanged. No GEMI request."""
    CompanySignal = _model("CompanySignal")
    as_of = timezone.now()
    signals = list(CompanySignal.objects
                   .filter(mode=SHADOW, detected_at__gte=as_of - timedelta(hours=hours), detected_at__lte=as_of)
                   .exclude(pk__in=list(exclude_ids)).order_by("pk")[:limit])
    return [process_company_signal(signal, as_of=as_of, dry_run=dry_run) for signal in signals]
