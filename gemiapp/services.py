from __future__ import annotations
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Any
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from .models import Company, DigestDelivery, DigestPreference, ImportRun


PAGE_SIZE = 200


def _get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    if not settings.GEMI_API_KEY:
        raise RuntimeError("Λείπει το GEMI_API_KEY από το περιβάλλον.")
    query = urllib.parse.urlencode(params, doseq=True)
    request = urllib.request.Request(
        f"{settings.GEMI_API_BASE}{path}?{query}",
        headers={"api_key": settings.GEMI_API_KEY, "Accept": "application/json", "User-Agent": "GEMI-Signal/1.0"},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code == 401:
                raise RuntimeError("Το GEMI_API_KEY δεν είναι έγκυρο.") from exc
            if exc.code in {429, 500, 502, 503, 504} and attempt < 3:
                time.sleep(2 ** attempt)
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
    return {
        "vat_number": str(item.get("afm") or ""), "name": str(item.get("coNameEl") or "Χωρίς επωνυμία"),
        "trade_names": " | ".join(str(x) for x in item.get("coTitlesEl") or []),
        "legal_type": _description(item.get("legalType")), "status": _description(status),
        "is_active": bool(status.get("isActive", item.get("isActive", True))),
        "incorporation_date": date.fromisoformat(str(item.get("incorporationDate"))[:10]),
        "gemi_office": _description(item.get("gemiOffice")), "prefecture": _description(item.get("prefecture")),
        "municipality": _description(item.get("municipality")), "city": str(item.get("city") or ""),
        "address": street, "postal_code": str(item.get("zipCode") or ""), "email": str(item.get("email") or ""),
        "website": str(item.get("url") or ""), "activities": activities, "raw_data": item,
    }


def import_for_date(target_date: date) -> ImportRun:
    run = ImportRun.objects.create(target_date=target_date)
    try:
        items = fetch_companies(target_date)
        created = updated = 0
        for item in items:
            _, was_created = Company.objects.update_or_create(gemi_number=str(item.get("arGemi")), defaults=company_defaults(item))
            created += int(was_created)
            updated += int(not was_created)
        run.fetched_count, run.created_count, run.updated_count = len(items), created, updated
        run.status, run.finished_at = "success", timezone.now()
        run.save()
    except Exception as exc:
        run.status, run.error_message, run.finished_at = "failed", str(exc), timezone.now()
        run.save()
        raise
    return run


def send_daily_digests(target_date: date) -> tuple[int, int]:
    sent = skipped = 0
    for preference in DigestPreference.objects.select_related("user").filter(frequency="daily", user__is_active=True):
        user = preference.user
        existing = DigestDelivery.objects.filter(user=user, digest_date=target_date).first()
        if not user.email or (existing and existing.status in {"sent", "skipped"}):
            skipped += 1
            continue
        companies = Company.objects.filter(incorporation_date=target_date)
        if preference.only_active:
            companies = companies.filter(is_active=True)
        if preference.legal_types:
            companies = companies.filter(legal_type__in=preference.legal_types)
        if preference.prefectures:
            companies = companies.filter(prefecture__in=preference.prefectures)
        companies = list(companies)
        if not companies and not preference.include_empty_digest:
            DigestDelivery.objects.update_or_create(user=user, digest_date=target_date, defaults={"status": "skipped", "company_count": 0, "error_message": ""})
            skipped += 1
            continue
        try:
            context = {"user": user, "companies": companies, "digest_date": target_date}
            text_body = render_to_string("emails/daily_digest.txt", context)
            html_body = render_to_string("emails/daily_digest.html", context)
            email = EmailMultiAlternatives(f"{len(companies)} νέες επιχειρήσεις · {target_date:%d/%m/%Y}", text_body, settings.DEFAULT_FROM_EMAIL, [user.email])
            email.attach_alternative(html_body, "text/html")
            email.send()
            DigestDelivery.objects.update_or_create(user=user, digest_date=target_date, defaults={"company_count": len(companies), "status": "sent", "error_message": ""})
            sent += 1
        except Exception as exc:
            DigestDelivery.objects.update_or_create(user=user, digest_date=target_date, defaults={"company_count": len(companies), "status": "failed", "error_message": str(exc)})
    return sent, skipped
