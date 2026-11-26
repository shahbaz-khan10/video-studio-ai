"""Phase 9 service: versioned character voice profiles, consent and samples."""

import hashlib
import json
import re
import tempfile
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    Character,
    CharacterVoiceProfile,
    Project,
    VoicePreset,
    VoiceReference,
    new_id,
    utcnow,
)
from ..schemas import (
    CharacterVoiceProfileOut,
    VoiceCharacteristics,
    VoiceConsentUpdate,
    VoiceEngineInfoOut,
    VoiceEngineParams,
    VoicePresetIn,
    VoicePresetOut,
    VoiceProfileBundleOut,
    VoiceProfileUpdate,
    VoiceReferenceOut,
)
from . import audio, voice_engine
from .media import AppError
from .storage import storage

CONSENT_STATEMENT = (
    "I confirm that I own this voice sample or have the speaker's explicit permission "
    "to use it, and I authorise its use only inside this project."
)

CHARACTERISTIC_CONTROLS = {
    "gender": "gender",
    "age": "age",
    "pitch": "pitch",
    "tone": "tone",
    "speed": "speed",
    "accent": "accent",
    "intensity": "intensity",
    "emotion": "emotion",
}
PARAM_CONTROLS = {
    "length_scale": "speed",
    "noise_scale": "variability",
    "noise_w_scale": "cadence",
    "speaker_id": "speaker_id",
    "volume": "volume",
    "seed": "seed",
}
ALLOWED_AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".aac", ".m4a", ".ogg", ".oga", ".webm"}


def _default_engine() -> str:
    selected = voice_engine.effective_backend()
    if selected not in ("none", "coqui"):
        return selected
    return "piper"


def _supported(engine: str, voice_id: str | None) -> set[str]:
    try:
        return voice_engine.capabilities(engine, voice_id)
    except Exception:
        return set(voice_engine.ENGINES.get(engine, voice_engine.ENGINES["none"])["controls"])


def _filter_characteristics(
    engine: str,
    voice_id: str | None,
    submitted: VoiceCharacteristics,
) -> tuple[dict, list[str]]:
    supported = _supported(engine, voice_id)
    defaults = VoiceCharacteristics().model_dump()
    result = dict(defaults)
    ignored: list[str] = []
    for key, value in submitted.model_dump().items():
        control = CHARACTERISTIC_CONTROLS.get(key, key)
        if control in supported:
            result[key] = value
        elif value != defaults[key]:
            ignored.append(key)
    return result, ignored


def _filter_params(engine: str, submitted: VoiceEngineParams) -> tuple[dict, list[str]]:
    supported = _supported(engine, None)
    result: dict = {}
    ignored: list[str] = []
    for key, value in submitted.model_dump().items():
        if value is None:
            continue
        if PARAM_CONTROLS.get(key, key) in supported:
            result[key] = value
        else:
            ignored.append(key)
    return result, ignored


def _signature(row: CharacterVoiceProfile | None, values: dict) -> str:
    return json.dumps(values, sort_keys=True, default=str)


def _values_from_payload(
    payload: VoiceProfileUpdate,
    engine: str,
    voice_id: str | None,
    model_id: str | None,
    reference_id: str | None,
    consent_status: str,
) -> dict:
    characteristics, _ = _filter_characteristics(engine, voice_id, payload.characteristics)
    params, _ = _filter_params(engine, payload.params)
    return {
        "engine": engine,
        "voice_id": voice_id,
        "model_id": model_id,
        "characteristics": characteristics,
        "params": params,
        "reference_id": reference_id,
        "consent_status": consent_status,
        "notes": payload.notes,
    }


def active_profile(db: Session, character_id: str, language: str) -> CharacterVoiceProfile | None:
    return db.scalar(
        select(CharacterVoiceProfile)
        .where(
            CharacterVoiceProfile.character_id == character_id,
            CharacterVoiceProfile.language == language,
            CharacterVoiceProfile.active.is_(True),
        )
        .order_by(CharacterVoiceProfile.version.desc(), CharacterVoiceProfile.created_at.desc())
    )


def profile_history(db: Session, character_id: str, language: str | None = None) -> list[CharacterVoiceProfile]:
    statement = select(CharacterVoiceProfile).where(CharacterVoiceProfile.character_id == character_id)
    if language:
        statement = statement.where(CharacterVoiceProfile.language == language)
    return list(db.scalars(statement.order_by(CharacterVoiceProfile.created_at, CharacterVoiceProfile.version)))


def _voice_label(engine: str, voice_id: str | None) -> str | None:
    option = voice_engine.find_voice(engine, voice_id, "ur") or voice_engine.find_voice(engine, voice_id, "hi")
    return option["label"] if option else voice_id


def usable(row: CharacterVoiceProfile) -> bool:
    if row.consent_status == "revoked":
        return False
    if row.reference_id and not row.reference:
        return False
    if row.reference and row.reference.consent_status in ("revoked", "none"):
        return False
    return True


def profile_out(db: Session, row: CharacterVoiceProfile) -> CharacterVoiceProfileOut:
    supported = _supported(row.engine, row.voice_id)
    characteristics = VoiceCharacteristics.model_validate(row.characteristics_json or {})
    params = VoiceEngineParams.model_validate(row.engine_params_json or {})
    ignored = [
        key
        for key, control in CHARACTERISTIC_CONTROLS.items()
        if control not in supported and getattr(characteristics, key) != getattr(VoiceCharacteristics(), key)
    ]
    ignored += [key for key, control in PARAM_CONTROLS.items() if control not in supported and getattr(params, key) is not None]
    option = None
    if row.voice_id:
        for language in ("ur", "hi", "en"):
            option = voice_engine.find_voice(row.engine, row.voice_id, language)
            if option:
                break
    meta = voice_engine.ENGINES.get(row.engine, voice_engine.ENGINES["none"])
    return CharacterVoiceProfileOut(
        id=row.id,
        project_id=row.project_id,
        character_id=row.character_id,
        language=row.language,
        version=row.version,
        active=bool(row.active),
        locked=bool(row.locked),
        engine=row.engine,
        engine_label=meta["label"],
        model_id=row.model_id,
        voice_id=row.voice_id,
        voice_label=option["label"] if option else row.voice_id,
        license=option["license"] if option else meta["license"],
        commercial_use=option.get("commercial_use") if option else meta["commercial_use"],
        characteristics=characteristics,
        params=params,
        supported_controls=sorted(supported),
        ignored_controls=sorted(set(ignored)),
        reference_id=row.reference_id,
        reference_url=f"/api/v1/voice-references/{row.reference_id}" if row.reference_id else None,
        consent_status=row.consent_status,
        consent_at=row.consent_at,
        revoked_at=row.revoked_at,
        usable=usable(row),
        preset_id=row.preset_id,
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def reference_out(reference: VoiceReference) -> VoiceReferenceOut:
    return VoiceReferenceOut(
        id=reference.id,
        project_id=reference.project_id,
        character_id=reference.character_id,
        language=reference.language,
        filename=reference.filename,
        url=f"/api/v1/voice-references/{reference.id}",
        duration_ms=reference.duration_ms,
        sample_rate=reference.sample_rate,
        channels=reference.channels,
        rms=reference.rms,
        peak=reference.peak,
        consent_status=reference.consent_status,
        consent_text_version=reference.consent_text_version,
        consent_at=reference.consent_at,
        revoked_at=reference.revoked_at,
        created_at=reference.created_at,
    )


def _ensure_character(db: Session, project: Project, character_id: str) -> Character:
    character = db.get(Character, character_id)
    if not character or character.project_id != project.id:
        raise AppError(404, "CHARACTER_NOT_FOUND", "Character not found in this project.")
    return character


def _check_lock(row: CharacterVoiceProfile | None, payload: VoiceProfileUpdate) -> None:
    if row and row.locked and payload.locked is not False:
        raise AppError(
            423,
            "VOICE_PROFILE_LOCKED",
            "Unlock this profile explicitly (locked=false) before changing its voice or settings.",
        )


def save_profile(
    db: Session,
    character: Character,
    payload: VoiceProfileUpdate,
    force_fields: set[str] | None = None,
) -> CharacterVoiceProfile:
    current = active_profile(db, character.id, payload.language)
    _check_lock(current, payload)
    fields = force_fields if force_fields is not None else set(payload.model_fields_set)

    engine = payload.engine if "engine" in fields and payload.engine else (current.engine if current else _default_engine())
    voice_id = payload.voice_id if "voice_id" in fields else (current.voice_id if current else None)
    model_id = payload.model_id if "model_id" in fields else (current.model_id if current else None)
    reference_id = payload.reference_id if "reference_id" in fields else (current.reference_id if current else None)
    consent_status = current.consent_status if current else "none"

    if "preset_id" in fields and payload.preset_id:
        preset = db.get(VoicePreset, payload.preset_id)
        if not preset or preset.project_id != character.project_id:
            raise AppError(404, "VOICE_PRESET_NOT_FOUND", "Voice preset not found in this project.")
        if preset.language != payload.language:
            raise AppError(422, "VOICE_PRESET_LANGUAGE", "Preset language does not match this profile.")
        if not (current and current.locked):
            engine = preset.engine
            voice_id = preset.voice_id
            model_id = preset.model_id
            payload = payload.model_copy(
                update={
                    "characteristics": VoiceCharacteristics.model_validate(preset.characteristics_json or {}),
                    "params": VoiceEngineParams.model_validate(preset.engine_params_json or {}),
                }
            )

    if reference_id:
        reference = db.get(VoiceReference, reference_id)
        if not reference or reference.character_id != character.id or reference.language != payload.language:
            raise AppError(404, "VOICE_REFERENCE_NOT_FOUND", "Voice sample not found for this character and language.")
        if reference.consent_status == "revoked":
            raise AppError(423, "VOICE_CONSENT_REVOKED", "This voice sample's permission was revoked.")
        consent_status = reference.consent_status

    values = _values_from_payload(payload, engine, voice_id, model_id, reference_id, consent_status)
    values["preset_id"] = payload.preset_id if "preset_id" in fields else (current.preset_id if current else None)
    signature = _signature(None, values)

    if current:
        current_values = {
            "engine": current.engine,
            "voice_id": current.voice_id,
            "model_id": current.model_id,
            "characteristics": current.characteristics_json or {},
            "params": current.engine_params_json or {},
            "reference_id": current.reference_id,
            "consent_status": current.consent_status,
            "notes": current.notes,
            "preset_id": current.preset_id,
        }
        if _signature(None, current_values) == signature:
            if "locked" in fields and payload.locked is not None:
                current.locked = bool(payload.locked)
            current.notes = payload.notes if "notes" in fields else current.notes
            current.updated_at = utcnow()
            db.flush()
            return current
        current.active = False
        version = current.version + 1
    else:
        version = 1

    row = CharacterVoiceProfile(
        id=new_id(),
        project_id=character.project_id,
        character_id=character.id,
        language=payload.language,
        version=version,
        active=True,
        locked=bool(payload.locked) if "locked" in fields and payload.locked is not None else False,
        engine=values["engine"],
        model_id=values["model_id"],
        voice_id=values["voice_id"],
        engine_params_json=values["params"],
        characteristics_json=values["characteristics"],
        reference_id=values["reference_id"],
        consent_status=values["consent_status"],
        preset_id=values["preset_id"],
        notes=values["notes"],
        consent_at=current.consent_at if current else None,
        revoked_at=None,
    )
    db.add(row)
    db.flush()
    for other in db.scalars(
        select(CharacterVoiceProfile).where(
            CharacterVoiceProfile.character_id == character.id,
            CharacterVoiceProfile.language == payload.language,
            CharacterVoiceProfile.id != row.id,
            CharacterVoiceProfile.active.is_(True),
        )
    ):
        other.active = False
    db.flush()
    return row


def activate_profile(db: Session, row: CharacterVoiceProfile) -> CharacterVoiceProfile:
    if not row.character_id:
        raise AppError(422, "VOICE_PROFILE_ORPHAN", "This profile is no longer linked to a character.")
    character = db.get(Character, row.character_id)
    if character and character.project_id != row.project_id:
        raise AppError(422, "VOICE_PROFILE_CONFLICT", "Profile and character projects differ.")
    if row.consent_status == "revoked":
        raise AppError(423, "VOICE_CONSENT_REVOKED", "A revoked voice profile cannot be activated.")
    for other in db.scalars(
        select(CharacterVoiceProfile).where(
            CharacterVoiceProfile.character_id == row.character_id,
            CharacterVoiceProfile.language == row.language,
            CharacterVoiceProfile.active.is_(True),
        )
    ):
        other.active = False
    row.active = True
    row.updated_at = utcnow()
    db.flush()
    return row


def reset_profile(db: Session, character: Character, language: str) -> CharacterVoiceProfile:
    current = active_profile(db, character.id, language)
    if current:
        current.active = False
    row = CharacterVoiceProfile(
        id=new_id(),
        project_id=character.project_id,
        character_id=character.id,
        language=language,
        version=(current.version + 1) if current else 1,
        active=True,
        engine=_default_engine(),
        engine_params_json={},
        characteristics_json=VoiceCharacteristics().model_dump(),
        consent_status="none",
    )
    db.add(row)
    db.flush()
    return row


def list_presets(db: Session, project_id: str) -> list[VoicePreset]:
    return list(
        db.scalars(
            select(VoicePreset)
            .where(VoicePreset.project_id == project_id)
            .order_by(VoicePreset.name, VoicePreset.language)
        )
    )


def preset_out(row: VoicePreset) -> VoicePresetOut:
    return VoicePresetOut(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        language=row.language,
        engine=row.engine,
        model_id=row.model_id,
        voice_id=row.voice_id,
        characteristics=VoiceCharacteristics.model_validate(row.characteristics_json or {}),
        params=VoiceEngineParams.model_validate(row.engine_params_json or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def save_preset(db: Session, project: Project, payload: VoicePresetIn, preset_id: str | None = None) -> VoicePreset:
    row = db.get(VoicePreset, preset_id) if preset_id else None
    if preset_id and (not row or row.project_id != project.id):
        raise AppError(404, "VOICE_PRESET_NOT_FOUND", "Voice preset not found in this project.")
    duplicate = db.scalar(
        select(VoicePreset).where(
            VoicePreset.project_id == project.id,
            VoicePreset.name == payload.name,
            VoicePreset.language == payload.language,
        )
    )
    if duplicate and (not row or duplicate.id != row.id):
        raise AppError(409, "VOICE_PRESET_EXISTS", "A preset with this name and language already exists.")
    if not row:
        row = VoicePreset(id=new_id(), project_id=project.id, name=payload.name, language=payload.language)
        db.add(row)
    row.name = payload.name
    row.language = payload.language
    row.engine = payload.engine
    row.model_id = payload.model_id
    row.voice_id = payload.voice_id
    row.engine_params_json = payload.params.model_dump(exclude_none=True)
    row.characteristics_json = payload.characteristics.model_dump()
    db.flush()
    return row


def _safe_sample_name(filename: str) -> str:
    safe = "".join(ch for ch in (filename or "sample.wav").lower() if ch.isalnum() or ch in ".-_")
    return safe[-80:] or "sample.wav"


def upload_reference(
    db: Session,
    project: Project,
    character: Character,
    language: str,
    content: bytes,
    filename: str,
    content_type: str | None,
    confirm_ownership: bool,
    policy_version: str,
) -> tuple[VoiceReference, CharacterVoiceProfile]:
    if not confirm_ownership or policy_version != settings.voice_consent_policy_version:
        raise AppError(
            423,
            "VOICE_CONSENT_REQUIRED",
            f"Confirm ownership/permission and accept the current policy ({CONSENT_STATEMENT}).",
        )
    if not content:
        raise AppError(422, "VOICE_REFERENCE_EMPTY", "The voice sample is empty.")
    if len(content) > settings.voice_reference_max_bytes:
        raise AppError(
            413,
            "VOICE_REFERENCE_TOO_LARGE",
            f"Voice samples must be smaller than {settings.voice_reference_max_mb} MB.",
        )
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_AUDIO_SUFFIXES and not (content_type or "").lower().startswith("audio/"):
        raise AppError(422, "VOICE_REFERENCE_FORMAT", "Upload a common audio file (wav, mp3, flac, aac, m4a, ogg, webm).")

    with tempfile.TemporaryDirectory(prefix="voice-sample-") as directory:
        temp = Path(directory) / f"sample{suffix or '.wav'}"
        temp.write_bytes(content)
        try:
            converted = audio.ensure_pcm_wav(temp, temp.parent)
        except AppError as error:
            raise AppError(422, "VOICE_REFERENCE_INVALID", f"Could not decode this audio sample: {error.message}") from error
        metrics = audio.validate_audio_file(converted, allow_silent=False)
        if not metrics["ok"]:
            raise AppError(422, "VOICE_REFERENCE_INVALID", "Invalid audio: " + ", ".join(metrics["issues"]))
        duration_s = (metrics["duration_ms"] or 0) / 1000
        if duration_s < settings.voice_reference_min_s or duration_s > settings.voice_reference_max_s:
            raise AppError(
                422,
                "VOICE_REFERENCE_DURATION",
                f"Voice samples must be between {settings.voice_reference_min_s:g} and "
                f"{settings.voice_reference_max_s:g} seconds long.",
            )
        if (metrics["sample_rate"] or 0) < settings.voice_reference_min_sample_rate:
            raise AppError(
                422,
                "VOICE_REFERENCE_RATE",
                f"Voice samples need at least {settings.voice_reference_min_sample_rate} Hz.",
            )
        if (metrics["channels"] or 0) > 2:
            raise AppError(422, "VOICE_REFERENCE_CHANNELS", "Voice samples must be mono or stereo.")
        version = int(
            db.scalar(
                select(func.count()).select_from(VoiceReference).where(
                    VoiceReference.project_id == project.id,
                    VoiceReference.character_id == character.id,
                    VoiceReference.language == language,
                )
            )
            or 0
        ) + 1
        storage_key = (
            f"projects/{project.id}/voice/{character.id}/{language}/v{version}/"
            f"{_safe_sample_name(filename)}.wav"
        )
        checksum = hashlib.sha256(converted.read_bytes()).hexdigest()
        stored_size = converted.stat().st_size
        storage.save(storage_key, converted)

    reference = VoiceReference(
        id=new_id(),
        project_id=project.id,
        character_id=character.id,
        language=language,
        storage_key=storage_key,
        filename=_safe_sample_name(filename),
        mime_type="audio/wav",
        size_bytes=stored_size,
        checksum_sha256=checksum,
        duration_ms=metrics["duration_ms"],
        sample_rate=metrics["sample_rate"],
        channels=metrics["channels"],
        rms=metrics["rms"],
        peak=metrics["peak"],
        consent_status="granted",
        consent_text_version=policy_version,
        consent_at=utcnow(),
    )
    db.add(reference)
    db.flush()

    current = active_profile(db, character.id, language)
    new_profile = current is None
    if current and current.locked:
        raise AppError(423, "VOICE_PROFILE_LOCKED", "Unlock the profile before replacing its voice sample.")
    payload = VoiceProfileUpdate(
        language=language,
        reference_id=reference.id,
        engine=current.engine if current else _default_engine(),
        voice_id=current.voice_id if current else None,
        model_id=current.model_id if current else None,
        characteristics=VoiceCharacteristics.model_validate(current.characteristics_json or {}) if current else VoiceCharacteristics(),
        params=VoiceEngineParams.model_validate(current.engine_params_json or {}) if current else VoiceEngineParams(),
    )
    forced = {"language", "reference_id", "engine", "voice_id", "model_id", "characteristics", "params"}
    profile = save_profile(db, character, payload, force_fields=forced)
    if new_profile and profile.consent_status != "granted":
        profile.consent_status = "granted"
        profile.consent_at = reference.consent_at
        db.flush()
    return reference, profile


def record_consent(
    db: Session,
    character: Character,
    language: str,
    payload: VoiceConsentUpdate,
) -> CharacterVoiceProfile:
    if payload.status == "granted" and (not payload.confirm_ownership or payload.policy_version != settings.voice_consent_policy_version):
        raise AppError(423, "VOICE_CONSENT_REQUIRED", CONSENT_STATEMENT)
    now = utcnow()
    for reference in db.scalars(
        select(VoiceReference).where(
            VoiceReference.character_id == character.id,
            VoiceReference.language == language,
        )
    ):
        reference.consent_status = payload.status
        reference.consent_text_version = payload.policy_version
        if payload.status == "granted":
            reference.consent_at = now
            reference.revoked_at = None
        else:
            reference.revoked_at = now

    current = active_profile(db, character.id, language)
    if payload.status == "revoked":
        if current:
            current.active = False
        row = CharacterVoiceProfile(
            id=new_id(),
            project_id=character.project_id,
            character_id=character.id,
            language=language,
            version=(current.version + 1) if current else 1,
            active=True,
            engine=current.engine if current else _default_engine(),
            voice_id=current.voice_id if current else None,
            model_id=current.model_id if current else None,
            engine_params_json=dict(current.engine_params_json or {}) if current else {},
            characteristics_json=dict(current.characteristics_json or {}) if current else VoiceCharacteristics().model_dump(),
            consent_status="revoked",
            consent_at=current.consent_at if current else None,
            revoked_at=now,
        )
        db.add(row)
        db.flush()
        return row
    if not current:
        return reset_profile(db, character, language)
    current.consent_status = "granted"
    current.consent_at = now
    current.revoked_at = None
    db.flush()
    return current


def delete_reference(db: Session, reference: VoiceReference) -> None:
    for profile in db.scalars(select(CharacterVoiceProfile).where(CharacterVoiceProfile.reference_id == reference.id)):
        profile.reference_id = None
        if profile.consent_status == "granted":
            profile.consent_status = "none"
    path = storage.absolute_path(reference.storage_key)
    db.delete(reference)
    db.flush()
    path.unlink(missing_ok=True)


def reference_bytes(reference: VoiceReference) -> tuple[bytes, str]:
    path = storage.absolute_path(reference.storage_key)
    if not path.is_file():
        raise AppError(404, "VOICE_REFERENCE_MISSING", "The voice sample file is missing.")
    return path.read_bytes(), "audio/wav"


def project_references(db: Session, project_id: str) -> list[VoiceReference]:
    return list(
        db.scalars(
            select(VoiceReference)
            .where(VoiceReference.project_id == project_id)
            .order_by(VoiceReference.created_at)
        )
    )


def project_profiles(db: Session, project_id: str) -> list[CharacterVoiceProfile]:
    return list(
        db.scalars(
            select(CharacterVoiceProfile)
            .where(CharacterVoiceProfile.project_id == project_id)
            .order_by(CharacterVoiceProfile.created_at)
        )
    )


def bundle(db: Session, project: Project) -> VoiceProfileBundleOut:
    engine = voice_engine.engine_info()
    profiles = project_profiles(db, project.id)
    references = project_references(db, project.id)
    return VoiceProfileBundleOut(
        project_id=project.id,
        engine=VoiceEngineInfoOut.model_validate(engine),
        profiles=[profile_out(db, row) for row in profiles],
        presets=[preset_out(row) for row in list_presets(db, project.id)],
        references=[reference_out(row) for row in references],
        consent_policy={
            "version": settings.voice_consent_policy_version,
            "statement": CONSENT_STATEMENT,
            "required": True,
        },
        sample_limits={
            "max_mb": settings.voice_reference_max_mb,
            "min_s": settings.voice_reference_min_s,
            "max_s": settings.voice_reference_max_s,
            "min_sample_rate": settings.voice_reference_min_sample_rate,
            "formats": sorted(ALLOWED_AUDIO_SUFFIXES),
        },
        limitations=[
            "Voice profiles are metadata and consent records; Phase 9 does not generate dubbed dialogue.",
            "Gender, age, pitch, tone, accent, intensity and emotion are only editable when the selected engine reports support.",
            "Custom samples are never used without explicit ownership/permission confirmation; revoking consent disables future use.",
            "Piper voice licences are per voice and must be reviewed via each MODEL_CARD before use.",
        ],
    )


def character_voice_payload(db: Session, project_id: str) -> list[dict]:
    """Compact active-profile list embedded in the Phase 7 character bundle."""
    characters = list(
        db.scalars(select(Character).where(Character.project_id == project_id).order_by(Character.created_at))
    )
    rows = project_profiles(db, project_id)
    active = {(row.character_id, row.language): row for row in rows if row.active}
    return [
        {
            "character_id": character.id,
            "name": character.name,
            "profiles": {
                language: profile_out(db, active[(character.id, language)]).model_dump(mode="json")
                for language in ("ur", "hi")
                if (character.id, language) in active
            },
        }
        for character in characters
    ]


def on_merge(db: Session, target: Character, source_ids: list[str]) -> None:
    if not source_ids:
        return
    for reference in db.scalars(
        select(VoiceReference).where(VoiceReference.character_id.in_(source_ids))
    ):
        reference.character_id = target.id
    for row in db.scalars(
        select(CharacterVoiceProfile).where(CharacterVoiceProfile.character_id.in_(source_ids))
    ):
        row.character_id = target.id
        row.active = False
    for language in ("ur", "hi"):
        if active_profile(db, target.id, language):
            continue
        candidate = db.scalar(
            select(CharacterVoiceProfile)
            .where(
                CharacterVoiceProfile.character_id == target.id,
                CharacterVoiceProfile.language == language,
                CharacterVoiceProfile.consent_status != "revoked",
            )
            .order_by(CharacterVoiceProfile.version.desc(), CharacterVoiceProfile.created_at.desc())
        )
        if candidate:
            candidate.active = True
    db.flush()


def on_split(db: Session, source: Character, new_character: Character) -> None:
    current = active_profile(db, source.id, "ur") or active_profile(db, source.id, "hi")
    if not current:
        return
    # A split identity starts with no proven voice or consented sample. Only
    # descriptive characteristics are copied as an inactive draft.
    draft = CharacterVoiceProfile(
        id=new_id(),
        project_id=source.project_id,
        character_id=new_character.id,
        language=current.language,
        version=1,
        active=False,
        engine=current.engine,
        model_id=current.model_id,
        voice_id=current.voice_id,
        engine_params_json=dict(current.engine_params_json or {}),
        characteristics_json=dict(current.characteristics_json or {}),
        consent_status="none",
        notes="Descriptive draft copied during character split; review before use.",
    )
    db.add(draft)
    db.flush()
