"""Send the inbox notification for every stored support message that never got one.

The safety net behind the worker's bounded retries: run after an SMTP outage or when the queue was
unreachable. Idempotent -- an already-notified message is never sent twice.
"""

from django.core.management.base import BaseCommand

from gemiapp.models import SupportMessage
from gemiapp.support import send_support_notification


class Command(BaseCommand):
    help = "Send pending support-message notifications to SUPPORT_INBOX."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only count the pending notifications.")

    def handle(self, *args, dry_run=False, **options):
        pending = list(SupportMessage.objects.filter(notified_at__isnull=True).order_by("id").values_list("id", flat=True))
        if dry_run:
            self.stdout.write(f"pending={len(pending)}")
            return
        sent = failed = 0
        for message_id in pending:
            try:
                send_support_notification(message_id)
                sent += 1
            except Exception:
                failed += 1
        self.stdout.write(f"pending={len(pending)} sent={sent} failed={failed}")
