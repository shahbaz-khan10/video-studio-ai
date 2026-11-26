from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


_JOB_COLUMNS = {
    "options_json": "JSON NOT NULL DEFAULT '{}'",
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


def init_db() -> None:
    from . import models

    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "sqlite":
        _migrate_sqlite()
