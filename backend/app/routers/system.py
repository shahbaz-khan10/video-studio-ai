from fastapi import APIRouter

from ..config import settings
from ..database import engine
from ..schemas import HealthOut, InfoOut
from ..services.media import probe_available

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
        version="0.2.0-phase3",
        max_upload_mb=settings.max_upload_mb,
        upload_chunk_mb=settings.upload_chunk_mb,
        thumbnail_count=settings.thumbnail_count,
        allowed_video_types=sorted(settings.allowed_content_types),
        media_probe_available=probe_available(),
        ai_stages="planned",
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
            "translation": False,
            "voice_generation": False,
            "lip_sync": False,
            "timeline_editor": False,
        },
    )
