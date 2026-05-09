from __future__ import annotations

import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import close_db, get_db, init_db
from app.models import (
    BatchJob,
    BatchLog,
    BatchStatus,
    DataEntry,
    EntryStatus,
)
from app.utils.logger import get_logger

logger = get_logger("main")
settings = get_settings()

app = FastAPI(
    title="Data Entry Bot API",
    description="Automated web portal data entry with Celery + Playwright",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup() -> None:
    logger.info("Starting Data Entry Bot API")
    await init_db()
    logger.info("Database initialized")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await close_db()
    logger.info("Data Entry Bot API shut down")


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class BatchJobOut(BaseModel):
    id: int
    source_file: str
    total_rows: int
    processed_rows: int
    completed_rows: int
    failed_rows: int
    skipped_rows: int
    status: str
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class DataEntryOut(BaseModel):
    id: int
    source_file: str
    row_number: int
    status: str
    portal_reference_id: Optional[str]
    receipt_path: Optional[str]
    retry_count: int
    error_message: Optional[str]
    processed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BatchLogOut(BaseModel):
    id: int
    batch_job_id: int
    entry_id: Optional[int]
    action: str
    message: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Batch endpoints
# ---------------------------------------------------------------------------

@app.post("/batches/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_batch(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Upload a CSV or Excel file to create a new batch job.

    The file is saved to INPUT_FILES_DIR and a batch job is created immediately.
    Processing is queued in Celery.
    """
    from app.services.portal_service import PortalService

    allowed_extensions = {".csv", ".xlsx", ".xls"}
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix}'. Allowed: {', '.join(allowed_extensions)}",
        )

    input_dir = Path(settings.input_files_dir)
    input_dir.mkdir(parents=True, exist_ok=True)
    dest = input_dir / (file.filename or f"upload_{datetime.now():%Y%m%d%H%M%S}{suffix}")

    # Avoid overwrite
    counter = 1
    while dest.exists():
        dest = input_dir / f"{dest.stem}_{counter}{dest.suffix}"
        counter += 1

    with open(dest, "wb") as fh:
        shutil.copyfileobj(file.file, fh)

    logger.info(f"Uploaded file saved: {dest}")

    portal_svc = PortalService()
    result = portal_svc.process_file(str(dest))

    return {
        "message": "Batch job created and queued",
        "batch_job_id": result.batch_job_id,
        "source_file": Path(result.source_file).name,
        "total_rows": result.total_rows,
    }


@app.post("/batches/trigger", status_code=status.HTTP_202_ACCEPTED)
async def trigger_folder_scan() -> dict:
    """Manually trigger a scan of the INPUT_FILES_DIR for new files."""
    from app.tasks.entry_tasks import scan_input_folder_task

    task = scan_input_folder_task.apply_async()
    return {"message": "Folder scan triggered", "task_id": task.id}


@app.get("/batches", response_model=List[BatchJobOut])
async def list_batches(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> List[BatchJob]:
    stmt = select(BatchJob).order_by(BatchJob.created_at.desc()).limit(limit).offset(offset)

    if status_filter:
        try:
            stmt = stmt.where(BatchJob.status == BatchStatus(status_filter))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status_filter}")

    result = await db.execute(stmt)
    return result.scalars().all()


@app.get("/batches/{batch_id}", response_model=BatchJobOut)
async def get_batch(
    batch_id: int,
    db: AsyncSession = Depends(get_db),
) -> BatchJob:
    batch = await db.get(BatchJob, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail=f"BatchJob {batch_id} not found")
    return batch


@app.get("/batches/{batch_id}/entries", response_model=List[DataEntryOut])
async def get_batch_entries(
    batch_id: int,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> List[DataEntry]:
    batch = await db.get(BatchJob, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail=f"BatchJob {batch_id} not found")

    stmt = (
        select(DataEntry)
        .where(DataEntry.source_file == batch.source_file)
        .order_by(DataEntry.row_number)
        .limit(limit)
        .offset(offset)
    )

    if status_filter:
        try:
            stmt = stmt.where(DataEntry.status == EntryStatus(status_filter))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status_filter}")

    result = await db.execute(stmt)
    return result.scalars().all()


# ---------------------------------------------------------------------------
# Entry endpoints
# ---------------------------------------------------------------------------

@app.get("/entries/{entry_id}", response_model=DataEntryOut)
async def get_entry(
    entry_id: int,
    db: AsyncSession = Depends(get_db),
) -> DataEntry:
    entry = await db.get(DataEntry, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"DataEntry {entry_id} not found")
    return entry


@app.post("/entries/{entry_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_entry(
    entry_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Manually re-queue a failed or skipped entry for processing."""
    from app.tasks.entry_tasks import process_entry_task

    entry = await db.get(DataEntry, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"DataEntry {entry_id} not found")

    if entry.status not in (EntryStatus.failed, EntryStatus.skipped):
        raise HTTPException(
            status_code=400,
            detail=f"Entry {entry_id} has status '{entry.status}'; only failed/skipped entries can be retried",
        )

    if entry.retry_count >= settings.max_retries:
        raise HTTPException(
            status_code=400,
            detail=f"Entry {entry_id} has reached max retries ({settings.max_retries})",
        )

    entry.status = EntryStatus.pending
    await db.flush()

    task = process_entry_task.apply_async(args=[entry_id])
    return {"message": "Entry re-queued", "entry_id": entry_id, "task_id": task.id}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/dashboard/stats")
async def dashboard_stats(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Aggregate stats for the monitoring dashboard."""
    today = datetime.now(timezone.utc).date()
    today_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)

    # Today's processed count
    today_processed_stmt = select(func.count(DataEntry.id)).where(
        DataEntry.processed_at >= today_start,
        DataEntry.status == EntryStatus.completed,
    )
    today_processed = (await db.execute(today_processed_stmt)).scalar() or 0

    # Total counts by status
    total_stmt = select(func.count(DataEntry.id))
    completed_stmt = select(func.count(DataEntry.id)).where(DataEntry.status == EntryStatus.completed)
    failed_stmt = select(func.count(DataEntry.id)).where(DataEntry.status == EntryStatus.failed)
    pending_stmt = select(func.count(DataEntry.id)).where(DataEntry.status == EntryStatus.pending)
    processing_stmt = select(func.count(DataEntry.id)).where(DataEntry.status == EntryStatus.processing)

    total = (await db.execute(total_stmt)).scalar() or 0
    completed = (await db.execute(completed_stmt)).scalar() or 0
    failed = (await db.execute(failed_stmt)).scalar() or 0
    pending = (await db.execute(pending_stmt)).scalar() or 0
    processing = (await db.execute(processing_stmt)).scalar() or 0

    success_rate = round((completed / total * 100) if total > 0 else 0, 1)

    # Active batches
    active_batches_stmt = select(func.count(BatchJob.id)).where(
        BatchJob.status.in_([BatchStatus.pending, BatchStatus.running])
    )
    active_batches = (await db.execute(active_batches_stmt)).scalar() or 0

    return {
        "today_processed": today_processed,
        "total_entries": total,
        "completed_entries": completed,
        "failed_entries": failed,
        "pending_entries": pending,
        "processing_entries": processing,
        "success_rate_pct": success_rate,
        "active_batches": active_batches,
    }


@app.get("/dashboard/recent-failures")
async def recent_failures(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return the last 20 failed entries with error details."""
    stmt = (
        select(DataEntry)
        .where(DataEntry.status == EntryStatus.failed)
        .order_by(DataEntry.updated_at.desc())
        .limit(20)
    )
    result = await db.execute(stmt)
    entries = result.scalars().all()

    return {
        "count": len(entries),
        "failures": [
            {
                "id": e.id,
                "source_file": Path(e.source_file).name,
                "row_number": e.row_number,
                "retry_count": e.retry_count,
                "error_message": e.error_message,
                "data_payload_preview": {
                    k: v for i, (k, v) in enumerate(e.data_payload.items()) if i < 5
                },
                "updated_at": e.updated_at.isoformat() if e.updated_at else None,
            }
            for e in entries
        ],
    }


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "data_entry_bot",
        "timestamp": datetime.utcnow().isoformat(),
    }
