from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.conf import settings
from django.urls import reverse

class Command(BaseCommand):
    help = "Resend verification emails to inactive users"

    def handle(self, *args, **options):
        inactive_users = User.objects.filter(is_active=False)
        count = 0
        domain = "gemileads.gr"
        protocol = "https"
        
        for user in inactive_users:
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            verify_url = f"{protocol}://{domain}{reverse('verify_email', kwargs={'uidb64': uid, 'token': token})}"
            
            subject = "Επιβεβαίωση email στο Gemi Leads"
            message = render_to_string("emails/verification.txt", {"verify_url": verify_url, "user": user})
            html_message = render_to_string("emails/verification.html", {"verify_url": verify_url, "user": user})
            
            send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [user.email], html_message=html_message)
            count += 1
            self.stdout.write(self.style.SUCCESS(f"Sent email to {user.email}"))
            
        self.stdout.write(self.style.SUCCESS(f"Finished! Sent {count} verification emails."))
