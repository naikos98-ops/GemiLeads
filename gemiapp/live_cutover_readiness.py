"""Read-only LIVE cutover readiness (``manage.py report_live_cutover_readiness``).

One report, one verdict: **READY** when no check blocks, **BLOCKED** otherwise. Every check is PASS, BLOCKED or
INFO (facts the operator must see but that never block on their own). The whole report runs inside one transaction
that is always rolled back, so it cannot write even by accident: the NEW_COMPANY materialiser and the SHADOW replay
are run in their dry-run forms, and nothing reaches GEMI.

The two backlog checks protect the creation-time boundary. A signal's mode is decided when the row is created, so an
eligible discovery evidence that still has no NEW_COMPANY signal, or a SHADOW signal whose opportunity work is still
missing, would be completed **after** the cutover -- as LIVE. Both backlogs must be zero before LIVE is switched on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef
from django.utils import timezone

from .company_signals import LIVE, SHADOW
from .live_cutover import cutover_state, supported_pipeline_modes
from .organization_entitlement import entitled_organizations

PASS = "PASS"
BLOCKED = "BLOCKED"
INFO = "INFO"
READY = "READY"

INGESTION_TASK = "gemiapp.tasks.run_gemi_ingestion_cycle_task"
EXPECTED_CRON = "13,43 * * * *"
DEFAULT_REPLAY_HOURS = 168
SCORE_COMPONENTS = 5


def _model(name):
    return apps.get_model("gemiapp", name)


@dataclass
class ReadinessReport:
    generated_at: object
    checks: list = field(default_factory=list)    # (status, name, detail)

    def add(self, status: str, name: str, detail="") -> None:
        self.checks.append((status, name, str(detail)))

    def gate(self, name: str, count: int, detail_if_blocked: str = "") -> None:
        self.add(PASS if not count else BLOCKED, name, count if not count or not detail_if_blocked
                 else f"{count} -- {detail_if_blocked}")

    @property
    def blocking(self) -> list:
        return [check for check in self.checks if check[0] == BLOCKED]

    @property
    def verdict(self) -> str:
        return BLOCKED if self.blocking else READY

    def lines(self) -> list[str]:
        out = [f"LIVE CUTOVER READINESS {self.generated_at.isoformat()}"]
        out += [f"[{status}] {name}: {detail}" for status, name, detail in self.checks]
        out.append(f"READINESS: {self.verdict}"
                   + (f" ({len(self.blocking)} blocking check(s))" if self.blocking else ""))
        out.append("read-only: nothing was written and no GEMI request was made.")
        return out


def build_readiness_report(*, replay_hours: int = DEFAULT_REPLAY_HOURS) -> ReadinessReport:
    """Every readiness check, inside a transaction that is always rolled back."""
    with transaction.atomic():
        report = _build(replay_hours=replay_hours)
        transaction.set_rollback(True)
    return report


def _build(*, replay_hours: int) -> ReadinessReport:
    from .g4_shadow_cycle import preflight
    from .new_company_signals import materialize_new_company_signals
    from .signal_replay import replay_window

    report = ReadinessReport(generated_at=timezone.now())
    CompanySignal, Opportunity = _model("CompanySignal"), _model("Opportunity")
    OpportunitySignal, Organization = _model("OpportunitySignal"), _model("Organization")

    # --- configuration and cutover state ------------------------------------------------------------------------
    state = cutover_state()
    report.add(INFO, "GEMI_LIVE_SIGNALS_ENABLED", int(state.live_signals_enabled))
    report.add(INFO, "GEMI_LIVE_OPPORTUNITIES_VISIBLE", int(state.live_opportunities_visible))
    report.add(INFO, "pipeline modes", ",".join(supported_pipeline_modes()))
    report.add(INFO, "cutover state",
               f"ever_activated={state.ever_activated} periods={state.periods} "
               f"first_opened_at={_iso(state.first_opened_at)} first_cycle={_iso(state.first_opened_by_cycle_at)} "
               f"open_since={_iso(state.open_period_opened_at)} last_closed_at={_iso(state.last_closed_at)}")
    report.gate("unauthorized LIVE signals", state.unauthorized_live_signals,
                "LIVE signals created outside every authorized LIVE period")
    legacy = bool(getattr(settings, "GEMI_TRANSITION_LEGACY_ACCESS", True))
    report.add(PASS if legacy else BLOCKED, "GEMI_TRANSITION_LEGACY_ACCESS (legacy navigation + Radar mirror)",
               int(legacy))
    report.add(INFO, "GEMI_DISCOVERY_V2_ENABLED / GEMI_DISCOVERY_SAMEDAY_LANE_ENABLED",
               f"{int(bool(getattr(settings, 'GEMI_DISCOVERY_V2_ENABLED', False)))} / "
               f"{int(bool(getattr(settings, 'GEMI_DISCOVERY_SAMEDAY_LANE_ENABLED', False)))}")
    _schedule(report)
    precheck = preflight(certification=False)
    refusals = [name for name, _ in precheck.refusals]
    report.add(PASS if not refusals else BLOCKED, "ingestion cycle preflight", "; ".join(refusals) or "no refusal")

    # --- signals and opportunities ------------------------------------------------------------------------------
    by_type = CompanySignal.objects.values_list("mode", "signal_type").annotate(n=Count("pk")).order_by("mode",
                                                                                                         "signal_type")
    report.add(INFO, "signals by mode/type", _pairs(by_type))
    by_status = (Opportunity.objects.values_list("latest_signal__mode", "status").annotate(n=Count("pk"))
                 .order_by("latest_signal__mode", "status"))
    report.add(INFO, "opportunities by latest_signal mode/status", _pairs(by_status))
    report.add(INFO, "organizations entitled / total",
               f"{entitled_organizations().count()} / {Organization.objects.count()}")

    report.gate("duplicate Opportunity (organization, radar, company)",
                Opportunity.objects.values("organization", "radar", "company").annotate(n=Count("pk"))
                .filter(n__gt=1).count())
    report.gate("duplicate OpportunitySignal links",
                OpportunitySignal.objects.values("opportunity", "signal").annotate(n=Count("pk"))
                .filter(n__gt=1).count())
    report.gate("Radar / Organization mismatches",
                Opportunity.objects.exclude(organization_id=F("radar__organization_id")).count())
    report.gate("Company / latest_signal mismatches",
                Opportunity.objects.exclude(company_id=F("latest_signal__company_id")).count())
    report.gate("Company / first_signal mismatches",
                Opportunity.objects.exclude(company_id=F("first_signal__company_id")).count())
    report.gate("latest_signal missing from OpportunitySignal history",
                Opportunity.objects.exclude(Exists(OpportunitySignal.objects.filter(
                    opportunity=OuterRef("pk"), signal=OuterRef("latest_signal")))).count())
    report.gate(f"opportunities without exactly {SCORE_COMPONENTS} score components",
                Opportunity.objects.annotate(n=Count("score_components")).exclude(n=SCORE_COMPONENTS).count())
    report.add(INFO, "opportunities of inactive Radars", Opportunity.objects.filter(radar__active=False).count())
    report.add(INFO, "Radar migration tombstones",
               _model("LegacyRadarMigrationMap").objects.filter(organization_radar__isnull=True).count())
    report.add(INFO, "opportunities of organizations not entitled now",
               Opportunity.objects.exclude(organization__in=entitled_organizations()).count())

    # --- backlogs that would cross the creation-time boundary --------------------------------------------------
    materialised = materialize_new_company_signals(dry_run=True)
    report.gate("NEW_COMPANY signals_would_create (all eligible discovery evidence)", materialised.signals_created,
                "run manage.py materialize_new_company_signals (SHADOW) before enabling LIVE")
    report.add(INFO, "eligible discovery evidence pending_no_company", materialised.unmaterialised_no_company)
    replay = replay_window(mode=SHADOW, since_hours=replay_hours, dry_run=True)
    backlog = replay.counters["created"] + replay.counters["updated"] + replay.errors
    report.add(PASS if not backlog else BLOCKED, f"SHADOW replay dry run ({replay_hours}h)",
               f"signals={replay.selected} created={replay.counters['created']} "
               f"updated={replay.counters['updated']} errors={replay.errors}")
    return report


def _schedule(report: ReadinessReport) -> None:
    from django_q.models import Schedule

    rows = list(Schedule.objects.filter(func=INGESTION_TASK).values_list("cron", flat=True))
    ok = rows == [EXPECTED_CRON]
    report.add(PASS if ok else BLOCKED, "ingestion schedule",
               f"rows={len(rows)} cron={','.join(rows) or '-'} (expected exactly one '{EXPECTED_CRON}')")


def _pairs(rows) -> str:
    return " ".join(f"{a}/{b}={n}" for a, b, n in rows) or "none"


def _iso(value) -> str:
    return value.isoformat() if value is not None else "-"


__all__ = ["BLOCKED", "INFO", "LIVE", "PASS", "READY", "ReadinessReport", "build_readiness_report"]
