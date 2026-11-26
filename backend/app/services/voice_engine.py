"""Local, open-source voice engines for profile metadata and real previews.

Phase 9 does not generate dubbed dialogue. It only synthesises a short preview
sentence when an engine is actually configured, and otherwise reports that no
preview is available instead of fabricating audio.
"""

import base64
import importlib.util
import json
import re
import threading
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass, fields
from pathlib import Path

import numpy as np

from ..config import PROJECT_ROOT, settings
from . import audio
from .media import AppError

CONTROL_DEFS: list[tuple[str, str]] = [
    ("voice", "Voice selection"),
    ("language", "Language"),
    ("gender", "Gender presentation"),
    ("age", "Age impression"),
    ("pitch", "Pitch"),
    ("tone", "Tone"),
    ("speed", "Speaking speed"),
    ("accent", "Accent"),
    ("intensity", "Voice intensity"),
    ("emotion", "Emotional style"),
    ("variability", "Variability / tone noise"),
    ("cadence", "Phoneme-width cadence"),
    ("speaker_id", "Speaker / take index"),
    ("volume", "Output volume"),
    ("seed", "Deterministic seed"),
    ("reference", "Consented reference sample"),
]

ENGINES: dict[str, dict] = {
    "piper": {
        "label": "Piper (OHF / piper1-gpl)",
        "license": "GPL-3.0-or-later code; each voice has its own MODEL_CARD licence",
        "commercial_use": None,
        "languages": ["en", "hi", "ur"],
        "controls": {
            "voice",
            "language",
            "gender",
            "speed",
            "variability",
            "cadence",
            "speaker_id",
            "volume",
        },
        "requirements": {
            "ram_cpu_gb": 0.5,
            "vram_gb": 0,
            "download_gb": 0.064,
            "params": "VITS / ONNX, typically 60-115 MB per voice",
        },
        "notes": (
            "CPU-first ONNX inference. Voice gender/accent are properties of the chosen voice, "
            "not runtime parameters. A sibling MODEL_CARD is required before a voice can be previewed."
        ),
        "supports_reference": False,
    },
    "mms": {
        "label": "Meta MMS TTS (VITS)",
        "license": "CC-BY-NC-4.0 weights; MIT code",
        "commercial_use": False,
        "languages": ["en", "hi", "ur"],
        "controls": {"voice", "language", "seed"},
        "requirements": {
            "ram_cpu_gb": 1.0,
            "vram_gb": 0,
            "download_gb": 0.145,
            "params": "~36M per language checkpoint",
        },
        "notes": (
            "Non-commercial licence. Opt-in only. No runtime voice cloning; a fixed seed keeps "
            "preview output reproducible."
        ),
        "supports_reference": False,
    },
    "remote": {
        "label": "Self-hosted voice worker",
        "license": "Depends on the worker you configure",
        "commercial_use": None,
        "languages": ["en", "hi", "ur"],
        "controls": {"voice", "language", "reference"},
        "requirements": {
            "ram_cpu_gb": 0,
            "vram_gb": 0,
            "download_gb": 0,
            "params": "Worker decides; no model is loaded in this process",
        },
        "notes": (
            "Optional local/r rented HTTP worker that accepts POST /synthesize. No paid API is "
            "called by default. Capabilities are read from the worker when available."
        ),
        "supports_reference": True,
    },
    "coqui": {
        "label": "Coqui XTTS-v2 (community fork)",
        "license": "MPL-2.0 code; CPML-1.0.0 non-commercial weights and outputs",
        "commercial_use": False,
        "languages": ["en", "hi"],
        "controls": {"voice", "language", "speed", "reference"},
        "requirements": {
            "ram_cpu_gb": 4.0,
            "vram_gb": 0,
            "download_gb": 2.1,
            "params": "~2.1 GB of weights",
        },
        "notes": (
            "Evaluated but not enabled: CPML forbids commercial use, Urdu is not in the official "
            "language list, and CPU latency is unverified. Metadata only in Phase 9."
        ),
        "supports_reference": True,
    },
    "none": {
        "label": "No voice engine configured",
        "license": "n/a",
        "commercial_use": None,
        "languages": [],
        "controls": set(),
        "requirements": {"ram_cpu_gb": 0, "vram_gb": 0, "download_gb": 0, "params": "n/a"},
        "notes": (
            "Profiles, consent and custom samples still work. Set VOICE_BACKEND=piper with a "
            "voice directory, 'mms' for non-commercial previews, or a self-hosted worker URL."
        ),
        "supports_reference": False,
    },
}

MMS_MODELS: dict[str, str] = {
    "en": "facebook/mms-tts-eng",
    "hi": "facebook/mms-tts-hin",
    "ur": "facebook/mms-tts-urd-script_arabic",
}

_LANG_PREFIX = {"ur": ("ur",), "hi": ("hi",), "en": ("en",)}
_LICENSE_LINE = re.compile(r"(?:^|\n)\s*(?:license|License)\s*[:=]\s*([^\n\r]+)", re.I)
_LOCK = threading.RLock()
_PIPER_CACHE: dict[str, dict] = {}
_PIPER_SCAN_KEY: tuple[str, int] | None = None
_REMOTE_CACHE: tuple[str, dict] | None = None
_ACTIVE_KEY: str | None = None
_ACTIVE: "LoadedVoice | None" = None


@dataclass
class LoadedVoice:
    engine: str
    key: str
    sample_rate: int
    handle: object
    extra: object = None

    def synthesize(
        self,
        text: str,
        params: dict,
        characteristics: dict,
        reference: bytes | None = None,
    ) -> bytes:
        raise NotImplementedError


class _PiperVoice(LoadedVoice):
    def synthesize(self, text: str, params: dict, characteristics: dict, reference: bytes | None = None) -> bytes:
        from piper import SynthesisConfig

        kwargs: dict = {
            "length_scale": float(params.get("length_scale") or 1.0),
            "noise_scale": float(params.get("noise_scale") or 0.667),
            "noise_w_scale": float(params.get("noise_w_scale") or 0.8),
        }
        if params.get("speaker_id") is not None:
            kwargs["speaker_id"] = int(params["speaker_id"])
        if params.get("volume") is not None:
            kwargs["volume"] = float(params["volume"])
        known = {item.name for item in fields(SynthesisConfig)} if hasattr(SynthesisConfig, "__dataclass_fields__") else set()
        if known:
            kwargs = {key: value for key, value in kwargs.items() if key in known}
        config = SynthesisConfig(**kwargs)
        handle = self.handle
        with audio._temp_wav_path(f"piper-{uuid_safe(self.key)}") as destination:
            handle.synthesize_wav(text, destination, syn_config=config)
            return _normalise_wav(destination, self.sample_rate)


class _MmsVoice(LoadedVoice):
    def synthesize(self, text: str, params: dict, characteristics: dict, reference: bytes | None = None) -> bytes:
        import torch

        tokenizer, model = self.handle
        seed = params.get("seed")
        if seed is not None:
            torch.manual_seed(int(seed))
        inputs = tokenizer(text, return_tensors="pt")
        with torch.no_grad():
            waveform = model(**inputs).waveform
        array = waveform.squeeze().detach().cpu().numpy().astype(np.float32)
        peak = float(np.max(np.abs(array))) if array.size else 0.0
        if peak > 0:
            array = array / max(peak, 1e-6) * 0.9
        with audio._temp_wav_path(f"mms-{uuid_safe(self.key)}") as destination:
            with wave.open(str(destination), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(self.sample_rate)
                wav_file.writeframes(np.round(array * 32767).astype("<i2").tobytes())
            return _normalise_wav(destination, self.sample_rate)


class _RemoteVoice(LoadedVoice):
    def synthesize(self, text: str, params: dict, characteristics: dict, reference: bytes | None = None) -> bytes:
        url = settings.voice_remote_url
        if not url:
            raise AppError(422, "VOICE_REMOTE_NOT_CONFIGURED", "No self-hosted voice worker is configured.")
        payload: dict = {
            "text": text,
            "voice": self.key,
            "language": params.get("language") or "ur",
            "parameters": params,
            "characteristics": characteristics,
        }
        if reference is not None:
            payload["reference_audio_base64"] = base64.b64encode(reference).decode("ascii")
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "audio/wav, application/json",
                **({"Authorization": f"Bearer {settings.voice_remote_token}"} if settings.voice_remote_token else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=settings.voice_preview_timeout_s) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise AppError(502, "VOICE_WORKER_FAILED", f"Self-hosted voice worker failed: {error}") from error
        if "json" in content_type:
            try:
                encoded = json.loads(raw.decode("utf-8")).get("audio_base64")
                if encoded:
                    raw = base64.b64decode(encoded)
            except (ValueError, AttributeError) as error:
                raise AppError(502, "VOICE_WORKER_FAILED", "Worker returned invalid JSON audio.") from error
        with audio._temp_wav_path(f"remote-{uuid_safe(self.key)}") as destination:
            destination.write_bytes(raw)
            return _normalise_wav(destination, self.sample_rate or None)


def uuid_safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)[:48]


def _normalise_wav(path: Path, sample_rate: int | None = None) -> bytes:
    converted = audio.ensure_pcm_wav(path, path.parent)
    metrics = audio.validate_audio_file(
        converted,
        expected_rate=sample_rate,
        allow_silent=False,
    )
    if not metrics["ok"]:
        raise AppError(
            422,
            "VOICE_PREVIEW_INVALID",
            "The voice engine did not produce usable audio: " + ", ".join(metrics["issues"]),
        )
    payload = converted.read_bytes()
    if converted != path:
        converted.unlink(missing_ok=True)
    return payload


def piper_dir() -> Path | None:
    configured = settings.voice_piper_models_dir
    if configured:
        return Path(configured).expanduser()
    fallback = settings.effective_voice_cache_dir / "piper"
    return fallback if fallback.is_dir() else None


def package_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def effective_backend() -> str:
    selected = (settings.voice_backend or "auto").strip().lower()
    if selected == "auto":
        if piper_dir() and package_available("piper"):
            return "piper"
        if settings.voice_remote_url:
            return "remote"
        return "none"
    return selected if selected in ENGINES else "none"


def _voice_language(code: str, name: str) -> str:
    combined = f"{code} {name}".lower()
    for language, prefixes in _LANG_PREFIX.items():
        if any(combined.startswith(prefix) or f"-{prefix}" in combined for prefix in prefixes):
            return language
    return code.split("_")[0].lower() or "en"


def _license_from_card(card: Path) -> str:
    if not card.is_file():
        return "unknown"
    text = card.read_text(encoding="utf-8", errors="ignore")
    match = _LICENSE_LINE.search(text)
    return match.group(1).strip() if match else "unknown"


def scan_piper_voices(force: bool = False) -> list[dict]:
    global _PIPER_CACHE, _PIPER_SCAN_KEY
    directory = piper_dir()
    if not directory or not directory.is_dir():
        _PIPER_CACHE.clear()
        _PIPER_SCAN_KEY = None
        return []
    signature = (str(directory), int(directory.stat().st_mtime_ns))
    if not force and _PIPER_SCAN_KEY == signature and _PIPER_CACHE:
        return list(_PIPER_CACHE.values())
    voices: dict[str, dict] = {}
    for model in sorted(directory.glob("*.onnx")):
        config_path = model.with_suffix(".onnx.json")
        if not config_path.is_file():
            continue
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = model.stem
        code = str(((config.get("language") or {}).get("code")) or "")
        sample_rate = int(((config.get("audio") or {}).get("sample_rate")) or 0) or None
        num_speakers = int(config.get("num_speakers") or 1)
        speaker_map = config.get("speaker_id_map") or {}
        card = directory / "MODEL_CARD" if (directory / "MODEL_CARD").is_file() else model.with_suffix(".MODEL_CARD")
        if not card.is_file():
            card = model.parent / f"{key}.MODEL_CARD"
        license_name = _license_from_card(card)
        commercial = None
        lowered = license_name.lower()
        if "non-commercial" in lowered or "nc" in lowered.split("-") or "by-nc" in lowered:
            commercial = False
        elif any(token in lowered for token in ("mit", "apache", "cc0", "cc by 4", "cc-by-4")):
            commercial = True
        name_lower = key.lower()
        gender = "female" if "female" in name_lower or "woman" in name_lower else "male" if "male" in name_lower or "man" in name_lower else "unknown"
        quality = key.rsplit("-", 1)[-1] if "-" in key else None
        voices[key] = {
            "id": key,
            "label": key.replace("-", " ").replace("_", " "),
            "language": _voice_language(code, key),
            "gender": gender,
            "quality": quality if quality in {"x_low", "low", "medium", "high"} else None,
            "sample_rate": sample_rate,
            "license": license_name,
            "commercial_use": commercial,
            "cached": True,
            "preview_available": package_available("piper") and license_name != "unknown",
            "supports_reference": False,
            "supports_speaker_id": num_speakers > 1 or bool(speaker_map),
            "source": "local",
            "path": str(model),
            "num_speakers": num_speakers,
            "speaker_map": speaker_map if isinstance(speaker_map, dict) else {},
        }
    _PIPER_CACHE = voices
    _PIPER_SCAN_KEY = signature
    return list(voices.values())


def mms_voices() -> list[dict]:
    available = package_available("transformers")
    out = []
    for language, model_id in MMS_MODELS.items():
        cached = _hf_cached(model_id)
        out.append(
            {
                "id": model_id,
                "label": f"MMS TTS {language.upper()} ({model_id.rsplit('/', 1)[-1]})",
                "language": language,
                "gender": "unknown",
                "quality": None,
                "sample_rate": 16000,
                "license": "CC-BY-NC-4.0",
                "commercial_use": False,
                "cached": cached,
                "preview_available": available,
                "supports_reference": False,
                "supports_speaker_id": False,
                "source": "huggingface",
            }
        )
    return out


def _hf_cached(model_id: str) -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache

        for filename in ("model.safetensors", "pytorch_model.bin"):
            if try_to_load_from_cache(model_id, filename) is not None:
                return True
    except Exception:
        pass
    local = settings.effective_voice_cache_dir / "hf" / model_id.replace("/", "--")
    return local.is_dir()


def _remote_info(force: bool = False) -> dict:
    global _REMOTE_CACHE
    url = settings.voice_remote_url
    if not url:
        return {"available": False, "voices": [], "capabilities": set(), "notes": "No worker URL configured."}
    if not force and _REMOTE_CACHE and _REMOTE_CACHE[0] == url:
        return _REMOTE_CACHE[1]
    headers = {"Accept": "application/json"}
    if settings.voice_remote_token:
        headers["Authorization"] = f"Bearer {settings.voice_remote_token}"
    voices: list[dict] = []
    capabilities: set[str] = set()
    notes = "Self-hosted worker configured."
    try:
        request = urllib.request.Request(f"{url.rstrip('/')}/voices", headers=headers)
        with urllib.request.urlopen(request, timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
        for entry in data if isinstance(data, list) else data.get("voices", []):
            if isinstance(entry, str):
                entry = {"id": entry, "name": entry}
            voices.append(
                {
                    "id": str(entry.get("id") or entry.get("voice") or ""),
                    "label": str(entry.get("name") or entry.get("label") or entry.get("id") or "voice"),
                    "language": str(entry.get("language") or "ur"),
                    "gender": str(entry.get("gender") or "unknown"),
                    "quality": entry.get("quality"),
                    "sample_rate": entry.get("sample_rate"),
                    "license": str(entry.get("license") or "see worker documentation"),
                    "commercial_use": entry.get("commercial_use"),
                    "cached": True,
                    "preview_available": True,
                    "supports_reference": bool(entry.get("supports_reference", True)),
                    "supports_speaker_id": bool(entry.get("supports_speaker_id", False)),
                    "source": "worker",
                }
            )
        voices = [voice for voice in voices if voice["id"]]
        notes = f"Worker reachable at {url}."
    except Exception as error:
        notes = f"Worker at {url} is not reachable: {error}"
    try:
        request = urllib.request.Request(f"{url.rstrip('/')}/capabilities", headers=headers)
        with urllib.request.urlopen(request, timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
        raw = data.get("capabilities", data) if isinstance(data, dict) else data
        if isinstance(raw, list):
            capabilities = {str(item) for item in raw}
    except Exception:
        capabilities = set()
    result = {"available": bool(voices) or "reachable" in notes, "voices": voices, "capabilities": capabilities, "notes": notes}
    _REMOTE_CACHE = (url, result)
    return result


def voices(engine: str | None = None, force: bool = False) -> list[dict]:
    selected = engine or effective_backend()
    if selected == "piper":
        return scan_piper_voices(force=force)
    if selected == "mms":
        return mms_voices()
    if selected == "remote":
        return _remote_info(force=force)["voices"]
    return []


def capabilities(engine: str | None = None, voice_id: str | None = None) -> set[str]:
    selected = engine or effective_backend()
    meta = ENGINES.get(selected, ENGINES["none"])
    allowed = set(meta["controls"])
    if selected == "remote":
        allowed |= _remote_info()["capabilities"]
    if voice_id and selected == "piper":
        option = next((item for item in scan_piper_voices() if item["id"] == voice_id), None)
        if option and option.get("num_speakers", 1) <= 1:
            allowed.discard("speaker_id")
        if option and option["gender"] != "unknown":
            allowed.discard("gender")
    return allowed


def find_voice(engine: str, voice_id: str | None, language: str) -> dict | None:
    options = voices(engine)
    if voice_id:
        return next((item for item in options if item["id"] == voice_id), None)
    return next((item for item in options if item["language"] == language), None)


def engine_info() -> dict:
    selected = effective_backend()
    meta = ENGINES[selected]
    engine_voices = voices(selected)
    supported = capabilities(selected)
    available = False
    if selected == "piper":
        available = package_available("piper") and bool(engine_voices)
    elif selected == "mms":
        available = package_available("transformers")
    elif selected == "remote":
        available = _remote_info()["available"]
    notes = meta["notes"]
    if selected == "remote" or selected == "none":
        notes = _remote_info()["notes"] if selected == "remote" else meta["notes"]
    with _LOCK:
        loaded = _ACTIVE is not None and _ACTIVE.engine == selected
    return {
        "backend": settings.voice_backend,
        "selected": selected,
        "label": meta["label"],
        "license": meta["license"],
        "commercial_use": meta["commercial_use"],
        "device": settings.voice_device,
        "available": available,
        "loaded": loaded,
        "languages": list(meta["languages"]),
        "voices": [
            {key: value for key, value in option.items() if key != "path"}
            for option in engine_voices
        ],
        "capabilities": [
            {"key": key, "label": label, "supported": key in supported}
            for key, label in CONTROL_DEFS
        ],
        "requirements": dict(meta["requirements"]),
        "notes": notes,
        "consent_required": True,
    }


def preview_available(engine: str, voice_id: str | None, language: str) -> bool:
    option = find_voice(engine, voice_id, language)
    if not option or not option.get("preview_available"):
        return False
    if engine == "piper" and not option.get("path"):
        return False
    if engine == "remote" and not settings.voice_remote_url:
        return False
    if engine == "coqui":
        return False
    return True


def _release_active() -> None:
    global _ACTIVE, _ACTIVE_KEY
    _ACTIVE = None
    _ACTIVE_KEY = None


def unload() -> None:
    with _LOCK:
        _release_active()


def load(engine: str, voice_id: str | None, language: str, char_id: str | None = None) -> LoadedVoice:
    global _ACTIVE, _ACTIVE_KEY
    selected = engine or effective_backend()
    if selected == "coqui" or selected == "none":
        raise AppError(422, "VOICE_PREVIEW_UNAVAILABLE", f"Engine '{selected}' cannot synthesise previews.")
    option = find_voice(selected, voice_id, language)
    if not preview_available(selected, voice_id, language):
        reason = "No usable voice is configured for this language."
        if option and option.get("license") == "unknown":
            reason = "This voice has no MODEL_CARD, so its licence could not be verified."
        raise AppError(422, "VOICE_PREVIEW_UNAVAILABLE", reason)
    key = f"{selected}:{option['id']}"
    with _LOCK:
        if _ACTIVE_KEY == key and _ACTIVE is not None:
            return _ACTIVE
        _release_active()
        if selected == "piper":
            from piper import PiperVoice

            handle = PiperVoice.load(option["path"], use_cuda=False)
            loaded = _PiperVoice(selected, key, int(option["sample_rate"] or 22050), handle)
        elif selected == "mms":
            import torch
            from transformers import AutoTokenizer, VitsModel

            model_id = option["id"]
            tokenizer = AutoTokenizer.from_pretrained(model_id)
            model = VitsModel.from_pretrained(model_id)
            model.eval()
            if settings.voice_device == "cpu" or not torch.cuda.is_available():
                model.to("cpu")
            loaded = _MmsVoice(selected, key, int(model.config.sampling_rate), (tokenizer, model))
        elif selected == "remote":
            loaded = _RemoteVoice(selected, key, int(option.get("sample_rate") or 22050), None)
        else:
            raise AppError(422, "VOICE_PREVIEW_UNAVAILABLE", "Unsupported voice engine.")
        _ACTIVE = loaded
        _ACTIVE_KEY = key
        return loaded


def synthesise_preview(
    text: str,
    engine: str,
    voice_id: str | None,
    language: str,
    params: dict,
    characteristics: dict,
    reference: bytes | None = None,
    char_id: str | None = None,
) -> bytes:
    """Return real PCM WAV bytes. Never returns placeholder or synthetic silence."""
    if len(text) > settings.voice_preview_max_chars:
        raise AppError(422, "VOICE_TEXT_TOO_LONG", "Preview text is too long.")
    loaded = load(engine, voice_id, language, char_id)
    return loaded.synthesize(text, params, characteristics, reference)


def reset_remote_cache() -> None:
    global _REMOTE_CACHE
    _REMOTE_CACHE = None


def vendored_voice_catalog() -> list[dict]:
    """Documented catalog entries; no weights are bundled or downloaded by default."""
    return [
        {"id": "ur_PK-aegis_female-medium", "language": "ur", "gender": "female", "license": "MIT (verify MODEL_CARD)"},
        {"id": "ur_PK-fasih-medium", "language": "ur", "gender": "male", "license": "MIT (verify MODEL_CARD, base Hi_IN model)"},
        {"id": "hi_IN-pratham-medium", "language": "hi", "gender": "male", "license": "CC BY-NC-SA 4.0"},
        {"id": "hi_IN-priyamvada-medium", "language": "hi", "gender": "female", "license": "CC BY-NC-SA 4.0"},
        {"id": "hi_IN-rohan-medium", "language": "hi", "gender": "male", "license": "IITM custom licence"},
    ]
