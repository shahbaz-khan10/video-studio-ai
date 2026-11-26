import subprocess
import threading
import time

import pytest

import app.services.jobs as jobs_service
from app.config import settings
from app.database import SessionLocal
from app.services.audio import extract_audio, probe_audio_stream

STEMS = ("dialogue", "music", "sfx", "ambience")
TERMINAL = {"completed", "failed", "cancelled"}


@pytest.fixture(scope="session")
def audio_video(tmp_path_factory):
    try:
        import imageio_ffmpeg
    except ImportError:
        pytest.skip("imageio-ffmpeg is not installed")

    path = tmp_path_factory.mktemp("phase4") / "tone.mp4"
    result = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=160x90:rate=8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=8000:duration=2",
            "-c:v",
            "mpeg4",
            "-q:v",
            "6",
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
        pytest.skip(f"Could not generate audio sample: {result.stderr[:200]}")
    return path


@pytest.fixture(scope="session")
def silent_video(tmp_path_factory):
    try:
        import imageio_ffmpeg
    except ImportError:
        pytest.skip("imageio-ffmpeg is not installed")

    path = tmp_path_factory.mktemp("phase4-silent") / "silent.mp4"
    result = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=160x90:rate=8",
            "-an",
            "-c:v",
            "mpeg4",
            "-q:v",
            "6",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not path.is_file():
        pytest.skip(f"Could not generate silent sample: {result.stderr[:200]}")
    return path


def create_project(client, name="Phase 4 film"):
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


def wait_job(client, job_id):
    job = None
    for _ in range(100):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in TERMINAL:
            return job
        time.sleep(0.05)
    return job


def start_audio_job(client, video_id):
    response = client.post(f"/api/v1/videos/{video_id}/audio/separate")
    assert response.status_code == 202, response.text
    return wait_job(client, response.json()["id"])


@pytest.mark.parametrize("output_format", ["wav", "flac", "mp3", "aac"])
def test_audio_extraction_formats_preserve_stream_properties(audio_video, tmp_path, output_format):
    info = probe_audio_stream(audio_video)
    extracted = extract_audio(audio_video, tmp_path, output_format, info)
    assert extracted.is_file()
    assert extracted.stat().st_size > 0
    decoded = probe_audio_stream(extracted)
    assert decoded["sample_rate"] == info["sample_rate"]
    assert decoded["channels"] == info["channels"]


def test_info_reports_phase4_capabilities(client):
    body = client.get("/api/v1/info").json()
    assert body["version"] == "0.5.0-phase8"
    assert body["ai_stages"] == "partial"
    for flag in (
        "audio_extraction",
        "audio_separation",
        "audio_preview",
        "audio_validation",
        "audio_cpu_processing",
        "job_cancellation",
        "job_retry",
    ):
        assert body["capabilities"][flag] is True
    assert body["capabilities"]["translation"] is True
    assert body["audio"]["cpu_fallback"] is True
    assert body["audio"]["browser_gpu_required"] is False
    assert {"wav", "flac", "mp3", "aac"} <= set(body["audio"]["extraction_formats"])


def test_extraction_separation_preview_download_and_original_preservation(client, audio_video):
    original_bytes = audio_video.read_bytes()
    project = create_project(client)
    accepted = upload(client, project["id"], audio_video)
    video_id = accepted["video"]["id"]
    original_checksum = accepted["video"]["checksum_sha256"]

    job = start_audio_job(client, video_id)
    assert job["status"] == "completed", job
    assert job["error_code"] is None
    assert job["progress"] == 100
    assert [step["name"] for step in job["steps"]] == [
        "validate_source",
        "probe_audio",
        "extract_audio",
        "separate_stems",
        "validate_stems",
        "finalize",
    ]
    assert all(step["status"] == "completed" for step in job["steps"])

    bundle = client.get(f"/api/v1/videos/{video_id}/audio").json()
    assert bundle["status"] == "completed"
    assert bundle["source"]["status"] == "available"
    assert bundle["source"]["validation"]["decode_ok"] is True
    assert bundle["source"]["sample_rate"] == 8000
    assert bundle["source"]["channels"] == 1
    assert set(bundle["stems"]) == set(STEMS)
    assert all(item["status"] == "available" for item in bundle["stems"].values())

    for role in STEMS:
        stem = bundle["stems"][role]
        assert stem["validation"]["ok"] is True, stem
        assert stem["validation"]["decode_ok"] is True
        assert stem["sample_rate"] == 8000
        assert stem["duration_ms"] is not None
        assert abs(stem["duration_ms"] - bundle["source"]["duration_ms"]) <= 250

        download = client.get(stem["download_url"])
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("audio/wav")
        assert download.content[:4] == b"RIFF"
        assert download.content[8:12] == b"WAVE"

    source_download = client.get(f"/api/v1/videos/{video_id}/audio/source/download")
    assert source_download.status_code == 200
    assert source_download.content[:4] == b"RIFF"

    assets = client.get(f"/api/v1/videos/{video_id}/assets").json()
    kinds = {asset["kind"] for asset in assets}
    assert {"source_audio", *(f"stem_{role}" for role in STEMS)} <= kinds

    asset = next(item for item in assets if item["kind"] == "stem_dialogue")
    media = client.get(asset["url"], params={"download": "1"})
    assert media.status_code == 200
    assert "attachment" in media.headers["content-disposition"]

    comparison = bundle["comparison"]
    assert comparison["aligned"] is True
    assert comparison["reconstruction"]["correlation"] is not None
    assert comparison["reconstruction"]["correlation"] > 0.3
    assert bundle["limitations"]

    stream = client.get(f"/api/v1/videos/{video_id}/stream")
    assert stream.content == original_bytes
    detail = client.get(f"/api/v1/videos/{video_id}").json()
    assert detail["checksum_sha256"] == original_checksum

    duplicate = client.post(f"/api/v1/videos/{video_id}/audio/separate")
    assert duplicate.status_code == 202
    duplicate_job = wait_job(client, duplicate.json()["id"])
    assert duplicate_job["status"] == "completed"
    final_bundle = client.get(f"/api/v1/videos/{video_id}/audio").json()
    assert final_bundle["source"]["version"] == 2
    assert all(final_bundle["stems"][role]["version"] == 2 for role in STEMS)


def test_failed_audio_job_retries_with_real_work(client, audio_video, monkeypatch):
    project = create_project(client, name="Retry film")
    video_id = upload(client, project["id"], audio_video)["video"]["id"]

    def fail_extraction(*_args, **_kwargs):
        raise RuntimeError("forced extraction failure")

    monkeypatch.setattr(jobs_service, "extract_audio", fail_extraction)
    failed = start_audio_job(client, video_id)
    assert failed["status"] == "failed", failed
    assert failed["error_code"] == "AUDIO_SEPARATION_FAILED"
    assert failed["error_message"]

    monkeypatch.setattr(jobs_service, "extract_audio", extract_audio)
    retry = client.post(f"/api/v1/jobs/{failed['id']}/retry")
    assert retry.status_code == 202, retry.text
    retried = wait_job(client, retry.json()["id"])
    assert retried["status"] == "completed", retried
    assert retried["type"] == "audio_separation"
    assert [step["name"] for step in retried["steps"]] == [
        "validate_source",
        "probe_audio",
        "extract_audio",
        "separate_stems",
        "validate_stems",
        "finalize",
    ]

    bundle = client.get(f"/api/v1/videos/{video_id}/audio").json()
    assert bundle["status"] == "completed"
    assert all(item["status"] == "available" for item in bundle["stems"].values())


def test_audio_job_can_be_cancelled(client, audio_video, monkeypatch):
    project = create_project(client, name="Cancellation film")
    video_id = upload(client, project["id"], audio_video)["video"]["id"]
    with SessionLocal() as db:
        job = jobs_service.create_audio_job(db, project["id"], video_id)
        job_id = job.id

    def slow_separate(_source, _destination, cancel_check=None, progress=None):
        for _ in range(200):
            if cancel_check:
                cancel_check()
            time.sleep(0.05)
        raise AssertionError("Separation should have been cancelled")

    monkeypatch.setattr(jobs_service, "separate_audio", slow_separate)
    worker = threading.Thread(
        target=jobs_service.run_audio_separation_job,
        args=(job_id, video_id),
        daemon=True,
    )
    worker.start()

    for _ in range(100):
        current = client.get(f"/api/v1/jobs/{job_id}").json()
        if current["status"] == "running" and current["current_step"] == "separate_stems":
            break
        time.sleep(0.05)

    response = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert response.status_code == 200
    worker.join(timeout=10)
    assert not worker.is_alive()

    cancelled = client.get(f"/api/v1/jobs/{job_id}").json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["error_code"] == "CANCELLED"
    assert any(step["status"] == "cancelled" for step in cancelled["steps"])

    bundle = client.get(f"/api/v1/videos/{video_id}/audio").json()
    assert bundle["status"] == "cancelled"
    assert all(item["status"] == "unavailable" for item in bundle["stems"].values())


def test_video_without_audio_is_rejected(client, silent_video):
    project = create_project(client, name="Silent film")
    video_id = upload(client, project["id"], silent_video)["video"]["id"]
    response = client.post(f"/api/v1/videos/{video_id}/audio/separate")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NO_AUDIO_STREAM"
