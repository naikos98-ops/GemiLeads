"""Operator-run: the deterministic SHADOW cycle of the G4 observation period.

    python manage.py run_g4_shadow_cycle [--dry-run] [--compare-date YYYY-MM-DD]
                                         [--replay-hours N [--replay-limit N]] [--verbose]

Discovery v2 (shadow) -> NEW_COMPANY materialisation -> the after-commit opportunity pipeline (observed, not
repeated) -> optional bounded replay -> one summary with the G4 metrics. The sequence, the safety precheck and
the reasoning live in ``gemiapp.g4_shadow_cycle``. Deliberately **not** in ``apps.SCHEDULES``: starting the
14-day observation is an explicit decision, and nothing here enables LIVE or the Discovery cutover.
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from gemiapp.g4_shadow_cycle import (
    DEFAULT_REPLAY_LIMIT,
    MAX_REPLAY_LIMIT,
    ShadowCycleRefused,
    run_g4_shadow_cycle,
)


class Command(BaseCommand):
    help = (
        "Ο ντετερμινιστικός κύκλος SHADOW του G4: Discovery v2 (shadow), υλοποίηση NEW_COMPANY, το pipeline "
        "ευκαιριών που τρέχει ήδη μετά το commit (καταγράφεται, δεν επαναλαμβάνεται), προαιρετική φραγμένη "
        "επανεκτέλεση, και μία αναφορά με τις μετρικές G4. Μόνο SHADOW: δεν ενεργοποιεί LIVE, δεν κάνει ingest, δεν "
        "δημιουργεί εταιρείες και δεν δείχνει τίποτα σε πελάτη. Μόνο το Discovery κάνει αιτήματα ΓΕΜΗ."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Χωρίς καμία εγγραφή (το Discovery κάνει όμως τα αιτήματά του).")
        parser.add_argument("--compare-date", metavar="YYYY-MM-DD",
                            help="Αναφορά σύγκρισης legacy/v2 για αυτή την ημέρα (μόνο ανάγνωση).")
        parser.add_argument("--replay-hours", type=int,
                            help="Φραγμένη επανεκτέλεση σημάτων SHADOW των τελευταίων N ωρών (εκτός όσων μόλις "
                                 "επεξεργάστηκαν). Χωρίς αυτό δεν γίνεται επανεκτέλεση.")
        parser.add_argument("--replay-limit", type=int, default=DEFAULT_REPLAY_LIMIT,
                            help=f"Ανώτατο πλήθος σημάτων στην επανεκτέλεση (1-{MAX_REPLAY_LIMIT}).")
        parser.add_argument("--verbose", action="store_true", help="Μία γραμμή ανά σήμα του pipeline.")

    def handle(self, *args, **options):
        compare_date = None
        if options["compare_date"]:
            try:
                compare_date = date.fromisoformat(options["compare_date"])
            except ValueError as exc:
                raise CommandError("Το --compare-date θέλει ημερομηνία YYYY-MM-DD.") from exc
        if options["replay_hours"] is not None and options["replay_hours"] < 1:
            raise CommandError("Το --replay-hours πρέπει να είναι τουλάχιστον 1.")
        if not 1 <= options["replay_limit"] <= MAX_REPLAY_LIMIT:
            raise CommandError(f"Το --replay-limit πρέπει να είναι από 1 έως {MAX_REPLAY_LIMIT}.")

        try:
            report = run_g4_shadow_cycle(
                dry_run=options["dry_run"], compare_date=compare_date,
                replay_hours=options["replay_hours"], replay_limit=options["replay_limit"],
            )
        except ShadowCycleRefused as exc:
            raise CommandError(f"Ο κύκλος σταμάτησε πριν από οποιαδήποτε εγγραφή ή αίτημα ΓΕΜΗ — {exc}") from exc
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        for line in report.lines():
            self.stdout.write(line)
        if options["verbose"]:
            for label, runs in (("pipeline", report.pipeline_runs), ("replay", report.replay_runs)):
                for run in runs:
                    self.stdout.write(f"  [{label}] {run.line()}")
        if report.materialisation is not None and report.materialisation.conflicts:
            self.stdout.write(self.style.ERROR(
                f"{report.materialisation.conflicts} συγκρούσεις: δεν επικαλύφθηκε κανένα υπάρχον γεγονός."))
        if report.dry_run:
            self.stdout.write(self.style.WARNING("[dry-run] δεν γράφτηκε τίποτα."))
        if report.failed_phases:
            raise CommandError(f"Απέτυχαν φάσεις: {', '.join(report.failed_phases)} "
                               f"(η αναφορά πιο πάνω δείχνει τι ολοκληρώθηκε· τίποτα δεν αναιρέθηκε).")
        self.stdout.write(self.style.SUCCESS(
            f"Ο κύκλος ολοκληρώθηκε (shadow). Αιτήματα ΓΕΜΗ: {report.gemi_requests}."))
