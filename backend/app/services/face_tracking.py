"""Phase 13 — face detection, tracking and active-speaker evidence.

YuNet (OpenCV Zoo, Apache-2.0) detects faces with five landmarks on every
frame of a dialogue window. Detections are linked into tracks by overlap.
For each track a mouth-motion signal is correlated with the energy of the
original dialogue audio: a face whose mouth moves when the original line is
spoken is evidence of the speaker. A visible face is never assumed to be
speaking without that evidence, a Phase 7 confirmation or a manual choice.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import numpy as np

from .tts_engine import ffmpeg_binary


@dataclass
class Detection:
    box: tuple[float, float, float, float]  # x, y, w, h in pixels
    score: float
    landmarks: list[tuple[float, float]]  # right eye, left eye, nose, mouth right, mouth left


@dataclass
class Track:
    id: int
    frames: dict[int, Detection] = field(default_factory=dict)
    activity: list[float] = field(default_factory=list)
    correlation: float = 0.0
    mean_activity: float = 0.0

    @property
    def coverage_frames(self) -> int:
        return len(self.frames)

    def median_box(self) -> tuple[float, float, float, float]:
        boxes = np.array([det.box for det in self.frames.values()])
        return tuple(float(v) for v in np.median(boxes, axis=0))


def frame_rate_string(fps: float) -> str:
    """ffmpeg rate string preserving NTSC rates exactly."""
    for exact in (Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001)):
        if abs(float(exact) - fps) < 0.002:
            return f"{exact.numerator}/{exact.denominator}"
    frac = Fraction(fps).limit_denominator(1001)
    return f"{frac.numerator}/{frac.denominator}"


def decode_frames(path: Path, start_frame: int, count: int, fps: float, width: int, height: int, scale_width: int | None = None):
    """Yield BGR frames [start_frame, start_frame + count) at native or scaled size."""
    out_w, out_h = width, height
    filters = []
    if scale_width and scale_width < width:
        out_w = scale_width - scale_width % 2
        out_h = int(round(height * out_w / width / 2) * 2)
        filters = ["-vf", f"scale={out_w}:{out_h}"]
    start_s = start_frame / fps
    cmd = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-ss", f"{start_s:.6f}", "-i", str(path),
           *filters, "-frames:v", str(count), "-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = out_w * out_h * 3
    try:
        for _ in range(count):
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(out_h, out_w, 3)
    finally:
        proc.stdout.close()
        proc.wait()


class FaceDetector:
    def __init__(self, model_path: Path, score_threshold: float = 0.75):
        import cv2

        if not model_path.is_file():
            raise FileNotFoundError(f"YuNet model not found at {model_path}")
        self._cv2 = cv2
        self.model_path = model_path
        self.threshold = score_threshold
        self._detector = None
        self._size = None

    def detect(self, image: np.ndarray) -> list[Detection]:
        h, w = image.shape[:2]
        if self._detector is None or self._size != (w, h):
            self._detector = self._cv2.FaceDetectorYN.create(str(self.model_path), "", (w, h), self.threshold, 0.3, 50)
            self._size = (w, h)
        _ok, faces = self._detector.detect(image)
        out = []
        for face in faces if faces is not None else []:
            x, y, bw, bh = (float(v) for v in face[:4])
            marks = [(float(face[4 + 2 * i]), float(face[5 + 2 * i])) for i in range(5)]
            out.append(Detection((x, y, bw, bh), float(face[14]), marks))
        return out


def _iou(a, b) -> float:
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    x1, y1 = max(ax1, bx1), max(ay1, by1)
    x2, y2 = min(ax1 + aw, bx1 + bw), min(ay1 + ah, by1 + bh)
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def build_tracks(per_frame: list[list[Detection]], max_gap: int = 6, min_iou: float = 0.3, min_frames: int = 3) -> list[Track]:
    tracks: list[Track] = []
    last_seen: dict[int, int] = {}
    for index, detections in enumerate(per_frame):
        used: set[int] = set()
        for det in sorted(detections, key=lambda d: -d.score):
            best, best_iou = None, min_iou
            for track in tracks:
                if track.id in used or index - last_seen[track.id] > max_gap:
                    continue
                previous = track.frames[last_seen[track.id]]
                overlap = _iou(previous.box, det.box)
                if overlap > best_iou:
                    best, best_iou = track, overlap
            if best is None:
                best = Track(id=len(tracks))
                tracks.append(best)
            best.frames[index] = det
            last_seen[best.id] = index
            used.add(best.id)
    return [track for track in tracks if track.coverage_frames >= min_frames]


def mouth_box(det: Detection, scale: float = 1.0) -> tuple[int, int, int, int]:
    """Mouth region from the two mouth-corner landmarks (x, y, w, h)."""
    (mrx, mry), (mlx, mly) = det.landmarks[3], det.landmarks[4]
    width = max(8.0, abs(mlx - mrx) * 1.6)
    cx, cy = (mrx + mlx) / 2, (mry + mly) / 2
    height = width * 0.75
    return (int((cx - width / 2) * scale), int((cy - height * 0.45) * scale), int(width * scale), int(height * scale))


def mouth_activity(frames: list[np.ndarray], track: Track) -> list[float]:
    import cv2

    patches: dict[int, np.ndarray] = {}
    for index, det in track.frames.items():
        x, y, w, h = mouth_box(det)
        frame = frames[index]
        x, y = max(0, x), max(0, y)
        crop = frame[y : y + h, x : x + w]
        if crop.size == 0:
            continue
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        patches[index] = cv2.resize(gray, (32, 24)).astype(np.float32)
    activity = [0.0] * len(frames)
    for index in sorted(patches):
        if index - 1 in patches:
            activity[index] = float(np.mean(np.abs(patches[index] - patches[index - 1])))
    return activity


def audio_energy_per_frame(audio: np.ndarray, sr: int, frames: int, fps: float) -> list[float]:
    out = []
    for i in range(frames):
        a = int(i / fps * sr)
        b = int((i + 1) / fps * sr)
        chunk = audio[a:b]
        out.append(float(np.sqrt(np.mean(chunk**2))) if chunk.size else 0.0)
    return out


def correlate(signal: list[float], energy: list[float], present: set[int]) -> float:
    idx = [i for i in range(len(signal)) if i in present and i - 1 in present]
    if len(idx) < 8:
        return 0.0
    a = np.array([signal[i] for i in idx])
    b = np.array([energy[i] for i in idx])
    # smooth both a little; mouth motion lags/leads audio by ~1 frame
    kernel = np.ones(3) / 3
    a = np.convolve(a, kernel, mode="same")
    b = np.convolve(b, kernel, mode="same")
    if a.std() < 1e-6 or b.std() < 1e-6:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


@dataclass
class Analysis:
    fps: float
    start_frame: int
    frame_count: int
    width: int
    height: int
    scale: float  # analysis frame width / original width
    tracks: list[Track]
    energy: list[float]

    def summary(self) -> list[dict]:
        out = []
        inv = 1 / self.scale
        for track in self.tracks:
            x, y, w, h = track.median_box()
            out.append({
                "track_id": track.id,
                "frames": track.coverage_frames,
                "coverage": round(track.coverage_frames / max(1, self.frame_count), 3),
                "box": [round(x * inv / self.width, 4), round(y * inv / self.height, 4),
                        round(w * inv / self.width, 4), round(h * inv / self.height, 4)],
                "first_frame": min(track.frames),
                "last_frame": max(track.frames),
                "speaking_evidence": round(track.correlation, 3),
                "mouth_activity": round(track.mean_activity, 3),
                "face_size_px": int(round(h * inv)),
            })
        return out


def analyse_window(video_path: Path, start_frame: int, frame_count: int, fps: float, width: int, height: int,
                   detector: FaceDetector, dialogue_audio: np.ndarray | None, audio_sr: int, analysis_width: int = 640) -> Analysis:
    frames = list(decode_frames(video_path, start_frame, frame_count, fps, width, height, scale_width=analysis_width))
    scale = frames[0].shape[1] / width if frames else 1.0
    per_frame = [detector.detect(frame) for frame in frames]
    tracks = build_tracks(per_frame)
    energy = audio_energy_per_frame(dialogue_audio, audio_sr, len(frames), fps) if dialogue_audio is not None else [0.0] * len(frames)
    for track in tracks:
        track.activity = mouth_activity(frames, track)
        present = set(track.frames)
        values = [track.activity[i] for i in present if i - 1 in present]
        track.mean_activity = float(np.mean(values)) if values else 0.0
        track.correlation = correlate(track.activity, energy, present)
    return Analysis(fps, start_frame, len(frames), width, height, scale, tracks, energy)


def choose_target(analysis: Analysis, manual_box: list[float] | None, phase7_box: list[float] | None) -> tuple[Track | None, str, list[dict]]:
    """Pick the face to modify. Returns (track, source, flags)."""
    flags: list[dict] = []
    tracks = analysis.tracks
    if not tracks:
        return None, "no_visible_face", [{"code": "no_visible_face", "severity": "info",
                                          "message": "No face is visible during this line (off-screen dialogue); nothing is modified."}]

    def nearest(norm_box: list[float]) -> Track | None:
        x, y, w, h = norm_box
        target = (x * analysis.width * analysis.scale, y * analysis.height * analysis.scale,
                  w * analysis.width * analysis.scale, h * analysis.height * analysis.scale)
        scored = [(max(_iou(target, det.box) for det in t.frames.values()), t) for t in tracks]
        best = max(scored, key=lambda item: item[0])
        return best[1] if best[0] > 0.1 else None

    if manual_box:
        track = nearest(manual_box)
        if track is not None:
            return track, "manual", flags
        flags.append({"code": "manual_face_not_found", "severity": "warning",
                      "message": "The face you selected was not found in this line's frames; please reselect."})
        return None, "needs_face_selection", flags
    if phase7_box:
        track = nearest(phase7_box)
        if track is not None:
            return track, "phase7_confirmed", flags
    ranked = sorted(tracks, key=lambda t: -t.correlation)
    best = ranked[0]
    second = ranked[1].correlation if len(ranked) > 1 else -1.0
    if best.correlation >= 0.3 and best.correlation - second >= 0.15 and best.coverage_frames >= 0.5 * analysis.frame_count:
        flags.append({"code": "auto_selected_face", "severity": "info",
                      "message": f"Face chosen from mouth-motion/audio evidence (r={best.correlation:.2f}). Confirm it is the speaker."})
        return best, "audio_visual_evidence", flags
    flags.append({"code": "face_selection_needed", "severity": "warning",
                  "message": f"{len(tracks)} face(s) visible but no clear speaker evidence; choose the speaking face."})
    return None, "needs_face_selection", flags
