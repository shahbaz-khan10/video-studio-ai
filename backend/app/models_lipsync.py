"""Phase 13 tables: face choices and lip-sync results (stored apart from models.py)."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import new_id, utcnow


class LipSyncFaceSelection(Base):
    """A user's choice of which visible face speaks a dialogue line."""

    __tablename__ = "lipsync_face_selections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    video_id: Mapped[str] = mapped_column(String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True)
    segment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("transcript_segments.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    character_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True)
    time_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    box_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # normalised x, y, w, h
    off_screen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class LipSyncResult(Base):
    """One lip-sync attempt for a line in one language. Outputs never overwrite the source video."""

    __tablename__ = "lipsync_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    video_id: Mapped[str] = mapped_column(String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True)
    segment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("transcript_segments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("processing_jobs.id", ondelete="SET NULL"), nullable=True)
    language: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="generated")
    review: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    engine: Mapped[str] = mapped_column(String(32), nullable=False, default="wav2lip")
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    target_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    audio_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    audio_ref: Mapped[str | None] = mapped_column(String(36), nullable=True)
    window_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    window_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_frame: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frame_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    after_key: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    before_key: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    faces_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    qc_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    flags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    processing_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Phase 14 (Task 5): lip-sync invalidation when dubbed audio timing/content changes.
    needs_regeneration: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    regeneration_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dubbed_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
