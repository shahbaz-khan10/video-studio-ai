from typing import Literal

import numpy as np
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models_lipsync  # noqa: F401 — registers Phase 13 tables before init_db
from ..services import job_queue
from ..database import get_db
from ..models import TranscriptSegment, Video
from ..models_lipsync import LipSyncResult
from ..schemas import JobOut
from ..services import face_tracking as ft
from ..services import lipsync as service
from ..services import lipsync_engine as engine
from ..services.media import AppError
from ..services.storage import storage

router = APIRouter(tags=["lipsync"])
Language = Literal["ur", "hi"]


class GenerateRequest(BaseModel):
    language: Language = "ur"
    segment_ids: list[str] = Field(default_factory=list, min_length=1, max_length=500)


class FaceSelectionIn(BaseModel):
    time_ms: int = Field(ge=0)
    box: list[float] | None = None
    character_id: str | None = None
    off_screen: bool = False


class ReviewIn(BaseModel):
    decision: Literal["accepted", "rejected"]
    note: str | None = Field(default=None, max_length=1000)


def _video(db: Session, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Video not found"})
    return video


def _segment(db: Session, video: Video, segment_id: str) -> TranscriptSegment:
    segment = db.get(TranscriptSegment, segment_id)
    if segment is None or segment.transcript.video_id != video.id:
        raise AppError(404, "SEGMENT_NOT_FOUND", "Dialogue line not found for this video.")
    return segment


@router.get("/lipsync/engine")
def lipsync_engine() -> dict:
    return engine.engine_info()


@router.get("/videos/{video_id}/lipsync")
def get_lipsync(video_id: str, language: Language = Query(default="ur"), db: Session = Depends(get_db)) -> dict:
    return service.bundle(db, _video(db, video_id), language)


@router.post("/videos/{video_id}/lipsync/segments/{segment_id}/analyze")
def analyze_segment(video_id: str, segment_id: str, language: Language = Query(default="ur"), db: Session = Depends(get_db)) -> dict:
    """Detect and track faces for this line and report speaking evidence per face."""
    video = _video(db, video_id)
    segment = _segment(db, video, segment_id)
    dub = service.dub_source(db, segment, language) or {"start_ms": segment.start_ms, "end_ms": segment.end_ms}
    window = service.window_for(video, segment, dub)
    try:
        analysis = service.analyse(db, video, segment, window)
    except engine.LipSyncUnavailable as error:
        raise AppError(503, "FACE_DETECTOR_UNAVAILABLE", str(error)) from error
    selection = service.selection_for(db, segment)
    track, source, flags = ft.choose_target(analysis, list(selection.box_json) if selection and selection.box_json else None,
                                            service.phase7_box(db, segment))
    mid_ms = int((segment.start_ms + segment.end_ms) / 2)
    if track is not None:
        mid_frame = sorted(track.frames)[len(track.frames) // 2]
        mid_ms = int(round((window["start_frame"] + mid_frame) / window["fps"] * 1000))
    return {
        "segment_id": segment.id,
        "window_start_ms": window["start_ms"],
        "window_end_ms": window["end_ms"],
        "frames": analysis.frame_count,
        "faces": analysis.summary(),
        "chosen_track": track.id if track else None,
        "source": source,
        "flags": flags,
        "frame_time_ms": mid_ms,
        "frame_url": f"/api/v1/videos/{video.id}/lipsync/frame?t_ms={mid_ms}",
    }


@router.get("/videos/{video_id}/lipsync/frame")
def frame_image(video_id: str, t_ms: int = Query(ge=0), db: Session = Depends(get_db)) -> Response:
    import cv2

    video = _video(db, video_id)
    fps, width, height = service._props(video)
    index = int(round(t_ms / 1000 * fps))
    frame = next(ft.decode_frames(service.video_path(video), index, 1, fps, width, height), None)
    if frame is None:
        raise AppError(404, "FRAME_NOT_FOUND", "No frame at that time.")
    ok, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return Response(data.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@router.put("/videos/{video_id}/lipsync/segments/{segment_id}/face")
def choose_face(video_id: str, segment_id: str, payload: FaceSelectionIn, db: Session = Depends(get_db)) -> dict:
    video = _video(db, video_id)
    segment = _segment(db, video, segment_id)
    row = service.set_selection(db, video, segment, payload.time_ms, payload.box, payload.character_id, payload.off_screen)
    return {"segment_id": segment.id, "time_ms": row.time_ms, "box": row.box_json, "off_screen": row.off_screen,
            "character_id": row.character_id}


@router.delete("/videos/{video_id}/lipsync/segments/{segment_id}/face", status_code=204)
def clear_face(video_id: str, segment_id: str, db: Session = Depends(get_db)) -> Response:
    video = _video(db, video_id)
    segment = _segment(db, video, segment_id)
    row = service.selection_for(db, segment)
    if row is not None:
        db.delete(row)
        db.commit()
    return Response(status_code=204)


@router.post("/videos/{video_id}/lipsync/generate", response_model=JobOut, status_code=202)
def generate(video_id: str, payload: GenerateRequest, background: BackgroundTasks, db: Session = Depends(get_db)) -> JobOut:
    video = _video(db, video_id)
    service.video_path(video)
    service._props(video)
    for segment_id in payload.segment_ids:
        _segment(db, video, segment_id)
    if engine.effective_backend() == "none":
        raise AppError(503, "LIPSYNC_ENGINE_UNAVAILABLE", "No lip-sync engine available: add Wav2Lip weights or configure LIPSYNC_REMOTE_URL.")
    job = service.create_lipsync_job(db, video, {"language": payload.language, "segment_ids": payload.segment_ids})
    job_queue.submit(background, job)
    return JobOut.model_validate(job)


@router.post("/lipsync/results/{result_id}/review")
def review_result(result_id: str, payload: ReviewIn, db: Session = Depends(get_db)) -> dict:
    row = db.get(LipSyncResult, result_id)
    if row is None:
        raise AppError(404, "RESULT_NOT_FOUND", "Lip-sync result not found.")
    return service.serialize_result(service.review(db, row, payload.decision, payload.note))


@router.get("/lipsync/results/{result_id}/video")
def result_video(result_id: str, variant: Literal["after", "before"] = "after", download: bool = False, db: Session = Depends(get_db)):
    row = db.get(LipSyncResult, result_id)
    key = (row.after_key if variant == "after" else row.before_key) if row else None
    if not key:
        raise AppError(404, "VIDEO_NOT_FOUND", "This result has no video.")
    path = storage.absolute_path(key)
    if not path.is_file():
        raise AppError(410, "VIDEO_MISSING", "The generated clip is missing.")
    name = f"lipsync_{row.language}_{variant}_attempt{row.attempt}.mp4" if download else None
    return FileResponse(path, media_type="video/mp4", filename=name, headers={"X-AI-Generated": "true" if variant == "after" else "false"})
