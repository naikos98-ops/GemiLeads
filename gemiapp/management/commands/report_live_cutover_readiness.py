"""Read-only: is the LIVE cutover ready? (gemiapp.live_cutover_readiness)

    python manage.py report_live_cutover_readiness [--replay-hours 168]

Prints one PASS / BLOCKED / INFO line per check and ``READINESS: READY`` or ``READINESS: BLOCKED``; exits non-zero
when BLOCKED. Writes nothing (one rolled-back transaction) and makes no GEMI request. It never enables anything.
"""

from django.core.management.base import BaseCommand, CommandError

from gemiapp.live_cutover_readiness import DEFAULT_REPLAY_HOURS, build_readiness_report


class Command(BaseCommand):
    help = "Read-only LIVE cutover readiness report: PASS/BLOCKED per check and one verdict."

    def add_arguments(self, parser):
        parser.add_argument("--replay-hours", type=int, default=DEFAULT_REPLAY_HOURS,
                            help="window of the SHADOW replay dry run (>= 1)")

    def handle(self, *args, **options):
        if options["replay_hours"] < 1:
            raise CommandError("--replay-hours must be at least 1.")
        report = build_readiness_report(replay_hours=options["replay_hours"])
        for line in report.lines():
            self.stdout.write(line)
        if report.blocking:
            raise CommandError(f"LIVE cutover readiness BLOCKED by {len(report.blocking)} check(s).")
