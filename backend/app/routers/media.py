from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import MediaAsset, ProcessingJob, Video
from ..schemas import AudioBundleOut, AudioStemOut, JobOut, MediaAssetOut
from ..services.audio import STEM_LABELS, STEM_ROLES
from ..services.jobs import create_audio_job, create_import_job, run_audio_separation_job, run_import_job
from ..services.storage import storage

router = APIRouter(tags=["media"])


def asset_to_out(asset: MediaAsset) -> MediaAssetOut:
    payload = MediaAssetOut.model_validate(asset)
    payload.url = f"/api/v1/media/{asset.id}"
    return payload


def find_video(db: Session, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Video not found"})
    return video


def _latest_by_kind(db: Session, video_id: str) -> dict[str, MediaAsset]:
    assets = db.scalars(
        select(MediaAsset)
        .where(MediaAsset.video_id == video_id)
        .order_by(MediaAsset.kind, MediaAsset.version.desc(), MediaAsset.created_at.desc())
    ).all()
    latest: dict[str, MediaAsset] = {}
    for asset in assets:
        latest.setdefault(asset.kind, asset)
    return latest


def _stem_out(
    asset: MediaAsset | None,
    role: str,
    label: str,
    video_id: str,
    metadata: dict | None = None,
) -> AudioStemOut:
    meta = metadata or {}
    if asset:
        meta = asset.metadata_json or {}
        validation = meta.get("validation") or {}
        return AudioStemOut(
            role=role,
            label=label,
            status="available",
            asset_id=asset.id,
            version=asset.version,
            mime_type=asset.mime_type,
            size_bytes=asset.size_bytes,
            url=f"/api/v1/media/{asset.id}",
            download_url=f"/api/v1/videos/{video_id}/audio/stems/{role}/download",
            duration_ms=validation.get("duration_ms"),
            sample_rate=validation.get("sample_rate"),
            channels=validation.get("channels"),
            confidence=meta.get("confidence"),
            validation=validation,
            comparison=meta.get("comparison") or {},
        )
    validation = meta.get("validation") or {}
    return AudioStemOut(
        role=role,
        label=label,
        status="unavailable",
        duration_ms=validation.get("duration_ms"),
        sample_rate=validation.get("sample_rate"),
        channels=validation.get("channels"),
        confidence=meta.get("confidence"),
        validation=validation,
        comparison=meta.get("comparison") or {},
    )


def build_audio_bundle(db: Session, video: Video) -> AudioBundleOut:
    latest = _latest_by_kind(db, video.id)
    job = db.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.video_id == video.id, ProcessingJob.type == "audio_separation")
        .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
    )
    audio_meta = (video.metadata_json or {}).get("audio") or {}
    available_roles = [role for role in STEM_ROLES if f"stem_{role}" in latest]
    if job:
        status = job.status
    elif len(available_roles) == len(STEM_ROLES):
        status = "completed"
    else:
        status = "idle"

    source_meta = audio_meta.get("source") or {}
    source = _stem_out(
        latest.get("source_audio"),
        "source",
        "Original audio",
        video.id,
        {**source_meta, "confidence": "source_extraction"},
    )
    if source.asset_id:
        source.download_url = f"/api/v1/videos/{video.id}/audio/source/download"

    stems: dict[str, AudioStemOut] = {}
    for role in STEM_ROLES:
        fallback = (audio_meta.get("stems") or {}).get(role) or {}
        if role not in fallback:
            fallback["confidence"] = audio_meta.get("confidence")
        stems[role] = _stem_out(
            latest.get(f"stem_{role}"),
            role,
            STEM_LABELS[role],
            video.id,
            fallback,
        )

    return AudioBundleOut(
        video_id=video.id,
        status=status,
        job=JobOut.model_validate(job) if job else None,
        source=source,
        stems=stems,
        backend=audio_meta.get("backend") or (job.current_step if job else None),
        model=audio_meta.get("model"),
        confidence=audio_meta.get("confidence"),
        limitations=audio_meta.get("limitations") or [],
        notes=audio_meta.get("notes") or [],
        comparison=audio_meta.get("comparison") or {},
    )


def _download_response(asset: MediaAsset, filename: str) -> FileResponse:
    path = storage.absolute_path(asset.storage_key)
    if not path.is_file():
        raise HTTPException(status_code=410, detail={"code": "FILE_MISSING", "message": "Stored media asset is missing"})
    return FileResponse(path, media_type=asset.mime_type, filename=filename)


@router.get("/videos/{video_id}/assets", response_model=list[MediaAssetOut])
def list_video_assets(
    video_id: str,
    kind: str | None = None,
    db: Session = Depends(get_db),
) -> list[MediaAssetOut]:
    find_video(db, video_id)
    query = select(MediaAsset).where(MediaAsset.video_id == video_id)
    if kind:
        query = query.where(MediaAsset.kind == kind)
    assets = db.scalars(query.order_by(MediaAsset.kind, MediaAsset.version.desc())).all()
    return [asset_to_out(asset) for asset in assets]


@router.get("/media/{asset_id}")
def get_media_asset(
    asset_id: str,
    download: bool = False,
    db: Session = Depends(get_db),
) -> Response:
    asset = db.get(MediaAsset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Media asset not found"})
    path = storage.absolute_path(asset.storage_key)
    if not path.is_file():
        raise HTTPException(status_code=410, detail={"code": "FILE_MISSING", "message": "Stored media asset is missing"})
    filename = f"{asset.kind}-v{asset.version}{Path(asset.storage_key).suffix}" if download else None
    return FileResponse(path, media_type=asset.mime_type, filename=filename)


@router.post("/videos/{video_id}/reprobe", response_model=JobOut, status_code=202)
def reprobe_video(
    video_id: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> JobOut:
    video = find_video(db, video_id)
    if not storage.absolute_path(video.storage_key).is_file():
        raise HTTPException(status_code=410, detail={"code": "FILE_MISSING", "message": "Stored video file is missing"})
    job = create_import_job(db, video.project_id, video.id, job_type="media_reprobe")
    client_metadata = (video.metadata_json or {}).get("browser") or {}
    background.add_task(run_import_job, job.id, video.id, client_metadata)
    return JobOut.model_validate(job)


@router.get("/videos/{video_id}/audio", response_model=AudioBundleOut)
def get_audio_bundle(video_id: str, db: Session = Depends(get_db)) -> AudioBundleOut:
    video = find_video(db, video_id)
    return build_audio_bundle(db, video)


@router.get("/videos/{video_id}/audio/stems", response_model=AudioBundleOut)
def get_audio_stems(video_id: str, db: Session = Depends(get_db)) -> AudioBundleOut:
    video = find_video(db, video_id)
    return build_audio_bundle(db, video)


@router.post("/videos/{video_id}/audio/separate", response_model=JobOut, status_code=202)
def separate_video_audio(
    video_id: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> JobOut:
    video = find_video(db, video_id)
    if not storage.absolute_path(video.storage_key).is_file():
        raise HTTPException(status_code=410, detail={"code": "FILE_MISSING", "message": "Stored video file is missing"})
    metadata = video.metadata_json or {}
    validation = metadata.get("stream_validation") or {}
    audio_stream_count = int(
        metadata.get("audio_stream_count", 0)
        or sum(
            1
            for item in (video.stream_layout or [])
            if str(item.get("codec_type", "")).lower() == "audio"
        )
    )
    if validation.get("audio") == "none" or (video.probe_status == "probed" and audio_stream_count == 0):
        raise HTTPException(
            status_code=422,
            detail={"code": "NO_AUDIO_STREAM", "message": "The source video has no audio stream to extract."},
        )
    active = db.scalar(
        select(ProcessingJob).where(
            ProcessingJob.video_id == video.id,
            ProcessingJob.type == "audio_separation",
            ProcessingJob.status.in_(["queued", "running"]),
        )
    )
    if active:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "AUDIO_JOB_ACTIVE",
                "message": "An audio separation job is already running for this video.",
            },
        )
    job = create_audio_job(db, video.project_id, video.id)
    background.add_task(run_audio_separation_job, job.id, video.id)
    return JobOut.model_validate(job)


@router.get("/videos/{video_id}/audio/source/download")
def download_source_audio(video_id: str, db: Session = Depends(get_db)) -> FileResponse:
    video = find_video(db, video_id)
    asset = _latest_by_kind(db, video.id).get("source_audio")
    if not asset:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "No extracted audio is available"})
    return _download_response(asset, f"{video.original_filename}-source{Path(asset.storage_key).suffix}")


@router.get("/videos/{video_id}/audio/stems/{role}/download")
def download_audio_stem(video_id: str, role: str, db: Session = Depends(get_db)) -> FileResponse:
    if role not in STEM_ROLES:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Unknown audio stem"})
    video = find_video(db, video_id)
    asset = _latest_by_kind(db, video.id).get(f"stem_{role}")
    if not asset:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": f"No {role} stem is available"})
    return _download_response(asset, f"{video.original_filename}-{role}.wav")
