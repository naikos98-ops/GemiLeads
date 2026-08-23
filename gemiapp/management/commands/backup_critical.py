"""Export the data that cannot be rebuilt from the ΓΕΜΗ API."""

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from gemiapp.models import (
    CustomerRadar,
    DigestPreference,
    PersonSuppression,
    RadarMatch,
    UserCompanyLead,
    UserSubscription,
)

FORMAT_VERSION = 1


def _plain(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _fields(instance, exclude=()):
    """Every concrete local field except the primary key and the named relations.

    Derived from the model rather than hardcoded so that a field added later is captured
    automatically: a backup that silently omits a new column is worse than no backup.
    """
    skip = set(exclude) | {"id"}
    return {
        f.name: _plain(getattr(instance, f.name))
        for f in instance._meta.local_fields
        if f.name not in skip and not f.is_relation
    }


class Command(BaseCommand):
    help = (
        "Εξάγει τα δεδομένα που ΔΕΝ ανακτώνται από το ΓΕΜΗ (χρήστες, συνδρομές, radars, "
        "leads, εναντιώσεις) σε ένα αρχείο JSON."
    )

    def add_arguments(self, parser):
        parser.add_argument("--output", help="Διαδρομή αρχείου (προεπιλογή: backups/gemileads-critical-<ημερομηνία>.json).")
        # Not named --stdout: call_command always passes a stdout kwarg of its own, which
        # would make this flag permanently true and silently ignore --output.
        parser.add_argument("--print", action="store_true", dest="print_only",
                            help="Εκτύπωση στην έξοδο αντί για αρχείο.")

    def handle(self, *args, **options):
        # Related objects are referenced by natural key, never by primary key. Company and
        # ActivityCode rows are rebuilt from the ΓΕΜΗ API on a restore and get fresh ids, so
        # a pk-based dump would silently reattach every lead to the wrong company.
        payload = {
            "format_version": FORMAT_VERSION,
            "exported_at": timezone.now().isoformat(),
            "note": "Δεν περιλαμβάνει δεδομένα ΓΕΜΗ: ανακτώνται από το API.",
            "users": [
                {**_fields(u, exclude=["password"]),
                 # The hash, not the password. Restoring it lets people keep their existing
                 # credentials instead of every account needing a reset.
                 "password_hash": u.password}
                for u in User.objects.order_by("id")
            ],
            "subscriptions": [
                {"user": s.user.username, **_fields(s)}
                for s in UserSubscription.objects.select_related("user").order_by("id")
            ],
            # DigestPreference.activity_codes is a JSONField and is captured by _fields;
            # only CustomerRadar.activity_codes is a real m2m needing its own lookup.
            "digest_preferences": [
                {"user": p.user.username, **_fields(p)}
                for p in DigestPreference.objects.select_related("user").order_by("id")
            ],
            "radars": [
                {"user": r.user.username,
                 "activity_codes": sorted(r.activity_codes.values_list("code", flat=True)),
                 **_fields(r)}
                for r in CustomerRadar.objects.select_related("user")
                                              .prefetch_related("activity_codes").order_by("id")
            ],
            "leads": [
                {"user": l.user.username, "company": l.company.gemi_number, **_fields(l)}
                for l in UserCompanyLead.objects.select_related("user", "company").order_by("id")
            ],
            "radar_matches": [
                {"radar": m.radar.name, "user": m.radar.user.username,
                 "company": m.company.gemi_number, "lead_user": m.lead.user.username,
                 "lead_company": m.lead.company.gemi_number, **_fields(m)}
                for m in RadarMatch.objects.select_related("radar__user", "company",
                                                           "lead__user", "lead__company").order_by("id")
            ],
            "person_suppressions": [
                {"company": s.company.gemi_number if s.company_id else None, **_fields(s)}
                for s in PersonSuppression.objects.select_related("company").order_by("id")
            ],
        }

        text = json.dumps(payload, ensure_ascii=False, indent=2)

        if options["print_only"]:
            self.stdout.write(text)
            return

        if options["output"]:
            path = Path(options["output"])
        else:
            path = Path("backups") / f"gemileads-critical-{timezone.localdate().isoformat()}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise CommandError(f"Αποτυχία εγγραφής στο {path}: {exc}") from exc

        counts = {k: len(v) for k, v in payload.items() if isinstance(v, list)}
        for name, n in counts.items():
            self.stdout.write(f"  {name:22} {n:>6}")
        size = path.stat().st_size
        self.stdout.write(self.style.SUCCESS(f"\nΑποθηκεύτηκε: {path}  ({size / 1024:.1f} KB)"))
        if not counts["users"]:
            self.stdout.write(self.style.WARNING(
                "Προσοχή: κανένας χρήστης. Σίγουρα τρέχεις σε σωστή βάση;"
            ))
