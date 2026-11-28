"""Phase 14 - professional multitrack timeline editor service.

Non-destructive clip model on top of Phase 3 video, Phase 4 stems, Phase 7
characters, Phase 9 voice profiles, Phase 10 takes, Phase 11 fitted dubbed
dialogue, Phase 12 mix studio, Phase 13 lip-sync segments and Phase 14
external audio imports.

Every mutation commits immediately (autosave), appends a full snapshot to
``timeline_history`` (undo/redo + interrupted-session recovery) and never
touches original source files.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import Character, MediaAsset, ProcessingJob, Transcript, TranscriptSegment, Video, new_id, utcnow
from ..models_timeline import (
    LIPSYNC_NEEDS_REGENERATION,
    LIPSYNC_PENDING,
    LIPSYNC_SYNCED,
    SOURCE_AUDIO_STEM,
    SOURCE_DUB_TRACK,
    SOURCE_DUB_TAKE,
    SOURCE_EXTERNAL_AUDIO,
    SOURCE_LIP_SYNC,
    SOURCE_ORIGINAL_AUDIO,
    SOURCE_VIDEO,
    TRACK_KINDS,
    ExternalAudio,
    TimelineClip,
    TimelineHistory,
    TimelineProject,
    TimelineTrack,
)
from .media import AppError
from .storage import storage

SAMPLE_RATE = 48000
FITTED = {"fitted", "fitted_fast", "needs_attention"}
MIN_CLIP_MS = 1
EXPORT_STEP_NAMES = ["collect_tracks", "render_tracks", "mux_outputs", "finalize"]

TRACK_COLORS = {
    "video": "#7c5cff",
    "original_english": "#4f8cff",
    "urdu_dub": "#2ec4b6",
    "hindi_dub": "#ffb454",
    "background_music": "#a78bfa",
    "sound_effects": "#f472b6",
    "ambience": "#34d399",
    "external_audio": "#60a5fa",
    "lip_sync": "#ef4444",
}

DEFAULT_TRACKS: list[dict] = [
    {"kind": "video", "name": "Original Video", "language": None},
    {"kind": "original_english", "name": "Original English Dialogue", "language": "en"},
    {"kind": "urdu_dub", "name": "Urdu Dub", "language": "ur"},
    {"kind": "hindi_dub", "name": "Hindi Dub", "language": "hi"},
    {"kind": "background_music", "name": "Background Music", "language": None},
    {"kind": "sound_effects", "name": "Sound Effects", "language": None},
    {"kind": "ambience", "name": "Ambience", "language": None},
    {"kind": "external_audio", "name": "External Audio", "language": None},
    {"kind": "lip_sync", "name": "Lip Sync", "language": None},
]

# Language tracks (Task 6).
LANGUAGE_TRACK_KINDS = {"original_english": "en", "urdu_dub": "ur", "hindi_dub": "hi"}


def ffmpeg_binary() -> str:
    from .media import ffmpeg_binary as _bin

    binary = _bin()
    if not binary:
        raise AppError(503, "FFMPEG_UNAVAILABLE", "No ffmpeg binary is available for audio rendering.")
    return binary


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def clip_fingerprint(clip: TimelineClip) -> str:
    payload = "|".join(
        str(value)
        for value in (
            clip.source_type, clip.source_id, clip.storage_key, clip.external_audio_id,
            clip.start_ms, clip.duration_ms, clip.source_in_ms, clip.source_out_ms,
            round(clip.volume_db, 3), clip.character_id, clip.language, clip.segment_id, clip.muted,
        )
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def serialize_clip(clip: TimelineClip) -> dict:
    meta = dict(clip.metadata_json or {})
    return {
        "id": clip.id,
        "timeline_id": clip.timeline_id,
        "track_id": clip.track_id,
        "source_type": clip.source_type,
        "source_id": clip.source_id,
        "external_audio_id": clip.external_audio_id,
        "storage_key": clip.storage_key,
        "name": clip.name,
        "character_id": clip.character_id,
        "character_name": clip.character_name,
        "segment_id": clip.segment_id,
        "language": clip.language,
        "start_ms": clip.start_ms,
        "duration_ms": clip.duration_ms,
        "end_ms": clip.start_ms + clip.duration_ms,
        "source_in_ms": clip.source_in_ms,
        "source_out_ms": clip.source_out_ms,
        "source_duration_ms": clip.source_duration_ms,
        "volume_db": clip.volume_db,
        "fade_in_ms": clip.fade_in_ms,
        "fade_out_ms": clip.fade_out_ms,
        "muted": clip.muted,
        "locked": clip.locked,
        "replaces_clip_id": clip.replaces_clip_id,
        "metadata": meta,
        "voice_profile": meta.get("voice_profile"),
        "replaced_by_clip_id": meta.get("replaced_by_clip_id"),
        "audio_url": f"/api/v1/timeline/clips/{clip.id}/audio" if clip.storage_key or clip.external_audio_id else None,
        "waveform_url": f"/api/v1/timeline/clips/{clip.id}/waveform",
        "fingerprint": clip_fingerprint(clip),
        "updated_at": clip.updated_at.isoformat() if clip.updated_at else None,
    }


def serialize_track(track: TimelineTrack) -> dict:
    return {
        "id": track.id,
        "timeline_id": track.timeline_id,
        "kind": track.kind,
        "name": track.name,
        "language": track.language,
        "color": track.color,
        "position": track.position,
        "enabled": track.enabled,
        "muted": track.muted,
        "solo": track.solo,
        "locked": track.locked,
        "volume_db": track.volume_db,
        "pan": track.pan,
    }


def _clips(db: Session, timeline_id: str) -> list[TimelineClip]:
    return list(db.scalars(select(TimelineClip).where(TimelineClip.timeline_id == timeline_id)))


def _tracks(db: Session, timeline_id: str) -> list[TimelineTrack]:
    return list(db.scalars(select(TimelineTrack).where(TimelineTrack.timeline_id == timeline_id).order_by(TimelineTrack.position)))


def serialize_timeline(db: Session, timeline: TimelineProject) -> dict:
    tracks = _tracks(db, timeline.id)
    clips = _clips(db, timeline.id)
    by_track: dict[str, list[dict]] = {track.id: [] for track in tracks}
    for clip in sorted(clips, key=lambda item: (item.start_ms, item.id)):
        by_track.setdefault(clip.track_id, []).append(serialize_clip(clip))
    total = timeline.duration_ms
    for clip in clips:
        total = max(total, clip.start_ms + clip.duration_ms)
    history_max = db.scalar(select(TimelineHistory.seq).where(TimelineHistory.timeline_id == timeline.id).order_by(TimelineHistory.seq.desc()).limit(1)) or 0
    return {
        "id": timeline.id,
        "project_id": timeline.project_id,
        "video_id": timeline.video_id,
        "name": timeline.name,
        "duration_ms": total,
        "sample_rate": timeline.sample_rate,
        "revision": timeline.revision,
        "autosave": timeline.autosave,
        "tracks": [
            {**serialize_track(track), "clips": by_track.get(track.id, [])}
            for track in tracks
        ],
        "clip_count": len(clips),
        "can_undo": timeline.history_cursor > 0,
        "can_redo": timeline.history_cursor < history_max,
        "history_cursor": timeline.history_cursor,
        "history_length": history_max,
        "saved_at": timeline.updated_at.isoformat() if timeline.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Snapshot history (undo/redo + recovery)
# ---------------------------------------------------------------------------


def _snapshot(db: Session, timeline: TimelineProject) -> dict:
    tracks = [serialize_track(track) for track in _tracks(db, timeline.id)]
    clips = [serialize_clip(clip) for clip in _clips(db, timeline.id)]
    return {"tracks": tracks, "clips": clips, "duration_ms": timeline.duration_ms, "name": timeline.name}


def _record(db: Session, timeline: TimelineProject, action: str) -> None:
    db.flush()
    snapshot = _snapshot(db, timeline)
    # truncate redo tail
    for row in db.scalars(
        select(TimelineHistory).where(TimelineHistory.timeline_id == timeline.id, TimelineHistory.seq > timeline.history_cursor)
    ):
        db.delete(row)
    db.flush()
    timeline.history_cursor += 1
    timeline.revision += 1
    db.add(
        TimelineHistory(
            id=new_id(), timeline_id=timeline.id, seq=timeline.history_cursor, action=action[:80], snapshot_json=snapshot
        )
    )
    timeline.updated_at = utcnow()
    db.commit()


def _restore(db: Session, timeline: TimelineProject, seq: int) -> None:
    row = db.scalar(select(TimelineHistory).where(TimelineHistory.timeline_id == timeline.id, TimelineHistory.seq == seq))
    if row is None:
        raise AppError(409, "HISTORY_MISSING", "That history snapshot is not available.")
    snap = row.snapshot_json or {}
    for clip in _clips(db, timeline.id):
        db.delete(clip)
    for track in _tracks(db, timeline.id):
        db.delete(track)
    db.flush()
    for track in snap.get("tracks", []):
        db.add(
            TimelineTrack(
                id=track["id"], timeline_id=timeline.id, kind=track["kind"], name=track["name"],
                language=track.get("language"), color=track.get("color", "#4f8cff"), position=track.get("position", 0),
                enabled=track.get("enabled", True), muted=track.get("muted", False), solo=track.get("solo", False),
                locked=track.get("locked", False), volume_db=track.get("volume_db", 0.0), pan=track.get("pan", 0.0),
            )
        )
    db.flush()
    for clip in snap.get("clips", []):
        db.add(
            TimelineClip(
                id=clip["id"], timeline_id=timeline.id, track_id=clip["track_id"], source_type=clip["source_type"],
                source_id=clip.get("source_id"), external_audio_id=clip.get("external_audio_id"), storage_key=clip.get("storage_key"),
                name=clip.get("name", "Clip"), character_id=clip.get("character_id"), character_name=clip.get("character_name"),
                segment_id=clip.get("segment_id"), language=clip.get("language"), start_ms=clip["start_ms"],
                duration_ms=clip["duration_ms"], source_in_ms=clip.get("source_in_ms", 0), source_out_ms=clip.get("source_out_ms"),
                source_duration_ms=clip.get("source_duration_ms"), volume_db=clip.get("volume_db", 0.0),
                fade_in_ms=clip.get("fade_in_ms", 0), fade_out_ms=clip.get("fade_out_ms", 0), muted=clip.get("muted", False),
                locked=clip.get("locked", False), replaces_clip_id=clip.get("replaces_clip_id"),
                metadata_json=clip.get("metadata", {}),
            )
        )
    timeline.duration_ms = snap.get("duration_ms", timeline.duration_ms)
    timeline.history_cursor = seq
    timeline.revision += 1
    timeline.updated_at = utcnow()
    db.commit()


def undo(db: Session, timeline: TimelineProject) -> dict:
    if timeline.history_cursor <= 0:
        raise AppError(409, "NOTHING_TO_UNDO", "There is nothing to undo.")
    _restore(db, timeline, timeline.history_cursor - 1)
    return serialize_timeline(db, timeline)


def redo(db: Session, timeline: TimelineProject) -> dict:
    history_max = db.scalar(select(TimelineHistory.seq).where(TimelineHistory.timeline_id == timeline.id).order_by(TimelineHistory.seq.desc()).limit(1)) or 0
    if timeline.history_cursor >= history_max:
        raise AppError(409, "NOTHING_TO_REDO", "There is nothing to redo.")
    _restore(db, timeline, timeline.history_cursor + 1)
    return serialize_timeline(db, timeline)


def history(db: Session, timeline: TimelineProject) -> list[dict]:
    rows = db.scalars(select(TimelineHistory).where(TimelineHistory.timeline_id == timeline.id).order_by(TimelineHistory.seq))
    return [{"seq": row.seq, "action": row.action, "at": row.created_at.isoformat(), "current": row.seq == timeline.history_cursor} for row in rows]


# ---------------------------------------------------------------------------
# Snapping
# ---------------------------------------------------------------------------


def snap_value(value_ms: int, snap_ms: int, targets: list[int] | None = None, tolerance_ms: int | None = None) -> int:
    """Snap to grid + nearby edges. snap_ms=0 disables grid snapping."""
    result = max(0, int(round(value_ms)))
    if snap_ms and snap_ms > 0:
        result = max(0, int(round(result / snap_ms)) * snap_ms)
    tol = tolerance_ms if tolerance_ms is not None else (max(1, snap_ms // 2) if snap_ms else 40)
    for target in sorted(set(targets or [])):
        if abs(result - target) <= tol:
            return max(0, int(target))
    return result


def _snap_targets(db: Session, track_id: str, exclude_clip_id: str | None = None) -> list[int]:
    targets = [0]
    for clip in db.scalars(select(TimelineClip).where(TimelineClip.track_id == track_id)):
        if clip.id == exclude_clip_id:
            continue
        targets.append(clip.start_ms)
        targets.append(clip.start_ms + clip.duration_ms)
    return targets


# ---------------------------------------------------------------------------
# Source resolution & audio decoding
# ---------------------------------------------------------------------------


def _clip_source_path(db: Session, clip: TimelineClip) -> Path | None:
    key = clip.storage_key
    if clip.source_type == SOURCE_EXTERNAL_AUDIO and clip.external_audio_id:
        external = db.get(ExternalAudio, clip.external_audio_id)
        key = external.storage_key if external else key
    if not key:
        return None
    path = storage.absolute_path(key)
    return path if path.is_file() else None


def clip_source_path(db: Session, clip: TimelineClip) -> Path | None:
    return _clip_source_path(db, clip)


def decode_audio(path: Path, sr: int = SAMPLE_RATE, start_ms: int | None = None, end_ms: int | None = None) -> np.ndarray:
    """Decode any audio/video container to mono float32 at ``sr`` (streaming, no full-file buffer in app memory beyond the requested window)."""
    command = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error"]
    if start_ms is not None:
        command += ["-ss", f"{max(0, start_ms) / 1000:.3f}"]
    command += ["-i", str(path)]
    if end_ms is not None and start_ms is not None:
        command += ["-t", f"{max(0, end_ms - start_ms) / 1000:.3f}"]
    elif end_ms is not None:
        command += ["-t", f"{max(0, end_ms) / 1000:.3f}"]
    command += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(result.stdout, dtype="<f4").astype(np.float32)


def peaks(audio: np.ndarray, buckets: int) -> list[list[float]]:
    buckets = max(1, int(buckets))
    if audio.size == 0:
        return [[0.0, 0.0] for _ in range(buckets)]
    edges = np.linspace(0, audio.size, buckets + 1).astype(int)
    out: list[list[float]] = []
    for a, b in zip(edges[:-1], edges[1:]):
        chunk = audio[a : max(b, a + 1)]
        out.append([round(float(chunk.min()), 4), round(float(chunk.max()), 4)])
    return out


def source_waveform(db: Session, clip: TimelineClip, start_ms: int, end_ms: int, buckets: int) -> dict:
    path = _clip_source_path(db, clip)
    if path is None:
        return {"peaks": [[0.0, 0.0]] * max(1, buckets), "available": False, "start_ms": start_ms, "end_ms": end_ms}
    # window relative to the *source*, clip trim applied
    audio = decode_audio(path, start_ms=start_ms, end_ms=end_ms)
    return {
        "peaks": peaks(audio, buckets),
        "available": True,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "clip_start_ms": clip.start_ms,
        "source_in_ms": clip.source_in_ms,
    }


# ---------------------------------------------------------------------------
# Creation / population from earlier phases
# ---------------------------------------------------------------------------


def _latest_asset(db: Session, video_id: str, kind: str) -> MediaAsset | None:
    return db.scalar(
        select(MediaAsset).where(MediaAsset.video_id == video_id, MediaAsset.kind == kind).order_by(MediaAsset.version.desc())
    )


def _original_audio_key(db: Session, video: Video) -> tuple[str, str] | None:
    for kind in ("stem_dialogue", "dialogue", "source_audio"):
        asset = _latest_asset(db, video.id, kind)
        if asset is not None and storage.absolute_path(asset.storage_key).is_file():
            return asset.storage_key, kind
    if storage.absolute_path(video.storage_key).is_file():
        return video.storage_key, "video"
    return None


def _character_name(db: Session, character_id: str | None, fallback: str | None) -> str | None:
    if character_id:
        character = db.get(Character, character_id)
        if character:
            return character.name
    return fallback


def _voice_profile_snapshot(db: Session, character_id: str | None, language: str | None) -> dict | None:
    if not character_id:
        return None
    from ..models import CharacterVoiceProfile

    langs = [language] if language else ["ur", "hi"]
    out: dict = {}
    for lang in langs:
        if not lang:
            continue
        profile = db.scalar(
            select(CharacterVoiceProfile)
            .where(CharacterVoiceProfile.character_id == character_id, CharacterVoiceProfile.language == lang, CharacterVoiceProfile.active.is_(True))
            .order_by(CharacterVoiceProfile.version.desc())
        )
        if profile is not None:
            out[lang] = {
                "profile_id": profile.id,
                "version": profile.version,
                "engine": profile.engine,
                "model_id": profile.model_id,
                "voice_id": profile.voice_id,
                "characteristics": profile.characteristics_json or {},
                "consent_status": profile.consent_status,
                "locked": profile.locked,
            }
    return out or None


def create_timeline(db: Session, video: Video, name: str = "Main timeline") -> TimelineProject:
    timeline = TimelineProject(
        id=new_id(),
        project_id=video.project_id,
        video_id=video.id,
        name=name,
        duration_ms=video.duration_ms or 0,
        sample_rate=SAMPLE_RATE,
        revision=0,
        history_cursor=0,
        autosave=True,
    )
    db.add(timeline)
    db.flush()
    for position, spec in enumerate(DEFAULT_TRACKS):
        db.add(
            TimelineTrack(
                id=new_id(), timeline_id=timeline.id, kind=spec["kind"], name=spec["name"], language=spec["language"],
                color=TRACK_COLORS.get(spec["kind"], "#4f8cff"), position=position, enabled=True, muted=False, solo=False,
                locked=False, volume_db=0.0, pan=0.0,
            )
        )
    db.flush()
    populate_from_phases(db, timeline, video)
    db.flush()
    _record(db, timeline, "create_timeline")
    db.refresh(timeline)
    return timeline


def get_or_create_timeline(db: Session, video: Video) -> TimelineProject:
    timeline = db.scalar(select(TimelineProject).where(TimelineProject.video_id == video.id).order_by(TimelineProject.created_at))
    if timeline is None:
        return create_timeline(db, video)
    return timeline


def populate_from_phases(db: Session, timeline: TimelineProject, video: Video) -> None:
    tracks = {track.kind: track for track in _tracks(db, timeline.id)}
    duration = video.duration_ms or timeline.duration_ms or 0

    # --- Original Video Track (Phase 3) ------------------------------------
    if "video" in tracks and storage.absolute_path(video.storage_key).is_file():
        db.add(
            TimelineClip(
                id=new_id(), timeline_id=timeline.id, track_id=tracks["video"].id, source_type=SOURCE_VIDEO,
                source_id=video.id, storage_key=video.storage_key, name=video.original_filename, start_ms=0,
                duration_ms=max(duration, 1), source_in_ms=0, source_out_ms=duration or None, source_duration_ms=duration or None,
                metadata_json={"thumbnails_url": f"/api/v1/videos/{video.id}/assets?kind=thumbnail"},
            )
        )

    # --- Original English Dialogue (Phase 3/4 original) --------------------
    original = _original_audio_key(db, video)
    if "original_english" in tracks and original is not None:
        key, kind = original
        db.add(
            TimelineClip(
                id=new_id(), timeline_id=timeline.id, track_id=tracks["original_english"].id,
                source_type=SOURCE_ORIGINAL_AUDIO if kind != "video" else SOURCE_VIDEO,
                source_id=None, storage_key=key, name="Original English Dialogue", language="en",
                start_ms=0, duration_ms=max(duration, 1), source_in_ms=0, source_out_ms=duration or None,
                source_duration_ms=duration or None, metadata_json={"original_kind": kind},
            )
        )

    # --- Stems (Phase 4): music / sfx / ambience ---------------------------
    stem_map = {
        "background_music": "stem_music",
        "sound_effects": "stem_sfx",
        "ambience": "stem_ambience",
    }
    for kind, asset_kind in stem_map.items():
        track = tracks.get(kind)
        asset = _latest_asset(db, video.id, asset_kind)
        if track is None or asset is None or not storage.absolute_path(asset.storage_key).is_file():
            continue
        # MediaAsset has no duration column; Phase 4 records it in metadata (else: film length).
        meta = asset.metadata_json or {}
        length = int(meta.get("duration_ms") or round(float(meta.get("duration_s") or 0) * 1000) or duration or 0)
        db.add(
            TimelineClip(
                id=new_id(), timeline_id=timeline.id, track_id=track.id, source_type=SOURCE_AUDIO_STEM,
                source_id=asset.id, storage_key=asset.storage_key, name=asset.kind, start_ms=0,
                duration_ms=max(length, 1), source_in_ms=0, source_out_ms=length or None, source_duration_ms=length or None,
                metadata_json={"asset_kind": asset.kind, "mime_type": asset.mime_type},
            )
        )

    # --- Phase 11 fitted dubbed dialogue -> Urdu / Hindi tracks ------------
    from ..models_timing import DubTimingPlacement
    from ..models_tts import DubAudioTake

    segments = {seg.id: seg for seg in _all_segments(db, video.id)}
    for language, kind in (("ur", "urdu_dub"), ("hi", "hindi_dub")):
        track = tracks.get(kind)
        if track is None:
            continue
        rows = db.scalars(
            select(DubTimingPlacement).where(
                DubTimingPlacement.video_id == video.id, DubTimingPlacement.language == language,
                DubTimingPlacement.placed_start_ms.isnot(None), DubTimingPlacement.status.in_(FITTED),
            )
        )
        for row in rows:
            if not row.storage_key or not storage.absolute_path(row.storage_key).is_file():
                continue
            seg = segments.get(row.segment_id)
            take = db.get(DubAudioTake, row.take_id) if row.take_id else None
            name = (seg.text if seg else None) or (take.text if take else None) or f"Line {row.segment_id[:8]}"
            db.add(
                TimelineClip(
                    id=new_id(), timeline_id=timeline.id, track_id=track.id, source_type=SOURCE_DUB_TAKE,
                    source_id=row.take_id, storage_key=row.storage_key,
                    name=name[:120], character_id=row.character_id,
                    character_name=_character_name(db, row.character_id, seg.speaker if seg else None),
                    segment_id=row.segment_id, language=language, start_ms=row.placed_start_ms or 0,
                    duration_ms=max(row.fitted_duration_ms or (row.placed_end_ms or 0) - (row.placed_start_ms or 0), 1),
                    source_in_ms=0, source_out_ms=row.fitted_duration_ms, source_duration_ms=row.fitted_duration_ms or row.take_duration_ms,
                    metadata_json={
                        "placement_id": row.id,
                        "status": row.status,
                        "strategy": row.strategy,
                        "approved": row.approved,
                        "voice_profile": _voice_profile_snapshot(db, row.character_id, language),
                    },
                )
            )

    # --- Phase 13 lip-sync generated segments ------------------------------
    lip_track = tracks.get("lip_sync")
    if lip_track is not None:
        LipSyncResult = _lipsync_model()
        for seg in db.scalars(select(LipSyncResult).where(LipSyncResult.video_id == video.id, LipSyncResult.is_current.is_(True))):
            if not seg.after_key:
                continue
            start_ms = seg.window_start_ms or 0
            end_ms = seg.window_end_ms or (start_ms + 1000)
            db.add(
                TimelineClip(
                    id=new_id(), timeline_id=timeline.id, track_id=lip_track.id, source_type=SOURCE_LIP_SYNC,
                    source_id=seg.id, storage_key=seg.after_key, name=f"Lip sync {start_ms}-{end_ms}",
                    character_id=seg.character_id, character_name=_character_name(db, seg.character_id, None),
                    segment_id=seg.segment_id, language=seg.language, start_ms=start_ms,
                    duration_ms=max(end_ms - start_ms, 1), source_in_ms=0, source_duration_ms=end_ms - start_ms,
                    metadata_json={"lipsync_result_id": seg.id, "status": seg.status, "review": seg.review, "engine": seg.engine,
                                   "needs_regeneration": bool(seg.needs_regeneration)},
                )
            )


def _all_segments(db: Session, video_id: str) -> list[TranscriptSegment]:
    transcript = db.scalar(select(Transcript).where(Transcript.video_id == video_id).order_by(Transcript.version.desc(), Transcript.created_at.desc()))
    if transcript is None:
        return []
    return list(db.scalars(select(TranscriptSegment).where(TranscriptSegment.transcript_id == transcript.id).order_by(TranscriptSegment.position)))


def list_segments(db: Session, video_id: str) -> list[dict]:
    return [
        {
            "id": seg.id, "position": seg.position, "start_ms": seg.start_ms, "end_ms": seg.end_ms, "text": seg.text,
            "speaker": seg.speaker, "language": seg.language,
        }
        for seg in _all_segments(db, video_id)
    ]


def list_characters(db: Session, project_id: str) -> list[dict]:
    from ..models import CharacterVoiceProfile

    characters = db.scalars(select(Character).where(Character.project_id == project_id).order_by(Character.name))
    out = []
    for character in characters:
        profiles = db.scalars(
            select(CharacterVoiceProfile).where(
                CharacterVoiceProfile.character_id == character.id, CharacterVoiceProfile.active.is_(True)
            )
        )
        out.append({
            "id": character.id, "name": character.name, "source_label": character.source_label, "status": character.status,
            "voice_profiles": [
                {"profile_id": p.id, "language": p.language, "version": p.version, "engine": p.engine, "voice_id": p.voice_id,
                 "model_id": p.model_id, "locked": p.locked, "consent_status": p.consent_status}
                for p in profiles
            ],
        })
    return out


# ---------------------------------------------------------------------------
# Clip editing operations (Task 2)
# ---------------------------------------------------------------------------


def _get_clip(db: Session, clip_id: str) -> TimelineClip:
    clip = db.get(TimelineClip, clip_id)
    if clip is None:
        raise AppError(404, "CLIP_NOT_FOUND", "Timeline clip not found.")
    return clip


def _get_track(db: Session, track_id: str) -> TimelineTrack:
    track = db.get(TimelineTrack, track_id)
    if track is None:
        raise AppError(404, "TRACK_NOT_FOUND", "Timeline track not found.")
    return track


def _get_timeline(db: Session, timeline_id: str) -> TimelineProject:
    timeline = db.get(TimelineProject, timeline_id)
    if timeline is None:
        raise AppError(404, "TIMELINE_NOT_FOUND", "Timeline not found.")
    return timeline


def _check_unlocked(clip: TimelineClip) -> None:
    if clip.locked:
        raise AppError(409, "CLIP_LOCKED", "This clip is locked.")


def _check_track_unlocked(track: TimelineTrack) -> None:
    if track.locked:
        raise AppError(409, "TRACK_LOCKED", "This track is locked.")


def _source_bound(clip: TimelineClip) -> int | None:
    if clip.source_out_ms is not None:
        return clip.source_out_ms
    return clip.source_duration_ms


def add_clip(
    db: Session,
    timeline_id: str,
    track_id: str,
    *,
    source_type: str,
    storage_key: str | None,
    name: str,
    start_ms: int,
    duration_ms: int,
    source_in_ms: int = 0,
    source_out_ms: int | None = None,
    source_duration_ms: int | None = None,
    source_id: str | None = None,
    external_audio_id: str | None = None,
    character_id: str | None = None,
    segment_id: str | None = None,
    language: str | None = None,
    volume_db: float = 0.0,
    metadata: dict | None = None,
    action: str = "add_clip",
) -> TimelineClip:
    timeline = _get_timeline(db, timeline_id)
    track = _get_track(db, track_id)
    _check_track_unlocked(track)
    if duration_ms < MIN_CLIP_MS:
        raise AppError(422, "INVALID_CLIP", "Clip duration must be positive.")
    if start_ms < 0:
        raise AppError(422, "INVALID_CLIP", "Clip start cannot be negative.")
    clip = TimelineClip(
        id=new_id(), timeline_id=timeline_id, track_id=track_id, source_type=source_type, source_id=source_id,
        external_audio_id=external_audio_id, storage_key=storage_key, name=name[:255], character_id=character_id,
        character_name=_character_name(db, character_id, None), segment_id=segment_id, language=language,
        start_ms=int(start_ms), duration_ms=int(duration_ms), source_in_ms=int(source_in_ms), source_out_ms=source_out_ms,
        source_duration_ms=source_duration_ms, volume_db=volume_db, metadata_json=metadata or {},
    )
    db.add(clip)
    db.flush()
    _record(db, timeline, action)
    db.refresh(clip)
    return clip


def check_clip_refs(db: Session, timeline: TimelineProject, track_id: str, external_audio_id: str | None,
                    character_id: str | None) -> None:
    """Ids in a clip request must belong to this timeline / project."""
    track = db.get(TimelineTrack, track_id)
    if track is None or track.timeline_id != timeline.id:
        raise AppError(404, "TRACK_NOT_FOUND", "Track not found in this timeline.")
    if external_audio_id:
        from ..models_timeline import ExternalAudio

        row = db.get(ExternalAudio, external_audio_id)
        if row is None or row.project_id != timeline.project_id:
            raise AppError(404, "EXTERNAL_AUDIO_NOT_FOUND", "External audio not found in this project.")
    if character_id:
        from ..models import Character

        row = db.get(Character, character_id)
        if row is None or row.project_id != timeline.project_id:
            raise AppError(404, "CHARACTER_NOT_FOUND", "Character not found in this project.")


def move_clip(db: Session, clip_id: str, start_ms: int, track_id: str | None = None, snap_ms: int = 0) -> TimelineClip:
    clip = _get_clip(db, clip_id)
    _check_unlocked(clip)
    timeline = _get_timeline(db, clip.timeline_id)
    target_track = _get_track(db, track_id) if track_id else _get_track(db, clip.track_id)
    _check_track_unlocked(target_track)
    if target_track.kind != _get_track(db, clip.track_id).kind and target_track.kind == "video":
        raise AppError(422, "INVALID_TRACK", "Audio clips cannot be moved onto the video track.")
    new_start = snap_value(start_ms, snap_ms, _snap_targets(db, target_track.id, clip.id))
    timing_changed = new_start != clip.start_ms or target_track.id != clip.track_id
    clip.start_ms = new_start
    clip.track_id = target_track.id
    clip.updated_at = utcnow()
    db.flush()
    if timing_changed:
        mark_lip_sync_stale_for_clip(db, clip, "dialogue timing changed (clip moved)")
    _record(db, timeline, "move_clip")
    db.refresh(clip)
    return clip


def trim_clip(
    db: Session,
    clip_id: str,
    *,
    source_in_ms: int | None = None,
    source_out_ms: int | None = None,
    start_ms: int | None = None,
    duration_ms: int | None = None,
    snap_ms: int = 0,
) -> TimelineClip:
    """Non-destructive trim. Source file is never modified.

    Any subset may be given explicitly; invariants enforced: source_in >= 0,
    source_out > source_in, source_out <= source_duration, duration >= 1.
    """
    clip = _get_clip(db, clip_id)
    _check_unlocked(clip)
    timeline = _get_timeline(db, clip.timeline_id)
    orig_start, orig_dur = clip.start_ms, clip.duration_ms
    orig_in = clip.source_in_ms
    orig_out = _source_bound(clip)

    new_in = orig_in if source_in_ms is None else int(source_in_ms)
    new_out = orig_out if source_out_ms is None else int(source_out_ms)
    new_start = orig_start if start_ms is None else max(0, int(start_ms))
    new_duration = orig_dur if duration_ms is None else int(duration_ms)

    if duration_ms is not None and source_out_ms is None and new_out is not None:
        new_out = new_in + new_duration

    if new_in < 0:
        raise AppError(422, "INVALID_TRIM", "Trim start cannot be negative.")
    if new_duration < MIN_CLIP_MS:
        raise AppError(422, "INVALID_TRIM", "Clip must keep at least 1 ms.")
    if new_out is not None and new_out <= new_in:
        raise AppError(422, "INVALID_TRIM", "Trim end must be after trim start.")
    if clip.source_duration_ms is not None and new_out is not None and new_out > clip.source_duration_ms + 1:
        raise AppError(422, "INVALID_TRIM", "Trim end cannot exceed the source duration.")
    if clip.source_duration_ms is not None and new_in > clip.source_duration_ms:
        raise AppError(422, "INVALID_TRIM", "Trim start cannot exceed the source duration.")

    targets = _snap_targets(db, clip.track_id, clip.id)
    new_start = snap_value(new_start, snap_ms, targets)
    new_duration = max(MIN_CLIP_MS, int(round(new_duration)))

    changed = (new_in != orig_in) or (new_out != orig_out) or (new_start != orig_start) or (new_duration != orig_dur)
    clip.source_in_ms = new_in
    clip.source_out_ms = new_out
    clip.start_ms = max(0, new_start)
    clip.duration_ms = new_duration
    clip.fade_in_ms = min(clip.fade_in_ms, new_duration)
    clip.fade_out_ms = min(clip.fade_out_ms, new_duration)
    clip.updated_at = utcnow()
    db.flush()
    if changed:
        mark_lip_sync_stale_for_clip(db, clip, "dialogue timing changed (clip trimmed)")
    _record(db, timeline, "trim_clip")
    db.refresh(clip)
    return clip


def trim_start(db: Session, clip_id: str, delta_ms: int, snap_ms: int = 0) -> TimelineClip:
    """Trim the start edge by delta_ms (positive = remove from front). Keeps right edge."""
    clip = _get_clip(db, clip_id)
    _check_unlocked(clip)
    new_in = clip.source_in_ms + int(delta_ms)
    source_bound = _source_bound(clip)
    max_in = (source_bound - MIN_CLIP_MS) if source_bound is not None else None
    if new_in < 0:
        raise AppError(422, "INVALID_TRIM", "Trim start cannot be negative.")
    if max_in is not None and new_in > max_in:
        raise AppError(422, "INVALID_TRIM", "Clip must keep at least 1 ms.")
    shift = int(delta_ms)
    new_start = max(0, clip.start_ms + shift)
    new_duration = (clip.start_ms + clip.duration_ms) - new_start
    if new_duration < MIN_CLIP_MS:
        raise AppError(422, "INVALID_TRIM", "Clip must keep at least 1 ms.")
    return trim_clip(db, clip_id, source_in_ms=new_in, start_ms=new_start, duration_ms=new_duration, snap_ms=snap_ms)


def trim_end(db: Session, clip_id: str, delta_ms: int, snap_ms: int = 0) -> TimelineClip:
    """Trim the end edge by delta_ms (positive = remove from end). Keeps left edge."""
    clip = _get_clip(db, clip_id)
    _check_unlocked(clip)
    source_bound = _source_bound(clip)
    new_duration = clip.duration_ms - int(delta_ms)
    if new_duration < MIN_CLIP_MS:
        raise AppError(422, "INVALID_TRIM", "Clip must keep at least 1 ms.")
    if source_bound is not None and clip.source_in_ms + new_duration > source_bound:
        new_duration = source_bound - clip.source_in_ms
        if new_duration < MIN_CLIP_MS:
            raise AppError(422, "INVALID_TRIM", "Clip must keep at least 1 ms.")
    new_out = clip.source_in_ms + new_duration if source_bound is None else min(source_bound, clip.source_in_ms + new_duration)
    return trim_clip(db, clip_id, source_out_ms=new_out, duration_ms=new_duration, snap_ms=snap_ms)


def split_clip(db: Session, clip_id: str, at_ms: int, snap_ms: int = 0) -> tuple[TimelineClip, TimelineClip]:
    clip = _get_clip(db, clip_id)
    _check_unlocked(clip)
    timeline = _get_timeline(db, clip.timeline_id)
    at = snap_value(at_ms, snap_ms, [clip.start_ms, clip.start_ms + clip.duration_ms])
    if at <= clip.start_ms or at >= clip.start_ms + clip.duration_ms:
        raise AppError(422, "INVALID_SPLIT", "Split point must be inside the clip.")
    orig_start, orig_dur = clip.start_ms, clip.duration_ms
    orig_in, orig_out = clip.source_in_ms, clip.source_out_ms
    orig_fade_in, orig_fade_out = clip.fade_in_ms, clip.fade_out_ms
    left_len = at - orig_start
    right_len = orig_dur - left_len
    # left
    clip.duration_ms = left_len
    clip.fade_out_ms = 0
    # right
    right = TimelineClip(
        id=new_id(), timeline_id=clip.timeline_id, track_id=clip.track_id, source_type=clip.source_type,
        source_id=clip.source_id, external_audio_id=clip.external_audio_id, storage_key=clip.storage_key,
        name=clip.name, character_id=clip.character_id, character_name=clip.character_name, segment_id=clip.segment_id,
        language=clip.language, start_ms=at, duration_ms=right_len, source_in_ms=orig_in + left_len,
        source_out_ms=orig_out, source_duration_ms=clip.source_duration_ms, volume_db=clip.volume_db,
        fade_in_ms=0, fade_out_ms=orig_fade_out, muted=clip.muted, locked=False,
        replaces_clip_id=clip.replaces_clip_id, metadata_json=dict(clip.metadata_json or {}),
    )
    clip.fade_in_ms = orig_fade_in
    right.fade_in_ms = 0
    db.add(right)
    db.flush()
    # lip sync timing changed for both halves
    mark_lip_sync_stale_for_clip(db, clip, "dialogue timing changed (clip split)")
    db.flush()
    _record(db, timeline, "split_clip")
    db.refresh(clip)
    db.refresh(right)
    return clip, right


def join_clips(db: Session, clip_ids: list[str]) -> TimelineClip:
    if len(clip_ids) < 2:
        raise AppError(422, "INVALID_JOIN", "Provide at least two clips to join.")
    clips = [_get_clip(db, cid) for cid in clip_ids]
    for clip in clips:
        _check_unlocked(clip)
    timeline = _get_timeline(db, clips[0].timeline_id)
    track_id = clips[0].track_id
    if any(clip.track_id != track_id for clip in clips):
        raise AppError(422, "INVALID_JOIN", "Clips must be on the same track.")
    clips.sort(key=lambda item: item.start_ms)
    # adjacency + same source + source continuity
    for a, b in zip(clips, clips[1:]):
        if a.start_ms + a.duration_ms != b.start_ms:
            raise AppError(422, "INVALID_JOIN", "Clips are not adjacent in time.")
        if a.source_type != b.source_type or a.storage_key != b.storage_key or a.source_id != b.source_id or a.external_audio_id != b.external_audio_id:
            raise AppError(422, "INVALID_JOIN", "Clips must share the same source to join.")
        a_end_src = a.source_in_ms + a.duration_ms
        if b.source_in_ms != a_end_src:
            raise AppError(422, "INVALID_JOIN", "Source ranges are not contiguous.")
        if abs(a.volume_db - b.volume_db) > 0.01 or a.muted != b.muted:
            raise AppError(422, "INVALID_JOIN", "Clips must have the same volume to join.")
    first, last = clips[0], clips[-1]
    first.duration_ms = (last.start_ms + last.duration_ms) - first.start_ms
    first.source_out_ms = last.source_out_ms
    first.fade_out_ms = last.fade_out_ms
    first.updated_at = utcnow()
    for clip in clips[1:]:
        db.delete(clip)
    db.flush()
    mark_lip_sync_stale_for_clip(db, first, "dialogue timing changed (clips joined)")
    _record(db, timeline, "join_clips")
    db.refresh(first)
    return first


def delete_clip(db: Session, clip_id: str) -> None:
    clip = _get_clip(db, clip_id)
    _check_unlocked(clip)
    timeline = _get_timeline(db, clip.timeline_id)
    mark_lip_sync_stale_for_clip(db, clip, "dubbed audio removed")
    db.delete(clip)
    db.flush()
    _record(db, timeline, "delete_clip")


def duplicate_clip(db: Session, clip_id: str, start_ms: int | None = None) -> TimelineClip:
    clip = _get_clip(db, clip_id)
    timeline = _get_timeline(db, clip.timeline_id)
    clone = TimelineClip(
        id=new_id(), timeline_id=clip.timeline_id, track_id=clip.track_id, source_type=clip.source_type,
        source_id=clip.source_id, external_audio_id=clip.external_audio_id, storage_key=clip.storage_key,
        name=clip.name, character_id=clip.character_id, character_name=clip.character_name, segment_id=clip.segment_id,
        language=clip.language, start_ms=clip.start_ms + clip.duration_ms if start_ms is None else max(0, int(start_ms)),
        duration_ms=clip.duration_ms, source_in_ms=clip.source_in_ms, source_out_ms=clip.source_out_ms,
        source_duration_ms=clip.source_duration_ms, volume_db=clip.volume_db, fade_in_ms=clip.fade_in_ms,
        fade_out_ms=clip.fade_out_ms, muted=clip.muted, locked=False, replaces_clip_id=None,
        metadata_json=dict(clip.metadata_json or {}) | {"duplicated_from": clip.id},
    )
    db.add(clone)
    db.flush()
    _record(db, timeline, "duplicate_clip")
    db.refresh(clone)
    return clone


def set_clip_props(
    db: Session,
    clip_id: str,
    *,
    name: str | None = None,
    volume_db: float | None = None,
    fade_in_ms: int | None = None,
    fade_out_ms: int | None = None,
    muted: bool | None = None,
    locked: bool | None = None,
    action: str = "update_clip",
) -> TimelineClip:
    clip = _get_clip(db, clip_id)
    _check_unlocked(clip)
    timeline = _get_timeline(db, clip.timeline_id)
    if name is not None:
        clip.name = name[:255]
    if volume_db is not None:
        if not math.isfinite(volume_db):
            raise AppError(422, "INVALID_VOLUME", "Volume must be a finite number.")
        clip.volume_db = max(-60.0, min(24.0, float(volume_db)))
    if fade_in_ms is not None:
        clip.fade_in_ms = max(0, min(int(fade_in_ms), clip.duration_ms))
    if fade_out_ms is not None:
        clip.fade_out_ms = max(0, min(int(fade_out_ms), clip.duration_ms))
    if muted is not None:
        clip.muted = bool(muted)
    if locked is not None:
        clip.locked = bool(locked)
    clip.updated_at = utcnow()
    db.flush()
    _record(db, timeline, action)
    db.refresh(clip)
    return clip


def reassign_character(db: Session, clip_id: str, character_id: str | None) -> TimelineClip:
    clip = _get_clip(db, clip_id)
    _check_unlocked(clip)
    timeline = _get_timeline(db, clip.timeline_id)
    video = db.get(Video, timeline.video_id)
    if character_id is not None:
        character = db.get(Character, character_id)
        if character is None or (video is not None and character.project_id != timeline.project_id):
            raise AppError(404, "CHARACTER_NOT_FOUND", "Character not found in this project.")
    meta = dict(clip.metadata_json or {})
    meta["voice_profile"] = _voice_profile_snapshot(db, character_id, clip.language)
    changed = character_id != clip.character_id
    clip.character_id = character_id
    clip.character_name = _character_name(db, character_id, None)
    clip.metadata_json = meta
    clip.updated_at = utcnow()
    db.flush()
    if changed:
        mark_lip_sync_stale_for_clip(db, clip, "character reassigned")
    _record(db, timeline, "reassign_character")
    db.refresh(clip)
    return clip


def assign_segment(db: Session, clip_id: str, segment_id: str | None) -> TimelineClip:
    clip = _get_clip(db, clip_id)
    _check_unlocked(clip)
    timeline = _get_timeline(db, clip.timeline_id)
    if segment_id is not None and db.get(TranscriptSegment, segment_id) is None:
        raise AppError(404, "SEGMENT_NOT_FOUND", "Dialogue segment not found.")
    changed = segment_id != clip.segment_id
    clip.segment_id = segment_id
    clip.updated_at = utcnow()
    db.flush()
    if changed:
        mark_lip_sync_stale_for_clip(db, clip, "dialogue segment reassigned")
    _record(db, timeline, "assign_segment")
    db.refresh(clip)
    return clip


# ---------------------------------------------------------------------------
# Track operations
# ---------------------------------------------------------------------------


def add_track(db: Session, timeline_id: str, kind: str, name: str, language: str | None = None) -> TimelineTrack:
    timeline = _get_timeline(db, timeline_id)
    if kind not in TRACK_KINDS:
        raise AppError(422, "INVALID_TRACK_KIND", f"Track kind must be one of {', '.join(TRACK_KINDS)}.")
    position = (db.scalar(select(TimelineTrack.position).where(TimelineTrack.timeline_id == timeline_id).order_by(TimelineTrack.position.desc()).limit(1)) or 0) + 1
    track = TimelineTrack(
        id=new_id(), timeline_id=timeline_id, kind=kind, name=name[:120], language=language,
        color=TRACK_COLORS.get(kind, "#60a5fa"), position=position, enabled=True, muted=False, solo=False, locked=False,
        volume_db=0.0, pan=0.0,
    )
    db.add(track)
    db.flush()
    _record(db, timeline, "add_track")
    db.refresh(track)
    return track


def set_track_props(
    db: Session,
    track_id: str,
    *,
    name: str | None = None,
    enabled: bool | None = None,
    muted: bool | None = None,
    solo: bool | None = None,
    locked: bool | None = None,
    volume_db: float | None = None,
    pan: float | None = None,
    language: str | None = None,
) -> TimelineTrack:
    track = _get_track(db, track_id)
    timeline = _get_timeline(db, track.timeline_id)
    if name is not None:
        track.name = name[:120]
    if enabled is not None:
        track.enabled = bool(enabled)
    if muted is not None:
        track.muted = bool(muted)
    if solo is not None:
        track.solo = bool(solo)
    if locked is not None:
        track.locked = bool(locked)
    if volume_db is not None:
        track.volume_db = max(-60.0, min(24.0, float(volume_db)))
    if pan is not None:
        track.pan = max(-1.0, min(1.0, float(pan)))
    if language is not None:
        track.language = language
    track.updated_at = utcnow()
    db.flush()
    _record(db, timeline, "update_track")
    db.refresh(track)
    return track


def remove_track(db: Session, track_id: str) -> None:
    track = _get_track(db, track_id)
    timeline = _get_timeline(db, track.timeline_id)
    db.delete(track)
    db.flush()
    _record(db, timeline, "remove_track")


# ---------------------------------------------------------------------------
# External audio (Task 3)
# ---------------------------------------------------------------------------


def add_external_audio_clip(
    db: Session,
    timeline_id: str,
    *,
    external_audio_id: str,
    start_ms: int,
    track_id: str | None = None,
    source_in_ms: int = 0,
    source_out_ms: int | None = None,
    character_id: str | None = None,
    segment_id: str | None = None,
    volume_db: float = 0.0,
    name: str | None = None,
    replace_clip_id: str | None = None,
    snap_ms: int = 0,
) -> TimelineClip:
    external = db.get(ExternalAudio, external_audio_id)
    if external is None:
        raise AppError(404, "AUDIO_IMPORT_NOT_FOUND", "External audio import not found.")
    timeline = _get_timeline(db, timeline_id)
    if external.project_id != timeline.project_id:
        raise AppError(403, "WRONG_PROJECT", "External audio belongs to a different project.")

    # Replace generated AI dialogue with external audio (non-destructive).
    replaced: TimelineClip | None = None
    if replace_clip_id:
        replaced = _get_clip(db, replace_clip_id)
        _check_unlocked(replaced)
        if track_id is None:
            track_id = replaced.track_id
        # place external at the replaced clip's position
        start_ms = replaced.start_ms
        if not name:
            name = replaced.name
        if character_id is None:
            character_id = replaced.character_id
        if segment_id is None:
            segment_id = replaced.segment_id

    track = _get_track(db, track_id) if track_id else _external_track(db, timeline)
    _check_track_unlocked(track)
    targets = _snap_targets(db, track.id)
    new_start = snap_value(start_ms, snap_ms, targets)
    source_len = external.duration_ms
    out = source_out_ms if source_out_ms is not None else source_len
    if source_len is not None and out is not None:
        out = min(out, source_len)  # never trim beyond the imported audio
    if out is not None and source_in_ms >= out:
        raise AppError(422, "INVALID_TRIM", "Trim end must be after trim start.")
    duration = (out - source_in_ms) if out is not None else max(external.duration_ms or 1000, 1)
    duration = max(MIN_CLIP_MS, int(duration))

    clip = TimelineClip(
        id=new_id(), timeline_id=timeline.id, track_id=track.id, source_type=SOURCE_EXTERNAL_AUDIO,
        source_id=external.id, external_audio_id=external.id, storage_key=external.storage_key,
        name=(name or external.original_filename)[:255], character_id=character_id,
        character_name=_character_name(db, character_id, None), segment_id=segment_id, language=track.language,
        start_ms=max(0, int(new_start)), duration_ms=duration, source_in_ms=max(0, int(source_in_ms)),
        source_out_ms=int(out) if out is not None else None, source_duration_ms=external.duration_ms,
        volume_db=volume_db, metadata_json={"external": {"filename": external.original_filename, "format": external.format,
                                                        "checksum_sha256": external.checksum_sha256, "import_id": external.id}},
    )
    db.add(clip)
    db.flush()

    if replaced is not None:
        # mute + mark replaced generated dialogue (keep it, never delete source)
        replaced.muted = True
        meta = dict(replaced.metadata_json or {})
        meta["replaced_by_clip_id"] = clip.id
        meta["replaced_by_external_audio_id"] = external.id
        replaced.metadata_json = meta
        clip.replaces_clip_id = replaced.id
        mark_lip_sync_stale_for_clip(db, replaced, "dubbed audio replaced with external audio")
        db.flush()

    _record(db, timeline, "add_external_audio" if replaced is None else "replace_with_external")
    db.refresh(clip)
    return clip


def _external_track(db: Session, timeline: TimelineProject) -> TimelineTrack:
    track = db.scalar(select(TimelineTrack).where(TimelineTrack.timeline_id == timeline.id, TimelineTrack.kind == "external_audio").order_by(TimelineTrack.position).limit(1))
    if track is None:
        track = add_track(db, timeline.id, "external_audio", "External Audio")
    return track


# ---------------------------------------------------------------------------
# Lip sync (Task 5, Phase 13)
# ---------------------------------------------------------------------------


def _lipsync_model():
    from ..models_lipsync import LipSyncResult

    return LipSyncResult


def serialize_lipsync(db: Session, row) -> dict:
    """Phase 13 LipSyncResult + Phase 14 invalidation. Never silently 'synced' when outdated."""
    needs = bool(getattr(row, "needs_regeneration", False))
    has_output = bool(getattr(row, "after_key", None))
    # recompute staleness vs current dubbed audio reference if we can
    synced = (not needs) and has_output and row.status != "failed"
    return {
        "id": row.id,
        "result_id": row.id,
        "video_id": row.video_id,
        "segment_id": row.segment_id,
        "language": row.language,
        "character_id": row.character_id,
        "start_ms": row.window_start_ms,
        "end_ms": row.window_end_ms,
        "window_start_ms": row.window_start_ms,
        "window_end_ms": row.window_end_ms,
        "attempt": row.attempt,
        "is_current": row.is_current,
        "status": row.status,
        "review": row.review,
        "engine": row.engine,
        "model": row.model,
        "audio_source": row.audio_source,
        "audio_ref": row.audio_ref,
        "after_url": f"/api/v1/lipsync/results/{row.id}/video?variant=after" if has_output else None,
        "before_url": f"/api/v1/lipsync/results/{row.id}/video?variant=before" if row.before_key else None,
        "output_url": f"/api/v1/lipsync/results/{row.id}/video?variant=after" if has_output else None,
        # Task 5
        "synced": synced,  # outdated output is never silently treated as synchronized
        "needs_regeneration": needs or not has_output,
        "regeneration_reason": getattr(row, "regeneration_reason", None),
        "dubbed_fingerprint": getattr(row, "dubbed_fingerprint", None),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_lipsync(db: Session, video_id: str, language: str | None = None) -> list[dict]:
    LipSyncResult = _lipsync_model()
    query = select(LipSyncResult).where(LipSyncResult.video_id == video_id, LipSyncResult.is_current.is_(True))
    if language:
        query = query.where(LipSyncResult.language == language)
    rows = db.scalars(query.order_by(LipSyncResult.created_at))
    return [serialize_lipsync(db, row) for row in rows]


def mark_lip_sync_stale_for_clip(db: Session, clip: TimelineClip, reason: str) -> int:
    """Flag Phase 13 lip-sync segments linked to this dubbed clip as needing regeneration."""
    if not clip.segment_id:
        return 0
    LipSyncResult = _lipsync_model()
    timeline = db.get(TimelineProject, clip.timeline_id)
    video_id = timeline.video_id if timeline else None
    query = select(LipSyncResult).where(LipSyncResult.segment_id == clip.segment_id, LipSyncResult.is_current.is_(True))
    if video_id:
        query = query.where(LipSyncResult.video_id == video_id)
    changed = 0
    for row in db.scalars(query):
        # only flag lip sync whose language matches the edited dubbed audio (or is unlabelled)
        if clip.language and row.language and clip.language != row.language:
            continue
        if not getattr(row, "needs_regeneration", False) or row.regeneration_reason != reason[:255]:
            row.needs_regeneration = True
            row.regeneration_reason = reason[:255]
            row.updated_at = utcnow()
            changed += 1
    if changed:
        db.flush()
    return changed


def mark_lip_sync_regenerated(db: Session, result_id: str, output_storage_key: str | None = None) -> object:
    LipSyncResult = _lipsync_model()
    row = db.get(LipSyncResult, result_id)
    if row is None:
        raise AppError(404, "LIPSYNC_NOT_FOUND", "Lip-sync segment not found.")
    row.needs_regeneration = False
    row.regeneration_reason = None
    if output_storage_key:
        row.after_key = output_storage_key
    db.commit()
    db.refresh(row)
    return row


def get_lipsync(db: Session, result_id: str):
    LipSyncResult = _lipsync_model()
    row = db.get(LipSyncResult, result_id)
    if row is None:
        raise AppError(404, "LIPSYNC_NOT_FOUND", "Lip-sync segment not found.")
    return row


# ---------------------------------------------------------------------------
# Export workflow (Task 6)
# ---------------------------------------------------------------------------


def _selected_language_tracks(tracks: list[TimelineTrack], languages: list[str] | None) -> list[TimelineTrack]:
    lang_tracks = [t for t in tracks if t.kind in LANGUAGE_TRACK_KINDS and t.enabled and not t.muted]
    if languages:
        wanted = set(languages)
        lang_tracks = [t for t in lang_tracks if (t.language or LANGUAGE_TRACK_KINDS[t.kind]) in wanted or t.kind in wanted]
    return sorted(lang_tracks, key=lambda t: t.position)


def _bed_tracks(tracks: list[TimelineTrack]) -> list[TimelineTrack]:
    kinds = {"background_music", "sound_effects", "ambience", "external_audio"}
    return [t for t in tracks if t.kind in kinds and t.enabled and not t.muted]


def export_plan(
    db: Session,
    timeline: TimelineProject,
    *,
    mode: str = "multitrack",  # multitrack | separate_language
    container: str = "mkv",
    languages: list[str] | None = None,
    include_beds: bool = True,
    include_original_english: bool | None = None,
) -> dict:
    tracks = _tracks(db, timeline.id)
    clips = _clips(db, timeline.id)
    clip_count = {track.id: 0 for track in tracks}
    for clip in clips:
        clip_count[clip.track_id] = clip_count.get(clip.track_id, 0) + 1

    # Original English is a language track too; explicit control to avoid accidental mixing.
    if mode not in ("multitrack", "separate_language"):
        raise AppError(422, "INVALID_MODE", "mode must be 'multitrack' or 'separate_language'.")

    lang_tracks = _selected_language_tracks(tracks, languages)
    if include_original_english is False:
        lang_tracks = [t for t in lang_tracks if t.kind != "original_english"]

    # guard: no accidental language combining
    selected_langs = [t.language or LANGUAGE_TRACK_KINDS[t.kind] for t in lang_tracks]
    video_track = next((t for t in tracks if t.kind == "video"), None)

    def track_info(track: TimelineTrack) -> dict:
        return {
            "track_id": track.id, "kind": track.kind, "name": track.name, "language": track.language,
            "stream_language": track.language or LANGUAGE_TRACK_KINDS.get(track.kind), "enabled": track.enabled,
            "muted": track.muted, "volume_db": track.volume_db, "clip_count": clip_count.get(track.id, 0),
        }

    beds = [track_info(t) for t in _bed_tracks(tracks)] if include_beds else []

    if mode == "multitrack":
        audio_streams = [track_info(t) for t in lang_tracks] + beds
        outputs = [{
            "kind": "multitrack",
            "container": container,
            "video": "copy original video stream; no re-encode",
            "audio_streams": audio_streams,  # separate selectable streams, languages never merged
            "language_count": len(lang_tracks),
            "notes": "One container with multiple selectable audio streams where container/player support it. Languages are never combined into a single stream.",
        }]
    else:
        outputs = []
        for track in lang_tracks:
            outputs.append({
                "kind": "separate_language",
                "language": track.language or LANGUAGE_TRACK_KINDS.get(track.kind),
                "track": track_info(track),
                "audio_streams": [track_info(track)] + beds,  # this language + beds; no other language
                "excluded_languages": [l for l in selected_langs if l != (track.language or LANGUAGE_TRACK_KINDS.get(track.kind))],
                "container": container,
                "suggested_filename_suffix": track.language or LANGUAGE_TRACK_KINDS.get(track.kind),
                "notes": "Separate video per language; no other dub language is included.",
            })

    return {
        "timeline_id": timeline.id,
        "mode": mode,
        "container": container,
        "duration_ms": timeline.duration_ms,
        "video_track": track_info(video_track) if video_track else None,
        "original_video_preserved": True,
        "languages_selected": selected_langs,
        "language_tracks": [track_info(t) for t in lang_tracks],
        "beds": beds,
        "outputs": outputs,
        "never_combines_languages": True,
    }


# ---------------------------------------------------------------------------
# Export rendering (backend; no GPU) ---------------------------------------
# ---------------------------------------------------------------------------


def render_track_wav(db: Session, timeline: TimelineProject, track: TimelineTrack, out_path: Path, sr: int = SAMPLE_RATE, check_active=None) -> Path:
    """Render one track to a full-length mono WAV, streaming in blocks."""
    clips = sorted(
        (clip for clip in _clips(db, timeline.id) if clip.track_id == track.id and not clip.muted and not (clip.source_type == SOURCE_VIDEO and track.kind == "video")),
        key=lambda clip: clip.start_ms,
    )
    total_ms = max([timeline.duration_ms or 0] + [clip.start_ms + clip.duration_ms for clip in clips]) + 500
    total = int(sr * total_ms / 1000)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    track_gain = math.pow(10, (track.volume_db or 0.0) / 20)
    cache: dict[str, np.ndarray | None] = {}
    block = sr * 30
    with wave.open(str(out_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        for start in range(0, total, block):
            if check_active:
                check_active()
            stop = min(total, start + block)
            buffer = np.zeros(stop - start, dtype=np.float32)
            for clip in clips:
                clip_start = int(sr * clip.start_ms / 1000)
                if clip_start >= stop:
                    break
                key = clip.id
                if key not in cache:
                    cache[key] = _clip_audio(db, clip, sr)
                audio = cache[key]
                if audio is None:
                    continue
                a = max(start, clip_start)
                b = min(stop, clip_start + len(audio))
                if b > a:
                    buffer[a - start : b - start] += audio[a - clip_start : b - clip_start]
            buffer *= track_gain
            pcm = (np.clip(buffer, -1, 1) * 32767).astype("<i2")
            handle.writeframes(pcm.tobytes())
    return out_path


def _clip_audio(db: Session, clip: TimelineClip, sr: int) -> np.ndarray | None:
    path = _clip_source_path(db, clip)
    if path is None:
        return None
    source_in = clip.source_in_ms
    source_out = clip.source_out_ms if clip.source_out_ms is not None else clip.source_duration_ms
    audio = decode_audio(path, sr=sr, start_ms=source_in, end_ms=source_out)
    if audio.size == 0:
        return None
    # trim/pad to clip duration
    want = int(sr * clip.duration_ms / 1000)
    if audio.size > want:
        audio = audio[:want]
    # volume
    audio = audio * math.pow(10, (clip.volume_db or 0.0) / 20)
    # fades (seconds)
    n = audio.size
    if clip.fade_in_ms > 0:
        fade_len = min(n, int(sr * clip.fade_in_ms / 1000))
        if fade_len > 0:
            audio[:fade_len] *= np.linspace(0, 1, fade_len, dtype=np.float32)
    if clip.fade_out_ms > 0:
        fade_len = min(n, int(sr * clip.fade_out_ms / 1000))
        if fade_len > 0:
            audio[n - fade_len :] *= np.linspace(1, 0, fade_len, dtype=np.float32)
    return np.clip(audio, -1, 1).astype(np.float32)


def create_export_job(db: Session, timeline: TimelineProject, video: Video, options: dict) -> ProcessingJob:
    from .jobs import create_import_job

    return create_import_job(db, timeline.project_id, video.id, "timeline_export", EXPORT_STEP_NAMES, options)


def run_export_job(job_id: str, timeline_id: str, options: dict | None = None) -> None:
    from sqlalchemy import update

    from .jobs import JobCancelled, _job_status, _set_step

    db = SessionLocal()
    try:
        job = db.get(ProcessingJob, job_id)
        timeline = db.get(TimelineProject, timeline_id)
        if not job or not timeline:
            return
        opts = options or job.options_json or {}
        video = db.get(Video, timeline.video_id)
        if video is None:
            raise AppError(404, "NOT_FOUND", "Video not found")

        def check_active() -> None:
            status = _job_status(job_id)
            if status is None or status == "cancelled":
                raise JobCancelled("Job was cancelled or removed")

        claim = db.execute(update(ProcessingJob).where(ProcessingJob.id == job_id, ProcessingJob.status == "queued").values(status="running"))
        db.commit()
        if claim.rowcount != 1:
            db.refresh(job)
            if job.status != "cancelled":
                return
            raise JobCancelled("Job was cancelled before it started")

        from . import final_export

        mode = opts.get("mode", "multitrack")
        container = str(opts.get("container", "mkv")).lower()
        if container not in final_export.ALLOWED_CONTAINERS:
            raise AppError(422, "INVALID_CONTAINER", "Container must be mkv, mp4 or mov.")
        use_lipsync = bool(opts.get("use_lipsync", True))
        multitrack_lipsync = opts.get("multitrack_lipsync_language")
        languages = opts.get("languages")
        include_beds = bool(opts.get("include_beds", True))
        out_dir_key = f"projects/{timeline.project_id}/timeline_exports/{job_id}"
        out_dir = storage.absolute_path(out_dir_key)
        out_dir.mkdir(parents=True, exist_ok=True)

        _set_step(db, job, "collect_tracks", "running", "Collecting tracks", 5)
        plan = export_plan(db, timeline, mode=mode, container=container, languages=languages, include_beds=include_beds)
        tracks = {track.id: track for track in _tracks(db, timeline.id)}
        video_path = storage.absolute_path(video.storage_key)
        _set_step(db, job, "collect_tracks", "completed", f"{len(plan['outputs'])} output(s)", 10)

        _set_step(db, job, "render_tracks", "running", "Rendering audio tracks", 15)
        outputs_meta: list[dict] = []
        duration_s = (video.duration_ms or timeline.duration_ms or 0) / 1000
        lipsync_used: dict[str, dict] = {}

        def picture_for(language: str | None, tmp_dir: Path) -> tuple[Path, bool]:
            """(video file, re-encoded?) — the lip-synced picture for ``language`` when available."""
            if not use_lipsync or not language or language in {"en", "original", "original_english"}:
                return video_path, False
            if language in lipsync_used:
                info = lipsync_used[language]
                return (Path(info["path"]), True) if info.get("path") else (video_path, False)
            composed = final_export.compose_lipsync_video(db, video, language, tmp_dir / f"picture_{language}.mp4", check_active)
            lipsync_used[language] = {"path": str(composed.path) if composed.path else None, "clips": composed.clips,
                                      "skipped": composed.skipped}
            return (composed.path, True) if composed.path else (video_path, False)

        def mux(picture: Path, reencoded: bool, wavs: list[Path], infos: list[dict], out_path: Path, default_index: int = 0) -> dict:
            command = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(picture)]
            for wav in wavs:
                command += ["-i", str(wav)]
            command += ["-map", "0:v:0", "-c:v", "copy"]
            for idx, info in enumerate(infos[: len(wavs)]):
                lang = final_export.iso639_2(info.get("stream_language"))
                command += ["-map", f"{idx + 1}:a:0", f"-metadata:s:a:{idx}", f"language={lang}",
                            f"-metadata:s:a:{idx}", f"title={info.get('name') or f'Audio {idx + 1}'}",
                            # MP4/MOV keep a track's name in its handler box; players (VLC, QuickTime) show it.
                            f"-metadata:s:a:{idx}", f"handler_name={info.get('name') or f'Audio {idx + 1}'}",
                            f"-disposition:a:{idx}", "default" if idx == default_index else "0"]
            command += ["-c:a", "aac", "-b:a", "192k"]
            if duration_s > 0:
                command += ["-t", f"{duration_s:.3f}"]  # exact film length (no -shortest truncation)
            if container in {"mp4", "mov"}:
                command += ["-movflags", "+faststart"]
            command.append(str(out_path))
            result = subprocess.run(command, capture_output=True, check=False)
            if result.returncode != 0:
                raise AppError(500, "EXPORT_MUX_FAILED", (result.stderr or b"").decode(errors="ignore")[-500:])
            probe = final_export.probe_streams(out_path)
            playable = final_export.decode_check(out_path, seconds=min(duration_s, 30.0) if duration_s else 30.0)
            problems = []
            if not playable:
                problems.append("the file does not decode")
            if duration_s and probe["duration_s"] is not None and abs(probe["duration_s"] - duration_s) > 0.1:
                problems.append(f"duration {probe['duration_s']}s differs from the film's {duration_s:.3f}s")
            if problems:
                raise AppError(500, "EXPORT_VALIDATION_FAILED", "; ".join(problems))
            return {"video": "lip-synced picture re-encoded (H.264 CRF 18)" if reencoded else "original video stream copied (no re-encode)",
                    "streams": probe["streams"], "titles": probe["titles"], "duration_s": probe["duration_s"],
                    "size_bytes": out_path.stat().st_size, "validated": True}

        with tempfile.TemporaryDirectory(prefix="timeline-export-") as tmp:
            tmp_dir = Path(tmp)
            # group: multitrack -> all streams in one output; separate -> per language
            def render_streams(stream_infos: list[dict], prefix: str) -> list[Path]:
                rendered: list[Path] = []
                for info in stream_infos:
                    check_active()
                    track = tracks.get(info["track_id"])
                    if track is None:
                        continue
                    wav_path = tmp_dir / f"{prefix}_{track.id}.wav"
                    render_track_wav(db, timeline, track, wav_path, check_active=check_active)
                    rendered.append(wav_path)
                return rendered

            if mode == "multitrack":
                stream_infos = plan["outputs"][0]["audio_streams"] if plan["outputs"] else []
                rendered = render_streams(stream_infos, "stream")
                _set_step(db, job, "render_tracks", "completed", f"{len(rendered)} streams", 55)
                _set_step(db, job, "mux_outputs", "running", "Muxing container", 60)
                if not video_path.is_file():
                    raise AppError(410, "VIDEO_MISSING", "Original video file is missing.")
                picture, reencoded = picture_for(multitrack_lipsync, tmp_dir)
                out_path = out_dir / f"multitrack.{container}"
                meta = mux(picture, reencoded, rendered, stream_infos, out_path)
                outputs_meta.append({"kind": "multitrack", "path": out_path.name, "storage_key": f"{out_dir_key}/multitrack.{container}",
                                     "audio_streams": len(rendered), "lipsync_language": multitrack_lipsync if reencoded else None, **meta})
            else:
                outputs = plan["outputs"]
                rendered_cache: dict[str, Path] = {}
                for index, output in enumerate(outputs):
                    check_active()
                    lang = output.get("language") or f"track{index}"
                    stream_infos = output["audio_streams"]
                    rendered: list[Path] = []
                    for info in stream_infos:
                        track = tracks.get(info["track_id"])
                        if track is None:
                            continue
                        if track.id in rendered_cache:
                            rendered.append(rendered_cache[track.id])
                            continue
                        wav_path = tmp_dir / f"{lang}_{track.id}.wav"
                        render_track_wav(db, timeline, track, wav_path, check_active=check_active)
                        rendered_cache[track.id] = wav_path
                        rendered.append(wav_path)
                    if not video_path.is_file():
                        raise AppError(410, "VIDEO_MISSING", "Original video file is missing.")
                    suffix = safe_name(str(output.get("suggested_filename_suffix") or lang))
                    out_path = out_dir / f"{safe_name(timeline.name)}_{suffix}.{container}"
                    picture, reencoded = picture_for(lang, tmp_dir)
                    stream_infos = [info for info in stream_infos if tracks.get(info["track_id"]) is not None]
                    meta = mux(picture, reencoded, rendered, stream_infos, out_path)
                    outputs_meta.append({"kind": "separate_language", "language": lang, "path": out_path.name,
                                         "storage_key": f"{out_dir_key}/{out_path.name}", "audio_streams": len(rendered),
                                         "lipsync": lipsync_used.get(lang, {}).get("clips", 0) if reencoded else 0, **meta})
                _set_step(db, job, "render_tracks", "completed", f"{len(outputs)} outputs", 55)
                _set_step(db, job, "mux_outputs", "completed", f"{len(outputs_meta)} file(s)", 90)

        if mode == "multitrack":
            _set_step(db, job, "mux_outputs", "completed", "Muxed", 90)
        _set_step(db, job, "finalize", "running", "Finalizing", 95)
        # store outputs on options_json for retrieval
        job.options_json = {**(job.options_json or {}), "outputs": outputs_meta,
                            "lipsync": {lang: {"clips": info["clips"], "skipped": info["skipped"]} for lang, info in lipsync_used.items()}}
        final = db.execute(
            update(ProcessingJob).where(ProcessingJob.id == job_id, ProcessingJob.status == "running").values(status="completed", current_step="finalize", progress=100)
        )
        if final.rowcount != 1:
            db.rollback()
            raise JobCancelled("Job was cancelled before finishing")
        db.commit()
        _set_step(db, job, "finalize", "completed", f"{len(outputs_meta)} output(s)", 100)
    except Exception as error:  # noqa: BLE001
        from .jobs import JobCancelled as _Cancelled

        db.rollback()
        current = db.get(ProcessingJob, job_id)
        if current is None:
            return
        if isinstance(error, _Cancelled) or current.status == "cancelled":
            current.status, current.error_code, current.error_message = "cancelled", "CANCELLED", "Cancelled by user"
        else:
            current.status = "failed"
            current.error_code = error.code if isinstance(error, AppError) else "TIMELINE_EXPORT_FAILED"
            current.error_message = str(error)[:1000]
        for step in current.steps:
            if step.status == "running":
                step.status, step.detail, step.finished_at = current.status, current.error_message[:500], utcnow()
        db.commit()
    finally:
        db.close()


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in value).strip()[:60] or "timeline"
