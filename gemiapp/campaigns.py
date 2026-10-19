"""Campaign landing pages and consent-safe analytics plumbing.

Two jobs, both deliberately small:

1. Attribution without storage. UTM parameters that arrive on a campaign landing page are
   carried forward *in the URL* of the signup CTA, never written to a cookie, the session or the
   database. GA4 (which only loads after consent, see includes/analytics.html) reads them from the
   page URL like any other campaign hit. A visitor who never consents leaves no trace at all.

2. One-shot analytics events. Some conversions happen on the server (an account is created, an
   email is verified, a Radar is saved) and the page that follows is the only place a browser
   can report them. ``queue_analytics_event`` parks the event in the session -- which, for those
   flows, already exists for the logged-in user or the signup POST -- and the next rendered page
   emits it through gtag *only if* gtag exists, i.e. only with consent. Nothing is sent
   server-to-server.

Every value that may reach GA4 is whitelisted: event names are fixed in code and parameter values
are either booleans, small integers, or tokens matching ``SAFE_TOKEN``. No email, name, ΑΦΜ,
search text or company identifier is ever an analytics parameter.
"""

import re
from urllib.parse import urlencode

UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

SESSION_KEY = "analytics_events"
MAX_QUEUED_EVENTS = 5

# The only event names server code may queue. Adding one is a deliberate act: document it in
# ANALYTICS_TRACKING_PLAN.md first.
ALLOWED_EVENTS = frozenset({
    "sign_up",              # GA4 recommended: account created (email form or social login)
    "email_verified",       # custom: account activated through the verification link
    "product_search",       # custom: dashboard opened with at least one filter applied
    "radar_created",        # custom: a Radar definition was saved
})

CAMPAIGNS = {
    "horeca": {"campaign_id": "gl_horeca", "template": "campaigns/horeca.html"},
    "insurance": {"campaign_id": "gl_insurance", "template": "campaigns/insurance.html"},
}


def clean_attribution(params):
    """Return only the UTM parameters whose values are short, plain tokens.

    Anything else -- an email someone pasted into utm_term, a URL, spaces, Greek text -- is
    dropped rather than escaped, so a malformed or abusive link cannot push free text into GA4
    or into our own HTML.
    """
    result = {}
    for key in UTM_KEYS:
        value = (params.get(key) or "").strip()
        if value and SAFE_TOKEN.match(value):
            result[key] = value
    return result


def attribution_query(params):
    """``?utm_...`` suffix for an internal link, or "" when there is nothing to carry."""
    cleaned = clean_attribution(params)
    return "?" + urlencode(cleaned) if cleaned else ""


def _clean_params(params):
    clean = {}
    for key, value in (params or {}).items():
        if not SAFE_TOKEN.match(str(key)):
            continue
        if isinstance(value, bool) or (isinstance(value, int) and 0 <= value <= 10_000):
            clean[key] = value
        elif isinstance(value, str) and SAFE_TOKEN.match(value):
            clean[key] = value
    return clean


def analytics_event(name, params=None):
    if name not in ALLOWED_EVENTS:
        raise ValueError(f"Analytics event {name!r} is not in ALLOWED_EVENTS")
    return {"name": name, "params": _clean_params(params)}


def queue_analytics_event(request, name, params=None):
    """Park an event for the next rendered page. Bounded, so a loop cannot grow the session."""
    events = list(request.session.get(SESSION_KEY, []))[-(MAX_QUEUED_EVENTS - 1):]
    events.append(analytics_event(name, params))
    request.session[SESSION_KEY] = events


def pop_queued_analytics_events(request):
    session = getattr(request, "session", None)
    if session is None or SESSION_KEY not in session:
        return []
    return session.pop(SESSION_KEY, [])


def search_event_params(request):
    """Which kinds of filter a dashboard search used -- presence only, never the values."""
    get = request.GET
    return {
        "has_kad": bool(get.getlist("kad")),
        "has_region": bool(get.get("prefecture", "").strip()),
        "has_legal_form": bool(get.get("legal_type", "").strip()),
        "has_date": bool(get.get("date_from", "").strip() or get.get("date_to", "").strip()),
        "has_name": bool(get.get("q", "").strip()),
    }
