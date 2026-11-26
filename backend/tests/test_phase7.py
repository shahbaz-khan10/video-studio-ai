import os
import subprocess
import time
from pathlib import Path

import pytest
from sqlalchemy import select

import app.services.diarization as diarization_service
import app.services.transcription as transcription_service
import app.services.vision as vision_service
from app.database import SessionLocal
from app.models import Character, CharacterAssignment, MediaAsset, ProcessingJob, SpeakerTurn, Video
from app.services.diarization import DiarizationResult, SpeakerTurn as DetectedTurn
from app.services.jobs import create_character_job, run_character_job
from app.services.media import file_sha256
from app.services.storage import storage
from app.services.transcription import ASRResult, ASRSegment
from app.services.vision import FaceTrackDetection, VisionResult, VisualCandidateDetection

TERMINAL = {"completed", "failed", "cancelled"}


@pytest.fixture(scope="module")
def phase7_video(tmp_path_factory):
    try:
        import imageio_ffmpeg
    except ImportError:
        pytest.skip("imageio-ffmpeg is not installed")

    path = tmp_path_factory.mktemp("phase7") / "dialogue.mp4"
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
        pytest.skip(f"Could not generate Phase 7 test video: {result.stderr[:200]}")
    return path


def create_project(client, name="Phase 7 film"):
    response = client.post(
        "/api/v1/projects",
        json={"name": name, "source_language": "en", "target_languages": ["ur", "hi"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def upload(client, project_id, path, label="video"):
    with path.open("rb") as handle:
        response = client.post(
            f"/api/v1/uploads/projects/{project_id}/videos",
            files={"file": (f"{label}.mp4", handle.read(), "video/mp4")},
        )
    assert response.status_code == 201, response.text
    return response.json()


def wait_job(client, job_id, timeout=15):
    job = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in TERMINAL:
            return job
        time.sleep(0.05)
    return job


def asr_result():
    return ASRResult(
        segments=[
            ASRSegment(start_ms=200, end_ms=900, text="first line", confidence=0.91, language="en", words=[]),
            ASRSegment(start_ms=1200, end_ms=1900, text="second line", confidence=0.88, language="en", words=[]),
        ],
        language="en",
        backend="fake-asr",
        model="fake-model",
    )


def make_transcript(client, video_id, monkeypatch):
    monkeypatch.setattr(transcription_service, "transcribe_file", lambda *args, **kwargs: asr_result())
    job = client.post(
        f"/api/v1/videos/{video_id}/transcript/transcribe",
        json={"language": "en", "prefer_dialogue_stem": True, "word_timestamps": True},
    )
    assert job.status_code == 202, job.text
    result = wait_job(client, job.json()["id"])
    assert result["status"] == "completed", result
    return client.get(f"/api/v1/videos/{video_id}/transcript").json()


def diarization_result(embeddings=None):
    embeddings = embeddings or [[1.0, 0.0], [0.0, 1.0]]
    return DiarizationResult(
        turns=[
            DetectedTurn(
                start_ms=200,
                end_ms=900,
                label="SPEAKER_00",
                confidence=0.91,
                embedding=embeddings[0],
                overlap_possible=True,
            ),
            DetectedTurn(
                start_ms=1200,
                end_ms=1900,
                label="SPEAKER_01",
                confidence=0.88,
                embedding=embeddings[1],
            ),
        ],
        backend="fake-diarization",
        model="fake-ecapa",
        vad_model="fake-silero",
        limitations=["fake diarization limitation"],
        notes=["fake diarization note"],
    )


def fake_vision(tmp_path, with_candidate=True):
    if not with_candidate:
        return lambda *args, **kwargs: VisionResult(candidates=[], tracks=[])

    def detect(video_path, points, destination, check_active=None, on_progress=None):
        destination.mkdir(parents=True, exist_ok=True)
        import cv2
        import numpy as np

        image_path = destination / "candidate.jpg"
        cv2.imwrite(str(image_path), np.full((40, 40, 3), 120, dtype="uint8"))
        candidate = VisualCandidateDetection(
            timestamp_ms=550,
            segment_id=None,
            image_path=image_path,
            bbox=[0.1, 0.1, 0.3, 0.4],
            confidence=0.55,
            track_label="VISIBLE_00",
        )
        track = FaceTrackDetection(
            label="VISIBLE_00",
            start_ms=550,
            end_ms=550,
            confidence=0.55,
            representative_path=image_path,
            bbox_samples=[{"timestamp_ms": 550, "bbox": candidate.bbox, "segment_id": None}],
        )
        return VisionResult(
            candidates=[candidate],
            tracks=[track],
            limitations=["fake vision limitation"],
            notes=["fake vision note"],
        )

    return detect


def start_detection(client, video_id, **overrides):
    payload = {"prefer_dialogue_stem": True, "detect_faces": True, "max_candidates": 6}
    payload.update(overrides)
    response = client.post(f"/api/v1/videos/{video_id}/characters/detect", json=payload)
    assert response.status_code == 202, response.text
    return response.json()


def test_info_reports_phase7_identity_capabilities(client):
    body = client.get("/api/v1/info").json()
    assert body["version"] == "0.5.0-phase8"
    for flag in (
        "speaker_diarization",
        "character_identities",
        "visual_candidate_detection",
        "manual_identity_review",
    ):
        assert body["capabilities"][flag] is True
    assert body["capabilities"]["face_recognition"] is False
    characters = body["characters"]
    assert characters["stable_ids"] is True
    assert characters["face_recognition"] is False
    assert characters["real_world_identity_recognition"] is False
    assert characters["diarization"]["paid_api_required"] is False
    assert characters["vision"]["requires_human_confirmation"] is True


def test_detection_persists_turns_candidates_and_manual_corrections(
    client, phase7_video, tmp_path, monkeypatch
):
    monkeypatch.setattr(diarization_service, "diarize_file", lambda *args, **kwargs: diarization_result())
    monkeypatch.setattr(vision_service, "detect_candidates", fake_vision(tmp_path))

    project = create_project(client)
    uploaded = upload(client, project["id"], phase7_video)
    video_id = uploaded["video"]["id"]
    make_transcript(client, video_id, monkeypatch)

    with SessionLocal() as db:
        video = db.get(Video, video_id)
        original_checksum = file_sha256(storage.absolute_path(video.storage_key))

    job = start_detection(client, video_id)
    steps = [step["name"] for step in job["steps"]]
    assert steps == [
        "select_inputs",
        "diarize_speakers",
        "detect_faces",
        "fuse_identities",
        "persist_characters",
        "finalize",
    ]
    finished = wait_job(client, job["id"])
    assert finished["status"] == "completed", finished

    bundle = client.get(f"/api/v1/videos/{video_id}/characters").json()
    assert len(bundle["characters"]) == 2
    assert len(bundle["speaker_turns"]) == 2
    assert bundle["speaker_turns"][0]["start_ms"] == 200
    assert bundle["speaker_turns"][0]["end_ms"] == 900
    assert bundle["speaker_turns"][0]["overlap_possible"] is True
    assert len(bundle["assignments"]) == 2
    assert len(bundle["dialogue"]) == 2
    assert len(bundle["visual_candidates"]) == 1
    assert len(bundle["face_tracks"]) == 1
    assert bundle["visual_candidates"][0]["confirmed"] is False
    assert bundle["visual_candidates"][0]["frame_url"]
    assert "fake diarization limitation" in bundle["limitations"]
    assert "fake vision limitation" in bundle["limitations"]
    assert all("real" not in str(item).lower() for item in bundle["limitations"])

    character_id = bundle["characters"][0]["id"]
    other_id = bundle["characters"][1]["id"]
    segment_id = bundle["dialogue"][0]["segment_id"]

    renamed = client.patch(
        f"/api/v1/characters/{character_id}", json={"name": "Lead", "status": "confirmed"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Lead"

    assigned = client.put(
        f"/api/v1/videos/{video_id}/segments/{segment_id}/character",
        json={"character_id": other_id, "reviewed": True},
    )
    assert assigned.status_code == 200, assigned.text
    assert next(item for item in assigned.json()["dialogue"] if item["segment_id"] == segment_id)[
        "character_id"
    ] == other_id

    confirmed = client.patch(
        f"/api/v1/visual-candidates/{bundle['visual_candidates'][0]['id']}",
        json={"confirmed_character_id": character_id, "notes": "Reviewer confirmed visible person cue"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["visual_candidates"][0]["confirmed"] is True
    assert confirmed.json()["visual_candidates"][0]["confirmed_character_id"] == character_id

    reopened = client.get(f"/api/v1/videos/{video_id}/characters").json()
    assert next(item for item in reopened["characters"] if item["id"] == character_id)["name"] == "Lead"
    assert next(item for item in reopened["dialogue"] if item["segment_id"] == segment_id)[
        "character_id"
    ] == other_id
    assert reopened["visual_candidates"][0]["notes"] == "Reviewer confirmed visible person cue"

    with SessionLocal() as db:
        video = db.get(Video, video_id)
        assert file_sha256(storage.absolute_path(video.storage_key)) == original_checksum


def test_same_character_across_videos_and_uncertain_similar_voice(
    client, phase7_video, tmp_path, monkeypatch
):
    monkeypatch.setattr(diarization_service, "diarize_file", lambda *args, **kwargs: diarization_result())
    monkeypatch.setattr(vision_service, "detect_candidates", fake_vision(tmp_path, with_candidate=False))

    project = create_project(client, "Across scenes")
    first = upload(client, project["id"], phase7_video, "scene1")
    # Different bytes so the upload dedupe treats this as a distinct scene.
    variant = tmp_path / "scene2.mp4"
    variant.write_bytes(phase7_video.read_bytes() + b"phase7-scene2-variant")
    second = upload(client, project["id"], variant, "scene2")
    make_transcript(client, first["video"]["id"], monkeypatch)
    make_transcript(client, second["video"]["id"], monkeypatch)

    first_job = start_detection(client, first["video"]["id"], detect_faces=False)
    assert wait_job(client, first_job["id"])["status"] == "completed"
    first_bundle = client.get(f"/api/v1/videos/{first['video']['id']}/characters").json()
    first_ids = sorted(item["id"] for item in first_bundle["characters"])
    assert len(first_ids) == 2

    second_job = start_detection(client, second["video"]["id"], detect_faces=False)
    assert wait_job(client, second_job["id"])["status"] == "completed"
    second_bundle = client.get(f"/api/v1/videos/{second['video']['id']}/characters").json()
    assert sorted(item["id"] for item in second_bundle["characters"]) == first_ids

    project_characters = client.get(f"/api/v1/projects/{project['id']}/characters").json()
    assert {item["id"] for item in project_characters} == set(first_ids)
    assert all(item["scene_count"] >= 1 for item in project_characters)

    # A third, acoustically ambiguous voice should be flagged rather than
    # silently merged with a confident identity.
    from app.services import characters as characters_service

    with SessionLocal() as db:
        characters = list(db.scalars(select(Character).where(Character.project_id == project["id"])))
        characters.sort(key=lambda item: item.created_at)
        ambiguous = [
            DetectedTurn(
                start_ms=100,
                end_ms=600,
                label="SPEAKER_AMBIGUOUS",
                confidence=0.7,
                embedding=[0.71, 0.71],
            )
        ]
        fusion = characters_service.fuse_turns(db, second["video"]["id"], None, ambiguous)
        decision = fusion["decisions"][0]
        assert decision["decision"] == "review"
        assert decision["similarity"] >= 0.6
        assert decision["margin"] < 0.06
        db.commit()


def test_merge_split_and_retranscription_migration(client, phase7_video, tmp_path, monkeypatch):
    monkeypatch.setattr(diarization_service, "diarize_file", lambda *args, **kwargs: diarization_result())
    monkeypatch.setattr(vision_service, "detect_candidates", fake_vision(tmp_path, with_candidate=False))

    project = create_project(client, "Merge and split")
    uploaded = upload(client, project["id"], phase7_video)
    video_id = uploaded["video"]["id"]
    make_transcript(client, video_id, monkeypatch)
    job = start_detection(client, video_id, detect_faces=False)
    assert wait_job(client, job["id"])["status"] == "completed"

    bundle = client.get(f"/api/v1/videos/{video_id}/characters").json()
    first_id, second_id = [item["id"] for item in bundle["characters"]]
    segment_id = bundle["dialogue"][0]["segment_id"]

    merged = client.post(
        f"/api/v1/characters/{first_id}/merge",
        json={"character_ids": [second_id], "name": "Merged lead"},
    )
    assert merged.status_code == 200, merged.text
    assert merged.json()["name"] == "Merged lead"
    after_merge = client.get(f"/api/v1/videos/{video_id}/characters").json()
    assert len(after_merge["characters"]) == 1
    assert len(after_merge["speaker_turns"]) == 2
    assert all(turn["character_id"] == first_id for turn in after_merge["speaker_turns"])

    split = client.post(
        f"/api/v1/characters/{first_id}/split",
        json={"name": "Split character", "segment_ids": [segment_id]},
    )
    assert split.status_code == 200, split.text
    after_split = client.get(f"/api/v1/videos/{video_id}/characters").json()
    assert len(after_split["characters"]) == 2
    assignment = next(item for item in after_split["assignments"] if item["segment_id"] == segment_id)
    split_id = next(item["id"] for item in after_split["characters"] if item["name"] == "Split character")
    assert assignment["character_id"] == split_id
    assert assignment["source"] == "user_split"

    # Re-transcribing creates a new version while keeping the correction.
    make_transcript(client, video_id, monkeypatch)
    migrated = client.get(f"/api/v1/videos/{video_id}/characters").json()
    assert migrated["metadata_json"]["transcript_version"] == 2
    first_line = next(item for item in migrated["dialogue"] if item["start_ms"] == 200)
    assert first_line["character_id"] == split_id
    assert first_line["source"] == "transcript_migration"


def test_detection_requires_transcript_and_retries_failed_job(
    client, phase7_video, tmp_path, monkeypatch
):
    project = create_project(client, "Failure and retry")
    uploaded = upload(client, project["id"], phase7_video)
    video_id = uploaded["video"]["id"]

    missing = client.post(
        f"/api/v1/videos/{video_id}/characters/detect",
        json={"prefer_dialogue_stem": True, "detect_faces": False, "max_candidates": 2},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NO_TRANSCRIPT_FOR_CHARACTER_DETECTION"

    make_transcript(client, video_id, monkeypatch)
    attempts = {"count": 0}

    def flaky(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("fake diarization failure")
        return diarization_result()

    monkeypatch.setattr(diarization_service, "diarize_file", flaky)
    monkeypatch.setattr(vision_service, "detect_candidates", fake_vision(tmp_path, with_candidate=False))

    with SessionLocal() as db:
        video = db.get(Video, video_id)
        failed = create_character_job(
            db, video.project_id, video.id, {"detect_faces": False, "max_candidates": 2}
        )
        failed_id = failed.id
    run_character_job(failed_id, video_id)
    with SessionLocal() as db:
        job = db.get(ProcessingJob, failed_id)
        assert job is not None
        assert job.status == "failed"
    failed_job = client.get(f"/api/v1/jobs/{failed_id}").json()
    assert failed_job["status"] == "failed"
    assert failed_job["error_code"] == "CHARACTER_DETECTION_FAILED"

    retry = client.post(f"/api/v1/jobs/{failed_id}/retry")
    assert retry.status_code == 202, retry.text
    retried = wait_job(client, retry.json()["id"])
    assert retried["status"] == "completed", retried
    bundle = client.get(f"/api/v1/videos/{video_id}/characters").json()
    assert len(bundle["characters"]) == 2
    assert attempts["count"] == 2

    # Keep a queued job in the DB so the endpoint sees an active detection.
    with SessionLocal() as db:
        video = db.get(Video, video_id)
        pending = create_character_job(
            db, video.project_id, video.id, {"detect_faces": False, "max_candidates": 6}
        )
        pending_id = pending.id
    duplicate = client.post(
        f"/api/v1/videos/{video_id}/characters/detect",
        json={"prefer_dialogue_stem": True, "detect_faces": False, "max_candidates": 6},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "CHARACTER_DETECTION_JOB_ACTIVE"
    with SessionLocal() as db:
        db.delete(db.get(ProcessingJob, pending_id))
        db.commit()


@pytest.mark.skipif(
    os.getenv("RUN_REAL_DIARIZATION") != "1",
    reason="Set RUN_REAL_DIARIZATION=1 to download and run real ECAPA/Silero models",
)
def test_real_local_diarization_smoke(client, phase7_video, tmp_path, monkeypatch):
    """Optional real-model smoke test; disabled in normal test runs."""
    monkeypatch.setattr(transcription_service, "transcribe_file", lambda *args, **kwargs: asr_result())
    project = create_project(client, "Real diarization")
    uploaded = upload(client, project["id"], phase7_video)
    video_id = uploaded["video"]["id"]
    make_transcript(client, video_id, monkeypatch)
    job = start_detection(client, video_id, detect_faces=False)
    result = wait_job(client, job["id"], timeout=600)
    assert result["status"] == "completed", result
    bundle = client.get(f"/api/v1/videos/{video_id}/characters").json()
    assert bundle["diarization"]["effective_backend"] in {"local", "remote"}
