import json
import logging
import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from .models import UserSubscription

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY

def pricing(request):
    context = {
        "stripe_price_pro": settings.STRIPE_PRICE_PRO,
        "stripe_price_business": settings.STRIPE_PRICE_BUSINESS,
    }
    return render(request, "pricing.html", context)


@login_required
@require_POST
def create_checkout_session(request):
    tier = request.POST.get("tier")
    if tier == "pro":
        price_id = settings.STRIPE_PRICE_PRO
    elif tier == "business":
        price_id = settings.STRIPE_PRICE_BUSINESS
    else:
        return redirect("pricing")

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
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        logger.error(f"Stripe Checkout Error: {str(e)}")
        return render(request, "pricing.html", {"error": str(e)})


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
        return redirect(session.url, code=303)
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
    except ValueError as e:
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError as e:
        return HttpResponse(status=400)

    # Handle the checkout.session.completed event
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("client_reference_id")
        stripe_customer_id = session.get("customer")
        stripe_subscription_id = session.get("subscription")

        if user_id:
            sub, _ = UserSubscription.objects.get_or_create(user_id=user_id)
            sub.stripe_customer_id = stripe_customer_id
            sub.stripe_subscription_id = stripe_subscription_id
            # Get the subscription details from Stripe to determine the tier
            try:
                stripe_sub = stripe.Subscription.retrieve(stripe_subscription_id)
                price_id = stripe_sub["items"]["data"][0]["price"]["id"]
                if price_id == settings.STRIPE_PRICE_BUSINESS:
                    sub.tier = "business"
                elif price_id == settings.STRIPE_PRICE_PRO:
                    sub.tier = "pro"
                sub.save()
            except Exception as e:
                logger.error(f"Error retrieving subscription: {str(e)}")

    elif event["type"] in ["customer.subscription.updated", "customer.subscription.deleted"]:
        subscription = event["data"]["object"]
        stripe_subscription_id = subscription.get("id")
        stripe_customer_id = subscription.get("customer")
        status = subscription.get("status")

        try:
            sub = UserSubscription.objects.get(stripe_subscription_id=stripe_subscription_id)
            if status in ["canceled", "unpaid", "past_due"]:
                sub.tier = "free"
                sub.stripe_subscription_id = ""
            else:
                price_id = subscription["items"]["data"][0]["price"]["id"]
                if price_id == settings.STRIPE_PRICE_BUSINESS:
                    sub.tier = "business"
                elif price_id == settings.STRIPE_PRICE_PRO:
                    sub.tier = "pro"
                else:
                    sub.tier = "free"
            sub.save()
        except UserSubscription.DoesNotExist:
            logger.error(f"Subscription {stripe_subscription_id} not found in DB")

    return HttpResponse(status=200)
