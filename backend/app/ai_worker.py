"""Phase 15 — remote AI processing server.

Deploy this on a separately provisioned machine (for example a rented GPU
server) from the same codebase, then point the main app at it:

    # on the processing machine
    REMOTE_WORKER_TOKEN=<long random secret> python -m uvicorn app.ai_worker:app --host 0.0.0.0 --port 9100
    # on the main app
    PROCESSING_REMOTE_URL=https://<processing-host>:9100  PROCESSING_REMOTE_TOKEN=<same secret>

It implements the HTTP contracts the app's phases already use for remote
workers, so heavy inference leaves the app host while orchestration, storage
and the database stay there:

    POST /translate            Phase 8   {texts, source, target} → {translations}
    POST /voice                Phase 9   {text, voice, language, parameters, characteristics} → audio/wav
    GET  /voice/voices         Phase 9   voice list
    GET  /voice/capabilities   Phase 9   supported controls
    POST /lipsync              Phase 13  multipart video + audio + face_boxes → video/mp4
    GET  /health                         models loaded, CPU/RAM/GPU

Every call needs "Authorization: Bearer <REMOTE_WORKER_TOKEN>". Put TLS in
front (reverse proxy) when crossing the internet. Nothing here calls a paid
service; GPU hosting itself is not free.
"""

from __future__ import annotations

import hmac
import io
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

TOKEN = os.environ.get("REMOTE_WORKER_TOKEN", "")
MAX_UPLOAD_MB = int(os.environ.get("REMOTE_WORKER_MAX_UPLOAD_MB", "512"))
ALLOW_NO_TOKEN = os.environ.get("REMOTE_WORKER_ALLOW_NO_TOKEN", "") == "1"  # local experiments only

app = FastAPI(title="CINEMA AI DUB — remote AI worker", version="0.8.0-phase15")
_busy = threading.Semaphore(1)  # one heavy model job at a time on this box
_stats = {"requests": 0, "started": time.time(), "last_request": None}


def require_token(authorization: str | None = Header(default=None)) -> None:
    if not TOKEN:
        if ALLOW_NO_TOKEN:
            return
        raise HTTPException(status_code=503, detail="REMOTE_WORKER_TOKEN is not set on the worker.")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, TOKEN):
        raise HTTPException(status_code=401, detail="Invalid worker token.")


@app.middleware("http")
async def limit_size(request: Request, call_next):
    length = int(request.headers.get("content-length") or 0)
    if length > MAX_UPLOAD_MB * 1024 * 1024:
        return JSONResponse({"detail": f"Upload exceeds {MAX_UPLOAD_MB} MB."}, status_code=413)
    _stats["requests"] += 1
    _stats["last_request"] = time.time()
    return await call_next(request)


@app.get("/health")
def health(_auth: None = Depends(require_token)) -> dict:
    from .services import lipsync_engine, resources, translation_engine, tts_engine

    return {
        "ok": True,
        "role": "remote_ai_worker",
        "capabilities": ["translate", "voice", "lipsync"],
        "translation": {"backend": translation_engine.effective_backend() if translation_engine.effective_backend() != "remote" else "nllb"},
        "lipsync": {"wav2lip_available": lipsync_engine.wav2lip_available()},
        "voice": {"engine": "mms", "available": tts_engine.mms_available()},
        "resources": resources.snapshot(include_models=True, light=True),
        "requests": _stats["requests"],
        "uptime_s": int(time.time() - _stats["started"]),
    }


# ---------------------------------------------------------------- translation


@app.post("/translate")
def translate(payload: dict, _auth: None = Depends(require_token)) -> dict:
    from .services import translation_engine

    texts = [str(t) for t in payload.get("texts") or []]
    target = str(payload.get("target") or "")
    if target not in {"ur", "hi"}:
        raise HTTPException(status_code=422, detail="target must be ur or hi")
    if len(texts) > 512:
        raise HTTPException(status_code=413, detail="At most 512 texts per request.")
    backend = translation_engine.effective_backend()
    if backend == "remote":  # never forward to ourselves
        backend = "indictrans2" if translation_engine.model_cached("indictrans2") else "nllb"
    with _busy:
        out = translation_engine.translate_texts(texts, target, backend=backend)
    return {"translations": out, "backend": backend, "model": translation_engine.model_id_for(backend)}


# ---------------------------------------------------------------- voice (Phase 9 contract)


@app.get("/voice/voices")
def voices(_auth: None = Depends(require_token)) -> list[dict]:
    return [
        {"id": "mms-ur", "name": "MMS Urdu (male-sounding)", "language": "ur", "gender": "male", "license": "CC-BY-NC-4.0",
         "commercial_use": False, "sample_rate": 24000, "supports_reference": False},
        {"id": "mms-hi", "name": "MMS Hindi (male-sounding)", "language": "hi", "gender": "male", "license": "CC-BY-NC-4.0",
         "commercial_use": False, "sample_rate": 24000, "supports_reference": False},
    ]


@app.get("/voice/capabilities")
def voice_capabilities(_auth: None = Depends(require_token)) -> dict:
    return {"capabilities": ["voice", "language", "speed", "seed"]}


@app.post("/voice")
def synthesize(payload: dict, _auth: None = Depends(require_token)) -> Response:
    from .services import tts_engine

    text = str(payload.get("text") or "").strip()
    language = str(payload.get("language") or (payload.get("parameters") or {}).get("language") or "ur")
    if not text or language not in {"ur", "hi"}:
        raise HTTPException(status_code=422, detail="text and language (ur|hi) are required")
    params = payload.get("parameters") or {}
    speed = 1.0
    if params.get("length_scale"):
        speed = 1.0 / float(params["length_scale"])
    render = tts_engine.VoiceRender(synth="builtin", language=language, model_id=tts_engine.builtin_model(language),
                                    native=["speed", "seed"], speed=max(0.5, min(2.0, speed)), seed=int(params.get("seed") or 0))
    with _busy:
        rendered = tts_engine.render_line(text[:2000], render)
    return Response(rendered.wav, media_type="audio/wav")


# ---------------------------------------------------------------- lip sync (Phase 13 contract)


def _probe(path: Path) -> tuple[int, int, float, int]:
    import re

    from .services.tts_engine import ffmpeg_binary

    info = subprocess.run([ffmpeg_binary(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    size = re.search(r"Video: .*?, (\d{2,5})x(\d{2,5})", info)
    fps = re.search(r"([\d.]+) fps", info)
    frames = subprocess.run([ffmpeg_binary(), "-hide_banner", "-i", str(path), "-map", "0:v:0", "-c", "copy", "-f", "null", "-"],
                            capture_output=True, text=True).stderr
    count = re.findall(r"frame=\s*(\d+)", frames)
    if not size or not fps:
        raise HTTPException(status_code=422, detail="Could not read the uploaded video.")
    return int(size.group(1)), int(size.group(2)), float(fps.group(1)), int(count[-1]) if count else 0


@app.post("/lipsync")
async def lipsync(
    video: UploadFile = File(...),
    audio: UploadFile = File(...),
    face_boxes: str = Form(...),
    fps: str = Form(...),
    model: str = Form("wav2lip"),
    _auth: None = Depends(require_token),
) -> Response:
    from .services import lipsync_engine as engine
    from .services.lipsync import _resample
    from .services.tts_engine import read_wav

    boxes = [tuple(b) if b else None for b in json.loads(face_boxes)]
    with tempfile.TemporaryDirectory(prefix="lipsync-remote-") as tmp:
        tmpdir = Path(tmp)
        src, wav, out = tmpdir / "segment.mp4", tmpdir / "dub.wav", tmpdir / "out.mp4"
        src.write_bytes(await video.read())
        wav.write_bytes(await audio.read())
        width, height, probed_fps, frames = _probe(src)
        rate = float(fps) if fps else probed_fps
        count = min(frames or len(boxes), len(boxes))
        samples, sr = read_wav(wav)
        audio16 = _resample(samples, sr, 16000) if sr != 16000 else samples
        with _busy:
            engine.generate_wav2lip(src, 0, count, rate, width, height, boxes[:count], audio16, wav, out)
        return Response(out.read_bytes(), media_type="video/mp4", headers={"X-Model": "wav2lip-gan"})
