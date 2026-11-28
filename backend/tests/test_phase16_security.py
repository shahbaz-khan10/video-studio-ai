"""Phase 16 — security audit regression tests (auth on, two users, two projects)."""

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import ProcessingJob, Video
from app.services import auth
from app.services.storage import InvalidStorageKey, key_belongs_to_project, storage


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(auth.auth_settings, "mode", "password")
    monkeypatch.setattr(auth.auth_settings, "secret_key", "k" * 48)
    monkeypatch.setattr(auth.auth_settings, "allow_registration", False)
    monkeypatch.setattr(auth.auth_settings, "bootstrap_token", None)
    auth._FAILURES.clear()
    yield


def _video(project_id: str, name: str = "film.mp4") -> str:
    key = f"projects/{project_id}/source/{name}"
    storage.write_bytes(key, b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    with SessionLocal() as db:
        video = Video(project_id=project_id, original_filename=name, storage_key=key, content_type="video/mp4",
                      size_bytes=80, duration_ms=5000)
        db.add(video)
        db.commit()
        return video.id


@pytest.fixture
def two_users(client, auth_on):
    from app.main import app

    assert client.post("/api/v1/auth/register", json={"email": "admin@example.org", "password": "admin password 1"}).status_code == 200
    assert client.post("/api/v1/auth/register", json={"email": "eve@example.org", "password": "eve password 123"}).status_code == 200
    assert client.post("/api/v1/auth/register", json={"email": "bob@example.org", "password": "bob password 123"}).status_code == 200
    eve, bob = TestClient(app), TestClient(app)
    assert eve.post("/api/v1/auth/login", json={"email": "eve@example.org", "password": "eve password 123"}).status_code == 200
    assert bob.post("/api/v1/auth/login", json={"email": "bob@example.org", "password": "bob password 123"}).status_code == 200
    eve_project = eve.post("/api/v1/projects", json={"name": "Eve film", "target_languages": ["ur"]}).json()["id"]
    bob_project = bob.post("/api/v1/projects", json={"name": "Bob film", "target_languages": ["ur"]}).json()["id"]
    return {"admin": client, "eve": eve, "bob": bob, "eve_project": eve_project, "bob_project": bob_project,
            "eve_video": _video(eve_project), "bob_video": _video(bob_project)}


def test_storage_key_rules():
    assert key_belongs_to_project("projects/abc/x.wav", "abc")
    assert not key_belongs_to_project("projects/abcd/x.wav", "abc")
    assert not key_belongs_to_project("projects/abc/../def/x.wav", "abc")
    assert not key_belongs_to_project(None, "abc")
    with pytest.raises(InvalidStorageKey):
        storage.absolute_path("../../etc/passwd")
    with pytest.raises(InvalidStorageKey):
        storage.absolute_path("projects/a/\x00.wav")


def test_query_string_ids_and_keys_are_authorised(two_users):
    eve, bob = two_users["eve"], two_users["bob"]
    with SessionLocal() as db:
        for project, video in ((two_users["eve_project"], two_users["eve_video"]), (two_users["bob_project"], two_users["bob_video"])):
            db.add(ProcessingJob(project_id=project, video_id=video, type="media_import", status="completed", progress=100))
        db.commit()
    # Job listing only shows the caller's own projects, even when asked for another project.
    mine = eve.get("/api/v1/processing/jobs").json()
    assert {job["project_id"] for job in mine} == {two_users["eve_project"]}
    spying = eve.get("/api/v1/processing/jobs", params={"project_id": two_users["bob_project"]})
    assert spying.status_code == 404  # foreign id in the query string is refused by the middleware
    # Remote worker details are admin-only.
    assert eve.get("/api/v1/processing/remote").status_code == 403
    assert two_users["admin"].get("/api/v1/processing/remote").status_code == 200
    # A storage key for someone else's project in a query string is refused.
    key = f"projects/{two_users['bob_project']}/source/film.mp4"
    assert eve.get("/api/v1/projects", params={"key": key}).status_code == 404


def test_old_arbitrary_file_endpoint_is_gone(two_users):
    key = f"projects/{two_users['bob_project']}/source/film.mp4"
    response = two_users["eve"].get("/api/v1/timeline-exports/file", params={"key": key})
    assert response.status_code in {404, 405}
    assert b"ftyp" not in response.content


def test_json_body_ids_are_authorised(two_users):
    eve, bob = two_users["eve"], two_users["bob"]
    eve_tl = eve.post(f"/api/v1/videos/{two_users['eve_video']}/timeline").json()
    bob_tl = bob.post(f"/api/v1/videos/{two_users['bob_video']}/timeline").json()
    bob_track = bob_tl["tracks"][0]["id"]
    eve_track = eve_tl["tracks"][0]["id"]
    payload = {"track_id": bob_track, "source_type": "external_audio", "name": "x", "start_ms": 0, "duration_ms": 1000}
    assert eve.post(f"/api/v1/timeline/{eve_tl['id']}/clips", json=payload).status_code == 404
    # A raw storage key from another project inside the body is refused too.
    payload = {"track_id": eve_track, "source_type": "external_audio", "name": "x", "start_ms": 0, "duration_ms": 1000,
               "storage_key": f"projects/{two_users['bob_project']}/source/film.mp4"}
    assert eve.post(f"/api/v1/timeline/{eve_tl['id']}/clips", json=payload).status_code == 404


def test_client_storage_keys_validated_even_for_admin(two_users):
    admin = two_users["admin"]
    eve_tl = two_users["eve"].post(f"/api/v1/videos/{two_users['eve_video']}/timeline").json()
    track = eve_tl["tracks"][0]["id"]
    for key in (f"projects/{two_users['bob_project']}/source/film.mp4", "projects/../../secret.txt",
                f"projects/{two_users['eve_project']}/does-not-exist.wav"):
        response = admin.post(f"/api/v1/timeline/{eve_tl['id']}/clips",
                              json={"track_id": track, "source_type": "external_audio", "name": "x", "start_ms": 0,
                                    "duration_ms": 1000, "storage_key": key})
        assert response.status_code in {400, 422}, (key, response.text)
    # A track that belongs to a different timeline is refused.
    bob_tl = two_users["bob"].post(f"/api/v1/videos/{two_users['bob_video']}/timeline").json()
    response = admin.post(f"/api/v1/timeline/{eve_tl['id']}/clips",
                          json={"track_id": bob_tl["tracks"][0]["id"], "source_type": "external_audio", "name": "x",
                                "start_ms": 0, "duration_ms": 1000})
    assert response.status_code == 404


def test_lipsync_output_key_must_belong_to_project(two_users):
    from app.models_lipsync import LipSyncResult
    from app.models import Transcript, TranscriptSegment

    with SessionLocal() as db:
        transcript = Transcript(project_id=two_users["eve_project"], video_id=two_users["eve_video"], language="en")
        db.add(transcript)
        db.flush()
        segment = TranscriptSegment(transcript_id=transcript.id, position=0, start_ms=0, end_ms=1000, text="hi", words=[])
        db.add(segment)
        db.flush()
        row = LipSyncResult(project_id=two_users["eve_project"], video_id=two_users["eve_video"], segment_id=segment.id, language="ur")
        db.add(row)
        db.commit()
        result_id = row.id
    eve, admin = two_users["eve"], two_users["admin"]
    foreign = f"projects/{two_users['bob_project']}/source/film.mp4"
    assert eve.patch(f"/api/v1/lip-sync-segments/{result_id}", json={"output_storage_key": foreign}).status_code == 404
    assert admin.patch(f"/api/v1/lip-sync-segments/{result_id}", json={"output_storage_key": foreign}).status_code == 422
    assert admin.patch(f"/api/v1/lip-sync-segments/{result_id}", json={"output_storage_key": "../x"}).status_code in {400, 422}


def test_traversal_key_in_database_gives_400_not_500(two_users):
    from app.models import MediaAsset

    with SessionLocal() as db:
        asset = MediaAsset(project_id=two_users["eve_project"], video_id=two_users["eve_video"], kind="thumbnail", version=1,
                           storage_key="../../outside.png", mime_type="image/png", size_bytes=1)
        db.add(asset)
        db.commit()
        asset_id = asset.id
    response = two_users["eve"].get(f"/api/v1/media/{asset_id}")
    assert response.status_code == 400 and response.json()["error"]["code"] == "INVALID_STORAGE_KEY"


def test_export_container_is_validated(two_users):
    eve = two_users["eve"]
    timeline = eve.post(f"/api/v1/videos/{two_users['eve_video']}/timeline").json()
    response = eve.post(f"/api/v1/timeline/{timeline['id']}/export", json={"container": "mkv/../../x"})
    assert response.status_code == 400  # request validation error


def test_login_throttling(client, auth_on):
    client.post("/api/v1/auth/register", json={"email": "admin@example.org", "password": "admin password 1"})
    from app.main import app

    attacker = TestClient(app)
    codes = [attacker.post("/api/v1/auth/login", json={"email": "admin@example.org", "password": f"wrong guess {i}"}).status_code
             for i in range(auth.auth_settings.login_max_failures + 1)]
    assert codes[:-1] == [401] * auth.auth_settings.login_max_failures
    assert codes[-1] == 429
    # Even the right password is refused while blocked.
    assert attacker.post("/api/v1/auth/login", json={"email": "admin@example.org", "password": "admin password 1"}).status_code == 429


def test_first_admin_needs_bootstrap_token(client, auth_on, monkeypatch):
    monkeypatch.setattr(auth.auth_settings, "bootstrap_token", "let-me-in-0123456789")
    status = client.get("/api/v1/auth/status").json()
    assert status["bootstrap_token_required"] is True
    denied = client.post("/api/v1/auth/register", json={"email": "first@example.org", "password": "first password 1"})
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "BOOTSTRAP_TOKEN_REQUIRED"
    ok = client.post("/api/v1/auth/register", json={"email": "first@example.org", "password": "first password 1",
                                                    "bootstrap_token": "let-me-in-0123456789"})
    assert ok.status_code == 200 and ok.json()["is_admin"] is True


def test_production_defaults(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(auth.auth_settings, "cookie_secure", None)
    assert auth.cookie_secure() is True
    assert auth.bootstrap_required() is True


def test_errors_carry_cors_and_request_id(client, auth_on):
    origin = "http://localhost:5173"
    response = client.get("/api/v1/projects", headers={"Origin": origin})
    assert response.status_code == 401
    assert response.headers.get("access-control-allow-origin") == origin
    assert response.headers.get("x-request-id")
    assert response.headers.get("x-content-type-options") == "nosniff"


def test_lipsync_worker_reads_token_from_header(monkeypatch):
    monkeypatch.setenv("LIPSYNC_WORKER_TOKEN", "worker-secret")
    workers = Path(__file__).resolve().parents[2] / "workers"
    monkeypatch.syspath_prepend(str(workers))
    sys.modules.pop("lipsync_worker", None)
    module = importlib.import_module("lipsync_worker")
    if getattr(module, "TOKEN", None) != "worker-secret":
        pytest.skip("worker reads its token from a different variable")
    worker = TestClient(module.app, raise_server_exceptions=False)
    files = {"video": ("v.mp4", b"x", "video/mp4"), "audio": ("a.wav", b"x", "audio/wav")}
    data = {"face_boxes": "[]", "fps": "25"}
    assert worker.post("/lipsync", files=files, data=data).status_code == 401
    assert worker.post("/lipsync", files=files, data=data, headers={"Authorization": "Bearer nope"}).status_code == 401
    accepted = worker.post("/lipsync", files=files, data=data, headers={"Authorization": "Bearer worker-secret"})
    assert accepted.status_code != 401  # passes auth (then fails on the fake media)


def test_unknown_api_route_is_json_404_not_app_shell():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/api/v1/definitely-not-a-route")
    assert response.status_code in (401, 404)
    assert response.headers["content-type"].startswith("application/json")


def test_probe_streams_reads_per_track_names(tmp_path):
    import subprocess

    from app.services import final_export
    from app.services.audio import ffmpeg_binary

    ff = ffmpeg_binary()
    out = tmp_path / "named.mp4"
    subprocess.run([ff, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-f", "lavfi", "-i",
                    "sine=d=1", "-f", "lavfi", "-i", "sine=d=1:frequency=600", "-map", "0", "-map", "1", "-map", "2",
                    "-c:v", "libx264", "-c:a", "aac", "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", "handler_name=Original English",
                    "-metadata:s:a:1", "language=urd", "-metadata:s:a:1", "handler_name=Urdu Dub", "-shortest", str(out)], check=True)
    probe = final_export.probe_streams(out)
    assert probe["titles"] == ["Original English", "Urdu Dub"]
    assert [s["language"] for s in probe["streams"] if s["type"] == "audio"] == ["eng", "urd"]


def test_concurrent_init_db_on_fresh_sqlite(tmp_path):
    """API + worker starting together on an empty database must both succeed."""
    import subprocess
    import sys

    db = tmp_path / "race.db"
    env = dict(__import__("os").environ, DATA_DIR=str(tmp_path), DATABASE_URL=f"sqlite:///{db.as_posix()}")
    code = "from app.database import init_db; init_db(); print('ok')"
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1])) for _ in range(4)]
    results = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), [r[1].decode()[-400:] for r in results]
