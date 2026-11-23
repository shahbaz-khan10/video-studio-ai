# CINEMA AI DUB — API Design

**Phase:** Phase 1 — Project Architecture (documentation only)
**Status:** Proposed contract. No endpoint has been implemented.

---

## 1. Conventions

- Base path: `/api/v1`
- Transport: HTTPS only in deployed environments.
- Auth: `Authorization: Bearer <JWT>` (short-lived access token) plus rotating refresh token in an HttpOnly cookie.
- Content types: `application/json`; uploads via `multipart/form-data` or resumable chunk protocol.
- IDs: UUID strings.
- Timestamps: ISO-8601 UTC.
- All request/response examples below are illustrative of the proposed contract, not live output.

Standard list response envelope:

```json
{
  "items": [],
  "page": 1,
  "per_page": 20,
  "total": 0
}
```

Standard error envelope:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Target language 'fr' is not supported.",
    "details": { "field": "target_languages" },
    "request_id": "01J...X"
  }
}
```

---

## 2. Authentication and project management

### 2.1 Auth endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/register` | Create local account. |
| POST | `/auth/login` | Issue access + refresh tokens. |
| POST | `/auth/refresh` | Rotate access token. |
| POST | `/auth/logout` | Revoke refresh token. |
| GET | `/auth/me` | Current user profile. |
| POST | `/auth/password/change` | Change password (Argon2id). |

Example — `POST /auth/login`

```json
{ "email": "director@example.com", "password": "correct-horse-battery" }
```

```json
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 900,
  "user": { "id": "9f1c...", "display_name": "Director", "role": "owner" }
}
```

### 2.2 Projects

| Method | Path | Purpose |
|---|---|---|
| GET | `/projects` | List owned/shared projects. |
| POST | `/projects` | Create project with source and target languages. |
| GET | `/projects/{id}` | Project detail. |
| PATCH | `/projects/{id}` | Rename, settings, status. |
| DELETE | `/projects/{id}` | Soft delete. |
| POST | `/projects/{id}/share` | Share with another user (role-limited). |
| GET | `/projects/{id}/activity` | Audit trail. |

Example — `POST /projects`

```json
{
  "name": "Monsoon Nights — Urdu/Hindi Dub",
  "source_language": "en",
  "target_languages": ["ur", "hi"],
  "settings": { "lip_sync_enabled": true, "remove_original_dialogue": true }
}
```

```json
{
  "id": "3f9a...",
  "name": "Monsoon Nights — Urdu/Hindi Dub",
  "status": "draft",
  "created_at": "2026-11-23T10:00:00Z"
}
```

---

## 3. Media upload and video

| Method | Path | Purpose |
|---|---|---|
| POST | `/uploads/init` | Start resumable upload; returns upload ID and chunk size. |
| PUT | `/uploads/{upload_id}/chunks/{index}` | Upload a chunk. |
| POST | `/uploads/{upload_id}/complete` | Assemble, hash, verify. |
| GET | `/uploads/{upload_id}` | Upload status. |
| POST | `/projects/{id}/videos` | Attach completed upload as project video. |
| GET | `/videos/{id}` | Video metadata and probe result. |
| POST | `/videos/{id}/reprobe` | Re-run probe (recovery). |

Example — `POST /uploads/init`

```json
{ "filename": "scene1.mp4", "size_bytes": 2147483648, "mime_type": "video/mp4" }
```

```json
{
  "upload_id": "u-8ac1...",
  "chunk_size_bytes": 8388608,
  "total_chunks": 256,
  "expires_at": "2026-11-24T10:00:00Z"
}
```

Example — `GET /videos/{id}`

```json
{
  "id": "v-2b...",
  "duration_ms": 5420000,
  "width": 1920,
  "height": 1080,
  "fps": 24,
  "video_codec": "h264",
  "audio_codec": "aac",
  "probe_status": "complete"
}
```

---

## 4. Processing jobs

| Method | Path | Purpose |
|---|---|---|
| POST | `/jobs` | Create a pipeline job or a single-stage job. |
| GET | `/jobs/{id}` | Job status including steps. |
| GET | `/jobs?project_id=&status=` | List jobs. |
| POST | `/jobs/{id}/cancel` | Cooperative cancellation. |
| POST | `/jobs/{id}/retry` | Retry failed/cancelled job. |
| POST | `/jobs/{id}/steps/{step_id}/retry` | Retry a single step. |
| GET | `/jobs/{id}/events` | SSE stream of progress events. |
| GET | `/jobs/{id}/report` | Per-step diagnostics. |

Example — `POST /jobs` (full pipeline)

```json
{
  "project_id": "3f9a...",
  "video_id": "v-2b...",
  "type": "full_dub",
  "options": {
    "dialogue_removal": "demix",
    "translate_to": ["ur", "hi"],
    "lip_sync": true,
    "preferred_tts_engine": "auto"
  }
}
```

```json
{
  "id": "job-7c1...",
  "status": "queued",
  "steps": [
    { "id": "s1", "name": "probe", "status": "pending" },
    { "id": "s2", "name": "extract_audio", "status": "pending" },
    { "id": "s3", "name": "demix", "status": "pending" },
    { "id": "s4", "name": "asr", "status": "pending" },
    { "id": "s5", "name": "diarize", "status": "pending" },
    { "id": "s6", "name": "detect_characters", "status": "pending" },
    { "id": "s7", "name": "translate", "status": "pending" },
    { "id": "s8", "name": "timing_fit", "status": "pending" },
    { "id": "s9", "name": "tts", "status": "pending" },
    { "id": "s10", "name": "lipsync", "status": "pending" },
    { "id": "s11", "name": "mix", "status": "pending" },
    { "id": "s12", "name": "export", "status": "pending" }
  ]
}
```

SSE event example:

```text
event: step_progress
data: {"job_id":"job-7c1...","step_id":"s4","name":"asr","progress":0.62,"message":"Transcribing 62%"}
```

---

## 5. Characters, voice profiles, dialogue and translations

| Method | Path | Purpose |
|---|---|---|
| GET | `/projects/{id}/characters` | List detected characters. |
| PATCH | `/characters/{id}` | Rename, merge, assign appearance. |
| POST | `/characters/{id}/merge` | Merge two detected identities. |
| POST | `/voice-profiles` | Create profile (characteristic-based or reference-based). |
| GET | `/voice-profiles?project_id=` | List profiles. |
| POST | `/voice-profiles/{id}/preview` | Synthesize a calibration sentence. |
| GET | `/videos/{id}/segments` | Dialogue segments with timings. |
| PATCH | `/segments/{id}` | Correct source text, timing, character. |
| GET | `/segments/{id}/translations` | Translations by language. |
| PUT | `/segments/{id}/translations/{lang}` | Approve/edit translation. |
| POST | `/segments/{id}/regenerate-tts` | Generate new take. |
| GET | `/segments/{id}/tts-takes?language=ur` | List takes. |
| POST | `/segments/{id}/select-take` | Choose active take. |

Example — `POST /voice-profiles`

```json
{
  "project_id": "3f9a...",
  "character_id": "c-1...",
  "name": "Hero — deep male",
  "language": "ur",
  "characteristics": { "deep": 0.8, "heavy": 0.4, "soft": 0.2, "male": true, "female": false },
  "reference_asset_id": null,
  "consent_status": "none"
}
```

```json
{
  "id": "vp-4d...",
  "engine": "local-tts",
  "model_revision": "2026-11-01",
  "status": "ready"
}
```

Example — `PUT /segments/{id}/translations/ur`

```json
{
  "translated_text": "یہ رات بہت لمبی ہے۔",
  "approved_by_user": true
}
```

---

## 6. Timeline, external audio, mixing and export

| Method | Path | Purpose |
|---|---|---|
| GET | `/projects/{id}/timeline` | Full timeline state. |
| POST | `/projects/{id}/timeline/operations` | Apply edit operation (trim/move/gain/…). |
| POST | `/projects/{id}/timeline/undo` | Undo last operation. |
| GET | `/projects/{id}/timeline/waveform?track_id=&resolution=` | Peak JSON. |
| POST | `/projects/{id}/audio/import` | Import external audio (resumable upload flow). |
| PATCH | `/audio-tracks/{id}` | Gain, mute, solo, pan, placement. |
| POST | `/projects/{id}/mix/preview` | Render preview mix. |
| GET | `/projects/{id}/mix/state` | Current mix graph and validation result. |
| POST | `/exports` | Start export. |
| GET | `/exports/{id}` | Export status + QC report. |
| GET | `/exports/{id}/download?track=urdu` | Signed download URL. |

Example — `POST /timeline/operations`

```json
{
  "op": "trim",
  "clip_id": "clip-9...",
  "new_start_ms": 12500,
  "new_duration_ms": 3400,
  "expected_revision": 7
}
```

Example — `POST /exports`

```json
{
  "project_id": "3f9a...",
  "preset": "all_tracks",
  "options": {
    "include_original": true,
    "languages": ["ur", "hi"],
    "lip_sync_applied": true,
    "loudness_target_lufs": -16
  }
}
```

```json
{
  "id": "exp-1a...",
  "status": "queued",
  "expected_tracks": ["original", "urdu", "hindi"]
}
```

---

## 7. Error handling and status semantics

| HTTP | `error.code` | Meaning |
|---|---|---|
| 400 | `VALIDATION_ERROR` | Malformed or unsupported request. |
| 401 | `UNAUTHENTICATED` | Missing/expired token. |
| 403 | `FORBIDDEN` | Not owner/shared user or role too low. |
| 404 | `NOT_FOUND` | Missing or inaccessible resource (do not leak existence). |
| 409 | `CONFLICT` | Revision mismatch, duplicate job, overlapping edit. |
| 413 | `FILE_TOO_LARGE` | Quota/size exceeded. |
| 422 | `UNPROCESSABLE_MEDIA` | Corrupt or unsupported media after probing. |
| 429 | `RATE_LIMITED` / `QUEUE_FULL` | Slow down or retry later. |
| 503 | `CAPACITY_UNAVAILABLE` | No GPU capacity for the requested stage. |

Job failures use explicit machine-readable codes such as `MODEL_OOM`, `MODEL_LOAD_FAILED`, `SEP_LOW_CONFIDENCE`, `ASR_EMPTY`, `TTS_SILENT`, `LIPSYNC_FRAME_MISMATCH`, `EXPORT_QC_FAILED`. The API always returns what was preserved and the next suggested action.

---

## 8. Security and file access

1. **Authorization:** every resource lookup is scoped to the authenticated user's projects; 404 used instead of 403 where existence would leak.
2. **Signed URLs:** media download URLs expire (default 15 minutes) and are scoped to one user/project/asset.
3. **Upload safety:** no path traversal; filenames sanitized; MIME sniffed; media re-probed server-side; payload size and chunk count limited.
4. **No client-side inference:** browsers never receive model weights or unrestricted file paths.
5. **Secrets:** stored in environment/secret manager, never in responses or logs; no keys embedded in frontend bundles.
6. **Audit:** auth events, job creation, exports, sharing and downloads logged with `request_id`.
7. **Consent:** reference voice upload requires a recorded consent flag; revocation disables further TTS for that profile.
8. **Rate limits:** per-user upload, job creation and export limits; global queue caps.
9. **CORS:** explicit allowlist, no wildcard with credentials.
10. **Transport:** HSTS, TLS 1.2+, secure cookies.

---

## 9. Versioning and compatibility

- API version in path (`/api/v1`); breaking changes require `/api/v2`.
- Optional fields added without breaking clients.
- Job option schemas are validated server-side; unknown options are rejected rather than silently ignored.
- Mobile clients detect older server versions via `GET /api/v1/info` and degrade gracefully.
