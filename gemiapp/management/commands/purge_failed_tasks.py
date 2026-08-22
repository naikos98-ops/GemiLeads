from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = (
        "Διαγράφει παλιά αποτυχημένα django-q tasks. Το django-q2 καθαρίζει μόνο τα επιτυχημένα "
        "(save_limit), οπότε τα αποτυχημένα συσσωρεύονται για πάντα και κρατούν το System Health "
        "μόνιμα σε Warning. Τρέξε πρώτα με --dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--older-than",
            type=int,
            default=7,
            help="Διάγραψε μόνο failures παλαιότερα από N ημέρες (προεπιλογή: 7).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Δείξε τι θα διαγραφεί, χωρίς να διαγραφεί τίποτα.",
        )

    def handle(self, *args, **options):
        from django_q.models import Task

        cutoff = timezone.now() - timedelta(days=options["older_than"])
        stale = Task.objects.filter(success=False, started__lt=cutoff)
        recent = Task.objects.filter(success=False, started__gte=cutoff)

        if recent.exists():
            self.stdout.write(
                self.style.WARNING(
                    f"{recent.count()} πρόσφατη/ες αποτυχία/ες (< {options['older_than']} ημερών) "
                    "διατηρούνται· υποδεικνύουν ενεργό πρόβλημα."
                )
            )

        if not stale.exists():
            self.stdout.write(self.style.SUCCESS("Δεν βρέθηκαν παλιά αποτυχημένα tasks."))
            return

        for task in stale.order_by("started")[:20]:
            self.stdout.write(f"  {task.started:%Y-%m-%d %H:%M} {task.func} — {str(task.result)[:90]}")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(f"[dry-run] θα διαγράφονταν {stale.count()} tasks."))
            return

        deleted = stale.delete()[0]
        self.stdout.write(self.style.SUCCESS(f"Διαγράφηκαν {deleted} παλιά αποτυχημένα tasks."))
