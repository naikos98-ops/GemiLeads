"""Operator-run: replay SHADOW CompanySignals through the shadow opportunity pipeline (G2 / the G4 observation period).

    python manage.py process_shadow_signals [--since-hours 24] [--limit 100] [--after-id N] [--dry-run]

Selects SHADOW signals detected in the window in id order (deterministic, and consistent with the ``--after-id``
cursor used to page through a backlog), at most ``--limit`` (1-1000), and runs each through
``gemiapp.opportunity_pipeline.process_company_signal`` with one ``as_of`` for the whole run. Safe to rerun: C8 is
idempotent. It never touches a LIVE signal, never changes a signal's mode, and never shows anything to a customer --
every customer surface reads LIVE-backed opportunities only. ``--dry-run`` matches and scores but writes nothing.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from gemiapp.company_signals import SHADOW
from gemiapp.models import CompanySignal
from gemiapp.opportunity_pipeline import process_company_signal

MAX_LIMIT = 1000
COUNTERS = ("considered", "entitled_considered", "skipped_not_entitled", "matched", "insufficient_state", "no_match",
            "created", "updated", "unchanged", "below_threshold", "skipped_live_backed")


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
        as_of = timezone.now()
        signals = list(CompanySignal.objects.filter(mode=SHADOW, detected_at__gte=as_of - timedelta(hours=since_hours),
                                                    detected_at__lte=as_of, pk__gt=options["after_id"])
                       .order_by("pk")[:limit])
        totals = dict.fromkeys(COUNTERS, 0)
        processed = failed = errors = 0
        for signal in signals:
            run = process_company_signal(signal, as_of=as_of, dry_run=options["dry_run"])
            processed += int(run.processed)
            failed += int(bool(run.skipped_reason))
            errors += len(run.errors)
            for name in COUNTERS:
                totals[name] += getattr(run, name)
            if options["verbose_runs"] or run.errors:
                self.stdout.write(run.line())
        prefix = "[dry-run] " if options["dry_run"] else ""
        last = signals[-1].pk if signals else None
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}shadow signals selected={len(signals)} processed={processed} failed={failed} errors={errors} "
            f"(window {since_hours}h, limit {limit}, after id {options['after_id']}, last id {last or '-'})"))
        self.stdout.write(f"{prefix}radars considered={totals['considered']} entitled={totals['entitled_considered']} "
                          f"skipped_not_entitled={totals['skipped_not_entitled']} matched={totals['matched']} "
                          f"insufficient_state={totals['insufficient_state']} no_match={totals['no_match']}")
        self.stdout.write(f"{prefix}opportunities created={totals['created']} updated={totals['updated']} "
                          f"unchanged={totals['unchanged']} below_threshold={totals['below_threshold']} "
                          f"skipped_live_backed={totals['skipped_live_backed']}")
        self.stdout.write("SHADOW only: no signal mode changed; nothing is visible to customers (LIVE-only surfaces).")
        if len(signals) == limit:
            self.stdout.write(f"More may remain: rerun with --after-id {last}.")
