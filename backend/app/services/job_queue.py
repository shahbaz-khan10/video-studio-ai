"""Phase 15 — central job queue, workers, leases and recovery.

Every heavy operation is a ``ProcessingJob`` row. Who runs it depends on
PROCESSING_MODE:

  inline  (default, local development) — the API process runs jobs on worker
          threads right after the request, exactly as before, but now with a
          lease, heartbeat, attempt history and restart recovery.
  queue   — the API only enqueues. Separate worker processes
          (``python -m app.worker``) on this or other machines sharing the
          database and storage claim jobs that match their capabilities.

A job is claimed with one conditional UPDATE (status queued, no live lease),
so two workers can never run the same job. A running job renews its lease
every few seconds; if its process dies the lease expires and the job is
re-queued (resuming where the job type supports it) or failed with a clear
"interrupted" message after too many attempts.
"""

from __future__ import annotations

import importlib
import os
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..config import PROJECT_ROOT
from ..database import SessionLocal
from ..models import JobStep, ProcessingJob, utcnow
from ..models_workers import JobAttempt, ProcessingWorker


class ProcessingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PROCESSING_", env_file=str(PROJECT_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    mode: str = "inline"  # inline | queue
    lease_seconds: int = 90
    heartbeat_seconds: int = 10
    max_attempts: int = 3
    recovery_interval_seconds: int = 30
    idle_unload_minutes: float = 15.0
    temp_retention_hours: float = 6.0
    worker_offline_after_seconds: int = 45


processing_settings = ProcessingSettings()


@dataclass(frozen=True)
class JobType:
    runner: str  # "module:function"
    classes: frozenset[str]  # capabilities a worker needs
    args: str = "options"  # options | metadata | none | target
    resumable: bool = False  # honours options["resume_from_job_id"]
    gpu_preferred: bool = False
    label: str = ""


JOB_TYPES: dict[str, JobType] = {
    "media_import": JobType("app.services.jobs:run_import_job", frozenset({"video"}), "metadata", label="Import & inspect"),
    "media_reprobe": JobType("app.services.jobs:run_import_job", frozenset({"video"}), "metadata", label="Re-inspect media"),
    "audio_separation": JobType("app.services.jobs:run_audio_separation_job", frozenset({"audio"}), "none", label="Audio separation"),
    "transcription": JobType("app.services.jobs:run_transcription_job", frozenset({"audio", "ai"}), label="Speech recognition"),
    "character_detection": JobType("app.services.jobs:run_character_job", frozenset({"audio", "video", "ai"}), label="Character detection"),
    "translation": JobType("app.services.translation:run_translation_job", frozenset({"ai"}), resumable=True, label="Translation"),
    "dub_audio_generation": JobType("app.services.dub_audio:run_dub_job", frozenset({"audio", "ai"}), resumable=True, label="Voice generation"),
    "dub_timing": JobType("app.services.dub_timing:run_timing_job", frozenset({"audio"}), resumable=True, label="Dialogue timing"),
    "mix_render": JobType("app.services.mix_studio:run_render_job", frozenset({"audio"}), label="Mix rendering"),
    "lipsync": JobType("app.services.lipsync:run_lipsync_job", frozenset({"video", "ai"}), resumable=True, gpu_preferred=True, label="Lip sync"),
    "timeline_export": JobType("app.services.timeline:run_export_job", frozenset({"video", "audio"}), "target", label="Timeline export"),
}
ALL_CLASSES = ("cpu", "gpu", "audio", "video", "ai")
DEFAULT_TYPE = JOB_TYPES["media_import"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def job_type(name: str) -> JobType:
    return JOB_TYPES.get(name, DEFAULT_TYPE)


def _resolve(spec: str):
    module_name, func = spec.split(":")
    return getattr(importlib.import_module(module_name), func)


def _runner_args(job: ProcessingJob) -> tuple:
    kind = job_type(job.type)
    options = dict(job.options_json or {})
    if kind.args == "none":
        return (job.id, job.video_id)
    if kind.args == "metadata":
        return (job.id, job.video_id, options.get("client_metadata") or {})
    if kind.args == "target":
        return (job.id, options.get("_target_id") or job.video_id, options)
    return (job.id, job.video_id, options)


# ---------------------------------------------------------------------------
# Worker identity
# ---------------------------------------------------------------------------

BOOT_ID = uuid.uuid4().hex[:8]
_inline_worker_id: str | None = None


def resources_snapshot() -> dict:
    try:
        from . import resources

        return resources.snapshot(include_models=True, light=True)
    except Exception:  # noqa: BLE001
        return {}


def register_worker(db: Session, worker_id: str, name: str, kind: str, classes: list[str], url: str | None = None) -> ProcessingWorker:
    row = db.get(ProcessingWorker, worker_id)
    if row is None:
        row = ProcessingWorker(id=worker_id, name=name, kind=kind)
        db.add(row)
    row.name = name
    row.kind = kind
    row.classes = sorted(set(classes))
    row.status = "online"
    row.host = socket.gethostname()
    row.pid = os.getpid()
    row.url = url
    row.started_at = utcnow()
    row.last_heartbeat_at = utcnow()
    row.resources_json = resources_snapshot()
    db.commit()
    db.refresh(row)
    db.expunge(row)  # usable after the session closes
    return row


def inline_worker_id() -> str:
    """The API process's own worker identity in inline mode."""
    global _inline_worker_id
    if _inline_worker_id is None:
        _inline_worker_id = f"api-{socket.gethostname()}-{os.getpid()}-{BOOT_ID}"[:64]
        with SessionLocal() as db:
            register_worker(db, _inline_worker_id, "API (inline jobs)", "inline", list(ALL_CLASSES))
    return _inline_worker_id


def worker_heartbeat(worker_id: str, status: str | None = None, current_job_id: str | None = "__keep__") -> None:
    with SessionLocal() as db:
        row = db.get(ProcessingWorker, worker_id)
        if row is None:
            return
        row.last_heartbeat_at = utcnow()
        if status:
            row.status = status
        if current_job_id != "__keep__":
            row.current_job_id = current_job_id
        row.resources_json = resources_snapshot()
        db.commit()


# ---------------------------------------------------------------------------
# Claiming and executing
# ---------------------------------------------------------------------------


def claim(db: Session, job_id: str, worker_id: str) -> bool:
    """Atomically take a queued job. False if someone else holds it or it was cancelled."""
    now = _now()
    lease = now + timedelta(seconds=processing_settings.lease_seconds)
    result = db.execute(
        update(ProcessingJob)
        .where(
            ProcessingJob.id == job_id,
            ProcessingJob.status == "queued",
            or_(ProcessingJob.worker_id.is_(None), ProcessingJob.lease_expires_at.is_(None), ProcessingJob.lease_expires_at < now),
        )
        .values(worker_id=worker_id, lease_expires_at=lease, attempts=ProcessingJob.attempts + 1)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def claim_next(db: Session, worker: ProcessingWorker) -> ProcessingJob | None:
    classes = set(worker.classes or [])
    now = _now()
    candidates = db.scalars(
        select(ProcessingJob)
        .where(
            ProcessingJob.status == "queued",
            or_(ProcessingJob.worker_id.is_(None), ProcessingJob.lease_expires_at.is_(None), ProcessingJob.lease_expires_at < now),
        )
        .order_by(ProcessingJob.created_at)
        .limit(50)
    ).all()
    gpu_online = None
    for job in candidates:
        kind = job_type(job.type)
        if not kind.classes <= classes:
            continue
        if kind.gpu_preferred and "gpu" not in classes:
            if gpu_online is None:
                cutoff = utcnow() - timedelta(seconds=processing_settings.worker_offline_after_seconds)
                gpu_online = any(
                    "gpu" in (w.classes or []) and _aware(w.last_heartbeat_at) >= _aware(cutoff) and w.status != "offline"
                    for w in db.scalars(select(ProcessingWorker))
                )
            age = (_now() - _aware(job.created_at)).total_seconds()
            if gpu_online and age < 120:
                continue  # leave it briefly for the GPU worker
        if claim(db, job.id, worker.id):
            db.refresh(job)
            return job
    return None


class _Heartbeat(threading.Thread):
    def __init__(self, job_id: str, worker_id: str, attempt_id: str):
        super().__init__(daemon=True)
        self.job_id, self.worker_id, self.attempt_id = job_id, worker_id, attempt_id
        self.stop_event = threading.Event()
        self.peak_rss = 0

    def run(self) -> None:
        try:
            import psutil

            proc = psutil.Process()
        except Exception:  # noqa: BLE001
            proc = None
        while not self.stop_event.wait(processing_settings.heartbeat_seconds):
            if proc is not None:
                self.peak_rss = max(self.peak_rss, proc.memory_info().rss)
            try:
                with SessionLocal() as db:
                    db.execute(
                        update(ProcessingJob)
                        .where(ProcessingJob.id == self.job_id, ProcessingJob.worker_id == self.worker_id)
                        .values(lease_expires_at=_now() + timedelta(seconds=processing_settings.lease_seconds))
                        .execution_options(synchronize_session=False)
                    )
                    db.commit()
                worker_heartbeat(self.worker_id)
            except Exception:  # noqa: BLE001 — a missed heartbeat must not kill the job
                pass


def execute(job_id: str, worker_id: str, already_claimed: bool = False) -> str | None:
    """Claim (unless done already) and run one job; record the attempt. Returns final status."""
    with SessionLocal() as db:
        if not already_claimed and not claim(db, job_id, worker_id):
            return None
        job = db.get(ProcessingJob, job_id)
        if job is None:
            return None
        attempt = JobAttempt(job_id=job.id, worker_id=worker_id, attempt=job.attempts or 1, outcome="running")
        db.add(attempt)
        db.commit()
        attempt_id = attempt.id
        runner = _resolve(job_type(job.type).runner)
        args = _runner_args(job)
    worker_heartbeat(worker_id, status="busy", current_job_id=job_id)
    beat = _Heartbeat(job_id, worker_id, attempt_id)
    try:
        import psutil

        beat.peak_rss = psutil.Process().memory_info().rss
    except Exception:  # noqa: BLE001
        pass
    beat.start()
    error_text = None
    try:
        runner(*args)
    except Exception as error:  # noqa: BLE001 — runners record their own failures; this is a last resort
        error_text = str(error)[:1000]
    finally:
        beat.stop_event.set()
        beat.join(timeout=2)
    with SessionLocal() as db:
        job = db.get(ProcessingJob, job_id)
        status = job.status if job else "missing"
        if job is not None:
            if error_text and status in {"queued", "running"}:
                job.status, job.error_code, job.error_message = "failed", "WORKER_EXCEPTION", error_text
                status = "failed"
            if status in {"queued", "running"}:
                # Runner returned without finishing (should not happen): let recovery decide.
                pass
            else:
                job.lease_expires_at = None
        attempt = db.get(JobAttempt, attempt_id)
        if attempt is not None:
            attempt.outcome = status if status in {"completed", "failed", "cancelled"} else "interrupted"
            attempt.detail = (job.error_message if job is not None else None) or error_text
            attempt.finished_at = utcnow()
            attempt.peak_rss_mb = int(beat.peak_rss / 1e6) if beat.peak_rss else None
        worker = db.get(ProcessingWorker, worker_id)
        if worker is not None:
            worker.current_job_id = None
            worker.status = "online"
            if status == "completed":
                worker.jobs_completed += 1
            elif status == "failed":
                worker.jobs_failed += 1
        db.commit()
    return status


# ---------------------------------------------------------------------------
# Submission (called by every router that creates a job)
# ---------------------------------------------------------------------------

_executor: ThreadPoolExecutor | None = None
_running_inline = 0
_running_lock = threading.Lock()
_last_activity = time.monotonic()


def _inline_run(job_id: str) -> None:
    global _running_inline, _last_activity
    with _running_lock:
        _running_inline += 1
    try:
        execute(job_id, inline_worker_id())
    finally:
        with _running_lock:
            _running_inline -= 1
            _last_activity = time.monotonic()


def running_inline() -> int:
    return _running_inline


def submit(background, job: ProcessingJob, *, metadata: dict | None = None, target_id: str | None = None) -> None:
    """Hand a freshly created job to whoever executes jobs in this deployment."""
    extra = {}
    if metadata is not None:
        extra["client_metadata"] = metadata
    if target_id is not None:
        extra["_target_id"] = target_id
    if extra:
        with SessionLocal() as db:
            row = db.get(ProcessingJob, job.id)
            if row is not None:
                row.options_json = {**(row.options_json or {}), **extra}
                db.commit()
    if processing_settings.mode == "queue":
        return  # a worker process will claim it
    if background is not None:
        background.add_task(_inline_run, job.id)
    else:
        _pool().submit(_inline_run, job.id)


def _pool() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="inline-job")
    return _executor


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def recover(db: Session, redispatch_inline: bool = False) -> dict:
    """Re-queue or fail jobs whose worker vanished; mark silent workers offline."""
    now = _now()
    report = {"requeued": [], "failed": [], "redispatched": [], "workers_offline": []}
    stale = db.scalars(
        select(ProcessingJob).where(
            ProcessingJob.status.in_(["queued", "running"]),
            ProcessingJob.worker_id.is_not(None),
            ProcessingJob.lease_expires_at.is_not(None),
            ProcessingJob.lease_expires_at < now,
        )
    ).all()
    for job in stale:
        kind = job_type(job.type)
        for attempt in db.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id, JobAttempt.outcome == "running")):
            attempt.outcome, attempt.finished_at = "interrupted", utcnow()
            attempt.detail = f"Worker {job.worker_id} stopped responding."
        claim_ok = db.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job.id, ProcessingJob.lease_expires_at < now)
            .values(lease_expires_at=None)
            .execution_options(synchronize_session=False)
        ).rowcount == 1
        if not claim_ok:
            continue
        if (job.attempts or 0) < processing_settings.max_attempts:
            options = dict(job.options_json or {})
            if kind.resumable:
                options["resume_from_job_id"] = job.id
            job.options_json = options
            job.status = "queued"
            job.worker_id = None
            job.error_code = None
            job.error_message = f"Recovered after an interruption (attempt {job.attempts} stopped)."
            for step in job.steps:
                if step.status == "running":
                    step.status, step.detail = "pending", "Interrupted — will run again"
            report["requeued"].append(job.id)
        else:
            job.status = "failed"
            job.error_code = "INTERRUPTED"
            job.error_message = f"The job was interrupted {job.attempts} times (worker stopped). Use Retry to run it again."
            for step in job.steps:
                if step.status in {"running", "pending"}:
                    step.status, step.detail, step.finished_at = "failed", "Interrupted", utcnow()
            report["failed"].append(job.id)
    db.commit()
    if redispatch_inline and processing_settings.mode == "inline":
        # Jobs queued by a previous API process never got a thread: run them now.
        boot_cutoff = utcnow() - timedelta(seconds=5)
        orphans = db.scalars(
            select(ProcessingJob).where(ProcessingJob.status == "queued", ProcessingJob.worker_id.is_(None))
        ).all()
        for job in orphans:
            if _aware(job.created_at) < _aware(boot_cutoff):
                report["redispatched"].append(job.id)
        for job_id in report["requeued"] + report["redispatched"]:
            _pool().submit(_inline_run, job_id)
    cutoff = utcnow() - timedelta(seconds=processing_settings.worker_offline_after_seconds)
    for worker in db.scalars(select(ProcessingWorker).where(ProcessingWorker.status != "offline")):
        if _aware(worker.last_heartbeat_at) < _aware(cutoff):
            worker.status = "offline"
            report["workers_offline"].append(worker.id)
    db.commit()
    return report


# ---------------------------------------------------------------------------
# Background maintenance in the API process
# ---------------------------------------------------------------------------

_monitor_started = False


def start_monitor() -> None:
    """Recovery, worker liveness, idle-model unloading and temp cleanup (one daemon thread)."""
    global _monitor_started
    if _monitor_started:
        return
    _monitor_started = True
    worker_id = inline_worker_id() if processing_settings.mode == "inline" else None

    def loop() -> None:
        first = True
        last_cleanup = 0.0
        while True:
            try:
                with SessionLocal() as db:
                    recover(db, redispatch_inline=first)
                first = False
                if worker_id:
                    worker_heartbeat(worker_id)
                from . import remote_mode

                if remote_mode.enabled():
                    remote_mode.refresh_worker_row()
                from . import resources

                idle_for = time.monotonic() - _last_activity
                if running_inline() == 0 and idle_for > processing_settings.idle_unload_minutes * 60:
                    resources.unload_models(reason="idle")
                if time.monotonic() - last_cleanup > 3600:
                    resources.cleanup_temp(dry_run=False)
                    last_cleanup = time.monotonic()
            except Exception:  # noqa: BLE001 — maintenance must never crash the API
                pass
            time.sleep(processing_settings.recovery_interval_seconds)

    threading.Thread(target=loop, daemon=True, name="job-monitor").start()


def job_history(db: Session, job_id: str) -> list[dict]:
    return [
        {
            "attempt": row.attempt,
            "worker_id": row.worker_id,
            "outcome": row.outcome,
            "detail": row.detail,
            "peak_rss_mb": row.peak_rss_mb,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        }
        for row in db.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id).order_by(JobAttempt.started_at))
    ]
