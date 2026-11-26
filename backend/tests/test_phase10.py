"""Phase 10 — natural voice generation (synthetic dialogue audio).

The default suite replaces only the neural synthesis call with a deterministic
voiced signal; text chunking, pitch/tone/tempo shaping (ffmpeg), resampling,
normalisation, storage, QC, jobs and the API all run for real.
Set RUN_REAL_TTS=1 to synthesise real Urdu and Hindi with MMS-TTS.
"""

import io
import os
import time
import wave

import numpy as np
import pytest
from sqlalchemy import select

import app.services.dub_audio as dub
import app.services.tts_engine as tts
from app import models
from app.database import SessionLocal
from app.models import (
    Character,
    CharacterAssignment,
    CharacterTranslationProfile,
    ProcessingJob,
    SegmentTranslation,
    Transcript,
    TranscriptSegment,
    Video,
)
from app.models_tts import DubAudioTake
from app.services.storage import storage

LINES = [
    ("Ali", "Sara, where are you going?", "سارہ، تم کہاں جا رہی ہو؟", "सारा, तुम कहाँ जा रही हो?", 0),
    ("Sara", "I am going to the market.", "میں بازار جا رہی ہوں۔", "मैं बाजार जा रही हूँ।", 3000),
    ("Ali", "Don't worry.", "فکر نہ کرو۔", "चिंता मत करो।", 6000),
    ("Sara", "I waited all day.", "میں سارا دن انتظار کرتی رہی۔", "मैं सारा दिन इंतजार करती रही।", 60000),
    ("Ali", "We must leave now.", "ہمیں ابھی نکلنا ہوگا۔", "हमें अभी निकलना होगा।", 63000),
    ("Sara", "Listen to me.", "میری بات سنو۔", "मेरी बात सुनो।", 120000),
]


class FakeSynth:
    """Stands in for the neural model: a voiced tone whose length follows the text."""

    def __init__(self):
        self.calls = []
        self.mode = "ok"
        self.fail_after = None

    def __call__(self, text, render, check_active=None, reference=None):
        if check_active:
            check_active()
        self.calls.append({"text": text, "seed": render.seed, "speed": render.speed, "synth": render.synth,
                           "engine": render.engine, "emotion": render.emotion, "reference": reference,
                           "characteristics": dict(render.characteristics)})
        if self.fail_after is not None:
            if self.fail_after <= 0:
                raise tts.TtsUnavailable("simulated GPU worker crash")
            self.fail_after -= 1
        sr = 16000
        seconds = max(0.4, 0.07 * len(text)) / max(0.5, render.speed if "speed" in render.native else 1.0)
        t = np.arange(int(sr * seconds)) / sr
        rng = np.random.default_rng(render.seed % 2**32)
        f0 = 120 + (render.seed % 40)
        signal = 0.4 * np.sin(2 * np.pi * f0 * t) + 0.15 * np.sin(2 * np.pi * 2 * f0 * t) + 0.01 * rng.standard_normal(t.size)
        envelope = np.clip(np.sin(np.pi * t / seconds) * 3, 0, 1)
        audio = (signal * envelope).astype(np.float32)
        if self.mode == "silent":
            audio = np.zeros_like(audio)
        elif self.mode == "loud":
            audio = audio * 8  # overdriven far past full scale
        return audio, sr


@pytest.fixture
def synth(monkeypatch):
    fake = FakeSynth()
    monkeypatch.setattr(tts, "synthesize_raw", fake)
    monkeypatch.setattr(tts, "unsupported_characters", lambda text, language: [])
    monkeypatch.setattr(tts, "load_builtin", lambda language, check_active=None: (None, None))
    monkeypatch.setattr(tts, "phase9_engine", lambda: None)
    return fake


def seed(client, lines=LINES, with_file=False, genders=None):
    project = client.post("/api/v1/projects", json={"name": "Phase 10 film", "target_languages": ["ur", "hi"]}).json()
    genders = genders if genders is not None else {"Ali": "male", "Sara": "female"}
    with SessionLocal() as db:
        key = f"projects/{project['id']}/source.mp4"
        if with_file:
            storage.write_bytes(key, b"\x00" * 64)
        video = Video(project_id=project["id"], original_filename="film.mp4", storage_key=key,
                      content_type="video/mp4", size_bytes=64, duration_ms=300000)
        db.add(video)
        db.flush()
        transcript = Transcript(project_id=project["id"], video_id=video.id, language="en", status="completed")
        db.add(transcript)
        db.flush()
        chars, segments = {}, []
        for position, (name, en, ur, hi, start) in enumerate(lines):
            segment = TranscriptSegment(transcript_id=transcript.id, position=position, start_ms=start,
                                        end_ms=start + 2500, text=en, words=[])
            db.add(segment)
            db.flush()
            segments.append(segment.id)
            if name not in chars:
                chars[name] = Character(project_id=project["id"], name=name, status="confirmed")
                db.add(chars[name])
                db.flush()
                if genders.get(name):
                    db.add(CharacterTranslationProfile(project_id=project["id"], character_id=chars[name].id, gender=genders[name]))
            db.add(CharacterAssignment(character_id=chars[name].id, segment_id=segment.id, video_id=video.id))
            for language, text in (("ur", ur), ("hi", hi)):
                if text:
                    db.add(SegmentTranslation(project_id=project["id"], video_id=video.id, segment_id=segment.id,
                                              language=language, text=text, source_text=en))
        db.commit()
        return {"project_id": project["id"], "video_id": video.id, "segments": segments,
                "characters": {name: item.id for name, item in chars.items()}}


def generate(client, ids, index, language="ur", **payload):
    return client.post(
        f"/api/v1/videos/{ids['video_id']}/dub-audio/segments/{ids['segments'][index]}/generate",
        json={"language": language, **payload},
    )


def wait_job(client, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def wav_info(payload: bytes) -> tuple[int, int, int, np.ndarray]:
    with wave.open(io.BytesIO(payload), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
        return handle.getframerate(), handle.getnchannels(), handle.getsampwidth(), np.frombuffer(frames, dtype="<i2")


# ---------------------------------------------------------------------------


def test_engine_info_is_honest_about_capabilities(client, synth):
    info = client.get("/api/v1/dub-audio/engine").json()
    assert info["builtin"]["license"].startswith("CC-BY-NC-4.0")
    assert info["builtin"]["commercial_use"] is False
    assert info["sample_rate"] == 24000
    assert "synthetic" in info["synthetic_disclosure"]
    assert set(info["all_emotions"]) >= {"happy", "sad", "angry", "fearful", "excited", "calm", "whispering", "shouting", "serious"}


def test_generate_urdu_and_hindi_take_and_download(client, synth):
    ids = seed(client)
    for language in ("ur", "hi"):
        response = generate(client, ids, 0, language)
        assert response.status_code == 200, response.text
        line = response.json()
        take = line["current_take"]
        assert take["status"] == "ready" and take["is_current"] and take["synthetic"] is True
        assert line["status"] == "ready"
        assert take["text"] == line["text"] and take["language"] == language
        audio = client.get(take["audio_url"])
        assert audio.status_code == 200 and audio.headers["x-synthetic-audio"] == "true"
        sr, channels, width, samples = wav_info(audio.content)
        assert (sr, channels, width) == (24000, 1, 2)
        peak = np.abs(samples).max() / 32768
        assert 0.05 < peak <= 10 ** (-1 / 20) + 1e-3  # audible, never past the -1 dBFS ceiling
        download = client.get(take["download_url"])
        assert "attachment" in download.headers["content-disposition"]
        assert f"Ali_{language}_0001_take1.wav" in download.headers["content-disposition"]


def test_line_without_translation_is_rejected(client, synth):
    ids = seed(client, [("Ali", "Hello.", "", "", 0)])
    assert generate(client, ids, 0, "ur").status_code == 409


def test_character_voice_consistency_across_scenes(client, synth):
    ids = seed(client)
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/dub-audio?language=ur").json()
    voices = {item["name"]: item["voice"] for item in bundle["characters"]}
    assert voices["Ali"]["fingerprint"] != voices["Sara"]["fingerprint"]
    assert voices["Sara"]["pitch_semitones"] >= 4  # female default on a male-sounding base voice
    assert voices["Ali"]["pitch_semitones"] <= 1.5
    assert voices["Ali"]["source"] == voices["Sara"]["source"] == "default"
    # Generate every line: each character keeps identical settings in every scene.
    for index in range(len(LINES)):
        assert generate(client, ids, index, "ur").status_code == 200
    with SessionLocal() as db:
        takes = db.scalars(select(DubAudioTake).where(DubAudioTake.video_id == ids["video_id"])).all()
        by_character: dict[str, set] = {}
        for take in takes:
            v = take.voice_json
            by_character.setdefault(take.character_id, set()).add(
                (v["pitch_semitones"], v["speed"], v["tone"], v["seed"], take.voice_fingerprint)
            )
    assert all(len(settings) == 1 for settings in by_character.values())
    assert len({next(iter(s)) for s in by_character.values()}) == 2
    # Settings are deterministic across separate requests / restarts.
    again = client.get(f"/api/v1/videos/{ids['video_id']}/dub-audio?language=ur").json()
    assert {c["name"]: c["voice"]["fingerprint"] for c in again["characters"]} == {k: v["fingerprint"] for k, v in voices.items()}


def test_regenerate_select_and_approve(client, synth):
    ids = seed(client)
    first = generate(client, ids, 1, "hi").json()["current_take"]
    second_line = generate(client, ids, 1, "hi", new_variation=True).json()
    second = second_line["current_take"]
    assert second["take_number"] == 2 and second["id"] != first["id"]
    assert second["voice"]["seed"] != first["voice"]["seed"]  # different delivery
    for key in ("pitch_semitones", "speed", "tone"):
        assert second["voice"][key] == first["voice"][key]  # same character voice
    assert [take["take_number"] for take in second_line["takes"]] == [2, 1]
    assert second_line["status"] == "ready"
    selected = client.post(f"/api/v1/dub-audio/takes/{first['id']}/select").json()
    assert selected["current_take"]["id"] == first["id"]
    approved = client.post(f"/api/v1/dub-audio/takes/{first['id']}/approve", json={"approved": True}).json()
    assert approved["status"] == "approved" and approved["current_take"]["approved"] is True


def test_emotion_controls_only_where_supported(client, synth):
    ids = seed(client)
    response = generate(client, ids, 0, "ur", emotion="angry")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EMOTION_NOT_SUPPORTED"
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/dub-audio?language=ur").json()
    assert all(item["voice"]["emotions"] == ["neutral"] for item in bundle["characters"])


def test_long_dialogue_is_chunked_with_pauses(client, synth):
    long_ur = "میں نے تمہیں کہا تھا۔ " * 6 + "پھر… ہم چلے گئے۔"
    ids = seed(client, [("Ali", "Long speech.", long_ur, "", 0)])
    take = generate(client, ids, 0, "ur").json()["current_take"]
    assert take["stats"]["chunks"] == 8
    assert len(synth.calls) == 8
    assert take["duration_ms"] > 8 * 400
    chunks = tts.chunk_text(long_ur)
    assert chunks[-2].pause_after_ms == tts.tts_settings.ellipsis_pause_ms
    assert chunks[0].pause_after_ms == tts.tts_settings.sentence_pause_ms and chunks[-1].pause_after_ms == 0


def test_loud_input_is_normalised_without_clipping(client, synth):
    synth.mode = "loud"
    ids = seed(client)
    take = generate(client, ids, 0, "ur").json()["current_take"]
    assert take["stats"]["peak_dbfs"] <= -0.99
    assert take["stats"]["clipped_ratio"] == 0
    assert not any(flag["code"] == "clipping" for flag in take["qc_flags"])


def test_empty_audio_is_detected_and_keeps_previous_take(client, synth):
    ids = seed(client)
    good = generate(client, ids, 2, "ur").json()["current_take"]
    synth.mode = "silent"
    line = generate(client, ids, 2, "ur").json()
    assert line["status"] == "failed"
    assert line["takes"][0]["status"] == "failed"
    assert any(flag["code"] == "empty_audio" for flag in line["takes"][0]["qc_flags"])
    assert line["current_take"]["id"] == good["id"]  # good audio is not replaced by a failure


def test_stored_audio_validation_detects_corruption(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"RIFF....not audio")
    assert tts.validate_wav_file(bad)["ok"] is False


def test_batch_generation_skips_approved_and_up_to_date(client, synth):
    ids = seed(client)
    response = client.post(f"/api/v1/videos/{ids['video_id']}/dub-audio/generate", json={"language": "ur"})
    assert response.status_code == 202, response.text
    job = wait_job(client, response.json()["id"])
    assert job["status"] == "completed", job
    synth_step = next(step for step in job["steps"] if step["name"] == "synthesize")
    assert synth_step["detail"] == "6 generated · 0 failed"
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/dub-audio?language=ur").json()
    assert bundle["summary"]["ready"] == 6

    # Nothing pending → no pointless regeneration.
    again = client.post(f"/api/v1/videos/{ids['video_id']}/dub-audio/generate", json={"language": "ur"})
    assert again.status_code == 409

    # Approve one line, change another line's translation, then run "all".
    first = bundle["lines"][0]["current_take"]["id"]
    client.post(f"/api/v1/dub-audio/takes/{first}/approve", json={"approved": True})
    with SessionLocal() as db:
        row = db.scalar(select(SegmentTranslation).where(SegmentTranslation.segment_id == ids["segments"][3], SegmentTranslation.language == "ur"))
        row.text = "میں پورا دن انتظار کرتی رہی۔"
        db.commit()
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/dub-audio?language=ur").json()
    assert bundle["lines"][3]["status"] == "stale_text"
    synth.calls.clear()
    job = wait_job(client, client.post(f"/api/v1/videos/{ids['video_id']}/dub-audio/generate", json={"language": "ur"}).json()["id"])
    assert job["status"] == "completed"
    assert [call["text"] for call in synth.calls] == ["میں پورا دن انتظار کرتی رہی۔"]
    synth.calls.clear()
    job = wait_job(client, client.post(f"/api/v1/videos/{ids['video_id']}/dub-audio/generate", json={"language": "ur", "mode": "all"}).json()["id"])
    assert len(synth.calls) == 5  # approved line protected even in "all"
    assert "1 approved" in next(step for step in job["steps"] if step["name"] == "select_lines")["detail"]


def test_batch_by_character_and_retry_failed_lines(client, synth):
    ids = seed(client)
    payload = {"language": "hi", "character_id": ids["characters"]["Sara"]}
    synth.mode = "silent"
    job = wait_job(client, client.post(f"/api/v1/videos/{ids['video_id']}/dub-audio/generate", json=payload).json()["id"])
    assert job["status"] == "completed"
    assert "3 failed" in next(step for step in job["steps"] if step["name"] == "synthesize")["detail"]
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/dub-audio?language=hi").json()
    assert bundle["summary"]["failed"] == 3 and bundle["summary"]["missing"] == 3
    synth.mode = "ok"
    synth.calls.clear()
    retry = client.post(f"/api/v1/videos/{ids['video_id']}/dub-audio/generate", json={"language": "hi", "mode": "failed"})
    assert wait_job(client, retry.json()["id"])["status"] == "completed"
    assert len(synth.calls) == 3
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/dub-audio?language=hi").json()
    assert bundle["summary"]["ready"] == 3


def test_failed_job_retry_resumes(client, synth, monkeypatch):
    ids = seed(client, with_file=True)
    calls = {"n": 0}

    def flaky_load(language, check_active=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise tts.TtsUnavailable("model download failed")
        return None, None

    monkeypatch.setattr(tts, "load_builtin", flaky_load)
    job = wait_job(client, client.post(f"/api/v1/videos/{ids['video_id']}/dub-audio/generate", json={"language": "ur"}).json()["id"])
    assert job["status"] == "failed" and job["error_code"] == "TTS_ENGINE_UNAVAILABLE"
    retry = client.post(f"/api/v1/jobs/{job['id']}/retry")
    assert retry.status_code == 202, retry.text
    retried = wait_job(client, retry.json()["id"])
    assert retried["status"] == "completed", retried
    assert client.get(f"/api/v1/videos/{ids['video_id']}/dub-audio?language=ur").json()["summary"]["ready"] == 6


def test_retry_skips_lines_finished_before_failure(client, synth):
    ids = seed(client, with_file=True)
    with SessionLocal() as db:
        video = db.get(Video, ids["video_id"])
        job = dub.create_dub_job(db, video, {"language": "ur"})
        job_id = job.id
    synth.fail_after = 2  # two lines succeed, then the "worker" fails every call
    dub.run_dub_job(job_id, ids["video_id"], {"language": "ur"})
    with SessionLocal() as db:
        assert db.get(ProcessingJob, job_id).status == "completed"
    synth.fail_after = None
    with SessionLocal() as db:
        from app.services.jobs import prepare_retry_job

        db.get(ProcessingJob, job_id).status = "failed"
        db.commit()
        retry, video_id, options = prepare_retry_job(db, job_id)
    synth.calls.clear()
    dub.run_dub_job(retry.id, video_id, options)
    assert len(synth.calls) == 4  # the 2 lines finished before the failure are not redone


def test_cancel_batch_job(client, synth):
    ids = seed(client)
    with SessionLocal() as db:
        job = dub.create_dub_job(db, db.get(Video, ids["video_id"]), {"language": "ur"})
        job_id = job.id
    assert client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 200
    dub.run_dub_job(job_id, ids["video_id"], {"language": "ur"})
    with SessionLocal() as db:
        assert db.get(ProcessingJob, job_id).status == "cancelled"
    assert synth.calls == []


def test_voice_preview_endpoint(client, synth):
    ids = seed(client)
    response = client.post(
        f"/api/v1/videos/{ids['video_id']}/dub-audio/voices/{ids['characters']['Sara']}/preview", json={"language": "hi"}
    )
    assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
    assert wav_info(response.content)[0] == 24000


# ---------------------------------------------------------------------------
# Phase 9 profile integration (fake Phase 9 engine; real Phase 10 pipeline)
# ---------------------------------------------------------------------------

phase9 = pytest.mark.skipif(not hasattr(models, "CharacterVoiceProfile"), reason="Phase 9 voice profiles not present")


class FakePhase9:
    ENGINES = {"piper": {"label": "Piper", "license": "voice-specific"}, "remote": {"label": "Worker", "license": "worker"}}

    def __init__(self, caps):
        self.caps = caps

    def capabilities(self, engine, voice_id=None):
        return self.caps.get(engine, set())

    def preview_available(self, engine, voice_id, language):
        return engine in self.caps

    def find_voice(self, engine, voice_id, language):
        return {"id": voice_id, "gender": "male"}

    def load(self, engine, voice_id, language):
        raise AssertionError("synthesize_raw is faked in tests")


def add_profile(ids, name, language, **fields):
    with SessionLocal() as db:
        profile = models.CharacterVoiceProfile(
            project_id=ids["project_id"], character_id=ids["characters"][name], language=language, version=1,
            active=True, **fields,
        )
        db.add(profile)
        db.commit()
        return profile.id


@phase9
def test_phase9_profile_drives_voice(client, synth, monkeypatch):
    monkeypatch.setattr(tts, "phase9_engine", lambda: FakePhase9({"piper": {"voice", "language", "speed"}}))
    ids = seed(client)
    add_profile(ids, "Sara", "ur", engine="piper", voice_id="ur_PK-aegis_female-medium",
                engine_params_json={"seed": 99}, characteristics_json={"gender": "female", "pitch": "high", "tone": "deep", "speed": 1.2})
    take = generate(client, ids, 1, "ur").json()["current_take"]
    voice = take["voice"]
    assert voice["source"] == "phase9_profile" and voice["synth"] == "phase9" and voice["engine"] == "piper"
    assert voice["voice_id"] == "ur_PK-aegis_female-medium" and voice["seed"] == 99
    assert voice["pitch_semitones"] == tts.PITCH_SEMITONES["high"] and voice["tone"] == "deep"
    assert voice["params"]["length_scale"] == round(1 / 1.2, 3)  # Piper speed is native
    assert "pitch high" in voice["shaped"] and "tone deep" in voice["shaped"]
    assert synth.calls[-1]["synth"] == "phase9"
    # Ali has no profile → still the built-in default; the two never share settings.
    ali = generate(client, ids, 0, "ur").json()["current_take"]["voice"]
    assert ali["source"] == "default" and ali["fingerprint"] != voice["fingerprint"]


@phase9
def test_phase9_unrunnable_engine_falls_back_visibly(client, synth, monkeypatch):
    monkeypatch.setattr(tts, "phase9_engine", lambda: FakePhase9({}))
    ids = seed(client)
    add_profile(ids, "Ali", "hi", engine="coqui", voice_id="xtts", characteristics_json={"pitch": "low", "tone": "heavy"})
    take = generate(client, ids, 0, "hi").json()["current_take"]
    assert take["voice"]["source"] == "phase9_profile_fallback" and take["voice"]["synth"] == "builtin"
    assert take["voice"]["pitch_semitones"] == -3.0 and take["voice"]["tone"] == "heavy"
    assert any(flag["code"] == "engine_fallback" for flag in take["qc_flags"])


@phase9
def test_phase9_emotion_and_consented_reference_only(client, synth, monkeypatch):
    monkeypatch.setattr(
        tts, "phase9_engine", lambda: FakePhase9({"remote": {"voice", "language", "reference", "emotion"}})
    )
    ids = seed(client)
    with SessionLocal() as db:
        reference = models.VoiceReference(
            project_id=ids["project_id"], character_id=ids["characters"]["Sara"], language="ur",
            storage_key=f"projects/{ids['project_id']}/refs/sara.wav", filename="sara.wav", mime_type="audio/wav",
            size_bytes=4, consent_status="granted",
        )
        db.add(reference)
        db.commit()
        reference_id = reference.id
    storage.write_bytes(f"projects/{ids['project_id']}/refs/sara.wav", b"RIFF")
    add_profile(ids, "Sara", "ur", engine="remote", voice_id="worker-voice", reference_id=reference_id,
                consent_status="granted", characteristics_json={"gender": "female"})
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/dub-audio?language=ur").json()
    sara = next(item for item in bundle["characters"] if item["name"] == "Sara")
    assert sara["voice"]["emotions"] == ["neutral", "happy", "sad", "angry", "calm", "serious"]
    assert "whispering" not in sara["voice"]["emotions"] and "shouting" not in sara["voice"]["emotions"]
    take = generate(client, ids, 1, "ur", emotion="sad").json()["current_take"]
    assert take["emotion"] == "sad"
    assert synth.calls[-1]["reference"] == b"RIFF"
    assert generate(client, ids, 1, "ur", emotion="whispering").status_code == 422

    # Revoking consent stops the sample from ever being sent.
    with SessionLocal() as db:
        db.get(models.VoiceReference, reference_id).consent_status = "revoked"
        db.commit()
    generate(client, ids, 1, "ur")
    assert synth.calls[-1]["reference"] is None


# ---------------------------------------------------------------------------
# Real model (opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.environ.get("RUN_REAL_TTS") != "1", reason="Set RUN_REAL_TTS=1 to synthesise with MMS-TTS")
def test_real_mms_urdu_and_hindi(client, monkeypatch):
    cache = os.environ.get("DUB_TTS_MODEL_CACHE_DIR")
    if cache:
        from pathlib import Path

        monkeypatch.setattr(tts.tts_settings, "model_cache_dir", Path(cache))
    monkeypatch.setattr(tts, "phase9_engine", lambda: None)
    ids = seed(client)
    for language in ("ur", "hi"):
        job = wait_job(client, client.post(f"/api/v1/videos/{ids['video_id']}/dub-audio/generate", json={"language": language}).json()["id"], timeout=600)
        assert job["status"] == "completed", job
        bundle = client.get(f"/api/v1/videos/{ids['video_id']}/dub-audio?language={language}").json()
        assert bundle["summary"].get("ready") == len(LINES), bundle["summary"]
        for line in bundle["lines"]:
            take = line["current_take"]
            assert take["stats"]["validated"] and take["stats"]["rms_dbfs"] > -30 and take["stats"]["clipped_ratio"] == 0
            assert 500 < take["duration_ms"] < 8000


def test_unknown_gender_characters_get_distinct_default_voices(client, synth):
    lines = [(f"Speaker {i}", f"Line {i}.", f"جملہ {i}۔", f"वाक्य {i}।", i * 3000) for i in range(4)]
    ids = seed(client, lines, genders={})
    bundle = client.get(f"/api/v1/videos/{ids['video_id']}/dub-audio?language=ur").json()
    fingerprints = {item["voice"]["fingerprint"] for item in bundle["characters"]}
    pitches = {item["voice"]["pitch_semitones"] for item in bundle["characters"]}
    assert len(fingerprints) == 4 and len(pitches) == 4
    assert all(item["voice"]["pitch_semitones"] <= 1.5 for item in bundle["characters"])  # male-sounding presets


def test_system_info_reports_voice_generation(client, synth):
    body = client.get("/api/v1/info").json()
    assert body["capabilities"]["voice_generation"] is True
    assert body["voice"]["generation"]["builtin"]["license"].startswith("CC-BY-NC")
