"""Phase 16 — final video assembly helpers.

* ``compose_lipsync_video`` splices approved/current Phase 13 lip-sync clips into
  the original picture (frame-accurate, streamed: constant memory). Only frames
  inside lip-sync windows change; everything else is the original decoded frame.
  The result must be re-encoded (H.264, CRF 18) because changed frames cannot be
  stream-copied. Without usable lip-sync results the original stream is copied.
* ``iso639_2`` maps app language codes to the 3-letter tags MKV/MP4 expect.
* ``probe_streams`` / ``validate_export`` check what was written.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Video
from .storage import storage
from .tts_engine import ffmpeg_binary

ALLOWED_CONTAINERS = {"mkv", "mp4", "mov"}
ISO639_2 = {"en": "eng", "ur": "urd", "hi": "hin", "original": "eng", "original_english": "eng"}
LIPSYNC_CRF = 18


def iso639_2(code: str | None) -> str:
    if not code:
        return "und"
    code = code.lower()
    if code in ISO639_2:
        return ISO639_2[code]
    return code if re.fullmatch(r"[a-z]{3}", code) else "und"


def ffprobe_binary() -> str:
    exe = ffmpeg_binary()
    candidate = exe.replace("ffmpeg", "ffprobe")
    return candidate if Path(candidate).is_file() else "ffprobe"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, check=False, **kwargs)


def probe_streams(path: Path) -> dict:
    """Stream list via ffmpeg's own banner (ffprobe is not shipped with imageio-ffmpeg)."""
    result = _run([ffmpeg_binary(), "-hide_banner", "-i", str(path)])
    banner = (result.stderr or b"").decode("utf-8", "replace")
    streams = []
    for match in re.finditer(r"Stream #0:(\d+)(?:\[0x[0-9a-f]+\])?(?:\(([a-z]{3})\))?: (Video|Audio): ([^\n]+)", banner):
        streams.append({"index": int(match.group(1)), "language": match.group(2), "type": match.group(3).lower(),
                        "codec": match.group(4).split()[0].rstrip(","), "detail": match.group(4)[:160]})
    # Per audio stream name: MKV stores "title"; MP4/MOV store it as the track's handler name.
    titles = []
    for block in re.split(r"\n\s+Stream #0:", banner)[1:]:
        head = block.split("\n", 1)[0]
        if ": Audio:" not in head:
            continue
        found = re.search(r"\n\s+title\s+:\s+([^\r\n]+)", block) or re.search(r"\n\s+handler_name\s+:\s+([^\r\n]+)", block)
        name = found.group(1).strip() if found else ""
        if name and name not in {"SoundHandler", "Sound Media Handler"}:
            titles.append(name)
    duration = re.search(r"Duration: (\d+):(\d+):([\d.]+)", banner)
    seconds = int(duration.group(1)) * 3600 + int(duration.group(2)) * 60 + float(duration.group(3)) if duration else None
    default_audio = [s["index"] for s in streams if s["type"] == "audio" and "(default)" in s["detail"]]
    return {"streams": streams, "titles": titles, "duration_s": seconds, "default_audio": default_audio}


def decode_check(path: Path, seconds: float | None = None) -> bool:
    """Decode the file (or its first ``seconds``) to prove it plays."""
    cmd = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-xerror", "-i", str(path)]
    if seconds:
        cmd += ["-t", f"{seconds:.3f}"]
    cmd += ["-f", "null", "-"]
    return _run(cmd).returncode == 0


@dataclass
class LipSyncComposition:
    path: Path | None
    clips: int
    skipped: list[dict]
    reencoded: bool


def _video_geometry(path: Path) -> tuple[int, int, float, int]:
    banner = (_run([ffmpeg_binary(), "-hide_banner", "-i", str(path)]).stderr or b"").decode("utf-8", "replace")
    match = re.search(r"Video: .*?, (\d{2,5})x(\d{2,5})[, \[]", banner)
    fps_match = re.search(r"([\d.]+) fps", banner)
    if not match:
        raise RuntimeError("Could not read the video geometry.")
    width, height = int(match.group(1)), int(match.group(2))
    fps = float(fps_match.group(1)) if fps_match else 25.0
    count = _run([ffmpeg_binary(), "-hide_banner", "-i", str(path), "-map", "0:v:0", "-c", "copy", "-f", "null", "-"])
    frames = re.findall(r"frame=\s*(\d+)", (count.stderr or b"").decode("utf-8", "replace"))
    return width, height, fps, int(frames[-1]) if frames else 0


def usable_lipsync_results(db: Session, video: Video, language: str) -> tuple[list, list[dict]]:
    from ..models_lipsync import LipSyncResult

    rows = list(db.scalars(select(LipSyncResult).where(
        LipSyncResult.video_id == video.id, LipSyncResult.language == language, LipSyncResult.is_current.is_(True))))
    usable, skipped = [], []
    for row in rows:
        reason = None
        if row.status not in {"generated", "ok", "completed", "ready"}:
            reason = f"status {row.status}"
        elif row.review == "rejected":
            reason = "rejected in review"
        elif getattr(row, "needs_regeneration", False):
            reason = "dub changed; needs regeneration"
        elif not row.after_key or row.start_frame is None or not row.frame_count:
            reason = "no output clip"
        elif not storage.absolute_path(row.after_key).is_file():
            reason = "output file missing"
        if reason:
            skipped.append({"result_id": row.id, "segment_id": row.segment_id, "reason": reason})
        else:
            usable.append(row)
    usable.sort(key=lambda r: r.start_frame)
    # Overlapping windows: keep the earlier clip's frames, trim the later one.
    return usable, skipped


def compose_lipsync_video(db: Session, video: Video, language: str, out_path: Path, check_active=None) -> LipSyncComposition:
    """Original picture with every usable lip-sync window spliced in (video only)."""
    rows, skipped = usable_lipsync_results(db, video, language)
    if not rows:
        return LipSyncComposition(None, 0, skipped, False)
    source = storage.absolute_path(video.storage_key)
    width, height, fps, total_frames = _video_geometry(source)
    frame_bytes = width * height * 3
    decoder = subprocess.Popen(
        [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-i", str(source), "-map", "0:v:0",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], stdout=subprocess.PIPE)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    encoder = subprocess.Popen(
        [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{width}x{height}", "-r", f"{fps:.6f}", "-i", "pipe:0", "-c:v", "libx264", "-preset", "medium",
         "-crf", str(LIPSYNC_CRF), "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_path)],
        stdin=subprocess.PIPE)
    used = 0
    try:
        index = 0
        queue = list(rows)
        clip_proc = None
        clip_end = -1
        while True:
            if check_active and index % 250 == 0:
                check_active()
            frame = decoder.stdout.read(frame_bytes)  # type: ignore[union-attr]
            if len(frame) < frame_bytes:
                break
            if clip_proc is None and queue and queue[0].start_frame <= index:
                row = queue.pop(0)
                if row.start_frame + row.frame_count <= index:
                    skipped.append({"result_id": row.id, "segment_id": row.segment_id, "reason": "overlaps an earlier lip-sync window"})
                else:
                    skip = index - row.start_frame
                    clip_proc = subprocess.Popen(
                        [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-i", str(storage.absolute_path(row.after_key)),
                         "-map", "0:v:0", "-vf", f"scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                        stdout=subprocess.PIPE)
                    for _ in range(skip):
                        clip_proc.stdout.read(frame_bytes)  # type: ignore[union-attr]
                    clip_end = row.start_frame + row.frame_count
                    used += 1
            if clip_proc is not None:
                replacement = clip_proc.stdout.read(frame_bytes)  # type: ignore[union-attr]
                if len(replacement) == frame_bytes:
                    frame = replacement
                if index + 1 >= clip_end:
                    clip_proc.stdout.close()  # type: ignore[union-attr]
                    clip_proc.wait()
                    clip_proc = None
            encoder.stdin.write(frame)  # type: ignore[union-attr]
            index += 1
        if clip_proc is not None:
            clip_proc.kill()
    finally:
        try:
            encoder.stdin.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        encoder.wait()
        decoder.kill()
        decoder.wait()
    if encoder.returncode != 0 or not out_path.is_file():
        raise RuntimeError("Encoding the lip-synced picture failed.")
    if total_frames and abs(index - total_frames) > 1:
        raise RuntimeError(f"Lip-sync composition wrote {index} of {total_frames} frames.")
    return LipSyncComposition(out_path, used, skipped, True)
