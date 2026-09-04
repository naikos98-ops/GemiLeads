"""Cheap pre-send validation for cold-outreach addresses.

The ΓΕΜΗ register is the only source of these addresses, and nothing verifies the email
field at registration: a company types it once when it incorporates and never revisits it.
So the feed carries typos ("gmial.com"), addresses on domains that have since lapsed, and
one-off addresses opened for the incorporation and abandoned. Every one of those is a hard
bounce, and a hard-bounce rate above ~1% is what Gmail/Microsoft read as "this sender does
not clean its list" -- which lands the *deliverable* mail in spam.

This module answers the one question DNS can answer for free: can this domain receive mail
at all? A domain with no MX (and no A/AAAA fallback) cannot, so the address is dead and
worth suppressing before it ever reaches Brevo. It deliberately does NOT try to prove a
specific mailbox exists -- that needs SMTP probing, which is slow, unreliable, and gets the
prober blocklisted. Expect this to catch the lapsed-domain and typo'd-domain share of the
bounces; "right domain, wrong mailbox" still bounces and is caught afterwards by the
webhook suppression path in gemiapp.email_tracking.
"""

import logging
from functools import lru_cache

import dns.exception
import dns.resolver
from django.conf import settings

logger = logging.getLogger(__name__)

# Per-lookup limits. A queue click resolves one domain per distinct company domain in the
# batch, so the whole batch's worst case is bounded by (distinct domains x timeout).
_DNS_TIMEOUT = 3.0
_DNS_LIFETIME = 5.0

# Verdicts. Only DEAD is actionable -- UNKNOWN means DNS itself failed to answer (timeout,
# SERVFAIL, no nameserver reachable), which says nothing about the address and must never
# suppress it.
DELIVERABLE = "deliverable"
DEAD = "dead"
UNKNOWN = "unknown"


def _resolver():
    r = dns.resolver.Resolver()
    r.timeout = _DNS_TIMEOUT
    r.lifetime = _DNS_LIFETIME
    return r


@lru_cache(maxsize=4096)
def domain_status(domain):
    """Return DELIVERABLE / DEAD / UNKNOWN for one mail domain.

    Cached for the life of the process: a batch of newly-registered companies routinely
    shares a handful of domains (gmail.com, otenet.gr, the same accountant's domain across
    every company they filed), and the cache collapses those to one query each.
    """
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return DEAD

    # Tests must never depend on live DNS: a real lookup makes the suite slow, offline-
    # hostile, and dependent on whether some example domain happens to resolve today.
    # Settings default this off, and only the test settings turn it on.
    if getattr(settings, "SKIP_MX_VALIDATION", False):
        return DELIVERABLE

    resolver = _resolver()
    try:
        answers = resolver.resolve(domain, "MX")
    except dns.resolver.NXDOMAIN:
        # The domain itself does not exist. Nothing can be delivered to it, ever.
        return DEAD
    except dns.resolver.NoAnswer:
        # The domain exists but publishes no MX. RFC 5321 §5.1 says senders then fall back
        # to the A/AAAA record, and plenty of small Greek business domains rely on exactly
        # that, so this is not yet a dead address -- check for an address record before
        # calling it.
        return _implicit_mx_status(resolver, domain)
    except (dns.resolver.NoNameservers, dns.exception.Timeout) as exc:
        # DNS is broken or unreachable *for us*, right now. Saying DEAD here would suppress
        # a live address permanently on the strength of a transient network fault.
        logger.warning("MX lookup for %s could not be resolved (%s)", domain, exc)
        return UNKNOWN
    except dns.exception.DNSException:
        logger.exception("Unexpected DNS failure for %s", domain)
        return UNKNOWN

    # A single "." target is the RFC 7505 null MX: the domain explicitly states it accepts
    # no mail. Treat it as dead rather than deliverable.
    hosts = [str(r.exchange).strip(".") for r in answers]
    if not any(hosts):
        return DEAD
    return DELIVERABLE


def _implicit_mx_status(resolver, domain):
    for record in ("A", "AAAA"):
        try:
            if resolver.resolve(domain, record):
                return DELIVERABLE
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
        except (dns.resolver.NoNameservers, dns.exception.Timeout):
            return UNKNOWN
        except dns.exception.DNSException:
            logger.exception("Unexpected DNS failure for %s/%s", domain, record)
            return UNKNOWN
    return DEAD


def email_status(email):
    """Return DELIVERABLE / DEAD / UNKNOWN for one address, judged only by its domain."""
    address = (email or "").strip()
    if "@" not in address:
        return DEAD
    _, _, domain = address.rpartition("@")
    return domain_status(domain)


def is_undeliverable(email):
    """True only when the address is provably undeliverable.

    UNKNOWN deliberately returns False: the caller uses this to decide whether to suppress
    an address forever, and a DNS timeout is not evidence of a bad address.
    """
    return email_status(email) == DEAD
