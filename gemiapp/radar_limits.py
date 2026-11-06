"""Automatic pause of Radars when the allowance shrinks (Founder decision 2026-11-06, for E7).

Before this, a downgrade (e.g. Business 10 -> Pro 5) left every Radar active and matching, because
``eligible_radars`` never looks at the limit; only ``radar_toggle`` refused to *activate* one more.

Rule, applied only when an entitlement change lowers the limit to a positive number:
  * keep the oldest active Radars up to the new limit, pause the newest ones beyond it;
  * a pause is exactly what the user's own pause does (``is_active=False``, criteria untouched,
    mirrored to the OrganizationRadar during the G4 transition);
  * nothing is ever resumed automatically: the user reactivates from the Radars page, within the limit.

Losing entitlement altogether (limit 0) is *not* handled here: Radars stay as they are and are simply
not evaluated without a subscription, so they work again as soon as one returns (E10 says exactly that).

Never raises into the caller (a Stripe webhook or a Superadmin action): each Radar is paused in its own
savepoint and a failure is logged and skipped.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction

from .models import CustomerRadar

logger = logging.getLogger(__name__)


def auto_pause_enabled() -> bool:
    return bool(getattr(settings, "RADAR_AUTO_PAUSE_ON_DOWNGRADE", True))


def pause_radars_over_limit(user, *, previous_limit: int, new_limit: int) -> list[int]:
    """Pause the newest active Radars beyond ``new_limit``; returns the ids paused."""
    if not auto_pause_enabled() or new_limit <= 0 or new_limit >= previous_limit:
        return []
    from .legacy_radar_sync import mirror_legacy_radar

    active = list(CustomerRadar.objects.filter(user=user, is_active=True, deleted_at__isnull=True)
                  .order_by("created_at", "pk"))
    paused = []
    for radar in reversed(active[new_limit:]):
        try:
            with transaction.atomic():
                radar.is_active = False
                radar.save(update_fields=["is_active", "updated_at"])
                mirror_legacy_radar(radar)
        except Exception:
            logger.exception("Could not pause Radar %s over the new limit", radar.pk)
            continue
        paused.append(radar.pk)
    if paused:
        logger.info("Paused %s Radar(s) of user %s: limit %s -> %s", len(paused), user.pk, previous_limit, new_limit)
    return paused
