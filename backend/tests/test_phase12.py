"""Phase 12 — professional audio mixing studio.

Stems and dub tracks are synthetic test signals with known content (tones at
known frequencies, bursts at known times), so every control is verified by
measuring the actual rendered audio.
"""

import hashlib
import io
import subprocess
import time
import wave

import numpy as np
import pytest

import app.services.mix_studio as mix
from app.database import SessionLocal
from app.models import MediaAsset, ProcessingJob, Video
from app.services import dub_timing
from app.services.storage import storage
from app.services.tts_engine import ffmpeg_binary

SR = 48000
DURATION = 12.0
DUB_BURSTS = [(2.0, 4.0), (6.5, 8.5)]  # where dub dialogue speaks (seconds)


def tone(freq, amp=0.2, seconds=DURATION, sr=SR):
    t = np.arange(int(sr * seconds)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def wav_bytes(audio, sr=SR):
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(pcm.tobytes())
    return buf.getvalue()


def dub_signal(f0=900.0, sr=24000):
    out = np.zeros(int(sr * DURATION), dtype=np.float32)
    t_all = np.arange(out.size) / sr
    for start, end in DUB_BURSTS:
        mask = (t_all >= start) & (t_all < end)
        out[mask] = 0.4 * np.sin(2 * np.pi * f0 * t_all[mask])
    return out


def decode(payload: bytes) -> np.ndarray:
    """Any rendered container → float32 stereo (n, 2) at 48 kHz."""
    result = subprocess.run([ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "2",
                             "-ar", str(SR), "pipe:1"], input=payload, capture_output=True, check=True)
    return np.frombuffer(result.stdout, dtype="<f4").reshape(-1, 2)


def band_db(audio: np.ndarray, freq: float, start: float, end: float, channel: int | None = None) -> float:
    seg = audio[int(start * SR):int(end * SR)]
    seg = seg.mean(axis=1) if channel is None else seg[:, channel]
    spectrum = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    freqs = np.fft.rfftfreq(len(seg), 1 / SR)
    band = (freqs > freq * 0.97) & (freqs < freq * 1.03)
    return float(20 * np.log10(spectrum[band].max() + 1e-12))


def rms_db(audio: np.ndarray, start: float, end: float, channel: int | None = None) -> float:
    seg = audio[int(start * SR):int(end * SR)]
    seg = seg.mean(axis=1) if channel is None else seg[:, channel]
    return float(20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-12))


@pytest.fixture
def studio(client):
    project = client.post("/api/v1/projects", json={"name": "Phase 12 film", "target_languages": ["ur", "hi"]}).json()
    with SessionLocal() as db:
        key = f"projects/{project['id']}/source.mp4"
        storage.write_bytes(key, b"\x00" * 64)
        video = Video(project_id=project["id"], original_filename="film.mp4", storage_key=key, content_type="video/mp4",
                      size_bytes=64, duration_ms=int(DURATION * 1000))
        db.add(video)
        db.flush()
        stems = {
            "stem_dialogue": tone(1500, 0.2),   # original English dialogue
            "stem_music": tone(220, 0.25),
            "stem_sfx": tone(5000, 0.1),
            "stem_ambience": tone(110, 0.1),
            "source_audio": tone(440, 0.2),
        }
        for kind, audio in stems.items():
            skey = f"projects/{project['id']}/assets/{kind}.wav"
            size = storage.write_bytes(skey, wav_bytes(audio))
            db.add(MediaAsset(project_id=project["id"], video_id=video.id, kind=kind, version=1, storage_key=skey,
                              mime_type="audio/wav", size_bytes=size, metadata_json={"backend": "test"}))
        meta = {"dub_timing": {}}
        for language, f0 in (("ur", 900.0), ("hi", 700.0)):
            tkey = f"projects/{project['id']}/dubtrack/{video.id}_{language}.wav"
            storage.write_bytes(tkey, wav_bytes(dub_signal(f0), 24000))
            meta["dub_timing"][language] = {"track_key": tkey, "clips": 2, "duration_ms": int(DURATION * 1000),
                                            "fingerprint": dub_timing._track_fingerprint([]), "rendered_at": "test"}
        video.metadata_json = meta
        db.commit()
        return {"project_id": project["id"], "video_id": video.id}


def put(client, ids, settings):
    response = client.put(f"/api/v1/projects/{ids['project_id']}/mix-studio/settings", json={"settings": settings})
    assert response.status_code == 200, response.text
    return response.json()


def base_settings(**changes):
    settings = mix.default_settings()
    settings["master"]["loudness"]["enabled"] = False
    settings["master"]["limiter"]["enabled"] = False
    settings["ducking"]["enabled"] = False
    for role in mix.ROLES:  # neutral chains so measurements isolate one control
        settings["tracks"][role]["eq"]["enabled"] = False
        settings["tracks"][role]["compressor"]["enabled"] = False
        settings["tracks"][role]["volume_db"] = 0.0
    for path, value in changes.items():
        node = settings
        keys = path.split(".")
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return settings


def preview(client, ids, target, settings, start_ms=0, length_ms=12000):
    response = client.post(f"/api/v1/videos/{ids['video_id']}/mix-studio/preview",
                           json={"target": target, "start_ms": start_ms, "length_ms": length_ms, "settings": settings})
    assert response.status_code == 200, response.text
    return decode(response.content)


def wait_job(client, job_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


# ---------------------------------------------------------------------------


def test_tracks_connect_to_real_files_only(client, studio):
    data = client.get(f"/api/v1/videos/{studio['video_id']}/mix-studio").json()
    tracks = {track["role"]: track for track in data["tracks"]}
    assert list(tracks) == ["original_dialogue", "urdu_dub", "hindi_dub", "music", "sfx", "ambience"]
    assert all(track["available"] for track in tracks.values())
    assert tracks["urdu_dub"]["origin"].startswith("Phase 11") and tracks["music"]["origin"].startswith("Phase 4")
    assert tracks["urdu_dub"]["included"] == {"original": False, "ur": True, "hi": False}
    assert tracks["original_dialogue"]["included"] == {"original": True, "ur": False, "hi": False}
    assert data["original_full"]["available"]
    assert "noise_reduction" in data["render_only_controls"] and "eq" in data["live_controls"]


def test_missing_sources_are_reported_not_faked(client):
    project = client.post("/api/v1/projects", json={"name": "Empty", "target_languages": ["ur"]}).json()
    with SessionLocal() as db:
        video = Video(project_id=project["id"], original_filename="a.mp4", storage_key="none", content_type="video/mp4",
                      size_bytes=1, duration_ms=5000)
        db.add(video)
        db.commit()
        video_id = video.id
    data = client.get(f"/api/v1/videos/{video_id}/mix-studio").json()
    assert not any(track["available"] for track in data["tracks"])
    assert all(track["reason"] for track in data["tracks"])
    response = client.post(f"/api/v1/videos/{video_id}/mix-studio/render", json={"target": "ur", "format": "wav"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "EMPTY_MIX"


def test_urdu_and_hindi_mixes_use_their_own_dub(client, studio):
    ur = preview(client, studio, "ur", base_settings())
    hi = preview(client, studio, "hi", base_settings())
    original = preview(client, studio, "original", base_settings())
    assert band_db(ur, 900, 2.2, 3.8) - band_db(ur, 700, 2.2, 3.8) > 30
    assert band_db(hi, 700, 2.2, 3.8) - band_db(hi, 900, 2.2, 3.8) > 30
    assert band_db(original, 1500, 1, 11) - band_db(ur, 1500, 1, 11) > 30  # English dialogue only in the original mix
    for audio in (ur, hi, original):
        assert band_db(audio, 220, 1, 11) > 20  # music bed present in every mix


def test_independent_volume_in_db(client, studio):
    loud = preview(client, studio, "ur", base_settings(**{"tracks.music.volume_db": -3.0}))
    quiet = preview(client, studio, "ur", base_settings(**{"tracks.music.volume_db": -15.0}))
    assert abs((band_db(loud, 220, 1, 11) - band_db(quiet, 220, 1, 11)) - 12.0) < 0.5
    assert abs(band_db(loud, 5000, 1, 11) - band_db(quiet, 5000, 1, 11)) < 0.3  # other tracks untouched


def test_mute_solo_and_enable(client, studio):
    muted = preview(client, studio, "ur", base_settings(**{"tracks.music.muted": True}))
    assert band_db(muted, 220, 1, 11) < band_db(muted, 5000, 1, 11) - 40
    soloed = preview(client, studio, "ur", base_settings(**{"tracks.sfx.solo": True}))
    assert band_db(soloed, 5000, 1, 11) > 20
    assert band_db(soloed, 220, 1, 11) < -10 and band_db(soloed, 900, 2.2, 3.8) < -10
    disabled = preview(client, studio, "ur", base_settings(**{"tracks.urdu_dub.enabled": False}))
    assert band_db(disabled, 900, 2.2, 3.8) < -10


def test_pan_balance(client, studio):
    left = preview(client, studio, "ur", base_settings(**{"tracks.music.pan": -1.0}))
    assert rms_db(left, 1, 11, 0) > -20
    assert band_db(left, 220, 1, 11, channel=1) < band_db(left, 220, 1, 11, channel=0) - 40


def test_equalizer(client, studio):
    flat = preview(client, studio, "ur", base_settings())
    hp = base_settings()
    hp["tracks"]["ambience"]["eq"] = {**hp["tracks"]["ambience"]["eq"], "enabled": True, "highpass_hz": 300}
    filtered = preview(client, studio, "ur", hp)
    assert band_db(flat, 110, 1, 11) - band_db(filtered, 110, 1, 11) > 15  # 110 Hz rumble removed
    boost = base_settings()
    boost["tracks"]["urdu_dub"]["eq"] = {**boost["tracks"]["urdu_dub"]["eq"], "enabled": True, "highpass_hz": 20,
                                         "mid_freq_hz": 900, "mid_gain_db": 6.0}
    boosted = preview(client, studio, "ur", boost)
    assert abs((band_db(boosted, 900, 2.2, 3.8) - band_db(flat, 900, 2.2, 3.8)) - 6.0) < 1.0


def test_compressor_reduces_loud_dialogue(client, studio):
    flat = preview(client, studio, "ur", base_settings())
    comp = base_settings()
    comp["tracks"]["urdu_dub"]["compressor"] = {"enabled": True, "threshold_db": -30.0, "ratio": 6.0, "attack_ms": 5.0,
                                                "release_ms": 100.0, "makeup_db": 0.0}
    compressed = preview(client, studio, "ur", comp)
    reduction = band_db(flat, 900, 2.5, 3.8) - band_db(compressed, 900, 2.5, 3.8)
    assert 8 < reduction < 20  # 0.4 peak (≈-8 dBFS) over a -30 dB threshold at 6:1


def test_fade_in_and_out(client, studio):
    settings = base_settings(**{"tracks.music.fade_in_ms": 2000, "master.fade_out_ms": 1500})
    audio = preview(client, studio, "ur", settings)
    steady = band_db(audio, 220, 9.0, 10.0)
    assert band_db(audio, 220, 0.0, 0.3) < steady - 15  # music starts near silent
    assert rms_db(audio, 11.85, 12.0) < rms_db(audio, 9.0, 10.0) - 20  # whole mix fades out


def test_ducking_keeps_dialogue_on_top(client, studio):
    off = preview(client, studio, "ur", base_settings())
    on = preview(client, studio, "ur", base_settings(**{"ducking.enabled": True}))
    during = band_db(off, 220, 2.5, 3.8) - band_db(on, 220, 2.5, 3.8)
    outside = band_db(off, 220, 9.5, 11.5) - band_db(on, 220, 9.5, 11.5)
    assert during > 4  # music pulled down under dialogue
    assert abs(outside) < 1.0  # and left alone elsewhere
    assert abs(band_db(off, 900, 2.5, 3.8) - band_db(on, 900, 2.5, 3.8)) < 0.5  # dialogue itself untouched


def test_noise_reduction_lowers_noise_floor(client, studio):
    with SessionLocal() as db:
        video = db.get(Video, studio["video_id"])
        tkey = video.metadata_json["dub_timing"]["ur"]["track_key"]
        noisy = dub_signal() + 0.02 * np.random.default_rng(1).standard_normal(int(24000 * DURATION)).astype(np.float32)
        storage.write_bytes(tkey, wav_bytes(noisy, 24000))
    plain = base_settings(**{"tracks.urdu_dub.solo": True})
    cleaned = base_settings(**{"tracks.urdu_dub.solo": True})
    cleaned["tracks"]["urdu_dub"]["noise_reduction"] = {"enabled": True, "amount_db": 20.0}
    before = preview(client, studio, "ur", plain)
    after = preview(client, studio, "ur", cleaned)
    assert rms_db(before, 4.5, 6.0) - rms_db(after, 4.5, 6.0) > 6  # gaps between lines get quieter
    assert abs(band_db(before, 900, 2.5, 3.8) - band_db(after, 900, 2.5, 3.8)) < 2  # speech kept


def test_dialogue_stays_in_sync(client, studio):
    audio = preview(client, studio, "ur", mix.default_settings())  # full default chain incl. EQ, comp, ducking, limiter
    env = np.abs(audio.mean(axis=1))
    t = np.arange(env.size) / SR
    sig = band_filter = None  # noqa: F841
    # 900 Hz energy envelope in 5 ms frames
    frame = int(0.005 * SR)
    frames = env[: env.size // frame * frame].reshape(-1, frame)
    spectrum_onsets = []
    for start, _end in DUB_BURSTS:
        window = audio[int((start - 0.3) * SR):int((start + 0.3) * SR)].mean(axis=1)
        n = window.size
        k = 900 * np.arange(n) / SR
        carrier = np.exp(-2j * np.pi * k)
        energy = np.abs(np.convolve(window * carrier, np.ones(int(0.004 * SR)), mode="same"))
        onset = np.argmax(energy > energy.max() * 0.5) / SR + start - 0.3
        spectrum_onsets.append(onset)
    for (start, _end), onset in zip(DUB_BURSTS, spectrum_onsets):
        assert abs(onset - start) < 0.012, (onset, start)


def test_settings_presets_history_and_reset(client, studio):
    pid = studio["project_id"]
    settings = put(client, studio, base_settings(**{"tracks.music.volume_db": -9.0}))
    assert settings["settings"]["tracks"]["music"]["volume_db"] == -9.0
    created = client.post(f"/api/v1/projects/{pid}/mix-studio/presets", json={"name": "Quiet music"})
    assert created.status_code == 201
    again = client.post(f"/api/v1/projects/{pid}/mix-studio/presets", json={"name": "Quiet music"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "PRESET_EXISTS"
    put(client, studio, base_settings(**{"tracks.music.volume_db": 3.0}))
    overwrite = client.post(f"/api/v1/projects/{pid}/mix-studio/presets", json={"name": "Quiet music", "overwrite": True})
    assert overwrite.json()["settings"]["tracks"]["music"]["volume_db"] == 3.0
    client.post(f"/api/v1/projects/{pid}/mix-studio/presets", json={"name": "Loud sfx", "settings": base_settings(**{"tracks.sfx.volume_db": 6.0})})
    presets = client.get(f"/api/v1/projects/{pid}/mix-studio/presets").json()
    loud = next(item for item in presets if item["name"] == "Loud sfx")
    applied = client.post(f"/api/v1/projects/{pid}/mix-studio/presets/{loud['id']}/apply").json()
    assert applied["settings"]["tracks"]["sfx"]["volume_db"] == 6.0
    assert applied["history"][-1]["reason"] == "applied preset “Loud sfx”"  # why the previous version was replaced
    restored = client.post(f"/api/v1/projects/{pid}/mix-studio/settings/restore-previous").json()
    assert restored["settings"]["tracks"]["music"]["volume_db"] == 3.0
    reset = client.post(f"/api/v1/projects/{pid}/mix-studio/settings/reset").json()
    assert reset["settings"] == mix.default_settings()
    assert client.delete(f"/api/v1/projects/{pid}/mix-studio/presets/{loud['id']}").status_code == 204
    # Phase 5 mixer keeps working independently.
    assert client.get(f"/api/v1/projects/{pid}/audio-mix").status_code == 200


def test_settings_validation(client, studio):
    bad = client.put(f"/api/v1/projects/{studio['project_id']}/mix-studio/settings", json={"settings": {"tracks": {"bogus": {}}}})
    assert bad.status_code == 422
    clamped = put(client, studio, {"tracks": {"music": {"volume_db": 99, "pan": -7}}, "master": {"limiter": {"ceiling_db": 5}}})
    assert clamped["settings"]["tracks"]["music"]["volume_db"] == 12.0 and clamped["settings"]["tracks"]["music"]["pan"] == -1.0
    assert clamped["settings"]["master"]["limiter"]["ceiling_db"] == -0.1
    conflict = client.put(f"/api/v1/projects/{studio['project_id']}/mix-studio/settings", json={"settings": {}, "base_revision": 1})
    assert conflict.status_code == 409


def _checksums(ids):
    with SessionLocal() as db:
        assets = db.query(MediaAsset).filter(MediaAsset.video_id == ids["video_id"]).all()
        video = db.get(Video, ids["video_id"])
        paths = [storage.absolute_path(a.storage_key) for a in assets] + [
            storage.absolute_path(v["track_key"]) for v in video.metadata_json["dub_timing"].values()]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


@pytest.mark.parametrize("target,fmt", [("ur", "wav"), ("hi", "aac")])
def test_render_validate_download(client, studio, target, fmt):
    before = _checksums(studio)
    response = client.post(f"/api/v1/videos/{studio['video_id']}/mix-studio/render", json={"target": target, "format": fmt})
    assert response.status_code == 202, response.text
    job = wait_job(client, response.json()["id"])
    assert job["status"] == "completed", job
    data = client.get(f"/api/v1/videos/{studio['video_id']}/mix-studio").json()
    render = data["renders"][0]
    assert render["status"] == "ready" and render["target"] == target and render["format"] == fmt
    stats = render["stats"]
    assert abs(stats["duration_s"] - DURATION) <= 0.06
    assert stats["sample_rate"] == 48000 and stats["channels"] == "stereo"
    assert abs(stats["integrated_lufs"] - (-16.0)) < 1.0  # loudness normalised to target
    assert stats["true_peak_dbfs"] <= -0.3  # limiter ceiling -1 dBFS (true peak can exceed sample peak slightly)
    payload = client.get(render["download_url"])
    assert payload.status_code == 200 and "attachment" in payload.headers["content-disposition"]
    assert payload.headers["content-disposition"].endswith(".wav\"" if fmt == "wav" else ".m4a\"") or (".wav" if fmt == "wav" else ".m4a") in payload.headers["content-disposition"]
    audio = decode(payload.content)
    assert abs(audio.shape[0] / SR - DURATION) <= 0.06
    dub_freq = 900 if target == "ur" else 700
    assert band_db(audio, dub_freq, 2.5, 3.8) > band_db(audio, 220, 2.5, 3.8)  # dialogue above music while speaking
    assert _checksums(studio) == before  # sources untouched


def test_cancel_and_retry_render(client, studio):
    with SessionLocal() as db:
        job = mix.create_render_job(db, db.get(Video, studio["video_id"]), "ur", "wav")
        job_id = job.id
    assert client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 200
    mix.run_render_job(job_id, studio["video_id"], None)
    with SessionLocal() as db:
        assert db.get(ProcessingJob, job_id).status == "cancelled"
    retry = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert retry.status_code == 202, retry.text
    assert wait_job(client, retry.json()["id"])["status"] == "completed"


def test_capability_flag(client, studio):
    caps = client.get("/api/v1/info").json()["capabilities"]
    assert caps["mixing_studio"] is True and caps["lip_sync"] is True  # enabled by Phase 13
