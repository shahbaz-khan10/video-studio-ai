import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import PROJECT_ROOT, settings
from .database import init_db
from .routers import (
    characters,
    jobs,
    media,
    mixer,
    projects,
    system,
    transcripts,
    translations,
    uploads,
    videos,
    voice_profiles,
)
from .routers import dub_audio  # Phase 10
from .routers import dub_timing  # Phase 11
from .routers import lipsync  # Phase 13
from .routers import mix_studio  # Phase 12
from .routers import timeline as timeline_router  # Phase 14
from .routers import external_audio  # Phase 14
from .routers import auth as auth_router  # Phase 15
from .routers import processing  # Phase 15
from .services import auth, job_queue
from . import models_timeline  # noqa: F401 - registers Phase 14 tables before init_db
from .services.media import AppError
from .services.storage import InvalidStorageKey
from .logging_setup import configure_logging

configure_logging()
log = logging.getLogger("cinema.api")
MAX_INSPECT_BODY = 2 * 1024 * 1024  # JSON bodies inspected for foreign ids


@asynccontextmanager
async def lifespan(_app: FastAPI):
    auth.check_configuration()
    init_db()
    from .services import remote_mode

    remote_mode.apply()
    if settings.environment != "test":
        job_queue.start_monitor()  # recovery, worker heartbeat, idle-model unload, temp cleanup
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.9.0-phase16",
    description="Central API for browser-based AI dubbing project management.",
    lifespan=lifespan,
)


app.include_router(system.router, prefix="/api/v1")
app.include_router(projects.router, prefix="/api/v1")
app.include_router(mixer.router, prefix="/api/v1")
app.include_router(uploads.router, prefix="/api/v1")
app.include_router(videos.router, prefix="/api/v1")
app.include_router(media.router, prefix="/api/v1")
app.include_router(jobs.router, prefix="/api/v1")
app.include_router(transcripts.router, prefix="/api/v1")
app.include_router(characters.router, prefix="/api/v1")
app.include_router(translations.router, prefix="/api/v1")
app.include_router(voice_profiles.router, prefix="/api/v1")
app.include_router(dub_audio.router, prefix="/api/v1")  # Phase 10 — voice generation
app.include_router(dub_timing.router, prefix="/api/v1")  # Phase 11 — dialogue timing
app.include_router(lipsync.router, prefix="/api/v1")  # Phase 13 — lip sync
app.include_router(mix_studio.router, prefix="/api/v1")  # Phase 12
app.include_router(timeline_router.router, prefix="/api/v1")  # Phase 14
app.include_router(external_audio.router, prefix="/api/v1")  # Phase 14 — mixing studio
app.include_router(auth_router.router, prefix="/api/v1")  # Phase 15 — sign-in
app.include_router(processing.router, prefix="/api/v1")  # Phase 15 — workers & resources


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """Authentication, project authorisation and path redaction (Phase 15)."""
    path = request.url.path
    request.state.user = None
    if auth.enabled() and request.method != "OPTIONS" and path.startswith("/api/") and path not in auth.PUBLIC_PATHS:
        from starlette.concurrency import run_in_threadpool

        user = await run_in_threadpool(auth.user_from_request, request)
        if user is None:
            return error_response(401, "UNAUTHENTICATED", "Sign in to continue.")
        request.state.user = user
        if path.startswith(auth.ADMIN_PREFIXES) and not user.is_admin:
            return error_response(403, "FORBIDDEN", "Administrator access required.")
        if not await run_in_threadpool(auth.authorize_path, path, user):
            return error_response(404, "NOT_FOUND", "Not found.")  # do not reveal other users' ids
        # Ids and storage keys can also arrive in the query string or a JSON body.
        ids, keys = auth.collect_references(dict(request.query_params))
        content_type = request.headers.get("content-type", "")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and content_type.startswith("application/json"):
            raw = await request.body()
            if len(raw) > MAX_INSPECT_BODY:
                return error_response(413, "BODY_TOO_LARGE", "JSON body is too large.")
            try:
                payload = json.loads(raw or b"null")
            except ValueError:
                payload = None
            auth.collect_references(payload, None, ids, keys)
        if (ids or keys) and not await run_in_threadpool(auth.authorize_ids, ids, keys, user):
            return error_response(404, "NOT_FOUND", "Not found.")
    response = await call_next(request)
    if auth.redaction_enabled() and response.headers.get("content-type", "").startswith("application/json"):
        body = b"".join([chunk async for chunk in response.body_iterator])
        cleaned = auth.redact(body.decode("utf-8", "replace")).encode("utf-8")
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(content=cleaned, status_code=response.status_code, headers=headers, media_type="application/json")
    return response


@app.middleware("http")
async def request_log_middleware(request: Request, call_next):
    """Access log with a request id (also returned as X-Request-ID)."""
    request_id = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001 — logged, then the generic handler answers
        log.exception("unhandled error id=%s %s %s", request_id, request.method, request.url.path)
        raise
    elapsed = (time.perf_counter() - started) * 1000
    if request.url.path.startswith("/api/"):
        level = logging.WARNING if response.status_code >= 500 else logging.INFO
        log.log(level, "%s %s -> %s %.0fms id=%s", request.method, request.url.path, response.status_code, elapsed, request_id)
    response.headers["X-Request-ID"] = request_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


# CORS last = outermost, so even 401/403/404 from the security layer carry CORS headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def error_response(status_code: int, code: str, message: str, details: object = None) -> JSONResponse:
    error: dict = {"code": code, "message": message, "request_id": str(uuid.uuid4())[:8]}
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"error": error})


@app.exception_handler(AppError)
async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message)


@app.exception_handler(InvalidStorageKey)
async def storage_key_handler(_request: Request, _exc: InvalidStorageKey) -> JSONResponse:
    return error_response(400, "INVALID_STORAGE_KEY", "Invalid file reference.")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return error_response(400, "VALIDATION_ERROR", "The request is invalid.", exc.errors()[:8])


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict):
        code = exc.detail.get("code", "HTTP_ERROR")
        message = exc.detail.get("message", "Request failed.")
    else:
        code = "HTTP_ERROR"
        message = str(exc.detail)
    return error_response(exc.status_code, code, message)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled %s on %s %s: %s", type(exc).__name__, request.method, request.url.path, exc, exc_info=exc)
    detail = str(exc)[:500]
    if auth.redaction_enabled():
        detail = None  # never leak internals (paths, SQL) outside development
    return error_response(500, "INTERNAL_ERROR", "Unexpected server error.", detail)


_dist = PROJECT_ROOT / "dist"
if (_dist / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            # An unknown API route must answer JSON 404, not the HTML app shell with 200.
            return error_response(404, "NOT_FOUND", "API route not found.")
        index = _dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return error_response(404, "NOT_FOUND", "Frontend build not found.")
