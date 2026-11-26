from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select, update as sql_update
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import ProcessingJob, Transcript, TranscriptSegment, Video, new_id, utcnow
from ..schemas import (
    JobOut,
    TranscriptOut,
    TranscriptSegmentOut,
    TranscriptUpdate,
    TranscriptionRequest,
)
from ..services.jobs import create_transcription_job, run_transcription_job
from ..services.media import AppError
from ..services.storage import storage

router = APIRouter(tags=["transcripts"])


def find_video(db: Session, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Video not found"})
    return video


def latest_transcript(db: Session, video_id: str) -> Transcript | None:
    return db.scalar(
        select(Transcript)
        .where(Transcript.video_id == video_id)
        .order_by(Transcript.version.desc(), Transcript.created_at.desc())
    )


def build_transcript_out(transcript: Transcript | None, video: Video) -> TranscriptOut:
    if transcript is None:
        return TranscriptOut(
            video_id=video.id,
            status="idle",
            language=settings.transcription_language,
            word_timestamps=settings.transcription_word_timestamps,
        )
    metadata = transcript.metadata_json or {}
    return TranscriptOut(
        id=transcript.id,
        video_id=video.id,
        job_id=transcript.job_id,
        version=transcript.version,
        status=transcript.status,
        language=transcript.language,
        source=transcript.source,
        backend=transcript.backend,
        model=transcript.model,
        word_timestamps=transcript.word_timestamps,
        revision=transcript.revision,
        segments=[TranscriptSegmentOut.model_validate(segment) for segment in transcript.segments],
        limitations=metadata.get("limitations") or [],
        notes=metadata.get("notes") or [],
        metadata_json=metadata,
        updated_at=transcript.updated_at,
    )


@router.get("/videos/{video_id}/transcript", response_model=TranscriptOut)
def get_transcript(video_id: str, db: Session = Depends(get_db)) -> TranscriptOut:
    video = find_video(db, video_id)
    return build_transcript_out(latest_transcript(db, video.id), video)


@router.post("/videos/{video_id}/transcript/transcribe", response_model=JobOut, status_code=202)
def transcribe_video(
    video_id: str,
    payload: TranscriptionRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> JobOut:
    video = find_video(db, video_id)
    if not storage.absolute_path(video.storage_key).is_file():
        raise HTTPException(
            status_code=410,
            detail={"code": "FILE_MISSING", "message": "Stored video file is missing"},
        )
    language = payload.language.strip().lower()
    if language not in settings.supported_transcription_languages:
        raise AppError(
            422,
            "UNSUPPORTED_TRANSCRIPTION_LANGUAGE",
            f"Unsupported transcription language: {language}. Supported: {', '.join(sorted(settings.supported_transcription_languages))}",
        )
    options = {
        "language": language,
        "prefer_dialogue_stem": payload.prefer_dialogue_stem,
        "word_timestamps": payload.word_timestamps,
    }
    job = create_transcription_job(db, video.project_id, video.id, options)
    background.add_task(run_transcription_job, job.id, video.id, job.options_json)
    return JobOut.model_validate(job)


@router.put("/videos/{video_id}/transcript", response_model=TranscriptOut)
def save_transcript(
    video_id: str,
    payload: TranscriptUpdate,
    db: Session = Depends(get_db),
) -> TranscriptOut:
    video = find_video(db, video_id)
    transcript = latest_transcript(db, video.id)
    if transcript is None:
        raise AppError(404, "TRANSCRIPT_NOT_FOUND", "Transcribe this video before editing its transcript.")
    expected_revision = transcript.revision
    if payload.revision is not None and payload.revision != expected_revision:
        raise AppError(
            409,
            "TRANSCRIPT_REVISION_CONFLICT",
            f"The transcript changed in another session. Reload before saving (revision {expected_revision}).",
        )

    seen_ids: set[str] = set()
    updates: list[dict] = []
    for item in payload.segments:
        if item.id and item.id in seen_ids:
            raise AppError(422, "DUPLICATE_TRANSCRIPT_SEGMENT", "A segment id appears more than once.")
        if item.id:
            seen_ids.add(item.id)
        text = item.text.strip()
        if not text:
            raise AppError(422, "EMPTY_TRANSCRIPT_SEGMENT", "Segment text cannot be empty.")
        start = max(0, int(item.start_ms))
        end = max(0, int(item.end_ms))
        if video.duration_ms and video.duration_ms > 0:
            if start >= video.duration_ms or end > video.duration_ms:
                raise AppError(
                    422,
                    "INVALID_SEGMENT_TIMING",
                    f"Segment timing must stay within the {video.duration_ms} ms video duration.",
                )
        if end <= start:
            raise AppError(422, "INVALID_SEGMENT_TIMING", "Segment end time must be after its start time.")
        updates.append(
            {"id": item.id, "start_ms": start, "end_ms": end, "text": text, "speaker": item.speaker}
        )

    existing = {segment.id: segment for segment in transcript.segments}
    for position, update in enumerate(updates):
        segment = existing.get(update["id"] or "")
        if segment is not None:
            timing_changed = (
                segment.start_ms != update["start_ms"] or segment.end_ms != update["end_ms"]
            )
            content_changed = (
                segment.text != update["text"] or segment.speaker != update["speaker"]
            )
            if timing_changed:
                segment.start_ms = update["start_ms"]
                segment.end_ms = update["end_ms"]
                segment.words_stale = True
            if content_changed:
                segment.text = update["text"]
                segment.speaker = update["speaker"]
                segment.words_stale = True
            if timing_changed or content_changed:
                segment.edited = True
            segment.position = position
            continue
        db.add(
            TranscriptSegment(
                id=new_id(),
                transcript_id=transcript.id,
                position=position,
                start_ms=update["start_ms"],
                end_ms=update["end_ms"],
                text=update["text"],
                confidence=None,
                language=transcript.language,
                speaker=update["speaker"],
                words=[],
                edited=True,
                words_stale=True,
            )
        )

    for segment_id, segment in existing.items():
        if segment_id not in seen_ids:
            db.delete(segment)

    new_revision = expected_revision + 1
    metadata = dict(transcript.metadata_json or {})
    metadata["last_edited_at"] = utcnow().isoformat()

    # Conditional update closes the read/check/write race between concurrent
    # saves. SQLite serialises the row write; a stale revision loses the claim
    # and the pending segment changes are rolled back.
    with db.no_autoflush:
        claim = db.execute(
            sql_update(Transcript)
            .where(Transcript.id == transcript.id, Transcript.revision == expected_revision)
            .values(revision=new_revision)
        )
    if claim.rowcount != 1:
        db.rollback()
        raise AppError(
            409,
            "TRANSCRIPT_REVISION_CONFLICT",
            "The transcript changed in another session. Reload before saving.",
        )

    transcript.revision = new_revision
    transcript.status = "edited"
    transcript.metadata_json = metadata
    db.commit()
    db.refresh(transcript)
    return build_transcript_out(transcript, video)
