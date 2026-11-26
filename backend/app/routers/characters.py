from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    Character,
    CharacterAssignment,
    FaceTrack,
    MediaAsset,
    ProcessingJob,
    Project,
    SpeakerTurn,
    Transcript,
    TranscriptSegment,
    Video,
    VisualCandidate,
    new_id,
    utcnow,
)
from ..schemas import (
    CharacterBundleOut,
    CharacterDetectionRequest,
    CharacterMergeRequest,
    CharacterOut,
    CharacterSplitRequest,
    CharacterUpdate,
    DialogueItemOut,
    FaceTrackOut,
    JobOut,
    SegmentAssignmentUpdate,
    SpeakerTurnOut,
    VisualCandidateOut,
    VisualCandidateUpdate,
)
from ..services import characters as characters_service
from ..services import diarization as diarization_service
from ..services import vision as vision_service
from ..services.jobs import create_character_job, run_character_job
from ..services.media import AppError
from ..services.storage import storage

router = APIRouter(tags=["characters"])


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


def build_character_out(db: Session, character: Character) -> CharacterOut:
    turns = list(db.scalars(select(SpeakerTurn).where(SpeakerTurn.character_id == character.id)))
    payload = CharacterOut.model_validate(character)
    payload.voice_characteristics = characters_service.voice_characteristics(character, turns)
    payload.dialogue_count = int(
        db.scalar(
            select(func.count())
            .select_from(CharacterAssignment)
            .where(CharacterAssignment.character_id == character.id)
        )
        or 0
    )
    payload.scene_count = characters_service.scene_count(db, character.id)
    return payload


def _asset_url(db: Session, asset_id: str | None) -> str | None:
    if not asset_id:
        return None
    asset = db.get(MediaAsset, asset_id)
    return f"/api/v1/media/{asset.id}" if asset else None


def build_bundle(db: Session, video: Video) -> CharacterBundleOut:
    job = db.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.video_id == video.id, ProcessingJob.type == "character_detection")
        .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
    )
    characters = list(
        db.scalars(
            select(Character)
            .where(Character.project_id == video.project_id)
            .order_by(Character.created_at, Character.id)
        )
    )
    turns = list(
        db.scalars(
            select(SpeakerTurn)
            .where(SpeakerTurn.video_id == video.id)
            .order_by(SpeakerTurn.start_ms, SpeakerTurn.end_ms)
        )
    )
    tracks = list(
        db.scalars(
            select(FaceTrack)
            .where(FaceTrack.video_id == video.id)
            .order_by(FaceTrack.start_ms, FaceTrack.label)
        )
    )
    candidates = list(
        db.scalars(
            select(VisualCandidate)
            .where(VisualCandidate.video_id == video.id)
            .order_by(VisualCandidate.timestamp_ms)
        )
    )
    assignments = list(
        db.scalars(
            select(CharacterAssignment)
            .where(CharacterAssignment.video_id == video.id)
            .order_by(CharacterAssignment.created_at)
        )
    )
    assignment_by_segment = {item.segment_id: item for item in assignments}

    dialogue: list[DialogueItemOut] = []
    transcript = latest_transcript(db, video.id)
    if transcript:
        for segment in db.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.transcript_id == transcript.id)
            .order_by(TranscriptSegment.position)
        ):
            assignment = assignment_by_segment.get(segment.id)
            dialogue.append(
                DialogueItemOut(
                    segment_id=segment.id,
                    character_id=assignment.character_id if assignment else None,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    text=segment.text,
                    source=assignment.source if assignment else "unassigned",
                    confidence=assignment.confidence if assignment else None,
                    reviewed=assignment.reviewed if assignment else False,
                )
            )

    metadata = (video.metadata_json or {}).get("characters") or {}
    face_out: list[FaceTrackOut] = []
    for track in tracks:
        item = FaceTrackOut.model_validate(track)
        item.representative_url = _asset_url(db, track.frame_asset_id)
        face_out.append(item)
    candidate_out: list[VisualCandidateOut] = []
    for candidate in candidates:
        item = VisualCandidateOut.model_validate(candidate)
        item.bbox = candidate.bbox_json or []
        item.frame_url = _asset_url(db, candidate.frame_asset_id)
        candidate_out.append(item)

    status = job.status if job else ("completed" if turns or metadata.get("status") == "completed" else "idle")
    return CharacterBundleOut(
        video_id=video.id,
        status=status,
        job=JobOut.model_validate(job) if job else None,
        characters=[build_character_out(db, character) for character in characters],
        speaker_turns=[SpeakerTurnOut.model_validate(item) for item in turns],
        face_tracks=face_out,
        visual_candidates=candidate_out,
        assignments=assignments,
        dialogue=dialogue,
        limitations=metadata.get("limitations") or [],
        notes=metadata.get("notes") or [],
        diarization=metadata.get("diarization") or diarization_service.production_info(),
        vision=metadata.get("vision") or vision_service.production_info(),
        metadata_json=metadata,
        updated_at=job.updated_at if job else None,
    )


@router.get("/projects/{project_id}/characters", response_model=list[CharacterOut])
def list_project_characters(project_id: str, db: Session = Depends(get_db)) -> list[CharacterOut]:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    characters = list(
        db.scalars(select(Character).where(Character.project_id == project_id).order_by(Character.created_at))
    )
    return [build_character_out(db, character) for character in characters]


@router.get("/videos/{video_id}/characters", response_model=CharacterBundleOut)
def get_character_bundle(video_id: str, db: Session = Depends(get_db)) -> CharacterBundleOut:
    return build_bundle(db, find_video(db, video_id))


@router.post("/videos/{video_id}/characters/detect", response_model=JobOut, status_code=202)
def detect_characters(
    video_id: str,
    payload: CharacterDetectionRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> JobOut:
    video = find_video(db, video_id)
    if not storage.absolute_path(video.storage_key).is_file():
        raise HTTPException(
            status_code=410,
            detail={"code": "FILE_MISSING", "message": "Stored video file is missing"},
        )
    if latest_transcript(db, video.id) is None:
        raise AppError(
            404,
            "NO_TRANSCRIPT_FOR_CHARACTER_DETECTION",
            "Transcribe this video before detecting characters.",
        )
    options = {
        "prefer_dialogue_stem": payload.prefer_dialogue_stem,
        "detect_faces": payload.detect_faces,
        "max_candidates": payload.max_candidates,
    }
    job = create_character_job(db, video.project_id, video.id, options)
    background.add_task(run_character_job, job.id, video.id, job.options_json)
    return JobOut.model_validate(job)


@router.patch("/characters/{character_id}", response_model=CharacterOut)
def update_character(
    character_id: str,
    payload: CharacterUpdate,
    db: Session = Depends(get_db),
) -> CharacterOut:
    character = db.get(Character, character_id)
    if not character:
        raise AppError(404, "CHARACTER_NOT_FOUND", "Character not found")
    if payload.name is not None:
        character.name = payload.name.strip()
    if payload.notes is not None:
        character.notes = payload.notes.strip() or None
    if payload.status is not None:
        if payload.status not in {"review", "confirmed"}:
            raise AppError(422, "INVALID_CHARACTER_STATUS", "Status must be review or confirmed.")
        character.status = payload.status
    character.updated_at = utcnow()
    db.commit()
    db.refresh(character)
    return build_character_out(db, character)


@router.post("/characters/{character_id}/merge", response_model=CharacterOut)
def merge_characters(
    character_id: str,
    payload: CharacterMergeRequest,
    db: Session = Depends(get_db),
) -> CharacterOut:
    target = db.get(Character, character_id)
    if not target:
        raise AppError(404, "CHARACTER_NOT_FOUND", "Character not found")
    target = characters_service.merge_characters(db, target, payload.character_ids, payload.name)
    db.commit()
    db.refresh(target)
    return build_character_out(db, target)


@router.post("/characters/{character_id}/split", response_model=CharacterOut)
def split_character(
    character_id: str,
    payload: CharacterSplitRequest,
    db: Session = Depends(get_db),
) -> CharacterOut:
    source = db.get(Character, character_id)
    if not source:
        raise AppError(404, "CHARACTER_NOT_FOUND", "Character not found")
    source = characters_service.split_character(
        db,
        source,
        payload.name,
        payload.segment_ids,
        payload.turn_ids,
        payload.face_track_ids,
    )
    db.commit()
    db.refresh(source)
    return build_character_out(db, source)


@router.put("/videos/{video_id}/segments/{segment_id}/character", response_model=CharacterBundleOut)
def assign_segment_character(
    video_id: str,
    segment_id: str,
    payload: SegmentAssignmentUpdate,
    db: Session = Depends(get_db),
) -> CharacterBundleOut:
    video = find_video(db, video_id)
    segment = db.get(TranscriptSegment, segment_id)
    transcript = db.get(Transcript, segment.transcript_id) if segment else None
    if not segment or not transcript or transcript.video_id != video.id:
        raise AppError(404, "SEGMENT_NOT_FOUND", "Dialogue segment not found for this video")
    if payload.character_id:
        character = db.get(Character, payload.character_id)
        if not character or character.project_id != video.project_id:
            raise AppError(404, "CHARACTER_NOT_FOUND", "Character not found for this project")
    existing = db.scalar(select(CharacterAssignment).where(CharacterAssignment.segment_id == segment.id))
    if existing is None:
        existing = CharacterAssignment(
            id=new_id(),
            character_id=payload.character_id,
            segment_id=segment.id,
            video_id=video.id,
            source="user",
            confidence=payload.confidence,
            reviewed=payload.reviewed,
        )
        db.add(existing)
    else:
        existing.character_id = payload.character_id
        existing.source = "user"
        existing.confidence = payload.confidence
        existing.reviewed = payload.reviewed
        existing.updated_at = utcnow()
    db.commit()
    return build_bundle(db, video)


@router.patch("/visual-candidates/{candidate_id}", response_model=CharacterBundleOut)
def update_visual_candidate(
    candidate_id: str,
    payload: VisualCandidateUpdate,
    db: Session = Depends(get_db),
) -> CharacterBundleOut:
    candidate = db.get(VisualCandidate, candidate_id)
    if not candidate:
        raise AppError(404, "VISUAL_CANDIDATE_NOT_FOUND", "Visual candidate not found")
    if payload.confirmed_character_id and payload.rejected:
        raise AppError(422, "VISUAL_CANDIDATE_CONFLICT", "A candidate cannot be confirmed and rejected.")
    character = None
    if payload.confirmed_character_id:
        character = db.get(Character, payload.confirmed_character_id)
        if not character or character.project_id != candidate.project_id:
            raise AppError(404, "CHARACTER_NOT_FOUND", "Character not found for this project")
    candidate.confirmed_character_id = character.id if character else None
    candidate.character_id = character.id if character else None
    candidate.confirmed = character is not None
    candidate.rejected = bool(payload.rejected) if not character else False
    if payload.notes is not None:
        candidate.notes = payload.notes.strip() or None
    candidate.updated_at = utcnow()
    if character and candidate.face_track_id:
        track = db.get(FaceTrack, candidate.face_track_id)
        if track and track.character_id is None:
            track.character_id = character.id
    db.commit()
    return build_bundle(db, find_video(db, candidate.video_id))
