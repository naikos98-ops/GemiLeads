"""G2: the shadow opportunity pipeline -- the one canonical entry point for a persisted CompanySignal.

    CompanySignal (SHADOW)
      -> C5 matching: active OrganizationRadars, the pre-filter, then the exact v1 evaluation
      -> only Radars of organizations that are **entitled now** (``organization_entitlement``)
      -> C6 score and C7 breakdown of every confirmed match
      -> C8 ``materialize_opportunity``: threshold, create or recapture, idempotent

Nothing is re-implemented here: matching, scoring, explanation and persistence are the existing C5-C8 functions,
called in order; this module chooses which evaluations may reach persistence and reports what happened.

Modes (LIVE cutover)
--------------------
SHADOW signals are always processed. A LIVE signal is processed only through the explicitly enabled LIVE path:
``GEMI_LIVE_SIGNALS_ENABLED`` on (``live_cutover.supported_pipeline_modes``) **and** the signal created inside an
authorized LIVE period (``live_cutover.signal_is_authorized_live``). Otherwise it is skipped (``live_not_enabled`` /
``live_not_authorized``) and left untouched, so a rollback of the flag stops LIVE processing without rewriting
anything. Nothing here promotes a signal or changes its mode. A SHADOW-backed opportunity is internal evaluation
data; customers see LIVE-backed opportunities only, and only while ``GEMI_LIVE_OPPORTUNITIES_VISIBLE`` is on.
Materialising sends nothing: no notification (the D37 emitters are the D31 assignment and the TASK_DUE job), no
email, digest or outreach, no assignment and no task.

Protection between modes
------------------------
C8 recaptures an existing opportunity with the newest qualifying signal whatever its mode. Two rules decide when
the pipeline lets it:

* a SHADOW signal never recaptures an opportunity whose current capture is LIVE-backed (``skipped_live_backed``):
  that would make a customer-visible row SHADOW-backed and hide it;
* a LIVE signal may recapture an existing SHADOW-backed opportunity **in place** -- the same row of the
  (organization, Radar, company) unique key, keeping its ``first_signal``, status (Do Not Contact included),
  assignment, notes and tasks. No second opportunity, no second link.

C8 itself is unchanged.

Entitlement
-----------
Only Radars of organizations whose owner currently holds an entitlement participate; the others are counted as
``skipped_not_entitled`` and never scored or persisted. Nothing already stored is deleted when an entitlement lapses.

Idempotency and determinism
---------------------------
C8 keys an opportunity by (organization, Radar, company) and links a signal once, so processing the same signal again
creates nothing new. Scores are captured ``as_of`` the processing time. A signal already linked to an opportunity has
been processed for that Radar: a replay leaves it exactly as it is (``unchanged``) instead of recapturing it at a
later ``as_of``, so replays never age a frozen capture and are write-free once done. A replay only does missing work
-- a failed run, a Radar created later, an entitlement regained.

Wiring and failure isolation
----------------------------
``record_company_signal`` registers ``process_signal_after_commit`` with ``transaction.on_commit`` when it creates a
signal, so the pipeline runs **synchronously, after the signal and its evidence are committed**, in the same process
as the (operator-run, batch) signal producer. A pipeline failure is logged and reported, never raised into the
producer, and cannot touch the committed signal; ``process_shadow_signals`` replays any signal. Synchronous, not
queued: the producers are operator-run batch commands (no request path, no schedule), and one django-q task per
signal would flood the ORM queue (``queue_limit`` 50) during a backfill and depend on a running worker.

Observability
-------------
``collect_pipeline_runs()`` lets a caller **observe** the runs that happen inside a block -- above all the ones the
``on_commit`` hook starts while a producer is writing signals, whose return value the hook itself discards. It is an
observer and nothing else: what runs, and what each run does, is identical whether or not anyone is collecting. An
orchestrator that wants the numbers therefore never has to process the same new signal a second time.

Every run returns a ``PipelineRun`` and logs one line: signal, mode, context status, Radars considered (active,
after C5's pre-filter), of those entitled, matched / insufficient state / no match, opportunities created /
updated / unchanged, below threshold, skipped (not entitled), errors. No schema, no customer analytics.

Reference data: KAD, region and legal-form criteria are compared against the snapshot's source ids; without a
snapshot (or reference rows behind a Radar's criteria) C5 reports ``INSUFFICIENT_STATE`` -- counted, never a match.
Signal-type-only Radars match without any reference data.
"""

from __future__ import annotations

import dataclasses
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from django.apps import apps
from django.db.models import Exists, OuterRef
from django.utils import timezone

from . import organization_radar_matching as matching
from .company_signals import LIVE, SHADOW
from .live_cutover import signal_is_authorized_live, supported_pipeline_modes
from .opportunities import _score_of, materialize_opportunity, qualifies
from .opportunity_score_breakdown import explain_opportunity_scores_for_report
from .organization_entitlement import entitled_organizations

logger = logging.getLogger(__name__)

# Accepted unconditionally. LIVE is added only by the explicitly enabled LIVE path (``supported_pipeline_modes``).
PIPELINE_MODES = (SHADOW,)
LIVE_NOT_ENABLED = "live_not_enabled"
LIVE_NOT_AUTHORIZED = "live_not_authorized"   # a LIVE signal created outside every authorized LIVE period
FAILED = "failed"


class PipelineError(ValueError):
    """The input is not a saved CompanySignal. Nothing was processed."""


@dataclass
class PipelineRun:
    signal_id: int
    signal_mode: str
    dry_run: bool = False
    processed: bool = False
    skipped_reason: str = ""
    context_status: str = ""
    considered: int = 0              # active Radars (all organizations) surviving C5's pre-filter
    entitled_considered: int = 0     # ... of organizations entitled now
    skipped_not_entitled: int = 0
    matched: int = 0
    insufficient_state: int = 0
    no_match: int = 0
    created: int = 0                 # dry run: would create
    updated: int = 0                 # dry run: existing opportunities that would be recaptured / linked
    unchanged: int = 0
    below_threshold: int = 0
    skipped_live_backed: int = 0     # an existing opportunity is LIVE-backed: a SHADOW signal never recaptures it
    errors: list = field(default_factory=list)  # (radar_id or None, exception class name)

    def line(self) -> str:
        prefix = "[dry-run] " if self.dry_run else ""
        if self.skipped_reason:
            return f"{prefix}signal={self.signal_id} mode={self.signal_mode} skipped={self.skipped_reason}"
        return (f"{prefix}signal={self.signal_id} mode={self.signal_mode} context={self.context_status or '-'} "
                f"considered={self.considered} entitled={self.entitled_considered} "
                f"skipped_not_entitled={self.skipped_not_entitled} matched={self.matched} "
                f"insufficient_state={self.insufficient_state} no_match={self.no_match} created={self.created} "
                f"updated={self.updated} unchanged={self.unchanged} below_threshold={self.below_threshold} "
                f"skipped_live_backed={self.skipped_live_backed} errors={len(self.errors)}")


def _model(name):
    return apps.get_model("gemiapp", name)


_collected_runs: ContextVar[list | None] = ContextVar("shadow_pipeline_runs", default=None)


@contextmanager
def collect_pipeline_runs():
    """Collect every ``PipelineRun`` produced inside this block, including the ones ``transaction.on_commit``
    starts. Observation only -- see the module docstring."""
    runs: list[PipelineRun] = []
    token = _collected_runs.set(runs)
    try:
        yield runs
    finally:
        _collected_runs.reset(token)


def process_company_signal(signal, *, as_of=None, dry_run: bool = False) -> PipelineRun:
    """Run the shadow pipeline for one saved CompanySignal and report what happened. Never raises for a pipeline
    failure (it is recorded in ``errors`` and logged); raises ``PipelineError`` only for an invalid input."""
    run = _process_company_signal(signal, as_of=as_of, dry_run=dry_run)
    observers = _collected_runs.get()
    if observers is not None:
        observers.append(run)
    return run


def _process_company_signal(signal, *, as_of, dry_run: bool) -> PipelineRun:
    CompanySignal = _model("CompanySignal")
    if not isinstance(signal, CompanySignal) or signal.pk is None:
        raise PipelineError("a saved CompanySignal is required")
    stored = CompanySignal.objects.filter(pk=signal.pk).first()
    if stored is None:
        raise PipelineError("the CompanySignal no longer exists")
    run = PipelineRun(signal_id=stored.pk, signal_mode=stored.mode, dry_run=dry_run)
    if stored.mode not in supported_pipeline_modes():
        run.skipped_reason = LIVE_NOT_ENABLED
        logger.info("Shadow opportunity pipeline: %s", run.line())
        return run
    if stored.mode == LIVE and not signal_is_authorized_live(stored):
        run.skipped_reason = LIVE_NOT_AUTHORIZED
        logger.error("Opportunity pipeline: LIVE signal %s was created outside every authorized LIVE period; "
                     "it is not processed", stored.pk)
        return run
    as_of = as_of or timezone.now()
    try:
        report = matching.explain_organization_radar_matches(stored)
        run.context_status = report.context.context_status
        run.considered = report.candidate_count
        organizations = {item.organization_id for item in report.evaluations}
        entitled = (set(entitled_organizations().filter(pk__in=organizations).values_list("pk", flat=True))
                    if organizations else set())
        kept = tuple(item for item in report.evaluations if item.organization_id in entitled)
        run.entitled_considered = len(kept)
        run.skipped_not_entitled = len(report.evaluations) - len(kept)
        run.matched = sum(1 for item in kept if item.evaluation.status == matching.MATCH)
        run.insufficient_state = sum(1 for item in kept if item.evaluation.status == matching.INSUFFICIENT_STATE)
        run.no_match = sum(1 for item in kept if item.evaluation.status == matching.NO_MATCH)
        breakdowns = explain_opportunity_scores_for_report(dataclasses.replace(report, evaluations=kept), as_of=as_of)
    except Exception as error:  # the committed signal is untouched; a replay can retry
        run.errors.append((None, type(error).__name__))
        run.skipped_reason = FAILED
        logger.exception("Shadow opportunity pipeline failed for signal %s", stored.pk)
        return run

    radars = {radar.pk: radar for radar in _model("OrganizationRadar").objects.filter(
        pk__in=[breakdown.radar_id for breakdown in breakdowns])}
    Opportunity, OpportunitySignal = _model("Opportunity"), _model("OpportunitySignal")
    for breakdown in breakdowns:
        radar, score = radars[breakdown.radar_id], _score_of(breakdown)
        existing = (Opportunity.objects.filter(organization_id=radar.organization_id, radar_id=radar.pk,
                                               company_id=stored.company_id)
                    .annotate(linked=Exists(OpportunitySignal.objects.filter(opportunity=OuterRef("pk"),
                                                                             signal_id=stored.pk)))
                    .values_list("latest_signal__mode", "linked").first())
        if existing is not None and stored.mode == SHADOW and existing[0] != SHADOW:
            run.skipped_live_backed += 1  # a SHADOW signal never recaptures a LIVE-backed opportunity
            continue
        # A LIVE signal recaptures an existing SHADOW-backed row in place: C8 keeps its first_signal and status.
        if existing is not None and existing[1]:
            run.unchanged += 1  # this signal was already processed for this Radar: never recaptured by a replay
            continue
        if dry_run:
            if not qualifies(radar, score):
                run.below_threshold += 1
            elif existing is not None:
                run.updated += 1
            else:
                run.created += 1
            continue
        try:
            result = materialize_opportunity(signal=stored, radar=radar, score=score, breakdown=breakdown)
        except Exception as error:  # one Radar's failure never blocks the others; each write is atomic
            run.errors.append((breakdown.radar_id, type(error).__name__))
            logger.exception("Shadow opportunity pipeline: signal %s, radar %s failed", stored.pk, breakdown.radar_id)
            continue
        if not result.eligible:
            run.below_threshold += 1
        elif result.created:
            run.created += 1
        elif result.rescored or result.signal_attached:
            run.updated += 1
        else:
            run.unchanged += 1
    run.processed = True
    logger.info("Shadow opportunity pipeline: %s", run.line())
    return run


def process_signal_after_commit(signal_id: int):
    """``transaction.on_commit`` hook registered by ``record_company_signal`` for a newly created signal. The signal
    is already committed: this never raises, so a pipeline problem can never reach the producer."""
    try:
        signal = _model("CompanySignal").objects.filter(pk=signal_id).first()
        return process_company_signal(signal) if signal is not None else None
    except Exception:
        logger.exception("Shadow opportunity pipeline hook failed for signal %s", signal_id)
        return None
