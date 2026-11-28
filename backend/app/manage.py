"""Phase 16 — operator commands.

Run from the backend directory (or `docker compose exec api python -m app.manage ...`):

    python -m app.manage init-db                      # create tables / apply SQLite migrations
    python -m app.manage check-config                 # report unsafe or missing production settings
    python -m app.manage create-admin --email a@b.org # create (or promote) an administrator
    python -m app.manage backup --out ./backups       # online SQLite backup (+ optional artifacts)
    python -m app.manage restore --from ./backups/cinema-20261128-101500
    python -m app.manage cleanup-temp [--apply]       # remove stale temp files (dry run by default)

The password for create-admin is read from --password-env (an environment variable name)
or prompted interactively, so it never appears in shell history.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from .config import settings


def _sqlite_path() -> Path | None:
    url = settings.database_url
    if not url.startswith("sqlite:///"):
        return None
    raw = url.removeprefix("sqlite:///")
    return None if raw in ("", ":memory:") else Path(raw)


def cmd_init_db(_args) -> int:
    from .database import init_db

    init_db()
    print(f"Database ready: {settings.database_url.split('@')[-1]}")
    return 0


def check_config() -> list[tuple[str, str]]:
    """Return (level, message) pairs; level is ERROR or WARN."""
    from .services import auth
    from .services.job_queue import processing_settings

    problems: list[tuple[str, str]] = []
    production = settings.environment.lower() == "production"
    a = auth.auth_settings
    if production and a.mode != "password" and not a.allow_insecure_production:
        problems.append(("ERROR", "ENVIRONMENT=production requires AUTH_MODE=password."))
    if a.mode == "password":
        if len(a.secret_key or "") < 32:
            problems.append(("ERROR", "AUTH_SECRET_KEY must be set and at least 32 characters."))
        if production and not auth.cookie_secure():
            problems.append(("WARN", "AUTH_COOKIE_SECURE is false: session cookies will travel over plain HTTP."))
        if production and a.allow_registration:
            problems.append(("WARN", "AUTH_ALLOW_REGISTRATION=true lets anyone who reaches the site create an account."))
    if production and any(origin.strip() == "*" for origin in settings.cors_origins.split(",")):
        problems.append(("ERROR", "CORS_ORIGINS must list explicit origins in production, not '*'."))
    if production and settings.database_url.startswith("sqlite") and processing_settings.mode == "queue":
        problems.append(("WARN", "SQLite with queue workers works (WAL mode) on ONE host only; use a local disk, not NFS."))
    for name in ("REMOTE_SEPARATION", "TRANSCRIPTION_REMOTE", "DIARIZATION_REMOTE", "TRANSLATION_REMOTE", "VOICE_REMOTE"):
        url = getattr(settings, f"{name.lower()}_url", "") or ""
        if url.startswith("http://") and "localhost" not in url and "127.0.0.1" not in url:
            problems.append(("WARN", f"{name}_URL uses plain http:// to a non-local host; tokens and media are sent unencrypted."))
    if shutil.disk_usage(settings.data_dir).free < 10 * 1024**3:
        problems.append(("WARN", f"Less than 10 GB free in DATA_DIR ({settings.data_dir})."))
    return problems


def cmd_check_config(_args) -> int:
    problems = check_config()
    for level, message in problems:
        print(f"{level}: {message}")
    if not problems:
        print("OK: no configuration problems found.")
    return 1 if any(level == "ERROR" for level, _ in problems) else 0


def cmd_create_admin(args) -> int:
    from sqlalchemy import select

    from .database import SessionLocal, init_db
    from .models_workers import UserAccount
    from .services import auth

    init_db()
    password = os.environ.get(args.password_env, "") if args.password_env else ""
    if not password:
        if not sys.stdin.isatty():
            print("No password: pass --password-env NAME or run interactively.", file=sys.stderr)
            return 2
        password = getpass.getpass("Password (min 10 characters): ")
        if password != getpass.getpass("Repeat password: "):
            print("Passwords do not match.", file=sys.stderr)
            return 2
    if len(password) < 10:
        print("Password must be at least 10 characters.", file=sys.stderr)
        return 2
    email = args.email.strip().lower()
    with SessionLocal() as db:
        user = db.scalar(select(UserAccount).where(UserAccount.email == email))
        if user:
            user.is_admin = True
            user.active = True
            user.password_hash = auth.hash_password(password)
            user.session_version = (user.session_version or 1) + 1  # signs out old sessions
            action = "updated"
        else:
            user = UserAccount(email=email, display_name=args.name, password_hash=auth.hash_password(password), is_admin=True)
            db.add(user)
            action = "created"
        db.commit()
    print(f"Administrator {action}: {email}")
    return 0


def cmd_backup(args) -> int:
    db_path = _sqlite_path()
    if db_path is None:
        print("Only SQLite databases are backed up by this command; use your database's own tools (e.g. pg_dump).", file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = Path(args.out).resolve() / f"cinema-{stamp}"
    target.mkdir(parents=True, exist_ok=False)
    # sqlite3's online backup API copies a consistent snapshot while the app keeps running.
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dest = sqlite3.connect(target / db_path.name)
    with dest:
        source.backup(dest)
    check = dest.execute("PRAGMA integrity_check").fetchone()[0]
    source.close()
    dest.close()
    manifest = {"created_utc": stamp, "database": db_path.name, "integrity": check, "artifacts": None}
    if args.with_artifacts:
        projects = settings.data_dir / "projects"
        if projects.exists():
            archive = target / "projects.tar"
            with tarfile.open(archive, "w") as tar:
                tar.add(projects, arcname="projects")
            manifest["artifacts"] = archive.name
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Backup written to {target} (integrity: {check})")
    return 0 if check == "ok" else 1


def cmd_restore(args) -> int:
    source = Path(args.source).resolve()
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    db_path = _sqlite_path()
    if db_path is None:
        print("Restore supports SQLite only.", file=sys.stderr)
        return 2
    if not args.yes:
        print(f"This REPLACES {db_path}" + (" and data/projects" if manifest.get("artifacts") else "") +
              ". Stop the API and workers first, then re-run with --yes.")
        return 2
    if db_path.exists():
        shutil.copy2(db_path, db_path.with_suffix(db_path.suffix + ".before-restore"))
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    shutil.copy2(source / manifest["database"], db_path)
    if manifest.get("artifacts"):
        projects = settings.data_dir / "projects"
        if projects.exists():
            projects.rename(projects.with_name(f"projects.before-restore-{datetime.now():%Y%m%d%H%M%S}"))
        with tarfile.open(source / manifest["artifacts"]) as tar:
            tar.extractall(settings.data_dir, filter="data")
    print(f"Restored from {source}")
    return 0


def cmd_cleanup_temp(args) -> int:
    from .services.resources import cleanup_temp

    print(json.dumps(cleanup_temp(dry_run=not args.apply), indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.manage", description="CINEMA AI DUB operator commands")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="create tables and apply migrations").set_defaults(func=cmd_init_db)
    sub.add_parser("check-config", help="report unsafe production settings").set_defaults(func=cmd_check_config)
    admin = sub.add_parser("create-admin", help="create or promote an administrator")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", default=None)
    admin.add_argument("--password-env", default=None, help="name of an environment variable holding the password")
    admin.set_defaults(func=cmd_create_admin)
    backup = sub.add_parser("backup", help="online database backup")
    backup.add_argument("--out", default=str(settings.data_dir / "backups"))
    backup.add_argument("--with-artifacts", action="store_true", help="also archive data/projects (can be large)")
    backup.set_defaults(func=cmd_backup)
    restore = sub.add_parser("restore", help="restore a backup folder")
    restore.add_argument("--from", dest="source", required=True)
    restore.add_argument("--yes", action="store_true")
    restore.set_defaults(func=cmd_restore)
    temp = sub.add_parser("cleanup-temp", help="remove stale temporary files")
    temp.add_argument("--apply", action="store_true")
    temp.set_defaults(func=cmd_cleanup_temp)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
