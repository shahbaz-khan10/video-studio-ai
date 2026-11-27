"""Phase 11 — dialogue timing & synchronisation (database orchestration).

Uses Phase 6 timestamps, Phase 7 characters, Phase 8 translations (for
shortening suggestions), Phase 9/10 voices via the current Phase 10 take.
"""

from __future__ import annotations

import io
import subprocess
import wave
from collections.abc import Callable

import numpy as np
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import MediaAsset, ProcessingJob, TranscriptSegment, Video, new_id, utcnow
from ..models_timing import DubTimingPlacement
from ..models_tts import DubAudioTake
from . import dub_audio
from . import timing_engine as engine
from . import translation_lang
from .media import AppError
from .storage import storage
from .tts_engine import ffmpeg_binary, read_wav, to_wav_bytes, tts_settings

ActiveCheck = Callable[[], None]
LANGUAGES = ("ur", "hi")
TIMING_STEP_NAMES = ["select_lines", "fit_lines", "check_sequence", "render_track", "finalize"]
FITTED = {"fitted", "fitted_fast"}


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


def _placements(db: Session, video_id: str, language: str) -> dict[str, DubTimingPlacement]:
    return {
        row.segment_id: row
        for row in db.scalars(
            select(DubTimingPlacement).where(DubTimingPlacement.video_id == video_id, DubTimingPlacement.language == language)
        )
    }


def context(db: Session, video: Video, language: str) -> list[dict]:
    """Ordered dialogue lines with their take, placement and overlap partners."""
    if language not in LANGUAGES:
        raise AppError(422, "UNSUPPORTED_LANGUAGE", "Language must be 'ur' or 'hi'.")
    lines = dub_audio._lines(db, video, language)
    placements = _placements(db, video.id, language)
    lines.sort(key=lambda item: (item["segment"].start_ms, item["segment"].position))
    for index, line in enumerate(lines):
        line["index"] = index
        line["take"] = dub_audio._current(line["takes"])
        line["placement"] = placements.get(line["segment"].id)
        line["overlaps"] = []
    # Original overlaps (sweep over start-sorted lines).
    for i, line in enumerate(lines):
        seg = line["segment"]
        for other in lines[i + 1 :]:
            if other["segment"].start_ms >= seg.end_ms:
                break
            amount = min(seg.end_ms, other["segment"].end_ms) - other["segment"].start_ms
            if amount <= 0:
                continue
            same = (line["character"].id if line["character"] else None) == (other["character"].id if other["character"] else None)
            line["overlaps"].append({"segment_id": other["segment"].id, "ms": amount, "same_speaker": same})
            other["overlaps"].append({"segment_id": seg.id, "ms": amount, "same_speaker": same})
    return lines


def _speaker(line: dict) -> str | None:
    return line["character"].id if line["character"] else (line["segment"].speaker or None)


def window_for(lines: list[dict], index: int) -> engine.Window:
    s = engine.timing_settings
    line = lines[index]
    seg = line["segment"]
    placement: DubTimingPlacement | None = line["placement"]
    if placement is not None and placement.manual and placement.manual_start_ms is not None and placement.manual_end_ms is not None:
        start, end = placement.manual_start_ms, placement.manual_end_ms
    else:
        start, end = seg.start_ms, seg.end_ms
    allow = placement.allow_overlap_ms if placement is not None else 0
    overlapping = {item["segment_id"] for item in line["overlaps"]}

    # Silence before: after the previous non-overlapping line ends (its dubbed end if already placed).
    lead_room = s.max_lead_ms
    for prev in reversed(lines[:index]):
        if prev["segment"].id in overlapping:
            lead_room = 0
            break
        prev_end = prev["segment"].end_ms
        prev_placement = prev["placement"]
        if prev_placement is not None and prev_placement.placed_end_ms is not None and prev_placement.status in FITTED | {"needs_attention"}:
            prev_end = max(prev_end, prev_placement.placed_end_ms)
        lead_room = max(0, start - prev_end - s.guard_ms)
        break
    # Silence after: until the next line starts (never into another speaker's line).
    tail_room = s.max_tail_ms
    for nxt in lines[index + 1 :]:
        if nxt["segment"].id in overlapping:
            tail_room = 0
            break
        nxt_start = nxt["segment"].start_ms
        nxt_placement = nxt["placement"]
        if nxt_placement is not None and nxt_placement.manual and nxt_placement.manual_start_ms is not None:
            nxt_start = nxt_placement.manual_start_ms
        tail_room = max(0, nxt_start - end - s.guard_ms)
        break
    return engine.Window(start_ms=start, end_ms=end, lead_room_ms=lead_room, tail_room_ms=tail_room + max(0, allow))


def line_status(line: dict) -> str:
    placement: DubTimingPlacement | None = line["placement"]
    take: DubAudioTake | None = line["take"]
    if take is None:
        return "no_audio"
    if placement is None or placement.status == "pending":
        return "pending"
    if placement.take_id != take.id and placement.status in FITTED | {"needs_attention"}:
        return "stale"
    return placement.status


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


def _get_or_create(db: Session, video: Video, line: dict, language: str) -> DubTimingPlacement:
    placement = line["placement"]
    if placement is None:
        placement = DubTimingPlacement(
            id=new_id(), project_id=video.project_id, video_id=video.id, segment_id=line["segment"].id, language=language,
            status="pending", tempo=1.0, pause_saved_ms=0, overflow_ms=0, allow_overlap_ms=0, details_json={}, flags=[],
            suggestions=[],
        )
        db.add(placement)
        db.flush()
        line["placement"] = placement
    return placement


def fit_line(db: Session, video: Video, lines: list[dict], index: int, language: str, job_id: str | None = None) -> DubTimingPlacement:
    line = lines[index]
    placement = _get_or_create(db, video, line, language)
    take: DubAudioTake | None = line["take"]
    seg: TranscriptSegment = line["segment"]
    placement.character_id = line["character"].id if line["character"] else None
    placement.job_id = job_id
    placement.error_message = None
    if take is None or not take.storage_key:
        placement.status = "no_audio"
        placement.take_id = None
        placement.flags = [engine._flag("no_audio", "info", "Generate this line's voice (Phase 10) first.")]
        placement.suggestions = []
        db.commit()
        return placement
    window = window_for(lines, index)
    try:
        audio, sr = read_wav(storage.absolute_path(take.storage_key))
        result = engine.fit(audio, sr, window)
    except (engine.TimingError, OSError, ValueError, EOFError, wave.Error) as error:
        placement.status = "failed"
        placement.take_id = take.id
        placement.error_message = str(error)[:900]
        placement.flags = [engine._flag("fit_failed", "error", str(error)[:300])]
        db.commit()
        return placement
    key = f"projects/{video.project_id}/dubfit/{language}/{placement.id}.wav"
    storage.write_bytes(key, to_wav_bytes(result.audio, result.sample_rate))
    placement.storage_key = key
    placement.take_id = take.id
    placement.status = result.status
    placement.strategy = result.strategy
    placement.window_start_ms = window.start_ms
    placement.window_end_ms = window.end_ms
    placement.placed_start_ms = result.placed_start_ms
    placement.placed_end_ms = result.placed_end_ms
    placement.take_duration_ms = result.take_duration_ms
    placement.speech_duration_ms = result.speech_duration_ms
    placement.fitted_duration_ms = result.fitted_duration_ms
    placement.tempo = result.tempo
    placement.pause_saved_ms = result.pause_saved_ms
    placement.overflow_ms = result.overflow_ms
    placement.details_json = {
        "steps": result.steps,
        "lead_room_ms": window.lead_room_ms,
        "tail_room_ms": window.tail_room_ms,
        "lead_used_ms": result.lead_used_ms,
        "tail_used_ms": result.tail_used_ms,
        "voiced_ratio": result.voiced_ratio,
        "original_duration_ms": seg.end_ms - seg.start_ms,
        "difference_ms": result.speech_duration_ms - (seg.end_ms - seg.start_ms),
        "take_number": take.take_number,
        "text": take.text,
    }
    flags = list(result.flags)
    for item in line["overlaps"]:
        if item["same_speaker"]:
            flags.append(engine._flag("same_speaker_overlap", "warning", "Overlaps another line by the same speaker; check the transcript."))
            break
    else:
        if line["overlaps"]:
            flags.append(engine._flag("original_overlap", "info", "Overlaps another character in the original; both are kept in their own windows."))
    if result.status == "needs_attention":
        flags.append(engine._flag("manual_correction", "error", "Cannot fit naturally — needs a shorter line, a faster take or manual timing."))
    placement.flags = flags
    syllables = translation_lang.target_syllables(take.text, language) if take.text else None
    placement.suggestions = engine.suggestions(result, window, syllables)
    db.commit()
    db.refresh(placement)
    return placement


def check_sequence(db: Session, lines: list[dict]) -> int:
    """Keep conversation order and stop one speaker's line covering another's."""
    s = engine.timing_settings
    problems = 0
    placed = [line for line in lines if line["placement"] is not None and line["placement"].placed_start_ms is not None
              and line["placement"].status in FITTED | {"needs_attention"}]
    for line in placed:
        line["placement"].flags = [item for item in (line["placement"].flags or []) if item["code"] not in {"order_changed", "speaker_overlap"}]
    for prev, cur in zip(placed, placed[1:]):
        p, c = prev["placement"], cur["placement"]
        original_overlap = next((item["ms"] for item in cur["overlaps"] if item["segment_id"] == prev["segment"].id), 0)
        if c.placed_start_ms < p.placed_start_ms and not original_overlap:
            c.flags = [*c.flags, engine._flag("order_changed", "error", "Starts before the previous line; conversation order would change.")]
            problems += 1
        overlap = p.placed_end_ms - c.placed_start_ms
        if overlap > 0 and _speaker(prev) != _speaker(cur):
            permitted = original_overlap + s.overlap_tolerance_ms + max(p.allow_overlap_ms, c.allow_overlap_ms)
            if overlap > permitted:
                p.flags = [*p.flags, engine._flag("speaker_overlap", "warning", f"Runs {overlap} ms over the next character's line.")]
                problems += 1
    db.commit()
    return problems


def select_lines(lines: list[dict], options: dict) -> tuple[list[int], dict]:
    mode = options.get("mode") or "pending"
    overwrite = bool(options.get("overwrite_manual"))
    wanted = set(options.get("segment_ids") or [])
    resume = options.get("resume_from_job_id")
    chosen, skipped = [], {"approved": 0, "up_to_date": 0, "no_audio": 0, "done_before_failure": 0}
    for line in lines:
        if wanted and line["segment"].id not in wanted:
            continue
        status = line_status(line)
        placement = line["placement"]
        if status == "no_audio":
            skipped["no_audio"] += 1
            continue
        if placement is not None and placement.approved and not overwrite:
            skipped["approved"] += 1
            continue
        if resume and placement is not None and placement.job_id == resume and status in FITTED | {"needs_attention"}:
            skipped["done_before_failure"] += 1
            continue
        if mode == "pending" and status in FITTED | {"needs_attention"} and status != "stale":
            skipped["up_to_date"] += 1
            continue
        if mode == "failed" and status not in {"failed", "needs_attention"}:
            skipped["up_to_date"] += 1
            continue
        chosen.append(line["index"])
    return chosen, skipped


# ---------------------------------------------------------------------------
# Track rendering & waveforms
# ---------------------------------------------------------------------------


def _clip_audio(placement: DubTimingPlacement) -> np.ndarray | None:
    if not placement.storage_key:
        return None
    path = storage.absolute_path(placement.storage_key)
    if not path.is_file():
        return None
    audio, _sr = read_wav(path)
    return audio


def _soft_limit(block: np.ndarray) -> np.ndarray:
    over = np.abs(block) > 0.89
    if over.any():
        block = block.copy()
        block[over] = np.sign(block[over]) * (0.89 + 0.11 * np.tanh((np.abs(block[over]) - 0.89) / 0.11))
    return block


def render_track(db: Session, video: Video, language: str, check_active: ActiveCheck | None = None) -> dict:
    """Mix all placed clips into one full-length dialogue track (streamed in blocks)."""
    sr = tts_settings.sample_rate
    rows = [
        row for row in _placements(db, video.id, language).values()
        if row.storage_key and row.placed_start_ms is not None and row.status in FITTED | {"needs_attention"}
    ]
    end_ms = max([video.duration_ms or 0] + [row.placed_end_ms or 0 for row in rows]) + 500
    total = int(sr * end_ms / 1000)
    key = f"projects/{video.project_id}/dubtrack/{video.id}_{language}.wav"
    path = storage.absolute_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda row: row.placed_start_ms)
    clips = {row.id: None for row in rows}
    block = sr * 30
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        for start in range(0, total, block):
            if check_active:
                check_active()
            stop = min(total, start + block)
            buffer = np.zeros(stop - start, dtype=np.float32)
            for row in rows:
                clip_start = int(sr * row.placed_start_ms / 1000)
                if clip_start >= stop:
                    break
                if clips[row.id] is None:
                    clips[row.id] = _clip_audio(row)
                clip = clips[row.id]
                if clip is None or clip_start + len(clip) <= start:
                    continue
                a = max(start, clip_start)
                b = min(stop, clip_start + len(clip))
                buffer[a - start : b - start] += clip[a - clip_start : b - clip_start]
            pcm = (np.clip(_soft_limit(buffer), -1, 1) * 32767).astype("<i2")
            handle.writeframes(pcm.tobytes())
            # free clips that ended before this block
            for row in rows:
                clip = clips[row.id]
                if clip is not None and int(sr * row.placed_start_ms / 1000) + len(clip) < start:
                    clips[row.id] = None
    meta = dict(video.metadata_json or {})
    timing = dict(meta.get("dub_timing") or {})
    timing[language] = {"track_key": key, "rendered_at": utcnow().isoformat(), "clips": len(rows), "duration_ms": end_ms,
                        "fingerprint": _track_fingerprint(rows)}
    meta["dub_timing"] = timing
    video.metadata_json = meta
    db.commit()
    return timing[language]


def _track_fingerprint(rows: list[DubTimingPlacement]) -> str:
    import hashlib

    payload = "|".join(f"{row.id}:{row.placed_start_ms}:{row.fitted_duration_ms}:{row.take_id}:{row.tempo}" for row in sorted(rows, key=lambda r: r.id))
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def track_info(db: Session, video: Video, language: str) -> dict | None:
    info = ((video.metadata_json or {}).get("dub_timing") or {}).get(language)
    if not info:
        return None
    rows = [row for row in _placements(db, video.id, language).values()
            if row.storage_key and row.placed_start_ms is not None and row.status in FITTED | {"needs_attention"}]
    exists = storage.absolute_path(info["track_key"]).is_file()
    return {**info, "exists": exists, "stale": _track_fingerprint(rows) != info.get("fingerprint"),
            "url": f"/api/v1/videos/{video.id}/dub-timing/track?language={language}"}


def _original_audio_path(db: Session, video: Video):
    for kind in ("stem_dialogue", "dialogue", "source_audio"):
        asset = db.scalar(
            select(MediaAsset).where(MediaAsset.video_id == video.id, MediaAsset.kind == kind).order_by(MediaAsset.version.desc())
        )
        if asset is not None and storage.absolute_path(asset.storage_key).is_file():
            return storage.absolute_path(asset.storage_key), kind
    path = storage.absolute_path(video.storage_key)
    return (path, "video") if path.is_file() else (None, None)


def _peaks(audio: np.ndarray, buckets: int) -> list[list[float]]:
    if audio.size == 0:
        return [[0.0, 0.0] for _ in range(buckets)]
    edges = np.linspace(0, audio.size, buckets + 1).astype(int)
    out = []
    for a, b in zip(edges[:-1], edges[1:]):
        chunk = audio[a:max(b, a + 1)]
        out.append([round(float(chunk.min()), 4), round(float(chunk.max()), 4)])
    return out


def waveform(db: Session, video: Video, source: str, start_ms: int, end_ms: int, buckets: int) -> dict:
    start_ms = max(0, start_ms)
    end_ms = max(start_ms + 100, end_ms)
    buckets = max(10, min(4000, buckets))
    if source == "original":
        path, kind = _original_audio_path(db, video)
        if path is None:
            raise AppError(404, "ORIGINAL_AUDIO_MISSING", "The original audio is not available.")
        sr = 8000
        result = subprocess.run(
            [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-ss", f"{start_ms / 1000:.3f}", "-t",
             f"{(end_ms - start_ms) / 1000:.3f}", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"],
            capture_output=True, check=False,
        )
        audio = np.frombuffer(result.stdout, dtype="<f4") if result.returncode == 0 else np.zeros(0, dtype=np.float32)
        expected = int(sr * (end_ms - start_ms) / 1000)
        if audio.size < expected:
            audio = np.concatenate([audio, np.zeros(expected - audio.size, dtype=np.float32)])
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 0:
            audio = audio / peak * 0.95  # display only
        return {"source": source, "kind": kind, "start_ms": start_ms, "end_ms": end_ms, "peaks": _peaks(audio, buckets)}
    if source not in LANGUAGES:
        raise AppError(422, "UNSUPPORTED_SOURCE", "Waveform source must be original, ur or hi.")
    sr = tts_settings.sample_rate
    length = int(sr * (end_ms - start_ms) / 1000)
    buffer = np.zeros(length, dtype=np.float32)
    for row in _placements(db, video.id, source).values():
        if row.placed_start_ms is None or row.placed_end_ms is None or row.status not in FITTED | {"needs_attention"}:
            continue
        if row.placed_end_ms <= start_ms or row.placed_start_ms >= end_ms:
            continue
        clip = _clip_audio(row)
        if clip is None:
            continue
        offset = int(sr * (row.placed_start_ms - start_ms) / 1000)
        a = max(0, offset)
        b = min(length, offset + len(clip))
        if b > a:
            buffer[a:b] += clip[a - offset : b - offset]
    return {"source": source, "kind": "dub", "start_ms": start_ms, "end_ms": end_ms, "peaks": _peaks(buffer, buckets)}


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def serialize_placement(row: DubTimingPlacement | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "status": row.status,
        "strategy": row.strategy,
        "manual": row.manual,
        "approved": row.approved,
        "manual_start_ms": row.manual_start_ms,
        "manual_end_ms": row.manual_end_ms,
        "allow_overlap_ms": row.allow_overlap_ms,
        "window_start_ms": row.window_start_ms,
        "window_end_ms": row.window_end_ms,
        "placed_start_ms": row.placed_start_ms,
        "placed_end_ms": row.placed_end_ms,
        "take_id": row.take_id,
        "take_duration_ms": row.take_duration_ms,
        "speech_duration_ms": row.speech_duration_ms,
        "fitted_duration_ms": row.fitted_duration_ms,
        "tempo": row.tempo,
        "pause_saved_ms": row.pause_saved_ms,
        "overflow_ms": row.overflow_ms,
        "details": row.details_json or {},
        "flags": row.flags or [],
        "suggestions": row.suggestions or [],
        "error_message": row.error_message,
        "audio_url": f"/api/v1/dub-timing/placements/{row.id}/audio" if row.storage_key else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def serialize_line(line: dict) -> dict:
    seg: TranscriptSegment = line["segment"]
    take: DubAudioTake | None = line["take"]
    placement = line["placement"]
    return {
        "segment_id": seg.id,
        "position": seg.position,
        "start_ms": seg.start_ms,
        "end_ms": seg.end_ms,
        "duration_ms": seg.end_ms - seg.start_ms,
        "source_text": seg.text,
        "text": line["translation"].text if line["translation"] else "",
        "character_id": line["character"].id if line["character"] else None,
        "character_name": line["character"].name if line["character"] else (seg.speaker or None),
        "take": {
            "id": take.id,
            "take_number": take.take_number,
            "duration_ms": take.duration_ms,
            "approved": take.approved,
            "audio_url": f"/api/v1/dub-audio/takes/{take.id}/audio",
        } if take else None,
        "difference_ms": (take.duration_ms - (seg.end_ms - seg.start_ms)) if take and take.duration_ms else None,
        "overlaps": line["overlaps"],
        "status": line_status(line),
        "placement": serialize_placement(placement),
    }


def bundle(db: Session, video: Video, language: str) -> dict:
    from ..schemas import JobOut

    lines = context(db, video, language)
    statuses = [line_status(line) for line in lines]
    job = db.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.video_id == video.id, ProcessingJob.type == "dub_timing")
        .order_by(ProcessingJob.created_at.desc())
    )
    _path, original_kind = _original_audio_path(db, video)
    return {
        "video_id": video.id,
        "language": language,
        "duration_ms": video.duration_ms,
        "original_audio": original_kind,
        "limits": {
            "natural_tempo": engine.timing_settings.natural_tempo,
            "max_tempo": engine.timing_settings.max_tempo,
            "min_pause_ms": engine.timing_settings.min_pause_ms,
            "max_tail_ms": engine.timing_settings.max_tail_ms,
            "max_lead_ms": engine.timing_settings.max_lead_ms,
        },
        "lines": [serialize_line(line) for line in lines],
        "summary": {status: statuses.count(status) for status in sorted(set(statuses))} | {"total": len(lines)},
        "track": track_info(db, video, language),
        "job": JobOut.model_validate(job).model_dump(mode="json") if job else None,
    }


# ---------------------------------------------------------------------------
# Manual edits
# ---------------------------------------------------------------------------


def update_placement(db: Session, video: Video, segment_id: str, language: str, changes: dict) -> DubTimingPlacement:
    lines = context(db, video, language)
    index = next((line["index"] for line in lines if line["segment"].id == segment_id), None)
    if index is None:
        raise AppError(404, "SEGMENT_NOT_FOUND", "Dialogue line not found in the latest transcript.")
    line = lines[index]
    placement = _get_or_create(db, video, line, language)
    if changes.get("reset"):
        placement.manual = False
        placement.manual_start_ms = None
        placement.manual_end_ms = None
        placement.allow_overlap_ms = 0
        placement.approved = False
    if "allow_overlap_ms" in changes and changes["allow_overlap_ms"] is not None:
        placement.allow_overlap_ms = int(changes["allow_overlap_ms"])
    start, end = changes.get("start_ms"), changes.get("end_ms")
    if start is not None or end is not None:
        new_start = int(start) if start is not None else (placement.manual_start_ms if placement.manual else line["segment"].start_ms)
        new_end = int(end) if end is not None else (
            placement.manual_end_ms if placement.manual and placement.manual_end_ms is not None
            else new_start + (line["segment"].end_ms - line["segment"].start_ms)
        )
        if new_start < 0 or new_end <= new_start:
            raise AppError(422, "INVALID_TIMING", "End must be after start, and start cannot be negative.")
        if video.duration_ms and new_end > video.duration_ms + 2000:
            raise AppError(422, "INVALID_TIMING", "The line cannot end after the video.")
        placement.manual = True
        placement.manual_start_ms = new_start
        placement.manual_end_ms = new_end
    db.flush()
    if changes.get("refit", True) and (start is not None or end is not None or changes.get("reset") or "allow_overlap_ms" in changes):
        fit_line(db, video, lines, index, language)
        check_sequence(db, context(db, video, language))
    if changes.get("approved") is not None:
        if changes["approved"] and placement.status not in FITTED | {"needs_attention"}:
            raise AppError(409, "NOT_FITTED", "Fit this line before approving its timing.")
        placement.approved = bool(changes["approved"])
    db.commit()
    db.refresh(placement)
    return placement


# ---------------------------------------------------------------------------
# Batch job
# ---------------------------------------------------------------------------


def create_timing_job(db: Session, video: Video, options: dict) -> ProcessingJob:
    from .jobs import create_import_job

    active = db.scalar(
        select(ProcessingJob).where(
            ProcessingJob.video_id == video.id, ProcessingJob.type == "dub_timing", ProcessingJob.status.in_(["queued", "running"])
        )
    )
    if active:
        raise AppError(409, "DUB_TIMING_JOB_ACTIVE", "A timing job is already running for this video.")
    return create_import_job(db, video.project_id, video.id, "dub_timing", TIMING_STEP_NAMES, options)


def run_timing_job(job_id: str, video_id: str, options: dict | None = None) -> None:
    from .jobs import JobCancelled, _job_status, _set_step

    db = SessionLocal()
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

        claim = db.execute(
            update(ProcessingJob).where(ProcessingJob.id == job_id, ProcessingJob.status == "queued").values(status="running")
        )
        db.commit()
        if claim.rowcount != 1:
            db.refresh(job)
            if job.status != "cancelled":
                return
            raise JobCancelled("Job was cancelled before it started")
        db.refresh(job)

        _set_step(db, job, "select_lines", "running", "Analysing timing", 3)
        lines = context(db, video, language)
        chosen, skipped = select_lines(lines, opts)
        if opts.get("overwrite_manual"):
            for index in chosen:
                placement = lines[index]["placement"]
                if placement is not None and (placement.manual or placement.approved):
                    placement.manual = False
                    placement.manual_start_ms = placement.manual_end_ms = None
                    placement.approved = False
            db.commit()
        detail = f"{len(chosen)} to fit · " + " · ".join(f"{v} {k.replace('_', ' ')}" for k, v in skipped.items() if v)
        _set_step(db, job, "select_lines", "completed", detail, 6)

        _set_step(db, job, "fit_lines", "running", f"0/{len(chosen)}", 8)
        counts = {"fitted": 0, "fitted_fast": 0, "needs_attention": 0, "failed": 0, "no_audio": 0}
        for done, index in enumerate(chosen):
            check_active()
            placement = fit_line(db, video, lines, index, language, job_id)
            counts[placement.status] = counts.get(placement.status, 0) + 1
            step = next((item for item in job.steps if item.name == "fit_lines"), None)
            if step:
                step.detail = f"{done + 1}/{len(chosen)}"
            job.progress = 8 + int(77 * (done + 1) / max(1, len(chosen)))
            job.current_step = "fit_lines"
            db.commit()
        summary = f"{counts['fitted']} natural · {counts['fitted_fast']} sped up · {counts['needs_attention']} need correction · {counts['failed']} failed"
        _set_step(db, job, "fit_lines", "completed", summary, 86)

        _set_step(db, job, "check_sequence", "running", "Checking order and overlaps", 88)
        problems = check_sequence(db, context(db, video, language))
        _set_step(db, job, "check_sequence", "completed", f"{problems} sequence/overlap warnings", 90)

        if opts.get("render_track", True):
            _set_step(db, job, "render_track", "running", "Rendering dialogue track", 91)
            info = render_track(db, video, language, check_active)
            _set_step(db, job, "render_track", "completed", f"{info['clips']} clips · {info['duration_ms'] / 1000:.1f}s", 98)
        else:
            _set_step(db, job, "render_track", "completed", "skipped", 98)

        final = db.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id, ProcessingJob.status == "running")
            .values(status="completed", current_step="finalize", progress=100, error_code=None, error_message=None)
        )
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
        if current is None:
            return
        if isinstance(error, _Cancelled) or current.status == "cancelled":
            current.status, current.error_code, current.error_message = "cancelled", "CANCELLED", "Cancelled by user"
            final_status, detail = "cancelled", "Cancelled by user"
        else:
            current.status = "failed"
            current.error_code = error.code if isinstance(error, AppError) else "DUB_TIMING_FAILED"
            current.error_message = str(error)[:1000]
            final_status, detail = "failed", str(error)[:500]
        for step in current.steps:
            if step.status == "running" or (final_status == "cancelled" and step.status == "pending"):
                step.status, step.detail, step.finished_at = final_status, detail, utcnow()
        db.commit()
    finally:
        db.close()
