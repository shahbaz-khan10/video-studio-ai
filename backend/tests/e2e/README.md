# Phase 16 end-to-end tools

None of these use mocks. They drive a real running server (and Chrome for the UI test), so pytest
does not collect them. Run them by hand:

| Script | What it checks |
|---|---|
| `pipeline_e2e.py` | All 16 pipeline stages through the HTTP API, with production auth on |
| `verify_exports.py` | Exported audio placement and sync, the external sting position, and lip-sync frame changes |
| `perf_reliability.py` | A large chunked upload with a mid-chunk network drop, resume after a client restart, a long-video import, cancel and retry, a long export, and peak RSS |
| `queue_smoke.py` | Queue mode: jobs run by a separate worker (used against the Docker stack) |
| `ui_smoke.py` | Real headless Chrome: sign-in, panels, export render and download, mobile and desktop layout, sign-out |

Test media is not committed. Phase 16 used:

- a still, synthetic (not a real person) face picture made into an 18.5 s, 720×720 clip, with a Windows TTS voice
- a 10-minute 1280×720 `testsrc2` clip (690 MB)
