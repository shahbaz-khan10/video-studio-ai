# CINEMA AI DUB — Phase Status Report

**Date:** 27 November 2026
**Branch:** `phase-8-translation`
**Phases complete:** 11 of 16 (69%). **Remaining:** 5 (Phases 12–16).

## Khulasa (summary)

- **11 phases mukammal hain (1 se 11).** 5 baqi hain (12 se 16).
- **Saare automated tests pass hain:** 118 pass, aur 4 opt-in real-model tests jaan boojh kar skip hain.
- **Phase 1–10 git mein commit ho chuke hain.** Phase 11 abhi commit nahi hua; files tayyar hain.
- **Apni awaaz se check karein.** Awaaz ki quality aur timing maine naap kar check ki hai, kaan se nahi suni.

---

## 1. Count

| | Phases | Share |
|---|---|---|
| Complete | 11 | 69% |
| Remaining | 5 | 31% |
| **Total** | **16** | |

## 2. Phase-by-phase status

| # | Phase | Status | Committed | Tests |
|---|---|---|---|---|
| 1 | Project Architecture | ✅ Complete | ✅ `efe91f4` | architecture docs (no code tests) |
| 2 | Professional User Interface | ✅ Complete | ✅ `445cd9a` | 7 (`test_api.py`) |
| 3 | Video Processing Engine | ✅ Complete | ✅ `445cd9a` | 4 |
| 4 | Audio Extraction & Separation | ✅ Complete | ✅ `b950f8d` | 6 |
| 5 | Original Dialogue Control | ✅ Complete | ✅ `b950f8d` | 4 |
| 6 | Speech Recognition | ✅ Complete | ✅ `b950f8d` | 9 (+1 real-model, opt-in) |
| 7 | Character Detection | ✅ Complete | ✅ `b950f8d` | 6 (+1 real-model, opt-in) |
| 8 | Translation Engine | ✅ Complete | ✅ `a60cdd8` | 29 (+1 real-model, opt-in) |
| 9 | Character Voice Profiles | ✅ Complete | ✅ `dbde922` | 10 |
| 10 | Natural Voice Generation | ✅ Complete | ✅ `9e0f3a6` | 22 (+1 real-model, opt-in) |
| 11 | Dialogue Timing & Synchronization | ✅ Complete | ⚠️ **Not yet committed** | 21 |
| 12 | Audio Mixing Studio | ⏳ Not started | — | — |
| 13 | AI Lip Sync & Facial Synchronization | ⏳ Not started | — | — |
| 14 | Professional Timeline Editor | ⏳ Not started | — | — |
| 15 | Device & Processing Optimization | ⏳ Not started | — | — |
| 16 | Final Testing & Production | ⏳ Not started | — | — |

The test counts are the number of test functions in each file. Phase 11 has 20 functions, and one runs once each for Urdu and Hindi, giving 21 test runs. Latest full run: **118 passed, 4 skipped, 0 failed** (about 2.5 minutes).

## 3. What each phase delivers

### Phase 1 — Project Architecture
The system architecture, database design, API design, AI pipeline, technology stack and the 16-phase roadmap, in `docs/architecture/`.

### Phase 2 — Professional User Interface
The React + Vite frontend: dashboard, projects list, project workspace and Studio page, with loading and error states.

### Phase 3 — Video Processing Engine
Resumable chunked uploads, file validation, checksum, metadata probing (ffprobe), frame index, thumbnails and versioned media assets. Video is streamed without re-encoding.

### Phase 4 — Audio Extraction & Separation
Source audio is extracted and separated into dialogue, music, effects and ambience stems (DSP, Demucs, or a self-hosted worker), each with a preview and validation.

### Phase 5 — Original Dialogue Control
A per-project audio mixer with volume, mute and solo for each stem, and a mixed preview synchronized with the video.

### Phase 6 — Speech Recognition
Open-source faster-whisper transcription on CPU, with word timestamps and an editable, timestamped transcript.

### Phase 7 — Character Detection
Speaker diarization (Silero VAD + SpeechBrain ECAPA + clustering) and visible face candidates (OpenCV). Characters can be reviewed, renamed, merged and split.

### Phase 8 — Translation Engine
English → Urdu (Urdu script + Roman Urdu) and Hindi, using NLLB-200 by default, with IndicTrans2 and MADLAD as options.
- **Context:** characters, relationships (aap / tum / tu), gender agreement, idioms, locked names, a glossary and scene notes.
- **Editor:** side-by-side editing, undo/redo, comparing versions, history, and quality flags. Manual edits are never silently overwritten.

### Phase 9 — Character Voice Profiles
A versioned voice profile per character and language: engine, voice, pitch, tone, speed and accent. Includes consented reference samples, presets and real previews.

### Phase 10 — Natural Voice Generation
Synthetic Urdu and Hindi dialogue in each character's voice, from Phase 9 profiles or a built-in MMS-TTS voice.
- **Voices stay consistent across scenes.**
- **Emotion controls appear only where the engine actually supports them.**
- **Per line:** regenerate, approve, preview, download.
- **Batch jobs:** progress, cancel, and retry of failed lines.
- **Quality checks:** clipping, empty audio and corrupted files.

### Phase 11 — Dialogue Timing & Synchronization
Each generated line is fitted into its original time window using the gentlest method that works, in this order: shorten pauses, use the silence after the line, start slightly early, then speed up within natural limits.
- **Limits:** speech is never sped up past 1.25×, and no words are ever cut.
- **Lines that don't fit** are flagged, with suggestions.
- **Overlaps:** dialogue that overlaps in the original keeps each speaker in their own window.
- **Timeline:** waveforms, drag to move, manual timing, and approval.
- **Video preview** plays the dub in sync, also after seeking.

## 4. Measured results (Phases 8–11)

| Area | Result |
|---|---|
| Translation (NLLB-200, CPU) | ~0.87 s per sentence; 3.5 GB RAM. Register and gender fixes verified on real output. |
| Voice generation (MMS-TTS, CPU) | ~0.4 s per line (about 4× faster than real time); 0.75 GB RAM; no clipping. |
| Voice intelligibility (Whisper round trip) | Urdu ~12%, Hindi ~19–21% character error (this includes Whisper's own mistakes). |
| Voice consistency (speaker similarity) | Same character across scenes 0.63–0.73; different characters 0.08–0.10. |
| Timing fit speed | ~0.04 s per line, about 1 minute for a 2-hour film. |
| Timing naturalness | No intelligibility loss up to 1.12×. One Hindi line degraded at 1.25×, which is flagged. |
| Video ↔ dub sync (headless Chrome) | Clips started within ~0 ms of their placed time, after start and after seeking. Speaker output latency is not included. |
| End-to-end demo | Upload → Whisper → characters → translation → voices → timing: all 5 Urdu and 5 Hindi lines fit at natural speed. |

## 5. Open items and known limitations

1. **Commit Phase 11.** Its files are ready in the working tree but not yet committed.
2. **Licences:** the default translation model (NLLB-200) and the default voice (MMS-TTS) are **non-commercial**. For a commercial release, switch to IndicTrans2 or MADLAD, and to commercially licensed voices.
3. **Built-in voice:** MMS-TTS has one male-sounding voice per language. Female characters get a raised pitch and sound synthetic. Better voices need Phase 9 Piper voices or a self-hosted GPU worker.
4. **Emotion:** none of the local engines supports emotional delivery. Only a self-hosted worker that declares emotion support can use it.
5. **Human listening:** voice quality and the sped-up timing were measured, not listened to. Please review them by ear.
6. **Merging:** Phases 8–11 are on branch `phase-8-translation`. To bring them to the main branch: `git switch ai_dub_video` then `git merge phase-8-translation`.

## 6. Next phase

**Phase 12 — Audio Mixing Studio.** It can build on the per-language dialogue tracks rendered in Phase 11 and the stems from Phase 4.

## 7. How to run

```bash
cd backend && pip install -r requirements.txt
cd backend && python -m uvicorn app.main:app --port 8000   # API + built UI at http://localhost:8000
npm install && npm run dev                                 # development UI at http://localhost:5173
cd backend && python -m pytest -q                          # full test suite
```
