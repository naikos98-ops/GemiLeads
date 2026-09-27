from django.core.management.base import BaseCommand, CommandError

from gemiapp.legacy_radar_sync import TransitionOff, resync_legacy_radars


class Command(BaseCommand):
    help = ("G4 transition: bring every legacy Radar's OrganizationRadar in line with it (existing drift from before "
            "the mirror was deployed). Dry run by default; --apply writes, one transaction per Radar, through the "
            "same mirror the Radar editor uses. Provenance only, never names; tombstones are never recreated. "
            "No Signal, Opportunity or billing write. Refuses after the LIVE cutover.")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Perform the planned changes (default: report only).")
        parser.add_argument("--user-id", type=int, help="Only this user's legacy Radars.")

    def handle(self, *args, **options):
        if options["user_id"] is not None and options["user_id"] < 1:
            raise CommandError("--user-id must be a positive integer")
        try:
            counters = resync_legacy_radars(apply=options["apply"], user_id=options["user_id"])
        except TransitionOff as off:
            raise CommandError(str(off)) from off
        self.stdout.write(f"mode={'apply' if options['apply'] else 'dry-run'}")
        for name, value in counters.items():
            self.stdout.write(f"{name}={value}")
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("[dry-run] zero database writes were performed. Re-run with --apply."))
        if counters["errors"]:
            raise CommandError(f"{counters['errors']} legacy Radar(s) failed; nothing half-written. See the log.")
