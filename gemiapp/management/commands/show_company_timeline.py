from django.core.management.base import BaseCommand, CommandError

from gemiapp.company_signals import LIVE, SHADOW
from gemiapp.company_timeline import ALL_MODES, DEFAULT_LIMIT, TimelineError, get_company_timeline
from gemiapp.models import Company


class Command(BaseCommand):
    help = (
        "Εσωτερική, μόνο-ανάγνωσης προβολή του timeline μιας εταιρείας (B6): σήματα με σειρά ανίχνευσης, "
        "ημερομηνίες και αναγνωριστικά πηγής. Χωρίς ονόματα, στοιχεία επικοινωνίας ή payloads, χωρίς εγγραφές."
    )

    def add_arguments(self, parser):
        parser.add_argument("gemi_number", help="Αριθμός ΓΕΜΗ της εταιρείας.")
        parser.add_argument("--mode", choices=[SHADOW, LIVE, ALL_MODES], default=SHADOW,
                            help="shadow (προεπιλογή), live, ή ρητά all_modes για εσωτερικό debugging.")
        parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Εγγραφές ανά σελίδα.")
        parser.add_argument("--before", default=None, help="Cursor της προηγούμενης σελίδας.")

    def handle(self, *args, **options):
        company = Company.objects.filter(gemi_number=options["gemi_number"]).only("pk", "gemi_number").first()
        if company is None:
            raise CommandError("Δεν βρέθηκε εταιρεία με αυτόν τον αριθμό ΓΕΜΗ.")
        try:
            page = get_company_timeline(company, mode=options["mode"], limit=options["limit"], before=options["before"])
        except TimelineError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"gemi={company.gemi_number} mode={options['mode']} entries={len(page.entries)}")
        for entry in page.entries:
            effective = entry.effective_date or entry.effective_at or "-"
            if entry.subject_kind == "kad":
                subject = f"kad={entry.activity_code}/{entry.kad_version or '-'}"
            elif entry.subject_kind:
                subject = f"{entry.subject_kind}={entry.before_source_id}->{entry.after_source_id}"
            elif entry.discovery_classification:
                subject = f"discovery={entry.discovery_classification}"
            else:
                subject = "-"
            self.stdout.write(
                f"#{entry.signal_id} {entry.detected_at.isoformat()} {entry.signal_type} {entry.mode} "
                f"effective={effective} ({entry.effective_precision}) {subject} group={entry.group_key} "
                f"provenance={entry.provenance_status}"
            )
        if page.next_cursor is not None:
            self.stdout.write(f"next={page.next_cursor.encode()}")
