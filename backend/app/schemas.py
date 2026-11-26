from datetime import datetime
from math import gcd

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    source_language: str = Field(default="en", min_length=2, max_length=16)
    target_languages: list[str] = Field(default_factory=lambda: ["ur", "hi"])


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    status: str | None = Field(default=None, min_length=1, max_length=32)


class MediaAssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    video_id: str
    kind: str
    version: int
    mime_type: str
    size_bytes: int
    checksum_sha256: str | None
    metadata_json: dict
    created_at: datetime
    url: str = ""


class VideoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    original_filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str | None
    duration_ms: int | None
    width: int | None
    height: int | None
    fps: float | None
    frame_count: int | None
    pixel_format: str | None
    color_space: str | None
    color_range: str | None
    bit_rate: int | None
    video_codec: str | None
    audio_codec: str | None
    audio_stream_count: int = 0
    subtitle_streams: list = Field(default_factory=list)
    aspect_ratio: str | None = None
    stream_validation: dict = Field(default_factory=dict)
    stream_layout: list
    probe_status: str
    probe_source: str | None
    metadata_json: dict
    created_at: datetime
    stream_url: str = ""


def build_video_out(video, stream_url: str = "") -> VideoOut:
    payload = VideoOut.model_validate(video)
    layout = video.stream_layout or []
    audio_streams = [
        item for item in layout if str(item.get("codec_type", "")).lower() == "audio"
    ]
    subtitle_streams = [
        {
            "index": item.get("index"),
            "codec_name": item.get("codec_name"),
            "language": item.get("language"),
        }
        for item in layout
        if str(item.get("codec_type", "")).lower() == "subtitle"
    ]
    width = next(
        (
            item.get("width")
            for item in layout
            if str(item.get("codec_type", "")).lower() == "video" and item.get("width")
        ),
        None,
    )
    height = next(
        (
            item.get("height")
            for item in layout
            if str(item.get("codec_type", "")).lower() == "video" and item.get("height")
        ),
        None,
    )
    aspect_ratio = None
    if width and height:
        divisor = gcd(int(width), int(height))
        aspect_ratio = f"{int(width) // divisor}:{int(height) // divisor}"

    metadata = video.metadata_json or {}
    payload.audio_stream_count = int(metadata.get("audio_stream_count", len(audio_streams)) or 0)
    payload.subtitle_streams = metadata.get("subtitle_streams") or subtitle_streams
    payload.aspect_ratio = metadata.get("aspect_ratio") or aspect_ratio
    payload.stream_validation = metadata.get("stream_validation") or {}
    payload.stream_url = stream_url
    return payload


class JobStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    status: str
    detail: str | None
    position: int


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    video_id: str
    type: str
    status: str
    progress: int
    current_step: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    steps: list[JobStepOut] = Field(default_factory=list)


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None
    source_language: str
    target_languages: list
    status: str
    created_at: datetime
    updated_at: datetime
    videos: list[VideoOut] = Field(default_factory=list)


class ProjectSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None
    source_language: str
    target_languages: list
    status: str
    created_at: datetime
    updated_at: datetime
    video_count: int = 0
    latest_job_status: str | None = None


class HealthOut(BaseModel):
    status: str
    app: str
    environment: str
    database: str


class InfoOut(BaseModel):
    app: str
    version: str
    max_upload_mb: int
    upload_chunk_mb: int
    thumbnail_count: int
    allowed_video_types: list[str]
    media_probe_available: bool
    ai_stages: str
    capabilities: dict


class ErrorOut(BaseModel):
    error: dict


class UploadAccepted(BaseModel):
    video: VideoOut
    job: JobOut


class UploadSessionCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    mime_type: str = Field(min_length=1, max_length=120)
    duration_ms: int | None = Field(default=None, ge=1, le=24 * 60 * 60 * 1000)
    width: int | None = Field(default=None, ge=1, le=16384)
    height: int | None = Field(default=None, ge=1, le=16384)


class UploadSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    filename: str
    content_type: str
    size_bytes: int
    chunk_size: int
    total_chunks: int
    received_bytes: int
    uploaded_chunks: list
    status: str
    expires_at: datetime
