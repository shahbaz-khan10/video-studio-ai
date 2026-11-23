import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Video
from ..schemas import VideoOut
from ..services.storage import storage

router = APIRouter(prefix="/videos", tags=["videos"])

CHUNK_SIZE = 1024 * 1024


def video_to_out(video: Video) -> VideoOut:
    payload = VideoOut.model_validate(video)
    payload.stream_url = f"/api/v1/videos/{video.id}/stream"
    return payload


def find_video(db: Session, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Video not found"})
    return video


@router.get("/{video_id}", response_model=VideoOut)
def get_video(video_id: str, db: Session = Depends(get_db)) -> VideoOut:
    return video_to_out(find_video(db, video_id))


@router.delete("/{video_id}", status_code=204)
def delete_video(video_id: str, db: Session = Depends(get_db)) -> Response:
    video = find_video(db, video_id)
    key = video.storage_key
    project_id = video.project_id
    db.delete(video)
    db.commit()
    path = storage.absolute_path(key)
    if path.is_file():
        path.unlink(missing_ok=True)
    remaining = storage.absolute_path(f"projects/{project_id}/source")
    if remaining.is_dir() and not any(remaining.iterdir()):
        remaining.rmdir()
    return Response(status_code=204)


@router.get("/{video_id}/stream")
def stream_video(video_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    video = find_video(db, video_id)
    path: Path = storage.absolute_path(video.storage_key)
    if not path.is_file():
        raise HTTPException(status_code=410, detail={"code": "FILE_MISSING", "message": "Stored video file is missing"})

    file_size = path.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type=video.content_type, filename=video.original_filename)

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
        suffix_length = int(end_raw)
        start = max(0, file_size - suffix_length)
        end = file_size - 1
    end = min(end, file_size - 1)

    if start > end or start >= file_size:
        raise HTTPException(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
            detail={"code": "RANGE_NOT_SATISFIABLE", "message": "Requested range is outside the file"},
        )

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
        iterator(),
        status_code=206,
        media_type=video.content_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Disposition": f'inline; filename="{video.original_filename}"',
        },
    )
