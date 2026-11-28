"""Phase 13 — AI lip sync.

The default suite swaps only the neural generator for a tiny deterministic
function and supplies face tracks directly; frame decoding, the mouth-region
blend, H.264 encoding, QC (frame counts, black frames, region PSNR), jobs,
queueing, retry and the API all run for real. Set RUN_REAL_LIPSYNC=1 (with the
Wav2Lip/SyncNet/YuNet weights installed) to run the real network.
"""

import hashlib
import io
import os
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import select

import app.services.lipsync as service
import app.services.lipsync_engine as engine
from app.database import SessionLocal
from app.models import Character, CharacterAssignment, ProcessingJob, Transcript, TranscriptSegment, Video, VisualCandidate
from app.models_lipsync import LipSyncResult
from app.models_timing import DubTimingPlacement
from app.services import face_tracking as ft
from app.services.storage import storage
from app.services.tts_engine import ffmpeg_binary, to_wav_bytes

W, H, FPS = 320, 240, 25.0
FACE_A = (60, 60, 80, 100)    # target face (x, y, w, h)
FACE_B = (200, 70, 70, 90)    # second visible face


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("p13") / "scene.mp4"
    result = subprocess.run(
        [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate={int(FPS)}:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=16000:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", "-y", str(path)],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        pytest.skip("ffmpeg could not create the test clip")
    return path


def detection(box):
    x, y, w, h = box
    marks = [(x + 0.3 * w, y + 0.4 * h), (x + 0.7 * w, y + 0.4 * h), (x + 0.5 * w, y + 0.6 * h),
             (x + 0.35 * w, y + 0.8 * h), (x + 0.65 * w, y + 0.8 * h)]
    return ft.Detection((float(x), float(y), float(w), float(h)), 0.95, marks)


class FakeFaces:
    """Supplies tracks for the analysis step (YuNet needs real faces)."""

    def __init__(self):
        self.mode = "evidence"  # evidence | ambiguous | none | single_low

    def __call__(self, db, video, segment, window):
        n = window["count"]
        tracks = []
        if self.mode != "none":
            a = ft.Track(id=0, frames={i: detection(FACE_A) for i in range(n)})
            a.correlation = 0.7 if self.mode == "evidence" else 0.1
            tracks.append(a)
            if self.mode != "single_low":
                b = ft.Track(id=1, frames={i: detection(FACE_B) for i in range(n)})
                b.correlation = 0.1
                tracks.append(b)
        return ft.Analysis(FPS, window["start_frame"], n, W, H, 1.0, tracks, [0.0] * n)


class FakeGenerator:
    """Stands in for Wav2Lip: returns the reference face with the lower half brightened."""

    def __init__(self):
        self.calls = 0
        self.black = False

    def __call__(self, mel, faces):
        import torch

        self.calls += 1
        out = faces[:, 3:].clone()
        out[:, :, 48:] = torch.clamp(out[:, :, 48:] + 0.5, 0, 1)
        if self.black:
            out = torch.zeros_like(out)
        return out


@pytest.fixture
def fakes(monkeypatch):
    faces = FakeFaces()
    gen = FakeGenerator()
    monkeypatch.setattr(service, "analyse", faces)
    monkeypatch.setattr(engine, "load_models", lambda need_generator=True, need_syncnet=True: (gen, None, "cpu"))
    monkeypatch.setattr(engine, "syncnet_available", lambda: False)
    monkeypatch.setattr(engine, "effective_backend", lambda: "wav2lip")
    return faces, gen


def seed(client, clip, lines=((1000, 3000), (3500, 5000)), dub=True):
    project = client.post("/api/v1/projects", json={"name": "Phase 13 film", "target_languages": ["ur", "hi"]}).json()
    with SessionLocal() as db:
        key = f"projects/{project['id']}/scene.mp4"
        storage.write_bytes(key, clip.read_bytes())
        video = Video(project_id=project["id"], original_filename="scene.mp4", storage_key=key, content_type="video/mp4",
                      size_bytes=clip.stat().st_size, duration_ms=6000, fps=FPS, width=W, height=H,
                      checksum_sha256=hashlib.sha256(clip.read_bytes()).hexdigest())
        db.add(video)
        db.flush()
        transcript = Transcript(project_id=project["id"], video_id=video.id, language="en", status="completed")
        db.add(transcript)
        db.flush()
        character = Character(project_id=project["id"], name="Thom", status="confirmed")
        db.add(character)
        db.flush()
        segs = []
        for pos, (start, end) in enumerate(lines):
            seg = TranscriptSegment(transcript_id=transcript.id, position=pos, start_ms=start, end_ms=end, text=f"line {pos}", words=[])
            db.add(seg)
            db.flush()
            segs.append(seg.id)
            db.add(CharacterAssignment(character_id=character.id, segment_id=seg.id, video_id=video.id))
            if dub:
                for language, freq in (("ur", 220), ("hi", 330)):
                    t = np.arange(int(24000 * (end - start - 200) / 1000)) / 24000
                    audio = (0.3 * np.sin(2 * np.pi * freq * t) * (np.sin(2 * np.pi * 3 * t) > 0)).astype(np.float32)
                    placement = DubTimingPlacement(project_id=project["id"], video_id=video.id, segment_id=seg.id, language=language,
                                                   status="fitted", placed_start_ms=start + 100, placed_end_ms=end - 100,
                                                   fitted_duration_ms=end - start - 200, tempo=1.0, pause_saved_ms=0, overflow_ms=0,
                                                   allow_overlap_ms=0, details_json={}, flags=[], suggestions=[])
                    db.add(placement)
                    db.flush()
                    placement.storage_key = f"projects/{project['id']}/dubfit/{language}/{placement.id}.wav"
                    storage.write_bytes(placement.storage_key, to_wav_bytes(audio, 24000))
        db.commit()
        return {"project_id": project["id"], "video_id": video.id, "segments": segs, "character_id": character.id, "key": key}


def wait_job(client, job_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def run(client, ids, language="ur", segments=None):
    response = client.post(f"/api/v1/videos/{ids['video_id']}/lipsync/generate",
                           json={"language": language, "segment_ids": segments or ids["segments"][:1]})
    assert response.status_code == 202, response.text
    return wait_job(client, response.json()["id"])


def line(client, ids, language="ur", index=0):
    data = client.get(f"/api/v1/videos/{ids['video_id']}/lipsync?language={language}").json()
    return next(item for item in data["lines"] if item["segment_id"] == ids["segments"][index])


def decode(content: bytes) -> np.ndarray:
    result = subprocess.run([ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                            input=content, capture_output=True, check=True)
    return np.frombuffer(result.stdout, np.uint8).reshape(-1, H, W, 3)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_engine_info_reports_evaluated_models_and_licences(client):
    info = client.get("/api/v1/lipsync/engine").json()
    names = " ".join(item["name"] for item in info["models_evaluated"])
    assert "LatentSync" in names and "MuseTalk" in names and "Wav2Lip" in names
    wav2lip = next(item for item in info["models_evaluated"] if item["name"].startswith("Wav2Lip"))
    assert "non-commercial" in wav2lip["license"]
    assert "approximate" in info["disclosure"]
    assert client.get("/api/v1/info").json()["capabilities"]["lip_sync"] is True


def test_mel_features_match_wav2lip_layout():
    wav = (0.1 * np.sin(2 * np.pi * 440 * np.arange(32000) / 16000)).astype(np.float32)
    mel = engine.melspectrogram(wav)
    assert mel.shape == (80, 161)  # 80 frames per second, centred
    assert mel.min() >= -4 and mel.max() <= 4
    basis = engine.mel_basis()
    assert basis.shape == (80, 401) and (basis >= 0).all()
    peak_band = int(np.argmax(mel[:, 80]))
    lo, hi = engine._hz_to_mel(engine.FMIN), engine._hz_to_mel(engine.FMAX)
    expected = (engine._hz_to_mel(440) - lo) / ((hi - lo) / (engine.N_MELS + 1)) - 1
    assert abs(peak_band - expected) <= 1  # 440 Hz lands in the band the Slaney mel scale predicts
    chunks = engine.mel_chunks(mel, 50, 25.0)
    assert len(chunks) == 50 and all(chunk.shape == (80, 16) for chunk in chunks)


def test_tracks_and_target_choice():
    frames = [[detection(FACE_A), detection(FACE_B)] for _ in range(20)]
    tracks = ft.build_tracks(frames)
    assert len(tracks) == 2
    analysis = ft.Analysis(FPS, 0, 20, W, H, 1.0, tracks, [0.0] * 20)
    for t in tracks:
        t.correlation = 0.1
    track, source, flags = ft.choose_target(analysis, None, None)
    assert track is None and source == "needs_face_selection"  # never assume a visible face is speaking
    manual = [FACE_B[0] / W, FACE_B[1] / H, FACE_B[2] / W, FACE_B[3] / H]
    track, source, _ = ft.choose_target(analysis, manual, None)
    assert source == "manual" and track.median_box()[0] == FACE_B[0]
    track, source, _ = ft.choose_target(analysis, None, [FACE_A[0] / W, FACE_A[1] / H, FACE_A[2] / W, FACE_A[3] / H])
    assert source == "phase7_confirmed" and track.median_box()[0] == FACE_A[0]
    tracks[0].correlation = 0.65
    track, source, flags = ft.choose_target(analysis, None, None)
    assert source == "audio_visual_evidence" and track.id == tracks[0].id and flags[0]["code"] == "auto_selected_face"
    empty = ft.Analysis(FPS, 0, 20, W, H, 1.0, [], [0.0] * 20)
    assert ft.choose_target(empty, None, None)[1] == "no_visible_face"


def test_track_boxes_interpolate_and_leave_gaps_untouched():
    track = ft.Track(id=0, frames={i: detection(FACE_A) for i in list(range(0, 5)) + list(range(8, 12))})
    boxes = engine.track_boxes(track, 20, 1.0, W, H)
    assert all(boxes[i] is not None for i in range(12))  # 3-frame gap interpolated
    assert all(boxes[i] is None for i in range(12, 20))  # no face → frame not modified
    x1, y1, x2, y2 = boxes[2]
    assert x1 < FACE_A[0] and y2 > FACE_A[1] + FACE_A[3]  # crop extends to the chin


def test_frame_rate_strings_preserve_ntsc():
    assert ft.frame_rate_string(23.976) == "24000/1001"
    assert ft.frame_rate_string(29.97) == "30000/1001"
    assert ft.frame_rate_string(25.0) == "25/1"


# ---------------------------------------------------------------------------
# Pipeline tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["ur", "hi"])
def test_generate_preserves_video_and_changes_only_target_mouth(client, clip, fakes, language):
    ids = seed(client, clip)
    job = run(client, ids, language)
    assert job["status"] == "completed", job
    current = line(client, ids, language)["current"]
    assert current["status"] == "generated" and current["review"] == "pending"
    assert current["target_source"] == "audio_visual_evidence" and current["audio_source"] == "phase11_placement"
    assert (current["width"], current["height"], current["fps"]) == (W, H, FPS)
    qc = current["qc"]
    assert qc["output"]["frames"] == current["frame_count"] and qc["output"]["black_frames"] == 0
    assert qc["face_coverage"] == 1.0 and qc["other_faces"] == 1
    assert qc["outside_face_psnr_db"] >= 40 and qc["upper_face_psnr_db"] >= 30
    after = client.get(current["after_url"])
    before = client.get(current["before_url"])
    assert after.status_code == 200 and after.headers["x-ai-generated"] == "true"
    a, b = decode(after.content), decode(before.content)
    assert a.shape == b.shape and a.shape[0] == current["frame_count"]
    mid = a.shape[0] // 2
    x, y, w, h = FACE_A
    mouth = (slice(y + int(0.7 * h), y + h), slice(x + 10, x + w - 10))
    assert np.abs(a[mid][mouth].astype(int) - b[mid][mouth].astype(int)).mean() > 20  # target mouth changed
    x2, y2, w2, h2 = FACE_B
    other = (slice(y2, y2 + h2), slice(x2, x2 + w2))
    assert np.abs(a[mid][other].astype(int) - b[mid][other].astype(int)).mean() < 3  # other face untouched
    eyes = (slice(y, y + int(0.35 * h)), slice(x, x + w))
    assert np.abs(a[mid][eyes].astype(int) - b[mid][eyes].astype(int)).mean() < 4  # upper face preserved
    # Source video untouched; outputs stored separately.
    with SessionLocal() as db:
        video = db.get(Video, ids["video_id"])
        assert hashlib.sha256(storage.absolute_path(ids["key"]).read_bytes()).hexdigest() == video.checksum_sha256
        result = db.get(LipSyncResult, current["id"])
        assert "/lipsync/" in result.after_key and result.after_key != ids["key"]


def test_audio_video_sync_window_and_duration(client, clip, fakes):
    ids = seed(client, clip)
    run(client, ids)
    current = line(client, ids)["current"]
    # Window covers the original line and the dub, snapped to whole frames.
    assert current["window_start_ms"] <= 1000 - 0 and current["window_end_ms"] >= 3000
    assert current["window_start_ms"] % 40 == 0 and current["window_end_ms"] % 40 == 0
    out = current["qc"]["output"]
    assert out["has_audio"] and abs(out["duration_s"] - current["frame_count"] / FPS) < 0.1
    audio = subprocess.run([ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "pipe:1"],
                           input=client.get(current["after_url"]).content, capture_output=True, check=True).stdout
    samples = np.frombuffer(audio, "<f4")
    lead = int(8000 * (1100 - current["window_start_ms"]) / 1000)
    assert np.abs(samples[: max(1, lead - 400)]).max() < 0.02  # silence before the placed dub starts
    assert np.abs(samples[lead + 400 : lead + 4000]).max() > 0.1  # dub plays where Phase 11 placed it


def test_ambiguous_faces_need_manual_choice(client, clip, fakes):
    faces, gen = fakes
    faces.mode = "ambiguous"
    ids = seed(client, clip)
    run(client, ids)
    latest = line(client, ids)["latest"]
    assert latest["status"] == "needs_face" and latest["after_url"] is None and gen.calls == 0
    assert any(flag["code"] == "face_selection_needed" for flag in latest["flags"])
    box = [FACE_B[0] / W, FACE_B[1] / H, FACE_B[2] / W, FACE_B[3] / H]
    response = client.put(f"/api/v1/videos/{ids['video_id']}/lipsync/segments/{ids['segments'][0]}/face",
                          json={"time_ms": 1500, "box": box, "character_id": ids["character_id"]})
    assert response.status_code == 200
    run(client, ids)
    current = line(client, ids)["current"]
    assert current["status"] == "generated" and current["target_source"] == "manual"
    a = decode(client.get(current["after_url"]).content)
    b = decode(client.get(current["before_url"]).content)
    x, y, w, h = FACE_A
    assert np.abs(a[10][y:y + h, x:x + w].astype(int) - b[10][y:y + h, x:x + w].astype(int)).mean() < 3  # face A now untouched
    assert client.put(f"/api/v1/videos/{ids['video_id']}/lipsync/segments/{ids['segments'][0]}/face",
                      json={"time_ms": 1500, "box": [0.5, 0.5, 2, 2]}).status_code == 422


def test_off_screen_and_no_face_lines_are_left_untouched(client, clip, fakes):
    faces, gen = fakes
    faces.mode = "none"
    ids = seed(client, clip)
    run(client, ids)
    assert line(client, ids)["latest"]["status"] == "no_face" and gen.calls == 0
    faces.mode = "evidence"
    client.put(f"/api/v1/videos/{ids['video_id']}/lipsync/segments/{ids['segments'][1]}/face", json={"time_ms": 4000, "off_screen": True})
    run(client, ids, segments=[ids["segments"][1]])
    latest = line(client, ids, index=1)["latest"]
    assert latest["status"] == "no_face" and latest["target_source"] == "manual_off_screen" and gen.calls == 0


def test_line_without_dub_audio(client, clip, fakes):
    ids = seed(client, clip, dub=False)
    run(client, ids)
    assert line(client, ids)["latest"]["status"] == "no_audio"


def test_phase7_confirmed_face_is_used(client, clip, fakes):
    faces, _ = fakes
    faces.mode = "ambiguous"
    ids = seed(client, clip)
    with SessionLocal() as db:
        db.add(VisualCandidate(project_id=ids["project_id"], video_id=ids["video_id"], segment_id=ids["segments"][0],
                               character_id=ids["character_id"], confirmed_character_id=ids["character_id"], confirmed=True,
                               timestamp_ms=1500, bbox_json=[FACE_A[0] / W, FACE_A[1] / H, FACE_A[2] / W, FACE_A[3] / H]))
        db.commit()
    run(client, ids)
    assert line(client, ids)["current"]["target_source"] == "phase7_confirmed"


def test_corrupted_output_is_detected(client, clip, fakes, monkeypatch):
    _, gen = fakes
    ids = seed(client, clip)
    real_probe = engine.probe_output
    monkeypatch.setattr(engine, "probe_output", lambda path, fps: {**real_probe(path, fps), "black_frames": 3})
    run(client, ids)
    latest = line(client, ids)["latest"]
    assert latest["status"] == "failed" and any(flag["code"] == "corrupted_frames" for flag in latest["flags"])
    assert line(client, ids)["current"] is None


def test_review_accept_reject_and_regenerate(client, clip, fakes):
    ids = seed(client, clip)
    run(client, ids)
    first = line(client, ids)["current"]
    rejected = client.post(f"/api/v1/lipsync/results/{first['id']}/review", json={"decision": "rejected", "note": "mouth too soft"}).json()
    assert rejected["review"] == "rejected" and rejected["review_note"] == "mouth too soft"
    run(client, ids)  # regenerate
    data = line(client, ids)
    assert data["attempts"] == 2 and data["current"]["attempt"] == 2
    accepted = client.post(f"/api/v1/lipsync/results/{first['id']}/review", json={"decision": "accepted"}).json()
    assert accepted["review"] == "accepted" and line(client, ids)["current"]["id"] == first["id"]
    download = client.get(f"/api/v1/lipsync/results/{first['id']}/video?variant=after&download=1")
    assert "attachment" in download.headers["content-disposition"]


def test_jobs_queue_one_at_a_time_and_retry_resumes(client, clip, fakes, monkeypatch):
    ids = seed(client, clip)
    with SessionLocal() as db:
        video = db.get(Video, ids["video_id"])
        first = service.create_lipsync_job(db, video, {"language": "ur", "segment_ids": ids["segments"]})
        first_id = first.id
    assert service._job_slot.acquire(timeout=1)
    import threading

    runner = threading.Thread(target=service.run_lipsync_job, args=(first_id, ids["video_id"], {"language": "ur", "segment_ids": ids["segments"]}))
    runner.start()
    time.sleep(1.5)
    with SessionLocal() as db:
        job = db.get(ProcessingJob, first_id)
        assert job.status == "queued" and next(s for s in job.steps if s.name == "queue").status == "running"
    service._job_slot.release()
    runner.join(timeout=60)
    with SessionLocal() as db:
        assert db.get(ProcessingJob, first_id).status == "completed"
    # Failure part-way, then retry resumes with the remaining line only.
    calls = {"n": 0}
    real = service.generate_segment

    def flaky(db, video, segment, language, job_id=None, check_active=None, progress=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise engine.LipSyncUnavailable("GPU worker went away")
        return real(db, video, segment, language, job_id, check_active, progress)

    monkeypatch.setattr(service, "generate_segment", flaky)
    job = run(client, ids, segments=ids["segments"])
    assert job["status"] == "failed" and job["error_code"] == "LIPSYNC_ENGINE_UNAVAILABLE"
    retry = client.post(f"/api/v1/jobs/{job['id']}/retry")
    assert retry.status_code == 202, retry.text
    retried = wait_job(client, retry.json()["id"])
    assert retried["status"] == "completed" and "1 lines" in next(s for s in retried["steps"] if s["name"] == "prepare")["detail"]


def test_cancel_lipsync_job(client, clip, fakes):
    ids = seed(client, clip)
    with SessionLocal() as db:
        job = service.create_lipsync_job(db, db.get(Video, ids["video_id"]), {"language": "ur", "segment_ids": ids["segments"]})
        job_id = job.id
    assert client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 200
    service.run_lipsync_job(job_id, ids["video_id"], {"language": "ur", "segment_ids": ids["segments"]})
    with SessionLocal() as db:
        assert db.get(ProcessingJob, job_id).status == "cancelled"
        assert db.scalars(select(LipSyncResult).where(LipSyncResult.video_id == ids["video_id"])).all() == []


def test_remote_gpu_worker_contract(client, clip, fakes, monkeypatch):
    import httpx

    monkeypatch.setattr(engine, "effective_backend", lambda: "remote")
    monkeypatch.setattr(engine.lipsync_settings, "remote_url", "http://gpu-worker:9000")
    seen = {}

    class Response:
        status_code = 200
        text = ""

        def __init__(self, content):
            self.content = content

    def fake_post(url, data=None, files=None, headers=None, timeout=None):
        seen.update(url=url, model=data["model"], boxes=data["face_boxes"])
        return Response(files["video"][1].read())  # worker returns the same frames

    monkeypatch.setattr(httpx, "post", fake_post)
    ids = seed(client, clip)
    run(client, ids)
    current = line(client, ids)["current"]
    assert seen["url"] == "http://gpu-worker:9000/lipsync" and seen["model"] == engine.lipsync_settings.remote_model
    assert current["status"] == "generated" and current["model"] == engine.lipsync_settings.remote_model
    assert '"' not in seen["boxes"][:1] and seen["boxes"].startswith("[[")


def test_engine_unavailable_returns_503(client, clip, monkeypatch):
    monkeypatch.setattr(engine, "effective_backend", lambda: "none")
    ids = seed(client, clip)
    response = client.post(f"/api/v1/videos/{ids['video_id']}/lipsync/generate", json={"language": "ur", "segment_ids": ids["segments"]})
    assert response.status_code == 503 and response.json()["error"]["code"] == "LIPSYNC_ENGINE_UNAVAILABLE"


def test_frame_endpoint(client, clip, fakes):
    ids = seed(client, clip)
    response = client.get(f"/api/v1/videos/{ids['video_id']}/lipsync/frame?t_ms=1200")
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg" and response.content[:2] == b"\xff\xd8"


# ---------------------------------------------------------------------------
# Real networks (opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.environ.get("RUN_REAL_LIPSYNC") != "1", reason="Set RUN_REAL_LIPSYNC=1 (and LIPSYNC_TEST_CLIP) to run Wav2Lip + SyncNet")
def test_real_wav2lip_improves_sync_confidence():
    clip_path = Path(os.environ["LIPSYNC_TEST_CLIP"])  # a talking-head clip with its own speech
    weights = Path(os.environ.get("LIPSYNC_WEIGHTS_DIR", ""))
    if weights:
        engine.lipsync_settings.weights_dir = weights
    probe = subprocess.run([ffmpeg_binary(), "-hide_banner", "-i", str(clip_path)], capture_output=True, text=True).stderr
    import re

    width, height = (int(v) for v in re.search(r"Video: .*?, (\d{2,5})x(\d{2,5})", probe).groups())
    fps = float(re.search(r"([\d.]+) fps", probe).group(1))
    count = int(fps * 3)
    detector = ft.FaceDetector(engine.lipsync_settings.effective_weights_dir / engine.lipsync_settings.face_detector)
    analysis = ft.analyse_window(clip_path, 0, count, fps, width, height, detector, None, 16000)
    assert analysis.tracks, "no face found in LIPSYNC_TEST_CLIP"
    track = max(analysis.tracks, key=lambda t: t.coverage_frames)
    boxes = engine.track_boxes(track, count, analysis.scale, width, height)
    speech = service._decode_audio(clip_path, 0, int(count / fps * 1000))
    shifted = np.roll(speech, 16000 // 2)  # audio half a second out of sync
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        wav.write_bytes(to_wav_bytes(shifted, 16000))
        before, after = Path(tmp) / "before.mp4", Path(tmp) / "after.mp4"
        engine.render_original(clip_path, 0, count, fps, width, height, wav, before)
        engine.generate_wav2lip(clip_path, 0, count, fps, width, height, boxes, shifted, wav, after)
        s_before = engine.sync_score(before, 0, count, fps, width, height, boxes, shifted)
        s_after = engine.sync_score(after, 0, count, fps, width, height, boxes, shifted)
    assert s_after["confidence"] > s_before["confidence"]
    assert abs(s_after["best_offset_frames"]) <= 3


def test_changed_dub_marks_lipsync_out_of_date(client, clip, fakes):
    ids = seed(client, clip)
    run(client, ids)
    current = line(client, ids)["current"]
    assert current["stale"] is False
    with SessionLocal() as db:
        placement = db.scalar(select(DubTimingPlacement).where(DubTimingPlacement.segment_id == ids["segments"][0],
                                                               DubTimingPlacement.language == "ur"))
        placement.placed_start_ms += 300  # Phase 11 timing changed after lip sync
        placement.placed_end_ms += 300
        db.commit()
    data = line(client, ids)
    assert data["current"]["stale"] is True and "changed" in data["current"]["stale_reason"]
    with SessionLocal() as db:
        row = db.get(LipSyncResult, current["id"])
        assert row.needs_regeneration is True  # visible to the Phase 14 timeline
    run(client, ids)  # regenerate against the new timing
    fresh = line(client, ids)["current"]
    assert fresh["attempt"] == 2 and fresh["stale"] is False


def test_rejected_result_is_not_used_downstream(client, clip, fakes):
    ids = seed(client, clip)
    run(client, ids)
    current = line(client, ids)["current"]
    client.post(f"/api/v1/lipsync/results/{current['id']}/review", json={"decision": "rejected"})
    data = line(client, ids)
    assert data["current"] is None and data["latest"]["review"] == "rejected"
    with SessionLocal() as db:
        assert db.scalars(select(LipSyncResult).where(LipSyncResult.video_id == ids["video_id"], LipSyncResult.is_current.is_(True))).all() == []
