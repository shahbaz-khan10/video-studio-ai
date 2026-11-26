import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import PROJECT_ROOT, settings
from .database import init_db
from .routers import characters, jobs, media, mixer, projects, system, transcripts, uploads, videos
from .services.media import AppError


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.4.0-phase7",
    description="Central API for browser-based AI dubbing project management.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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


def error_response(status_code: int, code: str, message: str, details: object = None) -> JSONResponse:
    error: dict = {"code": code, "message": message, "request_id": str(uuid.uuid4())[:8]}
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"error": error})


@app.exception_handler(AppError)
async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message)


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
async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    return error_response(500, "INTERNAL_ERROR", "Unexpected server error.", str(exc)[:500])


_dist = PROJECT_ROOT / "dist"
if (_dist / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        index = _dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return error_response(404, "NOT_FOUND", "Frontend build not found.")
