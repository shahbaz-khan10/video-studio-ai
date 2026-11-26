"""Visual candidate detection for Phase 7.

This module detects *visible person candidates* with OpenCV Haar cascades.
It deliberately does not perform face recognition, does not assign real-world
names, and never assumes that a visible face is the person speaking.
"""

import importlib.util
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import settings
from .media import ffmpeg_binary

ProgressCallback = Callable[[float], None]
ActiveCheck = Callable[[], None]


@dataclass
class VisualCandidateDetection:
    timestamp_ms: int
    segment_id: str | None
    image_path: Path
    bbox: list[float]
    confidence: float
    track_label: str
    notes: dict = field(default_factory=dict)


@dataclass
class FaceTrackDetection:
    label: str
    start_ms: int
    end_ms: int
    confidence: float
    representative_path: Path
    bbox_samples: list[dict] = field(default_factory=list)
    appearance: dict = field(default_factory=dict)


@dataclass
class VisionResult:
    candidates: list[VisualCandidateDetection]
    tracks: list[FaceTrackDetection]
    limitations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def available() -> bool:
    return _has_module("cv2") and ffmpeg_binary() is not None


def effective_backend() -> str:
    selected = settings.visual_detection_backend.strip().lower()
    if selected == "none":
        return "none"
    if selected == "auto":
        return "opencv" if available() else "none"
    if selected == "opencv" and not available():
        return "none"
    return selected if selected in {"opencv", "none"} else "none"


def production_info() -> dict:
    backend = effective_backend()
    limitations: list[str] = []
    if backend == "none":
        limitations.append("No local face-candidate detector is available; visual association is disabled.")
    return {
        "configured_backend": settings.visual_detection_backend,
        "effective_backend": backend,
        "opencv_available": _has_module("cv2"),
        "ffmpeg_available": ffmpeg_binary() is not None,
        "detector_model": settings.visual_detector_model,
        "max_candidates": settings.visual_max_candidates,
        "frame_width": settings.visual_frame_width,
        "face_recognition": False,
        "identity_claims": False,
        "requires_human_confirmation": True,
        "supports_offscreen_speakers": True,
        "license": "OpenCV: Apache-2.0",
        "confidence_note": "Haar cascade detections are uncalibrated candidate scores, not identity confidence.",
        "limitations": limitations,
    }


def sample_dialogue_points(
    segments: list[dict],
    turns: list[dict],
    max_candidates: int,
    duration_ms: int = 0,
) -> list[dict]:
    if max_candidates <= 0:
        return []
    points: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for item in segments:
        start = max(0, int(item.get("start_ms", 0)))
        end = max(start, int(item.get("end_ms", start)))
        timestamp = (start + end) // 2
        if duration_ms > 0:
            timestamp = min(timestamp, max(0, duration_ms - 1))
        key = (str(item.get("id") or "segment"), timestamp)
        if key in seen:
            continue
        seen.add(key)
        points.append(
            {
                "timestamp_ms": timestamp,
                "segment_id": item.get("id"),
                "start_ms": start,
                "end_ms": end,
            }
        )
    for item in turns:
        start = max(0, int(item.get("start_ms", 0)))
        end = max(start, int(item.get("end_ms", start)))
        timestamp = (start + end) // 2
        if duration_ms > 0:
            timestamp = min(timestamp, max(0, duration_ms - 1))
        key = ("turn", timestamp)
        if key in seen:
            continue
        seen.add(key)
        points.append(
            {
                "timestamp_ms": timestamp,
                "segment_id": None,
                "start_ms": start,
                "end_ms": end,
            }
        )
    points.sort(key=lambda item: item["timestamp_ms"])
    if len(points) <= max_candidates:
        return points
    indexes = [round(index * (len(points) - 1) / (max_candidates - 1)) for index in range(max_candidates)]
    return [points[index] for index in sorted(set(indexes))]


def extract_frame(video_path: Path, timestamp_ms: int, output_path: Path) -> bool:
    binary = ffmpeg_binary()
    if not binary:
        return False
    output_path.parent.mkdir(parents=True, exist_ok=True)
    width = max(160, int(settings.visual_frame_width))
    result = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0, timestamp_ms) / 1000:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-2",
            "-q:v",
            "4",
            "-y",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        errors="ignore",
        timeout=45,
        check=False,
    )
    return result.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0


def _detector() -> Any:
    import cv2

    cascade_file = str(Path(cv2.data.haarcascades) / f"{settings.visual_detector_model}.xml")
    detector = cv2.CascadeClassifier(cascade_file)
    if detector.empty():
        raise RuntimeError(f"OpenCV cascade could not be loaded: {cascade_file}")
    return detector


def _detect_in_image(image_path: Path) -> tuple[list[dict], tuple[int, int]]:
    import cv2

    image = cv2.imread(str(image_path))
    if image is None:
        return [], (0, 0)
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    faces = _detector().detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=4,
        minSize=(max(24, width // 20), max(24, height // 20)),
    )
    results: list[dict] = []
    for x, y, face_w, face_h in faces or []:
        results.append(
            {
                "bbox": [
                    round(float(x / width), 5),
                    round(float(y / height), 5),
                    round(float(face_w / width), 5),
                    round(float(face_h / height), 5),
                ],
                "pixel_bbox": [int(x), int(y), int(face_w), int(face_h)],
            }
        )
    return results, (width, height)


def _crop_face(image_path: Path, output_path: Path, pixel_bbox: list[int]) -> dict:
    import cv2

    image = cv2.imread(str(image_path))
    if image is None:
        return {}
    x, y, width, height = pixel_bbox
    height_full, width_full = image.shape[:2]
    margin_x = max(4, int(width * 0.18))
    margin_y = max(4, int(height * 0.18))
    x1 = max(0, x - margin_x)
    y1 = max(0, y - margin_y)
    x2 = min(width_full, x + width + margin_x)
    y2 = min(height_full, y + height + margin_y)
    crop = image[y1:y2, x1:x2]
    if crop.size == 0:
        return {}
    cv2.imwrite(str(output_path), crop)
    mean = cv2.mean(crop)[:3]
    return {
        "approx_mean_bgr": [round(float(value), 2) for value in mean],
        "crop_width": int(crop.shape[1]),
        "crop_height": int(crop.shape[0]),
    }


def _assign_tracks(candidates: list[VisualCandidateDetection], max_gap_ms: int = 3000) -> list[FaceTrackDetection]:
    tracks: list[FaceTrackDetection] = []
    for candidate in candidates:
        x, y, width, height = candidate.bbox
        center = (x + width / 2, y + height / 2)
        chosen: FaceTrackDetection | None = None
        best = 0.28
        for track in tracks:
            if candidate.timestamp_ms - track.end_ms > max_gap_ms:
                continue
            last = track.bbox_samples[-1]["bbox"]
            last_center = (last[0] + last[2] / 2, last[1] + last[3] / 2)
            distance = math.dist(center, last_center)
            size_change = abs(width - last[2]) + abs(height - last[3])
            score = distance + size_change * 0.25
            if score < best:
                best = score
                chosen = track
        if chosen is None:
            chosen = FaceTrackDetection(
                label=f"VISIBLE_{len(tracks):02d}",
                start_ms=candidate.timestamp_ms,
                end_ms=candidate.timestamp_ms,
                confidence=candidate.confidence,
                representative_path=candidate.image_path,
            )
            tracks.append(chosen)
        candidate.track_label = chosen.label
        chosen.end_ms = max(chosen.end_ms, candidate.timestamp_ms)
        chosen.start_ms = min(chosen.start_ms, candidate.timestamp_ms)
        chosen.confidence = max(chosen.confidence, candidate.confidence)
        chosen.bbox_samples.append(
            {
                "timestamp_ms": candidate.timestamp_ms,
                "bbox": candidate.bbox,
                "segment_id": candidate.segment_id,
            }
        )
        if candidate.notes.get("appearance"):
            chosen.appearance = candidate.notes["appearance"]
    return tracks


def detect_candidates(
    video_path: Path,
    points: list[dict],
    destination: Path,
    check_active: ActiveCheck | None = None,
    on_progress: ProgressCallback | None = None,
) -> VisionResult:
    backend = effective_backend()
    if backend == "none" or not points:
        return VisionResult(
            candidates=[],
            tracks=[],
            limitations=production_info()["limitations"],
            notes=["No visual candidates were sampled."] if not points else [],
        )

    destination.mkdir(parents=True, exist_ok=True)
    candidates: list[VisualCandidateDetection] = []
    skipped = 0
    for index, point in enumerate(points):
        if check_active:
            check_active()
        timestamp_ms = max(0, int(point["timestamp_ms"]))
        frame_path = destination / f"dialogue-frame-{timestamp_ms:08d}.jpg"
        if not extract_frame(video_path, timestamp_ms, frame_path):
            skipped += 1
            if on_progress:
                on_progress((index + 1) / len(points))
            continue
        try:
            faces, _size = _detect_in_image(frame_path)
        except Exception:
            faces = []
        for face_index, face in enumerate(faces):
            crop_path = destination / f"face-{timestamp_ms:08d}-{face_index:02d}.jpg"
            appearance = _crop_face(frame_path, crop_path, face["pixel_bbox"])
            if not crop_path.is_file():
                continue
            candidates.append(
                VisualCandidateDetection(
                    timestamp_ms=timestamp_ms,
                    segment_id=str(point["segment_id"]) if point.get("segment_id") else None,
                    image_path=crop_path,
                    bbox=face["bbox"],
                    confidence=0.55,
                    track_label="",
                    notes={"appearance": appearance, "detector": settings.visual_detector_model},
                )
            )
        if on_progress:
            on_progress((index + 1) / len(points))

    tracks = _assign_tracks(candidates)

    limitations = [
        "Visible-person candidates are detection boxes, not face recognition or identity matches.",
        "A visible face is never assumed to be speaking; all speaker links require manual confirmation.",
        "Haar cascade confidence is an uncalibrated candidate score.",
    ]
    if skipped:
        limitations.append(f"{skipped} sampled frame(s) could not be extracted from the source video.")
    notes = [
        f"Sampled {len(points)} dialogue-adjacent timestamps.",
        f"Found {len(candidates)} visible-person candidate(s) in {len(tracks)} approximate track(s).",
        "Off-screen and voice-over speakers legitimately have no visual candidate.",
    ]
    return VisionResult(candidates=candidates, tracks=tracks, limitations=limitations, notes=notes)
