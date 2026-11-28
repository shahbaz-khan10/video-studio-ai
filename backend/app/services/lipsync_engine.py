"""Phase 13 — lip-sync generation and sync scoring.

Engines
-------
wav2lip   Local Wav2Lip GAN (36 M params, 96×96 mouth region) — runs on CPU in
          this process. Research / non-commercial licence.
remote    Self-hosted GPU worker (e.g. LatentSync 1.5 or MuseTalk in their own
          environment) behind ``POST {LIPSYNC_REMOTE_URL}/lipsync``.

Only the selected face is modified, and only its mouth/jaw region (feathered
mask); everything else in the frame is the original picture. Frames are
streamed in batches so long or high-resolution lines do not fill memory.
"""

from __future__ import annotations

import gc
import io
import json
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pydantic_settings import BaseSettings, SettingsConfigDict
from scipy.signal import get_window, lfilter

from ..config import PROJECT_ROOT, settings
from .face_tracking import Track, decode_frames, frame_rate_string
from .tts_engine import ffmpeg_binary

ActiveCheck = Callable[[], None]


class LipSyncSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LIPSYNC_", env_file=str(PROJECT_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # auto | wav2lip | remote | none
    backend: str = "auto"
    weights_dir: Path | None = None
    wav2lip_checkpoint: str = "wav2lip_gan.pth"
    syncnet_checkpoint: str = "lipsync_expert.pth"
    face_detector: str = "face_detection_yunet_2023mar.onnx"
    device: str = "auto"
    batch_size: int = 32
    crf: int = 16
    preset: str = "medium"
    max_segment_s: float = 60.0
    window_pad_ms: int = 200
    remote_url: str | None = None
    remote_token: str | None = None
    remote_model: str = "latentsync-1.5"
    remote_timeout_s: float = 1800.0

    @property
    def effective_weights_dir(self) -> Path:
        return self.weights_dir or (settings.effective_model_cache_dir / "lipsync")


lipsync_settings = LipSyncSettings()


class LipSyncUnavailable(Exception):
    pass


MODELS_EVALUATED = [
    {
        "name": "LatentSync 1.5 / 1.6 (ByteDance)",
        "license": "OpenRAIL++ (commercial use allowed with use-based restrictions)",
        "weights": "huggingface.co/ByteDance/LatentSync-1.5 (≈5 GB needed of a 9.8 GB repo) + stabilityai/sd-vae-ft-mse",
        "vram": "≈8 GB (1.5), ≈18 GB (1.6) per the project's README",
        "resolution": "256×256 face region, latent diffusion (20 steps)",
        "status": "Supported through the remote GPU worker (workers/lipsync_worker.py). Not run here: the app's PyTorch is CPU-only and the official requirements (decord, CUDA PyTorch) target Linux/Python 3.10.",
    },
    {
        "name": "MuseTalk 1.5 (Tencent Music)",
        "license": "MIT (code and weights; bundled sub-models carry their own licences)",
        "weights": "huggingface.co/TMElyralab/MuseTalk (6.8 GB repo) + sd-vae, whisper, dwpose, face-parsing",
        "vram": "≈4–6 GB fp16; real-time on a V100 per authors",
        "resolution": "256×256 face region, single-step latent inpainting",
        "status": "Supported through the remote GPU worker contract. Not run here: depends on mmcv/mmpose builds that are not available for this Windows/Python 3.12 setup.",
    },
    {
        "name": "Wav2Lip GAN (Prajwal et al., 2020)",
        "license": "Research / non-commercial only (trained on LRS2)",
        "weights": "Official links are on the authors' SharePoint; this install used the huggingface.co/camenduru/Wav2Lip mirror, verified by loading every tensor with strict=True",
        "vram": "≈1.5 GB on GPU; runs on CPU (≈30 frames/s of 96×96 inference on this 6-core CPU)",
        "resolution": "96×96 face, mouth region blended back — visibly softer than the rest of the face on large faces",
        "status": "Integrated and tested locally.",
    },
    {
        "name": "SadTalker, Hallo, LivePortrait",
        "license": "MIT / MIT / MIT",
        "weights": "huggingface.co",
        "vram": "6–12 GB",
        "resolution": "—",
        "status": "Rejected: they animate a single portrait image, not an existing film shot with head motion, so they would replace the actor's performance.",
    },
]

_lock = threading.RLock()
_loaded: dict = {"generator": None, "syncnet": None, "device": None}


def _weights(name: str) -> Path:
    return lipsync_settings.effective_weights_dir / name


def wav2lip_available() -> bool:
    return _weights(lipsync_settings.wav2lip_checkpoint).is_file() and _weights(lipsync_settings.face_detector).is_file()


def syncnet_available() -> bool:
    return _weights(lipsync_settings.syncnet_checkpoint).is_file()


def effective_backend() -> str:
    requested = (lipsync_settings.backend or "auto").lower()
    if requested in {"wav2lip", "remote", "none"}:
        return requested
    if lipsync_settings.remote_url:
        return "remote"
    return "wav2lip" if wav2lip_available() else "none"


def device() -> str:
    requested = (lipsync_settings.device or "auto").lower()
    if requested in {"cpu", "cuda"}:
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001
        return "cpu"


def engine_info() -> dict:
    gpu = None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=5)
        gpu = out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        gpu = None
    return {
        "backend": effective_backend(),
        "device": device(),
        "wav2lip_available": wav2lip_available(),
        "syncnet_available": syncnet_available(),
        "weights_dir": str(lipsync_settings.effective_weights_dir),
        "remote_configured": bool(lipsync_settings.remote_url),
        "remote_model": lipsync_settings.remote_model if lipsync_settings.remote_url else None,
        "gpu_detected": gpu,
        "models_evaluated": MODELS_EVALUATED,
        "disclosure": "Lip movements are AI-generated and approximate; review every result before use.",
    }


def unload() -> None:
    with _lock:
        _loaded.update({"generator": None, "syncnet": None, "device": None})
        gc.collect()


def _release_other_models() -> None:
    for name in ("tts_engine", "translation_engine"):
        try:
            module = __import__(f"app.services.{name}", fromlist=["unload"])
            module.unload()
        except Exception:  # noqa: BLE001
            pass
    try:
        from . import translation_engine

        translation_engine.release_other_models()
    except Exception:  # noqa: BLE001
        pass


def load_models(need_generator: bool = True, need_syncnet: bool = True):
    from .lipsync_models import SyncNet, Wav2Lip, load_checkpoint

    with _lock:
        dev = device()
        if need_generator and _loaded["generator"] is None:
            if not wav2lip_available():
                raise LipSyncUnavailable(
                    f"Wav2Lip weights not found in {lipsync_settings.effective_weights_dir} "
                    f"({lipsync_settings.wav2lip_checkpoint}, {lipsync_settings.face_detector})."
                )
            _release_other_models()
            _loaded["generator"] = load_checkpoint(Wav2Lip(), str(_weights(lipsync_settings.wav2lip_checkpoint))).to(dev)
        if need_syncnet and _loaded["syncnet"] is None and syncnet_available():
            _loaded["syncnet"] = load_checkpoint(SyncNet(), str(_weights(lipsync_settings.syncnet_checkpoint))).to(dev)
        _loaded["device"] = dev
        return _loaded["generator"], _loaded["syncnet"], dev


# ---------------------------------------------------------------------------
# Audio features (Wav2Lip hyper-parameters, numpy re-implementation)
# ---------------------------------------------------------------------------

SR = 16000
N_FFT, HOP, WIN, N_MELS, FMIN, FMAX = 800, 200, 800, 80, 55, 7600
_MEL_BASIS: np.ndarray | None = None


def _hz_to_mel(f):
    f = np.asanyarray(f, dtype=np.float64)
    f_sp = 200.0 / 3
    mels = f / f_sp
    min_log_hz, min_log_mel, logstep = 1000.0, 1000.0 / f_sp, np.log(6.4) / 27.0
    return np.where(f >= min_log_hz, min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep, mels)


def _mel_to_hz(m):
    m = np.asanyarray(m, dtype=np.float64)
    f_sp = 200.0 / 3
    freqs = f_sp * m
    min_log_hz, min_log_mel, logstep = 1000.0, 1000.0 / f_sp, np.log(6.4) / 27.0
    return np.where(m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), freqs)


def mel_basis() -> np.ndarray:
    """Slaney-normalised mel filterbank, identical to librosa.filters.mel defaults."""
    global _MEL_BASIS
    if _MEL_BASIS is None:
        fft_freqs = np.linspace(0, SR / 2, 1 + N_FFT // 2)
        mel_f = _mel_to_hz(np.linspace(_hz_to_mel(FMIN), _hz_to_mel(FMAX), N_MELS + 2))
        fdiff = np.diff(mel_f)
        ramps = np.subtract.outer(mel_f, fft_freqs)
        weights = np.zeros((N_MELS, len(fft_freqs)))
        for i in range(N_MELS):
            lower = -ramps[i] / fdiff[i]
            upper = ramps[i + 2] / fdiff[i + 1]
            weights[i] = np.maximum(0, np.minimum(lower, upper))
        enorm = 2.0 / (mel_f[2 : N_MELS + 2] - mel_f[:N_MELS])
        _MEL_BASIS = weights * enorm[:, None]
    return _MEL_BASIS


def melspectrogram(wav: np.ndarray) -> np.ndarray:
    """(80, T) normalised log-mel, 80 frames per second (Wav2Lip audio.py)."""
    wav = lfilter([1, -0.97], [1], wav.astype(np.float64))
    pad = N_FFT // 2
    padded = np.pad(wav, (pad, pad), mode="reflect")
    window = get_window("hann", WIN, fftbins=True)
    frames = 1 + (len(padded) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(frames)[:, None]
    spectrum = np.abs(np.fft.rfft(padded[idx] * window, n=N_FFT, axis=1)).T
    mel = mel_basis() @ spectrum
    s = 20 * np.log10(np.maximum(1e-5, mel)) - 20
    return np.clip(8 * ((s + 100) / 100) - 4, -4, 4).astype(np.float32)


def mel_chunks(mel: np.ndarray, frames: int, fps: float) -> list[np.ndarray]:
    step = 80.0 / fps
    chunks = []
    for i in range(frames):
        start = int(i * step)
        if start + 16 > mel.shape[1]:
            chunks.append(mel[:, -16:] if mel.shape[1] >= 16 else np.pad(mel, ((0, 0), (0, 16 - mel.shape[1]))))
        else:
            chunks.append(mel[:, start : start + 16])
    return chunks


# ---------------------------------------------------------------------------
# Face boxes for a track at full resolution
# ---------------------------------------------------------------------------


def track_boxes(track: Track, frame_count: int, scale: float, width: int, height: int, smooth: int = 5) -> list[tuple[int, int, int, int] | None]:
    """Per-frame crop boxes (x1, y1, x2, y2) at full resolution; None = leave frame untouched."""
    known = sorted(track.frames)
    raw: list[np.ndarray | None] = [None] * frame_count
    inv = 1 / scale
    for i in known:
        x, y, w, h = track.frames[i].box
        raw[i] = np.array([x * inv, y * inv, w * inv, h * inv])
    # interpolate short gaps inside the track span (≤ 6 frames)
    for a, b in zip(known, known[1:]):
        if 1 < b - a <= 7:
            for i in range(a + 1, b):
                t = (i - a) / (b - a)
                raw[i] = raw[a] * (1 - t) + raw[b] * t
    out: list[tuple[int, int, int, int] | None] = []
    for i in range(frame_count):
        if raw[i] is None:
            out.append(None)
            continue
        window = [raw[j] for j in range(max(0, i - smooth // 2), min(frame_count, i + smooth // 2 + 1)) if raw[j] is not None]
        x, y, w, h = np.mean(window, axis=0)
        x1 = int(max(0, x - 0.08 * w))
        x2 = int(min(width, x + w + 0.08 * w))
        y1 = int(max(0, y - 0.04 * h))
        y2 = int(min(height, y + h + 0.12 * h))
        out.append((x1, y1, x2, y2) if x2 - x1 >= 24 and y2 - y1 >= 24 else None)
    return out


def _blend_mask(width: int, height: int) -> np.ndarray:
    """Feathered mask covering mouth and jaw; upper face (eyes, brows) stays original."""
    import cv2

    ys = np.linspace(0, 1, height)[:, None]
    xs = np.linspace(0, 1, width)[None, :]
    vertical = np.clip((ys - 0.45) / 0.15, 0, 1) * np.clip((1.0 - ys) / 0.06, 0, 1)
    horizontal = np.clip(xs / 0.14, 0, 1) * np.clip((1 - xs) / 0.14, 0, 1)
    mask = (vertical * horizontal).astype(np.float32)
    k = max(3, (min(width, height) // 12) | 1)
    return cv2.GaussianBlur(mask, (k, k), 0)[..., None]


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


@dataclass
class RenderResult:
    frames_written: int
    frames_modified: int
    seconds: float
    fps_out: float


def _encoder(out_path: Path, width: int, height: int, fps: float, audio_path: Path | None) -> subprocess.Popen:
    rate = frame_rate_string(fps)
    cmd = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", rate, "-i", "pipe:0"]
    if audio_path is not None:
        cmd += ["-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-crf", str(lipsync_settings.crf), "-preset", lipsync_settings.preset,
            "-pix_fmt", "yuv420p", "-r", rate, "-movflags", "+faststart", str(out_path)]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def _finish(proc: subprocess.Popen) -> None:
    proc.stdin.close()
    err = proc.stderr.read().decode("utf-8", "replace")
    if proc.wait() != 0:
        raise LipSyncUnavailable(f"Video encoding failed: {err[:400]}")


def render_original(video_path: Path, start_frame: int, count: int, fps: float, width: int, height: int,
                    audio_path: Path | None, out_path: Path) -> int:
    """Re-encode the untouched source frames with the same settings (fair 'before')."""
    enc = _encoder(out_path, width, height, fps, audio_path)
    written = 0
    for frame in decode_frames(video_path, start_frame, count, fps, width, height):
        enc.stdin.write(frame.tobytes())
        written += 1
    _finish(enc)
    return written


def generate_wav2lip(video_path: Path, start_frame: int, count: int, fps: float, width: int, height: int,
                     boxes: list, audio16k: np.ndarray, audio_path: Path, out_path: Path,
                     check_active: ActiveCheck | None = None, progress: Callable[[int, int], None] | None = None) -> RenderResult:
    import cv2
    import torch

    started = time.time()
    generator, _sync, dev = load_models(need_generator=True, need_syncnet=False)
    chunks = mel_chunks(melspectrogram(audio16k), count, fps)
    enc = _encoder(out_path, width, height, fps, audio_path)
    batch = max(1, lipsync_settings.batch_size)
    pending: list[tuple[int, np.ndarray]] = []
    modified = written = 0

    def flush():
        nonlocal modified, written
        work = [(i, f) for i, f in pending if boxes[i] is not None]
        if work:
            crops, mels = [], []
            for i, frame in work:
                x1, y1, x2, y2 = boxes[i]
                crops.append(cv2.resize(frame[y1:y2, x1:x2], (96, 96), interpolation=cv2.INTER_AREA))
                mels.append(chunks[i])
            faces = np.asarray(crops, dtype=np.float32)
            masked = faces.copy()
            masked[:, 48:] = 0
            face_in = torch.from_numpy(np.concatenate([masked, faces], axis=3).transpose(0, 3, 1, 2) / 255.0).float().to(dev)
            mel_in = torch.from_numpy(np.asarray(mels)[:, None]).float().to(dev)
            with _lock, torch.inference_mode():
                pred = generator(mel_in, face_in).cpu().numpy().transpose(0, 2, 3, 1) * 255.0
            lookup = {i: p for (i, _f), p in zip(work, pred)}
        else:
            lookup = {}
        for i, frame in pending:
            out = frame
            if i in lookup:
                x1, y1, x2, y2 = boxes[i]
                w, h = x2 - x1, y2 - y1
                patch = cv2.resize(np.clip(lookup[i], 0, 255).astype(np.uint8), (w, h), interpolation=cv2.INTER_CUBIC)
                mask = _blend_mask(w, h)
                out = frame.copy()
                region = out[y1:y2, x1:x2].astype(np.float32)
                out[y1:y2, x1:x2] = (region * (1 - mask) + patch.astype(np.float32) * mask).astype(np.uint8)
                modified += 1
            enc.stdin.write(np.ascontiguousarray(out).tobytes())
            written += 1
        pending.clear()
        if progress:
            progress(written, count)

    try:
        for index, frame in enumerate(decode_frames(video_path, start_frame, count, fps, width, height)):
            if check_active and index % batch == 0:
                check_active()
            pending.append((index, frame))
            if len(pending) >= batch:
                flush()
        if pending:
            flush()
    except Exception:
        enc.kill()
        raise
    _finish(enc)
    return RenderResult(written, modified, round(time.time() - started, 2), fps)


def generate_remote(before_silent: Path, audio_path: Path, boxes: list, fps: float, out_raw: Path) -> None:
    """Send the segment to a self-hosted GPU worker; it returns an mp4 of the same frames."""
    import httpx

    if not lipsync_settings.remote_url:
        raise LipSyncUnavailable("LIPSYNC_REMOTE_URL is not configured.")
    headers = {"Authorization": f"Bearer {lipsync_settings.remote_token}"} if lipsync_settings.remote_token else {}
    with before_silent.open("rb") as video_file, audio_path.open("rb") as audio_file:
        response = httpx.post(
            lipsync_settings.remote_url.rstrip("/") + "/lipsync",
            data={"model": lipsync_settings.remote_model, "fps": str(fps), "face_boxes": json.dumps(boxes)},
            files={"video": ("segment.mp4", video_file, "video/mp4"), "audio": ("dub.wav", audio_file, "audio/wav")},
            headers=headers,
            timeout=lipsync_settings.remote_timeout_s,
        )
    if response.status_code != 200 or not response.content:
        raise LipSyncUnavailable(f"Lip-sync worker failed ({response.status_code}): {response.text[:300]}")
    out_raw.write_bytes(response.content)


# ---------------------------------------------------------------------------
# Sync scoring (Wav2Lip lip-sync expert)
# ---------------------------------------------------------------------------


def sync_score(video_path: Path, start_frame: int, count: int, fps: float, width: int, height: int,
               boxes: list, audio16k: np.ndarray, max_offset: int = 8) -> dict | None:
    """SyncNet confidence: how much better the best audio offset matches than the median.

    Returns {confidence, best_offset_frames, similarity_at_0, windows}. High
    confidence with a best offset near 0 means mouth and audio agree.
    Caveat: this expert is the same network Wav2Lip was trained against, so it
    favours Wav2Lip output; treat it as a consistency check, not ground truth.
    """
    import cv2
    import torch

    _gen, syncnet, dev = load_models(need_generator=False, need_syncnet=True)
    if syncnet is None:
        return None
    chunks = mel_chunks(melspectrogram(audio16k), count, fps)
    lower: dict[int, np.ndarray] = {}
    for i, frame in enumerate(decode_frames(video_path, start_frame, count, fps, width, height)):
        if i < len(boxes) and boxes[i] is not None:
            x1, y1, x2, y2 = boxes[i]
            face = cv2.resize(frame[y1:y2, x1:x2], (96, 96), interpolation=cv2.INTER_AREA)
            lower[i] = face[48:].astype(np.float32) / 255.0
    starts = [i for i in range(count - 4) if all((i + k) in lower for k in range(5))]
    if len(starts) < 4:
        return None
    faces = torch.from_numpy(
        np.stack([np.concatenate([lower[i + k] for k in range(5)], axis=2).transpose(2, 0, 1) for i in starts])
    ).float().to(dev)
    F = torch.nn.functional
    offsets = list(range(-max_offset, max_offset + 1))
    sims = []
    with _lock, torch.inference_mode():
        face_emb = torch.cat([
            F.normalize(syncnet.face_encoder(faces[j : j + 64]).flatten(1), dim=1) for j in range(0, len(starts), 64)
        ])
        for offset in offsets:
            idx = [min(len(chunks) - 1, max(0, i + offset)) for i in starts]
            mel = torch.from_numpy(np.stack([chunks[j] for j in idx])[:, None]).float().to(dev)
            audio_emb = torch.cat([
                F.normalize(syncnet.audio_encoder(mel[j : j + 64]).flatten(1), dim=1) for j in range(0, len(idx), 64)
            ])
            sims.append(float(F.cosine_similarity(audio_emb, face_emb).mean()))
    sims_arr = np.array(sims)
    best = int(np.argmax(sims_arr))
    return {
        "confidence": round(float(sims_arr[best] - np.median(sims_arr)), 4),
        "best_offset_frames": offsets[best],
        "similarity_at_0": round(float(sims_arr[max_offset]), 4),
        "best_similarity": round(float(sims_arr[best]), 4),
        "windows": len(starts),
    }


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------


def probe_output(path: Path, fps: float) -> dict:
    """Decode the generated clip completely: frame count, duration, black/corrupted frames."""
    cmd = [ffmpeg_binary(), "-hide_banner", "-i", str(path), "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "gray",
           "-vf", "scale=64:-2", "pipe:1"]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.decode("utf-8", "replace")[-300:]}
    info = proc.stderr.decode("utf-8", "replace")
    import re

    size = re.search(r"Video: .*?, (\d{2,5})x(\d{2,5})", info)
    has_audio = "Audio:" in info
    data = np.frombuffer(proc.stdout, np.uint8)
    probe = subprocess.run([ffmpeg_binary(), "-hide_banner", "-i", str(path), "-vf", "scale=64:-2", "-frames:v", "1", "-f", "rawvideo",
                            "-pix_fmt", "gray", "pipe:1"], capture_output=True, check=False)
    frame_px = len(probe.stdout) or 1
    frames = data.size // frame_px if frame_px else 0
    means = data[: frames * frame_px].reshape(frames, frame_px).mean(axis=1) if frames else np.array([])
    duration = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", info)
    seconds = (int(duration.group(1)) * 3600 + int(duration.group(2)) * 60 + float(duration.group(3))) if duration else None
    return {
        "ok": frames > 0,
        "frames": int(frames),
        "black_frames": int((means < 3).sum()) if frames else 0,
        "duration_s": seconds,
        "has_audio": has_audio,
        "size": [int(size.group(1)), int(size.group(2))] if size else None,
    }


def region_psnr(a_path: Path, b_path: Path, start_frame: int, count: int, fps: float, width: int, height: int,
                boxes: list, region: str) -> float | None:
    """Mean PSNR between two renders inside a region: 'upper_face' or 'outside_face'."""
    pairs = zip(decode_frames(a_path, 0, count, fps, width, height), decode_frames(b_path, 0, count, fps, width, height))
    values = []
    for i, (fa, fb) in enumerate(pairs):
        box = boxes[i] if i < len(boxes) else None
        if box is None:
            continue
        x1, y1, x2, y2 = box
        if region == "upper_face":
            ya, yb = fa[y1 : y1 + int((y2 - y1) * 0.42), x1:x2], fb[y1 : y1 + int((y2 - y1) * 0.42), x1:x2]
        else:
            mask = np.ones(fa.shape[:2], bool)
            mask[y1:y2, x1:x2] = False
            ya, yb = fa[mask], fb[mask]
        if ya.size == 0:
            continue
        mse = float(np.mean((ya.astype(np.float32) - yb.astype(np.float32)) ** 2))
        values.append(99.0 if mse < 1e-6 else 10 * np.log10(255.0**2 / mse))
    return round(float(np.mean(values)), 2) if values else None
