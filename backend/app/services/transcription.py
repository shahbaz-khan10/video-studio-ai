"""Open-source speech recognition service for Phase 6.

The default local engine is faster-whisper (CTranslate2).  Models are loaded
lazily and only one model is kept in memory, so a long transcription job never
stacks multiple large models in the same worker.
"""

import gc
import importlib.util
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Any

from ..config import settings

ProgressCallback = Callable[[float], None]
ActiveCheck = Callable[[], None]


class TranscriptionCancelled(Exception):
    pass


class TranscriptionUnavailable(Exception):
    pass


@dataclass
class ASRSegment:
    start_ms: int
    end_ms: int
    text: str
    confidence: float | None = None
    language: str | None = None
    words: list[dict] = field(default_factory=list)


@dataclass
class ASRResult:
    segments: list[ASRSegment]
    language: str
    backend: str
    model: str
    limitations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


_MODEL_LOCK = threading.RLock()
_ACTIVE_MODEL: Any = None
_ACTIVE_KEY: tuple[str, str, str, str] | None = None


def faster_whisper_available() -> bool:
    if importlib.util.find_spec("faster_whisper") is None:
        return False
    try:
        from faster_whisper import WhisperModel  # noqa: F401

        return True
    except Exception:
        return False


def effective_backend() -> str:
    selected = settings.transcription_backend.strip().lower()
    if selected == "auto":
        if faster_whisper_available():
            return "faster-whisper"
        if settings.transcription_remote_url:
            return "remote"
        return "unavailable"
    return selected


def effective_device() -> str:
    selected = settings.transcription_device.strip().lower()
    if selected != "auto":
        return selected
    return "cpu"


def effective_compute_type(device: str) -> str:
    selected = settings.transcription_compute_type.strip().lower()
    if selected != "auto":
        return selected
    return "float16" if device == "cuda" else "int8"


def production_info() -> dict:
    backend = effective_backend()
    device = effective_device()
    limitations: list[str] = []
    if backend == "unavailable":
        limitations.append("No local ASR engine is installed and no remote ASR URL is configured.")
    if backend == "faster-whisper" and not faster_whisper_available():
        limitations.append("faster-whisper is selected but cannot be imported on this machine.")
    if backend == "remote" and not settings.transcription_remote_url:
        limitations.append("Remote ASR is selected but no URL is configured.")
    return {
        "configured_backend": settings.transcription_backend,
        "effective_backend": backend,
        "faster_whisper_available": faster_whisper_available(),
        "remote_configured": bool(settings.transcription_remote_url),
        "model": settings.transcription_model,
        "device": device,
        "compute_type": effective_compute_type(device),
        "language_setting": settings.transcription_language,
        "supported_languages": sorted(settings.supported_transcription_languages),
        "word_timestamps": settings.transcription_word_timestamps,
        "vad_filter": settings.transcription_vad_filter,
        "model_cache_dir": str(settings.effective_transcription_cache_dir),
        "single_model_in_memory": True,
        "browser_processing": False,
        "cpu_fallback": True,
        "limitations": limitations,
    }


def _normalize_segments(raw: list[dict], default_language: str) -> list[ASRSegment]:
    segments: list[ASRSegment] = []
    for item in raw:
        start_ms = max(0, int(round(float(item.get("start_ms", 0)))))
        end_ms = max(start_ms, int(round(float(item.get("end_ms", start_ms)))))
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        words = []
        for word in item.get("words") or []:
            word_start = max(0, int(round(float(word.get("start_ms", 0)))))
            word_end = max(word_start, int(round(float(word.get("end_ms", word_start)))))
            word_text = str(word.get("text") or "").strip()
            if not word_text:
                continue
            words.append(
                {
                    "start_ms": word_start,
                    "end_ms": word_end,
                    "text": word_text,
                    "confidence": word.get("confidence"),
                }
            )
        segments.append(
            ASRSegment(
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                confidence=item.get("confidence"),
                language=item.get("language") or default_language,
                words=words,
            )
        )
    segments.sort(key=lambda segment: (segment.start_ms, segment.end_ms))
    return segments


def _get_model(model_name: str, device: str, compute_type: str, check_active: ActiveCheck | None):
    global _ACTIVE_MODEL, _ACTIVE_KEY

    key = (model_name, device, compute_type, str(settings.effective_transcription_cache_dir))
    with _MODEL_LOCK:
        if check_active:
            check_active()
        if _ACTIVE_KEY == key and _ACTIVE_MODEL is not None:
            return _ACTIVE_MODEL
        if _ACTIVE_MODEL is not None:
            _ACTIVE_MODEL = None
            gc.collect()
        settings.effective_transcription_cache_dir.mkdir(parents=True, exist_ok=True)
        from faster_whisper import WhisperModel

        _ACTIVE_MODEL = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            download_root=str(settings.effective_transcription_cache_dir),
        )
        _ACTIVE_KEY = key
        if check_active:
            check_active()
        return _ACTIVE_MODEL


def _transcribe_faster_whisper_locked(
    path: Path,
    language: str,
    word_timestamps: bool,
    check_active: ActiveCheck | None,
    on_progress: ProgressCallback | None,
) -> ASRResult:
    device = effective_device()
    compute_type = effective_compute_type(device)
    model = _get_model(settings.transcription_model, device, compute_type, check_active)
    if check_active:
        check_active()

    requested_language = None if language == "auto" else language
    vad_filter = settings.transcription_vad_filter
    try:
        result, info = model.transcribe(
            str(path),
            language=requested_language,
            word_timestamps=word_timestamps,
            vad_filter=vad_filter,
            beam_size=5,
            condition_on_previous_text=False,
        )
    except Exception as error:
        if vad_filter:
            # A missing VAD runtime must not make the whole ASR stage unusable.
            result, info = model.transcribe(
                str(path),
                language=requested_language,
                word_timestamps=word_timestamps,
                vad_filter=False,
                beam_size=5,
                condition_on_previous_text=False,
            )
            vad_note = f"VAD filter unavailable, transcription continued without it: {str(error)[:180]}"
        else:
            raise

    detected_language = str(getattr(info, "language", None) or language)
    raw: list[dict] = []
    for segment in result:
        if check_active:
            check_active()
        avg_logprob = getattr(segment, "avg_logprob", None)
        confidence = None
        if avg_logprob is not None:
            confidence = round(max(0.0, min(1.0, math.exp(float(avg_logprob)))), 4)
        words = []
        for word in getattr(segment, "words", None) or []:
            probability = getattr(word, "probability", None)
            words.append(
                {
                    "start_ms": int(round(float(word.start) * 1000)),
                    "end_ms": int(round(float(word.end) * 1000)),
                    "text": str(word.word).strip(),
                    "confidence": round(float(probability), 4) if probability is not None else None,
                }
            )
        raw.append(
            {
                "start_ms": int(round(float(segment.start) * 1000)),
                "end_ms": int(round(float(segment.end) * 1000)),
                "text": str(segment.text),
                "confidence": confidence,
                "language": detected_language,
                "words": words,
            }
        )
        if on_progress:
            on_progress(float(segment.end))

    limitations = [
        "Whisper segment confidence is derived from average log probability, not a calibrated word error rate.",
        "Urdu and Hindi accuracy depends on the configured model size; tiny is fast but less reliable.",
    ]
    notes = [vad_note] if "vad_note" in locals() else []
    return ASRResult(
        segments=_normalize_segments(raw, detected_language),
        language=detected_language,
        backend="faster-whisper",
        model=settings.transcription_model,
        limitations=limitations,
        notes=notes,
    )


def _transcribe_faster_whisper(
    path: Path,
    language: str,
    word_timestamps: bool,
    check_active: ActiveCheck | None,
    on_progress: ProgressCallback | None,
) -> ASRResult:
    # Hold the model lease for the whole inference, not just model loading.
    # Concurrent jobs therefore share one resident model instead of swapping
    # or keeping multiple large models in memory at once.
    with _MODEL_LOCK:
        return _transcribe_faster_whisper_locked(
            path,
            language,
            word_timestamps,
            check_active,
            on_progress,
        )


def _transcribe_remote(
    path: Path,
    language: str,
    word_timestamps: bool,
    check_active: ActiveCheck | None,
    on_progress: ProgressCallback | None,
) -> ASRResult:
    if not settings.transcription_remote_url:
        raise TranscriptionUnavailable("Remote ASR is selected but TRANSCRIPTION_REMOTE_URL is not configured.")
    if check_active:
        check_active()
    try:
        import httpx
    except ImportError as error:
        raise TranscriptionUnavailable("httpx is required for the optional remote ASR backend.") from error

    headers = {"Content-Type": "application/json"}
    if settings.transcription_remote_token:
        headers["Authorization"] = f"Bearer {settings.transcription_remote_token}"
    with httpx.Client(timeout=httpx.Timeout(3600.0)) as client:
        response = client.post(
            settings.transcription_remote_url,
            headers=headers,
            json={
                "audio_path": str(path),
                "language": language,
                "model": settings.transcription_model,
                "word_timestamps": word_timestamps,
                "vad_filter": settings.transcription_vad_filter,
            },
        )
        response.raise_for_status()
        payload = response.json()
    if check_active:
        check_active()
    if on_progress:
        on_progress(1.0)
    detected_language = str(payload.get("language") or language)
    return ASRResult(
        segments=_normalize_segments(payload.get("segments") or [], detected_language),
        language=detected_language,
        backend="remote",
        model=str(payload.get("model") or settings.transcription_model),
        limitations=["Remote ASR is external to this application; verify its provider and cost policy."],
        notes=payload.get("notes") or [],
    )


def transcribe_file(
    path: Path,
    language: str,
    word_timestamps: bool,
    check_active: ActiveCheck | None = None,
    on_progress: ProgressCallback | None = None,
) -> ASRResult:
    backend = effective_backend()
    if backend == "faster-whisper":
        return _transcribe_faster_whisper(path, language, word_timestamps, check_active, on_progress)
    if backend == "remote":
        return _transcribe_remote(path, language, word_timestamps, check_active, on_progress)
    if backend in {"whisper", "whisperx"}:
        raise TranscriptionUnavailable(
            f"The {backend} backend is not installed in this build. Use faster-whisper or configure a remote URL."
        )
    raise TranscriptionUnavailable(
        "No speech recognition engine is available. Install faster-whisper or configure TRANSCRIPTION_REMOTE_URL."
    )
