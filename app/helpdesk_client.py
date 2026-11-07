import uuid

import httpx

from app.config import get_settings

TEMPORARY_4XX = {409, 429}


class HelpdeskError(Exception):
    pass


class HelpdeskTemporaryError(HelpdeskError):
    """Timeout, network error, 5xx, 409 or 429: retry later with the same Idempotency-Key."""


class HelpdeskPermanentError(HelpdeskError):
    """Other 4xx or an unexpected status: retrying will not help."""


def send_reply(reply_url: str, ticket_id: str, message: str, idempotency_key: uuid.UUID) -> None:
    """POST the reply; returns only on 201."""
    try:
        with httpx.Client(timeout=get_settings().helpdesk_timeout) as client:
            response = client.post(
                reply_url,
                json={"ticket_id": ticket_id, "message": message},
                headers={"Idempotency-Key": str(idempotency_key)},
            )
    except httpx.TransportError as exc:  # timeouts and network errors
        raise HelpdeskTemporaryError(f"helpdesk request failed: {exc!r}") from exc

    status = response.status_code
    if status == 201:
        return
    if status >= 500 or status in TEMPORARY_4XX:
        raise HelpdeskTemporaryError(f"helpdesk status {status}")
    raise HelpdeskPermanentError(f"helpdesk status {status}: {response.text[:200]}")
