from django.core.management.base import BaseCommand, CommandError

from gemiapp.legacy_radar_migration import DEFAULT_LIMIT, MAX_LIMIT, migrate_legacy_radars


class Command(BaseCommand):
    help = ("Copies eligible legacy CustomerRadar definitions into OrganizationRadar with durable provenance. "
            "Explicit operator action only; no history, Signals or Opportunities are created.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Classify real rows without any database writes.")
        parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                            help=f"Maximum legacy Radars to examine (1-{MAX_LIMIT}; default {DEFAULT_LIMIT}).")
        parser.add_argument("--user-id", type=int)
        parser.add_argument("--organization-id", type=int)
        parser.add_argument("--legacy-radar-id", type=int)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= MAX_LIMIT:
            raise CommandError(f"--limit must be between 1 and {MAX_LIMIT}")
        for option in ("user_id", "organization_id", "legacy_radar_id"):
            if options[option] is not None and options[option] < 1:
                raise CommandError(f"--{option.replace('_', '-')} must be a positive integer")
        report = migrate_legacy_radars(
            dry_run=options["dry_run"], limit=options["limit"], user_id=options["user_id"],
            organization_id=options["organization_id"], legacy_radar_id=options["legacy_radar_id"],
        )
        for line in report.lines():
            self.stdout.write(line)
        if report.dry_run:
            self.stdout.write(self.style.WARNING("[dry-run] zero database writes were performed."))
