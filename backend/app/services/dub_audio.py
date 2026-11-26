"""Phase 10 — natural voice generation for translated dialogue.

Voice resolution order for each character + language:
1. The active Phase 9 ``CharacterVoiceProfile`` (engine, voice, pitch, speed,
   tone, consented reference). Read defensively: Phase 9 is developed in
   parallel, so missing tables/fields simply fall through.
2. A deterministic default derived from the character id (and the Phase 8
   gender, if set) so the same character always sounds the same.

Every take stores the fully resolved settings and a fingerprint, so a take is
reproducible and the UI can tell when a line's voice or text has changed.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import asdict

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .. import models
from ..database import SessionLocal
from ..models import (
    Character,
    CharacterAssignment,
    ProcessingJob,
    SegmentTranslation,
    Transcript,
    TranscriptSegment,
    Video,
    new_id,
    utcnow,
)
from ..models_tts import DubAudioTake
from . import tts_engine as tts
from .media import AppError
from .storage import storage

ActiveCheck = Callable[[], None]
LANGUAGES = ("ur", "hi")
DUB_STEP_NAMES = ["select_lines", "resolve_voices", "load_engine", "synthesize", "quality_check", "finalize"]
MODES = {"pending", "missing", "failed", "all"}

PREVIEW_TEXT = {
    "ur": "السلام علیکم، یہ میری آواز کا نمونہ ہے۔",
    "hi": "नमस्ते, यह मेरी आवाज़ का नमूना है।",
}

# Deterministic default voices. MMS has one base (male) voice per language, so
# characters are separated by pitch / speed / tone. Female defaults raise the
# pitch with formants shifted; this is synthetic and sounds like it.
DEFAULT_VOICES = {
    "male": [
        {"pitch_semitones": 0.0, "speed": 1.0, "tone": "neutral"},
        {"pitch_semitones": -2.5, "speed": 0.95, "tone": "deep"},
        {"pitch_semitones": 1.5, "speed": 1.05, "tone": "bright"},
        {"pitch_semitones": -1.0, "speed": 0.92, "tone": "warm"},
    ],
    "female": [
        {"pitch_semitones": 5.0, "speed": 1.02, "tone": "bright"},
        {"pitch_semitones": 6.5, "speed": 1.0, "tone": "soft"},
        {"pitch_semitones": 4.0, "speed": 0.96, "tone": "warm"},
        {"pitch_semitones": 7.0, "speed": 1.06, "tone": "neutral"},
    ],
}


def _stable_int(*parts: str) -> int:
    return int(hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8], 16)


def latest_transcript(db: Session, video_id: str) -> Transcript | None:
    return db.scalar(
        select(Transcript)
        .where(Transcript.video_id == video_id)
        .order_by(Transcript.version.desc(), Transcript.created_at.desc())
    )


# ---------------------------------------------------------------------------
# Voice resolution (Phase 9 adapter)
# ---------------------------------------------------------------------------


def _phase9_profile(db: Session, character_id: str | None, language: str):
    model = getattr(models, "CharacterVoiceProfile", None)
    if model is None or not character_id:
        return None
    try:
        query = select(model).where(model.character_id == character_id, model.language == language)
        if hasattr(model, "active"):
            query = query.where(model.active.is_(True))
        if hasattr(model, "version"):
            query = query.order_by(model.version.desc())
        return db.scalars(query).first()
    except OperationalError:
        db.rollback()
        return None


def _profile_usable(profile) -> tuple[bool, str | None]:
    """Phase 9's own rule when available: revoked consent makes a profile unusable."""
    try:
        from . import voice_profiles  # Phase 9

        if hasattr(voice_profiles, "usable") and not voice_profiles.usable(profile):
            return False, "The Phase 9 profile is not usable (its voice sample consent is missing or revoked)."
    except Exception:  # noqa: BLE001
        pass
    if (getattr(profile, "consent_status", "") or "") == "revoked":
        return False, "The Phase 9 profile's consent was revoked."
    return True, None


def _consented_reference(db: Session, profile) -> tuple[str | None, bytes | None]:
    """Only a granted, unrevoked Phase 9 reference sample may ever be sent to an engine."""
    reference_id = getattr(profile, "reference_id", None)
    reference_model = getattr(models, "VoiceReference", None)
    if not reference_id or reference_model is None:
        return None, None
    reference = db.get(reference_model, reference_id)
    if reference is None or getattr(reference, "revoked_at", None):
        return None, None
    if (getattr(reference, "consent_status", "") or "") != "granted":
        return None, None
    path = storage.absolute_path(reference.storage_key)
    return (reference.id, path.read_bytes()) if path.is_file() else (None, None)


def _gender_hint(db: Session, character: Character | None, profile=None) -> str:
    chars = dict(getattr(profile, "characteristics_json", None) or {}) if profile is not None else {}
    if chars.get("gender") in {"male", "female"}:
        return chars["gender"]
    if character is None:
        return "unknown"
    profile_model = getattr(models, "CharacterTranslationProfile", None)
    if profile_model is not None:
        row = db.scalar(select(profile_model).where(profile_model.character_id == character.id))
        if row is not None and row.gender in {"male", "female"}:
            return row.gender
    return "unknown"


def _voice_gender(module, engine: str, voice_id: str | None, language: str) -> str:
    try:
        option = module.find_voice(engine, voice_id, language) if module is not None else None
    except Exception:  # noqa: BLE001
        option = None
    gender = (option or {}).get("gender") or "unknown"
    return gender if gender in {"male", "female"} else "unknown"


def _default_preset(db: Session, character: Character | None, key: str, gender: str) -> dict:
    """Give each character in a project a different default voice.

    Characters are numbered in creation order within their gender bucket, so
    two characters never share a preset (until a bucket is exhausted) and the
    assignment is stable across runs. Unknown gender uses the male-sounding
    presets, which match the base MMS voice; set the gender for a better fit.
    """
    bucket = DEFAULT_VOICES["female" if gender == "female" else "male"]
    if character is None:
        return bucket[_stable_int("voice", key) % len(bucket)]
    peers = list(
        db.scalars(
            select(Character).where(Character.project_id == character.project_id).order_by(Character.created_at, Character.id)
        )
    )
    same_bucket = [peer.id for peer in peers if (_gender_hint(db, peer) == "female") == (gender == "female")]
    index = same_bucket.index(character.id) if character.id in same_bucket else _stable_int("voice", key)
    return bucket[index % len(bucket)]


def resolve_voice(db: Session, character: Character | None, language: str, speaker_label: str | None = None) -> dict:
    """Resolve how a character speaks in ``language``, with an account of every decision."""
    key = character.id if character else f"label:{speaker_label or 'unknown'}"
    seed = _stable_int("seed", key)
    notes: list[str] = []
    applied: list[str] = []
    shaped: list[str] = []
    profile = _phase9_profile(db, character.id if character else None, language)
    gender = _gender_hint(db, character, profile)
    module = tts.phase9_engine()
    render = tts.VoiceRender(synth="builtin", language=language, engine="mms",
                             model_id=tts.builtin_model(language), native=sorted(tts.BUILTIN["native"]), seed=seed)
    source = "default"
    profile_id = None
    reference_bytes = None

    usable, reason = _profile_usable(profile) if profile is not None else (False, None)
    if profile is not None and not usable:
        notes.append(reason or "Phase 9 profile not usable.")
    if profile is not None and usable:
        profile_id = profile.id
        chars = dict(getattr(profile, "characteristics_json", None) or {})
        params = dict(getattr(profile, "engine_params_json", None) or {})
        engine = (getattr(profile, "engine", None) or "").lower()
        voice_id = getattr(profile, "voice_id", None)
        runnable = False
        if module is not None and engine:
            try:
                runnable = bool(module.preview_available(engine, voice_id, language))
            except Exception:  # noqa: BLE001
                runnable = False
        if runnable:
            source = "phase9_profile"
            native = tts.phase9_capabilities(engine, voice_id)
            render.synth = "phase9"
            render.engine = engine
            render.voice_id = voice_id
            render.model_id = getattr(profile, "model_id", None) or voice_id
            render.params = params
            render.characteristics = chars
            render.native = sorted(native)
            applied.append(f"{engine} voice {voice_id or 'default'}")
            if params.get("seed") is not None:
                render.seed = int(params["seed"])
            reference_id, reference_bytes = _consented_reference(db, profile)
            if reference_id and "reference" in native:
                render.reference_id = reference_id
                applied.append("consented voice sample")
            elif getattr(profile, "reference_id", None) and "reference" not in native:
                notes.append("The profile has a voice sample, but this engine cannot use samples; it is not used.")
        else:
            source = "phase9_profile_fallback"
            notes.append(
                f"The profile's engine “{engine or 'none'}” cannot run here, so the built-in MMS voice renders "
                "this character with the profile's pitch, tone and speed."
            )
            if params.get("seed") is not None:
                render.seed = int(params["seed"])

        # Characteristics the engine cannot do itself are shaped in post-processing.
        native = set(render.native)
        pitch = str(chars.get("pitch") or "medium")
        if "pitch" not in native and pitch in tts.PITCH_SEMITONES and pitch != "medium":
            render.pitch_semitones = tts.PITCH_SEMITONES[pitch]
            shaped.append(f"pitch {pitch}")
        tone = str(chars.get("tone") or "neutral")
        if "tone" not in native and tone in tts.TONE_FILTERS and tone != "neutral":
            render.tone = tone
            shaped.append(f"tone {tone}")
        speed = chars.get("speed")
        if speed is not None and abs(float(speed) - 1.0) >= 0.01:
            if "speed" in native and render.synth == "phase9" and params.get("length_scale") is None and render.engine == "piper":
                render.params = {**render.params, "length_scale": round(1 / float(speed), 3)}
                applied.append(f"speed {speed}")
            elif "speed" in native and render.synth == "builtin":
                render.speed = float(speed)
                applied.append(f"speed {speed}")
            elif "speed" not in native:
                render.speed = float(speed)
                shaped.append(f"speed {speed}")
        # Same base voice for every character? Keep characters apart.
        base_gender = "male" if render.synth == "builtin" else _voice_gender(module, render.engine, render.voice_id, language)
        if gender == "female" and base_gender == "male" and render.pitch_semitones <= 0:
            preset = _default_preset(db, character, key, "female")
            render.pitch_semitones = preset["pitch_semitones"]
            shaped.append(f"pitch +{preset['pitch_semitones']:g} st (female character, male-sounding base voice)")
        if chars.get("accent") and "accent" not in native:
            notes.append(f"Accent “{chars['accent']}” is not supported by this engine and is not applied.")
    else:
        preset = _default_preset(db, character, key, gender)
        render.pitch_semitones = preset["pitch_semitones"]
        render.speed = preset["speed"]
        render.tone = preset["tone"]
        shaped += [f"pitch {preset['pitch_semitones']:+g} st", f"tone {preset['tone']}"]
        applied.append(f"speed {preset['speed']}")
        notes.append(
            "No Phase 9 voice profile for this character and language yet: a fixed built-in voice is used"
            + (f" for a {gender} character." if gender != "unknown" else " (set the character's gender to pick a better default).")
        )

    return {
        "render": render,
        "source": source,
        "profile_id": profile_id,
        "gender": gender,
        "applied": applied,
        "shaped": shaped,
        "notes": notes,
        "reference": reference_bytes if render.reference_id else None,
        "fingerprint": render.fingerprint(),
    }


def voice_payload(resolved: dict) -> dict:
    render: tts.VoiceRender = resolved["render"]
    data = asdict(render)
    return {
        **data,
        "source": resolved["source"],
        "profile_id": resolved["profile_id"],
        "gender": resolved["gender"],
        "applied": resolved["applied"],
        "shaped": resolved["shaped"],
        "notes": resolved["notes"],
        "fingerprint": resolved["fingerprint"],
        "engine_label": render.label,
        "license": render.license,
        "emotions": render.emotions(),
    }


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------


def _lines(db: Session, video: Video, language: str) -> list[dict]:
    transcript = latest_transcript(db, video.id)
    if transcript is None:
        return []
    segments = list(transcript.segments)
    ids = [segment.id for segment in segments]
    if not ids:
        return []
    assignments = {
        item.segment_id: item.character_id
        for item in db.scalars(select(CharacterAssignment).where(CharacterAssignment.segment_id.in_(ids)))
    }
    translations = {
        item.segment_id: item
        for item in db.scalars(
            select(SegmentTranslation).where(
                SegmentTranslation.segment_id.in_(ids), SegmentTranslation.language == language
            )
        )
    }
    takes: dict[str, list[DubAudioTake]] = {}
    for take in db.scalars(
        select(DubAudioTake)
        .where(DubAudioTake.segment_id.in_(ids), DubAudioTake.language == language)
        .order_by(DubAudioTake.take_number)
    ):
        takes.setdefault(take.segment_id, []).append(take)
    characters = {item.id: item for item in db.scalars(select(Character).where(Character.project_id == video.project_id))}
    voices: dict[str, dict] = {}
    out = []
    for segment in segments:
        character = characters.get(assignments.get(segment.id) or "")
        voice_key = character.id if character else f"label:{segment.speaker or 'unknown'}"
        if voice_key not in voices:
            voices[voice_key] = resolve_voice(db, character, language, segment.speaker)
        out.append(
            {
                "segment": segment,
                "character": character,
                "translation": translations.get(segment.id),
                "takes": takes.get(segment.id, []),
                "voice": voices[voice_key],
            }
        )
    return out


def _current(takes: list[DubAudioTake]) -> DubAudioTake | None:
    return next((take for take in reversed(takes) if take.is_current and take.status == "ready"), None)


def line_status(line: dict) -> str:
    text = (line["translation"].text if line["translation"] else "").strip()
    takes: list[DubAudioTake] = line["takes"]
    current = _current(takes)
    latest = takes[-1] if takes else None
    if not text:
        return "no_text"
    if latest is not None and latest.status == "failed" and (current is None or latest.take_number > current.take_number):
        return "failed"
    if current is None:
        return "missing"
    if current.text.strip() != text:
        return "stale_text"
    if current.voice_fingerprint and current.voice_fingerprint != line["voice"]["fingerprint"]:
        return "stale_voice"
    return "approved" if current.approved else "ready"


def serialize_take(take: DubAudioTake) -> dict:
    return {
        "id": take.id,
        "segment_id": take.segment_id,
        "language": take.language,
        "take_number": take.take_number,
        "status": take.status,
        "is_current": take.is_current,
        "approved": take.approved,
        "text": take.text,
        "emotion": take.emotion,
        "engine": take.engine,
        "model": take.model,
        "voice_source": take.voice_source,
        "voice": take.voice_json,
        "sample_rate": take.sample_rate,
        "duration_ms": take.duration_ms,
        "size_bytes": take.size_bytes,
        "stats": take.stats_json,
        "qc_flags": take.qc_flags,
        "error_message": take.error_message,
        "audio_url": f"/api/v1/dub-audio/takes/{take.id}/audio" if take.storage_key else None,
        "download_url": f"/api/v1/dub-audio/takes/{take.id}/audio?download=1" if take.storage_key else None,
        "synthetic": True,
        "created_at": take.created_at.isoformat() if take.created_at else None,
    }


def serialize_line(line: dict) -> dict:
    segment: TranscriptSegment = line["segment"]
    character: Character | None = line["character"]
    translation = line["translation"]
    takes = line["takes"]
    current = _current(takes)
    return {
        "segment_id": segment.id,
        "position": segment.position,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "source_text": segment.text,
        "speaker_label": segment.speaker,
        "character_id": character.id if character else None,
        "character_name": character.name if character else None,
        "text": translation.text if translation else "",
        "translation_origin": translation.origin if translation else None,
        "status": line_status(line),
        "current_take": serialize_take(current) if current else None,
        "takes": [serialize_take(take) for take in reversed(takes)],
        "voice_fingerprint": line["voice"]["fingerprint"],
    }


def bundle(db: Session, video: Video, language: str) -> dict:
    if language not in LANGUAGES:
        raise AppError(422, "UNSUPPORTED_LANGUAGE", "Language must be 'ur' or 'hi'.")
    lines = _lines(db, video, language)
    characters: dict[str, dict] = {}
    for line in lines:
        character = line["character"]
        key = character.id if character else f"label:{line['segment'].speaker or 'unknown'}"
        if key not in characters:
            characters[key] = {
                "key": key,
                "character_id": character.id if character else None,
                "name": character.name if character else (line["segment"].speaker or "Unassigned"),
                "lines": 0,
                "voice": voice_payload(line["voice"]),
            }
        characters[key]["lines"] += 1
    statuses = [line_status(line) for line in lines]
    latest_job = db.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.video_id == video.id, ProcessingJob.type == "dub_audio_generation")
        .order_by(ProcessingJob.created_at.desc())
    )
    from ..schemas import JobOut

    return {
        "video_id": video.id,
        "project_id": video.project_id,
        "language": language,
        "engine": tts.engine_info(),
        "characters": list(characters.values()),
        "lines": [serialize_line(line) for line in lines],
        "summary": {status: statuses.count(status) for status in sorted(set(statuses))} | {"total": len(lines)},
        "job": JobOut.model_validate(latest_job).model_dump(mode="json") if latest_job else None,
    }


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _apply_overrides(render: tts.VoiceRender, overrides: dict) -> tts.VoiceRender:
    emotion = (overrides.get("emotion") or "neutral").lower()
    if emotion not in render.emotions():
        raise AppError(
            422,
            "EMOTION_NOT_SUPPORTED",
            f"“{emotion}” delivery is not supported by {render.label}. Supported: {', '.join(render.emotions())}.",
        )
    render.emotion = emotion
    speed_factor = overrides.get("speed_factor")
    if speed_factor is not None:
        render.speed = round(max(0.5, min(2.0, render.speed * float(speed_factor))), 3)
        if render.synth == "phase9" and "speed" in render.native and render.engine == "piper":
            length = float(render.params.get("length_scale") or 1.0)
            render.params = {**render.params, "length_scale": round(length / float(speed_factor), 3)}
    pitch_offset = overrides.get("pitch_offset")
    if pitch_offset is not None:
        render.pitch_semitones = round(max(-12.0, min(12.0, render.pitch_semitones + float(pitch_offset))), 2)
    if overrides.get("pause_ms") is not None:
        render.pause_ms = int(max(0, min(2000, int(overrides["pause_ms"]))))
    return render


def _prune(db: Session, segment_id: str, language: str) -> None:
    takes = list(
        db.scalars(
            select(DubAudioTake)
            .where(DubAudioTake.segment_id == segment_id, DubAudioTake.language == language)
            .order_by(DubAudioTake.take_number)
        )
    )
    removable = [take for take in takes if not take.is_current and not take.approved]
    excess = len(takes) - tts.tts_settings.max_takes_per_line
    for take in removable[: max(0, excess)]:
        if take.storage_key:
            path = storage.absolute_path(take.storage_key)
            path.unlink(missing_ok=True)
        db.delete(take)


def generate_take(
    db: Session,
    video: Video,
    segment: TranscriptSegment,
    language: str,
    overrides: dict | None = None,
    *,
    job_id: str | None = None,
    check_active: ActiveCheck | None = None,
    raise_engine_errors: bool = True,
) -> DubAudioTake:
    overrides = overrides or {}
    translation = db.scalar(
        select(SegmentTranslation).where(
            SegmentTranslation.segment_id == segment.id, SegmentTranslation.language == language
        )
    )
    text = (translation.text if translation else "").strip()
    if not text:
        raise AppError(409, "NO_TRANSLATION", "Translate this line before generating its voice.")
    assignment = db.scalar(select(CharacterAssignment).where(CharacterAssignment.segment_id == segment.id))
    character = db.get(Character, assignment.character_id) if assignment and assignment.character_id else None
    resolved = resolve_voice(db, character, language, segment.speaker)
    base_fingerprint = resolved["fingerprint"]
    render = _apply_overrides(resolved["render"], overrides)
    previous = list(
        db.scalars(select(DubAudioTake).where(DubAudioTake.segment_id == segment.id, DubAudioTake.language == language))
    )
    next_number = max((item.take_number for item in previous), default=0) + 1
    if overrides.get("new_variation") and previous:
        # Same voice settings, different delivery: VITS noise seed changes the
        # prosody of the read, not the character's pitch/speed/tone.
        render.seed = _stable_int("variation", str(render.seed), str(next_number))
    take = DubAudioTake(
        id=new_id(),
        project_id=video.project_id,
        video_id=video.id,
        segment_id=segment.id,
        character_id=character.id if character else None,
        job_id=job_id,
        language=language,
        take_number=next_number,
        status="failed",
        is_current=False,
        text=text,
        emotion=render.emotion,
        engine=render.engine,
        model=render.model_id,
        voice_source=resolved["source"],
        voice_profile_id=resolved["profile_id"],
        # Baseline character voice: changes to the profile mark the line stale;
        # per-take delivery tweaks are recorded in voice_json, not here.
        voice_fingerprint=base_fingerprint,
        voice_json=voice_payload({**resolved, "render": render}),
    )
    flags: list[dict] = []
    if render.synth == "builtin" or render.engine == "mms":
        missing = tts.unsupported_characters(text, language)
        if missing:
            flags.append(
                tts.flag(
                    "unspeakable_characters",
                    "warning",
                    "The voice cannot pronounce these characters and skips them: " + " ".join(missing[:12])
                    + (" (write numbers and names in the target script)" if any(c.isdigit() or c.isascii() for c in missing) else ""),
                )
            )
    for note in resolved["notes"]:
        if "cannot run here" in note:
            flags.append(tts.flag("engine_fallback", "warning", note))
    try:
        rendered = tts.render_line(
            text, render, check_active, target_ms=segment.end_ms - segment.start_ms, reference=resolved["reference"]
        )
    except tts.TtsUnavailable as error:
        if raise_engine_errors:
            raise
        take.error_message = str(error)[:1000]
        take.qc_flags = flags + [tts.flag("generation_failed", "error", str(error)[:300])]
        db.add(take)
        db.commit()
        return take
    # Short key: Windows paths over 260 characters fail in deep data directories.
    key = f"projects/{video.project_id}/dub/{language}/{take.id}.wav"
    size = storage.write_bytes(key, rendered.wav)
    check = tts.validate_wav_file(storage.absolute_path(key))
    take.storage_key = key
    take.sample_rate = rendered.sample_rate
    take.duration_ms = rendered.stats["duration_ms"]
    take.size_bytes = size
    take.stats_json = {**rendered.stats, "chunks": rendered.chunks, "validated": check.get("ok", False)}
    take.qc_flags = flags + rendered.flags
    if not check.get("ok") and not any(item["code"] == "empty_audio" for item in take.qc_flags):
        take.qc_flags.append(tts.flag("corrupted", "error", check.get("error") or "Stored audio failed validation."))
    hard_error = any(item["severity"] == "error" for item in take.qc_flags)
    take.status = "failed" if hard_error else "ready"
    if hard_error:
        take.error_message = "; ".join(item["message"] for item in take.qc_flags if item["severity"] == "error")
    else:
        db.execute(
            update(DubAudioTake)
            .where(DubAudioTake.segment_id == segment.id, DubAudioTake.language == language)
            .values(is_current=False)
        )
        take.is_current = True
    db.add(take)
    db.flush()
    _prune(db, segment.id, language)
    db.commit()
    db.refresh(take)
    return take


def preview_voice(db: Session, character: Character | None, language: str, text: str | None, overrides: dict | None = None) -> tts.Rendered:
    resolved = resolve_voice(db, character, language)
    render = _apply_overrides(resolved["render"], overrides or {})
    return tts.render_line((text or PREVIEW_TEXT[language]).strip()[:300], render, reference=resolved["reference"])


def set_approved(db: Session, take: DubAudioTake, approved: bool) -> DubAudioTake:
    if approved and take.status != "ready":
        raise AppError(409, "TAKE_NOT_READY", "Only a successfully generated take can be approved.")
    if approved and not take.is_current:
        select_take(db, take)
    take.approved = approved
    db.commit()
    db.refresh(take)
    return take


def select_take(db: Session, take: DubAudioTake) -> DubAudioTake:
    if take.status != "ready" or not take.storage_key:
        raise AppError(409, "TAKE_NOT_READY", "Only a successfully generated take can be used.")
    db.execute(
        update(DubAudioTake)
        .where(DubAudioTake.segment_id == take.segment_id, DubAudioTake.language == take.language)
        .values(is_current=False)
    )
    take.is_current = True
    db.commit()
    db.refresh(take)
    return take


# ---------------------------------------------------------------------------
# Batch job
# ---------------------------------------------------------------------------


def select_batch(db: Session, video: Video, language: str, options: dict) -> tuple[list[str], dict]:
    mode = options.get("mode") or "pending"
    include_approved = bool(options.get("include_approved"))
    wanted = set(options.get("segment_ids") or [])
    character_id = options.get("character_id")
    resume_job = options.get("resume_from_job_id")
    chosen: list[str] = []
    skipped = {"approved": 0, "up_to_date": 0, "no_text": 0, "done_before_failure": 0, "filtered": 0}
    for line in _lines(db, video, language):
        segment = line["segment"]
        if wanted and segment.id not in wanted:
            continue
        if character_id and (line["character"].id if line["character"] else None) != character_id:
            skipped["filtered"] += 1
            continue
        status = line_status(line)
        if status == "no_text":
            skipped["no_text"] += 1
            continue
        if resume_job and any(take.job_id == resume_job and take.status == "ready" for take in line["takes"]):
            skipped["done_before_failure"] += 1
            continue
        if status == "approved" and not include_approved:
            skipped["approved"] += 1
            continue
        if mode == "missing" and status != "missing":
            skipped["up_to_date"] += 1
            continue
        if mode == "failed" and status != "failed":
            skipped["up_to_date"] += 1
            continue
        if mode == "pending" and status in {"ready", "approved"}:
            skipped["up_to_date"] += 1
            continue
        chosen.append(segment.id)
    return chosen, skipped


def create_dub_job(db: Session, video: Video, options: dict) -> ProcessingJob:
    from .jobs import create_import_job

    active = db.scalar(
        select(ProcessingJob).where(
            ProcessingJob.video_id == video.id,
            ProcessingJob.type == "dub_audio_generation",
            ProcessingJob.status.in_(["queued", "running"]),
        )
    )
    if active:
        raise AppError(409, "DUB_AUDIO_JOB_ACTIVE", "A voice generation job is already running for this video.")
    return create_import_job(db, video.project_id, video.id, "dub_audio_generation", DUB_STEP_NAMES, options)


def run_dub_job(job_id: str, video_id: str, options: dict | None = None) -> None:
    from .jobs import JobCancelled, _job_status, _set_step

    db = SessionLocal()
    try:
        job = db.get(ProcessingJob, job_id)
        video = db.get(Video, video_id)
        if not job or not video:
            return
        opts = options or job.options_json or {}
        language = opts.get("language") or "ur"

        def check_active() -> None:
            status = _job_status(job_id)
            if status is None or status == "cancelled":
                raise JobCancelled("Job was cancelled or removed")

        claim = db.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id, ProcessingJob.status == "queued")
            .values(status="running")
        )
        db.commit()
        if claim.rowcount != 1:
            db.refresh(job)
            if job.status != "cancelled":
                return
            raise JobCancelled("Job was cancelled before it started")
        db.refresh(job)

        _set_step(db, job, "select_lines", "running", "Choosing lines", 3)
        chosen, skipped = select_batch(db, video, language, opts)
        detail = f"{len(chosen)} to generate · " + " · ".join(f"{value} {key.replace('_', ' ')}" for key, value in skipped.items() if value)
        _set_step(db, job, "select_lines", "completed", detail, 6)

        _set_step(db, job, "resolve_voices", "running", "Resolving character voices", 8)
        lines = {line["segment"].id: line for line in _lines(db, video, language)}
        voices = {line["voice"]["fingerprint"]: line["voice"] for seg, line in lines.items() if seg in set(chosen)}
        sources = sorted({voice["source"] for voice in voices.values()})
        _set_step(db, job, "resolve_voices", "completed", f"{len(voices)} voices · {', '.join(sources) or 'none'}", 10)

        _set_step(db, job, "load_engine", "running", "Loading speech engine", 12)
        check_active()
        labels = sorted({voice["render"].label for voice in voices.values()})
        if chosen and any(voice["render"].synth == "builtin" for voice in voices.values()):
            tts.load_builtin(language, check_active)
        _set_step(db, job, "load_engine", "completed", ", ".join(labels) or "nothing to load", 15)

        _set_step(db, job, "synthesize", "running", f"0/{len(chosen)}", 16)
        generated = failed = 0
        for index, segment_id in enumerate(chosen):
            check_active()
            segment = db.get(TranscriptSegment, segment_id)
            if segment is None:
                continue
            take = generate_take(
                db,
                video,
                segment,
                language,
                {"emotion": opts.get("emotion")} if opts.get("emotion") else None,
                job_id=job_id,
                check_active=check_active,
                raise_engine_errors=False,
            )
            if take.status == "ready":
                generated += 1
            else:
                failed += 1
            step = next((item for item in job.steps if item.name == "synthesize"), None)
            if step:
                step.detail = f"{index + 1}/{len(chosen)} · {failed} failed"
            job.progress = 16 + int(78 * (index + 1) / max(1, len(chosen)))
            job.current_step = "synthesize"
            db.commit()
        _set_step(db, job, "synthesize", "completed", f"{generated} generated · {failed} failed", 95)
        _set_step(
            db, job, "quality_check", "completed",
            "all takes validated" if not failed else f"{failed} lines need attention (retry failed lines)", 97,
        )
        final = db.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id, ProcessingJob.status == "running")
            .values(status="completed", current_step="finalize", progress=100, error_code=None, error_message=None)
        )
        if final.rowcount != 1:
            db.rollback()
            raise JobCancelled("Job was cancelled before finishing")
        db.commit()
        db.refresh(job)
        _set_step(db, job, "finalize", "completed", f"{generated} synthetic takes saved", 100)
    except Exception as error:  # noqa: BLE001 — record every failure on the job
        from .jobs import JobCancelled as _Cancelled

        db.rollback()
        current = db.get(ProcessingJob, job_id)
        if current is None:
            return
        if isinstance(error, (_Cancelled, tts.TtsCancelled)) or current.status == "cancelled":
            current.status = "cancelled"
            current.error_code = "CANCELLED"
            current.error_message = "Cancelled by user"
            final_status, detail = "cancelled", "Cancelled by user"
        else:
            current.status = "failed"
            current.error_code = error.code if isinstance(error, AppError) else (
                "TTS_ENGINE_UNAVAILABLE" if isinstance(error, tts.TtsUnavailable) else "DUB_AUDIO_FAILED"
            )
            current.error_message = str(error)[:1000]
            final_status, detail = "failed", str(error)[:500]
        for step in current.steps:
            if step.status == "running" or (final_status == "cancelled" and step.status == "pending"):
                step.status = final_status
                step.detail = detail
                step.finished_at = utcnow()
        db.commit()
    finally:
        db.close()
