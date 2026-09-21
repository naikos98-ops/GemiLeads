"""G6: what the application really spends of the ΓΕΜΗ request allowance, and how much is left.

Read-only. It makes no GEMI request, writes nothing and changes nothing: it summarises the rows that
gemiapp.ingestion.request_metrics wrote from the client's own request path. Only operational metadata is
involved -- no API key, no query parameters, no payload, no company identifier.
"""

from django.core.management.base import BaseCommand

from gemiapp.ingestion.rate_budget import MAX_REQUESTS_PER_MINUTE
from gemiapp.ingestion.request_metrics import HIGH_UTILISATION_SHARE, ROLLING_WINDOW_SECONDS, build_report


def _stamp(value) -> str:
    return "-" if value is None else value.strftime("%Y-%m-%d %H:%M:%S %Z")


class Command(BaseCommand):
    help = (
        "Αναφορά G6: πραγματικές εξερχόμενες κλήσεις προς το GEMI ανά lane, retries, σφάλματα, αναμονή στο "
        "κοινό budget και peak/headroom σε κυλιόμενο λεπτό. Μόνο ανάγνωση: καμία κλήση GEMI, καμία εγγραφή."
    )

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=float, default=24.0,
                            help="Παράθυρο παρατήρησης σε ώρες (προεπιλογή 24).")

    def handle(self, *args, **options):
        # Always against the application's safe ceiling: headroom relative to anything else is not G6 evidence.
        report = build_report(hours=options["hours"], ceiling=MAX_REQUESTS_PER_MINUTE)
        write = self.stdout.write

        write("GEMI REQUEST BUDGET - G6 observation")
        write(f"  window                : {_stamp(report.window_start)} .. {_stamp(report.window_end)} "
              f"({report.hours:g}h)")

        write("")
        write("CAPACITY")
        write(f"  safe ceiling          : {report.ceiling_per_minute}/min   (MAX_REQUESTS_PER_MINUTE, the "
              "theoretical application maximum)")
        write(f"  configured limit      : GEMI_RATE_LIMIT_PER_MINUTE={report.configured_limit_per_minute}")
        write(f"  effective capacity    : {report.effective_capacity_per_minute}/min   (what the budget enforces: "
              f"the configured limit clamped to 2..{report.ceiling_per_minute}, as BudgetConfig.from_settings)")
        write("  -> every G6 operational decision uses the EFFECTIVE capacity, not the theoretical ceiling.")

        write("")
        write("OUTBOUND ATTEMPTS (reached the transport and spent a slot; each retry counted separately)")
        write(f"  outbound attempts     : {report.sent_attempts}")
        write(f"  of which retries      : {report.retries}")
        write(f"  successful (2xx)      : {report.successes}")
        write(f"  logical calls         : {report.logical_calls}   (first attempts, including any that never sent)")
        write(f"  not sent (budget)     : {report.budget_timeouts + report.budget_unavailable}   "
              f"(excluded from every count above and from the peak)")

        write("")
        write("BY LANE (attempts that consumed a slot)")
        for lane in report.by_lane:
            write(f"  {lane.lane:<18}: attempts={lane.attempts} success={lane.successes} "
                  f"retries={lane.retries} failures={lane.failures} not_sent={lane.not_sent}")

        write("")
        write("OUTCOMES")
        write(f"  429 rate limited      : {report.rate_limited}")
        write(f"  5xx server errors     : {report.server_errors}")
        write(f"  4xx client errors     : {report.client_errors}")
        write(f"  transport/network     : {report.transport_errors}")
        write(f"  budget timeout        : {report.budget_timeouts}   (no slot within max_wait - nothing sent)")
        write(f"  budget unavailable    : {report.budget_unavailable}   (shared store failed - nothing sent)")
        if report.future_attempts:
            write(f"  NOTE: {report.future_attempts} recorded attempt(s) are stamped after the end of this "
                  "window, which means a worker's clock runs ahead. They are excluded from every figure above.")

        write("")
        write("BUDGET WAIT (time callers spent inside GemiRateBudget.acquire)")
        write(f"  total                 : {report.total_wait_seconds:.1f}s")
        write(f"  average per attempt   : {report.average_wait_seconds:.2f}s")
        write(f"  maximum               : {report.max_wait_seconds:.1f}s")

        write("")
        write(f"PEAK USAGE (exact rolling {ROLLING_WINDOW_SECONDS:.0f}s window, anchored at each attempt -")
        write("            this is a true rolling window, not a wall-clock minute bucket)")
        write(f"  peak outbound/60s     : {report.peak_rolling_60s}")
        write(f"  peak window began     : {_stamp(report.peak_window_start)}")
        write(f"  average rate          : {report.average_per_minute:.2f} requests/min over the window")
        write("")
        write(f"  vs EFFECTIVE capacity {report.effective_capacity_per_minute}/min (the decision basis):")
        write(f"    utilisation         : {report.utilisation_vs_capacity_pct:.1f}%")
        write(f"    headroom            : {report.headroom_vs_capacity} requests/min")
        if report.over_capacity:
            write(f"    OVER CAPACITY       : peak exceeded the effective capacity by {report.over_capacity} "
                  "request(s)/min - no headroom")
        elif report.at_or_over_capacity:
            write("    AT CAPACITY         : peak reached the effective capacity - no headroom")
        write(f"    saturated windows   : {report.saturated_windows} "
              f"(anchored windows reaching {report.effective_capacity_per_minute})")
        write(f"    high-utilisation    : {report.high_utilisation_windows} "
              f"(anchored windows at >={HIGH_UTILISATION_SHARE:.0%} of {report.effective_capacity_per_minute})")
        write(f"  vs safe ceiling {report.ceiling_per_minute}/min (theoretical maximum):")
        write(f"    utilisation         : {report.utilisation_vs_ceiling_pct:.1f}%")
        write(f"    headroom            : {report.headroom_vs_ceiling} requests/min")
        if report.over_ceiling:
            write(f"    OVER CEILING        : peak exceeded the safe ceiling by {report.over_ceiling} "
                  "request(s)/min - investigate: the budget should make this impossible")
        write(f"    saturated windows   : {report.saturated_ceiling_windows} "
              f"(anchored windows reaching {report.ceiling_per_minute})")
        write("  note: anchored windows overlap; these are counts of moments at which the rate was that")
        write("        high, not counts of separate periods.")

        if report.endpoints:
            write("")
            write("ENDPOINTS (normalised; identifiers removed)")
            for endpoint, count in report.endpoints:
                write(f"  {endpoint or '-':<32}: {count}")

        write("")
        write("G6 DECISION INPUT")
        if report.sent_attempts == 0:
            write("  No outbound attempt was recorded in this window. That is not evidence of headroom:")
            write("  check that GEMI_REQUEST_METRICS_ENABLED=1 and that a GEMI job actually ran.")
        write(f"  measured peak {report.peak_rolling_60s}/min against the effective capacity of "
              f"{report.effective_capacity_per_minute}/min leaves {report.headroom_vs_capacity} requests/min "
              f"unused ({report.headroom_vs_ceiling} against the theoretical ceiling of "
              f"{report.ceiling_per_minute}/min).")
        write("  G6_STATUS is not decided here: the repository defines no numeric threshold for it, so this")
        write("  command reports facts only. G4 remains NOT STARTED / NOT PASSED and LIVE remains prohibited.")
