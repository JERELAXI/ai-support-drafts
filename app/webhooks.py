import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Ticket
from app.schemas import HelpdeskWebhook, WebhookAccepted

logger = logging.getLogger(__name__)

router = APIRouter()


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
    # TODO(stage 4): enqueue generate_draft(new_id) after commit.
    return WebhookAccepted(duplicate=False)
