from datetime import date, timedelta
from django.core.management.base import BaseCommand, CommandError
from gemiapp.services import import_for_date, send_daily_digests


class Command(BaseCommand):
    help = "Εισάγει τις νέες επιχειρήσεις ΓΕΜΗ και στέλνει τα ημερήσια email digests."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="YYYY-MM-DD, προεπιλογή: χθες")
        parser.add_argument("--skip-email", action="store_true")

    def handle(self, *args, **options):
        try:
            target = date.fromisoformat(options["date"]) if options["date"] else date.today() - timedelta(days=1)
            run = import_for_date(target)
            self.stdout.write(self.style.SUCCESS(f"Import: {run.created_count} νέες, {run.updated_count} ενημερωμένες"))
            if not options["skip_email"]:
                sent, skipped = send_daily_digests(target)
                self.stdout.write(self.style.SUCCESS(f"Email: {sent} εστάλησαν, {skipped} παραλείφθηκαν"))
        except Exception as exc:
            raise CommandError(str(exc)) from exc
