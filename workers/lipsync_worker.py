"""Reference GPU worker for CINEMA AI DUB Phase 13 (LatentSync 1.5).

Runs on YOUR OWN GPU machine (local or rented) in LatentSync's own Python
environment, so its CUDA PyTorch / diffusers versions never touch the app.
Nothing here calls a paid API. Cloud GPU time is not free: budget for it.

Contract (what the app sends — see backend/app/services/lipsync_engine.py):
    POST /lipsync  multipart form
        video       mp4, the original segment frames (no audio)
        audio       wav, the dubbed line aligned to the segment
        face_boxes  JSON list per frame: [x1, y1, x2, y2] or null
        fps         frame rate of the segment
        model       requested model id (informational)
    → 200 video/mp4 with exactly the same frame count, size and rate.

Only the target face's region is cropped, lip-synced and composited back,
because LatentSync detects faces itself and could otherwise alter a different
face in a multi-person shot.

Setup (Linux + NVIDIA GPU with ≥ 8 GB VRAM, per the LatentSync 1.5 README):
    git clone https://github.com/bytedance/LatentSync && cd LatentSync
    bash setup_env.sh            # creates the env, downloads ByteDance/LatentSync-1.5
    pip install fastapi uvicorn python-multipart
    LATENTSYNC_REPO=$PWD uvicorn lipsync_worker:app --host 0.0.0.0 --port 9000
Then set on the app side:  LIPSYNC_BACKEND=remote  LIPSYNC_REMOTE_URL=http://<gpu-host>:9000

STATUS: written against LatentSync's documented CLI; not executed in the
development environment (no CUDA PyTorch / Linux toolchain there).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Header
from fastapi.responses import Response

REPO = Path(os.environ.get("LATENTSYNC_REPO", "."))
UNET_CONFIG = os.environ.get("LATENTSYNC_UNET_CONFIG", "configs/unet/stage2.yaml")
CHECKPOINT = os.environ.get("LATENTSYNC_CHECKPOINT", "checkpoints/latentsync_unet.pt")
STEPS = os.environ.get("LATENTSYNC_STEPS", "20")
GUIDANCE = os.environ.get("LATENTSYNC_GUIDANCE", "1.5")
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
TOKEN = os.environ.get("LIPSYNC_WORKER_TOKEN")

app = FastAPI(title="CINEMA AI DUB lip-sync worker (LatentSync)")


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"{cmd[0]} failed: {result.stderr[-800:]}")


def _crop_region(boxes: list, width: int, height: int) -> tuple[int, int, int, int]:
    present = [b for b in boxes if b]
    if not present:
        raise HTTPException(status_code=422, detail="No target face boxes supplied.")
    x1 = min(b[0] for b in present)
    y1 = min(b[1] for b in present)
    x2 = max(b[2] for b in present)
    y2 = max(b[3] for b in present)
    # generous margin: LatentSync needs context around the face, and even sizes
    w, h = x2 - x1, y2 - y1
    side = int(max(w, h) * 1.8)
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    side = min(side, width, height) // 2 * 2
    left = max(0, min(width - side, cx - side // 2))
    top = max(0, min(height - side, cy - side // 2))
    return left, top, side, side


@app.post("/lipsync")
async def lipsync(
    video: UploadFile = File(...),
    audio: UploadFile = File(...),
    face_boxes: str = Form(...),
    fps: str = Form(...),
    model: str = Form("latentsync-1.5"),
    authorization: str | None = Header(default=None),
):
    import hmac

    # The token travels in the Authorization header (it used to be read from the query
    # string by mistake, so every authenticated request was rejected).
    if TOKEN and not hmac.compare_digest((authorization or "").encode(), f"Bearer {TOKEN}".encode()):
        raise HTTPException(status_code=401, detail="Bad token")
    boxes = json.loads(face_boxes)
    with tempfile.TemporaryDirectory(prefix="ls-") as tmp:
        tmpdir = Path(tmp)
        src, wav = tmpdir / "src.mp4", tmpdir / "dub.wav"
        src.write_bytes(await video.read())
        wav.write_bytes(await audio.read())
        probe = subprocess.run([FFMPEG, "-hide_banner", "-i", str(src)], capture_output=True, text=True).stderr
        import re

        width, height = (int(v) for v in re.search(r"Video: .*?, (\d{2,5})x(\d{2,5})", probe).groups())
        left, top, cw, ch = _crop_region(boxes, width, height)
        crop = tmpdir / "crop.mp4"
        _run([FFMPEG, "-y", "-i", str(src), "-vf", f"crop={cw}:{ch}:{left}:{top}", "-c:v", "libx264", "-crf", "12", str(crop)])
        synced = tmpdir / "synced.mp4"
        _run([
            "python", "-m", "scripts.inference",
            "--unet_config_path", UNET_CONFIG,
            "--inference_ckpt_path", CHECKPOINT,
            "--inference_steps", STEPS,
            "--guidance_scale", GUIDANCE,
            "--video_path", str(crop),
            "--audio_path", str(wav),
            "--video_out_path", str(synced),
        ], cwd=REPO)
        out = tmpdir / "out.mp4"
        # composite the synced crop back over the untouched original frames
        _run([
            FFMPEG, "-y", "-i", str(src), "-i", str(synced),
            "-filter_complex", f"[1:v]scale={cw}:{ch}[s];[0:v][s]overlay={left}:{top}:shortest=1",
            "-r", fps, "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(out),
        ])
        return Response(out.read_bytes(), media_type="video/mp4")


@app.get("/health")
def health() -> dict:
    return {"ok": (REPO / CHECKPOINT).is_file(), "model": "latentsync-1.5", "repo": str(REPO)}
