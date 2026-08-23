import logging
from datetime import timedelta

from django.utils import timezone

from gemiapp.services import import_for_date, send_digests

logger = logging.getLogger(__name__)


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
