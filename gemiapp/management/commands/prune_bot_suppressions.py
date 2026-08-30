"""Delete OutreachSuppression rows that were created by an email security scanner, not a person.

Background: the outreach unsubscribe view used to opt an address out on a bare GET. Email
security scanners (Outlook Defender Safe Links, corporate proxies, Brevo link pre-fetch) fetch
every link in a delivered message, so they unsubscribed recipients who never touched the link.
The view is now two-step (a POST-only confirm), but the bad rows already written need removing
so those companies become eligible for outreach again.

Heuristic: a scanner hits several links from the same message within a second or two, so
suppressions cluster tightly in time. Rows that arrived within --window seconds of another
suppression are treated as bot-created. Review with --dry-run first; --email keeps specific
addresses (a genuine opt-out caught in a cluster).

    python manage.py prune_bot_suppressions --dry-run
    python manage.py prune_bot_suppressions --window 5 --apply
    python manage.py prune_bot_suppressions --apply --keep real@optout.gr
"""

from datetime import timedelta

from django.core.management.base import BaseCommand

from gemiapp.models import OutreachSuppression


class Command(BaseCommand):
    help = "Καθαρίζει OutreachSuppression rows που δημιουργήθηκαν από email scanners (bulk GET)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Εκτέλεση διαγραφών (χωρίς αυτό, dry-run).")
        parser.add_argument("--dry-run", action="store_true", help="Ρητό dry-run (default).")
        parser.add_argument("--window", type=int, default=5,
                            help="Δευτερόλεπτα: rows εντός αυτού από άλλο row θεωρούνται bot (default 5).")
        parser.add_argument("--keep", nargs="*", default=[],
                            help="Διευθύνσεις που ΔΕΝ διαγράφονται ό,τι κι αν δείχνει το heuristic.")
        parser.add_argument("--before",
                            help="Μόνο rows πριν από αυτή τη μέρα/ώρα (ISO). Το two-step fix ισχύει μετά το deploy.")

    def handle(self, *args, **opts):
        keep = {OutreachSuppression.normalize(e) for e in opts["keep"]}
        window = timedelta(seconds=opts["window"])

        qs = OutreachSuppression.objects.order_by("created_at")
        if opts["before"]:
            from django.utils.dateparse import parse_datetime
            cutoff = parse_datetime(opts["before"])
            if cutoff is None:
                self.stderr.write("Μη έγκυρο --before (χρησιμοποίησε ISO, π.χ. 2026-08-30T12:00).")
                return
            qs = qs.filter(created_at__lt=cutoff)

        rows = list(qs)
        if not rows:
            self.stdout.write("Καμία suppression στο εύρος.")
            return

        # A row is "clustered" if the row before or after it is within `window`.
        clustered = set()
        for i, row in enumerate(rows):
            prev_close = i > 0 and (row.created_at - rows[i - 1].created_at) <= window
            next_close = i < len(rows) - 1 and (rows[i + 1].created_at - row.created_at) <= window
            if prev_close or next_close:
                clustered.add(row.pk)

        to_delete = [r for r in rows if r.pk in clustered and r.email not in keep]
        kept_isolated = [r for r in rows if r.pk not in clustered]

        self.stdout.write(f"Suppressions total: {len(rows)}")
        self.stdout.write(f"Isolated (probably real, kept): {len(kept_isolated)}")
        for r in kept_isolated:
            self.stdout.write(f"  [keep] {r.email}  {r.created_at:%Y-%m-%d %H:%M:%S}")
        self.stdout.write(f"\nClustered (probably bot, to delete): {len(to_delete)}")
        for r in to_delete:
            self.stdout.write(f"  [del]  {r.email}  {r.created_at:%Y-%m-%d %H:%M:%S}")

        if not to_delete:
            self.stdout.write(self.style.SUCCESS("\nΤίποτα να διαγραφεί."))
            return

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING("\ndry-run: καμία αλλαγή. Τρέξε ξανά με --apply."))
            return

        freed_emails = {r.email for r in to_delete}
        ids = [r.pk for r in to_delete]
        n, _ = OutreachSuppression.objects.filter(pk__in=ids).delete()
        self.stdout.write(self.style.SUCCESS(f"\nΔιαγράφηκαν {n} suppressions."))

        # A CompanyOutreach row that failed *because of* one of these suppressions should
        # retry now. Match on the normalised recipient address.
        from gemiapp.models import CompanyOutreach

        stuck = [
            o for o in CompanyOutreach.objects.filter(
                status="failed", error_message__icontains="απεγγραφ"
            )
            if OutreachSuppression.normalize(o.sent_to) in freed_emails
        ]
        if stuck:
            requeued = CompanyOutreach.objects.filter(
                pk__in=[o.pk for o in stuck]
            ).update(status="pending", sent_at=None, error_message="")
            self.stdout.write(self.style.SUCCESS(
                f"{requeued} CompanyOutreach rows → pending (θα σταλούν στο επόμενο drain)."
            ))
            from django_q.tasks import async_task
            async_task(
                "gemiapp.tasks.send_company_outreach_task", [o.company_id for o in stuck]
            )
        else:
            self.stdout.write("Κανένα failed CompanyOutreach row δεν αντιστοιχεί.")
