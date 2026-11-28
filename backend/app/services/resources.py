"""Phase 15 — resource monitoring, model residency and temporary-file cleanup.

Models are loaded lazily by each service and only one heavy model per service
is ever resident. This module reports what is loaded, unloads idle models,
and removes abandoned temporary data that this application created.
"""

from __future__ import annotations

import gc
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings

# Temporary names created by this application (see tempfile calls in services/routers).
TEMP_PREFIXES = (
    "cinema-", "character-faces-", "diarization-decode-", "lipsync-", "mixprev-", "timeline-export-",
    "voice-sample-", "translation-", "dub-",
)

# Service modules that keep one resident model, and how to read/clear it.
MODEL_SLOTS = {
    "speech_recognition": ("app.services.transcription", ("_ACTIVE_MODEL",), "_ACTIVE_KEY", "_MODEL_LOCK"),
    "speaker_diarization": ("app.services.diarization", ("_ACTIVE_MODEL", "_ACTIVE_VAD"), "_ACTIVE_KEY", "_MODEL_LOCK"),
    "voice_profile_preview": ("app.services.voice_engine", ("_ACTIVE",), "_ACTIVE_KEY", "_LOCK"),
}
DICT_SLOTS = {
    "translation": ("app.services.translation_engine", "_loaded", ("model", "tokenizer"), "backend"),
    "voice_generation": ("app.services.tts_engine", "_loaded", ("model", "tokenizer"), "key"),
    "lip_sync": ("app.services.lipsync_engine", "_loaded", ("generator", "syncnet"), "device"),
}

_gpu_cache: tuple[float, dict | None] = (0.0, None)


def gpu_info() -> dict | None:
    """NVIDIA GPU name/memory/utilisation via nvidia-smi (cached 10 s). None if absent."""
    global _gpu_cache
    if time.monotonic() - _gpu_cache[0] < 10:
        return _gpu_cache[1]
    info = None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            name, total, used, util = [part.strip() for part in out.stdout.strip().splitlines()[0].split(",")]
            info = {"name": name, "memory_total_mb": int(total), "memory_used_mb": int(used), "utilization_pct": int(util)}
    except Exception:  # noqa: BLE001
        info = None
    try:
        import torch  # noqa: F401 — only if already importable

        cuda = bool(sys.modules.get("torch") and sys.modules["torch"].cuda.is_available())
    except Exception:  # noqa: BLE001
        cuda = False
    if info is not None:
        info["usable_by_app_pytorch"] = cuda
    _gpu_cache = (time.monotonic(), info)
    return info


def loaded_models() -> list[dict]:
    """Models currently resident in THIS process (modules never imported are skipped)."""
    out = []
    for name, (module_name, attrs, key_attr, _lock) in MODEL_SLOTS.items():
        module = sys.modules.get(module_name)
        if module is None:
            continue
        if any(getattr(module, attr, None) is not None for attr in attrs):
            out.append({"slot": name, "key": str(getattr(module, key_attr, None) or "")})
    for name, (module_name, dict_attr, fields, key) in DICT_SLOTS.items():
        module = sys.modules.get(module_name)
        state = getattr(module, dict_attr, None) if module else None
        if isinstance(state, dict) and any(state.get(field) is not None for field in fields):
            out.append({"slot": name, "key": str(state.get(key) or "")})
    return out


def unload_models(reason: str = "manual") -> list[str]:
    """Release every idle resident model. A model that is busy (lock held) is left alone."""
    released: list[str] = []
    for name, (module_name, attrs, key_attr, lock_attr) in MODEL_SLOTS.items():
        module = sys.modules.get(module_name)
        if module is None:
            continue
        lock = getattr(module, lock_attr, None)
        if lock is not None and not lock.acquire(blocking=False):
            continue
        try:
            if any(getattr(module, attr, None) is not None for attr in attrs):
                for attr in attrs:
                    setattr(module, attr, None)
                if hasattr(module, key_attr):
                    setattr(module, key_attr, None)
                released.append(name)
        finally:
            if lock is not None:
                lock.release()
    for name, (module_name, dict_attr, fields, _key) in DICT_SLOTS.items():
        module = sys.modules.get(module_name)
        state = getattr(module, dict_attr, None) if module else None
        lock = getattr(module, "_lock", None) if module else None
        if not isinstance(state, dict) or not any(state.get(field) is not None for field in fields):
            continue
        if lock is not None and not lock.acquire(blocking=False):
            continue
        try:
            module.unload()
            released.append(name)
        finally:
            if lock is not None:
                lock.release()
    if released:
        gc.collect()
        torch = sys.modules.get("torch")
        try:
            if torch is not None and torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
    return released


def _dir_size(path: Path, limit: int = 200_000) -> int:
    total = 0
    for count, (root, _dirs, files) in enumerate(os.walk(path)):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
        if count > limit:
            break
    return total


def snapshot(include_models: bool = True, light: bool = False) -> dict:
    data: dict = {"pid": os.getpid(), "time": datetime.now(timezone.utc).isoformat()}
    try:
        import psutil

        proc = psutil.Process()
        vm = psutil.virtual_memory()
        data.update({
            "cpu_count": psutil.cpu_count(logical=True),
            "cpu_percent": psutil.cpu_percent(interval=None),
            "ram_total_mb": int(vm.total / 1e6),
            "ram_available_mb": int(vm.available / 1e6),
            "ram_percent": vm.percent,
            "process_rss_mb": int(proc.memory_info().rss / 1e6),
        })
        disk = psutil.disk_usage(str(settings.data_dir))
        data.update({"disk_total_gb": round(disk.total / 1e9, 1), "disk_free_gb": round(disk.free / 1e9, 1)})
    except Exception as error:  # noqa: BLE001
        data["psutil_error"] = str(error)[:120]
    data["gpu"] = gpu_info()
    if include_models:
        data["models_loaded"] = loaded_models()
    if not light:
        artifacts = settings.data_dir / "artifacts"
        models = settings.effective_model_cache_dir
        data["storage"] = {
            "artifacts_gb": round(_dir_size(artifacts) / 1e9, 3) if artifacts.exists() else 0.0,
            "model_cache_gb": round(_dir_size(models) / 1e9, 3) if models.exists() else 0.0,
        }
        data["temp"] = temp_usage()
    return data


def _our_temp_entries() -> list[Path]:
    root = Path(tempfile.gettempdir())
    try:
        return [entry for entry in root.iterdir() if entry.name.startswith(TEMP_PREFIXES)]
    except OSError:
        return []


def temp_usage() -> dict:
    entries = _our_temp_entries()
    size = sum(_dir_size(e) if e.is_dir() else (e.stat().st_size if e.exists() else 0) for e in entries)
    return {"entries": len(entries), "size_mb": round(size / 1e6, 1)}


def cleanup_temp(dry_run: bool = True, older_than_hours: float | None = None) -> dict:
    """Remove this app's temporary files older than the retention period and expired upload sessions.

    Never touches uploaded originals, artifacts or anything not created by this application.
    """
    from .job_queue import processing_settings

    hours = processing_settings.temp_retention_hours if older_than_hours is None else older_than_hours
    cutoff = time.time() - hours * 3600
    removed, freed = [], 0
    for entry in _our_temp_entries():
        try:
            if entry.stat().st_mtime > cutoff:
                continue
            size = _dir_size(entry) if entry.is_dir() else entry.stat().st_size
            if not dry_run:
                shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink(missing_ok=True)
            removed.append(entry.name)
            freed += size
        except OSError:
            continue
    sessions = _expired_upload_sessions(dry_run)
    return {"dry_run": dry_run, "older_than_hours": hours, "temp_entries": removed, "freed_mb": round(freed / 1e6, 2),
            "expired_upload_sessions": sessions}


def _expired_upload_sessions(dry_run: bool) -> int:
    from sqlalchemy import select

    from ..database import SessionLocal
    from ..models import UploadSession
    from .storage import storage

    now = datetime.now(timezone.utc)
    count = 0
    with SessionLocal() as db:
        for session in db.scalars(select(UploadSession).where(UploadSession.status.in_(["pending", "uploading", "assembling"]))):
            expires = session.expires_at if session.expires_at.tzinfo else session.expires_at.replace(tzinfo=timezone.utc)
            if expires >= now:
                continue
            count += 1
            if not dry_run:
                storage.delete_upload_session(session.project_id, session.id)
                session.status = "expired"
        if not dry_run:
            db.commit()
    return count
