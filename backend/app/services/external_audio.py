"""Phase 14 - external audio import (Task 3).

Accepts WAV / MP3 / AAC / M4A / FLAC / OGG created in other applications.
The uploaded file is copied *unchanged* into project storage; all editing is
non-destructive. Duration/sample-rate/channels are probed with ffprobe/ffmpeg
and a cached waveform overview is generated once (Task 9: efficient previews,
no need to load everything into browser memory).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Project, new_id, utcnow
from ..models_timeline import AUDIO_EXTENSIONS, AUDIO_MIME, ExternalAudio
from .media import AppError, ffmpeg_binary, ffprobe_binary
from .storage import storage
from .timeline import peaks

MAX_AUDIO_MB = 512
_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_SAMPLE_RATE = re.compile(r"(\d+)\s*Hz")
_CHANNELS = re.compile(r"Hz,\s*([a-z0-9.() ]+?)(?:,|\n)")


def _safe_ext(filename: str) -> str:
    name = (filename or "").lower()
    for ext in AUDIO_EXTENSIONS:
        if name.endswith(ext):
            return ext
    raise AppError(422, "UNSUPPORTED_AUDIO", f"Unsupported audio format. Supported: {', '.join(sorted(AUDIO_EXTENSIONS))}.")


def detect_audio_format(head: bytes, filename: str) -> str:
    """Validate magic bytes + extension. Returns normalized format key."""
    ext = _safe_ext(filename)
    fmt = AUDIO_EXTENSIONS[ext]
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        # MP4/M4A audio container (also raw AAC in MP4)
        return "m4a" if fmt in ("m4a", "aac") else "m4a"
    # frame-synced MP3 / ADTS AAC
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        if (head[1] & 0xF6) == 0xF0:
            return "aac"
        return "mp3"
    # WAV extensible etc. without perfect magic: trust extension, ffmpeg verifies decodability later.
    # A raw AAC ADTS stream may not match above; fall back to extension.
    if fmt in ("wav", "flac", "ogg", "mp3", "aac", "m4a"):
        # reject obviously non-audio payloads
        if head[:5].lower().startswith(b"<?xml") or head[:1] == b"{" or head[:1] == b"[" or head[:9].lower() == b"<!doctype":
            raise AppError(422, "UNSUPPORTED_AUDIO", "File does not look like audio.")
        return fmt
    raise AppError(422, "UNSUPPORTED_AUDIO", "Unrecognised audio file.")


def probe_audio(path: Path) -> dict:
    """duration_ms / sample_rate / channels / codec. Never raises for unknown values."""
    duration_ms = None
    sample_rate = None
    channels = None
    codec = None
    ffprobe = ffprobe_binary()
    if ffprobe:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, errors="ignore", timeout=60, check=False,
        )
        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout or "{}")
            except json.JSONDecodeError:
                payload = {}
            streams = [s for s in payload.get("streams", []) if s.get("codec_type") == "audio"]
            stream = streams[0] if streams else {}
            codec = stream.get("codec_name")
            try:
                sample_rate = int(stream.get("sample_rate")) if stream.get("sample_rate") else None
            except (TypeError, ValueError):
                sample_rate = None
            channels = stream.get("channels")
            dur = payload.get("format", {}).get("duration") or stream.get("duration")
            try:
                duration_ms = int(float(dur) * 1000) if dur else None
            except (TypeError, ValueError):
                duration_ms = None
    if duration_ms is None:
        binary = ffmpeg_binary()
        if binary:
            result = subprocess.run([binary, "-hide_banner", "-i", str(path)], capture_output=True, text=True, errors="ignore", check=False)
            output = result.stderr or ""
            match = _DURATION.search(output)
            if match:
                hours, minutes, seconds = match.groups()
                duration_ms = int((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)
            if sample_rate is None:
                sr_match = _SAMPLE_RATE.search(output)
                if sr_match:
                    sample_rate = int(sr_match.group(1))
            audio_line = next((line for line in output.splitlines() if "Audio:" in line), "")
            if codec is None and "Audio:" in audio_line:
                codec = audio_line.split("Audio:", 1)[1].split(",", 1)[0].split()[0].strip() or None
    if duration_ms is None and path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as handle:
                sr = handle.getframerate()
                frames = handle.getnframes()
                channels = handle.getnchannels()
                sample_rate = sr
                duration_ms = int(frames / sr * 1000) if sr else None
        except (wave.Error, EOFError, OSError):
            pass
    return {"duration_ms": duration_ms, "sample_rate": sample_rate, "channels": channels, "codec": codec}


def verify_decodable(path: Path) -> bool:
    binary = ffmpeg_binary()
    if binary is None:
        # WAV can be verified with the stdlib
        if path.suffix.lower() == ".wav":
            try:
                with wave.open(str(path), "rb") as handle:
                    handle.getnframes()
                return True
            except (wave.Error, EOFError, OSError):
                return False
        return True  # cannot verify without ffmpeg; accept
    result = subprocess.run(
        [binary, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-f", "null", "-"],
        capture_output=True, check=False, timeout=120,
    )
    return result.returncode == 0


def _decode_mono(path: Path, sr: int) -> np.ndarray:
    binary = ffmpeg_binary()
    if binary is None:
        return np.zeros(0, dtype=np.float32)
    result = subprocess.run(
        [binary, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(result.stdout, dtype="<f4").astype(np.float32)


def import_external_audio(
    db: Session,
    project: Project,
    *,
    filename: str,
    content_type: str | None,
    source_path: Path,
    size_bytes: int,
    video_id: str | None = None,
    notes: str | None = None,
) -> ExternalAudio:
    if size_bytes <= 0:
        raise AppError(422, "EMPTY_FILE", "The selected file is empty.")
    if size_bytes > MAX_AUDIO_MB * 1024 * 1024:
        raise AppError(413, "FILE_TOO_LARGE", f"Audio exceeds the {MAX_AUDIO_MB} MB import limit.")
    with source_path.open("rb") as handle:
        head = handle.read(64)
    fmt = detect_audio_format(head, filename)
    if not verify_decodable(source_path):
        raise AppError(422, "UNDECODABLE_AUDIO", "The audio file could not be decoded. It may be corrupt.")

    audio_id = new_id()
    ext = _safe_ext(filename)
    base = f"projects/{project.id}/external_audio/{audio_id}"
    storage_key = f"{base}/original{ext}"
    destination = storage.absolute_path(storage_key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # copy unchanged; never modify source or our stored original
    shutil.copyfile(source_path, destination)
    checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
    probe = probe_audio(destination)

    # cached waveform overview (Task 9)
    peaks_key = f"{base}/peaks.json"
    try:
        audio = _decode_mono(destination, sr=8000)
        overview = peaks(audio, 2000)
        storage.absolute_path(peaks_key).write_text(json.dumps({"sample_rate": 8000, "buckets": 2000, "peaks": overview}))
    except Exception:  # noqa: BLE001 - waveform cache is best-effort
        peaks_key = None

    mime = AUDIO_MIME.get(fmt, content_type or "application/octet-stream")
    row = ExternalAudio(
        id=audio_id, project_id=project.id, video_id=video_id, original_filename=Path(filename).name[:255],
        storage_key=storage_key, content_type=mime, format=fmt, size_bytes=size_bytes, checksum_sha256=checksum,
        duration_ms=probe.get("duration_ms"), sample_rate=probe.get("sample_rate"), channels=probe.get("channels"),
        peaks_key=peaks_key, notes=notes, created_at=utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def serialize_external(row: ExternalAudio) -> dict:
    path = storage.absolute_path(row.storage_key)
    return {
        "id": row.id,
        "project_id": row.project_id,
        "video_id": row.video_id,
        "original_filename": row.original_filename,
        "format": row.format,
        "content_type": row.content_type,
        "size_bytes": row.size_bytes,
        "checksum_sha256": row.checksum_sha256,
        "duration_ms": row.duration_ms,
        "sample_rate": row.sample_rate,
        "channels": row.channels,
        "stored_unchanged": path.is_file(),
        "file_url": f"/api/v1/audio-imports/{row.id}/file",
        "waveform_url": f"/api/v1/audio-imports/{row.id}/waveform",
        "preview_url": f"/api/v1/audio-imports/{row.id}/file",
        "has_waveform_overview": bool(row.peaks_key),
        "notes": row.notes,
        "created_at": row.created_at.isoformat(),
    }


def cached_overview(row: ExternalAudio) -> dict | None:
    if not row.peaks_key:
        return None
    path = storage.absolute_path(row.peaks_key)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def import_waveform(db: Session, row: ExternalAudio, start_ms: int, end_ms: int, buckets: int) -> dict:
    path = storage.absolute_path(row.storage_key)
    start_ms = max(0, start_ms)
    end_ms = max(start_ms + 1, end_ms)
    buckets = max(1, min(4000, buckets))
    binary = ffmpeg_binary()
    if binary is None or not path.is_file():
        return {"peaks": [[0.0, 0.0]] * buckets, "available": False, "start_ms": start_ms, "end_ms": end_ms}
    sr = 8000
    result = subprocess.run(
        [binary, "-hide_banner", "-loglevel", "error", "-ss", f"{start_ms / 1000:.3f}", "-t", f"{(end_ms - start_ms) / 1000:.3f}",
         "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"],
        capture_output=True, check=False,
    )
    audio = np.frombuffer(result.stdout, dtype="<f4") if result.returncode == 0 else np.zeros(0, dtype=np.float32)
    return {"peaks": peaks(audio.astype(np.float32), buckets), "available": True, "start_ms": start_ms, "end_ms": end_ms,
            "duration_ms": row.duration_ms}
