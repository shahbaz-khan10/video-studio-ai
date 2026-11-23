# CINEMA AI DUB

Browser-first dubbing studio foundation with Phase 3 video processing. This build includes a responsive dashboard, project management, chunked/resumable upload, magic-byte validation, SHA-256 checksum, ffprobe/ffmpeg metadata probing, frame/fps indexing, versioned thumbnails and frame-map artifacts, original-file storage, range-based preview, and durable job progress. AI dubbing stages remain planned and are not enabled.

## Requirements

- Node.js 20.19+ or 22.12+
- Python 3.11+
- npm
- Optional: `ffprobe` on PATH. Without it, probing uses the bundled `imageio-ffmpeg` binary; browser metadata is the last fallback.

## Install

```powershell
npm install
python -m pip install -r backend\requirements.txt
```

Copy `.env.example` to `.env` only if you need to override defaults.

## Run in development

Terminal 1:

```powershell
cd backend
python -m uvicorn app.main:app --reload --port 8000
```

Terminal 2:

```powershell
npm run dev
```

Open `http://localhost:5173`. Vite proxies `/api` to `http://localhost:8000`.

## Verify

```powershell
npm run typecheck
npm run build
python -m pytest backend/tests
```

If PowerShell blocks `npm.ps1`, run `node node_modules/typescript/bin/tsc --noEmit` and `node node_modules/vite/bin/vite.js build` instead.

## API

- `GET /api/v1/health`
- `GET /api/v1/info`
- `GET, POST /api/v1/projects`
- `GET, PATCH, DELETE /api/v1/projects/{project_id}`
- `GET /api/v1/projects/{project_id}/jobs`
- `POST /api/v1/uploads/projects/{project_id}/videos`
- `POST /api/v1/uploads/projects/{project_id}/sessions`
- `GET /api/v1/uploads/sessions/{session_id}`
- `PUT /api/v1/uploads/sessions/{session_id}/chunks/{index}`
- `POST /api/v1/uploads/sessions/{session_id}/complete`
- `DELETE /api/v1/uploads/sessions/{session_id}`
- `GET, DELETE /api/v1/videos/{video_id}`
- `GET /api/v1/videos/{video_id}/stream` with HTTP range support
- `GET /api/v1/videos/{video_id}/assets?kind=thumbnail`
- `GET /api/v1/media/{asset_id}`
- `POST /api/v1/videos/{video_id}/reprobe`
- `GET /api/v1/jobs/{job_id}`

Uploaded originals are stored unchanged under `data/artifacts/projects/{project_id}/source/`. Derived probes, thumbnails, and frame maps use versioned keys under `data/artifacts/projects/{project_id}/probes/`. SQLite data is stored under `data/cinema.db`. The export policy remains stream-copy until a visual edit is introduced in a later phase.
