"""Phase 15 — authentication, project authorisation and response hygiene.

AUTH_MODE=off       local development: no sign-in (refused when ENVIRONMENT=production)
AUTH_MODE=password  accounts with scrypt-hashed passwords and a signed, HttpOnly
                    session cookie (also accepted as "Authorization: Bearer").

Every /api/v1 request is authenticated; any id in the URL that belongs to a
project (project, video, job, take, placement, result, ...) must belong to a
project the user is a member of. Unknown ids fall through to the route (404).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select, text

from ..config import PROJECT_ROOT, settings
from ..database import Base, SessionLocal


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUTH_", env_file=str(PROJECT_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    mode: str = "off"  # off | password
    secret_key: str | None = None
    session_hours: float = 168.0
    allow_registration: bool = False  # the very first account can always be created (becomes admin)
    cookie_secure: bool | None = None  # default: true when ENVIRONMENT=production (HTTPS)
    # Required to create the first (admin) account when ENVIRONMENT=production, so whoever
    # reaches a fresh deployment first cannot claim it. Or use: python -m app.manage create-admin
    bootstrap_token: str | None = None
    login_max_failures: int = 8
    login_window_s: int = 900
    allow_insecure_production: bool = False


auth_settings = AuthSettings()
COOKIE = "cinema_session"
PUBLIC_PATHS = {"/api/v1/health", "/api/v1/auth/login", "/api/v1/auth/logout", "/api/v1/auth/status", "/api/v1/auth/register"}
ADMIN_PREFIXES = ("/api/v1/processing/workers", "/api/v1/processing/resources", "/api/v1/processing/models",
                  "/api/v1/processing/cleanup", "/api/v1/processing/recover", "/api/v1/processing/status",
                  "/api/v1/processing/remote")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def enabled() -> bool:
    return (auth_settings.mode or "off").lower() == "password"


def check_configuration() -> None:
    """Refuse an unsafe production start."""
    if settings.environment.lower() == "production" and not enabled() and not auth_settings.allow_insecure_production:
        raise RuntimeError("ENVIRONMENT=production requires AUTH_MODE=password (or AUTH_ALLOW_INSECURE_PRODUCTION=true).")
    if enabled() and not auth_settings.secret_key:
        raise RuntimeError("AUTH_MODE=password requires AUTH_SECRET_KEY (e.g. python -c \"import secrets;print(secrets.token_urlsafe(48))\").")
    if enabled() and len(auth_settings.secret_key or "") < 32:
        raise RuntimeError("AUTH_SECRET_KEY must be at least 32 characters.")


# ---------------------------------------------------------------------------
# Passwords and session tokens
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _alg, n, r, p, salt, digest = stored.split("$")
        candidate = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(candidate, base64.b64decode(digest))
    except Exception:  # noqa: BLE001
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text_: str) -> bytes:
    return base64.urlsafe_b64decode(text_ + "=" * (-len(text_) % 4))


def make_token(user_id: str, version: int) -> str:
    payload = _b64(json.dumps({"u": user_id, "v": version, "e": int(time.time() + auth_settings.session_hours * 3600)}).encode())
    sig = _b64(hmac.new((auth_settings.secret_key or "").encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read_token(token: str) -> dict | None:
    try:
        payload, sig = token.split(".")
        expected = _b64(hmac.new((auth_settings.secret_key or "").encode(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(_unb64(payload))
        return data if data.get("e", 0) > time.time() else None
    except Exception:  # noqa: BLE001
        return None


@dataclass
class CurrentUser:
    id: str
    email: str
    is_admin: bool
    display_name: str | None


def user_from_request(request) -> CurrentUser | None:
    from ..models_workers import UserAccount

    token = request.cookies.get(COOKIE)
    header = request.headers.get("authorization", "")
    if not token and header.lower().startswith("bearer "):
        token = header[7:].strip()
    data = read_token(token) if token else None
    if not data:
        return None
    with SessionLocal() as db:
        user = db.get(UserAccount, data["u"])
        if user is None or not user.active or user.session_version != data.get("v"):
            return None
        return CurrentUser(user.id, user.email, user.is_admin, user.display_name)


# ---------------------------------------------------------------------------
# Project authorisation
# ---------------------------------------------------------------------------

_LOOKUPS: list[str] | None = None
# URL word before an id → the table that id lives in (checked first)
_HINTS = {
    "projects": "projects", "videos": "videos", "jobs": "processing_jobs", "characters": "characters",
    "sessions": "upload_sessions", "takes": "dub_audio_takes", "placements": "dub_timing_placements",
    "results": "lipsync_results", "segments": "transcript_segments", "translations": "transcript_segments",
    "references": "voice_references", "profiles": "character_voice_profiles", "presets": "voice_presets",
    "assets": "media_assets", "timelines": "timeline_projects", "clips": "timeline_clips",
}


def _lookups() -> list[str]:
    """SQL that maps an id to its project id, one statement per table that has one."""
    global _LOOKUPS
    if _LOOKUPS is None:
        out = []
        for table in Base.metadata.sorted_tables:
            cols = table.columns.keys()
            if table.name == "projects":
                out.append(("projects", "SELECT id FROM projects WHERE id = :id"))
            elif "id" in cols and "project_id" in cols:
                out.append((table.name, f"SELECT project_id FROM {table.name} WHERE id = :id"))
            elif table.name == "transcript_segments":
                out.append((table.name, "SELECT t.project_id FROM transcript_segments s JOIN transcripts t ON t.id = s.transcript_id WHERE s.id = :id"))
            elif "id" in cols and "timeline_id" in cols and "timeline_projects" in Base.metadata.tables:
                out.append((table.name, f"SELECT p.project_id FROM {table.name} c JOIN timeline_projects p ON p.id = c.timeline_id WHERE c.id = :id"))
        _LOOKUPS = out
    return _LOOKUPS


def project_of(db, hint: str | None, ident: str) -> str | None:
    ordered = sorted(_lookups(), key=lambda item: 0 if item[0] == _HINTS.get(hint or "") else 1)
    for _table, sql in ordered:
        try:
            row = db.execute(text(sql), {"id": ident}).first()
        except Exception:  # noqa: BLE001
            db.rollback()
            continue
        if row is not None:
            return row[0]
    return None


def accessible_project_ids(db, user: CurrentUser) -> set[str] | None:
    """None = everything (admin or auth off)."""
    from ..models_workers import ProjectMember

    if not enabled() or user is None or user.is_admin:
        return None
    return set(db.scalars(select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)))


def authorize_path(path: str, user: CurrentUser) -> bool:
    parts = path.split("/")
    ids = [(parts[i - 1] if i else None, part) for i, part in enumerate(parts) if UUID_RE.fullmatch(part)]
    return authorize_ids(ids, [], user)


KEY_PROJECT_RE = re.compile(r"^projects[/\\]([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})[/\\]")


def _hint_for_key(name: str | None) -> str | None:
    """profile_id -> profiles, clip_ids -> clips, project_id -> projects ..."""
    if not name:
        return None
    base = name[:-4] if name.endswith("_ids") else name[:-3] if name.endswith("_id") else name
    return base if base.endswith("s") else f"{base}s"


def collect_references(value, name: str | None = None, ids=None, keys=None, depth: int = 0):
    """Find every UUID and every projects/<id>/ storage key in a query/JSON payload."""
    ids = [] if ids is None else ids
    keys = [] if keys is None else keys
    if depth > 8 or len(ids) > 500:
        return ids, keys
    if isinstance(value, dict):
        for key, item in value.items():
            collect_references(item, str(key), ids, keys, depth + 1)
    elif isinstance(value, list):
        for item in value:
            collect_references(item, name, ids, keys, depth + 1)
    elif isinstance(value, str):
        lowered = value.strip().lower()
        if UUID_RE.fullmatch(lowered):
            ids.append((_hint_for_key(name), lowered))
        match = KEY_PROJECT_RE.match(lowered)
        if match:
            keys.append(match.group(1))
    return ids, keys


def authorize_ids(ids: list, key_projects: list, user: CurrentUser) -> bool:
    """Every referenced id/storage key must belong to one of the user's projects.

    Unknown ids fall through to the route (which answers 404); storage keys must
    name a project the user can access.
    """
    if user.is_admin or (not ids and not key_projects):
        return True
    with SessionLocal() as db:
        allowed = accessible_project_ids(db, user) or set()
        for project in key_projects:
            if project not in allowed:
                return False
        for hint, ident in ids:
            project = project_of(db, hint, ident)
            if project is not None and project not in allowed:
                return False
    return True


# ---------------------------------------------------------------------------
# Login throttling (per client address + email, in memory)
# ---------------------------------------------------------------------------

_FAILURES: dict[str, list[float]] = {}


def _throttle_key(address: str | None, email: str) -> str:
    return f"{address or '?'}|{email.strip().lower()}"


def login_blocked(address: str | None, email: str) -> int:
    """Seconds until another attempt is allowed (0 = allowed)."""
    now = time.time()
    window = auth_settings.login_window_s
    attempts = [t for t in _FAILURES.get(_throttle_key(address, email), []) if now - t < window]
    _FAILURES[_throttle_key(address, email)] = attempts
    if len(attempts) >= auth_settings.login_max_failures:
        return int(window - (now - attempts[0])) + 1
    return 0


def record_login_failure(address: str | None, email: str) -> None:
    _FAILURES.setdefault(_throttle_key(address, email), []).append(time.time())


def clear_login_failures(address: str | None, email: str) -> None:
    _FAILURES.pop(_throttle_key(address, email), None)


def cookie_secure() -> bool:
    if auth_settings.cookie_secure is not None:
        return bool(auth_settings.cookie_secure)
    return settings.environment.lower() == "production"


def bootstrap_required() -> bool:
    return settings.environment.lower() == "production" or bool(auth_settings.bootstrap_token)


def add_owner(db, project_id: str, user: CurrentUser | None) -> None:
    from ..models_workers import ProjectMember

    if user is None:
        return
    db.add(ProjectMember(project_id=project_id, user_id=user.id, role="owner"))
    db.commit()


# ---------------------------------------------------------------------------
# Response hygiene: never expose server filesystem paths
# ---------------------------------------------------------------------------


def _path_variants() -> list[str]:
    roots = {str(PROJECT_ROOT), str(settings.data_dir), str(settings.effective_model_cache_dir), os.path.expanduser("~")}
    out = set()
    for root in roots:
        if not root or len(root) < 4:
            continue
        for variant in (root, root.replace("\\", "/"), root.replace("\\", "\\\\")):
            out.add(variant)
    return sorted(out, key=len, reverse=True)


_WIN_PATH = re.compile(r"[A-Za-z]:(?:\\\\|\\|/)(?:[^\s\"'<>|]+)")


def redact(text_: str) -> str:
    for variant in _path_variants():
        text_ = text_.replace(variant, "<server>")
    return _WIN_PATH.sub("<path>", text_)


def redaction_enabled() -> bool:
    return enabled() or settings.environment.lower() != "development"
