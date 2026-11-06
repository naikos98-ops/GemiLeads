"""The daily lifecycle-email job (also scheduled as send_lifecycle_emails_task at 09:30).

    python manage.py send_lifecycle_emails --dry-run   # who would get what; writes and sends nothing
    python manage.py send_lifecycle_emails             # requires LIFECYCLE_EMAILS_ENABLED=1

The first real run only after the dry-run report has been shown to the Founder.
"""

from django.core.management.base import BaseCommand, CommandError

from gemiapp.lifecycle import lifecycle_enabled, lifecycle_start_at, run_daily


class Command(BaseCommand):
    help = "Reserve time-based lifecycle emails and send everything due."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report only: no row written, no email sent.")

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        if not dry_run and not lifecycle_enabled():
            raise CommandError("LIFECYCLE_EMAILS_ENABLED=0: refusing to send. Use --dry-run for the report.")
        self.stdout.write(f"LIFECYCLE_EMAILS_ENABLED={int(lifecycle_enabled())} · LIFECYCLE_START_AT={lifecycle_start_at()}")
        report = run_daily(dry_run=dry_run)
        for line in report.lines:
            self.stdout.write(line)
        if not dry_run:
            self.stdout.write(f"sent={report.sent} skipped={report.skipped} failed={report.failed} "
                              f"deferred={report.deferred} reserved={report.reserved}")
