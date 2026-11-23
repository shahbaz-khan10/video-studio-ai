from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "CINEMA AI DUB"
    environment: str = "development"
    data_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "data")
    database_url: str = Field(
        default_factory=lambda: f"sqlite:///{(PROJECT_ROOT / 'data' / 'cinema.db').as_posix()}"
    )
    max_upload_mb: int = 2048
    upload_chunk_mb: int = 8
    upload_session_ttl_hours: int = 24
    thumbnail_count: int = 6
    allowed_video_types: str = (
        "video/mp4,video/quicktime,video/webm,video/x-matroska,video/x-msvideo,video/ogg"
    )
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    ffprobe_binary: str | None = None

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def allowed_content_types(self) -> set[str]:
        return {item.strip() for item in self.allowed_video_types.split(",") if item.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


settings = Settings()
