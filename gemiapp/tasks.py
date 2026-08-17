import logging
from datetime import date, timedelta
from gemiapp.services import import_for_date, send_digests

logger = logging.getLogger(__name__)

def run_daily_pipeline_task():
    try:
        target = date.today() - timedelta(days=1)
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
