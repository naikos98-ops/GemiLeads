"""Create or promote a reserved superadmin account."""

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Δημιουργεί ή προάγει έναν λογαριασμό από τη λίστα SUPERADMIN_EMAILS. "
        "Οι λογαριασμοί αυτοί δεν δημιουργούνται από το Superadmin panel."
    )

    def add_arguments(self, parser):
        parser.add_argument("email", help="Πρέπει να ανήκει στη λίστα SUPERADMIN_EMAILS.")
        parser.add_argument("--password", help="Ορισμός κωδικού (παράλειψη για υπάρχοντα λογαριασμό).")

    def handle(self, *args, **options):
        email = options["email"].strip().lower()
        if email not in settings.SUPERADMIN_EMAILS:
            raise CommandError(
                f"Το {email} δεν ανήκει στη λίστα SUPERADMIN_EMAILS. "
                f"Επιτρεπτά: {', '.join(settings.SUPERADMIN_EMAILS)}"
            )

        user = User.objects.filter(username__iexact=email).first() or User.objects.filter(email__iexact=email).first()
        created = user is None
        if created:
            if not options["password"]:
                raise CommandError("Νέος λογαριασμός: απαιτείται --password.")
            user = User.objects.create_user(username=email, email=email, password=options["password"])
        elif options["password"]:
            user.set_password(options["password"])

        user.email = email
        user.is_active = True
        user.is_staff = True
        user.is_superuser = True
        user.save()

        self.stdout.write(self.style.SUCCESS(
            f"{'Δημιουργήθηκε' if created else 'Ενημερώθηκε'}: {email} (superadmin)"
        ))
