"""Export of the data that cannot be rebuilt from the ΓΕΜΗ API.

Shared by the management command and the Superadmin download button so the two cannot
drift apart: one implementation, one format, one set of tests.
"""

import json
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone

from gemiapp.models import (
    CustomerRadar,
    DigestPreference,
    PersonSuppression,
    RadarMatch,
    UserCompanyLead,
    UserSubscription,
)

FORMAT_VERSION = 1


def _plain(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _fields(instance, exclude=()):
    """Every concrete local field except the primary key and the named relations.

    Derived from the model rather than hardcoded so that a field added later is captured
    automatically: a backup that silently omits a new column is worse than no backup.
    """
    skip = set(exclude) | {"id"}
    return {
        f.name: _plain(getattr(instance, f.name))
        for f in instance._meta.local_fields
        if f.name not in skip and not f.is_relation
    }


def build_backup():
    """The critical tables, related rows referenced by natural key."""
    return {
        "format_version": FORMAT_VERSION,
        "exported_at": timezone.now().isoformat(),
        "note": "Δεν περιλαμβάνει δεδομένα ΓΕΜΗ: ανακτώνται από το API.",
        "users": [
            {**_fields(u, exclude=["password"]),
             # The hash, not the password. Restoring it lets people keep their existing
             # credentials instead of every account needing a reset.
             "password_hash": u.password}
            for u in User.objects.order_by("id")
        ],
        "subscriptions": [
            {"user": s.user.username, **_fields(s)}
            for s in UserSubscription.objects.select_related("user").order_by("id")
        ],
        # DigestPreference.activity_codes is a JSONField and is captured by _fields;
        # only CustomerRadar.activity_codes is a real m2m needing its own lookup.
        "digest_preferences": [
            {"user": p.user.username, **_fields(p)}
            for p in DigestPreference.objects.select_related("user").order_by("id")
        ],
        "radars": [
            {"user": r.user.username,
             "activity_codes": sorted(r.activity_codes.values_list("code", flat=True)),
             **_fields(r)}
            for r in CustomerRadar.objects.select_related("user")
                                          .prefetch_related("activity_codes").order_by("id")
        ],
        "leads": [
            {"user": l.user.username, "company": l.company.gemi_number, **_fields(l)}
            for l in UserCompanyLead.objects.select_related("user", "company").order_by("id")
        ],
        "radar_matches": [
            {"radar": m.radar.name, "user": m.radar.user.username,
             "company": m.company.gemi_number, "lead_user": m.lead.user.username,
             "lead_company": m.lead.company.gemi_number, **_fields(m)}
            for m in RadarMatch.objects.select_related("radar__user", "company",
                                                       "lead__user", "lead__company").order_by("id")
        ],
        "person_suppressions": [
            {"company": s.company.gemi_number if s.company_id else None, **_fields(s)}
            for s in PersonSuppression.objects.select_related("company").order_by("id")
        ],
    }



def backup_filename(today=None):
    return f"gemileads-critical-{(today or timezone.localdate()).isoformat()}.json"


def backup_json():
    return json.dumps(build_backup(), ensure_ascii=False, indent=2)
