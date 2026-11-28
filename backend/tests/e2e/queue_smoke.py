"""Queue-mode smoke: jobs must be executed by the separate worker process, not the API.

usage: python queue_smoke.py BASE FILM BOOTSTRAP_TOKEN
"""
import json, sys, time
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8")
BASE, FILM, TOKEN = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
c = httpx.Client(base_url=BASE + "/api/v1", timeout=600)
res = []


def rec(name, ok, detail=None):
    res.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {name} {json.dumps(detail, default=str)[:300] if detail is not None else ''}", flush=True)


def ok(r, codes=(200, 201, 202)):
    if r.status_code not in codes:
        raise RuntimeError(f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def wait(job_id, timeout=900):
    t = time.time()
    while time.time() - t < timeout:
        j = c.get(f"/jobs/{job_id}").json()
        if j["status"] in {"completed", "failed", "cancelled"}:
            return j
        time.sleep(1)
    raise TimeoutError(job_id)


r = c.post("/auth/register", json={"email": "ops@example.org", "password": "ops password 1234"})
rec("first account without bootstrap token refused", r.status_code == 403, r.status_code)
ok(c.post("/auth/register", json={"email": "ops@example.org", "password": "ops password 1234", "bootstrap_token": TOKEN}))
p = ok(c.post("/projects", json={"name": "Docker queue", "source_language": "en", "target_languages": ["ur"]}))
data = FILM.read_bytes()
s = ok(c.post(f"/uploads/projects/{p['id']}/sessions", json={"filename": FILM.name, "mime_type": "video/mp4", "size_bytes": len(data)}))
for i in range(s["total_chunks"]):
    ok(c.put(f"/uploads/sessions/{s['id']}/chunks/{i}", content=data[i * s["chunk_size"]:(i + 1) * s["chunk_size"]],
             headers={"Content-Type": "application/octet-stream"}))
done = ok(c.post(f"/uploads/sessions/{s['id']}/complete"))
j = wait(done["job"]["id"])
rec("import job ran in worker", j["status"] == "completed", {k: j.get(k) for k in ("status", "worker_id", "attempts", "error_message")})
vid = done["video"]["id"]
j = wait(ok(c.post(f"/videos/{vid}/audio/separate"))["id"])
rec("separation job ran in worker", j["status"] == "completed", {k: j.get(k) for k in ("status", "worker_id", "error_message")})
j = wait(ok(c.post(f"/videos/{vid}/transcript/transcribe", json={"language": "en", "prefer_dialogue_stem": False, "word_timestamps": True}))["id"])
tr = c.get(f"/videos/{vid}/transcript").json()
rec("transcription (faster-whisper) in container", j["status"] == "completed", {"status": j["status"], "error": j.get("error_message"),
                                                                                "segments": len(tr.get("segments", [])) if isinstance(tr, dict) else None})
tl = ok(c.get(f"/videos/{vid}/timeline"))
j = wait(ok(c.post(f"/timeline/{tl['id']}/export", json={"mode": "multitrack", "container": "mp4", "languages": ["en"], "use_lipsync": False}))["id"])
detail = {"status": j["status"], "error": j.get("error_message")}
if j["status"] == "completed":
    out = ok(c.get(f"/jobs/{j['id']}/timeline-export"))["outputs"][0]
    detail.update(duration=out["duration_s"], streams=[(x["type"], x["language"]) for x in out["streams"]], titles=out["titles"])
    body = c.get(f"/jobs/{j['id']}/timeline-export/files/0", params={"download": 1})
    detail["download"] = (body.status_code, len(body.content))
rec("export in container (system ffmpeg)", j["status"] == "completed", detail)
workers = c.get("/processing/status")
rec("worker visible to admin", workers.status_code == 200 and "json" in workers.headers.get("content-type", ""), workers.text[:200])
missing = c.get("/no-such-route")
rec("unknown API route is JSON 404", missing.status_code == 404 and "json" in missing.headers.get("content-type", ""), missing.status_code)
print(f"\n{sum(res)}/{len(res)} passed")
