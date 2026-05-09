from __future__ import annotations

import asyncio
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Optional

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import BatchJob, BatchLog, BatchStatus, DataEntry, EntryStatus
from app.tasks.celery_app import celery_app
from app.utils.logger import get_logger

logger = get_logger("entry_tasks")
settings = get_settings()

# Synchronous SQLAlchemy engine for Celery workers
_sync_db_url = settings.database_url.replace("+asyncpg", "")
_sync_engine = create_engine(_sync_db_url, pool_pre_ping=True)
SyncSession = sessionmaker(bind=_sync_engine, expire_on_commit=False)


def _batch_log(session: Session, batch_id: int, action: str, message: str, entry_id: Optional[int] = None) -> None:
    log = BatchLog(batch_job_id=batch_id, entry_id=entry_id, action=action, message=message)
    session.add(log)
    session.commit()
    logger.info(f"[batch={batch_id}] [{action}] {message}")


# ---------------------------------------------------------------------------
# Batch processing task
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, name="app.tasks.entry_tasks.process_batch_task", max_retries=1)
def process_batch_task(self, batch_job_id: int) -> dict:
    """
    Load all pending entries for a BatchJob and dispatch individual entry tasks.
    Updates the batch status to 'running'.
    """
    with SyncSession() as session:
        batch = session.get(BatchJob, batch_job_id)
        if not batch:
            raise ValueError(f"BatchJob {batch_job_id} not found")

        batch.status = BatchStatus.running
        batch.started_at = datetime.now(timezone.utc)
        session.commit()

        _batch_log(session, batch_job_id, "start", f"Processing batch for file: {batch.source_file}")

        # Fetch pending entries
        stmt = select(DataEntry).where(
            DataEntry.source_file == batch.source_file,
            DataEntry.status == EntryStatus.pending,
        ).order_by(DataEntry.row_number)
        entries = session.execute(stmt).scalars().all()
        entry_ids = [e.id for e in entries]

    logger.info(f"Batch {batch_job_id}: dispatching {len(entry_ids)} entries")

    # Dispatch entry tasks
    for entry_id in entry_ids:
        process_entry_task.apply_async(
            args=[entry_id],
            kwargs={"batch_job_id": batch_job_id},
        )

    return {"batch_job_id": batch_job_id, "entries_dispatched": len(entry_ids)}


# ---------------------------------------------------------------------------
# Single entry processing task
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, name="app.tasks.entry_tasks.process_entry_task",
                 max_retries=3, default_retry_delay=30)
def process_entry_task(self, entry_id: int, batch_job_id: Optional[int] = None) -> dict:
    """
    Process a single DataEntry:
      1. Mark as 'processing'
      2. Open browser session
      3. Log in to portal
      4. Fill and submit the form
      5. Download receipt
      6. Update record as 'completed' or 'failed'
      7. Update parent BatchJob counters
    """
    from app.automation.portal_bot import PortalBot

    with SyncSession() as session:
        entry = session.get(DataEntry, entry_id)
        if not entry:
            raise ValueError(f"DataEntry {entry_id} not found")
        entry.status = EntryStatus.processing
        session.commit()
        data_payload = dict(entry.data_payload)
        source_file = entry.source_file

    try:
        loop = asyncio.new_event_loop()
        try:
            reference_id, receipt_path = loop.run_until_complete(
                _run_portal_entry(data_payload, entry_id)
            )
        finally:
            loop.close()

        with SyncSession() as session:
            entry = session.get(DataEntry, entry_id)
            entry.status = EntryStatus.completed
            entry.portal_reference_id = reference_id
            entry.receipt_path = receipt_path
            entry.processed_at = datetime.now(timezone.utc)
            entry.error_message = None
            session.commit()
            if batch_job_id:
                _update_batch_counters(session, batch_job_id, "completed")

        logger.info(f"Entry {entry_id} completed. Reference: {reference_id}")
        return {"entry_id": entry_id, "status": "completed", "reference_id": reference_id}

    except Exception as exc:
        logger.error(f"Entry {entry_id} failed: {exc}")
        with SyncSession() as session:
            entry = session.get(DataEntry, entry_id)
            entry.status = EntryStatus.failed
            entry.error_message = str(exc)
            entry.retry_count = (entry.retry_count or 0) + 1
            entry.processed_at = datetime.now(timezone.utc)
            session.commit()
            if batch_job_id:
                _update_batch_counters(session, batch_job_id, "failed")

        raise self.retry(exc=exc, countdown=settings.retry_delay_seconds * (2 ** self.request.retries))


async def _run_portal_entry(data_payload: dict, entry_id: int):
    """Async helper: runs the full PortalBot flow for one entry."""
    from app.automation.portal_bot import PortalBot

    receipts_dir = Path(settings.receipts_dir) / f"entry_{entry_id}"
    receipts_dir.mkdir(parents=True, exist_ok=True)

    portal_config = {
        "url": settings.portal_url,
        "username": settings.portal_user,
        "password": settings.portal_pass,
    }

    async with PortalBot() as bot:
        reference_id, receipt_path = await bot.run_entry(
            portal_config=portal_config,
            entry_data=data_payload,
            receipt_dir=str(receipts_dir),
        )
    return reference_id, receipt_path


def _update_batch_counters(session: Session, batch_job_id: int, outcome: str) -> None:
    """Increment processed/completed/failed counters on the BatchJob."""
    batch = session.get(BatchJob, batch_job_id)
    if not batch:
        return
    batch.processed_rows = (batch.processed_rows or 0) + 1
    if outcome == "completed":
        batch.completed_rows = (batch.completed_rows or 0) + 1
    elif outcome == "failed":
        batch.failed_rows = (batch.failed_rows or 0) + 1
    elif outcome == "skipped":
        batch.skipped_rows = (batch.skipped_rows or 0) + 1

    # Auto-close batch when all rows processed
    total = batch.total_rows or 0
    processed = batch.processed_rows or 0
    if total > 0 and processed >= total:
        failed = batch.failed_rows or 0
        completed = batch.completed_rows or 0
        if failed == 0:
            batch.status = BatchStatus.completed
        elif completed == 0:
            batch.status = BatchStatus.failed
        else:
            batch.status = BatchStatus.partial
        batch.completed_at = datetime.now(timezone.utc)

        # Queue summary email
        send_batch_summary_task.apply_async(args=[batch_job_id], countdown=5)

    session.commit()


# ---------------------------------------------------------------------------
# Folder scanner task
# ---------------------------------------------------------------------------

@celery_app.task(name="app.tasks.entry_tasks.scan_input_folder_task")
def scan_input_folder_task() -> dict:
    """
    Scan INPUT_FILES_DIR for new CSV/Excel files that haven't been processed.
    For each new file, create a BatchJob and DataEntry records, then queue the batch.
    """
    from app.services.file_service import FileService

    input_dir = Path(settings.input_files_dir)
    input_dir.mkdir(parents=True, exist_ok=True)

    file_svc = FileService()
    new_batches = 0

    with SyncSession() as session:
        # Get already-known source files
        known_files_result = session.execute(
            select(BatchJob.source_file)
        ).scalars().all()
        known_files = set(known_files_result)

    for file_path in input_dir.iterdir():
        if file_path.suffix.lower() not in (".csv", ".xlsx", ".xls"):
            continue
        if str(file_path) in known_files:
            continue

        logger.info(f"Found new input file: {file_path}")
        try:
            df = file_svc.auto_read(str(file_path))
            if df.empty:
                logger.warning(f"File is empty, skipping: {file_path}")
                continue

            entry_records = file_svc.to_entry_records(df, str(file_path))

            with SyncSession() as session:
                batch = BatchJob(
                    source_file=str(file_path),
                    total_rows=len(entry_records),
                    status=BatchStatus.pending,
                )
                session.add(batch)
                session.flush()  # get batch.id

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
            process_batch_task.apply_async(args=[batch_id])
            new_batches += 1

        except Exception as exc:
            logger.error(f"Failed to process file {file_path}: {exc}")

    return {"scanned_dir": str(input_dir), "new_batches_created": new_batches}


# ---------------------------------------------------------------------------
# Retry failed entries task
# ---------------------------------------------------------------------------

@celery_app.task(name="app.tasks.entry_tasks.retry_failed_entries_task")
def retry_failed_entries_task() -> dict:
    """
    Find all DataEntry records with status='failed' and retry_count < MAX_RETRIES,
    then re-dispatch them.
    """
    max_retries = settings.max_retries
    dispatched = 0

    with SyncSession() as session:
        stmt = select(DataEntry).where(
            DataEntry.status == EntryStatus.failed,
            DataEntry.retry_count < max_retries,
        )
        entries = session.execute(stmt).scalars().all()
        entry_ids = [e.id for e in entries]

    logger.info(f"retry_failed_entries: found {len(entry_ids)} retriable entries")

    for entry_id in entry_ids:
        # Reset to pending
        with SyncSession() as session:
            entry = session.get(DataEntry, entry_id)
            if entry:
                entry.status = EntryStatus.pending
                session.commit()

        process_entry_task.apply_async(args=[entry_id])
        dispatched += 1

    return {"entries_requeued": dispatched}


# ---------------------------------------------------------------------------
# Batch summary email task
# ---------------------------------------------------------------------------

@celery_app.task(name="app.tasks.entry_tasks.send_batch_summary_task")
def send_batch_summary_task(batch_job_id: int) -> dict:
    """
    Send a batch completion summary email to the configured ALERT_EMAIL.
    """
    if not settings.alert_email:
        logger.info("No ALERT_EMAIL configured, skipping batch summary email")
        return {"sent": False, "reason": "no_alert_email"}

    with SyncSession() as session:
        batch = session.get(BatchJob, batch_job_id)
        if not batch:
            return {"sent": False, "reason": "batch_not_found"}
        summary = {
            "id": batch.id,
            "source_file": Path(batch.source_file).name,
            "status": batch.status.value,
            "total_rows": batch.total_rows,
            "completed_rows": batch.completed_rows,
            "failed_rows": batch.failed_rows,
            "skipped_rows": batch.skipped_rows,
            "started_at": batch.started_at.isoformat() if batch.started_at else "N/A",
            "completed_at": batch.completed_at.isoformat() if batch.completed_at else "N/A",
        }

    try:
        _send_batch_email(summary)
        logger.info(f"Batch {batch_job_id} summary email sent to {settings.alert_email}")
        return {"sent": True, "batch_id": batch_job_id}
    except Exception as exc:
        logger.error(f"Failed to send batch summary email: {exc}")
        return {"sent": False, "error": str(exc)}


def _send_batch_email(summary: dict) -> None:
    """Send a plain HTML batch summary email via SMTP."""
    subject = f"[Data Entry Bot] Batch #{summary['id']} {summary['status'].upper()} — {summary['source_file']}"
    html = f"""
    <html><body>
    <h2>Batch Job Summary</h2>
    <table border="1" cellpadding="8" cellspacing="0">
      <tr><th>Batch ID</th><td>{summary['id']}</td></tr>
      <tr><th>File</th><td>{summary['source_file']}</td></tr>
      <tr><th>Status</th><td>{summary['status']}</td></tr>
      <tr><th>Total Rows</th><td>{summary['total_rows']}</td></tr>
      <tr><th>Completed</th><td>{summary['completed_rows']}</td></tr>
      <tr><th>Failed</th><td>{summary['failed_rows']}</td></tr>
      <tr><th>Skipped</th><td>{summary['skipped_rows']}</td></tr>
      <tr><th>Started</th><td>{summary['started_at']}</td></tr>
      <tr><th>Completed</th><td>{summary['completed_at']}</td></tr>
    </table>
    <p>-- Data Entry Bot (automated)</p>
    </body></html>
    """
    msg = MIMEMultipart("alternative")
    msg["From"] = settings.smtp_user
    msg["To"] = settings.alert_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html"))

    port = settings.smtp_port
    if port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, port, timeout=30) as s:
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_pass)
            s.sendmail(settings.smtp_user, [settings.alert_email], msg.as_string())
    else:
        with smtplib.SMTP(settings.smtp_host, port, timeout=30) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_pass)
            s.sendmail(settings.smtp_user, [settings.alert_email], msg.as_string())
