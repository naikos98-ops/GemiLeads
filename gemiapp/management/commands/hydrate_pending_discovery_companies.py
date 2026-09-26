from django.core.management.base import BaseCommand, CommandError

from gemiapp.pending_company_hydration import (
    DEFAULT_LIMIT,
    DEFAULT_PACE_SECONDS,
    MAX_LIMIT,
    HydrationDisabled,
    hydrate_pending_companies,
)


class Command(BaseCommand):
    help = (
        "Δημιουργεί (μόνο δημιουργία, ποτέ ενημέρωση) τις εταιρείες που λείπουν τοπικά για ευρήματα του "
        "Discovery v2 που μένουν pending_no_company, με τα ίδια primitives του legacy importer. Ένα επικυρωμένο "
        "αίτημα ΓΕΜΗ ανά εταιρεία, στο κοινό rate budget (lane MONITORED_REFRESH). Δεν παράγει σήματα: μετά "
        "τρέχει το materialize_new_company_signals (πάντα SHADOW). Απαιτεί "
        "GEMI_DISCOVERY_PENDING_COMPANY_HYDRATION_ENABLED=1· δεν είναι προγραμματισμένο."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Καμία εγγραφή στη βάση. ΚΑΝΕΙ αιτήματα ΓΕΜΗ (ένα ανά επιλεγμένη εταιρεία) για να δείξει τι θα γινόταν.",
        )
        parser.add_argument(
            "--limit", type=int, default=DEFAULT_LIMIT,
            help=f"Μέγιστος αριθμός αριθμών ΓΕΜΗ σε αυτή την εκτέλεση (1-{MAX_LIMIT}, προεπιλογή {DEFAULT_LIMIT}).",
        )
        parser.add_argument(
            "--pace-seconds", type=float, default=DEFAULT_PACE_SECONDS,
            help=f"Αναμονή ανάμεσα σε αιτήματα (προεπιλογή {DEFAULT_PACE_SECONDS:g}s, δηλαδή ≤3 αιτήματα/λεπτό).",
        )

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= MAX_LIMIT:
            raise CommandError(f"Το --limit πρέπει να είναι από 1 έως {MAX_LIMIT}.")
        if options["pace_seconds"] < 0:
            raise CommandError("Το --pace-seconds δεν μπορεί να είναι αρνητικό.")
        try:
            report = hydrate_pending_companies(
                limit=options["limit"], dry_run=options["dry_run"], pace_seconds=options["pace_seconds"],
            )
        except HydrationDisabled as exc:
            raise CommandError(str(exc)) from exc
        for line in report.lines():
            self.stdout.write(line)
        if report.failed:
            raise CommandError(
                f"{report.failed} αποτυχίες{' (η εκτέλεση σταμάτησε: ' + report.abort_reason + ')' if report.aborted else ''}. "
                "Καμία μερική εγγραφή δεν έμεινε· οι εταιρείες αυτές παραμένουν εκκρεμείς."
            )
        if report.dry_run:
            self.stdout.write(self.style.WARNING("[dry-run] δεν γράφτηκε τίποτα στη βάση."))
        else:
            self.stdout.write(self.style.SUCCESS("Ολοκληρώθηκε. Επόμενο βήμα: materialize_new_company_signals (SHADOW)."))
