"""Operator alerting for the scheduled GEMI ingestion cycle (gemiapp.ingestion_cycle).

A cycle that runs every few minutes fails quietly: the run row says ``failed``, the cursor counts it, and nobody
is told. This module decides when a human should be, and says so through the project's one active alert channel:
an ERROR on a logger named in ``settings.OPERATOR_ALERT_LOGGERS`` reaches the operators by email
(``config.operator_alerts``; the existing SMTP relay, no new service). Nothing here contacts Sentry or anything
external, and nothing here can change what the cycle did.

When an operator is alerted
---------------------------
* ``write_failed``  -- the discovery run stopped on an unexpected Company/activity write failure
  (``stop_reason=ingest_write_failed``) for ``GEMI_INGESTION_ALERT_WRITE_FAILURE_THRESHOLD`` consecutive runs
  (default 2): it failed, and it failed again on the retry, so it is not transient and the frontier is held.
* ``ordering_anomaly`` -- a blocking ordering anomaly froze the cursor. Alerted on the first occurrence: the
  frontier does not unfreeze by itself and re-verifying it is an operator decision.
* ``repeated_failures`` -- ``GEMI_INGESTION_ALERT_FAILURE_THRESHOLD`` consecutive runs (default 3) ended without
  success, whatever the reason (GEMI unreachable, budget starvation, page limit).
* ``no_progress`` -- no successful run for ``GEMI_INGESTION_ALERT_STALE_SECONDS`` (default 2 h). This is what
  catches the failures that leave no run row at all: a lock that is never released, a cycle that is refused.
* ``refused`` -- ingest is enabled but the precheck refuses the cycle (a LIVE signal exists, the cursor is not
  initialised, a table is missing). It can never make progress until someone acts.
* ``phase_failed`` -- discovery succeeded but materialisation or the opportunity pipeline raised.

One voice
---------
The scheduled task runs the cycle inside ``config.operator_alerts.managed_alerting()``. Without it every failed
run would also email through the GEMI client's own ERROR ("giving up after N attempts"), one message per run for
as long as an outage lasts. Inside that block those records still reach the server log, and only this policy
emails. The legacy importer is outside it and alerts exactly as before.

When nobody is alerted
----------------------
One isolated failure is recorded -- the run row, the cursor's failure count and a WARNING on this logger (the
server log) -- and nothing else. So is every repeat of a condition already alerted: each kind is alerted once and
then only reminded every ``GEMI_INGESTION_ALERT_REMINDER_SECONDS`` (default 24 h) while it lasts. A cycle skipped
because another one holds the lock is not a failure. While ingest is disabled (the default) the task is dormant
and this module is never consulted.

Recovery
--------
The first fully successful cycle after an alert logs a WARNING, emails the operators once ("recovered", with the
kinds that were open) and clears them, so the next failure alerts afresh.

State
-----
What happened is read from the durable record -- ``GemiDiscoveryRun`` rows and the cursor -- never from memory.
Only the "already alerted" marks live in the shared (database) cache, one key per kind with the reminder interval
as its TTL. If that cache is unavailable the alert is sent anyway: a duplicate email is the safe failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.apps import apps
from django.conf import settings
from django.core.cache import caches
from django.core.mail import mail_admins
from django.utils import timezone

from .ingestion.discovery import INGEST, STOP_INGEST_WRITE_FAILED, STREAM_COMPANIES, get_cursor

# Named in settings.OPERATOR_ALERT_LOGGERS: an ERROR here is an operator email, a WARNING is a server-log line.
logger = logging.getLogger("gemiapp.ingestion_alerts")

WRITE_FAILED = "write_failed"
ORDERING_ANOMALY = "ordering_anomaly"
REPEATED_FAILURES = "repeated_failures"
NO_PROGRESS = "no_progress"
REFUSED = "refused"
PHASE_FAILED = "phase_failed"
KINDS = (WRITE_FAILED, ORDERING_ANOMALY, REPEATED_FAILURES, NO_PROGRESS, REFUSED, PHASE_FAILED)

DEFAULT_WRITE_FAILURE_THRESHOLD = 2
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_STALE_SECONDS = 2 * 3600
DEFAULT_REMINDER_SECONDS = 24 * 3600
STREAK_LOOKBACK = 200


@dataclass(frozen=True)
class AlertPolicy:
    write_failure_threshold: int = DEFAULT_WRITE_FAILURE_THRESHOLD
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    stale_seconds: int = DEFAULT_STALE_SECONDS
    reminder_seconds: int = DEFAULT_REMINDER_SECONDS


def policy_from_settings() -> AlertPolicy:
    return AlertPolicy(
        write_failure_threshold=max(1, int(getattr(
            settings, "GEMI_INGESTION_ALERT_WRITE_FAILURE_THRESHOLD", DEFAULT_WRITE_FAILURE_THRESHOLD))),
        failure_threshold=max(1, int(getattr(
            settings, "GEMI_INGESTION_ALERT_FAILURE_THRESHOLD", DEFAULT_FAILURE_THRESHOLD))),
        stale_seconds=max(60, int(getattr(settings, "GEMI_INGESTION_ALERT_STALE_SECONDS", DEFAULT_STALE_SECONDS))),
        reminder_seconds=max(60, int(getattr(
            settings, "GEMI_INGESTION_ALERT_REMINDER_SECONDS", DEFAULT_REMINDER_SECONDS))),
    )


@dataclass(frozen=True)
class Raised:
    kind: str
    alerted: bool      # False: the condition holds but was already alerted within the reminder interval
    message: str


def _key(kind: str) -> str:
    return f"gemi-ingestion-alert:{kind}"


def open_alerts() -> list[str]:
    """The kinds currently marked as alerted. Empty when the cache cannot be read."""
    try:
        found = caches["shared"].get_many([_key(kind) for kind in KINDS])
    except Exception:
        return []
    return [kind for kind in KINDS if _key(kind) in found]


def _raise(kind: str, message: str, *, policy: AlertPolicy, now: datetime) -> Raised:
    """Alert once per reminder interval; record every other occurrence as a WARNING."""
    try:
        first = bool(caches["shared"].add(_key(kind), now.isoformat(), policy.reminder_seconds))
    except Exception:
        first = True    # cannot tell whether it was sent: sending twice is the safe failure
    if first:
        logger.error("GEMI ingestion ALERT [%s]: %s", kind, message)
    else:
        logger.warning("GEMI ingestion [%s] continues (already alerted): %s", kind, message)
    return Raised(kind, first, message)


def failure_streak(stream: str = STREAM_COMPANIES) -> list:
    """The most recent ingest-mode runs that did not succeed, newest first, up to the last success."""
    GemiDiscoveryRun = apps.get_model("gemiapp", "GemiDiscoveryRun")
    streak = []
    for run in GemiDiscoveryRun.objects.filter(stream=stream, mode=INGEST).order_by("-id")[:STREAK_LOOKBACK]:
        if run.status == "success":
            break
        streak.append(run)
    return streak


def _stale(cursor, *, policy: AlertPolicy, now: datetime) -> timedelta | None:
    if cursor.last_success_at is None:
        return None     # never succeeded: an uninitialised cursor is a refusal, reported as such
    age = now - cursor.last_success_at
    return age if age > timedelta(seconds=policy.stale_seconds) else None


def _recover(*, now: datetime) -> list[str]:
    """Close every open alert after a fully successful cycle and tell the operators once."""
    recovered = open_alerts()
    if not recovered:
        return []
    try:
        caches["shared"].delete_many([_key(kind) for kind in recovered])
    except Exception:
        logger.warning("GEMI ingestion: could not clear the alert marks; they expire on their own.")
    message = (f"The GEMI ingestion cycle completed successfully at {now.isoformat()} after: "
               f"{', '.join(recovered)}. The discovery frontier is advancing again.")
    logger.warning("GEMI ingestion RECOVERED: %s", message)
    if not settings.DEBUG:      # the same rule as the alert handler: a development run notifies nobody
        try:
            mail_admins("GEMI ingestion recovered", message, fail_silently=True)
        except Exception as exc:
            logger.warning("GEMI ingestion: the recovery email was not sent (%s).", type(exc).__name__)
    return recovered


@dataclass
class AlertOutcome:
    raised: list
    recovered: list

    @property
    def alerted(self) -> list[str]:
        return [item.kind for item in self.raised if item.alerted]

    def summary(self) -> dict:
        return {"alerted": self.alerted, "open": [item.kind for item in self.raised], "recovered": self.recovered}


def evaluate_refusal(reason: str, *, policy: AlertPolicy | None = None, now: datetime | None = None) -> AlertOutcome:
    """Ingest is enabled but the precheck refused the cycle: nothing can progress until someone acts."""
    policy, now = policy or policy_from_settings(), now or timezone.now()
    raised = [_raise(REFUSED, f"the cycle is refused before any request or write: {reason}", policy=policy, now=now)]
    age = _stale(get_cursor(), policy=policy, now=now)
    if age is not None:
        raised.append(_raise(NO_PROGRESS, _no_progress(age), policy=policy, now=now))
    return AlertOutcome(raised, [])


def _no_progress(age: timedelta) -> str:
    hours, minutes = divmod(int(age.total_seconds()) // 60, 60)
    return (f"no successful ingestion run for {hours}h {minutes:02d}m: the discovery frontier has not been "
            f"confirmed or advanced in that time")


def evaluate_cycle(report, *, policy: AlertPolicy | None = None, now: datetime | None = None) -> AlertOutcome:
    """Decide what, if anything, an operator must hear about one finished (non dry-run) cycle.

    Reads the durable run history and the cursor; ``report`` only says how this invocation ended. Never raises
    for an alerting problem of its own -- the caller wraps it anyway -- and never changes ingestion state.
    """
    from .ingestion_cycle import OK, SKIPPED_LOCKED

    policy, now = policy or policy_from_settings(), now or timezone.now()
    cursor = get_cursor()
    raised: list[Raised] = []

    if report.status == OK:
        return AlertOutcome([], _recover(now=now))

    if report.status != SKIPPED_LOCKED:
        discovery = report.discovery
        streak = failure_streak()
        if discovery is not None and discovery.blocking_anomalies:
            raised.append(_raise(
                ORDERING_ANOMALY,
                f"a blocking ordering anomaly ({discovery.blocking_anomalies[0]['kind']}) froze the discovery "
                f"cursor at {cursor.high_water_mark or 'none'} (run {discovery.run_id}). Investigate, then "
                f"re-verify the frontier with bootstrap_gemi_discovery_v2 --force.",
                policy=policy, now=now))
        write_failures = 0
        for run in streak:
            if run.stop_reason != STOP_INGEST_WRITE_FAILED:
                break
            write_failures += 1
        if write_failures >= policy.write_failure_threshold:
            raised.append(_raise(
                WRITE_FAILED,
                f"{write_failures} consecutive runs stopped on an unexpected Company/activity write failure "
                f"(latest run {streak[0].pk}). The frontier is held at {cursor.high_water_mark or 'none'} and "
                f"every run retries the same record.",
                policy=policy, now=now))
        elif len(streak) >= policy.failure_threshold and not (discovery is not None and discovery.blocking_anomalies):
            reasons = sorted({f"{run.status}/{run.stop_reason or 'error'}" for run in streak})
            raised.append(_raise(
                REPEATED_FAILURES,
                f"{len(streak)} consecutive ingestion runs ended without success ({', '.join(reasons)}); "
                f"latest run {streak[0].pk}. The frontier is held at {cursor.high_water_mark or 'none'}.",
                policy=policy, now=now))
        phases = [name for name, _ in report.failed_phases if name != "discovery"]
        if phases:
            raised.append(_raise(
                PHASE_FAILED,
                f"discovery finished but these phases failed: {', '.join(phases)}. Companies already stored are "
                f"kept; signals are retried by the next cycle's catch-up.",
                policy=policy, now=now))
        if not raised:
            logger.warning("GEMI ingestion cycle did not succeed (status=%s, consecutive non-successful runs=%s); "
                           "recorded, no alert yet.", report.status, len(streak))

    age = _stale(cursor, policy=policy, now=now)
    if age is not None:
        raised.append(_raise(NO_PROGRESS, _no_progress(age), policy=policy, now=now))
    return AlertOutcome(raised, [])


def ingestion_health(*, now: datetime | None = None) -> list[str]:
    """A read-only description of where the scheduled ingestion stands. No write, no request."""
    now = now or timezone.now()
    cursor = get_cursor()
    streak = failure_streak()
    age = None if cursor.last_success_at is None else now - cursor.last_success_at
    return [
        f"cursor status={cursor.status} frontier={cursor.high_water_mark or 'none'} "
        f"consecutive_failures={cursor.consecutive_failures}",
        f"last success: {'never' if age is None else f'{int(age.total_seconds()) // 60} min ago'} "
        f"(last attempt: {cursor.last_attempted_at.isoformat() if cursor.last_attempted_at else 'never'})",
        f"consecutive non-successful ingest runs={len(streak)}"
        + (f" (latest: run {streak[0].pk} {streak[0].status}/{streak[0].stop_reason or 'error'})" if streak else ""),
        f"open alerts: {', '.join(open_alerts()) or 'none'}",
    ]
