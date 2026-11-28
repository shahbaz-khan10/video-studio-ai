# CINEMA AI DUB — Phase 16 Final Report (Final Testing & Production)

**Date:** 28 November 2026 · **Branch:** `phase-8-translation` (uncommitted) · **Version:** `0.9.0-phase16`

Every result below comes from a command that was actually run on the development PC:
Windows 10 Pro, a 6-core CPU, **no GPU**, Python 3.12, Node 22 and Docker Desktop 29.9.3.
Anything that was not run is listed under **B (skipped/blocked)** and **I**.

---

## A. Features implemented

**Phases 1–15 are unchanged.** All their tests still pass. Phase 16 added the following.

**1. Final multilingual export** (`services/final_export.py`, timeline export job)
- **Modes**
  - Multi-track: one file with English, Urdu and Hindi plus music/SFX/ambience/external tracks.
  - Separate-language: one file per language.
- **Containers:** MKV, MP4 or MOV. Anything else is rejected with 422.
- **Track labels**
  - ISO 639-2 language tags (`eng`, `urd`, `hin`).
  - Track names: MKV `title`; MP4/MOV `handler_name`, the name VLC/QuickTime show.
  - The main dialogue track is marked default.
- **Lip-synced picture:** for Urdu and Hindi, the Phase 13 Wav2Lip clips are composited back into
  the full film, frame-exact.
  - Only accepted, current clips are used.
  - Only that file is re-encoded (H.264 CRF 18, +faststart).
  - English, and any file without lip sync, copies the original video stream (no re-encode).
- **Checks on every file**
  - The exact film length is kept (`-t <duration>`, not `-shortest`).
  - The finished file is probed and test-decoded. A broken file fails the job (`EXPORT_VALIDATION_FAILED`).
- **Downloads:** `GET /jobs/{id}/timeline-export/files/{n}`, which checks ownership.
- **UI:** Timeline panel has container, "Use lip-synced picture", picture language for multi-track,
  Render export, live progress, and a file list with download buttons.

**2. Security**
- **Sign-in UI:** sign-in/registration screen, setup-token field, session-expiry handling, Sign out.
- **Bootstrap:** the first admin needs `AUTH_BOOTSTRAP_TOKEN` in production, or `manage.py create-admin`.
- **Login throttling:** 8 failures per 15 min per IP+email.
- **Cookies:** secure by default in production.
- **Ownership checks:** IDs and storage keys are checked in the path, the query string **and JSON
  bodies**. Client-supplied storage keys must belong to the project.
- **Path safety:** traversal, absolute and NUL keys are refused (400).
- **Endpoints**
  - The old `?key=` file endpoint was removed.
  - The job list is filtered by project access.
  - `/processing/remote` is admin-only.
  - Unknown `/api/...` routes return JSON 404 instead of the HTML app.
- **Headers:** `X-Request-ID`, `nosniff`, `Referrer-Policy`. CORS headers are kept on 401s.
- **Models and workers:** `torch.load(weights_only=True)` for lip-sync weights. The GPU lip-sync
  worker requires its bearer token (constant-time check).
- **Frontend:** no production source maps.

**3. Production tooling**
- **SQLite:** WAL mode, `busy_timeout` 30 s, `synchronous=NORMAL`.
- **Concurrent start fixed:** API and workers starting together on a fresh database no longer crash.
- **`python -m app.manage`:** `init-db`, `check-config`, `create-admin`, `backup` (online snapshot +
  integrity check, optional media archive), `restore`, `cleanup-temp`.
- **Logging:** text or JSON, optional rotating file, request IDs.
- **Docker**
  - Non-root image (UID 10001).
  - Compose runs `api` plus a queue `worker`.
  - `AUTH_SECRET_KEY` is required to start.
  - `.env` support.
  - `--proxy-headers` with `FORWARDED_ALLOW_IPS`.
  - `.dockerignore` hardened.
- **`.env.example`:** now covers every setting, including the Phase 9–16 ones.
- **Docs:** `docs/DEPLOYMENT.md`, `docs/LICENSES_AND_MODELS.md`, `DOCKER.md`.
- **Dependencies added:** `scipy` (imported by lip sync but undeclared), `psutil`, `websockets`.

**4. Bugs found and fixed in Phase 16**

| # | Bug | Effect | Fix |
|---|---|---|---|
| 1 | `populate_from_phases` read `asset.duration_ms`, which does not exist | Timeline GET returned **500** whenever Phase 4 stems existed | Read the duration from the asset metadata |
| 2 | Export titles kept a trailing `\r` | Garbled track names | Strip it |
| 3 | MP4/MOV exports lost track names | Tracks unnamed in MP4/MOV | Also write `handler_name`, and probe per track |
| 4 | API and worker initialising a fresh database together | One crashed with "table already exists" (reproduced 1 in 12 runs) | Retry is now race-safe |
| 5 | Unknown `/api/*` paths returned the HTML app with **200** | Misleading for API clients | Return JSON 404 |
| 6 | `scipy` was imported by lip sync but missing from `requirements.txt` | Fresh installs failed at lip sync | Added to requirements |
| 7 | Stale `timeline_editor False` assertion in the Phase 5 test | Test out of date | Fixed (committed in `c61fc0e`) |
| 8 | Old file endpoint took any `key` | Path probing | Replaced with a job-scoped endpoint |

## B. Tests

| Suite | Passed | Failed | Skipped | Blocked |
|---|---|---|---|---|
| Backend pytest (unit + API, all phases) | **231** | 0 | 5 (opt-in real models) | 0 |
| Opt-in real-model tests, run separately (Whisper ASR, ECAPA diarization, NLLB, MMS-TTS, Wav2Lip+SyncNet) | **5** | 0 | 0 | 0 |
| Phase 16 security tests (`test_phase16_security.py`) | **16** | 0 | 0 | 0 |
| Frontend typecheck + production build | ✅ | 0 | — | — |
| Real-browser UI smoke (headless Chrome, `tests/e2e/ui_smoke.py`) | **25/25** | 0 | | |
| Full pipeline E2E, production mode, auth on (`tests/e2e/pipeline_e2e.py`) | **16/16 stages** | 0 | | |
| Export verification (`verify_exports.py`) | **all checks met** (see below) | 0 | | |
| Large upload / long video / reliability (`perf_reliability.py`) | **9/9** | 0 | | |
| Docker build + compose stack, queue worker (`queue_smoke.py`) | **7/7** | 0 | | |
| `manage.py` commands (init-db, create-admin ×2, backup, check-config ×2, restore, cleanup-temp) | **all ran OK** | 0 | | |

**Full pipeline E2E (final run, 28 Nov).** Settings: production mode, sign-in on, the first admin
created with the bootstrap token, and `PROCESSING_MODE=queue`, so every job ran in a separate
`python -m app.worker` process. The test film was an 18.5 s 720×720 talking-head clip with English
speech. Total stage time was 283 s on CPU.

| Stage | Result |
|---|---|
| 1. Admin | A first account without the token was refused (403); with the token, the admin was created |
| 2. Project | Created |
| 3. Upload | Chunked upload with a resume check; metadata 18.48 s, 25 fps, 720×720 |
| 4. Separation | DSP: dialogue, music, sfx and ambience stems |
| 5. Dialogue/music control | Dialogue muted, music at 0.5 |
| 6. Transcription | faster-whisper: 3 segments with correct text |
| 7. Characters | 1 character |
| 8. Translation | NLLB, Urdu + Hindi: 44 s |
| 9. Voice profiles | Saved (Phase 9, MMS) for Urdu and Hindi |
| 10. TTS | 3/3 lines per language |
| 11. Timing | 3/3 fitted per language; max tempo 1.109× |
| 12. Mixes | Urdu and Hindi at −16.0 LUFS integrated; true peak −2.3 / −3.3 dBFS |
| 13. Lip sync (Wav2Lip, CPU) | First pass flagged `needs_face` (correct for this clip). After face selection, 3/3 segments generated per language, 28–40 s each. SyncNet confidence went from 0.01–0.03 (before) to about 0.6 (after), best offset +2 frames. Results were accepted in review. |
| 14. Timeline | Built from all phases |
| 15. External audio | WAV imported and placed at 500 ms |
| 16. Export | Multi-track MKV, plus separate EN/UR/HI MP4 files |

**Export verification** (decoded from the exported files):

| File | Result |
|---|---|
| Multi-track MKV | Video copied (not re-encoded), 18.5 s. Tracks: `eng` Original English, `urd` Urdu Dub, `hin` Hindi Dub, then Music/SFX/Ambience/External. All titles correct; all tracks present. |
| Urdu track placement | −19.8 dB inside its placement windows, −116.6 dB outside. Speech starts 41–49 ms after each placement (the TTS lead-in). |
| Hindi track placement | −19.7 dB inside, −109.2 dB outside. Onsets 50, 57 and **191 ms**: one line has a longer lead-in. |
| External sting | Starts at 498 ms (placed at 500 ms) |
| `_en.mp4` | Video copied; 0.0 pixel difference from the source |
| `_ur.mp4` / `_hi.mp4` | Lip-synced picture, re-encoded, with 3 clips each. Same frame count as the source (462). Mouth-region difference vs the original: 13.6–13.8 inside lip-sync windows vs 4.7–4.9 outside. Upper face: 2.56, so the rest of the face is preserved. |

**Reliability run details** (10-minute 1280×720 film, 690 MB, 83 chunks of 8 MB, against a live server):

| Check | Result |
|---|---|
| Network drop in the middle of a chunk | The server stored nothing for that chunk |
| Short chunk | Rejected (422) |
| Chunks sent out of order, then a new client session | The server remembered all 41 chunks |
| Duplicate chunk | Accepted harmlessly; the upload completed (local disk, 176 MB/s) |
| Import + metadata | 2.0 s, 600.0 s detected |
| Separation cancelled at 56%, then retried | Completed in 11.2 s (DSP) |
| 10-minute multi-track export | 23 s, 600.02 s long, video copied |
| Peak server RSS | 3.26 GB, with NLLB/Whisper/Wav2Lip still loaded from an earlier run |

## C. Known bugs and limitations

1. **Licences.** The default translation (NLLB-200), voices (MMS-TTS) and lip sync (Wav2Lip) are
   **non-commercial**. The app is not cleared for commercial use as installed. See G.
2. **Lip-sync quality.** Wav2Lip works on a 96×96 mouth crop, so the mouth is visibly softer than
   the rest of a large face.
   - Only near-frontal faces were tested.
   - Still or ambiguous faces need the manual face-selection step; this is by design.
   - The SyncNet score rose from 0.01–0.04 (before) to 0.45–0.63 (after) on the test clip. That is
     an automatic score; **nobody watched the result for quality.**
3. **Multi-track picture.** A multi-track file has one picture, so mouths can match only one
   language. The user chooses the original, Urdu or Hindi picture.
4. **Voices.** MMS-TTS has one voice per language, and female characters are pitch-shifted. There
   is no emotional delivery locally.
5. **Scale.** SQLite on a single host only. PostgreSQL and multi-host workers are untested.
6. **Login throttling is in-memory** per API process. It resets on restart and is not shared
   between several API replicas.
7. **Container FFmpeg.** Debian's `ffmpeg` is used. Its build, and the Windows build, are GPL; see G.
8. **HTTPS.** The container serves plain HTTP. TLS must come from a reverse proxy.
9. **Long films on CPU.** AI stages (Whisper, NLLB, TTS, Wav2Lip) were timed only on short clips.
   - Wav2Lip at about 29 s per 4.6 s segment means roughly **6× slower than real time**: about 1
     hour of CPU per 10 minutes of on-screen dialogue, per language. Extrapolated, not measured.

## D. Hardware requirements

| | Minimum | Recommended |
|---|---|---|
| CPU | 4 cores (x86-64, AVX2) | 8+ cores |
| RAM | 8 GB (API + one worker; models load lazily and unload after 15 min idle) | 16 GB |
| Disk | 15 GB for models and caches, plus about 2.5× each source film | SSD |
| GPU | not needed | NVIDIA 8 GB+ only for the optional remote LatentSync/MuseTalk worker (**untested**) |

What was measured:
- Peak 3.3 GB RSS for the API with models loaded.
- A 10-minute 690 MB film used 1.7 GB on disk (source, stems and one export).

## E. Local install

```bash
git clone <repo> && cd "video studio ai"
python -m pip install -r backend/requirements.txt      # Python 3.12
npm ci && npm run build                               # builds dist/ (served by the API)
copy .env.example .env                                # optional for local use; AUTH_MODE=off is fine on localhost
cd backend
python -m app.manage init-db
python -m uvicorn app.main:app --port 8000            # http://localhost:8000
# optional: queue mode
#   set PROCESSING_MODE=queue in .env, then in a second terminal:  python -m app.worker
python -m pytest -q                                   # tests
```

**Docker:** `copy .env.example .env`, set `AUTH_SECRET_KEY` and `AUTH_BOOTSTRAP_TOKEN`, then run
`docker compose up -d --build` and open http://localhost:4001.

**Lip-sync weights:** put `wav2lip_gan.pth`, `lipsync_expert.pth` and
`face_detection_yunet_2023mar.onnx` in `data/models/lipsync/`. They are research-only; see G.

## F. Production deployment requirements

See `docs/DEPLOYMENT.md`. In short:

- `ENVIRONMENT=production`
- `AUTH_MODE=password` with a 48+ character `AUTH_SECRET_KEY`
- `AUTH_BOOTSTRAP_TOKEN`, or `manage.py create-admin`
- An HTTPS reverse proxy, with `AUTH_COOKIE_SECURE=true`, explicit `CORS_ORIGINS` and `FORWARDED_ALLOW_IPS`
- `PROCESSING_MODE=queue` with at least one worker under a supervisor
- A local SSD for `DATA_DIR`
- Scheduled `manage.py backup`
- Disk monitoring
- `manage.py check-config` showing no errors
- A licence review

**Not production-proven:** it ran on one development PC and one local Docker Desktop stack. It has
not had a real public deployment, load testing with many users, or a penetration test.

## G. Model licences and restrictions

Full table: `docs/LICENSES_AND_MODELS.md`.

**Non-commercial:**
- NLLB-200 (CC-BY-NC-4.0)
- MMS-TTS (CC-BY-NC-4.0)
- Wav2Lip and SyncNet weights (research only)
- XTTS-v2, optional (CPML; outputs are restricted too)

**Commercial-friendly:**
- faster-whisper / Whisper (MIT)
- Silero VAD (MIT)
- SpeechBrain ECAPA (Apache-2.0)
- YuNet (MIT)
- IndicTrans2 (MIT)
- MADLAD-400 (Apache-2.0)
- Demucs (MIT)
- MuseTalk (MIT; sub-models vary)
- LatentSync (OpenRAIL++, with use-based restrictions)

**FFmpeg and Rubber Band:** GPL builds are used. Running them on your own server is normally fine;
**shipping** the binaries or images to others brings GPL obligations.

**Python and JS libraries:** all permissive (MIT, BSD or Apache).

## H. Cloud cost notes (no paid service was used or created)

- **This project used no cloud resources.** Nothing was deployed, purchased or signed up for.
- **CPU-only VPS:** 8 vCPU / 16 GB is the realistic size. It is billed monthly and the rate varies
  by provider and region. Storage grows by about 2.5× the size of each film, and backups double that.
- **GPU (optional, only for LatentSync/MuseTalk or faster Whisper/NLLB):**
  - Billed per hour, even when idle unless you stop it.
  - **Stop the instance when not rendering.**
  - Uploading films to a remote worker costs bandwidth. Egress (downloads) is often billed separately.
- **Hugging Face:** the model downloads used are free, but the gated IndicTrans2 needs an account
  token. No paid inference endpoints are used.
- Check current pricing yourself before turning anything on. The app never starts remote resources
  on its own; remote URLs are empty by default.

## I. What could not be tested

| Item | Reason |
|---|---|
| GPU execution (CUDA Whisper/NLLB/Wav2Lip), LatentSync/MuseTalk remote worker | No GPU on this PC; the installed PyTorch is CPU-only |
| Real cloud / remote AI worker over the internet | No cloud resources may be created (paid). Only the worker's auth was tested locally. |
| Low-RAM machine (e.g. 4 GB) | Not available. Only peak RSS was measured (3.3 GB with models loaded). |
| Full AI pipeline (Whisper/NLLB/TTS/Wav2Lip) on a long film | Hours of CPU time. Long-film testing covered upload, import, separation, cancel/retry and export only. |
| Real phones/tablets | Layout was checked in headless Chrome at 390 px and 1366 px only; touch gestures were not tested |
| Listening and watching for quality | Audio and lip sync were measured (loudness, placement, SyncNet, frame differences), not judged by a person |
| PostgreSQL, multi-host, many users at once | Out of scope for this single-host setup |
| GPU Docker image | Not built (needs a CUDA base image and the NVIDIA toolkit) |
| Penetration test by a third party | Only the in-house audit and the automated security tests |

## J. Files changed in Phase 16

All uncommitted on `phase-8-translation`.

**Backend, modified:**
- `backend/app/main.py`
- `backend/app/database.py`
- `backend/app/ai_worker.py`
- `backend/app/routers/auth.py`
- `backend/app/routers/processing.py`
- `backend/app/routers/timeline.py`
- `backend/app/services/auth.py`
- `backend/app/services/storage.py`
- `backend/app/services/timeline.py`
- `backend/app/services/lipsync_models.py`
- `backend/requirements.txt`
- `backend/tests/test_phase14_api.py`
- `workers/lipsync_worker.py`

**Backend, new:**
- `backend/app/services/final_export.py`
- `backend/app/manage.py`
- `backend/app/logging_setup.py`
- `backend/tests/test_phase16_security.py`
- `backend/tests/e2e/`: `pipeline_e2e.py`, `verify_exports.py`, `perf_reliability.py`,
  `queue_smoke.py`, `ui_smoke.py`, `README.md`

**Frontend, modified:**
- `src/App.tsx`
- `src/main.tsx`
- `src/components/Layout.tsx`
- `src/components/TimelineEditor.tsx`
- `src/components/TimelineEditor.css`
- `src/lib/timelineApi.ts`
- `src/styles.css`
- `vite.config.ts`

**Frontend, new:**
- `src/lib/auth.ts`
- `src/components/AuthGate.tsx`

**Deployment:**
- `Dockerfile`
- `docker-compose.yml`
- `.dockerignore`
- `.env.example`
- `DOCKER.md`

**Docs:**
- `docs/DEPLOYMENT.md` (new)
- `docs/LICENSES_AND_MODELS.md` (new)
- `docs/PHASE16_FINAL_REPORT.md` (this file)
- `PHASE_STATUS_REPORT.md`
