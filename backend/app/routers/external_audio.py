"""Phase 14 - external audio import endpoints (Task 3)."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Project, Video
from ..models_timeline import ExternalAudio
from ..services import external_audio as service
from ..services.media import AppError
from ..services.storage import storage

router = APIRouter(tags=["external-audio"])
CHUNK_SIZE = 1024 * 1024


def _project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    return project


def _audio(db: Session, audio_id: str) -> ExternalAudio:
    row = db.get(ExternalAudio, audio_id)
    if not row:
        raise AppError(404, "AUDIO_IMPORT_NOT_FOUND", "External audio import not found.")
    return row


SUPPORTED = ["wav", "mp3", "aac", "m4a", "flac", "ogg"]


@router.get("/audio-import-formats")
def formats() -> dict:
    return {
        "formats": SUPPORTED,
        "extensions": [".wav", ".mp3", ".aac", ".m4a", ".flac", ".ogg"],
        "mime_types": ["audio/wav", "audio/mpeg", "audio/aac", "audio/mp4", "audio/flac", "audio/ogg"],
        "note": "Audio created in other applications. Imported files are stored unchanged; edits are non-destructive.",
    }


@router.get("/projects/{project_id}/audio-imports")
def list_imports(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    _project(db, project_id)
    rows = db.scalars(select(ExternalAudio).where(ExternalAudio.project_id == project_id).order_by(ExternalAudio.created_at.desc()))
    return [service.serialize_external(row) for row in rows]


@router.post("/projects/{project_id}/audio-imports", status_code=201)
async def create_import(
    project_id: str,
    file: UploadFile = File(...),
    video_id: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    db: Session = Depends(get_db),
) -> dict:
    project = _project(db, project_id)
    if video_id is not None:
        video = db.get(Video, video_id)
        if video is None or video.project_id != project_id:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Video not found"})
    if not file.filename:
        raise AppError(422, "EMPTY_FILE", "No file was provided.")
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename).suffix) as tmp:
        tmp_path = Path(tmp.name)
        size = 0
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > service.MAX_AUDIO_MB * 1024 * 1024:
                tmp_path.unlink(missing_ok=True)
                raise AppError(413, "FILE_TOO_LARGE", f"Audio exceeds the {service.MAX_AUDIO_MB} MB import limit.")
            tmp.write(chunk)
    try:
        row = service.import_external_audio(
            db, project, filename=file.filename, content_type=file.content_type, source_path=tmp_path,
            size_bytes=size, video_id=video_id, notes=notes,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    return service.serialize_external(row)


@router.get("/audio-imports/{audio_id}")
def get_import(audio_id: str, db: Session = Depends(get_db)) -> dict:
    return service.serialize_external(_audio(db, audio_id))


@router.delete("/audio-imports/{audio_id}", status_code=204)
def delete_import(audio_id: str, purge: bool = False, db: Session = Depends(get_db)) -> Response:
    row = _audio(db, audio_id)
    db.delete(row)
    db.commit()
    if purge:
        # explicitly requested removal of OUR stored copy; the user's original file is never touched
        path = storage.absolute_path(row.storage_key)
        path.unlink(missing_ok=True)
    return Response(status_code=204)


def _stream_file(path: Path, media_type: str, filename: str, request: Request) -> Response:
    if not path.is_file():
        raise HTTPException(status_code=410, detail={"code": "FILE_MISSING", "message": "Stored audio file is missing"})
    file_size = path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type=media_type, filename=filename, headers={"Accept-Ranges": "bytes"})
    match = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match:
        raise HTTPException(status_code=416, detail={"code": "INVALID_RANGE", "message": "Invalid byte range"})
    start_raw, end_raw = match.groups()
    if not start_raw and not end_raw:
        raise HTTPException(status_code=416, detail={"code": "INVALID_RANGE", "message": "Invalid byte range"})
    if start_raw:
        start = int(start_raw)
        end = int(end_raw) if end_raw else file_size - 1
    else:
        start = max(0, file_size - int(end_raw))
        end = file_size - 1
    end = min(end, file_size - 1)
    if start > end or start >= file_size:
        raise HTTPException(status_code=416, headers={"Content-Range": f"bytes */{file_size}"}, detail={"code": "RANGE_NOT_SATISFIABLE", "message": "Range outside file"})

    def iterator():
        remaining = end - start + 1
        with path.open("rb") as handle:
            handle.seek(start)
            while remaining > 0:
                chunk = handle.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iterator(), status_code=206, media_type=media_type,
        headers={"Content-Range": f"bytes {start}-{end}/{file_size}", "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)},
    )


@router.get("/audio-imports/{audio_id}/file")
def stream_import(audio_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    row = _audio(db, audio_id)
    return _stream_file(storage.absolute_path(row.storage_key), row.content_type, row.original_filename, request)


@router.get("/audio-imports/{audio_id}/waveform")
def import_waveform(
    audio_id: str,
    start_ms: int = Query(default=0, ge=0),
    end_ms: int = Query(default=30000, ge=1),
    buckets: int = Query(default=600, ge=1, le=4000),
    db: Session = Depends(get_db),
) -> dict:
    row = _audio(db, audio_id)
    return service.import_waveform(db, row, start_ms, end_ms, buckets)


@router.get("/audio-imports/{audio_id}/overview")
def import_overview(audio_id: str, db: Session = Depends(get_db)) -> dict:
    row = _audio(db, audio_id)
    overview = service.cached_overview(row)
    if overview is None:
        return JSONResponse(status_code=404, content={"error": {"code": "NO_WAVEFORM", "message": "No cached waveform."}})
    return overview
