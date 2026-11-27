"""Phase 12 tables: mixing-studio settings, named presets and rendered mixes.

Kept separate from the Phase 5 ``audio_mixer_settings`` row, whose validator
rebuilds the document on every save and would drop Phase 12 fields.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import new_id, utcnow


class MixStudioSetting(Base):
    __tablename__ = "mix_studio_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    settings_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Previous versions, newest last, so "restore previous settings" works.
    history_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class MixPreset(Base):
    __tablename__ = "mix_presets"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_mix_preset_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    settings_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class MixRender(Base):
    """A rendered mix file. Source stems and dialogue tracks are only read."""

    __tablename__ = "mix_renders"

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
    target: Mapped[str] = mapped_column(String(16), nullable=False)  # original | ur | hi
    format: Mapped[str] = mapped_column(String(8), nullable=False)  # wav | aac
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    settings_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    tracks_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    storage_key: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stats_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
