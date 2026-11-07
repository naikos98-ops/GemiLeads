"""The words of every lifecycle email, built from the *current* state at send time.

Each builder takes (user, params, now) and returns an ``EmailContent`` or None when the email no
longer applies (the user already did the thing, resumed the subscription, ...). One structure renders
both the HTML (emails/lifecycle.html, on emails/_base.html) and the plain-text version, so the two
cannot drift.

Copy rules (brief 2026-11-06): benefit first · one CTA button · short Greek · «νέες καταχωρίσεις», never
«πελάτες/leads» as a promise · no promise of customers, sales or purchase intent · ΚΑΔ = exact code ·
νομός έδρας · no «ΓΕΜΗ» in subjects. Every claim below was checked against the code (see AGENTS.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from .models import CustomerRadar, SubscriptionPeriod, UserSubscription

TIER_NAMES = {"pro": "Pro", "business": "Business", "enterprise": "Enterprise", "custom": "Custom"}
SIGNATURE = ("Νίκος", "Gemi Leads")


def absolute(path: str) -> str:
    return f"{settings.BASE_URL.rstrip('/')}{path}"


def greeting(user) -> str:
    name = (user.first_name or "").strip()
    return f"Γεια σου {name}," if name else "Γεια σου,"


def local_date(value) -> str:
    return timezone.localtime(value).strftime("%d/%m/%Y")


@dataclass
class EmailContent:
    kind: str
    subject: str
    preheader: str
    kicker: str
    title: str
    greeting: str
    # Blocks before the button. Each block: ("p", text) | ("list", [items]) | ("steps", [items]) | ("notice", text)
    blocks: list
    cta_label: str = ""
    cta_url: str = ""
    # Blocks after the button.
    after: list = field(default_factory=list)
    # An optional quiet text link (e.g. the E8 question). Never a second button.
    text_link_label: str = ""
    text_link_url: str = ""
    reminder: bool = False
    unsubscribe_url: str = ""
    signature: tuple = SIGNATURE

    def headers(self, row) -> dict:
        headers = {"X-Mailin-Tag": row.brevo_tag}
        if self.reminder and self.unsubscribe_url:
            headers["List-Unsubscribe"] = f"<{self.unsubscribe_url}>"
        return headers


def _subscription(user):
    return UserSubscription.objects.filter(user=user).first()


def _active_radar_count(user) -> int:
    return CustomerRadar.objects.filter(user=user, is_active=True, deleted_at__isnull=True).count()


# --- E2 · welcome after first verification (text: brief 2026-11-04, unchanged apart from layout) ------

def build_e2(user, params, now):
    return EmailContent(
        kind="e2",
        subject="Καλώς ήρθες στο Gemi Leads — ένα βήμα για να ξεκινήσεις",
        preheader="Φτιάξε το πρώτο σου φίλτρο: ακριβής ΚΑΔ και νομός έδρας.",
        kicker="Καλώς ήρθες",
        title="Ένα βήμα για να ξεκινήσεις",
        greeting=greeting(user),
        blocks=[
            ("p", "Είμαι ο Νίκος και φτιάχνω το Gemi Leads. Ευχαριστώ που γράφτηκες."),
            ("p", "Το Gemi Leads σού δείχνει κάθε μέρα τις νέες επιχειρήσεις που καταχωρούνται, για να βρίσκεις "
                  "νωρίς ποιες ταιριάζουν σε αυτό που πουλάς."),
            ("p", "Ένα βήμα για αρχή — φτιάξε το πρώτο σου φίλτρο:"),
            ("steps", [
                "Σκέψου σε ποιους πουλάς.",
                "Διάλεξε τον ακριβή ΚΑΔ τους (κάθε κωδικός επιλέγεται ξεχωριστά).",
                "Διάλεξε τον νομό έδρας.",
                "Δες τη λίστα σου.",
            ]),
        ],
        cta_label="Φτιάξε το φίλτρο σου",
        cta_url=absolute(reverse("dashboard")),
        after=[
            ("p", "Κάθε πρωί στις 09:00 θα σου έρχεται email με τις νέες καταχωρίσεις της προηγούμενης μέρας."),
            ("p", "Αν κολλήσεις κάπου, απάντησε σε αυτό το email ή πάτα «Βοήθεια» μέσα στην εφαρμογή. "
                  "Το διαβάζω εγώ."),
        ],
    )


# --- E4 · paid welcome (brief 2026-11-04) -----------------------------------------------------------

def build_e4(user, params, now):
    tier = params.get("tier")
    sub = _subscription(user)
    # Still on that tier and paying: otherwise (cancelled before the worker ran, moved again) skip.
    if sub is None or not sub.has_active_paid_subscription or sub.tier != tier:
        return None
    tier_name = TIER_NAMES.get(tier, tier)
    return EmailContent(
        kind="e4",
        subject=f"Είσαι στο {tier_name} — στήσε το πρώτο σου Radar",
        preheader="Φτιάξε το πρώτο σου Radar: ακριβής ΚΑΔ, νομός έδρας, προεπισκόπηση.",
        kicker=tier_name,
        title=f"Είσαι στο {tier_name}",
        greeting=greeting(user),
        blocks=[
            ("p", f"Ευχαριστώ που πέρασες στο {tier_name}. Το εννοώ — κάθε συνδρομή μετράει πολύ σε αυτή τη φάση."),
            ("p", "Τι έχεις πλέον:"),
            ("list", [
                f"Έως {sub.radar_limit} ενεργά Radars. Κάθε νέα καταχώριση ελέγχεται με τα κριτήριά σου και "
                "βλέπεις γιατί ταιριάζει.",
                "Τα ταιριάσματα των Radars σου στο πρωινό email.",
                "Εξαγωγή σε CSV.",
            ]),
            ("p", "Πρώτο βήμα: φτιάξε το πρώτο σου Radar — ακριβής ΚΑΔ, νομός έδρας, και δες προεπισκόπηση πριν "
                  "το αποθηκεύσεις."),
        ],
        cta_label="Φτιάξε Radar",
        cta_url=absolute(reverse("radar_create")),
        after=[
            ("p", "Θες βοήθεια να το στήσεις σωστά; Απάντησε εδώ και πες μου τι πουλάς και σε ποιους. "
                  "Θα σου απαντήσω εγώ."),
            ("p", "Τη συνδρομή σου τη διαχειρίζεσαι από τις Ρυθμίσεις του λογαριασμού σου."),
        ],
    )


# --- E5 · paid for 24h, still no Radar ----------------------------------------------------------------

def build_e5(user, params, now):
    from .lifecycle import reminder_unsubscribe_url

    period = SubscriptionPeriod.objects.filter(pk=params.get("period_id"), user=user, ended_at__isnull=True).first()
    sub = _subscription(user)
    if period is None or sub is None or sub.radar_limit <= 0 or _active_radar_count(user):
        return None
    return EmailContent(
        kind="e5",
        subject="Το Radar σου δεν έχει στηθεί ακόμα",
        preheader="Απάντησε με τι πουλάς και σε ποιους και σου το στήνω εγώ.",
        kicker="Radar",
        title="Το Radar σου δεν έχει στηθεί ακόμα",
        greeting=greeting(user),
        blocks=[
            ("p", "Χωρίς Radar, το πρωινό σου email έχει τις νέες καταχωρίσεις της μέρας χωρίς φίλτρο — όχι μόνο "
                  "όσες σε αφορούν. Το Radar τις ελέγχει με τα δικά σου κριτήρια (ακριβής ΚΑΔ, νομός έδρας) και σου "
                  "δείχνει μόνο όσες ταιριάζουν."),
            ("p", "Θέλει 2 λεπτά. Ή, αν προτιμάς, απάντησε σε αυτό το email με τι πουλάς και σε ποιους και σου "
                  "το στήνω εγώ."),
        ],
        cta_label="Φτιάξε Radar",
        cta_url=absolute(reverse("radar_create")),
        reminder=True,
        unsubscribe_url=reminder_unsubscribe_url(user),
    )


# --- E8 · cancellation scheduled ------------------------------------------------------------------

def build_e8(user, params, now):
    sub = _subscription(user)
    if sub is None or sub.active_until is None or sub.active_until <= now:
        return None            # resumed (or already over) before the worker ran
    ends = local_date(sub.active_until)
    return EmailContent(
        kind="e8",
        subject=f"Η συνδρομή σου λήγει στις {ends}",
        preheader="Έως τότε όλα μένουν ενεργά.",
        kicker="Συνδρομή",
        title=f"Η συνδρομή σου λήγει στις {ends}",
        greeting=greeting(user),
        blocks=[
            ("p", f"Η ακύρωση καταχωρίστηκε. Έως τις {ends} όλα μένουν ενεργά: τα Radars σου, τα ταιριάσματά "
                  "τους στο πρωινό email και η εξαγωγή CSV."),
            ("p", "Αν άλλαξες γνώμη, μπορείς να συνεχίσεις τη συνδρομή σου με ένα κλικ από τις Ρυθμίσεις."),
        ],
        cta_label="Συνέχιση συνδρομής",
        cta_url=absolute(reverse("settings")),
        after=[("p", "Μία ερώτηση μόνο, αν έχεις ένα λεπτό — η απάντησή σου με βοηθά να το κάνω καλύτερο:")],
        text_link_label="Τι δεν σου κάλυψε;",
        text_link_url=absolute(reverse("churn_feedback") + "?context=cancel"),
    )


# --- E10 · back on Free -------------------------------------------------------------------------------

def build_e10(user, params, now):
    period = SubscriptionPeriod.objects.filter(pk=params.get("period_id")).first()
    sub = _subscription(user)
    if period is None or period.end_reason == "account_deleted":
        return None
    if sub is None or sub.has_entitlement:
        return None            # re-subscribed or has complimentary access: not on Free
    return EmailContent(
        kind="e10",
        subject="Είσαι πλέον στο Free",
        preheader="Τι κρατάς, τι σταματά, και πώς επιστρέφεις.",
        kicker="Free",
        title="Είσαι πλέον στο Free",
        greeting=greeting(user),
        blocks=[
            ("p", "Η συνδρομή σου έληξε και ο λογαριασμός σου είναι πλέον στο Free."),
            ("p", "Τι κρατάς:"),
            ("list", [
                "Τη λίστα με τις νέες καταχωρίσεις και τα φίλτρα της (ΚΑΔ, νομός έδρας).",
                "Το ημερήσιο email στις 09:00 με τις νέες καταχωρίσεις της προηγούμενης μέρας.",
                "Όσες επιχειρήσεις έχεις ήδη στα Leads σου, με τις σημειώσεις σου.",
            ]),
            ("p", "Τι σταματά:"),
            ("list", [
                "Τα Radars δεν ελέγχουν πλέον τις νέες καταχωρίσεις και τα ταιριάσματα δεν μπαίνουν στο email.",
                "Η εξαγωγή σε CSV.",
            ]),
            ("p", "Τα Radars σου μένουν αποθηκευμένα με τα κριτήριά τους. Αν αποκτήσεις ξανά συνδρομή, "
                  "ξαναρχίζουν να ελέγχουν τις νέες καταχωρίσεις χωρίς να τα στήσεις από την αρχή."),
        ],
        cta_label="Δες τα πλάνα",
        cta_url=absolute(reverse("pricing")),
    )


# --- E15 · account deleted (sent by gemiapp.account_deletion: the account no longer exists) ----------

def build_e15(first_name=""):
    name = (first_name or "").strip()
    return EmailContent(
        kind="e15",
        subject="Ο λογαριασμός σου διαγράφηκε",
        preheader="Τι διαγράφηκε και τι κρατάμε.",
        kicker="Λογαριασμός",
        title="Ο λογαριασμός σου διαγράφηκε",
        greeting=f"Γεια σου {name}," if name else "Γεια σου,",
        blocks=[
            ("p", "Διαγράψαμε τον λογαριασμό σου στο Gemi Leads και ό,τι ήταν συνδεδεμένο μαζί του: τα Radars, τα "
                  "Leads και τις σημειώσεις σου, τις ρυθμίσεις του email και τα μηνύματα υποστήριξης."),
            ("p", "Αν είχες ενεργή συνδρομή, ακυρώθηκε και δεν θα χρεωθείς ξανά."),
            ("p", "Τι κρατάμε: τα στοιχεία των πληρωμών σου (ποσά, ημερομηνίες, αριθμοί συναλλαγών), όσο το απαιτεί η "
                  "φορολογική νομοθεσία. Στη δική μας βάση μένουν χωρίς το όνομα, το email και τη διεύθυνσή σου. Η "
                  "Stripe, που επεξεργάζεται τις πληρωμές, διατηρεί τα δικά της αρχεία συναλλαγών."),
            ("p", "Δεν θα λάβεις άλλα email από εμάς — αυτό είναι το τελευταίο. Αν έχεις ερώτηση, απάντησε σε αυτό "
                  "το email."),
        ],
    )


BUILDERS = {"e2": build_e2, "e4": build_e4, "e5": build_e5, "e8": build_e8, "e10": build_e10}


def build_content(row, *, now=None):
    builder = BUILDERS.get(row.kind)
    if builder is None:
        raise LookupError(f"No builder for lifecycle email {row.kind}")
    return builder(row.user, row.params or {}, now or timezone.now())
