from celery import Celery
from kombu import Exchange, Queue

from app.config import get_settings

settings = get_settings()

celery_app = Celery("support", broker=settings.celery_broker_url)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    task_queues=(Queue("default", Exchange("default"), routing_key="default", durable=True),),
    task_default_delivery_mode="persistent",
    broker_connection_retry_on_startup=True,
    timezone="UTC",
)
