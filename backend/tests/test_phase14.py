"""Phase 14 - professional timeline editor + external audio import."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import wave

import numpy as np
import pytest

from app.database import SessionLocal
from app.models import Character, CharacterVoiceProfile, Project, Video
from app.models_lipsync import LipSyncResult
from app.models_timeline import ExternalAudio, TimelineClip, TimelineProject
from app.services import timeline as tl
from app.services.media import ffmpeg_binary
from app.services.storage import storage

SR = 24000


def _wav_bytes(audio: np.ndarray, sr: int = SR) -> bytes:
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(pcm.tobytes())
    return buf.getvalue()


def _tone(seconds: float, freq: float = 440.0, sr: int = SR, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(sr * seconds)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# ---------------------------------------------------------------------------
# Fixtures: a project + a real tiny source video + stored files
# ---------------------------------------------------------------------------


def _ffmpeg() -> str:
    binary = ffmpeg_binary()
    assert binary
    return binary


@pytest.fixture
def env(client):
    """client fixture gives a fresh DB; return a session factory."""
    return client


def _make_project(db, pid="proj1") -> Project:
    project = Project(id=pid, name="Phase14", target_languages=["ur", "hi"], status="draft")
    db.add(project)
    db.flush()
    return project


def _make_video(db, project, tmp_path, vid="vid1") -> Video:
    """A real 2-second silent video + source audio, stored under artifacts."""
    src = tmp_path / "src.mp4"
    subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=2",
         "-f", "lavfi", "-i", "sine=frequency=300:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", "-y", str(src)],
        check=True,
    )
    key = f"projects/{project.id}/source/{vid}.mp4"
    storage.write_bytes(key, src.read_bytes())
    video = Video(
        id=vid, project_id=project.id, original_filename="movie.mp4", storage_key=key, content_type="video/mp4",
        size_bytes=src.stat().st_size, duration_ms=2000, width=160, height=120, fps=24.0, probe_status="probed",
    )
    db.add(video)
    db.flush()
    return video


@pytest.fixture
def world(client, tmp_path):
    db = SessionLocal()
    project = _make_project(db)
    video = _make_video(db, project, tmp_path)
    timeline = tl.create_timeline(db, video)
    yield {"db": db, "project": project, "video": video, "timeline": timeline, "tmp": tmp_path}
    db.close()


def _track(timeline, kind):
    db = SessionLocal()
    try:
        return db.scalar(
            __import__("sqlalchemy").select(tl.TimelineTrack).where(tl.TimelineTrack.timeline_id == timeline.id, tl.TimelineTrack.kind == kind)
        )
    finally:
        db.close()


def _add_audio_clip(db, timeline, kind="external_audio", start_ms=1000, duration_ms=1000, tmp=None, segment_id=None, language=None, character_id=None):
    track = db.scalar(
        __import__("sqlalchemy").select(tl.TimelineTrack).where(tl.TimelineTrack.timeline_id == timeline.id, tl.TimelineTrack.kind == kind)
    )
    key = f"projects/{timeline.project_id}/external_audio/test/original.wav"
    storage.write_bytes(key, _wav_bytes(_tone(duration_ms / 1000, 440)))
    return tl.add_clip(
        db, timeline.id, track.id, source_type="external_audio", storage_key=key, name="clip", start_ms=start_ms,
        duration_ms=duration_ms, source_in_ms=0, source_out_ms=duration_ms, source_duration_ms=duration_ms,
        segment_id=segment_id, language=language, character_id=character_id,
    )


# ---------------------------------------------------------------------------
# Task 1: professional timeline
# ---------------------------------------------------------------------------


def test_default_tracks(world):
    out = tl.serialize_timeline(world["db"], world["timeline"])
    kinds = [t["kind"] for t in out["tracks"]]
    assert kinds == [
        "video", "original_english", "urdu_dub", "hindi_dub", "background_music", "sound_effects",
        "ambience", "external_audio", "lip_sync",
    ]
    # Original Video Track populated from the real source
    video_track = next(t for t in out["tracks"] if t["kind"] == "video")
    assert video_track["clips"], "video track should have a clip"
    assert video_track["clips"][0]["duration_ms"] == 2000
    assert video_track["color"] and video_track["name"]


def test_language_tracks_selectable(world):
    db = world["db"]
    tracks = {t["kind"]: t for t in tl.serialize_timeline(db, world["timeline"])["tracks"]}
    assert tracks["original_english"]["language"] == "en"
    assert tracks["urdu_dub"]["language"] == "ur"
    assert tracks["hindi_dub"]["language"] == "hi"
    tl.set_track_props(db, tracks["hindi_dub"]["id"], enabled=False)
    out = tl.serialize_timeline(db, world["timeline"])
    hi = next(t for t in out["tracks"] if t["kind"] == "hindi_dub")
    assert hi["enabled"] is False


# ---------------------------------------------------------------------------
# Task 2: timeline editing
# ---------------------------------------------------------------------------


def test_move_trim_split_join_delete_duplicate(world):
    db = world["db"]
    tlm = world["timeline"]
    clip = _add_audio_clip(db, tlm, start_ms=1000, duration_ms=1000)

    # move
    clip = tl.move_clip(db, clip.id, 500)
    assert clip.start_ms == 500

    # snap to grid
    clip = tl.move_clip(db, clip.id, 533, snap_ms=250)
    assert clip.start_ms % 250 == 0

    # trim start + end (non-destructive; source untouched)
    before_in = clip.source_in_ms
    clip = tl.trim_start(db, clip.id, 100)
    assert clip.source_in_ms == before_in + 100
    assert clip.duration_ms == 900 or clip.duration_ms < 1000
    clip = tl.trim_end(db, clip.id, 100)
    dur_after_trim = clip.duration_ms

    # split
    at = clip.start_ms + clip.duration_ms // 2
    left, right = tl.split_clip(db, clip.id, at)
    assert left.duration_ms + right.duration_ms == dur_after_trim
    assert right.start_ms == at
    assert right.source_in_ms > left.source_in_ms

    # join the two halves back
    joined = tl.join_clips(db, [left.id, right.id])
    assert joined.duration_ms == dur_after_trim

    # duplicate
    dup = tl.duplicate_clip(db, joined.id)
    assert dup.id != joined.id
    assert dup.start_ms == joined.start_ms + joined.duration_ms

    # delete
    tl.delete_clip(db, dup.id)
    ids = [c.id for c in tl._clips(db, tlm.id)]
    assert dup.id not in ids


def test_join_rejects_non_adjacent(world):
    db = world["db"]
    tlm = world["timeline"]
    a = _add_audio_clip(db, tlm, start_ms=0, duration_ms=500)
    b = _add_audio_clip(db, tlm, start_ms=800, duration_ms=500)
    with pytest.raises(tl.AppError):
        tl.join_clips(db, [a.id, b.id])


def test_volume_and_fades(world):
    db = world["db"]
    tlm = world["timeline"]
    clip = _add_audio_clip(db, tlm, start_ms=0, duration_ms=1000)
    clip = tl.set_clip_props(db, clip.id, volume_db=-6.0, fade_in_ms=200, fade_out_ms=300)
    assert clip.volume_db == -6.0
    assert clip.fade_in_ms == 200
    assert clip.fade_out_ms == 300
    # fades clamped to clip length
    clip = tl.set_clip_props(db, clip.id, fade_in_ms=99999)
    assert clip.fade_in_ms <= clip.duration_ms


def test_locked_clip_cannot_be_edited(world):
    db = world["db"]
    tlm = world["timeline"]
    clip = _add_audio_clip(db, tlm, start_ms=0, duration_ms=1000)
    tl.set_clip_props(db, clip.id, locked=True)
    with pytest.raises(tl.AppError):
        tl.move_clip(db, clip.id, 2000)


def test_undo_redo(world):
    db = world["db"]
    tlm = world["timeline"]
    before = len(tl.serialize_timeline(db, tlm)["tracks"][0]["clips"])
    clip = _add_audio_clip(db, tlm, kind="external_audio", start_ms=100, duration_ms=500)
    assert tl.serialize_timeline(db, tlm)["can_undo"]
    # undo the add
    out = tl.undo(db, tlm)
    ids = [c["id"] for t in out["tracks"] for c in t["clips"]]
    assert clip.id not in ids
    # redo
    out = tl.redo(db, tlm)
    ids = [c["id"] for t in out["tracks"] for c in t["clips"]]
    assert clip.id in ids
    assert out["can_undo"]


def test_undo_move_restores_position(world):
    db = world["db"]
    tlm = world["timeline"]
    clip = _add_audio_clip(db, tlm, start_ms=1000, duration_ms=500)
    orig_start = clip.start_ms
    tl.move_clip(db, clip.id, 3000)
    tl.undo(db, tlm)
    restored = db.get(TimelineClip, clip.id)
    assert restored.start_ms == orig_start


def test_history_recorded(world):
    db = world["db"]
    tlm = world["timeline"]
    _add_audio_clip(db, tlm, start_ms=0, duration_ms=500)
    hist = tl.history(db, tlm)
    assert len(hist) >= 2
    assert hist[0]["action"] == "create_timeline"


# ---------------------------------------------------------------------------
# Task 3: external audio import
# ---------------------------------------------------------------------------


def _make_audio_file(tmp_path, fmt, name):
    """Generate a real audio file in a given format with ffmpeg."""
    out = tmp_path / name
    result = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
         "-ac", "1", "-ar", "24000", "-y", str(out)],
        capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return out


@pytest.mark.parametrize(
    "fmt,ext",
    [("wav", "wav"), ("mp3", "mp3"), ("aac", "aac"), ("m4a", "m4a"), ("flac", "flac"), ("ogg", "ogg")],
)
def test_external_audio_formats(client, tmp_path, fmt, ext):
    from app.services.external_audio import import_external_audio

    db = SessionLocal()
    try:
        project = _make_project(db)
        src = _make_audio_file(tmp_path, fmt, f"in.{ext}")
        original_bytes = src.read_bytes()
        row = import_external_audio(
            db, project, filename=f"in.{ext}", content_type=None, source_path=src, size_bytes=len(original_bytes),
        )
        assert row.format in ("wav", "mp3", "aac", "m4a", "flac", "ogg")
        assert row.duration_ms and row.duration_ms > 100
        assert row.checksum_sha256
        # original user file unchanged
        assert src.read_bytes() == original_bytes
        # our stored copy unchanged
        stored = storage.absolute_path(row.storage_key)
        assert stored.read_bytes() == original_bytes
        # cached waveform overview
        assert row.peaks_key and storage.absolute_path(row.peaks_key).is_file()
        overview = json.loads(storage.absolute_path(row.peaks_key).read_text())
        assert overview["peaks"]
    finally:
        db.close()


def test_external_audio_rejects_non_audio(client, tmp_path):
    from app.services.external_audio import import_external_audio
    from app.services.media import AppError

    db = SessionLocal()
    try:
        project = _make_project(db)
        bad = tmp_path / "notaudio.wav"
        bad.write_bytes(b"this is definitely not audio at all" * 10)
        with pytest.raises(AppError):
            import_external_audio(db, project, filename="notaudio.wav", content_type=None, source_path=bad, size_bytes=bad.stat().st_size)
    finally:
        db.close()


def test_external_audio_rejects_unknown_extension(client, tmp_path):
    from app.services.external_audio import import_external_audio
    from app.services.media import AppError

    db = SessionLocal()
    try:
        project = _make_project(db)
        src = _make_audio_file(tmp_path, "wav", "in.wav")
        renamed = tmp_path / "in.txt"
        shutil.copy(src, renamed)
        with pytest.raises(AppError):
            import_external_audio(db, project, filename="in.txt", content_type=None, source_path=renamed, size_bytes=renamed.stat().st_size)
    finally:
        db.close()


def test_external_audio_clip_on_timeline_and_original_unchanged(world, tmp_path):
    db = world["db"]
    tlm = world["timeline"]
    src = _make_audio_file(tmp_path, "wav", "ext.wav")
    original_bytes = src.read_bytes()
    from app.services.external_audio import import_external_audio

    row = import_external_audio(db, world["project"], filename="ext.wav", content_type="audio/wav", source_path=src, size_bytes=len(original_bytes))
    stored = storage.absolute_path(row.storage_key)
    stored_before = stored.read_bytes()

    clip = tl.add_external_audio_clip(db, tlm.id, external_audio_id=row.id, start_ms=1234, volume_db=-3.0)
    assert clip.source_type == "external_audio"
    assert clip.start_ms == 1234
    assert clip.external_audio_id == row.id
    # trim + move non-destructive
    tl.trim_start(db, clip.id, 50)
    tl.move_clip(db, clip.id, 5000)
    # original imported file unchanged
    assert stored.read_bytes() == stored_before
    assert src.read_bytes() == original_bytes


def test_external_audio_preview_waveform(world, tmp_path):
    db = world["db"]
    src = _make_audio_file(tmp_path, "wav", "ext.wav")
    from app.services.external_audio import import_external_audio, import_waveform

    row = import_external_audio(db, world["project"], filename="ext.wav", content_type=None, source_path=src, size_bytes=src.stat().st_size)
    wave = import_waveform(db, row, 0, row.duration_ms or 500, 100)
    assert len(wave["peaks"]) == 100
    assert any(max(hi, -lo) > 0.01 for lo, hi in wave["peaks"])


def test_external_audio_any_position_and_trim(world, tmp_path):
    db = world["db"]
    tlm = world["timeline"]
    src = _make_audio_file(tmp_path, "wav", "ext.wav")
    from app.services.external_audio import import_external_audio

    row = import_external_audio(db, world["project"], filename="ext.wav", content_type=None, source_path=src, size_bytes=src.stat().st_size)
    dur = row.duration_ms or 500
    clip = tl.add_external_audio_clip(db, tlm.id, external_audio_id=row.id, start_ms=9999, source_in_ms=0, source_out_ms=dur)
    assert clip.start_ms == 9999
    clip = tl.trim_clip(db, clip.id, source_in_ms=10, source_out_ms=dur - 10, duration_ms=dur - 20)
    assert clip.source_in_ms == 10


def test_replace_generated_dialogue_with_external(world, tmp_path):
    db = world["db"]
    tlm = world["timeline"]
    # a generated dubbed clip on the urdu track
    track = db.scalar(__import__("sqlalchemy").select(tl.TimelineTrack).where(tl.TimelineTrack.timeline_id == tlm.id, tl.TimelineTrack.kind == "urdu_dub"))
    gen = tl.add_clip(db, tlm.id, track.id, source_type="dub_take", storage_key="projects/x/dubfit/ur/y.wav", name="AI line",
                      start_ms=1000, duration_ms=800, source_in_ms=0, source_out_ms=800, source_duration_ms=800, language="ur")
    src = _make_audio_file(tmp_path, "wav", "ext.wav")
    from app.services.external_audio import import_external_audio

    row = import_external_audio(db, world["project"], filename="ext.wav", content_type=None, source_path=src, size_bytes=src.stat().st_size)
    ext_clip = tl.add_external_audio_clip(db, tlm.id, external_audio_id=row.id, start_ms=1000, replace_clip_id=gen.id, snap_ms=0)
    # external placed at replaced clip position; replaced clip muted + linked; source intact
    assert ext_clip.start_ms == 1000
    assert ext_clip.replaces_clip_id == gen.id
    regenerated = db.get(TimelineClip, gen.id)
    assert regenerated.muted is True
    assert regenerated.metadata_json.get("replaced_by_clip_id") == ext_clip.id
    # original generated source file reference still present
    assert regenerated.storage_key == "projects/x/dubfit/ur/y.wav"


# ---------------------------------------------------------------------------
# Task 4: character assignment (Phase 7 / Phase 9)
# ---------------------------------------------------------------------------


def test_character_assignment_and_voice_profile(world):
    db = world["db"]
    tlm = world["timeline"]
    char = Character(id="char1", project_id=tlm.project_id, name="Hero", status="confirmed")
    db.add(char)
    db.flush()
    profile = CharacterVoiceProfile(
        id="vp1", project_id=tlm.project_id, character_id="char1", language="ur", version=1, active=True,
        engine="piper", voice_id="voice-1", model_id="m", engine_params_json={}, characteristics_json={"gender": "male"},
        consent_status="granted",
    )
    db.add(profile)
    db.flush()

    clip = _add_audio_clip(db, tlm, kind="urdu_dub", start_ms=0, duration_ms=500, language="ur")
    clip = tl.reassign_character(db, clip.id, "char1")
    assert clip.character_id == "char1"
    assert clip.character_name == "Hero"
    # voice profile info preserved on the clip
    vp = clip.metadata_json.get("voice_profile")
    assert vp and vp.get("ur", {}).get("voice_id") == "voice-1"
    # multiple clips for same character
    clip2 = _add_audio_clip(db, tlm, kind="urdu_dub", start_ms=1000, duration_ms=500, language="ur")
    clip2 = tl.reassign_character(db, clip2.id, "char1")
    # reassign to another character
    char2 = Character(id="char2", project_id=tlm.project_id, name="Villain", status="confirmed")
    db.add(char2)
    db.flush()
    clip2 = tl.reassign_character(db, clip2.id, "char2")
    assert clip2.character_id == "char2"
    assert clip2.character_name == "Villain"
    # character identity remains in Phase 7 tables (not overwritten)
    assert db.get(Character, "char1").name == "Hero"


def test_reassign_unknown_character_rejected(world):
    db = world["db"]
    tlm = world["timeline"]
    clip = _add_audio_clip(db, tlm, start_ms=0, duration_ms=500)
    with pytest.raises(tl.AppError):
        tl.reassign_character(db, clip.id, "nope")


def test_assign_to_dialogue_segment(world):
    db = world["db"]
    tlm = world["timeline"]
    # build a transcript segment
    from app.models import Transcript

    tr = Transcript(id="tr1", project_id=tlm.project_id, video_id=tlm.video_id, version=1, status="done", language="en")
    db.add(tr)
    db.flush()
    seg = __import__("app.models", fromlist=["TranscriptSegment"]).TranscriptSegment(
        id="seg1", transcript_id="tr1", position=0, start_ms=0, end_ms=1000, text="Hello",
    )
    db.add(seg)
    db.flush()
    clip = _add_audio_clip(db, tlm, kind="urdu_dub", start_ms=0, duration_ms=500, language="ur")
    clip = tl.assign_segment(db, clip.id, "seg1")
    assert clip.segment_id == "seg1"


# ---------------------------------------------------------------------------
# Task 5: lip sync integration (Phase 13)
# ---------------------------------------------------------------------------


def _add_lipsync(db, video, segment_id="seg1", language="ur", window_start=900, window_end=1100, after_key="projects/x/lipsync/after.mp4"):
    # need a segment for FK
    from app.models import Transcript, TranscriptSegment

    if db.get(Transcript, "tr-lips") is None:
        db.add(Transcript(id="tr-lips", project_id=video.project_id, video_id=video.id, version=1, status="done", language="en"))
        db.flush()
    if db.get(TranscriptSegment, segment_id) is None:
        db.add(TranscriptSegment(id=segment_id, transcript_id="tr-lips", position=0, start_ms=window_start, end_ms=window_end, text="x"))
        db.flush()
    row = LipSyncResult(
        id="ls1", project_id=video.project_id, video_id=video.id, segment_id=segment_id, language=language,
        attempt=1, is_current=True, status="generated", review="pending", engine="wav2lip",
        window_start_ms=window_start, window_end_ms=window_end, after_key=after_key, before_key="projects/x/lipsync/before.mp4",
        needs_regeneration=False,
    )
    db.add(row)
    db.flush()
    return row


def test_lipsync_display_and_invalidation(world):
    db = world["db"]
    tlm = world["timeline"]
    video = world["video"]
    seg_id = "seg1"
    _add_lipsync(db, video, segment_id=seg_id, language="ur")
    db.commit()

    # displayed
    rows = tl.list_lipsync(db, video.id)
    assert len(rows) == 1
    assert rows[0]["synced"] is True
    assert rows[0]["needs_regeneration"] is False
    assert rows[0]["after_url"]

    # dubbed clip linked to the segment on the urdu track
    track = db.scalar(__import__("sqlalchemy").select(tl.TimelineTrack).where(tl.TimelineTrack.timeline_id == tlm.id, tl.TimelineTrack.kind == "urdu_dub"))
    dub = tl.add_clip(db, tlm.id, track.id, source_type="dub_take", storage_key="projects/x/dubfit/ur/z.wav", name="line",
                      start_ms=950, duration_ms=200, source_in_ms=0, source_out_ms=200, source_duration_ms=200,
                      segment_id=seg_id, language="ur")
    # lip sync segment relationship preserved
    seg_row = db.get(LipSyncResult, "ls1")
    assert seg_row.segment_id == seg_id

    # change dialogue timing -> flag needs regeneration
    tl.move_clip(db, dub.id, 1200)
    seg_row = db.get(LipSyncResult, "ls1")
    assert seg_row.needs_regeneration is True
    out = tl.list_lipsync(db, video.id)
    assert out[0]["needs_regeneration"] is True
    assert out[0]["synced"] is False  # not silently treated as synchronized
    assert out[0]["regeneration_reason"]

    # mark regenerated -> synced again
    tl.mark_lip_sync_regenerated(db, "ls1")
    out = tl.list_lipsync(db, video.id)
    assert out[0]["synced"] is True
    assert out[0]["needs_regeneration"] is False


def test_lipsync_invalidation_on_trim_split_delete(world):
    db = world["db"]
    tlm = world["timeline"]
    video = world["video"]
    _add_lipsync(db, video, segment_id="seg1", language="hi")
    db.commit()
    track = db.scalar(__import__("sqlalchemy").select(tl.TimelineTrack).where(tl.TimelineTrack.timeline_id == tlm.id, tl.TimelineTrack.kind == "hindi_dub"))
    dub = tl.add_clip(db, tlm.id, track.id, source_type="dub_take", storage_key="projects/x/dubfit/hi/z.wav", name="line",
                      start_ms=900, duration_ms=200, source_in_ms=0, source_out_ms=200, source_duration_ms=200,
                      segment_id="seg1", language="hi")
    tl.trim_end(db, dub.id, 50)
    assert db.get(LipSyncResult, "ls1").needs_regeneration is True


def test_lipsync_language_isolation(world):
    """Editing Urdu dubbed audio must not flag a Hindi lip-sync segment."""
    db = world["db"]
    tlm = world["timeline"]
    video = world["video"]
    _add_lipsync(db, video, segment_id="seg1", language="hi")
    db.commit()
    track = db.scalar(__import__("sqlalchemy").select(tl.TimelineTrack).where(tl.TimelineTrack.timeline_id == tlm.id, tl.TimelineTrack.kind == "urdu_dub"))
    dub = tl.add_clip(db, tlm.id, track.id, source_type="dub_take", storage_key="projects/x/dubfit/ur/z.wav", name="line",
                      start_ms=900, duration_ms=200, source_in_ms=0, source_out_ms=200, source_duration_ms=200,
                      segment_id="seg1", language="ur")
    tl.move_clip(db, dub.id, 1500)
    assert db.get(LipSyncResult, "ls1").needs_regeneration is False


# ---------------------------------------------------------------------------
# Task 6: multilingual tracks + export workflow
# ---------------------------------------------------------------------------


def test_export_plan_multitrack_never_combines_languages(world):
    db = world["db"]
    tlm = world["timeline"]
    plan = tl.export_plan(db, tlm, mode="multitrack", container="mkv")
    assert plan["never_combines_languages"] is True
    assert plan["mode"] == "multitrack"
    langs = plan["languages_selected"]
    # original english + urdu + hindi present; not merged
    assert "en" in langs and "ur" in langs and "hi" in langs
    streams = plan["outputs"][0]["audio_streams"]
    stream_langs = [s["stream_language"] for s in streams if s["stream_language"] in ("en", "ur", "hi")]
    assert len(stream_langs) == len(set(stream_langs)), "no language appears twice / merged"


def test_export_plan_separate_language(world):
    db = world["db"]
    tlm = world["timeline"]
    plan = tl.export_plan(db, tlm, mode="separate_language", languages=["ur", "hi"], include_beds=False)
    assert plan["mode"] == "separate_language"
    langs = {o["language"] for o in plan["outputs"]}
    assert langs == {"ur", "hi"}
    for output in plan["outputs"]:
        other = ({"ur", "hi"} - {output["language"]})
        included = {s["stream_language"] for s in output["audio_streams"]}
        assert not (other & included), "separate export must not include another language"


def test_disabled_language_track_excluded_from_export(world):
    db = world["db"]
    tlm = world["timeline"]
    hi = db.scalar(__import__("sqlalchemy").select(tl.TimelineTrack).where(tl.TimelineTrack.timeline_id == tlm.id, tl.TimelineTrack.kind == "hindi_dub"))
    tl.set_track_props(db, hi.id, enabled=False)
    plan = tl.export_plan(db, tlm, mode="multitrack")
    assert "hi" not in plan["languages_selected"]


def test_original_video_preserved_flag(world):
    db = world["db"]
    plan = tl.export_plan(db, world["timeline"], mode="multitrack")
    assert plan["original_video_preserved"] is True
    assert plan["video_track"]


# ---------------------------------------------------------------------------
# Task 8: save + recover
# ---------------------------------------------------------------------------


def test_save_and_reopen(world):
    db = world["db"]
    tlm = world["timeline"]
    clip = _add_audio_clip(db, tlm, kind="external_audio", start_ms=777, duration_ms=333)
    tl.set_clip_props(db, clip.id, volume_db=-4.0, fade_in_ms=50)
    revision = tl.serialize_timeline(db, tlm)["revision"]
    # reopen in a fresh session
    db2 = SessionLocal()
    try:
        reopened = db2.get(TimelineProject, tlm.id)
        out = tl.serialize_timeline(db2, reopened)
        found = [c for t in out["tracks"] for c in t["clips"] if c["id"] == clip.id]
        assert found, "clip restored"
        assert found[0]["start_ms"] == 777
        assert found[0]["volume_db"] == -4.0
        assert found[0]["fade_in_ms"] == 50
        assert out["revision"] == revision
    finally:
        db2.close()


def test_recover_interrupted_session(world):
    """After an abrupt close, reopening returns last persisted state + history for undo."""
    db = world["db"]
    tlm = world["timeline"]
    clip = _add_audio_clip(db, tlm, start_ms=100, duration_ms=400)
    tl.move_clip(db, clip.id, 2500)
    # simulate abrupt end without closing gracefully: new session reads committed state
    db2 = SessionLocal()
    try:
        reopened = db2.get(TimelineProject, tlm.id)
        c = db2.get(TimelineClip, clip.id)
        assert c.start_ms == 2500
        # can undo the move
        tl.undo(db2, reopened)
        assert db2.get(TimelineClip, clip.id).start_ms == 100
    finally:
        db2.close()


def test_external_audio_reference_preserved_across_reopen(client, tmp_path):
    db = SessionLocal()
    try:
        project = _make_project(db)
        video = _make_video(db, project, tmp_path)
        timeline = tl.create_timeline(db, video)
        src = _make_audio_file(tmp_path, "wav", "ext.wav")
        from app.services.external_audio import import_external_audio

        row = import_external_audio(db, project, filename="ext.wav", content_type=None, source_path=src, size_bytes=src.stat().st_size)
        clip = tl.add_external_audio_clip(db, timeline.id, external_audio_id=row.id, start_ms=100)
        # reopen
        db2 = SessionLocal()
        try:
            reopened = db2.get(TimelineProject, timeline.id)
            out = tl.serialize_timeline(db2, reopened)
            found = [c for t in out["tracks"] for c in t["clips"] if c["id"] == clip.id][0]
            assert found["external_audio_id"] == row.id
            assert db2.get(ExternalAudio, row.id).storage_key == row.storage_key
        finally:
            db2.close()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Snapping
# ---------------------------------------------------------------------------


def test_snap_value():
    assert tl.snap_value(133, 250) == 250  # 133 rounds to 250
    assert tl.snap_value(120, 250) == 0  # 120 rounds down to 0
    assert tl.snap_value(133, 0) == 133  # grid disabled
    assert tl.snap_value(100, 250, targets=[100]) == 100  # snap to edge target
