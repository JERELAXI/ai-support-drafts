"""Key flows against the compose Postgres (database support_test, see conftest.py)."""

import asyncio
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from celery import Task
from sqlalchemy import func, insert, select

from app import helpdesk_client, operator, webhooks
from app.config import get_settings
from app.db import SyncSessionLocal, async_engine
from app.main import app
from app.models import Ticket, TicketStatus
from app.tasks import send_reply

pytestmark = pytest.mark.usefixtures("db")

REPLY_URL = "http://helpdesk.test/tickets/T-1/replies"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, uuid.UUID]]:
    """Record enqueue() calls from the webhook and the operator UI instead of publishing."""
    calls: list[tuple[str, uuid.UUID]] = []

    async def fake_enqueue(task: Task, ticket_id: uuid.UUID) -> None:
        calls.append((task.name, ticket_id))

    monkeypatch.setattr(webhooks, "enqueue", fake_enqueue)
    monkeypatch.setattr(operator, "enqueue", fake_enqueue)
    return calls


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as c:
        yield c
    # asyncpg connections are bound to this test's event loop.
    await async_engine.dispose()


def _create_ticket(status: TicketStatus, **values: str) -> uuid.UUID:
    with SyncSessionLocal() as session:
        ticket_id = session.execute(
            insert(Ticket)
            .values(
                event_id=f"evt-{uuid.uuid4()}",
                ticket_id="T-1",
                customer_email="customer@example.com",
                message="Як експортувати звіт у PDF?",
                reply_url=REPLY_URL,
                status=status,
                **values,
            )
            .returning(Ticket.id)
        ).scalar_one()
        session.commit()
    return ticket_id


def _get_ticket(ticket_id: uuid.UUID) -> Ticket:
    with SyncSessionLocal() as session:
        return session.get_one(Ticket, ticket_id)


@pytest.mark.anyio
async def test_concurrent_duplicate_webhook_creates_one_ticket(
    client: httpx.AsyncClient, enqueued: list[tuple[str, uuid.UUID]]
) -> None:
    body = {
        "event_id": "evt-dup-1",
        "ticket_id": "T-1",
        "customer_email": "customer@example.com",
        "message": "Як експортувати звіт у PDF?",
        "reply_url": REPLY_URL,
    }
    responses = await asyncio.gather(
        *(client.post("/webhooks/helpdesk", json=body) for _ in range(5))
    )

    assert [r.status_code for r in responses] == [202] * 5
    assert sorted(r.json()["duplicate"] for r in responses) == [False, True, True, True, True]
    with SyncSessionLocal() as session:
        ids = session.execute(select(Ticket.id).where(Ticket.event_id == "evt-dup-1")).all()
    assert len(ids) == 1
    assert enqueued == [("app.tasks.generate_draft", ids[0].id)]


@pytest.mark.anyio
async def test_concurrent_double_approve_transitions_once(
    client: httpx.AsyncClient, enqueued: list[tuple[str, uuid.UUID]]
) -> None:
    ticket_id = _create_ticket(TicketStatus.drafted, draft="draft text")
    settings = get_settings()
    auth = (settings.operator_user, settings.operator_password)

    responses = await asyncio.gather(
        *(
            client.post(
                f"/operator/tickets/{ticket_id}/approve",
                data={"final_message": f"reply {n}"},
                auth=auth,
            )
            for n in range(2)
        )
    )

    assert [r.status_code for r in responses] == [303, 303]
    locations = sorted(r.headers["location"] for r in responses)
    assert locations == ["/operator", "/operator?notice=already_processed"]
    ticket = _get_ticket(ticket_id)
    assert ticket.status == TicketStatus.approved
    assert ticket.final_message in {"reply 0", "reply 1"}
    assert enqueued == [("app.tasks.send_reply", ticket_id)]


def test_send_reply_retries_500_then_sent_with_same_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket_id = _create_ticket(TicketStatus.approved, final_message="Here is how to export.")
    statuses = iter([500, 201])
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(next(statuses))

    real_client = httpx.Client

    def mock_client(**kwargs: object) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(helpdesk_client.httpx, "Client", mock_client)

    # Eager apply(): Celery runs the retry immediately, ignoring the countdown.
    send_reply.apply(args=[str(ticket_id)])

    assert len(seen) == 2
    assert {r.headers["Idempotency-Key"] for r in seen} == {str(ticket_id)}
    ticket = _get_ticket(ticket_id)
    assert ticket.status == TicketStatus.sent
    assert ticket.attempts_send == 2
    assert ticket.last_error is None
    with SyncSessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Ticket)) == 1
