import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import JobStep, MediaAsset, ProcessingJob, Video, utcnow
from ..services.media import build_frame_index, file_sha256, generate_thumbnails, probe_video
from ..services.storage import storage

STEP_NAMES = [
    "validate_file",
    "compute_checksum",
    "probe_metadata",
    "build_frame_index",
    "create_thumbnails",
    "finalize",
]


def create_import_job(db: Session, project_id: str, video_id: str, job_type: str = "media_import") -> ProcessingJob:
    job = ProcessingJob(
        project_id=project_id,
        video_id=video_id,
        type=job_type,
        status="queued",
        progress=0,
        current_step=STEP_NAMES[0],
    )
    db.add(job)
    db.flush()
    for position, name in enumerate(STEP_NAMES):
        db.add(JobStep(job_id=job.id, position=position, name=name, status="pending"))
    db.commit()
    db.refresh(job)
    return job


def _utcz() -> datetime:
    return datetime.now(timezone.utc)


def _set_step(db: Session, job: ProcessingJob, name: str, status: str, detail: str | None, progress: int) -> None:
    step = next((item for item in job.steps if item.name == name), None)
    if step:
        step.status = status
        step.detail = detail
        if status == "running":
            step.started_at = _utcz()
        if status in {"completed", "failed"}:
            step.finished_at = _utcz()
    job.current_step = name
    job.progress = progress
    db.commit()


def _next_version(db: Session, video_id: str, kind: str) -> int:
    current = db.scalar(
        select(func.max(MediaAsset.version)).where(MediaAsset.video_id == video_id, MediaAsset.kind == kind)
    )
    return int(current or 0) + 1


def _register_asset(
    db: Session,
    video: Video,
    kind: str,
    source_path: Path,
    mime_type: str,
    metadata_json: dict | None = None,
) -> MediaAsset:
    version = _next_version(db, video.id, kind)
    storage_key = storage.asset_key(video.project_id, video.id, kind, version, source_path.name)
    storage.save(storage_key, source_path)
    stored = storage.absolute_path(storage_key)
    asset = MediaAsset(
        project_id=video.project_id,
        video_id=video.id,
        kind=kind,
        version=version,
        storage_key=storage_key,
        mime_type=mime_type,
        size_bytes=stored.stat().st_size,
        checksum_sha256=file_sha256(stored),
        metadata_json=metadata_json or {},
    )
    db.add(asset)
    db.commit()
    return asset


def _apply_probe(video: Video, probed: dict) -> None:
    for key in (
        "duration_ms",
        "width",
        "height",
        "fps",
        "frame_count",
        "pixel_format",
        "color_space",
        "color_range",
        "bit_rate",
        "video_codec",
        "audio_codec",
    ):
        setattr(video, key, probed.get(key))
    video.stream_layout = probed.get("stream_layout") or []
    video.probe_status = probed["probe_status"]
    video.probe_source = probed.get("probe_source")
    metadata = dict(video.metadata_json or {})
    metadata.update(
        {
            k: v
            for k, v in probed.items()
            if k
            not in {
                "duration_ms",
                "width",
                "height",
                "fps",
                "frame_count",
                "pixel_format",
                "color_space",
                "color_range",
                "bit_rate",
                "video_codec",
                "audio_codec",
                "stream_layout",
                "probe_status",
                "probe_source",
            }
        }
    )
    video.metadata_json = metadata


def run_import_job(job_id: str, video_id: str, client_metadata: dict) -> None:
    db = SessionLocal()
    try:
        job = db.get(ProcessingJob, job_id)
        video = db.get(Video, video_id)
        if not job or not video:
            return
        job.status = "running"
        db.commit()

        _set_step(db, job, "validate_file", "running", "Checking stored file", 10)
        path = storage.absolute_path(video.storage_key)
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("Stored video file is missing or empty")
        saved_size = path.stat().st_size
        _set_step(db, job, "validate_file", "completed", f"{saved_size} bytes on disk", 20)

        _set_step(db, job, "compute_checksum", "running", "Verifying checksum", 30)
        video.checksum_sha256 = file_sha256(path)
        db.commit()
        _set_step(db, job, "compute_checksum", "completed", video.checksum_sha256[:16], 40)

        _set_step(db, job, "probe_metadata", "running", "Reading video metadata", 50)
        try:
            probed = probe_video(path)
        except Exception as error:
            probed = {
                "duration_ms": client_metadata.get("duration_ms"),
                "width": client_metadata.get("width"),
                "height": client_metadata.get("height"),
                "fps": None,
                "frame_count": None,
                "pixel_format": None,
                "color_space": None,
                "color_range": None,
                "bit_rate": None,
                "video_codec": None,
                "audio_codec": None,
                "stream_layout": [],
                "probe_status": "client_metadata",
                "probe_source": "browser",
                "probe_output": str(error)[:1000],
            }
        _apply_probe(video, probed)
        db.commit()
        _set_step(db, job, "probe_metadata", "completed", f"Source: {probed['probe_source']}", 60)

        _set_step(db, job, "build_frame_index", "running", "Mapping time to frames", 70)
        if probed["probe_status"] == "probed":
            with tempfile.TemporaryDirectory(prefix="cinema-index-") as scratch:
                index_path = Path(scratch) / "frame-index.json"
                index_path.write_text(
                    json.dumps(build_frame_index(probed), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                _register_asset(
                    db,
                    video,
                    "frame_index",
                    index_path,
                    "application/json",
                    {
                        "fps": probed.get("fps"),
                        "frame_count": probed.get("frame_count"),
                        "duration_ms": probed.get("duration_ms"),
                    },
                )
        _set_step(db, job, "build_frame_index", "completed", "Frame map stored as versioned asset", 78)

        _set_step(db, job, "create_thumbnails", "running", "Extracting preview frames", 85)
        if probed["probe_status"] == "probed":
            with tempfile.TemporaryDirectory(prefix="cinema-thumbs-") as scratch:
                for thumbnail in generate_thumbnails(path, Path(scratch), probed):
                    _register_asset(
                        db,
                        video,
                        "thumbnail",
                        thumbnail["path"],
                        "image/jpeg",
                        {
                            "timestamp_ms": thumbnail["timestamp_ms"],
                            "width": thumbnail["width"],
                            "height": thumbnail["height"],
                        },
                    )
        _set_step(db, job, "create_thumbnails", "completed", "Sparse preview frames stored", 92)

        _set_step(db, job, "finalize", "running", "Saving video record", 96)
        job.status = "completed"
        job.progress = 100
        job.error_code = None
        job.error_message = None
        db.commit()
        _set_step(db, job, "finalize", "completed", "Video ready for preview", 100)
    except Exception as error:
        if "job" in locals() and job:
            job.status = "failed"
            job.error_code = "IMPORT_FAILED"
            job.error_message = str(error)[:1000]
            for step in job.steps:
                if step.status == "running":
                    step.status = "failed"
                    step.detail = str(error)[:500]
                    step.finished_at = utcnow()
            db.commit()
    finally:
        db.close()
