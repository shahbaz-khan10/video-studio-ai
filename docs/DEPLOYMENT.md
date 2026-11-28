# Production deployment guide (Phase 16)

This guide describes a **single-host** deployment: one machine, API plus queue workers, SQLite in
WAL mode, files on local disk. That is the only topology that was tested. Nothing here creates
cloud accounts or paid resources; pick and pay for hosting yourself.

## 1. Hardware

| Workload | Minimum | Recommended |
|---|---|---|
| API only (no AI jobs) | 1 vCPU, 1 GB RAM | 2 vCPU, 2 GB RAM |
| Worker, CPU, all models loaded at once | 4 cores, 8 GB RAM | 8 cores, 16 GB RAM |
| Disk | 15 GB (models ≈ 7.4 GB including caches) + about 3× each source film | SSD; the 10-minute 690 MB test film used 1.7 GB (source, 4 stems and one 711 MB export) |
| GPU (optional) | not required | NVIDIA with 8 GB+ VRAM for remote LatentSync/MuseTalk workers (**untested here**) |

Measured on the development PC, a 6-core Windows 10 CPU with no GPU:

| Measurement | Result |
|---|---|
| API + inline jobs, peak RSS during the 10-minute-film run (models from an earlier run still loaded) | about 3.3 GB |
| Wav2Lip on CPU | about 29 s per 4.6 s talking segment |
| NLLB translation, Urdu + Hindi, 3 lines | about 40 s, including model load |

## 2. Configuration

1. Copy `.env.example` to `.env`.
2. Set:
   - `ENVIRONMENT=production`
   - `AUTH_MODE=password`
   - `AUTH_SECRET_KEY` (48+ random characters)
   - `AUTH_BOOTSTRAP_TOKEN`
   - `CORS_ORIGINS=https://your.domain`
   - `AUTH_COOKIE_SECURE=true`
   - `PROCESSING_MODE=queue`
   - `LOG_FORMAT=json`
   - optionally `LOG_FILE`
3. Run `python -m app.manage check-config` and fix every `ERROR`.
4. Keep `.env` out of git; it is already ignored. Rotating `AUTH_SECRET_KEY` signs everyone out.

The app refuses to start in production with `AUTH_MODE=off`, unless you explicitly set
`AUTH_ALLOW_INSECURE_PRODUCTION=true`.

## 3. Database and migrations

- `python -m app.manage init-db` creates tables and applies the additive SQLite migrations. The API
  and workers also do this on start, and are safe when they start at the same time.
- SQLite runs in WAL mode with a 30 s busy timeout, so the API and several workers can share it
  **on one host and a local disk**. Do not put the database on NFS or SMB.
- PostgreSQL: `DATABASE_URL=postgresql+psycopg://...` is accepted by SQLAlchemy, but **it has not
  been tested**, and the built-in migrations are SQLite-only.

## 4. Processes

**Without Docker:**

```bash
cd backend
uvicorn app.main:app --host 127.0.0.1 --port 4001 --proxy-headers   # behind the reverse proxy
python -m app.worker                                                 # one or more
```

Run both under a supervisor (systemd, NSSM on Windows, or Docker `restart: unless-stopped`).
Workers hold leases. If a worker dies, its job is re-queued after `PROCESSING_LEASE_SECONDS` and
retried up to `PROCESSING_MAX_ATTEMPTS` times.

**Docker:** see `DOCKER.md`.

## 5. Frontend

`npm ci && npm run build` writes `dist/`. The API serves it on the same origin, so no CORS is
needed. Source maps are off in production builds; use `VITE_SOURCEMAP=true` to enable them.

## 6. Health checks and logs

- `GET /api/v1/health` returns 200 with `{"status":"ok","database":"ok"}`. It needs no sign-in.
- `GET /api/v1/processing/status` (admin) shows queue counts and live workers.
- Every response carries `X-Request-ID`. With `LOG_FORMAT=json`, the request log line holds the
  same id, so a user's error report can be matched to the server log.
- In production, 500 responses never include internal details (paths or SQL); the log has them.

## 7. Backup and recovery

```bash
python -m app.manage backup --out /backups                    # consistent online snapshot of the DB
python -m app.manage backup --out /backups --with-artifacts   # + data/projects (media, exports)
python -m app.manage restore --from /backups/cinema-YYYYMMDD-HHMMSS --yes   # stop API and workers first
```

- Each backup checks itself with `PRAGMA integrity_check` and writes a `manifest.json`.
- `restore` keeps the old database as `*.before-restore`.
- Schedule backups with cron or Task Scheduler, keep at least one copy on another machine, and
  test a restore now and then.

## 8. Storage security

- Uploaded and generated files live under `DATA_DIR/artifacts/projects/<project id>/`.
- Every download route checks that the user may open the project and that the stored key belongs
  to that project. Keys containing `..`, absolute paths or NUL bytes are refused.
- Use full-disk encryption on the host if films are confidential. The app does not encrypt files
  at rest.
- Temporary files older than `PROCESSING_TEMP_RETENTION_HOURS` are cleaned by the worker, or with
  `manage.py cleanup-temp --apply`.

## 9. Optional remote GPU workers

`PROCESSING_REMOTE_URL`, `LIPSYNC_REMOTE_URL` and the other `*_REMOTE_URL` settings point at
machines **you** run. Use HTTPS and a long random token for each. `workers/lipsync_worker.py`
refuses requests without its `LIPSYNC_WORKER_TOKEN`. GPU hosting is billed by the hour by every
provider, and bandwidth for film uploads is often billed too. Estimate the cost before turning one on.

## 10. Go-live checklist

- [ ] `check-config` shows no ERROR
- [ ] HTTPS in front, `AUTH_COOKIE_SECURE=true`, explicit `CORS_ORIGINS`
- [ ] administrator created; registration off (`AUTH_ALLOW_REGISTRATION=false`) unless wanted
- [ ] backups scheduled and one restore tested
- [ ] licences reviewed (`docs/LICENSES_AND_MODELS.md`). The default models are **non-commercial**.
- [ ] disk monitoring (projects grow quickly) and log rotation
