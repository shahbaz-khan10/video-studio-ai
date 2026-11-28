"""Phase 14 - professional multitrack timeline editor endpoints."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models_timeline  # noqa: F401 - registers Phase 14 tables before init_db
from ..services import job_queue
from ..database import get_db
from ..models import Video
from ..models_lipsync import LipSyncResult
from ..models_timeline import TimelineClip, TimelineProject, TimelineTrack
from ..schemas import JobOut
from ..services import timeline as service
from ..services.media import AppError
from ..services.storage import storage

router = APIRouter(tags=["timeline"])
CHUNK_SIZE = 1024 * 1024


def _video(db: Session, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Video not found"})
    return video


def _timeline(db: Session, timeline_id: str) -> TimelineProject:
    timeline = db.get(TimelineProject, timeline_id)
    if not timeline:
        raise AppError(404, "TIMELINE_NOT_FOUND", "Timeline not found.")
    return timeline


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TimelineCreate(BaseModel):
    name: str = Field(default="Main timeline", max_length=160)


class TimelineUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    autosave: bool | None = None


class ClipMove(BaseModel):
    start_ms: int = Field(ge=0)
    track_id: str | None = None
    snap_ms: int = Field(default=0, ge=0)


class ClipTrim(BaseModel):
    source_in_ms: int | None = Field(default=None, ge=0)
    source_out_ms: int | None = Field(default=None, ge=1)
    start_ms: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=1)
    snap_ms: int = Field(default=0, ge=0)


class TrimEdge(BaseModel):
    delta_ms: int
    snap_ms: int = Field(default=0, ge=0)


class ClipSplit(BaseModel):
    at_ms: int = Field(ge=0)
    snap_ms: int = Field(default=0, ge=0)


class ClipJoin(BaseModel):
    clip_ids: list[str] = Field(min_length=2)


class ClipDuplicate(BaseModel):
    start_ms: int | None = Field(default=None, ge=0)


class ClipProps(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    volume_db: float | None = None
    fade_in_ms: int | None = Field(default=None, ge=0)
    fade_out_ms: int | None = Field(default=None, ge=0)
    muted: bool | None = None
    locked: bool | None = None


class ClipCharacter(BaseModel):
    character_id: str | None = None


class ClipSegment(BaseModel):
    segment_id: str | None = None


class ClipAdd(BaseModel):
    track_id: str
    source_type: str = "external_audio"
    storage_key: str | None = None
    name: str = "Clip"
    start_ms: int = Field(ge=0)
    duration_ms: int = Field(ge=1)
    source_in_ms: int = Field(default=0, ge=0)
    source_out_ms: int | None = Field(default=None, ge=1)
    source_duration_ms: int | None = Field(default=None, ge=0)
    source_id: str | None = None
    external_audio_id: str | None = None
    character_id: str | None = None
    segment_id: str | None = None
    language: str | None = None
    volume_db: float = 0.0
    metadata: dict = Field(default_factory=dict)


class ExternalClipAdd(BaseModel):
    external_audio_id: str
    start_ms: int = Field(default=0, ge=0)
    track_id: str | None = None
    source_in_ms: int = Field(default=0, ge=0)
    source_out_ms: int | None = Field(default=None, ge=1)
    character_id: str | None = None
    segment_id: str | None = None
    volume_db: float = 0.0
    name: str | None = Field(default=None, max_length=255)
    replace_clip_id: str | None = None
    snap_ms: int = Field(default=0, ge=0)


class TrackAdd(BaseModel):
    kind: str
    name: str = Field(min_length=1, max_length=120)
    language: str | None = None


class TrackUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    enabled: bool | None = None
    muted: bool | None = None
    solo: bool | None = None
    locked: bool | None = None
    volume_db: float | None = None
    pan: float | None = None
    language: str | None = None


class LipSyncCreate(BaseModel):
    segment_id: str | None = None
    language: str | None = None
    character_id: str | None = None
    start_ms: int = 0
    end_ms: int = 0
    dubbed_clip_id: str | None = None
    dubbed_track_id: str | None = None
    dubbed_audio_key: str | None = None
    dubbed_fingerprint: str | None = None
    output_storage_key: str | None = None
    output_format: str = "mp4"
    status: str = "pending"
    engine: str | None = None


class LipSyncUpdate(BaseModel):
    status: str | None = None
    output_storage_key: str | None = None
    regenerated: bool | None = None


class ExportRequest(BaseModel):
    mode: str = "multitrack"  # multitrack | separate_language
    container: str = "mkv"
    languages: list[str] | None = None
    include_beds: bool = True
    include_original_english: bool | None = None


# ---------------------------------------------------------------------------
# Timeline lifecycle
# ---------------------------------------------------------------------------


@router.post("/videos/{video_id}/timeline", status_code=201)
def create_timeline(video_id: str, payload: TimelineCreate | None = None, db: Session = Depends(get_db)) -> dict:
    video = _video(db, video_id)
    timeline = service.create_timeline(db, video, (payload.name if payload else "Main timeline"))
    return service.serialize_timeline(db, timeline)


@router.get("/videos/{video_id}/timeline")
def get_or_create(video_id: str, db: Session = Depends(get_db)) -> dict:
    video = _video(db, video_id)
    timeline = service.get_or_create_timeline(db, video)
    return service.serialize_timeline(db, timeline)


@router.get("/timeline/{timeline_id}")
def get_timeline(timeline_id: str, db: Session = Depends(get_db)) -> dict:
    return service.serialize_timeline(db, _timeline(db, timeline_id))


@router.patch("/timeline/{timeline_id}")
def update_timeline(timeline_id: str, payload: TimelineUpdate, db: Session = Depends(get_db)) -> dict:
    timeline = _timeline(db, timeline_id)
    if payload.name is not None:
        timeline.name = payload.name
    if payload.autosave is not None:
        timeline.autosave = payload.autosave
    db.commit()
    return service.serialize_timeline(db, timeline)


@router.get("/videos/{video_id}/timeline/segments")
def video_segments(video_id: str, db: Session = Depends(get_db)) -> list[dict]:
    return service.list_segments(db, _video(db, video_id).id)


@router.get("/projects/{project_id}/timeline-characters")
def project_characters(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    return service.list_characters(db, project_id)


# ---------------------------------------------------------------------------
# Undo / redo / history / save
# ---------------------------------------------------------------------------


@router.post("/timeline/{timeline_id}/undo")
def undo(timeline_id: str, db: Session = Depends(get_db)) -> dict:
    return service.undo(db, _timeline(db, timeline_id))


@router.post("/timeline/{timeline_id}/redo")
def redo(timeline_id: str, db: Session = Depends(get_db)) -> dict:
    return service.redo(db, _timeline(db, timeline_id))


@router.get("/timeline/{timeline_id}/history")
def get_history(timeline_id: str, db: Session = Depends(get_db)) -> list[dict]:
    return service.history(db, _timeline(db, timeline_id))


@router.post("/timeline/{timeline_id}/save")
def save(timeline_id: str, db: Session = Depends(get_db)) -> dict:
    """Explicit save. (Every edit already autosaves; this returns the persisted state.)"""
    timeline = _timeline(db, timeline_id)
    db.commit()
    return service.serialize_timeline(db, timeline)


# ---------------------------------------------------------------------------
# Clip editing
# ---------------------------------------------------------------------------


@router.post("/timeline/{timeline_id}/clips", status_code=201)
def add_clip(timeline_id: str, payload: ClipAdd, db: Session = Depends(get_db)) -> dict:
    clip = service.add_clip(
        db, timeline_id, payload.track_id, source_type=payload.source_type, storage_key=payload.storage_key, name=payload.name,
        start_ms=payload.start_ms, duration_ms=payload.duration_ms, source_in_ms=payload.source_in_ms,
        source_out_ms=payload.source_out_ms, source_duration_ms=payload.source_duration_ms, source_id=payload.source_id,
        external_audio_id=payload.external_audio_id, character_id=payload.character_id, segment_id=payload.segment_id,
        language=payload.language, volume_db=payload.volume_db, metadata=payload.metadata,
    )
    return service.serialize_clip(clip)


@router.post("/timeline/{timeline_id}/clips/external", status_code=201)
def add_external(timeline_id: str, payload: ExternalClipAdd, db: Session = Depends(get_db)) -> dict:
    clip = service.add_external_audio_clip(
        db, timeline_id, external_audio_id=payload.external_audio_id, start_ms=payload.start_ms, track_id=payload.track_id,
        source_in_ms=payload.source_in_ms, source_out_ms=payload.source_out_ms, character_id=payload.character_id,
        segment_id=payload.segment_id, volume_db=payload.volume_db, name=payload.name, replace_clip_id=payload.replace_clip_id,
        snap_ms=payload.snap_ms,
    )
    return service.serialize_clip(clip)


@router.post("/timeline/{timeline_id}/clips/{clip_id}/replace-with-external")
def replace_with_external(timeline_id: str, clip_id: str, payload: ExternalClipAdd, db: Session = Depends(get_db)) -> dict:
    clip = service.add_external_audio_clip(
        db, timeline_id, external_audio_id=payload.external_audio_id, start_ms=payload.start_ms, track_id=payload.track_id,
        source_in_ms=payload.source_in_ms, source_out_ms=payload.source_out_ms, character_id=payload.character_id,
        segment_id=payload.segment_id, volume_db=payload.volume_db, name=payload.name, replace_clip_id=clip_id,
        snap_ms=payload.snap_ms,
    )
    return service.serialize_clip(clip)


@router.patch("/timeline/clips/{clip_id}/move")
def move_clip(clip_id: str, payload: ClipMove, db: Session = Depends(get_db)) -> dict:
    return service.serialize_clip(service.move_clip(db, clip_id, payload.start_ms, payload.track_id, payload.snap_ms))


@router.patch("/timeline/clips/{clip_id}/trim")
def trim_clip(clip_id: str, payload: ClipTrim, db: Session = Depends(get_db)) -> dict:
    clip = service.trim_clip(
        db, clip_id, source_in_ms=payload.source_in_ms, source_out_ms=payload.source_out_ms,
        start_ms=payload.start_ms, duration_ms=payload.duration_ms, snap_ms=payload.snap_ms,
    )
    return service.serialize_clip(clip)


@router.patch("/timeline/clips/{clip_id}/trim-start")
def trim_start(clip_id: str, payload: TrimEdge, db: Session = Depends(get_db)) -> dict:
    return service.serialize_clip(service.trim_start(db, clip_id, payload.delta_ms, payload.snap_ms))


@router.patch("/timeline/clips/{clip_id}/trim-end")
def trim_end(clip_id: str, payload: TrimEdge, db: Session = Depends(get_db)) -> dict:
    return service.serialize_clip(service.trim_end(db, clip_id, payload.delta_ms, payload.snap_ms))


@router.post("/timeline/clips/{clip_id}/split")
def split_clip(clip_id: str, payload: ClipSplit, db: Session = Depends(get_db)) -> dict:
    left, right = service.split_clip(db, clip_id, payload.at_ms, payload.snap_ms)
    return {"left": service.serialize_clip(left), "right": service.serialize_clip(right)}


@router.post("/timeline/clips/join")
def join_clips(payload: ClipJoin, db: Session = Depends(get_db)) -> dict:
    return service.serialize_clip(service.join_clips(db, payload.clip_ids))


@router.delete("/timeline/clips/{clip_id}", status_code=204)
def delete_clip(clip_id: str, db: Session = Depends(get_db)) -> Response:
    service.delete_clip(db, clip_id)
    return Response(status_code=204)


@router.post("/timeline/clips/{clip_id}/duplicate", status_code=201)
def duplicate_clip(clip_id: str, payload: ClipDuplicate | None = None, db: Session = Depends(get_db)) -> dict:
    return service.serialize_clip(service.duplicate_clip(db, clip_id, payload.start_ms if payload else None))


@router.patch("/timeline/clips/{clip_id}")
def update_clip(clip_id: str, payload: ClipProps, db: Session = Depends(get_db)) -> dict:
    clip = service.set_clip_props(
        db, clip_id, name=payload.name, volume_db=payload.volume_db, fade_in_ms=payload.fade_in_ms,
        fade_out_ms=payload.fade_out_ms, muted=payload.muted, locked=payload.locked,
    )
    return service.serialize_clip(clip)


@router.patch("/timeline/clips/{clip_id}/character")
def reassign_character(clip_id: str, payload: ClipCharacter, db: Session = Depends(get_db)) -> dict:
    return service.serialize_clip(service.reassign_character(db, clip_id, payload.character_id))


@router.patch("/timeline/clips/{clip_id}/segment")
def assign_segment(clip_id: str, payload: ClipSegment, db: Session = Depends(get_db)) -> dict:
    return service.serialize_clip(service.assign_segment(db, clip_id, payload.segment_id))


@router.get("/timeline/clips/{clip_id}/waveform")
def clip_waveform(clip_id: str, buckets: int = Query(default=200, ge=1, le=4000), db: Session = Depends(get_db)) -> dict:
    clip = db.get(TimelineClip, clip_id)
    if clip is None:
        raise AppError(404, "CLIP_NOT_FOUND", "Timeline clip not found.")
    start = clip.source_in_ms
    end = clip.source_out_ms if clip.source_out_ms is not None else (clip.source_duration_ms or (start + clip.duration_ms))
    return service.source_waveform(db, clip, start, max(start + 1, end), buckets)


def _stream_file(path: Path, media_type: str, request: Request) -> Response:
    if not path.is_file():
        raise HTTPException(status_code=410, detail={"code": "FILE_MISSING", "message": "Stored source file is missing"})
    file_size = path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type=media_type, headers={"Accept-Ranges": "bytes"})
    match = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match:
        raise HTTPException(status_code=416, detail={"code": "INVALID_RANGE", "message": "Invalid byte range"})
    start_raw, end_raw = match.groups()
    if start_raw:
        start = int(start_raw)
        end = int(end_raw) if end_raw else file_size - 1
    else:
        start = max(0, file_size - int(end_raw or 0))
        end = file_size - 1
    end = min(end, file_size - 1)
    if start > end or start >= file_size:
        raise HTTPException(status_code=416, headers={"Content-Range": f"bytes */{file_size}"}, detail={"code": "RANGE_NOT_SATISFIABLE", "message": "Range outside file"})

    def iterator():
        remaining = end - start + 1
        with path.open("rb") as handle:
            handle.seek(start)
            while remaining > 0:
                chunk = handle.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(iterator(), status_code=206, media_type=media_type,
                             headers={"Content-Range": f"bytes {start}-{end}/{file_size}", "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)})


@router.get("/timeline/clips/{clip_id}/audio")
def clip_audio(clip_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    clip = db.get(TimelineClip, clip_id)
    if clip is None:
        raise AppError(404, "CLIP_NOT_FOUND", "Timeline clip not found.")
    path = service.clip_source_path(db, clip)
    if path is None:
        raise AppError(410, "SOURCE_MISSING", "Clip source file is missing.")
    media = "video/mp4" if clip.source_type == "video" else "audio/wav"
    return _stream_file(path, media, request)


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------


@router.post("/timeline/{timeline_id}/tracks", status_code=201)
def add_track(timeline_id: str, payload: TrackAdd, db: Session = Depends(get_db)) -> dict:
    return service.serialize_track(service.add_track(db, timeline_id, payload.kind, payload.name, payload.language))


@router.patch("/timeline/tracks/{track_id}")
def update_track(track_id: str, payload: TrackUpdate, db: Session = Depends(get_db)) -> dict:
    track = service.set_track_props(
        db, track_id, name=payload.name, enabled=payload.enabled, muted=payload.muted, solo=payload.solo,
        locked=payload.locked, volume_db=payload.volume_db, pan=payload.pan, language=payload.language,
    )
    return service.serialize_track(track)


@router.delete("/timeline/tracks/{track_id}", status_code=204)
def remove_track(track_id: str, db: Session = Depends(get_db)) -> Response:
    service.remove_track(db, track_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Lip sync (Task 5 / Phase 13)
# ---------------------------------------------------------------------------


@router.get("/videos/{video_id}/lip-sync-segments")
def list_lipsync(video_id: str, language: str | None = None, db: Session = Depends(get_db)) -> list[dict]:
    _video(db, video_id)
    return service.list_lipsync(db, video_id, language)


@router.patch("/lip-sync-segments/{segment_id}")
def update_lipsync(segment_id: str, payload: LipSyncUpdate, db: Session = Depends(get_db)) -> dict:
    seg = service.get_lipsync(db, segment_id)
    if payload.regenerated:
        seg = service.mark_lip_sync_regenerated(db, segment_id, payload.output_storage_key)
        return service.serialize_lipsync(db, seg)
    if payload.status is not None:
        seg.status = payload.status
    if payload.output_storage_key is not None:
        seg.after_key = payload.output_storage_key
    db.commit()
    return service.serialize_lipsync(db, seg)


@router.get("/lip-sync-segments/{segment_id}/video")
def lipsync_video(segment_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    seg = service.get_lipsync(db, segment_id)
    key = seg.after_key
    if not key:
        raise AppError(404, "LIPSYNC_NOT_FOUND", "No lip-sync video available.")
    path = storage.absolute_path(key)
    return _stream_file(path, "video/mp4", request)


# ---------------------------------------------------------------------------
# Export workflow (Task 6)
# ---------------------------------------------------------------------------


@router.get("/timeline/{timeline_id}/export-plan")
def export_plan_get(timeline_id: str, payload: ExportRequest | None = None, db: Session = Depends(get_db)) -> dict:
    timeline = _timeline(db, timeline_id)
    req = payload or ExportRequest()
    return service.export_plan(db, timeline, mode=req.mode, container=req.container, languages=req.languages,
                               include_beds=req.include_beds, include_original_english=req.include_original_english)


@router.post("/timeline/{timeline_id}/export-plan")
def export_plan_post(timeline_id: str, payload: ExportRequest, db: Session = Depends(get_db)) -> dict:
    timeline = _timeline(db, timeline_id)
    return service.export_plan(db, timeline, mode=payload.mode, container=payload.container, languages=payload.languages,
                               include_beds=payload.include_beds, include_original_english=payload.include_original_english)


@router.post("/timeline/{timeline_id}/export", response_model=JobOut, status_code=202)
def export_render(timeline_id: str, payload: ExportRequest, background: BackgroundTasks, db: Session = Depends(get_db)) -> JobOut:
    timeline = _timeline(db, timeline_id)
    video = _video(db, timeline.video_id)
    options = payload.model_dump()
    job = service.create_export_job(db, timeline, video, options)
    job_queue.submit(background, job, target_id=timeline.id)
    return JobOut.model_validate(job)


@router.get("/jobs/{job_id}/timeline-export")
def export_outputs(job_id: str, db: Session = Depends(get_db)) -> dict:
    from ..models import ProcessingJob

    job = db.get(ProcessingJob, job_id)
    if job is None:
        raise AppError(404, "NOT_FOUND", "Job not found.")
    outputs = (job.options_json or {}).get("outputs", [])
    return {"job_id": job_id, "status": job.status, "outputs": outputs}


@router.get("/timeline-exports/file")
def export_file(key: str = Query(...), db: Session = Depends(get_db)) -> Response:
    path = storage.absolute_path(key)
    if not path.is_file():
        raise AppError(410, "FILE_MISSING", "Exported file is missing.")
    media = "video/x-matroska" if key.endswith(".mkv") else "video/mp4"
    return FileResponse(path, media_type=media, filename=path.name)
