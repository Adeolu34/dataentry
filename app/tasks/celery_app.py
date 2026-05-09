from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "data_entry_bot",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.entry_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    # Only one entry at a time per worker to avoid browser session conflicts
    worker_prefetch_multiplier=1,
    task_routes={
        "app.tasks.entry_tasks.process_batch_task": {"queue": "batches"},
        "app.tasks.entry_tasks.process_entry_task": {"queue": "entries"},
        "app.tasks.entry_tasks.scan_input_folder_task": {"queue": "scanner"},
        "app.tasks.entry_tasks.retry_failed_entries_task": {"queue": "retries"},
        "app.tasks.entry_tasks.send_batch_summary_task": {"queue": "notifications"},
    },
)

# ---------------------------------------------------------------------------
# Beat schedule
# ---------------------------------------------------------------------------
celery_app.conf.beat_schedule = {
    # Scan for new input files every 10 minutes
    "scan-input-folder": {
        "task": "app.tasks.entry_tasks.scan_input_folder_task",
        "schedule": crontab(minute="*/10"),
    },
    # Retry failed entries every 30 minutes
    "retry-failed-entries": {
        "task": "app.tasks.entry_tasks.retry_failed_entries_task",
        "schedule": crontab(minute="*/30"),
    },
}
