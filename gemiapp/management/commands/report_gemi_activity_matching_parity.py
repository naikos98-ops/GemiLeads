from django.core.management.base import BaseCommand

from gemiapp.ingestion.activities import matching_parity, resolve_current_activities_only


def _pct(legacy, current_only):
    if not legacy:
        return "n/a" if current_only else "+0.00%"
    return f"{(current_only - legacy) * 100 / legacy:+.2f}%"


class Command(BaseCommand):
    help = (
        "Συγκρίνει, για κάθε Radar, το σημερινό matching ΚΑΔ με το matching μόνο σε τρέχουσες δραστηριότητες "
        "(GEMI_MATCH_CURRENT_ACTIVITIES_ONLY). Μόνο αναφορά: δεν αλλάζει τίποτα. Εμφανίζει ids και πλήθη, "
        "ποτέ επωνυμίες ή προσωπικά δεδομένα."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--list-company-ids", action="store_true",
            help="Εμφανίζει και τα ids των εταιρειών που αφαιρούνται ή προστίθενται.",
        )

    def handle(self, *args, **options):
        flag = "1" if resolve_current_activities_only() else "0"
        self.stdout.write(f"GEMI_MATCH_CURRENT_ACTIVITIES_ONLY={flag} (η αναφορά δεν αλλάζει το live matching)")
        results = matching_parity()
        removed_companies, added_companies = set(), set()
        for result in results:
            self.stdout.write(
                f"radar={result.radar_id} active={'yes' if result.is_active else 'no'} kad_codes={result.activity_codes} "
                f"legacy={result.legacy} current_only={result.current_only} removed={len(result.removed)} "
                f"added={len(result.added)} change={_pct(result.legacy, result.current_only)} "
                f"existing_matches={result.existing_matches} "
                f"existing_matches_not_current_only={result.existing_matches_not_current_only}"
            )
            if result.removed_reasons:
                self.stdout.write("  removed reasons: " + " ".join(f"{k}={v}" for k, v in sorted(result.removed_reasons.items())))
            if result.added_reasons:
                self.stdout.write("  added reasons: " + " ".join(f"{k}={v}" for k, v in sorted(result.added_reasons.items())))
            if options["list_company_ids"]:
                self.stdout.write(f"  removed company ids: {result.removed}")
                self.stdout.write(f"  added company ids: {result.added}")
            removed_companies.update(result.removed)
            added_companies.update(result.added)
        legacy = sum(result.legacy for result in results)
        current_only = sum(result.current_only for result in results)
        self.stdout.write(
            f"total radars={len(results)} affected={sum(result.affected for result in results)} "
            f"legacy_matches={legacy} current_only_matches={current_only} "
            f"removed_matches={sum(len(result.removed) for result in results)} "
            f"added_matches={sum(len(result.added) for result in results)} change={_pct(legacy, current_only)} "
            f"distinct_companies_removed={len(removed_companies)} distinct_companies_added={len(added_companies)}"
        )
