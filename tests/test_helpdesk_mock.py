import asyncio
from collections.abc import Iterator

import httpx
import pytest

from helpdesk_mock.config import MockSettings, get_settings
from helpdesk_mock.main import ReplyStore, app, get_store

URL = "/tickets/T-1/replies"
BODY = {"ticket_id": "T-1", "message": "hello"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def store() -> ReplyStore:
    return ReplyStore()


def _client(store: ReplyStore, **settings: float) -> httpx.AsyncClient:
    mock_settings = MockSettings(**{"error_rate": 0, "slow_rate": 0, **settings})
    app.dependency_overrides[get_settings] = lambda: mock_settings
    app.dependency_overrides[get_store] = lambda: store
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock")


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_same_key_returns_same_201_without_new_record(store: ReplyStore) -> None:
    async with _client(store) as client:
        first = await client.post(URL, json=BODY, headers={"Idempotency-Key": "k1"})
        second = await client.post(URL, json=BODY, headers={"Idempotency-Key": "k1"})
        other = await client.post(URL, json=BODY, headers={"Idempotency-Key": "k2"})
        replies = (await client.get("/replies")).json()

    assert first.status_code == second.status_code == other.status_code == 201
    assert first.json() == second.json()
    assert [r["idempotency_key"] for r in replies] == ["k1", "k2"]


@pytest.mark.anyio
async def test_concurrent_same_key_stores_once(store: ReplyStore) -> None:
    async with _client(store) as client:
        responses = await asyncio.gather(
            *(client.post(URL, json=BODY, headers={"Idempotency-Key": "k1"}) for _ in range(20))
        )
        replies = (await client.get("/replies")).json()

    assert {r.status_code for r in responses} == {201}
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(replies) == 1


@pytest.mark.anyio
async def test_missing_key_is_400(store: ReplyStore) -> None:
    async with _client(store) as client:
        response = await client.post(URL, json=BODY)
    assert response.status_code == 400
    assert store.all() == []


@pytest.mark.anyio
async def test_injected_500_stores_nothing(store: ReplyStore) -> None:
    async with _client(store, error_rate=1) as client:
        response = await client.post(URL, json=BODY, headers={"Idempotency-Key": "k1"})
    assert response.status_code == 500
    assert store.all() == []


@pytest.mark.anyio
async def test_slow_answer_stores_before_delay(store: ReplyStore) -> None:
    async with _client(store, slow_rate=1, slow_seconds=0.5) as client:
        request = asyncio.create_task(
            client.post(URL, json=BODY, headers={"Idempotency-Key": "k1"})
        )
        await asyncio.sleep(0.2)
        # Still waiting for the answer, but the reply is already stored.
        assert not request.done()
        assert [r.idempotency_key for r in store.all()] == ["k1"]
        response = await request
    assert response.status_code == 201
