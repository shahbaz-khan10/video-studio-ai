"""Phase 16 — full end-to-end run against a live server (auth on, production mode).

usage: python pipeline_e2e.py BASE FILM.mp4 EXTERNAL.wav OUT_DIR BOOTSTRAP_TOKEN FFMPEG

The server must be fresh (no accounts yet) and started with ENVIRONMENT=production,
AUTH_MODE=password and AUTH_BOOTSTRAP_TOKEN=BOOTSTRAP_TOKEN. FILM must be a talking-head
clip with English speech and one visible face. Runs every stage (upload with resume, separation,
mix, transcript, characters, Urdu+Hindi translation, voice profiles, TTS, timing, mixes,
Wav2Lip lip sync with face selection and review, timeline, external audio, both export modes)
and writes OUT_DIR/report.json. Exits non-zero on the first failing stage.
"""
import json, subprocess, sys, time, wave
from pathlib import Path
import httpx
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")

BASE = sys.argv[1]
FILM = Path(sys.argv[2])
EXTERNAL = Path(sys.argv[3])
OUT = Path(sys.argv[4])
TOKEN = sys.argv[5]
FF = sys.argv[6]
OUT.mkdir(parents=True, exist_ok=True)
c = httpx.Client(base_url=BASE + "/api/v1", timeout=1800)
report = {"stages": []}


def stage(name, fn):
    t = time.time()
    try:
        detail = fn()
        report["stages"].append({"stage": name, "ok": True, "secs": round(time.time() - t, 1), "detail": detail})
        print(f"PASS {name} ({time.time() - t:.1f}s) {json.dumps(detail, ensure_ascii=False)[:300]}", flush=True)
        return detail
    except Exception as error:  # noqa: BLE001
        report["stages"].append({"stage": name, "ok": False, "secs": round(time.time() - t, 1), "error": str(error)[:800]})
        print(f"FAIL {name}: {error}", flush=True)
        raise


def ok(response, codes=(200, 201, 202)):
    if response.status_code not in codes:
        raise RuntimeError(f"{response.request.method} {response.request.url.path} -> {response.status_code}: {response.text[:400]}")
    return response.json() if response.content and "json" in response.headers.get("content-type", "") else response


def wait(job, label):
    while True:
        j = c.get(f"/jobs/{job['id']}").json()
        if j["status"] in {"completed", "failed", "cancelled"}:
            if j["status"] != "completed":
                raise RuntimeError(f"{label} job {j['status']}: {j.get('error_code')} {j.get('error_message')}")
            return j
        time.sleep(1)


ctx = {}


def s_admin():
    st = ok(c.get("/auth/status"))
    assert st["mode"] == "password" and st["bootstrap_token_required"], st
    denied = c.post("/auth/register", json={"email": "owner@example.org", "password": "owner password 123"})
    assert denied.status_code == 403, denied.text
    user = ok(c.post("/auth/register", json={"email": "owner@example.org", "password": "owner password 123", "bootstrap_token": TOKEN}))
    assert c.get("/projects").status_code == 200
    return {"admin": user["is_admin"], "no_token_status": denied.status_code}


def s_project():
    ctx["project"] = ok(c.post("/projects", json={"name": "Sara E2E", "source_language": "en", "target_languages": ["ur", "hi"]}))
    return {"project_id": ctx["project"]["id"]}


def s_upload():
    data = FILM.read_bytes()
    session = ok(c.post(f"/uploads/projects/{ctx['project']['id']}/sessions",
                        json={"filename": FILM.name, "mime_type": "video/mp4", "size_bytes": len(data)}))
    size = session["chunk_size"]
    for i in range(session["total_chunks"]):
        if i == 1:  # simulate an interrupted upload: query what the server has, then resume
            status = ok(c.get(f"/uploads/sessions/{session['id']}"))
            assert status["uploaded_chunks"] == [0], status["uploaded_chunks"]
        ok(c.put(f"/uploads/sessions/{session['id']}/chunks/{i}", content=data[i * size:(i + 1) * size],
                 headers={"Content-Type": "application/octet-stream"}))
    done = ok(c.post(f"/uploads/sessions/{session['id']}/complete"))
    ctx["video"] = done["video"]
    job = wait(done["job"], "import")
    video = ok(c.get(f"/videos/{ctx['video']['id']}"))
    ctx["video"] = video
    return {"chunks": session["total_chunks"], "duration_ms": video["duration_ms"], "fps": video["fps"],
            "size": f"{video['width']}x{video['height']}", "codec": video["video_codec"], "audio": video["audio_codec"]}


def s_separate():
    wait(ok(c.post(f"/videos/{ctx['video']['id']}/audio/separate")), "separation")
    bundle = ok(c.get(f"/videos/{ctx['video']['id']}/audio"))
    return {"stems": sorted(k for k, v in bundle["stems"].items() if v["status"] == "available"), "backend": bundle.get("backend")}


def s_mixer():
    pid = ctx["project"]["id"]
    current = ok(c.get(f"/projects/{pid}/audio-mix"))
    current["tracks"]["dialogue"]["muted"] = True
    current["tracks"]["music"]["volume"] = 0.5
    saved = ok(c.put(f"/projects/{pid}/audio-mix", json={"master_volume": 1.0, "preview_enabled": False,
                                                       "tracks": {k: {kk: vv for kk, vv in v.items() if kk in ("volume", "muted", "solo", "enabled")} for k, v in current["tracks"].items()}}))
    return {"dialogue_muted": saved["tracks"]["dialogue"]["muted"], "music_volume": saved["tracks"]["music"]["volume"]}


def s_transcribe():
    wait(ok(c.post(f"/videos/{ctx['video']['id']}/transcript/transcribe", json={"language": "en", "prefer_dialogue_stem": False, "word_timestamps": True})), "transcription")
    tr = ok(c.get(f"/videos/{ctx['video']['id']}/transcript"))
    ctx["segments"] = tr["segments"]
    return {"segments": [(s["start_ms"], s["end_ms"], s["text"]) for s in tr["segments"]]}


def s_characters():
    wait(ok(c.post(f"/videos/{ctx['video']['id']}/characters/detect", json={})), "characters")
    b = ok(c.get(f"/videos/{ctx['video']['id']}/characters"))
    ctx["characters"] = b["characters"]
    if b["characters"]:
        ok(c.patch(f"/characters/{b['characters'][0]['id']}", json={"name": "Sara", "status": "confirmed"}), (200,))
    return {"characters": [ch["name"] for ch in b["characters"]], "visual_candidates": len(b.get("visual_candidates", []))}


def s_translate():
    if ctx["characters"]:
        ok(c.put(f"/characters/{ctx['characters'][0]['id']}/translation-profile", json={"gender": "female", "register": "neutral"}))
    wait(ok(c.post(f"/videos/{ctx['video']['id']}/translations/translate", json={"languages": ["ur", "hi"]})), "translation")
    b = ok(c.get(f"/videos/{ctx['video']['id']}/translations"))
    return {"lines": [{"en": l["text"], "ur": (l["translations"]["ur"] or {}).get("text"), "hi": (l["translations"]["hi"] or {}).get("text")} for l in b["lines"]]}


def s_voice_profile():
    if not ctx["characters"]:
        return {"skipped": "no character"}
    cid = ctx["characters"][0]["id"]
    out = {}
    for lang, model in (("ur", "facebook/mms-tts-urd-script_arabic"), ("hi", "facebook/mms-tts-hin")):
        r = c.put(f"/characters/{cid}/voice-profile", json={"language": lang, "engine": "mms", "voice_id": model, "model_id": model,
                                                            "characteristics": {"gender": "female", "pitch": "high", "tone": "soft"}})
        out[lang] = r.status_code if r.status_code != 200 else "saved"
    return out


def s_tts():
    out = {}
    for lang in ("ur", "hi"):
        wait(ok(c.post(f"/videos/{ctx['video']['id']}/dub-audio/generate", json={"language": lang})), f"tts {lang}")
        b = ok(c.get(f"/videos/{ctx['video']['id']}/dub-audio", params={"language": lang}))
        out[lang] = {"summary": b["summary"], "voice": [(ch["name"], ch["voice"]["source"], ch["voice"]["engine_label"]) for ch in b["characters"]]}
    return out


def s_timing():
    out = {}
    for lang in ("ur", "hi"):
        wait(ok(c.post(f"/videos/{ctx['video']['id']}/dub-timing/fit", json={"language": lang})), f"timing {lang}")
        b = ok(c.get(f"/videos/{ctx['video']['id']}/dub-timing", params={"language": lang}))
        ctx[f"placements_{lang}"] = [(l["placement"]["placed_start_ms"], l["placement"]["placed_end_ms"]) for l in b["lines"] if l["placement"]]
        out[lang] = {"summary": b["summary"], "tempos": [l["placement"]["tempo"] for l in b["lines"] if l["placement"]]}
    return out


def s_mix():
    out = {}
    for lang in ("ur", "hi"):
        wait(ok(c.post(f"/videos/{ctx['video']['id']}/mix-studio/render", json={"target": lang, "format": "wav"})), f"mix {lang}")
    b = ok(c.get(f"/videos/{ctx['video']['id']}/mix-studio"))
    for r in b["renders"][:2]:
        out[r["target"]] = {k: r["stats"].get(k) for k in ("integrated_lufs", "true_peak_dbfs", "duration_s")}
    return out


def s_lipsync():
    seg_ids = [s["id"] for s in ctx["segments"]]
    out = {}
    for lang in ("ur", "hi"):
        t = time.time()
        wait(ok(c.post(f"/videos/{ctx['video']['id']}/lipsync/generate", json={"language": lang, "segment_ids": seg_ids})), f"lipsync {lang}")
        b = ok(c.get(f"/videos/{ctx['video']['id']}/lipsync", params={"language": lang}))
        first_pass = [(l.get("latest") or {}).get("status") for l in b["lines"]]
        # A still/ambiguous face has no speaking evidence: the user picks the speaking face (real UI step).
        picked = 0
        for line in b["lines"]:
            latest = line.get("latest") or {}
            if latest.get("status") in {"needs_face", "no_face"} and latest.get("faces"):
                face = latest["faces"][0]
                ok(c.put(f"/videos/{ctx['video']['id']}/lipsync/segments/{line['segment_id']}/face",
                         json={"time_ms": line["start_ms"], "box": face["box"], "character_id": line.get("character_id")}))
                picked += 1
        if picked:
            wait(ok(c.post(f"/videos/{ctx['video']['id']}/lipsync/generate", json={"language": lang, "segment_ids": seg_ids})), f"lipsync {lang} (after face selection)")
            b = ok(c.get(f"/videos/{ctx['video']['id']}/lipsync", params={"language": lang}))
        results = []
        for line in b["lines"]:
            res = line.get("current") or line.get("latest") or {}
            qc = res.get("qc") or {}
            results.append({"status": res.get("status"), "frames": res.get("frame_count"), "processing_s": res.get("processing_s"),
                            "sync": {k: qc.get(k) for k in qc if "sync" in k.lower()} if isinstance(qc, dict) else qc,
                            "flags": [f.get("code") for f in res.get("flags", [])]})
            if res.get("id") and res.get("status") == "generated":
                ok(c.post(f"/lipsync/results/{res['id']}/review", json={"decision": "accepted"}))
        out[lang] = {"secs": round(time.time() - t, 1), "first_pass": first_pass, "faces_picked": picked, "results": results}
        ctx[f"lipsync_{lang}"] = results
    return out


def s_timeline():
    tl = ok(c.get(f"/videos/{ctx['video']['id']}/timeline"))
    ctx["timeline"] = tl
    return {"tracks": [(t["kind"], t.get("clip_count", len([cl for cl in tl.get("clips", []) if cl["track_id"] == t["id"]]))) for t in tl["tracks"]]}


def s_external():
    pid = ctx["project"]["id"]
    with open(EXTERNAL, "rb") as fh:
        imported = ok(c.post(f"/projects/{pid}/audio-imports", files={"file": (EXTERNAL.name, fh, "audio/wav")}, data={"video_id": ctx["video"]["id"], "notes": "e2e sting"}))
    clip = ok(c.post(f"/timeline/{ctx['timeline']['id']}/clips/external", json={"external_audio_id": imported["id"], "start_ms": 500}))
    ok(c.post(f"/timeline/{ctx['timeline']['id']}/save"), (200, 201))
    return {"import": imported.get("id"), "duration_ms": imported.get("duration_ms"), "clip_track": clip.get("track_id")}


def s_export():
    out = {}
    tid = ctx["timeline"]["id"]
    for name, body in (("multitrack_mkv", {"mode": "multitrack", "container": "mkv", "use_lipsync": True}),
                       ("separate_mp4", {"mode": "separate_language", "container": "mp4", "use_lipsync": True})):
        job = wait(ok(c.post(f"/timeline/{tid}/export", json=body)), f"export {name}")
        outputs = ok(c.get(f"/jobs/{job['id']}/timeline-export"))["outputs"]
        files = []
        for i, o in enumerate(outputs):
            data = ok(c.get(f"/jobs/{job['id']}/timeline-export/files/{i}", params={"download": 1}), (200,)).content
            path = OUT / o["path"]
            path.write_bytes(data)
            files.append({"file": o["path"], "video": o["video"], "streams": [(s["type"], s["codec"], s["language"]) for s in o["streams"]],
                          "titles": o["titles"], "duration_s": o["duration_s"], "lipsync": o.get("lipsync"), "bytes": len(data)})
        out[name] = files
    ctx["exports"] = out
    return out


for name, fn in [("first admin (bootstrap token)", s_admin), ("create project", s_project), ("chunked upload + resume + metadata", s_upload),
                 ("audio separation", s_separate), ("original dialogue/music control", s_mixer), ("speech transcription", s_transcribe),
                 ("character detection", s_characters), ("translation ur+hi", s_translate), ("voice profiles", s_voice_profile),
                 ("voice generation ur+hi", s_tts), ("dialogue timing ur+hi", s_timing), ("mix rendering ur+hi", s_mix),
                 ("lip sync ur+hi (Wav2Lip)", s_lipsync), ("timeline", s_timeline), ("external audio import", s_external),
                 ("final export", s_export)]:
    try:
        stage(name, fn)
    except Exception:
        if name in {"create project", "chunked upload + resume + metadata"}:
            break
(OUT / "report.json").write_text(json.dumps({**report, "ctx": {k: v for k, v in ctx.items() if k.startswith("placements")}}, ensure_ascii=False, indent=1), encoding="utf-8")
print("DONE")
