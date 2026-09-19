"""D37 TASK_DUE generator: the one scheduled producer of in-app notifications (django-q, every day 08:00 Europe/Athens).

For every open task (``completed_at IS NULL``) with an assignee and ``due_on <= timezone.localdate()`` it creates, at
most once per (recipient, task, due date), a TASK_DUE notification for exactly ``task.assigned_to`` -- never another
recipient, and nothing when the task has none. Due-today and overdue are the same notification: a run that was
missed catches up, and a task still open tomorrow is not notified again. A completed task gets nothing; an existing
notification is never removed.

A notification is created only if the assignee may still read the task's opportunity under G5: an active membership of
the same organization whose role sees every opportunity, or a sales user the opportunity is still assigned to, and a
LIVE-backed opportunity. The role table is G5's own (``ROLE_CAPABILITIES``), so this is never a way around it.

Set-based and rerunnable: candidates are found in one query per batch, existing notifications are excluded in SQL, and
the partial unique constraint turns a concurrent duplicate into a no-op (``ignore_conflicts``). No network, no email.
"""

from dataclasses import dataclass

from django.apps import apps
from django.db import connection
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from .company_signals import LIVE
from .organization_access import NOTIFY, ROLE_CAPABILITIES, Capability

DUE_NOTIFICATION_BATCH = 500


def _model(name):
    return apps.get_model("gemiapp", name)


@dataclass(frozen=True)
class DueNotificationRun:
    today: object
    candidates: int
    created: int


def notification_schema_ready() -> bool:
    """Whether the notification table exists. A schema rolled back below 0054 while the daily schedule row still
    exists (``post_migrate`` never removes schedule rows) must produce a logged skip, not a failing task."""
    return _model("OrganizationNotification")._meta.db_table in connection.introspection.table_names()


def _assignee_may_read_opportunity() -> Q:
    """G5 read visibility of the task's opportunity for the task's assignee, expressed in SQL."""
    sees_all = sorted(role for role, caps in ROLE_CAPABILITIES.items()
                      if Capability.VIEW_ALL_OPPORTUNITIES in caps)
    sees_assigned = sorted(role for role, caps in ROLE_CAPABILITIES.items()
                           if Capability.VIEW_ASSIGNED_OPPORTUNITIES in caps
                           and Capability.VIEW_ALL_OPPORTUNITIES not in caps)
    return (Q(assigned_to__role__in=sees_all)
            | Q(assigned_to__role__in=sees_assigned, opportunity__assigned_to_id=F("assigned_to_id")))


def due_task_candidates(today):
    """Open, assigned, due tasks whose assignee may still read the opportunity and has no TASK_DUE for this date."""
    Notification = _model("OrganizationNotification")
    existing = Notification.objects.filter(notification_type=NOTIFY.TASK_DUE, task_id=OuterRef("pk"),
                                           recipient_id=OuterRef("assigned_to_id"), due_on=OuterRef("due_on"))
    return (_model("OpportunityTask").objects
            .filter(completed_at__isnull=True, assigned_to__isnull=False, due_on__lte=today,
                    assigned_to__organization_id=F("organization_id"),
                    opportunity__organization_id=F("organization_id"),
                    assigned_to__user__is_active=True, opportunity__latest_signal__mode=LIVE)
            .filter(_assignee_may_read_opportunity())
            .filter(~Exists(existing))
            .order_by("pk"))


def generate_due_task_notifications(*, today=None, batch_size: int = DUE_NOTIFICATION_BATCH) -> DueNotificationRun:
    """Create the missing TASK_DUE notifications for ``today`` (default: the configured local date). Rerunnable."""
    today = today or timezone.localdate()
    Notification = _model("OrganizationNotification")
    candidates = created = 0
    last = 0
    while True:
        batch = list(due_task_candidates(today).filter(pk__gt=last)
                     .values_list("pk", "organization_id", "assigned_to_id", "opportunity_id", "due_on")[:batch_size])
        if not batch:
            break
        last = batch[-1][0]
        candidates += len(batch)
        task_ids = [row[0] for row in batch]
        before = Notification.objects.filter(notification_type=NOTIFY.TASK_DUE, task_id__in=task_ids).count()
        Notification.objects.bulk_create(
            [Notification(organization_id=organization_id, recipient_id=recipient_id,
                          notification_type=NOTIFY.TASK_DUE, opportunity_id=opportunity_id, task_id=task_id,
                          due_on=due_on)
             for task_id, organization_id, recipient_id, opportunity_id, due_on in batch],
            ignore_conflicts=True)  # a concurrent run already created it: the unique constraint makes it a no-op
        created += Notification.objects.filter(notification_type=NOTIFY.TASK_DUE, task_id__in=task_ids).count() - before
    return DueNotificationRun(today=today, candidates=candidates, created=created)
