import hashlib
import json
import math
import subprocess

import pytest

from app.database import SessionLocal
from app.models import JobStep, ProcessingJob


HEADER = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00"


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory):
    try:
        import imageio_ffmpeg
    except ImportError:
        pytest.skip("imageio-ffmpeg is not installed")

    path = tmp_path_factory.mktemp("media") / "sample.mp4"
    result = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x180:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not path.is_file():
        pytest.skip(f"Could not generate sample video: {result.stderr[:200]}")
    return path


def create_project(client, name="Phase 3 film"):
    response = client.post(
        "/api/v1/projects",
        json={"name": name, "source_language": "en", "target_languages": ["ur", "hi"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def upload(client, project_id, path):
    with path.open("rb") as handle:
        response = client.post(
            f"/api/v1/uploads/projects/{project_id}/videos",
            files={"file": (path.name, handle.read(), "video/mp4")},
        )
    assert response.status_code == 201, response.text
    return response.json()


def test_probe_frame_index_thumbnails_and_reprobe(client, sample_video):
    project = create_project(client)
    accepted = upload(client, project["id"], sample_video)
    video_id = accepted["video"]["id"]

    detail = client.get(f"/api/v1/videos/{video_id}")
    assert detail.status_code == 200
    video = detail.json()
    assert video["probe_status"] == "probed"
    assert video["probe_source"] in {"ffprobe", "ffmpeg"}
    assert video["width"] == 320
    assert video["height"] == 180
    assert video["fps"] is not None and abs(video["fps"] - 10) < 0.2
    assert video["frame_count"] is not None and 18 <= video["frame_count"] <= 22
    assert video["duration_ms"] is not None and 1900 <= video["duration_ms"] <= 2100
    assert video["pixel_format"] == "yuv420p"
    assert video["audio_codec"] == "aac"
    assert video["audio_stream_count"] == 1
    assert video["aspect_ratio"] == "16:9"
    assert video["subtitle_streams"] == []
    assert video["stream_validation"]["video"] == "readable"
    assert video["stream_validation"]["audio"] == "readable"
    assert len(video["stream_layout"]) >= 2

    assets_response = client.get(f"/api/v1/videos/{video_id}/assets")
    assert assets_response.status_code == 200
    assets = assets_response.json()
    kinds = {asset["kind"] for asset in assets}
    assert "frame_index" in kinds
    assert "thumbnail" in kinds
    assert 1 <= sum(1 for asset in assets if asset["kind"] == "thumbnail") <= 6

    frame_asset = next(asset for asset in assets if asset["kind"] == "frame_index")
    frame_payload = client.get(frame_asset["url"])
    assert frame_payload.status_code == 200
    frame_index = json.loads(frame_payload.content)
    assert frame_index["authoritative_clock"] == "source_video_ms"
    assert frame_index["keyframe_policy"] == "source-derived; no video re-encode"
    assert frame_index["fps"] is not None and abs(frame_index["fps"] - 10) < 0.2
    assert len(frame_index["samples"]) >= 2
    assert frame_index["frame_at_ms"] == "floor(ms * fps / 1000)"

    first_thumbnail = next(asset for asset in assets if asset["kind"] == "thumbnail")
    thumb = client.get(first_thumbnail["url"])
    assert thumb.status_code == 200
    assert thumb.headers["content-type"].startswith("image/jpeg")
    assert thumb.content[:3] == b"\xff\xd8\xff"

    reprobe = client.post(f"/api/v1/videos/{video_id}/reprobe")
    assert reprobe.status_code == 202
    assert reprobe.json()["type"] == "media_reprobe"

    after = client.get(f"/api/v1/videos/{video_id}/assets", params={"kind": "frame_index"}).json()
    assert max(asset["version"] for asset in after) == 2
    assert {asset["version"] for asset in after} == {1, 2}
    assert client.get("/api/v1/media/not-found").status_code == 404


def test_resumable_upload(client):
    project = create_project(client, name="Chunked film")
    payload = HEADER + b"\x00" * (2 * 1024 * 1024 + 12345)
    init = client.post(
        f"/api/v1/uploads/projects/{project['id']}/sessions",
        json={
            "filename": "chunked.mp4",
            "size_bytes": len(payload),
            "mime_type": "video/mp4",
            "duration_ms": 1200,
            "width": 640,
            "height": 360,
        },
    )
    assert init.status_code == 201, init.text
    session = init.json()
    assert session["chunk_size"] == 1024 * 1024
    assert session["total_chunks"] == math.ceil(len(payload) / session["chunk_size"])

    for index in range(session["total_chunks"]):
        start = index * session["chunk_size"]
        chunk = payload[start : start + session["chunk_size"]]
        response = client.put(
            f"/api/v1/uploads/sessions/{session['id']}/chunks/{index}",
            content=chunk,
        )
        assert response.status_code == 200, response.text
        session = response.json()
    assert session["received_bytes"] == len(payload)
    assert sorted(session["uploaded_chunks"]) == list(range(session["total_chunks"]))

    bad_chunk = client.put(
        f"/api/v1/uploads/sessions/{session['id']}/chunks/0",
        content=b"too small",
    )
    assert bad_chunk.status_code == 422
    assert bad_chunk.json()["error"]["code"] == "INVALID_CHUNK_SIZE"

    complete = client.post(f"/api/v1/uploads/sessions/{session['id']}/complete")
    assert complete.status_code == 201, complete.text
    accepted = complete.json()
    assert accepted["video"]["size_bytes"] == len(payload)
    assert accepted["video"]["checksum_sha256"] == hashlib.sha256(payload).hexdigest()
    assert accepted["video"]["duration_ms"] == 1200
    assert client.get(f"/api/v1/uploads/sessions/{session['id']}").status_code == 404

    stream = client.get(accepted["video"]["stream_url"])
    assert stream.status_code == 200
    assert stream.content == payload


def test_incomplete_session_rejects_completion(client):
    project = create_project(client, name="Incomplete film")
    payload = HEADER + b"\x00" * (1024 * 1024 + 10)
    session = client.post(
        f"/api/v1/uploads/projects/{project['id']}/sessions",
        json={"filename": "partial.mp4", "size_bytes": len(payload), "mime_type": "video/mp4"},
    ).json()
    client.put(
        f"/api/v1/uploads/sessions/{session['id']}/chunks/0",
        content=payload[: session["chunk_size"]],
    )
    complete = client.post(f"/api/v1/uploads/sessions/{session['id']}/complete")
    assert complete.status_code == 409
    assert complete.json()["error"]["code"] == "UPLOAD_INCOMPLETE"

    cancelled = client.delete(f"/api/v1/uploads/sessions/{session['id']}")
    assert cancelled.status_code == 204
    assert client.get(f"/api/v1/uploads/sessions/{session['id']}").status_code == 404


def test_job_cancel_and_retry(client, tmp_path):
    project = create_project(client, name="Job lifecycle film")
    sample = tmp_path / "lifecycle.mp4"
    sample.write_bytes(HEADER + b"\x00" * 256)
    accepted = upload(client, project["id"], sample)
    video_id = accepted["video"]["id"]

    with SessionLocal() as db:
        queued = ProcessingJob(
            project_id=project["id"],
            video_id=video_id,
            type="media_import",
            status="queued",
            progress=0,
        )
        db.add(queued)
        db.flush()
        db.add(JobStep(job_id=queued.id, position=0, name="validate_file", status="pending"))
        db.commit()
        queued_id = queued.id

    cancel = client.post(f"/api/v1/jobs/{queued_id}/cancel")
    assert cancel.status_code == 200, cancel.text
    cancelled = cancel.json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["error_code"] == "CANCELLED"
    assert cancelled["steps"][0]["status"] == "cancelled"
    assert client.post(f"/api/v1/jobs/{queued_id}/cancel").status_code == 409

    with SessionLocal() as db:
        failed = ProcessingJob(
            project_id=project["id"],
            video_id=video_id,
            type="media_import",
            status="failed",
            progress=50,
            error_code="IMPORT_FAILED",
            error_message="Forced failure",
        )
        db.add(failed)
        db.commit()
        failed_id = failed.id

    retry = client.post(f"/api/v1/jobs/{failed_id}/retry")
    assert retry.status_code == 202, retry.text
    retried = retry.json()
    assert retried["id"] != failed_id
    assert retried["type"] == "media_import"
    assert retried["status"] in {"queued", "running", "completed"}
    assert client.get(f"/api/v1/jobs/{retried['id']}").status_code == 200
