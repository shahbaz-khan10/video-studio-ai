from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models_timing  # noqa: F401 — registers the Phase 11 table before init_db
from ..database import get_db
from ..models import TranscriptSegment, Video
from ..models_timing import DubTimingPlacement
from ..schemas import JobOut
from ..services import dub_audio
from ..services import dub_timing as service
from ..services import tts_engine as tts
from ..services.media import AppError
from ..services.storage import storage

router = APIRouter(tags=["dub-timing"])
Language = Literal["ur", "hi"]


class FitBatchRequest(BaseModel):
    language: Language = "ur"
    mode: Literal["pending", "failed", "all"] = "pending"
    overwrite_manual: bool = False
    segment_ids: list[str] | None = None
    render_track: bool = True


class FitLineRequest(BaseModel):
    language: Language = "ur"


class PlacementUpdate(BaseModel):
    language: Language = "ur"
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=1)
    allow_overlap_ms: int | None = Field(default=None, ge=0, le=1500)
    approved: bool | None = None
    reset: bool = False


class RegenerateFasterRequest(BaseModel):
    language: Language = "ur"
    speed_factor: float = Field(default=1.1, ge=1.0, le=1.3)


def _video(db: Session, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Video not found"})
    return video


def _line(db: Session, video: Video, segment_id: str, language: str) -> dict:
    lines = service.context(db, video, language)
    line = next((item for item in lines if item["segment"].id == segment_id), None)
    if line is None:
        raise AppError(404, "SEGMENT_NOT_FOUND", "Dialogue line not found in the latest transcript.")
    return service.serialize_line(line)


@router.get("/videos/{video_id}/dub-timing")
def get_timing(video_id: str, language: Language = Query(default="ur"), db: Session = Depends(get_db)) -> dict:
    return service.bundle(db, _video(db, video_id), language)


@router.post("/videos/{video_id}/dub-timing/fit", response_model=JobOut, status_code=202)
def fit_batch(video_id: str, payload: FitBatchRequest, background: BackgroundTasks, db: Session = Depends(get_db)) -> JobOut:
    video = _video(db, video_id)
    options = payload.model_dump()
    lines = service.context(db, video, payload.language)
    chosen, skipped = service.select_lines(lines, options)
    if not chosen and not payload.render_track:
        raise AppError(409, "NOTHING_TO_FIT", "No lines need timing with these settings.")
    if not chosen and not any(line["placement"] is not None and line["placement"].storage_key for line in lines):
        detail = "Generate voice audio (Phase 10) first." if skipped["no_audio"] else "No lines need timing with these settings."
        raise AppError(409, "NOTHING_TO_FIT", detail)
    job = service.create_timing_job(db, video, options)
    background.add_task(service.run_timing_job, job.id, video.id, job.options_json)
    return JobOut.model_validate(job)


@router.post("/videos/{video_id}/dub-timing/segments/{segment_id}/fit")
def fit_one(video_id: str, segment_id: str, payload: FitLineRequest, db: Session = Depends(get_db)) -> dict:
    video = _video(db, video_id)
    lines = service.context(db, video, payload.language)
    index = next((line["index"] for line in lines if line["segment"].id == segment_id), None)
    if index is None:
        raise AppError(404, "SEGMENT_NOT_FOUND", "Dialogue line not found in the latest transcript.")
    placement = lines[index]["placement"]
    if placement is not None and placement.approved:
        raise AppError(409, "TIMING_APPROVED", "This line's timing is approved. Unapprove it before refitting.")
    service.fit_line(db, video, lines, index, payload.language)
    service.check_sequence(db, service.context(db, video, payload.language))
    return _line(db, video, segment_id, payload.language)


@router.patch("/videos/{video_id}/dub-timing/segments/{segment_id}")
def edit_placement(video_id: str, segment_id: str, payload: PlacementUpdate, db: Session = Depends(get_db)) -> dict:
    video = _video(db, video_id)
    changes = payload.model_dump(exclude_unset=True)
    changes.pop("language", None)
    existing = next(
        (line["placement"] for line in service.context(db, video, payload.language) if line["segment"].id == segment_id), None
    )
    moving = any(key in changes for key in ("start_ms", "end_ms", "allow_overlap_ms")) or changes.get("reset")
    if existing is not None and existing.approved and moving and changes.get("approved") is not False:
        raise AppError(409, "TIMING_APPROVED", "This line's timing is approved. Unapprove it before changing it.")
    service.update_placement(db, video, segment_id, payload.language, changes)
    return _line(db, video, segment_id, payload.language)


@router.post("/videos/{video_id}/dub-timing/segments/{segment_id}/regenerate-faster")
def regenerate_faster(video_id: str, segment_id: str, payload: RegenerateFasterRequest, db: Session = Depends(get_db)) -> dict:
    """Ask Phase 10 for a faster read of the same voice, then refit the line."""
    video = _video(db, video_id)
    segment = db.get(TranscriptSegment, segment_id)
    if segment is None or segment.transcript.video_id != video.id:
        raise AppError(404, "SEGMENT_NOT_FOUND", "Dialogue line not found for this video.")
    try:
        take = dub_audio.generate_take(db, video, segment, payload.language, {"speed_factor": payload.speed_factor})
    except tts.TtsUnavailable as error:
        raise AppError(503, "TTS_ENGINE_UNAVAILABLE", str(error)) from error
    if take.status != "ready":
        raise AppError(409, "REGENERATION_FAILED", take.error_message or "The faster take failed quality checks.")
    lines = service.context(db, video, payload.language)
    index = next(line["index"] for line in lines if line["segment"].id == segment_id)
    if lines[index]["placement"] is not None and lines[index]["placement"].approved:
        lines[index]["placement"].approved = False
    service.fit_line(db, video, lines, index, payload.language)
    service.check_sequence(db, service.context(db, video, payload.language))
    return _line(db, video, segment_id, payload.language)


@router.get("/dub-timing/placements/{placement_id}/audio")
def placement_audio(placement_id: str, db: Session = Depends(get_db)):
    row = db.get(DubTimingPlacement, placement_id)
    if row is None or not row.storage_key:
        raise AppError(404, "AUDIO_NOT_FOUND", "This line has no fitted audio.")
    path = storage.absolute_path(row.storage_key)
    if not path.is_file():
        raise AppError(410, "AUDIO_MISSING", "The fitted audio file is missing.")
    return FileResponse(path, media_type="audio/wav", headers={"X-Synthetic-Audio": "true", "Cache-Control": "no-cache"})


@router.get("/videos/{video_id}/dub-timing/track")
def dialogue_track(video_id: str, language: Language = Query(default="ur"), download: bool = False, db: Session = Depends(get_db)):
    video = _video(db, video_id)
    info = service.track_info(db, video, language)
    if not info or not info["exists"]:
        raise AppError(404, "TRACK_NOT_RENDERED", "Run timing fitting to render the dialogue track.")
    name = f"dub_dialogue_{language}.wav" if download else None
    return FileResponse(storage.absolute_path(info["track_key"]), media_type="audio/wav", filename=name,
                        headers={"X-Synthetic-Audio": "true", "Cache-Control": "no-cache"})


@router.post("/videos/{video_id}/dub-timing/track/render")
def rerender_track(video_id: str, language: Language = Query(default="ur"), db: Session = Depends(get_db)) -> dict:
    video = _video(db, video_id)
    service.render_track(db, video, language)
    return service.track_info(db, video, language) or {}


@router.get("/videos/{video_id}/dub-timing/waveform")
def get_waveform(
    video_id: str,
    source: Literal["original", "ur", "hi"] = "original",
    start_ms: int = Query(default=0, ge=0),
    end_ms: int = Query(default=30000, ge=1),
    buckets: int = Query(default=600, ge=10, le=4000),
    db: Session = Depends(get_db),
) -> dict:
    if end_ms - start_ms > 15 * 60 * 1000:
        raise AppError(422, "RANGE_TOO_LONG", "Request at most 15 minutes of waveform at a time.")
    return service.waveform(db, _video(db, video_id), source, start_ms, end_ms, buckets)
