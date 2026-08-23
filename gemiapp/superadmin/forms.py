from django import forms
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

INPUT = ("w-full rounded-2xl border border-navy-900/15 bg-white px-4 py-3 text-sm "
         "outline-none transition focus:border-blue-500")

ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLE_CHOICES = [
    (ROLE_USER, "Απλός χρήστης"),
    (ROLE_ADMIN, "Διαχειριστής (πρόσβαση στο Django admin)"),
]


class AdminUserCreateForm(forms.Form):
    """Create an account from the Superadmin panel.

    Deliberately offers two roles only. Superadmin is not creatable here: that set is
    fixed in settings.SUPERADMIN_EMAILS, so a compromised session cannot mint one.
    """

    email = forms.EmailField(label="Email")
    first_name = forms.CharField(label="Όνομα", max_length=150, required=False)
    role = forms.ChoiceField(label="Ρόλος", choices=ROLE_CHOICES, initial=ROLE_USER)
    password = forms.CharField(label="Κωδικός", widget=forms.PasswordInput, min_length=8)
    is_active = forms.BooleanField(
        label="Ενεργός αμέσως", required=False, initial=True,
        help_text="Χωρίς αυτό, ο χρήστης πρέπει να επιβεβαιώσει το email του.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name != "is_active":
                field.widget.attrs["class"] = INPUT

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists() or User.objects.filter(username__iexact=email).exists():
            raise ValidationError("Υπάρχει ήδη λογαριασμός με αυτό το email.")
        return email

    def clean_password(self):
        password = self.cleaned_data["password"]
        validate_password(password)
        return password

    def clean(self):
        cleaned = super().clean()
        email = cleaned.get("email")
        # A reserved address must not be created here with a password chosen by whoever is
        # at the keyboard: that would be a way to take over a superadmin identity.
        if email and email in settings.SUPERADMIN_EMAILS:
            raise ValidationError(
                "Αυτό το email είναι δεσμευμένο για Superadmin και δεν δημιουργείται από εδώ."
            )
        return cleaned

    def save(self):
        role = self.cleaned_data["role"]
        user = User.objects.create_user(
            username=self.cleaned_data["email"],
            email=self.cleaned_data["email"],
            password=self.cleaned_data["password"],
            first_name=self.cleaned_data.get("first_name", ""),
            is_active=self.cleaned_data.get("is_active", True),
        )
        # is_staff opens the Django admin. is_superuser is never set here.
        user.is_staff = role == ROLE_ADMIN
        user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])
        return user
