import hashlib
import json
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from ..config import settings

_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_STREAM = re.compile(r"Stream #\d+:(\d+)[^\n]*?:\s*(Video|Audio|Subtitle|Data):\s*([A-Za-z0-9_-]+)")
_SIZE = re.compile(r"(\d{2,5})x(\d{2,5})")
_FPS = re.compile(r"([\d.]+)\s+fps")
_BITRATE = re.compile(r"(\d+)\s+kb/s")
_PIXEL_FORMAT = re.compile(r",\s*([a-z0-9_]+)(?:\([^)]*\))?,\s*\d{2,5}x\d{2,5}", re.IGNORECASE)
_COLOR_RANGE = re.compile(r"color_range:(\w+)", re.IGNORECASE)
_COLOR_SPACE = re.compile(r"color_space:(\w+)", re.IGNORECASE)


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def detect_container(head: bytes) -> str | None:
    if len(head) < 12:
        return None
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "avi"
    if head[:4] == b"OggS":
        return "ogg"
    return None


def validate_video_file(head: bytes, content_type: str | None, size_bytes: int) -> str:
    if size_bytes <= 0:
        raise AppError(422, "EMPTY_FILE", "The selected file is empty.")
    if size_bytes > settings.max_upload_bytes:
        raise AppError(
            413,
            "FILE_TOO_LARGE",
            f"Video exceeds the {settings.max_upload_mb} MB upload limit.",
        )
    container = detect_container(head)
    if container is None:
        raise AppError(422, "UNSUPPORTED_VIDEO", "File is not a recognised MP4, WebM, MKV, AVI or Ogg video.")
    if content_type and content_type not in settings.allowed_content_types:
        raise AppError(422, "UNSUPPORTED_MEDIA_TYPE", f"Unsupported content type: {content_type}")
    return container


def ffmpeg_binary() -> str | None:
    if settings.ffprobe_binary and Path(settings.ffprobe_binary).name.lower().startswith("ffmpeg"):
        return settings.ffprobe_binary
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def ffprobe_binary() -> str | None:
    if settings.ffprobe_binary:
        return settings.ffprobe_binary
    return shutil.which("ffprobe")


def ffmpeg_available() -> bool:
    return ffmpeg_binary() is not None


def probe_available() -> bool:
    return ffmpeg_available() or ffprobe_binary() is not None


def _rational_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        result = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return None
    return result if result > 0 else None


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "", "N/A") else None
    except (TypeError, ValueError):
        return None


def _probe_with_ffprobe(binary: str, path: Path) -> dict:
    result = subprocess.run(
        [binary, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        errors="ignore",
        timeout=45,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("ffprobe could not read media metadata")
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if not video:
        raise RuntimeError("ffprobe found no video stream")

    fmt = payload.get("format", {})
    duration_ms = _int_or_none(float(fmt.get("duration") or video.get("duration") or 0) * 1000)
    fps = _rational_float(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    frame_count = _int_or_none(video.get("nb_frames"))
    if frame_count is None and fps and duration_ms:
        frame_count = int(round((duration_ms / 1000) * fps))

    stream_layout = [
        {
            "index": item.get("index"),
            "codec_type": item.get("codec_type"),
            "codec_name": item.get("codec_name"),
            "width": _int_or_none(item.get("width")),
            "height": _int_or_none(item.get("height")),
            "sample_rate": _int_or_none(item.get("sample_rate")),
            "channels": _int_or_none(item.get("channels")),
            "duration_ms": _int_or_none(float(item.get("duration") or 0) * 1000),
        }
        for item in streams
    ]

    return {
        "duration_ms": duration_ms,
        "width": _int_or_none(video.get("width")),
        "height": _int_or_none(video.get("height")),
        "fps": fps,
        "frame_count": frame_count,
        "pixel_format": video.get("pix_fmt"),
        "color_space": video.get("color_space"),
        "color_range": video.get("color_range"),
        "bit_rate": _int_or_none(fmt.get("bit_rate") or video.get("bit_rate")),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name") if audio else None,
        "stream_layout": stream_layout,
        "probe_status": "probed",
        "probe_source": "ffprobe",
        "probe_output": (result.stdout or "")[-4000:],
    }


def _probe_with_ffmpeg(binary: str, path: Path) -> dict:
    result = subprocess.run(
        [binary, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        errors="ignore",
        timeout=45,
        check=False,
    )
    output = result.stderr or ""
    duration_match = _DURATION.search(output)
    duration_ms = None
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration_ms = int((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)

    video_line = next((line for line in output.splitlines() if "Video:" in line), "")
    size_match = _SIZE.search(video_line)
    fps_match = _FPS.search(video_line)
    bitrate_match = _BITRATE.search(video_line)
    pixel_match = _PIXEL_FORMAT.search(video_line)
    color_range = _COLOR_RANGE.search(output)
    color_space = _COLOR_SPACE.search(output)

    video_codec = re.search(r"Video:\s*([A-Za-z0-9_-]+)", video_line)
    audio_line = next((line for line in output.splitlines() if "Audio:" in line), "")
    audio_codec = re.search(r"Audio:\s*([A-Za-z0-9_-]+)", audio_line)
    fps = float(fps_match.group(1)) if fps_match else None
    frame_count = int(round((duration_ms / 1000) * fps)) if duration_ms and fps else None

    stream_layout = []
    for match in _STREAM.finditer(output):
        stream_layout.append(
            {
                "index": int(match.group(1)),
                "codec_type": match.group(2).lower(),
                "codec_name": match.group(3),
            }
        )

    if duration_ms is None and not size_match:
        raise RuntimeError("ffmpeg could not read media metadata")

    return {
        "duration_ms": duration_ms,
        "width": int(size_match.group(1)) if size_match else None,
        "height": int(size_match.group(2)) if size_match else None,
        "fps": fps,
        "frame_count": frame_count,
        "pixel_format": pixel_match.group(1).lower() if pixel_match else None,
        "color_space": color_space.group(1).lower() if color_space else None,
        "color_range": color_range.group(1).lower() if color_range else None,
        "bit_rate": int(bitrate_match.group(1)) * 1000 if bitrate_match else None,
        "video_codec": video_codec.group(1).lower() if video_codec else None,
        "audio_codec": audio_codec.group(1).lower() if audio_codec else None,
        "stream_layout": stream_layout,
        "probe_status": "probed",
        "probe_source": "ffmpeg",
        "probe_output": output[-4000:],
    }


def probe_video(path: Path) -> dict:
    probe = ffprobe_binary()
    if probe:
        return _probe_with_ffprobe(probe, path)
    ffmpeg = ffmpeg_binary()
    if not ffmpeg:
        raise RuntimeError("No ffprobe or ffmpeg binary is available")
    return _probe_with_ffmpeg(ffmpeg, path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_frame_index(probe: dict) -> dict:
    fps = probe.get("fps")
    duration_ms = probe.get("duration_ms") or 0
    frame_count = probe.get("frame_count")
    step_ms = 1000 if duration_ms <= 600000 else 5000
    samples = []
    for timestamp_ms in range(0, duration_ms + 1, step_ms):
        frame = int(round((timestamp_ms / 1000) * fps)) if fps else 0
        samples.append({"t_ms": timestamp_ms, "frame": frame})
        if len(samples) >= 1200:
            break
    if duration_ms and (not samples or samples[-1]["t_ms"] != duration_ms):
        samples.append({"t_ms": duration_ms, "frame": frame_count or 0})
    return {
        "authoritative_clock": "source_video_ms",
        "fps": fps,
        "frame_count": frame_count,
        "duration_ms": duration_ms or None,
        "time_base_ms": round(1000 / fps, 6) if fps else None,
        "frame_at_ms": "floor(ms * fps / 1000)",
        "ms_at_frame": "round(frame * 1000 / fps)",
        "keyframe_policy": "source-derived; no video re-encode",
        "samples": samples,
    }


def generate_thumbnails(path: Path, destination: Path, probe: dict) -> list[dict]:
    binary = ffmpeg_binary()
    if not binary:
        return []
    count = max(0, settings.thumbnail_count)
    if count == 0:
        return []
    destination.mkdir(parents=True, exist_ok=True)
    duration_ms = probe.get("duration_ms") or 0
    thumbnails: list[dict] = []
    for index in range(count):
        fraction = 0.5 if count == 1 else 0.05 + (0.9 * index / (count - 1))
        timestamp_ms = int(duration_ms * fraction) if duration_ms else 0
        output = destination / f"thumb-v1-{index:02d}.jpg"
        result = subprocess.run(
            [
                binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{timestamp_ms / 1000:.3f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=480:-2",
                "-q:v",
                "4",
                "-y",
                str(output),
            ],
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=45,
            check=False,
        )
        if result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
            thumbnails.append(
                {
                    "path": output,
                    "timestamp_ms": timestamp_ms,
                    "width": probe.get("width"),
                    "height": probe.get("height"),
                }
            )
    return thumbnails
