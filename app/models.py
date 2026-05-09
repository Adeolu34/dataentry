from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EntryStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"


class BatchStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    partial = "partial"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class DataEntry(Base):
    __tablename__ = "data_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source_file: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    data_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[EntryStatus] = mapped_column(
        Enum(EntryStatus, name="entry_status_enum"),
        nullable=False,
        default=EntryStatus.pending,
        index=True,
    )
    portal_reference_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    receipt_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    logs: Mapped[list[BatchLog]] = relationship(
        "BatchLog", back_populates="entry", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<DataEntry id={self.id} file={self.source_file!r} row={self.row_number} status={self.status}>"


class BatchJob(Base):
    __tablename__ = "batch_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source_file: Mapped[str] = mapped_column(String(512), nullable=False)
    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[BatchStatus] = mapped_column(
        Enum(BatchStatus, name="batch_status_enum"),
        nullable=False,
        default=BatchStatus.pending,
        index=True,
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    logs: Mapped[list[BatchLog]] = relationship(
        "BatchLog",
        back_populates="batch_job",
        cascade="all, delete-orphan",
        foreign_keys="BatchLog.batch_job_id",
    )

    def __repr__(self) -> str:
        return f"<BatchJob id={self.id} file={self.source_file!r} status={self.status}>"


class BatchLog(Base):
    __tablename__ = "batch_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    batch_job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("batch_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entry_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("data_entries.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    batch_job: Mapped[BatchJob] = relationship(
        "BatchJob", back_populates="logs", foreign_keys=[batch_job_id]
    )
    entry: Mapped[Optional[DataEntry]] = relationship(
        "DataEntry", back_populates="logs", foreign_keys=[entry_id]
    )

    def __repr__(self) -> str:
        return f"<BatchLog id={self.id} batch={self.batch_job_id} action={self.action}>"
