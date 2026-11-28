# Licences of models and libraries (Phase 16 audit)

Audit date: 2026-11-28. **This is an engineering summary, not legal advice.** Licences change.
Read the licence file or model card at the source before any commercial release, and keep
copies of the licences you rely on.

## Summary: can this build be used commercially?

**Not as installed by default.** These parts of the default pipeline are non-commercial:

| Default component | What it does | Licence | Commercial use |
|---|---|---|---|
| NLLB-200 distilled 600M (`facebook/nllb-200-distilled-600M`) | Urdu/Hindi translation fallback | CC-BY-NC-4.0 (weights) | **No** |
| MMS-TTS (`facebook/mms-tts-urd-script_arabic`, `facebook/mms-tts-hin`) | Urdu/Hindi speech | CC-BY-NC-4.0 (weights) | **No** |
| Wav2Lip GAN + SyncNet expert (`wav2lip_gan.pth`, `lipsync_expert.pth`) | Lip sync and its sync score | Research / non-commercial (the authors' terms; trained on LRS2) | **No.** The authors point commercial users to their company. |

Before commercial use, each of these needs a commercial licence or a replacement, for example:

| Need | Replacement with a permissive licence | Status in the app |
|---|---|---|
| Translation | IndicTrans2 (MIT; gated download, needs `HF_TOKEN` once) or MADLAD-400 (Apache-2.0, large) | Supported: `TRANSLATION_BACKEND=indictrans2` or `madlad` |
| Speech | Piper voices with a permissive model card (check each voice), or a self-hosted worker | Supported in Phase 9/10; check the licence of each voice |
| Lip sync | MuseTalk (MIT code and weights, but the bundled sub-models have their own licences) or LatentSync (OpenRAIL++, with use-based restrictions) on your own GPU worker | Remote worker contract exists (`workers/lipsync_worker.py`). **Not run in this project** (no GPU). |

Generated output can also be restricted. XTTS-v2 (CPML) covers outputs, not only the weights, so
dubs made with it are non-commercial too.

## AI models

| Model | Used for | Licence | Notes |
|---|---|---|---|
| faster-whisper tiny/small (CTranslate2 conversions of OpenAI Whisper) | Transcription | MIT (code and Whisper weights) | Commercial use OK |
| Silero VAD | Voice activity | MIT | OK |
| SpeechBrain `spkrec-ecapa-voxceleb` | Speaker embeddings | Apache-2.0 (model card) | OK. Trained on VoxCeleb, whose dataset terms are research-oriented; take legal advice if this matters. |
| OpenCV Haar cascade (`haarcascade_frontalface_default`) | Face candidates (Phase 7) | OpenCV licence (Apache-2.0 for OpenCV 4.5+; the cascade file has the Intel licence header) | OK |
| YuNet `face_detection_yunet_2023mar.onnx` (OpenCV Zoo) | Lip-sync face detection | MIT | OK |
| NLLB-200 distilled 600M | Translation (default when IndicTrans2 is absent) | **CC-BY-NC-4.0** | Non-commercial |
| IndicTrans2 en-indic dist 200M | Translation (optional) | MIT | Gated download on Hugging Face |
| MADLAD-400 3B | Translation (optional) | Apache-2.0 | Needs a lot of RAM/VRAM |
| Helsinki-NLP opus-mt-en-ur / en-hi | Found in the model cache; **not referenced by the current code** | CC-BY-4.0 | Can be deleted |
| Qwen2.5-7B-Instruct via Ollama | Optional translation refiner (off by default) | Apache-2.0 for 7B; other sizes have different licences | Self-hosted only |
| MMS-TTS urd/hin | Dubbed speech | **CC-BY-NC-4.0** | Non-commercial |
| Piper voices | Optional voices | Piper code is MIT (original) / GPL-3.0 (piper1-gpl fork); **each voice has its own licence** in its MODEL_CARD | The app shows each voice's licence and blocks previews when it is unknown |
| Coqui XTTS-v2 | Optional cloning (remote) | MPL-2.0 code; **CPML-1.0 weights and outputs, non-commercial** | Not installed |
| Demucs htdemucs | Optional stem separation | MIT | Not installed here; the built-in DSP separator was used |
| Wav2Lip GAN, SyncNet expert | Lip sync and its sync score | **Research / non-commercial** | Downloaded from a Hugging Face mirror (`camenduru/Wav2Lip`). Integrity was checked by loading every tensor with strict=True. |
| LatentSync 1.5 | Optional remote GPU lip sync | OpenRAIL++ (use-based restrictions) | Not run |
| MuseTalk 1.5 | Optional remote GPU lip sync | MIT (sub-models vary) | Not run |

## Native tools

| Tool | Licence | Notes |
|---|---|---|
| FFmpeg | LGPL-2.1+, or **GPL** when built with `--enable-gpl` (libx264, libx265, rubberband) | Builds that use libx264 (needed for H.264 lip-sync re-encodes) and the `rubberband` filter (Phase 11 time-stretch) are GPL: the Windows gyan.dev "essentials" build here (`--enable-gpl --enable-version3`, so GPLv3), the imageio-ffmpeg binaries and Debian's `ffmpeg` package in the Docker image. Running FFmpeg as a separate program on your own server is normally fine. **Shipping** those binaries (installers, Docker images you give to customers) means following the GPL: offer the corresponding source. |
| Rubber Band library | GPL-2.0+ (commercial licence sold separately) | Reached through FFmpeg's `rubberband` filter |

## Python libraries (versions installed and tested)

| Package | Version | Licence |
|---|---|---|
| fastapi | 0.165.0 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |
| sqlalchemy | 2.1.9 | MIT |
| pydantic / pydantic-settings | 2.14.4 / 2.16.3 | MIT |
| python-multipart | 0.0.34 | Apache-2.0 |
| imageio-ffmpeg | 0.8.1 | BSD-2-Clause (the bundled ffmpeg is GPL, see above) |
| numpy | 2.5.5 | BSD-3-Clause (plus bundled permissive licences) |
| scipy | 1.18.1 | BSD-3-Clause |
| scikit-learn | 1.9.1 | BSD-3-Clause |
| torch / torchaudio (CPU) | 2.15.0 / 2.11.1 | BSD-style / Apache-2.0 mix |
| transformers | 5.24.1 | Apache-2.0 |
| sentencepiece | 0.2.3 | Apache-2.0 |
| faster-whisper / ctranslate2 | 1.2.1 / 4.4.0 | MIT |
| av (PyAV) | 15.1.0 | BSD-3-Clause (its wheels bundle LGPL FFmpeg libraries) |
| speechbrain | 1.1.2 | Apache-2.0 |
| silero-vad | 6.3 | MIT |
| opencv-python-headless | 4.14.0.94 | Apache-2.0 |
| httpx | 0.28.1 | BSD-3-Clause |
| psutil | 7.2.2 | BSD-3-Clause |
| websockets | 17.4 | BSD-3-Clause |
| pytest | 9.2.0 | MIT (test only) |

## Frontend libraries

React 19 and react-dom (MIT), Vite and @vitejs/plugin-react (MIT), and TypeScript (Apache-2.0).
The production bundle includes only React; the rest are build tools.

## Paid services

None. The app calls no paid AI APIs. Remote URLs (`*_REMOTE_URL`) are empty by default and only
point at workers you host yourself.
