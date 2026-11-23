from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import MediaAsset, Video
from ..schemas import JobOut, MediaAssetOut
from ..services.jobs import create_import_job, run_import_job
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
def get_media_asset(asset_id: str, db: Session = Depends(get_db)) -> Response:
    asset = db.get(MediaAsset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Media asset not found"})
    path = storage.absolute_path(asset.storage_key)
    if not path.is_file():
        raise HTTPException(status_code=410, detail={"code": "FILE_MISSING", "message": "Stored media asset is missing"})
    return FileResponse(path, media_type=asset.mime_type)


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
