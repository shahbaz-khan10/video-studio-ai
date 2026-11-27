"""Phase 11 tables: where each dubbed line sits on the film timeline."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import new_id, utcnow


class DubTimingPlacement(Base):
    """The fitted placement of one line's current Phase 10 take, per language.

    ``manual`` marks a user-set window; ``approved`` locks the placement so
    batch fitting never changes it without explicit confirmation.
    """

    __tablename__ = "dub_timing_placements"
    __table_args__ = (UniqueConstraint("segment_id", "language", name="uq_dub_timing_segment_language"),)

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
    take_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("dub_audio_takes.id", ondelete="SET NULL"), nullable=True
    )
    character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True
    )
    job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("processing_jobs.id", ondelete="SET NULL"), nullable=True
    )
    language: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    strategy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # User-set window (only when manual) and permitted overlap with neighbours.
    manual_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    manual_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allow_overlap_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Result.
    window_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    window_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    placed_start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    placed_end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    take_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    speech_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fitted_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tempo: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    pause_saved_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overflow_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    storage_key: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    details_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    flags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    suggestions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
