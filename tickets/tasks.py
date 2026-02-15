from celery import shared_task
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def recalculate_rfm_task(self, organization_id):
    from tickets.models import Organization
    from tickets.services.segmentation.rfm_calculator import RFMCalculator

    org = Organization.objects.get(id=organization_id)
    try:
        org.rfm_recalc_in_progress = True
        org.save(update_fields=["rfm_recalc_in_progress"])
        RFMCalculator(org).calculate_all()
    except Exception as exc:
        logger.exception("RFM recalc failed for org %s", organization_id)
        raise self.retry(exc=exc)
    finally:
        org.rfm_recalc_in_progress = False
        org.save(update_fields=["rfm_recalc_in_progress"])
