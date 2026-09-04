import logging
from datetime import timedelta

from django.core.cache import caches
from django.utils import timezone

from gemiapp.services import import_for_date, send_digests

logger = logging.getLogger(__name__)


def _claim_pipeline_slot(name, ttl_seconds):
    """Atomically claim a per-slot lock in the shared (database) cache.

    Returns True for the one caller that got the slot, False for the rest. django-q's
    scheduler is not safe to run in more than one process against the same database -- and
    the web service runs the cluster on every instance via honcho, so scaling web to 2
    instances meant two schedulers each firing the intraday cron, i.e. two identical digest
    emails. This lock makes the pipeline body run once per slot no matter how many clusters
    are live. cache.add() is atomic and only succeeds when the key is absent.
    """
    slot = timezone.now().strftime("%Y%m%d%H")
    key = f"pipeline-slot:{name}:{slot}"
    # The shared (database) cache, not the per-process LocMem "default" -- the whole point is
    # that a second cluster on another instance sees the claim.
    return caches["shared"].add(key, "1", ttl_seconds)


def _claim_outreach_send_lock(ttl_seconds=1800):
    """Serialise outreach sending across every live cluster. Returns a release callable, or
    None when another run holds the lock.

    Unlike _claim_pipeline_slot this is not bucketed by the hour: the point is one sender at a
    time, not one run per slot, and it is released as soon as the run finishes. The TTL only
    bounds a worker that dies mid-send. Without this, a second deployed service picked up the
    same queue and both ran a full OUTREACH_DAILY_SEND_CAP budget.
    """
    key = "outreach-send-lock"
    if not caches["shared"].add(key, "1", ttl_seconds):
        return None
    return lambda: caches["shared"].delete(key)


def _pipeline_is_already_running(target_date):
    """True when an unfinished ImportRun for the same date is still in flight.

    Two concurrent pipelines contend for the same rows inside get_or_create and both stall, which
    is how a duplicated schedule row made every nightly run hit the task timeout.
    """
    from django.conf import settings
    from gemiapp.models import ImportRun

    # A run older than the task timeout must have been killed, so it no longer blocks anything.
    cutoff = timezone.now() - timedelta(seconds=settings.Q_CLUSTER.get("timeout", 1800))
    return ImportRun.objects.filter(
        target_date=target_date, status="running", started_at__gte=cutoff
    ).exists()


def run_daily_pipeline_task():
    try:
        # 23h TTL: the slot key must outlive this run but be gone before tomorrow's slot.
        if not _claim_pipeline_slot("daily", 23 * 3600):
            logger.info("Daily pipeline slot already claimed by another cluster; skipping.")
            return
        target = timezone.localdate() - timedelta(days=1)
        if _pipeline_is_already_running(target):
            logger.warning("Skipping daily pipeline: a run for %s is still in progress", target)
            return
        run = import_for_date(target)
        logger.info(f"Import: {run.created_count} νέες, {run.updated_count} ενημερωμένες")
        
        sent, skipped = send_digests(target, frequency="daily")
        logger.info(f"Daily Email: {sent} εστάλησαν, {skipped} παραλείφθηκαν")

    except Exception as exc:
        logger.error(f"Pipeline error: {exc}", exc_info=True)
        raise


def send_company_outreach_task(company_ids):
    """Send queued client-outreach emails. Enqueued by the Εύρεση Πελατών page.

    The web request only claims the rows (status="pending"); this worker does the SMTP
    round-trips, which are what used to time out the gunicorn worker.
    """
    from gemiapp.superadmin.services import process_pending_outreach

    release = _claim_outreach_send_lock()
    if release is None:
        # Another cluster is mid-send. The rows stay "pending"; the daily drain picks them up.
        logger.info("Outreach send lock held elsewhere; leaving %s queued.", len(company_ids))
        return {"sent": 0, "failed": 0, "skipped": len(company_ids)}
    try:
        sent, failed, skipped = process_pending_outreach(company_ids)
    finally:
        release()
    logger.info(
        "Client outreach: %s sent, %s failed, %s skipped (daily cap) of %s queued",
        sent, failed, skipped, len(company_ids),
    )
    # Anything skipped stays "pending" and is picked up by drain_pending_outreach_task, the
    # daily sweep registered in apps.py. This task deliberately schedules no follow-up of its
    # own: the per-batch ONCE schedule it used to create only retried its own company_ids and
    # was only created when that batch overflowed, so a backlog could sit for days.
    return {"sent": sent, "failed": failed, "skipped": skipped}


def drain_pending_outreach_task():
    """Send outreach rows left "pending" by the daily cap, oldest first. Runs daily.

    Batch-agnostic on purpose: it asks the database which rows are still pending rather than
    carrying a list of ids from whoever queued them, so a row cannot be stranded by the batch
    it happened to arrive in.
    """
    from gemiapp.models import CompanyOutreach
    from gemiapp.superadmin.services import process_pending_outreach

    if not _claim_pipeline_slot("outreach-drain", 23 * 3600):
        logger.info("Outreach drain slot already claimed by another cluster; skipping.")
        return

    # A worker killed between claiming a row and saving the result leaves it "sending"
    # forever. Nothing else reaps those, and the send lock's TTL is far shorter than this
    # sweep's, so anything still "sending" here belongs to a run that is long gone.
    stranded = CompanyOutreach.objects.filter(
        status="sending", created_at__lt=timezone.now() - timedelta(hours=1)
    ).update(status="pending")
    if stranded:
        logger.warning("Outreach drain: reset %s rows stranded in 'sending'.", stranded)

    company_ids = list(
        CompanyOutreach.objects.filter(status="pending")
        .order_by("created_at")
        .values_list("company_id", flat=True)
    )
    if not company_ids:
        logger.info("Outreach drain: nothing pending.")
        return {"sent": 0, "failed": 0, "skipped": 0}

    release = _claim_outreach_send_lock()
    if release is None:
        logger.info("Outreach send lock held elsewhere; drain deferred.")
        return {"sent": 0, "failed": 0, "skipped": len(company_ids)}
    try:
        sent, failed, skipped = process_pending_outreach(company_ids)
    finally:
        release()
    logger.info(
        "Outreach drain: %s sent, %s failed, %s still pending of %s backlog.",
        sent, failed, skipped, len(company_ids),
    )
    return {"sent": sent, "failed": failed, "skipped": skipped}


def run_intraday_pipeline_task():
    """
    Runs every three hours between 08:00 and 23:00 Europe/Athens, on the cron slots registered in
    apps.py. Calls the GEMI API for companies incorporated *today* and sends real-time alerts to
    Enterprise / Custom subscribers only.
    """
    current_hour = timezone.localtime().hour
    if not 8 <= current_hour <= 23:
        logger.info("Skipping intraday task outside the 08:00-23:00 window (hour=%s)", current_hour)
        return

    # One slot = one clock hour. TTL 2h so a slow run can't collide with the next slot but the
    # key is always gone well before the same slot comes round tomorrow.
    if not _claim_pipeline_slot("intraday", 2 * 3600):
        logger.info("Intraday pipeline slot already claimed by another cluster; skipping.")
        return

    try:
        today = timezone.localdate()
        if _pipeline_is_already_running(today):
            logger.warning("Skipping intraday pipeline: a run for %s is still in progress", today)
            return
        run = import_for_date(today)
        logger.info(f"Intraday Import: {run.created_count} νέες, {run.updated_count} ενημερωμένες")
        sent, skipped = send_digests(today, frequency="intraday")
        logger.info(f"Intraday Top Tier Email: {sent} εστάλησαν, {skipped} παραλείφθηκαν")
    except Exception as exc:
        logger.error(f"Intraday pipeline error: {exc}", exc_info=True)
        raise


def send_verification_email_task(user_id):
    """Send one account-verification email. Enqueued by signup and by the resend view.

    Lives in a worker rather than the web request because send_mail() opens an SMTP
    connection to Brevo inline: before EMAIL_TIMEOUT was set a stalled relay hung the
    gunicorn worker for hours (see the 2026-09-01 signup that reached Brevo six hours
    late), and even with the timeout a slow handshake is seconds the signup response
    should not be waiting on. The user sees "check your inbox" immediately either way.

    Takes a user id, not a request: the worker has no request to read get_host() from, so
    the link is built against settings.BASE_URL like the resend_verification_emails
    management command already does.
    """
    from gemiapp.services import send_verification_email_now

    send_verification_email_now(user_id)
