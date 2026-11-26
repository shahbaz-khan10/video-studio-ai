import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import select

import app.services.transcription as transcription_service
from app.database import SessionLocal
from app.models import MediaAsset
from app.services.jobs import create_transcription_job, run_transcription_job
from app.services.transcription import ASRResult, ASRSegment

TERMINAL = {"completed", "failed", "cancelled"}


@pytest.fixture(scope="module")
def speech_video(tmp_path_factory):
    try:
        import imageio_ffmpeg
    except ImportError:
        pytest.skip("imageio-ffmpeg is not installed")

    path = tmp_path_factory.mktemp("phase6") / "dialogue.mp4"
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
        pytest.skip(f"Could not generate a speech test video: {result.stderr[:200]}")
    return path


def create_project(client, name="Phase 6 film"):
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


def wait_job(client, job_id, timeout=10):
    job = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in TERMINAL:
            return job
        time.sleep(0.05)
    return job


def sample_result(language="en") -> ASRResult:
    return ASRResult(
        segments=[
            ASRSegment(
                start_ms=1200,
                end_ms=1900,
                text="second line",
                confidence=0.81,
                language=language,
                words=[
                    {
                        "start_ms": 1200,
                        "end_ms": 1350,
                        "text": "second",
                        "confidence": 0.9,
                    },
                    {
                        "start_ms": 1370,
                        "end_ms": 1900,
                        "text": "line",
                        "confidence": 0.86,
                    },
                ],
            ),
            ASRSegment(
                start_ms=200,
                end_ms=900,
                text="first line",
                confidence=0.92,
                language=language,
                words=[
                    {
                        "start_ms": 200,
                        "end_ms": 500,
                        "text": "first",
                        "confidence": 0.95,
                    },
                    {
                        "start_ms": 520,
                        "end_ms": 900,
                        "text": "line",
                        "confidence": 0.91,
                    },
                ],
            ),
        ],
        language=language,
        backend="fake-asr",
        model="fake-model",
        limitations=["fake limitation"],
        notes=["fake note"],
    )


def start_transcription(client, video_id, **overrides):
    payload = {"language": "en", "prefer_dialogue_stem": True, "word_timestamps": True}
    payload.update(overrides)
    response = client.post(f"/api/v1/videos/{video_id}/transcript/transcribe", json=payload)
    assert response.status_code == 202, response.text
    return response.json()


def test_info_reports_phase6_transcription_capabilities(client):
    body = client.get("/api/v1/info").json()
    assert body["version"] == "0.4.0-phase7"
    assert body["ai_stages"] == "partial"
    for flag in ("speech_recognition", "transcript_editor", "word_timestamps", "job_cancellation", "job_retry"):
        assert body["capabilities"][flag] is True
    assert body["capabilities"]["translation"] is False
    transcription = body["transcription"]
    assert set(transcription["supported_languages"]) == {"auto", "en", "hi", "ur"}
    assert transcription["browser_processing"] is False
    assert transcription["cpu_fallback"] is True
    assert transcription["single_model_in_memory"] is True


def test_transcription_prefers_dialogue_stem_and_aligns_segments(client, speech_video, monkeypatch):
    captured: dict = {}

    def fake_transcribe(path, **kwargs):
        captured["path"] = Path(path).name
        captured["language"] = kwargs["language"]
        captured["word_timestamps"] = kwargs["word_timestamps"]
        return sample_result(kwargs.get("language") or "en")

    monkeypatch.setattr(transcription_service, "transcribe_file", fake_transcribe)

    project = create_project(client)
    upload_result = upload(client, project["id"], speech_video)
    assert wait_job(client, upload_result["job"]["id"])["status"] == "completed"
    video_id = upload_result["video"]["id"]

    separation = client.post(f"/api/v1/videos/{video_id}/audio/separate")
    assert separation.status_code == 202, separation.text
    assert wait_job(client, separation.json()["id"])["status"] == "completed"

    job = start_transcription(client, video_id, language="ur")
    final = wait_job(client, job["id"])
    assert final["status"] == "completed", final
    assert final["progress"] == 100
    assert [step["name"] for step in final["steps"]] == [
        "select_audio",
        "prepare_engine",
        "transcribe",
        "persist_transcript",
        "finalize",
    ]
    assert "dialogue" in captured["path"]
    assert captured["language"] == "ur"
    assert captured["word_timestamps"] is True

    transcript = client.get(f"/api/v1/videos/{video_id}/transcript").json()
    assert transcript["source"] == "dialogue_stem"
    assert transcript["backend"] == "fake-asr"
    assert transcript["model"] == "fake-model"
    assert transcript["status"] == "completed"
    assert transcript["language"] == "ur"
    assert transcript["limitations"] == ["fake limitation"]
    assert transcript["notes"] == ["fake note"]
    assert [segment["text"] for segment in transcript["segments"]] == ["first line", "second line"]
    assert [segment["start_ms"] for segment in transcript["segments"]] == [200, 1200]
    assert [segment["end_ms"] for segment in transcript["segments"]] == [900, 1900]
    assert transcript["segments"][0]["words"][0]["start_ms"] == 200
    assert transcript["segments"][0]["words"][0]["text"] == "first"


def test_transcription_falls_back_to_original_video(client, speech_video, monkeypatch):
    captured: dict = {}

    def fake_transcribe(path, **kwargs):
        captured["path"] = Path(path).name
        return sample_result(kwargs.get("language") or "en")

    monkeypatch.setattr(transcription_service, "transcribe_file", fake_transcribe)
    project = create_project(client, "Fallback film")
    upload_result = upload(client, project["id"], speech_video)
    assert wait_job(client, upload_result["job"]["id"])["status"] == "completed"

    job = start_transcription(client, upload_result["video"]["id"])
    assert wait_job(client, job["id"])["status"] == "completed"
    assert captured["path"].endswith(".mp4")
    transcript = client.get(f"/api/v1/videos/{upload_result['video']['id']}/transcript").json()
    assert transcript["source"] == "original_video"


def test_transcript_edit_add_delete_and_revision_conflict(client, speech_video, monkeypatch):
    monkeypatch.setattr(
        transcription_service,
        "transcribe_file",
        lambda path, **kwargs: sample_result(kwargs.get("language") or "en"),
    )
    project = create_project(client, "Editing film")
    upload_result = upload(client, project["id"], speech_video)
    assert wait_job(client, upload_result["job"]["id"])["status"] == "completed"
    video_id = upload_result["video"]["id"]
    job = start_transcription(client, video_id)
    assert wait_job(client, job["id"])["status"] == "completed"

    original = client.get(f"/api/v1/videos/{video_id}/transcript").json()
    first = original["segments"][0]
    payload = {
        "revision": original["revision"],
        "segments": [
            {
                "id": first["id"],
                "start_ms": 250,
                "end_ms": 1050,
                "text": "Corrected first line.",
                "speaker": "Speaker A",
            },
            {
                "start_ms": 1200,
                "end_ms": 1900,
                "text": "Added line.",
                "speaker": None,
            },
        ],
    }
    saved = client.put(f"/api/v1/videos/{video_id}/transcript", json=payload)
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["revision"] == original["revision"] + 1
    assert body["status"] == "edited"
    assert body["metadata_json"]["last_edited_at"]
    assert len(body["segments"]) == 2

    corrected = body["segments"][0]
    assert corrected["id"] == first["id"]
    assert corrected["start_ms"] == 250
    assert corrected["end_ms"] == 1050
    assert corrected["text"] == "Corrected first line."
    assert corrected["speaker"] == "Speaker A"
    assert corrected["edited"] is True
    assert corrected["words_stale"] is True
    assert corrected["words"][0]["text"] == "first"

    added = body["segments"][1]
    assert added["text"] == "Added line."
    assert added["edited"] is True
    assert added["words"] == []

    conflict = client.put(
        f"/api/v1/videos/{video_id}/transcript",
        json={"revision": original["revision"], "segments": payload["segments"]},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "TRANSCRIPT_REVISION_CONFLICT"

    reopened = client.get(f"/api/v1/videos/{video_id}/transcript").json()
    assert reopened == body

    zero_length = client.put(
        f"/api/v1/videos/{video_id}/transcript",
        json={
            "revision": reopened["revision"],
            "segments": [
                {"id": first["id"], "start_ms": 500, "end_ms": 500, "text": "Invalid line"}
            ],
        },
    )
    assert zero_length.status_code == 422
    assert zero_length.json()["error"]["code"] == "INVALID_SEGMENT_TIMING"

    video = client.get(f"/api/v1/videos/{video_id}").json()
    too_long = client.put(
        f"/api/v1/videos/{video_id}/transcript",
        json={
            "revision": reopened["revision"],
            "segments": [
                {
                    "id": first["id"],
                    "start_ms": 500,
                    "end_ms": int(video["duration_ms"]) + 1000,
                    "text": "Invalid line",
                }
            ],
        },
    )
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "INVALID_SEGMENT_TIMING"


def test_transcription_ignores_unvalidated_dialogue_stem(client, speech_video, monkeypatch):
    captured: dict = {}

    def fake_transcribe(path, **kwargs):
        captured["path"] = Path(path).name
        return sample_result(kwargs.get("language") or "en")

    monkeypatch.setattr(transcription_service, "transcribe_file", fake_transcribe)
    project = create_project(client, "Unvalidated stem film")
    upload_result = upload(client, project["id"], speech_video)
    assert wait_job(client, upload_result["job"]["id"])["status"] == "completed"
    video_id = upload_result["video"]["id"]

    separation = client.post(f"/api/v1/videos/{video_id}/audio/separate")
    assert wait_job(client, separation.json()["id"])["status"] == "completed"
    with SessionLocal() as db:
        asset = db.scalar(
            select(MediaAsset)
            .where(MediaAsset.video_id == video_id, MediaAsset.kind == "stem_dialogue")
            .order_by(MediaAsset.version.desc())
        )
        assert asset is not None
        metadata = dict(asset.metadata_json or {})
        metadata["validation"] = {**(metadata.get("validation") or {}), "ok": False}
        asset.metadata_json = metadata
        db.commit()

    job = start_transcription(client, video_id)
    assert wait_job(client, job["id"])["status"] == "completed"
    assert "dialogue" not in captured["path"]
    transcript = client.get(f"/api/v1/videos/{video_id}/transcript").json()
    assert transcript["source"] in {"source_audio", "original_video"}


def test_duplicate_prevention_cancellation_and_retry(client, speech_video, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocking_transcribe(path, **kwargs):
        started.set()
        assert release.wait(8), "Transcription fake was never released"
        kwargs["check_active"]()
        return sample_result(kwargs.get("language") or "en")

    monkeypatch.setattr(transcription_service, "transcribe_file", blocking_transcribe)
    project = create_project(client, "Cancel film")
    upload_result = upload(client, project["id"], speech_video)
    assert wait_job(client, upload_result["job"]["id"])["status"] == "completed"
    video_id = upload_result["video"]["id"]

    with SessionLocal() as db:
        background_job = create_transcription_job(
            db,
            project["id"],
            video_id,
            {"language": "en", "prefer_dialogue_stem": True, "word_timestamps": True},
        )
    worker = threading.Thread(
        target=run_transcription_job,
        args=(background_job.id, video_id, {"language": "en", "prefer_dialogue_stem": True, "word_timestamps": True}),
    )
    worker.start()
    assert started.wait(5), "Transcription job did not reach the engine"
    duplicate = client.post(
        f"/api/v1/videos/{video_id}/transcript/transcribe",
        json={"language": "en"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "TRANSCRIPTION_JOB_ACTIVE"

    cancelled = client.post(f"/api/v1/jobs/{background_job.id}/cancel")
    assert cancelled.status_code == 200
    release.set()
    worker.join(timeout=10)
    final = wait_job(client, background_job.id)
    assert final["status"] == "cancelled"
    assert client.get(f"/api/v1/videos/{video_id}/transcript").json()["status"] == "idle"

    retried = client.post(f"/api/v1/jobs/{background_job.id}/retry")
    assert retried.status_code == 202, retried.text
    assert retried.json()["type"] == "transcription"
    assert wait_job(client, retried.json()["id"])["status"] == "completed"
    assert client.get(f"/api/v1/videos/{video_id}/transcript").json()["version"] == 1


def test_failed_transcription_can_be_retried(client, speech_video, monkeypatch):
    calls = {"count": 0}

    def flaky_transcribe(path, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated ASR failure")
        return sample_result(language="hi")

    monkeypatch.setattr(transcription_service, "transcribe_file", flaky_transcribe)
    project = create_project(client, "Retry film")
    upload_result = upload(client, project["id"], speech_video)
    assert wait_job(client, upload_result["job"]["id"])["status"] == "completed"
    video_id = upload_result["video"]["id"]

    job = start_transcription(client, video_id, language="hi")
    failed = wait_job(client, job["id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "TRANSCRIPTION_FAILED"
    assert "simulated ASR failure" in failed["error_message"]

    retried = client.post(f"/api/v1/jobs/{job['id']}/retry")
    assert retried.status_code == 202
    completed = wait_job(client, retried.json()["id"])
    assert completed["status"] == "completed", completed
    transcript = client.get(f"/api/v1/videos/{video_id}/transcript").json()
    assert transcript["language"] == "hi"
    assert transcript["status"] == "completed"


def test_unsupported_language_is_rejected(client, speech_video):
    project = create_project(client, "Language film")
    upload_result = upload(client, project["id"], speech_video)
    assert wait_job(client, upload_result["job"]["id"])["status"] == "completed"
    response = client.post(
        f"/api/v1/videos/{upload_result['video']['id']}/transcript/transcribe",
        json={"language": "de"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_TRANSCRIPTION_LANGUAGE"


@pytest.mark.skipif(os.environ.get("RUN_REAL_ASR") != "1", reason="Set RUN_REAL_ASR=1 for a real model download")
def test_real_english_asr_with_windows_voices(tmp_path):
    wav = tmp_path / "english.wav"
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{wav.as_posix()}'); "
        "$s.Speak('The quick brown fox jumps over the lazy dog.'); "
        "$s.Dispose()"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and wav.is_file(), result.stderr
    output = transcription_service.transcribe_file(
        wav,
        language="en",
        word_timestamps=True,
    )
    text = " ".join(segment.text for segment in output.segments).lower()
    assert "fox" in text
    assert any(segment.words for segment in output.segments)
