from django.core.management.base import BaseCommand, CommandError

from gemiapp.existing_user_provisioning import DEFAULT_LIMIT, MAX_LIMIT, provision_existing_user_organizations


class Command(BaseCommand):
    help = ("Gives each legacy Radar owner who belongs to no Organization exactly one Organization with an owner "
            "membership. Explicit operator action only; billing, Radars, leads, Signals and Opportunities are "
            "never touched. Run with --dry-run first.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Classify real users without any database writes.")
        parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                            help=f"Maximum users to examine (1-{MAX_LIMIT}; default {DEFAULT_LIMIT}).")
        parser.add_argument("--user-id", type=int, help="Examine only this user (still a legacy Radar owner).")
        parser.add_argument("--include-staff", action="store_true",
                            help="Also provision staff/superuser accounts that own legacy Radars (skipped by default).")

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= MAX_LIMIT:
            raise CommandError(f"--limit must be between 1 and {MAX_LIMIT}")
        if options["user_id"] is not None and options["user_id"] < 1:
            raise CommandError("--user-id must be a positive integer")
        report = provision_existing_user_organizations(
            dry_run=options["dry_run"], limit=options["limit"], user_id=options["user_id"],
            include_staff=options["include_staff"],
        )
        for line in report.lines():
            self.stdout.write(line)
        if report.dry_run:
            self.stdout.write(self.style.WARNING("[dry-run] zero database writes were performed."))
