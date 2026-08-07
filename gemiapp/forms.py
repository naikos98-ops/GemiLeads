from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from .models import DigestPreference


INPUT = "w-full rounded-2xl border border-navy-900/10 bg-white/70 px-4 py-3 text-navy-950 outline-none transition focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10"


class SignupForm(UserCreationForm):
    email = forms.EmailField(required=True, label="Email")

    class Meta:
        model = User
        fields = ("first_name", "email", "password1", "password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = INPUT
        self.fields["first_name"].label = "Όνομα"

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Υπάρχει ήδη λογαριασμός με αυτό το email.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        user.username = self.cleaned_data["email"]
        if commit:
            user.save()
            DigestPreference.objects.create(user=user)
        return user


class DigestPreferenceForm(forms.ModelForm):
    class Meta:
        model = DigestPreference
        fields = ("frequency", "only_active", "include_empty_digest")
        labels = {
            "frequency": "Συχνότητα email",
            "only_active": "Μόνο ενεργές επιχειρήσεις",
            "include_empty_digest": "Στείλε ενημέρωση ακόμη και χωρίς νέες εγγραφές",
        }
        widgets = {"frequency": forms.Select(attrs={"class": INPUT})}
