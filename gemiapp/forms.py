from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from .models import Company, CustomerRadar, DigestPreference, UserCompanyLead


INPUT = "w-full rounded-2xl border border-navy-900/10 bg-white/70 px-4 py-3 text-navy-950 outline-none transition focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10"
MULTI_INPUT = f"{INPUT} min-h-36"


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
        self.fields["password1"].help_text = "Ο κωδικός πρέπει να περιέχει τουλάχιστον 8 χαρακτήρες, συνδυάζοντας γράμματα και αριθμούς."
        self.fields["password2"].help_text = ""

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Υπάρχει ήδη λογαριασμός με αυτό το email.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        user.username = self.cleaned_data["email"]
        user.is_active = False
        if commit:
            user.save()
            DigestPreference.objects.get_or_create(user=user)
            CustomerRadar.objects.create(user=user, name="Όλες οι νέες επιχειρήσεις")
        return user

class DigestPreferenceForm(forms.ModelForm):
    class Meta:
        model = DigestPreference
        fields = ("frequency", "include_empty_digest")
        labels = {
            "frequency": "Συχνότητα email",
            "include_empty_digest": "Στείλε ενημέρωση ακόμη και χωρίς νέες εγγραφές",
        }
        widgets = {"frequency": forms.Select(attrs={"class": INPUT})}


class CustomerRadarForm(forms.ModelForm):
    prefectures = forms.MultipleChoiceField(
        label="Περιφερειακές ενότητες",
        required=False,
        widget=forms.SelectMultiple(attrs={"class": MULTI_INPUT}),
    )
    legal_types = forms.MultipleChoiceField(
        label="Νομικές μορφές",
        required=False,
        widget=forms.SelectMultiple(attrs={"class": MULTI_INPUT}),
    )

    class Meta:
        model = CustomerRadar
        fields = ("name", "name_query", "prefectures", "legal_types", "only_active", "frequency", "is_active")
        labels = {
            "name": "Όνομα Radar",
            "name_query": "Λέξη ή φράση στην επωνυμία",
            "only_active": "Μόνο ενεργές επιχειρήσεις",
            "frequency": "Συχνότητα ενημέρωσης",
            "is_active": "Το Radar είναι ενεργό",
        }
        widgets = {
            "name": forms.TextInput(attrs={"class": INPUT, "placeholder": "π.χ. Νέα εστιατόρια Αττικής"}),
            "name_query": forms.TextInput(attrs={"class": INPUT, "placeholder": "Προαιρετικό"}),
            "frequency": forms.Select(attrs={"class": INPUT}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["prefectures"].choices = [
            (value, value)
            for value in Company.objects.exclude(prefecture="").values_list("prefecture", flat=True).distinct().order_by("prefecture")
        ]
        self.fields["legal_types"].choices = [
            (value, value)
            for value in Company.objects.exclude(legal_type="").values_list("legal_type", flat=True).distinct().order_by("legal_type")
        ]

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if self.user:
            duplicates = CustomerRadar.objects.filter(user=self.user, deleted_at__isnull=True)
            if self.instance.pk:
                duplicates = duplicates.exclude(pk=self.instance.pk)
            normalized_name = name.casefold()
            if any(existing.casefold() == normalized_name for existing in duplicates.values_list("name", flat=True)):
                raise forms.ValidationError("Υπάρχει ήδη Radar με αυτό το όνομα.")
        return name


class LeadStatusForm(forms.ModelForm):
    class Meta:
        model = UserCompanyLead
        fields = ("status",)
        widgets = {"status": forms.Select(attrs={"class": INPUT})}


class LeadNotesForm(forms.ModelForm):
    class Meta:
        model = UserCompanyLead
        fields = ("notes",)
        labels = {"notes": "Ιδιωτικές σημειώσεις"}
        widgets = {
            "notes": forms.Textarea(attrs={
                "class": f"{INPUT} min-h-36 resize-y",
                "maxlength": "5000",
                "placeholder": "Κατέγραψε επαφή, επόμενο βήμα ή χρήσιμες πληροφορίες…",
            })
        }

    def clean_notes(self):
        notes = self.cleaned_data["notes"].strip()
        if len(notes) > 5000:
            raise forms.ValidationError("Οι σημειώσεις μπορούν να έχουν έως 5.000 χαρακτήρες.")
        return notes
