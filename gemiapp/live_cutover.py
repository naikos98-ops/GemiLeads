"""The LIVE cutover: two dormant switches and the persistent record of authorized LIVE periods.

Two flags, both off by default, deliberately independent
---------------------------------------------------------
``GEMI_LIVE_SIGNALS_ENABLED``       -- whether the scheduled ingestion cycle creates its NEW_COMPANY signals LIVE,
                                       and whether the opportunity pipeline may process a LIVE signal at all.
``GEMI_LIVE_OPPORTUNITIES_VISIBLE`` -- whether a customer may see (and act on) LIVE-backed opportunities.

LIVE processing and customer visibility are separate decisions: LIVE opportunities can be produced "dark" for a
while before anyone sees them, and hiding them again (rollback R1) never stops or rewrites anything. Visibility does
**not** consult the persistent state below; it is its own flag, read in one place (``customer_visible_signal_mode``).

Signal mode resolver
--------------------
``current_signal_mode()`` is the one place that turns the flag into a mode. The ingestion cycle calls it exactly
once, inside its mutex, and every NEW_COMPANY signal that cycle creates gets that mode. Snapshot/change producers
never call it: they stay SHADOW.

The boundary: creation time, recorded as authorized LIVE periods
----------------------------------------------------------------
A LIVE signal is legitimate only if it was **created** inside an authorized LIVE period. ``LiveSignalPeriod`` is the
append-only record of those periods: one row per period, ``opened_at`` when the first cycle with the flag on ran,
``closed_at`` when the first cycle with the flag off ran. At most one period is open (database constraint); a period
is closed, never deleted, and a re-enable opens a **new** row. So:

* "LIVE has ever been activated"           -> any row exists;
* "the authorized boundary"               -> the earliest ``opened_at`` (and its cycle's start time);
* "a historical LIVE row is legitimate"   -> its ``created_at`` falls inside some period, open or closed;
* "an unauthorized LIVE row"              -> a LIVE signal created outside every period.

Rollback (flag off) closes the open period at the next cycle and never touches a signal: historical LIVE rows stay
LIVE and stay legitimate, so they never stop ingestion. Re-enabling opens a new period at the next cycle; nothing
old is rewritten. Only the cycle writes periods (``record_cycle_signal_mode``), so the transitions are exactly the
flag changes the scheduled ingestion actually acted on, each with its timestamps. Repeating a transition is a no-op.

Known limit, accepted: between a flag change and the next cycle the open period is not yet closed; a LIVE signal
written by something other than the cycle in that window would still count as authorized. Nothing but the cycle
creates LIVE signals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.apps import apps
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .company_signals import LIVE, SHADOW

logger = logging.getLogger(__name__)

OPENED = "opened"
CLOSED = "closed"
UNCHANGED = "unchanged"


def _model(name):
    return apps.get_model("gemiapp", name)


# --- flags ------------------------------------------------------------------------------------------------------

def live_signals_enabled() -> bool:
    return bool(getattr(settings, "GEMI_LIVE_SIGNALS_ENABLED", False))


def live_opportunities_visible() -> bool:
    return bool(getattr(settings, "GEMI_LIVE_OPPORTUNITIES_VISIBLE", False))


def current_signal_mode() -> str:
    """The mode the ingestion cycle gives its NEW_COMPANY signals. Read once per cycle, inside the cycle mutex."""
    return LIVE if live_signals_enabled() else SHADOW


def supported_pipeline_modes() -> tuple:
    """The signal modes the opportunity pipeline may process now. SHADOW always; LIVE only while it is enabled."""
    return (SHADOW, LIVE) if live_signals_enabled() else (SHADOW,)


# --- customer visibility: the one rule -------------------------------------------------------------------------

def customer_visible_signal_mode() -> str | None:
    """The signal mode a customer-visible opportunity's current capture must rest on, or None while customers see
    no opportunity at all. Every customer surface and mutation derives its scope from this one function."""
    return LIVE if live_opportunities_visible() else None


def customer_visible(queryset, path: str = ""):
    """``queryset`` (of Opportunity, or of rows reaching it through ``path``) reduced to what customers may see."""
    mode = customer_visible_signal_mode()
    if mode is None:
        return queryset.none()
    return queryset.filter(**{f"{path}latest_signal__mode": mode})


# --- persistent authorized LIVE periods ------------------------------------------------------------------------

def authorized_live_q(created_at_ref: str = "created_at") -> Exists:
    """Exists(a LIVE period that contains the outer row's ``created_at_ref``)."""
    return Exists(_model("LiveSignalPeriod").objects.filter(
        opened_at__lte=OuterRef(created_at_ref)).filter(
        Q(closed_at__isnull=True) | Q(closed_at__gte=OuterRef(created_at_ref))))


def unauthorized_live_signals():
    """LIVE signals created outside every authorized LIVE period: always a defect."""
    return _model("CompanySignal").objects.filter(mode=LIVE).exclude(authorized_live_q())


def signal_is_authorized_live(signal) -> bool:
    return (signal.mode == LIVE
            and _model("CompanySignal").objects.filter(pk=signal.pk).filter(authorized_live_q()).exists())


def open_period():
    return _model("LiveSignalPeriod").objects.filter(closed_at__isnull=True).first()


def record_cycle_signal_mode(mode: str, *, cycle_started_at, now=None) -> str:
    """Make the persistent periods agree with the mode a real (non-dry-run) cycle just fixed. Idempotent.

    LIVE with no open period opens one; SHADOW with an open period closes it; anything else changes nothing.
    """
    if mode not in (SHADOW, LIVE):
        raise ValueError(f"unknown signal mode: {mode}")
    Period = _model("LiveSignalPeriod")
    now = now or timezone.now()
    with transaction.atomic():
        current = Period.objects.select_for_update().filter(closed_at__isnull=True).first()
        if mode == LIVE:
            if current is not None:
                return UNCHANGED
            try:
                with transaction.atomic():
                    Period.objects.create(opened_at=now, opened_by_cycle_at=cycle_started_at, is_open=True)
            except IntegrityError:  # another writer opened it first: the constraint allows only one
                return UNCHANGED
            logger.warning("LIVE signal period opened at %s (cycle started %s)", now.isoformat(),
                           cycle_started_at.isoformat())
            return OPENED
        if current is None:
            return UNCHANGED
        current.closed_at = max(now, current.opened_at)
        current.closed_by_cycle_at = cycle_started_at
        current.is_open = False
        current.save(update_fields=["closed_at", "closed_by_cycle_at", "is_open"])
        logger.warning("LIVE signal period #%s closed at %s (cycle started %s)", current.pk,
                       current.closed_at.isoformat(), cycle_started_at.isoformat())
        return CLOSED


@dataclass(frozen=True)
class CutoverState:
    live_signals_enabled: bool
    live_opportunities_visible: bool
    ever_activated: bool
    periods: int
    first_opened_at: object
    first_opened_by_cycle_at: object
    open_period_opened_at: object
    last_closed_at: object
    live_signals: int
    unauthorized_live_signals: int

    @property
    def authorized_live_signals(self) -> int:
        return self.live_signals - self.unauthorized_live_signals

    def lines(self) -> list[str]:
        return [
            f"GEMI_LIVE_SIGNALS_ENABLED={int(self.live_signals_enabled)} "
            f"GEMI_LIVE_OPPORTUNITIES_VISIBLE={int(self.live_opportunities_visible)}",
            f"LIVE ever activated={self.ever_activated} periods={self.periods} "
            f"first opened={_iso(self.first_opened_at)} (cycle {_iso(self.first_opened_by_cycle_at)}) "
            f"open since={_iso(self.open_period_opened_at)} last closed={_iso(self.last_closed_at)}",
            f"LIVE signals={self.live_signals} authorized={self.authorized_live_signals} "
            f"unauthorized={self.unauthorized_live_signals}",
        ]


def _iso(value) -> str:
    return value.isoformat() if value is not None else "-"


def cutover_state() -> CutoverState:
    """The cutover as it stands: flags, periods, LIVE signals. Reads only."""
    Period = _model("LiveSignalPeriod")
    periods = list(Period.objects.order_by("opened_at", "pk").values_list(
        "opened_at", "opened_by_cycle_at", "closed_at"))
    current = next((row for row in periods if row[2] is None), None)
    closed = [row[2] for row in periods if row[2] is not None]
    return CutoverState(
        live_signals_enabled=live_signals_enabled(), live_opportunities_visible=live_opportunities_visible(),
        ever_activated=bool(periods), periods=len(periods),
        first_opened_at=periods[0][0] if periods else None,
        first_opened_by_cycle_at=periods[0][1] if periods else None,
        open_period_opened_at=current[0] if current else None,
        last_closed_at=max(closed) if closed else None,
        live_signals=_model("CompanySignal").objects.filter(mode=LIVE).count(),
        unauthorized_live_signals=unauthorized_live_signals().count(),
    )
