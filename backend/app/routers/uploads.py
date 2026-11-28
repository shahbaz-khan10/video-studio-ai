import hashlib
import math
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..services import job_queue
from ..database import get_db
from ..models import Project, UploadSession, Video
from ..schemas import JobOut, UploadAccepted, UploadSessionCreate, UploadSessionOut
from ..services.jobs import create_import_job, run_import_job
from ..services.media import AppError, validate_video_file
from ..services.storage import storage
from .projects import video_to_out

router = APIRouter(prefix="/uploads", tags=["uploads"])
READ_CHUNK = 1024 * 1024


def _clean_int(value: int | None, maximum: int) -> int | None:
    if value is None or value <= 0:
        return None
    return min(value, maximum)


def _client_metadata(duration_ms: int | None, width: int | None, height: int | None) -> dict:
    return {
        "duration_ms": _clean_int(duration_ms, 24 * 60 * 60 * 1000),
        "width": _clean_int(width, 16384),
        "height": _clean_int(height, 16384),
    }


def _reject_duplicate(db: Session, project_id: str, checksum: str, size_bytes: int) -> None:
    existing = db.scalar(
        select(Video.id).where(
            Video.project_id == project_id,
            Video.checksum_sha256 == checksum,
            Video.size_bytes == size_bytes,
        )
    )
    if existing:
        raise AppError(409, "DUPLICATE_VIDEO", "This video has already been uploaded to the project.")


def _attach_video(
    db: Session,
    background: BackgroundTasks,
    project_id: str,
    filename: str,
    content_type: str,
    storage_key: str,
    size_bytes: int,
    checksum: str,
    metadata: dict,
) -> UploadAccepted:
    video = Video(
        project_id=project_id,
        original_filename=Path(filename).name[:255],
        storage_key=storage_key,
        content_type=content_type,
        size_bytes=size_bytes,
        checksum_sha256=checksum,
        duration_ms=metadata["duration_ms"],
        width=metadata["width"],
        height=metadata["height"],
        probe_status="pending",
        metadata_json={"browser": metadata},
    )
    db.add(video)
    db.flush()
    job = create_import_job(db, project_id, video.id)
    db.commit()
    db.refresh(video)
    db.refresh(job)
    job_queue.submit(background, job, metadata=metadata)
    return UploadAccepted(video=video_to_out(video), job=JobOut.model_validate(job))


def _find_session(db: Session, session_id: str) -> UploadSession:
    session = db.get(UploadSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Upload session not found"})
    return session


def _session_expired(session: UploadSession) -> bool:
    expires = session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= datetime.now(timezone.utc)


def _session_response(session: UploadSession) -> UploadSessionOut:
    return UploadSessionOut.model_validate(session)


@router.post(
    "/projects/{project_id}/videos",
    response_model=UploadAccepted,
    status_code=status.HTTP_201_CREATED,
)
async def upload_video(
    project_id: str,
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    duration_ms: int | None = Form(default=None),
    width: int | None = Form(default=None),
    height: int | None = Form(default=None),
    db: Session = Depends(get_db),
) -> UploadAccepted:
    if not db.get(Project, project_id):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})

    declared = file.size
    if declared is None:
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            declared = 0

    head = await file.read(64)
    validate_video_file(head, file.content_type, declared or len(head))

    temp = tempfile.NamedTemporaryFile(prefix="cinema-upload-", suffix=".part", delete=False)
    temp_path = Path(temp.name)
    digest = hashlib.sha256()
    total = 0
    try:
        with temp:
            temp.write(head)
            digest.update(head)
            total += len(head)
            while True:
                chunk = await file.read(READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if declared and total > declared:
                    raise AppError(413, "FILE_TOO_LARGE", "Uploaded file is larger than declared size.")
                if total > settings.max_upload_bytes:
                    raise AppError(413, "FILE_TOO_LARGE", "Uploaded file exceeds the configured limit.")
                temp.write(chunk)
                digest.update(chunk)
        validate_video_file(head, file.content_type, total)

        checksum = digest.hexdigest()
        _reject_duplicate(db, project_id, checksum, total)
        suffix = Path(file.filename or "video.mp4").suffix or ".mp4"
        storage_key = storage.video_key(project_id, suffix)
        storage.save(storage_key, temp_path)
        return _attach_video(
            db,
            background,
            project_id,
            file.filename or "video.mp4",
            file.content_type or "video/mp4",
            storage_key,
            total,
            checksum,
            _client_metadata(duration_ms, width, height),
        )
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


@router.post(
    "/projects/{project_id}/sessions",
    response_model=UploadSessionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_upload_session(
    project_id: str,
    payload: UploadSessionCreate,
    db: Session = Depends(get_db),
) -> UploadSessionOut:
    if not db.get(Project, project_id):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    if payload.size_bytes > settings.max_upload_bytes:
        raise AppError(413, "FILE_TOO_LARGE", f"Video exceeds the {settings.max_upload_mb} MB upload limit.")
    if payload.mime_type not in settings.allowed_content_types:
        raise AppError(422, "UNSUPPORTED_MEDIA_TYPE", f"Unsupported content type: {payload.mime_type}")

    chunk_size = max(1, settings.upload_chunk_mb) * 1024 * 1024
    session = UploadSession(
        project_id=project_id,
        filename=Path(payload.filename).name[:255],
        content_type=payload.mime_type,
        size_bytes=payload.size_bytes,
        chunk_size=chunk_size,
        total_chunks=math.ceil(payload.size_bytes / chunk_size),
        client_metadata=_client_metadata(payload.duration_ms, payload.width, payload.height),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return _session_response(session)


@router.get("/sessions/{session_id}", response_model=UploadSessionOut)
def get_upload_session(session_id: str, db: Session = Depends(get_db)) -> UploadSessionOut:
    return _session_response(_find_session(db, session_id))


@router.put("/sessions/{session_id}/chunks/{index}", response_model=UploadSessionOut)
async def upload_chunk(
    session_id: str,
    index: int,
    request: Request,
    db: Session = Depends(get_db),
) -> UploadSessionOut:
    session = _find_session(db, session_id)
    if session.status != "pending":
        raise AppError(409, "SESSION_CLOSED", "This upload session is no longer accepting chunks.")
    if _session_expired(session):
        raise AppError(410, "SESSION_EXPIRED", "This upload session has expired.")

    if index < 0 or index >= session.total_chunks:
        raise AppError(422, "INVALID_CHUNK_INDEX", "Chunk index is outside the upload range.")
    body = await request.body()
    expected = min(session.chunk_size, session.size_bytes - index * session.chunk_size)
    if not body or len(body) != expected:
        raise AppError(422, "INVALID_CHUNK_SIZE", f"Chunk must contain exactly {expected} bytes.")

    storage.write_bytes(storage.upload_part_key(session.project_id, session.id, index), body)
    uploaded = sorted(set(session.uploaded_chunks or []) | {index})
    session.uploaded_chunks = uploaded
    session.received_bytes = sum(
        min(session.chunk_size, session.size_bytes - item * session.chunk_size) for item in uploaded
    )
    db.commit()
    db.refresh(session)
    return _session_response(session)


@router.post(
    "/sessions/{session_id}/complete",
    response_model=UploadAccepted,
    status_code=status.HTTP_201_CREATED,
)
def complete_upload_session(
    session_id: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> UploadAccepted:
    session = _find_session(db, session_id)
    if session.status != "pending":
        raise AppError(409, "SESSION_CLOSED", "This upload session has already been completed.")
    if _session_expired(session):
        raise AppError(410, "SESSION_EXPIRED", "This upload session has expired.")
    expected_chunks = list(range(session.total_chunks))
    if sorted(session.uploaded_chunks or []) != expected_chunks:
        raise AppError(409, "UPLOAD_INCOMPLETE", "All chunks must be uploaded before completing.")

    project_id = session.project_id
    session_key = session.id
    temp = tempfile.NamedTemporaryFile(prefix="cinema-assemble-", suffix=".part", delete=False)
    temp_path = Path(temp.name)
    digest = hashlib.sha256()
    total = 0
    try:
        with temp:
            for index in expected_chunks:
                part = storage.absolute_path(storage.upload_part_key(session.project_id, session.id, index))
                if not part.is_file():
                    raise AppError(409, "UPLOAD_INCOMPLETE", f"Chunk {index} is missing.")
                with part.open("rb") as handle:
                    while True:
                        chunk = handle.read(READ_CHUNK)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > session.size_bytes:
                            raise AppError(422, "SIZE_MISMATCH", "Assembled file is larger than declared size.")
                        temp.write(chunk)
                        digest.update(chunk)
        if total != session.size_bytes:
            raise AppError(422, "SIZE_MISMATCH", "Assembled file size does not match the declared size.")
        with temp_path.open("rb") as handle:
            validate_video_file(handle.read(64), session.content_type, total)

        checksum = digest.hexdigest()
        _reject_duplicate(db, session.project_id, checksum, total)
        suffix = Path(session.filename).suffix or ".mp4"
        storage_key = storage.video_key(session.project_id, suffix)
        storage.save(storage_key, temp_path)
        result = _attach_video(
            db,
            background,
            session.project_id,
            session.filename,
            session.content_type,
            storage_key,
            total,
            checksum,
            session.client_metadata or {},
        )
        db.delete(session)
        db.commit()
        storage.delete_upload_session(project_id, session_key)
        return result
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_upload_session(session_id: str, db: Session = Depends(get_db)) -> Response:
    session = _find_session(db, session_id)
    project_id = session.project_id
    session_key = session.id
    db.delete(session)
    db.commit()
    storage.delete_upload_session(project_id, session_key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
