from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AudioMixerSetting, Project
from .media import AppError

MIX_TRACK_ROLES = (
    "dialogue",
    "music",
    "sfx",
    "ambience",
    "urdu_dub",
    "hindi_dub",
)
MIX_TRACK_LABELS = {
    "dialogue": "Original Dialogue",
    "music": "Background Music",
    "sfx": "Sound Effects",
    "ambience": "Ambience",
    "urdu_dub": "Urdu Dub",
    "hindi_dub": "Hindi Dub",
}
PLANNED_TRACKS = frozenset({"urdu_dub", "hindi_dub"})
DEFAULT_TRACK_VOLUME = {
    "dialogue": 0.9,
    "music": 0.8,
    "sfx": 0.8,
    "ambience": 0.55,
    "urdu_dub": 0.9,
    "hindi_dub": 0.9,
}


def default_settings() -> dict:
    return {
        "master_volume": 1.0,
        "preview_enabled": False,
        "tracks": {
            role: {
                "volume": DEFAULT_TRACK_VOLUME[role],
                "muted": False,
                "solo": False,
                "enabled": role not in PLANNED_TRACKS,
            }
            for role in MIX_TRACK_ROLES
        },
    }


def _clamp(value: object, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number:
        return fallback
    return min(1.0, max(0.0, number))


def normalize_settings(payload: dict) -> dict:
    incoming_tracks = payload.get("tracks") or {}
    if not isinstance(incoming_tracks, dict):
        raise AppError(422, "INVALID_MIX_SETTINGS", "Tracks must be an object.")
    unknown = sorted(set(incoming_tracks) - set(MIX_TRACK_ROLES))
    if unknown:
        raise AppError(422, "INVALID_MIX_TRACK", f"Unknown mixer track: {unknown[0]}")

    normalized = default_settings()
    normalized["master_volume"] = _clamp(payload.get("master_volume", 1.0), 1.0)
    normalized["preview_enabled"] = bool(payload.get("preview_enabled", False))
    for role in MIX_TRACK_ROLES:
        incoming = incoming_tracks.get(role)
        if not isinstance(incoming, dict):
            continue
        track = normalized["tracks"][role]
        track["volume"] = _clamp(incoming.get("volume", track["volume"]), track["volume"])
        track["muted"] = bool(incoming.get("muted", track["muted"]))
        track["solo"] = bool(incoming.get("solo", track["solo"]))
        track["enabled"] = bool(incoming.get("enabled", track["enabled"]))
    return normalized


def serialize_settings(project_id: str, setting: AudioMixerSetting) -> dict:
    payload = normalize_settings(setting.settings_json or {})
    return {
        "project_id": project_id,
        "master_volume": payload["master_volume"],
        "preview_enabled": payload["preview_enabled"],
        "tracks": {
            role: {"role": role, "label": MIX_TRACK_LABELS[role], **payload["tracks"][role]}
            for role in MIX_TRACK_ROLES
        },
        "updated_at": setting.updated_at,
    }


def _find_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise AppError(404, "NOT_FOUND", "Project not found")
    return project


def get_mixer_setting(db: Session, project_id: str) -> AudioMixerSetting:
    _find_project(db, project_id)
    setting = db.scalar(select(AudioMixerSetting).where(AudioMixerSetting.project_id == project_id))
    if not setting:
        setting = AudioMixerSetting(project_id=project_id, settings_json=default_settings())
        db.add(setting)
        db.commit()
        db.refresh(setting)
    return setting


def read_settings(db: Session, project_id: str) -> dict:
    return serialize_settings(project_id, get_mixer_setting(db, project_id))


def save_settings(db: Session, project_id: str, payload: dict) -> dict:
    setting = get_mixer_setting(db, project_id)
    setting.settings_json = normalize_settings(payload)
    db.commit()
    db.refresh(setting)
    return serialize_settings(project_id, setting)


def reset_settings(db: Session, project_id: str) -> dict:
    setting = get_mixer_setting(db, project_id)
    setting.settings_json = default_settings()
    db.commit()
    db.refresh(setting)
    return serialize_settings(project_id, setting)
