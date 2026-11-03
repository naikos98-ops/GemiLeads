"""In-app support: the «Βοήθεια» form for signed-in users.

Native form, no third-party chat widget: no external script, no cookie, no new consent and no new
processor. The flow is deliberately "store first, notify second":

    POST /support/  ->  SupportMessage row  ->  worker task  ->  email to SUPPORT_INBOX

The row is the record. The email is only a notification, with Reply-To set to the user, so a
reply from the inbox goes straight back to them. If SMTP fails the row stays (notify_attempts,
last_notify_error) and the task reschedules itself; `manage.py resend_support_notifications`
covers anything that still did not go out.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.mail import EmailMessage
from django.http import JsonResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from .models import SupportMessage

logger = logging.getLogger(__name__)

SUPPORT_RATE = "5/h"
MAX_NOTIFY_ATTEMPTS = 5
RETRY_BASE_MINUTES = 10

PLAN_LABELS = {
    "free": "Free",
    "pro": "Pro",
    "business": "Business",
    "enterprise": "Enterprise",
    "custom": "Custom",
}


def plan_label(user) -> str:
    """The plan the user is effectively on, as a customer would name it."""
    subscription = getattr(user, "subscription", None)
    tier = subscription.effective_tier if subscription is not None else "free"
    return PLAN_LABELS.get(tier, tier.title())


def _clean_page_url(value: str) -> str:
    """Keep the page the user wrote from only when it is a path on this site."""
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith("//"):
        return ""
    if not url_has_allowed_host_and_scheme(value, allowed_hosts=None):
        return ""
    return value[:500]


def validate_support_post(data) -> tuple[dict, dict]:
    """Return (cleaned, errors) for the two fields the user types."""
    errors = {}
    category = (data.get("category") or "").strip()
    if category not in dict(SupportMessage.CATEGORIES):
        errors["category"] = "Διάλεξε κατηγορία."
    message = (data.get("message") or "").replace("\r\n", "\n").replace("\x00", "").strip()
    if len(message) < SupportMessage.MESSAGE_MIN_LENGTH:
        errors["message"] = f"Γράψε τουλάχιστον {SupportMessage.MESSAGE_MIN_LENGTH} χαρακτήρες."
    elif len(message) > SupportMessage.MESSAGE_MAX_LENGTH:
        errors["message"] = f"Το μήνυμα ξεπερνά τους {SupportMessage.MESSAGE_MAX_LENGTH} χαρακτήρες."
    return {"category": category, "message": message}, errors


def notification_subject(support: SupportMessage) -> str:
    return f"[Support][{support.plan}] {support.get_category_display()} — {support.email}"


def build_notification(support: SupportMessage) -> EmailMessage:
    body = render_to_string("emails/support_notification.txt", {"support": support})
    return EmailMessage(
        notification_subject(support),
        body,
        settings.DEFAULT_FROM_EMAIL,
        [settings.SUPPORT_INBOX],
        reply_to=[support.email],
        headers={"X-Mailin-Tag": f"support:{support.pk}"},
    )


def send_support_notification(message_id: int) -> bool:
    """Email the inbox about one stored message. Returns True once it has been sent.

    Idempotent: an already-notified message is never sent twice. A failure is recorded on the row
    and re-raised so the worker logs it; the caller decides whether to retry.
    """
    support = SupportMessage.objects.filter(pk=message_id).first()
    if support is None or support.notified_at is not None:
        return support is not None
    try:
        build_notification(support).send()
    except Exception as exc:
        SupportMessage.objects.filter(pk=support.pk).update(
            notify_attempts=support.notify_attempts + 1,
            last_notify_error=type(exc).__name__[:300],
        )
        logger.exception("Support notification for message %s failed (attempt %s).",
                         support.pk, support.notify_attempts + 1)
        raise
    SupportMessage.objects.filter(pk=support.pk).update(
        notified_at=timezone.now(), notify_attempts=support.notify_attempts + 1, last_notify_error="",
    )
    logger.info("Support notification for message %s sent.", support.pk)
    return True


def schedule_retry(message_id: int, attempts: int) -> bool:
    """Queue one more attempt later (10, 20, 30… minutes), up to MAX_NOTIFY_ATTEMPTS."""
    if attempts >= MAX_NOTIFY_ATTEMPTS:
        logger.error("Support message %s: notification gave up after %s attempts; the message is stored "
                     "and visible in Superadmin → Support.", message_id, attempts)
        return False
    from django_q.models import Schedule

    Schedule.objects.create(
        func="gemiapp.tasks.send_support_notification_task",
        args=str(message_id),
        schedule_type=Schedule.ONCE,
        repeats=1,
        next_run=timezone.now() + timedelta(minutes=RETRY_BASE_MINUTES * attempts),
        name=f"support-notification-retry-{message_id}-{attempts}",
    )
    return True


def queue_notification(message_id: int) -> None:
    """Hand the notification to the worker. Never sends inline: a slow relay must not hang the request.
    If the queue itself is unreachable the message is still stored; the resend command picks it up."""
    from django_q.tasks import async_task

    try:
        async_task("gemiapp.tasks.send_support_notification_task", message_id)
    except Exception:
        logger.exception("Could not queue the notification for support message %s.", message_id)


def _wants_json(request) -> bool:
    return request.headers.get("X-Requested-With") == "fetch" or "application/json" in request.headers.get("Accept", "")


def _back(request):
    target = request.POST.get("page_url") or ""
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        target = "/"
    return redirect(target)


@login_required
@require_POST
@ratelimit(key="user", rate=SUPPORT_RATE, block=False)
def support_message_create(request):
    from django.contrib import messages

    if getattr(request, "limited", False):
        error = "Έστειλες πολλά μηνύματα σε λίγο χρόνο. Δοκίμασε ξανά σε λίγο ή γράψε μας στο " \
                f"{settings.EMAIL_REPLY_TO}."
        if _wants_json(request):
            return JsonResponse({"ok": False, "errors": {"__all__": error}}, status=429)
        messages.error(request, error)
        return _back(request)

    cleaned, errors = validate_support_post(request.POST)
    if errors:
        if _wants_json(request):
            return JsonResponse({"ok": False, "errors": errors}, status=400)
        messages.error(request, " ".join(errors.values()))
        return _back(request)

    user = request.user
    support = SupportMessage.objects.create(
        user=user,
        email=user.email,
        plan=plan_label(user),
        category=cleaned["category"],
        message=cleaned["message"],
        page_url=_clean_page_url(request.POST.get("page_url")),
        user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:400],
    )
    queue_notification(support.pk)

    success = f"Το μήνυμά σου στάλθηκε. Θα σου απαντήσουμε στο {user.email}."
    if _wants_json(request):
        return JsonResponse({"ok": True, "message": success})
    messages.success(request, success)
    return _back(request)
