from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models_tts  # noqa: F401 — registers the Phase 10 table before init_db
from ..database import get_db
from ..models import Character, TranscriptSegment, Video
from ..models_tts import DubAudioTake
from ..schemas import JobOut
from ..services import dub_audio as service
from ..services import tts_engine as tts
from ..services.media import AppError
from ..services.storage import storage

router = APIRouter(tags=["dub-audio"])

Language = Literal["ur", "hi"]
Emotion = Literal["neutral", "happy", "sad", "angry", "fearful", "excited", "calm", "whispering", "shouting", "serious"]


class GenerateLineRequest(BaseModel):
    language: Language = "ur"
    emotion: Emotion = "neutral"
    speed_factor: float | None = Field(default=None, ge=0.5, le=2.0)
    pitch_offset: float | None = Field(default=None, ge=-12, le=12)
    pause_ms: int | None = Field(default=None, ge=0, le=2000)
    new_variation: bool = False


class GenerateBatchRequest(BaseModel):
    language: Language = "ur"
    mode: Literal["pending", "missing", "failed", "all"] = "pending"
    include_approved: bool = False
    segment_ids: list[str] | None = None
    character_id: str | None = None
    emotion: Emotion | None = None


class ApproveRequest(BaseModel):
    approved: bool = True


class PreviewRequest(BaseModel):
    language: Language = "ur"
    text: str | None = Field(default=None, max_length=300)
    emotion: Emotion = "neutral"


def _video(db: Session, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Video not found"})
    return video


def _take(db: Session, take_id: str) -> DubAudioTake:
    take = db.get(DubAudioTake, take_id)
    if not take:
        raise AppError(404, "TAKE_NOT_FOUND", "Audio take not found.")
    return take


def _line(db: Session, video: Video, segment_id: str, language: str) -> dict:
    data = service.bundle(db, video, language)
    line = next((item for item in data["lines"] if item["segment_id"] == segment_id), None)
    if line is None:
        raise AppError(409, "SEGMENT_NOT_CURRENT", "This segment is not part of the latest transcript.")
    return line


@router.get("/dub-audio/engine")
def dub_audio_engine() -> dict:
    return tts.engine_info()


@router.get("/videos/{video_id}/dub-audio")
def get_dub_audio(video_id: str, language: Language = Query(default="ur"), db: Session = Depends(get_db)) -> dict:
    return service.bundle(db, _video(db, video_id), language)


@router.post("/videos/{video_id}/dub-audio/segments/{segment_id}/generate")
def generate_line(
    video_id: str, segment_id: str, payload: GenerateLineRequest, db: Session = Depends(get_db)
) -> dict:
    video = _video(db, video_id)
    segment = db.get(TranscriptSegment, segment_id)
    if segment is None or segment.transcript.video_id != video.id:
        raise AppError(404, "SEGMENT_NOT_FOUND", "Dialogue segment not found for this video.")
    try:
        service.generate_take(
            db,
            video,
            segment,
            payload.language,
            payload.model_dump(exclude={"language"}),
            raise_engine_errors=True,
        )
    except tts.TtsUnavailable as error:
        raise AppError(503, "TTS_ENGINE_UNAVAILABLE", str(error)) from error
    return _line(db, video, segment_id, payload.language)


@router.post("/videos/{video_id}/dub-audio/generate", response_model=JobOut, status_code=202)
def generate_batch(
    video_id: str, payload: GenerateBatchRequest, background: BackgroundTasks, db: Session = Depends(get_db)
) -> JobOut:
    video = _video(db, video_id)
    if service.latest_transcript(db, video.id) is None:
        raise AppError(404, "TRANSCRIPT_NOT_FOUND", "Transcribe and translate this video first.")
    options = payload.model_dump()
    chosen, _skipped = service.select_batch(db, video, payload.language, options)
    if not chosen:
        raise AppError(409, "NOTHING_TO_GENERATE", "No lines need audio with these settings (approved and up-to-date lines are skipped).")
    job = service.create_dub_job(db, video, options)
    background.add_task(service.run_dub_job, job.id, video.id, job.options_json)
    return JobOut.model_validate(job)


@router.get("/dub-audio/takes/{take_id}/audio")
def take_audio(take_id: str, download: bool = False, db: Session = Depends(get_db)):
    take = _take(db, take_id)
    if not take.storage_key:
        raise AppError(404, "AUDIO_NOT_FOUND", "This take has no audio (generation failed).")
    path = storage.absolute_path(take.storage_key)
    if not path.is_file():
        raise AppError(410, "AUDIO_MISSING", "The stored audio file is missing.")
    segment = db.get(TranscriptSegment, take.segment_id)
    character = db.get(Character, take.character_id) if take.character_id else None
    name = f"{(character.name if character else 'line').replace(' ', '_')}_{take.language}_{(segment.position + 1) if segment else 0:04d}_take{take.take_number}.wav"
    return FileResponse(
        path,
        media_type="audio/wav",
        filename=name if download else None,
        headers={"X-Synthetic-Audio": "true", "Cache-Control": "no-cache"},
    )


@router.post("/dub-audio/takes/{take_id}/approve")
def approve_take(take_id: str, payload: ApproveRequest, db: Session = Depends(get_db)) -> dict:
    take = service.set_approved(db, _take(db, take_id), payload.approved)
    return _line(db, db.get(Video, take.video_id), take.segment_id, take.language)


@router.post("/dub-audio/takes/{take_id}/select")
def select_take(take_id: str, db: Session = Depends(get_db)) -> dict:
    take = service.select_take(db, _take(db, take_id))
    return _line(db, db.get(Video, take.video_id), take.segment_id, take.language)


@router.post("/videos/{video_id}/dub-audio/voices/{voice_key}/preview")
def preview(video_id: str, voice_key: str, payload: PreviewRequest, db: Session = Depends(get_db)) -> Response:
    video = _video(db, video_id)
    character = None
    if not voice_key.startswith("label:"):
        character = db.get(Character, voice_key)
        if character is None or character.project_id != video.project_id:
            raise AppError(404, "CHARACTER_NOT_FOUND", "Character not found in this project.")
    try:
        rendered = service.preview_voice(db, character, payload.language, payload.text, {"emotion": payload.emotion})
    except tts.TtsUnavailable as error:
        raise AppError(503, "TTS_ENGINE_UNAVAILABLE", str(error)) from error
    return Response(rendered.wav, media_type="audio/wav", headers={"X-Synthetic-Audio": "true"})
