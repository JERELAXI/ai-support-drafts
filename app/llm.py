import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

NO_KEY_DELAY_RANGE = (5.0, 30.0)
NOT_FOUND_TEXT = "У базі знань відповіді не знайдено, потрібна відповідь оператора."

SYSTEM_PROMPT = """Ти — агент служби підтримки сервісу ReportHub. Готуєш чернетку відповіді клієнту.
Правила:
- Відповідай українською мовою, ввічливо й коротко.
- Використовуй тільки інформацію з наданих статей бази знань. Нічого не вигадуй.
- Додай у текст посилання (url) на статті, якими користувався.
- Якщо в статтях немає відповіді на питання, так і напиши, що відповідь має надати оператор, \
і поверни порожній список sources.
- Відповідь — тільки JSON-об'єкт: {"draft": "<текст відповіді>", "sources": ["<slug>", ...]}, \
де sources — slug-и статей, на які реально посилається текст."""


class LLMError(Exception):
    pass


class LLMTemporaryError(LLMError):
    """Timeout, network error, 5xx, 429 or malformed response: retry later."""


class LLMPermanentError(LLMError):
    """Other 4xx: retrying will not help."""


class Article(Protocol):
    slug: str
    title: str
    body: str
    url: str


@dataclass(frozen=True)
class DraftResult:
    draft: str
    sources: list[str]


def generate_draft(message: str, articles: list[Article]) -> DraftResult:
    """Articles must be ordered by relevance, best first."""
    settings = get_settings()
    payload = {
        "model": settings.llm_model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(message, articles)},
        ],
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}

    try:
        with httpx.Client(timeout=settings.llm_timeout) as client:
            response = client.post(
                f"{settings.llm_base_url}/chat/completions", json=payload, headers=headers
            )
    except httpx.TransportError as exc:  # timeouts and network errors
        raise LLMTemporaryError(f"llm request failed: {exc!r}") from exc

    if not settings.llm_api_key:
        logger.info("llm no-key mode: provider responded status=%d", response.status_code)
        return _static_draft(articles)

    if response.status_code == 429 or response.status_code >= 500:
        raise LLMTemporaryError(f"llm status {response.status_code}")
    if response.status_code >= 400:
        raise LLMPermanentError(f"llm status {response.status_code}: {response.text[:200]}")

    return _parse_response(response, {a.slug for a in articles})


def _user_prompt(message: str, articles: list[Article]) -> str:
    blocks = [f"slug: {a.slug}\ntitle: {a.title}\nurl: {a.url}\n---\n{a.body}" for a in articles]
    kb = "\n\n=====\n\n".join(blocks) if blocks else "(статей не знайдено)"
    return f"Статті бази знань:\n\n{kb}\n\nЗвернення клієнта:\n{message}"


def _parse_response(response: httpx.Response, allowed_slugs: set[str]) -> DraftResult:
    try:
        content = response.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        draft = str(data["draft"]).strip()
        raw_sources = data.get("sources") or []
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMTemporaryError(f"malformed llm response: {exc!r}") from exc
    if not draft:
        raise LLMTemporaryError("llm returned empty draft")

    sources = list(dict.fromkeys(s for s in raw_sources if s in allowed_slugs))
    return DraftResult(draft=draft, sources=sources)


def _static_draft(articles: list[Article]) -> DraftResult:
    time.sleep(random.uniform(*NO_KEY_DELAY_RANGE))
    if not articles:
        return DraftResult(draft=NOT_FOUND_TEXT, sources=[])
    best = articles[0]
    draft = (
        "Вітаємо! Дякуємо за звернення.\n\n"
        f"Відповідь на ваше питання є в статті «{best.title}»: {best.url}\n\n"
        "Якщо інструкція не допомогла, напишіть нам, і ми розберемося."
    )
    return DraftResult(draft=draft, sources=[best.slug])
