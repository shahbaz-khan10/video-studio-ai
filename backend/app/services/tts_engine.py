"""Phase 10 — speech synthesis and audio finishing for dubbed dialogue.

Synthesis paths
---------------
phase9   A character's active Phase 9 voice profile is rendered by Phase 9's own
         engine adapter (services/voice_engine.py: Piper, MMS, self-hosted
         worker), so a line sounds exactly like the profile preview.
builtin  Phase 10's built-in Meta MMS-TTS (VITS) for characters without a usable
         Phase 9 profile: facebook/mms-tts-urd-script_arabic, facebook/mms-tts-hin
         (CC-BY-NC-4.0, ~145 MB per language, faster than real time on CPU).

Every take is then finished identically: pitch / tone / tempo shaping that the
engine cannot do natively (ffmpeg rubberband + equaliser), one project sample
rate, edge silence trimmed, loudness-normalised under a hard peak ceiling.
"""

from __future__ import annotations

import gc
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..config import PROJECT_ROOT, settings

ActiveCheck = Callable[[], None]


class TtsSettings(BaseSettings):
    """Phase 10 settings live under their own prefix (DUB_TTS_*)."""

    model_config = SettingsConfigDict(
        env_prefix="DUB_TTS_", env_file=str(PROJECT_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    mms_model_ur: str = "facebook/mms-tts-urd-script_arabic"
    mms_model_hi: str = "facebook/mms-tts-hin"
    sample_rate: int = 24000
    target_rms_dbfs: float = -20.0
    peak_ceiling_dbfs: float = -1.0
    sentence_pause_ms: int = 280
    ellipsis_pause_ms: int = 600
    edge_silence_ms: int = 80
    max_chunk_chars: int = 220
    max_takes_per_line: int = 8
    model_cache_dir: Path | None = None
    # Use Phase 9 profiles when present (set false to force the built-in voice).
    use_phase9_profiles: bool = True


tts_settings = TtsSettings()


class TtsUnavailable(Exception):
    pass


class TtsCancelled(Exception):
    pass


# The delivery vocabulary the UI knows. Each voice exposes only what its
# engine really renders; everything else is shown disabled.
ALL_EMOTIONS = ["neutral", "happy", "sad", "angry", "fearful", "excited", "calm", "whispering", "shouting", "serious"]
# Phase 10 delivery → Phase 9 VoiceEmotion (sent to engines that declare "emotion").
PHASE9_EMOTION = {"happy": "happy", "sad": "sad", "angry": "angry", "calm": "calm", "serious": "serious"}

BUILTIN = {
    "label": "Meta MMS-TTS (VITS), built-in",
    "license": "CC-BY-NC-4.0 weights",
    "commercial_use": False,
    "languages": ["ur", "hi"],
    "native": {"speed", "seed"},
    "ram_gb": 0.75,
    "vram_gb": 0.0,
    "download_gb": 0.15,
    "notes": "One base voice per language (male-sounding). Characters are separated by pitch, tone and speed shaping. No cloning, no emotion control. Non-commercial licence.",
}

PITCH_SEMITONES = {"low": -3.0, "medium": 0.0, "high": 4.0}
TONE_FILTERS = {
    "neutral": "",
    "warm": "equalizer=f=250:t=q:w=1.0:g=3,equalizer=f=6000:t=q:w=1.0:g=-2",
    "bright": "equalizer=f=4500:t=q:w=1.2:g=4",
    "light": "highshelf=f=3000:g=3,lowshelf=f=180:g=-3",
    "deep": "lowshelf=f=200:g=5,highshelf=f=5000:g=-3",
    "soft": "highshelf=f=3500:g=-5,volume=0.85",
    "heavy": "lowshelf=f=150:g=6,equalizer=f=900:t=q:w=1.0:g=-2",
}

_lock = threading.RLock()
_loaded: dict[str, Any] = {"key": None, "model": None, "tokenizer": None}


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _cache_dir() -> Path:
    path = tts_settings.model_cache_dir or (settings.effective_model_cache_dir / "hf" / "hub")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def mms_available() -> bool:
    return _has_module("transformers") and _has_module("torch")


def builtin_model(language: str) -> str:
    return tts_settings.mms_model_ur if language == "ur" else tts_settings.mms_model_hi


def mms_cached(language: str) -> bool:
    folder = _cache_dir() / ("models--" + builtin_model(language).replace("/", "--"))
    return folder.is_dir() and (any(folder.rglob("*.safetensors")) or any(folder.rglob("*.bin")))


def phase9_engine():
    """Phase 9's engine adapter, or None when it is absent or incompatible."""
    if not tts_settings.use_phase9_profiles:
        return None
    try:
        from . import voice_engine  # Phase 9
    except Exception:  # noqa: BLE001
        return None
    needed = ("load", "capabilities", "preview_available", "ENGINES")
    return voice_engine if all(hasattr(voice_engine, name) for name in needed) else None


def phase9_capabilities(engine: str, voice_id: str | None) -> set[str]:
    module = phase9_engine()
    if module is None:
        return set()
    try:
        return set(module.capabilities(engine, voice_id))
    except Exception:  # noqa: BLE001
        return set()


def ffmpeg_binary() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def engine_info() -> dict:
    module = phase9_engine()
    phase9 = {}
    if module is not None:
        for name, meta in module.ENGINES.items():
            if name == "none":
                continue
            phase9[name] = {
                "label": meta.get("label"),
                "license": meta.get("license"),
                "commercial_use": meta.get("commercial_use"),
                "languages": meta.get("languages"),
                "emotion": "emotion" in phase9_capabilities(name, None),
                "requirements": meta.get("requirements"),
            }
    return {
        "sample_rate": tts_settings.sample_rate,
        "builtin": {
            **BUILTIN,
            "native": sorted(BUILTIN["native"]),
            "available": mms_available(),
            "cached": {lang: mms_cached(lang) for lang in ("ur", "hi")},
            "models": {lang: builtin_model(lang) for lang in ("ur", "hi")},
        },
        "phase9_available": module is not None,
        "phase9_engines": phase9,
        "loaded": _loaded["key"],
        "all_emotions": ALL_EMOTIONS,
        "shaping": {
            "pitch": "rubberband pitch shift (formants follow pitch)",
            "tone": sorted(TONE_FILTERS),
            "tempo": "rubberband time-stretch when the engine has no native speed",
        },
        "synthetic_disclosure": "All generated dialogue is synthetic (AI text-to-speech), not a human recording.",
    }


def unload() -> None:
    with _lock:
        _loaded.update({"key": None, "model": None, "tokenizer": None})
        gc.collect()


def _release_other_models() -> None:
    """Free translation / ASR / diarization / Phase 9 models before loading TTS."""
    try:
        from . import translation_engine

        if translation_engine._lock.acquire(blocking=False):
            try:
                translation_engine.unload()
            finally:
                translation_engine._lock.release()
        translation_engine.release_other_models()
    except Exception:  # noqa: BLE001
        pass


def load_builtin(language: str, check_active: ActiveCheck | None = None) -> tuple[Any, Any]:
    key = f"mms:{language}"
    with _lock:
        if _loaded["key"] == key:
            return _loaded["model"], _loaded["tokenizer"]
        if not mms_available():
            raise TtsUnavailable("The built-in voice needs `transformers` and `torch` (backend/requirements.txt).")
        unload()
        _release_other_models()
        if check_active:
            check_active()
        from transformers import AutoTokenizer, VitsModel

        name = builtin_model(language)
        try:
            tokenizer = AutoTokenizer.from_pretrained(name, cache_dir=str(_cache_dir()))
            model = VitsModel.from_pretrained(name, cache_dir=str(_cache_dir())).eval()
        except OSError as error:
            raise TtsUnavailable(f"Could not load {name}: {str(error)[:300]}") from error
        _loaded.update({"key": key, "model": model, "tokenizer": tokenizer})
        return model, tokenizer


def unsupported_characters(text: str, language: str) -> list[str]:
    """Characters the MMS vocabulary cannot speak (they would be skipped)."""
    try:
        _model, tokenizer = load_builtin(language)
    except TtsUnavailable:
        return []
    vocab = tokenizer.get_vocab()
    normalised = tokenizer.normalize_text(text) if hasattr(tokenizer, "normalize_text") else text.lower()
    seen: list[str] = []
    for char in normalised:
        if char.isspace() or char in vocab or char in seen:
            continue
        if re.match(r"[\.\,\!\?؟۔،।\-\"'“”‘’…:;()]", char):
            continue
        seen.append(char)
    return seen


# ---------------------------------------------------------------------------
# Text chunking & pauses
# ---------------------------------------------------------------------------


@dataclass
class Chunk:
    text: str
    pause_after_ms: int


def chunk_text(text: str, pause_ms: int | None = None) -> list[Chunk]:
    """Split dialogue into speakable chunks with natural pauses between them."""
    sentence_pause = tts_settings.sentence_pause_ms if pause_ms is None else pause_ms
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?؟۔।])\s+|(\.\.\.|…)", text)
    chunks: list[Chunk] = []
    buffer = ""
    for part in parts:
        if part is None or not part.strip():
            continue
        if part in {"...", "…"}:
            if buffer.strip():
                chunks.append(Chunk(buffer.strip(), tts_settings.ellipsis_pause_ms))
                buffer = ""
            elif chunks:
                chunks[-1].pause_after_ms = tts_settings.ellipsis_pause_ms
            continue
        if buffer:
            chunks.append(Chunk(buffer.strip(), sentence_pause))
        buffer = part
    if buffer.strip():
        chunks.append(Chunk(buffer.strip(), 0))
    out: list[Chunk] = []
    for chunk in chunks:
        if len(chunk.text) <= tts_settings.max_chunk_chars:
            out.append(chunk)
            continue
        pieces = [piece.strip() for piece in re.split(r"(?<=[,،;])\s+", chunk.text) if piece.strip()]
        current = ""
        for piece in pieces:
            if current and len(current) + len(piece) + 1 > tts_settings.max_chunk_chars:
                out.append(Chunk(current, 160))
                current = piece
            else:
                current = f"{current} {piece}".strip()
        if current:
            out.append(Chunk(current, chunk.pause_after_ms))
    if out:
        out[-1].pause_after_ms = 0
    return out


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


@dataclass
class VoiceRender:
    """Fully resolved, reproducible settings for one take."""

    synth: str  # "builtin" | "phase9"
    language: str
    engine: str = "mms"
    voice_id: str | None = None
    model_id: str | None = None
    params: dict = field(default_factory=dict)
    characteristics: dict = field(default_factory=dict)
    native: list[str] = field(default_factory=list)
    speed: float = 1.0  # applied natively when "speed" in native, else by time-stretch
    pitch_semitones: float = 0.0  # post-processing shift
    tone: str = "neutral"  # post-processing EQ
    emotion: str = "neutral"
    seed: int = 0
    pause_ms: int | None = None
    reference_id: str | None = None

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "synth": self.synth, "language": self.language, "engine": self.engine, "voice": self.voice_id,
                "model": self.model_id, "params": self.params, "chars": self.characteristics,
                "speed": round(self.speed, 3), "pitch": round(self.pitch_semitones, 2), "tone": self.tone,
                "seed": self.seed, "pause": self.pause_ms, "ref": self.reference_id,
            },
            sort_keys=True, default=str,
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def label(self) -> str:
        if self.synth == "builtin":
            return BUILTIN["label"]
        module = phase9_engine()
        meta = module.ENGINES.get(self.engine, {}) if module else {}
        return f"{meta.get('label', self.engine)} (Phase 9 profile)"

    @property
    def license(self) -> str | None:
        if self.synth == "builtin":
            return BUILTIN["license"]
        module = phase9_engine()
        return (module.ENGINES.get(self.engine, {}) if module else {}).get("license")

    def emotions(self) -> list[str]:
        if self.synth == "phase9" and "emotion" in self.native:
            return ["neutral", *PHASE9_EMOTION]
        return ["neutral"]


def _synth_builtin(text: str, render: VoiceRender, check_active: ActiveCheck | None) -> tuple[np.ndarray, int]:
    import torch

    model, tokenizer = load_builtin(render.language, check_active)
    with _lock:
        inputs = tokenizer(text, return_tensors="pt")
        if inputs["input_ids"].shape[1] == 0:
            return np.zeros(0, dtype=np.float32), model.config.sampling_rate
        model.speaking_rate = max(0.5, min(2.0, float(render.speed)))
        torch.manual_seed(int(render.seed) & 0x7FFFFFFF)
        with torch.inference_mode():
            waveform = model(**inputs).waveform[0].float().numpy()
    return waveform.astype(np.float32), int(model.config.sampling_rate)


def _decode_bytes(payload: bytes) -> tuple[np.ndarray, int]:
    sr = tts_settings.sample_rate
    result = subprocess.run(
        [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"],
        input=payload,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        raise TtsUnavailable(f"Engine returned undecodable audio: {result.stderr[:200]!r}")
    return np.frombuffer(result.stdout, dtype="<f4").copy(), sr


def _synth_phase9(text: str, render: VoiceRender, reference: bytes | None) -> tuple[np.ndarray, int]:
    module = phase9_engine()
    if module is None:
        raise TtsUnavailable("Phase 9 voice engine is not available.")
    params = dict(render.params)
    params.setdefault("seed", render.seed)
    params["language"] = render.language
    characteristics = dict(render.characteristics)
    if render.emotion != "neutral" and "emotion" in render.native:
        characteristics["emotion"] = PHASE9_EMOTION[render.emotion]
    try:
        loaded = module.load(render.engine, render.voice_id, render.language)
        payload = loaded.synthesize(text, params, characteristics, reference if "reference" in render.native else None)
    except Exception as error:  # noqa: BLE001 — Phase 9 raises AppError; surface it as engine failure
        message = getattr(error, "message", None) or str(error)
        raise TtsUnavailable(f"{render.engine} voice failed: {message}") from error
    return _decode_bytes(payload)


def synthesize_raw(
    text: str, render: VoiceRender, check_active: ActiveCheck | None = None, reference: bytes | None = None
) -> tuple[np.ndarray, int]:
    if render.synth == "builtin":
        return _synth_builtin(text, render, check_active)
    if render.synth == "phase9":
        return _synth_phase9(text, render, reference)
    raise TtsUnavailable(f"Unknown synthesis path '{render.synth}'.")


# ---------------------------------------------------------------------------
# Finishing: shaping, resample, trim, normalise, QC
# ---------------------------------------------------------------------------


def _shape(audio: np.ndarray, sr: int, render: VoiceRender) -> np.ndarray:
    filters: list[str] = []
    tempo = 1.0 if "speed" in render.native else float(render.speed)
    ratio = 2 ** (render.pitch_semitones / 12)
    if abs(render.pitch_semitones) >= 0.05 or abs(tempo - 1.0) >= 0.01:
        filters.append(f"rubberband=pitch={ratio:.5f}:tempo={tempo:.4f}:formant=shifted:pitchq=quality")
    tone = TONE_FILTERS.get(render.tone or "neutral", "")
    if tone:
        filters.append(tone)
    filters.append(f"aresample={tts_settings.sample_rate}")
    result = subprocess.run(
        [
            ffmpeg_binary(), "-hide_banner", "-loglevel", "error",
            "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
            "-af", ",".join(filters), "-f", "f32le", "-ac", "1", "pipe:1",
        ],
        input=audio.astype("<f4").tobytes(),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise TtsUnavailable(f"Audio post-processing failed: {result.stderr[:300]!r}")
    return np.frombuffer(result.stdout, dtype="<f4").copy()


def _dbfs(value: float) -> float:
    return float(20 * np.log10(max(value, 1e-9)))


def trim_silence(audio: np.ndarray, sr: int, keep_ms: int | None = None, threshold_dbfs: float = -45.0) -> np.ndarray:
    if audio.size == 0:
        return audio
    keep = int(sr * (tts_settings.edge_silence_ms if keep_ms is None else keep_ms) / 1000)
    frame = max(1, int(sr * 0.01))
    energy = np.sqrt(np.convolve(audio**2, np.ones(frame) / frame, mode="same"))
    voiced = np.where(energy > 10 ** (threshold_dbfs / 20))[0]
    if voiced.size == 0:
        return audio[:0]
    return audio[max(0, voiced[0] - keep) : min(audio.size, voiced[-1] + keep)]


def normalise(audio: np.ndarray) -> tuple[np.ndarray, dict]:
    if audio.size == 0:
        return audio, {"gain_db": 0.0}
    audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)
    audio = audio - float(audio.mean())
    rms = float(np.sqrt(np.mean(audio**2)))
    peak = float(np.max(np.abs(audio)))
    if peak <= 0:
        return audio, {"gain_db": 0.0}
    gain = min(10 ** (tts_settings.target_rms_dbfs / 20) / max(rms, 1e-9), 10 ** (tts_settings.peak_ceiling_dbfs / 20) / peak)
    return (audio * gain).astype(np.float32), {"gain_db": round(_dbfs(gain), 2)}


def analyse(audio: np.ndarray, sr: int) -> dict:
    if audio.size == 0:
        return {"duration_ms": 0, "peak_dbfs": -120.0, "rms_dbfs": -120.0, "clipped_ratio": 0.0, "has_nan": False}
    return {
        "duration_ms": int(round(audio.size / sr * 1000)),
        "peak_dbfs": round(_dbfs(float(np.max(np.abs(audio)))), 2),
        "rms_dbfs": round(_dbfs(float(np.sqrt(np.mean(audio**2)))), 2),
        "clipped_ratio": round(float(np.mean(np.abs(audio) >= 0.999)), 5),
        "has_nan": bool(np.isnan(audio).any()),
    }


def to_wav_bytes(audio: np.ndarray, sr: int) -> bytes:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        sr = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
        width = handle.getsampwidth()
        channels = handle.getnchannels()
    if width != 2:
        raise ValueError("Only 16-bit PCM WAV is supported")
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, sr


@dataclass
class Rendered:
    wav: bytes
    sample_rate: int
    stats: dict
    flags: list[dict]
    chunks: int


def flag(code: str, severity: str, message: str) -> dict:
    return {"code": code, "severity": severity, "message": message}


def render_line(
    text: str,
    render: VoiceRender,
    check_active: ActiveCheck | None = None,
    target_ms: int | None = None,
    reference: bytes | None = None,
) -> Rendered:
    """Synthesise one dialogue line (any length) into a finished WAV."""
    chunks = chunk_text(text, render.pause_ms)
    if not chunks:
        raise TtsUnavailable("There is no translated text to speak.")
    sr_out = tts_settings.sample_rate
    pieces: list[np.ndarray] = []
    flags: list[dict] = []
    for chunk in chunks:
        if check_active:
            check_active()
        raw, sr = synthesize_raw(chunk.text, render, check_active, reference)
        if raw.size == 0:
            flags.append(flag("chunk_empty", "warning", f"Nothing was produced for: “{chunk.text[:40]}”"))
            continue
        pieces.append(trim_silence(_shape(raw, sr, render), sr_out))
        if chunk.pause_after_ms:
            pieces.append(np.zeros(int(sr_out * chunk.pause_after_ms / 1000), dtype=np.float32))
    audio = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
    audio = trim_silence(audio, sr_out)
    if audio.size:
        pad = np.zeros(int(sr_out * tts_settings.edge_silence_ms / 1000), dtype=np.float32)
        audio = np.concatenate([pad, audio, pad])
    audio, gain = normalise(audio)
    stats = {**analyse(audio, sr_out), **gain}

    if stats["duration_ms"] < 150 or stats["rms_dbfs"] < -55:
        flags.append(flag("empty_audio", "error", "Generated audio is empty or silent."))
    if stats["has_nan"]:
        flags.append(flag("corrupted", "error", "Audio contained invalid samples."))
    if stats["clipped_ratio"] > 0.0005:
        flags.append(flag("clipping", "error", "Audio is clipping."))
    letters = len(re.findall(r"\w", text))
    if stats["duration_ms"] and letters and letters / (stats["duration_ms"] / 1000) > 30:
        flags.append(flag("possibly_truncated", "warning", "Audio is very short for this much text; parts may be missing."))
    if target_ms and stats["duration_ms"] > target_ms * 1.25 and stats["duration_ms"] - target_ms > 400:
        flags.append(
            flag(
                "longer_than_original",
                "info",
                f"{stats['duration_ms'] / 1000:.1f}s vs {target_ms / 1000:.1f}s original line (timing is fitted in Phase 11).",
            )
        )
    return Rendered(to_wav_bytes(audio, sr_out), sr_out, stats, flags, len(chunks))


def validate_wav_file(path: Path) -> dict:
    """Re-open a stored take and check it decodes and is not silent."""
    try:
        audio, sr = read_wav(path)
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": f"Cannot decode: {error}"}
    stats = analyse(audio, sr)
    return {"ok": stats["duration_ms"] >= 150 and stats["rms_dbfs"] > -55, "sample_rate": sr, **stats}
