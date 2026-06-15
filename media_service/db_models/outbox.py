"""Database models for the transactional outbox and webhook subscriptions.

``OutboxEvent`` rows are written in the *same* DB transaction as the state change
that produced them (see ``core/outbox.record_event``), so no state change is ever
silently un-notified. The service-owned maintenance worker later drains ``PENDING``
rows and POSTs each as a signed ``OutboxEventPayload`` to every matching
``Subscription`` (see ``maintenance_worker.deliver_outbox``).
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
import uuid

from sqlalchemy import JSON, Boolean, Column, DateTime, String
from sqlmodel import Field, SQLModel

from media_service.core.config import settings
from media_service.core.db_models import prefixed_tables
from media_service.db_models.media_objects import utcnow


class OutboxStatus(StrEnum):
    """Delivery lifecycle for an outbox event row."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class OutboxEvent(SQLModel, table=True):
    """A queued outbound webhook event, staged in the state-change transaction."""

    __tablename__ = prefixed_tables("outbox_event")
    __table_args__ = (
        {"mysql_engine": settings.DB_ENGINE, "mysql_charset": settings.DB_CHARSET},
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    event_type: str = Field(
        sa_column=Column(String(64), nullable=False, index=True),
    )
    object_id: uuid.UUID = Field(index=True)
    payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )
    status: OutboxStatus = Field(
        default=OutboxStatus.PENDING,
        sa_column=Column(String(16), nullable=False, index=True),
    )
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    delivered_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class Subscription(SQLModel, table=True):
    """A webhook subscriber: a URL, its HMAC signing secret, and the event filter."""

    __tablename__ = prefixed_tables("subscription")
    __table_args__ = (
        {"mysql_engine": settings.DB_ENGINE, "mysql_charset": settings.DB_CHARSET},
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    url: str = Field(sa_column=Column(String(2048), nullable=False))
    # Per-row HMAC signing secret (never a global env secret), so a subscriber can
    # verify the X-Signature header independently and secrets rotate per endpoint.
    secret: str = Field(sa_column=Column(String(255), nullable=False))
    # The dotted event types this subscriber wants; an empty list means "all".
    event_types: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    active: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, index=True),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
