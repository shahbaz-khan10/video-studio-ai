"""Phase 15 — central processing: queue, workers, recovery, resources, security, remote mode."""

import hashlib
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import JobStep, ProcessingJob, Project, Video, utcnow
from app.models_workers import JobAttempt, ProcessingWorker
from app.services import auth, job_queue, resources
from app.services.storage import storage

HEADER = b"\x00\x00\x00\x18ftypmp42"


def make_project_video(client, name="Phase 15 film"):
    project = client.post("/api/v1/projects", json={"name": name, "target_languages": ["ur"]}).json()
    with SessionLocal() as db:
        video = Video(project_id=project["id"], original_filename="a.mp4", storage_key=f"projects/{project['id']}/a.mp4",
                      content_type="video/mp4", size_bytes=1)
        db.add(video)
        db.commit()
        return project["id"], video.id


def make_job(project_id, video_id, job_type="translation", status="queued", **fields) -> str:
    with SessionLocal() as db:
        job = ProcessingJob(project_id=project_id, video_id=video_id, type=job_type, status=status, progress=0,
                            options_json={}, **fields)
        db.add(job)
        db.flush()
        db.add(JobStep(job_id=job.id, position=0, name="work", status="running" if status == "running" else "pending"))
        db.commit()
        return job.id


def worker(worker_id, classes):
    with SessionLocal() as db:
        return job_queue.register_worker(db, worker_id, worker_id, "local", classes)


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def test_claim_is_atomic_and_exclusive(client):
    project_id, video_id = make_project_video(client)
    job_id = make_job(project_id, video_id)
    results = []
    barrier = threading.Barrier(6)

    def contender(n):
        barrier.wait()
        with SessionLocal() as db:
            results.append(job_queue.claim(db, job_id, f"w{n}"))

    threads = [threading.Thread(target=contender, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1  # never two workers on one job
    with SessionLocal() as db:
        job = db.get(ProcessingJob, job_id)
        assert job.attempts == 1 and job.worker_id.startswith("w") and job.lease_expires_at is not None


def test_workers_only_take_jobs_they_can_run(client):
    project_id, video_id = make_project_video(client)
    lipsync = make_job(project_id, video_id, "lipsync")
    timing = make_job(project_id, video_id, "dub_timing")
    audio_worker = worker("audio-only", ["cpu", "audio"])
    with SessionLocal() as db:
        got = job_queue.claim_next(db, audio_worker)
        assert got.id == timing  # lipsync needs video+ai
        assert job_queue.claim_next(db, audio_worker) is None
    gpu = worker("gpu-1", ["gpu", "video", "ai"])
    cpu_ai = worker("cpu-ai", ["cpu", "video", "ai"])
    with SessionLocal() as db:
        # A fresh GPU-preferred job waits for the online GPU worker instead of going to the CPU worker.
        assert job_queue.claim_next(db, db.get(ProcessingWorker, cpu_ai.id)) is None
        assert job_queue.claim_next(db, db.get(ProcessingWorker, gpu.id)).id == lipsync


def test_queue_mode_enqueues_and_execute_records_history(client, monkeypatch):
    monkeypatch.setattr(job_queue.processing_settings, "mode", "queue")
    monkeypatch.setattr(job_queue.processing_settings, "heartbeat_seconds", 1)
    project_id, video_id = make_project_video(client)
    job_id = make_job(project_id, video_id, "dub_timing")
    with SessionLocal() as db:
        job_queue.submit(None, db.get(ProcessingJob, job_id))
        assert db.get(ProcessingJob, job_id).status == "queued"  # API only enqueued

    calls = {}

    def fake_runner(job_id_, video_id_, options):
        calls["args"] = (job_id_, video_id_)
        with SessionLocal() as db:
            db.get(ProcessingJob, job_id_).status = "running"
            db.commit()
        time.sleep(2.5)  # long enough for heartbeats to renew the lease
        with SessionLocal() as db:
            job = db.get(ProcessingJob, job_id_)
            calls["lease_during_run"] = job.lease_expires_at
            job.status = "completed"
            db.commit()

    monkeypatch.setattr(job_queue, "_resolve", lambda spec: fake_runner)
    w = worker("worker-test", ["cpu", "audio"])
    with SessionLocal() as db:
        job = job_queue.claim_next(db, db.get(ProcessingWorker, w.id))
    first_lease = job.lease_expires_at
    assert job_queue.execute(job.id, w.id, already_claimed=True) == "completed"
    assert calls["args"] == (job_id, video_id)
    assert job_queue._aware(calls["lease_during_run"]) > job_queue._aware(first_lease)  # heartbeat renewed
    history = client.get(f"/api/v1/jobs/{job_id}/history").json()
    assert history["history"][0]["worker_id"] == "worker-test" and history["history"][0]["outcome"] == "completed"
    assert history["requires"] == ["audio"] and history["resumable"] is True
    with SessionLocal() as db:
        assert db.get(ProcessingWorker, "worker-test").jobs_completed == 1


def test_recovery_requeues_with_resume_then_gives_up(client, monkeypatch):
    monkeypatch.setattr(job_queue.processing_settings, "max_attempts", 2)
    project_id, video_id = make_project_video(client)
    past = utcnow() - timedelta(minutes=5)
    job_id = make_job(project_id, video_id, "translation", status="running", worker_id="dead-worker", lease_expires_at=past, attempts=1)
    with SessionLocal() as db:
        db.add(JobAttempt(job_id=job_id, worker_id="dead-worker", attempt=1, outcome="running"))
        db.commit()
        report = job_queue.recover(db)
    assert report["requeued"] == [job_id]
    with SessionLocal() as db:
        job = db.get(ProcessingJob, job_id)
        assert job.status == "queued" and job.worker_id is None
        assert job.options_json["resume_from_job_id"] == job_id  # finished lines are skipped on resume
        assert job.steps[0].status == "pending"
        assert db.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id)).one().outcome == "interrupted"
        # second crash: attempts exhausted → clear failure, Retry available
        job.status, job.worker_id, job.lease_expires_at, job.attempts = "running", "dead-again", past, 2
        db.commit()
        report = job_queue.recover(db)
    assert report["failed"] == [job_id]
    with SessionLocal() as db:
        job = db.get(ProcessingJob, job_id)
        assert job.status == "failed" and job.error_code == "INTERRUPTED" and "Retry" in job.error_message


def test_live_lease_is_not_recovered_and_dead_workers_go_offline(client):
    project_id, video_id = make_project_video(client)
    future = utcnow() + timedelta(minutes=5)
    job_id = make_job(project_id, video_id, status="running", worker_id="alive", lease_expires_at=future, attempts=1)
    worker("silent-worker", ["cpu"])
    with SessionLocal() as db:
        db.get(ProcessingWorker, "silent-worker").last_heartbeat_at = utcnow() - timedelta(minutes=10)
        db.commit()
        report = job_queue.recover(db)
    assert job_id not in report["requeued"] and "silent-worker" in report["workers_offline"]


def test_inline_orphans_redispatched_after_restart(client, monkeypatch):
    project_id, video_id = make_project_video(client)
    job_id = make_job(project_id, video_id, "dub_timing", created_at=utcnow() - timedelta(minutes=2))
    ran = threading.Event()
    monkeypatch.setattr(job_queue, "_inline_run", lambda jid: ran.set() if jid == job_id else None)
    with SessionLocal() as db:
        report = job_queue.recover(db, redispatch_inline=True)
    assert job_id in report["redispatched"]
    assert ran.wait(5)


def test_inline_mode_routes_existing_jobs_through_queue(client, tmp_path):
    """A normal API job (upload → import) now runs with a lease and an attempt record."""
    import subprocess as sp

    from app.services.tts_engine import ffmpeg_binary

    clip = tmp_path / "v.mp4"
    sp.run([ffmpeg_binary(), "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=160x90:rate=10", "-pix_fmt", "yuv420p",
            "-y", str(clip)], check=True)
    project = client.post("/api/v1/projects", json={"name": "inline", "target_languages": ["ur"]}).json()
    up = client.post(f"/api/v1/uploads/projects/{project['id']}/videos", files={"file": ("v.mp4", clip.read_bytes(), "video/mp4")}).json()
    job = client.get(f"/api/v1/jobs/{up['job']['id']}").json()
    assert job["status"] == "completed"
    history = client.get(f"/api/v1/jobs/{up['job']['id']}/history").json()
    assert history["attempts"] == 1 and history["history"][0]["outcome"] == "completed"
    assert history["history"][0]["worker_id"].startswith("api-")


def test_real_worker_process_runs_a_queued_job(client, tmp_path):
    """Spawn `python -m app.worker --once` against the same database and storage."""
    import subprocess as sp

    from app.config import settings
    from app.services.tts_engine import ffmpeg_binary

    clip = tmp_path / "w.mp4"
    sp.run([ffmpeg_binary(), "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=160x90:rate=10", "-pix_fmt", "yuv420p",
            "-y", str(clip)], check=True)
    project = client.post("/api/v1/projects", json={"name": "queue", "target_languages": ["ur"]}).json()
    original = job_queue.processing_settings.mode
    job_queue.processing_settings.mode = "queue"
    try:
        up = client.post(f"/api/v1/uploads/projects/{project['id']}/videos", files={"file": ("w.mp4", clip.read_bytes(), "video/mp4")}).json()
    finally:
        job_queue.processing_settings.mode = original
    job_id = up["job"]["id"]
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "queued"
    env = {**os.environ, "PROCESSING_MODE": "queue", "DATA_DIR": str(settings.data_dir), "DATABASE_URL": settings.database_url}
    result = sp.run([sys.executable, "-m", "app.worker", "--classes", "cpu,video", "--once"], cwd=Path(__file__).parents[1],
                    env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-800:]
    assert f"{job_id} -> completed" in result.stdout
    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "completed"
    history = client.get(f"/api/v1/jobs/{job_id}/history").json()["history"]
    assert history[-1]["worker_id"].startswith("worker-")
    status = client.get("/api/v1/processing/status").json()
    assert any(w["id"].startswith("worker-") and w["status"] == "offline" for w in status["workers"])  # drained cleanly


# ---------------------------------------------------------------------------
# Resources & cleanup
# ---------------------------------------------------------------------------


def test_resources_report_and_model_unload(client):
    from app.services import translation_engine

    translation_engine._loaded.update({"backend": "nllb", "model_id": "x", "model": object(), "tokenizer": object(), "device": "cpu"})
    data = client.get("/api/v1/processing/resources").json()
    assert data["ram_total_mb"] > 0 and data["cpu_count"] >= 1 and "disk_free_gb" in data
    assert any(m["slot"] == "translation" for m in data["models_loaded"])
    released = client.post("/api/v1/processing/models/unload").json()
    assert "translation" in released["released"] and translation_engine._loaded["model"] is None


def test_cleanup_removes_only_our_old_temp_data(client):
    tmp = Path(tempfile.gettempdir())
    old = tmp / "lipsync-phase15-old"
    old.mkdir(exist_ok=True)
    (old / "f.bin").write_bytes(b"x" * 1000)
    fresh = tmp / "lipsync-phase15-fresh"
    fresh.mkdir(exist_ok=True)
    foreign = tmp / "someone-elses-file-phase15.tmp"
    foreign.write_bytes(b"keep")
    past = time.time() - 48 * 3600
    os.utime(old, (past, past))
    os.utime(foreign, (past, past))
    dry = client.post("/api/v1/processing/cleanup?dry_run=true&older_than_hours=24").json()
    assert "lipsync-phase15-old" in dry["temp_entries"] and old.exists()
    real = client.post("/api/v1/processing/cleanup?dry_run=false&older_than_hours=24").json()
    assert "lipsync-phase15-old" in real["temp_entries"]
    assert not old.exists() and fresh.exists() and foreign.exists()
    import shutil

    shutil.rmtree(fresh, ignore_errors=True)
    foreign.unlink(missing_ok=True)


def test_interrupted_upload_resumes_with_missing_chunks_only(client):
    project = client.post("/api/v1/projects", json={"name": "upload", "target_languages": ["ur"]}).json()
    payload = HEADER + os.urandom(3 * 1024 * 1024 + 777)
    session = client.post(f"/api/v1/uploads/projects/{project['id']}/sessions",
                          json={"filename": "big.mp4", "size_bytes": len(payload), "mime_type": "video/mp4"}).json()
    size, total = session["chunk_size"], session["total_chunks"]
    for index in (0, 2):  # connection drops after two chunks
        client.put(f"/api/v1/uploads/sessions/{session['id']}/chunks/{index}", content=payload[index * size:(index + 1) * size])
    state = client.get(f"/api/v1/uploads/sessions/{session['id']}").json()  # client reconnects and asks
    missing = sorted(set(range(total)) - set(state["uploaded_chunks"]))
    assert missing == [i for i in range(total) if i not in (0, 2)]
    for index in missing:
        client.put(f"/api/v1/uploads/sessions/{session['id']}/chunks/{index}", content=payload[index * size:(index + 1) * size])
    done = client.post(f"/api/v1/uploads/sessions/{session['id']}/complete").json()
    assert done["video"]["checksum_sha256"] == hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(auth.auth_settings, "mode", "password")
    monkeypatch.setattr(auth.auth_settings, "secret_key", "s" * 48)
    monkeypatch.setattr(auth.auth_settings, "allow_registration", False)
    yield


def signup(client, email, password="correct horse battery"):
    return client.post("/api/v1/auth/register", json={"email": email, "password": password})


def test_authentication_and_project_authorisation(client, auth_on):
    from fastapi.testclient import TestClient

    from app.main import app

    assert client.get("/api/v1/projects").status_code == 401
    assert client.get("/api/v1/health").status_code == 200
    admin = signup(client, "admin@example.org")
    assert admin.status_code == 200 and admin.json()["is_admin"] is True
    assert signup(TestClient(app), "late@example.org").status_code == 403  # registration closed after first user
    # admin creates a second (normal) account
    created = client.post("/api/v1/auth/register", json={"email": "editor@example.org", "password": "another long pass"})
    assert created.status_code == 200 and created.json()["is_admin"] is False

    editor = TestClient(app)
    bad = editor.post("/api/v1/auth/login", json={"email": "editor@example.org", "password": "wrong password!"})
    assert bad.status_code == 401
    assert editor.post("/api/v1/auth/login", json={"email": "editor@example.org", "password": "another long pass"}).status_code == 200
    mine = editor.post("/api/v1/projects", json={"name": "Editor film", "target_languages": ["ur"]}).json()
    theirs = client.post("/api/v1/projects", json={"name": "Admin film", "target_languages": ["ur"]}).json()
    listed = {p["id"] for p in editor.get("/api/v1/projects").json()}
    assert mine["id"] in listed and theirs["id"] not in listed
    assert editor.get(f"/api/v1/projects/{theirs['id']}").status_code == 404  # existence not revealed
    with SessionLocal() as db:
        video = Video(project_id=theirs["id"], original_filename="x.mp4", storage_key=f"projects/{theirs['id']}/x.mp4",
                      content_type="video/mp4", size_bytes=1)
        db.add(video)
        db.commit()
        foreign_video = video.id
    assert editor.get(f"/api/v1/videos/{foreign_video}/stream").status_code == 404
    assert editor.get(f"/api/v1/videos/{foreign_video}/translations").status_code == 404
    assert client.get(f"/api/v1/projects/{mine['id']}").status_code == 200  # admin sees everything
    assert editor.get("/api/v1/processing/status").status_code == 403
    assert client.get("/api/v1/processing/status").status_code == 200
    # bearer token works too; tampered token does not
    token = editor.cookies.get(auth.COOKIE)
    assert TestClient(app).get("/api/v1/projects", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert TestClient(app).get("/api/v1/projects", headers={"Authorization": f"Bearer {token[:-2]}xx"}).status_code == 401
    # logout everywhere invalidates every issued session
    assert editor.post("/api/v1/auth/logout-everywhere").status_code == 200
    assert TestClient(app).get("/api/v1/projects", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_passwords_are_hashed_not_stored():
    stored = auth.hash_password("correct horse battery")
    assert "correct horse" not in stored and stored.startswith("scrypt$")
    assert auth.verify_password("correct horse battery", stored) and not auth.verify_password("wrong", stored)


def test_production_requires_authentication(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(auth.auth_settings, "mode", "off")
    with pytest.raises(RuntimeError):
        auth.check_configuration()
    monkeypatch.setattr(auth.auth_settings, "mode", "password")
    monkeypatch.setattr(auth.auth_settings, "secret_key", "short")
    with pytest.raises(RuntimeError):
        auth.check_configuration()


def test_server_paths_are_redacted(client, auth_on, monkeypatch):
    from app.config import settings

    signup(client, "root@example.org")
    raw = f"Model missing at {settings.data_dir / 'models' / 'x.pth'} and C:\\Users\\someone\\secret.txt"
    cleaned = auth.redact(raw)
    assert str(settings.data_dir) not in cleaned and "someone" not in cleaned
    info = client.get("/api/v1/lipsync/engine").json()
    assert str(settings.data_dir) not in str(info) and str(settings.data_dir).replace("\\", "/") not in str(info)


# ---------------------------------------------------------------------------
# Remote processing mode
# ---------------------------------------------------------------------------


def test_remote_ai_worker_translation_with_token(client, monkeypatch):
    import socket

    import uvicorn

    from app import ai_worker
    from app.config import settings
    from app.services import remote_mode, translation_engine

    monkeypatch.setattr(ai_worker, "TOKEN", "t" * 40)
    monkeypatch.setattr(translation_engine, "_generate_local", lambda backend, texts, target, check: [f"[{target}] {t}" for t in texts])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(ai_worker.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        monkeypatch.setattr(remote_mode.remote_settings, "remote_url", f"http://127.0.0.1:{port}")
        monkeypatch.setattr(remote_mode.remote_settings, "remote_token", "wrong-token")
        assert remote_mode.health()["reachable"] is False and "401" in remote_mode.health()["error"]
        monkeypatch.setattr(remote_mode.remote_settings, "remote_token", "t" * 40)
        monkeypatch.setattr(settings, "translation_remote_url", None)
        monkeypatch.setattr(settings, "voice_remote_url", None)
        applied = remote_mode.apply()
        assert applied["enabled"] and settings.translation_remote_url == f"http://127.0.0.1:{port}"
        assert translation_engine.effective_backend() == "remote"
        out = translation_engine.translate_texts(["Hello.", "Where are you?"], "ur")
        assert out == ["[ur] Hello.", "[ur] Where are you?"]
        status = client.get("/api/v1/processing/remote").json()
        assert status["reachable"] is True and status["capabilities"] == ["translate", "voice", "lipsync"]
        with SessionLocal() as db:
            assert db.get(ProcessingWorker, remote_mode.REMOTE_WORKER_ID).status == "online"
    finally:
        settings.translation_remote_url = None
        settings.translation_remote_token = None
        settings.voice_remote_url = None
        from app.services import lipsync_engine

        lipsync_engine.lipsync_settings.remote_url = None
        server.should_exit = True
        thread.join(timeout=5)


def test_capabilities_report_processing(client):
    caps = client.get("/api/v1/info").json()["capabilities"]
    assert caps["worker_queue"] is True and caps["processing_mode"] == "inline" and caps["authentication"] == "off"
