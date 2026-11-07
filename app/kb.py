import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import KbArticle

MIN_WORD_LEN = 3
STEM_LEN = 5
TOP_N = 3

router = APIRouter()


class KbHit(BaseModel):
    slug: str
    title: str
    url: str
    rank: float


class KbArticleHit(KbHit):
    """Search hit with article body, for the LLM prompt (not exposed via /kb/search)."""

    body: str


def build_tsquery(text: str) -> str:
    """Build an OR-joined prefix tsquery: words shorter than 3 chars dropped, rest cut to a stem."""
    stems: dict[str, None] = {}
    for word in re.findall(r"[^\W_]+", text.lower()):
        if len(word) >= MIN_WORD_LEN:
            stems[word[:STEM_LEN]] = None
    return " | ".join(f"{stem}:*" for stem in stems)


def _search_stmt(tsquery: str) -> Select:
    query = func.to_tsquery("simple", tsquery)
    rank = func.ts_rank(KbArticle.search_vector, query).label("rank")
    return (
        select(KbArticle.slug, KbArticle.title, KbArticle.url, KbArticle.body, rank)
        .where(KbArticle.search_vector.op("@@")(query))
        .order_by(rank.desc(), KbArticle.id)
        .limit(TOP_N)
    )


async def search_articles(session: AsyncSession, text: str) -> list[KbArticleHit]:
    tsquery = build_tsquery(text)
    if not tsquery:
        return []
    rows = (await session.execute(_search_stmt(tsquery))).all()
    return [KbArticleHit(**row._asdict()) for row in rows]


def search_articles_sync(session: Session, text: str) -> list[KbArticleHit]:
    tsquery = build_tsquery(text)
    if not tsquery:
        return []
    rows = session.execute(_search_stmt(tsquery)).all()
    return [KbArticleHit(**row._asdict()) for row in rows]


@router.get("/kb/search", response_model=list[KbHit])
async def kb_search(
    q: Annotated[str, Query(min_length=1)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[KbHit]:
    hits = await search_articles(session, q)
    return [KbHit(**hit.model_dump(exclude={"body"})) for hit in hits]
