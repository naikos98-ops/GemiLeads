"""Render the client-outreach email to an HTML file for visual review.

Uses a real Company row when a --gemi number is given, otherwise a plausible
in-memory sample. Writes nothing to the database and sends no mail.

    python manage.py preview_outreach_email
    python manage.py preview_outreach_email --gemi 123456789000 --out email.html
"""

from datetime import date
import os
import tempfile
import webbrowser

from django.conf import settings
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string

from gemiapp.models import Company


class Command(BaseCommand):
    help = "Render templates/emails/client_outreach.html to a file for visual review."

    def add_arguments(self, parser):
        parser.add_argument("--gemi", help="GEMI number of a real Company to use as the sample.")
        parser.add_argument("--out", default="", help="Output path (default: a temp file).")
        parser.add_argument("--no-open", action="store_true", help="Do not open the file in a browser.")

    def handle(self, *args, **opts):
        if opts["gemi"]:
            company = Company.objects.get(gemi_number=opts["gemi"])
        else:
            company = Company(
                gemi_number="123456789000",
                name="ΠΑΡΑΔΕΙΓΜΑ ΕΜΠΟΡΙΚΗ ΜΟΝΟΠΡΟΣΩΠΗ ΙΚΕ",
                incorporation_date=date.today(),
                legal_type="ΙΔΙΩΤΙΚΗ ΚΕΦΑΛΑΙΟΥΧΙΚΗ ΕΤΑΙΡΕΙΑ",
                city="Θεσσαλονίκη",
                prefecture="ΘΕΣΣΑΛΟΝΙΚΗΣ",
                email="info@example.gr",
            )

        people = company.people if company.pk else []
        context = {
            "company": company,
            "contact_name": people[0]["name"] if people else "Γεώργιος Παπαδόπουλος",
            "signup_url": f"{settings.BASE_URL}/signup/",
            "site_url": settings.BASE_URL,
            "unsubscribe_url": f"{settings.BASE_URL}/outreach/unsubscribe/sample-token/",
        }

        html = render_to_string("emails/client_outreach.html", context)
        text = render_to_string("emails/client_outreach.txt", context)

        out = opts["out"] or os.path.join(tempfile.gettempdir(), "gemi_outreach_preview.html")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(html)

        self.stdout.write(self.style.SUCCESS(f"HTML γράφτηκε: {out}"))
        self.stdout.write("\n--- Plain-text version ---\n")
        self.stdout.write(text)

        if not opts["no_open"]:
            webbrowser.open(f"file://{os.path.abspath(out)}")
