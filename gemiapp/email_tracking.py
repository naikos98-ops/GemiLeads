"""Brevo delivery/engagement webhook -- durable logging only, no business logic depends on it.

Mirrors the shape of gemiapp.billing's Stripe webhook handling (persist first, verify auth,
never trust an unauthenticated delivery) but is deliberately much simpler: there is no money
and no entitlement on the other side of an "opened" or "unsubscribed" event, so there is no
idempotency requirement and no per-event-type dispatch -- every event is just appended to
EmailEngagementEvent for the superadmin dashboard to aggregate.
"""

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import EmailEngagementEvent

logger = logging.getLogger(__name__)


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
