"""Brevo delivery/engagement webhook.

Mirrors the shape of gemiapp.billing's Stripe webhook handling (persist first, verify auth,
never trust an unauthenticated delivery) but is deliberately much simpler: there is no money
and no entitlement behind an "opened" event, so there is no idempotency requirement -- every
event is appended to EmailEngagementEvent for the superadmin dashboard to aggregate.

One event type does carry business logic: a hard bounce means the address does not exist,
so it is also written to OutreachSuppression. Without that, the same dead address is picked
straight back out of the ΓΕΜΗ feed on the next batch and bounced again, and a hard-bounce
rate above ~1% is precisely what makes Gmail/Microsoft treat the rest of our mail as spam.
Soft bounces are excluded on purpose -- a full mailbox or a greylisting MX is temporary, and
suppressing on one would throw away recipients who are perfectly reachable tomorrow.
"""

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import EmailEngagementEvent, OutreachSuppression

logger = logging.getLogger(__name__)

# Brevo names the same event differently depending on which side sent it: the SMTP relay
# emits snake_case, the transactional API camelCase. Both are mapped, same as in
# gemiapp.superadmin.services._ENGAGEMENT_BUCKETS.
#
# "blocked" is included because Brevo only reports it for an address already on its own
# blocklist (a previous hard bounce, or a spam complaint) -- our suppression list should
# agree with that rather than keep re-sending. Soft bounces are deliberately absent.
_SUPPRESSING_EVENTS = frozenset({"hardBounce", "hard_bounce", "blocked"})


def _token_is_valid(token):
    """Constant-time comparison -- the token is a shared secret, not a signature, so this is
    the only check standing between this endpoint and an unauthenticated write; a naive `==`
    would leak the secret one character at a time through timing."""
    configured = settings.BREVO_WEBHOOK_TOKEN
    if not configured:
        return False
    return hmac.compare_digest(token.encode("utf-8"), configured.encode("utf-8"))


def _normalize_tag(raw):
    """Return a single bare tag string from whatever Brevo sent.

    The SMTP relay wraps the ``X-Mailin-Tag`` header value in a JSON array before it
    reaches the webhook, so a message sent with tag ``outreach:17`` arrives as the
    literal string ``'["outreach:17"]'`` in the payload's ``tag`` field. Unwrap that
    (and the transactional ``tags`` list form) down to ``outreach:17`` so it matches
    the bare tag the superadmin dashboard looks it up by.
    """
    if isinstance(raw, list):
        return (raw[0] or "") if raw else ""
    if not raw:
        return ""
    text = str(raw).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text
        if isinstance(parsed, list):
            return (parsed[0] or "") if parsed else ""
        return str(parsed)
    return text


def _record_event(item):
    if not isinstance(item, dict):
        return
    event_type = item.get("event", "")
    email = item.get("email", "") or ""
    tag = _normalize_tag(item.get("tag")) or _normalize_tag(item.get("tags"))
    EmailEngagementEvent.objects.create(
        event_type=event_type, email=email, tag=tag, payload=item,
    )

    if event_type in _SUPPRESSING_EVENTS and email:
        _suppress_bounced(email, event_type)


def _suppress_bounced(email, event_type):
    """Add a hard-bounced address to the outreach suppression list.

    Deliberately does not fail the event: the engagement row is already written and is the
    audit record, so a suppression write that loses a race (get_or_create against a
    concurrent unsubscribe) must not cost us the event or make Brevo retry the batch.
    """
    normalized = OutreachSuppression.normalize(email)
    if not normalized:
        return
    try:
        _, created = OutreachSuppression.objects.get_or_create(email=normalized)
    except Exception:
        logger.exception("Could not suppress bounced address %r", normalized)
        return
    if created:
        logger.info("Suppressed %s after %s", normalized, event_type)


@csrf_exempt
@require_POST
def brevo_webhook(request, token):
    """Brevo's own "token-based authentication" option for outbound webhooks: the same token
    configured in the Brevo dashboard is appended to the URL and sent back with every delivery,
    unmodified -- there is no request signature to verify, only this shared secret."""
    if not _token_is_valid(token):
        return HttpResponse(status=403)

    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponse(status=400)

    # Brevo can deliver a single event object or a batch array depending on the webhook's own
    # configuration -- accept either rather than assuming one.
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        try:
            _record_event(item)
        except Exception:
            # One malformed item in a batch must not lose the rest, and this is an audit log,
            # not a money-affecting delivery -- log and move on rather than failing the whole
            # request (which would make Brevo retry the entire batch, including the items that
            # already succeeded).
            logger.exception("Could not record Brevo engagement event: %r", item)

    return JsonResponse({"status": "ok"})
