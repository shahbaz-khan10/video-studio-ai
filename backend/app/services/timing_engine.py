"""Phase 11 — dialogue fitting engine (pure audio; no database).

Given one synthetic take and the time window its original line occupied, find
the least noticeable way to make it fit, in this order:

1. as generated (natural speed, placed at the original start)
2. shorten long internal pauses (never below ``min_pause_ms``)
3. use the silent gap after the line (never into another speaker's line)
4. a small lead into the silence before the line
5. gentle speed-up, pitch preserved (≤ ``natural_tempo``)
6. noticeable speed-up (≤ ``max_tempo``) — flagged for review
7. otherwise: place a best-effort clip, report the overflow and suggest fixes.

Words are never removed: only silence below a low threshold is trimmed, with
safety margins around speech, and the voiced duration is verified after
processing. Speech is never slowed down to fill a window.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import numpy as np
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..config import PROJECT_ROOT
from .tts_engine import ffmpeg_binary


class TimingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DUB_TIMING_", env_file=str(PROJECT_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    natural_tempo: float = 1.12
    max_tempo: float = 1.25
    min_pause_ms: int = 180
    pause_detect_ms: int = 220
    silence_dbfs: float = -42.0
    onset_margin_ms: int = 40
    tail_margin_ms: int = 70
    max_tail_ms: int = 700
    max_lead_ms: int = 200
    guard_ms: int = 80
    overlap_tolerance_ms: int = 120
    # Very short clips (single words) degrade quickly when sped up.
    short_clip_ms: int = 700


timing_settings = TimingSettings()


class TimingError(Exception):
    pass


@dataclass
class Window:
    """The time a line may occupy, plus the free silence around it."""

    start_ms: int
    end_ms: int
    lead_room_ms: int = 0  # silence available before start (after the previous line)
    tail_room_ms: int = 0  # silence available after end (before the next line)

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


@dataclass
class FitResult:
    audio: np.ndarray
    sample_rate: int
    status: str  # fitted | fitted_fast | needs_attention
    strategy: str
    tempo: float
    placed_start_ms: int
    placed_end_ms: int
    take_duration_ms: int
    speech_duration_ms: int
    fitted_duration_ms: int
    pause_saved_ms: int
    overflow_ms: int
    lead_used_ms: int
    tail_used_ms: int
    voiced_ratio: float
    flags: list[dict] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)


def _flag(code: str, severity: str, message: str) -> dict:
    return {"code": code, "severity": severity, "message": message}


def _frame_energy(audio: np.ndarray, sr: int, frame_ms: int = 10) -> tuple[np.ndarray, int]:
    frame = max(1, int(sr * frame_ms / 1000))
    usable = audio[: len(audio) // frame * frame]
    if usable.size == 0:
        return np.zeros(0), frame
    rms = np.sqrt(np.mean(usable.reshape(-1, frame) ** 2, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-9)), frame


def voiced_mask(audio: np.ndarray, sr: int) -> tuple[np.ndarray, int]:
    energy, frame = _frame_energy(audio, sr)
    return energy > timing_settings.silence_dbfs, frame


def voiced_ms(audio: np.ndarray, sr: int) -> int:
    mask, frame = voiced_mask(audio, sr)
    return int(mask.sum() * frame * 1000 / sr)


def trim_edges(audio: np.ndarray, sr: int) -> np.ndarray:
    """Remove leading/trailing silence, keeping margins so onsets and tails survive."""
    mask, frame = voiced_mask(audio, sr)
    if not mask.any():
        return audio[:0]
    first = int(np.argmax(mask)) * frame
    last = (len(mask) - int(np.argmax(mask[::-1]))) * frame
    start = max(0, first - int(sr * timing_settings.onset_margin_ms / 1000))
    end = min(len(audio), last + int(sr * timing_settings.tail_margin_ms / 1000))
    return audio[start:end]


def compress_pauses(audio: np.ndarray, sr: int, needed_ms: int) -> tuple[np.ndarray, int]:
    """Shorten internal pauses (longest first) by up to ``needed_ms`` total.

    Each pause keeps at least ``min_pause_ms`` so phrasing stays natural.
    Returns (audio, saved_ms).
    """
    if needed_ms <= 0:
        return audio, 0
    mask, frame = voiced_mask(audio, sr)
    pauses: list[tuple[int, int]] = []
    index = 0
    while index < len(mask):
        if not mask[index]:
            run_start = index
            while index < len(mask) and not mask[index]:
                index += 1
            # internal only: speech on both sides
            if run_start > 0 and index < len(mask):
                length_ms = (index - run_start) * frame * 1000 / sr
                if length_ms >= timing_settings.pause_detect_ms:
                    pauses.append((run_start * frame, index * frame))
        else:
            index += 1
    if not pauses:
        return audio, 0
    keep = int(sr * timing_settings.min_pause_ms / 1000)
    budget = int(sr * needed_ms / 1000)
    cuts: list[tuple[int, int]] = []
    for start, end in sorted(pauses, key=lambda item: item[0] - item[1]):  # longest first
        removable = (end - start) - keep
        if removable <= 0 or budget <= 0:
            continue
        take = min(removable, budget)
        middle = start + (end - start) // 2
        cuts.append((middle - take // 2, middle - take // 2 + take))
        budget -= take
    if not cuts:
        return audio, 0
    pieces, cursor = [], 0
    fade = max(1, int(sr * 0.008))
    for cut_start, cut_end in sorted(cuts):
        piece = audio[cursor:cut_start].copy()
        if piece.size > fade:
            piece[-fade:] *= np.linspace(1, 0, fade)
        pieces.append(piece)
        cursor = cut_end
    tail = audio[cursor:].copy()
    if tail.size > fade:
        tail[:fade] *= np.linspace(0, 1, fade)
    pieces.append(tail)
    result = np.concatenate(pieces)
    return result, int(round((len(audio) - len(result)) * 1000 / sr))


def stretch(audio: np.ndarray, sr: int, tempo: float) -> np.ndarray:
    """Pitch-preserving time compression with rubberband (formants preserved)."""
    if abs(tempo - 1.0) < 0.005 or audio.size == 0:
        return audio
    result = subprocess.run(
        [
            ffmpeg_binary(), "-hide_banner", "-loglevel", "error",
            "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
            "-af", f"rubberband=tempo={tempo:.4f}:pitch=1:formant=preserved:pitchq=quality:transients=crisp",
            "-f", "f32le", "-ac", "1", "pipe:1",
        ],
        input=audio.astype("<f4").tobytes(),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise TimingError(f"Time-stretch failed: {result.stderr[:300]!r}")
    return np.frombuffer(result.stdout, dtype="<f4").copy()


def _limit(audio: np.ndarray, ceiling: float = 0.891) -> np.ndarray:
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    return audio * (ceiling / peak) if peak > ceiling else audio


def fit(audio: np.ndarray, sr: int, window: Window) -> FitResult:
    """Fit one take into ``window`` using the gentlest strategy that works."""
    if window.duration_ms <= 0:
        raise TimingError("The dialogue window has no duration.")
    s = timing_settings
    take_ms = int(round(len(audio) * 1000 / sr))
    original_voiced = voiced_ms(audio, sr)
    speech = trim_edges(audio, sr)
    if speech.size == 0:
        raise TimingError("The take contains no audible speech.")
    speech_ms = int(round(len(speech) * 1000 / sr))
    steps = [f"trimmed edge silence {take_ms - speech_ms} ms"] if take_ms - speech_ms > 5 else []
    target = window.duration_ms
    tail_room = max(0, min(window.tail_room_ms, s.max_tail_ms))
    lead_room = max(0, min(window.lead_room_ms, s.max_lead_ms))

    def result(clip: np.ndarray, status: str, strategy: str, tempo: float, saved: int, lead: int, tail_limit: int) -> FitResult:
        clip = _limit(clip)
        dur = int(round(len(clip) * 1000 / sr))
        start = window.start_ms - lead
        end = start + dur
        allowed_end = window.end_ms + tail_limit
        overflow = max(0, end - allowed_end)
        used_tail = max(0, min(end, allowed_end) - window.end_ms)
        voiced_after = voiced_ms(clip, sr)
        expected = original_voiced / max(tempo, 1e-6)
        ratio = voiced_after / expected if expected else 1.0
        flags: list[dict] = []
        if ratio < 0.85:
            flags.append(_flag("speech_loss", "error", "Processing reduced audible speech; check this line."))
        if status == "fitted_fast":
            flags.append(_flag("fast_speech", "warning", f"Sped up ×{tempo:.2f} to fit; listen for naturalness."))
        if overflow:
            flags.append(
                _flag("overflow", "error", f"Still {overflow} ms longer than the available time even at ×{tempo:.2f}.")
            )
        return FitResult(
            audio=clip, sample_rate=sr, status=status, strategy=strategy, tempo=round(tempo, 3),
            placed_start_ms=start, placed_end_ms=end, take_duration_ms=take_ms, speech_duration_ms=speech_ms,
            fitted_duration_ms=dur, pause_saved_ms=saved, overflow_ms=overflow, lead_used_ms=lead,
            tail_used_ms=used_tail, voiced_ratio=round(ratio, 3), flags=flags, steps=list(steps),
        )

    # 1. as generated
    if speech_ms <= target:
        return result(speech, "fitted", "natural", 1.0, 0, 0, 0)

    # 2. shorten long pauses
    compressed, saved = compress_pauses(speech, sr, speech_ms - target)
    compressed_ms = speech_ms - saved
    if saved:
        steps.append(f"shortened pauses by {saved} ms")
    if compressed_ms <= target:
        return result(compressed, "fitted", "pauses", 1.0, saved, 0, 0)

    # 3. use the silent gap after the line
    if compressed_ms <= target + tail_room:
        steps.append(f"extended {compressed_ms - target} ms into the following silence")
        return result(compressed, "fitted", "tail_gap", 1.0, saved, 0, tail_room)

    # 4. small lead into the silence before the line
    if compressed_ms <= target + tail_room + lead_room:
        lead = compressed_ms - target - tail_room
        steps.append(f"started {lead} ms early, extended {tail_room} ms after")
        return result(compressed, "fitted", "lead_and_tail", 1.0, saved, lead, tail_room)

    room = target + tail_room + lead_room
    needed = compressed_ms / room
    max_tempo = s.natural_tempo if compressed_ms < s.short_clip_ms else s.max_tempo
    # 5. gentle speed-up
    if needed <= s.natural_tempo:
        tempo = max(1.0, needed * 1.005)
        clip = stretch(compressed, sr, tempo)
        lead = max(0, int(round(len(clip) * 1000 / sr)) - target - tail_room)
        steps.append(f"natural speed-up ×{tempo:.2f}")
        return result(clip, "fitted", "gentle_tempo", tempo, saved, min(lead, lead_room), tail_room)
    # 6. noticeable speed-up
    if needed <= max_tempo:
        tempo = needed * 1.005
        clip = stretch(compressed, sr, tempo)
        lead = max(0, int(round(len(clip) * 1000 / sr)) - target - tail_room)
        steps.append(f"noticeable speed-up ×{tempo:.2f}")
        return result(clip, "fitted_fast", "fast_tempo", tempo, saved, min(lead, lead_room), tail_room)
    # 7. cannot fit naturally: best effort at the maximum speed, nothing cut
    clip = stretch(compressed, sr, max_tempo)
    steps.append(f"capped at ×{max_tempo:.2f}" + (" (short word)" if max_tempo < s.max_tempo else "") + "; does not fit")
    return result(clip, "needs_attention", "overflow", max_tempo, saved, lead_room, tail_room)


def suggestions(fit_result: FitResult, window: Window, syllables: int | None) -> list[dict]:
    """Concrete alternatives when a line is fast or does not fit. Never auto-applied."""
    if fit_result.status == "fitted":
        return []
    out: list[dict] = []
    natural_room = window.duration_ms + min(window.tail_room_ms, timing_settings.max_tail_ms) + min(
        window.lead_room_ms, timing_settings.max_lead_ms
    )
    natural_len = (fit_result.speech_duration_ms - fit_result.pause_saved_ms) / timing_settings.natural_tempo
    if natural_len > 0 and natural_room > 0:
        keep_ratio = min(1.0, natural_room / natural_len)
        cut_pct = int(round((1 - keep_ratio) * 100))
        if cut_pct > 0:
            text = f"Shorten the translation by about {cut_pct}%"
            if syllables:
                text += f" (~{max(1, int(round(syllables * (1 - keep_ratio))))} of {syllables} syllables)"
            out.append({"action": "shorten_translation", "message": text + " so it fits at natural speed.", "cut_percent": cut_pct})
    speed_factor = min(1.2, max(1.05, round(fit_result.speech_duration_ms / max(1, natural_room) / timing_settings.natural_tempo, 2)))
    out.append({"action": "regenerate_faster", "message": f"Regenerate the voice at ×{speed_factor:.2f} speaking rate.", "speed_factor": speed_factor})
    out.append({"action": "manual_timing", "message": "Move the start earlier or extend the end in the timeline."})
    if fit_result.overflow_ms:
        out.append({"action": "allow_overlap", "message": f"Allow up to {fit_result.overflow_ms} ms overlap with the next line (if it suits the scene)."})
    return out
