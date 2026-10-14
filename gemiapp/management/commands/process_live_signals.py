"""Operator-run, dormant: replay LIVE CompanySignals through the opportunity pipeline (LIVE cutover).

    python manage.py process_live_signals [--since-hours 24] [--limit 100] [--after-id N] [--dry-run]

For work a LIVE cycle could not finish (a pipeline failure, a Radar created later, an entitlement regained). Refuses
unless ``GEMI_LIVE_SIGNALS_ENABLED`` is on; the pipeline itself still skips any LIVE signal created outside every
authorized LIVE period. Bounded (1-1000, id order), idempotent through C8, never changes a signal's mode, sends
nothing. Visibility to customers is a separate flag (``GEMI_LIVE_OPPORTUNITIES_VISIBLE``).
"""

from django.core.management.base import BaseCommand, CommandError

from gemiapp.company_signals import LIVE
from gemiapp.live_cutover import live_signals_enabled
from gemiapp.management.commands.process_shadow_signals import write_replay
from gemiapp.signal_replay import MAX_LIMIT, replay_signals


class Command(BaseCommand):
    help = "Replay LIVE signals through the opportunity pipeline. Refused unless LIVE processing is enabled."

    def add_arguments(self, parser):
        parser.add_argument("--since-hours", type=int, default=24, help="signals detected in the last N hours (>= 1)")
        parser.add_argument("--limit", type=int, default=100, help=f"at most this many signals (1-{MAX_LIMIT})")
        parser.add_argument("--after-id", type=int, default=0, help="only signals with a larger id (paging)")
        parser.add_argument("--dry-run", action="store_true", help="match and score, write nothing")
        parser.add_argument("--verbose-runs", action="store_true", help="print one line per signal")

    def handle(self, *args, **options):
        if not live_signals_enabled():
            raise CommandError("GEMI_LIVE_SIGNALS_ENABLED is off: LIVE signals are not processed.")
        since_hours, limit = options["since_hours"], options["limit"]
        if since_hours < 1:
            raise CommandError("--since-hours must be at least 1.")
        if not 1 <= limit <= MAX_LIMIT:
            raise CommandError(f"--limit must be from 1 to {MAX_LIMIT}.")
        totals = replay_signals(mode=LIVE, since_hours=since_hours, limit=limit, after_id=options["after_id"],
                                dry_run=options["dry_run"])
        write_replay(self, totals, since_hours=since_hours, limit=limit, after_id=options["after_id"],
                     verbose=options["verbose_runs"])
        if totals.selected == limit:
            self.stdout.write(f"More may remain: rerun with --after-id {totals.last_id}.")
