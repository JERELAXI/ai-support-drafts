import asyncio
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Ticket
from app.schemas import HelpdeskWebhook, WebhookAccepted
from app.tasks import generate_draft

logger = logging.getLogger(__name__)

router = APIRouter()

# Publishing must not hold the webhook response: the helpdesk times out after 2 s.
ENQUEUE_TIMEOUT_SECONDS = 1.5
ENQUEUE_RETRY_POLICY = {
    "max_retries": 1,
    "interval_start": 0,
    "interval_step": 0.2,
    "interval_max": 0.2,
}


def _enqueue_generate_draft(ticket_id: uuid.UUID) -> None:
    generate_draft.apply_async(args=[str(ticket_id)], retry=True, retry_policy=ENQUEUE_RETRY_POLICY)


@router.post(
    "/webhooks/helpdesk",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookAccepted,
)
async def helpdesk_webhook(
    payload: HelpdeskWebhook,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WebhookAccepted:
    stmt = (
        insert(Ticket)
        .values(
            event_id=payload.event_id,
            ticket_id=payload.ticket_id,
            customer_email=payload.customer_email,
            message=payload.message,
            reply_url=str(payload.reply_url),
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(Ticket.id)
    )
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    await session.commit()

    if new_id is None:
        logger.info("duplicate webhook ignored event_id=%s", payload.event_id)
        return WebhookAccepted(duplicate=True)

    logger.info("ticket created ticket=%s event_id=%s status=received", new_id, payload.event_id)
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_enqueue_generate_draft, new_id), timeout=ENQUEUE_TIMEOUT_SECONDS
        )
    except Exception:
        # The ticket is committed as received; the reaper will enqueue it later.
        logger.exception("enqueue generate_draft failed ticket=%s", new_id)
    else:
        logger.info("generate_draft enqueued ticket=%s", new_id)
    return WebhookAccepted(duplicate=False)
