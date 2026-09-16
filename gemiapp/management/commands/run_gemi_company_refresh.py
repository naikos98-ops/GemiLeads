from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from gemiapp.ingestion.refresh import (
    RefreshPlanError,
    RefreshPolicy,
    build_company_refresh_plan,
    policy_from_settings,
    run_company_refresh,
)


class Command(BaseCommand):
    help = (
        "Ανανεώνει τις παρακολουθούμενες εταιρείες (B4): επιλέγει όσες είναι due κατά το A9, φτιάχνει "
        "οικονομικό πλάνο αιτημάτων, φέρνει φρέσκα δεδομένα ΓΕΜΗ μέσω του κοινού client και αποθηκεύει "
        "snapshot (B3). Δεν παράγει κανένα σήμα και δεν αγγίζει Radars, matching ή digests. Με --plan-only "
        "δεν γίνεται κανένα αίτημα και δεν γράφεται τίποτα."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--plan-only", action="store_true",
            help="Μόνο το πλάνο: κανένα αίτημα ΓΕΜΗ, καμία εγγραφή, καμία αλλαγή monitoring.",
        )
        parser.add_argument(
            "--run-at", metavar="ISO8601",
            help="Η χρονική στιγμή επιλογής των due εταιρειών (προεπιλογή: τώρα). Πρέπει να έχει ζώνη ώρας.",
        )
        parser.add_argument("--max-requests", type=int, help="Όριο αιτημάτων για αυτή την εκτέλεση.")
        parser.add_argument("--max-pages", type=int, help="Όριο σελίδων ανά αναζήτηση.")
        parser.add_argument("--max-direct-details", type=int, help="Όριο απευθείας αιτημάτων ανά εταιρεία.")

    def handle(self, *args, **options):
        run_at = self._run_at(options.get("run_at"))
        policy = self._policy(options)
        try:
            if options["plan_only"]:
                plan = build_company_refresh_plan(run_at=run_at, policy=policy)
                for line in plan.lines():
                    self.stdout.write(line)
                self.stdout.write(self.style.WARNING(
                    "[plan-only] κανένα αίτημα ΓΕΜΗ, κανένα snapshot, καμία αλλαγή monitoring."
                ))
                return
            result = run_company_refresh(run_at=run_at, policy=policy)
        except RefreshPlanError as exc:
            raise CommandError(str(exc)) from exc
        for line in result.lines():
            self.stdout.write(line)

    def _run_at(self, value):
        if not value:
            return timezone.now()
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise CommandError("Το --run-at θέλει ISO-8601 ημερομηνία/ώρα με ζώνη ώρας.") from exc
        if timezone.is_naive(parsed):
            raise CommandError("Το --run-at πρέπει να έχει ζώνη ώρας.")
        return parsed

    def _policy(self, options):
        # Overrides may only make a run smaller: the command never raises a cap above its setting, and
        # exposes nothing else -- no raw query, no cursor, no tenant and no signal switch.
        base = policy_from_settings()
        values = {}
        for option, name in (
            ("max_requests", "max_requests_per_run"),
            ("max_pages", "max_pages_per_query"),
            ("max_direct_details", "max_direct_details_per_run"),
        ):
            value = options.get(option)
            if value is None:
                continue
            if value < 1:
                raise CommandError(f"Το --{option.replace('_', '-')} πρέπει να είναι τουλάχιστον 1.")
            values[name] = min(value, getattr(base, name))
        return RefreshPolicy(**{**base.__dict__, **values})
