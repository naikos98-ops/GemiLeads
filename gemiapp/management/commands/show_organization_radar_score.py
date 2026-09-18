from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from gemiapp.models import CompanySignal
from gemiapp.opportunity_score_breakdown import ScoreBreakdownError, explain_opportunity_scores
from gemiapp.opportunity_scoring import (
    OPPORTUNITY_SCORE_RULE_VERSION,
    ScoringError,
    score_matching_organization_radars,
)
from gemiapp.organization_radar_matching import MatchingError


def _evidence(component):
    """Stable, canonical evidence ids only -- never a name, address or contact detail."""
    parts = []
    for item in component.evidence:
        kind = type(item).__name__
        if kind == "KadEvidence":
            parts.append(f"{item.code}/{item.kad_version or '-'}")
        elif kind == "RegionEvidence":
            parts.append(f"{item.level}:{item.source_id}")
        elif kind == "LegalFormEvidence":
            parts.append(f"legal_type:{item.source_id}")
        elif kind == "SignalTypeEvidence":
            parts.append(item.signal_type)
        else:  # FreshnessEvidence
            parts.append(f"age_seconds={item.age_seconds} detected_at={item.detected_at.isoformat()}")
    return ",".join(parts) or "-"


class Command(BaseCommand):
    help = (
        "Εσωτερική, μόνο-ανάγνωσης προβολή του scoring (C6) και της ανάλυσής του (C7): βαθμολογεί τα επιβεβαιωμένα "
        "matches ενός σήματος σε ρητό χρόνο. Μόνο αναγνωριστικά, πόντοι και κωδικοί αιτιολογίας· χωρίς ονόματα, "
        "επαφές ή payloads, χωρίς εγγραφές."
    )

    def add_arguments(self, parser):
        parser.add_argument("--signal-id", type=int, required=True, help="Ένα υπαρκτό CompanySignal.")
        parser.add_argument("--as-of", required=True, help="Ρητή χρονική στιγμή ISO-8601 με ζώνη ώρας.")
        parser.add_argument("--radar-id", type=int, default=None, help="Προαιρετικό φίλτρο σε ένα Radar.")
        parser.add_argument("--breakdown", action="store_true",
                            help="Ανάλυση ανά συστατικό (C7): πόντοι, κωδικός αιτιολογίας και κανονικά στοιχεία.")

    def handle(self, *args, **options):
        as_of = parse_datetime(options["as_of"])
        if as_of is None or as_of.tzinfo is None:
            raise CommandError("Το --as-of πρέπει να είναι ISO-8601 με ζώνη ώρας (π.χ. 2026-09-18T09:00:00+03:00).")
        signal = CompanySignal.objects.filter(pk=options["signal_id"]).only("pk").first()
        if signal is None:
            raise CommandError("Δεν βρέθηκε σήμα με αυτό το id.")
        try:
            results = (explain_opportunity_scores(signal, as_of=as_of) if options["breakdown"]
                       else score_matching_organization_radars(signal, as_of=as_of))
        except (ScoringError, ScoreBreakdownError, MatchingError) as exc:
            raise CommandError(str(exc)) from exc
        if options["radar_id"] is not None:
            results = tuple(result for result in results if result.radar_id == options["radar_id"])
        self.stdout.write(
            f"signal=#{options['signal_id']} as_of={as_of.isoformat()} rule={OPPORTUNITY_SCORE_RULE_VERSION} "
            f"scored={len(results)}"
        )
        for result in results:
            self.stdout.write(
                f"radar=#{result.radar_id} organization=#{result.organization_id} company=#{result.company_id} "
                f"score={result.score}/100 class={result.score_class}"
                + ("" if options["breakdown"]
                   else " " + " ".join(f"{name}={points}" for name, points in result.component_points))
            )
            for component in getattr(result, "components", ()):
                self.stdout.write(
                    f"  {component.code}: {component.awarded_points}/{component.max_points} "
                    f"{component.status} {component.reason_code} [{_evidence(component)}]"
                )
