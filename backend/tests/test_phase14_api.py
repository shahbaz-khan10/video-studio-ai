"""Phase 14 - HTTP API + export rendering tests."""

from __future__ import annotations

import io
import subprocess
import time
import wave

import numpy as np

from app.services.media import ffmpeg_binary
from app.services.storage import storage

SR = 24000


def _wav_bytes(seconds=0.5, freq=440.0, sr=SR) -> bytes:
    t = np.arange(int(sr * seconds)) / sr
    pcm = (0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(pcm.tobytes())
    return buf.getvalue()


def _make_video_bytes(tmp_path) -> bytes:
    src = tmp_path / "src.mp4"
    subprocess.run(
        [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=160x120:d=2",
         "-f", "lavfi", "-i", "sine=frequency=300:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", "-y", str(src)],
        check=True,
    )
    return src.read_bytes()


def _setup_project_with_video(client, tmp_path) -> tuple[str, str]:
    r = client.post("/api/v1/projects", json={"name": "P14", "target_languages": ["ur", "hi"]})
    assert r.status_code in (200, 201), r.text
    project_id = r.json()["id"]
    video_bytes = _make_video_bytes(tmp_path)
    r = client.post(
        f"/api/v1/uploads/projects/{project_id}/videos",
        files={"file": ("movie.mp4", video_bytes, "video/mp4")},
    )
    assert r.status_code in (200, 201), r.text
    video_id = r.json()["video"]["id"]
    # let the import job finish quickly; timeline works regardless
    return project_id, video_id


def test_formats_endpoint(client):
    r = client.get("/api/v1/audio-import-formats")
    assert r.status_code == 200
    body = r.json()
    assert set(body["formats"]) >= {"wav", "mp3", "aac", "m4a", "flac", "ogg"}


def test_external_audio_http_upload(client, tmp_path):
    project_id, video_id = _setup_project_with_video(client, tmp_path)
    payload = _wav_bytes(0.5)
    r = client.post(
        f"/api/v1/projects/{project_id}/audio-imports",
        files={"file": ("voiceover.wav", payload, "audio/wav")},
        data={"video_id": video_id},
    )
    assert r.status_code == 201, r.text
    audio = r.json()
    assert audio["original_filename"] == "voiceover.wav"
    assert audio["duration_ms"] > 100
    assert audio["checksum_sha256"]
    assert audio["stored_unchanged"] is True
    aid = audio["id"]

    # list
    r = client.get(f"/api/v1/projects/{project_id}/audio-imports")
    assert any(a["id"] == aid for a in r.json())

    # metadata
    assert client.get(f"/api/v1/audio-imports/{aid}").json()["id"] == aid

    # preview streaming (range)
    r = client.get(f"/api/v1/audio-imports/{aid}/file", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206
    assert len(r.content) == 100

    # waveform
    r = client.get(f"/api/v1/audio-imports/{aid}/waveform", params={"start_ms": 0, "end_ms": 500, "buckets": 50})
    assert r.status_code == 200
    assert len(r.json()["peaks"]) == 50

    # reject non-audio
    r = client.post(f"/api/v1/projects/{project_id}/audio-imports", files={"file": ("x.wav", b"not audio", "audio/wav")})
    assert r.status_code == 422

    # reject unknown extension
    r = client.post(f"/api/v1/projects/{project_id}/audio-imports", files={"file": ("x.mp4", payload, "audio/wav")})
    assert r.status_code == 422


def test_timeline_http_flow(client, tmp_path):
    project_id, video_id = _setup_project_with_video(client, tmp_path)

    # create / get timeline
    r = client.post(f"/api/v1/videos/{video_id}/timeline", json={"name": "Main"})
    assert r.status_code == 201, r.text
    tl = r.json()
    tid = tl["id"]
    kinds = [t["kind"] for t in tl["tracks"]]
    assert "urdu_dub" in kinds and "hindi_dub" in kinds and "original_english" in kinds

    # get_or_create returns same
    r = client.get(f"/api/v1/videos/{video_id}/timeline")
    assert r.json()["id"] == tid

    # find external track
    external_track = next(t for t in tl["tracks"] if t["kind"] == "external_audio")

    # import external audio
    r = client.post(f"/api/v1/projects/{project_id}/audio-imports", files={"file": ("ext.wav", _wav_bytes(1.0), "audio/wav")})
    aid = r.json()["id"]

    # add external clip
    r = client.post(f"/api/v1/timeline/{tid}/clips/external", json={"external_audio_id": aid, "start_ms": 100, "track_id": external_track["id"]})
    assert r.status_code == 201, r.text
    clip = r.json()
    cid = clip["id"]
    assert clip["source_type"] == "external_audio"

    # move
    r = client.patch(f"/api/v1/timeline/clips/{cid}/move", json={"start_ms": 500, "snap_ms": 0})
    assert r.json()["start_ms"] == 500

    # trim
    r = client.patch(f"/api/v1/timeline/clips/{cid}/trim", json={"source_in_ms": 10, "source_out_ms": 400, "duration_ms": 390})
    assert r.json()["source_in_ms"] == 10

    # split
    start = r.json()["start_ms"]
    r = client.post(f"/api/v1/timeline/clips/{cid}/split", json={"at_ms": start + 100})
    assert r.status_code == 200
    left, right = r.json()["left"], r.json()["right"]

    # join
    r = client.post("/api/v1/timeline/clips/join", json={"clip_ids": [left["id"], right["id"]]})
    assert r.status_code == 200
    joined = r.json()

    # volume + fades
    r = client.patch(f"/api/v1/timeline/clips/{joined['id']}", json={"volume_db": -6, "fade_in_ms": 50, "fade_out_ms": 75})
    assert r.json()["volume_db"] == -6

    # duplicate
    r = client.post(f"/api/v1/timeline/clips/{joined['id']}/duplicate", json={})
    dup = r.json()
    assert dup["id"] != joined["id"]

    # undo/redo
    r = client.post(f"/api/v1/timeline/{tid}/undo")
    assert r.status_code == 200
    r = client.post(f"/api/v1/timeline/{tid}/redo")
    assert r.status_code == 200

    # track toggle language
    r = client.patch(f"/api/v1/timeline/tracks/{external_track['id']}", json={"enabled": False})
    assert r.json()["enabled"] is False

    # history
    r = client.get(f"/api/v1/timeline/{tid}/history")
    assert len(r.json()) >= 3

    # save
    r = client.post(f"/api/v1/timeline/{tid}/save")
    assert r.json()["id"] == tid

    # reopen
    r = client.get(f"/api/v1/timeline/{tid}")
    assert r.json()["id"] == tid

    # delete
    # find current joined clip id via reopen
    state = r.json()
    some_clip = next(c for t in state["tracks"] for c in t["clips"] if c["source_type"] == "external_audio")
    r = client.delete(f"/api/v1/timeline/clips/{some_clip['id']}")
    assert r.status_code == 204

    # clip waveform
    state = client.get(f"/api/v1/timeline/{tid}").json()
    clip2 = next(c for t in state["tracks"] for c in t["clips"] if c["source_type"] == "external_audio")
    r = client.get(f"/api/v1/timeline/clips/{clip2['id']}/waveform", params={"buckets": 32})
    assert r.status_code == 200 and len(r.json()["peaks"]) == 32

    # clip audio streaming
    r = client.get(f"/api/v1/timeline/clips/{clip2['id']}/audio")
    assert r.status_code == 200

    # segments + characters
    assert client.get(f"/api/v1/videos/{video_id}/timeline/segments").status_code == 200
    assert client.get(f"/api/v1/projects/{project_id}/timeline-characters").status_code == 200


def test_export_plan_http(client, tmp_path):
    project_id, video_id = _setup_project_with_video(client, tmp_path)
    tid = client.post(f"/api/v1/videos/{video_id}/timeline", json={}).json()["id"]
    r = client.post(f"/api/v1/timeline/{tid}/export-plan", json={"mode": "multitrack", "container": "mkv"})
    assert r.status_code == 200
    assert r.json()["never_combines_languages"] is True
    r = client.post(f"/api/v1/timeline/{tid}/export-plan", json={"mode": "separate_language", "languages": ["ur", "hi"]})
    langs = {o["language"] for o in r.json()["outputs"]}
    assert langs == {"ur", "hi"}


def test_export_render_multitrack(client, tmp_path):
    project_id, video_id = _setup_project_with_video(client, tmp_path)
    tid = client.post(f"/api/v1/videos/{video_id}/timeline", json={}).json()["id"]
    r = client.post(f"/api/v1/timeline/{tid}/export", json={"mode": "multitrack", "container": "mkv", "include_beds": False})
    assert r.status_code == 202, r.text
    job = r.json()
    job_id = job["id"]
    # poll job
    for _ in range(120):
        j = client.get(f"/api/v1/jobs/{job_id}").json()
        if j["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.5)
    assert j["status"] == "completed", j
    out = client.get(f"/api/v1/jobs/{job_id}/timeline-export").json()
    assert out["outputs"], "export produced outputs"
    key = out["outputs"][0]["storage_key"]
    r = client.get(f"/api/v1/jobs/{job_id}/timeline-export/files/0", params={"download": 1})
    assert r.status_code == 200
    assert len(r.content) > 0
    # verify it's a real video with audio streams
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mkv", delete=False) as f:
        f.write(r.content)
        path = f.name
    probe = subprocess.run([ffmpeg_binary(), "-hide_banner", "-i", path], capture_output=True, text=True, check=False)
    assert "Video:" in probe.stderr


def test_export_render_separate_language(client, tmp_path):
    project_id, video_id = _setup_project_with_video(client, tmp_path)
    tid = client.post(f"/api/v1/videos/{video_id}/timeline", json={}).json()["id"]
    r = client.post(f"/api/v1/timeline/{tid}/export", json={"mode": "separate_language", "languages": ["ur", "hi"], "include_beds": False})
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    for _ in range(120):
        j = client.get(f"/api/v1/jobs/{job_id}").json()
        if j["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.5)
    assert j["status"] == "completed", j
    out = client.get(f"/api/v1/jobs/{job_id}/timeline-export").json()["outputs"]
    langs = {o["language"] for o in out}
    assert langs == {"ur", "hi"}, langs
