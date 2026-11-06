"""Render every implemented lifecycle email (HTML + text) to files for review. Sends nothing.

The builders read live state (subscription, periods, Radars), so a sample account is created inside a
transaction that is always rolled back: the database is left exactly as it was.

    python manage.py preview_lifecycle_emails --out docs/lifecycle-email-previews
"""

from datetime import timedelta
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from gemiapp.lifecycle_content import BUILDERS
from gemiapp.models import LifecycleEmail, SubscriptionPeriod, UserSubscription


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
        kinds = opts["kind"] or sorted(BUILDERS, key=lambda k: int(k[1:]))
        written = []
        try:
            with transaction.atomic():
                written = self._render(out, kinds)
                raise _Rollback
        except _Rollback:
            pass
        for path in written:
            self.stdout.write(str(path))

    def _render(self, out, kinds):
        now = timezone.now()
        user = User.objects.create_user("preview-lifecycle@example.gr", "preview-lifecycle@example.gr",
                                        first_name="Μαρία")
        sub = UserSubscription.objects.get(user=user)
        sub.tier, sub.status, sub.stripe_subscription_id = "pro", "active", "sub_preview"
        sub.save()
        period = SubscriptionPeriod.objects.create(user=user, user_ref="preview", source="stripe", tier="pro",
                                                   started_at=now - timedelta(days=2))
        written = []
        for kind in kinds:
            params = {"tier": "pro", "period_id": period.pk}
            if kind == "e8":
                sub.active_until = now + timedelta(days=24)
                sub.save()
            if kind == "e10":
                sub.status, sub.active_until = "canceled", None
                sub.save()
                period.ended_at, period.end_reason = now, "canceled"
                period.save()
            row = LifecycleEmail(kind=kind, user=user, dedupe_key=f"preview:{kind}", params=params,
                                 brevo_tag=f"lifecycle:{kind}:preview")
            content = BUILDERS[kind](user, params, now)
            if content is None:
                self.stderr.write(f"{kind}: not applicable for the sample account")
                continue
            context = {"content": content, "user": user}
            for ext in ("html", "txt"):
                path = out / f"{kind}.{ext}"
                path.write_text(render_to_string(f"emails/lifecycle.{ext}", context), encoding="utf-8")
                written.append(path)
            (out / f"{kind}.subject.txt").write_text(content.subject, encoding="utf-8")
        return written
