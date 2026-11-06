"""Render every lifecycle email (HTML + text + subject) to files for review. Sends nothing, saves nothing.

The builders read live state (subscription, periods, Radars), so each email gets a sample account set up in
the state that triggers it, inside a transaction that is always rolled back.

    python manage.py preview_lifecycle_emails --out docs/lifecycle-email-previews
    python manage.py preview_lifecycle_emails --kind e4 --kind e8
"""

from datetime import date, timedelta
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.template.loader import render_to_string
from django.test import override_settings
from django.utils import timezone

from gemiapp.lifecycle_content import BUILDERS, build_e15
from gemiapp.lifecycle_content_reminders import REMINDER_BUILDERS
from gemiapp.models import Company, CustomerRadar, SubscriptionPeriod

ALL_KINDS = [f"e{n}" for n in range(1, 16)]
SAMPLE_WHATS_NEW = "Φίλτρο νομικής μορφής στη λίστα | Προεπισκόπηση Radar με τα αποτελέσματα των τελευταίων 30 ημερών"


class _Rollback(Exception):
    pass


class Command(BaseCommand):
    help = "Render all lifecycle emails to HTML/TXT files (no email is sent, nothing is saved)."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="lifecycle-email-previews", help="Output directory.")
        parser.add_argument("--kind", action="append", help="Only these kinds (e.g. --kind e2).")

    def handle(self, *args, **opts):
        out = Path(opts["out"])
        out.mkdir(parents=True, exist_ok=True)
        written = []
        try:
            with transaction.atomic(), override_settings(LIFECYCLE_WHATS_NEW=SAMPLE_WHATS_NEW):
                for kind in opts["kind"] or ALL_KINDS:
                    written.extend(self._render(out, kind))
                raise _Rollback
        except _Rollback:
            pass
        for path in written:
            self.stdout.write(str(path))

    def _sample(self, kind, now):
        user = User.objects.create_user(f"preview-{kind}@example.gr", f"preview-{kind}@example.gr",
                                        first_name="Μαρία", is_active=kind != "e1")
        sub = user.subscription
        params = {}
        paid = kind in ("e4", "e5", "e6", "e7", "e8", "e9")
        if paid:
            sub.tier, sub.status, sub.stripe_subscription_id = ("business" if kind == "e7" else "pro"), "active", "sub_p"
        if kind == "e7":
            sub.scheduled_tier, sub.scheduled_change_at = "pro", now + timedelta(days=18)
            for i in range(7):
                CustomerRadar.objects.create(user=user, name=f"Radar {i + 1}")
        if kind == "e8":
            sub.active_until = now + timedelta(days=24)
        if kind == "e9":
            sub.active_until = now + timedelta(days=3)
        sub.save()
        if kind in ("e4", "e5", "e10", "e13", "e14"):
            period = SubscriptionPeriod.objects.create(user=user, user_ref=f"preview-{kind}", source="stripe",
                                                       tier="pro", started_at=now - timedelta(days=40))
            if kind in ("e10", "e13", "e14"):
                period.ended_at, period.end_reason = now - timedelta(days=15), "canceled"
                period.save()
                sub.tier, sub.status = "pro", "canceled"
                sub.save()
            params = {"tier": "pro", "period_id": period.pk}
        if kind == "e6":
            radar = CustomerRadar.objects.create(user=user, name="Λογιστικά γραφεία Αττικής")
            params = {"radar_id": radar.pk}
        if kind == "e13":
            CustomerRadar.objects.create(user=user, name="Αττική", prefectures=["ΑΤΤΙΚΗΣ"])
        if kind in ("e11", "e13"):
            for n in range(12):
                Company.objects.create(gemi_number=f"99{kind}{n}", name=f"ΔΕΙΓΜΑ {n}", prefecture="ΑΤΤΙΚΗΣ",
                                       incorporation_date=date.today() - timedelta(days=2))
        return user, params

    def _render(self, out, kind):
        now = timezone.now()
        if kind == "e15":
            content, user = build_e15("Μαρία"), type("Recipient", (), {"email": "preview-e15@example.gr"})
        else:
            user, params = self._sample(kind, now)
            builder = BUILDERS.get(kind) or REMINDER_BUILDERS[kind]
            content = builder(user, params, now)
        if content is None:
            self.stderr.write(f"{kind}: not applicable for the sample account")
            return []
        context = {"content": content, "user": user}
        paths = []
        for ext in ("html", "txt"):
            path = out / f"{kind}.{ext}"
            path.write_text(render_to_string(f"emails/lifecycle.{ext}", context), encoding="utf-8")
            paths.append(path)
        (out / f"{kind}.subject.txt").write_text(content.subject, encoding="utf-8")
        return paths
