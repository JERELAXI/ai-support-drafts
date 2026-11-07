import logging
import random
import uuid
from datetime import timedelta
from typing import Any

from celery import Task
from sqlalchemy import Row, func, select, update

from app import helpdesk_client, rate_limit
from app.celery_app import celery_app
from app.config import get_settings
from app.db import SyncSessionLocal
from app.helpdesk_client import HelpdeskPermanentError, HelpdeskTemporaryError
from app.kb import search_articles_sync
from app.llm import LLMPermanentError, LLMTemporaryError
from app.llm import generate_draft as llm_generate_draft
from app.models import Ticket, TicketStatus

logger = logging.getLogger(__name__)

BACKOFF_BASE_SECONDS = 5
BACKOFF_MAX_SECONDS = 300
SEND_BACKOFF_MAX_SECONDS = 60
LAST_ERROR_MAX_LEN = 1000

REAP_LIMIT = 100
REAP_RECEIVED_AFTER = timedelta(minutes=1)
REAP_GENERATING_AFTER = timedelta(minutes=5)
REAP_APPROVED_AFTER = timedelta(minutes=2)
# Longer than the max send backoff (60 s) + timeout, so a live retry is not duplicated.
REAP_SENDING_AFTER = timedelta(minutes=5)

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


def _backoff(attempts: int, max_seconds: float = BACKOFF_MAX_SECONDS) -> float:
    delay = min(max_seconds, BACKOFF_BASE_SECONDS * 2 ** (attempts - 1))
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


def _start_sending(ticket_id: uuid.UUID) -> Row | None:
    """approved|sending -> sending and count the attempt in one commit, before the request."""
    with SyncSessionLocal() as session:
        row = session.execute(
            update(Ticket)
            .where(Ticket.id == ticket_id, Ticket.status.in_([S.approved, S.sending]))
            .values(status=S.sending, attempts_send=Ticket.attempts_send + 1, updated_at=func.now())
            .returning(
                Ticket.reply_url, Ticket.ticket_id, Ticket.final_message, Ticket.attempts_send
            )
        ).first()
        session.commit()
    if row is None:
        logger.info("ticket=%s skip -> sending: ticket not in approved/sending", ticket_id)
    else:
        logger.info(
            "ticket=%s approved|sending -> sending (attempt %d)", ticket_id, row.attempts_send
        )
    return row


@celery_app.task(bind=True, max_retries=None)
def send_reply(self: Task, ticket_id: str) -> None:
    tid = uuid.UUID(ticket_id)
    row = _start_sending(tid)
    if row is None:
        return
    attempts: int = row.attempts_send
    max_attempts = get_settings().max_send_attempts

    if attempts > max_attempts:
        # Earlier attempts never finished (worker lost mid-request): stop resending.
        error = f"no result after {max_attempts} attempts"
        logger.warning("ticket=%s send gave up: %s", tid, error)
        _transition(tid, S.sending, S.send_failed, last_error=error)
        return

    try:
        helpdesk_client.send_reply(row.reply_url, row.ticket_id, row.final_message, tid)
    except HelpdeskPermanentError as exc:
        error = f"{type(exc).__name__}: {exc}"[:LAST_ERROR_MAX_LEN]
        logger.warning("ticket=%s permanent helpdesk error: %s", tid, error)
        _transition(tid, S.sending, S.send_failed, last_error=error)
        return
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:LAST_ERROR_MAX_LEN]
        if attempts >= max_attempts:
            logger.warning("ticket=%s send gave up after %d attempts: %s", tid, attempts, error)
            _transition(tid, S.sending, S.send_failed, last_error=error)
            return
        # Stays in sending between retries; the Idempotency-Key prevents a duplicate reply.
        if _transition(tid, S.sending, S.sending, last_error=error) is None:
            return
        countdown = _backoff(attempts, SEND_BACKOFF_MAX_SECONDS)
        logger.warning(
            "ticket=%s send attempt %d/%d failed: %s; retry in %.0fs",
            tid,
            attempts,
            max_attempts,
            error,
            countdown,
            exc_info=not isinstance(exc, HelpdeskTemporaryError),
        )
        raise self.retry(countdown=countdown) from exc

    _transition(tid, S.sending, S.sent, last_error=None)


def _claim_stuck(
    status: TicketStatus, older_than: timedelta, limit: int, to_status: TicketStatus | None = None
) -> list[uuid.UUID]:
    """Touch updated_at (and optionally move to to_status) for tickets stuck in status."""
    if limit <= 0:
        return []
    stuck = (
        select(Ticket.id)
        .where(Ticket.status == status, Ticket.updated_at < func.now() - older_than)
        .order_by(Ticket.updated_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    with SyncSessionLocal() as session:
        ids = list(
            session.execute(
                update(Ticket)
                .where(Ticket.id.in_(stuck), Ticket.status == status)
                .values(status=to_status or status, updated_at=func.now())
                .returning(Ticket.id)
            ).scalars()
        )
        session.commit()
    for ticket_id in ids:
        if to_status is None:
            logger.info("ticket=%s reaper: stuck in %s, touched", ticket_id, status)
        else:
            logger.info("ticket=%s reaper: %s -> %s", ticket_id, status, to_status)
    return ids


def _requeue(task: Task, ticket_ids: list[uuid.UUID]) -> None:
    for ticket_id in ticket_ids:
        try:
            task.apply_async(args=[str(ticket_id)])
        except Exception:
            # updated_at is already touched: the next pass picks the ticket up again.
            logger.exception("ticket=%s reaper: enqueue %s failed", ticket_id, task.name)
        else:
            logger.info("ticket=%s reaper: %s enqueued", ticket_id, task.name)


@celery_app.task
def reap_stuck() -> None:
    budget = REAP_LIMIT
    received = _claim_stuck(S.received, REAP_RECEIVED_AFTER, budget)
    _requeue(generate_draft, received)
    budget -= len(received)

    # Picked up by the received rule on a later pass.
    generating = _claim_stuck(S.generating, REAP_GENERATING_AFTER, budget, S.received)
    budget -= len(generating)

    approved = _claim_stuck(S.approved, REAP_APPROVED_AFTER, budget)
    _requeue(send_reply, approved)
    budget -= len(approved)

    sending = _claim_stuck(S.sending, REAP_SENDING_AFTER, budget)
    _requeue(send_reply, sending)

    logger.info(
        "reaper picked received=%d generating=%d approved=%d sending=%d",
        len(received),
        len(generating),
        len(approved),
        len(sending),
    )
