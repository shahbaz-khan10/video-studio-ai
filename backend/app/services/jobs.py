import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete as db_delete
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..models import (
    Character,
    CharacterAssignment,
    FaceTrack,
    JobStep,
    MediaAsset,
    ProcessingJob,
    SpeakerTurn,
    Transcript,
    TranscriptSegment,
    Video,
    VisualCandidate,
    new_id,
    utcnow,
)
from ..services import characters as characters_service
from ..services import diarization as diarization_service
from ..services import transcription as transcription_service
from ..services import vision as vision_service
from ..services.audio import (
    STEM_LABELS,
    AudioCancelled,
    MIME_BY_FORMAT,
    compare_stems,
    extract_audio,
    probe_audio_stream,
    separate_audio,
    validate_audio_file,
)
from ..services.media import AppError, build_frame_index, file_sha256, generate_thumbnails, probe_video
from ..services.storage import storage

STEP_NAMES = [
    "validate_file",
    "compute_checksum",
    "probe_metadata",
    "build_frame_index",
    "create_thumbnails",
    "finalize",
]
AUDIO_STEP_NAMES = [
    "validate_source",
    "probe_audio",
    "extract_audio",
    "separate_stems",
    "validate_stems",
    "finalize",
]
TRANSCRIPTION_STEP_NAMES = [
    "select_audio",
    "prepare_engine",
    "transcribe",
    "persist_transcript",
    "finalize",
]
CHARACTER_STEP_NAMES = [
    "select_inputs",
    "diarize_speakers",
    "detect_faces",
    "fuse_identities",
    "persist_characters",
    "finalize",
]


class JobCancelled(Exception):
    pass


def _job_status(job_id: str) -> str | None:
    with SessionLocal() as check_db:
        job = check_db.get(ProcessingJob, job_id)
        return job.status if job else None


def _ensure_active(job_id: str) -> None:
    status = _job_status(job_id)
    if status is None or status == "cancelled":
        raise JobCancelled("Job was cancelled or removed")


def _steps_for_type(job_type: str) -> list[str]:
    if job_type == "audio_separation":
        return AUDIO_STEP_NAMES
    if job_type == "transcription":
        return TRANSCRIPTION_STEP_NAMES
    if job_type == "character_detection":
        return CHARACTER_STEP_NAMES
    return STEP_NAMES


def create_import_job(
    db: Session,
    project_id: str,
    video_id: str,
    job_type: str = "media_import",
    steps: list[str] | None = None,
    options: dict | None = None,
) -> ProcessingJob:
    step_names = steps or _steps_for_type(job_type)
    job = ProcessingJob(
        project_id=project_id,
        video_id=video_id,
        type=job_type,
        status="queued",
        progress=0,
        current_step=step_names[0],
        options_json=options or {},
    )
    db.add(job)
    db.flush()
    for position, name in enumerate(step_names):
        db.add(JobStep(job_id=job.id, position=position, name=name, status="pending"))
    db.commit()
    db.refresh(job)
    return job


def create_audio_job(db: Session, project_id: str, video_id: str) -> ProcessingJob:
    return create_import_job(db, project_id, video_id, "audio_separation", AUDIO_STEP_NAMES)


def create_transcription_job(
    db: Session,
    project_id: str,
    video_id: str,
    options: dict | None = None,
) -> ProcessingJob:
    active = db.scalar(
        select(ProcessingJob).where(
            ProcessingJob.video_id == video_id,
            ProcessingJob.type == "transcription",
            ProcessingJob.status.in_(["queued", "running"]),
        )
    )
    if active:
        raise AppError(
            409,
            "TRANSCRIPTION_JOB_ACTIVE",
            "A transcription job is already running for this video.",
        )
    return create_import_job(
        db,
        project_id,
        video_id,
        "transcription",
        TRANSCRIPTION_STEP_NAMES,
        options or {},
    )


def create_character_job(
    db: Session,
    project_id: str,
    video_id: str,
    options: dict | None = None,
) -> ProcessingJob:
    active = db.scalar(
        select(ProcessingJob).where(
            ProcessingJob.video_id == video_id,
            ProcessingJob.type == "character_detection",
            ProcessingJob.status.in_(["queued", "running"]),
        )
    )
    if active:
        raise AppError(
            409,
            "CHARACTER_DETECTION_JOB_ACTIVE",
            "A character detection job is already running for this video.",
        )
    return create_import_job(
        db,
        project_id,
        video_id,
        "character_detection",
        CHARACTER_STEP_NAMES,
        options or {},
    )


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
        _ensure_active(job_id)

        _set_step(db, job, "validate_file", "running", "Checking stored file", 10)
        _ensure_active(job_id)
        path = storage.absolute_path(video.storage_key)
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("Stored video file is missing or empty")
        saved_size = path.stat().st_size
        _set_step(db, job, "validate_file", "completed", f"{saved_size} bytes on disk", 20)
        _ensure_active(job_id)

        _set_step(db, job, "compute_checksum", "running", "Verifying checksum", 30)
        _ensure_active(job_id)
        video.checksum_sha256 = file_sha256(path)
        db.commit()
        _set_step(db, job, "compute_checksum", "completed", video.checksum_sha256[:16], 40)
        _ensure_active(job_id)

        _set_step(db, job, "probe_metadata", "running", "Reading video metadata", 50)
        _ensure_active(job_id)
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
        _ensure_active(job_id)

        _set_step(db, job, "build_frame_index", "running", "Mapping time to frames", 70)
        _ensure_active(job_id)
        if probed["probe_status"] == "probed":
            with tempfile.TemporaryDirectory(prefix="cinema-index-") as scratch:
                index_path = Path(scratch) / "frame-index.json"
                index_path.write_text(
                    json.dumps(build_frame_index(probed), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                _ensure_active(job_id)
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
        _ensure_active(job_id)

        _set_step(db, job, "create_thumbnails", "running", "Extracting preview frames", 85)
        _ensure_active(job_id)
        if probed["probe_status"] == "probed":
            with tempfile.TemporaryDirectory(prefix="cinema-thumbs-") as scratch:
                for thumbnail in generate_thumbnails(path, Path(scratch), probed):
                    _ensure_active(job_id)
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
        _ensure_active(job_id)

        _set_step(db, job, "finalize", "running", "Saving video record", 96)
        _ensure_active(job_id)
        job.status = "completed"
        job.progress = 100
        job.error_code = None
        job.error_message = None
        db.commit()
        _set_step(db, job, "finalize", "completed", "Video ready for preview", 100)
    except JobCancelled:
        current = db.get(ProcessingJob, job_id)
        if current:
            current.status = "cancelled"
            current.error_code = "CANCELLED"
            current.error_message = "Cancelled by user"
            for step in current.steps:
                if step.status in {"pending", "running"}:
                    step.status = "cancelled"
                    step.detail = "Cancelled by user"
                    step.finished_at = utcnow()
            db.commit()
    except Exception as error:
        current = db.get(ProcessingJob, job_id) if "job_id" in locals() else None
        if current and current.status != "cancelled":
            current.status = "failed"
            current.error_code = "IMPORT_FAILED"
            current.error_message = str(error)[:1000]
            for step in current.steps:
                if step.status == "running":
                    step.status = "failed"
                    step.detail = str(error)[:500]
                    step.finished_at = utcnow()
            db.commit()
    finally:
        db.close()


def run_audio_separation_job(job_id: str, video_id: str) -> None:
    db = SessionLocal()
    try:
        job = db.get(ProcessingJob, job_id)
        video = db.get(Video, video_id)
        if not job or not video:
            return
        job.status = "running"
        db.commit()
        _ensure_active(job_id)

        _set_step(db, job, "validate_source", "running", "Checking stored source video", 8)
        _ensure_active(job_id)
        video_path = storage.absolute_path(video.storage_key)
        if not video_path.is_file() or video_path.stat().st_size == 0:
            raise AppError(410, "FILE_MISSING", "Stored video file is missing or empty")
        _set_step(db, job, "validate_source", "completed", f"{video_path.stat().st_size} bytes on disk", 16)

        _set_step(db, job, "probe_audio", "running", "Reading source audio properties", 22)
        _ensure_active(job_id)
        audio_info = probe_audio_stream(video_path)
        if not audio_info.get("sample_rate") or not audio_info.get("channels"):
            raise AppError(422, "AUDIO_PROBE_INCOMPLETE", "Could not determine source sample rate and channels")
        _set_step(
            db,
            job,
            "probe_audio",
            "completed",
            f"{audio_info['codec']} · {audio_info['sample_rate']} Hz · {audio_info['channels']} ch",
            30,
        )

        _set_step(db, job, "extract_audio", "running", "Extracting a separate audio copy", 34)
        _ensure_active(job_id)
        output_format = settings.audio_extract_format.strip().lower()
        with tempfile.TemporaryDirectory(prefix="cinema-audio-") as scratch:
            scratch_dir = Path(scratch)
            source_audio = extract_audio(video_path, scratch_dir, output_format, audio_info)
            source_validation = validate_audio_file(
                source_audio,
                expected_duration_ms=None,
                expected_rate=audio_info.get("sample_rate"),
                expected_channels=audio_info.get("channels"),
                allow_silent=True,
            )
            if not source_validation["ok"]:
                raise RuntimeError(f"Extracted audio failed validation: {', '.join(source_validation['issues'])}")
            _set_step(db, job, "extract_audio", "completed", f"{source_audio.stat().st_size} bytes extracted", 40)

            _set_step(db, job, "separate_stems", "running", "Separating audio stems", 42)
            _ensure_active(job_id)

            def separation_progress(fraction: float) -> None:
                current = db.get(ProcessingJob, job_id)
                if current and current.status != "cancelled":
                    current.progress = 42 + int(max(0.0, min(1.0, fraction)) * 36)
                    db.commit()

            def check_active() -> None:
                status = _job_status(job_id)
                if status is None or status == "cancelled":
                    raise AudioCancelled("Job was cancelled or removed")

            result = separate_audio(source_audio, scratch_dir, check_active, separation_progress)
            _ensure_active(job_id)
            _set_step(
                db,
                job,
                "separate_stems",
                "completed",
                f"{result['backend']} · {result['model']}",
                80,
            )

            _set_step(db, job, "validate_stems", "running", "Checking generated stems", 84)
            _ensure_active(job_id)
            stem_files = {role: result["files"][role] for role in ("dialogue", "music", "sfx", "ambience")}
            missing = [role for role, path in stem_files.items() if not path.is_file() or path.stat().st_size == 0]
            if missing:
                raise RuntimeError(f"Separation did not produce usable stems: {', '.join(missing)}")
            expected_duration = source_validation.get("duration_ms")
            expected_rate = source_validation.get("sample_rate")
            validation = {
                role: validate_audio_file(
                    path,
                    expected_duration_ms=expected_duration,
                    expected_rate=expected_rate,
                    expected_channels=None,
                    allow_silent=True,
                )
                for role, path in stem_files.items()
            }
            invalid = [role for role, report in validation.items() if not report["ok"]]
            if invalid:
                raise RuntimeError(f"Stem validation failed for: {', '.join(invalid)}")
            comparison = compare_stems(source_audio, stem_files)
            _set_step(db, job, "validate_stems", "completed", "Duration, sample rate and decode checks passed", 92)

            _set_step(db, job, "finalize", "running", "Saving versioned audio artifacts", 96)
            _ensure_active(job_id)
            source_asset = _register_asset(
                db,
                video,
                "source_audio",
                source_audio,
                MIME_BY_FORMAT.get(output_format, "application/octet-stream"),
                {
                    "format": output_format,
                    "source_codec": audio_info.get("codec"),
                    "sample_rate": source_validation["sample_rate"],
                    "channels": source_validation["channels"],
                    "duration_ms": source_validation["duration_ms"],
                    "validation": source_validation,
                },
            )
            stem_assets = {}
            for role, path in stem_files.items():
                _ensure_active(job_id)
                stem_assets[role] = _register_asset(
                    db,
                    video,
                    f"stem_{role}",
                    path,
                    "audio/wav",
                    {
                        "role": role,
                        "label": STEM_LABELS[role],
                        "backend": result["backend"],
                        "model": result["model"],
                        "confidence": result["confidence"],
                        "validation": validation[role],
                        "comparison": comparison["stems"].get(role, {}),
                        "limitations": result["limitations"],
                    },
                )

            metadata = dict(video.metadata_json or {})
            metadata["audio"] = {
                "status": "completed",
                "backend": result["backend"],
                "model": result["model"],
                "confidence": result["confidence"],
                "source": {
                    "asset_id": source_asset.id,
                    "format": output_format,
                    "codec": audio_info.get("codec"),
                    "sample_rate": source_validation["sample_rate"],
                    "channels": source_validation["channels"],
                    "duration_ms": source_validation["duration_ms"],
                    "validation": source_validation,
                },
                "stems": {
                    role: {
                        "asset_id": asset.id,
                        "confidence": result["confidence"],
                        "validation": validation[role],
                        "comparison": comparison["stems"].get(role, {}),
                    }
                    for role, asset in stem_assets.items()
                },
                "comparison": comparison,
                "limitations": result["limitations"],
                "notes": result.get("notes", []),
            }
            video.metadata_json = metadata
            db.commit()

            job.status = "completed"
            job.progress = 100
            job.error_code = None
            job.error_message = None
            db.commit()
            _set_step(db, job, "finalize", "completed", "Source audio and four stems are ready", 100)
    except (AudioCancelled, JobCancelled):
        current = db.get(ProcessingJob, job_id)
        if current:
            current.status = "cancelled"
            current.error_code = "CANCELLED"
            current.error_message = "Cancelled by user"
            for step in current.steps:
                if step.status in {"pending", "running"}:
                    step.status = "cancelled"
                    step.detail = "Cancelled by user"
                    step.finished_at = utcnow()
            db.commit()
    except Exception as error:
        current = db.get(ProcessingJob, job_id) if "job_id" in locals() else None
        if current and current.status != "cancelled":
            current.status = "failed"
            current.error_code = "AUDIO_SEPARATION_FAILED" if current.type == "audio_separation" else "IMPORT_FAILED"
            current.error_message = str(error)[:1000]
            for step in current.steps:
                if step.status == "running":
                    step.status = "failed"
                    step.detail = str(error)[:500]
                    step.finished_at = utcnow()
            db.commit()
    finally:
        db.close()


def _latest_asset_for_kind(db: Session, video_id: str, kind: str) -> MediaAsset | None:
    return db.scalar(
        select(MediaAsset)
        .where(MediaAsset.video_id == video_id, MediaAsset.kind == kind)
        .order_by(MediaAsset.version.desc(), MediaAsset.created_at.desc())
    )


def _select_transcription_source(
    db: Session, video: Video, prefer_dialogue_stem: bool
) -> tuple[Path, str, int]:
    if prefer_dialogue_stem:
        asset = _latest_asset_for_kind(db, video.id, "stem_dialogue")
        if asset:
            path = storage.absolute_path(asset.storage_key)
            validation = (asset.metadata_json or {}).get("validation") or {}
            if path.is_file() and path.stat().st_size > 0 and validation.get("ok") is True:
                duration = int(validation.get("duration_ms") or video.duration_ms or 0)
                return path, "dialogue_stem", duration

    asset = _latest_asset_for_kind(db, video.id, "source_audio")
    if asset:
        path = storage.absolute_path(asset.storage_key)
        validation = (asset.metadata_json or {}).get("validation") or {}
        if path.is_file() and path.stat().st_size > 0 and validation.get("ok") is True:
            duration = int(validation.get("duration_ms") or video.duration_ms or 0)
            return path, "source_audio", duration

    video_path = storage.absolute_path(video.storage_key)
    if not video_path.is_file() or video_path.stat().st_size == 0:
        raise AppError(410, "FILE_MISSING", "Stored video file is missing or empty")
    audio_info = probe_audio_stream(video_path)
    duration = int(audio_info.get("duration_ms") or video.duration_ms or 0)
    return video_path, "original_video", duration


def _normalized_transcript_segments(raw: list, duration_ms: int) -> list[dict]:
    normalized: list[dict] = []
    for item in raw:
        text = str(getattr(item, "text", "") or "").strip()
        if not text:
            continue
        start = max(0, int(getattr(item, "start_ms", 0)))
        end = max(start, int(getattr(item, "end_ms", start)))
        if duration_ms > 0:
            if start >= duration_ms:
                continue
            end = min(end, duration_ms)
        if end <= start:
            end = min(duration_ms, start + 1) if duration_ms > 0 else start + 1
        words = []
        for word in getattr(item, "words", None) or []:
            word_text = str(word.get("text") or "").strip()
            if not word_text:
                continue
            word_start = max(0, int(word.get("start_ms") or 0))
            word_end = max(word_start, int(word.get("end_ms") or word_start))
            if duration_ms > 0:
                word_start = min(word_start, duration_ms)
                word_end = min(word_end, duration_ms)
            words.append(
                {
                    "start_ms": word_start,
                    "end_ms": word_end,
                    "text": word_text,
                    "confidence": word.get("confidence"),
                }
            )
        confidence = getattr(item, "confidence", None)
        normalized.append(
            {
                "start_ms": start,
                "end_ms": end,
                "text": text,
                "confidence": round(float(confidence), 4) if confidence is not None else None,
                "language": getattr(item, "language", None),
                "words": words,
            }
        )
    normalized.sort(key=lambda item: (item["start_ms"], item["end_ms"]))
    return normalized


def run_transcription_job(job_id: str, video_id: str, options: dict | None = None) -> None:
    db = SessionLocal()
    try:
        job = db.get(ProcessingJob, job_id)
        video = db.get(Video, video_id)
        if not job or not video:
            return
        job_options = options or job.options_json or {}
        language = str(job_options.get("language") or "auto").strip().lower()
        if language not in settings.supported_transcription_languages:
            raise AppError(
                422,
                "UNSUPPORTED_TRANSCRIPTION_LANGUAGE",
                f"Unsupported transcription language: {language}. Supported: {', '.join(sorted(settings.supported_transcription_languages))}",
            )
        prefer_dialogue_stem = bool(job_options.get("prefer_dialogue_stem", True))
        word_timestamps = bool(job_options.get("word_timestamps", True))

        job.status = "running"
        db.commit()
        _ensure_active(job_id)

        _set_step(db, job, "select_audio", "running", "Choosing dialogue or source audio", 8)
        _ensure_active(job_id)
        audio_path, source, duration_ms = _select_transcription_source(db, video, prefer_dialogue_stem)
        _set_step(db, job, "select_audio", "completed", f"{source} · {duration_ms} ms", 20)

        _set_step(db, job, "prepare_engine", "running", "Preparing local speech recognition", 28)
        _ensure_active(job_id)
        info = transcription_service.production_info()
        _set_step(db, job, "prepare_engine", "completed", str(info["effective_backend"]), 40)

        _set_step(db, job, "transcribe", "running", "Recognising dialogue", 45)
        _ensure_active(job_id)

        def check_active() -> None:
            status = _job_status(job_id)
            if status is None or status == "cancelled":
                raise JobCancelled("Job was cancelled or removed")

        def asr_progress(end_seconds: float) -> None:
            current = db.get(ProcessingJob, job_id)
            if not current or current.status == "cancelled":
                return
            if duration_ms > 0:
                fraction = max(0.0, min(1.0, (float(end_seconds) * 1000) / duration_ms))
            else:
                fraction = max(0.0, min(1.0, float(end_seconds) / 3600.0))
            current.progress = 45 + int(fraction * 43)
            db.commit()

        result = transcription_service.transcribe_file(
            audio_path,
            language=language,
            word_timestamps=word_timestamps,
            check_active=check_active,
            on_progress=asr_progress,
        )
        check_active()
        segments = _normalized_transcript_segments(result.segments, duration_ms)
        _set_step(
            db,
            job,
            "transcribe",
            "completed",
            f"{len(segments)} segments · {result.language}",
            90,
        )

        _set_step(db, job, "persist_transcript", "running", "Saving transcript revision", 92)
        # Claim the row before writing anything. The UPDATE takes a SQLite row
        # lock that is held until the transcript and final status commit
        # together, so a cancel cannot land between these steps.
        claim = db.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id, ProcessingJob.status == "running")
            .values(current_step="persist_transcript", progress=92)
        )
        if claim.rowcount != 1:
            raise JobCancelled("Job was cancelled before the transcript was saved")
        previous_transcript = db.scalar(
            select(Transcript)
            .where(Transcript.video_id == video.id)
            .order_by(Transcript.version.desc(), Transcript.created_at.desc())
        )
        previous_links: list[tuple[TranscriptSegment, CharacterAssignment]] = []
        if previous_transcript:
            assignment_by_segment = {
                item.segment_id: item
                for item in db.scalars(
                    select(CharacterAssignment).where(CharacterAssignment.video_id == video.id)
                )
            }
            for old_segment in db.scalars(
                select(TranscriptSegment).where(TranscriptSegment.transcript_id == previous_transcript.id)
            ):
                assignment = assignment_by_segment.get(old_segment.id)
                if assignment and assignment.character_id:
                    previous_links.append((old_segment, assignment))
        version = int(
            db.scalar(select(func.max(Transcript.version)).where(Transcript.video_id == video.id)) or 0
        ) + 1
        metadata = {
            "duration_ms": duration_ms,
            "timeline_offset_ms": 0,
            "source_file": audio_path.name,
            "limitations": list(result.limitations),
            "notes": list(result.notes),
        }
        transcript = Transcript(
            id=new_id(),
            project_id=video.project_id,
            video_id=video.id,
            job_id=job.id,
            version=version,
            status="completed",
            language=result.language or language,
            source=source,
            backend=result.backend,
            model=result.model,
            word_timestamps=word_timestamps,
            revision=1,
            metadata_json=metadata,
        )
        db.add(transcript)
        db.flush()
        new_segments: list[TranscriptSegment] = []
        for position, segment in enumerate(segments):
            row = TranscriptSegment(
                id=new_id(),
                transcript_id=transcript.id,
                position=position,
                start_ms=segment["start_ms"],
                end_ms=segment["end_ms"],
                text=segment["text"],
                confidence=segment["confidence"],
                language=segment["language"],
                words=segment["words"],
            )
            db.add(row)
            new_segments.append(row)

        migrated = 0
        for new_segment in new_segments:
            best_ratio = 0.0
            best_assignment: CharacterAssignment | None = None
            for old_segment, assignment in previous_links:
                start = max(new_segment.start_ms, old_segment.start_ms)
                end = min(new_segment.end_ms, old_segment.end_ms)
                if end <= start:
                    continue
                ratio = (end - start) / max(1, new_segment.end_ms - new_segment.start_ms)
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_assignment = assignment
            if best_assignment is not None and best_ratio >= 0.6:
                db.add(
                    CharacterAssignment(
                        id=new_id(),
                        character_id=best_assignment.character_id,
                        segment_id=new_segment.id,
                        video_id=video.id,
                        source="transcript_migration",
                        confidence=best_assignment.confidence,
                        reviewed=best_assignment.reviewed,
                    )
                )
                migrated += 1
        if migrated:
            notes = list(metadata.get("notes") or [])
            notes.append(f"Migrated {migrated} character assignment(s) to the new transcript version.")
            metadata["notes"] = notes
            transcript.metadata_json = metadata

        character_meta = dict((video.metadata_json or {}).get("characters") or {})
        if character_meta:
            character_meta["transcript_id"] = transcript.id
            character_meta["transcript_version"] = version
            video_meta = dict(video.metadata_json or {})
            video_meta["characters"] = character_meta
            video.metadata_json = video_meta

        persist_step = next(item for item in job.steps if item.name == "persist_transcript")
        persist_step.status = "completed"
        persist_step.detail = f"Transcript v{version} saved"
        persist_step.finished_at = _utcz()
        finalize_step = next(item for item in job.steps if item.name == "finalize")
        finalize_step.status = "running"
        finalize_step.started_at = _utcz()
        job.current_step = "finalize"
        job.progress = 98

        final_claim = db.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id, ProcessingJob.status == "running")
            .values(
                status="completed",
                current_step="finalize",
                progress=100,
                error_code=None,
                error_message=None,
            )
        )
        if final_claim.rowcount != 1:
            db.rollback()
            raise JobCancelled("Job was cancelled before finalising the transcript")
        db.commit()
        db.refresh(job)
        _set_step(db, job, "finalize", "completed", "Transcript is ready for review", 100)
    except (JobCancelled, transcription_service.TranscriptionCancelled):
        current = db.get(ProcessingJob, job_id)
        if current:
            if current.status != "cancelled":
                current.status = "cancelled"
                current.error_code = "CANCELLED"
                current.error_message = "Cancelled by user"
            for step in current.steps:
                if step.status in {"pending", "running"}:
                    step.status = "cancelled"
                    step.detail = "Cancelled by user"
                    step.finished_at = utcnow()
            db.commit()
    except Exception as error:
        current = db.get(ProcessingJob, job_id) if "job_id" in locals() else None
        if current and current.status != "cancelled":
            current.status = "failed"
            current.error_code = "TRANSCRIPTION_FAILED"
            current.error_message = str(error)[:1000]
            for step in current.steps:
                if step.status == "running":
                    step.status = "failed"
                    step.detail = str(error)[:500]
                    step.finished_at = utcnow()
            db.commit()
    finally:
        db.close()


def run_character_job(job_id: str, video_id: str, options: dict | None = None) -> None:
    db = SessionLocal()
    temp_dir: Path | None = None
    try:
        job = db.get(ProcessingJob, job_id)
        video = db.get(Video, video_id)
        if not job or not video:
            return
        job_options = options or job.options_json or {}
        prefer_dialogue_stem = bool(job_options.get("prefer_dialogue_stem", True))
        detect_faces = bool(job_options.get("detect_faces", True))
        try:
            max_candidates = max(0, min(120, int(job_options.get("max_candidates", settings.visual_max_candidates))))
        except (TypeError, ValueError):
            max_candidates = settings.visual_max_candidates

        transcript = db.scalar(
            select(Transcript)
            .where(Transcript.video_id == video.id)
            .order_by(Transcript.version.desc(), Transcript.created_at.desc())
        )
        if transcript is None:
            raise AppError(
                404,
                "NO_TRANSCRIPT_FOR_CHARACTER_DETECTION",
                "Transcribe this video before detecting characters.",
            )

        job.status = "running"
        db.commit()
        _ensure_active(job_id)

        def check_active() -> None:
            status = _job_status(job_id)
            if status is None or status == "cancelled":
                raise JobCancelled("Job was cancelled or removed")

        def diarization_progress(fraction: float) -> None:
            current = db.get(ProcessingJob, job_id)
            if not current or current.status == "cancelled":
                return
            current.progress = 20 + int(max(0.0, min(1.0, fraction)) * 25)
            db.commit()

        def vision_progress(fraction: float) -> None:
            current = db.get(ProcessingJob, job_id)
            if not current or current.status == "cancelled":
                return
            current.progress = 50 + int(max(0.0, min(1.0, fraction)) * 15)
            db.commit()

        _set_step(db, job, "select_inputs", "running", "Choosing dialogue and source video", 8)
        check_active()
        audio_path, source, duration_ms = _select_transcription_source(db, video, prefer_dialogue_stem)
        video_path = storage.absolute_path(video.storage_key)
        if not video_path.is_file() or video_path.stat().st_size == 0:
            raise AppError(410, "FILE_MISSING", "Stored video file is missing or empty")
        source_duration = int(duration_ms or video.duration_ms or 0)
        _set_step(
            db,
            job,
            "select_inputs",
            "completed",
            f"{source} · {source_duration} ms",
            15,
        )

        _set_step(db, job, "diarize_speakers", "running", "Detecting speaker turns", 20)
        check_active()
        result = diarization_service.diarize_file(audio_path, check_active, diarization_progress)
        check_active()
        _set_step(
            db,
            job,
            "diarize_speakers",
            "completed",
            f"{len(result.turns)} speaker turns · {result.backend}",
            45,
        )

        _set_step(db, job, "detect_faces", "running", "Sampling frames around dialogue", 50)
        vision_result = vision_service.VisionResult(
            candidates=[],
            tracks=[],
            limitations=["Visual candidate detection was disabled for this run."],
            notes=["Off-screen and voice-over speakers are expected to have no visible candidate."],
        )
        if detect_faces and max_candidates > 0:
            segment_rows = list(
                db.scalars(
                    select(TranscriptSegment)
                    .where(TranscriptSegment.transcript_id == transcript.id)
                    .order_by(TranscriptSegment.position)
                )
            )
            points = vision_service.sample_dialogue_points(
                [
                    {
                        "id": segment.id,
                        "start_ms": segment.start_ms,
                        "end_ms": segment.end_ms,
                    }
                    for segment in segment_rows
                ],
                [
                    {"start_ms": turn.start_ms, "end_ms": turn.end_ms}
                    for turn in result.turns
                ],
                max_candidates,
                source_duration,
            )
            tmp_root = settings.data_dir / "tmp"
            tmp_root.mkdir(parents=True, exist_ok=True)
            temp_dir = Path(tempfile.mkdtemp(prefix="character-faces-", dir=str(tmp_root)))
            vision_result = vision_service.detect_candidates(
                video_path, points, temp_dir, check_active, vision_progress
            )
        check_active()
        _set_step(
            db,
            job,
            "detect_faces",
            "completed",
            f"{len(vision_result.candidates)} candidate(s) · {len(vision_result.tracks)} track(s)",
            65,
        )

        _set_step(db, job, "fuse_identities", "running", "Linking voices to stable characters", 70)
        check_active()
        old_candidates = list(
            db.scalars(select(VisualCandidate).where(VisualCandidate.video_id == video.id))
        )
        old_confirmations = {
            (item.timestamp_ms, item.segment_id): item.confirmed_character_id
            for item in old_candidates
            if item.confirmed and item.confirmed_character_id
        }
        old_track_characters = {
            item.label: item.character_id
            for item in db.scalars(select(FaceTrack).where(FaceTrack.video_id == video.id))
            if item.character_id
        }

        # Re-running replaces auto detections but preserves manual corrections.
        db.execute(db_delete(VisualCandidate).where(VisualCandidate.video_id == video.id))
        db.execute(db_delete(FaceTrack).where(FaceTrack.video_id == video.id))
        db.execute(db_delete(SpeakerTurn).where(SpeakerTurn.video_id == video.id))
        db.execute(
            db_delete(CharacterAssignment).where(
                CharacterAssignment.video_id == video.id,
                CharacterAssignment.reviewed.is_(False),
                CharacterAssignment.source.notlike("user%"),
            )
        )

        turn_rows: list[SpeakerTurn] = []
        for turn in result.turns:
            row = SpeakerTurn(
                id=new_id(),
                project_id=video.project_id,
                video_id=video.id,
                transcript_id=transcript.id,
                character_id=None,
                label=turn.label,
                start_ms=turn.start_ms,
                end_ms=turn.end_ms,
                confidence=turn.confidence,
                overlap_possible=turn.overlap_possible,
                reviewed=False,
                assignment_source="auto_fusion",
                embedding_json=[float(value) for value in turn.embedding],
                notes_json=turn.notes,
            )
            db.add(row)
            turn_rows.append(row)
        db.flush()

        fusion = characters_service.fuse_turns(db, video.id, transcript.id, turn_rows)
        assigned_segments = characters_service.assign_transcript_segments(
            db, video.id, transcript.id, turn_rows, "auto_fusion"
        )
        _set_step(
            db,
            job,
            "fuse_identities",
            "completed",
            f"{len(fusion['characters'])} character(s) · {assigned_segments} dialogue link(s)",
            82,
        )

        _set_step(db, job, "persist_characters", "running", "Saving character evidence", 88)
        claim = db.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id, ProcessingJob.status == "running")
            .values(current_step="persist_characters", progress=88)
        )
        if claim.rowcount != 1:
            raise JobCancelled("Job was cancelled before characters were saved")

        track_map: dict[str, FaceTrack] = {}
        for detection in vision_result.tracks:
            asset = None
            if detection.representative_path.is_file():
                # storage.save() moves its source, so keep a distinct copy for
                # the track; candidate crops may point at the same file.
                representative = (temp_dir or settings.data_dir / "tmp") / f"track-{detection.label}.jpg"
                shutil.copy2(detection.representative_path, representative)
                asset = _register_asset(
                    db,
                    video,
                    "face_track",
                    representative,
                    "image/jpeg",
                    {
                        "kind": "visible_person_track",
                        "timestamp_ms": detection.start_ms,
                        "confidence": detection.confidence,
                        "identity_claim": False,
                    },
                )
            track = FaceTrack(
                id=new_id(),
                project_id=video.project_id,
                video_id=video.id,
                character_id=old_track_characters.get(detection.label),
                frame_asset_id=asset.id if asset else None,
                label=detection.label,
                start_ms=detection.start_ms,
                end_ms=detection.end_ms,
                confidence=detection.confidence,
                bbox_samples_json=detection.bbox_samples,
                appearance_json=detection.appearance,
            )
            db.add(track)
            track_map[detection.label] = track

        for detection in vision_result.candidates:
            if not detection.image_path.is_file():
                continue
            asset = _register_asset(
                db,
                video,
                "face_candidate",
                detection.image_path,
                "image/jpeg",
                {
                    "kind": "visible_person_candidate",
                    "timestamp_ms": detection.timestamp_ms,
                    "bbox": detection.bbox,
                    "confidence": detection.confidence,
                    "identity_claim": False,
                },
            )
            confirmed = old_confirmations.get((detection.timestamp_ms, detection.segment_id))
            track = track_map.get(detection.track_label)
            db.add(
                VisualCandidate(
                    id=new_id(),
                    project_id=video.project_id,
                    video_id=video.id,
                    segment_id=detection.segment_id,
                    character_id=confirmed,
                    face_track_id=track.id if track else None,
                    frame_asset_id=asset.id,
                    confirmed_character_id=confirmed,
                    timestamp_ms=detection.timestamp_ms,
                    bbox_json=detection.bbox,
                    confidence=detection.confidence,
                    confirmed=confirmed is not None,
                    rejected=False,
                    notes=None,
                )
            )

        limitations = list(result.limitations) + list(vision_result.limitations)
        notes = list(result.notes) + list(vision_result.notes)
        notes.append(f"Identity decisions: {len(fusion['decisions'])} acoustic group(s) reviewed automatically.")
        metadata = dict(video.metadata_json or {})
        metadata["characters"] = {
            "status": "completed",
            "backend": result.backend,
            "model": result.model,
            "vad": result.vad_model,
            "vision_backend": vision_service.effective_backend(),
            "source": source,
            "transcript_id": transcript.id,
            "transcript_version": transcript.version,
            "duration_ms": source_duration,
            "timeline_offset_ms": 0,
            "limitations": limitations,
            "notes": notes,
            "decisions": fusion["decisions"],
            "updated_at": utcnow().isoformat(),
        }
        video.metadata_json = metadata

        persist_step = next(item for item in job.steps if item.name == "persist_characters")
        persist_step.status = "completed"
        persist_step.detail = f"{len(fusion['characters'])} characters saved"
        persist_step.finished_at = _utcz()
        finalize_step = next(item for item in job.steps if item.name == "finalize")
        finalize_step.status = "running"
        finalize_step.started_at = _utcz()
        job.current_step = "finalize"
        job.progress = 98

        final_claim = db.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id, ProcessingJob.status == "running")
            .values(
                status="completed",
                current_step="finalize",
                progress=100,
                error_code=None,
                error_message=None,
            )
        )
        if final_claim.rowcount != 1:
            db.rollback()
            raise JobCancelled("Job was cancelled before finalising characters")
        db.commit()
        db.refresh(job)
        _set_step(db, job, "finalize", "completed", "Characters are ready for review", 100)
    except (JobCancelled, diarization_service.DiarizationCancelled):
        current = db.get(ProcessingJob, job_id) if "job_id" in locals() else None
        if current:
            if current.status != "cancelled":
                current.status = "cancelled"
                current.error_code = "CANCELLED"
                current.error_message = "Cancelled by user"
            for step in current.steps:
                if step.status in {"pending", "running"}:
                    step.status = "cancelled"
                    step.detail = "Cancelled by user"
                    step.finished_at = utcnow()
            db.commit()
    except AppError as error:
        current = db.get(ProcessingJob, job_id) if "job_id" in locals() else None
        if current and current.status != "cancelled":
            current.status = "failed"
            current.error_code = error.code
            current.error_message = str(error)[:1000]
            for step in current.steps:
                if step.status == "running":
                    step.status = "failed"
                    step.detail = str(error)[:500]
                    step.finished_at = utcnow()
            db.commit()
    except Exception as error:
        current = db.get(ProcessingJob, job_id) if "job_id" in locals() else None
        if current and current.status != "cancelled":
            current.status = "failed"
            current.error_code = "CHARACTER_DETECTION_FAILED"
            current.error_message = str(error)[:1000]
            for step in current.steps:
                if step.status == "running":
                    step.status = "failed"
                    step.detail = str(error)[:500]
                    step.finished_at = utcnow()
            db.commit()
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        db.close()


def cancel_job(db: Session, job_id: str) -> ProcessingJob | None:
    job = db.get(ProcessingJob, job_id)
    if not job:
        return None
    if job.status not in {"queued", "running"}:
        raise AppError(409, "JOB_NOT_CANCELLABLE", "Only queued or running jobs can be cancelled.")
    job.status = "cancelled"
    job.error_code = "CANCELLED"
    job.error_message = "Cancelled by user"
    for step in job.steps:
        if step.status in {"pending", "running"}:
            step.status = "cancelled"
            step.detail = "Cancelled by user"
            step.finished_at = utcnow()
    db.commit()
    db.refresh(job)
    return job


def prepare_retry_job(db: Session, job_id: str) -> tuple[ProcessingJob, str, dict]:
    job = db.get(ProcessingJob, job_id)
    if not job:
        raise AppError(404, "NOT_FOUND", "Job not found")
    if job.status not in {"failed", "cancelled"}:
        raise AppError(409, "JOB_NOT_RETRYABLE", "Only failed or cancelled jobs can be retried.")
    video = db.get(Video, job.video_id)
    if not video:
        raise AppError(404, "NOT_FOUND", "Video for this job no longer exists")
    if not storage.absolute_path(video.storage_key).is_file():
        raise AppError(410, "FILE_MISSING", "Stored video file is missing")
    if job.type == "transcription":
        context = job.options_json or {}
        retry = create_transcription_job(db, video.project_id, video.id, context)
        return retry, video.id, context
    if job.type == "character_detection":
        context = job.options_json or {}
        retry = create_character_job(db, video.project_id, video.id, context)
        return retry, video.id, context
    if job.type == "translation":
        from .translation import create_translation_job

        # Resume: lines already finished by the failed run are not redone.
        context = {**(job.options_json or {}), "resume_from_job_id": job.id}
        retry = create_translation_job(db, video, context)
        return retry, video.id, context
    if job.type == "dub_audio_generation":
        from .dub_audio import create_dub_job

        # Resume: lines voiced successfully by the failed run are not redone.
        context = {**(job.options_json or {}), "resume_from_job_id": job.id}
        retry = create_dub_job(db, video, context)
        return retry, video.id, context
    client_metadata = (video.metadata_json or {}).get("browser") or {}
    retry = create_import_job(db, video.project_id, video.id, job.type)
    return retry, video.id, client_metadata
