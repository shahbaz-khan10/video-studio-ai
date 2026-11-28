from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..services import job_queue
from ..database import get_db
from ..models import (
    Character,
    CharacterRelationship,
    CharacterTranslationProfile,
    ProcessingJob,
    Project,
    SegmentTranslation,
    TranscriptSegment,
    TranslationGlossaryTerm,
    TranslationSceneNote,
    Video,
)
from ..schemas import (
    GlossaryUpdate,
    JobOut,
    RelationshipsUpdate,
    SceneNoteUpdate,
    TranslateAllRequest,
    TranslateSegmentRequest,
    TranslationEdit,
    TranslationProfileUpdate,
    TranslationRestore,
)
from ..services import translation as service
from ..services import translation_engine as engine
from ..services.media import AppError

router = APIRouter(tags=["translations"])


def _video(db: Session, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Video not found"})
    return video


def _project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    return project


def _segment_in_latest(db: Session, segment_id: str) -> tuple[TranscriptSegment, Video]:
    segment = db.get(TranscriptSegment, segment_id)
    if not segment:
        raise AppError(404, "SEGMENT_NOT_FOUND", "Dialogue segment not found.")
    video = db.get(Video, segment.transcript.video_id)
    return segment, video


def _row(db: Session, segment_id: str, language: str) -> tuple[SegmentTranslation, TranscriptSegment]:
    if language not in service.LANGUAGES:
        raise AppError(422, "UNSUPPORTED_LANGUAGE", "Language must be 'ur' or 'hi'.")
    segment, _video_obj = _segment_in_latest(db, segment_id)
    row = db.scalar(
        select(SegmentTranslation).where(
            SegmentTranslation.segment_id == segment_id, SegmentTranslation.language == language
        )
    )
    if row is None:
        # Allow typing a translation by hand before any AI pass.
        row = SegmentTranslation(
            project_id=_video_obj.project_id,
            video_id=_video_obj.id,
            segment_id=segment.id,
            language=language,
            text="",
            origin="manual",
            status="empty",
            source_text=segment.text,
            history_json=[],
            revision=0,
        )
        db.add(row)
        db.flush()
    return row, segment


def _profiles_payload(db: Session, project_id: str) -> list[dict]:
    characters = list(
        db.scalars(select(Character).where(Character.project_id == project_id).order_by(Character.created_at))
    )
    profiles = {
        item.character_id: item
        for item in db.scalars(
            select(CharacterTranslationProfile).where(CharacterTranslationProfile.project_id == project_id)
        )
    }
    out = []
    for character in characters:
        profile = profiles.get(character.id)
        out.append(
            {
                "character_id": character.id,
                "name": character.name,
                "status": character.status,
                "gender": profile.gender if profile else "unknown",
                "register": profile.register if profile else "auto",
                "speaking_style": profile.speaking_style if profile else None,
                "name_ur": profile.name_ur if profile else None,
                "name_hi": profile.name_hi if profile else None,
                "name_roman": profile.name_roman if profile else None,
                "names_auto": bool(profile.names_auto) if profile else False,
            }
        )
    return out


def _relationships_payload(db: Session, project_id: str) -> list[dict]:
    return [
        {
            "id": item.id,
            "speaker_character_id": item.speaker_character_id,
            "listener_character_id": item.listener_character_id,
            "address_form": item.address_form,
            "relation": item.relation,
        }
        for item in db.scalars(
            select(CharacterRelationship).where(CharacterRelationship.project_id == project_id)
        )
    ]


def _glossary_payload(db: Session, project_id: str) -> list[dict]:
    return [
        {
            "id": item.id,
            "source": item.source,
            "ur": item.ur,
            "hi": item.hi,
            "roman": item.roman,
            "case_sensitive": item.case_sensitive,
            "notes": item.notes,
        }
        for item in db.scalars(
            select(TranslationGlossaryTerm)
            .where(TranslationGlossaryTerm.project_id == project_id)
            .order_by(TranslationGlossaryTerm.source)
        )
    ]


def _bundle(db: Session, video: Video) -> dict:
    transcript = service.latest_transcript(db, video.id)
    latest_job = db.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.video_id == video.id, ProcessingJob.type == "translation")
        .order_by(ProcessingJob.created_at.desc())
    )
    base = {
        "video_id": video.id,
        "project_id": video.project_id,
        "transcript_id": transcript.id if transcript else None,
        "transcript_revision": transcript.revision if transcript else None,
        "engine": engine.engine_info(),
        "job": JobOut.model_validate(latest_job).model_dump(mode="json") if latest_job else None,
        "profiles": _profiles_payload(db, video.project_id),
        "relationships": _relationships_payload(db, video.project_id),
        "glossary": _glossary_payload(db, video.project_id),
        "scene_notes": {},
        "lines": [],
        "summary": {},
    }
    if transcript is None:
        return base
    ctx = service.build_context(db, video, transcript)
    rows = service.load_rows(db, [line.segment.id for line in ctx.lines])
    lines = [service.serialize_line(line, rows.get(line.segment.id, {})) for line in ctx.lines]
    summary = {}
    for language in service.LANGUAGES:
        items = [line["translations"][language] for line in lines]
        summary[language] = {
            "total": len(items),
            "translated": sum(1 for item in items if item and item["text"].strip()),
            "manual": sum(1 for item in items if item and item["origin"] == "manual"),
            "stale": sum(1 for item in items if item and item["stale"]),
            "flagged": sum(
                1
                for item in items
                if item and any(flag["severity"] in {"error", "warning"} for flag in item["qc_flags"])
            ),
            "timing_long": sum(1 for item in items if item and (item.get("fit") or {}).get("status") == "long"),
        }
    base.update({"lines": lines, "summary": summary, "scene_notes": ctx.scene_notes})
    return base


def _line_payload(db: Session, segment_id: str) -> dict:
    segment, video = _segment_in_latest(db, segment_id)
    transcript = service.latest_transcript(db, video.id)
    ctx = service.build_context(db, video, transcript)
    if segment_id not in ctx.by_id:
        raise AppError(409, "SEGMENT_NOT_CURRENT", "This segment is not part of the latest transcript.")
    rows = service.load_rows(db, [segment_id])
    return service.serialize_line(ctx.lines[ctx.by_id[segment_id]], rows.get(segment_id, {}))


@router.get("/translation/engine")
def translation_engine_info() -> dict:
    return engine.engine_info()


@router.get("/videos/{video_id}/translations")
def get_translations(video_id: str, db: Session = Depends(get_db)) -> dict:
    return _bundle(db, _video(db, video_id))


@router.post("/videos/{video_id}/translations/translate", response_model=JobOut, status_code=202)
def translate_all(
    video_id: str,
    payload: TranslateAllRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> JobOut:
    video = _video(db, video_id)
    transcript = service.latest_transcript(db, video.id)
    if transcript is None or not transcript.segments:
        raise AppError(404, "TRANSCRIPT_NOT_FOUND", "Transcribe this video before translating it.")
    options = {
        "languages": list(dict.fromkeys(payload.languages)),
        "overwrite_manual": payload.overwrite_manual,
        "only_missing": payload.only_missing,
        "segment_ids": payload.segment_ids or None,
    }
    job = service.create_translation_job(db, video, options)
    job_queue.submit(background, job)
    return JobOut.model_validate(job)


@router.post("/videos/{video_id}/translations/segments/{segment_id}/translate")
def translate_one(
    video_id: str,
    segment_id: str,
    payload: TranslateSegmentRequest,
    db: Session = Depends(get_db),
) -> dict:
    video = _video(db, video_id)
    try:
        service.translate_segments(
            db,
            video,
            [segment_id],
            list(dict.fromkeys(payload.languages)),
            overwrite_manual=payload.force,
        )
    except engine.TranslationUnavailable as error:
        raise AppError(503, "TRANSLATION_ENGINE_UNAVAILABLE", str(error)) from error
    return _line_payload(db, segment_id)


@router.patch("/translations/{segment_id}/{language}")
def edit_translation(
    segment_id: str,
    language: str,
    payload: TranslationEdit,
    db: Session = Depends(get_db),
) -> dict:
    row, segment = _row(db, segment_id, language)
    if payload.roman_text is not None and language != "ur":
        raise AppError(422, "ROMAN_URDU_ONLY", "Roman text is only stored for Urdu.")
    service.save_manual(db, row, segment, payload.text, payload.roman_text, payload.base_revision)
    return _line_payload(db, segment_id)


@router.post("/translations/{segment_id}/{language}/restore")
def restore_translation(
    segment_id: str,
    language: str,
    payload: TranslationRestore,
    db: Session = Depends(get_db),
) -> dict:
    row, segment = _row(db, segment_id, language)
    service.restore_version(db, row, segment, payload.target, payload.index)
    return _line_payload(db, segment_id)


@router.post("/translations/{segment_id}/ur/romanize")
def romanize_translation(segment_id: str, db: Session = Depends(get_db)) -> dict:
    row, _segment = _row(db, segment_id, "ur")
    service.romanize_row(db, row)
    return _line_payload(db, segment_id)


@router.get("/projects/{project_id}/translation-profiles")
def list_profiles(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    _project(db, project_id)
    return _profiles_payload(db, project_id)


@router.put("/characters/{character_id}/translation-profile")
def update_profile(character_id: str, payload: TranslationProfileUpdate, db: Session = Depends(get_db)) -> dict:
    character = db.get(Character, character_id)
    if not character:
        raise AppError(404, "CHARACTER_NOT_FOUND", "Character not found.")
    profile = service.get_or_create_profile(db, character)
    names_changed = any(
        getattr(profile, attr) != getattr(payload, attr) for attr in ("name_ur", "name_hi", "name_roman")
    )
    profile.register = payload.speech_register
    for attr in ("gender", "speaking_style", "name_ur", "name_hi", "name_roman"):
        value = getattr(payload, attr)
        if isinstance(value, str):
            value = value.strip() or None
        setattr(profile, attr, value)
    if names_changed:
        profile.names_auto = False
    db.commit()
    return next(item for item in _profiles_payload(db, character.project_id) if item["character_id"] == character.id)


@router.get("/projects/{project_id}/character-relationships")
def list_relationships(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    _project(db, project_id)
    return _relationships_payload(db, project_id)


@router.put("/projects/{project_id}/character-relationships")
def replace_relationships(project_id: str, payload: RelationshipsUpdate, db: Session = Depends(get_db)) -> list[dict]:
    _project(db, project_id)
    ids = {item.id for item in db.scalars(select(Character).where(Character.project_id == project_id))}
    seen: set[tuple[str, str]] = set()
    for item in payload.relationships:
        if item.speaker_character_id not in ids or item.listener_character_id not in ids:
            raise AppError(422, "CHARACTER_NOT_IN_PROJECT", "Relationships must reference characters in this project.")
        if item.speaker_character_id == item.listener_character_id:
            raise AppError(422, "SELF_RELATIONSHIP", "A character cannot address themselves.")
        key = (item.speaker_character_id, item.listener_character_id)
        if key in seen:
            raise AppError(422, "DUPLICATE_RELATIONSHIP", "Each speaker → listener pair may appear once.")
        seen.add(key)
    db.execute(delete(CharacterRelationship).where(CharacterRelationship.project_id == project_id))
    for item in payload.relationships:
        db.add(
            CharacterRelationship(
                project_id=project_id,
                speaker_character_id=item.speaker_character_id,
                listener_character_id=item.listener_character_id,
                address_form=item.address_form,
                relation=(item.relation or "").strip() or None,
            )
        )
    db.commit()
    return _relationships_payload(db, project_id)


@router.get("/projects/{project_id}/translation-glossary")
def list_glossary(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    _project(db, project_id)
    return _glossary_payload(db, project_id)


@router.put("/projects/{project_id}/translation-glossary")
def replace_glossary(project_id: str, payload: GlossaryUpdate, db: Session = Depends(get_db)) -> list[dict]:
    _project(db, project_id)
    seen: set[str] = set()
    for item in payload.terms:
        key = item.source.strip().lower()
        if key in seen:
            raise AppError(422, "DUPLICATE_GLOSSARY_TERM", f"“{item.source}” appears more than once.")
        seen.add(key)
    db.execute(delete(TranslationGlossaryTerm).where(TranslationGlossaryTerm.project_id == project_id))
    for item in payload.terms:
        db.add(
            TranslationGlossaryTerm(
                project_id=project_id,
                source=item.source.strip(),
                ur=(item.ur or "").strip() or None,
                hi=(item.hi or "").strip() or None,
                roman=(item.roman or "").strip() or None,
                case_sensitive=item.case_sensitive,
                notes=(item.notes or "").strip() or None,
            )
        )
    db.commit()
    return _glossary_payload(db, project_id)


@router.put("/videos/{video_id}/translation-scenes/{segment_id}")
def set_scene_note(video_id: str, segment_id: str, payload: SceneNoteUpdate, db: Session = Depends(get_db)) -> dict:
    video = _video(db, video_id)
    segment, segment_video = _segment_in_latest(db, segment_id)
    if segment_video.id != video.id:
        raise AppError(404, "SEGMENT_NOT_FOUND", "Segment does not belong to this video.")
    note = db.scalar(select(TranslationSceneNote).where(TranslationSceneNote.segment_id == segment_id))
    text = payload.note.strip()
    if not text:
        if note:
            db.delete(note)
    elif note:
        note.note = text
    else:
        db.add(TranslationSceneNote(video_id=video.id, segment_id=segment.id, note=text))
    db.commit()
    return {"segment_id": segment_id, "note": text}
