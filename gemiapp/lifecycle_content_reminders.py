"""Words of the PR 3 lifecycle emails (E1, E3, E6, E7, E9, E11–E14). Same rules as lifecycle_content.

Claims checked against the code (2026-11-06):
* Free daily email = the previous day's registrations without a filter (send_digests, up to 100).
* Dashboard filters: exact ΚΑΔ codes (activity_records__code__in) and νομός έδρας (prefecture).
* A Radar checks new registrations only while the account has an entitlement (eligible_radars); its criteria
  are kept after the subscription ends. Leads stay visible on Free (lead_list has no entitlement gate).
* E7: there is NO automatic pause on a downgrade (eligible_radars does not check the limit; radar_toggle only
  refuses to *activate* above it). The text therefore asks the user to choose, and never claims a pause.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.urls import reverse

from .lifecycle_content import TIER_NAMES, EmailContent, _subscription, absolute, greeting, local_date
from .models import RADAR_LIMITS, Company, CustomerRadar, RadarMatch, SubscriptionPeriod


def registrations(count: int) -> str:
    """«1 νέα καταχώριση» / «N νέες καταχωρίσεις»."""
    return f"{count} νέα καταχώριση" if count == 1 else f"{count} νέες καταχωρίσεις"


def _unsubscribe(user):
    from .lifecycle import reminder_unsubscribe_url

    return reminder_unsubscribe_url(user)


def _reminder(content: EmailContent, user) -> EmailContent:
    content.reminder = True
    content.unsubscribe_url = _unsubscribe(user)
    return content


# --- E1 · still unverified after 24h (account email; the only one an inactive account may get) ------

def build_e1(user, params, now):
    from .services import verification_url

    if user.is_active:
        return None                       # verified in the meantime
    return EmailContent(
        kind="e1",
        subject="Επιβεβαίωσε το email σου για να ξεκινήσεις",
        preheader="Ένα κλικ και ο λογαριασμός σου είναι έτοιμος.",
        kicker="Λογαριασμός",
        title="Επιβεβαίωσε το email σου",
        greeting=greeting(user),
        blocks=[
            ("p", "Η εγγραφή σου στο Gemi Leads δεν έχει ολοκληρωθεί ακόμα. Πάτα το κουμπί για να επιβεβαιώσεις ότι "
                  "αυτή η διεύθυνση είναι δική σου και να δεις τις νέες καταχωρίσεις."),
        ],
        cta_label="Επιβεβαίωση email",
        cta_url=verification_url(user),
        after=[("notice", "Ο σύνδεσμος ισχύει για 3 ημέρες και μία φορά. Αν δεν έκανες εσύ την εγγραφή, αγνόησε αυτό "
                          "το μήνυμα.")],
    )


# --- E3 · Free, 48h after verification, never used a filter -------------------------------------------

def build_e3(user, params, now):
    sub = _subscription(user)
    profile = getattr(user, "lifecycle_profile", None)
    if sub is None or sub.radar_limit > 0 or (profile and profile.last_filter_used_at):
        return None
    return _reminder(EmailContent(
        kind="e3",
        subject="Βρες τις καταχωρίσεις που σε αφορούν σε 2 κλικ",
        preheader="Φίλτρο ΚΑΔ και νομός έδρας στη λίστα σου.",
        kicker="Συμβουλή",
        title="Βρες τις καταχωρίσεις που σε αφορούν",
        greeting=greeting(user),
        blocks=[
            ("p", "Το πρωινό σου email έχει τις νέες καταχωρίσεις της προηγούμενης μέρας χωρίς φίλτρο. Στη λίστα "
                  "μπορείς να δεις μόνο όσες σε αφορούν:"),
            ("steps", [
                "Άνοιξε «Αναζήτηση & Φίλτρα».",
                "Διάλεξε τον ακριβή ΚΑΔ των πελατών σου και τον νομό έδρας.",
            ]),
            ("p", "Θες να σου έρχονται μόνο αυτές στο email; Αυτό κάνει το Radar (Pro)."),
        ],
        cta_label="Άνοιξε τη λίστα",
        cta_url=absolute(reverse("dashboard")),
    ), user)


# --- E6 · a Radar active >= 7 days with no match ---------------------------------------------------------

def build_e6(user, params, now):
    radar = CustomerRadar.objects.filter(pk=params.get("radar_id"), user=user, is_active=True,
                                         deleted_at__isnull=True).first()
    if radar is None or radar.matches.exists():
        return None
    return _reminder(EmailContent(
        kind="e6",
        subject="Το Radar σου δεν βρήκε τίποτα αυτή την εβδομάδα",
        preheader=f"«{radar.name}»: πρόσθεσε ΚΑΔ ή νομό.",
        kicker="Radar",
        title="Το Radar σου δεν βρήκε τίποτα αυτή την εβδομάδα",
        greeting=greeting(user),
        blocks=[
            ("p", f"Το Radar «{radar.name}» ελέγχει τις νέες καταχωρίσεις εδώ και μία εβδομάδα, αλλά καμία δεν "
                  "ταίριαξε με τα κριτήριά του."),
            ("p", "Συνήθως βοηθά να προσθέσεις κι άλλους ακριβείς ΚΑΔ ή έναν ακόμη νομό έδρας. Με την προεπισκόπηση "
                  "βλέπεις αμέσως πόσες καταχωρίσεις θα είχαν ταιριάξει."),
        ],
        cta_label="Άνοιξε το Radar",
        cta_url=absolute(reverse("radar_detail", args=[radar.pk])),
    ), user)


# --- E7 · scheduled downgrade -------------------------------------------------------------------------------

def build_e7(user, params, now):
    sub = _subscription(user)
    if sub is None or not sub.scheduled_tier or not sub.scheduled_change_at or sub.scheduled_change_at <= now:
        return None
    when = local_date(sub.scheduled_change_at)
    new_tier = TIER_NAMES.get(sub.scheduled_tier, sub.scheduled_tier)
    limit = RADAR_LIMITS.get(sub.scheduled_tier, 0)
    active = CustomerRadar.objects.filter(user=user, is_active=True, deleted_at__isnull=True).count()
    blocks = [("p", f"Η αλλαγή καταχωρίστηκε: στις {when} περνάς στο {new_tier}. Έως τότε όλα μένουν όπως είναι.")]
    if active > limit:
        blocks.append(("p", f"Το {new_tier} επιτρέπει έως {limit} ενεργά Radars και τώρα έχεις {active}. Διάλεξε "
                            "ποια θα κρατήσεις ενεργά και βάλε τα υπόλοιπα σε παύση από τη σελίδα Radars — τα κριτήριά "
                            "τους μένουν αποθηκευμένα."))
    else:
        blocks.append(("p", f"Το {new_tier} επιτρέπει έως {limit} ενεργά Radars· τα δικά σου ({active}) χωράνε."))
    blocks.append(("p", "Άλλαξες γνώμη; Μπορείς να ακυρώσεις την υποβάθμιση από τις Ρυθμίσεις."))
    return EmailContent(
        kind="e7",
        subject=f"Στις {when} περνάς στο {new_tier}",
        preheader=f"Νέο όριο: {limit} ενεργά Radars.",
        kicker="Συνδρομή",
        title=f"Στις {when} περνάς στο {new_tier}",
        greeting=greeting(user),
        blocks=blocks,
        cta_label="Ακύρωση υποβάθμισης",
        cta_url=absolute(reverse("settings")),
    )


# --- E9 · 3 days before the scheduled end ------------------------------------------------------------------

def build_e9(user, params, now):
    sub = _subscription(user)
    if sub is None or sub.active_until is None or not (now < sub.active_until <= now + timedelta(days=3, hours=12)):
        return None
    ends = local_date(sub.active_until)
    return _reminder(EmailContent(
        kind="e9",
        subject="Η συνδρομή σου λήγει σε 3 μέρες",
        preheader=f"Στις {ends} σταματούν τα Radars.",
        kicker="Συνδρομή",
        title="Η συνδρομή σου λήγει σε 3 μέρες",
        greeting=greeting(user),
        blocks=[
            ("p", f"Στις {ends} η συνδρομή σου λήγει και περνάς στο Free. Από τότε σταματούν:"),
            ("list", [
                "Τα Radars: δεν ελέγχουν πλέον τις νέες καταχωρίσεις.",
                "Τα ταιριάσματα των Radars στο πρωινό email.",
                "Η εξαγωγή σε CSV.",
            ]),
            ("p", "Κρατάς τη λίστα με τα φίλτρα, το ημερήσιο email και τα Leads που έχεις ήδη. Τα Radars μένουν "
                  "αποθηκευμένα με τα κριτήριά τους."),
        ],
        cta_label="Συνέχιση συνδρομής",
        cta_url=absolute(reverse("settings")),
    ), user)


# --- E11 / E12 · inactivity -------------------------------------------------------------------------------

def week_count(user, now) -> tuple[int, bool]:
    """(N, from_radars): Radar matches of the last 7 days, or all new registrations when no Radar runs."""
    sub = _subscription(user)
    since = (now - timedelta(days=7)).date()
    radars = CustomerRadar.objects.filter(user=user, is_active=True, deleted_at__isnull=True)
    if sub is not None and sub.has_entitlement and radars.exists():
        return RadarMatch.objects.filter(radar__in=radars, matched_on__gte=since).values("company").distinct().count(), True
    return Company.objects.filter(incorporation_date__gte=since, incorporation_date__lte=now.date()).count(), False


def build_e11(user, params, now):
    count, from_radars = week_count(user, now)
    if count == 0:
        return None
    lead = (f"Τα Radars σου βρήκαν {registrations(count)} που ταιριάζουν με τα κριτήριά σου τις τελευταίες 7 μέρες."
            if from_radars else
            f"Τις τελευταίες 7 μέρες: {registrations(count)}. Με φίλτρο ΚΑΔ και νομού έδρας βλέπεις μόνο όσες σε "
            "αφορούν.")
    return _reminder(EmailContent(
        kind="e11",
        subject=f"{registrations(count)} αυτή την εβδομάδα",
        preheader="Δες ποιες σε αφορούν.",
        kicker="Εβδομάδα",
        title=f"{registrations(count)} αυτή την εβδομάδα",
        greeting=greeting(user),
        blocks=[("p", lead)],
        cta_label="Δες τες" if from_radars else "Άνοιξε τη λίστα",
        cta_url=absolute(reverse("lead_list" if from_radars else "dashboard")),
    ), user)


def build_e12(user, params, now):
    from .lifecycle import keep_url

    return _reminder(EmailContent(
        kind="e12",
        subject="Να συνεχίσω να σου γράφω;",
        preheader="Ένα κλικ αν θες να συνεχίσεις να λαμβάνεις συμβουλές.",
        kicker="Ερώτηση",
        title="Να συνεχίσω να σου γράφω;",
        greeting=greeting(user),
        blocks=[
            ("p", "Έχεις καιρό να μπεις στο Gemi Leads, οπότε σε ρωτάω πριν σου ξαναστείλω συμβουλές και υπενθυμίσεις."),
            ("p", "Αν θες να συνεχίσουν, πάτα το κουμπί. Αν δεν το πατήσεις μέσα σε 14 μέρες, σταματώ να τις στέλνω. "
                  "Το πρωινό email δεν αλλάζει."),
        ],
        cta_label="Ναι, κράτα με",
        cta_url=keep_url(user),
    ), user)


# --- E13 / E14 · win-back of former paid customers ----------------------------------------------------------

def would_have_matched(user, since, now) -> int:
    from .services import company_matches_radar

    radars = list(CustomerRadar.objects.filter(user=user, deleted_at__isnull=True).prefetch_related("activity_codes"))
    if not radars:
        return 0
    companies = Company.objects.filter(incorporation_date__gt=since.date(), incorporation_date__lte=now.date()) \
        .prefetch_related("activity_records")
    return sum(1 for company in companies if any(company_matches_radar(company, r)[0] for r in radars))


def build_e13(user, params, now):
    period = SubscriptionPeriod.objects.filter(pk=params.get("period_id"), user=user).first()
    sub = _subscription(user)
    if period is None or period.ended_at is None or (sub and sub.has_entitlement):
        return None
    count = would_have_matched(user, period.ended_at, now)
    if count:
        subject = f"Το Radar σου θα είχε βρει {registrations(count)}"
        lead = (f"Από τότε που έληξε η συνδρομή σου: {registrations(count)} με τα κριτήρια των Radars "
                "σου. Τα Radars είναι ακόμη αποθηκευμένα — με νέα συνδρομή ξαναρχίζουν να τις ελέγχουν.")
    else:
        subject = "Τα Radars σου είναι ακόμη αποθηκευμένα"
        lead = ("Τα Radars σου είναι ακόμη αποθηκευμένα με τα κριτήριά τους. Με νέα συνδρομή ξαναρχίζουν να ελέγχουν "
                "τις νέες καταχωρίσεις, χωρίς να τα στήσεις από την αρχή.")
    return _reminder(EmailContent(
        kind="e13", subject=subject, preheader="Τα κριτήριά σου σε περιμένουν.", kicker="Radar", title=subject,
        greeting=greeting(user), blocks=[("p", lead)],
        cta_label="Δες τα πλάνα", cta_url=absolute(reverse("pricing")),
    ), user)


def whats_new_items() -> list[str]:
    raw = getattr(settings, "LIFECYCLE_WHATS_NEW", "") or ""
    return [item.strip() for item in raw.split("|") if item.strip()]


def build_e14(user, params, now):
    items = whats_new_items()
    period = SubscriptionPeriod.objects.filter(pk=params.get("period_id"), user=user).first()
    sub = _subscription(user)
    if not items or period is None or (sub and sub.has_entitlement):
        return None
    return _reminder(EmailContent(
        kind="e14",
        subject="Τι νέο έχει το Gemi Leads",
        preheader="Λίγες αλλαγές από τότε που έφυγες.",
        kicker="Νέα",
        title="Τι νέο έχει το Gemi Leads",
        greeting=greeting(user),
        blocks=[("p", "Από τότε που έληξε η συνδρομή σου, αλλάξαμε μερικά πράγματα:"), ("list", items[:3])],
        cta_label="Δες τα πλάνα",
        cta_url=absolute(reverse("pricing")),
        after=[("p", "Αυτό είναι το τελευταίο email για επιστροφή — δεν θα σου ξαναγράψω γι' αυτό.")],
    ), user)


REMINDER_BUILDERS = {
    "e1": build_e1, "e3": build_e3, "e6": build_e6, "e7": build_e7, "e9": build_e9,
    "e11": build_e11, "e12": build_e12, "e13": build_e13, "e14": build_e14,
}
