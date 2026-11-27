"""Phase 12 — professional audio mixing studio.

Tracks are real files produced earlier; nothing is synthesised here:

    original_dialogue  Phase 4 ``stem_dialogue``
    music              Phase 4 ``stem_music``
    sfx                Phase 4 ``stem_sfx``
    ambience           Phase 4 ``stem_ambience``
    urdu_dub           Phase 11 rendered Urdu dialogue track (Phase 10 voices, fitted timing)
    hindi_dub          Phase 11 rendered Hindi dialogue track

Rendering is one ffmpeg filter graph streamed over the whole film (constant
memory), with an optional measuring pass for EBU R128 loudness normalisation.
Inputs are only read; outputs go to a new file.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import MediaAsset, ProcessingJob, Project, Video, new_id, utcnow
from ..models_mix import MixPreset, MixRender, MixStudioSetting
from . import dub_timing
from .media import AppError
from .storage import storage
from .tts_engine import ffmpeg_binary

ActiveCheck = Callable[[], None]
SAMPLE_RATE = 48000
TARGETS = ("original", "ur", "hi")
FORMATS = ("wav", "aac")
HISTORY_LIMIT = 15
RENDER_STEP_NAMES = ["collect_tracks", "measure_loudness", "render_mix", "validate", "finalize"]

TRACKS: list[dict] = [
    {"role": "original_dialogue", "label": "Original English Dialogue", "kind": "dialogue", "source": "stem_dialogue"},
    {"role": "urdu_dub", "label": "Urdu Dub", "kind": "dialogue", "source": "dub:ur"},
    {"role": "hindi_dub", "label": "Hindi Dub", "kind": "dialogue", "source": "dub:hi"},
    {"role": "music", "label": "Background Music", "kind": "bed", "source": "stem_music"},
    {"role": "sfx", "label": "Sound Effects", "kind": "bed", "source": "stem_sfx"},
    {"role": "ambience", "label": "Ambience", "kind": "bed", "source": "stem_ambience"},
]
ROLES = [track["role"] for track in TRACKS]
TRACK_BY_ROLE = {track["role"]: track for track in TRACKS}
DUB_ROLE = {"ur": "urdu_dub", "hi": "hindi_dub"}

# Which controls are rendered exactly in the browser preview vs only in the
# backend render (the UI labels the latter "render only").
LIVE_CONTROLS = ["volume_db", "muted", "solo", "pan", "enabled", "eq", "compressor", "gain", "limiter"]
RENDER_ONLY_CONTROLS = ["noise_reduction", "fades", "ducking", "loudness"]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _track_defaults(role: str) -> dict:
    dialogue = TRACK_BY_ROLE[role]["kind"] == "dialogue"
    dub = role in ("urdu_dub", "hindi_dub")
    return {
        "volume_db": {"original_dialogue": 0.0, "urdu_dub": 0.0, "hindi_dub": 0.0, "music": -3.0, "sfx": -2.0, "ambience": -6.0}[role],
        "muted": False,
        "solo": False,
        "pan": 0.0,
        "enabled": True,
        "gain_db": 0.0,
        # Gentle clarity defaults for synthetic dub dialogue only; stems are left as separated.
        "eq": {"enabled": dub, "highpass_hz": 80 if dialogue else 20, "low_gain_db": 0.0, "mid_freq_hz": 3000, "mid_gain_db": 2.0 if dub else 0.0, "high_gain_db": 0.0},
        "noise_reduction": {"enabled": False, "amount_db": 12.0},
        "compressor": {"enabled": dub, "threshold_db": -22.0, "ratio": 2.5, "attack_ms": 10.0, "release_ms": 150.0, "makeup_db": 2.0 if dub else 0.0},
        "fade_in_ms": 0,
        "fade_out_ms": 0,
    }


def default_settings() -> dict:
    return {
        "version": 1,
        "keep_original_dialogue_in_dub": False,
        "ducking": {"enabled": True, "threshold_db": -30.0, "ratio": 6.0, "attack_ms": 20.0, "release_ms": 350.0},
        "master": {
            "gain_db": 0.0,
            "limiter": {"enabled": True, "ceiling_db": -1.0},
            "loudness": {"enabled": True, "target_lufs": -16.0},
            "fade_in_ms": 0,
            "fade_out_ms": 0,
        },
        "tracks": {role: _track_defaults(role) for role in ROLES},
    }


RANGES = {
    "volume_db": (-60.0, 12.0),
    "pan": (-1.0, 1.0),
    "gain_db": (-24.0, 24.0),
    "highpass_hz": (20.0, 400.0),
    "low_gain_db": (-12.0, 12.0),
    "mid_freq_hz": (300.0, 8000.0),
    "mid_gain_db": (-12.0, 12.0),
    "high_gain_db": (-12.0, 12.0),
    "amount_db": (3.0, 40.0),
    "threshold_db": (-50.0, -3.0),
    "ratio": (1.0, 20.0),
    "attack_ms": (0.1, 500.0),
    "release_ms": (10.0, 3000.0),
    "makeup_db": (0.0, 18.0),
    "fade_in_ms": (0.0, 20000.0),
    "fade_out_ms": (0.0, 20000.0),
    "ceiling_db": (-12.0, -0.1),
    "target_lufs": (-31.0, -9.0),
}


def _num(value, key: str, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if math.isnan(number) or math.isinf(number):
        return fallback
    low, high = RANGES[key]
    return min(high, max(low, number))


def _merge_numbers(base: dict, incoming: dict) -> dict:
    out = dict(base)
    for key, value in (incoming or {}).items():
        if key not in out:
            continue
        if isinstance(out[key], bool):
            out[key] = bool(value)
        elif isinstance(out[key], (int, float)) and key in RANGES:
            out[key] = _num(value, key, out[key])
    return out


def normalize_settings(payload: dict | None) -> dict:
    payload = payload or {}
    if not isinstance(payload, dict):
        raise AppError(422, "INVALID_MIX_SETTINGS", "Mix settings must be an object.")
    tracks = payload.get("tracks") or {}
    if not isinstance(tracks, dict):
        raise AppError(422, "INVALID_MIX_SETTINGS", "Tracks must be an object.")
    unknown = sorted(set(tracks) - set(ROLES))
    if unknown:
        raise AppError(422, "INVALID_MIX_TRACK", f"Unknown track: {unknown[0]}")
    out = default_settings()
    out["keep_original_dialogue_in_dub"] = bool(payload.get("keep_original_dialogue_in_dub", False))
    out["ducking"] = _merge_numbers(out["ducking"], payload.get("ducking") or {})
    master = payload.get("master") or {}
    out["master"]["gain_db"] = _num(master.get("gain_db", 0.0), "gain_db", 0.0)
    out["master"]["fade_in_ms"] = _num(master.get("fade_in_ms", 0), "fade_in_ms", 0)
    out["master"]["fade_out_ms"] = _num(master.get("fade_out_ms", 0), "fade_out_ms", 0)
    out["master"]["limiter"] = _merge_numbers(out["master"]["limiter"], master.get("limiter") or {})
    out["master"]["loudness"] = _merge_numbers(out["master"]["loudness"], master.get("loudness") or {})
    for role in ROLES:
        incoming = tracks.get(role)
        if not isinstance(incoming, dict):
            continue
        track = out["tracks"][role]
        for key in ("volume_db", "pan", "gain_db", "fade_in_ms", "fade_out_ms"):
            if key in incoming:
                track[key] = _num(incoming[key], key, track[key])
        for key in ("muted", "solo", "enabled"):
            if key in incoming:
                track[key] = bool(incoming[key])
        for group in ("eq", "noise_reduction", "compressor"):
            if isinstance(incoming.get(group), dict):
                track[group] = _merge_numbers(track[group], incoming[group])
    return out


def _fingerprint(settings: dict) -> str:
    return hashlib.sha1(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:16]


def _project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise AppError(404, "NOT_FOUND", "Project not found")
    return project


def get_setting(db: Session, project_id: str) -> MixStudioSetting:
    _project(db, project_id)
    row = db.scalar(select(MixStudioSetting).where(MixStudioSetting.project_id == project_id))
    if row is None:
        row = MixStudioSetting(project_id=project_id, settings_json=default_settings(), history_json=[], revision=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _set(db: Session, row: MixStudioSetting, settings: dict, reason: str) -> MixStudioSetting:
    previous = row.settings_json or default_settings()
    if _fingerprint(previous) != _fingerprint(settings):
        history = list(row.history_json or [])
        history.append({"settings": previous, "reason": reason, "at": utcnow().isoformat(), "revision": row.revision})
        row.history_json = history[-HISTORY_LIMIT:]
        row.settings_json = settings
        row.revision = (row.revision or 1) + 1
    db.commit()
    db.refresh(row)
    return row


def settings_payload(row: MixStudioSetting) -> dict:
    return {
        "project_id": row.project_id,
        "settings": normalize_settings(row.settings_json),
        "revision": row.revision,
        "history": [{"reason": item["reason"], "at": item["at"], "revision": item["revision"]} for item in (row.history_json or [])],
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def save_settings(db: Session, project_id: str, payload: dict, base_revision: int | None = None) -> MixStudioSetting:
    row = get_setting(db, project_id)
    if base_revision is not None and base_revision != row.revision:
        raise AppError(409, "MIX_REVISION_CONFLICT", f"Mixer settings changed elsewhere (revision {row.revision}). Reload first.")
    return _set(db, row, normalize_settings(payload), "saved")


def reset_settings(db: Session, project_id: str) -> MixStudioSetting:
    return _set(db, get_setting(db, project_id), default_settings(), "reset to defaults")


def restore_previous(db: Session, project_id: str) -> MixStudioSetting:
    row = get_setting(db, project_id)
    history = list(row.history_json or [])
    if not history:
        raise AppError(409, "NO_PREVIOUS_SETTINGS", "There are no earlier mixer settings to restore.")
    entry = history.pop()
    row.history_json = history
    row.settings_json = normalize_settings(entry["settings"])
    row.revision = (row.revision or 1) + 1
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def preset_payload(row: MixPreset) -> dict:
    return {"id": row.id, "name": row.name, "settings": normalize_settings(row.settings_json),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def list_presets(db: Session, project_id: str) -> list[MixPreset]:
    _project(db, project_id)
    return list(db.scalars(select(MixPreset).where(MixPreset.project_id == project_id).order_by(MixPreset.name)))


def save_preset(db: Session, project_id: str, name: str, settings: dict | None, overwrite: bool) -> MixPreset:
    _project(db, project_id)
    name = (name or "").strip()
    if not name:
        raise AppError(422, "PRESET_NAME_REQUIRED", "Give the preset a name.")
    data = normalize_settings(settings if settings is not None else get_setting(db, project_id).settings_json)
    existing = db.scalar(select(MixPreset).where(MixPreset.project_id == project_id, MixPreset.name == name))
    if existing is not None:
        if not overwrite:
            raise AppError(409, "PRESET_EXISTS", f"A preset named “{name}” already exists. Confirm to overwrite it.")
        existing.settings_json = data
        db.commit()
        db.refresh(existing)
        return existing
    row = MixPreset(project_id=project_id, name=name[:120], settings_json=data)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def apply_preset(db: Session, project_id: str, preset_id: str) -> MixStudioSetting:
    preset = db.get(MixPreset, preset_id)
    if preset is None or preset.project_id != project_id:
        raise AppError(404, "PRESET_NOT_FOUND", "Preset not found.")
    return _set(db, get_setting(db, project_id), normalize_settings(preset.settings_json), f"applied preset “{preset.name}”")


def delete_preset(db: Session, project_id: str, preset_id: str) -> None:
    preset = db.get(MixPreset, preset_id)
    if preset is None or preset.project_id != project_id:
        raise AppError(404, "PRESET_NOT_FOUND", "Preset not found.")
    db.delete(preset)
    db.commit()


# ---------------------------------------------------------------------------
# Track sources
# ---------------------------------------------------------------------------


def _latest_asset(db: Session, video_id: str, kind: str) -> MediaAsset | None:
    return db.scalar(
        select(MediaAsset).where(MediaAsset.video_id == video_id, MediaAsset.kind == kind)
        .order_by(MediaAsset.version.desc(), MediaAsset.created_at.desc())
    )


def track_sources(db: Session, video: Video) -> dict[str, dict]:
    """Resolve every studio track to a real file, or explain why it is missing."""
    out: dict[str, dict] = {}
    for track in TRACKS:
        role, source = track["role"], track["source"]
        info = {"role": role, "label": track["label"], "kind": track["kind"], "available": False, "path": None,
                "url": None, "origin": None, "stale": False, "reason": None}
        if source.startswith("dub:"):
            language = source.split(":", 1)[1]
            timing = dub_timing.track_info(db, video, language)
            if timing and timing.get("exists"):
                info.update(available=True, path=str(storage.absolute_path(timing["track_key"])), url=timing["url"],
                            origin=f"Phase 11 {language.upper()} dialogue track", stale=bool(timing.get("stale")))
                if timing.get("stale"):
                    info["reason"] = "Timing changed after this track was rendered; re-render it in Dialogue Timing."
            else:
                info["reason"] = f"No {'Urdu' if language == 'ur' else 'Hindi'} dialogue track yet: generate voices (Phase 10) and fit timing (Phase 11)."
        else:
            asset = _latest_asset(db, video.id, source)
            path = storage.absolute_path(asset.storage_key) if asset else None
            if asset is not None and path.is_file():
                info.update(available=True, path=str(path), url=f"/api/v1/media/{asset.id}",
                            origin=f"Phase 4 separated stem ({(asset.metadata_json or {}).get('backend', 'separation')})")
            else:
                info["reason"] = "No separated stem yet: run Audio Separation (Phase 4)."
        out[role] = info
    source_audio = _latest_asset(db, video.id, "source_audio")
    out["_original_full"] = {
        "available": bool(source_audio and storage.absolute_path(source_audio.storage_key).is_file()),
        "url": f"/api/v1/media/{source_audio.id}" if source_audio else None,
    }
    return out


def included_roles(settings: dict, sources: dict, target: str) -> list[str]:
    """Tracks that actually reach the mix for ``target`` (enabled, unmuted, solo rules)."""
    tracks = settings["tracks"]
    candidates = []
    for role in ROLES:
        if role in ("urdu_dub", "hindi_dub") and DUB_ROLE.get(target) != role:
            continue
        if role == "original_dialogue" and target != "original" and not settings["keep_original_dialogue_in_dub"]:
            continue
        if not sources[role]["available"]:
            continue
        candidates.append(role)
    soloed = [role for role in candidates if tracks[role]["solo"] and tracks[role]["enabled"]]
    pool = soloed or candidates
    return [role for role in pool if tracks[role]["enabled"] and not tracks[role]["muted"]]


# ---------------------------------------------------------------------------
# Filter graph
# ---------------------------------------------------------------------------


def _lin(db: float) -> float:
    return 10 ** (db / 20)


_NOISE_FLOORS: dict[str, float] = {}


def estimate_noise_floor(path: str) -> float:
    """Noise floor (dBFS) of a file: RMS of its quietest 10% of 50 ms frames.

    ffmpeg's afftdn only removes noise near the floor it is given, so the floor
    is measured per file at the graph's 48 kHz rate (the floor depends on
    bandwidth), streamed in 1 s blocks with constant memory.
    """
    stat = Path(path).stat()
    key = f"{path}:{stat.st_size}:{int(stat.st_mtime)}"
    if key in _NOISE_FLOORS:
        return _NOISE_FLOORS[key]
    import numpy as np

    proc = subprocess.Popen([ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-i", path, "-ac", "1", "-ar", str(SAMPLE_RATE),
                             "-f", "f32le", "pipe:1"], stdout=subprocess.PIPE)
    frame = SAMPLE_RATE // 20  # 50 ms
    levels: list[float] = []
    carry = np.zeros(0, dtype=np.float32)
    while True:
        chunk = proc.stdout.read(SAMPLE_RATE * 4)  # type: ignore[union-attr]
        if not chunk:
            break
        data = np.concatenate([carry, np.frombuffer(chunk, dtype="<f4")])
        usable = data.size // frame * frame
        if usable:
            rms = np.sqrt(np.mean(data[:usable].reshape(-1, frame) ** 2, axis=1))
            levels.extend(rms.tolist())
        carry = data[usable:]
    proc.wait()
    values = np.array([v for v in levels if v > 1e-7]) if levels else np.zeros(0)
    if values.size == 0:
        floor = -80.0
    else:
        floor = float(20 * np.log10(np.percentile(values, 10)))
    floor = round(max(-80.0, min(-20.0, floor)), 1)
    _NOISE_FLOORS[key] = floor
    return floor


def _track_chain(track: dict, duration_s: float, window: tuple[float, float] | None, noise_floor_db: float | None = None) -> list[str]:
    chain = [f"aresample={SAMPLE_RATE}", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
    eq = track["eq"]
    if eq["enabled"]:
        if eq["highpass_hz"] > 20.5:
            chain.append(f"highpass=f={eq['highpass_hz']:.1f}:poles=2")
        if abs(eq["low_gain_db"]) >= 0.05:
            chain.append(f"bass=g={eq['low_gain_db']:.2f}:f=120")
        if abs(eq["mid_gain_db"]) >= 0.05:
            chain.append(f"equalizer=f={eq['mid_freq_hz']:.0f}:t=q:w=1.0:g={eq['mid_gain_db']:.2f}")
        if abs(eq["high_gain_db"]) >= 0.05:
            chain.append(f"treble=g={eq['high_gain_db']:.2f}:f=8000")
    if track["noise_reduction"]["enabled"] and noise_floor_db is not None:
        chain.append(f"afftdn=nr={track['noise_reduction']['amount_db']:.1f}:nf={noise_floor_db:.1f}")
    comp = track["compressor"]
    if comp["enabled"]:
        chain.append(
            "acompressor=threshold={:.6f}:ratio={:.2f}:attack={:.2f}:release={:.1f}:makeup={:.4f}:knee=2.5".format(
                max(0.000976563, _lin(comp["threshold_db"])), comp["ratio"], comp["attack_ms"], comp["release_ms"],
                min(64.0, max(1.0, _lin(comp["makeup_db"]))),
            )
        )
    level = track["volume_db"] + track["gain_db"]
    if abs(level) >= 0.01:
        chain.append(f"volume={level:.2f}dB")
    pan = track["pan"]
    if abs(pan) >= 0.01:  # balance law: the far side stays at unity
        chain.append(f"pan=stereo|c0={min(1.0, 1 - pan):.4f}*c0|c1={min(1.0, 1 + pan):.4f}*c1")
    chain += _fades(track["fade_in_ms"], track["fade_out_ms"], duration_s, window)
    return chain


def _fades(fade_in_ms: float, fade_out_ms: float, duration_s: float, window: tuple[float, float] | None) -> list[str]:
    """Fades on the film timeline; for a preview window they are offset accordingly."""
    offset = window[0] if window else 0.0
    length = window[1] if window else duration_s
    out = []
    if fade_in_ms > 0:
        start = 0.0 - offset
        if start + fade_in_ms / 1000 > 0:
            out.append(f"afade=t=in:st={max(0.0, start):.3f}:d={fade_in_ms / 1000:.3f}" if start >= 0 else
                       f"afade=t=in:st=0:d={fade_in_ms / 1000 + start:.3f}")
    if fade_out_ms > 0:
        start = duration_s - fade_out_ms / 1000 - offset
        if start < length:
            out.append(f"afade=t=out:st={max(0.0, start):.3f}:d={fade_out_ms / 1000:.3f}")
    return out


def build_graph(
    settings: dict,
    sources: dict,
    target: str,
    duration_s: float,
    window: tuple[float, float] | None = None,
    loudness_gain_db: float | None = None,
    measure: bool = False,
) -> tuple[list[str], str, list[str]]:
    """Return (ffmpeg input args, filter_complex, included roles)."""
    roles = included_roles(settings, sources, target)
    if not roles:
        raise AppError(409, "EMPTY_MIX", "No audible tracks for this mix (check availability, enable, mute and solo).")
    length = window[1] if window else duration_s
    inputs: list[str] = []
    parts: list[str] = []
    dialogue_labels, bed_labels = [], []
    for index, role in enumerate(roles):
        if window:
            inputs += ["-ss", f"{window[0]:.3f}", "-t", f"{window[1]:.3f}"]
        inputs += ["-i", sources[role]["path"]]
        track_settings = settings["tracks"][role]
        floor = estimate_noise_floor(sources[role]["path"]) if track_settings["noise_reduction"]["enabled"] else None
        chain = _track_chain(track_settings, duration_s, window, floor)
        chain += [f"apad=whole_dur={length:.3f}", f"atrim=end={length:.3f}", "asetpts=N/SR/TB"]
        label = f"t{index}"
        parts.append(f"[{index}:a]{','.join(chain)}[{label}]")
        (dialogue_labels if TRACK_BY_ROLE[role]["kind"] == "dialogue" else bed_labels).append(label)

    duck = settings["ducking"]
    dub_dialogue = [label for label, role in zip([f"t{i}" for i in range(len(roles))], roles) if role in ("urdu_dub", "hindi_dub")]
    mix_inputs: list[str] = []
    if duck["enabled"] and target in DUB_ROLE and dub_dialogue and bed_labels:
        key = dub_dialogue[0]
        parts.append(f"[{key}]asplit=2[{key}m][{key}k]")
        dialogue_labels = [f"{key}m" if label == key else label for label in dialogue_labels]
        bed_in = "".join(f"[{label}]" for label in bed_labels)
        if len(bed_labels) > 1:
            parts.append(f"{bed_in}amix=inputs={len(bed_labels)}:normalize=0:duration=longest[bed]")
        else:
            parts.append(f"{bed_in}anull[bed]")
        parts.append(
            "[bed][{}k]sidechaincompress=threshold={:.6f}:ratio={:.2f}:attack={:.1f}:release={:.1f}:makeup=1:knee=3[ducked]".format(
                key, max(0.000976563, _lin(duck["threshold_db"])), duck["ratio"], duck["attack_ms"], duck["release_ms"]
            )
        )
        mix_inputs = dialogue_labels + ["ducked"]
    else:
        mix_inputs = dialogue_labels + bed_labels
    joined = "".join(f"[{label}]" for label in mix_inputs)
    if len(mix_inputs) > 1:
        parts.append(f"{joined}amix=inputs={len(mix_inputs)}:normalize=0:duration=longest[mix]")
    else:
        parts.append(f"{joined}anull[mix]")

    master = settings["master"]
    chain = []
    gain = master["gain_db"] + (loudness_gain_db or 0.0)
    if abs(gain) >= 0.01:
        chain.append(f"volume={gain:.2f}dB")
    if measure:
        chain.append("ebur128=peak=true:framelog=quiet")
    else:
        if master["limiter"]["enabled"]:
            chain.append(f"alimiter=limit={_lin(master['limiter']['ceiling_db']):.5f}:attack=5:release=60:level=disabled:asc=1")
        chain += _fades(master["fade_in_ms"], master["fade_out_ms"], duration_s, window)
    chain.append(f"atrim=end={length:.3f}")
    parts.append(f"[mix]{','.join(chain)}[out]")
    return inputs, ";".join(parts), roles


# ---------------------------------------------------------------------------
# Running ffmpeg
# ---------------------------------------------------------------------------


def _run(args: list[str], total_s: float, progress: Callable[[float], None] | None, check_active: ActiveCheck | None) -> str:
    command = [ffmpeg_binary(), "-hide_banner", "-nostdin", "-y", *args]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    stderr_lines: list[str] = []
    import threading

    def drain() -> None:
        for line in proc.stderr:  # type: ignore[union-attr]
            stderr_lines.append(line)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            if line.startswith("out_time_us=") and progress and total_s > 0:
                try:
                    progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / total_s))
                except ValueError:
                    pass
            if check_active:
                try:
                    check_active()
                except Exception:
                    proc.kill()
                    raise
        proc.wait()
    finally:
        reader.join(timeout=5)
    stderr = "".join(stderr_lines)
    if proc.returncode != 0:
        tail = " ".join(stderr.strip().splitlines()[-4:])
        raise AppError(500, "MIX_RENDER_FAILED", f"ffmpeg failed: {tail[:600]}")
    return stderr


def _parse_ebur128(stderr: str) -> dict:
    summary = stderr[stderr.rfind("Summary:"):] if "Summary:" in stderr else stderr
    integrated = re.search(r"I:\s+(-?[\d.]+|-inf)\s+LUFS", summary)
    lra = re.search(r"LRA:\s+(-?[\d.]+)\s+LU", summary)
    peak = re.search(r"Peak:\s+(-?[\d.]+|-inf)\s+dBFS", summary)

    def value(match):
        if not match:
            return None
        return None if match.group(1) == "-inf" else float(match.group(1))

    return {"integrated_lufs": value(integrated), "lra": value(lra), "true_peak_dbfs": value(peak)}


def measure_loudness(settings: dict, sources: dict, target: str, duration_s: float, window=None,
                     progress=None, check_active=None) -> dict:
    inputs, graph, _roles = build_graph(settings, sources, target, duration_s, window, measure=True)
    stderr = _run([*inputs, "-filter_complex", graph, "-map", "[out]", "-f", "null", "-progress", "pipe:1", "-"],
                  window[1] if window else duration_s, progress, check_active)
    return _parse_ebur128(stderr)


def loudness_gain(settings: dict, measured: dict) -> float | None:
    loud = settings["master"]["loudness"]
    if not loud["enabled"] or measured.get("integrated_lufs") is None:
        return None
    return round(max(-30.0, min(30.0, loud["target_lufs"] - measured["integrated_lufs"])), 2)


def render_file(settings: dict, sources: dict, target: str, duration_s: float, output: Path, fmt: str,
                window=None, loudness_gain_db=None, progress=None, check_active=None) -> list[str]:
    inputs, graph, roles = build_graph(settings, sources, target, duration_s, window, loudness_gain_db)
    codec = ["-c:a", "pcm_s24le", "-f", "wav"] if fmt == "wav" else ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", "-f", "ipod"]
    output.parent.mkdir(parents=True, exist_ok=True)
    _run([*inputs, "-filter_complex", graph, "-map", "[out]", "-ar", str(SAMPLE_RATE), "-ac", "2", *codec,
          "-progress", "pipe:1", str(output)], window[1] if window else duration_s, progress, check_active)
    return roles


def validate_output(path: Path, expected_s: float) -> dict:
    """Decode the whole file, measure loudness/peaks and compare duration."""
    if not path.is_file() or path.stat().st_size < 64:
        return {"ok": False, "error": "Rendered file is missing or empty."}
    result = subprocess.run(
        [ffmpeg_binary(), "-hide_banner", "-nostdin", "-i", str(path), "-af", "ebur128=peak=true:framelog=quiet",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode != 0:
        return {"ok": False, "error": "Rendered file cannot be decoded."}
    stats = _parse_ebur128(result.stderr)
    duration = None
    for match in re.finditer(r"time=(\d+):(\d+):([\d.]+)", result.stderr):
        duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    probe = re.search(r"Audio: (\w+).*?(\d+) Hz, (\w+)", result.stderr)
    stats.update({
        "duration_s": round(duration, 3) if duration is not None else None,
        "codec": probe.group(1) if probe else None,
        "sample_rate": int(probe.group(2)) if probe else None,
        "channels": probe.group(3) if probe else None,
        "size_bytes": path.stat().st_size,
    })
    problems = []
    if duration is None or abs(duration - expected_s) > 0.06:
        problems.append(f"duration {duration}s differs from the film's {expected_s:.3f}s")
    if stats["integrated_lufs"] is None:
        problems.append("the mix is silent")
    stats["ok"] = not problems
    stats["problems"] = problems
    return stats


# ---------------------------------------------------------------------------
# High-level operations
# ---------------------------------------------------------------------------


def film_duration_s(video: Video) -> float:
    if not video.duration_ms:
        raise AppError(409, "DURATION_UNKNOWN", "The video duration is unknown; re-probe the video first.")
    return video.duration_ms / 1000


def studio_bundle(db: Session, video: Video) -> dict:
    from ..schemas import JobOut

    row = get_setting(db, video.project_id)
    settings = normalize_settings(row.settings_json)
    sources = track_sources(db, video)
    renders = list(db.scalars(select(MixRender).where(MixRender.video_id == video.id).order_by(MixRender.created_at.desc()).limit(20)))
    job = db.scalar(select(ProcessingJob).where(ProcessingJob.video_id == video.id, ProcessingJob.type == "mix_render")
                    .order_by(ProcessingJob.created_at.desc()))
    tracks = []
    for track in TRACKS:
        source = sources[track["role"]]
        tracks.append({**{k: v for k, v in source.items() if k != "path"},
                       "included": {target: track["role"] in included_roles(settings, sources, target) for target in TARGETS}})
    return {
        "video_id": video.id,
        "project_id": video.project_id,
        "duration_ms": video.duration_ms,
        "settings": settings_payload(row),
        "tracks": tracks,
        "original_full": sources["_original_full"],
        "presets": [preset_payload(item) for item in list_presets(db, video.project_id)],
        "renders": [render_payload(item) for item in renders],
        "job": JobOut.model_validate(job).model_dump(mode="json") if job else None,
        "live_controls": LIVE_CONTROLS,
        "render_only_controls": RENDER_ONLY_CONTROLS,
        "sample_rate": SAMPLE_RATE,
    }


def render_payload(row: MixRender) -> dict:
    return {
        "id": row.id, "target": row.target, "format": row.format, "status": row.status, "tracks": row.tracks_json,
        "size_bytes": row.size_bytes, "duration_ms": row.duration_ms, "stats": row.stats_json, "error_message": row.error_message,
        "url": f"/api/v1/mix-renders/{row.id}/file" if row.storage_key and row.status == "ready" else None,
        "download_url": f"/api/v1/mix-renders/{row.id}/file?download=1" if row.storage_key and row.status == "ready" else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def preview_window(db: Session, video: Video, target: str, start_ms: int, length_ms: int, settings_override: dict | None) -> bytes:
    """Render a short window with *exactly* the render processing (incl. render-only effects)."""
    if target not in TARGETS:
        raise AppError(422, "INVALID_TARGET", "Target must be original, ur or hi.")
    duration_s = film_duration_s(video)
    start_s = max(0.0, min(start_ms / 1000, duration_s - 0.5))
    length_s = max(1.0, min(length_ms / 1000, 30.0, duration_s - start_s))
    settings = normalize_settings(settings_override) if settings_override is not None else normalize_settings(get_setting(db, video.project_id).settings_json)
    sources = track_sources(db, video)
    gain = None
    if settings["master"]["loudness"]["enabled"]:
        cached = _cached_measurement(db, video, target, settings)
        if cached is not None:
            gain = loudness_gain(settings, cached)
        else:  # approximate: measure the window itself
            gain = loudness_gain(settings, measure_loudness(settings, sources, target, duration_s, (start_s, length_s)))
    import tempfile

    with tempfile.TemporaryDirectory(prefix="mixprev-") as tmp:
        out = Path(tmp) / "preview.wav"
        render_file(settings, sources, target, duration_s, out, "wav", (start_s, length_s), gain)
        return out.read_bytes()


def _cached_measurement(db: Session, video: Video, target: str, settings: dict) -> dict | None:
    meta = (video.metadata_json or {}).get("mix_loudness") or {}
    entry = meta.get(target)
    if entry and entry.get("fingerprint") == _fingerprint(settings) and entry.get("sources") == _sources_fingerprint(track_sources(db, video)):
        return entry["measured"]
    return None


def _sources_fingerprint(sources: dict) -> str:
    parts = []
    for role in ROLES:
        path = sources[role]["path"]
        if path and Path(path).is_file():
            stat = Path(path).stat()
            parts.append(f"{role}:{path}:{stat.st_size}:{int(stat.st_mtime)}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def create_render_job(db: Session, video: Video, target: str, fmt: str) -> ProcessingJob:
    from .jobs import create_import_job

    if target not in TARGETS or fmt not in FORMATS:
        raise AppError(422, "INVALID_RENDER_REQUEST", "Choose target original/ur/hi and format wav/aac.")
    film_duration_s(video)
    settings = normalize_settings(get_setting(db, video.project_id).settings_json)
    included_roles(settings, track_sources(db, video), target) or (_ for _ in ()).throw(
        AppError(409, "EMPTY_MIX", "No audible tracks for this mix (check availability, enable, mute and solo).")
    )
    active = db.scalar(select(ProcessingJob).where(ProcessingJob.video_id == video.id, ProcessingJob.type == "mix_render",
                                                   ProcessingJob.status.in_(["queued", "running"])))
    if active:
        raise AppError(409, "MIX_RENDER_ACTIVE", "A mix is already rendering for this video.")
    return create_import_job(db, video.project_id, video.id, "mix_render", RENDER_STEP_NAMES,
                             {"target": target, "format": fmt, "settings": settings})


def run_render_job(job_id: str, video_id: str, options: dict | None = None) -> None:
    from .jobs import JobCancelled, _job_status, _set_step

    db = SessionLocal()
    render: MixRender | None = None
    try:
        job = db.get(ProcessingJob, job_id)
        video = db.get(Video, video_id)
        if not job or not video:
            return
        opts = options or job.options_json or {}
        target, fmt = opts.get("target", "ur"), opts.get("format", "wav")
        settings = normalize_settings(opts.get("settings") or get_setting(db, video.project_id).settings_json)

        def check_active() -> None:
            status = _job_status(job_id)
            if status is None or status == "cancelled":
                raise JobCancelled("Job was cancelled or removed")

        claim = db.execute(update(ProcessingJob).where(ProcessingJob.id == job_id, ProcessingJob.status == "queued").values(status="running"))
        db.commit()
        if claim.rowcount != 1:
            db.refresh(job)
            if job.status != "cancelled":
                return
            raise JobCancelled("Job was cancelled before it started")
        db.refresh(job)

        _set_step(db, job, "collect_tracks", "running", "Resolving audio files", 3)
        duration_s = film_duration_s(video)
        sources = track_sources(db, video)
        roles = included_roles(settings, sources, target)
        if not roles:
            raise AppError(409, "EMPTY_MIX", "No audible tracks for this mix.")
        stale = [TRACK_BY_ROLE[r]["label"] for r in roles if sources[r]["stale"]]
        render = MixRender(id=new_id(), project_id=video.project_id, video_id=video.id, job_id=job_id, target=target, format=fmt,
                           status="rendering", settings_json=settings, tracks_json=[{"role": r, "label": TRACK_BY_ROLE[r]["label"],
                           "origin": sources[r]["origin"]} for r in roles], stats_json={})
        db.add(render)
        db.commit()
        _set_step(db, job, "collect_tracks", "completed",
                  ", ".join(TRACK_BY_ROLE[r]["label"] for r in roles) + (f" (outdated: {', '.join(stale)})" if stale else ""), 6)

        def progress_to(low: int, high: int, step: str):
            def update_progress(fraction: float) -> None:
                job.progress = low + int((high - low) * fraction)
                job.current_step = step
                db.commit()
            return update_progress

        gain = None
        if settings["master"]["loudness"]["enabled"]:
            _set_step(db, job, "measure_loudness", "running", "Measuring integrated loudness (EBU R128)", 7)
            measured = measure_loudness(settings, sources, target, duration_s, progress=progress_to(7, 40, "measure_loudness"), check_active=check_active)
            gain = loudness_gain(settings, measured)
            meta = dict(video.metadata_json or {})
            loud = dict(meta.get("mix_loudness") or {})
            loud[target] = {"fingerprint": _fingerprint(settings), "sources": _sources_fingerprint(sources), "measured": measured}
            meta["mix_loudness"] = loud
            video.metadata_json = meta
            db.commit()
            _set_step(db, job, "measure_loudness", "completed",
                      f"{measured.get('integrated_lufs')} LUFS → gain {gain:+.2f} dB" if gain is not None else "silent mix", 40)
        else:
            _set_step(db, job, "measure_loudness", "completed", "loudness normalisation off", 40)

        _set_step(db, job, "render_mix", "running", f"Rendering {fmt.upper()}", 41)
        ext = "wav" if fmt == "wav" else "m4a"
        key = f"projects/{video.project_id}/mixes/{render.id}.{ext}"
        output = storage.absolute_path(key)
        render_file(settings, sources, target, duration_s, output, fmt, None, gain, progress_to(41, 90, "render_mix"), check_active)
        _set_step(db, job, "render_mix", "completed", f"{output.stat().st_size / 1e6:.1f} MB", 90)

        _set_step(db, job, "validate", "running", "Decoding and measuring the output", 92)
        stats = validate_output(output, duration_s)
        stats["loudness_gain_db"] = gain
        stats["target_lufs"] = settings["master"]["loudness"]["target_lufs"] if settings["master"]["loudness"]["enabled"] else None
        render.storage_key = key
        render.size_bytes = stats.get("size_bytes")
        render.duration_ms = int(round((stats.get("duration_s") or 0) * 1000))
        render.stats_json = stats
        render.status = "ready" if stats["ok"] else "invalid"
        render.error_message = "; ".join(stats.get("problems") or []) or None
        db.commit()
        _set_step(db, job, "validate", "completed" if stats["ok"] else "failed",
                  f"{stats.get('integrated_lufs')} LUFS · true peak {stats.get('true_peak_dbfs')} dBFS · {stats.get('duration_s')}s"
                  + ("" if stats["ok"] else f" · {render.error_message}"), 97)
        if not stats["ok"]:
            raise AppError(500, "MIX_VALIDATION_FAILED", render.error_message or "Rendered mix failed validation.")
        final = db.execute(update(ProcessingJob).where(ProcessingJob.id == job_id, ProcessingJob.status == "running")
                           .values(status="completed", current_step="finalize", progress=100, error_code=None, error_message=None))
        if final.rowcount != 1:
            db.rollback()
            raise JobCancelled("Job was cancelled before finishing")
        db.commit()
        db.refresh(job)
        _set_step(db, job, "finalize", "completed", f"{target.upper()} mix ready ({fmt.upper()})", 100)
    except Exception as error:  # noqa: BLE001
        from .jobs import JobCancelled as _Cancelled

        db.rollback()
        if render is not None:
            current_render = db.get(MixRender, render.id)
            if current_render is not None and current_render.status != "invalid":
                current_render.status = "cancelled" if isinstance(error, _Cancelled) else "failed"
                current_render.error_message = str(error)[:900]
                if current_render.storage_key is None:
                    partial = storage.absolute_path(f"projects/{current_render.project_id}/mixes/{current_render.id}.{'wav' if current_render.format == 'wav' else 'm4a'}")
                    partial.unlink(missing_ok=True)
        current = db.get(ProcessingJob, job_id)
        if current is None:
            db.commit()
            return
        if isinstance(error, _Cancelled) or current.status == "cancelled":
            current.status, current.error_code, current.error_message = "cancelled", "CANCELLED", "Cancelled by user"
            final_status, detail = "cancelled", "Cancelled by user"
        else:
            current.status = "failed"
            current.error_code = error.code if isinstance(error, AppError) else "MIX_RENDER_FAILED"
            current.error_message = str(error)[:1000]
            final_status, detail = "failed", str(error)[:500]
        for step in current.steps:
            if step.status == "running" or (final_status == "cancelled" and step.status == "pending"):
                step.status, step.detail, step.finished_at = final_status, detail, utcnow()
        db.commit()
    finally:
        db.close()
