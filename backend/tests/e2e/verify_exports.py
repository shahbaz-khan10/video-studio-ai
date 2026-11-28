"""Inspect exported files: audio content/sync per stream and lip-sync frame changes.

usage: python verify_exports.py OUT_DIR FFMPEG SOURCE_FILM.mp4
OUT_DIR is the pipeline_e2e.py output. The mouth/eye regions are tuned for the 720x720
portrait test film used in Phase 16; adjust them for other footage.
"""
import json, subprocess, sys
from pathlib import Path
import numpy as np

EXP = Path(sys.argv[1]); FF = sys.argv[2]; SRC = sys.argv[3]
report = json.loads((EXP / "report.json").read_text(encoding="utf-8"))
place = report["ctx"]
out = {}


def audio(path, stream, sr=16000):
    raw = subprocess.run([FF, "-loglevel", "error", "-i", str(path), "-map", f"0:a:{stream}", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"],
                         capture_output=True).stdout
    return np.frombuffer(raw, dtype="<f4")


def rms(x):
    return float(20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)) if x.size else -120.0


def frames(path, w=720, h=720):
    raw = subprocess.run([FF, "-loglevel", "error", "-i", str(path), "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                         capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, h, w)


mkv = EXP / "multitrack.mkv"
names = ["Original English", "Urdu Dub", "Hindi Dub", "Music", "SFX", "Ambience", "External"]
streams = {}
for i, name in enumerate(names):
    a = audio(mkv, i)
    streams[name] = {"seconds": round(a.size / 16000, 2), "rms_db": round(rms(a), 1)}
out["multitrack_streams"] = streams

# dialogue placement check: energy inside placed dub windows vs outside
for lang, name in (("ur", "Urdu Dub"), ("hi", "Hindi Dub")):
    a = audio(mkv, names.index(name))
    t_in = np.concatenate([a[int(s * 16):int(e * 16)] for s, e in place[f"placements_{lang}"]])
    mask = np.ones(a.size, bool)
    for s, e in place[f"placements_{lang}"]:
        mask[int(s * 16):int(e * 16)] = False
    onsets = []
    env = np.convolve(np.abs(a), np.ones(160) / 160, mode="same")
    for s, _e in place[f"placements_{lang}"]:
        seg = env[int((s - 300) * 16):int((s + 600) * 16)]
        onsets.append(round((np.argmax(seg > 0.1 * env.max()) / 16) - 300, 1))
    out[f"{lang}_dialogue"] = {"inside_windows_db": round(rms(t_in), 1), "outside_windows_db": round(rms(a[mask]), 1),
                               "onset_vs_placement_ms": onsets}

# external audio sting placed at 500 ms
ext = audio(mkv, names.index("External"))
env = np.convolve(np.abs(ext), np.ones(80) / 80, mode="same")
out["external_onset_ms"] = round(np.argmax(env > 0.2 * env.max()) / 16, 1)

# lip sync: frame differences vs original inside / outside lip-sync windows (Urdu export)
orig = frames(Path(SRC))
for lang in ("ur", "hi", "en"):
    exp = frames(EXP / f"Main timeline_{lang}.mp4")
    n = min(len(orig), len(exp))
    diff = np.abs(exp[:n].astype(np.int16) - orig[:n].astype(np.int16))
    mouth = diff[:, 470:640, 240:480].mean(axis=(1, 2))   # lower face region of the 720x720 portrait
    upper = diff[:, 150:330, 200:520].mean(axis=(1, 2))   # eyes/forehead
    windows = place.get(f"placements_{lang}", [])
    inside = np.zeros(n, bool)
    for s, e in windows:
        inside[int(s / 40):int(e / 40)] = True
    out[f"frames_{lang}"] = {"frames": [len(orig), len(exp)],
                             "mouth_diff_inside": round(float(mouth[inside].mean()), 2) if inside.any() else None,
                             "mouth_diff_outside": round(float(mouth[~inside].mean()), 2),
                             "upper_face_diff_inside": round(float(upper[inside].mean()), 2) if inside.any() else None}
print(json.dumps(out, indent=1))
