from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models_mix  # noqa: F401 — registers Phase 12 tables before init_db
from ..services import job_queue
from ..database import get_db
from ..models import Video
from ..models_mix import MixRender
from ..schemas import JobOut
from ..services import mix_studio as service
from ..services.media import AppError
from ..services.storage import storage

router = APIRouter(tags=["mix-studio"])
Target = Literal["original", "ur", "hi"]


class SettingsUpdate(BaseModel):
    settings: dict
    base_revision: int | None = None


class PresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    settings: dict | None = None  # defaults to the current project settings
    overwrite: bool = False


class RenderRequest(BaseModel):
    target: Target = "ur"
    format: Literal["wav", "aac"] = "wav"


class PreviewRequest(BaseModel):
    target: Target = "ur"
    start_ms: int = Field(default=0, ge=0)
    length_ms: int = Field(default=15000, ge=1000, le=30000)
    settings: dict | None = None  # unsaved settings from the UI


def _video(db: Session, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Video not found"})
    return video


@router.get("/videos/{video_id}/mix-studio")
def get_studio(video_id: str, db: Session = Depends(get_db)) -> dict:
    return service.studio_bundle(db, _video(db, video_id))


@router.put("/projects/{project_id}/mix-studio/settings")
def put_settings(project_id: str, payload: SettingsUpdate, db: Session = Depends(get_db)) -> dict:
    return service.settings_payload(service.save_settings(db, project_id, payload.settings, payload.base_revision))


@router.post("/projects/{project_id}/mix-studio/settings/reset")
def reset(project_id: str, db: Session = Depends(get_db)) -> dict:
    return service.settings_payload(service.reset_settings(db, project_id))


@router.post("/projects/{project_id}/mix-studio/settings/restore-previous")
def restore_previous(project_id: str, db: Session = Depends(get_db)) -> dict:
    return service.settings_payload(service.restore_previous(db, project_id))


@router.get("/projects/{project_id}/mix-studio/presets")
def presets(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    return [service.preset_payload(item) for item in service.list_presets(db, project_id)]


@router.post("/projects/{project_id}/mix-studio/presets", status_code=201)
def create_preset(project_id: str, payload: PresetCreate, db: Session = Depends(get_db)) -> dict:
    return service.preset_payload(service.save_preset(db, project_id, payload.name, payload.settings, payload.overwrite))


@router.post("/projects/{project_id}/mix-studio/presets/{preset_id}/apply")
def apply_preset(project_id: str, preset_id: str, db: Session = Depends(get_db)) -> dict:
    return service.settings_payload(service.apply_preset(db, project_id, preset_id))


@router.delete("/projects/{project_id}/mix-studio/presets/{preset_id}", status_code=204)
def delete_preset(project_id: str, preset_id: str, db: Session = Depends(get_db)) -> Response:
    service.delete_preset(db, project_id, preset_id)
    return Response(status_code=204)


@router.post("/videos/{video_id}/mix-studio/preview")
def preview(video_id: str, payload: PreviewRequest, db: Session = Depends(get_db)) -> Response:
    """Exact-processing preview (includes render-only effects) for a short window."""
    audio = service.preview_window(db, _video(db, video_id), payload.target, payload.start_ms, payload.length_ms, payload.settings)
    return Response(audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@router.post("/videos/{video_id}/mix-studio/render", response_model=JobOut, status_code=202)
def render(video_id: str, payload: RenderRequest, background: BackgroundTasks, db: Session = Depends(get_db)) -> JobOut:
    video = _video(db, video_id)
    job = service.create_render_job(db, video, payload.target, payload.format)
    job_queue.submit(background, job)
    return JobOut.model_validate(job)


@router.get("/mix-renders/{render_id}/file")
def render_file(render_id: str, download: bool = Query(default=False), db: Session = Depends(get_db)):
    row = db.get(MixRender, render_id)
    if row is None or row.status != "ready" or not row.storage_key:
        raise AppError(404, "MIX_NOT_FOUND", "This mix is not available.")
    path = storage.absolute_path(row.storage_key)
    if not path.is_file():
        raise AppError(410, "MIX_MISSING", "The rendered file is missing.")
    ext = "wav" if row.format == "wav" else "m4a"
    media = "audio/wav" if row.format == "wav" else "audio/mp4"
    name = f"mix_{row.target}_{row.created_at:%Y%m%d_%H%M%S}.{ext}" if download else None
    return FileResponse(path, media_type=media, filename=name, headers={"Cache-Control": "no-cache"})


@router.delete("/mix-renders/{render_id}", status_code=204)
def delete_render(render_id: str, db: Session = Depends(get_db)) -> Response:
    row = db.get(MixRender, render_id)
    if row is None:
        raise AppError(404, "MIX_NOT_FOUND", "This mix does not exist.")
    if row.storage_key:
        storage.absolute_path(row.storage_key).unlink(missing_ok=True)
    db.delete(row)
    db.commit()
    return Response(status_code=204)
