from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


_JOB_COLUMNS = {
    "options_json": "JSON NOT NULL DEFAULT '{}'",
    # Phase 15 queue lease
    "worker_id": "VARCHAR(64)",
    "lease_expires_at": "DATETIME",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
}

_LIPSYNC_COLUMNS = {
    "needs_regeneration": "BOOLEAN NOT NULL DEFAULT 0",
    "regeneration_reason": "VARCHAR(255)",
    "dubbed_fingerprint": "VARCHAR(64)",
}

_VIDEO_COLUMNS = {
    "fps": "REAL",
    "frame_count": "INTEGER",
    "pixel_format": "VARCHAR(64)",
    "color_space": "VARCHAR(64)",
    "color_range": "VARCHAR(64)",
    "bit_rate": "INTEGER",
    "stream_layout": "JSON NOT NULL DEFAULT '[]'",
    "probe_source": "VARCHAR(32)",
}


def _ensure_sqlite_parent() -> None:
    url = settings.database_url
    if not url.startswith("sqlite:///"):
        return
    raw = url.removeprefix("sqlite:///")
    if raw == ":memory:":
        return
    Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_parent()
settings.data_dir.mkdir(parents=True, exist_ok=True)

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)

if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _migrate_sqlite() -> None:
    with engine.begin() as connection:
        existing = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(videos)")}
        for name, definition in _VIDEO_COLUMNS.items():
            if name not in existing:
                connection.exec_driver_sql(f"ALTER TABLE videos ADD COLUMN {name} {definition}")
        job_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(processing_jobs)")}
        for name, definition in _JOB_COLUMNS.items():
            if name not in job_columns:
                connection.exec_driver_sql(f"ALTER TABLE processing_jobs ADD COLUMN {name} {definition}")
        # Phase 14 - lip-sync invalidation columns on Phase 13 table
        try:
            lipsync_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(lipsync_results)")}
        except Exception:
            lipsync_columns = set()
        for name, definition in _LIPSYNC_COLUMNS.items():
            if lipsync_columns and name not in lipsync_columns:
                connection.exec_driver_sql(f"ALTER TABLE lipsync_results ADD COLUMN {name} {definition}")


def init_db() -> None:
    from . import (  # noqa: F401
        models,
        models_lipsync,
        models_workers,
        models_mix,
        models_timeline,
        models_timing,
        models_tts,
    )

    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "sqlite":
        _migrate_sqlite()
