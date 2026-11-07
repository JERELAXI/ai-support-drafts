import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session
from app.main import app


class _EmptyResult:
    def scalars(self) -> "_EmptyResult":
        return self

    def all(self) -> list[Any]:
        return []


class _FakeSession:
    async def execute(self, *args: Any, **kwargs: Any) -> _EmptyResult:
        return _EmptyResult()


async def _fake_session() -> AsyncIterator[_FakeSession]:
    yield _FakeSession()


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_session] = _fake_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _auth() -> tuple[str, str]:
    settings = get_settings()
    return settings.operator_user, settings.operator_password


def test_operator_without_credentials_is_401(client: TestClient) -> None:
    response = client.get("/operator")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


@pytest.mark.parametrize(
    "credentials",
    [("operator", "wrong"), ("wrong", "secret"), ("", "")],
)
def test_operator_wrong_credentials_is_401(
    client: TestClient, credentials: tuple[str, str]
) -> None:
    response = client.get("/operator", auth=credentials)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


def test_operator_valid_credentials(client: TestClient) -> None:
    response = client.get("/operator", auth=_auth())
    assert response.status_code == 200


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_operator_actions_require_auth(client: TestClient, action: str) -> None:
    response = client.post(
        f"/operator/tickets/{uuid.uuid4()}/{action}", data={"final_message": "hi"}
    )
    assert response.status_code == 401


def test_health_stays_open(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_webhook_stays_open(client: TestClient) -> None:
    # An invalid body must fail validation (422), not authentication.
    assert client.post("/webhooks/helpdesk", json={}).status_code == 422
