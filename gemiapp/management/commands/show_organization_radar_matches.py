from django.core.management.base import BaseCommand, CommandError

from gemiapp.company_signals import LIVE, SHADOW
from gemiapp.models import CompanySignal
from gemiapp.organization_radar_matching import (
    ORGANIZATION_RADAR_MATCH_RULE_VERSION,
    MatchingError,
    explain_organization_radar_matches,
    summarize_organization_radar_matching,
)


class Command(BaseCommand):
    help = (
        "Εσωτερική, μόνο-ανάγνωσης προβολή του matching των Organization Radars (C5): ποια ενεργά Radars ταιριάζουν "
        "σε ένα σήμα. Μόνο αναγνωριστικά και καταστάσεις· χωρίς ονόματα, επαφές ή payloads, χωρίς εγγραφές."
    )

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--signal-id", type=int, help="Ένα συγκεκριμένο CompanySignal.")
        target.add_argument("--mode", choices=[SHADOW, LIVE],
                            help="Σύνοψη για τα πιο πρόσφατα σήματα ενός ρητού mode (ποτέ μαζί).")
        parser.add_argument("--limit", type=int, default=100, help="Πλήθος σημάτων για τη σύνοψη.")

    def handle(self, *args, **options):
        try:
            if options["mode"]:
                summary = summarize_organization_radar_matching(mode=options["mode"], limit=options["limit"])
                self.stdout.write(
                    f"mode={summary.mode} rule={ORGANIZATION_RADAR_MATCH_RULE_VERSION} signals={summary.signals} "
                    f"candidates={summary.candidates} match={summary.matches} no_match={summary.no_matches} "
                    f"insufficient_state={summary.insufficient_state}"
                )
                for status, count in summary.context_statuses:
                    self.stdout.write(f"context {status}={count}")
                return
            signal = CompanySignal.objects.filter(pk=options["signal_id"]).only("pk").first()
            if signal is None:
                raise CommandError("Δεν βρέθηκε σήμα με αυτό το id.")
            report = explain_organization_radar_matches(signal)
        except MatchingError as exc:
            raise CommandError(str(exc)) from exc
        context = report.context
        self.stdout.write(
            f"signal=#{context.signal_id} type={context.signal_type} source={context.source_type} "
            f"company=#{context.company_id} context={context.context_status} "
            f"snapshot={context.state_snapshot_id or '-'} rule={ORGANIZATION_RADAR_MATCH_RULE_VERSION} "
            f"candidates={report.candidate_count}"
        )
        for item in report.evaluations:
            evaluation = item.evaluation
            self.stdout.write(
                f"radar=#{item.radar_id} organization=#{item.organization_id} result={evaluation.status} "
                f"matched={','.join(evaluation.matched_dimensions) or '-'} "
                f"failed={','.join(evaluation.failed_dimensions) or '-'} "
                f"unknown={','.join(evaluation.unknown_dimensions + evaluation.unknown_exclusions) or '-'} "
                f"excluded_by={evaluation.exclusion_hit or '-'}"
            )
