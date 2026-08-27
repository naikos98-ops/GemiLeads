import datetime as dt
import hashlib
import json
import logging
import secrets
import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from .models import StripeWebhookEvent, UserSubscription

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


def _as_plain_dict(obj):
    """A real object returned directly by the installed Stripe SDK has no `.get()` (this SDK
    version's StripeObject only supports attribute/bracket access, not the dict-style `.get(`
    this whole module is written against) -- immediately convert it via Stripe's own recursive
    `to_dict()` so the rest of this module's `.get(`-based access keeps working. A no-op for
    already-plain objects (e.g. the plain dicts test doubles construct)."""
    to_dict = getattr(obj, "to_dict", None)
    return to_dict() if callable(to_dict) else obj


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


# Session key prefix for the per-(session, tier) checkout attempt nonce. One nonce per tier,
# not one for the whole session, so a Pro attempt and a Business attempt from the same visitor
# never share an idempotency scope (Phase 3 test 3).
_CHECKOUT_ATTEMPT_SESSION_PREFIX = "checkout_attempt_nonce:"


def _checkout_attempt_nonce(request, tier):
    """A random, server-controlled identifier for one logical checkout attempt at this tier.

    Kept in the session (not a new persistence subsystem) and reused across retries of the SAME
    attempt -- an accidental double submit or a network-level retry of the same POST reads the
    nonce this function already wrote and gets it back unchanged. A genuinely new attempt (see
    _conclude_checkout_attempt below) has nothing left in the session to reuse, so it mints a
    fresh one. The nonce alone determines nothing about entitlement or price: it never leaves
    the server except folded into the opaque idempotency key derived from it below.
    """
    session_key = f"{_CHECKOUT_ATTEMPT_SESSION_PREFIX}{tier}"
    nonce = request.session.get(session_key)
    if not nonce:
        nonce = secrets.token_urlsafe(24)
        request.session[session_key] = nonce
    return nonce


def _conclude_checkout_attempt(request, tier):
    """Rotate out the nonce for this tier once a Checkout Session has actually been created.

    Deliberately NOT called when stripe.checkout.Session.create raises: a failure is ambiguous
    -- Stripe may or may not have processed the request before the error reached us -- and the
    correct, Stripe-recommended response to that ambiguity is to retry with the SAME idempotency
    key, not mint a new one. Only a confirmed creation means "this attempt is over," so only that
    path clears the nonce, letting a later, distinct attempt at the same tier get a fresh key
    instead of being silently deduplicated against a stale one.
    """
    request.session.pop(f"{_CHECKOUT_ATTEMPT_SESSION_PREFIX}{tier}", None)


def _stripe_idempotency_key(user_id, tier, nonce):
    """Deterministic and opaque: same (user, tier, nonce) always folds to the same key, so a
    retry of the same attempt reaches Stripe with the same key, while two different users, or
    the same user's Pro vs Business attempt, can never collide. Contains no Stripe secret
    material and nothing sensitive -- safe to log if that's ever needed for support/debugging.
    """
    raw = f"checkout:{user_id}:{tier}:{nonce}"
    return "ck_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


# Stripe subscription statuses that represent a still-live lifecycle: Stripe (or the customer,
# through Stripe's own retry/resume tools -- update a card, come back from a trial, unpause) can
# still renew, recover, or fix one of these without anyone starting an unrelated second
# subscription. Deliberately broader than UserSubscription.ALLOWED_PAID_STATUSES ("active" only,
# which gates ENTITLEMENT): a past_due or trialing subscription grants no access yet, but a
# second Checkout Session for the same customer while one of these is outstanding would very
# likely double-bill them once Stripe or the customer resolves it. "paused" is Stripe's
# subscription-pause feature (billing suspended, subscription otherwise intact); not used by this
# app today, but a subscription in that state is exactly as "still there" as any of the others.
LIVE_STRIPE_SUBSCRIPTION_STATUSES = frozenset({
    "active", "trialing", "past_due", "unpaid", "incomplete", "paused",
})

# Terminal: Stripe will never again bill or auto-recover a subscription in one of these states on
# its own. A new Checkout Session is safe. "incomplete_expired" is what "incomplete" becomes if
# the very first payment is never completed within Stripe's window -- also a dead end, not a
# live subscription waiting to be fixed.
TERMINAL_STRIPE_SUBSCRIPTION_STATUSES = frozenset({
    "canceled", "incomplete_expired",
})


def _existing_subscription_blocks_new_checkout(sub):
    """True/False from local state alone when the local status is one we already recognise as
    live or terminal -- which covers every status this app itself ever writes, via the webhook,
    so it is the overwhelming majority of real accounts. Returns None when the local status is
    neither: the caller must resolve that case explicitly (see
    _resolve_ambiguous_subscription_block) rather than assume either way. No network call here.
    """
    if not sub or not sub.stripe_subscription_id:
        return False
    if sub.status in LIVE_STRIPE_SUBSCRIPTION_STATUSES:
        return True
    if sub.status in TERMINAL_STRIPE_SUBSCRIPTION_STATUSES:
        return False
    return None


def _resolve_ambiguous_subscription_block(sub):
    """Only reached when the local status is neither a known-live nor a known-terminal value --
    e.g. a webhook once wrote "inactive" because Stripe sent no status on some event. Fetches the
    CURRENT state from Stripe with no DB lock held during the network call, then applies the same
    terminal/live split to the fresh read. This is deliberately read-only: it decides whether to
    block, but does not write the fresh status back onto `sub` -- reconciling the local projection
    from outside the webhook is out of scope for this guard (see Phase 1/2 for that machinery).

    A Stripe failure, or a status that is STILL unrecognised after a fresh read, both block: this
    guard protects real money, so uncertainty must fail closed, never open into a second charge.
    """
    try:
        stripe_sub = _as_plain_dict(stripe.Subscription.retrieve(sub.stripe_subscription_id))
    except Exception as e:
        logger.error(
            "Could not verify existing Stripe subscription before checkout: user_id=%s "
            "subscription_id=%s reason=%s", sub.user_id, sub.stripe_subscription_id, e,
        )
        return True

    if stripe_sub.get("status") in TERMINAL_STRIPE_SUBSCRIPTION_STATUSES:
        return False
    return True


def _blocks_new_checkout(sub):
    """Full decision, resolving the None ("ambiguous") case from _existing_subscription_blocks_new_checkout."""
    decision = _existing_subscription_blocks_new_checkout(sub)
    if decision is None:
        decision = _resolve_ambiguous_subscription_block(sub)
    return decision


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

    # getattr with a default is safe here: the reverse OneToOne descriptor's DoesNotExist is
    # also an AttributeError, by Django's own design, precisely so this pattern works. In
    # practice every User gets a UserSubscription from the post_save signal, so `sub` is None
    # only defensively.
    sub = getattr(request.user, "subscription", None)

    # Phase 4 guard: an existing live Stripe subscription must not get a second, unrelated one.
    # Checked BEFORE touching the Phase 3 checkout-attempt nonce -- if this blocks, no attempt
    # was ever started, so there is nothing to conclude or leave dangling.
    if _blocks_new_checkout(sub):
        messages.info(
            request,
            "Έχεις ήδη ενεργή συνδρομή. Διαχειρίσου την υπάρχουσα συνδρομή σου από τις ρυθμίσεις.",
        )
        return redirect("settings")

    domain_url = f"{request.scheme}://{request.get_host()}"

    # Reuse the existing Stripe Customer, if any, to avoid creating duplicate Customers -- a
    # Customer alone (no live subscription, e.g. after the one above ended) never blocks checkout.
    customer_id = sub.stripe_customer_id if sub and sub.stripe_customer_id else None

    nonce = _checkout_attempt_nonce(request, tier)
    idempotency_key = _stripe_idempotency_key(request.user.id, tier, nonce)

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

        checkout_session = stripe.checkout.Session.create(**session_args, idempotency_key=idempotency_key)
        # Only a confirmed creation ends this attempt -- see _conclude_checkout_attempt's
        # docstring for why a failure below intentionally does NOT reach this line.
        _conclude_checkout_attempt(request, tier)
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


# --- Phase 5b: cancel-at-period-end / resume ----------------------------------------------
#
# Both endpoints below are deliberately synchronous-thin: they make exactly one Stripe write
# (cancel_at_period_end True/False) and report the outcome. Neither writes tier, status,
# active_until, scheduled_tier, scheduled_change_at, or anything entitlement-related -- the
# local UserSubscription projection is updated later, only by the webhook (Phase 5c). This
# mirrors Phase 2's checkout.session.completed philosophy exactly: the synchronous response to
# a write is never trusted as the entitlement source, only Stripe's own confirmed state is.

# Statuses where scheduling a cancellation is an unambiguous, safe self-service action: the
# subscription is fully healthy and paying normally. A strict subset of Phase 4's
# LIVE_STRIPE_SUBSCRIPTION_STATUSES.
CANCELABLE_STRIPE_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing"})

# A payment problem already needs resolving; a self-service cancel-at-period-end here doesn't
# fix anything and only adds a second thing to reason about on top of the payment issue. Route
# to the Billing Portal instead. "paused" is deliberately in neither this set nor the cancelable
# one: Phase 4 already noted it's unused by this app today and its semantics for a cancel/resume
# mutation specifically are unconfirmed, so it falls into the fail-closed "unknown" branch below.
PROBLEMATIC_PAYMENT_STRIPE_SUBSCRIPTION_STATUSES = frozenset({"past_due", "unpaid", "incomplete"})


def _classify_subscription_status(status):
    """Three-way split reused from the Phase 4 taxonomy: 'terminal' (Stripe will never bill or
    recover this on its own), 'live' (still there in some form), or 'unknown' (neither -- fails
    closed by the caller, never treated as safe to act on)."""
    if status in TERMINAL_STRIPE_SUBSCRIPTION_STATUSES:
        return "terminal"
    if status in LIVE_STRIPE_SUBSCRIPTION_STATUSES:
        return "live"
    return "unknown"


def _retrieve_live_subscription(sub, action):
    """Fresh, no-DB-lock read of the Stripe subscription this projection references -- cancel
    and resume never act on cached local status alone, unlike the Phase 4 checkout guard (which
    only escalates to a live read when the local status is already ambiguous). A mutation
    endpoint always reads first, because the local `status` field lags Stripe by definition
    (it's a webhook-updated projection) and staleness here directly controls a real-money write.

    Returns the Stripe subscription object, or None on any failure (network, API error, missing
    subscription) -- callers must treat None as fail-closed: no mutation, no fabricated state,
    and the local row (including stripe_subscription_id) is never touched either way.
    """
    try:
        return _as_plain_dict(stripe.Subscription.retrieve(sub.stripe_subscription_id))
    except Exception as e:
        logger.error(
            "Could not verify Stripe subscription before %s: user_id=%s subscription_id=%s reason=%s",
            action, sub.user_id, sub.stripe_subscription_id, e,
        )
        return None


# Session key prefix for cancel/resume attempt nonces -- a separate namespace from the Phase 3
# checkout-attempt nonces (_CHECKOUT_ATTEMPT_SESSION_PREFIX), so a checkout attempt and a
# cancel/resume attempt can never collide or share an idempotency scope, even accidentally.
_SUBSCRIPTION_ACTION_ATTEMPT_SESSION_PREFIX = "subscription_action_attempt_nonce:"


def _subscription_action_attempt_nonce(request, action):
    """Same pattern as Phase 3's _checkout_attempt_nonce, scoped by `action` ("cancel" or
    "resume") instead of by tier -- a cancel attempt and a resume attempt never share a nonce."""
    session_key = f"{_SUBSCRIPTION_ACTION_ATTEMPT_SESSION_PREFIX}{action}"
    nonce = request.session.get(session_key)
    if not nonce:
        nonce = secrets.token_urlsafe(24)
        request.session[session_key] = nonce
    return nonce


def _conclude_subscription_action_attempt(request, action):
    """Rotate out the nonce only once Stripe has confirmed the mutation -- not on failure, for
    the same reason as Phase 3's _conclude_checkout_attempt: a failure is ambiguous, and a retry
    of the same attempt must reuse the same idempotency key, never mint a new one."""
    request.session.pop(f"{_SUBSCRIPTION_ACTION_ATTEMPT_SESSION_PREFIX}{action}", None)


def _subscription_action_idempotency_key(user_id, action, nonce):
    """Distinct raw-string namespace ("subscription_action:" vs Phase 3's "checkout:") and key
    prefix ("sa_" vs "ck_"), so this can never collide with a checkout idempotency key even if,
    hypothetically, the same nonce value ever came up in both."""
    raw = f"subscription_action:{user_id}:{action}:{nonce}"
    return "sa_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@login_required
@require_POST
def cancel_subscription(request):
    """Schedule the user's own subscription to end at the current period's end.

    Never touches local tier/status/entitlement -- see the module comment above. subscription id
    always comes from request.user.subscription, never from the request body.
    """
    sub = getattr(request.user, "subscription", None)
    if not sub or not sub.stripe_subscription_id:
        messages.error(request, "Δεν βρέθηκε ενεργή συνδρομή για ακύρωση.")
        return redirect("settings")

    stripe_sub = _retrieve_live_subscription(sub, "cancel")
    if stripe_sub is None:
        messages.error(
            request,
            "Δεν ήταν δυνατή η επιβεβαίωση της συνδρομής σου αυτή τη στιγμή. Δοκίμασε ξανά σε λίγο.",
        )
        return redirect("settings")

    status = stripe_sub.get("status")
    if status in PROBLEMATIC_PAYMENT_STRIPE_SUBSCRIPTION_STATUSES:
        messages.info(
            request,
            "Η συνδρομή σου έχει εκκρεμότητα πληρωμής. Διόρθωσέ την πρώτα από τη «Διαχείριση "
            "Συνδρομής» και δοκίμασε ξανά.",
        )
        return redirect("settings")
    if status not in CANCELABLE_STRIPE_SUBSCRIPTION_STATUSES:
        # terminal (already over -- nothing to cancel), or still unrecognised even after this
        # fresh read: fail closed either way, never guess.
        messages.error(request, "Δεν είναι δυνατή αυτή τη στιγμή η ακύρωση της συνδρομής σου.")
        return redirect("settings")

    nonce = _subscription_action_attempt_nonce(request, "cancel")
    idempotency_key = _subscription_action_idempotency_key(request.user.id, "cancel", nonce)

    try:
        stripe.Subscription.modify(
            sub.stripe_subscription_id,
            cancel_at_period_end=True,
            idempotency_key=idempotency_key,
        )
    except Exception as e:
        logger.error(
            "Stripe cancel failed: user_id=%s subscription_id=%s reason=%s",
            request.user.id, sub.stripe_subscription_id, e,
        )
        messages.error(request, "Η ακύρωση δεν ολοκληρώθηκε. Δοκίμασε ξανά.")
        return redirect("settings")

    _conclude_subscription_action_attempt(request, "cancel")
    messages.success(
        request,
        "Η συνδρομή σου θα ακυρωθεί στο τέλος της τρέχουσας περιόδου χρέωσης. "
        "Διατηρείς πλήρη πρόσβαση μέχρι τότε.",
    )
    return redirect("settings")


@login_required
@require_POST
def resume_subscription(request):
    """Undo a scheduled cancel-at-period-end, if the subscription hasn't actually ended yet.

    Never touches local tier/status/entitlement -- see the module comment above.
    """
    sub = getattr(request.user, "subscription", None)
    if not sub or not sub.stripe_subscription_id:
        messages.error(request, "Δεν βρέθηκε συνδρομή για επαναφορά.")
        return redirect("settings")

    stripe_sub = _retrieve_live_subscription(sub, "resume")
    if stripe_sub is None:
        messages.error(
            request,
            "Δεν ήταν δυνατή η επιβεβαίωση της συνδρομής σου αυτή τη στιγμή. Δοκίμασε ξανά σε λίγο.",
        )
        return redirect("settings")

    status = stripe_sub.get("status")
    if _classify_subscription_status(status) != "live":
        # terminal (already over), or unrecognised even after this fresh read.
        messages.error(request, "Δεν είναι δυνατή αυτή τη στιγμή η επαναφορά της συνδρομής σου.")
        return redirect("settings")

    if not stripe_sub.get("cancel_at_period_end"):
        messages.info(request, "Η συνδρομή σου είναι ήδη ενεργή, χωρίς προγραμματισμένη ακύρωση.")
        return redirect("settings")

    nonce = _subscription_action_attempt_nonce(request, "resume")
    idempotency_key = _subscription_action_idempotency_key(request.user.id, "resume", nonce)

    try:
        stripe.Subscription.modify(
            sub.stripe_subscription_id,
            cancel_at_period_end=False,
            idempotency_key=idempotency_key,
        )
    except Exception as e:
        logger.error(
            "Stripe resume failed: user_id=%s subscription_id=%s reason=%s",
            request.user.id, sub.stripe_subscription_id, e,
        )
        messages.error(request, "Η επαναφορά δεν ολοκληρώθηκε. Δοκίμασε ξανά.")
        return redirect("settings")

    _conclude_subscription_action_attempt(request, "resume")
    messages.success(request, "Η συνδρομή σου συνεχίζεται κανονικά, χωρίς διακοπή.")
    return redirect("settings")


# --- Phase 5d: immediate strict-upgrade with proration -------------------------------------
#
# Only Pro -> Business, Pro -> Enterprise, Business -> Enterprise. Same-tier and any downgrade
# are explicitly rejected here -- downgrades are Phase 5e (Subscription Schedule), never this
# path. Synchronous-thin exactly like cancel/resume (Phase 5b): exactly one Stripe write, never
# a second subscription, never a Checkout Session. Never writes tier/status/active_until/
# scheduled_*/entitlement locally -- the projection is updated later, only by the webhook,
# which already handles a price change on this subscription without any change of its own
# (Phase 5c's active_until wiring and Phase 1/2's tier extraction both already apply generically
# to any customer.subscription.updated event, upgrade-triggered or not).

TIER_RANK = {"pro": 1, "business": 2, "enterprise": 3}


def _is_strict_upgrade(current_tier, target_tier):
    current_rank = TIER_RANK.get(current_tier)
    target_rank = TIER_RANK.get(target_tier)
    if current_rank is None or target_rank is None:
        return False
    return target_rank > current_rank


def _price_id_of(price_field):
    """A Subscription's item.price comes back as a fully expanded Price object by default, but a
    SubscriptionSchedule phase's item.price does NOT -- it's just the price id string unless the
    caller explicitly expands it (this app never does). Handles both shapes so callers never have
    to know which endpoint they got `price_field` from."""
    if isinstance(price_field, dict):
        return price_field.get("id")
    return price_field


def _current_subscription_item(stripe_sub):
    """The single recurring item this app's subscriptions are built from (every Checkout
    Session this app creates has exactly one line item). Returns (item_id, tier, quantity), or
    (None, None, None) if the shape is not exactly one well-formed item with a recognised price
    -- callers must fail closed on that, never guess which item or tier was meant.
    """
    items = stripe_sub.get("items", {}).get("data", [])
    if len(items) != 1:
        return None, None, None
    item = items[0]
    item_id = item.get("id")
    price = item.get("price") or {}
    price_id = price.get("id")
    if not item_id or not price_id:
        return None, None, None
    tier = tier_for_price_id(price_id)
    if not tier:
        return None, None, None
    quantity = item.get("quantity") or 1
    return item_id, tier, quantity


_PLAN_CHANGE_ATTEMPT_SESSION_PREFIX = "plan_change_attempt_nonce:"


def _plan_change_attempt_nonce(request, target_tier):
    """Same pattern as Phase 3/5b's attempt nonces, in its own namespace (distinct prefix from
    both checkout and cancel/resume), scoped by target_tier the same way checkout is scoped by
    tier -- a Business attempt and an Enterprise attempt never share a nonce."""
    session_key = f"{_PLAN_CHANGE_ATTEMPT_SESSION_PREFIX}{target_tier}"
    nonce = request.session.get(session_key)
    if not nonce:
        nonce = secrets.token_urlsafe(24)
        request.session[session_key] = nonce
    return nonce


def _conclude_plan_change_attempt(request, target_tier):
    """Rotate out the nonce only once Stripe has confirmed the mutation -- not on failure, same
    reasoning as every other _conclude_*_attempt in this module: a failure is ambiguous, and a
    retry of the same attempt must reuse the same idempotency key."""
    request.session.pop(f"{_PLAN_CHANGE_ATTEMPT_SESSION_PREFIX}{target_tier}", None)


def _plan_change_idempotency_key(user_id, subscription_id, target_tier, nonce):
    """Scoped to user + subscription + target tier + nonce, with its own raw-string namespace
    ("plan_change:") and key prefix ("pc_") distinct from checkout ("checkout:"/"ck_") and
    cancel/resume ("subscription_action:"/"sa_"), so none of the three can ever collide."""
    raw = f"plan_change:{user_id}:{subscription_id}:{target_tier}:{nonce}"
    return "pc_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@login_required
@require_POST
def change_plan(request):
    """Immediate, prorated strict upgrade of the caller's own subscription.

    Only `target_tier` is ever read from the request; the current tier, the subscription id and
    the Stripe price id all come from a fresh, authoritative Stripe read -- never from the
    client, never from local cache alone.
    """
    target_tier = request.POST.get("target_tier")
    if target_tier not in SELECTABLE_TIERS:
        messages.error(request, "Μη έγκυρο πλάνο.")
        return redirect("settings")

    sub = getattr(request.user, "subscription", None)
    if not sub or not sub.stripe_subscription_id:
        messages.error(request, "Δεν βρέθηκε ενεργή συνδρομή για αναβάθμιση.")
        return redirect("settings")

    # A schedule-managed subscription (Phase 5a projection) is out of scope here entirely --
    # modifying it directly, outside the schedule, would conflict with whatever the schedule
    # already has planned. No implicit .release(): that decision belongs to Phase 5e/5f.
    if sub.scheduled_tier or sub.stripe_schedule_id:
        messages.info(
            request,
            "Υπάρχει ήδη προγραμματισμένη αλλαγή πλάνου στη συνδρομή σου. Επικοινώνησε μαζί μας "
            "αν θέλεις να την αλλάξεις πριν εφαρμοστεί.",
        )
        return redirect("settings")

    stripe_sub = _retrieve_live_subscription(sub, "change_plan")
    if stripe_sub is None:
        messages.error(
            request,
            "Δεν ήταν δυνατή η επιβεβαίωση της συνδρομής σου αυτή τη στιγμή. Δοκίμασε ξανά σε λίγο.",
        )
        return redirect("settings")

    status = stripe_sub.get("status")
    if status in PROBLEMATIC_PAYMENT_STRIPE_SUBSCRIPTION_STATUSES:
        messages.info(
            request,
            "Η συνδρομή σου έχει εκκρεμότητα πληρωμής. Διόρθωσέ την πρώτα από τη «Διαχείριση "
            "Συνδρομής» και δοκίμασε ξανά.",
        )
        return redirect("settings")
    if status not in CANCELABLE_STRIPE_SUBSCRIPTION_STATUSES:
        # Terminal, "paused", or still unrecognised even after this fresh read: fail closed.
        # CANCELABLE (active/trialing) is the same conservative "fully healthy" set Phase 5b
        # already uses for cancel -- an upgrade is no safer to allow outside it than a cancel is.
        messages.error(request, "Δεν είναι δυνατή αυτή τη στιγμή η αλλαγή πλάνου.")
        return redirect("settings")

    if stripe_sub.get("cancel_at_period_end"):
        # Conservative Phase 5d choice: never implicitly resume-and-upgrade in one operation.
        messages.info(
            request,
            "Η συνδρομή σου έχει προγραμματισμένη ακύρωση. Πάτησε πρώτα «Συνέχιση συνδρομής» "
            "και μετά δοκίμασε ξανά την αναβάθμιση.",
        )
        return redirect("settings")

    item_id, current_tier, quantity = _current_subscription_item(stripe_sub)
    if not item_id or not current_tier:
        logger.error(
            "Cannot determine current subscription item/tier for plan change: "
            "user_id=%s subscription_id=%s",
            request.user.id, sub.stripe_subscription_id,
        )
        messages.error(request, "Δεν είναι δυνατή αυτή τη στιγμή η αλλαγή πλάνου.")
        return redirect("settings")

    if current_tier != sub.tier:
        # Local projection disagrees with Stripe's own current price -- the same "never resolve
        # ambiguity by guessing" principle as Phase 4's local/Stripe mismatch handling. The
        # webhook reconciles sub.tier on its own; this request simply doesn't proceed on
        # inconsistent information.
        logger.error(
            "Local tier does not match Stripe's current price for plan change: user_id=%s "
            "subscription_id=%s local_tier=%s stripe_tier=%s",
            request.user.id, sub.stripe_subscription_id, sub.tier, current_tier,
        )
        messages.error(request, "Δεν είναι δυνατή αυτή τη στιγμή η αλλαγή πλάνου. Δοκίμασε ξανά σε λίγο.")
        return redirect("settings")

    if target_tier == current_tier:
        messages.info(request, "Αυτό είναι ήδη το τρέχον πλάνο σου.")
        return redirect("settings")

    if not _is_strict_upgrade(current_tier, target_tier):
        # A genuine strict downgrade -- handled entirely by the Phase 5e branch below, which
        # schedules the change at the current period end instead of applying it now.
        return _perform_scheduled_downgrade(
            request, sub, stripe_sub, item_id, current_tier, quantity, target_tier,
        )

    target_price_id = price_id_for_tier(target_tier)
    if not target_price_id:
        logger.error("STRIPE_PRICE_* not configured for target tier=%s", target_tier)
        messages.error(request, "Το πλάνο δεν έχει ρυθμιστεί σωστά στο σύστημα.")
        return redirect("settings")

    nonce = _plan_change_attempt_nonce(request, target_tier)
    idempotency_key = _plan_change_idempotency_key(
        request.user.id, sub.stripe_subscription_id, target_tier, nonce,
    )

    try:
        stripe.Subscription.modify(
            sub.stripe_subscription_id,
            items=[{"id": item_id, "price": target_price_id}],
            proration_behavior="always_invoice",
            payment_behavior="error_if_incomplete",
            idempotency_key=idempotency_key,
        )
    except Exception as e:
        logger.error(
            "Stripe plan change failed: user_id=%s subscription_id=%s target_tier=%s reason=%s",
            request.user.id, sub.stripe_subscription_id, target_tier, e,
        )
        messages.error(
            request,
            "Η αναβάθμιση δεν ολοκληρώθηκε -- πιθανό πρόβλημα πληρωμής. Δοκίμασε ξανά ή "
            "ενημέρωσε τη μέθοδο πληρωμής σου από τη «Διαχείριση Συνδρομής».",
        )
        return redirect("settings")

    _conclude_plan_change_attempt(request, target_tier)
    messages.success(
        request,
        "Η αναβάθμιση του πλάνου σου ξεκίνησε. Θα ενεργοποιηθεί μόλις επιβεβαιωθεί η πληρωμή.",
    )
    return redirect("settings")


# --- Phase 5e: scheduled downgrade at period end, via Stripe Subscription Schedule ----------
#
# Business -> Pro, Enterprise -> Business, Enterprise -> Pro. Never applied now: the current
# (higher) tier keeps billing and keeps entitlement until the current period genuinely ends,
# then Stripe itself flips the price -- no cron, no local timer, no second Subscription.modify.
# Exactly like every other billing mutation in this module, this never writes scheduled_tier/
# scheduled_change_at/stripe_schedule_id/tier/status/entitlement locally on success OR failure;
# only the webhook (subscription_schedule.updated for the projection, customer.subscription.
# updated for the eventual tier change itself) ever writes those fields.

# Two Stripe writes make up one logical downgrade attempt (create the schedule, then set its
# phases) -- they need two DIFFERENT idempotency keys (Stripe keys are per-operation), both
# deterministically derived from the SAME attempt nonce so a retry of the whole attempt reuses
# both consistently. Own session namespace, distinct from checkout/upgrade/cancel/resume.
_DOWNGRADE_ATTEMPT_SESSION_PREFIX = "downgrade_attempt_nonce:"


def _downgrade_attempt_nonce(request, target_tier):
    session_key = f"{_DOWNGRADE_ATTEMPT_SESSION_PREFIX}{target_tier}"
    nonce = request.session.get(session_key)
    if not nonce:
        nonce = secrets.token_urlsafe(24)
        request.session[session_key] = nonce
    return nonce


def _conclude_downgrade_attempt(request, target_tier):
    request.session.pop(f"{_DOWNGRADE_ATTEMPT_SESSION_PREFIX}{target_tier}", None)


def _downgrade_idempotency_key(user_id, subscription_id, target_tier, nonce, operation):
    """`operation` is "schedule_create" or "schedule_update" -- folded into the key itself so
    the two different Stripe API calls that make up one downgrade attempt never share a key
    with each other, while both stay reproducible from the same (user, subscription, target,
    nonce) on a retry. Prefix "dg_" and raw-string namespace "downgrade:" keep this from ever
    colliding with checkout ("ck_"), upgrade ("pc_"), or cancel/resume ("sa_") keys.
    """
    raw = f"downgrade:{operation}:{user_id}:{subscription_id}:{target_tier}:{nonce}"
    return "dg_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _perform_scheduled_downgrade(request, sub, stripe_sub, item_id, current_tier, quantity, target_tier):
    target_price_id = price_id_for_tier(target_tier)
    if not target_price_id:
        logger.error("STRIPE_PRICE_* not configured for target tier=%s", target_tier)
        messages.error(request, "Το πλάνο δεν έχει ρυθμιστεί σωστά στο σύστημα.")
        return redirect("settings")

    current_price_id = price_id_for_tier(current_tier)
    boundary = _subscription_period_end(stripe_sub)
    if boundary is None:
        logger.error(
            "Cannot determine current period end for scheduled downgrade: user_id=%s subscription_id=%s",
            request.user.id, sub.stripe_subscription_id,
        )
        messages.error(request, "Δεν είναι δυνατή αυτή τη στιγμή η προγραμματισμένη υποβάθμιση.")
        return redirect("settings")
    boundary_ts = int(boundary.timestamp())

    nonce = _downgrade_attempt_nonce(request, target_tier)

    # Step 1: find or create the schedule. A fresh Subscription read always shows the schedule
    # it's currently managed by (Stripe's own `schedule` field) -- if one is already there, a
    # prior create() succeeded even though this client may have seen a timeout, so THIS is
    # reused rather than attempting to create a second one (which Stripe would reject anyway).
    # This is the recovery path for the "create succeeded, modify failed/ambiguous" scenario:
    # no local bookkeeping of "a create is in flight" is needed, because Stripe itself is that
    # bookkeeping, one authoritative read away.
    existing_schedule_id = stripe_sub.get("schedule")
    if existing_schedule_id:
        try:
            schedule = _as_plain_dict(stripe.SubscriptionSchedule.retrieve(existing_schedule_id))
        except Exception as e:
            logger.error(
                "Could not verify existing Stripe schedule: user_id=%s subscription_id=%s "
                "schedule_id=%s reason=%s",
                request.user.id, sub.stripe_subscription_id, existing_schedule_id, e,
            )
            messages.error(
                request,
                "Δεν ήταν δυνατή η επιβεβαίωση της προγραμματισμένης αλλαγής. Δοκίμασε ξανά σε λίγο.",
            )
            return redirect("settings")
    else:
        create_key = _downgrade_idempotency_key(
            request.user.id, sub.stripe_subscription_id, target_tier, nonce, "schedule_create",
        )
        try:
            schedule = _as_plain_dict(stripe.SubscriptionSchedule.create(
                from_subscription=sub.stripe_subscription_id,
                idempotency_key=create_key,
            ))
        except Exception as e:
            logger.error(
                "Stripe schedule create failed: user_id=%s subscription_id=%s target_tier=%s reason=%s",
                request.user.id, sub.stripe_subscription_id, target_tier, e,
            )
            # Deliberately no .release() attempt here: the failure is ambiguous (Stripe may have
            # created the schedule despite the client seeing an error), and guessing wrong would
            # either abandon a schedule that did get created or release one a concurrent retry
            # is about to use. The nonce is retained, so a retry reaches the branch above instead
            # of here, because the next fresh Subscription.retrieve() will show the schedule if
            # it exists.
            messages.error(request, "Η προγραμματισμένη υποβάθμιση δεν ξεκίνησε -- δοκίμασε ξανά.")
            return redirect("settings")

    # Step 2: verify the schedule is genuinely in the single-phase, freshly-created state this
    # code knows how to build on. Anything else (a schedule this app didn't create, or one
    # that's already been modified into a different shape by a previous, unrelated attempt)
    # fails closed rather than guessing which phase is "current."
    phases = schedule.get("phases", [])
    if len(phases) != 1:
        logger.error(
            "Unexpected schedule phase count before downgrade modify: user_id=%s schedule_id=%s "
            "phase_count=%s",
            request.user.id, schedule.get("id"), len(phases),
        )
        messages.error(request, "Δεν είναι δυνατή αυτή τη στιγμή η προγραμματισμένη υποβάθμιση.")
        return redirect("settings")

    current_phase = phases[0]
    current_phase_items = current_phase.get("items", [])
    current_phase_price = _price_id_of(current_phase_items[0].get("price")) if len(current_phase_items) == 1 else None
    if current_phase_price != current_price_id:
        logger.error(
            "Schedule's current phase price does not match the subscription's current price: "
            "user_id=%s schedule_id=%s",
            request.user.id, schedule.get("id"),
        )
        messages.error(request, "Δεν είναι δυνατή αυτή τη στιγμή η προγραμματισμένη υποβάθμιση.")
        return redirect("settings")

    # Explicit, narrow allowlist of what's carried forward from the existing phase into the
    # rebuilt current-phase params -- everything else this app never sets on a subscription it
    # creates (automatic_tax, billing_thresholds, transfer_data, on_behalf_of, trial*,
    # default_tax_rates, invoice_settings, add_invoice_items, application_fee_percent,
    # default_payment_method) is deliberately NOT forwarded, so it falls back to the schedule's
    # own defaults rather than being guessed at in a shape that risks a rejected API call.
    current_phase_params = {
        "items": [{"price": current_price_id, "quantity": quantity}],
        "start_date": current_phase.get("start_date"),
        "end_date": boundary_ts,
        "proration_behavior": current_phase.get("proration_behavior") or "none",
    }
    if current_phase.get("collection_method"):
        current_phase_params["collection_method"] = current_phase["collection_method"]
    if current_phase.get("metadata"):
        current_phase_params["metadata"] = dict(current_phase["metadata"])
    existing_discounts = current_phase.get("discounts") or []
    if existing_discounts:
        current_phase_params["discounts"] = [
            {k: v for k, v in d.items() if k in ("coupon", "discount", "promotion_code") and v}
            for d in existing_discounts
        ]

    future_phase_params = {
        "items": [{"price": target_price_id, "quantity": quantity}],
        "start_date": boundary_ts,
        # No refund/credit/immediate invoice for the downgrade itself -- it only takes effect at
        # the boundary, at which point there is nothing to prorate.
        "proration_behavior": "none",
        # No end_date: the last phase is open-ended, so the subscription keeps recurring
        # normally on the lower tier after the transition instead of the schedule trying to end.
    }

    update_key = _downgrade_idempotency_key(
        request.user.id, sub.stripe_subscription_id, target_tier, nonce, "schedule_update",
    )
    try:
        stripe.SubscriptionSchedule.modify(
            schedule["id"],
            # "release" (Stripe's own default) hands the subscription back to being a plain,
            # normal recurring subscription once the schedule has nothing further to do, rather
            # than cancelling it -- exactly the "keep recurring on the new tier" behaviour
            # wanted here, made explicit rather than left implicit.
            end_behavior="release",
            phases=[current_phase_params, future_phase_params],
            idempotency_key=update_key,
        )
    except Exception as e:
        logger.error(
            "Stripe schedule modify failed: user_id=%s schedule_id=%s target_tier=%s reason=%s",
            request.user.id, schedule.get("id"), target_tier, e,
        )
        messages.error(request, "Η προγραμματισμένη υποβάθμιση δεν ολοκληρώθηκε -- δοκίμασε ξανά.")
        return redirect("settings")

    _conclude_downgrade_attempt(request, target_tier)
    messages.success(
        request,
        "Η υποβάθμιση προγραμματίστηκε για το τέλος της τρέχουσας περιόδου χρέωσης. Διατηρείς "
        "το τρέχον πλάνο σου μέχρι τότε.",
    )
    return redirect("settings")


# --- Phase 5f: cancel / release a pending scheduled downgrade -------------------------------
#
# Undoes what Phase 5e scheduled: the underlying subscription keeps running, unchanged, on its
# CURRENT (higher) tier -- only the future phase is removed. Never .cancel()s anything, never
# touches Subscription.modify, never creates a new subscription or schedule. Synchronous-thin
# exactly like every other mutation in this module: one Stripe write, then a message -- local
# scheduled_tier/scheduled_change_at/stripe_schedule_id are cleared only by the webhook.

# Own session namespace, distinct from checkout/upgrade/downgrade/cancel/resume. A user can only
# ever have one pending scheduled downgrade at a time (Phase 5e already blocks creating a second
# one while one exists), so this doesn't need a target-tier dimension the way the others do.
_CANCEL_DOWNGRADE_ATTEMPT_SESSION_KEY = "cancel_downgrade_attempt_nonce"


def _cancel_downgrade_attempt_nonce(request):
    nonce = request.session.get(_CANCEL_DOWNGRADE_ATTEMPT_SESSION_KEY)
    if not nonce:
        nonce = secrets.token_urlsafe(24)
        request.session[_CANCEL_DOWNGRADE_ATTEMPT_SESSION_KEY] = nonce
    return nonce


def _conclude_cancel_downgrade_attempt(request):
    request.session.pop(_CANCEL_DOWNGRADE_ATTEMPT_SESSION_KEY, None)


def _cancel_downgrade_idempotency_key(user_id, subscription_id, schedule_id, nonce):
    """Prefix "cdr_" and raw-string namespace "cancel_downgrade:" keep this from ever colliding
    with checkout ("ck_"), upgrade ("pc_"), downgrade-schedule ("dg_"), or cancel/resume
    ("sa_") keys."""
    raw = f"cancel_downgrade:{user_id}:{subscription_id}:{schedule_id}:{nonce}"
    return "cdr_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@login_required
@require_POST
def cancel_scheduled_downgrade(request):
    """Releases the schedule managing a pending downgrade, so the subscription goes back to
    being a plain, normal recurring subscription on its CURRENT tier -- the future (lower-tier)
    phase never happens. Never writes scheduled_tier/scheduled_change_at/stripe_schedule_id or
    tier/status/entitlement locally; only the subscription_schedule.released/.canceled webhook
    does that (see _handle_subscription_schedule_released).
    """
    sub = getattr(request.user, "subscription", None)
    if not sub or not sub.stripe_subscription_id:
        messages.error(request, "Δεν βρέθηκε ενεργή συνδρομή.")
        return redirect("settings")

    if not (sub.stripe_schedule_id and sub.scheduled_tier and sub.scheduled_change_at):
        messages.info(request, "Δεν υπάρχει προγραμματισμένη αλλαγή πλάνου προς ακύρωση.")
        return redirect("settings")

    if sub.active_until:
        # A pending downgrade schedule and a pending cancellation should never coexist -- Phase
        # 5e already refuses to create a downgrade schedule while cancel_at_period_end=True is
        # set. If it's happened anyway (e.g. a webhook race), that is exactly the kind of
        # ambiguous local state this app never resolves by guessing: fail closed rather than
        # picking one to trust over the other.
        logger.error(
            "Ambiguous local state before cancel-scheduled-downgrade: user_id=%s "
            "subscription_id=%s has both stripe_schedule_id and active_until set",
            request.user.id, sub.stripe_subscription_id,
        )
        messages.error(request, "Δεν είναι δυνατή αυτή τη στιγμή η ακύρωση της προγραμματισμένης αλλαγής.")
        return redirect("settings")

    # Fresh verification -- never release based on local cache alone. Covers the boundary race
    # explicitly: if the transition already happened (status="completed"), this is NOT reported
    # as a successful cancellation, and no automatic corrective action (e.g. an implicit
    # upgrade back) is taken.
    try:
        schedule = _as_plain_dict(stripe.SubscriptionSchedule.retrieve(sub.stripe_schedule_id))
    except Exception as e:
        logger.error(
            "Could not verify Stripe schedule before cancel: user_id=%s schedule_id=%s reason=%s",
            request.user.id, sub.stripe_schedule_id, e,
        )
        messages.error(
            request,
            "Δεν ήταν δυνατή η επιβεβαίωση της προγραμματισμένης αλλαγής. Δοκίμασε ξανά σε λίγο.",
        )
        return redirect("settings")

    if schedule.get("subscription") != sub.stripe_subscription_id:
        logger.error(
            "Schedule does not belong to the expected subscription: user_id=%s schedule_id=%s "
            "expected_subscription=%s schedule_subscription=%s",
            request.user.id, sub.stripe_schedule_id, sub.stripe_subscription_id, schedule.get("subscription"),
        )
        messages.error(request, "Δεν είναι δυνατή αυτή τη στιγμή η ακύρωση της προγραμματισμένης αλλαγής.")
        return redirect("settings")

    status = schedule.get("status")
    if status == "completed":
        # The future phase has already taken effect -- releasing now would be meaningless (and
        # Stripe's own release() only accepts not_started/active anyway). Tell the truth instead
        # of a false "cancelled successfully"; do NOT attempt an automatic upgrade back.
        messages.info(
            request,
            "Η αλλαγή πλάνου έχει ήδη εφαρμοστεί -- δεν υπάρχει πλέον κάτι να ακυρωθεί. Αν θέλεις "
            "διαφορετικό πλάνο τώρα, επίλεξέ το ξανά.",
        )
        return redirect("settings")
    if status in ("released", "canceled"):
        # Something else already resolved this (e.g. a webhook from an earlier attempt already
        # landed) -- safe to just inform, not an error.
        messages.info(request, "Δεν υπάρχει πλέον προγραμματισμένη αλλαγή πλάνου.")
        return redirect("settings")
    if status not in ("active", "not_started"):
        # Stripe's release() itself only accepts these two statuses; anything else is
        # unrecognised even after this fresh read.
        logger.error(
            "Unrecognised schedule status before cancel: user_id=%s schedule_id=%s status=%s",
            request.user.id, sub.stripe_schedule_id, status,
        )
        messages.error(request, "Δεν είναι δυνατή αυτή τη στιγμή η ακύρωση της προγραμματισμένης αλλαγής.")
        return redirect("settings")

    phases = schedule.get("phases", [])
    if len(phases) != 2:
        logger.error(
            "Unexpected phase count on schedule before cancel: user_id=%s schedule_id=%s phase_count=%s",
            request.user.id, sub.stripe_schedule_id, len(phases),
        )
        messages.error(request, "Δεν είναι δυνατή αυτή τη στιγμή η ακύρωση της προγραμματισμένης αλλαγής.")
        return redirect("settings")

    future_items = phases[-1].get("items", [])
    future_price_id = _price_id_of(future_items[0].get("price")) if len(future_items) == 1 else None
    future_tier = tier_for_price_id(future_price_id) if future_price_id else None
    if future_tier != sub.scheduled_tier:
        # Local/Stripe mismatch -- never release blindly. The webhook/reconciliation remains the
        # authority on what scheduled_tier should actually be; this request just doesn't act on
        # inconsistent information.
        logger.error(
            "Schedule future phase tier does not match local scheduled_tier: user_id=%s "
            "schedule_id=%s local_scheduled_tier=%s schedule_future_tier=%s",
            request.user.id, sub.stripe_schedule_id, sub.scheduled_tier, future_tier,
        )
        messages.error(request, "Δεν είναι δυνατή αυτή τη στιγμή η ακύρωση της προγραμματισμένης αλλαγής.")
        return redirect("settings")

    nonce = _cancel_downgrade_attempt_nonce(request)
    idempotency_key = _cancel_downgrade_idempotency_key(
        request.user.id, sub.stripe_subscription_id, sub.stripe_schedule_id, nonce,
    )

    try:
        stripe.SubscriptionSchedule.release(sub.stripe_schedule_id, idempotency_key=idempotency_key)
    except Exception as e:
        logger.error(
            "Stripe schedule release failed: user_id=%s schedule_id=%s reason=%s",
            request.user.id, sub.stripe_schedule_id, e,
        )
        messages.error(request, "Η ακύρωση της προγραμματισμένης αλλαγής δεν ολοκληρώθηκε -- δοκίμασε ξανά.")
        return redirect("settings")

    _conclude_cancel_downgrade_attempt(request)
    messages.success(request, "Η προγραμματισμένη αλλαγή πλάνου ακυρώθηκε. Παραμένεις στο τρέχον πλάνο σου.")
    return redirect("settings")


def _event_payload(event):
    """Convert a Stripe event to a plain JSON-serialisable dict for storage.

    A real `stripe.Event` is a StripeObject, not a plain dict: json-encoding it directly would
    fail, so we go through Stripe's own `to_dict()` (recursively plain) when available. Test
    doubles pass plain dicts, which have no `to_dict` and are already storable as-is.

    Even `to_dict()`'s output can still hold non-JSON-native values nested inside it (Stripe
    represents some monetary/rate fields as Decimal) that Django's plain JSONField encoder
    can't serialise -- round-tripped through json with a str fallback to flatten anything
    exotic into a plain, storable value, since this is a raw audit record, not data this app
    parses back out of the DB.
    """
    to_dict = getattr(event, "to_dict", None)
    payload = to_dict() if callable(to_dict) else dict(event)
    return json.loads(json.dumps(payload, default=str))


def _claim_webhook_event(event):
    """Persist the event by Stripe's own event id before any business processing runs, and
    decide whether this delivery should actually run that processing.

    Returns (StripeWebhookEvent, should_process). should_process is False when the event was
    already handled (status processed/ignored) by an earlier delivery, or is currently being
    handled by another in-flight delivery of the same event id (status received) -- in both
    cases the caller must not touch Stripe or UserSubscription again and simply returns 200.
    A previously failed event is reset to "received" so this delivery retries it.
    """
    stripe_event_id = event.get("id")
    event_type = event.get("type", "")
    payload = _event_payload(event)

    with transaction.atomic():
        record, created = StripeWebhookEvent.objects.get_or_create(
            stripe_event_id=stripe_event_id,
            defaults={"event_type": event_type, "payload": payload, "status": "received"},
        )
        if created:
            return record, True

        # Lock the existing row so a duplicate delivery arriving at (almost) the same instant
        # can't read the same pre-lock status and decide to reprocess concurrently. Postgres
        # (production) enforces this with a real row lock; SQLite (the test database) has no
        # row-level locking and silently ignores FOR UPDATE, so this only guards against real
        # concurrency in production -- the tests below exercise the resulting status-transition
        # logic sequentially, which is what SQLite can actually verify.
        record = StripeWebhookEvent.objects.select_for_update().get(pk=record.pk)
        if record.status in ("processed", "ignored"):
            return record, False
        if record.status == "failed":
            record.event_type = event_type
            record.payload = payload
            record.status = "received"
            record.error_message = ""
            record.save(update_fields=["event_type", "payload", "status", "error_message"])
            return record, True
        # status == "received": another delivery already claimed this event id and has not
        # reached a terminal status yet. Do not process it a second time concurrently.
        return record, False


def _finalize_webhook_event(record, status, error_message=""):
    with transaction.atomic():
        record.status = status
        record.error_message = error_message
        record.processed_at = timezone.now()
        record.save(update_fields=["status", "error_message", "processed_at"])


def _extract_tier_from_items(items):
    """None when there is genuinely nothing to look at (missing/empty item list) -- there is
    simply nothing to update in that case, which is not an error. Raises on the first item if
    its shape doesn't match what Stripe is expected to send (no "price"/"id"): that ambiguity
    must become a failed, retryable webhook event, never a guessed or fabricated tier.
    """
    if not items:
        return None
    price_id = items[0]["price"]["id"]
    return tier_for_price_id(price_id)


def _handle_checkout_session_completed(event):
    """A Checkout Session itself carries no subscription status, so the CURRENT subscription
    state is always read fresh from Stripe rather than trusted from the event body.

    That read is a network call and happens with no DB transaction or lock open. The local
    write only happens once the read (and the shape of what it returned) has fully succeeded --
    a retrieve failure, or a malformed subscription/items shape, leaves the existing
    UserSubscription row completely untouched rather than downgrading it to a guess: nothing
    below this comment runs, and nothing is saved. The exception is intentionally left
    uncaught here; it propagates to stripe_webhook, which records the delivery as a failed,
    retryable webhook event instead of accepting it as processed.
    """
    session = event["data"]["object"]
    user_id = session.get("client_reference_id")
    stripe_customer_id = session.get("customer")
    stripe_subscription_id = session.get("subscription")

    if not user_id:
        return

    status = None
    tier = None
    if stripe_subscription_id:
        stripe_sub = _as_plain_dict(stripe.Subscription.retrieve(stripe_subscription_id))
        status = stripe_sub.get("status") or "inactive"
        tier = _extract_tier_from_items(stripe_sub.get("items", {}).get("data", []))

    with transaction.atomic():
        sub, _ = UserSubscription.objects.get_or_create(user_id=user_id)
        if stripe_customer_id:
            sub.stripe_customer_id = stripe_customer_id
        if stripe_subscription_id:
            sub.stripe_subscription_id = stripe_subscription_id
            sub.status = status
            if tier:
                sub.tier = tier
        sub.save()


def _stripe_timestamp_to_datetime(value):
    """Stripe timestamps are Unix seconds. Returns a timezone-aware UTC datetime, or None if
    `value` isn't a usable timestamp -- never a naive datetime, never a guess."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _subscription_period_end(subscription):
    """current_period_end of the subscription's single item. Audit finding from Phase 5's
    design (still true here): this SDK/API version moved current_period_end off the top-level
    Subscription object onto each SubscriptionItem, so it must be read from
    items.data[0].current_period_end, never from the subscription object directly."""
    items = subscription.get("items", {}).get("data", [])
    if not items:
        return None
    return _stripe_timestamp_to_datetime(items[0].get("current_period_end"))


def _subscription_active_until(subscription):
    """Phase 5c authoritative active_until derivation: "the known future date on which
    entitlement ends because a termination is already scheduled" -- nothing else.

    None whenever cancel_at_period_end is not True: a normal recurring subscription has no
    scheduled expiry, regardless of current_period_end (that's a renewal boundary, not an
    expiry -- writing it here would be exactly the second, conflicting meaning this field is
    explicitly not allowed to carry).

    When cancel_at_period_end IS True: `cancel_at` is authoritative when Stripe provides it
    explicitly; only if it's absent does this fall back to the item's current_period_end.

    Raises RuntimeError -- deliberately, not returning None -- if cancel_at_period_end=True but
    neither value can be extracted. Returning None there would tell the UI "nothing is
    scheduled" while Stripe is saying the opposite; the caller (_handle_subscription_updated_or_
    deleted) does not catch this, so it propagates to stripe_webhook's existing failure handling:
    the event is recorded failed and Stripe retries, exactly like any other malformed-payload
    case already handled by this module.
    """
    if not subscription.get("cancel_at_period_end"):
        return None

    cancel_at = _stripe_timestamp_to_datetime(subscription.get("cancel_at"))
    if cancel_at is not None:
        return cancel_at

    period_end = _subscription_period_end(subscription)
    if period_end is not None:
        return period_end

    raise RuntimeError(
        "cancel_at_period_end=True but neither cancel_at nor a usable item current_period_end "
        "could be extracted from the Stripe subscription payload"
    )


def _handle_subscription_updated_or_deleted(event):
    """Unlike checkout.session.completed, Stripe includes the full subscription object on these
    events, so no extra API call is made here. A malformed/missing items shape on a subscription
    we do track locally still propagates uncaught -- see _handle_checkout_session_completed's
    docstring for why: a failed, retryable event beats a guessed tier or status. The same is now
    true of active_until (see _subscription_active_until's docstring).

    tier/status/active_until are computed here, before any DB access, and written together in
    one small atomic block below -- either all three land, or (on an exception raised while
    computing any of them) none do. This deliberately does NOT touch complimentary access.

    Phase 5e addition: if this update's new tier is exactly the tier a scheduled downgrade
    (Phase 5e) was waiting for, that IS the transition completing -- Stripe just flipped the
    subscription's price at the schedule boundary. scheduled_tier/scheduled_change_at/
    stripe_schedule_id are cleared in the SAME atomic write, so the system is never left with a
    "pending" downgrade that has, in fact, already happened. Any update whose tier does not match
    a pending scheduled_tier leaves those three fields completely untouched.
    """
    subscription = event["data"]["object"]
    stripe_subscription_id = subscription.get("id")
    status = subscription.get("status")

    try:
        sub = UserSubscription.objects.get(stripe_subscription_id=stripe_subscription_id)
    except UserSubscription.DoesNotExist:
        logger.error(f"Subscription {stripe_subscription_id} not found in DB")
        return

    tier = _extract_tier_from_items(subscription.get("items", {}).get("data", []))

    if event["type"] == "customer.subscription.deleted":
        # Entitlement removal on real termination goes exclusively through `status` becoming
        # "canceled" below -- active_until is "future scheduled expiry" only, never a historical
        # marker, so a subscription that has actually ended has nothing left to schedule.
        active_until = None
    else:
        active_until = _subscription_active_until(subscription)

    transition_completes_scheduled_downgrade = bool(tier) and bool(sub.scheduled_tier) and tier == sub.scheduled_tier

    with transaction.atomic():
        sub.status = status or ("canceled" if event["type"] == "customer.subscription.deleted" else "inactive")
        if tier:
            sub.tier = tier
        sub.active_until = active_until
        if transition_completes_scheduled_downgrade:
            sub.scheduled_tier = None
            sub.scheduled_change_at = None
            sub.stripe_schedule_id = ""
        sub.save()


def _handle_subscription_schedule_updated(event):
    """Projects a Stripe Subscription Schedule's pending phase transition onto the local
    UserSubscription it belongs to. This handler never writes to Stripe -- it only reflects,
    into scheduled_tier/scheduled_change_at/stripe_schedule_id, what Stripe has already decided.

    Only ever expects the exact shape this app's own downgrade schedules produce: exactly two
    phases (the current one, ending at the real period boundary, and one future phase carrying
    the downgrade target). Anything else -- a schedule this app didn't build this way, or one
    caught mid-setup (e.g. created but not yet given its second phase) -- fails closed (failed,
    retryable) rather than guessing which phase is "the" future one; see the Phase 5e deliverable
    for why an intermediate create-only state isn't expected to reach this handler at all.
    """
    schedule = event["data"]["object"]
    schedule_subscription_id = schedule.get("subscription")

    try:
        sub = UserSubscription.objects.get(stripe_subscription_id=schedule_subscription_id)
    except UserSubscription.DoesNotExist:
        # Not necessarily an error: a schedule this app doesn't track (a different environment,
        # one created directly in the Stripe dashboard, ...) looks identical to this from here,
        # and retrying would never make the row appear -- same convention as
        # _handle_subscription_updated_or_deleted's own DoesNotExist handling above.
        logger.error(f"Schedule subscription {schedule_subscription_id} not found in DB")
        return

    phases = schedule.get("phases", [])
    if len(phases) != 2:
        raise RuntimeError(f"Expected exactly 2 phases on a downgrade schedule, got {len(phases)}")

    future_phase = phases[-1]
    future_items = future_phase.get("items", [])
    if len(future_items) != 1:
        raise RuntimeError("Malformed future phase items on subscription schedule")

    future_price_id = _price_id_of(future_items[0].get("price"))
    future_tier = tier_for_price_id(future_price_id)
    if not future_tier:
        raise RuntimeError(f"Unrecognised future phase price on subscription schedule: {future_price_id!r}")

    scheduled_change_at = _stripe_timestamp_to_datetime(future_phase.get("start_date"))
    if scheduled_change_at is None:
        raise RuntimeError("Missing/invalid future phase start_date on subscription schedule")

    with transaction.atomic():
        sub.scheduled_tier = future_tier
        sub.scheduled_change_at = scheduled_change_at
        sub.stripe_schedule_id = schedule.get("id") or ""
        sub.save()


def _handle_subscription_schedule_released(event):
    """Handles both subscription_schedule.released (an explicit .release() call -- e.g. from
    this app's own cancel_scheduled_downgrade) and subscription_schedule.canceled.

    Both mean the same thing for this app's local bookkeeping: there is no longer a pending
    scheduled tier change to track, so scheduled_tier/scheduled_change_at/stripe_schedule_id are
    cleared -- and ONLY those three. tier/status/active_until/entitlement are never touched here.

    These two events are handled identically on purpose, but they are not the same thing at
    Stripe's side: .release() explicitly leaves the underlying subscription running unchanged,
    while a schedule can also be moved to "canceled" as a side effect of the underlying
    subscription itself being cancelled outright. If that happened, the real entitlement
    consequence is reported independently via customer.subscription.deleted, which already
    updates status/entitlement on its own (unchanged by Phase 5f) -- this handler intentionally
    does not try to infer or duplicate that from a schedule event, only clean up its own fields.
    """
    schedule = event["data"]["object"]
    # A released schedule moves its subscription reference to released_subscription; a canceled
    # one may still carry it under subscription. Check both so this finds the right row either way.
    schedule_subscription_id = schedule.get("released_subscription") or schedule.get("subscription")

    try:
        sub = UserSubscription.objects.get(stripe_subscription_id=schedule_subscription_id)
    except UserSubscription.DoesNotExist:
        logger.error(f"Released/canceled schedule subscription {schedule_subscription_id} not found in DB")
        return

    if sub.stripe_schedule_id and sub.stripe_schedule_id != schedule.get("id"):
        # A newer schedule has already replaced this one locally (e.g. this event is a late
        # arrival for an older schedule) -- clearing now would wipe out legitimate, more recent
        # scheduled_tier data. Same class of out-of-order risk already documented for
        # customer.subscription.updated elsewhere in this module, not a new one.
        logger.error(
            "Ignoring stale schedule release/cancel event for %s -- local row now tracks %s",
            schedule.get("id"), sub.stripe_schedule_id,
        )
        return

    with transaction.atomic():
        sub.scheduled_tier = None
        sub.scheduled_change_at = None
        sub.stripe_schedule_id = ""
        sub.save()


def _event_subject_id(event):
    """Best-effort id of the Stripe object this event is about (a checkout session id, a
    subscription id, ...), safe to log on its own -- never the payload, never request headers."""
    try:
        return event.get("data", {}).get("object", {}).get("id")
    except AttributeError:
        return None


@csrf_exempt
@require_POST
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")

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

    # A real construct_event() return value is a live Stripe SDK object, not a dict -- converted
    # once here (recursively, including event["data"]["object"]) so every handler below can keep
    # using plain `.get(`/bracket access uniformly, on both real events and test doubles.
    event = _as_plain_dict(event)

    record, should_process = _claim_webhook_event(event)
    if not should_process:
        return HttpResponse(status=200)

    event_type = event.get("type")
    try:
        if event_type == "checkout.session.completed":
            _handle_checkout_session_completed(event)
        elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
            _handle_subscription_updated_or_deleted(event)
        elif event_type == "subscription_schedule.updated":
            _handle_subscription_schedule_updated(event)
        elif event_type in ("subscription_schedule.released", "subscription_schedule.canceled"):
            _handle_subscription_schedule_released(event)
        else:
            _finalize_webhook_event(record, "ignored")
            return HttpResponse(status=200)
    except Exception as exc:
        # Deliberately broad, and deliberately not narrowed to stripe.StripeError: a transient
        # Stripe-side failure (stripe.APIConnectionError, stripe.APIError, stripe.RateLimitError
        # -- all subclasses of stripe.StripeError in this SDK version) is exactly as much of a
        # reason to fail this delivery for retry as a malformed/unexpected event shape raising a
        # plain KeyError -- both mean "we cannot be sure what actually happened," and neither
        # may be allowed to fall back to a guessed UserSubscription state. Only the exception's
        # own str() is logged/stored -- never the request, headers, or full payload (the
        # verified payload is already durably kept on the StripeWebhookEvent row for audit).
        logger.error(
            "Stripe webhook processing failed: event_id=%s event_type=%s subject=%s reason=%s",
            record.stripe_event_id, record.event_type, _event_subject_id(event), exc,
        )
        _finalize_webhook_event(record, "failed", error_message=str(exc)[:2000])
        raise

    _finalize_webhook_event(record, "processed")
    return HttpResponse(status=200)
