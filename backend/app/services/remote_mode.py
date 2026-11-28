"""Phase 15 — remote processing mode.

PROCESSING_REMOTE_URL / PROCESSING_REMOTE_TOKEN point the app at a separately
configured AI worker server (app/ai_worker.py). When set, translation, lip
sync and remote voice profiles run there; the app never contacts any service
on its own, and nothing is provisioned or paid for automatically.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from pydantic_settings import BaseSettings, SettingsConfigDict

from ..config import PROJECT_ROOT, settings


class RemoteSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PROCESSING_", env_file=str(PROJECT_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    remote_url: str | None = None
    remote_token: str | None = None
    remote_timeout_s: float = 5.0


remote_settings = RemoteSettings()
REMOTE_WORKER_ID = "remote-ai-worker"


def enabled() -> bool:
    return bool(remote_settings.remote_url)


def apply() -> dict:
    """Route the phases' remote backends to the configured AI worker server."""
    if not enabled():
        return {"enabled": False}
    from . import lipsync_engine

    base = remote_settings.remote_url.rstrip("/")
    token = remote_settings.remote_token
    settings.translation_remote_url = base
    settings.translation_remote_token = token
    settings.voice_remote_url = f"{base}/voice"
    settings.voice_remote_token = token
    lipsync_engine.lipsync_settings.remote_url = base
    lipsync_engine.lipsync_settings.remote_token = token
    return {"enabled": True, "url": base, "routes": ["translation", "lipsync", "voice profiles (engine=remote)"]}


def health() -> dict:
    """Ask the remote worker how it is doing (never raises)."""
    if not enabled():
        return {"reachable": False, "error": "not configured"}
    request = urllib.request.Request(
        remote_settings.remote_url.rstrip("/") + "/health",
        headers={"Authorization": f"Bearer {remote_settings.remote_token or ''}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=remote_settings.remote_timeout_s) as response:
            return {"reachable": True, **json.loads(response.read().decode("utf-8"))}
    except urllib.error.HTTPError as error:
        return {"reachable": False, "error": f"HTTP {error.code} (check PROCESSING_REMOTE_TOKEN)"}
    except Exception as error:  # noqa: BLE001
        return {"reachable": False, "error": str(error)[:200]}


def refresh_worker_row() -> dict:
    """Record the remote worker in the worker registry with its latest health."""
    from ..database import SessionLocal
    from ..models import utcnow
    from ..models_workers import ProcessingWorker

    info = health()
    if not enabled():
        return info
    with SessionLocal() as db:
        row = db.get(ProcessingWorker, REMOTE_WORKER_ID)
        if row is None:
            row = ProcessingWorker(id=REMOTE_WORKER_ID, name="Remote AI worker", kind="remote_http", classes=["ai", "gpu?"])
            db.add(row)
        row.url = remote_settings.remote_url
        row.classes = ["ai", "remote"] + (["gpu"] if (info.get("resources") or {}).get("gpu", {}) and
                                           (info.get("resources") or {}).get("gpu", {}).get("usable_by_app_pytorch") else [])
        row.status = "online" if info.get("reachable") else "offline"
        row.resources_json = info.get("resources") or {"error": info.get("error")}
        if info.get("reachable"):
            row.last_heartbeat_at = utcnow()
        db.commit()
    return info
