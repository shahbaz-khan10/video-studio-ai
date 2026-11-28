"""Phase 15 — processing status: workers, queue, resources, models and cleanup."""

from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models_workers  # noqa: F401 — registers Phase 15 tables
from ..database import get_db
from ..models import ProcessingJob, utcnow
from ..models_workers import ProcessingWorker
from ..services import job_queue, resources
from ..services.media import AppError

router = APIRouter(prefix="/processing", tags=["processing"])


def _worker_out(row: ProcessingWorker) -> dict:
    age = (utcnow() - job_queue._aware(row.last_heartbeat_at)).total_seconds()
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "classes": row.classes,
        "status": "offline" if age > job_queue.processing_settings.worker_offline_after_seconds else row.status,
        "host": row.host,
        "url": row.url,
        "current_job_id": row.current_job_id,
        "jobs_completed": row.jobs_completed,
        "jobs_failed": row.jobs_failed,
        "seconds_since_heartbeat": round(age, 1),
        "resources": row.resources_json,
        "started_at": row.started_at.isoformat() if row.started_at else None,
    }


@router.get("/status")
def processing_status(db: Session = Depends(get_db)) -> dict:
    counts = dict(db.execute(select(ProcessingJob.status, func.count()).group_by(ProcessingJob.status)).all())
    queued_by_type = dict(
        db.execute(select(ProcessingJob.type, func.count()).where(ProcessingJob.status == "queued").group_by(ProcessingJob.type)).all()
    )
    recent = utcnow() - timedelta(days=7)
    workers = [_worker_out(w) for w in db.scalars(select(ProcessingWorker).where(ProcessingWorker.last_heartbeat_at >= recent))]
    online = [w for w in workers if w["status"] != "offline"]
    capable = {cls for w in online for cls in (w["classes"] or [])}
    unservable = sorted(
        t for t in queued_by_type if not job_queue.job_type(t).classes <= capable
    ) if job_queue.processing_settings.mode == "queue" else []
    return {
        "mode": job_queue.processing_settings.mode,
        "jobs": {"queued": counts.get("queued", 0), "running": counts.get("running", 0), "completed": counts.get("completed", 0),
                 "failed": counts.get("failed", 0), "cancelled": counts.get("cancelled", 0)},
        "queued_by_type": queued_by_type,
        "waiting_for_capability": unservable,
        "workers": workers,
        "job_types": {
            name: {"label": t.label, "requires": sorted(t.classes), "gpu_preferred": t.gpu_preferred, "resumable": t.resumable}
            for name, t in job_queue.JOB_TYPES.items()
        },
        "settings": {
            "lease_seconds": job_queue.processing_settings.lease_seconds,
            "heartbeat_seconds": job_queue.processing_settings.heartbeat_seconds,
            "max_attempts": job_queue.processing_settings.max_attempts,
            "idle_unload_minutes": job_queue.processing_settings.idle_unload_minutes,
            "temp_retention_hours": job_queue.processing_settings.temp_retention_hours,
        },
    }


@router.get("/remote")
def remote_status() -> dict:
    from ..services import remote_mode

    if not remote_mode.enabled():
        return {"enabled": False, "note": "Set PROCESSING_REMOTE_URL and PROCESSING_REMOTE_TOKEN to use a separately configured processing server."}
    return {"enabled": True, "url": remote_mode.remote_settings.remote_url, **remote_mode.refresh_worker_row()}


@router.get("/resources")
def processing_resources() -> dict:
    return resources.snapshot(include_models=True, light=False)


@router.post("/models/unload")
def unload_models() -> dict:
    return {"released": resources.unload_models(reason="manual"), "models_loaded": resources.loaded_models()}


@router.post("/cleanup")
def cleanup(dry_run: bool = Query(default=True), older_than_hours: float | None = Query(default=None, ge=0)) -> dict:
    return resources.cleanup_temp(dry_run=dry_run, older_than_hours=older_than_hours)


@router.post("/recover")
def recover_now(db: Session = Depends(get_db)) -> dict:
    return job_queue.recover(db, redispatch_inline=False)


@router.get("/jobs")
def job_list(
    status: str | None = None,
    project_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = select(ProcessingJob).order_by(ProcessingJob.created_at.desc()).limit(limit)
    if status:
        query = query.where(ProcessingJob.status == status)
    if project_id:
        query = query.where(ProcessingJob.project_id == project_id)
    return [
        {
            "id": job.id, "type": job.type, "label": job_queue.job_type(job.type).label, "status": job.status,
            "progress": job.progress, "current_step": job.current_step, "project_id": job.project_id, "video_id": job.video_id,
            "worker_id": job.worker_id, "attempts": job.attempts, "error_code": job.error_code, "error_message": job.error_message,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        }
        for job in db.scalars(query)
    ]


@router.get("/workers/{worker_id}")
def worker_detail(worker_id: str, db: Session = Depends(get_db)) -> dict:
    row = db.get(ProcessingWorker, worker_id)
    if row is None:
        raise AppError(404, "WORKER_NOT_FOUND", "Worker not found.")
    return _worker_out(row)
