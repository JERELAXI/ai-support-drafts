import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Computed, DateTime, Enum, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TicketStatus(enum.StrEnum):
    received = "received"
    generating = "generating"
    drafted = "drafted"
    approved = "approved"
    sending = "sending"
    sent = "sent"
    generation_failed = "generation_failed"
    rejected = "rejected"
    send_failed = "send_failed"


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (Index("ix_tickets_status_updated_at", "status", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    event_id: Mapped[str] = mapped_column(Text, unique=True)
    ticket_id: Mapped[str] = mapped_column(Text)
    customer_email: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    reply_url: Mapped[str] = mapped_column(Text)
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, name="ticket_status"),
        server_default=TicketStatus.received.value,
    )
    draft: Mapped[str | None] = mapped_column(Text)
    draft_sources: Mapped[Any | None] = mapped_column(JSONB)
    final_message: Mapped[str | None] = mapped_column(Text)
    attempts_generate: Mapped[int] = mapped_column(Integer, server_default="0")
    attempts_send: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class KbArticle(Base):
    __tablename__ = "kb_articles"
    __table_args__ = (
        Index("ix_kb_articles_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', title || ' ' || body)", persisted=True)
    )
