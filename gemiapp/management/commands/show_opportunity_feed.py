from django.core.management.base import BaseCommand, CommandError

from gemiapp.models import Organization
from gemiapp.opportunity_feed import DEFAULT_PAGE_SIZE, FeedError, FeedFilters, get_opportunity_feed


class Command(BaseCommand):
    help = (
        "Εσωτερική, μόνο-ανάγνωσης προβολή του feed ευκαιριών (C9) ενός οργανισμού: μία κάρτα ανά εταιρεία, "
        "παγωμένα scores. Μόνο αναγνωριστικά, πόντοι, κωδικοί και χρόνοι· χωρίς ονόματα, επαφές ή payloads."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-id", type=int, required=True, help="Ρητός οργανισμός.")
        parser.add_argument("--limit", type=int, default=DEFAULT_PAGE_SIZE)
        parser.add_argument("--cursor", default=None, help="Cursor της προηγούμενης σελίδας.")
        parser.add_argument("--score-class", action="append", default=[], dest="score_classes")
        parser.add_argument("--status", action="append", default=[], dest="statuses")
        parser.add_argument("--radar-id", action="append", type=int, default=[], dest="radar_ids")
        parser.add_argument("--min-score", type=int, default=None)
        parser.add_argument("--max-score", type=int, default=None)

    def handle(self, *args, **options):
        organization = Organization.objects.filter(pk=options["organization_id"]).first()
        if organization is None:
            raise CommandError("Δεν βρέθηκε οργανισμός με αυτό το id.")
        filters = FeedFilters(
            score_classes=tuple(options["score_classes"]), statuses=tuple(options["statuses"]),
            radar_ids=tuple(options["radar_ids"]), min_score=options["min_score"], max_score=options["max_score"],
        )
        try:
            page = get_opportunity_feed(organization, filters, limit=options["limit"], cursor=options["cursor"])
        except FeedError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"organization=#{page.organization_id} cards={len(page.cards)} limit={page.limit}")
        for card in page.cards:
            self.stdout.write(
                f"company=#{card.company_id} gemi={card.company_gemi_number} score={card.score}/100 "
                f"class={card.score_class} status={card.status} reason={card.primary_reason_code or '-'} "
                f"primary=#{card.primary_opportunity_id} radar=#{card.primary_radar_id} "
                f"opportunities={card.opportunity_count} signals={card.relevant_signal_count} "
                f"detected={card.primary_signal_detected_at.isoformat()} scored_as_of={card.scored_as_of.isoformat()}"
            )
            for child in card.opportunities:
                self.stdout.write(
                    f"  opportunity=#{child.opportunity_id} radar=#{child.radar_id} score={child.score} "
                    f"class={child.score_class} status={child.status} signal={child.latest_signal_type}"
                )
        if page.next_cursor:
            self.stdout.write(f"next={page.next_cursor}")
