import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import wave
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

import numpy as np

from ..config import settings
from .media import AppError, ffmpeg_binary, ffprobe_binary

STEM_ROLES = ("dialogue", "music", "sfx", "ambience")
STEM_LABELS = {
    "dialogue": "Dialogue / Vocals",
    "music": "Background Music",
    "sfx": "Sound Effects",
    "ambience": "Ambience",
}
MIME_BY_FORMAT = {
    "wav": "audio/wav",
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "aac": "audio/aac",
}
_AUDIO_LINE = re.compile(
    r"Audio:\s*([A-Za-z0-9_-]+)[^\n]*?(\d{2,6})\s*Hz[^\n]*?(mono|stereo|quad|5\.1|7\.1|\d+)",
    re.I,
)
_DURATION_LINE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_CHANNEL_NAMES = {"mono": 1, "stereo": 2, "quad": 4, "5.1": 6, "7.1": 8}
_FRAME = 2048
_HOP = 1024
_BLOCK_SECONDS = 30


class AudioCancelled(Exception):
    pass


def demucs_available() -> bool:
    return importlib.util.find_spec("demucs") is not None


def production_info() -> dict:
    selected = settings.separation_backend.strip().lower()
    if selected == "auto":
        effective = "demucs" if demucs_available() else "remote" if settings.remote_separation_url else "dsp"
    else:
        effective = selected
    return {
        "configured_backend": selected,
        "effective_backend": effective,
        "demucs_available": demucs_available(),
        "demucs_model": settings.demucs_model,
        "demucs_device": settings.demucs_device,
        "remote_configured": bool(settings.remote_separation_url),
        "extraction_formats": sorted(settings.audio_formats),
        "default_extraction_format": settings.audio_extract_format,
        "model_cache_dir": str(settings.effective_model_cache_dir),
        "cpu_fallback": True,
        "browser_gpu_required": False,
    }


def _run(arguments: list[str], timeout: int = 900, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        errors="ignore",
        timeout=timeout,
        check=False,
        env=env,
    )


def probe_audio_stream(path: Path) -> dict:
    probe = ffprobe_binary()
    if probe:
        result = _run(
            [
                probe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,channels,duration",
                "-of",
                "json",
                str(path),
            ],
            timeout=60,
        )
        if result.returncode == 0:
            streams = json.loads(result.stdout or "{}").get("streams", [])
            if streams:
                stream = streams[0]
                return {
                    "codec": stream.get("codec_name"),
                    "sample_rate": int(stream.get("sample_rate") or 0) or None,
                    "channels": int(stream.get("channels") or 0) or None,
                    "duration_ms": int(float(stream.get("duration") or 0) * 1000) or None,
                }
    ffmpeg = ffmpeg_binary()
    if not ffmpeg:
        raise AppError(503, "FFMPEG_UNAVAILABLE", "No ffmpeg binary is available for audio extraction.")
    result = _run([ffmpeg, "-hide_banner", "-i", str(path)], timeout=60)
    output = result.stderr or ""
    match = _AUDIO_LINE.search(output)
    if not match:
        raise AppError(422, "NO_AUDIO_STREAM", "The source video has no readable audio stream.")
    codec, rate, layout = match.groups()
    channels = _CHANNEL_NAMES.get(layout.lower(), int(layout) if layout.isdigit() else 2)
    duration_match = _DURATION_LINE.search(output)
    duration_ms = None
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration_ms = int((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)
    return {
        "codec": codec.lower(),
        "sample_rate": int(rate),
        "channels": channels,
        "duration_ms": duration_ms,
    }


def extract_audio(
    video_path: Path,
    destination: Path,
    output_format: str = "wav",
    audio_info: dict | None = None,
) -> Path:
    output_format = output_format.strip().lower()
    if output_format not in settings.audio_formats:
        raise AppError(422, "UNSUPPORTED_AUDIO_FORMAT", f"Unsupported audio format: {output_format}")
    ffmpeg = ffmpeg_binary()
    if not ffmpeg:
        raise AppError(503, "FFMPEG_UNAVAILABLE", "No ffmpeg binary is available for audio extraction.")
    info = audio_info or probe_audio_stream(video_path)
    codec = {
        "wav": "pcm_s16le",
        "flac": "flac",
        "mp3": "libmp3lame",
        "aac": "aac",
    }[output_format]
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f"source-audio.{output_format}"
    arguments = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-map",
        "0:a:0",
        "-vn",
    ]
    if info.get("sample_rate"):
        arguments += ["-ar", str(info["sample_rate"])]
    if info.get("channels"):
        arguments += ["-ac", str(info["channels"])]
    arguments += ["-c:a", codec]
    if output_format == "mp3":
        arguments += ["-q:a", "2"]
    if output_format == "aac":
        arguments += ["-b:a", "192k"]
    arguments += ["-y", str(output)]
    result = _run(arguments, timeout=1800)
    if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        raise AppError(500, "EXTRACTION_FAILED", (result.stderr or "Could not extract audio")[-500:])
    return output


def _wav_info(path: Path) -> tuple[int, int, int, int]:
    try:
        with wave.open(str(path), "rb") as handle:
            return (
                handle.getframerate(),
                handle.getnchannels(),
                handle.getsampwidth(),
                handle.getnframes(),
            )
    except (wave.Error, OSError) as error:
        raise AppError(422, "INVALID_AUDIO", f"Only uncompressed PCM WAV files can be processed: {error}") from error


def _to_pcm16(path: Path, destination: Path | None = None) -> Path:
    ffmpeg = ffmpeg_binary()
    if not ffmpeg:
        raise AppError(503, "FFMPEG_UNAVAILABLE", "No ffmpeg binary is available for audio conversion.")
    target_dir = destination or path.parent
    output = target_dir / f"{path.stem}-pcm16.wav"
    result = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-c:a",
            "pcm_s16le",
            "-y",
            str(output),
        ],
        timeout=1800,
    )
    if result.returncode != 0 or not output.is_file():
        raise AppError(500, "AUDIO_CONVERSION_FAILED", (result.stderr or "Could not decode audio")[-500:])
    return output


def ensure_pcm_wav(path: Path, destination: Path) -> Path:
    if path.suffix.lower() == ".wav" and path.is_file():
        try:
            _, _, width, _ = _wav_info(path)
            if width == 2:
                return path
        except AppError:
            pass
    return _to_pcm16(path, destination)


def iter_wav_mono(path: Path, block_seconds: int = _BLOCK_SECONDS):
    rate, channels, width, _ = _wav_info(path)
    source = path
    if width != 2:
        source = _to_pcm16(path)
        rate, channels, width, _ = _wav_info(source)
    block_frames = max(1, rate * block_seconds)
    with wave.open(str(source), "rb") as handle:
        try:
            while True:
                raw = handle.readframes(block_frames)
                if not raw:
                    break
                data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                if channels > 1:
                    data = data.reshape(-1, channels).mean(axis=1)
                yield data, rate
        finally:
            handle.close()


def _WavWriter(path: Path, rate: int):
    class Writer:
        def __init__(self) -> None:
            self.path = path
            self.rate = rate
            self.handle = None

        def __enter__(self):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = wave.open(str(self.path), "wb")
            self.handle.setnchannels(1)
            self.handle.setsampwidth(2)
            self.handle.setframerate(self.rate)
            return self

        def write(self, samples: np.ndarray) -> None:
            if samples.size == 0 or self.handle is None:
                return
            pcm = np.round(np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
            self.handle.writeframesraw(pcm.tobytes())

        def __exit__(self, *_args) -> None:
            if self.handle:
                self.handle.close()

    return Writer()


def audio_metrics(path: Path) -> dict:
    rate, channels, width, frames = _wav_info(path)
    energy = 0.0
    samples = 0
    peak = 0.0
    block_rms: list[float] = []
    for block, _ in iter_wav_mono(path):
        if block.size == 0:
            continue
        block_energy = float(np.sum(block.astype(np.float64) ** 2))
        energy += block_energy
        samples += int(block.size)
        peak = max(peak, float(np.max(np.abs(block))))
        block_rms.append(float(np.sqrt(block_energy / block.size)))
    rms = float(np.sqrt(energy / max(1, samples)))
    return {
        "duration_ms": int(round(frames / rate * 1000)) if rate else None,
        "sample_rate": rate,
        "channels": channels,
        "sample_width": width,
        "rms": rms,
        "peak": peak,
        "non_silent": rms > 1e-5,
        "block_rms_mean": float(np.mean(block_rms)) if block_rms else 0.0,
    }


def decode_check(path: Path) -> bool:
    ffmpeg = ffmpeg_binary()
    if not ffmpeg:
        return False
    result = _run([ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"], timeout=1800)
    return result.returncode == 0


def validate_audio_file(
    path: Path,
    expected_duration_ms: int | None = None,
    expected_rate: int | None = None,
    expected_channels: int | None = None,
    allow_silent: bool = False,
) -> dict:
    issues: list[str] = []
    if not path.is_file() or path.stat().st_size == 0:
        raise AppError(500, "AUDIO_MISSING", "Generated audio file is missing or empty.")
    metrics = audio_metrics(path)
    decode_ok = decode_check(path)
    if not decode_ok:
        issues.append("ffmpeg could not decode the audio file")
    if expected_duration_ms and metrics["duration_ms"] is not None:
        tolerance = max(250, int(expected_duration_ms * 0.02))
        if abs(metrics["duration_ms"] - expected_duration_ms) > tolerance:
            issues.append("duration differs from the source audio")
    if expected_rate and metrics["sample_rate"] != expected_rate:
        issues.append("sample rate differs from the source audio")
    if expected_channels and metrics["channels"] != expected_channels:
        issues.append("channel count differs from the source audio")
    if not allow_silent and not metrics["non_silent"]:
        issues.append("audio is silent")
    return {
        "ok": decode_ok and not issues,
        "decode_ok": decode_ok,
        "issues": issues,
        **metrics,
    }


def compare_stems(source_wav: Path, stem_files: dict[str, Path]) -> dict:
    source_metrics = audio_metrics(source_wav)
    stem_metrics = {role: audio_metrics(path) for role, path in stem_files.items() if path.is_file()}
    stem_energies = {role: 0.0 for role in stem_files}
    source_energy = 0.0
    reconstruction_energy = 0.0
    cross_energy = 0.0
    aligned = True

    generators = {
        role: iter_wav_mono(path) for role, path in stem_files.items() if path.is_file()
    }
    source_generator = iter_wav_mono(source_wav)
    try:
        for source_block, _ in source_generator:
            reconstruction = np.zeros_like(source_block)
            for role, generator in generators.items():
                try:
                    block, _ = next(generator)
                except StopIteration:
                    aligned = False
                    block = np.zeros_like(source_block)
                if len(block) < len(source_block):
                    block = np.pad(block, (0, len(source_block) - len(block)))
                block = block[: len(source_block)].astype(np.float64)
                stem_energies[role] += float(np.sum(block**2))
                reconstruction += block
            source_block = source_block.astype(np.float64)
            source_energy += float(np.sum(source_block**2))
            reconstruction_energy += float(np.sum(reconstruction**2))
            cross_energy += float(np.sum(source_block * reconstruction))
        for generator in generators.values():
            try:
                next(generator)
                aligned = False
            except StopIteration:
                pass
    finally:
        source_generator.close()
        for generator in generators.values():
            generator.close()

    correlation = None
    if source_energy > 1e-12 and reconstruction_energy > 1e-12:
        correlation = float(cross_energy / np.sqrt(source_energy * reconstruction_energy))
    reconstruction_ratio = (
        float(np.sqrt(reconstruction_energy / source_energy)) if source_energy > 1e-12 else None
    )

    stems: dict[str, dict] = {}
    issues: list[str] = []
    for role, metrics in stem_metrics.items():
        ratio = float(np.sqrt(stem_energies[role] / source_energy)) if source_energy > 1e-12 else None
        duration_delta = None
        if metrics["duration_ms"] is not None and source_metrics["duration_ms"] is not None:
            duration_delta = metrics["duration_ms"] - source_metrics["duration_ms"]
        stems[role] = {
            "duration_ms": metrics["duration_ms"],
            "duration_delta_ms": duration_delta,
            "sample_rate": metrics["sample_rate"],
            "channels": metrics["channels"],
            "rms": metrics["rms"],
            "energy_ratio": ratio,
            "non_silent": metrics["non_silent"],
        }
        if duration_delta is not None and abs(duration_delta) > max(250, int(source_metrics["duration_ms"] * 0.02)):
            issues.append(f"{role} duration differs from the source audio")
        if metrics["sample_rate"] != source_metrics["sample_rate"]:
            issues.append(f"{role} sample rate differs from the source audio")

    if not aligned:
        issues.append("one or more stems did not align with the full source duration")
    if correlation is not None and correlation < 0.35:
        issues.append("summed stems correlate weakly with the original audio")

    return {
        "source_duration_ms": source_metrics["duration_ms"],
        "source_sample_rate": source_metrics["sample_rate"],
        "source_channels": source_metrics["channels"],
        "stems": stems,
        "reconstruction": {
            "correlation": correlation,
            "rms_ratio": reconstruction_ratio,
        },
        "aligned": aligned,
        "issues": issues,
    }


def _frame_rms_stream(path: Path) -> list[float]:
    values: list[float] = []
    for block, _ in iter_wav_mono(path):
        if block.size < _FRAME:
            if block.size:
                values.append(float(np.sqrt(np.mean(block**2))))
            continue
        for start in range(0, len(block) - _FRAME + 1, _HOP):
            frame = block[start : start + _FRAME]
            values.append(float(np.sqrt(np.mean(frame**2))))
    return values


def _masks(freqs: np.ndarray, power: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean_power = float(np.mean(power)) + 1e-10
    flatness = min(1.0, float(np.exp(np.mean(np.log(power + 1e-10))) / mean_power))
    voice_band = (freqs >= 180) & (freqs <= 3600)
    music_band = (freqs < 250) | (freqs > 4500)
    voice_weight = float(np.clip((0.85 - flatness) / 0.6, 0.2, 1.0))
    dialogue = voice_band.astype(np.float32) * voice_weight
    music = music_band.astype(np.float32) * 0.95
    residual = np.clip(1.0 - dialogue - music, 0.05, 1.0).astype(np.float32)
    return dialogue, music, residual


def dsp_separate(
    source_wav: Path,
    destination: Path,
    cancel_check=None,
    progress=None,
) -> dict:
    work = ensure_pcm_wav(source_wav, destination)
    rate, _, _, total_frames = _wav_info(work)
    rms_frames = _frame_rms_stream(work)
    median = float(np.median(rms_frames)) if rms_frames else 0.0
    threshold = median * 1.6 if median > 1e-5 else 0.0
    window = np.hanning(_FRAME).astype(np.float32)
    freqs = np.fft.rfftfreq(_FRAME, 1.0 / rate)

    outputs = destination / "dsp"
    writers = {role: _WavWriter(outputs / f"{role}.wav", rate) for role in STEM_ROLES}
    frame_index = 0
    block_frames = max(1, rate * _BLOCK_SECONDS)
    total_blocks = max(1, int(np.ceil(total_frames / block_frames)))

    def process(data: np.ndarray, limit: int) -> None:
        nonlocal frame_index
        if limit <= 0:
            return
        buffers = {role: np.zeros(limit, dtype=np.float32) for role in STEM_ROLES}
        normalization = np.zeros(limit, dtype=np.float32)
        for start in range(0, limit, _HOP):
            frame = data[start : start + _FRAME]
            if frame.size < _FRAME:
                frame = np.pad(frame, (0, _FRAME - frame.size))
            spectrum = np.fft.rfft(frame * window)
            power = np.abs(spectrum) ** 2
            dialogue_mask, music_mask, residual_mask = _masks(freqs, power)
            rms = rms_frames[frame_index] if frame_index < len(rms_frames) else median
            transient = bool(threshold and rms > threshold)
            assignments = {
                "dialogue": dialogue_mask,
                "music": music_mask,
                "sfx": residual_mask if transient else np.zeros_like(residual_mask),
                "ambience": np.zeros_like(residual_mask) if transient else residual_mask,
            }
            for role, mask in assignments.items():
                time_domain = np.fft.irfft(spectrum * mask, n=_FRAME) * window
                end = min(start + _FRAME, limit)
                buffers[role][start:end] += time_domain[: end - start]
            norm_end = min(start + _FRAME, limit)
            normalization[start:norm_end] += window[: norm_end - start] ** 2
            frame_index += 1
        inverse = np.maximum(normalization, 1e-6)
        for role, writer in writers.items():
            writer.write(buffers[role] / inverse)

    try:
        for writer in writers.values():
            writer.__enter__()
        carry = np.zeros(0, dtype=np.float32)
        for block_index, (block, _) in enumerate(iter_wav_mono(work)):
            if cancel_check:
                cancel_check()
            data = np.concatenate((carry, block)) if carry.size else block
            limit = max(0, len(data) - _FRAME)
            process(data, limit)
            carry = data[limit:] if limit else data
            if progress:
                progress(min(1.0, (block_index + 1) / total_blocks))
        if carry.size:
            if cancel_check:
                cancel_check()
            padded = np.pad(carry, (0, max(0, _FRAME - len(carry))))
            process(padded, len(carry))
            if progress:
                progress(1.0)
    finally:
        for writer in writers.values():
            writer.__exit__()

    return {
        "backend": "dsp",
        "model": "spectral-band-envelope-v1",
        "confidence": "approximate",
        "files": {role: writers[role].path for role in STEM_ROLES},
        "limitations": [
            "Dialogue is a band-limited estimate, not validated clean speech.",
            "Music is a spectral estimate; tonal overlap with dialogue remains.",
            "SFX and ambience use an RMS envelope heuristic, not event classification.",
        ],
    }


def _envelope_split(source_wav: Path, destination: Path) -> dict[str, Path]:
    work = ensure_pcm_wav(source_wav, destination)
    rate, _, _, _ = _wav_info(work)
    frame_len = max(1, int(rate * 0.02))
    rms_values: list[float] = []
    for block, _ in iter_wav_mono(work):
        for start in range(0, len(block), frame_len):
            frame = block[start : start + frame_len]
            if frame.size:
                rms_values.append(float(np.sqrt(np.mean(frame**2))))
    median = float(np.median(rms_values)) if rms_values else 0.0
    threshold = median * 1.55 if median > 1e-5 else 0.0
    paths = {
        "sfx": destination / "sfx.wav",
        "ambience": destination / "ambience.wav",
    }
    writers = {role: _WavWriter(path, rate) for role, path in paths.items()}
    try:
        for writer in writers.values():
            writer.__enter__()
        for block, _ in iter_wav_mono(work):
            for start in range(0, len(block), frame_len):
                frame = block[start : start + frame_len]
                if frame.size == 0:
                    continue
                rms = float(np.sqrt(np.mean(frame**2)))
                writers["sfx" if threshold and rms > threshold else "ambience"].write(frame)
    finally:
        for writer in writers.values():
            writer.__exit__()
    return paths


def _mix_drums_bass(drums: Path, bass: Path, destination: Path) -> Path:
    ffmpeg = ffmpeg_binary()
    if not ffmpeg:
        raise AppError(503, "FFMPEG_UNAVAILABLE", "No ffmpeg binary is available for stem mixing.")
    output = destination / "music.wav"
    result = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(drums),
            "-i",
            str(bass),
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0",
            "-c:a",
            "pcm_s16le",
            "-y",
            str(output),
        ],
        timeout=1800,
    )
    if result.returncode != 0 or not output.is_file():
        raise AppError(500, "STEM_MIX_FAILED", (result.stderr or "Could not mix music stems")[-500:])
    return output


def demucs_separate(source_wav: Path, destination: Path, cancel_check=None) -> dict:
    if cancel_check:
        cancel_check()
    model = settings.demucs_model
    output_root = destination / "demucs"
    output_root.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment["DEMUCS_HOME"] = str(settings.effective_model_cache_dir)
    result = _run(
        [
            sys.executable,
            "-m",
            "demucs",
            "-n",
            model,
            "--out",
            str(output_root),
            "--device",
            settings.demucs_device,
            str(source_wav),
        ],
        timeout=7200,
        env=environment,
    )
    if cancel_check:
        cancel_check()
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "Demucs failed")[-800:])
    base = output_root / model / source_wav.stem

    def find(stem: str) -> Path:
        direct = base / f"{stem}.wav"
        if direct.is_file():
            return direct
        matches = list((output_root / model).rglob(f"{stem}.wav"))
        if not matches:
            raise RuntimeError(f"Demucs did not produce the {stem} stem")
        return matches[0]

    vocals = ensure_pcm_wav(find("vocals"), destination)
    other = ensure_pcm_wav(find("other"), destination)
    music = _mix_drums_bass(find("drums"), find("bass"), destination)
    residual = _envelope_split(other, destination)
    return {
        "backend": "demucs",
        "model": model,
        "confidence": "candidate_not_validated",
        "files": {
            "dialogue": vocals,
            "music": music,
            "sfx": residual["sfx"],
            "ambience": residual["ambience"],
        },
        "limitations": [
            "Demucs vocals are a dialogue candidate and have not been validated as clean speech.",
            "Drums and bass are mixed as an approximate music bed.",
            "SFX and ambience are split from the residual stem by an RMS envelope heuristic.",
        ],
    }


def remote_separate(source_wav: Path, destination: Path, cancel_check=None) -> dict:
    url = settings.remote_separation_url
    if not url:
        raise RuntimeError("Remote separation backend is not configured")
    if cancel_check:
        cancel_check()
    boundary = uuid.uuid4().hex
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="source.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()
    payload_size = len(prefix) + source_wav.stat().st_size + len(suffix)
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection_type = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
    connection = connection_type(parsed.hostname, port, timeout=7200)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(payload_size),
    }
    if settings.remote_separation_token:
        headers["Authorization"] = f"Bearer {settings.remote_separation_token}"
    try:
        connection.putrequest("POST", path)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        connection.send(prefix)
        with source_wav.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                if cancel_check:
                    cancel_check()
                connection.send(chunk)
        connection.send(suffix)
        response = connection.getresponse()
        document = json.loads(response.read().decode("utf-8"))
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Remote backend returned {response.status}: {document}")
    finally:
        connection.close()

    if cancel_check:
        cancel_check()
    remote_files = document.get("stems") or {}
    missing = [role for role in STEM_ROLES if role not in remote_files]
    if missing:
        raise RuntimeError(f"Remote backend omitted stems: {', '.join(missing)}")
    files: dict[str, Path] = {}
    for role, remote_url in remote_files.items():
        if cancel_check:
            cancel_check()
        target = destination / f"{role}-remote.wav"
        with urlopen(remote_url, timeout=7200) as response, target.open("wb") as output:
            shutil.copyfileobj(response, output)
        files[role] = ensure_pcm_wav(target, destination)
    return {
        "backend": "remote",
        "model": document.get("model", "remote-unspecified"),
        "confidence": document.get("confidence", "remote_unvalidated"),
        "files": files,
        "limitations": [
            "Remote stems are only locally checked for decode, duration and sample rate.",
            "Remote GPU processing is optional and may incur cost outside this application.",
        ],
    }


def separate_audio(
    source_wav: Path,
    destination: Path,
    cancel_check=None,
    progress=None,
) -> dict:
    selected = settings.separation_backend.strip().lower()
    notes: list[str] = []
    if selected == "auto":
        if demucs_available():
            selected = "demucs"
        elif settings.remote_separation_url:
            selected = "remote"
        else:
            selected = "dsp"
            notes.append("Demucs is not installed; the built-in DSP estimator was used.")
    if selected == "demucs" and not demucs_available():
        raise RuntimeError("Demucs backend selected but the demucs package is not installed")
    if selected == "remote" and not settings.remote_separation_url:
        raise RuntimeError("Remote backend selected but no remote separation URL is configured")
    try:
        if selected == "demucs":
            result = demucs_separate(source_wav, destination, cancel_check)
        elif selected == "remote":
            result = remote_separate(source_wav, destination, cancel_check)
        elif selected == "dsp":
            result = dsp_separate(source_wav, destination, cancel_check, progress)
        else:
            raise RuntimeError(f"Unknown separation backend: {selected}")
    except AudioCancelled:
        raise
    except Exception as error:
        if selected != "dsp":
            notes.append(f"{selected} backend failed ({str(error)[:300]}); used DSP estimator instead.")
            result = dsp_separate(source_wav, destination, cancel_check, progress)
            selected = "dsp"
        else:
            raise
    result["backend"] = selected
    result["notes"] = notes
    result.setdefault("limitations", [])
    result["limitations"].extend(notes)
    return result
