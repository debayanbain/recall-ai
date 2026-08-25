"""A deferred extraction handed off to a third party (today: Apify).

Exists because long scrapes must not block a worker. The worker triggers a run and
returns immediately; the run finishes minutes later on the provider's infrastructure and
announces itself via webhook. This row is the correlation between the two halves —
without it a webhook naming `run_id` has no way back to a vault item.

It is also the audit trail the sweeper reads: a run that never called back is only
detectable because its row is still `running` past a deadline.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Column, DateTime, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models.base import new_uuid, utcnow


class RunStatus(StrEnum):
    """Stored as text, not a PG enum, so adding a state never needs ALTER TYPE."""

    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    timed_out = "timed_out"


class ExtractionRun(SQLModel, table=True):
    __tablename__ = "extraction_runs"
    __table_args__ = (
        # The webhook is at-least-once and the sweeper races it, so the provider's run id
        # is the idempotency key: a second delivery finds the row already terminal.
        UniqueConstraint("provider", "provider_run_id", name="uq_extraction_run_provider"),
    )

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    vault_item_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("vault_items.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    provider: str = Field(default="apify", sa_column=Column(Text, nullable=False))
    provider_run_id: str = Field(sa_column=Column(Text, nullable=False))
    dataset_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    status: str = Field(
        default=RunStatus.running, sa_column=Column(Text, nullable=False, index=True)
    )
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
