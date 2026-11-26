import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_expiry() -> datetime:
    return utcnow() + timedelta(hours=24)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    target_languages: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    videos: Mapped[list["Video"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    assets: Mapped[list["MediaAsset"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    upload_sessions: Mapped[list["UploadSession"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    jobs: Mapped[list["ProcessingJob"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    audio_mixer_setting: Mapped["AudioMixerSetting | None"] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True, uselist=False
    )
    transcripts: Mapped[list["Transcript"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    characters: Mapped[list["Character"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    speaker_turns: Mapped[list["SpeakerTurn"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    face_tracks: Mapped[list["FaceTrack"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    visual_candidates: Mapped[list["VisualCandidate"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    frame_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pixel_format: Mapped[str | None] = mapped_column(String(64), nullable=True)
    color_space: Mapped[str | None] = mapped_column(String(64), nullable=True)
    color_range: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bit_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audio_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stream_layout: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    probe_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    probe_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="videos")
    assets: Mapped[list["MediaAsset"]] = relationship(
        back_populates="video", cascade="all, delete-orphan", passive_deletes=True
    )
    jobs: Mapped[list["ProcessingJob"]] = relationship(
        back_populates="video", cascade="all, delete-orphan", passive_deletes=True
    )
    transcripts: Mapped[list["Transcript"]] = relationship(
        back_populates="video", cascade="all, delete-orphan", passive_deletes=True
    )
    speaker_turns: Mapped[list["SpeakerTurn"]] = relationship(
        back_populates="video", cascade="all, delete-orphan", passive_deletes=True
    )
    face_tracks: Mapped[list["FaceTrack"]] = relationship(
        back_populates="video", cascade="all, delete-orphan", passive_deletes=True
    )
    visual_candidates: Mapped[list["VisualCandidate"]] = relationship(
        back_populates="video", cascade="all, delete-orphan", passive_deletes=True
    )
    character_assignments: Mapped[list["CharacterAssignment"]] = relationship(
        back_populates="video", cascade="all, delete-orphan", passive_deletes=True
    )


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="assets")
    video: Mapped[Video] = relationship(back_populates="assets")


class UploadSession(Base):
    __tablename__ = "upload_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False)
    received_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_chunks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    client_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=default_expiry)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="upload_sessions")


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False, default="media_import")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_step: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    options_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="jobs")
    video: Mapped[Video] = relationship(back_populates="jobs")
    steps: Mapped[list["JobStep"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True, order_by="JobStep.position"
    )


class AudioMixerSetting(Base):
    __tablename__ = "audio_mixer_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    settings_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="audio_mixer_setting")


class JobStep(Base):
    __tablename__ = "job_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("processing_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[ProcessingJob] = relationship(back_populates="steps")


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("processing_jobs.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="original_video")
    backend: Mapped[str] = mapped_column(String(32), nullable=False, default="faster-whisper")
    model: Mapped[str] = mapped_column(String(120), nullable=False, default="tiny")
    word_timestamps: Mapped[bool] = mapped_column(default=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="transcripts")
    video: Mapped[Video] = relationship(back_populates="transcripts")
    job: Mapped[ProcessingJob | None] = relationship()
    segments: Mapped[list["TranscriptSegment"]] = relationship(
        back_populates="transcript", cascade="all, delete-orphan", passive_deletes=True, order_by="TranscriptSegment.position"
    )


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    transcript_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("transcripts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    speaker: Mapped[str | None] = mapped_column(String(80), nullable=True)
    words: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    edited: Mapped[bool] = mapped_column(default=False)
    words_stale: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    transcript: Mapped[Transcript] = relationship(back_populates="segments")
    character_assignments: Mapped[list["CharacterAssignment"]] = relationship(
        back_populates="segment", cascade="all, delete-orphan", passive_deletes=True
    )


class Character(Base):
    """A project-scoped identity. Names are user-owned; voice data is evidence only."""

    __tablename__ = "characters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="review")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    voice_embedding_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    voice_sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    appearance_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="characters")
    speaker_turns: Mapped[list["SpeakerTurn"]] = relationship(
        back_populates="character", foreign_keys="SpeakerTurn.character_id"
    )
    face_tracks: Mapped[list["FaceTrack"]] = relationship(
        back_populates="character", foreign_keys="FaceTrack.character_id"
    )
    assignments: Mapped[list["CharacterAssignment"]] = relationship(
        back_populates="character", foreign_keys="CharacterAssignment.character_id"
    )


class SpeakerTurn(Base):
    """A detected speaker start/stop interval with its voice evidence."""

    __tablename__ = "speaker_turns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    transcript_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("transcripts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    label: Mapped[str] = mapped_column(String(40), nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    overlap_possible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    assignment_source: Mapped[str] = mapped_column(String(40), nullable=False, default="auto")
    embedding_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    notes_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="speaker_turns")
    video: Mapped[Video] = relationship(back_populates="speaker_turns")
    character: Mapped[Character | None] = relationship(
        back_populates="speaker_turns", foreign_keys=[character_id]
    )


class FaceTrack(Base):
    """A detected visible-person track. It is never an identity claim by itself."""

    __tablename__ = "face_tracks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    frame_asset_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("media_assets.id", ondelete="SET NULL"), nullable=True
    )
    label: Mapped[str] = mapped_column(String(40), nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    bbox_samples_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    appearance_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="face_tracks")
    video: Mapped[Video] = relationship(back_populates="face_tracks")
    character: Mapped[Character | None] = relationship(
        back_populates="face_tracks", foreign_keys=[character_id]
    )
    frame_asset: Mapped[MediaAsset | None] = relationship()
    visual_candidates: Mapped[list["VisualCandidate"]] = relationship(
        back_populates="face_track", cascade="all, delete-orphan", passive_deletes=True
    )


class VisualCandidate(Base):
    """A face/person candidate near dialogue, requiring explicit human confirmation."""

    __tablename__ = "visual_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    segment_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("transcript_segments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    face_track_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("face_tracks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    frame_asset_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("media_assets.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True
    )
    timestamp_ms: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    bbox_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rejected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="visual_candidates")
    video: Mapped[Video] = relationship(back_populates="visual_candidates")
    character: Mapped[Character | None] = relationship(
        foreign_keys=[character_id]
    )
    face_track: Mapped[FaceTrack | None] = relationship(back_populates="visual_candidates")
    frame_asset: Mapped[MediaAsset | None] = relationship()


class SegmentTranslation(Base):
    """One target-language rendering of a transcript segment.

    ``text`` is what the user sees; ``ai_text`` is the latest machine
    suggestion. When ``origin`` is ``manual`` the engine never replaces
    ``text`` unless explicitly forced; it only refreshes ``ai_text``.
    """

    __tablename__ = "segment_translations"
    __table_args__ = (UniqueConstraint("segment_id", "language", name="uq_segment_translation_language"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    segment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("transcript_segments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("processing_jobs.id", ondelete="SET NULL"), nullable=True
    )
    language: Mapped[str] = mapped_column(String(8), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    roman_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="ai")
    roman_origin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="translated")
    ai_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_roman_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_mt: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    engine: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    qc_flags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    fit_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    context_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    history_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class CharacterTranslationProfile(Base):
    """How a character speaks in the dub: gender agreement, register, style, names."""

    __tablename__ = "character_translation_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    gender: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    register: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    speaking_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    name_ur: Mapped[str | None] = mapped_column(String(160), nullable=True)
    name_hi: Mapped[str | None] = mapped_column(String(160), nullable=True)
    name_roman: Mapped[str | None] = mapped_column(String(160), nullable=True)
    names_auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class CharacterRelationship(Base):
    """Directed: how ``speaker`` addresses ``listener`` (aap / tum / tu)."""

    __tablename__ = "character_relationships"
    __table_args__ = (UniqueConstraint("speaker_character_id", "listener_character_id", name="uq_relationship_pair"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    speaker_character_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    listener_character_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    address_form: Mapped[str] = mapped_column(String(16), nullable=False, default="formal")
    relation: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class TranslationGlossaryTerm(Base):
    """Project terminology and proper nouns pinned to fixed target renderings."""

    __tablename__ = "translation_glossary_terms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(160), nullable=False)
    ur: Mapped[str | None] = mapped_column(String(240), nullable=True)
    hi: Mapped[str | None] = mapped_column(String(240), nullable=True)
    roman: Mapped[str | None] = mapped_column(String(240), nullable=True)
    case_sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class TranslationSceneNote(Base):
    """Free-text scene context attached to the first segment of a detected scene."""

    __tablename__ = "translation_scene_notes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    segment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("transcript_segments.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class CharacterAssignment(Base):
    """A reviewable link between a transcript segment and a stable character ID."""

    __tablename__ = "character_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    segment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("transcript_segments.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="auto_fusion")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    character: Mapped[Character | None] = relationship(
        back_populates="assignments", foreign_keys=[character_id]
    )
    segment: Mapped[TranscriptSegment] = relationship(back_populates="character_assignments")
    video: Mapped[Video] = relationship(back_populates="character_assignments")
