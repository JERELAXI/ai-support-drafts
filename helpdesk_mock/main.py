"""Helpdesk mock: accepts replies, injects 500s and slow answers, dedups by Idempotency-Key.

Storage is in memory, so the service must run as a single process.
"""

import asyncio
import logging
import random
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from helpdesk_mock.config import MockSettings, get_settings

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="Helpdesk mock")


class ReplyIn(BaseModel):
    ticket_id: str
    message: str


class Reply(BaseModel):
    id: int
    idempotency_key: str
    ticket_id: str
    message: str
    received_at: datetime


class ReplyStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._by_key: dict[str, Reply] = {}

    async def save(self, key: str, ticket_id: str, message: str) -> tuple[Reply, bool]:
        """Return the reply for key and whether it was created by this call."""
        async with self._lock:
            existing = self._by_key.get(key)
            if existing is not None:
                return existing, False
            reply = Reply(
                id=len(self._by_key) + 1,
                idempotency_key=key,
                ticket_id=ticket_id,
                message=message,
                received_at=datetime.now(UTC),
            )
            self._by_key[key] = reply
            return reply, True

    def all(self) -> list[Reply]:
        return list(self._by_key.values())


store = ReplyStore()


def get_store() -> ReplyStore:
    return store


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "OK"}


@app.post("/tickets/{ticket_id}/replies", status_code=201, response_model=Reply)
async def create_reply(
    ticket_id: str,
    body: ReplyIn,
    settings: Annotated[MockSettings, Depends(get_settings)],
    replies: Annotated[ReplyStore, Depends(get_store)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Reply | JSONResponse:
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")

    roll = random.random()
    if roll < settings.error_rate:
        logger.info("mock key=%s injected 500", idempotency_key)
        return JSONResponse({"detail": "injected failure"}, status_code=500)

    reply, created = await replies.save(idempotency_key, ticket_id, body.message)
    logger.info(
        "mock key=%s ticket_id=%s %s reply id=%d",
        idempotency_key,
        ticket_id,
        "stored" if created else "duplicate, returning",
        reply.id,
    )

    if roll < settings.error_rate + settings.slow_rate:
        # Accepted on our side, but the client times out before it sees the 201.
        logger.info("mock key=%s injected delay %.1fs", idempotency_key, settings.slow_seconds)
        await asyncio.sleep(settings.slow_seconds)
    return reply


@app.get("/replies", response_model=list[Reply])
async def list_replies(replies: Annotated[ReplyStore, Depends(get_store)]) -> list[Reply]:
    return replies.all()
