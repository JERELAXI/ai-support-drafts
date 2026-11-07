import uuid
from collections.abc import Callable

import httpx
import pytest

from app import helpdesk_client
from app.helpdesk_client import HelpdeskPermanentError, HelpdeskTemporaryError

URL = "http://helpdesk.test/tickets/T-1/replies"
KEY = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _use_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    real_client = httpx.Client

    def client(**kwargs: object) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(helpdesk_client.httpx, "Client", client)


def test_201_sends_body_and_idempotency_key(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={})

    _use_transport(monkeypatch, handler)
    helpdesk_client.send_reply(URL, "T-1", "hello", KEY)

    assert len(seen) == 1
    assert seen[0].headers["Idempotency-Key"] == str(KEY)
    assert seen[0].read() == b'{"ticket_id":"T-1","message":"hello"}'


@pytest.mark.parametrize("status", [500, 502, 503, 409, 429])
def test_temporary_statuses(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    _use_transport(monkeypatch, lambda _: httpx.Response(status))
    with pytest.raises(HelpdeskTemporaryError):
        helpdesk_client.send_reply(URL, "T-1", "hello", KEY)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 200])
def test_permanent_statuses(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    _use_transport(monkeypatch, lambda _: httpx.Response(status))
    with pytest.raises(HelpdeskPermanentError):
        helpdesk_client.send_reply(URL, "T-1", "hello", KEY)


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")])
def test_timeout_and_network_errors_are_temporary(
    monkeypatch: pytest.MonkeyPatch, exc: httpx.TransportError
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise exc

    _use_transport(monkeypatch, handler)
    with pytest.raises(HelpdeskTemporaryError):
        helpdesk_client.send_reply(URL, "T-1", "hello", KEY)
