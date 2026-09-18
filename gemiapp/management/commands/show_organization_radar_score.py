from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from gemiapp.models import CompanySignal
from gemiapp.opportunity_scoring import (
    OPPORTUNITY_SCORE_RULE_VERSION,
    ScoringError,
    score_matching_organization_radars,
)
from gemiapp.organization_radar_matching import MatchingError


class Command(BaseCommand):
    help = (
        "Εσωτερική, μόνο-ανάγνωσης προβολή του scoring (C6): βαθμολογεί τα επιβεβαιωμένα matches ενός σήματος σε "
        "ρητό χρόνο. Μόνο αναγνωριστικά και πόντοι· χωρίς ονόματα, επαφές ή payloads, χωρίς εγγραφές."
    )

    def add_arguments(self, parser):
        parser.add_argument("--signal-id", type=int, required=True, help="Ένα υπαρκτό CompanySignal.")
        parser.add_argument("--as-of", required=True, help="Ρητή χρονική στιγμή ISO-8601 με ζώνη ώρας.")
        parser.add_argument("--radar-id", type=int, default=None, help="Προαιρετικό φίλτρο σε ένα Radar.")

    def handle(self, *args, **options):
        as_of = parse_datetime(options["as_of"])
        if as_of is None or as_of.tzinfo is None:
            raise CommandError("Το --as-of πρέπει να είναι ISO-8601 με ζώνη ώρας (π.χ. 2026-09-18T09:00:00+03:00).")
        signal = CompanySignal.objects.filter(pk=options["signal_id"]).only("pk").first()
        if signal is None:
            raise CommandError("Δεν βρέθηκε σήμα με αυτό το id.")
        try:
            scores = score_matching_organization_radars(signal, as_of=as_of)
        except (ScoringError, MatchingError) as exc:
            raise CommandError(str(exc)) from exc
        if options["radar_id"] is not None:
            scores = tuple(score for score in scores if score.radar_id == options["radar_id"])
        self.stdout.write(
            f"signal=#{options['signal_id']} as_of={as_of.isoformat()} rule={OPPORTUNITY_SCORE_RULE_VERSION} "
            f"scored={len(scores)}"
        )
        for score in scores:
            components = " ".join(f"{name}={points}" for name, points in score.component_points)
            self.stdout.write(
                f"radar=#{score.radar_id} organization=#{score.organization_id} company=#{score.company_id} "
                f"score={score.score}/100 class={score.score_class} {components}"
            )
