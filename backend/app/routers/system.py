from fastapi import APIRouter

from ..config import settings
from ..database import engine
from ..schemas import HealthOut, InfoOut
from ..services.audio import demucs_available, production_info
from ..services.characters import production_info as characters_info
from ..services.diarization import production_info as diarization_info
from ..services.media import probe_available
from ..services.transcription import production_info as transcription_info
from ..services.translation_engine import engine_info as translation_info
from ..services.vision import production_info as vision_info

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        database = "ok"
    except Exception:
        database = "unavailable"
    return HealthOut(
        status="ok" if database == "ok" else "degraded",
        app=settings.app_name,
        environment=settings.environment,
        database=database,
    )


@router.get("/info", response_model=InfoOut)
def info() -> InfoOut:
    return InfoOut(
        app=settings.app_name,
        version="0.5.0-phase8",
        max_upload_mb=settings.max_upload_mb,
        upload_chunk_mb=settings.upload_chunk_mb,
        thumbnail_count=settings.thumbnail_count,
        allowed_video_types=sorted(settings.allowed_content_types),
        media_probe_available=probe_available(),
        ai_stages="partial",
        capabilities={
            "projects": True,
            "video_upload": True,
            "resumable_upload": True,
            "video_metadata": True,
            "frame_index": True,
            "thumbnails": True,
            "versioned_artifacts": True,
            "video_preview": True,
            "stream_copy_policy": True,
            "processing_status": True,
            "job_cancellation": True,
            "job_retry": True,
            "audio_extraction": True,
            "audio_separation": True,
            "audio_preview": True,
            "audio_validation": True,
            "audio_cpu_processing": True,
            "audio_mixer": True,
            "dialogue_control": True,
            "synchronized_mix_preview": True,
            "project_mix_settings": True,
            "audio_demucs_available": demucs_available(),
            "audio_remote_backend_configured": bool(settings.remote_separation_url),
            "speech_recognition": True,
            "transcript_editor": True,
            "word_timestamps": True,
            "speaker_diarization": True,
            "character_identities": True,
            "visual_candidate_detection": True,
            "manual_identity_review": True,
            "face_recognition": False,
            "translation": True,
            "translation_editor": True,
            "context_aware_translation": True,
            "roman_urdu": True,
            "translation_refiner_configured": bool(settings.translation_refiner_url),
            "voice_generation": False,
            "lip_sync": False,
            "timeline_editor": False,
        },
        audio=production_info(),
        transcription=transcription_info(),
        characters={
            **characters_info(),
            "diarization": diarization_info(),
            "vision": vision_info(),
        },
        translation=translation_info(),
    )
