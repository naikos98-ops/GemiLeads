import logging
from datetime import date, timedelta
from gemiapp.services import import_for_date, send_digests

logger = logging.getLogger(__name__)


def _pipeline_is_already_running(target_date):
    """True when an unfinished ImportRun for the same date is still in flight.

    Two concurrent pipelines contend for the same rows inside get_or_create and both stall, which
    is how a duplicated schedule row made every nightly run hit the task timeout.
    """
    from django.conf import settings
    from django.utils import timezone
    from gemiapp.models import ImportRun

    # A run older than the task timeout must have been killed, so it no longer blocks anything.
    cutoff = timezone.now() - timedelta(seconds=settings.Q_CLUSTER.get("timeout", 1800))
    return ImportRun.objects.filter(
        target_date=target_date, status="running", started_at__gte=cutoff
    ).exists()


def run_daily_pipeline_task():
    try:
        target = date.today() - timedelta(days=1)
        if _pipeline_is_already_running(target):
            logger.warning("Skipping daily pipeline: a run for %s is still in progress", target)
            return
        run = import_for_date(target)
        logger.info(f"Import: {run.created_count} νέες, {run.updated_count} ενημερωμένες")
        
        sent, skipped = send_digests(target, frequency="daily")
        logger.info(f"Daily Email: {sent} εστάλησαν, {skipped} παραλείφθηκαν")
        
        if target.weekday() == 6:  # Sunday
            w_sent, w_skipped = send_digests(target, frequency="weekly")
            logger.info(f"Weekly Email: {w_sent} εστάλησαν, {w_skipped} παραλείφθηκαν")
    except Exception as exc:
        logger.error(f"Pipeline error: {exc}", exc_info=True)
        raise


def run_intraday_pipeline_task():
    """
    Intra-day 3-hour GEMI API pipeline task running between 08:00 and 00:00.
    Sends real-time email alerts ONLY to Top Tier (Enterprise / Real-Time) subscribers.
    """
    from django.utils import timezone
    current_hour = timezone.localtime().hour
    if not (8 <= current_hour <= 23 or current_hour == 0):
        logger.info("Skipping intraday task outside 08:00 - 00:00 window (hour=%s)", current_hour)
        return

    try:
        today = date.today()
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
