from datetime import datetime
from math import gcd
from typing import Literal

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


class AudioStemOut(BaseModel):
    role: str
    label: str
    status: str
    asset_id: str | None = None
    version: int | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    url: str | None = None
    download_url: str | None = None
    duration_ms: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    confidence: str | None = None
    validation: dict = Field(default_factory=dict)
    comparison: dict = Field(default_factory=dict)


class AudioBundleOut(BaseModel):
    video_id: str
    status: str
    job: JobOut | None = None
    source: AudioStemOut | None = None
    stems: dict[str, AudioStemOut] = Field(default_factory=dict)
    backend: str | None = None
    model: str | None = None
    confidence: str | None = None
    limitations: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    comparison: dict = Field(default_factory=dict)


class MixerTrackState(BaseModel):
    role: str
    label: str
    volume: float = Field(ge=0, le=1)
    muted: bool = False
    solo: bool = False
    enabled: bool = True


class AudioMixSettingsOut(BaseModel):
    project_id: str
    master_volume: float = Field(ge=0, le=1)
    preview_enabled: bool = False
    tracks: dict[str, MixerTrackState] = Field(default_factory=dict)
    updated_at: datetime | None = None


class MixerTrackUpdate(BaseModel):
    volume: float = Field(default=0.8, ge=0, le=1)
    muted: bool = False
    solo: bool = False
    enabled: bool = True


class AudioMixSettingsUpdate(BaseModel):
    master_volume: float = Field(default=1.0, ge=0, le=1)
    preview_enabled: bool = False
    tracks: dict[str, MixerTrackUpdate] = Field(default_factory=dict)


class TranscriptionRequest(BaseModel):
    language: str = Field(default="auto", min_length=2, max_length=16)
    prefer_dialogue_stem: bool = True
    word_timestamps: bool = True


class TranscriptWordOut(BaseModel):
    start_ms: int
    end_ms: int
    text: str
    confidence: float | None = None


class TranscriptSegmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    position: int
    start_ms: int
    end_ms: int
    text: str
    confidence: float | None = None
    language: str | None = None
    speaker: str | None = None
    words: list[TranscriptWordOut] = Field(default_factory=list)
    edited: bool = False
    words_stale: bool = False


class TranscriptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str | None = None
    video_id: str
    job_id: str | None = None
    version: int = 0
    status: str = "idle"
    language: str = "auto"
    source: str | None = None
    backend: str | None = None
    model: str | None = None
    word_timestamps: bool = True
    revision: int = 0
    segments: list[TranscriptSegmentOut] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata_json: dict = Field(default_factory=dict)
    updated_at: datetime | None = None


class TranscriptSegmentUpdate(BaseModel):
    id: str | None = None
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str = Field(min_length=0, max_length=20000)
    speaker: str | None = Field(default=None, max_length=80)


class TranscriptUpdate(BaseModel):
    revision: int | None = Field(default=None, ge=1)
    segments: list[TranscriptSegmentUpdate]


class CharacterDetectionRequest(BaseModel):
    prefer_dialogue_stem: bool = True
    detect_faces: bool = True
    max_candidates: int = Field(default=24, ge=0, le=120)


class CharacterUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    notes: str | None = Field(default=None, max_length=4000)
    status: str | None = Field(default=None, min_length=1, max_length=32)


class CharacterMergeRequest(BaseModel):
    character_ids: list[str] = Field(default_factory=list, min_length=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)


class CharacterSplitRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    segment_ids: list[str] = Field(default_factory=list)
    turn_ids: list[str] = Field(default_factory=list)
    face_track_ids: list[str] = Field(default_factory=list)


class SegmentAssignmentUpdate(BaseModel):
    character_id: str | None = None
    reviewed: bool = True
    confidence: float | None = Field(default=None, ge=0, le=1)


class VisualCandidateUpdate(BaseModel):
    confirmed_character_id: str | None = None
    rejected: bool = False
    notes: str | None = Field(default=None, max_length=4000)


class CharacterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    name: str
    source_label: str | None = None
    status: str
    confidence: float | None = None
    notes: str | None = None
    voice_characteristics: dict = Field(default_factory=dict)
    dialogue_count: int = 0
    scene_count: int = 0
    created_at: datetime
    updated_at: datetime


class SpeakerTurnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    video_id: str
    character_id: str | None = None
    label: str
    start_ms: int
    end_ms: int
    confidence: float | None = None
    overlap_possible: bool = False
    reviewed: bool = False
    assignment_source: str
    created_at: datetime


class FaceTrackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    video_id: str
    character_id: str | None = None
    label: str
    start_ms: int
    end_ms: int
    confidence: float | None = None
    representative_url: str | None = None
    sample_count: int = 0
    created_at: datetime


class VisualCandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    video_id: str
    character_id: str | None = None
    face_track_id: str | None = None
    segment_id: str | None = None
    timestamp_ms: int
    bbox: list = Field(default_factory=list)
    confidence: float | None = None
    frame_url: str | None = None
    confirmed_character_id: str | None = None
    confirmed: bool = False
    rejected: bool = False
    notes: str | None = None
    created_at: datetime


class CharacterAssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    character_id: str | None = None
    segment_id: str
    video_id: str
    source: str
    confidence: float | None = None
    reviewed: bool = False
    created_at: datetime
    updated_at: datetime


class DialogueItemOut(BaseModel):
    segment_id: str
    character_id: str | None = None
    start_ms: int
    end_ms: int
    text: str
    source: str = "unassigned"
    confidence: float | None = None
    reviewed: bool = False


class CharacterBundleOut(BaseModel):
    video_id: str
    status: str = "idle"
    job: JobOut | None = None
    characters: list[CharacterOut] = Field(default_factory=list)
    speaker_turns: list[SpeakerTurnOut] = Field(default_factory=list)
    face_tracks: list[FaceTrackOut] = Field(default_factory=list)
    visual_candidates: list[VisualCandidateOut] = Field(default_factory=list)
    assignments: list[CharacterAssignmentOut] = Field(default_factory=list)
    dialogue: list[DialogueItemOut] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    diarization: dict = Field(default_factory=dict)
    vision: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)
    updated_at: datetime | None = None


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
    audio: dict = Field(default_factory=dict)
    transcription: dict = Field(default_factory=dict)
    characters: dict = Field(default_factory=dict)
    translation: dict = Field(default_factory=dict)


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


# ---------------------------------------------------------------------------
# Phase 8 — translation
# ---------------------------------------------------------------------------

TranslationLanguage = Literal["ur", "hi"]


class TranslateAllRequest(BaseModel):
    languages: list[TranslationLanguage] = Field(default_factory=lambda: ["ur", "hi"], min_length=1)
    overwrite_manual: bool = False
    only_missing: bool = False
    segment_ids: list[str] | None = None


class TranslateSegmentRequest(BaseModel):
    languages: list[TranslationLanguage] = Field(default_factory=lambda: ["ur", "hi"], min_length=1)
    force: bool = False


class TranslationEdit(BaseModel):
    text: str | None = Field(default=None, max_length=20000)
    roman_text: str | None = Field(default=None, max_length=20000)
    base_revision: int | None = None


class TranslationRestore(BaseModel):
    target: Literal["ai", "history"] = "ai"
    index: int | None = None


class TranslationProfileUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    gender: Literal["unknown", "male", "female"] = "unknown"
    speech_register: Literal["auto", "formal", "neutral", "informal", "intimate"] = Field(default="auto", alias="register")
    speaking_style: str | None = Field(default=None, max_length=2000)
    name_ur: str | None = Field(default=None, max_length=160)
    name_hi: str | None = Field(default=None, max_length=160)
    name_roman: str | None = Field(default=None, max_length=160)


class RelationshipIn(BaseModel):
    speaker_character_id: str
    listener_character_id: str
    address_form: Literal["formal", "informal", "intimate"] = "formal"
    relation: str | None = Field(default=None, max_length=120)


class RelationshipsUpdate(BaseModel):
    relationships: list[RelationshipIn] = Field(default_factory=list, max_length=500)


class GlossaryTermIn(BaseModel):
    source: str = Field(min_length=1, max_length=160)
    ur: str | None = Field(default=None, max_length=240)
    hi: str | None = Field(default=None, max_length=240)
    roman: str | None = Field(default=None, max_length=240)
    case_sensitive: bool = False
    notes: str | None = Field(default=None, max_length=1000)


class GlossaryUpdate(BaseModel):
    terms: list[GlossaryTermIn] = Field(default_factory=list, max_length=2000)


class SceneNoteUpdate(BaseModel):
    note: str = Field(default="", max_length=4000)
