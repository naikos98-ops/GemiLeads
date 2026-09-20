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

from django.utils.log import AdminEmailHandler


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
