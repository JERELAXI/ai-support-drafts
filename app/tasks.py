import logging
import random
import uuid
from typing import Any

from celery import Task
from sqlalchemy import Row, func, update

from app import rate_limit
from app.celery_app import celery_app
from app.config import get_settings
from app.db import SyncSessionLocal
from app.kb import search_articles_sync
from app.llm import LLMPermanentError, LLMTemporaryError
from app.llm import generate_draft as llm_generate_draft
from app.models import Ticket, TicketStatus

logger = logging.getLogger(__name__)

BACKOFF_BASE_SECONDS = 5
BACKOFF_MAX_SECONDS = 300
LAST_ERROR_MAX_LEN = 1000

S = TicketStatus


def _transition(
    ticket_id: uuid.UUID, from_status: TicketStatus, to_status: TicketStatus, **values: Any
) -> Row | None:
    """Conditional atomic status change; None if the ticket is not in from_status."""
    with SyncSessionLocal() as session:
        row = session.execute(
            update(Ticket)
            .where(Ticket.id == ticket_id, Ticket.status == from_status)
            .values(status=to_status, updated_at=func.now(), **values)
            .returning(Ticket.message, Ticket.attempts_generate)
        ).first()
        session.commit()
    if row is None:
        logger.info(
            "ticket=%s skip %s -> %s: ticket not in %s",
            ticket_id,
            from_status,
            to_status,
            from_status,
        )
    else:
        logger.info("ticket=%s %s -> %s", ticket_id, from_status, to_status)
    return row


def _count_attempt(ticket_id: uuid.UUID) -> int | None:
    """Increment attempts_generate while the ticket is still generating; None otherwise."""
    with SyncSessionLocal() as session:
        attempts = session.execute(
            update(Ticket)
            .where(Ticket.id == ticket_id, Ticket.status == S.generating)
            .values(attempts_generate=Ticket.attempts_generate + 1, updated_at=func.now())
            .returning(Ticket.attempts_generate)
        ).scalar_one_or_none()
        session.commit()
    if attempts is None:
        logger.info("ticket=%s skip attempt count: ticket not in generating", ticket_id)
    return attempts


def _backoff(attempts: int) -> float:
    delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempts - 1))
    return delay * random.uniform(0.5, 1.0)


def _fail_or_retry(task: Task, ticket_id: uuid.UUID, attempts: int, exc: Exception) -> None:
    error = f"{type(exc).__name__}: {exc}"[:LAST_ERROR_MAX_LEN]
    max_attempts = get_settings().max_generate_attempts
    if attempts >= max_attempts:
        logger.warning(
            "ticket=%s generation gave up after %d attempts: %s", ticket_id, attempts, error
        )
        _transition(ticket_id, S.generating, S.generation_failed, last_error=error)
        return
    if _transition(ticket_id, S.generating, S.received, last_error=error) is None:
        return
    countdown = _backoff(attempts)
    logger.warning(
        "ticket=%s generation attempt %d/%d failed: %s; retry in %.0fs",
        ticket_id,
        attempts,
        max_attempts,
        error,
        countdown,
        exc_info=not isinstance(exc, LLMTemporaryError),
    )
    raise task.retry(countdown=countdown)


@celery_app.task(bind=True, max_retries=None)
def generate_draft(self: Task, ticket_id: str) -> None:
    tid = uuid.UUID(ticket_id)
    row = _transition(tid, S.received, S.generating)
    if row is None:
        return
    message: str = row.message

    attempts: int | None = None
    try:
        with SyncSessionLocal() as session:
            articles = search_articles_sync(session, message)

        limit = rate_limit.acquire()
        if limit.allowed:
            attempts = _count_attempt(tid)
            if attempts is None:
                return
            result = llm_generate_draft(message, articles)
    except LLMPermanentError as exc:
        error = f"{type(exc).__name__}: {exc}"[:LAST_ERROR_MAX_LEN]
        logger.warning("ticket=%s permanent llm error: %s", tid, error)
        _transition(tid, S.generating, S.generation_failed, last_error=error)
        return
    except Exception as exc:
        if attempts is None:  # failed before the attempt was counted
            attempts = _count_attempt(tid)
            if attempts is None:
                return
        _fail_or_retry(self, tid, attempts, exc)
        return

    if not limit.allowed:
        # Not a failure: back to received without spending an attempt.
        if _transition(tid, S.generating, S.received) is None:
            return
        logger.info("ticket=%s rate limited, retry in %ds", tid, limit.retry_after)
        raise self.retry(countdown=limit.retry_after)

    _transition(
        tid,
        S.generating,
        S.drafted,
        draft=result.draft,
        draft_sources=result.sources,
        last_error=None,
    )
