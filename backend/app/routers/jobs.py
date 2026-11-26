from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ProcessingJob
from ..schemas import JobOut
from ..services.dub_audio import run_dub_job
from ..services.translation import run_translation_job
from ..services.jobs import (
    cancel_job,
    prepare_retry_job,
    run_audio_separation_job,
    run_character_job,
    run_import_job,
    run_transcription_job,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)) -> JobOut:
    job = db.get(ProcessingJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Job not found"})
    return JobOut.model_validate(job)


@router.post("/{job_id}/cancel", response_model=JobOut)
def cancel_processing_job(job_id: str, db: Session = Depends(get_db)) -> JobOut:
    job = cancel_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Job not found"})
    return JobOut.model_validate(job)


@router.post("/{job_id}/retry", response_model=JobOut, status_code=202)
def retry_processing_job(
    job_id: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> JobOut:
    job, video_id, client_metadata = prepare_retry_job(db, job_id)
    if job.type == "audio_separation":
        background.add_task(run_audio_separation_job, job.id, video_id)
    elif job.type == "transcription":
        background.add_task(run_transcription_job, job.id, video_id, client_metadata)
    elif job.type == "character_detection":
        background.add_task(run_character_job, job.id, video_id, client_metadata)
    elif job.type == "translation":
        background.add_task(run_translation_job, job.id, video_id, client_metadata)
    elif job.type == "dub_audio_generation":
        background.add_task(run_dub_job, job.id, video_id, client_metadata)
    else:
        background.add_task(run_import_job, job.id, video_id, client_metadata)
    return JobOut.model_validate(job)
