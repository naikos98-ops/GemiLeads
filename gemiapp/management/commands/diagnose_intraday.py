"""One-shot answer to "why did no intraday email go out at 11:00?".

Every previous investigation of a missing digest needed several ad-hoc shell queries across
schedules, import runs, subscriptions and delivery rows, and the answer depended on which of them
the operator happened to run. This gathers all of them in the order the pipeline actually visits
them, so the first section that reads FAIL is the cause.

Read-only: it sends nothing, writes nothing, and touches no external service.
"""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from gemiapp.models import Company, DigestDelivery, ImportRun
from gemiapp.services import TOP_TIERS, digest_skip_reason

# The cron in apps.py. Duplicated as data here on purpose: if the two ever disagree, the report
# says so rather than silently reporting against the wrong schedule.
EXPECTED_INTRADAY_HOURS = [8, 11, 14, 17, 20, 23]


class Command(BaseCommand):
    help = (
        "Διαγνωστικό για τα 3ωρα (intraday) email: ποιος δικαιούται, τι έτρεξε και τι στάλθηκε. "
        "Δεν στέλνει και δεν αλλάζει τίποτα."
    )

    def add_arguments(self, parser):
        parser.add_argument("--date", help="YYYY-MM-DD, προεπιλογή: σήμερα (ώρα Ελλάδας).")

    def handle(self, *args, **options):
        from datetime import date as date_cls

        target = date_cls.fromisoformat(options["date"]) if options.get("date") else timezone.localdate()
        now = timezone.localtime()

        self._section("ΠΕΡΙΒΑΛΛΟΝ")
        self._line("Ημερομηνία αναφοράς", target.isoformat())
        self._line("Τοπική ώρα τώρα", now.strftime("%Y-%m-%d %H:%M:%S %Z"))
        self._line("BETA_MODE", getattr(settings, "BETA_MODE", None))
        self._line("LEGAL_BILLING_ACTIVE", settings.LEGAL_BILLING_ACTIVE)
        self._line("GEMI_API_KEY", "ορισμένο" if settings.GEMI_API_KEY else "ΛΕΙΠΕΙ")
        self._line("SMTP host/user", f"{settings.EMAIL_HOST} / {'ορισμένο' if settings.EMAIL_HOST_USER else 'ΛΕΙΠΕΙ'}")
        self._line("DEFAULT_FROM_EMAIL", settings.DEFAULT_FROM_EMAIL)

        self._schedules(now)
        self._runs(target, now)
        self._recipients(target)
        self._deliveries(target)

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            "Διάβασε τις ενότητες με τη σειρά. Η πρώτη που δείχνει πρόβλημα είναι η αιτία: "
            "χωρίς schedule δεν τρέχει τίποτα· χωρίς ImportRun δεν μπήκαν εγγραφές· "
            "χωρίς παραλήπτη enterprise/custom δεν στέλνεται 3ωρο email σε κανέναν."
        ))
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            "ΓΙΑ ΕΝΑ ΜΕΜΟΝΩΜΕΝΟ SLOT ΠΟΥ ΔΕΝ ΕΣΤΕΙΛΕ, δες τη γραμμή του στην ενότητα 2:\n"
            "  · λείπει η γραμμή          -> το task δεν έτρεξε (scheduler/worker)\n"
            "  · status=failed            -> έσκασε το import, το μήνυμα είναι δίπλα\n"
            "  · status=running & παλιά   -> σκοτώθηκε στο timeout, δεν έφτασε ποτέ στην αποστολή\n"
            "  · status=success & new=0   -> το ΓΕΜΗ δεν είχε νέα εγγραφή. Η σιωπή είναι σωστή.\n"
            "Το intraday ΔΕΝ γράφει γραμμή DigestDelivery όταν δεν έχει τι να στείλει, οπότε η "
            "ενότητα 2 —όχι η 4— είναι η πηγή αλήθειας για ένα συγκεκριμένο slot."
        ))

    # ------------------------------------------------------------------ sections

    def _schedules(self, now):
        self._section("1. SCHEDULER (django-q2)")
        try:
            from django_q.models import Schedule, Task
        except Exception as exc:  # pragma: no cover - only when django_q is absent
            self._fail(f"Δεν φορτώνει το django_q: {exc}")
            return

        schedules = list(Schedule.objects.all())
        if not schedules:
            self._fail("Καμία εγγραφή Schedule. Ούτε ημερήσιο ούτε 3ωρο pipeline πρόκειται να τρέξει.")
            self._hint("Το post_migrate signal τα δημιουργεί: τρέξε `manage.py migrate`.")
            return

        for schedule in schedules:
            self._line(
                schedule.name or schedule.func,
                f"cron={schedule.cron!r} next_run={self._when(schedule.next_run)} repeats={schedule.repeats}",
            )

        intraday = next((s for s in schedules if "intraday" in s.func), None)
        if intraday is None:
            self._fail("Λείπει το intraday schedule — δεν πρόκειται να σταλεί κανένα 3ωρο email.")
        elif intraday.next_run is None:
            self._fail("Το intraday schedule δεν έχει next_run· ο scheduler δεν θα το πυροδοτήσει.")
        elif intraday.next_run < timezone.now() - timedelta(hours=1):
            self._fail(
                f"Το next_run του intraday είναι στο παρελθόν ({self._when(intraday.next_run)}). "
                "Ο qcluster πιθανότατα δεν τρέχει."
            )
            self._hint("Στο Render ο worker ξεκινά από το Procfile (`honcho start`). Έλεγξε ότι ζει.")
        else:
            self._ok(f"Επόμενη εκτέλεση intraday: {self._when(intraday.next_run)}")

        cutoff = timezone.now() - timedelta(days=2)
        recent = Task.objects.filter(success=False, started__gte=cutoff)
        failure_count = recent.count()
        if failure_count:
            self._fail(f"{failure_count} αποτυχημένα tasks τις τελευταίες 2 ημέρες:")
            for task in recent.order_by("-started")[:5]:
                self._line(f"  {self._when(task.started)}", (task.result or "")[:200])
        else:
            self._ok("Καμία αποτυχία task τις τελευταίες 2 ημέρες.")

    def _runs(self, target, now):
        self._section(f"2. IMPORT RUNS ΓΙΑ {target}")
        runs = list(ImportRun.objects.filter(target_date=target).order_by("started_at"))
        if not runs:
            self._fail("Κανένα ImportRun. Το pipeline δεν κλήθηκε καθόλου γι' αυτή την ημερομηνία.")
        else:
            for run in runs:
                self._line(
                    self._when(run.started_at),
                    f"status={run.status} fetched={run.fetched_count} new={run.created_count} "
                    f"updated={run.updated_count} {('σφάλμα: ' + run.error_message[:160]) if run.error_message else ''}",
                )

        ran_hours = {timezone.localtime(run.started_at).hour for run in runs}
        due = [hour for hour in EXPECTED_INTRADAY_HOURS if hour <= now.hour] if target == now.date() else EXPECTED_INTRADAY_HOURS
        missed = [hour for hour in due if hour not in ran_hours]
        if missed:
            self._fail(f"Slots χωρίς εκτέλεση: {', '.join(f'{hour:02d}:00' for hour in missed)}")
            self._hint("Κενό slot σημαίνει ότι δεν έτρεξε το task, όχι ότι δεν βρέθηκαν εγγραφές.")
        elif due:
            self._ok(f"Έτρεξαν και τα {len(due)} slots που έχουν λήξει σήμερα.")

        # Whether the API returned anything is the difference between "broken" and "quiet day".
        companies_today = Company.objects.filter(incorporation_date=target).count()
        self._line("Εταιρείες με ημ. σύστασης αυτή την ημέρα", companies_today)
        if companies_today == 0 and runs:
            self._hint(
                "Το pipeline έτρεξε αλλά το ΓΕΜΗ δεν είχε καμία εγγραφή γι' αυτή την ημερομηνία. "
                "Τότε το intraday σιωπά εσκεμμένα: δεν στέλνει άδειο email."
            )

    def _recipients(self, target):
        self._section("3. ΠΑΡΑΛΗΠΤΕΣ")
        users = User.objects.select_related("subscription", "digest_preference").order_by("id")
        eligible_intraday, eligible_daily, blocked = [], [], []

        for user in users:
            label = user.email or user.username
            subscription = getattr(user, "subscription", None)
            tier = subscription.effective_tier if subscription else "-"
            pointer = subscription.last_sent_company_id if subscription else 0

            intraday_reason = digest_skip_reason(user, "intraday")
            daily_reason = digest_skip_reason(user, "daily")
            if intraday_reason is None:
                eligible_intraday.append((label, tier, pointer))
            elif daily_reason is None:
                eligible_daily.append((label, tier, intraday_reason))
            else:
                blocked.append((label, tier, daily_reason))

        self._line("Σύνολο λογαριασμών", users.count())

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"  Δικαιούνται 3ωρο email: {len(eligible_intraday)}"))
        for label, tier, pointer in eligible_intraday:
            # What a send right now would actually carry. An entitled recipient with 0 pending is
            # the signature of a correct silent slot: the pipeline ran and had nothing to report.
            pending = Company.objects.filter(incorporation_date=target, id__gt=pointer).count()
            self._line(
                f"  [OK] {label}",
                f"tier={tier} last_sent_company_id={pointer} εκκρεμείς νέες εγγραφές τώρα={pending}",
            )

        self.stdout.write("")
        self.stdout.write(self.style.WARNING(f"  Μόνο ημερήσιο (όχι 3ωρο): {len(eligible_daily)}"))
        for label, tier, reason in eligible_daily:
            self._line(f"  [ ! ] {label}", f"tier={tier} — {reason}")

        self.stdout.write("")
        self.stdout.write(self.style.ERROR(f"  Δεν λαμβάνουν κανένα digest: {len(blocked)}"))
        for label, tier, reason in blocked:
            self._line(f"  [ X ] {label}", f"tier={tier} — {reason}")

        if not eligible_intraday:
            self.stdout.write("")
            self._fail(
                "Κανένας λογαριασμός δεν δικαιούται 3ωρο email, άρα κανένα δεν επρόκειτο να σταλεί "
                "όσο σωστά κι αν έτρεξε το pipeline."
            )
            self._hint(
                f"Το intraday απαιτεί effective_tier σε {TOP_TIERS}. Complimentary πρόσβαση σε Pro ή "
                "Business δίνει ΜΟΝΟ ημερήσιο digest. Άλλαξέ το από /superadmin/users/<id>/."
            )

    def _deliveries(self, target):
        self._section(f"4. ΚΑΤΑΓΡΑΦΗ ΑΠΟΣΤΟΛΩΝ ΓΙΑ {target}")
        self._hint(
            "ΠΡΟΣΟΧΗ ΣΤΗΝ ΑΝΑΓΝΩΣΗ: το unique constraint είναι (user, digest_date, frequency), "
            "άρα το intraday έχει ΜΙΑ γραμμή για όλη την ημέρα, που ενημερώνεται σε κάθε αποστολή. "
            "Το πεδίο sent_at είναι auto_now_add, οπότε δείχνει μόνιμα την ΠΡΩΤΗ αποστολή της "
            "ημέρας — μια γραμμή με ώρα 08:00 μπορεί κάλλιστα να έχει ενημερωθεί στις 20:00. "
            "Για το τι έγινε σε συγκεκριμένο slot, δες την ενότητα 2."
        )
        deliveries = DigestDelivery.objects.filter(digest_date=target).select_related("user").order_by("sent_at")
        if not deliveries:
            self._line("(καμία εγγραφή DigestDelivery)", "")
            self._hint(
                "Το intraday καταγράφει γραμμή μόνο όταν όντως στείλει ή αποτύχει. Απουσία γραμμής "
                "σημαίνει ότι δεν υπήρχε τίποτα νέο να σταλεί ή ότι δεν έτρεξε καθόλου."
            )
            return
        for delivery in deliveries:
            self._line(
                f"1η αποστολή {self._when(delivery.sent_at)} · {delivery.frequency}",
                f"{delivery.user.email or delivery.user.username} status={delivery.status} "
                f"count={delivery.company_count} {delivery.error_message[:160]}",
            )

    # ------------------------------------------------------------------ output helpers

    def _section(self, title):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"=== {title} ==="))

    def _line(self, label, value):
        self.stdout.write(f"  {label}: {value}")

    def _ok(self, message):
        self.stdout.write(self.style.SUCCESS(f"  OK — {message}"))

    def _fail(self, message):
        self.stdout.write(self.style.ERROR(f"  ΠΡΟΒΛΗΜΑ — {message}"))

    def _hint(self, message):
        self.stdout.write(self.style.WARNING(f"     -> {message}"))

    @staticmethod
    def _when(value):
        return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if value else "-"
