"""Phase 11 — dialogue timing & synchronisation.

Takes are synthetic speech-like signals (voiced bursts with pauses) written
straight into Phase 10's take table, so fitting, rubberband time-stretching,
track rendering, waveforms, jobs and the API all run for real without a TTS
model. Real Urdu/Hindi naturalness is measured separately (see report).
"""

import io
import time
import wave

import numpy as np
import pytest
from sqlalchemy import select

import app.services.dub_audio as dub_audio
import app.services.dub_timing as timing
from app.database import SessionLocal
from app.models import Character, CharacterAssignment, MediaAsset, ProcessingJob, SegmentTranslation, Transcript, TranscriptSegment, Video
from app.models_timing import DubTimingPlacement
from app.models_tts import DubAudioTake
from app.services import timing_engine as te
from app.services.storage import storage
from app.services.tts_engine import to_wav_bytes

SR = 24000


def speech(bursts_ms: list[int], pauses_ms: list[int] | None = None, lead_ms: int = 80, tail_ms: int = 80, f0: float = 140) -> np.ndarray:
    """Voiced bursts separated by pauses, with edge silence (like a TTS take)."""
    pauses_ms = pauses_ms or [150] * (len(bursts_ms) - 1)
    parts = [np.zeros(int(SR * lead_ms / 1000), dtype=np.float32)]
    rng = np.random.default_rng(int(f0))
    for i, burst in enumerate(bursts_ms):
        t = np.arange(int(SR * burst / 1000)) / SR
        env = np.clip(np.sin(np.pi * t / (burst / 1000)) * 4, 0, 1)
        tone = 0.3 * np.sin(2 * np.pi * f0 * t) + 0.12 * np.sin(2 * np.pi * 2 * f0 * t) + 0.01 * rng.standard_normal(t.size)
        parts.append((tone * env).astype(np.float32))
        if i < len(bursts_ms) - 1:
            parts.append(np.zeros(int(SR * pauses_ms[i] / 1000), dtype=np.float32))
    parts.append(np.zeros(int(SR * tail_ms / 1000), dtype=np.float32))
    return np.concatenate(parts)


# ---------------------------------------------------------------------------
# Engine unit tests
# ---------------------------------------------------------------------------


def test_short_line_fits_naturally_without_slowing_down():
    audio = speech([400, 300])
    result = te.fit(audio, SR, te.Window(5000, 8000, 0, 0))
    assert result.status == "fitted" and result.strategy == "natural" and result.tempo == 1.0
    assert result.placed_start_ms == 5000 and result.fitted_duration_ms < 3000  # never stretched to fill
    assert result.speech_duration_ms < result.take_duration_ms  # edge silence trimmed


def test_long_pauses_are_shortened_but_kept_natural():
    audio = speech([700, 700, 700], [900, 900])
    natural = len(te.trim_edges(audio, SR)) * 1000 // SR
    result = te.fit(audio, SR, te.Window(0, natural - 900, 0, 0))
    assert result.strategy == "pauses" and result.tempo == 1.0
    assert result.pause_saved_ms >= 900
    mask, frame = te.voiced_mask(result.audio, SR)
    runs, current, seen_voice = [], 0, False
    for voiced in mask:
        if not voiced:
            current += 1
        else:
            if current and seen_voice:  # internal pauses only, not edge margins
                runs.append(current * frame * 1000 // SR)
            current, seen_voice = 0, True
    assert len(runs) == 2 and min(runs) >= te.timing_settings.min_pause_ms - 20  # every pause survives
    assert result.voiced_ratio >= 0.95


def test_silence_after_line_is_used_before_speeding_up():
    audio = speech([1500])
    result = te.fit(audio, SR, te.Window(0, 1300, 0, 600))
    assert result.strategy == "tail_gap" and result.tempo == 1.0 and result.overflow_ms == 0


def test_gentle_then_fast_then_needs_attention():
    audio = speech([900, 900], [150])
    speech_ms = len(te.trim_edges(audio, SR)) * 1000 // SR
    gentle = te.fit(audio, SR, te.Window(0, int(speech_ms / 1.08), 0, 0))
    assert gentle.status == "fitted" and gentle.strategy == "gentle_tempo" and 1.0 < gentle.tempo <= 1.12
    fast = te.fit(audio, SR, te.Window(0, int(speech_ms / 1.2), 0, 0))
    assert fast.status == "fitted_fast" and 1.12 < fast.tempo <= 1.25
    assert any(flag["code"] == "fast_speech" for flag in fast.flags)
    impossible = te.fit(audio, SR, te.Window(0, int(speech_ms / 1.6), 0, 0))
    assert impossible.status == "needs_attention" and impossible.tempo == 1.25
    assert impossible.overflow_ms > 0
    # Nothing is cut to force the match: all speech is still there at ×1.25.
    assert impossible.fitted_duration_ms >= int(speech_ms / 1.25) - 30
    assert impossible.voiced_ratio >= 0.9
    tips = {item["action"] for item in te.suggestions(impossible, te.Window(0, int(speech_ms / 1.6), 0, 0), 30)}
    assert tips >= {"shorten_translation", "regenerate_faster", "manual_timing", "allow_overlap"}


def test_pitch_is_preserved_when_speeding_up():
    audio = speech([1200, 1200], [150], f0=150)
    result = te.fit(audio, SR, te.Window(0, 2300, 0, 0))
    assert result.tempo > 1.05
    spectrum = np.abs(np.fft.rfft(result.audio))
    freqs = np.fft.rfftfreq(result.audio.size, 1 / SR)
    peak = freqs[np.argmax(spectrum[(freqs > 80) & (freqs < 400)]) + np.argmax(freqs > 80)]
    assert abs(peak - 150) < 8


def test_single_word_is_not_sped_up_past_natural_limit():
    audio = speech([350])
    result = te.fit(audio, SR, te.Window(0, 300, 0, 0))
    assert result.tempo <= te.timing_settings.natural_tempo
    assert result.status in {"fitted", "needs_attention"}


def test_silent_take_is_rejected():
    with pytest.raises(te.TimingError):
        te.fit(np.zeros(SR, dtype=np.float32), SR, te.Window(0, 1000, 0, 0))


# ---------------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------------


def seed(client, lines, languages=("ur", "hi"), duration_ms=40000, with_file=False, source_audio=False):
    """lines: (character, start_ms, end_ms, bursts) — bursts=None means no take."""
    project = client.post("/api/v1/projects", json={"name": "Phase 11 film", "target_languages": ["ur", "hi"]}).json()
    with SessionLocal() as db:
        key = f"projects/{project['id']}/source.mp4"
        if with_file:
            storage.write_bytes(key, b"\x00" * 64)
        video = Video(project_id=project["id"], original_filename="film.mp4", storage_key=key, content_type="video/mp4",
                      size_bytes=64, duration_ms=duration_ms)
        db.add(video)
        db.flush()
        if source_audio:
            original = np.zeros(int(16000 * duration_ms / 1000), dtype=np.float32)
            for _name, start, end, _b in lines:
                t = np.arange(int(16000 * (end - start) / 1000)) / 16000
                original[int(16 * start): int(16 * start) + t.size] = 0.4 * np.sin(2 * np.pi * 180 * t)
            akey = f"projects/{project['id']}/source_audio.wav"
            size = storage.write_bytes(akey, to_wav_bytes(original, 16000))
            db.add(MediaAsset(project_id=project["id"], video_id=video.id, kind="source_audio", version=1, storage_key=akey,
                              mime_type="audio/wav", size_bytes=size))
        transcript = Transcript(project_id=project["id"], video_id=video.id, language="en", status="completed")
        db.add(transcript)
        db.flush()
        chars, segs = {}, []
        for position, (name, start, end, bursts) in enumerate(lines):
            segment = TranscriptSegment(transcript_id=transcript.id, position=position, start_ms=start, end_ms=end,
                                        text=f"line {position}", words=[])
            db.add(segment)
            db.flush()
            segs.append(segment.id)
            if name not in chars:
                chars[name] = Character(project_id=project["id"], name=name, status="confirmed")
                db.add(chars[name])
                db.flush()
            db.add(CharacterAssignment(character_id=chars[name].id, segment_id=segment.id, video_id=video.id))
            for language in languages:
                db.add(SegmentTranslation(project_id=project["id"], video_id=video.id, segment_id=segment.id, language=language,
                                          text="لفظ لفظ" if language == "ur" else "शब्द शब्द", source_text=segment.text))
                if bursts is not None:
                    add_take(db, video, segment.id, chars[name].id, language, bursts)
        db.commit()
        return {"project_id": project["id"], "video_id": video.id, "segments": segs, "characters": {k: v.id for k, v in chars.items()}}


def add_take(db, video, segment_id, character_id, language, bursts, number=None):
    audio = speech(bursts, f0=130 if language == "ur" else 170)
    existing = db.scalars(select(DubAudioTake).where(DubAudioTake.segment_id == segment_id, DubAudioTake.language == language)).all()
    for take in existing:
        take.is_current = False
    take = DubAudioTake(project_id=video.project_id, video_id=video.id, segment_id=segment_id, character_id=character_id,
                        language=language, take_number=number or len(existing) + 1, status="ready", is_current=True,
                        text="لفظ لفظ" if language == "ur" else "शब्द शब्द", duration_ms=len(audio) * 1000 // SR,
                        sample_rate=SR, voice_json={}, stats_json={}, qc_flags=[])
    db.add(take)
    db.flush()
    take.storage_key = f"projects/{video.project_id}/dub/{language}/{take.id}.wav"
    storage.write_bytes(take.storage_key, to_wav_bytes(audio, SR))
    return take


def wait_job(client, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def fit_all(client, ids, language="ur", **payload):
    response = client.post(f"/api/v1/videos/{ids['video_id']}/dub-timing/fit", json={"language": language, **payload})
    assert response.status_code == 202, response.text
    return wait_job(client, response.json()["id"])


def bundle(client, ids, language="ur"):
    return client.get(f"/api/v1/videos/{ids['video_id']}/dub-timing?language={language}").json()


SCENE = [
    ("Ali", 1000, 3000, [600, 500]),          # short: fits naturally
    ("Sara", 4000, 6000, [900, 900, 600]),    # long: needs pauses/tail/tempo
    ("Ali", 6600, 7400, [1400, 1300]),        # far too long for 0.8 s and next line soon → needs attention
    ("Sara", 7600, 9000, [500]),
    ("Ali", 12000, 15000, [700, 600, 700]),   # fast/slow speech variety
]


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def test_timing_analysis_fields(client):
    ids = seed(client, SCENE)
    data = bundle(client, ids)
    line = data["lines"][1]
    assert (line["start_ms"], line["end_ms"], line["duration_ms"]) == (4000, 6000, 2000)
    assert line["take"]["duration_ms"] > 2000 and line["difference_ms"] == line["take"]["duration_ms"] - 2000
    assert line["character_name"] == "Sara" and line["status"] == "pending"
    assert data["limits"]["max_tempo"] == 1.25


@pytest.mark.parametrize("language", ["ur", "hi"])
def test_batch_fit_urdu_and_hindi(client, language):
    ids = seed(client, SCENE)
    job = fit_all(client, ids, language)
    assert job["status"] == "completed", job
    data = bundle(client, ids, language)
    statuses = [line["status"] for line in data["lines"]]
    assert statuses[0] == "fitted" and data["lines"][0]["placement"]["strategy"] == "natural"
    assert statuses[2] == "needs_attention"
    p2 = data["lines"][2]["placement"]
    assert p2["overflow_ms"] > 0 and any(s["action"] == "shorten_translation" for s in p2["suggestions"])
    assert any(flag["code"] == "manual_correction" for flag in p2["flags"])
    for line in data["lines"]:
        placement = line["placement"]
        assert placement["tempo"] <= 1.25
        assert placement["placed_start_ms"] >= line["start_ms"] - te.timing_settings.max_lead_ms
        assert placement["details"]["voiced_ratio"] >= 0.85
    # Conversation order is kept.
    starts = [line["placement"]["placed_start_ms"] for line in data["lines"]]
    assert starts == sorted(starts)
    # Track rendered at the film length with dialogue where it was placed.
    assert data["track"]["exists"] and not data["track"]["stale"]
    audio = client.get(data["track"]["url"])
    with wave.open(io.BytesIO(audio.content)) as handle:
        sr = handle.getframerate()
        samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").astype(np.float32) / 32768
    assert abs(len(samples) / sr * 1000 - 40500) < 50
    first = data["lines"][0]["placement"]
    inside = samples[int(sr * first["placed_start_ms"] / 1000): int(sr * first["placed_end_ms"] / 1000)]
    gap = samples[int(sr * 10.0): int(sr * 11.5)]
    assert np.abs(inside).max() > 0.1 and np.abs(gap).max() < 1e-3
    assert np.abs(samples).max() <= 0.999


def test_lines_never_spill_into_next_speakers_line(client):
    ids = seed(client, SCENE)
    fit_all(client, ids)
    data = bundle(client, ids)
    long_line, next_line = data["lines"][2], data["lines"][3]
    # Window extension stops before Sara's next line (guard), leftovers are reported as overflow.
    assert long_line["placement"]["details"]["tail_room_ms"] <= next_line["start_ms"] - long_line["end_ms"]
    assert any(flag["code"] == "speaker_overlap" for flag in long_line["placement"]["flags"]) or long_line["placement"]["overflow_ms"] > 0


def test_overlapping_original_dialogue(client):
    lines = [
        ("Ali", 1000, 4000, [800, 800]),
        ("Sara", 3000, 5000, [700, 600]),   # starts while Ali still speaks
        ("Sara", 4500, 5500, [400]),        # same speaker overlap (transcript issue)
        ("Zainab", 8000, 9000, [500]),
    ]
    ids = seed(client, lines)
    data = bundle(client, ids)
    assert {item["segment_id"] for item in data["lines"][0]["overlaps"]} == {ids["segments"][1]}
    fit_all(client, ids)
    data = bundle(client, ids)
    ali, sara, sara2 = (data["lines"][i] for i in range(3))
    assert ali["placement"]["details"]["tail_room_ms"] == 0  # cannot extend over Sara
    assert sara["placement"]["details"]["lead_room_ms"] == 0
    assert sara["placement"]["placed_start_ms"] == 3000  # each speaker stays in their own window
    assert any(flag["code"] == "original_overlap" for flag in ali["placement"]["flags"])
    assert any(flag["code"] == "same_speaker_overlap" for flag in sara2["placement"]["flags"])
    assert not any(flag["code"] == "order_changed" for line in data["lines"] for flag in line["placement"]["flags"])
    assert ali["character_name"] == "Ali" and sara["character_name"] == "Sara"


def test_manual_timing_saved_and_preserved(client):
    ids = seed(client, SCENE)
    fit_all(client, ids)
    seg = ids["segments"][2]
    moved = client.patch(f"/api/v1/videos/{ids['video_id']}/dub-timing/segments/{seg}",
                         json={"language": "ur", "start_ms": 6300, "end_ms": 7550, "allow_overlap_ms": 300})
    assert moved.status_code == 200, moved.text
    placement = moved.json()["placement"]
    assert placement["manual"] and placement["window_start_ms"] == 6300 and placement["window_end_ms"] == 7550
    assert placement["placed_start_ms"] <= 6300
    approved = client.patch(f"/api/v1/videos/{ids['video_id']}/dub-timing/segments/{seg}", json={"language": "ur", "approved": True})
    assert approved.json()["placement"]["approved"] is True
    # Approved timing is locked against moves and refits...
    assert client.patch(f"/api/v1/videos/{ids['video_id']}/dub-timing/segments/{seg}", json={"language": "ur", "start_ms": 6000}).status_code == 409
    assert client.post(f"/api/v1/videos/{ids['video_id']}/dub-timing/segments/{seg}/fit", json={"language": "ur"}).status_code == 409
    # ...and batch runs, even "all", skip it unless overwrite is confirmed.
    job = fit_all(client, ids, mode="all")
    assert "1 approved" in next(step for step in job["steps"] if step["name"] == "select_lines")["detail"]
    after = bundle(client, ids)["lines"][2]["placement"]
    assert after["manual"] and after["approved"] and after["window_start_ms"] == 6300
    fit_all(client, ids, mode="all", overwrite_manual=True)
    reset = bundle(client, ids)["lines"][2]["placement"]
    assert not reset["manual"] and not reset["approved"] and reset["window_start_ms"] == 6600


def test_invalid_manual_timing_rejected(client):
    ids = seed(client, SCENE)
    seg = ids["segments"][0]
    bad = client.patch(f"/api/v1/videos/{ids['video_id']}/dub-timing/segments/{seg}", json={"language": "ur", "start_ms": 5000, "end_ms": 4000})
    assert bad.status_code == 422


def test_new_take_marks_timing_stale_and_refits(client):
    ids = seed(client, SCENE)
    fit_all(client, ids)
    with SessionLocal() as db:
        video = db.get(Video, ids["video_id"])
        add_take(db, video, ids["segments"][0], ids["characters"]["Ali"], "ur", [300])
        db.commit()
    assert bundle(client, ids)["lines"][0]["status"] == "stale"
    assert bundle(client, ids)["track"]["stale"] is False  # track reflects placements until refit
    job = fit_all(client, ids)
    assert job["status"] == "completed"
    data = bundle(client, ids)
    assert data["lines"][0]["status"] == "fitted" and "1 to fit" in job["steps"][0]["detail"]


def test_regenerate_faster_uses_phase10_then_refits(client, monkeypatch):
    ids = seed(client, SCENE)
    fit_all(client, ids)
    calls = {}

    def fake_generate(db, video, segment, language, overrides=None, **kwargs):
        calls["overrides"] = overrides
        return add_take(db, video, segment.id, ids["characters"]["Ali"], language, [320, 260])

    monkeypatch.setattr(dub_audio, "generate_take", fake_generate)
    response = client.post(f"/api/v1/videos/{ids['video_id']}/dub-timing/segments/{ids['segments'][2]}/regenerate-faster",
                           json={"language": "ur", "speed_factor": 1.15})
    assert response.status_code == 200, response.text
    assert calls["overrides"] == {"speed_factor": 1.15}
    assert response.json()["status"] in {"fitted", "fitted_fast"}


def test_waveforms_for_original_and_dub(client):
    ids = seed(client, SCENE, source_audio=True)
    fit_all(client, ids)
    original = client.get(f"/api/v1/videos/{ids['video_id']}/dub-timing/waveform?source=original&start_ms=0&end_ms=10000&buckets=100").json()
    assert original["kind"] == "source_audio" and len(original["peaks"]) == 100
    loud = [max(abs(a), abs(b)) for a, b in original["peaks"]]
    assert max(loud[10:30]) > 0.5 and max(loud[31:39]) < 0.05  # speech at 1–3 s, silence 3.1–3.9 s
    dub = client.get(f"/api/v1/videos/{ids['video_id']}/dub-timing/waveform?source=hi&start_ms=0&end_ms=10000&buckets=100").json()
    assert len(dub["peaks"]) == 100 and max(max(abs(a), abs(b)) for a, b in dub["peaks"]) == 0  # Hindi not fitted yet
    fit_all(client, ids, "hi")
    dub = client.get(f"/api/v1/videos/{ids['video_id']}/dub-timing/waveform?source=hi&start_ms=0&end_ms=10000&buckets=100").json()
    assert max(max(abs(a), abs(b)) for a, b in dub["peaks"][10:30]) > 0.1


def test_lines_without_audio(client):
    ids = seed(client, [("Ali", 1000, 2000, None), ("Sara", 3000, 4000, None)])
    data = bundle(client, ids)
    assert {line["status"] for line in data["lines"]} == {"no_audio"}
    response = client.post(f"/api/v1/videos/{ids['video_id']}/dub-timing/fit", json={"language": "ur"})
    assert response.status_code == 409 and "Phase 10" in response.json()["error"]["message"]


def test_failed_job_retry_resumes_and_preserves_done_lines(client, monkeypatch):
    ids = seed(client, SCENE, with_file=True)
    original = timing.render_track
    monkeypatch.setattr(timing, "render_track", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    job = fit_all(client, ids)
    assert job["status"] == "failed" and "disk full" in job["error_message"]
    monkeypatch.setattr(timing, "render_track", original)
    calls = []
    real_fit = timing.fit_line
    monkeypatch.setattr(timing, "fit_line", lambda *a, **k: calls.append(a[3]) or real_fit(*a, **k))
    retry = client.post(f"/api/v1/jobs/{job['id']}/retry")
    assert retry.status_code == 202, retry.text
    retried = wait_job(client, retry.json()["id"])
    assert retried["status"] == "completed"
    assert calls == []  # every line was fitted before the failure, none redone
    assert bundle(client, ids)["track"]["exists"]


def test_cancel_timing_job(client):
    ids = seed(client, SCENE)
    with SessionLocal() as db:
        job = timing.create_timing_job(db, db.get(Video, ids["video_id"]), {"language": "ur"})
        job_id = job.id
    assert client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 200
    timing.run_timing_job(job_id, ids["video_id"], {"language": "ur"})
    with SessionLocal() as db:
        assert db.get(ProcessingJob, job_id).status == "cancelled"
        assert db.scalars(select(DubTimingPlacement).where(DubTimingPlacement.video_id == ids["video_id"])).all() == []


def test_fitted_clip_download_and_capabilities(client):
    ids = seed(client, SCENE)
    fit_all(client, ids)
    placement = bundle(client, ids)["lines"][1]["placement"]
    audio = client.get(placement["audio_url"])
    assert audio.status_code == 200 and audio.headers["x-synthetic-audio"] == "true"
    with wave.open(io.BytesIO(audio.content)) as handle:
        assert handle.getframerate() == 24000
        assert abs(handle.getnframes() * 1000 / 24000 - placement["fitted_duration_ms"]) < 5
    info = client.get("/api/v1/info").json()
    assert info["capabilities"]["dialogue_timing"] is True and info["capabilities"]["lip_sync"] is False
