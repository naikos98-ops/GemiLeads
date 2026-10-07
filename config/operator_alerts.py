"""Operator alerting for production GEMI ingestion failures.

A GEMI ingestion failure is silent by construction. The A2 contract check raises on a response it refuses,
``import_for_date`` marks the ``ImportRun`` failed, nothing is written -- and no digest goes out that day.
Render's log, the ``ImportRun`` row and django-q's failure record all hold the evidence, but none of them tells
anyone; the failure is discovered when a customer notices the missing email. This module carries the one active
signal: an ERROR from the ingestion path reaches a human.

Sentry is one valid implementation of the same requirement and remains entirely optional -- nothing here, and
nothing in ``gemiapp``, depends on it. This deployment uses Django's own ``AdminEmailHandler`` over the SMTP
relay that already sends the digests, so no new service and no new credential is involved.

Lives in ``config`` rather than ``gemiapp`` on purpose: Django configures logging inside ``django.setup()``,
*before* the app registry is populated, so a handler named in ``LOGGING`` must not import models.
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar

from django.utils.log import AdminEmailHandler

# The one logger that still emails while a managed section is running (see ``managed_alerting``).
MANAGED_ALERT_LOGGER = "gemiapp.ingestion_alerts"
_managed = ContextVar("operator_alerts_managed", default=False)


@contextmanager
def managed_alerting():
    """Run a block whose failures are reported by an alert policy instead of one email per ERROR.

    Every ERROR on an operator logger normally emails the operators. That is right for the legacy import, which
    runs seven times a day. The scheduled ingestion cycle runs every few minutes: during a GEMI outage each run
    would log its own client ERROR and send its own email. Inside this block those records still reach the
    console handler -- the server log is unchanged -- but only ``MANAGED_ALERT_LOGGER``, the cycle's alert
    policy, may email. Outside the block nothing changes.
    """
    token = _managed.set(True)
    try:
        yield
    finally:
        _managed.reset(token)


class ManagedAlertFilter(logging.Filter):
    """Drops, from the email handler only, records a managed section's alert policy answers for."""

    def filter(self, record):
        return not _managed.get() or record.name == MANAGED_ALERT_LOGGER


class OperatorEmailHandler(AdminEmailHandler):
    """``AdminEmailHandler`` that can never change what the caller was doing.

    Django already sends with ``fail_silently``, which swallows the SMTP and socket errors a relay normally
    produces. Anything else -- a misconfigured backend, a DNS failure surfacing as an unexpected type, a relay
    returning something the backend does not model -- would otherwise escape ``emit()``, propagate out of
    ``logger.error()`` and into the ingestion path, masking the very exception being reported and changing the
    failure semantics of the import.

    So every failure of the alert is routed to ``logging.Handler.handleError``, which honours
    ``logging.raiseExceptions`` and writes to stderr without propagating. The alert is best effort; the failure
    it reports is not, and it still reaches stderr through the console handler beside this one.
    """

    def emit(self, record):
        try:
            super().emit(record)
        except Exception:
            self.handleError(record)


# Re-exported so the LOGGING dict and the tests name the same level in one place.
OPERATOR_ALERT_LEVEL = logging.ERROR
