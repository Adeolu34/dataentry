from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import BatchJob, BatchStatus, DataEntry, EntryStatus
from app.services.file_service import FileService
from app.utils.logger import get_logger

logger = get_logger("portal_service")
settings = get_settings()

_sync_db_url = settings.database_url.replace("+asyncpg", "")
_sync_engine = create_engine(_sync_db_url, pool_pre_ping=True)
SyncSession = sessionmaker(bind=_sync_engine, expire_on_commit=False)


@dataclass
class BatchResult:
    batch_job_id: int
    source_file: str
    total_rows: int
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if self.total_rows == 0:
            return 0.0
        return round(self.completed / self.total_rows * 100, 1)

    @property
    def is_success(self) -> bool:
        return self.failed == 0 and self.completed > 0


class PortalService:
    """
    High-level orchestrator that coordinates FileService + EntryBot.

    Designed to be called from API endpoints or Celery tasks that need
    a simple, synchronous interface for processing a whole file.
    """

    def __init__(self) -> None:
        self._file_svc = FileService()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_file(self, file_path: str) -> BatchResult:
        """
        End-to-end pipeline for a single input file:

        1. Read and validate the file
        2. Create BatchJob + DataEntry records in the database
        3. Dispatch the batch Celery task
        4. Return a BatchResult with job metadata

        The actual entry processing happens asynchronously in Celery workers.
        Call ``get_entry_status`` or poll the API to check progress.
        """
        from app.tasks.entry_tasks import process_batch_task

        path = Path(file_path)
        logger.info(f"process_file: {path.name}")

        # Read
        df = self._file_svc.auto_read(file_path)

        # Validate (warn but don't block on missing columns — portal-specific)
        validation = self._file_svc.validate_schema(df, required_columns=[])
        if not validation.is_valid:
            logger.warning(f"File validation issues: {validation.errors}")

        # Convert rows → DataEntry records
        entry_records = self._file_svc.to_entry_records(df, file_path)

        batch_id: int
        with SyncSession() as session:
            batch = BatchJob(
                source_file=str(path),
                total_rows=len(entry_records),
                status=BatchStatus.pending,
            )
            session.add(batch)
            session.flush()

            for record in entry_records:
                entry = DataEntry(
                    source_file=record["source_file"],
                    row_number=record["row_number"],
                    data_payload=record["data_payload"],
                    status=EntryStatus.pending,
                )
                session.add(entry)

            session.commit()
            batch_id = batch.id

        logger.info(f"Created BatchJob {batch_id} with {len(entry_records)} entries")

        # Queue async processing
        process_batch_task.apply_async(args=[batch_id])

        return BatchResult(
            batch_job_id=batch_id,
            source_file=str(path),
            total_rows=len(entry_records),
        )

    def get_entry_status(self, entry_id: int) -> dict:
        """
        Return the current status dictionary for a single DataEntry.

        Raises ValueError if the entry is not found.
        """
        with SyncSession() as session:
            entry = session.get(DataEntry, entry_id)
            if not entry:
                raise ValueError(f"DataEntry {entry_id} not found")

            return {
                "id": entry.id,
                "source_file": Path(entry.source_file).name,
                "row_number": entry.row_number,
                "status": entry.status.value,
                "portal_reference_id": entry.portal_reference_id,
                "receipt_path": entry.receipt_path,
                "retry_count": entry.retry_count,
                "error_message": entry.error_message,
                "processed_at": (
                    entry.processed_at.isoformat() if entry.processed_at else None
                ),
                "created_at": entry.created_at.isoformat(),
            }

    def get_batch_status(self, batch_job_id: int) -> dict:
        """Return a status summary for a BatchJob."""
        with SyncSession() as session:
            batch = session.get(BatchJob, batch_job_id)
            if not batch:
                raise ValueError(f"BatchJob {batch_job_id} not found")

            return {
                "id": batch.id,
                "source_file": Path(batch.source_file).name,
                "status": batch.status.value,
                "total_rows": batch.total_rows,
                "processed_rows": batch.processed_rows,
                "completed_rows": batch.completed_rows,
                "failed_rows": batch.failed_rows,
                "skipped_rows": batch.skipped_rows,
                "success_rate_pct": (
                    round(batch.completed_rows / batch.total_rows * 100, 1)
                    if batch.total_rows
                    else 0
                ),
                "started_at": batch.started_at.isoformat() if batch.started_at else None,
                "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
                "created_at": batch.created_at.isoformat(),
            }
