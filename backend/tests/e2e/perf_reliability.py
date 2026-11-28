"""Phase 16 — large upload / long video / interruption / cancel+retry tests against a live server.

usage: python perf_reliability.py BASE EMAIL PASSWORD FILM SERVER_PORT
"""
import json, sys, threading, time
from pathlib import Path

import httpx
import psutil

sys.stdout.reconfigure(encoding="utf-8")
BASE, EMAIL, PASSWORD, FILM, PORT = sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4]), int(sys.argv[5])
c = httpx.Client(base_url=BASE + "/api/v1", timeout=600)
results = []


def server_proc():
    for conn in psutil.net_connections("tcp"):
        if conn.laddr and conn.laddr.port == PORT and conn.status == "LISTEN":
            return psutil.Process(conn.pid)


PROC = server_proc()
peak = {"rss": 0}
stop = threading.Event()


def sampler():
    while not stop.is_set():
        try:
            rss = PROC.memory_info().rss + sum(ch.memory_info().rss for ch in PROC.children(recursive=True))
            peak["rss"] = max(peak["rss"], rss)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)


threading.Thread(target=sampler, daemon=True).start()


def record(name, ok, detail):
    results.append({"test": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'} {name} {json.dumps(detail)[:400]}", flush=True)


def ok(r, codes=(200, 201, 202)):
    if r.status_code not in codes:
        raise RuntimeError(f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def wait(job_id, timeout=3600):
    t = time.time()
    while time.time() - t < timeout:
        j = c.get(f"/jobs/{job_id}").json()
        if j["status"] in {"completed", "failed", "cancelled"}:
            return j
        time.sleep(1)
    raise TimeoutError(job_id)


ok(c.post("/auth/login", json={"email": EMAIL, "password": PASSWORD}))
project = ok(c.post("/projects", json={"name": "Perf long film", "source_language": "en", "target_languages": ["ur", "hi"]}))
size_total = FILM.stat().st_size
session = ok(c.post(f"/uploads/projects/{project['id']}/sessions",
                    json={"filename": FILM.name, "mime_type": "video/mp4", "size_bytes": size_total}))
chunk, total = session["chunk_size"], session["total_chunks"]
sid = session["id"]
fh = FILM.open("rb")


def read_chunk(i):
    fh.seek(i * chunk)
    return fh.read(chunk)


# 1) network drop in the middle of a chunk body: the server must not store a half chunk.
def dying_body(data):
    yield data[: len(data) // 2]
    raise ConnectionResetError("simulated network drop")


try:
    c.put(f"/uploads/sessions/{sid}/chunks/0", content=dying_body(read_chunk(0)), headers={"Content-Type": "application/octet-stream"})
    dropped = "request unexpectedly completed"
except Exception as error:  # noqa: BLE001
    dropped = type(error).__name__
time.sleep(1)
status = ok(c.get(f"/uploads/sessions/{sid}"))
record("mid-chunk disconnect leaves no partial chunk", 0 not in status["uploaded_chunks"], {"client_error": dropped, "uploaded": status["uploaded_chunks"]})

# 2) a truncated chunk (wrong size) is rejected
r = c.put(f"/uploads/sessions/{sid}/chunks/0", content=read_chunk(0)[:1000], headers={"Content-Type": "application/octet-stream"})
record("short chunk rejected", r.status_code in (400, 409, 422), {"status": r.status_code})

# 3) upload ~half the chunks out of order, "lose" the client, then resume from the server's list
t0 = time.time()
first = list(range(0, total // 2))[::-1]
for i in first:
    ok(c.put(f"/uploads/sessions/{sid}/chunks/{i}", content=read_chunk(i), headers={"Content-Type": "application/octet-stream"}))
c.close()
c = httpx.Client(base_url=BASE + "/api/v1", timeout=600)  # new client = browser tab reopened
ok(c.post("/auth/login", json={"email": EMAIL, "password": PASSWORD}))
status = ok(c.get(f"/uploads/sessions/{sid}"))
have = set(status["uploaded_chunks"])
record("server remembers chunks after client restart", have == set(first), {"have": len(have), "expected": len(first)})
# duplicate re-send of an already uploaded chunk is harmless
ok(c.put(f"/uploads/sessions/{sid}/chunks/{first[0]}", content=read_chunk(first[0]), headers={"Content-Type": "application/octet-stream"}))
for i in range(total):
    if i not in have:
        ok(c.put(f"/uploads/sessions/{sid}/chunks/{i}", content=read_chunk(i), headers={"Content-Type": "application/octet-stream"}))
upload_s = time.time() - t0
done = ok(c.post(f"/uploads/sessions/{sid}/complete"))
record("large upload completes", True, {"mb": round(size_total / 1e6), "chunks": total, "chunk_mb": chunk / 1e6,
                                        "seconds": round(upload_s, 1), "mb_per_s": round(size_total / 1e6 / upload_s, 1)})
vid = done["video"]["id"]
t = time.time()
j = wait(done["job"]["id"])
video = ok(c.get(f"/videos/{vid}"))
record("long video import + metadata", j["status"] == "completed" and abs((video.get("duration_ms") or 0) / 1000 - 600) < 1,
       {"status": j["status"], "seconds": round(time.time() - t, 1), "duration_s": (video.get("duration_ms") or 0) / 1000,
        "w": video.get("width"), "h": video.get("height")})

# 4) cancel a running job, then retry it
job = ok(c.post(f"/videos/{vid}/audio/separate"))
for _ in range(120):
    if c.get(f"/jobs/{job['id']}").json()["status"] == "running":
        break
    time.sleep(0.5)
time.sleep(3)
ok(c.post(f"/jobs/{job['id']}/cancel"))
j = wait(job["id"], 300)
record("cancel running separation", j["status"] == "cancelled", {"status": j["status"], "progress": j.get("progress")})
retry = ok(c.post(f"/jobs/{job['id']}/retry"))
t = time.time()
j = wait(retry["id"])
bundle = ok(c.get(f"/videos/{vid}/audio"))
record("retry completes 10-min separation", j["status"] == "completed",
       {"status": j["status"], "seconds": round(time.time() - t, 1), "error": j.get("error_message"),
        "bundle_keys": sorted(bundle)[:8], "stems": str(bundle.get("stems"))[:160]})

# 5) timeline export of the 10-minute video (original audio, multitrack, video copied)
tl = ok(c.get(f"/videos/{vid}/timeline"))
t = time.time()
job = ok(c.post(f"/timeline/{tl['id']}/export", json={"mode": "multitrack", "container": "mkv", "languages": ["en"], "include_beds": True, "use_lipsync": False}))
j = wait(job["id"])
detail = {"status": j["status"], "seconds": round(time.time() - t, 1), "error": j.get("error_message")}
if j["status"] == "completed":
    out = ok(c.get(f"/jobs/{job['id']}/timeline-export"))["outputs"][0]
    detail.update({"duration_s": out["duration_s"], "video": out["video"], "size_mb": round(out["size_bytes"] / 1e6), "streams": len(out["streams"])})
record("10-min multitrack export (video copied)", j["status"] == "completed" and abs((detail.get("duration_s") or 0) - 600) < 1, detail)

stop.set()
record("server peak RSS during run", True, {"peak_mb": round(peak["rss"] / 1e6)})
Path(sys.argv[4]).with_suffix(".results.json").write_text(json.dumps(results, indent=1))
print(f"\n{sum(r['ok'] for r in results)}/{len(results)} passed")
