"""Phase 10 tables. Kept apart from models.py so Phase 9 work there is untouched."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import new_id, utcnow


class DubAudioTake(Base):
    """One synthetic (AI text-to-speech) rendering of a dialogue line.

    Takes are never overwritten: regenerating adds a take. ``is_current``
    marks the take used downstream; ``approved`` protects it from batch runs.
    """

    __tablename__ = "dub_audio_takes"

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
    character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("processing_jobs.id", ondelete="SET NULL"), nullable=True
    )
    language: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    take_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ready")
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    emotion: Mapped[str] = mapped_column(String(24), nullable=False, default="neutral")
    engine: Mapped[str] = mapped_column(String(32), nullable=False, default="mms")
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    voice_source: Mapped[str] = mapped_column(String(32), nullable=False, default="default")
    voice_profile_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    voice_fingerprint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    voice_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    storage_key: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stats_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    qc_flags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
