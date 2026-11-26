"""Open-source speaker diarization for Phase 7.

The default local engine uses Silero VAD for speech start/stop detection,
SpeechBrain ECAPA-TDNN for voice embeddings, and scikit-learn agglomerative
clustering for stable speaker grouping. All models run locally on CPU by
default. No Hugging Face token or paid API is required for the local path.
"""

import gc
import importlib.util
import json
import math
import subprocess
import tempfile
import threading
import uuid
import wave
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..config import settings
from .media import ffmpeg_binary

ProgressCallback = Callable[[float], None]
ActiveCheck = Callable[[], None]


class DiarizationCancelled(Exception):
    pass


class DiarizationUnavailable(Exception):
    pass


@dataclass
class SpeakerTurn:
    start_ms: int
    end_ms: int
    label: str
    confidence: float | None = None
    embedding: list[float] = field(default_factory=list)
    overlap_possible: bool = False
    notes: dict = field(default_factory=dict)


@dataclass
class DiarizationResult:
    turns: list[SpeakerTurn]
    backend: str
    model: str
    vad_model: str
    limitations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


_MODEL_LOCK = threading.RLock()
_ACTIVE_MODEL: Any = None
_ACTIVE_KEY: tuple[str, str, str] | None = None
_ACTIVE_VAD: Any = None
_ACTIVE_VAD_KEY: str | None = None


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def local_available() -> bool:
    return all(_has_module(name) for name in ("torch", "torchaudio", "silero_vad", "speechbrain", "sklearn"))


def effective_backend() -> str:
    selected = settings.diarization_backend.strip().lower()
    if selected == "auto":
        if local_available():
            return "local"
        if settings.diarization_remote_url:
            return "remote"
        return "unavailable"
    if selected == "local" and not local_available():
        return "unavailable"
    if selected == "remote" and not settings.diarization_remote_url:
        return "unavailable"
    return selected if selected in {"local", "remote"} else "unavailable"


def effective_device() -> str:
    selected = settings.diarization_device.strip().lower()
    if selected and selected != "auto":
        return selected
    return "cpu"


def production_info() -> dict:
    backend = effective_backend()
    limitations: list[str] = []
    if backend == "unavailable":
        limitations.append("No local diarization engine is installed and no remote diarization URL is configured.")
    if backend == "local" and not local_available():
        limitations.append("Local diarization dependencies are not importable on this machine.")
    if backend == "remote" and not settings.diarization_remote_url:
        limitations.append("Remote diarization is selected but no URL is configured.")
    return {
        "configured_backend": settings.diarization_backend,
        "effective_backend": backend,
        "local_available": local_available(),
        "remote_configured": bool(settings.diarization_remote_url),
        "model": settings.diarization_model,
        "vad": "silero-vad",
        "clustering": "sklearn.cluster.AgglomerativeClustering",
        "device": effective_device(),
        "vad_threshold": settings.diarization_vad_threshold,
        "min_speech_ms": settings.diarization_min_speech_ms,
        "min_silence_ms": settings.diarization_min_silence_ms,
        "min_turn_ms": settings.diarization_min_turn_ms,
        "max_chunk_s": settings.diarization_max_chunk_s,
        "cluster_distance": settings.diarization_cluster_distance,
        "match_threshold": settings.diarization_match_threshold,
        "review_threshold": settings.diarization_review_threshold,
        "match_margin": settings.diarization_match_margin,
        "model_cache_dir": str(settings.effective_diarization_cache_dir),
        "single_model_in_memory": True,
        "overlap_detection": "approximate; close embedding margins are flagged for review",
        "identifies_people": False,
        "paid_api_required": False,
        "licenses": {
            "silero-vad": "MIT",
            "speechbrain": "Apache-2.0",
            "spkrec-ecapa-voxceleb": "Apache-2.0",
            "scikit-learn": "BSD-3-Clause",
        },
        "limitations": limitations,
    }


def _get_vad_model() -> Any:
    global _ACTIVE_VAD, _ACTIVE_VAD_KEY
    from silero_vad import load_silero_vad

    if _ACTIVE_VAD is None:
        _ACTIVE_VAD = load_silero_vad()
        _ACTIVE_VAD_KEY = "silero-vad"
    return _ACTIVE_VAD


def _get_embedding_model(check_active: ActiveCheck | None) -> Any:
    global _ACTIVE_MODEL, _ACTIVE_KEY

    device = effective_device()
    key = (settings.diarization_model, device, str(settings.effective_diarization_cache_dir))
    with _MODEL_LOCK:
        if check_active:
            check_active()
        if _ACTIVE_KEY == key and _ACTIVE_MODEL is not None:
            return _ACTIVE_MODEL
        if _ACTIVE_MODEL is not None:
            _ACTIVE_MODEL = None
            gc.collect()
        settings.effective_diarization_cache_dir.mkdir(parents=True, exist_ok=True)
        import torch
        from speechbrain.inference.speaker import EncoderClassifier
        from speechbrain.utils.fetching import LocalStrategy

        run_opts = {"device": "cuda" if device == "cuda" else "cpu"}
        _ACTIVE_MODEL = EncoderClassifier.from_hparams(
            source=settings.diarization_model,
            savedir=str(settings.effective_diarization_cache_dir / "ecapa-voxceleb"),
            run_opts=run_opts,
            local_strategy=LocalStrategy.COPY,
        )
        _ACTIVE_KEY = key
        if device == "cuda" and torch.cuda.is_available():
            torch.cuda.empty_cache()
        if check_active:
            check_active()
        return _ACTIVE_MODEL


def _cosine_matrix(values: list[list[float]]) -> list[list[float]]:
    return [[sum(a * b for a, b in zip(left, right)) for right in values] for left in values]


def _normalize(value: list[float]) -> list[float]:
    norm = math.sqrt(sum(item * item for item in value))
    if norm <= 1e-12:
        return value
    return [item / norm for item in value]


def _read_waveform(path: Path, check_active: ActiveCheck | None) -> Any:
    """Decode audio to a 16 kHz mono float tensor without torchaudio I/O.

    Recent torchaudio builds moved audio decoding to torchcodec, which is not
    guaranteed to be installed. ffmpeg is already a project dependency, so we
    use it here and read the resulting PCM WAV with the standard library.
    """
    import numpy as np
    import torch

    binary = ffmpeg_binary()
    if not binary:
        raise DiarizationUnavailable("ffmpeg is required to decode audio for local diarization")
    if check_active:
        check_active()
    tmp_root = settings.data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="diarization-decode-", dir=str(tmp_root)) as temp:
        wav_path = Path(temp) / "speech.wav"
        result = subprocess.run(
            [
                binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-y",
                str(wav_path),
            ],
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=600,
            check=False,
        )
        if check_active:
            check_active()
        if result.returncode != 0 or not wav_path.is_file():
            raise DiarizationUnavailable(f"Could not decode audio: {result.stderr[:300]}")
        with wave.open(str(wav_path), "rb") as handle:
            frames = handle.readframes(handle.getnframes())
            sample_width = handle.getsampwidth()
    if not frames:
        return torch.empty(0, dtype=torch.float32)
    dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(sample_width)
    if dtype is None:
        raise DiarizationUnavailable(f"Unsupported audio sample width: {sample_width}")
    samples = np.frombuffer(frames, dtype=dtype).astype(np.float32)
    scale = 128.0 if sample_width == 1 else (32768.0 if sample_width == 2 else 2147483648.0)
    return torch.from_numpy(samples / scale)


def _speech_regions(path: Path, check_active: ActiveCheck | None) -> tuple[Any, list[dict]]:
    from silero_vad import get_speech_timestamps

    if check_active:
        check_active()
    waveform = _read_waveform(path, check_active)
    if check_active:
        check_active()
    model = _get_vad_model()
    regions = get_speech_timestamps(
        waveform,
        model,
        threshold=settings.diarization_vad_threshold,
        sampling_rate=16000,
        min_speech_duration_ms=settings.diarization_min_speech_ms,
        min_silence_duration_ms=settings.diarization_min_silence_ms,
        speech_pad_ms=80,
        return_seconds=False,
    )
    normalized: list[dict] = []
    for region in regions or []:
        start = max(0, int(region.get("start", 0)))
        end = max(start, int(region.get("end", start)))
        if end - start >= int(settings.diarization_min_turn_ms * 16):
            normalized.append({"start": start, "end": end})
    if not normalized and waveform.numel() > 0:
        duration_ms = int(waveform.numel() / 16)
        if duration_ms >= settings.diarization_min_turn_ms:
            normalized.append({"start": 0, "end": int(waveform.numel())})
    return waveform, normalized


def _make_chunks(regions: list[dict]) -> list[dict]:
    window = max(1, int(settings.diarization_max_chunk_s * 16000))
    hop = max(1, int(window * 0.75))
    min_samples = max(1, int(settings.diarization_min_turn_ms * 16))
    chunks: list[dict] = []
    for region in regions:
        start, end = region["start"], region["end"]
        if end - start <= window:
            if end - start >= min_samples:
                chunks.append({"start": start, "end": end})
            continue
        cursor = start
        while cursor < end:
            chunk_end = min(end, cursor + window)
            if chunk_end - cursor >= min_samples:
                chunks.append({"start": cursor, "end": chunk_end})
            if chunk_end >= end:
                break
            cursor += hop
    return chunks


def _cluster_embeddings(embeddings: list[list[float]]) -> list[int]:
    import numpy as np
    from sklearn.cluster import AgglomerativeClustering

    matrix = np.asarray(embeddings, dtype="float32")
    if len(embeddings) <= 1:
        return [0] * len(embeddings)
    model = AgglomerativeClustering(
        n_clusters=None,
        metric="cosine",
        linkage="average",
        distance_threshold=settings.diarization_cluster_distance,
    )
    return [int(value) for value in model.fit_predict(matrix)]


def _label_by_time(chunks: list[dict], labels: list[int]) -> dict[int, int]:
    first_seen: dict[int, int] = {}
    for index, chunk in enumerate(chunks):
        label = labels[index]
        start = chunk["start"]
        if label not in first_seen or start < first_seen[label]:
            first_seen[label] = start
    ordered = sorted(first_seen, key=lambda label: (first_seen[label], label))
    return {old: new for new, old in enumerate(ordered)}


def _merge_turns(chunks: list[dict], labels: list[int], confidences: list[float], overlaps: list[bool]) -> list[SpeakerTurn]:
    ordered = sorted(range(len(chunks)), key=lambda index: (chunks[index]["start"], chunks[index]["end"]))
    merged: list[dict] = []
    for index in ordered:
        chunk = chunks[index]
        label = labels[index]
        gap_ms = int(chunk["start"] / 16) - merged[-1]["end_ms"] if merged else 0
        if merged and merged[-1]["label"] == label and 0 <= gap_ms <= 1000:
            current = merged[-1]
            current["end_ms"] = max(current["end_ms"], int(chunk["end"] / 16))
            weight = max(1, chunk["end"] - chunk["start"])
            current["weight"] += weight
            current["confidence_total"] += confidences[index] * weight
            current["overlap"] = current["overlap"] or overlaps[index]
            continue
        merged.append(
            {
                "label": label,
                "start_ms": int(chunk["start"] / 16),
                "end_ms": int(chunk["end"] / 16),
                "weight": max(1, chunk["end"] - chunk["start"]),
                "confidence_total": confidences[index] * max(1, chunk["end"] - chunk["start"]),
                "overlap": overlaps[index],
            }
        )
    turns: list[SpeakerTurn] = []
    for item in merged:
        confidence = item["confidence_total"] / max(1, item["weight"])
        turns.append(
            SpeakerTurn(
                start_ms=item["start_ms"],
                end_ms=item["end_ms"],
                label=f"SPEAKER_{item['label']:02d}",
                confidence=round(max(0.0, min(1.0, confidence)), 4),
                overlap_possible=bool(item["overlap"]),
                notes={"merged_chunks": True},
            )
        )
    turns.sort(key=lambda turn: (turn.start_ms, turn.end_ms))
    return turns


def _diarize_local(
    path: Path,
    check_active: ActiveCheck | None,
    on_progress: ProgressCallback | None,
) -> DiarizationResult:
    if not local_available():
        raise DiarizationUnavailable("Local diarization dependencies are not installed")

    waveform, regions = _speech_regions(path, check_active)
    chunks = _make_chunks(regions)
    if not chunks:
        return DiarizationResult(
            turns=[],
            backend="local",
            model=settings.diarization_model,
            vad_model="silero-vad",
            limitations=["No speech regions were detected above the configured VAD threshold."],
            notes=["Silero VAD found no usable speech turns in the selected audio."],
        )

    model = _get_embedding_model(check_active)
    import torch

    embeddings: list[list[float]] = []
    for position, chunk in enumerate(chunks):
        if check_active:
            check_active()
        piece = waveform[chunk["start"] : chunk["end"]].unsqueeze(0)
        with torch.no_grad():
            encoded = model.encode_batch(piece)
        vector = encoded.detach().cpu().numpy().reshape(-1).tolist()
        embeddings.append(_normalize(vector))
        if on_progress:
            on_progress((position + 1) / len(chunks))

    if check_active:
        check_active()
    raw_labels = _cluster_embeddings(embeddings)
    remap = _label_by_time(chunks, raw_labels)
    labels = [remap[label] for label in raw_labels]
    unique = sorted(set(labels))
    centroids: dict[int, list[float]] = {}
    for label in unique:
        members = [embeddings[index] for index, value in enumerate(labels) if value == label]
        centroid = _normalize([sum(values) / len(values) for values in zip(*members)])
        centroids[label] = centroid

    centroid_similarities = _cosine_matrix([centroids[label] for label in unique])
    confidences: list[float] = []
    overlaps: list[bool] = []
    for index, label in enumerate(labels):
        own_index = unique.index(label)
        # Cohesion is similarity to this cluster's centroid. Self-similarity
        # is deliberately excluded, otherwise every chunk scores 1.0.
        cohesion = sum(a * b for a, b in zip(embeddings[index], centroids[label]))
        others = [
            centroid_similarities[own_index][other]
            for other in range(len(unique))
            if other != own_index
        ]
        nearest_other = max(others) if others else 0.0
        margin = cohesion - nearest_other if others else 1.0
        separation = 1.0 - nearest_other if others else cohesion
        confidence = 0.5 * max(0.0, cohesion) + 0.5 * max(0.0, min(1.0, separation))
        confidences.append(max(0.0, min(1.0, confidence)))
        overlaps.append(margin < settings.diarization_match_margin and len(unique) > 1)

    turns = _merge_turns(chunks, labels, confidences, overlaps)
    # Attach the embedding of the longest chunk per merged label for persistence.
    longest: dict[str, list[float]] = {}
    for index, chunk in enumerate(chunks):
        label = f"SPEAKER_{labels[index]:02d}"
        if label not in longest or (chunk["end"] - chunk["start"]) > longest[label][0]:
            longest[label] = (chunk["end"] - chunk["start"], embeddings[index])
    for turn in turns:
        turn.embedding = longest.get(turn.label, (0, []))[1]

    limitations = [
        "Diarization groups acoustic speakers; it does not identify real people.",
        "Overlapping speech is approximate and is flagged for review rather than separated into exact transcripts.",
    ]
    if effective_device() == "cpu":
        limitations.append("CPU processing is supported but long recordings take longer than GPU processing.")
    return DiarizationResult(
        turns=turns,
        backend="local",
        model=settings.diarization_model,
        vad_model="silero-vad",
        limitations=limitations,
        notes=[
            f"Silero VAD found {len(regions)} speech regions.",
            f"ECAPA-TDNN produced {len(embeddings)} voice chunks in {len(unique)} acoustic groups.",
        ],
    )


def _diarize_remote(
    path: Path,
    check_active: ActiveCheck | None,
    on_progress: ProgressCallback | None,
) -> DiarizationResult:
    url = settings.diarization_remote_url
    if not url:
        raise DiarizationUnavailable("Remote diarization is not configured")
    boundary = uuid.uuid4().hex
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()
    payload_size = len(prefix) + path.stat().st_size + len(suffix)
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection_type = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
    connection = connection_type(parsed.hostname, port, timeout=7200)
    request_path = parsed.path or "/"
    if parsed.query:
        request_path = f"{request_path}?{parsed.query}"
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(payload_size),
    }
    if settings.diarization_remote_token:
        headers["Authorization"] = f"Bearer {settings.diarization_remote_token}"
    try:
        if check_active:
            check_active()
        connection.putrequest("POST", request_path)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        connection.send(prefix)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                if check_active:
                    check_active()
                connection.send(chunk)
        connection.send(suffix)
        response = connection.getresponse()
        document = json.loads(response.read().decode("utf-8"))
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Remote diarization returned {response.status}: {document}")
    finally:
        connection.close()

    turns: list[SpeakerTurn] = []
    for item in document.get("turns") or []:
        start_ms = max(0, int(item.get("start_ms", 0)))
        end_ms = max(start_ms, int(item.get("end_ms", start_ms)))
        turns.append(
            SpeakerTurn(
                start_ms=start_ms,
                end_ms=end_ms,
                label=str(item.get("label") or f"SPEAKER_{len(turns):02d}"),
                confidence=item.get("confidence"),
                embedding=_normalize([float(value) for value in item.get("embedding") or []]),
                overlap_possible=bool(item.get("overlap_possible", False)),
                notes={"source": "remote"},
            )
        )
    if check_active:
        check_active()
    if on_progress:
        on_progress(1.0)
    return DiarizationResult(
        turns=turns,
        backend="remote",
        model=str(document.get("model") or "remote-unspecified"),
        vad_model=str(document.get("vad") or "remote-unspecified"),
        limitations=[
            "Remote diarization output is accepted as provided and is not locally re-validated.",
            "Remote GPU processing is optional and may incur cost outside this application.",
        ],
        notes=["Remote diarization was used because it was explicitly configured."],
    )


def diarize_file(
    path: Path,
    check_active: ActiveCheck | None = None,
    on_progress: ProgressCallback | None = None,
) -> DiarizationResult:
    backend = effective_backend()
    if backend == "local":
        return _diarize_local(path, check_active, on_progress)
    if backend == "remote":
        return _diarize_remote(path, check_active, on_progress)
    raise DiarizationUnavailable("No diarization backend is available on this machine")
