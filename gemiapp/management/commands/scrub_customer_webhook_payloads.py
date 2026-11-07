"""Redact personal fields from stored Stripe webhook payloads of one customer (manual deletions).

    python manage.py scrub_customer_webhook_payloads --email someone@example.gr --customer cus_123
    python manage.py scrub_customer_webhook_payloads --email someone@example.gr --apply

Dry run unless --apply. Keeps ids, amounts, dates and statuses; replaces customer_details, customer_email,
billing_details, receipt_email, shipping, name/email/phone/address (gemiapp.account_deletion.PERSONAL_KEYS)
with "[redacted]" in every stored event that mentions the customer id or the address.
"""

from django.core.management.base import BaseCommand, CommandError

from gemiapp.account_deletion import scrub_webhook_payloads


class Command(BaseCommand):
    help = "Redact a customer's personal data from stored Stripe webhook payloads (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--email", action="append", default=[])
        parser.add_argument("--customer", action="append", default=[], help="Stripe customer id (cus_...)")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        if not (opts["email"] or opts["customer"]):
            raise CommandError("Give at least one --email or --customer.")
        count = scrub_webhook_payloads(customer_ids=opts["customer"], emails=opts["email"], dry_run=not opts["apply"])
        verb = "scrubbed" if opts["apply"] else "would scrub"
        self.stdout.write(f"{verb} {count} stored Stripe event(s).")
        if not opts["apply"]:
            self.stdout.write("Dry run: τίποτα δεν άλλαξε. Ξανά με --apply.")
