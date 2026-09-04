from __future__ import annotations
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger(__name__)
from .kad import display_kad_code, normalize_kad_code, normalize_kad_search
from .models import (
    ActivityCode,
    Company,
    CompanyActivity,
    CustomerRadar,
    DigestDelivery,
    DigestPreference,
    ImportRun,
    RadarMatch,
    UserCompanyLead,
)


PAGE_SIZE = 200


@dataclass(frozen=True)
class MatchSummary:
    radars_checked: int = 0
    companies_checked: int = 0
    new_leads: int = 0
    new_matches: int = 0
    duplicate_matches: int = 0


def _get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    if not settings.GEMI_API_KEY:
        raise RuntimeError("Λείπει το GEMI_API_KEY από το περιβάλλον.")
    query = urllib.parse.urlencode(params, doseq=True)
    request = urllib.request.Request(
        f"{settings.GEMI_API_BASE}{path}?{query}",
        headers={"api_key": settings.GEMI_API_KEY, "Accept": "application/json", "User-Agent": "Gemi-Leads/1.0"},
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code == 401:
                raise RuntimeError("Το GEMI_API_KEY δεν είναι έγκυρο.") from exc
            if exc.code in {429, 500, 502, 503, 504} and attempt < 4:
                time.sleep(3 ** attempt)
                continue
            raise RuntimeError(f"GEMI API HTTP {exc.code}: {detail[:300]}") from exc
    raise RuntimeError("Το GEMI API δεν απάντησε.")


def _description(value: Any) -> str:
    return str(value.get("descr", "")) if isinstance(value, dict) else ""


def fetch_companies(target_date: date) -> list[dict[str, Any]]:
    target_iso = target_date.isoformat()
    found: dict[str, dict[str, Any]] = {}
    for active in (True, False):
        offset = 0
        while True:
            payload = _get("/companies", {
                "isActive": str(active).lower(), "resultsSortBy": "-incorporationDate",
                "resultsOffset": offset, "resultsSize": PAGE_SIZE,
            })
            results = payload.get("searchResults") or []
            if not results:
                break
            dates = []
            for item in results:
                item_date = str(item.get("incorporationDate") or "")[:10]
                if item_date:
                    dates.append(item_date)
                if item_date == target_iso:
                    found[str(item.get("arGemi"))] = item
            if dates and min(dates) < target_iso:
                break
            offset += len(results)
            total = int((payload.get("searchMetadata") or {}).get("totalCount") or 0)
            if len(results) < PAGE_SIZE or (total and offset >= total):
                break
    return list(found.values())


def company_defaults(item: dict[str, Any]) -> dict[str, Any]:
    activities = []
    for entry in item.get("activities") or []:
        activity = entry.get("activity") or {}
        activities.append({"code": activity.get("id", ""), "description": activity.get("descr", ""), "type": entry.get("type", "")})
    street = " ".join(filter(None, [str(item.get("street") or "").strip(), str(item.get("streetNumber") or "").strip()]))
    status = item.get("status") or {}

    raw_date_str = str(item.get("incorporationDate") or "")[:10]
    today = date.today()
    try:
        inc_date = date.fromisoformat(raw_date_str)
        if inc_date > today or inc_date.year < 1900:
            inc_date = today
    except (ValueError, TypeError):
        inc_date = today

    return {
        "vat_number": str(item.get("afm") or ""), "name": str(item.get("coNameEl") or "Χωρίς επωνυμία"),
        "trade_names": " | ".join(str(x) for x in item.get("coTitlesEl") or []),
        "legal_type": _description(item.get("legalType")), "status": _description(status),
        "is_active": bool(status.get("isActive", item.get("isActive", True))),
        "incorporation_date": inc_date,
        "gemi_office": _description(item.get("gemiOffice")), "prefecture": _description(item.get("prefecture")),
        "municipality": _description(item.get("municipality")), "city": str(item.get("city") or ""),
        "address": street, "postal_code": str(item.get("zipCode") or ""), "email": str(item.get("email") or ""),
        "website": str(item.get("url") or ""), "activities": activities, "raw_data": item,
    }


def sync_company_activities(company: Company, activities: list[dict[str, Any]]) -> None:
    records = []
    catalog_fallbacks = {}
    seen = set()
    for activity in activities:
        code = normalize_kad_code(activity.get("code"))
        activity_type = str(activity.get("type") or "")
        identity = (code, activity_type)
        if not code or identity in seen:
            continue
        seen.add(identity)
        description = str(activity.get("description") or "")
        catalog_fallbacks.setdefault(code, description)
        records.append(CompanyActivity(
            company=company,
            code=code,
            description=description,
            activity_type=activity_type,
        ))
    company.activity_records.all().delete()
    CompanyActivity.objects.bulk_create(records, batch_size=200)
    existing_codes = set(ActivityCode.objects.filter(normalized_code__in=catalog_fallbacks).values_list("normalized_code", flat=True))
    ActivityCode.objects.bulk_create([
        ActivityCode(
            code=display_kad_code(code),
            normalized_code=code,
            description=description or "Δραστηριότητα ΓΕΜΗ",
            search_text=normalize_kad_search(f"{display_kad_code(code)} {code} {description}"),
        )
        for code, description in catalog_fallbacks.items() if code not in existing_codes
    ], ignore_conflicts=True)


def filter_companies_for_radar(
    queryset,
    *,
    name_query="",
    prefectures=None,
    legal_types=None,
    only_active=True,
    activity_codes=None,
):
    if prefectures:
        queryset = queryset.filter(prefecture__in=prefectures)
    if legal_types:
        queryset = queryset.filter(legal_type__in=legal_types)
    if only_active:
        queryset = queryset.filter(is_active=True)
    if activity_codes:
        queryset = queryset.filter(activity_records__code__in=activity_codes).distinct()
    if name_query:
        # Matched against the denormalized, indexed accent-stripped name so the whole company
        # table never has to be pulled into Python just to answer a radar preview.
        queryset = queryset.filter(search_name__contains=normalize_kad_search(name_query))
    return queryset


def company_matches_radar(company: Company, radar: CustomerRadar) -> tuple[bool, dict[str, Any]]:
    if radar.only_active and not company.is_active:
        return False, {}
    if radar.name_query and normalize_kad_search(radar.name_query) not in normalize_kad_search(company.name):
        return False, {}
    if radar.prefectures and company.prefecture not in radar.prefectures:
        return False, {}
    if radar.legal_types and company.legal_type not in radar.legal_types:
        return False, {}

    wanted_codes = {item.normalized_code for item in radar.activity_codes.all()}
    company_codes = {item.code for item in company.activity_records.all()}
    matched_codes = sorted(wanted_codes & company_codes)
    if wanted_codes and not matched_codes:
        return False, {}

    reason = {
        "activity_codes": matched_codes,
        "prefecture": company.prefecture if radar.prefectures else "",
        "legal_type": company.legal_type if radar.legal_types else "",
        "name_query": radar.name_query if radar.name_query else "",
    }
    return True, reason


def eligible_radars():
    """Active, non-muted radars whose owner currently has a paid or complimentary entitlement."""
    radars = (
        CustomerRadar.objects.filter(is_active=True, deleted_at__isnull=True)
        .exclude(frequency="off")
        .select_related("user", "user__subscription")
        .prefetch_related("activity_codes")
    )
    return [
        radar for radar in radars
        if hasattr(radar.user, "subscription") and radar.user.subscription.has_entitlement
    ]


def _match_date(target_date: date, radars, import_run: ImportRun | None) -> tuple[int, int, int, int]:
    """Match every company incorporated on ``target_date`` against ``radars``.

    Returns (companies_checked, new_leads, new_matches, duplicate_matches).
    """
    companies = list(
        Company.objects.filter(incorporation_date=target_date).prefetch_related("activity_records")
    )
    new_leads = new_matches = duplicate_matches = 0

    for radar in radars:
        if target_date < timezone.localdate(radar.monitor_from):
            continue
        for company in companies:
            matched, reason = company_matches_radar(company, radar)
            if not matched:
                continue
            lead, lead_created = UserCompanyLead.objects.get_or_create(user=radar.user, company=company)
            new_leads += int(lead_created)
            _, match_created = RadarMatch.objects.get_or_create(
                radar=radar,
                company=company,
                defaults={
                    "lead": lead,
                    "import_run": import_run,
                    "matched_on": target_date,
                    "matched_activity_codes": reason["activity_codes"],
                    "match_reason": reason,
                },
            )
            new_matches += int(match_created)
            duplicate_matches += int(not match_created)

    return len(companies), new_leads, new_matches, duplicate_matches


@transaction.atomic
def match_imported_companies(import_run: ImportRun) -> MatchSummary:
    if import_run.status != "success":
        raise ValueError("Matching μπορεί να εκτελεστεί μόνο μετά από επιτυχημένο import.")

    radars = eligible_radars()
    companies_checked, new_leads, new_matches, duplicate_matches = _match_date(
        import_run.target_date, radars, import_run
    )

    return MatchSummary(
        radars_checked=len(radars),
        companies_checked=companies_checked,
        new_leads=new_leads,
        new_matches=new_matches,
        duplicate_matches=duplicate_matches,
    )


def match_companies_in_range(start_date: date, end_date: date) -> MatchSummary:
    """Run radar matching for every incorporation date in ``[start_date, end_date]``.

    Used after a bulk historical import. Each ``RadarMatch`` is stamped with the company's own
    incorporation date, so a backfill never makes months of historical leads look like today's
    matches (which would blast them into the next digest).
    """
    from datetime import timedelta

    radars = eligible_radars()
    companies_checked = new_leads = new_matches = duplicate_matches = 0

    current = start_date
    while current <= end_date:
        checked, leads, matches, duplicates = _match_date(current, radars, None)
        companies_checked += checked
        new_leads += leads
        new_matches += matches
        duplicate_matches += duplicates
        current += timedelta(days=1)

    return MatchSummary(
        radars_checked=len(radars),
        companies_checked=companies_checked,
        new_leads=new_leads,
        new_matches=new_matches,
        duplicate_matches=duplicate_matches,
    )


def import_for_date(target_date: date) -> ImportRun:
    run = ImportRun.objects.create(target_date=target_date)
    try:
        items = fetch_companies(target_date)
        created = updated = 0
        for item in items:
            defaults = company_defaults(item)
            company, was_created = Company.objects.update_or_create(gemi_number=str(item.get("arGemi")), defaults=defaults)
            sync_company_activities(company, defaults["activities"])
            created += int(was_created)
            updated += int(not was_created)
        run.fetched_count, run.created_count, run.updated_count = len(items), created, updated
        run.status, run.finished_at = "success", timezone.now()
        run.save()
        match_imported_companies(run)
    except Exception as exc:
        run.status, run.error_message, run.finished_at = "failed", str(exc), timezone.now()
        run.save()
        raise
    return run


NO_ENTITLEMENT = "No active subscription entitlement"
TOP_TIERS = ("enterprise", "custom")


def digest_skip_reason(user, frequency):
    """Why this user would not receive this digest, or None if they are a valid recipient.

    Shared by send_digests and the `digest_recipients` management command, so the diagnostic can
    never drift away from the behaviour it is meant to explain.
    """
    preference = getattr(user, "digest_preference", None)
    if preference is None:
        return "Δεν έχει DigestPreference (ο λογαριασμός δεν πέρασε ποτέ από signup/dashboard)"
    if preference.frequency == "off":
        return "Έχει απεγγραφεί (frequency=off)"
    if not user.is_active:
        return "Ανενεργός λογαριασμός (μη επιβεβαιωμένο email)"
    if not user.email:
        return "Ο λογαριασμός δεν έχει email"

    subscription = getattr(user, "subscription", None)
    if subscription is None or not subscription.has_entitlement:
        return NO_ENTITLEMENT
    if frequency == "intraday" and subscription.effective_tier not in TOP_TIERS:
        return f"Το intraday απαιτεί enterprise/custom (τρέχον tier: {subscription.effective_tier})"
    return None


def digest_email_tag(user_id: int, digest_date: date, frequency: str) -> str:
    """The `X-Mailin-Tag` value a digest email is sent with, and the same format
    gemiapp.email_tracking parses back out of incoming Brevo webhook events to match an
    engagement event (opened/clicked/unsubscribed/...) to the DigestDelivery row it's about.
    """
    return f"digest:{user_id}:{digest_date.isoformat()}:{frequency}"


def _send_digest_email(user, subject, body_text, body_html, tag):
    """Every digest send goes through here so the Brevo engagement tag is never forgotten on
    one call site but not another. `X-Mailin-Tag` is Brevo's (undocumented but confirmed
    working) SMTP-relay equivalent of the `tags` parameter their transactional API takes --
    they echo it back verbatim in every webhook event about this message."""
    message = EmailMultiAlternatives(
        subject, body_text, settings.DEFAULT_FROM_EMAIL, [user.email],
        headers={"X-Mailin-Tag": tag},
    )
    message.attach_alternative(body_html, "text/html")
    message.send()


def verification_email_tag(user_id: int) -> str:
    """The `X-Mailin-Tag` an account-verification email is sent with.

    Same purpose as digest_email_tag: without it a verification message is invisible in
    EmailEngagementEvent and every "the customer never got the email" report has to be
    chased through the Brevo dashboard by hand.
    """
    return f"verification:{user_id}"


def send_verification_email_now(user_id: int) -> bool:
    """Build and send the verification link for ``user_id``. Returns False when there is
    nothing to send (user gone, already verified, no address).

    The link is built from settings.BASE_URL rather than the signup request's Host header so
    this is callable from a worker, a management command or a view alike.
    """
    from django.contrib.auth.models import User
    from django.contrib.auth.tokens import default_token_generator
    from django.urls import reverse
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    user = User.objects.filter(pk=user_id).first()
    if user is None or user.is_active or not user.email:
        logger.info("Skipping verification email for user %s: nothing to send.", user_id)
        return False

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    verify_url = f"{settings.BASE_URL.rstrip('/')}{reverse('verify_email', kwargs={'uidb64': uid, 'token': token})}"
    context = {"verify_url": verify_url, "user": user}

    message = EmailMultiAlternatives(
        "Επιβεβαίωση email στο Gemi Leads",
        render_to_string("emails/verification.txt", context),
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
        headers={"X-Mailin-Tag": verification_email_tag(user.pk)},
    )
    message.attach_alternative(render_to_string("emails/verification.html", context), "text/html")
    message.send()
    logger.info("Verification email sent to user %s.", user.pk)
    return True


def send_digests(target_date: date, frequency: str = "daily") -> tuple[int, int]:
    if frequency == "weekly":
        raise ValueError("Το εβδομαδιαίο digest έχει καταργηθεί.")

    sent = skipped = 0
    start_date = end_date = target_date

    preferences = DigestPreference.objects.select_related("user", "user__subscription").exclude(frequency="off").filter(user__is_active=True)
    for preference in preferences:
        user = preference.user

        reason = digest_skip_reason(user, frequency)
        if reason:
            # Only the entitlement case is recorded, to keep the delivery log meaningful.
            if frequency != "intraday" and reason == NO_ENTITLEMENT:
                DigestDelivery.objects.update_or_create(
                    user=user, digest_date=target_date, frequency=frequency,
                    defaults={"status": "skipped", "company_count": 0, "error_message": NO_ENTITLEMENT}
                )
            skipped += 1
            continue

        if frequency != "intraday":
            existing = DigestDelivery.objects.filter(user=user, digest_date=target_date, frequency=frequency).first()
            if existing and existing.status in {"sent", "skipped"}:
                skipped += 1
                continue

        subscription = getattr(user, "subscription", None)
        last_sent_id = (subscription.last_sent_company_id or 0) if subscription else 0

        radar_filter = {
            "radar__user": user,
            "radar__is_active": True,
            "radar__deleted_at__isnull": True,
            "matched_on__gte": start_date,
            "matched_on__lte": end_date,
        }
        if frequency == "intraday":
            # A real-time alert must only carry what is new since the previous send. Without this
            # the same matched companies were repeated in all six emails of the day, while the
            # general section was already incremental.
            radar_filter["company__id__gt"] = last_sent_id
        else:
            radar_filter["radar__frequency"] = frequency

        matches = RadarMatch.objects.filter(**radar_filter).select_related("company", "radar")

        companies_dict = {}
        radar_company_ids = set()
        for match in matches:
            cid = match.company.id
            radar_company_ids.add(cid)
            if cid not in companies_dict:
                companies_dict[cid] = {"company": match.company, "radars": []}
            if match.radar.name not in companies_dict[cid]["radars"]:
                companies_dict[cid]["radars"].append(match.radar.name)

        company_data = list(companies_dict.values())
        company_data.sort(key=lambda x: (-x["company"].incorporation_date.toordinal() if x["company"].incorporation_date else 0, x["company"].name))

        # Fetch general companies (all incorporated on target date or unsent today's companies for intraday)
        general_companies = []
        if frequency == "intraday":
            gen_qs = (
                Company.objects
                .filter(incorporation_date=target_date, id__gt=last_sent_id)
                .exclude(id__in=radar_company_ids)
                .order_by("id")
            )
            general_companies = list(gen_qs)
        else:
            gen_qs = Company.objects.filter(incorporation_date=target_date).exclude(id__in=radar_company_ids).order_by("-id")
            general_companies = list(gen_qs[:100])

        if not company_data and not general_companies:
            if frequency == "intraday":
                skipped += 1
                continue
            has_radars = CustomerRadar.objects.filter(user=user, is_active=True, frequency=frequency, deleted_at__isnull=True).exists()
            if not (preference.include_empty_digest and has_radars):
                DigestDelivery.objects.update_or_create(user=user, digest_date=target_date, frequency=frequency, defaults={"status": "skipped", "company_count": 0, "error_message": ""})
                skipped += 1
                continue

        total_companies_count = len(company_data) + len(general_companies)
        try:
            from django.core.signing import TimestampSigner
            from django.urls import reverse
            signer = TimestampSigner()
            token = signer.sign(user.id)
            unsubscribe_url = f"{settings.BASE_URL}{reverse('unsubscribe', kwargs={'token': token})}"

            from .views import make_digest_export_token

            export_token = make_digest_export_token(user.id, target_date)
            export_url = f"{settings.BASE_URL}{reverse('digest_export_csv', kwargs={'token': export_token})}"

            context = {
                "user": user,
                "company_data": company_data,
                "general_companies": general_companies,
                "digest_date": target_date,
                "start_date": start_date,
                "end_date": end_date,
                "frequency": frequency,
                "unsubscribe_url": unsubscribe_url,
                "export_url": export_url,
            }
            if frequency == "intraday":
                subject = f"Gemi Leads Priority Alert · {total_companies_count} νέες επιχειρήσεις"
                txt_tmpl = "emails/daily_digest.txt"
                html_tmpl = "emails/daily_digest.html"
            else:
                subject = f"Gemi Leads · {total_companies_count} νέες επιχειρήσεις · {target_date:%d/%m/%Y}"
                txt_tmpl = "emails/daily_digest.txt"
                html_tmpl = "emails/daily_digest.html"

            body_text = render_to_string(txt_tmpl, context)
            body_html = render_to_string(html_tmpl, context)
            tag = digest_email_tag(user.id, target_date, frequency)

            if frequency == "intraday" and subscription is not None:
                # Intraday has no per-send DigestDelivery row to dedupe against (the unique
                # constraint allows only one row per user per day, but the alert fires up to
                # six times daily). Its only guard is subscription.last_sent_company_id -- and
                # a timed-out task that django-q retries would otherwise read the stale value,
                # rebuild the identical email and send it again (this is the "3 identical
                # emails" bug). Make the check-and-advance atomic: lock the row, and only send
                # if no concurrent/retried run has already moved the marker past what THIS
                # run built its email from.
                from .models import UserSubscription

                all_sent_ids = [c.id for c in general_companies] + list(radar_company_ids)
                max_sent_id = max(all_sent_ids) if all_sent_ids else last_sent_id
                with transaction.atomic():
                    locked = (
                        UserSubscription.objects.select_for_update()
                        .filter(pk=subscription.pk)
                        .first()
                    )
                    current_marker = (locked.last_sent_company_id or 0) if locked else 0
                    if current_marker != last_sent_id:
                        logger.info(
                            "Intraday digest for %s already sent by a concurrent run "
                            "(marker moved %s -> %s); skipping duplicate.",
                            user.email, last_sent_id, current_marker,
                        )
                        skipped += 1
                        continue
                    _send_digest_email(user, subject, body_text, body_html, tag)
                    if max_sent_id > current_marker:
                        locked.last_sent_company_id = max_sent_id
                        locked.save(update_fields=["last_sent_company_id"])
            else:
                _send_digest_email(user, subject, body_text, body_html, tag)

            DigestDelivery.objects.update_or_create(
                user=user, digest_date=target_date, frequency=frequency,
                defaults={"status": "sent", "company_count": total_companies_count, "error_message": ""},
            )
            sent += 1
        except Exception as exc:
            logger.exception("Failed sending digest to %s", user.email)
            DigestDelivery.objects.update_or_create(
                user=user, digest_date=target_date, frequency=frequency,
                defaults={"status": "failed", "company_count": total_companies_count, "error_message": str(exc)},
            )
            skipped += 1
    return sent, skipped


def send_user_yesterday_digest(user) -> int:
    """
    Sends all yesterday's matched GEMI records directly to the specified user's email address.
    Can be manually triggered by Superadmin for any user account.
    """
    from datetime import timedelta
    from django.core.signing import TimestampSigner
    from django.urls import reverse

    if not user.email:
        raise ValueError("Ο χρήστης δεν διαθέτει email διεύθυνση.")

    yesterday = timezone.localdate() - timedelta(days=1)
    matches = RadarMatch.objects.filter(
        radar__user=user,
        radar__is_active=True,
        radar__deleted_at__isnull=True,
        matched_on=yesterday,
    ).select_related("company", "radar")

    companies_dict = {}
    radar_company_ids = set()
    for match in matches:
        cid = match.company.id
        radar_company_ids.add(cid)
        if cid not in companies_dict:
            companies_dict[cid] = {"company": match.company, "radars": []}
        if match.radar.name not in companies_dict[cid]["radars"]:
            companies_dict[cid]["radars"].append(match.radar.name)

    company_data = list(companies_dict.values())
    company_data.sort(key=lambda x: (-x["company"].incorporation_date.toordinal() if x["company"].incorporation_date else 0, x["company"].name))

    general_companies = list(Company.objects.filter(incorporation_date=yesterday).exclude(id__in=radar_company_ids).order_by("-id")[:100])

    signer = TimestampSigner()
    token = signer.sign(user.id)
    unsubscribe_url = f"{settings.BASE_URL}{reverse('unsubscribe', kwargs={'token': token})}"

    total_count = len(company_data) + len(general_companies)
    context = {
        "user": user,
        "company_data": company_data,
        "general_companies": general_companies,
        "digest_date": yesterday,
        "start_date": yesterday,
        "end_date": yesterday,
        "frequency": "daily",
        "unsubscribe_url": unsubscribe_url,
    }
    subject = f"Gemi Leads · Χθεσινές Εγγραφές ({yesterday:%d/%m/%Y}) · {total_count} επιχειρήσεις"
    body_text = render_to_string("emails/daily_digest.txt", context)
    body_html = render_to_string("emails/daily_digest.html", context)

    tag = digest_email_tag(user.id, yesterday, "manual_yesterday")
    _send_digest_email(user, subject, body_text, body_html, tag)

    DigestDelivery.objects.update_or_create(
        user=user,
        digest_date=yesterday,
        frequency="manual_yesterday",
        defaults={"status": "sent", "company_count": total_count, "error_message": ""},
    )
    return total_count



def import_companies_since_date(start_date: date = date(2026, 1, 1)) -> tuple[int, int]:
    """
    Imports all companies from GEMI API starting from start_date up to today.
    Returns (created_count, updated_count).
    """
    start_iso = start_date.isoformat()
    created_count = 0
    updated_count = 0

    for active in (True, False):
        offset = 0
        while True:
            try:
                payload = _get("/companies", {
                    "isActive": str(active).lower(),
                    "resultsSortBy": "-incorporationDate",
                    "resultsOffset": offset,
                    "resultsSize": PAGE_SIZE,
                })
            except Exception as exc:
                logger.warning("Error fetching GEMI page at offset %d: %s. Sleeping before retry...", offset, exc)
                time.sleep(5)
                continue

            results = payload.get("searchResults") or []
            if not results:
                break

            stop_pagination = False
            with transaction.atomic():
                for item in results:
                    item_date_str = str(item.get("incorporationDate") or "")[:10]
                    if item_date_str and item_date_str < start_iso:
                        stop_pagination = True
                        break

                    defaults = company_defaults(item)
                    gemi_number = str(item.get("arGemi"))
                    company, created = Company.objects.update_or_create(
                        gemi_number=gemi_number,
                        defaults=defaults,
                    )
                    # Keep Company.activities in the same state as import_for_date: both paths
                    # write the identical company_defaults() shape rather than diverging on it.
                    sync_company_activities(company, defaults["activities"])

                    if created:
                        created_count += 1
                    else:
                        updated_count += 1

            if stop_pagination:
                break

            offset += len(results)
            total = int((payload.get("searchMetadata") or {}).get("totalCount") or 0)
            if len(results) < PAGE_SIZE or (total and offset >= total):
                break

            time.sleep(0.5)

    match_companies_in_range(start_date, date.today())
    return created_count, updated_count
