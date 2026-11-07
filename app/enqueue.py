import asyncio
import logging
import uuid

from celery import Task

logger = logging.getLogger(__name__)

# Publishing must not hold the HTTP response (the helpdesk webhook times out after 2 s).
ENQUEUE_TIMEOUT_SECONDS = 1.5
ENQUEUE_RETRY_POLICY = {
    "max_retries": 1,
    "interval_start": 0,
    "interval_step": 0.2,
    "interval_max": 0.2,
}


def _publish(task: Task, ticket_id: uuid.UUID) -> None:
    task.apply_async(args=[str(ticket_id)], retry=True, retry_policy=ENQUEUE_RETRY_POLICY)


async def enqueue(task: Task, ticket_id: uuid.UUID) -> None:
    """Publish task(ticket_id) off the event loop; call only after commit. Never raises."""
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_publish, task, ticket_id), timeout=ENQUEUE_TIMEOUT_SECONDS
        )
    except Exception:
        # The ticket is committed; the reaper will enqueue it later.
        logger.exception("enqueue %s failed ticket=%s", task.name, ticket_id)
    else:
        logger.info("%s enqueued ticket=%s", task.name, ticket_id)
