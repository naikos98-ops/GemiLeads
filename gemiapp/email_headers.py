"""Headers shared by every email the product sends to a user.

Every message to a user leaves from notifications@send.gemileads.gr, an address nobody reads.
Without a Reply-To, a customer who answers a digest or a verification email writes into a void.
One helper, so a new send path cannot forget it on one call site and not another.
"""

from django.conf import settings


def user_reply_to() -> list[str]:
    """The Reply-To list for an email to a user: the product contact address, or nothing."""
    address = (getattr(settings, "EMAIL_REPLY_TO", "") or "").strip()
    return [address] if address else []
