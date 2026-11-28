from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ProcessingJob
from ..schemas import JobOut
from ..services import job_queue
from ..services.jobs import cancel_job, prepare_retry_job

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)) -> JobOut:
    job = db.get(ProcessingJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Job not found"})
    return JobOut.model_validate(job)


@router.get("/{job_id}/history")
def get_job_history(job_id: str, db: Session = Depends(get_db)) -> dict:
    """Every attempt of a job: which worker ran it, how it ended, peak memory."""
    job = db.get(ProcessingJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Job not found"})
    kind = job_queue.job_type(job.type)
    return {
        "job_id": job.id,
        "type": job.type,
        "label": kind.label,
        "status": job.status,
        "attempts": job.attempts,
        "worker_id": job.worker_id,
        "requires": sorted(kind.classes),
        "gpu_preferred": kind.gpu_preferred,
        "resumable": kind.resumable,
        "history": job_queue.job_history(db, job.id),
    }


@router.post("/{job_id}/cancel", response_model=JobOut)
def cancel_processing_job(job_id: str, db: Session = Depends(get_db)) -> JobOut:
    job = cancel_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Job not found"})
    return JobOut.model_validate(job)


@router.post("/{job_id}/retry", response_model=JobOut, status_code=202)
def retry_processing_job(job_id: str, background: BackgroundTasks, db: Session = Depends(get_db)) -> JobOut:
    job, _video_id, client_metadata = prepare_retry_job(db, job_id)
    job_queue.submit(background, job, metadata=client_metadata if job_queue.job_type(job.type).args == "metadata" else None)
    return JobOut.model_validate(job)
