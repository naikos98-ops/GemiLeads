import os
import django
from datetime import date
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

recipient = 'naikos98@gmail.com'

# Dummy data
class DummyCompany:
    name = "TECH INNOVATIONS M.I.K.E."
    gemi_number = "123456789012"
    legal_type = "Ιδιωτική Κεφαλαιουχική Εταιρεία (Ι.Κ.Ε.)"
    city = "Αθήνα"
    prefecture = None

class DummyCompany2:
    name = "CAFE BAR ATHENS E.E."
    gemi_number = "987654321098"
    legal_type = "Ετερόρρυθμη Εταιρεία (Ε.Ε.)"
    city = "Πειραιάς"
    prefecture = None

company_data = [
    {'company': DummyCompany(), 'radars': ['Radar Τεχνολογίας', 'Νέες ΙΚΕ']},
    {'company': DummyCompany2(), 'radars': ['Εστίαση Αττικής']},
]

unsubscribe_url = "https://gemileads.gr/unsubscribe/dummy-token/"
digest_date = date.today()

class DummyUser:
    first_name = "Naiko"
    email = "naikos98@gmail.com"

# 1. Daily Digest
context_daily = {
    'company_data': company_data,
    'digest_date': digest_date,
    'unsubscribe_url': unsubscribe_url,
    'user': DummyUser(),
}
html_content = render_to_string('emails/daily_digest.html', context_daily)
text_content = render_to_string('emails/daily_digest.txt', context_daily)

msg = EmailMultiAlternatives(
    subject="Gemi Leads: 2 νέες επιχειρήσεις (Test Daily)",
    body=text_content,
    from_email=None,
    to=[recipient]
)
msg.attach_alternative(html_content, "text/html")
msg.send()

# 3. Verification Email
context_verify = {
    'user': DummyUser(),
    'verify_url': "https://gemileads.gr/verify/dummy-uid/dummy-token/",
}
html_content_v = render_to_string('emails/verification.html', context_verify)

msg_v = EmailMultiAlternatives(
    subject="Gemi Leads: Επιβεβαίωση Email",
    body="Παρακαλούμε επιβεβαιώστε το email σας.",
    from_email=None,
    to=[recipient]
)
msg_v.attach_alternative(html_content_v, "text/html")
msg_v.send()

# 4. Password Reset
context_reset = {
    'protocol': 'https',
    'domain': 'gemileads.gr',
    'uid': 'dummy-uid',
    'token': 'dummy-token',
}
html_content_r = render_to_string('registration/password_reset_email.html', context_reset)

msg_r = EmailMultiAlternatives(
    subject="Gemi Leads: Ανάκτηση Κωδικού Πρόσβασης",
    body="Ανάκτηση κωδικού",
    from_email=None,
    to=[recipient]
)
msg_r.attach_alternative(html_content_r, "text/html")
msg_r.send()

print("Sent all test emails successfully!")
