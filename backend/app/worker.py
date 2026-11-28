"""Phase 15 — standalone processing worker.

    python -m app.worker --classes cpu,audio,video,ai           # CPU worker
    python -m app.worker --classes gpu,video,ai --name gpu-1     # GPU worker (needs CUDA PyTorch)
    python -m app.worker --once                                   # run at most one job, then exit

Run with PROCESSING_MODE=queue on the API so the API only enqueues. Workers
must share the API's DATABASE_URL and DATA_DIR (same machine, or a shared
database and storage volume on another machine). Ctrl+C drains: the current
job finishes, no new job is claimed, and the worker is marked offline.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading
import time

from .database import SessionLocal, init_db
from .models_workers import ProcessingWorker
from .services import job_queue


def detect_classes() -> list[str]:
    classes = ["cpu", "audio", "video", "ai"]
    try:
        import torch

        if torch.cuda.is_available():
            classes.append("gpu")
    except Exception:  # noqa: BLE001
        pass
    return classes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CINEMA AI DUB processing worker")
    parser.add_argument("--classes", default=None, help="comma list of: cpu,gpu,audio,video,ai (default: auto)")
    parser.add_argument("--name", default=None)
    parser.add_argument("--poll", type=float, default=2.0, help="seconds between queue polls when idle")
    parser.add_argument("--once", action="store_true", help="exit after one job (or immediately if none)")
    args = parser.parse_args(argv)

    classes = [c.strip() for c in args.classes.split(",")] if args.classes else detect_classes()
    unknown = set(classes) - set(job_queue.ALL_CLASSES)
    if unknown:
        parser.error(f"unknown classes: {', '.join(sorted(unknown))}")
    if "gpu" in classes:
        try:
            import torch

            if not torch.cuda.is_available():
                print("warning: --classes includes gpu but this PyTorch cannot use CUDA", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
    init_db()
    worker_id = f"worker-{socket.gethostname()}-{os.getpid()}-{job_queue.BOOT_ID}"[:64]
    name = args.name or f"{'GPU' if 'gpu' in classes else 'CPU'} worker on {socket.gethostname()}"
    with SessionLocal() as db:
        job_queue.register_worker(db, worker_id, name, "local", classes)
    print(f"[worker] {worker_id} online · classes={','.join(classes)} · mode on API should be PROCESSING_MODE=queue", flush=True)

    draining = threading.Event()

    def stop(_signum, _frame):
        if draining.is_set():
            sys.exit(1)
        print("[worker] draining: finishing the current job, then exiting (Ctrl+C again to force)", flush=True)
        draining.set()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    last_beat = 0.0
    try:
        while not draining.is_set():
            if time.monotonic() - last_beat > job_queue.processing_settings.heartbeat_seconds:
                job_queue.worker_heartbeat(worker_id, status="online")
                with SessionLocal() as db:
                    job_queue.recover(db)
                last_beat = time.monotonic()
            with SessionLocal() as db:
                worker = db.get(ProcessingWorker, worker_id)
                job = job_queue.claim_next(db, worker) if worker else None
                job_id, job_type = (job.id, job.type) if job else (None, None)
            if job_id is None:
                if args.once:
                    break
                draining.wait(args.poll)
                continue
            print(f"[worker] running {job_type} {job_id}", flush=True)
            status = job_queue.execute(job_id, worker_id, already_claimed=True)
            print(f"[worker] {job_id} -> {status}", flush=True)
            if args.once:
                break
    finally:
        job_queue.worker_heartbeat(worker_id, status="offline", current_job_id=None)
        print(f"[worker] {worker_id} offline", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
