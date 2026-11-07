from dataclasses import dataclass

import httpx
import pytest

from app import llm
from app.config import get_settings


@dataclass(frozen=True)
class _Article:
    slug: str
    title: str
    body: str
    url: str


ARTICLES = [
    _Article("export-pdf", "Експорт звіту в PDF", "Натисніть «Експорт»...", "https://kb.test/pdf"),
    _Article("export-csv-excel", "Експорт у CSV та Excel", "...", "https://kb.test/csv"),
]


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> tuple[list[httpx.Request], list[float]]:
    """No-key settings, provider answering 401 via MockTransport, time.sleep recorded."""
    settings = get_settings().model_copy(
        update={"llm_api_key": "", "llm_base_url": "https://llm.test/v1"}
    )
    monkeypatch.setattr(llm, "get_settings", lambda: settings)

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"error": {"message": "missing api key"}})

    real_client = httpx.Client

    def client(**kwargs: object) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(llm.httpx, "Client", client)

    sleeps: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)
    return requests, sleeps


def test_no_key_makes_real_request_and_returns_static_draft(
    provider: tuple[list[httpx.Request], list[float]],
) -> None:
    requests, sleeps = provider

    result = llm.generate_draft("Як експортувати звіт у PDF?", ARTICLES)

    assert len(requests) == 1
    assert str(requests[0].url) == "https://llm.test/v1/chat/completions"
    assert "authorization" not in requests[0].headers
    assert result.sources == ["export-pdf"]  # best article only
    assert "Експорт звіту в PDF" in result.draft
    assert "https://kb.test/pdf" in result.draft
    assert len(sleeps) == 1
    assert llm.NO_KEY_DELAY_RANGE[0] <= sleeps[0] <= llm.NO_KEY_DELAY_RANGE[1]


def test_no_key_without_articles_returns_not_found(
    provider: tuple[list[httpx.Request], list[float]],
) -> None:
    requests, _ = provider

    result = llm.generate_draft("Щось зовсім інше", [])

    assert len(requests) == 1
    assert result.draft == llm.NOT_FOUND_TEXT
    assert result.sources == []
