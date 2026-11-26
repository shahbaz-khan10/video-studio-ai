"""Phase 9 routes for voice profiles, consented samples and real previews."""

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Character, CharacterVoiceProfile, Project, VoicePreset, VoiceReference
from ..schemas import (
    VoiceConsentUpdate,
    VoiceLanguage,
    VoicePresetIn,
    VoiceProfileUpdate,
    VoicePreviewRequest,
)
from ..services import voice_engine, voice_profiles as service
from ..services.media import AppError

router = APIRouter(tags=["voice-profiles"])


def _project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    return project


def _character(db: Session, character_id: str) -> Character:
    character = db.get(Character, character_id)
    if not character:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Character not found"})
    return character


def _profile(db: Session, profile_id: str) -> CharacterVoiceProfile:
    profile = db.get(CharacterVoiceProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Voice profile not found"})
    return profile


@router.get("/voice/engine")
def get_voice_engine() -> dict:
    return voice_engine.engine_info()


@router.get("/projects/{project_id}/voice-profiles")
def get_project_voice_profiles(project_id: str, db: Session = Depends(get_db)):
    return service.bundle(db, _project(db, project_id))


@router.get("/characters/{character_id}/voice-profiles")
def get_character_voice_profiles(
    character_id: str,
    language: VoiceLanguage | None = None,
    db: Session = Depends(get_db),
):
    _character(db, character_id)
    rows = service.profile_history(db, character_id, language)
    return [service.profile_out(db, row) for row in rows]


@router.put("/characters/{character_id}/voice-profile")
def save_character_voice_profile(
    character_id: str,
    payload: VoiceProfileUpdate,
    db: Session = Depends(get_db),
):
    character = _character(db, character_id)
    row = service.save_profile(db, character, payload)
    db.commit()
    return service.profile_out(db, row)


@router.post("/characters/{character_id}/voice-profile/reset")
def reset_character_voice_profile(
    character_id: str,
    language: VoiceLanguage = "ur",
    db: Session = Depends(get_db),
):
    character = _character(db, character_id)
    row = service.reset_profile(db, character, language)
    db.commit()
    return service.profile_out(db, row)


@router.post("/voice-profiles/{profile_id}/activate")
def activate_voice_profile(profile_id: str, db: Session = Depends(get_db)):
    row = service.activate_profile(db, _profile(db, profile_id))
    db.commit()
    return service.profile_out(db, row)


@router.get("/projects/{project_id}/voice-presets")
def list_voice_presets(project_id: str, db: Session = Depends(get_db)):
    _project(db, project_id)
    return [service.preset_out(row) for row in service.list_presets(db, project_id)]


@router.put("/projects/{project_id}/voice-presets")
def save_voice_preset(
    project_id: str,
    payload: VoicePresetIn,
    preset_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    row = service.save_preset(db, _project(db, project_id), payload, preset_id)
    db.commit()
    return service.preset_out(row)


@router.delete("/voice-presets/{preset_id}", status_code=204)
def delete_voice_preset(preset_id: str, db: Session = Depends(get_db)):
    row = db.get(VoicePreset, preset_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Voice preset not found"})
    db.delete(row)
    db.commit()
    return Response(status_code=204)


@router.post("/characters/{character_id}/voice-profile/reference")
async def upload_voice_reference(
    character_id: str,
    file: UploadFile,
    language: VoiceLanguage = Form(default="ur"),
    confirm_ownership: bool = Form(default=False),
    policy_version: str = Form(default="2026-11-26"),
    db: Session = Depends(get_db),
):
    character = _character(db, character_id)
    project = db.get(Project, character.project_id)
    if not project:
        raise AppError(404, "PROJECT_NOT_FOUND", "Project not found.")
    content = await file.read()
    reference, profile = service.upload_reference(
        db,
        project,
        character,
        language,
        content,
        file.filename or "sample.wav",
        file.content_type,
        confirm_ownership,
        policy_version,
    )
    db.commit()
    return {
        "reference": service.reference_out(reference),
        "profile": service.profile_out(db, profile),
    }


@router.get("/voice-references/{reference_id}")
def get_voice_reference(reference_id: str, db: Session = Depends(get_db)):
    reference = db.get(VoiceReference, reference_id)
    if not reference:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Voice sample not found"})
    payload, content_type = service.reference_bytes(reference)
    return Response(
        content=payload,
        media_type=content_type,
        headers={"Content-Disposition": 'inline; filename="voice-sample.wav"'},
    )


@router.delete("/voice-references/{reference_id}", status_code=204)
def delete_voice_reference(reference_id: str, db: Session = Depends(get_db)):
    reference = db.get(VoiceReference, reference_id)
    if not reference:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Voice sample not found"})
    service.delete_reference(db, reference)
    db.commit()
    return Response(status_code=204)


@router.put("/characters/{character_id}/voice-profile/consent")
def update_voice_consent(
    character_id: str,
    payload: VoiceConsentUpdate,
    language: VoiceLanguage = "ur",
    db: Session = Depends(get_db),
):
    character = _character(db, character_id)
    row = service.record_consent(db, character, language, payload)
    db.commit()
    return service.profile_out(db, row)


@router.post("/voice-preview")
def preview_voice(payload: VoicePreviewRequest, db: Session = Depends(get_db)):
    text = payload.text.strip()
    if not text:
        raise AppError(422, "VOICE_TEXT_EMPTY", "Enter a short sentence to preview.")
    profile: CharacterVoiceProfile | None = None
    character: Character | None = None
    supplied = payload.profile
    if payload.profile_id:
        profile = _profile(db, payload.profile_id)
        character = db.get(Character, profile.character_id) if profile.character_id else None
        language = profile.language
        engine = profile.engine
        voice_id = profile.voice_id
        params = dict(profile.engine_params_json or {})
        characteristics = dict(profile.characteristics_json or {})
        reference_id = profile.reference_id
    elif payload.character_id:
        character = _character(db, payload.character_id)
        profile = service.active_profile(db, character.id, payload.language)
        language = payload.language
        engine = profile.engine if profile else _engine_default()
        voice_id = profile.voice_id if profile else None
        params = dict(profile.engine_params_json or {}) if profile else {}
        characteristics = dict(profile.characteristics_json or {}) if profile else {}
        reference_id = profile.reference_id if profile else None
    else:
        language = payload.language
        engine = _engine_default()
        voice_id = supplied.voice_id if supplied else None
        params = supplied.params.model_dump(exclude_none=True) if supplied else {}
        characteristics = supplied.characteristics.model_dump() if supplied else {}
        reference_id = None

    if supplied and (engine != supplied.engine or voice_id != supplied.voice_id or supplied.model_fields_set - {"engine", "voice_id", "model_id"}):
        # A draft preview uses the draft's real settings without persisting it.
        engine = supplied.engine or engine
        voice_id = supplied.voice_id if "voice_id" in supplied.model_fields_set else voice_id
        filtered_chars, _ = service._filter_characteristics(engine, voice_id, supplied.characteristics)
        filtered_params, _ = service._filter_params(engine, supplied.params)
        characteristics = filtered_chars
        params = filtered_params
        language = supplied.language

    if profile and profile.consent_status == "revoked":
        raise AppError(423, "VOICE_CONSENT_REVOKED", "This voice profile's permission was revoked.")

    reference: bytes | None = None
    reference_used = False
    if reference_id:
        reference_row = db.get(VoiceReference, reference_id)
        if reference_row and reference_row.consent_status == "granted":
            meta = voice_engine.ENGINES.get(engine, voice_engine.ENGINES["none"])
            if meta["supports_reference"]:
                reference, _ = service.reference_bytes(reference_row)
                reference_used = True

    params = dict(params)
    params["language"] = language
    wav = voice_engine.synthesise_preview(
        text=text,
        engine=engine,
        voice_id=voice_id,
        language=language,
        params=params,
        characteristics=characteristics,
        reference=reference,
        char_id=character.id if character else None,
    )
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            "Content-Disposition": 'inline; filename="voice-preview.wav"',
            "X-Voice-Engine": engine,
            "X-Voice-Id": voice_id or "",
            "X-Voice-Reference-Used": "true" if reference_used else "false",
        },
    )


def _engine_default() -> str:
    return service._default_engine()


@router.get("/voice/catalog")
def voice_catalog() -> list[dict]:
    """Documented voices; no weights are bundled or downloaded automatically."""
    return voice_engine.vendored_voice_catalog()
