"""Phase 13 — AI lip sync orchestration.

For each selected line and language:
  dub audio  ← Phase 11 fitted clip at its placed time (or the Phase 10 take)
  window     ← original line ∪ dubbed placement, snapped to whole frames
  face       ← manual choice › Phase 7 confirmed candidate › audio-visual
               evidence; otherwise ask the user (never guess)
  render     ← "before" (original frames + dub) and "after" (lip-synced) clips
  QC         ← face coverage, sync confidence, frame/duration checks,
               upper-face and outside-face preservation
The source video is only read, never written.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import (
    Character,
    CharacterAssignment,
    MediaAsset,
    ProcessingJob,
    Transcript,
    TranscriptSegment,
    Video,
    VisualCandidate,
    new_id,
    utcnow,
)
from ..models_lipsync import LipSyncFaceSelection, LipSyncResult
from ..models_timing import DubTimingPlacement
from ..models_tts import DubAudioTake
from . import face_tracking as ft
from . import lipsync_engine as engine
from .media import AppError
from .storage import storage
from .tts_engine import ffmpeg_binary, read_wav, to_wav_bytes

ActiveCheck = Callable[[], None]
LANGUAGES = ("ur", "hi")
LIPSYNC_STEP_NAMES = ["queue", "prepare", "load_model", "process", "finalize"]
_job_slot = threading.Semaphore(1)  # one lip-sync job at a time: never two large models at once


def flag(code: str, severity: str, message: str) -> dict:
    return {"code": code, "severity": severity, "message": message}


def latest_transcript(db: Session, video_id: str) -> Transcript | None:
    return db.scalar(
        select(Transcript).where(Transcript.video_id == video_id).order_by(Transcript.version.desc(), Transcript.created_at.desc())
    )


def video_path(video: Video) -> Path:
    path = storage.absolute_path(video.storage_key)
    if not path.is_file():
        raise AppError(410, "FILE_MISSING", "The source video file is missing.")
    return path


def _props(video: Video) -> tuple[float, int, int]:
    if not video.fps or not video.width or not video.height:
        raise AppError(422, "VIDEO_NOT_PROBED", "Video frame rate and size are unknown; re-run media inspection first.")
    return float(video.fps), int(video.width), int(video.height)


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------


def _decode_audio(path: Path, start_ms: int, end_ms: int, sr: int = 16000) -> np.ndarray:
    result = subprocess.run(
        [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-ss", f"{start_ms / 1000:.3f}", "-t",
         f"{max(0.05, (end_ms - start_ms) / 1000):.3f}", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"],
        capture_output=True, check=False,
    )
    audio = np.frombuffer(result.stdout, dtype="<f4").copy() if result.returncode == 0 else np.zeros(0, np.float32)
    expected = int(sr * (end_ms - start_ms) / 1000)
    return np.pad(audio, (0, max(0, expected - audio.size)))[:expected]


def _resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    from scipy.signal import resample_poly
    from math import gcd

    g = gcd(src, dst)
    return resample_poly(audio, dst // g, src // g).astype(np.float32)


def original_dialogue_audio(db: Session, video: Video, start_ms: int, end_ms: int) -> np.ndarray:
    """Original dialogue for speaker evidence: Phase 4 dialogue stem if present, else the soundtrack."""
    for kind in ("stem_dialogue", "dialogue"):
        asset = db.scalar(select(MediaAsset).where(MediaAsset.video_id == video.id, MediaAsset.kind == kind).order_by(MediaAsset.version.desc()))
        if asset is not None and storage.absolute_path(asset.storage_key).is_file():
            return _decode_audio(storage.absolute_path(asset.storage_key), start_ms, end_ms)
    return _decode_audio(video_path(video), start_ms, end_ms)


def dub_source(db: Session, segment: TranscriptSegment, language: str) -> dict | None:
    """The dubbed audio to drive the lips: Phase 11 placement first, then the Phase 10 take."""
    placement = db.scalar(
        select(DubTimingPlacement).where(DubTimingPlacement.segment_id == segment.id, DubTimingPlacement.language == language)
    )
    if placement is not None and placement.storage_key and placement.placed_start_ms is not None and placement.status in {
        "fitted", "fitted_fast", "needs_attention"
    }:
        path = storage.absolute_path(placement.storage_key)
        if path.is_file():
            return {"source": "phase11_placement", "ref": placement.id, "path": path,
                    "start_ms": placement.placed_start_ms, "end_ms": placement.placed_end_ms}
    take = db.scalar(
        select(DubAudioTake).where(
            DubAudioTake.segment_id == segment.id, DubAudioTake.language == language,
            DubAudioTake.is_current.is_(True), DubAudioTake.status == "ready",
        )
    )
    if take is not None and take.storage_key and storage.absolute_path(take.storage_key).is_file():
        return {"source": "phase10_take", "ref": take.id, "path": storage.absolute_path(take.storage_key),
                "start_ms": segment.start_ms, "end_ms": segment.start_ms + (take.duration_ms or 0)}
    return None


def dub_fingerprint(dub: dict | None) -> str | None:
    """Identity of the dubbed audio a result was made from (source, take/placement, timing, file)."""
    if dub is None:
        return None
    import hashlib

    stat = dub["path"].stat() if dub["path"].is_file() else None
    payload = f"{dub['source']}|{dub['ref']}|{dub['start_ms']}|{dub['end_ms']}|{stat.st_size if stat else 0}|{int(stat.st_mtime) if stat else 0}"
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def window_for(video: Video, segment: TranscriptSegment, dub: dict) -> dict:
    fps, _w, _h = _props(video)
    pad = engine.lipsync_settings.window_pad_ms
    start_ms = max(0, min(segment.start_ms, dub["start_ms"]) - pad)
    end_ms = max(segment.end_ms, dub["end_ms"]) + pad
    if video.duration_ms:
        end_ms = min(end_ms, video.duration_ms)
    if (end_ms - start_ms) / 1000 > engine.lipsync_settings.max_segment_s:
        raise AppError(422, "SEGMENT_TOO_LONG", f"Lines longer than {engine.lipsync_settings.max_segment_s:.0f}s are not lip-synced in one pass.")
    start_frame = int(np.floor(start_ms / 1000 * fps))
    end_frame = int(np.ceil(end_ms / 1000 * fps))
    return {"start_frame": start_frame, "count": max(1, end_frame - start_frame), "fps": fps,
            "start_ms": int(round(start_frame / fps * 1000)), "end_ms": int(round(end_frame / fps * 1000))}


def dub_audio_window(dub: dict, window: dict) -> tuple[np.ndarray, np.ndarray, int]:
    """Dub clip placed inside the window: (16 kHz for the model, 24 kHz for the output, sr_out)."""
    clip, sr = read_wav(dub["path"])
    length_s = window["count"] / window["fps"]
    out = np.zeros(int(round(length_s * sr)), dtype=np.float32)
    offset = int(round((dub["start_ms"] - window["start_ms"]) / 1000 * sr))
    a, b = max(0, offset), min(out.size, offset + clip.size)
    if b > a:
        out[a:b] = clip[a - offset : b - offset]
    return _resample(out, sr, 16000), out, sr


# ---------------------------------------------------------------------------
# Faces
# ---------------------------------------------------------------------------


def _detector() -> ft.FaceDetector:
    path = engine.lipsync_settings.effective_weights_dir / engine.lipsync_settings.face_detector
    try:
        return ft.FaceDetector(path)
    except FileNotFoundError as error:
        raise engine.LipSyncUnavailable(str(error)) from error


def phase7_box(db: Session, segment: TranscriptSegment) -> list[float] | None:
    assignment = db.scalar(select(CharacterAssignment).where(CharacterAssignment.segment_id == segment.id))
    if assignment is None or not assignment.character_id:
        return None
    candidate = db.scalar(
        select(VisualCandidate).where(
            VisualCandidate.segment_id == segment.id,
            VisualCandidate.confirmed.is_(True),
            VisualCandidate.confirmed_character_id == assignment.character_id,
        )
    )
    return list(candidate.bbox_json) if candidate is not None and candidate.bbox_json else None


def analyse(db: Session, video: Video, segment: TranscriptSegment, window: dict) -> ft.Analysis:
    fps, width, height = _props(video)
    original = original_dialogue_audio(db, video, int(segment.start_ms), int(segment.end_ms))
    # Evidence uses only the original line's frames: that is when the real speaker's mouth moves.
    seg_start = int(np.floor(segment.start_ms / 1000 * fps))
    full = ft.analyse_window(video_path(video), window["start_frame"], window["count"], fps, width, height, _detector(),
                             None, 16000)
    lead = seg_start - window["start_frame"]
    energy = [0.0] * max(0, lead) + ft.audio_energy_per_frame(original, 16000, max(0, full.frame_count - max(0, lead)), fps)
    full.energy = energy[: full.frame_count]
    for track in full.tracks:
        line_frames = {i for i in track.frames if lead <= i}
        track.correlation = ft.correlate(track.activity, full.energy, line_frames)
    return full


def selection_for(db: Session, segment: TranscriptSegment) -> LipSyncFaceSelection | None:
    return db.scalar(select(LipSyncFaceSelection).where(LipSyncFaceSelection.segment_id == segment.id))


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _new_result(db: Session, video: Video, segment: TranscriptSegment, language: str, job_id: str | None) -> LipSyncResult:
    previous = db.scalars(select(LipSyncResult).where(LipSyncResult.segment_id == segment.id, LipSyncResult.language == language)).all()
    assignment = db.scalar(select(CharacterAssignment).where(CharacterAssignment.segment_id == segment.id))
    result = LipSyncResult(
        id=new_id(), project_id=video.project_id, video_id=video.id, segment_id=segment.id, language=language,
        attempt=max((item.attempt for item in previous), default=0) + 1, is_current=False, status="failed",
        engine=engine.effective_backend(), character_id=assignment.character_id if assignment else None, job_id=job_id,
        faces_json=[], qc_json={}, flags=[],
    )
    db.add(result)
    return result


def _make_current(db: Session, result: LipSyncResult) -> None:
    db.execute(
        update(LipSyncResult)
        .where(LipSyncResult.segment_id == result.segment_id, LipSyncResult.language == result.language)
        .values(is_current=False)
    )
    result.is_current = True


def generate_segment(db: Session, video: Video, segment: TranscriptSegment, language: str, job_id: str | None = None,
                     check_active: ActiveCheck | None = None, progress: Callable[[int, int], None] | None = None) -> LipSyncResult:
    started = time.time()
    result = _new_result(db, video, segment, language, job_id)
    selection = selection_for(db, segment)
    try:
        backend = engine.effective_backend()
        if backend == "none":
            raise engine.LipSyncUnavailable("No lip-sync engine is available (Wav2Lip weights missing and no GPU worker configured).")
        dub = dub_source(db, segment, language)
        if dub is None:
            result.status = "no_audio"
            result.flags = [flag("no_dub_audio", "info", "Generate and time this line's dubbed voice first (Phases 10–11).")]
            db.commit()
            return result
        result.audio_source, result.audio_ref = dub["source"], dub["ref"]
        result.dubbed_fingerprint = dub_fingerprint(dub)
        result.needs_regeneration = False
        result.regeneration_reason = None
        window = window_for(video, segment, dub)
        fps, width, height = _props(video)
        result.window_start_ms, result.window_end_ms = window["start_ms"], window["end_ms"]
        result.start_frame, result.frame_count, result.fps = window["start_frame"], window["count"], fps
        result.width, result.height = width, height
        if selection is not None and selection.off_screen:
            result.status = "no_face"
            result.target_source = "manual_off_screen"
            result.flags = [flag("off_screen", "info", "Marked as off-screen dialogue; the picture is left unchanged.")]
            db.commit()
            return result
        analysis = analyse(db, video, segment, window)
        result.faces_json = analysis.summary()
        track, source, flags = ft.choose_target(analysis, list(selection.box_json) if selection and selection.box_json else None,
                                                phase7_box(db, segment))
        result.target_source = source
        result.flags = flags
        if track is None:
            result.status = "no_face" if source == "no_visible_face" else "needs_face"
            result.processing_s = round(time.time() - started, 2)
            db.commit()
            return result
        boxes = engine.track_boxes(track, analysis.frame_count, analysis.scale, width, height)
        audio16, audio_out, sr_out = dub_audio_window(dub, window)
        src = video_path(video)
        base = f"projects/{video.project_id}/lipsync/{language}/{result.id}"
        with tempfile.TemporaryDirectory(prefix="lipsync-") as tmp:
            wav = Path(tmp) / "dub.wav"
            wav.write_bytes(to_wav_bytes(audio_out, sr_out))
            before = storage.absolute_path(f"{base}_before.mp4")
            after = storage.absolute_path(f"{base}_after.mp4")
            before.parent.mkdir(parents=True, exist_ok=True)
            engine.render_original(src, window["start_frame"], window["count"], fps, width, height, wav, before)
            if backend == "remote":
                silent = Path(tmp) / "segment.mp4"
                engine.render_original(src, window["start_frame"], window["count"], fps, width, height, None, silent)
                raw = Path(tmp) / "worker.mp4"
                engine.generate_remote(silent, wav, [list(b) if b else None for b in boxes], fps, raw)
                engine.render_original(raw, 0, window["count"], fps, width, height, wav, after)
                result.model = engine.lipsync_settings.remote_model
            else:
                engine.generate_wav2lip(src, window["start_frame"], window["count"], fps, width, height, boxes, audio16, wav, after,
                                        check_active, progress)
                result.model = engine.lipsync_settings.wav2lip_checkpoint
        result.before_key = f"{base}_before.mp4"
        result.after_key = f"{base}_after.mp4"
        result.qc_json, qc_flags = quality_check(db, video, segment, window, boxes, audio16, before, after, track, analysis)
        result.flags = flags + qc_flags
        result.status = "failed" if any(item["severity"] == "error" for item in qc_flags if item["code"] in {"corrupted_frames", "frame_count_mismatch"}) else "generated"
        result.review = "pending"
        if result.status == "generated":
            _make_current(db, result)
    except engine.LipSyncUnavailable:
        db.rollback()
        raise
    except AppError as error:
        result.status = "failed"
        result.error_message = error.message
        result.flags = [*(result.flags or []), flag("failed", "error", error.message)]
    except Exception as error:  # noqa: BLE001
        result.status = "failed"
        result.error_message = str(error)[:1000]
        result.flags = [*(result.flags or []), flag("failed", "error", str(error)[:300])]
    result.processing_s = round(time.time() - started, 2)
    db.add(result)
    db.commit()
    db.refresh(result)
    return result


def quality_check(db: Session, video: Video, segment: TranscriptSegment, window: dict, boxes: list, audio16: np.ndarray,
                  before: Path, after: Path, track: ft.Track, analysis: ft.Analysis) -> tuple[dict, list[dict]]:
    fps, width, height = _props(video)
    count = window["count"]
    flags: list[dict] = []
    out = engine.probe_output(after, fps)
    qc: dict = {"output": out}
    if not out.get("ok"):
        flags.append(flag("corrupted_frames", "error", "The generated clip could not be decoded."))
        return qc, flags
    if abs(out["frames"] - count) > 1:
        flags.append(flag("frame_count_mismatch", "error", f"Generated {out['frames']} frames, expected {count}."))
    if out["black_frames"]:
        flags.append(flag("corrupted_frames", "error", f"{out['black_frames']} black/corrupted frames in the output."))
    expected_s = count / fps
    if out.get("duration_s") is not None and abs(out["duration_s"] - expected_s) > 1.5 / fps + 0.05:
        flags.append(flag("duration_mismatch", "warning", f"Clip lasts {out['duration_s']:.2f}s, expected {expected_s:.2f}s."))
    covered = sum(1 for box in boxes if box is not None)
    qc["face_coverage"] = round(covered / max(1, count), 3)
    qc["frames_modified"] = covered
    face_px = int(np.median([b[3] - b[1] for b in boxes if b is not None])) if covered else 0
    qc["face_height_px"] = face_px
    if qc["face_coverage"] < 0.85:
        flags.append(flag("missing_faces", "warning", f"The face is visible in only {qc['face_coverage']:.0%} of frames; other frames are unchanged."))
    if face_px and face_px < 80:
        flags.append(flag("small_face", "warning", f"Face is only ~{face_px}px tall; mouth detail is limited."))
    if face_px > 260:
        flags.append(flag("large_face", "warning", f"Face is ~{face_px}px tall; the 96px Wav2Lip mouth region will look softer than the rest of the face."))
    others = [t for t in analysis.tracks if t.id != track.id]
    qc["other_faces"] = len(others)
    qc["upper_face_psnr_db"] = engine.region_psnr(before, after, 0, count, fps, width, height, boxes, "upper_face")
    qc["outside_face_psnr_db"] = engine.region_psnr(before, after, 0, count, fps, width, height, boxes, "outside_face")
    if qc["upper_face_psnr_db"] is not None and qc["upper_face_psnr_db"] < 30:
        flags.append(flag("unnatural_change", "warning", f"Eyes/forehead changed more than expected (PSNR {qc['upper_face_psnr_db']} dB)."))
    if qc["outside_face_psnr_db"] is not None and qc["outside_face_psnr_db"] < 40:
        flags.append(flag("unrelated_change", "error", "Pixels outside the target face changed; check the output."))
    if engine.syncnet_available():
        original = original_dialogue_audio(db, video, window["start_ms"], window["end_ms"])
        qc["sync_original"] = engine.sync_score(before, 0, count, fps, width, height, boxes, original)
        qc["sync_before"] = engine.sync_score(before, 0, count, fps, width, height, boxes, audio16)
        qc["sync_after"] = engine.sync_score(after, 0, count, fps, width, height, boxes, audio16)
        after_score = qc["sync_after"]
        if after_score is None:
            flags.append(flag("sync_unmeasured", "info", "Too few frames with a visible face to measure sync."))
        elif after_score["confidence"] < 0.05 or abs(after_score["best_offset_frames"]) > 2:
            flags.append(flag("poor_sync", "warning",
                              f"Mouth/audio agreement is weak (confidence {after_score['confidence']:.3f}, offset {after_score['best_offset_frames']} frames)."))
    return qc, flags


# ---------------------------------------------------------------------------
# Listing & review
# ---------------------------------------------------------------------------


def staleness(row: LipSyncResult, current_fp: str | None) -> str | None:
    """Why a generated result no longer matches the dub (None = up to date)."""
    if row.status != "generated":
        return None
    if getattr(row, "needs_regeneration", False):
        return getattr(row, "regeneration_reason", None) or "Marked for regeneration."
    if current_fp is None:
        return "The dubbed audio for this line was removed."
    if row.dubbed_fingerprint and row.dubbed_fingerprint != current_fp:
        return "The dubbed audio or its timing changed after this lip sync was made."
    return None


def serialize_result(row: LipSyncResult | None, current_fp: str | None = "__unknown__") -> dict | None:
    if row is None:
        return None
    stale = staleness(row, current_fp) if current_fp != "__unknown__" else (
        (getattr(row, "regeneration_reason", None) or "Marked for regeneration.") if getattr(row, "needs_regeneration", False) else None
    )
    return {
        "stale": bool(stale),
        "stale_reason": stale,
        "id": row.id, "language": row.language, "attempt": row.attempt, "is_current": row.is_current, "status": row.status,
        "review": row.review, "review_note": row.review_note, "engine": row.engine, "model": row.model,
        "target_source": row.target_source, "audio_source": row.audio_source,
        "window_start_ms": row.window_start_ms, "window_end_ms": row.window_end_ms, "frame_count": row.frame_count,
        "fps": row.fps, "width": row.width, "height": row.height, "faces": row.faces_json, "qc": row.qc_json,
        "flags": row.flags, "processing_s": row.processing_s, "error_message": row.error_message,
        "after_url": f"/api/v1/lipsync/results/{row.id}/video?variant=after" if row.after_key else None,
        "before_url": f"/api/v1/lipsync/results/{row.id}/video?variant=before" if row.before_key else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def bundle(db: Session, video: Video, language: str) -> dict:
    from ..schemas import JobOut

    if language not in LANGUAGES:
        raise AppError(422, "UNSUPPORTED_LANGUAGE", "Language must be 'ur' or 'hi'.")
    transcript = latest_transcript(db, video.id)
    segments = list(transcript.segments) if transcript else []
    ids = [s.id for s in segments]
    chars = {c.id: c for c in db.scalars(select(Character).where(Character.project_id == video.project_id))}
    assignments = {a.segment_id: a.character_id for a in db.scalars(select(CharacterAssignment).where(CharacterAssignment.segment_id.in_(ids)))} if ids else {}
    selections = {s.segment_id: s for s in db.scalars(select(LipSyncFaceSelection).where(LipSyncFaceSelection.video_id == video.id))}
    results: dict[str, list[LipSyncResult]] = {}
    for row in db.scalars(select(LipSyncResult).where(LipSyncResult.video_id == video.id, LipSyncResult.language == language).order_by(LipSyncResult.attempt)):
        results.setdefault(row.segment_id, []).append(row)
    lines = []
    for segment in segments:
        rows = results.get(segment.id, [])
        current = next((r for r in reversed(rows) if r.is_current), None)
        latest = rows[-1] if rows else None
        dub = dub_source(db, segment, language)
        current_fp = dub_fingerprint(dub)
        if current is not None:
            reason = staleness(current, current_fp)
            if reason and not getattr(current, "needs_regeneration", False):
                # Persist so Phase 14's timeline never treats outdated lip sync as synchronised.
                current.needs_regeneration = True
                current.regeneration_reason = reason[:255]
                db.commit()
        selection = selections.get(segment.id)
        character = chars.get(assignments.get(segment.id) or "")
        lines.append({
            "segment_id": segment.id, "position": segment.position, "start_ms": segment.start_ms, "end_ms": segment.end_ms,
            "text": segment.text, "character_id": character.id if character else None,
            "character_name": character.name if character else (segment.speaker or None),
            "dub": {"source": dub["source"], "start_ms": dub["start_ms"], "end_ms": dub["end_ms"],
                    "url": (f"/api/v1/dub-timing/placements/{dub['ref']}/audio" if dub["source"] == "phase11_placement"
                            else f"/api/v1/dub-audio/takes/{dub['ref']}/audio")} if dub else None,
            "selection": {"time_ms": selection.time_ms, "box": selection.box_json, "off_screen": selection.off_screen,
                          "character_id": selection.character_id} if selection else None,
            "current": serialize_result(current, current_fp),
            "latest": serialize_result(latest, current_fp),
            "attempts": len(rows),
        })
    job = db.scalar(select(ProcessingJob).where(ProcessingJob.video_id == video.id, ProcessingJob.type == "lipsync")
                    .order_by(ProcessingJob.created_at.desc()))
    return {"video_id": video.id, "language": language, "fps": video.fps, "width": video.width, "height": video.height,
            "engine": engine.engine_info(), "lines": lines,
            "job": JobOut.model_validate(job).model_dump(mode="json") if job else None}


def review(db: Session, result: LipSyncResult, decision: str, note: str | None) -> LipSyncResult:
    if result.status != "generated":
        raise AppError(409, "NOT_REVIEWABLE", "Only generated results can be accepted or rejected.")
    result.review = decision
    result.review_note = (note or "").strip() or None
    if decision == "accepted":
        _make_current(db, result)
    else:
        # A rejected result is never used downstream (e.g. the Phase 14 timeline).
        result.is_current = False
    db.commit()
    db.refresh(result)
    return result


def set_selection(db: Session, video: Video, segment: TranscriptSegment, time_ms: int, box: list[float] | None,
                  character_id: str | None, off_screen: bool) -> LipSyncFaceSelection:
    if not off_screen:
        if not box or len(box) != 4 or not all(0 <= v <= 1 for v in box) or box[2] <= 0 or box[3] <= 0:
            raise AppError(422, "INVALID_FACE_BOX", "Choose a face: box must be normalised [x, y, w, h] inside the frame.")
    if character_id and db.get(Character, character_id) is None:
        raise AppError(404, "CHARACTER_NOT_FOUND", "Character not found.")
    row = selection_for(db, segment)
    if row is None:
        row = LipSyncFaceSelection(project_id=video.project_id, video_id=video.id, segment_id=segment.id, time_ms=time_ms)
        db.add(row)
    row.time_ms = time_ms
    row.box_json = list(box) if box and not off_screen else []
    row.character_id = character_id
    row.off_screen = off_screen
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Jobs (queued: one lip-sync job runs at a time)
# ---------------------------------------------------------------------------


def create_lipsync_job(db: Session, video: Video, options: dict) -> ProcessingJob:
    from .jobs import create_import_job

    return create_import_job(db, video.project_id, video.id, "lipsync", LIPSYNC_STEP_NAMES, options)


def run_lipsync_job(job_id: str, video_id: str, options: dict | None = None) -> None:
    from .jobs import JobCancelled, _job_status, _set_step

    db = SessionLocal()
    acquired = False
    try:
        job = db.get(ProcessingJob, job_id)
        video = db.get(Video, video_id)
        if not job or not video:
            return
        opts = options or job.options_json or {}
        language = opts.get("language") or "ur"

        def check_active() -> None:
            status = _job_status(job_id)
            if status is None or status == "cancelled":
                raise JobCancelled("Job was cancelled or removed")

        _set_step(db, job, "queue", "running", "Waiting for the lip-sync engine", 1)
        while not _job_slot.acquire(timeout=1.0):
            check_active()
        acquired = True
        claim = db.execute(update(ProcessingJob).where(ProcessingJob.id == job_id, ProcessingJob.status == "queued").values(status="running"))
        db.commit()
        if claim.rowcount != 1:
            db.refresh(job)
            if job.status != "cancelled":
                return
            raise JobCancelled("Job was cancelled before it started")
        db.refresh(job)
        _set_step(db, job, "queue", "completed", "Engine free", 2)

        _set_step(db, job, "prepare", "running", "Selecting lines", 4)
        transcript = latest_transcript(db, video.id)
        wanted = set(opts.get("segment_ids") or [])
        resume = opts.get("resume_from_job_id")
        done_before = {
            r.segment_id for r in db.scalars(select(LipSyncResult).where(LipSyncResult.job_id == resume, LipSyncResult.language == language))
        } if resume else set()
        segments = [s for s in (transcript.segments if transcript else []) if (not wanted or s.id in wanted) and s.id not in done_before]
        _set_step(db, job, "prepare", "completed", f"{len(segments)} lines" + (f" · {len(done_before)} done before failure" if done_before else ""), 6)

        _set_step(db, job, "load_model", "running", f"Loading {engine.effective_backend()}", 8)
        check_active()
        if engine.effective_backend() == "wav2lip":
            engine.load_models(need_generator=True, need_syncnet=engine.syncnet_available())
        elif engine.effective_backend() == "none":
            raise engine.LipSyncUnavailable("No lip-sync engine is available.")
        _set_step(db, job, "load_model", "completed", engine.effective_backend(), 12)

        _set_step(db, job, "process", "running", f"0/{len(segments)}", 14)
        counts: dict[str, int] = {}
        for done, segment in enumerate(segments):
            check_active()

            def on_progress(frames: int, total: int, done=done) -> None:
                frac = (done + frames / max(1, total)) / max(1, len(segments))
                job.progress = 14 + int(80 * frac)
                step = next((item for item in job.steps if item.name == "process"), None)
                if step:
                    step.detail = f"line {done + 1}/{len(segments)} · frame {frames}/{total}"
                db.commit()

            result = generate_segment(db, video, segment, language, job_id, check_active, on_progress)
            counts[result.status] = counts.get(result.status, 0) + 1
            job.progress = 14 + int(80 * (done + 1) / max(1, len(segments)))
            db.commit()
        summary = " · ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(counts.items())) or "nothing to do"
        _set_step(db, job, "process", "completed", summary, 95)
        final = db.execute(update(ProcessingJob).where(ProcessingJob.id == job_id, ProcessingJob.status == "running")
                           .values(status="completed", current_step="finalize", progress=100, error_code=None, error_message=None))
        if final.rowcount != 1:
            db.rollback()
            raise JobCancelled("Job was cancelled before finishing")
        db.commit()
        db.refresh(job)
        _set_step(db, job, "finalize", "completed", summary, 100)
    except Exception as error:  # noqa: BLE001
        from .jobs import JobCancelled as _Cancelled

        db.rollback()
        current = db.get(ProcessingJob, job_id)
        if current is not None:
            if isinstance(error, _Cancelled) or current.status == "cancelled":
                current.status, current.error_code, current.error_message = "cancelled", "CANCELLED", "Cancelled by user"
                final_status, detail = "cancelled", "Cancelled by user"
            else:
                current.status = "failed"
                current.error_code = "LIPSYNC_ENGINE_UNAVAILABLE" if isinstance(error, engine.LipSyncUnavailable) else (
                    error.code if isinstance(error, AppError) else "LIPSYNC_FAILED")
                current.error_message = str(error)[:1000]
                final_status, detail = "failed", str(error)[:500]
            for step in current.steps:
                if step.status == "running" or (final_status == "cancelled" and step.status == "pending"):
                    step.status, step.detail, step.finished_at = final_status, detail, utcnow()
            db.commit()
    finally:
        if acquired:
            _job_slot.release()
        db.close()
