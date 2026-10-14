"""Operator-run: replay SHADOW CompanySignals through the shadow opportunity pipeline (G2 / the G4 observation period).

    python manage.py process_shadow_signals [--since-hours 24] [--limit 100] [--after-id N] [--dry-run]

Selects SHADOW signals detected in the window in id order (deterministic, and consistent with the ``--after-id``
cursor used to page through a backlog), at most ``--limit`` (1-1000), and runs each through
``gemiapp.opportunity_pipeline.process_company_signal`` with one ``as_of`` for the whole run. Safe to rerun: C8 is
idempotent. It never touches a LIVE signal, never changes a signal's mode, and never shows anything to a customer --
every customer surface reads LIVE-backed opportunities only. ``--dry-run`` matches and scores but writes nothing.
"""

from django.core.management.base import BaseCommand, CommandError

from gemiapp.company_signals import SHADOW
from gemiapp.signal_replay import MAX_LIMIT, replay_signals


class Command(BaseCommand):
    help = "Replay SHADOW signals through the shadow opportunity pipeline. Bounded, ordered, idempotent."

    def add_arguments(self, parser):
        parser.add_argument("--since-hours", type=int, default=24, help="signals detected in the last N hours (>= 1)")
        parser.add_argument("--limit", type=int, default=100, help=f"at most this many signals (1-{MAX_LIMIT})")
        parser.add_argument("--after-id", type=int, default=0, help="only signals with a larger id (paging)")
        parser.add_argument("--dry-run", action="store_true", help="match and score, write nothing")
        parser.add_argument("--verbose-runs", action="store_true", help="print one line per signal")

    def handle(self, *args, **options):
        since_hours, limit = options["since_hours"], options["limit"]
        if since_hours < 1:
            raise CommandError("--since-hours must be at least 1.")
        if not 1 <= limit <= MAX_LIMIT:
            raise CommandError(f"--limit must be from 1 to {MAX_LIMIT}.")
        # SHADOW only, always: this command never selects or processes a LIVE signal.
        totals = replay_signals(mode=SHADOW, since_hours=since_hours, limit=limit, after_id=options["after_id"],
                                dry_run=options["dry_run"])
        write_replay(self, totals, since_hours=since_hours, limit=limit, after_id=options["after_id"],
                     verbose=options["verbose_runs"])
        self.stdout.write("SHADOW only: no signal mode changed; nothing is visible to customers (LIVE-only surfaces).")
        if totals.selected == limit:
            self.stdout.write(f"More may remain: rerun with --after-id {totals.last_id}.")


def write_replay(command, totals, *, since_hours, limit, after_id, verbose):
    for run in totals.runs:
        if verbose or run.errors:
            command.stdout.write(run.line())
    prefix = "[dry-run] " if totals.dry_run else ""
    counters = totals.counters
    command.stdout.write(command.style.SUCCESS(
        f"{prefix}{totals.mode} signals selected={totals.selected} processed={totals.processed} "
        f"failed={totals.failed} errors={totals.errors} "
        f"(window {since_hours}h, limit {limit}, after id {after_id}, last id {totals.last_id or '-'})"))
    command.stdout.write(f"{prefix}radars considered={counters['considered']} "
                         f"entitled={counters['entitled_considered']} "
                         f"skipped_not_entitled={counters['skipped_not_entitled']} matched={counters['matched']} "
                         f"insufficient_state={counters['insufficient_state']} no_match={counters['no_match']}")
    command.stdout.write(f"{prefix}opportunities created={counters['created']} updated={counters['updated']} "
                         f"unchanged={counters['unchanged']} below_threshold={counters['below_threshold']} "
                         f"skipped_live_backed={counters['skipped_live_backed']}")
