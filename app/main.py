import logging
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.kb import router as kb_router
from app.operator import router as operator_router
from app.webhooks import router as webhooks_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="AI Support Drafts")
app.include_router(webhooks_router)
app.include_router(kb_router)
app.include_router(operator_router)


@app.get("/health")
async def health(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "OK"}
