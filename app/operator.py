import logging
import uuid
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import KbArticle, Ticket, TicketStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/operator")
templates = Jinja2Templates(directory="app/templates")

NOTICES = {
    "already_processed": "Тікет уже оброблено.",
    "empty_text": "Текст відповіді не може бути порожнім.",
}


def _back(notice: str | None = None) -> RedirectResponse:
    url = "/operator" + (f"?{urlencode({'notice': notice})}" if notice else "")
    return RedirectResponse(url, status_code=303)


async def _drafted_to(
    session: AsyncSession,
    ticket_id: uuid.UUID,
    to_status: TicketStatus,
    **values: str,
) -> bool:
    """Conditional atomic drafted -> to_status; False if the ticket is no longer drafted."""
    row = (
        await session.execute(
            update(Ticket)
            .where(Ticket.id == ticket_id, Ticket.status == TicketStatus.drafted)
            .values(status=to_status, updated_at=func.now(), **values)
            .returning(Ticket.id)
        )
    ).first()
    await session.commit()
    if row is None:
        logger.info(
            "ticket=%s skip %s -> %s: ticket not in %s",
            ticket_id,
            TicketStatus.drafted,
            to_status,
            TicketStatus.drafted,
        )
        return False
    logger.info("ticket=%s %s -> %s", ticket_id, TicketStatus.drafted, to_status)
    return True


@router.get("", response_class=HTMLResponse)
async def list_tickets(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    status: TicketStatus | None = None,
    notice: str | None = None,
) -> HTMLResponse:
    stmt = select(Ticket).order_by(Ticket.created_at.desc())
    if status is not None:
        stmt = stmt.where(Ticket.status == status)
    tickets = (await session.execute(stmt)).scalars().all()

    slugs = {slug for t in tickets for slug in (t.draft_sources or [])}
    articles: dict[str, KbArticle] = {}
    if slugs:
        rows = await session.execute(select(KbArticle).where(KbArticle.slug.in_(slugs)))
        articles = {a.slug: a for a in rows.scalars()}

    return templates.TemplateResponse(
        request,
        "operator.html",
        {
            "tickets": tickets,
            "articles": articles,
            "statuses": list(TicketStatus),
            "current_status": status,
            "notice": NOTICES.get(notice) if notice else None,
        },
    )


@router.post("/tickets/{ticket_id}/approve")
async def approve(
    ticket_id: uuid.UUID,
    final_message: Annotated[str, Form()],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RedirectResponse:
    text = final_message.strip()
    if not text:
        return _back("empty_text")
    if not await _drafted_to(session, ticket_id, TicketStatus.approved, final_message=text):
        return _back("already_processed")
    # TODO(stage 6): enqueue send_reply(ticket_id) after commit.
    return _back()


@router.post("/tickets/{ticket_id}/reject")
async def reject(
    ticket_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RedirectResponse:
    if not await _drafted_to(session, ticket_id, TicketStatus.rejected):
        return _back("already_processed")
    return _back()
