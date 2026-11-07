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


@pytest.mark.anyio
async def test_operator_actions_redirect_back_to_filter_tab(
    client: httpx.AsyncClient, enqueued: list[tuple[str, uuid.UUID]]
) -> None:
    approved = _create_ticket(TicketStatus.drafted, draft="draft text")
    rejected = _create_ticket(TicketStatus.drafted, draft="draft text")
    settings = get_settings()
    auth = (settings.operator_user, settings.operator_password)

    async def post(path: str, **data: str) -> str:
        response = await client.post(f"/operator/tickets/{path}", data=data, auth=auth)
        assert response.status_code == 303
        return response.headers["location"]

    assert await post(f"{approved}/approve?status=drafted", final_message=" ") == (
        "/operator?status=drafted&notice=empty_text"
    )
    assert await post(f"{approved}/approve?status=drafted", final_message="reply") == (
        "/operator?status=drafted"
    )
    assert await post(f"{approved}/approve?status=drafted", final_message="reply") == (
        "/operator?status=drafted&notice=already_processed"
    )
    assert await post(f"{rejected}/reject?status=drafted") == "/operator?status=drafted"
    assert await post(f"{rejected}/reject?status=drafted") == (
        "/operator?status=drafted&notice=already_processed"
    )
    assert await post(f"{rejected}/reject") == "/operator?notice=already_processed"


@pytest.mark.anyio
async def test_operator_list_limit_respects_filter_and_is_clamped(
    client: httpx.AsyncClient,
) -> None:
    for _ in range(3):
        _create_ticket(TicketStatus.drafted, draft="draft text")
    _create_ticket(TicketStatus.sent, final_message="sent text")
    settings = get_settings()
    auth = (settings.operator_user, settings.operator_password)

    async def page(query: str) -> str:
        response = await client.get(f"/operator?{query}", auth=auth)
        assert response.status_code == 200
        return response.text

    filtered = await page("status=drafted&limit=2")
    assert filtered.count('<article class="ticket') == 2
    assert "Показано 2 з 3" in filtered

    too_big = await page("limit=1000")
    assert too_big.count('<article class="ticket') == 4
    assert "Показано 4 з 4" in too_big

    too_small = await page("limit=0")
    assert too_small.count('<article class="ticket') == 1
    assert "Показано 1 з 4" in too_small


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
