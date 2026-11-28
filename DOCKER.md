# Docker runbook

The image builds the Vite frontend, copies `dist/` into the FastAPI runtime, and serves both on
**one port**. Since Phase 16, `docker-compose.yml` runs two services from the same image:

| Service | Command | Role |
|---|---|---|
| `api` | `uvicorn app.main:app` | Web app and REST API. Queues jobs (`PROCESSING_MODE=queue`). |
| `worker` | `python -m app.worker` | Runs the queued jobs and loads the heavy models (Whisper, NLLB, MMS, Wav2Lip). |

Both run as the unprivileged user `cinema` (UID 10001), in production mode with sign-in on.

## First start

```powershell
copy .env.example .env      # Linux/macOS: cp .env.example .env
# Edit .env and set at least:
#   AUTH_SECRET_KEY=<48+ random characters>   python -c "import secrets; print(secrets.token_urlsafe(48))"
#   AUTH_BOOTSTRAP_TOKEN=<another random value, used once to create the admin>
docker compose up -d --build
```

Open <http://localhost:4001>. The first screen creates the administrator and asks for the setup
token (`AUTH_BOOTSTRAP_TOKEN`). Or, from a shell:

```powershell
docker compose exec api python -m app.manage create-admin --email you@example.org
```

`docker compose up` refuses to start while `AUTH_SECRET_KEY` is empty.

## Useful commands

```powershell
docker compose ps
docker compose logs -f api worker
docker compose exec api python -m app.manage check-config     # unsafe settings report
docker compose exec api python -m app.manage backup           # -> data/backups/cinema-<UTC time>/
docker compose exec api python -m app.manage backup --with-artifacts   # also archives data/projects
docker compose exec api python -m app.manage cleanup-temp --apply
docker compose up -d --scale worker=2    # more workers (each needs its own RAM for models)
docker compose down
```

## Data, models and permissions

- `./data` on the host is mounted at `/app/data`. It holds:
  - projects and uploads
  - the SQLite database (WAL mode)
  - backups
  - model caches
- **Linux hosts:** the container user must be able to write to `./data`. Run
  `sudo chown -R 10001:10001 data`, or rebuild with `--build-arg APP_UID=$(id -u)`.
  Docker Desktop on Windows/macOS handles this for you.
- PyTorch is the CPU-only build, so CUDA packages are not pulled in.
- First runs need internet access:
  - Transcription, translation and speech download their open-source models into `./data/models`.
  - Lip sync needs the Wav2Lip weights in `data/models/lipsync`; see `docs/LICENSES_AND_MODELS.md`.
- `LIPSYNC_DEVICE=cpu` is the default. A GPU container needs a CUDA PyTorch build and the NVIDIA
  container toolkit. **That has not been built or tested.**

## HTTPS

The container speaks plain HTTP. For anything beyond localhost:

1. Put a TLS reverse proxy in front of it (Caddy, nginx, Traefik).
2. Set `AUTH_COOKIE_SECURE=true` and `CORS_ORIGINS=https://your.domain`.
3. Set `FORWARDED_ALLOW_IPS` to the proxy's address, so client IPs (used for login throttling)
   come from `X-Forwarded-For`. Never use `*` when the port is reachable directly.

See `docs/DEPLOYMENT.md` for the full production checklist.

## What was verified (Phase 16, Docker 29.9.3 on Windows)

- `docker build` succeeds. The image is 893 MB compressed and 3.74 GB on disk.
- `api` became healthy, and `/api/v1/health` reports production and a working database.
- The container runs as uid 10001.
- A first account without the bootstrap token is refused (403).
- Queued jobs ran in the `worker` container:
  - upload and import
  - separation
  - faster-whisper transcription
  - an MP4 timeline export with labelled tracks, which then downloaded
- `manage.py check-config` and `manage.py backup` (integrity ok) ran inside the container.
