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

    # Phase 4 — audio extraction and separation.
    # separation_backend: auto | dsp | demucs | remote
    separation_backend: str = "auto"
    demucs_model: str = "htdemucs"
    demucs_device: str = "cpu"
    model_cache_dir: Path | None = None
    remote_separation_url: str | None = None
    remote_separation_token: str | None = None
    audio_extract_format: str = "wav"

    # Phase 6 — speech recognition and dialogue transcription.
    # transcription_backend: auto | faster-whisper | remote
    transcription_backend: str = "auto"
    transcription_model: str = "tiny"
    transcription_device: str = "auto"
    transcription_compute_type: str = "auto"
    transcription_language: str = "auto"
    transcription_word_timestamps: bool = True
    transcription_vad_filter: bool = True
    transcription_model_cache_dir: Path | None = None
    transcription_remote_url: str | None = None
    transcription_remote_token: str | None = None

    # Phase 7 — speaker diarization and visual association.
    # diarization_backend: auto | local | remote
    diarization_backend: str = "auto"
    diarization_model: str = "speechbrain/spkrec-ecapa-voxceleb"
    diarization_device: str = "auto"
    diarization_vad_threshold: float = 0.5
    diarization_min_speech_ms: int = 250
    diarization_min_silence_ms: int = 200
    diarization_min_turn_ms: int = 350
    diarization_max_chunk_s: float = 5.0
    diarization_cluster_distance: float = 0.70
    diarization_match_threshold: float = 0.72
    diarization_review_threshold: float = 0.62
    diarization_match_margin: float = 0.06
    diarization_model_cache_dir: Path | None = None
    diarization_remote_url: str | None = None
    diarization_remote_token: str | None = None

    # visual_detection_backend: auto | opencv | none
    visual_detection_backend: str = "auto"
    visual_detector_model: str = "haarcascade_frontalface_default"
    visual_max_candidates: int = 24
    visual_frame_width: int = 480

    # Phase 8 — context-aware translation (open-source models only).
    # translation_backend: auto | indictrans2 | nllb | madlad | opus | remote
    #   auto prefers IndicTrans2 (MIT) when it is already in the local model
    #   cache, otherwise NLLB-200 (CC-BY-NC-4.0, non-commercial).
    translation_backend: str = "auto"
    translation_nllb_model: str = "facebook/nllb-200-distilled-600M"
    translation_indictrans2_model: str = "ai4bharat/indictrans2-en-indic-dist-200M"
    translation_madlad_model: str = "google/madlad400-3b-mt"
    translation_device: str = "auto"
    # Dynamic int8 quantisation of Linear layers on CPU: ~2x less RAM, faster.
    translation_cpu_quantize: bool = True
    translation_num_beams: int = 4
    translation_batch_size: int = 8
    translation_max_new_tokens: int = 160
    translation_scene_gap_ms: int = 6000
    translation_comfortable_syllables_per_s: float = 5.5
    translation_model_cache_dir: Path | None = None
    translation_remote_url: str | None = None
    translation_remote_token: str | None = None
    # Optional self-hosted open LLM (OpenAI-compatible endpoint such as Ollama,
    # llama.cpp server or vLLM on a rented GPU) that rewrites the MT draft with
    # full scene/character context. Never a paid API.
    translation_refiner_url: str | None = None
    translation_refiner_model: str = "qwen2.5:7b-instruct"
    translation_refiner_token: str | None = None
    translation_refiner_timeout_s: float = 120.0

    @property
    def effective_translation_cache_dir(self) -> Path:
        return self.translation_model_cache_dir or (self.effective_model_cache_dir / "hf")

    @property
    def translation_languages(self) -> set[str]:
        return {"ur", "hi"}

    @property
    def effective_model_cache_dir(self) -> Path:
        return self.model_cache_dir or (self.data_dir / "models")

    @property
    def effective_transcription_cache_dir(self) -> Path:
        return self.transcription_model_cache_dir or self.effective_model_cache_dir

    @property
    def effective_diarization_cache_dir(self) -> Path:
        return self.diarization_model_cache_dir or self.effective_model_cache_dir

    @property
    def supported_transcription_languages(self) -> set[str]:
        return {"auto", "en", "ur", "hi"}

    @property
    def audio_formats(self) -> set[str]:
        return {"wav", "flac", "mp3", "aac"}

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
