"""Phase 14 tables: professional multitrack timeline editor + external audio import.

Kept separate from models.py so earlier phases are untouched. Original source
files (uploaded video, extracted stems, Phase 10 takes, imported external
audio) are only ever *referenced*; timeline edits are non-destructive rows in
``timeline_clips``. Full snapshots in ``timeline_history`` back undo/redo and
session recovery.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import new_id, utcnow

# Track kinds (Task 1 + Task 4 + Task 5 + Task 6).
TRACK_KINDS = (
    "video",  # Original Video Track
    "original_english",  # Original English Dialogue
    "urdu_dub",  # Urdu Dub (language=ur)
    "hindi_dub",  # Hindi Dub (language=hi)
    "background_music",  # Background Music
    "sound_effects",  # Sound Effects
    "ambience",  # Ambience
    "external_audio",  # External Audio Tracks (multiple allowed)
    "lip_sync",  # Phase 13 generated lip-sync video segments
)

LANGUAGE_TRACKS = {"original_english": "en", "urdu_dub": "ur", "hindi_dub": "hi"}

# Clip source kinds.
SOURCE_VIDEO = "video"
SOURCE_AUDIO_STEM = "audio_stem"
SOURCE_DUB_TAKE = "dub_take"
SOURCE_DUB_TRACK = "dub_track"
SOURCE_EXTERNAL_AUDIO = "external_audio"
SOURCE_ORIGINAL_AUDIO = "original_audio"
SOURCE_LIP_SYNC = "lip_sync_video"

# Lip-sync segment statuses (Task 5). Outdated output is never silently
# treated as synchronized.
LIPSYNC_PENDING = "pending"
LIPSYNC_SYNCED = "synced"
LIPSYNC_NEEDS_REGENERATION = "needs_regeneration"
LIPSYNC_STALE = "stale"
LIPSYNC_FAILED = "failed"

# External audio formats (Task 3).
AUDIO_EXTENSIONS = {
    ".wav": "wav",
    ".mp3": "mp3",
    ".aac": "aac",
    ".m4a": "m4a",
    ".flac": "flac",
    ".ogg": "ogg",
}
AUDIO_MIME = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "aac": "audio/aac",
    "m4a": "audio/mp4",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
}


class ExternalAudio(Base):
    """One external audio file imported by the user (Task 3).

    The stored copy under ``storage_key`` is written once on import and never
    modified; trimming/moving on the timeline stores non-destructive ranges.
    The user's own original file on disk is never touched.
    """

    __tablename__ = "external_audio_imports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="SET NULL"), nullable=True, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    peaks_key: Mapped[str | None] = mapped_column(String(255), nullable=True)  # cached waveform overview
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class TimelineProject(Base):
    """A saved timeline editing session bound to one project (and video)."""

    __tablename__ = "timeline_projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False, default="Main timeline")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sample_rate: Mapped[int] = mapped_column(Integer, nullable=False, default=48000)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    history_cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    autosave: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class TimelineTrack(Base):
    __tablename__ = "timeline_tracks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    timeline_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("timeline_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    color: Mapped[str] = mapped_column(String(24), nullable=False, default="#4f8cff")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # selectable / rendered
    muted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    solo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    volume_db: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    pan: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class TimelineClip(Base):
    """A non-destructive clip referencing a source range (Task 2)."""

    __tablename__ = "timeline_clips"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    timeline_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("timeline_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    track_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("timeline_tracks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_type: Mapped[str] = mapped_column(String(24), nullable=False, default="audio_stem")
    source_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    external_audio_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("external_audio_imports.id", ondelete="SET NULL"), nullable=True, index=True
    )
    storage_key: Mapped[str | None] = mapped_column(String(255), nullable=True)  # resolved source file
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="Clip")
    # Task 4: character identity (Phase 7) + voice profile snapshot (Phase 9).
    character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    character_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    segment_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("transcript_segments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_in_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # trim start
    source_out_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)  # trim end (None = source end)
    source_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    volume_db: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fade_in_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fade_out_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    muted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    replaces_clip_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # external replacing generated dialogue
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class TimelineHistory(Base):
    """Full snapshot per revision; backs undo/redo + interrupted-session recovery."""

    __tablename__ = "timeline_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    timeline_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("timeline_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False, default="edit")
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
