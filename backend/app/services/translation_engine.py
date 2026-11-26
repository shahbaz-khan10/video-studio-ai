"""Open-source machine-translation backends (English → Urdu / Hindi).

Only one model is resident at a time: asking for a different backend unloads
the previous one first, so the app never holds two multi-GB translation models
in RAM simultaneously.

Backends
--------
indictrans2  ai4bharat/indictrans2-en-indic-dist-200M  MIT        (gated on HF: accept terms + token)
nllb         facebook/nllb-200-distilled-600M           CC-BY-NC-4.0 (non-commercial use only)
madlad       google/madlad400-3b-mt                     Apache-2.0 (3B params; GPU recommended)
remote       your own GPU worker exposing POST /translate
"""

from __future__ import annotations

import gc
import importlib.util
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from ..config import settings

ActiveCheck = Callable[[], None]


class TranslationUnavailable(Exception):
    pass


class TranslationCancelled(Exception):
    pass


BACKENDS: dict[str, dict] = {
    "indictrans2": {
        "label": "IndicTrans2 (AI4Bharat)",
        "license": "MIT",
        "commercial_use": True,
        "languages": {"ur": "urd_Arab", "hi": "hin_Deva"},
        "source": "eng_Latn",
        "params": "200M (distilled); 1B variant available",
        "ram_cpu_gb": 2.0,
        "vram_gb": 1.0,
        "download_gb": 0.9,
        "notes": "Gated on Hugging Face: accept the model terms and set HF_TOKEN before first download. Uses trust_remote_code.",
    },
    "nllb": {
        "label": "NLLB-200 distilled (Meta)",
        "license": "CC-BY-NC-4.0",
        "commercial_use": False,
        "languages": {"ur": "urd_Arab", "hi": "hin_Deva"},
        "source": "eng_Latn",
        "params": "600M",
        "ram_cpu_gb": 3.0,
        "vram_gb": 2.0,
        "download_gb": 2.5,
        "notes": "Non-commercial licence. Fine for evaluation and non-profit use; switch to IndicTrans2 or MADLAD for commercial releases.",
    },
    "madlad": {
        "label": "MADLAD-400 3B MT (Google)",
        "license": "Apache-2.0",
        "commercial_use": True,
        "languages": {"ur": "<2ur>", "hi": "<2hi>"},
        "source": None,
        "params": "3B",
        "ram_cpu_gb": 13.0,
        "vram_gb": 7.0,
        "download_gb": 11.8,
        "notes": "Commercially licensed but large; practical on a GPU (cloud worker) rather than a laptop CPU.",
    },
    "remote": {
        "label": "Remote GPU worker",
        "license": "depends on worker model",
        "commercial_use": None,
        "languages": {"ur": "ur", "hi": "hi"},
        "source": "en",
        "params": "n/a",
        "ram_cpu_gb": 0.1,
        "vram_gb": 0.0,
        "download_gb": 0.0,
        "notes": "POST {url}/translate {texts, source, target, model} -> {translations}. Self-hosted only.",
    },
}

_lock = threading.RLock()
_loaded: dict[str, Any] = {"backend": None, "model_id": None, "model": None, "tokenizer": None, "device": None}


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def transformers_available() -> bool:
    return _has_module("transformers") and _has_module("torch") and _has_module("sentencepiece")


def model_id_for(backend: str) -> str:
    return {
        "indictrans2": settings.translation_indictrans2_model,
        "nllb": settings.translation_nllb_model,
        "madlad": settings.translation_madlad_model,
        "remote": "remote",
    }.get(backend, backend)


def _cache_dir() -> Path:
    path = settings.effective_translation_cache_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def _hub_dir() -> Path:
    path = _cache_dir() / "hub"
    path.mkdir(parents=True, exist_ok=True)
    return path


def model_cached(backend: str) -> bool:
    folder = _hub_dir() / ("models--" + model_id_for(backend).replace("/", "--"))
    if not folder.is_dir():
        return False
    return any(folder.rglob("*.safetensors")) or any(folder.rglob("*.bin"))


def effective_backend() -> str:
    requested = (settings.translation_backend or "auto").lower()
    if requested == "remote":
        return "remote"
    if requested in {"indictrans2", "nllb", "madlad"}:
        return requested
    if settings.translation_remote_url:
        return "remote"
    if model_cached("indictrans2"):
        return "indictrans2"
    if model_cached("madlad") and effective_device() == "cuda":
        return "madlad"
    return "nllb"


def effective_device() -> str:
    requested = (settings.translation_device or "auto").lower()
    if requested in {"cpu", "cuda"}:
        return requested
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def engine_info() -> dict:
    backend = effective_backend()
    meta = BACKENDS[backend]
    return {
        "backend": backend,
        "model": model_id_for(backend),
        "label": meta["label"],
        "license": meta["license"],
        "commercial_use": meta["commercial_use"],
        "device": effective_device() if backend != "remote" else "remote",
        "cpu_quantize": settings.translation_cpu_quantize,
        "available": bool(settings.translation_remote_url) if backend == "remote" else transformers_available(),
        "cached": model_cached(backend) if backend != "remote" else True,
        "loaded": _loaded["backend"] == backend,
        "refiner_configured": bool(settings.translation_refiner_url),
        "refiner_model": settings.translation_refiner_model if settings.translation_refiner_url else None,
        "languages": ["ur", "hi"],
        "requirements": {
            "ram_cpu_gb": meta["ram_cpu_gb"],
            "vram_gb": meta["vram_gb"],
            "download_gb": meta["download_gb"],
            "params": meta["params"],
        },
        "notes": meta["notes"],
        "backends": {
            name: {key: value for key, value in info.items() if key != "languages"}
            for name, info in BACKENDS.items()
        },
    }


def unload() -> None:
    with _lock:
        _loaded.update({"backend": None, "model_id": None, "model": None, "tokenizer": None, "device": None})
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass


def release_other_models() -> list[str]:
    """Drop idle ASR / diarization models before loading a translation model.

    Uses a non-blocking lock acquire so a running transcription is never
    disturbed; its model is simply left in place.
    """
    released: list[str] = []
    from . import diarization, transcription

    for module, attrs in ((transcription, ("_ACTIVE_MODEL",)), (diarization, ("_ACTIVE_MODEL", "_ACTIVE_VAD"))):
        lock = getattr(module, "_MODEL_LOCK", None)
        if lock is None or not lock.acquire(blocking=False):
            continue
        try:
            for attr in attrs:
                if getattr(module, attr, None) is not None:
                    setattr(module, attr, None)
                    released.append(f"{module.__name__.rsplit('.', 1)[-1]}.{attr}")
            if hasattr(module, "_ACTIVE_KEY"):
                module._ACTIVE_KEY = None
            if hasattr(module, "_ACTIVE_VAD_KEY"):
                module._ACTIVE_VAD_KEY = None
        finally:
            lock.release()
    if released:
        gc.collect()
    return released


def _load(backend: str, check_active: ActiveCheck | None) -> tuple[Any, Any, str]:
    with _lock:
        model_id = model_id_for(backend)
        if _loaded["backend"] == backend and _loaded["model_id"] == model_id:
            return _loaded["model"], _loaded["tokenizer"], _loaded["device"]
        if not transformers_available():
            raise TranslationUnavailable(
                "Local translation needs `transformers`, `torch` and `sentencepiece`. Install backend/requirements.txt."
            )
        unload()  # never keep two translation models resident
        release_other_models()
        if check_active:
            check_active()
        os.environ.setdefault("HF_HOME", str(_cache_dir()))
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        device = effective_device()
        kwargs: dict[str, Any] = {"cache_dir": str(_hub_dir())}
        trust = backend == "indictrans2"
        try:
            if backend == "nllb":
                tokenizer = AutoTokenizer.from_pretrained(model_id, src_lang="eng_Latn", **kwargs)
            else:
                tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=trust, **kwargs)
            dtype = torch.float16 if device == "cuda" else torch.float32
            model = AutoModelForSeq2SeqLM.from_pretrained(
                model_id, trust_remote_code=trust, dtype=dtype, **kwargs
            )
        except OSError as error:
            message = str(error)
            if "gated" in message.lower() or "401" in message or "403" in message:
                raise TranslationUnavailable(
                    f"{model_id} is gated on Hugging Face. Accept its terms and set HF_TOKEN, or choose TRANSLATION_BACKEND=nllb."
                ) from error
            raise TranslationUnavailable(f"Could not load {model_id}: {message[:300]}") from error
        model.eval()
        if device == "cuda":
            model = model.to("cuda")
        elif settings.translation_cpu_quantize:
            model = torch.quantization.quantize_dynamic(model, {torch.nn.Linear}, dtype=torch.qint8)
        _loaded.update(
            {"backend": backend, "model_id": model_id, "model": model, "tokenizer": tokenizer, "device": device}
        )
        return model, tokenizer, device


def _indictrans_pre(text: str, tgt: str) -> str:
    # Minimal equivalent of IndicTransToolkit's IndicProcessor.preprocess for
    # English input: normalise quotes/spaces and prepend the language tags.
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"\s+", " ", text).strip()
    return f"eng_Latn {tgt} {text}"


def _generate_local(backend: str, texts: list[str], target: str, check_active: ActiveCheck | None) -> list[str]:
    model, tokenizer, device = _load(backend, check_active)
    import torch

    tgt = BACKENDS[backend]["languages"][target]
    if backend == "nllb":
        inputs = texts
        gen_kwargs = {"forced_bos_token_id": tokenizer.convert_tokens_to_ids(tgt)}
    elif backend == "indictrans2":
        inputs = [_indictrans_pre(text, tgt) for text in texts]
        gen_kwargs = {}
    else:  # madlad
        inputs = [f"{tgt} {text}" for text in texts]
        gen_kwargs = {}
    results: list[str] = []
    batch_size = max(1, settings.translation_batch_size)
    with _lock:
        for start in range(0, len(inputs), batch_size):
            if check_active:
                check_active()
            batch = inputs[start : start + batch_size]
            encoded = tokenizer(batch, return_tensors="pt", padding=True, truncation=True, max_length=256)
            if device == "cuda":
                encoded = {key: value.to("cuda") for key, value in encoded.items()}
            with torch.inference_mode():
                generated = model.generate(
                    **encoded,
                    num_beams=max(1, settings.translation_num_beams),
                    max_new_tokens=settings.translation_max_new_tokens,
                    no_repeat_ngram_size=4,
                    **gen_kwargs,
                )
            decoded = tokenizer.batch_decode(generated, skip_special_tokens=True, clean_up_tokenization_spaces=False)
            results.extend(item.strip() for item in decoded)
    return results


def _generate_remote(texts: list[str], target: str) -> list[str]:
    if not settings.translation_remote_url:
        raise TranslationUnavailable("TRANSLATION_REMOTE_URL is not configured.")
    headers = {}
    if settings.translation_remote_token:
        headers["Authorization"] = f"Bearer {settings.translation_remote_token}"
    url = settings.translation_remote_url.rstrip("/") + "/translate"
    try:
        response = httpx.post(
            url,
            json={"texts": texts, "source": "en", "target": target, "model": settings.translation_nllb_model},
            headers=headers,
            timeout=600,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as error:
        raise TranslationUnavailable(f"Remote translation worker failed: {error}") from error
    translations = payload.get("translations")
    if not isinstance(translations, list) or len(translations) != len(texts):
        raise TranslationUnavailable("Remote translation worker returned a malformed response.")
    return [str(item or "").strip() for item in translations]


def translate_texts(
    texts: list[str],
    target: str,
    check_active: ActiveCheck | None = None,
    backend: str | None = None,
) -> list[str]:
    """Translate English sentences to ``target`` ('ur' | 'hi'). Order preserved."""
    if target not in settings.translation_languages:
        raise TranslationUnavailable(f"Unsupported target language: {target}")
    if not texts:
        return []
    chosen = backend or effective_backend()
    if chosen == "remote":
        return _generate_remote(texts, target)
    return _generate_local(chosen, texts, target, check_active)


# ---------------------------------------------------------------------------
# Optional context refiner: a self-hosted open LLM behind an OpenAI-compatible
# /chat/completions endpoint (Ollama, llama.cpp server, vLLM, TGI).
# ---------------------------------------------------------------------------


def refine_with_llm(prompt: str) -> dict | None:
    if not settings.translation_refiner_url:
        return None
    headers = {"Content-Type": "application/json"}
    if settings.translation_refiner_token:
        headers["Authorization"] = f"Bearer {settings.translation_refiner_token}"
    url = settings.translation_refiner_url.rstrip("/") + "/chat/completions"
    body = {
        "model": settings.translation_refiner_model,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a film dubbing translator for Pakistani Urdu and Hindi. "
                    "Rewrite the draft so it sounds natural when spoken, keeps the meaning, the speaker's personality, "
                    "emotion, register and the fixed name spellings given. Adapt idioms and jokes to a cultural "
                    "equivalent instead of translating literally. Keep it about as short as the English. "
                    'Reply with JSON only: {"text": "...", "roman": "..."} (roman only for Urdu).'
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    try:
        response = httpx.post(url, json=body, headers=headers, timeout=settings.translation_refiner_timeout_s)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return None
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        return None
    try:
        import json

        parsed = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(parsed, dict) or not str(parsed.get("text") or "").strip():
        return None
    return {"text": str(parsed["text"]).strip(), "roman": (str(parsed.get("roman")).strip() if parsed.get("roman") else None)}
