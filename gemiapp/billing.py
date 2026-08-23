import logging
import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from .models import UserSubscription

logger = logging.getLogger(__name__)


class HttpResponseSeeOther(HttpResponseRedirect):
    """303 See Other, which is what Stripe expects after a POST to its hosted pages."""

    status_code = 303

stripe.api_key = settings.STRIPE_SECRET_KEY

# Stripe's SignatureVerificationError moved to the top-level namespace; stripe.error is a legacy
# alias that will disappear in a future major release.
SignatureVerificationError = getattr(
    stripe, "SignatureVerificationError", getattr(stripe, "error", stripe).SignatureVerificationError
)


def tier_for_price_id(price_id):
    """Map a Stripe price id back to the subscription tier it grants."""
    if not price_id:
        return None
    mapping = {
        settings.STRIPE_PRICE_PRO: "pro",
        settings.STRIPE_PRICE_BUSINESS: "business",
        settings.STRIPE_PRICE_ENTERPRISE: "enterprise",
    }
    mapping.pop(None, None)
    return mapping.get(price_id)


# Tiers a user can buy through Stripe Checkout. "custom" is quote-based and has no price id.
SELECTABLE_TIERS = ("pro", "business", "enterprise")

# Where an anonymous visitor's plan choice is parked while they authenticate.
PENDING_TIER_SESSION_KEY = "pending_checkout_tier"


def price_id_for_tier(tier):
    return {
        "pro": settings.STRIPE_PRICE_PRO,
        "business": settings.STRIPE_PRICE_BUSINESS,
        "enterprise": settings.STRIPE_PRICE_ENTERPRISE,
    }.get(tier)


def pricing(request):
    context = {
        "stripe_price_pro": settings.STRIPE_PRICE_PRO,
        "stripe_price_business": settings.STRIPE_PRICE_BUSINESS,
        "stripe_price_enterprise": settings.STRIPE_PRICE_ENTERPRISE,
    }
    return render(request, "pricing.html", context)


@require_POST
def create_checkout_session(request):
    """Create a Stripe Checkout session for the posted tier.

    Deliberately NOT wrapped in @login_required. That decorator redirects an unauthenticated
    POST to /login/?next=<this URL>; after logging in the browser replays `next` as a GET, and
    this POST-only view answers 405. Instead an anonymous request parks the chosen tier in the
    session and sends the user to login, which resumes through the GET-safe resume_checkout view.
    """
    # Beta: no charge may be initiated while billing is off. Checked here rather than only in the
    # template, so a direct POST (or a stale page left open when the flag is flipped) cannot reach
    # Stripe. Everything below stays intact and starts working again the moment the flag is on.
    if not settings.LEGAL_BILLING_ACTIVE:
        messages.info(
            request,
            "Η εφαρμογή είναι σε beta και οι πληρωμές δεν έχουν ενεργοποιηθεί ακόμη. "
            "Επικοινώνησε μαζί μας για πρόσβαση.",
        )
        return redirect("pricing")

    tier = request.POST.get("tier")
    if tier not in SELECTABLE_TIERS:
        return redirect("pricing")

    if not request.user.is_authenticated:
        # Only a validated tier is stored, never a caller-supplied URL or price id.
        request.session[PENDING_TIER_SESSION_KEY] = tier
        return redirect(f"{reverse('login')}?next={reverse('resume_checkout')}")

    price_id = price_id_for_tier(tier)
    if not price_id:
        messages.error(request, "Το πλάνο δεν έχει ρυθμιστεί σωστά στο σύστημα.")
        return redirect("pricing")

    domain_url = f"{request.scheme}://{request.get_host()}"
    
    # Try to find existing Stripe Customer ID to avoid creating duplicates
    customer_id = None
    try:
        if request.user.subscription.stripe_customer_id:
            customer_id = request.user.subscription.stripe_customer_id
    except UserSubscription.DoesNotExist:
        pass

    try:
        session_args = {
            "payment_method_types": ["card"],
            "line_items": [{"price": price_id, "quantity": 1}],
            "mode": "subscription",
            "success_url": domain_url + reverse("dashboard") + "?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": domain_url + reverse("pricing"),
            "client_reference_id": str(request.user.id),
        }
        
        if customer_id:
            session_args["customer"] = customer_id
        else:
            session_args["customer_email"] = request.user.email

        checkout_session = stripe.checkout.Session.create(**session_args)
        return HttpResponseSeeOther(checkout_session.url)
    except Exception as e:
        logger.error(f"Stripe Checkout Error: {str(e)}")
        return render(request, "pricing.html", {"error": str(e)})


@login_required
def resume_checkout(request):
    """GET-safe landing point after authenticating with a plan already selected.

    Renders a page that immediately re-submits the intended tier as a real POST, so the
    state-changing endpoint is still only ever reached by POST with a valid CSRF token.
    """
    tier = request.session.pop(PENDING_TIER_SESSION_KEY, None)
    if tier not in SELECTABLE_TIERS or not settings.LEGAL_BILLING_ACTIVE:
        return redirect("pricing")
    return render(request, "billing/resume_checkout.html", {"tier": tier})


@login_required
@require_POST
def customer_portal(request):
    try:
        customer_id = request.user.subscription.stripe_customer_id
    except UserSubscription.DoesNotExist:
        customer_id = None

    if not customer_id:
        return redirect("pricing")

    domain_url = f"{request.scheme}://{request.get_host()}"
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=domain_url + reverse("settings"),
        )
        return HttpResponseSeeOther(session.url)
    except Exception as e:
        logger.error(f"Stripe Portal Error: {str(e)}")
        return redirect("settings")


@csrf_exempt
@require_POST
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
    event = None

    if not settings.STRIPE_WEBHOOK_SECRET:
        # Ignore webhooks if not configured
        return HttpResponse(status=400)

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        return HttpResponse(status=400)
    except SignatureVerificationError:
        return HttpResponse(status=400)

    # Handle the checkout.session.completed event
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("client_reference_id")
        stripe_customer_id = session.get("customer")
        stripe_subscription_id = session.get("subscription")

        if user_id:
            sub, _ = UserSubscription.objects.get_or_create(user_id=user_id)
            if stripe_customer_id:
                sub.stripe_customer_id = stripe_customer_id
            if stripe_subscription_id:
                sub.stripe_subscription_id = stripe_subscription_id
                try:
                    stripe_sub = stripe.Subscription.retrieve(stripe_subscription_id)
                    sub.status = stripe_sub.get("status", "inactive")
                    items = stripe_sub.get("items", {}).get("data", [])
                    if items:
                        tier = tier_for_price_id(items[0]["price"]["id"])
                        if tier:
                            sub.tier = tier
                except Exception as e:
                    logger.error(f"Error retrieving subscription {stripe_subscription_id}: {str(e)}")
                    sub.status = "inactive"
            sub.save()

    elif event["type"] in ["customer.subscription.updated", "customer.subscription.deleted"]:
        subscription = event["data"]["object"]
        stripe_subscription_id = subscription.get("id")
        status = subscription.get("status")

        try:
            sub = UserSubscription.objects.get(stripe_subscription_id=stripe_subscription_id)
            sub.status = status or ("canceled" if event["type"] == "customer.subscription.deleted" else "inactive")
            items = subscription.get("items", {}).get("data", [])
            if items:
                tier = tier_for_price_id(items[0]["price"]["id"])
                if tier:
                    sub.tier = tier
            sub.save()
        except UserSubscription.DoesNotExist:
            logger.error(f"Subscription {stripe_subscription_id} not found in DB")

    return HttpResponse(status=200)
